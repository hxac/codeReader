# 布局总览与模拟退火框架

## 1. 本讲目标

本讲是「布局 Placement」单元的开篇。学完之后，你应该能够：

- 说清楚**布局要解决什么问题**：把聚簇网表 `ClusteredNetlist` 里的每一个逻辑块放到 FPGA 器件网格的一个合法位置上，目标是最小化**布线代价（线长）**和**时序代价（关键路径延迟）**。
- 理解 **VPR 为什么用模拟退火（Simulated Annealing, SA）做布局**：布局是一个规模巨大、代价函数高度非凸的优化问题，贪心搜索极易陷在局部最优，SA 用「按概率接受变差移动」来逃离局部最优。
- 读懂由 `Placer` 与 `PlacementAnnealer` 构成的**双层循环框架**：外层逐温度下降、内层在固定温度下反复尝试交换；以及退火结束后再追加一段温度归零的**淬火（quench）**做纯贪心收尾。
- 理解**退火调度（annealing schedule）**如何驱动整个搜索：温度 `t`、衰减因子 `alpha`、范围限制 `rlim`、关键度指数 `crit_exponent`、每温度移动数 `move_lim` 这几个量是如何随成功率自适应更新的。

本讲只做**总览与框架**，不深入移动生成器细节（留给 u5-l2）与代价/延迟模型细节（留给 u5-l3）。本讲聚焦「谁在调用谁、代价由哪几项组成、温度如何决定接受概率」这三件事。

## 2. 前置知识

本讲直接建立在前几讲的认知之上，请确认你已经理解以下概念：

- **`ClusteredNetlist`（u3-l3）**：打包后的聚簇网表，每个块携带逻辑块类型指针与盒内层次 `t_pb*`，是布局的**输入**。
- **器件网格 `DeviceGrid`（u2-l3）**：二维（或多层）网格 `t_grid_tile`，描述 FPGA 上「每个坐标位置放的是什么瓦片」，布局就是把聚簇块摆到这张网格的合法位置上。
- **`VprContext` 与 `g_vpr_ctx`（u3-l4）**：全局状态总线。布局阶段会写 `PlacementContext`（块坐标 `block_locs`），并读 `ClusteringContext`（网表）、`DeviceContext`（网格）、`TimingContext`（时序图）。
- **主流程 `vpr_api`（u3-l5）**：`vpr_place_flow` 按阶段动作 `e_stage_action`（DO/LOAD/SKIP）分派，最终调用本讲的入口 `try_place`。

一句话回顾数据流，本讲聚焦方括号这一段：

```
AtomNetlist → [Packer] → ClusteredNetlist → [Placer] → 带坐标的布局 → Router
```

此外，理解模拟退火只需要一个朴素的物理直觉：**金属退火**是先把金属加热到高温让原子剧烈运动（容忍混乱），再缓慢降温让原子逐步结晶到低能量的稳定排列。VPR 把这套思路搬到了布局优化上。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `vpr/src/place/place.h` | 声明布局入口函数 `try_place`，是 `vpr_place_flow` 调进布局阶段的门面。 |
| `vpr/src/place/placer.h` | 声明 `Placer` 类：封装布局所需的全部数据结构、代价函数、时序对象，并持有退火器 `PlacementAnnealer`，对外暴露 `place()` 与 `update_global_state()`。 |
| `vpr/src/place/placer.cpp` | `Placer::place()` 的实现——退火外层 `do-while` 循环 + 淬火段的编排。 |
| `vpr/src/place/annealer.h` | 声明 `PlacementAnnealer`（模拟退火器）、`t_annealing_state`（退火状态）、`t_swap_stats`（交换统计）、`t_swap_result`（单次交换结果）。是本讲最重要的头文件。 |
| `vpr/src/place/annealer.cpp` | 退火器的全部实现：内层循环 `placement_inner_loop`、单次交换 `try_swap_`、接受判定 `assess_swap_`、调度更新 `outer_loop_update`、初始温度估计等。 |
| `vpr/src/place/placer_state.h` | 声明 `PlacerState`（布局阶段局部可变状态，含块坐标 `BlkLocRegistry` 与 `PlacerTimingContext`），布局结束后才拷进全局 `PlacementContext`。 |
| `vpr/src/place/place_util.h` / `place_util.cpp` | `t_placer_costs`（代价项 + 归一化因子）、`get_total_cost`（总代价公式）、`update_norm_factors`（每温度更新归一化）、`get_place_inner_loop_num_move`（每温度移动数公式）。 |
| `vpr/src/base/vpr_types.h` | `e_place_algorithm`（三种布局算法）、`e_sched_type`（AUTO/USER 调度）、`t_annealing_sched`、`e_anneal_init_t_estimator`（初始温度估计器）等枚举与选项结构。 |

## 4. 核心概念与源码讲解

### 4.1 模拟退火原理

#### 4.1.1 概念说明

布局的本质是一个**优化问题**：在所有「合法摆放」中，找一个使某一代价函数最小的方案。VPR 的代价函数主要由两类项构成（详见 4.2）：

- **线长代价（bb_cost）**：用每个线网的包围盒（bounding box）周长估算它的布线长度，越短越好。
- **时序代价（timing_cost）**：每条连接的延迟乘以它的关键度（criticality），越关键、越慢的连接代价越高。

