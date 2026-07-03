# 复数一维 FFT：fft / ifft 与 norm、n、axis

## 1. 本讲目标

上一篇你已经跑通了第一次 `scipy.fft.fft` 调用，并验证了 `ifft(fft(x)) ≈ x`。本讲要慢下来，把 `fft` / `ifft` 这对最基础的复数一维变换**逐个参数**讲透。读完本讲，你应该能够：

1. 说清楚 `fft(x, n, axis, norm, overwrite_x, workers, *, plan)` 这七个参数各自的含义与默认值。
2. 理解参数 `n` 如何对输入做**截断**或**补零**，以及 `axis` 如何选择变换轴。
3. 彻底掌握三种归一化模式 `norm`（`backward` / `ortho` / `forward`）的数学含义，并能从源码层面解释它们是如何被映射到计算核心的。
4. 跟踪一次 `fft(x)` 调用如何从「公共签名」逐层穿透到 ducc 计算核心 `pfft.c2c`，理解 `overwrite_x`、`workers`、`plan` 在这条链路上的真实作用。

本讲只聚焦**复数一维**变换（`fft` / `ifft`）。实变换 `rfft`、多维变换 `fftn`、以及 `workers` 的多线程细节，会在后续讲义（u2-l2、u2-l3、u5-l3）展开。

---

## 2. 前置知识

### 2.1 DFT 与 FFT 的定义

一段长度为 \(N\) 的复信号 \(x[0..N-1]\) 的**离散傅里叶变换（DFT）**为：

\[
X[k] = \sum_{n=0}^{N-1} x[n] \, e^{-2\pi i \, k n / N}, \quad k = 0, 1, \dots, N-1
\]

直接按定义算是 \(O(N^2)\)，而**快速傅里叶变换（FFT）**把它降到 \(O(N \log N)\)。`scipy.fft.fft` 调用的就是这类快速算法。

注意上式**没有任何归一化系数**。这种「正变换不归一化、反变换除以 \(N\)」的约定，正是 `scipy.fft` 的默认模式 `norm="backward"`。本讲 4.3 会专门讨论归一化。

### 2.2 一维调用只需关心一个轴

`fft` 是**一维**变换：即使你传入一个多维数组，它也只沿着你指定的**某一个轴**做 FFT，其余轴原样保留。这个轴由参数 `axis` 指定，默认是最后一个轴（`axis=-1`）。

### 2.3 前置讲义承接

- u1-l1 / u1-l2 已建立「公共 API 层 → uarray 分派 → 后端 → ducc 核心」的四层心智模型。本讲会沿着这条链路往下走到计算核心。
- u1-l3 已演示过 8 点复指数信号的 `fft` 结果在索引 1 处出现峰值 8。本讲解释**为什么**是这个结果。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [`scipy/fft/_basic.py`](_basic.py) | **公共 API 层**。定义 `fft`/`ifft` 等函数的签名、docstring，并用 `@_dispatch` 装饰器把它们变成可分派的多方法。函数体本身只有一行 `return (Dispatchable(x, np.ndarray),)`。 |
| [`scipy/fft/_basic_backend.py`](_basic_backend.py) | **后端层**。`_ScipyBackend` 在这里按方法名查找到真正的实现：`fft` / `ifft` 调用 `_execute_1D`，它负责把数组路由到 numpy 或 xp 命名空间。 |
| [`scipy/fft/_duccfft/basic.py`](_duccfft/basic.py) | **计算核心（Python 封装）**。`c2c` 是复数一维变换的统一内核，`fft`/`ifft` 用 `functools.partial` 从它派生，最终调用 C 扩展 `pyduccfft`（别名 `pfft`）。 |
| [`scipy/fft/_duccfft/helper.py`](_duccfft/helper.py) | **预处理工具**。`_asfarray`（浮点化）、`_fix_shape`（截断/补零）、`_normalization`（norm 映射）、`_workers`（解析并行数）都在这里。 |
| [`scipy/fft/tests/test_basic.py`](tests/test_basic.py) | 测试。`test_fft` / `test_ifft` 精确验证了三种 `norm` 的缩放关系，是本讲实践的权威参照。 |

阅读建议：先看 `_basic.py` 的签名（4.1），再跳到 `helper.py` 看 `n` 与 `norm` 的处理（4.2、4.3），最后回到 `_basic_backend.py` + `basic.py` 看完整调用链（4.4）。

---

## 4. 核心概念与源码讲解

### 4.1 fft / ifft 的公共签名：参数全景与 uarray 分派

#### 4.1.1 概念说明

`scipy.fft.fft` 和 `ifft` 是一对互逆的复数一维变换。它们的**公共签名**完全对称，七个参数的含义如下：

