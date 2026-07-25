# ONNX 导出：自定义算子与 Dynamo 翻译

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚为什么 EdgeLLM 必须定义自己的 ONNX 自定义算子域（`trt::` / `trt_edgellm::`），而不能只用标准 ONNX 算子。
2. 跟着 `torch.onnx.export(dynamo=True)` 的真实调用链，讲明白「PyTorch 自定义算子 stub」是如何被翻译成 ONNX 图节点的，以及 `onnxscript` 在其中扮演的角色。
3. 看懂三个文件的分工：`export.py`（导出编排）、`onnx_custom_schemas.py`（算子 schema 注册）、`dynamo_translations.py`（算子翻译规则），并理解导出产物与下游 C++ 引擎构建器的契约。
4. 解释为什么导出后还要对 ONNX 做一堆「TRT 兼容性后处理」（dtype 修正、可选输入裁剪、scale 去重等）。

本讲承接 [u2-l3](u2-l3-default-decoder-implementation.md)（自定义算子 stub）与 [u2-l4](u2-l4-checkpoint-loading-and-repacking.md)（权重加载），把已经「建好空壳、灌好权重」的 `nn.Module` 真正固化成一张下游可消费的 ONNX 图。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 为什么需要自定义算子域

标准 ONNX 算子集（Gemm、MatMul、Softmax、LayerNorm……）表达的是「单个数学操作」。但 LLM 推理的瓶颈——attention、Mamba 状态机、MoE 专家路由、量化 GEMM——必须靠**融合的 C++ kernel** 才能跑得快。如果用标准算子把它们拆开成几十个小节点，TensorRT 的优化器（Myelin）要么融合不出来、要么融合后仍不如手写 kernel。

因此 EdgeLLM 的做法是：Python 侧用一个**不透明的自定义节点**代表整块融合计算（比如一个 `trt_edgellm::AttentionPlugin` 节点就涵盖 RoPE + KV cache 读写 + QK^T + softmax + 加权求和），C++ 侧再把这个节点映射到 [u8-l1](u8-l1-tensorrt-plugin-architecture.md) 的手写 TRT 插件。这就是 `trt` 与 `trt_edgellm` 两个自定义 ONNX 域存在的根本原因。

### 2.2 dynamo 导出的两段式

`torch.onnx.export(dynamo=True)` 实际是两步：

1. **`torch.export` 追踪**：把 Python 写的 `nn.Module` 追踪成一个 FX 图。这一步**不会执行算子的真实函数体**，而是靠 `register_fake` 做形状/类型传播（见 [u2-l3](u2-l3-default-decoder-implementation.md) 中 `ops.py` 的 stub 设计）。
2. **FX → ONNX lowering**：把 FX 图里的每个节点翻译成 ONNX 节点。对于标准 PyTorch 算子，PyTorch 自带翻译；对于 `torch.ops.trt.*` / `torch.ops.trt_edgellm.*` 这些自定义算子，必须由我们提供翻译规则。

第 2 步用到的就是 **`custom_translation_table`**——一张「FX 自定义算子 → onnxscript 函数」的映射表。

### 2.3 schema 与 translation 的区别（容易混淆）

| 文件 | 作用 | 回答的问题 |
|------|------|-----------|
| `onnx_custom_schemas.py` | 注册 ONNX **算子 schema** | 「`trt_edgellm::AttentionPlugin` 这个算子合法吗？它有几个输入、什么 dtype、有哪些属性？」 |
| `dynamo_translations.py` | 提供算子**翻译规则** | 「FX 图里出现 `torch.ops.trt.attention_plugin` 时，要往 ONNX 图里发射哪些节点？」 |

简单记：**schema 描述「算子的身份证」，translation 描述「算子怎么生成」**。两者缺一不可——没有 schema，ONNX 校验会报「未知算子」；没有 translation，dynamo 导出器不知道该把自定义算子节点翻译成什么。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲涉及的关键内容 |
|------|------|-------------------|
| [tensorrt_edgellm/onnx/export.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export.py) | LLM 导出编排 | `export_onnx` 公共入口、`_export_model` 核心调用、TRT 兼容性后处理 |
| [tensorrt_edgellm/onnx/onnx_custom_schemas.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/onnx_custom_schemas.py) | 算子 schema 注册 | 20 个自定义算子的 `OpSchema`、幂等注册函数 |
| [tensorrt_edgellm/onnx/dynamo_translations.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/dynamo_translations.py) | 翻译规则 | `@script()` 装饰的 onnxscript 函数、`build_custom_translation_table` |
| [tensorrt_edgellm/onnx/export_encoder.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export_encoder.py) | 视觉/音频编码器导出 | 复用同一条 dynamo 路径，仅模型构建与 I/O spec 不同 |

辅助记忆的承接点：`ops.py`（[u2-l3](u2-l3-default-decoder-implementation.md)）里 `@torch.library.custom_op("trt::attention_plugin")` 定义的 stub，就是 translation table 的「键」；`onnx_export_spec()`（`modeling_default.py`）产出的 `OnnxSpec`，就是 `export.py` 消费的「值」。

## 4. 核心概念与源码讲解

### 4.1 export 模块：dynamo 导出主线

#### 4.1.1 概念说明

