# 掩码分组 GEMM（解码阶段）

## 1. 本讲目标

本讲承接 u7-l1（连续布局的 M 轴分组 GEMM），继续讲「只对 M 轴分组」的 MoE 矩阵乘，但换一种内存布局——**masked 布局**。两者解决的是同一个问题（把多个 expert 的 token 放进一次 kernel launch），却服务于推理流水线上截然不同的两个阶段：u7-l1 的 **contiguous** 布局适合 prefill / 训练（CPU 提前知道每段长度），本讲的 **masked** 布局适合 **解码（decode）+ CUDA graph** 阶段（CPU 在录制 graph 时根本不知道每个 expert 究竟会收到几个 token）。

学完后你应当掌握：

- 理解 **masked 布局为何而生**：在 CUDA graph 解码场景下，CPU 无法提前知道每 expert 的 token 数，于是给每个 expert 预留一块固定大小（`max_m`）的行空间，再用一张 `masked_m` 张量在运行时标记「这块里前多少行是有效的」，kernel 只算有效部分。
- 看懂 `masked_m` 张量的语义，以及设备侧 `Scheduler` 的 `MGroupedMasked` 分支如何靠 **逐组累加 M 块数**（`current_m_cumsum`）把一块块 tile 正确地分配到对应 expert。
- 说清 **`expected_m` 参数**在配置选择（`get_best_config`）中扮演的角色——以及为什么传错它不会让结果出错，但会让性能塌掉。

本讲对应 API：

- `deep_gemm.m_grouped_fp8_fp4_gemm_nt_masked`（别名 `m_grouped_fp8_gemm_nt_masked`）
- `deep_gemm.m_grouped_bf16_gemm_nt_masked`

## 2. 前置知识

阅读本讲前，你需要已经建立以下认知（来自前置讲义）：

- **连续布局的 M 轴分组 GEMM**（u7-l1）：MoE 推理把一批 token 路由到若干 expert，每个 expert 用自己的权重 `[N, K]` 做一次矩阵乘；N、K 在所有 expert 间固定不变，变的只是每段的 M（token 数）。contiguous 布局把这些 token 首尾相接拼进一个 `[M, K]` 张量，用 `grouped_layout: [M]` 数组逐行标记归属，并要求每段 M/K 对齐到 `mk_alignment_for_contiguous_layout`。本讲讲的 masked 是它的「姊妹布局」，请随时对照。
- **NT 布局与架构派发**（u2-l1、u2-l3）：`nt` 是唯一真正的原生 kernel，`nn/tn/tt` 只是先 `.transpose()` 再转发；API 层先做校验、变换缩放因子（SF），再按 `device_runtime->get_arch_major()`（9=SM90，10=SM100）派发。本讲的 masked API 走完全相同的四步范式。
- **缩放因子 SF**（u2-l2）：FP8/FP4 输入以 `(tensor, sf)` 元组传入，SF 经 `transform_sf_pair_into_required_layout` 变换成 TMA 友好布局；SM90 用 FP32 SF，SM100 用打包 UE8M0 SF。
- **分块调度与 wave**（u6-l4）：设备内核启动 `kNumSMs` 个 CTA，每个 CTA 在 `while` 循环里反复领 `(m_block, n_block)` tile；`next_block_idx = (++current_iter) * kNumSMs + blockIdx.x`，`current_iter` 即 wave 编号。本讲要回答的核心问题就是：**在 masked 场景下，这个领 tile 的循环如何知道当前 tile 属于哪个 expert、tile 总数又是多少**。

几个术语约定：

- **expert / group**：本文交替使用，指 MoE 中的一个专家网络。`num_groups` 即 expert 数量。
- **`max_m`**：每个 expert 在张量里被预留的行数（张量 M 维的大小），是一个**编译/录制时已知的固定上界**。
- **`masked_m[g]`**：第 `g` 个 expert **实际有效**的 token 行数，是一个**运行时才知道**的值，满足 `0 <= masked_m[g] <= max_m`。
- **CUDA graph**：把一组 GPU 调用「录制」成可重放的图，从而消除逐次 launch 的 CPU 开销，是低延迟解码的关键手段。录制时张量形状固定，但某些「动态」信息（如每 expert 的 token 数）只能留到回放时填进固定大小的缓冲。
- **DeepEP**：DeepSeek 的 expert 并行（EP）通信库，负责把 token 在 GPU 间分发/聚合；它的低延迟输出天然契合本讲的 masked 布局。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`csrc/apis/gemm.hpp`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp) | API 派发层。`m_grouped_fp8_fp4_gemm_nt_masked` 做校验、变换 SF、按架构派发；`m_grouped_bf16_gemm_nt_masked` 是 BF16 版本。 |
| [`deep_gemm/include/deep_gemm/scheduler/gemm.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh) | 设备侧 `Scheduler` 模板。`MGroupedMasked` 分支定义了逐组累加、有效行判定的 tile 分配逻辑。 |
| [`csrc/jit_kernels/impls/sm100_fp8_fp4_gemm_1d1d.hpp`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_fp8_fp4_gemm_1d1d.hpp) | SM100 宿主 Runtime：`sm100_m_grouped_fp8_fp4_gemm_masked_1d1d` 构造 `GemmDesc`（含 `expected_m`）、选 config、建 TMA 描述符、生成并启动 kernel。 |
| [`csrc/jit_kernels/impls/sm90_fp8_gemm_1d2d.hpp`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d2d.hpp) | SM90 宿主 Runtime：`sm90_m_grouped_fp8_gemm_masked_1d2d`（1D2D kernel）。 |
| [`csrc/jit_kernels/heuristics/sm100.hpp`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp) 与 [`sm90.hpp`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp) | `get_layout_info` 用 `expected_m * expected_num_groups` 估算总 tile 数，驱动配置选择。 |
| [`tests/generators.py`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py) 与 [`tests/test_fp8_fp4.py`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_fp8_fp4.py) | `generate_m_grouped_masked` 构造带 `masked_m` 的输入；`test_m_grouped_gemm_masked` 演示调用与按 `masked_m[g]` 切片校验。 |
| [`deep_gemm/include/deep_gemm/common/types.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/common/types.cuh) | `GemmType` 枚举，`MGroupedMasked = 2`。 |

