# compile 接口与编译主流程

## 1. 本讲目标

本讲打开 `mlc_llm compile` 命令背后的接口层黑盒。在前面几讲里，你已经知道 `compile` 会「把模型编译成一个 `.so` / `.tar` / `.wasm` 模型库」，但这个库是怎么从一份 `mlc-chat-config.json` 长出来的，我们一直没有展开。学完本讲你应当能够：

- 讲清 `compile()` 主流程的五个阶段：解析配置 → 建模并量化 → 导出 Relax IRModule → 预处理参数（shard / pipeline）→ 运行 pass 流水线并 `build_func` 导出产物。
- 理解 `CompileArgs` 这只「编译信封」打包了哪些字段，以及 `OptimizationFlags` / `ModelConfigOverride` 两个伴生结构如何被解析、并在 `__post_init__` 里按 target 自我矫正。
- 掌握**参数预处理 preprocs** 的核心机制：编译期只把「分片配方」（shard recipe）写进参数元数据并生成一份分片 TIR PrimFunc，真正的切分留到运行期由 C++ `multi_gpu_loader` 执行；尤其能解释 `_apply_preproc_to_params_and_check_pipeline` 如何为 `tensor_parallel_shards > 1` 的参数生成这条配方与 TIR。
- 认识 `build_func` 的角色：它由 `--device`（target）决定，是「IRModule → 磁盘产物」的最后一跳，不同平台对应不同的导出实现（`.so` / `.tar` / `.wasm` / `.dylib`）。

本讲只讲**接口层与编排顺序**，不深入每一个 compiler pass 的算法（融合、派发、低层优化留到 U8），也不展开建图细节（U3 已讲过 Relax nn 模型）。

## 2. 前置知识

进入本讲前，请先回忆这几讲建立的心智模型：

- **u3-l2 的 Relax nn 模型**：模型用 `tvm.relax.frontend.nn` 写成，是一张「计算图 IR」。顶层 `LlamaForCausalLM` 暴露 `embed` / `get_logits` / `prefill` / `decode` / `batch_verify` / `create_paged_kv_cache` 等阶段方法，并用 `get_default_spec()` 声明每个方法的张量接口（形状、dtype、param_mode）。本讲 `compile()` 第一步就要消费这个 spec 把模型导出成 TVM 的 `IRModule`。
- **u5-l1 的量化接口**：`Model.quantize` 是一个以 `kind` 为键的字典，`Model.quantize[kind](model_config, quantization)` 返回 `(量化后的 nn.Module, QuantizeMapping)`。本讲的建图步骤就是调用它。
- **u3-l3 的配置探测**：`ModelConfigOverride`（本讲会再次出现）和 `from_dict` / `__post_init__` 派生字段（如 `context_window_size`、`prefill_chunk_size`、`tensor_parallel_shards`）的机制。
- **u4-l3 的 preshard**：`convert_weight` 里由环境变量 `MLC_INTERNAL_PRESHARD_NUM` 触发的「真真切权重落盘」机制，用的也是 `ShardSingleDim`。本讲的 preprocs 与它**共享** `ShardSingleDim`，但只记配方不真切——两者对比是理解本讲的关键。

几个本讲会用到的术语：

- **IRModule**：TVM 里「一整张计算图模块」的顶层容器，里面装着多个 Relax 函数（如 `prefill`、`decode`）和 TIR `PrimFunc`（底层算子）。编译就是把这张图一步步变形、降低、最后导出。
- **pass / pipeline**：对 IRModule 做一次变换的函数叫一个 pass；把多个 pass 串起来按顺序执行就是 pipeline（流水线）。
- **target**：编译的「目标硬件描述」，如 `cuda`、`metal`、`vulkan`、`webgpu`、`llvm`（CPU）。同一份模型图，不同 target 会编译出完全不同的底层代码。
- **tensor parallelism（张量并行）**：把一个大矩阵切到多张 GPU 上各算一部分，再把结果合并。本讲的 preprocs 就是为它准备权重分片的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/mlc_llm/interface/compile.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py) | 编译接口层本体：`CompileArgs`、`compile()` 入口、`_compile()` 主流程、`_apply_preproc_to_params_and_check_pipeline` |
| [python/mlc_llm/interface/compiler_flags.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compiler_flags.py) | `OptimizationFlags`（`--opt`）与 `ModelConfigOverride`（`--overrides`）两个伴生结构 |
| [python/mlc_llm/cli/compile.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/compile.py) | CLI 入口：解析 argv、`detect_*` 自动探测、组装参数后调 `compile()` |
| [python/mlc_llm/support/auto_target.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_target.py) | `detect_target_and_host`：把 `--device` 翻译成 `(Target, build_func)`，含各平台的 `build_*` 实现与 `PRESET` 表 |
| [python/mlc_llm/support/tensor_parallel.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/tensor_parallel.py) | `ShardSingleDim`：分片策略对象，提供 `gen_shard_info`（配方）与 `gen_tir`（分片 TIR） |
| [python/mlc_llm/compiler_pass/pipeline.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py) | `_mlc_llm_pipeline`：编译 pass 流水线本体（U8 会逐 pass 展开，本讲只关注它如何被挂载） |
| [cpp/multi_gpu/multi_gpu_loader.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/multi_gpu/multi_gpu_loader.cc) | 运行期消费 preprocs 配方、真正切分并 scatter 权重的 C++ 代码 |

---

## 4. 核心概念与源码讲解

### 4.1 `compile()` 主流程与 `CompileArgs`

#### 4.1.1 概念说明

`compile` 是 MLC 把「一份 `mlc-chat-config.json`」变成「一个可加载的模型库」的接口层函数。它本身**不做**具体的图优化，而是扮演一个**编排者（orchestrator）**：把配置、量化、建图、pass 流水线、产物导出这五件事按正确顺序串起来，并喂给 TVM 编译器。

