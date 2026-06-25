# K 轴分组 GEMM 与 psum 布局

## 1. 本讲目标

本讲承接 u7-l1（M 轴分组 contiguous 布局）与 u7-l2（masked 布局），讲解 DeepGEMM 的第三种分组方式：**K 轴分组 GEMM（k-grouped GEMM）**。学完后你应当能够：

- 说清楚「为什么分组轴选 K」、它与 MoE **权重梯度（wgrad）** 场景的对应关系。
- 看懂 `ks_cpu`、`grouped_layout` 这两个核心参数的语义，以及 `check_k_grouped_args` 如何用 `sum_k` 把它们与张量形状对齐。
- 理解 **psum 布局**：为什么需要它，以及 `get_next_psum_k_group` 如何用「对齐累计偏移」把任意长度的 K 段紧凑地拼进单个张量。
- 读懂「非 psum（SM90 NT）」与「psum（SM100 TN）」两条路径在 recipe、k_alignment、SF 打包上的差异。

## 2. 前置知识

### 2.1 wgrad：为什么分组轴会落在 K 上

普通前向 GEMM 是 `D = A @ B`，A 是激活、B 是权重；MoE 前向用 **M 轴分组**（把多个 expert 的 token 沿 M 拼起来），这正是 u7-l1 讲的内容。

而 **反向求权重梯度（weight gradient, wgrad）** 时，梯度公式变成 `∂L/∂W = Aᵀ @ ∂L/∂Y`。此时「多个 expert 的 token」这一维从外层（M）跑到了 **内层（K）**：每个 expert 贡献一段独立的 K 区间，M、N 反而固定。换句话说，**分组轴从 M 翻转到了 K**。所以 DeepGEMM 提供 K 轴分组的 GEMM 专门服务 MoE 的 wgrad。

> 直觉记忆：前向 token 多 → 分 M；反向算权重的 K 段多 → 分 K。

### 2.2 NT/TN 与 MN-major

回顾 u2-l1：函数名后缀 `nt/tn` 描述 A、B 的存储布局。本讲的 K 轴分组有两个入口：

- SM90：`k_grouped_fp8_gemm_nt_contiguous`（NT，A、B 都 K-major）。
- SM100：`k_grouped_fp8_gemm_tn_contiguous`（TN，A、B 都 **MN-major**）。

这一点和稠密 GEMM 不同——K 轴分组里两代架构的「主维」是相反的，原因会在 4.1 讲清。

### 2.3 缩放因子（SF）与 UE8M0 打包

回顾 u2-l2：FP8 必须逐块缩放，粒度由 `recipe=(gran_mn, gran_k)` 描述。SM100 用 **打包 UE8M0**（4 个 8 位指数压进一个 `int32`）来存缩放因子。本讲的 SF 会按 K 段组织，打包逻辑比稠密版更复杂，需要额外理解「K 段间如何对齐再打包」。

### 2.4 你需要带过来的术语

