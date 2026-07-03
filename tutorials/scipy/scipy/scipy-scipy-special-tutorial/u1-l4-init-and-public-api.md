# `__init__.py` 与公共 API 的组装

## 1. 本讲目标

`scipy.special` 的 250 多个函数并不写在一个文件里，而是分散在 `_ufuncs`、`_basic`、`_orthogonal`、`_multiufuncs`、`_logsumexp`、`_lambertw`、`_spherical_bessel`、`_ellip_harm` 等十来个子模块中。读者之所以能简单地写一句 `import scipy.special as sc; sc.erf(...)`、`sc.logsumexp(...)`、`sc.spherical_jn(...)` 就用到它们，全靠 [`__init__.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py) 把这些零散的零件**拼装**成一个统一的命名空间。

学完本讲，你应该能够：

- 说清 `__init__.py` 的「双重身份」：顶部超长文档字符串是按类别组织的**函数目录**，后半部分是一连串**导入语句**，负责把各子模块的函数汇聚进 `scipy.special` 命名空间。
- 解释 `from ._ufuncs import *`、`from ._basic import *` 等「星号导入」如何把子模块的 `__all__` 灌入父模块。
- 看懂 `__init__.py` 末尾 `__all__` 的组装方式：「四路聚合 + 一份手动补丁清单」，并能说出为什么某些函数（如 `multigammaln`、`logsumexp`、`lambertw`、`ellip_harm`）必须手动追加。
- 识别文件里那行 `# Deprecated namespaces ...` 导入的旧命名空间（`add_newdocs`、`basic`、`orthogonal`、`specfun`、`sf_error`、`spfun_stats`），知道它们将在 v2.0.0 被移除。

本讲精读 [`__init__.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py) 全文，并辅以类型桩 [`_ufuncs.pyi`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi) 中 `_ufuncs.__all__` 的定义。

## 2. 前置知识

- **包（package）与 `__init__.py`**：Python 里一个目录配上 `__init__.py` 就是一个「包」。`import scipy.special` 实际是先执行 `scipy/__init__.py`，再执行 `scipy/special/__init__.py`。后者就是本讲的主角——它是整个 `special` 子包的「门面」与「总装车间」。
- **`from X import *` 与 `__all__`**：`from module import *` 默认会把模块里所有「不以下划线开头」的名字都搬过来；但如果被导入模块定义了 `__all__` 列表，则**只搬 `__all__` 里列出的名字**。所以 `__all__` 既是「对外公开 API 清单」，也是「星号导入的白名单」。
- **命名空间（namespace）**：一个模块对象里「名字 → 对象」的映射。`scipy.special` 这个命名空间里要有 `erf`、`gamma`、`logsumexp` 等名字，靠的就是 `__init__.py` 里的导入语句把它们逐一绑定。
- **ufunc**：NumPy 通用函数。本模块绝大多数函数都是 ufunc（详见 u1-l1）。它们集中住在编译出来的 `_ufuncs` 扩展模块里。
- **「三层地图」**：本讲承接 u1-l2 建立的「Python 包装层 → Cython 层 → C/C++ 内核层」。`__init__.py` 处在最上层，它**不知道**底层是 Cython 还是 C++，只负责把下层的 Python 对象重新摆放到统一的货架上。

> 一句话定位：`__init__.py` 不实现任何数学函数，它只做一件事——**把别人实现好的函数，按统一的名字摆上 `scipy.special` 这个货架**，并顺手写好一份说明书（文档字符串）。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲怎么用它 |
|------|------|--------------|
| [`__init__.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py) | 包入口：文档字符串（函数目录）+ 一串导入语句（命名空间总装）+ `__all__`（公开 API 清单） | 4.1–4.4 全程主战场 |
| [`_ufuncs.pyi`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi) | `_ufuncs` 扩展模块的类型桩；开头就定义了 `_ufuncs.__all__`，是 `__all__` 四路聚合里最大的一路 | 4.3 论证「四路聚合」的最大贡献者 |

> 说明：`_ufuncs` 本体不是 `.py` 文件，而是构建时由 `functions.json` 生成的 Cython 扩展（详见 u1-l2、u3 单元）。我们在这里读不到 `_ufuncs.py`，但能从 `_ufuncs.pyi` 读到它导出的名字清单。

## 4. 核心概念与源码讲解

### 4.1 文档字符串：`__init__.py` 的「函数目录」第一身份

#### 4.1.1 概念说明

打开 [`__init__.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py)，会发现前 **783 行几乎全是一整段文档字符串**（从第 1 行的 `"""` 到第 783 行的 `"""`）。这段文字不是给人随便读的注释，而是 Sphinx 文档系统渲染 [SciPy 官方 API 页面](https://docs.scipy.org/doc/scipy/reference/special.html) 的**源材料**。

它有两个作用：

1. **函数目录**：用一长串 `.. autosummary::` 指令，按类别（Airy、椭圆、Bessel、统计、Gamma、误差函数、正交多项式、超几何……）列出全部公开函数，每个函数配一行简短说明。读者「想找某类函数」时，这里是第一入口。
2. **行为契约**：开篇就交代了本模块最重要的两条约定。

#### 4.1.2 核心流程

文档字符串开篇的几句话，是整个模块最重要的「使用须知」：

> Almost all of the functions below accept NumPy arrays as input arguments as well as single numbers. This means they follow broadcasting and automatic array-looping rules. Technically, they are NumPy universal functions. Functions which do not accept NumPy arrays are marked by a warning in the section description.

这段话的潜台词是：**「几乎所有函数都是 ufunc」是默认情况，例外要单独警告**。在文档里，「例外」长这样（以 Bessel 函数区的 `lmbda` 为例）：

```rst
The following function does not accept NumPy arrays (it is not a
universal function):

.. autosummary::

   lmbda -- Jahnke-Emden Lambda function, Lambdav(x).
```

也就是说：**判断一个函数「是不是 ufunc」，依据是它所在小节有没有这句警告文字，而不是看它的名字**。这条规则在 u1-l1 已经建立，本讲再次确认它就写在 `__init__.py` 里。

#### 4.1.3 源码精读

- [`__init__.py:13-19`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L13-L19)：开篇契约——「几乎所有函数都接受数组、遵循广播、本质是 ufunc；不接受的会在小节里警告」。
- [`__init__.py:110-116`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L110-L116)：一个「非 ufunc 警告」实例——`lmbda` 被单独标出。
- [`__init__.py:45-46`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L45-L46)：`Available functions` 大标题，其下按类别铺开全部 `autosummary`。

> 注意：这段超长文档字符串末尾带着 `# noqa: E501`（见第 783 行），意思是「行太长，别用 linter 报错」——因为 Sphinx 表格/指令不便随意折行。

#### 4.1.4 代码实践

**目标**：亲手感受文档字符串如何充当「函数目录」。

1. 在已安装 SciPy 的环境里执行 `python -c "import scipy.special as sc; print(sc.__doc__[:500])"`，观察打印出来的正是上面这段开篇契约。
2. 再执行 `python -c "import scipy.special as sc; print(sc.__doc__.count('autosummary'))"`，数一数文档里有多少处 `autosummary` 指令（每一处对应一个函数小节）。
3. 在源码里搜索字符串 `do not accept NumPy arrays`，统计有几处——这就是「非 ufunc 函数」被警告的小节数量。

**预期结果**：`sc.__doc__` 的开头确实是「Almost all of the functions below ...」；`autosummary` 出现几十次；`do not accept NumPy arrays` 出现若干次（如 Bessel 零点、Kelvin 零点、Riccati-Bessel、抛物柱函数序列等小节）。具体计数**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `__init__.py` 要把「是不是 ufunc」写在文档字符串里，而不是写在某个函数的 docstring 里？

**参考答案**：因为这是**模块级**的契约（广播、逐元素、`out=`），适用于一大批函数，逐一在每个函数 docstring 里重复既冗余又易遗漏；而「例外」按小节警告，正好与 `autosummary` 的分类结构对齐，读者定位某类函数时一眼就能看到。

**练习 2**：文档字符串里 `Raw statistical functions` 大类上方有一句 `.. seealso:: :mod:`scipy.stats`: Friendly versions of these functions.`，这说明什么？

**参考答案**：说明 `bdtr`、`fdtr`、`ndtr` 这类「原始统计函数」是 `scipy.stats` 的**底层**，`scipy.stats` 在它们之上提供了更友好的接口；普通用户应优先用 `scipy.stats`。

---

### 4.2 命名空间拼接：导入语句如何汇聚 7 个子模块

#### 4.2.1 概念说明

文档字符串结束后（第 786 行起），`__init__.py` 才开始真正的「干活」：用一连串导入语句，把分散在各子模块里的函数对象，**绑定到 `scipy.special` 这个命名空间**上。这一步是「命名空间拼接」（namespace assembly）。

这里有一个关键区分，初学者容易混淆：

- `from . import _ufuncs` —— 把**子模块对象本身**绑定成 `scipy.special._ufuncs`（一个 module 对象）。
- `from ._ufuncs import *` —— 把子模块 `__all__` 里列出的**那些函数**绑定成 `scipy.special.erf`、`scipy.special.gamma`……（直接可调用）。

两者通常**成对出现**：前者保证你能 `sc._ufuncs.xxx` 访问内部模块，后者把函数「提」到顶层货架。

#### 4.2.2 核心流程

`__init__.py` 后半段的导入可以分成几组，每组对应一个「货源」：

```
① 错误处理类           from ._sf_error import SpecialFunctionWarning, SpecialFunctionError
② 主体 ufunc           from . import _ufuncs ; from ._ufuncs import *        # 最大一路
③ 纯 Python 函数       from . import _basic   ; from ._basic import *
④ Array API 覆盖       from ._support_alternative_backends import *          # 覆写③④中的部分函数
⑤ logsumexp 家族       from ._logsumexp import logsumexp, softmax, log_softmax
⑥ 多输出聚合            from . import _multiufuncs ; from ._multiufuncs import *
⑦ 正交多项式            from . import _orthogonal ; from ._orthogonal import *
⑧ 小专项模块            from ._ellip_harm import (ellip_harm, ellip_harm_2, ellip_normal)
                        from ._lambertw import lambertw
                        from ._spherical_bessel import (spherical_jn, ...)
⑨ 已弃用旧命名空间      from . import add_newdocs, basic, orthogonal, specfun, sf_error, spfun_stats
```

注意一个**顺序细节**：第 ④ 行 `from ._support_alternative_backends import *` 排在 `_ufuncs`、`_basic` 之后。它不是新增函数，而是**用「带 Array API 多后端支持」的新版本覆盖**前面刚导入的同名函数（例如 `erf`、`gamma` 会被替换成能识别 PyTorch/JAX 数组的版本）。第 794–795 行的注释写得很直白：`# Replace some function definitions from _ufuncs and _basic to add Array API support`。

还有一点容易看漏：第 ⑤ 行对 `_logsumexp` 用的是**具名导入**（`import logsumexp, softmax, log_softmax`）而不是 `import *`。原因是 `_logsumexp` 模块里还有一些不该上货架的内部工具，具名导入可以精确地只搬这三个。

#### 4.2.3 源码精读

- [`__init__.py:786-789`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L786-L789)：先搬错误类，再成对导入主体 `_ufuncs`（`from . import _ufuncs` 绑定模块对象 + `from ._ufuncs import *` 搬函数）。
- [`__init__.py:791-792`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L791-L792)：同样成对地导入 `_basic`（纯 Python 实现的组合/零点/导数类函数）。
- [`__init__.py:794-796`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L794-L796)：`_support_alternative_backends` 的星号导入——这一步**覆盖**前面同名函数，加上 Array API 支持。
- [`__init__.py:798`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L798)：对 `_logsumexp` 用**具名导入**，只挑 `logsumexp`、`softmax`、`log_softmax` 三个。
- [`__init__.py:800-804`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L800-L804)：成对导入 `_multiufuncs`（`legendre_p_all`、`sph_harm_y_all` 等多输出聚合）和 `_orthogonal`（`roots_*`、`orthopoly1d` 系列）。
- [`__init__.py:806-817`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L806-L817)：三个「小专项」模块用括号具名导入——`_ellip_harm`（椭球谐调）、`_lambertw`（朗伯 W）、`_spherical_bessel`（球 Bessel）。
- [`__init__.py:819-820`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L819-L820)：第 ⑨ 组——已弃用的旧命名空间（详见 4.4）。

#### 4.2.4 代码实践

**目标**：验证「`from . import X`」与「`from .X import *`」确实做了不同的事。

1. 运行：
   ```python
   import scipy.special as sc
   print(type(sc._ufuncs))     # 应为 module 类型——因为 'from . import _ufuncs'
   print(type(sc.gamma))       # 应为 numpy.ufunc——因为 'from ._ufuncs import *' 把它提上来
   print("gamma" in dir(sc._ufuncs))  # True：gamma 住在 _ufuncs 模块里
   ```
2. 再验证覆盖关系：`from ._support_alternative_backends import *` 是否真的把 `gamma` 换成了带 Array API 支持的版本——读它的源码注释 [`__init__.py:794-795`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L794-L795) 即可确认意图（运行时验证留到 u10 单元）。

**预期结果**：`sc._ufuncs` 是模块对象，`sc.gamma` 是 `np.ufunc`，且 `sc.gamma is sc._ufuncs.gamma` 在**未开启** `SCIPY_ARRAY_API` 时为 `True`（覆盖层在默认关闭时直接回传原对象）。后一项**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_logsumexp` 用具名导入，而 `_basic` 用 `import *`？

**参考答案**：`_basic.__all__` 已经精确列出了要公开的函数，`import *` 正好按白名单搬运，省事；而 `_logsumexp` 若也用 `import *`，可能把模块内的辅助函数也搬上来，所以用具名导入精确控制只暴露 `logsumexp`/`softmax`/`log_softmax`。

**练习 2**：如果删除第 788 行的 `from . import _ufuncs`（只保留第 789 行的 `from ._ufuncs import *`），`scipy.special` 还能正常用吗？

**参考答案**：日常调用 `sc.erf` 不受影响（函数已被提上来），但 `sc._ufuncs` 这个名字就不存在了——任何依赖「通过子模块对象访问内部成员」的代码会出错。所以这两行各司其职，不能简单合并。

---

### 4.3 `__all__` 的组装：四路聚合 + 一份手动补丁

#### 4.3.1 概念说明

`__all__` 是一个模块「对外公开 API」的权威清单。对 `scipy.special` 来说，它还承担第二个职责：**告诉 IDE、`from scipy.special import *`、静态检查器「哪些名字算官方公开 API」**。

你可能会想：既然 `__init__.py` 已经用一堆 `import *` 把函数搬上来了，`__all__` 直接写 `__all__ = dir()` 不就行了？不行。原因有二：

1. `dir()` 会把子模块对象（`_ufuncs`、`_basic`）、已弃用命名空间（`basic`、`orthogonal`）、`test` 等都算进去，这些不该算「公开 API」。
2. 搬上来的函数里，有些来自 `_logsumexp`、`_ellip_harm`、`_lambertw`、`_spherical_bessel`、`_support_alternative_backends`——这些模块的 `__all__` **没有被纳入聚合**，必须手动补。

所以 `__init__.py` 采取了「**四路聚合 + 手动补丁**」的策略。

#### 4.3.2 核心流程

`__all__` 的最终值由两部分拼成：

```
__all__ = _ufuncs.__all__      # 第 1 路：主体 ufunc（最大，约 237 项，含 geterr/seterr/errstate）
       + _basic.__all__        # 第 2 路：纯 Python 函数（组合、零点、导数、factorial 等）
       + _orthogonal.__all__   # 第 3 路：roots_* 与 orthopoly1d 多项式
       + _multiufuncs.__all__  # 第 4 路：legendre_p_all / sph_harm_y_all 等多输出函数

__all__ += [                   # 手动补丁：来自「没参与聚合」的小模块
    'SpecialFunctionWarning', 'SpecialFunctionError',   # 来自 _sf_error
    'logsumexp', 'softmax', 'log_softmax',              # 来自 _logsumexp
    'multigammaln',                                      # 来自 _support_alternative_backends（唯一）
    'ellip_harm', 'ellip_harm_2', 'ellip_normal',       # 来自 _ellip_harm
    'lambertw',                                          # 来自 _lambertw
    'spherical_jn', 'spherical_yn', 'spherical_in', 'spherical_kn',  # 来自 _spherical_bessel
]
```

为什么 `_logsumexp`、`_ellip_harm`、`_lambertw`、`_spherical_bessel` 这几个模块不参与「四路聚合」？因为它们体量小、且在导入时用的是**具名导入**（见 4.2），它们本身甚至不一定定义了 `__all__`。与其让它们各自维护一份 `__all__` 再拼接，不如在 `__init__.py` 里直接把这几个名字写死——更直观、更不容易漏。

最微妙的是 `multigammaln`。它**不**来自 `_basic`，也**不**来自 `_ufuncs`，而是通过 `_support_alternative_backends` 间接进入命名空间（其本体住在 `_spfun_stats.py`）。而 `_support_alternative_backends.__all__` 是**故意不被纳入**聚合的——第 822–824 行的注释解释了原因：它覆盖的那些函数（如 `erf`、`gamma`）名字本来就已在 `_ufuncs.__all__` 里，不必重复算。唯独 `multigammaln` 是 `_support_alternative_backends` **独有**的，所以必须手动追加。这就是为什么它旁边有一句 `# pyrefly:ignore[bad-dunder-all]`——静态检查器 pyrefly 看不出这个名字是从哪儿来的（它是被 `import *` 间接带进来的），会误报「`__all__` 里有未定义名字」，这里显式忽略该告警。

#### 4.3.3 源码精读

- [`__init__.py:822-825`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L822-L825)：四路聚合的核心一句——`__all__ = _ufuncs.__all__ + _basic.__all__ + _orthogonal.__all__ + _multiufuncs.__all__`，以及解释「为什么不算 `_support_alternative_backends.__all__`」的注释。
- [`__init__.py:826-841`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L826-L841)：手动补丁清单，把来自 6 个小模块的 14 个名字（含两个错误类）追加进 `__all__`。
- [`__init__.py:832`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L832)：`'multigammaln', # pyrefly:ignore[bad-dunder-all]`——唯一一个「只能手动补、否则会从公开 API 漏掉」的名字。
- [`_ufuncs.pyi:5-243`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L5-L243)：`_ufuncs.__all__` 的完整定义——四路聚合里最大的一路，前三个是 `geterr`/`seterr`/`errstate`（错误处理三件套，详见 u2-l3），其余是各 ufunc 名。
- [`_ufuncs.pyi:291`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L291)（及其后大批）：每个名字都被声明为 `np.ufunc`，例如 `agm: np.ufunc`——类型桩用统一标注体现了「几乎全是 ufunc」这一设计。

> 旁注：`_ufuncs.pyi` 里还有一批**带下划线前缀**的内部 ufunc（如 `_spherical_jn`、`_lambertw`、`_riemann_zeta`，见 [`_ufuncs.pyi:258-290`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L258-L290)）。它们**不在** `__all__` 里，是给 `_spherical_bessel`、`_lambertw` 等「薄包装」用的底层内核（详见 u4-l4）。

#### 4.3.4 代码实践

**目标**：拆解 `__all__` 的四路贡献各占多少。

```python
import scipy.special as sc

# 四个货源各自的 __all__ 长度
print("_ufuncs   :", len(sc._ufuncs.__all__))
print("_basic    :", len(sc._basic.__all__))
print("_orthogonal:", len(sc._orthogonal.__all__))
print("_multiufuncs:", len(sc._multiufuncs.__all__))

# 总 __all__ 应等于「四路之和 + 14（手动补丁）」
total = (len(sc._ufuncs.__all__) + len(sc._basic.__all__)
         + len(sc._orthogonal.__all__) + len(sc._multiufuncs.__all__) + 14)
print("预测总数  :", total)
print("实际 __all__:", len(sc.__all__))
print("是否相等  :", total == len(sc.__all__))
```

**预期结果**：四路长度之和再加 14，应**精确等于** `len(sc.__all__)`。这能反证「四路聚合 + 14 个手动补丁」的组装模型是准确的。各路具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果有人新增了一个纯 Python 函数 `foo`，把它放进 `_basic.py` 并加入 `_basic.__all__`，但忘了在 `__init__.py` 文档字符串的 `autosummary` 里登记，会发生什么？

**参考答案**：`sc.foo` 能正常调用（因为 `from ._basic import *` 会搬上来，且 `__all__` 聚合会自动包含它），但**官方 HTML 文档里看不到它**——因为文档是按文档字符串里的 `autosummary` 渲染的。也就是说「能用」和「出现在文档里」是两条独立的链路。

**练习 2**：`multigammaln` 旁边的 `# pyrefly:ignore[bad-dunder-all]` 如果删掉，会怎样？

**参考答案**：静态检查器 pyrefly 会报警告 `bad-dunder-all`（`__all__` 中存在它「看不出定义来源」的名字），因为 `multigammaln` 是被 `from ._support_alternative_backends import *` 间接带进来的、没有显式 `import` 语句。功能不受影响，只是会留下一条静态检查告警。

---

### 4.4 已弃用的旧命名空间：为什么还留着它们

#### 4.4.1 概念说明

在 `__init__.py` 第 819–820 行，有这样一句看似多余的导入：

```python
# Deprecated namespaces, to be removed in v2.0.0
from . import add_newdocs, basic, orthogonal, specfun, sf_error, spfun_stats
```

这些 `add_newdocs`、`basic`、`orthogonal`、`specfun`、`sf_error`、`spfun_stats` 是 `scipy.special` **历史遗留**的子模块名。在很早的版本里，用户可能写过 `from scipy.special.basic import comb` 或 `scipy.special.specfun.xxx` 这样的代码。SciPy 后来把公开 API 统一收敛到了无前缀的名字（`scipy.special.comb`），并把内部模块改成了带下划线前缀的私有名（`_basic`、`_orthogonal`、`_sf_error`）。

但为了**不破坏老代码**，这些旧名字作为「别名」保留了一段时间，标注为「已弃用（deprecated）」，并计划在 v2.0.0 移除。本讲的学习目标之一就是「能识别它们」。

#### 4.4.2 核心流程

旧命名空间与新命名空间的对应关系大致是：

| 旧（已弃用，将在 v2.0.0 移除） | 新（当前推荐） | 说明 |
|------|------|------|
| `scipy.special.basic` | `scipy.special._basic` | 组合/阶乘/零点等纯 Python 函数的实现 |
| `scipy.special.orthogonal` | `scipy.special._orthogonal` | 正交多项式与高斯求积 |
| `scipy.special.sf_error` | `scipy.special._sf_error` | 错误类型与告警/异常类 |
| `scipy.special.specfun` | （底层 C 内核包装） | Zhang & Jin 的特殊函数库相关 |
| `scipy.special.spfun_stats` | `scipy.special._spfun_stats` | `multigammaln` 等统计函数 |
| `scipy.special.add_newdocs` | （已并入文档生成） | 旧版用来给 ufunc 添加 docstring 的脚本 |

带下划线前缀（`_basic`、`_orthogonal`…）是 Python 社区约定俗成的「内部实现，别从外部依赖」的标志。SciPy 正在把对外公开的名字与内部模块名彻底分开。

#### 4.4.3 源码精读

- [`__init__.py:819-820`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L819-L820)：旧命名空间的导入，注释明确写了 `to be removed in v2.0.0`。
- 对比 u1-l2 讲过的「命名约定」：不带下划线的小文件（如 `basic.py`、`orthogonal.py`、`sf_error.py`、`add_newdocs.py`、`specfun.py`、`spfun_stats.py`）正是这些「v2.0.0 弃用别名」的落点；而真正的实现带下划线（`_basic.py`、`_orthogonal.py`、`_sf_error.py`…）。

> 注意：这些旧模块对象（`basic`、`orthogonal` 等）会被 `dir(scipy.special)` 列出来，但**不在** `__all__` 里——所以它们「能访问」却不被当作「官方公开 API」。

#### 4.4.4 代码实践

**目标**：确认旧命名空间是「能访问但已弃用」的别名，并理解它们与 `__all__` 的关系。

```python
import scipy.special as sc

# 旧名字仍可访问
print(sc.basic is sc._basic)        # 多数情况下别名就是同一对象，待本地验证
print("basic" in sc.__all__)        # False：旧命名空间不算公开 API
print("_basic" in sc.__all__)       # False：带下划线的内部模块也不算
print("comb" in sc.__all__)         # True：comb 才是真正的公开函数

# 体会「能用 vs 官方」的差别
print(hasattr(sc, "orthogonal"), hasattr(sc, "_orthogonal"))  # 都为 True
```

**预期结果**：旧名字 `basic`/`orthogonal` 等确实能 `getattr` 到，但既不在 `__all__` 里、也随时可能在 v2.0.0 被删。新代码应一律用 `scipy.special.comb` 这类无前缀名。`sc.basic is sc._basic` 是否恒为 `True` **待本地验证**（取决于别名模块如何实现）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 SciPy 不直接删掉这些旧名字，而非要「弃用一段时间再删」？

**参考答案**：这是科学计算库的惯例——直接删除会让大量老脚本和下游库（教程、教材、第三方项目）瞬间报错。先标记为 deprecated、保留若干个版本，给用户迁移的时间，等到达一个主版本里程碑（v2.0.0）再清除。

**练习 2**：`dir(scipy.special)` 里会看到 `add_newdocs`，但 `scipy.special.add_newdocs` 不在 `__all__` 里。这两件事矛盾吗？

**参考答案**：不矛盾。`dir()` 反映「命名空间里实际存在哪些名字」（包括被导入的子模块），而 `__all__` 反映「作者声明哪些是官方公开 API」。一个名字可以「存在但不公开」——旧命名空间正属于这一类。

---

## 5. 综合实践

**任务**：完成规格里指定的综合练习——阅读 `__init__.py` 末尾的 `__all__` 赋值，回答四个函数的「出身」，并用代码对比 `dir(special)` 与 `special.__all__`。

### 第 1 步：指出四个函数分别来自哪个子模块

打开 [`__init__.py:825-841`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L825-L841)。这四个名字都**不在**「四路聚合」里，而都出现在「手动补丁清单」（第 826–841 行）中。回溯它们的导入语句：

| 函数 | 导入语句（行号） | 出处子模块 | 在 `__all__` 中的位置 |
|------|------|------|------|
| `logsumexp` | [`__init__.py:798`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L798) `from ._logsumexp import logsumexp, ...` | `_logsumexp` | 手动补丁（第 829 行） |
| `spherical_jn` | [`__init__.py:812-817`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L812-L817) `from ._spherical_bessel import (...)` | `_spherical_bessel` | 手动补丁（第 837 行） |
| `lambertw` | [`__init__.py:811`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L811) `from ._lambertw import lambertw` | `_lambertw` | 手动补丁（第 836 行） |
| `ellip_harm` | [`__init__.py:806-810`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L806-L810) `from ._ellip_harm import (...)` | `_ellip_harm` | 手动补丁（第 833 行） |

**结论**：这四个函数都来自「小专项模块」，因为它们各自的小模块没有参与 `__all__` 的四路聚合，所以必须手动追加——它们正是 4.3 所说「手动补丁」的典型成员。

### 第 2 步：用代码对比 `dir(special)` 与 `special.__all__`

```python
import scipy.special as sc

# 1) 只看「不带下划线开头」的公开名字
public_names = [n for n in dir(sc) if not n.startswith('_')]

# 2) 两个集合对比
all_set        = set(sc.__all__)
public_set     = set(public_names)

only_in_dir    = public_set - all_set   # 在 dir() 里、却不算官方 API
only_in_all    = all_set - public_set   # 理论上应为空（__all__ ⊆ 公开名）

print("len(dir 公开名)   :", len(public_names))
print("len(__all__)      :", len(sc.__all__))
print("仅 dir() 有（应为旧命名空间/子模块/test 等）:")
print("  ", sorted(only_in_dir))
print("仅 __all__ 有（应为空）:", sorted(only_in_all))
```

**需要观察的现象**：

1. `len(public_names)` 会**大于** `len(sc.__all__)`——因为 `dir()` 多算了子模块对象（`_ufuncs`、`_basic`…，虽然带下划线已被滤掉，但）和**不带下划线**的旧命名空间（`add_newdocs`、`basic`、`orthogonal`、`specfun`、`sf_error`、`spfun_stats`）以及 `test`。
2. `only_in_dir` 里大概率会出现：`add_newdocs`、`basic`、`orthogonal`、`specfun`、`sf_error`、`spfun_stats`、`test`（即 4.4 的旧命名空间 + `PytestTester` 实例 `test`）。
3. `only_in_all` 应当**为空**——这反证了 `__all__` 里的每个名字都确实被导入了命名空间。

**预期结果**：`__all__` 是 `dir()` 公开名的**子集**；多出来的名字正是已弃用旧命名空间与 `test`。具体数值**待本地验证**。

> 这个对比很好地体现了 `__all__` 的意义：它不是「命名空间里有什么」的描述，而是作者主动声明「我承诺哪些是稳定公开 API」的契约。旧命名空间虽然 `dir()` 能看到，但不在契约内，随时可能消失。

## 6. 本讲小结

- `__init__.py` 有**双重身份**：前 783 行文档字符串是按类别组织的**函数目录**（兼带「几乎所有函数都是 ufunc」的契约）；后半部分是一串**导入语句**，负责命名空间总装。
- `scipy.special` 命名空间由 `_ufuncs`、`_basic`、`_orthogonal`、`_multiufuncs`、`_logsumexp`、`_ellip_harm`、`_lambertw`、`_spherical_bessel` 等子模块**拼接**而成；`from . import X` 绑定模块对象，`from .X import *` 把函数提到顶层货架。
- `_support_alternative_backends` 排在 `_ufuncs`/`_basic` 之后，作用是**覆盖**同名函数、加上 Array API 多后端支持（运行时分发细节留到 u10）。
- `__all__` 采用「**四路聚合 + 14 项手动补丁**」：`_ufuncs.__all__ + _basic.__all__ + _orthogonal.__all__ + _multiufuncs.__all__`，再追加来自 6 个小模块的错误类与函数；`multigammaln` 是唯一必须手动补、否则会漏的特例。
- `# Deprecated namespaces` 那行导入的 `add_newdocs`、`basic`、`orthogonal`、`specfun`、`sf_error`、`spfun_stats` 是**历史遗留别名**，将在 v2.0.0 移除——它们 `dir()` 可见、但不在 `__all__`，不算官方公开 API。
- 「能用」（被导入命名空间）与「官方公开」（在 `__all__` 与文档 `autosummary` 里）是两条独立的链路，新增函数时两条都要照顾到。

## 7. 下一步学习建议

本讲把「`scipy.special` 的货架是怎么摆起来的」讲透了。接下来建议：

- **横向**：进入 **u2 单元（通用机制）**。本讲反复提到「几乎所有函数都是 ufunc」与「错误处理三件套」，u2-l1 会从 `_ufuncs.pyi` 的类型签名入手讲透 ufunc 的类型码、广播与 `out=`，u2-l3 会讲 `seterr`/`geterr`/`errstate` 如何与 `_sf_error` 联动。
- **纵向**：若你对「`_ufuncs` 这个最大的货源是怎么凭空生成出来的」更好奇，可以直接跳到 **u3 单元（代码生成管线）**，看 `functions.json` 如何驱动 `_generate_pyx.py` 生成 `_ufuncs.pyx`——那是本模块真正的「工程心脏」。
- **延伸阅读**：对照本讲的导入语句，去翻一眼 [`_basic.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_basic.py)、[`_logsumexp.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_logsumexp.py)、[`_orthogonal.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_orthogonal.py) 顶部的 `__all__` 定义，亲手验证「四路聚合」每一路的真实内容——这会让你对命名空间拼接的理解从「读讲义」变成「能自查源码」。
