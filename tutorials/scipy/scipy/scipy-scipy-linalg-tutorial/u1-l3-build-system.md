# 构建系统：Meson、f2py、Cython 与 C++ 扩展

## 1. 本讲目标

前两讲（[u1-l1](u1-l1-project-overview.md)、[u1-l2](u1-l2-directory-and-exports.md)）我们建立了 scipy.linalg 的「源码地图」：一堆 `_*` 开头的 Python 文件，靠 `__init__.py` 里的星号导入汇聚成一个命名空间。但你只要稍微翻一翻 [meson.build](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build)，就会发现一个明显的事实：scipy.linalg 的真实算力并不在那些 `.py` 文件里，而是在一批编译出来的二进制扩展模块里（`_flapack`、`_fblas`、`_batched_linalg`、`_decomp_update`、`cython_blas`……）。

本讲的目标是回答一个问题：**这些 `.so` / `.pyd` 扩展模块，究竟是从哪些源码、用什么工具、按什么规则「长」出来的？** 学完后你应当能够：

1. 理解 SciPy 基于 **Meson** 的构建流程，看懂一份 `meson.build` 文件。
2. 区分 **f2py**、**Cython**、**C/C++** 三条扩展生成路线，知道它们各自的产物与用途。
3. 对任意一个 scipy.linalg 编译扩展，能反查出它的「源码来源」与「生成工具」。
4. 知道从源码构建 scipy.linalg 大致需要哪些前置条件。

## 2. 前置知识

在进入源码前，先用大白话建立几个概念。

### 2.1 为什么要「编译扩展」

Python 很方便但慢。线性代数的核心运算（解方程、求特征值、矩阵分解）几十年前就被 Fortran 写的 **LAPACK / BLAS** 库做到极致了。scipy.linalg 的策略不是用 Python 重写这些算法，而是写一层「**胶水**」去调用已经存在的 LAPACK/BLAS。这层胶水必须编成机器码（C 扩展），才能既快又安全地和 Fortran 库互调。所以「构建」这一步，本质就是在造胶水。

### 2.2 Meson 是什么

[Meson](https://mesonbuild.com/) 是一个构建系统（和 CMake、Make 同类），用一种易读的 DSL（领域特定语言）描述「源码 → 产物」的规则。SciPy 从 1.9 起用 Meson 取代了老的 `distutils`/`numpy.distutils`。Meson 文件通常叫 `meson.build`。本讲的全部内容都围绕 [scipy/linalg/meson.build](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build) 这一个文件展开。

Meson 里你会反复看到三类「动词」：

| Meson 构件 | 作用 | 在本讲的典型用法 |
| --- | --- | --- |
| `custom_target` | 跑一条命令，生成一个（或几个）文件 | f2py 把 `.pyf.src` 生成 `.c`；Tempita 把 `.pyx.in` 生成 `.pyx` |
| `generator` | 可复用版的 `custom_target`，对很多输入套同一套规则 | Cython 把任意 `.pyx` 转成 `.c` |
| `py3.extension_module` | 把若干源文件编成一个可 import 的 Python 扩展（`.so`/`.pyd`） | 最终产出 `_flapack`、`_batched_linalg` 等 |

一条简单的记忆法：**`custom_target`/`generator` 负责「生成代码」，`extension_module` 负责「编译 + 链接成模块」**。很多扩展需要两步：先 `custom_target` 生成中间 `.c`，再 `extension_module` 编译它。

### 2.3 f2py、Cython、手写 C/C++ 三条路

scipy.linalg 的扩展主要有三种「出身」：

- **f2py 路线**：NumPy 自带的 Fortran-to-Python 工具。这里它**不**直接读 Fortran 源码，而是读一种叫 `.pyf.src` 的「签名文件」，据此生成调用 LAPACK/BLAS 的 C 扩展。产物：`_fblas`、`_flapack`。
- **Cython 路线**：`.pyx` 是一种「类 Python 但可声明 C 类型」的语言，由 Cython 编译器转成 C，再编成扩展。产物：`_decomp_update`、`_solve_toeplitz`、`cython_blas` 等。
- **手写 C/C++ 路线**：直接用人写的 `.c`/`.cc` 源码编译。产物：C++ 批处理后端 `_batched_linalg`、C 后端 `_internal_matfuncs`。

> 提示：本讲只讲「这些模块从哪来」，不讲每个模块内部的算法。算法分别属于第 3–8 单元。本讲是后续所有底层讲义（u7「BLAS/LAPACK 接口」、u8「批处理后端」）的前置。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`meson.build`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build) | 本讲的主角，定义 scipy.linalg 所有扩展的构建规则 |
| [`fblas.pyf.src`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/fblas.pyf.src) | `_fblas` 的 f2py 签名骨架（BLAS） |
| [`flapack.pyf.src`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/flapack.pyf.src) | `_flapack` 的 f2py 签名骨架（LAPACK），并声明了 `<prefix>` 模板简写 |
| [`flapack_gen.pyf.src`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/flapack_gen.pyf.src) | LAPACK「一般矩阵」例程的签名，用来展示 `<prefix>` 展开机制 |
| [`src/_batched_linalg_module.cc`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_batched_linalg_module.cc) | C++ 批量后端的入口文件，注册模块方法表 |
| [scipy/meson.build](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/meson.build) | 上一层 Meson 文件，定义 `fortranobject_dep`、`use_ilp64`、`f2py_gen` 等本文件依赖的全局变量 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先建立 Meson 的构建心智模型（4.1），再分别走 f2py 路线（4.2）与 Cython/C++ 路线（4.3）。