## 4. 核心概念与源码讲解

### 4.1 masked 布局的动机：解码阶段为何不能用 contiguous

#### 4.1.1 概念说明

先回顾 u7-l1 的 contiguous 布局：把 `num_groups` 个 expert 的 token **首尾相接**拼进一个 `[M, K]` 张量（`M = sum(每段 token 数)`），再用 `grouped_layout: [M]` 逐行标记归属。它的前提是：**在调用 GEMM 之前，CPU 已经确切知道每段的长度**，从而能算出总 M、算出每段的偏移、把对齐 padding 填好。

这个前提在 **prefill（预填充）** 和 **训练** 阶段成立——那时候每批 token 一次性到齐，CPU 有充分时间做拼接。但在 **解码（decode）** 阶段，情况完全不同：

1. **每步只产 1 个新 token**（或很少几个），推理是逐 token 推进的。
2. **CUDA graph 是低延迟解码的标配**：为了让每步解码尽可能快，框架会把整个解码 step 录制成一张可重放的 graph，回放时几乎不再走 CPU。录制 graph 时张量形状必须固定。
3. 然而每步里**每个 expert 究竟会收到几个 token，取决于这一步的路由结果，是运行时才知道的**——录制 graph 时无法预知，于是无法提前算出 contiguous 布局所需的「总 M」与「每段偏移」。

于是 contiguous 的「拼接 + 逐行标记 + 对齐」这套流程在解码阶段失效了。masked 布局给出了另一条路：

> 给每个 expert 预留一块**固定大小 `max_m`** 的行空间（张量形状提前定死为 `[G, max_m, K]`），把「每段实际多长」这个动态信息从张量形状里剥离出来，单独塞进一张 `[G]` 的小张量 `masked_m`，留到回放时再填。kernel 在运行时读 `masked_m`，只算每个 expert 前 `masked_m[g]` 行。

这正是 DeepSeek-V3 一类 MoE 模型解码阶段的典型形态：DeepEP 把 token 分发到各 rank 后，每个 rank 上每个 expert 的有效 token 数各不相同，但都装在固定大小的缓冲里，由一个 `masked_m` 数组描述。

#### 4.1.2 masked 与 contiguous 的布局对照

两种布局用同一组有效数据，却摆出完全不同的张量形状：

| 维度 | contiguous（u7-l1） | masked（本讲） |
| --- | --- | --- |
| 输入 A 形状 | `[M, K]`（M = 各段拼接后的总和） | `[G, max_m, K]`（每 expert 独占一块） |
| 权重 B 形状 | `[G, N, K]` | `[G, N, K]` |
| 输出 D 形状 | `[M, N]` | `[G, max_m, N]` |
| 归属描述 | `grouped_layout: [M]`（逐行标 expert 号，padding 行标 `-1`） | `masked_m: [G]`（每 expert 有效行数） |
| 是否需要拼接/padding | 需要：CPU 提前拼接并对齐 | 不需要：形状固定，padding 行「闲置」在缓冲里 |
| 总 tile 数 | 编译期由 `M` 决定 | **运行时**由 `sum(ceil(masked_m[g]/BLOCK_M))` 决定 |
| 典型阶段 | prefill / 训练 | 解码 / CUDA graph |

注意一个关键区别：contiguous 的 `grouped_layout` 长度是 `M`（逐行），而 masked 的 `masked_m` 长度是 `G`（逐 expert）。前者是「这一行属于谁」，后者是「这个 expert 有几行有效」——信息密度更高、开销更小，也更契合「形状固定、只填一个动态计数」的 CUDA graph 录制模型。

### 4.2 masked_m 张量与 MGroupedMasked 调度的有效行判定

#### 4.2.1 概念说明

masked 布局的核心数据结构是 `masked_m`：

- **类型**：`int32`，连续存放（`is_contiguous()`）。
- **形状**：`[G]`，即每个 expert 一个整数。
- **语义**：`masked_m[g]` 表示第 `g` 个 expert 在它那块 `max_m` 行的空间里，**前 `masked_m[g]` 行是有效 token**，其余是 padding。
- **约束**：`0 <= masked_m[g] <= max_m`（一个 expert 本步可能完全没分到 token，此时 `masked_m[g] = 0`）。

围绕它，设备侧的 `Scheduler` 要解决一个新问题：**普通 GEMM / contiguous 的 tile 总数在 launch 之前就定死了**（`num_blocks = num_m_blocks * num_n_blocks`），所以持久化调度器只要判断 `next_block_idx < num_blocks` 就知道还有没有活干。但 masked 的 tile 总数取决于 `masked_m`，而 `masked_m` 是设备运行时才读的数组——launch 时宿主都不知道。于是 `MGroupedMasked` 分支换了一套「**边走边累加**」的分配策略。

