# 归约、累积与方法分发

## 1. 本讲目标

学完本讲后，你应该能够：

- 画出 `arr.sum()` 与 `np.sum(arr)` 的完整调用链，并指出二者在哪一层汇合。
- 说出 `axis / dtype / out / keepdims / initial / where` 六个参数各自的物理含义与对结果形状的影响。
- 解释为什么 `sum / prod / max / min / any / all` 是「一个 ufunc 的 `.reduce`」，而 `mean / std / var` 是「在归约之上拼出来的复合运算」。
- 在 C 层定位 `PyUFunc_ReduceWrapper`，并指出 keepdims、identity、wheremask、可重排序性分别由哪段代码负责。

本讲是 u4-l3（ufunc 内部实现）的下游：归约（reduction）本质上就是「反复套用同一条 ufunc 内层循环」。承接 u4-l3 讲过的 `PyUFuncObject` 与 `identity` 字段，本讲把它们用到 `np.add.reduce` 这样的具体场景里。

---

## 2. 前置知识

- **归约（reduction）**：把一个数组沿某个（或多个）轴「压扁」成更小的数组，每个输出元素是若干输入元素的聚合。求和、求最大值、求均值都是归约。用一句话概括：输入形状 \((d_0, d_1, \dots, d_{n-1})\)，沿轴 \(k\) 归约后形状为 \((d_0, \dots, d_{k-1}, d_{k+1}, \dots, d_{n-1})\)——第 \(k\) 维被吃掉。
- **累积（accumulation）**：归约的「保留中间过程」版本。`cumsum` 不只给你最终的和，还给你每一步的部分和，输出与输入同形（`include_initial` 除外）。
- **uareduce = ufunc.reduce**：上一讲我们讲过 `np.add` 是一个 ufunc。任何 ufunc 都挂着一个 `.reduce` 方法，作用是「把这个二元 ufunc 沿轴反复折叠」。所以 `np.sum` 的内核不是别的，就是 `np.add.reduce`。
- **identity（幺元）**：归约需要一个「起始值」。加法的幺元是 0，乘法的幺元是 1，而 `np.maximum` 没有通用幺元（对空数组无法给出答案）。这个 0/1/无 的差别直接决定了 C 层走哪条初始化分支。
- **可重排序性（reorderable）**：浮点加法可以换序（精度会变，但语义允许），而 `np.subtract` 不能换序（\(a-b-c \neq a-c-b\)）。NumPy 用这个标记拒绝多轴不可重排序归约。

如果你对 ufunc 的 `identity` 字段、`reduce` 方法还不熟，建议先回到 u4-l3 复习。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| `numpy/_core/_methods.py` | ndarray 归约方法的 **Python 中间层** | 把 C 方法翻译成 `ufunc.reduce` 调用，并为 mean/std/var 拼装复合逻辑 |
| `numpy/_core/fromnumeric.py` | `np.sum / np.mean / np.max ...` 等 **顶层函数** | 用 `_wrapreduction` / `_wrapfunc` 决定走「对象自己的方法」还是「ufunc.reduce」 |
| `numpy/_core/src/multiarray/methods.c` | ndarray 的 **C 方法表** | `{"sum", array_sum}` 等登记项，转发到 `_methods._sum` |
| `numpy/_core/src/umath/ufunc_object.c` | ufunc 的 **`.reduce` 方法实现** | `ufunc_reduce` → `PyUFunc_Reduce` → `PyUFunc_ReduceWrapper` |
| `numpy/_core/src/umath/reduction.c` | 归约的 **C 核心引擎** | `PyUFunc_ReduceWrapper`：建 NpyIter、取幺元、跑内层循环 |

三句话概括整张地图：

1. `arr.sum()` 走 C 方法 → `_methods._sum` → `um.add.reduce`。
2. `np.sum(arr)` 走 `fromnumeric.sum` → `_wrapreduction` → `um.add.reduce`（ndarray 时直接跳到 ufunc，绕过 `_methods._sum`）。
3. 两条路最终都汇入 `um.add.reduce`，也就是 C 层的 `PyUFunc_Reduce` → `PyUFunc_ReduceWrapper`。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：方法层（`_methods.py`）、顶层函数与分发（`fromnumeric.py`）、C 层归约引擎（`reduction.c`）。

### 4.1 方法层分发：_methods.py

#### 4.1.1 概念说明

`_methods.py` 是一个「只为 ndarray 方法服务」的内部模块。它的存在回答了一个问题：**当 C 语言写的方法体（`array_sum`）需要写逻辑时，逻辑写在哪？** NumPy 的选择是：C 方法尽量薄，真正的算法落在 Python 的 `_methods.py` 里，便于阅读和维护。

这个模块有两个截然不同的职责：

1. **薄封装归约**：把 `sum / prod / max / min / any / all` 直接转成对应 ufunc 的 `.reduce` 调用。它们本身「没有算法」，只是参数透传。
2. **复合归约**：`mean / std / var` 不是单个 ufunc，而是在「先 sum、再除以计数」之上拼出来的，这里才有真正的 Python 逻辑。

