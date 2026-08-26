# A5 平台与 MX 低精度 matmul

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 A5（Ascend 950）后端与 a2a3 后端在目录组织、指令覆盖和硬件约束上的差异，并会用 `include/README.md` 状态表查询任意指令的平台可用性。
2. 理解 MX（microscaling）低精度数据格式：fp4 两个元素打包一个字节、每 32 个元素共享一个 `float8_e8m0_t` scale、scale tile 如何通过 `GetScaleAddr` 绑定到 L0 数据 tile。
3. 走读 `TMATMUL_MX` 指令的公共签名、A5 实现中的编译期检查，以及 `matmul_mxfp4_performance` 性能内核的「TLOAD→TEXTRACT→TMATMUL_MX→TSTORE」四级流水。
4. 理解 TPUSH/TPOP 的 `TileSplitAxis` 切分语义，以及本版本「no-split 场景固定派发到逻辑 sub-block 0」修复的原因与验证方法。
5. 掌握把一个 a2a3 内核适配到 A5 时的检查清单（tile 粒度、布局、scale 路径、状态表）。

## 2. 前置知识

- **Cube/Vector 混合核**：A5 的一个 AI Core 内同时有 AIC（Cube 矩阵核，跑 TMATMUL）和 AIV（Vector 向量核，跑向量指令）。`__DAV_CUBE__`/`__DAV_VEC__` 两个宏让同一份内核源码在两类核上各编译出一份代码（回忆 u4-l1 的条件装配思想）。
- **MX 格式**：microscaling 是一种分块量化格式——把数据切成固定大小的块，每块存一个 8 位指数 scale（e8m0），块内元素用极窄位宽（fp4/fp8）表示。PTO 中一个 scale 负责 32 个数据元素。
- **fp4 打包类型**：`float4_e2m1x2_t` 表示「一个字节里打包 2 个 4 位浮点数」，所以 m×k 的 fp4 矩阵在 GM 里只占 m×k/2 字节；`float8_e8m0_t` 是纯指数的 8 位 scale 类型。
- **sub-block**：A5 上一个 Cube 核可对应两个 Vector 子块（AIV0/AIV1），`get_subblockid()` 在运行期返回当前 AIV 的编号（0 或 1）。这是第 4.4 节修复的主角。
- **片上缓冲层级**：GM →（TLOAD/MTE2）→ L1 →（TEXTRACT/MTE1）→ L0A/L0B →（TMATMUL/M）→ L0C →（TSTORE/FIX）→ GM。本讲的 MX 内核完整走完这条链（u5-l3 已在 a2a3 上走过一遍）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/README.md](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/README.md) | 指令×后端（CPU/Costmodel/A2/A3/A5/Kirin）状态表 |
| [include/pto/npu/a5/README.md](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/README.md) | A5 实现目录说明：按指令（族）一文件组织 |
| [kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp) | A5 MXFP4 高性能 GEMM 内核（本讲主教材） |
| [kernels/manual/a5/matmul_mxfp4_performance/main.cpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/main.cpp) | host 侧：aclrt 资源管理与 golden 比对 |
| [docs/isa/TMATMUL_MX.md](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/TMATMUL_MX.md) | TMATMUL_MX 指令文档（签名/约束/示例） |
| [include/pto/npu/a5/TMatmul.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TMatmul.hpp) | A5 TMATMUL 家族实现，含 `CheckMadMxValid` |
| [include/pto/npu/a5/utils.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/utils.hpp) | `GetScaleAddr`：scale tile 地址换算 |
| [include/pto/npu/a5/TPush.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TPush.hpp) | A5 TPUSH 实现（TPipe/TMPipe、Producer/Consumer） |
| [include/pto/npu/a5/TPop.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TPop.hpp) | A5 TPOP 实现 |
| [include/pto/common/fifo.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/fifo.hpp) | `TileSplitAxis`/`Direction` 枚举与 RingFIFO |
| [tests/npu/a5/src/st/testcase/tpushpop_subblock_dispatch/tpushpop_subblock_dispatch_kernel.cpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/tpushpop_subblock_dispatch/tpushpop_subblock_dispatch_kernel.cpp) | 本版本新增的 sub-block 派发 ST 用例（7 个 case） |
| [kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp) | a2a3 对照组：half 精度 GEMM（u5-l3 主教材） |

## 4. 核心概念与源码讲解

### 4.1 A5 后端差异

#### 4.1.1 概念说明

PTO 的口号是「一份 Tile 抽象、多套实现」（u1-l5）。A2/A3 共用 `include/pto/npu/a2a3/`，A5（Ascend 950）则独立使用 `include/pto/npu/a5/`。差异不止是「另一套同名文件」：

1. **组织方式**：A5 目录按指令（族）一文件组织（`TAdd.hpp`、`TMatmul.hpp`、`TLoad.hpp`…），并带有 A5 专属的机制文件，如 `TPush.hpp`/`TPop.hpp`（Cube-Vector 核间 FIFO）、`utils.hpp`（MX scale 地址换算）。
2. **硬件模型**：A5 是 Cube+Vector 混合核（`__DAV_CUBE__`/`__DAV_VEC__` 双份代码生成），A2/A3 上 Cube 与 Vector 是独立核型。这带来了 TINSERT、TPUSH/TPOP 这类 A5 专属的「向 L1 插数据」「核间 FIFO」指令。
3. **指令覆盖不是超集也不是子集**：状态表里 A5 有而 A2/A3 没有的（TGEMV_MX、TPARTARGMAX、TINSERT…），也有 A2/A3 有而 A5 暂缺的行——移植前必须逐行查表。
4. **数据类型能力**：本版本（be5ccb76）为 A5 新增了 int64/uint64 位运算支持（TAND/TOR/TXOR/TNOT 及标量变体、TABS 的 int64 放行），用「一对 32 位寄存器 + 交织装载/分半存储」仿真，详见 u4-l7；A2/A3 的类型白名单没有 int64。

