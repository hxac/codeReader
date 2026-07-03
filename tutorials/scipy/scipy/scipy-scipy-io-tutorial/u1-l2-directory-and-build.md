# 目录结构、构建方式与公共 API 约定

## 1. 本讲目标

学完本讲，读者应该能够：

- 读懂 `scipy/io` 目录里的文件按什么规则分类，并能快速定位任意一种格式的实现代码。
- 读懂 `scipy/io/meson.build` 如何把 Python 文件和子目录组织成一个可安装的包。
- 区分「公共模块」和「私有实现模块（带 `_` 前缀）」，理解 SciPy 为什么坚持这种命名约定。
- 解释 `scipy.io.mmio` 这类「弃用命名空间包装」是如何通过模块级 `__getattr__` 转发到私有模块、并向用户发出 `DeprecationWarning` 的。

本讲承接上一讲建立的全局地图：你已经知道 scipy.io 是一个「多格式读写器集合」，但还没看过它的代码是怎么摆放、怎么构建的。本讲补齐这一层，为后面逐格式深入源码打下基础。本讲**刻意不展开任何一种格式的读写二进制细节**，只讲「骨架」。

## 2. 前置知识

- **Python 包（package）与模块（module）**：一个目录里有 `__init__.py` 就是包；包里每个 `.py` 文件是一个模块。`import scipy.io` 会触发 `scipy/io/__init__.py` 的执行。
- **`__init__.py` 的作用**：它是包的「入口脚本」，负责决定「这个包对外暴露哪些名字」。常见做法是在里面写若干行 `from .子模块 import xxx`，把子模块里的函数「提升」到包的顶层。
- **`__all__`**：一个列表，声明 `from 包 import *` 时会导出哪些名字。在 scipy.io 里它还隐含表达了「哪些名字算公共 API」。
- **模块级 `__getattr__`（PEP 562）**：Python 3.7+ 允许在模块里定义 `__getattr__(name)` 函数。当访问模块里「本来不存在的属性」时，Python 会自动调用它，而不是立刻报错。这是实现「懒加载」和「弃用转发」的关键钩子。
- **Meson**：一个用 Python 风格语法描述的构建系统，SciPy 用它编译 C/C++/Cython 扩展，并把 `.py` 文件安装到 site-packages。我们只需读懂 `meson.build` 里的两三个关键字，不必深入 Meson 本身。

如果对 `__getattr__` 不熟，先记住一句话：**它是模块级的一个「兜底函数」——当你访问模块上不存在的名字时，Python 不会马上报错，而是先来问它。**

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用它来讲什么 |
|---|---|---|
| `scipy/io/meson.build` | scipy.io 的构建脚本 | 如何列出要安装的 `.py`、如何递归子目录 |
| `scipy/io/__init__.py` | 包入口，公共 API 汇聚处 | re-export 模式与 `__all__` 的自动生成 |
| `scipy/io/mmio.py` | Matrix Market 的弃用包装 | `__getattr__` 转发的最小样例 |
| `scipy/io/idl.py` / `netcdf.py` / `harwell_boeing.py` | 三个同构的弃用包装 | 包装模式的批量印证 |
| `scipy/_lib/deprecation.py` | 全 SciPy 共用的弃用工具 | `_sub_module_deprecation` 的真正实现 |

补充：`scipy/io/_mmio.py` 是 Matrix Market 的**旧**实现（私有），本讲只引用它的存在，不展开它的读写逻辑（那是 u2-l3 的内容）。另外本讲会顺带提到 `scipy/io/_fast_matrix_market`（C++ 后端）作为「实现迁移」的真实案例。

## 4. 核心概念与源码讲解

### 4.1 scipy/io 目录的三类文件

#### 4.1.1 概念说明

打开 `scipy/io/` 目录，你会看到一堆 `.py` 文件，初看眼花缭乱。但只要掌握一个分类规则，就能立刻理清。每一个被支持的文件格式，在 scipy.io 里通常对应**两类文件**：

