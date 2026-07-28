# 代码生成与目标后端

## 1. 本讲目标

本讲承接 [u3-l4 OptimizeForTarget](u3-l4-optimize-target.md)，回答一个核心问题：**当 `OptimizeForTarget` 把 IR 改造到「可为 codegen 准备好」之后，TileLang 究竟是怎样把它变成可以跑在 GPU 上的 CUDA / HIP 源码与二进制的？**

学完本讲你应当能够：

- 说清 **host codegen** 与 **device codegen** 的分工，以及它们如何按 `target.kind.name` 分发到一组以 `target.build.tilelang_*` 命名的 TVM-FFI 全局函数；
- 看懂设备源码生成器（`CodeGenTileLangCUDA` / `CodeGenTileLangHIP`）如何遍历 IR，把 `LowerTileOp` 产出的高层 intrin 逐条「打印」成 `tl::mma_sync<...>`、`tl::tma_load(...)`、`tl::tl_gemm(...)` 这样的 C++ 模板调用文本；
- 理解 C++ 端为何要**回调**到 Python 注册的 `tilelang_callback_cuda_compile` / `tilelang_callback_hip_compile` 去真正调用 nvcc / hipcc，并掌握「编译」与「只出源码」两条路径的区别；
- 掌握 **NVSHMEM 设备库链接**在两条路径下各自的触发条件；
- 认识 TileLang 支持的全部目标后端（cuda / hip / c / llvm / metal / webgpu / cutedsl）及其选择逻辑。

本讲只讲「编译流水线的最后一公里」，不再重复 [u3-l1](u3-l1-compile-overview.md) 的总览与 [u3-l3](u3-l3-lower-legalize.md)/[u3-l4](u3-l4-optimize-target.md) 的 pass 细节。

## 2. 前置知识

- **TVM 的 target 与 runtime.Module**：TVM 用 `target` 字符串（如 `cuda -arch=sm_80`）描述目标设备；编译产物是一个 `runtime.Module`，它既能被加载执行，也能用 `inspect_source()` 取回生成的源码字符串。TileLang 构建在 TVM 之上，复用了这套机制。
- **codegen = 「源码打印机」**：TVM 的 codegen 本质是一个对 IR 树做遍历（visitor）、边遍历边往字符串流里写 C/CUDA 代码的过程。`T.copy` / `T.gemm` 这类原语在进入 codegen **之前**，就已经被 `LowerTileOp`（见 [u3-l3](u3-l3-lower-legalize.md)）降级成了 `builtin::ptx_mma`、`tl::tma_load` 等「低层 intrin」；codegen 只负责把这些 intrin 翻译成最终文本。
- **FFI 全局函数（PackedFunc）**：TVM 用一个全局函数表把 C++ 与 Python 串起来。C++ 用 `ffi::Function::GetGlobal("名字")` 取回一个在 Python 侧用 `@tvm_ffi.register_global_func` 注册的函数并调用它。本讲的「编译回调」就建立在这套机制上。
- **cubin / ptx / hsaco**：nvcc 把 CUDA 源码编成 GPU 可执行二进制叫 **cubin**；中间表示叫 **PTX**。hipcc 把 HIP 源码编成 AMD GPU 二进制叫 **hsaco**。本讲会频繁出现这几个词。

> 承接认知：[u3-l1](u3-l1-compile-overview.md) 已经讲过 `tilelang.lower` 是编译主入口，`device_codegen` 会按 target 选后端，`enable_device_compile` 控制「是否真的调用 nvcc/hipcc 编成二进制」。本讲把这条链路彻底拆开。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tilelang/engine/lower.py` | 定义 `device_codegen` / `host_codegen` / `device_codegen_without_compile`，以及两个编译回调 `tilelang_callback_cuda_compile` / `tilelang_callback_hip_compile` |
| `src/target/codegen_cuda.cc` | CUDA 设备源码生成器 `CodeGenTileLangCUDA`：把 IR 打印成 CUDA 文本（含 `tl::` 模板调用、头文件 `#include`、NVSHMEM `#include`） |
| `src/target/codegen_hip.cc` | HIP 设备源码生成器 `CodeGenTileLangHIP`：把 IR 打印成 HIP 文本 |
| `src/target/rt_mod_cuda.cc` | 注册 `target.build.tilelang_cuda`（含/不含编译两个变体），在 C++ 端调用 Python 编译回调，产出 CUDA runtime.Module |
| `src/target/rt_mod_hip.cc` / `rt_mod_cutedsl.cc` | 同上，分别对应 HIP 与 CuTe DSL 后端 |
| `tilelang/jit/adapter/libgen.py` | JIT 适配器路径（cython/nvrtc/ctypes/dlpack）下，把生成的源码用 nvcc/hipcc 子进程编成 `.so`，并在此处链接 NVSHMEM |
| `tilelang/utils/target.py` | target 字符串解析、`auto` 自动探测、支持的后端表 `SUPPORTED_TARGETS` |
| `docs/get_started/targets.md` | 官方 target 使用文档 |

