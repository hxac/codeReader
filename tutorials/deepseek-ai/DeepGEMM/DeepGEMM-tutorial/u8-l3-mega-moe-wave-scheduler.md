# Mega MoE 调度器与 wave 调度

## 1. 本讲目标

本讲深入到 Mega MoE 单个 mega-kernel 的「大脑」——设备侧调度器 `MegaMoEScheduler`。它是把 EP dispatch、Linear1、SwiGLU、Linear2、EP combine 这五步融合进一个 kernel 后，决定「每个 CTA 在每一刻该为哪个 expert、算哪一块、处于 L1 还是 L2 阶段」的核心组件。

学完后你应当能够：

1. 画出 `BlockPhase`（Linear1 ↔ Linear2）两阶段状态机，并解释它如何在一个 wave 内先算完所有 expert 的 L1、再回头算 L2。
2. 说清「持久化 CTA + wave + pool 偏移」这套调度模型，以及 `kNumExpertsPerWave` 如何决定一个 wave 处理几个 expert。
3. 读懂 `stored_num_tokens_per_expert` 这张 warp 分布式缓存的 lane 级布局，以及 `get_num_tokens` / `get_pool_block_offset` 如何用 shuffle/reduce 从中取数。
4. 从数学上推出为何 SM 数与 N block 计数都必须是偶数——这是 2-CTA cluster 能正确 multicast 的硬约束。

本讲承接 u8-l1（Mega MoE 概念与对称内存）与 u6-l4（分块调度与持久化 CTA）。建议先建立「mega-kernel 融合了什么」与「持久化调度 + L2 swizzle」的认知再进入本讲。

## 2. 前置知识

- **持久化调度（persistent scheduling）**：u6-l4 已讲过，launch 恰好 `kNumSMs` 个 CTA，每个 CTA 在 `while` 循环里反复领瓦片（`block_idx += kNumSMs`），而不是 launch 一大把一次性瓦片。Mega MoE 沿用这一模型。
- **CTA 与 cluster**：CTA 是 GPU 的一次线程块启动；SM100 上多个 CTA 可组成一个 cluster，cluster 内 CTA 共享分布式共享内存并能做 TMA multicast（一次加载广播给簇内所有 CTA）。Mega MoE 用的是 **2-CTA cluster**。
- **warp 与 lane**：一个 warp 有 32 个 lane（线程）。本讲会用 warp 的 32 个 lane 当「分布式的寄存器缓存」。
- **wave**：把全部 expert 切成若干「波」，每波处理 `kNumExpertsPerWave` 个 expert；同一波内先做完所有 expert 的 L1，再做 L2。
- **pool / ring buffer**：u8-l1 讲过的对称 ring buffer，所有 expert 的 token 拼成一个按 M-block 计数的环形池子。每个 expert 在池子里占一段连续的 M-block。
- **MMA、UMMA、multicast**：参见 u6-l2。本讲只需知道「两个 CTA 共享同一 A（token）瓦片」是靠 multicast 实现的，这是偶数约束的物理动机。

> 术语速查：`BLOCK_M/N/K` 是单个瓦片的 M/N/K 尺寸；`L1_SHAPE_N/K` 是 Linear1 的输出/归约维（`intermediate_hidden*2` / `hidden`），`L2_SHAPE_N/K` 是 Linear2 的（`hidden` / `intermediate_hidden`）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [scheduler/mega_moe.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh) | **本讲主角**。`MegaMoEScheduler` 模板结构体：`BlockPhase` 状态机、wave/expert 调度、lane 级 token 缓存、偶数约束断言。 |
| [impls/sm100_fp8_fp4_mega_moe.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh) | 设备 mega-kernel。本讲关注它如何**实例化**调度器、如何用 `for_each_block` 把每种线程的循环挂进调度器。 |
| [csrc/apis/mega.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp) | 宿主侧入口。校验 recipe、按 `arch_major==10` 派发到 SM100 实现。 |
| [csrc/jit_kernels/impls/sm100_fp8_fp4_mega_moe.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_fp8_fp4_mega_moe.hpp) | 宿主 Runtime：生成 kernel 源码、构造 TMA 描述符、用 `LaunchArgs(num_sms, …, 2)` 设定 2-CTA cluster。 |
| [csrc/jit_kernels/heuristics/mega_moe.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/mega_moe.hpp) | 启发式：计算 `num_experts_per_wave`、block 配置等，是 `kNumExpertsPerWave` 模板参数的来源。 |
| [layout/mega_moe.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/layout/mega_moe.cuh) | `Workspace` 结构：提供 `get_expert_recv_count_sum_ptr` 等地址计算，是调度器 token 缓存的数据来源。 |
| [ptx/utils.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/utils.cuh) | `get_lane_idx()`、`exchange()`（基于 `__shfl_sync`）等内联封装，lane 级缓存取数时用到。 |

---

## 4. 核心概念与源码讲解

### 4.1 BlockPhase 状态机：一个 wave 内先 L1 后 L2

#### 4.1.1 概念说明

普通分组 GEMM（u7）里，每个 expert 算的是一次完整的 `D = A @ B`。而 Mega MoE 把两层 MLP 融在一起：Linear1（FP8×FP4，输出 `intermediate_hidden*2`）、SwiGLU 激活、Linear2（FP8×FP4，输出 `hidden`）。对调度器而言，「算一块」就有了两种含义：

- **Linear1 块**：读 L1 激活（token）、算 L1 GEMM，产出中间结果。
- **Linear2 块**：读 L2 激活（SwiGLU 之后的中间结果）、算 L2 GEMM，产出最终输出。

`BlockPhase` 就是用来标记「当前这块属于哪个阶段」的枚举：

```cpp
enum class BlockPhase {
    None = 0,
    Linear1 = 1,
    Linear2 = 2
};
```

见 [mega_moe.cuh:13-17](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L13-L17)。`None` 表示「没有更多块了」，是调度结束哨兵。

