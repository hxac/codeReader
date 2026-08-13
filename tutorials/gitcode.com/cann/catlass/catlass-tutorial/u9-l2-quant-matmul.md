# 量化矩阵乘（W8A16/W4A8/FP8/MX）

## 1. 本讲目标

本讲聚焦 CATLASS 中一类「输入不是普通 fp16/fp32」的矩阵乘——**量化矩阵乘**。学完后你应当能够：

- 说清楚 **权重量化（W8A16/W4A8）** 的两条反量化路径：epilogue 反量化（搬出后再乘 scale）与 prologue 反量化（搬入前先转类型/乘 scale），以及各自的代价。
- 掌握 **FP8** 输入如何先在 AIV 上 cast 到 fp16、再交给 AIC 做 fp16 矩阵乘。
- 理解 **MX 微缩放（microscaling）量化**（MXFP8/MXFP4）的 per-block scale 处理，以及 Ascend950 上的**二级量化（dual-level / LEVEL0+LEVEL1）**与「AIV 侧预量化 → AIC 侧 MX matmul」的协作路径。
- 对照真实样例（12、32、53、63、65、71）建立从 AtlasA2 到 Ascend950 的量化算子导航。

## 2. 前置知识

阅读本讲前，请确认你已掌握以下概念（对应前置讲义）：

- **五层抽象与 AIC/AIV 协同**：Device→Kernel→Block→Tile→Basic；AIC(Cube) 跑 Mmad 矩阵乘累加，AIV(Vector) 在 UB 上做激活、量化、cast 等后处理（u1-l2、u2-l2）。
- **累加类型选择器**：int8/int4 输入在 L0C 按 **int32** 累加，half/bf16/fp8 按 **float** 累加（u3-l2）。这是理解「为什么权重量化可以先算整数积、后乘 scale」的关键。
- **Tile 数据搬运族与 Prologue**：GM→L1→L0→L0C→GM 的搬运链，以及 Prologue 作为「搬入前置处理」的位置（u5-l2、u4-l3）。
- **BlockEpilogue 与 EVG 后处理**：MMAD 之后涉及输出矩阵的操作（激活、反量化、cast）发生在 epilogue（u6-l1、u6-l2）。

下面先建立本讲的「坐标系」。

### 2.1 什么是量化、反量化

把高精度浮点 \(x\)（如 fp16）压成低精度整数或低比特浮点 \(q\) 以节省带宽与显存，称为**量化（quantize）**；把 \(q\) 还原回高精度近似值称为**反量化（dequantize）**：

\[
q = \text{round}(x / s), \qquad \hat{x} = q \times s
\]

其中 \(s\) 是 scale（缩放因子）。\(s\) 的粒度决定了精度与开销：

| 粒度 | 含义 | 开销 |
| --- | --- | --- |
| per-tensor | 整个矩阵一个 scale | 最小 |
| per-channel / per-token | 每列（权重）或每行（激活）一个 scale | 中等 |
| per-block (MX) | 每 32 个连续元素一个 scale（e8m0） | 较大，精度高 |

**量化矩阵乘的核心难点**：硬件的 Mmad 指令并不能直接吃任意低精度+任意 scale 的输入。CATLASS 的做法是：根据数据类型，把「反量化」插到数据流水的不同位置——**搬入前（prologue）、随路（fixpipe）、搬出后（epilogue）**——这就是本讲三条主线。

### 2.2 本讲用到的 AscendC 概念速查

- **mix kernel**：同一个 kernel 同时被 AIC 与 AIV 执行，靠 `AscendC::AIC` / `AscendC::AIV` 模板特化分流；AIV 跑预量化/后处理，AIC 跑 matmul。
- **SyncAll / CrossCore Flag**：AIC 与 AIV 之间的两种同步原语。`SyncAll<false>()` 是全核屏障；CrossCore Flag 是 tile 粒度的细粒度握手。
- **L0C**：AIC 上存放 Mmad 累加结果的缓存，默认按 fp32（或 int32）累加。

## 3. 本讲源码地图

本讲涉及的源码文件及其职责：

| 文件 | 层 | 职责 |
| --- | --- | --- |
| `include/catlass/gemm/kernel/quant_matmul.hpp` | Kernel | **epilogue 反量化**范式：AIC 算 int8×int8 积到 workspace，AIV epilogue 乘 scale |
| `include/catlass/gemm/kernel/w4a8_matmul.hpp` | Kernel | **prologue 反量化**范式：AIV 先 int4→int8，AIC 用反量化后的 B 算 matmul |
| `include/catlass/gemm/kernel/fp8_matmul.hpp` | Kernel | **FP8 cast 范式**：AIV cast fp8→fp16，AIC 做 fp16 matmul |
| `include/catlass/gemm/tile/atlasa2/cast_fp8_to_fp16.hpp` | Tile | FP8→fp16 的反量化微内核（UB 上 bit 操作 + scalar） |
| `include/catlass/gemm/tile/atlasa2/cast_int4_to_int8.hpp` | Tile | int4→int8 的 cast 微内核（UB 上 DataCopyPad + Cast） |
| `include/catlass/gemm/dispatch_policy.hpp` | — | `MmadMx`（MX matmul 策略）、`MmadAtlasA2PingPongWithPrologue`（带 prologue 乒乓） |
| `include/catlass/gemm/kernel/dual_level_quant_mx_batched_matmul_tla.hpp` | Kernel | Ascend950 二级量化 + MX FP4 batch matmul 单 kernel（AIV 预量化 + AIC matmul） |
| `examples/63_ascend950_dual_level_quant_mx_batch_matmul/dual_level_quant_mx_batch_matmul.cpp` | Host | 二级量化 MX FP4 样例组装 |

> 注：`include/catlass/gemm/tile/cast_fp8_to_fp16.hpp` 与 `cast_int4_to_int8.hpp` 只是按 `CATLASS_ARCH` 转发的薄头文件（当前仅 2201/AtlasA2 提供），真正的实现在其 `atlasa2/` 子目录里。

