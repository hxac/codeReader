# fclusterdata：一站式聚类流水线

## 1. 本讲目标

学完本讲，你应当能够：

- 用一句话说清 `scipy.cluster.hierarchy.fclusterdata` 是什么：它是把 **「`pdist` → `linkage` → `fcluster`」三步**打包成**一个函数**的便捷入口（convenience function）。
- 看懂它与 `linkage` 在**输入约定上的根本区别**：`linkage` 既吃压缩距离矩阵又吃观测矩阵，而 `fclusterdata` **只吃原始观测矩阵 `X`（N×M）**，内部自己算距离。
- 逐行读懂 `fclusterdata` 函数体（只有十几行），知道它如何把 `criterion` / `metric` / `method` / `depth` / `R` / `t` 这些参数**透传**给 `pdist`、`linkage`、`inconsistent`、`fcluster` 四个内部函数。
- 知道什么时候该用 `fclusterdata`（快速得到标签、不在乎中间产物），什么时候必须手动走三步（需要自检 `Z`、可视化、换距离度量细节、用 `monocrit`）。
- 认出 `fclusterdata` 相对于「手动三步」**丢失的能力**：不暴露 `monocrit`（故不能用 `monocrit` / `maxclust_monocrit` 准则）、不暴露 `optimal_ordering`，并且**不自动校验 method 与 metric 的兼容性**。

本讲是对 u5-l1（`fcluster` 的五种 `criterion`）的承接：u5-l1 讲「**给定树怎么切**」，本讲讲「**怎么用一个函数把建树 + 切树都包了**」。本讲几乎不涉及新算法，重点是**封装思想**和**参数透传映射**。

## 2. 前置知识

本讲建立在你已经掌握以下内容之上：

1. **u1-l3**：层次聚类最小工作链 `pdist → linkage → fcluster`，以及三类约定——观测矩阵 `X`（M×N，行=观测、列=特征）、linkage matrix `Z`（\((n-1)\times 4\)，簇号 \(n+i\) 表示第 \(i\) 步合并出的新簇）、扁平簇标签 `T` 从 **1** 起。
2. **u3-l1 / u3-l2**：`linkage` 的两种输入——一维压缩距离矩阵（`pdist` 输出，长度 \(n(n-1)/2\)）与二维观测矩阵；以及 `method`（single/complete/average/weighted/centroid/median/ward）的含义。
3. **u5-l1**：`fcluster` 的五种 `criterion`（`inconsistent` / `distance` / `maxclust` / `monocrit` / `maxclust_monocrit`），以及「单调 monocrit + 阈值」这一统一引擎。

再补两个本讲会用到的术语：

- **便捷函数（convenience function）**：本身不实现新算法，只是把一串常用调用「串好」、给出一组合理的默认参数，让用户少写几行。`fclusterdata` 就是典型，它的注释里明确写明与 MATLAB 的 `clusterdata` 函数类似。
- **不一致矩阵 `R`**：`inconsistent(Z, d)` 的产物，形状 \((n-1)\times 4\)，描述每个非叶簇在 `depth` 范围内的「不一致性」。`fcluster` 的默认准则 `inconsistent` 需要它。`fclusterdata` 在 `R=None` 时会自动算，所以你通常不用手算。

一句话复习数据流转：

\[
\underbrace{X}_{N\times M\ \text{观测矩阵}}
\;\xrightarrow{\text{pdist}}\;
\underbrace{Y}_{n(n-1)/2\ \text{压缩距离}}
\;\xrightarrow{\text{linkage}}\;
\underbrace{Z}_{(n-1)\times 4\ \text{树}}
\;\xrightarrow{\text{fcluster}}\;
\underbrace{T}_{n\ \text{标签}}
\]

`fclusterdata` 就是把这条链上从 `X` 到 `T` 的四步用一个函数调用包起来。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [hierarchy/_hierarchy_impl.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py) | Python 封装层。`fclusterdata` 在此定义，是一个**纯 Python 编排函数**，本身不含 Cython 代码，只串联调用 `pdist`/`linkage`/`inconsistent`/`fcluster`。 |

