# 编译总流程：DSL → TIR → 后端

## 1. 本讲目标

在前面的讲义里，你已经会用 `@tilelang.jit` 写出一个 GEMM kernel 并把它跑起来（u1-l4），也理解了 `T.gemm` 这类 tile op 是「DSL 留占位、后按硬件展开」的机制（u3-l1）。但当你调用 `.compile()` 时，**那段 Python 写的 DSL 到底是怎么变成一段可执行的 CUDA/HIP 源码的**？中间经历了哪些阶段、谁来决定执行顺序？

本讲把镜头拉到「编译器」这一层，目标是让你：

1. 理解 `tilelang.lower()` 作为编译器总入口，如何把一个 `PrimFunc` 经 Pass 流水线变成「可编译的 IR」。
2. 掌握 `device_codegen` / `host_codegen` 的**注册表 + 惰性加载**机制，以及它们如何被 `target` 解析出来。
3. 能在源码中精确定位三个关键枢纽：`pass_pipeline.resolve_pipeline`（按 target 选 Pass 序列）、`device_codegen.resolve_device_codegen`（按 target 选代码生成器）、以及目标判定 `determine_target`（把 `"auto"`/字符串/dict 解析成 TVM `Target`）。

学完本讲，你应能拿一个 kernel，对着 verbose 日志和 dump 出来的 IR，说清楚「现在 IR 走到了流水线的哪一步」。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，什么是 IRModule？** TVM 用一棵抽象语法树（AST）来表示程序，这棵树的语言叫 TIR（Tensor IR）。一个 `PrimFunc` 是 TIR 里的一个函数节点；一个 `IRModule` 就是一组带名字的 `PrimFunc` 的容器（`{global_symbol: PrimFunc}`）。tilelang 的编译过程，本质上就是**不断改写这个 IRModule**：每经过一个 Pass，IRModule 就被替换成一个「更底层、更接近硬件」的新 IRModule，直到它低到能直接翻译成 CUDA/HIP 文本。

**第二，什么是 Pass？** Pass 是「输入一个 IRModule、输出一个 IRModule」的变换函数。例如 `Simplify` 化简表达式、`LayoutInference` 推导 fragment 布局、`LowerTileOp` 把 `T.gemm` 占位节点展开成真实指令。一个后端（如 CUDA）的「Pass 流水线」就是一串按固定顺序排好的 Pass。

**第三，host 和 device 是分开的。** GPU 程序天然分两部分：运行在 CPU 上的 host 代码（负责分配显存、启动 kernel、同步）和运行在 GPU 上的 device 代码（kernel 本身）。tilelang 在编译中途会用 `SplitHostDevice` 把一个 IRModule 拆成 `host_mod` 与 `device_mod` 两份，分别走不同的代码生成器。理解这一点，才能看懂 `lower()` 最后为什么返回两个模块。

> 关键术语速查：`PrimFunc`（TIR 函数）、`IRModule`（函数容器）、`Pass`（IR→IR 变换）、`Target`（目标硬件描述）、`PassContext`（Pass 运行时的配置容器）、host/device 拆分。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/engine/lower.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py) | **编译器总入口**。定义 `lower()`、`lower_to_host_device_ir()`、`device_codegen()`、`host_codegen()`，串起整条链路 |
| [tilelang/engine/\_\_init\_\_.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/__init__.py) | engine 包的导出面，对外暴露 `lower` |
| [tilelang/backend/target.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/target.py) | 目标判定。`determine_target()` 把 `"auto"`/字符串/dict 解析为 TVM `Target` |
| [tilelang/backend/pass_pipeline/pipeline.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/pass_pipeline/pipeline.py) | Pass 流水线注册表。`PassPipeline` 类 + `resolve_pipeline()` |
| [tilelang/cuda/pipeline.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/pipeline.py) | CUDA 后端的真实 Pass 序列（约 50 个 Pass 的具体顺序） |
| [tilelang/backend/pass_pipeline/pipeline_utils.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/pass_pipeline/pipeline_utils.py) | 读取 PassContext 配置、决定某些 Pass 是否启用的辅助函数 |
| [tilelang/backend/device_codegen.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/device_codegen.py) | 设备代码生成注册表。`DeviceCodegen` + `resolve_device_codegen()` |
| [tilelang/cuda/codegen.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/codegen.py) | CUDA/CuTeDSL 两条设备代码生成路径的注册点 |
| [tilelang/backend/host_codegen.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/host_codegen.py) | 主机代码生成注册表（结构同 device_codegen） |
| [tilelang/engine/param.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/param.py) | `KernelParam` 与 `CompiledArtifact`：编译产物的数据结构 |
| [tilelang/engine/semantic_check.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/semantic_check.py) | lowering 前的语义检查 `PreLowerSemanticCheck` |

## 4. 核心概念与源码讲解

### 4.1 tilelang.engine：编译入口与 IRModule 流转

