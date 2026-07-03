# 项目定位与遗留模块身份

## 1. 本讲目标

本讲是整个 `scipy.fftpack` 学习手册的第一篇。读完本讲后，你应当能够：

- 说清楚 `scipy.fftpack` 是什么，它为什么被标记为 **legacy（遗留）** 模块。
- 理解 `.. legacy::` 指令的含义，以及 SciPy 官方对「新代码该用谁」的建议。
- 区分「合法的公共导入」与「会触发 `DeprecationWarning` 的遗弃导入」，并能解释背后的源码原因。
- 知道 `fftpack` 为什么至今仍然被保留，以及在什么场景下还有使用价值。

本讲不要求你已经会写傅里叶变换，只需要你有一点点 Python 和 numpy 的使用经验。

## 2. 前置知识

### 2.1 什么是傅里叶变换（直觉版）

傅里叶变换把一个信号从「时域」（随时间变化的曲线）转换到「频域」（由哪些频率的正弦波叠加而成）。例如一段含有噪声的正弦波，做完变换后在频谱上会看到一根尖峰，尖峰的位置就是信号的频率。

离散傅里叶变换（DFT）的定义为：

\[
X[k] = \sum_{n=0}^{N-1} x[n] \, e^{-2\pi i \, k n / N}, \qquad k = 0, 1, \dots, N-1
\]

「快速傅里叶变换」（FFT）就是计算上述公式的高效算法，把朴素的 \(O(N^2)\) 复杂度降到 \(O(N \log N)\)。`fftpack` 正是用来提供这些 FFT 及相关变换的模块。

### 2.2 什么是「遗留模块」

软件项目里，「legacy」通常指：**功能仍然可用、但官方不再推荐新代码使用、未来可能被移除的旧模块**。它往往被一个更新的、设计更好的模块所替代，但为了不破坏老用户的代码而暂时保留。理解这一点，是读懂 `fftpack` 的关键。

### 2.3 什么是 DeprecationWarning

`DeprecationWarning` 是 Python 标准库的一种警告类别。当一段代码被标记为「弃用」时，运行时调用它会发出这个警告，提醒你「这个东西未来会被删掉，别再用了」。本讲后面会强调一个关键区别：**`scipy.fftpack` 被标为 legacy（文档层），并不等于你每次 `import` 它都会触发 `DeprecationWarning`（运行层）**。

## 3. 本讲源码地图

本讲只聚焦一个文件，它是整个模块的「门面」：

| 文件 | 作用 |
| --- | --- |
| [`__init__.py`](__init__.py) | `scipy.fftpack` 包的入口。它通过一段文档字符串声明了自己的 legacy 身份，并用一系列 `import` 把各个子模块的函数聚合导出为公共 API。 |

为了讲清楚「遗弃导入为什么会报警」，本讲还会顺带引用两个辅助文件：

| 文件 | 作用 |
| --- | --- |
| [`basic.py`](basic.py) | 一个 **shim（垫片）模块**，专门用来拦截 `from scipy.fftpack.basic import ...` 这种遗弃写法，并发出弃用警告。 |
| `scipy/_lib/deprecation.py` | SciPy 公共的弃用工具函数 `_sub_module_deprecation`，shim 模块最终委托给它来发出警告。 |

## 4. 核心概念与源码讲解

本讲围绕三个最小模块展开：

- 4.1 模块文档字符串与 `legacy` 指令（本讲核心）
- 4.2 公共 API 的聚合导出机制（解释为什么 `from scipy.fftpack import fft` 是合法的）
- 4.3 遗弃命名空间 shim 与 `DeprecationWarning`（解释报警的真正来源）

### 4.1 模块文档字符串与 legacy 指令

#### 4.1.1 概念说明

每个 Python 模块的文件开头通常有一段用三引号包裹的字符串，称为**模块文档字符串（module docstring）**。它是这个模块的「自我介绍」：写明模块叫什么、提供哪些功能、有什么注意事项。对 `scipy.fftpack` 而言，这段 docstring 不仅是给人读的文档，还会被 SciPy 的文档构建工具（Sphinx）自动抓取，生成官方手册页面。所以**模块的身份声明（包括「我是遗留模块」）就写在 docstring 里**。

