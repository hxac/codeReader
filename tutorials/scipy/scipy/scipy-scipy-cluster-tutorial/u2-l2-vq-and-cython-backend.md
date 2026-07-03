# vq 编码函数与 Cython 后端 _vq.vq

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `scipy.cluster.vq.vq` 的输入（观测矩阵 `obs` 与码本 `code_book`）和输出（`code` 最近码字索引、`dist` 畸变）分别是什么。
- 看懂 `vq()` 函数中「浮点走 Cython 编译后端 `_vq.vq`、非浮点回退纯 Python `_py_vq`」的类型分发逻辑，并理解为什么这样设计。
- 读懂 Cython 包装层 `_vq.vq` 如何做严格的类型/维度校验、初始化输出数组。
- 理解核心 C 函数 `_vq._vq` 与 `cal_M` 如何用「展开平方距离 + BLAS 矩阵乘」把朴素的逐对距离计算加速，以及在特征数很小时为何回退到朴素算法。

本讲承接 [u2-l1 whiten](u2-l1-whiten-preprocessing.md)：白化后的 `obs` 接下来就要喂给 `vq` 做「编码」，而 `vq` 正是 k-means 迭代里被反复调用的热点函数，理解它的两层（Python 分发 + Cython 计算）结构是后续 [kmeans 迭代](u2-l3-kmeans-iteration.md) 的基础。

## 2. 前置知识

### 2.1 向量量化（Vector Quantization, VQ）与编码

向量量化的任务是：给定一个「码本」（code book，即一组代表性向量，也叫簇心/centroids），把每一个观测向量映射到离它**最近**的那个码字上。这个映射过程就叫「编码」。

- **观测矩阵 `obs`**：形状 `M×N`，`M` 个观测，每个观测 `N` 个特征。第 `i` 行 `obs[i]` 是一个观测。
- **码本 `code_book`**：形状 `k×N`，`k` 个码字（簇心）。第 `j` 行 `code_book[j]` 是第 `j` 个码字。
- **编码 `code`**：长度 `M` 的整数数组，`code[i]` 是离 `obs[i]` 最近的码字的**行索引**（`0` 到 `k-1`）。
- **畸变 `dist`**：长度 `M` 的浮点数组，`dist[i]` 是 `obs[i]` 到其最近码字的欧氏距离。

### 2.2 欧氏距离与「平方距离展开」技巧

两个 `N` 维向量 `a`、`b` 的欧氏距离的平方为：

\[
\lVert a - b \rVert^{2} = \sum_{n=1}^{N}(a_n - b_n)^{2}
\]

直接展开它，可以得到一个关键恒等式：

\[
\lVert a - b \rVert^{2} = \lVert a \rVert^{2} - 2\,(a \cdot b) + \lVert b \rVert^{2}
\]

其中 `a · b` 是内积。这个展开式是本讲 Cython 核心的灵魂：它把「`M×k` 个距离」的计算，转化成「一次矩阵乘（算所有内积）+ 两组向量各自平方和（算各自范数）」。矩阵乘可以调用高度优化的 BLAS 例程 `gemm`，比朴素三重循环快得多。请先记住这个恒等式，第 4.4 节会看到它如何落地。

### 2.3 Python 层与 Cython 层的分工（回顾）

正如 [u1-l2 结构与构建](u1-l2-structure-and-build.md) 所述，`scipy.cluster.vq` 采用双层架构：

- **Python 封装层 `_vq_impl.py`**：负责 docstring、输入校验、类型分发，对用户友好（容忍各种类型）。
- **Cython 性能层 `_vq.pyx`**：负责跑热点循环，只接受严格的 `float32`/`float64` 数组，追求速度。

`vq()` 是前门（宽容），`_vq.vq` 是引擎（严格）。本讲的核心就是讲清楚这扇门如何把不同的输入路由到正确的引擎，以及引擎内部如何工作。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [vq/_vq_impl.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py) | Python 封装层。含 `vq()`（类型分发入口）、`_py_vq()`（纯 Python 回退）、`py_vq`（已弃用的公开别名）。 |
| [vq/_vq.pyx](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq.pyx) | Cython 性能层。含 `vq()`（ndarray 包装）、`_vq()`（核心 C 函数）、`cal_M()`（BLAS 距离内核）、`_vq_small_nf()`（小特征数朴素算法）。 |
| [vq/tests/test_vq.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/tests/test_vq.py) | 测试。`TestVq` 类用固定数据 `X`/`initc`/`LABEL1` 验证两条路径结果一致。 |

调用链全景（自顶向下）：

```
用户调用 vq(obs, code_book)                    # _vq_impl.py: vq()
   │
   ├─ 若公共 dtype 是浮点 ──→ _vq.vq(c_obs, c_code_book)   # _vq.pyx: vq()  包装层
   │                             │
   │                             └─→ _vq(...)              # _vq.pyx: _vq()  核心 C 函数
   │                                   ├─ nfeat < 5 → _vq_small_nf(...)   # 朴素
   │                                   └─ 否则      → cal_M(...) + 平方和 # BLAS
   │
   └─ 否则（如整数） ──────→ _py_vq(obs, code_book)        # _vq_impl.py: _py_vq()
                                  └─→ cdist + argmin + min  # 纯 Python
```

