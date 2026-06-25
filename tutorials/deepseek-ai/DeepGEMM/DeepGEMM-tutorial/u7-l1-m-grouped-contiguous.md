# 连续布局的 M 轴分组 GEMM

## 1. 本讲目标

在 u6（设备内核内部）之后，本讲把视角拉回「宿主侧如何描述一个分组问题」，进入单元 7「分组 GEMM（MoE）」。Mixture-of-Experts（MoE）推理中最频繁的计算是：一批 token 被路由到若干 expert，每个 expert 要用自己的权重做一次矩阵乘。本讲只讲其中最基础、最高效的一种布局——**只对 M 轴分组、把多个 expert 的 token 拼成单个连续张量**的 `m_grouped_*_gemm_*_contiguous`。

学完后你应当掌握：

- 理解 **contiguous 布局**如何用一个张量承载「长度各不相同」的 expert token 段，以及为何 N、K 维度在所有 expert 间固定不变。
- 看懂 `grouped_layout` 数组的两种编码（逐行标记 / psum 前缀和），以及设备侧 `Scheduler` 的 `MGroupedContiguous` 分支如何用一行 `grouped_layout` 反查出某个 tile 属于哪个 expert。
- 说清 **m/k 对齐要求**的来源（保证每个 BLOCK_M 的 tile 不跨越 expert 边界），以及为何要引入 **psum 布局**来省掉 padding 行在缩放因子上的浪费。

本讲对应 API：`deep_gemm.m_grouped_fp8_fp4_gemm_nt_contiguous`（及其别名 `m_grouped_fp8_gemm_nt_contiguous`、BF16 版本 `m_grouped_bf16_gemm_nt_contiguous`）。

## 2. 前置知识

阅读本讲前，你需要已经建立以下认知（来自前置讲义）：

- **NT 布局与原生 kernel**（u2-l1）：DeepGEMM 以 `D = C + A @ B` 为约定，`nt` 是唯一真正的原生 kernel，`nn/tn/tt` 只是先对张量做 `.transpose()` 再转发给 `nt`。本讲的 `m_grouped_*_gemm_*_contiguous` 同样如此：`_nn_contiguous` 仅转置 B 后转发给 `_nt_contiguous`。
- **缩放因子 SF 与架构派发**（u2-l2、u2-l3）：FP8/FP4 输入以 `(tensor, sf)` 元组传入；API 层先做布局/形状/类型校验，再按 `device_runtime->get_arch_major()`（9=SM90，10=SM100）派发到不同实现。SF 经 `transform_sf_pair_into_required_layout` 变换成 TMA 友好布局。
- **分块调度与 wave**（u6-l4）：设备内核启动 `kNumSMs` 个 CTA，每个 CTA 在 `while` 循环里反复领 `(m_block, n_block)` tile，`get_swizzled_block_idx` 做对 L2 友好的块序重排。本讲讲的就是「在分组场景下，这个 tile 分配循环要如何知道当前 tile 归哪个 expert」。

几个术语约定：

- **expert / group**：本文交替使用，指 MoE 中的一个专家网络。`num_groups` 即 expert 数量。
- **padding 行**：为了让对齐成立，每个 expert 段会被向上凑整到对齐粒度，多出来的行。
- **contiguous（连续）**：指把各 expert 的 token 行首尾相接拼进同一个张量，与下一讲的「masked 布局」（各 expert 占用固定 `max_m` 行）相对。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`csrc/apis/gemm.hpp`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp) | API 派发层。`m_grouped_fp8_fp4_gemm_nt_contiguous` 做校验、变换 SF、按架构派发；`_nn_contiguous` 是它的转置薄包装。 |
| [`deep_gemm/include/deep_gemm/scheduler/gemm.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh) | 设备侧 `Scheduler` 模板。`MGroupedContiguous` 与 `MGroupedContiguousWithPsumLayout` 两个分支定义了分组 tile 的索引逻辑。 |
| [`tests/generators.py`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py) | 测试输入生成器。`generate_m_grouped_contiguous` 演示了如何构造对齐后的连续输入与两种 `grouped_layout`。 |
| [`csrc/jit_kernels/heuristics/runtime.hpp`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/runtime.hpp) | `mk_alignment_for_contiguous_layout` 旋钮及其理论下界的计算。 |
| [`deep_gemm/include/deep_gemm/common/types.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/common/types.cuh) | `GemmType` 枚举，列出所有分组变体。 |

