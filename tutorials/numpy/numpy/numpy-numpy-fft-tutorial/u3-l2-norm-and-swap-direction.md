# norm 归一化与 _swap_direction

## 1. 本讲目标

在上一讲 [u3-l1](./u3-l1-fft-ifft-raw-fft.md) 里，我们已经打通了 `fft`/`ifft` → `_raw_fft` 的主链路，但当时把归一化系数 `fct` 当成一个黑盒：只知道它被传给了 C++ 后端 ufunc，却没看它怎么算出来的。本讲专门拆开这个黑盒。

学完本讲，你应当能够：

- 说清 `backward` / `ortho` / `forward` 三种 `norm` 模式各自的缩放规则与数学含义。
- 看懂 `_raw_fft` 里那三路 `if/elif` 是如何把一个字符串（`norm`）变成一个标量（`fct`）的。
- 解释 `_swap_direction` 配合 `_SWAP_DIRECTION_MAP` 为什么要在**反向变换**时交换方向。
- 弄明白 `real_dtype = result_type(a.real.dtype, 1.0)` 这行是如何为 `fct` 的计算兜底精度的。

本讲只聚焦「归一化」这一件事，`out` 参数、`rfft`/`irfft` 的 Hermitian 减半等话题留给后续讲义。

## 2. 前置知识

阅读本讲前，建议先掌握：

- **DFT/IDFT 数学约定**（见 [u2-l1](./u2-l1-dft-math-conventions.md)）：正变换 `fft` 指数取负号、默认不归一化；反变换 `ifft` 指数取正号、默认带 \(1/n\) 归一化；由此 `ifft(fft(a)) ≈ a`。
- **`_raw_fft` 统一入口**（见 [u3-l1](./u3-l1-fft-ifft-raw-fft.md)）：所有一维变换都走它，用 `(is_real, is_forward)` 两个布尔选定后端 ufunc。
- 三个 NumPy 函数：`result_type`（按类型提升规则求结果 dtype）、`reciprocal`（求倒数 \(1/x\)）、`sqrt`（求平方根）。

几个本讲反复用到的术语：

- **归一化（normalization）**：在变换结果上乘一个标量，使正、反两个变换成为严格的互逆操作。
- **`norm` 模式的语义**：参数文档原话是「指示 forward/backward 这一对变换中**哪一个方向被缩放**、以及用什么因子缩放」。这句话是理解本讲全部代码的钥匙。
- **`fct`**：代码里归一化系数的变量名，它是一个**乘性**因子（因为 C++ ufunc 是拿它去**乘**结果，而不是除）。

## 3. 本讲源码地图

本讲几乎全部内容都集中在 [_pocketfft.py](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py) 这一个文件里，测试依据来自 `tests/test_pocketfft.py`。

| 文件 | 关键位置 | 作用 |
| --- | --- | --- |
| `_pocketfft.py` | `_raw_fft`（L58–L101） | 计算 `fct` 并把它交给后端 ufunc |
| `_pocketfft.py` | `_SWAP_DIRECTION_MAP`（L104–L105）+ `_swap_direction`（L108–L113） | 反向变换时交换 `norm` 方向 |
| `_pocketfft.py` | `hfft`（L624–L629）/ `ihfft`（L696–L701） | `_swap_direction` 的第二处使用点 |
| `tests/test_pocketfft.py` | `test_ifft`（L186–L195）、`test_all_1d_norm_preserving`（L411–L428） | 用断言验证三种 `norm` 的正确性 |

## 4. 核心概念与源码讲解

### 4.1 norm 三种归一化模式与 fct 计算

#### 4.1.1 概念说明

`fft`、`ifft` 以及所有多维/实数变体都有一个 `norm` 参数，取值为字符串 `"backward"`（默认）、`"ortho"`、`"forward"` 三选一（外加 `None` 等价于 `"backward"`）。

这三个模式描述的是「正反变换这一对里，谁被缩放、缩放多少」。对照 [u2-l1](./u2-l1-dft-math-conventions.md) 的数学约定，可以得到下表（设变换长度为 \(n\)）：

