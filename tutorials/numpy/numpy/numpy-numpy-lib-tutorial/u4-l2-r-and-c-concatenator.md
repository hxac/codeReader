# r_ 与 c_ 轴向拼接器

## 1. 本讲目标

学完本讲后，你应当能够：

- 理解 `np.r_` 与 `np.c_` 不是函数，而是「用方括号索引语法拼数组」的实例对象。
- 读懂 `AxisConcatenator.__getitem__` 这个迷你 DSL 引擎：它把切片、标量、数组、以及一串「字符串指令」翻译成 `concatenate` 调用。
- 区分 `r_`（沿第一轴拼接）与 `c_`（沿末轴拼接、并把 1D 输入升级为列向量）的默认行为差异。
- 掌握 `'0,2,0'` 这类三整数指令如何分别控制「拼接轴 / 最小维度 / 新轴摆放位置（trans1d）」。
- 理解 `result_type` 在这里的「弱标量提升」作用，以及 `'r'`/`'c'` 矩阵模式的开关。

## 2. 前置知识

本讲承接 [u4-l1 ix_ 与 nd_grid 网格构造](u4-l1-ix-and-ndgrid.md)。那里我们见过一个关键套路：**用 `__getitem__` 把方括号里的内容当参数**。`mgrid`/`ogrid` 都是「实例 + `__getitem__`」，`r_`/`c_` 与之同源，只是把「生成网格」换成了「拼接数组」。

你需要先具备几个基础概念：

- **切片对象 `slice`**：Python 里 `a[1:5:2]` 等价于 `a[slice(1, 5, 2)]`，方括号里的 `1:5:2` 会被构造成 `slice` 对象传入 `__getitem__`。多个逗号分隔的项会被打包成 `tuple` 传入。
- **`np.concatenate(arrays, axis)`**：沿指定轴把多个数组拼起来，是 numpy 最底层的拼接原语。
- **`np.arange` 与 `np.linspace`**：前者 `arange(start, stop, step)` 不含端点；后者 `linspace(start, stop, num)` 含端点、按点数生成。
- **`ndmin` 升维**：`np.array(x, ndmin=2)` 会把 1D 的 `x` 补成 2D，新轴默认补在**最前面**（shape 从 `(n,)` 变 `(1, n)`）。
- **弱类型（weak scalar）提升**：Python 标量（如 `0`、`10`）参与 `result_type` 时不会强行抬高数组 dtype，这是 NEP 50 的语义。本讲会看到 `r_` 专门为此做了处理。

一个总览直觉：`r_` 是「**r**ow-wise / 沿第一轴」的快捷拼接器，`c_` 是「**c**olumn-wise / 沿末轴」的快捷拼接器。它们真正的实现都藏在 [`AxisConcatenator`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L325-L342) 这一个基类里，`RClass`/`CClass` 只是给它喂了不同的默认参数。

## 3. 本讲源码地图

本讲只涉及一个源码文件，外加两处辅助引用：

