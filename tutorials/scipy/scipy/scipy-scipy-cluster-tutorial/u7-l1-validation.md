# 讲义 u7-l1：linkage / 不一致矩阵的校验：`is_valid_*` 与辅助判断

## 1. 本讲目标

本讲承接 u3-l1（凝聚式聚类与 linkage matrix 数据结构）。在那里我们已经建立了 linkage matrix `Z` 的形状与编号约定：(n−1)×4、前两列为被合并两簇编号、第三列为合并距离、第四列为新簇的原始观测数、新簇按 n+i 编号。本讲要回答一个工程化的问题：**当我们拿到一个 `Z`（或一个不一致矩阵 `R`）时，凭什么相信它是合法的？**

`scipy.cluster.hierarchy` 的所有建树、切树、可视化函数都假设输入的 `Z` 是「合法 linkage matrix」。一个手工拼凑或被错误改写的 `Z` 轻则让 `fcluster` 切出无意义的簇，重则让 Cython 后端访问越界、段错误（见 u5-l4 提到的 gh-22183）。因此模块提供了一组**校验函数**作为「守门员」。

学完本讲后，你应该能够：

1. 说清 `is_valid_linkage` / `_is_valid_linkage` 对 `Z` 做的 6 类内容检查（负索引、负距离、负计数、计数过大、簇未生成就引用、同一簇被合并两次），以及更前置的 dtype / 形状 / 维数 / 空矩阵 4 类结构检查。
2. 理解 `throw` / `warning` 两个参数的三态语义，以及 `materialize` 标志为何能避免对 Dask / JAX 惰性数组的「意外求值」。
3. 看懂 `is_valid_im` / `_is_valid_im` 对不一致矩阵 `R`（均值/标准差/计数/不一致系数四列）的校验规则。
4. 掌握 `is_monotonic`、`num_obs_linkage`、`correspond` 三个辅助函数如何**复用** `_is_valid_linkage`，以及它们各自额外做的一件事。

## 2. 前置知识

阅读本讲前，请确保你已经理解：

- **linkage matrix Z**（见 u3-l1）：形状 (n−1)×4。原始观测占编号 0…n−1，第 i 步（i 从 0 起）合并出的新簇编号为 n+i，整树共 2n−1 个节点；第 0 步只能引用原始观测，第 i 步最多只能引用到「上一步刚生成的新簇」。
- **不一致矩阵 R**（见 u5-l3）：形状 (n−1)×4，四列依次为：链接高度均值、链接高度样本标准差、链接计数、不一致系数（z 分数）。它是 `inconsistent(Z)` 的产物，也是 `fcluster(criterion='inconsistent')` 的输入。
- **压缩距离矩阵**（见 u3-l1）：`pdist` 输出的一维向量，长度 n(n−1)/2，由 `scipy.spatial.distance` 的 `num_obs_y` / `is_valid_y` 管理。
- **array API 抽象（xp）**（见 u2-l1、u7-l3）：`array_namespace(x)` 返回数组后端命名空间（默认 NumPy），`_asarray` 是 SciPy 版 `np.asarray`，`is_lazy_array` 判断是否为 Dask / JAX 惰性数组。

一个直觉性的比喻：linkage matrix 像一张「合并账本」，每一行记一笔合并交易。校验函数就是审计员——它先查账本格式（纸张尺寸对不对、是不是用规定墨水），再查每一笔交易的合理性（编号有没有写负数、有没有把「还没成立的子公司」提前拿来合并、有没有把同一家公司合并两次）。审计员有三种执法力度：**只看不动**（默认，返回 True/False）、**口头警告**（`warning=True`）、**直接报警**（`throw=True` 抛异常）。

## 3. 本讲源码地图

本讲全部源码集中在**一个文件**：

| 文件 | 作用 |
|------|------|
| [hierarchy/_hierarchy_impl.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py) | 纯 Python 封装层。本讲涉及的 8 个函数（含下划线私有版）全部在此：`is_valid_linkage`/`_is_valid_linkage`、`is_valid_im`/`_is_valid_im`、`is_monotonic`、`num_obs_linkage`、`correspond`，以及共享引擎 `_lazy_valid_checks`。 |

此外 `correspond` 会借力 `scipy.spatial.distance` 的 `is_valid_y` / `num_obs_y`（校验压缩距离矩阵）。本讲不涉及任何 Cython 后端——校验逻辑全部是纯 Python / array API 实现，刻意保持可读、可移植。

关键依赖关系（自顶向下）：

```
is_valid_linkage(Z) ──┐
is_monotonic(Z) ──────┼──► _is_valid_linkage(Z, ...) ──┐
num_obs_linkage(Z) ───┘                                 │
                                                         ├─► _lazy_valid_checks(...)  (统一内容检查引擎)
is_valid_im(R) ──► _is_valid_im(R, ...) ────────────────┘
correspond(Z, Y) ──► _is_valid_linkage(Z, throw=True) + distance.is_valid_y(Y, throw=True)
```

