# 目录结构与源码分层地图

## 1. 本讲目标

上一讲我们认识了 `scipy.special` 是什么，以及「几乎所有函数都是 NumPy ufunc」这一核心设计。本讲的目标是把镜头拉到**文件层面**，带你走一遍 `scipy/special/` 这个目录。

读完本讲，你应当能够：

1. 说出 `special/` 目录下每一类文件（`.py` 包装、`.pyx/.pxd` Cython、`.cpp/.h` C/C++ 内核、`functions.json` 声明、`.pyi` 类型桩）分别承担什么角色。
2. 在脑海里画出一条从 Python 公共 API 一路下钻到 C/C++ 数学内核的**分层调用链**。
3. 识别 `tests/`、`_precompute/`、`utils/`、`ellint_carlson_cpp_lite/` 等辅助目录各自承担的工程职责。

本讲只做「地图测绘」，不深入任何一层的实现细节——那是后续讲义的任务。先把地形看清楚，后面才不会迷路。

## 2. 前置知识

阅读本讲前，你需要具备以下基础概念（上一讲已建立）：

- **特殊函数（special function）**：数学里那些「有名有姓」、在物理/工程/统计中反复出现的函数，如 Bessel、Gamma、误差函数、正交多项式等。
- **NumPy ufunc（universal function）**：NumPy 的通用函数，天然支持标量/数组输入、广播和逐元素求值。`special` 里绝大多数函数都是 ufunc。
- **命名空间（namespace）**：`scipy.special` 这个统一的名字背后，其实是由多个子模块「拼装」出来的。

本讲会引入几个新术语，先用一句话解释：

- **Cython（`.pyx`/`.pxd`）**：一种带类型的 Python 方言，写出来长得像 Python，但能被编译成 C 代码，从而既能调用 C/C++ 库、又能被 Python 导入。`.pyx` 是实现文件，`.pxd` 是声明文件（类似 C 的头文件）。
- **扩展模块（extension module）**：用 C/C++/Cython 写、编译成 `.so`（Linux）或 `.pyd`（Windows）的二进制模块，Python 通过 `import` 加载它。`special` 之所以是「编译型」子模块，正是因为它依赖多个扩展模块。
- **Meson**：SciPy 使用的构建系统，由 `meson.build` 文件描述如何把源码编译成扩展模块。

> 关键直觉：`scipy.special` 不是一堆纯 Python 函数。它的「快」和「准」来自底层的 C/C++ 数学库；Python 层只是一个把它们包装成 ufunc 的薄壳子。本讲要画的，就是这层「壳子 → 内核」的地图。

## 3. 本讲源码地图

本讲涉及的关键文件如下表。本讲只精读前两个，其余文件将在后续讲义深入，这里仅说明它们在分层中的位置。

| 文件 | 角色 | 本讲是否精读 |
| --- | --- | --- |
| `__init__.py` | Python 包装层的「总装车间」：把各子模块的函数拼成 `scipy.special` 命名空间 | 是 |
| `meson.build` | 构建蓝图：声明有哪些扩展模块、各自由哪些源码编译而来 | 是 |
| `_basic.py` / `_orthogonal.py` / `_logsumexp.py` 等 `.py` | 纯 Python 包装层（组合数学、正交多项式、logsumexp 等） | 仅定位 |
| `cython_special.pyx` / `_specfun.pyx` / `_ellip_harm_2.pyx` | Cython 源（类型化 API 与部分内核胶水） | 仅定位 |
| `functions.json` | 声明式「函数名 → C/C++ 内核 → 类型签名」清单 | 仅定位 |
| `_special_ufuncs.cpp` / `_gufuncs.cpp` / `xsf_wrappers.cpp` / `boost_special_functions.h` / `cdflib.c` | C/C++ 数学内核 | 仅定位 |
| `tests/` / `_precompute/` / `utils/` / `ellint_carlson_cpp_lite/` | 辅助目录（测试、离线预计算、工具脚本、专项内核） | 仅定位 |