| `norm` | 正变换 `fft` 的缩放 | 反变换 `ifft` 的缩放 | 直觉 |
| --- | --- | --- | --- |
| `"backward"`（默认） | 不缩放（×1） | × \(1/n\) | 「backward」被缩放，即反变换带 \(1/n\) |
| `"ortho"` | × \(1/\sqrt{n}\) | × \(1/\sqrt{n}\) | 两端对称，正交归一化 |
| `"forward"` | × \(1/n\) | 不缩放（×1） | 「forward」被缩放，即正变换带 \(1/n\) |

无论哪种模式，正反变换都互为严格逆：`ifft(fft(a, norm=X), norm=X) ≈ a`。区别只在于这个 \(1/n\)（或 \(1/\sqrt{n}\)）的「分量」分配在正变换还是反变换上。

#### 4.1.2 核心流程

由于 C++ 后端 ufunc 是拿 `fct` 去**乘**结果（见 [u3-l1](./u3-l1-fft-ifft-raw-fft.md) 末尾的 `ufunc(a, fct, ...)`），所以 `fct` 必须是「乘性」形式，即把上表里的缩放因子直接写成倒数以外的乘数：

- 不缩放 → `fct = 1`
- × \(1/\sqrt{n}\) → `fct = reciprocal(sqrt(n))`，即 \(1/\sqrt{n}\)
- × \(1/n\) → `fct = reciprocal(n)`，即 \(1/n\)

`_raw_fft` 用一个三路分支把 `norm` 字符串映射成 `fct` 标量，伪代码如下：

```text
读入 norm（已是「正变换视角」的方向，反向变换会先被 _swap_direction 改写）
若 norm 是 None 或 "backward":  fct = 1
若 norm == "ortho":             fct = 1 / sqrt(n)
若 norm == "forward":           fct = 1 / n
否则:                            抛 ValueError
把 fct 连同 a 一起交给后端 ufunc
```

注意一个细节：这个分支是**以「正变换（forward）视角」**写出来的——`"backward"` 在这里被解释成「正变换不缩放」。等到反变换进来时，方向要对调，这正是下一节 `_swap_direction` 要做的事。

#### 4.1.3 源码精读

归一化的全部计算落在 `_raw_fft` 里。先看 `n` 的合法性检查与 `fct` 三路分支：

[归一化系数 fct 的计算（`_pocketfft.py` L62-L76）](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L62-L76) —— 注释说明这里要算归一化因子、并传入 dtype 以避免精度损失；随后三路分支把 `norm` 映射成 `fct`：

```python
# Calculate the normalization factor, passing in the array dtype to
# avoid precision loss in the possible sqrt or reciprocal.
...
real_dtype = result_type(a.real.dtype, 1.0)
if norm is None or norm == "backward":
    fct = 1
elif norm == "ortho":
    fct = reciprocal(sqrt(n, dtype=real_dtype))
elif norm == "forward":
    fct = reciprocal(n, dtype=real_dtype)
else:
    raise ValueError(f'Invalid norm value {norm}; should be "backward",'
                     '"ortho" or "forward".')
```

要点逐条对照：

- `norm is None or norm == "backward"`：`None` 与默认值 `"backward"` 走同一条路，`fct = 1`，正变换不缩放。这解释了实践任务里「为何传 `norm=None` 与 `'backward'` 等价」——它们在源码里就是同一个 `or` 分支。
- `norm == "ortho"`：`fct = reciprocal(sqrt(n, dtype=real_dtype))`，即 \(1/\sqrt{n}\)。
- `norm == "forward"`：`fct = reciprocal(n, dtype=real_dtype)`，即 \(1/n\)。
- 兜底 `else`：非法字符串（如 `"backward "` 带空格、`"Ortho"` 大写）会抛 `ValueError`，错误信息列出三个合法取值。

随后 `fct` 作为第二个实参传给后端 ufunc 做乘法缩放：

[把 fct 交给后端 ufunc（`_pocketfft.py` L101）](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L101) —— `ufunc(a, fct, axes=[(axis,), (), (axis,)], out=out)`，`fct` 就是上面算出来的乘性归一化系数。