## 4. 核心概念与源码讲解

### 4.1 `is_valid_linkage` 与 `_is_valid_linkage`：linkage matrix 的守门员

#### 4.1.1 概念说明

`is_valid_linkage(Z)` 是面向用户的公开入口，回答「这个 `Z` 是不是合法 linkage matrix」。它的判据写在 docstring 里，对第 i 行（i 从 0 起）要求：

\[
0 \leq \mathtt{Z[i,0]} \leq i+n-1,\qquad 0 \leq \mathtt{Z[i,1]} \leq i+n-1
\]

其中 n 是原始观测数。这条不等式的本质是**时序约束**：第 i 步合并时，被合并的两个簇编号都不能超过「上一步刚生成的新簇」n+i−1——也就是说**不能引用一个还没被创建的簇**（不能「穿越」）。此外，第四列计数代表新簇含多少原始观测，不能超过总观测数 n。

存在一个公开函数 `is_valid_linkage` 与一个带下划线的内部函数 `_is_valid_linkage`，两者是「宽容前门 / 严格引擎」的拆分（与 u2-l2 的 `vq`/`_vq` 同构思路）：

- **公开入口 `is_valid_linkage`**：负责 `_asarray` 规整、解析 `xp`，然后委托给私有版，且强制 `materialize=True`（即对惰性数组也允许求值来给出确定结论）。
- **私有引擎 `_is_valid_linkage`**：真正干活的函数，多一个 `materialize` 参数，默认 `False`——供模块内部其他函数调用时，**不会为了发警告而强制求值惰性数组**。

#### 4.1.2 核心流程

`_is_valid_linkage` 的检查分两大阶段：

```
阶段 A（结构检查，逐个 raise，try/except 统一收口）：
  1. dtype != float64  ──► TypeError("must contain doubles")
  2. ndim != 2         ──► ValueError("must have shape=2")
  3. shape[1] != 4     ──► ValueError("must have 4 columns")
  4. shape[0] == 0     ──► ValueError("at least two observations")
  （任一失败：throw 则 raise；warning 则发 ClusterWarning；否则返回 False）

阶段 B（内容检查，交给共享引擎 _lazy_valid_checks，见 4.2）：
  n = Z.shape[0]            # 注意：这里是「行数」= n_obs-1
  if n < 2: return True     # 仅 1 行时无法做内容检查，直接放行
  6 条布尔断言（任一为 True 即非法）：
    (1) 前两列出现负索引
    (2) 第三列出现负距离
    (3) 第四列出现负计数
    (4) 第四列计数 > n+1     # n=行数，n+1=n_obs
    (5) 每行前两列的较大者 >= n+1+i  # 引用了「本行才生成或更晚生成」的簇
    (6) 前两列不同值的个数 < n*2     # 有簇被合并了不止一次
```

> **行号坑提醒**：源码里局部变量 `n = Z.shape[0]` 是「合并行数」（= n_obs−1），**不是**原始观测数。判据 (4) 写成 `Z[:,3] > n+1`，这里的 `n+1` 才等于真正的观测数 n_obs。读源码时务必留意这个 `n` 的含义，否则会和 docstring 里用 n_obs 写的不等式对不上。

#### 4.1.3 源码精读

**公开入口**——只做规整 + 委托，并把 `materialize` 钉死为 `True`：

[is_valid_linkage 的实现体（hierarchy/_hierarchy_impl.py:2215-2218）](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2215-L2218)

```python
xp = array_namespace(Z)
Z = _asarray(Z, xp=xp)
return _is_valid_linkage(Z, warning=warning, throw=throw,
                         name=name, materialize=True, xp=xp)
```

**私有引擎的阶段 A**——4 条结构检查包在 try 里，except 统一按 throw/warning 分流。注意第 1 条是 `TypeError`（dtype 错），其余是 `ValueError`，所以 except 同时捕获 `(TypeError, ValueError)`：

[_is_valid_linkage 的结构检查（hierarchy/_hierarchy_impl.py:2227-2244）](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2227-L2244)

```python
name_str = f"{name!r} " if name else ''
try:
    if Z.dtype != xp.float64:
        raise TypeError(f'Linkage matrix {name_str}must contain doubles.')
    if len(Z.shape) != 2:
        raise ValueError(f'Linkage matrix {name_str}must have shape=2 ...')
    if Z.shape[1] != 4:
        raise ValueError(f'Linkage matrix {name_str}must have 4 columns.')
    if Z.shape[0] == 0:
        raise ValueError('Linkage must be computed on at least two observations.')
except (TypeError, ValueError) as e:
    if throw:
        raise
    if warning:
        _warning(str(e))
    return False
```

**私有引擎的阶段 B**——6 条内容断言，全部喂给 `_lazy_valid_checks`（每条是「(布尔数组, 报错文案)」二元组）。重点看第 (5)、(6) 两条最「算法味」的检查：

[_is_valid_linkage 的内容检查（hierarchy/_hierarchy_impl.py:2246-2264）](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2246-L2264)

