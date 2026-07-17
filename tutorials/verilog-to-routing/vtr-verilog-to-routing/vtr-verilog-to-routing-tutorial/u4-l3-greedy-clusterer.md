# 贪心聚簇器 GreedyClusterer

## 1. 本讲目标

上一讲（u4-l2）我们解决了「聚簇器面对的分子从哪里来」：`Prepacker` 把架构里的 pack pattern（如进位链）和原子网表组合成一个个 `t_pack_molecule`。本讲回答下一个问题：**给定一堆分子，如何把它们装进一个个合法的逻辑块簇里？**

VPR 的答案是一个「贪心」算法：一次只造一个簇，从挑一个种子分子开始，然后不断把「增益最高」的候选分子吸进来，直到塞不下或没有更好的候选为止。本讲学完后，你应当能够：

1. 说清 `GreedyClusterer` 一次聚簇迭代的完整流程：选种子 → 长簇 → 合法化。
2. 区分 `GreedySeedSelector`（决定「从谁开始」）与 `GreedyCandidateSelector`（决定「下一个吸谁进来」）的职责，并写出二者的协作顺序。
3. 理解「增益（gain）」是如何由共享、连接、时序、吸引力组等多项加权而成的，以及 APPack / 吸引力组如何提升聚类质量。

## 2. 前置知识

- **分子（molecule）**：u4-l2 引入的概念。聚簇器不直接搬原子，而是搬分子——分子是单个原子或一组被 pack pattern 绑定的原子（如一段进位链），用一个 `PackMoleculeId` 标识。
- **簇（cluster / legalization cluster）**：一个正在被填充的逻辑块实例，用 `LegalizationClusterId` 标识。一个簇最终会变成 `ClusteredNetlist` 里的一个 CLB（见 u3-l3）。
- **合法化（legalization）**：把分子塞进簇不仅要看「容量够不够」，还要看「簇内能不能布线」——即输入能否经由簇内 PB 层次连到分子的输入引脚、分子输出能否连到簇输出。这一步由 `ClusterLegalizer` 完成（下一讲 u4-l4 详讲）。
- **增益（gain）**：一个 0 分制的「把这个分子吸进当前簇有多划算」的打分。增益越高，越优先被吸。
- **PB 图（`t_pb_graph_node`）**：架构 `t_pb_type` 类型树按 `num_pb` 展开成的实例层模板，是簇内布线合法化的依据（见 u4-l1）。
- **`e_packer_state` 状态机**：`try_pack` 用「先紧后松」的多轮策略反复调用 `do_clustering`，装不下就逐步放宽约束（无关聚类、吸引力组等）。本讲聚焦单轮 `do_clustering` 内部。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [vpr/src/pack/greedy_clusterer.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_clusterer.h) | `GreedyClusterer` 类声明，定义聚簇主循环 `do_clustering`、长簇 `try_grow_cluster`、开簇 `start_new_cluster`。 |
| [vpr/src/pack/greedy_clusterer.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_clusterer.cpp) | 上述方法的实现，是本讲的主干。 |
| [vpr/src/pack/greedy_seed_selector.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_seed_selector.h) / [.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_seed_selector.cpp) | `GreedySeedSelector`：预计算每个分子的「种子增益」并排序，逐个提出好种子。 |
| [vpr/src/pack/greedy_candidate_selector.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_candidate_selector.h) / [.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_candidate_selector.cpp) | `GreedyCandidateSelector`：维护簇的增益统计 `ClusterGainStats`，提出下一个该吸进来的候选分子。 |
| [vpr/src/pack/attraction_groups.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/attraction_groups.h) | `AttractionInfo`：吸引力组，把「应被紧密打包在一起」的原子分组并给它们额外增益。 |
| [vpr/src/pack/pack.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pack.cpp) | `try_pack` 入口，构造并反复调用 `GreedyClusterer::do_clustering`。 |
| [vpr/src/pack/cluster_legalizer.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.h) | `ClusterLegalizer` 与 `ClusterLegalizationStrategy` 枚举（合法化力度）。 |
| [vpr/src/base/vpr_types.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h) | `e_cluster_seed` 枚举、`t_packer_opts` 中控制增益的各项权重。 |

---

## 4. 核心概念与源码讲解

### 4.1 贪心聚簇主循环

#### 4.1.1 概念说明

「贪心聚簇」的核心直觉很简单：**先把最难装的分子当种子装进一个空簇，再把和这个簇关系最铁的分子一个个吸进来，直到塞满。** 然后开下一个空簇，重复，直到所有分子都装完。