本讲涉及的关键函数（全部位于该文件）：

- `fclusterdata`（本讲主角，一站式入口；函数体第 2689–2702 行）
- 它内部调用的四个函数：`distance.pdist`（来自 `scipy.spatial.distance`）、`linkage`、`inconsistent`、`fcluster`。
- 辅助：`array_namespace` / `_asarray`（来自 `scipy._lib._array_api`，做多后端规整）。

> 注意：`fclusterdata` **不**直接调用 `_hierarchy.*` 这类 Cython 后端函数。Cython 是被它间接经由 `linkage` / `inconsistent` / `fcluster` 触发的。这与上一讲 `fcluster` 自己去调 `cluster_*` 不同。

---

## 4. 核心概念与源码讲解

### 4.1 fclusterdata：层次聚类的「一站式」入口

#### 4.1.1 概念说明

层次聚类在实战中其实是有「固定套路」的：拿到一批原始数据 `X` → 算两两距离 → 建树 → 在某个阈值处切平得到簇标签。这三到四步如果你每次都手写，既啰嗦又容易把 `pdist` 的输出和 `linkage` 的输入接错（比如忘了 `pdist` 返回的是压缩向量而不是方阵）。

`fclusterdata` 就是来消除这种模板代码的。它**只接受原始观测矩阵 `X`**（不接受距离矩阵），然后把上面那串步骤全自动跑完，最后只返回扁平簇标签 `T`。源码注释把它类比为 MATLAB 的 `clusterdata`：

> This function is similar to the MATLAB function ``clusterdata``.

[见 hierarchy/_hierarchy_impl.py:L2660（Notes 段）](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2660)

它的默认行为是「**欧氏距离 + single linkage + inconsistent 准则**」，这一组默认值恰好是「最经典、最不需要额外说明」的层次聚类配置。所以一句 `fclusterdata(X, t=1)` 就能出结果。

但便利是有代价的：**因为不暴露中间产物，你拿不到 `Z`、拿不到 `R`，也就没法画 dendrogram、没法自检树的合法性、没法用需要 `monocrit` 的准则**。这正是「何时用 `fclusterdata`、何时手动走三步」的判断依据（见 4.1.2 与 4.2.4）。

#### 4.1.2 核心流程

「一站式」与「手动三步」做的是**完全相同**的计算，区别只在写法：

```text
手动三步（你需要自己拼）：
  Y = pdist(X, metric=...)            # 1. 算距离
  Z = linkage(Y, method=...)          # 2. 建树
  R = inconsistent(Z, d=...)          # 3a. （默认准则需要）算不一致矩阵
  T = fcluster(Z, t=..., criterion=..., depth=..., R=R)   # 3b. 切平

fclusterdata（一步到位）：
  T = fclusterdata(X, t, criterion=..., metric=..., depth=..., method=..., R=...)
  # 内部依次执行上面四步，只把 T 返回给你
```

判定流程（伪代码）：

```text
def fclusterdata(X, t, criterion, metric, depth, method, R):
    规整 X：转成连续的 float64 二维数组；若不是二维则抛 TypeError
    Y = pdist(X, metric)          # 永远从 X 算距离
    Z = linkage(Y, method)        # 建树
    if R is None:
        R = inconsistent(Z, d=depth)   # 默认会算，供 inconsistent 准则用
    else:
        R = 规整用户传入的 R
    T = fcluster(Z, criterion=criterion, depth=depth, R=R, t=t)
    return T
```

**判断要不要用 `fclusterdata`**：

- **适合**：你只想要标签、数据量不大、用默认或常见配置、懒得管 `Z` 和 `R`。
- **不适合**：要画 dendrogram（需要 `Z`）、要用 `monocrit` / `maxclust_monocrit` 准则（`fclusterdata` 没有这个参数）、要自检树的合法性、要复用同一棵树在不同阈值上切多次（重复建树浪费）、要细控 `optimal_ordering`。

