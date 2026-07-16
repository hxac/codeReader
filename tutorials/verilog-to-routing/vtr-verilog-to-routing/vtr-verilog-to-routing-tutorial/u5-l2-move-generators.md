# 移动生成器

## 1. 本讲目标

本讲聚焦 VPR 模拟退火布局中一个极其关键的角色：**移动生成器（Move Generator）**。

在 u5-l1 里我们建立了退火的整体框架：`Placer` 指挥、`PlacementAnnealer` 在每个温度下反复「试交换」。但有一个问题被刻意绕开了——**每一次「试交换」到底交换哪两个块、把它们挪到哪里？** 这正是移动生成器要回答的问题。它决定了退火每一步如何扰动布局，直接关系到布局质量与收敛速度。

学完本讲，你应当能够：

1. 说清楚移动生成器在退火循环中的位置与职责。
2. 列出 `move_generators/` 目录下的全部移动生成器，并按「叶子型 / 聚合型」分类。
3. 读懂各类移动的选址策略：均匀、中位数、质心、加权质心、关键块均匀、可行域等。
4. 理解 `SimpleRLMoveGenerator` 如何用一个 k-臂赌徒 RL 智能体动态学习「该用哪一种移动」。
5. 区分两种典型的聚合策略：静态概率（`StaticMoveGenerator`）与强化学习（`SimpleRLMoveGenerator`）。

---

## 2. 前置知识

在进入源码前，先用通俗语言建立几个直觉。

### 2.1 移动（move）是什么

布局阶段，所有聚簇块（CLB）都已经摆在 `DeviceGrid` 的格子里。**一次移动**就是从当前布局里挑一个块 `b_from`，给它找一个目标位置 `to`，把它挪过去（若 `to` 被占，就与那里的块互换）。这一步在源码里被抽象成一个纯函数式的「提案」：`propose_move()` 只负责把这次扰动登记到 `blocks_affected` 里，**不立即改全局状态**，是否采纳由退火器按 Metropolis 准则裁定（见 u5-l1）。

### 2.2 「挑块」与「挑位置」是两件事

任何移动都可以拆成两步：

1. **挑哪个块搬**（from）：随机？挑关键路径上的块？挑特定块类型？
2. **搬到哪**（to）：在原地附近随机撒（受 `rlim` 约束）？搬到能最小化线长的中位数点？搬到「合力为零」的质心点？

不同移动生成器的差异，本质上就是这两步的策略组合。

### 2.3 k-臂赌徒（k-armed bandit）直觉

强化学习移动用到了一个经典模型——多臂赌徒：

- 有 k 台老虎机（这里 k = 可选移动种类），每台回报的期望未知。
- 每一轮你选一台拉一下，得到一个奖励（reward）。
- 你要平衡 **探索（exploration，试试不熟悉的机器）** 与 **利用（exploitation，多拉目前看来最好的）**，逐步逼近最优策略。

VPR 把「选哪种移动」当作这个赌徒问题：每种移动类型是一台老虎机，Q 值是该移动「平均能带来多少好处」的估计，每拉一次用退火代价的变化作为奖励去更新 Q 表。这是一个无需环境模型、在线增量学习的轻量 RL。

### 2.4 前置讲义衔接

本讲直接承接 **u5-l1（布局总览与模拟退火框架）**：那里讲过的接受准则 `exp(−ΔC/T)`、`rlim` 范围限制、`t_placer_costs` 代价聚合、`PlacerState` 局部状态等概念，本讲默认你已掌握，不再重复。同时用到了 u3-3（ClusteredNetlist）的聚簇块概念与 u2-3 的 `DeviceGrid`。

---

## 3. 本讲源码地图

本讲涉及的关键文件全部位于 `vpr/src/place/` 下：

| 文件 | 作用 |
|------|------|
| `move_generators/move_generator.h` | 抽象基类 `MoveGenerator`、奖励函数枚举 `e_reward_function`、移动结果统计 `MoveTypeStat`。 |
| `move_utils.h` | 移动相关的核心枚举与工具：`e_move_type`、`e_move_result`、`e_create_move`、`t_propose_action`，以及各类「选址」辅助函数。 |
| `move_generators/uniform_move_generator.*` | 经典均匀移动（随机挑块 + 原地附近随机选址）。 |
| `move_generators/critical_uniform_move_generator.*` | 关键块均匀移动（只挑关键路径上的块）。 |
| `move_generators/median_move_generator.*` 与 `weighted_median_move_generator.*` | 中位数移动：把块搬到最小化 HPWL 的中位数区域；加权版按关键度加权。 |
| `move_generators/centroid_move_generator.*` 与 `weighted_centroid_move_generator.*` | 质心移动：把块搬到「合力为零」位置；可被 NoC 吸引；加权版按连接数/关键度加权。 |
| `move_generators/feasible_region_move_generator.*` | 可行域移动：搬入能最小化关键路径延迟的区域。 |
| `move_generators/static_move_generator.*` | **聚合型**：按用户给定的固定概率轮询各种移动。 |
| `move_generators/simpleRL_move_generator.*` | **聚合型**：用一个 k-臂赌徒 RL 智能体动态选择移动类型。 |
| `move_generators/manual_move_generator.*` | 手动移动：供 GUI 调试用，由用户指定交换。 |
| `RL_agent_util.*` | `create_move_generators`（按布局选项实例化一对生成器）与 `select_move_generator`（按退火阶段选其一）。 |

> 提示：`move_generators/` 下共有 11 个头文件、11 个对应实现文件，定义了 10 个 `MoveGenerator` 派生类，外加 3 个 RL 智能体类（`KArmedBanditAgent` / `EpsilonGreedyAgent` / `SoftmaxAgent`，定义在 `simpleRL_move_generator.h` 中）。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**移动生成器接口** → **移动类型策略** → **强化学习移动**。

