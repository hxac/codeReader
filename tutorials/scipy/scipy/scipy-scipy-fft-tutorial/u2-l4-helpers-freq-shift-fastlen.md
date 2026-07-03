# 辅助函数：fftfreq / fftshift / next_fast_len

## 1. 本讲目标

学完本讲后，你应当能够：

- 用 `scipy.fft.fftfreq` / `rfftfreq` 为一段 FFT 结果生成「物理意义正确」的频率坐标轴。
- 解释 `fftshift` / `ifftshift` 在奇数长度与偶数长度下的差异，并能正确地居中显示频谱。
- 理解 FFT 对输入长度敏感的本质（平滑数），会用 `next_fast_len` / `prev_fast_len` 选出最优补零长度来加速。
- 读懂 `_helper.py` 中用 `update_wrapper` + `lru_cache` 把一个 C 函数「嫁接」上 Python docstring 与签名的元编程手法。

本讲只聚焦 `_helper.py` 一个文件，它是公共 API 层中最贴近「日常使用」的一组工具函数——本身不计算任何 FFT，但帮我们把 FFT 的结果变得**可读、可绘、可加速**。

## 2. 前置知识

在进入源码前，先用通俗语言铺垫三个概念。

**（1）FFT 输出的「频率排列」约定。** 回顾 [u2-l1](u2-l1-complex-fft.md)：`fft` 的输出按下标 `k = 0, 1, …, n-1` 排列，但只有前一半（`0..⌊n/2⌋`）是「正频率」，后一半对应「负频率」。也就是说，零频在**开头**而非中间，最高频（Nyquist）在**中间**。这与人画频谱时的直觉（零频居中、左右对称）相反，所以才需要 `fftfreq` 生成坐标、`fftshift` 把零频搬到中间。

**（2）采样间距 d 与物理频率。** 若信号以间距 `d`（单位：秒）采样，则采样率为 \(1/d\)，整段窗口时长为 \(n \cdot d\)。DFT 第 `k` 个 bin 对应的物理频率为 \(k / (n d)\)（单位 Hz）。`fftfreq(n, d)` 干的就是「把下标 `k` 换算成这个物理频率」。

**（3）FFT 为什么挑食：分治与平滑数。** 快速傅里叶变换（Cooley–Tukey 类算法）靠**递归分治**提速：把长度为 `n` 的变换拆成若干小素因子长度的变换。当 `n` 只含小素因子（如 2、3、5、7、11）时，分治最顺畅、最快；当 `n` 是大素数时，分治失效，只能退回 Bluestein 算法（仍保证 \(O(n\log n)\)，但常数大、明显变慢）。只含小素因子的数称为 **n-smooth number（n-平滑数）**。`next_fast_len` 就是「找 ≥ target 的最小平滑数」。

**Python 预备知识：** `functools.update_wrapper`、`functools.lru_cache`、`inspect.signature`。本讲 4.3 会用到，届时再结合源码细讲。

## 3. 本讲源码地图

本讲涉及三个文件：