```python
n = Z.shape[0]
if n < 2:
    return True

return _lazy_valid_checks(
    (xp.any(Z[:, :2] < 0),
     f'Linkage {name_str}contains negative indices.'),
    (xp.any(Z[:, 2] < 0),
     f'Linkage {name_str}contains negative distances.'),
    (xp.any(Z[:, 3] < 0),
     f'Linkage {name_str}contains negative counts.'),
    (xp.any(Z[:, 3] > n + 1),
     f'Linkage {name_str}contains excessive observations in a cluster'),
    (xp.any(xp.max(Z[:, :2], axis=1) >= xp.arange(n + 1, 2 * n + 1, dtype=Z.dtype)),
     f'Linkage {name_str}uses non-singleton cluster before it is formed.'),
    (xpx.nunique(Z[:, :2]) < n * 2,
     f'Linkage {name_str}uses the same cluster more than once.'),
    throw=throw, warning=warning, materialize=materialize, xp=xp
)
```

逐条解读两条「难点」：

- **判据 (5)「簇未生成就引用」**：`xp.arange(n+1, 2*n+1)` 生成长度为 n（行数）的序列 `[n+1, n+2, …, 2n]`，与每一行配对。第 i 行对应的阈值是 `n+1+i`（用行数 n 表达；换算成 n_obs 后即为 docstring 的 `i+n_obs`）。`xp.max(Z[:, :2], axis=1)` 取每行前两列的较大者，**一旦它 ≥ n+1+i** 就说明这一行引用了一个「编号 ≥ 本步新簇」的节点——而那个节点此刻还不存在，故非法。这正是 docstring 示例 `Z[3][1] = 20` 触发的那条报错。
- **判据 (6)「同一簇被合并两次」**：`xpx.nunique(Z[:, :2])` 数前两列（共 2n 个槽位）里**不同值**的个数。一棵合法树中，除根节点外的每个节点都恰好作为「孩子」出现一次，因此 2n 个槽位应当两两不同、即恰有 2n 个不同值。一旦 `< 2n`，说明有簇编号重复出现 → 某簇被合并了不止一次 → 非法。`xpx.nunique` 是 array_api_extra 提供的跨后端「去重计数」函数（NumPy 下等价于 `np.unique(...).size`）。

#### 4.1.4 代码实践

**实践目标**：亲手造出能分别触发 6 条内容检查的「坏 `Z`」，对照源码确认每条报错文案来自哪一行。

**操作步骤**（在装好 SciPy 的环境里运行）：

```python
import numpy as np
import warnings
from scipy.cluster.hierarchy import is_valid_linkage, linkage
from scipy.cluster import hierarchy
from scipy.spatial.distance import pdist

# 1) 先造一个「合法」的 Z（4 个观测），作为改坏的起点
Z0 = linkage(pdist(np.array([[0,0],[0,1],[1,0],[1,1]], float)))
print("合法基线:", is_valid_linkage(Z0))            # True

# 2) 造 5 个「坏 Z」，逐个用 warning=True 观察告警文案
def show(tag, Z):
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        ok = is_valid_linkage(Z, warning=True, throw=False)
        msgs = [str(x.message) for x in w if issubclass(x.category, hierarchy.ClusterWarning)]
    print(f"[{tag}] valid={ok}  消息={msgs}")

Z = Z0.copy(); Z[0,0] = -1                       # (1) 负索引
show("负索引", Z)
Z = Z0.copy(); Z[0,2] = -0.5                     # (2) 负距离
show("负距离", Z)
Z = Z0.copy(); Z[0,3] = -1                       # (3) 负计数
show("负计数", Z)
Z = Z0.copy(); Z[0,3] = 999                      # (4) 计数过大
show("计数过大", Z)
Z = Z0.copy(); Z[0,1] = 20                       # (5) 引用未生成簇（docstring 同款）
show("未生成簇", Z)

# 3) 验证 throw=True 时改抛异常，且异常类型正确
Z = Z0.copy(); Z[0,2] = -1
try:
    is_valid_linkage(Z, throw=True)
except ValueError as e:
    print("throw 抛出 ValueError:", e)
```

**需要观察的现象**：每条 `show` 都应打印 `valid=False`，且 `消息` 列表里恰好出现对应文案，如 `contains negative distances.`、`uses non-singleton cluster before it is formed.` 等。

**预期结果**：5 个坏 `Z` 全部返回 `False` 并触发对应 `ClusterWarning`；`throw=True` 时抛 `ValueError`。判据 (6) 较难手工构造（需让某簇编号在前两列出现两次），可在练习里尝试。

