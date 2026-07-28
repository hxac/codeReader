# tl_templates 模板下译

## 1. 本讲目标

本讲承接 u5-l3（CUDA/HIP codegen 后端），把视角从「codegen 印出源码文本」往前推一步，落到**这些源码文本里调用的那一大堆 `tl::` 设备函数到底从哪里来**。

学完本讲，你应当能够：

- 说清 `src/tl_templates/` 这个目录在整个编译器里扮演什么角色——它不是被编进 `libtilelang.so` 的逻辑，而是被 **`#include` 进生成代码**的设备端 C++ 模板库。
- 区分 CUDA 三代张量核指令封装：`mma.h`（Volta–Ampere，`mma.sync`）、`wgmma.h`（Hopper，`wgmma.mma_async`）、`tcgen05mma.h`（Blackwell，`tcgen05.mma`）。
- 读懂 HIP 与 MACA 的 `gemm.h` 模板，理解它们各自用什么方式发射矩阵乘指令（HIP 手写 swizzle + `__builtin_amdgcn_mfma_*`；MACA 走 CUTE 的 `TiledMMA`）。
- 把「codegen 的 `need_*` 标志 → 生成代码里的 `#include` → 对 `tl::xxx<...>(...)` 的调用」这条链路完整串起来，并知道指令选择发生在 `op/gemm.cc`。

## 2. 前置知识

阅读本讲前，请确保已理解以下概念（在 u5-l1、u5-l2、u5-l3 中建立）：

- **codegen 与生成代码**：TileLang 的 C++ codegen 把设备 TIR 印成一段 C/CUDA/HIP 源码文本，再交给设备编译器（`nvcc`/`hiprtc`/`mxcc`）编成可执行件。本讲关心的就是「印出来的这段文本里引用的库函数」。
- **张量核 / MMA 指令**：GPU 上做小矩阵乘的硬件指令。NVIDIA 叫 MMA / WGMMA / TCGEN05（按架构代际），AMD/ROCm 与 MetaX/MACA 叫 MFMA。它们都不是「函数调用」，而是通过内联 PTX 或编译器 builtin 触发的。
- **内联汇编（inline asm）**：在 C/C++ 里用 `asm volatile("...": ... : ...)` 直接写汇编。本讲会看到大量这种写法。
- **CUTE**：NVIDIA cuBLASLt/CUTLASS 里的张量布局与指令抽象库（`cute::SM80_*`、`cute::SM90::GMMA::*`、`cute::TiledMMA` 等）。CUDA 与 MACA 的模板都建立在它之上。
- **need_\* 标志机制**：codegen 类里一组布尔成员，在印码过程中按需置位，最终在 `Finish()` 里翻译成 `#include`（u5-l3 已介绍「按需 include」）。

一个一句话直觉：**`tl_templates` 是 TileLang 给生成代码准备的「设备端标准库」**，就像你写 CUDA 程序时会 `#include <cuda_fp16.h>` 一样，TileLang 生成的 kernel 会 `#include <tl_templates/cuda/instruction/mma.h>`。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
| --- | --- |
| `src/tl_templates/` | 设备端 C++ 模板库根目录，按后端分子目录：`cpp/`（公共）、`cpu/`、`cuda/`、`hip/`、`maca/`。 |
| `src/tl_templates/cuda/instruction/mma.h` | CUDA `mma.sync` 指令封装（SM70–SM89），暴露 `tl::mma_sync<...>`。 |
| `src/tl_templates/cuda/instruction/wgmma.h` | Hopper WGMMA 封装（SM90），暴露 `tl::wgmma_ss/wgmma_rs<...>`。 |
| `src/tl_templates/cuda/instruction/tcgen05mma.h` | Blackwell TCGEN05 封装（SM100），暴露 `tl::tcgen05mma_ss/ts<...>`。 |
| `src/tl_templates/cuda/reduce.h` | 线程束/跨线程归约模板：`tl::warp_reduce*`、`tl::AllReduce`。 |
| `src/tl_templates/hip/gemm.h` | HIP MFMA GEMM 模板：`GemmTensorOp` + `MfmaTraits`，暴露 `tl::gemm_ss/gemm_rs`。 |
| `src/tl_templates/maca/gemm.h` | MACA GEMM 模板（基于 CUTE）：`cute::GemmTensorOp`，暴露 `tl::gemm_ss/gemm_rs`。 |
| `src/tl_templates/maca/mma.h` | MACA 单条 MFMA builtin 薄封装 `__builtin_mxc_mma_*`。 |
| `src/cuda/codegen/codegen_cuda.cc` | CUDA codegen：在 `Finish()` 里按 `need_*` 印 `#include`，在 visitor 里把 builtin 调用印成 `tl::xxx<...>(...)`。 |
| `src/cuda/codegen/ptx.cc` | 给 codegen 复用的 PTX 辅助工具：dtype 枚举、形状解析、寄存器类型映射。 |
| `src/cuda/op/gemm.cc` | CUDA GEMM 算子的指令选择（`SelectInst` 返回 `cuda.mma/wgmma/tcgen05`）。 |
| `src/maca/codegen/codegen_maca.cc` | MACA codegen：同样的 include + 调用印出机制，含 `tvm_mfma` builtin 印码。 |