#### 4.1.1 概念说明

`tilelang.engine` 是整个编译器的「调度中枢」。它对外只暴露一个核心函数 `lower()`，对内把编译过程组织成一条清晰的流水线：

```
PrimFunc (你写的 DSL)
   │
   │  lower_to_host_device_ir()
   │  ┌─ determine_target()        解析 target
   │  ├─ PreLowerSemanticCheck()   lowering 前语义检查
   │  ├─ resolve_pipeline(target)  按 target 选 Pass 序列
   │  ├─ pipeline.lower(mod)       跑完整 Pass 流水线
   │  └─ Filter(host/device)       拆成 host_mod / device_mod
   ▼
(host_mod, device_mod)
   │
   │  device_codegen(device_mod)   生成设备源码 → kernel_source
   │  host_codegen(host_mod)       （可选）生成 host 代码
   ▼
CompiledArtifact
```

这条链路的关键设计是：**`lower()` 本身不写死任何后端逻辑**。它只负责「按 target 去查表」，把具体的 Pass 序列、代码生成器都委托给注册表。这就是为什么 tilelang 能同时支持 cuda/hip/metal/cpu/webgpu/cutedsl——每个后端各自注册自己的 pipeline 和 codegen，`lower()` 只做调度。

#### 4.1.2 核心流程

`lower()` 的真正主体是 `lower_to_host_device_ir()`。它的执行步骤是：

1. **封装 IRModule**：如果输入是单个 `PrimFunc`，用 `extrac_params()` 抽取参数列表（供后续 adapter 使用），并包成 `IRModule({global_symbol: func})`。
2. **解析 target**：若 `target` 是字符串（如 `"auto"`、`"cuda"`），调用 `determine_target(target)` 转成 TVM `Target` 对象（详见 4.2）。
3. **规范 host target**：`canon_target_host()` 决定 host 用 `llvm` 还是 `c`。
4. **语义检查**：`PreLowerSemanticCheck(mod)` 在任何 lowering 之前做后端无关的合法性校验。
5. **跑 Pass 流水线**：`resolve_pipeline(target)` 取得该后端的 `PassPipeline`，`pipeline.lower(mod, target)` 把整串 Pass 跑完。
6. **拆分 host/device**：用 `tirx.transform.Filter(...)` 把模块里的函数按 `calling_conv` 分成 host 与 device 两份。

`lower()` 拿到这两份模块后，对 `device_mod` 调用 `device_codegen()` 得到 `kernel_source`（生成的 CUDA/HIP 文本），最终打包成 `CompiledArtifact` 返回。

#### 4.1.3 源码精读

先看总入口 `lower()`。它先调 `lower_to_host_device_ir` 拿到拆分后的两份模块，再决定是否真的「编译设备码」：

[tilelang/engine/lower.py:297-342](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L297-L342) —— `lower()` 的全部逻辑：注意两个开关 `enable_host_codegen` 与 `enable_device_compile` 默认都是 `False`，因为 JIT 层有自己的 host/device 实现策略（见注释「we have our own host/device codegen implementation in jit」）。

真正干活的是 `lower_to_host_device_ir()`，这是本讲最重要的一段代码：

[tilelang/engine/lower.py:259-294](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L259-L294) —— 串起「封装模块 → 解析 target → 语义检查 → 跑 pipeline → 拆分 host/device」五步。第 286 行的 `PreLowerSemanticCheck(mod)` 在 pipeline 之前；第 288-289 行的 `resolve_pipeline` + `pipeline.lower` 是 Pass 流水线的实际执行点；第 291-292 行用 `Filter` 拆分。

`extrac_params()` 遍历函数参数，区分张量（在 `buffer_map` 里）和标量，生成 `KernelParam` 列表：

[tilelang/engine/lower.py:198-205](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L198-L205)

host/device 的判定靠 `calling_conv`（调用约定）。`is_device_call` 检查函数是否带 `DEVICE_KERNEL_LAUNCH` 标记；CPU `'c'` 后端另有 `C_PACKED_FUNC` 的特殊处理：

[tilelang/engine/lower.py:28-54](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L28-L54) —— 这组小函数决定一个 `PrimFunc` 属于 host 还是 device，正是第 291-292 行 `Filter` 使用的判别谓词。

最后，`device_codegen()` 与 `host_codegen()` 把模块交给对应代码生成器（详见 4.4）：

[tilelang/engine/lower.py:249-256](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L249-L256) —— `device_codegen` 走「编译」路径，`device_codegen_without_compile` 只生成源码不调用 nvcc。

