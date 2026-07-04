# 源码目录结构与模块组织

## 1. 本讲目标

上一讲我们从「能力目录」的角度看清了 `scipy.signal` 能做什么。本讲换一个视角，从**磁盘上的文件**入手，带你把 `scipy/signal/` 这个目录彻底看懂。读完本讲你应该能够：

1. 一眼区分 `signal/` 目录里的三类文件：**私有实现模块**（`_xxx.py`）、**弃用 stub 模块**（无下划线前缀的旧名字）、**编译扩展源码**（`.cc` / `.pyx` / Pythran）。
2. 读懂 `meson.build`，并解释「需要被安装的纯 Python 文件」与「需要被编译成二进制扩展的源文件」走的是两条完全不同的构建路径。
3. 理解 `_signal_api.py` 作为「私有实现聚合层」的作用，以及它为什么是「不公开的」。
4. 说清楚 `windows/` 子包与 `tests/` 目录各自的组织方式。

本讲是后续所有讲义的「地图」——后面每一篇讲义都会落到某个具体的 `_xxx.py` 上，本讲帮你建立坐标系。

## 2. 前置知识

在进入源码之前，先用大白话解释几个本讲会反复用到的概念。

- **模块（module）与子包（subpackage）**：在 Python 里，一个 `.py` 文件就是一个模块；一个含有 `__init__.py` 的目录就是一个（子）包。`scipy/signal/` 本身是 `scipy` 的一个子包，而它内部还有一个 `windows/` 子包。
- **私有命名约定**：Python 没有真正的「私有」，但社区约定**以单下划线 `_` 开头的名字表示「内部实现，不要直接依赖」**。`scipy.signal` 把这个约定用到了极致：几乎所有真正的实现都放在 `_xxx.py` 里。
- **编译扩展（compiled extension）**：纯 Python 跑得慢，热点路径会用 C / C++ / Cython / Pythran 写成「扩展模块」，编译后得到一个可以直接 `import` 的二进制 `.so` 文件，对 Python 来说它和普通模块没区别。Cython 源码后缀是 `.pyx`，Pythran 源码仍是 `.py` 但带特殊注释。
- **构建系统 Meson**：SciPy 用 [Meson](https://mesonbuild.com/) 作为构建系统，每个目录下的 `meson.build` 文件告诉构建工具「这个目录里要编译什么、安装什么」。
- **stub（桩）模块**：一个看起来存在、但本身几乎不包含实现，只是把访问「转发」到别处的模块。本讲会看到 `signal/` 里有一批这样的桩，专门用来兼容旧代码。

如果你还没读过上一讲（`u1-l1`），建议先看，本讲多次用到「能力目录」「私有实现模块」「弃用 stub」等上一讲引入的术语。

## 3. 本讲源码地图

本讲涉及的关键文件如下表。注意它们分属不同「类别」，这正是本讲要讲的核心。

| 文件 | 类别 | 作用 |
|------|------|------|
| [`meson.build`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/meson.build) | 构建脚本 | 声明要编译哪些扩展、安装哪些纯 Python 文件、递归进入哪些子目录 |
| [`_signal_api.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signal_api.py) | 私有实现聚合层 | 把所有 `_*` 实现模块的公开名字「收集」到一处，供下游装饰再导出 |
| [`__init__.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py) | 子包入口 | 对外暴露最终 API，并导入弃用 stub |
| [`filter_design.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/filter_design.py) | 弃用 stub（样板） | 旧名字 `scipy.signal.filter_design` 的兼容桩，转发到 `_filter_design` |
| [`windows/meson.build`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/meson.build) | 子包构建脚本 | 单独安装 `windows/` 下的三个 `.py` |

## 4. 核心概念与源码讲解

### 4.1 三类源文件的命名约定

#### 4.1.1 概念说明

打开 `scipy/signal/` 目录，你会看到几十个文件。如果按文件名规律归类，绝大多数都能归到下面三类之一：

1. **私有实现模块**：文件名以 `_` 开头的 `.py` 文件，例如 `_signaltools.py`、`_filter_design.py`。这里**才是真正的算法实现所在**。
2. **弃用 stub 模块**：文件名**没有** `_` 前缀、但和某个私有模块「几乎同名」的小文件，例如 `signaltools.py`（对应 `_signaltools.py`）、`filter_design.py`（对应 `_filter_design.py`）。它们几十行都不到，只是兼容旧导入路径的「转发器」，会在 SciPy v2.0.0 被移除。
3. **编译扩展源码**：扩展名是 `.cc`（C++）、`.pyx`（Cython）、或带 Pythran 注释的 `.py`，例如 `_correlate_nd.cc`、`_sosfilt.pyx`、`_max_len_seq_inner.py`。它们不会被当作普通 Python 安装，而是被编译成二进制扩展。

除了这三类，还有少量辅助文件：`.hh` / `.h` 是 C++ 共享头文件，`.pyi` 是类型存根（stub for type checkers）。

> 小贴士：注意区分两种「stub」——本讲的 stub 模块指**运行时兼容桩**（`.py`），而 `.pyi` 是**静态类型存根**，两者无关。

#### 4.1.2 核心流程

一个公开函数（比如 `butter`）在磁盘上「住在哪里」，可以用下面这张归属图来理解：

```
真正的算法实现        兼容旧路径              对外暴露
─────────────        ─────────              ────────
_filter_design.py ──(转发目标)──> filter_design.py(stub) ──┐
        │                                                  │
        └──────────> _signal_api.py(聚合) ──> __init__.py(暴露 scipy.signal.butter)
```

也就是说：

- **私有实现模块**是「源头」，函数真正写在这里。
- **stub 模块**是「幽灵」：你 `import scipy.signal.filter_design` 时它会出现，但它只是把你「劝退」到新路径并发出弃用警告。
- **`__init__.py`** 才是「正门」，聚合后的名字最终从这里暴露为 `scipy.signal.butter`。

#### 4.1.3 源码精读

先看一个典型的**弃用 stub 模块** `filter_design.py` 的全部内容，它只有 29 行：

[filter_design.py:1-4](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/filter_design.py#L1-L4) 顶部注释明确写「本文件不用于公开，将在 SciPy v2.0.0 移除，请改用 `scipy.signal` 命名空间」。

[filter_design.py:7-18](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/filter_design.py#L7-L18) 定义了 `__all__`，列出该旧模块「曾经」导出的名字（`butter`、`cheby1`、`freqz` 等）——注意这些只是字符串，文件里并没有任何同名函数定义。

[filter_design.py:25-28](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/filter_design.py#L25-L28) 定义了模块级 `__getattr__`。当有人访问 `scipy.signal.filter_design.butter` 时，Python 找不到 `butter` 就会调用这个函数，进而调用 `_sub_module_deprecation(...)`：它去 **`_filter_design`** 里取真正的 `butter`，同时发出 `DeprecationWarning`。关键参数是 `private_modules=["_filter_design"]`，它点明了真正的实现住在哪个私有模块。

`signal/` 目录下一共有 10 个这样的 stub，对应关系如下（全部可在 `__init__.py` 的导入块里看到）：

[__init__.py:322-326](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py#L322-L326) 列出了被显式导入的弃用子模块。

| 弃用 stub 模块 | 转发到的私有实现模块 |
|---|---|
| `signaltools.py` | `_signaltools` |
| `filter_design.py` | `_filter_design` |
| `fir_filter_design.py` | `_fir_filter_design` |
| `ltisys.py` | `_ltisys` |
| `lti_conversion.py` | `_lti_conversion` |
| `spectral.py` | `_spectral_py` |
| `bsplines.py` | `_spline_filters` |
| `spline.py` | `_spline`（编译扩展） |
| `waveforms.py` | `_waveforms` |
| `wavelets.py` | `_wavelets` |

注意 `spline.py` 这个特例：它转发到的 `_spline` 不是 `.py` 文件，而是**编译扩展**（见 4.2 节）。这说明 stub 的转发目标不一定是纯 Python。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目的是让你亲手把三类文件数出来。

1. **实践目标**：在不运行任何代码的前提下，仅凭文件名和上面学到的约定，把 `signal/` 根目录的文件分成三类。
2. **操作步骤**：
   - 在仓库根目录执行下面的命令，列出 `signal/` 下所有 `.py` / `.pyx` / `.cc` / `.hh` / `.h` / `.pyi` 文件。
   - 对每个文件，判断它属于「私有实现 / 弃用 stub / 编译扩展源 / 头文件 / 类型存根」中的哪一类。
   ```bash
   # 列出 signal 根目录（不含子目录）的源文件
   ls scipy/signal/*.py scipy/signal/*.pyx scipy/signal/*.cc scipy/signal/*.hh scipy/signal/*.h scipy/signal/*.pyi
   ```
3. **需要观察的现象**：你会看到大约 23 个 `_xxx.py`（私有实现 + Pythran 源 + 一个 `.pyi`）、10 个无下划线的 `xxx.py`（stub，都很小）、5 个 `.cc`、4 个 `.pyx`、以及 `_sigtools.hh`、`_splinemodule.h` 两个头文件。
4. **预期结果**：无下划线前缀的 `.py` 文件应当**只有 10 个**，且每一个都能在上面的「stub → 私有模块」对照表里找到。如果一个无下划线的 `.py` 文件体积只有 600~1100 字节左右，那它几乎一定是 stub——你可以用 `wc -l scipy/signal/*.py | sort -n` 验证，stub 都会排在最前面、行数极少。

> 本实践不需要运行 Python，重点训练「看文件名识类别」的直觉。

#### 4.1.5 小练习与答案

**练习 1**：`_max_len_seq_inner.py` 和 `_max_len_seq_inner.pyx` 两个文件同名（仅扩展名不同），它们分别是什么？为什么会有两份？

<details><summary>参考答案</summary>

`.py` 那份是 **Pythran** 源（文件里有 `#pythran export ...` 注释），`.pyx` 那份是 **Cython** 源。它们是同一个加速内核 `_max_len_seq_inner` 的两套实现，构建时根据是否启用 Pythran 二选一编译（见 4.2.3）。这是 SciPy「热点函数可同时提供 Pythran 与 Cython 两条编译路径」的典型做法。
</details>

**练习 2**：`bsplines.py`（stub）转发到的私有模块是 `_spline_filters`，而 `spline.py`（stub）转发到的是 `_spline`。这两个目标有什么本质不同？

<details><summary>参考答案</summary>

`_spline_filters` 是一个**纯 Python 模块**（`_spline_filters.py`），而 `_spline` 是一个**编译扩展**（由 `_splinemodule.cc` 编译而来，没有对应的 `_spline.py`）。所以 stub 的转发目标既可以是纯 Python，也可以是编译出来的二进制模块——对使用者透明。
</details>

---

### 4.2 meson.build 的两条安装路径

#### 4.2.1 概念说明

[`meson.build`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/meson.build) 是本目录的「施工图纸」。理解它的关键是认清里面**两条截然不同的路径**：

- **`py3.extension_module(...)`**：声明「把这些源文件**编译**成一个扩展模块（`.so`）」。用于 C/C++/Cython/Pythran 源码。
- **`py3.install_sources([...])`**：声明「把这些 `.py` 文件**原样复制**到安装目录」。用于纯 Python。

这两条路径的产物最终都会出现在你 `site-packages/scipy/signal/` 里，但一个是二进制、一个是文本，构建方式完全不同。本讲要回答的核心问题之一——「哪些模块是纯 Python、哪些有对应的 C/Cython 扩展」——正是靠读这两条路径来回答的。

#### 4.2.2 核心流程

`meson.build` 的整体结构可以概括为三段：

```
第一段：声明 6 个编译扩展（extension_module）
   ├── _sigtools          ← 由 5 个 .cc 源文件合并编译
   ├── _max_len_seq_inner ← Pythran 或 Cython 二选一
   ├── _peak_finding_utils← Cython
   ├── _sosfilt           ← Cython
   ├── _upfirdn_apply     ← Cython
   └── _spline            ← 由 1 个 .cc 编译

第二段：声明要安装的纯 Python 文件（install_sources，34 项）
   __init__.py, _signal_api.py, _filter_design.py, ... 以及 10 个 stub

第三段：递归进入子目录
   subdir('windows')  →  进入 windows/ 子包
   subdir('tests')    →  进入 tests/ 目录
```

注意一个关键细节：**编译扩展源文件（`.cc` / `.pyx` / Pythran 的 `.py`）都不在 `install_sources` 列表里**——它们被编译成二进制后以扩展模块的形式安装，而不是以源码形式安装。这就是为什么 `_max_len_seq_inner.py` 虽然是 `.py`，却不出现在 `install_sources` 中：它是 Pythran 的**输入**，不是要分发的 Python 模块。

#### 4.2.3 源码精读

**（a）多源合并编译：`_sigtools`**

[meson.build:1-14](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/meson.build#L1-L14) 把 5 个 C++ 源文件合并编译成**一个**扩展模块 `_sigtools`：

```meson
py3.extension_module('_sigtools',
  [
    '_firfilter.cc',
    '_sigtoolsmodule.cc',
    '_medianfilter.cc',
    '_lfilter.cc',
    '_correlate_nd.cc'
  ],
  ...
)
```

其中 `_sigtoolsmodule.cc` 承担「模块注册」（定义模块名和方法表），其余 4 个分别是 FIR 滤波、中值滤波、IIR `lfilter`、N-D 相关的内核。它们共享头文件 `_sigtools.hh`。这意味着在 Python 侧只有一个 `import _sigtools`，但底下挂了多种运算。

**（b）Pythran / Cython 双路径：`_max_len_seq_inner`**

[meson.build:16-34](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/meson.build#L16-L34) 用 `if use_pythran ... else ...` 给同一个扩展名 `_max_len_seq_inner` 提供两条编译路径：开启 Pythran 时编译 `_max_len_seq_inner.py`，否则编译 `_max_len_seq_inner.pyx`。两条路径产出的扩展名相同，Python 侧 `from ._max_len_seq_inner import _max_len_seq_inner`（见 `_max_len_seq.py:8`）完全无感。

**（c）Cython 批量编译循环**

[meson.build:36-51](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/meson.build#L36-L51) 用一个 `foreach` 循环把 3 个 `.pyx`（`_peak_finding_utils`、`_sosfilt`、`_upfirdn_apply`）各自编译成同名扩展。这是 Meson 处理「同质化重复定义」的惯用法，比逐个写 `extension_module` 更紧凑。

**（d）纯 Python 安装清单**

[meson.build:63-99](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/meson.build#L63-L99) 的 `py3.install_sources([...])` 是本讲的另一个核心。这份清单共 34 项，可分成三组：

- **子包入口与脚手架**：`__init__.py`、`_signal_api.py`、`_support_alternative_backends.py`、`_delegators.py`。
- **私有实现模块**（`_*` 前缀的 `.py`）：`_arraytools.py`、`_filter_design.py`、`_signaltools.py` 等约 20 个——这些是真正的算法实现。
- **弃用 stub**（无下划线前缀的 `.py`）：`bsplines.py`、`filter_design.py`、`signaltools.py` 等 10 个。

注意 `_spline.pyi` 也在清单里（它是编译扩展 `_spline` 的**类型存根**，给 mypy 等工具用），但 `_spline` 本身的源码 `_splinemodule.cc` 不在这份清单里——它走 `extension_module` 路径，在 [meson.build:53-61](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/meson.build#L53-L61) 单独编译。

#### 4.2.4 代码实践

这是本讲的**主实践**——动手把「Python 实现模块」与「编译扩展」的对应关系整理成表。

1. **实践目标**：列出 `signal/` 目录下所有以 `_` 开头的 Python 文件，并对照 `meson.build`，标注每个模块是「纯 Python」还是「有对应的编译扩展」，给出对应扩展的源文件与语言。
2. **操作步骤**：
   ```bash
   # 步骤 1：列出所有 _ 开头的源文件（根目录）
   ls scipy/signal/_*.py scipy/signal/_*.pyx scipy/signal/_*.cc

   # 步骤 2：在 meson.build 里定位所有 extension_module 的名字
   grep -n "extension_module" scipy/signal/meson.build

   # 步骤 3：在纯 Python 代码里找出谁 import 了这些编译扩展
   grep -rn "from \._sigtools\|from \._sosfilt\|from \._upfirdn_apply\|from \._peak_finding_utils\|from \._max_len_seq_inner\|from \._spline\b\|import _sigtools" scipy/signal/*.py
   ```
3. **需要观察的现象**：
   - 步骤 1 会列出约 23 个 `_*.py`、4 个 `_*.pyx`、6 个 `_*.cc`（其中 `_splinemodule.cc` 单独编译为 `_spline`，其余 5 个 `.cc` 合并为 `_sigtools`）。
   - 步骤 2 会显示 6 处 `extension_module`（`_sigtools`、`_max_len_seq_inner`、`_peak_finding_utils`、`_sosfilt`、`_upfirdn_apply`、`_spline`）。
   - 步骤 3 会显示每个编译扩展被哪个 `_*.py` 导入。
4. **预期结果**：整理出下面这张「Python 模块 ↔ 编译扩展」对照表（这张表是后续讲义的常用索引）：

   | 编译扩展 | 源文件 / 语言 | 被哪个 Python 模块导入 |
   |---|---|---|
   | `_sigtools` | `_firfilter.cc`/`_sigtoolsmodule.cc`/`_medianfilter.cc`/`_lfilter.cc`/`_correlate_nd.cc`（C++） | `_signaltools.py`、`_fir_filter_design.py` |
   | `_max_len_seq_inner` | `_max_len_seq_inner.py`(Pythran) 或 `.pyx`(Cython) | `_max_len_seq.py` |
   | `_peak_finding_utils` | `_peak_finding_utils.pyx`（Cython） | `_peak_finding.py` |
   | `_sosfilt` | `_sosfilt.pyx`（Cython） | `_signaltools.py`（注意：`_sosfilt` **没有**对应的 `.py` 包装，只在 `_signaltools.py:26` 被导入） |
   | `_upfirdn_apply` | `_upfirdn_apply.pyx`（Cython） | `_upfirdn.py` |
   | `_spline` | `_splinemodule.cc`（C++） | `_spline_filters.py`、`_signal_api.py` |

   其余 `_*.py`（如 `_czt.py`、`_filter_design.py`、`_ltisys.py`、`_spectral_py.py`、`_short_time_fft.py` 等）都是**纯 Python**，没有专门的编译扩展。> 待本地验证：上述对照表基于本仓库 HEAD 的源码静态分析得出；若你本地版本不同，请以步骤 3 的 `grep` 实际输出为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_max_len_seq_inner.py` 是个 `.py` 文件，却不出现在 `install_sources` 清单里？

<details><summary>参考答案</summary>

因为它不是要分发的 Python 模块，而是 **Pythran 编译器的输入**。开启 Pythran 时，它被编译成二进制扩展 `_max_len_seq_inner`；编译产物（而非源 `.py`）才会进入安装目录。把它放进 `install_sources` 反而会多余地散播一份「带 Pythran 注释、普通 Python 解释器无法正常使用」的源文件。
</details>

**练习 2**：`_sosfilt` 扩展在 `meson.build` 里被编译，但在磁盘上**没有** `_sosfilt.py`。这合理吗？为什么？

<details><summary>参考答案</summary>

合理。一个编译扩展不强制要求同名 `.py` 包装。`_sosfilt.pyx` 编译出的二进制模块本身就是可 `import` 的，`_signaltools.py:26` 直接 `from ._sosfilt import _sosfilt` 来用。是否再包一层 `.py` 取决于该扩展是否需要在 Python 层做参数校验、文档或预处理——`_sosfilt` 的逻辑足够简单，所以省去了包装层。
</details>

---

### 4.3 _signal_api.py：私有实现的聚合层

#### 4.3.1 概念说明

[`_signal_api.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signal_api.py) 只有 32 行，却地位特殊。它是一个**「裸 API」聚合层**：把分散在二十多个 `_*.py` 里的公开函数「收集」到一个命名空间，方便下游统一处理。

为什么需要这层？因为 `scipy.signal` 现在支持**多后端**（NumPy 之外还有 CuPy、JAX）。如果让 `__init__.py` 直接从各个 `_*.py` 里 `import *`，就没办法在这些函数外面统一「套一层后端委托装饰」。解决办法是：先用 `_signal_api.py` 把所有裸函数聚到一起，再由 `_support_alternative_backends.py` 统一装饰（这部分是 `u1-l4` 和 `u10-l1` 的重点）。

文件顶部文档字符串把这件事说得很直白：

> "This --- private! --- module only collects implementations of public API for _support_alternative_backends."

也就是说，`_signal_api` **本身是私有的**，用户不应直接 `import scipy.signal._signal_api`。

#### 4.3.2 核心流程

聚合的执行流程：

```
二十多个 _*.py 实现模块
        │  (各自 from ._xxx import *)
        ▼
_signal_api.py ─── 用 __all__ = [s for s in dir() if not s.startswith('_')] 自动收集
        │
        ▼
_support_alternative_backends.py（装饰 + 多后端委托）
        │
        ▼
__init__.py（最终暴露为 scipy.signal.*）
```

关键点：`_signal_api.py` 不需要**手写** `__all__`，而是用 `dir()` 过滤掉下划线开头的名字，自动得到「本模块里所有公开名字」。这种写法保证只要实现模块正确导出了公开函数，聚合层就会自动跟上。

#### 4.3.3 源码精读

[_signal_api.py:1-7](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signal_api.py#L1-L7) 文档字符串解释了它与 `_support_alternative_backends`、`__init__.py` 三者的分工。

[_signal_api.py:9-28](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signal_api.py#L9-L28) 是聚合的主体，注意几种不同的导入写法：

```python
from . import _sigtools, windows         # noqa: F401   ← 触发编译扩展与子包初始化
from ._waveforms import *                 # noqa: F403   ← 星号导入（依赖实现模块的 __all__）
from ._max_len_seq import max_len_seq     # noqa: F401   ← 具名导入
from ._spline import sepfir2d             # noqa: F401   ← 直接从编译扩展导入
```

- 第 9 行 `from . import _sigtools, windows`：导入编译扩展 `_sigtools`（确保 C 内核可用）和 `windows` 子包。`_sigtools` 此后并不直接出现在公开 API 里，但它必须被 import 一次以完成模块初始化。
- 大量 `from ._xxx import *`（带 `# noqa: F403`）：依赖每个实现模块自己定义的 `__all__` 来决定导出哪些名字。
- 第 14 行 `from ._spline import sepfir2d`：直接从**编译扩展** `_spline` 取一个函数——再次印证 stub 转发目标可以是编译模块。
- 第 28 行 `from .windows import get_window`：把窗函数的统一入口 `get_window` 也提升到 `scipy.signal` 命名空间（注释 "keep this one in signal namespace"）。

[_signal_api.py:31](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signal_api.py#L31) 自动生成 `__all__`：

```python
__all__: list[str] = [s for s in dir() if not s.startswith('_')]
```

最终，`__init__.py` 通过 `from ._support_alternative_backends import *` 拿到这份 `__all__`：

[__init__.py:316-319](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py#L316-L319) 完成接力，并用 `del _support_alternative_backends, _signal_api, _delegators` 把这些「脚手架」从公开命名空间里删掉——所以用户在 `scipy.signal` 里看不到它们。

#### 4.3.4 代码实践

1. **实践目标**：验证 `_signal_api.py` 的「自动 `__all__`」确实反映了所有实现模块的公开导出。
2. **操作步骤**（在本仓库已构建好 SciPy 的环境下；若未构建，转为纯阅读）：
   ```bash
   # 在 Python 里看 _signal_api 收集到了哪些名字（需已安装/构建 scipy）
   python -c "from scipy.signal import _signal_api as a; print(len(a.__all__), a.__all__[:10])"
   ```
   若环境不可用，则改为阅读：打开任一实现模块（如 `_waveforms.py`），找到它的 `__all__`，再确认这些名字能通过 `_signal_api.py` 的 `from ._waveforms import *` 进入聚合层。
3. **需要观察的现象**：`__all__` 的长度应是公开函数总数（上百个），且列表里**没有任何**以 `_` 开头的名字。
4. **预期结果**：你能看到诸如 `sawtooth`、`chirp`、`butter`、`lfilter` 等公开函数出现在 `__all__` 中，但 `_sigtools`、`windows`（子包）等不会出现（因为 `_sigtools` 以 `_` 开头被过滤；`windows` 虽不以 `_` 开头但它是个子包模块，聚合层只把 `get_window` 提升出来）。> 待本地验证：具体名字与数量取决于本地构建状态。

#### 4.3.5 小练习与答案

**练习 1**：`_signal_api.py` 第 9 行 `from . import _sigtools, windows` 里的 `_sigtools` 之后并没有被任何公开函数直接引用（公开函数都从 `_signaltools` 等模块导入），为什么还要 import 它？

<details><summary>参考答案</summary>

为了**触发编译扩展的加载/初始化**。`_signaltools.py`、`_fir_filter_design.py` 等会在自身被导入时 `import _sigtools`，但 `_signal_api.py` 作为聚合入口显式 import 一次，可以确保 C 扩展在内层模块使用前已就绪，也避免了「某些函数因扩展未加载而失败」的隐式依赖顺序问题。这是一种防御性的初始化顺序保证。
</details>

**练习 2**：`__init__.py:319` 里有一句 `del _support_alternative_backends, _signal_api, _delegators`。既然这三个名字是从 `_support_alternative_backends import *` 来的，怎么还能 `del` 它们？

<details><summary>参考答案</summary>

`__init__.py` 在 `import *` 之后还额外写了 `from . import _support_alternative_backends`（第 317 行），所以 `__init__` 模块自己的命名空间里**确实**绑定了 `_support_alternative_backends` 这个模块对象；同理 `_signal_api`、`_delegators` 也因 `_support_alternative_backends` 内部导入而在 `__init__` 的全局命名空间可见。`del` 的目的是把这些「内部脚手架」从用户可见的 `scipy.signal` 命名空间里清除，保持公开 API 干净。注释里的 `# noqa: F821` 正是为了让静态检查器忽略「看似未定义」的误报。
</details>

---

### 4.4 windows 子包与 tests 测试目录

#### 4.4.1 概念说明

`signal/` 目录下还有三个子目录：`windows/`、`tests/`、`docs/`。本节聚焦前两个。

- **`windows/` 是一个真正的子包**：它有自己的 `__init__.py`，对外暴露为 `scipy.signal.windows`。它内部同样遵循「私有实现 + 弃用 stub」的套路，可以看作 `signal/` 的「微缩版」。
- **`tests/` 不是子包而是测试集合**：里面是一堆 `test_*.py`，外加几个测试辅助文件（如 `mpsig.py` 用 mpmath 提供高精度参考值，`_scipy_spectral_test_shim.py` 是谱分析测试垫片）。它通过自己的 `meson.build` 以 `install_tag: 'tests'` 单独安装。

#### 4.4.2 核心流程

两个子目录的构建入口都在根 `meson.build` 末尾：

[meson.build:101-102](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/meson.build#L101-L102) 用 `subdir('windows')` 和 `subdir('tests')` 让 Meson 递归进入这两个子目录、执行它们各自的 `meson.build`。

```
signal/meson.build
   ├── subdir('windows')  →  执行 windows/meson.build（安装 3 个 .py）
   └── subdir('tests')    →  执行 tests/meson.build（安装 test_*.py 与数据）
```

#### 4.4.3 源码精读

**（a）windows 子包的三层分工**

[windows/meson.build:1-7](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/meson.build#L1-L7) 安装三个文件：`__init__.py`、`_windows.py`、`windows.py`——和 `signal/` 根目录一模一样的「实现 + stub」模式，只是没有编译扩展（窗函数都是纯 NumPy 计算）。

[windows/__init__.py:42-52](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/__init__.py#L42-L52) 从私有实现 `_windows` 星号导入，并显式 `from . import windows`（第 45 行）保留旧 stub 模块以兼容 `scipy.signal.windows.windows.hamming` 这类旧路径，同样计划 v2.0.0 移除。

[windows/windows.py:20-23](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/windows.py#L20-L23) 是子包内的弃用 stub，`__getattr__` 把访问转发到 `_windows`，和根目录的 stub 完全同构。这说明「私有实现 + stub」是 SciPy 子包的**通用模式**，不是 `signal/` 独有。

**（b）tests 目录的组织**

[tests/meson.build:1-29](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/tests/meson.build#L1-L29) 安装所有 `test_*.py` 与辅助文件（`mpsig.py`、`_scipy_spectral_test_shim.py`），用 `install_tag: 'tests'` 标记——这意味着只有显式安装测试时才会带上它们，不会进入普通用户的运行环境。

测试文件命名与实现模块**基本一一对应**：`test_signaltools.py` ↔ `_signaltools.py`、`test_filter_design.py` ↔ `_filter_design.py`、`test_spectral.py` ↔ `_spectral_py.py`、`test_windows.py` ↔ `windows/_windows.py`，依此类推。这种对应关系让你拿到任何一个实现模块都能立刻找到它的测试。

[tests/meson.build:31-36](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/tests/meson.build#L31-L36) 额外安装了一份测试数据 `data/GLB.Ts+dSST.csv`（一份全球温度时间序列，供谱分析/去趋势测试使用）。

#### 4.4.4 代码实践

1. **实践目标**：确认 `windows/` 子包与 `signal/` 根目录采用了相同的「私有实现 + stub」模式，并验证测试文件与实现模块的对应关系。
2. **操作步骤**：
   ```bash
   # 1. 看 windows 子包的文件构成
   ls scipy/signal/windows/

   # 2. 看 windows/__init__.py 是不是也是「from ._windows import * + 旧 stub」
   grep -n "import\|__all__" scipy/signal/windows/__init__.py

   # 3. 列出 tests/ 下的 test_*.py，观察与实现模块的对应
   ls scipy/signal/tests/test_*.py
   ```
3. **需要观察的现象**：
   - `windows/` 只有 3 个 `.py`（`__init__.py`、`_windows.py`、`windows.py`），无编译扩展。
   - `windows/__init__.py` 里有 `from ._windows import *` 与 `from . import windows`，结构和 `signal/__init__.py` 同构。
   - `tests/` 下的 `test_*.py` 文件名与各 `_*.py` 实现模块一一对应。
4. **预期结果**：你能得出结论——「私有实现 `_windows` + 弃用 stub `windows` + `__init__` 聚合」这套三件套，在子包层级重复出现，是整个 `signal` 的统一组织范式。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `windows/` 子包里**没有** `.cc` 或 `.pyx` 文件，而 `signal/` 根目录有 6 个编译扩展？

<details><summary>参考答案</summary>

窗函数（Hann、Hamming、Kaiser、DPSS 等）的计算本质是向量化的纯数值运算，NumPy 已经足够快，没有「热点循环」需要落到 C/Cython。而 `signal/` 根目录涉及 N-D 相关、`lfilter` 逐样本递推、SOS 级联、upfirdn 多速率处理等带内层循环的算法，纯 Python 太慢，所以才需要编译扩展。是否引入编译扩展取决于「该算法是否存在 Python 难以高效表达的逐元素/逐样本循环」。
</details>

**练习 2**：`tests/mpsig.py` 和 `tests/_scipy_spectral_test_shim.py` 不是 `test_` 开头，它们为什么也被安装在 `tests/` 里？

<details><summary>参考答案</summary>

它们是**测试辅助工具**而非测试用例本身。`mpsig.py` 用 `mpmath` 提供任意精度参考实现，用来验证 `signal` 数值结果的正确性（比如滤波器系数、卷积结果）；`_scipy_spectral_test_shim.py` 是连接「传统 STFT 接口」与「新 `ShortTimeFFT` 接口」的测试垫片，让同一套测试能覆盖两套 API。它们被 `test_*.py` 导入使用，所以必须随测试一起安装。
</details>

## 5. 综合实践

把本讲四个模块串起来，完成一张「`scipy/signal/` 目录全景表」。

**任务**：写一段 shell 命令，自动从 `meson.build` 与磁盘文件出发，生成一张 Markdown 表格，包含每一类文件的计数与代表文件；再人工补充「Python 模块 ↔ 编译扩展」对照。

参考命令（可在仓库根目录执行，输出供你整理）：

```bash
echo "=== 编译扩展（extension_module）==="
grep -oE "extension_module\('[^']+'" scipy/signal/meson.build

echo "=== 安装的纯 Python 文件数量 ==="
sed -n '/install_sources(\[/,/^  \],/p' scipy/signal/meson.build | grep -c "\.py'"

echo "=== 各类源文件计数 ==="
printf "私有实现 _*.py: "; ls scipy/signal/_*.py | grep -v _max_len_seq_inner.py | wc -l
printf "Pythran/Cython 源: "; ls scipy/signal/_*.pyx scipy/signal/_max_len_seq_inner.py | wc -l
printf "C++ 源 .cc: "; ls scipy/signal/*.cc | wc -l
printf "C++ 头 .hh/.h: "; ls scipy/signal/*.hh scipy/signal/*.h | wc -l
printf "弃用 stub(无下划线 .py): "; ls scipy/signal/*.py | grep -v "/_" | wc -l
```

**预期结果**：你应能据此写出一张总览表（数值以本地实际输出为准），形如：

| 类别 | 数量（约） | 代表文件 | 构建路径 |
|---|---|---|---|
| 私有实现模块 | 20+ | `_signaltools.py`、`_filter_design.py` | `install_sources` |
| Pythran/Cython 源 | 5 | `_sosfilt.pyx`、`_max_len_seq_inner.py` | `extension_module` |
| C++ 源 | 6 | `_correlate_nd.cc`、`_splinemodule.cc` | `extension_module` |
| C++ 头 | 2 | `_sigtools.hh`、`_splinemodule.h` | （被 .cc 包含） |
| 弃用 stub | 10 | `signaltools.py`、`filter_design.py` | `install_sources` |
| 子包 | 1 | `windows/` | `subdir` |

完成后再回到 4.2.4 的对照表，确认你能为每个编译扩展指出它的 Python 调用方。这张「全景表 + 对照表」就是本讲的交付物，后续每一篇讲义都会落在其中的某个文件上。

## 6. 本讲小结

- `signal/` 目录的文件可分为三类：**私有实现模块**（`_*.py`，真正的算法）、**弃用 stub**（无下划线小文件，转发到私有模块并发出 `DeprecationWarning`，v2.0.0 移除）、**编译扩展源**（`.cc` / `.pyx` / Pythran `.py`）。
- [`meson.build`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/meson.build) 用两条路径区分构建：`extension_module` 编译出 6 个二进制扩展（`_sigtools`、`_max_len_seq_inner`、`_peak_finding_utils`、`_sosfilt`、`_upfirdn_apply`、`_spline`），`install_sources` 原样安装 34 个纯 Python 文件。
- [`_signal_api.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signal_api.py) 是私有实现的「聚合层」，用 `dir()` 自动生成 `__all__`，为下游 `_support_alternative_backends` 的统一装饰提供单一入口；它本身私有，会被 `__init__.py` `del` 掉。
- 每个 Python 实现模块是否「配」编译扩展取决于算法是否有逐样本循环：`_signaltools`/`_upfirdn`/`_peak_finding`/`_max_len_seq`/`_spline_filters` 都有，而 `_czt`/`_short_time_fft`/`_filter_design` 等多为纯 Python。
- `windows/` 子包是 `signal/` 的微缩版，同样采用「私有实现 `_windows` + 弃用 stub `windows` + `__init__` 聚合」三件套，但因窗函数无需逐样本加速而没有编译扩展。
- `tests/` 的 `test_*.py` 与实现模块基本一一对应，并通过 `install_tag: 'tests'` 单独安装；`mpsig.py`、`_scipy_spectral_test_shim.py` 是测试辅助工具。

## 7. 下一步学习建议

本讲建立的是「文件地图」。接下来建议：

1. **进入 `u1-l3`（构建方式：Meson 与编译扩展入门）**：本讲只点了 `extension_module` 的名字，下一讲会深入讲 C / Cython / Pythran 三种编译路径的细节，特别是 `_max_len_seq_inner` 的双路径机制。
2. **进入 `u1-l4`（公共命名空间与 API 导出链路）**：本讲看到了 `_signal_api.py` 聚合，下一讲会追踪一个具体函数（如 `butter`）从 `_filter_design` → `_signal_api` → `_support_alternative_backends` → `__init__` 的完整接力，并解释弃用 stub 的 `_sub_module_deprecation` 机制。
3. **想直接看某个算法？** 对照本讲 4.2.4 的「Python 模块 ↔ 编译扩展」表，挑一个感兴趣的实现模块直接读。例如想学卷积就去看 `_signaltools.py`（配合 `_sigtools`），想学谱分析就去看 `_spectral_py.py`。