模块开头用一个很关键的技巧：把 ufunc 的 `.reduce` 方法在导入时绑成**局部别名**，省掉每次调用的一次属性查找。

#### 4.1.2 核心流程

`arr.sum(axis=1)` 的内部流程：

```
arr.sum(axis=1)                      # Python 调用
  → C 方法 array_sum                  # methods.c
    → _methods._sum(arr, axis=1, ...) # 转发到 Python 中间层
      → umr_sum(arr, 1, dtype, out, keepdims, initial, where)
        # umr_sum 就是 um.add.reduce
        → np.add.reduce(arr, axis=1, ...)   # 进入 ufunc 层（下一模块讲）
```

`mean` 的流程多两步：

```
arr.mean(axis=1)
  → array_mean → _methods._mean
    1. rcount = _count_reduce_items(arr, axis)   # 数被归约元素个数
    2. ret = umr_sum(arr, axis, dtype, ...)       # 先求和（复用 sum 的内核）
    3. ret = um.true_divide(ret, rcount, out=ret) # 再除以个数
```

也就是说，均值的数学定义被原样翻译成了源码：

\[
\bar{x}_{\text{axis}} = \frac{1}{N_{\text{axis}}}\sum_{\text{axis}} x
\]

其中 \(N_{\text{axis}}\) 就是 `_count_reduce_items` 算出来的「沿被归约轴的元素个数之积」。

#### 4.1.3 源码精读

先看模块开头的别名绑定。这几行是「性能优化的微缩样本」：