## 4. 核心概念与源码讲解

### 4.1 M 轴分组 contiguous：把变长 expert 拼成单个张量

#### 4.1.1 概念说明

MoE 前向的线性层要算的是一组形状不同的矩阵乘：

\[ D_g = A_g \cdot B_g^{\top}, \quad g = 0, 1, \dots, G-1 \]

其中 \(A_g\) 是分给第 \(g\) 个 expert 的 token 集合，行数 \(m_g\) 因 expert 而异（热门 expert 分到的 token 多），但每个 expert 的权重 \(B_g\) 形状一致，都是 \([N, K]\)。也就是说，**只有 M 轴是「变长 + 分组」的，N 和 K 在所有 expert 间固定**。

朴素做法是循环 `for g in range(G): gemm(A_g, B_g)`。问题有二：一是每个 expert 的 \(m_g\) 往往很小（几十到几千），小 GEMM 把 tensor core 喂不饱，GPU 利用率低；二是大量 kernel launch 带来固定开销。

DeepGEMM 的 **contiguous 布局** 把所有 expert 的 \(A_g\) **按 M 轴首尾相接**拼成一个大张量 \(A\)，形状为 \([M, K]\)，其中

\[ M = \sum_{g=0}^{G-1} \tilde{m}_g, \qquad \tilde{m}_g = \mathrm{align}(m_g,\, a) \]

\(a\) 是对齐粒度（见 4.3）。权重 \(B\) 不拼，保持 \([G, N, K]\)（一个 expert 一份）。输出 \(D\) 同样拼成 \([M, N]\)。这样一个长度各异的问题被描述成「一个 `[M,K] @ [G,N,K].mT` 的单一任务」，只需 **一次 kernel launch**，所有 tile 在 SM 间统一调度，避免了小 GEMM 的浪费。

#### 4.1.2 核心流程

`m_grouped_fp8_fp4_gemm_nt_contiguous` 的处理流程（沿袭 u2-l3 的四步范式）：

1. **布局校验**：A 必须是 K-major；B 在 SM90（FP8）也要求 K-major，SM100 放宽。
2. **形状/类型校验**：A 解析出 `(M, K)`，B 解析出 `(num_groups, N, K)`，D 解析出 `(M, N)`，三者交叉对齐；并根据是否 `use_psum_layout` 校验 `grouped_layout` 的长度（`[M]` 或 `[num_groups]`）。
3. **early return**：`M == 0` 直接返回。
4. **变换 SF**：把用户的 `(sfa, sfb)` 变换成 kernel 所需布局；若启用 psum 布局，把 `grouped_layout` 透传给 SF 变换，使 SFA 的打包跳过 padding 行。
5. **架构派发**：SM90 走 `sm90_m_grouped_fp8_gemm_contiguous_1d2d`，SM100 走 `sm100_m_grouped_fp8_fp4_gemm_contiguous_1d1d`。

#### 4.1.3 源码精读

API 主函数与签名（注意参数末尾的三个 psum 相关旋钮）：

[csrc/apis/gemm.hpp:166-177](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L166-L177) —— 函数定义。注释点明形状约定 `[M, K] @ [G, N, K].mT`（`.mT` 表示对最后两维做矩阵转置，即每个 expert 内部按 NT 处理）。

校验段：A 解析为 `(m, k)`，B 用 `check_grouped_ab_fp8_fp4` 解析为 `(num_groups, n, k)`，D 解析为 `(m, n)`：

[csrc/apis/gemm.hpp:186-204](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L186-L204) —— 形状/类型校验。其中第 197–204 行根据 `use_psum_layout` 分流：psum 时要求 `grouped_layout` 长度等于 `num_groups`；非 psum 时要求长度等于 `m`，且禁止传 `expected_m_for_psum_layout`。

SF 变换透传 psum 布局，再按架构派发：

[csrc/apis/gemm.hpp:213-231](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L213-L231) —— 关键是第 214 行：当 `use_psum_layout` 为真时把 `grouped_layout` 包装成 `psum_sfa_layout` 传给 `transform_sf_pair_into_required_layout`，让 SFA 打包「跳过 gap 行」（这是 psum 布局省内存的核心，4.3 详述）。第 220–228 行按「架构 + 变换后 SF dtype」双条件派发：SM90 + `float` SF 走 1d2d，SM100 + `int`（打包 UE8M0）SF 走 1d1d。

