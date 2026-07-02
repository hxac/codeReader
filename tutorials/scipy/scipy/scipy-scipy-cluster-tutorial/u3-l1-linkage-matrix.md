# 凝聚式聚类与 linkage matrix 数据结构

> 本讲是 hierarchy（层次聚类）子模块的「地基课」。后面所有讲义（七种方法分发、Cython 算法、fcluster 切树、dendrogram 画图……）都建立在你对 **linkage matrix Z** 这个数据结构的准确理解之上。本讲只讲清楚两件事：凝聚式聚类在做什么、Z 这张 (n−1)×4 的表每一列代表什么。

## 1. 本讲目标

学完本讲，你应当能够：

1. 用自己的话描述**凝聚式聚类**「从一片森林逐步合并成一棵树」的过程。
2. 拿到一个 linkage matrix `Z`，逐行读出：哪两个簇合并了、合并距离是多少、新簇包含多少个原始观测。
3. 说清楚 **`n + i` 编号约定**：为什么第 `i`（从 0 起）步合并出的新簇编号是 `n + i`，以及为什么原始观测编号是 `0 … n−1`。
4. 区分 `linkage` 的两种输入：**压缩距离矩阵**（`pdist` 的输出，一维）与**观测矩阵**（原始特征，二维），并能用 `distance.num_obs_y` / `distance.is_valid_y` 判断一个一维数组是不是合法的压缩距离矩阵。
5. 手工追踪一个 4 点的 `single` linkage 例子，画出对应的 `Z`，并用源码验证。

## 2. 前置知识

本讲默认你已经读过 [u1-l1 项目总览] 与 [u1-l3 快速上手]，知道：

- `scipy.cluster.hierarchy` 是层次 / 凝聚式聚类子模块，最小工作链是 `pdist → linkage → fcluster`。
- 一个**观测矩阵** `X` 是 M×N 的：M 行观测、N 列特征（见 u1-l3）。
- `linkage` 的真正实现藏在私有的 `hierarchy/_hierarchy_impl.py` 里，公共 API 只是重新导出（见 u1-l2 的双层架构）。

本讲会引入几个新术语，遇到时再解释：

- **凝聚式（agglomerative）**：自底向上、从每点一簇开始不断合并。与之相对的是自顶向下的**分裂式（divisive）**。scipy 只实现凝聚式。
- **簇（cluster）**：若干原始观测的集合。算法运行中，森林里同时存在「单点簇」和「多点簇」。
- **压缩距离矩阵（condensed distance matrix）**：把 n×n 的两两距离方阵只保留上三角（不含对角线）后拉平的一维向量，长度为 \(\binom{n}{2}=n(n-1)/2\)，正是 `scipy.spatial.distance.pdist` 的输出格式。
- **linkage matrix Z**：层次聚类的「产物表」，形状 (n−1)×4，是本讲的主角。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到哪里 |
|---|---|---|
| [hierarchy/_hierarchy_impl.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py) | hierarchy 的 Python 实现层 | `linkage` 函数与 docstring、`_LINKAGE_METHODS` 字典、输入校验 |
| [spatial/distance.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/spatial/distance.py) | 距离矩阵工具 | `num_obs_y`、`is_valid_y`：压缩距离矩阵的观测数推断与合法性校验 |
| [hierarchy/_hierarchy.pyx](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx) | hierarchy 的 Cython 性能层 | `condensed_index` 下标编码、`mst_single_linkage`、`label` / `LinkageUnionFind`：真正「写出 Z 每一行」的地方 |

> 提示：本讲重点是**数据结构**，不深入七种距离方法的数学细节（那是 u3-l2、u3-l4 的事），也不展开 Cython 算法的复杂度（那是 u4 的事）。我们只会从 `_hierarchy.pyx` 借用「Z 是怎么被一行行写出来」的少量代码，用来佐证 Z 各列的语义。

## 4. 核心概念与源码讲解

### 4.1 凝聚式聚类：从「森林」到「一棵树」

#### 4.1.1 概念说明

凝聚式聚类的核心思想用一句话讲就是：**一开始每个观测自成一簇，然后反复合并「当前最相似」的两个簇，直到所有点合成一簇为止。**

为什么要叫「凝聚（agglomerative）」？因为它是**自底向上**的：从最小的单位（单个观测）开始，像滚雪球一样把相似的簇粘到一起。每一次合并都不可逆——合并后就不会再把一个簇拆开。这正好和 k-means（vq 模块）形成对比：

| 维度 | k-means（vq） | 凝聚式（hierarchy） |
|---|---|---|
| 簇数 k | 需要预先指定 | 不需要预先指定，先建树再切 |
| 过程 | 迭代分配-更新，可来回改 | 单调合并，不可逆 |
| 产物 | 一组簇心 + 标签 | 一棵「合并树」（dendrogram） |
| 层次结构 | 无（平的） | 有（可以看出哪些簇更亲近） |

