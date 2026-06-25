# 分块调度与 L2 swizzle

## 1. 本讲目标

本讲承接 u6-l1（内核入口：SM90 FP8 GEMM 1D1D）。u6-l1 讲了「一个 block 内部，TMA warp-group 与 math warp-group 如何用双缓冲流水线合作算完一个输出瓦片」。但一次 GEMM 有成百上千个输出瓦片，**这些瓦片到底由哪个 SM 在什么时刻去算？按什么顺序算？**——这就是本讲的主角：**持久化分块调度器（persistent block scheduler）**，实现在 [`scheduler/gemm.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh) 的 `Scheduler` 模板里。

读完本讲，你应当能够：

1. 说清楚 **持久化调度** 与 **wave（波）** 的概念：为什么 DeepGEMM 不用「一个输出瓦片对应一个 CTA」的传统 launch 网格，而是让每个 SM 在一个 `while` 循环里反复「领瓦片、算、再领」，并理解 `current_iter * kNumSMs + blockIdx.x` 这个分配公式。
2. 画清楚 **L2 swizzle（块序重排）** 的映射规则：手算给定 `block_idx` 对应的 `(m_block, n_block)`，并解释「让相邻 SM 共享同一块 B（或 A）瓦片」如何显著提升 L2 缓存命中率。
3. 看懂 **multicast（多播）在 SM90 上的动态启用/禁用判定**：`is_peer_cta_alive`、`get_swizzled_block_idx` 里的奇偶对齐修正、`is_tma_multicast_valid` 三处逻辑，并理解为什么这套动态判定是 SM90 专属、SM100 不能这么做。

---

## 2. 前置知识

### 2.1 为什么要「调度」：从朴素 launch 说起

朴素 CUDA kernel 的写法是「输出多大，就 launch 多少个 block」：一个 GEMM 输出矩阵被切成 \( \text{num\_m\_blocks} \times \text{num\_n\_blocks} \) 个瓦片，launch 这么多个 CTA，硬件自己把它们分发到各 SM。这有两个问题：

- **SM 利用率不均**：如果瓦片数不是 SM 数的整数倍，最后一拨 CTA 会先算完，导致部分 SM 空转（尾波利用率低）。
- **L2 缓存冷**：相邻 CTA 各算各的瓦片，加载的全局内存数据互不相同，L2 命中率低。

**持久化调度（persistent kernel）** 的思路反过来：launch 恰好 `kNumSMs` 个 CTA（通常每个 SM 一个），让它们在一个 `while` 循环里**主动领取**输出瓦片，直到所有瓦片算完。这样：

- 每个 SM 干完一块立刻领下一块，天然负载均衡（尾波只剩「最后没领完的零头」）。
- 调度器可以**控制领取顺序**（swizzle），让同一拨并发执行的 CTA 尽量共享数据，把 L2 用满。

> 关键直觉：持久化调度的核心不是「调度」本身，而是「**顺序可控**」——只有自己管顺序，才能做 L2 swizzle 与 multicast 对齐。

### 2.2 wave（波）是什么

一个 **wave** 指「让全部 SM 各算一个瓦片的一轮完整扫描」。设瓦片总数为 \( B \)、SM 数为 \( S \)，则：

\[
\text{num\_waves} = \left\lceil B / S \right\rceil
\]

最后一波的活跃 SM 数为 \( B \bmod S \)（若整除则为 \( S \)），称为 **last_wave_util**（尾波利用率）。这两个量在宿主启发式里被用来估算 kernel 的总周期数（见 u5-l2 与 4.1.3 的源码印证）：波数越多、尾波越短，性能越差。**注意区分**：`num_waves` 是宿主侧「成本估算」的概念；设备侧调度器并不显式计算 wave，而是用「递增 `current_iter`」隐式地走过每一个波。

### 2.3 multicast（TMA 多播）速查

| 概念 | 含义 |
|------|------|
| **multicast** | SM90 cluster 能力：一次 TMA 加载把同一块全局内存**同时**搬进 cluster 内多个 CTA 的共享内存，省带宽（u4-l2、u6-l3） |
| **`kNumMulticast`** | 多播的 CTA 数，DeepGEMM 里只支持 `1`（关）或 `2`（2-CTA 多播） |
| **`kIsMulticastOnA`** | 多播沿 A 还是 B：多播 A 表示多个 CTA 共享同一块 A 瓦片、各算不同的 B 瓦片（对应「在 N 轴分组」） |
| **peer CTA** | 2-CTA 多播里，与自己配对共享数据的那个 CTA |

multicast 与本讲的紧密关系：**swizzle 的分组方向必须与 multicast 方向一致**——多播哪边，就把哪边作为「组内共享轴」。调度器用一个标志 `kIsMulticastOnA` 同时驱动 swizzle 与 multicast。

### 2.4 承接前讲

u6-l1 里 `sm90_fp8_gemm_1d1d_impl` 的 TMA 线程组和 math 线程组各有一句 `while (scheduler.get_next_block(m_block_idx, n_block_idx))`（[sm90_fp8_gemm_1d1d.cuh:179](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L179) 与 [sm90_fp8_gemm_1d1d.cuh:248](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L248)）。本讲就回答：**这个 `get_next_block` 内部到底怎么决定下一块算什么？**

---

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 |
|------|------|
| [`scheduler/gemm.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh) | `Scheduler` 模板：持久化分块调度器，含块分配、L2 swizzle、multicast 判定、分组/掩码/k 分组四类 `GemmType` 分支 |

