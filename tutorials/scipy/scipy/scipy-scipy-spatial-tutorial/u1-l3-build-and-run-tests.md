# 构建系统与运行测试

> 所属单元：u1 走进 scipy.spatial　·　难度：beginner　·　依赖：u1-l1、u1-l2

## 1. 本讲目标

本讲不碰任何算法，只回答两个「装起来、跑起来」的工程问题：

1. `scipy.spatial` 里那些 `import scipy.spatial` 后能用上的类（`KDTree`、`Delaunay`、`Voronoi`……）背后，到底有哪些 `.pyx`（Cython）、`.cxx`（C++）、`.c`（C）被编译成了 Python 扩展模块？Meson 是怎么把它们组织起来的？
2. 我在本机拿到这份源码后，怎么编译它，又怎么跑它的测试套件来确认「我装的是好的」？

学完后你应该能：

- 读懂 [`spatial/meson.build`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/meson.build) 里六个 `extension_module` 各自编译了什么、依赖了什么。
- 说清楚一个 `.pyx` 文件是怎样经过 Cython 生成器变成 `.c`/`.cpp`，再交给 C/C++ 编译器的。
- 知道 `scipy.spatial.test()` 这个测试入口是怎么挂到模块上的，以及为什么 `tests/__init__.py` 是个空文件。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 为什么需要「编译扩展模块」

Python 默认很慢。`scipy.spatial` 里像最近邻查询、三角剖分这种几何计算，如果纯用 Python 写，数据量一大就不可用。所以 SciPy 的做法是：**用 Python 写易读的对外接口，用 Cython / C / C++ 写吃性能的内核**，再把内核编译成一个 `.so`（Linux）/`.pyd`（Windows）文件，让 Python 通过 `import` 当作普通模块来调用。这个编译产物就叫「扩展模块（extension module）」。

- **Cython**：一种「带类型的 Python」。`.pyx` 文件看起来像 Python，但可以声明 C 类型、直接调 C 函数。Cython 工具会先把 `.pyx` 翻译成纯 C 或 C++ 代码（`.c` / `.cpp`），再用普通 C/C++ 编译器编译。
- **pybind11**：一个 C++ 头文件库，让你用很少的胶水代码把 C++ 函数/类暴露给 Python。`_distance_pybind` 用的就是它。
- **C 库直连**：`src/distance_wrap.c` 是纯 C 手写的扩展（f2py/旧式风格），不经过 Cython。

### 2.2 Meson 是什么

Meson 是一个「声明式」的构建系统。你在一个叫 `meson.build` 的文件里**描述**「我要编译什么、依赖什么、装到哪里」，Meson 就负责生成 Ninja 构建文件并调用编译器。它的核心概念就两个：

- `extension_module(name, sources, ...)`：声明一个 Python 扩展模块 `name`，由 `sources` 编译而来。
- `generator(prog, ...)`：声明一个「文件转换器」，比如把每个 `.pyx` 转成 `.c`。

SciPy 的构建是**分层**的：仓库根目录 [`meson.build`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/meson.build) 定义全局变量和公共依赖；[`scipy/meson.build`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/meson.build) 定义 Cython 生成器、`_cython_tree` 等公共组件；各子包的 `meson.build`（比如本讲的 `spatial/meson.build`）再拿来用。分层的好处是子包只写自己的部分，公共逻辑不重复。

### 2.3 测试入口：PytestTester

SciPy 的每个子模块都挂了一个叫 `test` 的属性，调用它就能跑该模块的测试。这个属性是 `scipy._lib._testutils` 里的 `PytestTester` 类实例。它的实现细节我们在第 4.3 节精读，现在只要记住：**`scipy.spatial.test()` 会去 `spatial/tests/` 目录里收集 `test_*.py` 并交给 pytest 执行**。

> 前两讲我们已经知道 `scipy.spatial` 的目录布局和 `__init__.py` 的导出机制。本讲把视角从「Python 层」下沉到「构建层」——回答「这些 `.pyx`/`.cpp` 是怎么变成能 import 的模块的」。

## 3. 本讲源码地图

