# compiler pass pipeline 总览

## 1. 本讲目标

在 u7-l1 中，我们看到了 `compile()` 如何把一个模型导出成 Relax `IRModule`，再交给 `build_func` 产出平台专用模型库。本讲要回答的下一个问题是：**从 `IRModule` 到模型库之间，编译器到底对它做了什么？**

读完本讲，你应该能够：

- 说出 MLC LLM 编译流水线 `_mlc_llm_pipeline` 的**五个阶段**，以及每个阶段的目标。
- 知道这条流水线是如何被**注册、取出并在 `PassContext` 中驱动**的。
- 区分哪些 pass 是**无条件执行**的，哪些只在**特定 target（如 GPU）下生效**。
- 学会读懂一个 `compiler_pass` 的统一写法（`@tvm.transform.module_pass` 装饰器），并理解 `_LogProgress` / `_DebugDump` 这类"哑 pass"的作用。

本讲是 U8（逐个深入讲解融合 / 派发 / 附加类 pass 的算法）的总纲地图，**只讲编排顺序与设计意图，不展开单个 pass 的算法细节**。

## 2. 前置知识

本讲假设你已经具备 u7-l1 的认知，并理解下面几个 TVM/Relax 概念（不熟悉也不影响阅读，这里给出最小解释）：

- **IRModule**：TVM 里"一段待优化程序"的容器，里面装着若干函数（Relax 函数或 TIR `PrimFunc`）。所有 pass 的输入输出都是 `IRModule`——即"吃一张图，吐一张改写后的图"。
- **Pass（编译 pass）**：对 `IRModule` 做一次结构变换的函数，例如"把 `rms_norm(add(x,y),w)` 融合成一个 kernel"。
- **PassContext**：pass 运行时的"环境/配置袋"，可以放全局开关（如是否启用 CUDA graph）。
- **Relax vs TIR**：Relax 是高层（算子级、含数据流）的中间表示；TIR 是底层（循环、线程绑定）的张量中间表示。一条流水线通常经历"Relax → TIR → 机器码/VM 字节码"的逐级降级。
- **target**：编译目标，如 `cuda`、`rocm`、`llvm`（CPU）、`vulkan`、`metal`、`webgpu`。许多 pass 只在特定 target 下有意义。

如果你还记得 u7-l1 里的 `_compile` 五步（建模+量化 → 导出 IRModule → preprocs → 组装 metadata/pass_config → `build_func` 挂流水线导出），那么本讲就是把其中"挂流水线"这一步彻底打开。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [python/mlc_llm/compiler_pass/pipeline.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py) | **主角**。定义 `_mlc_llm_pipeline`，把所有 pass 按五阶段串成一条 `Sequential`。 |
| [python/mlc_llm/interface/compile.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py) | 调用方。在 `PassContext` 里用 `relax.get_pipeline("mlc_llm", ...)` 取出并运行流水线。 |
| [python/mlc_llm/interface/compiler_flags.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compiler_flags.py) | 定义 `OptimizationFlags`（`flashinfer`/`cublas_gemm`/`cudagraph` 等）与 `OPT_FLAG_PRESET`（O0–O3）。这些开关直接决定哪些 GPU pass 生效。 |
| [python/mlc_llm/compiler_pass/attach_support_info.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_support_info.py) | Phase 0 里多个"附加元数据"pass 的集合（`AttachVariableBounds` 等），代表一类典型 pass 写法。 |
| [python/mlc_llm/compiler_pass/attach_sampler.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_sampler.py) | Phase 0 的 `AttachGPUSamplingFunc`，是"仅 GPU target 生效"的典型例子。 |
| [python/mlc_llm/compiler_pass/blas_dispatch.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/blas_dispatch.py) | Phase 1 的 `BLASDispatch`，cuBLAS/hipBLAS 派发，cuda/rocm 专用。 |

> 说明：本讲引用的"算法细节型 pass"（融合、低 batch 特化、内存估算等）的内部实现，会在 U8 各讲义中专门展开；本讲只关注它们在流水线里的**位置与职责**。

## 4. 核心概念与源码讲解

### 4.1 PassContext 驱动：流水线如何被装配与运行

#### 4.1.1 概念说明

一条编译流水线要解决三个问题：**注册**（把流水线挂到一个名字下）、**装配**（调用方按当前模型/target/开关把参数填进去）、**驱动**（在合适的 `PassContext` 里跑起来）。MLC LLM 把这三件事分别交给 TVM Relax 提供的三个机制：

