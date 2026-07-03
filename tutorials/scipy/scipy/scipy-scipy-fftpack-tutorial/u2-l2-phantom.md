# fftpack 变换的薄壳委托、overwrite_x 与精度契约

> 本讲定位：`u2-l2-phantom` 是手册里一个「占位（phantom）」槽位，原始大纲只给出了锚点文件 `_basic.py` 与「intermediate」层级，并未指定具体主题。为了让这一槽位对学习者真正有用，本讲挑选了 `_basic.py` 中一处**尚未被 u1-l4（fft/ifft 基础用法）、u2-l1（多维 FFT 与形状校验）、u2-l2（rfft 实数打包）覆盖**的中间层主题：**Python 薄壳层本身**——这 8 个公共函数在 Python 里到底做了什么、向调用者承诺了哪些契约。所有结论均来自真实源码，未杜撰任何接口或行为。

## 1. 本讲目标

学完本讲后，你应当能够：

1. 看懂 `_basic.py` 里 `fft / ifft / rfft / irfft / fftn / ifftn / fft2 / ifft2` 这 8 个函数的「Python 层」几乎不做事——它们只是把参数**原样转发**给底层 DUCC 后端。
2. 说清楚 `overwrite_x=True` 这个参数到底承诺了什么、又**没有**承诺什么（这是一条「许可」而非「保证」）。
3. 解释 fftpack 对输入数据类型的精度转换规则：half→single、非浮点→double、long-double 不被支持。
4. 理解为什么 fftpack 的函数签名里**没有** `norm` 参数，而转发调用里却固定传了一个 `None`。

本讲是 u1-l4 的补充：u1-l4 教你「怎么用 `fft/ifft`」，本讲带你「低头看 Python 薄壳这一层到底替你做了什么、向你保证什么」。

## 2. 前置知识

- **DFT 与 FFT**：离散傅里叶变换（DFT）是把时域序列变成频域序列的数学运算；FFT 是计算 DFT 的快速算法。这一层概念在 u1-l4 已建立，本讲不再重复数学定义。
- **薄壳（thin shell）/ 委托（delegation）**：一个函数自己不做核心计算，只整理参数后交给另一个「真正干活」的函数，这种设计叫薄壳或委托。本讲会看到 fftpack 的 Python 层正是这样的薄壳。
- **归一化约定（normalization）**：DFT 有「前向求和、逆向求平均（除以 n）」的不同写法。numpy/SciPy 用 `norm` 参数控制这一点，取值有 `"backward"`（默认）、`"ortho"`、`"forward"`。fftpack 用的是 `"backward"`。
- **数据类型与精度**：numpy 里的浮点精度从低到高大致是 `float16`（半精度）→ `float32`（单精度）→ `float64`（双精度）→ `longdouble`（扩展精度）。FFT 库通常只实现其中几档。

## 3. 本讲源码地图

本讲只涉及一个文件，但它承载了 fftpack 全部 8 个公共变换函数的 Python 层：

| 文件 | 作用 |
| --- | --- |
| `scipy/fftpack/_basic.py` | fftpack 的「新家」实现。定义 `fft / ifft / rfft / irfft / fftn / ifftn / fft2 / ifft2`，每个函数体都是「整理参数 + 一行委托给 `_duccfft`」。 |

辅助理解（本讲会引用但不在源码精读里展开）：

| 文件 | 作用 |
| --- | --- |
| `scipy/fftpack/_helper.py` | `_good_shape` 校验 `shape/axes`，被 `fftn/ifftn` 调用。 |

## 4. 核心概念与源码讲解

### 4.1 薄壳委托架构：Python 层只做「转发」

#### 4.1.1 概念说明

打开 `_basic.py` 你会发现一件反直觉的事：作为整个 fftpack 最核心的 8 个变换函数，它们的函数体**几乎都是一行 `return`**。真正计算 DFT 的代码并不在这个文件里。

这是因为 fftpack 采用了「**薄壳 + 后端委托**」的架构：

- **薄壳层**（`_basic.py`，纯 Python）：负责对外暴露稳定的函数签名、文档字符串，必要时做一点点参数整理（如多维变换里校验 `shape`）。
- **后端层**（`_duccfft`，编译过的 C/C++）：真正的高速 FFT 实现。DUCC 是 SciPy 内部使用的 FFT 后端。

