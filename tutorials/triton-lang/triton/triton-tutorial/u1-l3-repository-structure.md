# 仓库与目录结构地图

> 本讲属于「Unit 1 项目概览与上手」，承接 [u1-l1](u1-l1-project-overview.md)（Triton 是什么）与 [u1-l2](u1-l2-build-install-test.md)（构建安装）。

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 Triton 仓库顶层每个目录（`python/`、`lib/`、`include/`、`third_party/`、`bin/`、`test/`、`unittest/` 等）分别放哪一类代码。
- 把仓库里的代码准确地归到三层：**Python 前端层**、**C++/MLIR 编译器层**、**硬件后端层**，并能在每一层里定位关键入口文件。
- 理解「一条 kernel 的编译主线」在仓库目录里是如何流动的：从 `python/triton` 出发，经过 `lib/Dialect`、`lib/Conversion`，最终落到 `third_party/{nvidia,amd}/backend`。
- 区分 Triton 的两套测试体系：`test/` 下的 **lit 测试**（验证 MLIR pass 变换，无需 GPU）与 `python/test/` 下的 **pytest 测试**（端到端验证，多需 GPU）。

本讲**只建立地图，不深入实现**。后续每个进阶讲义都会回到这张地图上的某个坐标。

## 2. 前置知识

本讲是纯「认路」讲义，不需要你写过 kernel。但你最好已经具备下面这些来自前两讲的概念：

- **三层技术栈**：Triton = Python 前端 + C++/MLIR 编译器 + 硬件后端。
- **编译主线**：`AST → TTIR → TTGIR → LLIR → PTX/amdgcn → cubin/hsaco`。其中 **TTIR** 是逻辑层 IR（不关心硬件），**TTGIR** 是带 GPU 张量布局的物理层 IR，二者都是基于 MLIR 自定义的方言（dialect）。
- **MLIR**：一种用来「定义 IR 方言」的框架。Triton 在它之上定义了 `tt`（TTIR）和 `ttg`（TTGIR）两套方言。不理解细节没关系，只要知道「`.mlir` 文件是文本形式的 IR」即可。
- **editable 安装**：`pip install -e .` 会把 C++/MLIR 编译成 `python/triton/_C/libtriton/` 下的扩展，并把 `third_party` 的后端软链到 `triton.backends`。

一个形象的类比：把仓库看作一座工厂——`python/` 是「下单与调度的办公室」，`lib/`+`include/` 是「中央加工车间（编译器）」，`third_party/` 是「针对不同硬件的成品装配线（NVIDIA / AMD）」。本讲就是带你参观这座工厂的厂区平面图。

## 3. 本讲源码地图

本讲涉及的关键文件（都很短，建议打开跟着看）：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/README.md) | 项目总入口，含安装、构建、调试开关说明 |
| [python/triton/\_\_init\_\_.py](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/__init__.py) | 前端公共 API 的「总开关」，从这里能看到前端层的全貌 |
| [python/triton/backends/\_\_init\_\_.py](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/backends/__init__.py) | 后端发现机制，把 `third_party` 的硬件后端接入前端 |
| [setup.py](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py) | 安装脚本，揭示了「third_party 后端如何被软链进 triton.backends」 |
| [test/lit.cfg.py](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/test/lit.cfg.py) | lit 测试的配置，定义了 `.mlir/.ll` 测试如何运行 |
| [pytest.ini](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/pytest.ini) | pytest 测试的配置 |
| [examples/plugins/README.md](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/examples/plugins/README.md) | 唯一的 `examples/` 内容，演示 out-of-tree 插件扩展 |

## 4. 核心概念与源码讲解

### 4.1 顶层目录划分

#### 4.1.1 概念说明

走进仓库根目录，你会看到十几个顶层条目。它们不是随机堆放的，而是严格对应「工厂」里的不同功能区。先把它们分成四类记：

