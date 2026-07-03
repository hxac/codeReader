# nearest-neighbor chain 与 fast_linkage

> 本讲是 `scipy.cluster.hierarchy` 层次聚类核心算法的第二讲。在 u4-l1 我们已经搭好了三个底层数据结构（压缩距离矩阵下标、`Heap`、`LinkageUnionFind`），在 u4-l2 我们读完了 `single` 专用的 MST 算法。本讲把剩下的两条算法分支——`nn_chain` 与 `fast_linkage`——一次讲透，并回答一个关键问题：**为什么 `centroid`/`median` 必须走慢一档的 `fast_linkage`，而不能用 `nn_chain`？**

---

## 1. 本讲目标

学完本讲，你应该能够：

1. 看懂 `_hierarchy_impl.py` 中 `cy_linkage` 闭包如何按 `method` 把请求分派到 `mst_single_linkage` / `nn_chain` / `fast_linkage` 三条后端分支。
2. 手推 **最近邻链（nearest-neighbor chain）算法** 的「建链 → 找互为最近邻（RNN）→ 合并 → 更新距离」循环，并解释它为何能在 \(O(n^2)\) 完成。
3. 读懂 `find_min_dist` 这个被 `fast_linkage` 反复调用的 \(O(n)\) 线性扫描，以及它返回的 `Pair` 结构。
4. 理解 **fast_linkage（Müllner 的 Generic Clustering Algorithm）** 如何用一个支持「改值」的最小堆（`Heap`）维护「最近邻下界」，在最坏 \(O(n^3)\)、最优 \(O(n^2)\) 之间运行。
5. 用 **可还原性（reducibility）** 这一数学性质，解释 `centroid`/`median` 为何不能用 `nn_chain`、而 `ward` 却可以。

---

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义）：

- **linkage matrix Z** 的形状 (n−1)×4 与四列含义、`n+i` 簇编号约定（u3-l1）。
- **Lance-Williams 距离更新公式**：合并簇 \(x,y\) 后，新簇 \(x\cup y\) 到任一剩余簇 \(i\) 的距离可仅凭 \(d(x,i), d(y,i), d(x,y)\) 及三个簇大小递推算出，无需回到原始观测（u3-l4）。七个 `_xxx` 函数（`_single/_complete/_average/_centroid/_median/_ward/_weighted`）被钉成统一的函数指针签名 `linkage_distance_update`，并由 `linkage_methods` 表按下标 0..6 与方法编码对齐。
- **压缩距离矩阵** `condensed_index(n, i, j)`：把一维长度 \(n(n-1)/2\) 的数组当二维表用的地址翻译层（u4-l1）。
- **`Heap`**：位于 `_structures.pxi`、支持 \(O(\log n)\) 改值的最小二叉堆，靠 `index_by_key` / `key_by_index` 两张互逆反查表实现（u4-l1）。
- **`LinkageUnionFind` + `label()`**：用于把「按 RNN 顺序产生、未排序、簇号非法」的 Z 重新编号成合法 linkage matrix（u4-l1、u4-l2）。

几个本讲会用到的术语：

- **互为最近邻（RNN, reciprocal nearest neighbors）**：两个簇 \(x, y\) 满足「\(x\) 的最近邻是 \(y\)，且 \(y\) 的最近邻是 \(x\)」。凝聚式聚类的关键观察是：**RNN 对的合并一定是当前合法的下一步合并**（对可还原方法而言），因为不存在第三个簇比它们彼此更近。
- **可还原性（reducibility）**：一种距离更新公式的数学性质。粗略说，它保证「合并两簇不会让结果簇比原来更靠近任何第三个簇」。这正是 `nn_chain` 正确性的基石，也是本讲的灵魂概念，4.1 节会严格展开。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `hierarchy/_hierarchy.pyx` | Cython 性能层。本讲精读其中 `nn_chain`、`fast_linkage`、`find_min_dist` 三个函数，以及作为「朴素参考实现」的 `linkage`。 |
| `hierarchy/_structures.pxi` | 定义 `Pair` 结构体与 `Heap` 类。`fast_linkage` 依赖 `Heap` 的 `get_min`/`change_value`/`remove_min`。 |
| `hierarchy/_hierarchy_distance_update.pxi` | 七个 Lance-Williams 更新函数与函数指针类型。两条算法都通过 `linkage_methods` 表复用它。 |
| `hierarchy/_hierarchy_impl.py` | Python 封装层。本讲引用其中的 `_LINKAGE_METHODS`、`_EUCLIDEAN_METHODS` 常量与 `cy_linkage` 闭包（三路分派）。 |
| `hierarchy/tests/test_hierarchy.py` | 测试。`test_compare_with_trivial` 用朴素 `linkage` 校验三条优化分支结果一致，是本讲综合实践的依据。 |

---

## 4. 核心概念与源码讲解

### 4.1 linkage 方法的三路分发：谁走 nn_chain，谁走 fast_linkage

#### 4.1.1 概念说明

`scipy.cluster.hierarchy.linkage` 对外只暴露一个 `method` 字符串（`single/complete/average/centroid/median/ward/weighted`），但内部却有三条**完全不同**的 Cython 后端算法。决定走哪条的，不是方法名好不好听，而是一个数学性质：**该方法是否「可还原」（reducible）**。

回顾 u3-l2 的分派结论，并细化到本讲的视角：

