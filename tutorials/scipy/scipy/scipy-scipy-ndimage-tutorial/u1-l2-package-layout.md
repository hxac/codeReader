# 目录结构、文件职责与构建系统

## 1. 本讲目标

本讲承接 [u1-l1 项目定位与能力总览](u1-l1-project-overview.md)，把目光从「这个包能做什么」转向「这些功能在硬盘上长什么样」。

学完本讲你应该能够：

- 打开 `scipy/ndimage/` 目录后，立刻区分五类文件：Python 包装层（`_*.py` 与几个弃用桩文件）、C/C++/Cython 扩展源码（`src/`）、构建脚本（`meson.build`）、测试（`tests/`）、工具（`utils/`）。
- 读得懂 `meson.build`：知道它声明了哪三个「真正干活」的扩展模块（`_nd_image`、`_ni_label`、`_rank_filter_1d`），以及它们各自由哪些源文件编译而来。
- 在 `src/` 目录里把「头文件（`.h`）」与「实现文件（`.c`）」配对，并能说出每个功能域对应的 Python 包装文件与 C 内核文件。
- 看懂 `_nd_image` 这个 C 扩展是如何通过一张 `methods[]` 分发表把 Python 调用路由到 C 函数的。

## 2. 前置知识

### 什么是「扩展模块」（extension module）

NumPy/SciPy 里大部分数值计算跑得快，是因为底层不是纯 Python，而是用 C/C++/Cython 写的、编译成机器码的动态库。在 Windows 上是 `.pyd`、在 Linux/macOS 上是 `.so`。Python 通过 `import` 把这个动态库当成一个普通模块来用，这类模块就叫**扩展模块**。

`scipy.ndimage` 的速度来自三个这样的扩展模块：

- `_nd_image`：纯 C，绝大多数滤波/插值/形态学/测量的内核都在这里。
- `_ni_label`：Cython，专门做连通区域标记。
- `_rank_filter_1d`：C++，专门做一维滑动窗口秩滤波。

注意名字都带一个前导下划线 `_`，表示它们是**私有内部模块**，使用者不应直接 `import` 它们，而应通过公开命名空间 `scipy.ndimage.xxx` 调用。

### 什么是 Meson 构建系统