`reciprocal`、`result_type`、`sqrt` 三个函数都从 `numpy._core` 导入：

[导入归一化所需的三个函数（`_pocketfft.py` L36-L45）](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L36-L45) —— `reciprocal`、`result_type`、`sqrt` 与 `asarray`、`conjugate`、`empty_like`、`overrides`、`take` 一起从 `numpy._core` 引入。

> 备注：变量在代码里叫 `fct`，但文件顶部有一段注释把它称作 `inv_norm`（「需要除以的那个数的倒数」），并解释了为什么用乘法倒数而不是直接除——这部分留到 4.3 节展开。

#### 4.1.4 代码实践

实践目标：用同一个数组观察三种 `norm` 下 `fft` 输出的缩放比例。

操作步骤（示例代码）：

```python
import numpy as np
a = np.arange(1, 9, dtype=float)          # 长度 n=8 的实信号
F = np.fft.fft(a)                          # 默认 norm="backward"

print(np.allclose(np.fft.fft(a, norm="backward"), F))                 # True
print(np.allclose(np.fft.fft(a, norm="ortho"),    F / np.sqrt(8)))    # True
print(np.allclose(np.fft.fft(a, norm="forward"),  F / 8))             # True
```

需要观察的现象：`ortho` 恰好是默认结果除以 \(\sqrt{8}\)，`forward` 恰好是默认结果除以 \(8\)，与 4.1.2 的公式完全一致。

预期结果：三行打印均为 `True`。

#### 4.1.5 小练习与答案

**练习 1**：若把 `norm="forward"` 的 `fft` 结果再做一次 `norm="forward"` 的 `fft`，整体缩放是多少？

**答案**：`forward` 模式下每次正变换乘 \(1/n\)，做两次共乘 \(1/n^2\)。

**练习 2**：调用 `np.fft.fft(a, norm="Backward")`（首字母大写）会发生什么？

**答案**：落入 `else` 分支，抛 `ValueError: Invalid norm value Backward; ...`。`norm` 字符串是大小写敏感的。

---

### 4.2 _swap_direction 与 _SWAP_DIRECTION_MAP

#### 4.2.1 概念说明

4.1 节的 `fct` 分支是**以正变换视角**写出来的：「`backward` 表示正变换不缩放」。但 `_raw_fft` 是 `fft` 和 `ifft` **共用**的入口：

- `fft` 调用时 `is_forward=True`，正变换视角天然成立，分支直接可用。
- `ifft` 调用时 `is_forward=False`。此时「`backward` 表示反变换带 \(1/n\)」，可如果直接拿 `norm="backward"` 去走分支，会得到 `fct=1`（不缩放），这就错了——`ifft` 本该带 \(1/n\)。

解决办法不是为反变换再写一份分支，而是**在反变换进分支前，把 `norm` 的方向标签交换一下**：`"backward" ↔ "forward"`，`"ortho"` 保持不变。交换后，同一套分支就能对正反两个方向都给出正确的 `fct`。这正是 `_swap_direction` 干的事。

`"ortho"` 是这个交换的**不动点**（fixed point），因为 `ortho` 模式正反两端都乘 \(1/\sqrt{n}\)，方向无所谓。

#### 4.2.2 核心流程

`_swap_direction` 的映射表就是 `_SWAP_DIRECTION_MAP`：

```text
"backward"  ->  "forward"
None        ->  "forward"
"ortho"     ->  "ortho"      （不动点）
"forward"   ->  "backward"
其它         ->  抛 ValueError
```

在 `_raw_fft` 里，只有反变换（`is_forward=False`）才需要交换：

```text
if not is_forward:           # ifft / irfft 等反变换
    norm = _swap_direction(norm)   # 把方向标签对调成「正变换视角」
# 之后 norm 走 4.1 的 fct 三路分支
```

把这个交换和 4.1 的分支合起来，就得到完整的 `(变换, norm) -> fct` 决策表：