> 若你的环境未编译 SciPy 或命令报错，则标注「待本地验证」——不要假装已经跑过。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_is_valid_linkage` 在 `n < 2`（即只有 1 行）时直接 `return True`，跳过所有内容检查？

**参考答案**：只有 1 行意味着 n_obs=2，`Z` 形如 `[[0, 1, d, 2]]`。此时前两列只能是原始观测 0、1，既不可能为负、也不可能引用尚未生成的簇（尚无新簇），计数 2 也不可能超过 n_obs=2。所有 6 条内容检查在数学上恒为「通过」，故直接放行以省去无意义计算。

**练习 2**：把一个合法 `Z` 复制后，将某一行的 `Z[i,0]` 与另一行的 `Z[j,0]` 改成同一个簇编号，使其触发判据 (6)。提示：要让 `xpx.nunique(Z[:,:2])` 下降到 `< 2n`。

**参考答案**：例如对 4 观测的 `Z0`，`Z = Z0.copy(); Z[1,0] = Z0[0,0]` 会让簇编号 0 在前两列出现两次，`nunique` 由 4 降为 3 `< 4=2n`，`is_valid_linkage(Z, warning=True)` 应报 `uses the same cluster more than once.`。（注意：这种手工改法可能同时触犯判据 (5)，取决于具体取值——这正是多条检查协同工作之处。）

---

### 4.2 `_lazy_valid_checks`：throw / warning / materialize 的统一引擎

#### 4.2.1 概念说明

4.1 里把 6 条内容断言一股脑丢给了 `_lazy_valid_checks`。这个函数是**所有内容检查的共享引擎**：它接收一串 `(条件, 文案)` 二元组，统一负责「按 throw / warning 决定是抛异常、发警告、还是只返回布尔」。

它的难点不在校验逻辑，而在**惰性数组**。当 `Z` 是 Dask / JAX 数组时，`xp.any(Z[:,2] < 0)` 返回的是一个**未求值的 0 维布尔数组**，而不是 Python `bool`。一旦你对其调用 `bool()` 或 `if`，就会**强制触发整个任务图的计算**（Dask 合并所有分块、JAX 同步执行）。这在用户只想「静默检查」时是不可接受的副作用。因此引擎引入了第 3 个开关 `materialize`：

- `materialize=True`：允许求值（公开入口 `is_valid_linkage` 用，因为用户显式问「合不合法」，理应得到确定答案）。
- `materialize=False`：惰性输入下**忽略 throw / warning**，只返回一个 0 维布尔数组（内部调用用，避免在 `fcluster`、`cophenet` 等函数里意外触发大规模求值）。

`throw` 与 `warning` 的优先级：`throw` 高于 `warning`。两者都为 `False`（默认）时，纯返回布尔、完全静默。

#### 4.2.2 核心流程

```
输入：args = [(cond_0, msg_0), (cond_1, msg_1), ...]，每个 cond 是 0 维布尔数组
1. conds = xp.concat([reshape(cond) for cond, _ in args])   # 拼成一维布尔向量
2. lazy = is_lazy_array(conds)                              # 判惰性
3. if (未要求 throw 且未要求 warning) 或 (lazy 且不允许 materialize):
       return ~xp.any(conds)   # 惰性时返回 0 维数组，eager 时返回 Python bool
4. if is_dask(xp):  conds = conds.compute()                # Dask：只求一次值
5. conds = [bool(cond) for cond in conds]                  # 转成 Python bool（兼容 CuPy/Sparse 的传输守卫）
6. for cond, (_, msg) in zip(conds, args):
       if throw and cond:  raise ValueError(msg)           # throw 优先
       elif warning and cond: warnings.warn(msg, ClusterWarning, stacklevel=3)
7. return not any(conds)
```

几个关键设计：

- **步骤 3 的「短路」**：只要不需要副作用（不 throw 也不 warning），就绝不求值——这正是惰性友好的核心。
- **步骤 4「只算一次」**：Dask 下用 `conds.compute()` 把整条布尔向量一次性物化，而不是对每个 cond 各算一次，省去重复构建任务图。
- **步骤 5 用 `bool()` 而非 `np.asarray`**：注释解释了 CuPy / PyTorch 有「设备搬运守卫」、Sparse 有「稠密化守卫」，`np.asarray` 会被它们拦截，而 `bool()` 不会。

#### 4.2.3 源码精读

[_lazy_valid_checks 的完整实现（hierarchy/_hierarchy_impl.py:2267-2317）](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2267-L2317)

```python
def _lazy_valid_checks(*args, throw=False, warning=False, materialize=False, xp):
    conds = xp.concat([xp.reshape(cond, (1, )) for cond, _ in args])

    lazy = is_lazy_array(conds)
    if not throw and not warning or (lazy and not materialize):
        out = ~xp.any(conds)
        return out if lazy else bool(out)

    if is_dask(xp):
        # Only materialize the graph once, instead of once per check
        conds = conds.compute()

    # Don't call np.asarray(conds), as it would be blocked by the device transfer
    # guard on CuPy and PyTorch and the densification guard on Sparse, whereas
    # bool() will not.
    conds = [bool(cond) for cond in conds]

    for cond, (_, msg) in zip(conds, args):
        if throw and cond:
            raise ValueError(msg)
        elif warning and cond:
            warnings.warn(msg, ClusterWarning, stacklevel=3)

    return not any(conds)
