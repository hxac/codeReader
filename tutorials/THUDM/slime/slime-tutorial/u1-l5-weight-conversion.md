# 模型权重转换：HF 与 Megatron torch_dist 互转

## 1. 本讲目标

slime 的训练后端是 Megatron，而模型作者发布的初始权重几乎都是 HuggingFace（HF）格式。这两者**不能直接互读**。本讲要解决的就是「格式不匹配」这一关卡。读完本讲，你应该能够：

- 说清 HF 格式与 Megatron `torch_dist` 格式的本质差异，以及为什么必须转换。
- 会用 `tools/convert_hf_to_torch_dist.py` 把一个 HF 检查点转成 Megatron 可训练的 `torch_dist`。
- 会用 `tools/convert_torch_dist_to_hf.py` 把训练保存的 Megatron 检查点转回 HF，供推理或发布。
- 理解转换过程中 `model_provider` 如何先用参数搭出一个「空壳」Megatron 模型，再把 HF 权重「灌」进去。
- 知道为什么 `scripts/models/*.sh` 必须手写一堆 Megatron 参数。

本讲承接 [u1-l3 环境搭建](u1-l3-environment-setup.md)：你已经能在 Docker 镜像里 `import slime`，接下来要把第一个模型权重准备好，为 [u1-l4 运行第一个训练](u1-l4-first-training-run.md) 中的 `--ref-load` 提供 `torch_dist` 检查点。

## 2. 前置知识

### 2.1 什么是「检查点格式」

一个训练好的模型，本质是一堆数字（张量，tensor）。把这些张量连同它们的「名字」（参数名）一起存到磁盘上，就形成一个**检查点（checkpoint）**。不同的训练框架给参数起的名字、切分方式、落盘格式都不一样，这就是「格式」差异的来源。

- **HuggingFace 格式**：业界事实标准。权重用 `safetensors` 文件存，旁边有 `config.json` 描述模型结构。参数名形如 `model.layers.0.self_attn.q_proj.weight`，每个张量是**完整未切分**的。
- **Megatron `torch_dist` 格式**：NVIDIA Megatron-LM 的分布式检查点格式。它用的是 PyTorch 的 `torch.distributed.checkpoint` 机制，权重**按并行策略（TP/PP/EP）预先分片**存储，并带一份 `.metadata` 描述每个分片落在哪个 rank。

### 2.2 为什么 Megatron 不能直接读 HF

有两个硬约束：

1. **Megatron 不会从 `config.json` 自动推断结构。** 它要求你在命令行显式给出 `--num-layers`、`--hidden-size`、`--num-attention-heads` 等参数，再用这些参数搭模型。这就是为什么需要 `scripts/models/*.sh`。
2. **参数命名与布局完全不同。** Megatron 内部把 QKV 合在一起、按 TP 维切；HF 则把 `q_proj/k_proj/v_proj` 分开、不切。直接读名字都对不上。

所以转换的本质是：**按 Megatron 的并行策略搭一个空模型 → 把 HF 的张量逐一改名、reshape、切分后填进去 → 用 Megatron 自己的保存逻辑写成 `torch_dist`**。

### 2.3 一个直觉比喻

把转换想象成「搬家重新收纳」：

- HF 权重 = 一个个独立打包好的纸箱（每个矩阵一箱，完整）。
- `torch_dist` 权重 = 按新家（GPU 拓扑）的柜子尺寸重新切割、分装到不同房间（rank）的抽屉里。
- 转换脚本就是搬家公司：先按新房图纸把空柜子立起来（`model_provider`），再把旧纸箱里的东西拆开、按新尺寸裁剪、塞进对应的抽屉。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `tools/convert_hf_to_torch_dist.py` | HF → torch_dist 转换入口 | 参数解析、PP 自动推导、搭空模型、灌权重、保存 |
| `tools/convert_torch_dist_to_hf.py` | torch_dist → HF 转换入口 | 读分片检查点、展开层/专家、改名、分块写 safetensors |
| `slime/backends/megatron_utils/model_provider.py` | Megatron 模型构建器 | 用参数搭 GPTModel 空壳、critic 输出头、参数冻结 |
| `slime/backends/megatron_utils/hf_to_megatron/__init__.py` | HF 权重装载分发表 | 按 `model_type` 选对应的张量映射函数 |
| `slime/backends/megatron_utils/megatron_to_hf/__init__.py` | Megatron → HF 张量转换分发表 | 按模型名选反向映射 + 去 padding |
| `slime/backends/megatron_utils/megatron_to_hf/processors/padding_remover.py` | 去 vocab padding | 还原被 Megatron 补齐的词表 |
| `scripts/models/qwen3-0.6B.sh` | 示例模型参数 | 给 Megatron 的结构参数 |

