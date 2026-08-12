# 部署导出 deploy

## 1. 本讲目标

`deploy` 是 AMCT 大模型训练后量化（PTQ）四阶段链路 `eval → extract_ptq_data → ptq → deploy` 的最后一站。前三站在「浮点空间」里训练量化参数，`deploy` 则把这些参数**烘焙（bake）**进权重，产出一份能被推理引擎（vLLM / MindIE 等）直接加载的低比特 checkpoint。

学完本讲，你应当能够：

1. 说清 `deploy` 在四阶段链路中的定位，以及它为什么需要 `block` 和 `tensor` 两种导出粒度。
2. 跟踪 block 粒度导出的完整链路：逐层重建量化块 → 读回 PTQ 参数 → 调 `export_deploy` 产出 payload → 重写 `model.safetensors.index.json`。
3. 理解 `generate_quant_config` 如何按 `is_mx` 开关生成 `compressed-tensors` 格式的 `quantization_config`，以及它写入 `config.json` 的字段含义。
4. 区分 block 模式（烘焙 PTQ 结果）与 tensor 模式（FP8→bf16 反量化、或直接重新量化为 int/mxfp）的适用场景。
5. 看懂 `QuantLinear.export_deploy` → `WeightQuantizer.export_deploy` → 数据类型 `export_deploy` 三层调用产出的 `{qweight, weight_scale, ...}` payload 契约。

## 2. 前置知识

在进入源码前，先建立几个关键概念。

- **四阶段链路与目录接力**：`ptq` 阶段把每个量化单元的可学习参数存成 `layer_{idx}_{save_name}.pt`，落到 `*_param_dir` 目录；`deploy` 通过 `--attn_linear_param_dir` / `--moe_mlp_param_dir` 等参数把这些 `.pt` 读回来（详见 u4-l2）。所以 `deploy` 的输入是「原始浮点模型 + PTQ 参数目录」，输出是「可直接部署的低比特模型目录」。
- **safetensors 与 weight index**：HuggingFace 系大模型权重以 `.safetensors` 分片存储，`model.safetensors.index.json` 里的 `weight_map` 记录「每个权重张量名 → 它在哪个分片文件」。`deploy` 要重写这份索引，因为量化后权重名会变（多出 `.weight_scale` 等）、分片也会重组。
- **compressed-tensors 格式**：一种被 vLLM 等引擎识别的量化描述格式，写在 `config.json` 的 `quantization_config` 字段里，告诉引擎「这些层是 int/mxfp 量化的、位宽多少、哪些层要忽略」。`deploy` 负责生成它。
- **烘焙（bake）**：把训练态的「伪量化」（fake-quant，前向时即时量化/反量化）转换为部署态的「真量化」（real-quant，权重已固化成低比特整数 + scale）。`export_deploy` 就是这个转换函数。
- **payload**：一个权重经 `export_deploy` 后产出的字典，至少含 `qweight`（量化后权重），通常还含 `weight_scale`、`weight_bias` 等附加张量。

> 本讲承接 u4-l2（PTQ 主流程）与 u3-l4（BitPolicy 位宽配置），假设你已了解 PtqUnit、`*_param_dir`、`quant_dtype`（int/mxfp/hifp）等概念。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [amct_pytorch/workflows/llm_deploy.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py) | `LlmDeployWorkflow`：deploy 命令的编排骨架，含 block/tensor 两种 `_run_*` 与 weight index 重写。 |
| [amct_pytorch/common/models/llm/common/deploy_export.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py) | 四个核心函数：`export_block_deploy`、`generate_quant_config`、`convert_state_dict`、`quant_payload`。 |
| [amct_pytorch/common/models/llm/common/base.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py) | `BaseModel` 提供 `iter_deploy_bindings`、`build_quant_block`、`load_selected_layer_ptq_params` 等被 deploy 调用的模型适配接口。 |
| [amct_pytorch/quantization/modules/quant_linear.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_linear.py) | `QuantLinear.export_deploy`：block 模式的 payload 入口。 |
| [amct_pytorch/quantization/modules/quant_base.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/modules/quant_base.py) | `WeightQuantizer.export_deploy`：区分普通算法与带 `quantize()` hook 的算法（如 FlatQuant）两条落盘路径。 |
| [amct_pytorch/quantization/dtypes/int.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/int.py) / [mxfp.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/mxfp.py) | int / mxfp 数据类型的 `export_deploy`，产出 payload 的最底层。 |
| [amct_pytorch/cli/llm/deploy.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/deploy.py) | deploy 命令真入口：`parser_gen(command="deploy")` → `LlmDeployWorkflow` → `run()`。 |

## 4. 核心概念与源码讲解

### 4.1 deploy 的定位与两种粒度分发

#### 4.1.1 概念说明

`deploy` 的职责是把量化结果固化为可部署权重。但「量化结果」从哪来，决定了它有两种工作模式：