| 参数 | 默认值 | 作用 |
|------|--------|------|
| `x` | （必填） | 输入数组，可以是实数或复数。 |
| `n` | `None` | 输出变换轴的长度。比输入短则截断，比输入长则补零。 |
| `axis` | `-1` | 沿哪一根轴做变换。 |
| `norm` | `None`（等价 `"backward"`） | 归一化模式：`"backward"` / `"ortho"` / `"forward"`。 |
| `overwrite_x` | `False` | 是否允许破坏 `x` 的内存以换取效率。 |
| `workers` | `None` | 并行 worker 数（仅对多维 `x` 有效）。 |
| `plan` | `None` | 预设计划，**SciPy 当前不使用**，为下游厂商预留。 |

一个关键直觉：**`fft` / `ifft` 的函数体里没有任何「算 FFT」的代码**。它们的全部职责是「声明签名 + 把 `x` 标记为可被替换的 `Dispatchable`」。真正算 FFT 的逻辑在更深处，由 uarray 的分派机制触发。这是 `scipy.fft` 四层架构的体现。

#### 4.1.2 核心流程

公共函数如何被「改装」成一个可分派的多方法：

1. `@_dispatch` 装饰器调用 `generate_multimethod`，把函数注册到 domain `"numpy.scipy.fft"`。
2. 当你调用 `fft(x)` 时，uarray 不是直接执行函数体，而是去**后端**里找名字叫 `fft` 的实现。
3. 函数体 `return (Dispatchable(x, np.ndarray),)` 告诉 uarray：参数 `x` 是「可被后端替换的对象」，它的期望类型是 `np.ndarray`。
4. 默认后端 `_ScipyBackend` 找到 `_basic_backend.fft`，把（可能已被替换的）`x` 传进去，由后者真正计算。

也就是说，`fft` 的函数体更像是**一份「协议声明」**，而不是计算代码。

#### 4.1.3 源码精读

先看公共签名与装饰器（[_basic.py:25-28](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L25-L28)）：上面一层 `@xp_capabilities(allow_dask_compute=True)` 标注数组标准能力（u6-l2 详讲），下面一层 `@_dispatch` 才是把它变成多方法的关键。

`_dispatch` 的实现极其简洁（[_basic.py:18-22](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L18-L22)）：它调用 `generate_multimethod(func, _x_replacer, domain="numpy.scipy.fft")`，把函数 `func` 连同一个「参数替换器」`_x_replacer` 和一个**域**绑在一起。`domain` 字符串就是后端寻址的命名空间。

`_x_replacer` 的职责是：当 uarray 在分派时要把原始 `x` 换成后端版本的 `x`，它需要知道「该换哪个参数」（[_basic.py:7-15](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L7-L15)）。逻辑是：如果 `x` 是按位置参数传的，就替换 `args[0]`；否则替换关键字 `kwargs['x']`。

而 `fft` 的函数体本身只有一行（[_basic.py:168](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L168)）：

```python
return (Dispatchable(x, np.ndarray),)
```

它返回一个**只含一个元素的元组**——这个元素声明「`x` 是一个可分派对象，期望类型为 `np.ndarray`」。`ifft` 的函数体完全一样（[_basic.py:275](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L275)），区别只在签名上方的名字。`ifft` 的签名见 [_basic.py:173-174](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L173-L174)。

#### 4.1.4 代码实践

**实践目标**：亲手确认 `scipy.fft.fft` 是一个「被分派机制包装过」的多方法，而不是普通函数。

**操作步骤**（在 Python REPL 中）：

```python
import scipy.fft, numpy as np
import inspect

# 1) 看签名：七个参数都在
print(inspect.signature(scipy.fft.fft))

# 2) 看 __doc__ 里 norm 的描述（来自 _basic.py 的 docstring）
print(scipy.fft.fft.__doc__[:200])

# 3) 最简单的一次调用：4 点信号
print(scipy.fft.fft([0, 1, 0, 0]))
```

**需要观察的现象**：

- `signature` 应显示 `(x, n=None, axis=-1, norm=None, overwrite_x=False, workers=None, *, plan=None)`。注意 `plan` 在 `*` 之后，是**仅限关键字**参数。
- `fft([0, 1, 0, 0])` 的结果 `[1, -1j, -1, 1j]` 正是单位脉冲在 4 个频率上的 DFT——印证 \(X[k]=\sum x[n]e^{-2\pi i kn/N}\)。

**预期结果**：与 [_basic.py 文档示例](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L100-L109) 给出的「8 点对应频率 `[0,1,2,3,-4,-3,-2,-1]`」的约定一致：索引 0 是零频，正频在前半段，负频在后半段。