- `single` → `mst_single_linkage`：\(O(n^2)\)，单链接等价于最小生成树，u4-l2 已讲。
- `complete`、`average`、`weighted`、`ward` → `nn_chain`：\(O(n^2)\)，最近邻链算法。
- `centroid`、`median` → `fast_linkage`：最坏 \(O(n^3)\)，朴素通用算法。

这个分组在 `linkage` 的官方 docstring 里写得明明白白：

> For method 'single', an optimized algorithm based on minimum spanning tree is implemented. It has time complexity \(O(n^2)\). For methods 'complete', 'average', 'weighted' and 'ward', an algorithm called nearest-neighbors chain is implemented. It also has time complexity \(O(n^2)\). For other methods, a naive algorithm is implemented with \(O(n^3)\) time complexity.

#### 4.1.2 核心流程

Python 层在输入规整、`pdist` 折叠、有限性校验之后，把方法名映射成整数 `method_code`，再交给内嵌闭包 `cy_linkage` 做三路分派：

```text
linkage(y, method)
  ├── method_code = _LINKAGE_METHODS[method]          # 'ward' → 5
  ├── def cy_linkage(y, validate): ...
  │       if method == 'single':        → mst_single_linkage
  │       elif method in {complete, average, weighted, ward}: → nn_chain
  │       else (centroid, median):      → fast_linkage
  └── xpx.lazy_apply(cy_linkage, y, ...)              # 惰性桥接，eager 时短路
```

关键一点：分派的依据是 `method`（字符串），而**不是** `method_code`（整数）。`ward` 虽然和 `centroid`/`median` 同属 `_EUCLIDEAN_METHODS`（都要求欧氏度量），但 `ward` 可还原，因此走 `nn_chain`；`centroid`/`median` 不可还原，只能走 `fast_linkage`。这一点务必和「是否欧氏」区分开——欧氏只决定能不能用，可还原性才决定走哪条算法。

#### 4.1.3 源码精读

先看方法编码常量与「欧氏方法」集合（注意两者都包含 `ward`，但本讲的算法分组与它无关）：

[hierarchy/_hierarchy_impl.py:51-53](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L51-L53) — 把七种方法名映射为 0..6 整数编码，并标记 `centroid/median/ward` 必须配欧氏度量。

再看三路分派本身，这是本讲的「调度中枢」：

[hierarchy/_hierarchy_impl.py:955-965](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L955-L965) — `cy_linkage` 闭包：`single` 走 MST，四个可还原方法走 `nn_chain`，其余（即 `centroid/median`）走 `fast_linkage`。

闭包外层 `validate` 参数承担惰性数组的二次有限性检查（eager 输入已在更早处校验过一次）；最终由 `xpx.lazy_apply` 决定是直接调用（短路）还是注册进 dask 任务图：

[hierarchy/_hierarchy_impl.py:967-969](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L967-L969) — 对普通 NumPy 输入，`lazy_apply` 走快路径直接调用 `cy_linkage`，整层桥接近乎透明。

docstring 里关于复杂度的那段说明也值得直接定位，它就是本讲综合实践的「命题」：

[hierarchy/_hierarchy_impl.py:880-889](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L880-L889) — 官方对各方法时间/空间复杂度的承诺：single 与 nn_chain 为 \(O(n^2)\)，其余为 \(O(n^3)\)，内存统一 \(O(n^2)\)。

#### 4.1.4 代码实践（阅读型）

**实践目标**：在源码里亲手画出「方法 → 算法 → 复杂度」三栏对照表，确认分派逻辑。

**操作步骤**：

1. 打开 [hierarchy/_hierarchy_impl.py:51-53](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L51-L53)，记下每个方法的整数编码。
2. 打开 [hierarchy/_hierarchy_impl.py:960-965](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L960-L965)，逐行确认 `method` 落到哪个分支。
3. 对照 [hierarchy/_hierarchy_impl.py:880-889](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L880-L889) 的复杂度描述，填出下表：

| method | code | 可还原? | 欧氏? | 后端算法 | 复杂度 |
|--------|------|---------|-------|----------|--------|
| single | 0 | 是 | 否 | `mst_single_linkage` | \(O(n^2)\) |
| complete | 1 | 是 | 否 | `nn_chain` | \(O(n^2)\) |
| average | 2 | 是 | 否 | `nn_chain` | \(O(n^2)\) |
| centroid | 3 | **否** | 是 | `fast_linkage` | \(O(n^3)\) |
| median | 4 | **否** | 是 | `fast_linkage` | \(O(n^3)\) |
| ward | 5 | 是 | 是 | `nn_chain` | \(O(n^2)\) |
| weighted | 6 | 是 | 否 | `nn_chain` | \(O(n^2)\) |

**需要观察的现象**：`ward` 与 `centroid/median` 一样都是欧氏方法，但 `ward` 走的是 `nn_chain`，另两个走 `fast_linkage`——这证明「欧氏」与「可还原」是两个正交的概念。

**预期结果**：上表与 docstring 的复杂度描述完全吻合。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `cy_linkage` 里的 `elif` 条件误写成 `method in ('complete', 'average', 'weighted', 'centroid')`（把 `centroid` 错放进 `nn_chain`、把 `ward` 错放进 `fast_linkage`），会发生什么？