```

注意发警告用的是 `warnings.warn(msg, ClusterWarning, stacklevel=3)`，而非 4.1 里结构检查用的 `_warning(...)`。两者都产生 `ClusterWarning`，但 `_warning`（见下）会额外加 `scipy.cluster:` 前缀。`ClusterWarning` 本身只是一个空子类：

[ClusterWarning 与 _warning（hierarchy/_hierarchy_impl.py:66-74）](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L66-L74)

```python
class ClusterWarning(UserWarning):
    """A ``UserWarning`` raised during clustering."""
    pass


def _warning(s):
    warnings.warn(f'scipy.cluster: {s}', ClusterWarning, stacklevel=3)
```

#### 4.2.4 代码实践

**实践目标**：通过阅读源码（而非运行）理解「`materialize` 如何避免意外求值」，并设计一个能体现「惰性短路」的推理题。

**操作步骤**：

1. 在 `_lazy_valid_checks` 中定位步骤 3 的短路条件 `not throw and not warning or (lazy and not materialize)`。
2. 回答：当输入是 Dask 数组、调用方是 `fcluster` 内部（它用 `_is_valid_linkage(..., materialize=False)`），且既不 throw 也不 warning 时，函数返回什么？会不会触发 Dask 计算？
3. 对比：用户直接调 `is_valid_linkage(dask_Z)`（它把 `materialize=True` 传下去）且 `throw=True` 时，又会发生什么？

**需要观察的现象（源码阅读型）**：第 2 问应得到「返回一个 0 维 Dask 布尔数组、不触发计算」；第 3 问应得到「在步骤 4 执行 `conds.compute()` 物化后再判断」。

**预期结果**：能用一句话说清 `materialize` 的作用——「**控制校验是否允许为了给出确定结论而强制求值惰性数组**，公开入口允许、内部调用默认不允许」。

#### 4.2.5 小练习与答案

**练习**：`_lazy_valid_checks` 里 `throw` 和 `warning` 同时为 `True` 时，对一个非法条件会怎样？先后顺序由谁决定？

**参考答案**：由步骤 6 的 `if throw and cond: raise ... elif warning and cond: warn ...` 决定——`throw` 优先。只要某条件为真且 `throw=True`，立即 `raise ValueError(msg)`，根本走不到 `warning` 分支；循环也会因异常而中断，后续条件不再检查。因此「同时 throw + warning」等价于「只 throw」。

---

### 4.3 `is_valid_im` 与 `_is_valid_im`：不一致矩阵 R 的校验

#### 4.3.1 概念说明

`is_valid_im(R)` 校验的是**不一致矩阵** `R`（见 u5-l3，由 `inconsistent(Z)` 产生）。`R` 形状也是 (n−1)×4，但四列语义与 `Z` 完全不同：第 0 列链接高度均值、第 1 列链接高度标准差、第 2 列链接计数、第 3 列不一致系数。

校验规则按 `R` 的物理意义制定（见 docstring）：

- **结构**：必须是 2 维、float64、恰 4 列、至少 1 行。
- **内容**：标准差 `R[:,1]` 必须 ≥ 0（标准差非负）；计数 `R[:,2]` 必须 ≥ 0（且 docstring 说明上限 ≤ n−1，但源码内容检查只查非负，上限由 `inconsistent` 生成时保证）。

与 `is_valid_linkage` 完全对称，`is_valid_im` 也是「公开入口（强制 `materialize=True`）+ 私有引擎 `_is_valid_im`（默认 `materialize=False`）」的双层结构。

#### 4.3.2 核心流程

```
_is_valid_im(R, ...)：
  阶段 A（结构检查）：
    dtype != float64 ──► TypeError
    ndim != 2        ──► ValueError
    shape[1] != 4    ──► ValueError
    shape[0] < 1     ──► ValueError("at least one row")   # 注意：这里允许 1 行，与 Z 不同
    （失败按 throw/warning 分流）
  阶段 B（内容检查，3 条，交 _lazy_valid_checks）：
    R[:,0] < 0  → 负的链接高度均值
    R[:,1] < 0  → 负的链接高度标准差
    R[:,2] < 0  → 负的链接计数