1. **私有实现模块**：文件名以 `_` 开头（如 `_mmio.py`、`_idl.py`、`_netcdf.py`），里面是真正干活的代码——解析二进制、读写数组。
2. **弃用包装模块**：文件名**不带** `_`（如 `mmio.py`、`idl.py`、`netcdf.py`），它们非常短，只有十几行，唯一职责是「把还在用旧用法的用户引到新用法，同时发出弃用警告」。

加上一些公共/特殊文件，scipy/io 顶层文件可以归为三大类。

#### 4.1.2 核心流程

把目录里的文件按「职责」分桶，结构就清晰了：

```
scipy/io/
├── __init__.py          # 公共 API 汇聚 + __all__
├── meson.build          # 构建脚本
│
├── _fortran.py          # ← 私有实现（Fortran 无格式文件）
├── _idl.py              # ← 私有实现（IDL .sav）
├── _mmio.py             # ← 私有实现（Matrix Market，旧的纯 Python 版）
├── _netcdf.py           # ← 私有实现（NetCDF3）
├── wavfile.py           # ← 特例：WAV 的实现就在这里（没有 _wavfile.py）
│
├── mmio.py              # ← 弃用包装 → 转发到 _mmio
├── idl.py               # ← 弃用包装 → 转发到 _idl
├── netcdf.py            # ← 弃用包装 → 转发到 _netcdf
├── harwell_boeing.py    # ← 弃用包装 → 转发到 _harwell_boeing（子包）
│
├── arff/                # 子包：ARFF（公共命名空间 + 内部还有个 arffread 包装）
├── matlab/              # 子包：MATLAB（最复杂，整个第三单元讲它）
├── _harwell_boeing/     # 子包：H-B（私有，含 Fortran format 解析器）
├── _fast_matrix_market/ # 子包：C++ 高性能后端（私有，现在真正的 mmread 在这）
└── tests/               # 顶层测试
```

#### 4.1.3 源码精读

判断一个 `.py` 是「实现」还是「包装」，最可靠的办法是看它的**文件大小和首行注释**。例如 `mmio.py` 只有 17 行，开头就写明身份：

