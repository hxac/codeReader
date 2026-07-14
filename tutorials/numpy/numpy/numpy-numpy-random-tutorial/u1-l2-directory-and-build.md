# 目录结构、构建系统与模块地图

## 1. 本讲目标

在上一讲里，我们已经建立了对 `numpy.random` 的全局认知：知道它有推荐的 `Generator` / `default_rng` 新 API 和遗留的 `RandomState` 旧 API，并且知道新 API 是「BitGenerator 产出比特流 + Generator 转成分布」的两层架构。

但是，当你在 `numpy/random/` 目录里打开文件时，会看到一堆 `.pyx`、`.pxd`、`.c`、`.pyx.in`、`meson.build`，很容易迷路：

- 这些文件分别是什么？
- 它们是怎么被编译成可以 `import` 的 Python 模块的？
- 为什么有一个叫 `npyrandom` 的「静态库」？
- 不同扩展模块之间的依赖关系是怎样的？

本讲学完后，你应该能够：

1. 画出 `numpy/random/` 的目录树，并说明每个子目录的职责。
2. 区分 `.pyx` / `.pxd` / `.c` / `.pyx.in` 这四类源码文件的作用，建立「三层源码地图」。
3. 读懂 `meson.build`，说清楚 `npyrandom` 静态库是怎么构建的、9 个 Cython 扩展模块分别由哪些源码编译而来、它们分别链接了哪些静态库。
4. 理解 `meson.build` 的安装规则，知道最终哪些文件会被装进 `site-packages`。

## 2. 前置知识

本讲需要一点前置概念，我们用最通俗的方式解释。

### 2.1 什么是 Cython（.pyx / .pxd）

Cython 是一门「带类型注解的 Python 方言」。它的源码后缀是 `.pyx`，写起来很像 Python，但可以声明 C 级别的变量类型、调用 C 函数。Cython 编译器会把 `.pyx` 翻译成一个 `.c` 文件，再用普通 C 编译器编译成一个 Python 可以 `import` 的「扩展模块」（`.so` / `.pyd`）。

- `.pyx`：Cython 的**实现文件**，类似 `.c`/`.py`，包含真正的代码逻辑。
- `.pxd`：Cython 的**声明文件**，类似 C 的头文件 `.h`。它用来声明结构体、函数签名、`cdef class` 的成员，供其它 `.pyx` 文件 `cimport` 复用。

> 类比记忆：`.pxd` 之于 `.pyx`，就像 `.h` 之于 `.c`。

`numpy.random` 大量使用 Cython，是因为它既要暴露成 Python API，又要调用高性能的 C 采样函数。Cython 正好是这座桥。

### 2.2 什么是静态库

把一组 `.c` 文件编译、归档成一个单独的文件（Linux 上是 `libxxx.a`，MSVC 上是 `xxx.lib`），就叫「静态库」。别的地方用到里面的函数时，只要在链接阶段声明「我依赖这个静态库」，就能复用同一份 C 实现，而不必把源码复制过去。

`numpy.random` 把所有分布采样算法集中放在一个叫 **`npyrandom`** 的静态库里，各个 BitGenerator 包装模块只要链接它，就能共享同一份分布代码。

### 2.3 什么是 Meson

Meson 是 NumPy 使用的构建系统。它的配置文件叫 `meson.build`，用一种类似 Python 的语法描述「要编译什么、依赖什么、装到哪里」。NumPy 顶层有一个大的 `numpy/meson.build`，里面通过 `subdir('random')` 进入 `numpy/random/meson.build`（本讲的主角）。子目录的构建文件可以直接使用父级定义好的变量。

### 2.4 什么是 Tempita 模板（.pyx.in）