scipy 在 `linkage` 的 docstring 里用「**森林（forest）**」这个比喻描述过程，非常贴切：

> 算法开始时有一片「尚未被使用的簇」组成的森林。当森林里的两个簇 `s` 和 `t` 合并成一个新簇 `u` 时，`s` 和 `t` 从森林中移除，`u` 加入森林。当森林里只剩一个簇时，算法停止，这个簇就是根。

整个过程会产生恰好 **n−1 次合并**（n 个叶子合成 1 棵树，每次合并少一个簇：n → n−1 → … → 1，共 n−1 步）。这就是为什么 linkage matrix 固定有 **n−1 行**。

#### 4.1.2 核心流程

用伪代码描述凝聚式聚类的骨架（先忽略「最相似」到底怎么算）：

```
输入: n 个原始观测的两两距离
森林 ← { 每个观测自成一簇 }              # 共 n 个单点簇
对 i = 0, 1, ..., n-2:                    # 共 n-1 次合并
    (s, t, d) ← 在森林里挑出距离最小的两簇 s, t，距离为 d
    u ← 把 s 和 t 合并成新簇
    从森林移除 s, t；把 u 加入森林
    记录一行: [s的编号, t的编号, d, |u|]   # |u| 是 u 包含的原始观测数
输出: 一张 n-1 行、4 列的表 Z
```

注意三个关键点：

1. **每步只合并两个簇**：scipy 生成的是**二叉树**（每个内部节点恰好两个孩子）。
2. **「距离最小」的定义有 7 种**（single/complete/average/centroid/median/ward/weighted），这是 [u3-l2] 与 [u3-l4] 的内容，本讲先把它当作黑盒。
3. **新簇需要一个编号**：原始观测已经占用了 `0 … n−1`，所以第 `i`（从 0 起）步合并出的新簇就用 `n + i`。这就是下一节要讲的 `n+i` 约定。

#### 4.1.3 源码精读

「森林逐步合并」这段描述直接来自 `linkage` 的 docstring：

[hierarchy/_hierarchy_impl.py:745-753](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L745-L753) —— 中文复述：算法从一片森林开始；每次把两个簇 `s`、`t` 合并成 `u`，把 `s`、`t` 移出森林、把 `u` 放进森林；森林只剩一个簇时停止，该簇即树根。

而 docstring 紧接着说明了「每次合并还要维护一张距离表」：

[hierarchy/_hierarchy_impl.py:755-761](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L755-L761) —— 中文复述：每轮迭代都要更新距离矩阵，反映「新簇 u 与森林中剩余各簇的距离」。**这一步正是七种 linkage 方法的差异所在**（用 Lance-Williams 公式统一表达，见 u3-l4）。

`linkage` 末尾会把具体的合并计算交给 Cython 后端，并根据方法名分三条路（MST / 最近邻链 / 朴素）：

[hierarchy/_hierarchy_impl.py:955-969](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L955-L969) —— 中文复述：内部闭包 `cy_linkage(y, validate)` 按方法分派——`single` 走 `_hierarchy.mst_single_linkage(y, n)`，`complete/average/weighted/ward` 走 `_hierarchy.nn_chain(y, n, method_code)`，其余（`centroid/median`）走 `_hierarchy.fast_linkage(y, n, method_code)`；返回值形状写死为 `(n - 1, 4)`，即 Z 的 n−1 行。

注意第 968 行 `shape=(n - 1, 4)`：**这里就锁死了「linkage matrix 一共有 n−1 行、4 列」这个约定**，无论哪种方法、哪种后端，产物的形状都一样。这一点是本讲最重要的结论之一。

#### 4.1.4 代码实践

**目标**：用一个最小数据集亲眼看到「凝聚式 = 不断两两合并」，并确认合并次数 = n−1。

**操作步骤**（阅读型实践，可在本地或脑中完成）：

1. 阅读 `linkage` 的 docstring 中 [Examples](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L908-L921) 小节，注意它用的是 8 个点 `X = [[i] for i in [2, 8, 0, 4, 1, 9, 9, 0]]`。
2. 在本地 Python 中运行：

   ```python
   from scipy.cluster.hierarchy import linkage
   X = [[i] for i in [2, 8, 0, 4, 1, 9, 9, 0]]   # n = 8
   Z = linkage(X, method='single')
   print(Z.shape)        # 预期 (7, 4)  即 (n-1, 4)
   print(Z)
   ```

**需要观察的现象**：

- `Z.shape` 为 `(7, 4)`，行数 7 = n − 1 = 8 − 1。
- `Z` 每一行的第 3 列（`Z[:, 3]`）是新簇包含的原始观测数，**最后一行**的该值应当是 `8`（所有点最终合并成一簇）。

**预期结果**：`Z.shape == (7, 4)`，且 `Z[-1, 3] == 8.0`。