#### 4.2.2 核心流程：逐组累加 M 块数

`Scheduler` 模板用一个 `kGemmType` 编译期参数把不同布局的 tile 分配逻辑分开。`MGroupedMasked = 2` 的分支维护两个游标：

- `current_group_idx`：当前正在处理的 expert 编号。
- `current_m_cumsum`：**已经处理完的 expert 累计占了多少个 M 块**（即 cumulative M-block sum）。

领 tile 的逻辑（伪代码）如下：

```
next_block_idx = (++current_iter) * kNumSMs + blockIdx.x   # 持久化调度的线性 tile 号

while True:
    if current_group_idx == num_groups:        # 所有 expert 都分完 → 任务结束
        return False
    num_m_blocks = ceil_div(masked_m[current_group_idx], BLOCK_M)
    cumsum = current_m_cumsum + num_m_blocks    # 到当前组为止累计的 M 块数
    if next_block_idx < cumsum * num_n_blocks:  # 这个 tile 落在当前组范围内 → 命中
        break
    # 否则：当前组的 tile 不够分到这个号，前进到下一组
    current_group_idx += 1
    current_m_cumsum = cumsum

# 命中后，把「组内相对 tile 号」交给 swizzle 做 L2 友好重排
local_idx = next_block_idx - current_m_cumsum * num_n_blocks
get_swizzled_block_idx(local_idx, m_block_idx, n_block_idx)
```

关键直觉：`masked_m` 把每个 expert 的有效行数告诉设备侧，调度器据此算出「每个 expert 折合多少个 M 块」，把这些块数逐组累加，得到一个**不断增长的 tile 总边界** `cumsum * num_n_blocks`。每个 CTA 拿着自己的线性号 `next_block_idx` 去对齐这条边界——落在哪一段，就属于哪个 expert。落到组内后，再减去前若干组的总 tile 数得到「组内相对号」，交给和普通 GEMM 完全相同的 `get_swizzled_block_idx` 做 L2 swizzle。

> 注意：因为每个 expert 的 N、K 固定，`num_n_blocks` 在所有组里是同一个常数；变的只有每组的 M 块数。这就是为什么累加只针对 M 块。

#### 4.2.3 源码精读

先看 `MGroupedMasked` 在 `GemmType` 枚举里的位置：