| 调用 | `is_forward` | 传入 `norm` | 交换后 | `fct` |
| --- | --- | --- | --- | --- |
| `fft(..., norm="backward")` | True | `"backward"` | `"backward"` | \(1\) |
| `fft(..., norm="ortho")` | True | `"ortho"` | `"ortho"` | \(1/\sqrt{n}\) |
| `fft(..., norm="forward")` | True | `"forward"` | `"forward"` | \(1/n\) |
| `ifft(..., norm="backward")` | False | `"backward"` | `"forward"` | \(1/n\) |
| `ifft(..., norm="ortho")` | False | `"ortho"` | `"ortho"` | \(1/\sqrt{n}\) |
| `ifft(..., norm="forward")` | False | `"forward"` | `"backward"` | \(1\) |

对照 4.1.1 的语义表，每一行都对得上。

#### 4.2.3 源码精读

交换发生在 `_raw_fft` 计算归一化因子之前：

[反变换先交换方向（`_pocketfft.py` L64-L65）](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L64-L65) —— `if not is_forward: norm = _swap_direction(norm)`，把反变换的 `norm` 改写成「正变换视角」，这样下面的三路分支对正反方向都通用。

映射表与交换函数本身：

[_SWAP_DIRECTION_MAP 与 _swap_direction（`_pocketfft.py` L104-L113）](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L104-L113) —— 字典给出 `backward↔forward`、`None→forward`、`ortho→ortho` 的映射；`_swap_direction` 用 `try/except KeyError` 把非法值转成与 `fct` 分支一致的 `ValueError`：

```python
_SWAP_DIRECTION_MAP = {"backward": "forward", None: "forward",
                       "ortho": "ortho", "forward": "backward"}


def _swap_direction(norm):
    try:
        return _SWAP_DIRECTION_MAP[norm]
    except KeyError:
        raise ValueError(f'Invalid norm value {norm}; should be "backward", '
                         '"ortho" or "forward".') from None
```

注意 `None` 也被显式列入字典（映射到 `"forward"`），所以 `_swap_direction(None)` 不会报错。

**第二处使用点：`hfft` / `ihfft`。** 这对变换「复用」`irfft`/`rfft` 实现，但概念方向相反，所以也要交换：

[`hfft` 用 _swap_direction 改写方向后委托 irfft（`_pocketfft.py` L624-L629）](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L624-L629) —— `hfft` 概念上是「正变换」，却用 `irfft`（反变换算子）实现，于是先 `new_norm = _swap_direction(norm)` 再传给 `irfft`，让缩放分量落在正确的一侧。

[`ihfft` 同理委托 rfft（`_pocketfft.py` L696-L701）](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L696-L701) —— `ihfft` 概念上是「反变换」，却用 `rfft`（正变换算子）实现，同样先交换方向。

可见 `_swap_direction` 是一个**通用的「方向对调」工具**：凡是「用相反方向的算子来实现某个变换」的场合，都要靠它把 `norm` 语义拨正。

#### 4.2.4 代码实践

实践目标：验证任意 `norm` 下 `ifft(fft(a, norm=X), norm=X) ≈ a`，并确认 `None` 与 `"backward"` 等价。

操作步骤（示例代码）：

```python
import numpy as np
a = np.arange(1, 9, dtype=float) + 1j * np.arange(8, 0, -1)
for X in (None, "backward", "ortho", "forward"):
    rt = np.fft.ifft(np.fft.fft(a, norm=X), norm=X)
    print(X, np.allclose(rt, a))
# 顺手验证 None 与 "backward" 完全一致
print(np.array_equal(np.fft.fft(a, norm=None),
                     np.fft.fft(a, norm="backward")))
```

需要观察的现象：四种 `norm` 的往返都还原出 `a`；`None` 与 `"backward"` 的输出逐元素相等。

预期结果：四行 `True`，最后一行 `True`。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接在 `_raw_fft` 里写 `if is_forward: ... else: ...` 两套 `fct` 分支，而要引入 `_swap_direction`？

**答案**：交换方向后只需维护**一套** `fct` 分支，正反方向共用，减少重复、避免两套分支不一致的 bug。`hfft`/`ihfft` 还能复用同一个交换函数。

