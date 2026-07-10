# 内存层次与合并访问（coalesced access）

## 1. 本讲目标

本讲是 CUDA 性能优化的「第一性原理」：在动手写任何分块、流水线、Tensor Core 之前，必须先理解两件事——

- GPU 的存储器分好几层，越靠近计算单元的越快、越小、越贵；
- 访问最慢的那一层（HBM 显存）时，**线程访问地址的排布方式**直接决定了一次读写要花几个内存事务。

学完本讲，你应当能够：

1. 说出 HBM / L2 / SMEM / Register 四层存储各自的**带宽与延迟数量级**，并解释「把数据尽量留在更快的层」这一优化总纲。
2. 准确判断一个 kernel 中的访存**是否合并（coalesced）**：同一 warp 的 32 个线程访问连续、对齐地址时合并为 1 次内存事务，否则最坏会变成 32 次。
3. 对 `elementwise_add` 与 `dot` 这类一维 kernel，**指出它们为什么是合并访问**，并理解为什么向量化（float4）能进一步减少指令数。

---

## 2. 前置知识

本讲承接 [u2-l1 线程层次](u2-l1-thread-hierarchy.md)，默认你已经掌握：

- **warp**：GPU 最小的调度单位，固定 32 个线程（`WARP_SIZE = 32`）。这 32 个线程在同一时刻执行同一条指令（SIMT 模型）。
- **全局索引公式**：`idx = blockIdx.x * blockDim.x + threadIdx.x`，它让同一 warp 内相邻线程拿到**连续**的 `idx`——这一点是合并访问的前提。
- **host/device、kernel 启动（launch）** 这些基本概念。

> 一句话回顾：warp 是访存的单位。硬件不是「一个线程一个线程」去取数，而是「一个 warp 整体」去发起内存请求。本讲要回答的核心问题就是：**一个 warp 的 32 个内存请求，能不能被硬件合并成一次真正去显存搬数据的动作。**

补充两个本讲会用到的术语：

- **内存事务（memory transaction）**：硬件实际去某一级存储搬一段连续字节的一次操作。一次事务的粒度通常是 32 字节的 sector，或 128 字节的 cache line/segment。事务越少，效率越高。
- **算术强度（Arithmetic Intensity, AI）**：每搬运 1 字节数据能做多少次浮点运算，\( AI = \text{FLOPs} / \text{Bytes} \)。AI 小（如 GEMV、softmax）意味着「搬数据比算还累」，这类 kernel 叫 **memory-bound**，合并访问对它们尤其关键。

---

## 3. 本讲源码地图

本讲只涉及 3 个文件，都是 LeetCUDA 仓库中确认存在的：

| 文件 | 作用 | 本讲用到的部分 |
| --- | --- | --- |
| `kernels/interview/notes-v2.cu` | 单文件面试笔记，Phase 0 是纯注释的「GPU 架构 / 内存层次 / Roofline 速查」 | Phase 0 的内存层次带宽数、合并访问条目、瓶颈判断 |
| `kernels/elementwise/elementwise.cu` | 逐元素加 kernel（含 float4 向量化版） | `elementwise_add_f32_kernel`、`elementwise_add_f32x4_kernel` |
| `kernels/dot-product/dot_product.cu` | 点积 kernel（逐元素乘 + warp/block 归约） | `dot_prod_f32_f32_kernel`（分析其访存模式） |

阅读顺序建议：先看 `notes-v2.cu` 的 Phase 0 注释建立「地图」，再用 `elementwise.cu` / `dot_product.cu` 的真实 kernel 做案例验证。

---

## 4. 核心概念与源码讲解

### 4.1 GPU 内存层次：HBM / L2 / SMEM / Register

#### 4.1.1 概念说明

一颗 GPU 并没有「统一的一种内存」，而是堆叠了多种存储器，按「离计算核心由远到近」排列：

