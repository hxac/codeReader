# 低精度与 Block-Scaled 类型

## 1. 本讲目标

本讲聚焦 CUTLASS 在 Blackwell（SM100）架构上的「块缩放低精度」GEMM：即 **NVFP4 / MXFP4 / MXFP8** 这类「数据本身很窄（4 比特或 8 比特）+ 每一小块配一个缩放因子（scale factor）」的数据类型。

学完本讲，你应当能够：

1. 说出 FP8、FP4 的位级表示，以及 `float_e4m3_t` / `float_e5m2_t` / `float_e2m1_t` 的取舍。
2. 解释「块缩放（block scaling）」为什么必要，区分 **E8M0（MX，仅 2 的幂）** 与 **E4M3（NV，带尾数）** 两类缩放因子，以及 `SFVecSize`（每多少个数据元素共享一个缩放因子）的含义。
3. 在源码中读懂 `Sm1xxBlkScaledConfig` 如何把缩放因子张量 SFA/SFB 的形状与 GEMM 分块对齐。
4. 看懂 `sm100_blockscaled_mma_warpspecialized.hpp` 主循环如何用 TMA 搬数据与缩放因子、用 UTCCP 把缩放因子送进 TMEM，并由硬件指令 `tcgen05.mma.blockscaled` 完成「反量化 + 乘加」一气呵成的计算。
5. 在 example 72 的三个子例（72a/72b/72c）之间切换数据类型与输出缩放。

> 前置：本讲假定你已学过 **u2-l8（CollectiveBuilder 与主循环）**，知道 CUTLASS 3.x 的 kernel + collective mainloop + epilogue 三段式、`CollectiveBuilder` 的自动组装、以及 Hopper 的 producer/consumer warp 分流与 `PipelineTmaAsync`。本讲把这些机制「搬到 Blackwell」，并加上一块缩放因子的搬运与硬件融合。

## 2. 前置知识

在进入源码前，先用直觉建立四个概念。

### 2.1 为什么要更窄的精度

大模型推理与训练里，矩阵乘法的算力往往是瓶颈。把权重和激活从 FP16/BF16 压到 **FP8（8 比特）** 甚至 **FP4（4 比特）**，可以把同样的数据用更少的字节表示：

- 显存带宽占用直接减半（FP16→FP8）或减到四分之一（FP16→FP4）。
- Tensor Core 的吞吐随位宽下降而成倍上升——Blackwell 的 `tcgen05.mma.blockscaled` 在 FP4 下吞吐约为 FP8 的 2 倍、约为 Hopper FP8 WGMMA 的 4 倍（见 example 72a 文件头说明）。

代价是表示精度下降：4 比特只能表达 16 个不同的值。为了在「精度」与「覆盖范围」之间取得平衡，业界采用 **块缩放（block scaling）**：把数据切成一小块一小块，每块配一个缩放因子，块内用窄精度表达「相对值」，缩放因子表达「这一块整体多大」。这正是本讲的主题。

### 2.2 块缩放的数学含义

设一个数据块里有 \( V \) 个元素（\( V \) 即 `SFVecSize`），整块共享一个缩放因子 \( s \)。块内第 \( i \) 个元素存的是窄精度值 \( x_i \)，它代表的真实数值是

\[ \hat{x}_i = x_i \times s. \]

于是矩阵乘 \( D = A \times B \) 在块缩放下，等价于

\[ D_{mn} = \sum_k \bigl(A_{mk}\,s^A_{mk}\bigr)\bigl(B_{kn}\,s^B_{kn}\bigr) = \sum_k \bigl(s^A_{mk}\,s^B_{kn}\bigr)\bigl(A_{mk}B_{kn}\bigr). \]

关键观察：**缩放因子的乘法可以与乘加融合**。Blackwell 的 `tcgen05.mma.blockscaled` 指令正是直接吃「窄精度数据 + 缩放因子」作为操作数，由硬件在 Tensor Core 内部完成「反量化（乘上缩放因子）+ 乘加累加」，软件无需先把 FP4 解包成 FP16/BF16。这一点决定了 CUTLASS 这类 collective 的整体设计。

### 2.3 两个缩放因子家族：MX 与 NV

CUTLASS 同时支持两套块缩放标准（README 明确列出）：

- **MX（Microscaling，OCP 开放标准）**：缩放因子是 **E8M0**——只有 8 比特指数、没有尾数，因此只能表示 2 的幂（\( 2^{e} \)）。代表类型 `mx_float4_t`、`mx_float8_t`。
- **NV（NVIDIA 自有，NVFP4）**：缩放因子是 **E4M3**——4 比特指数 + 3 比特尾数，可以表示非 2 的幂的缩放（粒度更细）。代表类型 `nv_float4_t`。

两者的「数据」都可以是同一个底层类型 `float_e2m1_t`（4 比特浮点），区别仅在缩放因子的位宽与每块的元素数 \( V \)。

### 2.4 Blackwell 的两块新硬件地基

要理解本讲的 collective，需要知道 Blackwell（SM100）相对 Hopper（SM90）新增的两点（u2-l9 讲过 Hopper 的 warp specialization）：