### 4.1 移动生成器接口

#### 4.1.1 概念说明

移动生成器是一个**抽象基类 `MoveGenerator`**，定义了「提议一次移动」与「接收上一次移动结果反馈」这两个核心动作。所有具体策略（均匀、质心、RL……）都从它派生。

它解决的问题是：把退火器对「怎么扰动布局」的依赖，从硬编码退化成**可插拔的策略对象**。退火器（`PlacementAnnealer`）只持有一个 `MoveGenerator&` 引用，每步调用 `propose_move()`，不关心具体是哪种策略。这样既支持经典均匀移动，也支持基于 RL 的智能选择，且新加一种移动类型无需改动退火器。

#### 4.1.2 核心流程

一个移动生成器的生命周期与退火主循环耦合如下：

```
每个温度的内层循环，重复 move_lim 次：
  ┌─ move_generator.propose_move(...)   // 1. 提议：挑块 + 选址，登记到 blocks_affected
  │      返回 e_create_move::VALID 或 ABORT
  ├─ 退火器评估 ΔC，按 Metropolis 准则决定 ACCEPTED / REJECTED / ABORTED
  └─ move_generator.calculate_reward_and_process_outcome(...)  // 2. 反馈：RL 智能体据此更新 Q 表
```

关键点：

- `propose_move()` 是**纯虚函数**，每个派生类必须实现；它返回 `e_create_move` 表示这次提案是否合法可执行。
- `process_outcome()` 默认是**空实现**——普通的叶子移动生成器不需要学习，收到反馈什么都不做；只有 RL 聚合生成器会重写它来更新智能体。
- 两者通过 `t_propose_action` 这个小结构体串联：它记录「这一次用了哪种移动类型、哪种块类型」，既用于提议，也用于统计。

#### 4.1.3 源码精读

先看核心枚举与提案结构。`e_move_type` 列出了所有可选的移动类型，`e_move_result` 是退火器裁定后的三态结果，`t_propose_action` 是提案时在生成器与退火器间传递的动作描述：