- **block 粒度**：模型跑过完整的 `ptq` 流程，每层有训练好的 PTQ 参数（`.pt` 文件）。`deploy` 需要逐层重建量化块、读回这些参数、再把每个 `QuantLinear` 烘焙成低比特权重。这是 PTQ 链路的正常出口。
- **tensor 粒度**：模型已经是某个低比特格式（典型是 FP8 + `scale_inv`），但你想**不经过 PTQ** 直接转成另一种格式——例如把 FP8 权重反量化回 bf16，或直接重新量化成 int/mxfp。这时没有 PTQ 参数可读，只做张量级别的格式转换。

这两种模式由命令行参数 `--granularity` 选择，互斥分发。注意：u3-l2 提过 granularity 在不同阶段含义不同，这里 deploy 的 `block`/`tensor` 与 extract/ptq 阶段保持同名但走完全不同的代码路径。

`LlmDeployWorkflow.__init__` 还预先算好三个布尔标志，用于后续配置生成时区分数据类型家族：

[amct_pytorch/workflows/llm_deploy.py:50-60](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L50-L60) —— `is_mx` / `is_int` / `is_hif` 分别由 `quant_dtype` 是否以 `"mx"` / `"int"` / `"hif"` 开头判定（`quant_dtype` 取值为 `int` / `mxfp` / `hifp` / `bf16`）。这三个标志贯穿整个 deploy 流程，其中 `is_mx` 直接决定 `quantization_config` 里 `qtype` 是 `float` 还是 `int`（见 4.3）。

#### 4.1.2 核心流程

`run()` 的控制流非常简洁——和其它三个 Workflow 同构（u3-l2 讲过 `setup → 分发 → remove(sink_id)` 三段式）：

```
run()
 ├─ setup()                      # 建目录、注册组件、建 pipeline、挂日志
 ├─ 按 granularity 分发：
 │    ├─ "block"  → _run_blockwise()    # 烘焙 PTQ 结果
 │    └─ "tensor" → _run_tensorwise()   # 张量级格式转换
 │    └─ 其它     → raise ValueError
 └─ logger.remove(sink_id)       # 卸除临时日志 sink
```

[amct_pytorch/workflows/llm_deploy.py:87-98](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L87-L98) 是分发主体。`setup()` 与 u3-l2 描述的四步一致（[第 100-106 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L100-L106)）：建 `output_dir` → `_register_components()`（惰性注册模型/数据类型/算法）→ `_build_pipeline()`（从 `MODEL_REGISTRY` 取适配器）→ 挂日志 sink。其中 `_register_components` 只注册三类（[第 68-72 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L68-L72)），**不注册 SOLVER_REGISTRY**——因为 deploy 不再做训练/优化，无需求解器。

#### 4.1.3 源码精读

部署命令的调用入口与 examples 脚本，帮助你把命令行参数和 Workflow 串起来：