### 4.1 Meson 构建模型：custom_target / generator / extension_module 三件套

#### 4.1.1 概念说明

理解 scipy.linalg 的构建，关键是抓住 Meson 的「三件套」：`custom_target`、`generator`、`py3.extension_module`。

- **`custom_target(name, ...)`**：给一段「代码生成命令」起个名字。它声明「用这个命令、读这些输入、产出这些文件」。在本讲里，f2py、Tempita、`_generate_pyx.py` 三种代码生成器都是通过 `custom_target` 调用的。它有一个很重要的参数 `depend_files`：列出命令隐式依赖的额外文件，这样当那些文件改动时 Meson 知道要重新生成。
- **`generator(prog, ...)`**：和 `custom_target` 类似，但它是「**可复用模板**」，之后可以用 `gen.process(file1)`、`gen.process(file2)` 对一堆输入套用同一规则。本讲里 Cython 的「`.pyx` → `.c`」步骤就是用一个 `generator` 定义一次，再反复 `.process()` 多个 `.pyx`。
- **`py3.extension_module(name, sources, ...)`**：把源文件列表编译、链接成一个 Python 扩展模块，装到 `scipy/linalg/` 下。它的 `sources` 既可以是真实源码（`.cc`、`.c`），也可以是上面 `custom_target` / `generator` 产出的中间文件。

一句话串联：**`custom_target`/`generator` 生产 `.c`/`.pyx` 中间代码 → `extension_module` 把中间代码（连同手写源码）编成可 import 的 `.so`。**

#### 4.1.2 核心流程

以一个典型扩展（比如 Cython 的 `_solve_toeplitz`）为例，构建数据流如下：

```
_solve_toeplitz.pyx  ──[linalg_init_cython_gen.process()]──►  _solve_toeplitz.c
                                                                   │
                                                                   ▼
                          py3.extension_module('_solve_toeplitz', sources=[上面的.c], ...)
                                                                   │
                                                                   ▼
                                          安装到 scipy/linalg/_solve_toeplitz.*.so
```

而一个需要「先生成再编译」的扩展（如 f2py 的 `_flapack`）则是两段：

```
flapack.pyf.src ──[custom_target 'flapack_module', 调 generate_f2pymod]──► _flapackmodule.c
                                                                              │
                                                                              ▼
                                   py3.extension_module('_flapack', sources=[_flapackmodule.c], ...)
                                                                              │
                                                                              ▼
                                                       安装到 scipy/linalg/_flapack.*.so
```

#### 4.1.3 源码精读

先看本文件最顶部对 Cython `generator` 的定义。这段定义了三个大同小异的 generator，差别只在 `depends`（依赖哪些 `.pxd` 头）：

> [meson.build:42-57](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L42-L57) 定义 `linalg_init_cython_gen` 等 generator：参数 `cython_args` 是传给 Cython 的选项，`output: '@BASENAME@.c'` 表示「输入 `foo.pyx` 就产出 `foo.c`」（`@BASENAME@` 是 Meson 的占位符，取文件名去掉扩展名）。

