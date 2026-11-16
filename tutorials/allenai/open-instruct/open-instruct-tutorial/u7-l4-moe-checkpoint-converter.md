# u7-l4 MoE checkpoint 导出：OLMoE3/KDA 转 HF 与 chat EOS 列表

## 1. 本讲目标

学完本讲，你应该能够：

1. 会用 `scripts/train/debug/convert_moe_checkpoint_to_hf.py` 把 olmo-core MoE v2（KDA / OLMoE3，A4B）checkpoint 导出为 HuggingFace 格式，并理解为什么 `-c` 必须传训练时用的配置 JSON（含 `model` 与 `dataset.tokenizer` 两个节）。
2. 理解 OLMoDDP 融合优化器把「权威权重」存成 `module.<参数名>.main` 的布局，以及 `load_ddp_main_params` 如何按六类候选键匹配、做整型（元素数）校验、最终 gather 出完整 `state_dict`。
3. 掌握 chat EOS 列表默认值 `[100265, 100257]` 的动机：vLLM 的 OpenAI server 从 `generation_config.json` 取停止符，缺 `<|im_end|>` 会导致回合不停止、工具调用重复到 token 预算（BFCL 上发生过的真实故障）。
4. 理解校验策略与边界：默认 `--device cuda` 的理由（KDA 的 Triton 内核拒绝 CPU 张量，要避免「已转换但未校验」的假象）、KDA 1.2B 上严格 logit 校验必然失败这一已知问题及 `--skip-validation` + 功能性评估的替代方案、只支持本地/WEKA 路径、GDN 混合 MoE 会误入 dense-hybrid 导出器的告诫。

本讲是 u7-l3 的续篇：u7-l3 讲的是混合注意力（dense-hybrid）模型在 HF 与 OLMo-core 之间的权重转换；本讲讲的是另一类模型——MoE v2（KDA/OLMoE3）——从 olmo-core 训练 checkpoint 到 HF 格式的导出，两者互补但不重叠。

## 2. 前置知识