> 待本地验证：若你的 scipy 版本与本文 HEAD（`5f09bd71`）一致，输出与上述完全相符；不同版本细节可能略有差异，但「行数 = n−1」「末行 size = n」两条不变。

#### 4.1.5 小练习与答案

**练习 1**：如果有 100 个原始观测，`linkage` 产出的 Z 有多少行？为什么？

**答案**：99 行。因为 n 个单点簇要经过 n−1 = 99 次两两合并才能合成 1 个总簇，每次合并对应 Z 的一行。源码里 `shape=(n - 1, 4)`（[L968](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L967-L969)）也印证了这一点。

**练习 2**：凝聚式聚类为什么是「不可逆」的？这对最终产物有什么影响？

**答案**：因为每一步合并都把两个簇永久绑定成新簇，之后不会再拆开（森林里 `s`、`t` 被 `u` 替换，见 [L748-L753](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L748-L753)）。这意味着任何一次「贪心」选错最小对都无法回头，所以不同 method（对「最小距离」定义不同）会得到结构不同的树；也正因为不可逆，结果是一棵确定的层次树，而非像 k-means 那样的迭代解。

---

### 4.2 linkage matrix Z：四列含义与 n+i 编号约定

#### 4.2.1 概念说明

Z 是一张 `(n-1) × 4` 的表。第 `i` 行（`i` 从 0 起）描述「第 `i` 次合并」，四列含义如下：

| 列 | 名称 | 含义 |
|---|---|---|
| `Z[i, 0]` | 簇 A | 被合并的第一个簇的编号 |
| `Z[i, 1]` | 簇 B | 被合并的第二个簇的编号 |
| `Z[i, 2]` | 距离 | 簇 A 与簇 B 的合并距离（由 linkage 方法定义） |
| `Z[i, 3]` | 簇大小 | 合并后新簇包含的**原始观测**个数 |

这里最关键、也最容易绕晕人的是**编号约定**：

- 编号 `0, 1, …, n−1` 留给 **n 个原始观测**（叶子）。
- 第 `i` 步（`i` 从 0 起）合并出的新簇，编号为 **`n + i`**。

为什么要这样编号？因为算法**一边合并一边产生新簇**，新簇也得有个唯一身份证号。原始观测已经占用了 `0 … n−1`，所以第 0 步的新簇叫 `n`，第 1 步叫 `n+1`，……，第 `i` 步叫 `n+i`。一共 n−1 个新簇，编号到 `n + (n−2) = 2n−2` 为止。

举个例子，n=4 时：

- 原始观测：`0, 1, 2, 3`。
- 第 0 步合并 → 新簇编号 `4`（= n + 0）。
- 第 1 步合并 → 新簇编号 `5`（= n + 1）。
- 第 2 步合并 → 新簇编号 `6`（= n + 2），这就是树根。

所以你在 `Z[i, 0]` 或 `Z[i, 1]` 里看到**小于 n 的数字**，那是「某个原始观测」；看到 **≥ n 的数字**，那是「之前某步合并出来的新簇」，把它减去 n 就知道它是第几步产生的。

还有两条隐藏规则（在 [u7-l1 校验] 里会被 `is_valid_linkage` 严格检查）：

1. **`Z[i, 0] < Z[i, 1]`**：同一行里两簇编号总是从小到大排。
2. **`Z[:, 2]` 单调不减**：对 single/complete/average/weighted/ward 这几种方法，合并距离随步数递增（树是「单调的」）；centroid/median 可能轻微违反（会产生「倒置」inversion）。

#### 4.2.2 核心流程

读 Z 时，建议按下面的流程逐行解析：

```
对 i = 0 .. n-2:
    a, b, d, size = Z[i]
    把编号 a 解释为「簇」:
        若 a < n:  它是原始观测 a
        若 a ≥ n:  它是第 (a - n) 步合并出的新簇
    对 b 同理
    新簇编号 = n + i     # 这一行的产物
    新簇成员数 = size    # 应等于「a 的成员数 + b 的成员数」
```

簇大小 `Z[i, 3]` 有一个很好的自洽性：第 `i` 行的 size，等于它两个孩子（`Z[i,0]` 与 `Z[i,1]` 所代表簇）的 size 之和。叶子的 size 视为 1。这条性质正是源码里 `LinkageUnionFind.merge` 用「相加」得到新簇大小的依据（见 4.2.3）。

从代数上看，整棵树一共有 n 个叶子、n−1 个内部节点，共 2n−1 个节点，编号 `0 … 2n−2` 刚好用完。

#### 4.2.3 源码精读

Z 四列的官方定义写在 `linkage` 的 docstring 开头：

[hierarchy/_hierarchy_impl.py:736-743](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L736-L743) —— 中文复述：返回 (n−1)×4 的矩阵 Z；第 `i` 次迭代把编号 `Z[i,0]` 与 `Z[i,1]` 的两簇合并成新簇 `n + i`；编号小于 n 的是原始观测；`Z[i,2]` 是两簇距离；`Z[i,3]` 是新簇中的原始观测数。