接着看一个最干净的「单步 Cython 扩展」`_solve_toeplitz`，它是三件套里 `generator + extension_module` 两件套的组合：

> [meson.build:199-206](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L199-L206) 把 `_solve_toeplitz.pyx` 喂给 `linalg_init_cython_gen.process(...)` 得到 `.c`，再交给 `py3.extension_module('_solve_toeplitz', ...)` 编译安装。`subdir: 'scipy/linalg'` 决定最终 `.so` 装到哪个目录。

再看 `depend_files` 的用法——这在 f2py 段落里特别关键，因为一个 `.pyf.src` 会 `include` 多个其它 `.pyf.src`：

> [meson.build:87-102](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L87-L102) 定义 `flapack_module` 这个 `custom_target`：输入是 `flapack.pyf.src`，命令是 `generate_f2pymod`，`depend_files` 列出 8 个被 include 的子签名文件。意思是：只要这 8 个文件任意一个改了，就要重新生成 `_flapackmodule.c`。

#### 4.1.4 代码实践

**实践目标**：在 `meson.build` 中识别「生成代码」与「编译模块」这两类步骤。

**操作步骤**：
1. 打开 [meson.build](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build)。
2. 搜索关键字 `custom_target(`，统计它出现了几次，分别生成哪些中间文件。
3. 搜索 `generator(cython`，确认 Cython 的 `.pyx→.c` 规则定义在哪几行。
4. 搜索 `py3.extension_module(`，列出全部最终扩展模块的名字。

**需要观察的现象**：你会发现 `py3.extension_module` 的数量明显多于 `custom_target`——因为有些扩展（如 `_batched_linalg`）直接用现成源码编译，不需要「生成」步骤。

**预期结果**：`custom_target` 大致有 5 个（`cython_linalg`、`fblas_module`、`flapack_module`、`fblas64_module`、`flapack64_module`、`_decomp_update`），而 `extension_module` 有十几个。**（具体计数待本地核对，因为版本间会增删。）**

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_solve_toeplitz` 只需要 `generator` + `extension_module` 两步，而 `_flapack` 却要先 `custom_target` 再 `extension_module`？

**参考答案**：`_solve_toeplitz.pyx` 已经是完整的 Cython 源码，只需用 `generator` 把它转成 `.c` 即可；而 `_flapack` 的源不是 `.pyx` 而是 f2py 的 `.pyf.src` 签名文件，需要先用专门的命令 `generate_f2pymod` 把签名「翻译」成 `_flapackmodule.c`，这一步是 f2py 专属的、不能套用通用 Cython `generator`，所以单独用一个 `custom_target` 描述。

**练习 2**：`generator` 里的 `output: '@BASENAME@.c'` 是什么意思？

**参考答案**：`@BASENAME@` 是 Meson 的占位符，等于「输入文件名去掉扩展名」。输入 `_solve_toeplitz.pyx` 时 `@BASENAME@` = `_solve_toeplitz`，所以输出文件是 `_solve_toeplitz.c`。这让同一条规则能批量处理任意 `.pyx`。

---

### 4.2 f2py 路线：从 .pyf.src 签名生成 _flapack（及 LP64/ILP64 双份）

#### 4.2.1 概念说明

[LAPACK](https://www.netlib.org/lapack/) 和 [BLAS](https://www.netlib.org/blas/) 是 Fortran 写的数值库。要在 Python 里调它们，最自然的方式是用 NumPy 自带的 **f2py**。但 scipy.linalg 用 f2py 的方式有一点特别：它**不让 f2py 去解析 Fortran 源码**，而是手写了一套「**签名文件**」`.pyf.src`，人工描述「这个 LAPACK 例程叫什么、参数是什么、哪些是输入哪些是输出」。f2py 读这些签名，生成 `_flapackmodule.c` 这样的 C 扩展；扩展里再链接到系统已安装的 LAPACK/BLAS 库，完成实际计算。

这样做的好处是：签名文件比 Fortran 源码可控得多，能精确控制哪些参数对 Python 用户暴露、默认值是什么、内存怎么拷贝。代价是签名文件本身是一种要专门学的格式。

`.pyf.src` 相比纯 `.pyf` 多了一个 `.src` 后缀，因为它里面用了 f2py 的**模板简写**（下面 4.2.3 详述），需要先预处理再交给 f2py。

#### 4.2.2 核心流程

```
flapack.pyf.src （主骨架，include 8 个分类签名）
        │
        │  generate_f2pymod 脚本（封装 f2py），先做 <prefix> 模板展开
        ▼
