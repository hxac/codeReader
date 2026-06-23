# 遗传算法

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚**遗传算法（Genetic Algorithm, GA）**属于哪一类 AI 范式，以及它和前面讲过的梯度下降（见 u1-l4）、神经网络有什么根本区别。
- 复述实现一个遗传算法必须自备的「四件套」：**基因编码、适应度函数、交叉算子、变异算子**。
- 读懂课程 `Genetic.ipynb` 中的两个完整例子——公平分宝藏与 N 皇后问题——并能指出它们的基因编码、适应度、交叉、变异分别是怎么写的。
- 理解**种群规模、进化步数、交叉/变异概率、选择压力**等参数如何影响收敛速度与是否陷入局部最优。
- 动手用遗传算法求解一个组合优化问题（丢番图方程）。

## 2. 前置知识

在进入遗传算法之前，先回顾两个关键背景。

**第一个背景：优化问题与「梯度下降」的局限。** 在 u1-l4《从 examples 开始：第一个 AI 程序》里，模型靠「预测 ŷ、算误差、按误差更新权重」来学习，更新公式形如 \(w \leftarrow w + \eta \cdot \text{error} \cdot x\)。这是一种**基于梯度的优化（gradient-based optimization）**：它依赖误差随权重「平滑可导」这一前提，沿着下坡方向一步步走。

但很多现实问题的解空间是**离散的、不可导的**，例如：

- 把一堆钻石分成价值尽量相等的两堆（每个钻石要么在这堆要么在那堆，是 0/1 选择，不可导）；
- 在棋盘上摆皇后使互不攻击（解是排列，连续移动没有「梯度」可言）；
- 排课表、装箱、下料等组合优化问题。

对这类问题，梯度下降无从下手，遗传算法提供了一条**不依赖导数的优化思路**：不沿着某个方向「走」，而是维护一**群**候选解，靠「随机变化 + 优胜劣汰」让整体慢慢变好。

**第二个背景：进化论直觉。** GA 由 John Henry Holland 于 1975 年提出，模拟生物进化：一群个体（候选解）在环境中竞争，适应度高（更接近目标）的个体有更大机会把自己的「基因」传给下一代；通过**交叉（父母基因重组）**和**变异（随机小改动）**产生新个体。多代之后，种群整体适应度提高。

> 术语提示：在标准遗传算法文献里，「适应度高」通常意味着「更好」，目标是**最大化**。但本课程为了统一，把适应度函数定义为「**值越小越好**」，即我们要**最小化** fit。这一点在阅读源码时非常重要——下面所有 `if fit(g) < fit(...)` 的判断都是「新个体比旧的更好就替换」。

把这两点合起来一句话：**遗传算法 = 在离散解空间上做「种群 + 选择 + 交叉 + 变异」的免导数优化。**

## 3. 本讲源码地图

本讲涉及的关键文件都在课程第 6 单元「其他 AI 技术」下：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/README.md) | 课程讲义，给出 GA 的思想、四件套定义、伪代码、典型应用与本课作业。 |
| [Genetic.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/Genetic.ipynb) | 核心可执行 Notebook，含两个完整例子：公平分宝藏（Problem 1）、N 皇后（Problem 2）。 |
| [Diophantine.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/Diophantine.ipynb) | 作业起点，要求用 GA 求解丢番图方程 \(a+2b+3c+4d=30\)。 |

> 说明：本讲引用 `.ipynb` 的行号是 Notebook 源文件（JSON）的行号，与 GitHub 上 `#L` 锚点一致。

阅读顺序建议：先读 README 建立概念框架 → 再逐 cell 跑 `Genetic.ipynb` 的 Problem 1 → 跑 Problem 2 → 最后做 Diophantine 作业。

## 4. 核心概念与源码讲解

本讲对应三个最小模块：**种群与适应度**、**交叉与变异**、**收敛与参数**。

### 4.1 种群与适应度

#### 4.1.1 概念说明

要实现一个遗传算法，README 明确告诉你必须先备齐四样东西：