**练习 2**：`_swap_direction("ortho")` 返回什么？为什么？

**答案**：返回 `"ortho"`。因为 `ortho` 模式正反两端都乘 \(1/\sqrt{n}\)，方向对称，是交换的不动点。

---

### 4.3 real_dtype 与 reciprocal/sqrt 的精度保护

#### 4.3.1 概念说明

4.1 节的 `fct` 分支里，`sqrt` 和 `reciprocal` 都带了一个 `dtype=real_dtype` 参数，而 `real_dtype` 来自这行：

```python
real_dtype = result_type(a.real.dtype, 1.0)
```

它解决两个问题：

1. **精度保护**：归一化因子是一个标量，后续要作用到结果数组的每一个元素上。如果这个因子本身精度不够（比如算成了 `float32`），误差会被放大到整条变换结果。所以要保证 `sqrt(n)`、`reciprocal(n)` 至少在 double 精度下计算。
2. **复用为输出 dtype**：`real_dtype` 同时也是 `irfft`（复数→实数）自动分配输出数组时用的 dtype。

要理解这行，先看两个构件：

- **`a.real.dtype`**：取输入数组「实部」的 dtype。对实数数组就是它本身的 dtype；对复数数组则是底层浮点 dtype（`complex64` → `float32`，`complex128` → `float64`）。
- **`result_type(..., 1.0)`**：按 NumPy 类型提升规则求公共 dtype。Python 标量 `1.0` 被当作 `float64` 参与提升，于是任何低于 `float64` 的浮点类型都会被提升到 `float64`。

> 文件顶部还有一段注释解释了为什么用「乘性倒数」`inv_norm`（即 `fct`）而不是直接除：避免零长度轴时的除零问题。原文见 [归一化系数设计注释（`_pocketfft.py` L54-L57）](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L54-L57)。其要点是：C++ ufunc 拿 `fct` 做**乘法**，所以 `fct` 必须是「该除数的倒数」；用 `reciprocal` 在 Python 侧一次性算好这个倒数（并指定精度），既比在 ufunc 内层循环里逐元素做除法更快，也把潜在的除零行为收敛成单个标量上的 IEEE 754 结果（`reciprocal(0.0)` → `inf`），而非内层循环里到处触发。注意：当前 `_raw_fft` 开头的 `if n < 1: raise ValueError`（[L59-L60](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L59-L60)）已经把 `n=0` 的零长度轴挡在门外，所以这段注释更多是在交代「乘性倒数 + 显式 dtype」这套设计的初衷。

#### 4.3.2 核心流程

精度保护的完整链路：

```text
a.real.dtype                       # 取实部 dtype（complex64 -> float32）
result_type(a.real.dtype, 1.0)     # 与 1.0(float64) 提升 -> 至少 float64
        = real_dtype
reciprocal(sqrt(n, dtype=real_dtype))   # 在 real_dtype 精度下算 1/sqrt(n)
reciprocal(n,      dtype=real_dtype)    # 在 real_dtype 精度下算 1/n
```

常见输入的推导结果：

| 输入 `a.dtype` | `a.real.dtype` | `real_dtype`（与 `1.0` 提升后） |
| --- | --- | --- |
| `float32` | `float32` | `float64` |
| `float64` | `float64` | `float64` |
| `complex64` | `float32` | `float64` |
| `complex128` | `float64` | `float64` |

这正是 [u2-l1](./u2-l1-dft-math-conventions.md) 提到的「Type Promotion」在归一化这一步的落点：`float32`/`complex64` 一律提升到 double 参与计算，避免精度损失。对 `longdouble`/`float128` 这类扩展精度类型，`result_type(float128, 1.0)` 仍是 `float128`，扩展精度被保留——这就是用 `result_type(a.real.dtype, 1.0)` 而非硬编码 `float64` 的好处。

此外 `real_dtype` 还被复用为 `irfft` 的输出 dtype：

