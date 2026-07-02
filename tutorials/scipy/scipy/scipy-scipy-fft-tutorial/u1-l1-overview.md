# scipy.fft 是什么：定位与公共 API 全景

## 1. 本讲目标

本讲是整本 `scipy.fft` 学习手册的第一篇，目标只有三个：

1. 说清楚 `scipy.fft` 这个子包在 SciPy 里**到底是什么、为什么存在**，它和 `numpy.fft`、`scipy.fftpack` 是什么关系。
2. 把 `scipy.fft` 暴露给用户的**全部公共函数**理一遍，并按照官方文档的五大分类（FFT / 实变换 / Hankel / 辅助 / 后端）建立一张「全景地图」。
3. 学会只读 `__init__.py` 这一个文件，就能推断出一个 Python 包对外提供了哪些能力。

本讲**不**深入任何变换的算法细节，也不讲解后端分派机制——那些是后续讲义的主题。本讲只做一件事：**先看清整片森林**。

---

## 2. 前置知识

### 2.1 什么是傅里叶变换

如果你已经熟悉 DFT/FFT，可以跳过本节。

一段离散信号 \(x[0], x[1], \dots, x[N-1]\) 的**离散傅里叶变换（DFT）**定义为：

\[
X[k] = \sum_{n=0}^{N-1} x[n] \, e^{-2\pi i \, k n / N}, \quad k = 0, 1, \dots, N-1
\]

它把信号从「时域／空域」转换到「频域」，即把信号拆成不同频率的正弦／余弦分量的叠加。逆变换（IDFT）则反过来由 \(X[k]\) 还原 \(x[n]\)。

直接按定义计算 DFT 需要 \(O(N^2)\) 次乘法。**快速傅里叶变换（FFT）**是一类把复杂度降到 \(O(N \log N)\) 的算法，因此工程上「做一次傅里叶变换」几乎都指调用 FFT。

`scipy.fft` 就是 SciPy 提供给你的、用来调用这些快速变换的高层接口。

### 2.2 三个名字很像的模块

初学者最容易混淆三个名字：

| 模块 | 出自 | 角色 |
|------|------|------|
| `numpy.fft` | NumPy | NumPy 自带的 FFT，函数名基础（`fft`/`ifft`/`rfft`/`fft2`/`fftn` 等） |
| `scipy.fftpack` | SciPy（旧） | 基于 Fortran 的 FFTPACK 的老接口，仍可用，但官方推荐迁移 |
| `scipy.fft` | SciPy（新） | 自 SciPy 1.4 起引入的**统一、现代** FFT 子包，本手册的主角 |

它们的关键区别（后续讲义会逐一验证）：

- **`scipy.fft` 是 `numpy.fft` 的「超集」**：`numpy.fft` 里有的函数名（`fft`、`ifft`、`rfft`、`hfft`、`fftfreq`、`fftshift` 等），`scipy.fft` 基本都有，且同名同语义。
- **`scipy.fft` 多了三类能力**：实变换 DCT/DST、快速 Hankel 变换 FFTLog、以及可插拔的「后端（backend）」机制。
- **`scipy.fft` 的函数多了两个常用参数**：`workers`（多线程并行）和系统化的 `norm`（归一化模式），这些 `numpy.fft` 没有。
- **`scipy.fftpack` 是历史遗留**：命名混乱（如 `dct` 的默认类型、归一化约定都和现代直觉不同），`scipy.fft` 修正了这些默认行为，是官方推荐的现代写法。

一句话记忆：**要写新的 FFT 代码，优先 `scipy.fft`。**

### 2.3 你需要的一点 Python 基础

- 会 `import` 一个模块、调用其中的函数。
- 知道 `__init__.py` 是一个 Python「包（package）」的入口文件。
- 大致了解 `__all__` 这个特殊变量的作用（下面 4.2 会展开讲）。

---

## 3. 本讲源码地图

本讲只盯住**一个文件**：

