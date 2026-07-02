# kmeans2：多种初始化与空簇处理

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `scipy.cluster.vq.kmeans2` 与上一讲的 `kmeans` 在**停止条件、重启语义、返回值**上的关键区别。
- 掌握 `kmeans2` 的四种初始化方式 `minit`：`random` / `points` / `++` / `matrix`，以及它们各自落在哪个内部函数（`_krandinit` / `_kpoints` / `_kpp` / 直接用 `k`）。
- 理解 k-means++（`_kpp`）为什么用「D² 概率」逐步挑选种子，以及它为何通常比随机初始化更稳定。
- 看懂当某个簇在迭代中变空时，`kmeans2` 如何通过 `_missing_warn` / `_missing_raise` 处理，并把空簇的簇心**回退到上一轮位置**——这与 `kmeans`「直接丢弃空簇」的做法截然不同。

本讲承接 [u2-l3](u2-l3-kmeans-iteration.md)（`kmeans` 主流程与 `_kmeans` 引擎），把镜头从「单引擎、单一种子策略」切换到 `kmeans2` 的「固定迭代 + 多种初始化 + 空簇兜底」这一套设计。

## 2. 前置知识

### 2.1 为什么 k-means 对「初始化」极其敏感

k-means（Lloyd 算法）是一个**局部优化**算法：它从一个初始码本出发，反复执行「把每个点分配到最近簇心 → 用簇内均值更新簇心」，直到满足停止条件。它只能保证收敛到**局部最优**，而不一定是全局最优。

因此，**初始簇心放在哪里，基本决定了最终聚类的质量**。两种常见的「坏初始化」：

- 多个初始簇心挤在同一真实簇里 → 另一个真实簇没有簇心去认领，最终把一个簇硬拆成两个。
- 初始簇心落在数据稀疏区 → 该簇心始终吸引不到点，变成**空簇**。

`kmeans2` 正是为了给用户提供「多种初始化选择」而设计的：你可以用最朴素的随机采样（`random`）、随机挑真实点（`points`）、精心播种的 k-means++（`++`），或者完全自己指定（`matrix`）。

### 2.2 什么是「空簇」(empty cluster)

在一次「分配」步骤后，某个簇心 \(c_j\) 可能**一个点都没分到**。此时该簇没有成员，`update_cluster_means` 算不出均值（0/0）。这就是「空簇」。`kmeans2` 必须决定：

1. 是报警然后继续（`missing='warn'`），还是直接抛异常（`missing='raise'`）？
2. 空掉的簇心接下来放在哪里？

这两个问题的答案就是本讲第 4.4 节的内容。

### 2.3 与 `kmeans` 的快速对照

上一讲的 `kmeans` 与本讲的 `kmeans2` 是同一算法家族的两种封装，关键差异如下（先建立印象，源码细节后文展开）：

| 方面 | `kmeans`（u2-l3） | `kmeans2`（本讲） |
|---|---|---|
| 停止条件 | `thresh` 畸变收敛（变化量 ≤ 阈值即停） | 固定跑 `iter` 轮，**`thresh` 形同虚设** |
| `iter` 的含义 | 重启次数（多次随机初始化取最低畸变） | 真正的迭代轮数（单次运行） |
| 初始化方式 | 仅 `_kpoints`（随机挑点） | `minit` 四选一：`random` / `points` / `++` / `matrix` |
| 空簇处理 | 直接**丢弃**（码本缩小） | **保留** k 个簇心，回退到上一轮位置 + `warn`/`raise` |
| 返回值 | `(code_book, distortion)` | `(code_book, label)` |

记住这张表，本讲就是在逐行解释 `kmeans2` 这一列。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但其中的函数分工很清晰：