但这只是「契约」。**Z 的每一行究竟是怎么被写出来的？** 真正落笔的地方在 Cython 后端。以 `single` 方法为例，`mst_single_linkage` 先用最小生成树算法算出「谁和谁合并、距离多少」，再调用 `label()` 给簇重新编号、填上簇大小：

[hierarchy/_hierarchy.pyx:1075-1085](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1075-L1085) —— 中文复述：在 MST 主循环里，先把第 `k` 次合并的两个端点 `x`、`y` 和距离 `current_min` 写进 `Z[k,0..2]`；循环结束后按距离排序，再调用 `label(Z_arr, n)` 来「纠正簇标号并原地计算簇大小」。注意此刻 `Z[k,3]` 还没填——它由 `label` 负责填上。

`label` 用并查集（`LinkageUnionFind`）把「新簇编号」和「簇大小」一次性算好：

[hierarchy/_hierarchy.pyx:1121-1133](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1121-L1133) —— 中文复述：对每一行，先用 `uf.find` 找到 `x`、`y` 当前的根（因为它们可能已经被之前的合并包进更大的簇里），按「小号在前」写入 `Z[i,0]/Z[i,1]`（这就保证了 `Z[i,0] < Z[i,1]`），再用 `uf.merge(x_root, y_root)` 返回新簇大小写入 `Z[i,3]`。

而 `merge` 内部正是「两簇 size 相加 + 分配 `n+i` 新编号」：

[hierarchy/_hierarchy.pyx:1101-1107](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1101-L1107) —— 中文复述：`merge(x, y)` 把 `x`、`y` 的 parent 都指向 `self.next_label`（即当前的新簇编号，初始化为 `n`，每合并一次自增 1，于是第 `i` 步恰好是 `n+i`），新簇 size = `size[x] + size[y]`，写入并返回。这正是 4.2.1 里「size = 两孩子 size 之和」「新簇编号 = n+i」两条规则的代码出处。

并查集本身在构造时就预留了 `2n−1` 个槽位，对应整棵树的所有节点：

[hierarchy/_hierarchy.pyx:1096-1099](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1096-L1099) —— 中文复述：`LinkageUnionFind.__init__` 里 `parent = arange(2*n - 1)`、`next_label = n`、`size = ones(2*n - 1)`。`2n−1` 正是「n 个叶子 + n−1 个内部节点」的总数，`next_label` 从 `n` 起步分配新簇编号。

#### 4.2.4 代码实践

> 这正是任务规格里指定的实践。

**目标**：手工追踪 `linkage([[2],[8],[0],[4]], method='single')` 的每一次合并，画出 Z，并验证 `Z[:, 3]`（簇大小）列与手工推算一致。

**操作步骤**：

1. 把输入看作 4 个一维观测：obs0=2、obs1=8、obs2=0、obs3=4（`n = 4`）。
2. 算两两距离（绝对值，因为默认 `metric='euclidean'` 且是一维）：

   | 对 | 距离 |
   |---|---|
   | (0,1) = \|2−8\| | 6 |
   | (0,2) = \|2−0\| | 2 |
   | (0,3) = \|2−4\| | 2 |
   | (1,2) = \|8−0\| | 8 |
   | (1,3) = \|8−4\| | 4 |
   | (2,3) = \|0−4\| | 4 |

3. 用 single linkage（簇间距离 = 两簇中「最近两点」的距离）逐步合并：

   - **第 0 步**：最小距离 = 2，发生在 (0,2)（也与 (0,3) 并列，scipy 的 MST 实现从节点 0 出发、按节点序在并列时取小号，先选 (0,2)）。合并 obs0 与 obs2 → 新簇 `4`，size = 2。`Z[0] = [0, 2, 2, 2]`。
   - **第 1 步**：簇 {0,2} 到其余点的 single 距离：到 obs3 = min(\|2−4\|, \|0−4\|) = min(2,4) = 2；到 obs1 = min(6,8) = 6；obs1–obs3 = 4。最小仍是 2，合并簇 {0,2} 与 obs3 → 新簇 `5`，size = 3。`Z[1] = [3, 4, 2, 3]`（`3` 是 obs3，`4` 是上一步的新簇，3 < 4 故 3 在前）。
   - **第 2 步**：只剩簇 {0,2,3} 与 obs1，距离 = min(6,8,4) = 4。合并 → 新簇 `6`（树根），size = 4。`Z[2] = [1, 5, 4, 4]`。

4. 拼出手工 Z（注意是 **3 行 × 4 列**，即 `(n−1)×4 = 3×4`；任务规格里写的「4×4」其实是把「4 个观测」和矩阵形状混淆了，正确形状是 3×4）：

   ```
   [[0., 2., 2., 2.],
    [3., 4., 2., 3.],
    [1., 5., 4., 4.]]
   ```

