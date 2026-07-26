# tl_templates 模板库与第三方 ISA

## 1. 本讲目标

本讲回答一个贯穿整条编译链路的问题：**codegen 生成的 C++ 代码里，`tl::ascend::copy_gm_to_l1(...)`、`tl::ascend::mma(...)` 这些「类方法」到底调用了什么？PTO 路线里的 `TASSIGN / TMOV / TTRANS / TMATMUL` 又是谁提供的？**

学完本讲，你应当能够：

- 说清 `src/tl_templates/ascend/common.h` 与 `src/tl_templates/pto/common.h` 各自包装了哪些底层原语，以及二者风格的根本差别。
- 画出一张「TileLang 前端 `T.copy` / `T.gemm_v0` → TIR intrinsic → codegen → 模板函数 → AscendC / PTO 指令」的完整映射表。
- 解释 `catlass`、`pto-isa`、`shmem` 三个第三方子模块的来源、角色，以及为什么 JIT 编译时必须能 `-I` 到它们的头文件。
- 读懂两个 `printf.h` 调试头，理解 ascendc 与 pto 两条路线在设备端打印上的不同实现。

本讲承接 [u6-l2](u6-l2-dual-codegen.md)（双 codegen）与 [u3-l2](u3-l2-data-copy.md)（`T.copy` 与原子写回），是「编译器后端」单元的第三块拼图：codegen 把 TIR 翻译成 C++ 源码后，真正干活的正是这两个模板头文件里的内联函数。

## 2. 前置知识

阅读本讲前，请确认你已建立以下心智模型（来自前置讲义）：

- **两条 codegen 路线**：ascendc（基于 Ascend C / Catlass，默认主线）与 pto（基于 PTO IR，贴近硬件指令，支持 A5 仿真）。分发由 `target.model` 决定（见 [u6-l2](u6-l2-dual-codegen.md)）。
- **`T.copy` 的 scope 派发**：前端只声明「从哪块 buffer 搬到哪块 buffer」，走哪条 DMA 指令由 `src.scope()` 与 `dst.scope()` 自动决定，最终落到 `copy_gm_to_l1` / `copy_l1_to_l0a` 等模板（见 [u3-l2](u3-l2-data-copy.md)）。
- **Ascend 片上存储层级**：GM（全局）、L1（Cube 核缓存）、UB（Vector 核缓存）、L0A/L0B/L0C（Cube 寄存器级），其中 L1 默认按 zN 分形布局摆放（见 [u3-l1](u3-l1-memory-alloc.md)、[u4-l4](u4-l4-layout-swizzle.md)）。
- **bisheng 编译**：codegen 产出的是 **C++ 源码**（不是二进制），再由 CANN 的 bisheng 编译器编成 `.so`（ascendc 用 `-xasc`、pto 用 `-xcce`，见 [u6-l2](u6-l2-dual-codegen.md)）。

一个关键直觉：**模板库是 codegen 与硬件之间的「最后一层 C++ 胶水」**。它把 TIR intrinsic 翻译出的函数名，对接到 AscendC 官方 API（ascendc 路线）或 PTO 指令宏（pto 路线）。理解了这层胶水，你就能拿着 `func.get_kernel_source()` 打印出的 C++ 代码一路追到真实硬件指令。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/tl_templates/ascend/common.h](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h) | **ascendc 路线模板库**（约 1884 行）。包装 AscendC + Catlass 原语：各类 `copy_*`、`mma`、`gemm_v0`、reduce、布局标签、swizzle、shmem。 |
| [src/tl_templates/ascend/printf.h](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/printf.h) | ascendc 路线调试头，包装 `AscendC::DumpTensor`。 |
| [src/tl_templates/pto/common.h](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/common.h) | **pto 路线模板库**（约 1320+ 行）。包装 PTO IR 指令：`pto::Tile` 类型族、`TASSIGN/TMOV/TCVT/TEXTRACT/TMATMUL/TTRANS/TCMP`、`Sort`。 |
| [src/tl_templates/pto/printf.h](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/printf.h) | pto 路线调试头，用 `cce::printf` + `TPRINT` 打印，仅在 `_DEBUG`/`__CPU_SIM` 下生效。 |
| [.gitmodules](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/.gitmodules) | 声明 `catlass`、`pto-isa`、`shmem` 三个子模块的来源（均来自 `gitcode.com/cann`）。 |
| [src/target/codegen_ascend.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc) | ascendc codegen，`#include` ascend/common.h 并把 intrinsic 译成 `tl::ascend::*` 调用。 |
| [src/target/codegen_ascend_pto.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend_pto.cc) | pto codegen，`#include` pto/common.h 并发射 `TASSIGN/TMOV/...` 指令。 |

## 4. 核心概念与源码讲解

### 4.1 三子模块的角色：模板库依赖什么

#### 4.1.1 概念说明

`ascend/common.h` 与 `pto/common.h` 本身并不重新实现硬件指令，它们只是 **包装层（wrapper）**。真正提供「Ascend C 算子库」「PTO 指令集」「OpenSHMEM 风格核间通信」的是三个外部子模块，由 git submodule 管理。

#### 4.1.2 三个子模块一览

[.gitmodules](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/.gitmodules) 里登记了三个 Ascend 专用子模块，URL 全部指向华为 CANN 官方组织 `gitcode.com/cann`：