#### 4.1.3 源码精读

先看签名与它头上的装饰器：

```python
@xp_capabilities(cpu_only=True, reason="Cython code",
                 jax_jit=False, allow_dask_compute=True)
def fclusterdata(X, t, criterion='inconsistent',
                 metric='euclidean', depth=2, method='single', R=None):
```

[见 hierarchy/_hierarchy_impl.py:L2602-L2605（装饰器 + 函数签名）](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2602-L2605)

两点要记准确：

1. `fclusterdata` 头上用的是**直接写的** `@xp_capabilities(cpu_only=True, reason="Cython code", jax_jit=False, allow_dask_compute=True)`，**不是** u3-l3 里讲过的那个模块级复用别名 `lazy_cython`（那个带 `warnings=[("dask.array","merges chunks")]`）。差异在于：`fclusterdata` 允许 dask 整体计算（`allow_dask_compute=True`）且不额外发「合并分块」告警，同时明确禁止 `jax_jit`。原因也好理解——`fclusterdata` 自己是纯 Python 编排，dask 的告警应由它内部真正碰 Cython 的 `linkage` 等函数各自负责。
2. 默认参数集合：`criterion='inconsistent'`、`metric='euclidean'`、`depth=2`、`method='single'`、`R=None`。这正是「经典 single linkage + inconsistent 准则」。

再看 docstring 里那个端到端示例（默认配置）：

```python
>>> X = [[0, 0], [0, 1], [1, 0],
...      [0, 4], [0, 3], [1, 4],
...      [4, 0], [3, 0], [4, 1],
...      [4, 4], [3, 4], [4, 3]]
>>> fclusterdata(X, t=1)
array([3, 3, 3, 4, 4, 4, 2, 2, 2, 1, 1, 1], dtype=int32)
```

[见 hierarchy/_hierarchy_impl.py:L2677-L2683（示例）](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2677-L2683)

12 个二维点天然分成「左下角 3 个、左上角 3 个、右下角 3 个、右上角 3 个」四组，`t=1`（inconsistent 阈值）恰好把这四组各自切成一个簇，所以结果是 4 个簇、每簇 3 点（标签号 1/2/3/4 是按合并顺序分配的，与点的几何位置不直接对应）。

#### 4.1.4 代码实践

**实践目标**：亲手跑通 docstring 示例，确认 `fclusterdata` 的「一行出标签」体验，并观察标签编号从 1 起。

**操作步骤**（示例代码，需本地运行）：

```python
# 示例代码
from scipy.cluster.hierarchy import fclusterdata

X = [[0, 0], [0, 1], [1, 0],
     [0, 4], [0, 3], [1, 4],
     [4, 0], [3, 0], [4, 1],
     [4, 4], [3, 4], [4, 3]]

T = fclusterdata(X, t=1)
print(T)              # 预期: [3 3 3 4 4 4 2 2 2 1 1 1]
print(T.dtype)        # 预期: int32
print(len(set(T)))    # 预期: 4（四个簇）
```

**需要观察的现象**：

1. 返回值是一维 `int32` 数组，长度等于观测数 12。
2. 标签取值范围是 1–4（**从 1 起**，不是从 0），共 4 个不同簇。
3. 每个簇正好 3 个点（可以用 `np.bincount(T)` 或 `pd.Series(T).value_counts()` 验证）。

**预期结果**：与 docstring 完全一致——`array([3, 3, 3, 4, 4, 4, 2, 2, 2, 1, 1, 1], dtype=int32)`，4 个簇各 3 点。

> 说明：簇的**编号本身**（哪个簇叫 1、哪个叫 4）是由合并顺序决定的，对同一输入是**确定性**的；但它在语义上没有大小含义。比较两个聚类结果时，应比较「分组是否一致」（标签置换不变），而不是直接比数字。

#### 4.1.5 小练习与答案

**练习 1**：把上面示例的 `t` 改成 `2`，再改成 `0.5`，簇数分别会变多还是变少？为什么？

