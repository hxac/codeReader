# 深度强化学习（Deep Reinforcement Learning）

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清**强化学习（Reinforcement Learning, RL）**与监督学习、无监督学习的区别，理解它"边做边学（learning by doing）"的本质。
- 掌握强化学习的标准数学模型——**马尔可夫决策过程（MDP）**：状态、动作、奖励、策略，以及折扣回报 \(G_t\)、探索-利用权衡。
- 会用 **OpenAI Gym** 的统一接口（`reset` / `step` / `render`、`action_space` / `observation_space`）操作 CartPole 环境。
- 理解两大算法家族：**价值法**（Q-learning，并延伸到概念性的 DQN）与**策略法**（Policy Gradient、Actor-Critic）。
- 用 PyTorch 动手训练一个能平衡杆子的 CartPole 智能体，并把它迁移到 MountainCar 任务。

> **关于本讲内容范围的一个重要说明（请先读）**：本课指定主源码 `CartPole-RL-PyTorch.ipynb` 实际实现的是**策略梯度（Policy Gradient）**和 **Actor-Critic** 两种**策略法**算法；本课目录里另一个遗留文件 `notebook.ipynb` 实现的是**表格型 Q-learning**。本课目录**没有**实现完整的 DQN。因此本讲会：用 `notebook.ipynb` 里真实存在的 Q 表代码讲清 Q-learning，把 DQN 作为"把 Q 表换成神经网络"的**概念性延伸**来介绍（其桥梁正是 Actor-Critic 里那个学价值函数的 Critic 网络），并明确标注哪些是源码、哪些是延伸说明，绝不编造不存在的代码。

## 2. 前置知识

在开始前，建议你先具备以下基础（本手册前面的讲义已覆盖）：

- **神经网络基础**（见 u2-l4、u2-l5）：前向传播、反向传播、损失函数、优化器（SGD/Adam）。本讲的策略网络和价值网络仍是这些套路。
- **分类任务与损失**（见 u2-l5）：softmax + 交叉熵。策略梯度会复用 `log(prob)` 这种结构。
- **概率与期望的直觉**：理解"按概率采样一个动作"是什么意思。
- **Python / NumPy**：能看懂数组索引、`np.random.choice` 等。
- 承接 u2-l1 建立的**范式坐标系**：监督学习、无监督学习、强化学习并称机器学习三大范式，RL 是其中第三个。

## 3. 本讲源码地图

本讲涉及的真实源码文件如下：

| 文件 | 作用 |
| --- | --- |
| [lessons/6-Other/22-DeepRL/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/README.md) | 第 22 课讲义正文：讲清 RL 的基本概念、OpenAI Gym、CartPole、策略梯度与 Actor-Critic，并布置 MountainCar 作业。 |
| [lessons/6-Other/22-DeepRL/CartPole-RL-PyTorch.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/CartPole-RL-PyTorch.ipynb) | **本课指定主 Notebook**：用 PyTorch 实现策略梯度 + Actor-Critic 两种算法，训练 CartPole 平衡。 |
| [lessons/6-Other/22-DeepRL/notebook.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/notebook.ipynb) | 遗留草稿 Notebook：用**表格型 Q-learning**（含状态离散化、ε-贪心、Bellman 更新）解 CartPole。本讲用它讲 Q-learning。 |
| [lessons/6-Other/22-DeepRL/lab/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/lab/README.md) | 综合作业说明：训练 MountainCar 逃出山谷。 |
| [lessons/6-Other/22-DeepRL/lab/MountainCar.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/lab/MountainCar.ipynb) | 作业 Notebook：留空让你把本课算法迁移到 MountainCar（其文字提示是"adopt Policy Gradients and Actor-Critic"）。 |

> **提示**：`.ipynb` 的永久链接行号指向该 Notebook 的 JSON 源文件行；下面每处引用都会同时标明它属于哪个代码 cell，方便你在编辑器里定位。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：**MDP 与奖励** → **Gym 环境与 CartPole** → **Q-learning、价值函数与 DQN** → **策略梯度与 Actor-Critic**。

### 4.1 MDP 与奖励：强化学习的基本框架

#### 4.1.1 概念说明

强化学习的核心思想是**"边做边学"**：没有带标注的数据集，而是让一个**智能体（agent）**在一个**环境（environment）**里不断试错，靠环境给的**奖励（reward）**信号慢慢变强。课程 README 开篇就强调这一点：