这里有个贯穿全讲的分层意识：

- **CLI 层**（`cli/compile.py`）只负责把命令行字符串翻译成结构化对象（`Target`、`Quantization`、`Model`、`OptimizationFlags`…），与 u2-l1 讲的「cli / interface 两层」一致。
- **接口层**（`interface/compile.py`）拿到这些结构化对象后，才是真正的编译逻辑，因此它既能被命令行调，也能被 Python 代码直接调（JIT 兜底编译走的也是这条路径，见 u1-l4）。

#### 4.1.2 核心流程

`compile()` 的顶层骨架非常薄——解析配置、装信封、显示、干活：

```text
mlc_llm compile（CLI）                 python 代码 / JIT 也可直接调
        │                                       │
        ▼                                       ▼
   compile(config, quantization, model_type, target, opt,
           build_func, system_lib_prefix, output, overrides, debug_dump)
        │
        ├─ 1. 从 config(dict) 构造 model_config（from_dict；处理 active_vocab_size）
        ├─ 2. 装进 CompileArgs 信封（触发 opt.update 矫正）
        ├─ 3. args.display() 打印编译参数
        └─ 4. _compile(args, model_config)  ← 真正的编译主体
```

`_compile()` 内部又分五步（与源码注释的 Step 1/2/3 对应，再加 pass 与导出）：

```text
_compile(args, model_config):
    with args.target:                       # 进入 target 上下文
        op_ext.enable(...)                  # 按 opt 开关启用扩展算子派发
        # Step 1. 建立量化后的模型（nn.Module）
        model = args.model.quantize[kind](model_config, quantization)
        # Step 2. 导出成 TVM IRModule
        mod, named_params, ext_mods = model.export_tvm(spec=model.get_default_spec())
        # Step 3. 预处理参数（写 preprocs 配方 + 生成分片 TIR）
        additional_tirs = _apply_preproc_to_params_and_check_pipeline(named_params, model_config)
        # 组装 metadata、variable_bounds、pass_config
        with PassContext(config=pass_config):
            # Step 4+5. 挂载 mlc_llm pass 流水线，交 build_func 导出
            args.build_func(mod, args, pipeline=relax.get_pipeline("mlc_llm", ...))
        _report_memory_usage(metadata, model_config)
```

#### 4.1.3 源码精读