`scipy.fftpack` 是 SciPy 较早提供的傅里叶变换模块，名字里的 `fftpack` 来源于历史上经典的 **FFTPACK** Fortran 库。随着项目演进，SciPy 引入了一个全新的模块 `scipy.fft`，它在 API 设计、多线程支持、后端切换等方面都更现代，于是 `fftpack` 就从「主力模块」退化为「遗留模块」。

#### 4.1.2 核心流程

整个身份声明只占文档字符串的开头几行，逻辑非常简单：

1. `__init__.py` 以一个三引号字符串作为模块级文档字符串（docstring）。
2. 文档字符串开头先写模块标题。
3. 紧接着用 `.. legacy::` 指令声明这是遗留模块。
4. 指令体内用一句话给出官方建议：「新代码应使用 `scipy.fft`」。
5. 随后才是正常的功能清单（FFT、DCT、DST、辅助函数、卷积等）。

整条链路**只发生在文档层面**，不会影响运行时行为。

#### 4.1.3 源码精读

先看 `__init__.py` 文档字符串的开头：

[__init__.py:1-11](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/__init__.py#L1-L11)

这段是模块的「身份证」。第 3 行明确写着 `Legacy discrete Fourier transforms`，第 6-8 行就是关键的 legacy 指令：

```python
.. legacy::

   New code should use :mod:`scipy.fft`.
```

这里用了 reStructuredText（rst）的语法：`.. legacy::` 是一个指令，缩进的内容是它的正文。`:mod:`scipy.fft`` 是一个交叉引用，会被渲染成指向新版模块的链接。一句话总结：**官方的建议就是——新代码请用 `scipy.fft`，别再用 `fftpack`。** 这两行是整篇讲义最重要的代码点。

文档字符串中还有一句容易被忽略、但对正确使用很重要的提示：

[__init__.py:62-63](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/__init__.py#L62-L63)

它告诉我们：`fftshift`、`ifftshift`、`fftfreq` 这三个辅助函数其实是 numpy 的函数，`fftpack` 只是「再导出」了它们；官方建议直接从 numpy 导入。

#### 4.1.4 代码实践

**实践目标**：亲手打开 `__init__.py`，找到 legacy 指令并理解它在文档中的位置。

**操作步骤**：

1. 用编辑器打开 `scipy/fftpack/__init__.py`，定位第 1–11 行。
2. 在 Python 中以「查看文档字符串」的方式确认你读到的内容（**示例代码**）：

```python
# 示例代码：打印 fftpack 的模块文档字符串开头
import scipy.fftpack as fftpack
print(fftpack.__doc__[:200])
```

**需要观察的现象**：打印出的字符串应以 `Legacy discrete Fourier transforms` 开头，并包含 `.. legacy::` 字样。

**预期结果**：文档字符串的前 200 个字符里能看到「Legacy」「legacy」「New code should use」等关键词。你也可以再去 SciPy 官方文档网站搜索 `scipy.fftpack`，对比网页顶部的遗留横幅与源码里的指令是否一一对应。若无法访问网络，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`.. legacy::` 指令的正文只有一句话，它建议使用哪个模块？

> **参考答案**：建议使用 `scipy.fft`，原文为 `New code should use :mod:`scipy.fft`.`。

**练习 2**：为什么 `fftshift` 不算 `fftpack` 自己实现的函数？

> **参考答案**：因为根据 `__init__.py:62-63` 的说明，`fftshift`、`ifftshift`、`fftfreq` 都是 numpy 提供的函数，`fftpack` 只是再导出，官方建议直接 `from numpy.fft import fftshift`。

### 4.2 公共 API 的聚合导出机制

#### 4.2.1 概念说明

理解了「遗留身份」之后，下一个问题是：既然它是遗留的，那 `from scipy.fftpack import fft` 到底还合不合法、会不会报警？要回答这个问题，必须看 `__init__.py` 是如何把函数导出给用户的。

`fftpack` 把真正的实现分散在几个下划线开头的「私有」子模块里（如 `_basic.py`），然后在 `__init__.py` 里用 `from ._basic import *` 这种**聚合导入**把它们一次性搬进包的命名空间。这种「实现私有、导出公开」的组织方式，正是「合法导入」与「遗弃导入」的分界线。

#### 4.2.2 核心流程

1. `__all__` 列表声明包的公共名称。
2. 一组 `from ._xxx import *` 把私有子模块里的函数聚合进包命名空间。
3. 用户写 `from scipy.fftpack import fft`，命中的就是这条聚合导入，**合法、不报警**。

#### 4.2.3 源码精读

先看公共名称清单：

[__init__.py:81-91](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/__init__.py#L81-L91)

`__all__` 定义了「`from scipy.fftpack import *` 时会被导出的名字」，大致分成几类：复数 FFT（`fft`/`ifft`/`fftn`/`ifftn`/`fft2`/`ifft2`）、实数 FFT（`rfft`/`irfft`）、伪微分算子（`diff`/`hilbert` 等）、辅助函数（`fftfreq`/`fftshift`/`next_fast_len`）和实变换（`dct`/`dst` 等）。

再看聚合导入：

[__init__.py:93-96](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/__init__.py#L93-L96)

```python
from ._basic import *
from ._pseudo_diffs import *
from ._helper import *
from ._realtransforms import *
```

这就是答案的来源：`fft` 实际定义在 `_basic.py`，通过 `from ._basic import *` 被搬进 `scipy.fftpack` 命名空间。这条路径上没有任何 `warnings.warn` 调用，所以 **`from scipy.fftpack import fft` 是官方支持的、不会触发弃用警告的导入路径**。

#### 4.2.4 代码实践

**实践目标**：验证 `from scipy.fftpack import fft` 是否会产生 `DeprecationWarning`。

**操作步骤**（**示例代码**，非项目原有文件）：

```python
import warnings
warnings.simplefilter("error", DeprecationWarning)  # 把弃用警告升级为异常

from scipy.fftpack import fft   # 这是支持的路径
import numpy as np

x = np.array([0.0, 1.0, 0.0, -1.0])
print("fft =", fft(x))
```

**需要观察的现象**：第 4 行的 `from scipy.fftpack import fft` 是否会抛出 `DeprecationWarning` 异常。

**预期结果**：不会抛异常。`fft(x)` 正常输出一个复数数组，这就是 `fftpack` 的标准打包顺序（第 0 个分量是直流分量/0 频率，随后是正频率，最后是负频率）。更复杂的 FFT 用法会在本单元第 4 讲（`u1-l4`）展开。

#### 4.2.5 小练习与答案

**练习 1**：`fft` 这个名字在 `__all__` 里，它实际定义在哪个文件？

> **参考答案**：定义在 `_basic.py`（私有模块），通过 `__init__.py:93` 的 `from ._basic import *` 聚合导出。

**练习 2**：为什么把实现放在下划线开头的 `_basic.py`、再聚合到 `__init__.py`，比直接写在 `__init__.py` 里更好？

> **参考答案**：下划线前缀在 Python 社区约定中表示「私有/内部」。这样可以把实现细节与公共 API 分离：用户只被鼓励使用包级别的公共名称，而内部子模块结构可以自由重构，甚至（如下一节所示）被改造成弃用垫片。

### 4.3 遗弃命名空间 shim 与 DeprecationWarning

#### 4.3.1 概念说明

历史上有不少用户写了 `from scipy.fftpack.basic import fft` 这种**从子模块导入**的写法。虽然 `basic`（不带下划线）看起来像公共模块，但它原本只是实现细节，SciPy 并不打算长期维护它。为了既提醒老用户、又不立即让他们的代码崩溃，SciPy 保留了 `basic.py` 等不带下划线的同名文件，但把它们改造成 **shim（垫片）**：表面上还能导入，实际上会发出 `DeprecationWarning`，并在未来版本（SciPy 2.0.0）彻底移除。

这是初学者最容易踩的认知坑：**「被标为 legacy（文档层）」和「调用就报警告（运行层）」是两回事。**

#### 4.3.2 核心流程

弃用警告的触发链路如下：

```
用户写：from scipy.fftpack.basic import fft
  └─ Python 导入 basic 模块（basic.py 这个 shim）
      └─ basic.py 的 __all__ 里有 'fft'，但模块本身不定义它
          └─ Python 调用模块级 __getattr__('fft')
              └─ __getattr__ 调用 _sub_module_deprecation(...)
                  └─ warnings.warn(..., category=DeprecationWarning)  发出警告
                      └─ 同时把真正的 fft 返回给用户（功能仍可用）
```

也就是说：**功能仍然能用，但你会收到一条明确的弃用警告**。

#### 4.3.3 源码精读

先看 `__init__.py` 里挂载垫片的注释与导入：

[__init__.py:98-99](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/__init__.py#L98-L99)

注释 `# Deprecated namespaces, to be removed in v2.0.0` 直接点明：`basic`、`helper`、`pseudo_diffs`、`realtransforms` 这四个不带下划线的命名空间是弃用的，将在 v2.0.0 移除。

再看 `basic.py` 这个 shim 的内容：

[basic.py:1-20](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/basic.py#L1-L20)

第 1 行就声明 `This file is not meant for public use and will be removed in SciPy v2.0.0`。关键是第 17-20 行的 `__getattr__`：

```python
def __getattr__(name):
    return _sub_module_deprecation(sub_package="fftpack", module="basic",
                                   private_modules=["_basic"], all=__all__,
                                   attribute=name)
```

模块级 `__getattr__` 是 PEP 562 引入的特性：当访问模块里**不存在的属性**时，Python 会调用它。shim 正是利用这一点来拦截 `from scipy.fftpack.basic import fft`。

最终委托的弃用函数（位于 SciPy 公共工具库）会构造提示信息并发出警告：

[deprecation.py:53-68](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/deprecation.py#L53-L68)

第 54-58 行构造提示信息「请从 `scipy.fftpack` 命名空间导入」，第 68 行用 `category=DeprecationWarning` 正式发出警告。注意这个文件不在 `fftpack` 目录里，而是 SciPy 公共工具库 `scipy/_lib/deprecation.py`——所有子包的弃用垫片都复用它。

#### 4.3.4 代码实践

**实践目标**：对比「合法导入」与「遗弃导入」在警告行为上的差异，亲手触发一条 `DeprecationWarning`。这是本讲指定的核心实践。

**操作步骤**（**示例代码**，非项目原有文件）：

```python
import warnings
warnings.simplefilter("error", DeprecationWarning)  # 把弃用警告升级为异常

try:
    from scipy.fftpack.basic import fft   # 遗弃路径：走 shim 的 __getattr__
    print("没有报警（意外）")
except DeprecationWarning as w:
    print("收到 DeprecationWarning：", str(w)[:100])
```

**需要观察的现象**：`from scipy.fftpack.basic import fft` 是否会抛出 `DeprecationWarning`；提示信息里是否包含「`scipy.fftpack.basic` namespace is deprecated」和「removed in SciPy 2.0.0」。

**预期结果**：会抛出 `DeprecationWarning`，异常信息指向应改用 `scipy.fftpack` 命名空间。这与 4.2.4 中 `from scipy.fftpack import fft` 不报警形成鲜明对照——**报警的不是 `fftpack` 本身，而是被弃用的子模块命名空间 `basic`**。

> 说明：是否报警由源码逻辑（`basic.py` 的 `__getattr__` 与 `_sub_module_deprecation`）决定，结论可靠；但你机器上的具体警告文本可能随 SciPy 版本略有差异。本讲编写环境无法直接运行 SciPy，请本地运行核对，结果若不一致请标注「待本地验证」并记录你的 SciPy 版本号（`import scipy; print(scipy.__version__)`）。

#### 4.3.5 小练习与答案

**练习 1**：同样是「导入 `fft`」，`from scipy.fftpack import fft` 和 `from scipy.fftpack.basic import fft` 的区别是什么？

> **参考答案**：前者命中 `__init__.py` 的聚合导入 `from ._basic import *`，是支持路径、不报警；后者命中 `basic.py` 这个弃用 shim 的 `__getattr__`，会触发 `DeprecationWarning`。

**练习 2**：为什么 SciPy 不直接删掉 `basic.py`，而要保留一个会报警的垫片？

> **参考答案**：为了向后兼容。直接删除会让大量老代码立刻崩溃；保留垫片能在「给出明确迁移提示」的同时，给社区一个过渡期，到 v2.0.0 再真正移除。

**练习 3**：如果你接手维护一个老项目，里面写满了 `from scipy.fftpack import fft`，短期内需要立刻重写吗？

> **参考答案**：不必。公开 API `from scipy.fftpack import fft` 仍被支持、不报警告、未被移除，短期可继续运行。但长期应在新代码中改用 `scipy.fft`，并逐步迁移。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「身份与迁移」小任务：

1. **阅读源码**：打开 [`__init__.py`](__init__.py)，用自己的话写一段 3-5 行的说明，回答：`scipy.fftpack` 是什么？为什么它是 legacy？官方建议新代码用谁？给出 `.. legacy::` 指令所在的行号（第 6-8 行）作为依据。

2. **对比两个模块**：查阅 `scipy.fft`（新版）的 `__init__.py` 文档字符串标题，指出它是否带有「Legacy」字样。结合本讲所学，写一句话总结 `scipy.fftpack` 与 `scipy.fft` 的关系（遗留 vs 主力）。

3. **动手验证报警差异**：编写一个脚本，分别测试三种导入方式，并用 `warnings.catch_warnings(record=True)` 收集警告，整理成一张三行的小表格：
   - `from scipy.fftpack import fft`（合法）
   - `from scipy.fftpack.basic import fft`（遗弃命名空间）
   - `from scipy import fft as new_fft`（新版模块）

   预期：只有第二种会收到 `DeprecationWarning`。

4. **思考迁移**：如果你接手的项目里大量使用 `from scipy.fftpack.basic import fft`，根据本讲学到的源码知识，你会建议团队怎么迁移？至少说出两个要点（提示：迁移到哪个命名空间、为什么不会丢失功能）。

## 6. 本讲小结

- `scipy.fftpack` 是 SciPy 的**遗留**傅里叶变换模块，`__init__.py` 用 `.. legacy::` 指令（第 6-8 行）声明了这一身份。
- 官方明确建议：**新代码应使用 `scipy.fft`**，`fftpack` 仅为兼容老代码而保留。
- `fft`、`dct` 等公共函数实际定义在 `_basic.py` 等私有子模块，通过 `from ._basic import *`（第 93-96 行）聚合导出；`from scipy.fftpack import fft` 是**合法、不报警**的路径。
- 报警的真正来源是 `basic`/`helper` 等**不带下划线的遗弃命名空间**：它们是 `basic.py` 这样的 shim，靠模块级 `__getattr__` 委托 `_sub_module_deprecation` 发出 `DeprecationWarning`（`deprecation.py:68`），并将在 SciPy v2.0.0 移除。
- **遗留（文档层）≠ 弃用警告（运行层）**：legacy 标记只是文档建议，不影响公开 API 的运行行为。
- `fftshift`/`fftfreq` 等其实是 numpy 的函数，`fftpack` 只是再导出（第 62-63 行）。

## 7. 下一步学习建议

下一讲（`u1-l2` 目录结构与构建配置）将带你俯瞰整个 `fftpack` 目录：区分核心模块、shim 模块、Cython 扩展与测试，并理解 `meson.build` 是如何把这些文件编译安装的。

如果你已经迫不及待想写代码，可以先跳到 `u1-l4`（快速上手：一维复数 FFT）动手跑一个真正的 FFT 示例；但建议先按顺序读完本单元的目录与 API 讲解（`u1-l2`、`u1-l3`），建立起对项目结构的整体印象，再深入具体函数会轻松很多。

> 阅读提示：在进入下一篇之前，建议你先在本地把 4.3.4 节的脚本跑一遍，带着真实的运行印象继续学习，效果会更好。