`_nn_contiguous` 是 `_nt_contiguous` 的转置薄包装，印证「nt 为唯一原生 kernel」：

[csrc/apis/gemm.hpp:234-248](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L234-L248) —— 仅对 B 的张量和 SF 各做 `.transpose(1, 2)`（转置 expert 维之外的两维），再转发给 nt 版本；同时把 `expected_m_for_psum_layout` 固定为 `nullopt`（nn 路径不支持 psum）。

#### 4.1.4 代码实践

**实践目标**：确认「contiguous = 拼接 + 一次 launch」这一直觉。

**操作步骤**（源码阅读型）：

1. 打开 `tests/generators.py` 的 `generate_m_grouped_contiguous`，观察它如何把多个 expert 的行拼进单个 `a = torch.randn((m, k), ...)`，其中 `m = sum(aligned_ms)`。
2. 对照 `tests/test_fp8_fp4.py` 第 87–97 行，看一次真实的 `m_grouped_fp8_fp4_gemm_nt_contiguous` 调用如何传入 `(a, b, d, grouped_layout)`。
3. 在 `csrc/apis/gemm.hpp:219-231` 的派发点处，跟踪到 `sm90_m_grouped_fp8_gemm_contiguous_1d2d`（SM90）或 `sm100_m_grouped_fp8_fp4_gemm_contiguous_1d1d`（SM100）——确认整批 expert 只产生 **一次** kernel launch。

**需要观察的现象**：相比 `for g in range(G): fp8_gemm_nt(...)` 的 G 次 launch，这里无论 `num_groups` 多大，派发终点都只是一次 `LaunchRuntime::launch`。

**预期结果**：你会清楚地看到，「分组」这件事在 API 层之后就被压扁成了「一个更大的 `[M,N]` 输出 tile 空间」，分组信息全部编码进 `grouped_layout`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 contiguous 布局只支持「N、K 固定」的分组？如果不同 expert 的 N 不同会怎样？

**答案**：因为所有 expert 的权重被拼进同一个 `[G, N, K]` 张量、输出拼进同一个 `[M, N]` 张量，要求它们在 N、K 维度上同构，才能用同一组 TMA 描述符和同一套 tile 形状去寻址。若 expert 的 N 不同，就无法用单个张量承载，必须退回多次独立 GEMM 或改用其它布局。

**练习 2**：API 注释写的是 `[M, K] @ [G, N, K].mT`，这里的 `.mT`（matrix transpose）作用在哪两维？为什么不是普通 `.T`？

**答案**：`.mT` 转置每个 expert 内部最后两维，即把 `[G, N, K]` 视作 `[G, (N,K)]` 后转置 `(N,K)`，得到逻辑上的 `[G, K, N]`，从而与 K-major 的 A 构成 NT 关系。它**不**转置 expert 维 G（普通 `.T` 会把整个三维全部反转），这正是 NT 布局所需要的。

### 4.2 grouped_layout 索引：设备侧如何定位 expert

#### 4.2.1 概念说明

把 token 拼进单个 `[M, K]` 张量后，设备内核拿到一个 `(m_block, n_block)` tile，必须回答两个问题：

1. **这个 tile 属于哪个 expert？** —— 决定该 tile 要读哪一份权重 `B_g`（因为 B 没有被拼接）。
2. **这个 tile 里有没有 padding 行？** —— 决定是否要跳过那些 `-1` 标记的填充行的计算。

这两个答案都编码在一个 `int32` 数组 **`grouped_layout`** 里。DeepGEMM 提供两种编码，由 `use_psum_layout` 开关切换：

| 模式 | `grouped_layout` 形状 | 语义 |
| --- | --- | --- |
| 非 psum（`use_psum_layout=False`） | `[M]` | 逐行标记：`grouped_layout[row] = g`（该行属 expert g），padding 行为 `-1`。 |
| psum（`use_psum_layout=True`） | `[num_groups]` | 前缀和：`grouped_layout[g]` = expert g 的「累计结束行号」（含本组的有效行）。 |

#### 4.2.2 核心流程

**非 psum 模式下的索引逻辑**（最关键，对应 `GemmType::MGroupedContiguous`）：

