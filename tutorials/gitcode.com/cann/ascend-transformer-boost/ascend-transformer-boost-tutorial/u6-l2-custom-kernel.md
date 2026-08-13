# 自定义算子 Kernel 开发（AscendC 与 Tiling）

## 1. 本讲目标

本讲带你真正「下到最底层」——亲手写一个跑在昇腾 NPU AI Core 上的 Kernel。

读完本讲，你应当能够：

1. 说清一个 AscendC Kernel 的三段式流水（CopyIn → Compute → CopyOut）以及 `TQue` 双缓冲为什么能掩盖访存延迟。
2. 独立设计一份 `TilingData`，并写出 Host 侧的 Tiling 算法（计算 `blockDim`、`tilingId`、按核切分工作量）。
3. 理解 MKI 的 `KernelBase` / `OperationBase` 两个基类与 `REG_KERNEL_BASE` / `REG_OPERATION` 注册宏，并说清「注册名三处一致」这条铁律。
4. 对照 `customize_blockcopy` 真实样例，描述一个新 Kernel 从 Tiling 到 CopyIn/Compute/CopyOut 的完整实现步骤。

## 2. 前置知识

本讲是单元 6（自定义算子与插件开发）的第二篇，硬依赖 **u3-l4（Kernel 层与 MKI 框架）**。在继续之前，请确认你已掌握以下概念（这些是 u3-l4 已建立的认知，本讲不再重复）：

- **四件套**：每个算子在 Kernel 层由「AscendC kernel 计算（Device）+ tiling 切分（Host）+ MKI Operation/Kernel 注册（Host）+ CMake 构建」协作完成。
- **Host 与 Device 的分工**：Host（CPU）做 Tiling、形状推导、资源规划；Device（NPU AI Core）才真正跑 Kernel。
- **关键术语**：`TilingData`（Host 与 Device 间唯一的参数信使）、`BlockDim`（核间并行度）、`TilingKey/TilingId`（分支选择码）、`MKI`（Kernel 基础设施，提供 `KernelBase`/`OperationBase` 与注册宏）。

如果你对「为什么要分 Host/Device 两段」「Operation→Runner→KernelGraph→Kernel 这条链路怎么走」还不清楚，请先回看 u3-l4。本讲聚焦于链路最末端的 **Kernel 本身怎么写**，不再讲上层调度。

此外补充几个昇腾硬件基础概念（初学者可把它当作「CPU 程序」的类比）：

| 概念 | 类比 | 说明 |
|------|------|------|
| **GM（Global Memory）** | 内存/显存 | 全局大容量存储，所有核可见，但访问慢。Kernel 的输入输出张量（`GM_ADDR`）都放在这里。 |
| **UB（Unified Buffer）/ LocalTensor** | CPU 的 L1 缓存/寄存器 | 片上高速存储，单核私有，访问极快但容量小（910B 约 192KB）。Kernel 计算必须先把数据从 GM 搬到 UB。 |
| **AI Core（核）** | 一个 CPU 核 | 一块 NPU 有多个 AI Core，`BlockDim` 决定本次 Kernel 激活几个核并行。 |
| **流水线（Pipe）** | CPU 流水线 | 数据搬运（MTE2/MTE3）与向量计算（V）可并行，靠 `TQue` 与事件（`HardEvent`）同步。 |

## 3. 本讲源码地图

本讲以 `ops_customize/ops/customize_blockcopy`（KV Cache 块拷贝算子）为贯穿案例，涉及以下真实源码文件：

| 文件 | 作用 | 所属「件」 |
|------|------|-----------|
| [tiling/tiling_data.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/tiling/tiling_data.h) | 定义 `CustomizeBlockCopyTilingData` 结构（Host↔Device 信使） | Tiling |
| [tiling/customize_blockcopy_tiling.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/tiling/customize_blockcopy_tiling.cpp) | Host 侧 Tiling 算法（切分、设 `blockDim`/`tilingId`） | Tiling |
| [op_kernel/customize_blockcopy.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/op_kernel/customize_blockcopy.cpp) | AscendC Device Kernel（910B），三段式流水 | Kernel 计算 |
| [customize_blockcopy_kernel.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/customize_blockcopy_kernel.cpp) | `KernelBase` 子类 + `REG_KERNEL_BASE` 注册 | MKI 注册 |
| [customize_blockcopy_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/customize_blockcopy_operation.cpp) | `OperationBase` 子类 + `REG_OPERATION` 注册，按名选 Kernel | MKI 注册 |
| [CMakeLists.txt](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/CMakeLists.txt) | `add_operation` + `add_kernel`，声明算子与多芯片 Kernel | 构建 |
| [docs/starting_from_a_simple_operator.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/starting_from_a_simple_operator.md) | 官方入门文档，含最简 `addcustom` 三段式流水范例 | 教学范本 |

> 提示：本讲的 `customize_blockcopy` 与文档里的 `addcustom` 是一对好搭档——`addcustom` 是「教科书版」的最简三段式流水，`customize_blockcopy` 是「生产版」的真实复杂 Kernel。我们会先用前者建立心智模型，再用后者落地。

## 4. 核心概念与源码讲解

本讲对应三个最小模块：**AscendC Kernel（三段式流水）**、**Tiling（TilingData 与 Tiling 算法）**、**Kernel 注册（KernelBase/OperationBase 与注册宏）**。为了让你先有全局观，4.1 先用一节讲清这个算子在干什么、四件套如何对应到具体文件；4.2–4.4 再逐个深入三个模块。

