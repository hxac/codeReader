# kmeans 主流程：迭代、畸变收敛与簇心更新

## 1. 本讲目标

学完本讲后，你应该能够：

- 用一句话讲清 k-means 的「分配（assignment）→ 更新（update）」迭代本质，并指出 `scipy.cluster.vq` 里哪两个函数分别对应这两步。
- 读懂 `_kmeans` 这个「裸循环」：它如何反复调用 `vq` 分配、调用 `_vq.update_cluster_means` 更新簇心，并用一个 `deque(maxlen=2)` 比较前后两轮的「平均畸变」来判断收敛。
- 说清 `scipy` 对「畸变（distortion）」的非常规定义——它是**各观测到最近簇心的欧氏距离的均值**（非平方、非求和），与教材里「平方距离之和」不同，并能指出守护这条语义的回归测试。
- 看懂公开入口 `kmeans` 的两层逻辑：当传入初始码本时只跑一次、当传入簇数 `k` 时用 `_kpoints` 随机初始化并**多次重启（`iter`）取畸变最低**的码本，同时理解空簇被「直接丢弃」的策略。

本讲承接 [u2-l2 vq 编码函数与 Cython 后端](u2-l2-vq-and-cython-backend.md)：那里讲的 `vq` 正是 k-means 迭代里被反复调用的「分配步」热点函数；本讲把它和「更新步」`_vq.update_cluster_means` 串成完整的迭代循环。建议先扫一眼 [u2-l1 whiten](u2-l1-whiten-preprocessing.md) 关于 `xp`（数组命名空间）的说明，因为本讲会频繁出现 `xp.mean`、`xp.asarray` 这类跨后端调用。

## 2. 前置知识

### 2.1 Lloyd 算法：k-means 的「分配-更新」两步舞

k-means 的标准实现（Lloyd 算法）极其直白，核心就是一个无限循环，每轮做两件事：

1. **分配（assignment）**：对每个观测，找到离它最近的簇心，把它归到那个簇。这一步的数学本质就是向量量化里的「编码」——给定码本（簇心集合），把每个观测编码到最近码字。这正是 [u2-l2](u2-l2-vq-and-cython-backend.md) 讲的 `vq`。
2. **更新（update）**：对每个簇，把它的簇心更新为该簇所有成员的**均值**（重心）。

反复交替这两步，簇心会逐渐稳定下来——因为一旦「分配结果不再改变」，更新出的新簇心就和旧的一样，循环就没了意义。于是需要一个「停止条件」：当簇心稳定（或等价地，当畸变不再明显下降）时就退出。本讲要精读的 `_kmeans` 就是把这个过程写成代码。

### 2.2 畸变（distortion）：衡量码本好坏的标尺

「畸变」用来衡量一个码本（一组簇心）和数据的吻合程度，越小越好。这里有一个**必须记住的坑**：`scipy.cluster.vq.kmeans` 返回的畸变，是

\[
\text{distortion} \;=\; \frac{1}{M}\sum_{i=1}^{M} \lVert \text{obs}_i - \text{centroid}_{\text{code}(i)} \rVert
\]

即「各观测到其最近簇心的**欧氏距离（未平方）**的**均值**」。而教材里 k-means 的畸变通常是「**平方**距离之**和**」。`kmeans` 的 docstring 明确警告了这一差异（"Note the difference to the standard definition ... which is the sum of the squared distances"）。第 4.3 节会看到一条专门的回归测试守护这个定义。

为什么 `scipy` 选「均值、非平方」？因为 `vq` 返回的 `dist` 本来就是欧氏距离（见 [u2-l2 第 4.4 节](u2-l2-vq-and-cython-backend.md)末尾的 `sqrt`），直接对它求均值最自然，也便于和 `vq` 的输出互相校验。

### 2.3 `deque` 的定长滑动窗口技巧

Python 标准库 `collections.deque` 在指定 `maxlen` 后是一个**定长双端队列**：当它已满时再 `append`，会自动把最旧的一个元素挤出去。`_kmeans` 用 `deque(maxlen=2)` 来「只记住最近两轮的平均畸变」，从而能在 O(1) 空间里比较「这一轮 vs 上一轮」的变化量。如果手写，你需要手动维护一个长度为 2 的滑动窗口；`deque(maxlen=2)` 把这件事变成一行代码。第 4.3 节会逐轮追踪它的内容变化。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [vq/_vq_impl.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py) | Python 实现层。含 `_kmeans`（裸迭代循环）、`kmeans`（公开入口，多次重启）、`_kpoints`（随机选初始簇心）。 |
| [vq/_vq.pyx](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq.pyx) | Cython 性能层。含 `update_cluster_means`（更新步包装）与 `_update_cluster_means`（核心 C 实现）。 |
| [vq/tests/test_vq.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/tests/test_vq.py) | 测试。`TestKMeans` 类用 `test_kmeans_large_thres`、`test_kmeans_distortion_matches_vq_mean_regression_24069`、`test_kmeans_diff_convergence`、`test_kmeans_lost_cluster` 等守护本讲的每一条语义。 |

调用链全景（自顶向下）：

```text
用户调用 kmeans(obs, k_or_guess, iter, thresh, ...)
   │
   ├─ k_or_guess 是「初始码本数组」(xp_size != 1) ──→ _kmeans(obs, guess, thresh)   # 只跑一次
   │
   └─ k_or_guess 是「簇数 k」(标量) ──→ for i in range(iter):                        # 多次重启
            guess  = _kpoints(obs, k, rng)        # 随机选 k 个观测当初始簇心
            book,d = _kmeans(obs, guess, thresh)  # 跑一次完整迭代
            保留 d 最小的 book                    # 取最优
      返回 best_book, best_dist

_kmeans(obs, guess, thresh):                       # 裸迭代循环
   while 前后两轮平均畸变之差 > thresh:
       obs_code, distort = vq(obs, code_book)                   # ① 分配步（u2-l2 已讲）
       记录 mean(distort) 进 deque(maxlen=2)
       code_book, has_members = _vq.update_cluster_means(...)   # ② 更新步（本讲 4.1）
       code_book = code_book[has_members]                       # ③ 丢弃空簇
       diff = |deque[0] - deque[1]|
   用最终 code_book 重算一次 vq，返回 (code_book, 平均畸变)      # ④ 返回语义（本讲 4.3）
```

---

## 4. 核心概念与源码讲解

### 4.1 _vq.update_cluster_means：簇心更新与空簇标记

#### 4.1.1 概念说明

「更新步」的任务很纯粹：给定每个观测的簇标签 `labels`，把每个簇的簇心重新算成「该簇全体成员的均值（重心）」。这件事本身不难（`numpy` 里就是按标签分组求均值），但 `scipy` 把它放进 Cython 后端 `_vq.pyx` 是有原因的——它在 k-means 的每一轮迭代里都会被调用，属于热点路径，用 C 循环避免 Python 开销。