| 文件 | 作用 |
| --- | --- |
| [`numpy/lib/_index_tricks_impl.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L325-L587) | 本讲主角。`AxisConcatenator` 基类（325–445 行）、`RClass`（452–553 行）、`CClass`（556–587 行）全在此文件。 |
| [`numpy/lib/tests/test_index_tricks.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_index_tricks.py#L324-L363) | `TestConcatenator` 与 `test_c_`，给出 `r_`/`c_` 的行为断言，是实践的依据。 |
| [`numpy/matrixlib/defmatrix.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1041-L1106) | `bmat`，当方括号里**整体**是一个字符串时（MATLAB 风格矩阵字面量）被调用。 |

与 u4-l1 一致：`r_`/`c_` 出现在 `_index_tricks_impl.py` 的 `__all__` 里，但**没有**对应的薄再导出模块 `index_tricks.py`（已用 `Glob` 确认不存在），而是由顶层 `numpy/__init__.py` 直接取名暴露为 `np.r_`/`np.c_`。

## 4. 核心概念与源码讲解

### 4.1 AxisConcatenator 基类：__getitem__ 指令解析引擎

#### 4.1.1 概念说明

`AxisConcatenator` 是一个**类**，它的实例通过 `obj[key]` 触发 `__getitem__`，把方括号里的内容翻译成一次 `concatenate`。注意：`r_` 和 `c_` 是这个类的**实例**，不是函数。这就是为什么 `np.r_[...]` 用的是方括号、不是圆括号——你在做「索引」，而不是「调用」。

类文档一句话点明了它的定位：

> Translates slice objects to concatenation along an axis.

它解决的问题是：**用最少的字符快速拼数组**。比起写

```python
np.concatenate([np.arange(1, 5), np.array([10])])
```

你只需写 `np.r_[1:5, 10]`。方括号里可以混用切片、标量、数组，还能在开头塞一串字符串「指令」来改拼接轴、改最小维度、改新轴摆放位置——这就是它作为一个迷你 DSL（领域专用语言）的威力。

#### 4.1.2 核心流程

`__getitem__` 的执行流程可以用下面这段伪代码概括：

```
__getitem__(key):
  # 路径 A：整段 key 是字符串 → MATLAB 风格矩阵字面量
  if key 是 str:
      return bmat(key, 调用帧的全局/局部字典)

  if key 不是 tuple: key = (key,)           # 单项也包成元组

  # 复制默认属性到局部变量（首参数指令可改写它们）
  trans1d, ndmin, matrix, axis = self.trans1d, self.ndmin, self.matrix, self.axis

  for k, item in enumerate(key):
      if item 是 slice:
          复数步长 → linspace(start, stop, num=|step|)
          否则     → arange(start, stop, step)
          若 ndmin>1: 升维，并按 trans1d 用 swapaxes 转轴
      elif item 是 str（必须 k==0）:           # 字符串指令
          'r'/'c'      → matrix=True, col=(item=='c')
          'a,b[,c]'    → axis, ndmin[, trans1d]
          纯整数        → axis
      elif item 是标量: 直接收集（保留弱类型）
      else（数组类）:
          array(item, ndmin=ndmin)
          若 trans1d!=-1 且原 ndim<ndmin: 用 transpose 重排轴

      收集 newobj 与它的 dtype（标量则收标量本身）

  if 收集到对象:
      final_dtype = result_type(*收集到的类型)   # 弱标量提升
      把所有 obj 统一转为 final_dtype、ndmin=ndmin

  res = concatenate(objs, axis=axis)

  if matrix:
      res = makemat(res)                      # 转成 np.matrix
      若拼接前是 1D 且 col: res = res.T        # 'c' 要列矩阵
  return res
```

两条字符串处理路径要分清：

- **路径 A**：整段 `key` 就是字符串（如 `np.r_['1 2 3; 4 5 6']`），走 `bmat`，按 MATLAB 风格解析（空格分元素、`;` 分行，还能引用作用域里的变量名）。
- **路径 B**：`key` 是元组、且**第一个元素**是字符串（如 `np.r_['0,2', [1,2,3], [4,5,6]]`），走指令解析，设置 `axis`/`ndmin`/`trans1d`/`matrix`。

#### 4.1.3 源码精读

先看类的骨架与默认属性。`__slots__` 锁定四个可调旋钮，`concatenate` 与 `makemat` 被设为 `staticmethod`，注释明说这是为了让 `ma.mr_`（掩码数组的同名拼接器）能覆写 `concatenate`——这是一个**扩展点**：

[`numpy/lib/_index_tricks_impl.py:325-342`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L325-L342) — 类定义、`__slots__`、两个 staticmethod 钩子、`__init__`。`__init__(axis=0, matrix=False, ndmin=1, trans1d=-1)` 给出了四个旋钮的默认值：默认沿轴 0 拼、不产出 matrix、最小 1 维、`trans1d=-1`（新轴补在前）。

接着是核心 `__getitem__`。**路径 A**（整段字符串 → bmat）只占开头几行，关键是 `sys._getframe().f_back` 取到**调用者**的栈帧，把它的全局/局部字典传给 `bmat`，这样字符串里出现的变量名才能被解析：

[`numpy/lib/_index_tricks_impl.py:343-357`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L343-L357) — 字符串走 bmat、单项包元组、复制四个属性到局部变量。注意「复制属性」这一步：因为首参数指令会改写 `axis`/`ndmin`/`trans1d`/`matrix`，但只对**本次**调用生效，不能污染实例本身，所以拷到局部变量再改。

**切片分支**：复数步长走 `linspace`（含端点、按点数），否则走 `arange`。这与 u4-l1 的 `nd_grid` 完全同款约定。升维后用 `swapaxes(-1, trans1d)` 把数据轴挪到 `trans1d` 位置：

[`numpy/lib/_index_tricks_impl.py:365-381`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L365-L381) — slice → arange/linspace，ndmin>1 时升维并 swapaxes。

**字符串指令分支**（路径 B）：必须 `k==0`（只能放第一个），否则报 `special directives must be the first entry`。三种形式：`'r'`/`'c'` 开矩阵模式；含逗号的 `'a,b,c'` 拆成 `axis, ndmin, trans1d`；纯整数当 `axis`：

[`numpy/lib/_index_tricks_impl.py:382-405`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L382-L405) — 字符串指令解析。`continue` 表示指令本身不产生数组元素，只改旋钮。

**数组类分支**里的 `trans1d` 转轴是全讲最精巧的一段。当原数组维度 `item_ndim` 小于 `ndmin` 时，先 `array(item, ndmin=ndmin)`（新轴补在前），再用一个手算的轴排列 `axes` 做 `transpose`，把新轴摆到 `trans1d` 指定的位置：

[`numpy/lib/_index_tricks_impl.py:409-419`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L409-L419) — 数组类升维 + trans1d 重排。

这段排列公式可以这样理解。设要插入 \(k_2 = \text{ndmin} - \text{item\_ndim}\) 个新轴，`trans1d`（记为 \(t\)，负值先归一化为 \(k_1 = t + k_2 + 1\)）表示**原始数据轴在新形状里的起始位置**。令 `defaxes = [0,1,…,ndmin-1]`，则：

\[
\text{axes} = \text{defaxes}[:k_1] \;+\; \text{defaxes}[k_2:] \;+\; \text{defaxes}[k_1:k_2]
\]

直观含义：**先放 \(k_1\) 个新轴，接着放原始数据轴，最后放剩下 \(k_2-k_1\) 个新轴**。以 1D 输入升到 2D（\(k_2=1\)）为例：

| trans1d \(t\) | 归一化 \(k_1\) | 数据轴起始 | 新轴(1)位置 | `(n,)` 升到 2D 的 shape | 形态 |
| --- | --- | --- | --- | --- | --- |
| \(-1\)（默认） | \(k_2=1\) | 末尾 | 前面 | \((1, n)\) | 行向量 |
| \(0\) | \(0\) | 开头 | 末尾 | \((n, 1)\) | 列向量 |

这正是 `c_`（`trans1d=0`）把 1D 变列向量、而 `r_`（`trans1d=-1`）保持行向量的根源。

**弱标量提升**：标量被单独收集（存标量值本身，而非 0-d 数组的 dtype），数组则存其 `dtype`，最后统一 `result_type`：

[`numpy/lib/_index_tricks_impl.py:427-435`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L427-L435) — `result_type` 统一 dtype 后再 `concatenate`。注释「Ensure that scalars won't up-cast unless warranted」点明：Python 整数 `0` 不会把浮点数组拉成整数、也不会反向强行抬升，除非有更强的数组 dtype「 warrant」它。

**矩阵模式收尾**：若 `matrix=True`，用 `makemat`（即 `np.matrix`）包装结果；若原本是 1D 且指令是 `'c'`，再 `.T` 转成列矩阵：

[`numpy/lib/_index_tricks_impl.py:437-442`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L437-L442) — matrix 包装与 `'c'` 的转置。

最后，文件里有一段注释解释了**为什么要分成 `RClass`/`CClass` 两个子类**，而不是直接 `r_ = AxisConcatenator(0)`：

[`numpy/lib/_index_tricks_impl.py:447-449`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L447-L449) — 原因是「否则 `help(r_)` 拿不到正确的文档串」。子类各自带 docstring，`help(np.r_)` 才能显示 `r_` 专属的用法说明。

#### 4.1.4 代码实践

**实践目标**：通过阅读字符串指令分支，**先预测**三个表达式的输出 shape，再运行验证，确认你对 `axis`/`ndmin`/`trans1d` 三个旋钮的理解。

**操作步骤**：

1. 阅读上面的 [`_index_tricks_impl.py:382-405`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L382-L405) 与 [`409-419`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L409-L419) 两段源码。
2. 在纸上推断下列三式的结果 shape（输入都是 1D 的 `[1,2,3]` 与 `[4,5,6]`）：
   - `np.r_['0,2', [1,2,3], [4,5,6]]`
   - `np.r_['0,2,0', [1,2,3], [4,5,6]]`
   - `np.r_['1,2,0', [1,2,3], [4,5,6]]`
3. 运行下面脚本核对：

```python
# 示例代码
import numpy as np
a = [1, 2, 3]
b = [4, 5, 6]
print(np.r_['0,2', a, b])      # 预测 shape (2, 3)
print(np.r_['0,2,0', a, b])    # 预测 shape (6, 1)
print(np.r_['1,2,0', a, b])    # 预测 shape (3, 2)
```

**需要观察的现象**：`'0,2'`（trans1d 取默认 -1）把每个 1D 升成 `(1,3)` 的行，沿轴 0 叠成 `(2,3)`；`'0,2,0'`（trans1d=0）把每个 1D 升成 `(3,1)` 的列，沿轴 0 叠成 `(6,1)`；`'1,2,0'` 同样升成列，但沿轴 1 叠成 `(3,2)`。

**预期结果**：

```
[[1 2 3]
 [4 5 6]]          # shape (2, 3)
[[1]
 [2]
 [3]
 [4]
 [5]
 [6]]              # shape (6, 1)
[[1 4]
 [2 5]
 [3 6]]            # shape (3, 2)
```

这与 `RClass` 文档里的示例完全一致（见 [`_index_tricks_impl.py:525-539`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L525-L539)）。

#### 4.1.5 小练习与答案

**练习 1**：写出 `np.r_['1,2', [1,2,3], [4,5,6]]` 的结果 shape 并解释。

**参考答案**：shape `(1, 6)`，结果为 `[[1,2,3,4,5,6]]`。指令 `'1,2'` 设 `axis=1`、`ndmin=2`，`trans1d` 保持默认 `-1`。于是每个 1D 输入升维成 `(1,3)`（新轴在前，行向量），沿轴 1 拼接得到 `(1,6)`。

**练习 2**：为什么 `np.r_['c', [1,2,3]]` 返回的是 `(3,1)` 的列矩阵，而不是 `(1,3)` 的行矩阵？

**参考答案**：`'c'` 设 `matrix=True` 且 `col=True`。`[1,2,3]` 先拼成一个 1D 数组 `[1,2,3]`（`oldndim==1`），然后 `makemat` 把它包成 `(1,3)` 的 `np.matrix`（行矩阵）；因为 `oldndim==1 and col` 成立，最后执行 `res = res.T`，转置成 `(3,1)` 的列矩阵。`'r'` 则不会触发这步转置，保留 `(1,3)` 行矩阵。参见 [`_index_tricks_impl.py:437-442`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L437-L442)。

### 4.2 RClass 与 r_：沿第一轴拼接

#### 4.2.1 概念说明

`RClass` 是 `AxisConcatenator` 的子类，它的实例就是 `np.r_`。它把基类的默认 `axis` 钉死为 `0`，其余三个旋钮保持默认（`ndmin=1`、`trans1d=-1`、`matrix=False`）。所以 `r_` 的「人格」就是：**沿第一轴拼接、不做额外升维**。

它有两种主要用法（见其文档 [`_index_tricks_impl.py:452-546`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L452-L546)）：

1. 方括号里是逗号分隔的多个数组/标量 → 沿第一轴 stack 起来。
2. 方括号里是切片或标量 → 展开成 1D 数组（`start:stop:step` 等价 `arange`，复数步长等价 `linspace`）。

#### 4.2.2 核心流程

`r_` 的执行流程就是基类 `__getitem__` 在 `axis=0, ndmin=1, trans1d=-1` 下的特例：

```
r_[1:5, 10]:
  key = (slice(1,5,None), 10)
  slice(1,5,None) → arange(1,5) = [1,2,3,4]
  10 是标量 → 收集（弱类型）
  result_type(int64数组, 10) → int64
  concatenate([[1,2,3,4], [10]], axis=0) → [1,2,3,4,10]
```

复数步长分支：

```
r_[0:36:100j]:
  step=100j 是复数 → linspace(0, 36, num=100)   # 含端点，100 个点
```

#### 4.2.3 源码精读

`RClass` 自身极薄，只重写了 `__init__`，把 `axis` 钉为 `0`：

[`numpy/lib/_index_tricks_impl.py:549-553`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L549-L553) — `RClass.__init__` 调用 `AxisConcatenator.__init__(self, 0)`，随后 `r_ = RClass()` 生成全局单例。

所有真正的工作都继承自 4.1 讲过的 `__getitem__`。测试用例给出了 `r_` 的标准行为断言，可以作为「规格说明书」来读：

[`numpy/lib/tests/test_index_tricks.py:325-363`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_index_tricks.py#L325-L363) — `TestConcatenator`。其中 `test_1d` 断言 `r_[1,2,3,4,5,6]` 等于 `np.array([1,2,3,4,5,6])`，且 `r_[b, 0, 0, b]` 能把标量 `0` 穿插进数组；`test_mixed_type` 断言 `r_[10.1, 1:10]` 的 dtype 为 `f8`（浮点标量把整数切片拉成浮点）；`test_complex_step` 断言 `r_[0:36:100j]` 形状为 `(100,)`。

#### 4.2.4 代码实践

**实践目标**：用 `np.r_[1:5, 10]` 实现「按行拼接」（沿第一轴把切片展开结果与一个标量拼起来），验证结果 shape；并体验复数步长。

**操作步骤**：

```python
# 示例代码
import numpy as np

# (1) 切片 + 标量：沿第一轴拼接
res = np.r_[1:5, 10]
print(res, res.shape)          # 期望 [1 2 3 4 10], (5,)

# (2) 对比等价写法
print(np.concatenate([np.arange(1, 5), [10]]))

# (3) 复数步长：start:stop:numj → linspace（含端点）
g = np.r_[0:36:100j]
print(g.shape, g[0], g[-1])    # 期望 (100,) 0.0 36.0
```

**需要观察的现象**：`1:5` 被展开成 `arange(1,5)`（不含端点 5）；标量 `10` 作为弱类型标量参与拼接，不改变整数 dtype；复数步长 `100j` 的整数部分 `100` 被当作**点数**，端点 36 被包含。

**预期结果**：

```
[1 2 3 4 10] (5,)
[1 2 3 4 10]
(100,) 0.0 36.0
```

`test_1d` 与 `test_complex_step`（[test_index_tricks.py:325-329, 339-346](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_index_tricks.py#L325-L346)）正是这条实践的行为依据。

#### 4.2.5 小练习与答案

**练习 1**：把 `np.r_[1:5, 10]` 改写成不使用 `r_` 的等价 numpy 代码。

**参考答案**：`np.concatenate([np.arange(1, 5), np.array([10])])`，结果同为 `array([1, 2, 3, 4, 10])`。`r_` 的价值在于把「arange 展开 + concatenate」压成了一个方括号表达式。

**练习 2**：`np.r_[0:1:5j]` 等价于哪条 linspace 调用？

**参考答案**：等价于 `np.linspace(0, 1, 5)`，结果 `[0.0, 0.25, 0.5, 0.75, 1.0]`。复数步长的整数部分是点数、且**包含端点**，这是它与普通 `arange`（不含端点）的关键区别，承接自 u4-l1 的 `nd_grid` 约定。

### 4.3 CClass 与 c_：沿末轴拼接的列向量快捷方式

#### 4.3.1 概念说明

`CClass` 同样是 `AxisConcatenator` 的子类，实例即 `np.c_`。它与 `r_` 的唯一区别在于 `__init__` 喂给基类的三个参数：`axis=-1`、`ndmin=2`、`trans1d=0`。

这三个参数合起来产生一个极具辨识度的行为：**把每个 1D 输入升级成列向量，再沿最后一轴拼起来**。所以 `np.c_[a, b]`（a、b 为 1D）会把 a、b 当成两列，拼成一个 `(n, 2)` 的二维数组——这就是「按列拼接」的来历。

`CClass` 的文档一句话点破它与 `r_` 的等价关系：

> This is short-hand for `np.r_['-1,2,0', index expression]`

也就是说，`c_` 不是新机制，而是 `r_` 加上一串固定指令的语法糖。

#### 4.3.2 核心流程

`c_[a, b]`（a、b 为 1D，长度 n）的执行流程：

```
c_[[1,2,3], [4,5,6]]:   # axis=-1, ndmin=2, trans1d=0
  对 [1,2,3]: array(ndmin=2) → (1,3); trans1d=0 → transpose 到 (3,1)  # 列
  对 [4,5,6]: 同上 → (3,1)
  result_type → int64
  concatenate(两个 (3,1), axis=-1) → (3,2)
  结果: [[1,4],[2,5],[3,6]]
```

注意 `axis=-1` 对二维数组就是「最后一轴」（列方向），所以两个 `(3,1)` 的列向量沿 axis=-1 拼成 `(3,2)`。

#### 4.3.3 源码精读

`CClass` 同样极薄，只重写 `__init__`：

[`numpy/lib/_index_tricks_impl.py:583-587`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L583-L587) — `CClass.__init__` 调用 `AxisConcatenator.__init__(self, -1, ndmin=2, trans1d=0)`，随后 `c_ = CClass()` 生成单例。

把这三个值代入 4.1 讲过的 `trans1d` 公式（\(k_2 = 2-1 = 1\)，\(t=0 \Rightarrow k_1=0\)），就能推出 1D 输入会被摆成 `(n,1)` 的列向量——这就是「按列拼接」的数学根源。文档示例与测试断言如下：

[`numpy/lib/tests/test_index_tricks.py:429-431`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_index_tricks.py#L429-L431) — `test_c_`：`c_[np.array([[1,2,3]]), 0, 0, np.array([[4,5,6]])]` 等于 `[[1,2,3,0,0,4,5,6]]`。这里输入已是 2D 行向量，`ndmin=2` 不再升维，标量 `0` 被穿插进末轴。

`CClass` 文档里的经典示例（[`:573-578`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L573-L578)）给出 1D 输入的情形：`np.c_[np.array([1,2,3]), np.array([4,5,6])]` 得到 `[[1,4],[2,5],[3,6]]`。

#### 4.3.4 代码实践

**实践目标**：用 `np.c_[a, b]` 实现「按列拼接」，验证结果 shape 为 `(n, 2)`；并用 `r_` 的指令证明 `c_` 只是它的语法糖。

**操作步骤**：

```python
# 示例代码
import numpy as np

a = np.array([1, 2, 3])
b = np.array([4, 5, 6])

# (1) 按列拼接
res = np.c_[a, b]
print(res, res.shape)                       # 期望 [[1,4],[2,5],[3,6]], (3, 2)

# (2) 用 r_ 的指令复刻 c_：axis=-1, ndmin=2, trans1d=0
res2 = np.r_['-1,2,0', a, b]
print(np.array_equal(res, res2))            # 期望 True

# (3) 对比：若改用 r_ 默认（axis=0, ndmin=1），则按第一轴拼成 1D
print(np.r_[a, b])                          # 期望 [1 2 3 4 5 6]
```

**需要观察的现象**：`c_` 把两个 1D 数组当成两列、产出 `(3,2)`；`r_['-1,2,0', a, b]` 与之逐字节相等；而 `r_[a, b]`（默认）则把它们首尾相接成 1D 的 `(6,)`。三者的差异完全来自 `axis`/`ndmin`/`trans1d` 三个旋钮的取值。

**预期结果**：

```
[[1 4]
 [2 5]
 [3 6]] (3, 2)
True
[1 2 3 4 5 6]
```

#### 4.3.5 小练习与答案

**练习 1**：`np.c_[[1,2,3], [4,5,6]]` 的 shape 是多少？为什么 1D 输入会变成列？

**参考答案**：shape `(3, 2)`。因为 `CClass` 设 `ndmin=2`、`trans1d=0`：每个 1D 输入先 `array(ndmin=2)` 升成 `(1,3)`，再按 `trans1d=0` 经 transpose 把新轴摆到末尾，得到 `(3,1)` 的列向量；最后沿 `axis=-1` 拼成 `(3,2)`。

**练习 2**：用 `r_` 的字符串指令写出与 `np.c_[a, b]`（a、b 为 1D）完全等价的表达式。

**参考答案**：`np.r_['-1,2,0', a, b]`。三个数依次是 `axis=-1`（末轴）、`ndmin=2`、`trans1d=0`，正是 `CClass.__init__` 传给基类的三参数。这正是 `CClass` 文档所说的「short-hand for `np.r_['-1,2,0', ...]`」。

## 5. 综合实践

把本讲三个要点串起来：**切片展开（含复数步长）+ c_ 按列拼接 + 指令系统改轴**。

```python
# 示例代码
import numpy as np

# (1) 用 r_ 的复数步长生成 5 个等距采样点 x ∈ [0, 1]
x = np.r_[0:1:5j]                  # 等价 linspace(0, 1, 5) → [0, 0.25, 0.5, 0.75, 1.0]
print("x =", x, x.shape)           # (5,)

# (2) 用 c_ 把 x 与 x**2 拼成 (5, 2) 的坐标表（两列）
table = np.c_[x, x**2]
print("table shape =", table.shape)         # (5, 2)
print(table)

# (3) 用 r_ 指令复刻上面的 c_，验证等价
table2 = np.r_['-1,2,0', x, x**2]
print("c_ == r_['-1,2,0', ...]:", np.array_equal(table, table2))   # True

# (4) 改用 '0,2' 指令：转成按行堆叠，对比 shape
row_stack = np.r_['0,2', x, x**2]    # axis=0, ndmin=2, trans1d=-1(默认)
print("row_stack shape =", row_stack.shape)   # (2, 5)
```

**预期结果**：

```
x = [0.   0.25 0.5  0.75 1.  ] (5,)
table shape = (5, 2)
[[0.     0.    ]
 [0.25   0.0625]
 [0.5    0.25  ]
 [0.75   0.5625]
 [1.     1.    ]]
c_ == r_['-1,2,0', ...]: True
row_stack shape = (2, 5)
```

**思考点**：第 (3) 步证明了 `c_` 只是 `r_['-1,2,0', ...]` 的语法糖；第 (4) 步把 `trans1d` 从 `0` 换回默认 `-1`、`axis` 从 `-1` 换成 `0`，同样的两个 1D 输入就从「列拼 `(5,2)`」变成「行堆 `(2,5)`」——可见三个旋钮如何完全决定输出形态。若本地 numpy 版本行为有差异，请以实际输出为准（待本地验证）。

## 6. 本讲小结

- `np.r_` 与 `np.c_` 不是函数，而是 `AxisConcatenator` 的**实例**，靠 `__getitem__` 把方括号语法变成 `concatenate` 调用，与 u4-l1 的 `mgrid`/`ogrid` 同属「索引即参数」一族。
- `__getitem__` 是一个迷你 DSL 引擎：切片→`arange`/`linspace`，标量→弱类型收集，数组→按 `ndmin`/`trans1d` 升维转轴，最后 `result_type` 统一 dtype 再 `concatenate`。
- 字符串有两条路径：**整段**字符串走 `bmat`（MATLAB 风格矩阵字面量）；**元组首元素**字符串走指令解析，三整数 `'axis,ndmin,trans1d'` 分别控制拼接轴、最小维度、新轴摆放位置。
- `trans1d` 的本质是「原始数据轴在新形状里的起始位置」，公式 \(\text{axes} = \text{defaxes}[:k_1] + \text{defaxes}[k_2:] + \text{defaxes}[k_1:k_2]\)：默认 `-1` 让新轴在前（行向量），`0` 让新轴在末（列向量）。
- `RClass` 把 `axis` 钉为 `0`，故 `r_` 沿第一轴拼、不升维；`CClass` 设 `axis=-1, ndmin=2, trans1d=0`，故 `c_` 把 1D 输入变列向量后沿末轴拼，等价于 `r_['-1,2,0', ...]`。
- `concatenate`/`makemat` 被设为 `staticmethod` 是有意为之的扩展点，允许 `ma.mr_` 等子类覆写。

## 7. 下一步学习建议

- 下一讲 [u4-l3 迭代器与对角线索引：ndindex/ndenumerate/Arrayterator/fill_diagonal](u4-l3-iterators-and-diagonal.md) 会继续在本文件里往下走，讲 `ndindex`/`ndenumerate`/`IndexExpression`（`s_`/`index_exp`）与 `fill_diagonal`/`diag_indices`。其中 `s_`/`index_exp` 也是「索引即参数」的实例，与本讲同源，可对照阅读。
- 想深入「新轴摆放」的转轴逻辑，可回头读 u5（步长与广播技巧）里 `as_strided`/`broadcast_to` 对 shape/strides 的直接操作，理解 numpy 维度操作的底层共性。
- 若对矩阵字面量字符串路径（`bmat`）感兴趣，可阅读 `numpy/matrixlib/defmatrix.py` 的 `bmat` 与 `_from_string`，注意 `np.matrix` 是遗留类型、新代码应优先用普通 `ndarray`。
- 建议把本讲的「三旋钮预测」练习再做一遍：任意给一个 `'a,b,c'` 指令和一组 1D 输入，先在纸上推出 shape，再跑 `np.r_` 验证，直到完全无误。