辅助印证（用到调度器的真实 kernel 与宿主成本估算）：

| 文件 | 作用 |
|------|------|
| [`impls/sm90_fp8_gemm_1d1d.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh) | 普通 GEMM 内核：演示 `get_next_block` / `is_tma_multicast_valid` / `is_peer_cta_alive` 的真实调用 |
| [`csrc/jit_kernels/heuristics/sm90.hpp`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp) | 宿主启发式：用 `num_waves` / `last_wave_util` 估算成本，并静态判定 multicast 合法性 |
| [`common/types.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/common/types.cuh) | `GemmType` 枚举（Normal/MGroupedContiguous/MGroupedMasked/KGroupedContiguous/…） |

---

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：**块分配与 wave**、**L2 swizzle**、**multicast 判定**。三者层层递进：先看「谁来算哪块」（4.1），再看「按什么顺序算以喂饱 L2」（4.2），最后看「多播场景下的对齐修正」（4.3）。

### 4.1 块分配与 wave

#### 4.1.1 概念说明

`Scheduler` 是一个**设备侧**的轻量结构体，每个 CTA 持有一份。它的核心方法 `get_next_block(m_block_idx, n_block_idx)` 返回 `bool`：领到一块就回填 `(m_block, n_block)` 并返回 `true`；没活干了返回 `false`，外层 `while` 随即结束。

它解决两个问题：

1. **持久化领号**：用一个成员 `current_iter`（初值 `-1`）当「我已经干了几轮」的计数器。每调用一次自增，结合本 CTA 的 `blockIdx.x` 算出一个全局线性瓦片号 `next_block_idx`。
2. **按 `GemmType` 分支**：稠密、M 轴分组、掩码分组、K 轴分组各自的「瓦片总数」与「如何把线性号拆成 (m,n)」逻辑不同，靠 `if constexpr (kGemmType == ...)` 在编译期分流。

> 术语约定：本讲「block」有时指 **CTA**（线程块，硬件执行单元），有时指 **输出瓦片**（tile，输出矩阵的一小块）。上下文里 `block_idx` / `m_block` / `n_block` 都是指「瓦片」；`blockIdx.x` 指 CTA。SM 与 CTA 在持久化 kernel 里大致一一对应。

#### 4.1.2 核心流程

最关键的一行在 `get_next_block` 开头：

```cpp
const auto next_block_idx = (++ current_iter) * kNumSMs + blockIdx.x;
```

这条公式就是「持久化领号」的全部精髓，把它展开成伪代码：

```
领号(iter, cta):  返回 iter * kNumSMs + cta
```

- 第 0 轮（`iter=0`）：CTA 0..kNumSMs-1 分别领瓦片 0..kNumSMs-1 —— 这就是**第 0 个 wave**。
- 第 1 轮（`iter=1`）：各 CTA 领瓦片 kNumSMs..2*kNumSMs-1 —— **第 1 个 wave**。
- 依此类推。当 `next_block_idx >= num_blocks` 时返回 `false`，CTA 退出循环。

于是 wave 的概念自然浮现：**`current_iter` 就是 wave 编号**（从 0 起算），每个 wave 让全部 SM 各领一块。设瓦片总数 \( B \) 与 SM 数 \( S \)，则总波数为：

\[
\text{num\_waves} = \left\lceil B / S \right\rceil, \qquad
\text{last\_wave\_util} = \begin{cases} B \bmod S & B \bmod S \neq 0 \\ S & \text{否则} \end{cases}
\]

对于普通稠密 GEMM，瓦片总数在**构造函数**里就算好了：

\[
B = \text{num\_blocks} = \text{num\_m\_blocks} \times \text{num\_n\_blocks}
\]

其中 `num_m_blocks = ceil_div(shape_m, BLOCK_M)`、`num_n_blocks = ceil_div(shape_n, BLOCK_N)`。

#### 4.1.3 源码精读

**① 构造函数：算出瓦片总数（Normal/Batched 分支）**

[scheduler/gemm.cuh:88-115](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L88-L115) —— 构造函数按 `kGemmType` 分流。Normal 与 Batched 都有完整的 `num_m_blocks * num_n_blocks` 瓦片：

```cpp
num_m_blocks = math::ceil_div(shape_m, BLOCK_M);
num_n_blocks = math::ceil_div(shape_n, BLOCK_N);
...
if constexpr (kGemmType == GemmType::Normal or kGemmType == GemmType::Batched) {
    num_blocks = num_m_blocks * num_n_blocks;
}
```

> 注意 `MGroupedMasked` 分支**不**在这里设 `num_blocks`——因为掩码分组下，每个 expert 的有效行数运行时才知道，瓦片总数要在 `get_next_block` 里动态累加（见 4.1.4 的分组讨论）。

**② 领号公式与 Normal 分支退出判定**

[scheduler/gemm.cuh:197-198](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L197-L198) 与 [gemm.cuh:275-285](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L275-L285) —— `get_next_block` 的 Normal 分支最简洁，正好体现「领号 + 退出」：

```cpp
const auto next_block_idx = (++ current_iter) * kNumSMs + blockIdx.x;
...
} else {  // Normal
    if (next_block_idx >= num_blocks)
        return false;                       // 没活干了
    // SM90 multicast 相关，4.3 详述
    is_peer_cta_alive = num_n_blocks % kNumMulticast == 0 or ...;
    get_swizzled_block_idx(next_block_idx, m_block_idx, n_block_idx);  // 4.2 详述
}
return true;
```