> 提示：转换脚本本身只是「调度器」，真正干「改名+切分」脏活的是 `hf_to_megatron/` 和 `megatron_to_hf/` 两套按模型族（qwen/glm/deepseek…）实现的映射函数。本讲聚焦调度流程与 `model_provider`，映射函数的逐行细节留到进阶层。

## 4. 核心概念与源码讲解

### 4.1 权重格式差异与 model_provider 模型构建

#### 4.1.1 概念说明

无论方向是 HF→Megatron 还是反向，都绕不开一个核心问题：**Megatron 怎么知道要搭一个什么样的模型？**

答案是：用命令行参数。Megatron 不读 `config.json`，它要求你把模型结构用一长串参数描述出来。`slime` 在 `scripts/models/` 目录为每个支持模型预置了这份参数（例如 [scripts/models/qwen3-0.6B.sh](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/scripts/models/qwen3-0.6B.sh#L1-L17)），启动时 `source` 进来即可。

`model_provider` 就是「吃下这些参数 → 产出一个 Megatron `GPTModel` 实例」的工厂函数。它在转换和正式训练里都被复用，是理解整个 Megatron 后端的入口之一。

#### 4.1.2 核心流程

`model_provider` 的构建流程：

```text
命令行参数 (num-layers / hidden-size / num-experts / spec ...)
        │
        ▼
core_transformer_config_from_args(args)   # 参数 → TransformerConfig
        │
        ▼
选择 layer spec：
  ├─ 有 num_experts  → get_gpt_decoder_block_spec   (MoE 整块 spec)
  └─ 无 num_experts  → TE spec 或 local spec         (稠密层 spec)
        │
        ▼
GPTModel(config, transformer_layer_spec, vocab_size=padded_vocab_size, ...)
        │
        ▼
（若 role=="critic"）替换 output_layer 为标量输出头 LinearForLastLayer
        │
        ▼
freeze_model_params(model, args)          # 只训练/冻结指定正则的参数
        │
        ▼
返回 GPTModel
```

关键点：此时模型里的权重是**随机初始化的空壳**，转换脚本随后会把 HF 的真实权重覆盖进去。

#### 4.1.3 源码精读

入口 `get_model_provider_func` 只是做一层「冻结」包装，真正的工厂在 `_get_model_provider_func`：

- [slime/backends/megatron_utils/model_provider.py:229-230](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model_provider.py#L229-L230)：`get_model_provider_func` 把内部工厂包上 `freeze_model_params`，返回一个供 Megatron `get_model()` 调用的 provider。

- [slime/backends/megatron_utils/model_provider.py:120-145](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model_provider.py#L120-L145)：根据是否有 `num_experts` 与是否用 TransformerEngine（`use_te`），选择稠密层 spec（`get_gpt_layer_with_transformer_engine_spec` / `get_gpt_layer_local_spec`）或 MoE 块 spec（`get_gpt_decoder_block_spec`）。这是「按参数决定模型结构」的核心。

- [slime/backends/megatron_utils/model_provider.py:164-196](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model_provider.py#L164-L196)：用 `vocab_size=args.padded_vocab_size`、`share_embeddings_and_output_weights` 等关键字参数实例化 `GPTModel`。注意用的是 `padded_vocab_size`（被补齐后的词表大小），这就是后续「去 padding」问题的源头。

- [slime/backends/megatron_utils/model_provider.py:198-199](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model_provider.py#L198-L199)：当 `role=="critic"` 时，把输出层换成 `LinearForLastLayer(output_size=1)`，即打分头输出标量价值。

- [slime/backends/megatron_utils/model_provider.py:233-247](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model_provider.py#L233-L247)：`freeze_model_params` 用正则列表 `only_train_params_name_list`（先全冻结、命中则解冻）和 `freeze_params_name_list`（命中则冻结）控制哪些参数参与训练。本讲转换阶段不关心冻结，但训练阶段会用到。

模型参数从哪来？以 Qwen3-0.6B 为例：

- [scripts/models/qwen3-0.6B.sh:1-17](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/scripts/models/qwen3-0.6B.sh#L1-L17)：定义 `MODEL_ARGS` 数组，含 `--num-layers 28`、`--hidden-size 1024`、`--group-query-attention`、`--rotary-base 1000000`、`--vocab-size 151936` 等。`source` 这个文件后，`${MODEL_ARGS[@]}` 就能传给转换脚本和训练脚本。

#### 4.1.4 代码实践

**目标**：理解「Megatron 用参数搭空模型」，亲眼看到参数如何决定结构。

**步骤**：

1. 打开 [scripts/models/qwen3-0.6B.sh](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/scripts/models/qwen3-0.6B.sh#L1-L17)，逐行对照 Qwen3-0.6B 的 `config.json`，确认每个参数的来源（例如 `--num-attention-heads 16` ↔ `num_attention_heads`，`--group-query-attention` + `--num-query-groups 8` ↔ GQA）。
2. 打开 [slime/backends/megatron_utils/model_provider.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model_provider.py#L120-L145) 第 120–145 行，标出：Qwen3-0.6B 没有 `num_experts`，会走 `get_gpt_layer_with_transformer_engine_spec`（若用 TE）这条分支。
3. 在笔记里回答：如果误把 `--rotary-base` 写错（比如写成 `10000`），Megatron 在搭模型时会报错吗？还是静默接受、只是行为错误？

**预期结果**：Megatron 只校验参数的「合法性」（类型、范围），不校验参数是否和某个外部模型一致。所以 `--rotary-base` 写错不会报错，但训练出来的模型位置编码会全错——这正是文档反复强调「务必核对 `scripts/models/*.sh` 参数」的原因。

> 待本地验证：如果你有环境，可尝试 `source scripts/models/qwen3-0.6B.sh` 后 `echo ${MODEL_ARGS[@]}`，确认数组被正确加载。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `scripts/models/*.sh` 必须为每个模型单独维护一份参数，而不能像 HF 那样只读 `config.json`？

**答案**：因为 Megatron 的设计哲学是「显式优于隐式」——它要求调用方在命令行精确声明模型结构与并行策略，而不从检查点或 config 自动推断。HF 格式把结构信息内嵌在 `config.json`，Megatron 不解析它，所以必须人工把这些信息翻译成 Megatron 参数。

**练习 2**：`padded_vocab_size` 和原始 `vocab_size` 有什么区别？为什么需要 padded？

**答案**：Megatron 为了让词表维度能被张量并行（TP）和某个对齐因子（`make_vocab_size_divisible_by`，常为 128）整除，会把词表向上补齐。补齐后的维度叫 `padded_vocab_size`。例如某模型原始词表 151936 已能被 128 整除，则 padded 等于原始；否则会被补到更大的值，embedding 矩阵因此多出若干「填充行」。

---

### 4.2 convert_hf_to_torch_dist 入口：HF 转为 Megatron

#### 4.2.1 概念说明

`tools/convert_hf_to_torch_dist.py` 是把 HF 权重转成 Megatron `torch_dist` 的入口脚本。它的整体策略是**「借壳生蛋」**：

1. 用 Megatron 的参数解析与初始化，搭出一个按目标并行策略分好片的**空 Megatron 模型**；
2. 用 slime 的 `load_hf_weights` 把 HF 权重**改名+切分后灌进**这个空壳；
3. 调用 Megatron 原生的 `save_checkpoint` 把填好的模型写成 `torch_dist`。

这样得到的检查点和「真训练保存的检查点」在格式上完全一致，训练时 `--ref-load` 直接能用。

#### 4.2.2 核心流程

```text
get_args()
  ├─ parse_args(add_convertion_args)        # Megatron 参数 + 转换专用参数
  ├─ set_default_megatron_args(args)        # slime 填默认值
  └─ 若 world_size > 1 且 PP=1：
       自动推导 pipeline_model_parallel_size = world_size
       并算出最后一 stage 的层数 decoder_last_pipeline_num_layers
        │
dist.init_process_group(backend="nccl")      # 起分布式
init(args)                                   # 建 TP/PP/CP/EP 通信组
        │
model = get_model(get_model_provider_func(args), ...)   # 搭空 Megatron 模型
        │
load_hf_weights(args, model, hf_model_path)  # 灌 HF 权重
        │
save_checkpoint(1, model, None, None, 0)     # 存成 torch_dist（iter 1）
        │
rank 0：把 tracker 写成 "release"，重命名 iter_1 → release 目录
```

最后一行的「release」处理很关键：Megatron 的 checkpoint tracker 默认记录训练步号，训练时会自动从最新步恢复。把 tracker 标成 `release` 并把目录改名，表示这是一个**终态发布检查点**，不会被自动恢复逻辑误读——这正是给 `--ref-load` 用的语义。

#### 4.2.3 源码精读

**① 新增转换参数与 PP 自动推导**：

- [tools/convert_hf_to_torch_dist.py:20-34](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/convert_hf_to_torch_dist.py#L20-L34)：`add_convertion_args` 给 Megatron 解析器加上 `--hf-checkpoint`（输入 HF 路径）、`--custom-model-provider-path`（自定义模型 provider）、`--padded-vocab-size` 等转换专用参数。

- [tools/convert_hf_to_torch_dist.py:55-74](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/convert_hf_to_torch_dist.py#L55-L74)：当多卡转换且未指定 PP 时，自动令 `pp_size = world_size`，并用 `ceildiv` 算出最后一 stage 的层数，保证所有层恰好分完。`decoder_last_pipeline_num_layers` 的计算为：

  \[
  \text{last\_layers} = \text{num\_layers} - \left\lceil \frac{\text{num\_layers}}{\text{pp\_size}} \right\rceil \times (\text{pp\_size} - 1)
  \]

  含义是：前 `pp_size-1` 个 stage 各放 \(\lceil \text{num\_layers}/\text{pp\_size}\rceil\) 层，剩下的全给最后 stage。若算出来 ≤0（层不够分），就退一步把 `pp_size` 减半重试。

**② 起分布式 + 搭空模型 + 灌权重**：

- [tools/convert_hf_to_torch_dist.py:101-108](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/convert_hf_to_torch_dist.py#L101-L108)：初始化 NCCL 进程组，再调 slime 的 `init(args)` 建立各种并行通信组。

- [tools/convert_hf_to_torch_dist.py:114](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/convert_hf_to_torch_dist.py#L114)：`get_model(get_model_provider_func(args), ModelType.encoder_or_decoder, wrap_with_ddp=False)` —— 用 4.1 节的 provider 搭出空模型，`wrap_with_ddp=False` 表示转换阶段不包 DDP。

- [tools/convert_hf_to_torch_dist.py:118](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/convert_hf_to_torch_dist.py#L118)：`load_hf_weights(args, model, hf_model_path)` 把 HF 权重灌进去。它内部按 `model_type` 分发（见下）。

**③ HF 权重装载的分发表**：

- [slime/backends/megatron_utils/hf_to_megatron/__init__.py:12-30](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/hf_to_megatron/__init__.py#L12-L30)：`_LOADERS` 字典把 HF 的 `model_type`（如 `qwen3`、`glm4`、`deepseek_v3`）映射到对应的张量转换函数。这就是 slime「按模型族处理改名+切分」的注册表。

- [slime/backends/megatron_utils/hf_to_megatron/__init__.py:38-44](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/hf_to_megatron/__init__.py#L38-L44)：`load_hf_weights` 读 `config.model_type`，查表拿到对应 `get_hf_tensor`，再交给 `load_model_hf_weights` 完成逐张量映射。若 `model_type` 不在表里，会抛 `Unsupported HuggingFace model type`。

**④ 保存为 torch_dist + release 重命名**：

- [tools/convert_hf_to_torch_dist.py:129](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/convert_hf_to_torch_dist.py#L129)：`save_checkpoint(1, model, None, None, 0)` 调用 Megatron 原生保存逻辑，写出 `iter_0000001` 目录。

- [tools/convert_hf_to_torch_dist.py:131-138](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/convert_hf_to_torch_dist.py#L131-L138)：rank 0 把 tracker 文件内容写成字符串 `"release"`，并把 `iter_0000001` 目录 move 到 `iter_0000000/release`。这样训练侧 `--ref-load` 指向它即可。

#### 4.2.4 代码实践

**目标**：把 Qwen3-0.6B 的 HF 权重转成 Megatron `torch_dist`，并观察输出目录结构。

**步骤**：

1. 在已装好 slime + Megatron-LM 的环境（参考 u1-l3）里，下载小模型：

   ```bash
   hf download Qwen/Qwen3-0.6B --local-dir /root/Qwen3-0.6B
   ```

2. 加载模型参数并执行转换（单卡）：

   ```bash
   cd /root/slime
   source scripts/models/qwen3-0.6B.sh
   PYTHONPATH=/root/Megatron-LM python tools/convert_hf_to_torch_dist.py \
       ${MODEL_ARGS[@]} \
       --hf-checkpoint /root/Qwen3-0.6B \
       --save /root/Qwen3-0.6B_torch_dist
   ```

3. 转换完成后查看输出目录：

   ```bash
   ls /root/Qwen3-0.6B_torch_dist
   ls /root/Qwen3-0.6B_torch_dist/iter_0000000/release
   ```

**需要观察的现象**：

- 标准输出会打印 `Using pipeline model parallel size: 1, decoder last pipeline num layers: ...`。
- 输出目录下出现 `latest_checkpointed_iteration.txt`（内容应为 `release`）和 `iter_0000000/release/`，后者含 `__avg_...` 分片文件、`.metadata`、`common.pt` 等。

**预期结果**：`--ref-load /root/Qwen3-0.6B_torch_dist` 即可被训练脚本直接加载。

> 待本地验证：上述命令需在具备 GPU 与 Megatron-LM 的环境运行；纯 CPU 环境无法完成（脚本第 95 行 `torch.cuda.set_device` 会失败）。

#### 4.2.5 小练习与答案

**练习 1**：为什么单卡转换时，`pp_size` 自动推导那段代码（第 55–74 行）不会触发？

**答案**：自动推导的前提是 `pipeline_model_parallel_size == 1 and world_size > 1`。单卡时 `world_size == 1`，条件不满足，直接用默认 PP=1，所有层都在一个 stage 里。

**练习 2**：转换脚本把 tracker 写成 `"release"` 而不是步号 `1`，目的是什么？

**答案**：`release` 是 Megatron 的「终态检查点」语义。训练时自动恢复逻辑会读 tracker 找最新步号来 resume；标记成 `release` 可避免这个「初始权重检查点」被当作可 resume 的训练中间态误读，同时 `--ref-load` 仍能正确加载它作为参考权重。

---

### 4.3 convert_torch_dist_to_hf 入口：Megatron 转回 HF

#### 4.3.1 概念说明

`tools/convert_torch_dist_to_hf.py` 是反向转换：把 Megatron `torch_dist`（通常是训练保存的某个 `iter_xxx`）转回 HF 的 `safetensors` 格式，用于推理发布、评测或对接其他工具。

它的难点在于 `torch_dist` 是**分片+按层折叠**存储的，需要先「读取所有分片 → 展开成每层/每专家一个张量 → 按模型族反向改名（合并 QKV、还原 TP 切分）→ 去 vocab padding → 分块写成 safetensors」。

#### 4.3.2 核心流程

```text
读 common.pt → 取出训练时的 megatron_args（含 num_layers/num_experts/vocab_size）
        │
dist_cp.state_dict_loader._load_state_dict(state_dict, storage_reader=WrappedStorageReader)
   └─ EmptyStateDictLoadPlanner：按 .metadata 自动给每个键分配正确 dtype/shape 的空张量
        │
get_named_params(args, state_dict)
   └─ get_layer_param：把 ".layers." 折叠的张量展开成 .layers.{i}.
   └─ get_expert_param：把 ".experts." 折叠的张量展开成每专家
        │
save_tensors(...)
   ├─ 对每个张量：remove_padding（embedding/output_layer 截断到 vocab_size）
   ├─ convert_to_hf：按 model_name 选反向映射函数（如 convert_qwen2_to_hf）
   ├─ 累计到约 chunk_size（默认 5GB）就切一个 safetensors 文件
   └─ 写 model-XXXXX-of-YYYYY.safetensors + index.json
        │
copy_assets：从 origin-hf-dir 拷贝 tokenizer、config.json 等非权重文件
```

#### 4.3.3 源码精读

**① 读分片检查点的两个「魔法」类**：

- [tools/convert_torch_dist_to_hf.py:34-45](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/convert_torch_dist_to_hf.py#L34-L45)：`WrappedStorageReader` 重写 `read_metadata`，用 `UnpicklerWrapper` 安全地反序列化 `.metadata`——遇到 megatron/glm 开头的类名就用空 `DummyClass` 代替，避免加载时触发真实模块的 import。

- [tools/convert_torch_dist_to_hf.py:48-63](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/convert_torch_dist_to_hf.py#L48-L63)：`EmptyStateDictLoadPlanner` 在 `set_up_planner` 里，根据元数据里的 `TensorStorageMetadata` 为每个键**自动创建正确 dtype 和 size 的空张量**，跳过 `optimizer`/`_state` 键。这样加载器只把模型权重（不含优化器状态）读出来。

**② 展开折叠的层与专家**：

- [tools/convert_torch_dist_to_hf.py:83-103](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/convert_torch_dist_to_hf.py#L83-L103)：`get_layer_param` 用正则判断张量名里是否含 `.layers.{i}.`：不含则说明该张量是「所有层堆叠」的第 0 维大小 = `num_layers`，于是循环切出每层；`get_expert_param` 同理处理专家维。`get_named_params` 在名字前补 `module.module.` 前缀以匹配 Megatron 命名。

**③ 改名 + 去 padding + 分块写盘**：

- [tools/convert_torch_dist_to_hf.py:106-126](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/convert_torch_dist_to_hf.py#L106-L126)：`save_tensors` 对每个张量先 `remove_padding`（若给了 `--vocab-size`），再 `convert_to_hf` 改名/合并；按字节累计到 `chunk_size`（默认 5GB）就新开一个 safetensors 文件。

- [tools/convert_torch_dist_to_hf.py:146-162](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/convert_torch_dist_to_hf.py#L146-L162)：写 `model.safetensors.index.json`（含 `total_size` 和 `weight_map`），再逐文件 `safetensors.torch.save_file`，命名规则为 `model-{i:05d}-of-{num:05d}.safetensors`。

**④ 反向映射分发表与去 padding**：

- [slime/backends/megatron_utils/megatron_to_hf/__init__.py:23-33](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/megatron_to_hf/__init__.py#L23-L33)：`convert_to_hf` 先去掉 `module.` 前缀、`remove_padding`，再调 `_convert_to_hf_core` 选模型族函数，最后可选量化。

- [slime/backends/megatron_utils/megatron_to_hf/__init__.py:41-57](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/megatron_to_hf/__init__.py#L41-L57)：`_convert_to_hf_core` 把 `model_name` 归一化（去 `_`、`-`）后按子串匹配分发到 `convert_qwen2_to_hf`、`convert_glm4_to_hf`、`convert_deepseekv3_to_hf` 等。这就是反向「改名+合并 QKV+还原 TP」的核心路由。

- [slime/backends/megatron_utils/megatron_to_hf/processors/padding_remover.py:6-12](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/megatron_to_hf/processors/padding_remover.py#L6-L12)：`remove_padding` 只对 `embedding.word_embeddings.weight` 和 `output_layer.weight` 截断到 `param[:vocab_size]`，其余张量原样返回。这正是「vocab size 是否一致」的来源——只要显式传 `--vocab-size`，补齐的填充行会被裁掉。

**⑤ 主流程入口**：

- [tools/convert_torch_dist_to_hf.py:224-230](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/convert_torch_dist_to_hf.py#L224-L230)：先 `torch.load(common.pt)` 取出训练时的 `args`（拿到 `num_layers`/`num_experts`/`vocab_size`），再用 `WrappedStorageReader` + `EmptyStateDictLoadPlanner` 加载权重。注意它**不需要 GPU、不需要起分布式**，可以在纯 CPU 上跑。

- [tools/convert_torch_dist_to_hf.py:243-244](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/convert_torch_dist_to_hf.py#L243-L244)：若给了 `--origin-hf-dir`，最后 `copy_assets` 把原 HF 目录里的 `tokenizer.json`、`config.json` 等非权重文件拷过来，让产物是个完整可用的 HF 目录。

#### 4.3.4 代码实践

**目标**：把 4.2 节得到的 `torch_dist` 转回 HF，对比词表大小。

**步骤**：

1. 执行反向转换：

   ```bash
   PYTHONPATH=/root/Megatron-LM python tools/convert_torch_dist_to_hf.py \
       --input-dir /root/Qwen3-0.6B_torch_dist/iter_0000000/release/ \
       --output-dir /root/Qwen3-0.6B_roundtrip \
       --origin-hf-dir /root/Qwen3-0.6B \
       --vocab-size 151936 \
       --force
   ```

2. 对比词表大小——读两个目录里 `embedding` 相关张量的第 0 维：

   ```bash
   python - <<'PY'
   import safetensors.torch as st
   def vocab(path, key):
       with st.safe_open(path, framework="pt") as f:
           return f.get_tensor(key).shape[0]
   print("origin :", vocab("/root/Qwen3-0.6B/model.safetensors",
                           "model.embed_tokens.weight"))
   print("round  :", vocab("/root/Qwen3-0.6B_roundtrip/model-00001-of-00001.safetensors",
                           "model.embed_tokens.weight"))
   PY
   ```

   > 注意：上述文件名与张量键名可能因模型版本而异，请用 `safe_open(...).keys()` 先确认实际键名，再替换。这是「示例代码」，需按实际输出调整。

**需要观察的现象**：

- 反向转换**不需要 GPU**，可在 CPU 上完成，且会打印 `find ... in torch_dist ckpt` 列出每个权重键。
- 产物目录含 `model-*.safetensors`、`model.safetensors.index.json`，以及从原 HF 目录拷来的 `tokenizer.json`、`config.json`。

**预期结果**：两个 `embed_tokens.weight` 的第 0 维都应是 `151936`（一致）。如果不一致，多半是转换时**没传 `--vocab-size`**，导致 embedding 带着被 Megatron 补齐的填充行原样写出，第 0 维会略大。

> 待本地验证：实际 safetensors 分片数、键名需以本地运行为准；若模型权重被分到多个文件，需在 `index.json` 的 `weight_map` 里定位 `embed_tokens` 所在分片。

#### 4.3.5 小练习与答案

**练习 1**：反向转换脚本 `get_args` 里 `assert world_size <= args.num_layers` 这类校验在反向脚本里**不存在**。为什么反向脚本不需要 GPU 也能跑？

**答案**：反向脚本只做「读分片 → 张量改名/合并/去 padding → 写 safetensors」，全程是 CPU 上的张量搬运，不需要建 NCCL 通信组、不需要 `torch.cuda.set_device`，所以能在纯 CPU 跑。正向脚本则必须搭真正的 Megatron 模型（要建并行通信组、可能用 TE），所以需要 GPU。

**练习 2**：如果不传 `--vocab-size`，转回的 HF 模型加载后会有什么隐患？

**答案**：embedding 和 output_layer 会保留 Megatron 补齐的填充行，第 0 维大于真实词表。虽然多出的行对应「用不到的 token id」，通常不影响前向，但会让 `config.json` 里的 `vocab_size` 与权重实际维度对不上，可能导致加载校验失败或下游工具误判。所以文档建议在不确定时手动指定 `--vocab-size`。

**练习 3**：反向转换如何知道一个模型有多少层、多少专家？

**答案**：它从 `torch_dist` 目录下的 `common.pt` 里 `torch.load` 出训练时保存的 `args`，从中读 `num_layers`、`num_experts`、`vocab_size` 等（见主流程第 224 行）。这样就不需要用户再重复传一遍结构参数。

---

## 5. 综合实践

**任务**：完整跑通一次「HF → torch_dist → HF」的往返（round-trip）转换，并验证权重数值没有被破坏。

**操作步骤**：

1. **正向转换**（需 GPU + Megatron-LM）：按 4.2.4 节把 `/root/Qwen3-0.6B` 转成 `torch_dist`，输出到 `/root/Qwen3-0.6B_torch_dist`。

2. **反向转换**（CPU 即可）：按 4.3.4 节把它转回 `/root/Qwen3-0.6B_roundtrip`，务必带上 `--vocab-size 151936` 和 `--origin-hf-dir /root/Qwen3-0.6B`。

3. **数值一致性检查**（示例代码）：逐张量对比原始 HF 与 roundtrip 后的 HF，统计最大绝对误差。

   ```python
   # 示例代码：对比两个 HF 目录的同名张量
   import safetensors.torch as st
   import glob, os

   def load_all(directory):
       out = {}
       for f in sorted(glob.glob(os.path.join(directory, "*.safetensors"))):
           with st.safe_open(f, framework="pt") as g:
               for k in g.keys():
                   out[k] = g.get_tensor(k)
       return out

   a = load_all("/root/Qwen3-0.6B")
   b = load_all("/root/Qwen3-0.6B_roundtrip")
   common = set(a) & set(b)
   for k in sorted(common):
       diff = (a[k].float() - b[k].float()).abs().max().item()
       print(f"{k:60s} max_abs_diff={diff:.3e}  shape={tuple(a[k].shape)}")
   ```

**预期结果**：

- 词表大小一致（均为 151936）。
- 由于涉及一次 HF→Megatron 的改名/reshape 再 Megatron→HF 的逆变换，绝大多数张量 `max_abs_diff` 应为 `0.000e+00`（仅改名/切分还原，无数值运算）。若个别张量出现非零小误差，需检查是否触发了 dtype 变化或 padding 未对齐。

**思考延伸**：如果在正向转换时误用了错误的 `--rotary-base`（与模型真实值不符），这次 round-trip 还能检出问题吗？

> 答案：**不能**。rotary base 只影响训练时的位置编码计算，权重张量本身（不含 RoPE 的预计算表）数值不变，round-trip 对比不会发现异常。这正说明：**round-trip 一致只证明「格式转换无损」，不能证明「参数语义正确」**——结构参数是否匹配仍需对照 `config.json` 人工核对。

## 6. 本讲小结

- Megatron 无法直接读 HF 检查点：它要求命令行显式声明结构，参数命名/布局也与 HF 不同，必须转换。
- `model_provider` 是「参数 → Megatron GPTModel」的工厂，在转换与训练中复用；它用 `padded_vocab_size` 搭模型，埋下了「去 padding」的伏笔。
- `convert_hf_to_torch_dist.py` 走「搭空模型 → 灌 HF 权重 → Megatron 原生保存 → 标 release」的借壳路线，产物可直接给 `--ref-load` 用。
- `hf_to_megatron/_LOADERS` 与 `megatron_to_hf/_convert_to_hf_core` 是两张按 `model_type`/`model_name` 分发的注册表，把「改名+切分/合并」按模型族封装。
- `convert_torch_dist_to_hf.py` 在纯 CPU 上运行：用 `WrappedStorageReader`/`EmptyStateDictLoadPlanner` 安全读分片，展开折叠的层/专家，去 padding，分块写 safetensors。
- `--vocab-size` 是反向转换去 padding 的关键；round-trip 一致只证明格式无损，不证明结构参数语义正确，仍需核对 `scripts/models/*.sh`。

## 7. 下一步学习建议

- 现在你已能为 [u1-l4 运行第一个训练](u1-l4-first-training-run.md) 准备好 `--ref-load` 所需的 `torch_dist` 权重，建议接着动手把第一个训练脚本跑起来。
- 若想深入「改名+切分」的逐张量细节，可在进阶层阅读 `slime/backends/megatron_utils/hf_to_megatron/qwen.py` 与 `megatron_to_hf/qwen2.py` 这对正向/反向映射函数。
- 若关心低精度（bf16 训练 + fp8 推理）的权重处理，可预习 `megatron_to_hf/processors/quantizer_fp8.py`，它会在 `convert_to_hf` 的 `quantize_params` 环节被调用，这部分将在专家层 [u8-l5 模型插件与低精度](u8-l5-model-plugins-low-precision.md) 详讲。