| 函数 | 位置（行） | 作用 |
|---|---|---|
| `kmeans2` | `vq/_vq_impl.py` 主入口 | 校验参数、按 `minit` 初始化、跑固定迭代、处理空簇、返回 `(code_book, label)` |
| `_valid_init_meth` | `vq/_vq_impl.py` | `minit` 字符串到初始化函数的字典 |
| `_kpoints` | `vq/_vq_impl.py` | `minit='points'`：无放回随机挑 k 个真实点 |
| `_krandinit` | `vq/_vq_impl.py` | `minit='random'`：按数据的均值/协方差拟合高斯后采样 |
| `_kpp` | `vq/_vq_impl.py` | `minit='++'`：k-means++ 的 D² 概率播种 |
| `_valid_miss_meth` | `vq/_vq_impl.py` | `missing` 字符串到空簇处理函数的字典 |
| `_missing_warn` | `vq/_vq_impl.py` | 空簇时发 `UserWarning` |
| `_missing_raise` | `vq/_vq_impl.py` | 空簇时抛 `ClusterError` |

> 说明：`vq`、`update_cluster_means` 已在 u2-l2 / u2-l3 讲过，本讲直接复用其结论——`vq` 负责「分配」、`update_cluster_means` 负责「求均值更新簇心」。

---

## 4. 核心概念与源码讲解

### 4.1 kmeans2 总体流程：固定迭代与「未使用的 thresh」

#### 4.1.1 概念说明

`kmeans2` 是一个**面向单次运行**的 k-means 封装：它跑**固定 `iter` 轮**「分配—更新」循环，不提前停。和 `kmeans` 的 `_kmeans` 引擎（用 `deque(maxlen=2)` 比较前后畸变、变化量 ≤ `thresh` 即停）不同，`kmeans2` 的 `thresh` 参数在文档里明确写着「(not used yet)」——它**目前完全不参与逻辑**。这是一个容易踩坑的点：你以为调小 `thresh` 能让它早点停，其实没用。

#### 4.1.2 核心流程

`kmeans2(data, k, iter=10, thresh=1e-5, minit='random', missing='warn', *, rng=None)` 的执行过程可以概括为：

```
1. 校验：iter >= 1；missing 必须是 'warn'/'raise'
2. 确定 nc（簇数）与初始 code_book：
     - 若 minit=='matrix' 或 k 是数组  -> 直接把 k 当初始码本，nc = k.shape[0]
     - 否则 nc = int(k)，按 minit 字符串分发到 _kpoints/_krandinit/_kpp 生成初始码本
3. for _ in range(iter):                       # 固定轮数，不提前停
       label = vq(data, code_book)[0]          # 分配：每个点找最近簇心
       new_cb, has_members = update_cluster_means(data, label, nc)  # 更新簇心
       if 存在空簇:
           missing 方法 warn 或 raise
           new_cb[空簇] = code_book[空簇]       # 空簇心回退到上一轮位置
       code_book = new_cb
4. 返回 (code_book, label)
```

注意第 3 步里 `label` 是在「更新簇心**之前**」算出来的，所以函数返回的 `label` 对应的是**倒数第二轮的码本**，与最终 `code_book` 并非严格自洽（见 4.1.4 的实践）。

#### 4.1.3 源码精读

函数签名与装饰器（注意 `thresh` 在签名里但后面不使用）：

[`vq/_vq_impl.py:593-596`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L593-L596) —— `@xp_capabilities(...)` 声明 cpu_only / 不可 jax_jit；`@_transition_to_rng("seed")` 让旧的 `seed=` 参数平滑迁移到新的 `rng=`。`thresh=1e-5` 出现在签名中，但下面看不到它进入任何判断。

文档里对 `thresh` 的说明（关键证据，证明它确实没用）：

[`vq/_vq_impl.py:618-619`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L618-L619) ——「thresh : float, optional (not used yet)」。

主循环（固定 `iter` 轮，没有 `while diff > thresh`）：

[`vq/_vq_impl.py:766-777`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L766-L777) —— 每轮先 `vq` 分配得到 `label`，再 `update_cluster_means` 得到新码本与 `has_members` 布尔数组；若有空簇就调用 `miss_meth()` 并把空簇心回退；最后把 `new_code_book` 赋回 `code_book`。返回的是最终的 `code_book` 和**最后一次 `vq` 得到的** `label`。

对比 `_kmeans` 引擎的循环（来自 u2-l3）：