最朴素的解法是**贪心/局部搜索**：随机挑两个块交换位置，若代价下降就接受，上升就拒绝。问题在于代价函数高度非凸，到处是局部最优——贪心法会很快卡在一个「任何单次交换都只会变差」的局部谷底，而这个谷底离全局最优可能很远。

**模拟退火**的核心思想是：**以一个随温度衰减的概率接受「变差」的移动**，从而有机会翻越代价山丘、跳出局部最优；随着温度降到很低，接受变差移动的概率趋于零，搜索逐渐收敛到一个（通常很好的）局部最优。这与金属退火「先加热、后缓慢冷却」完全同构：

| 物理退火 | VPR 布局退火 |
|---|---|
| 温度 | 代价函数的「温度」参数 `t` |
| 原子排列的能量 | 布局的总代价 `cost` |
| 原子位置扰动 | 随机交换两个块的位置 |
| 高温下原子混乱 | 高温下几乎任何交换都被接受，广泛探索 |
| 缓慢降温结晶 | 温度按 `alpha` 衰减，逐渐收敛 |
| 冷却到零度 | 淬火（quench）：只接受改进移动 |

接受准则是模拟退火的灵魂。设某次交换带来的代价变化为 \(\Delta C\)（正表示变差），当前温度为 \(T\)，则接受概率为：

\[
P(\text{accept}) =
\begin{cases}
1, & \Delta C \le 0 \quad (\text{变好的移动总是接受}) \\
\exp\!\left(-\dfrac{\Delta C}{T}\right), & \Delta C > 0 \quad (\text{变差的移动按概率接受})
\end{cases}
\]

这个公式直接揭示了「温度如何影响接受概率」：

- 温度 \(T\) 很高时，\(-\Delta C / T\) 接近 0，\(\exp(\cdot)\) 接近 1，**几乎所有变差移动都被接受**——搜索接近随机游走，能广泛探索。
- 温度 \(T\) 很低时，\(-\Delta C / T\) 是一个很大的负数，\(\exp(\cdot)\) 趋近 0，**几乎只接受改进移动**——搜索退化为贪心，在当前区域精细打磨。
- 当 \(T = 0\) 时，变差移动的接受概率严格为 0（代码里直接判 REJECTED），这就是淬火阶段的语义。

每次交换有三种结局（枚举 `e_move_result`，定义在 `move_utils.h`）：

- **ACCEPTED**：移动被接受，块位置真的更新。
- **REJECTED**：移动被拒绝，块位置回滚到交换前。
- **ABORTED**：交换本身就不合法（例如找不到合适的目标位置、违反块尺寸或约束），根本没产生有效代价，直接丢弃。

#### 4.1.2 核心流程

一次完整的模拟退火布局，骨架是一个「外层降温度 + 内层试交换」的嵌套循环，外加一段淬火收尾：

```text
估计初始温度 t0                       # 用一批试探性交换的代价统计推算
t = t0
while 退火未结束:
    内层循环：在固定温度 t 下做 move_lim 次交换
        每次交换：
          ① 生成一个移动（交换/搬移若干块）
          ② 计算代价变化 ΔC（线长 + 时序 + 可选拥塞/interposer/NoC）
          ③ 按 exp(-ΔC/t) 概率决定接受/拒绝；接受则提交，否则回滚
          ④ 累计 accepted / rejected / aborted 统计
    外层更新：根据本轮成功率调整 alpha、rlim、crit_exponent，降温 t *= alpha
    判断是否到达退出温度 t_exit

淬火（quench）：t = 0
    再做一轮内层循环，此时只接受改进移动（纯贪心收尾）
最终做一次完整时序分析，校验合法性，必要时回滚到检查点
```

几个要点先建立直觉（细节在 4.3 展开）：

- **温度下降不是固定比例**：`AUTO_SCHED` 下，衰减因子 `alpha` 会根据上一轮的**成功率**（被接受的移动占比）动态调整——成功率太高说明探索太散，要加快降温；成功率太低说明快卡死了，要放慢降温。
- **每次交换的范围 `rlim` 会收缩**：高温时允许块跨越大半个器件；温度降下来后 `rlim` 跟着缩小，块只在邻近区域微调，这既加速收敛也让搜索更聚焦。
- **代价增量计算是增量的**：每次交换只重新计算受影响线网的包围盒与受影响连接的时序代价，而不是每次都从头算整张网表——这是布局能在合理时间跑完的关键。

#### 4.1.3 源码精读

接受准则的实现是 `assess_swap_`，它精确对应上面的分段公式：

[assess_swap_ 实现 annealer.cpp:1105-1126](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L1105-L1126) — 单次交换的接受判定：`delta_c <= 0` 直接 ACCEPTED；`t == 0` 直接 REJECTED（淬火语义）；否则生成一个随机数 `fnum`，计算 `prob_fac = exp(-delta_c / t)`，当 `prob_fac > fnum` 时接受（爬山），否则拒绝。