---

## 4. 核心概念与源码讲解

### 4.1 vq()：类型分发入口

#### 4.1.1 概念说明

用户面对的 `vq(obs, code_book)` 是一个「宽容的前门」：它不要求输入必须是某种特定 dtype，也不要求 `obs` 和 `code_book` dtype 必须一致。它的职责是：

1. 规整输入（`_asarray` + `check_finite`）。
2. 判断两个数组的**公共 dtype** 是否为「实数浮点」。
3. 若是 → 把数据交给编译过的 Cython 后端 `_vq.vq`（快路径）。
4. 若不是（例如整数、复数等）→ 回退到纯 Python 实现 `_py_vq`（慢路径，但兼容性好）。

这种「能用快的就用快的，不行就退而求其次」的设计，是 SciPy 在「性能」与「通用性」之间权衡的典型手法。

#### 4.1.2 核心流程

```text
vq(obs, code_book, check_finite=True):
  1. xp = array_namespace(obs, code_book)        # 取数组后端命名空间（numpy/jax/...）
  2. obs        = _asarray(obs, check_finite)     # 规整 + 有限值检查
     code_book = _asarray(code_book, check_finite)
  3. ct = xp.result_type(obs, code_book)          # 公共 dtype（如 float32+float64 → float64）
  4. 若 xp.isdtype(ct, kind='real floating'):     # 浮点快路径
        把 obs / code_book 都 astype 到 ct
        转成 numpy 数组
        result = _vq.vq(c_obs, c_code_book)        # 调 Cython
        用 xp.asarray 把结果包回原后端
     否则:                                         # 非浮点慢路径
        return _py_vq(obs, code_book, check_finite=False)
```

注意 `@xp_capabilities(cpu_only=True, ...)` 装饰器声明：这个函数只在 CPU 上跑（因为内部要用 `cdist`/Cython），不支持 `jax.jit`，但允许 dask 计算。这一点在 [u2-l1](u2-l1-whiten-preprocessing.md) 讲 `xp_capabilities` 时已提过。

#### 4.1.3 源码精读

整个 `vq()` 函数定义与分发逻辑：[vq/_vq_impl.py:86-157](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L86-L157) — 这是本讲的「主角」，负责规整、取公共 dtype、分发。

最关键的分发几行：[vq/_vq_impl.py:148-157](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L148-L157)

```python
ct = xp.result_type(obs, code_book)

if xp.isdtype(ct, kind='real floating'):
    c_obs = xp.astype(obs, ct, copy=False)
    c_code_book = xp.astype(code_book, ct, copy=False)
    c_obs = np.asarray(c_obs)
    c_code_book = np.asarray(c_code_book)
    result = _vq.vq(c_obs, c_code_book)
    return xp.asarray(result[0]), xp.asarray(result[1])
return _py_vq(obs, code_book, check_finite=False)
```

逐行解读：

- `ct = xp.result_type(obs, code_book)`：取两个数组的「运算公共 dtype」。例如 `float32` 与 `float64` 混合时结果是 `float64`；`int64` 与 `float64` 混合时结果是 `float64`；两个 `int64` 结果仍是 `int64`。
- `xp.isdtype(ct, kind='real floating')`：判断 `ct` 是不是实数浮点。**这是决定走哪条路径的唯一判据。**
- 浮点路径：先把两边都 `astype` 到公共浮点 dtype（解决 `float32`/`float64` 不一致的问题），再用 `np.asarray` 转成真正的 numpy 数组（Cython 后端只认 numpy 的 C 缓冲区），调用 `_vq.vq`，最后用 `xp.asarray` 把结果包回原数组后端（保持返回值类型与输入一致）。
- 非浮点路径：直接调用 `_py_vq`，并把 `check_finite=False` 传下去，因为前面已经检查过有限值了，无需重复检查。

> **为什么整数会走慢路径？** 因为 Cython 后端 `_vq.vq` 内部硬编码只支持 `float32`/`float64`（见 4.3 节）。整数输入无法直接喂给它，所以 `vq()` 把整数路由到纯 Python 的 `_py_vq`。测试 `test_vq` 正是用 `dtype` 参数化 `["float64", "int64"]` 来覆盖这两条路径。

#### 4.1.4 代码实践

**实践目标**：亲手触发两条不同的分发路径，观察输出 dtype 的差异。

**操作步骤**：