[`vq/_vq_impl.py:260-270`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L260-L270) —— 那里是 `while diff > thresh:`，用 `deque(maxlen=2)` 比较 `prev_avg_dists[0]` 与 `[1]`。`kmeans2` 里没有这一套，所以 `thresh` 自然用不上。

#### 4.1.4 代码实践：观察 thresh 失效与 label 轻微滞后

**实践目标**：验证两点——(a) 改 `thresh` 不影响结果；(b) 返回的 `label` 与最终 `code_book` 可能不严格自洽。

**操作步骤**（示例代码，可直接粘贴运行）：

```python
import numpy as np
from scipy.cluster.vq import kmeans2

rng = np.random.default_rng(0)
data = rng.standard_normal(size=(200, 2))

cbA, labA = kmeans2(data, 3, minit='points', seed=5, iter=10, thresh=1e-5)
cbB, labB = kmeans2(data, 3, minit='points', seed=5, iter=10, thresh=1e9)  # 阈值放大 1e14 倍
print("thresh 不影响结果：", np.array_equal(cbA, cbB), np.array_equal(labA, labB))

# 检查 label 是否仍是「最终 code_book 下」的最近簇心
nearest = np.linalg.norm(data[:, None, :] - cbA[None, :, :], axis=-1).argmin(axis=1)
print("label 与最终码本下的最近簇一致的比例：", np.mean(labA == nearest))
```

**需要观察的现象 / 预期结果**：

- 第一个打印应为 `True`——`thresh` 改了 1e14 倍结果完全相同，证明它确实没参与逻辑。
- 第二个比例**通常很高但未必是 100%**，因为 `label` 是用「上一轮码本」分配的、而 `code_book` 之后又更新了一次。这就是「label 轻微滞后」现象。具体数值**待本地验证**，但其小于 100% 是由源码 766–775 行的顺序决定的。

#### 4.1.5 小练习与答案

**练习 1**：既然 `thresh` 没用，想让 `kmeans2`「多迭代直到真正收敛」该怎么办？
**答案**：把 `iter` 调大（比如 50、100）。`kmeans2` 靠固定轮数保证终止，不靠畸变阈值；要近似收敛就只能多跑几轮，或改用会自动判断收敛的 `kmeans`。

**练习 2**：为什么 `kmeans2` 选择「固定轮数」而不是「畸变收敛」？
**答案**：固定轮数让运行时间可预测（`iter` 轮，每轮两次 O(M·k·N)），适合作为更大流水线里的一个确定步骤；而 `kmeans` 的收敛判定需要额外维护历史畸变（`deque`）并在每轮比较，语义更接近「跑到稳定」。

---

### 4.2 minit 初始化分发：_valid_init_meth 与四种策略

#### 4.2.1 概念说明

`minit` 是 `kmeans2` 区别于 `kmeans` 最直观的参数：它决定「初始 k 个簇心从哪儿来」。四种取值：

- `'points'`：从数据里**无放回**随机挑 k 个真实点当簇心（和 `kmeans` 用的 `_kpoints` 是同一个函数）。
- `'random'`：用数据的均值和协方差拟合一个高斯，从高斯里采样 k 个点（这些点**不一定是真实数据点**，可能落在数据稀疏区）。
- `'++'`：k-means++ 的 D² 概率播种，逐步、有意识地「把种子撒开」（见 4.3 节）。
- `'matrix'`：把 `k` 参数本身当作初始码本数组，跳过任何随机初始化。

注意 `'matrix'` 并**没有**对应的初始化函数，它是一个特殊的「旁路」：当你把 `k` 传成数组时，`kmeans2` 直接用你给的数组当码本。

#### 4.2.2 核心流程：分发的两个分支

分发逻辑藏在一段 `if ... else ...` 里，关键是判断「`k` 到底是簇数还是数组」：

```
若 minit=='matrix' 或 k 是数组（xp_size(code_book) > 1）:
    校验 k 与 data 的形状一致
    nc = k.shape[0]                  # 簇数 = 数组行数
    code_book = k                    # 直接用，不做任何初始化
否则:
    nc = int(k)                      # k 是簇数
    init_meth = _valid_init_meth[minit]   # 查表
    code_book = init_meth(data, k, rng, xp)  # 调用对应初始化函数
```