> 说明：本实践为本地运行型，上述输出在标准 numpy/scipy 环境下应稳定复现。若结果因浮点误差有极小虚部（如 `1e-17j`），属正常现象。

#### 4.1.5 小练习与答案

**练习 1**：`fft` 的函数体只有 `return (Dispatchable(x, np.ndarray),)`，那真正的 FFT 计算代码在哪？
**答案**：在默认后端 `_ScipyBackend` 里。uarray 根据方法名 `fft` 找到 `_basic_backend.fft`，后者调用 `_execute_1D`，最终落到 `_duccfft.basic.c2c` 与 C 扩展 `pyduccfft`。

**练习 2**：为什么 `plan` 是仅限关键字参数（`*` 之后）？
**答案**：因为它是「为下游厂商预留」的实验性参数，SciPy 当前并不真正使用它。用 `*` 强制关键字传参，可以避免位置参数顺序被未来改动破坏，是一种向后兼容的保护。

---

### 4.2 参数 n 与 axis：截断、补零与变换轴

#### 4.2.1 概念说明

参数 `n` 决定**输出变换轴的长度**。它有两种行为：

- 若 `n` **小于**输入在 `axis` 上的长度：**截断**，只取前 `n` 个元素参与变换。
- 若 `n` **大于**输入长度：**补零**（zero-padding），在末尾补 `n - len` 个 0。
- 若 `n` 为 `None`：使用输入本身在 `axis` 上的长度。

补零是信号处理里的常用技巧：它不会增加信号的真实频率分辨率（那取决于信号时长），但会**插值**出更密集的频谱点，让谱线更平滑，也常用于把长度凑成对 FFT 更友好的「快速长度」（见 u2-l4 的 `next_fast_len`）。

`axis` 则决定沿哪根轴变换。对一维数组，`axis=-1` 和 `axis=0` 等价；对二维数组，`axis=0` 变换列方向、`axis=-1`（即 `axis=1`）变换行方向。

#### 4.2.2 核心流程

`n` 的截断/补零在进入 C 内核前由 `_fix_shape_1d` 统一处理（伪代码）：

```
输入: tmp（已浮点化的数组）, n（目标长度）, axis
1. 若 n < 1: 抛 ValueError
2. 调用 _fix_shape(tmp, shape=(n,), axes=(axis,))
     - 对目标轴构造切片:
         若 tmp.shape[axis] >= n: 切片 = [0:n]        # 截断：取前 n 个
         否则:                      切片 = [0:原长]     # 全取，待补零
     - 若不需补零: 直接返回切片视图 (不拷贝)
     - 若需补零: 新建 (n,) 长度的全零数组，把原数据拷进前半段
3. 返回 (处理后的数组, 是否发生了拷贝)
```

注意两个细节：(1) 截断取的是**前 `n` 个**（低索引），补零补在**末尾**（高索引）；(2) 截断用切片返回的是**视图**（view），不拷贝内存，而补零必须新建数组拷贝。

#### 4.2.3 源码精读

`_fix_shape` 是 N-D 版本的通用实现（[_duccfft/helper.py:146-170](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L146-L170)），关键逻辑：

```python
for n, ax in zip(shape, axes):
    if x.shape[ax] >= n:
        index[ax] = slice(0, n)        # 截断：取前 n 个
    else:
        index[ax] = slice(0, x.shape[ax])  # 全取，标记需补零
        must_copy = True
...
z = np.zeros(s, x.dtype)   # 补零：建一个目标大小的新数组
z[index] = x[index]        # 把原数据放进前半段
return z, True
```

一维的 `_fix_shape_1d` 只是先做 `n<1` 校验，再委托给上面的 `_fix_shape`（[_duccfft/helper.py:173-178](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L173-L178)）。

而在内核 `c2c` 中，只有当 `n is not None` 时才会调用它（[_duccfft/basic.py:22-24](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L22-L24)）。`axis` 则在最后一步原样传给 C 扩展（[_duccfft/basic.py:31](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L31) 的 `(axis,)`）。

公共 docstring 对 `n` 的描述见 [_basic.py:40-44](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L40-L44)。

#### 4.2.4 代码实践

**实践目标**：直观看到 `n` 如何改变**输出长度**与**频谱形态**。

**操作步骤**：

```python
import numpy as np
import scipy.fft

x = np.ones(10)                       # 长度 10 的常数信号
y8  = scipy.fft.fft(x, n=8)           # 截断到 8
y10 = scipy.fft.fft(x)                # 默认 10
y16 = scipy.fft.fft(x, n=16)          # 补零到 16

print(y8.shape, y10.shape, y16.shape) # (8,) (10,) (16,)
```