### 4.1 算子功能与「四件套」全景

#### 4.1.1 概念说明：CustomizeBlockCopy 在做什么

`customize_blockcopy` 是一个 **KV Cache 块拷贝算子**，典型用于 MoE（混合专家）等需要重组 KV Cache 的场景。它的语义是：按 `srcBlockIndices`（源块下标列表）和 `dstBlockIndices`（目标块下标列表），把 K/V Cache 中的整块数据从源位置搬到目标位置，并且支持「一个源块拷到多个目标块」（由 `cumSum` 累加和描述这种一对多映射）。

它有 5 个输入、2 个输出（输出就是搬完后的 K/V Cache，in-place 风格）：

| 下标 | 张量 | 含义 | dtype |
|------|------|------|-------|
| in 0 | `kCache` | Key 缓存，shape `[blockCount, blockSize, numHead, headSizeK]` | fp16/bf16/int8 |
| in 1 | `vCache` | Value 缓存，shape 同上 | 同 K |
| in 2 | `srcBlockIndices` | 源块下标列表 | int32 |
| in 3 | `dstBlockIndices` | 目标块下标列表 | int32 |
| in 4 | `cumSum` | 每个源块累计对应的目标块数量 | int32 |
| out 0 | `kCacheOut` | 搬运后的 K | 同 K |
| out 1 | `vCacheOut` | 搬运后的 V | 同 V |

参数结构非常简单，只有一个 `type` 字段区分 K/V Cache 的排布（ND 或 NZ）：