`export.py` 是 LLM（纯文本/decoder-only）导出的总入口。它要做四件事：

1. 调 `_setup_fp8kv_scales_for_export` 把 FP8 KV scale 预提取成 Python 常量（避免追踪期产生数据依赖）。
2. 从模型拿 `onnx_export_spec()`，得到 wrapper 模块、dummy 输入、I/O 名、动态形状。
3. 调 `torch.onnx.export(dynamo=True, ...)` 真正导出。
4. 跑一堆 TRT 兼容性后处理 pass，修正 dynamo 导出器与 TRT 之间的格式差异。

关键设计是：**一张 ONNX 图同时覆盖 prefill（`past_len=0`）和 decode（`past_len>0`）两个阶段**，KV cache 与 Mamba 状态都以图输入/输出的形式暴露，运行时循环复用。这一点写在文件开头的 docstring 里。

#### 4.1.2 核心流程

```
export_onnx(model, output_path, ...)
        │
        ├─ _setup_fp8kv_scales_for_export(model)   # 预提取 FP8 scale 为常量
        ├─ _export_model(model, output_path)        # 见下
        │       ├─ spec = model.onnx_export_spec()              # 拿 OnnxSpec
        │       ├─ translation_table = build_custom_translation_table()  # 来自 4.3
        │       ├─ with _permissive_inline_opset():             # 兼容 opset 冲突
        │       │      torch.onnx.export(spec.wrapped, spec.args,
        │       │          dynamo=True, custom_translation_table=...,
        │       │          opset_version=24, ...)
        │       └─ 后处理 pass（按量化类型条件触发）
        │             · _fix_nvfp4_weight_dtype          # INT8→FLOAT4E2M1
        │             · _strip_onnxscript_internal_attrs  # 去掉 _outputs 属性
        │             · _fix_initializer_dtypes           # FP32→FP16 等
        │             · _strip_attention_plugin_optional_inputs
        │             · externalize_model_weights         # 大权重外置
        ├─ write_runtime_artifacts(...)            # 写 config.json/embedding/分词器
        └─ patch_external_weight_manifest(...)     # 修正外置权重清单
```

#### 4.1.3 源码精读

公共入口 `export_onnx` 的签名揭示了导出能接受的所有「开关」：FP8 embedding、词表裁剪目录、外置权重种类、以及 TP 场景下按 rank 命名的 config 文件名。