> 说明：本讲规格里提到的 `src/tl_templates/cuda/gemm.h` 在当前仓库并不存在——CUDA 的 GEMM 指令封装按代际拆进了 `cuda/instruction/` 下的 `mma.h`/`wgmma.h`/`tcgen05mma.h`。本讲据此实际文件讲解，不虚构路径。

## 4. 核心概念与源码讲解

### 4.1 tl_templates：生成代码的「设备端标准库」

#### 4.1.1 概念说明

`src/tl_templates/` 是一组**纯头文件（header-only）C++ 模板**。它最反直觉的一点是：这些头文件**不参与 `libtilelang.so` 的编译**，而是被 `#include` 进 codegen 生成的 kernel 源码里，再由设备编译器一起编成最终的 kernel。

为什么这样设计？因为张量核指令（`mma.sync`、`wgmma.mma_async`、`tcgen05.mma`、MFMA builtin）有强烈的「编译期特化」需求——指令的形状 `M×N×K`、数据类型、布局（row/col major）、是否转置，全是模板参数。用 C++ 模板把这些写成 `template <...> void mma_sync(...)`，再让 codegen 印出 `tl::mma_sync<kFloat16, kFloat16, kFloat32, 16, 8, 16, false, true>(...)`，编译器就会在编译期挑出唯一一条正确的 PTX 指令。这比 codegen 自己一行行拼内联汇编字符串更可维护、可复用。

目录按后端划分，结构完全对称：

```
src/tl_templates/
├── cpp/      # 跨后端公共（half.hpp 等）
├── cpu/      # CPU 后端（gemm.h 当前为 "Not Implemented" 占位）
├── cuda/     # CUDA：含 instruction/ 子目录（按代际拆分）+ copy.h/reduce.h/...
├── hip/      # ROCm/HIP：gemm.h/reduce.h/copy.h/...
└── maca/     # MetaX/MACA：gemm.h/mma.h/reduce.h/copy.h/...
```

#### 4.1.2 核心流程

模板与 codegen 的协作链路如下：

```text
codegen 遍历设备 TIR
        │  遇到 builtin 调用（如 builtin::ptx_mma()）
        ▼
置位 need_mma_instruction_h_ = true
        │  印出文本：tl::mma_sync<...>(...)  （这是普通字符串拼接）
        ▼
Finish() 时：因 need_mma_instruction_h_ 为真
        │  印出：#include <tl_templates/cuda/instruction/mma.h>
        ▼
生成源码 = #include + kernel 主体（含 tl::mma_sync 调用）
        │  交给 nvcc/hiprtc/mxcc
        ▼
设备编译器展开模板 → 选中具体 SMxx_* 实现 → 内联 PTX → 机器码
```

关键在于：**codegen 只决定「调用哪个模板、填什么模板参数」，真正的指令发射由模板在设备编译期完成**。

#### 4.1.3 源码精读

模板的「调用入口」由 codegen 决定。以 CUDA codegen 的 `Finish()` 为例，所有 `tl_templates` 头文件都由一组 `need_*` 布尔标志按需引入：