- **构建与项目治理**：`CMakeLists.txt`、`setup.py`、`pyproject.toml`、`Makefile`、`cmake/`、`scripts/`、`utils/`、`.github/`、`AGENTS.md`、`CONTRIBUTING.md`。
- **Python 前端层**：`python/`。
- **C++/MLIR 编译器层**：`lib/` 与 `include/`。
- **硬件后端层**：`third_party/`，外加 `bin/`（C++ 命令行工具）。
- **测试**：`test/`（lit）、`unittest/`（C++ 单元测试）、以及 `python/` 内部的 `python/test/`（pytest）。
- **文档与示例**：`docs/`、`examples/`、`python/tutorials/`。

注意一个容易混淆的点：`python/` 目录里**既有前端代码、又有测试、又有教程、又有参考 kernel 库**。它本身就是一个「Python 子项目」。

#### 4.1.2 核心流程

下面这张「厂区平面图」就是仓库顶层结构（括号内是一句话职责）：

```
triton/
├── python/          # 【Python 前端层】前端 API、运行时、编译编排、语言 tl、教程、参考库、pytest 测试
│   ├── triton/      #   核心 Python 包（你会 import 的那个 triton）
│   ├── triton_kernels/  #  参考实现 kernel 库（matmul 等最佳实践）
│   ├── tutorials/   #   官方教程（01-vector-add.py 等）
│   ├── examples/    #   gluon 示例
│   └── test/        #   pytest 测试（多需 GPU）
├── lib/             # 【C++/MLIR 编译器层】编译器的实现代码
│   ├── Dialect/     #   IR 方言定义（Triton=TTIR, TritonGPU=TTGIR, Gluon...）
│   ├── Conversion/  #   IR 之间的转换 pass（TritonToTritonGPU, TritonGPUToLLVM）
│   ├── Target/      #   目标发射（LLVMIR）
│   ├── Analysis/    #   分析工具
│   └── Tools/       #   编译器内部工具
├── include/         # 【C++/MLIR 编译器层】lib/ 对应的头文件（triton/Dialect, Conversion, ...）
├── third_party/     # 【硬件后端层】按硬件厂商分目录
│   ├── nvidia/      #   NVIDIA 后端（backend/, hopper/, lib/, ...）
│   ├── amd/         #   AMD 后端（backend/, lib/, python/, ...）
│   ├── proton/      #   Triton 专用 profiler
│   └── f2reduce/    #   归约算法库
├── bin/             # 【硬件后端层周边】C++ 命令行工具源码（triton-opt.cpp 等）
├── test/            # 【测试】lit 测试（.mlir/.ll，FileCheck，无需 GPU）
├── unittest/        # 【测试】C++ googletest
├── docs/            # 【文档】Sphinx 文档源
├── examples/        # 【示例】插件（plugins）示例
├── cmake/ scripts/ utils/  # 构建/脚本辅助
├── CMakeLists.txt   # 顶层 CMake（编译 lib/、bin/、third_party）
├── setup.py         # Python 安装入口（含 CMakeBuild 把后端软链进来）
├── pyproject.toml   # Python 包元数据
└── Makefile         # 常用命令封装（make test / test-nogpu / dev-install）
```

两个关键体量数字，帮你建立直觉：`python/triton/` 下约有 **109 个 `.py` 文件**，而 `lib/` + `include/` 下约有 **221 个 `.cpp`/`.h` 文件**。也就是说，**编译器层（C++）的代码量明显大于前端层（Python）**——这正呼应了 u1-l1 的结论：Triton 的「重活」在编译器里。

#### 4.1.3 源码精读

根目录的 [README.md](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/README.md) 第 25 行一句话定调了整个项目，也暗示了三层结构存在的必要性：

> Triton, a language **and compiler** for writing highly efficient custom Deep-Learning primitives.

「language」（对应 `python/`）+「compiler」（对应 `lib/`/`include/`）+ 跨硬件（对应 `third_party/`）这三个词，正是仓库三层划分的源头。

构建顶层由两个文件分治：