## 4. 核心概念与源码讲解

### 4.1 host/device 代码生成的分工与 `target.build.tilelang_*` 分发

#### 4.1.1 概念说明

经过 `OptimizeForTarget` 之后，整个 `IRModule` 里的 `PrimFunc` 被分成了两类：

- **device 函数**：被打上 `calling_conv = DEVICE_KERNEL_LAUNCH` 标记的 kernel（见 [u3-l1](u3-l1-compile-overview.md)），是要上 GPU 跑的。它们由 **device codegen** 处理。
- **host 函数**：负责「准备参数、发起 kernel 启动」的 CPU 侧胶水代码。它们由 **host codegen** 处理。

`lower()` 用 `tir.transform.Filter` 按调用约定把这两类函数筛进两个子模块（`host_mod` / `device_mod`，见 [u3-l1](u3-l1-compile-overview.md)），再分别送进 `device_codegen` 与 `host_codegen`。两者最终都通过 `tvm.ffi.get_global_func("target.build.xxx")` 取一个**已注册的 TVM-FFI 全局函数**来干活——也就是说，「选哪个后端」=「拼哪个全局函数名」。

#### 4.1.2 核心流程

device codegen 的分发逻辑非常简洁：

```text
device_codegen(device_mod, target):
    LowerDeviceStorageAccessInfo → LowerIntrin → Simplify
    if target.kind.name == "cuda":
        func = "target.build.tilelang_" + ("cutedsl" if "cutedsl" in target.keys else "cuda")
    elif target.kind.name == "hip":
        func = "target.build.tilelang_hip"
    else: 报错
    调用该全局函数 → 返回 runtime.Module
```

host codegen 则固定走 `target.build.llvm`（host 默认 `llvm`）或 `target.build.tilelang_c`（host 为 `c` 时）。

#### 4.1.3 源码精读

`device_codegen` 在 [tilelang/engine/lower.py:204-217](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L204-L217) 定义。它先做三道通用降级（存储访问合法化、intrin 降级、化简），再按 `target.kind.name` 拼出全局函数名并调用。注意 cuda 分支里那行三元表达式——**CuTe DSL 后端是通过在 target 的 `keys` 里塞一个 `"cutedsl"` 来识别的**，因此它复用了 `cuda` 的 target 字符串，只是把 `tilelang_cuda` 换成 `tilelang_cutedsl`。

[host_codegen](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L186-L201) 的结构类似，但多做了 FP8/BF16 存储合法化、`LowerTVMBuiltin`、`CombineContextCall` 等 host 专用 pass，最后按 host target 选 `target.build.llvm` 或 `target.build.tilelang_c`。

这些 `target.build.tilelang_*` 全局函数在哪里注册？在 C++ 端的运行时模块文件里，用 `TVM_FFI_STATIC_INIT_BLOCK` 静态注册：