```

> 与 `_is_valid_linkage` 的差异：`R` 至少要 1 行（`shape[0] < 1` 报错），而 `Z` 至少要 1 行但内容检查要求 `n >= 2`；且 `R` 的内容检查只有 3 条（不查第 3 列不一致系数，因为 z 分数可正可负）。

#### 4.3.3 源码精读

**私有引擎**——结构与 `Z` 版几乎逐行对应，只换了文案与判据：

[_is_valid_im 的实现（hierarchy/_hierarchy_impl.py:2085-2120）](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2085-L2120)

```python
def _is_valid_im(R, warning=False, throw=False, name=None, materialize=False, *, xp):
    name_str = f"{name!r} " if name else ''
    try:
        if R.dtype != xp.float64:
            raise TypeError(f'Inconsistency matrix {name_str}must contain doubles (double).')
        if len(R.shape) != 2:
            raise ValueError(f'Inconsistency matrix {name_str}must have shape=2 ...')
        if R.shape[1] != 4:
            raise ValueError(f'Inconsistency matrix {name_str}must have 4 columns.')
        if R.shape[0] < 1:
            raise ValueError(f'Inconsistency matrix {name_str}must have at least one row.')
    except (TypeError, ValueError) as e:
        if throw:
            raise
        if warning:
            _warning(str(e))
        return False

    return _lazy_valid_checks(
        (xp.any(R[:, 0] < 0),
         f'Inconsistency matrix {name_str} contains negative link height means.'),
        (xp.any(R[:, 1] < 0),
         f'Inconsistency matrix {name_str} contains negative link height standard deviations.'),
        (xp.any(R[:, 2] < 0),
         f'Inconsistency matrix {name_str} contains negative link counts.'),
        throw=throw, warning=warning, materialize=materialize, xp=xp
    )
```

**公开入口**——和 `is_valid_linkage` 一样只做规整 + 强制 `materialize=True`，被 `@xp_capabilities(warnings=[("dask.array","see notes"),("jax.numpy","see notes")])` 装饰（声明对惰性后端的注意义务）：

[is_valid_im 的实现体（hierarchy/_hierarchy_impl.py:2079-2082）](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2079-L2082)

```python
xp = array_namespace(R)
R = _asarray(R, xp=xp)
return _is_valid_im(R, warning=warning, throw=throw, name=name,
                    materialize=True, xp=xp)
```

#### 4.3.4 代码实践

**实践目标**：复现 docstring 的示例——把一个合法 `R` 的某个标准差取反，观察 `is_valid_im` 由 `True` 变 `False`。

**操作步骤**：

```python
import numpy as np
from scipy.cluster.hierarchy import ward, inconsistent, is_valid_im
from scipy.spatial.distance import pdist

X = [[0,0],[0,1],[1,0],[0,4],[0,3],[1,4],[4,0],[3,0],[4,1],[4,4],[3,4],[4,3]]
Z = ward(pdist(X))
R = inconsistent(Z)
print("原 R 合法:", is_valid_im(R))      # True

R[-1, 1] = R[-1, 1] * -1                 # 把最后一行的标准差取反（docstring 同款）
print("改坏后:", is_valid_im(R))          # False
print("throw 抛错:")
try:
    is_valid_im(R, throw=True)
except ValueError as e:
    print("  ", e)
```

**需要观察的现象**：第二次 `is_valid_im` 返回 `False`，`throw=True` 抛出文案为 `contains negative link height standard deviations.` 的 `ValueError`。

**预期结果**：如上。若取反的是第 0 列（均值）或第 2 列（计数），文案会相应变成 `negative link height means.` 或 `negative link counts.`——可对照源码 3 条断言逐一验证。

#### 4.3.5 小练习与答案

**练习**：为什么 `is_valid_im` 的内容检查**不**校验第 3 列（不一致系数）的正负，也不校验 `R[:,2]`（计数）的上界 ≤ n−1？

**参考答案**：第 3 列不一致系数本质是 z 分数 \((h-\bar h)/\sigma\)，可正可负，没有「必须非负」的物理约束，故不查。计数 `R[:,2]` 的上界确实应为 ≤ n−1（一个簇最多包含全部 n−1 次链接），但这个上界由 `inconsistent` 在生成时天然保证（它只在有限深度内统计），手工篡改的场景极少；源码选择只查「非负」这一最关键的不变量，把上界校验留给信任生成器，体现了「校验查致命不变量、不查冗余」的取舍。

---

### 4.4 三个辅助函数：`is_monotonic`、`num_obs_linkage`、`correspond`

#### 4.4.1 概念说明

这三个函数都不是「从零造轮子」，而是**复用 `_is_valid_linkage`** 再加一点点自己的逻辑：

- **`is_monotonic(Z)`**：判断 `Z` 的距离列是否**单调不减**。单调性指「后合并的簇，距离不小于先合并的」（见 u3-l4：ward/single 等可还原方法天然单调，centroid/median 可能距离倒挂而不单调）。它先用 `throw=True` 保证 `Z` 合法，再查 `Z[:,2]`。
- **`num_obs_linkage(Z)`**：从 `Z` 反推原始观测数 n。因为 `Z` 有 n−1 行，所以 `n = Z.shape[0] + 1`。先校验合法性再返回行数+1。
- **`correspond(Z, Y)`**：判断 linkage matrix `Z` 与压缩距离矩阵 `Y` 是否「可能对应同一组观测」——即两者推出的观测数是否相等。它是 `fclusterdata`、`cophenet` 等流程里的 sanity check。

#### 4.4.2 核心流程

```
is_monotonic(Z):
    _is_valid_linkage(Z, throw=True)          # 先确保 Z 合法（否则后续比较无意义）
    return xp.all(Z[1:, 2] >= Z[:-1, 2])      # 每个距离 >= 前一个距离