5. 用源码验证（运行下面这段）：

   ```python
   from scipy.cluster.hierarchy import linkage
   Z = linkage([[2], [8], [0], [4]], method='single')
   print(Z)
   print("size 列 =", Z[:, 3])   # 预期 [2., 3., 4.]
   ```

**需要观察的现象**：

- `Z` 形状为 `(3, 4)`，与「n−1 行」一致。
- 每行 `Z[i, 0] < Z[i, 1]`（小号在前）。
- `Z[:, 2]`（距离列）= `[2, 2, 4]`，单调不减。
- `Z[:, 3]`（簇大小列）= `[2, 3, 4]`，与本节手工推算完全一致。
- 出现的新簇编号依次是 `4, 5, 6`（即 `n+0, n+1, n+2`）。

**预期结果**：

```
[[0. 2. 2. 2.]
 [3. 4. 2. 3.]
 [1. 5. 4. 4.]]
size 列 = [2. 3. 4.]
```

> 待本地验证：以上是按本文 HEAD（`5f09bd71`）的 `mst_single_linkage`（Prim 从节点 0 出发、`np.argsort(..., kind='mergesort')` 稳定排序、`label` 用并查集重编号）严格追踪的结果。若你的环境版本一致，逐元素相符。

#### 4.2.5 小练习与答案

**练习 1**：在 `Z = [[0,2,2,2],[3,4,2,3],[1,5,4,4]]`（n=4）里，`5` 这个编号代表什么？它是第几步合并出来的？

**答案**：`5 ≥ n=4`，所以它是一个新簇（内部节点），不是原始观测。它是第 `5 − n = 5 − 4 = 1` 步合并出来的（即 `Z[1]` 这一行产生的），由 `merge` 在 `next_label` 自增时分配（见 [pyx L1101-L1107](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1101-L1107)）。它代表簇 {0,2,3}。

**练习 2**：为什么 `Z[i, 3]` 一定等于它两个孩子 size 之和？请结合源码说明。

**答案**：因为 `label` 调用 `uf.merge(x_root, y_root)`，而 `merge` 里 `size = self.size[x] + self.size[y]`（[pyx L1104](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1101-L1107)）。叶子初始 size=1（`size = ones(2*n-1)`），每次合并严格相加，所以任意新簇的 size 就等于它包含的原始观测数。例如上例 `Z[1,3]=3` = size[obs3]=1 + size[簇4]=2。

**练习 3**：n=4 时，整棵树一共有多少个节点（叶子 + 内部节点）？最大编号是多少？

**答案**：共 `2n−1 = 7` 个节点（4 叶 + 3 内部），编号 `0 … 2n−2 = 0…6`。这与 `LinkageUnionFind` 构造时 `parent = arange(2*n - 1)`（[pyx L1097](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1096-L1099)）的长度一致。

---

### 4.3 两种输入：压缩距离矩阵与观测矩阵（num_obs_y / is_valid_y）

#### 4.3.1 概念说明

`linkage` 的第一个参数 `y` 很「双面」——它接受两种完全不同的输入：

1. **压缩距离矩阵（condensed distance matrix）**：一维（`ndim==1`），长度为 \(\binom{n}{2}=n(n-1)/2\)，是 `scipy.spatial.distance.pdist` 的输出。它只存方阵的上三角（不含对角线），按行优先展开：

   \[
     \text{condensed}(i,j)\text{ 的位置} = n\cdot i - \frac{i(i+1)}{2} + (j-i-1),\quad i<j
   \]

   例如 n=4 时，6 个元素依次是 (0,1),(0,2),(0,3),(1,2),(1,3),(2,3) 的距离。

2. **观测矩阵（observation matrix）**：二维（`ndim==2`），M×N，每行一个观测。此时 `linkage` 会**先用 `metric` 算 `pdist`** 把它转成压缩距离矩阵，再聚类。

为什么要支持两种输入？因为很多时候你**已经有距离**（比如基因间的演化距离、自定义相似度），没必要再回到原始坐标；而另一些时候你只有**原始特征**，让 scipy 帮你算距离更方便。

但这里有个**大坑**：当你想用 `centroid`/`median`/`ward` 这三种方法时，它们**只在欧氏距离下数学上正确**（因为它们要算「质心」）。如果你直接喂一个非欧氏的预计算距离矩阵，scipy 没法替你检查，结果会**静默地错**。源码里用一个元组 `_EUCLIDEAN_METHODS` 标记这三种方法。

还有一个小坑：如果你本意是传观测矩阵，却不小心传了一个「n×n 的对称、对角为 0、非负」的方阵（典型的没压缩的距离矩阵），scipy 会发一个 `ClusterWarning` 提醒你「这看起来像个没压缩的距离矩阵」。

#### 4.3.2 核心流程