## 4. 核心概念与源码讲解

### 4.1 源码分层架构

#### 4.1.1 概念说明

`scipy.special` 的源码可以清晰地分成**三层**（外加一个「声明层」），从上到下依次是：

```
┌─────────────────────────────────────────────────────────┐
│  第 1 层：Python 包装层（.py）                            │
│  __init__.py / _basic.py / _orthogonal.py / _logsumexp.py │
│  职责：组装命名空间、参数预处理、纯 Python 实现           │
└────────────────────────┬────────────────────────────────┘
                         │ import / 调用
┌────────────────────────▼────────────────────────────────┐
│  第 2 层：Cython 胶水层（.pyx / .pxd / .pxi）             │
│  cython_special.pyx / _specfun.pyx / 生成出的 _ufuncs.pyx │
│  职责：把 C/C++ 内核包装成 Python 可调用的 ufunc/类型化函数│
└────────────────────────┬────────────────────────────────┘
                         │ cimport / 直接调用
┌────────────────────────▼────────────────────────────────┐
│  第 3 层：C/C++ 数学内核层（.c / .cpp / .cxx / .h / .hh）  │
│  xsf_wrappers.cpp / boost_special_functions.h /           │
│  cdflib.c / ellint_carlson_wrap.cxx / sf_error.cc          │
│  职责：真正算数学的那一层                                  │
└─────────────────────────────────────────────────────────┘

        ┌──── 声明层：functions.json（描述 1→3 的映射） ────┐
        └ _generate_pyx.py（按 functions.json 生成第 2 层） ┘
```

为什么要分这么多层？因为这三层各有不可替代的职责：

- **Python 层**写起来快、表达力强，适合做参数校验、命名空间组装、纯算法实现（如 `logsumexp` 的数值稳定技巧）。但它慢，不能直接调 C 库。
- **C/C++ 层**是性能与精度的来源（xsf、Boost.Math、Cephes、cdflib 都是久经验证的数学库）。但它不能被 Python 直接 `import`。
- **Cython 层**就是中间的「翻译官」：它既能像 C 一样调用内核函数，又能被 Python 当模块导入，从而把内核包装成 ufunc 暴露出去。

> 一个隐藏的「第四层」是 `functions.json` + `_generate_pyx.py`：它们不是运行时代码，而是**构建期**的声明与代码生成器。`functions.json` 声明「哪个 Python 函数名对应哪个 C/C++ 内核、什么类型签名」，`_generate_pyx.py` 据此自动生成大段 Cython ufunc 注册代码。这是本模块的「工程心脏」，详见 U3 单元。

#### 4.1.2 核心流程

以调用 `scipy.special.jv(0, 1.0)`（整数阶 Bessel 函数）为例，调用链自上而下穿过三层：

1. Python 层：`jv` 这个名字来自 `from ._ufuncs import *`（见 `__init__.py`），它实际是一个 ufunc 对象。
2. Cython 层：这个 ufunc 由**生成出来的** `_ufuncs.pyx` 在编译期注册，其内层循环（inner loop）`cimport` 了某个 C 内核函数。
3. C/C++ 层：真正的 Bessel 计算发生在 xsf 或 Cephes 的 C/C++ 实现里。
4. 错误回流：若内核检测到域错误，由 `sf_error.cc` 跨越 GIL 把信号转成 Python 告警/异常（详见 U7）。

需要强调：第 2 层里的 `_ufuncs.pyx` 和 `_ufuncs_cxx.pyx` **并不存在于源码目录里**——它们是 `meson.build` 在构建时由 `_generate_pyx.py` 从 `functions.json` 生成的。这也是为什么你在 `ls` 时看不到 `_ufuncs.pyx`，却能 `import scipy.special._ufuncs`。

#### 4.1.3 源码精读

先看 Python 包装层如何「总装」出命名空间。`__init__.py` 在文档字符串之后，用一连串 `from .子模块 import *` 把分散在各处的函数汇聚到 `scipy.special` 这个名字下：