为什么「先装难装的」？因为一个使用了大量输入引脚的分子（比如一个大 LUT 或一段进位链）很难塞进一个已经半满的簇——簇内布线资源可能不够。所以让它当种子、从空簇开始装，成功率最高。这正是种子增益会考虑「使用的外部输入数」的原因（见 4.2）。

为什么「贪心」？因为每一步都只看局部最优（当前增益最高的候选），不去搜索全局最优组合——那是 NP 难问题。贪心换来的是速度：VPR 要在分钟级内处理成千上万的原子。

#### 4.1.2 核心流程

`do_clustering` 的主循环可以用下面伪代码概括：

```
构造 GreedyCandidateSelector（维护候选增益）
构造 GreedySeedSelector（预排序种子）
seed = seed_selector.get_next_seed()
while seed 有效:
    # 第一次尝试：跳过每步簇内布线（快但可能漏检非法）
    cluster = try_grow_cluster(seed, strategy = SKIP_INTRA_LB_ROUTE)
    if cluster 无效:
        # 第二次尝试：每加一个分子都做完整合法化（慢但保成功）
        cluster = try_grow_cluster(seed, strategy = FULL)
    assert cluster 有效            # 种子一定能成簇（否则直接致命报错）
    更新进度统计 / 打印进度
    seed = seed_selector.get_next_seed()
返回 各逻辑块类型的使用实例数
```

`try_grow_cluster` 单个簇的生长流程：

```
设置合法化策略 strategy
cluster_id = start_new_cluster(seed)            # 用种子开一个新簇，挑块类型/模式
gain_stats = candidate_selector.create_cluster_gain_stats(seed, cluster_id)
candidate = candidate_selector.get_next_candidate_for_cluster(gain_stats)
while candidate 有效 且 重复候选数 < 上限:
    success = try_add_candidate_mol_to_cluster(candidate, cluster_id)
    if success: 更新增益（成功）
    else:       更新增益（失败，记录失败次数）
    下一个 candidate
if 不是 ensure_legal_final_routing(cluster_id):  # 最终合法性兜底
    销毁非法簇，返回无效 id
finalize_cluster / clean_cluster
返回 cluster_id
```

两种合法化策略由枚举定义：