这样的好处是：**对外的 API 永远稳定**，而底层算法可以随时替换。事实上 fftpack 历史上用过 Fortran 版 FFTPACK，后来迁移到 DUCC——这次迁移对调用者完全透明，正是因为有这层薄壳。

#### 4.1.2 核心流程

以 `fft` 为例，调用链是：

```text
用户调用 scipy.fftpack.fft(x, n, axis, overwrite_x)
        │
        ▼
_basic.fft(...)            # 薄壳：仅整理、转发
        │  return _duccfft.fft(x, n, axis, None, overwrite_x)
        ▼
scipy.fft._duccfft.fft(...)  # 后端：真正的 FFT 计算
        │
        ▼
返回复数 ndarray
```

关键点：薄壳层把用户的 `overwrite_x` 原样传下去，并在 `axis` 和 `overwrite_x` 之间**插入了一个硬编码的 `None`**。这个 `None` 占据的是后端 `norm` 参数的位置——见 4.3 节。

#### 4.1.3 源码精读

文件顶部的两行 import 揭示了薄壳依赖的两个对象：

[_basic.py:L8-L9](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L8-L9) —— `_duccfft` 是真正的计算后端，`_good_shape` 是多维变换要用的形状校验工具。

`fft` 的完整函数体只有一行（其余全是文档字符串）：

[_basic.py:L88-L88](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L88-L88) —— 把 `(x, n, axis, None, overwrite_x)` 转发给 `_duccfft.fft`，自己不做任何计算。

`ifft` 同样是一行委托：

[_basic.py:L144-L144](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L144-L144) —— 委托给 `_duccfft.ifft`，注意第 4 个参数同样是硬编码的 `None`。

实数变换 `rfft / irfft` 则委托给**带 `_fftpack` 后缀**的后端函数（这个后缀用来区分 fftpack 的「实数交错打包」与 `scipy.fft` 的「复数打包」，详见 u2-l2）：

[_basic.py:L205-L205](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L205-L205) —— `rfft` 委托给 `_duccfft.rfft_fftpack`。

多维函数 `fftn` 多了一步形状整理，但本质仍是委托：

[_basic.py:L336-L337](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L336-L337) —— 先用 `_good_shape` 规整 `shape`，再委托给 `_duccfft.fftn`。

#### 4.1.4 代码实践

**实践目标**：用 Python 自省能力，亲眼确认 `_basic.py` 里的公共函数确实是「薄壳」，并定位到它们委托的后端。

**操作步骤**（待本地验证）：

```python
# 示例代码
import inspect
import scipy.fftpack as fp
import scipy.fftpack._basic as basic

# 1. 确认 scipy.fftpack.fft 定义在 _basic.py
print(fp.fft.__module__)          # 预期: scipy.fftpack._basic

# 2. 打印 fft 的源码，看看函数体是不是只有一行 return
print(inspect.getsource(basic.fft))
```

**需要观察的现象**：`inspect.getsource(basic.fft)` 打印出的函数体里，除了文档字符串，应只有一行 `return _duccfft.fft(...)`。

**预期结果**：`__module__` 输出 `scipy.fftpack._basic`，源码确认薄壳委托。

#### 4.1.5 小练习与答案

**练习 1**：`fft2` 和 `ifft2` 的函数体里调用的是谁？它们和 `fftn / ifftn` 是什么关系？

**参考答案**：`fft2` 调用的是 `fftn(x, shape, axes, overwrite_x)`，`ifft2` 调用的是 `ifftn(...)`（见 [_basic.py:L397](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L397) 与 [L428](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L428)）。`fft2 / ifft2` 只是「固定在最后两个轴上」的 `fftn / ifftn`，本身又是一层薄壳。

**练习 2**：如果未来 SciPy 把后端从 DUCC 换成别的实现，`_basic.py` 里需要改动哪些函数体？为什么调用者不受影响？

**参考答案**：需要改 `fft/ifft/rfft/irfft/fftn/ifftn` 这 6 个函数体里的 `return _duccfft.xxx(...)` 行（`fft2/ifft2` 委托给 `fftn/ifftn`，无需直接改）。调用者不受影响，因为对外签名（参数名、顺序、返回约定）由薄壳保证稳定，后端替换对使用者透明——这正是薄壳架构的意义。

---

### 4.2 `overwrite_x` 契约：一条「许可」而非「保证」