- **TMEM（Tensor Memory）**：每个 SM 私有的高速存储。UMMA 指令（`tcgen05.mma`，即 Blackwell 的矩阵乘加）的**累加器落在 TMEM** 里，而不是寄存器（这与 Hopper 的 wgmma 不同）。块缩放因子也被搬进 TMEM 供指令读取。
- **块缩放专用指令 `tcgen05.mma.blockscaled`**：在普通 UMMA 的基础上多接收 SFA、SFB 两个缩放因子操作数，自动完成「乘缩放 + 乘加」。本讲的核心 collective 就是围绕这条指令组织的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `examples/72_blackwell_narrow_precision_gemm/72a_blackwell_nvfp4_bf16_gemm.cu` | NVFP4×NVFP4→BF16 的端到端示例，本讲的主线 |
| `examples/72_blackwell_narrow_precision_gemm/72b_blackwell_nvfp4_nvfp4_gemm.cu` | NVFP4 输出且带输出缩放因子（SFD）的示例 |
| `examples/72_blackwell_narrow_precision_gemm/72c_blackwell_mixed_mxfp8_bf16_gemm.cu` | MXFP8×MXFP4→BF16 混合精度示例 |
| `include/cutlass/float_subbyte.h` | FP4 数据类型 `float_e2m1_t` 与块缩放包装类型 `mx_float4_t`/`nv_float4_t` |
| `include/cutlass/float8.h` | FP8 类型、E8M0/E4M3 缩放因子类型、`mx_float8_t` 包装 |
| `include/cutlass/detail/sm100_blockscaled_layout.hpp` | 缩放因子张量布局配置 `Sm1xxBlkScaledConfig`（本讲的「布局说明书」） |
| `include/cutlass/gemm/collective/sm100_blockscaled_mma_warpspecialized.hpp` | 块缩放主循环 collective（producer 搬运 + consumer 发指令） |
| `include/cutlass/gemm/dispatch_policy.hpp` | 块缩放相关的 Schedule 标签与 mainloop policy |
| `include/cutlass/arch/mma.h` | `OpClassBlockScaledTensorOp` 算子类标签 |
| `include/cute/atom/mma_traits_sm100.hpp` | 块缩放 MMA 指令的 traits（`SFVecSize`、TMEM 缩放因子片段） |

> 注意：本讲的示例文件名为 `72a/72b/72c_*.cu`（不是 manifest 中写的 `narrow_precision_gemm.cu`），请按实际文件名阅读。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**① FP8/FP4 表示** → **② 块缩放因子** → **③ 块缩放 collective** → **④ 缩放因子的加载与硬件反量化**。

---

### 4.1 FP8/FP4 表示

#### 4.1.1 概念说明

CUTLASS 用一组 C++ 结构体表达「窄精度浮点」。它们都继承自 `float_exmy_base`（`E` 指数位、`M` 尾数位），共同特点是「用整数存储位模式 + 提供与 `float` 的相互转换」。本模块只看三类：

- **`float_e4m3_t`**（FP8，4 指数 + 3 尾数）：精度优先，可表示的数值更密，是推理里最常用的 FP8。
- **`float_e5m2_t`**（FP8，5 指数 + 2 尾数）：动态范围优先，常用于反向传播梯度。
- **`float_e2m1_t`**（FP4，2 指数 + 1 尾数）：4 比特浮点，是 NVFP4/MXFP4 的底层数据类型。

它们都是「子字节类型」（`sizeof_bits < 8`），存储用一个 8 位/16 位整数打底，多个元素打包进一个字节（u1-l4 已讲过子字节打包与 `sizeof_bits`）。

#### 4.1.2 核心流程

以 `float_e2m1_t` 为例，2 比特指数 + 1 比特尾数 + 1 比特符号 = 4 比特，能表达 16 个值（带符号后是 ±8 个量级）。源码文件头注释给出了它的取值集合：

\[ \text{float\_e2m1\_t 的正数值集合} = \{0,\ 0.5,\ 1,\ 1.5,\ 2,\ 3,\ 4,\ 6\} \]

注意它**没有 Inf、没有 NaN、有 denormal**。这正是 FP4「粒度极粗」的写照——单独使用误差极大，必须靠块缩放因子把每一块的量级「拉回来」。这也是模块 ② 存在的根本原因。

#### 4.1.3 源码精读

`float_e2m1_t` 的定义与位级说明：