样例导航（按主题）：

| 样例 | 平台 | 主题 | 反量化位置 |
| --- | --- | --- | --- |
| `12_quant_matmul` | AtlasA2 | W8A8 int8 量化 | epilogue（搬出后） |
| `32_w4a8_matmul` | AtlasA2 | W4A8 int4 权重 | prologue（搬入前 cast + 随路 deq） |
| `29_a2_fp8_e4m3_matmul` | AtlasA2 | FP8 | prologue（AIV cast） |
| `53_ascend950_fp8_mx_matmul` | Ascend950 | MXFP8 | AIC 原生 MX（随 scale） |
| `63_ascend950_dual_level_quant_mx_batch_matmul` | Ascend950 | 二级量化 MXFP4 batch | AIV 预量化 + AIC MX |
| `65_ascend950_fp8_mx_grouped_matmul_slice_m_swiglu_mx_quant` | Ascend950 | 分组 MXFP8 + SwiGLU + 在线 MX 量化 | AIV 预量化 + AIC MX + 在线再量化 |
| `71_ascend950_fp8_mx_grouped_matmul_finalize_routing` | Ascend950 | 分组 MXFP8 + FinalizeRouting（确定性/非确定性两版） | AIC MX + AIV 路由后处理 |

---

## 4. 核心概念与源码讲解

### 4.1 权重量化反量化（epilogue vs prologue）

#### 4.1.1 概念说明

权重量化最典型的两种格式是 **W8A16 / W8A8**（权重 8 位）与 **W4A8**（权重 4 位、激活 8 位）。它们的输入 A、B 不是普通 fp16，而是 int8/int4。问题在于：Mmad 指令只能对**相同类型、且硬件支持的**操作数做乘累加。于是反量化 scale 该插在哪里，就分成了两条路线：

- **epilogue 反量化（搬出后）**：A、B 都是 int8，硬件能直接做 int8×int8 矩阵乘，在 L0C 按 **int32** 累加。scale（per-channel 权重 scale + per-token 激活 scale）留到 AIV epilogue 里乘，最后 cast 到 fp16。代表样例 `12_quant_matmul`。
- **prologue 反量化（搬入前）**：当某一边是 int4（无 int4 matmul 指令），必须**先**把它 cast 成 int8（在 AIV 上跑一个 prologue pass，写到 workspace），AIC 再用转换后的 int8 算；per-tensor 的 scale 在搬运/fixpipe 随路乘上。代表样例 `32_w4a8_matmul`。

两者的取舍是本模块的核心：

| 维度 | epilogue 反量化 | prologue 反量化 |
| --- | --- | --- |
| scale 粒度 | 细：per-channel + per-token | 粗：per-tensor（标量） |
| matmul 域 | 整数 int32 累加（精确、省带宽） | 转换后随路 dequant，输出直出 fp16 |
| 额外开销 | workspace 存 int32 中间结果 + AIV epilogue pass | workspace 存 int8 转换后权重 + AIV prologue pass |
| 适用 | 双边都是 int8 | 某边是 int4 等无直接指令的类型 |

#### 4.1.2 核心流程

**epilogue 反量化（`12_quant_matmul`）数据流：**

```text
GM A(int8), B(int8)
   │  AIC: BlockMmad 做 int8×int8 → L0C int32 累加
   ▼
GM workspace (ElementC = int32)        ← 中间结果暂存
   │  AIV: BlockEpilogue 读 int32，先乘 per-channel scale，
   │       再乘 per-token scale，cast 到 fp16
   ▼
GM D (fp16)                             ← 最终输出
```

其数学形式为：

\[
D_{ij} = \text{tscale}_i \cdot \bigl(\text{wscale}_j \cdot \sum_k A_{ik} B_{kj}\bigr), \qquad A,B \in \text{int8},\ \sum \in \text{int32}
\]

整数乘累加精确无误，scale 只在最后一次性乘进去。

**prologue 反量化（`32_w4a8_matmul`）数据流：**

```text
GM A(int8), B(int4)
   │  AIV: blockMmad.Prologue() 把 int4 B → int8 B'，写到 workspace
   ▼
GM workspace (int8 B')
   │  AIC: BlockMmad 用 A(int8) 与 B'(int8) 做 matmul，
   │       per-tensor scalar 在 TileCopyWithPrologueDeqPerTensor 随路乘
   ▼
GM C (fp16)                             ← 直接输出 fp16
```

#### 4.1.3 源码精读

**(1) epilogue 范式的 Kernel：`QuantMatmul`**

`QuantMatmul` 是一个典型的 mix kernel，AIC 与 AIV 各司其职。AIC 侧只做整数 matmul，把结果写到 workspace（注意 `gmC` 绑定的是 `ptrWorkspace`，不是最终输出）：