```python
import numpy as np
from scipy.cluster.vq import vq

# 同一组数据，分别用 float64 和 int64
obs       = np.array([[3.0, 3], [4, 3], [9, 2]])
code_book = np.array([[3.0, 3], [6.2, 4.0], [5.8, 1.8]])

code_f, dist_f = vq(obs, code_book)                          # float64 → Cython 路径
code_i, dist_i = vq(obs.astype(np.int64), code_book.astype(np.int64))  # int64 → _py_vq 路径

print("float 路径 code dtype:", code_f.dtype)   # 期望 int32（来自 Cython）
print("int   路径 code dtype:", code_i.dtype)   # 期望 int64（来自 numpy argmin）
print("float code:", code_f)
print("int   code:", code_i)
```

**需要观察的现象**：

- 两次得到的 `code`（最近码字索引）数值应当**完全相同**——这说明两条路径计算结果一致，只是实现不同。
- `code_f.dtype` 是 `int32`（Cython 后端 `outcodes` 的类型，见 4.3 节），而 `code_i.dtype` 是 `int64`（numpy 的 `argmin` 默认返回 `intp`，在 64 位平台是 `int64`）。这正是测试里反复出现的注释 `# label1.dtype varies between int32 and int64 over platforms` 的来源。

**预期结果**：两组 `code` 数值相等；dtype 不同。若在你的环境上结果与此不符，标记为「待本地验证」并记录实际输出。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `obs` 传成 `float32`、`code_book` 传成 `float64`，`vq()` 会报错吗？最终走哪条路径？

**参考答案**：不会报错。`result_type(float32, float64) = float64`，是实数浮点，走 Cython 快路径。`vq()` 会先把两边都 `astype` 到 `float64` 再交给 `_vq.vq`。注意：这是 `vq()` 的宽容行为；若你直接调底层 `_vq.vq(float32_obs, float64_book)` 反而会抛 `TypeError`（见 4.3 节的 dtype 一致性检查）。

**练习 2**：为什么 `vq()` 在回退 `_py_vq` 时要显式传 `check_finite=False`？

**参考答案**：因为 `vq()` 自己在最前面已经用 `_asarray(..., check_finite=True)` 对 `obs` 和 `code_book` 做过有限值检查了。回退 `_py_vq` 时若再检查一次是重复劳动，所以关掉。

---

### 4.2 _py_vq：纯 Python 回退实现

#### 4.2.1 概念说明

`_py_vq` 是 `vq` 的「保底实现」：用纯 Python + numpy 写成，不依赖任何编译代码。它适用于两类场景：

1. 输入 dtype 不是 `float32`/`float64`（如整数、复数）——Cython 后端不收。
2. 作为正确性的「参照实现」——测试里反复用它和 Cython 后端对照（见 `test_vq_large_nfeat` 等）。

它的源码注释直言：比 C 版本慢约 20 倍（"about 20 times slower than the C version"）。

#### 4.2.2 核心流程

```text
_py_vq(obs, code_book, check_finite=True):
  1. 规整输入 + 有限值检查
  2. 若 obs.ndim != code_book.ndim → 抛 ValueError
  3. 若是 1 维 → 各自升维成列向量 (obs[:, newaxis])
  4. dist = cdist(obs, code_book)        # M×k 的两两欧氏距离矩阵
  5. code     = argmin(dist, axis=1)     # 每行最小值的列索引 = 最近码字
  6. min_dist = min(dist, axis=1)        # 每行最小值 = 到最近码字的距离
  7. return code, min_dist
```

这里的 `cdist` 来自 `scipy.spatial.distance`，默认度量是欧氏距离（**非平方**）。这一点很重要：它决定了 `_py_vq` 返回的 `dist` 是真正的欧氏距离，于是 Cython 路径也必须返回欧氏距离才能两者对齐（见 4.4 节末尾的 `sqrt`）。

#### 4.2.3 源码精读

`_py_vq` 的完整实现：[vq/_vq_impl.py:160-212](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L160-L212)

距离计算与取最小值的三行核心：[vq/_vq_impl.py:208-212](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L208-L212)

```python
# Once `cdist` has array API support, this `xp.asarray` call can be removed
dist = xp.asarray(cdist(obs, code_book))
code = xp.argmin(dist, axis=1)
min_dist = xp.min(dist, axis=1)
return code, min_dist
```

解读：

- `cdist(obs, code_book)` 计算 `M×k` 矩阵，`dist[i, j]` 是 `obs[i]` 到 `code_book[j]` 的欧氏距离。由于 `cdist` 目前还不支持 array API（只认 numpy），结果要用 `xp.asarray` 包一层以兼容其他后端——注释说明待 `cdist` 支持 array API 后这行可移除。
- `argmin(dist, axis=1)` 沿「码字」轴取最小值索引，得到每个观测的最近码字编号 `code`。
- `min(dist, axis=1)` 沿同一轴取最小值，得到每个观测到最近码字的距离 `min_dist`。

升维处理：[vq/_vq_impl.py:204-206](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L204-L206) 说明当输入是 1 维（标量观测）时，用 `obs[:, xp.newaxis]` 升成列向量，统一走 2 维逻辑。