**需要观察的现象**：

- 三个输出长度分别为 8、10、16，与 `n`（或默认输入长度）一一对应。
- 常数信号的 DFT 理论上只有零频（索引 0）非零。截断到 8 仍是常数，故 `y8[0]==8`、其余近 0；补零到 16 后信号在尾部突变（10 个 1 后接 6 个 0），不再是纯常数，频谱会出现非零的高频分量——这就是「补零 = 频域插值」带来的泄漏现象。

**预期结果**：`y8[0]` 应为 `8`（截断后长度），`y10[0]` 应为 `10`，`y16[0]` 应为 `10`（直流分量等于信号总和）。其余位置的数值形态可自行打印观察。

> 说明：本实践为本地运行型，形状结果确定；具体频谱数值请本地打印确认。

#### 4.2.5 小练习与答案

**练习 1**：对一个长度 10 的信号调用 `fft(x, n=8)`，用的是 `x` 的前 8 个点还是后 8 个点？
**答案**：前 8 个点。`_fix_shape` 用 `slice(0, n)`，即从索引 0 开始取。

**练习 2**：为什么补零会触发内存拷贝，而截断不会？
**答案**：截断 `slice(0, n)` 只是返回原数组的一个视图（view），不分配新内存；补零必须新建一个更大的全零数组并把原数据拷进去（`z[index] = x[index]`），所以一定拷贝。这也解释了内核里为什么要把 `copied` 标志回传给 `overwrite_x`（见 4.4.3）。

---

### 4.3 norm 归一化：backward / ortho / forward 的数学与映射

#### 4.3.1 概念说明

DFT 的定义里没有归一化系数，所以正变换 `fft` 和反变换 `ifft` 的「能量分配」可以有多种约定。`scipy.fft` 用 `norm` 参数提供三种模式，规则是「正反变换的 \(1/N\) 因子放在哪一侧」：

| `norm` | 正变换 `fft` | 反变换 `ifft` | 直觉 |
|--------|--------------|---------------|------|
| `"backward"`（默认） | 不归一化 | 乘 \(1/N\) | 传统信号处理约定 |
| `"ortho"` | 乘 \(1/\sqrt{N}\) | 乘 \(1/\sqrt{N}\) | 正交（酉）变换，能量守恒 |
| `"forward"` | 乘 \(1/N\) | 不归一化 | 把 \(1/N\) 放到正变换 |

一个关键性质：**三种模式下，`ifft(fft(x, norm=A), norm=A) ≈ x` 恒成立**，即正反配对总是可逆的。区别只在于「缩放系数分给谁」。

数学上，记未归一化的 DFT 为 \(\mathrm{DFT}\)、未归一化的 IDFT 为 \(\mathrm{IDFT}\)，则：

\[
\text{backward:}\quad \mathrm{fft}=\mathrm{DFT},\quad \mathrm{ifft}=\tfrac{1}{N}\mathrm{IDFT}
\]

\[
\text{ortho:}\quad \mathrm{fft}=\tfrac{1}{\sqrt{N}}\mathrm{DFT},\quad \mathrm{ifft}=\tfrac{1}{\sqrt{N}}\mathrm{IDFT}
\]

\[
\text{forward:}\quad \mathrm{fft}=\tfrac{1}{N}\mathrm{DFT},\quad \mathrm{ifft}=\mathrm{IDFT}
\]

#### 4.3.2 核心流程

源码没有为三种 norm 写三套逻辑，而是用一个**整数映射表** `_NORM_MAP`，再根据「当前是正变换还是反变换」做一次翻转：

```
1. 查表:  inorm = _NORM_MAP[norm]
        None / "backward" -> 0
        "ortho"            -> 1
        "forward"          -> 2
2. 翻转:  若是正变换(forward=True): mode = inorm
        若是反变换(forward=False): mode = 2 - inorm
3. 把整数 mode 传给 C 扩展 pfft.c2c, 由它按 mode 施加对应缩放
```

这个 `2 - inorm` 的翻转非常巧妙：它让同一张表同时服务于正反两个方向。例如 `backward` 在正变换侧是 `0`（不缩放）、在反变换侧变成 `2`（缩放 \(1/N\)），正好对应「正变换不归一化、反变换除以 \(N\)」。

整数 mode 在 `pyduccfft` 内部的含义（由可观察行为推断）：`0` = 不施加 \(1/N\) 类缩放，`2` = 施加完整 \(1/N\) 缩放，`1` = 施加 \(1/\sqrt{N}\) 缩放。

#### 4.3.3 源码精读