- `@register_pipeline("mlc_llm")`：把流水线注册到名字 `"mlc_llm"`。
- `relax.get_pipeline("mlc_llm", **kwargs)`：按名字取出流水线，并把 `target`、`flashinfer`、`metadata` 等作为"工厂参数"注入。
- `with PassContext(config=...)`：在运行期提供全局开关（如是否启用 CUDA graph）。

把这三者想成"工厂 + 订单 + 车间环境"：`register_pipeline` 是注册了一个名为 `mlc_llm` 的工厂模板，`get_pipeline` 是按当前订单参数实例化一条流水线，`PassContext` 是进入车间时设定的环境（开/关某些机器）。

#### 4.1.2 核心流程

1. 编译期，`import mlc_llm.compiler_pass`（compile.py 顶部 `from mlc_llm import compiler_pass as _`）会触发 `pipeline.py` 被导入，其上的 `@register_pipeline("mlc_llm")` 装饰器把 `_mlc_llm_pipeline` 登记进 TVM 的全局流水线表。
2. `_compile` 准备好 `metadata` 与 `pass_config` 后，进入 `with PassContext(config=pass_config):` 代码块。
3. 在该上下文内调用 `args.build_func(mod, args, pipeline=relax.get_pipeline("mlc_llm", ...))`，把 `IRModule` 和装配好的流水线一并交给 `build_func`。
4. `build_func` 最终以流水线为 pass 作用于 `mod`，完成从 Relax IRModule 到平台产物的降级。

#### 4.1.3 源码精读

**注册端**——`_mlc_llm_pipeline` 用装饰器登记名字，并声明一长串"工厂参数"（这些参数在流水线**装配时**确定，而不是每个 token 运行时确定）：

