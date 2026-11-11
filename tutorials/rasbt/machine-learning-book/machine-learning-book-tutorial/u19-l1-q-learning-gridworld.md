# GridWorld 环境与表格型 Q-learning

## 1. 本讲目标

本讲是强化学习单元的第一讲，也是全书代码的收官主题。读完本讲，你应该能够：

1. 理解强化学习与监督学习的根本区别：没有现成标签，只有环境给出的延迟奖励。
2. 读懂 `GridWorldEnv` 如何用一张**转移概率表 P** 完整定义一个网格世界的环境动力学（状态、动作、奖励、终止条件）。
3. 读懂 `Agent` 的两个核心方法：`choose_action` 的 ε-greedy 探索-利用权衡（含平局随机打破技巧），以及 `_learn` 中表格型 Q-learning 的 TD 更新规则。
4. 读懂 `run_qlearning` 的训练主循环，并能解读 `plot_learning_history` 画出的学习曲线（步数下降、终局奖励从 -1 向 +1 迁移）。
5. 通过调参实验（ε、γ、episode 数）体会探索率与折扣因子对收敛速度和最终回报的影响。

---

## 2. 前置知识

### 2.1 从监督学习到强化学习

前面 18 章的所有模型——从感知机到 GNN——都在做同一件事：给定样本 \( (x, y) \)，让模型的预测尽量接近标签 \( y \)。强化学习（Reinforcement Learning, RL）换了一个设定：

- **没有标签**。环境只给出标量的**奖励**（reward），而且往往延迟到达——在网格世界里，智能体要走完一整条路径后才知道结果是 +1 还是 -1。
- **数据是自己挣来的**。训练数据不是静态数据集，而是智能体（agent）与环境（environment）反复交互产生的经历。
- **动作会影响未来**。当前的动作决定了之后会遇到哪些状态，所以学习目标不是"单步预测正确"，而是"长期累积回报最大"。

两者的循环骨架其实相通：仍是「采样 → 计算更新目标 → 更新参数」，只是更新目标从标签误差变成了下文的 TD 目标。

### 2.2 马尔可夫决策过程（MDP）与回报

RL 的标准数学模型是马尔可夫决策过程，由五元组 \( (S, A, P, R, \gamma) \) 描述：