num_obs_linkage(Z):
    _is_valid_linkage(Z, throw=True)
    return Z.shape[0] + 1                      # n_obs = 行数 + 1

correspond(Z, Y):
    _is_valid_linkage(Z, throw=True)           # Z 合法
    distance.is_valid_y(Y, throw=True)         # Y 合法（压缩距离矩阵）
    return distance.num_obs_y(Y) == num_obs_linkage(Z)   # 观测数是否相等
```

#### 4.4.3 源码精读

**`is_monotonic`**——核心就一行向量比较，含义是「第 2 列从第 1 行起，每个值都不小于前一行」：

[is_monotonic 的实现体（hierarchy/_hierarchy_impl.py:1977-1982）](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1977-L1982)

```python
xp = array_namespace(Z)
Z = _asarray(Z, xp=xp)
_is_valid_linkage(Z, throw=True, name='Z', xp=xp)

# We expect the i'th value to be greater than its successor.
return xp.all(Z[1:, 2] >= Z[:-1, 2])
```

**`num_obs_linkage`**——注意它用 `throw=True`，意味着传入非法 `Z` 会直接抛异常而非返回错误值：

[num_obs_linkage 的实现体（hierarchy/_hierarchy_impl.py:2354-2357）](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2354-L2357)

```python
xp = array_namespace(Z)
Z = _asarray(Z, xp=xp)
_is_valid_linkage(Z, throw=True, name='Z', xp=xp)
return Z.shape[0] + 1
```

**`correspond`**——双端校验（`Z` 与 `Y` 都要合法），再比观测数。`distance.num_obs_y` 用解二次方程从压缩距离向量长度 \(k=n(n-1)/2\) 反推 n：

[correspond 的实现体（hierarchy/_hierarchy_impl.py:2410-2415）](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2410-L2415)

```python
xp = array_namespace(Z, Y)
Z = _asarray(Z, xp=xp)
Y = _asarray(Y, xp=xp)
_is_valid_linkage(Z, throw=True, xp=xp)
distance.is_valid_y(Y, throw=True)
return distance.num_obs_y(Y) == num_obs_linkage(Z)
```

其中 `distance.num_obs_y` 通过 \(d=\lceil\sqrt{2k}\rceil\) 反推、并验证 \(d(d-1)/2=k\)（见 [scipy/spatial/distance.py:2558-2567](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/spatial/distance.py#L2558-L2567)）。

#### 4.4.4 代码实践

**实践目标**：用同一组数据对比 `ward`（单调）与 `median`（非单调），并验证 `correspond` 的判等逻辑。

**操作步骤**：

```python
import numpy as np
from scipy.cluster.hierarchy import (ward, median, is_monotonic,
                                     num_obs_linkage, correspond)
from scipy.spatial.distance import pdist

X = np.array([[0,0],[0,1],[1,0],[0,4],[0,3],[1,4],
              [4,0],[3,0],[4,1],[4,4],[3,4],[4,3]], float)
Y = pdist(X)

Zw, Zm = ward(Y), median(Y)
print("ward   单调?", is_monotonic(Zw), " 观测数:", num_obs_linkage(Zw))
print("median 单调?", is_monotonic(Zm), " 观测数:", num_obs_linkage(Zm))
print("Z-Y 对应?", correspond(Zw, Y))     # True（都来自 12 个观测）