[python/mlc_llm/compiler_pass/pipeline.py:L81-L94](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py#L81-L94) —— 用 `@register_pipeline("mlc_llm")` 注册流水线，参数包括 `target`、`flashinfer`、`cublas_gemm`、`faster_transformer`、`allreduce_strategy`、`variable_bounds`、`metadata`、`ext_mods`、`debug_dump` 等。

注意它内部又包了一层 `@tvm.transform.module_pass` 的 `_pipeline`，真正的 pass 列表在 `tvm.transform.Sequential([...])` 里：

[python/mlc_llm/compiler_pass/pipeline.py:L102-L105](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py#L102-L105) —— 把整条流水线包成一个 `module_pass`，所有 pass 装进一个 `Sequential`（顺序执行）。

**驱动端**——`_compile` 在 `PassContext` 里取出并运行流水线：

[python/mlc_llm/interface/compile.py:L199-L205](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L199-L205) —— 构造 `pass_config`（含 `relax.backend.use_cuda_graph` 与 `tirx.disable_cse_tir`），并进入 `with PassContext(config=pass_config):`。

[python/mlc_llm/interface/compile.py:L205-L223](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L205-L223) —— 在 `PassContext` 内调用 `build_func`，并用 `relax.get_pipeline("mlc_llm", target=..., flashinfer=..., cublas_gemm=..., metadata=..., ...)` 把当前模型的所有装配参数注入流水线。

这里有一个关键分工值得记住：

- **进 `get_pipeline` 的参数**（`flashinfer`、`cublas_gemm`、`metadata` 等）在**装配期**决定，会变成流水线里某些 pass 的构造参数或条件开关。
- **进 `PassContext` 的参数**（`use_cuda_graph`）是**运行期全局开关**，被 TVM 内置 pass（如 `RewriteCUDAGraph`）读取。

#### 4.1.4 代码实践

**实践目标**：亲眼看到"注册 → 取出 → 驱动"三步在代码里如何衔接。

**操作步骤**：

1. 打开 `pipeline.py`，确认 `_mlc_llm_pipeline` 上方的装饰器名字是 `"mlc_llm"`。
2. 打开 `compile.py`，定位 `relax.get_pipeline("mlc_llm", ...)` 这一行，数一数它传入了多少个关键字参数。
3. 在 `compile.py` 中找到 `with PassContext(config=pass_config):`，查看 `pass_config` 字典里有哪两个键。

**需要观察的现象**：`get_pipeline` 的关键字参数与 `_mlc_llm_pipeline` 的形参**一一对应**（`target`/`flashinfer`/`cublas_gemm`/`faster_transformer`/`allreduce_strategy`/`variable_bounds`/`cuda_graph_symbolic_capture_hints`/`additional_tirs`/`ext_mods`/`metadata`/`debug_dump`）。

**预期结果**：你会清楚地看到，`compile.py` 里 `--opt` 解析出的每个开关（如 `args.opt.flashinfer`）都被**原样**喂进了 `get_pipeline`，这正是 u7-l1 中"`OptimizationFlags.update` 按 target 矫正 `--opt`"的最终消费点。

> 待本地验证：若你在本地安装了 mlc_llm 与 TVM，可以在 Python 里 `from tvm.relax import get_pipeline; print(get_pipeline("mlc_llm"))` 观察返回的 pass 对象；若 TVM 版本不同，API 名称可能略有差异。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `with PassContext(config=pass_config):` 这一层去掉（直接调用 `build_func`），`RewriteCUDAGraph` 还会生效吗？

**参考答案**：不会按预期生效。`RewriteCUDAGraph` 读取的是 `PassContext` 里的 `relax.backend.use_cuda_graph` 开关；脱离该上下文，开关为默认值（关闭），CUDA graph 改写不会发生。注意 `pass_config["relax.backend.use_cuda_graph"]` 的值来自 `args.opt.cudagraph`，而 `OptimizationFlags.update` 又限定 `cudagraph` 仅在 `cuda` target 下为真（见 compiler_flags.py 的 `_cudagraph`）。

**练习 2**：`get_pipeline` 的参数是"装配期"还是"运行期"决定的？

**参考答案**：装配期（编译期）。它们在模型被编译成库时一次性确定，写入流水线各 pass 的构造函数；与模型推理运行期无关。

---

### 4.2 五阶段流水线与各阶段目标

#### 4.2.1 概念说明

`_mlc_llm_pipeline` 把全部 pass 装进一个 `tvm.transform.Sequential`，并用注释把它们划分成 **Phase 0 ~ Phase 4** 五个阶段（源码注释里的 Phase 编号从 0 开始）。五个阶段的设计意图可以用一句话概括：

> **先补全信息（Phase 0），再做高层算子级融合（Phase 1），再把 Relax 降级到 TIR（Phase 2），再做 TIR 级融合与清理（Phase 3），最后做底层调度、内存规划与 VM/CUDA graph 降级（Phase 4）。**

之所以要分阶段、按这个顺序，是因为每一步都依赖前一步建立的"形态"：

- 融合（Phase 1/3）必须在降级（Phase 2）前后各做一次：高层融合处理算子级模式，TIR 融合处理已展开成循环的 kernel。
- 内存规划（`StaticPlanBlockMemory`）必须在算子融合/降级基本完成后做，否则规划的 buffer 会被后续改写推翻。
- CUDA graph 改写（`RewriteCUDAGraph`）必须在内存规划之后做，因为它要把一段计算"拍照"成一个可重放的整体。

#### 4.2.2 核心流程

下面用伪流程描述一条 `IRModule` 在流水线里的旅程（方括号里是对应的 dump 文件名，由 `--debug-dump` 触发）：

```
IRModule (导出后)
   │
   ▼  Phase 0：附加元数据 / 运行时函数 / 绑定 target      [debug-phase0.py]
   │  - 派发 KV cache 创建函数、附加采样/logit/softmax/
   │    推测解码辅助/embedding 分配等运行时函数
   │  - 附加变量上下界、CUDA graph 捕获提示、流水线并行 stage 数
   │  - BindTarget：把 target 信息写进 TIR
   │
   ▼  Phase 1：高层算子图优化（融合/派发）                [debug-phase1.py]
   │  - Triton kernel 派发、FT 反量化 epilogue 融合
   │  - 反量化+转置融合、(可选) cuBLAS/hipBLAS 派发
   │  - add+RMSNorm 融合、transpose+matmul 融合
   │
   ▼  Phase 2：Relax → TIR 降级（TVM 官方 zero 流水线）   [debug-phase2.py]
   │  - DispatchSampling / DispatchSortScan
   │  - LegalizeOps / AnnotateTIROpPattern
   │  - FoldConstant / FuseOps / FuseTIR
   │
   ▼  Phase 3：TIR 级优化                                [debug-phase3.py]
   │  - 反量化+matmul+ewise 融合、反量化+take 融合
   │  - DeadCodeElimination、清理 TIR 属性
   │
   ▼  Phase 4：底层优化 + VM 字节码降级                   [debug-phase4.py / debug-phase5.py]
      - 低 batch GEMV 特化、DLight 默认调度
      - 整型收窄、scatter tuple、流水线并行改写
      - 一系列 Relax→VM 的最终降级（CallTIRRewrite、内存规划等）
      - CUDA graph 改写、IPC storage 降级、挂全局符号
```

> 关于"五阶段"的诚实说明：源码注释把阶段标为 **Phase 0–4**（共 5 个带编号的阶段）。其中 Phase 4 在文件里其实跨了两段——先是"Low-level Optimizations"（dump 为 `debug-phase4.py`），紧接着是"Lowering to VM bytecode"（dump 为 `debug-phase5.py`）。本讲按学习目标把它们合并视为第 5 个大阶段"底层优化与 CUDA graph/内存规划"，因为它们都属于"降级到底层、不再有高层算子融合"的范畴。

#### 4.2.3 源码精读

**Phase 0：附加元数据与运行时函数**

[python/mlc_llm/compiler_pass/pipeline.py:L106-L120](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py#L106-L120) —— Phase 0。这一阶段几乎不改变主计算图的结构，而是把"运行期需要的辅助函数和属性"**贴**到 `IRModule` 上：派发 KV cache 创建函数、附加 softmax/logit 处理/GPU 采样/推测解码辅助/embedding 分配等 PrimFunc，并附加变量上下界、CUDA graph 捕获提示、流水线并行 stage 数等属性，最后用 `BindTarget` 把 target 绑定到 TIR。

代表性的"附加"pass 长什么样——`AttachVariableBounds` 给每个 Relax 函数贴上 `tir_var_upper_bound` 属性，帮助后续内存规划：

[python/mlc_llm/compiler_pass/attach_support_info.py:L12-L28](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_support_info.py#L12-L28) —— 遍历所有 Relax 函数，挂上变量上界属性，专门兼容 RWKV 里 `max_seq_len=-1` 的情况。

**Phase 1：高层算子图优化（融合 + 派发）**

[python/mlc_llm/compiler_pass/pipeline.py:L121-L133](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py#L121-L133) —— Phase 1。在 Relax 高层图上做模式匹配与融合：`DispatchTritonKernel`（把通用 Triton 调用替换为具体 kernel）、`FuseFTDequantizeEpilogue`、`FuseDequantizeTranspose`、可选的 `BLASDispatch`、`FuseAddRMSNorm`、`FuseTransposeMatmul`。

融合类 pass 的典型写法（用 `PyExprMutator` 改写图）——`FuseAddRMSNorm` 匹配 `rms_norm(add(x1,x2),w)` 模式并替换为一个融合 kernel：

[python/mlc_llm/compiler_pass/fuse_add_norm.py:L149-L165](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_add_norm.py#L149-L165) —— `FuseAddRMSNorm` 的 pass 类定义，`transform_module` 把改写工作委托给一个 `PyExprMutator`。（算法细节见 u8-l1。）

**Phase 2：Relax → TIR 降级（继承 TVM 官方 zero 流水线）**

[python/mlc_llm/compiler_pass/pipeline.py:L134-L143](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py#L134-L143) —— Phase 2。这一段几乎完全是 TVM Relax 官方的标准降级流水线：`DispatchSampling`、`DispatchSortScan`、`LegalizeOps`、`AnnotateTIROpPattern`、`FoldConstant`、`FuseOps`、`FuseTIR`。注释明确写"inherited TVM Relax's official 'zero' pipeline"。

**Phase 3：TIR 级优化**

[python/mlc_llm/compiler_pass/pipeline.py:L144-L150](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py#L144-L150) —— Phase 3。降到 TIR 后再做一次融合（这次是在 TIR 层面）：`FuseDequantizeMatmulEwise`、`FuseDequantizeTake`，然后 `DeadCodeElimination` 删死代码，`CleanUpTIRAttrs(["op_pattern"])` 清掉只用一次的属性。

**Phase 4：底层优化 + VM 字节码降级**

[python/mlc_llm/compiler_pass/pipeline.py:L151-L167](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py#L151-L167) —— Phase 4 前半段（底层优化）。`LowBatchGemvSpecialize` 为小 batch 生成 GEMV 特化版本，再用 DLight 的 `ApplyDefaultSchedule` 套默认 GPU 调度（非 llvm 时用一整套 GPU 调度，llvm 时只用 `cpu.GEMV`）。

[python/mlc_llm/compiler_pass/pipeline.py:L168-L204](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py#L168-L204) —— Phase 4 后半段（降级到 VM 字节码）。包含 `LiftTIRGlobalBufferAlloc`、`ForceNarrowIndexToInt32`、`ScatterTupleGetItem`、`PipelineParallelRewrite`、一系列 Relax→VM 的最终降级（`RewriteDataflowReshape`/`ToNonDataflow`/`RemovePurityChecking`/`CallTIRRewrite`/`StaticPlanBlockMemory`/`AttachMetadataWithMemoryUsage`）、`RewriteCUDAGraph`+`AttachCUDAGraphAllocInitFunc`、`LowerGPUIPCAllocStorage`、`LowerAllocTensor`、`KillAfterLastUse`、`LowerRuntimeBuiltin`、`VMShapeLower`、`AttachGlobalSymbol`，最后 `AttachExternModules(ext_mods)` 挂上外部模块（如 cuBLAS/Triton）。

其中两处"可观测产物"很关键：

- `AttachMetadataWithMemoryUsage` 把 `memory_usage` 估算结果连同模型元数据一起写进一个名为 `_metadata` 的 Relax 函数，运行期引擎会读它来做显存规划：

[python/mlc_llm/compiler_pass/estimate_memory_usage.py:L16-L36](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/estimate_memory_usage.py#L16-L36) —— 把 `memory_usage` 估算填进 metadata，并 emit 一个返回 JSON 字符串的 `_metadata` 函数。（算法见 u8-l4。）

- 低 batch 特化 pass 通过 DLight 的 `LowBatchGEMV` 为 bucket `[2,4]` 生成特化 kernel：

[python/mlc_llm/compiler_pass/low_batch_specialization.py:L10-L45](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/low_batch_specialization.py#L10-L45) —— 对每个 TIR `PrimFunc`，用 `dl.gpu.LowBatchGEMV(bucket)` 尝试生成低 batch 特化版本。（原理见 u8-l4。）

#### 4.2.4 代码实践

**实践目标**：用 `--debug-dump` 把每个阶段的 `IRModule` 落盘，亲眼看到五个阶段对图做了什么。

**操作步骤**：

1. 选一个小模型（如 RedPajama-INCITE-Chat-3B-v1）并已完成 `gen_config`。
2. 运行编译时加上 `--debug-dump ./dump`：
   ```bash
   mlc_llm compile ./dist/RedPajama-INCITE-Chat-3B-v1-q4f16_1-MLC/mlc-chat-config.json \
       --device cuda --debug-dump ./dump
   ```
3. 进入 `./dump` 目录，按顺序打开 `debug-phase0.py` ~ `debug-phase5.py`。
4. 对比 `debug-phase1.py`（融合前的高层图）与 `debug-phase3.py`（TIR 融合后），观察 `rms_norm(add(...))` 这类模式是否消失、是否被替换为单个 `call_tir`。

**需要观察的现象**：

- `debug-phase0.py` 里能看到大量被附加的辅助函数（采样、softmax、推测解码辅助等）与属性。
- 从 `debug-phase2.py` 开始，Relax 算子被降级为 TIR `PrimFunc`（出现 `@T.prim_func`）。
- `debug-phase5.py` 里出现 `AttachGlobalSymbol` 后的全局符号与 VM 相关结构。

**预期结果**：你能用一句话说出每个 dump 文件对应"哪一阶段之后"的 IR 形态。如果本地没有 GPU/无法编译，这条实践标注为**待本地验证**；此时可退化为纯阅读实践——直接对照本讲的阶段说明，在 `pipeline.py` 的 `Sequential` 里把每个 pass 与五个 `_DebugDump` 文件名对上号。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `StaticPlanBlockMemory`（内存规划）放在 Phase 4，而不是 Phase 1？

**参考答案**：因为内存规划需要基于**最终形态的算子结构**来计算每个中间 buffer 的生命周期与复用。如果它在 Phase 1 做，后续的融合、降级、CUDA graph 改写都会改变算子结构，导致规划结果失效。所以它必须等融合与降级基本完成（Phase 4 后半段）才执行。

**练习 2**：Phase 2 的注释说"inherited TVM Relax's official 'zero' pipeline"，这句话暗示了什么？

**参考答案**：暗示 Phase 2 里这一串 pass（`LegalizeOps`/`FuseOps`/`FuseTIR` 等）不是 MLC LLM 自己写的，而是直接复用了 TVM Relax 官方提供的标准降级流水线（名为 "zero"）。MLC LLM 的特色工作集中在 Phase 0/1/3/4，Phase 2 基本是"借力"官方基础设施。

**练习 3**：`_DebugDump` 在流水线里出现 6 次（phase0~phase5），它本身会改变 `IRModule` 吗？

**参考答案**：不会。`_DebugDump` 是一个"哑 pass"（dummy pass），它的 `transform_module` 只在 `debug_dump` 路径非空时把当前 `IRModule` 写到文件，然后原样返回 `mod`。它的唯一作用是给开发者一个"在流水线的某个点拍快照"的钩子。

---

### 4.3 Pass 的统一写法与"GPU 条件化"

#### 4.3.1 概念说明

流水线里总共有二十多个 pass，但它们的"外形"高度一致，理解了写法约定，你就能快速读懂任何一个 pass。此外，流水线里有相当一部分 pass **只在特定 target（尤其是 GPU）下生效**，识别这些条件化是看懂流水线的关键。

MLC LLM 的 pass 有两个约定：

1. **统一外形**：每个 pass 都是一个被 `@tvm.transform.module_pass(opt_level=0, name="...")` 装饰的类，实现 `transform_module(self, mod, ctx) -> IRModule`。"做什么"由类名和 `name` 说明，"怎么做"通常委托给一个 `PyExprMutator`（改写图）或 `PyExprVisitor`（只读分析）。
2. **条件化的两种写法**：
   - **外层条件**：在 `Sequential` 列表里用 Python 三元表达式 `X if cond else tvm.transform.Sequential([])`，让整个 pass 在不满足条件时变成空 pass。
   - **内层条件**：pass 内部读 `target.kind.name` 自己决定是否提早 `return mod`（什么都不改）。

#### 4.3.2 核心流程

识别一个 pass 是否"GPU 专用"，按下面顺序排查：

1. 看 `pipeline.py` 里它是否被包在 `... if <cond> else tvm.transform.Sequential([])` 中——若是，看 `<cond>`。
2. 若没有外层条件，打开该 pass 的源码，看 `transform_module` 开头是否有 `if target.kind.name not in [...]: return mod` 这类早退。
3. 看 `OptimizationFlags.update`（compiler_flags.py）里对应的开关是否被 target 限定——例如 `_cublas_gemm` 要求 `cuda`/`rocm` 且量化格式匹配。

#### 4.3.3 源码精读

**约定 1：统一外形 + 两个"哑 pass"**

`_LogProgress` 和 `_DebugDump` 是最简单的 pass，用来理解外形最合适：

[python/mlc_llm/compiler_pass/pipeline.py:L48-L58](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py#L48-L58) —— `_LogProgress`：被 `@module_pass` 装饰，`transform_module` 只记一条日志，原样返回 `mod`。

[python/mlc_llm/compiler_pass/pipeline.py:L61-L78](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py#L61-L78) —— `_DebugDump`：在 `debug_dump` 非空时把 `mod.script()` 写到文件，原样返回 `mod`。

所有"真"pass 都遵循同样的外形，只是 `transform_module` 内部做实事。

**约定 2：外层条件（Python 三元 → 空 pass）**

Phase 1 的 `BLASDispatch` 与 `FuseAddRMSNorm` 都是外层条件化的例子：

[python/mlc_llm/compiler_pass/pipeline.py:L126-L131](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py#L126-L131) —— `BLASDispatch` 仅当 `cublas_gemm` 为真时执行；`FuseAddRMSNorm` 仅当 `target.kind.name != "llvm"` 时执行，否则用空 `Sequential([])` 占位。

注意 `BLASDispatch` 的构造函数本身也强制 target：

[python/mlc_llm/compiler_pass/blas_dispatch.py:L19-L31](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/blas_dispatch.py#L19-L31) —— 构造时若 target 既不是 `cuda` 也不是 `rocm`，直接抛异常；这正是它必须被外层 `if cublas_gemm` 守护的原因（而 `cublas_gemm` 又被 `OptimizationFlags.update` 限定为 cuda/rocm）。

Phase 4 后半段还有三处外层条件：

[python/mlc_llm/compiler_pass/pipeline.py:L169-L178](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py#L169-L178) —— `LiftTIRGlobalBufferAlloc` 仅非 llvm 时执行；`ForceNarrowIndexToInt32` 仅当 target 不是 `cuda` 时执行（CUDA 保持 64 位索引）。

**约定 3：内层条件（pass 内部读 target 早退）**

`AttachGPUSamplingFunc` 是典型——它没有外层 `if`，而是构造时存 target、在 `transform_module` 开头判断：

[python/mlc_llm/compiler_pass/attach_sampler.py:L29-L34](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_sampler.py#L29-L34) —— 若 target 不是 `cuda`/`vulkan`/`metal`/`webgpu`，直接 `return mod`（不附加任何 GPU 采样函数）。

更细粒度的内层条件——`AttachSequenceLengthPaddingFactor` 只对 CUDA SM100a（Blackwell）做序列长度 padding：

[python/mlc_llm/compiler_pass/attach_support_info.py:L125-L136](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/attach_support_info.py#L125-L136) —— 仅当 `target.kind.name == "cuda"` 且 `arch == "sm_100a"` 且使用了 CUTLASS groupwise gemm 时，才把 padding factor 设为 4。

**开关源头：`OptimizationFlags` 与 `OPT_FLAG_PRESET`**

外层条件的开关（`cublas_gemm`/`flashinfer`/`cudagraph`/`cutlass`/`faster_transformer`/`ipc_allreduce_strategy`）都来自 `OptimizationFlags`，并按 target 矫正：

[python/mlc_llm/interface/compiler_flags.py:L84-L137](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compiler_flags.py#L84-L137) —— `OptimizationFlags.update`：`flashinfer` 仅 CUDA≥80、`cublas_gemm` 仅 cuda/rocm 且量化匹配、`faster_transformer`/`cutlass`/`cudagraph` 仅 cuda。

[python/mlc_llm/interface/compiler_flags.py:L198-L227](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compiler_flags.py#L198-L227) —— `OPT_FLAG_PRESET`：`--opt O0/O1/O2/O3` 对应不同开关组合，例如 O2/O3 才默认开 `flashinfer`+`cudagraph`。

#### 4.3.4 代码实践

**实践目标**：识别流水线里所有"GPU/特定 target 专用"的 pass，并说明其条件来源。

**操作步骤**：

1. 在 `pipeline.py` 的 `Sequential` 中，找出所有形如 `X if cond else tvm.transform.Sequential([])` 的外层条件 pass。
2. 对于没有外层条件的 pass，抽查 `attach_sampler.py`、`attach_support_info.py`、`dispatch_kv_cache_creation.py`、`dispatch_triton_kernel.py`，看它们是否在内部用 `target.kind.name` 早退。
3. 对每个 GPU 专用 pass，追溯它的开关最终来自 `OptimizationFlags` 的哪个字段、以及 `update` 方法里对应的 `_xxx(target)` 函数。

**需要观察的现象**：你会发现"GPU 专用 pass"的条件分布在三处——`pipeline.py` 外层三元、pass 内部早退、`OptimizationFlags.update`。

**预期结果**：你能列出一张表（见下方综合实践），准确标注每个条件 pass 的生效条件。这是本讲最重要的可迁移技能。

#### 4.3.5 小练习与答案

**练习 1**：`ForceNarrowIndexToInt32` 的条件是 `target.kind.name != "cuda"`，这意味着什么？

**参考答案**：CUDA target **不**做索引收窄到 int32（保持 64 位索引），而其他 target（如 vulkan/metal/webgpu/llvm）会把索引收窄到 int32。原因是不同后端对 64 位整数的支持程度不同；这是"target 决定 pass 行为"的典型例子。

**练习 2**：为什么 `BLASDispatch` 既需要外层 `if cublas_gemm`，又需要在构造函数里检查 target？

**参考答案**：双重保险。外层 `if cublas_gemm` 防止在用户没开该开关时构造它；构造函数里的 target 检查防止"开关开了但 target 不支持"（比如在 vulkan 上开了 `cublas_gemm`）时静默错误——它选择抛异常把问题前置暴露。而 `OptimizationFlags.update` 进一步在更早的阶段就把 `cublas_gemm` 在非 cuda/rocm target 下矫正为 `False`，三层共同保证只有 cuda/rocm 才真正派发 BLAS。

---

## 5. 综合实践

把本讲的三块知识（五阶段、各阶段目标、GPU 条件化）串成一张完整的 pass 分类表。请打开 [python/mlc_llm/compiler_pass/pipeline.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py)，按下表把 `Sequential` 里的每个 pass 归类，并填写"生效条件"列。

| 分组 | 阶段 | 包含的 pass（示例） | 生效条件（GPU 专用？） |
| --- | --- | --- | --- |
| 附加（attach） | Phase 0 | `DispatchKVCacheCreation`, `AttachSoftmaxWithTemperature`, `AttachGPUSamplingFunc`, ... | `AttachGPUSamplingFunc` 仅 cuda/vulkan/metal/webgpu；其余见各自源码 |
| 融合（fuse） | Phase 1 / Phase 3 | `FuseAddRMSNorm`, `FuseTransposeMatmul`, `FuseDequantizeMatmulEwise`, ... | `FuseAddRMSNorm` 仅非 llvm |
| 派发（dispatch） | Phase 1 | `DispatchTritonKernel`, `BLASDispatch` | `BLASDispatch` 仅 cuda/rocm 且 `cublas_gemm` |
| 降级（lowering） | Phase 2 / Phase 4 后半 | `LegalizeOps`, `FuseOps`, `CallTIRRewrite`, `LowerRuntimeBuiltin`, ... | 多为无条件（官方 zero 流水线） |
| TIR 优化 | Phase 3 / Phase 4 前半 | `FuseDequantizeTake`, `LowBatchGemvSpecialize`, `CleanUpTIRAttrs` | `LowBatchGemvSpecialize` 配合 DLight GPU 调度 |
| 底层（low-level） | Phase 4 后半 | `StaticPlanBlockMemory`, `RewriteCUDAGraph`, `LiftTIRGlobalBufferAlloc`, `ForceNarrowIndexToInt32`, `AttachGlobalSymbol` | `RewriteCUDAGraph` 由 `use_cuda_graph`(cuda) 控制；`LiftTIRGlobalBufferAlloc` 非llvm；`ForceNarrowIndexToInt32` 非cuda |

**任务要求**：

1. 把上表"包含的 pass"列补全为 `Sequential` 里的**全部** pass（不要遗漏 `_LogProgress`/`_DebugDump` 这类辅助 pass，但可在备注里标注它们是"哑 pass"）。
2. 对每个标了"GPU 专用"或带条件的 pass，写出它的条件来源属于哪一层（外层三元 / pass 内部早退 / `OptimizationFlags.update`）。
3. 用一句话回答：如果 target 是纯 CPU（`llvm`），Phase 1 与 Phase 4 里有哪些 pass 会变成空操作或被跳过？

**参考答案要点**（用于自查）：

- CPU(llvm) 下被跳过/置空的包括：`FuseAddRMSNorm`（外层 `!=llvm` 不成立→空）、`LiftTIRGlobalBufferAlloc`（非 llvm 才执行→跳过）、DLight 改用 `cpu.GEMV` 而非整套 GPU 调度、`AttachGPUSamplingFunc`（target 不在白名单→内部早退）、`BLASDispatch`（`cublas_gemm` 在 `_cublas_gemm` 里被矫正为 False→外层置空）、`RewriteCUDAGraph`（`cudagraph` 仅 cuda→`use_cuda_graph` 关）。
- 这正解释了为什么同一个模型编译到 CPU 和 GPU 用的 pass 子集不同：**流水线是"按 target 自适应"的**。

> 这是一项"源码阅读型实践"，不需要运行模型即可完成；若要验证你的归类，可用本讲 4.2.4 的 `--debug-dump` 在两种 target 下各跑一次，对比 dump 文件差异。

## 6. 本讲小结

- MLC LLM 的编译流水线用 `@register_pipeline("mlc_llm")` 注册，由 `compile.py` 通过 `relax.get_pipeline("mlc_llm", ...)` 在 `PassContext` 中装配并驱动。
- 流水线分为 **Phase 0–4** 五个阶段：附加元数据/运行时函数 → 高层算子图融合/派发 → Relax 降级到 TIR → TIR 级融合/清理 → 底层调度/内存规划/VM 与 CUDA graph 降级。
- 每个阶段都有明确目标，顺序不可随意调换：融合要在降级前后各做一次，内存规划与 CUDA graph 改写必须放在最后。
- 所有 pass 遵循统一外形（`@tvm.transform.module_pass` + `transform_module`），并用"外层三元 / 内部早退 / `OptimizationFlags.update`"三层机制实现 target 自适应。
- `_LogProgress` 与 `_DebugDump` 是不改图的"哑 pass"，后者配合 `--debug-dump` 可逐阶段导出 IR 快照，是理解流水线最直接的工具。
- 装配期参数（进 `get_pipeline`）与运行期开关（进 `PassContext`）是两条不同的配置通道，前者塑形流水线、后者控制内置 pass 行为。

## 7. 下一步学习建议

本讲只画了"地图"，没有展开任何单个 pass 的算法。建议接下来按 U8 的顺序深入各阶段内部：

- **u8-l1 算子融合 pass**：深入 `FuseAddRMSNorm`、`FuseDequantizeMatmulEwise`、`FuseTransposeMatmul` 的模式匹配与改写机制（本讲 Phase 1/3 的主角）。
- **u8-l2 后端派发 pass**：深入 `BLASDispatch`、`DispatchTritonKernel`、`DispatchKVCacheCreation` 如何把通用算子换成后端专用实现（本讲 Phase 0/1 的派发类）。
- **u8-l3 运行时函数附加 pass**：深入 `AttachGPUSamplingFunc`、`AttachLogitProcessFunc`、`AttachSpecDecodeAuxFuncs` 为何要在编译期生成运行期函数（本讲 Phase 0 的附加类）。
- **u8-l4 低 batch 特化、内存估算与流水线并行**：深入 `LowBatchGemvSpecialize`、`AttachMetadataWithMemoryUsage`、`PipelineParallelRewrite`（本讲 Phase 4 的底层类）。

阅读这些讲义时，建议随时回到本讲的五阶段表，把每个深入讲解的 pass "钉"回它所在的阶段位置，保持全局视野。