这个函数还有一个关键副产物：它会告诉你**哪些簇是空的**（没有任何成员）。空簇的簇心无法求均值（除以零），所以函数把空簇的位置全填 0，并返回一个布尔数组 `has_members` 标记「哪些簇有成员」。上层函数（`_kmeans` 和 `kmeans2`）对空簇有不同处理策略——`_kmeans` 选择**直接丢弃**空簇（见 4.3 节），`kmeans2` 则选择「警告/报错 + 保留旧位置」（见 [u2-l4](u2-l4-kmeans2-init-and-empty.md)）。

#### 4.1.2 核心流程

```text
update_cluster_means(obs, labels, nc):        # _vq.pyx 包装层
  1. 连续化 obs / labels
  2. 校验: obs ∈ {float32, float64}; labels 必须是 1 维; labels 转 int32
  3. cb = np.zeros((nc, nfeat))               # 新码本，初始全 0
  4. 按 dtype 调用 _update_cluster_means(...)

_update_cluster_means(obs*, labels*, cb*, nobs, nc, nfeat):   # 核心 C 实现
  # 第一遍：累加每个簇的成员向量之和，并数成员个数
  obs_count = np.zeros(nc)                     # 每簇成员计数
  for i in 0..nobs-1:
      label = labels[i]
      cb[label] += obs[i]                      # 同簇向量累加到 cb[label]
      obs_count[label] += 1
  # 第二遍：每个簇的和 ÷ 成员数 = 均值（重心）
  for i in 0..nc-1:
      if obs_count[i] > 0:
          cb[i] /= obs_count[i]                # 有成员 → 求均值
      # 空簇保持全 0（不除，避免除零）
  return obs_count > 0                         # has_members 布尔数组
```

注意两遍扫描的设计：第一遍**累加**（`cb[label] += obs[i]`）同时**计数**（`obs_count[label] += 1`），第二遍统一**相除**。之所以拆成两遍而不是「边累加边除」，是因为每个簇的成员要全部累加完才能知道它的总数，才能做一次除法。

#### 4.1.3 源码精读