> **参考答案**：`inconsistent` 准则里 `t` 是「不一致系数的阈值」——`t` 越大，越多簇节点满足「不一致值 ≤ t」从而被并入更大的簇，因此簇数**变少**；`t` 越小则切得更细、簇数**变多**。所以 `t=2` 会得到 ≤4 个簇（甚至可能全部并成 1 个），`t=0.5` 会得到 ≥4 个簇。具体数字「待本地验证」。

**练习 2**：`fclusterdata` 头上的装饰器是 `lazy_cython` 吗？为什么？

> **参考答案**：**不是**。它用的是直接写的 `@xp_capabilities(cpu_only=True, reason="Cython code", jax_jit=False, allow_dask_compute=True)`，与 `lazy_cython`（带 dask「merges chunks」告警的复用别名）不同。原因是 `fclusterdata` 本身是纯 Python 编排函数，dask 相关告警应由它内部真正调用 Cython 的 `linkage` / `fcluster` 各自负责。

---

### 4.2 三步流水线的内部组装：pdist → linkage → inconsistent → fcluster

#### 4.2.1 概念说明

`fclusterdata` 的函数体只有一个职责：**按固定顺序组装好三（或四）步调用**。理解它的关键是搞清「每个内部函数吃什么、吐什么、`fclusterdata` 的哪个参数喂给它」。

四步各自的角色：

1. **`distance.pdist(X, metric=metric)`**——把 N×M 的观测矩阵变成长度 \(n(n-1)/2\) 的压缩距离向量 `Y`。`metric` 决定用什么距离（默认欧氏）。这是 `fclusterdata` 与 `linkage` 最大的不同点：`linkage` 让你自己喂距离，`fclusterdata` 替你算。
2. **`linkage(Y, method=method)`**——把 `Y` 凝聚成 \((n-1)\times 4\) 的树 `Z`。`method` 决定合并策略（默认 single）。
3. **`inconsistent(Z, d=depth)`**——（仅当 `R=None` 时）算不一致矩阵 `R`，供默认的 `inconsistent` 准则使用。如果你传了 `R`，这一步被跳过。
4. **`fcluster(Z, criterion=criterion, depth=depth, R=R, t=t)`**——在 `Z` 上按 `criterion` 切平，返回标签 `T`。

> 一个容易忽略的点：`depth` 参数**同时**喂给了第 3 步（`inconsistent` 的 `d`）和第 4 步（`fcluster` 的 `depth`）。只有 `inconsistent` / `monocrit` 系准则会用 `depth`，其它准则忽略它——所以改 `depth` 在 `criterion='distance'` 下不会有任何效果（详见 4.2.4 的实践）。

#### 4.2.2 核心流程

`fclusterdata` 函数体（第 2689–2702 行）做的事，按顺序是：

```text
1. 多后端规整：  xp = array_namespace(X)
                 X  = _asarray(X, order='C', dtype=float64)
2. 形状校验：    若 X.ndim != 2 → raise TypeError
3. 第一步算距离：Y = pdist(X, metric=metric)
4. 第二步建树：   Z = linkage(Y, method=method)
5. 第三步备 R：   if R is None: R = inconsistent(Z, d=depth)
                 else:          R = _asarray(R, order='C')
6. 第四步切平：   T = fcluster(Z, criterion=criterion, depth=depth, R=R, t=t)
7. 返回：         return T
```

各中间产物的形状流转：

| 变量 | 含义 | 形状 / 长度 |
|------|------|-------------|
| `X` | 观测矩阵（输入） | \(N\times M\) |
| `Y` | 压缩距离向量 | \(N(N-1)/2\) |
| `Z` | linkage matrix（树） | \((N-1)\times 4\) |
| `R` | 不一致矩阵（可选） | \((N-1)\times 4\) |
| `T` | 扁平簇标签（输出） | \(N\) |

#### 4.2.3 源码精读

现在逐行看真正的函数体。先看输入规整与形状校验：