- \( S \)：状态集合（本讲是 5×6 网格的 30 个格子）。
- \( A \)：动作集合（上、右、下、左共 4 个）。
- \( P(s' \mid s, a) \)：转移概率——在状态 s 执行动作 a 后到达 s' 的概率。本讲的环境是确定性的，每条转移概率都是 1。
- \( R \)：奖励函数。本讲只有走进终点格才有 ±1，其余转移奖励为 0。
- \( \gamma \in [0,1] \)：**折扣因子**，决定未来奖励的分量。

智能体每一步交互产生 \( (s_t, a_t, r_{t+1}, s_{t+1}) \)。**回报**（return）是从时刻 \( t \) 起的累计折扣奖励：

\[
G_t = r_{t+1} + \gamma r_{t+2} + \gamma^2 r_{t+3} + \cdots
\]

γ 越小，越"短视"：k 步之后的奖励被乘上 \( \gamma^k \) 剧烈缩水。本讲的网格世界从起点到金子最少要 8 步，若 γ=0.5，走到金子的回报只有 \( 0.5^7 \approx 0.0078 \)——学习信号会变得非常微弱。

**策略**（policy）π 是从状态到动作的映射；**动作价值函数** \( Q(s,a) \) 表示"在状态 s 执行动作 a、之后按某策略走下去"的期望回报。RL 的目标就是找到最优的 \( Q^* \)，对应的贪婪策略 \( \arg\max_a Q^*(s,a) \) 即最优策略。

### 2.3 Q-learning 更新规则

Q-learning 是**时序差分**（TD）学习的一种：不等整条轨迹结束，每走一步就用"当前奖励 + 下一状态的最大 Q 值"作为目标来修正当前 Q 值：

\[
Q(s_t, a_t) \leftarrow Q(s_t, a_t) + \alpha \left[ \underbrace{r_{t+1} + \gamma \max_{a} Q(s_{t+1}, a)}_{\text{TD 目标 } q_{target}} - Q(s_t, a_t) \right]
\]

- \( \alpha \) 是学习率（本讲 0.01）；
- 若 \( s_{t+1} \) 是终止状态，没有"未来"，TD 目标退化为 \( q_{target} = r_{t+1} \)；
- 方括号里的差值叫 TD 误差。奖励就这样从终点一格一格向后传播：第一次走进金子，只有金子旁边那个 \( Q(s,a) \) 被抬高一点；重复足够多次，高值才能沿路径"渗透"回起点。

Q-learning 是**离策略**（off-policy）算法：用来与环境交互的行为策略是 ε-greedy（下面讲），但更新目标里的 \( \max_a \) 直接逼近**贪婪**最优策略。对照算法 SARSA 则用"实际执行的下一个动作"更新，属于在策略（on-policy）。

### 2.4 OpenAI Gym 与 DiscreteEnv

OpenAI Gym 是 RL 的标准环境库，规定了统一的接口：`env.reset()` 回到初始状态，`env.step(action)` 返回 `(next_state, reward, done, info)`。本仓库的 notebook 用 `check_packages` 检查 `gym >= 0.20.0`（见 [ch19/ch19.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/ch19.ipynb) 开头，运行输出为 `[OK] gym 0.21.0`）。注意代码用的是 **gym 0.20/0.21 旧 API**：`reset()` 直接返回状态、`step()` 返回四元组；新版 gymnasium 返回 `(state, info)` 和五元组，与本书代码不兼容。

其中 `gym.envs.toy_text.discrete.DiscreteEnv` 是"离散状态、离散动作"环境的基类，构造它只需提供四样东西：`nS`（状态数）、`nA`（动作数）、转移表 `P` 和初始状态分布 `isd`。**转移表 P 是本讲环境文件的核心**，格式为：

```text
P[s][a] = [(概率, 下一状态, 奖励, 是否终止), ...]
```

提供 P 之后，`reset`/`step` 全部由基类代劳——这正是 `GridWorldEnv` 只需 60 行就能定义一个完整环境的原因。

---

## 3. 本讲源码地图

| 文件 | 作用 | 关键位置 |
|---|---|---|
| [ch19/gridworld/gridworld_env.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/gridworld/gridworld_env.py) | 定义网格世界环境：动作语义、状态编码、终点/陷阱布局、转移表 P、图形渲染 | `GridWorldEnv.__init__` 构建转移表；`render` 可视化 |
| [ch19/gridworld/agent.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/gridworld/agent.py) | 定义学习智能体：Q 表、ε-greedy 选动作、Q-learning 更新、ε 衰减 | `choose_action`、`_learn`、`_adjust_epsilon` |
| [ch19/gridworld/qlearning.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/gridworld/qlearning.py) | 训练入口：把环境与智能体组装成 episode 循环，并绘制学习曲线 | `run_qlearning`、`plot_learning_history` |
| [ch19/ch19.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/ch19.ipynb) | 同一份代码的 notebook 形式（分三个单元格），另含理论推导与书图 | `run_qlearning` 单元格 |
| [ch19/figures/19_09.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/figures/19_09.png) | 书中 50 个 episode 的学习曲线（步数 + 终局奖励） | 结果参照 |

运行方式提示：`qlearning.py` 以 `from gridworld_env import GridWorldEnv` 平铺导入，须在 `ch19/gridworld/` 目录下执行 `python qlearning.py`。仓库只提供 `.py` 脚本与 notebook，没有单独的测试文件。

---

## 4. 核心概念与源码讲解

### 4.1 GridWorldEnv：用转移表定义环境动力学

#### 4.1.1 概念说明

网格世界（GridWorld）是 RL 教材的经典玩具环境：一个 5 行 6 列的格子世界，智能体从左上角出发，每步可向上/右/下/左移动一格；世界里埋着一个"金子"格（进入 +1）和三个"陷阱"格（进入 -1），进入任意一个即回合（episode）结束。它的全部"物理规则"——包括撞墙会怎样、奖励怎么发、何时终止——都预先写进一张转移表 P，交给 gym 的 `DiscreteEnv` 基类。这种"查表即环境"的方式让环境动力学完全透明、可手工验证，是理解 MDP 概念 \( P \) 与 \( R \) 的最佳载体。

#### 4.1.2 核心流程

`GridWorldEnv.__init__` 的构建流程：

1. 用四个 lambda 定义动作语义（撞边界则原地不动）；
2. 建立格子坐标 `(row, col)` 与整数状态号 `s` 的双向映射：\( s = row × num\_cols + col \)；
3. 摆放金子与陷阱，得到终止状态列表 `terminal_states`；
4. 三重循环（每个状态 × 每个动作）查表填 `P[s][a]`：
   - 下一状态 = 动作函数作用于当前坐标；
   - 奖励 = 进入金子 +1、进入陷阱 -1、其余 0；
   - 终止状态的出边改为自环且 `done=True`；
5. 初始状态分布 `isd` 把全部概率放在状态 0（左上角）；
6. 调用 `super().__init__(nS, nA, P, isd)` 交给 gym，再构建图形显示。

#### 4.1.3 源码精读

**动作定义与边界钳位**——四个 lambda 用 `max`/`min` 把移动限制在网格内，撞墙等于原地不动（奖励 0）：

[gridworld_env.py:69-75](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/gridworld/gridworld_env.py#L69-L75) 把动作编号 0/1/2/3 映射到上/右/下/左：

```python
move_up    = lambda row, col: (max(row - 1, 0), col)
move_down  = lambda row, col: (min(row + 1, num_rows - 1), col)
move_left  = lambda row, col: (row, max(col - 1, 0))
move_right = lambda row, col: (row, min(col + 1, num_cols - 1))
self.action_defs = {0: move_up, 1: move_right, 2: move_down, 3: move_left}
```

**状态编码**——[gridworld_env.py:78-83](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/gridworld/gridworld_env.py#L78-L83) 先算 `nS = num_cols * num_rows`，再建两个字典完成坐标 ↔ 状态号的互查，编码规则是行优先：`s // num_cols` 为行、`s % num_cols` 为列。

**终点布局**——[gridworld_env.py:86-96](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/gridworld/gridworld_env.py#L86-L96)：金子在 `(num_rows//2, num_cols-2)`，三个陷阱分别在其下、左、上三格。对 `qlearning.py` 主程序用的 5×6 网格，布局与状态号如下（s = 行×6+列）：

```text
列→    0     1     2     3      4      5
行0   S(0)   .     .     .      .      .
行1    .     .     .     .    陷阱(10)  .
行2    .     .     .  陷阱(15) 金子(16) .
行3    .     .     .     .    陷阱(22)  .
行4    .     .     .     .      .      .
```

注意金子左、上、下三个方向的邻格全是陷阱，**只能从右边的 (2,5) 绕进去**——所以从起点 (0,0) 出发的最短路径是"右 5 格、下 2 格、左 1 格"共 **8 步**。`terminal_states = [16, 22, 15, 10]`（金子在前），构造时 [gridworld_env.py:97](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/gridworld/gridworld_env.py#L97) 会直接把它打印出来。

**转移表构建（本文件的心脏）**——[gridworld_env.py:100-119](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/gridworld/gridworld_env.py#L100-L119)：

```python
P = defaultdict(dict)
for s in range(nS):
    row, col = self.state2grid_dict[s]
    P[s] = defaultdict(list)
    for a in range(nA):
        action = self.action_defs[a]
        next_s = self.grid2state_dict[action(row, col)]
        if self.is_terminal(next_s):
            r = (1.0 if next_s == self.terminal_states[0] else -1.0)
        else:
            r = 0.0
        if self.is_terminal(s):
            done = True
            next_s = s
        else:
            done = False
        P[s][a] = [(1.0, next_s, r, done)]
```

三个要点：

1. `P[s][a] = [(1.0, next_s, r, done)]`——单元素、概率 1.0，即确定性环境；gym 的 `DiscreteEnv.step` 会按概率从这个列表里抽样并返回 `(next_s, r, done)`。
2. 奖励挂在"**进入**终止格"的转移上（`next_s` 是终止格才有 ±1），普通转移一律 0。
3. 终止状态自身的出边被改写为自环（`next_s = s, done=True`）。细读会发现一个小瑕疵：奖励 `r` 在改写**之前**已按原 `next_s` 算好，于是"陷阱格朝金子方向"的出边会记成 `(1.0, 陷阱, +1.0, True)`。由于训练循环一遇到 `done` 立即 `reset`，这些出边永远不会被走到，对学习无影响——但这个细节说明逐行精读的价值。

**初始分布与基类交接**——[gridworld_env.py:121-125](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/gridworld/gridworld_env.py#L121-L125)：`isd` 是长度 nS 的向量，`isd[0] = 1.0` 把回合起点钉死在左上角；随后 `super().__init__(nS, nA, P, isd)` 把 P 与 isd 交给 `DiscreteEnv`，从此 `env.reset()`/`env.step()` 全部由 gym 按 P 驱动。

**渲染（可选部分）**——[gridworld_env.py:133-194](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/gridworld/gridworld_env.py#L133-L194) 的 `_build_display` 用 gym 的 `rendering` 模块画边框、网格线、陷阱（黑圆）、金子（黄三角）和智能体（蓝方块）；[gridworld_env.py:196-209](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/gridworld/gridworld_env.py#L196-L209) 的 `render` 按 `self.s` 平移智能体并 `time.sleep(delay)`。注意两点：`__init__` 末尾（[L128](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/gridworld/gridworld_env.py#L128)）就会调用 `_build_display`，即**构造环境就需要图形显示**；文件末尾 [gridworld_env.py:217-231](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/gridworld/gridworld_env.py#L217-L231) 还有一个"随机动作试玩"的 `__main__` 演示，可作为环境冒烟测试。

#### 4.1.4 代码实践：手工验证转移表

1. **实践目标**：不依赖运行，仅凭状态编码规则手算若干 `P` 条目，再与代码对照，确认你真的读懂了环境动力学。
2. **操作步骤**：
   - 按 \( s = row × 6 + col \) 手算：状态 0（左上角）执行动作 1（右）；状态 5（右上角）执行动作 1；状态 17（即 (2,5)）执行动作 3（左）。
   - 写出每条的 `(next_s, r, done)` 三元组。
   - （可选，需本地带显示环境）进入 `ch19/gridworld/` 运行 `python gridworld_env.py`，观察随机游走的图形界面与每步打印的 `env.step` 返回值。
3. **需要观察的现象**：手算结果与下面参考答案一致；运行 `python gridworld_env.py` 时开头会打印 `[16, 22, 15, 10]`。
4. **预期结果**（按代码推导）：
   - `P[0][1] = [(1.0, 1, 0.0, False)]`——右移到 (0,1)；
   - `P[5][1] = [(1.0, 5, 0.0, False)]`——右边界钳位，原地不动、奖励 0；
   - `P[17][3] = [(1.0, 16, 1.0, True)]`——从 (2,5) 左移进入金子：唯一能拿 +1 的入口。
5. 图形界面的运行效果**待本地验证**（需要桌面环境或 Xvfb 虚拟显示；无显示的服务器上连构造环境都会失败，见 4.3.4 的无头运行方案）。

#### 4.1.5 小练习与答案

**练习 1**：把网格改成 4×6（`GridWorldEnv` 的默认参数），金子和陷阱的状态号分别是多少？
**答案**：金子 `(4//2, 6-2) = (2,4)` → s = 16；陷阱 (3,4)→22、(2,3)→15、(1,4)→10。与 5×6 完全相同，因为这些格子的行号都小于 4，且编码只依赖列数 6。

**练习 2**：为什么奖励要挂在"进入终止格"的转移上，而不是"身处终止格"的状态上？
**答案**：本环境是回合制（episodic），进入终止格后回合立即结束、不再有后续转移。若把奖励放在状态上，终止格本身没有出边可拿奖励（出边是自环），奖励永远发不出去；放在转移上，"最后一步动作"才能得到明确的反馈信号——这正是 RL 中"奖励属于转移 \( (s, a, s') \)"的通用约定。

**练习 3**：从起点到金子的最短路径是多少步？为什么？
**答案**：8 步。金子的左、上、下邻格全是陷阱，只能从右侧的 (2,5) 进入；从 (0,0) 到 (2,5) 的曼哈顿距离是 2+5=7 步，再加 1 步左移进金子，共 8 步。这个数字是后文学习曲线的收敛下限。

---

### 4.2 Agent：Q 表、ε-greedy 与 TD 更新

#### 4.2.1 概念说明

`Agent` 类是表格型 Q-learning 的全部：一张以状态为键、以 4 维动作价值向量为值的 **Q 表**，加两个方法——`choose_action` 决定"怎么走"（行为策略），`_learn` 决定"怎么学"（更新规则）。

**探索-利用困境**是 RL 特有的问题：始终选当前 Q 值最大的动作（利用），可能锁死在一条偶然发现的次优路径上；始终乱走（探索），又永远学不到东西。ε-greedy 是最简单的折中——以概率 ε 随机选动作，以概率 1-ε 选贪婪动作；并让 ε 随训练逐步衰减，实现"先广撒网、后精收割"。

#### 4.2.2 核心流程

```text
choose_action(state):
    若 uniform(0,1) < ε:        # 探索
        从 4 个动作中均匀随机选一个
    否则:                       # 利用
        取 Q[state]（4 维向量）
        随机打乱动作顺序后取 argmax   # 平局随机打破
        返回对应的动作编号

_learn(transition):              # transition = (s, a, r, next_s, done)
    q_target = r                                若 done（回合结束，无未来）
             = r + γ · max_a Q[next_s][a]       否则
    Q[s][a] += α · (q_target − Q[s][a])         # 向 TD 目标挪一小步
    ε ← max(ε_min, ε × 0.95)                    # 每步衰减探索率
```

#### 4.2.3 源码精读

**构造函数与 Q 表**——[agent.py:21-36](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/gridworld/agent.py#L21-L36)：

```python
def __init__(self, env,
             learning_rate=0.01,
             discount_factor=0.9,
             epsilon_greedy=0.9,
             epsilon_min=0.1,
             epsilon_decay=0.95):
    ...
    self.q_table = defaultdict(lambda: np.zeros(self.env.nA))
```

五个超参数的含义与默认值：

| 参数 | 含义 | 默认值 |
|---|---|---|
| `learning_rate` (α) | 每次向 TD 目标修正的步长 | 0.01 |
| `discount_factor` (γ) | 折扣因子 | 0.9 |
| `epsilon_greedy` (ε) | 初始探索率 | **0.9**（90% 随机！） |
| `epsilon_min` | ε 的下限 | 0.1 |
| `epsilon_decay` | ε 的每步乘法衰减率 | 0.95 |

`q_table` 用 `defaultdict(lambda: np.zeros(nA))` 实现"**懒初始化**"：没见过的状态第一次被访问时自动得到全 0 向量，无需预分配 30×4 的矩阵——这使该 Agent 原则上也能直接用于状态数未知或很大的环境。

**ε-greedy 与平局随机打破**——[agent.py:38-47](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/gridworld/agent.py#L38-L47)：

```python
def choose_action(self, state):
    if np.random.uniform() < self.epsilon:
        action = np.random.choice(self.env.nA)
    else:
        q_vals = self.q_table[state]
        perm_actions = np.random.permutation(self.env.nA)
        q_vals = [q_vals[a] for a in perm_actions]
        perm_q_argmax = np.argmax(q_vals)
        action = perm_actions[perm_q_argmax]
    return action
```

贪婪分支里的三行"排列-取 argmax"不是炫技：训练早期 Q 表大量为全 0，`np.argmax` 遇到并列最大值时**固定返回最前面的动作编号**，会让"利用"分支退化成永远选动作 0。先随机打乱动作顺序再取 argmax，就把平局打破成了均匀随机。于是全 0 阶段，贪婪分支本身也近似均匀探索——这是初学时容易忽略的巧思。

**Q-learning 更新**——[agent.py:49-61](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/gridworld/agent.py#L49-L61)：

```python
def _learn(self, transition):
    s, a, r, next_s, done = transition
    q_val = self.q_table[s][a]
    if done:
        q_target = r
    else:
        q_target = r + self.gamma * np.max(self.q_table[next_s])
    self.q_table[s][a] += self.lr * (q_target - q_val)
    self._adjust_epsilon()
```

与 2.3 节的公式逐项对应：`done` 时 TD 目标退化为即时奖励（终止状态没有未来）；`np.max(self.q_table[next_s])` 正是离策略的贪婪目标，它与行为策略 ε-greedy **不必一致**——这就是"Q-learning 学的是最优策略、用 explore 策略收集数据"。注意 `next_s` 若从未见过，`defaultdict` 会现场生成全 0 向量，`np.max` 返回 0，公式依然成立。

**ε 衰减**——[agent.py:63-65](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/gridworld/agent.py#L63-L65)：

```python
def _adjust_epsilon(self):
    if self.epsilon > self.epsilon_min:
        self.epsilon *= self.epsilon_decay
```

关键细节：`_adjust_epsilon` 在**每次 `_learn`（即每个环境步）**后调用，而不是每个 episode。从 0.9 衰减到 0.1 需要 \( n \) 步满足 \( 0.9 × 0.95^n \le 0.1 \)，即 \( n \ge \ln 9 / \ln(1/0.95) \approx 43 \) 步——头一两个随机游走的 episode 就足以把 ε 打到下限，之后训练实际是在 ε=0.1 下进行的。

#### 4.2.4 代码实践：单步更新的手算验证

1. **实践目标**：不通跑训练，用手算验证 `choose_action` 的随机比例与 `_learn` 的更新幅度，确认对 α 与 ε 机制的理解。
2. **操作步骤**：
   - 手算：全新 Agent 的某状态 `s` 执行动作 `a` 后走进金子（`r=1.0, done=True`），更新后 `Q[s][a]` 是多少？
   - 再手算：若 `done=False, r=0` 且 `Q[next_s]` 全 0，更新后 `Q[s][a]` 是多少？
   - （可选，需本地环境）把 `qlearning.py`、`agent.py`、`gridworld_env.py` 复制到自己的实验目录，注释掉 `__init__` 里的 `self._build_display(...)` 与训练循环里的 `env.render(...)`（得到无头可运行版本，仓库源码不动），然后：

     ```python
     import numpy as np
     np.random.seed(0)
     env = GridWorldEnv(5, 6)
     agent = Agent(env, epsilon_greedy=0.3)
     agent._learn((17, 3, 1.0, 16, True))   # 从 (2,5) 左移进金子
     print(agent.q_table[17])               # 期望只有第 3 个元素非 0
     print(agent.epsilon)                    # 0.3*0.95 = 0.285
     ```

3. **需要观察的现象**：`q_table[17]` 形如 `[0. 0. 0. 0.01 0.]`；ε 变为 0.285（一步衰减）。
4. **预期结果**：由公式 \( Q \leftarrow 0 + 0.01 × (1 − 0) = 0.01 \) 推得；第二个场景 \( q_{target} = 0 + 0.9 × 0 = 0 \)，Q 保持 0——**只有碰到非零奖励的转移，Q 值才会动**。另可多次调用 `agent.choose_action(0)` 统计随机动作占比是否接近 30%。
5. 本地数值输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么初始 `epsilon_greedy=0.9` 这么大？若一开始就设 0.0 会怎样？
**答案**：Q 表初始全 0，智能体对环境一无所知，必须先大量探索才能"发现"金子在哪。ε=0 意味着完全贪婪，而全 0 表的贪婪分支经平局打破虽近似均匀随机，可一旦某条路径的 Q 值偶然被抬高，agent 会立刻锁死在这条（可能绕远甚至通往陷阱的）路径上，再也看不到反例——缺乏探索的值迭代无法保证收敛到最优。

**练习 2**：ε 的衰减为什么按"每步"而不是"每 episode"？
**答案**：一个 episode 的步数本身在变化（随机游走时几十步，学会之后 8 步）。按步衰减让探索率的退火与"见过的转移数量"挂钩，节奏更可控；按 episode 衰减则在早期长 episode 里保持了过久的纯随机。本环境里 43 步就到达下限，两者差别不大，但这是实现时需要自觉选择的粒度。

**练习 3**：`_learn` 中若去掉 `if done` 分支、统一用 `r + γ·max Q[next_s]`，会发生什么？
**答案**：终止状态的 `next_s` 的 Q 值（≈0）会被继续折入目标，终局奖励 ±1 被稀释为 \( r + γ × 0 \)——本环境中恰好仍是 r，因为终止状态的 Q 恒为 0（其出边自环且从不被训练）。但在有"终止状态自身也有价值"或自环奖励的环境里，这个分支是必要的；显式写出 `q_target = r` 是把"回合结束即无未来"这一语义表达清楚的正确写法。

---

### 4.3 run_qlearning：训练主循环与 final_reward 陷阱

#### 4.3.1 概念说明

`qlearning.py` 把环境与智能体组装成完整的训练程序。RL 的训练单位是 **episode（回合）**：从 `reset()` 出发，agent 反复"选动作 → 环境转移 → 学习更新"，直到 `done` 为真；如此循环 `num_episodes` 个回合。与监督学习不同，这里**采样与学习交替进行**：每走一步就立刻用这条转移更新 Q 表（单步在线更新），而非攒完一个 batch 再学。

#### 4.3.2 核心流程

```text
run_qlearning(agent, env, num_episodes=50):
    history = []
    对每个 episode:
        state = env.reset()                    # 回到左上角
        final_reward, n_moves = 0.0, 0
        循环:
            action  = agent.choose_action(state)      # ε-greedy
            next_s, reward, done, _ = env.step(action) # 查 P 表转移
            agent._learn((state, action, reward, next_s, done))
            state = next_s; n_moves += 1
            若 done: 跳出
        history.append((n_moves, final_reward))       # 记录曲线数据
    返回 history
```

#### 4.3.3 源码精读

**数据结构与随机种子**——[qlearning.py:22-25](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/gridworld/qlearning.py#L22-L25)：`np.random.seed(1)` 在导入时固定全局随机种子，使整次训练（探索、平局打破）可复现；`Transition` 用 `namedtuple` 把五元组 `(state, action, reward, next_state, done)` 变成带字段名的轻量记录，比裸元组可读。

**主循环**——[qlearning.py:28-48](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/gridworld/qlearning.py#L28-L48)：

```python
def run_qlearning(agent, env, num_episodes=50):
    history = []
    for episode in range(num_episodes):
        state = env.reset()
        env.render(mode='human')
        final_reward, n_moves = 0.0, 0
        while True:
            action = agent.choose_action(state)
            next_s, reward, done, _ = env.step(action)
            agent._learn(Transition(state, action, reward,
                                    next_s, done))
            env.render(mode='human', done=done)
            state = next_s
            n_moves += 1
            if done:
                break
            final_reward = reward
        history.append((n_moves, final_reward))
        print(f'Episode {episode}: Reward {final_reward:.2} #Moves {n_moves}')
    return history
```

逐行看：内层 `while True` 就是 2.1 节所说的 RL 交互循环——每步先 `choose_action`（行为策略），再 `env.step` 查 P 表得到转移，然后**立即** `agent._learn` 做单步更新。`history` 记录每个 episode 的 `(步数, 终局奖励)`，是学习曲线的原始数据。

**一个值得注意的陷阱：`final_reward = reward` 位于 `if done: break` 之后**（[qlearning.py:42-44](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/gridworld/qlearning.py#L42-L44)）。逐行追踪控制流：走到终止格的那一步 `done=True`，循环在赋值**之前**就 `break` 了，`final_reward` 停留在上一次迭代的值——而普通转移的奖励恒为 0，所以**按仓库当前代码，`history` 里的 final_reward 恒为 0.0**，每个 episode 都打印 `Reward 0.0`。与之对照，书中的学习曲线图 [ch19/figures/19_09.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/figures/19_09.png) 下半部分是 ±1 的阶跃线，说明出书的版本把该赋值放在了 `break` 之前。notebook 中的版本（[ch19/ch19.ipynb:797-819](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/ch19.ipynb#L797-L819)）与脚本一致，同样存在此问题，且 ERRATA 目录尚无相关条目。做实验时在本地副本中把 `final_reward = reward` 上移一行到 `if done:` 之前即可修复；`n_moves` 不受影响，始终是真实步数。

**入口**——[qlearning.py:72-78](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/gridworld/qlearning.py#L72-L78)：`GridWorldEnv(num_rows=5, num_cols=6)`（注意默认参数是 4×6，主程序显式改成 5×6）、默认超参数的 `Agent`、50 个 episode。

#### 4.3.4 代码实践：无头运行准备 + 跟踪一条调用链

1. **实践目标**：解决"服务器无显示器跑不了"的现实问题，并用日志跟踪一条 episode 的完整调用链。
2. **操作步骤**：
   - 把 `ch19/gridworld/` 下三个 `.py` 复制到自己的实验目录（不动仓库源码）。
   - 注释两处渲染：`__init__` 里的 `self._build_display(gold_cell, trap_cells)`（[gridworld_env.py:128](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/gridworld/gridworld_env.py#L128)）与 `run_qlearning` 里的两行 `env.render(...)`；同时把 `epsilon_greedy` 暂时调小（如 0.1）。
   - 在 `_learn` 内临时加一行 `print(s, a, r, next_s, done, self.q_table[s][a])`，运行 `python qlearning.py`，观察前两个 episode 的转移序列。
   - 顺手修复 4.3.3 指出的 `final_reward` 位置问题。
3. **需要观察的现象**：打印出的转移里，绝大多数 `r=0.0`，只有终止那一步出现 `r=1.0` 或 `r=-1.0` 且 `done=True`；对应的 `Q[s][a]` 从 0 变为 0.01（首次）。
4. **预期结果**：第一个 episode 步数较多（ε=0.9 时近乎随机游走）；构造环境时打印 `[16, 22, 15, 10]`。具体数值**待本地验证**。
5. 提示：保留渲染的完整运行需要约几分钟——`render` 每次调用 `time.sleep(0.05)`、终局睡 1 秒，且需要 pyglet 图形界面；这也正是无头实验先去渲染的原因。

#### 4.3.5 小练习与答案

**练习 1**：`history` 里两个量分别衡量什么？哪个更适合作"学会了"的证据？
**答案**：`n_moves` 是回合步数，衡量路径效率（下限 8 步）；`final_reward` 是终局奖励（+1 金子 / -1 陷阱），衡量结果好坏。步数低且稳定在 8 附近、奖励稳定 +1，两者合起来才是"学会了最短路径"。单看奖励有一个盲区：可能收敛到"稳定拿 +1 但绕远"的策略，此时步数能暴露问题。

**练习 2**：为什么每个 episode 都从 `env.reset()`（状态 0）重新开始？
**答案**：这是回合制任务：终止状态没有有意义的未来（出边是自环），必须回到初始状态分布 `isd` 重新采样起点。同时起点固定在 (0,0) 也保证每个 episode 都从同一条最短路径的起端开始评估，学习曲线的步数才可比。

**练习 3**：本讲的训练循环与 u14-l2 的 CNN 训练循环相比，骨架上有何异同？
**答案**：同：都是"循环内采样数据 → 计算更新目标 → 更新参数"，都靠 `np.random`/种子保证可复现。异：CNN 的数据来自预先装好的 DataLoader、目标是标签、每 epoch 完整过一遍固定数据集；Q-learning 的数据由 agent 自己与 env 现场生成（`env.step`）、目标是随学习不断变化的 TD 目标 \( r + γ\max_a Q \)（自举，bootstrapping），且逐条更新而非按批更新。数据分布随策略改变而改变，是 RL 独有的不稳定性来源。

---

### 4.4 plot_learning_history：学习曲线的解读与实验设计

#### 4.4.1 概念说明

RL 没有现成的"测试集准确率"，学习曲线是判断训练是否成功的主要手段。本书把两个指标画成上下两个子图：**步数曲线**（应从高位下降并收敛到最优步数 8 附近）与**终局奖励曲线**（应从 -1/0 混杂逐渐稳定在 +1）。理解曲线的"为什么"比看图本身重要：步数下降的前提是奖励从金子逐步反向传播回起点附近的 Q 值——这正是折扣因子 γ 与学习率 α 共同决定的动力学。

#### 4.4.2 核心流程

```text
plot_learning_history(history):
    上子图: episodes vs n_moves      → 折线 + 圆点标记（plot）
    下子图: episodes vs final_reward → 阶梯线（step）
    保存 q-learning-history.png 并 show
```

#### 4.4.3 源码精读

[qlearning.py:51-69](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/gridworld/qlearning.py#L51-L69)：

```python
def plot_learning_history(history):
    fig = plt.figure(1, figsize=(14, 10))
    ax = fig.add_subplot(2, 1, 1)
    episodes = np.arange(len(history))
    moves = np.array([h[0] for h in history])
    plt.plot(episodes, moves, lw=4, marker="o", markersize=10)
    ...
    ax = fig.add_subplot(2, 1, 2)
    rewards = np.array([h[1] for h in history])
    plt.step(episodes, rewards, lw=4)
    ...
    plt.savefig('q-learning-history.png', dpi=300)
    plt.show()
```

两个细节：从 `history` 的元组里分别解包出步数与奖励（`h[0]`、`h[1]`）；奖励只有 ±1（修复后）两种取值，用 `plt.step` 阶梯线比普通折线更能表达"离散跳变"。参照书图 [figures/19_09.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/figures/19_09.png)：上半部分步数从约 60～80 起伏下降，后半段贴近 10 左右（略高于理论最优 8，因为 ε=0.1 仍有残余探索、且偶有回合掉进陷阱重来）；下半部分终局奖励早期 +1/-1 交替，后期 +1 占主导——这就是"学会避开陷阱、走近路"的视觉证据。

#### 4.4.4 代码实践：曲线对比实验的准备工作

1. **实践目标**：搭好比图实验的脚手架——把训练函数参数化，让"改一个超参跑一次"变成一行调用。
2. **操作步骤**：在实验目录里把主程序改成：

   ```python
   def run_experiment(label, num_episodes=50, epsilon=0.9, gamma=0.9):
       np.random.seed(1)                       # 每组实验同种子，公平对比
       env = GridWorldEnv(5, 6)
       agent = Agent(env, epsilon_greedy=epsilon, discount_factor=gamma)
       history = run_qlearning(agent, env, num_episodes)
       env.close()
       plot_learning_history(history)          # 也可改为存成不同文件名
       return history
   ```

   （示例代码，非仓库原有。）先跑 `run_experiment('baseline')` 确认脚手架可用。
3. **需要观察的现象**：baseline 的步数曲线形态与书图 19_09 大体一致（下降、收敛到 10 上下）。
4. **预期结果**：受随机性的影响，具体每条曲线会有差异；整体趋势**待本地验证**。
5. 注意：若未修复 4.3.3 的 `final_reward` 位置，下子图将全为 0——这本身就是一个检验你是否读懂主循环的信号。

#### 4.4.5 小练习与答案

**练习 1**：为什么步数曲线后期仍围绕 10 波动，而不是精确钉在 8？
**答案**：两个原因：ε=0.1 的残余探索使约 10% 的步骤是随机动作，可能绕路甚至踩陷阱；且 Q 值是逐次逼近的估计值，未完全收敛时个别状态的最优动作仍在轮换。

**练习 2**：曲线横轴为什么是 episode 而不是环境步数？
**答案**：episode 是本任务的"完整尝试"单位，每个 episode 的 `(步数, 终局奖励)` 才是定义良好的绩效指标；横轴用步数则早期长随机回合会主导刻度，掩盖后期的改进。当然，若想比较"样本效率"（同样环境交互步数下谁学得快），用总步数作横轴反而更公平——这是实验设计时可以自问的好问题。

---

## 5. 综合实践：探索率、折扣因子与训练轮数的对比实验

这是本讲的主实践，目标是把 4 个模块串起来：环境 → 智能体 → 训练 → 曲线，并用控制变量法回答"ε、γ、episode 数各如何影响学习"。

**准备**：按 4.3.4 做好无头副本（去渲染、修复 `final_reward`）、按 4.4.4 准备 `run_experiment`。

**实验设计**（控制变量，一次只改一个）：

| 组别 | epsilon_greedy | discount_factor | num_episodes | 考察问题 |
|---|---|---|---|---|
| A（基线） | 0.9 | 0.9 | 50 | 参照 |
| B1 | 0.4 | 0.9 | 50 | 降低初始探索 |
| B2 | 0.1 | 0.9 | 50 | 恒定低探索 |
| C | 0.9 | **0.5** | 50 | 短视折扣 |
| D | 0.9 | 0.9 | **100** | 更长训练 |

**步骤**：

1. 依次运行五组，把每组的 `history` 存下来（列表即可），并用 `plot_learning_history` 分别出图。
2. 汇总每组的：前 10 个 episode 平均步数、后 10 个 episode 平均步数、后 10 个 episode 的 +1 比例。
3. 对比 A/B1/B2 的下降速度与最终步数；对比 A/C 的曲线形态与最终 +1 比例；对比 A/D 的后期稳定性。

**需要观察的现象与预期分析**（均为定性预期，具体数值**待本地验证**）：

- **ε 的影响**：ε 从 0.9 降到 0.4/0.1 后，早期步数下降更快（更早依赖已学到的 Q 值）；但过低的 ε 有锁死次优路径的风险——特别提醒：由于 ε 每步衰减，A 组其实约 43 步后就与 B2 同处 ε=0.1，差别主要集中在前几个 episode。若某低 ε 组最终步数明显高于 8 且 +1 比例不高，就是"探索不足收敛到次优"的证据。
- **γ 的影响**：γ=0.5 时 8 步外的 +1 被折扣为 \( 0.5^7 \approx 0.008 \)，Q 值的绝对量级与"8 步 vs 9 步"的 Q 差都被压到 10⁻³ 量级，奖励传播回起点更慢，预计需要更多 episode 才能把平均步数压下来，甚至 50 个 episode 内明显欠收敛；γ=0.9（基线）是"看得见 8 步未来"的合理取值。
- **episode 数的影响**：100 集的后期曲线应更平稳地贴住最优区间，说明 50 集时尚未完全收敛。

**预期结果**：一份五组曲线的对比图 + 一张汇总表，并能用"探索-利用权衡"与"折扣回报"两个概念写出 3～5 句结论。

---

## 6. 本讲小结

- 强化学习没有标签，只有环境返回的延迟奖励；数据由 agent 与 env 的交互（`reset`/`step` 循环）现场产生，训练目标从标签误差换成 TD 目标 \( r + γ\max_a Q(s', a) \)。
- `GridWorldEnv` 用一张转移表 `P[s][a] = [(1.0, next_s, r, done)]` 完整定义了确定性环境动力学：状态号按 \( s = row×num\_cols + col \) 编码，奖励只挂在进入金子(+1)/陷阱(-1)的转移上，终止状态出边自环，起点固定在左上角；5×6 网格的最短夺金路径为 8 步。
- `Agent` 是表格型 Q-learning 的最小实现：`defaultdict` 懒初始化的 Q 表、ε-greedy 行为策略（贪婪分支用随机排列打破 argmax 平局）、每步 \( Q \leftarrow Q + α(q_{target} − Q) \) 的离策略更新，ε 按**步**衰减（0.9→0.1 约 43 步）。
- `run_qlearning` 是"episode → 逐步交互、即时更新"的训练主循环；精读发现 `final_reward = reward` 位于 `break` 之后，导致仓库版本记录的终局奖励恒为 0（书图 19_09 的 ±1 来自修复前的版本），本地实验时应上移该赋值。
- 学习曲线用步数（效率）与终局奖励（成败）双指标判读：步数收敛到 8 附近、+1 占主导即学会最短路径；ε 控制探索-利用，γ 掘定折扣奖励的"视野"，二者共同决定收敛速度与最终回报。

## 7. 下一步学习建议

表格型 Q-learning 把 Q 值存进字典，前提是状态可枚举（这里只有 30 个格子）。一旦状态空间爆炸——比如 CartPole 的连续四维观测——表格就存不下了，必须用神经网络来**逼近** Q 函数。下一讲 [u19-l2 Deep Q-Network：CartPole 平衡实验] 将精读 `ch19/cartpole/main.py`：Q 网络结构、replay buffer 采样、`fit` 的批量目标计算与目标网络的周期同步。届时你会发现 `agent.py` 里的 `_learn` 被拆成"存经验"与"批量拟合"两步，而 ε-greedy 与 ε 衰减几乎原封不动地保留——本讲的 `Agent` 类正是 DQN 的骨架。建议先自行重读 [ch19/ch19.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/ch19.ipynb) 中 "A glance at deep Q-learning" 一节，带着"哪些被保留了、哪些被替换了"的问题进入下一讲。