有些源码是「有规律地重复」的，比如为 `int8 / int16 / int32 / int64` 各写一遍几乎相同的函数。NumPy 用一个叫 Tempita 的模板工具，写一个 `_bounded_integers.pyx.in` 模板，构建时由 `tempita_cli` 把它「展开」成真正的 `_bounded_integers.pyx`。模板文件第一行的 `#!python` 就是 Tempita 的标记。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [`meson.build`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/meson.build) | 本子系统的构建中枢：定义静态库、9 个扩展模块、安装规则 |
| [`__init__.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/__init__.py) | 包入口，决定 `import numpy.random` 后能看到哪些名字 |
| [`c_distributions.pxd`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/c_distributions.pxd) | Cython 侧对 `npyrandom` 静态库函数签名的声明，是 `.pyx` 调用 C 函数的桥梁 |
| `bit_generator.pxd` / `__init__.pxd` | 声明核心结构体 `bitgen_t` 与 `BitGenerator` 类 |
| `_common.pyx` / `_generator.pyx` / `bit_generator.pyx` 等 `.pyx` | 各扩展模块的实现 |
| `src/` 目录 | 全部 C 实现（分布算法 + 各生成器内核） |

---

## 4. 核心概念与源码讲解

### 4.1 目录结构总览：三层源码与生成器分目录

#### 4.1.1 概念说明

打开 `numpy/random/`，你会看到文件名带下划线前缀的 Python/Cython 文件（`_generator.pyx`、`_pcg64.pyx` 等）、一个 `src/` 目录、一个 `include/` 目录、一个 `tests/` 目录、一个 `_examples/` 目录。

理解这套布局的关键是抓住两条线索：

1. **语言三层**：Python/Cython 接口层（`.pyx`/`.py`） → Cython 声明层（`.pxd`） → 纯 C 实现层（`src/**/*.c`）。上一层调用下一层。
2. **按生成器分目录**：`src/` 下面每一种 BitGenerator（`mt19937`、`pcg64`、`philox`、`sfc64`）各有自己的子目录，放各自的 C 内核；而所有「分布采样」算法集中放在 `src/distributions/`，与具体生成器无关。

这种「生成器内核各自独立、分布算法集中共享」的切分，正是上一讲说的「两层架构」在文件系统上的投影。

#### 4.1.2 核心流程

`numpy/random/` 的目录可以归纳为下面这棵树：

```
numpy/random/
├── meson.build              ← 构建中枢
├── __init__.py / __init__.pxd / __init__.pyi   ← 包入口（运行期/类型/声明）
├── bit_generator.pyx/.pxd/.pyi                  ← BitGenerator 基类 + SeedSequence
├── _generator.pyx/.pyi                          ← Generator 类 + default_rng
├── _common.pyx/.pxd/.pyi                        ← 分布方法的公共模板（广播/约束）
├── _bounded_integers.pyx.in/.pxd.in/.pyi        ← 区间整数（Tempita 模板）
├── _pickle.py/.pyi                              ← pickle 辅助构造器
├── _mt19937.pyx / _pcg64.pyx / _philox.pyx / _sfc64.pyx   ← 各生成器的 Cython 包装
├── mtrand.pyx                                   ← 遗留 RandomState
├── c_distributions.pxd                          ← 对 npyrandom C 库的声明
├── include/                                     ← 内部 C 头文件
│   ├── aligned_malloc.h
│   └── legacy-distributions.h
├── src/                                         ← 全部 C 实现
│   ├── distributions/    ← 分布采样算法（→ 编进 npyrandom 静态库）
│   │   ├── distributions.c
│   │   ├── logfactorial.c / .h
│   │   ├── random_hypergeometric.c
│   │   ├── random_mvhg_count.c
│   │   ├── random_mvhg_marginals.c
│   │   └── ziggurat_constants.h
│   ├── legacy/           ← 遗留分布（legacy-distributions.c）
│   ├── mt19937/          ← MT19937 内核（mt19937.c / mt19937-jump.c / randomkit.c）
│   ├── pcg64/            ← PCG64 内核（pcg64.c / pcg64.h）
│   ├── philox/           ← Philox 内核（philox.c / philox.h）
│   ├── sfc64/            ← SFC64 内核（sfc64.c / sfc64.h）
│   └── splitmix64/       ← splitmix64（用于种子初始化）
├── tests/                ← 测试与 tests/data 的 CSV 测试集
└── _examples/            ← cython / cffi / numba 三套扩展示例
```

一个值得记住的对应关系：**顶层每一个 `_xxx.pyx` 包装文件，几乎都对应 `src/xxx/` 里的一组 C 文件**。例如 `_pcg64.pyx` 包装 `src/pcg64/pcg64.c`。而 `_generator.pyx` 和 `bit_generator.pyx` 是「容器/调度层」，它们本身不带生成器内核，而是调用进 `npyrandom` 静态库里的分布算法。

#### 4.1.3 源码精读

包入口 `__init__.py` 用一组 `from . import` 把这些扩展模块拼接成最终的公开 API。注意它把哪些名字导入了进来：

[`__init__.py:180-191`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/__init__.py#L180-L191) 这一段说明：`default_rng`/`Generator` 来自 `_generator`、四个生成器分别来自 `_mt19937`/`_pcg64`/`_philox`/`_sfc64`、`BitGenerator`/`SeedSequence` 来自 `bit_generator`、遗留的 `RandomState` 通过 `from .mtrand import *` 进来。

```python
from . import _bounded_integers, _common, _pickle
from ._generator import Generator, default_rng
from ._mt19937 import MT19937
from ._pcg64 import PCG64, PCG64DXSM
from ._philox import Philox
from ._sfc64 import SFC64
from .bit_generator import BitGenerator, SeedSequence
from .mtrand import *
```

这正好印证了上一讲提到的「新旧两套 API」：左侧 `_generator`/`_pcg64`/... 是新 API，右侧 `mtrand` 是遗留 API。它们在 `__init__.py` 这里汇合。

#### 4.1.4 代码实践

1. **实践目标**：建立目录与公开名字之间的直觉。
2. **操作步骤**：
   - 在仓库里列出 `numpy/random/` 顶层文件（例如 `ls numpy/random`）。
   - 对照上面的目录树，把每个顶层 `.pyx` 文件对应到一个「它能 import 出来的名字」（提示：看 `__init__.py` 的 import 行）。
3. **观察现象**：你会发现 `Generator` 来自 `_generator`、`PCG64` 来自 `_pcg64`、`MT19937` 来自 `_mt19937`，名字几乎一一对应。
4. **预期结果**：写一张「顶层 `.pyx` → 公开类/函数」对照表。
5. 若无法在本地构建运行，可只做源码阅读并标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`_pickle.py` 是纯 Python 文件，为什么它会和 `.pyx` 一起出现在这个目录里？

> **答案**：因为 pickle 序列化需要一个稳定的、跨版本的「构造器函数」来重建对象。这些构造器（如 `__generator_ctor`）用纯 Python 实现最稳妥，所以 `_pickle.py` 作为辅助层存在，详见后续 pickle 讲义。

**练习 2**：`src/distributions/` 里的算法，和某个具体生成器（比如 PCG64）耦合吗？

> **答案**：不耦合。分布算法只通过一个 `bitgen_t *` 指针（见下一讲）索取「下一个随机数」，完全不关心它来自哪个生成器。这正是把分布代码集中放进 `npyrandom` 静态库、而不是塞进每个生成器目录的原因。

---

### 4.2 npyrandom 静态库：分布采样的 C 内核

#### 4.2.1 概念说明

「分布采样」是指把均匀随机比特流转换成某种概率分布（正态、泊松、二项……）的数学过程。NumPy 把所有这些 C 实现集中打包成一个**静态库 `npyrandom`**，供多个扩展模块共享链接。

这样做有三个好处：

1. **复用**：`_generator`、`_common`、各个 BitGenerator 包装都要调分布函数，不必各自复制一份 `distributions.c`。
2. **单一实现**：所有新 API 的分布行为来自同一份代码，避免分叉。
3. **可被外部扩展链接**：`npyrandom` 会被安装到 `numpy/random/lib`，第三方（Cython/CFFI/Numba）扩展也能 `find_library('npyrandom')` 链接它（见 `_examples/cython/meson.build`）。

#### 4.2.2 核心流程

构建 `npyrandom` 的流程是：

```
5 个 C 源码文件（全部在 src/distributions/）
        │  (用 C 编译器编译 + 归档)
        ▼
   libnpyrandom.a / npyrandom.lib   ← 静态库
        │  (install: true)
        ▼
   <site-packages>/numpy/random/lib/   ← 安装位置
```

5 个源文件分别是：

- `src/distributions/distributions.c`：绝大多数分布采样（正态、指数、均匀、二项、泊松……）。
- `src/distributions/logfactorial.c`：对数阶乘表（超几何分布等用到）。
- `src/distributions/random_hypergeometric.c`：超几何分布。
- `src/distributions/random_mvhg_count.c`：多元超几何（count 法）。
- `src/distributions/random_mvhg_marginals.c`：多元超几何（marginals 法）。

#### 4.2.3 源码精读

`meson.build` 顶部就是构建 `npyrandom` 的全部内容：

[`meson.build:1-19`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/meson.build#L1-L19) 定义 `npyrandom_sources`（5 个 C 文件），并用 `static_library(...)` 把它们归档成 `npyrandom`，安装到 `numpy/random/lib`：

```meson
npyrandom_sources = [
  'src/distributions/logfactorial.c',
  'src/distributions/distributions.c',
  'src/distributions/random_mvhg_count.c',
  'src/distributions/random_mvhg_marginals.c',
  'src/distributions/random_hypergeometric.c',
]

npyrandom_lib = static_library('npyrandom',
  npyrandom_sources,
  ...
  install: true,
  install_dir: np_dir / 'random/lib',
  name_prefix: name_prefix_staticlib,
  name_suffix: name_suffix_staticlib,
)
```

几个要点：

- `np_dir = py.get_install_dir() / 'numpy'`（定义于顶层 `numpy/meson.build:338`），所以 `np_dir / 'random/lib'` 就是 `<site-packages>/numpy/random/lib`。
- `name_prefix_staticlib` / `name_suffix_staticlib` 控制文件名：在 MSVC 上产出 `npyrandom.lib`，其它平台产出默认的 `libnpyrandom.a`（定义于顶层 `numpy/meson.build:35-41`）。这是为了避免历史 distutils 构建找不到库的问题（注释里写了 gh-23981）。
- 依赖 `py_dep` 和 `np_core_dep`，因为分布代码会用到 Python C-API 和 NumPy 的核心定义。

那么 `.pyx` 怎么知道这个库里有哪些函数？靠 `c_distributions.pxd`。它是 Cython 侧的「函数清单」：

[`c_distributions.pxd:7-33`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/c_distributions.pxd#L7-L33) 用 `cdef extern from "numpy/random/distributions.h"` 声明了一长串 C 函数签名，例如标准均匀、标准指数、标准正态的各种变体：

```cython
cdef extern from "numpy/random/distributions.h":
    ...
    double random_standard_uniform(bitgen_t *bitgen_state) nogil
    void random_standard_uniform_fill(bitgen_t* bitgen_state, npy_intp cnt, double *out) nogil
    double random_standard_normal(bitgen_t* bitgen_state) nogil
    ...
```

注意三件事：

1. 每个分布函数的第一个参数都是 `bitgen_t *bitgen_state`——这就是「给我一个能产随机比特的东西」的抽象入口，和具体生成器无关（这是下一讲的重点）。
2. 几乎都标了 `nogil`，表示可以释放 Python 全局解释器锁、在多线程里高速采样。
3. 这里的 `"numpy/random/distributions.h"` 是构建期生成并安装的公开头文件（源码树里没有这个 `.h`，它由构建系统生成，并通过 `numpy/_core/include/meson.build` 安装到 `numpy/_core/include/numpy/random/`）。

再举一个带状态结构的例子，二项分布的声明：

[`c_distributions.pxd:9-28`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/c_distributions.pxd#L9-L28) 声明了 `binomial_t` 结构体（缓存二项采样的中间状态），以及 [`c_distributions.pxd:90`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/c_distributions.pxd#L90) 的 `random_binomial(bitgen_t *brng, double p, int64_t n, binomial_t *binomial)`，它会把上一次采样的临时量存进 `binomial_t` 以加速重复调用。

#### 4.2.4 代码实践

1. **实践目标**：体会「`c_distributions.pxd` 是 `npyrandom` 库的函数清单」。
2. **操作步骤**：
   - 打开 `c_distributions.pxd`，搜索三个名字：`random_standard_normal`、`random_poisson`、`random_binomial`。
   - 记下它们的完整签名。
3. **观察现象**：三个函数的第一个参数都是 `bitgen_t *bitgen_state`。
4. **预期结果**：你能复述出它们的参数类型与返回类型，并解释「为什么分布函数只依赖 `bitgen_t *`，而不依赖 `PCG64` 或 `MT19937`」——因为这是抽象接口，生成器只要把自己塞进 `bitgen_t` 就能被复用。
5. 待本地验证（无需编译，纯阅读）。

#### 4.2.5 小练习与答案

**练习 1**：`npyrandom` 静态库被安装到了哪个目录？为什么 NumPy 要把它装出来？

> **答案**：装到 `<site-packages>/numpy/random/lib`。装出来是为了让第三方 Cython/CFFI/Numba 扩展也能链接同一份分布实现（见 `_examples/cython/meson.build` 里的 `cc.find_library('npyrandom', dirs: npyrandom_path)`）。

**练习 2**：为什么 `_mt19937.pyx`、`_pcg64.pyx` 这些生成器包装模块也要链接 `npyrandom`？

> **答案**：因为生成器包装模块除了暴露原始比特流，往往还会实现 `random_raw` 之外的能力，且要保证「同一个生成器也能用统一的分布接口」。链接 `npyrandom` 让它们都能调用同一套分布算法，而不是各自实现一遍。

---

### 4.3 Cython 扩展模块列表：从 .pyx 到可 import 模块

#### 4.3.1 概念说明

`numpy.random` 一共有 **9 个** Cython 扩展模块。它们由 `meson.build` 里的 `random_pyx_sources` 列表统一描述，再用一个 `foreach` 循环批量编译。

理解这一段的关键是 `random_pyx_sources` 里每一项的格式：

```meson
[名称, 源码列表, 额外 c_args, 要链接的静态库列表]
```

也就是说，每一行就回答了四个问题：编译出来的模块叫什么、由哪些源码编译、要加什么编译参数、要链接哪些库。把它读通，你就掌握了整个子系统的编译依赖图。

#### 4.3.2 核心流程

```
random_pyx_sources（9 项）
        │
        ▼  foreach 逐项处理
py.extension_module(...)
        │  Cython 把 .pyx 翻译成 .c → C 编译 → 链接 npyrandom / npymath
        ▼
9 个 .so 扩展模块，装到 <site-packages>/numpy/random/
```

其中还有一个**代码生成**环节：`_bounded_integers.pyx.in` 和 `_bounded_integers.pxd.in` 是 Tempita 模板，构建时先用 `tempita_cli` 展开成真正的 `.pyx` / `.pxd`，再进入上面的编译流程。

#### 4.3.3 源码精读

先看 Tempita 模板的展开（代码生成）：

[`meson.build:32-45`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/meson.build#L32-L45) 用 `custom_target` 把 `_bounded_integers.pxd.in` 和 `_bounded_integers.pyx.in` 通过 `tempita_cli` 展开成 `_bounded_integers.pxd` / `_bounded_integers.pyx`：

```meson
_cython_tree_random += custom_target('_bounded_integer_pxd',
  output: '_bounded_integers.pxd',
  input: '_bounded_integers.pxd.in',
  command: [tempita_cli, '@INPUT@', '-o', '@OUTPUT@'],
  install: true,
  ...
)

_bounded_integers_pyx = custom_target('_bounded_integer_pyx',
  output: '_bounded_integers.pyx',
  input: '_bounded_integers.pyx.in',
  command: [tempita_cli, '@INPUT@', '-o', '@OUTPUT@'],
)
```

> 注意 `_bounded_integers.pyx.in` 第一行是 `#!python`，这正是 Tempita 模板的标记。展开后会为每种整型宽度生成特化代码，这是后续「区间整数」讲义的主题。

接着是核心的扩展模块清单：

[`meson.build:61-81`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/meson.build#L61-L81) 定义了 9 个扩展模块。把每一项拆开看：

```meson
random_pyx_sources = [
  ['_bounded_integers', _bounded_integers_pyx, [], [npyrandom_lib, npymath_lib]],
  ['_common', '_common.pyx', [], [npyrandom_lib]],
  ['_mt19937', ['_mt19937.pyx', 'src/mt19937/mt19937.c', 'src/mt19937/mt19937-jump.c'], [], [npyrandom_lib]],
  ['_philox', ['_philox.pyx', 'src/philox/philox.c'], [], [npyrandom_lib]],
  ['_pcg64', ['_pcg64.pyx', 'src/pcg64/pcg64.c'], ['-U__GNUC_GNU_INLINE__'], [npyrandom_lib]],
  ['_sfc64', ['_sfc64.pyx', 'src/sfc64/sfc64.c'], [], [npyrandom_lib]],
  ['bit_generator', 'bit_generator.pyx', [], [npyrandom_lib]],
  ['_generator', fs.copyfile('_generator.pyx'), [], [npyrandom_lib, npymath_lib]],
  ['mtrand', [fs.copyfile('mtrand.pyx'), 'src/distributions/distributions.c', 'src/legacy/legacy-distributions.c'],
    ['-DNP_RANDOM_LEGACY=1'], [npymath_lib]],
]
```

把这一段读成一张表，依赖关系就一目了然：

| 扩展模块 | 源码 | 额外 c_args | 链接库 |
| --- | --- | --- | --- |
| `_bounded_integers` | `_bounded_integers.pyx`（生成） | — | npyrandom, npymath |
| `_common` | `_common.pyx` | — | npyrandom |
| `_mt19937` | `_mt19937.pyx` + `mt19937.c` + `mt19937-jump.c` | — | npyrandom |
| `_philox` | `_philox.pyx` + `philox.c` | — | npyrandom |
| `_pcg64` | `_pcg64.pyx` + `pcg64.c` | `-U__GNUC_GNU_INLINE__` | npyrandom |
| `_sfc64` | `_sfc64.pyx` + `sfc64.c` | — | npyrandom |
| `bit_generator` | `bit_generator.pyx` | — | npyrandom |
| `_generator` | `_generator.pyx` | — | npyrandom, npymath |
| `mtrand` | `mtrand.pyx` + `distributions.c` + `legacy-distributions.c` | `-DNP_RANDOM_LEGACY=1` | **npymath（不链接 npyrandom）** |

有两个非常值得注意的细节：

1. **生成器包装模块直接把 C 内核和 `.pyx` 一起编译**。例如 `_pcg64` 这一行的源码列表里同时有 `_pcg64.pyx` 和 `src/pcg64/pcg64.c`，二者编进同一个扩展模块。而分布算法则通过链接 `npyrandom` 复用。
2. **遗留模块 `mtrand` 是个例外**：它**不**链接 `npyrandom`，而是把 `src/distributions/distributions.c` 和 `src/legacy/legacy-distributions.c` **直接编译进自己的扩展**，并且用 `-DNP_RANDOM_LEGACY=1` 这个宏启用「遗留模式」。这正是上一讲说的「旧 API 的比特流被冻结、保证跨版本一致」在构建层面的体现——遗留分布走自己的一套，与新 API 的 `npyrandom` 隔离，互不影响。

还有一个 `fs.copyfile` 的小细节值得解释：[`meson.build:71-73`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/meson.build#L71-L73) 注释说明，`_generator.pyx` 和 `mtrand.pyx` 会 `import` 自 `_bounded_integers`，而它的 `.pxd` 只存在于构建目录里，所以需要用 `fs.copyfile` 把这两个 `.pyx` 也拷到构建目录，让 Cython 转译时能找到对应声明。

最后，9 个模块用同一个循环编译：

[`meson.build:82-93`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/meson.build#L82-L93) 对每一项调用 `py.extension_module`，统一带上 `include_directories: 'src'`（所以 C 源码能 `#include "numpy/random/distributions.h"`）、`subdir: 'numpy/random'`（装到正确位置）：

```meson
foreach gen: random_pyx_sources
  py.extension_module(gen[0],
    [gen[1], _cython_tree, _cython_tree_random],
    c_args: [c_args_random, gen[2]],
    include_directories: 'src',
    dependencies: np_core_dep,
    link_with: gen[3],
    install: true,
    subdir: 'numpy/random',
    cython_args: cython_args,
  )
endforeach
```

#### 4.3.4 代码实践

1. **实践目标**：亲手把 `random_pyx_sources` 翻译成依赖关系。
2. **操作步骤**：
   - 打开 `meson.build` 第 61–81 行。
   - 按上表的格式，把每一项拆成「模块名 / 源码 / 链接库」三列，自己写一遍。
   - 特别圈出 `mtrand` 这一行，标注它「不链接 npyrandom、自己编译 distributions.c」。
3. **观察现象**：你会发现生成器包装模块（`_mt19937`/`_philox`/`_pcg64`/`_sfc64`）的结构高度一致，都是「一个 `.pyx` + 一个同名 C 内核」。
4. **预期结果**：得到一张完整的「扩展模块 → 源码 → 静态库」表，作为综合实践的素材。
5. 待本地验证（纯阅读）。

#### 4.3.5 小练习与答案

**练习 1**：`_pcg64` 这一行的第三列是 `['-U__GNUC_GNU_INLINE__']`，它和别的模块不同，这意味着什么？

> **答案**：这是给 C 编译器的一个参数（`-U` 取消定义某个宏），用来取消 `__GNUC_GNU_INLINE__`，调整 `pcg64.c` 里 `inline` 函数的链接语义（GNU inline vs C99 inline），避免重复定义或链接错误。它说明 PCG64 的 C 内核对 inline 语义比较敏感，需要特殊处理。

**练习 2**：为什么 `mtrand` 要单独用 `-DNP_RANDOM_LEGACY=1`？

> **答案**：因为同一份 `distributions.c` 既要服务于新 API（编进 `npyrandom`），又要服务于遗留 API（直接编进 `mtrand`）。这个宏用来在编译时切换某些函数的行为，让 `mtrand` 拿到与历史版本位级别一致的遗留实现，避免新 API 的改进污染旧 API 的冻结承诺。

---

### 4.4 安装规则：哪些文件最终进入 site-packages

#### 4.4.1 概念说明

「编译」和「安装」是两件事。编译产出 `.so` 扩展模块和静态库；安装则决定**最终哪些文件**会被复制进用户的 `site-packages`。`meson.build` 的后半段就是一连串 `py.install_sources(...)`，把 Python 源码、类型存根（`.pyi`）、Cython 声明（`.pxd`）、测试、示例都装出去。

理解安装规则的意义在于：

- `.pyi` 文件让 IDE 能做类型提示。
- `.pxd` 文件让第三方 Cython 扩展能 `cimport numpy.random`。
- `tests/` 和 `tests/data/` 让用户安装后仍可运行随机数回归测试。
- `_examples/` 给出 Cython/CFFI/Numba 三种扩展范式。

#### 4.4.2 核心流程

```
py.install_sources(...)  ← 多组
   ├── Python 源码 + .pyi + .pxd + LICENSE      → numpy/random/
   ├── tests/*.py                               → numpy/random/tests/
   ├── tests/data/*.csv + *.pkl.gz              → numpy/random/tests/data/
   ├── _examples/cffi/*.py                      → numpy/random/_examples/cffi/
   ├── _examples/cython/*.pyx + meson.build     → numpy/random/_examples/cython/
   └── _examples/numba/*.py                     → numpy/random/_examples/numba/
```

加上前两节的两条：扩展模块 `.so` 装到 `numpy/random/`，静态库装到 `numpy/random/lib`，公开头文件 `bitgen.h`/`distributions.h` 装到 `numpy/_core/include/numpy/random/`。这就是完整的安装产物。

#### 4.4.3 源码精读

第一组：Python 源码、类型存根、声明文件、许可证：

[`meson.build:97-119`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/meson.build#L97-L119) 把 `__init__.py`、各 `.pyi`、各 `.pxd`、`_pickle.py`、`LICENSE.md`、`mtrand.pyi` 等装到 `numpy/random`。注意它装的是「源码层」文件，而不是编译产物——`.pxd` 尤其重要，因为它让外部 Cython 代码能 `cimport numpy.random.bit_generator`。

第二组与第三组：测试与测试数据：

[`meson.build:121-137`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/meson.build#L121-L137) 安装所有测试文件，[`meson.build:139-158`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/meson.build#L139-L158) 安装 `tests/data/` 下的 CSV 测试集与历史 pickle 文件。这些 CSV（如 `mt19937-testset-1.csv`、`pcg64-testset-1.csv`）保存了生成器的「逐输出期望值」，用于回归测试——保证生成器的比特流不被意外改变。这是后续「测试体系」讲义的核心素材。

第四、五、六组：扩展示例：

[`meson.build:160-183`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/meson.build#L160-L183) 分别安装 `_examples/cffi`、`_examples/cython`、`_examples/numba` 三个目录的示例。其中 `_examples/cython/meson.build` 自己也是一个独立的小 meson 工程，演示了第三方如何 `find_library('npyrandom')` 并链接它来写自己的采样扩展。

#### 4.4.4 代码实践

1. **实践目标**：区分「源码树里有什么」和「安装后用户能看到什么」。
2. **操作步骤**：
   - 在 `meson.build` 里搜索所有 `py.install_sources`。
   - 列出每个 `install_sources` 调用对应的 `subdir`（安装目标子目录）。
3. **观察现象**：你会发现安装目标被精确分成 `numpy/random`、`numpy/random/tests`、`numpy/random/tests/data`、`numpy/random/_examples/{cffi,cython,numba}`。
4. **预期结果**：写一张「源码文件 → 安装子目录」对照表，确认 `.so`/`.lib` 之外的所有「交付物」。
5. 待本地验证（纯阅读）。

#### 4.4.5 小练习与答案

**练习 1**：为什么要把 `.pxd` 文件也装出去？普通 Python 用户需要它吗？

> **答案**：普通 Python 用户运行时不需要 `.pxd`，但**写 Cython 扩展的第三方开发者**需要它来 `cimport`（例如 `from numpy.random cimport bitgen_t`）。这是 NumPy 把随机数 C 接口开放给外部使用的必要交付物。

**练习 2**：`tests/data/*.csv` 被装出去有什么用？

> **答案**：这些 CSV 是生成器的「标准答案」。安装后，用户可以在自己的环境里运行 `numpy.random.tests`，用这些期望值逐个比对生成器输出，一旦有人改动算法导致比特流变化，测试就会失败。它是「可复现性」的护城河。

---

## 5. 综合实践

**任务**：阅读 `meson.build`，画出一张完整的「**扩展模块 → C 源码 → 静态库**」依赖图，并回答几个追问。

**操作步骤**：

1. 打开 [`meson.build`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/meson.build)。
2. 先读 [`meson.build:3-19`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/meson.build#L3-L19)，确定 `npyrandom` 静态库由哪 5 个 C 文件构成。
3. 再读 [`meson.build:61-81`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/meson.build#L61-L81)，对 9 个扩展模块逐个列出：模块名、直接编译进来的 C 源码、链接的静态库。
4. 画一张图，左列是 9 个扩展模块，中间是 C 源码，右列是 `npyrandom` / `npymath` 两个静态库，用箭头连出依赖。

**参考依赖图（文字版）**：

```
_bounded_integers ──┐  (pyx 来自 .pyx.in 模板展开)
                    ├──→ distributions.c ──→ npyrandom ──┐
_common ────────────┤                                    │
_mt19937 ─(mt19937.c, mt19937-jump.c)─────────────────→ npyrandom
_philox  ─(philox.c)──────────────────────────────────→ npyrandom
_pcg64   ─(pcg64.c)───────────────────────────────────→ npyrandom
_sfc64   ─(sfc64.c)───────────────────────────────────→ npyrandom
bit_generator ────────────────────────────────────────→ npyrandom
_generator ───────────────────────────────────────────→ npyrandom, npymath
mtrand ─(distributions.c, legacy-distributions.c)─→ npymath   ★ 不链接 npyrandom
```

其中 `npyrandom` 自身由 `src/distributions/{distributions,logfactorial,random_mvhg_count,random_mvhg_marginals,random_hypergeometric}.c` 归档而成。

**追问（试着回答）**：

- 为什么 4 个生成器包装模块（`_mt19937`/`_philox`/`_pcg64`/`_sfc64`）都把自己的 C 内核「直接编译」进扩展，而不是也做成静态库？
  > 提示：每个生成器内核只服务于自己一个包装模块，做成静态库没有复用收益；而分布算法要被很多模块共享，所以集中成 `npyrandom`。
- `mtrand` 为什么宁可自己再编译一份 `distributions.c` 也不链接 `npyrandom`？
  > 提示：隔离。遗留 API 要冻结比特流，用 `-DNP_RANDOM_LEGACY=1` 编译出与历史一致的实现，绝不能跟着新 API 的 `npyrandom` 一起被改动。

**预期结果**：一张清晰的依赖图 + 能口头解释上面两个追问。无需真正编译；若想本地验证，可在已构建的 NumPy 里执行 `python -c "import numpy.random._pcg64, numpy.random._generator, numpy.random.mtrand"` 确认这些扩展模块确实被装出来了（待本地验证）。

---

## 6. 本讲小结

- `numpy/random/` 按「语言三层」组织：`.py`/`.pyx`（接口）→ `.pxd`（声明）→ `src/**/*.c`（实现）；并按「生成器分目录」把各生成器内核放在 `src/{mt19937,pcg64,philox,sfc64}/`，分布算法集中放在 `src/distributions/`。
- 构建中枢是 `meson.build`：它先把 `src/distributions/` 下的 5 个 C 文件归档成 **`npyrandom` 静态库**，安装到 `numpy/random/lib`。
- `_bounded_integers.pyx`/`.pxd` 由 `.pyx.in`/`.pxd.in` 经 **Tempita 模板展开**生成，是代码生成的典型例子。
- `random_pyx_sources` 一共声明 **9 个** Cython 扩展模块，每个生成器包装模块都是「一个 `.pyx` + 一个同名 C 内核」，并统一链接 `npyrandom`。
- 遗留模块 `mtrand` 是例外：它**不链接 `npyrandom`**，而是直接编译 `distributions.c` + `legacy-distributions.c`，并用 `-DNP_RANDOM_LEGACY=1` 启用遗留语义——这是旧 API「冻结比特流」承诺在构建层面的实现。
- `c_distributions.pxd` 是 `npyrandom` 库的 Cython 侧「函数清单」，所有分布函数都以 `bitgen_t *` 为第一参数，体现了「分布算法与具体生成器解耦」的契约。

## 7. 下一步学习建议

到这里，你已经掌握了 `numpy.random` 的「骨架」：目录、构建、扩展模块与静态库的依赖关系。接下来建议：

1. 先读 **u2-l1「bitgen_t：连接 C 与 Cython 的核心结构」**，弄清楚 `bitgen_t` 这个结构体（`state` 指针 + 四个 `next_*` 函数指针）为什么是 BitGenerator 和分布层之间的契约——它解释了本讲反复出现的「分布函数只依赖 `bitgen_t *`」的根本原因。
2. 再读 **u2-l2「BitGenerator 基类」** 与 **u2-l3「Generator 包装」**，从 Python/Cython 层看清本讲的扩展模块内部是如何把 C 内核和分布算法粘合起来的。
3. 想提前感受「链接 `npyrandom` 写自己的扩展」可以翻一翻 `_examples/cython/meson.build` 与 `extending.pyx`，那是 u7 单元的内容，但现在扫一眼能加深对本讲「静态库被外部复用」的理解。