[mmio.py:1-3](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/mmio.py#L1-L3) —— 弃用包装用注释直接声明身份：「这个文件不供公开使用，将在 SciPy v2.0.0 移除，请改用 `scipy.io` 命名空间」。

而真正的实现 `_mmio.py` 有 3 万多字符、定义了 `MMFile` 类、`mmread`/`mmwrite`/`mminfo` 等函数（参见 [_mmio.py:86](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L86) 处的 `def mmread`）。两者体量悬殊，一眼可辨。

> **一个值得注意的细节（不要被注释骗了）**：`scipy/io/__init__.py` 第 114–115 行有一句 `from . import arff, harwell_boeing, idl, mmio, netcdf, wavfile`，上面的注释写着「Deprecated namespaces」。但如果你真的打开 `wavfile.py`，会发现它**不是**弃用包装，而是 WAV 的**完整实现**（有真实的 `read`/`write` 函数，约 30 KB，且没有对应的 `_wavfile.py`）。
>
> 也就是说：`mmio` / `idl` / `netcdf` / `harwell_boeing` 这四个顶层文件确实是「壳」，但 `wavfile` 是个例外——它的实现就放在不带前缀的文件里，本身仍是公共子命名空间（`scipy.io.wavfile.read` 是文档里明确支持的用法）。阅读源码时，**以文件内容为准**，而不是完全依赖那条注释。

#### 4.1.4 代码实践

源码阅读型实践，无需运行：

1. 实践目标：学会用「文件大小 + 首行注释」快速判断一个文件是「实现」还是「包装」。
2. 操作步骤：在仓库里对比 `_mmio.py`（实现）与 `mmio.py`（包装）的行数；再打开 `idl.py` 与 `harwell_boeing.py`，确认它们和 `mmio.py` 几乎一字不差。
3. 需要观察的现象：四个包装文件结构完全同构，只有 `__all__` 列表和 `_sub_module_deprecation` 调用里的 `module` / `private_modules` 两个参数不同。
4. 预期结果：你能凭直觉判断——「带 `_` 的是肉，不带 `_` 的同名短文件是壳」。

#### 4.1.5 小练习与答案

**练习 1**：`scipy/io/` 顶层里，`_netcdf.py` 和 `netcdf.py` 哪个是真正的实现？为什么？
**答案**：`_netcdf.py` 是实现（带 `_` 前缀、文件大、定义了 `netcdf_file` 类）；`netcdf.py` 是弃用包装（不带前缀、只有十几行、首行注释声明将被移除）。

**练习 2**：`wavfile.py` 不带 `_` 前缀，但它是包装吗？
**答案**：不是。它是 WAV 的真实实现，没有对应的 `_wavfile.py`，`scipy.io.wavfile` 仍是公共命名空间。它是「不带前缀的实现」这个特例，正好提醒我们：分类要靠看代码，不能只看顶层注释。

---

### 4.2 Meson 构建：install_sources 与 subdir

#### 4.2.1 概念说明

Python 包要在用户机器上「能用」，需要把 `.py` 文件**安装**到 site-packages 的 `scipy/io/` 路径下，还要把 C/C++/Cython 扩展**编译**成平台相关的二进制。SciPy 用 **Meson** 这个构建系统来统一管理这两件事。我们读 `meson.build`，只需认识两个最关键的原语：

- `py3.install_sources([...], subdir: ...)`：告诉 Meson「把列表里这些 `.py` 文件原样复制安装到指定子目录」。这里的 `py3` 是 Meson 的 Python 模块对象。
- `subdir('xxx')`：递归进入子目录 `xxx/`，去执行那里的 `meson.build`。

#### 4.2.2 核心流程

`scipy/io/meson.build` 一共做了三件事，按顺序：

```
1) install_sources([...])         # 列出顶层所有要安装的 .py
2) subdir('tests')                # 进入测试目录
3) subdir('matlab')               # 进入 matlab 子包，执行 matlab/meson.build
   subdir('arff')                 # 进入 arff 子包
   subdir('_harwell_boeing')      # 进入 H-B 子包
   subdir('_fast_matrix_market')  # 进入 C++ 后端子包
```

注意一个要点：被 `install_sources` 列出的文件里，**既包括实现（`_mmio.py` 等），也包括包装（`mmio.py` 等）**。为什么包装也要安装？因为用户可能还在 `import scipy.io.mmio`，必须让这个模块在 site-packages 里真实存在，包装里的 `__getattr__` 才有机会被触发。

#### 4.2.3 源码精读

[scipy/io/meson.build:1-14](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/meson.build#L1-L14) —— `py3.install_sources([...], subdir: 'scipy/io')` 把 10 个顶层 `.py` 安装到 `site-packages/scipy/io/`。注意列表里实现与包装**并列**：`'_mmio.py'`（实现）和 `'mmio.py'`（包装）都在一起。

[scipy/io/meson.build:16-20](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/meson.build#L16-L20) —— 五个 `subdir(...)` 递归进入子包。每个子包有自己的 `meson.build`。对于纯 Python 子包（如 `arff/`），子 `meson.build` 也是 `install_sources`；对于含编译产物的子包（如 `matlab/`、`_fast_matrix_market/`），还会出现 `py3.extension_module(...)` 来编译 `.pyx`/`.cpp`。

例如 matlab 子包就用 `extension_module` 编译三个 Cython 扩展，见 [scipy/io/matlab/meson.build:1-25](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/meson.build#L1-L25) 对 `_streams`、`_mio_utils`、`_mio5_utils` 三个 `.pyx` 的编译定义。这部分细节留到 u3-l7 再讲，本讲你只需要知道「`subdir` 让构建递归下去」即可。

#### 4.2.4 代码实践

源码阅读型实践：

1. 实践目标：验证「安装列表」与「目录实际文件」一一对应。
2. 操作步骤：数一下 `scipy/io/` 顶层有多少个 `.py` 文件，再数 `meson.build` 里 `install_sources` 列了多少个名字，对比是否一致。
3. 需要观察的现象：顶层 `.py` 文件应与 `install_sources` 列表一一对应；子包目录（`arff`、`matlab` 等）则各自由 `subdir` 接管，不出现在顶层列表里。
4. 预期结果：顶层共 10 个 `.py` 全部出现在列表中——`__init__.py`、`_fortran.py`、`_idl.py`、`_mmio.py`、`_netcdf.py`、`harwell_boeing.py`、`idl.py`、`mmio.py`、`netcdf.py`、`wavfile.py`。子包不在此列。

#### 4.2.5 小练习与答案

**练习 1**：如果新增一种格式 `foo`（只有纯 Python 实现），需要修改 `meson.build` 的哪些地方？
**答案**：在 `install_sources` 列表里加上 `'_foo.py'`（实现）；如果还提供弃用包装，再加 `'foo.py'`。若它是独立子包，则新建 `foo/meson.build` 并在顶层加一行 `subdir('foo')`。

**练习 2**：`subdir('tests')` 为什么不会把测试代码装进用户的 site-packages？
**答案**：`subdir` 只是「执行子目录的构建脚本」。装不装取决于子脚本怎么做——`tests/meson.build` 主要用来注册测试，通常不调用针对生产路径的 `install_sources`。

---

### 4.3 公共/私有模块约定与 re-export

#### 4.3.1 概念说明

SciPy 有一条贯穿全库的命名约定：

- **公共 API**：只通过**顶层包**（`scipy.io`）或**带名字的公共子包**（`scipy.io.wavfile`、`scipy.io.arff`）暴露。用户应该写 `from scipy.io import mmread`。
- **私有实现**：放进 `_` 前缀模块。下划线在 Python 社区是「这是内部的，别从外面依赖它」的约定俗成，SciPy 在文档里也据此承诺：下划线模块的内部布局可以随时改。

`scipy/io/__init__.py` 用一行行 `from .xxx import yyy` 把私有实现里的公共函数「提升」到 `scipy.io` 顶层，这个过程叫 **re-export（重新导出）**。这样一来，用户面向的是稳定的 `scipy.io.mmread`，而开发者把它放在哪个文件里实现、要不要换成 C++ 后端，都可以自由调整——Matrix Market 就真的经历过这种迁移（从 `_mmio` 迁到 `_fast_matrix_market`），而用户几乎没有感知。

#### 4.3.2 核心流程

`__init__.py` 的执行流程：

```
1) from .matlab import loadmat, savemat, whosmat       # 从子包提升
2) from ._netcdf import netcdf_file, netcdf_variable   # 从私有模块提升
3) from ._fortran import FortranFile, ...
4) from ._fast_matrix_market import mminfo, mmread, mmwrite  # 当前 mmread 的真正来源
5) from ._idl import readsav
6) from ._harwell_boeing import hb_read, hb_write
7) from . import arff, harwell_boeing, idl, mmio, netcdf, wavfile  # 载入若干子模块
8) __all__ = [s for s in dir() if not s.startswith('_')]  # 自动生成公共清单
```

第 7 行把几个子模块名也挂到包上（这步是为了让弃用包装能被访问到）。第 8 行的妙处在于：它把当前命名空间里**所有不带 `_` 开头的名字**自动收进 `__all__`，既包括上面 re-export 的函数，也包括第 7 行引入的子模块名——但 `test` 对象（在 `__all__` 之后才定义）不会进去，因为生成 `__all__` 时它还没出现。

#### 4.3.3 源码精读

[scipy/io/__init__.py:102-112](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/__init__.py#L102-L112) —— re-export 段落。注意来源各式各样：有的来自子包（`.matlab`），有的来自私有模块（`._netcdf`、`._fortran`、`._fast_matrix_market`、`._idl`、`._harwell_boeing`）。

[scipy/io/__init__.py:114-115](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/__init__.py#L114-L115) —— 把几个子模块（含弃用包装）也 import 进来，使它们能被访问。

[scipy/io/__init__.py:117](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/__init__.py#L117) —— `__all__ = [s for s in dir() if not s.startswith('_')]`，把「不带下划线即公共」的约定代码化。

#### 4.3.4 代码实践

可运行实践（轻量，纯 import）：

1. 实践目标：验证 re-export 把私有模块里的函数暴露到了顶层，且它们就是同一个函数对象。
2. 操作步骤：
   ```python
   import scipy.io as sio
   from scipy.io import _fast_matrix_market as fmm
   # 顶层 mmread 与 _fast_matrix_market 里的 mmread 应是同一个对象
   print('顶层来源:', sio.mmread.__module__)
   print('是同一个对象:', sio.mmread is fmm.mmread)   # 预期 True
   print('在 __all__ 里:', 'mmread' in sio.__all__)     # 预期 True
   ```
3. 需要观察的现象：`sio.mmread.__module__` 指向 `scipy.io._fast_matrix_market`，说明顶层 `mmread` 正是从这个私有模块提升上来的；`is` 判断为 `True`；`__all__` 里包含 `mmread`。
4. 预期结果：三行分别输出模块路径、`True`、`True`。（待本地验证）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `scipy.io.__all__` 用列表推导式自动生成，而不是手写一个列表？
**答案**：自动生成可以避免「新增了函数却忘了加进 `__all__`」的人为遗漏，并且和「不带 `_` 即公共」的约定天然一致——两套规则永远同步。

**练习 2**：`from scipy.io import test` 会成功吗？`test` 在 `__all__` 里吗？
**答案**：`from scipy.io import test` 会成功（`test` 对象确实存在于命名空间，见 `__init__.py` 末尾的 `test = PytestTester(__name__)`），但它**不在** `__all__` 里。因为 `__all__` 在第 117 行生成时，第 120 行的 `test = PytestTester(...)` 还没执行。

---

### 4.4 弃用命名空间包装与 _sub_module_deprecation

这是本讲的核心模块。

#### 4.4.1 概念说明

历史上，很多用户这样用 Matrix Market：

```python
from scipy.io.mmio import mmread   # 旧用法：从子模块 mmio 导入
```

SciPy 2.0 想统一成：

```python
from scipy.io import mmread         # 新用法：从顶层导入
```

但如果直接删掉 `scipy.io.mmio`，全世界依赖旧用法的代码会瞬间崩溃。SciPy 的折中方案是：**保留 `mmio` 模块，但它退化成一个「包装」**——当你访问它的属性时，它会：

1. 发出一个 `DeprecationWarning`，告诉你「请改用顶层导入」；
2. 仍然把真正的 `mmread` 返回给你，让你的代码继续能跑。

这样旧代码不会立刻坏，但每次用都会被「戳一下」提醒迁移。这个机制的核心是 PEP 562 的**模块级 `__getattr__`**，加上一个全 SciPy 共用的工具函数 `_sub_module_deprecation`。

#### 4.4.2 核心流程

当你执行 `scipy.io.mmio.mmread` 时，发生的事情：

```
1) Python 在 mmio 模块自身的字典里找 'mmread'
   → 找不到（mmio.py 里既没有 def mmread，也没有 from ... import mmread，
            只定义了 __getattr__）
2) Python 回退调用 mmio.__getattr__('mmread')
3) __getattr__ 调用 _sub_module_deprecation(
       sub_package='io', module='mmio',
       private_modules=['_mmio'], all=['mminfo','mmread','mmwrite'],
       attribute='mmread')
4) _sub_module_deprecation 内部：
   a) 检查 'mmread' in all → 是，继续
   b) warnings.warn(DeprecationWarning, '请从 scipy.io 命名空间导入…')
   c) 真正 import scipy.io._mmio，返回它的 mmread 属性
5) 你拿到的是 _mmio 里的 mmread 函数，同时控制台多了一条弃用警告
```

如果你访问一个根本不存在的名字，比如 `scipy.io.mmio.read`（注意 Matrix Market 的函数叫 `mmread`，不是 `read`）：

```
1) __getattr__('read') 被调用
2) _sub_module_deprecation 检查 'read' in all → 否
3) 直接 raise AttributeError：
   "scipy.io.mmio 没有 read 属性；而且 scipy.io.mmio 整个命名空间已弃用"
```

这是一个很巧妙的设计：**合法名字走「警告 + 转发」，非法名字走「带弃用提示的报错」**，两条路径都把「请迁移」的信息传达给用户。

#### 4.4.3 源码精读

先看包装本身，它极短。以 `mmio.py` 为例：

[scipy/io/mmio.py:14-17](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/mmio.py#L14-L17) —— `__getattr__(name)` 把所有属性访问都委托给 `_sub_module_deprecation`，传入「子包名 `io`、模块名 `mmio`、私有模块 `_mmio`、合法名字清单、被访问的属性名」。`idl.py`、`netcdf.py`、`harwell_boeing.py` 三个文件与之完全同构，只有 `__all__` 和 `private_modules` 两个参数不同（见 [idl.py:14-17](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/idl.py#L14-L17)、[netcdf.py:14-17](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/netcdf.py#L14-L17)、[harwell_boeing.py:14-17](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/harwell_boeing.py#L14-L17)）。注意 `harwell_boeing` 的 `private_modules` 是 `['_harwell_boeing']`——一个**子包**，说明转发目标可以是模块也可以是包。

再看真正干活的工具函数（它位于 scipy.io 之外的共享库）：

[scipy/_lib/deprecation.py:15-78](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py#L15-L78) —— `_sub_module_deprecation`。关键三段：

- [第 44-49 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py#L44-L49)：`if attribute not in all: raise AttributeError(...)`——拦截非法名字。
- [第 68 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py#L68)：`warnings.warn(message, category=DeprecationWarning, stacklevel=3)`——发出警告。`stacklevel=3` 是为了让警告指向「用户的那行代码」而不是库内部，方便用户定位。
- [第 70-78 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/deprecation.py#L70-L78)：循环遍历 `private_modules`，从私有模块里 `getattr` 出目标属性并返回；只有当**所有**私有模块都没有该属性时，才抛 `AttributeError`。

#### 4.4.4 代码实践（本讲主实践）

可运行实践，完整追踪转发链：

1. 实践目标：亲眼看到「访问 `scipy.io.mmio.mmread` 触发 `DeprecationWarning`，且返回的对象就是私有模块 `_mmio` 里的 `mmread`」；并理解它和顶层 `scipy.io.mmread` 的微妙区别。
2. 操作步骤：
   ```python
   import warnings
   import scipy.io as sio
   import scipy.io._mmio as _mmio

   # (a) 捕获弃用警告
   with warnings.catch_warnings(record=True) as w:
       warnings.simplefilter('always')
       f = sio.mmio.mmread            # 触发 mmio.__getattr__('mmread')
       print('返回的函数:', f)
       print('捕获到警告数:', len(w))
       print('警告类别:', w[0].category.__name__)
       print('警告信息:', str(w[0].message))

   # (b) 确认返回的就是 _mmio 里的 mmread（同一个对象）
   print('f 来自:', f.__module__)
   print('f is _mmio.mmread:', f is _mmio.mmread)   # 预期 True

   # (c) 关键对比：它和顶层 sio.mmread 是【不同】的函数对象！
   print('f is sio.mmread:', f is sio.mmread)        # 预期 False
   print('顶层 sio.mmread 来自:', sio.mmread.__module__)  # scipy.io._fast_matrix_market

   # (d) 访问一个不存在的名字，观察 AttributeError
   try:
       sio.mmio.read                 # 故意写错名字（应为 mmread）
   except AttributeError as e:
       print('AttributeError:', e)
   ```
3. 需要观察的现象：
   - 步骤：捕获到 1 条 `DeprecationWarning`，信息里包含「please import ... from the `scipy.io` namespace」和「removed in SciPy 2.0.0」。
   - 步骤：`f is _mmio.mmread` 为 `True`，证明包装只是转发到旧的私有实现，没有造假。
   - 步骤：`f is sio.mmread` 为 `False`！因为旧的弃用路径仍指向旧实现 `_mmio`，而顶层 `scipy.io.mmread` 已经迁到新的 C++ 后端 `_fast_matrix_market`。这正是「弃用机制保留历史行为」的活教材。
   - 步骤：抛出 `AttributeError`，信息同时说明「`read` 不存在」和「`mmio` 命名空间已弃用」。
4. 预期结果：上述四项全部如预期。（待本地验证）

**手写等价转发伪代码**（示例代码，非项目原码，仅帮助理解）：

```python
# 示例代码：手写一个等价的弃用转发模块
import warnings

_PUBLIC = ["mminfo", "mmread", "mmwrite"]

def __getattr__(name):
    if name not in _PUBLIC:
        raise AttributeError(f"scipy.io.mmio 已弃用，且没有 {name} 属性")
    warnings.warn(
        f"请改用 `from scipy.io import {name}`；"
        f"scipy.io.mmio 命名空间将在 SciPy 2.0.0 移除",
        DeprecationWarning, stacklevel=2,
    )
    from scipy.io import _mmio as _impl   # 懒加载真正的实现
    return getattr(_impl, name)

def __dir__():
    return list(_PUBLIC)
```

**为什么 SciPy 要这样做？（设计动机）**

- **保护向后兼容**：旧代码不会因为升级而立刻崩溃，迁移可以渐进推进。
- **解耦 API 与文件布局**：用户只认 `scipy.io.mmread`，开发者可随时把实现从 `_mmio` 换到 `_fast_matrix_market`（Matrix Market 正是这样迁移的），用户无感。上面的步骤 就见证了这一点。
- **懒加载**：`__getattr__` 只在被访问时才 `import` 实现模块，避免包导入时就加载所有格式的重型代码。
- **统一告警**：所有包装复用同一个 `_sub_module_deprecation`，警告文案、`stacklevel`、错误处理全库一致，维护成本极低。

#### 4.4.5 小练习与答案

**练习 1**：`scipy.io.mmio` 里到底定义了 `mmread` 函数吗？
**答案**：没有。`mmio.py` 里既没有 `def mmread`，也没有 `from ... import mmread`。`mmread` 是在访问时由 `__getattr__` 动态从私有实现里取回来的。

**练习 2**：为什么 `_sub_module_deprecation` 的 `private_modules` 参数是一个列表而不是单个字符串？
**答案**：因为一个公共命名空间的合法名字，可能分散在多个私有模块里（函数文档说「possibly spread over several modules」）。函数会依次在列表里的私有模块中查找，直到找到为止；只有全都没找到才报错。

**练习 3**：执行 `scipy.io.mmio.__all__` 能直接拿到 `["mminfo", "mmread", "mmwrite"]` 吗？为什么访问它**不会**触发 `DeprecationWarning`？
**答案**：能。因为 `__all__` 是在模块字典里**真实定义**的普通变量，Python 直接就能找到它，根本不会走到 `__getattr__`。`__getattr__` 只在「字典里找不到名字」时才被调用——这正是它「懒」的本质。

## 5. 综合实践

把本讲三件事——目录分类、构建、弃用转发——串起来做一个小任务：

**任务：假设要新增一种格式 `xyz`，请按 scipy.io 的现有约定补齐三处改动清单。**

1. 先阅读现有 Matrix Market 的完整落地方式作为模板：
   - 实现：`_mmio.py`（本讲不读细节，知道它在即可）。
   - 提升：`__init__.py` 第 110 行 `from ._fast_matrix_market import mminfo, mmread, mmwrite`。
   - 包装：`mmio.py` 的 `__getattr__` 转发到 `_mmio`。
   - 构建：`meson.build` 列表里的 `'_mmio.py'` 和 `'mmio.py'`。
2. 仿照它，写出新增 `xyz` 格式（提供 `xyz_read` 函数）需要的改动清单（不必真的提交）：
   - 新建 `_xyz.py`，实现 `xyz_read`。
   - 在 `__init__.py` 加 `from ._xyz import xyz_read`。
   - 新建弃用包装 `xyz.py`，照抄 `mmio.py` 模板，把 `module` 改成 `'xyz'`、`private_modules` 改成 `['_xyz']`、`__all__` 改成 `['xyz_read']`。
   - 在 `__init__.py` 的 `from . import ...` 行补上 `xyz`。
   - 在 `meson.build` 的 `install_sources` 列表加上 `'_xyz.py'` 和 `'xyz.py'`。
3. 自检：用一句话解释——为什么包装文件 `xyz.py` 里**不能**直接写 `from ._xyz import xyz_read`（那样就不会触发警告了），而必须用 `__getattr__`？
4. 参考答案：直接 `import` 会让旧用法静默通过、用户永远收不到迁移提醒，失去了「弃用」的意义；`__getattr__` 的价值正是在「保持可用」的同时「每次戳一下」。Matrix Market 当年从 `_mmio` 迁到 `_fast_matrix_market` 时，正是因为顶层走 re-export、旧路径走带警告的包装，才能在不破坏任何用户代码的前提下完成切换。

## 6. 本讲小结

- `scipy/io` 顶层文件分三类：私有实现（`_` 前缀，如 `_mmio.py`）、弃用包装（同名无前缀，如 `mmio.py`）、以及特例 `wavfile.py`（实现就在无前缀文件里，不是包装）。
- `meson.build` 用 `py3.install_sources([...], subdir: ...)` 安装 `.py`，用 `subdir(...)` 递归子包；**实现与包装都要被安装**，否则旧用法会直接找不到模块。
- SciPy 的公共 API 只从顶层包/公共子包暴露，私有实现藏在 `_` 模块，通过 `__init__.py` 的 re-export 提升。
- `__all__ = [s for s in dir() if not s.startswith('_')]` 把「不带下划线即公共」的约定代码化。
- 弃用包装靠 PEP 562 的模块级 `__getattr__` 实现：合法名字「警告 + 转发」，非法名字「带弃用提示的报错」，真正干活的是共享函数 `_sub_module_deprecation`。
- 这样做的目的：向后兼容、API 与文件布局解耦、懒加载、统一告警，为 SciPy 2.0 的命名空间收敛铺路。

## 7. 下一步学习建议

- 本讲建立了「目录—构建—公共/私有约定」的骨架，但还没读过任何一种格式的**真正读写逻辑**。
- 下一讲 **u1-l3（快速上手）** 会用一个 WAV 读写 round-trip 把手感建立起来，让你第一次真正跑通一段 scipy.io 代码。
- 之后第二单元 **u2** 会从最简单的 WAV（RIFF chunk）开始，逐格式深入私有实现模块（`_mmio.py`、`_netcdf.py`、`_idl.py` 等）。
- 对弃用机制想深挖的读者，可跳到第四单元 **u4-l2（弃用命名空间与公共/私有模块约定）**，那里会展开 `_sub_module_deprecation` 的全部参数和更多包装样例（包括 `arff`、`matlab` 子包内部的包装）。