#### 4.1.2 核心流程

把一个内核从 a2a3 迁到 A5 的判断流程：

```text
查 include/README.md 状态表
   ├── 用到的每条指令在 A5 列都是 Yes？
   ├── dtype 在 A5 的 Check 白名单里？（各后端白名单不同，见 u2-l1/u4-l3）
   ├── tile 粒度/布局满足 A5 约束？（如 TMATMUL_MX 要求 K 为 64 的倍数）
   └── 需要 CV 核间流水 / MX 低精度？ → 用 A5 专属指令（TPUSH/TPOP、*_MX）
```

#### 4.1.3 源码精读

A5 目录的自述文件说明了组织方式——每条指令（族）一个头文件，A5 专属模式放在各自指令头里：

- [include/pto/npu/a5/README.md:5-13](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/README.md#L5-L13) 说明 A5 实现按指令（族）组织，并指向 `docs/isa/` 与 `tests/npu/a5/src/st/` 两个配套位置。

状态表是平台适配的第一入口。以本讲的两位主角为例：

- [include/README.md:96](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/README.md#L96) `TMATMUL_MX` 一行：CPU=Yes、Costmodel=TODO、A2/A3=TODO、A5=Yes、Kirin=Yes。也就是说 MX matmul 目前只在 A5/Kirin 与 CPU 模拟器上可用，A2/A3 尚未接入。
- [include/README.md:82](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/README.md#L82) `TGEMV_MX` 一行：CPU=TODO、A2/A3=TODO、A5=Yes——A5 独有的 MX 向量乘，CPU 模拟器都还没实现。

对照组：a2a3 的 GEMM 内核入口与调参（half 输入、baseM=128/baseK=64/baseN=256）：

- [kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp:237-270](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L237-L270) 是 `GemmPerformance` 内核入口与 `LaunchGEMME2E` 的参数区：`blockDim=24, m=n=k=6144, singleCoreM=1536, singleCoreN=1024, baseM=128, baseN=256, baseK=64, stepKa=stepKb=4`，数据类型为 `float, half, half, float`。这些数字在 4.3 节与 A5 版本逐项对比。

#### 4.1.4 代码实践

1. **实践目标**：建立「先查表、再写码」的习惯，并亲眼确认 A5 与 CPU/A2A3 的覆盖差集。
2. **操作步骤**：打开 `include/README.md` 状态表，筛选出「A5=Yes 且 CPU≠Yes」的行（如 `TGEMV_MX`、`TPARTARGMAX`、`TPARTARGMIN`、`TRANDOM`、`TQUANT`、`TConcat`）；再筛选「A2/A3=Yes 且 A5=TODO/No」的行。
3. **需要观察的现象**：两个方向的差集都非空——平台覆盖是交错而非包含关系。
4. **预期结果**：能列出至少 3 条 A5 独有指令，并说出各自文档路径（表内每条指令都链接到 `docs/isa/*.md`）。完整跑法见第 5 节综合实践。

#### 4.1.5 小练习与答案

**练习 1**：`TMATMUL_MX` 在 A2/A3 列是什么状态？这意味着什么？
**答案**：TODO（[include/README.md:96](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/README.md#L96)）。表示该指令属于公开 API/文档面，但 `include/pto/npu/a2a3/` 尚未实现或未接入——MX matmul 内核不能直接移植到 A2/A3。

**练习 2**：为什么 A5 需要 `TPUSH/TPOP` 而 a2a3 不需要？
**答案**：A5 是 Cube+Vector 混合核，同一物理核内 AIC 与 AIV 要通过片上 FIFO 交换数据（L0C→UB 或 UB→L1）；a2a3 上 Cube/Vector 是独立核型，数据交互走 GM，不需要这对核间 FIFO 指令。

**练习 3**：本版本 A5 新增的 int64/uint64 位运算走的是什么实现路线？
**答案**：用一对 32 位向量寄存器（low/high 半字）仿真 64 位元素：`vlds(DINTLV_B32)` 解交织装载、位运算无进位可两半独立做、`vsts(NORM_B32)` 分半写回。详见 u4-l7。

### 4.2 MX 数据格式

#### 4.2.1 概念说明

MX（microscaling）把量化粒度从「整张矩阵」缩小到「32 个元素的小块」：

\[ C_{i,j} = \sum_{k=0}^{K-1} \left( a_{i,k} \cdot sa_{i,\lfloor k/32 \rfloor} \right) \cdot \left( b_{k,j} \cdot sb_{\lfloor k/32 \rfloor,j} \right) \]

- 数据 A/B 用 `float4_e2m1x2_t`（fp4，两元素一字节）或 fp8 存储；
- scale `sa`/`sb` 用 `float8_e8m0_t`（纯指数），每 32 个数据元素共享 1 个字节，因此 scaleK = K/32；
- 硬件在 Cube 乘法内部完成「乘 scale」的反量化，C 仍是 float 累加。

在 PTO 里这意味着**每条操作数要准备两个 tile**：数据 tile（fp4 分形布局）+ scale tile（`TileType::ScaleLeft/ScaleRight`）。scale tile 有专用别名 `TileLeftScaleCompact`/`TileRightScaleCompact`（基块 32 字节，见 [include/pto/common/pto_tile.hpp:1754-1767](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_tile.hpp#L1754-L1767)）。

#### 4.2.2 核心流程

一个 m×k 的 fp4 矩阵在各级存储中的形态：

```text
GM：  m×(k/2) 字节（>>1 打包）      scale: m×(k/32) 字节（>>5）
L1：  TileMatA [baseM, baseK×stepKa]（分形 ND/DN）  TileScaleA [baseM, baseScaleK×stepKscaleA]
L0：  TileLeftCompact [baseM, baseK]               LeftScaleTile [baseM, baseScaleK]
       └─ scale 地址 = GetScaleAddr(数据tile地址) >> 4，独立地址空间
计算： TMATMUL_MX(c, a, aScale, b, bScale) → 硬件按 32 元素块应用 scale
```

#### 4.2.3 源码精读

GM 偏移计算同时体现两种「除法」：fp4 打包除 2（`>>1`）、scale 密度除 32（`>>5`）：

- [kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp:72-76](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L72-L76) `gmOffsetA = (rowStart * k) >> 1`（fp4 两元素一字节），`gmOffsetScaleA = (rowStart * k) >> SHIFT_SCALE_FACTOR`（`SHIFT_SCALE_FACTOR=5`，即 32 元素一个 scale），`gmOffsetC` 正常按元素算（C 是 bf16）。常量 `SCALE_FACTOR=32`、`SHIFT_SCALE_FACTOR=5` 定义在 [同文件:16-20](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L16-L20)。

scale tile 不是随便找个 UB/L0 空位放的，它由数据 tile 的地址推导：

- [include/pto/npu/a5/utils.hpp:82-86](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/utils.hpp#L82-L86) `GetScaleAddr` 把 L0 数据 tile 地址右移 `SHIFT_MX_ADDR=4`（[utils.hpp:19](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/utils.hpp#L19)），得到 scale 所在地址空间的编号。
- [kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp:119-122](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L119-L122) 四个 L0 scale tile 全部用 `TASSIGN(aScaleTile[i], GetScaleAddr(aTile[i].data()))` 绑定——scale 跟着自己的数据 tile 走，乒乓切换时天然同步。

GM 侧的 MX 布局提示（A5 专属枚举）：

- [kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp:176-183](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L176-L183) 四个 GlobalTensor 的 Layout 参数分别是 `Layout::ND`（A 数据）、`Layout::DN`（B 数据）、`Layout::MX_A_ND`（A 的 scale）、`Layout::MX_B_DN`（B 的 scale）——MX 布局枚举描述的是「数据+scale 在 GM 中如何交错/伴随存放」，是 A5 路径新增的布局种类。

host 侧文件大小换算要跟上打包规则：

- [kernels/manual/a5/matmul_mxfp4_performance/main.cpp:34-41](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/main.cpp#L34-L41) `aFileSize = m*k*sizeof(U)/2`（fp4）、`aScaleFileSize = m*k/32*sizeof(X)`——分配/拷贝错了字节数，内核读到的就是错位数据。

#### 4.2.4 代码实践

1. **实践目标**：亲手算一遍 MX 格式的内存账。
2. **操作步骤**：对默认规模 `m=2040, k=8192, n=8100`（[main.cpp:100-104](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/main.cpp#L100-L104)），用纸笔算：A/B 数据文件各多少字节？两个 scale 文件各多少字节？若换成 fp8（`matmul_mxfp8_performance` 同目录兄弟示例）数据文件变为多少？
3. **需要观察的现象**：scale 文件只有数据文件的 1/16（fp4 时 1/2 字节/元素 对 1/32 字节/元素）。
4. **预期结果**：A=2040×8192/2=8,355,840 字节，scaleA=2040×8192/32=522,240 字节；fp8 时 A 翻倍为 16,711,680 字节而 scale 不变。**待本地验证**（可用 `ls -l` 对照生成的 bin 文件）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `baseScaleK = baseK / 32`，而 `stepKscaleA = stepKa * 4`（`mxScalePara=4`）？
**答案**：见 [mxmatmul_performance_kernel.cpp:350-352](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L350-L352)。数据 panel 每 `stepKa=2` 次迭代重载一次；scale 比 data 小 16 倍（fp4 时），一次可以多囤——同一个 scale panel 覆盖 4 个数据 panel 的 k 跨度，所以重载频率只有数据路的 1/4，减少 MTE2 压力。

**练习 2**：scale tile 用 `GetScaleAddr(data_addr)` 绑定而不是独立 TASSIGN 一个地址，好处是什么？
**答案**：scale 与数据在 L0 里构成「主从」关系——数据 tile 乒乓切换时（`aTile[0]/aTile[1]`），对应 scale tile（`aScaleTile[0]/aScaleTile[1]`）由同一 `data()` 推导，天然指向同一乒乓槽位，程序员不需要维护两套地址表，也不会出现数据/scale 槽位错配。

### 4.3 低精度 matmul

#### 4.3.1 概念说明

`TMATMUL_MX` 是 TMATMUL 的 MX 变体：多接两个 scale tile 操作数，硬件在矩阵乘内部完成分块反量化。公共 API 层有普通/累加（`c = c + A·B`）/bias 三种形式（[docs/isa/TMATMUL_MX.md:63-87](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/TMATMUL_MX.md#L63-L87)）。性能内核 `matmul_mxfp4_performance` 把它嵌进完整的四级流水，示范了 A5 上「非对齐规模（m=2040、n=8100 都不是 2 的幂）」的 MX GEMM 写法。

#### 4.3.2 核心流程

内核的流水心智模型（源码注释原文，[mxmatmul_performance_kernel.cpp:21-28](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L21-L28)）：

```text
每个 (i, j) 输出块、每个 kIter：
  TLOAD    (MTE2): GM→L1，装 [baseM, baseK×stepKa] 数据 panel（+ 每 4 次装 scale panel）
  TEXTRACT (MTE1): L1→L0，切出本次 baseK 块到 L0A/L0B 乒乓槽（+切 scale）
  TMATMUL_MX (M):  c = A·B 或 c += A·B（k==0 用普通形式，之后用累加形式）
  TSTORE   (FIX):  K 循环结束后 L0C→GM 写回
事件：MTE2↔MTE1（ID 0/1 分给 A/B panel）、M↔MTE1（乒乓槽复用）、M↔FIX（写回）
```

三级乒乓标志 `mte2DBFlag`（L1 数据槽）、`mte2mxDBFlag`（L1 scale 槽）、`mte1DBFlag`（L0A/L0B 槽）各自独立翻转。

#### 4.3.3 源码精读

累加模式的选择——k==0 首轮用普通形式初始化 L0C，之后全部用累加形式：

- [kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp:30-39](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L30-L39) `MatmulAcc` 按传入的 `k` 分流到 `TMATMUL_MX(cTile, aTile, aScaleTile, bTile, bScaleTile)` 或五操作数累加形式 `TMATMUL_MX(cTile, cTile, ...)`。这与 a2a3 的 TMATMUL/TMATMUL_ACC 二选一（[gemm_performance_kernel.cpp:31-33](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L31-L33)）同构，只是 MX 版把累加做成同名重载。

A5 实现层的编译期闸门：

- [include/pto/npu/a5/TMatmul.hpp:138-160](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TMatmul.hpp#L138-L160) `CheckMadMxValid` 的四条 static_assert：① A/B 必须是受支持的 FP4/FP8 组合且 C 恒为 float；② `TileLeft::Cols`（K）必须是 64 的倍数（fp4 还要求偶数）；③ A/B/C 的 fractal 布局必须严格符合 Left 列主+RowMajor 内布局 / Right 行主+ColMajor 内布局 / Acc 列主；④ 累加器字节数不得超过 L0C 容量。这些约束与 [docs/isa/TMATMUL_MX.md:89-98](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/TMATMUL_MX.md#L89-L98) 的 Constraints 一致。
- [include/pto/npu/a5/TMatmul.hpp:310-325](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TMatmul.hpp#L310-L325) `TMATMUL_MX_IMPL` 先取有效区 m/k/n 做 `CheckDynamicMmad`（运行期 [1,4095] 断言），再过 `CheckMadMxValid`，最后落到 CCE 内建 `TMatmulMx`。

九种 tile 类型一次看全（本内核的类型字典）：

- [kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp:354-374](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L354-L374) L1 层 4 个（`TileMatA/TileMatB` 用 `TileType::Mat` 动态形状，`TileScaleA/TileScaleB` 是 32 字节基块的 scale staging）+ L0 层 5 个（`TileLeftCompact/TileRightCompact` 数据、`TileLeftScaleCompact/TileRightScaleCompact` scale、`TileAccCompact<float>` 累加器，全部带 `-1,-1` 动态有效区）。

L0 乒乓布局：每槽固定 32 KiB：

- [kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp:111-117](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L111-L117) `aTile[0]=0x0`、`aTile[1]=0x0+L0_PINGPONG_BYTES`（32 KiB，[同文件:19](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L19)），bTile 同理；注释明确要求每槽足迹 ≤32 KiB 才放得进 ping/pong 位。

K 循环内的事件编排（M 与 MTE1 的乒乓握手）：

- [kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp:135-161](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L135-L161) 先 `WaitFlag<PIPE_M, PIPE_MTE1>` 等 TMATMUL 用完本槽，再 TEXTRACT 数据与 scale，数据 panel 用尽时 `SetFlag<PIPE_MTE1, PIPE_MTE2>` 归还 L1 槽，随后 `SetFlag/WaitFlag<PIPE_MTE1, PIPE_M>` 放行本次乘法、`SetFlag<PIPE_M, PIPE_MTE1>` 宣布乘完——下一轮可写另一槽。首尾补齐用 [InitSyncFlags/WaitSyncFlags:255-271](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L255-L271)（预置 4 个反向事件，收尾收净，与 u3-l3 的乒乓配平原则一致）。

写回走 FIX 流水线：

- [kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp:234-253](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L234-L253) `StoreResult` 用 `PIPE_M↔PIPE_FIX` 事件把 L0C 的 cTile 经 fixpipe 写回 GM（A5 上 L0C→GM 属 FIX 流水线，回忆 u3-l2 的指令-流水线映射）。

尾部处理与非对齐规模：

- [mxmatmul_performance_kernel.cpp:419-424](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L419-L424) `RunMxMatmulDispatch` 对最后一个 M/N 块取 `m % singleCoreM` 作为有效规模，再由 [Compute:294-300](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L294-L300) 把 `remM/remN` 传进动态有效区——2040×8100 这类非整除规模靠 Tile 的动态掩码（u2-l3）消化，无需改动分形容量。

与 a2a3 版的调参对比（同一套「TLOAD→TEXTRACT→matmul→写回」骨架在两代上的刻度差）：

| 维度 | a2a3 gemm_performance | A5 mxmatmul_performance |
| --- | --- | --- |
| 数据类型 | half×half→float | fp4(`float4_e2m1x2_t`)×fp4→float，scale `float8_e8m0_t` |
| 计算指令 | TMATMUL / TMATMUL_ACC | TMATMUL_MX（普通/累加重载） |
| baseM×baseK×baseN | 128×64×256（[a2a3:261-263](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L261-L263)） | 256×256×256（[a5:455-457](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L455-L457)） |
| L0 乒乓 | 按 baseK=64 切块 | 固定 32 KiB 槽 + scale 从 tile 地址推导 |
| 额外操作数 | 无 | 每侧多 1 个 scale tile（L1/L0 各一层） |
| 写回流水 | MTE3 | FIX（fixpipe） |
| 规模/核数 | 6144³，blockDim=24 | 2040×8192×8100，blockDim=32 |

#### 4.3.4 代码实践

1. **实践目标**：不跑硬件也能验证你对流水与事件的理解。
2. **操作步骤**：
   - 通读 `MacroMatmul`（[mxmatmul_performance_kernel.cpp:125-161](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L125-L161)），把 kIter=0..7 的每一步画成时序图：横轴是 MTE2/MTE1/M/FIX 四条流水线，纵轴是迭代号，标出每个 Set/Wait 事件的 (srcPipe, dstPipe, id)。
   - 单独追 scale 的重载节奏：`kModstepKscaleA` 为 0 的迭代才发生 scale TLOAD（[同文件:198-212](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L198-L212)），确认 stepKscaleA=8 时 scale 装载频率是数据路的 1/4。
3. **需要观察的现象**：同一对流水线之间的事件 ID 严格配对；M↔MTE1 只用 ID 0/1（乒乓两槽）。
4. **预期结果**：得到一张「四级流水×8 迭代」的事件依赖图；若删掉 `WaitFlag<PIPE_M, PIPE_MTE1>`（[同文件:137](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L137)），CPU 模拟器上结果不变（事件是空操作），但 NPU 上会出现 TMATMUL 未读完槽位就被 TEXTRACT 覆盖的竞态——这正是 u3-l1 讲过的「CPU 检不出事件链错误」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 A5 敢用 baseK=256 而 a2a3 只有 64？
**答案**：fp4 每元素仅半字节，同样 256 列的 K 块在 L0 中的字节数只有 half 的 1/4，L0A/L0B 装得下更大的 K 跨度；再加上 K 必须是 64 的倍数（`CheckMadMxValid`），256 正好复用满 32 KiB 乒乓槽。

**练习 2**：把本内核的 `T`（输出类型）从 bfloat16_t 改成 half，需要动哪些地方？
**答案**：`LaunchMxMatmul` 的 `bfloat16_t` 模板实参（[mxmatmul_performance_kernel.cpp:439-443](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L439-L443)）与 host 侧 `MxMatmul<uint16_t,...>` 的比对逻辑（uint16_t 只是 bf16 的位容器）需同步；注意 L0C 累加器恒为 float（约束①），输出类型只影响 fixpipe 出口的量化转换。改动属实验性质，**待本地验证**。

**练习 3**：`matmul_mxfp8_performance` 与本示例是什么关系？
**答案**：同目录兄弟示例（[kernels/manual/a5/](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/README.md)），骨架相同，仅把数据类型换成 fp8 组合（`(float, float8_e4m3_t, float8_e4m3_t)` 等四组，见 [docs/isa/TMATMUL_MX.md:94-96](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/TMATMUL_MX.md#L94-L96)），GM 偏移不再 `>>1`。

### 4.4 TPUSH/TPOP 派发

#### 4.4.1 概念说明

TPUSH/TPOP 是 A5 混合核内部的核间 FIFO 指令（注意与跨 NPU 的 TPUT/TGET 区分，见 u1-l1）。生产者 `allocate→push→record`，消费者 `wait→pop→free`。关键模板参数 `TileSplitAxis` 决定 FIFO 槽位如何映射到两个 AIV 子块：

- [include/pto/common/fifo.hpp:17-23](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/fifo.hpp#L17-L23) 定义了 5 种切分：`TILE_NO_SPLIT`（1:1，只用 AIV0）、`TILE_UP_DOWN`（按行上下分）、`TILE_LEFT_RIGHT`（按列左右分）及两个 ODD 变体（奇数尺寸的分法）。

subBlockId 参与两个方向的地址算术：V2C 时生产者 AIV 用它决定写入 L1 槽的行/列窗口（`pushVec2MatFiFo`），C2V_GM 时消费者 AIV 用它决定从 GM 槽读回的子区间（`popVecTileFromGMFiFo`）。

**本版本修复（commit ba725bef）**：两参重载（隐式取 ID）此前无条件调用 `get_subblockid()`。但对 `TILE_NO_SPLIT` 而言 FIFO 只有一条逻辑 lane，合法 subBlockId 只能是 0——跨核握手只置 sub-block 0 的 flag，Acc→UB 也固定走 `SingleModeVec0`。在这条路径上读硬件 sub-block ID 既无意义，又把一个运行期标量读带进 AIC/AIV 两侧的代码生成。修复用 `if constexpr` 让 no-split 固定传字面量 0，split 路径保持 `get_subblockid()`，显式三参重载不变。

#### 4.4.2 核心流程

```text
TPUSH_IMPL(pipe, tile)                     // 两参重载
  1. shouldWaitFree? → allocate<Split>()    // 稀疏同步：周期性查空间
  2. 地址计算：
       Split == TILE_NO_SPLIT → push(..., 0)          ← 修复点：字面量 0
       否则                  → push(..., get_subblockid())
  3. record<Split>()                         // 通知消费者数据就绪

跨核握手 flag 的分流（以 Producer 为例）：
  setIntraBlockBySplit(pipe, flag):
      set_intra_block(pipe, flag)                        // 恒置 sub-block 0 的 flag
      if Split != TILE_NO_SPLIT:
          set_intra_block(pipe, flag + 16)               // 再置 sub-block 1（VEC_CORE_ID_OFFSET=16）
```

#### 4.4.3 源码精读

修复后的两处 `if constexpr` 分流：

- [include/pto/npu/a5/TPush.hpp:717-739](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TPush.hpp#L717-L739) `TPUSH_IMPL` 两参重载：第 2 步地址计算处，`TILE_NO_SPLIT` 传 `0`，否则传 `get_subblockid()`；显式三参重载（[同文件:741-759](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TPush.hpp#L741-L759)）不读硬件 ID，完全由调用方指定。
- [include/pto/npu/a5/TPop.hpp:36-41](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TPop.hpp#L36-L41) `TPOP_IMPL` 两参重载对称地做了同样的分流。

「no-split 只有一条逻辑 lane」在握手代码里的物证：

- [include/pto/npu/a5/TPush.hpp:102-118](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TPush.hpp#L102-L118) `setIntraBlockBySplit`/`waitIntraBlockBySplit`：先操作 `flagId`，仅当 `Split != TILE_NO_SPLIT` 才追加 `flagId + VEC_CORE_ID_OFFSET`（=16，[同文件:45](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TPush.hpp#L45)）。no-split 时 AIV1 根本不参与握手——这正是 subBlockId 在该路径上无意义的原因。
- [include/pto/npu/a5/TPush.hpp:213-214](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TPush.hpp#L213-L214) Acc→UB 路径在 `TILE_NO_SPLIT` 下固定走 `AccToVecMode::SingleModeVec0`——数据也只发给 AIV0，与握手行为互为印证。

ST 用例如何「钉死」这份契约：

- [tests/npu/a5/src/st/testcase/tpushpop_subblock_dispatch/tpushpop_subblock_dispatch_kernel.cpp:11-43](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/tpushpop_subblock_dispatch/tpushpop_subblock_dispatch_kernel.cpp#L11-L43) 文件头注释列出了四类派发路径的测试意图；用例选了 subBlockId 真正参与地址计算的 V2C（生产者侧）与 C2V_GM（消费者侧）两个方向。
- [同文件:85-96](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/tpushpop_subblock_dispatch/tpushpop_subblock_dispatch_kernel.cpp#L85-L96) V2C 用例定义 `TPipe<FLAG_ID=0, DIR_V2C, SlotSize=K*N*sizeof(T), FIFO_DEPTH=2, LocalSlotNum=2, IsNoSplit>`——`IsNoSplit` 模板位直接来自 `SplitAxis` 的选择。
- [同文件:116-150](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/tpushpop_subblock_dispatch/tpushpop_subblock_dispatch_kernel.cpp#L116-L150) AIV 生产者分支：no-split 时 `isProducer = (subBlockIdx == 0)` 把 AIV1 挡在 FIFO 外；显式 ID 变体传 `1 - subBlockIdx`（把对端的 ID 递进去，若实现偷偷改读硬件 ID，结果立刻错位）。
- [同文件:153-194](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/tpushpop_subblock_dispatch/tpushpop_subblock_dispatch_kernel.cpp#L153-L194) Cube 消费者分支把整个槽 TPOP 回来做一次真 TMATMUL——golden 数据因此能「指认」每个 AIV 写了槽内的哪个行窗口。
- [同文件:305-340](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/tpushpop_subblock_dispatch/tpushpop_subblock_dispatch_kernel.cpp#L305-L340) tilingKey 1–7 派发出 7 个用例：no-split 隐式/显式、上下分隐式、上下分显式换 ID（V2C 4 个）+ C2V_GM 的 no-split 隐式、分片隐式、分片显式换 ID（3 个）。提交信息里的变异验证表明：把 split 分支也改成字面量 0 会让 case3/6 失败（max diff 12.38），让三参重载忽略入参会令 case4/7 失败——用例确有约束力。

#### 4.4.4 代码实践

1. **实践目标**：理解修复的语义保持性（为什么换成字面量 0 不会改变任何合法程序的行为）。
2. **操作步骤**：
   - 对照阅读 [TPush.hpp:727-731](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TPush.hpp#L727-L731) 与 [TPop.hpp:36-41](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TPop.hpp#L36-L41)，再顺着 `push`/`pop` 的 no-split 分支（如 [TPush.hpp:252-254](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TPush.hpp#L252-L254)、[TPush.hpp:297-298](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TPush.hpp#L297-L298)）确认 subBlockId 在这些分支里本来就被丢弃。
   - 阅读 `gen_data.py`（同用例目录）看 golden 如何按「哪个 AIV 写哪半」计算。
3. **需要观察的现象**：no-split 的所有 push/pop 分支地址都与 subBlockId 无关；Acc 生产者路径根本不接收该参数。
4. **预期结果**：能写出一句话结论——「修复只是把一个被丢弃的运行期读取换成了编译期常量，语义不变、代码生成更干净」。硬件验证需 A5 环境（`python3 tests/script/run_st.py -r sim -v a5 -t tpushpop_subblock_dispatch`），本机无可运行环境时标注**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 case2（no-split + 显式非零 ID）在修复前后都能通过？
**答案**：no-split 下 `pushVec2MatFiFo`/`pushVec2GMFiFo`/`popVecTileFromGMFiFo` 的地址分支都取 0，显式传入的 ID 被丢弃；case2 锁定的是「no-split 槽位地址与 sub-block ID 无关」这一前提，该前提正是修复可以用字面量 0 替换 `get_subblockid()` 的依据。

**练习 2**：`VEC_CORE_ID_OFFSET=16` 与 `FlagID+1 < 16` 的 static_assert（[TPush.hpp:46-47](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TPush.hpp#L46-L47)）有什么联系？
**答案**：AIV1 的 flag 编号 = AIV0 的编号 +16。硬件上 intra-block flag 编号须小于 32（`FlagID+3<16` 的 Both 断言同理取保守上限 16），因此 FlagID 本身必须留出 +16 的余量，否则 AIV1 的 flag 会越界。

**练习 3**：如果要新增 `TILE_LEFT_RIGHT` 的显式换 ID 用例，golden 应该怎么变？
**答案**：参照 case4 的思路，把显式 ID 换成 `1 - get_subblockid()`，左右两半列窗口互换；golden 需按「AIV0 写右半、AIV1 写左半」重算（`pushVec2MatFiFo` 的 `TILE_LEFT_RIGHT` 分支以 `ProdN * subBlockId` 为列偏移，见 [TPush.hpp:258-264](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TPush.hpp#L258-L264)）。

### 4.5 平台适配

#### 4.5.1 概念说明

「平台适配」是把前四个模块串成可执行清单：同一算法落到 A5 时，哪些东西必须换、哪些可以直接搬。核心认知是：PTO 保证**指令签名**跨代一致（common 层声明），但不保证**指令可用性**（状态表）与**约束刻度**（各后端 Check）一致。

#### 4.5.2 核心流程

a2a3 GEMM → A5 MX GEMM 的适配 diff 清单：

```text
① dtype：half → float4_e2m1x2_t（+新增 scale 操作数 float8_e8m0_t）
② 计算指令：TMATMUL/TMATMUL_ACC → TMATMUL_MX 普通/累加重载
③ tile：新增 TileScaleA/TileScaleB（L1 staging + L0 Compact 两层），
   scale 用 GetScaleAddr(数据tile.data()) 绑定
④ GM 偏移：数据 >>1（fp4 打包）、scale >>5（32 元素/字节）
⑤ 布局：GlobalTensor Layout 增加 MX_A_ND / MX_B_DN
⑥ 粒度：baseK 64→256，L0 乒乓改为固定 32 KiB 槽
⑦ 写回：MTE3 → FIX（fixpipe）
⑧ 查表：用到的每条指令在 A5 列为 Yes？dtype 在 A5 Check 白名单？
```

#### 4.5.3 源码精读

- 对照锚点一（a2a3 侧类型与布局）：[kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp:79-86](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L79-L86) 只有两个 GlobalTensor（ND/DN），没有 scale。
- 对照锚点二（A5 侧对应物）：[kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp:176-183](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L176-L183) 四个 GlobalTensor，多出的两个用 MX 布局；[同文件:354-374](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L354-L374) tile 从 5 种增至 9 种。
- 平台约束的第一现场永远是各后端 Check：A5 的 `CheckMadMxValid`（[TMatmul.hpp:138-160](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TMatmul.hpp#L138-L160)）与 a2a3 的 `CheckMadValid` 白名单不同（后者无 MX 组合）——「CPU 跑通 ≠ 全后端合法」（u4-l3 的核心结论）在 MX 路径上同样成立：CPU 列虽已标 Yes（[include/README.md:96](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/README.md#L96)），但 A2/A3 列是 TODO，直接把该内核编给 a2a3 会在装配层失败。

#### 4.5.4 代码实践

1. **实践目标**：产出一页可复用的《A2A3→A5 内核适配 checklist》。
2. **操作步骤**：以 4.5.2 的 8 条为骨架，逐条填入「源码证据（文件:行）+ 失败表现」两列。例如 ③ 的证据是 [mxmatmul_performance_kernel.cpp:119-122](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L119-L122)，失败表现是「scale 未绑 GetScaleAddr 时 TMATMUL_MX 读到错 scale、数值整体偏移一个 2 的幂」。
3. **需要观察的现象**：清单里每一条都能在两个示例内核中找到左右对照的代码位置。
4. **预期结果**：得到 8 行×3 列的表格；第 5 节的综合实践会用到它。

#### 4.5.5 小练习与答案

**练习 1**：`matmul_mxfp4_performance` 里 `TPUSH/TPOP` 一条都没用，为什么它仍是「A5 专属」示例？
**答案**：它的核心指令 TMATMUL_MX 在 A2/A3 列是 TODO（状态表 L96），且依赖 MX 布局与 GetScaleAddr 等 A5 机制；平台归属由「指令可用性 + 机制依赖」决定，而不是是否用了 CV FIFO。

**练习 2**：同一个内核想在 CPU 模拟器上跑数值验证，需要改什么？
**答案**：TMATMUL_MX 的 CPU 列已是 Yes，理论上定义 `__CPU_SIM` 即可用 CPU 模拟器验证数值（fp4/scale 语义由 CPU 模板给出）；但 host 侧 aclrt 调用与 ccec 专属语法需按 CPU ST 的骨架（u5-l1）重写 main，属实验性操作，**待本地验证**。

## 5. 综合实践

把规格里的三项任务串成一次完整的「平台对比 + 修复走读」：

**任务 A：两代 GEMM 内核差异清单。**
逐段对比 [kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp) 与 [kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp)，产出三栏对照表：模板参数（T/U/S/X 的含义与取值）、tile 布局（9 种 vs 5 种，scale 层的有无）、指令调用（TMATMUL_MX vs TMATMUL/TMATMUL_ACC；FIX vs MTE3 写回；GM 偏移的 `>>1`/`>>5`）。4.3.3 的对比表是答案骨架，你需要把每一格换成自己核实过的行号证据。

**任务 B：找出 3 条「A5 支持而 CPU 不支持」的指令并解释。**
在 [include/README.md](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/README.md) 状态表中筛选（参考答案）：

1. `TGEMV_MX`（[L82](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/README.md#L82)，CPU=TODO，A5=Yes）：MX 缩放版 GEMV——向量×矩阵乘带 scale tile，与 TMATMUL_MX 同族但面向瘦矩阵场景。
2. `TPARTARGMAX`（[L113](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/README.md#L113)，CPU=TODO，A5=Yes）：分段 argmax 规约——返回最大值所在索引而非值本身，A2/A3 亦未实现，是 A5 规约指令面的补齐项。
3. `TRANDOM`（[L126](https://github.com/hw-native-sys-pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/README.md#L126)，CPU=No，A5=Yes）：片上随机数生成，配合 common 层的 `TRandomKey/TRandomCounter` 种子类型（u2-l1）；CPU 列是显式 No（不计划模拟）而非 TODO。
   另有 `TPARTARGMIN`（L114）、`TQUANT`（L128）、`TConcat`（L63）等同类行可替换。

**任务 C：no-split 修复走读。**
阅读修复后的 [include/pto/npu/a5/TPush.hpp:717-739](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TPush.hpp#L717-L739) 与 [include/pto/npu/a5/TPop.hpp:26-50](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TPop.hpp#L26-L50)，回答：no-split 路径为何改用固定 sub-block 0？——因为 `TILE_NO_SPLIT` 的 FIFO 只有一条逻辑 lane：握手 flag 只置 sub-block 0（`setIntraBlockBySplit` 跳过 +16）、Acc→UB 固定 `SingleModeVec0`、所有 push/pop 分支的地址都不消费 subBlockId；继续读 `get_subblockid()` 只会把无意义的运行期标量读塞进代码生成。再对照 [tests/npu/a5/src/st/testcase/tpushpop_subblock_dispatch/tpushpop_subblock_dispatch_kernel.cpp:11-43](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/tpushpop_subblock_dispatch/tpushpop_subblock_dispatch_kernel.cpp#L11-L43) 的 7 个 case 说明验证意图：case1/2/5 锁定「no-split 地址与 ID 无关」（修复的语义依据），case3/4/6/7 锁定 split 与显式 ID 路径不被修复波及（变异验证中它们分别对两类错误实现失败）。有 A5 环境时运行 `python3 tests/script/run_st.py -r sim -v a5 -t tpushpop_subblock_dispatch` 应得 7/7 PASS；无环境则记录**待本地验证**。

## 6. 本讲小结

- A5（Ascend 950）是 Cube+Vector 混合核的独立后端目录（`include/pto/npu/a5/`），指令覆盖与 a2a3 交错而非包含；移植第一步永远是查 `include/README.md` 状态表。
- MX 格式 = 打包窄位宽数据（fp4 两元素一字节，`>>1`）+ 每 32 元素一个 `float8_e8m0_t` scale（`>>5`）；scale tile 用 `GetScaleAddr(数据tile地址)>>4` 绑定，随数据乒乓槽自动切换。
- `TMATMUL_MX` 在 A5 由 `CheckMadMxValid` 把守：C 恒 float、K 为 64 的倍数、fractal 布局严格、累加器不超 L0C；`matmul_mxfp4_performance` 演示了它的完整四级流水（TLOAD→TEXTRACT→TMATMUL_MX→TSTORE/FIX）与 256×256×256 粒度、32 KiB L0 乒乓、scale 1/4 频率重载等 A5 刻度。
- TPUSH/TPOP 的 `TileSplitAxis` 决定 FIFO 槽到 AIV 子块的映射；本版本修复让 no-split 场景固定派发逻辑 sub-block 0（`if constexpr` 编译期常量替换 `get_subblockid()`），语义不变、代码生成更干净，并由 7 个 ST 用例双向锁定。
- 平台适配清单八条：dtype、计算指令、scale tile、GM 偏移、布局枚举、tile 粒度、写回流水、状态表/白名单——「一份签名、多套实现」保住的是 API 形状，保不住可用性与约束刻度。
- 本版本 A5 的另两项更新——int64/uint64 位运算（寄存器对仿真，u4-l7 专讲）与 A5 ST 用例扩容（u5-l1 专讲）——与 MX 路径无耦合，可按需跳读。

## 7. 下一步学习建议

- **u6-l6（dispatch_mega_combine）**：A5 混合核 MegaMoE 融合算子，把本讲的 TPUSH/TPOP CV pipe、`*_MX` 低精度 matmul（`pto_gmm_mx_preload.hpp`）与任务邮箱调度全部推到生产级规模，是 A5 机制的综合考场。
- **u6-l1/u6-l5（通信 ISA 与 RDMA 后端）**：厘清「核间 TPUSH/TPOP」与「跨 NPU TPUT/TGET」两条线的边界后，再去读异步传输后端。
- **源码延伸阅读**：`include/pto/npu/a5/TInsert.hpp`（V2C 路径的写半边）、`include/pto/common/fifo.hpp`（RingFIFO/DataFIFO 两种 FIFO 底座）、`kernels/manual/a5/matmul_mxfp8_performance/`（fp8 版对照）。
- **动手方向**：按 u8-l2 的 checklist，为状态表中一条 A5=Yes、CPU=TODO 的指令（如 TPARTARGMAX）起草 CPU 实现，体验「多后端补齐」的完整链路。