`_valid_init_meth` 就是把字符串映射到函数的字典：

```python
_valid_init_meth = {'random': _krandinit, 'points': _kpoints, '++': _kpp}
```

注意它**只有三个键**——`'matrix'` 不在里面，因为 `'matrix'` 走的是上面那条「直接用 k」的旁路，根本不查这张表。

#### 4.2.3 源码精读

分发主体（同时确定 `nc` 与 `code_book`）：

[`vq/_vq_impl.py:740-762`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L740-L762) —— `if minit == 'matrix' or xp_size(code_book) > 1:` 走「直接当码本」分支（校验秩与特征维后 `nc = code_book.shape[0]`）；否则 `nc = int(code_book)`，再用 `_valid_init_meth[minit]` 查表并调用 `init_meth(data, code_book, rng, xp)` 生成初始码本。

初始化方法字典：

[`vq/_vq_impl.py:574`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L574) —— `_valid_init_meth = {'random': _krandinit, 'points': _kpoints, '++': _kpp}`。

`_kpoints`（`'points'` 方法）：

[`vq/_vq_impl.py:445-468`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L445-L468) —— `rng.choice(data.shape[0], size=int(k), replace=False)` 无放回挑 k 个行索引，再用 `xp.take(data, idx, axis=0)` 取出对应行。注释里提到把 `idx` 转成默认整型 dtype 是为了规避 numpy#25607。

`_krandinit`（`'random'` 方法）核心：

[`vq/_vq_impl.py:471-519`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L471-L519) —— 先算 `mu = mean(data)`，再分三种情况采样：1 维数据用标量方差；特征数 > 样本数（协方差矩阵秩亏）走 SVD；正常情况算协方差 `\_cov` 再做 Cholesky 分解 `\x = z @ cholesky(_cov).T`（其中 `z ~ N(0,I)`），最后 `x += mu`。也就是说 `'random'` 采样自 \( \mathcal{N}(\mu, \Sigma) \)，点是「凭空生成」的，不必落在真实数据上。

#### 4.2.4 代码实践：观察四种 minit 的初始簇心形态

**实践目标**：直观感受四种初始化在「初始簇心是否落在真实点上」上的差异。

**操作步骤**：

```python
import numpy as np
from scipy.cluster.vq import kmeans2

rng = np.random.default_rng(7)
data = np.array([[0., 0.], [0.1, 0.], [0., 0.1],
                 [10., 10.], [10.1, 10.], [10., 10.1]])

# 用 iter=0 不行（会报错），改用 iter=1 并观察第一轮前的初始化
# 这里直接调用内部初始化函数（仅供学习，正式代码请用 minit 参数）
from scipy.cluster.vq._vq import _vq  # 仅说明，无需用到
# 更简单：用 missing='raise' 捕获空簇前，打印一次 kmeans2 的结果反推初始质量
for minit in ['random', 'points', '++']:
    cb, lab = kmeans2(data, 2, minit=minit, seed=1, iter=1)
    print(minit, "label=", lab.tolist())
```

**需要观察的现象 / 预期结果**：`'points'` 因为挑的是真实点，初始就把两个簇分得很干净；`'random'` 采样自整体高斯，初始簇心可能落在 (5,5) 附近，第一轮容易把所有点都分给某个簇；`'++'` 通常第一轮就能把两个真实簇各自认领一个种子。具体标签**待本地验证**。

#### 4.2.5 小练习与答案

**练习**：`minit='matrix'` 和直接给 `_kpoints` 挑出来的数组当 `k`，结果会一样吗？
**答案**：不会。`minit='matrix'` 时 `kmeans2` 完全跳过 `_valid_init_meth` 查表，直接把你给的数组当码本（`nc = k.shape[0]`）；而把同一个数组当 `k` 但 `minit` 取其它值时，由于 `xp_size(code_book) > 1`，仍会走「直接当码本」分支——实际上二者等价。真正不同的是：若你传一个**标量** `k=3` 配 `minit='points'`，才会触发 `_kpoints` 随机挑点。判定的唯一依据是 `xp_size(code_book) > 1` 与 `minit == 'matrix'`。