> 强化学习被视为与监督学习、无监督学习并列的基本机器学习范式之一……RL 基于"边做边学"。比如我们第一次见到一个电脑游戏时，即使不懂规则也会开始玩，很快就能靠"玩 + 调整行为"提升水平。
> —— [README.md:L3](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/README.md#L3-L3)

强化学习的标准数学模型叫**马尔可夫决策过程（Markov Decision Process, MDP）**，它由五个要素组成：

- **状态 \(s\)**：环境当前所处的情形（如杆子的位置与角度）。
- **动作 \(a\)**：智能体在某状态下可做的事（如向左、向右推车）。
- **奖励 \(r\)**：环境对该动作的即时打分。
- **转移**：执行动作后环境进入的新状态 \(s'\)（通常带随机性）。
- **策略 \(\pi\)**：智能体的决策函数，\(a=\pi(s)\)；也可写成概率形式 \(\pi(a|s)\)——在状态 \(s\) 下选动作 \(a\) 的概率。

强化学习的**目标**是找一个策略 \(\pi\)，使**长期累计奖励最大化**，而不是只盯眼前一步。这个累计量叫**回报（return）**，通常用**折扣（discount）**来弱化遥远未来的奖励：

\[
G_t = r_t + \gamma\, r_{t+1} + \gamma^2\, r_{t+2} + \cdots = \sum_{k=0}^{\infty} \gamma^k\, r_{t+k}, \qquad 0 \le \gamma \le 1
\]

其中 \(\gamma\) 是折扣因子（本课用 0.99）。它越接近 1，智能体越"有远见"。

RL 有两个让初学者容易踩坑的特点：

1. **延迟奖励**：通常到一局结束才知道输赢，单看某一步无法判断它好不好——"我们无法断定单独某一步是否走得好，只有在游戏结束时才收到奖励"（[README.md:L12](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/README.md#L12-L12)）。这跟监督学习"每条样本都有标准答案"完全不同。
2. **探索-利用权衡（exploration vs exploitation）**：每一步都要在"沿用目前已知最优策略（利用）"与"尝试未知新状态（探索）"之间平衡（[README.md:L14](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/README.md#L14-L14)）。一味利用会困在局部最优，一味探索则学不到稳定策略。

#### 4.1.2 核心流程

强化学习的运行骨架是一个**智能体-环境交互循环**：

```
观察状态 s
   │
   ▼
策略 π 选动作 a ──────► 环境执行 a
                          │
                          ▼
                  得到奖励 r、新状态 s'
                          │
   ◄─────────────────────┘
更新策略（用 r、s' 作为反馈）
回到"观察状态 s'"，循环直至 done
```

关键反馈链条是：**奖励驱动策略更新**。不同算法的区别，本质上就是"如何用奖励去更新策略"——本讲会遇到两条路线：直接更新策略分布（策略法），或先估计每个动作的长期价值再据此选动作（价值法）。

#### 4.1.3 源码精读

课程把"策略"的形式说得很清楚：它就是要训练的模型，输入状态、输出动作（或动作概率）。

> RL 算法的目标是训练一个模型——即**策略 \(\pi\)**——对给定状态返回相应动作……我们也可以把策略看成概率性的：对任意状态 \(s\) 和动作 \(a\)，它返回"在状态 \(s\) 下应采取 \(a\)"的概率 \(\pi(a|s)\)。
> —— [README.md:L56](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/README.md#L56-L56)

策略梯度算法里对折扣回报的解释，直接对应上面的 \(G_t\) 公式：

> 我们构造一个**累计奖励向量**，表示实验中每一步的总奖励，并对早期奖励做**折扣**（乘以系数 \(\gamma=0.99\)）以削弱其作用，然后强化实验路径上那些产生更大奖励的步骤。
> —— [README.md:L62](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/README.md#L62-L62)

#### 4.1.4 代码实践：用随机策略感受"延迟奖励"

1. **实践目标**：用一个完全随机的智能体跑一局 CartPole，亲眼看到"没有策略时活不了几步"，建立对奖励、episode、done 的直觉。
2. **操作步骤**：在 `ai4beg` 环境里 `pip install gym` 后，运行下面这段**示例代码**（节选自课程 README 与 PyTorch Notebook 的入门 cell，未训练任何模型）：

   ```python
   import gym
   env = gym.make("CartPole-v1")      # 见 CartPole-RL-PyTorch.ipynb 第 3 个代码 cell
   env.reset()
   done = False
   total_reward = 0
   while not done:
       obs, rew, done, info = env.step(env.action_space.sample())  # 随机动作
       total_reward += rew
   print(f"Total reward: {total_reward}")
   ```

3. **需要观察的现象**：`total_reward` 通常是个位数到二十几（杆子很快倒下）；每次运行结果不同（因为动作随机）。
4. **预期结果**：随机策略下 episode 很短——这正是"没有学到策略"的表现。本讲后面的算法目标就是把 `total_reward` 训到接近 500（CartPole-v1 的满分上限）。
5. **注意点（待本地验证）**：老版 `gym`（如 Notebook 里装的 0.25.0）的 `step` 返回 4 元组 `(obs, reward, done, info)`；若你装的是较新的 `gymnasium`，`step` 返回 5 元组（多一个 `truncated`），且 `render` 需在 `gym.make` 时传 `render_mode`。如遇参数数量不匹配，按报错调整即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么说强化学习里"单看某一步无法判断好坏"？这对训练算法意味着什么？
**参考答案**：因为奖励常常延迟到局末才给（延迟奖励），单步的即时 \(r\) 不能代表这一步的真正价值。这意味着算法必须用**累计（折扣）回报** \(G_t\) 来评价一步的好坏，而不是用即时奖励。

**练习 2**：折扣因子 \(\gamma=0\) 和 \(\gamma=1\) 分别代表什么极端策略？
**参考答案**：\(\gamma=0\) 时 \(G_t=r_t\)，智能体完全短视、只看眼前一步；\(\gamma=1\) 时未来奖励不打折，智能体无限远视（在无限长 episode 里回报可能发散，故实践中常取 0.9~0.99）。

---

### 4.2 OpenAI Gym 环境与 CartPole

#### 4.2.1 概念说明

要做强化学习实验，首先得有一个**模拟环境**——它定义"游戏规则"、能被反复运行并给出观测与奖励。本课用的是业界最流行的 **OpenAI Gym**：

> 做 RL 的一个好工具是 OpenAI Gym——一个**模拟环境**，能模拟从 Atari 游戏到平衡杆物理在内的众多环境，是训练强化学习算法最流行的模拟环境之一，由 OpenAI 维护。
> —— [README.md:L16-L18](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/README.md#L16-L18)

**CartPole（平衡杆）** 是 Gym 里最经典的入门任务：一个小车可在水平方向左右移动，车上竖着一根杆子，目标是让杆子尽量久地不倒（类似杂技艺人在手上立杆，但限定在一维）。每撑住一个时间步得 +1 分，倒下或越界则局结束，目标是让累计奖励（撑住的步数）最大化。

#### 4.2.2 核心流程

Gym 把所有环境都封装成**同一套接口**，这是它最强大的设计——学会一个环境，就会用所有环境：

1. `env = gym.make("CartPole-v1")`：创建环境。
2. `env.reset()`：开始新一局，返回初始观测。
3. `action = ...`：从**动作空间 `env.action_space`** 里选一个动作。
4. `obs, reward, done, info = env.step(action)`：执行一步，返回**观测**（来自观测空间）、**奖励**、**是否结束**、附加信息。
5. `env.render()`：渲染当前画面（可选）。
6. 循环 3–5 直到 `done` 为真。

课程对这套接口的说明：

> 每个环境都用完全相同的方式访问：`env.reset` 开始新实验；`env.step` 执行一步模拟，它接收来自**动作空间**的一个**动作**，返回来自**观测空间**的一个**观测**，以及奖励和终止标志。
> —— [README.md:L48-L50](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/README.md#L48-L50)

CartPole 的具体规格：

- **动作空间**：`Discrete(2)`，两个离散动作——0（向左推）、1（向右推）。
- **观测空间**：`Box`，4 维连续向量，分别是：小车位置、小车速度、杆子角度、杆子角速度。

#### 4.2.3 源码精读

主 Notebook 里正是这样创建环境并打印两个空间的：

[CartPole-RL-PyTorch.ipynb:L46-L46](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/CartPole-RL-PyTorch.ipynb#L46-L46)（第 3 个代码 cell）——创建 CartPole 环境：

```python
env = gym.make("CartPole-v1")
print(f"Action space: {env.action_space}")        # Discrete(2)
print(f"Observation space: {env.observation_space}")  # Box(...)
```

Notebook 的 markdown 明确解释了观测的 4 个分量含义（[CartPole-RL-PyTorch.ipynb:L84-L87](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/CartPole-RL-PyTorch.ipynb#L84-L87)）：

- Position of cart（小车位置）
- Velocity of cart（小车速度）
- Angle of pole（杆子角度）
- Rotation rate of pole（杆子角速度）

策略梯度算法里，`run_episode` 把"观测→动作→奖励"这条交互链完整跑了一遍（[CartPole-RL-PyTorch.ipynb:L141-L142](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/CartPole-RL-PyTorch.ipynb#L141-L142)）：

```python
action = np.random.choice(num_actions, p=np.squeeze(action_probs.detach().numpy()))
nstate, reward, done, info = env.step(action)
```

注意这里第一行：动作是**按策略网络输出的概率分布采样**得到的（不是 argmax），这正是策略法"概率性策略 \(\pi(a|s)\)"的体现。

#### 4.2.4 代码实践：探查动作空间与观测空间

1. **实践目标**：亲手创建 CartPole，确认动作/观测空间的形状与取值范围，为后续算法做准备。
2. **操作步骤**：运行主 Notebook 第 3 个 cell，再追加两行打印边界：

   ```python
   print(env.observation_space.low)   # 观测各维下界
   print(env.observation_space.high)  # 观测各维上界
   ```

3. **需要观察的现象**：`Action space: Discrete(2)`；`Observation space: Box(...)`，形状 `(4,)`；`low`/`high` 给出每个观测分量的取值范围（注意位置和角度有界，而速度/角速度的边界是极大值，表示实际不限制）。
4. **预期结果**：确认"输入是 4 维连续向量、输出是 2 选 1 的离散动作"。这决定了后面策略网络的输入层是 4、输出层是 2。
5. **注意点**：如果 `print` 报 `observation_space` 没有 `low`/`high`，说明你创建的环境被包装过，可用 `env.unwrapped.observation_space` 取底层属性。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Gym 的"统一接口"对 RL 研究很重要？
**参考答案**：因为算法只与抽象的"状态、动作、奖励、done"打交道，不依赖具体环境的物理细节。换一个环境（比如从 CartPole 换到 MountainCar），算法代码几乎不用改，只需把 `env.make(...)` 换掉——这正是本讲综合实践能把算法迁移到 MountainCar 的根本原因。

**练习 2**：CartPole 的观测是 4 维连续值、动作是 2 个离散值。一个能直接吃这个观测、吐动作概率的最简神经网络，输入输出维度应分别是多少？
**参考答案**：输入 4（=观测维度），输出 2（=动作数），最后接 softmax 把 2 个 logits 变成概率。

---

### 4.3 Q-learning、价值函数与 DQN

#### 4.3.1 概念说明

求解 MDP 有两条主流路线：

- **价值法（value-based）**：不直接学策略，而是学一个**价值函数**——评估"在状态 \(s\) 做动作 \(a\) 之后，还能期望拿到多少累计奖励"。最著名的是 **Q 值** \(Q(s,a)\)。做决策时挑 Q 值最大的动作即可。
- **策略法（policy-based）**：直接学策略 \(\pi(a|s)\)（下一节的策略梯度、Actor-Critic 属此）。

本节聚焦价值法。**Q-learning** 的核心是**贝尔曼方程（Bellman equation）**：一个动作的好坏 = 即时奖励 + 之后能拿到的最大未来回报（再打折）：

\[
Q(s,a) \;\leftarrow\; (1-\alpha)\,Q(s,a) \;+\; \alpha\Big(\,r + \gamma \max_{a'} Q(s',a')\,\Big)
\]

其中 \(\alpha\) 是学习率。直觉是：用"新观测到的 \(r + \gamma\max_{a'}Q(s',a')\)"去**软更新**旧估计 \(Q(s,a)\)。

**Q-learning 的经典做法是把 \(Q(s,a)\) 存成一张表（Q-Table）**：状态当行、动作当列，表里填数值。但 CartPole 的观测是**连续的**（位置、角度是小数），无法穷举成有限的行。解决办法是**状态离散化（discretization）**：把每个连续维度切成若干区间（bin），落进哪个区间就用哪个整数编号，于是连续观测变成有限的离散状态，才能塞进 Q 表。

> 当状态从连续变为离散后，DQN 就是"把这张 Q 表换成一个神经网络"——输入状态、输出每个动作的 Q 值。这样就不必离散化，也能处理天文数字的状态空间。**注意：本课目录没有实现完整 DQN，本节讲的是它的前身 Q-learning 的真实代码；DQN 在此作为概念延伸介绍。**

#### 4.3.2 核心流程

表格型 Q-learning 的训练循环（ε-贪心 + Bellman 更新）：

```
for 每一局 episode:
    obs = env.reset()
    while not done:
        s = discretize(obs)              # 连续观测 → 离散状态
        if random() < epsilon:           # 以 ε 概率利用
            a = 按 Q 表的概率分布采样
        else:                            # 否则探索
            a = 随机动作
        obs, r, done, _ = env.step(a)
        s' = discretize(obs)
        Q[s,a] ← (1-α)Q[s,a] + α(r + γ·max_a' Q[s',a'])   # Bellman 更新
```

其中 **ε-贪心（epsilon-greedy）** 就是 4.1 节"探索-利用权衡"的具体落地：大部分时候按学到的 Q 表选（利用），偶尔随机乱走（探索），避免陷入局部最优。

#### 4.3.3 源码精读（真实 Q-learning 代码）

本课目录里的 `notebook.ipynb` 完整实现了表格型 Q-learning。先看**状态离散化**——把 4 维连续观测压成整数元组（[notebook.ipynb:L241-L242](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/notebook.ipynb#L241-L242)）：

```python
def discretize(x):
    return tuple((x/np.array([0.25, 0.25, 0.01, 0.1])).astype(np.int))
```

它用一组手工设定的"分辨率"把每个分量除掉再取整——分辨率越细，离散状态越多、越精确，但 Q 表也越大、越难学满。

Q 表本身是一个字典，键是 `(状态, 动作)`，值是估计的 Q 值；未访问过的键默认为 0（[notebook.ipynb:L336-L341](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/notebook.ipynb#L336-L341)）：

```python
Q = {}
actions = (0,1)
def qvalues(state):
    return [Q.get((state,a),0) for a in actions]
```

三个超参数（[notebook.ipynb:L357-L359](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/notebook.ipynb#L357-L359)）：

```python
alpha = 0.3      # 学习率 α
gamma = 0.9      # 折扣因子 γ
epsilon = 0.90   # 利用概率 ε（注意：此处 0.90 表示 90% 时候利用）
```

训练循环里的**探索-利用分支**（[notebook.ipynb:L392-L397](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/notebook.ipynb#L392-L397)）：

```python
if random.random()<epsilon:
    # exploitation - chose the action according to Q-Table probabilities
    v = probs(np.array(qvalues(s)))
    a = random.choices(actions,weights=v)[0]
else:
    # exploration - randomly chose the action
    a = np.random.randint(env.action_space.n)
```

注意它"利用"时并非直接取 argmax，而是把 Q 值转成概率再采样（`probs`），这比纯贪心更柔和、保留了少量探索。整段训练循环跑 10 万局（[notebook.ipynb:L384-L403](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/notebook.ipynb#L384-L403)），核心就是这一行**贝尔曼更新**（[notebook.ipynb:L402-L402](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/notebook.ipynb#L402-L402)）：

```python
Q[(s,a)] = (1 - alpha) * Q.get((s,a),0) + alpha * (rew + gamma * max(qvalues(ns)))
```

逐项对照贝尔曼方程：`(1-alpha)*Q.get((s,a),0)` 是 \((1-\alpha)Q(s,a)\)，`rew + gamma*max(qvalues(ns))` 正是 \(r + \gamma\max_{a'}Q(s',a')\)。这一行就是 Q-learning 的全部灵魂。

**从 Q-learning 到 DQN 的概念延伸**：把上面这张字典 Q 表，换成一个神经网络 \(Q_\theta(s,a)\)（输入状态、输出各动作的 Q 值），用贝尔曼误差做回归损失去训练它，就是 **DQN（Deep Q-Network）**。其好处是不必手工离散化、能处理图像等高维状态；代价是训练更难稳定（需要经验回放、目标网络等技巧，超出本课范围）。**本课目录未实现完整 DQN**——但下一节 Actor-Critic 里的 **Critic** 就是一个学状态价值 \(V(s)\) 的神经网络，可视为价值法思想在策略法中的体现，是通往 DQN 的桥梁。

#### 4.3.4 代码实践：运行表格 Q-learning 并画奖励曲线

1. **实践目标**：亲眼看到 Q 表从"啥也不会"到"能平衡杆子"，理解价值法如何靠奖励自我提升。
2. **操作步骤**：在 `ai4beg` 环境打开 `notebook.ipynb`，从上到下依次运行到"Let's Start Q-Learning!"那一节（约第 19 个代码 cell，[notebook.ipynb:L384](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/notebook.ipynb#L384-L384)），训练 10 万局。然后用 Notebook 自带的滑动平均函数画图：

   ```python
   def running_average(x,window):
       return np.convolve(x,np.ones(window)/window,mode='valid')
   plt.plot(running_average(rewards,100))
   ```

3. **需要观察的现象**：原始 `rewards` 曲线抖动剧烈看不出趋势；滑动平均后能看到**平均奖励随训练逐步上升**。
4. **预期结果**：训练足够久后，平均生存步数会从初期的二三十逐步上升（CartPole-v1 上限 500）。**待本地验证具体数值**——Q-learning 对离散化分辨率和超参数敏感，结果可能波动。
5. **延伸操作（不改源码）**：把 `discretize` 里的分辨率数组改粗或改细（如把 `[0.25,0.25,0.01,0.1]` 换成 `[0.5,0.5,0.02,0.2]`），观察收敛速度与最终效果的变化，体会"离散化粒度"这一价值法特有的旋钮。

#### 4.3.5 小练习与答案

**练习 1**：贝尔曼更新里，为什么目标项要取 \(\max_{a'}Q(s',a')\) 而不是某个固定动作的 Q 值？
**参考答案**：因为我们假设在下一个状态 \(s'\) 会采取**最优**动作，所以应取所有可能动作里 Q 值最大的那个作为对未来的最佳估计。这正是 Q-learning"离策略（off-policy）"学习最优价值的来源。

**练习 2**：把 Q 表换成神经网络（即 DQN）后，为什么还需要"经验回放（experience replay）"和"目标网络"这类额外机制？
**参考答案**：连续采样得到的样本之间高度相关，会破坏神经网络对独立同分布样本的假设、导致训练发散；经验回放把过去的转移存起来随机抽样，去相关性。目标网络则让贝尔曼目标在一段时间内保持稳定，避免"追着一个不停移动的靶子学"而震荡。这些都是为稳定价值网络训练而引入的（**本课未实现**，仅作理解）。

---

### 4.4 Policy Gradient 与 Actor-Critic（本课 PyTorch Notebook 的核心）

> 本节对应**本课指定的主源码** `CartPole-RL-PyTorch.ipynb`。它实现的不是 Q-learning，而是策略法里最基础的两个算法：**策略梯度（Policy Gradient）** 与 **Actor-Critic**。

#### 4.4.1 概念说明

**策略梯度（Policy Gradient）** 的思路最直接：用一个神经网络当策略 \(\pi_\theta(a|s)\)——输入状态、输出动作概率分布。它和分类网络长得几乎一样，**关键区别是：我们事先并不知道每一步该选哪个动作**（没有标签）。

那怎么训练？用**奖励当"软标签"的权重**：对一局实验里每一步，算出它的折扣回报 \(G_t\)，然后强化那些"回报大的步骤所选的动作"的概率。形式上，策略梯度的损失是：

\[
\mathcal{L} = -\,\mathbb{E}\big[\,\log \pi_\theta(a|s)\,\cdot\, G_t\,\big]
\]

这个形式很像交叉熵（都有 \(\log\pi\)），区别在于乘的不是 0/1 标签，而是**回报 \(G_t\)**——回报越大，越要增大该动作的概率。负号是因为优化器做的是"最小化"损失，而我们要"最大化"期望回报。

课程对策略梯度的描述（[README.md:L58-L64](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/README.md#L58-L64)）：建模策略为"输入状态、返回动作概率"的网络；构造累计奖励向量并做折扣归一化；强化回报大的步骤。

**Actor-Critic** 是策略梯度的升级版（[README.md:L66-L75](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/README.md#L66-L75)）：一个网络同时输出两样东西——

- **Actor（演员）**：策略 \(\pi(a|s)\)，决定选什么动作。
- **Critic（评论员）**：估计当前状态的价值 \(V(s)\)，即"从这状态起还能期望拿多少回报"。

课程类比得很形象：这结构像 GAN（见 u3-l5），两个网络互相对抗又一起训练——Actor 提议动作，Critic 吹毛求疵地评估结果；我们的目标是让两者协同变强。Critic 学到的 \(V(s)\) 正是上一节说的"价值函数"，所以 **Actor-Critic = 策略法 + 价值法的合体**。

#### 4.4.2 核心流程

**策略梯度**训练一轮（一个 episode）的步骤：

```
1. 跑完一局，收集 trace：states, actions, probs, rewards
2. 算折扣回报 dr = discounted_rewards(rewards)  # 并归一化
3. gradients = one_hot(actions) - probs          # 实际动作与预测概率的差
4. gradients *= dr                                # 用回报加权
5. target = alpha*gradients + probs               # 合成"软目标"
6. 用 states, target 做一步监督式训练（loss = -mean(log(prob)*target)）
```

**Actor-Critic** 训练一步的关键：用 Critic 的估计和真实回报之差——**优势（advantage）**——来分别构造两个损失：

\[
A(s,a) = G_t - V(s)
\]

- **Actor 损失**：\(-\log\pi(a|s)\cdot A\)（优势为正就增大该动作概率，为负就减小）。
- **Critic 损失**：\(A^2\)（让 Critic 的估值 \(V(s)\) 逼近真实回报 \(G_t\)，就是回归）。

#### 4.4.3 源码精读（真实 PyTorch 代码）

**策略网络**的定义（[CartPole-RL-PyTorch.ipynb:L108-L118](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/CartPole-RL-PyTorch.ipynb#L108-L118)，第 7 个代码 cell）——一个 4→128→2 的小网络，末层 softmax 输出动作概率：

```python
num_inputs = 4
num_actions = 2
model = torch.nn.Sequential(
    torch.nn.Linear(num_inputs, 128, bias=False, dtype=torch.float32),
    torch.nn.ReLU(),
    torch.nn.Linear(128, num_actions, bias=False, dtype=torch.float32),
    torch.nn.Softmax(dim=1)
)
```

`run_episode` 负责跑完一局并收集 trace（[CartPole-RL-PyTorch.ipynb:L134-L142](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/CartPole-RL-PyTorch.ipynb#L134-L142)）：每步把状态喂进网络得 `action_probs`，按概率采样动作，执行 `env.step`，并记录状态、动作、概率、奖励。

**折扣回报**函数（[CartPole-RL-PyTorch.ipynb:L185-L196](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/CartPole-RL-PyTorch.ipynb#L185-L196)）——倒序累加 \(s \leftarrow r + \gamma s\)，这正是 \(G_t\) 的递推实现；随后做归一化，让回报变成有正有负的"权重"：

```python
def discounted_rewards(rewards,gamma=0.99,normalize=True):
    ret = []
    s = 0
    for r in rewards[::-1]:
        s = r + gamma * s
        ret.insert(0, s)
    if normalize:
        ret = (ret-np.mean(ret))/(np.std(ret)+eps)
    return ret
```

**策略梯度的损失**（[CartPole-RL-PyTorch.ipynb:L217-L224](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/CartPole-RL-PyTorch.ipynb#L217-L224)）——就是上面 \(\mathcal{L}=-\mathbb{E}[\log\pi\cdot G_t]\) 的直接翻译：

```python
def train_on_batch(x, y):
    optimizer.zero_grad()
    predictions = model(x)
    loss = -torch.mean(torch.log(predictions) * y)
    loss.backward()
    optimizer.step()
    return loss
```

主训练循环跑 300 局（[CartPole-RL-PyTorch.ipynb:L234-L248](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/CartPole-RL-PyTorch.ipynb#L234-L248)），每局：跑 trace → 算 gradients = one_hot − probs → 乘折扣回报 → 合成 target → `train_on_batch`。

**Actor-Critic** 部分定义了两个网络（[CartPole-RL-PyTorch.ipynb:L306-L340](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/CartPole-RL-PyTorch.ipynb#L306-L340)）：`Actor` 末层输出 logits 经 softmax 构成 `Categorical` 分布（输出动作概率），`Critic` 末层输出单个标量 \(V(s)\)。两个损失的写法（[CartPole-RL-PyTorch.ipynb:L405-L406](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/CartPole-RL-PyTorch.ipynb#L405-L406)）正好对应上面的公式：

```python
advantage = returns - values              # A = G - V
actor_loss = -(log_probs * advantage.detach()).mean()   # -log π · A
critic_loss = advantage.pow(2).mean()                   # A^2 回归
```

注意 `advantage.detach()`：算 Actor 损失时把 advantage 当作常数，只让梯度流回 Actor；Critic 损失则让梯度更新 Critic 使其逼近真实回报。两个 `zero_grad/backward/step` 分别更新两套参数（详见 [CartPole-RL-PyTorch.ipynb:L360-L408](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/CartPole-RL-PyTorch.ipynb#L360-L408) 的 `run_episode(actor, critic, n_iters)`）。

#### 4.4.4 代码实践：训练策略梯度并观察收敛

1. **实践目标**：跑通主 Notebook 的策略梯度部分，画出"奖励随训练轮数上升"的曲线。
2. **操作步骤**：打开 `CartPole-RL-PyTorch.ipynb`，从顶部依次运行到策略梯度的训练 cell（[CartPole-RL-PyTorch.ipynb:L237](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/CartPole-RL-PyTorch.ipynb#L237-L237)），它会训练 300 局并 `plt.plot(history)`。
3. **需要观察的现象**：训练开始时 `history`（每局总奖励）很低且抖动；随轮数增加，曲线整体上扬。
4. **预期结果**：策略梯度在 CartPole-v1 上通常能把平均奖励训到几百（接近上限 500）。**待本地验证**——策略梯度方差较大，单次运行可能不稳定，必要时多跑几次或增加训练轮数。
5. **延伸操作（不改源码逻辑）**：把 `discounted_rewards` 的 `gamma` 从 0.99 改成 0.5，观察"短视"会让训练变好还是变差，体会折扣因子对长期收益权衡的影响。

#### 4.4.5 小练习与答案

**练习 1**：策略梯度的损失 \(-\log\pi(a|s)\cdot G_t\) 和分类任务的交叉熵损失 \(-\log\pi(a|s)\) 形式很像，区别在哪？为什么需要这个区别？
**参考答案**：分类任务里乘的是固定的 0/1 标签（已知正确答案）；策略梯度里乘的是回报 \(G_t\)（没有标准答案，只能用"这步最终带来多少收益"当权重）。这个区别是因为 RL 里我们事先不知道哪步对、只能用收益大小来"奖惩"动作概率。

**练习 2**：Actor-Critic 相比纯策略梯度，引入 Critic 解决了什么问题？
**参考答案**：纯策略梯度直接用回报 \(G_t\) 当权重，方差很大（不同局的回报波动剧烈），训练不稳。Critic 提供一个基线 \(V(s)\)，用优势 \(A=G_t-V(s)\) 代替 \(G_t\) 能大幅降低方差——回报整体偏高的局不会让所有动作都被过度强化，从而训练更稳、更快。

---

## 5. 综合实践：把算法迁移到 MountainCar

本讲综合作业是 README 末尾布置的 [Train a Mountain Car](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/README.md#L112-L114)，对应 [lab/MountainCar.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/lab/MountainCar.ipynb)。

**任务背景**（见 [lab/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/22-DeepRL/lab/README.md)）：一辆车被困在山谷里，目标是冲出山谷到达旗子处。动作是向左加速、向右加速、不动（3 个离散动作）；观测是小车的位置和速度（2 维连续）。与 CartPole 不同，MountainCar 每步奖励为 −1（鼓励尽快登顶），是典型的**稀疏、延迟奖励**任务——随机策略几乎永远拿不到正反馈，因此它比 CartPole 更难，是检验算法泛化能力的好题目。

**实践步骤**：

1. 打开 `lab/MountainCar.ipynb`，先运行前几个 cell：`env = gym.make('MountainCar-v0')`，再用随机动作跑一局，确认你看得懂它的观测与动作（参考 4.2 的方法打印 `action_space`、`observation_space`）。
2. **选择一种本讲学过的算法迁移过去**（lab Notebook 文字提示是 "adopt Policy Gradients and Actor-Critic algorithms from the lesson"）：
   - **路线 A（策略法，推荐）**：把 `CartPole-RL-PyTorch.ipynb` 里的策略梯度代码拷过来，把 `num_inputs` 从 4 改成 2、`num_actions` 从 2 改成 3，`env.make` 换成 `MountainCar-v0`，其余结构基本不动。
   - **路线 B（价值法）**：把 `notebook.ipynb` 的表格 Q-learning 迁过来，重新设计 `discretize` 的分辨率（位置和速度两维），调整 ε、α、γ。
3. 训练若干轮，用 `plt.plot` 画出**奖励曲线**（建议用滑动平均平滑）。
4. 记录：训练多少轮后平均奖励趋于稳定？最好成绩是多少？哪种算法在这个稀疏奖励任务上更有效？

**需要观察的现象与预期结果**：

- 随机策略下，MountainCar 几乎永远失败（平均奖励停留在最差值附近，约 −200）。
- 训练后奖励曲线应缓慢上升（绝对值变小，即更少步数登顶）。MountainCar 的标准"解决"阈值是 100 局平均奖励 ≥ −110，**能否达到待本地验证**——它对探索策略很敏感，可能需要更长的探索阶段或更高的初始 ε。
- 记录下你的奖励曲线图，作为本次实践的产出。

**核心收获**（lab README 的 takeaway）：把 RL 算法适配到新环境往往很直接，因为 OpenAI Gym 对所有环境用同一套接口，算法本身几乎不依赖环境的物理性质——你甚至可以把环境作为参数传给同一个训练函数。这正是 4.2 节"Gym 统一接口"价值的最终验证。

## 6. 本讲小结

- 强化学习是"边做边学"的第三大范式，标准模型是 **MDP**：状态 \(s\)、动作 \(a\)、奖励 \(r\)、策略 \(\pi\)，目标是最大化**折扣回报** \(G_t=\sum\gamma^k r_{t+k}\)；两大难点是**延迟奖励**与**探索-利用权衡**。
- **OpenAI Gym** 用统一接口（`make`/`reset`/`step`/`render`、`action_space`/`observation_space`）封装所有环境；CartPole 是 4 维连续观测、2 个离散动作的平衡任务。
- **价值法**：Q-learning 用 Q 表存 \(Q(s,a)\)，靠**贝尔曼更新** \(Q\leftarrow(1-\alpha)Q+\alpha(r+\gamma\max Q)\) 自我提升；连续状态需先**离散化**。**DQN = 把 Q 表换成神经网络**（概念延伸，本课未实现完整版本）。
- **策略法**（本课主 Notebook 真实实现）：**策略梯度**用网络直接输出动作概率，损失 \(-\log\pi(a|s)\cdot G_t\) 用回报当权重；**Actor-Critic** 再加一个学价值 \(V(s)\) 的 Critic，用**优势** \(A=G_t-V(s)\) 降方差，是策略法与价值法的合体。
- 三种算法都只与抽象的"状态/动作/奖励"打交道，因此可平滑迁移到 MountainCar 等新环境——Gym 的统一接口是关键。

## 7. 下一步学习建议

- **深入经典 RL**：本课 README 的"Review & Self Study"指向姊妹课程 [ML-For-Beginners 的强化学习单元](https://github.com/microsoft/ML-For-Beginners/blob/main/8-Reinforcement/README.md)，那里更系统地讲 Q-learning、价值迭代等经典方法，推荐作为本讲的补充。
- **实现一个真正的 DQN**：本课只到表格 Q-learning 和 Actor-Critic 为止。建议你尝试把 Q 表换成神经网络，加入经验回放与目标网络，在 CartPole 或 Atari 上跑一个最小 DQN，体会"价值网络"的工程细节。
- **看更前沿的应用**：README 的"Other RL Tasks"提到用 CNN 处理 Atari 截图（把 u3-l2 的卷积网络接到 RL 上）、Alpha Zero 自我对弈学棋、以及工业控制服务 Bonsai——这些是 RL 从课程走向真实世界的延伸方向。
- **回顾本单元其他支线**：RL 与 u5-l1 遗传算法同属"非梯度的优化/学习"思路，可对照体会"靠奖励/适应度驱动"与"靠梯度驱动"两类范式的异同。