- 访问 **A**：A 是扁平拼接的，tile 的第 `m_block` 个 BLOCK_M 块对应的全局行就是 `m_block * BLOCK_M`，**不需要** group 偏移。
- 访问 **B / SFB**（按 expert 分组的张量）：需要先查 expert 索引。因为对齐保证了「一个 BLOCK_M 的 tile 不跨越 expert 边界」（见 4.3），所以只需采样该 tile 的**第一行** `grouped_layout[m_block * BLOCK_M]` 即可代表整个 tile 的归属：

\[ \text{group\_idx}(m\_block) = \max(0,\; \text{grouped\_layout}[m\_block \cdot \text{BLOCK\_M}]) \]

然后 B 的全局列偏移 = `group_idx * N + n_block * BLOCK_N`。`max(0, ...)` 是为了把 `-1`（padding）钳到 0，避免越界寻址——真正的跳过由 `is_computation_valid` 兜底。

- **跳过 padding**：计算前用 `is_computation_valid` 检查 `grouped_layout[m_offset + m_block*BLOCK_M] >= 0`，遇到 `-1` 的 warp 直接不算。

**psum 模式下的索引逻辑**（对应 `GemmType::MGroupedContiguousWithPsumLayout`）：不再逐行查表，而是按 expert 顺序「扫段」。每段的 M-block 数由相邻 psum 偏移之差（并对齐到 BLOCK_M）推出，见 4.3.2。

#### 4.2.3 源码精读

构造函数按 `kGemmType` 分流初始化，注意 `MGroupedContiguous` 与 `MGroupedContiguousWithPsumLayout` 的区别：

[scheduler/gemm.cuh:88-115](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L88-L115) —— `MGroupedContiguous` 分支（第 95–97 行）：`num_blocks = num_m_blocks * num_n_blocks`，仅保存 `grouped_layout` 指针；M-block 总数由整段 M 计算。`MGroupedContiguousWithPsumLayout` 分支（第 100–103 行）：`num_m_blocks` 从**第一组**的 psum 偏移 `grouped_layout[0]` 起算，因为后面组的 M-block 数会随调度动态变化。

非 psum 模式下访问分组张量的核心索引函数：

[scheduler/gemm.cuh:160-162](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L160-L162) —— `MGroupedContiguous` 的 `get_global_idx`：当 `kWithGroupOffset` 为真时，`offset = max(0, grouped_layout[m_block_idx * BLOCK_M])`，即「采样 tile 首行得 expert 索引」；否则 `offset = 0`（用于不分组、或像 A 这样已扁平化的张量）。返回 `offset * shape_dim + block_idx * block_size`。