```
┌──────────────────────────────────────────────┐
│  HBM (显存，全局内存 global memory)            │  ← 最大、最慢、所有线程可见
│    ↕                                          │
│  L2 Cache                                     │  ← 全 SM 共享
│    ↕                                          │
│  L1 / Shared Memory (SMEM)                    │  ← 一个 SM 内、block 间共享
│    ↕                                          │
│  Register File (寄存器)                        │  ← 一个线程私有，最快
└──────────────────────────────────────────────┘
```

| 层级 | 谁能访问 | 容量量级 | 定位 |
| --- | --- | --- | --- |
| HBM（global memory） | 所有线程 | 几十 GB | kernel 输入输出数据默认存这里，最慢但最大 |
| L2 Cache | 所有 SM 共享 | 几十 MB | 硬件自动管理，对程序员透明 |
| Shared Memory (SMEM) | 同一个 block 的线程 | 每 SM 几十~几百 KB | 程序员显式管理，常用来做「分块复用」 |
| Register | 单个线程私有 | 每 SM 256KB 量级 | 程序员通过局部变量隐式使用，最快 |

理解这张表的意义在于**优化总纲**：数据离核心越近，带宽越高、延迟越低；但容量越小。所以高性能 kernel 的本质就是「**把热点数据尽量搬到更快的层去复用，减少对最慢的 HBM 的访问次数**」。这正好呼应了 [u1-l1] 中提到的优化直觉——寄存器 > SMEM > L2 > HBM。

#### 4.1.2 核心流程

数据从 HBM 一路搬到寄存器参与计算的典型路径如下（以 elementwise 为例）：

1. kernel 启动时，输入数组 `a`、`b` 已经位于 **HBM**。
2. 每个 warp 发起读请求，数据先进入 **L2 Cache**（命中则不必再回 HBM）。
3. 若 kernel 用了 `__shared__`，则由程序员先把 HBM 数据 `load` 进 **SMEM**，供同 block 多线程复用（本讲的 elementwise / dot 不需要 SMEM 中转，直接从 global 读进寄存器）。
4. 最终数据进入 **寄存器**，由 ALU 做加法/乘法，结果再原路写回 HBM。

带宽与延迟的数量级对比如下（带宽数字来自仓库 Phase 0 的 H100 参考；延迟列为通用参考量级，便于建立直觉，**待本地验证**）：

| 层级 | 等效带宽（H100 参考） | 延迟量级（通用参考） |
| --- | --- | --- |
| HBM3 | ~3.35 TB/s（理论），实际 ~2.5–3.0 TB/s | ~400–800 周期 |
| L2 Cache | ~12 TB/s（50MB，跨 SM 共享） | ~200 周期 |
| L1 / SMEM | ~19 TB/s（每 SM ~228KB） | ~20–30 周期 |
| Register | ~100+ TB/s 等效 | ~0 延迟（~1 周期） |

可以看到：从寄存器到 HBM，**延迟差出几百倍**。这就是为什么「减少 HBM 访问」几乎是所有优化的出发点。

> ⚠️ 注意：带宽/延迟的具体数字会随 GPU 代际（Ampere / Ada / Hopper / Blackwell）变化，本表用于建立量级直觉。仓库 Phase 0 明确标注这些是 **H100 参考**值。

#### 4.1.3 源码精读

仓库把这套内存层次速查写在了 `notes-v2.cu` 的 Phase 0 注释里。先看带宽数量级：

这是 Phase 0 给出的四层存储带宽数量级（H100 参考）——HBM 实际只有 ~2.5–3.0 TB/s，而寄存器等效带宽高达 100+ TB/s，两者相差近两个数量级：

[notes-v2.cu:35-39 — Memory Hierarchy 带宽量级（H100）](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L35-L39)

紧接着 Phase 0 用「算术强度 AI」给出了判断 kernel 受限于什么的依据：AI 小于机器的 FLOPS/带宽 比值就是 memory-bound，AI 足够大才是 compute-bound，线程不够多则 latency-bound：

[notes-v2.cu:41-44 — 关键瓶颈判断（memory/compute/latency bound）](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L41-L44)

这两段注释把本讲的「为什么」讲透了：

