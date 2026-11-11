# Deep Q-Network：CartPole 平衡实验

## 1. 本讲目标

学完本讲,你应该能够:

1. 说清楚「为什么要用神经网络近似 Q(s,a)」,以及它与上一讲表格型 Q-learning 的本质区别(函数逼近 vs 查表)。
2. 逐行读懂 `ch19/cartpole/main.py` 中的 `DQNAgent`:Q 网络结构、经验回放(replay buffer)、TD 目标构造、Adam + MSE 批量拟合。
3. 理解经验回放与批量更新(一次梯度更新消费 32 个转移)各自解决什么问题。
4. 理解目标网络(target model)的概念与作用,并知道**仓库代码并未实现它**(本讲会讲清这一取舍,并给出自行补全的示例代码)。
5. 会解读 CartPole「按步计回报」的学习曲线,并完成 batch_size = 8 / 32 / 128 的对比实验。

## 2. 前置知识

本讲是第 19 章收官讲,直接承接 [u19-l1](u19-l1-q-learning-gridworld.md) 的表格型 Q-learning。请先确认你还记得这些概念(不记得就回去翻一眼):

- **MDP 五元组**、奖励与回报、折扣因子 γ、策略。
- **动作价值函数 Q(s,a)**:在状态 s 执行动作 a,之后按某策略走完所能拿到的期望折扣回报。
- **TD 目标与离策略(off-policy)更新**:Q-learning 的更新目标 \( r + \gamma \max_{a'} Q(s', a') \),行为策略是 ε-greedy、更新目标指向贪心策略。
- **episode(回合)**:从 `env.reset()` 到终止条件触发的一段交互。

在此基础上,本讲需要三个新直觉:

**① 从查表到函数逼近。** 上一讲的 GridWorld 只有 30 个离散状态,一张 `defaultdict` 就能装下整个 Q 表。但真实任务的状态往往是连续的:CartPole 的状态是 4 个连续浮点数(小车位置、小车速度、杆的角度、杆的角速度)。想用表格覆盖它,必须离散化,而 4 维连续空间离散化后格子数指数膨胀(维数灾难),且每个格子的 Q 值只能靠恰好落进去的样本更新、彼此不共享。解决办法是**参数化**:用一组权重 w 的神经网络 \( Q(s, a; \mathbf{w}) \) 来逼近 Q 函数——网络见过的状态会泛化到相邻的未见状态。这正是「Deep」Q-Network 中 Deep 的含义。

**② 网络输出是「动作维向量」。** CartPole 只有 2 个动作(向左推、向右推),让网络一次输出 2 维向量、第 k 维即 \( Q(s, a_k) \),取 `argmax` 就是贪心动作。这与第 11 章 MNIST 网络「输出 10 维、argmax 取类别」的套路完全一致——区别只在训练信号:监督学习来自人工标签,DQN 的标签来自环境返回的奖励。

**③ 训练循环仍是五步骨架。** 前向、算损失、`backward`、`step`、`zero_grad`——这套在 [u12-l3](u12-l3-nn-autograd-xor.md) 建立的骨架在 DQN 里原样复用,变的只是「标签怎么来」。

另外复习一个 PyTorch 细节:构造 TD 目标时要套 `torch.no_grad()`,因为**目标是我们回归的标签,不是要优化的预测**,不能让梯度流进目标(与 u14-l2 评估循环里 `no_grad` 的用法同源)。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ch19/cartpole/main.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L1-L169) | 本讲唯一的核心源码:Transition 五元组、DQNAgent(网络/回放/学习)、训练主循环与学习曲线绘制,共约 170 行 |
| [ch19/README.md](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/README.md#L42-L46) | 第 19 章「A glance at deep Q-learning」小节大纲:Replay memory、确定 target 值、实现 deep Q-learning |
| [ch19/ch19.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/ch19.ipynb) | 与 main.py 等价的 notebook 版本(含 gym 0.20.0 的版本检查与书中配图 19_10~19_13) |

注意两件仓库事实,避免踩坑:

- 根目录 `environment.yml` **不包含 gym**;notebook 的版本检查要求 `gym >= 0.20.0`(实测输出 gym 0.21.0)。运行本讲代码需自行 `pip install gym==0.21.0`(或 0.20)。新版 `gymnasium` 的 API 与书不兼容(`reset()` 返回 `(state, info)` 元组、渲染需指定 `render_mode`),照抄书代码会报错。
- 仓库版网络是 **4 个 Linear 层**(见 4.1.3),一些资料把它笼统称作「多层感知机/两层隐网络」,以源码为准。

## 4. 核心概念与源码讲解

### 4.1 从 Q 表到 Q 网络:用神经网络近似 Q(s,a)

#### 4.1.1 概念说明

表格型 Q-learning 对每个 (状态, 动作) 对存一个数。当状态连续或高维时,这条路走不通,改为学一个函数 \( Q(s, a; \mathbf{w}) \)。好处有三:

1. **泛化**:更新 \( Q(s, a) \) 时,与 s 相近的状态也会被「顺带」修正,样本利用率高得多。
2. **存储**:参数量只由网络结构决定,与状态空间大小无关。
3. **一次前向,全部动作打分**:网络输出 \( |A| \) 维向量,贪心动作就是 `argmax`,选动作只需一次前向。

代价是引入了新的不稳定性:TD 目标里的 \( \max_{a'} Q(s', a'; \mathbf{w}) \) 用的正是被优化的同一组 w,**目标随参数一起动**——这是 4.4 目标网络要回应的问题。

#### 4.1.2 核心流程

```
输入状态 s (4 维连续向量)
   │
   ▼
Linear(4→256) → ReLU
Linear(256→128) → ReLU
Linear(128→64)  → ReLU
Linear(64→2)                 ← 输出 [Q(s,左), Q(s,右)]
   │
   ▼
argmax → 选中的动作(ε-greedy 之外的时刻)
```

数学上,DQN 要拟合的仍然是贝尔曼最优方程的近似:

\[
Q^*(s, a) \approx r + \gamma \max_{a'} Q^*(s', a')
\]

网络以 TD 目标为标签做回归(损失函数见 4.3)。

#### 4.1.3 源码精读

网络与优化器在 `_build_nn_model` 中构建:

[ch19/cartpole/main.py:L52-L63](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L52-L63) —— 用 `nn.Sequential` 堆叠 4 个 Linear 层(3 个隐藏层,宽度 256/128/64,ReLU 激活),输出维度直接取 `env.action_space.n`(CartPole 为 2);损失函数是 `MSELoss`,优化器是 Adam(学习率默认 1e-3,见 `__init__`)。

```python
self.model = nn.Sequential(nn.Linear(self.state_size, 256),
                           nn.ReLU(),
                           nn.Linear(256, 128),
                           nn.ReLU(),
                           nn.Linear(128, 64),
                           nn.ReLU(),
                           nn.Linear(64, self.action_size))
```

状态与动作维度从环境自动读取:

[ch19/cartpole/main.py:L40-L41](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L40-L41) —— `observation_space.shape[0]` 得到 4(状态维数),`action_space.n` 得到 2(离散动作数)。换环境时代码无需改动。

选动作的 ε-greedy:

[ch19/cartpole/main.py:L68-L73](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L68-L73) —— 以概率 ε 随机选动作(探索);否则 `no_grad` 下前向一次,输出形状 `(1, 2)`、`[0]` 取第一行得 2 维 Q 值,`argmax` 取贪心动作。注意与表格版一样是**行为策略用 ε-greedy、评估动作用 max**,Q-learning 的离策略结构原封不动。

```python
def choose_action(self, state):
    if np.random.rand() <= self.epsilon:
        return np.random.choice(self.action_size)
    with torch.no_grad():
        q_values = self.model(torch.tensor(state, dtype=torch.float32))[0]
    return torch.argmax(q_values).item()
```

#### 4.1.4 代码实践(源码阅读型,无需环境)

1. **实践目标**:确认网络结构、参数量与输入输出形状。
2. **操作步骤**:在已装 PyTorch 的环境里(不需要 gym)运行:

```python
# 示例代码:手动复现 _build_nn_model 并检查形状
import torch, torch.nn as nn
state_size, action_size = 4, 2
model = nn.Sequential(nn.Linear(state_size, 256), nn.ReLU(),
                      nn.Linear(256, 128), nn.ReLU(),
                      nn.Linear(128, 64), nn.ReLU(),
                      nn.Linear(64, action_size))
x = torch.zeros(1, state_size)          # 模拟一个状态(注意代码用 (1,4) 形状)
print(model(x).shape)                    # 期望 torch.Size([1, 2])
print(sum(p.numel() for p in model.parameters()))  # 参数量
```

3. **需要观察的现象**:输出形状 `(1, 2)`;参数量 = (4×256+256) + (256×128+128) + (128×64+64) + (64×2+2) = 42,562。
4. **预期结果**:与上式一致;把 `x` 换成 `torch.zeros(7, 4)`(批前向)应得 `(7, 2)`,这就是 4.3 中批量前向的形状来源。

#### 4.1.5 小练习与答案

**练习 1**:为什么输出层不加 softmax 或 sigmoid,而直接输出原始值?
**答**:输出是 Q 值(期望回报),是任意实数、不是概率分布;比较动作大小只需相对顺序,`argmax` 对原始值即可。这与 u12-l3 的结论呼应——输出层激活要按任务语义选,Q 回归任务用恒等(无激活)。

**练习 2**:若把环境换成动作数为 7 的任务,代码要改哪里?
**答**:不用改。`action_size` 由 `env.action_space.n` 自动读出,网络输出维数随之变为 7;`choose_action` 的 `argmax` 与随机采样也自动适配。

### 4.2 经验回放:replay buffer 与 Transition 五元组

#### 4.2.1 概念说明

有了网络,最朴素的训练方式是「每交互一步,就用刚发生的这一个转移 (s, a, r, s') 做一次梯度更新」。这有两个问题:

1. **样本强相关**:连续转移来自同一条轨迹、甚至同一回合,前后状态高度相似。SGD 假设样本近似独立同分布,拿一串强相关样本连续更新,梯度方向抖来抖去,容易震荡发散。
2. **样本用一次就扔**:昂贵的交互数据只做一次更新就被丢弃,浪费。

经验回放(experience replay)把每个转移存进一个大缓冲区,训练时从中**随机抽一小批**:随机性打碎了时间相关性(相邻两步的被抽到的概率独立),老样本可被反复抽样、多次复用,一举解决两个问题。它之所以可行,根本原因还是 Q-learning 的**离策略**性质:TD 目标里的 `max` 不依赖实际采取动作的行为策略,所以旧策略(旧参数)产生的数据对当前策略依然有效。

缓冲区用 `deque(maxlen=...)` 实现:装满后新样本自动挤掉最旧的——**回放记忆是有容量限制的**,旧经验会遗忘,防止偏向早期(更差的)策略。

#### 4.2.2 核心流程

```
每交互一步:
    transition = (s, a, r, s', done)   # namedtuple Transition
    memory.append(transition)          # deque 满(2000 条)则自动淘汰最旧的

每次训练:
    samples = random.sample(memory, batch_size)   # 均匀随机抽 32 条
    _learn(samples)                               # 见 4.3
```

主循环开工前还有一段 **warm-up(预热)**:先用纯随机策略跑 500 步,只存不学,把缓冲区填到一定量,避免一开始就靠极少样本更新。

#### 4.2.3 源码精读

转移的数据结构:

[ch19/cartpole/main.py:L28-L30](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L28-L30) —— `namedtuple` 定义五元组 `(state, action, reward, next_state, done)`,与上一讲 gridworld 的 `qlearning.py` 完全同名同构;`done` 标记回合是否终止,决定 TD 目标里要不要加自举项(见 4.3)。

缓冲区与存取:

[ch19/cartpole/main.py:L43](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L43) —— `self.memory = deque(maxlen=max_memory_size)`,`max_memory_size` 默认 2000(构造参数在 [L34-L38](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L34-L38))。

[ch19/cartpole/main.py:L65-L66](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L65-L66) —— `remember` 只有一行 `append`,接口名与表格版 Agent 保持一致。

[ch19/cartpole/main.py:L107-L109](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L107-L109) —— `replay` 用 `random.sample` 从缓冲区均匀随机抽 `batch_size` 条,交给 `_learn`。`random.sample` 要求当前存量 ≥ batch_size,所以预热步数(500)必须大于 batch_size——这也是综合实践中 batch_size 最大取 128 仍安全的原因。

```python
def replay(self, batch_size):
    samples = random.sample(self.memory, batch_size)
    return self._learn(samples)
```

预热填充:

[ch19/cartpole/main.py:L135-L146](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L135-L146) —— 主入口先循环 `init_replay_memory_size = 500` 次:选动作(`ε=1.0` 时全部随机)、`env.step`、`remember` 存入;`done` 则重置环境,否则状态前移。注意此阶段**不调用 `replay`**,也不产生损失。

#### 4.2.4 代码实践(源码阅读型)

1. **实践目标**:验证 deque 的淘汰行为与 random.sample 的前提。
2. **操作步骤**:

```python
# 示例代码
from collections import deque
import random
m = deque(maxlen=5)
for i in range(8):
    m.append(i)
print(m)                          # deque([3,4,5,6,7]) —— 最旧的 0,1,2 被挤掉
print(random.sample(m, 3))        # 3 个互不相同的元素,顺序随机
random.sample(m, 10)              # ValueError: Sample larger than population
```

3. **需要观察的现象**: maxlen 满后 append 自动淘汰队首;抽样数超过存量直接抛异常。
4. **预期结果**: 如注释所示。这解释了两个设计:为什么 2000 条的缓冲区在训练中期以后永远存满旧策略数据、为什么 batch_size 不能大于预热后的样本量。

#### 4.2.5 小练习与答案

**练习 1**:SARSA(on-policy)能直接配经验回放吗?为什么 Q-learning 可以?
**答**:不能直接配。SARSA 的 TD 目标是 \( r + \gamma Q(s', a') \),其中 \( a' \) 必须是**当前行为策略**接下来真会采取的动作;缓冲区里的旧数据由旧策略产生,与当前策略不匹配。Q-learning 的目标取 `max`,与行为策略无关,旧数据仍是对最优 Q 的有效样本,所以能离策略回放。

**练习 2**:把 `max_memory_size` 从 2000 改成 200,预期训练会有什么变化?
**答**:缓冲区只能看到最近约 200 个转移,样本多样性骤降、相关性回升,且早期覆盖到的状态空间很快被遗忘,学习曲线会明显更抖、收敛更慢甚至不收敛(待本地验证)。

### 4.3 `_learn`:TD 目标构造与批量 MSE 拟合

#### 4.3.1 概念说明

这是 DQN 的心脏。对每个转移,定义 TD 目标:

\[
y = \begin{cases} r & \text{若 } done \\ r + \gamma \max_{a'} Q(s', a'; \mathbf{w}) & \text{否则} \end{cases}
\]

它是「对 \( Q(s,a) \) 应该是多少」的估计。训练就是让网络的 \( Q(s, a) \) 往 y 靠:最小化均方误差

\[
L(\mathbf{w}) = \mathbb{E}_{(s,a,r,s') \sim \text{buffer}}\left[ \left( Q(s,a;\mathbf{w}) - y \right)^2 \right]
\]

关键技巧是 **target_all(整行目标)**:网络对状态 s 输出的是一个 2 维向量(两个动作各一个 Q 值),但我们只想更新被实际执行的动作 a 的那一维。做法是:把网络当前的输出整行复制下来,只把第 a 位替换成 TD 目标 y,其余位保持网络原值,然后拿这行「修改过的输出」当回归标签。由于其余位标签 = 网络当前输出,这些位置的误差为 0,MSE 实际只惩罚被替换的那一位——**一次前向 + 一次 MSE,就完成了「只更新 Q(s,a)」的语义**。

「批量」的含义:`batch_size=32` 个转移的 32 个 (输入, 标签) 对堆成一个 batch,五步骨架跑**一轮**(一次 backward、一次 step)。对比表格版 `_learn` 一次吃一个转移、每步改一个表格项,DQN 每 32 个转移共享一次梯度更新——梯度是 32 个样本误差的平均,方差更小、方向更稳;同时一次矩阵前向吞吐 32 个样本,比 32 次单样本前向高效得多(与 u11-l2 的 minibatch 分析同一逻辑,只是那时反向传播是手推的,现在交给 autograd)。

#### 4.3.2 核心流程

`_learn(batch_samples)` 的两段式结构:

```
第一阶段(逐转移构造标签,全部在 no_grad 下):
    for (s, a, r, next_s, done) in batch_samples:
        if done:  y = r
        else:     y = r + gamma * max(Q(next_s))      # 自举目标
        target_all = Q(s).clone()                     # 网络当前输出
        target_all[a] = y                             # 只改被采取动作那一位
        收集 (s, target_all)
        _adjust_epsilon()                             # 注意:在循环内!

第二阶段(一次梯度更新):
    optimizer.zero_grad()
    pred = model(batch_states)                        # (32, 2)
    loss = MSE(pred, stack(batch_targets))            # (32, 2) 对 (32, 2)
    loss.backward(); optimizer.step()
```

#### 4.3.3 源码精读

TD 目标与 target_all 的构造:

[ch19/cartpole/main.py:L75-L92](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L75-L92) —— 整段包在 `with torch.no_grad():` 里:先按 `done` 分支算 y(非终止时用 `self.model(next_s)[0].max()` 自举,[L84-L85](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L84-L85);终止时 y=r,不再自举——终止状态之后没有未来回报);再把网络对 s 的输出整行取下、只改第 a 位([L87-L88](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L87-L88))。`s.flatten()` 把 (1,4) 压成 (4,) 以便堆叠成批。

```python
with torch.no_grad():
    if done:
        target = r
    else:
        pred = self.model(torch.tensor(next_s, dtype=torch.float32))[0]
        target = r + self.gamma * pred.max()

    target_all = self.model(torch.tensor(s, dtype=torch.float32))[0]
    target_all[a] = target
```

一个值得咀嚼的细节:构造标签时网络参数尚未更新(`optimizer.step()` 在循环之后),而 batch 前向([L95](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L95))用的还是同一组参数——所以 `pred` 与 `target_all` 中**未被替换的位置数值完全相等**,loss 全部来自被替换的第 a 位。这就是上一节「target_all 技巧」在代码里的落点。

批量梯度更新(熟悉的五步骨架):

[ch19/cartpole/main.py:L94-L99](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L94-L99) —— `zero_grad` → 批前向得 (32,2) 预测 → 与 `torch.stack` 出的 (32,2) 标签算 MSE → `backward` → `step`。MSE 默认对所有元素取平均,即每个转移、每个动作位权重相同。

[ch19/cartpole/main.py:L103-L105](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L103-L105) —— `_adjust_epsilon`:ε 高于下限 0.01 就乘衰减系数 0.995。**注意它的调用位置在 [L92](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L92),在 `for transition in batch_samples` 循环体内**——每学一个样本就衰减一次,一次 `replay(32)` 就要连乘 32 次 0.995(约 0.85)。训练循环里每个环境步都调用一次 replay,于是 ε 从 1.0 降到 0.01 大约只需 \( \lceil \ln 0.01 / \ln 0.995 \rceil \div 32 \approx 29 \) 个环境步——探索期结束得非常快。这是实现上的一个耦合点:batch_size 越大,ε 衰减越快,直接影响综合实验的结果解读。

#### 4.3.4 代码实践(源码阅读型,无需环境)

1. **实践目标**:用数值验证「loss 只来自被替换的第 a 位」。
2. **操作步骤**:跑以下示例脚本(只依赖 torch 与 numpy):

```python
# 示例代码:手工复现 _learn 的标签构造,验证误差来源
import torch, torch.nn as nn, numpy as np
torch.manual_seed(1); np.random.seed(1)
model = nn.Sequential(nn.Linear(4, 8), nn.ReLU(), nn.Linear(8, 2))
s = np.random.randn(1, 4).astype(np.float32); a, r, done = 1, 1.0, False
with torch.no_grad():
    q_row = model(torch.tensor(s))[0].clone()     # 网络对 s 的原始输出
    target_all = model(torch.tensor(s))[0]
    target_all[a] = r + 0.95 * q_row.max()        # 复现 L81-L88(自举目标)
loss_fn = nn.MSELoss()
with torch.no_grad():
    pred = model(torch.tensor(s))
    print("整体 MSE:", loss_fn(pred, target_all.unsqueeze(0)).item())
    per_elem = (pred[0] - target_all) ** 2
    print("各位置平方误差:", per_elem.tolist())
    only_a = per_elem[a].unsqueeze(0).unsqueeze(0).mean()
    print("仅第 a 位 / 全部元素 的比值:", (only_a / per_elem.mean()).item())
```

3. **需要观察的现象**:除第 a 位外的平方误差应约为 0,「比值」应约为 2.0(2 个动作位中只有 1 个非零)。
4. **预期结果**:与上述一致——证明 target_all 技巧确实把学习信号集中到了被采取动作的 Q 值上。

#### 4.3.5 小练习与答案

**练习 1**:`done=True` 时为什么目标就是 r,不加自举项?
**答**:终止状态之后不存在任何后续状态与动作,未来的折扣回报为 0,TD 目标退化为 \( y = r + \gamma \cdot 0 = r \)。若照常自举,等于给一个「不存在的未来」估值,会系统性高估终止前的 Q 值。

**练习 2**:表格版 Q-learning 用 `q_table[s][a] += lr * (q_target - q_val)` 增量更新,DQN 用 MSE + 梯度下降,两者在「更新谁」上有何共同点?
**答**:都只更新被实际执行动作 (s,a) 的估值、不动其他动作——表格版靠索引 `[s][a]` 精确写入,网络版靠 target_all 技巧把其余位置的梯度压为 0。更新方向也同构:都是把当前估计朝 TD 目标 \( r + \gamma\max Q(s') \) 拉近,步长分别是 lr 与(学习率缩放后的)梯度步。

**练习 3**:如果把 batch 里 32 个样本拆成 32 次单样本更新(每次一个 backward+step),与现在一次批量更新相比,计算上和统计上各差在哪?
**答**:计算上,32 次小矩阵乘的启动开销远大于一次 (32,4) 的批量矩阵乘,GPU/向量化效率低;统计上,单样本梯度噪声大、路径震荡,批量梯度是 32 个误差的平均、方差降到约 1/32,但每次更新的信息成本也从「32 个样本」变成「1 个样本」——同等内容量下更新次数变多、每次更不稳。

### 4.4 目标网络 target model:给自举目标一个「慢速锚」

#### 4.4.1 概念说明

4.3 的 TD 目标里,`max Q(s')` 用的就是正在被优化的同一组权重 w——**回归的目标随回归模型一起移动**,像追着自己尾巴跑。参数一动,所有状态的 Q 值(包括目标)整体漂移,误差相关性很强,训练容易震荡甚至发散。

标准 DQN(Mnih et al., 2015)的解法是**目标网络(target network)**:另存一份冻结副本 \( Q(s, a; \mathbf{w}^-) \),TD 目标一律用副本计算:

\[
y = r + \gamma \max_{a'} Q(s', a'; \mathbf{w}^-)
\]

副本参数 \( \mathbf{w}^- \) 不参与梯度更新,只在**每隔固定周期**(例如每 N 个环境步或每若干回合)用在线网络整体拷贝一次(\( \mathbf{w}^- \leftarrow \mathbf{w} \))。两次拷贝之间目标是常数,「追移动目标」变成「追阶段性冻结的目标」,训练稳定得多。代价是目标滞后于最新策略。

**必须诚实地指出:仓库的 `main.py` 没有实现目标网络。** 全文只有一个 `self.model`,自举项与被回归项都用它([L84-L87](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L84-L87))。书中 [README 的小节标题](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/README.md#L44-L46)「Determining the target values for computing the loss」讲的是 4.3 那套 TD 目标构造,而非目标网络的周期同步。CartPole 任务小、网络容量也小,简化实现通常也能收敛,但这属于已知取舍,不是 DQN 的完整形态。

#### 4.4.2 核心流程

```
初始化: target_model = 在线网络的一份拷贝,不注册进 optimizer
每个环境步: y = r + gamma * max target_model(s')   # 目标永远查冻结副本
每隔 sync_period 步: target_model.load_state_dict(model.state_dict())
```

#### 4.4.3 源码精读(示例代码,仓库未实现)

以下是基于仓库 `DQNAgent` 的最小补全示例,**明确标注为示例代码,不是仓库原有内容**:

```python
# 示例代码:在 DQNAgent 上补一个目标网络
def _build_nn_model(self):
    self.model = nn.Sequential(...)                    # 原样
    self.target_model = copy.deepcopy(self.model)      # 冻结副本
    for p in self.target_model.parameters():
        p.requires_grad = False                        # 不进优化器、不求梯度
    self.loss_fn = nn.MSELoss()
    self.optimizer = torch.optim.Adam(self.model.parameters(), self.lr)
    # 注意 optimizer 只收 self.model 的参数

def _learn(self, batch_samples):
    ...
    pred = self.target_model(torch.tensor(next_s, dtype=torch.float32))[0]
    target = r + self.gamma * pred.max()               # 自举项改查冻结副本
    ...

def sync_target(self):                                 # 周期调用
    self.target_model.load_state_dict(self.model.state_dict())
```

主循环里在 [L167-L168](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L167-L168) 调用 `agent.replay(batch_size)` 处,可以加一个步数计数器,例如每 100 个环境步执行一次 `agent.sync_target()`。`deepcopy` / `load_state_dict` 的用法正是 [u13-l2](u13-l2-project-structure-checkpoint.md) 讲过的 state_dict 机制的直接复用。

#### 4.4.4 代码实践(进阶,可选)

1. **实践目标**:亲手给仓库版 DQN 加上目标网络,观察学习曲线是否更平稳。
2. **操作步骤**:复制 `main.py` 到自己的实验目录;按 4.4.3 示例修改(自举项查 `target_model`、新增 `sync_target`、主循环每 100 步同步);跑 200 回合对比原版曲线。
3. **需要观察的现象**:两条 `total_rewards` 曲线的平滑程度与后期稳定性差异。
4. **预期结果**:CartPole 较简单,两版可能都能到 400+ 的量级;带目标网络的版本曲线抖动通常更小。同步周期过短(如每步同步)会退化回原版,过长则目标过旧、学习变慢。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**:目标网络为什么不能和在线网络共享参数?把它也交给 optimizer 会怎样?
**答**:共享或一同优化的话,「目标」又会随每步梯度立即漂移,回到追移动目标的不稳定局面,目标网络就失去意义。冻结副本的作用正是让目标在两次同步之间保持常数。

**练习 2**:目标网络与经验回放在「稳定训练」上各解决哪一面?
**答**:经验回放解决**数据侧**——随机抽样打散转移的时间相关性,让 minibatch 近似独立同分布;目标网络解决**目标侧**——切断「目标依赖被优化参数」的反馈回路。两者搭配才构成完整 DQN。

### 4.5 主循环:episode 交互与按步计回报的监控

#### 4.5.1 概念说明

CartPole-v1 的任务:小车上一根倒立的杆,每步给小车向左或向右一个推力,杆尽量不倒。**奖励按步发放**:每存活一步得 +1,杆倾角过大或小车出界则 `done=True`,回合结束且单回合步数上限 500。所以与 GridWorld「终局一次性给 ±1」不同,CartPole 的**总回报 = 存活步数**,学习曲线天然用「每回合活了多少步」来监控——这就是「按步计算回报」:不需要等终局大奖励,每一步的 +1 累积起来本身就是性能指标,回报的提升即时反映在曲线上。

监控指标由此变得极简:每回合结束时记录存活步数即可(代码记为 `total_rewards`)。理论上折扣回报是 \( \sum_t \gamma^t \cdot 1 \),但监控曲线用不加折扣的原始步数更直观;折扣 γ=0.95 只作用在训练目标内部。

#### 4.5.2 核心流程

```
全局设置: EPISODES=200, batch_size=32, 预热=500
1. 建环境与 agent;随机策略跑 500 步填满部分缓冲区(4.2)
2. for e in 0..199:                       # 每个 episode
       state = env.reset()
       for i in 0..499:                   # 每回合最多 500 步
           action  = agent.choose_action(state)
           next_s, r, done, _ = env.step(action)
           agent.remember(...)            # 先存再学
           state = next_s
           if done:
               total_rewards.append(i)    # 记录「回报」(见下)
               break
           loss = agent.replay(32)        # 每个存活步都训练一次
3. plot_learning_history(total_rewards)
```

#### 4.5.3 源码精读

全局设置:

[ch19/cartpole/main.py:L124-L127](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L124-L127) —— 回合数 200、批大小 32、预热 500,三个超参数集中在文件顶部,方便实验修改(综合实践就改这里)。

环境与智能体创建:

[ch19/cartpole/main.py:L129-L131](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac337afc3c0a91/ch19/cartpole/main.py#L129-L131) —— `gym.make('CartPole-v1')` 建环境;`state_size`、`action_size` 在 `DQNAgent.__init__` 里自动从环境读取。老版 gym 的 `env.reset()` 直接返回状态数组(新版 gymnasium 返回 `(state, info)` 元组,照抄会解包失败)。

训练主循环:

[ch19/cartpole/main.py:L148-L168](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L148-L168) —— 双层结构:外层回合、内层最多 500 步;每步「存入缓冲区 → 状态前移 → 若终止则记录并跳出,否则训练一次」。注意训练调用在 `done` 判断之后:终止步**只存不学**。

回报记录的细节:

[ch19/cartpole/main.py:L163-L166](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L163-L166) —— `total_rewards.append(i)` 记录的是终止时的循环变量 i。由于 i 从 0 计数,若回合在第 i 次迭代终止,真实总回报是 \( i+1 \)(共发生 i+1 次 step、每次 +1)。也就是说曲线数值比真实回报**整体小 1**——量级判断无碍,做精细对比时记得这一 off-by-one。此外 `if e % 10 == 0: env.render()`([L151-L152](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L151-L152)、[L161-L162](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L161-L162))每 10 回合渲染一帧动画;在无显示器的服务器上应注释掉,否则报错。

```python
if done:
    total_rewards.append(i)
    print(f'Episode: {e}/{EPISODES}, Total reward: {i}')
    break
loss = agent.replay(batch_size)
```

学习曲线绘制:

[ch19/cartpole/main.py:L112-L121](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L112-L121) —— `plot_learning_history` 把回合序号与 `total_rewards` 画成折线,与上一讲 gridworld 的同名函数同构,只是纵轴从「步数+终局奖励」双子图变成单一总回报。理想形态:初期 20 步上下(随机策略水平),随 ε 衰减与网络学习爬升,最终逼近 500 的天花板(曲线记录值上限 499)。

#### 4.5.4 代码实践(运行型,需 gym 0.20/0.21)

1. **实践目标**:完整跑通一次训练,亲眼看到回报爬升。
2. **操作步骤**:
   - 在 pyml-book 环境中 `pip install gym==0.21.0`(environment.yml 未含 gym,需手动装;不要装 gymnasium,API 不兼容)。
   - 复制 `ch19/cartpole/main.py` 到自己的实验目录(不改源码目录)。
   - 无显示器的机器:注释掉两处 `env.render()`。
   - 为快速验证,可先把 `EPISODES` 从 200 改为 50。
   - 运行 `python main.py`。
3. **需要观察的现象**:终端逐回合打印 `Episode: e/200, Total reward: i`;前期数字小且波动,后期逐渐增大;结束后弹出总回报曲线。
4. **预期结果**:50 回合内可看到回报从约 10~30 上升到几十~一百多的量级;完整 200 回合通常能爬到数百步(书中图 19_13 展示了训练末期的回报水平)。DQN 对随机种子敏感,单次运行的绝对数值有波动,**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**:为什么 CartPole 的监控用「存活步数」而 GridWorld 用「步数 + 终局奖励」两个指标?
**答**:GridWorld 奖励稀疏——绝大多数步 r=0、只有到达金子或陷阱才有 ±1,步数与终局奖励分别反映「路径效率」与「结局好坏」,缺一不可;CartPole 每步奖励恒 +1,总回报本身就等于存活步数,一个指标足够。

**练习 2**:如果把内层循环 `for i in range(500)` 的上限改成 100,曲线会发生什么?
**答**:回合被人为截断在 100 步(即便杆没倒),`total_rewards` 的天花板变成 99,无法再区分「能活 100 步」与「能活 500 步」的策略,后期曲线会挤在 99 附近失去分辨力。上限应与环境的回合步数上限一致。

## 5. 综合实践:batch_size = 8 / 32 / 128 对比实验

这是本讲的收官实践,把四个模块(网络、回放、批量更新、回报监控)串成一次可控实验:**固定其他一切,只改批量大小,观察训练稳定性与样本效率的变化**。

### 5.1 实践目标

理解 batch_size 同时影响三件事:

1. **梯度质量**:批越大,单次梯度的方差越小、更新越平稳;
2. **计算/样本成本**:每个环境步都训练一次,批越大每次前向反传越贵,而交互的样本产生速率不变;
3. **探索时长**(仓库实现的特有耦合):ε 衰减挂在 `_learn` 的样本循环里([L92](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L92)),批越大,每个环境步 ε 连乘的次数越多,探索期越短——ε 从 1.0 降到 0.01 所需的环境步数约为 919/batch_size,即 batch=8 约 115 步、batch=32 约 29 步、batch=128 约 8 步。

### 5.2 操作步骤

1. 把 `ch19/cartpole/main.py` 复制三份到自己目录:`main_b8.py`、`main_b32.py`、`main_b128.py`(不改动仓库源码)。
2. 每份注释掉两处 `env.render()`;将 `EPISODES` 统一设为同一值(建议 100~200,量力而行);分别把 [L126](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L126) 的 `batch_size` 改为 8、32、128。
3. 把 `plot_learning_history(total_rewards)` 一行替换为保存数据,便于事后合图:

```python
# 示例代码:替换原 plot 调用,落盘后统一画图
np.save(f'return_history_b{batch_size}.npy', np.array(total_rewards))
```

4. 依次运行三份脚本;然后合并绘图:

```python
# 示例代码
import numpy as np, matplotlib.pyplot as plt
for b in (8, 32, 128):
    h = np.load(f'return_history_b{b}.npy')
    plt.plot(np.arange(1, len(h)+1), h, label=f'batch={b}')
plt.xlabel('Episodes'); plt.ylabel('Total rewards'); plt.legend(); plt.show()
```

5. (可选,强烈建议)DQN 方差大:每档 batch 至少跑 2~3 个随机种子(改 [L25-L26](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/cartpole/main.py#L25-L26) 的 `np.random.seed` / `torch.manual_seed`),对曲线做平滑(如 10 回合滑动平均)后再比较。

### 5.3 需要观察的现象

- 三条曲线的**爬升速度**:多少回合后脱离随机水平(约 20 步上下)。
- 曲线的**抖动程度**:相邻回合回报的波动幅度。
- **后期上限**:是否逼近 500 天花板、何时达到。
- 若同时打印每回合耗时:batch=128 的每回合 wall-time 应明显更长。

### 5.4 预期结果与分析要点(待本地验证)

- **batch=8**:每次 replay 只平均 8 个样本的误差,梯度噪声大,曲线最抖;但每个环境步的 ε 只衰减 8 次,探索期最长,前期偶有更高峰值。训练慢在「震荡」。
- **batch=32(仓库默认)**:方差与成本的折中,曲线爬升较平稳。
- **batch=128**:梯度最平稳,但每个环境步的计算量约为 batch=8 的数倍(训练成为瓶颈),且 ε 约 8 个环境步就衰减到底、探索严重不足,可能早早陷入次优策略、爬升反而偏慢。
- 解释框架:批大小↔梯度方差↔稳定性是统计侧;批大小↔每步计算量是计算侧;批大小↔ε 衰减速率是本实现的耦合副作用(把 `_adjust_epsilon` 挪出样本循环即可解耦,可作为附加思考题)。

若本机没有可运行的 gym 环境,退化为源码阅读型实践:用 4.3.4 的示例脚本对比 batch=8/128 时单步损失的数量级波动(重复抽样多次,观察 loss 的方差),同样能得出「批越大梯度估计越稳」的结论。

## 6. 本讲小结

- 状态连续/高维时,Q 表不可行,改用神经网络 \( Q(s,a;\mathbf{w}) \) 做函数逼近;网络一次前向输出全部动作的 Q 值,`argmax` 即贪心动作,ε-greedy 与离策略更新的 Q-learning 骨架原样保留。
- 经验回放把 (s,a,r,s',done) 五元组存入 `deque(maxlen=2000)`、训练时 `random.sample` 随机抽批:打散时间相关性、复用旧样本,其合法性来自 Q-learning 的离策略性质;开工前先用 500 步随机策略预热缓冲区。
- `_learn` 的核心是 target_all 技巧:TD 目标 \( y = r + \gamma\max Q(s') \)(终止则 y=r)只写进被采取动作那一维、其余维保持网络当前输出,MSE 因此只惩罚该维;32 个转移堆成一个 batch 走一次五步梯度更新,方差小、吞吐高。
- 目标网络通过冻结副本 + 周期同步切断「目标随参数漂移」的反馈回路,是标准 DQN 的稳定性支柱——但仓库 `main.py` 未实现,自举项与回归项共用同一个 `self.model`,属于简化实现(4.4 给出了补全示例)。
- CartPole 奖励按步发放(+1/步),总回报即存活步数,监控只需每回合记录步数;注意 `total_rewards.append(i)` 比真实回报小 1、曲线天花板 499、ε 衰减挂在样本循环内导致探索期极短这三个实现细节。
- 运行依赖需注意:environment.yml 不含 gym,需手动装 gym 0.20/0.21;新版 gymnasium 的 `reset`/`render` API 与书代码不兼容。

## 7. 下一步学习建议

本讲是第 19 章也是全书代码之旅的收官。建议三条延伸路线:

1. **补全 DQN**:按 4.4 的示例给仓库版加上目标网络,再进一步了解 Double DQN(自举动作由在线网络选、估值由目标网络算,缓解 max 算子的高估偏差)与优先经验回放(Prioritized Experience Replay)——它们都是对本讲两个组件的小步改造,非常适合作为练手项目。
2. **换环境**:把 `gym.make('CartPole-v1')` 换成 `Acrobot-v1` 或 `LunarLander-v2`,验证 `DQNAgent` 靠 `observation_space`/`action_space` 自动适配的设计;同时迁移到现代 `gymnasium` API(处理 `reset` 元组返回值与 `render_mode`),让老代码活在 2021 年之后的生态里。
3. **回望全书**:从 ch02 的感知机到本讲的 DQN,贯穿始终的只有几件事——一个参数化模型、一个可导的损失、一个沿梯度更新的循环,以及「先直觉、再源码」的阅读方法。可以重读 [ch19/ch19.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch19/ch19.ipynb) 末尾的 Chapter and book summary,并对照 ERRATA 目录核对书与本讲义的差异。