> 对照设备内核 `sm90_fp8_gemm_1d2d.cuh`：访问 A 时 `kWithGroupOffsetA = (kGemmType == GemmType::MGroupedMasked)`，对 `MGroupedContiguous` 取 false（A 不加偏移）；访问 B 时 [sm90_fp8_gemm_1d2d.cuh:203](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d2d.cuh#L203) 显式写 `get_global_idx<true>(shape_n, BLOCK_N, n_block_idx, m_block_idx)`，把 `m_block_idx` 作为第 4 参传入，正是为了在 `get_global_idx` 内部反查 expert。

padding 跳过判断（非 psum 模式）：

[scheduler/gemm.cuh:311-315](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L311-L315) —— `MGroupedContiguous` 的 `is_computation_valid` 返回 `grouped_layout[m_offset + m_block_idx * BLOCK_M] >= 0`。设备内核在每个 math warp-group 发 MMA 前调用它（见 [sm90_fp8_gemm_1d2d.cuh:274](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d2d.cuh#L274)），命中 `-1` 的行块直接不算，既省算力又避免污染输出。

附带一点：SM90 的 TMA multicast 在分组场景还多一道 expert 一致性检查——相邻两个 CTA 要 multicast 同一份 B，必须属同一 expert：

[scheduler/gemm.cuh:297-306](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L297-L306) —— `MGroupedContiguous` 的 `is_tma_multicast_valid`：multicast 在 B 上时恒真；multicast 在 A 上时，比较 `grouped_layout[m_block_idx * BLOCK_M]` 与 `grouped_layout[(m_block_idx ^ 1) * BLOCK_M]`（peer CTA 的 tile 首行）是否同一 expert。这正依赖于「tile 不跨 expert 边界」这一对齐保证。

#### 4.2.4 代码实践

**实践目标**：亲手构造一个 3-expert 的非 psum `grouped_layout`，验证「采样 tile 首行即可定位 expert」。

**操作步骤**（可在本地 Python 里运行，无 GPU 也能验证索引逻辑）：

```python
# 示例代码：仅演示 grouped_layout 的构造与索引逻辑，非项目原代码
import torch

num_groups = 3
mk_alignment = 128                 # 假设对齐粒度（见 4.3）
actual_ms = [300, 250, 130]        # 每个 expert 的真实 token 数
aligned_ms = [-(-m // mk_alignment) * mk_alignment for m in actual_ms]  # 向上取整对齐
# aligned_ms = [384, 256, 256]，M = 896

m = sum(aligned_ms)
grouped_layout = torch.empty(m, dtype=torch.int32)
start = 0
for g, (am, alm) in enumerate(zip(actual_ms, aligned_ms)):
    grouped_layout[start : start + am] = g          # 有效行标 expert 号
    grouped_layout[start + am : start + alm] = -1   # padding 行标 -1
    start += alm

# 模拟设备侧「采样 tile 首行定位 expert」（BLOCK_M = 128）
BLOCK_M = 128
for m_block in range(m // BLOCK_M):
    head_row = m_block * BLOCK_M
    expert = int(max(0, grouped_layout[head_row]))   # 即 get_global_idx 的 offset 来源
    is_pad = int(grouped_layout[head_row]) == -1     # 即 is_computation_valid
    print(f"m_block={m_block}, head_row={head_row}, expert={expert}, is_padding_block={is_pad}")
```

**需要观察的现象**：因为每个 `aligned_ms` 都是 128 的倍数，每个 BLOCK_M=128 的 tile 首行恰好落在某个 expert 段的开头，采样得到的 `expert` 单调递增（0,0,0,1,1,2,2 对应 7 个 tile）。

**预期结果**：tile 0/1/2 → expert 0，tile 3/4 → expert 1，tile 5/6 → expert 2；其中 tile 2、4、6 含 padding 行（`is_padding_block` 仅在该 tile 全部由 padding 构成时才为真，部分 padding 的 tile 首行仍是有效 expert 号，padding 行由 `is_computation_valid` 按 warp 粒度跳过）。

> 说明：上面的构造逻辑与 `generate_m_grouped_contiguous` 完全一致，可直接对照 [tests/generators.py:343-355](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py#L343-L355)。

#### 4.2.5 小练习与答案

**练习 1**：为什么设备侧只采样 tile 的**第一行** `grouped_layout[m_block * BLOCK_M]`，而不是遍历 tile 内全部行？

**答案**：因为对齐保证（4.3）使每个 BLOCK_M 大小的 tile **不会跨越 expert 边界**，tile 内所有有效行同属一个 expert，所以首行即代表。遍历全部行既无必要也拖慢内核。

**练习 2**：访问 A 时为什么不需要 group 偏移，访问 B 时却需要？

**答案**：A 被扁平拼接成单个 `[M, K]`，tile 的行号 `m_block * BLOCK_M` 直接就是 A 中的全局行，无需换算。B 没有被拼接，是 `[G, N, K]`，必须先反查出 expert 索引 `g` 再按 `g * N` 偏移，所以要走 `get_global_idx<true>` 带 group 偏移的路径。

**练习 3**：`get_global_idx` 里对 expert 索引用了 `max(0, grouped_layout[...])`，为什么要 `max`？

**答案**：padding 行的标记是 `-1`，若不加 `max` 直接用作偏移会得到负地址、越界访问。`max(0, ...)` 把它钳到 0（读到第 0 个 expert 的数据），真正「不计算」这些行的工作交给 `is_computation_valid` 在 MMA 前兜底，二者配合既安全又省算力。

### 4.3 对齐要求与 psum 布局动机

#### 4.3.1 概念说明

contiguous 布局能成立，前提是 **每个 expert 的段长 \(m_g\) 都向上对齐到某个粒度 \(a\)**（`mk_alignment_for_contiguous_layout`，默认 128）。这个对齐同时承担两个职责：

1. **tile 不跨边界**：保证「一个 BLOCK_M 的 tile 完整落在单个 expert 内」，这是 4.2「采样首行定位 expert」成立的几何前提。若 \(a < \text{BLOCK\_M}\)，一个 tile 可能横跨两个 expert，首行采样就失效了。
2. **padding 干净**：每个段凑整后多出的行，要么标记 `-1`（非 psum），要么由 psum 布局直接跳过。

**psum 布局的动机**：非 psum 的 `[M]` 逐行布局里，那些 `-1` padding 行在物理上仍占据 `A` 的存储，更要命的是它们对应的 **缩放因子 SFA 行也会被分配**——\(M\) 越大、padding 越多，SFA 的浪费越严重。psum（prefix-sum）布局改用 `[num_groups]` 的紧凑数组，`grouped_layout[g]` 存「expert g 的累计结束偏移」，各段在 `A`/`SFA` 中**首尾紧贴、不留 padding 间隙**；变换 SF 时把该布局透传进去，让 SFA 打包跳过 gap 行（见 API 第 214 行 `psum_sfa_layout`）。代价是设备侧要按 expert 顺序扫段、稍复杂的状态机，且需要一个编译期提示 `expected_m_for_psum_layout` 帮助特化。

#### 4.3.2 核心流程

**对齐粒度的取值**（`get_theoretical_mk_alignment_for_contiguous_layout`）：

- **SM90**：固定 128（`kLegacyMKAlignmentForContiguousLayout`）。
- **SM100**：在候选集 \(\{64, 96, \dots, 224\}\)（步长 32）中，取「不小于 `expected_m` 的最小值」，从而在保证覆盖的前提下尽量降低对齐、减少 padding。最终值可通过 `set_mk_alignment_for_contiguous_layout` 覆写。

**psum 模式的设备侧扫段**（`MGroupedContiguousWithPsumLayout` 的 `get_next_block`）：

```
current_group_idx = 0
current_psum_m = grouped_layout[0]          # 第 0 组结束偏移
num_m_blocks = ceil_div(current_psum_m, BLOCK_M)
loop:
    if next_block_idx < (current_m_block_cumsum + num_m_blocks) * num_n_blocks:
        命中当前组，break
    else:
        current_group_idx++
        last_psum_m = align(current_psum_m, BLOCK_M)        # 上一组对齐到 BLOCK_M 的边界
        current_psum_m = grouped_layout[current_group_idx]  # 本组结束偏移（不对齐）
        current_m_block_cumsum += num_m_blocks
        num_m_blocks = ceil_div(current_psum_m - last_psum_m, BLOCK_M)  # 本组的 M-block 数
```

注意 `last_psum_m` 是按 BLOCK_M 对齐的，而 `current_psum_m` 不对齐——这意味着**最后一个 block 可能不满 BLOCK_M**。这个尾巴由 `get_aligned_effective_m_in_block` 处理：它把有效行数向上对齐到 `UMMA_STEP_N=16`（SM100 MMA 的步长），既不浪费整段 padding、又能满足 MMA 对形状的最低要求。

#### 4.3.3 源码精读

对齐粒度旋钮与理论下界：

[heuristics/runtime.hpp:39-57](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/runtime.hpp#L39-L57) —— `get_mk_alignment_for_contiguous_layout` 返回当前旋钮值（默认 128）；`get_theoretical_mk_alignment_for_contiguous_layout` 给出理论下界：SM90 直接返回 128（第 48–49 行），SM100 在 `{224, 192, …, 32}` 中从大到小找一个仍能覆盖 `expected_m` 的最小值（第 51–56 行）。

测试生成器里对齐的实际用法：

[tests/generators.py:332-355](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py#L332-L355) —— 第 333 行 `aligned_ms = [align(actual_m, get_mk_alignment_for_contiguous_layout()) ...]` 把每个 expert 的 token 数向上对齐；第 353 行 `a[actual_end: aligned_end] = 0` 把 padding 行清零，使量化后的 SFA padding 规整（不会读到未初始化内存）。`grouped_layout` 的两种填充——psum 存 `actual_end`（第 348 行），非 psum 存 expert 号 + `-1`（第 350–351 行）——都在这里一目了然。

psum 模式设备侧扫段与尾巴处理：

[scheduler/gemm.cuh:217-237](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L217-L237) —— `MGroupedContiguousWithPsumLayout` 的 `get_next_block`：按 expert 顺序累计 M-block，`last_psum_m` 对齐到 BLOCK_M、`current_psum_m` 取自 `grouped_layout`（不对齐），最后把 `m_block_idx += last_psum_m / BLOCK_M`（第 237 行）把局部 block 索引映射回全局。

[scheduler/gemm.cuh:188-195](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L188-L195) —— `get_aligned_effective_m_in_block`：仅 psum 布局且未启用 `kEnsureZeroPadding` 时，本组最后一个 block 的有效行数取 `current_psum_m - m_block_idx * BLOCK_M`，并对齐到 `UMMA_STEP_N=16`；其余情况返回完整 `BLOCK_M`。`ensure_zero_padding=True` 时则宁可用满 BLOCK_M（靠零填充保证正确），换取更简单的索引。

> 提醒：`expected_m_for_psum_layout` 仅在 psum 模式有意义（API 第 203 行非 psum 时断言它为 `nullopt`），它作为编译期提示帮助内核特化最大 expert 行数。

#### 4.3.4 代码实践

**实践目标**：体会「对齐粒度越小 → padding 越少 → 显存越省」与 psum 布局的关系。

**操作步骤**：

1. 读 [tests/test_fp8_fp4.py:82-84](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_fp8_fp4.py#L82-L84)，看测试如何先 `get_theoretical_mk_alignment_for_contiguous_layout()` 取理论值、再 `set_mk_alignment_for_contiguous_layout(alignment)` 设进去，然后才生成输入。
2. 在 SM100（`get_arch_major()==10`）上分别把对齐设为 32 与 128，构造同样的 `num_groups=4, expected_m_per_group=1024` 输入，统计两种设置下的总 \(M = \sum \tilde{m}_g\) 与 SFA 行数。
3. 对同一组输入，对比 `use_psum_layout=False` 与 `True` 的 SFA 张量大小（可用 `deep_gemm.testing.count_bytes` 观察变换后 SFA）。

**需要观察的现象**：对齐越小，总 M 越接近真实 token 总数，padding 行越少；psum 布局下 SFA 比非 psum 更小（gap 行被跳过）。

**预期结果**：在小 `expected_m`、多 expert 的场景下，psum + 小对齐能显著降低 SFA 显存。若无 SM100 设备，此对比标注为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 SM90 把对齐固定为 128，而 SM100 允许小到 32？

**答案**：SM90 的 1D2D kernel 与其 WGMMA/SF 粒度要求较大的 M 对齐（128），简化了实现；SM100 的 2-CTA cluster 与 UMMA 放宽了几何约束，且 `get_aligned_effective_m_in_block` 能处理不满 BLOCK_M 的尾巴（对齐到 16），故可用更细的对齐（步长 32 的候选集）来减少 padding。

**练习 2**：`get_aligned_effective_m_in_block` 在 psum 布局里把尾巴对齐到 16 而非 BLOCK_M，这会不会算错？

**答案**：不会。它只对本组「最后一个不满 BLOCK_M 的 block」缩短有效行，且对齐到 `UMMA_STEP_N=16`（MMA 的最小步长），保证 MMA 不越界读取、padding 区不参与累加。若开了 `ensure_zero_padding=True`，则改用零填充到完整 BLOCK_M，行为更保守但同样正确。

## 5. 综合实践

把本讲三个模块串起来：构造一个 **3-expert、SM90 风格（对齐 128、非 psum）** 的 FP8 contiguous 输入，调用 `m_grouped_fp8_fp4_gemm_nt_contiguous`，并**逐 expert 校验**输出。

```python
# 示例代码：综合实践，结构对齐 tests/generators.py 与 tests/test_fp8_fp4.py
import torch
from deep_gemm.testing import calc_diff
from tests.generators import (
    generate_m_grouped_contiguous, MajorTypeAB, KernelType, QuantConfig,
)
from deep_gemm.utils import align, get_mk_alignment_for_contiguous_layout
import deep_gemm

num_groups, expected_m_per_group = 3, 1024
n, k = 4096, 4096

# 1) 选定并设置对齐（SM90 理论值恒为 128，SM100 会更小）
alignment = deep_gemm.get_theoretical_mk_alignment_for_contiguous_layout()
deep_gemm.set_mk_alignment_for_contiguous_layout(alignment)

# 2) 生成器返回 m（拼接后总行数）、a=(fp8,sf)、b=(fp8[group,n,k],sf)、grouped_layout、d、ref_d
m, a, b, grouped_layout, d, ref_d = generate_m_grouped_contiguous(
    num_groups, expected_m_per_group, n, k,
    MajorTypeAB.KMajor, MajorTypeAB.KMajor,   # SM90 FP8 要求 A、B 均 K-major
    use_ue8m0=False, use_psum_layout=False, quant_config=QuantConfig(),
)

# 3) 一次 launch 完成全部 expert 的 GEMM
deep_gemm.m_grouped_fp8_fp4_gemm_nt_contiguous(a, b, d, grouped_layout)

# 4) 逐 expert 切片校验（每个 expert 的有效行段；padding 行 ref 已置零，整体比较亦可）
start = 0
for g in range(num_groups):
    end = start + align(expected_m_per_group, get_mk_alignment_for_contiguous_layout())
    diff = calc_diff(d[start:end], ref_d[start:end])
    print(f"expert {g}: rows [{start}:{end}], diff={diff:.5f}")
    assert diff < 1e-3
    start = end
print("All experts passed.")
```

**思考延伸**（可选）：

- 把 `use_psum_layout` 改为 `True`，观察 `grouped_layout` 从 `[M]` 变成 `[num_groups]`，并把校验循环改成按 psum 偏移切片（参考 [tests/test_fp8_fp4.py:98-104](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_fp8_fp4.py#L98-L104)）。
- 在调用前后各打印一次 `grouped_layout` 与 SFA 的形状，直观感受 psum 对 SFA 体积的压缩。

> 说明：上述脚本需在已按 u1-l2 构建好 `deep_gemm._C` 的 SM90/SM100 环境中运行；若无对应硬件，标注为「待本地验证」，但索引与校验逻辑可参照源码静态理解。

## 6. 本讲小结

- **contiguous 布局**把多个变长 expert 的 token 按 M 轴首尾拼接进单个 `[M, K]` 张量，权重保留为 `[G, N, K]`，把一组形状各异的 GEMM 压成 **一次 kernel launch** 的 `[M,N]` tile 调度问题——前提是 N、K 在所有 expert 间固定。
- **`grouped_layout`** 有两种编码：非 psum 的 `[M]` 逐行标记（expert 号 / `-1` padding），psum 的 `[num_groups]` 前缀和（累计结束偏移）。
- 设备侧 `Scheduler::get_global_idx` 在 `MGroupedContiguous` 分支靠 **采样 tile 首行** `grouped_layout[m_block*BLOCK_M]` 反查 expert，`is_computation_valid` 用 `>= 0` 跳过 padding，`is_tma_multicast_valid` 保证 multicast 的相邻 CTA 同属一个 expert。
- **对齐要求**（`mk_alignment_for_contiguous_layout`，SM90 固定 128、SM100 可低至 32）保证 tile 不跨 expert 边界，是「采样首行定位 expert」成立的几何前提。
- **psum 布局**用紧凑前缀和数组消除 padding 间隙、并通过 `psum_sfa_layout` 让 SFA 打包跳过 gap 行，换取更小的显存占用；代价是设备侧按 expert 扫段、以及 `expected_m_for_psum_layout` 编译期提示。

## 7. 下一步学习建议

- **u7-l2 掩码分组 GEMM（解码阶段）**：学习 `m_grouped_*_gemm_nt_masked`。它与本讲的 contiguous 布局互补——当处于 CUDA graph 解码、CPU 无法提前知道每 expert 的 token 数时，用 `masked_m` 标记每 expert 的有效行，内核只算有效部分。对照 `MGroupedMasked` 调度分支与本文的 `MGroupedContiguous`，能看清「为什么解码场景不能用 contiguous」。
- **u7-l3 K 轴分组 GEMM 与 psum 布局**：本讲的 psum 只是 M 轴的「前奏」；K 轴分组（MoE 权重梯度 wgrad）才把 psum 布局（`KGroupedContiguousWithPsumLayout`）用到极致，建议接着读 `get_next_psum_k_group` 与 `check_k_grouped_args`。
- **源码延伸**：想看设备内核如何把 `get_global_idx` / `is_computation_valid` 真正接进 TMA 加载与 WGMMA 流水线，可精读 [`sm90_fp8_gemm_1d2d.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d2d.cuh) 与 [`sm100_fp8_fp4_gemm_1d1d.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh) 中调度器的调用点。