- HBM 是瓶颈层（带宽最低、延迟最高）；
- 判断一个 kernel 会不会卡在 HBM 上，看它的算术强度 AI；
- elementwise / dot 这类「每搬一个数只做一次运算」的 kernel，AI 极低（约 0.25），是典型的 memory-bound，因此**减少 HBM 访问次数（也就是合并访问）几乎是它们唯一的优化方向**。

#### 4.1.4 代码实践

**实践目标**：把 Phase 0 的内存层次数字内化为一张自己的速查表。

**操作步骤**：

1. 打开 `kernels/interview/notes-v2.cu`，定位到第 35–44 行（Phase 0 的 Memory Hierarchy 与瓶颈判断注释）。
2. 用一张表抄录四层存储的「带宽量级」与「谁可访问」。
3. 用 Phase 0 提供的 AI 公式，对 `elementwise_add`（读 2 个 float、写 1 个 float、做 1 次加法）估算算术强度。

**需要观察的现象**：你会算出 `elementwise_add` 的 \( AI = \frac{1\ \text{FLOP}}{12\ \text{Bytes}} \approx 0.083 \)，远小于 H100 FP32 的 ridge point（约 20:1）。

**预期结果**：确认 `elementwise_add` 是严重 memory-bound，从而理解为什么后续（[u2-l3]）对它做向量化优化收效显著。

> 待本地验证：上述 AI 计算为纸面推导，无需运行；如需实测带宽，需要用 ncu 工具（见 [u16-l1]）。

#### 4.1.5 小练习与答案

**练习 1**：为什么把数据「从 HBM 分块加载到 shared memory 复用」能提升性能？

> **参考答案**：HBM 带宽最低、延迟最高；SMEM 带宽约为 HBM 的 6 倍、延迟低一个数量级。把会被多次访问的数据搬到 SMEM，后续命中就不必再回 HBM，从而减少对最慢层的访问次数。这正是后续 GEMM tiling（[u9-l1]）的核心思想。

**练习 2**：寄存器的容量最小，为什么反而最值得用？

> **参考答案**：寄存器延迟接近 0、等效带宽最高（100+ TB/s）。把中间累加结果（如 dot 的 `prod`、GEMM 的输出累加器）放在寄存器里反复读写，几乎没有访存代价。代价是寄存器总量有限，过多会压低 occupancy。

---

### 4.2 合并访问（coalesced access）：判定条件

#### 4.2.1 概念说明

知道了 HBM 是瓶颈，下一个问题就是：**怎样才算「高效地」访问 HBM？** 答案是**合并访问（coalesced memory access）**。

合并访问描述的是一个 **warp**（不是单个线程）发起全局内存读写的效率：

- 当一个 warp 的 32 个线程访问的地址**落在同一段连续、对齐的 128 字节区间**内时，硬件把这些请求**合并成 1 次内存事务**——最高效。
- 反之，如果线程访问的地址分散（比如跨行、跨大步长），硬件无法合并，**最坏会拆成 32 次内存事务**，带宽利用率降到 1/32。

这正是为什么合并访问是「elementwise 这类 kernel 的核心考点」（见 `notes-v2.cu` Phase 2 开头注释：逐元素操作的考点就是 memory coalescing）。

#### 4.2.2 核心流程

判定一次访存是否合并，关键是看「warp 内 32 个线程访问的地址」的分布。以最常见的 `float`（4 字节）为例：

- 一个 warp 有 32 个线程，每个线程读 1 个 float，共 \( 32 \times 4 = 128 \) 字节。
- HBM 的最小事务粒度按 128 字节对齐的 segment 衡量。
- 若线程 `i` 访问的地址是 `base + i * 4`（即 `a[idx]`，`idx` 连续），则 32 个地址恰好填满**一个**对齐的 128 字节 segment → **1 次事务**。

形式化地，一个 warp 触发的内存事务数为：

\[
\text{transactions} = \big|\,\{\,\text{warp 内 32 个地址所涉及的 distinct 128B segments}\,\}\,\big|
\]

几种典型情形：