**参考答案**：`centroid` 不可还原，用 `nn_chain` 会得到**错误**的 Z（RNN 不再保证是合法的下一步合并，可能出现距离倒挂/簇结构错误）；`ward` 改走 `fast_linkage` 结果仍正确，只是从 \(O(n^2)\) 退化到最坏 \(O(n^3)\)，变慢但不错。这正是为什么分派必须按可还原性而非方法名分组。

**练习 2**：`method_code` 这个整数在哪里被真正使用？字符串 `method` 又在哪里被使用？

**参考答案**：`method_code` 传给 Cython 后端 `nn_chain(y, n, method_code)` / `fast_linkage(y, n, method_code)`，在那里被用作 `linkage_methods[method]` 函数指针表的下标（见 4.2.3、4.4.3）。字符串 `method` 只在 Python 层 `cy_linkage` 里用来**选择算法分支**。即：字符串选算法，整数选距离公式。

---

### 4.2 nn_chain：最近邻链算法

#### 4.2.1 概念说明

`nn_chain` 实现的是经典的**最近邻链算法**（Benzécri 1982、Juan 1982，由 Müllner 在 arXiv:1109.2378 中现代化重述）。它的核心思想极其优雅：

> 维护一条「每个元素都是上一个元素的最近邻」的链（栈）。当链顶两元素**互为最近邻**（RNN）时，立即合并它们；否则把当前链顶的最近邻压入链中，继续延伸。

为什么这样一定能找到合法的合并？因为有限距离下，一条严格递减的最近邻链不可能无限延伸——它最终必然在某个 RNN 对上「碰头」（否则就会形成距离严格递减的环，矛盾）。而**对可还原方法**，一个 RNN 对的合并就是当前全局合法的下一步（不存在第三个簇比这对彼此更近，且合并后也不会凭空冒出更近的对手）。这就是为什么 `nn_chain` 只能用于可还原方法。

每一步「找最近邻」是 \(O(n)\) 的线性扫描，一共做 \(O(n)\) 次合并、链的总延伸次数也是 \(O(n)\) 量级，故总体 \(O(n^2)\)。注意：`nn_chain` **不使用 `Heap`**——它靠「链 + 偏好链内元素避免成环」的技巧，把堆省掉了。

#### 4.2.2 核心流程

```text
初始化: D = dists 的副本; size[i] = 1 (0 表示已丢弃); new_dist = linkage_methods[method]
for k in 0..n-2:                       # 共 n-1 次合并
    if 链空: 任选一个活跃簇作为链底
    while True:                        # 延伸链直到链顶是 RNN
        x = 链顶
        current_min = (链长>1) ? D[x, 链次顶] : +∞   # 偏好链内上一个元素
        for 每个活跃簇 i (i != x):
            if D[x, i] < current_min:  y = i; current_min = D[x,i]   # 找 x 的真最近邻 y
        if 链长>1 且 y == 链次顶:  break           # x 与链次顶互为最近邻 → RNN，停止延伸
        把 y 压入链; 继续
    # 此时 x, y 是 RNN，合并
    弹出 x, y (chain_length -= 2)
    if x > y: 交换                     # fastcluster 约定：保证 Z[k,0] < Z[k,1]
    记录 Z[k] = (x, y, current_min, size[x]+size[y])
    size[x] = 0; size[y] = size[x]+size[y]          # 丢弃 x，y 接管为新簇
    for 每个活跃簇 i (i != y):
        D[i, y] = new_dist(D[i,x], D[i,y], current_min, size_x, size_y, size_i)  # Lance-Williams
# 收尾: Z 是按 RNN 顺序产生的，簇号非法、未必升序
按距离 mergesort 排序 Z
label(Z, n)                            # 用 LinkageUnionFind 重排簇号、回填簇大小
return Z
```

两个要点：①「偏好链内上一个元素」体现在把 `current_min` 初值设为 `D[x, 链次顶]` 而非 `+∞`，配合严格 `<` 比较，等距时优先选链内元素，既能更快碰到 RNN、又能避免在等距簇间反复横跳成环；②合并后**只更新 D、不重排链**——被弹出的两个簇从链顶消失，新的链顶继续往下找它的最近邻，链因此自然收缩与延伸。

#### 4.2.3 源码精读

整个函数骨架：

[hierarchy/_hierarchy.pyx:924-948](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L924-L948) — `nn_chain` 函数签名与初始化：`D` 是距离副本（会被原地改写），`size` 标记簇是否活跃，`new_dist` 通过 `linkage_methods[method]` 取出当前方法的 Lance-Williams 函数指针。

链的存储与主循环框架：

[hierarchy/_hierarchy.pyx:951-963](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L951-L963) — `cluster_chain` 是一条用数组+`chain_length` 模拟的栈；链空时随便挑一个 `size[i]>0` 的簇作链底。

「延伸链 + 检测 RNN」的内层循环是算法的心脏：

[hierarchy/_hierarchy.pyx:966-990](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L966-L990) — 先用链次顶初始化 `current_min`（偏好链内元素、避免成环），再线性扫描所有活跃簇找 `x` 的真最近邻 `y`；若 `y` 恰为链次顶则 `break`（检测到 RNN），否则把 `y` 压栈继续延伸。

合并、记录 Z、丢弃旧簇：

