# 目标后端 codegen 深入

## 1. 本讲目标

u3-l5 给出了代码生成的「全景图」：device codegen 把 device kernel 打印成 GPU 源码、host codegen 打印 CPU 启动器，二者按 `target.kind.name` 分发到 `target.build.tilelang_*` 全局函数。本讲不再讲地图，而是**打开黑盒**，回答三个深入问题：

1. `CodeGenTileLangCUDA` 到底是怎么一行一行把 TIR 打印成 CUDA `.cu` 源码的？`T.copy` / `T.gemm` 在源码里变成了什么文本？
2. PTX（NVIDIA 并行线程执行指令）字符串是怎么拼出来的？`intrin_rule` 又是如何把 `T.rsqrt` 这类数学函数映射成 `__rsqrtf` 的？
3. 打印出来的 `.cu` 源码被 nvcc 编译成 cubin 后，运行时是怎么被 `cuLaunchKernel` 跑起来的？分布式 kernel 的 `meta_data` 基址表又是怎么注入到设备端的？

学完本讲，你应当能够：

- 说清 codegen 作为「源码打印机」的工作模型与 visitor 分发机制；
- 在 `codegen_cuda.cc` 中定位任意一个 `tl.*` intrin 被 emit 成的具体 CUDA 文本片段；
- 解释 PTX 字符串构造器（`ptx.cc`）与数学函数映射规则（`intrin_rule_cuda.cc`）的作用；
- 描述 `TileScaleCUDAModuleNode` 如何加载 cubin、解析 grid/block、调用 `cuLaunchKernel`，并注入分布式 `meta_data`。

## 2. 前置知识

- **TIR PrimFunc 与 visitor 模式**：TVM 把 kernel 表示成一棵 TIR 语句树（For / If / BufferStore / Call 等）。codegen 继承 `CodeGenC`，用「访问者（visitor）」模式遍历这棵树，每遇到一类节点就重写一个 `VisitStmt_` / `VisitExpr_` 方法，向字符串流里写 C/CUDA 文本。本讲反复出现的 `VisitExpr_(const CallNode *op, ...)` 就是「遇到一个函数调用节点时该怎么办」。
- **`tl.*` intrin 的双轨制（来自 u7-l1）**：A 轨 `tl.tileop.*`（如 `tl.tileop.copy` / `tl.tileop.gemm`）会被 `LowerTileOp` 降级成低层 intrin；B 轨 `tl.*`（如 `tl.tma_load` / `tl.tl_gemm` / `tl.ptx_wgmma_ss`）只注册名字与副作用，**不经降级**，由 codegen 原样打印成 C 调用。本讲讲的 codegen 主要处理 B 轨 intrin 与上游 `builtin::*` 的最终打印。
- **GEMM 模板族（来自 u7-l2）**：`src/tl_templates/cuda/` 下有 `tl::gemm_ss/rs/sr`、`tl::wgmma_ss`、`tl::tma_load`、`tl::cp_async_gs` 等 C++ 设备模板。codegen 的工作就是**生成对这些模板的调用文本**，真正的指令（mma/wgmma/TMA）藏在模板内部，由 nvcc 实例化。
- **CUDA 驱动 API**：`cuModuleLoadData`（把 cubin/ptx 加载成 `CUmodule`）、`cuModuleGetFunction`（取出 `CUfunction` 句柄）、`cuLaunchKernel`（启动 kernel）。本讲讲运行时模块时要用到。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/target/codegen_cuda.h](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.h) | `CodeGenTileLangCUDA` 类声明，以及一组「是否需要某个头文件」的特性标志位（`need_mma_h_`、`use_nvshmem_` 等）。 |
| [src/target/codegen_cuda.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc) | 本讲主角。设备源码生成器：`AddFunction` 打印 `__global__` 函数，`VisitExpr_(CallNode)` 把每个 `tl.*` intrin 翻译成 CUDA 文本，`Finish` 按特性标志位补 `#include`。 |
| [src/target/codegen_cpp.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cpp.cc) | host 侧 C 源码生成器（`CodeGenTileLangCPP`）：打印 CPU 上的 kernel 启动器与对 packed func 的调用。 |
| [src/target/ptx.h](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/ptx.h) / [src/target/ptx.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/ptx.cc) | 内联 PTX 字符串构造器：`PrintMMAAssembly` / `PrintWGMMAAssembly` / `PrintCpAsyncAssembly` 等，以及 `Replacer` 文本替换工具。 |
| [src/target/intrin_rule_cuda.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/intrin_rule_cuda.cc) | CUDA 数学函数映射规则（`FLowerIntrinsic`）：按数据类型给函数名加后缀（`rsqrt` → `rsqrtf` / `hrsqrt`）。 |
| [src/target/rt_mod_cuda.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/rt_mod_cuda.cc) | codegen 入口 `BuildTileLangCUDA`：调用 `CodeGenTileLangCUDA` 得到源码 → 回调 nvcc 编译 → 创建运行时模块；并注册 `target.build.tilelang_cuda`。 |
| [src/runtime/tilescale_cuda_module.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/runtime/tilescale_cuda_module.cc) | 运行时模块 `TileScaleCUDAModuleNode`：加载 cubin、查 `CUfunction`、`cuLaunchKernel` 启动、注入分布式 `meta_data`。 |
| [src/op/gemm.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc) | `GemmNode::Lower`：在降级阶段拼出 `tl::gemm_ss<...>` 模板字符串，经 `tl::tl_gemm` builtin 交给 codegen 打印。 |

---

## 4. 核心概念与源码讲解

### 4.1 codegen 架构：「源码打印机」与 visitor 模式

#### 4.1.1 概念说明

TileLang 的 codegen **不直接生成机器码**，而是生成「文本」——一份 `.cu`（或 `.cpp` / `.hip`）源码文件，再交给 nvcc/hipcc 去编译。所以你可以把 `CodeGenTileLangCUDA` 理解成一台**源码打印机**：它遍历 TIR 树，把每个节点翻译成对应的 CUDA 文本，最终拼接成一段可读的 `.cu` 程序。

这层设计的好处是：

- 真正的硬件指令（mma / wgmma / TMA）封装在 `src/tl_templates/cuda/*.h` 的 C++ 模板里，codegen 只需生成「调用这些模板的语句」；
- 生成的源码可读、可调试（你可以用 `kernel.get_kernel_source()` 把它打印出来看）；
- 模板由 nvcc 实例化，能享受编译器优化与架构分发（u7-l2 讲过的 sm75/sm80/sm90/sm100 模板选择）。

整个 codegen 的入口是 `BuildTileLangCUDA`，它做完三件事：① 用 `CodeGenTileLangCUDA` 打印源码；② 回调 Python 侧的 `tilelang_callback_cuda_compile` 调 nvcc；③ 用返回的 cubin 构造运行时模块。

#### 4.1.2 核心流程

```text
IRModule(已 OptimizeForTarget)
        │
        ▼
BuildTileLangCUDA(mod, target)              ← rt_mod_cuda.cc
        │  1. CodeGenTileLangCUDA cg; cg.Init(false)
        │  2. for each PrimFunc: cg.AddFunction(gvar, f)   ← 遍历打印 __global__ 函数
        │  3. code = cg.Finish()                            ← 按特性标志位补 #include
        │  4. code = tilelang_callback_cuda_postproc(code)  ← 可选后处理
        │  5. ptx/cubin = tilelang_callback_cuda_compile(code, target, config)
        ▼
runtime::TileScaleCUDAModuleCreate(data, fmt, ExtractFuncInfo(mod), code)
        │
        ▼
TileScaleCUDAModuleNode（运行时模块，见 4.4）
```