```python
    xp = array_namespace(X)
    X = _asarray(X, order='C', dtype=xp.float64, xp=xp)

    if X.ndim != 2:
        raise TypeError('The observation matrix X must be an n by m array.')
```

[见 hierarchy/_hierarchy_impl.py:L2689-L2693（规整 + 形状校验）](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2689-L2693)

- `array_namespace(X)`：取数组后端命名空间（默认 NumPy，开了 `SCIPY_ARRAY_API` 才可能是别的）。这与 u2-l1、u3-l3 一致。
- `_asarray(X, order='C', dtype=xp.float64, ...)`：转成 **C 连续的 float64**。`order='C'` 很关键——后续 `pdist` 和 Cython 后端都期望连续内存布局。
- `X.ndim != 2`：**只接受二维**。这就是「`fclusterdata` 只吃观测矩阵」的硬性约束：传一维压缩距离向量进来会被拒（`linkage` 能吃一维，但 `fclusterdata` 不能）。

接着是核心的三步流水线：

```python
    Y = distance.pdist(X, metric=metric)
    Z = linkage(Y, method=method)
    if R is None:
        R = inconsistent(Z, d=depth)
    else:
        R = _asarray(R, order='C', xp=xp)
    T = fcluster(Z, criterion=criterion, depth=depth, R=R, t=t)
    return T
```

[见 hierarchy/_hierarchy_impl.py:L2695-L2702（三步流水线主体）](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2695-L2702)

逐行解读：

- `Y = distance.pdist(X, metric=metric)`（[L2695](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2695)）：`distance` 是文件顶部 `import scipy.spatial.distance as distance`（[L44](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L44)）的别名。`metric` 直接透传。
- `Z = linkage(Y, method=method)`（[L2696](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2696)）：注意这里 `linkage` 收到的是**一维的 `Y`**，所以 `linkage` 内部不会再做 `pdist`，也**不会**对 `metric` 做欧氏校验（见 u3-l2：欧氏校验只在 2D 输入时触发）。这是一个重要后果——见下方「兼容性陷阱」。
- `R` 分支（[L2697-L2700](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2697-L2700)）：`R=None` 时用 `inconsistent(Z, d=depth)` 现算（[L2698](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2698)）；用户传了 `R` 就只做 `_asarray` 规整、**不复算**。这让「批量在同一棵树上用不同 `t` 切」成为可能（外部预算一次 `R`、复用）。
- `T = fcluster(Z, criterion=criterion, depth=depth, R=R, t=t)`（[L2701](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2701)）：`Z` 和 `R` 已经备好，把切树工作完全交给 u5-l1 讲过的 `fcluster`。注意 `fclusterdata` **没有** `monocrit` 参数，所以这里也不传——这就是为什么 `fclusterdata` 不支持 `monocrit` / `maxclust_monocrit` 准则。
- `return T`（[L2702](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2702)）：直接返回，**不做任何额外包装**。所以 `fclusterdata` 的返回值就是 `fcluster` 的返回值（一维 `int32`，从 1 起）。

**参数透传映射表**（本讲最重要的一张表）：

| `fclusterdata` 参数 | 默认值 | 透传给哪个内部函数 | 对应实参 |
|---|---|---|---|
| `X` | — | `distance.pdist` | 第 1 实参 |
| `metric` | `'euclidean'` | `distance.pdist` | `metric=` |
| `method` | `'single'` | `linkage` | `method=` |
| `depth` | `2` | `inconsistent` **和** `fcluster` | `inconsistent(d=)`、`fcluster(depth=)` |
| `criterion` | `'inconsistent'` | `fcluster` | `criterion=` |
| `t` | — | `fcluster` | `t=` |
| `R` | `None` | `fcluster`（`None` 时先由 `inconsistent` 现算） | `R=` |

对照三个内部函数的签名可以验证这张表：