关键设计：调度器**不会**把单个 expert 的 L1 和 L2 紧挨着算（那需要 L2 的输入——即 L1 的全量输出——立即可用，会破坏流水线）。它的策略是 **wave 级的两阶段**：一个 wave 内，先把该 wave 涉及的全部 expert 的 L1 全算完，再切回这些 expert 算 L2。这样 L1 的全部产出有时间被 SwiGLU 处理并落进 ring buffer，L2 阶段再消费。

#### 4.1.2 核心流程

状态机由一个成员 `next_phase`（初值 `Linear1`）驱动，核心循环在 `get_next_block`：

```
循环：
  若所有 expert 已处理完 → 返回 (None, …)
  若 next_phase == Linear1:
      若当前 wave 的 L1 还有块可领 → 返回 (Linear1, expert, m, n)
      否则 → 切换 next_phase = Linear2，回到 wave 头一个 expert
  否则 (next_phase == Linear2):
      若当前 wave 的 L2 还有块可领 → 返回 (Linear2, expert, m, n)
      否则 → 切换 next_phase = Linear1（进入下一个 wave）
```

一次 `for_each_block` 的调用轨迹大致是：

```
wave0: L1(expert0), L1(expert1), …, L1(expert_{kNumExpertsPerWave-1})
       → L2(expert0), L2(expert1), …, L2(expert_{kNumExpertsPerWave-1})
wave1: L1(expert_k), … → L2(expert_k), …
… 直到所有 expert 的 L1+L2 全部派发完，返回 None
```

注意 Linear1 与 Linear2 的 N block 数不同（`kNumL1BlockNs = L1_SHAPE_N/BLOCK_N`，`kNumL2BlockNs = L2_SHAPE_N/BLOCK_N`），所以同一 expert 在两个阶段的瓦片总数不一样，调度器对两个阶段分别有 `fetch_next_l1_block` / `fetch_next_l2_block`。

#### 4.1.3 源码精读

状态机的「心脏」是 `get_next_block`，见 [mega_moe.cuh:150-183](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L150-L183)。摘关键分支：

```cpp
if (next_phase == BlockPhase::Linear1) {
    if (fetch_next_l1_block()) {
        n_block_idx = block_idx - m_block_idx * kNumL1BlockNs;
        block_idx += kNumSMs;                       // 持久化：本 CTA 跳到下一波同位瓦片
        return {BlockPhase::Linear1, current_local_expert_idx, m_block_idx, n_block_idx};
    } else {
        // 当前 wave 的 L1 完成，转 L2；回到 wave 的起始 expert
        next_phase = BlockPhase::Linear2;
        set_expert_idx(math::align<uint32_t, false>(current_local_expert_idx - 1, kNumExpertsPerWave));
    }
} else {
    if (fetch_next_l2_block()) { /* … 同理返回 Linear2 … */ }
    else {
        next_phase = BlockPhase::Linear1;           // L2 也完成，进入下一个 wave 的 L1
    }
}
```

要点：