```cpp
struct CustomizeBlockCopy {
    enum Type { BLOCK_COPY_CACHE_ND = 0, BLOCK_COPY_CACHE_NZ = 1 };
    Type type = BLOCK_COPY_CACHE_ND;
};
```
（见 [customizeblockcopy.h:15-25](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/include/customizeblockcopy.h#L15-L25)）

#### 4.1.2 核心流程：四件套如何协作

把 u3-l4 的「四件套」落到 `customize_blockcopy` 的具体文件上：

```text
运行时调用顺序（Host → Device）

 ┌─────────────────────────────────────────────────────────────┐
 │ ① Tiling（Host）                                            │
 │   customize_blockcopy_tiling.cpp                            │
 │   读输入 shape → 算 blockDim/tilingId → 填 TilingData        │
 │   把 TilingData 拷到 Device                                 │
 └─────────────────────────────────────────────────────────────┘
                          ↓ Device 侧启动 Kernel（blockDim 个核并行）
 ┌─────────────────────────────────────────────────────────────┐
 │ ② Kernel 计算（Device）                                      │
 │   op_kernel/customize_blockcopy.cpp                         │
 │   InitTilingData 读 TilingData → Init → Process             │
 │     Process 内部：CopyIn → Compute → CopyOut（三段式）       │
 └─────────────────────────────────────────────────────────────┘
                          ↑
 ┌─────────────────────────────────────────────────────────────┐
 │ ③ MKI 注册（Host）                                           │
 │   customize_blockcopy_kernel.cpp  → REG_KERNEL_BASE         │
 │   customize_blockcopy_operation.cpp → REG_OPERATION         │
 │   把「算子名/Kernel 名」登记进框架，供 Runner 按名查找        │
 └─────────────────────────────────────────────────────────────┘
                          ↑
 ┌─────────────────────────────────────────────────────────────┐
 │ ④ 构建                                                      │
 │   CMakeLists.txt → add_operation + add_kernel               │
 └─────────────────────────────────────────────────────────────┘
```

关键串接关系（u3-l4 已总结，本讲验证）：上游 Runner 用字符串 `"CustomizeBlockCopyOperation"` 找到 Operation；Operation 的 `GetBestKernel` 又用字符串 `"CustomizeBlockCopyKernel"` 找到 Kernel；`add_kernel` 再把 Kernel 类名与 Device 端 `.cpp` 绑定。**这三处名字必须完全一致**，否则运行时找不到 Kernel。

> 本节无独立代码实践，它只是全景图。下面三节分别深入 Tiling、Kernel 计算、注册。

### 4.2 Tiling：Host 侧的切分算法

#### 4.2.1 概念说明

**Tiling（切分）解决的核心矛盾**：待处理的数据量（如几百万个元素）远大于单个核的片上存储 UB（约 192KB），也多于核数。因此 Host 必须在 Kernel 启动前把数据「切块」，告诉每个核「你处理哪一段、每段多大、用哪个分支」。这些切分信息打包成 `TilingData`，是 Host 与 Device 间**唯一的参数信使**。

设计 Tiling 时要回答三个问题：

1. **核间怎么分**（`blockDim`）：激活几个核，每个核处理多少工作量。
2. **核内怎么切**（`tileNum`/每块大小）：UB 放不下全部数据，要在核内循环分块搬入搬出。
3. **走哪个分支**（`tilingId`）：同一个 Kernel 可能支持多种 dtype/排布，用 `tilingId` 当选择码，Device 端用 `TILING_KEY_IS(...)` 判断后实例化不同模板。

#### 4.2.2 核心流程

`CustomizeBlockCopyTiling` 函数的执行步骤：

1. 从 `launchParam` 读出 5 个输入张量的 shape，拿到 `blockCount`、`sourceCount`、`destinationCount`、`cumSumCount`。
2. 根据 `param.type`（ND/NZ）调用不同的子函数解析 K/V 的 `blockSize/numHead/headSizeK/headSizeV`，并做合法性校验。
3. 读取 K 的 `dtype`，算出 `typeByte`（每个元素占几个字节），用它构造 `tilingKey`。
4. 计算 `blockDim`：`actualCore = min(destinationCount, 芯片向量核数)`，再把 `destinationCount` 尽量均匀切给各核。
5. 把所有结果写进 `TilingData`，调用 `kernelInfo.SetBlockDim(...)` 与 `kernelInfo.SetTilingId(...)`。

按核切工作量的数学关系（标准的不均匀分配）：

设总目标块数 \(D\) = `destinationCount`，核数 \(N\) = `actualCore`，则

\[
\text{perCore} = \lfloor D / N \rfloor,\qquad \text{tail} = D \bmod N
\]

前 `tail` 个核各处理 `perCore + 1` 个块，其余核各处理 `perCore` 个块。这样 \(D = \text{tail}\cdot(\text{perCore}+1) + (N-\text{tail})\cdot\text{perCore}\) 恰好分完。

#### 4.2.3 源码精读

**(a) TilingData 结构——Host 与 Device 共享的信使**

```cpp
constexpr uint32_t TILING_DTYPE_IDX = 100000000;
struct CustomizeBlockCopyTilingData {
    uint32_t blockCount;
    uint32_t blockSize;
    uint32_t numHead;
    uint32_t headSizeK;
    uint32_t headSizeV;
    uint32_t sourceCount;
    uint32_t destinationCount;
    uint32_t typeByte;
    uint32_t blockDim;
    uint32_t perCoreCopyCount;
    uint32_t tailCoreCopyCount;
};
```
见 [tiling_data.h:15-29](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/tiling/tiling_data.h#L15-L29)。

> 注意：**`TilingData` 的字段顺序是 Host 与 Device 之间的隐式契约**。Host 按这个顺序写、Device 按这个顺序读（见 4.3 节 `InitTilingData` 的偏移读取），顺序错位就会读到错的数据。同时 `TILING_DTYPE_IDX = 100000000` 是 dtype 选择码的基数。

**(b) 主 Tiling 函数——切分与设码**

关键片段（按核切分、设 `tilingId`/`blockDim`）：

```cpp
uint32_t tilingKey = TILING_DTYPE_IDX * typeByte;          // dtype 选择码

uint64_t maxCore = PlatformInfo::Instance().GetCoreNum(CoreType::CORE_TYPE_VECTOR);
uint32_t actualCore = destinationCount < maxCore ? destinationCount : maxCore;

tilingDataPtr->blockDim = actualCore;
tilingDataPtr->perCoreCopyCount = destinationCount / actualCore;
tilingDataPtr->tailCoreCopyCount = destinationCount % actualCore;

kernelInfo.SetBlockDim(actualCore);     // 告诉框架激活几个核
kernelInfo.SetTilingId(tilingKey);      // 告诉 Device 走哪个分支
```
见 [customize_blockcopy_tiling.cpp:162-175](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/tiling/customize_blockcopy_tiling.cpp#L162-L175)。

解读：
- `typeByte` 由 `GetTensorElementSize(inDtype)` 得到：int8=1、fp16/bf16=2。因此 int8 的 `tilingKey = 1 亿`，fp16/bf16 的 `tilingKey = 2 亿`，正好对应 4.3 节里 Device 端 `TILING_KEY_IS(100000000)` 与 `TILING_KEY_IS(200000000)` 的两个分支。
- `actualCore` 取「目标块数」与「芯片核数」的较小值：工作量少于核数时没必要空转那么多核。

**(c) ND/NZ 双排布解析**

Tiling 还要处理两种 K/V Cache 排布。ND 是标准 `[blockCount, blockSize, numHead, headSize]`；NZ 是 310P 芯片的特殊布局 `[blockCount, head*headSize/16, blockSize, 16]`，且 `numHead` 固定为 1。两套解析逻辑分别封装在 `BlockCopyTilingNd` 与 `BlockCopyTilingNz` 中（见 [customize_blockcopy_tiling.cpp:35-91](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/tiling/customize_blockcopy_tiling.cpp#L35-L91)），由 `param.type` 分派（[L128-134](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/tiling/customize_blockcopy_tiling.cpp#L128-L134)）。这是「同一 Kernel 用 `tilingId`/参数区分多形态」思想的 Host 侧体现。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：理解 Tiling 如何把运行时 shape 翻译成「每核工作量 + 分支码」。
2. **操作步骤**：
   - 打开 [customize_blockcopy_tiling.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/tiling/customize_blockcopy_tiling.cpp)，定位 `CustomizeBlockCopyTiling` 函数。
   - 假设一次推理输入为：`destinationCount = 10`，芯片向量核数 `maxCore = 4`。手算 `actualCore`、`perCoreCopyCount`、`tailCoreCopyCount`。
   - 假设 K 是 fp16（`typeByte = 2`），手算 `tilingKey`，并预判 Device 端会走哪个 `TILING_KEY_IS` 分支。
3. **需要观察的现象**：核数大于工作量时 `actualCore` 被钳到工作量；前 `tail` 个核多干一个块。
4. **预期结果**：`actualCore = 4`、`perCoreCopyCount = 2`、`tailCoreCopyCount = 2`（核 0、1 各处理 3 块，核 2、3 各处理 2 块，合计 10）；fp16 时 `tilingKey = 200000000`，Device 走 `CustomizeBlockCopy<half>` 分支。
5. 待本地验证（无 NPU 环境无法实跑，上述为静态推算）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `TilingData` 里 `perCoreCopyCount` 与 `typeByte` 两个字段的声明顺序对调，会发生什么？
> **答案**：Host 按「新顺序」写、Device 的 `InitTilingData` 仍按「旧偏移」读，两者错位，每个核会拿到错误的工作量与分支码，导致结果错误甚至越界。这正是「字段顺序是隐式契约」的危险之处——改 `TilingData` 必须同步改 Device 端读取代码。

**练习 2**：为什么 `actualCore` 要取 `min(destinationCount, maxCore)` 而不是直接用 `maxCore`？
> **答案**：当目标块数少于核数时，多余核没有数据可处理，强行激活只会浪费资源、还可能因空核读到非法偏移。钳到 `destinationCount` 保证「至多一核一块」上限，避免空转。

### 4.3 AscendC Kernel：Device 侧的三段式流水

#### 4.3.1 概念说明

Kernel 是真正跑在 AI Core 上的函数。AscendC 用「三段式流水」作为标准写法：

```text
        ┌─────────┐   ┌─────────┐   ┌─────────┐
GM  →   │ CopyIn  │ → │ Compute │ → │ CopyOut │  → GM
        │ GM→UB   │   │ UB上算  │   │ UB→GM   │
        └─────────┘   └─────────┘   └─────────┘
```

- **CopyIn**：用 `DataCopy` 把数据从慢速 GM 搬到快速的片上 UB（`LocalTensor`）。
- **Compute**：在 UB 上做向量运算（`Add`、`Cast`、`CompareScalar` 等），这是「计算」真正发生处。
- **CopyOut**：把结果搬回 GM。

**双缓冲（Double Buffer）**是这套流水的关键：用 `TQue<..., BUFFER_NUM=2>` 申请两块 UB 缓冲。当核在第 `i` 块上 Compute 时，DMA 引擎可以并行搬运第 `i+1` 块的 CopyIn，从而**用搬运掩盖计算**，让访存延迟几乎「消失」。这就是为什么三段式要拆成 `for` 循环逐块处理。

> 教科书范例：文档里的 `addcustom` 是最纯净的三段式——`CopyIn` 搬 x、y，`Compute` 做 `Add`，`CopyOut` 搬 z，循环 `tileNum` 次。见 [starting_from_a_simple_operator.md:296-347](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/starting_from_a_simple_operator.md#L296-L347)。`customize_blockcopy` 比它复杂，但骨架完全一致。

#### 4.3.2 核心流程

`customize_blockcopy.cpp` 的 Device Kernel 经历两阶段（对应「先定位、再搬运」）：

1. **入口**：`extern "C" __global__ __aicore__ void customize_blockcopy(...)` 是 Kernel 入口（[L305-322](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/op_kernel/customize_blockcopy.cpp#L305-L322)）。它先用 `InitTilingData` 把 Device 端收到的字节流解析回 `TilingData` 结构，再用 `TILING_KEY_IS(...)` 按 dtype 选模板实例（int8 → `CustomizeBlockCopy<int8_t>`，fp16 → `CustomizeBlockCopy<half>`），最后 `Init` + `Process`。
2. **Search 阶段**（[L51-68](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/op_kernel/customize_blockcopy.cpp#L51-L68)）：每个核根据自己负责的目标块区间（`gmOffset_`），在 `cumSum` 中搜索自己对应的**源侧起始下标 `cumSumOffset_`**。这里用到了 `SearchCopyIn`（搬 cumSum）→ `SearchCompute`（用 `CompareScalar`+`Select`+`ReduceSum` 数出满足条件的个数）。注意：Compute 的「产物」是一个标量 `cumSumOffset_`，不是张量，所以这一阶段没有 CopyOut。
3. **CopyBlocks 阶段**（[L70-80](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/op_kernel/customize_blockcopy.cpp#L70-L80)）：`CopyIndicesIn` 搬入 src/dst 下标与 cumSum，`CopyBlocks` 计算出「一个源块对应几个目标块」，再 `CopyOneSrc2MultiDst` 把真实 K/V 数据经 UB 中转从源块拷到（可能多个）目标块。这里 K/V 的搬运本身就是 `CopyOneBlockIn`（GM→UB）→ `CopyOneBlockOut`（UB→GM）的 CopyIn/CopyOut 流水，只是中间没有数值计算（纯数据搬运）。

由此可见，三段式流水是「模板」而非「教条」：当计算产物是标量（Search 阶段），就只有 CopyIn+Compute；当任务是纯搬运（K/V 拷贝），就只有 CopyIn+CopyOut。**核心是「数据要在 UB 上处理、靠 `TQue` 双缓冲掩盖延迟」这一思想**。

#### 4.3.3 源码精读

**(a) Kernel 入口与分支选择**

```cpp
extern "C" __global__ __aicore__ void customize_blockcopy(
    GM_ADDR kCache, GM_ADDR vCache, GM_ADDR srcBlockIndices,
    GM_ADDR dstBlockIndices, GM_ADDR cumSum, GM_ADDR kCacheOut,
    GM_ADDR vCacheOut, GM_ADDR tiling)
{
    AtbOps::CustomizeBlockCopyTilingData tilingData;
    InitTilingData(tiling, &(tilingData));          // 字节流 → 结构体
    TPipe pipe;
    if (TILING_KEY_IS(100000000)) {                 // int8 分支
        CustomizeBlockCopy<int8_t> op;
        op.Init(kCache, vCache, srcBlockIndices, dstBlockIndices, cumSum, &tilingData, &pipe);
        op.Process();
    }
    if (TILING_KEY_IS(200000000)) {                 // fp16/bf16 分支
        CustomizeBlockCopy<half> op;
        op.Init(kCache, vCache, srcBlockIndices, dstBlockIndices, cumSum, &tilingData, &pipe);
        op.Process();
    }
}
```
见 [customize_blockcopy.cpp:305-322](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/op_kernel/customize_blockcopy.cpp#L305-L322)。

解读：
- `TILING_KEY_IS(100000000)` 与 Tiling 阶段 `SetTilingId(TILING_DTYPE_IDX * typeByte)` 一一对应（int8 时 `typeByte=1` → 1 亿；fp16 时 `typeByte=2` → 2 亿）。**Host 设的码与 Device 判的码必须同源**。
- 用 C++ 模板 `<int8_t>` / `<half>` 共用同一套 Kernel 逻辑（类 `CustomizeBlockCopy<T>`），只是元素类型不同，这是 AscendC 写多 dtype Kernel 的常见手法。
- `InitTilingData` 按字节偏移逐字段还原结构体（[L287-303](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/op_kernel/customize_blockcopy.cpp#L287-L303)），偏移量（0、4、8…）与 `TilingData` 字段顺序严格对应——再次印证 4.2 说的「隐式契约」。

**(b) Init：绑定 GM、初始化 UB 双缓冲队列**

```cpp
pipe->InitBuffer(inQueueX, BUFFER_NUM, TILE_LENGTH * INT32_SIZE);   // BUFFER_NUM=2 双缓冲
pipe->InitBuffer(inQueueY, BUFFER_NUM, TILE_LENGTH * INT32_SIZE);
...
pipe->InitBuffer(src2dstQueueK, BUFFER_NUM, OUT_UB_SIZE);           // K/V 搬运缓冲
pipe->InitBuffer(src2dstQueueV, BUFFER_NUM, OUT_UB_SIZE);
```
见 [customize_blockcopy.cpp:40-48](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/op_kernel/customize_blockcopy.cpp#L40-L48)。

`TQue`（队列）用于在流水线各阶段间传递 `LocalTensor`，`BUFFER_NUM=2` 即双缓冲；`TBuf`（普通缓冲）用于不需要跨阶段排队的临时数据（如 `maskBuf`、`onesBuf`）。

**(c) 三段式的标准写法（以 K/V 块搬运为例）**

```cpp
__aicore__ inline void CopyOneBlockIn(int32_t srcBlockIndex, int32_t progress, int32_t processLength)
{
    LocalTensor<T> src2dstLocalK = src2dstQueueK.AllocTensor<T>();   // ① 申请 UB
    DataCopyPad(src2dstLocalK, kCacheGm[srcBlockIndex * blockSizeinElement_ + progress * outUbLength_],
                copyParams, padParams);                              // ② GM → UB（CopyIn）
    src2dstQueueK.EnQue(src2dstLocalK);                             // ③ 入队，通知下游
}
__aicore__ inline void CopyOneBlockOut(int32_t dstBlockIndex, int32_t progress, int32_t processLength)
{
    LocalTensor<T> src2dstLocalK = src2dstQueueK.DeQue<T>();        // ④ 出队
    DataCopyPad(kCacheGm[dstBlockIndex * blockSizeinElement_ + progress * outUbLength_],
                src2dstLocalK, copyParams);                          // ⑤ UB → GM（CopyOut）
    src2dstQueueK.FreeTensor(src2dstLocalK);                        // ⑥ 释放 UB
}
```
见 [customize_blockcopy.cpp:225-248](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/op_kernel/customize_blockcopy.cpp#L225-L248)。

这是 AscendC 三段式的「肌肉记忆」：`AllocTensor`（申请）→ `DataCopy`（搬入）→ `EnQue`（入队）→ `DeQue`（出队）→ 计算/搬出 → `FreeTensor`（释放）。`EnQue/DeQue` 配合双缓冲，让「下一块的搬入」与「当前块的处理」自动并行。

> 注意 `DataCopyPad`（带 padding 的拷贝）：当一段长度不是 32 字节对齐时，普通 `DataCopy` 会出错，需要用 `DataCopyPad` 在尾部补齐。`customize_blockcopy` 处理的是变长索引/块，所以大量使用 `DataCopyPad`。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：看清三段式流水在真实复杂 Kernel 里的落地。
2. **操作步骤**：
   - 在 [customize_blockcopy.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/op_kernel/customize_blockcopy.cpp) 中找到 `SearchCompute`（[L127-156](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/op_kernel/customize_blockcopy.cpp#L127-L156)）。
   - 画出它的「CopyIn（在 SearchCopyIn 已搬入 cumSum）→ Compute（Cast/CompareScalar/Select/ReduceSum）→ 标量结果存 `cumSumOffset_`」结构。
   - 再对照文档里最简的 `addcustom` 三段式（[starting_from_a_simple_operator.md:306-347](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/starting_from_a_simple_operator.md#L306-L347)），列出两者在「Compute 产物」「是否有 CopyOut」上的差异。
3. **需要观察的现象**：`SearchCompute` 用了 `SetFlag/WaitFlag<HardEvent::MTE2_S>` 和 `<HardEvent::V_S>`——这些是流水线事件同步，确保「搬入完成后再读标量」「向量计算完成后再读结果」。
4. **预期结果**：`addcustom` 是「CopyIn→Compute→CopyOut」完整三段、产物是张量；`SearchCompute` 是「CopyIn→Compute」两段、产物是标量 `cumSumOffset_`，故无 CopyOut。两者都遵循「UB 上处理 + 队列同步」骨架。
5. 待本地验证（运行需 NPU 环境）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `BUFFER_NUM` 取 2 而不是 1 或很大？
> **答案**：取 1 则无缓冲，Compute 必须等 CopyIn 完成才能开始，无法掩盖访存延迟；取很大则浪费宝贵的 UB 空间（UB 总共才约 192KB）。2 是「当前块计算 + 下一块搬入」并行的最小开销选择，是工程上的甜点。

**练习 2**：如果想让这个 Kernel 同时支持 bf16（也是 `typeByte=2`），Device 端需要改吗？
> **答案**：不用改。bf16 与 fp16 的 `typeByte` 都是 2，Host 算出的 `tilingKey` 都是 2 亿，走同一个 `TILING_KEY_IS(200000000)` 分支。但 `<half>` 模板实例化的是 fp16——若要真正按 bf16 计算，需在分支内增加 bf16 的模板实例化或类型判断。这里体现了「`tilingKey` 按 typeByte 区分不够精细时，需在分支内进一步判 dtype」。

### 4.4 Kernel 注册：KernelBase 与 OperationBase

#### 4.4.1 概念说明

写完 Tiling 和 Kernel，还差最后一步——**把它们登记进 MKI 框架**，让上游 Runner 能按名字找到。MKI 提供两个基类与两个注册宏：

| 基类 | 职责 | 注册宏 | 对应文件 |
|------|------|--------|---------|
| `KernelBase` | 管单个 Kernel 的 Tiling（`InitImpl`）、能力校验（`CanSupport`）、`TilingData` 大小（`GetTilingSize`） | `REG_KERNEL_BASE` | `*_kernel.cpp` |
| `OperationBase` | 管算子的输入输出个数、形状推导（`InferShapeImpl`）、**按名选 Kernel**（`GetBestKernel`） | `REG_OPERATION` | `*_operation.cpp` |

两者关系：`OperationBase` 是「调度入口」，`GetBestKernel` 通过 `GetKernelByName("XxxKernel")` 找到 `KernelBase` 子类；`KernelBase` 在 `InitImpl` 里调用你写的 Tiling 函数完成切分。注册名是衔接点——**`REG_OPERATION` 的名字 = 上游 Runner 的 `opDesc` 字符串；`GetKernelByName` 的名字 = `REG_KERNEL_BASE` 的名字 = `add_kernel` 关联的 Kernel 类名**，三处必须一致（u3-l4 已总结，此处验证）。

#### 4.4.2 核心流程

注册一个算子 Kernel 的步骤：

1. 写 `KernelBase` 子类：重写 `CanSupport`（校验）、`GetTilingSize`（返回 `sizeof(TilingData)`）、`InitImpl`（调 Tiling 函数填 `kernelInfo_`），末尾 `REG_KERNEL_BASE(XxxKernel)`。
2. 写 `OperationBase` 子类：重写 `GetInputNum`/`GetOutputNum`、`InferShapeImpl`、`GetBestKernel`（`return GetKernelByName("XxxKernel")`），末尾 `REG_OPERATION(XxxOperation)`。
3. 在 `CMakeLists.txt` 里 `add_operation(XxxOperation "...")` 注册 Host 代码、`add_kernel(xxx ascend910b vector op_kernel/xxx.cpp XxxKernel)` 把 Device `.cpp` 与 Kernel 类名绑定。
4. 若要支持多芯片，再加一行 `add_kernel(xxx ascend310p vector op_kernel/xxx_310p.cpp XxxKernel)`（同名 Kernel、不同芯片、不同 `.cpp`）。

#### 4.4.3 源码精读

**(a) KernelBase 子类与 REG_KERNEL_BASE**

```cpp
class CustomizeBlockCopyKernel : public KernelBase {
public:
    bool CanSupport(const LaunchParam &launchParam) const override {
        MKI_CHECK(launchParam.GetInTensorCount() == TENSOR_INPUT_NUM, "in tensor num invalid", return false);
        MKI_CHECK(launchParam.GetParam().Type() == typeid(OpParam::CustomizeBlockCopy), ...);
        return true;
    }
    uint64_t GetTilingSize(const LaunchParam &launchParam) const override {
        return sizeof(CustomizeBlockCopyTilingData);          // 告诉框架要给 TilingData 分多大
    }
    Status InitImpl(const LaunchParam &launchParam) override {
        return CustomizeBlockCopyTiling(launchParam, kernelInfo_);   // 调用你写的 Tiling
    }
};
REG_KERNEL_BASE(CustomizeBlockCopyKernel);   // ← 注册名
```
见 [customize_blockcopy_kernel.cpp:28-70](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/customize_blockcopy_kernel.cpp#L28-L70)。

解读：
- `GetTilingSize` 返回 `sizeof(TilingData)`，框架据此在 Device 分配一块缓冲，Host 的 Tiling 结果会拷到那里。
- `InitImpl` 是「真正干活」的钩子，它把 `kernelInfo_`（含 `blockDim`/`tilingId`）填好——框架随后会读取这些值来启动 Kernel。
- `REG_KERNEL_BASE(CustomizeBlockCopyKernel)` 把这个类登记为全局可查的 Kernel，名字 `CustomizeBlockCopyKernel`。

**(b) OperationBase 子类与 GetBestKernel**

```cpp
Kernel *GetBestKernel(const LaunchParam &launchParam) const override {
    MKI_CHECK(IsConsistent(launchParam), "Failed to check consistent", return nullptr);
    MKI_CHECK(launchParam.GetParam().Type() == typeid(OpParam::CustomizeBlockCopy), ...);
    return GetKernelByName("CustomizeBlockCopyKernel");   // ← 按名找上面的 Kernel
}
```
见 [customize_blockcopy_operation.cpp:99-105](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/customize_blockcopy_operation.cpp#L99-L105)，注册宏在 [L186](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/customize_blockcopy_operation.cpp#L186) `REG_OPERATION(CustomizeBlockCopyOperation)`。

这里 `GetKernelByName("CustomizeBlockCopyKernel")` 的字符串必须与 `REG_KERNEL_BASE(CustomizeBlockCopyKernel)` 完全一致，否则返回 `nullptr`、算子执行失败。`OperationBase` 还承担输入输出个数（[L33-47](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/customize_blockcopy_operation.cpp#L33-L47) 返回 5 入 2 出）与形状推导（`InferShapeImpl` 把输出置为输入 K/V 的形状，[L78-88](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/customize_blockcopy_operation.cpp#L78-L88)）。

**(c) CMake：注册与多芯片**

```cmake
set(customize_blockcopy_srcs
    ${CMAKE_CURRENT_LIST_DIR}/customize_blockcopy_operation.cpp
    ${CMAKE_CURRENT_LIST_DIR}/customize_blockcopy_kernel.cpp
    ${CMAKE_CURRENT_LIST_DIR}/tiling/customize_blockcopy_tiling.cpp
)
add_operation(CustomizeBlockCopyOperation "${customize_blockcopy_srcs}")   # Host 代码 + 注册名

add_kernel(customize_blockcopy ascend910b vector
    op_kernel/customize_blockcopy.cpp      CustomizeBlockCopyKernel)       # 910B 的 Device 代码
add_kernel(customize_blockcopy ascend310p vector
    op_kernel/customize_blockcopy_310p.cpp CustomizeBlockCopyKernel)       # 310P 的 Device 代码（同名 Kernel）
```
见 [CMakeLists.txt:1-14](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/CMakeLists.txt#L1-L14)。

解读：
- `add_operation` 的第一个参数 `CustomizeBlockCopyOperation` 必须与 `REG_OPERATION` 一致。
- 同一个算子为 910B 和 310P 各写一份 Device Kernel（`customize_blockcopy.cpp` 与 `customize_blockcopy_310p.cpp`，后者因 310P 的 NZ 排布与 UB 容量不同而单独实现），但**共用同一个 Kernel 类名 `CustomizeBlockCopyKernel`**——框架按芯片挑对应的 `.cpp` 编译产物，名字却统一，这是「多芯片适配」的标准做法。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：验证「注册名三处一致」这条铁律在源码中的落地。
2. **操作步骤**：
   - 用本仓库搜索工具，分别在以下三处查找字符串 `CustomizeBlockCopyKernel`：
     - `customize_blockcopy_kernel.cpp` 的 `REG_KERNEL_BASE`（注册）。
     - `customize_blockcopy_operation.cpp` 的 `GetKernelByName`（查找）。
     - `CMakeLists.txt` 的 `add_kernel`（绑定 Device `.cpp`）。
   - 同样查找 `CustomizeBlockCopyOperation`：`REG_OPERATION`（注册）与上层 Runner 的 `opDesc`。
3. **需要观察的现象**：三处（或四处）字符串拼写、大小写完全相同。
4. **预期结果**：全部一致。若任一处拼错（如少了 `Copy`），运行时 `GetKernelByName` 返回空、算子无法执行——这是自定义算子最常见、最难排查的低级错误。
5. 待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`KernelBase` 的 `InitImpl` 与 `OperationBase` 的 `GetBestKernel` 谁先被调用？
> **答案**：`GetBestKernel` 先被调用——它负责「按名选出要用的 Kernel 实例」；选中后框架才调用该 Kernel 的 `InitImpl`（即调你的 Tiling 函数）完成切分。即「先选 Kernel，再做 Tiling」。

**练习 2**：为什么 910B 和 310P 用两份不同的 `.cpp`，却共用同一个 `CustomizeBlockCopyKernel` 类名？
> **答案**：从调度角度看，无论哪种芯片，算子的「逻辑身份」是同一个（上游 Runner 只认 `opDesc` 字符串），故类名统一；但从实现看，310P 的 NZ 排布、UB 容量、对齐要求与 910B 差异大，必须用不同代码。CMake 的 `add_kernel` 按「芯片 → 对应 `.cpp`」分别编译，运行时框架据芯片加载正确产物，从而「名字统一、实现分流」。

## 5. 综合实践

**任务**：参照 `addcustom` 文档范例与 `customize_blockcopy` 真实样例，为一个假想的「逐元素乘法 `myelementmul`（z = x * y，fp16）」算子，写出它 Kernel 层从 Tiling 到 CopyIn/Compute/CopyOut 再到注册的完整设计（不要求可编译，重点是步骤与对应文件）。

请按以下清单产出（标注「示例代码」的为你自行编写的伪代码，非仓库现有代码）：

1. **`TilingData` 设计**（参考 [tiling_data.h:17-29](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/tiling/tiling_data.h#L17-L29)）：至少包含 `totalLength`、`tileNum`、`blockDim`、`typeByte`，并说明每字段的 Host/Device 含义。

2. **Tiling 算法**（参考 [customize_blockcopy_tiling.cpp:162-175](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/tiling/customize_blockcopy_tiling.cpp#L162-L175)）：写出 `actualCore = min(totalLength 对应核数, maxCore)`、按核切 `tileNum`、`tilingKey = TILING_DTYPE_IDX * typeByte`、`SetBlockDim`/`SetTilingId` 的伪代码。

3. **Device Kernel 三段式**（参考 [customize_blockcopy.cpp:225-248](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/op_kernel/customize_blockcopy.cpp#L225-L248) 与文档 [addcustom CopyIn/Compute/CopyOut](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/starting_from_a_simple_operator.md#L306-L347)）：写出 `CopyIn`（`DataCopy` x、y 入 UB）→ `Compute`（`Mul(z, x, y)`）→ `CopyOut`（`DataCopy` z 回 GM）的循环骨架，并标出 `AllocTensor/EnQue/DeQue/FreeTensor` 的位置与 `TQue` 双缓冲。

4. **入口与分支**（参考 [customize_blockcopy.cpp:305-322](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/op_kernel/customize_blockcopy.cpp#L305-L322)）：写 `extern "C"` 入口，用 `TILING_KEY_IS(...)` 在 fp16 时实例化 `MyElementMul<half>`。

5. **注册三件套**（参考 [customize_blockcopy_kernel.cpp:62-70](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/customize_blockcopy_kernel.cpp#L62-L70) 与 [customize_blockcopy_operation.cpp:99-105](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/customize_blockcopy_operation.cpp#L99-L105)）：写 `KernelBase` 子类（`REG_KERNEL_BASE(MyElementMulKernel)`）、`OperationBase` 子类（`GetBestKernel` 内 `GetKernelByName("MyElementMulKernel")`、`REG_OPERATION(MyElementMulOperation)`）、`CMakeLists.txt`（`add_operation` + `add_kernel`）。

**验收标准**：能说清每一步「这一步在哪个文件、对应 `customize_blockcopy` 的哪段代码、为什么这么做」，并指出「注册名三处一致」「TilingData 字段顺序契约」「双缓冲掩盖延迟」这三条原则分别落实在你的设计的哪一处。

> 想真正编译运行？`ops_customize` 目录支持独立编译与测试。可参照 [README.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/README.md) 的「方式一：单独编译」，`cd ops_customize && bash build.sh`；测试用例写法见 [customize_blockcopy_test.cpp:93-132](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/tests/customize_blockcopy_test.cpp#L93-L132)（标准的 aclInit→CreateContext→Setup→Execute→同步→释放骨架，详情见 u2-l1）。运行需真实昇腾 NPU 与 CANN 环境，本环境无法实跑，待本地验证。

## 6. 本讲小结

- **Tiling 是 Host↔Device 的唯一信使**：`TilingData` 字段顺序是隐式契约，Host 写、Device 按相同偏移读；Tiling 算法负责「按核切工作量（`blockDim`/`perCore`/`tail`）+ 设分支码（`tilingId`）」。
- **三段式流水（CopyIn→Compute→CopyOut）是 AscendC 的标准骨架**：数据必须在片上 UB 处理，靠 `TQue` 双缓冲（`BUFFER_NUM=2`）让「下一块搬入」与「当前块计算」并行，掩盖访存延迟；产物是标量时可省 CopyOut，纯搬运时可省 Compute。
- **`tilingKey` 用 `typeByte` 编码 dtype 分支**：Host `SetTilingId(TILING_DTYPE_IDX * typeByte)`、Device `TILING_KEY_IS(...)`，两端必须同源；多 dtype 常用 C++ 模板 `<half>/<int8_t>` 复用同一 Kernel 类。
- **MKI 注册靠两个基类 + 两个宏**：`KernelBase`（`REG_KERNEL_BASE`，管 Tiling）与 `OperationBase`（`REG_OPERATION`，管形状推导与选 Kernel）。
- **注册名三处一致是铁律**：`REG_OPERATION` 名 = 上游 Runner 的 `opDesc`；`GetKernelByName` 名 = `REG_KERNEL_BASE` 名 = `add_kernel` 关联的 Kernel 类名。
- **多芯片适配**：同一 Kernel 类名为 910B/310P 各写一份 Device `.cpp`，由 CMake `add_kernel` 按芯片分别编译，实现「名字统一、实现分流」。

## 7. 下一步学习建议

本讲只完成了「Kernel 层」——Tiling、Device Kernel、MKI 注册。但要真正在 ATB 里调用 `myelementmul`，还需把它接到上层 `Operation`/`Runner`，并补齐 Param、`atb_ops_info.ini`、测试等交付件。建议按以下顺序继续：

1. **u6-l3（自定义算子的框架集成）**：紧接本讲，讲解如何写 ATB 层的 `Operation`（`InferShapeImpl`/`CreateRunner`）与 `OpsRunner`（`SetupKernelGraph`），用 `REG_RUNNER_TYPE`/`REG_OP_PARAM` 把 Kernel 接进 `Operation → Runner → KernelGraph → Kernel` 链路。这是本讲的直接下游。
2. **u6-l4（算子交付件与配置体系）**：补齐 `Param` 定义、`infer_op_params.h`、`atb_ops_info.ini` 规格约束、`param_to_json` 序列化等「交付件」。
3. **u6-l5（ops_customize 独立编译开发流程）**：掌握不重编 ATB 即可开发自定义算子的完整命令与 `customize_ops_info.ini` 配置。
4. 想深入 AscendC 本身（Tiling 算法进阶、Cube/Vector 区别、同步原语 `HardEvent`），可阅读文档 [starting_from_a_simple_operator.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/starting_from_a_simple_operator.md) 与昇腾官方 AscendC 开发指南。
