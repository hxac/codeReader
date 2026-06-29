# 目录结构与包入口、延迟导入

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `scipy` 顶层包的目录是怎么组织的、哪些是「真源码」、哪些是「构建产物」。
- 在源码里找到包入口 `scipy/__init__.py`，并解释它在你 `import scipy` 的那一瞬间到底做了哪几件事。
- 理解 **PEP 562 模块级 `__getattr__`** 如何实现「子包用到才加载」的延迟导入（lazy import），并能解释它带来的好处。
- 看懂导入时的两道「安全闸门」：NumPy 版本校验、扩展模块可用性检查。
- 认识 `PytestTester`，理解为什么 `scipy.test()` 和 `scipy.linalg.test()` 这样的写法能直接跑测试。

本讲承接 [u1-l1](u1-l1-project-overview.md)（SciPy 是什么、有哪些子包）和 [u2-l2](u1-l2-build-system-and-source-build.md)（构建系统）。本讲只关心一个问题：**当你在终端敲下 `python -c "import scipy"` 时，究竟发生了什么。**

## 2. 前置知识

### 2.1 什么是包入口 `__init__.py`

在 Python 里，一个「包（package）」就是一个含有 `__init__.py` 的目录。当你写 `import scipy` 时，Python 解释器会去执行 `scipy/__init__.py` 这个文件里的所有顶层代码。这个文件就是包的**入口**——它决定了「导入这一刻」会发生什么。

> 关键直觉：`import scipy` 不是「免费」的。它会真的运行一段 Python 代码。这段代码可以做任何事：校验依赖、加载配置、注册测试入口、抛出有用的报错。本讲要精读的就是这段代码。

### 2.2 什么是延迟导入（lazy import）

普通的导入是「饿汉式」的：你 `import scipy` 时，如果入口里写了 `from . import cluster`，那么 `cluster` 子包也会被立刻加载。

延迟导入是「懒汉式」的：入口里**并不**真的去 import 子包，而是只把子包的**名字**登记到一个列表里；等你真正第一次访问 `scipy.cluster` 时，再去加载它。这样做的好处是：

- **导入快**：`import scipy` 几乎瞬间完成，不会因为某个子包（比如要编译的 `linalg`）而变慢。
- **省内存**：你只用 `scipy.constants`，就不会把庞大的 `scipy.signal` 也加载进来。
- **避免循环导入**：子包之间互相依赖时，延迟加载能解耦启动顺序。

### 2.3 PEP 562：模块级 `__getattr__`