按需 include 的开关，位于 [src/cuda/codegen/codegen_cuda.cc:604-620](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/codegen_cuda.cc#L604-L620)：

```cpp
if (need_mma_instruction_h_) {
  decl_stream << "#include <tl_templates/cuda/instruction/mma.h>\n";
}
if (need_wgmma_instruction_h_) {
  decl_stream << "#include <tl_templates/cuda/instruction/wgmma.h>\n";
}
...
if (need_tcgen05mma_instruction_h_) {
  decl_stream << "#include <tl_templates/cuda/instruction/tcgen05mma.h>\n";
}
```

这段就是「设备端标准库」的入口：只有 kernel 真正用到某种指令时，对应的头文件才会被 include 进来，避免无谓的编译开销与架构冲突。

模板参数里的数据类型来自一个公共枚举，定义在 [src/tl_templates/cuda/common.h:370](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/cuda/common.h#L370) 附近的 `enum class DataType`（含 `kFloat16`、`kFloat32`、`kBFloat16`、`kInt8`、`kTensorFloat32`、`kFloat8_e4m3` 等）。模板全靠它做编译期分派。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：建立「模板库 = 被 include 的头文件」的直觉。
2. **操作步骤**：
   - 在仓库根目录执行 `ls src/tl_templates/`，确认五个后端子目录。
   - 执行 `find src/tl_templates -name '*.cc'`。
3. **需要观察的现象**：第二个命令应当**没有任何输出**——整个 `tl_templates` 没有一个 `.cc` 源文件，全是 `.h`/`.hpp`，证明它是 header-only 模板库。
4. **预期结果**：你会看到只有 `.h`/`.hpp` 文件；这印证了它不单独编译，而是被生成代码 include。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `tl_templates` 里的指令封装要用 C++ 模板（`template <int M, int N, int K, ...>`），而不是普通函数？
  - **答案**：MMA/MFMA 指令的形状、类型、布局都是编译期常量，且映射到固定的内联 PTX。用模板可以让设备编译器在编译期完成特化，既保证选对唯一一条指令，又让 codegen 只需印一行 `tl::mma_sync<...>(...)` 而不必手拼汇编字符串。
- **练习 2**：`need_mma_instruction_h_` 这个布尔标志在哪里被「读」、在哪里被「写」？
  - **答案**：在 `Finish()` 里被「读」（为真则印 `#include`），在 codegen visitor 处理 `builtin::ptx_mma()` 调用时被「写」（置 `true`）。

---

### 4.2 cuda instruction：MMA / WGMMA / TCGEN05 指令封装

这是本讲规格点名的核心模块，也是**代码实践任务**所在。

#### 4.2.1 概念说明

`src/tl_templates/cuda/instruction/` 下按 NVIDIA 架构代际提供三套矩阵乘指令封装：

| 文件 | 架构 | 硬件指令 | 公开入口 |
| --- | --- | --- | --- |
| `mma.h` | SM70–SM89（Volta/Turing/Ampere/Ada） | `mma.sync.aligned.mNxNxK...` | `tl::mma_sync<...>` |
| `wgmma.h` | SM90（Hopper） | `wgmma.mma_async...` | `tl::wgmma_ss/wgmma_rs<...>` |
| `tcgen05mma.h` | SM100（Blackwell） | `tcgen05.mma...` | `tl::tcgen05mma_ss/ts<...>` |

此外还有 `mma_sm70.h`（Volta 专属）、`mma_sp.h`/`wgmma_sp.h`（2:4 稀疏）等变体。

这三套封装的共同设计模式是「**泛型主模板 + 全特化表**」：

1. 先给一个**主模板**，其默认实现是 `static_assert(false)`（「不支持的配置」）；
2. 再用一簇**全特化**（通常由宏批量生成）把「受支持的 类型×形状×布局」逐个映射到具体的 CUTE 实现；
3. 调用方只需写 `tl::mma_sync<kFloat16, kFloat16, kFloat32, 16, 8, 16, false, true>(...)`，编译器自动查表选中实现，若配置不支持则编译期报错。

这种「不支持的配置在编译期直接 `static_assert` 失败」是张量核编程的典型安全网——绝不在运行期才发现指令不存在。

> 术语：`TN` 表示 A 行优先（row-major）、B 列优先（col-major），是 GEMM 里最常见的布局，所以多数封装带 `_TN` 后缀。

#### 4.2.2 核心流程

以 `mma_sync` 为例的分派流程：

```text
tl::mma_sync<A,B,C,M,N,K,TransA,TransB>(c, a, b)
        │  查 MmaDispatcher<A,B,C,M,N,K,TransA,TransB,...> 的特化
        ▼
MmaDispatcher::exec(c, a, b)
        │  call_fma<Impl>(...) 展开 Impl::fma(d..., a..., b..., c...)
        ▼
cute::SM80_16x8x16_F32F16F16F32_TN::fma(...)
        │  内含 asm volatile("mma.sync.aligned.m16n8k16.row.col.f32.f16.f16.f32 ...")
        ▼
一条 mma.sync PTX 指令
```

WGMMA 思路相同，差异在于：WGMMA 的 A/B 操作数是 **shared memory descriptor**（64 位描述符，描述一块 shared memory 的基址/leading dim/stride），而非寄存器地址；所以 `wgmma_ss`（A、B 都来自 shared memory）接收 `uint64_t desc_a, desc_b`，而 `wgmma_rs`（A 来自寄存器，B 来自 shared memory）接收寄存器指针 `a` 与描述符 `desc_b`。

#### 4.2.3 源码精读

**MMA 入口**——公开函数 `tl::mma_sync`，内部委托给 `MmaDispatcher`，见 [src/tl_templates/cuda/instruction/mma.h:271-285](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/cuda/instruction/mma.h#L271-L285)：

```cpp
template <DataType AType, DataType BType, DataType CType, int M, int N, int K,
          bool TransA, bool TransB, bool Saturate = false>
TL_DEVICE void mma_sync(
    typename detail::MmaDispatcher<...>::CRegType *c,
    const typename detail::MmaDispatcher<...>::ARegType *a,
    const typename detail::MmaDispatcher<...>::BRegType *b) {
  using Dispatcher = detail::MmaDispatcher<AType, BType, CType, M, N, K, ...>;
  static_assert(!std::is_void_v<typename Dispatcher::CRegType>,
                "tl::mma_sync: unsupported configuration");
  Dispatcher::exec(c, a, b, c);
}
```

注意返回类型本身就「借用」了 `MmaDispatcher` 的内嵌类型（`CRegType` 等）——这是 SFINAE 式的编译期检查：若没有匹配的特化，主模板里这些类型是 `void`，紧接着的 `static_assert` 直接拦下。

**分派表是如何生成的**——靠宏 `TL_DEFINE_MMA_DISPATCHER` 批量产出特化，见 [src/tl_templates/cuda/instruction/mma.h:172-191](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/cuda/instruction/mma.h#L172-L191)；随后用一行宏调用登记一条具体配置，见 [src/tl_templates/cuda/instruction/mma.h:194-197](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/cuda/instruction/mma.h#L194-L197)：

```cpp
// FP16 输入，A row/B col，累加到 FP32
TL_DEFINE_MMA_DISPATCHER(kFloat16, kFloat16, kFloat32, 16, 8, 16, false, true,
                         false, cute::SM80_16x8x16_F32F16F16F32_TN)
```

这一行声明：「`<FP16,FP16,FP32,16,8,16,不转置A,转置B>` 这组参数 → 用 `cute::SM80_16x8x16_F32F16F16F32_TN`」。最底层的 PTX 就藏在这个 CUTE 实现的 `fma()` 里，例如 SM75 自定义实现的 `asm volatile("mma.sync.aligned.m16n8k8.row.col.f16...")`，见 [src/tl_templates/cuda/instruction/mma.h:63-79](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/cuda/instruction/mma.h#L63-L79)。

**WGMMA 入口**——同样「泛型主模板 + 全特化 + 宏」，公开函数见 [src/tl_templates/cuda/instruction/wgmma.h:458-471](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/cuda/instruction/wgmma.h#L458-L471)：

```cpp
template <DataType A_type, DataType B_type, DataType C_type, int M, int N,
          int K, bool tnspA, bool tnspB, int scaleA = 1, int scaleB = 1>
TL_DEVICE void wgmma_ss(uint64_t desc_a, uint64_t desc_b, uint32_t *c,
                        bool scale_out) {
  WgmmaSSImpl<A_type, B_type, C_type, M, N, K, tnspA, tnspB, scaleA,
              scaleB>::execute(desc_a, desc_b, c, scale_out);
}
```

它的辅助结构 `CallWgmmaSS` 把寄存器数组解包成 CUTE `Impl::fma` 所需的实参列表，见 [src/tl_templates/cuda/instruction/wgmma.h:36-56](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/cuda/instruction/wgmma.h#L36-L56)。`scale_out` 为 `true` 表示「累加到 C」，为 `false` 表示「覆盖 C」。

**TCGEN05 入口**（Blackwell）直接在内联 PTX 里写 `tcgen05.mma`，且只在 `elect_one_sync()`（每 warp 选一个线程）里发射，见 [src/tl_templates/cuda/instruction/tcgen05mma.h:42-57](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/cuda/instruction/tcgen05mma.h#L42-L57)：

```cpp
template <>
TL_DEVICE void tcgen05mma_ts<DataType::kFloat16, false>(...) {
  if (cute::elect_one_sync()) {
    asm volatile("{ ... tcgen05.mma.ctagroup::1.kind::f16 [%0], [%1], %2 ... }"
                 : : "r"(tmem_c), "r"(tmem_a), "l"(desc_b), ...);
  }
}
```

这里出现的 `tmem_c`/`tmem_a` 是 Blackwell 独有的 **TMEM（Tensor Memory）** 地址——TCGEN05 的累加器不再放在线程寄存器，而在一块专门的片上存储里，这也是 `tcgen05_ld`/`tcgen05_st` 等模板存在的原因。

#### 4.2.4 代码实践（本讲指定任务）

> **任务**：在 `src/tl_templates/cuda/instruction` 中找到 MMA 与 WGMMA 的封装函数，说明它们如何被 codegen 调用生成指令。

1. **实践目标**：把「模板封装」与「codegen 调用点」两边对上号。
2. **操作步骤**：
   - 在 `src/tl_templates/cuda/instruction/mma.h` 中定位 `tl::mma_sync`（上文已给出行号 271–285）；在 `wgmma.h` 中定位 `tl::wgmma_ss`/`tl::wgmma_rs`（行号 458–471）。
   - 打开 `src/cuda/codegen/codegen_cuda.cc`，找到处理 `builtin::ptx_mma()` 的分支（约 2854 行起），核心印码见 [src/cuda/codegen/codegen_cuda.cc:2897-2903](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/codegen_cuda.cc#L2897-L2903)：

     ```cpp
     need_mma_instruction_h_ = true;
     this->PrintIndent();
     std::string mma_call =
         "tl::mma_sync<(AType), (BType), (CType), (M), (N), (K), (TransA), "
         "(TransB)>(reinterpret_cast<(CRegType)*>((C_ptr) + (C_offset)), ...);";
     ```

   - 再找到处理 `tl::ptx_wgmma_ss()` 的分支，见 [src/cuda/codegen/codegen_cuda.cc:3162-3166](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/codegen_cuda.cc#L3162-L3166)：

     ```cpp
     need_wgmma_instruction_h_ = true;
     std::string wgmma_asm_code =
         "tl::wgmma_ss<(AType), (BType), (CType), (M), (N), (K), (tnspA), "
         "(tnspB), (scaleA), (scaleB)>(uint64_t((desc_a) + (A_offset)), ...);";
     ```

3. **需要观察的现象**：codegen 的印码逻辑就是**字符串模板 + `Replacer` 占位符替换**——`(AType)`、`(M)`、`(desc_a)` 等占位符被实际 dtype 字符串、数字、变量名替换后，直接 `<< this->stream`。
4. **预期结果**：你能清楚看到「codegen 一边置 `need_*` 标志（触发 include）、一边把 `tl::mma_sync<...>(...)` 当字符串印进 kernel」，而模板在设备编译期才把这一行展开成 `mma.sync` PTX。**指令选择发生在更上游的 `op/gemm.cc`**（见 4.4），它决定生成 `ptx_mma` 还是 `ptx_wgmma` 调用。
5. （本步骤为源码阅读型，无需运行。）

#### 4.2.5 小练习与答案

- **练习 1**：`mma.h` 里 `MmaDispatcher` 的主模板 `exec` 为什么要写 `static_assert(always_false_v<...>, "unsupported configuration")`？
  - **答案**：为了让「未登记的类型/形状组合」在**编译期**就失败，而不是生成一条错误的或 noop 的指令。因为主模板若不写断言，C++ 会认为它是个合法但空的实现，运行期不会有任何报错。
- **练习 2**：`wgmma_ss` 的前两个参数为何是 `uint64_t desc_a, desc_b`，而 `mma_sync` 的参数是寄存器指针？
  - **答案**：WGMMA（Hopper）直接从 shared memory 取操作数，用 64 位 **descriptor** 描述那块 shared memory（基址/leading dimension/swizzle 等）；而 `mma.sync`（Ampere 及以前）的操作数已经由 `ldmatrix` 等加载到线程寄存器里，故传寄存器指针。
- **练习 3**：TCGEN05 的 `tcgen05mma_ts` 为什么包在 `if (cute::elect_one_sync())` 里？
  - **答案**：Blackwell 的 `tcgen05.mma` 是**整个 warp/CTA 级**的异步操作，只需要 warp 内一个线程发射 PTX 即可，其余线程发射会造成重复触发。

---

### 4.3 hip / maca 模板：MFMA GEMM 的两种写法

#### 4.3.1 概念说明

HIP（ROCm）和 MACA（MetaX）都做 MFMA 矩阵乘，但两份 `gemm.h` 风格迥异，对照阅读能很直观地看出「同一个目标、两种工程取舍」：

- **`hip/gemm.h`——手写布局 + builtin**：自己用模板算出 swizzle 布局、自己写从 shared memory 取数到寄存器的索引映射（`reverse_index_map`），最后调用编译器 builtin `__builtin_amdgcn_mfma_*`。可读、可控，但与 CUDA 体系无任何代码复用。
- **`maca/gemm.h`——复用 CUTE**：直接用 `cute::TiledMMA`、`cute::copy`、`cute::gemm` 这套 CUTLASS 抽象，把「指令原子（`MACA_16x16x16_F32F16F16F32`）+ swizzle 布局 + warp 切分」声明式地组合起来，body 几乎全是 CUTE 调用。

两者的公开入口都叫 `tl::gemm_ss`（shared-shared）与 `tl::gemm_rs`（register-shared），由 codegen 在生成代码里调用。

> 术语复习：MACA 的 `warp_size = 64`（CUDA 为 32）。这一点直接决定了模板里线程到数据的索引映射（`reverse_index_map`）与 CUDA 不同。

#### 4.3.2 核心流程

**HIP 的 `GemmTensorOp::body` 流程**：

```text
for ki in 内层 K 维:
    每个 warp 按手写索引映射(reverse_index_map) 从 shared 取 A、B 到寄存器 A_local/B_local
    for kp in kPack:
        MfmaTraits<A_type>::mfma_op(b_ptr, a_ptr, acc_ptr)   // → __builtin_amdgcn_mfma_*
```

**MACA 的 `cute::GemmTensorOp::body` 流程**（CUTE 风格）：

```text
用 SmemLayoutA/B（OperandTraits 选出的 swizzle）构造 shared 张量视图
用 TiledMMA（Instruction::MMA × warp 网格）取 thread_slice
for k in 内层 K 维:
    cute::copy(...) 把 A、B 从 shared 拷到寄存器 fragment
    cute::gemm(tiled_mma, tCrA(k), tCrB(k), acc)   // → MACA_16x16x16_* MMA 原子
```

两者最终都落到一条 MFMA 指令；区别在「布局、取数、warp 切分」是自己算（HIP）还是交给 CUTE 声明式完成（MACA）。

#### 4.3.3 源码精读

**HIP 的指令分派**用 `MfmaTraits` 模板特化实现「类型 → builtin」映射，half 的特化见 [src/tl_templates/hip/gemm.h:24-30](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/hip/gemm.h#L24-L30)：

```cpp
template <> struct MfmaTraits<half> {
  template <typename AccType>
  static TL_DEVICE void mfma_op(const half *b, const half *a, AccType *c) {
    *c = __builtin_amdgcn_mfma_f32_16x16x16f16(*((float16x4 *)b),
                                               *((float16x4 *)a), *c, 0, 0, 0);
  }
};
```

可对照 int8 特化用 `__builtin_amdgcn_mfma_i32_16x16x32_i8`（[src/tl_templates/hip/gemm.h:12-21](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/hip/gemm.h#L12-L21)）。这里 `16x16x16` 是 MFMA 的 instr 形状，`0, 0, 0` 三个参数分别是 blgp/cbsz/abid（MFMA 的线程广播控制位，本讲不展开）。HIP 的 body 把这些 mfma_op 排进 `kPack`/`inner_k` 双层循环，见 [src/tl_templates/hip/gemm.h:158-232](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/hip/gemm.h#L158-L232)。公开入口 `tl::gemm_ss/rs` 见 [src/tl_templates/hip/gemm.h:298-318](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/hip/gemm.h#L298-L318)。

**MACA 的 CUTE 路径**先把「类型三元组 → MMA 原子」声明在 `DispatchInstruction`，见 [src/tl_templates/maca/gemm.h:12-17](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/maca/gemm.h#L12-L17)：

```cpp
template <> struct DispatchInstruction<half_t, half_t, float> {
  using MMA = MMA_Atom<MMA_Traits<MACA_16x16x16_F32F16F16F32>>;
};
```

随后 `GemmTensorOp::body` 几乎全是 CUBE 调用（`partition_fragment_A/B`、`copy`、`gemm`），逻辑非常紧凑，见 [src/tl_templates/maca/gemm.h:85-120](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/maca/gemm.h#L85-L120)。其 shared memory 的 swizzle 布局由 `OperandTraits` 按 `bits/N/K/K_inner` 选择（例如 `Swizzle<4,2,4>` 等），见 [src/tl_templates/maca/gemm.h:32-58](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/maca/gemm.h#L32-L58)。公开入口 `tl::gemm_ss/rs` 见 [src/tl_templates/maca/gemm.h:160-176](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/maca/gemm.h#L160-L176)。

> 补充：MACA 还有一份极薄的 [src/tl_templates/maca/mma.h](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/maca/mma.h#L5-L11)，直接包了一层 `__builtin_mxc_mma_*`（MetaX 版 MFMA builtin），用于需要单条指令、而非整块 GEMM 的场景。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：体会「同一指令、两种封装风格」。
2. **操作步骤**：
   - 同时打开 `src/tl_templates/hip/gemm.h` 的 `body`（158–232 行）与 `src/tl_templates/maca/gemm.h` 的 `body`（85–120 行）。
   - 数一数各自「手写索引/循环」与「调用库抽象」的比例。
3. **需要观察的现象**：HIP 的 body 里你能看到显式的 `for (int ki...)`、`reverse_index_map(...)`、`make_swizzle_layout(...)`、`A_local[...] = A_shared[...]`；MACA 的 body 里这些几乎都消失，取而代之的是 `copy(...)`、`gemm(...)`、`partition_fragment_A(...)`。
4. **预期结果**：MACA 因为复用 CUTE，body 行数更少、更声明式；HIP 因为一切手写，控制更细但代码更长。两者最终都发射 `16x16x16` 的 FP16→FP32 MFMA。
5. （本步骤为源码阅读型，无需运行。）

#### 4.3.5 小练习与答案

- **练习 1**：`hip/gemm.h` 里 `warp_size` 是多少？这会影响什么？
  - **答案**：是 64（`static constexpr int warp_size = 64;`）。它决定了 `reverse_index_map` 把 64 个 lane 映射到 MFMA 所需矩阵元素的方式，与 CUDA（32）完全不同，也决定了 `local_size_a/b/c = (micro_size_x*k)/warp_size` 的分摊。
- **练习 2**：MACA 的 `GemmTensorOp` 里 `TileMma` 是怎么由「指令原子」拼成的？
  - **答案**：`TiledMMA<typename Instruction::MMA, Layout<Shape<num_warp_m,num_warp_n,1>>, ...>`——把单个 `MACA_16x16x16_*` MMA 原子按 `num_warp_m × num_warp_n` 的 warp 网格铺开，得到覆盖整个 `M×N` tile 的 partition，CUTE 自动负责 thread 到数据的映射。

---

### 4.4 模板与 codegen：指令选择与调用印出

#### 4.4.1 概念说明

现在把 4.1–4.3 串起来：模板封装只是「被动的库」，真正决定「**生成代码里出现哪一行 `tl::xxx<...>`、include 哪个头**」的是 **codegen**，而 codegen 的选择又由更上游的 **算子层指令选择** 决定。完整的因果链是：

```text
op/gemm.cc::SelectInst   ──返回指令键（cuda.mma / cuda.wgmma / cuda.tcgen05）──▶
gemm 的 Lower()           ──据此产生 builtin 调用（ptx_mma / ptx_wgmma_ss / ...）──▶
codegen visitor           ──识别 builtin，印出 tl::xxx<...>(...) 文本，置 need_* ──▶
codegen Finish()          ──按 need_* 印 #include <tl_templates/...>            ──▶
设备编译器                 ──展开模板 → PTX/builtin → 机器码
```

一句话：**模板封装 = 「能调用什么」；指令选择 = 「该调用什么」；codegen = 把该调用的那一行印出来并备好头文件**。

#### 4.4.2 核心流程

以 CUDA GEMM 为例：

1. `Gemm::SelectInst` 依架构与算子标志返回 `kCudaMMA`/`kCudaWGMMA`/`kCudaTCGEN05`。
2. GEMM 算子的 `Lower()`（细节在 u4-l2）按返回的键，把 `T.gemm` 降级成对 `builtin::ptx_mma()`、`tl::ptx_wgmma_ss()` 或 `tl::ptx_tcgen05_mma_ss()` 的 TIR `Call`。
3. CUDA codegen 遍历到这些 `Call` 时：① 置对应 `need_*_instruction_h_`；② 用字符串模板印出 `tl::mma_sync<...>(...)` / `tl::wgmma_ss<...>(...)` 等。
4. `Finish()` 把头文件 include 进来，交给 `nvcc`。

MACA 的链路对称：GEMM 算子（`src/maca/op/gemm.cc`）降级成对 `tl::gemm_ss/rs`（走 `maca/gemm.h` 的 CUTE 路径）或 `tl::tvm_mfma()`（走单条 `__builtin_mxc_mma_*`）的调用。

#### 4.4.3 源码精读

**CUDA 指令选择**——`SelectInst` 的判优顺序：显式 `isWgmma_`/`isTcgen05_` 优先，否则按架构能力从高到低回退，见 [src/cuda/op/gemm.cc:266-287](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/op/gemm.cc#L266-L287)：

```cpp
static String SelectInst(const GemmNode &op, int block_size, Target target) {
  if (op.isWgmma_) { ...; return kCudaWGMMA; }
  if (op.isTcgen05_) { ...; return kCudaTCGEN05; }
  if (AllowTcgen5Mma(op, target)) { return kCudaTCGEN05; }
  if (AllowWgmma(op, block_size, target)) { return kCudaWGMMA; }
  return kCudaMMA;
}
```

返回值（`"cuda.mma"`/`"cuda.wgmma"`/`"cuda.tcgen05"`）就是 u4-l2 讲过的指令键，决定后续 `Lower()` 产生哪个 builtin。

**codegen 印码**——处理 `builtin::ptx_mma()` 时，先解析参数（shape/dtype/layout），再用 `Replacer` 把占位符替换成具体值，最后印出，关键三行见 [src/cuda/codegen/codegen_cuda.cc:2897](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/codegen_cuda.cc#L2897)（置标志）、[2899-2903](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/codegen_cuda.cc#L2899-L2903)（字符串模板）。其中的 dtype 字符串、寄存器类型、形状解析来自公共工具 `src/cuda/codegen/ptx.cc`（`DTypeFromString`、`DTypeEnumToString`、`GetMMARegisterType`、`ParseMMAShape` 等）。注意 `ptx.cc` 还提供一个 `PrintWGMMAAssembly` 直接拼 PTX 的函数，但当前 wgmma 的主路径用的是模板调用（`tl::wgmma_ss<...>`），`ptx.cc` 主要充当 dtype/形状工具箱与校验（`CheckWGMMAConfigValidity`）。

**MACA 的 MFMA 印码**——处理 `tl::tvm_mfma()` 时，按 prefix 拼出 `__builtin_mxc_mma_<prefix>`，见 [src/maca/codegen/codegen_maca.cc:2120-2138](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L2120-L2138)：

```cpp
std::string call_mfma_code = R"({
  *((({C_dtype}*){c_ref}) + {c_bias}) = {mfma_buildin}(*((({A_dtype}*){a_ref}) + {a_bias}),
                                                  *((({B_dtype}*){b_ref}) + {b_bias}), {c}, 0, 0, 0);
})";
std::string mfma_buildin = "__builtin_mxc_mma_" + prefix;
```

而 MACA 的 `Finish()` 同样按 `need_*` 印 include，且**无条件**引入 `maca/gemm.h`（整块 GEMM 模板），见 [src/maca/codegen/codegen_maca.cc:352-354](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L352-L354)。

> 一个值得注意的结构性细节：MACA codegen 的 `Finish()` 里也声明了对 `tl_templates/maca/instruction/{mma,wgmma,tcgen05mma,mma_sm70}.h` 的条件 include（[codegen_maca.cc:322-333](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L322-L333)）。这是从 CUDA 侧镜像过来的代码骨架，但当前 MACA 的 GEMM 实际走 `maca/gemm.h`（CUTE）与 `tvm_mfma` builtin 两条路径，这些 `instruction/` 头文件对应的 `need_*` 标志在 MACA 上通常不会被置位。阅读时不必纠结，理解「MACA 的 MFMA 由 `maca/gemm.h` 与 builtin 负责」即可。

**reduce 模板的接线**——`reduce.h` 里的 `tl::warp_reduce_sum/max/min/...` 同样由 codegen 在识别到 `tl::warp_reduce_*()` builtin 时印出，见 [src/cuda/codegen/codegen_cuda.cc:4574-4587](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/codegen_cuda.cc#L4574-L4587)；其底层是递归 XOR 蝶形归约 `AllReduce`，offset≥32 走 shared memory+barrier，<32 走 `shfl_xor_sync`，见 [src/tl_templates/cuda/reduce.h:220-296](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/cuda/reduce.h#L220-L296)。这是「模板库」覆盖范围远不止 MMA 的一个例证。

#### 4.4.4 代码实践（源码阅读 + 可选运行）

1. **实践目标**：验证「指令选择 → builtin → codegen 印码 → include」这条链在你脑子里是闭环的。
2. **操作步骤**：
   - 读 `src/cuda/op/gemm.cc:266-287` 的 `SelectInst`，记下三条返回路径。
   - 在 `src/cuda/codegen/codegen_cuda.cc` 搜索 `ptx_mma()`、`ptx_wgmma_ss()`、`ptx_tcgen05_mma_ss`，确认它们各自置了哪个 `need_*` 标志、印了哪一行 `tl::xxx<...>`。
   - （可选运行）若有 CUDA 环境，参考 u1-l4 跑一个 GEMM，用 `get_kernel_source()` 取生成源码，在源码顶部确认有 `#include <tl_templates/cuda/instruction/...>`，在 body 里找到 `tl::mma_sync` 或 `tl::wgmma_ss` 的调用。
3. **需要观察的现象**：生成源码顶部的 include 与算子实际使用的指令一致；body 里的模板调用参数（`<kFloat16,kFloat16,kFloat32,16,8,16,...>`）与 `SelectInst` 的选择一致。
4. **预期结果**：若在 Ampere(SM80) 上、未显式要求 wgmma，`SelectInst` 回退到 `cuda.mma`，生成源码里应出现 `mma.h` 的 include 与 `tl::mma_sync` 调用；若在 Hopper(SM90) 上，则出现 `wgmma.h` 与 `tl::wgmma_ss`。
5. 若无可用的 CUDA 设备，本步骤降级为源码阅读——**「待本地验证」**生成源码的具体形态。

#### 4.4.5 小练习与答案

- **练习 1**：如果你在一台 Ampere GPU 上调用 `T.gemm`（不指定 wgmma/tcgen05），生成代码里会出现 `tl::wgmma_ss` 吗？
  - **答案**：不会。`SelectInst` 会因 `AllowTcgen5Mma`/`AllowWgmma` 在 Ampere 上不满足，回退返回 `kCudaMMA`，于是 `Lower` 产生 `builtin::ptx_mma()`，codegen 印的是 `tl::mma_sync`，include 的是 `mma.h`，`need_wgmma_instruction_h_` 保持 false。
- **练习 2**：为什么 `ptx.cc` 里的 dtype/形状工具被 CUDA 与 MACA codegen 都需要？
  - **答案**：因为印码时要统一把 TIR 里的 dtype 字符串解析成内部枚举、再转成模板参数字符串（如 `kFloat16`），还要把 `"16x8x16"` 解析成 `(16,8,16)`，这些是后端无关的纯工具逻辑，集中放在 `ptx.cc` 避免重复。

---

## 5. 综合实践

把本讲四条线索拧成一个任务：**追踪一条 `T.gemm` 从指令选择到模板展开的全过程，并预测生成源码的形态。**

1. 选定一个具体配置：FP16 输入、FP32 累加、`block_M=128, block_N=128, block_K=32`，target 为 `cuda`（SM80 Ampere）。
2. **指令选择**：在 `src/cuda/op/gemm.cc` 的 `SelectInst` 里推断它会返回哪个键，并写出依据（`AllowTcgen5Mma`/`AllowWgmma` 在 SM80 上的真假）。
3. **builtin**：根据返回的键，说出 `Lower()` 会产生 `builtin::ptx_mma()` 还是 `tl::ptx_wgmma_ss()`。
4. **codegen 印码**：在 `codegen_cuda.cc` 里定位对应分支，写出它将印出的 `tl::xxx<...>(...)` 字符串与它置位的 `need_*` 标志。
5. **模板展开**：在 `tl_templates/cuda/instruction/` 里定位被调用的模板，指出它最终会走到哪个 CUTE 实现（例如 `cute::SM80_16x8x16_F32F16F16F32_TN`）与哪条 PTX（`mma.sync.aligned.m16n8k16...`）。
6. **验证**：若有设备，按 u1-l4 的方式编译该 GEMM，用 `get_kernel_source()` 取出源码，逐项核对第 4、5 步的预测；若无设备，把预测结果写成一份「源码阅读报告」，标注「待本地验证」。

完成本任务后，你应该能不依赖运行、仅凭源码就推断出任意 (target, dtype, shape) 下 TileLang 会选中哪条张量核指令。

## 6. 本讲小结

- `src/tl_templates/` 是**设备端 header-only C++ 模板库**，被 codegen `#include` 进生成代码、由设备编译器展开，本身不进 `libtilelang.so`。
- CUDA 的张量核封装按代际分成 `mma.h`（`tl::mma_sync`，SM70–89）、`wgmma.h`（`tl::wgmma_ss/rs`，SM90）、`tcgen05mma.h`（`tl::tcgen05mma_ss/ts`，SM100），统一采用「泛型主模板 + 全特化表 + `static_assert` 安全网」模式。
- HIP 的 `gemm.h` 走**手写 swizzle + `__builtin_amdgcn_mfma_*`**；MACA 的 `gemm.h` 走 **CUTE 抽象（`TiledMMA` + `MACA_16x16x16_*`）**，两者公开入口都是 `tl::gemm_ss/gemm_rs`，体现同一目标的两种工程取舍。
- codegen 与模板的接线靠两个动作：① visitor 识别 builtin 后印出 `tl::xxx<...>(...)` 字符串并置 `need_*` 标志；② `Finish()` 按标志印 `#include`。
- 真正的指令选择发生在算子层 `op/gemm.cc::SelectInst`（返回 `cuda.mma/wgmma/tcgen05`），它决定下游产生哪个 builtin、最终展开成哪条 PTX/指令。
- `tl_templates` 不止 MMA：`reduce.h`（`warp_reduce`/`AllReduce`）、`copy.h`、`barrier.h` 等覆盖了 kernel 的方方面面，连接方式与 MMA 完全一致。

## 7. 下一步学习建议

- **进入 u6（张量核与 intrinsics）**：本讲讲的是 C++ 模板「如何发射一条指令」，u6-l1 将从 Python 侧讲 `tilelang/intrinsics` 的各类 **TensorCoreIntrinEmitter**——即「`T.gemm` 在 Python 层如何被翻译成对 `tl.gemm.lower` 的调用」，与本讲构成 Python↔C++ 的闭环。
- **回顾 u4-l2（tile 算子与 T.gemm 的分派）**：本讲 4.4 的 `SelectInst` 正是 u4-l2 两级分派中「C++ 第一级」的 CUDA 实现；可对照阅读 `src/rocm/op/gemm.cc` 与 `src/maca/op/gemm.cc` 的同名逻辑，看后端差异。
- **延伸阅读源码**：若对 Blackwell 感兴趣，可顺读 `src/tl_templates/cuda/tcgen_05.h`、`tcgen_05_ld.h`、`tcgen_05_st.h` 与 `src/cuda/transform/inject_tcgen05_fence.cc`，理解 TMEM 与 fence 的协同；若对稀疏感兴趣，可读 `mma_sp.h`/`wgmma_sp.h` 与 `maca/gemm_sp.h`。