#### 4.2.1 概念说明

fftpack 的每个变换函数都有一个布尔参数 `overwrite_x`，默认 `False`。它的文档说明是：

> If True, the contents of `x` can be destroyed; the default is False.

注意措辞：是 **can be destroyed**（**可能**被破坏），不是 **will be destroyed**（**一定会**被破坏）。这是理解这个参数的关键：

- `overwrite_x=False`（默认）：fftpack **承诺不破坏**你的输入数组 `x`。需要时会内部拷贝一份再算。
- `overwrite_x=True`：你**授权** fftpack 可以就地复用 `x` 的内存来存放中间结果。**但不保证一定复用**——后端可能出于算法原因仍然另开缓冲区。

所以这是一个**性能提示**（performance hint）：当你不再需要原始 `x` 时，传 `True` 可能让 fftpack 省掉一次内存拷贝、更快；但它不是你必须依赖的「破坏行为」。

#### 4.2.2 核心流程

```text
overwrite_x=False  ──►  fftpack 保证 x 不变（必要时内部拷贝）
overwrite_x=True   ──►  fftpack 被允许改写 x（但可能不改）
```

这条「许可」自上而下逐层透传：`_basic.fft` 把 `overwrite_x` 原样交给 `_duccfft.fft`（见 4.1.3 的委托行）。Python 薄壳层**完全不解释、不处理**这个参数，只负责把它送到真正算 FFT 的后端。

#### 4.2.3 源码精读

`fft` 的签名与文档对 `overwrite_x` 的定义：

[_basic.py:L31-L32](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L31-L32) —— 参数声明与「contents of x can be destroyed」的措辞，注意是「can be」。

委托行里 `overwrite_x` 作为最后一个位置参数透传给后端：

[_basic.py:L88-L88](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L88-L88) —— `overwrite_x` 被 Python 层原样转发，薄壳不做任何额外处理。

`ifft` 用的是完全相同的契约（[_basic.py:L110-L111](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L110-L111)）。

#### 4.2.4 代码实践

**实践目标**：验证「`overwrite_x` 不影响计算正确性」，并理解它只是性能许可。

**操作步骤**（待本地验证）：

```python
# 示例代码
import numpy as np
from scipy.fftpack import fft

x = np.arange(64, dtype=np.float64)

a = fft(x, overwrite_x=False)
b = fft(x, overwrite_x=True)

# 1. 正确性：两种调用结果必须完全一致
print(np.array_equal(a, b))   # 预期 True

# 2. 契约演示：默认 False 时，输入数组保证不变
print(np.array_equal(x, np.arange(64, dtype=np.float64)))  # 预期 True
```

**需要观察的现象**：`overwrite_x` 取 `True/False` 得到的结果完全相同；默认 `False` 时原始 `x` 保持不变。

**预期结果**：第 1 个比较为 `True`（正确性一致）；第 2 个比较为 `True`（输入未被破坏）。**不要**尝试断言「`overwrite_x=True` 之后 `x` 一定被改」——后端是否真改写是不可保证的，写依赖它的测试会偶发失败。

#### 4.2.5 小练习与答案

**练习 1**：为什么 fftpack 不把 `overwrite_x` 做成「保证破坏」？从「后端实现自由度」角度思考。

**参考答案**：因为不同长度、不同精度下，后端可能选用不同算法（如需要额外工作缓冲区），并非所有情况都能就地完成计算。若承诺「一定破坏」，就会强制后端在无法就地时也要先做无意义的就地写，反而更慢。设计成「许可」让后端自由选择最优实现，是更合理的工程取舍。

**练习 2**：在一次处理超大数组、且之后不再需要原始数据的批处理中，应该传 `overwrite_x=True` 还是 `False`？为什么？

**参考答案**：传 `True`。此时你已不需要原始 `x`，授权 fftpack 复用其内存可能省掉一次大数组拷贝，降低峰值内存与耗时；即使后端没复用，结果也正确，没有任何风险。

---

### 4.3 精度转换规则与那个硬编码的 `None`

#### 4.3.1 概念说明

薄壳层除了转发参数，还通过**文档字符串**向调用者承诺了两条「输入类型/精度」规则。这些规则不是 Python 层代码实现的，而是后端 `_duccfft` 的行为，但 fftpack 把它们写进每个函数的 Notes 里作为正式契约：