**③ wave 是宿主成本估算的单位（印证 2.2）**

`num_waves` / `last_wave_util` 这两个量在宿主启发式 [`sm90.hpp:202-208`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L202-L208) 计算，用来估 kernel 周期数：

```cpp
const auto num_blocks = ...;
const auto num_waves = ceil_div(num_blocks, desc.num_sms);
const auto num_last_blocks = num_blocks % desc.num_sms;
const auto last_wave_util = num_last_blocks == 0 ? desc.num_sms : num_last_blocks;
...
float wave_efficiency = static_cast<float>(num_blocks) / (num_waves * desc.num_sms);
int64_t num_cycles = ... / wave_efficiency;
```

设备侧调度器并不显式算 wave，但它「递增 `current_iter`」走过每一个波——两端是同一个物理事实的两种表达。

**④ 真实调用点（接 u6-l1）**

[sm90_fp8_gemm_1d1d.cuh:179](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L179)（TMA 线程组）与 [sm90_fp8_gemm_1d1d.cuh:248](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L248)（math 线程组）各有一个 `while (scheduler.get_next_block(...))`。注意两套线程组**共享同一个 `scheduler` 对象**、各自独立调 `get_next_block`，于是 TMA 与 math 会对齐到**同一个** `(m_block, n_block)`（它们各自维护自己的 `current_iter`，但起始一致、退出条件一致）。

#### 4.1.4 其它 GemmType 分支速览（选读）

`get_next_block` 用 `if constexpr` 为四类分组场景写了不同的领号/退出逻辑，核心差异在于「瓦片总数随分组动态变化」：

- **`MGroupedMasked`** [gemm.cuh:200-216](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L200-L216)：解码阶段每 expert 的有效行数（`masked_m`）运行时才知，故用 `while` 累加 `current_m_cumsum`，逐 expert 把 `num_m_blocks` 算出来再判断 `next_block_idx` 落在哪个 expert。
- **`MGroupedContiguousWithPsumLayout`** [gemm.cuh:217-237](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L217-L237)：SM100 的 psum 布局，每个 group 的 `num_m_blocks` 随 group 推进而变，靠 `current_m_block_cumsum` 累加。
- **K 轴分组（`is_k_grouped_contiguous`）** [gemm.cuh:238-261](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L238-L261)：用于 MoE 权重梯度（wgrad），K 轴变长。每个 group 的 `num_blocks` 固定（M×N），但 group 数 `kNumGroups` 决定总任务，靠 `current_num_valid_groups` 累加，并用 `get_next_k_group` / `get_next_psum_k_group` 跳过空 group。

这三类都会在拿到线性号后，**减去所属 group 的累计偏移**再调同一个 `get_swizzled_block_idx`——也就是说 4.2 的 swizzle 逻辑是所有 `GemmType` 共用的。

#### 4.1.5 代码实践

**实践目标**：手算一个 Normal GEMM 的 `num_m_blocks` / `num_n_blocks` / `num_blocks` / `num_waves`，验证「`current_iter` 即 wave 编号」。

**给定参数**：

| 参数 | 值 |
|------|----|
| `BLOCK_M`, `BLOCK_N`, `BLOCK_K` | 128, 128, 128 |
| `shape_m` × `shape_n` × `shape_k` | 3072 × 2048 × 4096 |
| `kNumSMs`（H100） | 132 |

**操作步骤**：

1. 算 `num_m_blocks = ceil(3072/128) = 24`、`num_n_blocks = ceil(2048/128) = 16`。
2. 算 `num_blocks = 24 × 16 = 384`。
3. 算 `num_waves = ceil(384/132) = 3`，`last_wave_util = 384 mod 132 = 120`。

**需要观察的现象**：第 0、1 波各 132 个 SM 满载（共领走 264 块），第 2 波只有 120 个 SM 有活（领走 384−264=120 块），其余 12 个 SM 直接退出循环。

**预期结果**：

| wave (`current_iter`) | 本波领走的 `next_block_idx` 区间 | 活跃 SM 数 |
|---|---|---|
| 0 | [0, 131] | 132 |
| 1 | [132, 263] | 132 |
| 2 | [264, 383] | 120 |

> ⚠️ 本实践为**纸笔推演型**，无需 GPU。若想验证，可在 SM90 机器上设 `DG_PRINT_CONFIGS=1`（见 u10-l4）查看启发式选出的 `BLOCK_M/N/K` 与 `num_sms`，再代入上表。

#### 4.1.6 小练习与答案

**练习 1**：为什么 `current_iter` 初值是 `-1` 而不是 `0`？

> **答**：因为领号公式是 `(++current_iter) * kNumSMs + blockIdx.x`，用的是**前置自增**。初值 `-1`，第一次调用先自增到 `0`，于是第 0 波领到 `0*132 + blockIdx.x = blockIdx.x ∈ [0,131]`，正好对应第 0 个 wave。若初值为 `0`，第一波就会跳到 `[132, 263]`，漏掉前 132 块。

**练习 2**：如果 `num_blocks` 恰好是 `kNumSMs` 的整数倍，`last_wave_util` 是多少？这对性能意味着什么？