- [CMakeLists.txt](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/CMakeLists.txt)：负责编译 C++/MLIR 部分（`lib/`、`bin/`、`third_party/*/lib`），产物最终落进 `python/triton/_C/libtriton/`。
- [setup.py](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py)：负责 Python 安装，并（关键！）把 `third_party` 后端软链进 Python 命名空间（见 4.2.3）。

#### 4.1.4 代码实践

**实践目标**：用「自己的眼睛」确认顶层划分，而不是死记这张图。

**操作步骤**：

1. 在仓库根目录运行 `ls`，对照上面的平面图，把每个条目归到四类（前端 / 编译器 / 后端 / 其他）。
2. 运行下面这条命令，统计三层各自的代码规模：

```bash
# Python 前端层（核心包）
find python/triton -name '*.py' | wc -l
# C++/MLIR 编译器层
find lib include -name '*.cpp' -o -name '*.h' | wc -l
# 硬件后端层（nvidia + amd 的 C++）
find third_party/nvidia third_party/amd -name '*.cpp' -o -name '*.h' | wc -l
```

**需要观察的现象**：前端 Python 文件数量约一百多个，编译器 C++ 文件两百多个，后端 C++ 文件也有相当规模。

**预期结果**：你会直观感受到「编译器层最重」，从而理解为什么后续学习会花大量篇幅在 `lib/` 上。

#### 4.1.5 小练习与答案

**练习 1**：仓库里有几处叫 `Dialect` 的目录？分别在哪个层？
**参考答案**：两处主要位置——`lib/Dialect/`（C++/MLIR 编译器层，方言的**实现**）和 `include/triton/Dialect/`（同层，对应的**头文件**）。此外 `third_party/proton/Dialect/` 也定义了 Proton 自己的方言。`lib/Conversion/` 与 `include/triton/Conversion/` 也是类似的「实现 + 头文件」配对关系。

**练习 2**：`unittest/` 和 `python/test/` 都是「测试」，它们测的对象有何不同？
**参考答案**：`unittest/` 是 **C++ googletest**，测的是编译器层（`lib/`）的 C++ 逻辑；`python/test/` 是 **pytest**，从前端 Python 出发做端到端验证（多数需要 GPU）。

---

### 4.2 前端 / 编译器 / 后端三层定位

这是本讲的核心模块。目标不是讲清每一层怎么实现，而是让你**在目录里看懂三层的边界与衔接点**。

#### 4.2.1 概念说明

回顾一条 kernel 的编译主线：

```
Python kernel 函数
   │  （前端层：python/triton）
   ▼
AST  ──code_generator──▶  TTIR        （lib/Dialect/Triton）
                             │  TritonToTritonGPU
                             ▼
                          TTGIR        （lib/Dialect/TritonGPU）
                             │  TritonGPUToLLVM + 后端 pass
                             ▼
                          LLIR ──▶ PTX/amdgcn ──▶ cubin/hsaco
                                  （硬件后端层：third_party/{nvidia,amd}）
```

三层分别负责：

- **前端层（`python/triton`）**：提供 `@triton.jit`、`tl` 语言、`triton.compile`、autotune、缓存、driver 调度。它的产出是 TTIR，并**编排**后续阶段。
- **编译器层（`lib/` + `include/`）**：定义 TTIR/TTGIR 方言、实现 IR 之间的转换 pass、做 GPU 优化、lowering 到 LLVM。这是真正的「编译器大脑」。
- **硬件后端层（`third_party/`）**：针对 NVIDIA / AMD 把后端特有的阶段（如 `make_ptx`/`make_cubin`、`make_hsaco`）、driver、方言扩展接进来。

一个关键认知：**前端层并不直接调用编译器层的 C++**，而是通过一个编译好的 Python 扩展 `triton._C.libtriton` 桥接。你在 `examples/plugins/README.md` 里能看到这行导入：

```python
from triton._C.libtriton import ir, passes
```

`_C` 表示「compiled C extension」，这个包在源码树里只有占位（`python/triton/_C/libtriton/linear_layout.pyi`），真正的 `.so`/`.pyd` 是 editable 安装时由 CMake 构建出来的（见 u1-l2）。