1. **半精度（half / float16）输入会被转成单精度（float32）**。
2. **非浮点输入（如整数）会被转成双精度（float64）**。
3. **扩展精度（longdouble）不被支持**。

同时，你会注意到 fftpack 的函数签名里**没有 `norm` 参数**，而委托行里却固定写了一个 `None`。这个 `None` 正是 `norm` 参数的值——fftpack 把归一化**硬编码为 `None`（即 `"backward"` 约定）**，对外不暴露选择余地。这解释了为什么 u1-l4 总结里的口诀「fft 求和、ifft 求平均」成立：`backward` 约定下，前向变换不归一化、逆向变换除以 n。

#### 4.3.2 核心流程

输入数组 `x` 进入 fftpack 时的类型分派：

```text
x.dtype 是 float16    ──► 内部转 float32，输出复数为 complex64
x.dtype 是 float32    ──► 直接算，       输出 complex64
x.dtype 是 float64    ──► 直接算，       输出 complex128
x.dtype 是整数等非浮点 ──► 内部转 float64，输出 complex128
x.dtype 是 longdouble ──► 不支持（报错或落回，行为以后端为准）
```

归一化方面：

```text
fftpack.fft  ──► 后端 norm=None (= "backward") ──► 前向不归一化（求和）
fftpack.ifft ──► 后端 norm=None (= "backward") ──► 逆向除以 n（求平均）
```

#### 4.3.3 源码精读

精度规则写在 `fft` 文档字符串的 Notes 段：

[_basic.py:L61-L64](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L61-L64) —— 明确三条精度规则：half→single、非浮点→double、long-double 不支持。

`ifft` 的同一规则（[_basic.py:L124-L127](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L124-L127)），说明这是 8 个函数共享的统一契约。

`fft` 的前向公式（无归一化、纯求和）：

[_basic.py:L16-L18](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L16-L18) —— `y(j) = (x * exp(-2π·i·j·k/n)).sum()`，注意是 `.sum()` 而非 `.mean()`。

`ifft` 的逆向公式（除以 n、求平均）：

[_basic.py:L96-L97](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L96-L97) —— `y(j) = (x * exp(+2π·i·j·k/n)).mean()`，这里是 `.mean()`，即求和后再除以 n。

那个硬编码的 `None`：对比 fftpack 的函数签名与委托行就能看出「缺了一个 `norm`」：

- 签名 [_basic.py:L12-L12](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L12-L12)：`def fft(x, n=None, axis=-1, overwrite_x=False)` —— 没有 `norm`。
- 委托 [_basic.py:L88-L88](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L88-L88)：`_duccfft.fft(x, n, axis, None, overwrite_x)` —— 第 4 个位置参数（`axis` 之后、`overwrite_x` 之前）被填成硬编码 `None`，这正是后端 `norm` 槽位。fftpack 因此永远使用 `backward` 归一化，且不把 `norm` 暴露给用户。

#### 4.3.4 代码实践

**实践目标**：亲手验证三条精度转换规则，并确认 fftpack 输出的复数精度随输入走。

**操作步骤**（待本地验证）：

```python
# 示例代码
import numpy as np
from scipy.fftpack import fft

n = 8
cases = {
    "int (非浮点)": np.arange(n),                  # 整数
    "float16 (半精度)": np.arange(n, dtype=np.float16),
    "float32 (单精度)": np.arange(n, dtype=np.float32),
    "float64 (双精度)": np.arange(n, dtype=np.float64),
}

for name, x in cases.items():
    y = fft(x)
    print(f"{name:18s} -> 输出 dtype = {y.dtype}")
```

**需要观察的现象**：整数输入得到 `complex128`；`float16` 与 `float32` 都得到 `complex64`；`float64` 得到 `complex128`。

**预期结果**：

```text
int (非浮点)       -> 输出 dtype = complex128
float16 (半精度)   -> 输出 dtype = complex64
float32 (单精度)   -> 输出 dtype = complex64
float64 (双精度)   -> 输出 dtype = complex128
```

这与 [_basic.py:L61-L64](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L61-L64) 的承诺一致。若你的环境对 longdouble 输入调用 `fft`，请观察是否按文档「不支持」处理——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么整数输入会被转成 `float64`（双精度）而不是 `float32`？