---

### 4.3 k-means++ 的 D² 概率播种原理（_kpp 深入）

#### 4.3.1 概念说明

k-means++（Arthur & Vassilvitskii, 2007）的核心思想：**不要完全随机地选种子，而是让「离已选种子越远的点」越有可能成为下一个种子**。这样初始簇心自然散开，每个真实簇大概率先分到一个种子。

它的播种规则用一句话说就是「按 D² 加权抽样」：

- 第 1 个种子：从数据里**均匀随机**挑一个。
- 第 \(i\) 个种子（\(i \geq 2\)）：对每个数据点 \(x\)，先算它到**所有已选种子**的最短距离平方
  \[ D(x)^2 = \min_{c \in C} \|x - c\|^2 \]
  然后以概率
  \[ P(x) = \frac{D(x)^2}{\sum_{x'} D(x')^2} \]
  抽选下一个种子。

直观上：\(D(x)^2\) 大的点（远离已有种子）概率高，于是种子被「推」向尚未被覆盖的区域。

**理论保证**：k-means++ 选出的初始码本，其期望畸变不超过全局最优畸变的 \(O(\log k)\) 倍（具体地 \(8(\ln k + 2)\) 倍）。这是它比朴素随机初始化更稳定的数学根源。

#### 4.3.2 核心流程：逆 CDF 抽样

`_kpp` 用「逆累积分布函数（inverse-CDF）抽样」实现按 \(P(x)\) 的离散抽样，避免自己写加权随机：

```
init = 空数组 (k × dims)
for i in range(k):
    if i == 0:
        data_idx = rng_integers(rng, n)        # 均匀挑第一个
    else:
        D2 = cdist(init[:i,:], data, metric='sqeuclidean').min(axis=0)  # 每点到最近种子的平方距离
        probs  = D2 / D2.sum()                  # 归一化为概率
        cumprobs = probs.cumsum()               # 累积概率（离散 CDF）
        r = rng.uniform()                       # [0,1) 均匀随机数
        data_idx = searchsorted(cumprobs, r)    # 在 CDF 上二分定位
    init[i,:] = data[data_idx,:]
```

`searchsorted(cumprobs, r)` 的含义：在单调递增的累积概率数组里找到第一个 `≥ r` 的位置——这正是「按概率抽样」的标准实现，复杂度 \(O(\log n)\)。整个 `_kpp` 的复杂度是 \(O(k \cdot n)\)（每选一个种子对所有点算一次 `cdist`）。

#### 4.3.3 源码精读

`_kpp` 主体：

[`vq/_vq_impl.py:522-571`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L522-L571) —— 第一个种子用 `rng_integers(rng, data.shape[0])`（来自 `scipy._lib._util`，兼容 `Generator` 与旧 `RandomState`）；后续种子用 `cdist(init[:i,:], data, metric='sqeuclidean').min(axis=0)` 算 D²，归一化后 `cumsum` 再 `searchsorted` 定位。注意它用 `metric='sqeuclidean'`（平方欧氏距离），这正是「D²」里的平方。`xpx.at(init)[i, :].set(...)` 是 array_api_extra 的不可变索引赋值，兼容 JAX/Dask。

#### 4.3.4 代码实践：手写 D² 播种并与 _kpp 对比

**实践目标**：用纯 numpy 复现「D² 概率播种」，理解 `searchsorted` 那一步在做什么。

**操作步骤**：

```python
import numpy as np
from scipy.spatial.distance import cdist

def my_kpp(data, k, rng):
    n = data.shape[0]
    init = np.empty((k, data.shape[1]))
    idx0 = rng.integers(n)
    init[0] = data[idx0]
    for i in range(1, k):
        d2 = cdist(init[:i], data, metric='sqeuclidean').min(axis=0)
        probs = d2 / d2.sum()
        cum = probs.cumsum()
        r = rng.random()
        idx = int(np.searchsorted(cum, r))
        init[i] = data[idx]
    return init

rng = np.random.default_rng(3)
data = np.vstack([rng.standard_normal((50, 2)) + [0, 0],
                  rng.standard_normal((50, 2)) + [10, 10],
                  rng.standard_normal((50, 2)) + [0, 10]])
init = my_kpp(data, 3, np.random.default_rng(3))
print("手写 kpp 初始簇心=\n", init)
# 观察：三个初始簇心通常分别落在三个真实簇附近
```