[hierarchy/_hierarchy.pyx:992-1009](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L992-L1009) — `chain_length -= 2` 弹出 RNN 对；`if x > y` 交换是 fastcluster 约定（保证 `Z[k,0]<Z[k,1]`）；`size[x]=0` 丢弃 `x`，`size[y]=nx+ny` 让 `y` 槽位代表新簇。

Lance-Williams 距离更新——这就是 u3-l4 那套公式的实际调用点：

[hierarchy/_hierarchy.pyx:1011-1020](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1011-L1020) — 对每个活跃簇 `i`，用 `new_dist(...)` 把 `D[i,y]` 原地更新为新簇到 `i` 的距离；注意它复用了 `condensed_index` 把一维 `D` 当二维表用。

最后是 `nn_chain` 区别于 `fast_linkage` 的关键收尾——**排序 + label**：

[hierarchy/_hierarchy.pyx:1022-1029](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1022-L1029) — 因为合并是按 RNN 发现顺序产生的（既非升序、簇号也非法），必须先按距离 `mergesort`（稳定排序，保等距语义），再用 `label()`（依赖 `LinkageUnionFind`）重排簇号、回填 `Z[:,3]` 簇大小。

`label` 本身很短，它正是 u4-l1 讲过的 `LinkageUnionFind` 的唯一调用者：

[hierarchy/_hierarchy.pyx:1121-1133](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1121-L1133) — 逐行扫描 Z，用并查集 `find` 把旧簇号翻译成「当前活跃代表」，保证小的在左，`merge` 申请新父节点 `n+i` 并返回合并后簇大小填入第四列。

#### 4.2.4 代码实践（阅读 + 手推型）

**实践目标**：在一个 5 点小数据集上，手推 `nn_chain(method='single')` 的建链过程，验证它与 SciPy 输出一致。

**操作步骤**：

```python
# 示例代码（非项目原有代码）
import numpy as np
from scipy.cluster.hierarchy import linkage
from scipy.spatial.distance import pdist

X = np.array([[0.0], [1.0], [3.0], [7.0], [8.0]])   # 5 个一维点
y = pdist(X)                                         # 压缩距离: [1,3,7,8, 2,6,7, 4,5, 1]
Z = linkage(y, method='single')
print(Z)
```

**手推链**（`single` 下 `new_dist=min`）：

1. 链空 → 链底 = 簇 0。链：`[0]`
2. 链顶 0 的最近邻：d(0,1)=1 最小 → y=1。1≠链次顶（链长=1），压栈。链：`[0,1]`
3. 链顶 1 的最近邻：偏好链次顶 0，d(1,0)=1；扫其余 d(1,2)=2 都更大 → y=0。y==链次顶 → **RNN (0,1)**，break。合并 0,1 → 新簇 5（距离 1）。链：`[0]`（弹掉 0,1，剩空，但下一轮会重选链底）

   > 实际上弹出后链长变 0，下一轮 k 循环重新挑链底。读者可继续推 (7,8) 这对距离也是 1 的 RNN，再逐步合并。

**需要观察的现象**：每次合并都发生在「互为最近邻」的链顶两元素上；链在延伸与收缩之间交替。

**预期结果**：手推得到的合并序列与 `Z`（按距离稳定排序后）一致；最前两行应是距离为 1 的 (0,1) 与 (3,4 对应的) 合并。具体数值「待本地验证」（取决于你给的点），但**结构**应吻合。

#### 4.2.5 小练习与答案

**练习 1**：`nn_chain` 为什么不需要 `Heap`？它用什么机制替代了「快速取全局最小对」？

**参考答案**：它用「最近邻链 + RNN 检测」替代。链保证链顶两元素一旦互为最近邻就是合法合并，无需全局扫描找最小对；找单个簇的最近邻只需 \(O(n)\) 线性扫描。代价是这条技巧**只在可还原方法上正确**。

**练习 2**：合并后 `size[x]=0`、`size[y]=nx+ny`，为什么是「丢弃 x、用 y 代表新簇」而不是新建一个槽位？

**参考答案**：`D` 是固定大小的压缩矩阵，按下标 0..n−1 寻址，无法动态扩列。复用 `y` 槽位（把新簇的距离写进 `D[*,y]`）既省空间又免去重编号；`x` 槽位用 `size[x]=0` 标记为「死」，后续扫描自动跳过。最终正确的簇号（`n+k`）由收尾的 `label()` 重新赋予。

**练习 3**：为什么收尾要用 `kind='mergesort'` 而不是默认的快速排序？

**参考答案**：`mergesort` 是**稳定排序**。当存在等距合并时（如上例两个距离都是 1 的 RNN 对），稳定性能保证它们的相对顺序与 `nn_chain` 发现顺序一致，从而使 `label()` 重建的簇结构与文献/参考实现完全对齐；不稳定排序可能打乱等距项顺序，导致 Z 行序差异（虽不影响聚类正确性，但会破坏与固定测试数据的逐元素比对）。

---

### 4.3 find_min_dist：压缩矩阵上的线性最近邻

#### 4.3.1 概念说明

`find_min_dist` 是 `fast_linkage` 的「基本动作」：在压缩距离矩阵 `D` 中，找出某个簇 `x` 的最近邻。它返回一个 `Pair` 结构体（`{key, value}`），`key` 是最近邻的簇号、`value` 是距离。