[`deep_gemm/include/deep_gemm/common/types.cuh:20-28`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/common/types.cuh#L20-L28) — 列出全部 `GemmType`，`MGroupedMasked = 2` 与 `MGroupedContiguous = 1`、`MGroupedContiguousWithPsumLayout = 5` 并列，同属「M 轴分组」家族。

接着看构造函数里的分支——`MGroupedMasked` 与 contiguous 不同，**它不在构造时算 `num_blocks`**（因为算不出来），只把 `grouped_layout`（也就是 `masked_m` 的设备指针）记下来：

[`deep_gemm/include/deep_gemm/scheduler/gemm.cuh:98-99`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L98-L99) — `MGroupedMasked` 构造分支：仅 `this->grouped_layout = grouped_layout;`，不预计算 `num_blocks`。对比上一行 `MGroupedContiguous` 会算 `num_blocks = num_m_blocks * num_n_blocks`，区别一目了然。

构造函数里的 `current_m_cumsum` 字段就是「逐组累加 M 块数」用的游标：

[`deep_gemm/include/deep_gemm/scheduler/gemm.cuh:54-55`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L54-L55) — 注释 `// Only used for masked layout` 下的 `uint32_t current_m_cumsum = 0;`。

下面是核心的领 tile 循环，对应 4.2.2 的伪代码：

[`deep_gemm/include/deep_gemm/scheduler/gemm.cuh:200-216`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L200-L216) — `get_next_block` 的 `MGroupedMasked` 分支：`num_m_blocks = ceil_div(grouped_layout[current_group_idx], BLOCK_M)` 即「当前 expert 折合几个 M 块」；`current_m_block_cumsum = current_m_cumsum + num_m_blocks` 是累计边界；命中条件 `next_block_idx < current_m_block_cumsum * num_n_blocks`；未命中则 `current_group_idx ++, current_m_cumsum = current_m_block_cumsum;` 推进到下一组。命中后用 `next_block_idx - current_m_cumsum * num_n_blocks` 求组内相对号再 swizzle。

命中某个组之后，要把「组内 M 块号」映射回全局 `[G, M, K]` 张量里的真实地址。这由 `get_global_idx` 完成——`MGroupedMasked` 在 M 维上会叠一个组偏移：

[`deep_gemm/include/deep_gemm/scheduler/gemm.cuh:163-165`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L163-L165) — `get_global_idx` 的 `MGroupedMasked`（与 psum 共用）分支：当 `kWithGroupOffset` 为真时 `offset = current_group_idx`，返回 `offset * shape_dim + block_idx * block_size`。这把「组号 × 每组行数 + 组内行号」扁平化成一个线性索引。

这个扁平化索引正好匹配 TMA 描述符对 masked 输入的构造方式。masked 的 A/B/D 都是 3D 张量（`[G, max_m, K]` / `[G, N, K]` / `[G, max_m, N]`），但 TMA 描述符把 `num_groups` 折叠进了外维，等价于把 `[G, M]` 看成 `[G*M]`：

[`csrc/jit_kernels/impls/runtime_utils.hpp:198-208`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L198-L208) — `make_tma_a_desc` 当 `num_groups > 1` 时把外维设成 `shape_m * num_groups`（`get_inner_outer_dims(major, shape_k, shape_m * num_groups)`），即 A 被当成 `[G*M, K]` 二维张量喂给 TMA。于是设备侧算出的扁平 `m_idx = current_group_idx * shape_m + m_block_idx * BLOCK_M` 能直接命中正确行。

设备侧发起 TMA 时确实用了这个带组偏移的索引（注意 `MGroupedMasked` 时 `kWithGroupOffset` 为真，故 M 维叠了组偏移）：

[`deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh:221-224`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh#L221-L224) — `m_idx = get_global_idx<(kGemmType == MGroupedMasked), MN>(shape_m, BLOCK_M, m_block_idx)`：模板实参 `(kGemmType == GemmType::MGroupedMasked)` 正是 `kWithGroupOffset`，于是 masked 时 M 维自动叠 `current_group_idx * shape_m` 偏移。

最后看「有效行判定」——`masked_m` 不仅用来分 tile，还用来在计算时跳过 padding 行。这体现在 `is_computation_valid`：

[`deep_gemm/include/deep_gemm/scheduler/gemm.cuh:316-317`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L316-L317) — `is_computation_valid` 的 `MGroupedMasked` 分支：`return m_offset + m_block_idx * BLOCK_M < grouped_layout[current_group_idx];`。也就是「这块 tile 的起始行是否还在当前 expert 的有效行数 `masked_m[current_group_idx]` 之内」。注意它用的是 `<`（严格小于）而非对齐——因为一个 `BLOCK_M` 大小的 tile 内可能前几行有效、后几行越界，判定的是 tile 起点是否有效。

补充一个两代架构的差异：SM90 的 1D2D kernel 会真正调用 `is_computation_valid` 来跳过无用的 WGMMA 计算（WGMMA 结果落在寄存器里，跳过能省指令）：

[`deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d2d.cuh:274`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d2d.cuh#L274) — `if (scheduler.is_computation_valid(m_block_idx, math_wg_idx * WGMMA::M))` 包住实际计算，越界的 math warp 直接不算。

而 SM100 的 1D1D kernel **不调用** `is_computation_valid`（它把整个 `BLOCK_M` tile 都算完写回），靠调用方把 padding 行的 SFA 清零来保证 padding 行贡献为 0（见 4.2.4 的生成器）。两代架构用不同方式处理 padding，是阅读时需要注意的差异点。

还有一个 multicast 相关的简化：masked 的 tile 边界天然按 `BLOCK_M` 对齐到 expert（每个 expert 的有效块数 `ceil(masked_m[g]/BLOCK_M)`），所以 multicast 不需要像 contiguous 那样检查「相邻 CTA 是否同 expert」——调度器直接断言 masked 必然对齐：

[`deep_gemm/include/deep_gemm/scheduler/gemm.cuh:293-296`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L293-L296) — `is_tma_multicast_valid` 对 `MGroupedMasked`（以及 Normal、k-grouped、Batched、psum）直接 `return true`；只有 `MGroupedContiguous` 才需要逐行比对相邻 CTA 的 expert 号。

以及 SM90 调度注释里点明 masked 无需维护 `is_peer_cta_alive`：

[`deep_gemm/include/deep_gemm/scheduler/gemm.cuh:280`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L280) — `// NOTES: we don't have to set is_peer_cta_alive for masked grouped GEMM, as it must be aligned`。

#### 4.2.4 代码实践（源码阅读 + 生成器追踪）

由于 masked 调度需要 SM90/SM100 硬件，这里先做一次「源码阅读型实践」，把 `masked_m` 从生成到被消费的整条链路串起来，再给出一个可运行的脚本骨架（标「待本地验证」）。

1. **实践目标**：用 `masked_m` 的一组具体取值，手算 `MGroupedMasked` 调度器会把哪些 `(m_block, n_block)` 分给哪个 expert，验证你对 4.2.2 累加逻辑的理解。
2. **操作步骤**：
   - 打开 [`tests/generators.py:380-408`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py#L380-L408) 的 `generate_m_grouped_masked`，看它如何生成 `masked_m`：
     - [`tests/generators.py:389-393`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py#L389-L393)：`masked_m[j] = int(expected_m_per_group * random.uniform(0.7, 1.3))`，即每 expert 有效行数在期望值上下 30% 浮动；并断言 `masked_m.amax() <= max_m`。
     - [`tests/generators.py:404-406`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py#L404-L406)：把每个 expert 超出 `masked_m[j]` 的 SFA padding 行**清零**（`a[1][j, masked_m[j].item():] = 0`）——这就是 4.2.3 末尾说的「SM100 靠清零 SFA 处理 padding」的来源。
   - 假设 `G=3, max_m=256, BLOCK_M=128, num_n_blocks=4`，`masked_m = [100, 256, 30]`。手算每个 expert 折合的 M 块数：`ceil(100/128)=1, ceil(256/128)=2, ceil(30/128)=1`，累计边界（乘 `num_n_blocks=4`）依次是 `1*4=4, (1+2)*4=12, (1+2+1)*4=16`。
   - 那么 tile 线性号 `0..3` → expert 0，`4..11` → expert 1，`12..15` → expert 2；`next_block_idx >= 16` 即任务结束。这与调度器循环逐字对应。
3. **需要观察的现象**：expert 1（`masked_m=256` 恰好整除 `BLOCK_M=128`）没有 padding 块；expert 0 和 expert 2 的最后一块 tile 是「部分有效」（起点在界内但 tile 尾部越界），对它们 `is_computation_valid` 在 SM90 上会触发跳过。
4. **预期结果**：你手算的「tile 号 → expert」映射，应当和调度器 `get_next_block` 用 `current_m_cumsum` 累加给出的划分完全一致。若不一致，检查是否漏乘了 `num_n_blocks`。
5. **可运行脚本骨架（待本地验证）**：下面这段脚本在 SM90/SM100 上复现生成器与 masked 调用，并按 `masked_m[g]` 切片校验。它不是项目原有代码，仅作示例。

```python
# 示例代码：仅作演示，需在 SM90/SM100 + 已安装 deep_gemm 的环境运行（待本地验证）
import torch, deep_gemm
from deep_gemm.testing import calc_diff
from tests.generators import generate_m_grouped_masked

G, max_m, expected_m_per_group, n, k = 6, 1024, 1024, 7168, 3072
# generate_m_grouped_masked 内部会让 masked_m 在 expected_m 上下浮动
a, b, masked_m, psum_m, d, ref_d = generate_m_grouped_masked(
    G, max_m, expected_m_per_group, n, k, use_ue8m0=True)

# expected_m 是一个“估计上界”，测试里给了 1.2 倍裕量（见 test_fp8_fp4.py:162）
deep_gemm.m_grouped_fp8_fp4_gemm_nt_masked(
    a, b, d, masked_m, int(expected_m_per_group * 1.2))

# 关键：只能校验每个 expert 前 masked_m[g] 行，后面的 padding 行结果无意义
for g in range(G):
    mg = masked_m[g].item()
    if mg == 0:
        continue
    diff = calc_diff(d[g, :mg], ref_d[g, :mg])
    assert diff < 1e-3, (g, mg, diff)
```

#### 4.2.5 小练习与答案

**练习 1**：若把 `masked_m` 改成 `[256, 256, 0]`（第三个 expert 本步没分到 token），调度器会怎样处理 expert 2？

**答案**：`ceil(0/BLOCK_M)=0`，expert 2 折合 0 个 M 块，累计边界不增长（`current_m_cumsum` 不变），`current_group_idx` 会直接越过它推进；没有任何 tile 会命中 expert 2。本质上是「空 expert 被自动跳过」，这正是 decode 阶段某些 expert 偶尔空载时所需的行为。

**练习 2**：为什么 `masked_m` 用 `int32` 而不是 `int64` 或 `bool` 掩码？

**答案**：它要存的是「有效行数」这个计数，而非「每行是否有效」的布尔掩码，所以 `bool` 不够；用计数而非逐行掩码让元数据体积从 `O(G*max_m)` 降到 `O(G)`。选 `int32` 是因为行数上界 `max_m` 通常远小于 2^31，且 `int32` 与设备侧 `int* grouped_layout`、TMA 索引运算的字宽一致（见 [`csrc/apis/gemm.hpp:276`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L276) 的 `kInt` 断言）。

### 4.3 expected_m 参数与配置选择

#### 4.3.1 概念说明

看一眼 masked API 的签名，你会发现一个 contiguous API 里没有的参数——`expected_m`：

[`csrc/apis/gemm.hpp:250-259`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L250-L259) — `m_grouped_fp8_fp4_gemm_nt_masked` 的签名，参数列表里除了 `a/b/d/masked_m` 外多了一个 `const int& expected_m`。

为什么需要它？回到本讲开头的矛盾：

- **选 config 必须知道「大概有多少 tile」**：`get_best_config`（u5-l2）要比较不同 `block_m/block_n/cluster` 候选，比的是 `num_waves`、`last_wave_util`、`num_cycles`，这些都依赖总 tile 数。
- **但 masked 的总 tile 数运行时才知道**：它等于 `sum_g ceil(masked_m[g]/BLOCK_M) * num_n_blocks`，而 `masked_m` 在配置/编译时还不知道。

于是需要一个**估计值** `expected_m`：它代表「**预计每个 expert 大约有多少 token**」，配置选择器据此估算总工作量。注意三个层次的区别，别混淆：

| 量 | 何时已知 | 用途 |
| --- | --- | --- |
| `max_m` | 编译/录制 graph 时 | 决定张量形状 `[G, max_m, K]`，是硬上界 |
| `expected_m` | 调用前由用户给出 | **估计**每 expert token 数，驱动 config 选择 |
| `masked_m[g]` | 运行时（回放时填） | **实际**每 expert 有效行数，驱动真实 tile 分配与计算 |

`expected_m` 是「估计」，`masked_m` 是「实际」。配置选择用估计，真实计算用实际——这就是 masked 能在「形状固定、计数动态」的 CUDA graph 里工作的全部秘密。

#### 4.3.2 核心流程：expected_m 如何驱动 config

`expected_m` 流经三处，作用层层递进：

**第一处：填进 `GemmDesc.expected_m`，作为配置选择的输入。**

[`csrc/jit_kernels/impls/sm100_fp8_fp4_gemm_1d1d.hpp:253-265`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_fp8_fp4_gemm_1d1d.hpp#L253-L265) — SM100 masked 构造 `GemmDesc`：`.m = m`（即 `max_m`，张量真实 M 维），但 `.expected_m = expected_m`（用户估计），`.expected_num_groups = num_groups`。注意 `.m` 与 `.expected_m` 是两个不同字段，前者是张量形状、后者是工作量估计。SM90 版本同理：[`csrc/jit_kernels/impls/sm90_fp8_gemm_1d2d.hpp:235-246`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d2d.hpp#L235-L246)。

这条思路在 contiguous 的注释里被点破（psum 也是「actual M is dynamic」才用 expected_m）：

[`csrc/jit_kernels/impls/sm100_fp8_fp4_gemm_1d1d.hpp:179-180`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_fp8_fp4_gemm_1d1d.hpp#L179-L180) — 注释 `// NOTES: If actual M is dynamic, estimate config via num_groups and expected_m.`（出现在紧邻的 contiguous 函数里，原理通用）。

**第二处：`get_layout_info` 用 `expected_m * expected_num_groups` 估算总 tile 数。**

[`csrc/jit_kernels/heuristics/sm100.hpp:230-233`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp#L230-L233) — 估算总块数：

\[
\text{num\_blocks} = \left\lceil \frac{\text{expected\_m}}{\text{block\_m}} \right\rceil \cdot \left\lceil \frac{\text{expected\_n}}{\text{block\_n}} \right\rceil \cdot \text{expected\_num\_groups}
\]

再 `num_waves = ceil_div(num_blocks, num_sms)`。SM90 完全相同：[`csrc/jit_kernels/heuristics/sm90.hpp:203-206`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L203-L206)。

也就是说，`expected_m` 通过「每 expert 估计块数 × expert 数」给出**总工作量**，进而决定选多大的 `block_m`、几个 wave、要不要 multicast——`expected_m` 偏小会选到过大的 `block_m`（实际 token 填不满，利用率差），偏大则会多估工作量（影响尾波利用率评估）。

**第三处：`expected_m` 是否进入编译期特化（compiled_dims）？**

这里要澄清一个常见误解：`expected_m` **本身不被烤成编译期常量**。真正进入 JIT 模板特化的是张量形状 `m/n/k`（受 `compiled_dims` 控制，masked 默认 `"nk"`，见 [`csrc/apis/gemm.hpp:694`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L694) 的注册默认值）。`expected_m` 只在**宿主侧的配置选择**里用一次，选完 config 后它的历史使命就结束了——设备 kernel 完全不读 `expected_m`，只读运行时的 `masked_m`。

这一点很关键：**`expected_m` 估错了不会让结果出错**（结果由 `masked_m` 决定），**只会让选出来的 config 不够优**。这也是为什么测试里敢放心给 `expected_m` 加 20% 裕量而不担心正确性：

[`tests/test_fp8_fp4.py:162`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_fp8_fp4.py#L162) — 调用时 `expected_m = int(expected_m_per_group * 1.2)`，刻意高估一点作为裕量。同处的 TODO 注释（[`tests/test_fp8_fp4.py:132`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_fp8_fp4.py#L132)）也警告：当**实际** `m` 大于 `expected_m` 时，效率可能显著下降。

`get_expected_m()` 的「0 表示默认」约定把「没给估计」和「给了估计」统一在一个 getter 里：

[`csrc/jit_kernels/heuristics/config.hpp:29-33`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/config.hpp#L29-L33) — `get_expected_m() const { return expected_m > 0 ? expected_m : m; }`：没填 `expected_m`（=0）就退回用张量真实 M 维 `m`（即 `max_m`）作估计。masked 总是显式填了 `expected_m`，所以走前者。

#### 4.3.3 源码精读：API 层的校验与派发

把整条宿主链路串起来。API 入口先做一组校验，其中两条与 masked 强相关：

[`csrc/apis/gemm.hpp:266-276`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L266-L276) — 形状/类型校验：用 `check_grouped_ab_fp8_fp4` 从 3D 的 `a=[G,M,K]`、`b=[G,N,K]` 解出 `(num_groups, m, k)` 与 `(num_groups, n, k)`，并要求 `d=[G,M,N]`、`masked_m=[G]` 的 `num_groups` **四处一致**；关键断言 `expected_m > 0 and m > 0 and n > 0 and k > 0 and num_groups > 0`，以及 `masked_m.scalar_type() == torch::kInt`。

注意 masked 没有 contiguous 那样的「m/k 对齐」断言——因为 masked 的每个 expert 占满固定 `max_m` 行，tile 不跨 expert 边界这件事由「每 expert 独占一块空间」天然保证，无需对齐 padding。这也是 masked 与 contiguous 在 API 层最显眼的差异（contiguous 的 m/k 对齐由 `mk_alignment_for_contiguous_layout` 在别处把关，见 u7-l1）。

SF 变换时，masked 把 `num_groups` 同时传给 A 和 B 的 SF（因为两边都是 3D 分组）：

[`csrc/apis/gemm.hpp:281-283`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L281-L283) — `transform_sf_pair_into_required_layout(..., num_groups, num_groups, disable_ue8m0_cast)`，两个 `num_groups` 分别对应 SFA、SFB 的分组数。对比 contiguous 的调用（[`csrc/apis/gemm.hpp:215-217`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L215-L217)）A 的 SF 分组数传的是 `std::nullopt`（因为 contiguous 的 A 是 2D），区别正源于 A 的维度不同。

最后按架构派发，与所有 FP8 GEMM 一样用「架构 + 变换后 SF dtype」双条件：

[`csrc/apis/gemm.hpp:285-296`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L285-L296) — SM90 + FP32 SF → `sm90_m_grouped_fp8_gemm_masked_1d2d`（1D2D）；SM100 + 打包 UE8M0 SF（int32）→ `sm100_m_grouped_fp8_fp4_gemm_masked_1d1d`（1D1D）。注意 masked 在两代架构上用的 kernel 形态不同：SM90 走 1D2D，SM100 走 1D1D——这与 contiguous 一致（u7-l1 已述）。

SM90 宿主 Runtime 把 `masked_m` 的设备指针当作 `grouped_layout` 传给设备 kernel（调度器里 `grouped_layout` 与 `masked_m` 是同一个东西）：

[`csrc/jit_kernels/impls/sm90_fp8_gemm_1d2d.hpp:280`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d2d.hpp#L280) — `.grouped_layout = masked_m.data_ptr()`。SM100 同样：[`csrc/jit_kernels/impls/sm100_fp8_fp4_gemm_1d1d.hpp:301`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_fp8_fp4_gemm_1d1d.hpp#L301)。

#### 4.3.4 代码实践：expected_m 的「估错只伤性能不伤正确性」

1. **实践目标**：在真实硬件上验证「`expected_m` 大小不影响数值正确性，只影响性能/利用率」。
2. **操作步骤**（需 SM90/SM100 环境，待本地验证）：
   - 用 [`tests/test_fp8_fp4.py:129`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_fp8_fp4.py#L129) 的 `test_m_grouped_gemm_masked` 为模板，固定一组 `(G, max_m, n, k)` 与一组 `masked_m`。
   - 分别用 `expected_m = masked_m.min()`、`expected_m = masked_m.max()`、`expected_m = int(expected_m_per_group*1.2)` 调用同一个 masked API，输出都写到独立的 `d`。
   - 对三者都按 `d[g, :masked_m[g]]` 切片，用 `calc_diff` 与 `ref_d[g, :masked_m[g]]` 比较（参考 [`tests/test_fp8_fp4.py:167-174`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_fp8_fp4.py#L167-L174) 的校验方式）。
   - 再用 `bench_kineto`（参考 [`tests/test_fp8_fp4.py:176-178`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_fp8_fp4.py#L176-L178)）测三者的耗时。
3. **需要观察的现象**：
   - 三次 `calc_diff` 应基本相同（差异仅来自浮点非确定性），都低于阈值——证明 `expected_m` 不影响正确性。
   - 三次耗时不同：`expected_m` 偏小（选了过大 `block_m`）时，因为实际 token 填不满大 block，wave 利用率低、更慢；`expected_m` 接近真实均值时最快。
   - 若开 `DG_PRINT_CONFIGS=1`（u5-l2），可看到三次选出的 `block_m/block_n/cluster` 不同。
4. **预期结果**：正确性 diff 三者一致；性能上「接近真实均值的 `expected_m`」最优。这就是 [`tests/test_fp8_fp4.py:132`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_fp8_fp4.py#L132) 那条 TODO（实际 m 超过 expected_m 时效率下降）要警示的现象。
5. 若无法在本地运行，明确记为「待本地验证」，并改为源码阅读：对照 4.3.2 的三处引用，说明 `expected_m` 只出现在 `get_best_config` 的输入里、不出现在设备 kernel 任何模板参数或运行时参数里。

#### 4.3.5 小练习与答案

**练习 1**：如果调用时压根不给 `expected_m`（或给 0），会发生什么？

**答案**：API 层会因 `DG_HOST_ASSERT(expected_m > 0)`（[`csrc/apis/gemm.hpp:274`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L274)）抛 `DGException`，翻译成 Python 异常。即便绕过断言，`config.hpp` 的 `get_expected_m()` 也会退回用 `m`（即 `max_m`）作估计——这通常高估工作量，可能选到偏大的 block，性能不佳。所以 masked 强制要求显式给 `expected_m`。

**练习 2**：为什么测试里用 `int(expected_m_per_group * 1.2)` 而不是直接用 `expected_m_per_group`？

**答案**：因为生成器让真实 `masked_m` 在 `expected_m_per_group` 的 0.7~1.3 倍间随机浮动（[`tests/generators.py:392`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py#L392)），真实值可能高于期望。给 1.2 倍裕量能让 `expected_m` 大概率盖住真实值，避免「实际 m > expected_m → 效率塌掉」的坑（TODO 警示）。这模拟了线上解码时「按一个偏保守的上界来估」的工程实践。

**练习 3**：masked 默认 `compiled_dims="nk"`（[`csrc/apis/gemm.hpp:694`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L694)）。结合本讲，为什么 M 维不特化（留运行时）是合理的？

**答案**：M 维对每个 expert 都是 `max_m`，但有效行 `masked_m[g]` 各不相同且运行时才知；把 M 留运行时（不特化）让同一份编译产物能服务任意 `masked_m` 组合，避免因 `masked_m` 变化触发重编译。N、K 在所有 expert 间固定且通常随模型结构稳定，特化它们能换性能而不带来组合爆炸。这正是 u5-l3「形状特化即投资」权衡在 masked 上的体现。

## 5. 综合实践

把本讲三块知识（masked 动机、masked_m 调度、expected_m）串成一个贯穿任务：**用同一批有效数据，分别走 masked 与 contiguous 两条 API，验证它们在「有效部分」数值一致，并解释为何 masked 多需要一个 `expected_m`。**

任务设计（需 SM90/SM100 环境，待本地验证）：

1. **生成数据**：调用 [`generate_m_grouped_masked`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py#L380-L408) 得到 `a=[G,max_m,K], b=[G,N,K], masked_m=[G], d=[G,max_m,N], ref_d`（注意 `b` 的形状天然同时满足两种 API：masked 要 `[G,N,K]`，contiguous 也要 `[G,N,K]`）。

2. **跑 masked**：按 4.2.4 的脚本调用 `m_grouped_fp8_fp4_gemm_nt_masked(a, b, d_masked, masked_m, expected_m)`。

3. **构造等价 contiguous 输入**：把每个 expert 前 `masked_m[g]` 行**按 `mk_alignment_for_contiguous_layout` 对齐 padding 后**首尾拼接成一个 `[M_cont, K]` 张量 `a_cont`，并构造 `grouped_layout_cont: [M_cont]`（有效行标 expert 号、padding 行标 `-1`）。可参考 u7-l1 的 `generate_m_grouped_contiguous` 思路。

4. **跑 contiguous**：调用 `m_grouped_fp8_fp4_gemm_nt_contiguous(a_cont, b, d_cont, grouped_layout_cont)`。

5. **对照**：对每个 expert，把 `d_masked[g, :masked_m[g]]` 与从 `d_cont` 中按 `grouped_layout_cont` 取出的对应有效行做 `calc_diff`，应低于阈值。

6. **回答两个问题**（写在实践报告里）：
   - 为什么 masked 必须传 `expected_m` 而 contiguous 不用？（提示：contiguous 的总 M 在调用前已知，masked 的总 tile 数运行时才知。）
   - 在 CUDA graph 解码场景下，哪种布局更合适？为什么？（提示：masked 的张量形状固定、只需回放时填 `masked_m`，契合 graph 录制模型。）

若本地无法运行，至少完成第 6 步的书面论证，并手算第 3 步在 `G=2, masked_m=[3,5], alignment=4` 时 `a_cont` 与 `grouped_layout_cont` 的样子（答案：expert0 占 3 行→pad 到 4 行、expert1 占 5 行→pad 到 8 行，`grouped_layout_cont = [0,0,0,-1, 1,1,1,1,1,-1,-1,-1]`，`M_cont=12`）。

## 6. 本讲小结

- **masked 布局为解码/CUDA graph 而生**：每 expert 预留固定 `max_m` 行（张量形状 `[G, max_m, K]` 提前定死），把「每段多长」这个动态信息剥离成 `[G]` 的 `masked_m`，回放时再填——避开了 contiguous 需要「提前知道总 M」的前提。
- **`masked_m[g]` = 第 g 个 expert 的有效行数**，`int32`、长度 `G`；设备侧 `Scheduler` 的 `MGroupedMasked` 分支靠 `current_m_cumsum`（逐组累加 M 块数）把 tile 线性号对齐到 expert 边界，`is_computation_valid` 用 `masked_m[current_group_idx]` 判定 tile 是否在有效行内。
- **masked 的 TMA 把 `num_groups` 折进外维**（A 视作 `[G*M, K]`），`get_global_idx` 在 M 维叠 `current_group_idx * shape_m` 偏移，使扁平索引直接命中正确行。
- **`expected_m` 是工作量估计**：填进 `GemmDesc.expected_m`，经 `get_layout_info` 用 `ceil(expected_m/block_m) * expected_num_groups` 估算总 tile 数来选 config；它**不进编译期特化、不被设备 kernel 读取**，估错只伤性能不伤正确性。
- **两代架构差异**：SM90 走 1D2D 并用 `is_computation_valid` 跳过越界 math warp；SM100 走 1D1D、算满整 tile，靠调用方清零 padding 行的 SFA 保证 padding 贡献为 0。
- **masked 无需 m/k 对齐断言**：每 expert 独占固定空间，tile 不跨 expert 边界天然成立；这与 contiguous 必须对齐 padding 形成对照。

## 7. 下一步学习建议

- **u7-l3（K 轴分组 GEMM 与 psum 布局）**：本讲和 u7-l1 都是「M 轴分组」，下一讲转到「K 轴分组」（MoE 权重梯度 wgrad），并深入 psum 布局。psum 布局（`MGroupedContiguousWithPsumLayout`）的调度器分支与 masked 共用了 `get_global_idx`（本讲 4.2.3 引用过），读完 u7-l3 你会完整理解 `scheduler/gemm.cuh` 里所有「M/K 分组」分支。
- **回头重读 u5-l2、u5-l3**：本讲多次提到 `expected_m` 驱动 `get_best_config`、`compiled_dims="nk"` 的特化权衡。带着 masked 的实例重读启发式与调优旋钮，会理解得更透。
- **若关心 decode 全链路**：可延伸阅读 DeepEP（专家并行通信）如何产出本讲所需的 `masked_m` 与固定大小缓冲，以及它与 DeepGEMM masked kernel 如何衔接——这部分超出本仓库范围，但理解它能让你看清「token 路由 → 通信 → masked GEMM」的解码端到端。