| 文件 | 作用 | 是否本讲重点 |
| --- | --- | --- |
| `spatial/meson.build` | 声明 spatial 的六个扩展模块 + 安装规则 + 子目录递归 | ✅ 主角 |
| `spatial/__init__.py` | 末尾几行把 `PytestTester` 挂成 `test` 属性 | ✅ 主角 |
| `spatial/tests/__init__.py` | **空文件**，仅让 `tests/` 成为 Python 包 | ✅ 用来讲一个反直觉点 |
| `spatial/tests/meson.build` | 声明安装哪些测试文件和数据基准 | 辅助 |
| `scipy/meson.build` | 定义 `cython_gen` / `cython_gen_cpp` / `_cython_tree` | 辅助（生成器定义来源） |
| `scipy/_lib/meson.build` | 定义 `_lib_pxd`（Cython 公共头依赖） | 辅助 |
| `scipy/_lib/_testutils.py` | `PytestTester` 类的实现 | ✅ 主角 |
| 根 `meson.build` | 定义 `version_link_args`、`qhull_r_dep`、`fs` | 辅助 |

## 4. 核心概念与源码讲解

### 4.1 Meson 构建系统与六个扩展模块总览

#### 4.1.1 概念说明

`scipy.spatial` 对外暴露的能力（KDTree、Delaunay、距离函数等）背后，对应着 **六个** 编译产物。我们在前两讲看到 `__init__.py` 里有一行行 `from ._kdtree import *`、`from ._qhull import *`——这些 `._xxx` 正是六个扩展模块里的几个。把它们和扩展模块对上号，是理解整个子包构建的第一步。

这六个扩展模块分别是：

| 扩展模块 | 主要源文件类型 | 语言栈 | 关键依赖 |
| --- | --- | --- | --- |
| `_qhull` | Cython `.pyx` + C 辅助 | Cython→C | 外部 C 库 **qhull_r** |
| `_ckdtree` | Cython `.pyx` + 7 个 C++ `.cxx` | Cython→C++ | numpy |
| `_distance_wrap` | 纯手写 C | C | numpy |
| `_distance_pybind` | pybind11 C++ | C++ | numpy、**pybind11** |
| `_voronoi` | Cython `.pyx` | Cython→C | numpy |
| `_hausdorff` | Cython `.pyx` | Cython→C | numpy |

它们全部由 [`spatial/meson.build`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/meson.build) 里的 `py3.extension_module(...)` 声明。`py3` 是 Meson 的 Python 模块对象，`extension_module` 就是「声明一个 Python 扩展」的函数。

#### 4.1.2 核心流程

一个扩展模块从源码到 `.so` 的整体流程：

1. **源收集**：把要编译的 `.pyx`（先转 C/C++）、`.cxx`、`.c`、`.cpp` 列成一个列表。
2. **依赖绑定**：声明 `include_directories`（头文件搜索路径）、`dependencies`（外部库，如 numpy、qhull_r、pybind11）、`c_args`/`cpp_args`（编译警告开关）。
3. **链接控制**：`link_args: version_link_args` 用来在 Linux 上限制只导出 `PyInit_*` 符号（避免符号污染）。
4. **安装位置**：`install: true` + `subdir: 'scipy/spatial'` 表示编译出的 `.so` 最终装到 `site-packages/scipy/spatial/` 下。

每条 `extension_module` 都遵守上面这个模板，区别只在「源文件是什么、依赖了谁」。

#### 4.1.3 源码精读

**(a) 六个扩展模块的声明**

下面逐一看 [`spatial/meson.build`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/meson.build) 中的六处 `extension_module`。