**需要观察的现象 / 预期结果**：三个初始种子应分别靠近 (0,0)、(10,10)、(0,10) 三个簇心，因为 D² 会把后选的种子推向「离已选种子最远」的区域。具体坐标**待本地验证**，但「每个真实簇大概率先分到一个种子」是 D² 加权的必然趋势。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_kpp` 第一个种子用「均匀随机」而不是 D²？
**答案**：因为最初没有任何已选种子，\(D(x)^2\) 无从定义（到空集的最小距离没有意义）。所以必须先用均匀随机打破对称，之后才有 D² 可算。

**练习 2**：如果所有数据点都和已选种子重合（\(D(x)^2\) 全为 0），`_kpp` 会怎样？
**答案**：`D2.sum() == 0` 会使 `probs = 0/0` 得到 NaN，后续 `cumsum`/`searchsorted` 行为未定义。这是退化情形（数据完全重合或 k 大于真实簇数时的极端情况），实践中应避免对完全重合数据用大 k。

---

### 4.4 空簇处理：_missing_warn / _missing_raise 与簇心回退

#### 4.4.1 概念说明

如 2.2 节所述，迭代中某簇可能一个点都分不到（`has_members[j] == False`）。`kmeans2` 的处理策略由 `missing` 参数决定：

- `missing='warn'`（默认）：调用 `_missing_warn()`，发出 `UserWarning`，然后**继续**。
- `missing='raise'`：调用 `_missing_raise()`，抛出 `ClusterError`，**终止**算法。

但无论 warn 还是 raise 之前，都有一个共同的「簇心回退」动作：把空掉的簇心**重置为它在上一轮的位置**。这是 `kmeans2` 与 `kmeans` 处理空簇的根本差异：

- `kmeans` / `_kmeans`：`code_book = code_book[has_members]` —— 直接把空簇**删掉**，码本变小。
- `kmeans2`：`new_code_book[~has_members] = code_book[~has_members]` —— **保留** k 个簇心，空簇回退到原位。

为什么 `kmeans2` 必须保留？因为它要**保证返回的 `code_book` 恰好是 k 行**（用户要的就是 k 个簇），不能像 `kmeans` 那样自由收缩。

#### 4.4.2 核心流程

每一轮迭代里，分配与更新之后：

```
new_code_book, has_members = update_cluster_means(data, label, nc)
if 不是所有簇都有成员（即 has_members.all() 为 False）:
    miss_meth()                                   # warn 或 raise
    new_code_book[~has_members] = code_book[~has_members]   # 空簇心回退到上一轮位置
code_book = new_code_book
```

注意一个重要推论：因为空簇心被**回退到它失去成员之前的同一个位置**，下一轮它从这个位置出发，很可能**再次**吸引不到点——于是空簇常常会在**每一轮**都触发，warning 可能连续打印 `iter` 次。这是「回退」策略的副作用：它防止了除零，但不试图修复空簇。

#### 4.4.3 源码精读

空簇处理字典与两个回调：

[`vq/_vq_impl.py:574-590`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L574-L590) —— `_missing_warn()` 用 `warnings.warn(...)` 提示重新初始化；`_missing_raise()` 直接 `raise ClusterError(...)`；`_valid_miss_meth = {'warn': _missing_warn, 'raise': _missing_raise}` 是分发字典。

`missing` 参数校验（在函数最开头）：

[`vq/_vq_impl.py:718-721`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L718-L721) —— `miss_meth = _valid_miss_meth[missing]`，传错字符串会抛 `ValueError("Unknown missing method ...")`。

主循环里的空簇分支：

[`vq/_vq_impl.py:770-775`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L770-L775) —— `if not has_members.all():` 进入处理：先 `miss_meth()`（warn 或 raise），再用 `new_code_book[~has_members] = code_book[~has_members]` 把空簇心回退。注意 `raise` 模式下 `miss_meth()` 会直接抛异常，回退那一行根本执行不到——也就是说 `'raise'` 是「检测到即终止」，而 `'warn'` 是「检测到、回退、继续」。

#### 4.4.4 代码实践：构造一个会触发空簇警告的数据集

**实践目标**：亲手触发一次空簇 `UserWarning`，并体会 `missing='raise'` 的差别。

**操作步骤**：

```python
import warnings
import numpy as np
from scipy.cluster.vq import kmeans2, ClusterError