- `block_idx += kNumSMs` 是 u6-l4 讲过的持久化调度：每个 CTA 处理完一块后，把游标向前推 `kNumSMs`，正好跳到「下一波里由同一个 CTA 负责的那块」，天然负载均衡。
- L1 → L2 切换时，用 `set_expert_idx(align_down(current_local_expert_idx - 1, kNumExpertsPerWave))` 把 expert 游标**回退到本 wave 的起点**。`align<…, false>` 是向下对齐（`math::align` 的 `kDoCeilAlignment=false` 分支，见 [math.cuh:26-29](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/common/math.cuh#L26-L29)）。`current_local_expert_idx - 1` 是 L1 刚处理到的 expert，对齐到 wave 边界即得到本 wave 第一个 expert，L2 阶段从这里重新扫。
- L2 → L1 切换时**不**回退 expert（`else { next_phase = Linear1; }`），因为下一步循环里 `fetch_next_l1_block` 会自然推进到下一个 wave 的 expert 区间——`get_wave_expert_end_idx` 会重新按 `current_local_expert_idx` 计算新 wave 的上界。

`fetch_next_l1_block` 与 `fetch_next_l2_block`（[mega_moe.cuh:118-147](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L118-L147)）结构相同，区别只在用 `kNumL1BlockNs` 还是 `kNumL2BlockNs`。以 L1 为例：

```cpp
while (current_local_expert_idx < wave_end_expert_idx) {
    const auto num_m_blocks = get_current_num_m_blocks();   // ⌈tokens/BLOCK_M⌉
    m_block_idx = block_idx / kNumL1BlockNs;                // 行
    if (m_block_idx < num_m_blocks) return true;            // 本 expert 还有行可算
    block_idx -= num_m_blocks * kNumL1BlockNs;              // 跳过本 expert 的全部瓦片
    advance_expert_idx();                                   // 进入下一个 expert
}
```

这里 `block_idx` 是一个**跨越多个 expert 的全局线性游标**：在当前 expert 的 `[num_m_blocks × kNumL1BlockNs]` 瓦片网格里，`m_block_idx = block_idx / kNumL1BlockNs` 是行号，`n_block_idx = block_idx % kNumL1BlockNs` 是列号；当行号超过本 expert 的行数，就减去本 expert 的瓦片总数、推进到下一个 expert。

#### 4.1.4 代码实践

**目标**：用纸笔追踪一个微型配置下 `get_next_block` 的输出序列，验证「先 L1 后 L2、wave 切换」的行为。

**步骤**：

1. 打开 [mega_moe.cuh:150-183](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L150-L183) 与 [fetch_next_l1_block](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L118-L131)。
2. 取一组便于手算的参数（仅为理解，非真实可运行配置）：
   - `kNumExpertsPerRank = 2`，`kNumExpertsPerWave = 1`（即每波 1 个 expert，共 2 波）。
   - 假设 expert0 有 1 个 M-block、expert1 有 1 个 M-block；`kNumL1BlockNs = 2`、`kNumL2BlockNs = 2`；`kNumSMs = 4`。
3. 模拟 CTA `blockIdx.x = 0`（初始 `block_idx = 0`）连续调用 `get_next_block`，记录每次返回的 `(phase, expert, m, n)`。

**预期结果**（待手算确认）：

```
(Linear1, expert0, m=0, n=0)   # block_idx: 0 → 0+4=4，但专家0只有 1×2=2 块，0<2 命中
(Linear2, expert0, m=0, n=0)   # L1 完，切 L2；注意 block_idx 此时的演化需按代码精确推
(Linear1, expert1, m=0, n=…)
(Linear2, expert1, m=0, n=…)
(None, …)
```

> 注意：`block_idx += kNumSMs` 与跨 expert 的 `block_idx -= num_m_blocks*kNumL1BlockNs` 交织，精确数值要严格按代码逐步代入。本练习的重点是观察 **phase 序列一定是 L1,L2,L1,L2…**（每个 wave 先全 L1 后全 L2），而不是 L1,L1,L2,L2。若你算出的序列不符合这一规律，说明对 `set_expert_idx` 回退或 wave 边界理解有误。

4. **可观察现象**：把每次返回的 `block_phase` 连起来，应严格满足「同一 wave 内所有 Linear1 出现在所有 Linear2 之前」。

#### 4.1.5 小练习与答案

**Q1**：为什么不在单个 expert 内「算完 L1 立刻算 L2」？

**答**：L2 的输入是 L1 全部输出经 SwiGLU 处理后的中间结果。若紧挨着算，L1 的产出尚未写回 ring buffer、SwiGLU 尚未跑完，L2 无输入可读。wave 级两阶段（先批量 L1、再批量 L2）让 L1 产出有时间落盘并完成 SwiGLU，保证 L2 阶段输入就绪，从而支撑 dispatch/compute/combine 的 overlap 流水线（见 u8-l4）。

**Q2**：`get_next_block` 返回 `None` 的条件是什么？

**答**：当 `current_local_expert_idx >= kNumExpertsPerRank`，即所有 expert 都已派发完毕（[mega_moe.cuh:152-153](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L152-L153)）。此时 `for_each_block` 的循环退出。

---

### 4.2 per-expert token 计数的 lane 级缓存

#### 4.2.1 概念说明

调度要决定每个 expert 算几行（`num_m_blocks = ⌈tokens/BLOCK_M⌉`），就必须知道每个 expert 收到了几个 token。但 MoE 的路由是动态的——expert 收到多少 token 要等 dispatch 阶段（NVLink 通信）完成后才知道。调度器于是：

1. 在调度开始前，用 `fetch_expert_recv_count` 从全局内存**自旋等待**并读取每个 expert 的最终 token 数。
2. 把这 `kNumExpertsPerRank` 个整数**散布到一个 warp 的 32 个 lane 的寄存器里**缓存起来，后续查询不再碰全局内存。

这就是 `stored_num_tokens_per_expert`——一张「warp 分布式寄存器缓存」。

#### 4.2.2 核心流程

设本 rank 共 `E = kNumExpertsPerRank` 个 expert，一个 warp 有 32 个 lane。把 expert 编号按「跨 lane 交错」的方式分配：

\[
\text{lane } l \text{ 负责缓存的 expert 集合} = \{\, i \times 32 + l \;\big|\; i = 0, 1, \dots, \text{kNumExpertsPerLane}-1 \,\}
\]

其中每个 lane 需要缓存的 expert 数为

\[
\text{kNumExpertsPerLane} = \left\lceil \frac{E}{32} \right\rceil
\]

见模板默认参数 [mega_moe.cuh:25](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L25)。

举例：`E = 48`，则 `kNumExpertsPerLane = ⌈48/32⌉ = 2`。lane 0 缓存 expert {0, 32}，lane 1 缓存 {1, 33}，…，lane 15 缓存 {15, 47}，lane 16~31 缓存 {16, (越界)}（越界槽位不读）。

有了这张缓存表，两个查询都能用 warp 内通信完成：

- **查某 expert 的 token 数** (`get_num_tokens`)：持有 expert `e` 的 lane 是 `e % 32`（因为 `e = i×32 + l ⟹ l = e mod 32`，`i = e / 32`）。该 lane 把自己缓存的值用 `__shfl_sync` 广播给所有 lane。
- **查某 expert 之前的累计 M-block 数** (`get_pool_block_offset`)：每个 lane 累加自己缓存的、编号小于 `e` 的 expert 的 `⌈tokens/BLOCK_M⌉`，再用 warp 归约 `__reduce_add_sync` 求和。

#### 4.2.3 源码精读

**填充缓存**——`fetch_expert_recv_count`，见 [mega_moe.cuh:185-199](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L185-L199)：

```cpp
#pragma unroll
for (uint32_t i = 0; i < kNumExpertsPerLane; ++ i) {
    const auto expert_idx = i * 32 + ptx::get_lane_idx();   // 本 lane 负责的 expert
    uint64_t value = 0;
    if (expert_idx < kNumExpertsPerRank) {
        do {
            value = ptx::ld_volatile(workspace.get_expert_recv_count_sum_ptr(expert_idx));
        } while (static_cast<uint32_t>(value >> 32) != kNumSMs * kNumRanks);  // 等所有贡献者到齐
    }
    stored_num_tokens_per_expert[i] = static_cast<uint32_t>(value);
}
__syncwarp();
```

要点：

- `value` 是 64 位：**高 32 位是「到达计数」**（dispatch 阶段每个 SM×rank 各自 +1，达到 `kNumSMs * kNumRanks` 表示全部到齐），**低 32 位才是 token 数**。自旋直到高 32 位凑齐，再取低 32 位，确保读到的是最终值。
- 地址 `get_expert_recv_count_sum_ptr` 来自 `Workspace`（[layout/mega_moe.cuh:150-153](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/layout/mega_moe.cuh#L150-L153)），是 dispatch 阶段写入的统计区。

**查 token 数**——`get_num_tokens`，见 [mega_moe.cuh:71-79](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L71-L79)：

```cpp
uint32_t valid_value;
#pragma unroll
for (uint32_t i = 0; i < kNumExpertsPerLane; ++ i)
    valid_value = (expert_idx == i * 32 + ptx::get_lane_idx()) ?
        stored_num_tokens_per_expert[i] : valid_value;   // 命中的 lane 填入值
return ptx::exchange(valid_value, expert_idx % 32);       // 从持有 lane 广播
```

`ptx::exchange(v, src_lane)` 是 `__shfl_sync` 封装，返回 `src_lane` 那个 lane 的 `v`（[ptx/utils.cuh:30-40](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/ptx/utils.cuh#L30-L40)）。由于只有 `expert_idx % 32` 那个 lane 的 `valid_value` 被正确填入，广播后全体 lane 都拿到 expert `e` 的 token 数。

**查累计 pool 偏移**——`get_pool_block_offset`，见 [mega_moe.cuh:82-90](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L82-L90)：

```cpp
uint32_t num_blocks = 0;
#pragma unroll
for (uint32_t i = 0; i < kNumExpertsPerLane; ++ i)
    if (i * 32 + ptx::get_lane_idx() < expert_idx)                          // 只算 e 之前的 expert
        num_blocks += math::ceil_div(stored_num_tokens_per_expert[i], BLOCK_M);
return __reduce_add_sync(0xffffffff, num_blocks);                            // warp 求和
```

每个 lane 各自累加它缓存的、编号小于 `e` 的 expert 的 M-block 数，再 warp 归约求和，得到 expert `e` 在 pool 里的起始 M-block 偏移。设备 kernel 正是用它定位 token 数据：`pool_block_idx = get_current_pool_block_offset() + m_block_idx`，见 [sm100_fp8_fp4_mega_moe.cuh:679-680](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L679-L680)。

#### 4.2.4 代码实践

**目标**：理解 `kNumExpertsPerLane` 的含义与 lane→expert 的映射。

**步骤**：

1. 打开 [mega_moe.cuh:19-29](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L19-L29)，确认 `kNumExpertsPerLane = constexpr_ceil_div(kNumExpertsPerRank, 32u)`。
2. 对 `kNumExpertsPerRank ∈ {32, 48, 64, 96}`，手算 `kNumExpertsPerLane`，并写出 lane 0、lane 15、lane 31 各自缓存的 expert 编号集合（注意越界槽要剔除）。

**预期结果**：

| kNumExpertsPerRank | kNumExpertsPerLane | lane 0 缓存 | lane 15 缓存 | lane 31 缓存 |
| --- | --- | --- | --- | --- |
| 32 | 1 | {0} | {15} | {31} |
| 48 | 2 | {0, 32} | {15, 47} | {31}（slot1 越界） |
| 64 | 2 | {0, 32} | {15, 47} | {31, 63} |
| 96 | 3 | {0, 32, 64} | {15, 47, 79} | {31, 63, 95} |

3. **可观察现象**：查询 `expert = 47` 时，`get_num_tokens` 里命中的 lane 是 `47 % 32 = 15`，对应 `i = 47/32 = 1`，即 lane 15 的 `stored_num_tokens_per_expert[1]`。若你推导的命中 lane 与此不符，说明对「`e = i×32 + l ⟹ l = e%32`」的拆解有误。

#### 4.2.5 小练习与答案

**Q1**：为什么不直接用一个数组 `num_tokens[E]` 放共享内存，而要散到 lane 寄存器里？

**答**：寄存器访问比共享内存更快、且不占共享内存容量（mega-kernel 的共享内存已非常紧张，要放 A/B/SF/CD/多级 barrier 等）。把 `E` 个值散到 32 个 lane，每 lane 只占 `kNumExpertsPerLane` 个寄存器；查询时用 `__shfl`/`__reduce_add_sync` 在 warp 内通信即可汇聚，零额外存储、零全局内存回读。

**Q2**：`fetch_expert_recv_count` 里的自旋条件 `value >> 32 != kNumSMs * kNumRanks` 是在等什么？

**答**：在等 dispatch 阶段所有贡献者（`kNumSMs` 个 SM × `kNumRanks` 个 rank）都把它们的「到达计数」累加进高 32 位。只有凑齐才能保证低 32 位的 token 数是最终值，避免读到中间未完成的数据。

---

### 4.3 wave/expert 调度：持久化 CTA、wave 边界与 pool 偏移

#### 4.3.1 概念说明

把「有哪些块要算」（4.1 的状态机）和「每个 expert 的 token 数」（4.2 的缓存）拼起来，就是完整的 wave/expert 调度。三个概念：

- **持久化 CTA**：launch `kNumSMs` 个 CTA，每个 CTA 反复领块，`block_idx += kNumSMs` 跨「迭代」（即跨 expert 内的不同瓦片、跨 wave）推进。
- **wave 边界**：`kNumExpertsPerWave` 决定一波处理几个 expert；`get_wave_expert_end_idx` 给出当前 wave 的 expert 上界。一个 wave 内，调度器把该范围内所有 expert 的瓦片（先 L1 后 L2）派发给所有 CTA。
- **pool 偏移**：所有 expert 的 token 拼在一个按 M-block 计数的环形池里。expert `e` 的起始 M-block = 它之前所有 expert 的 M-block 总和（4.2 的 `get_pool_block_offset`）。`(expert, m_block)` 经此偏移落到全局 pool 位置。

`kNumExpertsPerWave` 不是写死的，而是宿主启发式根据「token 总量、ring buffer 容量、SM 数」算出来的——既要填满 SM，又不能超过 ring buffer 容量。

#### 4.3.2 核心流程

一次完整调度（`for_each_block`，[mega_moe.cuh:201-220](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L201-L220)）：

```
1. fetch_expert_recv_count()   # 自旋等待并填充 lane 级 token 缓存
2. set_expert_idx(0)           # 从 expert 0 开始（同时算出它的 token 数与 pool 偏移）
3. 循环：
     (phase, expert, m, n) = get_next_block()
     若 phase == None → 结束
     否则执行用户回调 func(phase, expert, num_k_blocks(phase), m, n)
```

wave 边界由 `get_wave_expert_end_idx` 给出（[mega_moe.cuh:65-69](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L65-L69)）：

\[
\text{end} = \min\!\Big(\,\big\lceil (\text{cur}+1) \,/\, \text{kNumExpertsPerWave} \big\rceil \times \text{kNumExpertsPerWave},\;\; \text{kNumExpertsPerRank}\Big)
\]

即「把当前 expert 向上对齐到 wave 边界，再夹回总 expert 数」，处理最后一个不满 `kNumExpertsPerWave` 的尾波。

`kNumExpertsPerWave` 的来源是宿主启发式 `get_num_experts_per_wave_for_mega_moe`（[heuristics/mega_moe.hpp:134-185](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/mega_moe.hpp#L134-L185)）。它的核心权衡是：

- **下界**：要让所有 SM 都忙起来。用 `num_expected_l1_blocks_per_expert`（每 expert 期望瓦片数）估算「至少要几个 expert 凑齐一波才能填满 SM」，再乘一个不均衡系数 `kImbalanceFactor=2`。
- **上界**：受 ring buffer 容量限制。`get_num_wave_pool_tokens(…, num_experts_per_wave, …)` 必须不超过 `num_ring_tokens`，否则一波的 token 装不下（[heuristics/mega_moe.hpp:80-93](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/mega_moe.hpp#L80-L93) 与 [134-144](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/mega_moe.hpp#L134-L144)）。
- **折中**：在 `[下界, min(上界, 2×下界)]` 区间里扫一遍，挑让**尾波利用率最高**的值（`tail_ratio = 余数 / kNumExpertsPer_wave`，[heuristics/mega_moe.hpp:172-184](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/mega_moe.hpp#L172-L184)）。

最终 `kNumExpertsPerWave` 作为编译期模板参数烤进 kernel（[sm100_fp8_fp4_mega_moe.hpp:79](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_fp8_fp4_mega_moe.hpp#L79) 的 `args.config.num_experts_per_wave`），再传给调度器模板（设备侧 [sm100_fp8_fp4_mega_moe.cuh:286-292](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L286-L292)）。

#### 4.3.3 源码精读

调度器在 kernel 内的实例化（[sm100_fp8_fp4_mega_moe.cuh:286-292](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L286-L292)）：

```cpp
auto scheduler = sched::MegaMoEScheduler<
    BLOCK_M, BLOCK_N, BLOCK_K,
    L1_SHAPE_N, L1_SHAPE_K,
    L2_SHAPE_N, L2_SHAPE_K,
    kNumExpertsPerRank,        // = kNumExperts / kNumRanks（见设备模板 line 48）
    kNumExpertsPerWave,
    kNumSMs, kNumRanks>(workspace);
```

`for_each_block` 把调度器循环和具体计算缝合（[sm100_fp8_fp4_mega_moe.cuh:666-669](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L666-L669)）。注意 `num_k_blocks` 也随 phase 变化：L1 用 `kNumL1BlockKs`、L2 用 `kNumL2BlockKs`：

```cpp
scheduler.for_each_block([&](const sched::BlockPhase& block_phase,
                             const uint32_t& local_expert_idx,
                             const uint32_t& num_k_blocks,
                             const uint32_t& m_block_idx, const uint32_t& n_block_idx) { … });
```

`for_each_block` 本体（[mega_moe.cuh:201-220](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L201-L220)）：

```cpp
fetch_expert_recv_count();     // 填 lane 级缓存
set_expert_idx(0);             // 初始化到 expert 0
while (true) {
    CUTE_TIE_DECL(get_next_block(), block_phase, current_local_expert_idx, m_block_idx, n_block_idx);
    if (block_phase == BlockPhase::None) break;
    func(block_phase, current_local_expert_idx,
         block_phase == BlockPhase::Linear2 ? kNumL2BlockKs : kNumL1BlockKs,
         m_block_idx, n_block_idx);
}
```

推进 expert 的两个辅助函数：`advance_expert_idx`（顺序 +1，同时累加 pool 偏移）与 `set_expert_idx`（跳到任意 expert，重算 token 数与 pool 偏移），见 [mega_moe.cuh:92-102](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L92-L102)。`get_current_num_m_blocks` 就是 `⌈current_num_tokens / BLOCK_M⌉`（[mega_moe.cuh:108-110](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L108-L110)）。

#### 4.3.4 代码实践

**目标**：观察真实运行中 `kNumExpertsPerWave` 的取值，并理解它如何被 ring 容量与 SM 数夹逼。

**步骤**：

1. 在 SM100 机器上设环境变量 `DG_PRINT_CONFIGS=1`（或 `DG_JIT_DEBUG=1`），运行 `tests/test_mega_moe.py` 中的一个多进程用例。**待本地验证**（需要 SM100 GPU 与多进程环境）。
2. 观察控制台首次打印的 `MegaMoEConfig(…)`，重点看 `num_experts_per_wave=` 字段（打印逻辑在 [heuristics/mega_moe.hpp:309-318](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/mega_moe.hpp#L309-L318)）。
3. 对照源码 [get_num_experts_per_wave_for_mega_moe](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/mega_moe.hpp#L134-L185)，判断打印值落在哪条分支：
   - 若等于 `num_max_experts_per_wave` → 被 ring 容量卡住（line 163-164）。
   - 若等于 `num_min_expected_experts_to_fill_sms` → 每 expert 瓦片数已 ≥ SM 数，用最小 wave 换 L2 复用（line 167-168）。
   - 否则 → 是扫区间内尾波利用率最高的值（line 172-184）。

**预期结果**：prefill（token 多）时每 expert 瓦片多，`num_experts_per_wave` 倾向较小值；decode（token 少、每 expert 期望瓦片 <1）时会触发 line 159-160，`num_min_expected_experts_to_fill_sms` 被设为 `num_experts_per_rank`，再被 ring 容量夹到 `num_max_experts_per_wave`。

#### 4.3.5 小练习与答案

**Q1**：`get_wave_expert_end_idx` 里 `cute::min(aligned, kNumExpertsPerRank)` 的 `min` 是为处理什么情况？

**答**：处理最后一个不满 `kNumExpertsPerWave` 的尾波。若 `kNumExpertsPerRank` 不是 `kNumExpertsPerWave` 的整数倍，最后一个 wave 的 expert 上界会被夹回到 `kNumExpertsPerRank`，避免越界。

**Q2**：为什么 `kNumExpertsPerWave` 要作为编译期模板参数，而不是运行时参数？

**答**：因为 `get_wave_expert_end_idx`、pool 偏移计算等都依赖它，把它烤成编译期常量后编译器能展开循环、消除除法/取模（`align(..., kNumExpertsPerWave)` 变成位运算或常量乘法），且 `for` 循环上限 `kNumExpertsPerLane` 等派生量也变成编译期已知。代价是不同 `kNumExpertsPerWave` 会触发不同 kernel 的 JIT 编译（见 u3）。

---

### 4.4 2-CTA cluster 约束：为何 SM 数与 N block 数都必须为偶数

#### 4.4.1 概念说明

Mega MoE 在 SM100 上用**固定的 2-CTA cluster**：每两个相邻 CTA（`blockIdx.x` 为 `{2j, 2j+1}`）组成一个簇。簇内两个 CTA 协作的关键好处是 **A（token）瓦片的 multicast**——两个 CTA 共享同一块 A，一次 TMA 加载广播给两者，省一半 A 的加载带宽；同时它们各算相邻的一块 N（权重）瓦片，合起来覆盖 `2×BLOCK_N` 列。

要让这个好处成立，簇内两个 CTA 必须：**落在同一个 m_block 行、n_block 相差 1**（这样 A 瓦片相同、B 瓦片相邻）。调度器的瓦片映射规则必须保证这一点。源码用三条 `DG_STATIC_ASSERT` 把它钉死：

```cpp
DG_STATIC_ASSERT(kNumSMs % 2 == 0, "Number of SMs must be even for 2-CTA cluster");
DG_STATIC_ASSERT(kNumL1BlockNs % 2 == 0, "L1 N block count must be even for 2-CTA cluster");
DG_STATIC_ASSERT(kNumL2BlockNs % 2 == 0, "L2 N block count must be even for 2-CTA cluster");
```

见 [mega_moe.cuh:39-41](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L39-L41)（注释在 [37-38](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L37-L38)）。注意它与 u6-l4 的差异：SM90 的 TMA multicast 可逐 CTA 动态开关，SM100 的 2-CTA cluster 是**静态固定**的，无法运行时关闭，所以必须靠断言在编译期保证整除。

#### 4.4.2 核心流程（数学推导）

记 \(N\) = `kNumL1BlockNs`（L1 的 N 方向瓦片数，L2 同理），单个 expert 的瓦片网格为 \(R \times N\)（\(R = \lceil \text{tokens}/B_M\rceil\) 行）。调度器对当前 expert 把线性游标 `block_idx` 映射为：

\[
m = \left\lfloor \frac{\text{block\_idx}}{N} \right\rfloor, \qquad n = \text{block\_idx} \bmod N
\]

2-CTA cluster 把 CTA 配对为 \(\{2j,\, 2j+1\}\)。要保证配对两 CTA 落在同一行（\(m\) 相同）、列差 1，需要：

\[
\left\lfloor \frac{2j}{N} \right\rfloor = \left\lfloor \frac{2j+1}{N} \right\rfloor
\]

这个等式**唯一失效**的情形是 \(2j \bmod N = N-1\)（即 \(2j\) 恰在行末、\(2j+1\) 跳到下一行首）。

**关键观察**：`block_idx` 的奇偶性在每个 CTA 的整个生命周期里**恒定不变**。理由：

- 初始 `block_idx = blockIdx.x`（[mega_moe.cuh:62](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L62)），CTA `2j` 恒偶、`2j+1` 恒奇。
- 每步 `block_idx += kNumSMs`（[160](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L160)、[172](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L172)）。
- 跨 expert 时 `block_idx -= num_m_blocks × N`（[127](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L127)、[143](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L143)）。

只要 **`kNumSMs` 偶** 且 **`N` 偶**，则 `kNumSMs` 与 `num_m_blocks × N` 都是偶数，增减偶数不改变奇偶性。于是 CTA `2j` 的 `block_idx` 恒为偶、CTA `2j+1` 恒为奇。

**回到失效条件** \(2j \bmod N = N-1\)：左边 \(2j \bmod N\) 恒偶（因 \(2j\) 偶、\(N\) 偶 ⟹ 余数偶）；右边 \(N-1\) 在 \(N\) 偶时为**奇**。偶 ≠ 奇，故失效条件**永不成立**。∎

所以 `N`（`kNumL1BlockNs`/`kNumL2BlockNs`）为偶 ⟹ 配对两 CTA 永远同行同 A 瓦片 ⟹ multicast 恒成立。而 `kNumSMs` 为偶除了维持奇偶性外，还有一个更直接的理由：**grid 维度（= `num_sms` 个 CTA）必须被 cluster_size=2 整除**，否则会有落单 CTA 无法成簇。这两个偶数约束共同保证了 2-CTA cluster 在所有 wave、所有 expert 上都正确配对。

#### 4.4.3 源码精读

cluster 维度由宿主 `LaunchArgs` 的第 4 个参数设定为 `2`，见 [sm100_fp8_fp4_mega_moe.hpp:220-222](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_fp8_fp4_mega_moe.hpp#L220-L222)：

```cpp
.launch_args = LaunchArgs(num_sms,
                          config.num_dispatch_threads + config.num_non_epilogue_threads + config.num_epilogue_threads,
                          config.smem_size, 2)   // ← cluster_dim = 2
```

`num_sms` 来自 `device_runtime->get_num_sms()`（[198](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_fp8_fp4_mega_moe.hpp#L198)）。设备侧也用 2-SM 的 TMEM 分配器呼应：`using Allocator = cute::TMEM::Allocator2Sm;`（[sm100_fp8_fp4_mega_moe.cuh:67](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L67)）——两个 CTA 共享同一块 tensor memory，这正是簇内协作的另一面。

`kNumL1BlockNs = L1_SHAPE_N / BLOCK_N`、`kNumL2BlockNs = L2_SHAPE_N / BLOCK_N` 是模板默认参数（[mega_moe.cuh:26-27](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L26-L27)）。而 `L1_SHAPE_N = 2×intermediate_hidden`、`BLOCK_N = 128`（启发式里写死，[heuristics/mega_moe.hpp:257](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/mega_moe.hpp#L257)），所以 `kNumL1BlockNs = 2×intermediate_hidden/128`。断言它为偶，等价于约束 `intermediate_hidden` 是 `128` 的整数倍且商满足偶数性——`intermediate_hidden` 通常取 128 的较大倍数（如若干 ×128），自然满足。

此外还有一组形状整除断言（`L1/L2_SHAPE_N % BLOCK_N == 0`、`L1/L2_SHAPE_K % BLOCK_K == 0`，[mega_moe.cuh:31-34](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L31-L34)），以及 `0 < kNumExpertsPerWave <= kNumExpertsPerRank`（[35](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L35)）。它们保证瓦片网格规整、wave 配置合法。

#### 4.4.4 代码实践

**目标**：亲手验证「`N` 为偶 ⟹ 配对 CTA 恒同行」的推导，并理解断言的物理含义。

**步骤**：

1. 打开 [mega_moe.cuh:31-41](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/mega_moe.cuh#L31-L41)，逐条抄下 6 条 `DG_STATIC_ASSERT` 与它们的提示串。
2. 取 \(N = 4\)（偶），模拟一个 1 行 4 列的 expert 网格（`num_m_blocks=1`）。对 CTA `blockIdx.x ∈ {0,1,2,3,4,5}`（前 3 个簇 `{0,1},{2,3},{4,5}`），按 `m = floor(block_idx/N)`、`n = block_idx mod N` 填表：

   | blockIdx | block_idx | m | n | 簇配对 | 同行？ | 列差 |
   | --- | --- | --- | --- | --- | --- | --- |
   | 0 | 0 | 0 | 0 | {0,1} | ✓ | n: 0→1 |
   | 1 | 1 | 0 | 1 | {0,1} | ✓ | — |
   | 2 | 2 | 0 | 2 | {2,3} | ✓ | n: 2→3 |
   | 3 | 3 | 0 | 3 | {2,3} | ✓ | — |
   | 4 | 4 | 1 | 0 | {4,5} | ✓ | n: 0→1 |
   | 5 | 5 | 1 | 1 | {4,5} | ✓ | — |

   可见每个簇内两 CTA 永远同行、列差 1。

3. 反例验证：把 \(N\) 改成 **3（奇）**，重做上表。此时 CTA 2（block_idx=2）落在行末（\(2 \bmod 3 = 2 = N-1\)），CTA 3（block_idx=3）跳到下一行首（\(m=1, n=0\)）。簇 `{2,3}` 的两 CTA **不同行**，multicast 失效。这正是断言要禁止的情形。
4. 解释 `kNumSMs % 2 == 0`：若 `num_sms=5`（奇），grid 有 5 个 CTA，cluster_size=2 只能配出 2 个完整簇，第 5 个 CTA 落单无法成簇——故 grid 维度必须被 2 整除。

**预期结果**：偶数 \(N\) 下所有簇同行；奇数 \(N\) 下必存在落单错位的簇。从而理解三条偶数断言是 multicast 正确性的**编译期保证**。

#### 4.4.5 小练习与答案

**Q1**：为何 SM100 这里用编译期 `DG_STATIC_ASSERT` 保证偶数，而 u6-l4 的 SM90 GEMM 却能运行时动态关 multicast？

**答**：SM90 的 TMA multicast 可以逐 CTA、运行时决定开或关（调度器用 `is_tma_multicast_valid` 等动态判定，遇不整除就关掉），所以不需要编译期整除保证。SM100 的 2-CTA cluster 是 launch 时静态固定的，无法运行时关闭，故必须靠 `DG_STATIC_ASSERT` 在编译期确保 `num_sms`、`kNumL1BlockNs`、`kNumL2BlockNs` 都为偶，否则簇配对会错位。不满足时直接编译失败，而非运行时悄悄出错。

**Q2**：如果 `intermediate_hidden` 取了一个让 `kNumL1BlockNs` 为奇的值，会发生什么？

**答**：`kNumL1BlockNs = 2×intermediate_hidden / BLOCK_N`。若它为奇，`DG_STATIC_ASSERT(kNumL1BlockNs % 2 == 0, …)` 在 JIT 编译期失败，kernel 编译报错（经 u3 的 `DGException` 翻译为 Python 异常）。所以 `intermediate_hidden` 必须选得让该商为偶——实际配置中 `intermediate_hidden` 一般是 128 的较大偶倍数，自然满足。

---

## 5. 综合实践

**任务**：给定一个具体的 Mega MoE 配置，手算一个完整 wave 的调度输出，把本讲四个模块串起来。

**配置**（仅为理解，非真实可运行形状）：

- `kNumExpertsPerRank = 4`，`kNumExpertsPerWave = 2`（2 个 wave）。
- expert 的 token 数依次为 `tokens = [64, 32, 64, 32]`，`BLOCK_M = 32`，故每 expert 的 M-block 数 `num_m_blocks = [2, 1, 2, 1]`。
- `kNumL1BlockNs = 4`（偶），`kNumL2BlockNs = 4`（偶），`kNumSMs = 4`。
- 初始 `stored_num_tokens_per_expert` 已填好（假设 dispatch 已完成）。

**要求**：

1. **lane 缓存**（4.2）：算 `kNumExpertsPerLane = ⌈4/32⌉ = 1`。写出 lane 0~3 各自缓存的 expert（lane 0→expert0=64，lane1→expert1=32，lane2→expert2=64，lane3→expert3=32）。验证 `get_num_tokens(2)` 命中 lane `2%32=2`，返回 64。
2. **pool 偏移**（4.2/4.3）：算 `get_pool_block_offset(e)`：expert0→0，expert1→`⌈64/32⌉=2`，expert2→`2+1=3`，expert3→`3+2=5`。
3. **wave 边界**（4.3）：wave0 的 `get_wave_expert_end_idx` 从 expert0 出发 = `min(align(1, 2), 4) = 2`，即 expert {0,1}；wave1 从 expert2 出发 = `min(align(3,2),4)=4`，即 expert {2,3}。
4. **状态机序列**（4.1）：模拟 CTA `blockIdx.x=0` 在 wave0 的调用序列，写出前若干次 `get_next_block` 返回的 `(phase, expert, m, n)`，确认 wave0 内**所有 Linear1 出现在所有 Linear2 之前**。
5. **偶数约束**（4.4）：`kNumL1BlockNs=4` 偶、`kNumSMs=4` 偶，验证簇 `{0,1}`、`{2,3}` 内两 CTA 在 expert0 的瓦片网格（2 行 × 4 列）里永远同行、列差 1。

**验收**：把 1~5 的结果整理成一张表/图。若你的状态机序列里出现了「L2 出现在某 expert 的 L1 还没全部派发完之前」，说明对 4.1 的 wave 级两阶段理解有误，回头重看 `get_next_block` 的 `set_expert_idx(align_down(...))` 分支。

> 本任务全程是源码阅读 + 手算，无需 GPU。若想对照真实运行，可在 SM100 上用 `DG_PRINT_CONFIGS=1` 跑 `tests/test_mega_moe.py`，比对打印的 `num_experts_per_wave` 与你手算的 wave 划分（**待本地验证**）。

## 6. 本讲小结

- `MegaMoEScheduler` 用 `BlockPhase{None,Linear1,Linear2}` 状态机驱动 `get_next_block`，在一个 wave 内**先算完所有 expert 的 L1、再回头算 L2**，让 L1 产出有时间完成 SwiGLU 供 L2 消费。
- 调度沿用 u6-l4 的**持久化 CTA**：launch `kNumSMs` 个 CTA，每块后 `block_idx += kNumSMs`；`block_idx` 是跨 expert 的全局线性游标，由 `fetch_next_l1/l2_block` 按 `[num_m_blocks × N]` 网格解析成 `(m, n)`。
- 每个 expert 的 token 数由 `fetch_expert_recv_count` 自旋等待 dispatch 完成后，**散布到 warp 32 个 lane 的寄存器**（`stored_num_tokens_per_expert`，每 lane `kNumExpertsPerLane=⌈E/32⌉` 个），查询靠 `__shfl_sync`/`__reduce_add_sync` 汇聚。
- `kNumExpertsPerWave` 由宿主启发式在「填满 SM」与「不超 ring 容量」之间夹逼，并扫区间选尾波利用率最高的值，最终烤成编译期模板参数。
- SM100 用**固定 2-CTA cluster**，三条偶数断言（`kNumSMs`、`kNumL1BlockNs`、`kNumL2BlockNs` 均偶）在编译期保证簇内配对 CTA `{2j,2j+1}` 恒落同一 m_block 行、n_block 差 1，从而使 A 瓦片 multicast 恒成立——根因是 `block_idx` 奇偶性恒定，偶数 CTA 永不可能落在奇数列 \(N-1\) 上。

## 7. 下一步学习建议

- **u8-l4 融合 mega 内核与通信重叠**：本讲只讲了「调度器决定算什么」，下一讲讲「dispatch pull / GEMM / SwiGLU / combine 如何在同一个 kernel 内 overlap」，特别是 `grid_sync`（u6-l3 已铺垫）在 wave 切换与 L1→L2 过渡里的同步作用。
- **回看 u8-l1/u8-l2**：调度器的 pool 偏移直接消费 u8-l1 的 ring token 预算与 SymmBuffer 布局；Linear1 输出经 SwiGLU 变成 Linear2 输入的布局转换，依赖 u8-l2 的 gate/up 交错与 UTCCP SF 转置。
- **对照 u6-l4 与 u7**：普通 GEMM 的 `Scheduler`（持久化 + L2 swizzle + 动态 multicast）与本讲的 `MegaMoEScheduler`（持久化 + 两阶段 + 静态 2-CTA）是同一思想的两套实现，对比阅读能加深对「持久化调度」家族的理解。
- **源码延伸**：若想看调度结果如何驱动 TMA 加载与 UMMA 计算，可读 [sm100_fp8_fp4_mega_moe.cuh:666](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_mega_moe.cuh#L666) 起的三个 `for_each_block` 回调（dispatch warp、TMA load warp、math warp 各一份），体会同一个调度器如何被不同线程角色复用。