Python 3.7 的 [PEP 562](https://peps.python.org/pep-0562/) 允许在**模块**里定义两个特殊函数：

- `__getattr__(name)`：当访问模块里**不存在**的属性时被调用。
- `__dir__()`：当调用 `dir(模块)` 时被调用。

这正是延迟导入的实现基础：把子包名字放进一个清单，但**不**真正定义它们；当用户访问 `scipy.cluster` 时，因为 `cluster` 没有被定义，Python 就会去调用 `scipy.__getattr__("cluster")`，我们在那里再执行真正的 import。

> 对初学者的提示：`__getattr__` 在「类」里很常见（属性找不到时触发），PEP 562 只是把同样的机制搬到了「模块」层面。

### 2.4 数学术写约定

本讲用区间表示版本约束。行内公式写作 \( \text{version} \in [2.0.0,\ 9.9.99) \)，表示「大于等于 2.0.0 且严格小于 9.9.99」。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 | 是否提交到 git |
|------|------|----------------|
| `scipy/__init__.py` | 顶层包入口，本讲主角 | ✅ 真源码 |
| `scipy/_lib/_testutils.py` | 提供 `PytestTester` 等测试工具 | ✅ 真源码 |
| `scipy/version.py` | 提供 `__version__` 版本号 | ❌ **构建时生成** |
| `scipy/__config__.py.in` | `show_config()` 的模板 | ✅ 模板源码 |
| `scipy/meson.build` | 构建脚本，负责生成上面两个文件 | ✅ 真源码 |
| `tools/gitversion.py` | 计算/写入版本号的脚本 | ✅ 真源码 |
| `scipy/_distributor_init.py` | 发行商自定义初始化钩子 | ✅ 真源码 |

> ⚠️ 重要：规格里列出的 `scipy/version.py` **在源码树里并不存在**——它是 meson 构建时动态生成的（详见 [u1-l2](u1-l2-build-system-and-source-build.md)）。所以本讲不会伪造它的行号，而是讲清楚它「从哪里来」。

## 4. 核心概念与源码讲解

### 4.1 顶层包入口 `scipy/__init__.py` 的整体结构

#### 4.1.1 概念说明

`scipy/__init__.py` 是整个 SciPy 包的总开关。它只有约 140 行，却承担了五件大事：

1. **加载构建配置**：从构建产物 `scipy/__config__.py` 里取出 `show_config`。
2. **登记版本号**：从构建产物 `scipy/version.py` 里取出 `__version__`。
3. **校验 NumPy 版本**：NumPy 太老或太新都会报警告。
4. **探测扩展模块**：故意导入一个编译扩展（`LowLevelCallable`），作为「安装是否损坏」的金丝雀（canary）检查。
5. **挂上 `test` 和延迟子包加载**：注册 `PytestTester`，定义 `submodules` 清单和 `__getattr__`。

#### 4.1.2 核心流程

`import scipy` 时的执行顺序（严格自上而下）：

```
1. import importlib          （为延迟导入做准备）
2. 取 numpy 版本号
3. try: from scipy.__config__ import show   （加载构建配置）
4. from scipy.version import version        （取版本号）
5. 执行 _distributor_init                   （发行商钩子）
6. 校验 numpy 版本是否落在 [2.0.0, 9.9.99)
7. try: from scipy._lib._ccallback import LowLevelCallable  （金丝雀检查）
8. test = PytestTester(__name__)            （挂上 .test()）
9. 定义 submodules / __all__ / __dir__ / __getattr__
```

注意第 3、4 步依赖两个**构建产物**文件。如果你在源码目录里（还没构建）直接 `import scipy`，第 3 步就会失败并给出一个非常有用的报错——这一点会在 4.3 节展开。

#### 4.1.3 源码精读

**（a）文件开头的 docstring 是「子包地图」的权威来源之一。** 它列出了所有公开子包及其一句话用途：

[scipy/__init__.py:8-29](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L8-L29) —— 列出 `cluster`…`stats` 共 17 个子包及其一句话说明。

[scipy/__init__.py:30-37](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L30-L37) —— 列出主命名空间下的 4 个公共 API：`__version__`、`LowLevelCallable`、`show_config`、`test`。

**（b）先准备工具，再加载配置与版本。**

[scipy/__init__.py:41](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L41) —— `import importlib as _importlib`，这个引用稍后会被 `__getattr__` 用到。

[scipy/__init__.py:46-52](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L46-L52) —— 从构建产物 `scipy.__config__` 取 `show` 并改名为 `show_config`。如果失败（典型场景：在未构建的源码目录里 import），抛出一句人话报错。

[scipy/__init__.py:55](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L55) —— 从构建产物 `scipy.version` 取 `version` 并改名为 `__version__`。

**（c）发行商钩子。**

[scipy/__init__.py:58-60](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L58-L60) —— 执行 `from . import _distributor_init` 然后立刻 `del`。它本身只是尝试 import 一个可能不存在的 `_distributor_init_local`，给 Linux 发行版（如 conda、Debian）留一个注入自定义初始化代码的口子（见 [scipy/_distributor_init.py:15-18](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_distributor_init.py#L15-L18)）。

**（d）收尾：`submodules` 清单与 `__all__`。**

[scipy/__init__.py:95-113](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L95-L113) —— `submodules` 列表，17 个子包名字，**正是延迟导入的依据**。

[scipy/__init__.py:115-120](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L115-L120) —— `__all__` = 子包 + 4 个公共 API。注意它**只列名字**，并不导入。

> ⚠️ 一致性约束：docstring 里的子包清单（L8-L29）、`submodules` 列表（L95-L113）、`__all__`（L115-L120）三处必须保持一致。这是 u1-l1 提到的「双重权威来源」的具体体现。

#### 4.1.4 代码实践

**目标**：亲眼看到 `import scipy` 不会加载全部子包。

**步骤**：

1. 打开一个 Python 解释器。
2. 先执行 `import scipy`。
3. 执行 `import sys` 后查看 `sorted(name for name in sys.modules if name.startswith('scipy.') )`，记录此时已经加载的 `scipy.*` 模块。
4. 再执行 `scipy.cluster`（只访问一次），再次打印上面的列表，对比新增了哪些模块。

**预期现象**：

- 第 3 步：你会看到少量必备模块（如 `scipy._lib` 相关、`scipy.__config__`、`scipy.version`），但**看不到** `scipy.cluster`、`scipy.linalg` 等业务子包。
- 第 4 步：访问 `scipy.cluster` 后，`scipy.cluster` 及其依赖才出现在 `sys.modules` 里。

**待本地验证**：第 3 步具体列出的模块集合，取决于你的安装方式，请以本地实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `scipy/__init__.py` 里要用 `del _distributor_init`（[L60](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L60)）？

> **答案**：`_distributor_init` 只是一个「启动钩子」，执行完它的副作用即可，不需要把它暴露成 `scipy._distributor_init` 这个公共属性。`del` 是为了保持顶层命名空间干净。

**练习 2**：如果你发现 docstring 里的子包列表和 `submodules` 不一致，会造成什么后果？

> **答案**：docstring 是给人读的文档，`submodules` 是给 `__getattr__` 用的程序依据。若某子包只在 docstring 里、不在 `submodules` 里，那么 `scipy.该子包` 的延迟导入将不生效（会走 `__getattr__` 的 fallback 分支抛 `AttributeError`）。所以二者必须一致。

---

### 4.2 延迟导入机制 `__getattr__`

#### 4.2.1 概念说明

这是本讲的核心。SciPy 用 PEP 562 的模块级 `__getattr__` 实现延迟导入。要点是：

- 入口**不**写 `from . import cluster` 这种语句。
- 只把子包名字登记在 `submodules` 列表里。
- 定义 `__getattr__(name)`：当 `name` 属于 `submodules` 时，才真正 `import` 它并返回。

这样 `import scipy` 永远很快，而每个子包「第一次被点到名」时才加载。

#### 4.2.2 核心流程

```
用户写 scipy.cluster
  └─ Python 发现 scipy 命名空间里没有 cluster（因为没 import）
       └─ 触发 scipy.__getattr__("cluster")
            └─ "cluster" in submodules ?  → 是
                 └─ return importlib.import_module("scipy.cluster")
                      └─ 真正加载 cluster 子包，并缓存进 sys.modules
```

后续再次访问 `scipy.cluster` 时，因为模块对象已经被注入到 `sys.modules` 和 `scipy` 的命名空间，就不会再触发 `__getattr__` 了（只加载一次）。

#### 4.2.3 源码精读

**整个延迟导入就靠这一段：**

[scipy/__init__.py:127-141](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L127-L141) —— 模块级 `__getattr__`，三段式分支：

- [L128-L129](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L128-L129)：`name in submodules` 时，用 `_importlib.import_module(f'scipy.{name}')` 按需加载。这是延迟导入的核心。
- [L130-L134](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L130-L134)：对已删除的旧子包 `odr` 给出友好报错（`scipy.odr` 在 1.17 弃用、1.19 移除，建议改用 `odrpack`）。
- [L135-L141](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L135-L141)：兜底分支，先尝试从 `globals()` 取（用于 `LowLevelCallable`/`test`/`show_config`/`__version__` 这几个已定义的名字），取不到再抛标准 `AttributeError`。

**配合 `__dir__` 让 `dir(scipy)` 也「看起来对」：**

[scipy/__init__.py:123-124](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L123-L124) —— `__dir__()` 返回 `__all__`，这样即便子包还没加载，`dir(scipy)` 也能列出它们的名字，补全（tab completion）也能正常工作。

> 设计洞察：`__getattr__` 负责按需「加载」，`__dir__` 负责让工具「看见」——两者配合，用户感受不到「子包其实还没加载」。

#### 4.2.4 代码实践

**目标**：用 `importlib` 手动触发延迟导入，并测量加载耗时差异。

**步骤**（示例代码，请自行运行）：

```python
# 示例代码：观察延迟导入的耗时
import time
import importlib
import sys

# 1. 确认尚未加载 linalg
print("linalg 已加载?", "scipy.linalg" in sys.modules)

# 2. 手动触发延迟导入（等价于第一次访问 scipy.linalg）
t0 = time.perf_counter()
mod = importlib.import_module("scipy.linalg")  # 等价于执行 scipy.__getattr__("linalg")
dt = time.perf_counter() - t0
print(f"首次加载 scipy.linalg 耗时: {dt*1000:.1f} ms")

# 3. 第二次访问（已缓存）应该极快
t0 = time.perf_counter()
mod2 = importlib.import_module("scipy.linalg")
dt2 = time.perf_counter() - t0
print(f"第二次访问 scipy.linalg 耗时: {dt2*1000:.4f} ms")
```

**需要观察的现象**：

- 首次加载 `scipy.linalg` 通常需要几十到几百毫秒（它要加载编译扩展和 BLAS 封装）。
- 第二次访问几乎为 0，因为模块已缓存在 `sys.modules["scipy.linalg"]`。

**待本地验证**：具体毫秒数与机器、安装方式、磁盘缓存相关，请以本地实测为准。

#### 4.2.5 小练习与答案

**练习 1**：如果用户写 `from scipy import cluster`，会触发 `__getattr__` 吗？

> **答案**：会。`from scipy import cluster` 在底层等价于「先 `import scipy`，再 `scipy.cluster`」，后者正是属性访问，会触发 `__getattr__("cluster")`。所以 `from ... import` 同样享受延迟加载。

**练习 2**：`__getattr__` 的兜底分支（[L135-L141](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L135-L141)）为什么要先查 `globals()`，而不是直接抛 `AttributeError`？

> **答案**：因为 `LowLevelCallable`、`test`、`show_config`、`__version__` 这些名字是在入口里通过普通赋值/导入绑定到模块命名空间（`globals()`）的，但它们**不在** `submodules` 里。PEP 562 的 `__getattr__` 只在「正常查找失败」时才被调用，所以这里用 `globals()` 兜底取回它们；只有连 `globals()` 都没有时才说明名字真的不存在，才抛 `AttributeError`。

---

### 4.3 导入时的版本校验与扩展模块检查

#### 4.3.1 概念说明

`import scipy` 不只是「加载代码」，它还要做两道**安全检查**：

1. **NumPy 版本闸门**：SciPy 强依赖 NumPy 的 C-API，版本太老会缺特性、太新可能 ABI 不兼容，所以必须把 NumPy 版本限定在一个区间内。
2. **扩展模块金丝雀**：SciPy 的核心计算都是编译扩展（`.so`/`.pyd`）。如果安装损坏（比如只装了纯 Python 部分但扩展没编出来），很多函数会运行时才崩。入口故意先 import 一个扩展模块，**把崩溃提前到导入时**，并给出可读的报错。

此外，本节还要回答一个初学者常问的问题：`scipy.__version__` 这个版本号到底是从哪儿来的？

#### 4.3.2 核心流程

**版本约束**（数学表示）：

设当前 NumPy 版本为 \( v_{np} \)，则要求

\[
\text{Version}(2.0.0) \;\le\; \text{Version}(v_{np}) \;<\; \text{Version}(9.9.99)
\]

不满足时只发 `UserWarning`（不阻止导入），提醒用户环境可能有问题。

**版本号来源链**：

```
pyproject.toml 里的 version = "..."
        │ (被 tools/gitversion.py 读取)
        ▼
tools/gitversion.py 在构建时运行
        │ (若是 dev 版本，再拼上 git 提交日期+hash)
        ▼
写入构建产物 scipy/version.py（含 version、full_version、release 等变量）
        │ (由 scipy/meson.build 的 custom_target 触发)
        ▼
scipy/__init__.py: from scipy.version import version as __version__
```

#### 4.3.3 源码精读

**（a）NumPy 版本校验。**

[scipy/__init__.py:63-74](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L63-L74) —— 注意它用的是 SciPy 自己 vendored 的 `scipy._external.packaging_version`（不依赖外部 `packaging` 包），定义 `np_minversion='2.0.0'`、`np_maxversion='9.9.99'`，校验通过后 `del Version, parse` 清理命名空间。

[scipy/__init__.py:65-66](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L65-L66) —— 版本上下界的定义处。注释提到维护分支要把 `np_maxversion` 调到 `N+3`。

**（b）扩展模块金丝雀检查。**

[scipy/__init__.py:77-87](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L77-L87) —— 注释明确说「这是 SciPy 内第一个被导入的扩展模块」。故意 import `LowLevelCallable`（来自 `scipy._lib._ccallback`，一个编译扩展），失败则抛「安装似乎损坏，请重装」的友好报错。这种「先探测一个扩展」的写法在工程上叫**金丝雀检查（canary check）**。

**（c）`show_config` 的来源与「在源码目录里 import」的报错。**

[scipy/__init__.py:46-52](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L46-L52) —— 如果 `scipy.__config__` 导入失败，抛出经典报错：*"you cannot import SciPy while being in scipy source directory"*。这正是 u1-l2 讲过的——构建产物 `__config__.py` 不存在时（比如你 `cd` 进了未构建的源码目录），第一时间提醒你。

**（d）`show_config` 的真实实现来自构建产物模板。**

`scipy/__config__.py` 是构建时由模板 `scipy/__config__.py.in` 渲染而来：

[scipy/__config__.py.in:117-166](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__config__.py.in#L117-L166) —— `show()` 函数，支持 `mode='stdout'`（打印）或 `mode='dicts'`（返回字典）。模板里的 `@BLAS_NAME@`、`@LAPACK_VERSION@` 等占位符会在构建时被 meson 替换成真实值。

[scipy/__config__.py.in:25-108](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__config__.py.in#L25-L108) —— `CONFIG` 字典的结构，分四大块：Compilers、Machine Information、Build Dependencies（含 BLAS/LAPACK/pybind11）、Python Information。这就是 `scipy.show_config()` 输出的全部内容来源。

**（e）版本号的生成逻辑（构建产物 `version.py` 的来源）。**

> 注意：`scipy/version.py` 不在源码树里（`git ls-files scipy/version.py` 为空），所以这里只能引用**生成它的脚本和构建规则**。

[scipy/meson.build:432-451](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/meson.build#L432-L451) —— meson 用 `custom_target` 调用 `tools/gitversion.py` 生成 `version.py`；如果是从 sdist 构建（`version.py` 已存在）则直接安装。

[tools/gitversion.py:6-18](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/tools/gitversion.py#L6-L18) —— `init_version()` 从 `pyproject.toml` 读出基础版本号。

[tools/gitversion.py:21-54](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/tools/gitversion.py#L21-L54) —— `git_version()`：只有当版本号含 `dev`（开发版）时，才会追加 `+git<日期>.<hash前7位>`（见 [L50-L52](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/tools/gitversion.py#L50-L52)）。这就是为什么你从 git 源码构建时版本号形如 `1.19.0.dev0+git20260628.814922d`。

[tools/gitversion.py:71-83](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/tools/gitversion.py#L71-L83) —— 生成的 `version.py` 内容模板，包含 `version`、`full_version`、`short_version`、`git_revision`、`release` 等变量。

> 一句话：`scipy.__version__` 不是写死在源码里的字符串，而是构建时根据 `pyproject.toml` + git 信息「算」出来、写进 `version.py` 的。这也解释了为什么 u1-l2 说 `version.py` 是构建动态生成的。

#### 4.3.4 代码实践

**目标**：用 `scipy.show_config()` 收集构建信息，并理解它来自构建产物。

**步骤**：

1. 运行 `python -c "import scipy; scipy.show_config()"`，观察输出的四大块（Compilers / Machine Information / Build Dependencies / Python Information）。
2. 运行 `python -c "import scipy; print(scipy.show_config(mode='dicts')['Build Dependencies']['blas'])"`，以字典形式取出 BLAS 信息。
3. 执行 `python -c "import scipy; print(scipy.__version__, scipy.version.release)"`，查看版本号以及它是否是正式发布版（`release` 为 `True` 表示非 dev 版）。

**预期现象**：

- `show_config()` 会显示 BLAS/LAPACK 的名称（如 `openblas`）、版本、检测方式（如 `meson`/`pkgconfig`）等。
- `__version__` 若是从 git 主干构建的开发版，会带 `+git...` 后缀，且 `scipy.version.release` 为 `False`。

**待本地验证**：BLAS 名称、版本号具体取值取决于你的构建环境，以本地输出为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 NumPy 版本不满足时只是 `warn`，而不是直接 `raise`？

> **答案**：版本边界是「软约束」。某些略超边界的组合也许仍能工作；直接 `raise` 会把用户卡死，连试的机会都没有。用 `UserWarning` 既提醒了风险，又不阻断使用，是更友好的工程取舍。

**练习 2**：如果你 `cd` 进 SciPy 的 git 源码目录后运行 `python -c "import scipy"`，会看到哪句报错？为什么？

> **答案**：会看到 *"Error importing SciPy: you cannot import SciPy while being in scipy source directory"*（[L49-L52](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L49-L52)）。因为当前目录的 `scipy/` 还没有构建产物 `__config__.py`，`from scipy.__config__ import show` 失败，于是抛出这句提示，引导你退出源码目录再用已安装的 SciPy。

**练习 3**：开发版 `1.19.0.dev0` 构建出来的 `__version__` 会比正式版多出什么？

> **答案**：多出 `+git<日期>.<7位hash>` 后缀（见 [tools/gitversion.py:50-52](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/tools/gitversion.py#L50-L52)），形如 `1.19.0.dev0+git20260628.814922d`，便于精确定位是哪一次提交构建的。

---

### 4.4 PytestTester：包自带的 `test()` 入口

#### 4.4.1 概念说明

`PytestTester` 是 SciPy 给每个（子）包都挂上的一个「测试入口对象」。它让你能写：

- `scipy.test()` —— 跑整个 SciPy 的测试。
- `scipy.cluster.test()` —— 只跑 `cluster` 子包的测试。

它本质上是一个**可调用对象**（实现了 `__call__`），被构造时绑定到一个模块名，调用时用 pytest 跑该模块的测试。这是 SciPy「自带测试入口」设计的核心。

#### 4.4.2 核心流程

```
scipy/__init__.py 里：
    from scipy._lib._testutils import PytestTester
    test = PytestTester(__name__)   # __name__ == "scipy"
    del PytestTester                # 不把类本身暴露为公共属性

用户调用 scipy.test(label="fast") 时：
    └─ PytestTester.__call__(label="fast")
         └─ 用 sys.modules[self.module_name] 找到 scipy 包
         └─ 拼 pytest 参数（--showlocals、-m "not slow" 等）
         └─ 调用 pytest.main(args) 真正跑测试
         └─ 返回 (exit code == 0) 的布尔值
```

子包 `cluster` 的 `__init__.py` 里也有类似的 `test = PytestTester("scipy.cluster")`，所以 `scipy.cluster.test()` 只跑 cluster 的测试。

#### 4.4.3 源码精读

**（a）入口里如何挂上 `test`。**

[scipy/__init__.py:90-92](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L90-L92) —— `from scipy._lib._testutils import PytestTester` → `test = PytestTester(__name__)` → `del PytestTester`。`__name__` 在这里就是 `"scipy"`。

**（b）`PytestTester` 类本体。**

[scipy/_lib/_testutils.py:63-142](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_testutils.py#L63-L142) —— `PytestTester` 完整定义。

[scipy/_lib/_testutils.py:93-94](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_testutils.py#L93-L94) —— `__init__` 只记下模块名，**不**加载 pytest。这也是一种延迟——只有真正调用 `test()` 才 `import pytest`（见 [L98](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_testutils.py#L98)）。

[scipy/_lib/_testutils.py:96-142](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_testutils.py#L96-L142) —— `__call__` 是核心：构造 `pytest_args`，处理 `label`（`"fast"` 时加 `-m "not slow"`）、`verbose`、`coverage`、`parallel`（依赖 pytest-xdist，见 [L127-L133](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_testutils.py#L127-L133)），最后 `pytest.main(pytest_args)` 并返回布尔结果（[L137-L142](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_testutils.py#L137-L142)）。

[scipy/_lib/_testutils.py:42](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_testutils.py#L42) —— 这个模块还导出了 `check_free_memory`、`_TestPythranFunc`、`IS_MUSL` 等其他共享测试工具，是后续 u13-l2 测试体系讲义的基础。

> 设计洞察：`PytestTester` 把「在哪个命名空间跑测试」用构造参数 `module_name` 参数化，于是同一个类既能服务 `scipy`，也能服务每个子包，避免了重复代码。

#### 4.4.4 代码实践

**目标**：用 `scipy.test` 收集测试入口信息（不一定要跑完所有测试）。

**步骤**（示例代码）：

```python
# 示例代码：探测 test 入口对象
import scipy

# 1. 确认 test 是一个可调用对象，且绑定了 "scipy"
print("类型:", type(scipy.test))
print("绑定模块:", scipy.test.module_name)   # 应为 "scipy"

# 2. 只收集、不真跑：用 pytest 的 --collect-only 走 extra_argv
#    （注意：这会真正启动 pytest 收集，可能较慢）
# scipy.test(extra_argv=["--collect-only", "-q"])
```

**需要观察的现象**：

- `type(scipy.test)` 是 `PytestTester`，`module_name` 是 `"scipy"`。
- 访问 `scipy.cluster.test`，其 `module_name` 应为 `"scipy.cluster"`（说明子包各自挂了自己的实例）。

**待本地验证**：`--collect-only` 的具体耗时和输出条目数取决于安装，以本地为准。如果想跑真实测试，建议先用范围小的子包，例如 `scipy.constants.test(label="fast")`，避免一次性跑太久。

> 提示：完整的测试运行方式（pytest 配置、`spin test`）是 [u1-l4](u1-l4-dev-workflow-spin.md) 的主题，本讲只需理解 `test` 这个对象的来源即可。

#### 4.4.5 小练习与答案

**练习 1**：`scipy.test` 和 `scipy.cluster.test` 是不是**同一个**对象？

> **答案**：不是。它们是 `PytestTester` 的两个不同实例，分别绑定 `"scipy"` 和 `"scipy.cluster"`（`module_name` 不同），所以作用范围不同。但它们是**同一个类**的实例。

**练习 2**：为什么 `PytestTester.__init__` 里不直接 `import pytest`？

> **答案**：为了让 `import scipy` 保持轻量。pytest 是个较重的依赖，只有真正需要跑测试的人才该付这个加载成本。把它推迟到 `__call__`（[L98](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_testutils.py#L98)）里才 import，是延迟加载思想的又一处体现。

---

## 5. 综合实践

**任务**：写一个脚本，把本讲四个最小模块串起来——目录结构、延迟导入、版本/配置检查、`test` 入口。

**要求**完成以下脚本（示例代码，请自行运行并填入观察结果）：

```python
# 综合实践：scipy 包入口探秘
import sys
import time
import importlib

# === 模块 1：目录结构（看哪些是构建产物）===
print("== 1. 命名空间里的公共 API ==")
import scipy
print(sorted(n for n in dir(scipy) if not n.startswith('_') or n == '__version__'))

# === 模块 2：延迟导入（对比首次/二次访问耗时）===
print("\n== 2. 延迟导入 ==")
print("导入前 linalg 是否已加载:", "scipy.linalg" in sys.modules)
t0 = time.perf_counter()
_ = importlib.import_module("scipy.linalg")  # 触发 scipy.__getattr__("linalg")
print(f"首次加载 linalg 耗时: {(time.perf_counter()-t0)*1000:.1f} ms")
print("导入后 linalg 是否已加载:", "scipy.linalg" in sys.modules)

# === 模块 3：版本与配置检查 ===
print("\n== 3. 版本与配置 ==")
print("__version__      :", scipy.__version__)
print("是否正式发布版   :", scipy.version.release)
cfg = scipy.show_config(mode='dicts')
print("BLAS 名称        :", cfg['Build Dependencies']['blas'].get('name'))
print("BLAS 检测方式    :", cfg['Build Dependencies']['blas'].get('detection method'))

# === 模块 4：test 入口对象 ===
print("\n== 4. test 入口 ==")
print("scipy.test 绑定模块    :", scipy.test.module_name)
print("scipy.cluster.test 绑定:", scipy.cluster.test.module_name)
```

**你需要回答的问题**（写在注释或报告里）：

1. 第 2 步，首次加载 `scipy.linalg` 耗时大约是多少？第二次访问呢？这说明延迟导入省下了什么？
2. 第 3 步，你的 SciPy 是正式发布版还是 dev 版？BLAS 用的是什么？
3. 第 4 步，`scipy.test` 与 `scipy.cluster.test` 的 `module_name` 是否不同？这印证了 4.4 节的哪个设计？

**预期结果**：脚本应能顺利打印四段信息，且第 4 步两个 `module_name` 不同（分别是 `"scipy"` 和 `"scipy.cluster"`）。具体数值**待本地验证**。

## 6. 本讲小结

- `scipy/__init__.py` 是包入口，`import scipy` 时依次完成：加载构建配置 → 取版本号 → 发行商钩子 → NumPy 版本校验 → 扩展模块金丝雀检查 → 挂上 `test` → 定义延迟导入设施。
- **延迟导入**靠 PEP 562 的模块级 `__getattr__`：子包名字只登记在 `submodules`，第一次被访问时才 `importlib.import_module` 真正加载，`__dir__` 配合让补全/`dir()` 正常。
- 两道安全闸门：NumPy 版本须 \( \in [2.0.0, 9.9.99) \)（不满足只警告）；故意先导入编译扩展 `LowLevelCallable` 作为「安装是否损坏」的金丝雀。
- `scipy.__version__` 与 `scipy.show_config()` 的数据都来自**构建产物** `version.py` / `__config__.py`——前者由 `tools/gitversion.py` 在构建时生成（dev 版追加 git 后缀），后者由 `__config__.py.in` 模板渲染。
- `PytestTester` 是可调用对象，按构造时绑定的模块名跑 pytest，使 `scipy.test()` 与 `scipy.<子包>.test()` 成为统一测试入口。
- docstring 子包清单、`submodules` 列表、`__all__` 三处必须一致，这是公共 API 治理的基本要求（为 u13-l4 埋下伏笔）。

## 7. 下一步学习建议

- **下一讲 [u1-l4](u1-l4-dev-workflow-spin.md)**：学习 `spin` 开发 CLI 与 pytest 配置，把本讲的 `scipy.test()` 与正式的 `spin test` 流程对接起来。
- 若想深入了解「延迟导入」的更多用法，可继续阅读 [scipy/_lib/_util.py](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_util.py)，那里有 `_lazyselect` 等更细粒度的惰性机制（对应 [u2-l1](u2-l1-lib-util-helpers.md)）。
- 若对「构建产物如何生成」感兴趣，建议精读 `scipy/meson.build` 中 [version.py 生成段](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/meson.build#L432-L451) 与 `tools/gitversion.py`，并结合 u1-l2 的构建链理解。
- 想看子包如何各自挂上 `test`，可打开任一子包入口（如 `scipy/cluster/__init__.py`）观察 `test = PytestTester(__name__)` 的写法。