| 文件 | 作用 | 本讲用到的部分 |
|------|------|----------------|
| [`_helper.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_helper.py) | 全部辅助函数的对外签名 + array API 兼容分支 | `fftfreq`/`rfftfreq`、`fftshift`/`ifftshift`、`next_fast_len`/`prev_fast_len` |
| [`_duccfft/helper.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py) | 计算核心层，提供 C 扩展函数 `good_size` / `prev_good_size` | 第 13 行的 import |
| [`tests/test_helper.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/tests/test_helper.py) | 辅助函数的真实测试断言 | 用作实践的「标准答案」 |

记住 [u1-l2](u1-l2-directory-layout.md) 的四层架构：`_helper.py` 属于**公共 API 层**，它本身不实现算法；`next_fast_len` 的真正计算委托给计算核心层的 C 扩展 `pyduccfft.good_size`。本讲会反复在这两层之间穿梭。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：频率刻度、居中零频、最优补零长度。

### 4.1 频率刻度：fftfreq 与 rfftfreq

#### 4.1.1 概念说明

做完 `X = fft(x)` 后，你拿到的是一串复数 `X[0], X[1], …, X[n-1]`，每个对应一个「频率 bin」。但下标 `k` 本身没有物理单位——它只是个序号。要把频谱画出来或做物理分析，必须知道每个 bin 对应多少赫兹（Hz）。`fftfreq(n, d)` 就是「bin 下标 → 物理频率」的换算器。

`rfftfreq` 是它的「半谱版」：因为实信号的 FFT 满足 Hermitian 对称（见 [u2-l2](u2-l2-real-and-hermitian-fft.md)），`rfft` 只返回非负频率那一半，所以 `rfftfreq` 也只返回非负频率，长度为 `n//2 + 1`。

#### 4.1.2 核心流程

设窗口长度为 `n`、采样间距为 `d`。`fftfreq` 返回的频率数组满足：

\[
f_k =
\begin{cases}
\dfrac{k}{n\,d}, & 0 \le k \le \lfloor n/2 \rfloor \quad\text{(正频率与零频)}\\[6pt]
\dfrac{k-n}{n\,d}, & \lfloor n/2 \rfloor < k \le n-1 \quad\text{(负频率)}
\end{cases}
\]

用列表写得更直观（与源码 docstring 完全一致）：

```
n 为偶数: f = [0, 1, ...,   n/2-1,     -n/2, ..., -1] / (d*n)
n 为奇数: f = [0, 1, ..., (n-1)/2, -(n-1)/2, ..., -1] / (d*n)
```

注意三点：

1. 频率范围被采样率限制在 \([-1/(2d),\, 1/(2d)]\)，这正是**奈奎斯特定理**的体现：最高可分辨频率是采样率 \(1/d\) 的一半。
2. 偶数长度时，正中间那个 bin 是 `-n/2`（即 Nyquist，记为**负**频率）；奇数长度没有单独的 Nyquist bin，正负频率关于 0 严格对称。
3. `d` 只是一个整体缩放因子：`fftfreq(n, d) == fftfreq(n, 1) / d`。

对 `rfftfreq(n, d)`，只取非负部分，长度 `n//2+1`：

```
n 为偶数: f = [0, 1, ...,   n/2-1,     n/2] / (d*n)
n 为奇数: f = [0, 1, ..., (n-1)/2-1, (n-1)/2] / (d*n)
```

docstring 特别提醒：与 `fftfreq` 不同，`rfftfreq` 把 Nyquist 频率分量视为**正**频率（因为半谱里它确实出现在正频率一侧）。

#### 4.1.3 源码精读

[`fftfreq` 的完整定义与 docstring](_helper.py#L149-L199) 在 `_helper.py` 中。先看 docstring 里给出的公式（L159-L160），它就是 4.1.2 里那两行列表的出处。

真正干活的函数体只有三行：

[fftfreq 的实现：xp 命名空间分流](_helper.py#L192-L199)

```python
xp = np if xp is None else xp
# numpy does not yet support the `device` keyword
if hasattr(xp, 'fft') and xp.__name__ != 'numpy':
    return xp.fft.fftfreq(n, d=d, device=device)
if device is not None:
    raise ValueError('device parameter is not supported for input array type')
return np.fft.fftfreq(n, d=d)
```

这几行体现了 scipy.fft 对**数组标准（array API）**的兼容设计（详见 [u6](u6-l1-execute-numpy-xp-dispatch.md) 系列）：

- `xp` 是「数组命名空间」，默认 `None` 时回退到 NumPy。
- 若传入的 `xp`（如 CuPy、PyTorch）自带 `fft.fftfreq`，就**优先调用它**，这样频率数组会和输入数据落在同一个设备（CPU/GPU）上。
- 否则用 `np.fft.fftfreq` 兜底。
- 注意那行注释：NumPy 的 `fftfreq` **暂不支持** `device` 关键字，所以即便传了 `xp=numpy`，也会走到最后的 `np.fft.fftfreq(n, d=d)` 而**忽略** `device`；只有当 `xp` 既无 `fft` 属性、又传了非空 `device` 时才报错。

[`rfftfreq` 的实现](_helper.py#L252-L259) 与 `fftfreq` **结构完全对称**，只是把 `fftfreq` 换成 `rfftfreq`、docstring 公式换成半谱版（L213-L214）。两者都套了 `@xp_capabilities()` 装饰器声明数组标准能力。

> 小结：`fftfreq`/`rfftfreq` 不参与任何 FFT 计算，它们只是「按公式生成一组浮点数」，但正确使用它们是把频谱画对的前提。

#### 4.1.4 代码实践

**实践目标：** 用 `fftfreq` / `rfftfreq` 生成频率轴，并核对公式。

**操作步骤：**

```python
# 示例代码（非项目原有，由本讲编写）
import numpy as np
from scipy import fft

n = 8
d = 0.1                      # 采样间距 0.1 秒，采样率 10 Hz
x = np.array([-2, 8, 6, 4, 1, 0, 3, 5], dtype=float)

freq = fft.fftfreq(n, d=d)   # fft 输出对应的完整频率轴
print("fftfreq:", freq)
# 预期：[ 0.   1.25  2.5   3.75 -4.  -3.75 -2.5  -1.25]

rFreq = fft.rfftfreq(n, d=d) # rfft 半谱对应的非负频率轴
print("rfftfreq:", rFreq)
# 预期：[0.   1.25 2.5  3.75 5.  ]   长度 = 8//2+1 = 5
```

**需要观察的现象：**

- `fftfreq` 长度始终是 `n`，前半为正频、后半为负频，中间（索引 4）是 `-4.0` 即 Nyquist（负）。
- `rfftfreq` 长度是 `n//2 + 1 = 5`，全是非负频率，末尾的 `5.0` 正是采样率的一半（奈奎斯特频率）。

**预期结果：** 上方注释中的数值。可用 `tests/test_helper.py` 的 [`test_definition`](tests/test_helper.py#L510-L526) 交叉验证：它断言 `9 * fft.fftfreq(9)` 等于 `[0,1,2,3,4,-4,-3,-2,-1]`，正是「先正后负」排列的硬证据。

**待本地验证：** 上述输出与 NumPy 版本/平台无关，可直接复现。

#### 4.1.5 小练习与答案

**练习 1：** 若把 `d` 改成 `0.2`（采样率减半），`fftfreq(8, 0.2)` 的结果会如何变化？

**参考答案：** 频率整体缩小为原来的 1/2，因为 `d` 只是缩放因子。结果为 `[0, 0.625, 1.25, 1.875, -2.0, -1.875, -1.25, -0.625]`。奈奎斯特频率也从 5 Hz 降到 2 Hz。

**练习 2：** 为什么 `rfftfreq(7)` 返回的长度是 4 而非 3？

**参考答案：** 长度公式是 `n//2 + 1 = 7//2 + 1 = 4`。奇数长度 `n=7` 时非负频率为 `0,1,2,3`，其中最高 bin `(n-1)/2 = 3`。

---

### 4.2 居中零频：fftshift 与 ifftshift

#### 4.2.1 概念说明

`fft` 把零频放在数组开头，这叫「零频居左」。画频谱时我们更习惯「零频居中」——负频在左、零频在中、正频在右，就像一对待统的对称钟形曲线。`fftshift` 就是干这个搬运的。

`ifftshift` 是 `fftshift` 的**严格逆运算**：对任意 `x`，恒有 `ifftshift(fftshift(x)) == x`。它常用于「把一个居中构造的滤波器 / 窗口还原回 fft 约定的排列」再送进 `ifft`。

#### 4.2.2 核心流程

`fftshift` 沿每条指定轴把数组「循环移位」半个周期，等价于：

\[ \texttt{fftshift}(x) = \texttt{roll}(x,\; \lfloor n/2 \rfloor) \]

`ifftshift` 则向反方向移位，等价于移位 \(\lceil n/2 \rceil\)。由此得到本模块最关键的结论：

- **偶数长度**：\(\lfloor n/2 \rfloor = \lceil n/2 \rceil = n/2\)，两者移位量相同 → `fftshift` 与 `ifftshift` **完全一致**。
- **奇数长度**：\(\lfloor n/2 \rfloor \neq \lceil n/2 \rceil\)，两者相差 **一个样本** → 不等价，但 `ifftshift(fftshift(x)) == x` 仍成立。

用 `n=9`（奇）举例，来自 `test_helper.py` 的 [`test_definition`](tests/test_helper.py#L436-L444)：

```
x          = [0, 1, 2, 3, 4, -4, -3, -2, -1]
fftshift(x)= [-4,-3,-2,-1, 0,  1,  2,  3,  4]     # 零频(0)被推到正中(index 4)
ifftshift 上式 = x                                  # 严格还原
```

`axes` 参数控制对哪些轴做搬移，默认 `None` 表示**所有轴**。这在多维频谱（如 2-D 图像的频域）里特别有用。

#### 4.2.3 源码精读

[`fftshift` 的实现](_helper.py#L307-L312)：

```python
xp = array_namespace(x)
if hasattr(xp, 'fft'):
    return xp.fft.fftshift(x, axes=axes)
x = np.asarray(x)
y = np.fft.fftshift(x, axes=axes)
return xp.asarray(y)
```

与 `fftfreq` 同样的 array API 兼容套路：优先用数组库自带的 `xp.fft.fftshift`；没有则转成 NumPy 算完再 `xp.asarray` 转回原命名空间。`ifftshift` 的实现（[`_helper.py:350-355`](_helper.py#L350-L355)）与之逐行对称，只是调用 `ifftshift`。

注意 docstring 的一句关键提示（L267）：

> `y[0]` 只有在 `len(x)` 为偶数时才是 Nyquist 分量。

这呼应了 4.1 的讨论：偶数长度存在单独的 Nyquist bin，奇数长度不存在。

#### 4.2.4 代码实践

**实践目标：** 验证奇/偶长度下 `fftshift` 与 `ifftshift` 的异同。

**操作步骤：**

```python
# 示例代码（非项目原有，由本讲编写）
import numpy as np
from scipy import fft

x_odd  = np.array([0, 1, 2, 3, 4, -4, -3, -2, -1])      # n=9 奇
x_even = np.array([0, 1, 2, 3, 4, -5, -4, -3, -2, -1])  # n=10 偶

print("奇: fftshift == ifftshift ?",
      np.array_equal(fft.fftshift(x_odd), fft.ifftshift(x_odd)))   # False
print("偶: fftshift == ifftshift ?",
      np.array_equal(fft.fftshift(x_even), fft.ifftshift(x_even))) # True

# 恒等关系（奇偶皆成立）
print("ifftshift(fftshift(x))==x (奇):",
      np.allclose(fft.ifftshift(fft.fftshift(x_odd)), x_odd))      # True
print("ifftshift(fftshift(x))==x (偶):",
      np.allclose(fft.ifftshift(fft.fftshift(x_even)), x_even))    # True
```

**需要观察的现象：**

- 奇数长度：`fftshift` 与 `ifftshift` 结果**不同**（差一个样本）。
- 偶数长度：两者结果**相同**。
- 但无论奇偶，`ifftshift(fftshift(x))` 恒等于 `x`。

**预期结果：** 注释中标注的 `False / True / True / True`。这正是 `tests/test_helper.py` 中 [`test_inverse`](tests/test_helper.py#L446-L449)（对 n=1,4,9,100,211 都断言 `ifftshift(fftshift(x))==x`）所验证的不变量。

**待本地验证：** 直接可复现，不依赖平台。

#### 4.2.5 小练习与答案

**练习 1：** 解释为什么对偶数长度 `fftshift` 与 `ifftshift` 相同，而对奇数长度不同。

**参考答案：** 二者的移位量分别为 \(\lfloor n/2 \rfloor\) 与 \(\lceil n/2 \rceil\)。偶数时二者相等（都为 `n/2`），故结果一致；奇数时相差 1，故结果不同。

**练习 2：** 你想先在「零频居中」的坐标系里设计一个低通滤波器 `H`，再用 `ifft` 变回时域。应先用 `fftshift` 还是 `ifftshift` 把 `H` 转回 fft 约定的「零频居左」排列？

**参考答案：** 用 `ifftshift`。因为 `ifftshift` 是 `fftshift` 的逆：`fftshift` 把「居左」变「居中」，`ifftshift` 把「居中」变「居左」，正是 `ifft` 期望的输入排列。

---

### 4.3 最优补零长度：next_fast_len 与 prev_fast_len

#### 4.3.1 概念说明

如 [前置知识](#2-前置知识) 所述，FFT 在「平滑数」长度上最快，在大素数长度上明显变慢。但实际数据长度往往不受我们控制——比如某段语音恰好 93059 个采样点（93059 是素数）。

两种应对策略：

- **向后补零**：把数据末尾补 0 到一个略大的平滑数 `n'`，再算 `fft(x, n')`。补零不改变信号的有效频率内容（只是更密的频率插值），却换来大幅加速。`next_fast_len(target)` 返回「≥ target 的最小平滑数」，告诉你该补到多长。
- **向前截断**：丢弃少量尾部样本到一个略小的平滑数，适合「宁可损失一点数据也要最快」。`prev_fast_len(target)` 返回「≤ target 的最大平滑数」。

> 注意：补零/截断改变了数据长度，频率分辨率会随之改变（见 4.1 的公式，`n` 变了频率刻度也变）。所以补零后**必须用新的 `n'`** 去 `fftfreq`。

`next_fast_len` 的 docstring 给出了一个真实测过的例子（来自 [_helper.py#L48-L67](_helper.py#L48-L67)）：素数 93059 直接 FFT 需 11.4 ms；补零到 `next_fast_len(93059, real=True)=93312` 后只要 1.6 ms（快 7.3 倍）；而图省事补到下一个 2 的幂 131072 反而要 3.0 ms。**结论：最优长度不是 2 的幂，而是「最小的平滑数」。**

#### 4.3.2 核心流程

`real` 参数决定「平滑」的标准：

- `real=False`（复变换 `fft`）：允许的素因子为 2、3、5、7、11（即 11-smooth）。
- `real=True`（实变换 `rfft`/`hfft`）：允许的素因子为 2、3、5（即 5-smooth，更严格，因为实变换内核支持的基数更少）。

`prev_fast_len` 的 docstring（[L115-L116](_helper.py#L115-L116)）也明确写了同样的基数假设。

`tests/test_helper.py` 提供了可核对的「标准答案」。比如 [`testnext_fast_len_small`](tests/test_helper.py#L78-L84)：

```
{7: 8, 17: 18, 1021: 1024, 1536: 1536, ...}   # real=True
```

即 `next_fast_len(7, real=True)==8`、`next_fast_len(17, real=True)==18`、`next_fast_len(1021, real=True)==1024`。`7` 不是 5-smooth，故向上找到 `8=2³`；`17` 同理找到 `18=2·3²`。而 `1021`（素数）找到 `1024=2¹⁰`。

#### 4.3.3 源码精读：把 C 函数嫁接上 Python docstring

这是本讲最有「元编程味道」的一段。先看 [`_helper.py:14-70`](_helper.py#L14-L70)：这是一个名为 `next_fast_len` 的**纯 docstring 占位函数**，函数体只有一句 `pass`——它根本不计算任何东西，只是为了承载那段详尽的 docstring 和签名。

真正的「调包」发生在 [`_helper.py:75-79`](_helper.py#L75-L79)：

```python
_sig = inspect.signature(next_fast_len)
next_fast_len = update_wrapper(lru_cache(_helper.good_size), next_fast_len)
next_fast_len = xp_capabilities(out_of_scope=True)(next_fast_len)
next_fast_len.__wrapped__ = _helper.good_size
next_fast_len.__signature__ = _sig
```

逐行拆解这段「狸猫换太子」：

1. `_sig = inspect.signature(next_fast_len)`：先把占位函数的签名 `(target, real=False)` 存下来。
2. `lru_cache(_helper.good_size)`：把真正的 C 函数 `good_size` 包上一层 `lru_cache`（默认 `maxsize=128`）。`good_size` 是纯函数（相同输入恒返回相同输出），缓存它可避免对常见长度反复调用 C 扩展。注意这里 `lru_cache` 直接以函数为位置参数调用，等价于「无参装饰器直接作用于函数」。
3. `update_wrapper(被包装的C函数, 占位函数)`：把占位函数的 `__name__`、`__doc__`、`__module__` 等元信息**拷贝**到缓存后的 C 函数上。于是最终对象既保有 C 函数的计算能力，又拥有人类可读的 docstring——在 REPL 里 `help(scipy.fft.next_fast_len)` 能看到完整文档。
4. 再套 `@xp_capabilities(out_of_scope=True)`，声明它不参与数组标准分派（`next_fast_len` 只处理 Python int，跟数组库无关，所以 `out_of_scope`）。
5. 最后手动补上 `__wrapped__`（指向原始 C 函数，便于 `inspect.unwrap` 追溯）和 `__signature__`（恢复 `(target, real=False)` 签名，否则 `inspect.signature` 会失效）。

`_helper.good_size` 本身来自计算核心层：见 [`_duccfft/helper.py:13`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L13)：

```python
from .pyduccfft import good_size, prev_good_size
```

即 `good_size` 是 C 扩展 `pyduccfft` 暴露的函数。这与 [u1-l2](u1-l2-directory-layout.md) 的四层架构吻合：算法在 C 里，Python 只负责包装与文档化。

[`prev_fast_len` 的对应包装](_helper.py#L142-L146) 几乎一模一样，只有两处细节差异：

```python
_sig_prev_fast_len = inspect.signature(prev_fast_len)
prev_fast_len = update_wrapper(lru_cache()(_helper.prev_good_size), prev_fast_len)
...
```

注意 `lru_cache()(_helper.prev_good_size)` 用的是「先调用 `lru_cache()` 拿装饰器，再装饰函数」的写法，而 `next_fast_len` 用的是「`lru_cache(func)`」的简写。两者**效果完全相同**（都得到默认 `maxsize=128` 的缓存），只是风格不同——读源码时不要被这点差异迷惑。

#### 4.3.4 代码实践

**实践目标：** 量化素数长度 FFT 的「慢」，并验证补零到 `next_fast_len` 后的加速。

**操作步骤：**

```python
# 示例代码（非项目原有，由本讲编写）
import time
import numpy as np
from scipy import fft

rng = np.random.default_rng(0)
min_len = 93059                       # 素数长度，FFT 的最坏情况
a = rng.standard_normal(min_len)

# 1) 直接对素数长度做 FFT
t0 = time.perf_counter()
for _ in range(5):
    b1 = fft.fft(a)
t_prime = (time.perf_counter() - t0) / 5
print(f"素数长度 {min_len}: {t_prime*1e3:.2f} ms")

# 2) 补零到 next_fast_len 后做 FFT
fast = fft.next_fast_len(min_len, real=True)
print("next_fast_len(93059, real=True) =", fast)   # 预期 93312

t0 = time.perf_counter()
for _ in range(5):
    b2 = fft.fft(a, fast)
t_fast = (time.perf_counter() - t0) / 5
print(f"补零长度 {fast}: {t_fast*1e3:.2f} ms")
print(f"加速比: {t_prime / t_fast:.1f}x")
```

**需要观察的现象：**

- `next_fast_len(93059, real=True)` 返回 `93312 = 2⁷·3⁶`（只含因子 2、3，故满足 `real=True` 的 5-smooth 约束）。
- 素数长度明显比补零长度慢，加速比通常在数倍量级。

**预期结果：** docstring 记录的参考值是 11.4 ms → 1.6 ms（约 7 倍）。**在你自己机器上的具体毫秒数会不同**（与 CPU、线程数、NumPy/SciPy 版本有关），请以本地实测为准（**待本地验证**），但「补零后更快」这一定性结论稳定成立。

**延伸观察：** 试试 `fft.fft(a, 131072)`（下一个 2 的幂），按 docstring 它会比 93312 更慢（3.0 ms vs 1.6 ms）——可见「2 的幂」并非最优，「最小平滑数」才是。

#### 4.3.5 小练习与答案

**练习 1：** 不运行代码，推断 `next_fast_len(11, real=False)` 与 `next_fast_len(11, real=True)` 的值。

**参考答案：** `real=False` 时 11 自身就是 11-smooth（11≤11），故返回 `11`（与 [`test_keyword_args`](tests/test_helper.py#L123-L125) 中 `next_fast_len(target=7, real=False)==7` 同理）；`real=True` 时只允许 2、3、5，11 不够格，向上找到 `12=2²·3`（与 `testnext_fast_len_small` 中 `{17:18}` 同规律）。

**练习 2：** 为什么 `next_fast_len` 要用 `lru_cache` 包装，而 `fftfreq` 不用？

**参考答案：** `good_size` 是纯函数且会被高频反复查询（每次带 `n` 的 FFT 内部都可能触发长度检查），缓存能显著省下重复的 C 调用开销；`fftfreq` 则只是按公式一次性生成数组、且结果依赖 `n,d,xp,device` 多个参数，调用频次低、收益小，故无需缓存。

---

## 5. 综合实践

把三个模块串起来：**生成一段含已知频率的信号，绘制居中频谱，并比较「素数长度 vs 最优补零长度」的耗时**。

```python
# 示例代码（非项目原有，由本讲编写，建议在 Jupyter 或脚本中运行）
import time
import numpy as np
import matplotlib.pyplot as plt
from scipy import fft

rng = np.random.default_rng(42)
N_target = 93059                       # 素数长度
d = 1e-3                               # 采样间距 1 ms，采样率 1000 Hz

# 信号：50 Hz 正弦 + 噪声
t = np.arange(N_target) * d
sig = np.sin(2*np.pi*50*t) + 0.5*rng.standard_normal(N_target)

# --- 性能对比 ---
N_fast = fft.next_fast_len(N_target, real=True)
print("next_fast_len:", N_fast, " 加速比见下")

for N, label in [(N_target, "素数"), (N_fast, "平滑数(补零)")]:
    t0 = time.perf_counter()
    for _ in range(5):
        X = fft.rfft(sig, n=N)        # 实信号用 rfft，得到半谱
    print(f"{label:12s} N={N}: {(time.perf_counter()-t0)/5*1e3:.2f} ms")

# --- 居中频谱 ---
freq = fft.rfftfreq(N_fast, d=d)       # 注意用补零后的新长度 N_fast
mag = np.abs(X)

plt.plot(freq, mag)
plt.xlabel("频率 (Hz)"); plt.ylabel("|X|")
plt.title("居中前的 rfft 频谱（零频在最左）")
plt.show()
```

**说明与观察点：**

1. `next_fast_len(N_target, real=True)` 返回 93312；补零后 `rfft` 明显更快（**待本地验证**具体毫秒）。
2. 用 `rfftfreq` 时**必须传 `N_fast`** 而非 `N_target`，因为补零改变了长度与频率刻度。
3. `rfft` 输出已是「非负频率」，零频在最左、Nyquist 在最右；若用 `fft`（全谱）则零频在最左、需额外 `fftshift` 才居中。本例用 `rfft` 故无需 shift。
4. 频谱在 50 Hz 处应出现明显尖峰。

> 想练习 `fftshift` 的话，把 `rfft` 换成 `fft`，频率轴换成 `freq = fft.fftshift(fft.fftfreq(N_fast, d=d))`，幅度换成 `fft.fftshift(np.abs(fft.fft(sig, n=N_fast)))`，即可得到对称居中的全谱图。

## 6. 本讲小结

- `fftfreq(n, d)` / `rfftfreq(n, d)` 把 FFT 的 bin 下标换算成物理频率；前者返回全谱（先正后负），后者返回半谱（全非负，Nyquist 视为正）。
- `fftshift` 把零频从开头搬到中间（循环移位 \(\lfloor n/2\rfloor\)）；`ifftshift` 是其严格逆（移位 \(\lceil n/2\rceil\)）。偶数长度二者相同，奇数长度差一个样本。
- `next_fast_len` / `prev_fast_len` 返回最优补零/截断长度（平滑数），可让素数长度 FFT 大幅提速；最优长度未必是 2 的幂，而是「最小的 n-smooth 数」。
- `_helper.py` 用 `update_wrapper` + `lru_cache` 把 C 扩展 `pyduccfft.good_size` 嫁接上 Python docstring 与签名，是「四层架构」中跨层包装的典型手法。
- 所有 helper 函数都遵循 array API 兼容约定：优先用数组库自带实现，否则回退 NumPy，使它们能在 CuPy/PyTorch 等后端上正确工作。

## 7. 下一步学习建议

- **继续读公共 API 层：** 本讲已扫清 `_helper.py`。建议回头看 [u2-l1](u2-l1-complex-fft.md)～[u2-l3](u2-l3-multidimensional-fft.md) 中 `n` 参数与 `_fix_shape` 的截断/补零行为，你会发现它与本讲的 `next_fast_len` 是天然搭档。
- **进入分派机制：** 4.3 的 `@xp_capabilities` 装饰器在 [u6-l2](u6-l2-xp-capabilities-array-api.md) 会系统讲解；想先理解「函数如何变成可分派多方法」可读 [u4-l1](u4-l1-uarray-dispatch.md)。
- **深入计算核心：** `good_size` 背后的 C 扩展 `pyduccfft` 与 `c2c`/`r2c` 内核在 [u5](u5-l1-ducc-basic-kernels.md) 系列展开；`_duccfft/helper.py` 里的 `_init_nd_shape_and_axes`、`_fix_shape`、`_normalization` 则是 [u5-l2](u5-l2-input-preprocessing.md) 的主题。