> **答**：此时 `num_blocks % kNumSMs == 0`，`last_wave_util = kNumSMs`（最后一波也满载），尾波无浪费。这正是启发式 [sm90.hpp:208](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L208) 里 `num_last_blocks == 0 ? desc.num_sms : num_last_blocks` 想要的理想情形——`wave_efficiency` 更高、`num_cycles` 更低。

---

### 4.2 L2 swizzle

#### 4.2.1 概念说明

领到线性瓦片号 `block_idx` 后，朴素做法是按行主序拆成 `(m, n)`：\( m = \lfloor b / \text{num\_n\_blocks}\rfloor,\ n = b \bmod \text{num\_n\_blocks} \)。但这样**同一波里相邻 CTA**（`block_idx` 连续）会落在**不同的 n 列**，各自加载不同的 B 瓦片，L2 毫无复用。

`get_swizzled_block_idx` 做的是**L2 友好的块序重排**：把瓦片按「组」组织，让**同一组内连续的若干瓦片共享同一个 B（或 A）瓦片**。这样同一波里相邻 CTA 共享数据，一块 B 瓦片被一个 CTA 加载进 L2 后，同组的其他 CTA 直接命中，省下大量全局→L2 带宽。

两个关键设计：

1. **主轴 / 辅轴**：由 `kIsMulticastOnA` 决定哪条轴是「组内快速变化的主轴」、哪条是「组间慢速变化的辅轴」。多播/共享 B 时，主轴是 M（连续瓦片变 m、共享 n）；多播/共享 A 时反之。
2. **组大小 `kNum1DBlocksPerGroup`**：每组沿主轴含多少瓦片（8 或 16），由 `get_num_1d_blocks_per_group` 在编译期按「最小化 L2 工作集」选出。

#### 4.2.2 核心流程

记主轴块数 \( P \)、辅轴块数 \( S \)、组大小 \( G \)（= `kNum1DBlocksPerGroup`）。一组覆盖的瓦片数为：

\[
B_g = S \cdot G
\]

对线性号 \( b \)，先定位组与组内偏移：

\[
\text{group\_idx} = \left\lfloor b / B_g \right\rfloor,\quad
\text{first} = \text{group\_idx} \cdot G,\quad
\text{in\_group} = b \bmod B_g
\]

再算本组沿主轴实际有的块数（最后一段可能不足 \( G \)）：

\[
g = \min(G,\ P - \text{first})
\]

最后拆成 `(m, n)`（以 `kIsMulticastOnA == false`、即在 M 轴分组为例）：

\[
m = \text{first} + (\text{in\_group} \bmod g),\qquad
n = \text{in\_group} / g
\]

效果：连续的 \( g \) 个 `in_group` 值对应**同一个 n**（共享 B 瓦片）、m 从 `first` 开始递增。当 `kIsMulticastOnA == true` 时主/辅轴互换，m 与 n 的公式对调（连续瓦片共享 A 瓦片）。

> 直觉：**让「同一波里相邻 CTA」共享一个瓦片列**。朴素行主序让相邻 CTA 跑不同列（B 瓦片各不相同）；swizzle 让相邻 CTA 跑同一列（B 瓦片复用 8 倍）。这正是「L2 友好」的来源。

#### 4.2.3 源码精读

**① 组大小选择：最小化 L2 工作集**

[scheduler/gemm.cuh:14-26](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L14-L26) —— `get_num_1d_blocks_per_group` 在 `{8, 16}` 里挑使「usage」最小的候选。usage 是 L2 工作集的近似（以在 M 轴分组为例）：

```cpp
const auto usage = kIsMulticastOnA ?
    candidate * BLOCK_N + math::constexpr_ceil_div(kNumSMs, candidate) * BLOCK_M :  // 分组在 N
    candidate * BLOCK_M + math::constexpr_ceil_div(kNumSMs, candidate) * BLOCK_N;   // 分组在 M
```

含义：一项 `candidate * BLOCK_M` 是一组内 M 轴数据（A 瓦片的工作集），另一项 `ceil_div(kNumSMs, candidate) * BLOCK_N` 是「一个 wave 覆盖多少个不同的 n 列」乘以每列 B 瓦片大小（B 瓦片的工作集）。候选越小则后一项越大、候选越大则前一项越大，取折中。

**② swizzle 主体**

[scheduler/gemm.cuh:117-153](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L117-L153) —— 严格对应 4.2.2 的公式：

```cpp
const auto primary_num_blocks   = kIsMulticastOnA ? num_n_blocks : num_m_blocks;  // P
const auto secondary_num_blocks = kIsMulticastOnA ? num_m_blocks : num_n_blocks;  // S
const auto num_blocks_per_group = secondary_num_blocks * kNum1DBlocksPerGroup;     // B_g
const auto group_idx   = block_idx / num_blocks_per_group;
auto first_block_idx   = group_idx * kNum1DBlocksPerGroup;                         // first
auto in_group_idx      = block_idx % num_blocks_per_group;                         // in_group
num_blocks_in_group    = min(kNum1DBlocksPerGroup, primary_num_blocks - first_block_idx);  // g
// ...（multicast 对齐修正，见 4.3）...
if constexpr (kIsMulticastOnA) {
    m_block_idx = in_group_idx / num_blocks_in_group;
    n_block_idx = first_block_idx + in_group_idx % num_blocks_in_group;
} else {
    m_block_idx = first_block_idx + in_group_idx % num_blocks_in_group;            // m = first + in%g
    n_block_idx = in_group_idx / num_blocks_in_group;                              // n = in/g
}
```