> [tensorrt_edgellm/onnx/export.py:71-127](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export.py#L71-L127) —— `export_onnx` 公共 API：先解析外置权重请求、拒绝量化 lm_head 的外置化，再委托 `_export_model` 出图，最后写运行时产物与修正清单。

真正的导出调用集中在 `_export_model` 里，是全篇最关键的一段：

> [tensorrt_edgellm/onnx/export.py:719-739](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export.py#L719-L739) —— 拿到 `spec`、构建 `translation_table`，在 `_permissive_inline_opset()` 上下文里调用 `torch.onnx.export(dynamo=True, ...)`。注意三个参数：`custom_translation_table`（自定义算子怎么翻译）、`opset_version=24`（固定 opset）、`external_data=True`（大权重外置到 `.data` 文件）。

`_OPSET_VERSION` 是模块级常量，所有 LLM/编码器导出都钉死在 24：

> [tensorrt_edgellm/onnx/export.py:396](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export.py#L396) —— `_OPSET_VERSION = 24`。

`_permissive_inline_opset` 是一个很「现实」的补丁：torch 内置函数按 opset 18 编译，而我们的 FP8 `QuantizeLinear` 需要 opset 21（带 `output_dtype`），inliner 一旦同时遇到两者就会抛 `Opset mismatch: 18 != 21`。这个上下文管理器把 inline 时的版本冲突策略改成「取 max」，因为标准 ONNX 域向后兼容，opset 21 是 18 的超集：

> [tensorrt_edgellm/onnx/export.py:399-436](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export.py#L399-L436) —— 临时 monkey-patch `InlinePass._instantiate_call`，在调用原逻辑前把 `function.opset_imports` 与全局 imports 合并取最大版本。

`_setup_fp8kv_scales_for_export` 体现了一个重要的追踪约束：

> [tensorrt_edgellm/onnx/export.py:439-465](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export.py#L439-L465) —— 在追踪**之前**把每个 attention 模块的 `q/k/v_scale` buffer `.item()` 成 Python float 存进 `module._qkv_scales_float`。因为在追踪期对 tensor 调 `.item()` 会产生 `torch.export` 无法 guard 的数据依赖表达式；提前取成常量，它们就成了编译期常量，从而能作为 ONNX 属性安全写入。

**TRT 兼容性后处理**是 dynamo 导出区别于「教科书导出」的地方。dynamo 导出器是为通用 ONNX runtime 设计的，而 TRT 的解析器有一系列更严格的假设。下面四个 pass 各修一类问题：

> [tensorrt_edgellm/onnx/export.py:134-224](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export.py#L134-L224) —— `_fix_nvfp4_weight_dtype`：模型把 FP4 权重按 `[out, in//2]` 存成 INT8（每字节两个 nibble），但 TRT 的 `DequantizeLinear(block_size=...)` 要求 `FLOAT4E2M1`（elem_type=23）且 logical shape 为 `[out, in]`。这个 pass **不改字节、只改元素类型声明与最后一维长度（×2）**——「同样的字节，不同的 ONNX 类型与形状」。

> [tensorrt_edgellm/onnx/export.py:227-283](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export.py#L227-L283) —— `_strip_attention_plugin_optional_inputs`：`torch.export` 给每个 `AttentionPlugin` 节点都挂上两个可选输入（`attention_mask`、`attention_pos_id`），未用时是空字符串。但 TRT 的 C++ 插件 vanilla 模式严格要求恰好 7 个输入（`kNUM_REQUIRED_INPUTS=7`），多出来的空项会触发 `nullptr` 断言。这个 pass 按节点属性（tree/vision_block/vanilla）裁剪到 9/8/7 个输入。

> [tensorrt_edgellm/onnx/export.py:317-389](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export.py#L317-L389) —— `_dedup_shared_dql_scales`：dynamo 导出器会把相同的标量 initializer（如 NVFP4 的 per-tensor global scale）去重成单个 initializer，被跨层的许多 `DequantizeLinear` 节点共享；而 TRT 的 Myelin 编译器在这种扇出场景会段错误。该 pass 把 ≤1KB 的共享标量复制成多份，每个 DQL 各拿一份（大张量不动以免翻倍显存）。

> [tensorrt_edgellm/onnx/export.py:468-710](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export.py#L468-L710) —— `_fix_initializer_dtypes`：单趟完成多项修正——共享 DQL scale 去重、FP32 权重降为 FP16（修 BF16 检查点里 tied lm_head 的 dtype 问题）、插件必需的 FP32 输入保留/还原、element-wise 算子 FP32 输入对齐。

最后，主流程在 `_export_model` 末尾按量化类型条件触发这些 pass，并做权重外置：

> [tensorrt_edgellm/onnx/export.py:742-767](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export.py#L742-L767) —— 根据 `uses_nvfp4_weights` / `uses_mxfp8_weights` 选择性跑 NVFP4 dtype 修正、去 onnxscript 内部属性、initializer dtype 修正、attention 可选输入裁剪、权重外置。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：建立「导出主流程」的全景，能对着源码说出每一步的产出。

**操作步骤**：

1. 打开 `tensorrt_edgellm/onnx/export.py`，定位 `export_onnx`（L71）与 `_export_model`（L713）。
2. 用纸笔画出 4.1.2 的流程图，在每个节点旁标注对应的函数名与行号区间。
3. 回答：如果导出的是一个 NVFP4 量化模型，4.1.2 流程图里的「后处理 pass」哪几个会被实际触发？（提示：看 L742-748 的 `if nvfp4:` / `if mxfp8:` 条件。）

**预期结果**：你应该能指出 NVFP4 会触发 `_fix_nvfp4_weight_dtype`、`_fix_initializer_dtypes(dedup_dql_scales=True)`、`_strip_attention_plugin_optional_inputs`，而 `_strip_onnxscript_internal_attrs` 只在 mxfp8 时触发。这印证了「后处理 pass 是按量化格式条件选配的」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_permissive_inline_opset` 取「max」是安全的，而不是「min」？
> **答案**：标准 ONNX 域严格向后兼容，opset 21 是 opset 18 的超集。取 max（21）既能用 21 的新算子（带 `output_dtype` 的 `QuantizeLinear`），又不会破坏 18 的功能；取 min 会丢掉 21 才有的能力，导致 FP8 算子无法表达。

**练习 2**：`_setup_fp8kv_scales_for_export` 为什么必须在追踪**之前**调用，而不是在某个 attention 模块的 forward 里现取？
> **答案**：追踪期对 tensor buffer 调 `.item()` 会产生数据依赖的符号表达式，`torch.export` 无法对它生成 guard；提前取成 Python float 并存为模块属性，值就成了编译期常量，可安全作为 ONNX 节点属性写入。

---

### 4.2 custom schemas 模块：自定义算子域的注册

#### 4.2.1 概念说明

ONNX 要校验一张图的合法性，必须认识图里出现的**每一个算子**。标准算子（Gemm、Softmax）ONNX 自带定义；自定义算子（`trt_edgellm::AttentionPlugin`）则必须由调用方先「注册 schema」——告诉 ONNX：这个算子叫什么、在哪个域、有几个输入输出、各自的 dtype 约束、有哪些属性。

`onnx_custom_schemas.py` 就是一份「算子身份证登记册」，集中定义了 EdgeLLM 全部 20 个自定义算子的 schema，并提供一个幂等的注册函数。它的关键约束写在文件头注释里：必须在 `torch.onnx.export(..., optimize=True)` **之前**调用，否则优化阶段遇到自定义节点会因「未知算子」而失败。

#### 4.2.2 核心流程

```
register_tensorrt_edgellm_onnx_custom_schemas()   # 幂等
        │
        ├─ 检查全局 flag _registered_tensorrt_edgellm_schemas（已注册则直接返回）
        └─ for s in _ALL_CUSTOM_SCHEMAS:           # 20 个 OpSchema
                _safe_register_schema(s)            # 已存在则忽略异常
```

每个 `OpSchema` 描述四件事：

- `name` + `domain`：算子全名（如 `AttentionPlugin` @ `trt_edgellm`）。
- `since_version`：schema 修订号，全文件统一钉在 23（见 `_SCHEMA_SINCE_VERSION`）。
- `inputs` / `outputs`：每个形参的名字、类型约束（`type_str`）、是否可选/变长。
- `attributes`：算子属性的名字、类型（INT/FLOAT/FLOATS）、是否必需。
- `type_constraints`：把 `type_str`（如 `"T"`）绑定到具体 dtype 列表。

#### 4.2.3 源码精读

注册函数与幂等保护：

> [tensorrt_edgellm/onnx/onnx_custom_schemas.py:1499-1506](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/onnx_custom_schemas.py#L1499-L1506) —— `register_tensorrt_edgellm_onnx_custom_schemas`：用模块级布尔 flag 保证「跨包只注册一次」，遍历 `_ALL_CUSTOM_SCHEMAS` 逐个注册。

> [tensorrt_edgellm/onnx/onnx_custom_schemas.py:35-40](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/onnx_custom_schemas.py#L35-L40) —— `_safe_register_schema`：try/except 吞掉「schema 已存在」异常，使注册可重复调用而不报错。

`_SCHEMA_SINCE_VERSION` 是全局常量，所有 schema 共用：

> [tensorrt_edgellm/onnx/onnx_custom_schemas.py:32](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/onnx_custom_schemas.py#L32) —— `_SCHEMA_SINCE_VERSION = 23`。注释提醒：若运行时期望更新的 schema 修订，需要 bump 此值。

拿最有代表性的 `AttentionPlugin` schema 看一个完整定义（域是 `trt_edgellm`）：

> [tensorrt_edgellm/onnx/onnx_custom_schemas.py:47-189](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/onnx_custom_schemas.py#L47-L189) —— `AttentionPlugin` schema：9 个输入（q/k/v/past_key_value/context_lengths/rope_rotary_cos_sin/kvcache_start_index + 两个可选的 attention_mask/attention_pos_id），2 个输出（attn_output + present_key_value）；类型约束 `T=fp16`、`T_KV∈{fp16, fp8e4m3}`（后者正是 FP8 KV cache 的来源）；属性含 `num_q_heads`/`num_kv_heads`/`head_size`（必需）与 `enable_tree_attention`/`enable_fp8_kv_cache`/`qkv_scales`/`attention_scale` 等（可选）。一个 schema 涵盖 vanilla / FP8-KV / tree / tree+FP8-KV 全部组合。

再看一个量化算子（域是 `trt`），用于 NVFP4 激活量化：

> [tensorrt_edgellm/onnx/onnx_custom_schemas.py:264-322](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/onnx_custom_schemas.py#L264-L322) —— `TRT_FP4DynamicQuantize` schema：2 个输入（fp16 激活 + fp16 global scale），2 个输出（打包的 FP4 张量 + per-block scale），属性含 `axis`/`block_size`（典型 16）/`scale_type`（17=FLOAT8E4M3FN）。

以及一个 INT4 GEMM 算子（域 `trt_edgellm`），后续实践会用到：

> [tensorrt_edgellm/onnx/onnx_custom_schemas.py:517-573](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/onnx_custom_schemas.py#L517-L573) —— `Int4GroupwiseGemmPlugin` schema：3 个输入（input/qweight=int8/scales=fp16），1 个输出，属性 `gemm_n`/`gemm_k`/`group_size` 全部必需。

最后是登记册本体——20 个 schema 的元组：

> [tensorrt_edgellm/onnx/onnx_custom_schemas.py:1473-1494](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/onnx_custom_schemas.py#L1473-L1494) —— `_ALL_CUSTOM_SCHEMAS`：从 `AttentionPlugin` 到 `Gemma4AudioAttentionPlugin` 共 20 个 OpSchema 的有序元组，是注册的唯一数据源。

> **关于 FP8 的设计细节**：文件头注释特别说明「FP8 用的是标准 `QuantizeLinear`/`DequantizeLinear`，而不是自定义 FP8 schema」。这与 4.3 节看到 FP8 translation 用标准 opset21 算子是一致的——能走标准就走标准，自定义只用于「标准无法表达」的融合算子。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：读懂 schema 的四个组成部分，并能判断一个算子的「输入个数/dtype 约束」。

**操作步骤**：

1. 打开 `onnx_custom_schemas.py`，找到 `AttentionPlugin` schema（L47）。
2. 数一数：它声明了几个必需属性？几个可选属性？（看每个 `OpSchema.Attribute` 的 `required=` 字段。）
3. 看 `type_constraints`：`T_KV` 允许哪两种 dtype？这对应了哪两个运行时特性？

**预期结果**：必需属性有 `num_q_heads`、`num_kv_heads`、`head_size`、`enable_tree_attention` 四个；可选属性包括 `enable_fp8_kv_cache`、`enable_vision_block_attention`、`sliding_window_size`、`qkv_scales`、`attention_scale`。`T_KV` 允许 `fp16` 与 `fp8e4m3fn`，前者是普通 KV cache，后者是 FP8 KV cache（省显存）。

#### 4.2.5 小练习与答案

**练习 1**：为什么注册函数要用「全局 flag + try/except」双重幂等保护？
> **答案**：导出可能在一次进程里被多次调用（比如 VLM 同时导出 LLM 子图、视觉子图、各 draft 子图），而 schema 是进程级全局状态。flag 保证不重复遍历注册，try/except 防御「别的包已经注册过同名 schema」的情况——两种机制分别挡住「自己重复调」与「别人先注册」两类场景。

**练习 2**：`since_version=23` 与 `export.py` 里的 `_OPSET_VERSION=24` 是什么关系？
> **答案**：`since_version` 是**单个自定义算子 schema 的引入版本**（在它所属的 `trt`/`trt_edgellm` 域内），`_OPSET_VERSION` 是**整张 ONNX 图标准域的 opset**。它们是不同维度：自定义算子的 since 是 23，图本身用标准域 opset 24。校验时 ONNX 按各自域的版本独立检查。

---

### 4.3 dynamo translations 模块：PyTorch 自定义算子 → ONNX 节点

#### 4.3.1 概念说明

`dynamo_translations.py` 是「翻译官」。dynamo 导出器在 FX 图里看到 `torch.ops.trt.attention_plugin.default(...)` 这样的节点时，不知道该往 ONNX 里发射什么——它不是标准 PyTorch 算子。翻译表 `build_custom_translation_table()` 告诉导出器：「看到这个节点，就运行这个 onnxscript 函数，由它发射对应的 ONNX 节点」。

这里的 `@script()` 装饰器（来自 `onnxscript`）是关键：它把一个**普通 Python 函数**编译成一个**可被 ONNX 导出器内联的图函数**。函数体里用的 `_trt_edgellm.AttentionPlugin(...)`、`_trt.TRT_FP4DynamicQuantize(...)`、`_op21.DequantizeLinear(...)` 都是 onnxscript 提供的「ONNX 算子句柄」，调用它们等价于「往 ONNX 图里发射一个对应节点」。

两个自定义域在文件顶部实例化为 Opset 句柄：

> [tensorrt_edgellm/onnx/dynamo_translations.py:42-43](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/dynamo_translations.py#L42-L43) —— `_trt = Opset("trt", 1)` 与 `_trt_edgellm = Opset("trt_edgellm", 1)`，分别对应 schema 文件里 `domain="trt"` / `domain="trt_edgellm"` 的算子。注意这里的 `1` 是 Opset 句柄的版本号（onnxscript 用），与 schema 的 `since_version=23` 不是一回事。

#### 4.3.2 核心流程

一条自定义算子从 FX 图到 ONNX 节点的完整路径：

```
nn.Module 前向里调用了 torch.ops.trt.attention_plugin.default(q,k,v,...)
            │  (torch.export 追踪)
            ▼
FX 图节点: call_function(torch.ops.trt.attention_plugin.default)
            │  (torch.onnx.export dynamo lowering)
            │  查 custom_translation_table[torch.ops.trt.attention_plugin.default]
            ▼
对应 @script() 函数 _attention_plugin_translation 被内联
            │  函数体内调用 _trt_edgellm.AttentionPlugin(...)
            ▼
ONNX 图里出现一个 domain=trt_edgellm, op_type=AttentionPlugin 的节点
（该节点的合法性由 4.2 注册的 schema 保证）
```

`build_custom_translation_table()` 做三件事，返回最终的字典：

1. 先调 `register_tensorrt_edgellm_onnx_custom_schemas()` 注册 schema（保证节点合法）。
2. import `..models.ops`（副作用：注册全部 `torch.library.custom_op` stub，使 `torch.ops.trt.*` 可访问）。
3. 返回 `{torch.ops.<域>.<算子>.default: <translation 函数>}` 字典。

#### 4.3.3 源码精读

翻译表的构造入口：

> [tensorrt_edgellm/onnx/dynamo_translations.py:1048-1119](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/dynamo_translations.py#L1048-L1119) —— `build_custom_translation_table`：先注册 schema、再 import ops 触发 stub 注册、最后返回 26 条「FX 算子 → onnxscript 函数」映射。注意键是 `torch.ops.trt.attention_plugin.default` 这种 `.default` overload 句柄。

拿 attention 看一个「1 对 1」的翻译——FX 的一个 `attention_plugin` 节点，直接对应 ONNX 里一个 `AttentionPlugin` 节点：

> [tensorrt_edgellm/onnx/dynamo_translations.py:50-100](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/dynamo_translations.py#L50-L100) —— `_attention_plugin_translation`：`@script()` 装饰，函数体内一行 `_trt_edgellm.AttentionPlugin(...)` 把全部输入按位传入、属性按名传入、`_outputs=2` 声明双输出。统一覆盖 vanilla/FP8-KV/tree/tree+FP8-KV，因为签名对齐了完整 schema，`torch.export` 把 kwargs 归一化为位置参数后仍能正确对齐。

再看一个「1 对多」的翻译——FX 的一个 NVFP4 激活量化节点，在 ONNX 里展开成「动态量化 + 两次反量化」的子图：

> [tensorrt_edgellm/onnx/dynamo_translations.py:135-163](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/dynamo_translations.py#L135-L163) —— `_nvfp4_act_qdq_translation`：先 `_trt.TRT_FP4DynamicQuantize(...)` 产出 `x_f4` 与 per-block scale `sx_f8`，再 `_trt.DequantizeLinear(sx_f8, global_scale_f16)` 算出组合 scale，最后 `_trt.DequantizeLinear(x_f4, dq_scale, block_size=16)` 反量化回 FP16。注释说明这与 ModelOpt `export_fp4(dynamic)` 产出的图一致。

NVFP4 反量化的数学含义可写作（per-block 反量化）：

\[
y_{i} = \text{FP4\_to\_FP16}(x_{i}) \;\times\; \underbrace{\big(\text{FP8\_to\_FP32}(sf_{b(i)}) \times global\_scale\big)}_{\text{组合 scale}}
\]

其中 \(b(i)\) 是元素 \(i\) 所属的 block（block_size=16），组合 scale 来自两次 DequantizeLinear。对应的权重反量化翻译在：

> [tensorrt_edgellm/onnx/dynamo_translations.py:166-186](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/dynamo_translations.py#L166-L186) —— `_nvfp4_dequantize_translation`：两次**标准** ONNX `DequantizeLinear`——先把 FP8 per-block scale 反量化成 FP32 scale，再把 FLOAT4E2M1 权重按 block 反量化，最后 Cast 到 FP16 以匹配激活 dtype 进 MatMul。注意注释「weight initializer 在导出后由 `_fix_nvfp4_weight_dtype` 从 INT8 改写为 FLOAT4E2M1」——这正是 4.1.3 的后处理 pass 与此处翻译的呼应。

INT4 GEMM 是「1 对 1」的最简示例，适合做实践对象：

> [tensorrt_edgellm/onnx/dynamo_translations.py:252-268](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/dynamo_translations.py#L252-L268) —— `_int4_groupwise_gemm_translation`：一行 `_trt_edgellm.Int4GroupwiseGemmPlugin(...)` 把 input/qweight/scales 与 `gemm_n`/`gemm_k`/`group_size` 三个属性传出，FX 的一个节点对应 ONNX 里一个 `Int4GroupwiseGemmPlugin` 节点。

最后看一种「带分发的」翻译——`causal_conv1d` 根据是否需要中间状态、是否走 DDTree 路径，分发到三个不同的 `@script()` 函数：

> [tensorrt_edgellm/onnx/dynamo_translations.py:409-451](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/dynamo_translations.py#L409-L451) —— `_causal_conv1d_intermediate_dispatch`：普通 Python 函数（非 `@script`），根据 `use_ddtree_state` 与 `tree_parent_ids/tree_depths` 是否就绪，在「带中间状态的 MTP 版」「DDTree 版」之间选择，并做参数校验。translation 表里挂的是这种 dispatch 函数，运行时按入参形态决定发射哪种 conv 节点。

> **一个关键区分**：表里的值有两种——一类是 `@script()` 编译过的图函数（如 `_attention_plugin_translation`，直接内联成 ONNX 子图）；另一类是普通 Python dispatch 函数（如 `_causal_conv1d_intermediate_dispatch`），它在运行时挑选要调用的 `@script()` 函数。dynamo 导出器对两者都支持。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：完整跑通「schema 定义 → translation 规则 → 最终 ONNX 节点形态」的三角对应，直观理解一个自定义算子在图里长什么样。

**操作步骤**：

1. 在 `onnx_custom_schemas.py` 找到 `Int4GroupwiseGemmPlugin` 的 schema 定义（L517-573），记下它的 domain（`trt_edgellm`）、输入（input/qweight/scales）、属性（`gemm_n`/`gemm_k`/`group_size`）。
2. 在 `dynamo_translations.py` 找到它的翻译规则 `_int4_groupwise_gemm_translation`（L252-268），确认 translation 表把它挂在 `torch.ops.trt.int4_groupwise_gemm.default` 下（见 `build_custom_translation_table`，L1078-1079）。
3. 在 `ops.py`（[u2-l3](u2-l3-default-decoder-implementation.md)）找到 `@torch.library.custom_op("trt::int4_groupwise_gemm")` stub，确认它的 `register_fake` 提供了形状传播。
4. 用一句话回答：这个算子最终在 ONNX 图里以什么节点形式出现？

**预期结果**：它在 ONNX 图里就是一个**单节点**——`domain="trt_edgellm"`、`op_type="Int4GroupwiseGemmPlugin"`、3 个输入（input/qweight/scales）、3 个属性（gemm_n/gemm_k/group_size）、1 个输出。这是「1 对 1」翻译的典型：FX 的一个 `int4_groupwise_gemm` 节点，经 translation 内联后，原样变成 ONNX 的一个自定义节点，合法性由 schema 保证，C++ 侧再由 `Int4GroupwiseGemmPlugin` TRT 插件执行。

> 对比着看 NVFP4 激活量化（步骤同上，换成 `_nvfp4_act_qdq_translation`）：它是「1 对多」——FX 的一个节点会在 ONNX 里展开成 `TRT_FP4DynamicQuantize` + 两次 `DequantizeLinear` 共三个节点组成的小子图。这两种形态（1 对 1 / 1 对多）覆盖了 translation 表里几乎全部算子。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `build_custom_translation_table` 里要先 import `..models.ops`（标了 `noqa: F401`）？
> **答案**：`import ops` 的副作用是触发模块顶部所有 `@torch.library.custom_op("trt::...")` 的注册，使 `torch.ops.trt.attention_plugin`、`torch.ops.trt_edgellm.causal_conv1d` 这些句柄真正可访问。表里的键就是这些 `.default` overload 句柄，若不先 import，访问 `torch.ops.trt.*` 会报「未注册算子」。

**练习 2**：`_attention_plugin_translation` 用「一个函数覆盖四种模式」，这与「写四个 translation 函数」相比有什么好处？
> **答案**：因为 `torch.export` 会把 forward 里的 kwargs 归一化成位置参数，统一签名的函数总能与 FX 图的位置参数对齐，避免「按模式分四个函数后属性顺序错位」的隐患；同时 schema 也只有一个 `AttentionPlugin`，单一翻译函数与单一 schema 一一对应，维护成本最低。不同模式靠属性值（`enable_tree_attention` 等）在 C++ 插件内部分支，而非在导出侧分支。

---

### 4.4 编码器导出：复用同一条 dynamo 路径（扩展模块）

`export_encoder.py` 虽然不在三个必讲最小模块里，但它是理解「导出框架通用性」的关键——视觉/音频编码器与 LLM 共用同一条 dynamo 导出路径，差异只在「怎么建模型」与「怎么给 I/O spec」。

#### 4.4.1 概念说明

文件头 docstring 说明它取代了旧的 `export_visual.py` / `export_audio.py`，统一成一个模块，因为两类编码器走完全相同的 dynamo 导出，区别仅是模型构建方式与 I/O spec 来源。

#### 4.4.2 核心流程

```
export_visual_onnx / export_audio_onnx
        │
        ├─ 按 model_type 在 _VISUAL_REGISTRY / _AUDIO_MODEL_TYPES 查表
        ├─ 动态 import 对应 family 的 build_fn，from-scratch 建模型
        ├─ model.get_onnx_export_args(...) 拿 I/O spec
        └─ _run_dynamo_export(...)   # ← 与 LLM 同款 torch.onnx.export(dynamo=True)
                ├─ build_custom_translation_table()   # 复用 4.3 的翻译表
                ├─ with _permissive_inline_opset(): torch.onnx.export(...)
                └─ （仅视觉）量化权重的 TRT 后处理，复用 export.py 的 pass
```

#### 4.4.3 源码精读

视觉编码器按 `model_type` → family → module → build_fn 三级查表：

> [tensorrt_edgellm/onnx/export_encoder.py:81-100](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export_encoder.py#L81-L100) —— `_VISUAL_REGISTRY`：把 `qwen3_vl`/`internvl_chat`/`phi4mm`/`gemma4` 等 `model_type` 映射到内部 family 名。

核心导出助手复用了 4.1 / 4.3 的全部机制：

> [tensorrt_edgellm/onnx/export_encoder.py:209-251](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export_encoder.py#L209-L251) —— `_run_dynamo_export`：与 LLM 的 `_export_model` 几乎同构——同样 `build_custom_translation_table()`、同样 `_permissive_inline_opset()`、同样 `torch.onnx.export(dynamo=True, opset_version=_OPSET_VERSION)`。这说明「自定义算子域 + dynamo 翻译」是一个**跨模型类型通用**的导出底座。

视觉编码器在导出后也会按量化类型跑 TRT 后处理（直接 import `export.py` 的私有 pass）：

> [tensorrt_edgellm/onnx/export_encoder.py:316-329](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/onnx/export_encoder.py#L316-L329) —— 视觉图 NVFP4/MXFP8 时复用 `_fix_nvfp4_weight_dtype` / `_strip_onnxscript_internal_attrs` / `_fix_initializer_dtypes`，但注意 `cast_fp32_weights_to_fp16=False`——因为视觉图里 RMSNorm 的 `.float()` 产生的 FP32 常量是合法的，不能像 LLM 那样一律降成 FP16。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：验证「编码器与 LLM 共用同一条导出底座」这一论断。

**操作步骤**：

1. 对比 `export.py` 的 `_export_model`（L713）与 `export_encoder.py` 的 `_run_dynamo_export`（L209），列出两者**相同**的步骤（提示：translation table、`_permissive_inline_opset`、`torch.onnx.export` 的关键参数）。
2. 找出两者**不同**的步骤（提示：谁调 `onnx_export_spec()`？谁调 `get_onnx_export_args()`？后处理 pass 的条件是否一样？）。

**预期结果**：相同点是都用 `build_custom_translation_table()`、都在 `_permissive_inline_opset()` 下用 `dynamo=True` 导出、都固定 opset 24、都做权重外置。不同点是 LLM 从 `model.onnx_export_spec()` 拿 spec（spec 里自带 wrapper），编码器从 `model.get_onnx_export_args(...)` 拿四元组；后处理上编码器显式关掉了 FP32→FP16 降级。

#### 4.4.5 小练习与答案

**练习**：为什么 `export_encoder.py` 要从 `export.py` import 一堆「私有」（下划线开头）函数，而不是把它们提到一个公共模块？
> **答案**：这些 pass（`_fix_nvfp4_weight_dtype` 等）原本就是为 LLM 导出写的，编码器复用是后来的需求。直接 import 私有函数是最小改动路径，代价是耦合；若后续有第三类模型复用，重构提公共模块才划算。这是工程上「先复用、后重构」的典型权衡。

## 5. 综合实践

把本讲三个模块串起来，做一次「全链路源码追踪」。

**任务**：选定 NVFP4 量化模型里的一条算子链——激活经过 NVFP4 量化后再进 INT4 MoE 专家 GEMM——追踪它在导出各阶段的形态变化。

**步骤**：

1. **起点**：在 `ops.py`（[u2-l3](u2-l3-default-decoder-implementation.md)）找到 `nvfp4_act_qdq` 与 `int4_moe_plugin` 的 `@torch.library.custom_op` stub 与 `register_fake`。确认它们是「追踪期占位、不执行真实计算」。
2. **schema 侧**：在 `onnx_custom_schemas.py` 找到 `TRT_FP4DynamicQuantize`（L264）、`Nvfp4MoePlugin`（L1139）的 schema，记下各自 domain、输入个数、关键属性。
3. **translation 侧**：在 `dynamo_translations.py` 找到 `_nvfp4_act_qdq_translation`（L135）与 `_nvfp4_moe_plugin_translation`（L836），说明前者是「1 对多」（展开成量化+两次反量子图），后者是「1 对 1」（单节点）。
4. **编排侧**：在 `export.py` 的 `_export_model`（L742-762）确认 NVFP4 会触发哪些后处理 pass，并解释 `_fix_nvfp4_weight_dtype` 为什么必须与 `_nvfp4_dequantize_translation` 配合（提示：translation 假设权重是 FLOAT4E2M1，而 stub 存的是 INT8，靠后处理改写元素类型）。
5. **契约侧**：用一句话总结这张 ONNX 图交给 [u4-l1](u4-l1-builder-architecture-eight-stages.md) 的 C++ 构建器时，构建器需要认识哪些自定义域与算子。

**预期产出**：一张表格，列出「算子名 / 所属域 / schema 输入输出 / translation 形态（1对1 还是 1对多）/ 对应的 C++ 插件或 TRT 原生算子」。这张表就是导出产物与引擎构建器之间的「契约摘要」。

> 如果本机有 GPU 且已装好 `tensorrt_edgellm`，可以额外跑一遍 `tensorrt-edgellm-export`（见 [u1-l5](u1-l5-end-to-end-pipeline-walkthrough.md)），用 `onnx` 库加载产物 `model.onnx`，`print` 出所有 `node.op_type` 与 `node.domain`，验证你追踪的算子确实以自定义节点出现。若无 GPU，此步标注「待本地验证」即可。

## 6. 本讲小结

- EdgeLLM 用两个自定义 ONNX 域（`trt`、`trt_edgellm`）表达 attention / Mamba / MoE / 量化 GEMM 等融合算子，因为标准 ONNX 算子无法高效表达这些性能瓶颈，必须 lowering 到 C++ 插件/算子。
- `onnx_custom_schemas.py` 登记 20 个算子的「身份证」（schema），幂等注册，保证 ONNX 校验能认识这些节点；FP8 故意走标准 `QuantizeLinear/DequantizeLinear` 而非自定义 schema。
- `dynamo_translations.py` 提供「FX 自定义算子 → onnxscript 函数」的翻译表；翻译有「1 对 1」（如 `Int4GroupwiseGemmPlugin`）和「1 对多」（如 NVFP4 激活量化展开成量化+两次反量子图）两种形态。
- `export.py` 用 `torch.onnx.export(dynamo=True)` 把一张同时覆盖 prefill 与 decode 的图导出，固定 opset 24，KV cache/Mamba 状态以图 I/O 暴露。
- dynamo 导出器面向通用 runtime，TRT 解析器更严格，因此导出后必须跑一串 TRT 兼容性后处理（NVFP4 dtype 改写、attention 可选输入裁剪、共享 DQL scale 去重、initializer dtype 修正等）。
- 视觉/音频编码器（`export_encoder.py`）复用同一条 dynamo 导出底座与同一张翻译表，差异仅在模型构建与 I/O spec 来源——印证了「自定义算子域 + dynamo 翻译」是跨模型类型的通用框架。

## 7. 下一步学习建议

本讲产出的是「合法的、TRT 友好的 ONNX 图」。接下来：

- 进入 [u4-l1 构建器架构与八阶段流程](u4-l1-builder-architecture-eight-stages.md)，看 C++ 构建器如何解析这张 ONNX、把自定义节点映射到 TRT 插件、编译出 `.engine`。本讲的「契约摘要」会直接派上用场。
- 进入 [u2-l6 导出 CLI 与多模态组件编排](u2-l6-export-cli-multimodal-orchestration.md)，看 `scripts/export.py` 如何按 `model_type` 编排本讲的 `export_onnx` 与 `export_encoder.py` 的各编码器导出，产出多张子图与 sidecar。
- 若想深入自定义节点的 C++ 落地，跳读 [u8-l1 TensorRT 插件架构](u8-l1-tensorrt-plugin-architecture.md)，对照本讲 schema 里声明的输入/属性，看 C++ 插件是如何一一接收的。