#### 4.2.2 核心流程：前端层的内部地图

`python/triton/` 包内部又分了几个子模块，各自对应一个学习单元：

| 子目录 | 职责 | 对应后续讲义 |
| --- | --- | --- |
| `runtime/` | `jit.py`（JITFunction）、`autotuner.py`（autotune/heuristics）、`driver.py`（设备/流管理）、`cache.py`（缓存）、`build.py`（编译 C 源）、`interpreter.py`（TRITON_INTERPRET）、`_allocation.py`（显存分配）、`_async_compile.py`（异步编译） | Unit 3 / 4 / 9 |
| `compiler/` | `compiler.py`（compile 编排）、`code_generator.py`（AST→TTIR）、`make_launcher.py`（生成启动器）、`errors.py` | Unit 5 / 6 |
| `language/` | `core.py`、`standard.py`、`math.py`、`semantic.py`、`extra/libdevice.py` —— 这就是 `tl` | Unit 2 |
| `backends/` | `__init__.py`（后端发现）、`compiler.py`（`BaseBackend` 抽象）、`driver.py`（`DriverBase` 抽象） | Unit 8 |
| `tools/` | `compile.py`（AOT 编译）、`disasm.py`（反汇编）、`gsan.py`、`link.py` 等 Python 命令行工具 | Unit 11 |
| `experimental/` | `gluon/`（实验性低级语言）、`gsan/` | Unit 12 |
| `knobs.py` | 全部配置旋钮（环境变量入口） | Unit 11 |
| `_C/libtriton/` | 编译出的 C++ 扩展占位 | （贯穿全文） |

而编译器层 `lib/` 的结构与编译主线一一对应：

```
lib/Dialect/Triton/        # TTIR 方言（IR/ + Transforms/）
lib/Dialect/TritonGPU/     # TTGIR 方言（IR/ + Transforms/，含 Pipeliner、WarpSpecialization）
lib/Conversion/TritonToTritonGPU/   # TTIR → TTGIR
lib/Conversion/TritonGPUToLLVM/     # TTGIR → LLVM IR
lib/Target/LLVMIR/         # LLVM 目标 / 调试信息
```

> 提示：`lib/` 与 `include/triton/` 是**镜像关系**。`lib/Dialect/Triton/IR/Ops.cpp` 的声明在 `include/triton/Dialect/Triton/IR/` 下。读 C++ 时，头文件（`include/`）看接口，实现（`lib/`）看逻辑。

#### 4.2.3 源码精读

**① 前端公共 API 的总开关**——[python/triton/\_\_init\_\_.py](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/__init__.py)。

第 2 行标注了版本（对应 u1-l1 提到的 Python 3.10–3.14 / Triton 3.8.0）：

```python
__version__ = '3.8.0'
```

