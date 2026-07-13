# 拼接与切分：stack/split 家族

## 1. 本讲目标

学完本讲，你将能够：

- 说清 `column_stack` 与 `dstack` 各自「先把数组补成几维、再沿哪根轴拼接」。
- 解释 `split` 与 `array_split` 的唯一区别：标量整数情形下的等分校验。
- 用 `array_split` 标量分支的 `divmod` 公式，推算任意轴长、段数下每段的长度分布。
- 看懂 `hsplit`/`vsplit`/`dsplit` 如何复用同一个 `_hvdsplit_dispatcher`，再借 `split` 的 `axis` 参数完成轴向分发；特别是 `hsplit` 对 1D 数组的「降轴」特判。
- 在真实源码里定位这 8 个函数的入口与关键行。

## 2. 前置知识

承接 u1-l2，numpy.lib 的公开函数几乎都长成「dispatcher + impl」的样子：被 `@array_function_dispatch(_xxx_dispatcher)` 装饰，dispatcher 只负责把参与运算的数组参数收集成一个 tuple（供 NEP-18 的 `__array_ufunc__` 协议派发），真正的逻辑写在被装饰的函数体里。本讲的 8 个函数全部位于 `_shape_base_impl.py`，无一例外地遵循这一写法。

动手前先建立两个直觉：

- **拼接（stack/concatenate）= 先对齐维度，再沿某根轴相加。** 对齐维度通常靠 reshape 或 `atleast_nd` 把低维数组「垫高」。
- **切分（split）= 沿某根轴切片。** 切出来的子数组是原数组的视图（view），不复制数据。

关于轴向：第 0 轴是「行」，第 1 轴是「列」，第 2 轴是「深度」。`vsplit`/`hsplit`/`dsplit` 名字里的 v/h/d 就分别对应 axis 0/1/2。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [_shape_base_impl.py](_shape_base_impl.py) | 本讲全部 8 个函数的实现与 dispatcher |
| numpy/_core/shape_base.py | 提供 `_arrays_for_stack_dispatcher`，被 `column_stack`/`dstack` 的 dispatcher 复用 |

所有永久链接基于当前 HEAD `b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b`。

## 4. 核心概念与源码讲解

### 4.1 column_stack：把 1D 数组拼成列

#### 4.1.1 概念说明

`column_stack` 接收一组 1D 或 2D 数组，把它们拼成一个 2D 数组。记忆点是：**1D 数组会被当成「一列」来拼**，2D 数组则像 `hstack` 那样原样横向拼接。

```
a = [1,2,3]   (shape (3,))
b = [4,5,6]   (shape (3,))
column_stack((a,b)) = [[1,4],
                       [2,5],
                       [3,6]]     shape (3,2)
```

`a` 成为第 1 列、`b` 成为第 2 列。

#### 4.1.2 核心流程

```
对 tup 中每个数组 v：
    若 v.ndim < 2（0D 或 1D）：
        把 v 垫成 2D 再转置 → 变成「一列」(shape (N,1))
    否则（已是 2D）：
        原样保留
最后沿 axis=1（列方向）concatenate
```

关键一步是「垫成 2D 再转置」：1D 数组 `[1,2,3]` 用 `ndmin=2` 得到 `[[1,2,3]]`（shape (1,3)），再 `.T` 转置成 shape (3,1) 的列向量。

#### 4.1.3 源码精读

dispatcher 把传入序列交给 `_arrays_for_stack_dispatcher` 校验（要求是 list/tuple 这类可索引序列，防止误传单个 ndarray）：

[_shape_base_impl.py:L603-L604](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L603-L604) —— `column_stack` 的 dispatcher，委托给共享的 `_arrays_for_stack_dispatcher`。

实现体的核心逻辑：

[_shape_base_impl.py:L642-L648](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L642-L648) —— 逐个元素判断维度，`ndim<2` 时用 `ndmin=2` 垫维再 `.T` 转成列，最后 `concatenate(arrays, 1)` 沿列方向拼。