_flapackmodule.c + _flapack-f2pywrappers.f   （custom_target 产物）
        │
        │  py3.extension_module，链接 lapack_lp64_dep + fortranobject_dep
        ▼
_flapack.*.so   （可 import，被 lapack.py 里的 get_lapack_funcs 取出）
```

注意一个重要细节：scipy.linalg 会构建**两份** f2py 扩展——`_fblas`/`_flapack`（**LP64**，用 32 位整数）和 `_fblas_64`/`_flapack_64`（**ILP64**，用 64 位整数）。这两份由 `meson.build` 里的两个 `if` 分支分别控制：

- `if needs_lp64_fblas`（LP64 分支）
- `if use_ilp64`（ILP64 分支）

`use_ilp64` 是一个 Meson 构建选项，来自上一层 [scipy/meson.build](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/meson.build#L217-L226) 的 `get_option('use-ilp64')`。整数位宽的意义会在 u9-l2「ILP64」讲义里展开，这里只要知道「存在两套并行构建」即可。

#### 4.2.3 源码精读

先看 LP64 的 BLAS 骨架 `fblas.pyf.src`。整个文件非常短，因为它只是个「外壳」，真正内容靠 `include` 拉进来：

> [fblas.pyf.src:10-22](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/fblas.pyf.src#L10-L22) 声明了一个叫 `_fblas` 的 python module，`usercode` 里 `#define F_INT int`（把 LAPACK/BLAS 用的「Fortran 整数」定义为普通 32 位 `int`，这正是 LP64 的标志），然后 `interface` 块里 `include` 了 BLAS 的 Level 1/2/3 三个签名文件。

> 对比记忆：ILP64 版的 `fblas_64.pyf.src`（在仓库里同名带 `_64`）会把 `F_INT` 定义成 64 位整数——这就是两套扩展最根本的差别。

再看 LAPACK 的骨架 `flapack.pyf.src`，它额外做了一件重要的事：在文件开头用注释定义了一组「模板简写」：