- `linkage(y, method='single', metric='euclidean', optimal_ordering=False)`（[L723](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L723)）：`fclusterdata` 只用了它的 `method`，没用 `optimal_ordering`（这是另一个被「吞掉」的能力）。
- `inconsistent(Z, d=2)`（[L1623](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1623)）：`fclusterdata` 把 `depth` 喂给它的 `d`。
- `fcluster(Z, t, criterion='inconsistent', depth=2, R=None, monocrit=None)`（[L2420](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2420)）：`fclusterdata` 用了 `criterion`/`depth`/`R`/`t`，**没用** `monocrit`。

**兼容性陷阱（重要）**：因为 `linkage` 收到的是一维 `Y`，`linkage` 不会校验 `metric` 是否与 `method` 兼容。docstring 因此提醒用户「See `distance.pdist` for descriptions and `linkage` to verify compatibility with the linkage method」。具体后果：`method='ward'` / `'centroid'` / `'median'` 这三种在数学上**要求欧氏距离**，但如果你传 `metric='cityblock'`，`fclusterdata` **不会报错**，会静默算出一个数学上不太站得住脚的结果。要避免这种情况，用这三法时别改 `metric`（保持默认 `'euclidean'`）。这一行为「待本地验证」细节，但 docstring 的兼容性提醒已明确把责任交给用户。

#### 4.2.4 代码实践

**实践目标**：验证「参数透传」——证明 `fclusterdata(X, t, criterion=..., method=..., metric=..., depth=...)` 与「用**相同**参数手动走三步」得到的标签**逐位相同**；并顺便验证 `depth` 对 `distance` 准则无效。

**操作步骤**（示例代码，需本地运行）：

```python
# 示例代码
import numpy as np
from scipy.spatial.distance import pdist
from scipy.cluster.hierarchy import fclusterdata, linkage, inconsistent, fcluster

rng = np.random.default_rng(0)
X = np.vstack([rng.normal(0, 0.3, size=(15, 2)),
               rng.normal(4, 0.3, size=(15, 2)),
               rng.normal([0, 4], 0.3, size=(15, 2))])   # 45×2，三团

# —— 一站式 ——
T_one = fclusterdata(X, t=3, criterion='maxclust',
                     method='ward', metric='euclidean', depth=2)

# —— 手动三步（参数与上面完全对应）——
Y = pdist(X, metric='euclidean')                  # 对应 metric
Z = linkage(Y, method='ward')                     # 对应 method
R = inconsistent(Z, d=2)                          # 对应 depth
T_manual = fcluster(Z, t=3, criterion='maxclust', depth=2, R=R)  # 对应 criterion/t/depth/R

print(np.array_equal(T_one, T_manual))   # 预期: True（参数一致 → 逐位相同）
print(np.bincount(T_one))                # 预期: 前导 0，后三段各约 15

# —— 验证 depth 对 distance 准则无效 ——
Td_depth2  = fclusterdata(X, t=5.0, criterion='distance', depth=2,  method='ward')
Td_depth99 = fclusterdata(X, t=5.0, criterion='distance', depth=99, method='ward')
print(np.array_equal(Td_depth2, Td_depth99))   # 预期: True（depth 被忽略）
```

**需要观察的现象**：

1. `np.array_equal(T_one, T_manual)` 为 `True`——这证明 `fclusterdata` 确实只是「按相同参数串了三步」，没有任何额外魔法，连标签编号都一致（因为计算路径完全相同）。
2. `np.bincount(T_one)` 显示标签恰好分成 3 段、每段约 15（因为我们用 maxclust=3 造了三团数据）。
3. `distance` 准则下，`depth=2` 与 `depth=99` 结果完全相同——印证 `depth` 只对 `inconsistent` 系准则有意义。

**预期结果**：两行 `True`；`bincount` 呈现 3 个簇。若你的数据三团分得开，簇大小应接近 `[15, 15, 15]`（顺序可能不同）。具体数字「待本地验证」。

> 比较技巧：因为这里两条路径用的是**同一棵树、同一个 `R`**，标签是逐位相同的，可以直接用 `np.array_equal`。如果换成「不同 method 的结果对比」（如 ward vs single），标签编号不再对应，就要用 `from scipy.cluster.hierarchy import leaders` 之类做标签置换无关的比较，或者直接比「分组是否一致」。