注意它用严格大于 `>` 比较 `prob_fac` 和均匀随机数 `fnum`，因此接受概率恰好是 \(\exp(-\Delta C / T)\)。该方法的声明在 [annealer.h:268-277](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.h#L268-L277)，其上方注释把「负 ΔC 总是接受、正 ΔC 按温度衰减概率接受」的语义写得很清楚。

交换的三种结局由枚举刻画：

[e_move_result move_utils.h:23-27](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_utils.h#L23-L27) — `REJECTED` / `ACCEPTED` / `ABORTED` 三态，分别对应「拒绝并回滚」「接受并提交」「非法直接丢弃」。

交换统计则记录在 `t_swap_stats`，外层调度正是靠它算成功率：

[t_swap_stats annealer.h:29-34](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.h#L29-L34) — 累计 rejected / accepted / aborted 次数与 `try_swap_` 调用次数，三者之和即总尝试数，`accepted / 总数` 即成功率。

单次交换的总流程在 `try_swap_`，它把「生成移动 → 算 ΔC → 判接受 → 提交/回滚」串起来，是理解代价项构成的最佳入口（其 ΔC 计算在 4.2 节细讲）：

[try_swap_ 实现 annealer.cpp:506-833](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L506-L833) — 注释明确说明「先把块搬到新位置、再算代价增量；接受则更新反向查找表，拒绝则回滚」。其中 L696 处调用 `assess_swap_(delta_c, annealing_state_.t)` 把代价变化与当前温度交给接受准则裁决。

#### 4.1.4 代码实践

**实践目标**：亲手核对接受准则公式，建立「温度如何控制探索 vs 收敛」的定量直觉。

**操作步骤**：

1. 打开 [annealer.cpp:1105](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L1105) 的 `assess_swap_`，对照本节公式，确认 `exp(-delta_c / t)` 就是 \(\exp(-\Delta C / T)\)。
2. 取三个数值实验（手算或计算器）：
   - \(\Delta C = 10, T = 100\)：接受概率 \(e^{-0.1} \approx 0.905\)（高温，几乎接受）。
   - \(\Delta C = 10, T = 10\)：接受概率 \(e^{-1} \approx 0.368\)（中温，约三分之一概率接受）。
   - \(\Delta C = 10, T = 1\)：接受概率 \(e^{-10} \approx 4.5\times10^{-5}\)（低温，几乎不可能接受）。
3. 验证 `t == 0` 分支：当温度归零，无论 \(\Delta C\) 多小（只要为正），都会直接 REJECTED——这正是淬火阶段「只接受改进」的实现。

**需要观察的现象**：同样的变差量 \(\Delta C\)，在高温下几乎总能被接受，在低温下几乎总被拒绝。温度就是「对变差的容忍度」旋钮。

**预期结果**：你会清楚地看到，模拟退火并非「一开始就贪心」，而是先用高温度做近乎随机的广泛探索，再随降温逐步收紧到贪心打磨。**运行行为待本地验证**：若你已编译 `vpr`（见 u1-l2），可用 `--disp on` 打开图形界面，在布局阶段会看到块在网格上剧烈跳动、随后逐渐稳定——那就是温度下降的外在表现（图形界面相关见 u8-l4）。

#### 4.1.5 小练习与答案

**练习 1**：为什么纯贪心搜索（只接受 \(\Delta C \le 0\) 的移动）做不好布局？

> **答案**：布局代价函数非凸，存在大量局部最优。纯贪心一旦进入一个「任何单次交换都只会变差」的局部谷底就再也出不来，而这个谷底离全局最优可能很远。模拟退火用 \(\exp(-\Delta C / T)\) 的概率接受变差移动，让搜索能翻越代价山丘跳出局部最优。

**练习 2**：当温度 \(T \to 0^+\) 时，\(\exp(-\Delta C / T)\) 趋向什么？这与代码里 `t == 0` 直接 REJECTED 是否一致？

> **答案**：对任意 \(\Delta C > 0\)，\(-\Delta C / T \to -\infty\)，故 \(\exp(-\Delta C / T) \to 0\)，即接受概率趋于零。代码在 `t == 0` 时直接返回 REJECTED 正是这个极限情形的实现（还避免了 `exp(-inf)` 的数值问题），二者一致。

### 4.2 Placer 主循环

#### 4.2.1 概念说明

`PlacementAnnealer` 只管「在给定温度下做交换」这件内层的事；真正把「逐温度下降 + 淬火收尾 + 检查点 + 最终时序分析」编排起来的，是 `Placer` 类。可以这样理解二者分工：

- **`Placer` 是指挥官**：负责构造所有布局所需的对象（代价结构、时序引擎、延迟模型、退火器、日志器），驱动外层温度循环，决定何时进入淬火，并在退火结束后做合法性校验与检查点回滚。
- **`PlacementAnnealer` 是执行者**：负责内层循环——在当前温度下做 `move_lim` 次交换尝试，并维护退火状态（温度、`rlim`、统计）。

`Placer` 还负责管理**布局阶段局部状态 `PlacerState`**。这一点承接 u3-l4 的全局状态体系：布局过程中反复改写块坐标等数据，VPR 把这些**可变的中间状态放在一个局部的 `PlacerState` 对象里**，而不是直接写全局 `PlacementContext`；直到布局全部跑完、结果合法，才由 `update_global_state()` 把局部状态拷进全局上下文供布线阶段使用。`placer_state.h` 的文件头注释把这一约定说得非常明白：

> A `PlacerState` object contains the placement state which is subject to change during the placement stage. … At the end of the placement stage, one of these object is copied to global placement context (`PlacementContext`). The `PlacementContext` should not be used before the end of the placement stage.

布局的代价函数由 `t_placer_costs` 聚合，它把若干项代价与各自的**归一化因子**放在一起。理解归一化是理解代价公式的关键：线长代价与时序代价量纲不同、量级也不同，直接相加没有意义。VPR 的做法是把每一项除以「上一轮的该项总值」做归一化，使各项都落在 1 附近、量级可比，再用权重加权求和。

#### 4.2.2 核心流程

`Placer::place()` 的主循环结构非常清晰，是一个 `do-while` 外层退火 + 紧随其后的淬火段：

```text
Placer::place()
  ┌─ 退火阶段（if 非 quench_only）
  │   do:
  │     annealer.outer_loop_update_timing_info_and_cost_terms()  # 更新时序信息与归一化因子
  │     （可选）保存检查点
  │     annealer.placement_inner_loop()                          # 内层：当前温度下做 move_lim 次交换
  │     打印本温度状态
  │   while annealer.outer_loop_update_state()                  # 降温、调 alpha/rlim，返回是否继续
  │
  ├─ 淬火阶段
  │   annealer.start_quench()                                    # 温度置 0、恢复 move_lim
  │   annealer.outer_loop_update_timing_info_and_cost_terms()
  │   annealer.placement_inner_loop()                            # 纯贪心：只接受改进移动
  │
  └─ 收尾
      最终完整时序分析 → 取关键路径
      若检查点更好则回滚到检查点
      校验布局合法性
```

每温度迭代的代价更新与归一化发生在 `outer_loop_update_timing_info_and_cost_terms`：它先（在时序驱动算法下）做一次**全量时序更新**刷新关键度与 slack，再更新各代价项的**归一化因子**，最后用 `get_total_cost` 重算当前总代价。归一化因子之所以要每温度更新一次，是因为随着布局改善，各代价项的绝对量级会变化，归一化的「分母」必须跟着刷新。

单次交换的代价增量 ΔC 按当前布局算法分三种情形计算（在 `try_swap_` 内）：

```text
BOUNDING_BOX_PLACE（纯线长）:
    ΔC = Δbb_cost · bb_cost_norm            (+ 可选 interposer 项)

CRITICALITY_TIMING_PLACE（默认，线长+时序）:
    ΔC = (1 - timing_tradeoff) · Δbb · bb_cost_norm
       + timing_tradeoff · Δtiming · timing_cost_norm
       + congestion_factor · Δcong · congestion_cost_norm
       (+ 可选 interposer 项 + NoC 项)

SLACK_TIMING_PLACE（基于 setup slack，主要用于淬火）:
    ΔC = analyze_setup_slack_cost(...) · timing_cost_norm
```

其中 `timing_tradeoff` 是用户可调的「时序 vs 线长」权重（命令行 `--timing_tradeoff`）。可见 CRITICALITY_TIMING_PLACE 每次交换至少要评估**线长项**和**时序项**，时序驱动时还要顺带维护拥塞项（若开启）。

#### 4.2.3 源码精读

`Placer::place()` 的退火主循环就在下面这段——它就是本讲「退火主循环」的真正所在：

[Placer::place 退火 do-while placer.cpp:352-393](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/placer.cpp#L352-L393) — 外层温度循环：每轮先 `outer_loop_update_timing_info_and_cost_terms()` 刷新时序与归一化，必要时在 LATE_IN_THE_ANNEAL 阶段保存检查点，再 `placement_inner_loop()` 跑完一整个温度的内层交换，循环条件 `outer_loop_update_state()` 决定是否继续降温。

紧跟着的淬火段：

[淬火段 placer.cpp:395-413](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/placer.cpp#L395-L413) — `start_quench()` 把温度置零、把 `move_lim` 恢复到最大值，随后再跑一轮内层循环；注释写明此时「只接受降低代价的交换」。淬火后做最终时序分析并打印 post-quench CPD（关键路径延迟）。

这段循环的「模板」其实就写在 `annealer.h` 的类注释里，是理解用法的最佳导读：

[PlacementAnnealer 用法示例 annealer.h:159-178](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.h#L159-L178) — 文档给出的标准用法：`do { update_timing; inner_loop; } while(update_state);` 然后调 `start_quench()` 再跑一次 inner_loop。`placer.cpp` 的实现与此完全对应。

代价项的容器与总代价公式在 `t_placer_costs`：

[t_placer_costs 结构 place_util.h:67-156](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/place_util.h#L67-L156) — 聚合 `cost`（加权总代价）、`bb_cost`（线长）、`timing_cost`（delay·criticality）、`congestion_cost`（布线拥塞估计）、`interposer_cost` / `interposer_cong_cost`（interposer 切割相关），以及各自对应的 `*_norm` 归一化因子。

总代价公式 `get_total_cost`：

[get_total_cost place_util.cpp:50-71](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/place_util.cpp#L50-L71) — BOUNDING_BOX_PLACE 下总代价仅为归一化线长；时序驱动算法下为 `(1-timing_tradeoff)·归一化线长 + timing_tradeoff·归一化时序`；再叠加拥塞、interposer、NoC 各加权项。

单次交换里 CRITICALITY_TIMING_PLACE 的 ΔC 计算正是这份公式的增量版本：

[try_swap_ 中 ΔC 计算 annealer.cpp:617-637](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L617-L637) — ΔC 由「归一化线长增量」与「归一化时序增量」按 `timing_tradeoff` 加权，再加可选的拥塞/interposer 增量。注释明确：该算法为节省算力使用「略陈旧」的时序信息。

归一化因子的刷新在 `update_norm_factors`，它揭示了一个重要细节——**线长归一化就是 1/bb_cost，时序归一化是 1/timing_cost 且有上限**：

[update_norm_factors place_util.cpp:14-48](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/place_util.cpp#L14-L48) — 每项归一化因子取该项当前总值的倒数；时序归一化用 `MAX_INV_TIMING_COST = 1e12` 封顶，防止时序约束很松时倒数爆炸；非时序驱动算法下 `timing_cost_norm` 置 NaN 表示不使用。

最后是 `PlacerState` 的结构，它解释了「布局阶段的可变状态住在哪里」：

[PlacerState placer_state.h:127-160](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec62818c9db312e24967de9d6283410ab19/vpr/src/place/placer_state.h#L127-L160) — 持有 `PlacerTimingContext`（时序代价相关：连接延迟、连接时序代价、setup slack 等）、`PlacerRuntimeContext`（耗时统计）与 `BlkLocRegistry`（块坐标 + 网格占位 + 物理引脚映射）；沿用 u3-l4 的 `Context` 不可拷贝基类与 `xxx()`/`mutable_xxx()` 双 getter 模式。

`Placer` 类本身的成员列表则展示了它要同时打理多少对象：

[Placer 类成员 placer.h:77-137](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/placer.h#L77-L137) — 持有选项、`t_placer_costs costs_`、`PlacerState placer_state_`、随机数发生器 `rng_`、各类代价处理器（`NetCostHandler`/`InterposerCostHandler`/`NocCostHandler`）、共享的 `PlaceDelayModel`、时序引擎 `SetupTimingInfo`、slacks/criticalities/invalidator，以及退火器 `annealer_` 与检查点 `placement_checkpoint_`。

#### 4.2.4 代码实践

**实践目标**：把「一次交换要评估哪些代价项」整理成清单，并理解归一化为何让不同量纲的项可加。

**操作步骤**：

1. 打开 [annealer.cpp:617](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L617)，记录 `CRITICALITY_TIMING_PLACE` 分支里 ΔC 由哪几项相加（线长、时序、拥塞、interposer、NoC）。
2. 打开 [place_util.cpp:14](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/place_util.cpp#L14)，确认每项归一化因子 = 该项当前总值的倒数。
3. 思考：若不做归一化，直接把 `bb_cost`（可能是几百几千）和 `timing_cost`（delay·criticality，量级不同）相加会怎样？

**需要观察的现象**：

- 默认算法 `CRITICALITY_TIMING_PLACE` 下，每次交换**至少**评估线长项与时序项；拥塞项只在 `congestion_factor > 0` 且 `rlim` 收缩到阈值后才启用（见 `outer_loop_update_timing_info_and_cost_terms`，[annealer.cpp:862-871](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L862-L871)），interposer/NoC 项仅在对应器件特性或选项开启时才出现。
- `timing_cost_norm` 被封顶在 `1e12`，避免时序约束很松时归一化因子爆炸。

**预期结果**：你会得到一张「代价项 → 是否默认启用 → 归一化方式 → 权重」的表，例如：

| 代价项 | 默认启用 | 归一化 | 权重系数 |
|---|---|---|---|
| bb_cost（线长） | 是 | `1/bb_cost` | `1 - timing_tradeoff` |
| timing_cost（时序） | 是（时序驱动） | `1/timing_cost`（≤1e12） | `timing_tradeoff` |
| congestion_cost | 否（需 `congestion_factor>0` 且 rlim 触发） | `1/congestion_cost` | `congestion_factor` |
| interposer / noc | 否（需对应器件/选项） | 各自倒数 | 各自 factor |

**运行行为待本地验证**：若已编译 `vpr`，可加 `--echo_placement_on` 之类调试开关或查看布局结束时的日志，它会打印 `bb_cost`、`timing_cost`、总 `cost` 的最终值——对照本表理解这些数字的来源。

#### 4.2.5 小练习与答案

**练习 1**：为什么布局过程中用局部 `PlacerState`，而不是直接写全局 `PlacementContext`？

> **答案**：布局是一个会反复试错、中途产生大量「半成品/被拒绝」中间状态的过程。若直接写全局上下文，布线阶段就有可能在布局未完成时读到不完整的坐标。VPR 用局部 `PlacerState` 隔离这些可变状态，只在布局全部结束、结果合法且（必要时）比对检查点后，才由 `update_global_state()` 一次性拷进全局 `PlacementContext`，保证全局状态始终是「已完成、一致」的。

**练习 2**：`get_total_cost` 与 `try_swap_` 里的 ΔC 公式是什么关系？

> **答案**：二者是「全量」与「增量」的同一份公式。`get_total_cost` 用当前各代价项总值算总代价（外层每温度调用一次，用于归一化与状态展示）；`try_swap_` 里的 ΔC 只计算受本次交换影响的增量，再套用相同的权重与归一化因子。这样每次交换只需局部重算，而不必从头算整张网表的代价。

### 4.3 退火调度

#### 4.3.1 概念说明

退火调度（annealing schedule）决定了「温度怎么降、每次降多少、什么时候停」。调度好坏直接决定布局质量：降太快会卡在劣解，降太慢会浪费算力。VPR 的调度状态全部封装在 `t_annealing_state` 里，核心是这几个量：

- **`t`**：当前温度。
- **`alpha`**：温度衰减因子，每外层迭代执行 `t *= alpha`。
- **`rlim`**：移动范围限制，约束每次交换的目标位置离原位置多远。
- **`crit_exponent`**：关键度指数，用于「锐化」时序关键度（让关键连接更突出）。
- **`move_lim`**：当前温度下内层循环尝试多少次交换。

VPR 提供两种调度，由 `e_sched_type` 选择：

- **`USER_SCHED`**：用户在命令行显式给定 `init_t`（初始温度）、`alpha_t`（衰减）、`exit_t`（退出温度），调度完全固定。适合调试与可复现实验。
- **`AUTO_SCHED`（默认）**：初始温度由试探性交换自动估计，`alpha` 随成功率动态调整，退出温度由代价量级自动推算。这是生产环境用的自适应调度。

此外还有一个独立的选项 `e_anneal_init_t_estimator`，决定 `AUTO_SCHED` 下初始温度的估计算法：

- **`COST_VARIANCE`**：用一批试探交换被接受代价的**标准差**除以一个常数作为初始温度。
- **`EQUILIBRIUM`**：用**二分搜索**找「期望代价变化为零」的平衡温度。

#### 4.3.2 核心流程

`AUTO_SCHED` 下每个外层迭代的调度更新逻辑（`outer_loop_update`）大致是：

```text
outer_loop_update(success_rate):
  # 1) 根据 success_rate 选 alpha（成功率越高，降温越快）
  if   success_rate > 0.96: alpha = 0.5     # 探索太散，猛烈降温
  elif success_rate > 0.80: alpha = 0.9
  elif success_rate > 0.15 或 rlim > 1: alpha = 0.95   # 正常缓降
  else:                     alpha = 0.8     # 几乎卡死，稍微回热式慢降

  # 2) 降温
  t *= alpha

  # 3) 判断是否到达退出温度
  t_exit = 0.005 · (cost / 线网数) · (1 + 拥塞/interposer 因子)
  if t < t_exit: 退出退火

  # 4) 更新移动范围 rlim（向 0.44 的目标成功率靠拢）
  rlim *= (1 - 0.44 + success_rate)        # 成功率高则放大范围，低则收缩
  rlim ∈ [FINAL_RLIM=1, UPPER_RLIM=网格边长-1]

  # 5) 更新关键度指数（rlim 越小，crit_exponent 越锐利）
  crit_exponent = 在 td_place_exp_first 与 td_place_exp_last 之间按 rlim 线性插值
```

两个关键直觉：

- **`rlim` 的目标是让成功率维持在约 0.44**。这是模拟退火的一个经验最优值——成功率太低说明搜索空间被限制得太死，太高说明限制太松。`update_rlim` 用 `rlim *= (1 - 0.44 + success_rate)` 这一简单公式把成功率拉回 0.44 附近：成功率高于 0.44 就放大范围（接受更多探索），低于 0.44 就收缩范围。
- **`crit_exponent` 随 `rlim` 收缩而锐化**。退火后期 `rlim` 很小、布局已较优，此时应把注意力集中在少数关键连接上，于是把关键度指数调大（关键连接显得更关键），引导搜索优先优化关键路径。

每温度的移动数 `move_lim` 由 `get_place_inner_loop_num_move` 计算：

\[
\texttt{move\_lim} = \texttt{inner\_num} \cdot (\text{num\_blocks})^{4/3}
\]

（`CIRCUIT` 缩放下；`DEVICE_CIRCUIT` 缩放还额外乘以器件面积的相关幂次）。即电路越大，每个温度要尝试的交换越多，保证统计意义上的充分采样。命令行 `--inner_num`（默认约 1.0）可整体缩放布局努力程度。

初始温度的估计发生在退火开始之前：构造退火器时先以一个极低温度（`1e-15`）做一批试探性交换，统计代价变化，再按所选估计器（COST_VARIANCE 或 EQUILIBRIUM）算出 `t0`。

#### 4.3.3 源码精读

退火状态类 `t_annealing_state` 的字段与含义：

[t_annealing_state annealer.h:76-137](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.h#L76-L137) — 公开成员 `t`、`alpha`、`num_temps`、`rlim`、`crit_exponent`、`move_lim`/`move_lim_max`，私有常量 `UPPER_RLIM`、`FINAL_RLIM=1`、`INVERSE_DELTA_RLIM`；对外暴露 `outer_loop_update()`，对内拆出 `update_rlim()` / `update_crit_exponent()`。注释里点明 `rlim` 与 `crit_exponent` 「Currently only updated by AUTO_SCHED」。

调度的核心 `outer_loop_update`，把 USER/AUTO 两条分支与自适应 alpha、t_exit、rlim、crit_exponent 全部串起来：

[t_annealing_state::outer_loop_update annealer.cpp:124-191](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L124-L191) — `USER_SCHED` 分支（L135-143）用固定 `alpha_t` 降温、以 `exit_t` 为终止条件；`AUTO_SCHED` 分支（L155 起）按 `cost/线网数` 推算 `t_exit`、按成功率选 alpha、降温，再更新 `rlim` 与 `crit_exponent`。

自适应 alpha 的四档阈值：

[alpha 自适应 annealer.cpp:164-175](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L164-L175) — `success_rate > 0.96 → alpha=0.5`；`> 0.8 → 0.9`；`> 0.15 或 rlim>1 → 0.95`；否则 `0.8`。注释说明这是「根据成功率自动调节 alpha」。

退出温度的推算：

[t_exit 计算 annealer.cpp:155-161](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L155-L161) — `t_exit = 0.005 · cost / 线网数`，并在器件有 interposer 切割或开了拥塞因子时按相应因子放大，注释解释这是因为额外代价项会抬高总代价量级。

移动范围与关键度指数的更新，分别对应「维持 0.44 成功率」与「随 rlim 锐化」两个直觉：

[update_rlim annealer.cpp:193-197](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L193-L197) — `rlim *= (1 - 0.44 + success_rate)`，再用 `[FINAL_RLIM, UPPER_RLIM]` 钳位；注释点明目标是「让接受概率维持在 0.44 附近」。

[update_crit_exponent annealer.cpp:199-206](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L199-L206) — 按 rlim 接近 FINAL_RLIM 的程度在 `td_place_exp_first` 与 `td_place_exp_last` 之间线性插值；rlim 越小、布局越精炼，指数越锐利，让关键连接更受关注。

每温度移动数公式：

[get_place_inner_loop_num_move place_util.cpp:79-98](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec62818c9db312e24967de9d6283410ab19/vpr/src/place/place_util.cpp#L79-L98) — `CIRCUIT` 缩放下 `move_lim = inner_num · num_blocks^(4/3)`，`DEVICE_CIRCUIT` 缩放下额外乘器件面积的幂次；最后用 `max(move_lim, 1)` 防止非正。

初始温度的估计入口与两种算法：

[estimate_starting_temperature_ annealer.cpp:315-330](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec62818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L315-L330) — `USER_SCHED` 直接返回用户给的 `init_t`；`AUTO_SCHED` 按估计器分派到 `COST_VARIANCE` 或 `EQUILIBRIUM`。

[estimate_starting_temp_using_cost_variance_ annealer.cpp:455-504](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L455-L504) — 做一批试探交换，统计被接受代价的标准差，返回 `std_dev / 64` 作为初始温度。

[estimate_equilibrium_temp_ annealer.cpp:332-453](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec62818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L332-L453) — 收集一批接受/拒绝交换的代价变化，用二分搜索找「期望代价变化为零」的平衡温度，注释详细说明了单调性依据与边界处理。

最后是相关的选项枚举与结构：

- [e_sched_type vpr_types.h:383-385](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L383-L385)：`AUTO_SCHED`（默认，统计驱动）/ `USER_SCHED`（用户固定）。
- [t_annealing_sched vpr_types.h:824-830](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L824-L830)：`type`、`inner_num`、`init_t`、`alpha_t`、`exit_t`。
- [e_anneal_init_t_estimator vpr_types.h:1024](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L1024)：`COST_VARIANCE` / `EQUILIBRIUM`，对应命令行 `--anneal_auto_init_t_estimator`。
- [e_place_algorithm vpr_types.h:853-857](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L853-L857) 与 [is_timing_driven vpr_types.h:905-907](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L905-L907)：默认 `CRITICALITY_TIMING_PLACE`，决定是否在调度里更新 `crit_exponent`。

#### 4.3.4 代码实践

**实践目标**：把 `AUTO_SCHED` 的「成功率 → alpha → 降温」自适应链条手工跑一遍，理解温度为何能自动收敛。

**操作步骤**：

1. 打开 [annealer.cpp:124](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L124) 的 `outer_loop_update`。
2. 假设当前 `t = 100`，手算三种成功率下的下一温度：
   - `success_rate = 0.97` → alpha = ? → `t` 变成 ?
   - `success_rate = 0.50` → alpha = ? → `t` 变成 ?
   - `success_rate = 0.10`（且 rlim 已为 1）→ alpha = ? → `t` 变成 ?
3. 打开 [annealer.cpp:193](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L193) 的 `update_rlim`，对同样的三种成功率计算 `rlim` 的乘数（`1 - 0.44 + success_rate`），验证「成功率高于 0.44 放大、低于 0.44 收缩」。
4. 打开 [place_util.cpp:79](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/place_util.cpp#L79)，假设 `inner_num=1`、电路有 1000 个块，计算 `move_lim`（应为 \(1000^{4/3} \approx 10000\)）。

**需要观察的现象**：

- 成功率 0.97 时 alpha=0.5，温度「腰斩」，因为探索过于发散、应快速降温聚焦。
- 成功率 0.50 时 alpha=0.95，温度缓慢下降，处于健康的探索-收敛平衡。
- 成功率 0.10 且 rlim 已到 1 时 alpha=0.8，介于二者之间——此时搜索接近冻结，调度选择温和降温避免过早锁死。
- `rlim` 乘数随成功率单调变化，把成功率稳定拉向 0.44。

**预期结果**：你会得到一张「成功率 → alpha → 新温度 → rlim 乘数」的对照表，直观看到调度是一个**负反馈系统**：成功率偏离 0.44 时，alpha 与 rlim 共同把它拉回来。**运行行为待本地验证**：若已编译 `vpr`，跑一个中等规模电路，布局日志会逐温度打印 `T`、成功率（`accept rate`）等，可对照本表观察 alpha 如何随成功率波动。

#### 4.3.5 小练习与答案

**练习 1**：`update_rlim` 为什么把目标成功率设在 0.44？

> **答案**：0.44 是模拟退火的一个经验最优接受率——在此率下，搜索既能充分探索（不会因范围太小而卡死），又能逐步收敛（不会因范围太大而一直随机游走）。`rlim *= (1 - 0.44 + success_rate)` 这一公式是个简单负反馈：成功率高于 0.44 就放大 `rlim`（引入更多探索、压低成功率），低于 0.44 就收缩 `rlim`（提高成功率），使成功率稳定在 0.44 附近。

**练习 2**：`crit_exponent` 为什么要在 `rlim` 收缩时变得更锐利？

> **答案**：退火后期 `rlim` 很小，布局已基本成型，剩下的是精细打磨关键路径。此时应把注意力集中在少数最关键的连接上。把 `crit_exponent` 调大能「锐化」关键度分布——关键连接显得更关键、非关键连接显得更不重要，从而引导搜索优先优化真正影响关键路径延迟的连接。`update_crit_exponent` 按 rlim 接近终值的程度在 `td_place_exp_first` 与 `td_place_exp_last` 之间线性插值实现这一点。

**练习 3**：淬火（quench）与正常退火的根本区别是什么？它为什么放在最后？

> **答案**：淬火把温度置零（`start_quench` 设 `t=0`），此时 `assess_swap_` 对任何 \(\Delta C > 0\) 直接 REJECTED，搜索退化为纯贪心——只接受改进移动。它放在最后是因为：经过完整退火后布局已在一个很好的区域内，此时不再需要「翻山越岭」的探索，而需要精确的局部打磨来榨干最后的代价改善；同时 `move_lim` 恢复到最大值，保证有足够的采样把局部最优找出来。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「布局退火全链路追踪」任务：

1. **入口与编排**：从 `try_place`（[place.h:8-19](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/place.h#L8-L19)）出发，确认它构造 `Placer` 并调用 `Placer::place()`（[placer.cpp:352](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/placer.cpp#L352)）。画出 `Placer` → `PlacementAnnealer` 的调用关系，标注谁负责外层温度循环、谁负责内层交换。
2. **代价清单**：在 [annealer.cpp:617-637](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L617-L637) 记录默认算法下一次交换评估的全部代价项，并在 [place_util.cpp:14-48](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/place_util.cpp#L14-L48) 标出每项的归一化方式与权重。
3. **接受准则**：在 [annealer.cpp:1105-1126](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L1105-L1126) 写下接受概率公式，并解释 `t` 与 `delta_c` 各自如何影响结果。
4. **调度自适应**：在 [annealer.cpp:124-191](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L124-L191) 追踪 `success_rate → alpha → t` 与 `success_rate → rlim` 两条负反馈链，说明它们如何把成功率稳定在 0.44、把温度降到 t_exit。

最终产出一张「从 `try_place` 到淬火结束」的完整流程图，图上标清：外层循环、内层循环、代价项、接受准则、调度变量、淬火段。这张图就是接下来 u5-l2（移动生成器）与 u5-l3（代价/延迟模型）要逐层填实的骨架。

## 6. 本讲小结

- 布局把 `ClusteredNetlist` 的每个块摆到 `DeviceGrid` 的合法位置，目标是**最小化线长代价与时序代价**；因为代价函数非凸，VPR 用**模拟退火**而非纯贪心。
- 模拟退火的灵魂是接受准则：变好（\(\Delta C \le 0\)）的移动总是接受，变差的移动按 \(\exp(-\Delta C / T)\) 的概率接受；温度高时容忍变差、广泛探索，温度低时趋近贪心、精细收敛（`assess_swap_`）。
- 框架是 `Placer`（指挥官）+ `PlacementAnnealer`（执行者）的双层结构：`Placer::place()` 跑一个「逐温度 `do-while` + 淬火」的外层循环，每个温度调用 `placement_inner_loop()` 做内层交换；退火结束后做最终时序分析、检查点比对与合法性校验。
- 代价由 `t_placer_costs` 聚合，默认算法 `CRITICALITY_TIMING_PLACE` 下每次交换评估**线长 + 时序**（外加可选的拥塞、interposer、NoC 项），各项用「该项总值的倒数」做归一化、用 `timing_tradeoff` 等权重加权（`get_total_cost` / `update_norm_factors`）。
- 退火调度封装在 `t_annealing_state`：`AUTO_SCHED`（默认）下 `alpha` 随成功率四档自适应、`rlim` 把成功率拉向 0.44、`crit_exponent` 随 `rlim` 收缩而锐化、退出温度由 `cost/线网数` 推算；初始温度由 `COST_VARIANCE` 或 `EQUILIBRIUM` 估计器自动算出。
- 布局的可变中间状态住在局部 `PlacerState`（含 `BlkLocRegistry` 与 `PlacerTimingContext`），只在全部结束后才由 `update_global_state()` 拷进全局 `PlacementContext`，保证全局状态始终一致完整。

## 7. 下一步学习建议

本讲是布局单元的骨架，接下来的讲义会往骨架里填肉：

- **u5-l2 移动生成器**：深入 `move_generators/` 目录，看退火每一步的「交换/搬移」具体是怎么生成的——均匀随机、关键路径优先、强化学习（`simpleRL_move_generator`）、质心等不同策略如何决定探索方向。本讲的 `try_swap_` 第①步「生成移动」正是它们的调用点。
- **u5-l3 布局代价与延迟模型**：拆解 `delay_model/` 下的 `delta/simple/override` 延迟模型如何估算连接延迟，以及 `move_transactions` 与 `compressed_grid` 如何支撑增量代价计算与事务回滚。本讲 4.2 的「时序项」与归一化在那里有完整展开。

建议先把本讲的「双层循环 + 代价项 + 调度」流程图画稳，再带着它进入 u5-l2——你会很容易把各种移动生成器放进 `placement_inner_loop` 的第①步里。