包装层 `update_cluster_means` 的定义与校验：[vq/_vq.pyx:301-363](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq.pyx#L301-L363) — 连续化、dtype/维度校验、按 dtype 分发到核心函数，返回 `(cb, has_members)`。

它的 docstring 里对空簇行为的说明很关键：[vq/_vq.pyx:323-327](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq.pyx#L323-L327)

```python
    Notes
    -----
    The empty clusters will be set to all zeros and the corresponding elements
    in `has_members` will be `False`. The upper level function should decide
    how to deal with them.
```

这段话定下了「空簇全 0 + has_members=False，怎么处理交给上层」的契约。本讲的 `_kmeans` 选择丢弃，`kmeans2` 选择保留旧位置——两者都建立在这个契约之上。

核心 C 实现里的「累加 + 计数」第一遍：[vq/_vq.pyx:272-284](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq.pyx#L272-L284)

```python
    # Calculate the sums the numbers of obs in each cluster
    obs_count = np.zeros(nc, np.intc)
    obs_p = obs
    for i in range(nobs):
        label = labels[i]
        cb_p = cb + nfeat * label

        for j in range(nfeat):
            cb_p[j] += obs_p[j]

        # Count the obs in each cluster
        obs_count[label] += 1
        obs_p += nfeat
```

逐行要点：

- `obs_count = np.zeros(nc, np.intc)`：每簇成员计数，长度等于簇数 `nc`。
- `cb_p = cb + nfeat * label`：用**指针算术**直接定位到 `cb[label]` 这一行的起点（`cb` 是行主序，每行 `nfeat` 个元素）。这是 Cython 拿裸指针提速的典型写法。
- 内层 `for j` 把第 `i` 个观测的每个特征累加进 `cb[label]`；外层同时 `obs_count[label] += 1` 数成员。

第二遍「相除求均值」：[vq/_vq.pyx:286-295](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq.pyx#L286-L295)

```python
    cb_p = cb
    for i in range(nc):
        cluster_size = obs_count[i]

        if cluster_size > 0:
            # Calculate the centroid of each cluster
            for j in range(nfeat):
                cb_p[j] /= cluster_size

        cb_p += nfeat
```

注意 `if cluster_size > 0` 的保护：空簇（`cluster_size == 0`）跳过除法，保持全 0——这正是 docstring 承诺的行为，也避免了除以零。

最后返回 `has_members`：[vq/_vq.pyx:297-298](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq.pyx#L297-L298) — `return obs_count > 0`，把「计数数组」一举转成「是否有成员」的布尔数组，精炼。

#### 4.1.4 代码实践

**实践目标**：用纯 `numpy` 复现 `update_cluster_means` 的「分组求均值 + 空簇标记」语义，验证两者一致。

**操作步骤**：

```python
import numpy as np
from scipy.cluster.vq._vq_impl import _vq

# 4 个观测，2 个特征；标签把第 0、3 个观测归到簇 0，第 1、2 个归到簇 1，簇 2 故意留空
obs    = np.array([[0., 0.], [10., 10.], [12., 8.], [2., 2.]])
labels = np.array([0, 1, 1, 0])          # 簇 2 没有成员
nc     = 3

# 官方 Cython 实现
cb, has_members = _vq.update_cluster_means(obs, labels, nc)

# 手动复现：按标签分组求均值
my_cb = np.zeros((nc, obs.shape[1]))
my_count = np.zeros(nc, dtype=int)
for x, lab in zip(obs, labels):
    my_cb[lab] += x
    my_count[lab] += 1
my_has = my_count > 0
my_cb[my_has] /= my_count[my_has, None]   # 只对有成员的簇相除

print("cb:\n", cb)
print("my_cb:\n", my_cb)
print("码本一致:", np.allclose(cb, my_cb))                  # 期望 True
print("has_members:", has_members, " (簇 2 应为 False)")    # 期望 [ True  True False]
print("空簇簇心:", cb[2], " (应为全 0)")                     # 期望 [0. 0.]
```

**需要观察的现象**：

- 簇 0 的簇心 = `mean([0,0],[2,2])` = `[1, 1]`；簇 1 的簇心 = `mean([10,10],[12,8])` = `[11, 9]`。
- `has_members` 为 `[True, True, False]`，空簇 2 的簇心保持 `[0, 0]`。
- 你的手动实现与官方输出逐元素一致。

**预期结果**：`cb` 与 `my_cb` 一致、`has_members` 正确标记空簇、空簇簇心全 0。若不一致请核对标签与 `nc` 是否对应。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `update_cluster_means` 要拆成「累加 + 相除」两遍，而不是「每来一个成员就把簇心增量更新」一遍搞定？

**参考答案**：增量更新簇心（每来一个成员就 `centroid = (centroid*n + x)/(n+1)`）也能得到正确结果，但会引入更多次浮点除法，且数值上不如「先求总和再除一次」稳定。两遍法的优点是：每簇只做**一次**除法，累加用纯加法（快且数值友好），同时顺便数出成员数用于空簇判断。

**练习 2**：如果把 `if cluster_size > 0` 的保护去掉、直接对空簇相除，会发生什么？为什么上层 `_kmeans` 不用担心这个问题？

**参考答案**：空簇会触发除以零——整数除零抛 `ZeroDivisionError`，浮点除零得到 `nan`/`inf`，污染后续 `vq` 的距离计算。上层 `_kmeans` 不用担心，是因为它在拿到 `has_members` 后立刻执行 `code_book = code_book[has_members]`（见 4.3 节）把空簇从码本里**剔除**，空簇的 `[0,0]` 占位值根本不会被使用。

---

### 4.2 _kmeans：分配-更新迭代主循环

#### 4.2.1 概念说明

`_kmeans` 是 k-means 的「裸引擎」：给它一组观测 `obs` 和一个**初始猜测** `guess`（初始码本），它就老老实实地跑「分配→更新」循环，直到收敛，返回最终码本和平均畸变。它**不做**初始化、**不做**多次重启——那些是上层 `kmeans` 的职责。这种「裸循环 vs 包装」的分工让代码很清晰：`_kmeans` 只关心「给定起点，如何收敛」。

它的循环体正是第 2.1 节描述的 Lloyd 两步：

- **分配步**：调 `vq(obs, code_book)` 得到每个观测的簇标签 `obs_code` 和到最近簇心的距离 `distort`（这一步的细节在 [u2-l2](u2-l2-vq-and-cython-backend.md) 已讲透）。
- **更新步**：调 `_vq.update_cluster_means(obs, obs_code, k)` 把簇心更新为各簇均值，同时拿到 `has_members` 标记。

两步之间，它顺手做两件小事：(1) 用 `deque` 记录本轮平均畸变，供收敛判断；(2) 用 `code_book[has_members]` 丢弃空簇。循环退出后，它还会用最终码本**重算一次**畸变再返回——这个细节留到 4.3 节展开。

#### 4.2.2 核心流程

```text
_kmeans(obs, guess, thresh=1e-5, xp=None):
  code_book = guess
  prev_avg_dists = deque([inf], maxlen=2)     # 滑动窗口，初值 inf 保证至少跑一轮
  np_obs = np.asarray(obs)                     # update_cluster_means 需要 numpy
  diff = inf
  while diff > thresh:
      # ① 分配步
      obs_code, distort = vq(obs, code_book, check_finite=False)
      prev_avg_dists.append(mean(distort))     # 记录本轮平均畸变
      # ② 更新步
      code_book, has_members = _vq.update_cluster_means(np_obs, np.asarray(obs_code),
                                                        code_book.shape[0])
      code_book = code_book[has_members]       # ③ 丢弃空簇
      code_book = xp.asarray(code_book)        # 切回原数组后端
      # ④ 收敛判断：本轮 vs 上一轮平均畸变之差
      diff = abs(prev_avg_dists[0] - prev_avg_dists[1])
  # ⑤ 用最终码本重算畸变
  _, final_distortions = vq(obs, code_book, check_finite=False)
  return code_book, mean(final_distortions)
```

几个一眼要抓住的点：

- `vq` 喂的是原始 `obs`（可能是任意数组后端），而 `update_cluster_means` 喂的是 `np_obs`（强制 numpy），因为后者是 Cython 函数只认 numpy。这就是为什么循环里有 `np.asarray` 和 `xp.asarray` 的来回转换——「分配步跨后端、更新步回 numpy」的桥接。
- 收敛判据是 `diff = abs(本轮平均畸变 - 上一轮平均畸变)`，当它 ≤ `thresh`（默认 `1e-5`）时停。注意比的是**畸变的变化量**，不是畸变本身。
- `deque([inf], maxlen=2)` 的初值 `inf` 是个巧妙设计：第一轮 `diff = abs(inf - mean_1) = inf`，必定 `> thresh`，从而保证**至少跑满一轮**（否则若 `guess` 恰好已是最优，循环可能一轮都不跑，连一次分配-更新都没有）。

#### 4.2.3 源码精读

`_kmeans` 的完整定义：[vq/_vq_impl.py:222-274](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L222-L274) — 这是本讲的核心，建议通读。

循环体（分配→记录→更新→丢弃→判收敛）：[vq/_vq_impl.py:259-270](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L259-L270)

```python
    np_obs = np.asarray(obs)
    while diff > thresh:
        # compute membership and distances between obs and code_book
        obs_code, distort = vq(obs, code_book, check_finite=False)
        prev_avg_dists.append(xp.mean(distort, axis=-1))
        # recalc code_book as centroids of associated obs
        obs_code = np.asarray(obs_code)
        code_book, has_members = _vq.update_cluster_means(np_obs, obs_code,
                                                          code_book.shape[0])
        code_book = code_book[has_members]
        code_book = xp.asarray(code_book)
        diff = xp.abs(prev_avg_dists[0] - prev_avg_dists[1])
```

逐行解读：

- `obs_code, distort = vq(obs, code_book, check_finite=False)`：分配步。`obs_code[i]` 是 `obs[i]` 的簇号，`distort[i]` 是它到最近簇心的欧氏距离。`check_finite=False` 是因为上层 `kmeans` 入口已经检查过有限值，循环里无需重复检查。
- `prev_avg_dists.append(xp.mean(distort, axis=-1))`：把「本轮平均畸变」塞进定长队列。`axis=-1` 是为了兼容 1 维观测（标量特征）的情况——对 1 维数组 `mean(axis=-1)` 等价于整体求均值。
- `obs_code = np.asarray(obs_code)`：`vq` 返回的 `obs_code` 可能是别的数组后端，转成 numpy 才能喂给 Cython 的 `update_cluster_means`。
- `code_book.shape[0]` 作为 `nc` 传给 `update_cluster_means`：注意传的是**当前簇数**，随着空簇被丢弃，这个数会逐轮缩小。
- `code_book = code_book[has_members]`：用布尔索引**丢弃空簇**——这是 `kmeans`（区别于 `kmeans2`）的空簇策略，4.3 节详述。
- `diff = xp.abs(prev_avg_dists[0] - prev_avg_dists[1])`：取队列里仅有的两个元素（上一轮、本轮）作差。

> **数组后端桥接小结**：循环里 `obs`（原始后端）→ `vq` → `np.asarray` → `update_cluster_means`（numpy）→ `code_book[has_members]`（numpy）→ `xp.asarray`（切回原始后端）→ 下一轮 `vq`。这条链让「惰性/异构数组输入」与「Cython 热点计算」能协同工作，是 [u2-l1](u2-l1-whiten-preprocessing.md) 提到的 array API 抽象的具体落地。

#### 4.2.4 代码实践

**实践目标**：参照 `_kmeans` 用纯 `numpy` 实现一个「单次 k-means 迭代循环」，复现它的分配-更新-收敛逻辑，并与官方 `scipy.cluster.vq.kmeans` 的簇心对比。

**操作步骤**：

```python
import numpy as np
from scipy.cluster.vq import vq, kmeans, whiten

# ---- 准备数据：两个明显的簇 + 一个离群点 ----
features = np.array([[1.9, 2.3],
                     [1.5, 2.5],
                     [0.8, 0.6],
                     [0.4, 1.8],
                     [0.1, 0.1],
                     [2.0, 0.5]])
obs = whiten(features)                         # kmeans 前要先白化（见 u2-l1）
guess = np.array([obs[0], obs[2]])             # 初始码本：随便选两个观测

# ---- 手写 _kmeans 风格的迭代循环 ----
def my_kmeans(obs, code_book, thresh=1e-5):
    code_book = np.array(code_book, dtype=float)
    prev = [np.inf]                            # 模拟 deque(maxlen=2) 的滑动窗口
    while True:
        obs_code, distort = vq(obs, code_book, check_finite=False)
        avg = np.mean(distort)
        prev.append(avg)                       # 只保留最近两个
        prev = prev[-2:]
        # 更新步：按簇求均值；手动版用循环，省去空簇处理的细节
        new_cb = np.zeros_like(code_book)
        for c in range(len(code_book)):
            members = obs[obs_code == c]
            if len(members) > 0:               # 有成员才更新（对应 has_members）
                new_cb[c] = members.mean(axis=0)
            else:
                new_cb[c] = code_book[c]       # 空簇保留旧位置（简化处理）
        code_book = new_cb
        if abs(prev[0] - prev[1]) <= thresh:   # 收敛：畸变变化 <= 阈值
            break
    _, final_dist = vq(obs, code_book, check_finite=False)
    return code_book, np.mean(final_dist)

my_book, my_dist = my_kmeans(obs, guess)

# ---- 官方：传同一个 guess，kmeans 只跑一次（iter 对 guess 模式无效）----
off_book, off_dist = kmeans(obs, guess)

print("我的簇心:\n", np.round(my_book, 5))
print("官方簇心:\n", np.round(off_book, 5))
# 排序后比较（簇的顺序可能不同）
print("簇心集合一致:", np.allclose(np.sort(my_book.flat), np.sort(off_book.flat)))
```

**需要观察的现象**：

- 你的循环会在若干轮后因 `abs(prev[0]-prev[1]) <= 1e-5` 自动停止，无需预先指定迭代次数。
- 手写版与官方版的簇心集合（忽略簇的排列顺序）应当一致。注意：当 `guess` 是数组时，`kmeans` 直接调 `_kmeans` 跑**一次**（见 4.4 节），所以两者可比。
- 若你的手写版处理空簇的方式（保留旧位置）与官方（丢弃）不同，当数据恰好产生空簇时簇数会不一致；本例数据不会产生空簇，所以一致。

**预期结果**：簇心集合一致（`allclose` 为 `True`）。若你想严格对齐官方「丢弃空簇」的行为，可在循环里 `code_book = new_cb[[len(obs[obs_code==c])>0 for c in range(len(new_cb))]]` 模拟 `code_book[has_members]`。

#### 4.2.5 小练习与答案

**练习 1**：`_kmeans` 循环里的收敛判据是「畸变**变化量**小于阈值」，而不是「畸变本身小于阈值」。这两种判据各有什么问题？

**参考答案**：「畸变本身小于阈值」的问题是：阈值的含义依赖数据尺度——同样 `1e-5`，对尺度 1 的数据算「很小」，对尺度 1000 的数据却几乎不可能达到，导致永不收敛或过早停止。「畸变变化量小于阈值」衡量的是「是否还在改善」：只要相邻两轮畸变几乎不变（`diff ≤ thresh`），说明算法已无法再显著降低畸变，无论数据尺度多大都能稳定停在一个合理的「平台」。这也是 Lloyd 算法常用的停止准则。

**练习 2**：循环里为什么是 `code_book.shape[0]`（当前簇数）传给 `update_cluster_means`，而不是固定用最初的 `k`？

**参考答案**：因为 `code_book = code_book[has_members]` 会把空簇从码本里剔除，导致簇数逐轮**可能减少**。`update_cluster_means` 需要知道「现在码本里有几个簇」才能正确分配 `cb` 的行。如果固定传最初的 `k`，一旦前面有空簇被剔除，`labels` 里的簇号就会超出新的码本行数，导致越界。

---

### 4.3 收敛判据与返回语义：deque、畸变定义、最终重算、空簇丢弃

#### 4.3.1 概念说明

`_kmeans` 看似简单，却藏着四个容易被忽略、但都被测试死死守护的语义细节。本节把它们集中讲清：

1. **`deque([inf], maxlen=2)` 如何保证「至少跑一轮」**：定长队列 + 无穷初值的小技巧。
2. **畸变的精确定义**：是 `mean(distort)`，即欧氏距离的均值（非平方、非求和）。
3. **循环退出后为何要「重算一次畸变」**：保证返回的畸变与返回的码本严格自洽。
4. **空簇被「直接丢弃」**：`kmeans` 与 `kmeans2` 在空簇策略上的根本区别。

#### 4.3.2 核心流程（用一个真实回归测试逐轮追踪）

用官方测试 `test_kmeans_large_thres`（[vq/tests/test_vq.py:383-388](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/tests/test_vq.py#L383-L388)）的数据做手工追踪。输入 `obs = [1,2,3,4,10]`，`k=1`，`thresh=1e16`（超大阈值，用于逼出「一轮收敛」行为）。`_kpoints` 随机选 1 个观测当初始码本，假设选中 `[1]`：

| 轮次 | `vq` 用的码本 | `distort` | `mean(distort)` 入队 | 队列内容 | 更新后的新码本 | `diff` | 是否继续 |
|------|--------------|-----------|----------------------|----------|----------------|--------|----------|
| 初值 | — | — | — | `[inf]` | `[1]` | `inf` | — |
| 1 | `[1]` | `[0,1,2,3,9]` | `3` | `[inf, 3]` | `[4]`（均值=4） | `inf` | 是 |
| 2 | `[4]` | `[3,2,1,0,6]` | `2.4` | `[3, 2.4]` | `[4]`（不变） | `0.6` | 否（`0.6 < 1e16`） |

循环退出后，用最终码本 `[4]` 重算：`dists = [3,2,1,0,6]`，`mean = 2.4`。返回 `([4], 2.4)`——与测试断言 `res[0]==[4.]`、`res[1]==2.4` **完全吻合**。

这张表把四个细节全演示了：

- **细节 1（至少跑一轮）**：第 1 轮 `diff = abs(inf - 3) = inf`，必 `> thresh`，所以哪怕初始码本碰巧很好，也至少跑满一轮。
- **细节 2（畸变定义）**：第 1 轮畸变 `3 = mean([0,1,2,3,9])`，是**欧氏距离的均值**，不是平方距离之和（后者会是 `0+1+4+9+81=95`）。
- **细节 3（最终重算）**：循环里最后一次入队的畸变是第 2 轮的 `2.4`，返回前又用 `[4]` 重算得到 `2.4`——本例两者恰好相等（因为第 2 轮更新没改变码本），但语义上它们是两回事（见 4.3.3）。
- **细节 4（空簇）**：`k=1` 不会产生空簇，所以这里看不到丢弃；空簇行为见下方 `test_kmeans_lost_cluster`。

#### 4.3.3 源码精读

**细节 1 + 收敛判断**——队列初始化与 `diff` 计算：[vq/_vq_impl.py:256-257](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L256-L257) 与 [vq/_vq_impl.py:270](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L270)

```python
    diff = xp.inf
    prev_avg_dists = deque([diff], maxlen=2)
    ...
        diff = xp.abs(prev_avg_dists[0] - prev_avg_dists[1])
```

`deque([inf], maxlen=2)` 的精妙：第一轮 `append(mean_1)` 后队列是 `[inf, mean_1]`，`diff = abs(inf - mean_1) = inf`，必继续；第二轮 `append(mean_2)` 后 `inf` 被挤出，队列变成 `[mean_1, mean_2]`，`diff = abs(mean_1 - mean_2)` 才是真正的「两轮对比」。用 `maxlen=2` 自动挤出旧值，省去手动维护下标。

**细节 2（畸变定义）**——守护它的回归测试 `test_kmeans_distortion_matches_vq_mean_regression_24069`：[vq/tests/test_vq.py:390-403](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/tests/test_vq.py#L390-L403)

```python
    rng = np.random.default_rng(1)
    codebook, distortion = kmeans(wh, 3, iter=5, rng=rng)

    _, dists = vq(wh, codebook, check_finite=False)
    xp_assert_close(distortion, xp.mean(dists, axis=-1), rtol=1e-12, atol=1e-12)
```

这条测试（针对 issue gh-24069）直接断言：`kmeans` 返回的 `distortion` 必须**精确等于** `mean(vq(wh, codebook) 的距离)`。它锁死了「畸变 = 欧氏距离均值」这条语义——任何人若误把它改成「平方距离之和」或「求和」，这条测试立刻失败。

**细节 3（最终重算）**——循环退出后的重算：[vq/_vq_impl.py:272-274](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L272-L274)

```python
    _, final_distortions = vq(obs, code_book, check_finite=False)
    final_distortions_avg = xp.mean(final_distortions, axis=-1)
    return code_book, final_distortions_avg
```

为什么不直接返回队列里最后一个畸变值？因为循环里的「第 t 轮畸变」是用**第 t 轮开始时的码本**（即第 t-1 轮更新后的码本）算的，而循环退出时码本又被第 t 轮的 `update_cluster_means` **更新了一次**。退出条件是「畸变**变化量**小」，不代表码本没动。所以返回前必须用**最终码本**重算一次畸变，确保「返回的畸变」与「返回的码本」严格自洽。在 4.3.2 的例子里两者恰好相等（因为已到固定点），但这是特例，不是通则。

**细节 4（空簇丢弃）**——`code_book = code_book[has_members]`：[vq/_vq_impl.py:268](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L268)。守护它的测试是 `test_kmeans_lost_cluster`：[vq/tests/test_vq.py:270-287](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/tests/test_vq.py#L270-L287)

```python
    def test_kmeans_lost_cluster(self, xp):
        # This will cause kmeans to have a cluster with no points.
        data = xp.asarray(TESTDATA_2D)
        initk = xp.asarray([[-1.8127404, -0.67128041],
                            [2.04621601, 0.07401111],
                            [-2.31149087, -0.05160469]])
        kmeans(data, initk)                     # kmeans 静默丢弃空簇，不报错
        with warnings.catch_warnings():
            ...
            kmeans2(data, initk, missing='warn')   # kmeans2 选择告警
        assert_raises(ClusterError, kmeans2(data, initk, missing='raise'))  # 或报错
```

对比一目了然：同样的「会产生空簇」的初始码本，`kmeans` 直接丢弃空簇、不声不响地返回更少的簇（docstring 明确："centroids assigned to no observations are removed during iterations"）；而 `kmeans2` 走 `_missing_warn`/`_missing_raise`（[vq/_vq_impl.py:577-590](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L577-L590)），要么告警要么抛 `ClusterError`，并保留簇数。这就是为什么 `kmeans` 返回的簇心数**可能少于**你要求的 `k`。

> **另一个收敛回归测试**：`test_kmeans_diff_convergence`（[vq/tests/test_vq.py:431-436](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/tests/test_vq.py#L431-L436)）用 `obs=[-3,-1,0,1,1,8]`、初始 `[-3, 0.99]`，断言最终收敛到 `[[-0.4, 8.]]`、畸变 `1.0667`。它守护的是「用畸变变化量收敛」这条准则本身（针对 issue gh-8727），可以自己手工追踪验证。

#### 4.3.4 代码实践

**实践目标**：通过加日志观察 `_kmeans` 的逐轮畸变变化，亲眼看到 `deque` 收敛与「最终重算」。

**操作步骤**（源码阅读型实践，不修改 SciPy 源码）：

```python
import numpy as np
from scipy.cluster.vq import vq, kmeans, whiten
from scipy.cluster.vq._vq_impl import _kmeans, _vq
from collections import deque

# 复刻 _kmeans，但在循环里打印每轮畸变
def traced_kmeans(obs, guess, thresh=1e-5):
    code_book = np.asarray(guess, dtype=float)
    prev = deque([np.inf], maxlen=2)
    np_obs = np.asarray(obs)
    diff = np.inf
    rnd = 0
    while diff > thresh:
        rnd += 1
        obs_code, distort = vq(obs, code_book, check_finite=False)
        avg = np.mean(distort)
        prev.append(avg)
        code_book, has_members = _vq.update_cluster_means(np_obs, np.asarray(obs_code),
                                                          code_book.shape[0])
        code_book = code_book[has_members]
        diff = abs(prev[0] - prev[1])
        print(f"轮{rnd}: 入队畸变={avg:.6f}, 队列={list(np.round(prev,6))}, "
              f"diff={diff:.2e}, 簇数={len(code_book)}, 继续={diff > thresh}")
    _, final = vq(obs, code_book, check_finite=False)
    print(f"循环退出, 最终重算畸变={np.mean(final):.6f}")
    return code_book, np.mean(final)

obs   = whiten(np.array([[1.,1],[1.1,0.9],[8.,8],[8.2,7.8],[1.2,1.1]]))
guess = np.array([obs[0], obs[2]])
traced_kmeans(obs, guess)
```

**需要观察的现象**：

- 第 1 轮的 `diff` 一定是 `inf`（因为队列是 `[inf, mean_1]`），所以「继续=True」——这就是「至少跑一轮」的保证。
- 从第 2 轮起，`diff` 是真实的两轮畸变差，会迅速变小直到 `≤ 1e-5`。
- 「循环退出」那行打印的「最终重算畸变」与最后一轮「入队畸变」在本例应相等（已到固定点）。

**预期结果**：看到 `diff` 从 `inf` → 某个较大值 → … → `≤ 1e-5` 的下降序列，循环在 2~4 轮内停止；最终重算畸变与最后一轮入队畸变一致。若把数据换成更复杂的分布，可能会看到两者有微小差异（印证细节 3 的存在）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `deque([diff], maxlen=2)` 改成 `deque([], maxlen=2)`（初值不为 `inf`，空队列），第 1 轮会发生什么？

**参考答案**：第 1 轮 `append(mean_1)` 后队列是 `[mean_1]`，长度只有 1。此时执行 `diff = abs(prev[0] - prev[1])` 会抛 `IndexError`（队列只有 1 个元素，`prev[1]` 越界）。`inf` 初值不仅填满了队列使其在第 1 轮就有 2 个元素，还顺便让第 1 轮 `diff=inf` 保证继续——一举两得。

**练习 2**：`test_kmeans_distortion_matches_vq_mean_regression_24069` 为什么要用 `rtol=1e-12, atol=1e-12` 这么严的容差？

**参考答案**：因为这条测试要守护的是「畸变**定义**」（均值，而非平方和）这条**数学等式**，不是近似行为。`kmeans` 返回的畸变本应**精确**等于 `mean(vq 的距离)`（因为返回前正是这么重算的，见细节 3），所以容差可以也应当极严。如果这里出现超容差的偏差，说明有人改动了畸变的定义或返回路径，是必须捕获的回归。

**练习 3**：为什么 `kmeans` 选择「丢弃空簇」而不是像 `kmeans2` 那样报错或保留？

**参考答案**：`kmeans` 的定位是「多次重启、取畸变最低」的稳健接口——它的 `iter` 参数会跑多次随机初始化（见 4.4 节），偶尔出现空簇是随机初始化的正常现象，丢弃后继续比较各次重启的畸变即可，没必要因此中断。而 `kmeans2` 是「单次初始化、固定 `iter` 轮」的接口，空簇往往意味着初始化或数据有问题，应当让用户知道（`warn`）或直接中止（`raise`）。两种策略服务于不同的使用场景。

---

### 4.4 kmeans：公开入口的分发、多次重启与 _kpoints 初始化

#### 4.4.1 概念说明

`_kmeans` 只知道「给定起点如何收敛」，但 k-means 的结果**高度依赖初始码本**——不同的初始猜测可能收敛到畸变不同的局部最优。`kmeans` 作为公开入口，要解决两件事：

1. **分发**：判断 `k_or_guess` 是「初始码本数组」还是「簇数 k」。前者 → 直接调 `_kmeans` 跑**一次**（用户已指定起点，`iter` 被忽略）；后者 → 进入多次重启流程。
2. **多次重启取最优**：当只给簇数 `k` 时，用 `_kpoints` 随机选 `k` 个观测当初始码本，调 `_kmeans` 跑一次，记录畸变；重复 `iter` 次（默认 20），**保留畸变最低**的那个码本。这是降低「初始敏感」的常用手段。

注意 `iter` 的语义陷阱：它指的是**重启次数**，**不是**「k-means 算法的迭代轮数」（后者由 `thresh` 收敛判据自动决定）。docstring 反复强调了这一点（"This parameter does not represent the number of iterations of the k-means algorithm"）。

#### 4.4.2 核心流程

```text
kmeans(obs, k_or_guess, iter=20, thresh=1e-5, *, rng=None):
  1. 解析数组后端 xp，规整 obs / guess
  2. if iter < 1: raise ValueError
  3. 分发:
       if xp_size(guess) != 1:          # 是初始码本数组
           if xp_size(guess) < 1: raise ValueError
           return _kmeans(obs, guess, thresh)      # 只跑一次，iter 被忽略
       # 否则是标量 k
       k = int(guess)
       if k != guess: raise ValueError("标量必须是整数")
       if k < 1:     raise ValueError
  4. rng = check_random_state(rng)        # 归一化随机源
  5. best_dist = inf
     for i in range(iter):                # 多次重启
         guess = _kpoints(obs, k, rng)    # 随机选 k 个观测当初始簇心
         book, dist = _kmeans(obs, guess, thresh)
         if dist < best_dist:             # 保留畸变最低
             best_book, best_dist = book, dist
     return best_book, best_dist
```

`_kpoints` 的逻辑极简：从 `data` 的行里**无放回**随机抽 `k` 行当初始簇心（`rng.choice(data.shape[0], size=k, replace=False)`）。

#### 4.4.3 源码精读

`kmeans` 的签名与两个装饰器：[vq/_vq_impl.py:277-280](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L277-L280)

```python
@xp_capabilities(cpu_only=True, jax_jit=False, allow_dask_compute=True)
@_transition_to_rng("seed")
def kmeans(obs, k_or_guess, iter=20, thresh=1e-5, check_finite=True, *, rng=None):
```

- `@xp_capabilities(...)`：声明只在 CPU 跑（内部要调 Cython），不支持 `jax.jit`，但允许 dask 计算——和 `vq` 的约束一致（见 [u2-l2 第 4.1 节](u2-l2-vq-and-cython-backend.md)）。
- `@_transition_to_rng("seed")`：处理 SPEC7 的随机源迁移——允许旧的 `seed=` 参数继续工作（兼容），同时把各种随机源（整数、`RandomState`、`Generator`）统一归一化成 `rng`。守护它的测试是 `test_kmeans_and_kmeans2_random_seed`（[vq/tests/test_vq.py:438-456](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/tests/test_vq.py#L438-L456)），它验证「相同 seed 两次调用结果相同」。

入口校验与分发：[vq/_vq_impl.py:409-429](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L409-L429)

```python
    if isinstance(k_or_guess, int):
        xp = array_namespace(obs)
    else:
        xp = array_namespace(obs, k_or_guess)
    obs = _asarray(obs, xp=xp, check_finite=check_finite)
    guess = _asarray(k_or_guess, xp=xp, check_finite=check_finite)
    if iter < 1:
        raise ValueError(f"iter must be at least 1, got {iter}")

    # Determine whether a count (scalar) or an initial guess (array) was passed.
    if xp_size(guess) != 1:
        if xp_size(guess) < 1:
            raise ValueError(f"Asked for 0 clusters. Initial book was {guess}")
        return _kmeans(obs, guess, thresh=thresh, xp=xp)

    # k_or_guess is a scalar, now verify that it's an integer
    k = int(guess)
    if k != guess:
        raise ValueError("If k_or_guess is a scalar, it must be an integer.")
    if k < 1:
        raise ValueError(f"Asked for {k} clusters.")
```

分发判据是 `xp_size(guess)`（数组总元素数）：`!= 1` 说明是「码本数组」（`k×N`，元素数 ≥ 2），走单次 `_kmeans`；`== 1` 说明是标量 `k`，走多次重启。注意它先把 `k_or_guess` 用 `_asarray` 包成数组再判 size——这样无论是 Python `int` 还是数组都能统一处理。`k < 1` 的校验由 `test_kmeans_0k`（[vq/tests/test_vq.py:377-381](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/tests/test_vq.py#L377-L381)）守护，断言 `kmeans(X, 0)` 抛 `ValueError`。

多次重启取最优：[vq/_vq_impl.py:431-442](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L431-L442)

```python
    rng = check_random_state(rng)

    # initialize best distance value to a large value
    best_dist = xp.inf
    for i in range(iter):
        # the initial code book is randomly selected from observations
        guess = _kpoints(obs, k, rng, xp)
        book, dist = _kmeans(obs, guess, thresh=thresh, xp=xp)
        if dist < best_dist:
            best_book = book
            best_dist = dist
    return best_book, best_dist
```

- `best_dist = xp.inf`：用无穷大初始化「最优畸变」，保证第一次重启的结果一定被采纳。
- `if dist < best_dist`：只保留畸变更低的码本——「多次重启取最优」的核心。
- 每次 `guess = _kpoints(...)` 都是全新的随机初始化，所以 `iter` 次重启是 `iter` 个**不同**的局部最优候选。

`_kpoints` 的实现：[vq/_vq_impl.py:445-468](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L445-L468)

```python
def _kpoints(data, k, rng, xp):
    """Pick k points at random in data (one row = one observation)."""
    idx = rng.choice(data.shape[0], size=int(k), replace=False)
    # convert to array with default integer dtype (avoids numpy#25607)
    idx = xp.asarray(idx, dtype=xp.asarray([1]).dtype)
    return xp.take(data, idx, axis=0)
```

- `rng.choice(data.shape[0], size=k, replace=False)`：从行索引里**无放回**抽 `k` 个，保证初始簇心不重复。
- `idx = xp.asarray(idx, dtype=...)`：转成「默认整数 dtype」——注释指明这是为了规避 numpy#25607 这个 bug。
- `xp.take(data, idx, axis=0)`：按抽中的行索引取出初始码本（`k×N`）。

> **`iter` 是重启次数，不是迭代轮数**：这是 `kmeans` 最容易踩的坑。`test_kmeans_simple`（[vq/tests/test_vq.py:256-260](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/tests/test_vq.py#L256-L260)）传 `iter=1` 时，因为 `initc` 是数组（`xp_size != 1`），直接走单次 `_kmeans` 分支，`iter=1` 实际被忽略——印证了「传码本时 `iter` 无效」。只有传标量 `k` 时 `iter` 才生效，表示重启次数。

#### 4.4.4 代码实践

**实践目标**：验证「多次重启取最优」确实能降低对初始点的敏感，并观察 `iter` 的重启语义。

**操作步骤**：

```python
import numpy as np
from scipy.cluster.vq import kmeans, whiten

rng = np.random.default_rng(0)
# 构造 3 个明显分离的簇
a = rng.normal(0,  0.3, size=(30, 2))
b = rng.normal(8,  0.3, size=(30, 2))
c = rng.normal(16, 0.3, size=(30, 2))
obs = whiten(np.vstack([a, b, c]))

# 同一个 rng 起点，分别重启 1 次和 20 次
for n_iter in [1, 5, 20]:
    book, dist = kmeans(obs, 3, iter=n_iter, rng=np.random.default_rng(0))
    print(f"iter={n_iter:2d}: 最终畸变={dist:.5f}, 簇心数={len(book)}")

# 对照：传初始码本数组，iter 被忽略（只跑一次）
guess = np.array([obs[0], obs[30], obs[60]])
book_g, dist_g = kmeans(obs, guess, iter=20)   # iter=20 在此被忽略
print(f"传码本(忽略 iter): 畸变={dist_g:.5f}")
```

**需要观察的现象**：

- `iter=1` 时畸变通常较大（单次随机初始化可能不够好）；随着 `iter` 增大，畸变**单调不增**（因为 `best_dist` 只在更优时更新），最终趋于稳定。
- 传初始码本数组时，无论 `iter` 传多少都只跑一次（走 `_kmeans` 单次分支）。
- 若某次重启恰好让某个簇空了，返回的「簇心数」会**少于 3**（空簇被丢弃）。

**预期结果**：畸变随 `iter` 增大而下降或持平；传码本时 `iter` 无效。具体数值依赖随机性，故畸变的绝对值标记为「待本地验证」，但「单调不增」的趋势应稳定可复现。

#### 4.4.5 小练习与答案

**练习 1**：`kmeans(obs, obs[:3])`（传 3 个观测当初始码本）和 `kmeans(obs, 3)`（传簇数 3）在行为上有什么本质区别？

**参考答案**：前者把 `k_or_guess` 当「初始码本数组」（`xp_size(obs[:3]) = 3×N ≠ 1`），走单次 `_kmeans`，`iter` 被忽略，结果确定性强（起点固定）；后者把 `k_or_guess` 当「簇数 k」（标量，`xp_size == 1`），走多次重启，每次用 `_kpoints` 随机初始化，跑 `iter` 次取最优，结果依赖 `rng`。简言之：传码本=「指定起点跑一次」，传 k=「随机起点跑多次取最优」。

**练习 2**：为什么 `best_dist` 初始化为 `xp.inf` 而不是 `0`？如果初始化为 `0` 会怎样？

**参考答案**：`if dist < best_dist` 用的是严格小于。初始化为 `inf` 保证第一次重启的 `dist`（有限值）一定 `< inf`，从而被采纳为初始最优；之后每次更优的才会覆盖。若初始化为 `0`，则任何 `dist ≥ 0` 都不会 `< 0`，`best_book` 永远不会被赋值，函数最后 `return best_book, best_dist` 会抛 `UnboundLocalError`（`best_book` 未定义）——除非所有重启畸变都恰好为 0（极罕见）。

**练习 3**：`_kpoints` 用 `replace=False`（无放回抽样）。如果改成 `replace=True`（有放回），会对 k-means 造成什么影响？

**参考答案**：有放回抽样可能抽到**重复**的行作为初始簇心——两个簇心初始重合。第一轮 `vq` 分配时，离这两个重合簇心等距的点会因 `vq` 的「严格小于」比较（见 [u2-l2 第 4.4 节](u2-l2-vq-and-cython-backend.md)）全归到第一个，第二个簇可能一开始就没有成员，立刻变成空簇被丢弃，实际簇数少于 `k`。无放回抽样保证初始簇心互不相同，降低这种退化概率。

---

## 5. 综合实践

把本讲四块知识串成一条完整的「k-means 透视」：自己从零实现一个等价于 `scipy.cluster.vq.kmeans` 的 `my_kmeans`（含多次重启），并用本讲引用的几条**回归测试数据**验证它的正确性。

```python
import numpy as np
from scipy.cluster.vq import vq, kmeans, whiten
from scipy.cluster.vq._vq_impl import _vq, _kpoints

def _my_raw_kmeans(obs, guess, thresh=1e-5):
    """复刻 _kmeans：分配-更新循环 + deque 收敛 + 丢弃空簇 + 最终重算。"""
    code_book = np.asarray(guess, dtype=float)
    prev = [np.inf]                              # 手动滑动窗口（等价 deque(maxlen=2)）
    while True:
        obs_code, distort = vq(obs, code_book, check_finite=False)
        prev.append(np.mean(distort)); prev = prev[-2:]
        # 更新步：直接用官方 Cython 函数（重点在复刻循环逻辑）
        code_book, has_members = _vq.update_cluster_means(np.asarray(obs),
                                                          np.asarray(obs_code),
                                                          code_book.shape[0])
        code_book = code_book[has_members]       # 丢弃空簇
        if abs(prev[0] - prev[1]) <= thresh:
            break
    _, final = vq(obs, code_book, check_finite=False)
    return code_book, np.mean(final)

def my_kmeans(obs, k_or_guess, iter=20, thresh=1e-5, rng=None):
    """复刻 kmeans：码本 vs 标量分发 + 多次重启取最优。"""
    rng = np.random.default_rng() if rng is None else np.random.default_rng(rng)
    guess = np.asarray(k_or_guess)
    if guess.size != 1:                          # 初始码本 → 单次
        return _my_raw_kmeans(obs, guess, thresh)
    k = int(guess)
    best_dist, best_book = np.inf, None
    for _ in range(iter):                        # 标量 k → 多次重启
        g = _kpoints(np.asarray(obs), k, rng, np)
        book, dist = _my_raw_kmeans(obs, g, thresh)
        if dist < best_dist:
            best_book, best_dist = book, dist
    return best_book, best_dist

# ---- 验证 1：复刻 test_kmeans_large_thres 的数据 ----
x = np.array([1., 2, 3, 4, 10])
b, d = my_kmeans(x, 1, thresh=1e16, rng=0)
print(f"[large_thres] 码本={b.flatten()}, 畸变={d:.4f} (期望 [4.], 2.4000)")

# ---- 验证 2：畸变定义 = mean(vq 距离) ----
obs = whiten(np.array([[8.,1e-5,1e-5,1e-5]] + [[1e-5,1e-5,1e-5,1e-5]]*51
                      + [[0.,1e-5,1e-5,1e-5]]))
book, dist = my_kmeans(obs, 3, iter=5, rng=1)
_, dists = vq(obs, book, check_finite=False)
print(f"[畸变定义] 返回畸变={dist:.8e}, mean(vq)={np.mean(dists):.8e}, "
      f"一致={np.isclose(dist, np.mean(dists), rtol=1e-12, atol=1e-12)}")

# ---- 验证 3：与官方 kmeans 在同一随机起点下簇心集合一致 ----
rng0 = np.random.default_rng(123)
feat = whiten(rng0.normal(size=(60, 2)) * np.array([1, 5]))   # 故意不等方差
mine_b, mine_d = my_kmeans(feat, 3, iter=10, rng=7)
off_b,  off_d  = kmeans(feat, 3, iter=10, rng=7)
print(f"[对比官方] 我的畸变={mine_d:.5f}, 官方畸变={off_d:.5f}")
```

**完成标准**：

1. 验证 1 能复现 `码本=[4.]`、`畸变=2.4`——证明你复刻的循环与官方 `_kmeans` 在 `thresh` 极大时行为一致。
2. 验证 2 的「一致」为 `True`——证明你的 `my_kmeans` 也满足「畸变 = 欧氏距离均值」这条语义（即回归测试 gh-24069 守护的等式）。
3. 验证 3 你的畸变与官方畸变接近（簇心可能因空簇丢弃顺序略有差异，但畸变应非常接近）——证明「分配-更新-收敛-重启」整条链你都理解了。
4. 能解释：为什么验证 1 用 `thresh=1e16` 能逼出「一轮收敛」？为什么验证 2 必须用 `allclose` 而非精确相等？（答：浮点；但容差极严因为这是定义性等式。）

---

## 6. 本讲小结

- k-means 的 Lloyd 算法就是「**分配**（`vq`，[u2-l2](u2-l2-vq-and-cython-backend.md)）→ **更新**（`_vq.update_cluster_means`）」的交替循环；`_kmeans` 是把这个循环写成代码的裸引擎，`kmeans` 是负责初始化与多次重启的公开包装。
- `_vq.update_cluster_means`（[vq/_vq.pyx:301-363](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq.pyx#L301-L363)）用「累加 + 计数 / 相除」两遍扫描求各簇均值，空簇保持全 0 并通过 `has_members` 布尔数组上报，把「如何处理空簇」的决定权交给上层。
- `_kmeans`（[vq/_vq_impl.py:222-274](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L222-L274)）用 `deque(maxlen=2)` 只记最近两轮平均畸变，以 `diff = abs(本轮 - 上一轮) ≤ thresh` 为收敛判据；`inf` 初值保证至少跑一轮。
- 「畸变」在 `scipy` 里是**各观测到最近簇心欧氏距离的均值**（非平方、非求和），由回归测试 gh-24069 严格守护；循环退出后用最终码本**重算一次**畸变再返回，保证返回值与码本自洽。
- `kmeans` 的空簇策略是**直接丢弃**（`code_book[has_members]`），返回簇心数可能少于 `k`；这与 `kmeans2` 的「警告/报错 + 保留」截然不同（`test_kmeans_lost_cluster` 同时覆盖两者）。
- `kmeans`（[vq/_vq_impl.py:277-442](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L277-L442)）按 `xp_size(guess)` 分发：传码本数组→单次 `_kmeans`（`iter` 被忽略）；传标量 `k`→`_kpoints` 随机初始化 + `iter` 次重启取畸变最低。**`iter` 是重启次数，不是迭代轮数。**

## 7. 下一步学习建议

- 下一篇 [u2-l4 kmeans2：多种初始化与空簇处理](u2-l4-kmeans2-init-and-empty.md) 会讲 `kmeans2`：它有 `random/points/++/matrix` 四种初始化（`_krandinit`/`_kpoints`/`_kpp`），用固定 `iter` 轮而非畸变收敛作停止条件，并用 `_missing_warn`/`_missing_raise` 处理空簇。学完后建议把 `kmeans` 与 `kmeans2` 做一张对比表（初始化、停止条件、空簇策略、畸变是否返回）。
- 想巩固「畸变定义」这条语义，可阅读 `test_kmeans_distortion_matches_vq_mean_regression_24069`（[vq/tests/test_vq.py:390-403](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/tests/test_vq.py#L390-L403)）和对应的 issue gh-24069，理解为什么 SciPy 要专门加一条测试来锁死它。
- 想深入「数组后端桥接」，可对比 `_kmeans` 循环里 `np.asarray` / `xp.asarray` 的来回转换与 [u2-l1](u2-l1-whiten-preprocessing.md) 讲的 `array_namespace`、[u2-l2](u2-l2-vq-and-cython-backend.md) 讲的 `xp_capabilities`，体会 Cython 热点（numpy-only）与跨后端 API 的协作模式。
- vq 子模块学完本讲即告完成，接下来可进入 [u3 单元 层次聚类基础](u3-l1-linkage-matrix.md)，那是 `scipy.cluster` 的另一条主线。