**参考答案**：因为 FFT 涉及三角函数（`exp`）和大量乘加，整数本身没有精度损失风险，但变换结果必然是带小数的复数。转成最高常规精度 `float64` 能最大程度避免数值误差，是更安全、更符合通用科学计算预期的默认选择。

**练习 2**：用 `scipy.fft.fft`（新版）时你可以传 `norm="ortho"`；用 `scipy.fftpack.fft`（本讲对象）时可以吗？为什么？

**参考答案**：不可以。fftpack 的 `fft` 签名里没有 `norm` 参数（[_basic.py:L12](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L12)），它在委托时把 `norm` 硬编码为 `None`（即 `"backward"`）。这又是「fftpack 是遗留模块、能力更窄」的一个体现——需要正交归一化等新特性时，应改用 `scipy.fft`。

---

## 5. 综合实践

把本讲三个模块串起来：**为 fftpack 写一个「契约自检」小脚本**。

任务：写一段代码，对一个随机复数数组 `x` 同时做以下检查，并打印每项结论：

1. **薄壳定位**：打印 `scipy.fftpack.fft.__module__`，确认它指向 `_basic`。
2. **往返一致性（backward 归一化）**：验证 `np.allclose(ifft(fft(x)), x)`，体会「fft 求和、ifft 除以 n」使得乘积归一。
3. **`overwrite_x` 不改正确性**：比较 `fft(x, overwrite_x=False)` 与 `fft(x, overwrite_x=True)` 是否 `array_equal`。
4. **精度随输入走**：分别用 `float32` 和 `int` 版本的 `x` 调 `fft`，打印输出 `dtype`，确认分别是 `complex64` 和 `complex128`。

参考框架（待本地验证）：

```python
# 示例代码
import numpy as np
from scipy.fftpack import fft, ifft

rng = np.random.default_rng(0)
x = rng.standard_normal(32) + 1j * rng.standard_normal(32)

print("1. fft 定义模块:", fft.__module__)
print("2. 往返一致:", np.allclose(ifft(fft(x)), x))
print("3. overwrite 不影响结果:",
      np.array_equal(fft(x, overwrite_x=False), fft(x, overwrite_x=True)))
print("4. float32 ->", fft(x.astype(np.float32).real).dtype,
      "| int ->", fft(np.arange(32)).dtype)
```

**预期**：第 1 项为 `scipy.fftpack._basic`；第 2、3 项为 `True`；第 4 项依次为 `complex64`、`complex128`。

完成后再回到源码：把脚本里每条结论对应回 `_basic.py` 的具体行号（薄壳委托 L88、往返公式 L16-L18 与 L96-L97、精度规则 L61-L64），你就把「行为—源码—契约」三者对上了。

## 6. 本讲小结

- `_basic.py` 的 8 个公共变换函数是**薄壳**：函数体基本只有一行 `return _duccfft.xxx(...)`，真正计算在编译后端 DUCC。
- `overwrite_x=True` 是一条**性能许可**（「can be destroyed」），不是「保证破坏」；它被 Python 层原样透传给后端，不影响结果正确性。
- fftpack 对输入精度有统一契约：**half→single、非浮点→double、longdouble 不支持**；输出复数精度随输入走（`complex64` 或 `complex128`）。
- 委托行里那个硬编码的 `None` 是后端的 `norm` 槽位，fftpack **不暴露 `norm` 参数**、永远使用 `"backward"` 归一化——这解释了「fft 求和、ifft 求平均」。
- 薄壳架构让 fftpack 历史上从 Fortran FFTPACK 迁移到 DUCC 时，对外签名保持稳定、对调用者透明。

## 7. 下一步学习建议

- 想看「薄壳之外、参数真正被整理」的地方，请进入 **u2-l1 多维复数 FFT 与形状校验**，精读 `_helper.py:_good_shape`（[_helper.py:L105-L115](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_helper.py#L105-L115)），理解 `fftn/ifftn` 在委托前如何校验 `shape/axes`。
- 想理解薄壳委托的「实数分支」为何用带 `_fftpack` 后缀的后端，请复习 **u2-l2 实数序列 FFT：rfft/irfft 与实数打包格式**。
- 若你对「薄壳背后 DUCC 是怎么实现的」感兴趣，可以继续阅读 `scipy.fft._duccfft`（在本仓库 `scipy/fft/` 目录下，超出 fftpack 范围），那是真正的算法所在。