[numpy/_core/_methods.py:L17-L24](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_methods.py#L17-L24) —— 把每个 ufunc 的 `.reduce` 方法在导入时绑定成局部名（`umr_sum = um.add.reduce`），后续调用 `umr_sum(...)` 等价于 `np.add.reduce(...)`，但少一次属性查找。注释里那句「save those O(100) nanoseconds!」直白说明了意图。

再看 `_sum`，它是「薄封装归约」的代表：

[numpy/_core/_methods.py:L47-L49](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_methods.py#L47-L49) —— `_sum` 一行实现：把参数按位置透传给 `umr_sum`。`_amax / _amin / _prod` 的结构完全一样，只是换成 `umr_maximum / umr_minimum / umr_prod`。

[numpy/_core/_methods.py:L39-L45](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_methods.py#L39-L45) —— `_amax` / `_amin`。注意它们的第三个位置参数是 `None`（对应 `dtype`），因为最大最小值不需要累加器 dtype。

接着看「复合归约」`_mean`，这是本模块最有内容的一段：

[numpy/_core/_methods.py:L115-L146](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_methods.py#L115-L146) —— `_mean` 的三段式：(1) `_count_reduce_items` 算个数；(2) 对整数/布尔输入默认提升到 `float64`（避免整除丢精度），对 `float16` 用 `float32` 中间态；(3) `umr_sum` 求和后 `um.true_divide` 除以计数。`rcount == 0` 时还会抛 `Mean of empty slice` 告警。

计数函数本身值得一看，它揭示了 `where=` 掩码的实现：

[numpy/_core/_methods.py:L73-L94](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_methods.py#L73-L94) —— `_count_reduce_items`：默认情况（`where is True`）只是把被归约轴的长度连乘；当 `where` 是布尔数组时，它把掩码广播到数组形状，再对 `True` 求和得到「真正参与计算的元素个数」。这就解释了为什么 `np.mean(a, where=mask)` 能在忽略部分元素的同时把分母也调对。

> 关键结论：`_methods.py` 中，`sum/prod/max/min/any/all` 是「一行透传」，`mean/std/var` 是「先 sum 再除」。`_var`（[L148-L215](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_methods.py#L148-L215)）进一步在 `_mean` 之上计算方差 \(\frac{1}{N}\sum (x-\bar{x})^2\)，`_std` 再对 `_var` 开平方。

#### 4.1.4 代码实践

**目标**：亲手验证「mean = sum / count」这条等式在源码里成立。

**操作步骤**：

1. 构造一个整数数组，分别调用 `np.sum` 与 `np.mean`。
2. 用 `_count_reduce_items` 的逻辑手算分母，再用 `sum / count` 复现 `mean`。

```python
# 示例代码（不是项目原有代码）
import numpy as np
from numpy._core import _methods

a = np.arange(12, dtype=np.int64).reshape(3, 4)
# 沿 axis=1 求均值
rcount = _methods._count_reduce_items(a, axis=1, keepdims=False)  # 应为 4
manual_mean = a.sum(axis=1, dtype=np.float64) / rcount
print(rcount)              # 4
print(manual_mean)         # [1.5, 5.5, 9.5]
print(a.mean(axis=1))      # [1.5, 5.5, 9.5]  —— 二者一致
```

3. 再读 [_methods.py:L132-L135](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_methods.py#L132-L135)，确认 `true_divide(ret, rcount, out=ret)` 把和原地除以计数。

**需要观察的现象**：整数数组的 `mean` 返回 `float64`（因为 `_mean` 把整数默认提升为 `'f8'`）；而 `sum` 对 `int64` 输入返回 `int64`。

**预期结果**：`manual_mean` 与 `a.mean(axis=1)` 完全相等，验证「mean 是 sum 与 count 的组合」。

> 说明：以上是供你本地运行的示例。如果你环境的 NumPy 是已安装发行版，`numpy._core._methods` 可能不在公开路径，可改为 `from numpy.core import _methods` 或直接观察 `a.sum/a.mean` 的返回值来佐证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_sum` 的签名里 `dtype` 可以是 `None`，而 `_amax` 的对应位置直接写成常量 `None`？

**参考答案**：求和需要决定累加器精度（小整数相加可能溢出，要升到平台 int 或更高），所以 `dtype` 是用户可控的参数；最大最小值只是「挑一个元素」，不存在累加，没有累加器 dtype 的概念，因此 `_amax` 直接把 `None` 写死在第三位（见 [_methods.py:L40-L41](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_methods.py#L40-L41)）。

**练习 2**：`np.std` 在源码里调用了几次 ufunc？分别是什么？

**参考答案**：`_std`（[L217-L229](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_methods.py#L217-L229)）调用 `_var`，后者内部用了 `add.reduce`（求和）、`subtract`、`square`（或 `multiply`+`conjugate`）、`true_divide`，最后 `_std` 再用 `um.sqrt`。即标准差是「sum、subtract、square、divide、sqrt」的一串 ufunc 组合，而非单个归约。

---

### 4.2 顶层归约函数与分发：fromnumeric.py

#### 4.2.1 概念说明

`fromnumeric.py` 提供 `np.sum / np.mean / np.max / np.cumsum ...` 这些**函数形式**的归约。函数和方法（`arr.sum()`）在数学上等价，但调度路径不同，而且函数形式必须处理一种方法形式不用操心的情况：**输入可能不是 ndarray**。

考虑三种输入：

1. 真正的 `np.ndarray`：直接用 `ufunc.reduce` 最快。
2. ndarray 子类（如 `np.ma.MaskedArray`）或第三方数组（如 pandas）：应该**优先调用它自己的 `.sum` 方法**，尊重其自定义语义（NEP-18 `__array_function__` 的精神）。
3. 任意 `array_like`（列表、元组）：先转成 ndarray 再归约。

`fromnumeric.py` 用两个小工具 `_wrapfunc` 与 `_wrapreduction` 统一处理这三类输入。

#### 4.2.2 核心流程

`np.sum(arr)` 的分发流程（ndarray 情形）：

```
np.sum(arr, axis=1)
  → fromnumeric.sum                           # 顶层函数
    → _wrapreduction(arr, np.add, 'sum', ...)
      → type(arr) is ndarray？ 是
        → np.add.reduce(arr, axis=1, ...)     # 直接进 ufunc，绕过 _methods._sum
```

注意这条路径**绕过了 `_methods._sum`**——因为 `np.add.reduce` 本身就是目的地，没必要再绕一圈。而 `arr.sum()`（方法形式）才会经过 `_methods._sum`。两条路殊途同归于 `np.add.reduce`。

`np.sum(obj)` 的分发流程（非 ndarray 情形）：

```
np.sum(obj, axis=1)                # obj 是 pandas.Series 等
  → _wrapreduction(obj, np.add, 'sum', ...)
    → type(obj) is not ndarray？ 是
      → getattr(obj, 'sum')(axis=1, ...)      # 调用对象自己的方法
```

对 `mean`，因为它是复合运算，无法「直通 ufunc」，所以两条路都经过 `_methods._mean`：

```
np.mean(arr) → mean() → _methods._mean(arr, ...)   # ndarray 也走这里
arr.mean()   → array_mean → _methods._mean
```

累积（`cumsum` / `cumulative_sum`）走的是 ufunc 的另一个方法 `.accumulate`：

```
np.cumulative_sum(x, axis=0)
  → _cumulative_func(x, um.add, axis, ...)
    → um.add.accumulate(x, axis=0, ...)       # ufunc.accumulate，不是 reduce
```

#### 4.2.3 源码精读

先看分发核心 `_wrapreduction`：

[numpy/_core/fromnumeric.py:L72-L95](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/fromnumeric.py#L72-L95) —— `_wrapreduction`。读这段代码注意三件事：

- 签名是 **positional-only**（参数表末尾的 `/`）。上方 [L67-L71](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/fromnumeric.py#L67-L71) 的注释解释：这是每个归约的热路径，避免构造临时 kwargs 字典。
- `keepdims/initial/where` 用 `_NoValue` 哨兵判断「用户是否传了」，只把传了的塞进 `passkwargs`。这样默认值不会污染下游（例如 `keepdims` 默认值不应被子类方法看到）。
- 调度分支：`type(obj) is not mu.ndarray` 时优先 `getattr(obj, method)`（尊重子类/第三方）；否则 `ufunc.reduce(obj, ...)`（ndarray 直通）。

再看顶层 `sum` 函数本体——它的实现只有一行：

[numpy/_core/fromnumeric.py:L2504-L2506](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/fromnumeric.py#L2504-L2506) —— `sum` 的 `return` 把一切交给 `_wrapreduction(a, np.add, 'sum', ...)`。函数体前面对生成器输入做了拒绝（[L2496-L2502](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/fromnumeric.py#L2496-L2502)），其余全是文档。

`max` 同样一行，但换成 `np.maximum`：

[numpy/_core/fromnumeric.py:L3201-L3202](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/fromnumeric.py#L3201-L3202) —— `max` → `_wrapreduction(a, np.maximum, 'max', ...)`。`amax` 是 `max` 的别名（[L3218-L3219](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/fromnumeric.py#L3218-L3219)）。

`mean` 的分发略有不同，因为它要保证 ndarray 也走 `_methods._mean`：

[numpy/_core/fromnumeric.py:L3894-L3903](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/fromnumeric.py#L3894-L3903) —— `mean`：非 ndarray 时优先 `a.mean(...)`；ndarray 时落到 `_methods._mean(...)`。这里**没有**调用 `ufunc.reduce`，因为 mean 不是单个 ufunc。

最后看累积函数 `_cumulative_func`，它揭示 `cumulative_sum` 与老 `cumsum` 的关系：

[numpy/_core/fromnumeric.py:L2716-L2742](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/fromnumeric.py#L2716-L2742) —— `_cumulative_func` 的内核是 `func.accumulate(x, axis=..., dtype=..., out=...)`（`func` 为 `um.add` 或 `um.multiply`）。`include_initial=True` 时在结果前补一个幺元（用 `func.identity`），这让 `cumulative_sum` 的形状比 `cumsum` 多一截。所以 Array API 风格的 `cumulative_sum` 是老 `cumsum` 的「带初始值增强版」，二者底层都是 `np.add.accumulate`。

> 旁证 `__array_function__` 的接线：每个顶层函数都套了 `@array_function_dispatch(_xxx_dispatcher)`（如 [L2383](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/fromnumeric.py#L2383)）。dispatcher 函数（如 [L2378-L2380](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/fromnumeric.py#L2378-L2380)）只负责「返回哪些参数是数组类对象」，供 NEP-18 调度器检查它们是否重载了 `__array_function__`。这套机制在 u7-l2 详讲，这里只要知道它存在。

#### 4.2.4 代码实践（本讲主任务）

**目标**：用一个 \(4 \times 5 \times 3\) 的数组，系统验证 `axis` 与 `keepdims` 如何决定结果形状。

**操作步骤**：

```python
# 示例代码
import numpy as np

a = np.arange(4*5*3, dtype=np.float64).reshape(4, 5, 3)   # shape (4, 5, 3)

# (1) 单轴归约：被指定的那一维消失
print(a.sum(axis=0).shape)             # (5, 3)
print(a.sum(axis=1).shape)             # (4, 3)
print(a.sum(axis=2).shape)             # (4, 5)

# (2) axis=None：所有维一起塌缩成标量
print(a.sum(axis=None).shape)          # ()

# (3) 多轴归约（元组）：被指定的那些维一起消失
print(a.sum(axis=(0, 2)).shape)        # (5,)
print(a.sum(axis=(1, 2)).shape)        # (4,)

# (4) keepdims=True：被归约的维保留为 1，可继续与原数组广播
print(a.sum(axis=1, keepdims=True).shape)   # (4, 1, 3)
centered = a - a.mean(axis=1, keepdims=True)  # (4,5,3)-(4,1,3) 广播 → (4,5,3)

# (5) mean 与 sum 的形状规则完全一致（只差 dtype）
print(a.mean(axis=(0, 2)).shape)       # (5,)
```

**对照源码解释形状规则**：对形状 \((4,5,3)\) 沿 `axis=1` 归约——

- `keepdims=False`（默认）：被归约的轴（第 1 维，长度 5）被**移除**，结果形状 \((4,3)\)。这正是 C 层 `result_axes[i] = NPY_ITER_REDUCTION_AXIS(-1)` 的效果（见下一模块 [reduction.c:L258](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/reduction.c#L258)）。
- `keepdims=True`：被归约的轴**保留为长度 1**，结果形状 \((4,1,3)\)。对应 C 层 `result_axes[i] = NPY_ITER_REDUCTION_AXIS(curr_axis)` 并让该轴长度为 1。

把规则写成一行：

\[
\text{out.shape}[i] =
\begin{cases}
\text{in.shape}[i], & i \notin \text{axis} \\
1, & i \in \text{axis} \text{ 且 } \texttt{keepdims=True} \\
\text{(该维消失)}, & i \in \text{axis} \text{ 且 } \texttt{keepdims=False}
\end{cases}
\]

**需要观察的现象**：`keepdims=True` 让 `a.mean(axis=1, keepdims=True)` 形状 \((4,1,3)\) 能与 `a` 形状 \((4,5,3)\) 广播（第 1 维 1 拉伸到 5），从而 `centered` 每个元素减去对应行的均值。若忘了 `keepdims`，`a.mean(axis=1)` 是 \((4,3)\)，与 \((4,5,3)\) 广播会报形状错。

**预期结果**：所有 `shape` 打印与注释一致；`centered` 每行均值应接近 0（可用 `centered.mean(axis=1)` 验证）。若你的环境未装可运行 NumPy，则「待本地验证」形状输出，但形状推导规则可直接从源码确定。

#### 4.2.5 小练习与答案

**练习 1**：`np.sum(arr)`（arr 是 ndarray）会不会调用 `arr.sum()`？为什么？

**参考答案**：不会。`_wrapreduction` 在 `type(obj) is mu.ndarray` 时直接走 `ufunc.reduce(obj, ...)`（[fromnumeric.py:L82-L95](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/fromnumeric.py#L82-L95)），绕过对象方法以省一次调用开销。只有非 ndarray（或 ndarray 子类）才会 `getattr(obj, 'sum')`。两条路最终都到 `np.add.reduce`。

**练习 2**：对 \(4 \times 5 \times 3\) 数组，`a.sum(axis=(0,1,2))` 与 `a.sum(axis=None)` 结果有何异同？

**参考答案**：数值相同——都是全部元素之和。`axis=None` 在 `_count_reduce_items` 里被展开成 `tuple(range(arr.ndim))`（[_methods.py:L77-L78](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_methods.py#L77-L78)），所以 `axis=None` 等价于「列出所有轴」。一个小区别：`axis=None` 时 sum 可能走更高精度的「成对求和」快速路径（见 sum 的文档字符串说明）。

**练习 3**：为什么 `np.subtract.reduce` 不允许同时传两个轴？

**参考答案**：减法不可交换、不可重排序（\(a-b-c\) 换序结果不同）。C 层在 [reduction.c:L206-L213](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/reduction.c#L206-L213) 检查 `NPY_METH_IS_REORDERABLE` 标志，发现该 ufunc 不可重排序且 `count_axes > 1` 时抛 `reduction operation ... is not reorderable`。

---

### 4.3 C 层归约循环：reduction.c

#### 4.3.1 概念说明

前面两个模块都停在 `ufunc.reduce`。本模块下沉到 C 层，看「反复套用 ufunc 内层循环」到底是怎么跑的。核心是 `reduction.c` 里的 `PyUFunc_ReduceWrapper`——一个用 `NpyIter`（即 `np.nditer` 的 C 实现）驱动归约的引擎。

理解这段代码，关键抓住四个概念：

1. **结果轴映射（result_axes）**：把「输入的哪些轴」映射到「输出的哪些轴」。被归约的轴要么消失（`keepdims=False`），要么变成长度 1（`keepdims=True`）。
2. **幺元（identity）初始化**：有幺元的归约（加法→0，乘法→1）把结果预填成幺元；没有幺元的（max/min）必须先从输入拷一个元素过来当起点。
3. **wheremask**：`where=` 布尔掩码在 C 层表现为第三个迭代操作数，决定哪些元素真正参与折叠。
4. **可重排序性**：决定是否允许多轴归约。

`PyUFunc_ReduceWrapper` 是一个通用框架：它不关心「加」还是「乘」，只负责建迭代器、做初始化、然后回调一个由 ufunc 提供的内层循环（`reduce_loop`）。具体「加」或「乘」的语义由那条内层循环携带。

#### 4.3.2 核心流程

`PyUFunc_ReduceWrapper` 的执行流水线：

```
1. 可重排序性检查    —— count_axes 数被归约轴数，不可重排序且 >1 轴则报错
2. 建结果轴映射      —— 遍历输入各轴，被归约轴映射到 -1(消失) 或 长度1(keepdims)
3. 建 NpyIter        —— 把 result(op[0]) 与 operand(op[1]) [及 wheremask(op[2])]
                        一起迭代，标志含 BUFFERED|EXTERNAL_LOOP|GROWINNER
4. 取初始值          —— 有 initial 用之；否则 get_reduction_initial 取幺元；
                        都没有则 PyArray_CopyInitialReduceValues 拷首个元素
5. 取 strided loop   —— wheremask? GetMaskedStridedLoop : get_strided_loop
6. 初始化 result     —— 有幺元则把 result 全部填成幺元(raw_array_assign_scalar)
7. 跑循环            —— loop(context, strided_loop, auxdata, iter, ...)
                        在迭代器喂进来的每段缓冲区上反复调用内层循环
8. 收尾              —— 检查浮点异常，释放 iter/auxdata，返回 result
```

其中第 4 步是分叉点：

- `np.add.reduce`（幺元 0）：走「把 result 填 0，然后对每个元素做 `result += x`」。
- `np.maximum.reduce`（无幺元）：走 `PyArray_CopyInitialReduceValues`——把输入沿被归约轴的第 0 个切片拷进 result，然后跳过已拷的元素继续比较。

#### 4.3.3 源码精读

先看入口签名与文档注释，它把整个函数的输入输出讲得很清楚：

[numpy/_core/src/umath/reduction.c:L177-L183](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/reduction.c#L177-L183) —— `PyUFunc_ReduceWrapper` 的签名。注意 `axis_flags`（哪些轴被归约）、`keepdims`、`initial`、`loop`（由 ufunc 提供的内层循环回调）这几个关键形参。上方 [L147-L176](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/reduction.c#L147-L176) 的 TODO 注释很有意思：作者承认这其实就是「带 keepdims/axis 的通用 gufunc (i)->() 的第二份独立实现」，未来可能合并。

可重排序性检查：

[numpy/_core/src/umath/reduction.c:L206-L213](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/reduction.c#L206-L213) —— 若 ufunc 未设 `NPY_METH_IS_REORDERABLE` 且被归约轴数 `count_axes(...) > 1`，直接抛 `not reorderable`。`count_axes` 本体在 [L35-L47](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/reduction.c#L35-L47)，就是数 `axis_flags` 里 `True` 的个数。

结果轴映射（keepdims 的真正来源）：

[numpy/_core/src/umath/reduction.c:L250-L265](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/reduction.c#L250-L265) —— 遍历输入每一维：非归约轴顺序映射到结果轴（`curr_axis++`）；归约轴在 `keepdims` 时映射到 `NPY_ITER_REDUCTION_AXIS(curr_axis)`（长度 1），否则映射到 `-1`（删除）。这段代码就是 4.2.4 实践里形状规则的 C 层依据。

建迭代器与取结果：

[numpy/_core/src/umath/reduction.c:L286-L296](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/reduction.c#L286-L296) —— 用 `NpyIter_AdvancedNew` 同时迭代 result 与 operand（有 wheremask 时加第三个 op）。标志位含义：`BUFFERED`（缓冲非连续数据）、`EXTERNAL_LOOP`（外层循环返回一段连续区，减少调用次数）、`GROWINNER`（内层尽量长）、`ZEROSIZE_OK`（允许空数组）、`DONT_NEGATE_STRIDES`（避免与首元素拷贝逻辑冲突）。`result` 直接从迭代器拿（`NpyIter_GetOperandArray(iter)[0]`）。

取初始值（identity 分叉点）：

[numpy/_core/src/umath/reduction.c:L305-L339](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/reduction.c#L305-L339) —— 若用户给了 `initial`，用它打包进 `initial_buf`；否则调用 `context->method->get_reduction_initial(...)`——这正是 u4-l3 讲过的 `identity` 字段在归约里的体现（加法返回 0、乘法返回 1、最大值返回「无」）。返回「无」时 `initial_buf` 被释放，下一步改走拷首元素。

初始化 result（两条分支）：

[numpy/_core/src/umath/reduction.c:L369-L400](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/reduction.c#L369-L400) —— 有 `initial_buf` 时，用 `raw_array_assign_scalar` 把 result 整体填成幺元（所以空数组 `np.sum` 返回 0）；没有时调 `PyArray_CopyInitialReduceValues` 把输入沿归约轴的第 0 个切片拷进 result。`PyArray_CopyInitialReduceValues` 本体在 [L75-L145](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/reduction.c#L75-L145)，遇到空输入会抛 `zero-size array to reduction operation ... which has no identity`——这就是 `np.max([])` 报错、而 `np.sum([])` 返回 0 的根因。

取内层循环并真正执行：

[numpy/_core/src/umath/reduction.c:L341-L356](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/reduction.c#L341-L356) —— 选 strided loop：有 wheremask 用 `PyArrayMethod_GetMaskedStridedLoop`，否则 `get_strided_loop`。这是 u4-l3 讲过的新式 ArrayMethod 机制。

[numpy/_core/src/umath/reduction.c:L418-L422](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/reduction.c#L418-L422) —— 真正跑循环：`loop(context, strided_loop, auxdata, iter, dataptr, strideptr, countptr, iternext, ...)`。`loop` 是 `ufunc_object.c` 提供的 `reduce_loop`，它用迭代器喂进来的 `dataptr/strideptr/countptr` 在每段缓冲区上反复调用 `strided_loop`，把 `result` 一点点更新出来。

最后，是谁调用了 `PyUFunc_ReduceWrapper`？看 ufunc 的 `.reduce` 方法实现：

[numpy/_core/src/umath/ufunc_object.c:L2686-L2693](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/ufunc_object.c#L2686-L2693) —— `PyUFunc_Reduce` 先试 `try_reduce_contiguous` 快速路径（仅对 `axis=None`、连续、对齐、无需转型的输入，绕过 NpyIter 直接跑内层循环并写进一个 0 维结果），不满足才回落到 `PyUFunc_ReduceWrapper`。这正是 sum 文档里「不带 axis 时用成对求和、精度更高」的工程落点。`try_reduce_contiguous` 见 [L2534-L2549](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/ufunc_object.c#L2534-L2549)。

而 `PyUFunc_Reduce` 又挂在 ufunc 类型的 `reduce` 方法表上：

[numpy/_core/src/umath/ufunc_object.c:L6694-L6697](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/ufunc_object.c#L6694-L6697) —— `ufunc_methods[]` 里 `{"reduce", ufunc_reduce, ...}`。`np.add.reduce(...)` 就是通过这张表找到 `ufunc_reduce` → `PyUFunc_Reduce` → `PyUFunc_ReduceWrapper` 的。

> 把整条 C 链与 Python 链拼起来：`arr.sum()` →（methods.c [L3059-L3061](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/methods.c#L3059-L3061) 登记 `"sum"` → [L2410-L2413](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/methods.c#L2410-L2413) 的 `array_sum`）→ `_methods._sum` → `um.add.reduce` →（ufunc_object.c `ufunc_methods`）→ `PyUFunc_Reduce` → `PyUFunc_ReduceWrapper`（reduction.c）→ 内层 strided loop。

#### 4.3.4 代码实践

**目标**：用运行时行为佐证 C 层的三条机制——「幺元决定空数组行为」「wheremask 影响分母」「不可重排序拒绝多轴」。

**操作步骤**：

```python
# 示例代码
import numpy as np

# (1) 幺元 vs 无幺元：空数组的差别
print(np.array([], dtype=np.float64).sum())          # 0.0  —— add 有幺元 0
try:
    np.array([], dtype=np.float64).max()              # 报错 —— maximum 无幺元
except ValueError as e:
    print("max([]) raises:", e)
# 用 initial 给无幺元归约一个起点
print(np.array([], dtype=np.float64).max(initial=-1)) # -1.0

# (2) where= 掩码：对照 _count_reduce_items 把分母也调小
a = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
mask = np.array([[True, True, False], [True, False, False]])
print(a.sum(axis=1, where=mask))                      # [3., 4.]  只加 True 的
print(a.mean(axis=1, where=mask))                     # [1.5, 4.] 分母是 True 个数

# (3) 不可重排序归约拒绝多轴
b = np.arange(12).reshape(3, 4)
print(np.subtract.reduce(b, axis=0).shape)            # (4,) —— 单轴 OK
try:
    np.subtract.reduce(b, axis=(0, 1))                # 报错 —— 多轴不行
except ValueError as e:
    print("subtract.reduce multi-axis raises:", e)
```

**对照源码解释**：

- (1) 对应 [reduction.c:L369-L400](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/reduction.c#L369-L400)：`sum` 有幺元 0，空输入返回 0；`max` 无幺元，走 `PyArray_CopyInitialReduceValues`，在 [L99-L104](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/reduction.c#L99-L104) 撞上 `shape_orig[idim] == 0` 抛 `zero-size array ... which has no identity`。给了 `initial` 后走 `initial_buf` 分支，不再需要拷首元素。
- (2) 对应 [_methods.py:L85-L93](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_methods.py#L85-L93)：`_count_reduce_items` 对 `where` 数组求和得到真正参与计算的元素数，故 `mean` 分母是 `True` 的个数而非轴长。注意：直接用 `where` 必须给 `initial`（见 [reduction.c:L381-L388](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/reduction.c#L381-L388) 的报错路径），`_sum`/`_mean` 因为 add 有幺元所以隐式满足。
- (3) 对应 [reduction.c:L206-L213](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/reduction.c#L206-L213)：`np.subtract` 未设 `NPY_METH_IS_REORDERABLE`，多轴被拒。

**需要观察的现象**：`np.array([]).max()` 报 `zero-size array to reduction operation maximum which has no identity`；`np.subtract.reduce(b, axis=(0,1))` 报 `reduction operation 'maximum' ... is not reorderable`（确切函数名随调用）。

**预期结果**：三条运行时行为与源码注释一一对应。若环境无 NumPy，则「待本地验证」，但报错信息与触发条件可从 reduction.c 直接读出。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `np.sum(np.zeros((0, 3)), axis=0)` 不报错，且结果是长度 3 的全 0 数组？

**参考答案**：`add` 有幺元 0，`initial_buf` 分支用 `raw_array_assign_scalar` 把 result（形状 (3,)）整体填 0。注意 [reduction.c:L295-L304](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/reduction.c#L295-L304) 的 `empty_iteration` 判断：外层迭代虽空，但归约结果仍按第 1 维（长度 3）分配并填幺元。

**练习 2**：`try_reduce_contiguous` 快速路径在什么条件下触发？为什么需要它？

**参考答案**：条件见 [ufunc_object.c:L2547-L2549](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/ufunc_object.c#L2547-L2549)：`out==NULL && wheremask==NULL && initial==NULL && keepdims==0 && naxes==ndim` 且输入平凡可迭代、对齐、无需转型。即「全数组归约、无额外参数、内存连续」。需要它是因为 `PyUFunc_ReduceWrapper` 要建 NpyIter，对小而连续的数组，迭代器开销超过计算本身，快速路径直接在缓冲区上跑内层循环，省掉迭代器。

**练习 3**：`np.add.accumulate([1,2,3])` 与 `np.add.reduce([1,2,3])` 在底层复用了同一套 C 代码吗？

**参考答案**：没有完全复用。`reduce` 走 `PyUFunc_ReduceWrapper`（reduction.c），`accumulate` 走 [ufunc_object.c:L2750](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/ufunc_object.c#L2750) 附近的 `accumulate` 专用路径（也用 NpyIter，但语义不同：保留每步中间值而非折叠成一个值）。二者共用 ufunc 的内层 strided loop，但外层框架是两套。这也是 `cumsum(a)[-1]` 可能不等于 `sum(a)` 的原因——accumulate 不走成对求和。

---

## 5. 综合实践

把三个模块串起来，做一次「端到端调用链追踪 + 行为验证」。

**任务**：选定 `np.prod`，从 Python 顶层一路追到 C 层，并设计实验验证你对源码的理解。

**步骤**：

1. **画调用链**。参考本讲源码，写出 `a.prod(axis=0)` 与 `np.prod(a, axis=0)`（a 是 ndarray）的两条路径，标出经过的文件、函数、行号。提示：
   - `arr.prod()` → methods.c [L3020](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/methods.c#L3020) 登记 `"prod"` → [L2440-L2443](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/methods.c#L2440-L2443) `array_prod` → `_methods._prod`（[_methods.py:L51-L53](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_methods.py#L51-L53)）→ `umr_prod = um.multiply.reduce`（[_methods.py:L21](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_methods.py#L21)）→ `PyUFunc_Reduce` → `PyUFunc_ReduceWrapper`。
   - `np.prod(a)` → fromnumeric.py 的 `prod`（[L3366](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/fromnumeric.py#L3366)）→ `_wrapreduction(a, np.multiply, 'prod', ...)` →（ndarray 直通）`np.multiply.reduce(a, ...)` → 同上 C 层。

2. **预测行为**（写下来再运行）：
   - `np.prod(np.ones((2,3), dtype=np.int8), dtype=np.int8)` 会溢出吗？为什么？（提示：6 个 1 相乘不溢出，但思考如果元素是 2 会怎样。）
   - `np.prod(np.array([], dtype=np.int64))` 返回什么？（提示：乘法幺元。）
   - `np.prod(np.ones((2,3)), axis=1, keepdims=True)` 的形状？

3. **运行验证**：

   ```python
   # 示例代码
   import numpy as np
   print(np.prod(np.ones((2,3), dtype=np.int8), dtype=np.int8))   # int8(1)
   print(np.prod(np.array([], dtype=np.int64)))                   # 1
   print(np.prod(np.ones((2,3)), axis=1, keepdims=True).shape)    # (2, 1)
   ```

4. **回到源码核验**：`np.prod` 的空数组返回 1，对应 [reduction.c:L328](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/reduction.c#L328) 的 `get_reduction_initial`——对 `multiply` 这个 ufunc，`identity` 是 `PyUFunc_One`（见 u4-l3）。

**验收标准**：你能不看本讲答案，向别人讲清「`a.prod()` 为什么等价于反复做乘法、为什么空数组返回 1、为什么 keepdims 让第 1 维变 1」，并能在源码里指到具体行。

---

## 6. 本讲小结

- **归约 = ufunc.reduce**：`sum=add.reduce`、`prod=multiply.reduce`、`max=maximum.reduce`、`min=minimum.reduce`、`any=logical_or.reduce`、`all=logical_and.reduce`。它们在 `_methods.py` 里只是一行透传。
- **两条调用链殊途同归**：`arr.sum()` 经 C 方法 → `_methods._sum` → `add.reduce`；`np.sum(arr)`（ndarray）经 `_wrapreduction` 直通 `add.reduce`，绕过 `_methods._sum`。二者最终都进 `PyUFunc_Reduce`。
- **mean/std/var 是复合归约**：`_mean` = 「`add.reduce` 求和 ÷ `_count_reduce_items` 计数」，所以它们**一定**经过 `_methods.py`，不像 sum 可以绕过。
- **`_wrapreduction` 的分发哲学**：ndarray 直通 ufunc 求快；非 ndarray 优先调对象自己的方法以尊重子类/第三方（NEP-18）。positional-only 签名与 `_NoValue` 哨兵都是为热路径性能服务。
- **C 层 `PyUFunc_ReduceWrapper` 的四件事**：用 `NpyIter` 建迭代、用 `result_axes` 落实 keepdims、用 `get_reduction_initial`/`CopyInitialReduceValues` 处理幺元、用 `NPY_METH_IS_REORDERABLE` 守护多轴。
- **identity 决定空数组行为**：有幺元的归约（sum→0, prod→1）对空输入返回幺元；无幺元的（max/min）抛 `zero-size ... which has no identity`，除非给 `initial`。

---

## 7. 下一步学习建议

- **横向扩展到 SIMD 与性能**：本讲的 `try_reduce_contiguous` 与 strided loop 是 u4-l5（广播、SIMD 与性能优化）的入口。建议接着读 `numpy/_core/src/umath/loops_*.c` 生成的内层循环，以及 `.dispatch.c` 如何为不同 CPU 选 SIMD 实现。
- **纵向深入 ArrayMethod**：本讲多次提到 `get_strided_loop`、`get_reduction_initial`、`NPY_METH_IS_REORDERABLE`，这些都是新一代 ArrayMethod（NEP 43）的回调。若要自定义归约行为或写自定义 dtype，去 u8-l3（自定义 dtype 与 DType API）读 `dtype_api.h` 与 `dtypemeta.c`。
- **回头看累积与排序**：本讲的 `accumulate`（cumsum/cumprod）只点到为止；若需要排序类「归约」的完整图景，可读 `fromnumeric.py` 的 `sort/argsort/partition`（本讲源码地图已列出）。
- **nditer 的全貌**：`PyUFunc_ReduceWrapper` 的迭代器就是 `np.nditer`。要彻底理解 keepdims/广播在迭代层的实现，建议直接读 u9-l2（迭代器 nditer）与 `numpy/_core/src/multiarray/nditer_api.c`。
