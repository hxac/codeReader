# 构建方式：Meson 与编译扩展入门

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `scipy/signal/meson.build` 用 `extension_module`（编译）和 `install_sources`（原样复制）这两条路径分别产出了什么。
- 拆解一个 `py3.extension_module(...)` 调用里每个字段的含义（源文件、依赖、include 目录、安装位置）。
- 解释 `pyx_files` 列表 + `foreach` 循环为什么能用几行代码批量编译出 3 个 Cython 扩展。
- 追踪 `_max_len_seq_inner` 的 Pythran/Cython 双路径：`use_pythran` 开关如何从 `meson.options` 一路传导到 `generator`，并最终决定编译哪个输入文件。
- 区分 C/C++ 源、Cython `.pyx` 源、Pythran `.py` 源这三类编译输入的写法差异。

## 2. 前置知识

承接 [u1-l2](u1-l2-directory-structure.md)，你已经知道 signal 目录里的文件分三类：私有实现模块（`_*.py`）、弃用 stub、编译扩展源（`.cc`/`.pyx`/Pythran `.py`），并且 `meson.build` 会"编译出 6 个二进制扩展，并原样安装一批纯 Python 文件"。本讲要回答的核心问题是：**这个"编译"和"安装"具体是怎么发生的？由谁驱动？**

需要先建立的几个概念：

- **Meson**：一个用 Python 描述的构建系统（构建脚本叫 `meson.build`）。SciPy 从 1.9 起用它取代了旧的 `setup.py`/`distutils`。Meson 读取 `meson.build`，再配合 Ninja 生成器高效地增量编译。
- **二进制扩展（extension module）**：一段 C/C++/Cython 编译出的 `.so`（Linux/macOS）或 `.pyd`（Windows），能被 `import` 当作普通 Python 模块使用，但内部跑的是编译后的机器码。signal 里的计算热点（N-D 相关、lfilter 内核、SOS 滤波等）都住在这里。
- **Cython**：一门 Python 超集语言（`.pyx`），给 Python 代码加上静态类型标注后，先翻译成 C，再编译成扩展。优点是写法贴近 Python、可手动标注类型提速。
- **Pythran**：一个把"带类型注解的纯 Python"翻译成高性能 C++ 的编译器（输入是普通 `.py`，靠特殊注释声明类型）。它常作为 Cython 的替代路径。
- **生成器（generator）**：Meson 的一个关键抽象，把"运行某个外部程序（如 `cython` 或 `pythran`）把源文件翻译成 `.c`/`.cpp`"这件事封装成可复用的规则，再喂给 `extension_module` 编译。这是理解本讲 4.3、4.4 的核心。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| `scipy/signal/meson.build` | **本讲主角**。定义 signal 子包全部编译扩展与纯 Python 安装。 |
| `scipy/signal/windows/meson.build` | windows 子包的构建文件，只做 `install_sources`（无编译扩展），作为对照。 |
| `scipy/meson.build` | 上一层构建文件，定义了 `use_pythran`、`np_dep`、`cython_gen`、`pythran_gen`、`version_link_args` 等被本目录复用的"全局原料"。 |
| `meson.options` | 顶层构建选项，声明 `use-pythran` 开关及其默认值。 |
| `scipy/signal/_max_len_seq_inner.py` | Pythran 路径的输入（纯 Python + `#pythran export` 注释）。 |
| `scipy/signal/_max_len_seq_inner.pyx` | Cython 路径的输入（带类型标注的 `.pyx`）。 |

> 提示：下面所有永久链接都指向当前 HEAD `ce1f6477`。文件路径前缀 `scipy/signal/` 与本讲运行目录一致。

## 4. 核心概念与源码讲解

### 4.1 meson.build 顶层：两类产物路径

#### 4.1.1 概念说明

打开 `scipy/signal/meson.build`，通篇只用到两个 `py3.` 方法：

- `py3.extension_module(name, sources, ...)`：把 `sources` **编译**成一个可 import 的二进制扩展模块。
- `py3.install_sources(files, ...)`：把 `files` **原样复制**到安装目录，不做任何编译。

也就是说，整份文件干两件事：编译 6 个扩展（`_sigtools`、`_max_len_seq_inner`、`_peak_finding_utils`、`_sosfilt`、`_upfirdn_apply`、`_spline`），再安装一批纯 Python 文件（包括私有实现模块和弃用 stub）。这与 u1-l2 的结论完全对应——本讲要拆开看它"具体怎么做"。