[`irfft` 自动分配输出时用 real_dtype（`_pocketfft.py` L90-L96）](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L90-L96) —— 当 `out is None` 且是 `irfft`（复入实出）时，`out_dtype = real_dtype`；其余变换输出复数，`out_dtype = result_type(a.dtype, 1j)`。

#### 4.3.3 源码精读

[推导 real_dtype 并用它保护 fct 精度（`_pocketfft.py` L67-L73）](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L67-L73) —— 一行算出 `real_dtype`，随后 `ortho` 与 `forward` 两个分支都把 `real_dtype` 透传给 `sqrt`/`reciprocal`：

```python
real_dtype = result_type(a.real.dtype, 1.0)
if norm is None or norm == "backward":
    fct = 1
elif norm == "ortho":
    fct = reciprocal(sqrt(n, dtype=real_dtype))
elif norm == "forward":
    fct = reciprocal(n, dtype=real_dtype)
```

注意 `"backward"` 分支 `fct = 1` 是 Python 整数 `1`，不涉及精度问题；只有 `ortho`/`forward` 才需要算倒数，才用得上 `real_dtype`。

三个函数的导入位置：[导入 reciprocal/result_type/sqrt（`_pocketfft.py` L36-L45）](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L36-L45)。

#### 4.3.4 代码实践

实践目标：观察 `float32` 输入下，`fft` 输出被提升为 `complex128`（即 `real_dtype` 推到 `float64` 的连带效应）。

操作步骤（示例代码）：

```python
import numpy as np
a32 = np.arange(8, dtype=np.float32)
print(np.fft.fft(a32).dtype)          # complex128（而非 complex64）
print(np.fft.rfft(a32).dtype)         # complex128
print(np.fft.irfft(np.fft.rfft(a32)).dtype)  # float64（irfft 输出用 real_dtype）
```

需要观察的现象：`float32` 输入并未得到 `complex64`/`float32` 输出，而是被提升到 double 族。

预期结果：依次为 `complex128`、`complex128`、`float64`。

> 进一步（源码阅读型）：在本地副本里把 [_pocketfft.py L67](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L67) 临时改成 `real_dtype = a.real.dtype`（去掉与 `1.0` 的提升），重新编译后重跑上面的打印，对比 dtype 是否变成 `complex64`/`float32`——以此直观验证 `1.0` 在类型提升里的作用。（此步涉及改源码并重编译，仅建议在隔离环境中尝试；本讲义不假定你已运行。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `real_dtype` 用 `a.real.dtype` 而不是 `a.dtype`？

**答案**：`a.dtype` 对复数数组是 `complex64`/`complex128`，不能直接作为 `sqrt`/`reciprocal`（它们要返回实数浮点）的输出 dtype。取 `a.real.dtype` 拿到底层实浮点 dtype（`complex64` → `float32`）。

**练习 2**：`result_type(np.dtype("float32"), 1.0)` 等于什么？为什么？

**答案**：等于 `float64`。因为 Python `1.0` 被当作 `float64`，按提升规则 `float32` 与 `float64` 的公共类型是 `float64`。这正是 `float32` 输入也得到 double 精度 `fct` 的原因。

---

## 5. 综合实践

把本讲三块内容串起来，完成下面这个综合任务。

**任务**：用一个固定的复信号，系统地验证三种 `norm` 的缩放规则、往返恒等性，以及 `None` 与 `"backward"` 的等价性。

操作步骤（示例代码）：

```python
import numpy as np

rng = np.random.default_rng(0)
a = rng.standard_normal(16) + 1j * rng.standard_normal(16)
n = a.size
F = np.fft.fft(a)                       # 默认 backward

# 1) 三种 norm 的缩放关系（对应 4.1）
assert np.allclose(np.fft.fft(a, norm="backward"), F)
assert np.allclose(np.fft.fft(a, norm="ortho"),    F / np.sqrt(n))
assert np.allclose(np.fft.fft(a, norm="forward"),  F / n)

# 2) ortho 往返还原（对应 4.2，也是讲义要求验证的核心式）
assert np.allclose(np.fft.ifft(np.fft.fft(a, norm="ortho"), norm="ortho"), a)

# 3) None 与 "backward" 完全一致（对应 4.1.3 的 or 分支）
assert np.array_equal(np.fft.fft(a, norm=None), np.fft.fft(a, norm="backward"))

print("all assertions passed")
```