[include/catlass/gemm/kernel/quant_matmul.hpp:141-143](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/kernel/quant_matmul.hpp#L141-L143) —— AIC 把 `ElementC`（int32）的累加结果写入 workspace 的 `gmC`，`layoutC` 现场构造为 RowMajor。

[include/catlass/gemm/kernel/quant_matmul.hpp:150-174](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/kernel/quant_matmul.hpp#L150-L174) —— AIC 的 SPMD 分核循环：按 `BlockScheduler` 分配 C 基本块，调用 `blockMmad` 完成整数乘累加；若策略 `ASYNC` 则用 callback 触发跨核同步，否则循环末尾手动调用 `aicFinishSync()`。

AIC 写完一块后，要通知对应的 AIV「这块的 int32 结果可以读了」。这里用的是跨核同步原语 `AicFinishSync` / `AivWaitSync`，一对 Set/Wait Flag：

[include/catlass/gemm/kernel/quant_matmul.hpp:53-75](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/kernel/quant_matmul.hpp#L53-L75) —— `AicFinishSync` 在 AIC 的 `PIPE_FIX` 上 `CrossCoreSetFlagWithReverse`，`AivWaitSync` 在 AIV 的 `PIPE_MTE3` 上 `CrossCoreWaitFlagWithReverse`。这就是「AIC fixpipe 搬出 ↔ AIV 读取」的 tile 粒度握手。

AIV 侧的 `operator()<AIV>` 才是反量化发生的地方：

[include/catlass/gemm/kernel/quant_matmul.hpp:184-225](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/kernel/quant_matmul.hpp#L184-L225) —— AIV 用 `blockScheduler` 切同样的 MN 网格，从 workspace 读 int32 的 `gmBlockC`，组装 `EpilogueParams`（含 `ptrScale`/`ptrPerTokenScale`），调用 `blockEpilogue(...)` 完成乘 scale + cast，最终写回 `ptrD`（fp16）。

> 在 Host 端（`examples/12_quant_matmul/quant_matmul.cpp:126-153`）可以看到这条链路的组装：`CType = GemmType<int32_t, ...>`、`BlockEpilogue` 里挂了 `TileRowBroadcastMul`（per-channel）、`TileOneBlkColumnBroadcastMul`（per-token）、`TileCopy`（cast 到 fp16）。这正好对应上面的两层 scale 乘法。

**(2) prologue 范式的 Kernel：`W4A8Matmul`**

`W4A8Matmul` 同样是 mix kernel，但**反量化在 AIC 数据通路里、int4→int8 cast 在 AIV prologue 里**。先看 AIV 的 prologue：

[include/catlass/gemm/kernel/w4a8_matmul.hpp:167-197](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/kernel/w4a8_matmul.hpp#L167-L197) —— AIV 按 `matmulBlockScheduler` 切 KN 网格，读 int4 的 `gmPrologueB`，调用 `blockMmad.Prologue(...)` 把转换后的 int8 写到 workspace 的 `gmBlockB`（按 `aicoreIdx * Capacity * STAGES` 分核排布）。

[include/catlass/gemm/kernel/w4a8_matmul.hpp:129-165](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/kernel/w4a8_matmul.hpp#L129-L165) —— AIC 侧从 workspace 读转换后的 int8 `gmBlockB`，与 `gmBlockA`(int8) 做 matmul，结果直出 `ElementC`(fp16)。注意第 146 行 `gmOffsetB = aicoreIdx * layoutBlockB.Capacity() * BlockMmad::STAGES`——每核独占一段 workspace，避免与 AIV prologue 写入冲突。

`workspace` 大小由 `GetWorkspaceSize` 决定，正是「prologue 范式需要额外存转换后权重」的体现：

[include/catlass/gemm/kernel/w4a8_matmul.hpp:100-103](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/kernel/w4a8_matmul.hpp#L100-L103) —— workspace = `STAGES * K * N * sizeof(ElementB) * aicoreNum`，即每个 AIC 核、每个缓冲 stage 一块 int8 的 B。

**(3) int4→int8 cast 微内核**

转换本身在 Tile 层完成。`TileCastInt4ToInt8` 是一个 AIV 上跑的 UB 微内核，用 `DataCopyPad` 把 int4（每字节存 2 个）搬进 UB，再用 `Cast` 拆成 int8：

[include/catlass/gemm/tile/atlasa2/cast_int4_to_int8.hpp:122-160](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/tile/atlasa2/cast_int4_to_int8.hpp#L122-L160) —— 核心循环：`DataCopyPad`（GM→UB，int4 按 `(tileLen+1)/2` 字节）→ `WaitFlag<MTE2_V>` → 两次 `Cast`（先 int4→half workspace，再 half→int8 输出）→ `DataCopyPad`（UB→GM int8）。注意它用 `STAGES`（默认 2）做乒乓，靠 `V_MTE2`/`MTE3_V` HardEvent 在搬运与计算间握手。

> 一个关键细节：`sizeof(ElementSrc) == sizeof(ElementDst)` 的 static_assert（第 36 行）看似奇怪——其实 int4 在 C++ 里没有原生类型，`ElementSrc` 用的是 `AscendC::int4b_t`，`sizeof` 与 int8 相同，真实的「半字节」语义靠 `DataCopyExtParams` 里 `(tileLen+1)/2 * sizeof(int8_t)` 的字节折算体现。

#### 4.1.4 代码实践

**实践目标**：亲手比对 epilogue 与 prologue 两条反量化路径，理解 scale 位置差异。

**操作步骤（源码阅读型）**：

1. 打开 `examples/12_quant_matmul/quant_matmul.cpp`，定位 `CType = Gemm::GemmType<int32_t, ...>`（约 126-128 行），确认累加类型是 int32；再找到 `BlockEpilogue` 的组装（约 151-153 行），数一数挂了几个乘 scale 的 Tile 组件。
2. 打开 `examples/32_w4a8_matmul/w4a8.cpp`，定位 `PrologueB = Gemm::Tile::TileCastInt4ToInt8<...>`（约 130 行）与 `TileCopy = Gemm::Tile::TileCopyWithPrologueDeqPerTensor<...>`（约 132 行），确认：cast 在 prologue、dequant（per-tensor）随 TileCopy 走。
3. 对照 `w4a8.cpp:55` 的 `float scalar = 1.5` 与 `quant_matmul.cpp` 里的 `hostScale`/`hostPerTokenScale`（数组），体会「per-tensor 标量」与「per-channel/per-token 数组」的粒度差。

**需要观察的现象 / 预期结果**：

- `12_quant_matmul` 中 scale 是**两个数组**（per-channel `hostScale[n]` + per-token `hostPerTokenScale[m]`），说明反量化在 epilogue、可支持细粒度；其 `ElementC=int32`，workspace 非零。
- `32_w4a8_matmul` 中 scale 是**单个标量** `scalar`，说明 dequant 是 per-tensor，随路乘在搬运里；其 `ElementC=half`，输出直出，不需要 epilogue 再乘 scale。

> 这两个样例都依赖真实 NPU（AtlasA2）。若本地无 NPU，本实践以源码阅读为准；如需运行，可用 `bash scripts/build.sh 12_quant_matmul` / `32_w4a8_matmul` 编译后执行，预期均输出 `Compare success.`。**待本地验证运行。**

#### 4.1.5 小练习与答案

**练习 1**：为什么 `12_quant_matmul` 敢把 scale 留到最后乘，而不怕精度损失？

**答案**：因为 int8×int8 的乘累加在 L0C 按 **int32** 精确累加（无溢出、无舍入），整数域的 \(\sum_k A_{ik}B_{kj}\) 是完全精确的；scale 只是最后一次性线性放大，唯一精度损失来自末尾 int32→fp16 的 cast，远小于「先反量化成 fp16 再累加」的多次舍入。

**练习 2**：`32_w4a8_matmul` 为什么必须先在 AIV 把 int4 cast 成 int8，而不能像 12 那样直接喂给 AIC？

**答案**：因为昇腾没有 int4 的 Mmad 指令。W4A8 里 B 是 int4，必须先转成硬件支持的 int8 才能进入 AIC 的 matmul 数据通路；A 是 int8 本身可直接用，所以只对 B 做 prologue cast（`PrologueA = void`）。

---

### 4.2 FP8 路径（AIV cast + AIC fp16 matmul）

#### 4.2.1 概念说明

FP8（如 e4m3 / e5m2）用 8 位浮点大幅降低带宽，但同样面临「硬件能否直接做 FP8×FP8 矩阵乘」的问题。在 AtlasA2 上，`FP8Matmul` 采用与 W4A8 类似的 **prologue cast** 思路：先在 AIV 上把 FP8 反量化（cast）成 fp16 并乘上 per-tensor 的 scalar+zeroPoint，写到 workspace；AIC 再用普通的 fp16×fp16 矩阵乘（L0C 按 fp32 累加）。

与权重量化的区别：

- 输出 `ElementC` 来自 `PrologueA/B` 的源类型推导——若设了 Prologue，`ElementA_` 取 `PrologueA::ElementSrc`（FP8），否则取 `ElementA`。这让 Kernel 同时记录「物理输入类型（FP8）」与「matmul 实际类型（fp16）」。
- FP8 的 cast 不是简单类型转换，而是带 **scalar/zeroPoint 反量化**的位操作（见 4.2.3）。

> 在 Ascend950 上，FP8 有原生的 **MX FP8** 路径（样例 53），AIC 直接吃 FP8 数据 + e8m0 scale，无需 AIV cast。这放到 4.3 讲。

#### 4.2.2 核心流程

```text
GM A(fp8), B(fp8), scalar, zeroPoint
   │  AIV: blockMmad.Prologue() 对 A、B 做 TileCastFp8ToFp16Dequant
   │       → 乘 scalar、加 zeroPoint，写 fp16 到 workspace gmWA/gmWB
   ▼
GM workspace: gmWA(fp16), gmWB(fp16), gmWC(float, 中间累加)
   │  AIC: 用 gmWA × gmWB 做 fp16 matmul，L0C fp32 累加到 gmWC
   ▼
GM C (fp16)
```

反量化公式（per-tensor）：

\[
\hat{x}_{fp16} = \text{cast\_fp8\_to\_fp16}(q_{fp8}) \times \text{scalar} + \text{zeroPoint}
\]

#### 4.2.3 源码精读

**(1) Kernel：`FP8Matmul` 的类型推导与双核分工**

[include/catlass/gemm/kernel/fp8_matmul.hpp:33-44](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/kernel/fp8_matmul.hpp#L33-L44) —— 当存在 `PrologueA`/`PrologueB` 时，`ElementA_`/`LayoutA_` 取 prologue 的 **源**（`ElementSrc`/`LayoutSrc`，即 FP8），否则退化为 `ElementA`。`Params::layoutA` 也用 `LayoutA_`，这样 Host 传入的是 FP8 的布局，Kernel 内部按 FP8 寻址。

AIV 侧跑 prologue cast（注意它一次循环处理「两个行块或两个列块」）：

[include/catlass/gemm/kernel/fp8_matmul.hpp:158-201](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/kernel/fp8_matmul.hpp#L158-L201) —— AIV 用 `MakeCoord(L1TileShape::M * mScalar, L1TileShape::N * nScalar)` 构造调度块（`mScalar/nScalar` 是 AIV 处理粒度倍数），从 `gmA`(fp8) 读、写到 `gmWA`(fp16)，调用 `blockMmad.Prologue(...)`。`mScalar/nScalar` 把多个 L1 tile 合并到一个 AIV 反量化任务，提升 AIV 吞吐。

AIC 侧才是真正的 matmul：

[include/catlass/gemm/kernel/fp8_matmul.hpp:203-227](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/kernel/fp8_matmul.hpp#L203-L227) —— AIC 用 `gmWA`/`gmWB`(fp16) 做 matmul，结果写 `gmWC`(float)。注意它不再读原始 FP8 的 `ptrA`/`ptrB`，而是读 workspace 里 cast 后的 fp16——这正是 prologue 范式的标志。

**(2) FP8→fp16 反量化微内核**

cast 的难点：FP8 的指数/尾数位宽与 fp16 不同，要用 UB 上的位操作（移位、掩码、或运算）把 FP8 的位模式重排成 fp16，再乘 scalar。`TileCastFp8ToFp16Dequant::Dequant` 完成这件事：

[include/catlass/gemm/tile/atlasa2/cast_fp8_to_fp16.hpp:324-373](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/tile/atlasa2/cast_fp8_to_fp16.hpp#L324-L373) —— 一次 `Dequant` 的位操作序列：`Cast<half,uint8_t>`（先把 fp8 字节当 uint8 转 half）→ `Adds(1024)`（调整指数偏置）→ `ShiftLeft(7)`（对齐到 fp16 指数位）→ `And(value_vector1)`（掩码提取指数）→ `ShiftLeft(1)` → `And(value_vector2)`（提取尾数）→ `Or`（拼成 fp16）→ `Muls(1<<8)` → `Adds(zeroPoint)` → `Muls(scalar)`。其中 `value_vector1=0x4000`、`value_vector2=0x3FFF` 是预置的掩码向量。

外层 `operator()` 负责 GM↔UB 的搬运与多 tile 合并：

[include/catlass/gemm/tile/atlasa2/cast_fp8_to_fp16.hpp:158-186](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/tile/atlasa2/cast_fp8_to_fp16.hpp#L158-L186) —— 每轮：`WaitFlag<V_MTE2>` → `DataCopyPad`（GM fp8 → UB）→ `SetFlag<MTE2_V>` → `Dequant` → `DataCopyPad`（UB fp16 → GM）→ `SetFlag<V_MTE2>`，buffer 在 `BUFFER_NUM=2` 间轮转。`COMPUTE_LENGTH` 控制单次处理元素数，`tilesInALoop` 决定一轮合并几个 tile。

#### 4.2.4 代码实践

**实践目标**：跟踪 FP8 的「源类型 → matmul 类型」推导链。

**操作步骤**：

1. 在 `include/catlass/gemm/kernel/fp8_matmul.hpp:33-44` 处，假设 `PrologueA` 非 void 且 `ElementSrc` 是 `float8_e4m3_t`，写出 `ElementA_` 的推导结果。
2. 阅读 `TileCastFp8ToFp16Dequant::Dequant`（cast_fp8_to_fp16.hpp:324-373），把 11 步位操作画成一条流水，标出哪一步把 FP8 指数对齐到 fp16、哪一步乘 scalar。
3. 对照 `examples/29_a2_fp8_e4m3_matmul`（如有），确认 Host 传入的 `scalar`/`zeroPoint` 如何经 `MmadParams` 传到 `Params`（fp8_matmul.hpp:141 的 `{{args.scalar, args.zeroPoint}, ...}`）。

**需要观察的现象 / 预期结果**：

- `ElementA_` 解析为 `float8_e4m3_t`（FP8），而 AIC matmul 实际用的是 cast 后的 `half`。
- 位操作流水里，`ShiftLeft(7)` 是把 FP8 指数挪到 fp16 指数位段的关键一步；末尾 `Muls(scalar)` 才把 per-tensor scale 乘上。

> 运行型实践依赖 AtlasA2 NPU。**待本地验证运行。**

#### 4.2.5 小练习与答案

**练习 1**：FP8 路径里 `gmWC` 为什么是 `float` 而不是 `half`？

**答案**：因为 fp16×fp16 在 L0C 按 **fp32** 累加（`ElementAccumulatorSelector` 把 half/bf16 升到 float）。`gmWC` 存的是累加结果，故用 float；最终输出 C 再 cast 回 fp16。

**练习 2**：为什么 `FP8Matmul` 要分别记录 `ElementA`（matmul 类型）与 `ElementA_`（源类型）？

**答案**：因为物理输入是 FP8（`ElementA_`，用于 Host 传参与 GM 寻址），而 AIC 实际 matmul 用的是 cast 后的 fp16（`ElementA`）。两者不同，需要分别记录，才能让「按 FP8 布局算偏移、按 fp16 做计算」同时成立。

---

### 4.3 MX 微缩放与二级量化（AIV 预量化 + AIC MX matmul）

#### 4.3.1 概念说明

**MX（Microscaling）** 是 OCP 的低比特浮点量化标准。与 per-tensor/per-channel 不同，MX 给**每 32 个连续元素**配一个独立的 power-of-2 scale（用 `float8_e8m0_t` 表示，即 8 位纯指数，值 = \(2^{e-127}\)）。常见格式：

- **MXFP8**：数据 e4m3/e5m2 + e8m0 scale（per-32）。
- **MXFP4**：数据 e2m1（4 位）+ e8m0 scale（per-32），数据量更小、对 scale 精度更敏感。

反量化与量化分别为：

\[
\hat{x}_i = q_i \times 2^{e_{b}-127}, \quad b = \bigl\lfloor i / 32 \bigr\rfloor, \qquad
q_i = \text{round}\bigl(x_i / 2^{e_b-127}\bigr)
\]

MX 矩阵乘即 \(C = (s^A \odot A_{fp}) @ (s^B \odot B_{fp})\)，scale 沿各自的 MX 块作用。

在 **Ascend950** 上，硬件有原生 MX matmul 支持，AIC 可直接吃 FP8/FP4 数据 + e8m0 scale，无需 AIV cast（样例 53）。CATLASS 用 `MmadMx` 调度策略 + `PackedMxTileCopyTla` 承载这条原生路径。

**二级量化（dual-level）** 是在 MX 基础上的增强，专为 FP4 这种极低精度设计。标准 MX 只有一级 e8m0 scale（per-32）；二级量化再加一级**更粗的 fp32 scale（per-512）**，两级共同刻画动态范围：

\[
\hat{x}_i = s^{(0)}_{\lfloor i/512 \rfloor} \cdot 2^{e^{(1)}_{\lfloor i/32 \rfloor}-127} \cdot q_i
\]

其中 LEVEL0（\(s^{(0)}\)，fp32，per-512）是粗粒度、高精度；LEVEL1（\(e^{(1)}\)，e8m0，per-32）是细粒度 MX scale，AIC MX matmul 原生消费这一级。两级层次化标定，能在 FP4 极窄的表示范围内兼顾动态范围与精度。>（两级 scale 的精确组合方式以 `block_epilogue_dual_level_quant_mx.hpp` 的 AIV 量化实现为准；上面是层次化标定的标准解释。）

#### 4.3.2 核心流程（AIV 预量化 + AIC MX matmul 协作）

新迁入的三个样例（63/65/71）都遵循「AIV 侧先把输入量化到 workspace，再通知 AIC 做 MX matmul」的协作范式：

```text
GM 输入 (fp16 / fp8)
   │  AIV: BlockQuant* 把输入全量量化
   │       → FP4/FP8 packed 数据 + e8m0 scale (+ LEVEL0 fp32 scale)
   │       → 写入 workspace
   ▼
GM workspace (量化数据 + scales)
   │  同步: SyncAll<false>() 或 CrossCore Flag
   ▼
   │  AIC: BlockMmadTla (MmadMx + PackedMxTileCopyTla) 原生 MX matmul
   ▼
GM 输出 (fp32 / bf16)
```

三个样例的变体：

- **63（二级量化 MXFP4 batch）**：AIV 用 `BlockQuantDualLevelMx` 把 fp16 A/B 全量量化为 FP4 + 两级 scale，写 workspace；AIC 做 MX FP4 batch matmul。同步用 **一次 `SyncAll<false>()`**（全核屏障）。
- **65（分组 MXFP8 + SwiGLU + 在线 MX 量化）**：AIC 做分组 MXFP8 matmul，AIV 把结果按 N 轴均分成 Act/Gate，做 SwiGLU 激活后**再在线 MX 量化**成 FP8 输出。
- **71（分组 MXFP8 + FinalizeRouting，确定性/非确定性两版）**：AIC 分组 MXFP8 matmul 写 workspace，AIV 做 Logit 加权 + Scatter Add 聚合（FinalizeRouting）。提供两版调度：确定性版 `ColumnBlockSwizzle`、非确定性版 `GemmGroupedAswtTailSplitSwizzle`（尾块多核拆分，多核利用率更高）。

#### 4.3.3 源码精读

**(1) MX 调度策略 `MmadMx`**

[include/catlass/gemm/dispatch_policy.hpp:430-444](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/dispatch_policy.hpp#L430-L444) —— `MmadMx` 继承 `MmadBase<ArchTag, false>`（ASYNC=false），关键参数：`ENABLE_UNIT_FLAG`、`L1_SCALE_FACTOR_K`（GM→L1 的 MX scale 一次驻留的 L1 K 条带个数，默认 16）、各级 `STAGES`、`ENABLE_L1_RESIDENT`。它驱动底层 `BlockMmadTla` 选 MX 主循环实现，并由 `PackedMxTileCopyTla` 在搬运时把 FP4/FP8 + e8m0 scale 一起喂给硬件 MX 指令。

> 对照：W4A8 用的 `MmadAtlasA2PingPongWithPrologue`（dispatch_policy.hpp:37-41）继承 `MmadAtlasA2`、`STAGES=2`，是为「带 prologue 的乒乓」设计的；而 `MmadMx` 是为「原生 MX」设计的——两者是不同硬件能力下的不同策略。

**(2) 二级量化样例 63 的 Host 组装**

类型选型是理解这个样例的钥匙：

[examples/63_ascend950_dual_level_quant_mx_batch_matmul/dual_level_quant_mx_batch_matmul.cpp:94-98](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/examples/63_ascend950_dual_level_quant_mx_batch_matmul/dual_level_quant_mx_batch_matmul.cpp#L94-L98) —— 输入 `ElementInput=float16_t`；MX matmul 的 A/B 是 `float4_e2m1x2_t`（FP4，两个 FP4 打包成一字节）；MX scale 是 `float8_e8m0_t`；输出 C 是 `bfloat16_t`。

两级 scale 的尺寸计算——直接印证 LEVEL0(per-512)/LEVEL1(per-32)：

[examples/63_ascend950_dual_level_quant_mx_batch_matmul/dual_level_quant_mx_batch_matmul.cpp:116-125](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/examples/63_ascend950_dual_level_quant_mx_batch_matmul/dual_level_quant_mx_batch_matmul.cpp#L116-L125) —— `scaleA1K = CeilDiv<512>(k)` 对应 LEVEL0（fp32，per-512），`sizeScaleA1/A2` 用 `sizeof(float)`；`mxScaleK = CeilDiv<MX_SCALE_GROUP_NUM>(k)`（`MX_SCALE_GROUP_NUM=32`）对应 LEVEL1（e8m0，per-32），`sizeScaleA2` 用 `sizeof(ElementMxScale)`。

AIC matmul 与 AIV 量化的组件分别组装：

[examples/63_ascend950_dual_level_quant_mx_batch_matmul/dual_level_quant_mx_batch_matmul.cpp:186-219](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/examples/63_ascend950_dual_level_quant_mx_batch_matmul/dual_level_quant_mx_batch_matmul.cpp#L186-L219) —— AIC 侧：`DispatchPolicy = MmadMx<Ascend950, true, 16>`（开 unitFlag，L1_SCALE_FACTOR_K=16），`L1TileShape=256×256×512`、`L0TileShape=256×256×256`，`TileCopyMmad = PackedMxTileCopyTla<...>`（带 A/B 的 e8m0 scale 布局），`BlockMmad = BlockMmadTla<...>`。

[examples/63_ascend950_dual_level_quant_mx_batch_matmul/dual_level_quant_mx_batch_matmul.cpp:232-238](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/examples/63_ascend950_dual_level_quant_mx_batch_matmul/dual_level_quant_mx_batch_matmul.cpp#L232-L238) —— AIV 侧：`TileCopyQuant = TileCopyDualLevelQuantMx<...>`（吃 InputType/OutputType/Scale1Type/Scale2Type 四个 GemmType），`BlockQuant = BlockQuantDualLevelMx<...>`（量化子块 `QuantSubTileShape=128×512`）。

launch 核数策略也值得注意——它**总是按物理 AIC 数 launch**，而不是 `min(aicCoreNum, taskNum)`：

[examples/63_ascend950_dual_level_quant_mx_batch_matmul/dual_level_quant_mx_batch_matmul.cpp:240-249](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/examples/63_ascend950_dual_level_quant_mx_batch_matmul/dual_level_quant_mx_batch_matmul.cpp#L240-L249) —— 注释明确说明：single-kernel 路径按物理 AIC 数 launch，空闲 AIC（`loopIdx >= coreLoops`）自然跳过 matmul 循环无害退出；收益是 AIV 端的 `QuantAllScheduler` 能用上全部 `aicCoreNum * 2` 个 AIV subblock，提升量化并行度。

**(3) AIV 预量化 → AIC MX matmul 的同步**

协作的关键是同步。样例 63 用单次全核 `SyncAll<false>()`，AIV 和 AIC 两侧必须配对调用：

[include/catlass/gemm/kernel/dual_level_quant_mx_batched_matmul_tla.hpp:409-411](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/kernel/dual_level_quant_mx_batched_matmul_tla.hpp#L409-L411) —— AIV 侧完成全量量化后，`PipeBarrier<PIPE_ALL>` → `SyncAll<false>()`，通知 AIC「workspace 里的量化数据就绪」。

[include/catlass/gemm/kernel/dual_level_quant_mx_batched_matmul_tla.hpp:415-426](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/kernel/dual_level_quant_mx_batched_matmul_tla.hpp#L415-L426) —— AIC 侧**开头**就 `SyncAll<false>()` 等 AIV 量化完成（不再 per-tile callback）；随后空跑的核（launch 数 > coreLoops）也必须参与这次 SyncAll，否则 AIV 的 V↔C 同步会卡死。

AIC 拿到 workspace 里的 FP4 数据 + e8m0 scale 后，用 TLA 的 `GetTile` 切出每块的量化数据与对应 scale，调用 `blockMmad`：

[include/catlass/gemm/kernel/dual_level_quant_mx_batched_matmul_tla.hpp:471-490](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/kernel/dual_level_quant_mx_batched_matmul_tla.hpp#L471-L490) —— 对 A/B/MxScaleA/MxScaleB/C 分别 `GetTile`（注意 MX scale 的 K 坐标要除以 `MX_SCALE_GROUP_NUM=32`），然后 `blockMmad(tensorBlockA, tensorBlockB, tensorBlockC, actualBlockShape, tensorBlockMxScaleA, tensorBlockMxScaleB)`——把数据与两级中的原生可消费的 e8m0 scale 一起喂给 MX matmul。

> 对照 4.1 的 `QuantMatmul`（per-tile CrossCore Flag 握手）与这里的 `SyncAll`（一次全核屏障）：粗粒度全量预量化适合一次性 `SyncAll`；而「AIC 边算边搬出、AIV 边读边后处理」的流式场景则更适合 tile 粒度 Flag（如样例 71 的 FinalizeRouting 用 CrossCore Flag 做 tile 级流水）。

#### 4.3.4 代码实践

**实践目标**：建立「AIV 预量化 → AIC MX matmul」的协作图，并理解样例 71 确定性/非确定性调度的取舍。

**操作步骤（源码阅读型 + 可选运行）**：

1. **画 63 的协作时序**：按 4.3.3 的引用，在一张图上画出 AIV（`BlockQuantDualLevelMx` 量化 → `SyncAll`）与 AIC（`SyncAll` → `BlockMmadTla` MX matmul）两条时间线，标出 workspace 的写入/读取点。
2. **核对两级 scale 尺寸**：用 63 的代码（L116-125）手算 `k=1024` 时 `scaleA1K`（LEVEL0 数）与 `mxScaleK`（LEVEL1 数），验证 512 与 32 两个 block size。
3. **对比 71 两版调度**：打开 `examples/71_ascend950_fp8_mx_grouped_matmul_finalize_routing/README.md` 的「确定性版与非确定性版差异」表，记录 Kernel/BlockScheduler/BlockEpilogue/尾块处理四列的差异。
4. **（可选）运行 63**：`bash scripts/build.sh 63_ascend950_dual_level_quant_mx_batch_matmul -DCATLASS_ARCH=3510` → `python3 examples/63_ascend950_dual_level_quant_mx_batch_matmul/gen_data.py 1 1024 1024 1024` → `./output/bin/63_ascend950_dual_level_quant_mx_batch_matmul 1 1024 1024 1024 0`。

**需要观察的现象 / 预期结果**：

- `k=1024`：`scaleA1K = CeilDiv(1024,512) = 2`（LEVEL0，2 个 fp32 scale），`mxScaleK = CeilDiv(1024,32) = 32`（LEVEL1，32 个 e8m0 scale）。两者比例正好 16:1（512/32）。
- 71 确定性版用 `ColumnBlockSwizzle`（按列分块、无尾块拆分），输出顺序确定；非确定性版用 `GemmGroupedAswtTailSplitSwizzle`（`startBlockIdx_` 跨 group 滚动 + 尾部 tile 多核拆分 `UpdateTailTile`），多核利用率更高但非确定。两版输入参数与精度一致。
- 运行 63 预期输出 `Compare success.`。**待本地验证运行。**

#### 4.3.5 小练习与答案

**练习 1**：为什么样例 63 要用两级量化，而样例 53（MXFP8）只用一级 MX scale？

**答案**：63 的数据是 **FP4（e2m1，仅 4 位）**，表示范围极窄，单级 e8m0（power-of-2）per-32 scale 难以兼顾大动态范围与局部精度；加一级更粗的 fp32 per-512 scale（LEVEL0）做层次化标定，可在 FP4 的窄表示下显著提升精度。MXFP8（53）本身 8 位、动态范围足够，一级 MX scale 即可。

**练习 2**：样例 63 的 launch 用了「按物理 AIC 数 launch 而非 `min(aicCoreNum, taskNum)`」，这样做的一个好处和一个前提分别是什么？

**答案**：好处是 AIV 端量化能用上全部 `aicCoreNum * 2` 个 AIV subblock，提升量化并行度；前提是空闲 AIC（任务不足时 `loopIdx >= coreLoops`）必须**仍参与 `SyncAll<false>()`**，否则 AIV 侧的 V↔C 同步会因等待方不足而卡死。

---

## 5. 综合实践

**综合任务**：把本讲三条量化路径整理成一张「反量化位置决策表」，并为每种场景选一条路径。

请按下表填空（答案见后）：

| 场景 | A/B 类型 | 推荐路径 | 反量化位置 | 代表样例 |
| --- | --- | --- | --- | --- |
| LLM 推理，权重 int8、激活 int8，要 per-token 精度 | int8/int8 | ? | ? | ? |
| 权重 int4，无 int4 指令 | int8/int4 | ? | ? | ? |
| AtlasA2 上的 FP8 推理 | fp8/fp8 | ? | ? | ? |
| Ascend950 上的 FP8 推理，硬件支持 MX | fp8/fp8 + e8m0 | ? | ? | ? |
| Ascend950 上极低比特 FP4，要高精度标定 | fp4/fp4 + 两级 scale | ? | ? | ? |

**参考答案**：

1. int8/int8 → **epilogue 反量化**（`QuantMatmul`），整数 int32 累加后乘 per-channel+per-token scale，样例 `12_quant_matmul`。
2. int8/int4 → **prologue 反量化**（`W4A8Matmul`），AIV 先 int4→int8 cast、per-tensor scalar 随路 dequant，样例 `32_w4a8_matmul`。
3. fp8(A2) → **prologue cast**（`FP8Matmul` + `TileCastFp8ToFp16Dequant`），AIV cast 到 fp16 再 AIC fp16 matmul，样例 `29_a2_fp8_e4m3_matmul`。
4. fp8(950)+MX → **AIC 原生 MX**（`MmadMx` + `PackedMxTileCopyTla`），AIC 直接吃 e8m0 scale，样例 `53_ascend950_fp8_mx_matmul`。
5. fp4+两级 → **AIV 预量化（二级）+ AIC MX matmul**（`DualLevelQuantMxBatchedMatmulTla`），AIV 产 FP4+LEVEL0(fp32,per-512)+LEVEL1(e8m0,per-32)，`SyncAll` 后 AIC 算，样例 `63_ascend950_dual_level_quant_mx_batch_matmul`。

**延伸（可选）**：若你手头有 Ascend950 环境，依次跑通 `53`（一级 MXFP8）与 `63`（二级 MXFP4），对比相同 (M,N,K) 下两者的输出 dtype、workspace 占用与精度阈值，体会「比特数下降 → 需要更精细的 scale 标定」这一量化设计的核心矛盾。

## 6. 本讲小结

- 量化矩阵乘的本质难题是「反量化该插在数据流水的哪个位置」，CATLASS 据此分出 **epilogue / prologue / 随路 / 原生 MX** 多条路径。
- **权重量化**：epilogue 反量化（`QuantMatmul`）走 int32 精确累加 + 末尾乘细粒度 scale，适合双边 int8；prologue 反量化（`W4A8Matmul`）先 AIV cast int4→int8、per-tensor scalar 随路乘，适合无直接指令的 int4。
- **FP8**：AtlasA2 用 prologue cast（AIV 位操作 cast fp8→fp16 + scalar/zeroPoint，AIC fp16 matmul）；`ElementA_`/`ElementA` 双类型分别记录「源类型」与「matmul 类型」。
- **MX 微缩放**：per-32 的 e8m0 power-of-2 scale；Ascend950 有原生 MX matmul（`MmadMx` + `PackedMxTileCopyTla`），AIC 直接吃 FP8/FP4 + scale。
- **二级量化（dual-level）**：在 MX 一级 e8m0(per-32) 之外加一级粗粒度 fp32(per-512)，层次化标定以挽救 FP4 的窄表示；走「AIV 预量化全量到 workspace → `SyncAll<false>()` → AIC MX matmul」协作范式（样例 63/65/71）。
- 同步原语的选择随数据流形态而变：全量预量化用一次 `SyncAll`，流式 tile 级协作（如 71 FinalizeRouting）用 CrossCore Flag 做 tile 粒度流水；样例 71 还提供确定性（`ColumnBlockSwizzle`）与非确定性（`GemmGroupedAswtTailSplitSwizzle` + 尾块拆分）两种调度取舍。

## 7. 下一步学习建议

- **深入 EVG 后处理**：65/71 的 SwiGLU、在线 MX 量化、FinalizeRouting 都是 AIV 后处理，本质是 EVG（Epilogue Visitor Graph）声明式后处理的应用——继续学 **u6-l3 / u6-l4（EVG 框架与执行模型）**，理解如何用 Visitor 节点组合这些后处理。
- **分组矩阵乘机制**：65/71 都是 grouped 形态，配合 slice-M 调度——继续学 **u9-l1（GroupedMatmul 分组矩阵乘）**，理解 `groupList`、`GemmGroupedAswtTailSplitSwizzle` 等分组调度。
- **Ascend950 特性与迁移**：MX 原生 matmul、Mutex 同步、TLA 编程模型都是 950 新特性——继续学 **u10-l1 / u10-l2（A2→950 迁移与 950 特有能力）**，把本讲的 MX 路径放回 950 的整体能力图中。
- **建议精读源码**：`include/catlass/epilogue/block/block_epilogue_dual_level_quant_mx.hpp`（两级量化的精确算法）、`include/catlass/gemm/kernel/grouped_mx_matmul_slice_m_swiglu_mx_quant_tla.hpp`（在线再量化）、`include/catlass/gemm/block/block_mmad_pingpong_mutex_tla.hpp`（950 的 Mutex 同步主循环）。