> [flapack.pyf.src:10-25](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/flapack.pyf.src#L10-L25) 列出 `<prefix=s,d,c,z>` 等简写约定。含义是：LAPACK 的每个例程按数据类型有四个变体——`s`(单精度实数)、`d`(双精度实数)、`c`(单精度复数)、`z`(双精度复数)，分别对应前缀。比如 LU 分解例程 `getrf` 实际有 `sgetrf/dgetrf/cgetrf/zgetrf` 四个真实 Fortran 例程。写签名时只需写一处 `<prefix>getrf`，模板会自动展开成四个。

这个 `<prefix>` 机制是「一份签名管四种类型」的关键。来看一个真实例程 `gebal`（矩阵平衡）的签名片段：

> [flapack_gen.pyf.src:4-31](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/flapack_gen.pyf.src#L4-L31) 用 `subroutine <prefix>gebal(...)` 声明例程，`<ftype>`、`<ctype>` 等占位符会随 `<prefix>` 同步替换为对应的 Fortran/C 类型；`intent(in,out,copy,out=ba)` 这类 f2py 语义标注决定了参数对 Python 是输入还是输出、是否拷贝。展开后这一段就变成 `sgebal/dgebal/cgebal/zgebal` 四份。

最后回到 `meson.build`，看这份签名如何变成可 import 的模块（LP64 分支）：

> [meson.build:87-114](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L87-L114) 先用 `custom_target('flapack_module', ...)` 把 `flapack.pyf.src` 经 `generate_f2pymod` 生成 `_flapackmodule.c`，再用 `py3.extension_module('_flapack', ...)` 编译它，`dependencies: [lapack_lp64_dep, fortranobject_dep]` 表示链接系统 LAPACK 库和 f2py 运行时辅助库 `fortranobject`。

注意第 74–76 行有一条很有信息量的注释：

> [meson.build:74-76](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L74-L76)（注释）说明 `_fblas` 模块**故意链接 LAPACK 而非只链 BLAS**：因为个别例程（如 `spmv`）的实/复数版本分别落在 BLAS 和 LAPACK 里，历史上把这类复数例程也放进 `_fblas`，所以必须同时链上 LAPACK。

ILP64 分支结构几乎一样，只是输入换成 `fblas_64.pyf.src` / `flapack_64.pyf.src`、链接换成 `blas_lapack_wrapper_lib_ilp64`：

> [meson.build:117-169](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L117-L169) 在 `if use_ilp64` 分支里构建 `_fblas_64` 与 `_flapack_64`，命令额外带上 `f2py_ilp64_opts`（包含 `--f2cmap int64_f2cmap`，用于把整数映射成 64 位）。

#### 4.2.4 代码实践

**实践目标**：体会「一份签名 → 四种类型」的展开。

**操作步骤**：
1. 打开 [flapack_gen.pyf.src](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/flapack_gen.pyf.src)，找到 `subroutine <prefix>gesv(...)`（LU 求解）这一段。
2. 读它的注释 `lu,piv,x,info = gesv(a,b,...)`，这是 Python 侧将看到的调用形式。
3. 心算把它展开：`<prefix>` 取 `s/d/c/z` 时，对应的真实 LAPACK 例程名分别是什么。

**需要观察的现象**：注意签名里 `callstatement { ... --piv[i++]); }` 这类 C 代码块——LAPACK 用 1 基 Fortran 下标，Python 用 0 基，这段胶水负责把返回的 `piv` 减 1 转成 Python 习惯。

**预期结果**：`<prefix>gesv` 展开为 `sgesv / dgesv / cgesv / zgesv` 四个 LAPACK 例程，但 Python 用户只会看到一个 `gesv`，类型分发在运行时由 [lapack.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/lapack.py) 里的 `get_lapack_funcs` 完成（详见 u7-l1）。

#### 4.2.5 小练习与答案

**练习 1**：`.pyf.src` 里 `usercode ''' #define F_INT int '''` 这一行的作用是什么？为什么 LP64 版和 ILP64 版在这里会不同？

**参考答案**：`F_INT` 是签名里所有「LAPACK/BLAS 整数参数」（如矩阵维数 `n`、`info`）的 C 类型别名。LP64 版定义为 `int`（32 位），ILP64 版定义为 64 位整数。LAPACK 库本身也分 32/64 位整数 ABI，扩展里的 `F_INT` 必须和所链接的库一致，否则会内存越界——这就是为什么要构建两套扩展。

**练习 2**：为什么 `_flapack` 的 `custom_target` 要在 `depend_files` 里列出 8 个 `flapack_*.pyf.src`？

**参考答案**：因为 `flapack.pyf.src` 本身只是骨架，真正的内容是靠 `include 'flapack_gen.pyf.src'` 等语句拉进来的。Meson 不会自动跟踪 `#include`/`include` 这类文本包含关系，必须显式在 `depend_files` 里声明，这样当某个被包含的签名文件被修改时，Meson 才知道要重新生成 `_flapackmodule.c`。

---

### 4.3 Cython 与 C/C++ 路线：_decomp_update（Cython+Tempita）与 _batched_linalg（C++）

#### 4.3.1 概念说明

除 f2py 之外，scipy.linalg 还有两类扩展来源。

**(a) Cython 路线**：[Cython](https://cython.org/) 是一种「Python 超集」语言。`.pyx` 文件里可以写 Python 语法，也可以声明 C 类型、直接调 C 函数，由 Cython 编译器翻译成 C，再编成扩展。它比 f2py 灵活——f2py 只能调外部 Fortran 库，Cython 可以写任意数值内核。scipy.linalg 用 Cython 写了多个性能敏感的内核：Toeplitz 求解（`_solve_toeplitz`）、QR 增量更新（`_decomp_update`）、矩阵平方根分块（`_matfuncs_sqrtm_triu`）、对称/Hermitian 判定（`_cythonized_array_utils`），以及面向 Cython 用户的 `cython_blas`/`cython_lapack`。

其中 `_decomp_update` 多了一层花样：它的源是 `_decomp_update.pyx.in`，是一个 **Tempita 模板**。Tempita 是一个轻量模板引擎，先用它把 `.pyx.in` 渲染成真正的 `.pyx`（为不同数值类型生成多份相似代码），再用 Cython 编译。这是「为多种数据类型避免手写重复代码」的常见手段。

**(b) 手写 C/C++ 路线**：性能最强但维护成本最高的方式。scipy.linalg 有两个手写后端：

- **C++ 批量后端 `_batched_linalg`**：现代新增的「一摞矩阵批量求逆/求解/分解」后端（见 u8 单元），源码在 [`src/`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/) 下，一个 `.cc` 入口 + 一组 `.hh` 头。
- **C 后端 `_internal_matfuncs`**：矩阵函数的底层内核，含 Padé 近似计算矩阵指数（`_matfuncs_expm.c`）等。

#### 4.3.2 核心流程

**Cython + Tempita（`_decomp_update`）三步走**：

```
_decomp_update.pyx.in  ──[custom_target, 调 tempita]──►  _decomp_update.pyx
                                                                  │
                                                                  ▼  linalg_cython_gen.process()
                                                          _decomp_update.c
                                                                  │
                                                                  ▼  py3.extension_module()
                                                  _decomp_update.*.so
```

**手写 C++（`_batched_linalg`）一步到位**：

```
src/_batched_linalg_module.cc + src/_linalg_*.hh  ──[py3.extension_module]──►  _batched_linalg.*.so
```

注意 C++ 路线**没有「生成代码」步骤**，源码就是人手写的，所以 `extension_module` 的 `sources` 直接列 `.cc`/`.hh`。

#### 4.3.3 源码精读

先看 Tempita + Cython 的 `_decomp_update`：

> [meson.build:243-256](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L243-L256) 第一个 `custom_target('_decomp_update', ...)` 用 `tempita` 程序把 `_decomp_update.pyx.in` 渲染成 `_decomp_update.pyx`；然后 `py3.extension_module('_decomp_update', linalg_cython_gen.process(_decomp_update_pyx), ...)` 把这个生成的 `.pyx` 喂给 Cython `generator` 转成 `.c` 再编译。注意这里 `extension_module` 的 sources 是 `_decomp_update_pyx`（一个 Meson target 对象），不是字符串——Meson 会自动把 target 的产物接入。

对比一下「纯 Cython、无 Tempita」的 `_matfuncs_sqrtm_triu`，它省掉第一步：

> [meson.build:209-216](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L209-L216) 直接 `_matfuncs_sqrtm_triu.pyx` 经 `linalg_init_cython_gen.process(...)` 转 `.c` 再 `extension_module`。这是最简单的 Cython 扩展形态。

再看 C++ 批量后端 `_batched_linalg`，它的 `sources` 是一串真实源文件：

> [meson.build:182-196](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L182-L196) `py3.extension_module('_batched_linalg', ['src/_common_array_utils.hh', 'src/_linalg_lu_det.hh', 'src/_linalg_inv.hh', 'src/_linalg_solve.hh', 'src/_npymath.hh', 'src/_batched_linalg_module.cc'], include_directories: ['src'], dependencies: [np_dep, lapack_dep], ...)`。所有头文件和入口 `.cc` 一起编译，`include_directories: ['src']` 让 `.cc` 能 `#include "_linalg_inv.hh"`，`dependencies` 链接 NumPy 和 LAPACK。

这个 C++ 模块把一批内部函数注册成 Python 可见的方法，注册表就在入口 `.cc` 里：

> [src/_batched_linalg_module.cc:1495-1507](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_batched_linalg_module.cc#L1495-L1507) `module_methods[]` 把 `_det/_lu/_inv/_solve/_svd/_lstsq/_eig/_cholesky/_qr/_bandwidth` 这些 C++ 函数（来自各 `_linalg_*.hh`）登记为 Python 方法 `_batched_linalg._inv` 等。这就是 Python 层 `_basic.py` 调批量后端时的入口点。

> [src/_batched_linalg_module.cc:1547-1551](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_batched_linalg_module.cc#L1547-L1551) `PyInit__batched_linalg` 是 Python 导入 `_batched_linalg` 时实际寻找的 C 符号——扩展模块名和这个 init 函数名必须严格对应（`PyInit_` + 模块名）。

最后看手写 C 的矩阵函数后端 `_internal_matfuncs`，它把两个 C 内核源文件和模块入口编在一起：

> [meson.build:258-268](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L258-L268) `py3.extension_module('_internal_matfuncs', ['_matfuncsmodule.c', 'src/_matfuncs_expm.c', 'src/_matfuncs_sqrtm.c'], include_directories: ['src/'], dependencies: [np_dep, lapack_dep], ...)`。`_matfuncsmodule.c` 是模块入口（含 `PyInit`），`_matfuncs_expm.c`/`_matfuncs_sqrtm.c` 是被它调用的算法内核。这条链路的具体算法在 u5、u8 单元展开。

#### 4.3.4 代码实践

**实践目标**：把三个扩展（`_decomp_update`、`_batched_linalg`、`_internal_matfuncs`）分别归到对应工具链，并验证模块名与 init 函数的对应关系。

**操作步骤**：
1. 在 [meson.build](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build) 中定位这三个 `extension_module` 的行号。
2. 对 `_decomp_update`，往上找到它的 `custom_target`（Tempita 步骤），确认它「先模板渲染、再 Cython 编译」。
3. 对 `_batched_linalg`，确认它的 sources 全是 `src/*.cc`/`src/*.hh`，没有任何代码生成步骤。
4. 打开 [src/_batched_linalg_module.cc](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_batched_linalg_module.cc)，搜索 `PyInit_`，观察 init 函数名与模块名 `_batched_linalg` 的关系。

**需要观察的现象**：`_decomp_update` 是唯一「`custom_target`(Tempita) + `generator`(Cython) + `extension_module`」三件全用的扩展；`_batched_linalg` 只用 `extension_module` 一件。

**预期结果**：
- `_decomp_update` → Cython（带 Tempita 预处理），依据 [meson.build:243-256](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L243-L256)。
- `_batched_linalg` → 手写 C++，依据 [meson.build:182-196](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L182-L196)。
- `_internal_matfuncs` → 手写 C，依据 [meson.build:258-268](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L258-L268)。

**待本地验证**：若你想确认编译产物，可在已装好 SciPy 的环境里执行 `python -c "import scipy.linalg._batched_linalg as m; print(m.__file__)"`，应得到一个 `.so` 路径。

#### 4.3.5 小练习与答案

**练习 1**：`_decomp_update.pyx.in` 为什么要先用 Tempita 处理成 `.pyx`，而不是直接写 `.pyx`？

**参考答案**：QR 增量更新要为实数（float/double）和复数（complex64/128）等多种数据类型各生成一份高度相似但类型不同的代码。用 Tempita 模板写一份 `.pyx.in`，通过循环/占位符自动展开成多份，能避免手写重复代码、减少维护成本和出错概率。展开后才是标准 Cython 能编译的 `.pyx`。

**练习 2**：假如有人新增了一个纯算法 C 文件 `src/_linalg_new.c`，并想把它编进 `_batched_linalg` 模块，需要在 `meson.build` 的哪里改动？

**参考答案**：在 [meson.build:182-196](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L182-L196) 的 `_batched_linalg` 这个 `extension_module` 的 sources 列表里加上 `'src/_linalg_new.c'`。因为它在 `include_directories: ['src']` 下，新文件能直接 `#include` 现有的 `.hh` 头。

---

## 5. 综合实践

把本讲三件事（识别工具链、读 `meson.build`、对应行号）串起来，完成规格里指定的实践任务。

**任务**：阅读 [meson.build](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build)，列出 `_flapack`、`_batched_linalg`、`_decomp_update` 三个扩展分别由哪种工具（f2py / C++ / Cython）产生，并写出**依据的行**。

**操作步骤**：
1. **`_flapack`**：在文件中找到 `py3.extension_module('_flapack', ...)`（LP64 分支，约 [L106-L114](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L106-L114)）。往上追溯到它的 sources 是 `flapack_module` 这个 `custom_target`（约 [L87-L102](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L87-L102)），该 target 的 `input: 'flapack.pyf.src'`、`command: [generate_f2pymod, ...]`。
   → **结论：f2py 路线**（从 `.pyf.src` 签名生成 `_flapackmodule.c` 再编译）。
2. **`_batched_linalg`**：找到 `py3.extension_module('_batched_linalg', [...])`（约 [L182-L196](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L182-L196)）。sources 直接是 `src/_batched_linalg_module.cc` 等一堆 `.cc`/`.hh`，没有前置 `custom_target`。
   → **结论：手写 C++ 路线**（直接编译）。
3. **`_decomp_update`**：找到 `py3.extension_module('_decomp_update', linalg_cython_gen.process(_decomp_update_pyx), ...)`（约 [L249-L256](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L249-L256)）。其中 `_decomp_update_pyx` 来自上面的 `custom_target('_decomp_update', ...)`（约 [L243-L247](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L243-L247)），用 `tempita` 把 `_decomp_update.pyx.in` 渲染成 `.pyx`，再交给 `linalg_cython_gen`（Cython generator）。
   → **结论：Cython 路线**（带 Tempita 预处理）。

**预期结果汇总表**：

| 扩展模块 | 生成工具 | 关键依据（meson.build 行号） | 源码出处 |
| --- | --- | --- | --- |
| `_flapack` | **f2py** | L87–114（`custom_target` + `extension_module`） | `flapack.pyf.src` + 8 个被 include 的 `flapack_*.pyf.src` |
| `_batched_linalg` | **手写 C++** | L182–196（仅 `extension_module`） | `src/_batched_linalg_module.cc` + `src/_linalg_*.hh` |
| `_decomp_update` | **Cython（+Tempita）** | L243–256（`custom_target` Tempita + `generator` Cython + `extension_module`） | `_decomp_update.pyx.in` |

**延伸思考（可选）**：再补查 `_fblas`、`_solve_toeplitz`、`_internal_matfuncs`、`cython_blas` 分别属于哪条路线，自己画一张「scipy.linalg 扩展全景表」。提示：`cython_blas` 比较特别——它的 `.pyx` 不是仓库里现成的文件，而是由 `cython_linalg` 这个 `custom_target`（[L9-L37](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L9-L37)）用 `_generate_pyx.py` 从签名文本生成的（详见 u7-l3）。

## 6. 本讲小结

- scipy.linalg 的真实算力来自一批**编译扩展**，构建规则全部写在 [meson.build](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build) 里。
- Meson 三件套：`custom_target`/`generator` 负责**生成中间代码**，`py3.extension_module` 负责**编译链接成可 import 的 `.so`**。
- 扩展有三条出身路线：**f2py**（`.pyf.src` 签名 → `_fblas`/`_flapack`，靠 `<prefix>` 一签管四型）、**Cython**（`.pyx` → `.c`，`_decomp_update` 还多一层 Tempita 模板）、**手写 C/C++**（`_batched_linalg` 批量后端、`_internal_matfuncs` 矩阵函数后端）。
- f2py 路线会构建 **LP64 与 ILP64 两套**扩展（`_flapack` / `_flapack_64`），差别在 `F_INT` 整数位宽，由 `use-ilp64` 选项控制。
- 想定位「某扩展从哪来」：先找 `extension_module(name, ...)`，再看它的 sources 是字符串源码（手写）、`custom_target` 产物（f2py/Tempita）、还是 `generator.process(...)`（Cython）。

## 7. 下一步学习建议

本讲只解决了「模块从哪来」。接下来：

- **想会用**：先看 [u1-l4 第一个线性代数程序](u1-l4-first-program.md)，把 `solve`/`inv`/`det`/`norm` 跑起来，建立「Python API → 底层扩展」的体感。
- **想读懂 f2py 接口**：第 7 单元（u7）会讲 [blas.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/blas.py)/[lapack.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/lapack.py) 里的 `get_blas_funcs`/`get_lapack_funcs` 如何在运行时按 dtype 选 s/d/c/z 前缀，以及 `_generate_pyx.py` 怎么生成 `cython_blas.pyx`。
- **想读懂 C++ 批量后端**：第 8 单元（u8）会逐个剖析 `src/_linalg_*.hh` 的批量内核与错误聚合机制。
- **想从源码构建**：参考 [SciPy 官方构建文档](https://scipy.org/install/)，准备 Fortran/C/C++ 编译器、LAPACK/BLAS 库与 Meson + Ninja，再用 `pip install . --no-build-isolation` 体验本讲描述的全部规则。