[vpr/src/place/move_utils.h:23-27](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_utils.h#L23-L27) —— `e_move_result` 三态枚举：`REJECTED`（被退火准则拒绝）、`ACCEPTED`（被接受）、`ABORTED`（因进位链等限制无法执行）。

[vpr/src/place/move_utils.h:30-42](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_utils.h#L30-L42) —— `e_move_type` 枚举所有移动类型，注意 `NUMBER_OF_AUTO_MOVES` 是一个**哨兵值**，正好分隔「自动移动」与 `MANUAL_MOVE`，后续 `StaticMoveGenerator` 会用它作为数组大小。

[vpr/src/place/move_utils.h:55-58](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_utils.h#L55-L58) —— `t_propose_action`：`move_type` 是本次移动种类，`logical_blk_type_index = -1` 表示「不限定块类型、随便挑」，RL 智能体可以选择把它设成具体块类型。

接着看基类本身：

[vpr/src/place/move_generators/move_generator.h:88-116](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/move_generator.h#L88-L116) —— `MoveGenerator` 类与构造函数。构造时注入五样东西：可变引用 `placer_state_`、只读 `place_macros_`（进位链等宏块信息）、只读 `net_cost_handler_`（线网代价计算）、奖励函数 `reward_func_`、随机数源 `rng_`。注意它 `delete` 了默认构造、拷贝构造与拷贝赋值——生成器持有引用，**禁止拷贝**，避免状态被意外共享/分裂。

[vpr/src/place/move_generators/move_generator.h:133-137](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/move_generator.h#L133-L137) —— 纯虚 `propose_move()`：参数 `blocks_affected`（输出）、`proposed_action`（入参兼出参）、`rlim`（当前范围限制）、`placer_opts`、`criticalities`（时序关键度，供时序导向移动使用）。这是所有派生类必须实现的接口。

[vpr/src/place/move_generators/move_generator.h:147](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/move_generator.h#L147) —— 虚函数 `process_outcome()` 的**空默认实现** `{}`。普通移动生成器收到反馈无需做事；RL 生成器会 override 它。

[vpr/src/place/move_generators/move_generator.h:158-160](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/move_generator.h#L158-L160) —— 非虚的 `calculate_reward_and_process_outcome()`：先按奖励函数算出 reward，再调用 `process_outcome()`。它是个模板方法，把「算奖励」逻辑固定在基类，把「用奖励学习」逻辑下放给派生类。

最后看退火器是怎么调用这两个接口的：

[vpr/src/place/annealer.cpp:562](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L562) —— `try_swap_()` 内部调用 `move_generator.propose_move(...)` 提议一次移动（手动移动和布线块移动有专门分支）。

[vpr/src/place/annealer.cpp:815](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L815) —— 在评估完 ΔC 并裁定结果后，调用 `calculate_reward_and_process_outcome(...)` 把反馈回传给生成器（仅当这次移动确实来自移动生成器、而非手动/布线块移动时）。

#### 4.1.4 代码实践

**实践目标**：确认基类接口的两条路径（提议 + 反馈），并看清退火器如何与生成器交互。

**操作步骤**：

1. 打开 `move_generator.h`，定位 `propose_move`（纯虚）与 `process_outcome`（空默认实现）。
2. 在 `annealer.cpp` 中搜索 `propose_move` 与 `calculate_reward_and_process_outcome` 两个调用点，观察它们的先后顺序。
3. 阅读调用点之间约 250 行（562→815）的逻辑，思考：在「提议」与「反馈」之间，退火器做了哪些事？

**需要观察的现象**：提议在前、反馈在后；中间是「记录受影响线网 → 增量算 ΔC → Metropolis 裁定」。

**预期结果**：你会看到一次完整的 try_swap_ 是「提议移动 → 算代价 → 裁定接受/拒绝 → 回传奖励」四步闭环。

**待本地验证**：第 3 步要读懂约 250 行代价评估代码，若一时读不完，可先记下函数名与注释，后续配合 u5-l3（延迟/代价模型）再回看。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `MoveGenerator` 要 `delete` 拷贝构造与拷贝赋值？

> 参考答案：因为它持有多处引用（如 `placer_state_` 是 `std::reference_wrapper`）与注入的常量引用。拷贝会导致两个对象指向同一份被引用状态，语义混乱；且移动生成器是「单例式」策略对象，本就不应被复制。这与 u3-4 里 `Context` 不可拷贝的动机一致。

**练习 2**：`propose_move` 是纯虚函数，`process_outcome` 是带空实现的虚函数，这种差异设计有何用意？

> 参考答案：每个派生类**必须**实现提议逻辑（没有默认行为可言），故纯虚；而「接收反馈」只有 RL 生成器关心，普通叶子生成器无需任何动作，故提供空默认实现，避免强制每个派生类都写一个空壳。

---

### 4.2 移动类型策略

#### 4.2.1 概念说明

本模块讲解各类**叶子型移动生成器**——它们真正决定「挑哪个块、搬到哪」。这一类是退火扰动的「兵种」，各有专长。

> ⚠️ 概念澄清（也是后面 RL 模块的关键铺垫）：`SimpleRLMoveGenerator` 与 `StaticMoveGenerator` **不是叶子**，而是**聚合型**生成器——它们内部持有一组叶子生成器，负责「决定这次用哪种叶子」。所以「比较 simpleRL 与 critical_uniform」其实是在比较两个**不同层次**的东西：前者是「选哪种移动的策略」，后者是「一种具体的移动」。

把叶子移动按「挑选 from 块」与「选择 to 位置」两个维度分类，可以列出下表：

| 移动类型 | 挑 from 块 | 选 to 位置 | 主要目标 |
|----------|-----------|-----------|---------|
| `UNIFORM` | 随机挑任意可动块 | 原地附近 `rlim` 范围内随机撒 | 全局均匀探索（经典 VPR 移动） |
| `CRIT_UNIFORM` | 只挑关键路径上的块 | 同上，原地附近随机撒 | 时序优化 |
| `MEDIAN` | 随机挑块 | 搬到最小化 HPWL 的**中位数区域** | 线长优化 |
| `W_MEDIAN` | 随机挑块 | 按**关键度加权**的中位数区域 | 时序+线长 |
| `CENTROID` | 随机挑块 | 搬到连接的**质心（合力为零点）** | 线长优化（解析式直觉） |
| `W_CENTROID` | 随机挑块 | 按**连接数/关键度加权**的质心 | 时序+线长 |
| `FEASIBLE_REGION` | 只挑关键块 | 搬入能最小化关键路径延迟的**可行域** | 时序优化 |
| `NOC_ATTRACTION_CENTROID` | 随机挑块 | 质心被拉向同组 NoC 路由器 | NoC 场景 |
| `MANUAL_MOVE` | 用户指定 | 用户指定 | GUI 调试 |

#### 4.2.2 核心流程

各类叶子移动的通用骨架是「挑块 → 选址 → 登记受影响块」，可以用伪代码概括：

```
propose_move(blocks_affected, proposed_action, rlim, opts, criticalities):
    b_from = 挑一个块(...)              # 关键差异点 1：是否限定关键块/块类型
    if 没有 b_from: return ABORT
    to = 选一个目标位置(b_from, rlim, ...)  # 关键差异点 2：均匀/中位数/质心/可行域
    if 找不到合法 to: return ABORT
    create_move(blocks_affected, b_from, to, ...)   # 登记受影响块（含宏块展开）
    if 不满足 floorplan 约束: return ABORT
    return VALID
```

三类「选址」直觉（对应 `move_utils.h` 中的 `find_to_loc_uniform` / `find_to_loc_median` / `find_to_loc_centroid`）：

- **均匀选址**：在以 `from` 为中心、半径 `rlim` 的方形区域内均匀随机取点。
- **中位数选址**：对块连接的所有线网，分别计算其包围盒（bounding box）的 x、y 坐标，取所有坐标的**中位数**作为目标区域中心。直觉：把块放到所有连接的「中间」，最小化半周长线长 HPWL。对一条连接了若干引脚的线网，其包围盒边长决定了线长下界，中位数位置正是使最大边长最小的点。
- **质心选址**：把块的每条连接建模为一根弹簧，求**合力为零**的位置，即所有邻居位置的加权平均（质心）。这借鉴了解析式布局器（analytical placer）的受力模型。

中位数的数学直觉可写作：对块的所有邻居坐标 \((x_i, y_i)\)，求使最大距离最小的点。一维情况下，使 \(\max_i |x - x_i|\) 最小的 \(x\) 正是这些 \(x_i\) 的中位数（位于 min 与 max 之间的任意点等价，代码取中位数区域中心再加随机扰动）。质心则是加权平均：

\[
x_{\text{centroid}} = \frac{\sum_i w_i x_i}{\sum_i w_i}, \quad
y_{\text{centroid}} = \frac{\sum_i w_i y_i}{\sum_i w_i}
\]

加权版把权重 \(w_i\) 取为连接的关键度，使时序关键的连接对质心拉力更大。

#### 4.2.3 源码精读

**均匀移动**——经典 VPR 移动，挑块时 `highly_crit_block=false`：

[vpr/src/place/move_generators/uniform_move_generator.cpp:28-52](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/uniform_move_generator.cpp#L28-L52) —— 调用 `propose_block_to_move(..., highly_crit_block=false, ...)` 任意挑一个可动块；再用 `find_to_loc_uniform(...)` 在 `rlim` 范围内随机选址。这是最朴素的「全局均匀探索」。

[vpr/src/place/move_generators/uniform_move_generator.h:7-12](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/uniform_move_generator.h#L7-L12) —— 头文件注释明确：「随机挑一个块（所有块等概率），再在压缩网格空间以该块为中心、`rlim` 为半径的范围内随机移动」。

**关键块均匀移动**——与均匀移动唯一的关键区别是挑块时只挑关键块：

[vpr/src/place/move_generators/critical_uniform_move_generator.cpp:31-38](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/critical_uniform_move_generator.cpp#L31-L38) —— 这里 `highly_crit_block=true`，并传入 `criticalities`、`&net_from`、`&pin_from`，要求 `propose_block_to_move` 只从「有一条或多条关键线网」的块里挑。选址逻辑与均匀移动完全一样（同样调用 `find_to_loc_uniform`），区别只在**挑谁**。

[vpr/src/place/move_generators/critical_uniform_move_generator.h:8-17](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/critical_uniform_move_generator.h#L8-L17) —— 注释点明：「从关键块（含一条或多条关键线网的块）里随机挑一个，移到 `rlim` 范围内的随机位置」。

**质心移动**——搬到「合力为零」点，并可被 NoC 吸引：

[vpr/src/place/move_generators/centroid_move_generator.h:7-21](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/centroid_move_generator.h#L7-L21) —— 注释解释：「根据块与其他块的连接，计算作用在块上的力/权重，搬到合力为零的位置」；并说明当开启 NoC 吸引时，质心会被拉向同组（仅经低扇出线网可达）的 NoC 路由器。

[vpr/src/place/move_generators/centroid_move_generator.h:84-96](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/centroid_move_generator.h#L84-L96) —— `calculate_centroid_loc_()` 计算精确质心位置；当开启加权时，`criticalities` 指针非空，权重取自关键度。

**中位数移动**——搬到最小化 HPWL 的中位数区域：

[vpr/src/place/move_generators/median_move_generator.h:7-19](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/median_move_generator.h#L7-L19) —— 注释：「以线长最小化为目标，把随机块搬进它的中位数区域——即最小化 HPWL 的位置范围。遍历移动块所有引脚所在线网，算出各线网包围盒坐标，分别放进 x/y 向量取中位数」。

**加权中位数 / 加权质心**——继承自对应非加权版，按关键度加权：

[vpr/src/place/move_generators/weighted_centroid_move_generator.h:7-17](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/weighted_centroid_move_generator.h#L7-L17) —— 加权质心「借鉴解析式布局器：把线网连接建模为弹簧，算受力平衡位置」，直接继承 `CentroidMoveGenerator` 复用代码（见其 `weighted_` 标志）。

[vpr/src/place/move_generators/weighted_median_move_generator.h:7-15](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/weighted_median_move_generator.h#L7-L15) —— 加权中位数「按引起每条包围盒边的引脚的时序关键度，对包围盒边加权后取中位数」。

**可行域移动**——搬入最小化关键路径延迟的区域：

[vpr/src/place/move_generators/feasible_region_move_generator.h:7-21](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec62818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/feasible_region_move_generator.h#L7-L21) —— 引用 Chen et al. FPGA 2005 的工作，可行域是「使块放置后能最小化关键路径延迟的位置/区域」，算法是挑关键块 → 找其关键输入与最关键输出 → 按论文方法算可行域。

**辅助选址函数**——三种选址策略的统一入口：

[vpr/src/place/move_utils.h:226-286](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_utils.h#L226-L286) —— `find_to_loc_uniform` / `find_to_loc_median` / `find_to_loc_centroid` 三个选址函数，注释说明它们都在「压缩网格（compressed grid）」空间工作——这是把器件网格上同类型的瓦片压缩成稠密坐标的优化（详见 u5-l3），让 `rlim` 表示「同类型资源的距离」而非原始物理距离。

#### 4.2.4 代码实践（本讲核心实践任务）

**实践目标**：列出 `move_generators/` 目录下所有移动生成器，并比较 `simpleRL` 与 `critical_uniform` 两种策略的选择依据。

**操作步骤**：

1. 列出目录下所有头文件（用 `Glob` 或 `ls vpr/src/place/move_generators/`）。
2. 为每个生成器归类：叶子型还是聚合型？它挑块的策略是什么？选址的策略是什么？
3. 重点比较 `SimpleRLMoveGenerator` 与 `CriticalUniformMoveGenerator`。

**需要观察的现象**：你会得到约 10 个 `MoveGenerator` 派生类，其中 `StaticMoveGenerator` 和 `SimpleRLMoveGenerator` 是聚合型（内部持有 `vtr::vector<e_move_type, std::unique_ptr<MoveGenerator>> all_moves`），其余是叶子型。

**预期结果（目录清单与对比）**：

目录下的移动生成器（类名）：

- 叶子型：`UniformMoveGenerator`、`MedianMoveGenerator`、`CentroidMoveGenerator`、`WeightedCentroidMoveGenerator`、`WeightedMedianMoveGenerator`、`CriticalUniformMoveGenerator`、`FeasibleRegionMoveGenerator`、`ManualMoveGenerator`。
- 聚合型：`StaticMoveGenerator`（固定概率）、`SimpleRLMoveGenerator`（RL 学习）。
- 另有 3 个 RL 智能体类（不是 `MoveGenerator` 派生）：`KArmedBanditAgent`、`EpsilonGreedyAgent`、`SoftmaxAgent`。

`simpleRL` 与 `critical_uniform` 的对比——**它们根本不在同一层**：

| 维度 | `SimpleRLMoveGenerator` | `CriticalUniformMoveGenerator` |
|------|------------------------|------------------------------|
| 抽象层次 | 聚合型（元策略） | 叶子型（具体移动） |
| 解决的问题 | 「这次该用哪一种移动？」 | 「这一次具体怎么扰动布局？」 |
| 选择依据 | 维护 Q 表，按 RL 策略（ε-greedy 或 softmax）在多种移动间分配概率 | 固定地只挑关键路径块、原地附近随机选址 |
| 是否学习 | 是，每次移动后用代价变化更新 Q 表 | 否，`process_outcome` 是空实现 |
| 内部结构 | 持有一组叶子生成器 + 一个 RL 智能体 | 直接实现 `propose_move` |

一句话总结：`SimpleRLMoveGenerator` 是**调度员**，它会（在 `propose_move` 里）调用包括 `CriticalUniformMoveGenerator` 在内的叶子生成器；而 `critical_uniform` 是被它调度的一种**具体兵种**。

> 注：`CriticalUniformMoveGenerator` 是否被 `SimpleRLMoveGenerator` 调度，取决于退火阶段——见 4.3.3，它只出现在「第二状态」（退火后期/淬火）的可用移动列表里。

#### 4.2.5 小练习与答案

**练习 1**：`UniformMoveGenerator` 和 `CriticalUniformMoveGenerator` 的源码几乎一样，唯一区别在哪？这个区别如何影响布局结果？

> 参考答案：唯一区别是调用 `propose_block_to_move` 时传 `highly_crit_block` 一个为 `false`、一个为 `true`（前者不传 `criticalities`，后者传）。均匀版随机挑任意可动块，倾向于全局探索、改善平均线长；关键版只挑时序关键块，把有限的扰动预算花在能改善关键路径的块上，优化最高工作频率。两者选址逻辑完全相同（都用 `find_to_loc_uniform`）。

**练习 2**：中位数移动和质心移动都以「减小线长」为导向，它们的几何直觉有何不同？

> 参考答案：中位数是把块放到所有连接邻居坐标的「中位数点」，最小化的是 HPWL（包围盒半周长），对离群邻居鲁棒（中位数不受极端值影响）；质心是把块放到所有邻居坐标的加权平均（重心），等价于弹簧受力平衡点，对每个邻居一视同仁地拉。中位数对最小化最大边长最优，质心对最小化总距离（平方和）更自然。

**练习 3**：为什么「加权」版的中位数/质心要把权重取为时序关键度？

> 参考答案：关键度高的连接对延迟影响大，给它更大权重，相当于「优先把块拉近时序关键的邻居」，从而在减小线长的同时额外优化关键路径延迟——这是把线长目标与时序目标融合进同一种定向移动的方式。

---

### 4.3 强化学习移动

#### 4.3.1 概念说明

本模块讲解 `SimpleRLMoveGenerator`——**唯一会「学习」的聚合型生成器**。它的核心思想是：与其用固定概率（`StaticMoveGenerator`）在各种移动间轮询，不如让一个**强化学习智能体**在线学习「当前阶段哪种移动最有效」，动态调整各移动被选中的概率。

这里用的是 RL 中最简单的模型之一：**k-臂赌徒（k-armed bandit）**。它不需要环境模型、不需要状态转移，只在「k 种动作」之间做增量学习，开销极小，适合嵌在退火每一步里。VPR 提供了两种经典赌徒算法：**ε-greedy** 与 **softmax**。

值得强调的是，`SimpleRLMoveGenerator` 本身**也是一个 `MoveGenerator`**——它的 `propose_move` 不直接扰动布局，而是先问智能体「这次用哪种移动」，再委托给对应的叶子生成器；它的 `process_outcome` 把退火反馈转成奖励去训练智能体。这种「用组合代替继承」的设计，让它能无缝替换 `StaticMoveGenerator`。

#### 4.3.2 核心流程

整体数据流：

```
退火每一步：
  SimpleRLMoveGenerator.propose_move(...)
    │
    ├─ proposed_action = agent.propose_action()     // 智能体按 Q 表选一种移动（+块类型）
    │      └─ ε-greedy: 以 ε 随机探索，否则取 argmax Q
    │      └─ softmax:  以 P(a) ∝ exp(f(Q(a))) 采样
    │
    └─ all_moves[action.move_type].propose_move(...)  // 委托给叶子生成器真正扰动

  退火器评估 ΔC、裁定结果

  SimpleRLMoveGenerator.calculate_reward_and_process_outcome(...)
    ├─ 按 reward_func 把 ΔC 换算成 reward
    └─ agent.process_outcome(reward, ...)            // 增量更新 Q[last_action]
           Q(a) ← Q(a) + step·(reward − Q(a))
```

**奖励函数**（`e_reward_function`）决定「什么样的移动算好」，共有四种（见 `move_generator.h:73-79`）：

- `BASIC`：直接用 `−ΔC`（代价下降越多奖励越高，上升为负奖励）。
- `NON_PENALIZING_BASIC`：代价下降给 `−ΔC`，上升给 0（不惩罚爬山）。
- `RUNTIME_AWARE`：在 `NON_PENALIZING_BASIC` 基础上，按各移动的相对运行时归一化（贵的移动要更值）。
- `WL_BIASED_RUNTIME_AWARE`：在运行时感知基础上，再按线长/时序权重偏向（默认奖励函数）。

**Q 值更新**是增量平均或指数加权平均（由 `--place_agent_gamma` 控制）：

- 增量平均（`gamma < 0`）：步长 \( \alpha = 1/n \)，n 为该动作累计被选次数。
- 指数加权（`gamma ∈ [0,1]`）：令「`move_lim` 步前的样本权重占 `gamma`」，反解出

\[
\alpha = 1 - e^{\ln(\text{gamma}) / \text{move\_lim}}
\]

更新规则统一为：

\[
Q(a) \leftarrow Q(a) + \alpha \cdot (\text{reward} - Q(a))
\]

**两种智能体的选择策略**：

- **ε-greedy**：以概率 ε 在所有动作里均匀随机探索，以概率 \(1-\epsilon\) 取当前 Q 最大的动作（利用）。默认 ε = 0.3。
- **softmax**：每个动作的选中概率正比于其 Q 值的缩放截断指数：

\[
P(a) = \frac{\exp(f(Q(a)))}{\sum_b \exp(f(Q(b)))}, \quad f(x) = \min(1000x,\, 3)
\]

其中截断 `min(1000x, 3)` 防止 Q 值差距过大导致概率塌缩到单一动作。

#### 4.3.3 源码精读

**SimpleRLMoveGenerator——聚合 + 委托**：

[vpr/src/place/move_generators/simpleRL_move_generator.cpp:21-28](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/simpleRL_move_generator.cpp#L21-L28) —— `propose_move` 只有短短两行：先让智能体 `propose_action()` 选出 `move_type`，再委托 `all_moves[move_type]->propose_move(...)`。注意 `all_moves` 是按 `e_move_type` 索引的叶子生成器数组，构造时一次性建好。

[vpr/src/place/move_generators/simpleRL_move_generator.cpp:30-32](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/simpleRL_move_generator.cpp#L30-L32) —— `process_outcome` 同样只做转发，把奖励交给智能体更新。

[vpr/src/place/move_generators/simpleRL_move_generator.h:257-286](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/simpleRL_move_generator.h#L257-L286) —— 构造函数模板：按 `e_move_type` 把 7~8 种叶子生成器实例化进 `all_moves`（开启 NoC 时多一个 `NOC_ATTRACTION_CENTROID`）。构造函数用 `enable_if` 限定只接受 `EpsilonGreedyAgent` 或 `SoftmaxAgent`，否则编译期报错。

**KArmedBanditAgent——Q 表与更新**：

[vpr/src/place/move_generators/simpleRL_move_generator.h:17-31](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/simpleRL_move_generator.h#L17-L31) —— `KArmedBanditAgent` 基类与纯虚 `propose_action()`。注意动作空间可以是「只选移动类型」或「选移动类型 + 块类型」（`e_agent_space`），后者 Q 表是 `移动数 × 块类型数` 的二维表。

[vpr/src/place/move_generators/simpleRL_move_generator.h:91-104](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/simpleRL_move_generator.h#L91-L104) —— 关键成员：`q_`（每个动作的估计价值）、`num_action_chosen_`（每个臂被拉次数 n）、`time_elapsed_`（各移动类型的相对运行时，用于运行时感知奖励）、`exp_alpha_`（步长，<0 表示用增量平均）。

[vpr/src/place/move_generators/simpleRL_move_generator.cpp:120-149](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/simpleRL_move_generator.cpp#L120-L149) —— `KArmedBanditAgent::process_outcome`：先 `++num_action_chosen_[last_action_]`；若奖励函数是运行时感知，把 reward 除以该移动类型的运行时 `time_elapsed_[move_type]`；再按 `exp_alpha_` 决定步长（增量平均 `1/n` 或指数加权 `exp_alpha_`）；最后 `q_[last_action_] += step * (reward - q_[last_action_])`。这正是前面给出的 Q 更新公式。

[vpr/src/place/move_generators/simpleRL_move_generator.cpp:167-187](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/simpleRL_move_generator.cpp#L167-L187) —— `set_step()`：把 `gamma`（目标衰减比例）与 `move_lim`（每温度步数）换算成指数加权步长 `\alpha = 1 - exp(log(gamma)/move_lim)`，注释清楚地解释了 `gamma` 的含义。

**ε-greedy 智能体**：

[vpr/src/place/move_generators/simpleRL_move_generator.cpp:226-278](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/simpleRL_move_generator.cpp#L226-L278) —— `EpsilonGreedyAgent::propose_action`：以 `rng_.frand() < epsilon_` 判定走探索还是利用。探索时在累积分布 `cumm_epsilon_action_prob_`（各动作等概率的 CDF）上二分采样；利用时 `std::max_element(q_)` 取最大 Q 的动作。最后用 `action_to_move_type_` / `action_to_blk_type_` 把动作索引翻译成 `(move_type, blk_type)`。

**softmax 智能体**：

[vpr/src/place/move_generators/simpleRL_move_generator.cpp:339-355](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec62818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/simpleRL_move_generator.cpp#L339-L355) —— `SoftmaxAgent::propose_action`：每步先 `set_action_prob_()` 重算各动作概率，再在累积概率上采样。

[vpr/src/place/move_generators/simpleRL_move_generator.cpp:376-411](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/simpleRL_move_generator.cpp#L376-L411) —— `set_action_prob_`：用 `scaled_clipped_exp`（`min(1000x, 3)`）把 Q 缩放成指数、归一化得到概率，再算累积 CDF。当智能体也选块类型时，概率还乘上各块类型的数量占比 `block_type_ratio_`。

**对比：静态聚合生成器（固定概率，不学习）**：

[vpr/src/place/move_generators/static_move_generator.cpp:49-68](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_generators/static_move_generator.cpp#L49-L68) —— `StaticMoveGenerator::propose_move`：用用户给的固定概率构造累积分布，每次生成一个随机数落到哪个区间就选哪种移动。它与 `SimpleRLMoveGenerator` 结构对称（都持有 `all_moves`），区别是概率来自**用户静态指定**而非**RL 在线学习**。

**生成器的实例化与按阶段选择**：

[vpr/src/place/RL_agent_util.cpp:8-31](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/RL_agent_util.cpp#L8-L31) —— `create_move_generators`：若 `--RL_agent_placement off`，则两个状态都用 `StaticMoveGenerator`；否则构造两个 `SimpleRLMoveGenerator`（分别对应退火的第一、第二状态）。

[vpr/src/place/RL_agent_util.cpp:51-65](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/RL_agent_util.cpp#L51-L65) —— 两个状态的可用移动集不同：**第一状态**只有 `{UNIFORM, MEDIAN, CENTROID}`（时序驱动加 `W_CENTROID`）；**第二状态**再加上 `{W_MEDIAN, CRIT_UNIFORM, FEASIBLE_REGION}` 等时序导向移动。所以 `CriticalUniformMoveGenerator` 只在第二状态（退火后期/淬火）才被 RL 调度——这正解释了 4.2 里「critical_uniform 是否被 simpleRL 调度取决于阶段」的注解。

[vpr/src/place/RL_agent_util.cpp:161-177](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/RL_agent_util.cpp#L161-L177) —— `select_move_generator`：按 `e_agent_state`（EARLY / LATE）和是否在淬火（quench）从两个生成器里选一个。

[vpr/src/place/annealer.cpp:1000-1008](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L1000-L1008) —— 状态切换条件：退火温度衰减因子 `alpha` 落在 `(0.6, 0.85)` 时，把智能体从 `EARLY` 切到 `LATE`，激活第二状态。

**相关命令行选项默认值**（供实践参考，来自 `read_options.cpp`）：

[vpr/src/base/read_options.cpp:2690-2724](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L2690-L2724) —— `--RL_agent_placement`（默认 on）、`--place_agent_multistate`（默认 on）、`--place_agent_epsilon`（默认 0.3）、`--place_agent_gamma`（默认 0.05）、`--place_reward_fun`（默认 WLbiased_runtime_aware）。

[vpr/src/base/read_options.cpp:2790-2796](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L2790-L2796) —— `--place_agent_algorithm`（默认 softmax，可选 e_greedy）、`--place_agent_space`（默认 move_block_type，即同时学习块类型）。

#### 4.3.4 代码实践

**实践目标**：理解 RL 移动如何被开关与配置，并观察 Q 表学习的过程。

**操作步骤（源码阅读型，无需编译）**：

1. 在 `RL_agent_util.cpp:8` 的 `create_move_generators` 里，找到「RL 关闭时走 StaticMoveGenerator、RL 开启时走 SimpleRLMoveGenerator」的分支判断（第 18 行 `if (!placer_opts.RL_agent_placement)`）。
2. 跟踪一次 `SimpleRLMoveGenerator` 的构造：`RL_agent_util.cpp:93` 构造它，同时构造一个 `EpsilonGreedyAgent` 或 `SoftmaxAgent`（第 79 / 121 行），并 `set_step(gamma, move_lim)`（第 92 行）。
3. 在 `simpleRL_move_generator.cpp:120` 的 `KArmedBanditAgent::process_outcome` 里，对照公式核对 Q 更新：`q_[last_action_] += step * (reward - q_[last_action_])`。

**可选运行型实践（待本地验证）**：构建 VPR 后，对比关闭与开启 RL 的布局结果。例如（具体电路/架构文件按 u1-l4 取自 `vtr_flow`）：

```shell
# 关闭 RL，退化为静态概率
./build/vpr/vpr <arch.xml> <circuit.blif> --RL_agent_placement off
# 开启 RL 并用 ε-greedy，ε=0.2
./build/vpr/vpr <arch.xml> <circuit.blif> --place_agent_algorithm e_greedy --place_agent_epsilon 0.2
```

**需要观察的现象**：

- 关闭 RL 时，日志会打印 `Using static probabilities for choosing each move type` 及各移动的固定概率。
- 开启 RL 时，日志会打印 `Using simple RL 'Softmax agent' for choosing move and block types`（或 EpsilonGreedyAgent）；退火后期会出现 `Agent's 2nd state:` 表示切换到第二状态。

**预期结果**：开启 RL 后，退火日志多出智能体类型与状态切换信息；最终布局的代价（cost）通常应优于或接近静态概率（RL 的设计目标）。QoR 对比需多次运行取均值（退火有随机性）。

**待本地验证**：本机是否已构建 VPR、能否找到合适的电路与架构文件，需你按 u1-l2/u1-l4 自行确认；上述命令仅为示例。

> 想观察 Q 表逐行变化？`simpleRL_move_generator.cpp:146` 的 `write_agent_info` 会把每步的 `last_action`、reward、全部 Q 值与各臂被拉次数写进文件——但默认 `agent_info_file_` 是空指针（第 218 / 325 行被注释），需手动取消注释并重新编译才能生成 `agent_info.txt`。这属于「修改源码」范畴，仅建议在本地实验副本上进行。

#### 4.3.5 小练习与答案

**练习 1**：`SimpleRLMoveGenerator` 与 `StaticMoveGenerator` 都是聚合型生成器，结构高度相似，本质区别是什么？

> 参考答案：`StaticMoveGenerator` 用**用户静态指定**的固定概率轮询各种移动，整个退火过程概率不变；`SimpleRLMoveGenerator` 用一个 k-臂赌徒 RL 智能体，**根据每次移动的奖励在线学习并更新**各移动的选中概率。前者无反馈闭环（`process_outcome` 空），后者有。

**练习 2**：为什么退火分两个状态（EARLY / LATE），且第二状态才包含 `CRIT_UNIFORM`、`FEASIBLE_REGION` 等时序导向移动？

> 参考答案：退火早期温度高、布局还很乱，时序关键度估计噪声大，这时主要靠 `UNIFORM/MEDIAN/CENTROID` 做全局粗调；后期温度低、布局接近收敛，时序信息更可靠，才引入 `CRIT_UNIFORM/FEASIBLE_REGION` 等精准的时序导向移动做精修。`annealer.cpp:1003` 用 `alpha ∈ (0.6, 0.85)` 作为切换时机。

**练习 3**：softmax 智能体里 `scaled_clipped_exp` 为什么要把指数截断在 3？

> 参考答案：Q 值之间微小的差会被指数放大成悬殊的概率比（`exp(1000x)` 放大 1000 倍），导致概率塌缩到单一动作、丧失探索。截断在 `min(1000x, 3)` 既让较好的动作获得更高概率，又保证其他动作不会被压到接近零，维持合理的探索度。

---

## 5. 综合实践

设计一个贯穿本讲的小任务：**用源码阅读还原「一次 RL 退火步」的完整调用链，并画出概率如何被学习改变**。

1. **起点**：`annealer.cpp` 的 `try_swap_`（第 506 行附近）调用 `move_generator.propose_move`（第 562 行）。
2. **进入 RL 聚合**：假设当前是第二状态、`SimpleRLMoveGenerator`。跟踪 `simpleRL_move_generator.cpp:26-27`：`agent.propose_action()` 选出（比如）`CRIT_UNIFORM`，再委托 `all_moves[CRIT_UNIFORM]->propose_move`。
3. **进入叶子**：跟踪 `critical_uniform_move_generator.cpp:31-58`：`propose_block_to_move(highly_crit_block=true)` 挑关键块 → `find_to_loc_uniform` 选址 → `create_move` 登记受影响块。
4. **回到退火器**：退火器算 ΔC、裁定 ACCEPTED/REJECTED。
5. **反馈学习**：`annealer.cpp:815` 调用 `calculate_reward_and_process_outcome` → `move_generator.cpp:20` 按奖励函数算 reward → `simpleRL_move_generator.cpp:31` 转发给 `agent.process_outcome` → `simpleRL_move_generator.cpp:138-141` 更新 `q_[CRIT_UNIFORM]`。

**产出**：画一张时序图，标注每一步发生在哪个文件、哪个函数，以及数据（`proposed_action`、`reward`、`q_`）如何在退火器、聚合生成器、叶子生成器、RL 智能体之间流动。

**进阶思考**：如果要把一种全新的移动类型（例如「只搬 NoC 路由器附近的块」）接入 RL 调度，需要改哪几处？（提示：`e_move_type` 加枚举、`SimpleRLMoveGenerator` 构造函数 `all_moves` 加实例化、`RL_agent_util.cpp` 第一/二状态的可用移动列表加入新类型。）

---

## 6. 本讲小结

- 移动生成器是退火循环里「**每一步如何扰动布局**」的策略对象，通过 `propose_move()` 提议、`process_outcome()` 接收反馈，由退火器按 Metropolis 准则裁定是否采纳。
- 它们分为两层：**叶子型**（Uniform / Median / Centroid / WeightedMedian / WeightedCentroid / CriticalUniform / FeasibleRegion / Manual）真正挑块选址；**聚合型**（Static / SimpleRL）持有多个叶子、决定本次用哪一种。
- 叶子移动的差异集中在两点——「挑谁」（随机 vs 关键块 vs 指定块类型）与「搬到哪」（均匀撒 vs 中位数 vs 质心 vs 可行域）；加权版把权重取为时序关键度，融合线长与时序目标。
- `SimpleRLMoveGenerator` 用 **k-臂赌徒 RL**（ε-greedy 或 softmax）在线学习各移动的 Q 值，按 `Q(a) ← Q(a) + α(reward − Q(a))` 增量更新，是 VPR 布局「自学扰动策略」的核心机制。
- 退火分两个状态：早期用粗调移动（Uniform/Median/Centroid），当温度衰减因子 `alpha ∈ (0.6,0.85)` 时切换到含 `CriticalUniform/FeasibleRegion` 等时序精修移动的第二状态（受 `--place_agent_multistate` 控制）。
- 关键澄清：`simpleRL`（调度员）与 `critical_uniform`（被调度的具体兵种）不在同一抽象层，前者会在第二状态调用后者。

---

## 7. 下一步学习建议

1. **u5-l3（布局代价与延迟模型）**：本讲多次出现的 `rlim`、`compressed grid`、增量代价计算，以及 `calculate_reward_and_process_outcome` 里 reward 所依赖的 `ΔC` 如何被高效算出，都由下一讲系统讲解。这是理解「为什么反馈能驱动 RL 学习」的钥匙。
2. **回看 u5-l1**：带着本讲对移动生成器的理解重读退火框架，你会更清楚 `PlacementAnnealer::try_swap_` 的完整四步闭环，以及退火调度（`t_annealing_state`）如何与移动生成器的两状态切换对齐。
3. **延伸阅读**：源码注释多次引用 *RLPlace*（Elgammal et al., IEEE TCAD 2021）与 *Learn to Place*（ICFPT 2020），想深入 RL 布局与定向移动的算法原理，可对照这两篇论文阅读 `simpleRL_move_generator.cpp` 与各加权移动的实现。