先看入口 `compile()`。它从命令行读到的 `config` 是一个 `dict`（由 CLI 层 `json.load(mlc-chat-config.json)` 得来，见 [cli/compile.py:L138-L139](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/compile.py#L138-L139)）。`compile()` 先把它变成结构化的 `model_config` 对象：

[compile.py:L228-L265](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L228-L265) 完成三件事：抽出 `active_vocab_size`（u2-l2 讲过：用 HF tokenizer 实测的真实词表大小，覆盖被 padding 的 `vocab_size`）、用 `model_type.config.from_dict(config)` 构造配置对象（u3-l3 的 `from_dict` 机制，未命中字段统统塞进 `kwargs`）、最后装进 `CompileArgs` 并调 `_compile`。其中 `from_dict` 这步是「JSON → 带校验的 dataclass」的关键。

`CompileArgs` 是一只「编译信封」，把编译需要的全部输入打包到一个 dataclass 里，方便在 `build_func` 等-TVME-回调之间整体传递。字段定义见 [compile.py:L27-L41](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L27-L41)：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `config` | `Path` | 已解析好的 `model_config`（注意：类型注解写 `Path` 是历史遗留，实际传入的是 `model_config` 对象） |
| `quantization` | `Quantization` | 量化配置对象（u5-l1） |
| `model` | `Model` | 架构信封（u3-l1） |
| `target` | `Target` | 目标硬件 |
| `opt` | `OptimizationFlags` | `--opt` 优化开关 |
| `build_func` | `Callable` | 「IRModule → 磁盘产物」的导出函数，由 target 决定 |
| `system_lib_prefix` | `str` | iOS/Android 静态库前缀 |
| `output` | `Path` | 输出文件路径 |
| `overrides` | `ModelConfigOverride` | `--overrides` 模型结构参数覆盖 |
| `debug_dump` | `Optional[Path]` | 调试 dump 目录（每个 phase 后导出 IR） |

`CompileArgs` 有一个关键的 `__post_init__`：

[compile.py:L42-L43](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L42-L43) 在信封构造完成的瞬间调用 `self.opt.update(self.target, self.quantization)`，这是「opt 自我矫正」——用户在命令行写的 `--opt` 是「愿望」，`update` 会按 target 与量化方案把它矫正成「现实」。例如 `flashinfer` 只在 CUDA 且算力 ≥ 80 时才真正生效（见 [compiler_flags.py:L87-L101](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compiler_flags.py#L87-L101)），`cublas_gemm` 只对 fp16/bf16/fp32 或 fp8 量化生效（[compiler_flags.py:L103-L113](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compiler_flags.py#L103-L113)）。于是 `display()` 打印出来的 `--opt` 与用户传入的可能不同——这正是 u2-l2 提到的「OptimizationFlags.update 按 target 矫正 `--opt` 实际生效项」的落点。

接下来看 `_compile()` 的 Step 1 与 Step 2。注意整段被包在 `with args.target:` 里（[compile.py:L136](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L136)），这会把该 target 设为 TVM 全局当前 target，后续 pass 里 `Target.current()` 就能取到它。`op_ext.enable(...)`（[compile.py:L137-L142](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L137-L142)）根据 opt 开关启用对应的后端算子派发（flashinfer / faster_transformer / cutlass），这决定了后面 `dispatch_triton_kernel`、`blas_dispatch` 等 pass 能否找到专用 kernel。

Step 1 建立量化后的模型，并对张量并行 + 不兼容量化做前置拒绝：

[compile.py:L143-L160](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L143-L160)。这里有两道「配置校验前置」的护栏：`ft-quant` 不支持张量并行（直接 `NotImplementedError`），`KN` 权重布局（`q3f16_0`、`q4f16_0`）也不支持张量并行。L160 才是真正建图：`args.model.quantize[args.quantization.kind](model_config, args.quantization)` —— 即 u5-l1 讲的两级查表（名字 → `Quantization` 配置 → `kind` → 量化函数），返回量化后的 `nn.Module`，丢弃 `QuantizeMapping`（编译期不需要它，那是 convert_weight 的事）。

Step 2 把 `nn.Module` 导出成 TVM 的 IRModule：

[compile.py:L161-L166](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L161-L166) 调用 `model.export_tvm(spec=model.get_default_spec(), allow_extern=True)`，得到三样东西：

- `mod`：装着 `prefill`/`decode`/`batch_verify` 等 Relax 函数的 `IRModule`；
- `named_params`：`List[(name, nn.Parameter)]`，每个参数还带着它的 `attrs`（这是下一节 preprocs 的载体）；
- `ext_mods`：外部模块（如手写 Triton/cutlass kernel 的外部库），最终由 `AttachExternModules` 挂到 IRModule 上。

`get_default_spec()` 就是 u3-l2 讲的「张量接口契约」，它声明每个阶段方法的形状、dtype、param_mode，`export_tvm` 据此决定导出哪些 Relax 函数。

#### 4.1.4 代码实践

**实践目标**：把 `compile()` 从「dict 配置」到「调 `_compile`」的顶层骨架走一遍，确认它确实是个薄编排层。

**操作步骤**：

1. 打开 [compile.py:L228-L265](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L228-L265)，在脑中（或纸上）把 `compile()` 的语句编号 1–7。
2. 对照 [cli/compile.py:L141-L152](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/compile.py#L141-L152)，确认 CLI 传入的每个关键字参数分别由哪个 `detect_*` 或命令行选项产生（例如 `target`/`build_func` 来自 `detect_target_and_host`，`opt` 来自 `--opt` 经 `OptimizationFlags.from_str`）。
3. 思考：为什么 `compile()` 要先把 `active_vocab_size` 从 `config` 里 `pop` 出来再 `from_dict`？（提示：`vocab_size` 是架构字段会被 `from_dict` 校验，而 `active_vocab_size` 是运行期实测值，走 `kwargs` 通道。）

**需要观察的现象**：`compile()` 函数体里没有任何图优化代码，也没有 `import tvm` 的算子调用——它只做组装与转发。

**预期结果**：你应当能得出「`compile()` = 解析配置 + 装信封 + 转发 `_compile`」的结论，真正干活的是 `_compile` 和 `build_func`。

**待本地验证**：若你已 `pip install mlc_llm`，可在 Python 里 `from mlc_llm.interface.compile import compile, CompileArgs; help(compile)` 查看签名，确认它就是本节描述的参数列表。

#### 4.1.5 小练习与答案

**练习 1**：`CompileArgs` 的 `__post_init__` 为什么只调 `opt.update`，而不调 `overrides` 的某个 update？`overrides` 又是在什么时机生效的？

> **答案**：`opt` 必须在信封构造时就按 target/quantization 矫正，因为后续 `display()` 和 pass 流水线都要用矫正后的值。`overrides`（`ModelConfigOverride`）作用对象是 `model_config`，它在 `_compile` 内部由 [compile.py:L135](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L135) 的 `args.overrides.apply(model_config)` 生效——即建图之前，这样覆盖后的 `tensor_parallel_shards`、`prefill_chunk_size` 等才会真正影响生成的图。

**练习 2**：Step 1 里 `args.model.quantize[args.quantization.kind](...)` 用到了哪两级查表？为什么编译期要丢弃返回的第二个值？

> **答案**：第一级：`QUANTIZATION` 名字 → `Quantization` 配置（CLI 层已完成，结果存在 `args.quantization`）；第二级：`args.quantization.kind`（如 `group-quant`）→ `args.model.quantize[kind]` 量化函数（由 `make_quantization_functions` 工厂生产，u5-l1）。返回的 `(model, quantize_map)` 中，`quantize_map` 记录的是「原始权重名 → 量化名」拆分关系，那是 convert_weight 转权重时用的；编译期只关心改写后的计算图 `model`，所以丢弃 `_`。

---

### 4.2 参数预处理 preprocs（shard / pipeline）

#### 4.2.1 概念说明

`export_tvm` 吐出的 `named_params` 是一组带 `attrs` 的 `nn.Parameter`。其中一部分参数（典型是注意力的 `qkv_proj`、`o_proj`、FFN 的 `gate_up_proj`、`down_proj`）在模型定义时就被标注了 `shard_strategy`——一个 `ShardSingleDim` 对象，说明「这个权重在张量并行时该沿哪一维切」。

本节的核心是一个常被混淆的区分：

> **编译期只「记配方」，运行期才「真切」。**

`compile()` **不会**在编译期把权重切了（它根本不接触权重文件，权重是 `convert_weight` 的产物）。它在编译期做的是两件事：

1. 给每个带 `shard_strategy` 且 `tensor_parallel_shards > 1` 的参数，往它的 `attrs["preprocs"]` 列表里追加一条**配方（recipe）**——一个 dict，含 `func_name`、`in_shape`、`out_shape`、`out_dtype`。
2. 用 `shard_strategy.gen_tir(...)` 生成一份**分片 TIR PrimFunc**（名字就是 `func_name`），把它塞进 IRModule，随模型库一起编译。

这条配方最终会序列化进模型库的 metadata（JSON）。运行期加载多 GPU 权重时，C++ 侧的 `multi_gpu_loader` 读到配方，就知道：「先把完整权重按 `in_shape` 读进来，调用名为 `func_name` 的 TIR 函数（已编译进库）把它变成 `out_shape`，然后第 0 份给 GPU0、第 1 份给 GPU1……」。

这与 u4-l3 的 **preshard** 形成对照：preshard 在 convert_weight 阶段就**真的把权重切成 N 份落盘**，启动快但绑定卡数；preprocs 只记配方，权重仍是整份，运行期现场切，灵活但启动多一步计算。两者共享 `ShardSingleDim`，必须读同一个 `tensor_parallel_shards` 才能对得上。

#### 4.2.2 核心流程

```text
对每个 (name, param) in named_params:
    preprocs = param.attrs.get("preprocs", [])          # 可能已有别的预处理
    shard_strategy = param.attrs.get("shard_strategy")  # 模型定义时标注，可能为 None

    if shard_strategy is not None and tensor_parallel_shards > 1:
        recipe = shard_strategy.gen_shard_info(shards, weight=param)   # ① 追加配方
        preprocs.append(recipe)
        if shard_strategy.name not in extra_tirs:
            extra_tirs[name] = shard_strategy.gen_tir(shards, weight=param)  # ② 生成 TIR（去重）

    param.attrs["preprocs"] = preprocs                  # ③ 写回参数属性

    # —— 顺带做 pipeline parallel 的校验与补默认值 ——
    if pipeline_parallel_stages != 1:
        assert param 已标注 pipeline_stages
    param.attrs["pipeline_stages"] = 去重后的 stage 列表（默认 [0]）
```

`shard_strategy` 是怎么来的？它在**模型定义**时由 `tp.ShardSingleDim(...)` 构造并挂到参数 `attrs` 上。以 Starcoder2 为例，[starcoder2_model.py:L162-L192](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/starcoder2/starcoder2_model.py#L162-L192) 给 `qkv_proj` 挂 `ShardSingleDim("_shard_qkv_weight", dim=0, segs=[q,k,v])`、给 `o_proj` 挂 `ShardSingleDim("_shard_o", dim=1)`。`dim=0` 表示沿输出维切（行切），`dim=1` 表示沿输入维切（列切）——这正是张量并行的标准切法：`QKV` 行切、`O` 列切。

#### 4.2.3 源码精读

函数本体在 [compile.py:L62-L95](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L62-L95)。逐段看：

[compile.py:L67-L82](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L67-L82) 是分片的核心：

- L68 取出该参数已有的 `preprocs` 列表（默认空）。`preprocs` 是个列表，因为一个参数可能先后经历多个预处理（这里只追加分片这一步）。
- L69 取 `shard_strategy`，未标注则是 `None`。
- L70 的判断条件 **`shard_strategy is not None and tensor_parallel_shards > 1`** 是关键开关：单卡（`tensor_parallel_shards == 1`）时完全不切，连配方都不记。
- L72-L75 调 `gen_shard_info` 生成配方并 `append`。
- L77-L81 用 `shard_strategy.name` 去重地生成 TIR——`_shard_qkv_weight` 这个名字在所有层都一样（同一种切法），所以只在第一次遇到时生成一份 TIR，复用给所有层。
- L82 把追加后的 `preprocs` 写回 `param.attrs`。

`gen_shard_info` 与 `gen_tir` 都来自 `ShardSingleDim`：

[tensor_parallel.py:L85-L92](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/tensor_parallel.py#L85-L92) 是配方。以一个 `[O, I]` 权重、`dim=0`、`shards=N` 为例：

```python
{
    "func_name": "_shard_qkv_weight",
    "in_shape":  [O * N, I],          # _compute_in_shape：dim 维乘以 shards
    "out_shape": (N, O, I),           # 多了一个前置的 shards 维
    "out_dtype": "..."
}
```

注意 `in_shape` 把目标切分维**放大了 N 倍**——这是因为运行期 loader 读入的是「按 head 交错排布后的完整权重」（由 convert_weight 的 preshard 或加载时的 reshape 产生），它的行数是原始的 N 倍。`out_shape` 则在最前面多了一个 `shards` 维，表示输出是「N 份」。

[tensor_parallel.py:L36-L83](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/tensor_parallel.py#L36-L83) 是 `gen_tir`，它用 TVM 的张量表达式（`te.compute` + `topi.reshape/transpose/concatenate`）描述了一个**纯张量重排**计算：

- 输入占位符 `w` 的形状是 `in_shape`（如 `[O*N, I]`）；
- 对 `segs` 中的每一段（QKV 场景下 `segs=[q,k,v]`，三段分别处理），`te.compute` 截取对应区段，`reshape` 成 `[shards, seg, ...]`，`transpose` 把 `shards` 维换到最前；
- 各段 `concatenate` 起来，得到形状 `(shards, O, I)` 的输出 `o`；
- `te.create_prim_func([w, o])` 把它封成一个 TIR `PrimFunc`。

这种「reshape + transpose」的妙处在于**交错分布**：reshape `[seg*N, I] → [N, seg, I]` 使得原本连续的 `seg` 行被打散到 N 个 shard，每个 shard 拿到均衡的、跨整个 head 范围的子集——避免某个 shard 总是拿到「靠前的 head」造成负载不均。这正是张量并行想要的。

这些 TIR 随后被喂给流水线。在 [pipeline.py:L113](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py#L113)，`AttachAdditionalPrimFuncs(additional_tirs)` 把它们挂到 IRModule 上（实现见 [attach_support_info.py:L31-L42](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_support_info.py#L31-L42)，就是给每个 TIR 加 `global_symbol` 属性塞进 `mod`），于是 `_shard_qkv_weight` 这份 TIR 会随模型库一起编译成可执行函数。

最后，配方序列化进 metadata。在 [compile.py:L123-L131](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L123-L131) 的 `_get_param_metadata`，每个参数的 `preprocs` 被原样写进 `metadata["params"]`，最终随 `relax.build` 编进库的 metadata JSON。

**运行期消费侧**（理解 preprocs 闭环必看）。C++ 的 `multi_gpu_loader.cc` 读取配方并真切：

[multi_gpu_loader.cc:L103-L127](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/multi_gpu/multi_gpu_loader.cc#L103-L127) 的 `BroadcastOrShardAndScatter`：

- 若 `param_info.preprocs` 非空（`needs_sharding`），loader 先按 `preprocs[0].in_shape` 接收完整权重（L104），再调 `preprocs.Apply(param, param_info)` 执行那份编译好的 TIR 函数（L123），得到形状 `(num_shards, ...)` 的结果，最后 `ScatterFromWorker0` 把第 i 份发给第 i 个 worker。
- 若为空（单卡或未标注），直接 `BroadcastFromWorker0` 全量广播。

配方的 C++ 结构定义在 [model.h:L61-L67](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/metadata/model.h#L61-L67)，字段正是 `func_name / in_shape / out_shape / out_dtype`，与 Python 侧一一对应。

至于 **pipeline parallel**（流水线并行）部分，[compile.py:L84-L94](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L84-L94) 做的是：当 `pipeline_parallel_stages != 1` 时，断言每个参数都已显式标注 `pipeline_stages`（说明它属于哪个 stage），否则给一个默认值 `[0]`。这同样是「记元数据」，真正的流水线切分由后端 pass `PipelineParallelRewrite`（U8）完成。

#### 4.2.4 代码实践

**实践目标**：亲手构造一个 `ShardSingleDim`，验证 `gen_shard_info` 返回的配方形状，并对照 `gen_tir` 输出的 TIR，理解「放大 N 倍的 in_shape」与「多一维的 out_shape」。

**操作步骤**：

1. 若已安装 `mlc_llm` 与 `tvm`，在 Python 中运行（**示例代码**）：

   ```python
   from tvm.relax.frontend import nn
   from mlc_llm.support.tensor_parallel import ShardSingleDim

   # 模拟一个 qkv 权重 [q+k+v, I] = [3072, 4096]，dim=0，shards=2
   w = nn.Parameter((3072, 4096), dtype="float16")
   strat = ShardSingleDim("_shard_qkv_weight", dim=0, segs=[1024, 1024, 1024])

   print("recipe:", strat.gen_shard_info(shards=2, weight=w))
   # 期望 in_shape=[6144, 4096], out_shape=(2, 3072, 4096)

   tir_func = strat.gen_tir(shards=2, weight=w)
   print(tir_func.script())   # 读 TIR：reshape + transpose 的纯重排
   ```

2. 阅读打印出的 TIR，找到 `te.compute` 截取区段、`reshape` 成 `[shards, seg, ...]`、`transpose` 把 shards 维提到最前的那几行。
3. 对照 [compile.py:L70-L81](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L70-L81)，确认这份 `gen_shard_info` 的输出会被 `append` 进 `preprocs`、`gen_tir` 的输出会进 `extra_tirs`。

**需要观察的现象**：`in_shape` 的第 0 维正好是原 shape 第 0 维的 2 倍；`out_shape` 比 `in_shape` 多一个前置的 `2`（shards 维），其余维与原 shape 一致。

**预期结果**：你能用一句话解释「为什么 in_shape 放大 N 倍而 out_shape 多一维」——因为输入是交错排布的整份权重（行数 ×N），输出是切好的 N 份（前置 shards 维），运行期再 scatter 给各卡。

**待本地验证**：若本机无 GPU 或未装 tvm，可改为纯源码阅读：对照 [tensor_parallel.py:L94-L97](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/tensor_parallel.py#L94-L97) 的 `_compute_in_shape` 手算 `[O,I],dim=0,N=2 → [O*2, I]`，并对照 [tensor_parallel.py:L85-L92](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/tensor_parallel.py#L85-L92) 推出 `out_shape=(2, O, I)`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `gen_tir` 用 `shard_strategy.name not in extra_tirs` 做去重，而不是「每个参数各生成一份 TIR」？

> **答案**：同一种切法（如所有层的 `_shard_qkv_weight`）共享同一个名字与同一份 TIR 实现，只是各层权重形状不同。TIR 函数内部用占位符 `w` 抽象了具体形状，所以一份函数可服务所有层。去重避免了在 IRModule 里塞入几十份重复 PrimFunc。

**练习 2**：把 `tensor_parallel_shards` 从 2 改成 1，`_apply_preproc_to_params_and_check_pipeline` 的行为会怎么变？模型库里还会有 `_shard_qkv_weight` 这个函数吗？

> **答案**：L70 的条件 `tensor_parallel_shards > 1` 不满足，整段跳过：`preprocs` 不追加配方、`extra_tirs` 不生成 TIR。模型库里不会有 `_shard_qkv_weight`，metadata 里这些参数的 `preprocs` 也是空，运行期 loader 走 `BroadcastFromWorker0` 全量广播。

**练习 3**：preprocs（编译期记配方）和 preshard（convert_weight 真切）都基于 `ShardSingleDim`，两者可以同时用吗？

> **答案**：实际是二选一的关系，由是否设置环境变量 `MLC_INTERNAL_PRESHARD_NUM` 决定（u4-l3）。preshard 把权重预切好落盘，运行期 loader 用 `LoadMultiGPUPresharded` 各取一份；preprocs 把整份权重随库带的 TIR 现场切。两端读同一个 `tensor_parallel_shards` 才能对齐，否则切数不匹配会出错。

---

### 4.3 `build_func` 与产物导出

#### 4.3.1 概念说明

`_compile` 走到 Step 4+5 时，IRModule 已经准备好（含 Relax 函数 + 分片 TIR + 各类 attach 的辅助函数 + metadata）。最后一步是 `args.build_func(mod, args, pipeline=...)`。`build_func` 是一个**由 target 决定的回调函数**，签名固定为 `(IRModule, CompileArgs, Pass) -> None`，职责是：

1. 用传入的 `pipeline`（`mlc_llm` pass 流水线）调用 `relax.build(...)`，把 IRModule 编译成可执行的 VM 模块；
2. 把 VM 模块 `export_library` 成目标平台对应的磁盘文件（`.so` / `.dylib` / `.dll` / `.tar` / `.wasm`）。

之所以把 `build_func` 设计成「按 target 注入」的回调，是因为**不同平台的导出方式差异巨大**：iPhone 要用 `xcode` 编 Metal 并打 `.tar` 静态库，Android 用 `ndk` 编 OpenCL，WebGPU 要链接 `mlc_wasm_runtime.bc` 产出 `.wasm`，而 CUDA/Vulkan/Metal(桌面) 则是常规 `.so`/`.dylib`。把这些差异封进各自的 `build_*` 函数，`_compile` 主体就与具体平台彻底解耦。

#### 4.3.2 核心流程

```text
cli/compile.py
   target, build_func = detect_target_and_host(parsed.device, parsed.host, ...)
        │  按 device 字符串选 PRESET 或自动探测，返回 (Target, build_func)
        ▼
_compile（Step 4+5）
   with PassContext(config={"relax.backend.use_cuda_graph": ..., "tirx.disable_cse_tir": True}):
       args.build_func(
           mod, args,
           pipeline = relax.get_pipeline("mlc_llm", target=..., additional_tirs=..., metadata=..., ...),
       )
        │
        ▼  build_func 内部（以 _build_default 为例）
   mod = _add_system_lib_prefix(mod, prefix, is_system_lib)
   relax.build(mod, target=args.target, relax_pipeline=pipeline, system_lib=...)
       .export_library(str(args.output))
        │
        ▼
   磁盘产物：output.so / output.tar / output.wasm / output.dylib
```

#### 4.3.3 源码精读

`build_func` 的来源是 [cli/compile.py:L125-L129](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/compile.py#L125-L129)：`detect_target_and_host(parsed.device, parsed.host, ...)` 同时返回 `target` 与 `build_func`，这正是 u2-l2 讲的「`--device` 经 detect_target_and_host 变成 Target + build_func」。

[auto_target.py:L31-L66](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_target.py#L31-L66) 是 `detect_target_and_host`：先 `_detect_target_gpu` 选 target 与 build，再补 host（CPU target），给 cuda/rocm target 追加 `libs`（thrust、rocblas 等），最后返回 `(target, build_func)`。`_detect_target_gpu` 的分支顺序值得注意（[auto_target.py:L83-L121](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_target.py#L83-L121)）：

- 命中 `PRESET` 字典（iphone/macabi/android/webgpu/mali/opencl/metal/vulkan 等）→ 用对应的 `target` 配置和 `build` 函数（如 `_build_iphone`、`_build_webgpu`）；
- `auto` 或已知设备 → 自动探测并 `_build_default`；
- 形如 `cuda xx` 的设备串 → `Target.from_device` + `_build_default`；
- 纯 target 字符串 → `Target(hint)` + `_build_default`。

各 `build_*` 实现的核心差异在 `export_library` 的 `fcompile` 与 `libs` 参数。最通用的是 [auto_target.py:L310-L330](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_target.py#L310-L330) 的 `_build_default`：按 `output.suffix` 判断 `system_lib`（`.tar`/`.lib` → 静态库 `True`，`.so`/`.dylib`/`.dll` → 动态库 `False`），加 `system_lib_prefix`，`relax.build(...).export_library(output)`。特化的例如：

- [auto_target.py:L251-L288](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_target.py#L251-L288) `_build_webgpu`：断言 `.wasm`、`system_lib=True`，并额外链接 `mlc_wasm_runtime.bc`（WASM 运行时），缺它则报错提示跑 `web/prep_emcc_deps.sh`。
- [auto_target.py:L180-L202](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_target.py#L180-L202) `_build_iphone`：用 `@register_global_func("tvm_callback_metal_compile")` 注册 Metal 编译回调，`export_library` 用 `tar.tar` 打包。

回到 `_compile`，看它如何调用 `build_func`：

[compile.py:L199-L223](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L199-L223)。先组装 `pass_config`：`relax.backend.use_cuda_graph`（由 `opt.cudagraph` 决定）+ `tirx.disable_cse_tir`（一个临时 workaround，避免 TVM CSE 在 host codegen 时产生悬空的 `cse_v*` 变量）。然后在 `PassContext(config=pass_config)` 里调 `args.build_func`，传入用 `relax.get_pipeline("mlc_llm", ...)` 构造的 pipeline。

`relax.get_pipeline("mlc_llm", ...)` 这一行是 pass 流水线的挂载点——它按名字查到 `pipeline.py` 里 `@register_pipeline("mlc_llm")` 注册的 `_mlc_llm_pipeline`（[pipeline.py:L81-L209](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py#L81-L209)），并把这一大堆参数（target、flashinfer、variable_bounds、`additional_tirs`、`metadata`、`ext_mods`、`debug_dump` 等）透传给它。注意 `additional_tirs`（我们的分片 TIR）就是在这里被传进流水线、由 Phase 0 的 `AttachAdditionalPrimFuncs` 挂上 IRModule 的。pipeline 内部的五阶段（attach → 高层融合 → Relax 降级到 TIR → TIR 优化 → 底层 dlight/CUDA graph/内存规划）是 U8 的内容，本讲只把它当成一个「黑盒 pass 序列」。

最后，[compile.py:L224](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L224) 调 `_report_memory_usage(metadata=metadata, config=model_config)`，把各函数的显存估算（由 `AttachMetadataWithMemoryUsage` pass 写进 metadata）汇报出来，供运行期显存规划参考。

补一点：`metadata` 字典（[compile.py:L180-L198](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L180-L198)）是编译期与运行期共享的「契约」——它包含 `model_type`、`quantization`、`context_window_size`、`tensor_parallel_shards`、`kv_state_kind`、`params`（含 preprocs）等，会被编进库的 metadata JSON。C++ 引擎加载库时（FunctionTable，U9）正是从这里读到所有运行期需要的元信息。`_infer_kv_state_kind`（[compile.py:L98-L105](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L98-L105)）按模型名推断 KV 状态类型（rwkv→`rnn_state`、qwen3_5→`hybrid`、medusa→`none`、其余→`kv_cache`），这是给运行期 KV cache 分配用的。

#### 4.3.4 代码实践

**实践目标**：搞清 `--device` → `build_func` → 产物后缀 的对应关系，验证「不同平台产出不同格式」。

**操作步骤**：

1. 打开 [auto_target.py:L423-L554](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_target.py#L423-L554) 的 `PRESET` 表，自制一张对照表：

   | device hint | target.kind | build 函数 | 产物后缀 |
   | --- | --- | --- | --- |
   | `iphone:generic` | metal | `_build_iphone` | `.tar` |
   | `android:generic` | opencl | `_build_android` | `.tar` |
   | `webgpu:generic` | webgpu | `_build_webgpu` | `.wasm` |
   | `metal:x86-64` | metal | `_build_metal_x86_64` | `.dylib` |
   | `cuda`（自动探测） | cuda | `_build_default` | `.so` |
   | `vulkan:generic` | vulcan | `_build_default` | `.so`/`.tar` |

2. 对照各 `_build_*` 函数里的 `assert output.suffix == ...`，确认产物后缀是由 build 函数硬性约束的（你传错后缀会 assert 失败）。
3. 思考：为什么 `_build_webgpu` 需要额外找 `mlc_wasm_runtime.bc`，而 `_build_default` 不需要？（提示：WebGPU/WASM 没有操作系统提供的动态库机制，运行时要静态链进同一个 `.wasm`。）

**需要观察的现象**：`build_func` 内部的骨架高度一致——`_add_system_lib_prefix` → `relax.build(mod, target, relax_pipeline=pipeline, system_lib=...)` → `.export_library(output, ...)`，差异只在 `system_lib` 取值与 `export_library` 的 `fcompile`/`libs` 参数。

**预期结果**：你能说出「换 `--device` 等于换 build_func 与 target，产物格式随之改变」，并指出生成 `.so`/`.dylib`/`.dll`/`.tar`/`.wasm` 各自对应的 device。

**待本地验证**：若本机有 CUDA，可尝试 `mlc_llm compile ... --device cuda -o build/model.so`，观察日志里 `Generated: ...model.so`。无 GPU 环境则改为纯源码阅读本表。

#### 4.3.5 小练习与答案

**练习 1**：`build_func` 的签名为什么是 `(mod, args, pipeline)` 而不是 `(mod, target, output)`？多带一个 `CompileArgs` 有什么好处？

> **答案**：`build_func` 需要的信息不止 target 与 output——还有 `system_lib_prefix`、`debug_dump`（android 会 dump `kernel.cl`）等，这些都在 `args` 里。把整个 `CompileArgs` 传进去，避免签名随需求膨胀，也契合 u2-l1 讲的「信封式传参」。`pipeline` 单独作为第三参，是因为它由 `_compile` 现场用 `relax.get_pipeline(...)` 构造、不属于 `CompileArgs`。

**练习 2**：`pass_config["tirx.disable_cse_tir"] = True` 这条 workaround 如果去掉，可能出什么问题？

> **答案**：注释说明是应对 TVM CSE（公共子表达式消除）回归——CSE 可能在 host codegen 时产生悬空的 `cse_v*` 变量（定义被消除但引用还在），导致编译出的库在运行期崩。临时关掉 TIR 级 CSE 规避，等 TVM 修好回归后这条可删（代码里也有对应 TODO 注释，[compile.py:L200-L203](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L200-L203)）。

**练习 3**：`metadata` 字典最终流向哪里？为什么它对运行期引擎如此重要？

> **答案**：`metadata` 被传进 `relax.get_pipeline(...)`，最终由 `AttachMetadataWithMemoryUsage` 等 pass 写进模型库的 metadata JSON。运行期 C++ 引擎（FunctionTable，U9）加载库时从这份 JSON 读到 `tensor_parallel_shards`、`kv_state_kind`、`params`（含 preprocs 配方）、显存估算等全部运行期信息——它是编译期与运行期共享的契约，地位类似于 `mlc-chat-config.json`，但服务对象是「模型库内部」。

---

## 5. 综合实践

把本讲三节串起来，做一次「**编译期全景跟踪**」。选一个小模型（如 RedPajama-INCITE-Chat-3B-v1，或任意你已跑通 `gen_config` 的小模型），完成以下任务：

1. **顶层骨架**：从 [cli/compile.py:L141-L152](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/compile.py#L141-L152) 出发，画一张时序图，标出 `detect_target_and_host`（产出 target+build_func）、`detect_model_type`、`detect_quantization`、`compile()`、`_compile()`、`build_func` 的调用先后与各自输入输出。

2. **五步标注**：在 [compile.py:L108-L225](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L108-L225) 的 `_compile` 里，用五种颜色（或五个标签）分别标出：①建模+量化（Step 1）、②导出 IRModule（Step 2）、③preprocs 预处理（Step 3）、④组装 metadata/pass_config、⑤build_func 导出。确认 pass 流水线是在第 ⑤ 步内被 `relax.get_pipeline("mlc_llm", ...)` 挂上、由 `build_func` 驱动执行的。

3. **preprocs 闭环**：用 `--overrides tensor_parallel_shards=2`（或直接读对应模型 `_model.py` 里的 `ShardSingleDim` 标注）跟踪一次分片配方的一生：
   - 在 [compile.py:L70-L81](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L70-L81) 处，`gen_shard_info` 把配方写进 `param.attrs["preprocs"]`、`gen_tir` 把 TIR 收进 `additional_tirs`；
   - 在 [compile.py:L198](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L198) 处，`_get_param_metadata` 把 `preprocs` 抄进 `metadata["params"]`；
   - 在 [pipeline.py:L113](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py#L113) 处，`AttachAdditionalPrimFuncs` 把 TIR 挂进 IRModule、随库编译；
   - 在 [multi_gpu_loader.cc:L110-L127](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/multi_gpu/multi_gpu_loader.cc#L110-L127) 处，运行期 loader 读配方、调那份编译好的 TIR 切分、scatter 给各卡。

   写一段话（150 字以内）解释：「为什么说 preprocs 是『编译期记配方、运行期真切』」，并指出它和 convert_weight 的 preshard 共享了什么、又差在哪里。

4. **产物验证**（**待本地验证**，需可联网下载权重且有目标后端）：实际跑一次

   ```bash
   mlc_llm compile ./dist/<model>/mlc-chat-config.json \
       --device cuda --overrides tensor_parallel_shards=1 \
       -o build/<model>-cuda.so
   ```

   观察日志中的 `Compiling with arguments:` 块（来自 `CompileArgs.display`，[compile.py:L45-L59](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L45-L59)），确认 `--opt` 经 `update` 矫正后的值（例如非 cuda 或算力<80 时 `flashinfer=0`）。再对比 `tensor_parallel_shards=2` 重编一次，确认产物 metadata 里相关参数的 `preprocs` 非空（可用 `strings build/<model>-cuda.so | grep _shard` 或读库的 metadata JSON 验证）。

> 若本机无法实际编译，任务 1–3 是纯源码阅读型实践，可独立完成；任务 4 标注「待本地验证」。重点是在脑中把「配置 → IRModule → preprocs 配方 → pass 流水线 → build_func → 磁盘库」这条链路完整走通。

## 6. 本讲小结

- `compile()` 是个**薄编排层**：把 dict 配置变成 `model_config`、装进 `CompileArgs` 信封、转发给 `_compile`；CLI 层只做字符串→结构化对象的翻译。
- `CompileArgs` 在 `__post_init__` 调 `opt.update(target, quantization)`，把 `--opt` 的「愿望」按 target/量化矫正成「现实」（flashinfer 需 CUDA≥80、cublas_gemm 需特定量化等）。
- `_compile` 的五步：建模+量化（`model.quantize[kind]`）→ 导出 IRModule（`export_tvm(spec)`）→ preprocs 预处理 → 组装 metadata/pass_config → `build_func` 挂 pipeline 导出。
- **preprocs 的核心是「编译期记配方、运行期真切」**：`_apply_preproc_to_params_and_check_pipeline` 为带 `shard_strategy` 且 `tensor_parallel_shards>1` 的参数，用 `gen_shard_info` 写配方、用 `gen_tir` 生成分片 TIR；配方进 metadata、TIR 进 IRModule，运行期由 C++ `multi_gpu_loader` 消费。
- `ShardSingleDim` 是 preprocs 与 convert_weight preshard 的**共享底座**；区别是 preshard 真切落盘、preprocs 只记配方，二者必须读同一个 `tensor_parallel_shards`。
- `build_func` 由 `--device`（target）决定，封装了「IRModule → 平台产物」的最后一跳，不同平台对应不同 `build_*` 与产物后缀（`.so`/`.dylib`/`.tar`/`.wasm`）；`metadata` 作为编译期与运行期的共享契约被编进库。

## 7. 下一步学习建议

- **U7-L2「compiler pass pipeline 总览」**：本讲把 `_mlc_llm_pipeline` 当成黑盒，下一讲打开它，按「附加 / 高层融合 / Relax 降级 / TIR 优化 / 底层」五阶段逐 pass 拆解，理解 `additional_tirs` 与 `metadata` 在 Phase 0 是怎么被挂上去的。
- **U8「编译优化 pass 深入」**：深入 `FuseDequantizeMatmulEwise`、`BLASDispatch`、`AttachGPUSamplingFunc`、`LowBatchGemvSpecialize`、`PipelineParallelRewrite` 等具体 pass，理解本节一笔带过的 `PipelineParallelRewrite` 如何真正实现流水线切分。
- **U9-L4「模型运行时与 FunctionTable」**：从运行期视角看本讲产物——C++ 引擎如何加载模型库、读 metadata、用 FunctionTable 解析出 `prefill`/`decode` 等函数，以及 `multi_gpu_loader` 如何消费 preprocs 配方（本讲运行期侧的延续）。
- **复习 u4-l3 与 u5-l1**：若对 preshard 与量化的衔接仍有模糊，回看 u4-l3 的 `apply_preshard` 与 u5-l1 的 `quantize_model`/`quantize_weight` 两条路径，巩固「编译期改图 vs 转权重」的分工。