`linkage` 对输入的处理流程可以画成下面这条分支：

```
y = _asarray(y, float64)                       # 先统一成 float64 的 C 序数组
若 method 在 _EUCLIDEAN_METHODS 且 metric != 'euclidean' 且 y 是 2D:
    抛 ValueError                              # centroid/median/ward 必须欧氏
根据 y.ndim 分流:
    ndim == 1 (压缩距离矩阵):
        distance.is_valid_y(y, throw=True)     # 校验长度必须是 binomial(n,2)
    ndim == 2 (观测矩阵):
        若长得像「未压缩的距离方阵」: 发 ClusterWarning
        y = distance.pdist(y, metric)          # 转成压缩距离矩阵
    其它:
        抛 ValueError ("y must be 1 or 2 dimensional")
校验 y 全有限 (无 NaN/Inf)
n = distance.num_obs_y(y)                       # 由压缩矩阵长度反推观测数 n
... 进入 cy_linkage，用 n 和 y 跑 Cython 后端
```

两个关键工具函数都来自 `scipy.spatial.distance`：

- `is_valid_y(y)`：判断一维数组长度是否等于某个 \(\binom{n}{2}\)。
- `num_obs_y(Y)`：由压缩矩阵长度反推出观测数 n。

它们的数学核心都是同一个小技巧——**由长度 k 反解 n**。因为 \(k = n(n-1)/2\)，所以：

\[
  n = \left\lceil \sqrt{2k} \right\rceil
\]

再用 \((d(d-1)/2) == k\) 验证一下是否真的合法（防止 k 不是二项系数的情况，比如 k=7）。

#### 4.3.3 源码精读

`linkage` 里分流 1D / 2D 输入、并调用校验的代码：

[hierarchy/_hierarchy_impl.py:930-946](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L930-L946) —— 中文复述：`centroid/median/ward` 若配了非欧氏 metric 且输入是 2D 观测矩阵，直接抛错；`y.ndim==1` 时调 `distance.is_valid_y(y, throw=True, name='y')` 校验是合法压缩距离矩阵；`y.ndim==2` 时若发现「对角全 0、非负、对称」就发 `ClusterWarning`（疑似未压缩的距离方阵），然后用 `distance.pdist(y, metric)` 转成压缩形式；其它维度抛错。

`_EUCLIDEAN_METHODS` 与七种方法的编码字典：

[hierarchy/_hierarchy_impl.py:51-53](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L51-L53) —— 中文复述：`_LINKAGE_METHODS` 把七种方法名映射成整数码（single=0、complete=1、average=2、centroid=3、median=4、ward=5、weighted=6），这些码会传给 Cython 后端；`_EUCLIDEAN_METHODS = ('centroid', 'median', 'ward')` 标记「只允许欧氏」的三种。

然后是用压缩矩阵长度反推 n 的那一行：

[hierarchy/_hierarchy_impl.py:952-953](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L952-L953) —— 中文复述：`n = distance.num_obs_y(y)`，由压缩距离矩阵的长度算出原始观测数 n；紧接着 `method_code = _LINKAGE_METHODS[method]` 取方法码。这个 n 之后会传给 `mst_single_linkage(y, n)` 等后端，也决定了 Z 的行数 n−1。

现在去看 `num_obs_y` 本体，它就是上面那个 \(\lceil\sqrt{2k}\rceil\) 技巧：