- CUDA：[src/target/rt_mod_cuda.cc:107-113](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/rt_mod_cuda.cc#L107-L113) 注册了 `tilelang_cuda` 与 `tilelang_cuda_without_compile` 两个变体；
- HIP：[src/target/rt_mod_hip.cc:118-124](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/rt_mod_hip.cc#L118-L124) 注册 `tilelang_hip` / `tilelang_hip_without_compile`；
- CuTe DSL：[src/target/rt_mod_cutedsl.cc:62-66](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/rt_mod_cutedsl.cc#L62-L66) 只注册了 `_without_compile` 变体（DSL 后端不需要传统 nvcc 编译）。

`lower()` 决定调哪个变体的开关在 [tilelang/engine/lower.py:288](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L288)：`enable_device_compile=True` 走 `device_codegen`（真编译），否则走 `device_codegen_without_compile`（只出源码）。而这两个开关又由执行后端决定（见 4.3 与 4.5）。

#### 4.1.4 代码实践

**目标**：直观看到「同一个 kernel，不同 target 走不同的全局函数」。

1. 准备一个最小 matmul `@T.prim_func`（可复用 `examples/quickstart.py` 的内层函数）。
2. 分别用 `tilelang.lower(func, target="cuda")` 与 `target="hip"`（若本机有 ROCm）调用，在 `device_codegen` 处加一行 `print(global_func)`（仅本地调试，勿提交）。
3. 观察打印：cuda 下是 `target.build.tilelang_cuda`，hip 下是 `target.build.tilelang_hip`。
4. **预期结果**：后端选择完全由 `target.kind.name` 这一个字符串驱动，分发逻辑集中在一处、易于扩展。

> 若本机无 GPU/ROCm，第 2 步可能在 nvcc/hipcc 阶段报错——这正常，因为你只关心 `global_func` 名字，可在报错前打印。无法运行时标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 CuTe DSL 后端只注册了 `_without_compile` 变体，却没有「带编译」的变体？

**参考答案**：CuTe DSL 走的是 NVIDIA 的 CuTe DSL（C++ 模板库 + 运行时编译）路线，它的「编译」由 DSL 自己的 JIT 在运行期完成（对应 `cutedsl` 执行后端），不需要 TileLang 在 `lower` 阶段调 nvcc 产 cubin。所以 codegen 只需产出源码即可（见 [rt_mod_cutedsl.cc:41-60](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/rt_mod_cutedsl.cc#L41-L60)）。

**练习 2**：若要新增一个假想后端 `vulkan`，至少要改动哪几处？

**参考答案**：至少三处——(1) 在 `device_codegen_without_compile` 的分支里加 `elif target.kind.name == "vulkan"`（[lower.py:224-238](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L224-L238)）；(2) 写一个 C++ codegen 并注册 `target.build.tilelang_vulkan`；(3) 在 `SUPPORTED_TARGETS` 里加描述（见 4.5）。

### 4.2 设备源码生成器内部：从 TIR 到 CUDA/HIP 文本

#### 4.2.1 概念说明

`target.build.tilelang_cuda` 背后真正干活的是 C++ 类 `CodeGenTileLangCUDA`（HIP 对应 `CodeGenTileLangHIP`）。它继承自 TVM 的 `CodeGenC`，本质是一个「带状态的源码打印机」：对 IR 树做 visitor 遍历，每遇到一种节点（`ForNode`、`CallNode`、`AllocateNode`……）就往输出流里写一段对应的 CUDA/HIP 代码。

一个关键认知：**到了 codegen 这一关，`T.copy` / `T.gemm` 已经不存在了**——它们在 `LowerTileOp`（[u3-l3](u3-l3-lower-legalize.md)）里被降级成了 `builtin::ptx_mma`、`tl::tma_load`、`tl::ptx_wgmma_ss`、`tl::tl_gemm` 等低层 intrin。codegen 的 `VisitExpr_(const CallNode*)` 就是一个巨大的 `if-else if` 链，把这些 intrin 一条条翻译成文本。

#### 4.2.2 核心流程

源码生成分两步走：

1. **逐函数打印**：对 `device_mod` 里每个 `PrimFunc` 调用 `AddFunction`，打印函数签名（`extern "C" __global__` 前缀 + `__launch_bounds__` + 参数列表）和函数体。
2. **收尾 `Finish()`**：在源码顶部补上所有需要的 `#include`（`tl_templates/cuda/*.h` 这些 CUTLASS 风格的设备模板头、`nvshmem.h`、`mma.h` 等），然后返回完整源码字符串。

「需要哪些头」是由遍历过程中置位的**特性标志位**决定的——例如遍历中遇到了 wmma intrin 就把 `need_mma_h_` 置真，`Finish()` 据此决定是否 `#include <mma.h>`。

#### 4.2.3 源码精读

**函数前缀**：[src/target/codegen_cuda.cc:220-222](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L220-L222) 把每个 kernel 打印成 `extern "C" __global__`，这保证生成的符号能被运行时按名字 `cuGetProcAddress` 取到。`__launch_bounds__` 则由 [PrintExtraAttrs](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L249-L265) 从 IR 的 `thread_extent` 属性里反推出每个 block 的线程数后打印。

**模板调用打印**：codegen 最有代表性的两处。其一是普通 mma（Ampere 及以前）走 `builtin::ptx_mma`，被打印成一个 `tl::mma_sync<...>(...)` 模板调用——模板实参（数据类型、M/N/K、是否转置）来自 IR 节点的参数，通过一个 `Replacer` 做字符串替换填入，见 [src/target/codegen_cuda.cc:1841-1886](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L1841-L1886)。其二是走 CUTLASS 风格「算子实例」的 `tl::tl_gemm`，它把一个完整的 op 实例名（一个长字符串类型）当函数名直接 emit，见 [src/target/codegen_cuda.cc:2558-2564](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L2558-L2564)。这两处就是本讲综合实践要在生成源码里定位的「模板调用片段」。

**收尾头文件**：[Finish()](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L267-L343) 按特性标志位 `#include` 一组设备模板头：`tl_templates/cuda/gemm.h`、`copy.h`、`reduce.h`、`ldsm.h`、`threadblock_swizzle.h`、`intrin.h`（[第 315-324 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L315-L324)）。这些头就是上面 `tl::mma_sync` / `tl::gemm_*` 等模板的真正定义所在（详见 [u7-l2 CUDA 模板](u7-l2-cuda-gemm-templates.md)）。**当 kernel 用到了分布式原语时**，`Finish()` 还会额外 `#include <nvshmem.h>` / `<tl_templates/cuda/distributed.h>` 等（[第 297-313、326-331 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L297-L331)），这是 NVSHMEM 链接的「源码侧」前置条件。

HIP 后端的 `CodeGenTileLangHIP::Finish()` 结构相同，只是换成 `hip/hip_runtime.h` 与 `tl_templates/hip/*.h`，见 [src/target/codegen_hip.cc:138-157](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_hip.cc#L138-L157)；它的 mma 走 AMD 的 `__builtin_amdgcn_mfma_*`（[第 900-964 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_hip.cc#L900-L964)）。

#### 4.2.4 代码实践

**目标**：用 `IRModule` 的打印与 codegen 输出对照，体会「intrin → 文本」的翻译。

1. 对一个 matmul `prim_func` 调 `tilelang.lower(func, target="cuda")` 得到 `CompiledArtifact`。
2. 打印 `artifact.device_mod`（`print(device_mod.script())`），在 IR 里找 `T.call_intrin` 或 `ptx_mma` 之类的低层调用——这是 codegen 的**输入**。
3. 打印 `artifact.kernel_source`（即 codegen 的**输出**），对照找到对应的 `tl::mma_sync<...>(...)` 或 `tl::tl_gemm(...)` 文本。
4. **预期结果**：能一一对应「IR 里的一个 Call 节点」↔「源码里的一行 `tl::xxx(...)` 调用」。若 IR 里看不到 `T.gemm`、只剩低层 intrin，正说明 `LowerTileOp` 已在 codegen 之前完成了降级。

> 无法本地运行时，可只读 `src/target/codegen_cuda.cc` 的 `VisitExpr_` 与 `Finish()`，人工对照「intrin 名 → emit 的文本」，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 codegen 里有一堆 `need_xxx_h_` 标志位，而不是无脑 `#include` 所有头？

**参考答案**：避免生成冗余、避免引入目标架构不支持的依赖（如老架构没有 wgmma）。标志位只在遍历中真正用到对应 intrin 时才置位，`Finish()` 据此按需 include，让生成的源码最小化、可读、可编。

**练习 2**：`extern "C"` 前缀对运行时为什么重要？

**参考答案**：它禁止 C++ 名称修饰（name mangling），使 kernel 的符号名与其 `global_symbol` 完全一致，运行时才能用 `cuGetProcAddress`/`dlsym` 按字符串名取到函数指针并启动。

### 4.3 编译回调：`tilelang_callback_cuda_compile` / `tilelang_callback_hip_compile`

#### 4.3.1 概念说明

codegen 只产出了**源码字符串**，但 GPU 跑的是**二进制**。把源码变成二进制需要调用外部工具链（nvcc / hipcc）。TileLang 选择了一个精巧的设计：**C++ 的 codegen 把源码交给一个在 Python 侧注册的 FFI 回调函数去编译**，而不是在 C++ 里直接 fork nvcc。

这样做的好处是：编译选项（arch、fast-math、NVSHMEM 链接、ptxas 调参等）都由 Python 控制，方便用户用环境变量/pass_config 灵活配置，也方便在 Python 侧做缓存。

#### 4.3.2 核心流程

「带编译」路径（`enable_device_compile=True`，即 `tvm_ffi` 执行后端）：

```text
BuildTileLangCUDA(mod, target):                # C++
    codegen 打印出源码 code
    若有 tilelang_callback_cuda_postproc 则后处理 code
    通过 FFI 取 Python 注册的 tilelang_callback_cuda_compile(code, target, pass_config)
        ↓ 回到 Python
        nvcc.compile_cuda(code, "cubin", arch=["-arch=sm_xx"], options=[...])  # 真正调 nvcc
        返回 cubin 二进制字节串 ptx（历史命名，实为 cubin）
    if ptx[0] != '/': fmt = "cubin"            # 非路径 → 当成内存二进制
    TileScaleCUDAModuleCreate(ptx, fmt, ...)    # 把 cubin 包进 runtime.Module
```

「只出源码」路径（`enable_device_compile=False`，即 cython/nvrtc/ctypes/dlpack 等后端）：C++ 完全不调回调，直接用占位 `"ptx"` 把源码包进 runtime.Module 返回；真正的 nvcc 编译延后到 JIT 适配器的 `LibraryGenerator.compile_lib()`（见 4.4）。

#### 4.3.3 源码精读

**C++ 端调用回调**：[BuildTileLangCUDA](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/rt_mod_cuda.cc#L46-L81) 在 codegen 出 `code` 后，用 `ffi::Function::GetGlobal("tilelang_callback_cuda_compile")` 取回 Python 函数并调用（[第 68-78 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/rt_mod_cuda.cc#L68-L78)），注意它把当前 `PassContext` 的 config 也传了过去——这样 fast-math、ptxas 寄存器用量等 pass_config 才能传到 nvcc。

这里有一个容易踩坑的「历史命名」细节：变量名叫 `ptx`、默认 `fmt = "ptx"`，但回调返回的其实是 cubin 字节；只有当返回串以 `'/'` 开头时（表示是一个 `.cubin` 文件路径）才保持 `ptx`——否则置为 `"cubin"` 表示这是内存里的二进制 blob（[第 66-75 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/rt_mod_cuda.cc#L66-L75)）。`BuildTileLangCUDAWithoutCompile`（[第 83-105 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/rt_mod_cuda.cc#L83-L105)）则不调回调，直接把源码塞回去。

**Python 端回调实现**：[tilelang/engine/lower.py:59-135](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L59-L135) 用 `@tvm_ffi.register_global_func("tilelang_callback_cuda_compile", override=True)` 注册。它做三件事：(1) 定位 `src/` 模板路径与 `3rdparty/cutlass/include` 头路径；(2) 从 target 解析 `sm_xx` arch；(3) 从 `pass_config` 读 fast-math / ptxas 选项，组装 nvcc 选项后调 `nvcc.compile_cuda(code, "cubin", arch, options)`（[第 127-133 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L127-L133)）。HIP 回调 [tilelang_callback_hip_compile](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L138-L161) 结构对称，调 `hipcc.compile_hip` 产 hsaco。

#### 4.3.4 代码实践

**目标**：观察「源码 → nvcc → cubin」这一跳确实发生在 Python 回调里。

1. 复制 quickstart 的 matmul，用 `@tilelang.jit(execution_backend="tvm_ffi")`（这是唯一会触发 `device_codegen` 真编译的后端）编译。
2. 在 `tilelang_callback_cuda_compile` 函数体首行临时加 `print("nvcc arch =", arch, "opts =", options)`（本地调试用）。
3. 运行，观察是否打印了 `["-arch=sm_xx", "-std=c++17", "-I.../src", "-I.../cutlass/include"]` 之类选项。
4. **预期结果**：确认 cubin 是由这个 Python 回调调用 nvcc 产生的，而非 C++ 内部产出。把 `execution_backend` 换成 `"cython"` 再试，应**不**打印（因为走的是 without_compile 路径，编译挪到了 4.4 的 `compile_lib`）。

> 无 GPU 时第 1 步会失败；可仅在第 2 步断点处读 `code` 内容，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么把回调注册成 `override=True`？

**参考答案**：`tilelang_callback_cuda_compile` 这个名字可能在多处被注册（例如测试或 `tilelang/contrib/hipcc.py` 里也注册了 hip 版同名函数）。`override=True` 保证后注册的覆盖先注册的，使当前 Python 进程里取到的是 TileLang 自己的实现。

**练习 2**：`tvm_ffi` 后端与 `cython` 后端，cubin 分别在哪里产生？

**参考答案**：`tvm_ffi` 后端在 `tilelang_callback_cuda_compile`（C++ codegen 回调到 Python 调 nvcc）里产生，cubin 直接嵌入 runtime.Module；`cython` 后端在 `LibraryGenerator.compile_lib()`（Python 直接 fork nvcc）里产生，编译成磁盘上的 `.so` 再 `ctypes.CDLL` 加载（见 4.4）。

### 4.4 NVSHMEM 设备库链接的触发条件

#### 4.4.1 概念说明

当 kernel 用到了分布式原语（putmem/getmem、CP-engine 远程拷贝等，见 [u6-l2](u6-l2-nvshmem-primitives.md)/[u6-l3](u6-l3-cpengine-remote-copy.md)），生成的 CUDA 源码里就会出现 `nvshmem_*` / `tl::get_remote_base_ptr` 等符号。这些符号的定义在 NVSHMEM 的**设备库** `libnvshmem_device`（device 端）与**主机库** `libnvshmem_host`（host 端）里，必须在编译/链接阶段接上，否则 cubin/`.so` 装载时会报未定义符号。

关键认知：**因为存在两条编译路径（4.3 的回调路径 与 JIT 的 `compile_lib` 路径），NVSHMEM 链接也要在两处分别实现**，否则只有一种执行后端能用分布式。

#### 4.4.2 核心流程

NVSHMEM 链接的触发受一个共同的开关控制：环境变量 `TILELANG_USE_DISTRIBUTED` 与 `TILELANG_USE_NVSHMEM` 同时为真（[tilelang/env.py:263-270](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/env.py#L263-L270)）。两处链接点：

| 路径 | 文件 | 链接的库 | 触发条件 |
| --- | --- | --- | --- |
| 回调路径（tvm_ffi 后端） | `tilelang/engine/lower.py` | `-lnvshmem_device -rdc=true` | `env.USE_DISTRIBUTED and env.USE_NVSHMEM` 且 NVSHMEM 路径就绪 |
| JIT 路径（cython/nvrtc/ctypes/dlpack） | `tilelang/jit/adapter/libgen.py` | `-lnvshmem_host -lnvshmem_device -rdc=true` | `env.USE_NVSHMEM and is_cuda_target` |

注意 `-rdc=true`（可重定位设备代码）是 NVSHMEM 设备库的硬性要求——NVSHMEM 的设备运行时依赖跨翻译单元的设备符号链接，必须开 RDC。JIT 路径还提供了 `TL_DISABLE_RDC` 开关以便在不需要时关掉。

此外还有**源码侧**的前置：codegen 在遍历到分布式 intrin 时会把 `use_distributed_` / `use_nvshmem_` 置位，`Finish()` 据此 `#include <nvshmem.h>`（4.2.3）。

#### 4.4.3 源码精读

回调路径的链接在 [tilelang/engine/lower.py:89-99](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L89-L99)：当 `env.USE_DISTRIBUTED and env.USE_NVSHMEM` 且 `NVSHMEM_INCLUDE_DIR` / `NVSHMEM_LIB_PATH` 都就绪时，往 nvcc 选项里追加 include、lib 路径、`-lnvshmem_device` 与 `-rdc=true`；否则抛错提示安装 `nvidia-nvshmem-cu12`。

JIT 路径的链接在 [tilelang/jit/adapter/libgen.py:129-135](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/adapter/libgen.py#L129-L135)：`if env.USE_NVSHMEM and is_cuda_target(target)` 时，追加 `-lnvshmem_host`、`-lnvshmem_device`，并按 `TL_DISABLE_RDC` 决定是否加 `-rdc=true`。注意这里同时链了 host 与 device 两个库，因为 `compile_lib` 产出的是一个同时含 host 启动代码与 device kernel 的 `.so`。

#### 4.4.4 代码实践

**目标**：验证「不开分布式开关时，NVSHMEM 链接不会出现」。

1. 不设 `TILELANG_USE_DISTRIBUTED`，用任意执行后端编译一个**普通** matmul。
2. 若用 `tvm_ffi` 后端，在 `tilelang_callback_cuda_compile` 打印 `options`；若用 cython 后端，设 `verbose=True` 看 `compile_lib compilation command`。
3. 确认命令行里**没有** `-lnvshmem_*` / `-rdc=true`。
4. 再设 `TILELANG_USE_DISTRIBUTED=1`、`TILELANG_USE_NVSHMEM=1` 重试（即便 kernel 没用到分布式原语），观察链接选项出现。
5. **预期结果**：NVSHMEM 链接只由环境开关驱动，与 kernel 是否真用了分布式原语无关（用了分布式原语但没开开关，会在加载时报未定义符号）。

> 无 NVSHMEM 环境时第 4 步可能因路径未就绪抛错，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 NVSHMEM 必须开 `-rdc=true`？

**参考答案**：NVSHMEM 的设备端 API 调用了在其他设备翻译单元里定义的符号（其设备运行时），需要「可重定位设备代码」模式让链接器在设备代码之间做符号解析。关掉 RDC 会导致这些符号在 cubin 装载时未定义。

**练习 2**：如果用 `cython` 后端跑分布式 kernel，但忘了开 `TILELANG_USE_DISTRIBUTED`，会在哪一步报错？

**参考答案**：编译/链接阶段不会报错（因为没接 NVSHMEM 库），但生成的 `.so` 里会出现 `nvshmem_*` 未定义符号，在 `ctypes.CDLL` 加载时（或首次调用 kernel 时）报「undefined symbol」。

### 4.5 多后端选择：cuda / hip / c / llvm / metal / webgpu / cutedsl

#### 4.5.1 概念说明

TileLang 支持一整套目标后端。它们由 target 字符串的「基础名」决定，每个基础名对应 `device_codegen_without_compile` 分支里的一个 `target.build.*` 全局函数。`auto` 会按 CUDA → HIP → Metal 的顺序自动探测本机可用后端。

#### 4.5.2 核心流程

```text
用户给 target 字符串 (默认 "auto")
  → determine_target():
       "auto" → 探测 check_cuda_availability / check_hip_availability / check_metal_availability
       "cutedsl" → 特殊归一化为带 "cutedsl" key 的 cuda target
       其它 → 直接 Target(str) 校验
  → lower() 里 device_codegen_without_compile 按 target.kind.name 分发:
       cuda  → target.build.tilelang_cuda_without_compile
       hip   → target.build.tilelang_hip_without_compile
       c     → target.build.tilelang_cpp
       llvm  → target.build.llvm          (复用上游 TVM)
       webgpu→ target.build.webgpu        (复用上游 TVM)
       metal → target.build.metal         (复用上游 TVM)
```

`device_codegen_without_compile`（[lower.py:220-240](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py#L220-L240)）是「后端全家福」的单一真相源：只有 `cuda` / `hip` / `cutedsl` 是 TileLang 自己实现的 codegen，`c` 是 TileLang 的 host/`cpp` 生成器，而 `llvm` / `webgpu` / `metal` 直接复用上游 TVM 的同名 `target.build.*`。

#### 4.5.3 源码精读

支持的后端表 [SUPPORTED_TARGETS](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/target.py#L14-L23) 列出了 8 个基础名及一句话描述，可通过 `describe_supported_targets()` 在运行时查到（[第 26-30 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/target.py#L26-L30)）。

[determine_target](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/target.py#L92-L172) 处理三类输入：`"auto"` 走探测（探测到 CUDA 时还会用 `torch.cuda.get_device_capability` 推 `sm_xx`，[第 119-127 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/target.py#L119-L127)）；`"cutedsl"` 走 `normalize_cutedsl_target` 归一化（[第 67-89 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/target.py#L67-L89)）；其它字符串直接交给 `Target(str)` 校验，失败时给出可读的受支持后端列表。

执行后端与 codegen 路径的对应关系在 [tilelang/jit/kernel.py:244-333](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/kernel.py#L244-L333)：只有 `tvm_ffi` 后端把 `enable_host_codegen` / `enable_device_compile` 都设为 `True`（走真编译），其余后端（cython/nvrtc/torch/cutedsl）都设为 `False`（走只出源码 + 各自的 JIT 编译）。`torch` 后端专属 `metal` target（[第 303-316 行](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/kernel.py#L303-L316)）。各后端的详细适配机制见 [u3-l6 JIT 适配器](u3-l6-jit-adapters.md)。

官方文档 [docs/get_started/targets.md](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/get_started/targets.md) 给出了常见 target 字符串、`-arch=sm_xx` 与 GPU 型号的对应表（sm_20 ~ sm_100a），以及「no kernel image available」等典型排错建议。

#### 4.5.4 代码实践

**目标**：用代码列出所有受支持后端，并体会 `auto` 的探测顺序。

1. 运行：
   ```python
   from tilelang.utils.target import describe_supported_targets
   for name, doc in describe_supported_targets().items():
       print(f"{name:>8}: {doc}")
   ```
2. 运行 `from tilelang.utils.target import determine_target; print(determine_target("auto"))`，看本机被探测成 `cuda` / `hip` / `metal` 中的哪一个。
3. 故意传一个非法串 `determine_target("xxxx")`，观察错误信息里是否列出了 8 个受支持后端。
4. **预期结果**：第 1 步打印 8 项；第 2 步取决于本机；第 3 步报错且提示 `cuda/hip/metal/llvm/webgpu/c/cutedsl/auto`。

> 这是纯 Python 调用，无需 GPU 即可验证。

#### 4.5.5 小练习与答案

**练习 1**：`cutedsl` 后端与 `cuda` 后端的 target 字符串有什么关系？

**参考答案**：`cutedsl` 复用了 `cuda` 的 target 字符串体系（接受相同的 `-arch` 等选项），只是在 target 的 `keys` 集合里多加一个 `"cutedsl"` 标记。codegen 据此把全局函数名从 `tilelang_cuda` 切到 `tilelang_cutedsl`（4.1.3）。

**练习 2**：为什么 `llvm` / `webgpu` / `metal` 不在 TileLang 自己的 codegen 文件里实现？

**参考答案**：它们复用上游 TVM 已经成熟的同名 `target.build.*` 后端。TileLang 只在 `device_codegen_without_compile` 的分发里把请求转给 `tvm.ffi.get_global_func("target.build.llvm/webgpu/metal")` 即可，无需重造轮子。

## 5. 综合实践

**任务**：把本讲学到的「codegen 产出源码」与「`T.gemm` 经 LowerTileOp 降级」串起来，亲手读一份真实生成的 CUDA 源码。

步骤：

1. 准备一个 matmul kernel（可直接用 `examples/quickstart.py` 的 `matmul` 内层函数：`T.alloc_shared` → `T.copy` → `T.gemm` → `T.copy` 回 global）。
2. 用 `@tilelang.jit`（默认 `tvm_ffi` 后端）或 `tilelang.compile` 编译，得到 `kernel`。
3. 取出设备源码：
   ```python
   src = kernel.get_kernel_source(kernel_only=True)   # 仅 device kernel
   # 或 kernel.get_kernel_source(kernel_only=False)  # host 启动器 + device
   print(src)
   ```
   该 API 见 [tilelang/jit/adapter/base.py:89-93](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/adapter/base.py#L89-L93)：`kernel_only=True` 返回 `mod.imports[0].inspect_source()`（device 源码），`False` 还拼上 host 启动器源码。
4. 在打印出的源码里完成以下「寻宝与注释」：
   - 找到 `extern "C" __global__` 函数前缀与 `__launch_bounds__`（对应 4.2.3 的 `PrintFuncPrefix` / `PrintExtraAttrs`）。
   - 找到 `Finish()` 产出的 `#include` 区，确认有 `tl_templates/cuda/gemm.h`、`copy.h`、`reduce.h`，并按本机架构看是否有 `instruction/mma.h`（Ampere）或 `instruction/wgmma.h`（Hopper）。
   - **定位由 LowerTileOp/codegen 生成的「模板调用片段」**：在函数体里找形如 `tl::mma_sync<...>(...)`（Ampere 及以前，对应 [codegen_cuda.cc:1841-1886](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L1841-L1886)）、或 `tl::warpgroup_commit_batch`/`wgmma` 系列（Hopper，对应 `ptx_wgmma_ss` 分支）、或 `tl::tl_gemm(<op_instance>, A, B, C)`（CUTLASS 风格，对应 [codegen_cuda.cc:2558-2564](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L2558-L2564)）的调用，在源码旁用注释写清：它来自哪条 IR intrin、由哪个 `VisitExpr_` 分支打印、模板实参（dtype/M/N/K）分别是什么。
   - 找到 `T.copy` 对应的搬运指令（可能是 `tl::tma_load(...)`、`tl::cp_async_gs<...>(...)` 或普通向量存取），同样注释其来源。
5. （进阶）把 `num_stages` 改大（如 1 → 3），重新 `get_kernel_source()`，对比 shared memory 缓冲数量与 `cp_async`/`tma` barrier 的变化，体会 [u4-l2 软件流水线](u4-l2-software-pipeline.md) 注入的代码长什么样。

**预期结果**：你能拿出一**段带中文注释的生成 CUDA 源码**，清楚标注每个 `tl::xxx` 调用对应原始 `T.copy` / `T.gemm` 的哪一步、由哪段 codegen 逻辑产出——这说明你已彻底打通「DSL 原语 → LowerTileOp 降级 → codegen 打印 → 模板调用」的整条链路。

> 无 GPU 时，第 2 步编译会失败；可改为直接读 `src/target/codegen_cuda.cc` 的 `VisitExpr_` 各分支与 `Finish()`，人工对照「intrin → emit 文本」，并标注「待本地验证」。

## 6. 本讲小结

- **host/device 分工**：`device_codegen` 把 device kernel 打印成 GPU 源码、`host_codegen` 打印 CPU 启动器，二者都按 `target.kind.name` 分发到以 `target.build.tilelang_*` 命名的 TVM-FFI 全局函数（4.1）。
- **codegen 即源码打印机**：`CodeGenTileLangCUDA/HIP` 遍历 IR，把 `LowerTileOp` 已经降级好的低层 intrin（`ptx_mma`、`tma_load`、`tl_gemm` 等）逐条翻译成 `tl::xxx` 模板调用文本，`Finish()` 按特性标志位补 `#include`（4.2）。
- **编译回调**：C++ codegen 把源码交给 Python 注册的 `tilelang_callback_cuda_compile` / `hip_compile` 真正调 nvcc/hipcc 产 cubin/hsaco；`enable_device_compile` 决定走「真编译」还是「只出源码」（4.3）。
- **两条编译路径**：`tvm_ffi` 后端走回调路径（cubin 嵌入 runtime.Module），`cython/nvrtc/ctypes/dlpack` 后端走 `LibraryGenerator.compile_lib()` 路径（编成磁盘 `.so` 再 ctypes 加载）。
- **NVSHMEM 链接**：受 `TILELANG_USE_DISTRIBUTED`/`TILELANG_USE_NVSHMEM` 开关驱动，必须在「回调路径」与「JIT `compile_lib` 路径」**两处分别**接 `-lnvshmem_device`/`-lnvshmem_host` 与 `-rdc=true`（4.4）。
- **多后端**：TileLang 自实现 cuda/hip/cutedsl/c 的 codegen，llvm/webgpu/metal 复用上游 TVM；`auto` 按 CUDA→HIP→Metal 探测；`SUPPORTED_TARGETS` 是后端清单的单一真相源（4.5）。

## 7. 下一步学习建议

- **往下看 JIT 适配器**：本讲反复提到的「两条编译路径」在 [u3-l6 JIT 适配器与运行时调用](u3-l6-jit-adapters.md) 里逐一展开（tvm_ffi / cython / nvrtc / torch / dlpack / cutedsl 各自如何封装 `JITKernel`、如何供给张量、如何测延迟）。
- **深入 codegen 内部**：[u7-l3 目标后端 codegen 深入](u7-l3-codegen-internals.md) 会逐行讲 `codegen_cuda.cc` 如何 emit 源码、`rt_mod_cuda` 如何加载 cubin、`intrin_rule`/`ptx` 如何做指令映射。
- **设备模板**：本讲看到的 `tl_templates/cuda/gemm.h` 等头是 [u7-l2 CUDA 模板与 GEMM 内核族](u7-l2-cuda-gemm-templates.md) 的主题（sm70~sm120、mma/wgmma/tcgen05 指令模板）。
- **分布式落地**：若你对 4.4 的 NVSHMEM 链接感兴趣，可直接跳到 [u6 分布式编程](u6-l1-distributed-overview.md)，看那些被链接进来的 `nvshmem_*` 符号在 kernel 里到底怎么用。