| 子模块路径 | 来源 | 提供的核心能力 | 被谁 include |
| --- | --- | --- | --- |
| `3rdparty/catlass` | `gitcode.com/cann/catlass.git` | **Ascend C Template Library**：布局标签（`zN/nZ/zZ`）、`TileCopyTla`、`CopyL0CToGmTla`、`GemmIdentityBlockSwizzle`、`MakeLayout/MakeTensor` 等 | ascend/common.h、ascend/printf.h |
| `3rdparty/pto-isa` | `gitcode.com/cann/pto-isa.git` | **PTO 指令集头** `pto/pto-inst.hpp`：`pto::Tile`、`TASSIGN/TMOV/TCVT/TEXTRACT/TMATMUL/TMATMUL_ACC/TTRANS/TCMP/TSORT32` 等 | pto/common.h、pto/printf.h |
| `3rdparty/shmem` | `gitcode.com/cann/shmem.git` | **核间共享内存通信** `shmem.h`：`aclshmemx_mte_put_nbi/get_nbi` 等 OpenSHMEM 风格原语（PE 间数据搬运） | ascend/common.h |

对应源码：

```cpp
// ascend/common.h 开头
#include "catlass/catlass.hpp"
#include "catlass/arch/arch.hpp"
#include "catlass/detail/tag_to_layout.hpp"
#include "catlass/gemm/block/block_swizzle.hpp"
#include "catlass/gemm/tile/tile_copy.hpp"
#include "catlass/layout/layout.hpp"
#include "shmem.h"
```

见 [ascend/common.h:1-17](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L1-L17)。

```cpp
// pto/common.h 开头
#include <pto/pto-inst.hpp>
```