- **基因（gene）** \(g \in \Gamma\)：一种把「问题的一个候选解」编码成可操作数据结构的方式。常用编码是**数值序列**或**位向量（bit vector）**。
- **适应度函数（fitness function）** \(\mathrm{fit}: \Gamma \to \mathbb{R}\)：给每个基因打分。本课程约定**值越小越好**。
- **交叉（crossover）** \(\Gamma^2 \to \Gamma\)：把两个父代基因重组出一个新的合法子代基因。
- **变异（mutation）** \(\Gamma \to \Gamma\)：对一个基因做随机小改动。

README 中的对应原文：[README.md:L7-L21](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/README.md#L7-L21)（四条核心思想与四件套定义）。

**种群（population）** \(G \subset \Gamma\) 就是「同时手里拿着的一批候选解」。与梯度下降「只维护一个当前最优解、沿着梯度走」不同，GA 同时养着一群解，让它们互相竞争、交配、变异，从而在解空间里**多点并行探索**，降低陷入单个局部最优的概率。

#### 4.1.2 核心流程

GA 的整体骨架（README 给出的伪代码）：

1. 选一个**初始种群** \(G \subset \Gamma\)（通常随机生成若干个基因）。
2. 随机决定本步做**交叉**还是**变异**。
3. 交叉：随机挑两个基因 \(g_1, g_2\)，算 \(g = \mathrm{crossover}(g_1, g_2)\)；若 \(\mathrm{fit}(g) < \mathrm{fit}(g_1)\) 或 \(\mathrm{fit}(g) < \mathrm{fit}(g_2)\)，就用 \(g\) 替换掉对应父代。
4. 变异：随机挑一个基因 \(g\)，用 \(\mathrm{mutate}(g)\) 替换它。
5. 回到第 2 步，直到 fit 足够小或达到步数上限。

伪代码见 [README.md:L23-L32](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/README.md#L23-L32)。

#### 4.1.3 源码精读

看 Problem 1「公平分宝藏」如何落地「基因 + 适应度」。

**问题定义**：有一组数 \(S\)，要把它分成两个子集 \(S_1, S_2\)，使两边之和的差尽量小：

\[
\left|\sum_{i\in S_1} i - \sum_{j\in S_2} j\right| \to \min
\]

Notebook 用 \(N=200\) 颗钻石、价格在 1–10000 之间随机生成数据（[Genetic.ipynb:L114-L116](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/Genetic.ipynb#L114-L116)）。

**基因编码**：用一个长度为 \(N\) 的二值向量 \(B \in \{0,1\}^N\)，第 \(i\) 位为 1 表示第 \(i\) 个数分进 \(S_1\)，为 0 则分进 \(S_2\)。`generate` 函数随机生成这样的二值向量：

```python
def generate(S):
    return np.array([random.randint(0,1) for _ in S])
```

见 [Genetic.ipynb:L147-L151](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/Genetic.ipynb#L147-L151)：随机生成一个 0/1 向量作为「一种分法」的基因。

**适应度函数**：直接把「两边之和的差」当作代价：

```python
def fit(B,S=S):
    c1 = (B*S).sum()
    c2 = ((1-B)*S).sum()
    return abs(c1-c2)
```

见 [Genetic.ipynb:L180-L185](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/Genetic.ipynb#L180-L185)。`B*S` 用掩码取出分进 \(S_1\) 的数求和，`(1-B)*S` 取出分进 \(S_2\) 的数求和，取绝对差。差越小分得越公平，故 fit 越小越好。

**初始种群**：把 `generate` 重复 `pop_size` 次，得到一批候选解：

```python
pop_size = 30
P = [generate(S) for _ in range(pop_size)]
```

见 [Genetic.ipynb:L231-L232](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/Genetic.ipynb#L231-L232)。这就是「同时养 30 个不同的分法」。

注意：随机生成的初始分法往往很差，Notebook 里第一个 `fit(b)` 算出来约 13 万；GA 的任务就是把这 13 万一步步压下去。

#### 4.1.4 代码实践

**实践目标**：亲手感受「基因 + 适应度」这两件套，理解 fit 越小越好。

**操作步骤**：

1. 打开 `lessons/6-Other/21-GeneticAlgorithms/Genetic.ipynb`，选好 `ai4beg` 内核（见 u1-l3）。
2. 依次运行前几个 cell：导入库 → 生成集合 \(S\)（`N=200`）→ `generate` 出一个随机基因 `b` → 调用 `fit(b)`（[Genetic.ipynb:L180](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/Genetic.ipynb#L180)）。
3. 新建一个 cell，构造一个「全 0」基因并打分：

```python
# 示例代码（非 Notebook 原有，便于观察）
b_all0 = np.zeros_like(b)
print(fit(b_all0))
```

**需要观察的现象**：随机分法的 fit 通常很大（数万量级），且每次重跑结果差别很大；而「全 0」基因的 fit 应等于 `sum(S)`（所有钻石都进了 \(S_2\)，差就是总价）。

**预期结果**：随机基因 fit 在几万到十几万之间波动；`fit(b_all0) == sum(S)`。这验证了你对适应度函数的理解——初始种群里几乎没有「好解」，全靠后续进化。

> 待本地验证：由于 `S` 是随机生成的，你看到的数值会与本讲引用的 133784 不同，但量级一致即可；若无法运行 Notebook，可纯纸笔推演——当 \(B\) 全为 0 时 \(c_1=0\)，fit 即 `sum(S)`。

#### 4.1.5 小练习与答案

**练习 1**：如果把财宝平分的 `fit` 改成 `c1 - c2`（去掉绝对值），会发生什么？

**参考答案**：差可能为负，且把所有钻石都塞进 \(S_2\)（\(c_1=0\)）会得到一个很大的负值，被算法误判为「最优」。绝对值保证「差为 0」才是真正的最优，与正负无关。

**练习 2**：为什么本课约定「fit 越小越好」，而不是像很多教材那样「越大越好」？

**参考答案**：本课把 fit 当作「代价/误差」（如分宝藏的两边之差、皇后互相攻击的对数），代价自然是越小越优；这样 GA 的目标统一为「最小化」，和前面课程里「最小化损失函数」的口径一致，便于对照理解。

---

### 4.2 交叉与变异

#### 4.2.1 概念说明

有了基因和适应度，还需要两种「制造新个体」的算子：

- **变异（mutation）**：对单个基因做一处随机小改动。它的作用是**注入新基因、扰动搜索**，把种群从局部最优里「踢」出去，避免全部个体越来越像、失去多样性。
- **交叉（crossover）**：取两个父代基因各一部分拼成一个子代基因。它的作用是**组合两个解各自的好特征**，让种群朝更优区域集中。这是 GA 区别于「纯随机搜索」的关键——好基因片段能在个体间传播、累积。

README 对两者的描述见 [README.md:L7-L21](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/README.md#L7-L21)：「Crossover allows us to combine two solutions together」「Mutations are introduced to destabilize optimization and get us out of the local minimum」。

#### 4.2.2 核心流程

两种算子的最朴素实现（以二值向量为例）：

- **变异**：随机选一个位置，把该位的 0 变 1、1 变 0（取反）。
- **交叉**：再生成一个随机 0/1 掩码 \(x\)，子代每位「按掩码从父代 1 或父代 2 取」，即 \(b_1 \odot x + b_2 \odot (1-x)\)（\(\odot\) 表逐位乘）。这叫**均匀交叉（uniform crossover）**。

交叉的数学形式可写成逐位选择：

\[
\text{child}_i = \begin{cases} (b_1)_i & x_i = 1 \\ (b_2)_i & x_i = 0 \end{cases},\quad x_i \in \{0,1\}\ \text{随机}
\]

#### 4.2.3 源码精读

Problem 1 的 `mutate` 与 `xover`：

```python
def mutate(b):
    x = b.copy()
    i = random.randint(0,len(b)-1)
    x[i] = 1-x[i]
    return x

def xover(b1,b2):
    x = generate(b1)
    return b1*x+b2*(1-x)
```

见 [Genetic.ipynb:L205-L213](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/Genetic.ipynb#L205-L213)。`mutate` 先 `copy` 再取反一位（不污染原基因）；`xover` 复用 `generate` 造一个随机掩码，做逐位混合的均匀交叉。

对比 Problem 2「N 皇后」用了一套**不同的编码与算子**，正好说明「同一套 GA 框架、按问题定制」的思路：

- **编码**：用长度为 \(N\) 的列表 \(L\)，第 \(i\) 个数表示第 \(i\) 行皇后所在的列号（1~N）。
- **适应度** `fit(L)`：统计有多少对皇后互相攻击（同列或同对角线），值越小越好：[Genetic.ipynb:L437-L443](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/Genetic.ipynb#L437-L443)。
- **变异**：随机挑一位、换成 1~N 的随机列号。
- **交叉**：**单点交叉（single-point crossover）**——随机选一个切点，前半段取自父代 1、后半段取自父代 2。

```python
def mutate(G):
    x=random.randint(0,len(G)-1)
    G[x]=random.randint(1,len(G))
    return G

def xover(G1,G2):
    x=random.randint(0,len(G1))
    return np.concatenate((G1[:x],G2[x:]))
```

见 [Genetic.ipynb:L513-L522](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/Genetic.ipynb#L513-L522)。两种交叉（均匀 vs 单点）都合法，选哪种取决于问题编码——这说明交叉/变异没有唯一正确写法，是 GA 实现中最需要「按问题设计」的部分。

两种算子对照：

| 维度 | 公平分宝藏（位向量） | N 皇后（列表） |
| --- | --- | --- |
| 交叉方式 | 均匀（逐位随机取） | 单点（一刀两段拼接） |
| 变异方式 | 翻转一位 | 改写一个元素的值 |
| 是否恒为合法解 | 是（0/1 总合法） | 不一定（可能产生重复列） |

#### 4.2.4 代码实践

**实践目标**：手动调用 `mutate` 和 `xover`，直观看到「变异改一处、交叉拼两半」，并发现「合法解」的差异。

**操作步骤**：

1. 在 N 皇后区域运行 `xover` 的演示 cell（[Genetic.ipynb:L522](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/Genetic.ipynb#L522) 调用 `xover([1,2,3,4],[5,6,7,8])`），多跑几次，观察结果形如 `[1,2,7,8]`。
2. 构造一个会暴露「重复列」问题的例子并打分：

```python
# 示例代码（非 Notebook 原有）
child = xover(np.array([1,2,3,4,5]), np.array([5,4,3,2,1]))
print(child, fit(child))
```

3. 回到公平分宝藏，对任意 `b` 调用 `mutate(b)`，用 `(b!=mutate(b)).sum()` 验证「恰好只改了一位」。

**需要观察的现象**：`xover` 的结果每位随机来自 `b1` 或 `b2`（公平分宝藏）或前半/后半拼接（N 皇后）；N 皇后的 `child` 可能出现重复元素导致 `fit` 不为 0——但没关系，适应度会因「同列攻击」给它高分从而淘汰它。

**预期结果**：理解「交叉 = 父代基因重组、变异 = 单点随机扰动」；`(b!=mutate(b)).sum() == 1` 恒成立。

> 待本地验证：含随机性，多次输出不同；若无法运行，结论可由代码直接读出。

#### 4.2.5 小练习与答案

**练习 1**：Problem 2 的 `mutate` 直接修改了传入的 `G`（没有 `copy`），而 Problem 1 的 `mutate` 用了 `b.copy()`。这会带来什么隐患？

**参考答案**：Problem 2 的 `mutate` 是**就地修改（in-place）**，会改动种群里原有的基因；如果在交叉产物上调用了它却没意识到这一点，可能污染父代。Problem 1 用 `copy` 更安全。这是 GA 实现中常见的副作用坑。

**练习 2**：对 N 皇后这种「每个列号 1~N 各出现一次」的排列解，单点交叉得到的子代可能「列号重复」从而不是合法排列。Notebook 为什么仍然允许这样做？

**参考答案**：因为它的适应度函数 `fit(L)` 直接统计「互相攻击的对数」，列号重复只会让某些皇后同列攻击，从而 fit 变大、被自然淘汰。GA 不要求每个中间个体都合法，只要适应度能衡量好坏、最终能收敛到合法解即可（合法解即 fit=0）。

---

### 4.3 收敛与参数

#### 4.3.1 概念说明

把种群、适应度、交叉、变异装进一个「进化主循环」，就得到完整的 GA。本课在 Notebook 里给出了**两种不同风格的实现**，值得对照：

- **Problem 1（`evolve`）**：**稳态式（steady-state）**。每步只改动种群里的一个个体——做交叉时只在「子代确实更优」时才替换较差的那个父代；做变异时用变异体替换当前最差个体。种群大小基本恒定，好坏解长期共存。
- **Problem 2（`genetic`）**：**世代式（generational）**。每代「淘汰最差的 1/3 → 补几个随机新个体 → 按适应度比例选父母交叉出整整一代新种群」，整批替换。它还引入了**适应度比例选择（fitness-proportional selection / 轮盘赌）**：越好的个体被选作父母的概率越大。

「同一套 GA 思想、多种工程实现」正是 README 所说「具体实现因问题而异」的体现。两种实现都能收敛到 fit=0。

控制收敛的关键参数：

| 参数 | 作用 | 调大的影响 |
| --- | --- | --- |
| `pop_size`（种群大小） | 每代个体数量，决定多样性 | 多样性好、更抗局部最优，但每代更慢 |
| 变异/交叉概率 | 每步做变异还是交叉 | 变异概率过高 → 接近随机搜索；过低 → 跳不出局部最优 |
| 步数上限 `n` | 进化多少代 | 给算法更多收敛时间，但可能徒增开销 |

README 里有一句点睛之笔：「Genetic algorithms are simple to implement, but their behavior is difficult to understand.」（[README.md:L58](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/README.md#L58)）——这正是参数调优难的写照。

#### 4.3.2 核心流程

**Problem 1 的 `evolve` 主循环**（每步 30% 概率变异、70% 概率交叉）：

1. 记录当前种群最小 fit；
2. 若已达 0 则提前结束；
3. 否则按概率走变异或交叉分支，用更优的新个体替换种群里的旧个体；
4. 循环 `n` 步，最后返回种群里最好的基因与每步最小 fit 的历史。

**Problem 2 的世代式循环**：循环到 fit=0 为止，每代调用 `nxgeneration`：先 `discard_unfit` 砍掉最差 1/3、补随机个体，再 `choose_rand` 按权重选父母、`xover` 交叉、按 `mutation_prob` 概率 `mutate`，凑满新一代。

收敛性方面，GA 没有梯度下降那样的理论保证，但有两个经验规律：

- **选择压力过强**（只留最好的几个）→ 种群迅速趋同、失去多样性 → **早熟收敛**，卡在局部最优。
- **变异概率过大** → 接近随机搜索，收敛慢；**过小** → 跳不出局部最优。典型取值在 0.01~0.1。

适应度比例选择（轮盘赌）把「越小越好」的 fit 翻转成「越大越好的权重」：

\[
w_i=\frac{mf-\mathrm{fit}_i}{\sum_{j}(mf-\mathrm{fit}_j)},\qquad mf=\frac{N(N-1)}{2}
\]

其中 \(mf\) 是「最坏适应度」（所有皇后两两攻击的对数），fit 越小则 \(mf-\mathrm{fit}\) 越大、被选中概率越高。

#### 4.3.3 源码精读

`evolve` 完整实现：

```python
def evolve(P,S=S,n=2000):
    res = []
    for _ in range(n):
        f = min([fit(b) for b in P])
        res.append(f)
        if f==0:
            break
        if random.randint(1,10)<3:           # 30% 概率：变异
            i = random.randint(0,len(P)-1)
            b = mutate(P[i])
            i = np.argmax([fit(z) for z in P]) # 找最差个体替换掉
            P[i] = b
        else:                                  # 70% 概率：交叉
            i = random.randint(0,len(P)-1)
            j = random.randint(0,len(P)-1)
            b = xover(P[i],P[j])
            if fit(b)<fit(P[i]):               # 子代更优才替换父代
                P[i]=b
            elif fit(b)<fit(P[j]):
                P[j]=b
    i = np.argmin([fit(b) for b in P])
    return (P[i],res)
```

见 [Genetic.ipynb:L267-L293](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/Genetic.ipynb#L267-L293)。几个关键点：`res` 记录每步最小 fit 用于画收敛曲线；`random.randint(1,10)<3` 即 30% 变异概率；变异分支用 `np.argmax` 定位最差个体（因为 fit 越大越差）替换，相当于「淘汰最弱」；交叉分支只在子代更优时替换较差父代（精英保留式替换）。收敛曲线绘制见 [Genetic.ipynb:L324-L325](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/Genetic.ipynb#L324-L325) 的 `plt.plot(hist)`，Notebook 把 fit 从 13 万一路压到 4，已非常接近完美平分。

Problem 2 的轮盘赌选择 `choose_rand`：

```python
def choose_rand(P):
    N=len(P[0][0])
    mf = N*(N-1)//2                       # 最大可能的攻击对数
    z = [mf-x[1] for x in P]              # 越好权重越大
    tf = sum(z)
    w = [x/tf for x in z]                 # 归一化为概率
    p = np.random.choice(len(P),2,False,p=w)
    return p[0],p[1]
```

见 [Genetic.ipynb:L540-L547](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/Genetic.ipynb#L540-L547)。这就是「轮盘赌选择」——好个体的扇区更大、更易被选中；`np.random.choice` 的 `False` 表示无放回，保证两个父母不同。

Problem 2 的世代式核心：

```python
mutation_prob = 0.1

def discard_unfit(P):              # 只保留适应度最好的 1/3
    P.sort(key=lambda x:x[1])
    return P[:len(P)//3]

def nxgeneration(P):
    gen_size=len(P)
    P = discard_unfit(P)
    P.extend(generate(len(P[0][0]),3))   # 补几个随机新个体维持多样性
    new_gen = []
    for _ in range(gen_size):
        p1,p2 = choose_rand(P)           # 按适应度比例选父母
        n = xover(P[p1][0],P[p2][0])
        if random.random()<mutation_prob:
            n=mutate(n)
        nf = fit(n)
        new_gen.append((n,nf))
    return new_gen

def genetic(N,pop_size=100):
    P = generate(N,pop_size)
    mf = min([x[1] for x in P])
    n=0
    while mf>0:                           # 收敛判据：fit=0
        n+=1
        mf = min([x[1] for x in P])
        P = nxgeneration(P)
    mi = np.argmin([x[1] for x in P])
    return P[mi]
```

见 [Genetic.ipynb:L601-L641](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/Genetic.ipynb#L601-L641)。其中每个个体存成 `(基因, fit值)` 元组以避免反复重算 fit（注释里提到「calculating fitness function is time consuming」）。`discard_unfit` 是精英保留（排序后取前 1/3）；`mutation_prob=0.1` 即每个新生子代有 10% 概率被变异；`genetic(8)` 会返回一个合法的八皇后解（fit=0）。

**GA vs 暴力搜索**：Notebook 先用回溯 `nqueens([],20,False)` 全量搜 20 皇后并 `%timeit` 计时（[Genetic.ipynb:L417](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/Genetic.ipynb#L417)），再用 `%timeit genetic(10)` 计时 GA（[Genetic.ipynb:L668](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/Genetic.ipynb#L668)）。全量搜索保证找到解但随 \(N\) 指数爆炸，GA 不保证最优却通常快得多——这是 README「Speeding up exhaustive search」的实证。

#### 4.3.4 代码实践

**实践目标**：本讲的核心实践——修改「种群大小」与「变异率」，观察收敛速度变化。

**操作步骤**：

1. 先完整跑通 Problem 1，得到基准收敛曲线 `hist` 与最终 fit。
2. **改种群大小**：在不同 cell 用不同 `pop_size` 生成初始种群并对比曲线：

```python
# 示例代码（基于 Notebook 既有函数）
for pop_size in [10, 30, 100]:
    P = [generate(S) for _ in range(pop_size)]
    _, hist = evolve(P, n=2000)
    plt.plot(hist, label=f'pop={pop_size}')
plt.legend(); plt.xlabel('step'); plt.ylabel('best fit')
```

3. **改变异/交叉比例**：复制 `evolve` 改名为 `evolve_v2`，把 `random.randint(1,10)<3`（30% 变异）分别改成 `<1`（10% 变异）和 `<8`（80% 变异），固定 `pop_size=30`，比较收敛曲线。
4.（Problem 2 方向）把 `mutation_prob` 从 0.1 改成 0.01 与 0.5，重跑 `genetic(8)`，统计平均需要多少代才收敛。

**需要观察的现象**：

- 种群太小（如 10）→ 多样性不足、易早熟，最终 fit 卡在较大值；种群太大（如 100）→ 单步更慢但曲线更稳。
- 变异率太低 → 曲线早早趋平却没到 0（卡局部最优）；变异率太高 → 曲线抖动剧烈、像随机搜索。

**预期结果**：存在一组「够用的」参数（如 `pop_size=30~100`、变异率 0.05~0.1），能在合理步数内把 fit 压到接近 0。这正是 GA「调参」的核心经验。

> 待本地验证：因含随机性，多次运行的代数/最终 fit 会波动；建议每个参数组合跑 5 次取平均再比较。

#### 4.3.5 小练习与答案

**练习 1**：`evolve` 的变异分支用 `np.argmax` 找最差个体替换，而交叉分支只在子代更优时替换较差父代。这两种「替换策略」分别体现了什么思想？

**参考答案**：变异分支「淘汰最差」保证种群下限不断提高；交叉分支「子代更优才替换父代」是精英保留，避免好基因被随机交叉破坏。两者都在「让种群整体变好」与「保留多样性」之间权衡。

**练习 2**：把 `mutation_prob` 设成 0 会发生什么？设成 1 又会怎样？

**参考答案**：设成 0（完全不变异）→ 种群只有交叉重组，一旦所有个体趋同就再无新基因，极易卡在局部最优；设成 1（每个子代都变异）→ 接近随机搜索，好基因难以稳定积累，收敛极慢甚至不收敛。可见变异率是「探索 vs 利用」的旋钮。

**练习 3**：为什么 Problem 2 要把每个个体存成 `(基因, fit值)` 元组，而 Problem 1 不存？

**参考答案**：Problem 2 的 `fit(L)` 是双层循环、计算很慢（\(O(N^2)\)），缓存 fit 值可避免每代重复计算；Problem 1 的 `fit(B)` 只是几次向量运算、很快，重算开销可接受，故直接每次现算。

---

## 5. 综合实践

**任务**：用遗传算法求解课程作业里的丢番图方程。

> 题目见 [README.md:L66-L77](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/README.md#L66-L77)，起点 Notebook 为 `Diophantine.ipynb`（方程定义见 [Diophantine.ipynb:L7-L18](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/Diophantine.ipynb#L7-L18)）。

方程：\(a + 2b + 3c + 4d = 30\)，要求找**整数根**。提示把每个根限制在 \([0,30]\)，并以「四个根的列表」作为基因。

**要求你把本讲三个模块串起来，独立设计 GA 的四件套**：

1. **基因编码**：\([a,b,c,d]\)，每个分量是 0~30 的随机整数。
2. **适应度函数**：\(\mathrm{fit}(g) = |a + 2b + 3c + 4d - 30|\)，越小越好（注意系数 2、3、4 要乘进去）。
3. **交叉**：可仿照 Problem 2 的单点交叉，在四个分量中选一切点拼接；或仿照 Problem 1 的均匀交叉。
4. **变异**：随机挑一个分量，重新赋一个 0~30 的随机整数。
5. **主循环**：可复用 `evolve` 的稳态式骨架——每步按概率选交叉或变异，更优才替换；记录每步最小 fit，直到 fit=0 或达步数上限。

**参考骨架代码（示例代码，需自行补全主循环）**：

```python
import numpy as np, random

COEF = np.array([1,2,3,4])
TARGET = 30

def fit(g):                       # 适应度：与目标之差的绝对值
    return abs((COEF*g).sum() - TARGET)

def generate_one():               # 一条随机基因
    return np.array([random.randint(0,30) for _ in range(4)])

def mutate(g):                    # 随机改一个分量（先 copy 避免污染）
    x = g.copy()
    x[random.randint(0,3)] = random.randint(0,30)
    return x

def xover(g1,g2):                 # 均匀交叉
    mask = np.array([random.randint(0,1) for _ in range(4)])
    return g1*mask + g2*(1-mask)
```

**需要观察的现象**：

- `fit` 应在若干步内归零，输出一个满足方程的根（如 `[a,b,c,d]` 使 \(a+2b+3c+4d=30\)）。
- 参数扫描（`pop_size ∈ {20, 50, 100}` × 变异率 `{0.1, 0.3}` 各跑 5 次取中位数）应复现 4.3.4 的趋势：种群偏小或变异率偏高时，归零所需步数更多、方差更大。
- 解不唯一——方程有多组整数根，每次跑可能得到不同的合法解，这本身就是 GA「随机性」的体现。

**预期结果**：能稳定输出一个 `fit==0` 的四元组；中等种群（约 50）+ 低变异率（0.1）通常最稳。若个别组合长时间不收敛，把它当作「早熟/震荡」的案例记录下来。

> 若环境无法运行 Notebook，标注「待本地验证」，但代码骨架与设计推导可直接由源码读出，不影响理解。这是「源码阅读 + 动手实现」型综合实践：你既要读懂 `Genetic.ipynb` 两个例子的写法，又要照葫芦画瓢为新问题定制四件套。

## 6. 本讲小结

- 遗传算法是一种**免导数（derivative-free）的进化优化**方法，专治离散、不可导的组合优化问题；它和 u1-l4 的梯度下降是两条不同的优化路线。
- 实现 GA 必须自备**四件套**：基因编码、适应度函数（本课约定越小越好）、交叉算子、变异算子。
- GA 维护**一整个种群**而非单个解，靠「多点并行探索」降低陷入局部最优的概率。
- **交叉**负责把好基因片段组合传播（利用），**变异**负责注入新基因、跳出局部最优（探索）；二者构成「探索 vs 利用」的核心张力。
- Notebook 给出两种实现风格——`evolve` 的**稳态式**与 `genetic` 的**世代式 + 轮盘赌选择**——证明同一思想可有多种工程实现。
- **种群大小、进化步数、交叉/变异概率、选择压力**是关键调参旋钮：变异率太低易早熟、太高近似随机搜索。

## 7. 下一步学习建议

- **横向对照**：回到 u1-l4，把「梯度下降更新权重」与「GA 选择+变异更新种群」并列，体会两类优化范式各自适合的问题类型。
- **下一讲 u5-l2《深度强化学习》**：README 末尾提到的「用遗传算法训练神经网络玩 Super Mario」是一条经典支线，而真正系统地学习「智能体通过奖励学习策略」要进入强化学习——下一讲将讲 Q-learning 与 DQN，可把 GA 的「适应度」与 RL 的「累积奖励」做对照。
- **延伸阅读**：README 推荐的视频讲计算机如何用「神经网络 + 遗传算法」学玩 Super Mario，可作为 GA 与神经网络结合的直观案例；Wikipedia「Genetic algorithm」词条对早熟收敛、模式定理等有更深入讨论。
- **继续阅读源码**：若对世代式实现感兴趣，可重读 `Genetic.ipynb` 中 `choose`（[L549-L565](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/6-Other/21-GeneticAlgorithms/Genetic.ipynb#L549-L565)）这一套用「拒绝采样」实现的轮盘赌选择，对照 `choose_rand` 的向量化版本，体会同一选择算子的两种写法。