需要观察的现象与预期结果：所有 `assert` 均通过，最终打印 `all assertions passed`。

**解释「为何 `norm=None` 与 `'backward'` 等价」**：在 [L68-L69](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L68-L69) 的分支条件 `if norm is None or norm == "backward"` 里，二者被同一个 `or` 捕获，都得到 `fct = 1`；同时 `_SWAP_DIRECTION_MAP` 里 `None` 与 `"backward"` 都映射到 `"forward"`（[L104-L105](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L104-L105)），所以反变换行为也完全一致。函数签名里 `norm=None` 只是「未传参」的哨兵，语义上就是默认值 `"backward"`。

**测试依据对照**：上面的「往返还原」断言与官方测试 `test_ifft`（[tests/test_pocketfft.py L186-L195](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/tests/test_pocketfft.py#L186-L195)）思路一致——它用 `@pytest.mark.parametrize('norm', (None, 'backward', 'ortho', 'forward'))` 对四种取值逐一断言 `ifft(fft(x, norm=norm), norm=norm) ≈ x`。而 `test_fft2`（[L203-L206](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/tests/test_pocketfft.py#L203-L206)）则直接断言 `fft2(x, norm="ortho") ≈ fft2(x)/sqrt(30*20)`、`norm="forward"` 对应除以 `30.*20.`，与本讲 4.1 的缩放公式一一对应。若想跑官方套件确认，可执行 `numpy.fft.test()`（模块级测试入口）。

## 6. 本讲小结

- `norm` 三种模式的语义：`"backward"`（默认）让反变换带 \(1/n\)、`"ortho"` 让正反两端都带 \(1/\sqrt{n}\)、`"forward"` 让正变换带 \(1/n\)。
- `_raw_fft` 用一个三路 `if/elif` 把 `norm` 映射成乘性系数 `fct`：`backward/None → 1`、`ortho → reciprocal(sqrt(n))`、`forward → reciprocal(n)`。
- 该分支以「正变换视角」书写；反变换（`is_forward=False`）进分支前先用 `_swap_direction` 把 `"backward"↔"forward"` 对调，`"ortho"` 为不动点，于是同一套分支对正反方向都正确。
- `_swap_direction` + `_SWAP_DIRECTION_MAP` 是通用「方向对调」工具，除了 `_raw_fft`，`hfft`/`ihfft` 也用它来修正「用相反方向算子实现」带来的方向错位。
- `real_dtype = result_type(a.real.dtype, 1.0)` 把 `fct` 的计算精度兜到至少 `float64`，避免 `float32`/`complex64` 输入时归一化因子先丢精度；同一变量还被 `irfft` 复用为输出 dtype。
- 用乘性倒数 `fct`（而非传 `n` 让 ufunc 做除法）既快（乘法优于除法）又规避了零长度轴的逐元素除零问题，文件顶部注释对此有说明。

## 7. 下一步学习建议

- 本讲的 `real_dtype` 还连接着「输出 dtype 决策」，这正是下一讲 [u3-l3 out 参数、类型提升与输出 dtype](./u3-l3-out-arg-and-dtype.md) 的主题，建议紧接着读，把 `out_dtype = real_dtype` 与 `out_dtype = result_type(a.dtype, 1j)` 这一对看全。
- 想看 `norm` 在多维场景的体现，可跳到 [u4-l1 fftn/ifftn 与 fft2/ifft2 多维变换](./u4-l1-fftn-fft2-multidim.md)，体会 `_raw_fftnd` 逐轴复用 `_raw_fft` 时同一套 `fct` 机制如何沿每个轴独立生效。
- 若对「`fct` 如何被 C++ ufunc 消费」感兴趣，可提前翻到 [u5-l1 _pocketfft_umath gufunc 注册](./u5-l1-gufunc-registration.md)，看 signature `"(n),()->(m)"` 里那个标量参数位就是 `fct`。