# 只有「两个」真实团，却要求 k=3 个簇 —— 第三个簇心很容易变空
data = np.array([[0., 0.], [0.01, 0.], [-0.01, 0.],
                 [10., 10.], [10.01, 10.], [9.99, 10.]])

warned = False
for seed in range(50):
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cb, lab = kmeans2(data, 3, minit='points', seed=seed, iter=10, missing='warn')
        if any("empty" in str(x.message).lower() for x in w):
            print("seed", seed, "触发空簇警告；标签", lab.tolist(),
                  "各簇点数", np.bincount(lab, minlength=3).tolist())
            warned = True
            break
if not warned:
    print("50 个种子内未触发（可加大范围或改用 minit='random'）")

# 对比 missing='raise'：一旦空簇就抛异常
for seed in range(50):
    try:
        kmeans2(data, 3, minit='points', seed=seed, iter=10, missing='raise')
    except ClusterError as e:
        print("seed", seed, "抛出 ClusterError:", e)
        break
```

**需要观察的现象 / 预期结果**：

- `missing='warn'` 时，应在某个 seed 下捕获到 `"One of the clusters is empty."` 警告，且 `np.bincount(lab)` 里会有一项为 0（即存在没分到点的簇）。因为 4.4.2 提到的「回退后再次变空」现象，warning 在单个 `kmeans2` 调用里可能**重复出现**（最多 `iter` 次）。
- `missing='raise'` 时，同样条件下会抛 `ClusterError` 而非仅警告。
- 具体哪个 seed 触发**待本地验证**（依赖随机种子），但「数据真实团数 < k」是触发空簇的高危结构。

> 小贴士：实践中遇到空簇，最稳妥的修复是换用 `minit='++'`（种子撒得更开，空簇概率显著降低），或适当减小 k。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `kmeans2` 用「回退到原位」而不是像很多教材说的「把空簇心重新随机放到离它最远的点」？
**答案**：实现简单且确定性强（无需额外随机性与重算距离），代价是空簇可能反复出现。`kmeans2` 选择了「保守回退 + 让用户自己决定 warn/raise」，把决策权交给用户；更激进的「重放到最远点」策略留给上层应用或 `sklearn.KMeans` 等实现。

**练习 2**：`missing='raise'` 时，回退那行 `new_code_book[~has_members] = code_book[~has_members]` 会执行吗？
**答案**：不会。因为 `_missing_raise()` 直接 `raise ClusterError`，循环中断，紧跟其后的回退语句执行不到。所以 `'raise'` 是「发现空簇即终止」，而 `'warn'` 才会真正用到回退逻辑继续跑。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「初始化方法对 k-means 稳定性的影响」小调研。

**任务**：构造一个有明显 3 团结构的二维数据集，分别用 `minit='random'`、`'points'`、`'++'` 各跑 30 次（用 `seed=0..29`），统计最终畸变的均值与标准差，验证 k-means++ 最稳定；再额外演示一次空簇警告。

**参考代码**：

```python
import warnings
import numpy as np
from scipy.cluster.vq import kmeans2

rng = np.random.default_rng(2024)
data = np.vstack([
    rng.standard_normal((80, 2)) + [0, 0],
    rng.standard_normal((80, 2)) + [8, 8],
    rng.standard_normal((80, 2)) + [0, 8],
])

def distortion(data, cb, label):
    # 与 kmeans 一致：到所属簇心的平均欧氏距离（非平方、非求和）
    return np.linalg.norm(data - cb[label], axis=1).mean()