[scipy/special/__init__.py:786-804](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L786-L804) —— 这段导入语句是分层架构的「咽喉」：先从 `_ufuncs`（Cython 层产物）拿全部 ufunc，再从 `_basic`、`_multiufuncs`、`_orthogonal` 等纯 Python 子模块补充非 ufunc 函数。注意第 796 行的 `from ._support_alternative_backends import *` 会**覆盖**部分函数定义以加入 Array API 多后端支持。

其余几个手工导入的薄包装子模块：

[scipy/special/__init__.py:806-817](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L806-L817) —— 这里显式地从 `_ellip_harm`、`_lambertw`、`_spherical_bessel` 三个小模块导入函数。它们是「薄包装」：在底层 ufunc 之上做一点点参数预处理（详见 U4）。

而 `_ufuncs` 本身（即 Cython 层产物）来自哪里、由什么编译，答案在 `meson.build`：

[scipy/special/meson.build:68-80](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/meson.build#L68-L80) —— `custom_target('cython_special', ...)` 是声明层的入口：它把 `_generate_pyx.py`、`functions.json`、`_add_newdocs.py` 作为输入，在构建时产出 `_ufuncs.pyx`、`_ufuncs_cxx.pyx` 等源文件。这就是「声明 → 生成 Cython」的管线。

生成出的 `.pyx` 再被 Cython 编译器转成 `.c`/`.cpp`，最后编成扩展模块：

[scipy/special/meson.build:98-114](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/meson.build#L98-L114) —— `_ufuncs` 扩展模块的目标定义。它的源码包含两部分：`ufuncs_sources`（`_cosine.c`、`xsf_wrappers.cpp`、`sf_error.cc` 等 C/C++ 内核）和 `uf_cython_gen.process(cython_special[0])`（即生成出的 `_ufuncs.pyx`）。注意 `link_with: cdflib_lib`——它链接了概率分布库 `cdflib`，这是 C/C++ 内核层的一个组件。

把上面三段串起来，你就看到了完整的三层贯穿：

```
__init__.py  ──import──▶  _ufuncs（扩展模块）
                              ▲
            meson.build 编译  │ 源码 = C/C++内核 + 生成出的 _ufuncs.pyx
                              │
            functions.json ──(生成)──▶ _ufuncs.pyx ──(调用)──▶ xsf_wrappers.cpp / cdflib.c / ...
```

#### 4.1.4 代码实践

**实践目标**：亲手验证「Python 层 → Cython 层 → C/C++ 内核层」三层确实存在，且 `_ufuncs` 是一个编译出的二进制扩展模块（而非 `.py` 文件）。

**操作步骤**：

1. 在已安装 SciPy 的环境中执行下面的命令。

```python
import scipy.special as sc
import scipy.special._ufuncs as u
print(type(sc.jv))              # <class 'numpy.ufunc'>
print(sc.jv.__module__ if hasattr(sc.jv, '__module__') else '?')
print(u.__file__)               # 应指向一个 .so/.pyd，而非 .py
```

2. 观察输出。

**需要观察的现象**：

- `type(sc.jv)` 应为 `numpy.ufunc`，印证它是 ufunc（Cython 层产物）。
- `u.__file__` 应指向类似 `.../_ufuncs.cpython-3xx-...so` 的**二进制文件路径**，证明它不是源码里的 `.py`，而是编译产物。

**预期结果**：`jv` 是 ufunc，`_ufuncs` 来自 `.so` 文件。

> 待本地验证：不同平台/Python 版本下 `.so` 的确切文件名会不同，但「它是个编译扩展」这一结论不变。

#### 4.1.5 小练习与答案

**练习 1**：如果删除 `__init__.py` 第 789 行的 `from ._ufuncs import *`，`scipy.special.jv` 还能正常调用吗？为什么？

> **参考答案**：不能。`jv` 是 ufunc，其来源正是 `_ufuncs` 扩展模块。去掉这行导入后，`jv` 这个名字不再进入 `scipy.special` 命名空间（除非被其他导入带入）。这印证了 `__init__.py` 作为「总装车间」的关键地位。

**练习 2**：为什么 `_ufuncs.pyx` 在源码目录里找不到，却能被 `import`？

> **参考答案**：因为 `_ufuncs.pyx` 是构建期由 `_generate_pyx.py` 从 `functions.json` 生成的中间文件，生成后立刻被 Cython 编译成 `.c`、再编译成 `.so`。源码包里只保留「声明」(`functions.json`)和「生成器」(`_generate_pyx.py`)，不保留生成产物。

### 4.2 Python/Cython/C++ 文件命名约定

#### 4.2.1 概念说明

`special/` 目录里文件众多，但命名有几条**强约定**，掌握了就能「望文生义」地判断一个文件属于哪一层、起什么作用。

先看一张按扩展名归类的速查表（基于对当前目录的实际统计）：

| 扩展名 | 数量 | 所属层 | 含义 |
| --- | --- | --- | --- |
| `.py` | 23 | Python 包装层 | 纯 Python：命名空间组装、参数预处理、纯算法实现、代码生成器、测试工具 |
| `.pyx` | 3 | Cython 层 | Cython 实现（`_specfun.pyx`、`cython_special.pyx`、`_ellip_harm_2.pyx`） |
| `.pxd` | 9 | Cython 层 | Cython 声明（类似 C 头文件，供 `cimport`） |
| `.pxi` | 2 | Cython 层 | 被拼接到生成代码里的代码片段 |
| `.pyi` | 2 | 类型桩 | 给静态检查器/IDE 用的类型签名（`_ufuncs.pyi`、`cython_special.pyi`） |
| `.c` | 2 | C/C++ 内核层 | C 源（`_cosine.c`、`cdflib.c`） |
| `.cpp` | 5 | C/C++ 内核层 | C++ 源（`_special_ufuncs.cpp`、`_gufuncs.cpp`、`xsf_wrappers.cpp` 等） |
| `.cc` | 1 | C/C++ 内核层 | C++ 源（`sf_error.cc`） |
| `.cxx` | 1 | C/C++ 内核层 | C++ 源（`ellint_carlson_wrap.cxx`） |
| `.h`/`.hh` | 若干 | C/C++ 内核层 | C/C++ 头文件（`.hh` 多见于 `ellint_carlson_cpp_lite/`） |
| `.json` | 1 | 声明层 | `functions.json`：函数声明清单 |

> 小提示：`.cc`、`.cxx`、`.cpp` 都是 C++ 源文件的不同后缀习惯，本质相同；用不同后缀通常只是历史或工具链偏好。

除了扩展名，**前缀下划线**也是重要约定：

- 以 `_` 开头的模块（如 `_basic`、`_ufuncs`、`_logsumexp`）是「内部」实现，理论上不应被用户直接 `import`（虽然技术上可以）。它们是 `__init__.py` 拼装命名空间的「零件」。
- 不带 `_` 的小文件（`basic.py`、`orthogonal.py`、`specfun.py`、`sf_error.py`、`spfun_stats.py`、`add_newdocs.py`）是**已弃用的旧命名空间**，将在 v2.0.0 移除。

还有几条「语义命名」约定：

- `*_wrappers.cpp/.h`（`xsf_wrappers`、`dd_real_wrappers`）：把外部 C++ 数学库包装成 Cython 能调用的 C 接口。
- `boost_special_functions.h`：Boost.Math 库的接入层。
- `sf_error.*`（`.h`/`.cc`/`.pxd`/`.py`）：跨三层的统一错误处理机制（sf = special function）。
- `_special_ufuncs.cpp` / `_gufuncs.cpp`：区别于「生成式」路径，这两条是**直接在 C++ 里注册 ufunc**的更新路径（详见 U8）。

#### 4.2.2 核心流程

如何判断一个文件属于哪一层？用下面的决策流程：

```
看到文件名
   │
   ├── 是 .json? ───────────────▶ 声明层 (functions.json)
   ├── 是 .pyi? ─────────────────▶ 类型桩层
   ├── 是 .py?
   │      ├── 内容是「导入并组装」? ──▶ Python 包装层·总装 (__init__.py)
   │      ├── 是代码生成器? ────────▶ 声明层辅助 (_generate_pyx.py)
   │      └── 其他 ────────────────▶ Python 包装层·具体函数实现 (_basic.py 等)
   ├── 是 .pyx / .pxd / .pxi? ───▶ Cython 层
   └── 是 .c / .cpp / .cc / .cxx / .h / .hh? ──▶ C/C++ 内核层
```

#### 4.2.3 源码精读

命名约定在源码里有据可查。先看「已弃用的旧命名空间」这一约定——它们不带下划线、文件很小，只是重新导出内部模块：

[scipy/special/meson.build:217-244](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/meson.build#L217-L244) —— `python_sources` 列出了所有需要安装的 Python 源文件。可以看到 `_basic.py`（内部）与 `basic.py`（弃用外壳）、`_orthogonal.py` 与 `orthogonal.py`、`specfun.py` 与 `_specfun.pyx` 成对出现，这正是「内部实现 + 弃用别名」的双轨命名。

弃用的动机在 `__init__.py` 里有明确注释：

[scipy/special/__init__.py:819-820](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L819-L820) —— 注释 `# Deprecated namespaces, to be removed in v2.0.0` 直接说明了不带下划线的小模块为何存在：为了向后兼容旧的导入路径（如 `scipy.special.specfun`），它们将在 v2.0.0 移除。

再看 Cython 层与 C/C++ 层的边界——`meson.build` 把不同语言源文件分到不同的列表里，体现分层：

[scipy/special/meson.build:14-24](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/meson.build#L14-L24) —— `ufuncs_sources`（含 `_cosine.c`、`xsf_wrappers.cpp`、`sf_error.cc`）和 `ufuncs_cxx_sources`（含 `ellint_carlson_wrap.cxx`）这两个列表，把 C/C++ 内核层的源文件集中归类，供 `_ufuncs` / `_ufuncs_cxx` 扩展模块分别链接。

#### 4.2.4 代码实践

**实践目标**：用 `ls` 与 `grep` 把目录里的文件按层归类，验证上面的速查表。

**操作步骤**：

```bash
cd $(python -c "import scipy.special, os; print(os.path.dirname(scipy.special.__file__))")
# 注意：上式得到的是「已安装」的目录，那里没有 .pyx/.pxd（构建产物）。
# 要看完整源码分层，请进入 SciPy 源码仓库的 scipy/special/ 目录：
#   ls *.py *.pyx *.pxd *.pxi *.pyi *.c *.cpp *.cc *.cxx *.h *.hh *.json
```

**需要观察的现象**：在**源码仓库**里，你能看到 `.py`/`.pyx`/`.pxd`/`.pxi`/`.c`/`.cpp`/`.h`/`.json` 各类文件并存；而在**已安装目录**里，`.pyx`/`.pxd`/`.pxi` 大多消失（已被编译进 `.so`），只剩下 `.py` 和 `.pyi`。

**预期结果**：源码仓库里各类文件齐全；已安装目录里只剩 `.py`、`.pyi` 和若干 `.so`。这正是「源码分层」与「安装产物」的差异。

> 待本地验证：具体可见的文件取决于你是看源码仓库还是 site-packages 安装目录。

#### 4.2.5 小练习与答案

**练习 1**：`sf_error` 这个名字同时出现在 `.h`、`.cc`、`.pxd`、`.py` 四种文件里。这四种分别属于哪一层？为什么需要同名的四个文件？

> **参考答案**：`.h`/`.cc` 属于 C/C++ 内核层（定义错误码与触发函数）；`.pxd` 属于 Cython 层（让 Cython 代码能 `cimport` 到 C 接口）；`.py` 属于 Python 包装层（定义 `SpecialFunctionWarning`/`SpecialFunctionError` 两个 Python 类）。同名四件套体现了错误处理「贯穿三层」的需要：C 内核检测错误 → Cython 调用 C 函数 → Python 层抛出对应的告警/异常。

**练习 2**：`_special_ufuncs.cpp` 和（生成出的）`_ufuncs.pyx` 都是用来「提供 ufunc」的，为什么要分两套？

> **参考答案**：`_ufuncs.pyx` 是**生成式**路径（由 `functions.json` + `_generate_pyx.py` 产生，较老）；`_special_ufuncs.cpp` 是**直接在 C++ 里用 `xsf::numpy::ufunc` 注册**的新路径，无需经过 Cython 代码生成。两者并存是迁移过程中的双轨现状，新函数倾向于走 `_special_ufuncs.cpp` 路径（详见 U8）。

### 4.3 辅助目录职责

#### 4.3.1 概念说明

除了上述「核心源码」，`special/` 下还有四个目录，它们不参与函数实现，但承担着**测试、参考数据生成、工具脚本、专项内核**等工程职责：

| 目录 | 职责 | 代表文件 |
| --- | --- | --- |
| `tests/` | 单元测试与数值验证 | `test_basic.py`、`test_mpmath.py`、`data/`（参考数据集） |
| `_precompute/` | 离线高精度预计算脚本（用 mpmath 生成参考系数/数据） | `gammainc_asy.py`、`lambertw.py`、`utils.py` |
| `utils/` | 构建期/数据工具脚本 | `makenpz.py`（把文本数据打包成 `.npz`）、`convert.py`、`datafunc.py` |
| `ellint_carlson_cpp_lite/` | Carlson 椭圆积分的轻量 C++ 实现（头文件库） | `_rf.hh`、`_rd.hh`、`ellint_carlson.hh` |

为什么需要 `_precompute/`？因为数值数学库的正确性必须用**可信参考值**来验证。许多特殊函数的渐近展开系数无法用双精度算准，需要用 mpmath 任意精度库离线算到几十位精度，再固化成数据供运行时或测试使用。这是「数值库工程」的典型实践（详见 U9）。

为什么 `ellint_carlson_cpp_lite/` 单独成目录？因为 Carlson 椭圆积分（支撑 `elliprf`/`elliprd`/`elliprg`/`elliprj`）用的是一套独立的、纯头文件的 C++ 实现，自成体系，故单独存放。

#### 4.3.2 核心流程

这几个目录与构建、测试的关系：

```
_precompute/*.py ──(离线运行)──▶ 高精度参考数据/系数 ──▶ 固化进运行时代码 或 测试
tests/data/*.txt  ──(makenpz.py)──▶ *.npz ──▶ 被 tests/ 下的 FuncData 测试加载
ellint_carlson_cpp_lite/*.hh ──(作为 source 依赖)──▶ 被 _ufuncs_cxx 链接
utils/makenpz.py ──(custom_target)──▶ 把文本数据转 .npz
```

#### 4.3.3 源码精读

`meson.build` 末尾用 `subdir()` 把两个子目录的构建纳入主流程：

[scipy/special/meson.build:251-252](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/meson.build#L251-L252) —— `subdir('tests')` 和 `subdir('_precompute')` 让 Meson 进入这两个子目录继续读取它们各自的 `meson.build`，把测试与预计算的构建逻辑挂接到主构建里。

`utils/` 的核心脚本是 `makenpz.py`，它在 `meson.build` 里被 `custom_target` 调用，把文本参考数据转成二进制 `.npz` 供测试加载：

[scipy/special/meson.build:181-214](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/meson.build#L181-L214) —— `npz_files` 列表与随后的 `foreach` 循环，分别把 `tests/data/boost/`、`tests/data/gsl/`、`tests/data/local/` 下的文本数据，通过 `utils/makenpz.py` 打包成 `boost.npz`、`gsl.npz`、`local.npz`，安装到测试数据目录。这展示了 `utils/` 作为「构建期工具脚本」的角色。

`ellint_carlson_cpp_lite/` 则作为**源码依赖**注入到 `_ufuncs_cxx` 扩展模块：

[scipy/special/meson.build:116-131](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/meson.build#L116-L131) —— `ellint_files` 列出 Carlson 椭圆积分的所有头文件，`ellint_dep = declare_dependency(sources: ellint_files)` 把它们声明为一个依赖。这些头文件随后作为 source 依赖参与到 `_ufuncs_cxx` 的编译中（见第 147 行 `dependencies` 里的 `ellint_dep`）。

#### 4.3.4 代码实践

**实践目标**：定位这四个辅助目录，并理解它们的「产物」去向。

**操作步骤**：

```bash
# 在 SciPy 源码仓库的 scipy/special/ 下
ls tests/ | head -20            # 看测试文件，注意 data/ 子目录
ls tests/data/                  # 看参考数据来源(boost/gsl/local)
ls _precompute/                 # 看离线预计算脚本
ls utils/                       # 看工具脚本(makenpz.py 等)
ls ellint_carlson_cpp_lite/     # 看 Carlson 椭圆积分头文件库
```

**需要观察的现象**：

- `tests/data/` 下应有 `boost/`、`gsl/`、`local/` 等子目录，里面是 `.txt` 文本参考数据。
- `_precompute/` 下应有 `gammainc_asy.py`、`lambertw.py`、`zetac.py` 等脚本，以及它们对应的测试 `tests/test_precompute_*.py`。
- `utils/` 下应有 `makenpz.py`。
- `ellint_carlson_cpp_lite/` 下应有 `_rf.hh`、`_rd.hh`、`_rg.hh`、`_rj.hh`、`_rc.hh` 等 Carlson 积分头文件。

**预期结果**：四个目录各自的内容与上表一致，印证它们的工程分工。

#### 4.3.5 小练习与答案

**练习 1**：`tests/data/` 下的 `.txt` 文件，是如何变成测试可加载的数据的？

> **参考答案**：构建时，`meson.build` 的 `npz_files` 循环调用 `utils/makenpz.py`，把 `tests/data/boost`、`gsl`、`local` 下的文本文件分别打包成 `boost.npz`、`gsl.npz`、`local.npz`，安装到测试数据目录；测试代码再通过 `np.load` 加载这些 `.npz` 来校验函数实现。`utils/` 目录在这里扮演「构建期数据转换工具」的角色。

**练习 2**：为什么 Carlson 椭圆积分的实现要单独放在 `ellint_carlson_cpp_lite/` 目录，而不是和 `xsf_wrappers.cpp` 放一起？

> **参考答案**：因为它是一套**独立的、纯头文件的 C++ 库**（`_rf.hh`/`_rd.hh` 等，自成体系），在 `meson.build` 里通过 `declare_dependency(sources: ellint_files)` 整体作为一个 source 依赖注入 `_ufuncs_cxx`。单独成目录既反映它的库独立性，也便于作为 source 依赖被引用。

## 5. 综合实践

把本讲三节的知识串起来，完成下面这个「目录测绘」任务。

**任务**：在 SciPy 源码仓库的 `scipy/special/` 目录里，绘制一张「Python → Cython → C/C++」的分层调用关系简图，并标注每个文件属于哪一层。

**操作步骤**：

1. 用 `ls` 与文件扩展名，把顶层文件分成三大类（Python 包装层 / Cython 层 / C/C++ 内核层），外加「声明层」(`functions.json`) 和「类型桩」(`.pyi`)。可参考 4.2.1 的速查表。
2. 找出一个具体函数（如 `airy` 或 `betainc`），按下面的方式追踪它的「出生地」：
   - 在 `__init__.py` 里确认它来自 `from ._ufuncs import *`（即 Cython 层产物）。
   - 在 `functions.json` 里找到它的声明（哪个 C/C++ 头文件、什么类型签名）——这指向 C/C++ 内核层的某个 wrapper。
3. 画出类似下面的简图（以 `betainc` 为例，结论留待 U3/U8 验证）：

```
Python 层:   __init__.py  ──(from ._ufuncs import *)──▶  betainc (ufunc 对象)
                                                          ▲
Cython 层:   _ufuncs.pyx (生成产物)  ──注册 ufunc────────┘
                  │ cimport 内核
C/C++ 层:    boost_special_functions.h (Boost.Math 实现) ◀── 由 functions.json 指定头文件
声明层:      functions.json ──(描述 betainc→Boost 内核)──▶ _generate_pyx.py ──生成──▶ _ufuncs.pyx
```

4. 最后，在图上标出四个辅助目录（`tests/`、`_precompute/`、`utils/`、`ellint_carlson_cpp_lite/`）挂在哪一层之外（它们是「支撑设施」）。

**需要观察的现象**：你会清楚地看到一个函数「在 Python 里被调用、在 C++ 里被计算」的完整链路，且 `functions.json` 是连接两层的关键声明。

**预期结果**：得到一张标注清晰的分层图，能解释任意一个 `special` 函数「从哪个文件来、最终在哪里被计算」。

> 待本地验证：第 2 步中 `betainc` 具体来自 Boost 还是其他后端，需在 `functions.json` 里核对（这是 U3 的内容）。本练习只要求画出框架并标注「待 U3 确认」即可。

## 6. 本讲小结

- `scipy/special/` 的源码分为**三层**：Python 包装层（`.py`）、Cython 胶水层（`.pyx`/`.pxd`/`.pxi`）、C/C++ 数学内核层（`.c`/`.cpp`/`.h` 等），外加一个构建期的**声明层**（`functions.json` + `_generate_pyx.py`）。
- `__init__.py` 是「总装车间」，用一连串 `from .子模块 import *` 把分散在 `_ufuncs`、`_basic`、`_orthogonal`、`_multiufuncs` 等子模块的函数拼成统一的 `scipy.special` 命名空间。
- 文件命名有强约定：以下划线开头是「内部」实现；不带下划线的小文件（`basic.py` 等）是 v2.0.0 将移除的弃用别名；`*_wrappers.cpp`、`boost_special_functions.h`、`sf_error.*` 各有固定语义。
- `_ufuncs.pyx` / `_ufuncs_cxx.pyx` **不在源码目录里**——它们是构建期由 `_generate_pyx.py` 从 `functions.json` 生成的，这正是本模块「声明式代码生成」的工程心脏。
- `meson.build` 把不同语言的源文件分列表归类，编译出 `_ufuncs`、`_ufuncs_cxx`、`_special_ufuncs`、`_gufuncs`、`cython_special`、`_ellip_harm_2`、`_specfun` 等多个扩展模块。
- 四个辅助目录各司其职：`tests/`（测试与参考数据）、`_precompute/`（离线高精度预计算）、`utils/`（构建期工具脚本）、`ellint_carlson_cpp_lite/`（Carlson 椭圆积分专项内核）。

## 7. 下一步学习建议

本讲只画了「地图」，还没有进入任何一层的实现。建议按下面的顺序继续：

1. **下一讲（U1-L3）**：学习如何用 Meson 构建 `special`、如何导入与运行测试，把「源码」变成「可运行环境」。本讲看到的 `meson.build` 将在那讲具体展开。
2. **如果想先理解「拼装」**：跳到 U1-L4，精读 `__init__.py` 末尾 `__all__` 的组装逻辑，搞清楚 250+ 个函数如何被归类。
3. **如果想理解「代码生成心脏」**：直接进入 U3 单元，从 `functions.json`（U3-L1）和 `_generate_pyx.py`（U3-L2）入手，这是本模块最不同于其他子模块的工程特性。
4. **延伸阅读源码**：建议先通读 `meson.build` 全文（仅 253 行），它是理解整个目录结构最浓缩的「索引」；再随意挑一个 `_xxx.py`（如 `_logsumexp.py`，体量适中）感受 Python 包装层的写法。