| warp 内地址模式 | 涉及 segment 数 | 是否合并 |
| --- | --- | --- |
| `a[idx]`，`idx` 连续且 128B 对齐 | 1 | ✅ 完全合并 |
| `a[idx]`，`idx` 连续但未对齐 | 2 | ⚠️ 跨 segment，略亏 |
| `a[idx * stride]`，stride 很大（如转置的列写） | 32 | ❌ 完全不合并 |
| 全部线程读**同一个**地址 | 1（广播） | ✅ 广播合并 |

> 直觉记忆：**「相邻线程读相邻元素」= 合并**。这正是 [u2-l1] 那条灵魂公式 `idx = blockIdx.x * blockDim.x + threadIdx.x` 的副产品——因为 warp 内 `threadIdx.x` 连续，所以 `idx` 连续，所以地址连续，所以天然合并。

#### 4.2.3 源码精读

仓库 Phase 0 的「优化手段速查清单」第 1 条就明确给出了合并访问的判定标准——同一 warp 访问连续 128B 对齐地址则 1 次事务，否则最坏 32 次：

[notes-v2.cu:53-55 — Coalesced Memory Access 优化条目](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L53-L55)

而「最坏 32 次」并非夸大。在 Phase 5 的矩阵转置专题里，仓库用真实场景印证了这一点——朴素转置按列优先写入，warp 的 32 个线程分散在 32 个不连续地址上，于是产生 32 次内存事务：

[notes-v2.cu:879-887 — 转置 naive 版非合并写入导致 32 次事务](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L879-L887)

这段注释把「为什么转置难写快」讲得非常清楚：**读**是按行优先（warp 32 线程访问 32 个连续地址 → 合并读取 ✓），但**写**是按列优先（32 线程跨 row 行分散 → 非合并写入 ✗，32 次事务）。这种「读合并、写不合并」的不对称，正是后续需要用 SMEM 中转 + merge_write 来修复的（详见 [u7-l1]）。

> 小结：合并访问看的是**一个 warp**的地址分布，不是看单个线程。`a[idx]` 配合连续 `idx` 是合并的黄金模式。

#### 4.2.4 代码实践

**实践目标**：用一个反例加深对「不合并 = 32 次事务」的理解（纯阅读型实践，不需运行）。

**操作步骤**：

1. 阅读 `notes-v2.cu` 第 879–887 行的转置 naive 版注释。
2. 想象一个 16×16 的 block、`blockDim = (16, 16)`，warp 内 32 个线程的 `threadIdx.x` 取 0..31 连续，但它们写 `y` 时地址是 `y[r][c]` 沿「列」方向跨行分布。
3. 数一下：这 32 个写入地址分别落在多少个 128B segment 上。

**需要观察的现象**：你会算出这 32 个写地址分散在 32 个不同的 segment 上（因为列方向上相邻元素相隔一整行，远超 128B）。

**预期结果**：确认朴素转置的写入触发了 32 次事务，从而理解为什么 Phase 0 把 Coalesced Access 列为「优化清单第 1 条」。

#### 4.2.5 小练习与答案

**练习 1**：以下哪种访问是合并的？(a) warp 内每个线程读 `a[threadIdx.x]`；(b) 每个线程读 `a[threadIdx.x * 1024]`。

> **参考答案**：(a) 合并——相邻线程读相邻元素，32 个地址落在 1 个 128B segment；(b) 不合并——相邻线程地址相隔 1024×4=4096 字节，32 个地址落在 32 个 segment，产生 32 次事务。

**练习 2**：为什么「全部线程读同一个地址」只算 1 次事务，却不浪费带宽？

> **参考答案**：硬件对 warp 内同一地址的访问做**广播**——一次事务把数据取回后，广播给 warp 内所有线程，所以仍只算 1 次事务，带宽没有浪费。

---

### 4.3 源码精读：elementwise 与 dot 的访存模式

#### 4.3.1 概念说明

有了 4.1（内存层次）和 4.2（合并判定），现在把它们应用到 LeetCUDA 两个最基础的一维 kernel 上：`elementwise_add` 和 `dot`。目标是用「相邻线程读相邻元素」这条规则，**判定它们的访存是否合并**，并解释为什么这两个 kernel 已经天然高效、唯一的优化空间只剩「向量化减少指令数」。