print("minit     mean     std      min      max")
for minit in ['random', 'points', '++']:
    ds = []
    for s in range(30):
        cb, lab = kmeans2(data, 3, minit=minit, seed=s, iter=20)
        ds.append(distortion(data, cb, lab))
    ds = np.array(ds)
    print(f"{minit:8s}  {ds.mean():.4f}  {ds.std():.4f}  {ds.min():.4f}  {ds.max():.4f}")

# 额外：触发一次空簇警告（真实团数=2，要 k=3）
small = np.array([[0., 0.], [0.01, 0.], [10., 10.], [10.01, 10.]])
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    for s in range(100):
        kmeans2(small, 3, minit='points', seed=s, iter=10, missing='warn')
    print("空簇警告次数（累计所有迭代）：",
          sum("empty" in str(x.message).lower() for x in w))
```

**预期结论**（数值**待本地验证**，趋势由 4.3 节理论保证）：

- `'++'` 的畸变 `std` 通常最小、`mean` 最低，因为 D² 播种几乎总能把 3 个种子放进 3 个真实团。
- `'points'` 次之，偶尔会两个种子落在同一个团而导致畸变偏高。
- `'random'` 最不稳定，因为高斯采样的种子可能落在团与团之间的稀疏区。
- 空簇警告在「真实团数 < k」的结构下，扫描若干个 seed 后几乎必然出现。

**思考题**：把上面 `kmeans2(...)` 换成 `kmeans(data, 3)`（上一讲的函数），同样统计 30 次的畸变分布。结合 2.3 节的对照表，解释为什么 `kmeans` 的畸变分布通常比 `kmeans2(..., minit='points')` 更集中。（提示：`kmeans` 的 `iter` 是重启次数，每次独立随机初始化后取最低畸变，相当于自带了「多次取最优」。）

## 6. 本讲小结

- `kmeans2` 跑**固定 `iter` 轮**「分配—更新」循环，**`thresh` 参数目前完全不起作用**；这与 `kmeans` 的「畸变收敛」语义不同。
- 初始化由 `minit` 字符串经 `_valid_init_meth` 字典分发到 `_kpoints`（挑真实点）/ `_krandinit`（高斯采样）/ `_kpp`（k-means++）；`'matrix'` 是「直接把 k 当码本」的旁路，不查表。
- k-means++（`_kpp`）用「D² 概率 + `searchsorted` 逆 CDF 抽样」把种子撒开，期望畸变有 \(O(\log k)\) 的理论保证，这是它最稳定的根源。
- 空簇处理由 `_valid_miss_meth` 分发：`'warn'` 发警告后继续、`'raise'` 抛 `ClusterError` 终止；二者之前都有「空簇心回退到上一轮位置」的动作，保证返回的码本始终是 k 行——这与 `kmeans`「直接丢弃空簇」截然不同。
- 返回的 `label` 是用「上一轮码本」分配的，与最终 `code_book` 不严格自洽，是源码执行顺序带来的轻微滞后。
- `@_transition_to_rng("seed")` 使旧的 `seed=` 参数仍可用并平滑迁移到 `rng=`，`@xp_capabilities(cpu_only=True, ...)` 声明了 cpu_only 等能力约束。

## 7. 下一步学习建议

- **横向对比**：把本讲的 `kmeans2` 与 u2-l3 的 `kmeans` 放在一起，针对同一个数据集画「畸变 vs 种子」的箱线图，直观感受「固定迭代单次运行」与「多次重启取最优」的差异。
- **进入层次聚类主线**：vq 子模块到此已基本讲完，建议进入 [u3-l1 凝聚式聚类与 linkage matrix 数据结构](u3-l1-linkage-matrix.md)，开始层次聚类（hierarchy）这条线。
- **若继续深挖 vq**：可阅读 `vq/_vq.pyx` 中 `update_cluster_means` 的 Cython 实现，看它如何用「累加 + 计数 / 相除」两遍扫描高效求各簇均值并产出 `has_members` 布尔数组——这正是本讲 4.4 节空簇检测的源头。