注意 `num_blocks_in_group`（= \( g \)）既是拆分用的除数，又会被 4.3 的 multicast 判定复用。

**③ 静态断言锁死组大小与多播数整除**

[scheduler/gemm.cuh:118](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L118) —— `DG_STATIC_ASSERT(kNum1DBlocksPerGroup % kNumMulticast == 0, ...)`：组大小必须是多播数的整数倍，否则多播配对会跨组错位。由于候选只有 8/16、多播数只有 1/2，恒满足。

#### 4.2.4 代码实践

**实践目标**：对 4.1.5 的同一个 Normal GEMM（`num_m_blocks=24, num_n_blocks=16`），假设 `kIsMulticastOnA=false`（在 M 轴分组）、`kNum1DBlocksPerGroup=8`，推演 `get_swizzled_block_idx` 对前若干 `block_idx` 给出的 `(m_block, n_block)`，并与朴素行主序对比。

**操作步骤**：

1. 先算 \( P=24,\ S=16,\ G=8,\ B_g = 16\times 8 = 128 \)。
2. 逐个代入 4.2.2 公式，填下表。

**需要观察的现象**：连续 8 个 `block_idx` 共享同一个 `n`（B 瓦片），`m` 从 `first` 起递增；跨过 `B_g=128` 边界进入下一组，`first` 跳到 8。

**预期结果**：

| `block_idx` | group | first | in_group | \(g\) | `m_block` | `n_block` |
|---|---|---|---|---|---|---|
| 0 | 0 | 0 | 0 | 8 | 0 | 0 |
| 1 | 0 | 0 | 1 | 8 | 1 | 0 |
| 7 | 0 | 0 | 7 | 8 | 7 | 0 |
| 8 | 0 | 0 | 8 | 8 | 0 | 1 |
| 15 | 0 | 0 | 15 | 8 | 7 | 1 |
| 127 | 0 | 0 | 127 | 8 | 7 | 15 |
| 128 | 1 | 8 | 0 | 8 | 8 | 0 |
| 135 | 1 | 8 | 7 | 8 | 15 | 0 |

对比朴素行主序（\( m=\lfloor b/16\rfloor,\ n=b\bmod 16 \)）：`block_idx` 0,1,2,…,15 会落在 `(0,0),(0,1),…,(0,15)`——同一行、n 全变，相邻 CTA 的 B 瓦片全不同，无 L2 复用。swizzle 把它们重排成 `(0,0),(1,0),…,(7,0),(0,1),…`——相邻 8 个 CTA 共享 `n=0` 的 B 瓦片，命中率约提升 8 倍。

> ⚠️ 本实践为**纸笔推演型**。`kNum1DBlocksPerGroup` 的实际取值由 4.2.3① 的 `get_num_1d_blocks_per_group` 决定（依赖 `BLOCK_M/N` 与 `kNumSMs`），这里假设为 8 仅为演算方便；若你的硬件/形状使它取 16，把表里的 8 换成 16 重算即可。

#### 4.2.5 小练习与答案

**练习 1**：把 `kIsMulticastOnA` 从 `false` 改成 `true`，4.2.4 表格会怎么变？共享的是 A 还是 B？

> **答**：主辅轴互换：\( P=16 \)（n）、\( S=24 \)（m）、\( B_g = 24\times 8 = 192 \)。拆分公式对调为 `n = first + in%g`、`m = in/g`。于是连续 8 个瓦片共享**同一个 m**（A 瓦片）、n 从 `first` 递增——共享的是 **A**。这与「multicast on A = 多个 CTA 共享 A 瓦片」一致。

**练习 2**：为什么 `get_num_1d_blocks_per_group` 只在 `{8, 16}` 里选，而不选更大的值（比如 32）？

> **答**：组越大，一组内 M 轴数据工作集（`candidate * BLOCK_M`）越大，可能超出 L2 容量反而降低命中率；组太小则一个 wave 覆盖的不同列太多（`ceil_div(kNumSMs, candidate) * BLOCK_N` 大），B 工作集大。8/16 是经验上 L2 友好的甜点区，再大会让单组工作集过大。

---

### 4.3 multicast 判定

#### 4.3.1 概念说明

multicast（2-CTA 多播）要求「配对的两个 CTA 共享同一块瓦片」。这带来三处对齐/合法性判定，分别管不同的边界：

| 判定 | 位置 | 回答的问题 |
|------|------|-----------|
| `is_peer_cta_alive` | `get_next_block` Normal 分支 | 我的配对 CTA（peer）在不在界内？发给它的多播加载要不要等？ |
| 奇偶对齐修正 | `get_swizzled_block_idx` | 本组块数是奇数时，多播配不齐怎么办？（**仅 SM90**） |
| `is_tma_multicast_valid` | 独立方法 | 这块该不该发多播？（分组场景下两 CTA 是否属同一 expert） |

关键架构差异（也是本模块的核心结论）：

> **SM90 的 TMA multicast 可以逐 CTA 动态关闭**（`num_tma_multicast=1`），所以调度器能在运行时「遇到奇数尾巴就关掉多播」；**SM100 用固定的 2-CTA cluster 模型，无法动态关闭**，必须在宿主启发式里静态保证对齐，否则直接拒绝该候选。

#### 4.3.2 核心流程

**① `is_peer_cta_alive`（peer 是否在界）**