注意 `array(arr, copy=None, subok=True, ndmin=2).T` 一行做了三件事：`ndmin=2` 垫到 2 维、`subok=True` 保留子类、`.T` 转成列。已是 2D 的数组直接 append，行为与 `hstack` 完全一致。

`_arrays_for_stack_dispatcher` 的定义在 `_core` 里（同 commit）：

[numpy/_core/shape_base.py:L206-L211](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/shape_base.py#L206-L211) —— 校验 `tup` 必须是可索引序列，否则抛 `TypeError`。

#### 4.1.4 代码实践

1. 实践目标：观察 1D 数组如何被拼成列、2D 数组如何被原样横向拼接。
2. 操作步骤：
   ```python
   import numpy as np
   a = np.array([1, 2, 3])
   b = np.array([4, 5, 6])
   print(np.column_stack((a, b)))        # [[1 4],[2 5],[3 6]]，shape (3,2)

   C = np.array([[10, 20], [30, 40]])    # 2D shape (2,2)
   D = np.array([[50], [60]])            # 2D 列 shape (2,1)
   print(np.column_stack((C, D)).shape)  # (2,3) —— 2D 原样横向拼
   ```
3. 观察现象：第一次输出 3 行 2 列，两个 1D 各成一列；第二次输出 (2,3)，2D 数组未转置直接拼接。
4. 预期结果：与各行注释一致（此例与 `_shape_base_impl.py` 中 `column_stack` 的 docstring 示例同源）。待本地验证。

#### 4.1.5 小练习与答案

**练习 1：** `np.column_stack(([1,2],[3,4],[5,6]))` 的 shape 是多少？
**答案：** (2,3)。三个长度为 2 的 1D 数组各成一列，拼成 2 行 3 列。

**练习 2：** 为什么 `column_stack` 对 2D 数组的行为和 `hstack` 一样？
**答案：** 因为 2D 数组 `ndim < 2` 为假，跳过转置直接 append，最后沿 axis=1 concatenate，与 `hstack` 完全一致。

### 4.2 dstack：沿第三轴（深度）拼接

#### 4.2.1 概念说明

`dstack` 沿第 3 根轴（axis=2，即「深度」）拼接，结果至少是 3 维。记忆点是「**先把所有数组补成至少 3 维，再沿 axis=2 拼**」。它是 `dsplit` 的逆运算，名字 d 取自 depth。

维度补齐规则（由 `atleast_3d` 决定）：

- 1D `(N,)` → `(1, N, 1)`
- 2D `(M, N)` → `(M, N, 1)`
- 3D 及以上 → 不变

#### 4.2.2 核心流程

```
arrs = atleast_3d(*tup)          # 每个数组补成 ≥3 维
return concatenate(arrs, axis=2) # 沿深度轴拼接
```

对比 `column_stack`：`column_stack` 把 1D 补到 2 维（成列），`dstack` 把 1D/2D 都补到 3 维（成「深度片」）。这就是「`dstack` 比 `column_stack` 多一维」的由来。

#### 4.2.3 源码精读

[_shape_base_impl.py:L651-L652](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L651-L652) —— `dstack` 的 dispatcher，同样复用 `_arrays_for_stack_dispatcher`。

[_shape_base_impl.py:L708-L712](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L708-L712) —— `dstack` 实现：`atleast_3d` 补维 → 处理单数组解包 → `concatenate(arrs, 2)`。

注意 `if not isinstance(arrs, tuple)` 这一行：`atleast_3d(*tup)` 在只传一个数组时会返回单个 ndarray 而非 tuple，因此要手动包成 `(arrs,)`，确保 `concatenate` 的入参形态一致。

#### 4.2.4 代码实践

1. 实践目标：验证 `dstack` 把 1D 数组补成 (1,N,1) 后沿深度拼接。
2. 操作步骤：
   ```python
   import numpy as np
   a = np.array((1, 2, 3))   # (3,)
   b = np.array((4, 5, 6))   # (3,)
   r = np.dstack((a, b))
   print(r.shape, r.tolist())
   ```
3. 观察现象：`a`→(1,3,1)、`b`→(1,3,1)，沿 axis=2 拼成 (1,3,2)。
4. 预期结果：shape (1,3,2)，内容 `[[[1, 4], [2, 5], [3, 6]]]`（与 `dstack` docstring 示例同源）。待本地验证。

#### 4.2.5 小练习与答案

**练习 1：** 两个 shape (2,3) 的 2D 数组做 `dstack`，结果 shape？
**答案：** (2,3,2)。每个 (2,3) 补成 (2,3,1)，沿 axis=2 拼成 (2,3,2)。

**练习 2：** 为什么说 `dstack` 是 `dsplit` 的逆？
**答案：** `dsplit` 沿 axis=2 切，`dstack` 沿 axis=2 拼，且 `dstack` 的补维规则恰好还原 `dsplit` 之前的形状，两者在维度操作上对称。

### 4.3 array_split：切分的真正内核（支持不等分）

#### 4.3.1 概念说明

`array_split` 是整个 split 家族里真正干活的那一个。它接收 `indices_or_sections`，可以是：

- 一个整数 N：把该轴尽量均分成 N 段，**允许不等分**——多出来的元素从前往后每段多分一个。
- 一个序列（list/1D 数组）：把序列里的值当作切分点（下标），在这些位置切断。

`split`、`hsplit`、`vsplit`、`dsplit` 最终都会调用它。

#### 4.3.2 核心流程

```
Ntotal = ary.shape[axis]                     # 该轴总长度
若 indices_or_sections 有 len()（序列）：
    Nsections = len(seq) + 1
    div_points = [0] + list(seq) + [Ntotal]  # 显式切分点
否则（标量整数 Nsections）：
    Neach, extras = divmod(Ntotal, Nsections)
    前 extras 段长度 = Neach + 1，其余段长度 = Neach
    div_points = cumsum([0, 长度1, 长度2, ...])
把 axis 交换到第 0 轴 → 逐段切片 sary[st:end] → 交换回去
返回子数组列表（均为视图）
```

标量分支的长度分布是关键。设轴长为 \(L\)、段数为 \(n\)，令 \(q, r = \mathrm{divmod}(L, n)\)：

\[
\mathrm{size}_i = \begin{cases} q+1 & i < r \\ q & i \geq r \end{cases}
\]

即余数 \(r\) 个「较大的段」排在最前面。例如 \(L=9,\ n=4\)：\(q=2,\ r=1\)，段长依次 \([3,2,2,2]\)，切分点 `cumsum([0,3,2,2,2]) = [0,3,5,7,9]`。

#### 4.3.3 源码精读

[_shape_base_impl.py:L715-L716](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L715-L716) —— `array_split` 的 dispatcher，返回 `(ary, indices_or_sections)`。

[_shape_base_impl.py:L747-L764](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L747-L764) —— 计算 `div_points` 的两分支：序列分支直接拼 `[0] + list(seq) + [Ntotal]`；标量分支用 `divmod` 算出「前 extras 段多 1」的长度表，再 `cumsum`。

注意标量分支里 `section_sizes` 以 `[0]` 开头，因此 `cumsum()` 后第一个边界正好是 0，与序列分支的形式统一，后续切片循环可共用同一套 `div_points`。

[_shape_base_impl.py:L766-L773](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L766-L773) —— 用 `swapaxes` 把目标轴搬到第 0 轴，切片后搬回；返回值是子数组列表，每个都是原数组的视图。

#### 4.3.4 代码实践

1. 实践目标：验证不等分时「余数段排前面」的规则，并确认返回的是视图。
2. 操作步骤：
   ```python
   import numpy as np
   x = np.arange(9)
   parts = np.array_split(x, 4)            # 9/4: q=2, r=1
   print([p.shape[0] for p in parts])      # [3, 2, 2, 2]
   parts[0][0] = 999                        # 改第一个子数组的首元素
   print(x[0])                             # 999 → 证明 parts[0] 是 x 的视图
   ```
3. 观察现象：段长 `[3,2,2,2]`；改 `parts[0][0]` 后 `x[0]` 同步变化。
4. 预期结果：与注释一致（段长分布与 `array_split` docstring 示例 `np.array_split(x, 4)` 同源）。待本地验证。

#### 4.3.5 小练习与答案

**练习 1：** `np.array_split(np.arange(10), 3)` 各段长度？
**答案：** \(10 = 3\times 3 + 1\)，\(q=3,\ r=1\)，段长 `[4,3,3]`。

**练习 2：** `np.array_split(np.arange(8), [2,5])` 返回几段、各段下标范围？
**答案：** 3 段，`div_points = [0,2,5,8]`，分别是 `[0:2]`、`[2:5]`、`[5:8]`。

### 4.4 split：在 array_split 之上加一层「等分校验」

#### 4.4.1 概念说明

`split` 与 `array_split` 的唯一区别：当 `indices_or_sections` 是整数时，`split` 要求轴长必须能被该整数整除，否则抛 `ValueError`。传序列（显式切分点）时不做整除校验。

#### 4.4.2 核心流程

```
若 indices_or_sections 是标量整数：
    若 ary.shape[axis] % sections != 0：
        raise ValueError('array split does not result in an equal division')
否则（序列）：跳过校验
return array_split(ary, indices_or_sections, axis)
```

它用 `try: len(...) except TypeError` 来区分「序列」与「标量」——标量没有 `len`，会触发 TypeError 进入校验分支。

#### 4.4.3 源码精读

[_shape_base_impl.py:L776-L777](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L776-L777) —— `split` 的 dispatcher，签名与 `array_split` 相同。

[_shape_base_impl.py:L848-L856](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L848-L856) —— `split` 的全部逻辑：用 `len()` 探测类型，标量且不能整除时报错，最后把活儿全交给 `array_split`。

这就是「`split` 只是 `array_split` 的严格版」的全部证据——没有独立的切分实现，只多了一道校验。

#### 4.4.4 代码实践

1. 实践目标：对比 `split` 与 `array_split` 在「不能整除」时的行为差异。
2. 操作步骤：
   ```python
   import numpy as np
   x = np.arange(7)                  # 长度 7
   try:
       np.split(x, 3)               # 7 % 3 != 0 → 报错
   except ValueError as e:
       print("split 报错:", e)
   print(len(np.array_split(x, 3)))  # 不报错，返回 3 段 [3,2,2]
   ```
3. 观察现象：`split` 抛 `ValueError`；`array_split` 正常返回 3 段。
4. 预期结果：`split` 报错信息含 "equal division"；`array_split` 返回长度 3 的列表。待本地验证。

#### 4.4.5 小练习与答案

**练习 1：** `np.split(np.arange(9), [3,5,6,10])` 会报错吗？返回几段？
**答案：** 不报错（序列模式不校验整除）。`div_points = [0,3,5,6,9]`，10 超过 `Ntotal=9` 被截断，实际返回 5 段，最后一段为空（与 `split` docstring 示例同源）。

**练习 2：** 为什么 `split` 只在标量分支校验，序列分支不校验？
**答案：** 序列给出的是显式切分点，段长由用户自己决定，无所谓「等分」；只有「分成 N 段」的标量语义才承诺等分，故只在此分支校验。

### 4.5 hsplit / vsplit / dsplit 与 _hvdsplit_dispatcher：轴向便捷封装

#### 4.5.1 概念说明

`hsplit`/`vsplit`/`dsplit` 是 `split` 的「轴向快捷方式」，分别对应 axis=1/0/2（h=horizontal 列方向、v=vertical 行方向、d=depth 深度）。三者共享同一个 dispatcher `_hvdsplit_dispatcher`，名字里的 hvd 正是它们三个的合称。

一个重要特例：**`hsplit` 对 1D 数组沿 axis=0 切**（因为 1D 数组没有「列」）。另两个则对维度有硬性要求（`vsplit` 需 ≥2 维，`dsplit` 需 ≥3 维），不满足直接报错。

#### 4.5.2 核心流程

| 函数 | 要求维度 | 切分轴 | 本质 |
|---|---|---|---|
| `hsplit` | ≥1（0D 报错） | 1D 时 axis=0，否则 axis=1 | `split(ary, x, 1)` |
| `vsplit` | ≥2 | axis=0 | `split(ary, x, 0)` |
| `dsplit` | ≥3 | axis=2 | `split(ary, x, 2)` |

三者都只是「校验维度 + 选定 axis + 调 `split`」的薄封装，没有任何独立的切分逻辑。

#### 4.5.3 源码精读

[_shape_base_impl.py:L859-L860](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L859-L860) —— 三个函数共用的 `_hvdsplit_dispatcher`，仅返回 `(ary, indices_or_sections)`。

`hsplit` 的 1D 特判：

[_shape_base_impl.py:L926-L931](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L926-L931) —— `hsplit`：0D 报错；`ndim>1` 沿 axis=1；**1D 时退化到 axis=0**。这正是「`hsplit` 对 1D 数组特殊处理」的代码出处。

`vsplit` 与 `dsplit` 更简单，纯粹「校验 + 转发」：

[_shape_base_impl.py:L983-L985](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L983-L985) —— `vsplit`：要求 ≥2 维，沿 axis=0。

[_shape_base_impl.py:L1029-L1031](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L1029-L1031) —— `dsplit`：要求 ≥3 维，沿 axis=2。

一个细节：`hsplit` 在第 926 行用函数式 `_nx.ndim(ary)` 取维度做 0D 判断，第 928 行又改用属性式 `ary.ndim` 判断是否大于 1；`vsplit`/`dsplit` 全程使用 `_nx.ndim(ary)`。两种写法等价，只是风格不一。

#### 4.5.4 代码实践

1. 实践目标：体验 `hsplit` 对 1D 的特判，以及 `vsplit`/`dsplit` 的维度门槛。
2. 操作步骤：
   ```python
   import numpy as np
   a1 = np.arange(6)                      # 1D
   print([p.shape for p in np.hsplit(a1, 2)])   # [(3,), (3,)] → 退到 axis=0

   x = np.arange(16).reshape(4, 4)
   print([p.shape for p in np.vsplit(x, 2)])    # [(2,4), (2,4)]

   y = np.arange(16).reshape(2, 2, 4)
   print([p.shape for p in np.dsplit(y, 2)])    # [(2,2,2), (2,2,2)]

   # 维度不足应报错（取消注释可见 ValueError）：
   # np.dsplit(np.arange(4).reshape(2, 2), 2)   # 需要 ≥3 维
   ```
3. 观察现象：`hsplit` 对 1D 成功（沿 axis=0）；`dsplit` 对 2D 报错。
4. 预期结果：与各行注释一致。待本地验证。

#### 4.5.5 小练习与答案

**练习 1：** 为什么 `hsplit` 需要 1D 特判，而 `vsplit`/`dsplit` 不需要？
**答案：** `hsplit` 默认 axis=1，但 1D 数组没有 axis=1；为方便对 1D 序列也能「横向切」，特判退到 axis=0。`vsplit`/`dsplit` 的目标轴在各自最低维度要求下都存在（`vsplit` 要 2 维、axis=0 必有；`dsplit` 要 3 维、axis=2 必有），无需特判。

**练习 2：** 三个函数共用一个 dispatcher 有什么好处？
**答案：** 它们的公开参数完全相同 `(ary, indices_or_sections)`，共用 `_hvdsplit_dispatcher` 既避免重复代码，也保证 NEP-18 `__array_function__` 派发时三者行为一致。

## 5. 综合实践

任务：取一个 3D 数组，分别用 `split` 与 `array_split` 按不同切分点切分，比较结果长度与各段形状，并把本讲的 `dstack`（拼）与 split 家族（切）串起来验证互逆。

```python
import numpy as np

# 1) 构造 3D 数组 (2, 3, 4)
a = np.arange(24).reshape(2, 3, 4)

# 2) 验证 dstack 是 dsplit 的逆
slices = np.dsplit(a, 2)                       # 沿 axis=2 切成 2 个 (2,3,2)
a_back = np.dstack(slices)                     # 再拼回来
print("dstack(dsplit(a)) 还原:", np.array_equal(a, a_back))   # True

# 3) split vs array_split：标量整数，能否整除
print("split(a, 2, axis=0) 段数:", len(np.split(a, 2, axis=0)))          # 2
print("split(a, 3, axis=1) 段数:", len(np.split(a, 3, axis=1)))          # 3（长度3，可整除）
print("array_split(a, 2, axis=1) 各段 axis=1 长度:",
      [p.shape[1] for p in np.array_split(a, 2, axis=1)])                # [2, 1]

# 4) 用切分点序列（显式下标），split 与 array_split 行为一致
pts = [1]
r_split = np.split(a, pts, axis=1)
r_asplit = np.array_split(a, pts, axis=1)
print("序列切分点：段数相等？", len(r_split) == len(r_asplit))            # True
print("各段 shape:", [p.shape for p in r_split])                         # [(2,1,4), (2,2,4)]
```

观察要点：

- `dstack(dsplit(a))` 无损还原 `a`，说明拼与切的维度操作对称。
- `split` 在能整除时正常返回，`array_split` 永远不报错且按「余数段排前」分配。
- 传切分点序列时 `split` 与 `array_split` 行为完全相同（`split` 不做整除校验）。

预期结果见各行注释；若本地 numpy 版本在默认参数等细节上有差异，以实际输出为准。待本地验证。

## 6. 本讲小结

- `column_stack` 与 `dstack` 都靠「先补维、再 concatenate」实现：前者把 1D 补成 2D 列（axis=1 拼），后者把 1D/2D 补成 3D（axis=2 拼），故 `dstack` 比 `column_stack` 多一维。
- `array_split` 是切分家族的真正内核：标量分支用 `divmod` 把余数摊到前若干段，序列分支直接拼切分点，返回值都是视图。
- `split` 仅在标量整数分支多一道「能整除」校验，其余完全委托给 `array_split`。
- `hsplit`/`vsplit`/`dsplit` 共用 `_hvdsplit_dispatcher`，本质是 `split` 的 axis=1/0/2 快捷方式；`hsplit` 对 1D 数组特判退到 axis=0。
- 全部 8 个函数都遵循 `_xxx_dispatcher + impl` 双函数写法，dispatcher 只收集数组参数供 NEP-18 派发。
- 切分结果都是原数组的视图，修改子数组会影响原数组。

## 7. 下一步学习建议

- 本讲只覆盖了 `column_stack`/`dstack` 两个拼接函数；完整的 `stack`/`vstack`/`hstack`/`block` 实现在 `numpy/_core/shape_base.py`，建议对照阅读 `_arrays_for_stack_dispatcher` 与 `_vhstack_dispatcher`，理解「沿新轴 stack」与「沿已有轴 concatenate」的区别。
- 切分返回视图，这与 u3-l1 的 `expand_dims`（reshape 视图）、第 5 单元的 `as_strided`/`sliding_window_view` 同属「视图家族」。下一讲 u3-l3 会讲 `kron`/`tile`/`eye`/`diag`/`tri` 等矩阵构造与平铺函数，它们与 split 家族共同构成 `_shape_base_impl.py` 的另一半。
- 若想深入「视图不复制」的内存含义，可先读 u5-l1 的 `as_strided` 与 `byte_bounds`。