[python/triton/\_\_init\_\_.py:8-28](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/__init__.py#L8-L28) 这一段 import 几乎就是前端层的一张「目录索引」——`from .runtime import ...`（运行时）、`from .compiler import compile`（编译编排）、`from . import language`（即 `tl`）、`from . import tools`。**想知道前端层有哪些公共能力，读这个文件的 `__all__` 即可。**

**② 后端发现机制**——[python/triton/backends/\_\_init\_\_.py](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/backends/__init__.py)。

这是「前端层 ↔ 硬件后端层」的衔接点。核心函数是 `_discover_backends`，它有两条路径：

- 默认路径（[第 58 行](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/backends/__init__.py#L58)）：通过 Python `entry_points(group="triton.backends")` 发现后端，每个 entry point 指向一个含 `compiler.py` 和 `driver.py` 的模块。
- 快捷路径（[第 42-55 行](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/backends/__init__.py#L42-L55)）：当 `TRITON_BACKENDS_IN_TREE=1` 时，直接扫描 `triton.backends` 命名空间下的子目录，`import triton.backends.<name>.compiler / driver`。

这说明：**每个硬件后端在 Python 侧就是一个「提供 `compiler.py` + `driver.py` 的包」**。`third_party/nvidia/backend/` 和 `third_party/amd/backend/` 正是两个这样的包。

**③ `third_party` 如何变成 `triton.backends`**——[setup.py](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py)。

这是理解目录结构最关键的一步。`setup.py` 在安装时把 `third_party` 的后端**软链（symlink）**进 `python/triton/backends/` 下：

- [setup.py:103](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py#L103)：定义后端安装目标目录 `python/triton/backends/<backend_name>`。
- [setup.py:386](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py#L386)：`backends = [*BackendInstaller.copy(["nvidia", "amd"]), *BackendInstaller.copy_externals()]`——in-tree 后端就是 `nvidia` 和 `amd`。
- [setup.py:443](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py#L443)：`update_symlink(backend.install_dir, backend.backend_dir)`——把 `third_party/<name>/backend` 软链成 `triton.backends.<name>`。
- [setup.py:533](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py#L533)：注册 `entry_points["triton.backends"]`，供 ② 中的默认路径发现。

同样地，Proton 被软链成 `triton.profiler`（[setup.py:466-468](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/setup.py#L466-L468)）。

> 所以一个常被问到的疑问——「为什么 `import triton.backends.nvidia` 能成功，但仓库里 `python/triton/backends/` 下却看不到 `nvidia` 目录？」——答案就是：它是 editable 安装时由 `setup.py` 创建的**符号链接**，指向 `third_party/nvidia/backend/`。这一点务必记住，否则你会在目录里「找不到」后端代码。

#### 4.2.4 代码实践

**实践目标**：验证 `third_party` 后端确实通过软链接入了 `triton.backends`，并定位三个层的入口文件。

**操作步骤**：

1. 如果已 editable 安装，在仓库根目录运行：

```bash
# 看软链：python/triton/backends/nvidia 应指向 third_party/nvidia/backend
ls -l python/triton/backends/
# 确认后端包结构
ls python/triton/backends/nvidia/   # 应看到 compiler.py driver.py
```

2. 若未安装（看不到软链），直接看源头的后端包：

```bash
ls third_party/nvidia/backend/      # compiler.py driver.py driver.c ...
ls third_party/amd/backend/
```

3. 在三个层各定位一个「入口文件」：
   - 前端：`python/triton/__init__.py`
   - 编译器：`lib/Dialect/Triton/IR/Dialect.cpp`
   - 后端：`third_party/nvidia/backend/compiler.py`

**需要观察的现象**：`python/triton/backends/nvidia` 是一个符号链接（箭头 `->`），指向 `third_party/nvidia/backend`。

**预期结果**：你亲眼确认了「前端 ↔ 后端」的衔接是软链 + entry point 发现，而不是把后端代码复制进 `python/`。如果环境未安装，则跳到第 2 步直接看 `third_party`，结论一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `lib/` 和 `include/` 要分开存放？
**参考答案**：这是 C++ 的惯例——`include/` 放**头文件（声明/接口）**，`lib/` 放**实现（`.cpp`）**。两者目录结构镜像（如 `include/triton/Dialect/` ↔ `lib/Dialect/`）。好处是：其它项目（比如插件、第三方后端）只需要引用 `include/` 就能使用 Triton 的编译器接口，而不必看到实现细节。

**练习 2**：`from triton._C.libtriton import ir, passes` 里的 `_C` 在源码树里为什么几乎是空的？
**参考答案**：`_C` 代表 compiled C extension。源码树里只放了 `.pyi` 类型存根（如 `linear_layout.pyi`）；真正的 Python 扩展模块（`.so`）是 CMake 把 `lib/` 的 C++/MLIR 代码编译后**生成**到 `python/triton/_C/libtriton/` 下的。所以它「安装后才存在」。

**练习 3**：一个新硬件厂商要给 Triton 加后端，最少要在仓库哪里放代码？
**参考答案**：按现有 `nvidia`/`amd` 的范式，应在 `third_party/<vendor>/backend/` 下提供 `compiler.py`（实现 `BaseBackend`，注册 `add_stages`）和 `driver.py`（实现 `DriverBase`），并在 `setup.py` 的 `copy([...])` 里加上厂商名。后端发现机制会自动把它接入。

---

### 4.3 测试目录与两套测试体系

#### 4.3.1 概念说明

Triton 有**两套并行的测试体系**，分别测不同层级、需要不同环境：

| 维度 | lit 测试 | pytest 测试 |
| --- | --- | --- |
| 位置 | 仓库根的 `test/` | `python/test/` |
| 测什么 | **编译器 IR pass** 的变换（给定 IR → 跑 pass → 期望 IR） | **端到端**：从前端 Python 启动 kernel，校验数值/行为 |
| 文件类型 | `.mlir`、`.ll`（文本 IR） | `test_*.py` / `*_test.py` |
| 断言方式 | `FileCheck`（模式匹配 IR 文本） | Python `assert` / pytest |
| 驱动工具 | `triton-opt`、`triton-llvm-opt`、`llc` | `pytest` |
| 是否需要 GPU | **否**（纯编译器变换） | **多数需要** |
| 对应层级 | C++/MLIR 编译器层 | 前端 + 后端 + 运行时 |

这套分工非常合理：编译器的 pass 变换是确定性的、不依赖硬件，用 lit + FileCheck 既快又稳定；而「这个 kernel 算得对不对」必须真正在 GPU 上跑，所以用 pytest。

另外还有第三类：`unittest/`（C++ googletest），用于编译器层 C++ 逻辑的细粒度单元测试，规模较小。

#### 4.3.2 核心流程

**lit 测试的组织**：`test/` 的子目录**镜像了 `lib/` 的结构**——`test/Triton/`（TTIR）、`test/TritonGPU/`（TTGIR）、`test/Conversion/`、`test/Analysis/`、`test/Gluon/`、`test/Hopper/`、`test/NVWS/`、`test/Plugins/`、`test/Proton/`、`test/Tools/`、`test/LLVMIR/`。每个 `.mlir` 文件顶部写着要跑哪个 pass、期望输出是什么。

一个 lit 测试的运行流程：

```
.mlir 文件（含 RUN: 行和 CHECK: 行）
   │  lit 测试运行器读取
   ▼
执行 RUN 行里的命令，通常是：triton-opt -<some-pass> input.mlir
   │  triton-opt 加载 IR、跑指定 pass
   ▼
输出变换后的 IR  ──▶  FileCheck 按 CHECK 行做文本模式匹配
   │
   ▼
全部 CHECK 命中 = 通过
```

**pytest 测试的组织**：`python/test/` 下分 `unit/`（单元）、`regression/`（回归）、`microbenchmark/`（微基准）、`gluon/`、`gsan/`、`backend/`、`kernel_comparison/`，由根目录的 `conftest.py` 统一配置（如按硬件参数化）。

#### 4.3.3 源码精读

**lit 配置**——[test/lit.cfg.py](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/test/lit.cfg.py)。

第 19-20 行声明了 lit 只把 `.mlir` 和 `.ll` 当测试文件：

```python
config.suffixes = ['.mlir', '.ll']
```

[test/lit.cfg.py:58-64](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/test/lit.cfg.py#L58-L64) 注册了 lit 测试可调用的工具，其中 `triton-opt` 是主力——它来自 `bin/triton-opt.cpp` 编译出的可执行文件：

```python
tools = [
    'triton-opt',
    'triton-llvm-opt',
    'mlir-translate',
    'llc',
    ToolSubst('%PYTHON', config.python_executable, unresolved='ignore'),
]
```

举个真实测试样例：`test/Triton/ops.mlir`、`test/Triton/loop-unroll.mlir`、`test/Triton/canonicalize.mlir` 等都在验证 TTIR 层 pass 的行为。

**pytest 配置**——[pytest.ini](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/pytest.ini)。

第 4 行定义了 pytest 收集哪些文件作为测试，**还把 `tutorials/*.py` 和 `examples/*.py` 也纳入测试**（保证官方教程始终可运行）：

```ini
python_files = test_*.py *_test.py tutorials/*.py examples/*.py
```

#### 4.3.4 代码实践

**实践目标**：用肉眼读懂一个 lit 测试在做什么，建立「`.mlir` 测试 = IR 变换校验」的直觉。

**操作步骤**：

1. 打开一个最简单的 lit 测试文件，例如 [test/Triton/vecadd.mlir](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/test/Triton/vecadd.mlir)，阅读它顶部的 `// RUN:` 行和 `// CHECK:` 行。
2. 对照 [bin/triton-opt.cpp](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/bin/triton-opt.cpp)，理解 `triton-opt` 是一个独立的 MLIR pass 驱动器（不需要 GPU，不需要 Python 运行时）。
3. 打开一个 pytest 测试，例如 `python/test/unit/language/test_core.py`（用 `ls python/test/unit/language/` 确认实际文件名），看它如何 `import triton`、启动 kernel 并 `assert` 数值。

**需要观察的现象**：

- lit 测试里 `RUN` 行调用 `triton-opt`，`CHECK` 行描述期望的 IR 文本片段。
- pytest 测试里有 `@triton.jit`、`tl.load`、`torch` 对照、`assert torch.allclose(...)`。

**预期结果**：你能用一句话说出两类测试的区别——「lit 测编译器把 IR 变成什么，pytest 测 kernel 算得对不对」。

> 说明：本实践是「源码阅读型」，不要求运行。若你想真正运行 lit 测试，需要先 `make` 出 `triton-opt`（见 u1-l2 的 `make test-nogpu`），属于后续练习。

#### 4.3.5 小练习与答案

**练习 1**：`test/` 下的子目录为什么和 `lib/` 长得那么像？
**参考答案**：因为 lit 测试是**按编译器子系统组织**的——`test/Triton/` 对应 `lib/Dialect/Triton/`（TTIR），`test/TritonGPU/` 对应 `lib/Dialect/TritonGPU/`（TTGIR），`test/Conversion/` 对应 `lib/Conversion/`。这种镜像让你改了某个 pass 后，能立刻在同名测试目录下找到对应的回归测试。

**练习 2**：为什么 `pytest.ini` 要把 `tutorials/*.py` 也算作测试？
**参考答案**：官方教程就是 Triton 的「门面」，必须始终保持可运行、结果正确。把教程纳入 pytest 能在 CI 里持续守护它们，避免教程随 API 演进而失效。

**练习 3**：没有 GPU 的机器，能运行哪一类测试？
**参考答案**：lit 测试（`test/`，纯 IR 变换）和 `unittest/`（C++ googletest）都不需要 GPU；此外设 `TRITON_INTERPRET=1` 后部分 pytest 也能在解释器模式下跑（见 u1-l1、u1-l2）。完整的无 GPU 组合是 `make test-nogpu`。

---

## 5. 综合实践

**任务**：亲手绘制一张「Triton 仓库三色目录树」，把全讲的知识串成一张图。

**要求**：

1. 从仓库根目录出发，画出至少三层的目录树（根 → 顶层目录 → 关键子目录）。
2. 用三种颜色（或三种标记，如 🟦/🟧/🟥）分别标注：
   - 🟦 **Python 前端层**（`python/triton/` 及其子目录）
   - 🟧 **C++/MLIR 编译器层**（`lib/` 与 `include/`）
   - 🟥 **硬件后端层**（`third_party/nvidia`、`third_party/amd`、`third_party/proton`、`bin/`）
3. 在每个关键目录旁用一句话注明职责（可参考 4.1.2 的平面图）。
4. 用箭头在图上标出**编译主线的流动**：从 `python/triton`（前端）→ `lib/Dialect/Triton`（TTIR）→ `lib/Dialect/TritonGPU`（TTGIR）→ `lib/Conversion` → `third_party/{nvidia,amd}/backend`（产物 cubin/hsaco）。
5. 在图上单独标注两个「衔接点」：
   - 前端调用编译器的桥：`python/triton/_C/libtriton`（编译生成）。
   - 后端接入前端的桥：`setup.py` 把 `third_party/*/backend` 软链成 `triton.backends.*`，由 `backends/__init__.py` 发现。
6. 最后标注两套测试的位置与分工：`test/`（lit，无需 GPU）与 `python/test/`（pytest，多需 GPU）。

**验收标准**：拿着你自己画的这张图，你应该能回答：「我要改 TTIR 的某个操作定义，去哪个文件？」「我要给 AMD 后端加一个编译阶段，去哪个目录？」「我想验证一个 pass 的 IR 变换，跑哪个测试？」——如果三个问题都能从图上直接定位，本讲就达标了。

> 提示：你可以用任意工具画（Markdown 树、Mermaid、纸笔拍照均可）。重点是**三色分层 + 编译主线箭头 + 两个衔接点标注**这三件事都做到。

## 6. 本讲小结

- 仓库顶层按**四类**划分：构建治理（`CMakeLists.txt`/`setup.py`/`Makefile`/`cmake/`）、Python 前端（`python/`）、C++/MLIR 编译器（`lib/`+`include/`）、硬件后端（`third_party/`+`bin/`）。
- **三层结构**与编译主线一一对应：前端层产出并编排 TTIR（`python/triton`），编译器层定义 TTIR/TTGIR 方言与转换 pass（`lib/Dialect`、`lib/Conversion`），后端层负责硬件特有阶段与产物（`third_party/{nvidia,amd}/backend`）。
- 前端公共 API 的「目录索引」是 [python/triton/\_\_init\_\_.py](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/__init__.py)（`__version__ = '3.8.0'`，`__all__` 列出全部能力）。
- 「前端 ↔ 编译器」的桥是编译产物 `triton._C.libtriton`；「前端 ↔ 后端」的桥是 `setup.py` 的软链 + `backends/__init__.py` 的 entry point 发现——`triton.backends.nvidia` 实际指向 `third_party/nvidia/backend`。
- `lib/` 与 `include/` 是镜像关系（实现 vs 头文件），`test/` 子目录又镜像了 `lib/`，方便定位回归测试。
- 两套测试体系：`test/`（lit + FileCheck，`.mlir/.ll`，纯编译器变换，无需 GPU）与 `python/test/`（pytest，端到端，多需 GPU），外加 `unittest/`（C++ googletest）。

## 7. 下一步学习建议

有了这张地图，接下来的学习就有了坐标系。建议按下面的顺序继续：

1. **先动手写第一个 kernel**，把「前端层」跑通：进入 [u1-l4 编写并运行第一个 Triton kernel](u1-l4-first-kernel.md)，基于 `python/tutorials/01-vector-add.py` 体验 `@triton.jit` 与 `tl`。
2. **再深入前端语言**：Unit 2（[u2-l1 tl 核心类型](u2-l1-tl-core-types.md)）会带你读 `python/triton/language/core.py`。
3. **当你想理解「kernel 是怎么变成 GPU 代码的」**，回到编译器层：Unit 5（[u5-l1 compile 入口](u5-l1-compile-entry-stages.md)）从前端 `triton.compile` 出发，Unit 7（[u7-l1 TTIR 方言](u7-l1-ttir-dialect.md)）带你读 `lib/Dialect/Triton/IR/Ops.cpp`。
4. **想看硬件差异**：Unit 8（[u8-l1 后端发现与接口](u8-l1-backend-discovery-interface.md)）会展开本讲提到的 `backends/__init__.py` 与 `BaseBackend`，并对比 `third_party/nvidia` 与 `third_party/amd`。

> 一个阅读小窍门：以后每打开一个新文件，先在心里问「它属于三色中的哪一层？」——这个问题能帮你快速定位、避免在 200+ 个 C++ 文件里迷路。