[include/cutlass/float_subbyte.h:71-79](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/float_subbyte.h#L71-L79) —— 注释列出 E2M1 的取值范围（±{0,0.5,1,1.5,2,3,4,6}）、无 Inf/NaN、指数偏置为 1，结构体继承 `float_exmy_base<E2M1,...>`。

FP8 的两个类型与上面的 E2M1 同源（都在 `float_exmy_base` 家族里），本讲不再逐行展开；它们是 `float_e4m3_t` / `float_e5m2_t`，存储各占 8 位。

#### 4.1.4 代码实践

**实践目标**：直观感受 FP4 的表示粒度。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 `include/cutlass/float_subbyte.h` 第 71–94 行，确认 `float_e2m1_t` 的正数值集合只有 8 个。
2. 打开 `include/cutlass/float8.h`，分别找到 `float_e4m3_t`、`float_e5m2_t` 的注释块（同样以 `// Exponent bias` 风格标注位段），比较它们的指数/尾数位分配。

**需要观察的现象**：E4M3 尾数多（3 位）、范围小；E5M2 指数多（5 位）、范围大但精度低；E2M1 两样都很少。

**预期结果**：你能用自己的话解释「为什么 FP4 单独用精度太差、必须配缩放因子」。

**待本地验证**：若有 CUDA 环境，可写一个把若干 `float` 值 round-trip 到 `float_e2m1_t` 再转回 `float` 的小程序，观察量化误差。

#### 4.1.5 小练习与答案

**练习 1**：`float_e2m1_t` 占多少比特？为什么说它是「子字节类型」？

> **答**：4 比特。因为 `sizeof_bits<float_e2m1_t>::value < 8`（u1-l4 定义），两个元素才凑满一个字节，所以 CUTLASS 用位打包存储（`Array<float_e2m1_t, N>` 走 subbyte 分支）。

**练习 2**：`float_e4m3_t` 与 `float_e5m2_t` 各适合什么场景？

> **答**：E4M3 尾数多、精度高、范围小，适合前向激活与权重；E5M2 指数多、范围大、精度低，适合反向梯度（动态范围大）。

---

### 4.2 块缩放因子（Block Scaling Factor）

#### 4.2.1 概念说明

窄精度数据本身粒度粗，CUTLASS 的解法是「**把数据切成块，每块配一个缩放因子**」。本模块回答两个问题：

1. **缩放因子本身是什么类型？** 两类：
   - `float_ue8m0_t`（E8M0，8 比特**无符号纯指数**）：只能表示 \( 2^{e} \)，用于 **MX** 家族（MXFP4/6/8）。
   - `float_ue4m3_t`（UE4M3，4 指数 + 3 尾数，无符号）：可表示非 2 的幂的缩放，用于 **NVFP4**。
2. **数据类型与缩放因子如何绑定？** CUTLASS 提供「包装类型」把「数据类型 + 缩放因子类型」配成一对：`mx_float4_t`、`mx_float8_t`、`nv_float4_t`，各自用 `DataType` 与 `ScaleFactorType` 两个内嵌类型别名声明这一对。

#### 4.2.2 核心流程

三对包装的绑定关系：

| 包装类型 | `DataType`（数据） | `ScaleFactorType`（缩放因子） | 标准 |
| --- | --- | --- | --- |
| `mx_float4_t<float_e2m1_t>` | `float_e2m1_t`（FP4） | `float_ue8m0_t`（E8M0） | OCP MXFP4 |
| `mx_float8_t<float_e4m3_t>` | `float_e4m3_t`（FP8） | `float_ue8m0_t`（E8M0） | OCP MXFP8 |
| `nv_float4_t<float_e2m1_t>` | `float_e2m1_t`（FP4） | `float_ue4m3_t`（UE4M3） | NVIDIA NVFP4 |

引入第二个关键量 **`SFVecSize`**（Scale Factor Vector Size）：**一个缩放因子覆盖多少个数据元素**。它决定了缩放因子张量比数据张量小多少倍。源码里这个值由 MMA 指令的 traits 给出（`TiledMma::SFVecSize`），概念上：

- NVFP4：每 16 个 FP4 元素共享 1 个缩放因子。
- MXFP4 / MXFP8：每 32 个元素共享 1 个缩放因子。

于是缩放因子张量的总元素数 ≈ 数据元素数 / `SFVecSize`。

#### 4.2.3 源码精读

`mx_float4_t` 与 `nv_float4_t` 把数据类型与缩放因子类型配对（注意两者 `ScaleFactorType` 不同）：

[include/cutlass/float_subbyte.h:493-513](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/float_subbyte.h#L493-L513) —— `mx_float4_t` 的 `ScaleFactorType = float_ue8m0_t`；`nv_float4_t` 的 `ScaleFactorType = float_ue4m3_t`；二者 `DataType` 都是模板参数（约束只能是 `float_e2m1_t`）。

MXFP8 包装（结构与 MXFP4 对称）：

[include/cutlass/float8.h:1319-1327](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/float8.h#L1319-L1327) —— `mx_float8_t<F8Type>` 的 `ScaleFactorType = float_ue8m0_t`、`DataType = F8Type`（限定 `float_e4m3_t`/`float_e5m2_t`）。

两类缩放因子本身的位级定义：

[include/cutlass/float8.h:1074-1081](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/float8.h#L1074-L1081) —— `float_ue4m3_t`（UE4M3，4 指数 + 3 尾数，范围 [0:448]，偏置 7）——NVFP4 专用缩放因子。

[include/cutlass/float8.h:1161-1163](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/float8.h#L1161-L1163) —— `float_ue8m0_t`（E8M0，纯 8 比特无符号指数）——MX 家族专用缩放因子；其 `convert_to_float` 只需把指数左移成浮点位（无舍入），因为它是纯 2 的幂。

> 这些包装类型**不是数值类型本身**，而是「类型对」的描述，供 `CollectiveBuilder` 在编译期推导出需要再搬运一份缩放因子张量。example 72 正是通过 `ElementA::DataType` 与 `ElementA::ScaleFactorType` 分别取数据指针与缩放因子指针。

#### 4.2.4 代码实践

**实践目标**：确认三种输入组合各自的缩放因子类型。

**操作步骤**：

1. 在 example 72a 第 96、101 行确认 `ElementA = ElementB = nv_float4_t<float_e2m1_t>`。
2. 在 example 72a 第 181–183 行确认 `block_SFA`/`block_SFB` 的元素类型是 `ElementA::ScaleFactorType`（即 `float_ue4m3_t`）。
3. 切到 example 72c 第 97、102 行，确认 `ElementA = mx_float8_t<float_e4m3_t>`、`ElementB = mx_float4_t<float_e2m1_t>`，于是它的缩放因子类型变成 `float_ue8m0_t`。

**预期结果**：你能在源码层面指出「NVFP4 用 UE4M3 缩放、MX 系列用 E8M0 缩放」的证据。

#### 4.2.5 小练习与答案

**练习 1**：为什么 MX 选用纯指数的 E8M0，而 NVFP4 选用带尾数的 UE4M3？

> **答**：E8M0 实现简单（只存指数，反量化是左移/乘 2 的幂），适合标准化与跨厂商一致；UE4M3 有尾数，缩放粒度更细，能在 FP4 极少可表示值的基础上进一步降低量化误差，是 NVIDIA 为追求精度做的增强。

**练习 2**：若 A 是 NVFP4（`SFVecSize=16`），M=K=1024，SFA 大约有多少个缩放因子？

> **答**：\( (1024\times1024)/16 = 65536 \) 个，每个 1 字节（UE4M3）≈ 64 KB；而 A 本体仅 \( 1024\times1024\times 4\text{bit}/8 = 512 \) KB，缩放因子开销约 12.5%。

---

### 4.3 块缩放 collective（Narrow Precision Collective）

#### 4.3.1 概念说明

有了「数据 + 缩放因子」两套张量，主循环 collective 要同时搬运并喂给 `tcgen05.mma.blockscaled`。CUTLASS 把这件事做成一个专门的 collective：

- **算子类标签** `OpClassBlockScaledTensorOp`（区别于普通 `OpClassTensorOp`）告诉 `CollectiveBuilder`「我要走块缩放路径」。
- **主循环策略** `MainloopSm100TmaUmmaWarpSpecializedBlockScaled` 对应文件 `sm100_blockscaled_mma_warpspecialized.hpp`，与普通 SM100 GEMM 的主循环同构，但多搬两份缩放因子、并在发 MMA 时把缩放因子作为额外操作数传入。
- **Schedule 标签家族**（`KernelScheduleBlockScaledGemmSm100`、`KernelScheduleMxNvf4Sm100`、`KernelScheduleMxf8f6f4Sm100` 等）让 builder 按数据类型选到正确的 1SM/2SM 特化。

这套设计与 u2-l8 讲过的 `CollectiveBuilder` 自动组装完全一致：你只要给 `ArchTag=Sm100`、`OperatorClass=OpClassBlockScaledTensorOp`、数据/布局/tile/cluster，builder 就推断出 `TiledMma`、TMA 拷贝原子、共享内存布局与流水线级数。

#### 4.3.2 核心流程

块缩放 collective 的主循环沿用 Hopper 的 warp specialization（u2-l9、u3-l1），但操作数从「A、B」两份变成「A、B、SFA、SFB」四份：

```
Producer warp(由 elect_one 选中 1 个线程):
  for 每个 K-tile:
    producer_acquire(等缓冲空闲)
    TMA: gmem A   -> smem A
    TMA: gmem B   -> smem B
    TMA: gmem SFA -> smem SFA      # 比普通 GEMM 多这两条
    TMA: gmem SFB -> smem SFB
    (TMA 硬件 complete_transaction 翻转满门屏障)

Consumer warp group:
  for 每个 K-tile:
    consumer_wait(等数据就绪)
    UTCCP: smem SFA -> TMEM SFA    # 缩放因子从共享内存搬进 TMEM
    UTCCP: smem SFB -> TMEM SFB
    for k_block in K-tile 内细分块:
      tcgen05.mma.blockscaled( A_smem, B_smem, TMEM_SFA, TMEM_SFB, D_tmem )
      # 硬件一次完成: 反量化(A*SFA, B*SFB) -> 乘加 -> 累加进 TMEM
    consumer_release(归还缓冲给 producer)
```

注意三处与 Hopper 的区别：① 缩放因子先入 smem 再入 **TMEM**（不是寄存器）；② MMA 累加器在 **TMEM**；③ 缩放因子作为 MMA 的**额外操作数**参与，硬件自动融合反量化。

#### 4.3.3 源码精读

算子类标签（告诉 builder 走块缩放路径）：

[include/cutlass/arch/mma.h:134](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/arch/mma.h#L134) —— `struct OpClassBlockScaledTensorOp {};` 一个空标签，专用于块缩放 Tensor Core。

主循环策略（policy 标签，驱动 collective 偏特化）：

[include/cutlass/gemm/dispatch_policy.hpp:1075-1082](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/dispatch_policy.hpp#L1075-L1082) —— `MainloopSm100TmaUmmaWarpSpecializedBlockScaled`，内嵌 `Schedule = KernelTmaWarpSpecializedBlockScaledSm100<...>`（即 u2-l7 讲过的「一个标签一个内核」分派机制）。

Schedule 标签家族（builder 按此选 NVFP4 / MXFP4 / MXFP8 特化）：

[include/cutlass/gemm/dispatch_policy.hpp:820-831](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/dispatch_policy.hpp#L820-L831) —— `KernelScheduleBlockScaledGemmSm100` 基类，派生 `KernelScheduleMxNvf4Sm100`（MXFP4/NVFP4）与 `KernelScheduleMxf8f6f4Sm100`（MXFP8/6/4），各有 1Sm/2Sm 版本。

collective 主模板的模板参数（注意 `ElementPairA/B`、`GmemTiledCopyPairA/B`、`SmemLayoutAtomPairA/B` 都是「**Pair**」，即数据与缩放因子成对）：

[include/cutlass/gemm/collective/sm100_blockscaled_mma_warpspecialized.hpp:61-102](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_blockscaled_mma_warpspecialized.hpp#L61-L102) —— 偏特化签名：policy 为 `MainloopSm100TmaUmmaWarpSpecializedBlockScaled`，`ElementPairA_` 实际是 `cute::tuple<ElementA, ElementSFA>`。

从 Pair 里拆出数据与缩放因子类型、并取出 `SFVecSize`：

[include/cutlass/gemm/collective/sm100_blockscaled_mma_warpspecialized.hpp:121-178](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_blockscaled_mma_warpspecialized.hpp#L121-L178) —— `SFVecSize = TiledMma::SFVecSize`（来自指令 traits）；`ElementSF`、`LayoutSFA`、`LayoutSFB` 都从 Pair 的第二个元素取出；并 `static_assert` 要求 SFA 与 SFB 数据类型一致。

MMA 指令 traits 里的 `SFVecSize` 与 TMEM 缩放因子片段类型：

[include/cute/atom/mma_traits_sm100.hpp:3416-3428](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/mma_traits_sm100.hpp#L3416-L3428) —— `ValTypeSFA`/`ValTypeSFB`、`SFVecSize = 32`（此为 MXF8F6F4 指令的值）、`FrgTypeSFA/SFB = UMMA::tmem_sf_frg<sf_type, SFVecSize, ...>`，即缩放因子片段**落在 TMEM**。

example 72a 用 `CollectiveBuilder` 装配整个块缩放 GEMM（与 u2-l9 的 Hopper 装配步骤同构，仅 ArchTag/OperatorClass/数据类型不同）：

[examples/72_blackwell_narrow_precision_gemm/72a_blackwell_nvfp4_bf16_gemm.cu:114-147](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/72_blackwell_narrow_precision_gemm/72a_blackwell_nvfp4_bf16_gemm.cu#L114-L147) —— `ArchTag=Sm100`、`OperatorClass=OpClassBlockScaledTensorOp`、`MmaTileShape=<256,256,256>`、`ClusterShape=<2,4,1>`，先后构造 epilogue 与 mainloop 的 `CollectiveBuilder`，再 `GemmUniversal` + `GemmUniversalAdapter`。`StageCountAutoCarveout` 用 epilogue 的 `SharedStorage` 大小扣留出主循环缓冲级数（与 u2-l9 完全一致）。

#### 4.3.4 代码实践

**实践目标**：对比 example 72 的三个子例，看「换数据类型」改了哪几行。

**操作步骤**：

1. 72a（L96-L119）：A/B 都是 `nv_float4_t<float_e2m1_t>`，输出 BF16，`AlignmentA=32`（32 个 4 比特元素 = 16 字节）。
2. 72c（L97-L104）：A 是 `mx_float8_t<float_e4m3_t>`（`AlignmentA=16`，16 个 8 比特 = 16 字节），B 是 `mx_float4_t<float_e2m1_t>`（`AlignmentB=128`）。
3. 在三份文件里都确认 `OperatorClass = OpClassBlockScaledTensorOp` 与 `ArchTag = Sm100` 不变。

**需要观察的现象**：尽管数据类型、对齐、tile/cluster 不同，三例的 `CollectiveBuilder`/`GemmKernel`/`Gemm` 装配骨架几乎逐行相同——这正是「策略标签 + builder 自动推断」的回报。

**预期结果**：你能说出「换 NVFP4↔MXFP8 只需改 Element 类型与 Alignment，builder 自动选到对应的 1Sm/2Sm 块缩放特化」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 72a 的 `AlignmentA = 32` 而 72c 的 `AlignmentA = 16`？

> **答**：`AlignmentA` 以「元素数」为单位、目标是 16 字节对齐。NVFP4 每元素 4 比特，需 32 个元素才凑 16 字节；MXFP8 每元素 8 比特，16 个元素即 16 字节。

**练习 2**：`OpClassBlockScaledTensorOp` 与普通 `OpClassTensorOp` 在 builder 里起什么作用？

> **答**：它是算子类标签，builder 用它在编译期分派到块缩放专用的 collective（多搬 SFA/SFB、发 blockscaled 指令），而不是普通 GEMM 的 collective。

---

### 4.4 缩放因子加载与硬件反量化（Scale Factor Loading & Dequantization）

#### 4.4.1 概念说明

这是本讲最关键、也最容易被误解的模块。先给结论：**块缩放 GEMM 里没有「软件反量化」这一步**。

回顾 2.2 节的数学：真实值 \( \hat{x}_i = x_i \times s \)。在老的混合精度流程里，通常会先用 `NumericConverter` 把 FP4「解包」成 BF16/FP32（反量化），再做 BF16/BF16 的 GEMM。而 Blackwell 的 `tcgen05.mma.blockscaled` 直接吃「FP4 数据 + 缩放因子」，在 Tensor Core 内部把「乘缩放 + 乘加」一次做完。CUTLASS 的任务因此被简化成两件事：

1. **把缩放因子搬到 MMA 能读到的地方**：gmem → smem（TMA）→ TMEM（UTCCP 指令，Unified Copy to TMEM）。
2. **发 MMA 时把缩放因子作为操作数传进去**：`cute::gemm(tiled_mma.with(accumulate, tCtSFA, tCtSFB), A, B, D)`。

所以本模块的「反量化」其实是**硬件在指令内部完成的**，软件只管搬运与对齐。

#### 4.4.2 核心流程

缩放因子从产生到消费的完整链路：

```
[主机端]  确定 SFA/SFB 的 Layout
          LayoutSFA = Sm1xxBlkScaledConfig::tile_atom_to_shape_SFA(M,N,K,L)
          分配 size(filter_zeros(LayoutSFA)) 个缩放因子

[主机端]  make_tma_atom 构造 tma_load_sfa / tma_load_sfb 描述符
          (描述符里编码了交织布局)

[设备端 Producer] TMA: gmem SFA/SFB -> smem SFA/SFB  (与 A/B 同 stage)
[设备端 Consumer] UTCCP: smem SFA/SFB -> TMEM SFA/SFB
[设备端 Consumer] tcgen05.mma.blockscaled(A_smem, B_smem, TMEM_SFA, TMEM_SFB) -> TMEM D
```

**布局对齐**的精髓：缩放因子张量不是「连续 M×K/SFVecSize」的简单排布，而是一种**交织（interleaved）布局**——按 128 行/列为一块（`Blk_MN=128`）、每块 4 个缩放因子（`Blk_SF=4`）排布，专门匹配 `tcgen05.mma.blockscaled` 指令读取 TMEM 时的物理排布。这个交织布局由 `Sm1xxBlkScaledConfig` 唯一描述，主机端必须按它分配与填充，否则 `can_implement` 会失败。

#### 4.4.3 源码精读

**① 缩放因子布局说明书**（本模块的核心）。`Sm1xxBlockScaledBasicChunk` 定义缩放因子的「原子块」：

[include/cutlass/detail/sm100_blockscaled_layout.hpp:48-59](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/detail/sm100_blockscaled_layout.hpp#L48-L59) —— `Blk_MN = 128`（每 128 行/列一块）、`Blk_SF = 4`（每块 4 个缩放因子）；`SfKMajorAtom` 的 Shape 为 `((32,4),(SFVecSize,4))`、Stride 为 `((16,4),(0,1))`——这正是交织布局，Stride 里的 `_0` 表示该维「不消耗存储」，会被 `filter_zeros` 过滤掉。

`Sm1xxBlockScaledConfig` 提供「把原子块铺到完整 (M,K) 或 (N,K) 上」的函数：

[include/cutlass/detail/sm100_blockscaled_layout.hpp:61-114](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/detail/sm100_blockscaled_layout.hpp#L61-L114) —— `tile_atom_to_shape_SFA(problem)` 把 `SfAtom` 铺到 `(M,K,L)`；`tile_atom_to_shape_SFB(problem)` 铺到 `(N,K,L)`。即 **SFA 沿 M、K 维铺，SFB 沿 N、K 维铺**，二者共享同一套原子布局。

**② 主机端按布局分配缩放因子**（example 72a 的 `initialize`）：

[examples/72_blackwell_narrow_precision_gemm/72a_blackwell_nvfp4_bf16_gemm.cu:333-354](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/72_blackwell_narrow_precision_gemm/72a_blackwell_nvfp4_bf16_gemm.cu#L333-L354) —— 用 `Sm1xxBlkScaledConfig::tile_atom_to_shape_SFA/SFB` 算出 `layout_SFA`/`layout_SFB`；再 `block_SFA.reset(size(filter_zeros(layout_SFA)))` 分配（`filter_zeros` 去掉零步长维得到实际元素数）。注意缩放因子用的是 `Layout` 而非 `Stride`（注释说明「Scale Factor tensors have an interleaved layout」）。

**③ 主机端构造缩放因子 TMA 描述符**：

[include/cutlass/gemm/collective/sm100_blockscaled_mma_warpspecialized.hpp:543-573](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_blockscaled_mma_warpspecialized.hpp#L543-L573) —— `make_tma_atom_A_sm100` / `make_tma_atom_B_sm100` 分别为 SFA、SFB 构造 TMA 描述符（含一个 fallback 版本，用于非标准 cluster）。

**④ Producer 用 TMA 搬四份数据**（A、B、SFA、SFB 共用同一 stage 的屏障）：

[include/cutlass/gemm/collective/sm100_blockscaled_mma_warpspecialized.hpp:914-919](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_blockscaled_mma_warpspecialized.hpp#L914-L919) —— 四条 `copy(...->with(*tma_barrier, mcast_mask), ...)`，分别发 A、B、SFA、SFB 的 TMA 搬运；缩放因子与数据共用同一个满门屏障（注释：不为 sf_pipeline 单独同步，统一用 mainloop pipeline）。

**⑤ Consumer 把缩放因子从 smem 搬进 TMEM（UTCCP），再发 blockscaled MMA**：

[include/cutlass/gemm/collective/sm100_blockscaled_mma_warpspecialized.hpp:1064-1080](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_blockscaled_mma_warpspecialized.hpp#L1064-L1080) —— 先 `copy(tiled_copy_s2t_SFA/SFB, ...)`（SM100_UTCCP，smem→TMEM），再 `cute::gemm(tiled_mma.with(accumulate_, tCtSFA(_,_,k_block), tCtSFB_mma(_,_,k_block)), tCrA, tCrB, accumulators)`——这就是 `tcgen05.mma.blockscaled`，**缩放因子作为 `.with(...)` 的额外参数传入，硬件自动反量化与乘加**。

**⑥ 共享内存 / TMEM 里为缩放因子留位置**：

[include/cutlass/gemm/collective/sm100_blockscaled_mma_warpspecialized.hpp:274-304](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm100_blockscaled_mma_warpspecialized.hpp#L274-L304) —— `SharedStorage` 含 `smem_SFA`/`smem_SFB` 引擎；`TmemStorage` 含 `tCtSFA`/`tCtSFB`（TMEM 里的缩放因子片段）；`SFTransactionBytes` 统计缩放因子占用的 TMA 字节数，用于屏障 `expect_tx` 计数。

**⑦ 软件侧的缩放因子数值转换**（用于主机参考实现与无法走硬件的路径）：

[include/cutlass/numeric_conversion.h:2246-2310](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/numeric_conversion.h#L2246-L2310) —— `Array<float,N> <= Array<float_ue8m0_t,N>` 等转换器：把 E8M0 缩放因子转成浮点（用 `cvt.rn.bf16x2.ue8m0x2` PTX），证明「软件也能反量化」，只是块缩放 GEMM 主路径把它交给了硬件。

#### 4.4.4 代码实践（本讲的主实践）

**实践目标**：说明 NVFP4 输入时，缩放因子张量 SFA/SFB 的形状如何与 GEMM 分块对齐。

**操作步骤**：

1. 打开 example 72a 第 333–346 行的 `initialize`，确认 `layout_SFA = Sm1xxBlkScaledConfig::tile_atom_to_shape_SFA(make_shape(M,N,K,1))`，即 SFA 是按 `(M,K)`（外加 batch L）铺开的。
2. 打开 `sm100_blockscaled_layout.hpp` 第 48–59 行，读出原子块参数：`Blk_MN=128`、`Blk_SF=4`，原子 Shape `((32,4),(SFVecSize,4))`。
3. 推导 SFA 的总缩放因子数：一个原子块覆盖 \( 128 \) 个 M 行与 \( \text{SFVecSize}\times 4 \) 个 K 列，恰好对应 \( 128\times 4 = 512 \) 个缩放因子（即每 128 行 4 个、每 SFVecSize 个 K 元素 1 个），于是整张 SFA 的缩放因子数 \( = \dfrac{M\cdot K}{\text{SFVecSize}} \)。这与 4.2.5 练习 2 一致。
4. 在 example 72a 第 353–354 行确认分配用的是 `size(filter_zeros(layout_SFA))`——`filter_zeros` 把 Stride 为 0 的那维（即 `SFVecSize` 那维）去掉，得到的就是真实存储元素数。
5. 在第 610–618 行（`can_implement`）确认：主机提供的 `args.layout_SFA` 必须**等于** `Sm1xxBlkScaledConfig::tile_atom_to_shape_SFA` 推导出的引用布局，否则不支持。这说明「交织布局对齐」是硬约束。

**需要观察的现象**：缩放因子张量在逻辑上是 `(M, K/SFVecSize)` 量级（SFB 是 `(N, K/SFVecSize)`），但物理上是 128-行/列交织排布；MMA tile 的 K 维（如 72a 的 `MmaTileShape<256,256,256>`）必须是 `SFVecSize` 的整数倍，才能让缩放因子边界与 tile 边界对齐。

**预期结果**：你能写出这样的结论——

> 对 NVFP4（`SFVecSize=16`）的 \( M\times K \) 矩阵 A，其缩放因子张量 SFA 共有 \( M K / 16 \) 个 UE4M3 缩放因子，按「每 128 行一块、每块 4 个、沿 K 每 16 个元素一个」的方式交织排布；该布局由 `Sm1xxBlkScaledConfig::tile_atom_to_shape_SFA` 唯一决定，主机必须照此分配并填充，TMA 描述符据此构造，从而保证 consumer 发 `tcgen05.mma.blockscaled` 时每个 tile 都能取到对齐的缩放因子。

**待本地验证**：在 72a 里临时把 `--k` 改成非 `SFVecSize` 整数倍的值（如 17），观察 `can_implement` 是否报错，以验证「K 必须被 SFVecSize 整除」这一隐含约束。

#### 4.4.5 小练习与答案

**练习 1**：为什么块缩放 GEMM 不需要软件反量化？反量化发生在哪里？

> **答**：因为 `tcgen05.mma.blockscaled` 指令把「乘缩放因子 + 乘加累加」融合在 Tensor Core 内部完成；软件只需把数据 A/B 与缩放因子 SFA/SFB 搬到 smem/TMEM 并作为操作数传入。反量化发生在硬件指令内部，软件无额外开销。

**练习 2**：SFA 和 SFB 分别沿 GEMM 的哪些维铺开？为什么？

> **答**：SFA 沿 (M, K) 铺（A 是 M×K），SFB 沿 (N, K) 铺（B 是 K×N，列主序下即 N×K）。二者都把 K 维按 `SFVecSize` 切块、把 M/N 维按 128 切块并 4 路交织，匹配指令的 TMEM 读取排布。

**练习 3**：缩放因子在共享内存（smem）和张量内存（TMEM）里各存一份，为什么需要 TMEM 这一份？

> **答**：`tcgen05.mma.blockscaled` 的缩放因子操作数必须落在 TMEM（见 `FrgTypeSFA = tmem_sf_frg`）。TMA 只能搬到 smem，所以 consumer 先用 UTCCP（`copy(tiled_copy_s2t_SFA, ...)`）把 smem 的缩放因子搬进 TMEM，再发 MMA。

---

## 5. 综合实践

把四个模块串起来，做一个「换数据类型 + 验证缩放因子对齐」的小任务。

**任务**：以 example 72a 为模板，把输入从 NVFP4×NVFP4 改成「A 用 MXFP8、B 用 MXFP4、输出仍为 BF16」（即逼近 72c），并在源码层面验证缩放因子的对齐关系。

**步骤**：

1. **改类型与对齐**（参考 72c 第 97–104 行）：把 `ElementA` 改为 `cutlass::mx_float8_t<cutlass::float_e4m3_t>`、`AlignmentA=16`；`ElementB` 改为 `cutlass::mx_float4_t<cutlass::float_e2m1_t>`、`AlignmentB=128`。`OperatorClass`、`ArchTag`、`MmaTileShape`、`ClusterShape` 暂不动。
2. **观察缩放因子类型自动切换**：由于 `block_SFA` 用 `ElementA::ScaleFactorType`，改完类型后 SFA 自动变成 `float_ue8m0_t`（MX 的 E8M0），SFB 同理。确认 `initialize_block` 里 `float_ue8m0_t` 分支（72a 第 311–314 行）会把缩放因子初始化到 `[1,4]` 范围。
3. **推导新的 SF 张量大小**：MXFP8 的 `SFVecSize=32`，所以 SFA 元素数 \( = M K / 32 \)；MXFP4 的 `SFVecSize` 亦为 32。与 NVFP4（`SFVecSize=16`）相比，缩放因子张量小了一半。
4. **核对布局不变**：`Sm1xxBlkScaledConfig::tile_atom_to_shape_SFA/SFB` 的交织结构（`Blk_MN=128`、`Blk_SF=4`）与数据类型无关，只随 `SFVecSize` 变化——这正是它能用同一份 collective 服务 NVFP4/MXFP4/MXFP8 的原因。
5. **构建运行**（需 Blackwell SM100a、CUDA 12.8+）：
   ```bash
   cmake .. -DCUTLASS_NVCC_ARCHS=100a
   make 72a_blackwell_nvfp4_bf16_gemm -j
   # 改名/改源后同理构建你的可执行文件
   ./72a_blackwell_nvfp4_bf16_gemm --m=2048 --n=2048 --k=2048
   ```
6. **验证**：example 自带主机参考实现 `GettBlockScalingMainloopParams` + `Gemm3x`（72a 第 400–419 行），它会用同一套 SFA/SFB 做软件块缩放参考 GEMM，输出 `Passed` 即正确。

**预期结论**：你应能解释——「换数据类型只动了 `ElementA/B` 与 `Alignment`，`CollectiveBuilder` 自动选到 MX 的块缩放特化；缩放因子张量大小随 `SFVecSize` 变化，但交织布局结构不变；最终由 `tcgen05.mma.blockscaled` 在硬件内完成反量化与乘加。」

> 若无 Blackwell 硬件，步骤 1–4 仍可完成（纯源码阅读与推导），步骤 5–6 标注「待本地验证」。

## 6. 本讲小结

- **FP4/FP8 表示**：`float_e2m1_t`（4 比特）、`float_e4m3_t`/`float_e5m2_t`（8 比特）都是 `float_exmy_base` 家族的子字节浮点；FP4 粒度极粗，必须配缩放因子。
- **两类缩放因子**：MX 家族用 E8M0（`float_ue8m0_t`，纯 2 的幂），NVFP4 用 UE4M3（`float_ue4m3_t`，带尾数、更细）；包装类型 `mx_float4_t`/`mx_float8_t`/`nv_float4_t` 用 `DataType`+`ScaleFactorType` 绑定数据与缩放因子。
- **SFVecSize**：一个缩放因子覆盖的数据元素数（NVFP4=16，MXFP4/MXFP8=32），决定缩放因子张量比数据张量小多少倍。
- **缩放因子布局**：由 `Sm1xxBlkScaledConfig` 唯一描述，按 128 行/列一块（`Blk_MN`）、每块 4 个（`Blk_SF`）交织排布；`tile_atom_to_shape_SFA/SFB` 把它铺到 (M,K)/(N,K)，主机必须照此分配。
- **块缩放 collective**：`OpClassBlockScaledTensorOp` + `MainloopSm100TmaUmmaWarpSpecializedBlockScaled`，沿袭 Hopper warp specialization，但 producer 用 TMA 多搬 SFA/SFB、consumer 用 UTCCP 把它们送进 TMEM。
- **无软件反量化**：`tcgen05.mma.blockscaled` 直接吃「数据 + 缩放因子」，在 Tensor Core 内部融合「乘缩放 + 乘加累加」，累加器落 TMEM；软件只负责搬运与对齐。

## 7. 下一步学习建议

- **u3-l7（Blackwell SM100 集体 GEMM）**：本讲的 collective 与普通 SM100 GEMM 的 collective 高度同构，学完本讲再去读 `sm100_mma_warpspecialized.hpp` 与 UMMA/TMEM 的通用机制，会非常顺畅——重点对比「块缩放版多了 SFA/SFB 的搬运与 TMEM 缓存」。
- **深入指令层**：阅读 `include/cute/atom/mma_traits_sm100.hpp` 中 `SM100_MMA_MXF8F6F4_SS` 与 `InstrDescriptorBlockScaled`，理解 `tcgen05.mma.blockscaled` 的指令描述符如何编码数据/缩放因子格式。
- **输出端缩放（72b）**：本讲只讲了输入缩放 SFA/SFB；example 72b 还演示了**输出缩放因子 SFD**（`LinCombBlockScaleFactor`），把累加结果再量化回 NVFP4 输出，建议作为进阶练习。
- **稀疏块缩放**：`include/cutlass/gemm/collective/sm100_blockscaled_sparse_mma_warpspecialized.hpp` 把 2:4 结构化稀疏与块缩放结合，可作为更高级的扩展阅读。