| 文件 | 行数 | 作用 |
|------|------|------|
| [`scipy/fft/__init__.py`](__init__.py) | 115 行 | `scipy.fft` 子包的入口。它只做两件事：用一段**模块文档字符串**把全部公共函数分成五类展示，再用一组 `from ... import` 把这些函数真正导入到 `scipy.fft` 命名空间，最后用一个 `__all__` 列表声明「对外公开的名字」。 |

虽然入口文件只有 100 多行，但它**是一张索引**：每个 `from ._xxx import ...` 都指向一个子模块（`_basic`、`_realtransforms`、`_fftlog`、`_helper`、`_backend`、`_duccfft.helper`），这些子模块才是后续讲义的主角。本讲先看懂这张索引。

---

## 4. 核心概念与源码讲解

### 4.1 模块文档字符串：scipy.fft 提供什么

#### 4.1.1 概念说明

每个 Python 模块文件开头那段用三引号包起来的字符串，叫**模块文档字符串（module docstring）**。它有两个用途：

1. 给人看：在交互式环境里 `help(scipy.fft)` 或 `print(scipy.fft.__doc__)` 时显示出来。
2. 给工具看：SciPy 用 [Sphinx](https://www.sphinx-doc.org/) 自动生成官方文档，`__init__.py` 的 docstring 就是 [SciPy FFT 文档页](https://docs.scipy.org/doc/scipy/reference/fft.html) 的直接来源。

`scipy.fft` 的 docstring 特别重要，因为它**把全部公共函数按功能分成了五大类**，这正是本讲要建立的「全景地图」的官方依据。

文档里那些 `.. autosummary::` 是 Sphinx 的指令，作用是「自动生成下面这些函数的摘要表格」——你可以把它理解成一个「函数清单声明」。

#### 4.1.2 核心流程

docstring 的组织流程可以概括为：

```
模块标题
  └─ 5 段 autosummary 清单（= 5 大功能分类）
       ├─ Fast Fourier Transforms (FFTs)      → 复/实/Hermitian 变换
       ├─ Discrete Sin and Cosine Transforms  → DCT / DST（实变换）
       ├─ Fast Hankel Transforms              → Hankel 变换
       ├─ Helper functions                    → 频率/移位/长度/worker 辅助
       └─ Backend control                     → 后端控制
```

这五类的划分**不是随意的**，而是按「数学性质」分组：

- **FFT 类**：输入／输出都在「复数频域」里打转（含实输入 `rfft`、Hermitian 谱 `hfft` 等变体）。
- **DCT/DST 类**：输入是实数，输出也是实数，用的是「余弦／正弦」基（常见于图像、音频压缩）。
- **Hankel 类**：在对数空间做的特殊变换，天体物理、统计里用得多。
- **helper 类**：本身不做变换，而是配合变换使用（生成频率轴、把零频移到中心、寻找最优补零长度、控制并行线程数）。
- **backend 类**：控制「变换实际由谁来计算」，是 `scipy.fft` 区别于 `numpy.fft` 的核心机制。

#### 4.1.3 源码精读

先看 docstring 的开头，它点明了这个子包的名字和定位：

[\`__init__.py\`:L1-L6](__init__.py#L1-L6) —— 模块文档字符串开头，标题声明这是 `scipy.fft`（离散傅里叶变换子包）。

**第一类：FFT 变换**（共 18 个函数，是数量最多的一类）：

[\`__init__.py\`:L8-L31](__init__.py#L8-L31) —— `Fast Fourier Transforms` 段落，列出了 `fft`/`ifft`/`fft2`/`ifft2`/`fftn`/`ifftn`（复数，1D/2D/ND）、`rfft`/`irfft`/`rfft2`/`irfft2`/`rfftn`/`irfftn`（实输入）、`hfft`/`ihfft`/`hfft2`/`ihfft2`/`hfftn`/`ihfftn`（Hermitian 谱）。命名规律：前缀 `r` 表示 real（实输入），前缀 `h` 表示 Hermitian；后缀 `2`/`n` 表示维度。

**第二类：实变换 DCT/DST**（共 8 个函数）：

[\`__init__.py\`:L33-L46](__init__.py#L33-L46) —— `Discrete Sin and Cosine Transforms` 段落，列出 `dct`/`idct`/`dctn`/`idctn`（余弦）和 `dst`/`idst`/`dstn`/`idstn`（正弦）。这些是 `numpy.fft` **没有**的能力。

**第三类：快速 Hankel 变换**（共 2 个函数）：

[\`__init__.py\`:L48-L55](__init__.py#L48-L55) —— `Fast Hankel Transforms` 段落，只有 `fht`（正变换）和 `ifht`（逆变换）。

**第四类：辅助函数**（共 9 个）：

[\`__init__.py\`:L57-L71](__init__.py#L57-L71) —— `Helper functions` 段落，包含频率轴 `fftfreq`/`rfftfreq`、移位 `fftshift`/`ifftshift`、最优长度 `next_fast_len`/`prev_fast_len`、Hankel 专用 `fhtoffset`，以及控制并行线程的 `set_workers`/`get_workers`。

**第五类：后端控制**（共 4 个）：

[\`__init__.py\`:L73-L83](__init__.py#L73-L83) —— `Backend control` 段落，列出 `set_backend`/`skip_backend`（局部作用域）和 `set_global_backend`/`register_backend`（全局／永久）。

> 小观察：`fhtoffset` 虽然和 Hankel 变换相关，但 docstring 把它归在 **Helper** 类，而不是 Hankel 类——因为它是「辅助计算」而非「变换本身」。这种分类细节在 4.2 的实践里会再次出现。

#### 4.1.4 代码实践

**实践目标**：用 `print(scipy.fft.__doc__)` 亲眼看到这段 docstring，确认它就是官方文档的来源。

**操作步骤**（示例代码，非项目原有代码）：

```python
# 示例代码：在 Python REPL 或脚本中运行
import scipy.fft

# 打印前 30 行文档字符串，对照本讲 4.1.3 引用的源码
for i, line in enumerate(scipy.fft.__doc__.splitlines()[:30], start=1):
    print(f"{i:>3}: {line}")
```

**需要观察的现象**：输出的第 1~6 行应当是标题块，第 8 行附近出现 `Fast Fourier Transforms (FFTs)`，第 14 行附近开始是 `fft - Fast (discrete) Fourier Transform (FFT)` 这样的「函数名 - 一句话说明」清单。

**预期结果**：你看到的文本和 [`__init__.py`](__init__.py#L1-L84) 里第 1~84 行的 docstring **逐字一致**，证明官方网页文档就是由这段字符串生成的。

**运行结果**：待本地验证（取决于你安装的 SciPy 版本，但内容应与本讲引用的源码一致）。

#### 4.1.5 小练习与答案

**练习 1**：docstring 里第一类（FFT）一共列了多少个函数？其中名字带 `r` 前缀的有哪些，分别是什么含义？

> **参考答案**：18 个。带 `r` 前缀的有 `rfft`、`irfft`、`rfft2`、`irfft2`、`rfftn`、`irfftn` 共 6 个，`r` 代表 real（实数输入），它们只输出「半谱」以利用实信号的共轭对称性、节省计算和存储。

**练习 2**：`next_fast_len` 属于哪一类？为什么它不放在 FFT 类里？

> **参考答案**：属于 Helper 类。因为它本身**不做**傅里叶变换，只是帮你算出一个「对 FFT 友好的补零长度」（把长度凑成小素数之积以加速），是配合 `fft` 使用的辅助工具。

---

### 4.2 scipy.fft 公共 API：导入结构与 `__all__`

#### 4.2.1 概念说明

光有 docstring 还不够——docstring 只是「说明书」，真正让 `scipy.fft.fft` 这个名字「能用」的，是 `__init__.py` 里的两组代码：

1. **`from ... import`**：从子模块把函数**搬进** `scipy.fft` 命名空间。例如 `from ._basic import fft, ifft, ...` 让 `scipy.fft.fft` 指向 `_basic` 模块里定义的 `fft`。
2. **`__all__`**：一个字符串列表，声明「`from scipy.fft import *` 时应该导出哪些名字」。它同时是「这是我们的公开 API」的契约——不在 `__all__` 里的名字（比如 `_basic`、`_duccfft`）被视为内部实现，用户不应依赖。

这两个机制合在一起回答了一个问题：**用户能 `scipy.fft.xxx` 调用的，到底是哪些 `xxx`？** 答案就是 `__all__` 里的 41 个名字。

#### 4.2.2 核心流程

公共 API 的「组装流程」可以画成一条流水线：

```
子模块（实现）              __init__.py（搬运+声明）           用户调用
─────────────────         ────────────────────────         ─────────────
_basic.py            ─┐
_realtransforms.py   ─┼─►  from ._xxx import ...   ─►  __all__ = [...]  ─►  scipy.fft.fft(...)
_fftlog.py           ─┤                                  scipy.fft.dct(...)
_helper.py           ─┤                                  scipy.fft.fht(...)
_backend.py          ─┤                                  scipy.fft.fftfreq(...)
_duccfft/helper.py   ─┘                                  scipy.fft.set_backend(...)
```

注意一个**关键分工**：`__init__.py` 自己几乎不写算法，它只是「组装车间」。真正干活的算法在 `_duccfft`（C 扩展 `pyduccfft`），分派逻辑在 `_backend`，这些是后续讲义（u4/u5）的内容。本讲只要记住：**入口文件是索引，不是实现。**

#### 4.2.3 源码精读

**导入语句**（把子模块的函数搬进来）：

[\`__init__.py\`:L86-L97](__init__.py#L86-L97) —— 六组 `from ._xxx import ...`，分别从 `_basic`（18 个 FFT 函数）、`_realtransforms`（8 个 DCT/DST）、`_fftlog`（`fht`/`ifht`/`fhtoffset`）、`_helper`（6 个辅助）、`_backend`（4 个后端控制）、`_duccfft.helper`（`set_workers`/`get_workers`）导入。

注意一个细节：`fhtoffset` 来自 `_fftlog`（第 91 行），`set_workers`/`get_workers` 来自 `_duccfft.helper`（第 97 行）——虽然 docstring 把它们都归在 Helper 类，但它们的「实现出处」各不相同。这印证了「**文档分类 ≠ 代码出处**」。

**`__all__` 声明**（公开 API 契约）：

[\`__init__.py\`:L99-L109](__init__.py#L99-L109) —— 一个扁平的字符串列表，列出全部 41 个公开名字。它和 docstring 的五大分类**内容一致，但顺序不同**：`__all__` 是按「先 FFT、再 helper、再 DCT/DST、再 Hankel、最后 backend」的顺序排的，并不严格按 docstring 的五段顺序。这也是为什么实践任务需要你「手动分组」。

**测试入口**（附加能力）：

[\`__init__.py\`:L112-L114](__init__.py#L112-L114) —— 引入 `PytestTester` 并绑定成 `scipy.fft.test`，让你能直接 `scipy.fft.test()` 跑这个子包自带的测试套件。这是 SciPy 每个子包的标准惯例。

#### 4.2.4 代码实践（本讲核心实践）

**实践目标**：编写脚本，打印 `scipy.fft.__all__`，并按 docstring 的五大分类（FFT / DCT-DST / Hankel / helper / backend）分组输出，验证每一个名字都能对上号。

**操作步骤**（示例代码，非项目原有代码）：

```python
# 示例代码：按五大分类整理 scipy.fft 的公共 API
import scipy.fft

# 按 __init__.py 模块文档字符串(docstring L8-L83)的五大分类手动定义分组
groups = {
    "1) FFT（复/实/Hermitian 变换，来自 _basic）": [
        'fft', 'ifft', 'fft2', 'ifft2', 'fftn', 'ifftn',
        'rfft', 'irfft', 'rfft2', 'irfft2', 'rfftn', 'irfftn',
        'hfft', 'ihfft', 'hfft2', 'ihfft2', 'hfftn', 'ihfftn',
    ],
    "2) DCT/DST（离散余弦/正弦变换，来自 _realtransforms）": [
        'dct', 'idct', 'dctn', 'idctn', 'dst', 'idst', 'dstn', 'idstn',
    ],
    "3) Hankel（快速 Hankel 变换，来自 _fftlog）": [
        'fht', 'ifht',
    ],
    "4) helper（频率/移位/长度/worker 辅助，来自 _helper/_fftlog/_duccfft）": [
        'fftfreq', 'rfftfreq', 'fftshift', 'ifftshift',
        'next_fast_len', 'prev_fast_len', 'fhtoffset',
        'set_workers', 'get_workers',
    ],
    "5) backend（后端控制，来自 _backend）": [
        'set_backend', 'skip_backend', 'set_global_backend', 'register_backend',
    ],
}

public = set(scipy.fft.__all__)
classified = set()
for name, members in groups.items():
    print(f"\n== {name}  ({len(members)} 个) ==")
    for m in members:
        ok = "OK" if m in public else "MISSING"
        print(f"   {m:<16} {ok}")
        classified.add(m)

# 校验：__all__ 里的名字是否全部被分到某一类
print("\n---- 校验 ----")
print("scipy.fft.__all__ 总数 :", len(scipy.fft.__all__))
print("已分类总数            :", len(classified))
print("未被分到任何一类的名字 :", sorted(public - classified))
print("分类里有但不在 __all__ 的:", sorted(classified - public))
```

**需要观察的现象**：

1. 五个分类的函数都被标记为 `OK`。
2. 最后的「校验」区显示 `__all__` 总数 = 41，已分类总数 = 41。
3. 「未被分到任何一类的名字」应当是**空列表** `[]`。

**预期结果**：脚本证明 `scipy.fft` 的 41 个公开名字可以**无遗漏、无重复**地归入 docstring 划分的五大类，从而你心里有了一张完整的「全景地图」：18(FFT) + 8(DCT/DST) + 2(Hankel) + 9(helper) + 4(backend) = 41。

**运行结果**：待本地验证（`len(scipy.fft.__all__)` 应为 41；若你装的是更老／更新的版本，个别函数可能略有出入，届时请以本地实际输出为准并回头对照 [`__init__.py`:L99-L109](__init__.py#L99-L109)）。

#### 4.2.5 小练习与答案

**练习 1**：`from scipy.fft import *` 之后，能用 `fftpack` 这个名字吗？为什么？

> **参考答案**：不能。`fftpack` 既不在 `__all__` 里，也不是 `scipy.fft` 子模块导出的名字。`__all__` 是公开 API 的契约，`*` 导入只会取里面的 41 个名字。注意：`scipy.fftpack` 是**另一个独立的子包**（旧版 FFT 接口），和 `scipy.fft` 不是一回事。

**练习 2**：`fhtoffset` 和 `set_workers` 在 docstring 里都属于 Helper 类，但它们的导入来源分别是哪个子模块？

> **参考答案**：`fhtoffset` 来自 `._fftlog`（[第 91 行](__init__.py#L91)），`set_workers` 来自 `._duccfft.helper`（[第 97 行](__init__.py#L97)）。这说明「文档的功能分类」和「代码的物理位置」是两个独立的维度。

**练习 3**：`scipy.fft.test` 是一个函数，但它**没有**出现在 `__all__` 里。这矛盾吗？

> **参考答案**：不矛盾。`test` 在 [第 113 行](__init__.py#L113) 由 `PytestTester(__name__)` 创建，作用是跑测试套件，属于「开发期辅助」而非「数学 API」。不放进 `__all__` 表示它不是核心变换接口的一部分，但因为它被绑定到了模块命名空间，所以仍然可以用 `scipy.fft.test()` 调用。

---

## 5. 综合实践

把本讲的「定位」和「API 全景」串起来，做一个对照实验：**证明 `scipy.fft` 是 `numpy.fft` 的超集**。

任务（示例代码，非项目原有代码）：

```python
# 示例代码：对比 scipy.fft 与 numpy.fft 的公共 API
import numpy.fft as nfft
import scipy.fft as sfft

n_names = set(dir(nfft))          # numpy.fft 里所有名字
s_names = set(sfft.__all__)       # scipy.fft 的公开 API

# 只挑出「看起来像函数」的小写名字做对比，过滤掉私有名和全大写常量
def is_public(name):
    return name.islower() and not name.startswith('_')

shared   = sorted(n for n in s_names if n in n_names and is_public(n))
only_sci = sorted(n for n in s_names if n not in n_names and is_public(n))

print(f"scipy.fft 与 numpy.fft 同名 : {len(shared)} 个")
print("  例:", shared[:8], "...")
print(f"scipy.fft 独有(无 numpy 对应): {len(only_sci)} 个")
print("  例:", only_sci)
```

预期你会看到：`fft`/`ifft`/`rfft`/`fftfreq`/`fftshift` 等是两边**同名共享**的，而 `dct`/`dst`/`fht`/`set_backend`/`next_fast_len`/`set_workers` 等是 `scipy.fft` **独有**的。

这个实验直接印证了本讲的核心结论：**`scipy.fft` = `numpy.fft` 的能力 ＋ 实变换 ＋ Hankel ＋ 后端机制 ＋ workers 并行**。运行结果待本地验证。

---

## 6. 本讲小结

- `scipy.fft` 是 SciPy 自 1.4 起引入的**现代、统一**傅里叶变换子包，是写新代码时的首选，`scipy.fftpack` 是历史遗留。
- 它是 `numpy.fft` 的**超集**：同名同语义的函数都有，另外多了 DCT/DST、Hankel、后端控制、`workers` 并行等能力。
- 入口文件 [`__init__.py`](__init__.py#L1-L115) 只有两件事：一段把全部公共函数分成**五大类**的模块文档字符串（[L1-L84](__init__.py#L1-L84)），和一组把函数真正搬进命名空间的导入（[L86-L97](__init__.py#L86-L97)）加 `__all__` 契约（[L99-L109](__init__.py#L99-L109)）。
- 五大分类共 **41 个公开名字**：18(FFT) + 8(DCT/DST) + 2(Hankel) + 9(helper) + 4(backend)。
- 「文档的功能分类」与「代码的物理出处」是两个维度：例如 `fhtoffset` 属 Helper 类却来自 `_fftlog`，`set_workers` 属 Helper 类却来自 `_duccfft.helper`。
- `__init__.py` 是**索引不是实现**——真正算变换的算法藏在 `_duccfft`（C 扩展 `pyduccfft`）里，这是后续讲义要拆开看的内容。

---

## 7. 下一步学习建议

你已经看清了「森林」，接下来该认识第一棵「树」。建议按以下顺序继续：

1. **先动手用起来**：进入下一讲 [u1-l3 导入、运行与第一次调用](u1-l1-install-and-first-run.md)，亲手调用一次 `scipy.fft.fft`，验证 `fft`/`ifft` 的可逆性。
2. **再看目录全貌**：阅读 [u1-l2 目录结构与四层架构](u1-l2-directory-layout.md)，建立「公共 API → uarray 分派 → 后端 → ducc 核心」的心智模型，弄清本讲反复提到的 `_basic`、`_backend`、`_duccfft` 之间到底什么关系。
3. **想直接查函数用法**：可以跳到 u2 系列（复数 FFT、实/Hermitian 变换、多维变换、辅助函数）按需查阅。

记住本讲的「全景地图」——后续每一篇讲义，本质上都只是在这张地图的某一个分类里深入下去。
