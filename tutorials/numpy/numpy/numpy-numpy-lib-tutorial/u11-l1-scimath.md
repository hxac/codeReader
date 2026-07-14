# scimath 复数域安全运算

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 `numpy.lib.scimath`（别名 `np.emath`）与普通 ufunc（`np.sqrt`、`np.log` 等）在「负数、越界」输入上的行为差异。
- 理解 scimath 的核心设计招式：**先用一个 `_fix_*` 前置修正函数检测输入是否越界，必要时把输入提升为复数，再把计算原封不动地委托给同名普通 ufunc**。
- 读懂 `_tocomplex`、`_fix_real_lt_zero`、`_fix_real_abs_gt_1`、`_fix_int_lt_zero` 四个内部工具的职责与触发条件。
- 知道为什么 scimath 的输出 dtype 是「由数据值决定」而非「由输入 dtype 决定」，并能据此预测结果。
- 区分 `sqrt/log/log2/log10/logn`（负实数修正）、`arcsin/arccos/arctanh`（绝对值越界修正）、`power`（混合修正）三组函数的修正策略。

## 2. 前置知识

本讲默认你了解以下概念（不熟悉的术语下面会顺带解释）：

- **ufunc（通用函数）**：numpy 里对数组逐元素运算的函数，如 `np.sqrt`、`np.log`、`np.power`。它们对实数数组超出定义域的输入会返回 `nan` 或 `inf` 并发出 `RuntimeWarning`。
- **定义域与支割线（branch cut）**：很多数学函数在实数轴上有定义域限制。例如 \(\sqrt{x}\) 只对 \(x\ge 0\) 有实数值，\(\log(x)\) 只对 \(x>0\) 有实数值，\(\arccos(x)\) 只对 \(|x|\le 1\) 有实数值。当把函数延拓到**复数域**时，沿某条线（支割线）函数值会发生跳变，因此需要约定取「主值（principal value）」。
- **复数主值**：复变函数约定主值辐角 \(\arg(z)\in(-\pi,\pi]\)。例如对负实数 \(x<0\)，有 \(\arg(x)=\pi\)，于是
  - \(\sqrt{x}=i\sqrt{|x|}\)（如 \(\sqrt{-1}=i\)）；
  - \(\log(x)=\ln|x|+i\pi\)（如 \(\log(-e)=1+i\pi\)）。
- **dtype 提升**：把整型/浮点数组转换成复数数组（`float64 → complex128`、`float32 → complex64`）。numpy 的 ufunc 一旦看到复数 dtype 输入，就会走「复数实现」，自动算出含虚部的主值，而不再返回 `nan`。
- **薄再导出层与 `_impl` 实现层 / dispatcher+impl 双函数写法**：这两点是 u1-l1、u1-l2 已建立的认知，本讲直接承接，不再重复展开。

> 一句话直觉：**普通 ufunc 在实数越界时会「缴械」返回 nan/inf；scimath 的全部本事，就是在越界的那一瞬间把输入悄悄升级成复数，让同一个 ufunc 改走复数实现，从而给出数学上正确的主值。**

## 3. 本讲源码地图

本讲只涉及两个源文件（外加一处顶层别名）：