2-CTA 多播把 `block_idx` 与 `block_idx ^ 1` 配对（偶数与下一个奇数）。若整个网格在某轴上对 `kNumMulticast` 整除，则 peer 永远在界内（常量旁路）；否则需逐块判断 peer 是否 `< num_blocks`：

\[
\text{is\_peer\_alive} = \big(\text{num\_n\_blocks} \bmod k\;=\;0\big)\ \lor\ \big(\text{num\_m\_blocks} \bmod k\;=\;0\big)\ \lor\ \big((\text{idx}\oplus 1) < \text{num\_blocks}\big)
\]

其中 \( k = \text{kNumMulticast} \)。前两项是「整轴对齐」的常量旁路，第三项是逐块判定。

**② 奇偶对齐修正（仅 SM90）**

若本组实际块数 \( g \) 为奇数，多播配不齐。SM90 把它拆成「主体偶数段 + 尾部单块」：

\[
g' = g \oplus 1 \quad(\text{奇数 } g \text{ 向下取偶})
\]

- 落在主体段的瓦片（`in_group < g' * S`）：用 `g'`（偶数，正常多播）。
- 落在尾部段的瓦片：`num_blocks_in_group = 1`（这块**不多播**，单 CTA 自加载）。

**③ `is_tma_multicast_valid`（分组合法性）**

普通/掩码/k 分组/Batched/psum 布局都直接 `return true`（瓦片天然同质）。唯独 `MGroupedContiguous` 在多播 B 时需检查：配对的两 M 瓦片是否属**同一个 expert**（`grouped_layout` 里 `group_idx` 相等），否则两 CTA 要的是不同 B 瓦片、多播非法。

#### 4.3.3 源码精读

**① `is_peer_cta_alive` 的三条析取**

[scheduler/gemm.cuh:281-283](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L281-L283)（注释点明「masked 分组必然对齐，无需设此位」）：

```cpp
is_peer_cta_alive = num_n_blocks % kNumMulticast == 0 or   // N 轴整除：常量旁路
                    num_m_blocks % kNumMulticast == 0 or   // M 轴整除：常量旁路
                    (next_block_idx ^ 1) < num_blocks;     // peer 在界内
```

它的真实消费者在 math 侧 [sm90_fp8_gemm_1d1d.cuh:262](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L262)：决定 `empty_barrier` 的 arrive 目标——peer 活着就发给配对 CTA，否则发给自己所在 cluster rank：

```cpp
auto target_cta = scheduler.is_peer_cta_alive ? lane_idx : cute::block_rank_in_cluster();
```

**② 奇偶对齐修正（`#if __CUDA_ARCH__ < 1000` 锁死 SM90）**

[scheduler/gemm.cuh:129-142](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L129-L142) —— 注释明确「SM90 可动态关多播，SM100 用 2-CTA 不能动态关」：

```cpp
// NOTES: for SM90 only, as SM90 can dynamically disable TMA multicast
// while SM100 uses 2-CTA, which can not be dynamically disabled
#if __CUDA_ARCH__ < 1000
    if (kNumMulticast > 1 and num_blocks_in_group % 2 != 0) {
        if (in_group_idx < (num_blocks_in_group ^ 1) * secondary_num_blocks) {
            num_blocks_in_group = num_blocks_in_group ^ 1;   // 主体段用偶数 g'
        } else {
            in_group_idx      -= (num_blocks_in_group ^ 1) * secondary_num_blocks;
            first_block_idx   += num_blocks_in_group ^ 1;
            num_blocks_in_group = 1;                          // 尾部单块不多播
        }
    }
#endif
```

例如 `num_blocks_in_group = 7`（奇）：\( 7\oplus 1 = 6 \)。前 6 块（×辅轴块数）正常多播，第 7 块单独走、`num_blocks_in_group=1`，于是下游 `is_tma_multicast_valid` 见到 `num_blocks_in_group==1` 就返回 `false`（见 ③）。

**③ `is_tma_multicast_valid`**

[scheduler/gemm.cuh:290-307](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L290-L307) —— 先用「本组单块」短路，再按 `GemmType` 分流；`MGroupedContiguous` 且多播 B 时查两 M 瓦片是否同 expert：

```cpp
if (num_blocks_in_group == 1)
    return false;                                   // 单块组：无 peer，不多播
if constexpr (Normal or Masked or k_grouped or Batched or ...PsumLayout) {
    return true;
} else {  // MGroupedContiguous
    if constexpr (kIsMulticastOnA) {
        return true;
    } else {
        const auto group_idx       = grouped_layout[m_block_idx * BLOCK_M];
        const auto peer_group_idx  = grouped_layout[(m_block_idx ^ 1) * BLOCK_M];
        return group_idx == peer_group_idx;          // 配对两块须属同一 expert
    }
}
```

它的真实消费者在 TMA 侧 [sm90_fp8_gemm_1d1d.cuh:182-184](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L182-L184)：把「是否多播」折算成 A、B 各自的多播份数：

```cpp
const bool is_tma_multicast_valid = scheduler.is_tma_multicast_valid(m_block_idx);
const uint32_t num_tma_multicast_a = (kIsTMAMulticastOnA     and is_tma_multicast_valid) ? kNumTMAMulticast : 1;
const uint32_t num_tma_multicast_b = (not kIsTMAMulticastOnA and is_tma_multicast_valid) ? kNumTMAMulticast : 1;
DG_STATIC_ASSERT(kNumTMAMulticast <= 2, "Scheduler does not support > 2 TMA multicast");
```