#### 4.2.5 小练习与答案

**练习 1**：`fclusterdata` 支持 `criterion='monocrit'` 吗？为什么？

> **参考答案**：**不支持**。`monocrit` 准则需要用户传入一个单调的 `monocrit` 数组，而 `fclusterdata` 的签名里**没有** `monocrit` 参数（函数体调 `fcluster` 时也没传它），docstring 也只列出 `'inconsistent'`、`'distance'`、`'maxclust'` 三种。要用 `monocrit` 必须手动走 `linkage` → `fcluster(..., criterion='monocrit', monocrit=MR)`。

**练习 2**：如果你想「在同一棵树上、用 10 个不同的阈值 `t` 各切一次」，应该用 `fclusterdata` 还是手动三步？为什么？

> **参考答案**：应该**手动三步**。`fclusterdata` 每调一次都会**重新建树**（重跑 `pdist` + `linkage`），10 次调用就建了 10 次同一棵树，浪费计算。手动时你只建一次 `Z`、算一次 `R`，然后对 10 个 `t` 各调一次 `fcluster`，省下 9 次 `pdist`/`linkage`。这正是 4.1.2 说的「不适合」场景之一。

**练习 3**：`fclusterdata(X, t=2)`（默认 `method='single'`）与 `linkage(X, method='single')` 后再 `fcluster`，在 `metric` 默认时结果一致；那 `fclusterdata` 把 `X` 传给 `linkage` 了吗？

> **参考答案**：**没有**。`fclusterdata` 先用 `pdist(X)` 把 `X` 变成压缩距离向量 `Y`，再把 `Y`（而不是 `X`）传给 `linkage`。所以 `linkage` 收到的是一维距离矩阵，走的是它的「1D 输入」分支，不会再内部 `pdist` 一次。这正好解释了为什么 `linkage` 在这里**不会**对 `method` 做欧氏度量校验（兼容性陷阱）。

---

## 5. 综合实践

**任务**：用 `fclusterdata` 对一组二维点得到「指定簇数」的标签，再用 `pdists + linkage + fcluster` 手动复现**完全相同**的结果，验证 `fclusterdata` 确实只是这三步的封装；最后探查一个 `fclusterdata` 隐藏的兼容性陷阱。

**步骤 1：造数据 + 一站式得到 3 个簇**

```python
# 示例代码
import numpy as np
from scipy.cluster.hierarchy import fclusterdata

rng = np.random.default_rng(42)
X = np.vstack([rng.normal([0, 0], 0.4, size=(20, 2)),
               rng.normal([6, 0], 0.4, size=(20, 2)),
               rng.normal([3, 5], 0.4, size=(20, 2))])   # 60×2，明显三团

T_auto = fclusterdata(X, t=3, criterion='maxclust', method='ward')
print("簇数:", len(set(T_auto)))          # 预期 3
print("每簇大小:", np.bincount(T_auto))   # 预期三段各约 20
```

**步骤 2：手动三步复现**

```python
# 示例代码
from scipy.spatial.distance import pdist
from scipy.cluster.hierarchy import linkage, inconsistent, fcluster

Y = pdist(X, metric='euclidean')                 # 与 fclusterdata 默认 metric 对应
Z = linkage(Y, method='ward')                    # 与 method='ward' 对应
R = inconsistent(Z, d=2)                         # 与默认 depth=2 对应
T_manual = fcluster(Z, t=3, criterion='maxclust', depth=2, R=R)

print("逐位相同:", np.array_equal(T_auto, T_manual))   # 预期 True
```

**步骤 3：探查兼容性陷阱（ward + 非欧氏度量）**

```python
# 示例代码
# ward 数学上要求欧氏距离；这里故意用 cityblock，观察 fclusterdata 是否拦你
T_bad = fclusterdata(X, t=3, criterion='maxclust',
                     method='ward', metric='cityblock')   # 不会报错！
print("非欧氏 + ward 也跑出结果，簇数:", len(set(T_bad)))
# 正确做法：用 ward 就保持 metric='euclidean'（默认）
```