[Meson](https://mesonbuild.com/) 是 SciPy 当前使用的构建系统，配置写在 `meson.build` 文件里。它的作用相当于「告诉编译器：把这些 `.c`/`.cpp`/`.pyx` 文件编译成一个 `.so`，再装到 Python 包目录的指定位置」。本讲你只需要看懂 `meson.build` 里几个关键函数名：

- `py3.extension_module(name, sources, ...)`：定义一个扩展模块，给出名字和源文件清单。
- `py3.install_sources(list, subdir: ...)`：把一批纯 Python 文件安装到包目录。
- `subdir('tests')`：进入子目录继续读取那里的 `meson.build`。

### CPython 扩展的最小骨架

一个 C 扩展要能被 `import`，至少要有两样东西：

1. 一个 `PyMethodDef` 数组（在本包里叫 `methods[]`）：这是一张「Python 名 → C 函数指针」的分发表。
2. 一个 `PyInit_<模块名>` 函数：Python 在 `import` 时调用它来初始化模块。

记住这两点，第 4.5 节看 `nd_image.c` 就不费劲了。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [meson.build](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/meson.build) | 构建脚本 | 三个扩展模块的定义、`python_sources` 安装清单 |
| [__init__.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/__init__.py) | 公开命名空间入口 | 如何装配公开 API、弃用子模块 |
| [src/nd_image.c](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c) | `_nd_image` 扩展主文件 | `methods[]` 分发表、`PyInit__nd_image`、`Py_Correlate1D` 包装函数 |
| [src/nd_image.h](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.h) | `_nd_image` 的头文件 | 定义 NumPy 唯一符号 |

辅助参考（用于建立目录全景，不在本讲精读）：

- `src/` 下其余 `ni_*.c/.h`：各功能域的 C 内核。
- `src/_ni_label.pyx`、`src/_rank_filter_1d.cpp`：另外两个扩展的源码。
- `tests/`、`utils/`：测试与工具。

## 4. 核心概念与源码讲解

### 4.1 整体目录布局：五类文件各司其职

#### 4.1.1 概念说明

`scipy/ndimage/` 目录被刻意组织成「薄 Python 层 + 厚 C 层」的结构。从使用者的角度看，调用的是 `scipy.ndimage.gaussian_filter`；但从源码角度看，这个调用要穿过好几层文件才到达真正干活的 C 代码。理解目录布局，就是建立一张「文件 → 职责」的速查表。

#### 4.1.2 目录树

```text
scipy/ndimage/
├── __init__.py                      # 公开命名空间入口（装配公开 API）
├── _ndimage_api.py                  # 私有：聚合五个实现模块的裸 API
├── _support_alternative_backends.py # 私有：CuPy/JAX 后端委托层
├── _delegators.py                   # 私有：后端委托的签名/delegator
├── _filters.py                      # 私有：Filters 功能域 Python 包装
├── _fourier.py                      # 私有：Fourier 功能域 Python 包装
├── _interpolation.py                # 私有：Interpolation 功能域 Python 包装
├── _measurements.py                 # 私有：Measurements 功能域 Python 包装
├── _morphology.py                   # 私有：Morphology 功能域 Python 包装
├── _ni_support.py                   # 私有：跨功能域共享工具（边界模式/输出/axes）
├── _ni_docstrings.py                # 私有：共享文档字符串模板
├── filters.py / fourier.py ...      # 弃用桩：旧子模块命名空间（v2.0.0 移除）
├── meson.build                      # 构建脚本
├── src/                             # C/C++/Cython 扩展源码
│   ├── nd_image.c / nd_image.h      # _nd_image 扩展主文件 + 头
│   ├── ni_*.c / ni_*.h              # 各功能域 C 内核 + 头
│   ├── _ni_label.pyx                # _ni_label 扩展（Cython）
│   └── _rank_filter_1d.cpp          # _rank_filter_1d 扩展（C++）
├── tests/                           # 测试套件（按功能域分文件）
└── utils/                           # 工具脚本（生成测试向量等）
```

读这张表要抓住一条主线：**前导下划线 `_` 开头的文件都是内部实现，没有下划线且和功能域同名的文件（`filters.py` 等）是即将移除的旧入口**。

#### 4.1.3 核心流程

使用者的一次调用，文件层面的穿透顺序大致是：

```text
scipy.ndimage.gaussian_filter          # __init__.py 暴露的名字
  → _support_alternative_backends      # 后端委托层（u1-l3、u7-l1 详讲）
  → _ndimage_api / _filters            # 裸 API + 功能域包装（校验参数）
  → _nd_image.correlate1d (C)          # 编译扩展，真正算数
  → ni_filters.c / ni_support.c        # C 内核
```

本讲不展开每一层（那是后续讲义的任务），重点落在两端：**顶层的目录与 `__init__.py`**、**底层的 `src/` 与 `meson.build`**。

### 4.2 Python 包装层：python_sources 安装清单

#### 4.2.1 概念说明

扩展模块（`.so`）只是「算得快的内核」，它本身不知道 `gaussian_filter` 的默认参数、不知道怎么校验 `sigma`、不知道怎么写文档字符串。这些「人情味」的工作由纯 Python 文件承担。Meson 需要一份清单，告诉它「这些 `.py` 文件也要安装到 `scipy/ndimage/` 目录下」。这份清单就是 `python_sources`。

#### 4.2.2 源码精读

[meson.build:56-78](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/meson.build#L56-L78) 定义了 `python_sources` 并把它们安装到包目录：

```python
python_sources = [
  '__init__.py',
  '_ndimage_api.py',
  '_delegators.py',
  '_support_alternative_backends.py',
  '_filters.py',
  '_fourier.py',
  '_interpolation.py',
  '_measurements.py',
  '_morphology.py',
  '_ni_docstrings.py',
  '_ni_support.py',
  'filters.py',
  'fourier.py',
  'interpolation.py',
  'measurements.py',
  'morphology.py'
]

py3.install_sources(
  python_sources,
  subdir: 'scipy/ndimage'
)
```

这份清单可以分成三组来看：

| 分组 | 文件 | 角色 |
| --- | --- | --- |
| 入口 | `__init__.py` | 公开命名空间 |
| 私有实现 | `_filters.py`、`_fourier.py`、`_interpolation.py`、`_measurements.py`、`_morphology.py` | 五大功能域的 Python 包装 |
| 私有支撑 | `_ni_support.py`、`_ni_docstrings.py`、`_ndimage_api.py`、`_support_alternative_backends.py`、`_delegators.py` | 共享工具、API 聚合、后端委托 |
| 弃用桩 | `filters.py`、`fourier.py`、`interpolation.py`、`measurements.py`、`morphology.py` | 旧子模块入口，v2.0.0 移除 |

注意：源码里有 5 个功能域实现文件（`_*.py`）和 5 个同名无下划线的弃用桩文件一一对应。弃用桩文件非常小（`filters.py` 只有 28 行），它的全部职责是：当有人写 `from scipy.ndimage.filters import gaussian_filter` 这种**旧式导入**时，触发一个 `DeprecationWarning` 并把调用转发到新位置。例如 [filters.py:24-27](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/filters.py#L24-L27)：

```python
def __getattr__(name):
    return _sub_module_deprecation(sub_package='ndimage', module='filters',
                                   private_modules=['_filters'], all=__all__,
                                   attribute=name)
```

> 结论：**新代码只从 `scipy.ndimage` 顶层导入**，例如 `from scipy.ndimage import gaussian_filter`。带下划线的私有文件和同名无下划线的旧桩都不要直接 import。

#### 4.2.3 代码实践

**目标**：亲手验证「顶层导入」与「旧子模块导入」的区别。

**步骤**：

1. 在装好 SciPy 的环境里运行下面脚本。
2. 观察两次导入分别打印什么。

```python
# 示例代码
import warnings
import scipy.ndimage as ndi

# (A) 正确方式：从顶层命名空间导入
print("顶层导入的函数对象:", ndi.gaussian_filter)
print("所在模块:", ndi.gaussian_filter.__module__)

# (B) 旧式导入：应触发 DeprecationWarning
warnings.simplefilter("always")
try:
    from scipy.ndimage.filters import gaussian_filter as old_gf
    print("旧式导入得到的对象:", old_gf)
except Exception as e:
    print("旧式导入抛出:", type(e).__name__, e)
```

**预期结果**：

- (A) 中 `ndi.gaussian_filter.__module__` 一般形如 `scipy.ndimage._filters`（或经后端委托包装后的命名空间，受 `SCIPY_ARRAY_API` 环境变量影响）。
- (B) 会打印一条 `DeprecationWarning`，说明 `scipy.ndimage.filters` 子模块已弃用。

> 关于 `__module__` 的确切取值在不同 SciPy 版本/环境变量下可能不同，若与上述不符，以你本地输出为准（待本地验证）。

#### 4.2.4 小练习与答案

**练习 1**：`python_sources` 里 `_filters.py` 和 `filters.py` 各承担什么职责？为什么两者都要被安装？

**参考答案**：`_filters.py` 是 Filters 功能域的**真正实现**（参数校验、调用 C 内核），是私有模块；`filters.py` 是**弃用桩**，仅用于兼容旧的 `scipy.ndimage.filters` 导入路径并发出弃用警告。两者都要安装，因为旧代码可能仍在用旧路径，移除桩会直接破坏这些代码——计划在 v2.0.0 才彻底删掉桩文件。

**练习 2**：`_ni_support.py` 为什么被列在 `python_sources` 里、却不在五个功能域文件之中？

**参考答案**：因为它提供**跨功能域共享**的工具（边界模式编码 `_extend_mode_to_code`、输出数组获取 `_get_output`、序列归一化 `_normalize_sequence` 等），被 `_filters`/`_interpolation`/`_morphology` 等多个功能域 import。它是支撑层而非某个功能域本身，详见 [u1-l4 共享支撑工具](u1-l4-shared-support-helpers.md)。

### 4.3 构建系统 meson.build 与三个扩展模块

#### 4.3.1 概念说明

`meson.build` 顶部的三个 `py3.extension_module(...)` 调用定义了 `scipy.ndimage` 全部性能来源。理解它们，就理解了「这个包的算力是怎么被组装出来的」。注意还有两个扩展 `_ctest`、`_cytest` 带了 `install_tag: 'tests'`，它们是测试用的样板扩展，不在本讲重点关注范围。

#### 4.3.2 三个「生产级」扩展对照表

| 扩展模块 | 语言 | 源文件 | 用途 |
| --- | --- | --- | --- |
| `_nd_image` | C | 8 个 `src/*.c` | 绝大多数滤波/插值/形态学/测量/傅里叶的内核 |
| `_ni_label` | Cython | `src/_ni_label.pyx` | 连通区域标记 |
| `_rank_filter_1d` | C++ | `src/_rank_filter_1d.cpp` | 一维滑动窗口秩滤波 |

#### 4.3.3 源码精读：_nd_image（纯 C 扩展，8 个源文件）

[meson.build:1-17](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/meson.build#L1-L17) 定义了 `_nd_image`：

```python
py3.extension_module('_nd_image',
  [
    'src/nd_image.c',
    'src/ni_filters.c',
    'src/ni_fourier.c',
    'src/ni_interpolation.c',
    'src/ni_measure.c',
    'src/ni_morphology.c',
    'src/ni_splines.c',
    'src/ni_support.c'
  ],
  include_directories: ['../_build_utils/src'],
  dependencies: [np_dep, ccallback_dep],
  link_args: version_link_args,
  install: true,
  subdir: 'scipy/ndimage'
)
```

逐项说明：

- 第一个参数 `'_nd_image'` 是模块名，最终生成 `_nd_image.cpython-3xx-<platform>.so`。
- 第二个参数是源文件列表。注意 `nd_image.c` 是「入口/胶水」，其余 `ni_*.c` 是各功能域的算法实现，一起编译进**同一个** `.so`。
- `include_directories: ['../_build_utils/src']`：引入 SciPy 构建工具的头文件（如 `ccallback` 支持的低层回调机制）。
- `dependencies: [np_dep, ccallback_dep]`：依赖 NumPy 的 C API 和 ccallback 库（`generic_filter` 的低层回调要用）。
- `subdir: 'scipy/ndimage'`：把编译产物安装到 `scipy/ndimage/` 目录，这样 `import scipy.ndimage._nd_image` 才能找到它。

#### 4.3.4 源码精读：_ni_label（Cython 扩展）

[meson.build:19-26](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/meson.build#L19-L26)：

```python
py3.extension_module('_ni_label',
  cython_gen.process('src/_ni_label.pyx'),
  c_args: cython_c_args,
  dependencies: np_dep,
  link_args: version_link_args,
  install: true,
  subdir: 'scipy/ndimage'
)
```

关键差异：源文件不是直接给 `.c`，而是 `cython_gen.process('src/_ni_label.pyx')`。Cython 源文件 `.pyx` 会先被「翻译」成 C 代码，再编译。`c_args: cython_c_args` 是为这种自动生成的 C 代码准备的编译选项。为什么连通区域标记要单独用 Cython 而不是塞进 `_nd_image`？因为它的逐行扫描 + 等价类合并逻辑用 Cython 的 fused type 写起来更简洁（详见 [u6-l4](u6-l4-cython-label-rank-filter.md)）。

#### 4.3.5 源码精读：_rank_filter_1d（C++ 扩展）

[meson.build:28-34](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/meson.build#L28-L34)：

```python
py3.extension_module('_rank_filter_1d',
  'src/_rank_filter_1d.cpp',
  link_args: version_link_args,
  install: true,
  dependencies: np_dep,
  subdir: 'scipy/ndimage'
)
```

这里源文件后缀是 `.cpp`，Meson 会自动用 C++ 编译器编译。这个扩展专门服务 `rank_filter` / `median_filter` / `percentile_filter` 的一维滑动窗口内核。

#### 4.3.6 代码实践

**目标**：把 `meson.build` 里三个扩展模块的「源文件来源」整理成一张表。

**步骤**：

1. 重新读一遍 [meson.build:1-34](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/meson.build#L1-L34)。
2. 填写下面这张「扩展 → 源文件」对照表（答案见本节末）。

| 扩展模块 | 语言 | 源文件 |
| --- | --- | --- |
| `_nd_image` | C | ? |
| `_ni_label` | Cython | ? |
| `_rank_filter_1d` | C++ | ? |

**预期结果**：

| 扩展模块 | 语言 | 源文件 |
| --- | --- | --- |
| `_nd_image` | C | `nd_image.c` + `ni_filters.c` + `ni_fourier.c` + `ni_interpolation.c` + `ni_measure.c` + `ni_morphology.c` + `ni_splines.c` + `ni_support.c`（共 8 个） |
| `_ni_label` | Cython | `_ni_label.pyx`（经 `cython_gen.process` 翻译成 C 后编译） |
| `_rank_filter_1d` | C++ | `_rank_filter_1d.cpp` |

### 4.4 src/ 目录的 C 头文件与实现文件对应关系

#### 4.4.1 概念说明

C 程序习惯把「声明」放在头文件 `.h`、把「实现」放在 `.c`。`scipy.ndimage` 的 `src/` 严格遵循「一个内核模块一对 `.h`/`.c`」的约定。掌握了这张对应表，你就能在阅读某个功能域源码时迅速定位「函数声明在哪、实现在哪」。

#### 4.4.2 头/实现对应表

| 头文件 `.h` | 实现文件 `.c` | 对应功能域 | Python 包装 |
| --- | --- | --- | --- |
| `nd_image.h` | `nd_image.c` | 扩展入口 + 胶水（`methods[]`、包装函数） | （无，是 C 扩展本体） |
| `ni_support.h` | `ni_support.c` | C 端通用抽象（迭代器、行缓冲、边界扩展） | `_ni_support.py`（Python 端对应） |
| `ni_filters.h` | `ni_filters.c` | 滤波内核 | `_filters.py` |
| `ni_fourier.h` | `ni_fourier.c` | 傅里叶滤波内核 | `_fourier.py` |
| `ni_interpolation.h` | `ni_interpolation.c` | 插值/几何变换内核 | `_interpolation.py` |
| `ni_splines.h` | `ni_splines.c` | 样条系数计算内核 | `_interpolation.py`（样条预滤波） |
| `ni_measure.h` | `ni_measure.c` | 测量/统计内核 | `_measurements.py` |
| `ni_morphology.h` | `ni_morphology.c` | 形态学内核 | `_morphology.py` |

加上两个独立扩展：

| 扩展源文件 | 语言 | 用途 |
| --- | --- | --- |
| `_ni_label.pyx` | Cython | 连通区域标记（无独立 `.h`，自包含） |
| `_rank_filter_1d.cpp` | C++ | 一维秩滤波（无独立 `.h`，自包含） |

> 命名规律：`ni_` 前缀 = "ndimage implementation"，是 C 内核的统一前缀；`nd_image.c` 不带 `ni_` 是因为它代表扩展模块本身。

#### 4.4.3 源码精读：头文件如何被串联

[nd_image.c:36-44](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L36-L44) 在文件开头一次性 include 了所有功能域头文件：

```c
#include "nd_image.h"
#include "ni_support.h"

#include "ni_filters.h"
#include "ni_fourier.h"
#include "ni_morphology.h"
#include "ni_interpolation.h"
#include "ni_measure.h"

#include "ccallback.h"
```

这段代码揭示了 `_nd_image` 扩展的「胶水」本质：`nd_image.c` 自己不实现算法，而是把参数从 Python 解析出来，再调用 `ni_*.h` 里声明的 `NI_Correlate1D`、`NI_GeometricTransform` 等内核函数。`ni_splines.h` 没在这里直接 include，是因为它被 `ni_interpolation.c`/`ni_filters.c` 内部使用（见 4.4.2 表）。

另外，[nd_image.h:37-38](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.h#L37-L38) 定义了 NumPy 的唯一符号：

```c
#define PY_ARRAY_UNIQUE_SYMBOL _scipy_ndimage_ARRAY_API
#include <numpy/arrayobject.h>
```

`PY_ARRAY_UNIQUE_SYMBOL` 保证整个 `_nd_image` 扩展只导入 NumPy C API 一次，避免在嵌入多解释器或静态链接时出现符号冲突。

#### 4.4.4 小练习与答案

**练习 1**：我想阅读「高斯滤波」的 C 实现，应该打开哪个文件？

**参考答案**：高斯滤波属于 Filters 功能域。先看 Python 包装 `_filters.py`（`gaussian_filter` / `_gaussian_kernel1d`），它最终调用 `_nd_image.correlate1d`；C 端真正的相关运算在 `ni_filters.c`（声明在 `ni_filters.h`）。注意：高斯核的**生成**在 Python 侧，C 侧只负责「拿着核做相关」。

**练习 2**：为什么 `ni_support.c` 被编进 `_nd_image`，而 `_ni_label.pyx` 却独立成一个扩展？

**参考答案**：`ni_support.c` 提供的是被几乎所有 `ni_*.c` 内核依赖的通用抽象（迭代器、行缓冲、边界扩展），必须和它们编在同一个扩展里才能直接调用；而 `_ni_label.pyx` 是 Cython 源、有独立的 fused-type 特化与逐行扫描逻辑，且标记算法相对独立，所以单独编译成 `_ni_label` 扩展。

### 4.5 _nd_image 模块的方法分发表与入口

#### 4.5.1 概念说明

第 2 节说过，一个 C 扩展需要一张「Python 名 → C 函数」的分发表和 `PyInit_<名字>` 初始化函数。`_nd_image` 用一张 `methods[]` 数组集中登记所有可被 Python 调用的方法。这张表是「Python 侧函数名」与「C 侧函数」的**单一事实来源**——`meson.build` 里没有列这些方法名，它们只存在于 `nd_image.c` 里。

#### 4.5.2 核心流程

```text
Python:  ndi._nd_image.correlate1d(input, weights, axis, output, mode, cval, origin)
              │
              ▼  查 methods[] 表
C:       {"correlate1d", Py_Correlate1D}  →  Py_Correlate1D(obj, args)
              │  用 PyArg_ParseTuple 把 args 拆开
              ▼
C:       NI_Correlate1D(input, weights, axis, output, mode, cval, origin)
              │  （定义在 ni_filters.c，真正算数）
              ▼
         返回结果
```

注意：日常使用时你**不会**直接调用 `_nd_image.correlate1d`，而是经过 `_filters.py` 的 `correlate1d`，后者负责参数校验和边界模式编码后，再调用这个 C 方法。这里展示的是「最底层」的入口。

#### 4.5.3 源码精读：methods[] 分发表

[nd_image.c:1325-1348](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L1325-L1348) 是完整的分发表：

```c
static PyMethodDef methods[] = {
    {"correlate1d",                 (PyCFunction)Py_Correlate1D,                 METH_VARARGS, NULL},
    {"correlate",                   (PyCFunction)Py_Correlate,                   METH_VARARGS, NULL},
    {"uniform_filter1d",            (PyCFunction)Py_UniformFilter1D,             METH_VARARGS, NULL},
    {"min_or_max_filter1d",         (PyCFunction)Py_MinOrMaxFilter1D,            METH_VARARGS, NULL},
    {"min_or_max_filter",           (PyCFunction)Py_MinOrMaxFilter,              METH_VARARGS, NULL},
    {"rank_filter",                 (PyCFunction)Py_RankFilter,                  METH_VARARGS, NULL},
    {"generic_filter",              (PyCFunction)Py_GenericFilter,               METH_VARARGS, NULL},
    {"generic_filter1d",            (PyCFunction)Py_GenericFilter1D,             METH_VARARGS, NULL},
    {"fourier_filter",              (PyCFunction)Py_FourierFilter,               METH_VARARGS, NULL},
    {"fourier_shift",               (PyCFunction)Py_FourierShift,                METH_VARARGS, NULL},
    {"spline_filter1d",             (PyCFunction)Py_SplineFilter1D,              METH_VARARGS, NULL},
    {"geometric_transform",         (PyCFunction)Py_GeometricTransform,          METH_VARARGS, NULL},
    {"zoom_shift",                  (PyCFunction)Py_ZoomShift,                   METH_VARARGS, NULL},
    {"find_objects",                (PyCFunction)Py_FindObjects,                 METH_VARARGS, NULL},
    {"value_indices",               (PyCFunction)NI_ValueIndices,                METH_VARARGS, NULL},
    {"watershed_ift",               (PyCFunction)Py_WatershedIFT,                METH_VARARGS, NULL},
    {"distance_transform_bf",       (PyCFunction)Py_DistanceTransformBruteForce, METH_VARARGS, NULL},
    {"distance_transform_op",       (PyCFunction)Py_DistanceTransformOnePass,    METH_VARARGS, NULL},
    {"euclidean_feature_transform", (PyCFunction)Py_EuclideanFeatureTransform,   METH_VARARGS, NULL},
    {"binary_erosion",              (PyCFunction)Py_BinaryErosion,               METH_VARARGS, NULL},
    {"binary_erosion2",             (PyCFunction)Py_BinaryErosion2,              METH_VARARGS, NULL},
    {NULL,                          NULL,                                        0,            NULL}
};
```

读这张表的几个要点：

- 每一行 `{字符串名, C 函数指针, 调用约定, 文档}` 就是「Python 看到的名字 → C 函数」的一条映射。
- `METH_VARARGS` 表示该 C 函数接收的是位置参数元组（要用 `PyArg_ParseTuple` 解析）。
- 最后一行 `{NULL, NULL, 0, NULL}` 是结束哨兵，必须有。
- 注意有些 C 方法名与公开 API 名**不同**：例如公开的 `fourier_gaussian`/`fourier_uniform`/`fourier_ellipsoid` 在 C 端共用同一个 `fourier_filter`（由内部参数区分），`affine_transform`/`map_coordinates`/`shift`/`zoom`/`rotate` 共用 `zoom_shift` 或 `geometric_transform`。这说明 C 层比 Python 层更「聚合」。

#### 4.5.4 源码精读：一个包装函数 Py_Correlate1D

[nd_image.c:177-200](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L177-L200) 是 `correlate1d` 的 C 包装函数，是理解「胶水层」的最佳样本：

```c
static PyObject *Py_Correlate1D(PyObject *obj, PyObject *args)
{
    PyArrayObject *input = NULL, *output = NULL, *weights = NULL;
    int axis, mode;
    double cval;
    npy_intp origin;

    if (!PyArg_ParseTuple(args, "O&O&iO&idn" ,
                          NI_ObjectToInputArray, &input,
                          NI_ObjectToInputArray, &weights, &axis,
                          NI_ObjectToOutputArray, &output, &mode, &cval,
                          &origin))
        goto exit;

    NI_Correlate1D(input, weights, axis, output, (NI_ExtendMode)mode, cval,
                   origin);
    PyArray_ResolveWritebackIfCopy(output);

exit:
    Py_XDECREF(input);
    Py_XDECREF(weights);
    Py_XDECREF(output);
    return PyErr_Occurred() ? NULL : Py_BuildValue("");
}
```

这段代码体现了 C 扩展包装函数的标准三段式：

1. **解析参数**：`PyArg_ParseTuple` 用格式串 `"O&O&iO&idn"` 把 Python 元组拆成 C 变量。其中 `O&` 表示「用一个自定义转换函数把对象转成数组」（`NI_ObjectToInputArray`/`NI_ObjectToOutputArray`），`i` 是 int（axis、mode），`d` 是 double（cval），`n` 是 `npy_intp`（origin）。
2. **调用内核**：`NI_Correlate1D(...)` 是 `ni_filters.c` 里真正干活的函数，`mode` 被强转为 `NI_ExtendMode` 枚举（这就是 Python 侧 `_extend_mode_to_code` 把 mode 字符串编成整数码的对应物）。
3. **清理并返回**：`Py_XDECREF` 释放引用计数；`PyErr_Occurred() ? NULL : Py_BuildValue("")` 表示「出错返回 NULL（抛异常），否则返回 None」。

#### 4.5.5 源码精读：模块定义与初始化

[nd_image.c:1372-1385](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L1372-L1385) 定义模块并暴露初始化函数：

```c
static struct PyModuleDef moduledef = {
    .m_base = PyModuleDef_HEAD_INIT,
    .m_name = "_nd_image",
    .m_size = 0,
    .m_methods = methods,
    .m_slots = _nd_image_slots,
};


PyMODINIT_FUNC
PyInit__nd_image(void)
{
    return PyModuleDef_Init(&moduledef);
}
```

- `.m_methods = methods` 把 4.5.3 的分发表挂到模块上。
- `.m_name = "_nd_image"` 必须与扩展名、与 `meson.build` 第一个参数一致。
- `PyInit__nd_image`（注意双下划线：`PyInit_` + `_nd_image`）是 Python `import` 时的入口。`.m_slots` 用的是「多阶段初始化」（multi-interpreter / per-interpreter GIL 支持），真正的初始化逻辑在 `_nd_image_module_exec` 里调用 `_import_array()` 引入 NumPy C API。

#### 4.5.6 代码实践

**目标**：建立「公开 API 名 ↔ C 方法名」的映射意识，体会 C 层比 Python 层更「聚合」。

**步骤**：

1. 打开 [nd_image.c:1325-1348](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L1325-L1348) 的 `methods[]`。
2. 对照 `__init__.py` 里 [Filters autosummary 列表](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/__init__.py#L15-L43)（约 26 个公开 Filters 函数）。
3. 回答：为什么公开的 Filters 函数有 20 多个，而 `methods[]` 里 Filter 相关的 C 方法只有 7 个左右？

**需要观察的现象**：

- 公开函数里 `gaussian_filter`、`uniform_filter`、`prewitt`、`sobel`、`laplace` 等**都不在** `methods[]` 里。
- `methods[]` 里只有 `correlate1d`、`correlate`、`uniform_filter1d`、`min_or_max_filter*`、`rank_filter`、`generic_filter*` 这几条。

**预期结果**：高阶滤波（高斯、Sobel、Prewitt、Laplace…）在 Python 层（`_filters.py`）被实现为「构造一个一维核 → 沿各轴调用 `correlate1d`」的组合，所以它们复用同一个 C 方法 `correlate1d`。这就是 C 层「更聚合」的原因：C 只提供少数通用原语，组合的多样性留给 Python。

#### 4.5.7 小练习与答案

**练习 1**：`PyInit__nd_image` 这个函数名为什么有两个连续的下划线？

**参考答案**：CPython 规定扩展模块的初始化函数必须叫 `PyInit_<模块名>`。这里模块名是 `_nd_image`（本身带一个下划线前缀），于是拼起来就是 `PyInit_` + `_nd_image` = `PyInit__nd_image`，中间出现双下划线。如果名字对不上，`import` 会失败。

**练习 2**：在 `methods[]` 中找出「一个 C 方法服务多个公开函数」的两个例子。

**参考答案**：

- `fourier_filter`（`Py_FourierFilter`）一个 C 方法服务公开的 `fourier_gaussian`、`fourier_uniform`、`fourier_ellipsoid` 三个函数，靠传入的参数区分核类型。
- `zoom_shift`（`Py_ZoomShift`）和 `geometric_transform`（`Py_GeometricTransform`）服务 `affine_transform`、`map_coordinates`、`shift`、`zoom`、`rotate` 等多个插值类公开函数。

**练习 3**：`Py_Correlate1D` 里 `mode` 参数是 `int`，但 Python 侧用户传的是字符串如 `"reflect"`。这个转换在哪一层完成？

**参考答案**：在 Python 包装层 `_filters.py` 里，调用 C 内核前会用 `_ni_support._extend_mode_to_code(mode, ...)` 把字符串编成整数码，再把整数传给 `_nd_image.correlate1d`。C 端再把这个 int 强转为 `NI_ExtendMode` 枚举。所以用户面对字符串、C 面对整数，Python 包装层是翻译者（详见 [u1-l4](u1-l4-shared-support-helpers.md)）。

## 5. 综合实践

**任务**：在本地构建好的 SciPy 安装目录里找到三个扩展的 `.so` 文件，并把它们与 `meson.build` 声明的源文件一一对应起来，画出「编译产物 ← 源文件」溯源图。

**步骤**：

1. 运行下面脚本，定位三个扩展模块在磁盘上的位置。

```python
# 示例代码
import scipy.ndimage as ndi
import os

for ext_name in ["_nd_image", "_ni_label", "_rank_filter_1d"]:
    mod = getattr(ndi, ext_name)          # 取到已编译的扩展模块对象
    path = getattr(mod, "__file__", None)
    print(f"{ext_name:18s} -> {path}")
    if path:
        print(f"   目录: {os.path.dirname(path)}")
        print(f"   文件大小: {os.path.getsize(path)} bytes")
```

2. `cd` 到脚本打印出的目录（通常形如 `.../site-packages/scipy/ndimage/`），用 `ls` 查看：

```bash
ls -la <打印出的目录> | grep -E '_nd_image|_ni_label|_rank_filter_1d'
```

   你应当看到形如 `_nd_image.cpython-3xx-x86_64-linux-gnu.so` 的文件（具体平台后缀因机器而异）。

3. 对每个 `.so`，依据 [meson.build:1-34](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/meson.build#L1-L34) 写出它由哪些 `src/*.c`/`.pyx`/`.cpp` 编译而来。

4. 用一个符号检查命令验证 `_nd_image` 确实包含了多个 C 文件的符号（可选）：

```bash
# 示例命令（Linux/macOS）
nm -D <...>/_nd_image.*.so | grep -i -E 'NI_Correlate1D|NI_GeometricTransform|NI_BinaryErosion' | head
```

   预期能看到来自 `ni_filters.c`、`ni_interpolation.c`、`ni_morphology.c` 的内核符号都出现在同一个 `.so` 里——这就是 4.3.3 所说「8 个源文件编译进同一个扩展」的证据。

**预期结果（溯源图）**：

```text
_nd_image.*.so        ← nd_image.c (入口/methods[])
                       + ni_support.c   (迭代器/行缓冲/边界)
                       + ni_filters.c   (滤波内核)
                       + ni_fourier.c   (傅里叶内核)
                       + ni_interpolation.c (插值/几何)
                       + ni_splines.c   (样条系数)
                       + ni_measure.c   (测量/统计)
                       + ni_morphology.c (形态学)

_ni_label.*.so        ← _ni_label.pyx (Cython 翻译后编译)

_rank_filter_1d.*.so  ← _rank_filter_1d.cpp (C++)
```

> 若你的环境没有可写的本地构建，符号检查那一步可能受限；此时以「源码阅读 + 文件存在性检查」为准（待本地验证）。

## 6. 本讲小结

- `scipy/ndimage/` 目录是「薄 Python 层 + 厚 C 层」结构：`_*.py` 是私有包装与支撑，`filters.py` 等无下划线同名文件是 v2.0.0 待移除的弃用桩。
- `meson.build` 顶部定义了三个生产级扩展：`_nd_image`（8 个 C 文件）、`_ni_label`（1 个 Cython）、`_rank_filter_1d`（1 个 C++）；另有 `_ctest`/`_cytest` 两个测试用样板扩展。
- `python_sources` 列出了需要安装的纯 Python 文件，分入口、私有实现、私有支撑、弃用桩四组。
- `src/` 严格遵循「一个内核模块一对 `.h`/`.c`」：`ni_*.h` 声明、`ni_*.c` 实现，`nd_image.c` 是把它们粘起来的入口。
- `_nd_image` 用一张 `methods[]` 分发表把 Python 名映射到 C 包装函数（如 `Py_Correlate1D`），包装函数三段式：解析参数 → 调 `NI_*` 内核 → 清理返回。
- C 层比 Python 公开层更「聚合」：少数 C 原语（`correlate1d`、`fourier_filter`、`zoom_shift`…）支撑了数十个公开函数。

## 7. 下一步学习建议

- 下一讲 [u1-l3 四层架构与公开 API 装配链](u1-l3-architecture-layers.md) 会把本讲的「文件平面图」升级为「调用纵深图」，讲清 `_ndimage_api → _support_alternative_backends → __init__` 的装配顺序。
- 想深入共享工具（边界模式编码、输出数组、axes 归一化），读 [u1-l4 共享支撑工具](u1-l4-shared-support-helpers.md) 与 [_ni_support.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_ni_support.py)。
- 想理解 C 扩展内部机制（迭代器、行缓冲、边界扩展、内核循环），进入单元 6，先读 [u6-l1 _nd_image 扩展模块与方法分发表](u6-l1-nd-image-c-extension.md) 与 [u6-l2 C 端迭代器、行缓冲与边界扩展](u6-l2-c-iterators-line-buffers.md)。