> 包导出面 [tilelang/engine/\_\_init\_\_.py:1](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/__init__.py#L1) 只 re-export `lower`、`is_device_call`、`KernelParam`，即 engine 对外的全部公共符号。

#### 4.1.4 代码实践

**实践目标**：直接调用 `tilelang.lower()`，绕开 JIT 装饰器，亲手观察编译产物 `CompiledArtifact` 的内部结构。

**操作步骤**：

1. 把 `examples/quickstart.py` 里的 `matmul` 函数体复制出来，先用 `@T.prim_func`（或仍用 `@tilelang.jit` 后取 `.func`）拿到一个 `PrimFunc` 对象。示例代码（非项目原有，仅为说明调用方式）：

   ```python
   import tilelang
   import tilelang.language as T

   @tilelang.jit
   def matmul(A, B, block_M, block_N, block_K):
       M, N, K = T.const("M, N, K")
       A: T.Tensor((M, K), T.float16)
       B: T.Tensor((K, N), T.float16)
       C = T.empty((M, N), T.float16)
       with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
           A_shared = T.alloc_shared((block_M, block_K), T.float16)
           B_shared = T.alloc_shared((block_K, block_N), T.float16)
           C_local = T.alloc_fragment((block_M, block_N), T.float32)
           T.clear(C_local)
           for ko in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
               T.copy(A[by * block_M, ko * block_K], A_shared)
               T.copy(B[ko * block_K, bx * block_N], B_shared)
               T.gemm(A_shared, B_shared, C_local)
           T.copy(C_local, C[by * block_M, bx * block_N])
       return C

   # 拿到 PrimFunc
   func = matmul.func(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32)
   # 直接走编译入口（不启用 device compile，只生成源码）
   artifact = tilelang.lower(func, target="cuda")
   ```

2. 打印 `artifact` 的字段：`type(artifact)`、`artifact.params`、`len(artifact.params)`、`artifact.kernel_source[:200]`。

**需要观察的现象**：`artifact` 是 `CompiledArtifact` 实例；`params` 是 3 个 `KernelParam`（A、B、C），其中 C 的形状含 `M, N` 符号；`kernel_source` 是一段以 `extern "C" __global__` 开头的 CUDA 文本。

**预期结果**：能成功打印 kernel 源码片段。若机器无 GPU，`target="cuda"` 仍可生成源码（因为默认 `enable_device_compile=False`，不调 nvcc）；若仍报错，可改 `target="c"` 观察 CPU 后端产物。具体字段类型与源码内容**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`lower()` 默认 `enable_device_compile=False`，那它怎么还能返回 `kernel_source`？
**答案**：因为 `device_codegen_without_compile()` 会走 `build_without_compile` 路径——它让代码生成器产出 CUDA/HIP **文本**但不调用 nvcc 编译成 cubin（见 4.4 的 `DeviceCodegen.lower` 二选一逻辑）。真正的二进制编译由 JIT 层的 adapter 负责。

**练习 2**：为什么 `lower_to_host_device_ir` 要在跑 Pass 流水线**之前**做 `PreLowerSemanticCheck`？
**答案**：语义检查是「后端无关」的合法性校验（如嵌套循环结构、fragment 循环约束），越早报错越能给出贴近用户原始 DSL 的错误信息；放到 lowering 之后，IR 已被大幅改写，错误信息会难以理解。

**练习 3**：`extrac_params` 区分张量与标量的依据是什么？
**答案**：看函数参数 `var` 是否出现在 `func.buffer_map` 中——在的就是张量（用 `KernelParam.from_buffer`），不在的就是标量（用 `KernelParam.from_var`，形状为空）。

---

### 4.2 目标判定 determine_target

#### 4.2.1 概念说明

`determine_target` 是 `lower_to_host_device_ir` 的**第一步**（第 274-275 行）。它要把用户给的 `target`——可能是 `"auto"`、`"cuda"`、`{"kind": "cuda", "arch": "sm_90"}` 这样的 dict、或已经是 `Target` 对象——统一解析成一个合法的 TVM `Target`。

这套机制也是「注册表驱动」的：每个后端可以注册自己的**探测器**（detector，用于 `"auto"` 时按优先级探测硬件）和**归一化器**（normalizer，把各种写法转成标准形式）。这样 tilelang 不必把硬件探测逻辑写死在主流程里。

#### 4.2.2 核心流程

- 若 `target == "auto"`：先看 `Target.current()`（有没有当前上下文 target），没有就调 `auto_detect_target()`，按注册顺序依次跑探测器，第一个成功返回的结果即用。
- 否则走 `_validate_manual_target()`：先让注册的 normalizer 尝试归一化；归一化不了再按 `Target`/`dict`/`str` 三种类型分别校验（构造 `Target(...)` 不抛异常即合法）。
- 最后 `_finalize_target()` 决定是否包成 `Target` 对象返回。

#### 4.2.3 源码精读

[tilelang/backend/target.py:122-134](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/target.py#L122-L134) —— `determine_target` 的全部逻辑：第 128-130 行处理 `"auto"`，第 132 行处理显式 target。

`"auto"` 的探测循环，任一探测器抛异常会被收集进错误信息，全部失败才报错：

[tilelang/backend/target.py:66-78](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/target.py#L66-L78)

显式 target 的校验，按 `Target`/`dict`/`str` 分支处理：

[tilelang/backend/target.py:85-109](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/target.py#L85-L109)

#### 4.2.4 代码实践

**实践目标**：观察 `determine_target` 对不同输入的解析结果。

**操作步骤**：

```python
from tilelang.backend.target import determine_target
print(determine_target("cuda"))          # 字符串
print(determine_target({"kind": "cuda", "arch": "sm_90"}))  # dict
print(determine_target("cuda", return_object=True))  # 返回 Target 对象
# print(determine_target("auto"))        # 会触发硬件探测，无 GPU 可能报错
```

**需要观察的现象**：字符串和 dict 返回的是归一化后的 dict/字符串；`return_object=True` 时返回 `tvm.target.Target` 实例。

**预期结果**：合法 target 正常返回；非法 target（如 `determine_target("not_a_backend")`）抛 `AssertionError`。`"auto"` 在无 GPU 机器上的具体探测顺序与报错信息**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`determine_target("auto")` 的探测顺序由谁决定？
**答案**：由 `register_target_detector` 注册的顺序决定（`_TARGET_DETECTORS` 字典的插入顺序）。各后端在各自模块导入时注册探测器，所以顺序取决于后端模块的加载顺序。

**练习 2**：为什么 dict 形式的 target 要先 `Target(target)` 试构造一次？
**答案**：为了尽早校验合法性——如果 dict 缺少必需字段或字段值非法，`Target(...)` 会立刻抛异常，`_validate_manual_target` 把它包装成更友好的 `AssertionError`，而不是等到后面 lowering 时才报一个晦涩的错误。

---

### 4.3 tilelang.backend.pass_pipeline：Pass 流水线与 PassContext

#### 4.3.1 概念说明

`resolve_pipeline` 是 `lower_to_host_device_ir` 的**核心步骤**（第 288 行）。它按 `target.kind.name`（如 `"cuda"`、`"hip"`、`"c"`）从注册表里取出对应的 `PassPipeline`，然后 `pipeline.lower(mod, target)` 把这条 Pass 序列依次作用到 IRModule 上。

`PassPipeline` 的设计非常薄——它只是一个「名字 + 一个 lower 函数」的包装。真正的 Pass 顺序完全由各后端自己在那个 lower 函数里**硬编码**。以 CUDA 为例，这个 lower 函数（`CUDAPassPipelineBody`）串了大约 50 个 Pass，从 `BindTarget` 一路到 `PersistThreadblock`。

与 Pass 流水线配套的是 `PassContext`：它是 Pass 运行时的「配置容器」，携带 `pass_configs`（用户传入的开关，如 `tl.enable_fast_math`、`tl.disable_vectorize`）和「仪器」（instruments，如 dump IR、统计耗时）。pipeline 里的很多 Pass 会回读 PassContext 来决定自己是否启用、以什么参数运行。

#### 4.3.2 核心流程

CUDA Pass 流水线可粗分为三大段（节选关键 Pass，完整顺序见源码）：

```
【前置 prologue】把 DSL 语义展开成可布局推理的 IR
  BindTarget → MaterializeKernelLaunch → LegalizeNegativeIndex
  → InjectAssumes → Simplify → LayoutReducer
  → ProducerConsumerWarpSpecialized → LowerBlackwell2SM
  → PipelinePlanning → InjectSoftwarePipeline     ← 软件流水线（u3-l3）
  → LayoutInference → LowerTileOp                  ← 布局推理与 tile op 展开（u3-l1/u3-l4）

【主体 body】把 IR 降到接近 CUDA 文本
  → LowerSharedTmem → PlanAndUpdateBufferAllocationLocation
  → HoistGlobalBufferAllocations → LowerOpaqueBlock
  → FlattenBuffer → ConfigIndexBitwidth
  → VectorizeLoop → StorageRewrite → UnrollLoop
  → LowerThreadAllreduce                          ← 归约（u3-l2）
  → SplitHostDevice                              ← 在这里拆 host/device
  → MergeSharedMemoryAllocations → ThreadSync    ← shared 同步

【收尾】生成调用封装
  → MakePackedAPI → LowerDeviceKernelLaunch → PersistThreadblock
```

注意：`SplitHostDevice` 出现在 pipeline **内部**（而不是 `lower_to_host_device_ir` 之外）。pipeline 跑完后，模块里的函数已经各自带上了 `calling_conv` 标记，`lower_to_host_device_ir` 第 291-292 行的 `Filter` 才能把它们分成 host/device 两堆。

PassContext 的传递路径是：用户传给 `@tilelang.jit(...)` 或 `.compile(pass_configs=...)` 的字典 → `normalize_pass_configs` 归一化 → JIT 层用 `tvm.transform.PassContext(opt_level=3, config=pass_configs, ...)` 打开一个上下文 → 在这个上下文里调用 `tilelang.lower(...)` → pipeline 里的 Pass 通过 `get_pass_context()` 读到这些配置。

#### 4.3.3 源码精读

先看 `PassPipeline` 与注册表，它极其简洁：

[tilelang/backend/pass_pipeline/pipeline.py:11-23](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/pass_pipeline/pipeline.py#L11-L23) —— `PassPipeline` 只持有 `name` 和一个 `_lower` 回调。

[tilelang/backend/pass_pipeline/pipeline.py:46-48](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/pass_pipeline/pipeline.py#L46-L48) —— `resolve_pipeline` 就是 `get_pipeline(target.kind.name)`，按 target 种类名查表。

再看 CUDA 后端如何注册自己的 pipeline。`CUDAPassPipelineBody` 是真实的 Pass 序列，注意其中 `PipelinePlanning → InjectSoftwarePipeline → LayoutInference → LowerTileOp` 的关键顺序（为什么流水线规划要先于布局推理，注释里有解释）：

[tilelang/cuda/pipeline.py:68-138](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/pipeline.py#L68-L138) —— prologue 段。第 106-109 行的注释解释了「先规划流水线、后做布局推理」的原因：让布局推理直接看到最终流水线化后的结构。

[tilelang/cuda/pipeline.py:141-254](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/pipeline.py#L141-L254) —— body 段。第 213 行的 `SplitHostDevice` 是 host/device 拆分点；第 224 行的 `MergeSharedMemoryAllocations` 注释说明它必须在 `SplitHostDevice` 之后。

[tilelang/cuda/pipeline.py:257-259](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/pipeline.py#L257-L259) —— 把 `CUDAPassPipelineBody` 包成名为 `"cuda"` 的 pipeline 并注册。

pipeline 里的 Pass 如何回读 PassContext？看辅助函数 `allow_vectorize`：

[tilelang/backend/pass_pipeline/pipeline_utils.py:10-14](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/pass_pipeline/pipeline_utils.py#L10-L14) —— 它读 `tirx.disable_vectorize`，决定 `VectorizeLoop` 是否启用（对应 pipeline 第 185 行 `VectorizeLoop(enable_vectorize=allow_vectorize(...))`）。

最后看 JIT 层如何打开 PassContext 并调用 `lower`：

[tilelang/jit/kernel.py:268-283](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L268-L283) —— 注意这个 `with` 块叠了四层上下文：耗时上报、`jit_phase("lower", verbose=...)`、`tvm.transform.PassContext(opt_level=3, config=pass_configs, instruments=...)`、以及 `self.target`。`tilelang.lower(...)` 就在最内层执行，从而能通过 `Target.current()` 和 `get_pass_context()` 拿到 target 与配置。

#### 4.3.4 代码实践

**实践目标**：开启 IR dump 与 JIT 诊断，跟踪一个 GEMM 编译时 Pass 的执行顺序与各阶段 IR。

**操作步骤**：

1. 准备一段最小脚本（基于 `examples/quickstart.py` 的 `matmul`），编译时传入 dump IR 配置：

   ```python
   import tilelang
   import tilelang.language as T

   @tilelang.jit
   def matmul(A, B, block_M, block_N, block_K):
       M, N, K = T.const("M, N, K")
       A: T.Tensor((M, K), T.float16)
       B: T.Tensor((K, N), T.float16)
       C = T.empty((M, N), T.float16)
       with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
           A_shared = T.alloc_shared((block_M, block_K), T.float16)
           B_shared = T.alloc_shared((block_K, block_N), T.float16)
           C_local = T.alloc_fragment((block_M, block_N), T.float32)
           T.clear(C_local)
           for ko in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
               T.copy(A[by * block_M, ko * block_K], A_shared)
               T.copy(B[ko * block_K, bx * block_N], B_shared)
               T.gemm(A_shared, B_shared, C_local)
           T.copy(C_local, C[by * block_M, bx * block_N])
       return C

   kernel = matmul.compile(
       M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32,
       pass_configs={"tl.enable_dump_ir": True, "tl.dump_ir_path": "./dump_ir"},
   )
   ```

2. 运行环境设为 `TILELANG_JIT_DIAGNOSTICS=1`（或对等价的 verbose 开关置真），以便在日志里看到 `TileLang JIT phase start: lower ...` 这样的阶段标记。
3. 运行后查看 `./dump_ir/` 目录。

**需要观察的现象**：
- 控制台出现 `TileLang JIT phase start: lower` 与 `done: lower elapsed=...` 的诊断行。
- `./dump_ir/` 下生成一系列 IR 文件，**文件名按 Pass 名编号**，能从中读出 Pass 的执行顺序。
- 在序列中找到 `LayoutInference`、`LowerTileOp`、`SplitHostDevice`、`InjectSoftwarePipeline` 出现的位置。

**预期结果**：dump 目录里 Pass 文件的数量与顺序应与 `CUDAPassPipelineBody`（4.3.2 的流程图）大体对应；`LowerTileOp` 之后的 IR 里 `tl.tileop.gemm` 占位节点应消失，被展开成底层调用。具体文件命名规则与文件总数**待本地验证**（依赖 TVM `DumpIR` 仪器实现）。

**对照阅读**：把 dump 出的某个 Pass 前后两份 IR 与 `tilelang/cuda/pipeline.py` 里的注释对照，确认 `PipelinePlanning`→`InjectSoftwarePipeline`→`LayoutInference` 的顺序，并理解「为什么流水线规划要先于布局推理」。

#### 4.3.5 小练习与答案

**练习 1**：如果想让某个 kernel **不**做循环向量化，该怎么关掉？
**答案**：传 `pass_configs={"tirx.disable_vectorize": True}`。它经 `normalize_pass_configs` 进 PassContext，`allow_vectorize()`（pipeline_utils.py 第 10-14 行）读到后返回 `False`，于是 `VectorizeLoop(enable_vectorize=False)` 跳过向量化。

**练习 2**：为什么 `SplitHostDevice` 在 pipeline **内部**，而 `Filter(host/device)` 在 `lower_to_host_device_ir` 里？
**答案**：`SplitHostDevice` 是 IR 变换——它给每个函数打上 `calling_conv` 标记（host 还是 device kernel），这是 Pass 流水线的一部分；`Filter` 只是按这个标记把函数分拣到两个 IRModule 容器，不做任何 IR 变换，所以在 pipeline 跑完之后由 `lower_to_host_device_ir` 执行。

**练习 3**：`PassPipeline` 类本身不包含任何 Pass 列表，Pass 顺序存在哪里？
**答案**：存在各后端传入的那个 lower 回调函数里（如 CUDA 的 `CUDAPassPipelineBody`），顺序就是函数体内一行行 `mod = xxx_pass()(mod)` 的书写顺序。`PassPipeline` 只是给这串调用起了个名字并放进注册表。

---

### 4.4 tilelang.backend.device_codegen：设备/主机代码生成

#### 4.4.1 概念说明

Pass 流水线跑完、host/device 拆分完后，IRModule 已经低到「接近目标语言文本」。**代码生成（codegen）**负责把这最后一层 IR 翻译成 CUDA/HIP/C 等源码（并可选地编译成二进制）。

tilelang 把代码生成器也做成「注册表 + 惰性加载」：

- **注册表**：每个后端用 `register_device_codegen` 注册若干 `DeviceCodegen` 条目，每条带一个 `supports_target` 谓词（用于在同一种类下区分变体，比如 CUDA 下区分「普通 CUDA」与「CuTeDSL」）。
- **惰性加载**：为避免 import 时把所有后端都加载进来，用 `register_lazy_device_codegen` 只登记一个模块路径，等真正用到该 target 时才 `import_module` 触发注册。
- **解析**：`resolve_device_codegen(target)` 按 `target.kind.name` 找到候选列表，过滤出第一个匹配的 `DeviceCodegen`。

`DeviceCodegen` 提供「编译」与「只生成源码」两条路径（`build` / `build_without_compile`），分别对应 `lower()` 里 `enable_device_compile=True/False` 两个分支。host 侧的 `host_codegen` 采用完全相同的注册表结构，只是默认在 `lower()` 里不启用（`enable_host_codegen=False`），由 JIT 层自己处理。

#### 4.4.2 核心流程

```
lower() 拿到 device_mod
   │
   │  device_codegen(device_mod, target)        enable_device_compile=True
   │  device_codegen_without_compile(...)       enable_device_compile=False (默认)
   ▼
先 _prepare_device_codegen_mod:  LowerIntrin → Simplify → HoistBroadcastValues
   │
   │  resolve_device_codegen(target).lower(mod, target, compile_device=?)
   ▼
codegen 调用 TVM 全局函数 (如 target.build.tilelang_cuda)
   │
   │  compile_device=True 时，内部触发 tilelang_callback_cuda_compile
   │  → nvcc 编译 → cubin/fatbin（带 CUDABinaryCache 缓存）
   ▼
IRModule（inspect_source() 得到 kernel_source 文本）
```

#### 4.4.3 源码精读

`DeviceCodegen` 数据类与解析逻辑：

[tilelang/backend/device_codegen.py:27-44](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/device_codegen.py#L27-L44) —— 注意 `lower()` 方法第 39-44 行按 `compile_device` 在 `build` 与 `build_without_compile` 间二选一；若所需路径未注册则抛 `ValueError`。

`global_func_device_codegen` 把一个 TVM 全局函数包成 `DeviceCodegenFunc`：

[tilelang/backend/device_codegen.py:18-24](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/device_codegen.py#L18-L24)

解析与惰性加载：

[tilelang/backend/device_codegen.py:69-73](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/device_codegen.py#L69-L73) —— `register_lazy_device_codegen` 只记一个 import 路径。

[tilelang/backend/device_codegen.py:102-110](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/device_codegen.py#L102-L110) —— `resolve_device_codegen` 取第一个匹配项；无匹配则报错并列出已注册项。

惰性注册的集中登记处在 backend 包初始化时：

[tilelang/backend/\_\_init\_\_.py:42-47](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/__init__.py#L42-L47) —— 把 cuda/hip/c/llvm/metal/webgpu 的 codegen 模块都登记为惰性加载。

CUDA 后端真正注册 codegen 的地方。它注册了**两条**路径：普通 CUDA 与 CuTeDSL，用 `supports_target` 谓词（检查 target.keys 里有没有 `"cutedsl"`）区分：

[tilelang/cuda/codegen.py:16-36](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/codegen.py#L16-L36) —— build 与 build_without_compile 分别绑到 `target.build.tilelang_cuda` 与 `target.build.tilelang_cuda_without_compile` 两个 TVM 全局函数。

回到 `lower.py` 看 codegen 如何被调用。`_prepare_device_codegen_mod` 做 codegen 前的最后清理，`device_codegen` / `device_codegen_without_compile` 二者共用它：

[tilelang/engine/lower.py:242-256](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L242-L256)

设备源码最终编译成二进制的回调（`enable_device_compile=True` 时被 codegen 内部触发）。它处理 nvcc 选项、fast_math、ptxas、以及 `CUDABinaryCache` 缓存：

[tilelang/engine/lower.py:101-175](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L101-L175) —— `tilelang_callback_cuda_compile`，注册为 TVM 全局函数 `tilelang_callback_cuda_compile`。第 154-164 行是二进制缓存命中逻辑。

host 侧的 `host_codegen()` 在 `lower.py` 里是另一串 TVM 标准变换（`BindTarget`、`LowerTVMBuiltin` 等）加 `resolve_host_codegen(target_host).lower(...)`：

[tilelang/engine/lower.py:215-239](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L215-L239)

代码生成的最终产物结构。`CompiledArtifact` 同时持有 host_mod、device_mod、参数列表、源码与（可选）runtime module：

[tilelang/engine/param.py:153-167](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/param.py#L153-L167)

#### 4.4.4 代码实践

**实践目标**：验证「同一种 target 下可注册多个 codegen 变体」与「惰性加载」两个机制。

**操作步骤**（源码阅读型）：

1. 读 [tilelang/cuda/codegen.py:8-13](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/codegen.py#L8-L13) 的两个谓词 `_is_cutedsl_target` / `_is_plain_cuda_target`，理解它们如何用 `target.keys` 里的 `"cutedsl"` 区分两条 CUDA codegen 路径。
2. 读 [tilelang/backend/device_codegen.py:85-88](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/device_codegen.py#L85-L88) 的 `_matching_device_codegens`，确认它先 `_ensure_device_codegens_loaded`（触发惰性 import）再做 `codegen.matches(target)` 过滤。
3. （可选，需 GPU）在 4.1.4 的脚本里加 `enable_device_compile=True` 调用 `tilelang.lower(func, target="cuda", enable_device_compile=True)`，观察是否触发 `tilelang_callback_cuda_compile`。

**需要观察的现象**：第 1 步可解释「为什么 CuTeDSL backend 与普通 CUDA backend 共用 `target.kind.name == "cuda"` 却走不同代码生成器」；第 2 步可解释「为什么 import tilelang 时不会立刻加载所有后端」。

**预期结果**：能口述出「`resolve_device_codegen` → `_matching_device_codegens` → 惰性 import codegen 模块 → 该模块在 import 时 `register_device_codegen` → `matches` 过滤」这条链路。第 3 步若执行，二次调用相同 kernel 应命中 `CUDABinaryCache`（见 lower.py 第 162-164 行），编译耗时显著下降——具体耗时**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 CUDA 后端要注册两个 `DeviceCodegen`（`"cuda"` 和 `"cutedsl"`）而不是一个？
**答案**：因为它们都对应 `target.kind.name == "cuda"`，但底层代码生成策略不同（普通 CUDA 走 CUTLASS 模板，CuTeDSL 走 CuTe DSL Python 路径）。用 `supports_target` 谓词（检查 `target.keys` 是否含 `"cutedsl"`）让 `resolve_device_codegen` 能在同一种类下挑出正确变体。

**练习 2**：`enable_device_compile=False` 时，`tilelang_callback_cuda_compile` 会被调用吗？
**答案**：不会。`enable_device_compile=False` 走 `build_without_compile` 路径，只产出 CUDA 源码文本；`tilelang_callback_cuda_compile`（调 nvcc）只在 `build`（`compile_device=True`）路径里被触发。

**练习 3**：`host_codegen` 的注册表结构与 `device_codegen` 有何异同？
**答案**：结构几乎相同（都是 `dataclass` + 注册表 + 惰性加载 + `resolve_*`）。区别是：host 侧多了 `HostCodegenHook`（设备后端在 host codegen 前插入钩子，如 Metal 的 `MarkHostMetalContext`），且 `lower()` 默认 `enable_host_codegen=False`，host 代码通常由 JIT 层自行处理。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「端到端的编译链路追踪」。

**任务**：用 `examples/quickstart.py` 的 `matmul` kernel，生成一张完整的「IRModule 流转图」，要求图上标注：

1. **入口**：`@tilelang.jit` 的函数如何变成 `PrimFunc`（承接 u2-l1）。
2. **target 解析**：`target="cuda"` 经 `determine_target` 变成 `Target`（4.2）。
3. **语义检查点**：`PreLowerSemanticCheck` 的位置（4.1）。
4. **Pass 流水线**：至少标出 `PipelinePlanning`、`InjectSoftwarePipeline`、`LayoutInference`、`LowerTileOp`、`SplitHostDevice`、`MakePackedAPI` 六个 Pass 的先后（4.3）。
5. **host/device 拆分**：`Filter` 把模块分成两份（4.1）。
6. **代码生成**：`resolve_device_codegen` → `target.build.tilelang_cuda` → `kernel_source`，以及（可选）`tilelang_callback_cuda_compile` → cubin（4.4）。
7. **产物**：`CompiledArtifact` 的字段（4.4）。

**建议做法**：

- 开启 `pass_configs={"tl.enable_dump_ir": True, "tl.dump_ir_path": "./dump_ir"}` 与 `TILELANG_JIT_DIAGNOSTICS=1` 编译一次。
- 用 `kernel.get_kernel_source()` 取出生成的 CUDA 文本，在其中找到 `T.gemm` 被展开后的痕迹（如 `cute::` 或 mma 指令），反推它是由 `LowerTileOp` Pass 产生的。
- 把上述七个阶段画成一张从左到右的流程图（手绘或任何画图工具均可），并在每个节点标注对应的源码文件与行号（本讲给出的永久链接）。

**验收标准**：能指着图上任意一个节点，说出「这一步在哪个文件的哪个函数里发生、输入输出各是什么 IRModule」。若某 Pass 的具体效果无法确认，标注「待本地验证」而非臆测。

## 6. 本讲小结

- `tilelang.lower()` 是编译器总入口，主体是 `lower_to_host_device_ir()`：它按「封装模块 → 解析 target → 语义检查 → 跑 pipeline → 拆分 host/device」五步推进，IRModule 在其中被逐层改写。
- 目标判定 `determine_target` 用「探测器 + 归一化器」注册表把 `"auto"`/字符串/dict 统一解析为 TVM `Target`，是 lowering 的第一步。
- Pass 流水线 `resolve_pipeline(target)` 按 `target.kind.name` 选后端序列；`PassPipeline` 只是薄包装，真实顺序写在各后端的 lower 回调里（CUDA 约 50 个 Pass）。
- PassContext 是 Pass 运行时的配置容器，由 JIT 层用 `with tvm.transform.PassContext(config=pass_configs, ...)` 打开；pipeline 里的 Pass 通过 `get_pass_context()` 回读开关（如 `disable_vectorize`）。
- 设备/主机代码生成 `resolve_device_codegen` / `resolve_host_codegen` 采用「注册表 + 惰性加载 + supports_target 谓词」，使同一种类下可区分多个变体（如普通 CUDA 与 CuTeDSL）。
- `SplitHostDevice` 是 pipeline **内部**的 IR 变换，给函数打上 `calling_conv` 标记；其后的 `Filter` 才把函数分拣成 `host_mod` / `device_mod`。最终产物是 `CompiledArtifact`。

## 7. 下一步学习建议

本讲只讲了**编译总流程的骨架**，许多细节被有意略过，后续讲义会逐一展开：

- **u4-l2（jit 装饰器与 lazy/eager 模式）**：接本讲 4.1.2 的「JIT 层如何打开 PassContext 并调用 lower」，深入 `@tilelang.jit` 的两种模式与 `.compile()` 的完整调用链。
- **u4-l3（编译缓存机制）**：接本讲 4.4 提到的 `CUDABinaryCache`，讲 JITKernel 缓存键的构造与失效。
- **u5（从 Python 到 TIR）**：本讲假设输入已经是 `PrimFunc`，u5 讲 eager builder 如何把 Python 函数体构建成这个 `PrimFunc`。
- **u6（Pass 体系与代码生成）**：本讲只列出 Pass 名，u6 会精读 `LayoutInference`、`LowerTileOp`、`InjectSoftwarePipeline` 等关键 Pass 的内部实现，以及 CUDA codegen 如何把 IR 翻成 CUTLASS 模板调用。

建议接下来按 **u4-l2 → u4-l3 → u6-l1** 的顺序学习，先把 JIT 与缓存补全，再回到 Pass 内部细节。