#### 4.1.2 核心流程

```text
meson.build 顶层
├── py3.extension_module   →  编译产物（.so / .pyd），可 import，共 6 个
│   ├── _sigtools            ← 5 个 .cc 源（C++）
│   ├── _max_len_seq_inner   ← Pythran(.py) 或 Cython(.pyx)，二选一
│   ├── _peak_finding_utils  ← Cython(.pyx)，经 pyx_files 循环
│   ├── _sosfilt             ← Cython(.pyx)，经 pyx_files 循环
│   ├── _upfirdn_apply       ← Cython(.pyx)，经 pyx_files 循环
│   └── _spline              ← 1 个 .cc 源（C++）
├── py3.install_sources     →  原样复制纯 Python 文件（不编译）
├── subdir('windows')       →  递归进入 windows/meson.build
└── subdir('tests')         →  递归进入 tests/meson.build
```

`subdir('windows')` 是 Meson 递归子目录的方式：它会去执行 `windows/meson.build`。注意 windows 子包里**只有 `install_sources`、没有 `extension_module`**——这与 u1-l2 "windows 子包无编译扩展" 的结论一致。

#### 4.1.3 源码精读

文件末尾的两行 `subdir` 把子目录纳入构建，这一句决定了 windows 子包会被递归处理：

[subdir 递归进入 windows 与 tests：meson.build:L101-L102](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/meson.build#L101-L102)

对照 windows 子包的构建文件，确认它确实没有任何编译扩展，只有三个纯 Python 文件被原样安装：

[windows 子包构建文件：windows/meson.build:L1-L8](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/meson.build#L1-L8)

`__init__.py`、`_windows.py`、`windows.py` 三个纯 Python 文件被装进 `scipy/signal/windows/`。没有 `.cc`、没有 `.pyx`，也就没有性能热点需要编译加速——这是 windows 能保持"纯 Python"的根本原因。

#### 4.1.4 代码实践（源码阅读型）

1. 实践目标：在 `signal/meson.build` 里数清 `extension_module` 的调用点与最终模块数的对应关系。
2. 操作步骤：打开 `scipy/signal/meson.build`，把所有 `py3.extension_module(` 的行标出来。
3. 需要观察的现象：字面调用点有 5 处（`_sigtools` 1 处、`_max_len_seq_inner` 的 if/else 2 处、`pyx_files` 循环 1 处、`_spline` 1 处），但最终产出 6 个模块（循环展开 3 个 + 其余各 1 个，且 `_max_len_seq_inner` 的 if/else 只会有一条命中）。
4. 预期结果：6 个可 import 扩展 + 一批纯 Python 文件。
5. 待本地验证：`pip install` 后到 site-packages 的 `scipy/signal/` 目录确认 6 个 `.so`（或 `.pyd`）是否齐全。

#### 4.1.5 小练习与答案

- **练习**：为什么 `windows/meson.build` 不需要 `extension_module`，而 `signal/meson.build` 需要？
- **答案**：windows 子包全部是纯 Python 实现（`_windows.py`），没有需要编译加速的热点循环；signal 子包的 N-D 相关、lfilter、SOS 滤波等是计算密集型热点，必须用 C/C++/Cython 编译才能拿到可用性能，所以需要 `extension_module`。

---

### 4.2 extension_module：编译扩展的定义模板

#### 4.2.1 概念说明

`py3.extension_module(...)` 是 Meson Python 提供的方法，用来声明"把这些源文件编译成一个 Python 扩展模块"。它最常见的几个字段：

- 第一参数：模块名（如 `'_sigtools'`），决定最终 `import scipy.signal._sigtools`。
- `sources`：源文件列表。
- `dependencies`：编译/链接依赖（如 NumPy 的头文件依赖 `np_dep`）。
- `include_directories`：额外的头文件搜索路径。
- `install: true` + `subdir: 'scipy/signal'`：声明"安装到 `scipy/signal/` 目录"。

#### 4.2.2 核心流程

以纯 C/C++ 扩展（`_sigtools`、`_spline`）为例：

```text
源 .cc 文件  →（C++ 编译器）→  目标文件  →（链接器 + Python C-API）→  _sigtools.so
                                                                     （安装到 scipy/signal/）
```

`_sigtools` 把 5 个 `.cc` 源合并进同一个扩展；`_spline` 只用 1 个 `_splinemodule.cc`。

#### 4.2.3 源码精读

`_sigtools` 的定义：把 5 个 C++ 源合并、依赖 NumPy，并把 `../_build_utils/src` 加入头文件路径（那里放着跨子包共享的构建工具头文件）。这段是本目录里"多源 C++ 扩展"的范本：

[_sigtools 的 extension_module 定义：meson.build:L1-L14](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/meson.build#L1-L14)

`_spline` 的定义：结构相同，但只有一个源文件、没有额外 include 目录：

[_spline 的 extension_module 定义：meson.build:L53-L61](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/meson.build#L53-L61)

两个字段需要补充说明（它们都来自上一层 `scipy/meson.build`，本目录直接复用，不重复定义）：

- `np_dep`：NumPy C API 的头文件依赖 + 禁用弃用 API 的宏（`-DNPY_NO_DEPRECATED_API=NPY_1_9_API_VERSION`），定义见 [scipy/meson.build:L81](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/meson.build#L81-L81)。
- `version_link_args`：控制导出符号可见性的链接参数，让扩展只暴露必要的 Python 入口、隐藏内部符号。

#### 4.2.4 代码实践（源码阅读型）

1. 实践目标：对比 `_sigtools` 与 `_spline` 两个定义，找出它们的"相同骨架"与"差异点"。
2. 操作步骤：阅读 `meson.build` 第 1–14 行与第 53–61 行。
3. 需要观察的现象：相同的字段（`dependencies: np_dep`、`link_args: version_link_args`、`install: true`、`subdir: 'scipy/signal'`）与不同的字段（源文件数量、`include_directories`）。
4. 预期结果：你会看到一个稳定的"模板"——所有 C/C++ 扩展都套用这个模板，只改模块名和源文件。
5. 待本地验证：到 `scipy/meson.build` 搜索 `np_dep` 与 `version_link_args` 的赋值，确认它们确实是全局共享。

#### 4.2.5 小练习与答案

- **练习**：`_sigtools` 为什么需要 `include_directories: ['../_build_utils/src']`，而 `_spline` 不需要？
- **答案**：`_sigtools` 的 C++ 源引用了 `scipy/_build_utils/src` 下的共享头文件（公共声明一类），所以要把该目录加进头文件搜索路径；`_spline` 只依赖 NumPy 与标准头，`np_dep` 已覆盖 NumPy 头，因此无需额外 include。

---

### 4.3 pyx_files 循环：Cython 扩展的批量构建

#### 4.3.1 概念说明

`_peak_finding_utils`、`_sosfilt`、`_upfirdn_apply` 三个扩展都是"单个 `.pyx` 源、编译成扩展"的同一种模式。与其把 `extension_module(...)` 写三遍，Meson 用 `foreach` 循环 + 列表来批量生成——这正是本讲指定的第二个最小模块。

#### 4.3.2 核心流程

Cython 扩展的关键，是先用 **generator** 把 `.pyx` 翻译成 `.c`，再交给 `extension_module` 编译：

```text
_sosfilt.pyx  ──cython_gen.process()──▶  _sosfilt.c  ──C 编译器──▶  _sosfilt.so
              （外部命令 cython）           （生成的中间 C）
```

`cython_gen` 是在 `scipy/meson.build` 里定义的一个 `generator`，它的 `output` 模板是 `@BASENAME@.c`（产出 C 而非 C++）。`@BASENAME@` 是 Meson 的占位符，会被替换成源文件去掉扩展名后的名字（如 `_sosfilt`）。

#### 4.3.3 源码精读

列表定义：每个元素是 `[模块名, 源文件]` 的二元组：

[pyx_files 列表：meson.build:L36-L40](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/meson.build#L36-L40)

循环体：用 `cython_gen.process(pyx_file[1])` 把 `.pyx` 翻成 `.c`，再交给 `extension_module`：

[foreach 循环批量编译 Cython 扩展：meson.build:L42-L51](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/meson.build#L42-L51)

`cython_gen` 本身的定义在上一层（注意它的 `output` 是 `@BASENAME@.c`）：

[cython_gen generator 定义：scipy/meson.build:L501-L505](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/meson.build#L501-L505)

注意循环体里的 `c_args: cython_c_args`——它来自 `scipy/meson.build` 顶部，在 MinGW（Windows gcc）下会额外加上格式化字符串相关的告警抑制参数。

#### 4.3.4 代码实践（源码阅读型）

1. 实践目标：理解 `foreach` 如何用一份模板生成三个扩展，体会"列表驱动"的写法。
2. 操作步骤：在 `meson.build` 第 36–51 行，把 `pyx_file[0]`（模块名）和 `pyx_file[1]`（源文件）手写展开三次，预期得到三条 `extension_module` 调用。
3. 需要观察的现象：三条调用的结构完全一致，只有名字和源文件不同。
4. 预期结果：展开后等价于三个独立的 `_peak_finding_utils` / `_sosfilt` / `_upfirdn_apply` 扩展定义。
5. 待本地验证：到构建目录（如 `build/scipy/signal/`）查看是否生成了 `_sosfilt.c` 这类中间文件。

#### 4.3.5 小练习与答案

- **练习**：如果想新增一个 Cython 扩展 `_foo`（源文件 `_foo.pyx`），需要改哪些地方？
- **答案**：只需在 `pyx_files` 列表里加一行 `['_foo', '_foo.pyx']`，循环会自动把它编译并安装，无需新增任何 `extension_module` 调用。

---

### 4.4 _max_len_seq_inner 的 Pythran/Cython 双路径

#### 4.4.1 概念说明

这是本讲最重要的设计，也是本讲代码实践任务的核心。`_max_len_seq_inner` 这个扩展有**两套等价的源**：

- Pythran 输入：`_max_len_seq_inner.py`（看起来就是普通 Python，顶部有 `#pythran export` 类型注解）。
- Cython 输入：`_max_len_seq_inner.pyx`（带 `cdef` 类型声明的 Cython）。

Meson 用一个 `if use_pythran / else` 二选一：开了 Pythran 就编译 `.py`，否则编译 `.pyx`。两者算的是同一个东西——`max_len_seq` 的内层移位寄存器循环，结果一致，只是编译路径不同。上层 Python 代码 `_max_len_seq.py` 只管 `from ._max_len_seq_inner import _max_len_seq_inner`，对走哪条路径无感知。

#### 4.4.2 核心流程

```text
use_pythran == True ?
├── 是 → pythran_gen.process('_max_len_seq_inner.py')  →  .cpp  →  _max_len_seq_inner.so
│        （依赖 [pythran_dep, np_dep]，C++ 编译，带 cpp_args_pythran）
└── 否 → cython_gen.process('_max_len_seq_inner.pyx')   →  .c    →  _max_len_seq_inner.so
         （依赖 np_dep，C 编译，带 cython_c_args）
```

两条路径产出的模块对外接口完全相同（同名 `_max_len_seq_inner`、同名函数），所以 `_max_len_seq.py` 的 import 无论哪条路径都能成功。

#### 4.4.3 源码精读

`if/else` 二选一的构建分支（这是本讲最关键的一段）：

[_max_len_seq_inner 的 Pythran/Cython 双路径：meson.build:L16-L34](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/meson.build#L16-L34)

Pythran 输入文件——顶部两行 `#pythran export` 声明了两种整型特化（`int32`/`int64` 的 taps），其余是朴素 Python：

[Pythran 注解与函数签名：_max_len_seq_inner.py:L6-L10](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_max_len_seq_inner.py#L6-L10)

Cython 输入文件——三个 `@cython` 装饰器关掉除零检查、边界检查、负索引，函数参数用 typed memoryview（`Py_ssize_t[::1]`、`np.int8_t[::1]`）标注：

[Cython 装饰器与类型标注：_max_len_seq_inner.pyx:L11-L17](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_max_len_seq_inner.pyx#L11-L17)

上层调用方对路径无感——只 import 同一个名字：

[导入编译内核：_max_len_seq.py:L8](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_max_len_seq.py#L8-L8)

把两份源放在一起对比，能清楚看到"同一算法的两种写法"：

| 维度 | Pythran (`.py`) | Cython (`.pyx`) |
|---|---|---|
| 类型声明位置 | 顶部 `#pythran export` 注释 | 函数参数直接标注（`cdef` / typed memoryview） |
| 性能开关 | 由 Pythran 自动向量化 | `@cython.cdivision/boundscheck/wraparound` 装饰器 |
| 编译产物 | `.cpp`（C++） | `.c`（C） |
| 额外依赖 | `pythran_dep` + `cpp_args_pythran` | 仅 `np_dep` + `cython_c_args` |

#### 4.4.4 代码实践（本讲核心任务）

1. 实践目标：在 `meson.build` 中找出 `_max_len_seq_inner` 的两种构建路径，说明 Pythran 与 Cython 各自的输入文件与依赖差异。
2. 操作步骤：
   - 打开 `scipy/signal/meson.build` 第 16–34 行，定位 `if use_pythran` 分支（Pythran 路径）和 `else` 分支（Cython 路径）。
   - 记录每条路径的：输入文件、`generator`、产出语言（`.cpp`/`.c`）、`dependencies`、编译参数。
   - 打开 `_max_len_seq_inner.py` 与 `_max_len_seq_inner.pyx`，对比类型声明方式。
3. 需要观察的现象：两条路径 `extension_module` 的模块名都叫 `_max_len_seq_inner`，但 `process()` 的输入与依赖不同。
4. 预期结果（填表）：
   - Pythran 路径：输入 `_max_len_seq_inner.py`，经 `pythran_gen`，产出 `.cpp`，依赖 `[pythran_dep, np_dep]`，参数 `cpp_args_pythran`（及一条未使用局部 typedef 的告警抑制）。
   - Cython 路径：输入 `_max_len_seq_inner.pyx`，经 `cython_gen`，产出 `.c`，依赖 `np_dep`，参数 `cython_c_args`。
5. 待本地验证：分别用 `meson setup -Duse-pythran=true ...` 与 `-Duse-pythran=false ...` 两次配置构建，确认两次都能成功 `import scipy.signal._max_len_seq_inner`，且 `max_len_seq` 输出一致。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 `_max_len_seq_inner` 要维护两份等价源，而不是只用其中一种？
- **答案**：为了在"是否安装了 Pythran"两种环境下都能编译。Pythran 默认开启（性能通常更优、可自动向量化），但在受限环境（无法装 Pythran 工具链）里可以关掉 `use-pythran` 回退到 Cython，保证可移植性。
- **练习 2**：上层 `_max_len_seq.py` 如何做到对底层走哪条路径无感知？
- **答案**：它只写 `from ._max_len_seq_inner import _max_len_seq_inner`——两条路径产出的扩展模块同名、同函数签名，import 语句无需任何条件分支。

---

### 4.5 use_pythran 开关：从 meson.options 到 generator 的全链路

#### 4.5.1 概念说明

`use_pythran` 不是凭空出现的变量，而是一个可由用户配置的构建选项。它的生命周期是：选项声明（`meson.options`）→ 读取（`scipy/meson.build`）→ 生成 generator（`scipy/meson.build`）→ 分支使用（`signal/meson.build`）。理解这条链路，就理解了"为什么 4.4 的 `if use_pythran` 能工作"。

#### 4.5.2 核心流程

```text
meson.options: option('use-pythran', value: true)        ← 用户可改
        │
        ▼  get_option
scipy/meson.build: use_pythran = get_option('use-pythran')
        ├── find_program('pythran') 仅在 use_pythran 为真时执行
        └── if use_pythran: pythran_gen = generator(pythran, ...)   ← 否则 pythran_gen 不存在
        │
        ▼  被子目录复用
signal/meson.build: if use_pythran: pythran_gen.process(.py) else: cython_gen.process(.pyx)
```

关键点：`pythran_gen` 这个 generator 只在 `use_pythran` 为真时才被定义。所以 `signal/meson.build` 必须用 `if use_pythran` 守卫——否则在关闭 Pythran 时引用了不存在的 `pythran_gen` 会直接报错。

#### 4.5.3 源码精读

选项声明：默认值为 `true`，并写明关掉后会回退到纯 Python 或 Cython：

[use-pythran 选项声明：meson.options:L23-L26](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/meson.options#L23-L26)

读取选项并按需查找 `pythran` 程序（`find_program` 只在 `use_pythran` 为真时调用，所以关掉时机器上不需要装 Pythran）：

[读取 use_pythran 并查找 pythran 程序：scipy/meson.build:L148-L151](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/meson.build#L148-L151)

仅在开启时定义 `pythran_gen`（产出 `.cpp`，命令是 `pythran -E 输入 -o 输出`）：

[pythran_gen generator 定义：scipy/meson.build:L513-L518](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/meson.build#L513-L518)

#### 4.5.4 代码实践（源码阅读型）

1. 实践目标：完整追踪 `use_pythran` 从声明到生效的四个文件位置。
2. 操作步骤：依次打开 `meson.options` 第 23–26 行、`scipy/meson.build` 第 148–151 行（读取）、`scipy/meson.build` 第 513–518 行（生成器）、`scipy/signal/meson.build` 第 16–34 行（消费），连成一条链。
3. 需要观察的现象：选项值如何从"声明"流向"条件查找程序"→"条件定义 generator"→"条件选择源文件"。
4. 预期结果：你能画出本节 4.5.2 的那张流程图，并解释为什么关掉 Pythran 后构建仍然能成功（回退到 Cython）。
5. 待本地验证：`meson configure build -Duse-pythran=false` 后重新构建，观察 `_max_len_seq_inner` 是否改为由 `.pyx` 编译。

#### 4.5.5 小练习与答案

- **练习**：如果把 `use-pythran` 默认值改成 `false`，signal 子包的构建产物会有什么变化？是否仍能正常 `import scipy.signal`？
- **答案**：`_max_len_seq_inner` 会改由 `_max_len_seq_inner.pyx`（Cython）编译而来，其余 5 个扩展不受影响；因为上层 import 语句不变，`scipy.signal` 仍能正常导入，`max_len_seq` 行为一致，只是该内核的实现路径从 Pythran 换成了 Cython。

---

## 5. 综合实践

把本讲的"两类产物、extension_module 模板、pyx_files 循环、双路径、use_pythran 链路"串起来，完成下面这份 **signal 子包构建全景图**：

1. 打开 `scipy/signal/meson.build`，画一张表，列出全部 6 个编译扩展，每行包含：模块名、源文件、源语言、走的路径（直接 C/C++ / Cython 循环 / Pythran-Cython 二选一）。
2. 对 `_max_len_seq_inner` 单独画一张"双路径决策图"：标出 `use_pythran` 的取值如何决定输入文件与 generator。
3. 对照 `scipy/meson.build`，标出 `np_dep`、`cython_gen`、`pythran_gen`、`version_link_args` 各自的定义行号，说明它们为什么必须定义在父级而非本目录。
4. 最后回答一个开放问题：如果某个新算法既想用 Pythran 加速、又想保证在无 Pythran 环境可构建，参照 `_max_len_seq_inner` 你需要准备哪两份源、并在 `meson.build` 里怎么写？

预期产出：一张 6 行的扩展表 + 一张双路径图 + 一段"两份源 + if/else"的 meson 片段说明。本实践为纯源码阅读型，无需运行构建即可完成；若条件允许，可用 `meson setup -Duse-pythran=true/false` 做两次实验验证双路径切换（待本地验证）。

## 6. 本讲小结

- `signal/meson.build` 用 `extension_module`（编译）与 `install_sources`（原样复制）两条路径分别产出 6 个二进制扩展和一批纯 Python 文件。
- 所有 C/C++ 扩展（`_sigtools`、`_spline`）套用同一个 `extension_module` 模板，只改模块名、源文件与 include 目录。
- `_peak_finding_utils`/`_sosfilt`/`_upfirdn_apply` 通过 `pyx_files` 列表 + `foreach` 循环批量构建，新增 Cython 扩展只需加一行。
- `_max_len_seq_inner` 是唯一的双路径扩展：`use_pythran` 为真走 Pythran（`.py`→`.cpp`），否则走 Cython（`.pyx`→`.c`），两条路径产出同名同接口的模块。
- `use_pythran` 的链路是 `meson.options` 声明 → `scipy/meson.build` 读取并按需定义 `pythran_gen` → `signal/meson.build` 用 `if` 选择源文件。
- generator（`cython_gen`/`pythran_gen`）是"把源翻译成中间 C/C++"的可复用规则，是理解 Cython/Pythran 扩展如何被编译的关键抽象。

## 7. 下一步学习建议

本讲讲清了"怎么编译"，但还没讲"编译出来的扩展内部到底算什么、又如何被暴露给用户"。建议：

- 进入 **[u1-l4 公共命名空间与 API 导出链路](u1-l4-namespace-export-chain.md)**，看 `_max_len_seq_inner` 这类编译内核如何被 `_max_len_seq.py` 包装后，经 `_signal_api` 聚合、`_support_alternative_backends` 装饰，最终暴露为 `scipy.signal.max_len_seq`。
- 若对具体算子感兴趣，可提前跳读：`_sigtools` 的 N-D 相关内核（对应 u3-l4）、`_sosfilt.pyx` 的 SOS 滤波（对应 u4-l3）、`_peak_finding_utils.pyx` 的峰值检测（对应 u8-l2），体会"编译扩展 = 性能热点"的对应关系。
- 想系统理解 SciPy 整体构建（Fortran/BLAS/pybind11 等），可读仓库根目录的 `meson.build`、`meson.options` 与 `scipy/meson.build`，本讲的 generator/option 机制在那里被反复使用。