# 构造一个「观测数对不上」的 Y：只取前 6 个点
Y6 = pdist(X[:6])
print("Z(12点) vs Y(6点) 对应?", correspond(Zw, Y6))   # False
```

**需要观察的现象**：`ward` 单调为 `True`、`median` 为 `False`（与 docstring 示例一致）；`correspond(Zw, Y)` 为 `True`，而 `correspond(Zw, Y6)` 为 `False`。

**预期结果**：如上。可顺手 `print(Zm[:,2])` 观察 `median` 的距离列确实出现「先大后小」的倒挂，从而理解 `is_monotonic` 为何返回 `False`。

#### 4.4.5 小练习与答案

**练习 1**：`is_monotonic` 为什么先调 `_is_valid_linkage(Z, throw=True)`？如果去掉这行，对一个形状错误的 `Z` 会怎样？

**参考答案**：先校验能保证 `Z` 至少有 4 列、是 2 维，从而使后续 `Z[1:, 2]` / `Z[:-1, 2]` 的切片有意义。若去掉，传入如 3 列的数组会让 `Z[:, 2]` 取到第 3 列（本应是「距离」列）甚至越界，得到无意义结果或 `IndexError`，而非清晰的「`Z` 非法」报错。`throw=True` 确保非法输入立即以明确异常终止，而不是返回误导性的布尔值。

**练习 2**：`correspond(Z, Y)` 返回 `True` 是否意味着 `Z` 一定是由 `Y` 经 `linkage` 算出来的？

**参考答案**：**不是**。`correspond` 只检查「两者推出的原始观测数相等」这一个必要条件，完全不比较 `Z` 内的距离值与 `Y` 的一致性。一个用完全不同的距离矩阵 `Y'`（但观测数相同）算出的 `Z'`，也会让 `correspond(Z', Y)` 返回 `True`。docstring 用词「could possibly correspond」正是此意——它只是「可能对应」的 sanity check，不是内容一致性的证明。

---

## 5. 综合实践

把本讲四个模块串起来，写一个**「防御性聚类包装器」**小工具。要求：

1. 输入一个原始观测矩阵 `X` 和一个 method 字符串，内部执行 `pdist → linkage → inconsistent`。
2. 在每一步用本讲的校验函数做防御性检查：
   - 用 `is_valid_linkage(Z, throw=True)` 守住 `linkage` 产物；
   - 用 `is_valid_im(R, throw=True)` 守住 `inconsistent` 产物；
   - 用 `correspond(Z, Y)` 确认 `Z` 与 `Y` 观测数一致；
   - 用 `num_obs_linkage(Z)` 断言它等于 `len(X)`；
   - 最后用 `is_monotonic(Z)` 打印一条提示（不抛错），告诉用户该 method 是否单调。
3. 故意传一个「错误的 method」（例如让 `linkage` 抛错）或一个「观测数对不上的 `Y`」，观察你的包装器在哪一步、用哪个校验函数把问题拦下。

参考骨架（示例代码，非项目原有代码）：

```python
# 示例代码：防御性聚类包装器
from scipy.spatial.distance import pdist
from scipy.cluster.hierarchy import (linkage, inconsistent,
    is_valid_linkage, is_valid_im, correspond, num_obs_linkage, is_monotonic)

def safe_cluster(X, method='ward'):
    Y = pdist(X)
    Z = linkage(Y, method=method)
    is_valid_linkage(Z, throw=True, name='Z')          # 守 linkage
    assert correspond(Z, Y), "Z 与 Y 观测数不一致"
    assert num_obs_linkage(Z) == len(X), "观测数对不上"
    R = inconsistent(Z)
    is_valid_im(R, throw=True, name='R')               # 守 inconsistent
    print(f"method={method} 单调? {is_monotonic(Z)}")
    return Z, R
```

通过这个练习，你会切身体会到：校验函数不是「写完就扔」的边角料，而是把「非法输入」挡在 Cython 后端**之前**、把「模糊错误」转成「精确异常」的关键工程设施——这正是 gh-22183 这类越界 bug 能被 `_is_valid_linkage` 提前拦截的原因。

## 6. 本讲小结

- `is_valid_linkage` / `_is_valid_linkage` 是 linkage matrix 的守门员：4 条结构检查（dtype/维数/列数/非空）+ 6 条内容检查（负索引、负距离、负计数、计数过大、簇未生成就引用、同一簇合并两次）。注意源码局部变量 `n = Z.shape[0]` 是行数而非观测数。
- `_lazy_valid_checks` 是所有内容检查的共享引擎，用 `throw`（抛 `ValueError`）> `warning`（发 `ClusterWarning`）> 静默返回布尔的三态语义，并用 `materialize` 开关控制「是否允许为给结论而强制求值 Dask / JAX 惰性数组」。
- `is_valid_im` / `_is_valid_im` 与 `Z` 版严格对称，校验不一致矩阵 `R` 的结构与「均值/标准差/计数非负」，但不校验第 3 列 z 分数（可正可负）。
- `is_monotonic`、`num_obs_linkage`、`correspond` 三个辅助函数全部复用 `_is_valid_linkage(..., throw=True)` 做前置合法性保证，再分别加「距离列单调不减」「行数+1」「Z 与 Y 观测数相等」的一点点自有逻辑。
- 公开入口（`is_valid_linkage` / `is_valid_im`）强制 `materialize=True`，私有引擎默认 `False`——这是「用户显式发问应得确定答案、内部调用不应意外触发大规模求值」的工程权衡。

## 7. 下一步学习建议

- **u7-l2（DisjointSet、leaders、isomorphism）**：继续工程化主题，看 `DisjointSet` 并查集与 `leaders` 如何在校验之后的合法 `Z` 上工作。
- **u7-l3（array API 兼容、惰性数组与测试体系）**：本讲反复出现的 `materialize` / `is_lazy_array` / `xp_capabilities` 在那里有系统性总结，并讲解 `test_hierarchy.py` 中 `TestIsValidLinkage` / `TestIsValidInconsistent` 如何用 `xpx.at(...).set(...)` 精确制造「坏 Z / 坏 R」来逐条覆盖校验规则。
- **回看 u5-l3（inconsistent）与 u5-l4（cophenet）**：理解 `_is_valid_linkage` 在 `cophenetic_distances` 入口处（gh-22183 修复点）拦截非法 `Z`、防止 Cython 后端 `members` 越界的真实场景。