它有一个乍看奇怪的设计：**只扫描下标大于 `x` 的簇**（`for i in range(x+1, n)`）。这是因为 `fast_linkage` 维护的 `neighbor`/`min_dist` 数组只记录「向高序号方向的最近邻」（数组长度只有 n−1，最后一个簇没有比自己序号大的邻居）。这是一种约定，配合 `fast_linkage` 的「下界」机制协同工作（见 4.4）。

#### 4.3.2 核心流程

```text
find_min_dist(n, D, size, x):
    current_min = +∞; y = -1
    for i in x+1 .. n-1:
        if size[i] == 0:  continue           # 跳过已丢弃的簇
        dist = D[condensed_index(n, x, i)]
        if dist < current_min:
            current_min = dist; y = i
    if y == -1:  raise ValueError(...)       # x 没有任何活着的「高序号」邻居 → 输入非法
    return Pair(y, current_min)
```

复杂度 \(O(n)\)。它在 `fast_linkage` 里被调用 \(O(n)\) 到 \(O(n^2)\) 次，因此是决定 `fast_linkage` 总体复杂度的因子。

#### 4.3.3 源码精读

[hierarchy/_hierarchy.pyx:768-789](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L768-L789) — `find_min_dist`：只扫 `x+1..n-1`，跳过 `size[i]==0` 的死簇，用 `condensed_index` 把一维 `D` 当二维表读，找不到任何邻居时报错（提示输入含负/无穷/NaN 距离），最后返回 `Pair(y, current_min)`。

`Pair` 结构体本身定义在 `_structures.pxi`，与 `Heap` 同住一个文件：