1. **`_qhull`** —— 唯一依赖外部 C 库的模块：

   [`meson.build:13-25`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/meson.build#L13-L25) 把 `_qhull.pyx`（经本包自定义的 Cython 生成器 `spt_cython_gen` 转 C）、`qhull_misc.h`、`qhull_misc.c` 一起编译，并链接外部库 `qhull_r_dep`。这个模块支撑了 `Delaunay`/`ConvexHull`/`Voronoi`/`HalfspaceIntersection`。

2. **`_ckdtree`** —— Cython + C++ 混合，且源最多：

   先在 [`meson.build:27-35`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/meson.build#L27-L35) 列出 7 个 C++ 内核文件，再在 [`meson.build:37-54`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/meson.build#L37-L54) 把它们与 `cython_gen_cpp.process('_ckdtree.pyx')`（Cython 转 **C++**）合起来编译。`cpp_args` 里还挂了两个抑制特定编译器警告的开关（`_cpp_Wno_unneeded_internal_declaration`、`_cpp_Wno_unused_function`）。这是本讲实践任务的剖析对象。

3. **`_distance_wrap`** —— 纯 C 手写，最简单：

   [`meson.build:56-63`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/meson.build#L56-L63) 只编译一个 `src/distance_wrap.c`，依赖仅 `np_dep`。没有 Cython、没有 pybind11。

4. **`_distance_pybind`** —— pybind11 后端：

   [`meson.build:65-72`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/meson.build#L65-L72) 编译 `src/distance_pybind.cpp`，依赖里多了 `pybind11_dep`。`_distance_wrap` 与 `_distance_pybind` 是距离度量的两套后端（详见 u9 单元）。

5. **`_voronoi`**：

   [`meson.build:74-81`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/meson.build#L74-L81) 编译 `cython_gen.process('_voronoi.pyx')`（Cython 转 C），单独加了 `-Wno-maybe-uninitialized` 警告抑制。

6. **`_hausdorff`**：

   [`meson.build:83-90`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/meson.build#L83-L90) 编译 `cython_gen.process('_hausdorff.pyx')`，支撑 `directed_hausdorff`。

**(b) 两个「公共依赖」从哪来**

`version_link_args` 和 `qhull_r_dep` 不是 spatial 自己定义的，它们来自根 [`meson.build`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/meson.build)：

- `version_link_args` 在 [`meson.build:133-141`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/meson.build#L133-L141)：默认空列表，仅在非 `clang-cl` 且链接器支持 `--version-script` 时，指向一个只导出 `PyInit_*` 的链接脚本。
- `qhull_r_dep` 在 [`meson.build:231-241`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/meson.build#L231-L241)：按「是否用系统库」三种策略获取 Qhull（系统包 / 带 fallback / 纯 subproject）。

**(c) 安装规则**

除了编译扩展，[`meson.build` 后段](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/meson.build#L92-L122) 还用 `py3.install_sources` 安装三类东西：Qhull 许可证、`.pyi` 类型存根（`_qhull.pyi` 等）、纯 Python 源文件（`_kdtree.py`、`distance.py` 等）。最后两行 [`subdir('tests')`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/meson.build#L121) 和 [`subdir('transform')`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/meson.build#L122) 让 Meson 递归进入子目录的 `meson.build`。

#### 4.1.4 代码实践

> **实践目标**：亲手把 `_ckdtree` 扩展的「输入清单」拆解清楚，验证你对 `extension_module` 的理解。

1. 打开 [`spatial/meson.build`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/meson.build)。
2. 定位 `_ckdtree` 的声明（第 37–54 行）。
3. 把它编译的**全部源文件**抄进一张表（提示：包括 `ckdtree_src` 里的 7 个 `.cxx`，以及 `cython_gen_cpp.process('_ckdtree.pyx')` 生成的 1 个 C++ 文件）。
4. 把它的**依赖/配置**也抄下来：`include_directories`、`cpp_args`、`dependencies`、`link_args`、`subdir`。
5. 预期产出一张这样的清单：

   | 类别 | 内容 |
   | --- | --- |
   | C++ 内核（7 个） | `build.cxx`、`count_neighbors.cxx`、`query.cxx`、`query_ball_point.cxx`、`query_ball_tree.cxx`、`query_pairs.cxx`、`sparse_distances.cxx` |
   | Cython 生成（1 个） | `_ckdtree.pyx` → `_ckdtree.cpp` |
   | 头文件搜索路径 | `../_lib`、`../_build_utils/src`、`ckdtree/src` |
   | 编译参数 | `cython_cpp_args` + 两个 `-Wno-...` 开关 |
   | 依赖 | `np_dep` |
   | 链接参数 | `version_link_args` |
   | 安装子目录 | `scipy/spatial` |

6. **需要观察的现象**：你会发现 `_ckdtree` 是六个扩展里唯一「Cython + 多个手写 C++」的组合，这暗示了它的内核（建树、查询）写在 C++ 里，Cython 只做 Python 包装——这正好是 u8 单元要深入的内容。

> 说明：本实践是「阅读型」，无需编译，预期结果是上面这张清单。是否能在本机编译成功，取决于编译器/numpy 是否就绪——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：六个扩展里，哪一个**既不**用 Cython **也不**用 pybind11，而是纯 C？

> **答案**：`_distance_wrap`（[`meson.build:56-63`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/meson.build#L56-L63)），只编译 `src/distance_wrap.c`。

**练习 2**：`_qhull` 扩展为什么比其它五个多一个 `dependencies: qhull_r_dep`？

> **答案**：因为它直接调用外部 C 库 Qhull 来做凸包/三角剖分等几何运算；其它五个不直接依赖 Qhull。`qhull_r_dep` 在根 [`meson.build:231-241`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/meson.build#L231-L241) 定义。

**练习 3**：`install: true` + `subdir: 'scipy/spatial'` 共同决定了什么？

> **答案**：编译出的 `.so` 在 `pip install` 后会落到 `site-packages/scipy/spatial/` 目录下，从而让 `from scipy.spatial import ...` 能找到它。

---

### 4.2 Cython `.pyx` 到 C/C++ 的生成流程

#### 4.2.1 概念说明

`.pyx` 文件**不能**直接被 C 编译器理解。中间需要一步「转译」：Cython 程序读取 `.pyx`，吐出等价的 `.c`（或 `.cpp`）文件，这一步在 Meson 里由 `generator(...)` 声明。

为什么是「生成器」而不是「手动先转好再提交」？因为 SciPy 选择**只把 `.pyx` 提交进版本库**，`.c`/`.cpp` 由构建时动态生成。好处是 `.pyx` 是唯一的真相来源，避免生成的 C 代码和 `.pyx` 不一致。

`spatial/meson.build` 里出现了**两个不同的 Cython 生成器**，这是本节的关键细节：

- `spt_cython_gen`：本包**自定义**的，专给 `_qhull.pyx` 用，因为它需要额外的 `.pxd` 依赖。
- `cython_gen` / `cython_gen_cpp`：**项目级**的通用生成器，`_voronoi`/`_hausdorff` 用前者（转 C），`_ckdtree` 用后者（转 C++）。

> 名词解释：`.pxd` 是 Cython 的「头文件」，用来声明 C 级别的结构体、函数签名、`cdef class` 等，供 `.pyx` `cimport`。`generator` 里的 `depends` 字段，作用是声明「这些辅助文件变了，就要重新生成」，是一种正确性保险。

#### 4.2.2 核心流程

一个 `.pyx` 变成可链接代码的步骤：

1. Meson 调用 `cython` 程序，参数由 `cython_args` 给定（[`scipy/meson.build:474`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/meson.build#L474)：`-3`（按 Python 3 语法）、`--fast-fail`、`--include-dir` 等）。
2. `.pyx` → `@BASENAME@.c`（纯 C 生成器 `cython_gen`）或 `.cpp`（`--cplus` 的 `cython_gen_cpp`）。`@BASENAME@` 是 Meson 模板占位符，代表「去掉扩展名后的文件名」。
3. 生成的 `.c`/`.cpp` 和其它手写源一起进入 `extension_module`，交给 C/C++ 编译器。
4. 链接成 `.so`。

两条生成器产物的差异用一个对比说清：

| 生成器 | 定义位置 | `--cplus`? | 用于谁 |
| --- | --- | --- | --- |
| `cython_gen` | `scipy/meson.build` | 否 → `.c` | `_voronoi.pyx`、`_hausdorff.pyx` |
| `cython_gen_cpp` | `scipy/meson.build` | 是 → `.cpp` | `_ckdtree.pyx` |
| `spt_cython_gen` | `spatial/meson.build` | 否 → `.c` | `_qhull.pyx`（带额外 `.pxd` 依赖） |

#### 4.2.3 源码精读

**(a) spatial 自定义生成器与 `_spatial_pxd`**

[`spatial/meson.build:1-4`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/meson.build#L1-L4) 先定义 `_spatial_pxd`：用 `fs.copyfile` 把 `_qhull.pxd` 和 `setlist.pxd` 复制到构建目录，供 Cython `cimport` 时找到。

接着 [`spatial/meson.build:8-11`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/meson.build#L8-L11) 定义本包专属的 `spt_cython_gen`，注意它的 `depends` 列了四个东西：`_cython_tree`、`_spatial_pxd`、`_lib_pxd`、`cython_lapack_pxd`——比通用生成器多，这就是它要单独定义的原因。

**(b) `_cython_tree` 是什么、为什么需要**

`_cython_tree` 在 [`scipy/meson.build:472`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/meson.build#L472) 定义，注释（第 468–471 行）解释得很直白：它把顶层 `__init__.py` 复制到构建目录，**「骗过」Cython**，让它以为 `.pyx` 处于一个完整 Python 包里，从而能做相对导入（`cimport scipy._lib...`）。这是 SciPy 全包通用的技巧。

**(c) 通用生成器**

[`scipy/meson.build:501-511`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/meson.build#L501-L511) 定义了 `cython_gen`（输出 `.c`）和 `cython_gen_cpp`（多了 `--cplus`，输出 `.cpp`）。spatial 里的 `_voronoi`、`_hausdorff`、`_ckdtree` 都直接复用这两个。

**(d) `_lib_pxd`**

[`scipy/_lib/meson.build:1-5`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/_lib/meson.build#L1-L5) 定义 `_lib_pxd`，把 `_ccallback_c.pxd`、`messagestream.pxd` 等 Cython 公共头复制到构建目录。`messagestream.pxd` 正是 `_qhull.pyx` 用来捕获 Qhull C 库输出信息所依赖的（注释 [`spatial/meson.build:7`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/meson.build#L7) 也提到了这点）。

#### 4.2.4 代码实践

> **实践目标**：追踪一个 `.pyx` 的「转译命令」，确认生成器确实在做 `.pyx → .c/.cpp`。

1. 读 [`scipy/meson.build:474`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/meson.build#L474) 的 `cython_args`，记下它的每个参数含义：`-3`（Python3）、`--fast-fail`（首个错误即停）、`--output-file @OUTPUT@`（输出到 Meson 指定位置）、`--include-dir @BUILD_ROOT@`（头搜索根）、`@INPUT@`（输入 `.pyx`）。
2. 读 [`scipy/meson.build:507-511`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/meson.build#L507-L511)，对比 `cython_gen_cpp`：它在 `cython_args` 前加了 `['--cplus']`，于是产物从 `.c` 变成 `.cpp`。
3. 回到 [`spatial/meson.build:74-81`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/meson.build#L74-L81)（`_voronoi`，用 `cython_gen`）和 [`spatial/meson.build:37-38`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/meson.build#L37-L38)（`_ckdtree`，用 `cython_gen_cpp`）。
4. **需要观察的现象**：`_voronoi.pyx` → `_voronoi.c`；`_ckdtree.pyx` → `_ckdtree.cpp`。两者用不同的生成器，是因为 `_ckdtree` 的 C++ 内核需要和 C++ 代码一起链接。
5. 如果本机已编译过 SciPy，可在构建目录 `build/scipy/spatial/` 下找到生成的 `_ckdtree.cpp`、`_voronoi.c`、`_qhull.c` 文件作为佐证——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_qhull.pyx` 要用本包自定义的 `spt_cython_gen`，而不是通用的 `cython_gen`？

> **答案**：因为 `_qhull.pyx` 通过 `cimport` 依赖额外的 `.pxd` 文件（`_qhull.pxd`、`setlist.pxd`，即 `_spatial_pxd`，以及 `cython_lapack_pxd`），生成器的 `depends` 必须把这些列上，Cython 才能在构建目录里找到它们（见 [`meson.build:8-11`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/meson.build#L8-L11)）。

**练习 2**：`@BASENAME@` 和 `@OUTPUT@` 这些带 `@` 的记号是什么？

> **答案**：Meson 的模板占位符。`@BASENAME@` = 输入文件去掉扩展名后的名字（`_voronoi.pyx` → `_voronoi`），所以 `output: '@BASENAME@.c'` 会产出 `_voronoi.c`；`@INPUT@`/`@OUTPUT@` 是命令行里输入/输出路径的占位。

**练习 3**：`_cython_tree` 复制 `__init__.py` 到构建目录的目的是什么？

> **答案**：让 Cython「以为」构建目录里有一个完整的 `scipy` 包，从而允许 `.pyx` 里写 `from scipy._lib.xxx cimport ...` 这样的相对/包内导入（见 [`scipy/meson.build:468-472`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/meson.build#L468-L472) 的注释）。

---

### 4.3 PytestTester 测试入口与 tests 目录

#### 4.3.1 概念说明

SciPy 给**每个**子模块统一挂了一个 `test` 属性，调用它就跑该模块的测试。这个机制靠 `scipy._lib._testutils` 里的 `PytestTester` 类实现。它本身不是测试框架，只是一个「把 pytest 调用包起来」的薄壳。

一个容易踩坑的反直觉点：**`spatial/tests/__init__.py` 是个 0 字节的空文件**。读者第一反应可能是「测试收集逻辑写在 `tests/__init__.py` 里吧」——并不是。它存在的唯一目的是让 `tests/` 目录被 Python 当成一个**包**，从而能被 `pip install` 和 `--pyargs` 正确找到；真正的测试发现由 `pytest` 自己按 `test_*.py` 命名约定完成。

#### 4.3.2 核心流程

`scipy.spatial.test()` 被调用时发生的事：

1. `PytestTester.__call__` 拿到调用者模块名（`scipy.spatial`）。
2. 把 `label`（默认 `'fast'`）翻译成 pytest 标记过滤：`-m "not slow"`，即默认跳过慢测试。
3. 构造 pytest 参数列表（`--showlocals`、`--tb=short`，可选 `-n` 并行、`--cov` 覆盖率）。
4. 追加 `--pyargs` + 模块名，让 pytest 按已安装包的方式收集测试。
5. 调用 `pytest.main(pytest_args)` 执行。
6. 返回布尔值：退出码为 0 表示全部通过。

#### 4.3.3 源码精读

**(a) 在 `__init__.py` 里挂上入口**

[`spatial/__init__.py:128-130`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/__init__.py#L128-L130) 是关键三行：

```python
from scipy._lib._testutils import PytestTester
test = PytestTester(__name__)
del PytestTester
```

`__name__` 此时是 `'scipy.spatial'`，所以这个 `test` 实例绑定了 spatial 命名空间。`del PytestTester` 是为了不让 `PytestTester` 这个名字泄漏进模块公共 API（否则它会出现在 `dir()` 里）。

**(b) `PytestTester` 实现**

完整类定义见 [`scipy/_lib/_testutils.py:63-142`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/_lib/_testutils.py#L63-L142)，几个要点：

- 构造只存模块名：[`L93-94`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/_lib/_testutils.py#L93-L94)。
- 默认 `label="fast"` 时加 `-m not slow`：[`L118-121`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/_lib/_testutils.py#L118-L121)。所以「跑 `test()`」默认不跑标了 `slow` 的测试，要全跑得传 `test(label='full')`。
- `--pyargs` + 模块名定位测试：[`L135`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/_lib/_testutils.py#L135)。
- 调 `pytest.main` 并据退出码返回布尔：[`L138`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/_lib/_testutils.py#L138) 与 [`L142`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/_lib/_testutils.py#L142)。

**(c) 空的 `tests/__init__.py` 与 `tests/meson.build`**

[`spatial/tests/__init__.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/__init__.py) 是 0 字节文件——只充当包标记。真正声明「安装哪些测试」的是 [`spatial/tests/meson.build:1-14`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/meson.build#L1-L14)，它列出 8 个 `test_*.py`；其后的 [`L16-51`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/meson.build#L16-L51) 列出 `data/` 下大量 `.txt`/`.npz` 数据基准（这些是 `pdist`/`cdist` 的回归用参考数据，u11 会用到）。注意 `install_tag: 'tests'`——这些文件只在带 `tests` tag 安装时才装。

#### 4.3.4 代码实践

> **实践目标**：跑通 spatial 的测试子集，并理解默认行为。

操作步骤（**待本地验证**，需要本机已 `pip install` 或 editable 安装 SciPy）：

1. 确认能导入：

   ```bash
   python -c "import scipy.spatial as s; print(hasattr(s, 'test'))"
   ```

   预期输出 `True`。
2. 跑一个**子集**，并计时（用 `extra_argv` 把范围缩到一个文件，避免全量太慢）：

   ```bash
   time python -c "import scipy.spatial as s; s.test(extra_argv=['scipy/spatial/tests/test_slerp.py'])"
   ```

   预期：pytest 收集并运行 `test_slerp.py` 里的用例，末尾打印耗时。
3. 体验 `label` 差异：默认 `test()` 等价于 `-m "not slow"`；想跑全部则 `s.test(label='full')`。

**需要观察的现象**：第 2 步会输出 pytest 的进度点和结果汇总；返回值是 `True`（退出码 0 表示通过）。如果改传一个不存在的文件名，会看到「no tests ran」。具体耗时取决于机器——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`spatial/__init__.py` 末尾为什么要 `del PytestTester`？

> **答案**：因为该文件的 `__all__` 用 `[s for s in dir() if not s.startswith('_')]` 动态生成（见 u1-l2）。若不 `del`，`PytestTester` 会以非下划线开头被收进公共 API，污染对外接口。`del` 后只剩真正想要的 `test`。

**练习 2**：`tests/__init__.py` 是空的，那「哪些文件算测试」由谁决定？

> **答案**：由 pytest 的命名约定（`test_*.py`）决定，并由 `tests/meson.build` 的 `py3.install_sources(...)` 决定**哪些会被安装**。`__init__.py` 只负责把 `tests/` 变成 Python 包。

**练习 3**：`scipy.spatial.test()` 默认会跑标了 `@pytest.mark.slow` 的测试吗？

> **答案**：不会。默认 `label="fast"`，会追加 `-m "not slow"`（[`_testutils.py:118-121`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/_lib/_testutils.py#L118-L121)）。要跑慢测试需显式 `test(label='full')`。

---

## 5. 综合实践

把三个最小模块串起来，完成一个「从构建声明到测试验证」的小闭环：

1. **读声明**：打开 [`spatial/meson.build`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/meson.build)，画一张表，列出六个 `extension_module` 的「名字 / 源文件 / Cython 生成器 / 关键依赖」。
2. **追踪一条 `.pyx`**：选 `_voronoi.pyx`，写出它经过的完整链路：`_voronoi.pyx` → （`cython_gen` 生成）`_voronoi.c` → （C 编译）`_voronoi.so` → 被 `__init__.py` 的 `from ._qhull import *` 这类语句（实际是 `from ._spherical_voronoi import SphericalVoronoi`，而后者内部 `cimport` 了 `_voronoi`）使用。
3. **验证入口**：写一个小脚本，调用 `scipy.spatial.test(extra_argv=[...])` 只跑 `test_kdtree.py`，并打印它的返回值与耗时。
4. **反思**：回答一个问题——如果你给 `_ckdtree.pyx` 加了一个新的 `cimport xxx`（依赖某个新 `.pxd`），你需要改 `spatial/meson.build` 的哪一处才能让构建不出错？

   > 参考答案：要更新对应生成器的 `depends` 列表，或在 `_spatial_pxd`/`_lib_pxd` 里 `fs.copyfile` 那个新 `.pxd`，否则 Cython 在构建目录里 `cimport` 不到。

完成第 1–3 步后，你就把「源码 → 编译 → 安装 → 测试」整条工程链在 spatial 子包上走了一遍。

## 6. 本讲小结

- `scipy.spatial` 把性能内核编译成 **六个** Python 扩展模块：`_qhull`、`_ckdtree`、`_distance_wrap`、`_distance_pybind`、`_voronoi`、`_hausdorff`，全部在 [`spatial/meson.build`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/meson.build) 里用 `py3.extension_module` 声明。
- 这六个模块的语言栈各异：Cython→C（`_qhull`/`_voronoi`/`_hausdorff`）、Cython→C++（`_ckdtree`）、纯 C（`_distance_wrap`）、pybind11 C++（`_distance_pybind`）。
- `.pyx` 经 Meson `generator(cython, ...)` 转成 `.c`/`.cpp` 再编译；`spatial` 为 `_qhull.pyx` 自定义了带额外 `.pxd` 依赖的 `spt_cython_gen`，其余复用项目级 `cython_gen`/`cython_gen_cpp`。
- 测试入口 `scipy.spatial.test` 是 [`PytestTester`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/_lib/_testutils.py#L63-L142) 实例，默认 `label="fast"` 会用 `-m "not slow"` 跳过慢测试。
- `tests/__init__.py` 是 0 字节空文件，只起包标记作用；真正的测试清单和数据基准在 [`tests/meson.build`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/meson.build) 里声明。

## 7. 下一步学习建议

- 现在你已经知道「这些扩展怎么被编译出来」，下一站 **u2 单元（KDTree 最近邻查询）** 会真正打开 `_ckdtree` 这个扩展，从 Python 用法层面理解它对外提供什么。
- 如果你对构建本身更感兴趣，可以直接阅读 [`scipy/meson.build`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/meson.build) 第 465–518 行附近的 Cython/pythran 生成器定义，以及根 [`meson.build`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/meson.build) 的外部依赖（qhull_r、boost、pybind11）声明。
- 想理解「测试怎么写、怎么用 data 基准」，可先翻一眼 [`spatial/tests/meson.build`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/meson.build) 列出的 `data/pdist-*.txt`，这会为 **u11-l2（测试体系与实践）** 埋下伏笔。