**预期结果与现象**：

1. 步骤 1：得到正好 3 个簇，每簇约 20 点。
2. 步骤 2：`np.array_equal` 为 `True`——**铁证** `fclusterdata` 就是这三步的封装。
3. 步骤 3：`fclusterdata` **不会**因 `ward + cityblock` 抛错，会静默返回结果。这印证了 4.2.3 讲的兼容性陷阱：因为 `linkage` 收到的是 1D 距离向量、不做欧氏校验，兼容性责任全在用户。具体标签「待本地验证」，但「不报错」这一点是确定的。

**思考延伸**：如果步骤 2 把 `fcluster` 的 `criterion` 换成 `'distance'`、`t=5.0`，`R` 还有用吗？（答：没用。`distance` 准则不看 `R`，只看 `Z` 的合并高度。你甚至可以传 `R=None` 给 `fcluster`。但 `fclusterdata` 无论准则是什么都会**无条件**算 `R`——这也是它不如手动三步「省」的一个小地方。）

## 6. 本讲小结

- `fclusterdata` 是一个**纯 Python 便捷函数**，把 `pdist → linkage → inconsistent → fcluster` 打包成一个入口，只返回扁平簇标签 `T`；其注释表明它对标 MATLAB 的 `clusterdata`。
- 它**只接受二维观测矩阵 `X`**（不接受距离矩阵），在 `_asarray` 转 C 连续 float64 后强制 `X.ndim==2`；这与 `linkage`「距离矩阵 / 观测矩阵都吃」不同。
- 函数体（第 2689–2702 行）就四步：`pdist(X, metric)` → `linkage(Y, method)` → `inconsistent(Z, d=depth)`（`R=None` 时）→ `fcluster(Z, criterion, depth, R, t)`。
- 参数透传要点：`metric→pdist`、`method→linkage`、`depth→inconsistent 和 fcluster 两处`、`criterion/t/R→fcluster`；默认是 `euclidean + single + inconsistent + depth=2`。
- 它**丢失**了三个能力：`monocrit`（故不支持 `monocrit` / `maxclust_monocrit` 准则）、`optimal_ordering`、以及对 `method` 与 `metric` 兼容性的自动校验（`ward/centroid/median` 配非欧氏度量会静默跑过，由用户自负）。
- 选择建议：只想要标签且用常见配置 → `fclusterdata`；需要 `Z`/dendrogram、需要 `monocrit`、要在同一棵树上切多次、要细控距离度量 → 手动三步。

## 7. 下一步学习建议

- **u5-l3（inconsistent 与 max 系列统计）**：本讲反复提到 `R = inconsistent(Z, d=depth)`，下一讲会讲清 `R` 四列（均值/标准差/计数/不一致系数）的含义、`depth` 如何限定计算范围，以及 `maxRstat` / `maxinconsts` 如何把 `R` 聚合成单调的 `monocrit`——这正是 `fclusterdata` 不支持、你必须手动走三步才能用的 `monocrit` 准则的基础。
- **u5-l4（cophenetic 距离与 cophenet 相关系数）**：本讲的 `distance` 准则本质是切「cophenetic 距离」，下一讲会讲清这个量的定义和如何用它评估一棵树对原始距离的保真度。
- **u6（树的表示、可视化与最优叶序）**：`fclusterdata` 故意不返回 `Z`，所以画不了 dendrogram；若要可视化，回到手动 `linkage` 拿到 `Z` 后，按 u6-l1（`to_tree` / `ClusterNode`）、u6-l2（`dendrogram`）继续。
- **源码延伸阅读**：把本讲的 `fclusterdata` 函数体（第 2604–2702 行）和 u5-l1 的 `fcluster`、u3-l2 的 `linkage` 三处源码并排读一遍，你会对「hierarchy 模块的 Python 封装层如何用薄函数互相串联」有完整的认识。