| 文件 | 作用 |
| --- | --- |
| [scimath.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/scimath.py#L1-L13) | **薄再导出层**。整个文件只有 13 行，用 `from ._scimath_impl import ...` 把实现层的 9 个函数搬到 `numpy.lib.scimath` 命名空间。 |
| [_scimath_impl.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L1-L21) | **实现层**。本讲全部源码精读都在这里：模块文档与导入、`__all__`、4 个 `_fix_*`/`_tocomplex` 工具、3 个 dispatcher、9 个公开函数。 |
| numpy/\_\_init\_\_.py:455 | 顶层 `from .lib import scimath as emath`，使 `np.emath` 成为 `np.lib.scimath` 的别名。 |

`__all__` 共 9 个公开函数（[\_scimath_impl.py:L23-L26](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L23-L26)）：

```
'sqrt', 'log', 'log2', 'logn', 'log10', 'power', 'arccos', 'arcsin', 'arctanh'
```

它们恰好分成三组，对应三种修正策略，也是本讲第 4 节的三个核心模块。

## 4. 核心概念与源码讲解

### 4.1 scimath 的定位与「前置修正 + 复用 ufunc」范式

#### 4.1.1 概念说明

`scimath` 的名字来自 **sci**entific **math**：它提供一批「对用户更友好」的数学包装函数。友好的含义很具体——**当输入落在实数定义域之外时，给出复数域上的数学主值，而不是 `nan`**。

考虑一个最典型的对比：

| 输入 | `np.sqrt(-1)` | `np.emath.sqrt(-1)` |
| --- | --- | --- |
| 结果 | `nan`（并发出 `invalid value encountered` 警告） | `1j` |
| 含义 | 实数 sqrt 遇到负数，无法表示，放弃 | 复数主值 \(\sqrt{-1}=i\) |

scimath 实现这一点的手段极其简洁，可以概括成一个两行范式：

```python
def scimath_func(x):
    x = _fix_XXX(x)      # ① 前置修正：检测越界，必要时把 x 提升为复数
    return nx.func(x)    # ② 委托给同名普通 ufunc；输入已是复数时它给出主值
```

这里的关键洞察是：**numpy 的普通 ufunc 本身就内置了正确的复数实现**（遵循 C99 的支割线约定）。当输入是**实数**且越界时，它返回 `nan`；但只要输入 dtype 变成**复数**，同一个 ufunc 就会算出数学上正确的主值。所以 scimath 不需要重新实现任何数学，它只需要「在正确的时机把 dtype 升级成复数」。这份「正确的时机」判断，就是 `_fix_*` 函数的全部职责。

这也解释了 scimath 最反直觉的一条性质：**输出 dtype 取决于数据值，而非输入 dtype**。同一个函数、同一个输入 dtype，只要数组里存在一个越界元素，整个结果就升为复数；否则保持实数。

#### 4.1.2 核心流程

scimath 的执行流程可以拆成「修正层」与「计算层」两段：

```text
输入 x (可能是 list/标量/数组)
   │
   ▼
① asarray(x)                      # 规整成数组
   │
   ▼
② 检测越界:  any( isreal(x) & (越界条件) )   # 只要存在一个越界的「实」元素
   │           是 ───────────────► 否
   │           │                    │
   ▼           ▼                    ▼
③ _tocomplex(x)  (提升为复数)     原样返回 (仍为实数)
   │                                │
   └────────────┬───────────────────┘
                ▼
④ nx.<同名 ufunc>(x)              # 复数输入 → 主值; 实数输入 → 实数结果
                │
                ▼
              输出
```

三个要点：

1. **惰性提升（lazy promotion）**：`_fix_*` 用 `any(...)` 判断「是否存在越界元素」。只要没有越界元素，就**不提升**，结果保持实数 dtype；只有存在越界元素时才整体升复数。
2. **isreal 过滤**：修正条件里都带 `isreal(x)`，目的是只关心「虚部恰好为 0 的实元素」。一个本就是复数的数组无需提升（它本来就会走复数实现）。
3. **dispatcher**：每个公开函数都用 `@array_function_dispatch(...)` 装饰，遵循 NEP-18 的 `__array_function__` 派发协议（承接 u1-l2）。dispatcher 只负责把参与运算的数组参数收集成元组，不做计算。

#### 4.1.3 源码精读

**薄再导出层** [scimath.py:L1-L13](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/scimath.py#L1-L13)：把实现层的名字搬到公开命名空间。注意它连 `__all__`、`__doc__` 也一并再导出，保证 `help(numpy.lib.scimath)` 能看到模块文档。

**顶层别名** [numpy/\_\_init\_\_.py:L455](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/__init__.py#L455)：`from .lib import scimath as emath`，这就是 `np.emath.sqrt(...)` 能用的原因。因此 `np.emath` 与 `np.lib.scimath` 是同一个模块对象。

**三个 dispatcher**（实现层）：

- 一元函数统一用 [_unary_dispatcher:L181-L182](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L181-L182)，返回 `(x,)`，被 `sqrt/log/log2/log10/arccos/arcsin/arctanh` 共用。
- `logn` 是二元（底数 n + 真数 x），用 [_logn_dispatcher:L343-L344](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L343-L344)，返回 `(n, x)`。
- `power` 也是二元（底 x + 指数 p），用 [_power_dispatcher:L435-L436](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L435-L436)，返回 `(x, p)`。

**模块文档** [\_scimath_impl.py:L1-L16](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L1-L16) 明确点明了本模块的使命：对「输出 dtype 在某些输入域内不同于输入 dtype」的函数做更友好的包装，并在支割线处给出复数主值。

#### 4.1.4 代码实践

1. **实践目标**：直观对比 scimath 与普通 ufunc 在越界输入上的差异，并验证 `np.emath` 就是 `np.lib.scimath`。
2. **操作步骤**：
   ```python
   import numpy as np
   import warnings

   print(np.emath is np.lib.scimath)        # 验证别名

   with warnings.catch_warnings():
       warnings.simplefilter("always")      # 让警告显形
       print(np.sqrt(-1))                   # 普通 ufunc

   print(np.emath.sqrt(-1))                 # scimath
   print(np.emath.sqrt([-1, 4]))            # 注意结果 dtype
   print(np.emath.sqrt([1, 4]))             # 与上一行对比 dtype
   ```
3. **需要观察的现象**：`np.sqrt(-1)` 会打印一条 `invalid value encountered in sqrt` 警告并返回 `nan`；`np.emath.sqrt(-1)` 不报警告，返回 `1j`；`np.emath.sqrt([-1,4])` 的 dtype 是 `complex128`，而 `np.emath.sqrt([1,4])` 的 dtype 仍是 `float64`。
4. **预期结果**：`True`；`nan`（带警告）；`1j`；`[0.+1.j, 2.+0.j]`（complex128）；`[1., 2.]`（float64）。
5. 若本地 numpy 版本/打印格式略有差异，以实际输出为准（**待本地验证**）。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 scimath 不需要为 `sqrt` 单独写复数开方算法？
  - **答案**：因为 `np.sqrt` 这个 ufunc 本身就有正确的复数实现，遵循 C99 支割线约定。scimath 只要在输入含负实数时把 dtype 升成复数，复数实现就会自动给出主值 \(\sqrt{x}=i\sqrt{|x|}\)，无需重写数学。
- **练习 2**：`np.emath.sqrt([1,4])` 与 `np.emath.sqrt([-1,4])` 的输出 dtype 为什么不同？
  - **答案**：`_fix_real_lt_zero` 用 `any(isreal(x) & (x<0))` 判断。前者无负数，不提升，结果保持 `float64`；后者含 `-1`，整体提升为 `complex128`。这正是「输出 dtype 由数据值决定」的体现。

---

### 4.2 类型提升内核：`_tocomplex` 与 `isreal` 检测

#### 4.2.1 概念说明

所有 `_fix_*` 修正函数在决定「要提升」之后，最终都落到同一个工具 `_tocomplex`：它把任意数组转成「能装下原数据的最小复数类型」。这里「最小」是有讲究的——单精度浮点（`single/float32`）、字节型（`byte`）、短整型（`short`）等较窄的类型升级为 `csingle`（`complex64`），其余升级为 `cdouble`（`complex128`）。这样能避免无谓地把 `float32` 提到 `complex128` 而损失精度效率。

另一个关键依赖是 `isreal`（从 `_type_check_impl` 导入，[L21](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L21)）。它逐元素判断「虚部是否为 0」，返回布尔数组。修正函数用它来锁定「真正的实元素」，避免对一个本就是复数（虚部非零）的数组做无意义的越界判断。

> 术语：`astype` 永远会**复制**一份新数组。`_tocomplex` 的文档明确强调「a copy is always made」，所以即便输入已是复数，结果也是一个独立副本——这保证了后续 ufunc 不会污染调用者的原数组。

#### 4.2.2 核心流程

`_tocomplex` 的逻辑：

```text
arr
 │
 ▼
arr.dtype.type 是否是 (single, byte, short, ubyte, ushort, csingle) 之一?
 │ 是 ─────────► 否
 ▼              ▼
arr.astype(csingle)   arr.astype(cdouble)
 │                      │
 └───────┬──────────────┘
         ▼
   complex 数组 (独立副本)
```

#### 4.2.3 源码精读

[\_tocomplex:L32-L93](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L32-L93) 的核心只有结尾两分支（[L89-L93](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L89-L93)）：

```python
if issubclass(arr.dtype.type, (nt.single, nt.byte, nt.short, nt.ubyte,
                               nt.ushort, nt.csingle)):
    return arr.astype(nt.csingle)
else:
    return arr.astype(nt.cdouble)
```

其文档字符串给出了三组对照示例（`short→complex64`、`double→complex128`、`csingle→complex64` 且为副本），可作为理解 dtype 映射的权威依据。模块顶部还定义了 [_ln2 = nx.log(2.0)](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L29)，是历史遗留常数（当前实现中 `log2` 等直接复用 `nx.log2`，并未用到它）。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：验证 `_tocomplex` 的「最小复数类型」选择规则。
2. **操作步骤**：阅读 [\_tocomplex 文档示例:L50-L87](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L50-L87)，然后在本地执行：
   ```python
   import numpy as np
   for t in [np.short, np.single, np.double, np.csingle]:
       a = np.array([1, 2, 3], t)
       print(t.__name__, "->", np.lib.scimath._tocomplex(a).dtype)
   ```
3. **需要观察的现象**：`short` 与 `single`、`csingle` 都映射到 `complex64`；`double` 映射到 `complex128`。
4. **预期结果**：`short -> complex64`、`single -> complex64`、`double -> complex128`、`csingle -> complex64`（具体类型名以本地为准，**待本地验证**）。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `_tocomplex` 要把 `float32` 映射到 `complex64` 而不是 `complex128`？
  - **答案**：保持精度与内存的「最小够用」原则。`float32` 的实部、虚部各占 4 字节，合起来正好是 8 字节的 `complex64`；升到 `complex128` 会无谓翻倍内存且不带来额外信息。
- **练习 2**：`isreal(np.array([-1+0j, 2+0j]))` 返回什么？这为什么对 `_fix_real_lt_zero` 重要？
  - **答案**：返回 `array([True, True])`（两个元素虚部都是 0）。它让修正函数即便面对复数 dtype 数组，也能识别出「落在实轴上的负数」并触发提升；不过此时 `_tocomplex` 只是复制，不会改变 dtype 等级。

---

### 4.3 负实数修正 `_fix_real_lt_zero`：`sqrt` 与 `log` 家族

#### 4.3.1 概念说明

`sqrt`、`log`、`log2`、`log10`、`logn` 这五个函数的共同点是：实数定义域都是 \(x>0\)（`sqrt` 是 \(x\ge0\)），越界方向都是「负实数」。因此它们共用同一个修正函数 `_fix_real_lt_zero`：**只要存在负实数元素，就把整个数组提升为复数**。

对应的数学主值（复数延拓，主辐角 \(\arg\in(-\pi,\pi]\)）：

- \(\sqrt{x}=i\sqrt{|x|}\)，对 \(x<0\)。例：\(\sqrt{-1}=i\)。
- \(\log(x)=\ln|x|+i\pi\)，对 \(x<0\)。例：\(\log(-e)=1+i\pi\)。
- \(\log_b(x)=\dfrac{\ln x}{\ln b}\)，`logn` 用换底公式实现。
- \(\log_2\)、\(\log_{10}\) 同理。

> 注意边界：`log(0)` 返回 `-inf`、`log(inf)` 返回 `inf`，这与普通 `np.log` **完全一致**——因为 `0` 和 `inf` 都不是「负实数」，不会触发提升。只有严格的 \(x<0\) 才走复数。

#### 4.3.2 核心流程

`_fix_real_lt_zero` 的逻辑：

```text
x
 │ asarray(x)
 ▼
存在 any( isreal(x) & (x < 0) ) ?
 │ 是 ─────────────► 否
 ▼                   ▼
_tocomplex(x)        x (保持实数)
 │                   │
 └────────┬──────────┘
          ▼
  nx.<sqrt|log|log2|log10>(x)      # logn 额外对底数 n 也做一次修正
```

`logn` 的特殊性在于它有**两个**参数（底数 `n` 与真数 `x`），二者都可能是负数，所以它对 `n` 和 `x` **各调用一次** `_fix_real_lt_zero`，再用换底公式 `nx.log(x)/nx.log(n)` 计算（[L380-L382](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L380-L382)）。

#### 4.3.3 源码精读

**修正函数** [\_fix_real_lt_zero:L96-L122](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L96-L122)，核心三行（[L119-L122](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L119-L122)）：

```python
x = asarray(x)
if any(isreal(x) & (x < 0)):
    x = _tocomplex(x)
return x
```

**五个公开函数的实现体**都极短，是范式的标准样板：

| 函数 | 修正 | 计算 | 行号 |
| --- | --- | --- | --- |
| `sqrt` | `_fix_real_lt_zero(x)` | `nx.sqrt(x)` | [L237-L238](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L237-L238) |
| `log` | `_fix_real_lt_zero(x)` | `nx.log(x)` | [L287-L288](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L287-L288) |
| `log10` | `_fix_real_lt_zero(x)` | `nx.log10(x)` | [L339-L340](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L339-L340) |
| `log2` | `_fix_real_lt_zero(x)` | `nx.log2(x)` | [L431-L432](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L431-L432) |
| `logn` | `_fix_real_lt_zero(x)` 且 `_fix_real_lt_zero(n)` | `nx.log(x)/nx.log(n)` | [L380-L382](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L380-L382) |

以 `sqrt` 为例（[L185-L238](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L185-L238)），文档明确写出 `np.emath.sqrt(-1) == 1j`、`np.emath.sqrt([-1,4]) == [0.+1.j, 2.+0.j]`，并提示浮点 `0.0` 与 `-0.0` 在复数开方下会得到不同符号的纯虚结果（`complex(-4.0, 0.0)` 开方得 `2j`，`complex(-4.0, -0.0)` 得 `-2j`）——这是支割线两侧主值符号差异的体现。

`log` 的文档（[L267-L272](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L267-L272)）则点明：除了「实 \(x<0\) 时返回复数主值」这一处不同，`scimath.log` 与 `numpy.log` 在 `0→-inf`、`inf→inf`、`x.imag≠0→复数主值` 上完全一致。

#### 4.3.4 代码实践（本讲核心实践）

1. **实践目标**：用 `numpy.lib.scimath.sqrt` 对负数求平方根，对比 `np.sqrt` 的 `nan` 结果；并观察「值决定 dtype」现象。
2. **操作步骤**：
   ```python
   import numpy as np
   import warnings

   # 对比一：负标量
   with warnings.catch_warnings():
       warnings.simplefilter("always")
       r1 = np.sqrt(-1)            # 普通 ufunc
   r2 = np.emath.sqrt(-1)          # scimath
   print("np.sqrt(-1)    ->", r1)
   print("scimath.sqrt(-1)->", r2)

   # 对比二：值决定 dtype
   a = np.emath.sqrt([1, 4, 9])        # 无负数
   b = np.emath.sqrt([-1, 4, 9])       # 含负数
   print("无负数: dtype =", a.dtype, "值 =", a)
   print("含负数: dtype =", b.dtype, "值 =", b)

   # 对比三：log 家族
   print("scimath.log(-e) =", np.emath.log(-np.e), " (期望 1+πj)")
   print("scimath.logn(2, [-4, 8]) =", np.emath.logn(2, [-4, 8]))
   ```
3. **需要观察的现象**：`np.sqrt(-1)` 触发 `invalid value encountered in sqrt` 警告并得 `nan`；`scimath.sqrt(-1)` 得 `1j` 无警告；`a` 为 `float64`、`b` 为 `complex128`；`scimath.log(-e)` 实部为 1、虚部约为 \(\pi\)。
4. **预期结果**：`nan`（带警告）；`1j`；无负数 `dtype=float64` 值 `[1.,2.,3.]`；含负数 `dtype=complex128` 值 `[0.+1.j, 2.+0.j, 3.+0.j]`；`log(-e)≈(1+3.14159j)`；`logn(2,[-4,8])` 中 `-4` 项为 `2.+4.5324j`、`8` 项为 `3.+0.j`（数值取自源码文档示例，**待本地验证**）。

#### 4.3.5 小练习与答案

- **练习 1**：`np.emath.log(0)` 返回什么？为什么不是复数？
  - **答案**：返回 `-inf`。因为 `0` 不满足 `x<0`，不触发 `_fix_real_lt_zero` 的提升，直接走实数 `nx.log(0)=-inf`。这与普通 `np.log(0)` 行为一致。
- **练习 2**：`np.emath.logn(2, [-4, 8])` 中 `-4` 那一项为什么是 `2.+4.5324j`？
  - **答案**：\(\log_2(-4)=\dfrac{\ln 4 + i\pi}{\ln 2}=\dfrac{\ln4}{\ln2}+i\dfrac{\pi}{\ln2}=2 + i\cdot\dfrac{\pi}{\ln2}\approx 2+4.5324j\)。`logn` 对真数与底数都做负实数修正后，用换底公式算出复数主值。

---

### 4.4 绝对值越界修正 `_fix_real_abs_gt_1`：反三角与反双曲函数

#### 4.4.1 概念说明

`arcsin`、`arccos`、`arctanh` 三个函数的实数定义域不是「正负」问题，而是「**绝对值不超过 1**」的问题：

- \(\arccos(x)\) 实数定义域 \([-1,1]\)，主值区间 \([0,\pi]\)；
- \(\arcsin(x)\) 实数定义域 \([-1,1]\)，主值区间 \([-\pi/2,\pi/2]\)；
- \(\operatorname{arctanh}(x)\) 实数定义域 \((-1,1)\)（开区间，端点 \(x=\pm1\) 分别得 \(\pm\infty\)）。

当 \(|x|>1\) 时，这三个函数没有实数值，普通 ufunc 返回 `nan`；scimath 则通过 `_fix_real_abs_gt_1` 把输入提升为复数，给出复数主值。例如对 \(|x|>1\)：

\[ \arccos(x) = -i\,\log\!\bigl(x + i\sqrt{1-x^2}\bigr) \]

此时 \(1-x^2<0\)，\(\sqrt{1-x^2}\) 为纯虚数，整个表达式自然落到复数域。源码文档示例 `np.emath.arccos([1,2])` 给出 `[0.-0.j, 0.-1.317j]`，即 \(\arccos(2)\approx -1.317i\)。

#### 4.4.2 核心流程

`_fix_real_abs_gt_1` 的逻辑与 `_fix_real_lt_zero` 同构，只是判据换成「绝对值大于 1」：

```text
x
 │ asarray(x)
 ▼
存在 any( isreal(x) & (abs(x) > 1) ) ?
 │ 是 ─────────────────► 否
 ▼                       ▼
_tocomplex(x)            x (保持实数)
 │                       │
 └──────────┬────────────┘
            ▼
  nx.<arccos|arcsin|arctanh>(x)
```

注意端点行为：`arctanh(1)=inf`、`arctanh(-1)=-inf`，因为 \(|x|=1\) 不满足 `abs(x)>1`，不触发提升，直接走实数实现（源码文档在 [L631-L636](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L631-L636) 演示了用 `warnings.catch_warnings` 忽略 `arctanh(eye(2))` 的 `divide by zero` 警告并得到 `inf`）。

#### 4.4.3 源码精读

**修正函数** [\_fix_real_abs_gt_1:L153-L178](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L153-L178)，核心三行（[L175-L178](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L175-L178)）：

```python
x = asarray(x)
if any(isreal(x) & (abs(x) > 1)):
    x = _tocomplex(x)
return x
```

三个公开函数的实现体同样是两行样板：

| 函数 | 修正 | 计算 | 行号 |
| --- | --- | --- | --- |
| `arccos` | `_fix_real_abs_gt_1(x)` | `nx.arccos(x)` | [L537-L538](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L537-L538) |
| `arcsin` | `_fix_real_abs_gt_1(x)` | `nx.arcsin(x)` | [L585-L586](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L585-L586) |
| `arctanh` | `_fix_real_abs_gt_1(x)` | `nx.arctanh(x)` | [L641-L642](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L641-L642) |

#### 4.4.4 代码实践

1. **实践目标**：观察 `|x|>1` 时 scimath 与普通 ufunc 的差异，并理解端点 \(|x|=1\) 的特殊行为。
2. **操作步骤**：
   ```python
   import numpy as np
   import warnings

   with warnings.catch_warnings():
       warnings.simplefilter("always")
       print("np.arccos(2)        =", np.arccos(2))     # 普通 ufunc
   print("scimath.arccos(2)    =", np.emath.arccos(2))
   print("scimath.arcsin(2)    =", np.emath.arcsin(2))
   print("scimath.arctanh(2)   =", np.emath.arctanh(2))

   # 端点 |x|=1 不触发提升
   with warnings.catch_warnings():
       warnings.simplefilter("ignore")
       print("scimath.arctanh(1)  =", np.emath.arctanh(1))   # inf，非复数
   ```
3. **需要观察的现象**：`np.arccos(2)` 返回 `nan` 并报警告；`scimath.arccos(2)` 返回纯虚复数；`scimath.arctanh(1)` 返回 `inf`（实数，非复数）。
4. **预期结果**：`np.arccos(2)=nan`（带 `invalid value` 警告）；`scimath.arccos(2)≈-1.317j`；`scimath.arcsin(2)≈1.571-1.317j`；`scimath.arctanh(2)` 为复数；`scimath.arctanh(1)=inf`（数值精度以本地为准，**待本地验证**）。

#### 4.4.5 小练习与答案

- **练习 1**：`_fix_real_lt_zero` 与 `_fix_real_abs_gt_1` 在结构上几乎相同，它们唯一的本质区别是什么？
  - **答案**：判据不同。前者检测 `isreal(x) & (x < 0)`（负实数），用于 `sqrt/log` 家族；后者检测 `isreal(x) & (abs(x) > 1)`（绝对值超 1），用于反三角/反双曲函数。其余（asarray、any、_tocomplex）完全一致。
- **练习 2**：为什么 `np.emath.arctanh(1)` 返回的是实数 `inf` 而不是复数？
  - **答案**：因为 `abs(1) > 1` 为假，不触发 `_fix_real_abs_gt_1` 的提升，输入保持实数；实数 `nx.arctanh(1)` 在数学上发散到 \(+\infty\)，于是返回 `inf`（并可能伴随 `divide by zero` 警告）。

---

### 4.5 `power` 的混合修正：`_fix_real_lt_zero` + `_fix_int_lt_zero`

#### 4.5.1 概念说明

`power(x, p)`（即 \(x^p\)）是本讲里唯一一个对**两个参数都做修正**、且对两个参数用**不同**修正函数的成员：

- 对**底数 `x`** 用 `_fix_real_lt_zero`：底数为负时，\(x^p\) 通常需要复数（除非 `p` 恰好是整数）。
- 对**指数 `p`** 用 `_fix_int_lt_zero`：指数为负整数时，把它转成浮点（`* 1.0`），避免整数除法/取整问题。

注意 `_fix_int_lt_zero` 与 `_fix_real_lt_zero` 的区别：它**不提升到复数**，只是把含负数的整数数组乘以 `1.0` 转成浮点（[L147-L150](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L147-L150)）。这是因为指数本身不需要复数表示，但负整数指数必须脱离整数类型才能正确表达「倒数」语义。

数学上 \(x^p = e^{p\log x}\)。当 \(x<0\) 且 `p` 非整数时，\(\log x\) 为复数，从而 \(x^p\) 为复数。源码文档示例 `np.emath.power([-2, 4], 2)` 得到 `[4.-0.j, 16.+0.j]`——虽然结果是实的，但因为底数含 `-2` 触发了提升，结果 dtype 仍是复数（带 `-0.j` 虚部）。

#### 4.5.2 核心流程

```text
x (底数)                     p (指数)
 │                            │
 ▼ _fix_real_lt_zero          ▼ _fix_int_lt_zero
x: 含负实数则升复数           p: 含负数(整数)则 *1.0 转浮点
 │                            │
 └─────────────┬──────────────┘
               ▼
        nx.power(x, p)
```

`_fix_int_lt_zero` 的内部逻辑（[L147-L150](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L147-L150)）：

```python
x = asarray(x)
if any(isreal(x) & (x < 0)):
    x = x * 1.0
return x
```

#### 4.5.3 源码精读

`power` 的实现体在 [L489-L491](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L489-L491)：

```python
x = _fix_real_lt_zero(x)
p = _fix_int_lt_zero(p)
return nx.power(x, p)
```

`_fix_int_lt_zero` 定义在 [L125-L150](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L125-L150)，文档示例展示 `[-1,2]` 被转成 `[-1., 2.]`（浮点）。dispatcher 用 [_power_dispatcher:L435-L436](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_scimath_impl.py#L435-L436) 返回 `(x, p)`。

#### 4.5.4 代码实践

1. **实践目标**：理解 `power` 对底数与指数的不同修正，以及「底数含负数即得复数 dtype」的现象。
2. **操作步骤**：
   ```python
   import numpy as np
   print(np.emath.power([-2, 4], 2))      # 底数含负，结果复数
   print(np.emath.power([2, 4], -2))      # 指数负整数，转浮点
   print(np.emath.power([2, 4], 2))       # 全正，结果实数
   ```
3. **需要观察的现象**：第一行结果带 `-0.j`/`+0.j` 虚部（complex128）；第二行是浮点倒数 `[0.25, 0.0625]`；第三行是整数 `[4, 16]`（实数，未提升）。
4. **预期结果**：`[4.-0.j, 16.+0.j]`；`[0.25, 0.0625]`；`[4, 16]`（与源码文档示例一致，**待本地验证**）。

#### 4.5.5 小练习与答案

- **练习 1**：`np.emath.power([-2, 4], 2)` 的结果明明都是实数 4 和 16，为什么 dtype 是复数、还带 `-0.j`？
  - **答案**：底数 `-2` 是负实数，触发 `_fix_real_lt_zero` 把整个 `x` 提升为复数；之后 `nx.power` 在复数域计算，得到 `4-0j`、`16+0j`（虚部为 0 但保留复数 dtype）。这再次体现了「值决定 dtype」。
- **练习 2**：为什么 `power` 对指数 `p` 用 `_fix_int_lt_zero`（转浮点）而不是 `_fix_real_lt_zero`（转复数）？
  - **答案**：指数本身不需要复数表示。负整数指数的真正问题是整数类型无法表达「倒数」（如 \(2^{-2}=0.25\)），所以只需乘 `1.0` 转成浮点即可；而底数为负才是产生复数结果的原因，故对底数才用 `_fix_real_lt_zero`。

## 5. 综合实践

设计一个贯穿本讲的小任务：**写一个「安全幂-对数往返」实验，验证 scimath 的复数主值在数学上自洽**。

1. **目标**：对一批含负数的底数，用 `scimath.power` 求幂，再用 `scimath.log` 还原，验证 \(e^{\log(x)}=x\)（在复数主值意义下）对负数也成立。
2. **步骤**：
   ```python
   import numpy as np

   x = np.array([-2.0, -0.5, 3.0])          # 含负底数
   logx = np.emath.log(x)                    # 复数主值
   recovered = np.exp(logx)                  # 用普通 exp 还原
   print("x        =", x)
   print("log(x)   =", logx)
   print("exp(log) =", recovered)
   print("近似还原?", np.allclose(recovered, x))

   # 再验证 power 的复数结果
   print("power(x, 0.5) =", np.emath.power(x, 0.5))   # 负数的 0.5 次方 = 复数开方
   print("sqrt(x)       =", np.emath.sqrt(x))         # 应与上一行一致
   ```
3. **需要观察的现象**：`log(x)` 对负元素给出 `实部+πj` 形式；`exp(log(x))` 还原回原值（`allclose` 为 True）；`power(x,0.5)` 与 `sqrt(x)` 对负元素都给出纯虚主值且一致。
4. **预期结果**：`exp(log)` 数值上还原 `x`（`allclose` 为 `True`，可能有极小浮点误差）；`power([-2,-0.5,3],0.5)` 与 `sqrt([-2,-0.5,3])` 逐元素相等。这印证了 scimath 给出的确实是复数主值，使 \(\exp\circ\log\) 恒等式成立。
5. 想一想：如果把 `np.emath.log` 换成普通 `np.log`，`log(x)` 会变成什么？`exp(log(x))` 还能还原吗？（答案：`np.log` 对负数返回 `nan`，`exp(nan)=nan`，无法还原——这正是 scimath 存在的意义。）

## 6. 本讲小结

- scimath（别名 `np.emath`）与普通 ufunc 的唯一本质差异：**输入越界时返回复数主值，而非 `nan`**。
- 核心范式是两步：① `_fix_*` 前置修正检测越界并按需把输入提升为复数；② 委托给同名普通 ufunc，复数输入自动走其内置的复数主值实现。scimath 不重写任何数学。
- 三个修正函数对应三组判据：`_fix_real_lt_zero`（负实数，服务于 `sqrt/log/log2/log10/logn`）、`_fix_real_abs_gt_1`（绝对值大于 1，服务于 `arcsin/arccos/arctanh`）、`_fix_int_lt_zero`（负数转浮点，仅供 `power` 的指数）。
- `_tocomplex` 选择「最小复数类型」：`float32` 等窄类型升 `complex64`，其余升 `complex128`，且永远复制。
- 关键反直觉性质：**输出 dtype 由数据值决定**——只要存在一个越界元素，整个结果升复数；否则保持实数。
- 结构上仍是 numpy.lib 一贯的「薄再导出层 `scimath.py` + 实现层 `_scimath_impl.py`」与「dispatcher+impl 双函数写法」，并经顶层 `numpy/__init__.py` 以 `emath` 别名暴露。

## 7. 下一步学习建议

- **横向对照类型判定**：本讲反复用到 `isreal`，它来自 `_type_check_impl`。建议接着阅读 u10-l1《类型检查与标量判定》，理解 `isreal`（逐元素）与 `isrealobj`（看 dtype）的区别，以及 `real`/`imag` 的鸭子类型提取。
- **深入 ufunc 的复数实现**：scimath 之所以能「白嫖」复数主值，是因为底层 ufunc 遵循 C99 支割线约定。可阅读 `numpy/_core/src/umath/` 下 `cmath` 相关的 C 实现，了解主值辐角约定。
- **继续本单元**：u11-l2《vectorize 与 gufunc 签名解析》会讲解如何把任意 Python 函数包装成广义 ufunc，与本讲的「包装普通 ufunc」形成对照——一个是改行为，一个是改调用方式。