见 [pto/common.h:1](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/common.h#L1)。

> **注意**：这三个子模块在仓库里是 git submodule，默认 checkout 时不一定拉取内容（本讲写作环境里目录即为空）。但只要走 [u1-l2](u1-l2-install-and-build.md) 的 `install_ascend.sh` / `build_wheel_ascend.sh` 递归拉取子模块，或装了官方 wheel，这些头文件就会被 `setup.py` 打进 wheel 的 include 目录，供 JIT 阶段 `libgen.py` 用 `-I{TL_ROOT}/3rdparty/...` 引用。**没有它们，codegen 生成的 C++ 根本编不过。**

#### 4.1.3 核心流程

从写一个 kernel 到最终硬件指令的链路如下（以 ascendc 路线的 `T.copy` 为例）：

```
T.copy(A_gm, A_l1)                      # 前端 DSL（Python）
   │  JIT 捕获
   ▼
tir.call_intrin("tl.ascend_copy_v2")    # TIR intrinsic（scope=shared.l1）
   │  AscendCopy::Lower 按 scope 选模板
   ▼
"tl::ascend::copy_gm_to_l1<...>(...)"   # codegen 产出的 C++ 字符串
   │  bisheng -xasc 编译
   ▼
catlass::TileCopyTla<Arch::AtlasA2>     # ascend/common.h 内联调用
   │
   ▼
AscendC::DataCopyPad                    # AscendC 官方原语 → 硬件 DMA
```

PTO 路线平行存在，只是最后一层换成 `pto::TMOV / pto::TMATMUL` 等指令宏。

#### 4.1.4 小练习与答案

**练习 1**：为什么这三个子模块必须作为 git submodule 而不是直接拷贝进 `src/`？
**参考答案**：它们体积大、版本与 CANN 强绑定、且由华为官方维护。用 submodule 可以在升级 CANN 时只改 commit 指针，避免手工合并上万行 C++ 模板代码；同时保持本仓库自身只包含「包装逻辑」而非「原语实现」。

**练习 2**：在仓库根目录执行 `git submodule status`，确认 `catlass`、`pto-isa`、`shmem` 三者的 commit 哈希。若显示前缀 `-`，说明什么？
**参考答案**：前缀 `-` 表示该子模块尚未初始化/拉取（目录为空）。需执行 `git submodule update --init 3rdparty/catlass 3rdparty/pto-isa 3rdparty/shmem` 才能让 codegen 生成的代码在本地编译通过。

---

### 4.2 ascend/common.h：用 Catlass 包装 AscendC 原语

#### 4.2.1 概念说明

ascendc 路线把 Ascend 当成一块「类 GPU」的硬件来编程：用 `LocalTensor<T>` / `GlobalTensor<T>` 两类张量对象表示片上 / 全局数据，用 `AscendC::DataCopyPad`、`AscendC::Mmad`、`AscendC::WholeReduceSum` 等官方 API 发指令。但官方 API 接口繁琐、布局细节多，所以 `ascend/common.h` 用 Catlass 提供的布局抽象把它们包成一组「语义直白」的模板函数，供 codegen 直接按名字调用。

这层的核心抽象是 **「布局标签（layout tag）」**：用编译期类型（`layout::zN`、`layout::nZ`、`layout::zZ`、`layout::RowMajor`）描述数据怎么摆，由 Catlass 的 `MakeLayout / MakeTensor / TileCopyTla` 在编译期算出正确的 DMA 参数。

#### 4.2.2 核心流程

整个头文件以 `namespace tl::ascend` 组织，开头一次性引入 Catlass、tla、AscendC 三个命名空间并定义布局别名：

```cpp
namespace tl::ascend {
using namespace Catlass;
using namespace tla;
using namespace AscendC;
using ArchTag = Arch::AtlasA2;          // 目标硬件代际
using LayoutGM   = layout::RowMajor;    // GM 行优先
using LayoutL0A  = layout::zZ;          // L0A 用 zZ
using LayoutL0B  = layout::nZ;          // L0B 用 nZ
using LayoutL1   = layout::zN;          // L1  默认 zN（分形）
using LayoutL1T  = layout::nZ;          // L1 转置路径用 nZ
```

见 [ascend/common.h:23-36](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L23-L36)。

这组别名就是 [u4-l4](u4-l4-layout-swizzle.md) 里讲的「scope 与 layout 正交」在 C++ 层的落点：`LayoutL1 = zN` 对应 `AscendInferBufferScope` 给 L1 buffer 注入的默认布局。

#### 4.2.3 源码精读

**(a) `copy_gm_to_l1`：GM → L1 的分形搬运**

```cpp
template <typename T, uint32_t dstM, uint32_t dstN>
CATLASS_DEVICE void
copy_gm_to_l1(LocalTensor<T> dstTensor, GlobalTensor<T> srcTensor,
              uint32_t realSrcN = 1, uint32_t realTailM = 0,
              uint32_t realTailN = 0, bool need_clear = true) {
  uint32_t tailM = realTailM == 0 ? dstM : realTailM;
  uint32_t tailN = realTailN == 0 ? dstN : realTailN;
  // 尾块且需要清零时，先把整个 L1 tile 用 InitConstValue 清零
  if (need_clear && (tailM != dstM || tailN != dstN)) {
    AscendC::InitConstValue(dstTensor, {1, ... , 0, 0});
    AscendC::PipeBarrier<PIPE_MTE2>();
  }
  // 用布局标签构造源（GM 行优先）和目标（L1 zN）的 tensor 视图
  auto layout = MakeLayoutFromTag(LayoutGM{tailM, realSrcN});
  auto src = tla::MakeTensor<...>(srcTensor, src_LAYOUT);   // GM
  constexpr auto layoutInL1 = tla::MakeLayout<T, LayoutL1_>(dstM, dstN);
  auto dst = tla::MakeTensor<...>(dstTensor, layoutInL1);   // L1 (A1)
  TileCopyTla<ArchTag, decltype(src), decltype(dst)> tileCopier;
  tileCopier(dst, src);   // 最终落到 AscendC::DataCopyPad 类 DMA
}
```

见 [ascend/common.h:55-86](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L55-L86)。要点：

- `need_clear` 参数控制是否对整个 L1 tile 清零——这服务于「拼接 / 纵向合并」搬运，避免第二次 DMA 把第一次写进同一 zN tile 的数据冲掉（见源码注释）。
- `TileCopyTla` 是 Catlass 的搬运分发器，按 `ArchTag` 与 src/dst 的 `TPosition`（`GM`→`A1`）自动选出正确的 DataCopy 指令。

**(b) `copy_l1_to_l0a` / `copy_l1_to_l0b`：L1 → Cube 寄存器**

```cpp
template <typename T, uint32_t srcM, uint32_t srcN, bool transpose = false>
CATLASS_DEVICE void copy_l1_to_l0a(LocalTensor<T> dstTensor,
                                   LocalTensor<T> srcTensor,
                                   uint32_t dstM, uint32_t dstN) {
  // transpose 决定 L1 用 zN 还是 nZ
  using LayoutL1_ = std::conditional_t<transpose,
                       Catlass::detail::TagToLayout_t<T, LayoutL1T>,
                       Catlass::detail::TagToLayout_t<T, LayoutL1>>;
  // 源在 A1（L1），目标在 A2（L0A）
  auto dst = MakeTensor<...>(dstTensor, layoutAInL0, A2);
  TileCopyTla<ArchTag, decltype(src), decltype(dst)> tileCopier;
  tileCopier(dst, src);
}
```

见 [ascend/common.h:88-108](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L88-L108)。`copy_l1_to_l0b` 结构完全对称，只是目标位置换成 `B2`（L0B）并固定 `nZ`，见 [ascend/common.h:110-131](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L110-L131)。这正是 [u3-l3](u3-l3-gemm-mma.md) 里讲的「`T.gemm_v0` 模板内部把 L1→L0 搬运掏出来」所对应的底层实现。

**(c) `mma`：单条硬件矩阵乘**

```cpp
template <typename T1, typename T2, uint32_t M, uint32_t N>
CATLASS_DEVICE void mma(LocalTensor<T1> const A, LocalTensor<T1> const B,
                        LocalTensor<T2> const C, bool init, uint32_t K,
                        uint32_t n_actual = N, uint8_t unitFlag = 0) {
  MmadParams mmadParams;
  mmadParams.m = M;  mmadParams.n = n_actual;  mmadParams.k = K;
  mmadParams.cmatrixInitVal  = init;       // init=true 清零累加器
  mmadParams.cmatrixSource  = false;       // 累加时 C 来自 L0C
  mmadParams.unitFlag        = unitFlag;   // 驱动 mma→fixpipe 流水
  Mmad(C, A, B, mmadParams);               // AscendC 硬件矩阵乘累加指令
}
```

见 [ascend/common.h:133-166](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L133-L166)。关键参数：

- `init`：即 [u3-l3](u3-l3-gemm-mma.md) 里的 K 分段累加语义——首段 `init=true` 清零 L0C，后续段 `init=false` 在旧值上累加。
- `unitFlag`：驱动硬件 `mma→fixpipe` 流水（`0b10` 留在 L0C、`0b11` 释放给配对的 fixpipe），让 `fixpipe(tile i)` 与 `mma(tile i+1)` 在双缓冲 L0C 上重叠。
- `n_actual`：运行期输出列数，支持变长 GEMM（如 attention 里按实际窗口长度算 QK）。

**(d) `copy_l0c_to_gm`：Cube 结果写回 GM（fixpipe）**

```cpp
CopyL0CToGmTla<ArchTag, decltype(src), decltype(dst),
               ScaleGranularity::NO_QUANT, enRelu> tileCopier;
tileCopier(dst, src, unitFlag);   // 落到 AscendC::Fixpipe
```

见 [ascend/common.h:168-194](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L168-L194)。`unitFlag` 与上面 `mma` 的 `unitFlag` 配对，构成硬件 `mma→fixpipe` 流水。

**(e) UB 系搬运与原子写回**

- `copy_gm_to_ub` / `copy_ub_to_gm`：用 `AscendC::DataCopyPad`，并在尾块用 `AscendC::Duplicate` 填充 `padValue`（与 `AscendTailMaskPropagation` pass 配合，见 [ascend/common.h:215-260](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L215-L260)）。
- `atomic_add_ub_to_gm` / `atomic_add_l0c_to_gm`：`SetAtomicAdd` + 普通 copy + `disable_dma_atomic_compat()` 三件套，对应 [u3-l2](u3-l2-data-copy.md) 的 `T.tile.atomic_add`，见 [ascend/common.h:262-283](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L262-L283)。
- `copy_ub_to_ub`：同类型走 `DataCopy`，跨类型走 `Cast`（精度模式按 dtype 对选择 `CAST_NONE`/`CAST_RINT`），见 [ascend/common.h:285-336](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L285-L336)。
- `copy_ub_to_l1`：行优先 UB → zN L1，用 `Nd2NzParams` 做 ND→NZ 重排，见 [ascend/common.h:338-357](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L338-L357)。

**(f) `gemm_v0` 模板内部：copy + mma 的乒乓流水**

块级 `T.gemm_v0` 最终落到这个模板。它在 K/N 双层循环里反复调用 `copy_l1_to_l0a` / `copy_l1_to_l0b` / `mma`，并用 `WaitFlag/SetFlag<HardEvent::M_MTE1>` 等做乒乓缓冲同步：

```cpp
for (uint32_t kL0Idx = 0; kL0Idx < kL0split; kL0Idx++) {
  uint32_t pp = (tileIdx & 1);                 // 乒乓选缓冲
  uint32_t l0a_base = pp * (M * kL0Size);
  WaitFlag<HardEvent::M_MTE1>(L0AB_EVENT + pp);
  tl::ascend::copy_l1_to_l0a<T1, M, K>(l0a[l0a_base], A[...], M, kSize);
  tl::ascend::copy_l1_to_l0b<T1, K, N>(l0b[l0b_base], B[...], kSize, nTile);
  SetFlag<HardEvent::MTE1_M>(L0AB_EVENT + pp);
  WaitFlag<HardEvent::MTE1_M>(L0AB_EVENT + pp);
  PipeBarrier<PIPE_M>();
  tl::ascend::mma<T1, T2, M, nTile>(l0a[l0a_base], l0b[l0b_base],
                                    C[cNOffset], initflag, kSize, ...);
  SetFlag<HardEvent::M_MTE1>(L0AB_EVENT + pp);
  tileIdx++;
}
```

见 [ascend/common.h:1204-1243](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L1204-L1243)。这段印证了 [u3-l3](u3-l3-gemm-mma.md) 的结论：`T.gemm_v0` 模板内部本就调用 `mma`，且把 L1→L0 搬运 + 流水同步全包在里面，对用户透明。

**(g) swizzle 与 shmem**

- `thread_block_swizzle`：用 Catlass 的 `GemmIdentityBlockSwizzle` 重排核间任务以提升 L2 命中（对应 [u4-l4](u4-l4-layout-swizzle.md) 的 `T.use_swizzle`），见 [ascend/common.h:196-213](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L196-L213)。
- `shmem_put_nbi` / `shmem_get_nbi`：调用 shmem 子模块的 `aclshmemx_mte_put_nbi/get_nbi`，实现 PE（处理单元）间 GM 数据搬运，是 OpenSHMEM 风格的核间通信原语，见 [ascend/common.h:383-432](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L383-L432)。

#### 4.2.4 代码实践

**实践目标**：在 `ascend/common.h` 中定位 `copy_gm_to_l1` 与 `copy_l1_to_l0a` 的实现，并构建一张从 TileLang `T.copy` 到 AscendC 原语的映射表（见第 5 节综合实践统一完成）。

**操作步骤（源码阅读型）**：

1. 打开 [ascend/common.h:55-86](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L55-L86)，确认 `copy_gm_to_l1` 的 dst 是 `A1`（L1），搬运器是 `TileCopyTla`。
2. 打开 [ascend/common.h:88-108](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L88-L108)，确认 `copy_l1_to_l0a` 的 src 是 `A1`、dst 是 `A2`（L0A）。
3. 用 `func.get_kernel_source()` 打印一个 GEMM 的生成代码（参考 [u1-l5](u1-l5-jit-and-pipeline.md)），在其中搜索 `copy_gm_to_l1`、`copy_l1_to_l0a`、`mma` 三个调用点。

**需要观察的现象**：生成代码里每条 `tl::ascend::copy_*` / `tl::ascend::mma` 调用的参数个数与模板签名一致；`copy_gm_to_l1` 后通常紧跟一条 `PipeBarrier<PIPE_MTE2>` 或 `SetFlag` 同步。

**预期结果**：能画出 `T.copy(GM→L1)` → `copy_gm_to_l1` → `TileCopyTla` → `DataCopyPad` 的三级调用链。**待本地验证**（需装好 CANN/bisheng 才能跑通 JIT）。

#### 4.2.5 小练习与答案

**练习 1**：`copy_l1_to_l0a` 的模板参数 `transpose` 改变了哪两件事？
**参考答案**：① L1 源数据的布局标签（`transpose=false` 用 `LayoutL1=zN`、`true` 用 `LayoutL1T=nZ`）；② 源 tensor 的逻辑形状（转置时交换 `srcM/srcN` 顺序传入 `MakeLayout`）。最终影响 `TileCopyTla` 生成的 DMA 参数。

**练习 2**：为什么 `mma` 要把 `cmatrixSource = false` 显式写死？源码注释怎么说？
**参考答案**：`MmadParams` 不默认初始化 `cmatrixSource`，而硬件在 `cmatrixInitVal==false`（累加 mma）时总会读这个字段。若不写死，K 分段累加序列在同时设 `unitFlag` 时会读到随机值导致 Cube 挂死。`false`（C 来自 L0C）正是所有累加调用者的本意。

---

### 4.3 pto/common.h：用 PTO 指令宏包装 IR 原语

#### 4.3.1 概念说明

pto 路线的编程模型与 ascendc 截然不同。它**不使用 `LocalTensor` 对象**，而是 **「Tile 类型 + 地址」** 的模型：

- 先声明一个编译期 **Tile 类型**（描述形状、布局、pad 策略），如 `TileMatL0A<T,M,K>`。
- 运行期用 `pto::TASSIGN(tile, addr)` 把这个 Tile **绑定到某块内存地址**。
- 再用 `pto::TMOV / TMATMUL / TTRANS ...` 等**指令宏**对 Tile 做运算。

这种模型更贴近硬件指令（PTO IR 一一对应底层指令），因此 pto 路线便于 A5 仿真与指令级调试（见 [u7-l5](u7-l5-camodel-sim.md)）。`pto/common.h` 就是把这些指令宏按 TileLang 的语义包装成一组同名模板函数，让 codegen 能像 ascendc 那样按名字调用。

#### 4.3.2 核心流程

头文件以 `namespace tl::ascend_pto` 组织，开头定义一组 **Tile 类型别名**（全是 `pto::Tile<...>` 的实例化）：

| 别名 | TileType | 典型用途 |
| --- | --- | --- |
| `TileMatL1` | `Mat`（ColMajor×RowMajor） | L1 矩阵（zN） |
| `TileMatL1ZN` | `Mat`（RowMajor×ColMajor） | L1 矩阵（nZ，转置路径） |
| `TileMatL0A` | `Left` | L0A（Cube 左矩阵） |
| `TileMatL0B` | `Right` | L0B（Cube 右矩阵） |
| `TileUbDataND/DN/Nz` | `Vec` | UB 上的向量/矩阵 tile |
| `pto::TileAcc<T,M,N>` | — | L0C 累加器 |

见 [pto/common.h:13-63](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/common.h#L13-L63)。

#### 4.3.3 源码精读

**(a) Tile + 地址模型：`TASSIGN` + `TMOV`**

最典型的搬运是「同地址空间内移动一块数据」，模板 `mov_tile` 把它包成两步：

```cpp
template <typename T, int32_t shape>
AICORE PTO_INLINE void mov_tile(int32_t src_addr, int32_t dst_addr,
                                int32_t src_offset, int32_t dst_offset,
                                int32_t len) {
  TileUbDataND<T, 1, shape, 1, shape> src_temp_ub;
  pto::TASSIGN(src_temp_ub, src_addr + src_offset * len);   // 绑定源地址
  TileUbDataND<T, 1, shape, 1, shape> dst_temp_ub;
  pto::TASSIGN(dst_temp_ub, dst_addr + dst_offset * len);   // 绑定目标地址
  pto::TMOV(dst_temp_ub, src_temp_ub);                      // 发射搬移指令
}
```

见 [pto/common.h:65-75](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/common.h#L65-L75)。这是 pto 路线最核心的范式——**先 `TASSIGN` 建立地址视图，再发指令**。类型转换版 `cvt_tile` 把 `TMOV` 换成 `TCVT(dst, src, rmode)`，见 [pto/common.h:77-87](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/common.h#L77-L87)。

**(b) L1 → L0A/L0B：`TEXTRACT`**

```cpp
template <typename T, uint32_t M, uint32_t N, uint32_t M_L1, uint32_t N_L1,
          bool transpose = false>
AICORE PTO_INLINE void copy_l1_to_l0a(
    TileMatL0A<T, M, N, M, N> &l0a,
    std::conditional_t<transpose, TileMatL1ZN<...>, TileMatL1<...>> &A,
    uint32_t indexRow, uint32_t indexCol) {
  pto::TEXTRACT(l0a, A, indexRow, indexCol);   // 从 L1 tile 抽取一块到 L0A
}
```

见 [pto/common.h:110-118](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/common.h#L110-L118)。注意与 ascendc 的差别：ascendc 用 `TileCopyTla` 按 TPosition 自动选 DMA，pto 直接发一条 `TEXTRACT` 指令，行/列下标由参数显式给出。

**(c) mma：`TMATMUL` / `TMATMUL_ACC`**

```cpp
template <typename T1, typename T2, int M, int N, int K, int validM = M,
          int validN = N>
AICORE PTO_INLINE void mma(TileMatL0A<T1, M, K> l0a,
                           TileMatL0B<T1, K, N> l0b,
                           pto::TileAcc<T2, M, N, validM, validN> &C,
                           bool init) {
  if (init) {
    pto::TMATMUL(C, l0a, l0b);          // 清零后乘累加
  } else {
    pto::TMATMUL_ACC(C, C, l0a, l0b);   // 在 C 旧值上累加
  }
}
```

见 [pto/common.h:130-140](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/common.h#L130-L140)。与 ascendc 的 `Mmad` + `cmatrixInitVal` 对应：pto 路线把 init/accumulate 拆成两条不同指令 `TMATMUL` / `TMATMUL_ACC`，语义更直白。

**(d) `gemm_v0_inner`：完整 K 分段流水**

把 `copy_l1_to_l0a/l0b` + `mma` 串起来，并用 `set_flag/wait_flag` 在 `PIPE_M / PIPE_MTE1 / PIPE_FIX` 之间同步：

```cpp
pto::TASSIGN(l0a, 0x0);  pto::TASSIGN(l0b, 0x0);
set_flag(PIPE_M, PIPE_MTE1, war_event_id);
wait_flag(PIPE_M, PIPE_MTE1, war_event_id);
...
copy_l1_to_l0a<T1, M, CurrentK, M, K, false>(l0a, A, 0, kL0Idx * kL0Size);
copy_l1_to_l0b<T1, CurrentK, N, K, N, false>(l0b, B, kL0Idx * kL0Size, 0);
set_flag(PIPE_MTE1, PIPE_M, war_event_id);
wait_flag(PIPE_MTE1, PIPE_M, war_event_id);
pto::TMATMUL(C, l0a, l0b);   // 或 TMATMUL_ACC
```

见 [pto/common.h:142-193](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/common.h#L142-L193)。这是 pto 路线版的「`T.gemm_v0` 内部实现」，与 ascendc 的乒乓缓冲流水一一对应。

**(e) 其它指令包装**

- `transpose` → `pto::TTRANS(dst, src, tmp)`（需一块临时 tile），见 [pto/common.h:1306-1315](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/common.h#L1306-L1315)。
- `compare` → `pto::TCMP(dst, src0, src1, mode)`，见 [pto/common.h:1320-1326](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/common.h#L1320-L1326)。
- 二元/一元运算：`enum class BinaryOp { TADD, TSUB, TMUL, TDIV, TMAX, TMIN, TAND, TOR }`（[pto/common.h:412](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/common.h#L412)）与 `enum class UnaryOp { TEXP, TLOG, TABS, TRECIP, TSQRT, TRSQRT, TRELU, TNOT }`（[pto/common.h:450](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/common.h#L450)），由模板按枚举值分发到对应 PTO 指令。
- `Sort`：归并排序 + `TSORT32`（内部统一升 float 处理 half），支持 full sort 与 TopK，见 [pto/common.h:1110-1115](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/common.h#L1110-L1115)。
- 动态搬运：`copy_gm_to_l1_dynamic` / `copy_gm_to_ub_dynamic` / `copy_l0c_to_gm_dynamic` / `copy_ub_to_gm_dynamic`（见 [pto/common.h:238](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/common.h#L238)、[pto/common.h:304](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/common.h#L304)、[pto/common.h:267](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/common.h#L267)、[pto/common.h:362](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/common.h#L362)）。

#### 4.3.4 代码实践

**实践目标**：对比 ascendc 与 pto 两条路线在「L1→L0A 搬运」上的不同抽象。

**操作步骤（源码阅读型）**：

1. 打开 [ascend/common.h:88-108](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L88-L108)（`copy_l1_to_l0a`，用 `TileCopyTla`）。
2. 打开 [pto/common.h:110-118](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/common.h#L110-L118)（`copy_l1_to_l0a`，用 `TEXTRACT`）。
3. 对比两者的参数列表：ascendc 传 `LocalTensor` 对象 + dstM/dstN；pto 传 Tile 引用 + indexRow/indexCol。

**需要观察的现象**：ascendc 隐藏了地址（封装在 `LocalTensor` 内），pto 把地址作为一等公民（`TASSIGN` 绑定）。

**预期结果**：能用一句话概括差别——「ascendc 是对象模型，pto 是地址模型」。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：pto 路线里，每次搬运前为什么几乎都有一对 `TASSIGN(tile, addr)`？
**参考答案**：PTO 的 Tile 类型只描述形状/布局，本身不持有数据地址。必须用 `TASSIGN` 把 Tile 绑定到具体内存地址后，后续 `TMOV/TEXTRACT` 等指令才知道去哪儿读/写。这是 pto「地址模型」的核心。

**练习 2**：`TMATMUL` 与 `TMATMUL_ACC` 的区别对应 ascendc 路线里的哪个参数？
**参考答案**：对应 `MmadParams::cmatrixInitVal`。`TMATMUL`（init=true，清零累加器）等价 `cmatrixInitVal=true`；`TMATMUL_ACC`（在旧值上累加）等价 `cmatrixInitVal=false`。pto 拆成两条指令，ascendc 用一个参数控制。

---

### 4.4 codegen 如何衔接模板库 + printf 调试头

#### 4.4.1 概念说明

模板库写好了，还要被 codegen 「按名字调用」。两个 codegen 文件分别在自己生成的 C++ 顶部 `#include` 对应的 `common.h`，然后在 `VisitExpr_(CallNode)` 里把 TIR intrinsic 译成 `tl::ascend::*`（ascendc）或 `pto::*` / `tl::ascend_pto::*`（pto）调用。两个 `printf.h` 则是设备端调试入口。

#### 4.4.2 核心流程

- ascendc codegen 顶部：`decl_stream << "#include \"tl_templates/ascend/common.h\"\n";`，见 [codegen_ascend.cc:102](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L102)。
- pto codegen 顶部：`this->stream << "#include \"tl_templates/pto/common.h\"\n";`，见 [codegen_ascend_pto.cc:474](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend_pto.cc#L474)。

#### 4.4.3 源码精读

**(a) ascendc 的 intrinsic → 模板名分发表**

codegen 用一张静态表把 copy 类 intrinsic 名映射到模板函数名，并记录附加参数个数：

```cpp
static const std::unordered_map<std::string, int> kCopyOpExtraArgs = {
    {"copy_l0c_to_gm", 3},      {"copy_gm_to_l1", 3},
    {"copy_l1_to_l0a", 2},      {"copy_l1_to_l0b", 2},
    {"copy_gm_to_ub", 4},       {"copy_ub_to_gm", 3},
    {"atomic_add_ub_to_gm", 3}, {"atomic_add_l0c_to_gm", 3},
    {"copy_ub_to_ub", 6}};
```

见 [codegen_ascend.cc:2718-2723](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L2718-L2723)。这张表的名字与 `ascend/common.h` 里的模板函数一一对应——**这就是「TIR intrinsic → 模板」的契约**。

其它算子的分发（gemm、reduce）在另一个 visitor 里：

```cpp
} else if (op->op.same_as(tl::ascend_gemm_v0())) {
  GemmOpCodegen(op);                              // → tl::ascend::gemm 模板
} else if (op->op.same_as(tl::ascend_wholereducesum())) {
  PrintOpCall(op, "AscendC::WholeReduceSum", ...); // 直接调官方原语
```

见 [codegen_ascend.cc:660-675](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L660-L675)。注意 `WholeReduceSum` 直接调 `AscendC::` 官方 API，绕过了模板包装——说明模板库只包装「需要布局/流水复杂处理」的原语，简单的官方 API 可由 codegen 直发。

**(b) pto 的指令发射**

pto codegen 直接把指令宏名打印出来，例如转置发 `TTRANS`、类型转换发 `TCVT`/`TMOV`、L1→L0A 发 `copy_l1_to_l0a`：

```cpp
// codegen_ascend_pto.cc
TransposeCodegen(op, "TTRANS");                       // 见 :1009
std::string api_name = is_cast ? "TCVT" : "TMOV";     // 见 :1373
std::string api_name = is_a ? "copy_l1_to_l0a" : "copy_l1_to_l0b"; // 见 :1462
```

引用：[codegen_ascend_pto.cc:1009](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend_pto.cc#L1009)、[:1373](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend_pto.cc#L1373)、[:1462](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend_pto.cc#L1462)。而每条指令前都会先 `TASSIGN(temp, addr)` 绑定地址，例如 [codegen_ascend_pto.cc:263](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend_pto.cc#L263)。

**(c) 两个 printf.h 调试头**

ascendc 版包装官方 `AscendC::DumpTensor`，区分 `LocalTensor` 与 `GlobalTensor` 两个重载：

```cpp
template <typename T>
__aicore__ void DumpTensor(const LocalTensor<T> &src, uint32_t desc,
                           uint32_t dumpSize, uint8_t dim,
                           const uint32_t shapeInfo[]) {
  if (dim > 0 && shapeInfo != nullptr) {
    AscendC::ShapeInfo shapeInfoParams(dim, shapeInfo);
    AscendC::DumpTensor(src, desc, dumpSize, shapeInfoParams);
  } else {
    AscendC::DumpTensor(src, desc, dumpSize);
  }
}
```

见 [ascend/printf.h:16-36](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/printf.h#L16-L36)。

pto 版则用 `cce::printf` + `TPRINT`，且**整段被 `#if defined(_DEBUG) || defined(__CPU_SIM)` 包住**——release 模式下 `DumpTensor` 是空函数，零开销：

```cpp
#if defined(_DEBUG) || defined(__CPU_SIM)
template <typename Tile>
AICORE inline void DumpTensor(Tile &src, uint32_t desc, uint32_t dumpSize, ...) {
  pipe_barrier(PIPE_ALL);
  cce::printf("=== DumpTensor [desc=%u] UB tile, dumpSize=%u ===\n", ...);
  TPRINT(src);
}
#else
template <typename Tile>
AICORE inline void DumpTensor(Tile &, uint32_t, uint32_t, uint8_t, const uint32_t[]) {}
#endif
```

见 [pto/printf.h:21-109](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/printf.h#L21-L109)。这与 [u7-l4](u7-l4-debug-profiling.md) 讲的 `TL_PTO_DEBUG` / camodel 仿真场景对应——pto 的打印主要服务于 CPU 仿真（`__CPU_SIM`）。

#### 4.4.4 代码实践

**实践目标**：在生成代码里验证「intrinsic 名 → 模板名」的衔接。

**操作步骤**：

1. 对一个简单 GEMM 调 `func.get_kernel_source()`，在返回的 C++ 字符串里搜索 `#include "tl_templates/ascend/common.h"`，确认头文件已被引入。
2. 在同一份代码里搜索 `copy_gm_to_l1`、`copy_l1_to_l0a`、`mma`，确认它们以 `tl::ascend::` 前缀出现。
3. 把同一 kernel 用 `target='pto'` 重新编译，搜索 `TASSIGN`、`TMATMUL`、`TEXTRACT`。

**需要观察的现象**：ascendc 代码里看到的是「类方法风格」（`tl::ascend::copy_gm_to_l1<...>(...)`），pto 代码里看到的是「指令宏风格」（`pto::TMOV(...)`、`pto::TMATMUL(...)`）。

**预期结果**：两份代码功能等价、风格不同，印证 [u6-l2](u6-l2-dual-codegen.md) 的结论。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`codegen_ascend.cc` 里 `WholeReduceSum` 直接调 `AscendC::WholeReduceSum`，没经过模板包装。为什么 `copy_gm_to_l1` 却要包装？
**参考答案**：reduce 类原语语义单一、无需布局转换，官方 API 可直发；而 `copy_gm_to_l1` 涉及 GM 行优先 → L1 zN 的布局重排、尾块清零、`TileCopyTla` 按 `ArchTag` 选指令等复杂逻辑，包装成模板才能把这些细节集中管理、避免 codegen 重复实现。

**练习 2**：pto 的 `printf.h` 里 `DumpTensor` 在 release 模式下为什么是空函数体？
**参考答案**：因为 `cce::printf` / `TPRINT` 是仿真期调试手段，会严重拖慢真实硬件执行且硬件上无意义。用 `#if defined(_DEBUG) || defined(__CPU_SIM)` 隔离，release 编译时退化成空函数，既保留调用点又零开销。

## 5. 综合实践：绘制 T.copy → AscendC 原语映射表

把本讲知识串起来，完成本讲规格里要求的核心实践任务。

### 5.1 实践目标

在 `ascend/common.h` 中定位 `copy_gm_to_l1` 与 `copy_l1_to_l0a` 的实现，绘制一张从 **TileLang 前端 `T.copy`** 出发，经 **TIR intrinsic → codegen 译出的模板名 → 模板内部调用的 Catlass/AscendC 原语** 的完整映射表。

### 5.2 操作步骤

1. **读前端**：回顾 [u3-l2](u3-l2-data-copy.md)，列出 `T.copy` 支持的全部 scope 组合（GM↔L1、GM↔UB、L1→L0A/L0B、L0C→GM、UB↔UB、UB↔L1、L0C→UB）。
2. **读模板**：逐一在 [ascend/common.h](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h) 里定位对应模板函数（行号见 4.2.3 节各小段）。
3. **读分发**：在 [codegen_ascend.cc:2718-2723](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L2718-L2723) 确认 intrinsic 名 → 模板名的一一对应。
4. **填表**：按下表逐行填写「底层原语」列（即模板内部最终调用的 AscendC / Catlass API）。

### 5.3 参考映射表（ ascendc 路线）

| TileLang `T.copy` 路径 | codegen 译出的模板调用 | 模板内部底层原语 | 源码位置 |
| --- | --- | --- | --- |
| GM → L1 | `tl::ascend::copy_gm_to_l1` | `TileCopyTla` → `DataCopyPad`（+ `InitConstValue` 清零） | [common.h:55-86](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L55-L86) |
| L1 → L0A | `tl::ascend::copy_l1_to_l0a` | `TileCopyTla`（A1→A2） | [common.h:88-108](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L88-L108) |
| L1 → L0B | `tl::ascend::copy_l1_to_l0b` | `TileCopyTla`（A1→B2） | [common.h:110-131](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L110-L131) |
| L0C → GM | `tl::ascend::copy_l0c_to_gm` | `CopyL0CToGmTla` → `Fixpipe` | [common.h:168-194](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L168-L194) |
| GM → UB | `tl::ascend::copy_gm_to_ub` | `DataCopyPad` + `Duplicate`(pad) | [common.h:215-249](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L215-L249) |
| UB → GM | `tl::ascend::copy_ub_to_gm` | `DataCopyPad` | [common.h:251-260](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L251-L260) |
| UB → UB（同 dtype） | `tl::ascend::copy_ub_to_ub` | `DataCopy` | [common.h:285-336](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L285-L336) |
| UB → UB（跨 dtype） | `tl::ascend::copy_ub_to_ub` | `Cast`（`CAST_NONE`/`CAST_RINT`） | 同上 |
| UB → L1 | `tl::ascend::copy_ub_to_l1` | `DataCopyPad`（`Nd2NzParams`，ND→NZ） | [common.h:338-357](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L338-L357) |

### 5.4 需要观察的现象 / 预期结果

- 每条 `T.copy` 路径都能在表中找到唯一对应的模板函数与底层原语，无遗漏。
- 同一前端原语（`T.copy`）因 scope 不同，落到完全不同的硬件指令（`DataCopyPad` / `Fixpipe` / `Cast` / ND→NZ 重排），印证 [u3-l2](u3-l2-data-copy.md) 「scope 派发」的设计。

如果本地装好了 CANN/bisheng，可进一步用 `func.get_kernel_source()` 打印 GEMM 生成代码，逐行对照表中第二列的模板调用名。**完整跑通需待本地验证。**

## 6. 本讲小结

- **模板库是 codegen 与硬件之间的最后一层 C++ 胶水**：[ascend/common.h](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h) 包装 AscendC+Catlass，[pto/common.h](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/common.h) 包装 PTO 指令宏，二者由 [u6-l2](u6-l2-dual-codegen.md) 的两个 codegen 分别 `#include`。
- **三子模块各司其职**：`catlass` 提供布局标签与 `TileCopyTla` 等搬运/计算模板；`pto-isa` 提供 `pto::Tile` 类型族与 `TASSIGN/TMOV/TMATMUL/...` 指令；`shmem` 提供 `aclshmemx_mte_*` 核间通信原语，均来自 `gitcode.com/cann`。
- **ascendc 是对象模型，pto 是地址模型**：前者用 `LocalTensor`/`GlobalTensor` 隐藏地址、靠 `TPosition` 自动选 DMA；后者用 `TASSIGN(tile, addr)` 显式绑定地址、直发指令宏。
- **L1→L0A 搬运是观察两条路线差异的最佳样本**：ascendc 走 `TileCopyTla`（[common.h:88-108](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L88-L108)），pto 走 `TEXTRACT`（[pto/common.h:110-118](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/common.h#L110-L118)）。
- **`T.gemm_v0` 的模板内部 = `copy_l1_to_l0a/l0b` + `mma` + 乒乓 flag 流水**：ascendc 版见 [common.h:1204-1243](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L1204-L1243)，pto 版见 [pto/common.h:142-193](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/pto/common.h#L142-L193)。
- **调试头风格也不同**：ascendc 的 `printf.h` 包装官方 `DumpTensor`；pto 的 `printf.h` 用 `cce::printf`+`TPRINT` 且仅 `_DEBUG`/`__CPU_SIM` 下生效，release 时空函数零开销。

## 7. 下一步学习建议

- **继续向下追硬件**：读 [src/target/rt_mod_ascend.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/rt_mod_ascend.cc) 与 [u6-l4](u6-l4-runtime-bisheng.md)，理解这些 C++ 源码如何被 `libgen.py` 调 bisheng 编成 `.so` 并被运行时加载。
- **向上回到 pass**：本讲看到的 `need_clear`、`unitFlag`、`n_actual` 等参数，其取值由 [u6-l1](u6-l1-pass-overview.md) 的各 pass（`LowerTileOp`、`AscendTailMaskPropagation`、`AscendMemoryPlanning`）决定，建议结合 [u6-l6](u6-l6-lower-tile-tailmask.md) 阅读。
- **实战对照**：跑 [u7-l4](u7-l4-debug-profiling.md) 的 `T.dump_tensor`，在生成代码里观察 `DumpTensor` 调用如何落到本讲的 `printf.h`；或跑 [u7-l5](u7-l5-camodel-sim.md) 的 camodel 仿真，体会 pto 路线在仿真下的指令打印。
- **想贡献新算子**：若要新增一个 codegen 需要支持的搬运/计算路径，工作通常落在三处——① `src/op/` 加 intrinsic；② 对应 codegen 的 `VisitExpr_` 加分支；③ 本讲的 `common.h` 加一个模板函数包装底层原语。可参考 [u7-l7](u7-l7-contributing.md)。