[amct_pytorch/cli/llm/deploy.py:22-25](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/deploy.py#L22-L25) —— 命令真入口，固定三段式 `parser_gen(command="deploy")` → `LlmDeployWorkflow(args)` → `run()`。

[examples/deploy.sh:19-27](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/deploy.sh#L19-L27) —— 一条典型的 block 模式 deploy 命令：`--granularity block --quant_target mlp attn-linear --quant_dtype int --bit_config .../w8a8.yaml`。注意它**没有**显式传 `*_param_dir`，实际使用时要补上 ptq 阶段产出的参数目录。

#### 4.1.4 代码实践

1. **实践目标**：理清 granularity 分发与数据类型标志。
2. **操作步骤**：
   - 打开 [llm_deploy.py 的 `__init__`](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L50-L60) 与 [`run`](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L87-L98)。
   - 在纸上列一张表，对 `quant_dtype` 的四种取值 `int` / `mxfp` / `hifp` / `bf16`，分别写出 `is_mx` / `is_int` / `is_hif` 的真假。
3. **需要观察的现象**：注意 `bf16` 三个标志全为 `False`——这意味着它既不走 mxfp 的 `float` 分支，也不走 int 分支。
4. **预期结果**：`int→(F,T,F)`、`mxfp→(T,F,F)`、`hifp→(F,F,T)`、`bf16→(F,F,F)`。`is_mx` 会在 4.3 直接决定 `qtype`，是本讲最关键的一个布尔位。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_register_components` 在 deploy 阶段不注册 `SOLVER_REGISTRY`？
> **答**：deploy 是烘焙导出，不再做任何优化训练，用不到求解器；它只需要模型适配器（重建量化块）、数据类型（export_deploy 落盘）和算法（FlatQuant 等带 `quantize()` hook 的算法在导出时仍需其 `export_deploy`）。

**练习 2**：如果用户传了一个 `--granularity model`，deploy 会怎样？
> **答**：`run()` 的 if/elif 都不命中，走到 [第 94-96 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L94-L96) 的 `else` 分支，抛出 `ValueError("Unsupported granularity 'model' for deploy.")`。deploy 只接受 block 与 tensor。

---

### 4.2 block 粒度导出：逐层烘焙与 weight index 重写

#### 4.2.1 概念说明

block 模式是 PTQ 链路的正常出口，做四件事：

1. **重建量化块**：对每个 decoder layer，用模型适配器构建一个带 `QuantLinear` 的量化块，并把该层在 ptq 阶段训练好的参数（`.pt`）加载进去——这一步把「训练成果」装回模块。
2. **烘焙每个 QuantLinear**：遍历块内所有 `QuantLinear`，调 `module.export_deploy()` 得到 payload（`qweight` + scale 等），按下划线规则命名后收集。
3. **挑出需忽略的 Linear**：块里不只有被量化的 `QuantLinear`，还有没量化的 `PlainLinear` 和原始 `nn.Linear`，后两者要写进 `config.json` 的 `ignore` 列表，否则推理引擎会误判。
4. **重写权重分片与索引**：量化层产出新分片 `layer_XXX.safetensors`；未被量化的权重（embed_tokens、norm、lm_head 等）重新打包成 `rest_XXXXX.safetensors`；最后重写 `model.safetensors.index.json` 的 `weight_map` 和 `total_size`。

#### 4.2.2 核心流程

`_run_blockwise` 的主干（[第 208-247 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L208-L247)）：

```
_run_blockwise()
 ├─ _copy_support_files()                 # 复制 config.json/tokenizer 等非权重文件
 ├─ original_index = _load_weight_index() # 读原始 index.json（或单分片合成）
 ├─ 逐层循环 for layer_idx in range(num_layers):
 │     ├─ layer_tensors, tensor_routes = export_block_deploy(pipeline, layer_idx, quant_ignore_layers)
 │     ├─ updated_weight_map.update(_write_block_file(layer_idx, layer_tensors))  # 写 layer_XXX.safetensors
 │     └─ replaced_original_weights.update(_collect_replaced_original_weights(...))
 ├─ updated_weight_map.update(_write_remaining_original_weights(...))  # 写 rest_XXXXX.safetensors
 ├─ _refresh_weight_index(...)            # 重写 index.json
 └─ _refresh_config(quant_ignore_layers)  # 重写 config.json 的 quantization_config
```

其中 `export_block_deploy` 是每层的核心（[deploy_export.py 第 136-153 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L136-L153)）：

```
export_block_deploy(pipeline, layer_idx, quant_ignore_layers)
 ├─ block = pipeline.build_quant_block(layer_idx).to(device).eval()
 ├─ pipeline.load_selected_layer_ptq_params(layer_idx, block, strict=False)  # 装回 .pt
 ├─ quant_ignore_layers.extend(get_quant_ignore_linear_names(block, weight_prefix))
 └─ for weight_key, module in pipeline.iter_deploy_bindings(layer_idx, block):  # 只 yield QuantLinear
        payload = module.export_deploy()
        deploy_tensors[weight_key]            = payload["qweight"]
        对每个 extra(scale/bias):
            deploy_tensors[weight_key.replace(".weight", f".{extra_name}")] = extra_tensor
```

payload 的命名映射规则很关键：权重键 `xxx.weight` 的 `qweight` 直接占用 `xxx.weight`，附加张量则把 `.weight` 替换成 `.{extra_name}`——例如 `weight_scale` → `xxx.weight_scale`、`weight_bias` → `xxx.weight_bias`。这正是 compressed-tensors / MindIE 约定的 scale 命名。

#### 4.2.3 源码精读

**`get_quant_ignore_linear_names`——区分三种 Linear（本讲核心之一）**

[amct_pytorch/common/models/llm/common/deploy_export.py:101-133](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L101-L133) 把块内模块分三类：

- `QuantLinear`：已被量化的，**跳过**（它的权重已由 `iter_deploy_bindings` 烘焙产出，不能再进 ignore）。
- `PlainLinear`：AMCT 的「占位兼容线性层」（见 u5-l3，为对齐 `QuantLinear` 签名而包装原始 Linear），**整层进 ignore**；但其内部那个真正的 `nn.Linear` 子模块（如 `self_attn.kv_b_proj.linear`）要跳过，避免和包装路径 `self_attn.kv_b_proj` 重复。
- 原始 `nn.Linear`：既不是 QuantLinear 也不在 PlainLinear 内部的，**进 ignore**。

代码用两个前缀元组（`quant_linear_prefixes` / `plain_linear_prefixes`）配合 `name.startswith(prefix)` 实现这个三层判定（[第 107-131 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L107-L131)）。

**`iter_deploy_bindings`——只把 QuantLinear 暴露给烘焙**

[amct_pytorch/common/models/llm/common/base.py:324-329](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L324-L329) 是基类实现：遍历块内模块，**只**对 `QuantLinear` yield `(f"{weight_prefix}{name}.weight", module)`。这保证了只有被量化的层会被 `export_deploy`，与 ignore 列表互为补集。部分模型（如 glm5_2）会覆写它，把打包的 MoE expert 路径翻译回 checkpoint 里的解包路径（[glm5_2.py 第 210-229 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/glm/glm5_2/glm5_2.py#L210-L229)）。

**分片写入与剩余权重打包**

[llm_deploy.py 第 303-307 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L303-L307) `_write_block_file`：每层量化结果写一个 `layer_{idx:0{width}d}.safetensors`，`width` 按层数位数对齐（如 32 层 → 两位补零 `layer_00.safetensors`）。

[第 309-351 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L309-L351) `_write_remaining_original_weights`：处理「没被任何 QuantLinear 替换掉的原始权重」（embed_tokens、norm、lm_head、以及未量化层）。它把剩余权重按 8GB 上限（`max_shard_size = 8 * 1024**3`）重新分片为 `rest_00000.safetensors`、`rest_00001.safetensors`……。

**index 重写**

[第 192-206 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L192-L206) `_refresh_weight_index`：遍历 `updated_weight_map` 的所有分片文件，按**实际磁盘大小**累加得到 `total_size`（不是用原始值），写出新的 `model.safetensors.index.json`。`_collect_replaced_original_weights`（[第 74-85 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L74-L85)）借助 `tensor_routes` 把附加键（`.weight_scale` 等）回溯到原始 `.weight`，判定哪些原始权重已被替代、不该再进 `rest_*`。

> 小贴士：`_write_safetensor_file`（[第 353-359 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L353-L359)）用「先写 `.tmp` 再 `os.replace`」的原子写，避免写到一半进程被杀产生半个分片。

#### 4.2.4 代码实践

1. **实践目标**：验证三种 Linear 的归类逻辑。
2. **操作步骤**：阅读 [get_quant_ignore_linear_names](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L101-L133)，假设一个块里有：`self_attn.q_proj`（QuantLinear）、`self_attn.kv_b_proj`（PlainLinear，内部含 `.linear`）、`self_attn.o_proj`（原始 nn.Linear）。手推 `quant_ignore_layers` 会追加哪些名字。
3. **需要观察的现象**：注意 `iter_deploy_bindings` 只 yield `q_proj` 一个，而 ignore 列表收集的是另外两类。
4. **预期结果**：`iter_deploy_bindings` 烘焙 `...self_attn.q_proj.weight`（+ scale/bias）；ignore 追加 `...self_attn.kv_b_proj`（PlainLinear 整层，其内部 `.linear` 被前缀判定跳过）和 `...self_attn.o_proj`（原始 Linear）；`q_proj` 因是 QuantLinear 既不进 ignore 也不重复。三类各得其所，无遗漏无重复。**待本地验证**：若有 AMCT 环境，可在 `export_block_deploy` 调用前后打印 `quant_ignore_layers` 对照。

#### 4.2.5 小练习与答案

**练习 1**：为什么 PlainLinear 内部的 `nn.Linear`（如 `kv_b_proj.linear`）不能单独进 ignore？
> **答**：因为它的包装层 `kv_b_proj`（PlainLinear）已经整层进了 ignore（[第 120-122 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L120-L122)）。若再把内部 `.linear` 也加进去，会产生 `kv_b_proj` 和 `kv_b_proj.linear` 两条重叠条目，推理引擎按前缀匹配时会重复处理。所以用 `plain_linear_prefixes` 把内部子模块显式跳过（[第 129-130 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L129-L130)）。

**练习 2**：`_refresh_weight_index` 为什么不直接沿用原始 `total_size`？
> **答**：量化后权重的字节数变了（INT8 权重比 bf16 小一半，还新增了 scale 张量），分片大小完全不同。`total_size` 必须按输出目录里实际分片文件大小重新累加（[第 194-197 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L194-L197)），否则索引里的元数据与磁盘不符。

**练习 3**：`tensor_routes` 在 `_collect_replaced_original_weights` 里起什么作用？
> **答**：一个原始 `.weight` 烘焙后可能裂成多个键（`.weight` + `.weight_scale` + `.weight_bias`），`tensor_routes` 把这些裂出来的键都映射回原始的 `weight_key`（[export_block_deploy 第 146/152 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L146-L152)）。判定「替代」时只需检查回溯后的基名是否在原始 `weight_map` 中，避免把 `.weight_scale` 误当成独立的未替代权重。

---

### 4.3 quantization_config 生成：compressed-tensors 格式与 is_mx 分支

#### 4.3.1 概念说明

光产出量化权重还不够，推理引擎需要一份「说明书」才知道怎么解读它们。这份说明书就是 `config.json` 里的 `quantization_config`，采用 `compressed-tensors` 格式。它至少说明：

- **config_groups**：把模型里的模块按类型（`Linear`、`MoEGMM` 等）分组，每组声明权重/激活的位宽、量化策略（per-channel / per-token / per-group）、observer 类型。
- **format**：`int-quantized` 或 `float-quantized`。
- **ignore**：哪些模块**不**量化（即 4.2 收集的 PlainLinear / 原始 Linear 名单）。
- **quant_method**：固定 `compressed-tensors`（部分 mxfp 场景由 `cache_scheme` 改写为 `mxfp8`）。

`is_mx` 是这里的总开关：它决定 `qtype` 是 `float`（mxfp）还是 `int`，进而决定 observer、strategy、group_size、format 全套参数。

#### 4.3.2 核心流程

`_refresh_config`（block 模式，[第 159-180 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L159-L180)）：

```
_refresh_config(quant_ignore_layers)
 ├─ 读 output_dir/config.json
 ├─ 若 quant_dtype is not None:
 │     ├─ cache_scheme = pipeline.cache_scheme()    # kv_cache / li_cache 方案（可选）
 │     ├─ bits_scheme  = pipeline.bits_scheme()     # 每组 (targets, w_bits, a_bits)（可选）
 │     ├─ quantization_config = generate_quant_config(cache_scheme, ignores, is_mx, bits_scheme)
 │     └─ config["quantization_config"] = quantization_config
 ├─ 否则: config.pop("quantization_config", None)
 └─ 写回 config.json
```

`generate_quant_config`（[deploy_export.py 第 70-98 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L70-L98)）的核心是第一行 `qtype = "float" if is_mx else "int"`——这是本讲实践任务要解释的关键判定。然后按 `bits_scheme` 逐组调 `generate_quant_group` 填充位宽与策略。

#### 4.3.3 源码精读

**`generate_quant_group`——is_mx 如何分化整套参数**

[deploy_export.py 第 27-60 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L27-L60) 根据 `qtype` 取值分流：

| 参数 | qtype="float"（mxfp） | qtype="int" |
| --- | --- | --- |
| `observer` | `minmax` | `memoryless` |
| 激活 `strategy` | `group` | `token` |
| 权重 `strategy` | `group` | `channel` |
| `group_size` | `32` | `None` |

这正好对应 u2-l2 讲过的两类格式差异：mxfp 是「沿权重 -1 轴每 32 元素共享指数」的 per-group 浮点量化，所以 observer 用 minmax、strategy 用 group、group_size=32；而 int 是朴素的 per-channel（权重）/ per-token（激活）整数量化，无需 group_size。`generate_quant_config` 在 `is_mx` 为真时还会额外塞入 `weight_block_size: [1, 32]`（[第 96-97 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L96-L97)），再次点明 32 的共享块大小。`format` 字段也由它决定：`"float-quantized" if is_mx else "int-quantized"`（[第 88 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L88)）。

**bits_scheme 与 cache_scheme——由模型适配器提供**

`bits_scheme` 默认是 `_default_bits_scheme()`（[第 63-67 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L63-L67)，两组都是 W8A8 的 `Linear` + `MoEGMM`），但适配器可覆写。例如 glm5_2 的 [bits_scheme](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/glm/glm5_2/glm5_2.py#L192-L208) 会读 BitPolicy：默认组用全局 `w_bits/a_bits`，仅当 MoE routed 权重是 W4 时才追加一组 `MoEGMM` W4A8。`cache_scheme`（[glm5_2 第 157-190 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/glm/glm5_2/glm5_2.py#L157-L190)）则描述 KV cache 是否量化，会被 `update` 进 `quant_config`（[第 94-95 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L94-L95)）。两者都是「模型相关、非通用」的信息，所以放在适配器里而非 deploy_export.py。

> 注：`generate_quant_config` 用 `config.update(cache_scheme)` 合并 cache 方案，这意味着 `cache_scheme` 返回的键（如 `kv_cache_scheme`、`quant_method`）可以**覆盖**前面设的字段——例如 w8a8-mxfp 场景会把 `quant_method` 从 `compressed-tensors` 改成 `mxfp8`（[glm5_2 第 188-189 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/glm/glm5_2/glm5_2.py#L188-L189)）。

#### 4.3.4 代码实践

1. **实践目标**：解释 `is_mx` 如何决定 `qtype`，并对比两种输出。
2. **操作步骤**：阅读 [generate_quant_config](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L70-L98) 与 [generate_quant_group](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L27-L60)。假设 `quant_dtype="mxfp"`、`bits_scheme` 取默认值，手写 `quantization_config` 的关键字段；再假设 `quant_dtype="int"` 重做一次。
3. **需要观察的现象**：重点看 `qtype`、`format`、`group_size`、`weight_block_size` 这几项在两种数据类型下的差异。
4. **预期结果**：
   - mxfp：`qtype="float"`、`format="float-quantized"`、每组 `group_size=32`、权重/激活 strategy 均为 `group`，顶层多出 `weight_block_size:[1,32]`。
   - int：`qtype="int"`、`format="int-quantized"`、`group_size=None`、权重 `channel`/激活 `token`，无 `weight_block_size`。
   两者 `quant_method` 都为 `compressed-tensors`、`ignore` 都来自 4.2 的 `quant_ignore_layers`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 mxfp 的权重 strategy 是 `group` 而 int 是 `channel`？
> **答**：mxfp 每 32 个元素共享一个指数（per-group，group_size=32），所以 strategy 必须是 `group` 来匹配共享块结构；int 整数量化通常按输出通道共享一个 scale（per-channel），没有 32 的子块概念，故 strategy 为 `channel`、group_size 为 None。

**练习 2**：`_refresh_config` 中 `cache_scheme_fn` / `bits_scheme_fn` 用 `getattr(..., None)` + `callable` 判定，为什么这么谨慎？
> **答**：并非所有模型适配器都实现了 `cache_scheme` / `bits_scheme`（基类就没定义它们）。用 `getattr + callable` 探测后，缺失时返回 `None`，`generate_quant_config` 再回退到 `_default_bits_scheme()`（[第 74-75 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L74-L75)）。这样新模型即便不覆写也能导出一份可用的默认 W8A8 配置。

**练习 3**：如果 `quant_dtype` 为 `None`，`_refresh_config` 会做什么？
> **答**：走 [第 175-176 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L175-L176) 的 `else`，`config.pop("quantization_config", None)`——即移除任何已有的量化配置字段，相当于导出一个「不声明量化」的模型。

---

### 4.4 tensor 粒度导出：FP8→bf16 反量化与 int/mxfp 的 quant_payload

#### 4.4.1 概念说明

tensor 模式**不跑 PTQ**、不读 `.pt` 参数，它对原始权重做纯格式转换，面向两类需求：

1. **FP8 → bf16 反量化**：源模型是 FP8（每个权重 1 字节，配 `scale_inv` 缩放因子），目标是转回 bf16 浮点。典型场景是从一个 FP8 checkpoint 转出可被不支持 FP8 的框架加载的 bf16 模型。
2. **直接重新量化为 int/mxfp**：把当前（bf16）权重直接按数据类型量化规则压成 int 或 mxfp，不经过任何校准/训练。这相当于「Min-Max 式」的朴素量化导出。

与 block 模式「逐层重建 + 装回 PTQ 参数 + 算法烘焙」相比，tensor 模式没有 `QuantLinear`、没有算法，只用数据类型对象的 `export_deploy` 直接落盘。

#### 4.4.2 核心流程

`_run_tensorwise`（[第 249-301 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L249-L301)）按**源分片文件**循环（与 block 模式的按层循环不同）：

```
_run_tensorwise()
 ├─ _copy_support_files()
 ├─ quant_layers    = pipeline.generate_tensorwise_quant_layers()   # {层名(去.weight): bit}
 ├─ quant_ignore_layers = pipeline.generate_tensorwise_ignore_layers()
 ├─ for source_file in 原始分片:
 │     current_state_dict = load_file(source_file)
 │     for weight_name, weight in current_state_dict:
 │         若是 .scale 张量: 跳过
 │         weight = convert_state_dict(...)        # FP8 → bf16（若是 1 字节权重）
 │         new_state_dict[weight_name] = weight
 │         若 quant_dtype in ["int","mxfp"] 且该层在 quant_layers:
 │             new_state_dict.update(quant_payload(quant_cls, weight_name, weight, bit))
 │     _write_safetensor_file(source_file, new_state_dict)
 ├─ _refresh_weight_index(...)
 └─ _refresh_config(quant_ignore_layers)
```

注意 tensor 模式**保持原始分片结构**（每个源文件还是写成同名分片），只是替换文件内容；而 block 模式会把分片彻底重组成 `layer_*` / `rest_*`。

#### 4.4.3 源码精读

**`convert_state_dict`——FP8→bf16 反量化**

[deploy_export.py 第 156-184 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L156-L184) 的判定入口是 `weight.element_size() == 1`——FP8 张量每元素 1 字节，bf16/fp16 都是 2 字节，由此识别。找到对应的 `scale_inv`（按 `scale_inv_name` 从 `original_weight_map` 定位它所在的分片，按需加载并缓存到 `loaded_files`）后调 `weight_dequant`。MX 打包形式（`torch.int8`）走 `is_mx=True, is_packed=True` 分支，普通 FP8 走默认分支。概念上反量化即：

\[ x_{\text{bf16}} = \mathrm{dequant}(x_{\text{fp8}},\ \text{scale\_inv},\ \text{block\_size}) \]

其中 MX 模式下 \(\text{block\_size}=32\)（来自 [BaseModel.block_size](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L76-L78)，每 32 元素共享指数）。缺 `scale_inv` 时打 warning 并跳过（[第 180-183 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L180-L183)）。

**`quant_payload`——直接重新量化落盘**

[deploy_export.py 第 187-196 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L187-L196)：现场 `quant_obj = quant_cls(bits=int(bit))` 新建一个数据类型量化器（从 `DTYPE_REGISTRY` 取，不经过任何算法），调 `quant_obj.export_deploy(weight)` 得到 payload，再用与 block 模式**完全相同**的命名规则（`.weight` → `.weight_scale` 等）展开成张量字典。它与 block 模式的区别在于：block 是 `QuantLinear` 整模块导出（带算法参数），tensor 是裸权重 + 裸数据类型导出。

**两层数据类型 export_deploy 的 payload 契约**

- int：[int.py 第 50-56 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/int.py#L50-L56) 产出 `{qweight, weight_scale, weight_bias(或 None)}`，由 `weight_quant(..., real_quant=True)` 做真量化。
- mxfp：[mxfp.py 第 52-57 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/mxfp.py#L52-L57) 产出 `{qweight, weight_scale}`，`qweight` 是 `float8_e4m3fn`（8-bit）或打包 uint4（4-bit），`weight_scale` 是共享指数 `e8m0`（见 `deploy` 方法 [第 41-50 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/dtypes/mxfp.py#L41-L50)）。

**`generate_tensorwise_quant_layers` / `..._ignore_layers`**

这两个由模型适配器实现（基类 [base.py 第 109-113 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L109-L113) 直接 `raise NotImplementedError`）。例如 glm5_2 的 [generate_tensorwise_quant_layers](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/glm/glm5_2/glm5_2.py#L231-L254) 遍历所有层 + expert，按 BitPolicy 给每个待量化层名打上位宽；[generate_tensorwise_ignore_layers](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/glm/glm5_2/glm5_2.py#L256-L274) 则生成要写进 `ignore` 的模块名（如 indexer 的 `wk`、`weights_proj`）。它们是 tensor 模式专属的「量化/忽略名单」，作用类比 block 模式的 `iter_deploy_bindings` + `get_quant_ignore_linear_names`，但因为不构建量化模块，只能靠配置枚举。

> 补充：`_refresh_config_tensor`（[第 182-190 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L182-L190)）是 bf16 专用变体——设 `torch_dtype=bfloat16` 并移除 `quantization_config`，对应 `_convert_tensor` 只支持 `bf16`（[第 112-117 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L112-L117)）。`_run_tensorwise` 末尾统一调 `_refresh_config`，由它按 `quant_dtype` 决定是否生成量化配置。

#### 4.4.4 代码实践

1. **实践目标**：跟踪一个 FP8 权重在 tensor 模式下的两种去向。
2. **操作步骤**：阅读 [_run_tensorwise](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L249-L301) 与 [convert_state_dict](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L156-L184)。假设源模型某层 `mlp.gate_proj.weight` 是 FP8（element_size=1）、且同名 `.scale` 存在，分别推演 `--quant_dtype bf16` 和 `--quant_dtype int` 两种命令下，输出分片里该层会变成什么、多出哪些键。
3. **需要观察的现象**：注意 `.scale` 张量本身在循环开头被跳过（[第 270-271 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L270-L271)），以及 `convert_state_dict` 总是先把 FP8 反量化成高精度，再决定是否重新量化。
4. **预期结果**：
   - bf16：`mlp.gate_proj.weight` 变为 bf16 张量，原始 `.scale` 被丢弃，不新增键。
   - int（该层在 `quant_layers`）：先反量化为高精度，再经 `quant_payload` 产 INT 量化结果——`mlp.gate_proj.weight`（qweight）+ 新增 `mlp.gate_proj.weight_scale`（+ `weight_bias`）。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：tensor 模式为什么按「源分片文件」循环，而 block 模式按「层」循环？
> **答**：block 模式要逐层构建 `QuantLinear` 量化块、装回 PTQ 参数再烘焙，天然以层为单位；tensor 模式不构建模块，只对张量做格式转换，按源分片读入/写出最直接，也保留了原始分片结构（同名文件）。

**练习 2**：`quant_payload` 里 `new_weight_name = weight_name.rsplit(".", 1)[0]` 的作用是什么？
> **答**：把 `xxx.weight` 去掉后缀得到 `xxx`（[第 286 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L286)），用来在 `quant_layers`（键是去掉 `.weight` 的层名，见 [glm5_2 第 232 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/glm/glm5_2/glm5_2.py#L232) docstring）里查该层的位宽 `bit`，从而决定用几比特重新量化。

**练习 3**：为什么 `convert_state_dict` 用 `element_size() == 1` 而不是 `dtype == torch.float8_e4m3fn` 来识别 FP8？
> **答**：MX 打包形式下权重可能是 `torch.int8`（打包后的 uint4 解包为 int8 存储，[第 174-177 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L174-L177)），dtype 不唯一，但它们的共同特征是「每元素 1 字节」。用 `element_size() == 1` 能统一识别这两种 FP8 存储形式，再在内部按 dtype 分流调用不同的 `weight_dequant` 参数。

---

## 5. 综合实践

本实践把 4.2 与 4.3 串起来，对应本讲规格里的核心实践任务。

**任务**：对照 [`_run_blockwise`](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L208-L247)，画一张 block 模式导出的数据流图，并回答两个关键问题。

**步骤**：

1. **画出三层循环结构**：`_run_blockwise`（逐层）→ `export_block_deploy`（逐 QuantLinear）→ `module.export_deploy()`（产 payload）。标注每一层的输入输出：层循环输入 `layer_idx`、输出 `layer_XXX.safetensors`；模块循环输入 `QuantLinear`、输出 `{qweight, weight_scale, ...}`。

2. **回答问题一（区分三种 Linear）**：参考 [`get_quant_ignore_linear_names`](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L101-L133)，说明导出时如何区分：
   - 「已被量化的 `QuantLinear`」——由 [`iter_deploy_bindings`](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L324-L329) yield 出来烘焙成 payload，**不**进 ignore；
   - 「`PlainLinear` 与原始 `nn.Linear`」——由 `get_quant_ignore_linear_names` 收集进 `quant_ignore_layers`，最终写入 `config.json` 的 `quantization_config.ignore`。
   指出这两者互为补集，且靠 `quant_linear_prefixes` 前缀判定避免 `QuantLinear` 重复进 ignore。

3. **回答问题二（is_mx 决定 qtype）**：参考 [`generate_quant_config`](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/deploy_export.py#L70-L98) 第 76 行 `qtype = "float" if is_mx else "int"`。解释 `is_mx`（来自 [`__init__`](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_deploy.py#L58-L60) 的 `quant_dtype.startswith("mx")`）如何一路传递：`is_mx → qtype → generate_quant_group 的 observer/strategy/group_size → format 字段 + weight_block_size`。结论：mxfp 选 `float`（per-group、group_size=32、minmax、`float-quantized`），int 选 `int`（per-channel/per-token、memoryless、`int-quantized`）。

4. **观察收尾**：注意三条收尾动作的顺序——`_write_remaining_original_weights`（打包未量化权重）→ `_refresh_weight_index`（重写索引）→ `_refresh_config`（写量化配置）。三者都用 `_write_safetensor_file` 的原子写。

**预期产出**：一张数据流图 + 两个问题的文字解答。若本地有 AMCT 与一个已 ptq 过的小模型，可运行 deploy 后用 `safe_open` 检查某个 `layer_*.safetensors` 里的键名（应见 `.weight` / `.weight_scale`），并查看 `config.json` 的 `quantization_config` 字段验证你的推演。否则标注「待本地验证」。

## 6. 本讲小结

- `deploy` 是 PTQ 链路终点，输入「浮点模型 + PTQ 参数目录」，输出「可部署低比特模型目录」；由 `--granularity` 在 `block`（烘焙 PTQ 结果）与 `tensor`（纯格式转换）间分发。
- **block 模式**逐层重建量化块、装回 `.pt` 参数，经 `iter_deploy_bindings` 只烘焙 `QuantLinear`，payload 按 `.weight → .weight_scale/.weight_bias` 规则命名，逐层写 `layer_XXX.safetensors`，剩余权重重打包为 `rest_*.safetensors`，最后原子重写 `index.json`。
- `get_quant_ignore_linear_names` 用三类前缀判定把 `QuantLinear`（烘焙）/ `PlainLinear`（整层忽略，内部子模块跳过）/ 原始 `nn.Linear`（忽略）分开，烘焙名单与 ignore 名单互为补集。
- **`generate_quant_config`** 生成 compressed-tensors 配置，核心是 `qtype = "float" if is_mx else "int"`：mxfp 走 per-group（32）/minmax/`float-quantized`，int 走 per-channel+per-token/memoryless/`int-quantized`；`bits_scheme` 与 `cache_scheme` 由模型适配器提供。
- **tensor 模式**不构建量化模块，按源分片循环：`convert_state_dict` 用 `element_size()==1` 识别 FP8 并按 `scale_inv` 反量化，`quant_payload` 用裸数据类型 `export_deploy` 直接重新量化为 int/mxfp，保持原始分片结构。
- payload 契约贯穿两模式：int 产 `{qweight, weight_scale, weight_bias?}`，mxfp 产 `{qweight, weight_scale}`；命名映射规则两模式一致。

## 7. 下一步学习建议

- 想搞清 `QuantLinear` 训练态与烘焙态的差异、以及 `WeightQuantizer.export_deploy` 如何区分普通算法与 FlatQuant 这类带 `quantize()` hook 的算法，请阅读 **u7-l1（QuantLinear 与量化器模块）**——它承接本讲的 payload 契约，展开 `export_deploy` 的内部三分支。
- 想了解 int / mxfp 的 `export_deploy` 底层 `weight_quant` / `shared_exponents` 是怎么把浮点压成低比特的，请阅读 **u7-l2（量化数据类型与 export_deploy 落盘）**。
- 想看懂 `_run_blockwise` 调用的 `build_quant_block` / `load_selected_layer_ptq_params` 背后的逐层加载与 PTQ 单元划分，请回顾 **u5-l1（LLM 模型适配基类 BaseModel）** 与 **u4-l2（PTQ 训练后量化主流程）**。
- 若你对 `quantization_config` 里 `kv_cache_scheme`、`MoEGMM` 分组等模型相关字段感兴趣，可对照具体适配器（如 [glm5_2.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/glm/glm5_2/glm5_2.py) 的 `cache_scheme` / `bits_scheme`）阅读，这部分会在 **u5-l2（模型注册与多模型适配）** 系统讲解。