[hierarchy/_structures.pxi:5-7](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_structures.pxi#L5-L7) — `Pair` 是 `{int key; double value}` 的 C 结构体，`find_min_dist` 用它把「最近邻簇号 + 距离」作为一个值返回，避免 Python 元组的装箱开销。

#### 4.3.4 代码实践（阅读型）

**实践目标**：理解 `find_min_dist` 的「只看高序号」约定，并确认它不会漏掉任何邻居。

**操作步骤**：

1. 阅读 [hierarchy/_hierarchy.pyx:774-781](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L774-L781)，确认循环范围是 `range(x+1, n)`。
2. 思考：若 `x` 的真正最近邻是某个 `i < x`，`find_min_dist` 会不会漏掉它？

**需要观察的现象**：在 `fast_linkage` 的整体设计里，`i < x` 方向的邻居关系由「下界更新」阶段（4.4 节的步骤 5）维护——当一个低序号簇 `z` 的邻居被并入 `y` 时，会主动用 `D[z,y]` 更新 `min_dist[z]`。所以 `find_min_dist` 只负责「高序号方向」即可，两者合起来覆盖全部邻居。

**预期结果**：理解到 `find_min_dist` 不是「找全局最近邻」，而是「在 fast_linkage 的下界框架下，补全高序号方向的最近邻」，这是一个分工。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `find_min_dist` 找不到任何邻居时要抛 `ValueError`，而不是返回一个哨兵值？

**参考答案**：在合法输入下，只要还有 ≥2 个活跃簇，任何活跃簇都至少有一个活跃邻居。找不到邻居意味着 `D` 里含负数、`inf` 或 `NaN`（使所有距离比较失效），属于输入非法，应立即报错而非静默返回错误结果。错误信息也明确提示「检查距离是否含负/无穷/NaN」。

**练习 2**：`find_min_dist` 用的是严格 `<` 比较。如果改成 `<=`，会对 `fast_linkage` 产生什么影响？

**参考答案**：等距时 `<=` 会偏向**后**扫到的（序号更大的）簇，改变等距情况下的邻居选择，进而可能改变合并顺序。由于 `fast_linkage` 末尾不像 `nn_chain` 那样强制 mergesort+label（它直接按合并顺序产出已排序的 Z），这种改变可能让等距项的 Z 行序与参考实现不一致，破坏测试比对。因此严格 `<` 配合「先入为主」是有意为之的稳定性选择。

---

### 4.4 fast_linkage：带堆的朴素通用算法

#### 4.4.1 概念说明

`fast_linkage` 实现的是 Müllner 论文（arXiv:1109.2378）中的 **Generic Clustering Algorithm**。它是为「不可还原」方法（`centroid`/`median`）准备的兜底算法——因为这类方法不能用 `nn_chain`，只能老老实实「每轮找全局最近对、合并、更新全部距离」。

朴素做法每轮找最近对要 \(O(n^2)\)，共 \(n-1\) 轮 → \(O(n^3)\)。`fast_linkage` 的优化在于：为每个簇缓存一个「最近邻候选 + 距离下界」，把这些下界塞进一个支持 **\(O(\log n)\) 改值** 的最小堆（`Heap`），于是「取全局最近对」变成 \(O(\log n)\)，只有在缓存失效时才回退到 \(O(n)\) 的 `find_min_dist` 重算。最优情况（缓存几乎不失效）整体 \(O(n^2)\)，最坏情况（频繁失效）仍是 \(O(n^3)\)。

它和 `nn_chain` 的关系可以一句话概括：

| 维度 | nn_chain | fast_linkage |
|------|----------|--------------|
| 适用方法 | 可还原（complete/average/weighted/ward） | 全部，但实际只用于不可还原（centroid/median） |
| 选最近对机制 | 最近邻链 + RNN 检测 | `Heap` 缓存的「最近邻下界」 |
| 用 `Heap`？ | 否 | 是 |
| 用 `label()`？ | 是（产出无序） | 否（产出已排序、簇号合法） |
| 复杂度 | \(O(n^2)\) | \(O(n^2)\) 最优 / \(O(n^3)\) 最坏 |

#### 4.4.2 核心流程

```text
初始化:
    D = dists 副本; size[i]=1; cluster_id[i]=i
    neighbor[x], min_dist[x] = find_min_dist(x)    # 每个簇向高序号方向的最近邻下界
    min_dist_heap = Heap(min_dist)                 # 把下界堆化
    new_dist = linkage_methods[method]

for k in 0..n-2:
    # ① 选出全局最近对（带惰性校验）
    for i in 0..n-k-1:                             # 理论上是 while True，这里用定长循环规避浮点坑
        x, dist = min_dist_heap.get_min()
        y = neighbor[x]
        if dist == D[x, y]:  break                 # 缓存仍与 D 一致 → 这就是真·全局最小
        # 缓存失效，重算 x 的最近邻并刷新堆
        y, dist = find_min_dist(x); neighbor[x]=y; min_dist[x]=dist; heap.change_value(x, dist)
    min_dist_heap.remove_min()                     # x 即将被丢弃，移出堆

    # ② 记录合并
    id_x, id_y = cluster_id[x], cluster_id[y]      # 用「逻辑簇号」写进 Z
    Z[k] = (min(id_x,id_y), max(id_x,id_y), dist, size[x]+size[y])
    size[x]=0; size[y]=size[x]+size[y]; cluster_id[y]=n+k

    # ③ Lance-Williams 更新 D（新簇 y 到所有活跃簇）
    for 每个活跃 z (z != y):
        D[z,y] = new_dist(D[z,x], D[z,y], dist, size_x, size_y, size_z)

    # ④ 把原本指向 x 的低序号邻居「逻辑迁移」到 y
    for z in 0..x-1:  if neighbor[z]==x:  neighbor[z]=y

    # ⑤ 更新低序号方向的下界（D[z,y] 可能比原 min_dist[z] 更小）
    for z in 0..y-1:
        if size[z]>0 and D[z,y] < min_dist[z]:
            neighbor[z]=y; min_dist[z]=D[z,y]; heap.change_value(z, ...)

    # ⑥ 为新簇 y 重算高序号方向的最近邻
    if y < n-1:  y 的 neighbor/min_dist = find_min_dist(y); 刷新堆

return Z   # 注意：fast_linkage 直接返回，无需 sort+label
```

核心是步骤 ① 的「惰性校验」：堆里的 `min_dist[x]` 只是个**下界**，`D` 被 Lance-Williams 改写后它可能过期。取出堆顶后，检查 `min_dist[x] == D[x, neighbor[x]]` 是否 still holds——若成立，说明这个下界仍是 `x` 的真实最近邻距离，且因为是堆顶，它就是全局最小，可以放心合并；若不成立，重算并刷新堆，再取下一个堆顶。Müllner 证明至多 \(n-k\) 次刷新必能收敛，故外层用 `for i in range(n-k)` 而非 `while True`（注释明说这是为了在浮点环境下更「靠谱」）。

#### 4.4.3 源码精读

初始化与堆构建：

[hierarchy/_hierarchy.pyx:819-845](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L819-L845) — `D` 距离副本、`size`、`cluster_id`（记录每个槽位对应的「逻辑簇号」用于写 Z）、`neighbor`/`min_dist`（向高序号方向的最近邻下界，长度 n−1）；逐簇调 `find_min_dist` 填初值后，用 `Heap(min_dist)` 线性时间堆化。

步骤 ①「惰性校验选最小对」——这是 `fast_linkage` 最精巧的一段：

[hierarchy/_hierarchy.pyx:847-866](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L847-L866) — `for i in range(n-k)` 是带界的「理论 while True」；取堆顶 `(x, dist)`，若 `dist == D[x, neighbor[x]]` 则缓存有效、`break` 合并；否则 `find_min_dist` 重算 `x` 的最近邻，`change_value` 刷新堆；最终 `remove_min` 把将丢弃的 `x` 移出堆。

步骤 ②③「记录合并 + Lance-Williams 更新」：

[hierarchy/_hierarchy.pyx:868-893](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L868-L893) — 用 `cluster_id` 的逻辑簇号写 Z（保证 `id_x<id_y`），更新 `size`/`cluster_id`，然后用 `new_dist(...)` 把新簇 `y` 到每个活跃簇的距离原地写回 `D`。

步骤 ④⑤⑥「邻居迁移 + 下界更新 + 为 y 重算」：

[hierarchy/_hierarchy.pyx:895-919](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L895-L919) — 把指向被丢弃 `x` 的低序号邻居迁到 `y`（逻辑猜测）；对低序号簇用 `D[z,y]` 收紧下界（若更小则更新堆）；最后为 `y` 调一次 `find_min_dist` 重算高序号方向最近邻。三段合起来保证下一轮堆里每个簇的下界仍有效或可被惰性校正。

最后直接 `return Z.base`（无 sort、无 label）：

[hierarchy/_hierarchy.pyx:921](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L921) — `fast_linkage` 每轮都选全局最近对合并，产出天然按距离升序、簇号合法的 Z，因此无需 `nn_chain` 那套收尾。

支撑这一切的 `Heap`，关键是 `change_value`（u4-l1 详述）：

[hierarchy/_structures.pxi:53-60](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_structures.pxi#L53-L60) — `change_value(key, value)` 靠 `index_by_key[key]` 一次定位到元素在堆数组中的位置，改值后按新旧大小关系选择 `sift_up` 或 `sift_down` 恢复堆序，全程 \(O(\log n)\)。这正是 `heapq` 做不到、而 `fast_linkage` 必须自定义堆的原因。

#### 4.4.4 代码实践（验证型：朴素参考实现 vs fast_linkage）

**实践目标**：用 SciPy 自带的「朴素 \(O(n^3)\) 参考」校验 `fast_linkage`（及另外两条分支）结果一致。

**操作步骤**：

`_hierarchy.pyx` 里其实还藏着一个朴素 \(O(n^3)\) 的 `linkage(dists, n, method)` 函数，它每轮用双重循环暴力扫整个 `D` 找最近对（[hierarchy/_hierarchy.pyx:686-765](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L686-L765)）。它不被生产路径调用，只作为**测试里的 ground truth**。我们可以复现这个测试思路：

```python
# 示例代码（非项目原有代码），思路同 test_hierarchy.py::test_compare_with_trivial
import numpy as np
from scipy.cluster.hierarchy import linkage
from scipy.spatial.distance import pdist
from scipy.cluster import _hierarchy            # 编译后的 Cython 后端

rng = np.random.RandomState(0)
X = rng.rand(20, 2)
d = pdist(X)
n = X.shape[0]

for method, code in [('centroid', 3), ('median', 4)]:
    Z_fast = linkage(d, method)                 # 走 fast_linkage
    Z_trivial = _hierarchy.linkage(d, n, code)  # 走朴素 O(n^3) 参考
    print(method, np.allclose(Z_fast, Z_trivial, rtol=1e-14, atol=1e-15))
```

**需要观察的现象**：两种实现路径完全不同（一个用堆+下界，一个暴力双循环），但结果在极高精度（`rtol=1e-14`）下完全一致。

**预期结果**：两个 method 都打印 `True`。这正是 [hierarchy/tests/test_hierarchy.py:123-132](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/tests/test_hierarchy.py#L123-L132) `test_compare_with_trivial` 对全部 7 种方法做的事——用朴素实现给三条优化分支兜底。具体运行「待本地验证」。

> 旁注：`centroid`/`median` 不可还原，意味着它们的 linkage matrix **可能出现距离倒挂**（孩子合并高度大于父节点），即非单调。朴素 `linkage` 与 `fast_linkage` 都会忠实地产生这种「合法但非单调」的 Z，两者因此必须逐元素一致。

#### 4.4.5 小练习与答案

**练习 1**：步骤 ① 里，为什么取出堆顶后还要检查 `dist == D[x, y]`？堆顶难道不就是全局最小吗？

**参考答案**：堆里存的是**下界**，不是实时距离。Lance-Williams 更新会改写 `D`，但堆里的 `min_dist[x]` 不会自动同步（同步代价太大）。所以堆顶的 `dist` 可能是过期的：它或许基于一个已被改写的 `D[x, neighbor[x]]`。检查 `dist == D[x, y]` 就是在问「这个下界还作数吗」；只有作数时它才是真·全局最小，否则重算。这是「惰性求值」的典型套路。

**练习 2**：`fast_linkage` 末尾不调用 `label()`，凭什么保证 Z 的簇号合法、第四列簇大小正确？

**参考答案**：因为它每轮都选**全局最近对**合并，并用 `cluster_id[y]=n+k` 即时赋予新簇正确的 `n+k` 编号、用 `size[y]=nx+ny` 即时算出簇大小。合并顺序天然升序、簇号即时合法，所以无需 `nn_chain` 那种事后重建。代价是「选全局最近对」比「找 RNN」更贵。

**练习 3**：既然 `fast_linkage` 对**所有**方法都正确，为什么不干脆对 7 种方法都用它，省去 `nn_chain`？

**参考答案**：性能。`fast_linkage` 最坏 \(O(n^3)\)，而 `nn_chain` 稳定 \(O(n^2)\)。对 `complete/average/weighted/ward` 这些可还原方法，用 `nn_chain` 能快一个量级（尤其 ward 在大数据集上常见）。`fast_linkage` 只在「不得不」时（不可还原的 centroid/median）才启用。这是「正确性兜底 + 性能优先」的典型分层设计。

---

## 5. 综合实践

**任务**：把本讲的核心命题——「分派依据是可还原性」——在源码与运行结果两个层面同时验证。

**步骤**：

1. **定位三条分支**。在 [hierarchy/_hierarchy_impl.py:960-965](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L960-L965) 找到 `single`→`mst_single_linkage`、四方法→`nn_chain`、`centroid/median`→`fast_linkage` 的分派；在 [hierarchy/_hierarchy.pyx:924](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L924) 与 [hierarchy/_hierarchy.pyx:792](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L792) 找到两个算法本体。

2. **用 docstring 的复杂度承诺做基准**。阅读 [hierarchy/_hierarchy_impl.py:880-889](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L880-L889)，确认 single/nn_chain 系 \(O(n^2)\)、fast_linkage 系 \(O(n^3)\)。

3. **运行时验证复杂度差异**。对 `n = 200, 400, 800, 1600` 的随机数据，分别计时 `linkage(X, 'ward')`（走 nn_chain）与 `linkage(X, 'centroid')`（走 fast_linkage）：

   ```python
   # 示例代码（非项目原有代码）
   import time, numpy as np
   from scipy.cluster.hierarchy import linkage
   for n in [200, 400, 800, 1600]:
       X = np.random.RandomState(0).rand(n, 2)
       t = time.perf_counter(); linkage(X, 'ward');      tw = time.perf_counter()-t
       t = time.perf_counter(); linkage(X, 'centroid');  tc = time.perf_counter()-t
       print(f"n={n:5d}  ward(nn_chain)={tw:.3f}s  centroid(fast_linkage)={tc:.3f}s  比值={tc/tw:.1f}")
   ```

   **预期**：`ward` 的时间随 \(n\) 近似线性翻倍（\(O(n^2)\) 的标志：n 翻倍、时间 4 倍），`centroid` 翻得更快（\(O(n^3)\) 的标志：n 翻倍、时间 8 倍），两者比值随 \(n\) 增大而拉开。具体数值「待本地验证」。

4. **回答关键问题**：为什么 `centroid`/`median` 必须用 `fast_linkage` 而不能用 `nn_chain`？

   **参考答案要点**（结合 u3-l4 的公式）：
   - `nn_chain` 的正确性依赖 RNN 对「一定是当前合法的下一步合并」，而这依赖方法的**可还原性**：\(d(x\cup y, i) \geq \min(d(x,i), d(y,i))\)。
   - `complete/average/weighted` 是真实距离的 max / 加权平均，恒满足上式（加权平均 ≥ min）；`ward` 虽在平方距离上递推，但其 size 加权形式也满足可还原性。故这四个能用 `nn_chain`。
   - `centroid`/`median` 在**平方距离**上递推后再开方，合并后新中心可能比原来更靠近第三簇，即 \(d(x\cup y, i) < \min(d(x,i), d(y,i))\) 可能成立——可还原性被破坏。此时 RNN 不再保证是合法合并，`nn_chain` 会出错。可对照 [hierarchy/_hierarchy_distance_update.pxi:45-54](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_distance_update.pxi#L45-L54) 中 `_centroid`/`_median` 公式里那个**被减项**（`\(- size_x*size_y*d_xy*d_xy/(size_x+size_y)\)` 与 `\(- 0.25*d_xy*d_xy`），正是它让结果可以小于 \(\min\)。
   - `fast_linkage` 每轮重新确认全局最近对，不依赖可还原性，因此对任何方法都正确，是 `centroid`/`median` 的唯一安全选择。

---

## 6. 本讲小结

- `cy_linkage` 按 **`method` 字符串**（非编码）三路分派：`single`→MST，`complete/average/weighted/ward`→`nn_chain`，`centroid/median`→`fast_linkage`。分派依据是**可还原性**，与「是否欧氏」正交。
- `nn_chain` 用「最近邻链 + RNN 检测」替代全局找最小对，省掉了堆，稳定 \(O(n^2)\)；靠「偏好链内上一个元素」避免成环；产出无序，需 mergesort + `label()`（`LinkageUnionFind`）收尾。
- `find_min_dist` 是 \(O(n)\) 的线性最近邻扫描，**只看高序号方向**，返回 `Pair` 结构体；它是 `fast_linkage` 的基本动作，决定其复杂度量级。
- `fast_linkage`（Müllner Generic Algorithm）用 `Heap` 缓存每个簇的「最近邻下界」，靠「惰性校验 `dist == D[x,y]`」决定是否重算，最优 \(O(n^2)\)、最坏 \(O(n^3)\)；产出已排序、簇号合法，无需 `label()`。
- 两条算法都通过 `linkage_methods[method]` 函数指针表复用同一套 Lance-Williams 更新公式（u3-l4），区别只在「如何选最近对」与「如何收尾」。
- 可还原性是分派的灵魂：`centroid`/`median` 的平方距离递推含被减项，可能 \(d(x\cup y,i)<\min(d(x,i),d(y,i))\)，破坏 RNN 合法的保证，故只能用 `fast_linkage`。

---

## 7. 下一步学习建议

- **横向收口 u4 单元**：把本讲与 u4-l1（数据结构）、u4-l2（MST 单链接）并排读，画出「输入 pdist 距离矩阵 → 三选一算法 → 统一 Z 输出」的完整对照图，确认三种算法对同一输入产出完全一致的 Z（这正是 `test_compare_with_trivial` 守护的不变量）。
- **进入 u5**：Z 一旦正确产出，下一站是 u5-l1 `fcluster`，看如何用 `distance`/`maxclust`/`inconsistent`/`monocrit` 等准则把树切平成 flat 簇，其中 `cluster_monocrit` 等就在本文件 [hierarchy/_hierarchy.pyx:237](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L237) 紧挨着我们读过的算法。
- **想深读算法理论**：直接读 docstring 引用的 Müllner 论文 arXiv:1109.2378，本讲的 `nn_chain`/`fast_linkage`/`mst_single_linkage` 正是它的实现；论文里对可还原性、RNN、Generic Algorithm 有完整证明。
- **工程化视角**：可结合 u7-l3 回看 `_hierarchy_impl.py` 顶部的 `lazy_cython` / `xpx.lazy_apply`，理解这三条 Cython 后端是如何被「仅 CPU、对 dask 发合并告警」的能力声明包裹起来的。