这个 `1`/`kNumTMAMulticast` 最终传给 `tma::copy` 的多播份数参数（[sm90_fp8_gemm_1d1d.cuh:221-224](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L221-L224)），实现 SM90 的「逐瓦片动态开/关多播」。

**④ SM100 的静态对齐（对照）**

SM100 不能动态关多播，故在宿主启发式里**静态拒绝**不对齐的候选：[`sm90.hpp:89-91`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L89-L91) 对 masked/psum 布局要求 `ceil_div(n, block_n) % (cluster_m * cluster_n) == 0`；[`sm90.hpp:63-67`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L63-L67) 还对 `KGroupedContiguous`（group 数 > 4）与 `Batched` 直接 `disable_multicast`。两端互补：**SM90 容忍运行时奇偶尾巴，SM100 在编译期就规避**。

#### 4.3.4 代码实践

**实践目标**：用一个「辅轴不对齐」的小例子，验证 SM90 奇偶对齐修正的拆分行为，并解释 SM100 为何不能这么做。

**给定参数**：`num_m_blocks = 7`（奇，假设这是某组沿主轴的实际块数，即 `num_blocks_in_group` 初始为 7）、`kNumMulticast = 2`、`secondary_num_blocks = 4`。

**操作步骤**：

1. 代入 4.3.2②：\( g=7,\ g'=7\oplus1=6 \)。
2. 主体段覆盖 `in_group < 6*4 = 24` 的瓦片，这些用 `g'=6` 正常多播；尾部段 `in_group ∈ [24, 27]`（共 3 个瓦片，对应主轴第 6 块 × 4 个辅轴）走 `num_blocks_in_group=1` 不多播。
3. 对照：若这是 SM100，`#if __CUDA_ARCH__ < 1000` 整段被编译掉，奇数 `g` 会直接让多播配对错位——所以 SM100 必须靠启发式 `ceil_div(n,block_n) % cluster == 0` 在更上游拒绝此类形状。

**需要观察的现象**：SM90 上，`num_blocks_in_group` 在同一次 `get_swizzled_block_idx` 调用内会从 7「修正」成 6 或 1，取决于 `in_group_idx` 落在主体段还是尾部段；下游 `is_tma_multicast_valid` 对尾部段（`num_blocks_in_group==1`）返回 `false`，自动关闭该瓦片的多播。

**预期结果**：你能说清楚「SM90 靠运行时动态修正 + `num_blocks_in_group==1` 短路关多播；SM100 靠宿主静态整除校验」这对互补设计。本实践为源码阅读 + 纸笔推演型。

#### 4.3.5 小练习与答案

**练习 1**：`is_peer_cta_alive` 前两个析取项（`num_n_blocks % kNumMulticast == 0` 与 `num_m_blocks % kNumMulticast == 0`）为什么叫「常量旁路（constant bypass）」？

> **答**：因为 `num_n_blocks`、`num_m_blocks` 在构造函数里就定死了，这两个整除判断对整个 kernel 是**编译期/常量**结果。一旦某轴对 `kNumMulticast` 整除，则所有瓦片的 peer 都必然在界内，无需逐块算 `(idx^1) < num_blocks`——编译器可把逐块判定旁路掉，省掉每块的运算。这正是注释「Always aligned on N/M (constant bypass)」的含义。

**练习 2**：为什么 `is_tma_multicast_valid` 里 `MGroupedContiguous` 在多播 B（`kIsMulticastOnA=false`）时要查两 M 瓦片同 expert，而多播 A（`kIsMulticastOnA=true`）时直接 `return true`？

> **答**：多播 B 时，配对的两 CTA **共享同一块 B 瓦片**、各算不同的 M 行——若这两行分属不同 expert，它们的 A 瓦片不同没问题（A 各自加载），但共享的 B 必须对两 expert 都有效；MoE 里每个 expert 的权重 B 不同，故必须同 expert 才能共享 B。多播 A 时共享的是 A 瓦片（输入 token），不同 CTA 算不同 N 列（不同 B），A 对所有 N 列都一样，故无需检查。本练习预告了 u7（分组 GEMM/MoE）的 expert 边界问题。

---

## 5. 综合实践

**综合实践目标**：把三个最小模块串起来，完整推演一次 Normal SM90 GEMM 在「带 multicast」配置下的调度过程，并解释调度器如何同时服务「负载均衡」「L2 复用」「多播对齐」三个目标。

**任务步骤**：

1. **设定参数**。沿用 4.1.5 / 4.2.4 的形状：`BLOCK_M=BLOCK_N=128`，`shape_m=3072, shape_n=2048`，故 `num_m_blocks=24, num_n_blocks=16, num_blocks=384`；`kNumSMs=132`；再假设开启 2-CTA multicast 且 `kIsMulticastOnA=false`（多播 B，在 M 轴分组），`kNum1DBlocksPerGroup=8`。
2. **算 wave 与尾波**（4.1）。写出 `num_waves=3`、`last_wave_util=120`，标出每波各 CTA 领走的 `next_block_idx` 区间。
3. **推 swizzle 序列**（4.2）。对 `next_block_idx ∈ {0,1,7,8,128,135}`，按 4.2.4 的方法填出 `(m_block, n_block)`，并指出哪几个瓦片**共享同一块 B 瓦片**（即 `n_block` 相同）。
4. **判 multicast 合法性**（4.3）。对上述每个瓦片：
   - 判断它是否落在「主轴块数对 2 取余为奇」的组里（本例 `num_m_blocks=24`、`g=min(8,24-first)`，各组 `g=8` 均偶，故**不触发**奇偶修正——请说明这一点）；
   - 调 `is_tma_multicast_valid`：Normal 类型返回 `true`（只要 `num_blocks_in_group != 1`）；
   - 调 `is_peer_cta_alive`：因 `num_m_blocks=24` 对 2 整除，命中常量旁路，恒为 `true`。
5. **画一张「wave × CTA → (m,n)」表**，在第 0 波里圈出「共享 B 瓦片」的 CTA 组（相邻 8 个 CTA 同 `n`），直观感受 L2 复用。

**预期结果**：

- 一张清晰的 wave/CTA/(m,n) 对照表，体现「`current_iter` = wave」「每波相邻 CTA 共享 B 瓦片」。
- 结论：调度器用 `current_iter*kNumSMs+blockIdx.x` 实现**负载均衡**（持久化、尾波可控）；用 `get_swizzled_block_idx` 实现**L2 复用**（相邻 CTA 同 n）；用 `is_peer_cta_alive` / 奇偶修正 / `is_tma_multicast_valid` 实现**多播对齐**（SM90 动态、逐瓦片）。
- 边界判断：本例因 `num_m_blocks` 对 2 整除且各组 `g` 为偶，奇偶修正与 `is_peer_cta_alive` 的逐块分支均不触发——这正好印证「对齐的形状最省调度开销」，也是宿主启发式偏爱整除候选的原因。

> ⚠️ 本综合实践为**纸笔推演 + 源码阅读型**，无需 GPU。若要实测，可在 SM90 上设 `DG_JIT_DUMP_SASS=1` 与 `DG_PRINT_CONFIGS=1`（见 u10-l4），对照选出的 `BLOCK_M/N/K`、`cluster` 与 `num_sms` 重算上表。

---

## 6. 本讲小结

- **持久化调度与 wave**：`get_next_block` 用 `next_block_idx = (++current_iter) * kNumSMs + blockIdx.x` 让每个 SM 在 `while` 循环里反复领瓦片，`current_iter` 即 wave 编号；瓦片数 \( B = \text{num\_m\_blocks}\times\text{num\_n\_blocks} \)，波数 \( \lceil B/S\rceil \)、尾波利用率决定性能。
- **L2 swizzle**：`get_swizzled_block_idx` 把线性瓦片号按「组」重排，让同一波里相邻 CTA 共享同一块 B（或 A）瓦片，组大小 `kNum1DBlocksPerGroup`（8/16）由 `get_num_1d_blocks_per_group` 最小化 L2 工作集选出；分组方向由 `kIsMulticastOnA` 决定，与多播方向一致。
- **multicast 判定三处**：`is_peer_cta_alive`（peer 是否在界，含整除常量旁路）、`get_swizzled_block_idx` 内的奇偶对齐修正（奇数尾巴拆成偶数主体+单块尾）、`is_tma_multicast_valid`（分组场景两 CTA 须同 expert）。
- **SM90 vs SM100 的架构分水岭**：SM90 的 TMA multicast 可逐 CTA 动态关闭（`num_tma_multicast=1`），故调度器运行时修奇偶尾巴；SM100 用固定 2-CTA cluster 不能动态关，必须在宿主启发式静态保证整除（`ceil_div(n,block_n) % cluster == 0`），否则拒绝候选。
- **跨层衔接**：本讲补全了 u6-l1 里 `while (scheduler.get_next_block(...))` 背后的「瓦片从哪来、按什么序、是否多播」全部逻辑；`num_waves`/`last_wave_util` 又与 u5-l2 的宿主成本模型对齐。调度器的 `GemmType` 分支（masked/k-grouped）预告了 u7（分组 GEMM/MoE），multicast 的 expert 边界检查预告了 u7-l1/u7-l2 的 expert 对齐问题。

---

## 7. 下一步学习建议

1. **进入分组 GEMM（MoE）**：本讲的 `MGroupedMasked` / K 轴分组分支只是点到为止，建议进入 u7-l1（连续布局的 M 轴分组 GEMM）与 u7-l2（掩码分组 GEMM），看 `grouped_layout`、`masked_m`、`expected_m` 如何与调度器的动态累加（`current_m_cumsum` / `current_num_valid_groups`）配合。
2. **回看启发式选布局**：带着本讲的 wave/swizzle/multicast 直觉重读 u5-l2（布局候选与最优配置选择），理解宿主为何用 `num_waves`、`last_wave_util`、整除约束筛选候选——它选出的 `BLOCK_M/N/K` 与 `cluster` 正是本讲调度器的全部编译期输入。
3. **对照 SM100 设备 kernel**：阅读 `sm100_fp8_fp4_gemm_1d1d.cuh`，确认 SM100 用的 2-CTA cluster 模型下，调度器不再有奇偶修正（`#if __CUDA_ARCH__ < 1000` 段被编译掉），对齐完全由宿主静态保证。
4. **动手验证（可选）**：若手头有 SM90 机器，设 `DG_JIT_DUMP_SASS=1`（u10-l4）dump 一个 multicast kernel，结合 `ncu` 观察 L2 命中率，对比「开 swizzle」与「关 multicast」两种配置下 B 瓦片的加载次数差异，直观验证 4.2 的 L2 复用结论。