- **checkpoint 与 state_dict**：训练框架定期把模型参数（以及优化器状态）存到磁盘，叫 checkpoint；`state_dict` 是「参数名 → 张量」的字典，是 PyTorch 生态搬运权重的通用载体。
- **MoE（Mixture of Experts）**：把 Transformer 的 FFN 层换成多个「专家」网络，路由器按 token 动态选择少数专家计算，用远小的激活参数量换更大的总参数量。本讲的对象 OLMoE3（内部代号 KDA / A4B，激活约 4B）就是 MoE v2 架构。
- **DDP 与融合优化器**：DDP（DistributedDataParallel）是最朴素的数据并行——每张卡持有完整模型副本，各自前向反向后同步梯度。MoE v2 模型拒绝 FSDP 分片（见 [open_instruct/olmo_core_utils.py:L611-L622](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/open_instruct/olmo_core_utils.py#L611-L622) 的说明），只能走 OLMo-core 的 DDP 训练模块。**融合优化器**（fused optimizer）把多参数的优化器步骤融合成一次内核，并习惯把 fp32 的「主权重」（master weight）以 `<参数名>.main` 后缀存进状态——这就是本讲要处理的关键布局。
- **meta 设备与 `to_empty`**：PyTorch 允许先在 `meta` 设备上「只搭形状不占内存」地构建模型，再用 `to_empty` 搬到真实设备填充未初始化内存。`load_ddp_main_params` 用这一招先建骨架、再把 checkpoint 里的值逐个拷进去。
- **`generation_config.json` 与 EOS**：HF 格式的模型目录里，`config.json` 描述结构，`generation_config.json` 描述生成期默认行为（采样参数、停止符等）。`eos_token_id` 是「生成到这些 token 就停」的列表；服务框架（如 vLLM 的 OpenAI 兼容 server）会从这里读取停止符。
- **`<|im_end|>` 与 `<|endoftext|>`**：Olmo 3 的 chat tokenizer（`allenai/olmo-3-tokenizer-instruct-dev`）里，`<|endoftext|>` 是 id 100257（同时是 eos/bos，见 [open_instruct/olmo_core_utils.py:L782-L789](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/open_instruct/olmo_core_utils.py#L782-L789) 的注释），`<|im_end|>` 是 id 100265，聊天里每轮 assistant 回复以 `<|im_end|>` 收尾。
- **WEKA**：Ai2 内部的分布式文件系统，路径形如 `/weka/...`，在集群容器里挂载后可当本地路径用。
- **Triton 内核**：GPU 上编译执行的高性能内核；fla（flash-linear-attention）库为 KDA 这类线性注意力提供的 Triton 内核**只能在 GPU 张量上运行**，传 CPU 张量会直接报错——这是本讲校验设备默认值背后的关键事实。
- **BFCL**：Berkeley Function Calling Leaderboard，评测模型工具调用能力的公开榜单；本讲故障案例就发生在这上面。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [scripts/train/debug/convert_moe_checkpoint_to_hf.py](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L1-L185) | 本讲主角：MoE v2（KDA/OLMoE3）checkpoint → HF 格式的转换脚本，约 185 行，含 OLMoDDP `.main` 权重收集、chat EOS 写入与校验策略 |
| [open_instruct/olmo_core_utils.py](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/open_instruct/olmo_core_utils.py#L611-L648) | 背景：MoE v2 模型为何走 OLMoDDP 训练模块（拒绝 FSDP、优化器配置来自 checkpoint 的 `train_module` 节），这正是产生 `.main` 布局的训练侧源头 |
| [open_instruct/olmo_core_hybrid.py](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/open_instruct/olmo_core_hybrid.py#L230-L244) | u7-l3 讲过的 dense-hybrid 权重转换（`convert_hybrid_state_to_hf` 等），与本讲 converter 是两条并行的导出路线 |
| [scripts/train/olmo-hybrid/convert_instruct_sft_sweep.sh](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/olmo-hybrid/convert_instruct_sft_sweep.sh#L1-L31) | 姊妹脚本：早期 dense-hybrid 转换的做法——用 gantry 起容器跑 olmo-core 侧自带的转换示例，同样带 `--skip-validation`，可对照理解本脚本的演进 |
| [pyproject.toml](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/pyproject.toml#L22) | `ai2-olmo-core` 是主依赖，钉在特定 git rev（第 77 行）；而脚本文档字符串要求 olmo-core 在 `akshitab/emo_modularity` 分支或更晚——运行前需要确认钉住的 rev 满足要求 |

## 4. 核心概念与源码讲解

本讲的四个最小模块：

1. converter 入口与参数（`-i`/`-o`/`-c`/`--eos-token-ids`/`--device`/`--skip-validation`）
2. OLMoDDP `.main` 权重布局收集（`load_ddp_main_params`）
3. `generation_config.json` 的 chat EOS 写入（`write_generation_eos`）
4. 校验策略与已知告诫

### 4.1 converter 入口与参数

#### 4.1.1 概念说明

这是一个命令行脚本：输入一个 olmo-core 训练 checkpoint 目录和一份训练配置 JSON，输出一个可直接被 `transformers` / vLLM 加载的 HF 模型目录。它本体是对 olmo-core 自带转换器 `convert_checkpoint_to_hf` 的「薄封装」（thin wrapper，见[文档字符串 L3-L6](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L3-L6)），封装补上了三件 olmo-core 原生路径没有覆盖的事：

1. OLMoDDP 融合优化器的 `.main` 权重布局收集（模块 4.2）；
2. `generation_config.json` 的 chat EOS 列表写入（模块 4.3）；
3. 面向 KDA 的校验设备与跳过策略（模块 4.4）。

为什么模型配置要从**外部文件**传入，而不是读 checkpoint 自带的 `config.json`？文档字符串 [L8-L11](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L8-L11) 给出答案：**训练配置才是「块结构」与保存权重相匹配的那一份**。checkpoint 里的 config 可能来自预训练阶段或另一条流水线，块命名/结构与 SFT 权重对不上；直接用它重建模型会加载错位。所以必须传**启动这次训练时用的** olmo-core 训练配置 JSON，且其中必须含有 `model` 节（建模型）和 `dataset.tokenizer` 节（建 tokenizer）。仓库里不带这份配置，需要从各训练 run 处获取。

#### 4.1.2 核心流程

`main()` 的执行流程（[L126-L180](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L126-L180)）：

```text
解析命令行参数
    ↓
读取 -c 指定的训练配置 JSON
    ├── model_config       = payload["model"]                  # 建模型用
    └── tokenizer_config   = payload["dataset"]["tokenizer"]   # 建 tokenizer 用（缺失则 SystemExit）
    ↓
在临时目录 work_dir 中调用 load_ddp_main_params(<输入>/model_and_optim, ...)
    → 得到完整的 CPU state_dict（或 None，表示常规 checkpoint）
    ↓
调用 olmo-core 的 convert_checkpoint_to_hf(...)
    （bf16 导出、写入 config.json / generation_config.json / 权重文件，
      可选地做双实现 logit 校验）
    ↓
write_generation_eos(输出目录, eos_token_ids)   # 改写 generation_config.json
    ↓
logger.info("CONVERSION_OK")
```

一个容易被忽略的工程细节在 [L160-L165](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L160-L165)：`load_keys` 是生成器（惰性读取），所以临时目录 `work_dir` 必须在 `with` 块内被**完整消费**——函数返回的已是物化好的 CPU 张量，临时目录随后才能清理。注释明确要求「Keep the consumption inside the block」。

#### 4.1.3 源码精读

参数定义集中在 [L127-L151](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L127-L151)：

- [L128-L130](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L128-L130)：三个必填参数——`-i` checkpoint 输入路径、`-o` HF 输出目录、`-c` 训练配置 JSON（帮助文本直接写明需要 `model + dataset.tokenizer`）。
- [L131-L132](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L131-L132)：`-t` tokenizer id，默认 `allenai/olmo-3-tokenizer-instruct-dev`；`-s` 最大序列长度，默认 8192。
- [L133](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L133)：`--skip-validation` 用 `dest="validate", action="store_false"` 实现——传了它 `args.validate` 就是 `False`，后面原样透传给转换器。
- [L138-L144](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L138-L144)：`--eos-token-ids`，`nargs="+"` 接收一个或多个整数，默认 `list(CHAT_EOS_TOKEN_IDS)` 即 `[100265, 100257]`。它上方的注释（[L134-L137](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L134-L137)）记录了默认值的动机，模块 4.3 展开。
- [L150](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L150)：`--device` 用 `type=torch.device` 直接解析成设备对象，**默认 `cuda`**。为什么不是 CPU？注释（[L145-L149](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L145-L149)）专门解释，模块 4.4 展开。

配置解析与校验在 [L153-L157](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L153-L157)：`payload["model"]` 直接下标访问（缺 `model` 节会 KeyError，说明这份 JSON 必须是完整训练配置）；`dataset.tokenizer` 用 `.get` 链式取值，缺失时给出明确的 `SystemExit` 报错信息。

最终转换调用在 [L166-L178](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L166-L178)：向 olmo-core 的 `convert_checkpoint_to_hf` 传入 `model_state_dict`（模块 4.2 的产物，可为 None）、`dtype=DType.bfloat16`、`validate=args.validate`、`device` 与 `validation_device` 都用同一个 `args.device`。最后 [L179-L180](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L179-L180) 写 EOS、打印 `CONVERSION_OK`（一个便于在集群日志里 grep 的完成信号）。

另外注意两处仓库约定：脚本用 `from open_instruct import logger_utils` + `logger = logger_utils.setup_logger(__name__)`（[L53-L55](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L53-L55)），遵守项目「不用裸 logging」的编码规范；`prepare_cli_environment()`（[L184](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L184)）是 olmo-core 提供的 CLI 环境初始化（随机种子等）。

#### 4.1.4 代码实践

**实践目标**：熟悉脚本的命令行接口，确认本机能否导入它（这直接检验 olmo-core 依赖是否满足文档字符串的要求）。

**操作步骤**：

1. 通读 [L126-L151](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L126-L151) 的参数定义。
2. 尝试：

   ```bash
   uv run python scripts/train/debug/convert_moe_checkpoint_to_hf.py --help
   ```

3. 无论成功与否，记下结果；若失败，读一遍报错里提到的模块名，对照[文档字符串 L5-L6](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L5-L6)（要求 olmo-core 在 `akshitab/emo_modularity` 或更晚，早期 rev 会对 peri-LN checkpoint 抛 `NotImplementedError`）与 [pyproject.toml:L77](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/pyproject.toml#L77) 钉住的 rev。

**需要观察的现象**：`--help` 打印出的 usage 与六个参数的 help 文本；或 `ImportError`（例如 `olmo_core.nn.hf` 里没有 `convert_checkpoint_to_hf`）。

**预期结果**：如果钉住的 olmo-core rev 足够新，能看到完整 usage；否则会看到导入失败——这不是脚本 bug，而是文档字符串声明的 olmo-core 版本前置条件未满足。**待本地验证**（取决于 `uv sync` 解析到的 olmo-core rev）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `-c` 不能传 checkpoint 目录里自带的 `config.json`？

**答案**：因为训练配置才是「块结构与保存权重相匹配」的那一份（文档字符串 [L8-L11](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L8-L11)）。checkpoint 自带 config 可能来自预训练或其他流水线，用它重建出的模型块结构与 SFT 权重对不上；而且训练配置还提供 `dataset.tokenizer` 节，这是 checkpoint config 里没有的信息。

**练习 2**：`--skip-validation` 是怎么实现的？它在 `args` 里变成什么？

**答案**：[L133](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L133) 用 `dest="validate", action="store_false"`：不传时 `args.validate` 默认为 `True`，一传就翻转为 `False`，随后原样透传给 `convert_checkpoint_to_hf(validate=...)`。

**练习 3**：`-c` 传的 JSON 缺 `dataset.tokenizer` 节会发生什么？缺 `model` 节呢？

**答案**：缺 `dataset.tokenizer` 会命中 [L156-L157](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L156-L157) 的检查，`SystemExit` 并打印 `<config> has no dataset.tokenizer section`；缺 `model` 节则 [L154](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L154) 的 `payload["model"]` 直接 `KeyError`——两个节的失败方式不同，前者有友好报错，后者是裸异常。

### 4.2 OLMoDDP `.main` 权重布局收集

#### 4.2.1 概念说明

这是整个脚本技术含量最高的部分。问题背景（`load_ddp_main_params` 的文档字符串，[L58-L68](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L58-L68)）：

- MoE v2 模型（如 OLMoE3/A4B）用 OLMo-core 的 `OLMoDDPTrainModule` 训练。这个训练模块的融合优化器把**权威权重**（authoritative weights，fp32 主副本）存为 `module.<参数名>.main` 张量，**而不是**常规布局的 `model.<参数名>`。
- 因此 olmo-core 原生的转换器面对这类 checkpoint 会找不到权重——历史上「Olmo 3.5 hero」一系的每次 SFT 转换都死在 `Missing key in checkpoint state_dict: model.embeddings.weight`（2026-09-16 的记录，[L64-L66](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L64-L66)）。
- olmo-core 的 `akshitab/emo_modularity` 分支（f2cf93839）教会了自家 `convert_checkpoint_to_hf` 识别这种布局（`_load_ddp_optimizer_model_state`）；本函数就是把那个逻辑**移植**过来：同样的候选键、收集出同样的完整 state_dict，交给转换器。

为什么会有六类候选键？因为不同训练代码给参数加的前缀/后缀不同：

- `model.` 前缀：常规「模型在优化器状态之下」的命名；
- `module.` 前缀：DDP 把模型包进 `module` 属性后的命名；
- 无前缀：某些保存路径直接用裸参数名；
- `.main` 后缀：融合优化器的 fp32 主权重——本函数要找的正是它；
- 组合起来就是六种形态，逐一尝试、取第一个命中的。

#### 4.2.2 核心流程

`load_ddp_main_params(model_and_optim_dir, model_config, work_dir)` 的流程（[L58-L111](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L58-L111)）：

```text
读取 checkpoint 元数据 → checkpoint_keys（全部键的集合）
    ↓
没有任何键以 ".main" 结尾？
    ├── 是 → 返回 None（常规 checkpoint，让 olmo-core 原生路径处理）
    └── 否 ↓（OLMoDDP 布局，继续）
按 model_config 构建模型：先 meta 设备搭形状（不占内存）→ to_empty 到 CPU
    ↓
遍历 model.named_parameters()，对每个参数名 name 依次尝试候选键：
    1. model.{name}
    2. model.module.{name}
    3. {name}
    4. module.{name}
    5. {name}.main
    6. module.{name}.main        ← OLMoDDP 权威权重通常命中这里
    取第一个出现在 checkpoint_keys 里的键
    ↓
有参数六个候选全部落空？→ 收集进 missing，统一 RuntimeError（缺一个都不行）
    ↓
load_keys 一次性加载所有选中的键
    ↓
逐个校验并拷贝：
    - 值不是张量 → TypeError
    - 元素数 numel 与目标参数不一致 → RuntimeError
    - 通过后 reshape 到目标形状、cast 到目标 dtype，copy_ 进去
    ↓
补充 named_buffers：model_buffer.{name} 键存在则同样加载
    ↓
get_model_state_dict(full_state_dict=True, cpu_offload=True)
    → gather 出完整的 CPU state_dict 返回
```

两点值得咀嚼的设计：

- **整型校验（numel）而非形状校验**：`value.numel() != destination.numel()` 只比元素总数，随后 `value.reshape(destination.shape)` 再重排。这容忍了 checkpoint 里参数被拉平或维度顺序不同的情况——只要总元素数一致就能安全重排；不一致则说明键匹配错了（对上了形状不兼容的张量），必须硬失败。
- **缺一个参数权重就直接报错**：模型是 `to_empty` 出来的——没被拷贝的参数里是**未初始化内存**。如果静默跳过，导出的模型会「看起来正常、logits 全错」，这正是这个脚本要消灭的失败模式（对照模块 4.4 里 38% logit 错误那次事故）。宁可中断，不给「半转换」的模型放行。

#### 4.2.3 源码精读

- [L69-L71](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L69-L71)：`get_checkpoint_metadata` 拿到 checkpoint 全部键；`any(key.endswith(".main"))` 是**布局探测**——没有 `.main` 后缀就返回 `None`，让 olmo-core 原生路径接管，脚本对常规 checkpoint 完全透明。
- [L73-L74](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L73-L74)：`TransformerConfig.from_dict(model_config).build(init_device="meta")` 按训练配置建模型骨架，再 `to_empty(device="cpu")`——这就是 `-c` 必须传**训练**配置的原因落地处：模型结构必须与保存的权重逐参数对齐。
- [L78-L92](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L78-L92)：候选键元组与首匹配循环。`next((k for k in candidates if k in checkpoint_keys), None)` 取第一个命中者；`None` 则进 `missing`。
- [L93-L94](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L93-L94)：`missing` 非空时抛 `RuntimeError`，把所有缺失参数名一次性列出（方便对照配置排查，而不是修一个又冒一个）。
- [L95-L104](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L95-L104)：`load_keys` 按 `work_dir`（分布式 checkpoint 的临时工作目录）加载；`zip(..., strict=True)` 保证键、目标、值三者数量严格一致；随后是 TypeError / numel RuntimeError / `copy_` 三连。
- [L105-L109](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L105-L109)：buffer（非参数状态，如各种缓存）从 `model_buffer.{name}` 键补充，且**不存在的键直接跳过**——与参数的硬失败形成对比：参数缺了模型就是错的，buffer 缺了往往可以重算/默认。
- [L110-L111](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L110-L111)：`dist_cp_sd.get_model_state_dict(model, options=StateDictOptions(full_state_dict=True, cpu_offload=True))` 用 PyTorch 分布式 checkpoint 的官方 API gather 出完整 CPU state_dict——至此 `.main` 布局被「洗」成了任何 HF 转换器都能吃的标准形态。

训练侧的源头可以对照 [open_instruct/olmo_core_utils.py:L611-L648](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/open_instruct/olmo_core_utils.py#L611-L648)：MoE v2 模型拒绝 FSDP、必须走 OLMoDDP 训练模块，优化器配置直接继承自 checkpoint 的 `train_module` 节——也就是说，**产生 `.main` 布局的训练路径本身就在这个仓库里**，本函数是那条路径的逆操作。

#### 4.2.4 代码实践

**实践目标**：在不依赖 olmo-core、不需要真实 checkpoint 的情况下，单独复现并验证六类候选键的首匹配逻辑。

**操作步骤**：

1. 把下面的片段存成独立脚本运行（这是**示例代码**，逻辑忠实复刻 [L78-L94](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L78-L94)，但不导入项目源文件）：

   ```python
   # 示例代码：复现 load_ddp_main_params 的候选键匹配逻辑
   def match(param_names: list[str], checkpoint_keys: set[str]) -> dict[str, str | None]:
       resolved = {}
       for name in param_names:
           candidates = (
               f"model.{name}",
               f"model.module.{name}",
               name,
               f"module.{name}",
               f"{name}.main",
               f"module.{name}.main",
           )
           resolved[name] = next((k for k in candidates if k in checkpoint_keys), None)
       return resolved

   # 模拟一个 OLMoDDP checkpoint 的键集合：大多权重是 module.<name>.main，
   # 个别（如 embeddings）存成了裸名。
   checkpoint_keys = {
       "module.backbone.block0.attn.q.weight.main",
       "module.backbone.block0.attn.k.weight.main",
       "backbone.block1.mlp.weight",            # 假设它以裸名保存
       "module.backbone.block2.mlp.weight.main",
   }
   param_names = [
       "backbone.block0.attn.q.weight",
       "backbone.block0.attn.k.weight",
       "backbone.block1.mlp.weight",
       "backbone.block2.mlp.weight",
       "backbone.block9.attn.v.weight",         # 六个候选都不会命中
   ]
   for name, key in match(param_names, checkpoint_keys).items():
       print(f"{name:45s} -> {key}")
   ```

2. 对照输出，把每个参数命中了六类候选中的哪一类写成一张表。
3. 修改 `checkpoint_keys`：把 `backbone.block1.mlp.weight` 也改成 `.main` 形态，重跑并观察匹配结果变化。

**需要观察的现象**：前四个参数各自命中的键（注意 `backbone.block1.mlp.weight` 应命中裸名候选 3，其他命中候选 6）；第五个参数返回 `None`——对应真实代码里进入 `missing` 列表。

**预期结果**：`backbone.block9.attn.v.weight -> None`；如果把真实代码的 `missing` 分支接上（[L93-L94](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L93-L94)），这次调用会以 `RuntimeError: DDP checkpoint is missing weights for model parameters: backbone.block9.attn.v.weight` 告终。本实践纯 Python，可直接运行验证。

#### 4.2.5 小练习与答案

**练习 1**：`load_ddp_main_params` 什么时候返回 `None`？返回 `None` 之后流程怎么走？

**答案**：checkpoint 元数据里没有任何以 `.main` 结尾的键时（[L70-L71](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L70-L71)），说明是常规布局，返回 `None`；随后 [L166-L178](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L166-L178) 把 `model_state_dict=None` 传给 `convert_checkpoint_to_hf`，由 olmo-core 原生逻辑自己从 checkpoint 读权重。

**练习 2**：为什么参数缺权重是硬报错，而 buffer 的键不存在时却静默跳过？

**答案**：参数没被拷贝时，`to_empty` 留下的是未初始化内存，导出的模型会静默产生错误输出——这是最危险的失败模式，必须挡下（[L93-L94](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L93-L94)）；buffer 通常可由框架重算或有默认值（[L105-L109](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L105-L109) 用 `if key in checkpoint_keys` 跳过缺失项），不影响权重正确性。

**练习 3**：numel 校验为什么比直接比较 `value.shape == destination.shape` 更宽容，同时又同样安全？

**答案**：`numel` 只要求元素总数相等（[L100-L104](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L100-L104)），允许 checkpoint 里的张量被拉平或维度排布不同，随后 `reshape` 统一；总数不一致则必然是匹配到了错误的张量，reshape 无从谈起，此时报错保证不会把形状不兼容的数据硬拷进去。

### 4.3 `generation_config.json` 的 chat EOS 写入

#### 4.3.1 概念说明

转换器写出的 HF 目录里，`generation_config.json` 的 `eos_token_id` 派生自 `config.json`——对 Olmo 3 tokenizer 来说那是 100257（`<|endoftext|>`）一个值。问题在于：**chat 模型每轮回复其实是以 `<|im_end|>`（100265）收尾的**。注释 [L134-L137](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L134-L137) 记录了不补上它的真实后果：

> vLLM 的 OpenAI server 从 `generation_config.json` 取停止符。一个没带 `<|im_end|>` 的 A4B SFT 导出，在 BFCL 上**把它的工具调用一路重复到 token 预算**——回合永远不结束。

所以脚本在转换完成后追加一步：把 `eos_token_id` 改写成列表 `[100265, 100257]`。生成时只要采样出 \( t \in \{100265,\ 100257\} \) 就停止——两个停止符都保留，`<|endoftext|>` 作为兜底。

这里的选择题值得注意：**为什么改 `generation_config.json`，而不去改 tokenizer？**（综合实践里会让你完整论证，先给要点：tokenizer 是被训练数据管线、chat template、无数下游共享的冻结工件，其 `eos_token` 语义是「训练/分词期」概念；「chat 回合用哪个 token 停」是**生成期部署配置**，`generation_config.json` 正是 HF 生态为此准备的、逐 checkpoint 可变的文件——chat 导出与基座导出可以不同，而不必 fork tokenizer。）

`--eos-token-ids` 的覆盖场景（文档字符串 [L30-L32](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L30-L32)）：默认列表是给「默认 Olmo 3 chat tokenizer 的 chat SFT 导出」用的；如果 tokenizer 的 id 不同，或者做**基座/midtrain 导出**（只应在 `<|endoftext|>` 处停），就显式传 `--eos-token-ids 100257`。

#### 4.3.2 核心流程

```text
convert_checkpoint_to_hf 写出输出目录（含 config.json / generation_config.json / 权重）
    ↓
write_generation_eos(输出目录, eos_token_ids):
    1. 读取 generation_config.json
    2. generation_config["eos_token_id"] = list(eos_token_ids)   # 标量 → 列表
    3. 以 indent=2 + 末尾换行 写回
    4. 记录日志
    ↓
main 打印 CONVERSION_OK
```

默认值来自模块级常量 `CHAT_EOS_TOKEN_IDS = (100265, 100257)`（[L114](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L114)），在 argparse 里转成 list 作为 `--eos-token-ids` 的 default——所以「不传」就得到 chat 列表，「传了」就是完全覆盖（不是追加）。

#### 4.3.3 源码精读

[L117-L123](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L117-L123) 是完整的 `write_generation_eos`：读 JSON、替换 `eos_token_id` 为列表、`json.dumps(..., indent=2) + "\n"` 写回（保持文件风格与 HF 生态一致），最后 `logger.info` 打印最终列表。函数文档字符串点明它存在的原因：「转换器从 config.json 派生 eos_token_id」——即 olmo-core 原生导出不会写 chat 停止列表，这步是补丁。调用点在 [L179](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L179)，紧接转换之后。

注释 [L137](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L137) 还写了这个补丁的退役条件：「一旦 olmo-core 钉住版本的 `save_hf_model` 自己写 chat 停止列表，就删掉这个覆盖」——这是一条有意识的技术债标记。

#### 4.3.4 代码实践

**实践目标**：单独验证 `write_generation_eos` 的改写行为（标量 → 列表、覆盖语义），不依赖 olmo-core。

**操作步骤**：

1. 运行下面的**示例代码**（忠实复刻 [L117-L123](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L117-L123) 的逻辑，作用于一个伪造的 `generation_config.json`）：

   ```python
   # 示例代码：复现 write_generation_eos 的改写逻辑
   import json, pathlib, tempfile

   def write_generation_eos(output_dir: pathlib.Path, eos_token_ids: list[int]) -> None:
       path = output_dir / "generation_config.json"
       generation_config = json.loads(path.read_text())
       generation_config["eos_token_id"] = list(eos_token_ids)
       path.write_text(json.dumps(generation_config, indent=2) + "\n")
       print("eos_token_id =", generation_config["eos_token_id"])

   with tempfile.TemporaryDirectory() as d:
       out = pathlib.Path(d)
       # 模拟 olmo-core 转换器刚写出的 generation_config.json：
       # eos_token_id 从 config.json 派生，只有 <|endoftext|> 一个
       (out / "generation_config.json").write_text(
           json.dumps({"eos_token_id": 100257, "do_sample": True}) + "\n"
       )
       print("改写前:", (out / "generation_config.json").read_text().strip())

       write_generation_eos(out, [100265, 100257])   # chat 导出（默认）
       print("改写后:", (out / "generation_config.json").read_text().strip())

       write_generation_eos(out, [100257])           # 基座/midtrain 导出（覆盖）
       print("再次覆盖:", (out / "generation_config.json").read_text().strip())
   ```

2. 观察三次文件内容的变化，注意其他键（`do_sample`）是否保留。

**需要观察的现象**：`eos_token_id` 从标量 `100257` 变为列表 `[100265, 100257]`；再传 `[100257]` 时变回单元素列表（不是追加、不是恢复标量）；`do_sample` 始终保留；文件带两空格缩进和末尾换行。

**预期结果**：如上所述。本实践纯标准库，可直接运行验证。

#### 4.3.5 小练习与答案

**练习 1**：默认 EOS 列表里为什么保留 100257（`<|endoftext|>`），而不是只留 100265（`<|im_end|>`）？

**答案**：chat 模型的每轮回复以 `<|im_end|>` 收尾，这是新增停止符的原因；但 `<|endoftext|>` 是 tokenizer 的原生 eos（[open_instruct/olmo_core_utils.py:L782-L789](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/open_instruct/olmo_core_utils.py#L782-L789) 提到该 tokenizer eos==bos==100257），保留它作为兜底停止符，避免模型偶尔直接输出 eos 时生成停不下来。列表语义是「任一命中即停」。

**练习 2**：`--eos-token-ids 100257` 与不传参数，写出的 `generation_config.json` 有什么区别？分别适用什么导出场景？

**答案**：不传默认写 `[100265, 100257]`，适用默认 Olmo 3 chat tokenizer 的 chat SFT 导出（文档字符串 [L30-L32](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L30-L32)）；传 `100257` 写 `[100257]`，适用基座/midtrain 导出（只应在 `<|endoftext|>` 处停）或 tokenizer 的 chat 停止符 id 不同的情形。

**练习 3**：为什么这个补丁写在 open-instruct 的薄封装里，而不是直接改 olmo-core？

**答案**：注释 [L137](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L137) 说明这是等上游的临时措施：olmo-core 是独立钉版的外部依赖（[pyproject.toml:L77](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/pyproject.toml#L77)），open-instruct 侧无法即时改它；在封装层补写 `generation_config.json` 立即止血，等 olmo-core 的 `save_hf_model` 自己写 chat 停止列表后再删掉覆盖。

### 4.4 校验策略与已知告诫

#### 4.4.1 概念说明

「转换」这件事最大的风险不是报错，而是**不报错但转错了**：权重布局对不上、dtype 溢出、块结构错位，都可能产出一个能加载、能生成、但 logits 全错的模型。olmo-core 的转换器因此提供严格校验：同一份权重分别用 olmo-core 实现和 HF 实现各跑一次前向，比对 logits。本脚本围绕这个校验做了三层决策，每层都对应一次真实事故：

1. **`--device` 默认 cuda**（不是 CPU）：KDA 用到的 fla Triton 内核拒绝 CPU 张量。若默认 CPU，权重会**先写完**、校验才崩——产物目录看起来转换成功，只是「没校验完」，恰恰是这种状态曾放行过一个 logit 错误率 38% 的 Olmo-Hybrid-7B 转换（[L145-L149](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L145-L149)）。默认 CUDA 让这类失败在写产物前就炸得明明白白。
2. **KDA 1.2B 上严格校验必然失败**：文档字符串 [L21-L28](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L21-L28) 记录：所有 OLMoE3 latent-KDA 1.2B checkpoint（包括一个已知良好的 midtrain checkpoint）上，两个前向实现都有约 92–99% 的 logits 不匹配、绝对差约 1.5；与 checkpoint、配置、`OLMO_HF_*` 对齐环境变量都无关（2026-08-20 完成二分排查）。严格校验只在 275M/480M 的阶梯模型上通过过。因此 scaling-ladders 生产负载的做法是 `--skip-validation --device cpu` + **用功能性评估替代校验**；并已验证 CPU 与 CUDA 两种设备上的转换结果在全部 35 GB 上逐位一致。
3. **失败要响亮**：默认值的设计哲学是「不确定时让失败尽早、尽吵」，而不是「尽量跑完」。这也是模块 4.2 里缺参数硬报错的同一思想。

除校验外，文档字符串还列出三条使用边界：

- **路径只支持本地/WEKA**（[L18-L19](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L18-L19)）：`-i` 会与 `pathlib` 拼接、EOS 步骤写本地目录，所以 `gs://` 之类的远端路径不可用。
- **GDN 混合 MoE 会走错导出器**（[L34-L37](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L34-L37)）：基于 GDN（Gated Delta Net）的混合 MoE 会让 olmo-core 的 `is_olmo_hybrid_model` 返回 True，从而被路由到 dense-hybrid 导出器，因缺 `feed_forward` 属性而崩溃。KDA 不受影响，因为 `KimiDeltaAttention` 不是 `GatedDeltaNet` 的子类。
- **olmo-core 版本前置**（[L5-L6](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L5-L6)）：需要 `akshitab/emo_modularity` 或更晚的 olmo-core；更早的 rev 对 peri-LN checkpoint 抛 `NotImplementedError`。

#### 4.4.2 核心流程

把「该传什么旗标」整理成决策表：

| 场景 | 命令 | 理由 |
| --- | --- | --- |
| 小模型（如 275M/480M 阶梯模型）、有 GPU | 默认（不传旗标） | 严格 logit 校验在这些规模上通过过；默认 `--device cuda` 让 Triton 内核可跑、失败发生在写产物前 |
| KDA 1.2B chat SFT 导出 | `--skip-validation --device cpu` | 严格校验在该规模必然失败（双实现分歧，非 checkpoint 问题）；改用功能性评估验证；CPU/GPU 转换已验证逐位一致 |
| 基座 / midtrain 导出 | 追加 `--eos-token-ids 100257` | 基座只应在 `<|endoftext|>` 停，不需要 chat 停止列表 |
| tokenizer 的 chat 停止符 id 非 100265 | `--eos-token-ids <实际id> ...` | 默认列表绑定默认 Olmo 3 chat tokenizer |
| 远端 `gs://` 路径 | 不支持 | 先同步到本地或 WEKA |

校验（`validate=True` 时）的内部流程：转换完成后，同一份 HF 权重分别在 olmo-core 实现与 HF 实现上前向，逐位置比对 logits；超出容差即失败。`--device` 同时充当导出设备与校验设备（[L176-L177](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L176-L177) 的 `device` 与 `validation_device`）。

#### 4.4.3 源码精读

- [L145-L150](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L145-L150)：`--device` 默认 CUDA 的完整理由写在注释里——Triton 内核对 CPU 张量报「Pointer argument (at 0) cannot be accessed from Triton」；而在 CPU 上「权重照样写出来」，失败形态恰是「已转换但未校验」的 checkpoint——正是放行过 38% logit 错误转换的那种状态，注释末尾写明「Do not silence it」（别把它静音掉）。
- [L21-L28](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L21-L28)：KDA 校验失效的完整记录（失配率、绝对差、二分日期、生产替代方案、35 GB 逐位一致的 CPU/CUDA 对照）——读源码时这类「事故备忘」是最宝贵的信息，它解释了为什么推荐旗标与直觉相反。
- [L17-L19](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L17-L19)：KDA 旗标建议与本地/WEKA 路径限制，写在用法示例的正下方，是使用前的第一屏。
- [L34-L37](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L34-L37)：GDN 混合 MoE 误路由的告诫及 KDA 不受影响的原因（类继承关系）。

对照材料：[scripts/train/olmo-hybrid/convert_instruct_sft_sweep.sh:L25-L30](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/olmo-hybrid/convert_instruct_sft_sweep.sh#L25-L30) 展示的早期做法是「用 gantry 起容器跑 olmo-core 侧自带的转换示例 + `--skip-validation`」；本脚本把这条路收进仓库、补上 OLMoDDP 布局与 EOS 处理，并**有意识地**选择了不同的校验默认值——可以体会两类脚本对「校验失败」的不同取舍。

#### 4.4.4 代码实践

**实践目标**：通过纯阅读完成三个旗标场景的推演，检验对校验策略的理解（本实践不需要 GPU，也不运行转换）。

**操作步骤**：

1. 精读 [L13-L37](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L13-L37)（用法示例 + 两段告诫）与 [L145-L150](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L145-L150)（设备默认值注释）。
2. 为下面三个场景各写出完整命令行（`-i`/`-o`/`-c` 用占位符）并写一句理由：
   - (a) 用默认 chat tokenizer 的 A4B chat SFT checkpoint 导出；
   - (b) 同一 run 的 midtrain checkpoint 导出（后续继续预训练用）；
   - (c) 没有可用 GPU 的机器上做 (a) 的导出。
3. 把你的答案与下文「预期结果」对照。

**需要观察的现象**：自己写出的三条命令在 `--skip-validation`、`--device`、`--eos-token-ids` 三个旗标上的差异。

**预期结果**：
- (a) `--skip-validation --device cpu`（KDA 1.2B 严格校验必然失败，生产做法），EOS 用默认列表；
- (b) 在 (a) 基础上加 `--eos-token-ids 100257`（基座/midtrain 只应在 `<|endoftext|>` 停）；
- (c) 与 (a) 相同——本就对 KDA 传 `--device cpu`；但要点是回答「如果这不是 KDA 而是小模型怎么办」：没有 GPU 时严格校验跑不了（Triton 拒 CPU），只能 `--skip-validation` 并务必用功能性评估补验，绝不能假装校验过了。

**待本地验证**：真实转换命令需 olmo-core checkpoint、满足版本要求的 olmo-core 与相应硬件，本机无法执行；以上为源码阅读推演。

#### 4.4.5 小练习与答案

**练习 1**：默认 `--device cuda` 防的是哪种具体假象？它是怎么发生的？

**答案**：防「已转换但未校验」的假象（[L145-L149](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L145-L149)）。若默认 CPU：校验阶段跑 KDA/fla 的 Triton 内核时因「Pointer argument (at 0) cannot be accessed from Triton」崩溃，而此时**权重已经写出**，输出目录看起来就是一次成功的转换、只是校验没跑完——历史上正是这种状态放行过 logit 错误率 38% 的 Olmo-Hybrid-7B 转换。默认 CUDA 使失败发生在写产物之前，无法被误读为成功。

**练习 2**：KDA 1.2B 上严格校验失败，为什么可以放心 `--skip-validation`？依据是什么？

**答案**：依据有二（[L21-L28](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L21-L28)）：其一，失败与 checkpoint 质量无关——包括已知良好 midtrain checkpoint 在内的**每一个** OLMoE3 latent-KDA 1.2B checkpoint 都同样失败（92–99% 失配、绝对差 ~1.5，二分排查排除了配置与环境变量），说明是两个前向实现在该规模上的固有分歧，不是转换错误；其二，CPU 与 CUDA 两种设备的转换产物已在全部 35 GB 上验证逐位一致。但「跳过」不等于「不验」：要用功能性评估（跑一个 eval）替代 logit 对比。

**练习 3**：一个基于 GDN 的混合 MoE checkpoint 误用本脚本会发生什么？为什么 KDA 没有这个问题？

**答案**：olmo-core 的 `is_olmo_hybrid_model` 对 GDN 混合 MoE 返回 True，于是被路由到 dense-hybrid 导出器，后者因模型缺少 `feed_forward` 属性而崩溃（[L34-L37](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L34-L37)）。KDA 不受影响，因为其注意力实现 `KimiDeltaAttention` 不是 `GatedDeltaNet` 的子类，不会被误判为 hybrid 模型。

## 5. 综合实践

把本讲四个模块串起来，完成一份「转换器走读报告」：

1. **候选键匹配与回退顺序图**。通读 `load_ddp_main_params`（[L58-L111](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L58-L111)），画出六类候选键（`model.x` / `model.module.x` / `x` / `module.x` / `x.main` / `module.x.main`）的匹配与回退顺序图：以「参数名 name」为入口，六个候选按序探测 `checkpoint_keys`，首中即出；全落空进 `missing`，任一 `missing` 触发 `RuntimeError`。在图旁用两三句话解释**为何缺任一参数权重必须直接报错**（提示：`to_empty` 的未初始化内存 + 「半转换模型比失败更危险」的教训）。
2. **三个旗标场景**。书面回答：
   - 基座/midtrain 导出应如何传 `--eos-token-ids`？（答：`--eos-token-ids 100257`，只留 `<|endoftext|>`。）
   - KDA 生产转换为何传 `--skip-validation --device cpu`？（答：1.2B 规模双实现分歧使严格校验必然失败且与 checkpoint 无关，生产以功能性评估替代；CPU/GPU 产物已验证逐位一致。）
   - 默认 `--device cuda` 防的是什么假象？（答：CPU 上 Triton 内核崩在权重写出之后，产出「已转换未校验」目录——曾放行 38% logit 错误的那种状态。）
3. **EOS 设计分析**。写一段 150 字左右的分析：为什么 EOS 列表写进 `generation_config.json` 而不是改 tokenizer？（要点：tokenizer 是共享冻结工件、`eos_token` 是分词期语义；chat 停止符是生成期部署配置，`generation_config.json` 逐 checkpoint 可变，chat/基座导出可不同且无需 fork tokenizer；脚本对 tokenizer 只引用不修改。）并回答：注释 [L137](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/scripts/train/debug/convert_moe_checkpoint_to_hf.py#L137) 说 olmo-core 钉住版本的 `save_hf_model` 自己写 chat 停止列表后，这个覆盖何时可以删除？（答：当本仓库升级到这样的 olmo-core rev——其导出原生把 chat 停止列表写进 `generation_config.json`——时，`write_generation_eos` 的默认改写成为冗余即可删；在那之前它防止 BFCL 式的工具调用循环。）

如果你手头有一个真实的 OLMoE3/KDA checkpoint 与满足要求的 olmo-core 环境，可加做一步：按 4.4.2 的决策表选旗标跑一次真实转换，用 `transformers` 加载输出目录、确认 `generation_config.json` 的 `eos_token_id` 为预期列表（**待本地验证**）。

## 6. 本讲小结

- 本脚本是 olmo-core 转换器的薄封装，补三件事：OLMoDDP `.main` 布局收集、chat EOS 写入、面向 KDA 的校验策略；模型配置必须从**训练配置 JSON**（含 `model` 与 `dataset.tokenizer` 节）传入，因为它的块结构才与保存权重对齐。
- OLMoDDP 融合优化器把权威权重存为 `module.<参数名>.main`；`load_ddp_main_params` 以 `.main` 后缀探测布局，按六类候选键首匹配收集，经张量类型与 numel 整型校验后 gather 成完整 CPU `state_dict`——缺任一参数权重直接 `RuntimeError`，不给未初始化内存留流出厂的机会。
- `generation_config.json` 默认写 `eos_token_id = [100265, 100257]`（`<|im_end|>`, `<|endoftext|>`）：vLLM OpenAI server 从这里取停止符，缺 `<|im_end|>` 曾使 A4B SFT 导出在 BFCL 上把工具调用重复到 token 预算；`--eos-token-ids` 可覆盖（基座/midtrain 传 `100257`）。
- `--device` 默认 `cuda` 是刻意选择：KDA 的 Triton 内核拒绝 CPU 张量，默认 CPU 会先写出权重再在校验处崩，制造「已转换但未校验」的假象——正是放行过 38% logit 错误转换的那种状态。
- 已知边界：KDA 1.2B 上严格 logit 校验必然失败（双实现固有分歧），生产用 `--skip-validation --device cpu` + 功能性评估，CPU/CPU 产物已验证 35 GB 逐位一致；路径仅限本地/WEKA；GDN 混合 MoE 会误入 dense-hybrid 导出器而崩溃，KDA 因 `KimiDeltaAttention` 不是 `GatedDeltaNet` 子类而免疫。

## 7. 下一步学习建议

- 下一讲建议进入 u8（MILES 单元）：从 u8-l1（MILES 总览与架构分层）开始，理解新一代 RL 系统如何组织 checkpoint 导出与评估（u8-l7 会从 MILES 视角再谈 checkpoint 转换/导出，与本讲互补）。
- 源码延伸阅读：`open_instruct/olmo_core_utils.py` 中 OLMoDDP 训练模块配置的构建（[L611-L648](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/open_instruct/olmo_core_utils.py#L611-L648)）——从训练侧理解 `.main` 布局的来源；以及 PyTorch `torch.distributed.checkpoint.state_dict` 的 `get_model_state_dict` / `StateDictOptions` 官方文档，理解 full state dict gather 的机制。
- 若想横向对比另一条导出路线，重读 u7-l3 中 `open_instruct/olmo_core_hybrid.py` 的 `convert_hybrid_state_to_hf` / `load_hf_hybrid_model`（[L230-L244](https://github.com/allenai/open-instruct/blob/28c1816664491493242d453c2c7280ee56e6c20a/open_instruct/olmo_core_hybrid.py#L230-L244)），并思考：dense-hybrid 转换在仓库内实现权重映射，而 MoE 转换委托给 olmo-core——两种分工各自的取舍是什么。