> **关于公开的 `py_vq`**：[vq/_vq_impl.py:215-219](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L215-L219) 把 `_py_vq` 用 `_deprecated` 装饰成了一个公开但已弃用的别名 `py_vq`，并会在 SciPy 1.20.0 移除。`__init__.py` 里也专门为它追加了 `__all__`。日常代码请用 `vq`，不要用 `py_vq`。

#### 4.2.4 代码实践

**实践目标**：直接调用 `_py_vq`，验证它就是「cdist + argmin」的直白封装。

**操作步骤**：

```python
import numpy as np
from scipy.cluster.vq._vq_impl import _py_vq
from scipy.spatial.distance import cdist

obs       = np.array([[3.0, 3], [4, 3], [9, 2]])
code_book = np.array([[3.0, 3], [6.2, 4.0], [5.8, 1.8]])

# 官方 _py_vq
code, dist = _py_vq(obs, code_book, check_finite=False)

# 手动复现
D = cdist(obs, code_book)
my_code = D.argmin(axis=1)
my_dist = D.min(axis=1)

print("code 一致:", np.array_equal(code, my_code))   # 期望 True
print("dist 一致:", np.allclose(dist, my_dist))      # 期望 True
```

**需要观察的现象**：手动用 `cdist + argmin + min` 得到的 `code`、`dist` 与 `_py_vq` 完全一致。

**预期结果**：两个布尔值都为 `True`。这印证了 `_py_vq` 的实现就是这三步。

#### 4.2.5 小练习与答案

**练习 1**：`_py_vq` 返回的 `dist` 是平方欧氏距离还是欧氏距离？为什么这一点对和 Cython 路径对齐很关键？

**参考答案**：是欧氏距离（非平方），因为 `cdist` 默认度量 `'euclidean'` 返回的就是开过根号的距离。这要求 Cython 路径 `_vq._vq` 在内部算完平方距离后也必须 `sqrt`（见 4.4 节），否则两条路径的 `dist` 差一个根号，无法对齐。测试 `test_vq_large_nfeat` 正是用 `xp_assert_close(dis1, dis0)` 直接比较两者的 `dist`。

**练习 2**：为什么 `_py_vq` 要在 1 维输入时做 `obs[:, xp.newaxis]` 升维？

**参考答案**：`cdist` 要求 2 维输入（每行一个向量）。1 维数组语义上是「若干个标量观测」，每个观测只有 1 个特征，升成列向量（`M×1`）后才能与 `code_book`（`k×1`）正确配对计算距离。

---

### 4.3 _vq.vq：Cython 包装层（严格的 ndarray 前置层）

#### 4.3.1 概念说明

`_vq.pyx` 里定义的 `vq(np.ndarray obs, np.ndarray codes)` 是 Cython 后端的「包装层」。它不做距离计算本身，而是承担三件事：

1. **连续化**：用 `np.ascontiguousarray` 保证内存布局连续（Cython 要拿裸指针）。
2. **严格校验**：dtype 必须一致、必须是 `float32`/`float64`、ndim 必须一致、特征数（列数）必须一致。
3. **分发到核心 C 函数 `_vq`**：按 dtype 是 `float32` 还是 `float64` 调用对应模板实例。

它和 Python 层的 `vq()` 形成鲜明对比：后者宽容，前者严格。这也是为什么 Python 层要先 `astype` 到公共 dtype 再交给它——绝不能让两个不同 dtype 的数组到达这里。

#### 4.3.2 核心流程

```text
_vq.vq(obs, codes):                        # _vq.pyx
  1. obs, codes = ascontiguousarray(...)    # 连续化
  2. 校验:
       obs.dtype == codes.dtype             否则 TypeError
       dtype ∈ {float32, float64}           否则 TypeError
       obs.ndim == codes.ndim               否则 ValueError
       (2D 时) obs 列数 == codes 列数        否则 ValueError
       ndim ∈ {1, 2}                        否则 ValueError
  3. 解析 nobs / ncodes / nfeat
  4. outdists = empty(nobs, dtype=obs.dtype); outdists.fill(inf)
     outcodes = empty(nobs, dtype=int32)
  5. 按 dtype 调用 _vq(<指针>, ..., outcodes, outdists)
  6. return outcodes, outdists
```

注意输出数组的设计：`outdists` 初始化为 `+inf`，`outcodes` 为 `int32`。把 `outdists` 初始化成无穷大，是让核心函数里的「比谁更小」逻辑能正确工作（见 4.4 节）。

#### 4.3.3 源码精读