注意第 5 步的返回值：如果回调返回的字符串以 `/` 开头（即一个文件路径，代表已编译好的 cubin 文件），`fmt` 设为 `"cubin"`；否则视为 PTX 文本，`fmt = "ptx"`。运行时模块会据此决定是直接加载二进制还是先编译 PTX。

#### 4.1.3 源码精读

codegen 入口 `BuildTileLangCUDA` 与 `target.build.tilelang_cuda` 的注册：

[rt_mod_cuda.cc:46-81](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/rt_mod_cuda.cc#L46-L81) 构造 `CodeGenTileLangCUDA`，逐个 `AddFunction`，`Finish` 取源码，依次回调 `tilelang_callback_cuda_postproc` 与 `tilelang_callback_cuda_compile`，最后 `TileScaleCUDAModuleCreate` 产出运行时模块。注意它强制要求每个函数的 `calling_conv == kDeviceKernelLaunch`（即必须是 device kernel）。

[rt_mod_cuda.cc:107-113](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/rt_mod_cuda.cc#L107-L113) 把 `BuildTileLangCUDA` 与 `BuildTileLangCUDAWithoutCompile` 注册为 TVM-FFI 全局函数 `target.build.tilelang_cuda` / `target.build.tilelang_cuda_without_compile`。这正是 u3-l5 讲过的「按 `target.kind.name` 分发」的落点——`target.kind == "cuda"` 时，TVM 的 build 框架会查到这个全局函数并调用它。

`BuildTileLangCUDAWithoutCompile`（[rt_mod_cuda.cc:83-105](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/rt_mod_cuda.cc#L83-L105)）对应 `enable_device_compile=False` 的场景：只打印源码、不调 nvcc，产物 `fmt` 固定为 `"ptx"` 占位。这是「我只要源码看一眼」的开关。

`ExtractFuncInfo`（[rt_mod_cuda.cc:10-44](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/rt_mod_cuda.cc#L10-L44)）从每个 PrimFunc 抽取参数类型与 `kKernelLaunchParams`（grid/block 标签），存进 `FunctionInfo`，供运行时启动 kernel 时知道「哪个参数是 grid 维度、哪个是 block 维度」。注意它把 `grid_constant` 指针参数标记成特殊的 `kDLGridConstant` 类型，把 `bool` 映射成 `int32`（device 运行时不直接吃 bool）。

`CodeGenTileLangCUDA` 继承自上游 TVM 的 `CodeGenC`，重写了一大批 visitor：

[codegen_cuda.h:34-75](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.h#L34-L75) 声明了类与一组 `final` 重写：`PrintType`、`VisitExpr_(CallNode)`、`VisitStmt_(ForNode/AllocateNode/...)`、`GetBufferRef` 等。还有一组私有特性标志位（[codegen_cuda.h:108-150](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.h#L108-L150)），如 `enable_fp8_`、`need_wgmma_instruction_h_`、`use_nvshmem_`、`use_distributed_`——它们在打印过程中被置位，最后在 `Finish()` 里决定要 `#include` 哪些头文件。这是一种**延迟 include** 的设计：只有真的用到了 wgmma，才 include wgmma.h。

> **关键直觉**：codegen 的输出是一段「普通的、人能读懂的」CUDA C++，它大量调用 `tl::` 命名空间下的设备模板。你看到的不是 PTX 汇编，而是 `tl::tma_load(...)`、`tl::gemm_ss<...>(...)` 这样的高层调用。

#### 4.1.4 代码实践

**实践目标**：亲手拿到 codegen 打印出来的源码，验证「源码打印机」模型。

**操作步骤**：

1. 复制 `examples/quickstart.py`，把 `@tilelang.jit` 改成先 `tilelang.lower`，或在拿到 kernel 后调用 `get_kernel_source()`。
2. 用 `print(kernel.get_kernel_source())` 打印生成的 `.cu` 源码。
3. 在源码里搜索 `tl::`，观察它调用了哪些设备模板。

**需要观察的现象**：

- 文件顶部会有一长串 `#include <tl_templates/cuda/...>`（由 `Finish()` 补齐）；
- kernel 函数签名是 `extern "C" __global__ void xxx(...)`；
- 主体里能看到 `tl::cp_async_gs` / `tl::tma_load` / `tl::gemm_ss<...>` 之类的调用，而**不是**裸的 `mma`/`wmma` PTX。

**预期结果**：你会清楚看到「TIR → 调用 `tl::` 模板的 CUDA 源码」这一层翻译结果。如果改 `num_stages`、tile 大小，模板调用的模板参数（如 `<128, 128, 32, ...>`）会随之变化。

#### 4.1.5 小练习与答案

**练习 1**：`BuildTileLangCUDA` 为什么要 `ICHECK(calling_conv == CallingConv::kDeviceKernelLaunch)`？

**参考答案**：因为这个 codegen 只负责打印 device kernel（`__global__` 函数）。host 函数在另一条 codegen（`codegen_cpp`）里打印。`kDeviceKernelLaunch` 是 u3-l1 提到的「device kernel 标记」，用它来防止把 host 函数误当成 device 函数打印。

**练习 2**：`CodeGenTileLangCUDA` 的特性标志位（如 `need_wgmma_instruction_h_`）为什么不在生成函数签名时就 include，而要拖到 `Finish()`？

**参考答案**：因为只有遍历函数体、遇到 wgmma 相关 intrin 时才知道「这个 kernel 用到了 wgmma」。延迟到 `Finish()` 统一补 include，能避免 include 用不到的头文件，也让 include 列表与实际用到的指令精确对应。

---

### 4.2 CodeGenTileLangCUDA：TIR → CUDA 源码的 emit 机制

#### 4.2.1 概念说明

`CodeGenTileLangCUDA` 打印一个 device kernel 分两步：先用 `AddFunction` 打印**函数签名与参数列表**，再递归 `PrintStmt(f->body)` 打印**函数体**。函数体里的每一条 `tir::Call`（即一个 intrin 调用）都进入同一个巨大的分发函数 `VisitExpr_(CallNode)`。

这个分发函数把 intrin 分成**两种 emit 风格**：

1. **`tl::` 模板调用风格**（主流）：打印一句形如 `tl::tma_load(desc, mbar, ...);` 或 `tl::gemm_ss<128,128,32,...>(A, B, C);` 的 C++ 调用。具体指令藏在模板里。
2. **内联 PTX 风格**（少数）：直接往源码里写一段 `__asm__ __volatile__("ptx 指令")`，或调用 `ptx.cc` 里的字符串构造器生成内联汇编。用于少量 `tl::` 模板未覆盖、或需要精确控制指令发射的场景（如带谓词的 `ldg32` 加载）。

> 提醒：`T.copy` 与 `T.gemm` 在前端只是 `tl.tileop.copy` / `tl.tileop.gemm`（A 轨），它们在 `LowerTileOp`（u3-l3）里已经被降级成低层 intrin（`tl.tma_load` / `builtin.ptx_cp_async` / `tl.tl_gemm` / `tl.ptx_wgmma_ss` 等）。所以 codegen 看到的不是 `T.copy`，而是这些低层 intrin——本节讲的就是它们如何变成文本。

#### 4.2.2 核心流程

```text
AddFunction(gvar, f)
  ├── DeclareFunction / InitFuncState / ReserveKeywordsAsUnique
  ├── PrintFuncPrefix            → "extern \"C\" __global__ "
  ├── 打印返回类型 + 函数名 + (
  ├── 遍历 params：打印每个参数类型
  │     ├── grid_constant 指针 → "__grid_constant__ const T name"
  │     ├── handle + no_alias  → "T __restrict__ name"
  │     └── readonly 标记      → "const T name"
  ├── ) {\n
  └── PrintStmt(f->body)        → 进入 visitor，逐节点打印
        └── 遇到 CallNode → VisitExpr_(CallNode) 分发：
              ├── builtin.ptx_cp_async   → "tl::cp_async_gs<N>(...)"        （模板风格）
              ├── tl.tma_load            → "tl::tma_load(desc, mbar, ...)"  （模板风格）
              ├── tl.tl_gemm             → PrintCallExtern(op_instance_str) （模板风格）
              ├── tl.ptx_wgmma_ss        → "tl::wgmma_ss<...>(...)" + Replacer（模板风格）
              ├── builtin.ptx_ldg32      → asm volatile("... ld.global ...")（内联 PTX 风格）
              └── ... 其余几十种 intrin
```

#### 4.2.3 源码精读

**(a) 函数签名与参数打印**

[codegen_cuda.cc:220-222](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L220-L222) 把函数前缀打印成 `extern "C" __global__ `，所以每个 device kernel 都是 C 链接、可被驱动 API 按名字查到的全局符号。

[codegen_cuda.cc:3660-3704](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L3660-L3704) 遍历参数列表，逐个打印类型与名字。关键细节：

- `grid_constant` 指针参数（Hopper 上 TMA 描述符等）加 `__grid_constant__ const` 前缀（[codegen_cuda.cc:3673-3679](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L3673-L3679)）；
- 若函数带 `kNoAlias` 属性且参数不在 `kNonRestrictParams` 名单里，加 `__restrict__`（[codegen_cuda.cc:3697-3699](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L3697-L3699)）；
- `tl.readonly_param_indices` 标记的参数加 `const`（[codegen_cuda.cc:3653-3658](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L3653-L3658)、[codegen_cuda.cc:3686-3689](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L3686-L3689)）。这些修饰符帮助 nvcc 做别名分析与只读缓存优化。

**(b) 模板调用风格：T.copy → cp.async / TMA**

`builtin::ptx_cp_async`（A 轨 copy 降级后的结果之一）被打印成 `tl::cp_async_gs<N>(...)` 模板调用：

[codegen_cuda.cc:1476-1500](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L1476-L1500) 取出 dst/src 指针与偏移、拷贝字节数 `size`，打印 `tl::cp_async_gs<size>(dst+offset, src+offset);`；若带第 6 个参数（谓词），改用 `tl::cp_async_gs_conditional<size>(..., condition);`。配套的 `commit_group` / `wait_group` 打印成 `tl::cp_async_commit` 与 `tl::cp_async_wait<n>`（[codegen_cuda.cc:1495-1500](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L1495-L1500)）。这里的 `<size>` / `<n>` 是 C++ 模板参数，由 codegen 直接拼进字符串。

TMA 异步加载则打印成 `tl::tma_load(...)`：

[codegen_cuda.cc:1581-1603](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L1581-L1603) 取出描述符 `desc`、mbarrier 对象与坐标参数，按 eviction policy 决定是否加 `tl::CacheHintSm90::EVICT_FIRST/LAST` 模板参数，打印 `tl::tma_load(desc, mbar, coord0, coord1, ...);`。`print_mbarrier_obj` lambda（[codegen_cuda.cc:1463-1475](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L1463-L1475)）负责把 mbarrier id 打印成 `mbarrier[id]`。

**(c) 模板调用风格：T.gemm → tl_gemm 与 wgmma_ss**

`T.gemm` 在 `LowerTileOp` 里被 `GemmNode::Lower` 降级。后者先把模板名拼成一个字符串，再用 `tl::tl_gemm` builtin 包起来：

[gemm.cc:523-541](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc#L523-L541) 根据 A/B/C 是否在 fragment 选 `tl::gemm_ss` / `gemm_rs` / `gemm_sr`，然后把 M/N/K、warp 切分、转置、stride 等拼成模板参数串 `ss << op_name << "<" << m_ << ", " << n_ << ", ...>`。

[gemm.cc:570-572](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc#L570-L572) 把这整个字符串作为 `StringImm`，连同 A/B/C 指针组成 `Call(Handle, tl::tl_gemm(), {StringImm(ss.str()), Aptr, Bptr, Cptr})`。

codegen 端只需把这个字符串原样打印成一次函数调用：

[codegen_cuda.cc:2558-2564](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L2558-L2564) 取出 `op_instance`（即上面拼好的 `tl::gemm_ss<...>` 串），调 `PrintCallExtern` 打印成 `tl::gemm_ss<128,128,32,...>(A, B, C);`。`tl_gemm_sp`（稀疏 GEMM）走完全相同的路径，只是额外把 `enable_sparse_gemm_` 置位以便 `Finish()` 补 `gemm_sp.h`（[codegen_cuda.cc:2565-2573](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L2565-L2573)）。

> 这正是 u7-l2 强调的「TIR 描述、C++ 模板落地」分层：降级阶段拼模板名，codegen 阶段只做字符串打印，真正的 mma/wgmma 指令在模板内部。

Hopper 的 wgmma 走 `tl::ptx_wgmma_ss`，但 emit 方式略有不同——它用一个**模板字面量 + 文本替换**（`Replacer`）来生成调用串：

[codegen_cuda.cc:1992-2059](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L1992-L2059) 先反序列化出 shape、A/B layout、dtype、描述符、offset 等 15 个参数；然后写一个带占位符的模板串 `"tl::wgmma_ss<(AType), (BType), (CType), (M), (N), (K), ...>(...)"`，再用 `Replacer` 把每个 `(AType)`、`(M)` 等占位符替换成实际值（如 `tl::DataType::kFloat16`、`128`）。替换完直接写进输出流。这里还把 `kFloat32` 改写成 `kTensorFloat32`（TF32），是 wgmma 的特殊要求。

**(d) 内联 PTX 风格**

少数 intrin 不走 `tl::` 模板，而是直接写一段内联汇编进源码。带谓词的只读全局加载 `ptx_ldg32` 就是一例：

[codegen_cuda.cc:2416-2446](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L2416-L2446) 直接 `this->stream << "asm volatile (\n"`，逐行打印一段 PTX：`setp.ne.b32` 设谓词、`@p ld.global.nc.f32 %0, [%1]` 做带谓词的只读加载，并用 GCC 内联汇编的 `"=f"(reg)` / `"l"(addr)` / `"r"(guard)` 约束把 C++ 变量绑到 PTX 寄存器。这种写法把 PTX 指令**字面**地嵌进生成的 `.cu` 源码。

`__ldg`（显式只读缓存加载）则打印成对 CUDA 内置函数 `__ldg(&(...))` 的调用（[codegen_cuda.cc:2447-2463](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L2447-L2463)）。此外，codege 还保留了用 `ptx.cc` 构造器生成内联 cp.async 汇编的另一条路径（[codegen_cuda.cc:2360-2379](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L2360-L2379)），即 `PrintCpAsyncAssembly` / `PrintCpAsyncBulkAsm`。它们与「`tl::` 模板」路径是同一类搬运 intrin 的两种 emit 粒度——模板路径生成高层 C++ 调用、内联 PTX 路径生成裸汇编字符串。

**(e) 分布式原语与 NVSHMEM 的打印**

当 kernel 用到远程通信原语时，codegen 会把 `tl.Putmem*` / `tl.GetPE` 等 B 轨 intrin 直接打印成 `nvshmem*` C API 调用，并置位 `use_nvshmem_`：

[codegen_cuda.cc:2758-2815](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L2758-L2815) `get_pe` → `nvshmem_my_pe()`、`putmem` → `nvshmemx_putmem_nbi_block(...)`、`barrier_all` → `nvshmem_barrier_all()` 等，每次都把 `use_nvshmem_ = true`。`Finish()` 据此 include `<nvshmem.h>` / `<nvshmemx.h>`（见 4.1）。这是 u6-l2「Python intrin → C++ Op → codegen 打印 nvshmem 文本」三段式的最后一环。

#### 4.2.4 代码实践

**实践目标**：在生成的源码里定位 `T.copy` 与 `T.gemm` 对应的模板调用片段。

**操作步骤**：

1. 用一个 matmul kernel（基于 `examples/quickstart.py`），`print(kernel.get_kernel_source())` 打印源码。
2. 在源码中搜索 `tl::gemm_ss` 或 `tl::gemm_rs`（取决于你的 A/B/C 在哪一级显存）。
3. 再搜索 `tl::tma_load` 或 `tl::cp_async_gs`（取决于目标架构是否支持 TMA）。
4. 把 target 在 Hopper（sm90）与非 Hopper 之间切换（例如 `tilelang.compile(..., target="cuda")` 探测，或显式指定 arch），重新打印源码对比。

**需要观察的现象**：

- `T.gemm` 变成一行 `tl::gemm_ss<block_M, block_N, block_K, warp_m, warp_n, ...>(A_shared, B_shared, C_fragment);`，模板参数与你在 kernel 里写的 tile 大小一一对应；
- `T.copy`（global→shared）在 sm90 上变成 `tl::tma_load(...)`，在 sm80 上变成 `tl::cp_async_gs<16>(...)` 或类似；
- 改 `num_stages` 后，循环结构（prologue/body/epilogue）与 mbarrier 的 `expect_tx`/`wait` 调用数量会变化（承接 u4-l2 软件流水）。

**预期结果**：你能用一句话描述「我的 `T.gemm` 在源码里是这一行模板调用」，并把模板参数解释回 tile 大小与 warp 切分。若无法本地运行 GPU，可改为纯源码阅读：跟踪 [gemm.cc:535](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc#L535) 的 `ss << op_name << "<"...` 到 [codegen_cuda.cc:2563](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L2563) 的 `PrintCallExtern` 这条链，标注每段字符串的来源。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `tl_gemm` 的第一个参数是一个 `StringImm`（字符串），而不是结构化的字段？

**参考答案**：因为 GEMM 的模板参数（M/N/K、warp 切分、转置、stride、架构开关等）组合非常多，且最终都要拼成一个 C++ 模板名 `tl::gemm_ss<...>`。降级阶段（`GemmNode::Lower`）最清楚这些参数，直接拼成字符串最简单；codegen 只需把这个字符串当函数名打印即可，不必理解其内部结构。这是一种「把 C++ 模板实例化的决策权交给降级阶段、codegen 只做透传」的分工。

**练习 2**：`ptx_wgmma_ss` 用 `Replacer` 做文本替换，相比直接 `<<` 拼接有什么好处？

**参考答案**：wgmma 调用串很长且参数众多（类型枚举、M/N/K、tnsp 标志、scale 标志、描述符、offset 等）。用占位符模板串 + 替换表，能让「调用骨架」与「参数取值」分离，可读性好、改动时不易漏掉某个参数；也方便统一处理 `kFloat32 → kTensorFloat32` 这类枚举重映射。

---

### 4.3 PTX 字符串生成与 intrin_rule 指令映射

#### 4.3.1 概念说明

codegen 的两种 emit 风格里，「内联 PTX」需要精确拼出 PTX 汇编字符串。`ptx.cc` 就是这套**字符串构造器**的集合：每个函数返回一段 `__asm__ __volatile__("...")` 文本，涵盖 mma、wgmma、cp.async、mbarrier、ldmatrix 等。它与 `ptx.h` 里定义的 `ptx::DataType` 枚举、`Replacer` 替换器配合使用。

另一条独立的线是 **intrin_rule**：它解决「`T.exp` / `T.rsqrt` 这类数学函数该翻译成哪个 CUDA 内置函数」的问题。比如对 `float`，`exp` 应变成 `expf`；对 `half`，变成 `hexp`；对 `rsqrt`，变成 `__rsqrtf`。这是 TVM 的 `FLowerIntrinsic` 机制——给每个 tir op 在每个后端上注册一个「如何 lower」的规则。

> 二者的区别：`ptx.cc` 服务于**张量核心 / 异步拷贝**等低层指令的字符串拼接；`intrin_rule` 服务于**标量数学函数**到 CUDA 内置函数名的映射。它们都是「指令映射」，但作用域不同。

#### 4.3.2 核心流程

```text
【内联 PTX 路径】
降级产生的 builtin.ptx_mma / tl.ptx_wgmma_ss / builtin.ptx_cp_async 等
        │ (codegen VisitExpr_(CallNode) 里调用)
        ▼
ptx.cc: PrintMMAAssembly / PrintWGMMAAssembly / PrintCpAsyncAssembly / Print*BarrierAsm
        │  按 dtype enum、shape、layout 选 PTX 指令模板
        │  用 Replacer 替换寄存器/地址占位符
        ▼
返回一段含 "mma.sync ...\n" / "cp.async ...\n" 的字符串 → 写进 .cu 源码

【intrin_rule 路径】
tir.exp / tir.rsqrt 等 op（在 LowerTileOp 之前的普通数学调用）
        │  TVM 按 target 查 "cuda.FLowerIntrinsic"
        ▼
intrin_rule_cuda.cc: CUDAMath / CUDAFastMath
        │  按 dtype.bits() 给函数名加后缀：exp→expf / hexp / expl
        ▼
返回新的 Call 节点（函数名已替换）→ codegen 当普通函数调用打印
```

#### 4.3.3 源码精读

**(a) PTX 数据类型与 Replacer**

[ptx.h:45-69](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/ptx.h#L45-L69) 定义 `ptx::DataType` 枚举，覆盖从 `kInt4` / `kUInt4` 到 `kFloat8_e4m3` / `kBFloat16` / `kTensorFloat32` / `kBit1` 等所有 PTX 基本与矩阵数据类型。`PrintMMAAssembly` 等函数接收 dtype 字符串，内部转成这个枚举来选 PTX 指令的 `.f16` / `.bf16` / `.e4m3` 后缀。

[ptx.h:96-119](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/ptx.h#L96-L119) 是 `Replacer` 类：`register_rule(pattern, replacement)` 注册替换规则，`rewrite(str)` 顺序做字符串替换。注释里提到「等迁移到 C++20 后应改用 `std::format`」——它本质上是一个简易的 `std::format` 替身，用来把带占位符的 PTX 模板串填上实际寄存器名与地址。

**(b) MMA / WGMMA 汇编构造**

[ptx.h:142-152](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/ptx.h#L142-L152) 声明 `PrintMMAAssembly`：接收 shape（如 `"16x8x16"`）、A/B layout（row/col）、A/B/C dtype、A/B/C 指针与 offset、metadata（稀疏用）、bit_op（1-bit mma 的 xor/and）、saturate 等参数，返回一段 `mma.sync` PTX 文本。它在 codegen 里被 mma 路径调用（[codegen_cuda.cc:1987-1991](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L1987-L1991)）。

[ptx.h:163-174](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/ptx.h#L163-L174) 声明 `PrintWGMMAAssembly`：参数类似但多了 `a_is_k_major` / `b_is_k_major`、`scale_out` / `scale_in_a/b`、`a_is_shared`、描述符 `a_desc` / `b_desc` 等，返回 `wgmma.mma_async` PTX 文本。注意它接收的是**共享内存描述符**（descriptor），这正是 u7-l2 讲过的 wgmma 「ss 变体操作数为 shared memory 描述符」的体现。

**(c) cp.async 与 mbarrier 汇编构造**

[ptx.h:201-205](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/ptx.h#L201-L205) 声明 `PrintCpAsyncAssembly`：接收 shared/global 指针与 offset、字节数（4/8/16），返回 `cp.async.ca.shared.global` PTX。还有带谓词的 `PrintPredicatedCpAsyncAssembly`（[ptx.h:216-219](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/ptx.h#L216-L219)）。它们对应 codegen 里 [codegen_cuda.cc:2361-2366](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L2361-L2366) 的内联 PTX 路径。

mbarrier 相关的 `PrintInitBarrierThreadCountAsm` / `PrintArriveBarrierAsm` / `PrintArriveBarrierExpectTxAsm` / `PrintWaitBarrierAsm`（[ptx.h:248-270](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/ptx.h#L248-L270)）生成 `mbarrier.init` / `mbarrier.arrive` / `mbarrier.arrive.expect_tx` / `mbarrier.try_wait` 等 PTX——这些正是 u4-l2/u4-l3 软件流水与 warp 特化里 mbarrier 握手的底层指令文本。

> **直觉总结**：`ptx.cc` 是一台「PTX 文本机床」。降级阶段决定「要发一条 mma」，codegen 调 `PrintMMAAssembly`，它根据 dtype/shape/layout 把 `mma.sync` 指令的精确文本造出来，嵌进 `.cu` 源码。模板路径（`tl::gemm_ss`）与内联 PTX 路径（`PrintMMAAssembly`）是两种粒度的选择：前者把细节藏进 C++ 模板、后者直接写汇编。

**(d) intrin_rule：数学函数映射**

[intrin_rule_cuda.cc:17-57](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/intrin_rule_cuda.cc#L17-L57) 定义 `CUDAMath` 仿函数：输入 `DataType t` 与函数名 `name`，按类型返回带后缀的 CUDA 函数名。规则很直观：

- `float64` → 原名（`exp`）；`float32` → 加 `f`（`expf`）；`float16` → 加 `h` 前缀（`hexp`），但 `fabs` 特殊化为 `__habs`、`round` 为 `hrint`；
- `bfloat16` → 同 float16 规则；
- `int32` → 加 `__` 前缀（`__abs`）；`int64` → 加 `__` + `ll` 后缀（`__absll`）。

[intrin_rule_cuda.cc:133-135](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/intrin_rule_cuda.cc#L133-L135) 注册 `tir.rsqrt` 在 cuda 后端的 lower 规则为 `DispatchPureExtern<CUDAMath>`——即把 `rsqrt(x)` 当外部函数调用，名字按 `CUDAMath` 规则改写（`float32` → `rsqrtf`，`float16` → `hrsqrt`）。TVM 的 `FLowerIntrinsic` 机制会在 lowering 时查 `"cuda.FLowerIntrinsic"` 这个属性，找到规则后把 `Call` 节点的 op 名替换掉，替换后的节点再交回 codegen 当普通函数调用打印。

[intrin_rule_cuda.cc:106-117](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/intrin_rule_cuda.cc#L106-L117) 还定义了 `CUDAWarpIntrinsic`：把通用的 `tvm_warp_shuffle` 映射成 CUDA 的 `tir.cuda.__shfl_sync` / `__shfl_up_sync` / `__shfl_down_sync`（[intrin_rule_cuda.cc:124-131](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/intrin_rule_cuda.cc#L124-L131) 的 `DispatchCUDAShuffle`）。这是 reduce 里 warp 内 shuffle 规约（u2-l3 提到的 `warp_reduce_*`）落到 `__shfl_sync` 的映射点。

#### 4.3.4 代码实践

**实践目标**：观察 `intrin_rule` 如何改变生成源码里的数学函数名。

**操作步骤**：

1. 写一个 elementwise kernel，内部用 `T.rsqrt(x)` 或 `T.exp(x)`。
2. 分别用 `float32` 与 `float16`（half）作为计算 dtype，`print(kernel.get_kernel_source())`。
3. 在两份源码里搜索 `rsqrt` / `exp`。

**需要观察的现象**：

- `float32` 版本里是 `rsqrtf(...)` / `expf(...)`；
- `float16` 版本里是 `hrsqrt(...)` / `hexp(...)`；
- 这与 [intrin_rule_cuda.cc:19-33](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/intrin_rule_cuda.cc#L19-L33) 的分支完全对应。

**预期结果**：你能在源码里直接看到后缀/前缀的差异，印证「数学函数名是按数据类型动态拼出来的」。若本地无 GPU，改为阅读 `CUDAMath::operator()` 的 switch 分支，逐类型预测函数名。

#### 4.3.5 小练习与答案

**练习 1**：`ptx::DataType` 枚举里为什么要把 `kFloat32` 和 `kTensorFloat32` 分开？

**参考答案**：在 PTX/wgmma 语境下，`float32` 累加可以走「TF32 输入」（tensor float 32，19 位精度）模式。mma/wgmma 指令的 `.tf32` 后缀要求输入虽存在 32 位寄存器里、但只取 TF32 精度。所以枚举上要把「逻辑 float32」与「TF32 输入」区分开，方便 `PrintMMAAssembly` 选对的指令后缀。codegen 里 [codegen_cuda.cc:2033-2034](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L2033-L2034) 把 `kFloat32` 改写成 `kTensorFloat32` 也是同一原因。

**练习 2**：`intrin_rule_cuda.cc` 里只看到注册了 `tir.rsqrt` 一个规则，那 `exp` / `log` / `sin` 这些是怎么映射的？

**参考答案**：TileLang 复用上游 TVM 的 intrin 规则体系。上游 TVM 已经为 cuda 后端注册了大量数学函数的 `FLowerIntrinsic` 规则（`exp`/`log`/`sin`/`cos` 等）。TileLang 在 [intrin_rule_cuda.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/intrin_rule_cuda.cc) 只**补充**自己额外需要的规则（如 `rsqrt`、warp shuffle 的特殊路径），其余沿用上游。这是「站在 TVM 肩膀上」的典型做法。

---

### 4.4 rt_mod_cuda：运行时模块的加载与 kernel 启动

#### 4.4.1 概念说明

codegen 打印出 `.cu` 源码、nvcc 编出 cubin（或 PTX）之后，产物被包成一个**运行时模块** `TileScaleCUDAModuleNode`。它是 codegen 与「真正跑 kernel」之间的桥梁，承担三件事：

1. **加载**：用 CUDA 驱动 API 把 cubin/ptx 加载成 `CUmodule`，按函数名取出 `CUfunction` 句柄；
2. **启动**：把 Python 传进来的张量指针打包成 `void**`，连同 grid/block 维度与动态共享内存大小，调 `cuLaunchKernel`；
3. **分布式注入**：对分布式 kernel，把 host 侧算好的远程基址表拷贝到 device 的 `__constant__ meta_data` 符号。

这个模块是 TileScale 对上游 TVM `CUDAModuleNode` 的扩展——多了 `meta_data` 注入这条分布式链路（承接 u6-l3/u6-l5）。

#### 4.4.2 核心流程

```text
TileScaleCUDAModuleCreate(data, fmt, fmap, cuda_source)
        │
        ▼
TileScaleCUDAModuleNode（持有 cubin/ptx 字节、fmap、源码）
        │
        ▼  GetFunction(kernel_name)
        │  ├── name == "__tilescale_init_table" → TileScaleInitDistributedTable
        │  ├── name == tvm_prepare_global_barrier → PrepGlobalBarrier
        │  └── 其它 → TileScaleCUDAWrappedFunc
        ▼
TileScaleCUDAWrappedFunc::operator()(args)
        │  1. launch_param_config_.Extract(args) → grid_dim / block_dim / dyn_shmem
        │  2. fcache_[dev] = m_->GetFunc(dev, name)   ← 首次：cuModuleLoadData + cuModuleGetFunction
        │  3. 若 dyn_shmem > 0：cuFuncSetAttribute(MAX_DYNAMIC_SHARED_SIZE_BYTES)
        │  4. strm = device_api.cuda.get_stream(dev)
        │  5. cuLaunchKernel(func, grid, block, dyn_shmem, strm, void_args, nullptr)
```

#### 4.4.3 源码精读

**(a) 模块类与生命周期**

[tilescale_cuda_module.cc:66-91](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/runtime/tilescale_cuda_module.cc#L66-L91) 定义 `TileScaleCUDAModuleNode`，持有 `data_`（cubin/ptx 字节）、`fmt_`（`"cubin"` 或 `"ptx"`）、`fmap_`（函数信息）、`cuda_source_`，以及**每张卡一个**的 `module_` 数组（`kTileScaleMaxNumGPUs` 个 `CUmodule`）。`kind()` 返回 `"tilescale_cuda"`，`GetPropertyMask` 标记自己既可序列化（`kBinarySerializable`）又可运行（`kRunnable`）。析构时逐卡 `cuModuleUnload`（[tilescale_cuda_module.cc:76-83](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/runtime/tilescale_cuda_module.cc#L76-L83)）。

`WriteToFile` / `SaveToBytes` / `InspectSource`（[tilescale_cuda_module.cc:93-128](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/runtime/tilescale_cuda_module.cc#L93-L128)）支持把模块序列化（落盘 `.cubin`/`.cu`/`.ptx`）或取出源码，这是 `get_kernel_source()` 与磁盘缓存的底层支撑。

**(b) 加载：cuModuleLoadData + cuModuleGetFunction**

[tilescale_cuda_module.cc:131-151](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/runtime/tilescale_cuda_module.cc#L131-L151) 是 `GetFunc(device_id, func_name)`：首次调用时 `cuModuleLoadData` 把 cubin/ptx 加载成 `CUmodule`，然后 `cuModuleGetFunction` 按名字取出 `CUfunction` 句柄。注意加载后立即查 `runtime.nvshmem.cumodule_init` 全局函数——如果存在（即分布式/NVSHMEM 场景），就把这个 `CUmodule` 传给它做 NVSHMEM 初始化（如注册对称堆）。这是 NVSHMEM 路线在运行时的挂载点（承接 u6-l4）。

`GetGlobal`（[tilescale_cuda_module.cc:154-178](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/runtime/tilescale_cuda_module.cc#L154-L178)）类似，但取的是 `__constant__` / `__device__` 全局变量（如 `meta_data`）的设备指针 `CUdeviceptr`，并 `ICHECK_EQ(nbytes, expect_nbytes)` 校验大小。

**(c) 启动：cuLaunchKernel**

[tilescale_cuda_module.cc:246-294](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/runtime/tilescale_cuda_module.cc#L246-L294) 是 `TileScaleCUDAWrappedFunc::operator()`，即「调用 kernel」的真正落点：

1. `launch_param_config_.Extract(args)` 从调用参数里抽出 grid/block 维度与动态共享内存大小（`ThreadWorkLoad wl`）；
2. 首次调用缓存 `CUfunction`：`fcache_[device_id] = m_->GetFunc(...)`（[tilescale_cuda_module.cc:251-253](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/runtime/tilescale_cuda_module.cc#L251-L253)）；
3. 若用到动态共享内存，`cuFuncSetAttribute(CU_FUNC_ATTRIBUTE_MAX_DYNAMIC_SHARED_SIZE_BYTES, ...)` 申请超额 shared memory，并按 device 缓存上次设置的大小避免重复设（[tilescale_cuda_module.cc:255-270](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/runtime/tilescale_cuda_module.cc#L255-L270)）；
4. 通过 `device_api.cuda.get_stream` 取当前 CUDA stream（[tilescale_cuda_module.cc:272-277](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/runtime/tilescale_cuda_module.cc#L272-L277)），保证 kernel 跑在调用方的 stream 上（与 torch 等 framework 对齐）；
5. [tilescale_cuda_module.cc:278-281](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/runtime/tilescale_cuda_module.cc#L278-L281) 调 `cuLaunchKernel(func, grid_dim, block_dim, dyn_shmem_size, strm, void_args, nullptr)`。`void_args` 是张量指针打包数组，由 `PackFuncVoidAddr` 在外层完成。

启动失败时会把 grid/block 维度拼进错误信息，方便排查「shared memory 不够」「grid 过大」等问题（[tilescale_cuda_module.cc:282-293](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/runtime/tilescale_cuda_module.cc#L282-L293)）。

**(d) GetFunction 分发与分布式 meta_data 注入**

[tilescale_cuda_module.cc:332-356](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/runtime/tilescale_cuda_module.cc#L332-L356) 是 `GetFunction(name)` 总分发：

- `name == "__tilescale_init_table"` → 返回 `TileScaleInitDistributedTable`（分布式专用）；
- `name == tvm_prepare_global_barrier` → 返回 `TileScaleCUDAPrepGlobalBarrier`（全局屏障初始化，`cuMemsetD32` 清零全局屏障计数器，[tilescale_cuda_module.cc:308-330](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/runtime/tilescale_cuda_module.cc#L308-L330)）；
- 否则查 `fmap_`，用 `TileScaleCUDAWrappedFunc` 包成一个 `PackedFunc` 返回。

`TileScaleInitDistributedTable` 是 TileScale 相对上游 TVM 最关键的扩展：

[tilescale_cuda_module.cc:190-222](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/runtime/tilescale_cuda_module.cc#L190-L222) 接收 host 侧的基址表指针与大小、stream，先通过 `GetGlobal(device_id, "meta_data", kMetaDataSize)` 取出 device 端 `__constant__ uint64_t meta_data[1024]` 的设备指针（首次缓存进 `pcache_`），再用 `cuMemcpyHtoD` 把 host 表拷过去。注释明确指出：**必须用驱动 API `cuMemcpyHtoD` 而非 `cudaMemcpyToSymbol`**，因为这个 symbol 住在动态加载的 `CUmodule` 里。

这条链路与 codegen 的 [codegen_cuda.cc:326-331](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L326-L331) 严格对应——codegen 在 `use_distributed_` 时打印 `extern "C" __constant__ uint64_t meta_data[1024];` 与 `#include <tl_templates/cuda/distributed.h>`，运行时再把 allocator 算出的远程基址表（u6-l5）写进去。device 侧的 `get_rank` / `get_remote_base_ptr` 就是从这块 `meta_data` 里按 `0 / 1 / 2+rank` 偏移读取（u6-l3/u6-l5）。

#### 4.4.4 代码实践

**实践目标**：跟踪一次 `kernel(...)` 调用，看它如何走到 `cuLaunchKernel`。

**操作步骤**（源码阅读型实践）：

1. 从 Python 调用 `kernel(A, B, C)` 出发，跟踪 `JITKernel.__call__`（u3-l6）如何拿到 `rt_mod`，再调 `rt_mod.get_function(kernel_name)`。
2. 在 [tilescale_cuda_module.cc:333](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/runtime/tilescale_cuda_module.cc#L333) 的 `GetFunction` 确认它返回的是 `TileScaleCUDAWrappedFunc`。
3. 顺着 [tilescale_cuda_module.cc:246](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/runtime/tilescale_cuda_module.cc#L246) 的 `operator()` 走到 `cuLaunchKernel`（[tilescale_cuda_module.cc:278](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/runtime/tilescale_cuda_module.cc#L278)）。
4. 画出调用链：`Python kernel(...) → JITKernel → rt_mod.get_function → TileScaleCUDAWrappedFunc::operator() → cuLaunchKernel`。

**需要观察的现象**：

- grid/block 维度来自你 `T.Kernel(grid_x, grid_y, threads=...)` 的声明，经 `launch_param_config_` 解析；
- 首次调用会触发 `cuModuleLoadData`（有一次性编译/加载开销），之后 `fcache_` 命中；
- 若 kernel 用了动态 shared memory（`T.alloc_shared` 在动态区），会看到 `cuFuncSetAttribute` 调整上限。

**预期结果**：你能完整解释「Python 一个 `kernel(...)` 调用，最终变成一次 `cuLaunchKernel(func, grid, block, shmem, stream, args)`」。这是把本讲与 u3-l6（JIT 适配器）缝起来的关键一环。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `TileScaleCUDAModuleNode` 要为每张卡各维护一个 `CUmodule`，而不是全局一个？

**参考答案**：因为 `cuModuleLoadData` 加载出的 `CUmodule` 与当前 device 上下文绑定，且 NVSHMEM 初始化（`runtime.nvshmem.cumodule_init`）也是按 device 做的（每张卡的对称堆不同）。多卡场景下，kernel 可能在不同卡上启动，故按 `device_id` 缓存独立的 `CUmodule`（`module_[device_id]`），并用 `mutex_` 保护惰性加载。

**练习 2**：`__tilescale_init_table` 为什么必须用 `cuMemcpyHtoD` 而非 `cudaMemcpyToSymbol`？

**参考答案**：`cudaMemcpyToSymbol` 只能用于**静态链接**进程序的全局符号；而 `meta_data` 是 cubin 里、由 `cuModuleLoadData` **动态加载**到 `CUmodule` 中的 symbol，运行时只能拿到它的设备指针 `CUdeviceptr`。驱动 API `cuMemcpyHtoD` 直接按设备指针拷贝，不依赖符号的静态可见性，所以这里必须用它。注释（[tilescale_cuda_module.cc:214-218](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/runtime/tilescale_cuda_module.cc#L214-L218)）明确说明了这一点。

---

### 4.5 host codegen 与多后端（hip / cutedsl）

#### 4.5.1 概念说明

前面四节聚焦 CUDA device 侧。但一个完整的编译产物还有 **host 侧代码**：一段 CPU 上的 C 函数，负责把张量指针打包、调起 device kernel、处理与 Python/框架的 packed func 交互。这是 `CodeGenTileLangCPP` 的职责。此外，TileLang 自实现了多个与 CUDA 平行的 codegen 后端——HIP（AMD ROCm）、CuTeDSL、C host——它们与 CUDA 后端结构同构。

#### 4.5.2 核心流程

```text
【host 侧】
host_mod（SplitHostDevice 产出）
        │
        ▼
CodeGenTileLangCPP（codegen_cpp.cc）
        │  AddFunction：打印 CPU 启动器函数
        │  VisitExpr_(CallNode)：
        │     tvm_call_packed_lowered → TVMBackendGetFuncFromEnv + TVMFuncCall
        │     tvm_call_cpacked_lowered → 带 resource_handle 的 C 调用
        ▼
打印成 .cpp → 编进 host 库

【多后端】（与 cuda 同构）
target=cuda   → target.build.tilelang_cuda   (CodeGenTileLangCUDA)   + tilescale_cuda rt_mod
target=rocm   → target.build.tilelang_hip    (CodeGenTileLangHIP)    + rt_mod_hip
target=cutedsl→ target.build.tilelang_cutedsl_without_compile (CodeGenTileLangCuTeDSL)
target=c      → target.build.tilelang_c      (CodeGenTileLangCHost)
```

#### 4.5.3 源码精读

**(a) host codegen：打印 CPU 启动器**

[codegen_cpp.cc:41-56](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cpp.cc#L41-L56) `CodeGenTileLangCPP::Init` 在 decl_stream 里写 `// tilelang target: ...` 注释与 `#include <tl_templates/cpp/common.h>` / `<tl_templates/cpp/gemm.h>`。说明 host 侧也可能调用一些 `tl::` 的 cpp 模板（如 host 侧的参考实现）。

[codegen_cpp.cc:254-319](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cpp.cc#L254-L319) `AddFunction` 打印 host 函数签名：`extern "C"` 前缀（[codegen_cpp.cc:90-94](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cpp.cc#L90-L94)），参数里同样处理 `grid_constant` 与 `__restrict__`。host 函数不是 `__global__`，而是普通 C 函数。

[codegen_cpp.cc:382-421](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cpp.cc#L382-L421) 是 host 侧 `VisitExpr_(CallNode)`，核心处理两类 packed func 调用：

- `tvm_call_packed_lowered`：先 `TVMBackendGetFuncFromEnv` 按名字从环境查到目标函数（如 device kernel 的启动入口），再 `TVMFuncCall` 调用它（[codegen_cpp.cc:182-224](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cpp.cc#L182-L224) 的 `PrintGetFuncFromBackend` / `PrintFuncCall`）；
- `tvm_call_cpacked_lowered`：带 `resource_handle` 的 C 风格直接调用（[codegen_cpp.cc:226-252](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cpp.cc#L226-L252)）。

这就是 host/device 协作的文本层：host 函数负责打包参数与按名字查找 device 入口，device 函数（4.2 节打印的 `__global__`）负责真正计算。

**(b) 平行后端**

CUDA 后端是「模板」，HIP 与 cutedsl 与之同构：

- **HIP**（AMD ROCm）：[codegen_hip.h:20](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_hip.h#L20) 声明 `CodeGenTileLangHIP : public CodeGenC`，结构与 CUDA 几乎一致（HIP 是 CUDA-like 的 ROCm 编程模型），[rt_mod_hip.cc:121-122](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/rt_mod_hip.cc#L121-L122) 注册 `target.build.tilelang_hip`。差别在打印的设备模板与运行时用的是 HIP/ROCm API（`hipModuleLoadData` / `hipLaunchKernel`）。
- **CuTeDSL**：[codegen_cutedsl.h:21](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cutedsl.h#L21) 声明 `CodeGenTileLangCuTeDSL : public CodeGenTileLangPY`，[rt_mod_cutedsl.cc:64](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/rt_mod_cutedsl.cc#L64) 注册 `target.build.tilelang_cutedsl_without_compile`（注意它只有 without_compile 变体，不走 nvcc）。
- **C host**：[codegen_c_host.cc:507](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_c_host.cc#L507) 注册 `target.build.tilelang_c`，用于 CPU / 调试。

这些后端共享 u3-l5 讲过的「按 `target.kind.name` 分发到 `target.build.tilelang_*`」机制，只是各自的源码打印机与运行时模块不同。CUDA 后端是其中最完整、最复杂、也最能代表 TileScale 能力的那一个。

#### 4.5.4 代码实践

**实践目标**：比较 CUDA 与 HIP 两套 codegen 的同构性。

**操作步骤**（源码阅读型）：

1. 打开 [codegen_cuda.h:34](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.h#L34) 与 [codegen_hip.h:20](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_hip.h#L20)，对比两个类的继承关系与重写方法列表。
2. 对比 [rt_mod_cuda.cc:110](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/rt_mod_cuda.cc#L110) 与 [rt_mod_hip.cc:121](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/rt_mod_hip.cc#L121) 的注册名。
3. 在 `codegen_hip.cc` 中找 GEMM/copy 对应的 emit 分支，看它打印的是 `tl::` 模板还是 HIP 专用模板。

**需要观察的现象**：两套 codegen 的类结构、visitor 重写、入口函数命名高度对称，差异主要在打印的指令模板与底层运行时 API。

**预期结果**：你能说出「新增一个后端 = 复制一套 codegen_xxx + rt_mod_xxx + 注册 target.build.tilelang_xxx」这个规律。这是 u7-l4（transform pass 扩展）之外、另一条「二次开发」的扩展路径。

#### 4.5.5 小练习与答案

**练习 1**：host codegen 里 `tvm_call_packed_lowered` 为什么要在调用前先 `TVMBackendGetFuncFromEnv`？

**参考答案**：host 函数在编译期不知道它要调用的 device kernel 入口的函数指针——这个入口是运行时由 `TileScaleCUDAModuleNode::GetFunction` 动态返回的 `PackedFunc`。`TVMBackendGetFuncFromEnv(module, "name", &ptr)` 是 TVM 运行时的「按名字查函数」机制：首次调用时从所在模块环境里查出目标函数指针并缓存（`if (ptr == NULL) {...}`），之后直接用缓存指针调 `TVMFuncCall`。这解耦了 host 代码与 device 函数的绑定时机。

**练习 2**：为什么 cutedsl 后端只注册了 `tilelang_cutedsl_without_compile`，没有「真编译」版本？

**参考答案**：CuTeDSL（CUTe Domain Specific Language）走的是 Python 端的动态生成与 JIT 路径，不像 CUDA 那样需要 codegen 出 `.cu` 再调 nvcc 产 cubin。它生成的是 CuTeDSL 的 Python/C++ 中间表示，由 CuTeDSL 运行时自己处理实例化，所以不需要 codegen 阶段的 nvcc 回调，只提供 without_compile 变体即可。

---

## 5. 综合实践

把本讲四块知识串起来，做一次「全链路追踪」：

1. **写 kernel**：基于 `examples/quickstart.py`，写一个 matmul kernel（`T.copy` + `T.gemm` + `T.Pipelined`）。
2. **取源码**：`print(kernel.get_kernel_source())`，在生成的 `.cu` 里：
   - 顶部标注哪些 `#include` 来自 [codegen_cuda.cc:267-343](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L267-L343) 的 `Finish()`（按你用到的指令种类）；
   - 在函数体里圈出 `T.copy` 对应的 `tl::tma_load` / `tl::cp_async_gs`（4.2 节）、`T.gemm` 对应的 `tl::gemm_ss<...>`（4.2 节）；
   - 若用到 `T.rsqrt` / `T.exp`，圈出 `rsqrtf` / `expf`，对应 4.3 节的 intrin_rule。
3. **画调用链**：画出从 `Python kernel(A,B,C)` 到 `cuLaunchKernel` 的完整链路，标注每一步落在哪个文件（u3-l6 的 JITKernel → 本讲 4.4 的 `TileScaleCUDAWrappedFunc`）。
4. **（可选，需多卡）**：若你跑过分布式示例（u6），在源码里找到 `__constant__ uint64_t meta_data[1024];`（[codegen_cuda.cc:330](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L330)），并跟踪 `kernel.initialize(allocator=...)` 如何触发 `__tilescale_init_table` → `cuMemcpyHtoD`（4.4 节）把基址表写进这块 constant memory。

最终产出：一份带注释的生成源码 + 一张调用链时序图，能向别人讲清「我的 TileLang 代码是怎么变成 GPU 上跑的指令的」。

## 6. 本讲小结

- TileLang codegen 是一台**源码打印机**：遍历 TIR、用 visitor 把每个节点翻译成 CUDA/C 文本，最终产物是一份可读的 `.cu`，再交 nvcc 编译。入口是 `BuildTileLangCUDA`，注册为 `target.build.tilelang_cuda`。
- `CodeGenTileLangCUDA::VisitExpr_(CallNode)` 用一个巨大的 if-else 链分发每个 `tl.*` / `builtin.*` intrin，主流走 **`tl::` 模板调用风格**（`tl::tma_load` / `tl::cp_async_gs` / `tl::gemm_ss<...>` / `tl::wgmma_ss<...>`），少数走**内联 PTX 风格**（`asm volatile(...)`）。`T.copy`/`T.gemm` 在到达 codegen 前已被 `LowerTileOp` 降级成这些低层 intrin。
- `ptx.cc` 是 PTX 文本构造器（`PrintMMAAssembly` / `PrintWGMMAAssembly` / `PrintCpAsyncAssembly` / mbarrier 系列），配合 `ptx::DataType` 枚举与 `Replacer` 字符串替换；`intrin_rule_cuda.cc` 用 `FLowerIntrinsic` 把数学函数按数据类型映射成 CUDA 内置名（`rsqrt`→`rsqrtf`/`hrsqrt`、warp shuffle→`__shfl_sync`）。
- `TileScaleCUDAModuleNode` 是运行时模块：`cuModuleLoadData` 加载 cubin、`cuModuleGetFunction` 取句柄、`cuLaunchKernel` 启动，并按 device 缓存 `CUmodule`/`CUfunction`、按需设置动态 shared memory 上限、从 `device_api.cuda.get_stream` 取 stream。
- 相比上游 TVM 的 `CUDAModule`，TileScale 多了 `__tilescale_init_table`：用 `cuMemcpyHtoD` 把远程基址表写进 device 的 `__constant__ meta_data[1024]`，支撑 CP-engine 分布式远程寻址；加载 `CUmodule` 后还会触发 `runtime.nvshmem.cumodule_init` 钩子做 NVSHMEM 初始化。
- host 侧由 `CodeGenTileLangCPP` 打印 CPU 启动器（`tvm_call_packed_lowered` → `TVMBackendGetFuncFromEnv` + `TVMFuncCall`）；HIP / CuTeDSL / C 是与 CUDA 同构的平行后端，各自注册 `target.build.tilelang_*`。

## 7. 下一步学习建议

- **向上回看**：结合 u3-l4（`OptimizeForTarget`）与 u3-l5（codegen 总览），对照本讲看清「IR 在哪一步变成低层 intrin、codegen 在哪一步把它打印成文本」的完整交接点。
- **向设备模板深入**：本讲只讲到「codegen 打印 `tl::gemm_ss<...>` 调用」。这些模板的内部实现（mma/wgmma/tcgen05 指令封装、按 SM 架构分发）是 u7-l2 的内容，建议带着本节打印出来的源码去读 `src/tl_templates/cuda/gemm.h` / `gemm_sm90.h`，把「调用」与「实现」对上。
- **向 pass 扩展深入**：如果你想在 codegen 之前再加一道变换，阅读 u7-l4（Transform pass 深入与扩展），学习如何注册新的 `tl.*` builtin（参考 [builtin.cc:42-48](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/builtin.cc#L42-L48) 的 `TIR_DEFINE_TL_BUILTIN`）并在 `codegen_cuda.cc` 的 `VisitExpr_(CallNode)` 里加一个 emit 分支。
- **向分布式运行时深入**：本讲的 `meta_data` 注入与 NVSHMEM 钩子是 u6 单元的运行时基石。若要理解「远程基址表是怎么算出来的」，回到 u6-l5（IPC 张量与 tilescale_ext 内存管理）读 `get_allocator` 与 `kernel.initialize`。