[cluster_legalizer.h:L66-L71](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.h#L66-L71) —— `FULL` 每加一个分子都跑完整簇内布线；`SKIP_INTRA_LB_ROUTE` 跳过中间每步的簇内布线、只在最后统一做一次。后者更快，但如果最后的统一布线失败，整个簇作废，于是改用前者重试。

#### 4.1.3 源码精读

`do_clustering` 的初始化阶段先造好两个选择器，并取出第一个种子：

[greedy_clusterer.cpp:L130-L156](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_clusterer.cpp#L130-L156) —— 构造 `GreedyCandidateSelector` 与 `GreedySeedSelector`，然后 `seed_selector.get_next_seed()` 取出第一个种子。注意 `max_molecule_stats`（所有分子的最大统计值）被同时传给两个选择器，用于增益项的归一化。

主循环用「先快后慢」两段式尝试长簇：

[greedy_clusterer.cpp:L166-L208](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_clusterer.cpp#L166-L208) —— `while (seed_mol_id.is_valid())` 内：先用 `SKIP_INTRA_LB_ROUTE` 长簇；若返回无效 id，再用 `FULL` 重试；最后断言 `new_cluster_id.is_valid()` 与种子已被聚类。注释里的「基本算法」清楚写出了这种两段式思路。

`try_grow_cluster` 里关键的候选吸收循环：

[greedy_clusterer.cpp:L292-L329](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_clusterer.cpp#L292-L329) —— `while (candidate_mol_id.is_valid() && num_repeated_molecules < max_num_repeated_molecules)`：尝试把候选塞进簇，按成功/失败分别调用增益更新，再取下一个候选；若下一个候选和上一个相同，重复计数加一。正常情况下重复一次就停；但当吸引力组启用时，`max_num_repeated_molecules` 被放大到 500，让簇尽量塞满同组成员（见 4.4）。

簇长完后的合法性兜底：

[greedy_clusterer.cpp:L341-L350](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_clusterer.cpp#L341-L350) —— `ensure_legal_final_routing` 做最终的簇内布线检查；若失败，扣减类型计数、`destroy_cluster` + `compress`、返回无效 id，触发外层的 FULL 重试。

`start_new_cluster` 决定种子放进哪种逻辑块、哪个模式：

[greedy_clusterer.cpp:L463-L479](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_clusterer.cpp#L463-L479) —— 双层循环遍历候选逻辑块类型与该类型的各模式，调用 `cluster_legalizer.start_new_cluster(seed_mol_id, type, j)`，第一个通过的即采用；全部失败则 `VPR_FATAL_ERROR`（种子必须能成簇）。

#### 4.1.4 代码实践

**实践目标**：看清「一次聚簇迭代」中 `try_grow_cluster` 的两次调用与兜底逻辑。

**操作步骤**：

1. 打开 [greedy_clusterer.cpp:L166](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_clusterer.cpp#L166) 的 `while` 主循环。
2. 在第 179 行（第一次 `try_grow_cluster` 调用之后、`if (!new_cluster_id.is_valid())` 之前）和第 207 行（`VTR_ASSERT(new_cluster_id.is_valid())`）各加一行日志，例如：
   ```cpp
   VTR_LOG("DEBUG: seed %zu first_strategy_valid=%d\n", size_t(seed_mol_id), new_cluster_id.is_valid());
   ```
   （示例代码，仅供本地观察，不要提交。）
3. 用 `make -j8 vpr` 重新编译，再跑一个小的打包（如 `run_vtr_flow.py` 跑一个 blink 设计，见 u1-l4）。

**需要观察的现象**：日志会显示对每个种子，`first_strategy_valid` 大多数时候为 1（即快的策略就够），偶尔为 0（触发慢的 FULL 重试）。

**预期结果**：绝大多数种子第一次（跳过簇内布线）就能成簇；只有少数布局拥挤的种子需要第二次完整合法化。这印证了「先快后慢」的设计动机。

**待本地验证**：若你暂时无法编译，可改为纯阅读实践——通读 `try_grow_cluster`（L235–L367），用自己的话写出「候选循环何时退出」的三种条件。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `do_clustering` 要先用 `SKIP_INTRA_LB_ROUTE` 再用 `FULL`，而不是一开始就用 `FULL`？

**参考答案**：簇内布线（intra-lb route）是合法化里最贵的一步。先跳过它、只在最后统一检查一次，能在大多数簇上省掉大量中间布线开销；只有最后检查失败时才退回到每步都做完整布线的 FULL 策略。这是「快路径优先、慢路径兜底」的经典优化。

**练习 2**：主循环里 `VTR_ASSERT(new_cluster_id.is_valid())`（[L207](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_clusterer.cpp#L207)）为什么一定成立？万一不成立会怎样？

**参考答案**：因为 `start_new_cluster` 保证种子一定能放进某个逻辑块（全部失败会直接 `VPR_FATAL_ERROR` 终止程序），而 FULL 策略下每加一个分子都做完整合法化，注释明确说「这不可能失败」。所以两次尝试后簇必然有效；若断言失败，说明出现了未预期的架构/算法 bug。

---

### 4.2 种子选择器 GreedySeedSelector

#### 4.2.1 概念说明

聚簇是「一个簇一个簇地造」，**先造哪个分子当种子，会显著影响最终聚类质量**。`GreedySeedSelector` 的职责就是：在聚簇开始前，给每个分子算一个「种子增益」，按增益从高到低排好序；之后每次被要种子时，就按顺序返回下一个还没被聚类的分子。

它的设计有两个关键点：

- **预计算 + 排序**：增益在构造函数里一次性算好并 `stable_sort`，之后 `get_next_seed()` 只是顺序遍历，非常快（O(1) 摊还）。
- **不重复、不返已聚类**：它维护一个游标 `seed_index_`，假定「一旦分子被聚类就再也不会被取消聚类」，因此只需跳过已被聚类的，不会回头。

#### 4.2.2 核心流程

```
构造时：
  seed_mols_ = 全部分子
  若是 timing_driven：算每个原子的 criticality（关键度）
  for 每个分子 mol：
      mol_gain = max(它的每个原子的 get_seed_gain(...))   # 分子增益取其原子增益的最大值
  stable_sort(seed_mols_, 按增益降序)
  若有 RAM 组：stable_partition 把 RAM 种子挪到最前
  seed_index_ = 0

get_next_seed():
  while seed_index_ < seed_mols_.size():
      mol = seed_mols_[seed_index_++]
      if mol 未被聚类: return mol
  return INVALID    # 全部聚类完毕
```

种子增益由 `e_cluster_seed` 枚举控制（默认 `blend2`，见 vpr_types.h 注释）：

[vpr_types.h:L150-L155](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L150-L155) —— `TIMING`、`MAX_INPUTS`、`BLEND`、`MAX_PINS`、`MAX_INPUT_PINS`、`BLEND2` 等多种选种策略。

以 `BLEND` 为例，其种子增益是一个加权和，并用分子包含的块数做放大：

\[
\text{blend\_gain} = \big(0.5 \cdot \text{criticality} + 0.5 \cdot \frac{\text{num\_used\_ext\_inputs}}{\max\_\text{num\_used\_ext\_inputs}}\big) \times \big(1 + 0.2 \cdot (\text{num\_blocks} - 1)\big)
\]

直觉：关键度高（timing critical）的分子优先当种子，有助于时序；用的外部输入越多越优先当种子，因为这些分子最难塞进半满的簇；分子越大（`num_blocks` 越多，如长进位链）越优先。

#### 4.2.3 源码精读

种子增益的核心计算（`get_seed_gain`）：

[greedy_seed_selector.cpp:L34-L128](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_seed_selector.cpp#L34-L128) —— 一个 `switch` 按 `seed_type` 分派：`TIMING` 直接返回原子关键度；`MAX_INPUTS` 返回分子使用的外部输入数；`BLEND` 是上面公式（L57-L72）；`BLEND2`（L89-L123）是更精细的多项加权和，用一组固定权重（`INPUT_PIN_WEIGHT=0.5`、`USED_INPUT_PIN_WEIGHT=0.2`、`BLOCKS_WEIGHT=0.2`、`CRITICALITY_WEIGHT=0.1`）组合多个归一化比值。注释里都写明了每种策略的「直觉」。

构造函数里给分子打分并稳定排序：

[greedy_seed_selector.cpp:L202-L234](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_seed_selector.cpp#L202-L234) —— 对每个分子，遍历其原子取 `get_seed_gain` 的最大值作为分子增益；随后用 `std::stable_sort` 按增益降序。注释特别强调用 *stable*（稳定）排序：不同标准库的 `std::sort` 对等增益元素的顺序未定义，会导致不同编译器下种子顺序不同、结果不可复现。

RAM 种子前置：

[greedy_seed_selector.cpp:L240-L247](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_seed_selector.cpp#L240-L247) —— 若有 RAM 组，用 `std::stable_partition` 把属于 RAM 组的种子挪到最前，让 RAM 簇先成形，从而给后续非 RAM 簇提供更完整的连通性信息。

`get_next_seed` 的顺序遍历与跳过：

[greedy_seed_selector.cpp:L261-L282](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_seed_selector.cpp#L261-L282) —— 游标 `seed_index_` 单调递增，遇到已被聚类的就 `continue` 跳过，否则返回；越界返回 `INVALID`。注释点明了核心假设：分子一旦被聚类就永不取消。

#### 4.2.4 代码实践

**实践目标**：理解 `get_next_seed` 为何是 O(1) 摊还，以及 RAM 种子前置的效果。

**操作步骤**：

1. 阅读 [greedy_seed_selector.cpp:L261-L282](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_seed_selector.cpp#L261-L282)。
2. 回答：如果允许「分子被聚类后又取消聚类」，这段代码会出什么问题？（提示：游标 `seed_index_` 只前进。）
3. 阅读构造函数末尾对 `seed_index_ = 0` 的赋值，确认第一个被提出的种子就是增益最高的分子。

**需要观察的现象**（纯阅读型）：游标只会前进、从不回退；被跳过的只是「已被聚类」的，不是「曾被提出」的——因为提出后往往紧接着就被聚类了。

**预期结果**：你会确认 `get_next_seed` 的总工作量为 O(分子数)，分摊到每次调用是 O(1)。

#### 4.2.5 小练习与答案

**练习 1**：`BLEND` 种子增益里乘以 \((1 + 0.2(\text{num\_blocks}-1))\) 这一项的目的是什么？

**参考答案**：放大含多个原子的大分子（如长进位链分子）的种子增益，让它们更优先当种子。大分子最难塞进半满的簇，让它们从空簇开始装能提高成功率与聚类质量。

**练习 2**：为什么分子增益取其「所有原子增益的最大值」而不是平均值？

**参考答案**：分子的「难度/关键性」由它最难处理或最关键的原子决定。取最大值确保只要分子里有一个高增益原子，整个分子就被优先当种子，避免把含有进位链头等关键结构的分子埋在后面。

---

### 4.3 候选选择器 GreedyCandidateSelector

#### 4.3.1 概念说明

种子选定、空簇开好后，剩下的问题就是：**这个簇接下来该吸哪个分子进来？** `GreedyCandidateSelector` 负责这件事，并且维护一份随簇生长不断更新的「增益表」。

这是聚簇器里最复杂的一个类，因为它要同时做两件事：

1. **维护增益**：每当一个分子被成功吸进簇（或尝试失败），相关原子的共享/连接/时序增益都要更新。
2. **提出候选**：根据增益表，从「和当前簇关系最紧密的未聚类分子」里挑出增益最高的那个返回。

它把每个正在生长的簇的状态装在一个 `ClusterGainStats` 结构里——这是理解候选选择的关键。

#### 4.3.2 核心流程

`GreedyCandidateSelector` 围绕 `ClusterGainStats` 提供四个核心方法（[greedy_candidate_selector.h:L275-L383](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_candidate_selector.h#L275-L383)），其协作时序如下：

```
# 开新簇时
gain_stats = create_cluster_gain_stats(seed, cluster_id)      # 初始化增益表，把种子当成功分子更新一次

# 每轮吸收
candidate = get_next_candidate_for_cluster(gain_stats)         # 用增益表挑最高增益候选
if 把 candidate 成功塞进簇:
    update_cluster_gain_stats_candidate_success(gain_stats, candidate)   # 更新增益（标记受影响网/块）
else:
    update_cluster_gain_stats_candidate_failed(gain_stats, candidate)    # 记一次失败次数

# 簇长完
update_candidate_selector_finalize_cluster(gain_stats)         # 收尾：记录簇内互连网供后续 transitive 查找
```

**候选搜索是分级的**（`add_general_cluster_molecule_candidates`）：优先级从高到低依次是

1. 强连通 + 时序关键（低扇出网直接相连的分子）；
2. 传递连通（隔两跳，如喂给另一个簇里加器的那些 FF）；
3. 弱连通（高扇出网相连）；
4. 吸引力组成员（若簇有关联的吸引力组）。

只有当前一级把候选耗尽（`feasible_blocks` 空了）才进入下一级。最终从 `feasible_blocks` 这个优先队列里弹出增益最高的分子。

**增益的计算**（`update_total_gain`）把三项加权：共享增益（`sharing_gain`，能复用多少已有输入网）、连接增益（`connection_gain`，能吸收多少连接）、时序增益（`timing_gain`，关键路径收益）。默认 `connection_driven=true`、`timing_driven=true` 时，对每个被标记的块 `b`：

\[
g_b = \frac{(1 - w_c)\cdot \text{sharing\_gain}_b + w_c \cdot \text{connection\_gain}_b}{\text{num\_used\_pins}_b}
\]

\[
\text{gain}_b = w_t \cdot \text{timing\_gain}_b + (1 - w_t) \cdot g_b
\]

其中 \(w_c\) 是 `connection_gain_weight`、\(w_t\) 是 `timing_gain_weight`（均来自 `t_packer_opts`）。除以 `num_used_pins` 是为了归一化，避免引脚多的块凭体量占便宜。

#### 4.3.3 源码精读

`ClusterGainStats` 承载了簇生长中的全部统计（[greedy_candidate_selector.h:L44-L149](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_candidate_selector.h#L44-L149)）：四个 `unordered_map` 分别存 `gain`/`timing_gain`/`connection_gain`/`sharing_gain`（键是 `AtomBlockId`）；`mol_failures` 记录每个分子失败次数；`feasible_blocks` 是一个「懒弹出唯一优先队列」`LazyPopUniquePriorityQueue<PackMoleculeId, float>`，堆顶是当前增益最高的候选；还有 APPack 相关的 `flat_cluster_position` / `mol_pos_sum`、以及 `candidates_propose_limit` / `num_candidates_proposed` 控制每级最多提几个候选。

`get_next_candidate_for_cluster` 的分级搜索与弹堆：

[greedy_candidate_selector.cpp:L689-L710](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_candidate_selector.cpp#L689-L710) —— RAM 簇走 `add_ram_cluster_molecule_candidates`（只提供同一物理 RAM 组的原子），其余走 `add_general_cluster_molecule_candidates`；只要 `feasible_blocks` 非空且未达本轮提议上限，就 `pop()` 堆顶作为最佳候选。

四级候选来源的编排：

[greedy_candidate_selector.cpp:L771-L821](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_candidate_selector.cpp#L771-L821) —— 注释列出 4 级优先级；初始搜索只做一次连通+时序（`initial_search_for_feasible_blocks` 标志）；当 `feasible_blocks` 空时才依次尝试传递连通、高扇出连通、吸引力组。`prioritize_transitive_connectivity` 控制第 2、3 级的先后。

增益的合成公式：

[greedy_candidate_selector.cpp:L635-L654](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_candidate_selector.cpp#L635-L654) —— 即上面两个公式对应的代码：`connection_driven` 分支用连接增益加权，再除以 `num_used_pins`；`timing_driven` 时再与时序增益做凸组合。

失败处理：

[greedy_candidate_selector.cpp:L658-L668](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_candidate_selector.cpp#L658-L668) —— 候选塞簇失败时，只在 `mol_failures` 里把该分子的失败计数 +1，供后续决策参考（频繁失败的分子会被降低优先级）。

#### 4.3.4 代码实践

**实践目标**：看清「候选搜索四级优先级」与「增益合成公式」。

**操作步骤**：

1. 打开 [greedy_candidate_selector.cpp:L757-L821](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_candidate_selector.cpp#L757-L821)，对照注释把 4 级优先级抄成一张表。
2. 打开 [greedy_candidate_selector.cpp:L635-L654](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_candidate_selector.cpp#L635-L654)，找到 `connection_gain_weight`、`timing_gain_weight` 两个权重，再回到 [vpr_types.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h) 找它们的默认值（注释里写了 `timing_gain_weight` 默认 0.9 一类信息）。

**需要观察的现象**：默认 `connection_driven` 与 `timing_driven` 同时开启，所以增益同时受「连接吸收」和「时序关键」驱动；权重让二者做凸组合（和为 1）。

**预期结果**：你会确认候选选择默认是「连接 + 时序」双重驱动，而非单一目标。

#### 4.3.5 小练习与答案

**练习 1**：为什么候选搜索要分四级、且前一级耗尽才进下一级，而不是一次性把所有相关分子都放进堆？

**参考答案**：强连通、低扇出的分子最值得优先吸收（局部性好、布线短）；传递/高扇出连通是退而求其次的弱信号；吸引力组是特殊约束下的补救。分级的目的是让「质量最好的候选」先用上，避免弱信号候选在堆里抢走名额，同时减少每次构建候选集合的开销。

**练习 2**：增益公式里为什么要除以 `num_used_pins`？

**参考答案**：归一化。一个引脚很多的大块即便只共享一两个网，原始共享项也可能数值偏大；除以已用引脚数，把增益变成「每引脚的平均收益」，使大小块可比，避免大块凭体量长期占据堆顶。

---

### 4.4 吸引力组与 APPack：增益的增强

#### 4.4.1 概念说明

到此为止的增益只反映「电学连接」的紧密程度。但有两种场景需要额外的「吸引力」：

1. **吸引力组（Attraction Groups）**：当用户给了布局/平面规划（floorplanning）约束，而某次聚类后某些区域过满时，`try_pack` 会把「被挤在同一区域、应该紧密打包」的原子组成一个吸引力组。同组成员互相之间有额外增益，促使聚簇器把它们塞进同一簇，从而缓解区域过满。目前吸引力组基于 partition（分区）创建，注释说未来可用于其他概念。

2. **APPack**：一种「扁平布局引导」的打包——先做一个扁平（原子级）布局，再用布局里的物理位置引导聚簇：物理位置近的原子更可能被吸进同一簇。`GreedyCandidateSelector` 持有 `APPackContext`，当 `use_appack` 开启时，会用扁平位置选「无关候选」（`get_unrelated_candidate_for_cluster_appack`），并把簇的质心 `flat_cluster_position` 用于增益计算。

#### 4.4.2 核心流程

吸引力组对增益的影响落在 `update_total_gain` 里：对每个被标记的块，若它和当前簇属于同一吸引力组，就给它的 `gain` 加上一个组增益（默认 0.08）：

\[
\text{gain}_b \mathrel{+}= \text{att\_grp\_gain} \quad (\text{若 } b \text{ 与簇同组})
\]

吸引力组还改变了主循环的「重复候选上限」：正常情况下候选重复 1 次就停止长簇；但若启用了吸引力组，`try_grow_cluster` 把上限抬到 500（`attraction_groups_max_repeated_molecules_`），让簇尽可能多地吸收同组成员，达到更紧密的打包。

APPack 则主要影响两处：候选选择器构造时预计算一份「按设备位置空间分布」的无关候选数据 `appack_unrelated_clustering_data_`（一个三维 `NdMatrix`，下标 `[layer][x][y]`）；以及在 `get_next_candidate_for_cluster` 里，当没有连通候选时，按扁平位置就近挑无关候选。

#### 4.4.3 源码精读

`AttractionGroup` 结构与默认增益：

[attraction_groups.h:L36-L45](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/attraction_groups.h#L36-L45) —— `group_atoms` 存组成员，`gain` 默认 `0.08`，即同组原子被提议入簇时的额外增益加成。

`AttractionInfo` 类的职责与创建时机：

[attraction_groups.h:L47-L62](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/attraction_groups.h#L47-L62) —— 构造函数按 floorplan 约束填充（无约束则不建组）；`create_att_groups_for_overfull_regions` 在某轮聚类后区域过满时被调用——这正是 `try_pack` 多轮「先紧后松」里开启吸引力组的入口（见 u4-l1）。

吸引力组增益在 `update_total_gain` 中的注入：

[greedy_candidate_selector.cpp:L622-L627](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_candidate_selector.cpp#L622-L627) —— 若块的吸引力组 id 与簇的 `attraction_grp_id` 相同，则 `gain[blk_id] += att_grp_gain`，让同组块更易被选中。

吸引力组开启时抬高重复上限：

[greedy_clusterer.h:L215-L223](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_clusterer.h#L215-L223) 与 [greedy_clusterer.cpp:L283-L285](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_clusterer.cpp#L283-L285) —— 常量 `attraction_groups_max_repeated_molecules_ = 500`；当 `attraction_groups.num_attraction_groups() > 0` 时，`max_num_repeated_molecules` 从 1 抬到 500，使簇持续吸收同组分子直到候选耗尽。

APPack 的空间无关候选数据：

[greedy_candidate_selector.h:L641-L651](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_candidate_selector.h#L641-L651) —— `appack_unrelated_clustering_data_` 是按 `[layer][x][y]` 索引的三维矩阵，每个网格位置存一组按增益排序的分子；APPack 未启用时不初始化。

APPack 引导的无关候选选择：

[greedy_candidate_selector.cpp:L723-L733](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_candidate_selector.cpp#L723-L733) —— 当允许无关聚类且没有连通候选时，若 `use_appack`，按簇类型允许的无关聚类次数上限，调用 `get_unrelated_candidate_for_cluster_appack` 基于扁平位置挑候选。

#### 4.4.4 代码实践

**实践目标**：追踪「区域过满 → 开启吸引力组 → 抬高重复上限」这条联动链。

**操作步骤**：

1. 在 [attraction_groups.h:L53-L57](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/attraction_groups.h#L53-L57) 阅读 `create_att_groups_for_overfull_regions` 的注释。
2. 在 [pack.cpp:L382-L390](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pack.cpp#L382-L390) 看 `try_pack` 如何根据 `floorplan_regions_overfull` 决定下一轮 `packer_state`（这正是「过满才开吸引力组」的开关）。
3. 在 [greedy_clusterer.cpp:L283-L285](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_clusterer.cpp#L283-L285) 确认吸引力组开启时重复上限从 1 抬到 500。

**需要观察的现象**：吸引力组不是默认开启的；它只在「有 floorplan 约束且某轮聚类后区域过满」时才被创建，是一种自适应的补救机制。

**预期结果**：你会看到一条完整的自适应链：过满检测 → 创建吸引力组 → 抬高重复上限 → 簇吸收更多同组分子 → 区域密度提升。

#### 4.4.5 小练习与答案

**练习 1**：吸引力组的默认增益是 0.08（[attraction_groups.h:L44](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/attraction_groups.h#L44)）。为什么是这么小的一个值，而不是 1.0？

**参考答案**：增益需要与共享/连接/时序项相平衡。0.08 是一个温和的「加成」，让同组原子在连接紧密程度相近时略占优势，但不至于无视电学连接强行把毫无关系的同组原子塞在一起——那样会损害布线长度与时序。它是一个轻量偏向，而非硬约束。

**练习 2**：APPack 的 `appack_unrelated_clustering_data_` 为什么是按设备 `[layer][x][y]` 空间分布的，而不是像普通无关聚类那样全局一份？

**参考答案**：APPack 的目标是「物理位置近的原子优先同簇」。把无关候选按位置分桶后，给位于某区域的簇挑无关候选时，只需查该位置附近的桶，就能快速找到物理邻近的分子，比在全局列表里逐个判断距离高效得多。

---

## 5. 综合实践

**任务**：画出一次「聚簇迭代」中 `GreedySeedSelector` 与 `GreedyCandidateSelector` 的协作时序图，并标注每一步读写的数据。

请按以下步骤完成：

1. **准备**：通读 [greedy_clusterer.cpp:L235-L367](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_clusterer.cpp#L235-L367)（`try_grow_cluster` 全文）和 [greedy_seed_selector.cpp:L261-L282](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_seed_selector.cpp#L261-L282)（`get_next_seed`）。

2. **画时序图**：横轴为时间，纵轴为三个角色 `GreedyClusterer`（主控）、`GreedySeedSelector`、`GreedyCandidateSelector`。至少画出以下交互：
   - `Clusterer → SeedSelector.get_next_seed()` → 返回种子 `s`；
   - `Clusterer`：`start_new_cluster(s)` 开簇；
   - `Clusterer → CandidateSelector.create_cluster_gain_stats(s)` → 返回 `gain_stats`；
   - `Clusterer → CandidateSelector.get_next_candidate_for_cluster(gain_stats)` → 返回候选 `c`；
   - `Clusterer`：`try_add_candidate_mol_to_cluster(c)`；
   - 按成功/失败分别画 `update_cluster_gain_stats_candidate_success/failed`；
   - 循环回 `get_next_candidate_for_cluster`，直到候选无效；
   - `Clusterer`：`ensure_legal_final_routing` → 若失败，回到主循环用 `FULL` 重试，并重新走一遍上述流程；
   - `Clusterer → CandidateSelector.update_candidate_selector_finalize_cluster`。

3. **标注数据**：在每个箭头上写清传入与返回的关键数据（种子 id、`ClusterGainStats`、候选 id、成功/失败标志）。

4. **反思**：在图旁用两三句话回答——为什么 `create_cluster_gain_stats` 只在每个簇开头调用一次，而 `get_next_candidate_for_cluster` 与两个 `update_*` 在循环里反复调用？

**参考答案要点**：`create_cluster_gain_stats` 建立的是「这个簇」的增益表，簇一旦开好、种子落定，表就建好了，无需重建；而每吸收一个候选（成功或失败）都会改变簇内已包含的原子集合，从而改变其余候选的共享/连接/时序增益，所以 `get_next_candidate_for_cluster` 与两个 `update_*` 必须每轮循环都调用以反映最新状态。这也解释了为什么 `ClusterGainStats` 是一个「随簇生长而滚动更新」的对象，而不是一次性预算好的静态表。

## 6. 本讲小结

- `GreedyClusterer::do_clustering` 是贪心聚簇主循环：`while (有种子) { try_grow_cluster(快); 若失败 try_grow_cluster(慢); 取下一个种子 }`，体现「先快后慢、种子必成簇」的设计。
- `try_grow_cluster` 单簇生长：`start_new_cluster` 开簇 → `create_cluster_gain_stats` 建增益表 → 循环 `get_next_candidate → 尝试塞簇 → 按成功/失败更新增益` → `ensure_legal_final_routing` 兜底。
- `GreedySeedSelector` 在构造时一次性算好每个分子的种子增益并 `stable_sort`（保证跨编译器可复现），`get_next_seed` 以 O(1) 摊还顺序返回未聚类分子；RAM 种子被前置。
- `GreedyCandidateSelector` 维护 `ClusterGainStats`，按「强连通+时序 → 传递连通 → 高扇出连通 → 吸引力组」四级搜索候选，从优先队列弹增益最高的分子；增益由共享、连接、时序三项加权合成并按引脚数归一化。
- 吸引力组（`AttractionInfo`）在区域过满时被自适应创建，给同组原子额外增益（默认 0.08）并把重复候选上限从 1 抬到 500，实现更紧密的打包。
- APPack 用扁平布局的位置引导聚簇：按 `[layer][x][y]` 分桶预存无关候选，让物理邻近的原子优先同簇。

## 7. 下一步学习建议

本讲只关心「分子被选进簇」的决策，刻意把「塞进去到底合不合法」委托给了 `ClusterLegalizer`。下一讲 **u4-l4 聚簇合法化与簇内布线** 会打开这个黑盒：

- 阅读 [cluster_legalizer.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.h) 与 [cluster_router.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_router.h)，理解 `add_mol_to_cluster` 与 `ensure_legal_final_routing` 如何判定簇内可布线性。
- 阅读 [lb_type_rr_graph.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/lb_type_rr_graph.h)，看簇内布线用的是什么图结构（它和第 6 单元的全局 RR Graph 是两套东西）。

此外，若你想理解本讲多次提到的 `t_packer_opts` 各权重默认值从哪来，可回顾 u1-l5（命令行与参数体系）中 `read_options` 的实现；想理解聚簇产物如何进入布局，可回顾 u3-l3（ClusteredNetlist）。