#### 4.3.2 核心流程

判定一个一维 kernel 访存是否合并，固定四步：

1. 找到 warp 内线程的索引 `idx` 随 `threadIdx.x` 如何变化。
2. 看 `idx` 是否连续（相邻 `threadIdx.x` → 相邻 `idx`）。
3. 看地址 `&a[idx]` 是否落在同一对齐 128B segment。
4. 结论：连续且对齐 → 合并（1 事务）；否则按 segment 数计事务。

对 `elementwise_add_f32_kernel` 和 `dot_prod_f32_f32_kernel`，由于 `idx` 都由 [u2-l1] 的灵魂公式给出，warp 内 32 个线程的 `idx` 恰好连续，因此**读 `a`、读 `b`、写 `c` 全部合并**。

#### 4.3.3 源码精读

**① elementwise_add：教科书级的合并访问**

先看 `elementwise.cu` 的基础版。`idx` 用连续公式算出，每个线程读 `a[idx]`、读 `b[idx]`、写 `c[idx]`——三组访存都是「相邻线程访问相邻元素」，天然合并：

[elementwise.cu:23-28 — elementwise_add_f32_kernel，idx 连续 → 读写全合并](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/elementwise/elementwise.cu#L23-L28)

关键就两行：

```cpp
int idx = blockIdx.x * blockDim.x + threadIdx.x;  // warp 内 idx 连续
if (idx < N)
  c[idx] = a[idx] + b[idx];   // 读 a/读 b/写 c 均合并
```

> 细节：`if (idx < N)` 是越界保护（[u2-l1] 讲过），它会让 block 尾部那一个 warp 里部分线程不参与访存。这部分线程的「不访问」不产生事务，对合并性没有负面影响。

`notes-v2.cu` Phase 2 收录了同一份实现（行号略有差异），并明确点出「逐元素操作的考点就是 memory coalescing」：

[notes-v2.cu:380-384 — Phase 2 收录的 elementwise_add](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/interview/notes-v2.cu#L380-L384)

**② dot product：仍是合并读，再叠加归约**

再看 `dot_product.cu` 的基础版。它的访存部分与 elementwise 完全同构——`idx` 连续，每个线程读 `a[idx]`、读 `b[idx]`、相乘得到 `prod`：

[dot_product.cu:34-57 — dot_prod_f32_f32_kernel，读 a/b 合并，再 warp+block 归约](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/dot-product/dot_product.cu#L34-L57)

关键访存行：

```cpp
float prod = (idx < N) ? a[idx] * b[idx] : 0.0f;  // 读 a/b，合并
```

注意 dot 比 elementwise 多了一个**归约**阶段（先 warp 内用 `__shfl_xor_sync` 归约，再经 SMEM 做 block 归约，最后 `atomicAdd` 写回单个 `y`）：

```cpp
prod = warp_reduce_sum_f32<WARP_SIZE>(prod);   // 寄存器内，不走 HBM
if (lane == 0) reduce_smem[warp] = prod;        // 写 SMEM（不是 HBM）
__syncthreads();
...
if (tid == 0) atomicAdd(y, prod);              // 只 1 个线程写 HBM
```

- 归约的前半段（`warp_reduce_sum_f32`）完全在**寄存器**里做（用 `__shfl_xor_sync` 线程间交换），不产生 HBM 访问——这正是 4.1 讲的「尽量留在更快层」。
- 写回 `y` 只有 `tid == 0` 一个线程做 `atomicAdd`，单点写，不存在合并与否的问题（归约原语的细节留到 [u4-l1]）。

所以 dot 的 HBM 访存只有「读 `a`、读 `b`」两处，且都合并。它和 elementwise 一样，**已经是合并访问的范本**。

**③ 为什么向量化能进一步提升？**

既然已经合并，为什么 `notes-v2.cu` 还要提供 `relu_vec4` / `elementwise_add_vec4` / `dot_vec4`？因为合并访问只保证「1 个 warp 用 1 次事务取回 128B」，而向量化（float4）让**每个线程**用**一条指令**就取走 16 字节，从而把「访存指令条数」压到原来的 1/4，减小了指令发射压力和 warp 调度开销（这部分留到 [u2-l3] 详讲）。这里只看它的访存依然合并：

[elementwise.cu:33-50 — elementwise_add_f32x4_kernel，每线程处理 4 个连续 float，访存仍合并](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/elementwise/elementwise.cu#L33-L50)

关键点：`idx = 4 * (...)`，warp 内 32 个线程分别负责 `[idx, idx+1, idx+2, idx+3]` 这 4 组，整体仍是 32×4=128 个连续 float = 512 字节，跨 4 个连续 128B segment → 4 次事务取回 4 倍数据，**单位字节的合并效率不变**，但指令数降到 1/4。

#### 4.3.4 代码实践（本讲主实践，源码阅读型）

**实践目标**：独立判定 `dot_product.cu` 中 `dot_prod_f32_f32_kernel` 的访存是否合并，并给出理由。

**操作步骤**：

1. 打开 `kernels/dot-product/dot_product.cu`，阅读第 34–57 行的 `dot_prod_f32_f32_kernel`。
2. 列出该 kernel 中**所有访问全局内存（HBM）**的语句：读 `a[idx]`、读 `b[idx]`、`atomicAdd(y, ...)`。
3. 对每一处，回答：是哪个 warp 的哪些线程访问？地址是否连续对齐？
4. 区分出哪些是访问 HBM、哪些是访问 SMEM（`reduce_smem`）、哪些只在寄存器内（`warp_reduce_sum_f32` 的 `__shfl_xor_sync`）。

**需要观察的现象**：

- `a[idx]` / `b[idx]`：warp 内 `idx` 连续 → 合并读，每 warp 1 次事务。
- `reduce_smem[warp] = prod`：访问的是 **SMEM** 不是 HBM，本讲不评合并性（SMEM 的 bank conflict 是 [u7-l2] 的主题）。
- `__shfl_xor_sync`：寄存器内 warp 通信，不走内存。
- `atomicAdd(y, prod)`：只有 `tid == 0` 一个线程写，单点写，无合并问题。

**预期结果**：得出结论——`dot` kernel 的 HBM 读取（`a`、`b`）是**完全合并**的；它的性能瓶颈在「搬数据」本身（AI≈0.25，memory-bound），而非访存模式不优。这正是为什么 dot/elementwise 后续只能靠**向量化**（[u2-l3]）来减指令，而无法靠「改访问模式」再省事务。

> 待本地验证：若想看到真实事务数与带宽，需用 `ncu --set memory` 对该 kernel profile（见 [u16-l1]）。

#### 4.3.5 小练习与答案

**练习 1**：`dot_prod_f32_f32_kernel` 里 `prod = warp_reduce_sum_f32<WARP_SIZE>(prod)` 这一行为什么不产生 HBM 访问？

> **参考答案**：`warp_reduce_sum_f32` 用 `__shfl_xor_sync` 在 warp 内的**寄存器之间**交换数据（蝶形归约），全程不读写 HBM，也不读写 SMEM。归约结果留在每个线程自己的寄存器里。

**练习 2**：如果把 `dot` kernel 改成「每个线程读 `a[idx*4]`（步长 4）」会怎样？

> **参考答案**：相邻线程地址相隔 16 字节，32 个线程仍可能落在较少的 segment 内（具体取决于对齐），但整体访存变得「稀疏」、可读性差且容易踩未对齐。正确做法是像 `elementwise_add_f32x4_kernel` 那样，让每个线程负责**连续**的 4 个元素（`idx = 4*base`，访问 `idx..idx+3`），既保持合并又减少指令。

**练习 3**：`elementwise_add` 的算术强度 \( AI \) 约为多少？据此判断它是 memory-bound 还是 compute-bound。

> **参考答案**：\( AI = \frac{1\ \text{FLOP（一次加法）}}{3 \times 4\ \text{Bytes（读 a、读 b、写 c）}} = \frac{1}{12} \approx 0.083 \) FLOP/Byte，远低于 H100 FP32 的 ridge point（~20），所以是**严重 memory-bound**。

---

## 5. 综合实践

把本讲三个模块串起来，完成一份「内存层次 + 合并访问」小报告（纯源码阅读型，无需 GPU）：

1. **抄录地图**：阅读 `kernels/interview/notes-v2.cu` 第 35–44 行，列出 HBM / L2 / SMEM / Register 四层的带宽量级，并标注哪一层是 kernel 性能瓶颈、为什么。
2. **判定合并**：阅读 `kernels/dot-product/dot_product.cu` 第 34–57 行的 `dot_prod_f32_f32_kernel`，逐条列出它的 HBM 访存语句（读 `a`、读 `b`、`atomicAdd` 写 `y`），判定哪些合并、哪些是单点写，并解释「为什么 `a[idx]`、`b[idx]` 合并」（用到 4.2.2 的 segment 公式）。
3. **延伸推演**：对比 `dot_prod_f32_f32_kernel`（第 34–57 行）与 `dot_prod_f32x4_f32_kernel`（第 62–88 行），说明向量化版如何在不破坏合并性的前提下把每个线程的工作量从 1 个 float 提到 4 个。
4. **写一句话结论**：用算术强度 AI 论证 elementwise / dot 这类 kernel「优化只能靠减少 HBM 指令数（向量化），而非靠改访问模式省事务」。

> 预期产物：一张四层存储带宽表 + dot kernel 的访存合并分析 + 一段 AI 论证。完成后你应当能在看到任意一维 kernel 时，本能地先问一句：「warp 内线程的地址连不连续？」

---

## 6. 本讲小结

- GPU 存储分四层：HBM（慢而大）→ L2 → SMEM → Register（快而小）；优化总纲是「把热点数据尽量搬到更快的层」，减少对最慢的 HBM 的访问。
- 仓库 Phase 0 给出了 H100 的带宽量级参考（HBM ~2.5–3.0 TB/s、L2 ~12 TB/s、SMEM ~19 TB/s、Register ~100+ TB/s）和 memory/compute/latency-bound 的判断依据（算术强度 AI）。
- **合并访问看的是整个 warp**：32 个线程访问连续、对齐的 128B 地址 → 1 次内存事务；分散访问最坏 32 次。判定口诀是「相邻线程读相邻元素」。
- `elementwise_add` 与 `dot` 用 `idx = blockIdx.x*blockDim.x + threadIdx.x` 让 warp 内 `idx` 连续，所以读 `a`、读 `b`、写 `c` **全部天然合并**——它们已经是合并访问的范本。
- `dot` 的归约阶段（`__shfl_xor_sync` + SMEM）不产生 HBM 访问，只有 `a[idx]`、`b[idx]` 是合并读、`atomicAdd(y)` 是单点写。
- elementwise / dot 的 AI≈0.083–0.25，严重 memory-bound，所以它们后续的优化方向是「向量化减少访存指令数」（[u2-l3]），而不是「改访问模式省事务」。

---

## 7. 下一步学习建议

- **立刻衔接 [u2-l3 向量化访存]**：本讲多次提到「合并已满，只能靠向量化减指令」，下一讲就用 `relu_f32x4_kernel` / `elementwise_add_f32x4_kernel` / `dot_vec4` 把这件事彻底讲透。
- **横向阅读源码**：再看一眼 `notes-v2.cu` 第 347–407 行（Phase 2 的 relu / elementwise_add 及其 vec4 版），对比「朴素版」与「向量化版」在指令数上的差异。
- **后续伏笔**：
  - 合并访问解决的是 **HBM** 的访问效率；当数据进到 **SMEM** 后，要操心的是 **bank conflict**（[u7-l2]）。
  - 归约原语（`warp_reduce_sum` / block reduce）在本讲只是路过，完整推导见 [u4-l1]。
  - 想用真实数字验证合并效果，学完 [u16-l1] 的 ncu 指标后可以回来实测 dot kernel 的事务数与带宽。