[spatial/distance.py:2558-2568](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/spatial/distance.py#L2558-L2568) —— 中文复述：先 `_asarray(Y)`，调 `is_valid_y(Y, throw=True)` 保证合法；取 `k = Y.shape[0]`，算 `d = int(ceil(sqrt(k*2)))`，再用 `(d*(d-1)/2) != k` 复核，合法则返回 `d` 即观测数 n。

再看 `is_valid_y` 的校验逻辑：

[spatial/distance.py:2482-2500](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/spatial/distance.py#L2482-L2500) —— 中文复述：要求 `len(y.shape) == 1`（必须一维）；取 `n = y.shape[0]`（这里变量名 n 实为长度 k），算 `d = ceil(sqrt(n*2))`，若 `(d*(d-1)/2) != n` 则不合法；`throw=True` 时直接抛异常，`warning=True` 时只告警，否则返回 False。`try/except` 把上面这些校验包起来，由 throw/warning 决定如何对外报告。

最后，压缩矩阵里 (i,j) 到下标的映射在 Cython 里是这样实现的（这与 `pdist` 的展开顺序一致）：

[hierarchy/_hierarchy.pyx:20-29](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L20-L29) —— 中文复述：`condensed_index(n, i, j)`：若 `i < j` 返回 `n*i - i*(i+1)/2 + (j-i-1)`；若 `i > j` 就把 i、j 互换再用同一公式（保证对称距离取一次）。这正是 MST 等后端在压缩向量里随机访问某个 (i,j) 距离时用的下标函数。

#### 4.3.4 代码实践

**目标**：体会「两种输入等价」「n 由长度反推」「is_valid_y 拦截非法长度」三件事。

**操作步骤**：

```python
import numpy as np
from scipy.cluster.hierarchy import linkage
from scipy.spatial.distance import pdist, num_obs_y, is_valid_y

# 4 个二维观测
X = np.array([[0., 0.], [0., 1.], [5., 5.], [6., 5.]])

# 方式 A: 直接传观测矩阵
ZA = linkage(X, method='single')

# 方式 B: 先 pdist 得到压缩距离矩阵，再传给 linkage
y = pdist(X)
print("y 的长度 =", len(y), " 期望 =", 4 * 3 // 2)   # 6 = C(4,2)
print("num_obs_y(y) =", num_obs_y(y))                 # 反推出 n = 4
print("is_valid_y(y) =", is_valid_y(y))               # True
ZB = linkage(y, method='single')

print("两种输入结果是否相同:", np.allclose(ZA, ZB))   # 预期 True

# 故意造一个非法长度的向量 (长度 7, 不是任何 binomial(n,2))
bad = np.array([1., 1.2, 1.0, 0.5, 1.3, 0.9, 0.4])
print("is_valid_y(bad) =", is_valid_y(bad))           # 预期 False
try:
    linkage(bad, method='single')
except ValueError as e:
    print("linkage 拒绝非法输入:", e)
```

**需要观察的现象**：

- `len(y) == 6`，即 \(\binom{4}{2}\)。
- `num_obs_y(y) == 4`：仅凭压缩向量的长度 6，就反推出原始观测数 4。
- `is_valid_y(y)` 为 True，`is_valid_y(bad)` 为 False（长度 7 不是任何 \(\binom{n}{2}\)：\(\binom{4}{2}=6\)、\(\binom{5}{2}=10\)，跳过了 7）。
- `ZA` 与 `ZB` 完全相同：说明「传观测矩阵」只是多了一步内部 `pdist`，最终等价。

**预期结果**：

```
y 的长度 = 6  期望 = 6
num_obs_y(y) = 4
is_valid_y(y) = True
两种输入结果是否相同: True
is_valid_y(bad) = False
linkage 拒绝非法输入: Length n of condensed distance matrix 'y' must be a binomial coefficient ...
```

> 待本地验证：异常的具体措辞随版本略有差异，但「长度 7 被拒」「两种输入等价」「num_obs_y 反推为 4」三条不变。

#### 4.3.5 小练习与答案

**练习 1**：`pdist` 对 5 个观测的输出长度是多少？`num_obs_y` 会从它反推出多少？

**答案**：长度 = \(\binom{5}{2}=5\times4/2=10\)。`num_obs_y` 用 `d = ceil(sqrt(10*2)) = ceil(4.47) = 5`，再验 `(5*4/2)==10` ✓，返回 5（[distance.py L2564-L2568](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/spatial/distance.py#L2564-L2568)）。

**练习 2**：为什么长度 7 不是合法的压缩距离矩阵？

**答案**：因为不存在整数 n 使 \(\binom{n}{2}=7\)。\(\binom{4}{2}=6\)、\(\binom{5}{2}=10\)，7 落在中间。`is_valid_y` 算 `d = ceil(sqrt(14)) = 4`，再验 `(4*3/2)=6 != 7`，故判为非法（[distance.py L2489-L2493](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/spatial/distance.py#L2489-L2493)）。

**练习 3**：下面的调用会怎样？

```python
linkage(np.array([[0.,1.],[2.,3.]]), method='ward', metric='cityblock')
```

**答案**：会抛 `ValueError: method=ward requires the distance metric to be Euclidean`。因为 `ward` 在 `_EUCLIDEAN_METHODS` 里，而 `metric='cityblock'`（曼哈顿距离）非欧氏，且输入是 2D 观测矩阵，命中 [L930-L932](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L930-L932) 的检查。如果你改成传一个**预计算的** cityblock 压缩距离矩阵（1D），scipy 不会拦你，但结果在数学上是错的——这就是 docstring Notes 第 2 条警告的内容（[L890-L894](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L890-L894)）。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「纸上 + 代码」双验证的小任务。

**任务**：给定 4 个一维观测 `[[2],[8],[0],[4]]`，请：

1. **手算**：写出它们的两两距离方阵，再按上三角展开成压缩距离向量 `y`（长度应为 6），并指出每个元素对应哪对 (i,j)。
2. **手推**：用 single linkage 逐步合并，写出 3×4 的 Z，标出每行的新簇编号（应为 4、5、6）。
3. **反推**：不看数据，仅凭 `len(y)=6` 用 `num_obs_y` 的公式算 n，验证 `n=4`。
4. **代码核对**：运行下面这段，逐元素对比你的手算与 scipy 输出：

   ```python
   import numpy as np
   from scipy.cluster.hierarchy import linkage, leaves_list
   from scipy.spatial.distance import pdist, num_obs_y, is_valid_y

   X = [[2], [8], [0], [4]]
   y = pdist(X)
   print("压缩距离向量 y =", y)                 # 期望 [6. 2. 2. 8. 4. 4.]
   print("num_obs_y(y) =", num_obs_y(y))        # 期望 4
   print("is_valid_y(y) =", is_valid_y(y))      # 期望 True

   Z = linkage(X, method='single')
   print("Z =\n", Z)
   # 期望:
   # [[0. 2. 2. 2.]
   #  [3. 4. 2. 3.]
   #  [1. 5. 4. 4.]]
   print("距离列 Z[:,2] =", Z[:, 2])            # 期望 [2. 2. 4.]  单调不减
   print("簇大小列 Z[:,3] =", Z[:, 3])          # 期望 [2. 3. 4.]
   print("叶子顺序 leaves_list =", leaves_list(Z))  # 观察最终的叶子排列
   ```

5. **解释**：用一句话说明「为什么 `Z[-1, 3]` 一定等于 n」。

**参考答案要点**：

- y = `[6, 2, 2, 8, 4, 4]`，依次对应 (0,1),(0,2),(0,3),(1,2),(1,3),(2,3)。
- 手推 Z 即 4.2.4 的结果 `[[0,2,2,2],[3,4,2,3],[1,5,4,4]]`。
- `num_obs_y`：`d = ceil(sqrt(6*2)) = ceil(3.464) = 4`，`(4*3/2)=6` 合法，返回 4。
- `Z[-1, 3] == n` 的原因：最后一次合并把所有点合成一簇，而 `size = 两孩子 size 之和`、叶子 size=1，递推下来根簇的 size 就是 n（见 [pyx L1101-L1107](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1101-L1107) 的累加逻辑）。

> 待本地验证：`leaves_list(Z)` 的具体顺序随方法/版本可能不同，重点是 Z 的四列与上述一致。

## 6. 本讲小结

- **凝聚式聚类**是自底向上的：每个观测先自成一簇，每步合并当前最相似的两簇，共进行 **n−1 次**合并，得到一棵二叉树。
- **linkage matrix Z** 是 `(n−1) × 4` 的表：`Z[i,0]/Z[i,1]` 是被合并的两簇编号、`Z[i,2]` 是合并距离、`Z[i,3]` 是新簇包含的原始观测数；新簇编号遵循 **`n + i`** 约定（i 从 0 起），原始观测编号 `0 … n−1`。
- Z 的「形状 (n−1,4)」「`Z[i,0]<Z[i,1]`」「`Z[:,3]` = 两孩子 size 之和」都由后端代码（`shape=(n-1,4)`、`label`、`LinkageUnionFind.merge`）硬性保证。
- `linkage` 接受**两种输入**：一维压缩距离矩阵（`pdist` 输出）或二维观测矩阵（内部再 `pdist`）；输入合法性由 `distance.is_valid_y` 校验，观测数 n 由 `distance.num_obs_y` 从长度反推（\(n=\lceil\sqrt{2k}\rceil\)）。
- `centroid`/`median`/`ward` 三种方法**只允许欧氏距离**，传观测矩阵配非欧氏 metric 会被拦下；但传预计算的非欧氏距离矩阵不会被拦，需自行负责。
- 本讲把 Z 当作「契约 / 数据结构」来读，刻意没碰「七种距离怎么算」和「Cython 算法复杂度」，这两块留给 [u3-l2]、[u3-l4] 与第 4 单元。

## 7. 下一步学习建议

理解了 Z 的结构后，建议按下面的顺序继续：

1. **[u3-l2 linkage() 总入口与七种方法分发]**：回到 `linkage` 函数本身，看它如何用 `_LINKAGE_METHODS` 字典和一条 `if` 链把请求分派到 `mst_single_linkage` / `nn_chain` / `fast_linkage` 三个 Cython 后端，以及七种方法的编码。
2. **[u3-l3 Python 与 Cython 的桥接]**：搞懂 `lazy_cython` 装饰器与 `xpx.lazy_apply` 如何让 Cython 后端也能吃 dask 惰性数组——这会解释本讲里 `xpx.lazy_apply(cy_linkage, y, ...)`（[L967-L969](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L967-L969)）那一行的来龙去脉。
3. **[u3-l4 Lance-Williams 距离更新公式]**：去看 `_hierarchy_distance_update.pxi`，理解「每次合并后如何更新新簇到其它簇的距离」如何用一个统一递推式表达七种方法——这是凝聚式聚类「最相似」判定的真正数学核心。
4. 之后进入**第 4 单元（Cython 算法）**：本讲里点到为止的 `condensed_index`、`LinkageUnionFind`、`mst_single_linkage`、`label` 会在那里被完整剖析。