Cython 包装层 `vq()` 的定义：[vq/_vq.pyx:177-238](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq.pyx#L177-L238)

连续化与严格校验：[vq/_vq.pyx:197-221](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq.pyx#L197-L221)

```python
# Ensure the arrays are contiguous
obs = np.ascontiguousarray(obs)
codes = np.ascontiguousarray(codes)

if obs.dtype != codes.dtype:
    raise TypeError('observation and code should have same dtype')
if obs.dtype not in (np.float32, np.float64):
    raise TypeError('type other than float or double not supported')
if obs.ndim != codes.ndim:
    raise ValueError(...)
...
```

输出数组初始化与按 dtype 分发到核心函数：[vq/_vq.pyx:223-238](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq.pyx#L223-L238)

```python
outdists = np.empty((nobs,), dtype=obs.dtype)
outcodes = np.empty((nobs,), dtype=np.int32)
outdists.fill(np.inf)

if obs.dtype.type is np.float32:
    _vq(<float32_t *>obs.data, <float32_t *>codes.data,
        ncodes, nfeat, nobs, <int32_t *>outcodes.data,
        <float32_t *>outdists.data)
elif obs.dtype.type is np.float64:
    _vq(<float64_t *>obs.data, <float64_t *>codes.data,
        ncodes, nfeat, nobs, <int32_t *>outcodes.data,
        <float64_t *>outdists.data)

return outcodes, outdists
```

要点：

- `outcodes` 恒为 `int32`——这解释了 4.1.4 节里 `code_f.dtype` 为何是 `int32`。
- `_vq(...)` 通过 `<float32_t *>obs.data` 这种「取裸指针」的方式把 numpy 数组的内存直接交给 C 函数，零拷贝。`_vq` 是一个 Cython **fused type**（融合类型）函数，对 `float32_t` 和 `float64_t` 各实例化一份（见文件顶部 `ctypedef fused vq_type`），所以这里按 dtype 二选一调用。

> **测试印证**：`test__vq_invalid_type` 直接给 `_vq.vq` 传整数数组，期望它抛 `TypeError`（因为整数不在 `{float32, float64}` 里）；`test__vq_sametype` 传一个 `float64`、一个 `float32`，期望抛 `TypeError`（dtype 不一致）。这两个测试保护的正是上面这两段校验。注意：公开的 `vq()` 因为先做了 `astype`，永远不会触发这些错误。

#### 4.3.4 代码实践

**实践目标**：体会「宽容前门 vs 严格引擎」的差别——公开 `vq` 不报错的输入，直接调底层 `_vq.vq` 会报错。

**操作步骤**：

```python
import numpy as np
from scipy.cluster.vq import vq
from scipy.cluster.vq._vq_impl import _vq

obs_f32 = np.array([[3.0, 3], [4, 3]], dtype=np.float32)
cb_f64  = np.array([[3.0, 3], [6.2, 4.0]], dtype=np.float64)

# (a) 公开 vq：宽容，自动统一 dtype，不报错
code, dist = vq(obs_f32, cb_f64)
print("公开 vq 正常返回:", code)

# (b) 直接调底层 _vq.vq：严格，dtype 不一致 → TypeError
try:
    _vq.vq(obs_f32, cb_f64)
except TypeError as e:
    print("底层 _vq.vq 报错:", e)
```

**需要观察的现象**：(a) 正常返回；(b) 抛 `TypeError: observation and code should have same dtype`。

**预期结果**：与上述一致。这条对比直观展示了 Python 层 `vq()` 的「astype 统一 dtype」起到了什么保护作用。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `outdists` 要 `.fill(np.inf)` 而 `outcodes` 不用初始化填充？

**参考答案**：核心函数 `_vq` 用「如果 `dist_sqr < low_dist[i]` 则更新」来选最小值。`low_dist` 就是 `outdists`，初始化为 `+inf` 保证第一个码字的距离一定比它小、从而被选中；之后任何更小的也会更新它。`outcodes` 只在「找到更小距离」时才被赋值，最终一定会被赋上某个 `j`，所以不需要预填。

**练习 2**：`_vq.vq` 用了 `np.ascontiguousarray`。如果传入一个转置得到的非连续视图（`arr.T`），会发生什么？

**参考答案**：`ascontiguousarray` 会复制一份成 C 连续布局的数组再处理，逻辑上结果不变，但多了一次内存拷贝。这是为了安全拿裸指针——非连续内存的指针运算会越界或取错值。

---

### 4.4 _vq._vq 与 cal_M：BLAS 加速的距离计算内核

#### 4.4.1 概念说明

这是整个 vq 模块性能的核心。核心 C 函数 `_vq`（在 `_vq.pyx` 里，名字与包装层同名但小写内部）要计算每个观测到每个码字的欧氏距离并取最小。朴素做法是三重循环（`M × k × N`），当特征数 `N` 大时很慢。

关键优化来自第 2.2 节那个恒等式：

\[
\lVert \text{obs}_i - \text{code}_j \rVert^{2}
= \lVert \text{obs}_i \rVert^{2} - 2\,(\text{obs}_i \cdot \text{code}_j) + \lVert \text{code}_j \rVert^{2}
\]

把右端三项分别批量计算：

- \(\lVert \text{obs}_i \rVert^{2}\)：对每个观测算一次平方和，共 `M` 个，存入 `obs_sqr`。
- \(\lVert \text{code}_j \rVert^{2}\)：对每个码字算一次平方和，共 `k` 个，存入 `codes_sqr`。
- \(\text{obs}_i \cdot \text{code}_j\)：所有 `M×k` 个内积，可以用**一次矩阵乘** `obs @ code_book.T` 算完——这正是 `cal_M` 用 BLAS `gemm` 做的事。

于是距离矩阵的每个元素就是 `dist_sqr[i,j] = M[i,j] + obs_sqr[i] + codes_sqr[j]`（其中 `cal_M` 把 `alpha` 设为 `-2.0`，所以 `M[i,j] = -2*(obs_i·code_j)`，正好凑出平方距离）。

> **何时不用 BLAS？** 当特征数 `nfeat < 5` 时，调用 BLAS 的固定开销超过了它带来的加速，于是 `_vq` 回退到朴素三重循环 `_vq_small_nf`。这是「小规模避免函数调用开销」的常见工程取舍。

#### 4.4.2 核心流程

```text
_vq(obs*, code_book*, ncodes, nfeat, nobs, codes*, low_dist*):   # _vq.pyx
  if nfeat < 5:
      _vq_small_nf(...)              # 朴素三重循环，直接算 diff² 求和
      return

  # BLAS 路径:
  obs_sqr[i]   = vec_sqr(obs[i])      # 每个 obs 的平方和   (i=0..nobs-1)
  codes_sqr[j] = vec_sqr(code[j])     # 每个 code 的平方和  (j=0..ncodes-1)
  cal_M(...)                        # M = -2 * (obs @ code_book.T)，一次 BLAS gemm

  for i in 0..nobs-1:
      for j in 0..ncodes-1:
          dist_sqr = M[i,j] + obs_sqr[i] + codes_sqr[j]   # = ||obs_i - code_j||²
          if dist_sqr < low_dist[i]:
              codes[i]   = j
              low_dist[i] = dist_sqr
      # 开根号回到欧氏距离；防止浮点负数
      low_dist[i] = sqrt(low_dist[i]) if low_dist[i] > 0 else 0
```

`cal_M` 内部（[vq/_vq.pyx:39-53](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq.pyx#L39-L53)）：用 `alpha=-2.0, beta=0.0` 调 `sgemm`（float32）或 `dgemm`（float64），计算 `M = alpha * code_book^T * obs`。注意 BLAS 的 Fortran ABI 是列主序，所以参数里 `code_book` 和 `obs` 的 leading dimension 都是 `nfeat`，转置标志 `"T"`/`"N"` 的设置是为了让结果 `M[i,j]` 正好是第 `i` 个观测与第 `j` 个码字的内积。

朴素回退 `_vq_small_nf`（[vq/_vq.pyx:135-174](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq.pyx#L135-L174)）：标准三重循环，`diff = code[k] - obs[k]; dist_sqr += diff*diff`，最后 `sqrt`。

#### 4.4.3 源码精读

平方和辅助函数 `vec_sqr`：[vq/_vq.pyx:31-36](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq.pyx#L31-L36) — 累加 `p[i]*p[i]`，返回向量自身的平方和（即范数平方）。

BLAS 距离内核 `cal_M`：[vq/_vq.pyx:39-53](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq.pyx#L39-L53)

```python
cdef inline void cal_M(blas_int nobs, blas_int ncodes, blas_int nfeat,
                       vq_type *obs, vq_type *code_book, vq_type *M) noexcept:
    """Calculate M = obs * code_book.T"""
    cdef vq_type alpha = -2.0, beta = 0.0
    if vq_type is float32_t:
        sgemm("T", "N", &ncodes, &nobs, &nfeat,
               &alpha, code_book, &nfeat, obs, &nfeat, &beta, M, &ncodes)
    else:
        dgemm("T", "N", &ncodes, &nobs, &nfeat,
              &alpha, code_book, &nfeat, obs, &nfeat, &beta, M, &ncodes)
```

注意 `alpha = -2.0`：它让 `M[i,j] = -2 * (obs_i · code_j)`，配合后面加上的 `obs_sqr[i] + codes_sqr[j]`，正好拼出 \(\lVert \text{obs}_i - \text{code}_j \rVert^{2}\)。

核心函数 `_vq`：[vq/_vq.pyx:56-132](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq.pyx#L56-L132)

特征数分流的判断：[vq/_vq.pyx:80-84](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq.pyx#L80-L84)

```python
# When the number of features is less than this number,
# switch back to the naive algorithm to avoid high overhead.
if nfeat < 5:
    _vq_small_nf(obs, code_book, ncodes, nfeat, nobs, codes, low_dist)
    return 0
```

平方和与距离拼装、取最小、开根号：[vq/_vq.pyx:114-130](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq.pyx#L114-L130)

```python
# M[i][j] is the inner product of the i-th obs and j-th code
# M = obs * codes.T
cal_M(nobs, ncodes, nfeat, obs, code_book, <vq_type *>M.data)

for i in range(nobs):
    for j in range(ncodes):
        dist_sqr = (M[i, j] + obs_sqr[i] + codes_sqr[j])
        if dist_sqr < low_dist[i]:
            codes[i] = j
            low_dist[i] = dist_sqr

    # dist_sqr may be negative due to float point errors
    if low_dist[i] > 0:
        low_dist[i] = sqrt(low_dist[i])
    else:
        low_dist[i] = 0
```

最后这段有两个细节值得圈出：

1. **`dist_sqr` 可能为负**：当某个观测恰好等于某个码字时，理论距离是 0，但浮点运算可能让 `M[i,j] + obs_sqr[i] + codes_sqr[j]` 算出微小的负数。直接 `sqrt` 会得到 `nan`，所以用 `if low_dist[i] > 0 ... else 0` 兜底。测试 `test_vq_large_features`（用 `×1000000` 的大数放大浮点误差）就是在保护这条逻辑。
2. **最终返回欧氏距离**：`sqrt` 之后 `low_dist` 才是欧氏距离（非平方），这正是它能与 `_py_vq`（`cdist` 也返回欧氏距离）对齐的原因。

#### 4.4.4 代码实践

**实践目标**：自己用 numpy 的「平方距离展开」恒等式复现 `_vq._vq` 的 BLAS 路径，并与官方 `vq` 的 `code`、`dist` 完全比对。

**操作步骤**：

```python
import numpy as np
from scipy.cluster.vq import vq

rng = np.random.default_rng(0)
obs       = rng.random((20, 8))          # 20 个观测，8 个特征（nfeat >= 5 走 BLAS 路径）
code_book = rng.random((3, 8))           # 3 个码字

# ---- 官方 vq（浮点 → Cython BLAS 路径）----
code_off, dist_off = vq(obs, code_book)

# ---- 手动复现：用恒等式 ||o-c||² = ||o||² - 2(o·c) + ||c||² ----
obs_sqr   = np.sum(obs**2, axis=1)            # 形状 (20,)
code_sqr  = np.sum(code_book**2, axis=1)      # 形状 (3,)
cross     = obs @ code_book.T                 # 形状 (20,3)，即 obs_i·code_j
dist_sqr  = cross * (-2.0) + obs_sqr[:, None] + code_sqr[None, :]  # (20,3) 平方距离
dist_sqr  = np.maximum(dist_sqr, 0.0)         # 防浮点负数，对齐 Cython 的兜底
my_code   = dist_sqr.argmin(axis=1)
my_dist   = np.sqrt(dist_sqr[np.arange(len(obs)), my_code])

print("code 一致:", np.array_equal(code_off, my_code))   # 期望 True
print("dist 一致:", np.allclose(dist_off, my_dist))      # 期望 True
```

**需要观察的现象**：

- `code` 完全一致（最近码字索引相同）。
- `dist` 在浮点容差内一致（`allclose`）。

**预期结果**：两个布尔值均为 `True`。这说明你手写的「展开平方距离」版本与官方 Cython 内核在数学上等价——官方只是把 `obs @ code_book.T` 换成了 BLAS `gemm`、把循环换成了 C 而已。

**延伸观察**：把 `obs` 的特征数改成 `nfeat=3`（`obs = rng.random((20,3))`，`code_book = rng.random((3,3))`），官方会改走 `_vq_small_nf` 朴素路径，但你的复现结果仍应与之一致（因为朴素路径和 BLAS 路径数学等价，只是实现不同）。

#### 4.4.5 小练习与答案

**练习 1**：`cal_M` 里 `alpha=-2.0`。如果误写成 `alpha=-1.0`，`vq` 的输出会怎样？

**参考答案**：此时 `M[i,j] = -(obs_i·code_j)`，于是 `dist_sqr = -(obs_i·code_j) + ||obs_i||² + ||code_j||²`，不再等于真正的平方距离。虽然 `argmin`（选最近码字）在「最近邻关系」上通常仍可能正确（因为 \(-2\) 与 \(-1\) 只是缩放内积项，但加上两个平方和项后排序会变），但 `dist` 的**数值**会错，且与 `_py_vq` 对不上。这正说明 `-2.0` 是为了严格凑出 \(\lVert a-b\rVert^2\) 的展开式。

**练习 2**：为什么 `nfeat < 5` 时反而放弃 BLAS、改用朴素三重循环？

**参考答案**：BLAS `gemm` 调用有固定的函数调用与参数检查开销。当 `nfeat` 很小，矩阵乘本身计算量很小，这个固定开销占比过大，朴素循环反而更快。源码注释明确写道 "switch back to the naive algorithm to avoid high overhead"。这是一种「小规模用直白代码、大规模用批量化库」的常见优化策略。

**练习 3**：核心循环里为什么有 `if low_dist[i] > 0: sqrt(...) else: 0`？去掉这个判断直接 `sqrt` 会怎样？

**参考答案**：浮点误差可能让本应为 0 的平方距离算成微小负数（如 `-1e-16`），`sqrt(负数)` 会得到 `nan`，污染结果。兜底成 0 保证数值健壮。测试 `test_vq_large_features` 故意把数据放大到 `1e6` 量级来加剧这种浮点误差，正是为了守住这条逻辑。

---

## 5. 综合实践

把本讲四块知识串起来：写一个「双路径对比探针」，用**同一组数据**分别触发 Cython BLAS 路径、Cython 朴素路径、纯 Python 路径，验证三者结果一致，并观察 dtype 差异。

```python
import numpy as np
from scipy.cluster.vq import vq
from scipy.cluster.vq._vq_impl import _py_vq, _vq

def probe(name, obs, code_book):
    code, dist = vq(obs, code_book)
    print(f"[{name}] code={code.tolist()} dtype={code.dtype} "
          f"dist={np.round(dist,4).tolist()}")
    return code, dist

rng = np.random.default_rng(42)

# 路径 A：float64 + nfeat=8 → Cython BLAS 路径
obsA, cbA = rng.random((10, 8)), rng.random((3, 8))
probe("A Cython-BLAS (float64,nfeat=8)", obsA, cbA)

# 路径 B：float64 + nfeat=3 → Cython 朴素路径 (_vq_small_nf)
obsB, cbB = rng.random((10, 3)), rng.random((3, 3))
probe("B Cython-naive (float64,nfeat=3)", obsB, cbB)

# 路径 C：int64 → 纯 Python _py_vq 路径
obsC = obsA.astype(np.int64) * 10
cbC  = cbA.astype(np.int64) * 10
probe("C py_vq (int64)", obsC, cbC)

# 一致性核验：对同一浮点数据，Cython 与 _py_vq 必须对齐
code_fast, dist_fast = vq(obsA, cbA)
code_slow, dist_slow = _py_vq(obsA, cbA, check_finite=False)
print("Cython vs _py_vq code 一致:", np.array_equal(code_fast, code_slow))
print("Cython vs _py_vq dist 一致:", np.allclose(dist_fast, dist_slow))
```

**完成标准**：

1. 能解释路径 A/B/C 分别走了 `vq()` 的哪个分支、`_vq._vq` 的哪个子路径。
2. 最后两行一致性核验均为 `True`——这同时验证了「平方距离展开 + BLAS」「朴素三重循环」「cdist + argmin」三种实现数学等价。
3. 能指出路径 C 的 `code.dtype` 是 `int64` 而路径 A/B 是 `int32`，并解释原因（`_py_vq` 走 numpy `argmin` 返回 `intp`；Cython 后端 `outcodes` 固定 `int32`）。

---

## 6. 本讲小结

- `vq(obs, code_book)` 返回 `(code, dist)`：`code[i]` 是离 `obs[i]` 最近的码字索引，`dist[i]` 是该欧氏距离。
- Python 层 `vq()` 的分发判据是「公共 dtype 是否实数浮点」：是 → Cython 快路径；否（如整数）→ 纯 Python `_py_vq` 慢路径。
- `_py_vq` 是 `cdist + argmin + min` 的直白封装，返回欧氏距离，作为兼容兜底与正确性参照（比 C 版慢约 20 倍）；公开别名 `py_vq` 已弃用。
- Cython 包装层 `_vq.vq` 是严格前置层：连续化、强制 `float32`/`float64` 且 dtype 一致、初始化 `outdists=inf` 与 `outcodes=int32`，再按 dtype 调核心函数。
- 核心 `_vq._vq` 用恒等式 \(\lVert a-b\rVert^2=\lVert a\rVert^2-2(a\cdot b)+\lVert b\rVert^2\) 把距离计算拆成「平方和 + 一次 BLAS `gemm`」；`nfeat<5` 时回退朴素三重循环 `_vq_small_nf` 以避开 BLAS 调用开销。
- 浮点健壮性：`cal_M` 用 `alpha=-2.0` 凑齐展开式；最终 `sqrt` 前对微小负数兜底为 0，保证数值正确。

## 7. 下一步学习建议

- 下一篇 [u2-l3 kmeans 主流程](u2-l3-kmeans-iteration.md) 会把本讲的 `vq` 嵌进「分配-更新」迭代循环，并用到本讲顺带提到的 `_vq.update_cluster_means`（[vq/_vq.pyx:301-363](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq.pyx#L301-L363)）做簇心更新，建议先扫一眼那个函数。
- 想深入 BLAS 接口，可阅读 `scipy.linalg.cython_blas` 里 `dgemm`/`sgemm` 的签名约定（列主序、`op(A)`/`op(B)` 转置标志），对照 `cal_M` 的参数理解「行主序 numpy 数组如何喂给列主序 BLAS」。
- 想巩固「宽容前门 vs 严格引擎」的双层模式，可对比 `scipy.cluster.hierarchy` 中 `linkage()` 与其 Cython 后端 `_hierarchy.*` 的类似分工（见 [u3 单元](u3-l2-linkage-dispatch.md)）。