- `grouped_layout`：一个 `int32` 张量，长度 = expert 数，本讲里它的语义会随「是否 psum」而变。
- `ks_cpu`：一个 CPU 端 `list[int]`，存放每个 expert 的真实 K 长度。
- `k_alignment`：K 段对齐粒度，决定 padding 与 TMA 对齐。
- `GemmType`：设备侧调度器（Scheduler）的分支开关，本讲涉及 `KGroupedContiguous` 与 `KGroupedContiguousWithPsumLayout`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`csrc/apis/gemm.hpp`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp) | K 轴分组的 Python↔C++ 入口：参数校验、SF 变换、按架构派发；含 `check_k_grouped_args` |
| [`csrc/jit_kernels/heuristics/runtime.hpp`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/runtime.hpp) | `HeuristicsRuntime` 单例，提供 `k_alignment` 的 get/set 与理论值推导 |
| [`csrc/apis/layout.hpp`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/layout.hpp) | `transform_k_grouped_sf_into_required_layout`：把用户 SF 变换成 kernel 所需的 K 分组打包布局 |
| [`deep_gemm/include/deep_gemm/scheduler/gemm.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh) | 设备侧 `Scheduler`，含本讲核心 `get_next_psum_k_group` |
| [`tests/generators.py`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py) | `build_psum_layout_from_ks` / `generate_k_grouped_contiguous_psum`：psum 输入构造，是 `get_next_psum_k_group` 的 Python 镜像 |

---

## 4. 核心概念与源码讲解

### 4.1 K 轴分组 wgrad：为什么是 K 轴、1D1D 与 recipe 约束

#### 4.1.1 概念说明

K 轴分组的算子形态是：

\[
D[g] = C[g] + A[\text{k\_start}_g : \text{k\_end}_g]^\top \, @ \, B[\text{k\_start}_g : \text{k\_end}_g], \quad g = 0 \dots G-1
\]

其中：

- M、N 对所有 expert **固定不变**（这是硬约束，因为 D 的形状是 `[num_groups, M, N]`）。
- 每个 expert g 占用连续张量 `A`、`B` 中的一段 K 区间 `[k_start_g, k_end_g)`。
- `C[g]` 是可累加的初值，输出 `D[g]` 与之形状相同。

这对应 MoE 反向：M=N 不变，多个 expert 的 K 段拼在一起。输入 `A`、`B` 都是一根「长 K」的连续张量（`A` 形状 `[sum_k, M]`，`B` 形状 `[sum_k, N]`），靠 `ks_cpu` 告诉 host 每段多长，靠 `grouped_layout` 告诉 device 每段边界。

#### 4.1.2 核心流程

把一次 K 轴分组调用串起来：

1. **host 校验**：recipe 必须是 `(1,1,gran_k)`；读 `k_alignment`；用 `check_k_grouped_args` 算出 `sum_k` 并核对张量形状。
2. **SF 变换**：对 A、B 的 SF 各调用 `transform_k_grouped_sf_into_required_layout`，把逐块缩放因子按 K 段重排成 TMA 友好的打包布局。
3. **派发**：SM100 走 `sm100_k_grouped_fp8_gemm_1d1d`（TN，MN-major），SM90 走 `sm90_k_grouped_fp8_gemm_1d1d`（NT，K-major）。
4. **device 调度**：`Scheduler` 的 K-grouped 分支按 wave 依次领出每个 expert 的 tile，用 `get_global_idx` 给 A/B/D/SF 计算带段偏移的全局地址。

两条路径里都有两个关键硬约束：**必须用 1D1D kernel**，**recipe 的 `gran_mn` 必须为 1**。下面看源码如何断言它们。

#### 4.1.3 源码精读

先看 SM100（TN）入口对 recipe 与对齐的约束。`recipe=(gran_mn_a, gran_mn_b, gran_k)`：

[csrc/apis/gemm.hpp:299-314](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L299-L314) 这段断言 K 轴分组必须是 1D1D kernel，且 `gran_mn` 两侧都为 1、`gran_k` 只能取 32 或 128：

```cpp
static void k_grouped_fp8_gemm_tn_contiguous(...) {
    // Must be 1D1D kernel
    DG_HOST_ASSERT(std::get<0>(recipe) == 1 and std::get<1>(recipe) == 1);
    const int gran_k = std::get<2>(recipe);
    DG_HOST_ASSERT(gran_k == 32 or gran_k == 128);
    const int k_alignment = heuristics_runtime->get_mk_alignment_for_contiguous_layout();
    DG_HOST_ASSERT(k_alignment % 32 == 0);
```

`gran_mn == 1` 的含义：**缩放因子在 M（或 N）方向不分组**，只在 K 方向按 `gran_k` 切块。这是因为 wgrad 里每个 expert 的 K 段要被精确切片，SF 必须沿 K 对齐到段边界，而 MN 方向整体保持不变，所以不在 MN 上再分。

再看 SM90（NT）入口，约束更严格——recipe 写死、且 **NT 路径不支持 psum**：

[csrc/apis/gemm.hpp:357-361](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L357-L361)

```cpp
    // Must be 1D1D kernel
    DG_HOST_ASSERT(recipe == std::make_tuple(1, 1, 128));
    // No psum on FP8 NT
    DG_HOST_ASSERT(not use_psum_layout and ks_cpu.has_value() and not ks_cpu.value().empty());
```

两条路径对比小结：

| 维度 | SM90 `..._nt_contiguous` | SM100 `..._tn_contiguous` |
| --- | --- | --- |
| 布局 / 主维 | NT，A/B 均 K-major | TN，A/B 均 MN-major |
| recipe | 写死 `(1,1,128)` | `(1,1,gran_k)`，gran_k∈{32,128} |
| k_alignment | 硬编码 128 | `get_mk_alignment_for_contiguous_layout()`（≥32） |
| psum 布局 | **不支持**（必须 `ks_cpu` 非空） | 支持 |
| 缩放因子 | FP32 | 打包 UE8M0 |

「SM90 NT 只能 128、不支持 psum」的根本原因在于 SM90 的 WGMMA 强制 K-major、SF 是 FP32 且软件相乘（见 u6-l2）；而 psum 这种「段间紧凑、段首对齐」的布局需要 SM100 UMMA 的硬件 SF 与 MN-major TMA 才能高效吸收。这也解释了为什么两代架构的主维相反：SM90 必须把 K 放成连续内维（K-major），SM100 则把 MN 放成连续内维（MN-major）。

#### 4.1.4 代码实践

**实践目标**：确认两代架构在 K 轴分组上走的是不同的函数与主维。

**操作步骤**：

1. 打开 `tests/test_fp8_fp4.py` 中的 `test_k_grouped_gemm_contiguous`，看它如何按架构选择函数：

[tests/test_fp8_fp4.py:196-197](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_fp8_fp4.py#L196-L197) 这里用 `get_arch_major()` 在 `nt`（SM90）与 `tn`（SM100）之间二选一。

2. 把它和上面两张表对照，确认 SM90→NT、SM100→TN。

**需要观察的现象**：在 SM90 机器上 `get_arch_major()==9`，函数名是 `k_grouped_fp8_gemm_nt_contiguous`；在 SM100 上则走 `tn`。

**预期结果**：理解 K 轴分组「一台机器一个入口」，且两入口的主维与对齐策略不同。（若你手头没有 SM90/SM100 卡，此项为「源码阅读型实践」，能讲清两条派发即可。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 K 轴分组的 `recipe` 强制 `gran_mn=1`，而稠密 GEMM 可以用 `gran_mn=128`？

**参考答案**：稠密 GEMM 的 M、N 是均匀大块，可以按 128 分组缩放以省 SF 带宽；K 轴分组里每个 expert 的 K 段是独立的、长度可变，必须沿 K 精确切到 `gran_k` 粒度，而 MN 方向所有 expert 共享相同的 M/N 范围、不需要也不应该在 MN 上再切块，故 `gran_mn=1`。

**练习 2**：`k_grouped_fp8_gemm_nt_contiguous` 为什么断言 `not use_psum_layout`？

**参考答案**：psum 布局需要 SM100 的 MN-major TMA 与硬件吸收的 UE8M0 SF（见 4.3）；SM90 走 NT、K-major、FP32 软件缩放，不具备这些条件，因此 NT 路径不支持 psum，必须显式传入非空的 `ks_cpu`。

---

### 4.2 ks_cpu 与 grouped_layout 的语义及 sum_k 校验

#### 4.2.1 概念说明

K 轴分组有两个「描述符」张量，它们是 host 与 device 之间的契约：

- **`ks_cpu`**：`std::optional<std::vector<int>>`，CPU 端的「每个 expert 的真实 K 长度」列表，长度 = `num_groups`。可空（psum + 无需同步时）。
- **`grouped_layout`**：`torch::Tensor`（`int32`，长度 = `num_groups`）。非 psum 时它直接存每段 K 长度；psum 时它存每段的**累计结束偏移**（end offset）。设备 kernel 只读这一个张量。

`grouped_layout` 的双重身份是本模块的关键：**同一块内存，在两种布局下语义不同**，必须配合 `use_psum_layout` 来解读。

#### 4.2.2 核心流程

`check_k_grouped_args` 做三件事，并用「算出一个 `sum_k` 返回」把校验与派发串起来：

1. 校验 `grouped_layout` 连续、是 `int32`、长度恰为 `num_groups`。
2. **分支 A（有 `ks_cpu`）**：逐项断言 `k % k_alignment == 0`，累加得到 `sum_k` 返回。
3. **分支 B（无 `ks_cpu`）**：断言必须 `use_psum_layout`，返回调用方传入的兜底 `sum_k_if_ks_cpu_missing`（取自张量第 0 维实际大小）。

返回的 `sum_k` 随后被用来核对 `A`、`B` 的形状（`A` 是 `[sum_k, M]`，`B` 是 `[sum_k, N]`）。

#### 4.2.3 源码精读

[csrc/apis/gemm.hpp:48-69](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L48-L69) 这是 `check_k_grouped_args` 的全部逻辑：

```cpp
static int check_k_grouped_args(const std::optional<std::vector<int>>& ks_cpu,
                                const torch::Tensor& grouped_layout,
                                const int& num_groups,
                                const bool& use_psum_layout,
                                const int& k_alignment,
                                const int& sum_k_if_ks_cpu_missing = 0) {
    DG_HOST_ASSERT(grouped_layout.is_contiguous());
    DG_HOST_ASSERT(grouped_layout.scalar_type() == torch::kInt);
    DG_HOST_ASSERT(static_cast<int>(grouped_layout.numel()) == num_groups);

    if (ks_cpu.has_value() and not ks_cpu.value().empty()) {
        DG_HOST_ASSERT(static_cast<int>(ks_cpu.value().size()) == num_groups);
        int sum_k = 0;
        for (const auto k: ks_cpu.value()) {
            DG_HOST_ASSERT(k % k_alignment == 0);   // 每段 K 必须对齐
            sum_k += k;
        }
        return sum_k;
    }
    DG_HOST_ASSERT(use_psum_layout);                 // 无 ks_cpu ⇒ 必须 psum
    return sum_k_if_ks_cpu_cpu_missing;
}
```

注意第 62 行的 `k % k_alignment == 0`：**只要提供了 `ks_cpu`，每段 K 就必须是对齐的**。这是「非 psum 语义」的代价——你必须把每段权重物理地 padding 到 `k_alignment` 的倍数。

而 psum 语义恰恰放松了这一点：不提供 `ks_cpu` 时，每段 K 可以是任意长度，对齐只发生在「段首」。这正是 4.3 要讲的省显存来源。

调用方如何拿到 `sum_k_if_ks_cpu_missing`？看 SM100（TN）入口：

[csrc/apis/gemm.hpp:317-323](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L317-L323)

```cpp
    const auto [num_groups, m, n] = get_shape<3>(d);
    const auto [sum_k_ , m_] = get_shape<2>(a.first);
    const auto [sum_k__, n_] = get_shape<2>(b.first);

    const int sum_k = check_k_grouped_args(ks_cpu, grouped_layout, num_groups,
                                           use_psum_layout, k_alignment,
                                           static_cast<int>(a.first.size(0)));
```

psum 时 `ks_cpu` 为空，兜底值就取 `A` 张量实际的行数 `a.first.size(0)`（即 `total_k`，已经按对齐后的累计偏移分配好）。随后 `sum_k == sum_k_ == sum_k__` 断言把 `A`、`B` 两条「长 K」对齐。

`k_alignment` 本身从哪来？看 `HeuristicsRuntime`：

[csrc/jit_kernels/heuristics/runtime.hpp:10-15](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/runtime.hpp#L10-L15) 默认是 128（legacy 兼容值），可通过 `set_mk_alignment_for_contiguous_layout` 调小（SM100 上可低至 32）：

```cpp
class HeuristicsRuntime {
    static constexpr int kLegacyMKAlignmentForContiguousLayout = 128;
    ...
    int mk_alignment_for_contiguous_layout = kLegacyMKAlignmentForContiguousLayout;
```

#### 4.2.4 代码实践

**实践目标**：验证「有 `ks_cpu` ⇒ 每段必须对齐」这条规则，并观察断言触发。

**操作步骤**（源码阅读 + 思考，无需 GPU）：

1. 设想 `num_groups=2`、`k_alignment=128`，传入 `ks_cpu=[256, 200]`。
2. 走查 `check_k_grouped_args`：`200 % 128 == 72 ≠ 0`，第 62 行 `DG_HOST_ASSERT` 失败，pybind11 会把它翻译成 Python 异常（见 u2-l3）。
3. 改成 `ks_cpu=[256, 256]`：通过，`sum_k = 512`。

**需要观察的现象 / 预期结果**：不对齐的 `ks_cpu` 直接抛异常；对齐的才返回 `sum_k`。这解释了为什么 psum 要存在——当你**无法**把每段 K padding 到 128 时，只能走 psum。

#### 4.2.5 小练习与答案

**练习 1**：`ks_cpu` 与 `grouped_layout` 都描述「每段多长」，为什么要两个？

**参考答案**：`ks_cpu` 是 host 端 CPU 可见的 `vector<int>`，便于 host 做 `sum_k` 校验、算 `k % k_alignment`；`grouped_layout` 是 device 可见的 GPU `int32` 张量，kernel 在 SM 上直接读取它来定位段边界。两者职责不同：一个管 host 校验与派发，一个管 device 寻址。psum 无 `ks_cpu` 时，`grouped_layout` 还兼任「累计偏移」语义（见 4.3）。

**练习 2**：为什么 psum 路径允许 `ks_cpu` 为空？

**参考答案**：psum 的段边界信息已经全部编码进 `grouped_layout` 的累计偏移里（device 自己用 `get_next_psum_k_group` 推导 `k_start/k_end`），host 不再需要逐段 K 来做对齐断言，因此 `ks_cpu` 可空，`sum_k` 改由张量实际行数兜底。

---

### 4.3 psum 布局：按对齐累计偏移组织变长 K 段

#### 4.3.1 概念说明

**psum（partial sum）布局**解决一个问题：非 psum 要求每段 K 对齐到 `k_alignment`，导致段尾出现大量 padding；同时缩放因子必须每段独立打包到 `gran_k*4`，浪费 SF 显存。psum 的思路是：

- **段首对齐、段尾紧贴**：每段的真实数据紧接上一段（对齐后的）起点，padding 只出现在「段间间隙」，且 padding 必须**清零**（否则会污染累加）。
- 用一个 `grouped_layout` 数组记录每段的**累计结束偏移** `end_g`，device 据此实时推导每段的 `[k_start_g, k_end_g)`。

形式化地，设对齐粒度为 \(A\)（即 `kKAlignment`），各段真实 K 为 \(k_0, k_1, \dots\)，令 \(end_{-1}=0\)，则：

\[
k\_start_g = \mathrm{align}(end_{g-1},\, A), \qquad end_g = k\_start_g + k_g, \qquad shape\_k_g = end_g - k\_start_g = k_g
\]

其中 \(\mathrm{align}(x, A) = \lceil x / A \rceil \cdot A\) 把 `x` 向上取整到 `A` 的倍数。注意 `shape_k_g` 恰好等于真实 `k_g`——**段内没有 padding，padding 只在段间的 `[end_{g-1}, k_start_g)` 间隙里**。

这个递推在 device（`get_next_psum_k_group`）和 host/Python（`build_psum_layout_from_ks`）两侧是**镜像**的。

#### 4.3.2 核心流程

device 侧 Scheduler 在 K-grouped 分支里，每领出一组 tile 就推进一个 expert：

1. 构造时调一次 `get_next_psum_k_group` 取第 0 段的 `k_start/k_end/shape_k`。
2. `get_next_block` 的 while 循环里，当当前段的所有 tile 被领完（`next_block_idx >= (num_valid_groups+1)*num_blocks`），就推进 `current_group_idx` 并再调 `get_next_psum_k_group` 取下一段。
3. `get_global_idx` 在 `IndexType::K` 时用 `current_k_start` 作为 K 维偏移，让 A/B 的 TMA 命中正确段；`IndexType::SF_K` 用 `current_sf_k_cumsum` 给 SF 寻址。

省显存的两处：

- **数据（A/B）**：psum 放松了 `k % k_alignment == 0` 的硬约束，用户可存真实（短）权重，padding 只填段间间隙。
- **缩放因子（SF）**：整个 SF 缓冲被**连续打包**，每段边界最多引入 3 个 padding 元素，而不是每段独立 padding 到 `gran_k*4`。多段小 K 时 SF 显存节省显著（见 4.3.5 的算例）。

#### 4.3.3 源码精读

先看 device 核心 `get_next_psum_k_group`，它与上面的公式逐行对应：

[deep_gemm/include/deep_gemm/scheduler/gemm.cuh:74-85](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L74-L85)

```cpp
CUTLASS_DEVICE void get_next_psum_k_group(uint32_t &group_idx, uint32_t &shape_k,
                                           uint32_t &k_start, uint32_t &k_end) const {
    // NOTES: `grouped_layout[i]` is the psum end offset (K elements);
    //        each group starts at `align(prev_end, kKAlignment)`. Skip empty groups.
    for (; group_idx < kNumGroups; ++ group_idx) {
        const auto next_k_end = static_cast<uint32_t>(grouped_layout[group_idx]);
        k_start = math::align(k_end, kKAlignment);
        shape_k = next_k_end - k_start;
        k_end = next_k_end;
        if (shape_k > 0)
            break;
    }
}
```

逐行解读：

- `grouped_layout[group_idx]` 读出本段累计结束偏移 `next_k_end`（即公式里的 `end_g`）。
- `k_start = align(k_end, kKAlignment)`：把**上一段的 `k_end`** 向上对齐到 `A`，得到本段起点。
- `shape_k = next_k_end - k_start`：本段真实长度。
- `k_end = next_k_end`：滚动更新，供下一段对齐用。
- `shape_k > 0` 跳过空段（`k==0` 的 expert）。

`math::align` 的定义就是向上取整到倍数：

[deep_gemm/include/deep_gemm/common/math.cuh:27-29](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/common/math.cuh#L27-L29)

```cpp
CUTLASS_HOST_DEVICE T align(T a, T b) {
    return (kDoCeilAlignment ? ceil_div(a, b) : (a / b)) * b;
}
```

对照非 psum 的版本 `get_next_k_group`，它只读每段长度、不做对齐滚动：

[deep_gemm/include/deep_gemm/scheduler/gemm.cuh:66-72](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L66-L72) 非 psum 直接 `shape_k = grouped_layout[group_idx]`（每段长度），靠 host 保证已对齐；psum 读的是「累计偏移」并自行对齐。

Scheduler 持有 psum 专用状态：

[deep_gemm/include/deep_gemm/scheduler/gemm.cuh:58-63](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L58-L63)

```cpp
// Only used for k-grouped layout
uint32_t current_shape_k, current_num_valid_groups = 0, current_k_cumsum = 0, current_sf_k_cumsum = 0;
// NOTES: only used by the non-psum path; the psum path never reads them.
uint32_t next_group_idx, next_shape_k;
// Only used for `KGroupedContiguousWithPsumLayout`
uint32_t current_k_start = 0, current_k_end = 0;
```

`current_k_start`/`current_k_end` 是 psum 专属；非 psum 用 `current_k_cumsum`（逐段长度累加）。这两套字段在 `get_global_idx` 里分流，psum 用 `current_k_start`：

[deep_gemm/include/deep_gemm/scheduler/gemm.cuh:166-180](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/scheduler/gemm.cuh#L166-L180)

```cpp
} else if constexpr (is_k_grouped_contiguous(kGemmType)) {
    auto offset = 0;
    if constexpr (kWithGroupOffset) {
        ...
        } else if constexpr (kIndexType == IndexType::K) {
            if constexpr (kGemmType == GemmType::KGroupedContiguousWithPsumLayout)
                offset = current_k_start;     // psum: 段首对齐偏移
            else
                offset = current_k_cumsum;    // 非 psum: 累计长度
        } else if constexpr (kIndexType == IndexType::SF_K) {
            offset = current_sf_k_cumsum;     // SF 的 K 偏移
        }
    }
    return offset + block_idx * block_size;
```

那么 `GemmType` 如何被选成 psum？看 SM100 派发：

[csrc/jit_kernels/impls/sm100_fp8_fp4_gemm_1d1d.hpp:333-334](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_fp8_fp4_gemm_1d1d.hpp#L333-L334)

```cpp
    const auto desc = GemmDesc {
        .gemm_type = use_psum_layout ? GemmType::KGroupedContiguousWithPsumLayout
                                     : GemmType::KGroupedContiguous,
```

`use_psum_layout` 这个布尔开关一路传到设备 Scheduler 的 `GemmType`，决定走 psum 还是非 psum 的所有分支。`GemmType` 枚举本身定义在：

[deep_gemm/include/deep_gemm/common/types.cuh:20-28](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/common/types.cuh#L20-L28) 注意 `KGroupedContiguous(=3)` 与 `KGroupedContiguousWithPsumLayout(=6)` 是两个独立值，`is_k_grouped_contiguous` 同时返回 `true`。

现在看 host 侧如何把用户 SF 变换成 psum 打包布局：

[csrc/apis/layout.hpp:107-117](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/layout.hpp#L107-L117) K 轴分组的 SF 变换按 (dtype, arch) 四路派发，psum 的核心在打包函数里：

```cpp
    // FP32 on SM100
    if (sf.scalar_type() == torch::kFloat and arch_major == 10)
        return get_k_grouped_mn_major_tma_aligned_packed_ue8m0_tensor(
            sf, grouped_layout, ks_cpu, gran_k, k_alignment, use_psum_layout);
    // INT (already packed UE8M0) on SM100
    if (sf.scalar_type() == torch::kInt and arch_major == 10)
        return check_k_grouped_packed_ue8m0_tensor(
            sf, grouped_layout, ks_cpu, gran_k, k_alignment, use_psum_layout);
```

最后看省显存的关键——打包后的 SF 行数 `packed_sf_k` 如何计算：

[csrc/jit_kernels/impls/smxx_layout.hpp:277-289](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/smxx_layout.hpp#L277-L289)

```cpp
    int packed_sf_k = 0;
    if (has_synced_ks) {
        int ref_sf_k = 0;
        for (const auto k: ks_cpu.value()) {
            ref_sf_k   += ceil_div(k, gran_k);
            packed_sf_k += ceil_div(k, gran_k * 4);   // 非 psum: 每段独立打包到 gran_k*4
        }
        DG_HOST_ASSERT(use_psum_layout or ref_sf_k == sf_k);
    } else {
        packed_sf_k = (sf_k + num_groups * 3) / 4;     // psum: 整块连续打包, 每段边界≤3 slack
    }
```

两个分支的对比正是 psum 省 SF 显存的根据：

- **非 psum**：每段独立 `ceil_div(k_i, gran_k*4)`，即每段 SF 都要 padding 到能整除 `gran_k*4`（=4 个打包单元）。
- **psum**：把整个 SF 缓冲当成一根连续流，`ceil_div(sf_k + 3*G, 4)`，每段边界最多补 3 个 slack 元素让打包对齐。

Python 侧的镜像 `build_psum_layout_from_ks` 与 device 公式完全一致，便于离线验证：

[tests/generators.py:480-487](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/generators.py#L480-L487)

```python
def build_psum_layout_from_ks(real_ks: List[int], k_alignment: int) -> List[int]:
    # Convert raw per-group K sizes to psum end offsets
    psum, prev_end = [], 0
    for k in real_ks:
        end = align(prev_end, k_alignment) + k   # = align(k_end_prev, A) + k  == device 的 end_g
        psum.append(end)
        prev_end = end
    return psum
```

#### 4.3.4 代码实践

**实践目标**：手算 psum 布局下每段的 `k_start/k_end`，并验证 device 与 Python 两侧公式一致；同时量化 SF 显存节省。

**操作步骤**（纯 Python，无需 GPU）：

1. 复制下面这段「最小复现」，它把 device 的 `get_next_psum_k_group` 翻译成 Python，并和 `build_psum_layout_from_ks` 对照：

```python
# 示例代码：psum 偏移推导（对应 device get_next_psum_k_group）
import math
def align(x, a):            # 对应 math::align
    return math.ceil(x / a) * a

def build_psum_layout_from_ks(real_ks, A):   # host/Python 镜像
    psum, prev_end = [], 0
    for k in real_ks:
        end = align(prev_end, A) + k
        psum.append(end); prev_end = end
    return psum

# device 侧的 get_next_psum_k_group：从 psum 偏移反推每段 k_start/shape_k
def device_trace(psum_offsets, A):
    k_end = 0; k_start = 0; rows = []
    for g, next_k_end in enumerate(psum_offsets):
        k_start = align(k_end, A)
        shape_k = next_k_end - k_start
        k_end = next_k_end
        if shape_k > 0:
            rows.append((g, k_start, k_end, shape_k))
    return rows

real_ks = [100, 200, 50]            # 任意非对齐 K
A = 128                              # kKAlignment
psum = build_psum_layout_from_ks(real_ks, A)
print("grouped_layout(psum offsets) =", psum)        # 传给 device 的张量
for g, ks, ke, sk in device_trace(psum, A):
    print(f"expert {g}: k_start={ks}, k_end={ke}, shape_k={sk}")  # shape_k 应 == real_ks[g]
```

2. 运行后应看到每段 `shape_k` 恰好等于 `real_ks[g]`，且 `k_start` 都被对齐到 128 的倍数；段间间隙 `[end_{g-1}, k_start_g)` 就是 padding（必须清零）。

3. **SF 节省算例**：4 个 expert，每段 `k=128`，`gran_k=128`：
   - 非 psum：每段 `ceil_div(128, 128*4)=ceil_div(128,512)=1` 打包行，共 **4** 行。
   - psum：`sf_k = 4`，`packed_sf_k = (4 + 4*3)/4 = 16/4 = 4`……此处 4 段恰好填满打包行；若改为 2 个 expert 各 `k=128`：非 psum 仍 2 行，psum `(2+6)/4=2`，相同。把 expert 数加到很多、每段很小（如 8 段各 `k=128`）时，非 psum 恒为 8 行，psum 为 `(8+24)/4=8`——可见 psum 的优势集中在「多段且每段 SF 行能塞进同一打包行」时；而**真正的硬收益是放松了对齐约束**，让你不必把每段权重 padding 到 128。

**需要观察的现象 / 预期结果**：
- `shape_k` 严格等于输入 `real_ks[g]`（段内无 padding）。
- 所有 `k_start` 是 `A` 的倍数。
- 与 `tests/test_bf16.py` 中 `check_bf16_psum_zero_padding` 的断言一致：段间 gap 行的 A 与 D 必须全零。

**待本地验证**：上述 SF 打包行数算例建议在真实 SM100 上跑 `tests/test_bf16.py::test_k_grouped_gemm_contiguous`（设 `DG_PRINT_CONFIGS=1`）对照；若没有 SM100 卡，则以源码公式与 Python 复现为准。

> 想看真实调用形态，可读 [tests/test_bf16.py:171-199](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_bf16.py#L171-L199)：它在 psum 时分别用 `None`、`[]`、真实 `ks_cpu` 三种方式调用，并断言数值一致——这正是 psum 允许 `ks_cpu` 为空的端到端验证。

#### 4.3.5 小练习与答案

**练习 1**：给定 `real_ks=[100, 200, 50]`、`A=128`，手算 `grouped_layout`（psum 偏移）与每段 `k_start/k_end`。

**参考答案**（注意 `align(0,128)=0`，起始无 padding）：
- `end_0 = align(0,128)+100 = 0+100 = 100`。
- `end_1 = align(100,128)+200 = 128+200 = 328`。
- `end_2 = align(328,128)+50 = 384+50 = 434`。
- 故 `grouped_layout = [100, 328, 434]`，`total_k = align(434,128)=512`。
- 段 0：`k_start=0, k_end=100, shape_k=100`；段 1：`k_start=128, k_end=328, shape_k=200`；段 2：`k_start=384, k_end=434, shape_k=50`。间隙 `[100,128)`、`[328,384)`、`[434,512)` 为 padding（须清零）。

**练习 2**：为什么 psum 的 padding 行必须清零？参考 `tests/test_bf16.py` 的 `check_bf16_psum_zero_padding`。

**参考答案**：因为 device kernel 会按对齐后的 `k_start` 用 TMA 读取整块（含间隙），间隙里的 A/B 数据会进入乘加累加；若非零就会污染 `D[g]`。`check_bf16_psum_zero_padding` 正是断言输入 padding 行 `a[current_m:aligned_m]` 与输出 padding 行都为零。对 FP8，对应要求调用方对 padding 区的 SFA 清零以保证零贡献（见 u7-l2 的 `ensure_zero_padding` 思路）。

**练习 3**：非 psum 与 psum 的 `grouped_layout` 内容有何不同？

**参考答案**：非 psum 时 `grouped_layout[g]` 是第 g 段的**长度**（host 已保证 `% k_alignment == 0`），device 用 `get_next_k_group` 直接读、用 `current_k_cumsum` 累加定位；psum 时 `grouped_layout[g]` 是第 g 段的**累计结束偏移**，device 用 `get_next_psum_k_group` 读、用 `align(prev_end, A)` 推出 `k_start`，靠 `current_k_start` 定位。

---

## 5. 综合实践

**任务**：用本讲三个模块的知识，手动复现一次 K 轴分组 psum 调用的「全流程契约」，不依赖 GPU。

1. **选定问题**：`num_groups=3`，`M=64`，`N=64`，`real_ks=[96, 160, 64]`，`k_alignment=128`，`gran_k=128`，SM100（TN，MN-major）。
2. **算 `grouped_layout`**：用 `build_psum_layout_from_ks`（或 4.3.4 的 Python 复现）算出 psum 偏移数组与 `total_k`。
3. **构造数据契约**：说明 `A` 形状应为 `[total_k, M]`、`B` 形状 `[total_k, N]`、`D`/`C` 形状 `[3, M, N]`；标出每段真实数据落在 `A` 的哪几行、哪些间隙必须清零。
4. **走 host 校验**：因为走 psum、`ks_cpu=None`，说明 `check_k_grouped_args` 会走分支 B，`sum_k` 取 `A.size(0)=total_k`，且**不会**触发 `k % k_alignment == 0` 断言（这正是 psum 的好处）。
5. **走 device 调度**：写出 `get_next_psum_k_group` 会依次返回的三组 `(k_start, k_end, shape_k)`，并解释 `get_global_idx` 在 `IndexType::K` 时如何用 `current_k_start` 把第 g 段的 tile 映射到 `A` 的正确行。
6. **反思**：若改用非 psum，`real_ks` 必须改成什么？padding 总量会变成多少？

**预期结果**：你能不查源码就讲清「`ks_cpu`/`grouped_layout` 两个描述符 → host 校验 → device 段定位 → SF 连续打包」这条完整链路，并说清 psum 相对非 psum 在「放松对齐约束」与「SF 连续打包」两处的设计动机。

---

## 6. 本讲小结

- **K 轴分组服务 MoE wgrad**：前向分 M、反向算权重的 K 段独立 → 分 K；M、N 固定，每 expert 占 `A`/`B` 的一段 K。
- **两代架构两入口**：SM90 走 NT（K-major，recipe 写死 `(1,1,128)`，FP32 SF，**不支持 psum**）；SM100 走 TN（MN-major，recipe `(1,1,32或128)`，UE8M0 SF，**支持 psum**）。K 轴分组一律 1D1D、`gran_mn=1`。
- **`ks_cpu` 与 `grouped_layout`**：前者 host 端逐段长度（用于 `sum_k` 校验与 `k%k_alignment` 断言），后者 device 端寻址张量；psum 时后者语义变为「累计结束偏移」。
- **`check_k_grouped_args`**：有 `ks_cpu` 则逐段断言对齐并累加 `sum_k`；无 `ks_cpu` 则强制 `use_psum_layout` 并用张量行数兜底。
- **psum 布局**：段首对齐、段尾紧贴，padding 只在段间间隙且须清零；`get_next_psum_k_group` 用 `k_start = align(prev_end, A)`、`shape_k = end_g - k_start` 实时推导，与 Python `build_psum_layout_from_ks` 镜像。
- **psum 的收益**：放松「每段 K 必须对齐」的硬约束（用户可存真实短权重），并让整个 SF 缓冲连续打包（每段边界 ≤3 slack），而非每段独立 padding 到 `gran_k*4`。

## 7. 下一步学习建议

- **SM100 设备内核内部**：本讲的 psum 调度最终落到 `sm100_k_grouped_fp8_gemm_1d1d`，建议接着读 `deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh`，看 device 如何用 `current_k_start` 切换 TMA 描述符、跨段推进软件流水线（对应 u6-l1/u6-l3 的双缓冲与栅栏）。
- **SF 打包内核**：`get_k_grouped_mn_major_tma_aligned_packed_ue8m0_tensor` 内部用 `PackFP32IntoUE8M0Runtime`（一个独立的 JIT kernel）做打包，可结合 u3-l2（代码生成）理解它如何把 FP32→UE8M0 的逐段打包编译成 device kernel。
- **Mega MoE 的联动**：K 轴分组的 wgrad 是 Mega MoE 反向的一部分，学完本讲可进入 u8（Mega MoE 融合内核），看 Linear1/Linear2 的前向（M 分组）与反向（K 分组）如何在一个 mega-kernel 里被调度。
- **跑通测试**：在 SM100 上运行 `tests/test_bf16.py::test_k_grouped_gemm_contiguous` 与 `tests/test_fp8_fp4.py::test_k_grouped_gemm_contiguous`，设 `DG_PRINT_CONFIGS=1` 与 `DG_JIT_DEBUG=1` 观察选中的 config 与段调度。