映射表与翻转函数在 [_duccfft/helper.py:181-192](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L181-L192)：

```python
_NORM_MAP = {None: 0, 'backward': 0, 'ortho': 1, 'forward': 2}

def _normalization(norm, forward):
    """Returns the pyduccfft normalization mode from the norm argument"""
    try:
        inorm = _NORM_MAP[norm]
        return inorm if forward else (2 - inorm)
    except KeyError:
        raise ValueError(...)   # 非法 norm 字符串
```

注意 `None` 和 `'backward'` 都映射到 `0`，所以「不传 `norm`」与「显式 `norm="backward"`」行为完全一致——这正是 `norm` 默认值为 `None` 的原因。

在内核 `c2c` 中调用它（[_duccfft/basic.py:19](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L19)）：`norm = _normalization(norm, forward)`，这里的 `forward` 就是 `c2c` 的第一个参数（`fft` 传 `True`、`ifft` 传 `False`）。算出的整数随后传给 `pfft.c2c`（[_duccfft/basic.py:31](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L31)）。

三种模式的可观察缩放关系，可由官方测试精确印证。`test_fft` 对长度 30 的复信号断言（[tests/test_basic.py:64-72](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/tests/test_basic.py#L64-L72)）：

```python
xp_assert_close(fft.fft(x, norm="backward"), expect)
xp_assert_close(fft.fft(x, norm="ortho"),  expect / sqrt(30))
xp_assert_close(fft.fft(x, norm="forward"), expect / 30)
```

即相对默认（backward）结果，`ortho` 整体除以 \(\sqrt{N}\)，`forward` 整体除以 \(N\)——与 4.3.1 的数学完全吻合。`test_ifft` 则验证了三种 norm 下 `ifft(fft(x, norm=A), norm=A) == x`（[tests/test_basic.py:80-85](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/tests/test_basic.py#L80-L85)）。

#### 4.3.4 代码实践

**实践目标**：用三种 `norm` 验证 `fft` 的缩放关系，并确认正反配对可逆。

**操作步骤**：

```python
import numpy as np
import scipy.fft

rng = np.random.default_rng(0)
x = rng.standard_normal(30) + 1j*rng.standard_normal(30)

base = scipy.fft.fft(x)                      # 默认 backward
print("ortho  / backward:", np.allclose(scipy.fft.fft(x, norm="ortho"),   base / np.sqrt(30)))
print("forward/ backward:", np.allclose(scipy.fft.fft(x, norm="forward"), base / 30))

for A in ["backward", "ortho", "forward"]:
    xr = scipy.fft.ifft(scipy.fft.fft(x, norm=A), norm=A)
    print(f"ifft(fft) norm={A}: 还原误差 {np.max(np.abs(xr - x)):.2e}")
```

**需要观察的现象**：两个 `allclose` 都应为 `True`；三种 norm 的还原误差都应在 \(10^{-15}\) 量级（浮点精度）。

**预期结果**：三行还原误差均为极小值（约 `1e-15`），证明正反配对恒可逆，差别只在缩放归属。

> 说明：本实践为本地运行型，结论在双精度下稳定成立。

#### 4.3.5 小练习与答案

**练习 1**：如果不传 `norm`（即 `norm=None`），等价于三种模式里的哪一种？
**答案**：等价于 `"backward"`。因为 `_NORM_MAP` 里 `None` 和 `'backward'` 都映射到整数 `0`。

**练习 2**：为什么 `_normalization` 要做 `2 - inorm` 这个翻转，而不是直接返回 `inorm`？
**答案**：因为同一张表（`0/1/2`）要同时服务正反两个方向。`backward` 要求「正变换不缩放、反变换缩放 \(1/N\)」，所以正变换侧取 `0`、反变换侧必须取 `2`。`2 - inorm` 正好把 `0↔2` 对调、`1` 保持不变（`ortho` 在两侧都是 \(1/\sqrt{N}\)），一行代码就实现了方向相关的缩放归属。

**练习 3**：`norm="ortho"` 有什么物理意义？
**答案**：它让 `fft` 成为一个**酉（unitary）变换**，满足 \(\sum|x[n]|^2 = \sum|X[k]|^2\)（Parseval 定理），即变换前后信号能量（范数）守恒。这在需要把频谱幅度当作「能量分布」来解释时很有用。

---

### 4.4 从签名到计算核心：_execute_1D、c2c 与 Bluestein

本节把前几节拼起来：跟踪一次 `fft(x)` 如何穿透四层架构到达 C 扩展，顺带说清 `overwrite_x`、`workers`、`plan` 三个参数的真实作用，以及「任意长度都能 \(O(N\log N)\)」的 Bluestein 保证。

#### 4.4.1 概念说明

- **`overwrite_x`**：一个「性能提示」。设为 `True` 表示「调用后我不再需要原始 `x`，你可以随意改写它的内存」。实现**可能**（但不保证）把结果直接写回 `x` 的内存以省一次分配。设为 `False`（默认）则保证不破坏 `x`。
- **`workers`**：并行线程数。仅当 `x` 至少是二维时才有意义——并行是把**多条独立的 1-D FFT** 分给不同线程，而不是把一条 FFT 拆开。负值会从 `os.cpu_count()` 回绕。细节见 u5-l3。
- **`plan`**：预设计划，SciPy 当前**完全不使用**，仅为兼容下游（如 mkl-fft、cuFFT）厂商保留。传入非 `None` 会在内核里直接抛 `NotImplementedError`。
- **Bluestein 算法**：当长度 \(N\) 不易分解（如素数）时，传统 Cooley-Tukey FFT 退化。`scipy.fft` 用 Bluestein 算法把任意长度的 DFT 转化为一个更长的、长度为「好分解」的 DFT 来算，从而保证**任何长度都绝不差于 \(O(N\log N)\)**。

#### 4.4.2 核心流程

`fft(x)` 的完整调用链：

```
scipy.fft.fft(x, ...)                     # _basic.py 公共签名 (返回 Dispatchable)
        │  uarray 按 domain="numpy.scipy.fft" 分派
        ▼
_ScipyBackend.__ua_function__             # 按方法名 "fft" 查找
        ▼
_basic_backend.fft(x, ...)                # _basic_backend.py
        │  调用 _execute_1D('fft', _duccfft.fft, ...)
        ▼
_execute_1D:                              # numpy 分支 (最常见)
        │  is_numpy(xp) 为真 -> 直接调用 duccfft_func
        ▼
_duccfft.fft(x, ...) == c2c(True, x, ...) # _duccfft/basic.py
        │  1. _asfarray(x)         浮点化 / 对齐 / 字节序
        │  2. _normalization(...)  norm -> 整数 mode
        │  3. _workers(...)        解析并行数
        │  4. _fix_shape_1d(...)   若给了 n, 截断/补零
        ▼
pfft.c2c(tmp, (axis,), True, mode, out, workers)   # C 扩展 pyduccfft
        │  内部按长度选 Cooley-Tukey 或 Bluestein
        ▼
返回复数 ndarray
```

注意 `_basic_backend.fft` 与 `_duccfft.fft` **同名但分属两层**：前者是后端路由，后者（`functools.partial(c2c, True)`）才是真正的计算入口。

#### 4.4.3 源码精读

后端层的 `fft` 极其薄，只是把参数转发给 `_execute_1D`（[_basic_backend.py:77-80](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L77-L80)）：

```python
def fft(x, n=None, axis=-1, norm=None, overwrite_x=False, workers=None, *, plan=None):
    return _execute_1D('fft', _duccfft.fft, x, n=n, axis=axis, norm=norm,
                       overwrite_x=overwrite_x, workers=workers, plan=plan)
```

`_execute_1D` 的核心是**三分支路由**（[_basic_backend.py:27-49](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L27-L49)）。对最常见的 numpy 输入，走第一个分支（[_basic_backend.py:30-33](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L30-L33)）：直接 `np.asarray(x)` 后调用 `_duccfft.fft`，把 `overwrite_x`、`workers`、`plan` 原样透传。另外两个分支（xp.fft 直连、转 numpy 回退）属于数组标准兼容，u6-l1 详讲。其中 `_validate_fft_args`（[_basic_backend.py:8-15](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L8-L15)）会**对非 numpy 后端禁用 `workers`/`plan`**——因为只有 ducc 核心支持它们。

计算核心 `c2c` 是真正的「做 FFT」之处（[_duccfft/basic.py:11-31](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L11-L31)）。它按 4.4.2 的顺序完成预处理，关键几行：

```python
def c2c(forward, x, n=None, axis=-1, norm=None, overwrite_x=False, workers=None, *, plan=None):
    if plan is not None:
        raise NotImplementedError('Passing a precomputed plan is not yet supported ...')
    tmp = _asfarray(x)
    overwrite_x = overwrite_x or _datacopied(tmp, x)   # 若已拷贝, 不如放开覆盖
    norm = _normalization(norm, forward)
    workers = _workers(workers)
    if n is not None:
        tmp, copied = _fix_shape_1d(tmp, n, axis)
        overwrite_x = overwrite_x or copied
    ...
    out = (tmp if overwrite_x and tmp.dtype.kind == 'c' else None)   # 仅复数+允许覆盖时原地写
    return pfft.c2c(tmp, (axis,), forward, norm, out, workers)
```

三个细节：

1. **`plan`**：第一行就拦截，传非 `None` 立刻抛 `NotImplementedError`（[_duccfft/basic.py:14-16](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L14-L16)）。
2. **`overwrite_x`**：只有当「用户允许覆盖 **且** 输入是复数」时，才把 `tmp` 作为输出缓冲 `out` 传下去（[_duccfft/basic.py:29](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L29)）。实数输入会先被 `_asfarray`/`c2r` 升级，所以原地写只在复数路径上有意义。注意 `_datacopied` 和补零的 `copied` 都会**反向打开** `overwrite_x`——既然已经拷了一份新内存，不如就放开让实现复用它。
3. **`workers`**：由 `_workers`（[_duccfft/helper.py:195-208](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L195-L208)）解析：`None` 取线程局部默认（默认 1），负值从 `cpu_count` 回绕，`0` 抛错。

`fft` / `ifft` 从 `c2c` 派生的写法是本架构的一个亮点（[_duccfft/basic.py:34-37](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L34-L37)）：

```python
fft  = functools.partial(c2c, True)   ; fft.__name__  = 'fft'
ifft = functools.partial(c2c, False)  ; ifft.__name__ = 'ifft'
```

`forward=True` 就是正变换、`False` 就是反变换——`c2c` 一个函数同时承担 `fft`/`ifft`，靠 `forward` 参数和 `_normalization` 的翻转完成方向区分。`__name__` 手动设置是为了让 `partial` 对象在报错/调试时显示正确的名字。

最后是 Bluestein。它在 C 扩展 `pyduccfft` 内部实现（Python 层不可见），但**设计意图**写在了公共 docstring 里（[_basic.py:91-98](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L91-L98)）：

> For poorly factorizable sizes, `scipy.fft` uses Bluestein's algorithm and so is never worse than O(`n` log `n`).

也就是说，无论 `n` 是 2 的幂、合数还是素数，`pfft.c2c` 内部都会自动挑选合适的算法，对用户完全透明。这正是 `scipy.fft` 相比「只对 2 的幂快」的朴素实现的一大优势。

#### 4.4.4 代码实践

**实践目标**：跟踪调用链并验证「任意长度都是 \(O(N\log N)\)」。

**操作步骤**：

```python
import numpy as np, scipy.fft, timeit

# 1) plan 被禁用：这行会抛 NotImplementedError
try:
    scipy.fft.fft([1, 2, 3, 4], plan="something")
except NotImplementedError as e:
    print("plan 被拒绝:", e)

# 2) Bluestein 保证：素数长度 vs 2 的幂长度, 耗时应同一量级
def cost(n):
    x = np.ones(n, dtype=complex)
    return timeit.timeit(lambda: scipy.fft.fft(x), number=200)

for n in [1024, 1009]:           # 1009 是素数
    print(f"n={n:5d}  耗时 {cost(n):.4f}s")
```

**需要观察的现象**：

- 第 1 步抛 `NotImplementedError`，证实 `plan` 在 SciPy 里尚未实现。
- 第 2 步：素数长度 1009 的耗时**不应**比 2 的幂 1024 慢一个数量级——若有 Bluestein，二者同量级；若没有（朴素实现），素数会退化到 \(O(N^2)\) 而慢几十倍。

**预期结果**：两个 `n` 的耗时在同一量级（具体数值随机器而异）。如果观察到素数明显慢于 2 的幂很多倍，请检查是否误用了不带 Bluestein 的旧实现。

> 说明：耗时数值「待本地验证」，但「素数与 2 的幂同量级」这一**相对结论**应稳定成立。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_execute_1D` 要对非 numpy 后端调用 `_validate_fft_args` 禁用 `workers`/`plan`？
**答案**：因为 `workers` 和 `plan` 是 ducc 核心特有的能力。当走 xp.fft（如 CuPy）分支时，目标命名空间的 `fft` 不支持这两个参数，强行传会出错，所以 `_validate_fft_args` 检测到非 `None` 就直接抛 `ValueError`。

**练习 2**：`fft` 和 `ifft` 在 `_duccfft/basic.py` 里是同一个函数吗？
**答案**：是。它们都是 `functools.partial(c2c, ...)` 的产物：`fft` 固定 `forward=True`，`ifft` 固定 `forward=False`。共用 `c2c` 一个实现，靠 `forward` 区分方向、靠 `_normalization(norm, forward)` 决定缩放归属。

**练习 3**：`overwrite_x=True` 时，结果一定写回原 `x` 的内存吗？
**答案**：不一定。源码只是**允许**实现复用 `x` 的内存（把 `tmp` 作为 `out` 传下去），docstring 明确说「this is in no way guaranteed」。而且只有复数输入且未发生拷贝时才会传 `out`；实数输入或触发补零拷贝时，`out` 仍为 `None`。所以不能依赖 `x` 被原地改写。

---

## 5. 综合实践

把本讲的 `n`（截断/补零）和 `norm`（三种归一化）串成一个完整的验证脚本。

**任务**：构造一个长度 10 的复信号，分别用 `n=8`（截断）和 `n=16`（补零）调用 `fft`，对比输出长度；再用三种 `norm` 验证 `fft→ifft` 的缩放关系。

**参考脚本**（可在本地直接运行）：

```python
import numpy as np
import scipy.fft

rng = np.random.default_rng(42)
x = rng.standard_normal(10) + 1j * rng.standard_normal(10)

# ---- Part A: n 的截断与补零 ----
for n in (8, 10, 16):
    y = scipy.fft.fft(x, n=n)
    tag = "截断" if n < 10 else ("默认" if n == 10 else "补零")
    print(f"n={n:2d} ({tag}): 输出长度 = {y.shape[0]}")

# ---- Part B: 三种 norm 的 fft->ifft 缩放关系 ----
N = 10
base = scipy.fft.fft(x)                       # backward 基准
print("\nfft 缩放 (相对 backward):")
print("  ortho  : allclose =",
      np.allclose(scipy.fft.fft(x, norm="ortho"),   base / np.sqrt(N)))
print("  forward: allclose =",
      np.allclose(scipy.fft.fft(x, norm="forward"), base / N))

print("\n正反配对可逆性 (ifft(fft(x, A), A) == x):")
for A in ("backward", "ortho", "forward"):
    xr = scipy.fft.ifft(scipy.fft.fft(x, norm=A), norm=A)
    print(f"  norm={A:8s}: 最大误差 = {np.max(np.abs(xr - x)):.2e}")
```

**预期结果**：

- Part A：输出长度依次为 `8 / 10 / 16`。
- Part B：两个 `allclose` 为 `True`；三种 norm 的正反配对最大误差均在 `1e-15` 量级。

**进阶思考**（可选）：把 `x` 换成一个已知频率的纯正弦 `np.sin(2*np.pi*np.arange(10)/10)`，观察 `n=16` 补零后频谱主峰位置不变、但谱线变密——这就是「频域插值」。

> 说明：长度与 `allclose` 结论确定；具体数值请本地运行确认。

---

## 6. 本讲小结

- `fft` / `ifft` 的公共签名有七个参数：`x`、`n`、`axis`、`norm`、`overwrite_x`、`workers`、`plan`；其中 `plan` 是仅限关键字参数且 SciPy 当前不使用。
- 公共函数体只有 `return (Dispatchable(x, np.ndarray),)`——它是一份「分派协议声明」，真正计算在四层架构的更深处。
- 参数 `n` 通过 `_fix_shape` 实现**截断（取前 n 个，返回视图）**或**补零（末尾补 0，新建数组）**；`axis` 选择变换轴。
- `norm` 三种模式（`backward`/`ortho`/`forward`）由 `_NORM_MAP` + `2 - inorm` 翻转一行实现，保证正反配对恒可逆，区别只在 \(1/N\) 缩放归属。
- 一次 `fft(x)` 的调用链：`_basic.fft → _basic_backend.fft → _execute_1D → _duccfft.c2c → pfft.c2c`；`overwrite_x` 仅在复数路径下可能原地写、`workers` 由 `_workers` 解析、`plan` 立即被拒。
- Bluestein 算法（在 C 扩展内）保证**任意长度**的变换都不差于 \(O(N\log N)\)，对用户透明。

---

## 7. 下一步学习建议

- 想了解**实输入**的半谱变换与 Hermitian 对称？继续看 **u2-l2（rfft/irfft 与 hfft/ihfft）**，那里会讲 `_swap_direction` 如何翻转归一化方向。
- 想把变换扩展到**多维**？看 **u2-l3（fftn/fft2 与 s、axes 控制）**，理解多轴变换如何复用本讲的 `axis` 思想。
- 想知道为什么补零常凑到「快速长度」、以及 `fftfreq`/`fftshift` 怎么用？看 **u2-l4（辅助函数）**。
- 想深入 `workers` 的多线程模型与 `set_workers` 上下文管理？看 **u5-l3（并行 workers）**。
- 想看清 `_execute_1D` 另外两个分支（xp.fft 直连、转 numpy 回退）如何支持 CuPy/PyTorch？看 **u6-l1（数组标准与跨后端执行）**。
