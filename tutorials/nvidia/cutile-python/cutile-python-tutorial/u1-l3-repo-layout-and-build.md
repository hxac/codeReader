# 源码目录结构与构建系统

## 1. 本讲目标

学完本讲，你应当能够：

- 画出 cuTile Python 仓库的顶层目录地图，说出 `src/`、`cext/`、`docs/`、`samples/`、`test/`、`experimental/`、`cmake/` 各自装了什么。
- 在 `src/cuda/tile/` 下，把 `_ir`、`_passes`、`_bytecode`、`compilation`、`tune`、`jax` 等子包归到「前端 / 优化 / 后端 / 运行时」四类里，并解释为什么有的包会横跨多类。
- 看懂 `pyproject.toml` 的 `[tool.setuptools.packages.find]` 规则，说清「哪些 Python 包会被打进 `cuda-tile` 发行版、哪些不会」。
- 用一句话描述 `setup.py → CMake → cext/CMakeLists.txt` 这条 C++ 扩展构建链，并解释可编辑构建（editable build）为什么要用「符号链接」把 `.so` 放回源码树。
- 说清 `cext`（C++ 扩展 `_cext`）在整体架构里的「桥接」角色：它是 Python 运行时与 CUDA Driver API 之间唯一的那座桥。

本讲是「上手运行」单元的第三篇，承接 [u1-l1 项目总览](u1-l1-project-overview.md) 与 [u1-l2 环境搭建与第一个内核](u1-l2-install-and-first-kernel.md)：前两讲建立了「AST → HIR → Tile IR → 字节码 → cubin → cuLaunchKernel」的全链路直觉、并把你本地的环境跑通了。本讲不再讲怎么用 cuTile，而是带你**俯瞰整个仓库**——看清这条链路在源码里分别住在哪些目录、又是怎么被构建出来的。这样后续 U5（前端）、U6（优化）、U7（后端）、U8（运行时）讲义再深入某个文件时，你脑子里始终有一张「我在地图的哪一格」的方位感。

## 2. 前置知识

- **Python 包（package）与模块（module）**：一个目录里放 `__init__.py` 就构成一个包；`import cuda.tile` 实际是导入 `cuda/tile/__init__.py`。本讲会反复用到「包」这个词。
- **setuptools / pyproject.toml**：Python 打包的事实标准。`pyproject.toml` 描述「这个包叫什么、依赖什么、包含哪些子包」；setuptools 按它来打包发布。
- **src 布局（src layout）**：把 Python 源码放在 `src/` 子目录下，而不是仓库根目录。好处是只有「安装之后」才能 `import`，避免「在仓库根目录随手 import 到未安装的代码」这种隐蔽 bug。
- **CMake**：跨平台的 C/C++ 构建系统。cuTile 用它来编译 C++ 扩展。你只需知道「CMake 读 `CMakeLists.txt`，生成构建文件，再编译出 `.so`」即可，不要求会写 CMake。
- **C++ 扩展（C++ extension）**：cuTile 主体是纯 Python，但「真正启动 GPU、调用 CUDA Driver」那部分用 C++ 写，编译成 `.so`（Linux）/`.dll`（Windows）后才能被 Python `import`。这个产物在本项目里叫 `_cext`。
- **DLPack**：一个跨框架的张量内存交换标准（PyTorch、CuPy、JAX 都支持）。cuTile 用它来「零拷贝」接收宿主张量（回顾 u1-l2）。
- **可编辑构建（editable install）**：`pip install -e .`，让「安装好的包」指向你的源码目录，改完源码立即生效，不用反复重装。回顾 u1-l2，本讲会从构建系统角度把它讲透。

如果你对上面某项完全陌生，没关系——本讲会在用到时再点一句。

## 3. 本讲源码地图

本讲偏「工程结构」，引用以构建配置与目录入口为主：

| 文件 | 作用 |
| --- | --- |
| [pyproject.toml](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/pyproject.toml) | 包元数据 + setuptools 包发现规则：决定哪些子包属于 `cuda-tile`。 |
| [setup.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/setup.py) | 自定义 `build_ext`，驱动 CMake 编译 C++ 扩展，并实现可编辑构建的符号链接。 |
| [CMakeLists.txt](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/CMakeLists.txt) | 顶层 CMake：查找 CUDA Toolkit、抓取 DLPack、把构建委派给 `cext/`。 |
| [cext/CMakeLists.txt](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/cext/CMakeLists.txt) | 真正把 `cext/*.cpp` 编译成 `_cext` 扩展模块（及静态库、测试可执行文件）的地方。 |
| [src/cuda/tile/__init__.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/__init__.py) | `cuda.tile` 的公共 API 出口，也是画「模块依赖树」时的根节点。 |
| [src/cuda/tile/_cext.pyi](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_cext.pyi) | C++ 扩展的类型存根（stub），用纯 Python 接口的形式声明了 `cext` 暴露给上层的全部能力。 |

> 说明：本讲指定的核心文件是两个构建配置（`pyproject.toml`、`setup.py`）与两个 `CMakeLists.txt`；`__init__.py` 与 `_cext.pyi` 用来支撑「目录职责划分」与「cext 桥接角色」两个最小模块。

## 4. 核心概念与源码讲解

本讲围绕四个最小模块展开：**仓库顶层目录地图**（4.1）、**`src/cuda/tile` 子包的四层职责**（4.2）、**setuptools 包发现规则**（4.3）、**cext 的 CMake 构建**（4.4）。前两个回答「代码住在哪里」，后两个回答「代码怎么被打包和编译出来」。其中 4.3、4.4 对应规格要求覆盖的「setuptools package find」「cext CMake build」两个最小模块。

### 4.1 仓库顶层目录地图

#### 4.1.1 概念说明

在深入任何一个子目录之前，先从最顶层看清 cuTile Python 仓库分成几大块。一个「编译器 + 运行时」项目，通常会把这些东西分目录存放：源码、C++ 桥接、文档、示例、测试、构建脚本、实验性功能。cuTile 也不例外。

#### 4.1.2 核心流程

仓库根目录的主要条目及其职责：

| 顶层条目 | 类别 | 作用 |
| --- | --- | --- |
| `src/` | 源码 | 全部 Python 源码，根包是 `cuda.tile`（命名空间 `cuda/` → `tile/`）。 |
| `cext/` | C++ 源码 | C++ 扩展源码，编译产物是 `_cext`（Python 运行时与 CUDA 之间的桥）。 |
| `docs/` | 文档 | Sphinx 文档源（`.rst`），可构建出官方文档站点。 |
| `samples/` | 示例 | 一组可运行的内核示例（MatMul、LayerNorm、Attention…）及 `quickstart/` 快速入门、`templates/` 模板。 |
| `test/` | 测试 | pytest 测试集与 benchmark，验证编译器与运行时的正确性/性能。 |
| `experimental/` | 实验 | 尚未稳定的实验性包（如 `tile_experimental` 的 autotuner、`cuda-lang`），独立打包，**不属于** `cuda.tile`。 |
| `cmake/` | 构建辅助 | CMake 辅助脚本（`FetchXLAHeaders.cmake`、`FindCUDAToolkit.cmake`）。 |
| `scripts/` | 工具脚本 | 开发用脚本（如 license 检查、cpplint）。 |
| `CMakeLists.txt` / `setup.py` / `pyproject.toml` | 构建配置 | 三个构建入口（见 4.3、4.4）。 |

有两个要点先记住：

1. **Python 源码与 C++ 源码是分家的**：`src/` 是纯 Python（编译器前端、优化、后端、Python 运行时），`cext/` 是 C++（GPU 启动桥接）。它们靠一个叫 `_cext` 的扩展模块连起来。
2. **`experimental/` 不是 `cuda.tile` 的一部分**：它是一个独立的发行包，需要单独 `pip install ./experimental/tile_experimental`（见 [README.md:126-146](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/README.md#L126-L146)）。这一点在 4.3 的包发现规则里会再次得到印证。

#### 4.1.3 源码精读

README 的「Building from Source」一节直接印证了上面的划分，它列出了从源码构建所需的依赖，并点明「cuTile 主要是 Python，但包含一个需要编译的 C++ 扩展」：

> cuTile is written mostly in Python, but includes a C++ extension which needs to be built.
>
> 见 [README.md:83-91](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/README.md#L83-L91)

而顶层 `CMakeLists.txt` 的开头几行，把「这是一个叫 cuda-tile-python 的 CMake 工程」确定下来，并要求 C++17（MSVC 下 C++20）：

[CMakeLists.txt:5-14](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/CMakeLists.txt#L5-L14) —— 设定 `cmake_minimum_required(VERSION 3.18)`、`project(cuda-tile-python)`、C++ 标准，与本讲「CMake 负责编译 C++ 部分」一致。

#### 4.1.4 代码实践

**实践目标**：在不打开任何子目录内部文件的前提下，仅凭顶层目录名，给每个目录贴上「属于工程链路的哪一段」的标签。

**操作步骤**：

1. 在仓库根目录执行 `ls -F`（或直接看本讲 4.1.2 的表）。
2. 对每个顶层目录，判断它属于：`Python 编译器源码` / `C++ 桥接源码` / `文档与示例` / `测试` / `构建配置` / `实验性功能`。
3. 特别留意：`src/` 和 `cext/` 各自产生什么产物（一个是 `.py`，一个是 `.so`）。

**需要观察的现象**：你会注意到「真正和 GPU 打交道的 C++ 代码」与「编译器逻辑的 Python 代码」在物理上完全隔离，唯一的连接点是 `_cext` 这个扩展模块。

**预期结果**：`src/` → Python 编译器源码；`cext/` → C++ 桥接源码；`docs/`+`samples/` → 文档与示例；`test/` → 测试；`experimental/` → 实验性功能；`CMakeLists.txt`+`setup.py`+`pyproject.toml` → 构建配置。

> 待本地验证：若你已按 u1-l2 做过可编辑构建，`ls build/` 应能看到 CMake 的构建树，里面会有编译 `cext` 产生的中间产物。

#### 4.1.5 小练习与答案

**练习 1**：如果有人问你「cuTile 的编译器是 Python 写的还是 C++ 写的」，你怎么回答最准确？

> **参考答案**：编译器主体（前端 ast2hir/hir2ir、优化 pass、后端 ir2bytecode、字节码格式）是纯 Python，住在 `src/cuda/tile/`；只有「把 cubin 加载到 GPU 并启动」这一段运行时桥接是 C++，住在 `cext/`。`tileiras`（把字节码编成 cubin）则是另一个独立的外部二进制工具，既不在 `src/` 也不在 `cext/`。

**练习 2**：`experimental/` 下的代码改了，会自动随根目录的 `pip install -e .` 生效吗？

> **参考答案**：不会。`experimental/` 是独立打包的，根目录的 `pyproject.toml` 并不包含它（见 4.3）。它需要单独 `pip install ./experimental/tile_experimental` 才能被 import。

---

### 4.2 `src/cuda/tile` 子包：前端 / 优化 / 后端 / 运行时

#### 4.2.1 概念说明

`src/cuda/tile/` 是整个项目最核心的目录。回顾 u1-l1 的编译全链路：

```
Python kernel → AST → HIR → Tile IR →（优化 pass）→ 字节码 →（tileiras）cubin → cuLaunchKernel
```

这条链路可以粗略切成四段，正好对应本讲练习要你标注的四类：

- **前端（frontend）**：把 Python 函数变成 IR（AST→HIR→IR），以及面向用户的 API/stub。
- **优化（optimization）**：在 Tile IR 上跑各种变换 pass（DCE、整除传播、token 排序…）。
- **后端（backend）**：把优化后的 IR 变成字节码、再变成 cubin（含 AOT 导出、磁盘缓存）。
- **运行时（runtime）**：`launch`、调度、JIT 触发、C++ 桥接。

`src/cuda/tile/` 下的每个子包/文件，基本都能归到这四类之一。理解这张归类表，是后续 U5–U8 阅读源码的「地图」。

#### 4.2.2 核心流程

把 `src/cuda/tile/` 的关键条目按四类归位（带★的是「横跨多类」的骨架性模块）：

| 子包 / 文件 | 归类 | 职责 |
| --- | --- | --- |
| `_passes/`（`ast2hir.py`、`hir2ir.py`、`ast_util.py`） | 前端 | Python AST → HIR → Tile IR 的降级。 |
| `_stub.py`、`__init__.py`、`_datatype.py`、`_numeric_semantics.py`、`_annotated_function.py`、`_compiler_options.py` | 前端 | 用户 API（`ct.load`/`ct.add`…）、类型与数值语义、参数注解、编译选项。 |
| `_passes/`（`dce.py`、`code_motion.py`、`dataflow_analysis.py`、`propagate_divby.py`、`token_order.py`、`loop_split.py`、`rewrite_patterns.py`、`unhoist_partition_views.py`、`eliminate_assign_ops.py`） | 优化 | Tile IR 上的各类优化 pass。 |
| `_ir2bytecode.py`、`_bytecode/` | 后端 | IR → TileIR 字节码的编码与二进制格式（含版本管理）。 |
| `compilation/`、`_cache.py` | 后端 | AOT 导出（cubin/字节码）、签名与名称修饰、JIT 磁盘缓存。 |
| `_execution.py`、`_dispatch_mode.py`、`_context.py`、`tune/`、`jax/`、`_cext.pyi`（+ `cext/`） | 运行时 | `kernel`/`launch`、调度模式、TileContext、自动调优、JAX 互操作、C++ 桥接。 |
| ★ `_ir/` | 贯穿 | Tile IR 的核心数据结构（IRContext/Builder/Var/Operation/类型/Op 实现），前端产出、优化改写、后端消费都用它。 |
| ★ `_compile.py` | 贯穿 | 编译流水线编排（`compile_tile`），把前端→优化→后端串成一条流水线。 |
| `_load_libcuda.py` | 运行时/后端 | 定位并加载 `tileiras` 与 libcuda。 |

> 注意：`_passes/` 这个目录**同时**装着前端降级（`ast2hir`/`hir2ir`）和优化 pass（`dce`/`code_motion`…）。所以它不是「某一类」，而是「前端 + 优化」两类并存。归类时不要一刀切。

#### 4.2.3 源码精读

`src/cuda/tile/__init__.py` 是这张地图的根节点——`cuda.tile` 对外暴露的所有符号都从这里 `import`。它的 `from ... import` 语句本身就是一张「依赖指纹」：

[src/cuda/tile/__init__.py:7-14](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/__init__.py#L7-L14) —— 第一行 `from cuda.tile._cext import launch` 直接点明了「运行时的 `launch` 来自 C++ 扩展 `_cext`」；紧接着的 `ByTarget`、`MemoryOrder`、`MemoryScope` 来自 `_by_target.py`、`_memory_model.py`。这几行已经把「运行时 + 内存模型」两类模块挂到了根上。

[src/cuda/tile/__init__.py:58-165](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/__init__.py#L58-L165) —— 这里一大段 `from cuda.tile._stub import (...)` 把全部用户 API（`load`/`store`/`add`/`mma`/`sum`/`gather`…）从 `_stub.py` 拉进来。`_stub.py` 就是 4.2.2 表里「前端 / 用户 API」那一格的核心。

最后，子包以「命名空间包」的方式被挂上来：

[src/cuda/tile/__init__.py:169-176](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/__init__.py#L169-L176) —— `from cuda.tile import tune`、`import cuda.tile.compilation as compilation`，把 `tune/`（运行时/调优）与 `compilation/`（后端/AOT）这两个子包接到公共 API 上。

#### 4.2.4 代码实践

**实践目标**：用 `__init__.py` 的 import 顺序，倒推出「哪些模块是 `cuda.tile` 的直接依赖」。

**操作步骤**：

1. 打开 [src/cuda/tile/__init__.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/__init__.py)。
2. 把所有 `from cuda.tile.xxx import ...` 与 `import cuda.tile.xxx` 语句里的 `xxx` 收集起来，得到一份「直接依赖模块清单」。
3. 对照本讲 4.2.2 的归类表，给清单里每个模块标上四类之一。

**需要观察的现象**：你会发现清单里**没有**出现 `_ir2bytecode.py`、`_compile.py`、`_passes/`、`_ir/`——这些是「内部实现模块」，不直接在顶层 `__init__.py` 里 import，而是在被调用时（如 `launch` 触发 JIT）才间接拉起来。这正说明「公共 API 面」与「编译器内部实现」是分开的。

**预期结果**：直接依赖清单大致为 `_cext`、`_by_target`、`_memory_model`、`_numeric_semantics`、`_datatype`、`_exception`、`_stub`、`_context`、`tune`、`_execution`、`compilation`；归类后能看到「运行时 + 前端 API + 后端导出」三类在顶层露头，而「优化 pass / IR 内部」藏在更深处。

> 待本地验证：可在装好 cuTile 的环境里执行 `python -c "import cuda.tile; print(cuda.tile.__file__)"` 确认根 `__init__.py` 的位置。

#### 4.2.5 小练习与答案

**练习 1**：为什么把 `_ir/` 归为「贯穿」而不是某一类？

> **参考答案**：因为 `_ir/` 定义的是 Tile IR 的数据结构本身（`IRContext`、`Builder`、`Var`、`Operation`、类型系统）。前端产生它、优化 pass 改写它、后端消费它（编成字节码）——它是三类共享的「公共语言」，不独属于任何一段。

**练习 2**：`_passes/` 目录里，怎么区分哪些文件是「前端」、哪些是「优化」？

> **参考答案**：看职责——`ast2hir.py`、`hir2ir.py`、`ast_util.py` 负责把 Python 降级成 IR，属于前端；其余如 `dce.py`、`code_motion.py`、`dataflow_analysis.py`、`token_order.py`、`loop_split.py` 等负责在已成形的 IR 上做变换，属于优化。

---

### 4.3 setuptools 包发现：pyproject.toml 如何决定打包哪些模块

（对应最小模块：**setuptools package find**）

#### 4.3.1 概念说明

知道代码「住在哪里」之后，下一个问题是：「`pip install cuda-tile` 时，到底哪些目录会被装进用户的 site-packages？」这由 `pyproject.toml` 的包发现规则（`[tool.setuptools.packages.find]`）决定。

这是一个容易被忽略、但很重要的细节：**不是 `src/` 下所有目录都会被打包**。cuTile 用了「显式 include 清单」，只打包它点名的子包。这意味着——如果你新建了一个子包却忘了加进清单，它就不会出现在发行版里。

#### 4.3.2 核心流程

setuptools 打包的发现流程：

1. `where = ["src"]`：把 `src/` 作为搜索根。
2. `include = [...]`：**只**收下清单里列出的包。
3. 把这些包按「命名空间包」安装——`cuda`、`cuda.tile`、`cuda.tile.compilation` 等。
4. 同时用 `package-data` 把非 `.py` 的资源文件（如 `VERSION`）一并带上。
5. 版本号从 `src/cuda/tile/VERSION` 动态读取。

关键观察：清单是**显式且有限**的 8 个包，`experimental/`（它在另一个根下，且独立打包）和任何未列出的目录都不会进入 `cuda-tile`。

#### 4.3.3 源码精读

`pyproject.toml` 的包发现段落：

```toml
[tool.setuptools.packages.find]
where = ["src"]
include = [
    "cuda",
    "cuda.tile",
    "cuda.tile.compilation",
    "cuda.tile._ir",
    "cuda.tile._passes",
    "cuda.tile._bytecode",
    "cuda.tile.tune",
    "cuda.tile.jax",
]
```

> 见 [pyproject.toml:53-64](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/pyproject.toml#L53-L64) —— 这份清单正好对应 4.2 里讲的核心子包：`_ir`、`_passes`、`_bytecode`、`compilation`、`tune`、`jax` 全部在列。注意 `experimental/` **不在**这里（它走自己的 `experimental/tile_experimental/pyproject.toml`），`test`、`samples` 也不在——所以它们不会随主包发行。

版本与包数据：

[pyproject.toml:50-51](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/pyproject.toml#L50-L51) —— `version = {file = "src/cuda/tile/VERSION"}`，版本号从 `VERSION` 文件动态读取（这就是为什么 `src/cuda/tile/VERSION` 这个文件存在）。

[pyproject.toml:66-67](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/pyproject.toml#L66-L67) —— `package-data` 把 `VERSION` 文件作为 `cuda.tile` 包的数据带上，否则安装后读不到版本。

依赖与构建后端：

[pyproject.toml:5-10](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/pyproject.toml#L5-L10) —— 构建后端是 `setuptools.build_meta`，锁定了 `setuptools==80.10.2`。

[pyproject.toml:37-42](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/pyproject.toml#L37-L42) —— 运行时依赖只有 `typing-extensions`；`[tileiras]` 可选依赖展开成 `cuda-toolkit[tileiras,nvcc,nvvm]>=13.2,<13.4`（回顾 u1-l2）。这说明纯 Python 部分的运行时依赖极少，重头都在 C++ 扩展和外部 tileiras 上。

#### 4.3.4 代码实践

**实践目标**：验证「清单外的目录不会被装进 `cuda-tile`」。

**操作步骤**：

1. 打开 [pyproject.toml:53-64](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/pyproject.toml#L53-L64)，数一下 `include` 清单里有几个包。
2. 对照 `src/cuda/tile/` 下实际存在的子目录（`_ir`、`_passes`、`_bytecode`、`compilation`、`tune`、`jax` 等），确认它们都在清单里。
3. 思考：`src/cuda/tile/` 下那些「单文件模块」（如 `_compile.py`、`_stub.py`、`_execution.py`）为什么不需要单独列在 `include` 里？

**需要观察的现象**：你会发现 `include` 只列**子包**（带 `__init__.py` 的目录），不列「根包 `cuda.tile` 下的单个 `.py` 文件」——因为它们已经属于 `cuda.tile` 这个包，随包一起打包。

**预期结果**：清单含 8 项（`cuda`、`cuda.tile` 及 6 个子包）；`experimental/` 不在其中；根包下的单文件模块因属于 `cuda.tile` 自动被打包。

> 待本地验证：在可编辑安装后执行 `python -c "import cuda.tile, cuda.tile.tune, cuda.tile.jax; print('ok')"`，三者都应能 import；若尝试 `import cuda.tile_experimental` 则需要单独安装实验包才会成功。

#### 4.3.5 小练习与答案

**练习 1**：如果有人在 `src/cuda/tile/` 下新建了一个子包 `src/cuda/tile/_foo/`（含 `__init__.py`），但不改 `pyproject.toml`，会发生什么？

> **参考答案**：在可编辑安装（`pip install -e .`）下，因为源码就在原地，可能碰巧能 import；但在打成 wheel/正式安装后，`_foo` 不会进入 site-packages，`import cuda.tile._foo` 会失败。原因是 `packages.find` 的 `include` 是显式白名单，未列出的子包不会被收集。

**练习 2**：为什么版本号要用一个独立的 `VERSION` 文件 + `dynamic = ["version"]`，而不是直接写死在 `pyproject.toml` 里？

> **参考答案**：把版本单独放 `src/cuda/tile/VERSION`，既能让 `pyproject.toml` 通过 `dynamic` 读取，又能让运行时的 `cuda.tile._version.__version__` 从同一来源读取（见 `__init__.py` 第 5 行 `from cuda.tile._version import __version__`），保证「打包元数据的版本」与「运行时 `import` 看到的版本」始终一致，单一事实来源（single source of truth）。

---

### 4.4 cext 的 CMake 构建：setup.py 如何把 C++ 编译成 `_cext`

（对应最小模块：**cext CMake build**）

#### 4.4.1 概念说明

纯 Python 不需要编译，但 `cext/` 下的 C++ 必须先编成 `_cext.so`（Linux）/`_cext.dll`（Windows）才能被 `import cuda.tile._cext`。这件事涉及三个文件的接力：

- **`setup.py`**：自定义 `build_ext` 命令，拦截 Python 打包过程的「构建扩展」阶段，转去调用 CMake。
- **顶层 `CMakeLists.txt`**：查找 CUDA Toolkit、抓取 DLPack/XLA 头、把构建委派给 `cext/`。
- **`cext/CMakeLists.txt`**：真正编译 `cext/*.cpp`，产出 `_cext` 扩展模块。

`cext` 在架构里扮演的是**桥接（bridge）**角色：Python 运行时（`launch`、`TileDispatcher`、`TileContext`）想调用 CUDA Driver API（如 `cuLaunchKernel`），唯一通道就是这个 C++ 扩展。回顾 u1-l1，`cuLaunchKernel` 这一步就发生在这里。

这里有一个「鸡生蛋」问题：Python 侧由 setuptools 打包，C++ 侧由 CMake 编译，但用户只想敲一次 `pip install`。cuTile 的解法是**让 setuptools 在编译扩展时去调用 CMake**——这正是 `setup.py` 里 `BuildExtWithCmake` 的职责。

#### 4.4.2 核心流程

从 `pip install -e .` 到 `_cext.so` 的完整构建链：

```
pip install -e .
   │
   ▼  setuptools 触发 build_ext
setup.py: BuildExtWithCmake.run()
   │
   ├─ _cmake(...)   →  cmake -B build  项目根   （配置：生成构建文件）
   │     传入 DLPACK_PATH / XLA_PATH / 构建类型 / Python 解释器
   │
   ├─ _make(...)    →  cmake --build build     （编译）
   │
   └─ 把 build/.../lib_cext.so 复制/符号链接 到 源码树
        ├─ 可编辑模式（editable_mode=True）→ 符号链接（link="sym"）
        └─ 普通模式                       → 普通复制（link=None）
```

CMake 侧（顶层 → `cext/`）的准备：

```
顶层 CMakeLists.txt
  ├─ find_package(Python) / find_package(CUDAToolkit)
  ├─ FetchContent 拉取 dlpack 头（除非给了 DLPACK_PATH）
  ├─ fetch_xla_headers()（除非给了 XLA_PATH）
  └─ add_subdirectory(cext)
       └─ cext/CMakeLists.txt
            ├─ _cext_static（STATIC 静态库，复用给多个目标）
            └─ _cext（MODULE，即 Python 扩展，链接 _cext_static）
```

为什么可编辑构建要用**符号链接**而不是复制？因为复制是「一次性快照」——你之后改了 C++ 源码、用 `make -C build` 重编出的新 `.so`，源码树里的副本不会更新，Python 还是 import 到旧的。而符号链接指向 `build/` 里的真身，重编后立即生效。这正是 README 说的「`pip install -e .` 只需一次，之后改 C++ 用 `make -C build` 即可」（见 [README.md:119-124](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/README.md#L119-L124)）。

#### 4.4.3 源码精读

**第一步：`setup.py` 声明扩展并注册自定义命令。**

[setup.py:97-104](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/setup.py#L97-L104) —— `setup()` 只声明了一个扩展 `Extension("cuda.tile._cext", [])`（源码列表为空，因为实际源码由 CMake 管），并用 `cmdclass` 把 `build_ext` 换成 `BuildExtWithCmake`。这就是「Python 打包钩子 → CMake」的连接点。空源码列表是关键信号：setuptools 只负责「登记有这么一个扩展」，真正的编译交给 CMake。

**第二步：`BuildExtWithCmake` 在配置阶段调用 CMake。**

[setup.py:41-52](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/setup.py#L41-L52) —— `_cmake` 拼出 `cmake -B <build_dir> <project_root>` 命令，把 `DLPACK_PATH`、`XLA_PATH`、构建类型、Python 解释器都作为 `-D` 参数传入；两个用户选项 `DISABLE_INTERNAL`、`ENABLE_DEV_FEATURES` 也在这里透传给 CMake。

**第三步：编译。**

[setup.py:30-37](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/setup.py#L30-L37) —— `_make` 在非 Windows 下跑 `cmake --build <build_dir> --parallel <n>`，Windows 下用 `msbuild`。

**第四步：把产物链接/复制进源码树（可编辑安装的关键）。**

[setup.py:54-80](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/setup.py#L54-L80) —— `run()` 决定 `build_dir`（可编辑模式下用项目根的 `build/`，否则用 `build_temp`；可被环境变量 `CUDA_TILE_CEXT_BUILD_DIR` 覆盖），然后对每个扩展把产物落到源码树。关键两行：

```python
# Create a symlink to the build directory if in editable mode, otherwise copy
link = "sym" if self.editable_mode else None
file_util.copy_file(ext_build_path, ext_path, update=1, link=link,
                    dry_run=self.dry_run)
```

可编辑模式下 `link="sym"` → 符号链接；普通模式 `link=None` → 复制。`_get_csrc_dir` 把扩展名 `cuda.tile._cext` 映射到构建目录里的 `cext` 子目录（见 [setup.py:83-86](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/setup.py#L83-L86)），`_get_build_lib_filename` 给出真实文件名 `lib_cext.so`（见 [setup.py:89-94](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/setup.py#L89-L94)）。

**第五步：顶层 CMake 的准备。**

[CMakeLists.txt:70-71](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/CMakeLists.txt#L70-L71) —— `find_package(Python ...)` 与 `find_package(CUDAToolkit REQUIRED)`：找到 Python 开发头和 CUDA Toolkit，这是编译 C++ 桥接的两个硬依赖。

[CMakeLists.txt:78-91](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/CMakeLists.txt#L78-L91) —— DLPack 的处理：若设了 `DLPACK_PATH`（即环境变量 `CUDA_TILE_CMAKE_DLPACK_PATH`）就用本地的，否则用 `FetchContent` 从 GitHub 拉 `dlpack v1.1`。这对应 README「会自动下载 DLPack」的说法，也为离线/内网构建留了口子。

[CMakeLists.txt:96](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/CMakeLists.txt#L96) —— `add_subdirectory(cext)`：把构建委派给 `cext/CMakeLists.txt`。

顶层 CMake 还提供一个手动开发期快捷命令 `devinstall`（[CMakeLists.txt:73-75](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/CMakeLists.txt#L73-L75)），就是直接 `ln -fs` 把 `.so` 链进源码树——和上面 `link="sym"` 等价的手动版本。另外，`internal/` 目录在开源仓库里通常不存在，但构建不会失败，因为 [CMakeLists.txt:99-108](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/CMakeLists.txt#L99-L108) 用 `if(EXISTS ... internal/CMakeLists.txt)` 守卫，存在才 `add_subdirectory(internal)`，否则跳过。

**第六步：cext 真正的编译规则。**

[cext/CMakeLists.txt:47-57](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/cext/CMakeLists.txt#L47-L57) —— 先编一个静态库 `_cext_static`，把 9 个 `.cpp`（`coroutine_util`、`cuda_loader`、`cuda_helper`、`memory`、`py`、`stream_buffer`、`tile_kernel`、`xla_ffi`、`xla_ffi_py`）打包进去。先做静态库是为了让多个目标（扩展模块、共享库、测试）复用同一份编译产物。

[cext/CMakeLists.txt:66-76](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/cext/CMakeLists.txt#L66-L76) —— 再编真正的 Python 扩展：`add_library(_cext MODULE module.cpp)`，并链接 `_cext_static`。`MODULE` 类型库就是 Python 可 `import` 的扩展模块。

**`_cext.pyi`：桥接接口的「合同」**

`cext/` 里这些 C++ 函数对 Python 暴露成什么样子？看类型存根：

[src/cuda/tile/_cext.pyi:13-18](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_cext.pyi#L13-L18) —— `def launch(stream, grid, kernel, kernel_args, /)`：这就是 u1-l1/u1-l2 里 `ct.launch` 最终落到的地方，由 C++ 实现，负责把 kernel 真正交给 `cuLaunchKernel`。

[src/cuda/tile/_cext.pyi:55-91](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_cext.pyi#L55-L91) —— `TileDispatcher`、`TileContext`、`CallingConvention.cutile_python_v1()` 等。这些类与方法都是 C++ 实现、Python 调用，正是「桥接」的明证。尤其是 `CallingConvention.cutile_python_v1()`，它对应 u1-l1 讲过的那个调用约定。

把 `_cext.pyi` 和 `cext/*.cpp` 对应起来看：`launch` ↔ `tile_kernel.cpp`、`cuda_loader.cpp`（加载 cubin、启动 kernel）；`xla_ffi.cpp`/`xla_ffi_py.cpp` ↔ JAX 互操作；`py.cpp` ↔ Python C API 辅助。`_cext` 就是把这些 C++ 能力「打包成一个 Python 模块」的桥。

#### 4.4.4 代码实践

**实践目标**：跑通一次可编辑构建，亲手摸到那个符号链接，并复述「一次 `pip install -e .` 经历了哪些构建步骤」。

**操作步骤**（承接 u1-l2，假设已具备 CUDA Toolkit 13.1+ 与 nvcc）：

1. 在仓库根目录执行 `pip install -e .`，观察终端里出现 `cmake -B build ...`（配置）和 `cmake --build build ...`（编译）两段输出。
2. 安装完成后执行 `ls -l src/cuda/tile/_cext.so`，看它是不是符号链接（`->` 指向 `build/cext/lib_cext.so` 之类，文件类型显示为 `l`）。
3. 执行 `python -c "import cuda.tile._cext as c; print(c.dev_features_enabled())"`，确认扩展可加载。
4. 回到源码：读 [setup.py:54-80](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/setup.py#L54-L80) 把 `run()` 的步骤写下来，与本步观察到的两段 CMake 输出对应。

**需要观察的现象**：你会看到构建职责被清晰三层切分——`setup.py` 管「Python 打包钩子 + 符号链接」、顶层 `CMakeLists.txt` 管「找依赖」、`cext/CMakeLists.txt` 管「编 C++」，没有哪一层越界。`_cext.so` 显示为 `l`（link）类型。

**预期结果**：symlink 存在；`import` 成功并打印 `True` 或 `False`（取决于是否启用开发特性，开源默认构建通常是 `False`）；能口述出「`pip install -e .` → setuptools 调 `BuildExtWithCmake.run` → `cmake -B build` 配置（找 Python/CTK/DLPack/XLA）→ `cmake --build build` 编出 `_cext.so` → 符号链接到 `src/cuda/tile/` 下」这条完整链路。

> 待本地验证：改一处 C++ 后只需 `cmake --build build`（或 `make -C build`）增量重编，符号链接自动指向新 `.so`，`import cuda.tile` 即用上新产物——这正是 editable symlink 的设计意图。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `cext/CMakeLists.txt` 要先编一个 `_cext_static` 静态库，再编 `_cext` MODULE？

> **参考答案**：为了让同一份编译产物被多个目标复用。`_cext_static` 把 9 个公共 `.cpp` 编成静态库，`_cext`（Python 扩展）、`_cext_shared`（带 `--no-undefined` 检查的共享库）、以及几个 C++ 测试可执行文件（`test_stream_buffer` 等，见 [cext/CMakeLists.txt:101-126](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/cext/CMakeLists.txt#L101-L126)）都链接它，避免重复编译、保证符号一致。

**练习 2**：可编辑构建用符号链接而不是复制，解决了什么痛点？

> **参考答案**：复制是一次性快照，之后用 `make -C build` 重编出的新 `.so` 不会反映到源码树，Python 仍 import 旧版；符号链接指向 `build/` 里的真身，重编后立即生效，因此 `pip install -e .` 只需做一次。

**练习 3**：Python 端调用 `ct.launch(...)` 最终落到哪个 C++ 实现入口？

> **参考答案**：落到 `cext/tile_kernel.cpp` 与 `cext/cuda_loader.cpp` 等 C++ 源（经 `_cext` 扩展暴露）。`_cext.pyi` 里的 `launch` 签名就是这份 C++ 实现的 Python 侧「合同」。

---

## 5. 综合实践

把本讲四个模块融会贯通，完成本讲指定的实践任务：**画出一张从 `src/cuda/tile/__init__.py` 出发的模块依赖树，标注每个子目录属于「前端 / 优化 / 后端 / 运行时」中的哪一类**。

**任务要求**：

1. 以 `cuda.tile.__init__` 为根，向下展开它直接 `import` 的模块/子包（参考 4.2.4 的清单）。
2. 对树上的每个叶子（子包或内部模块），标注四类之一；遇到 `_ir/`、`_compile.py` 这种横跨多类的，标成「贯穿（骨架）」并说明原因。
3. 在图上单独标出 `_cext` 节点，并画一条虚线连到 `cext/*.cpp`，表示「这是 C++ 桥接，不是纯 Python」。
4. 用一句话总结：cuTile 的「纯 Python 编译器」与「C++ 运行时桥接」在这张树上是怎么衔接的。

**参考要点**（不是唯一答案）：

- 根 `cuda.tile` 直接依赖：`_cext`（运行时/C++ 桥）、`_stub`（前端/API）、`_datatype`/`_numeric_semantics`（前端/类型语义）、`_memory_model`/`_by_target`（运行时/内存模型）、`_execution`（运行时/装饰器+launch）、`_context`（运行时/TileContext）、`_exception`、`tune`（运行时/调优）、`compilation`（后端/AOT 导出）。
- 藏在更深处、不在顶层 import 的：`_passes`（前端降级 + 优化 pass 并存）、`_ir`（贯穿的 IR 骨架）、`_ir2bytecode` + `_bytecode`（后端）、`_compile`（贯穿的流水线编排）、`_cache`（后端/缓存）、`_load_libcuda`（运行时/加载 tileiras）。
- `_cext` 虚线连到 `cext/`：`tile_kernel.cpp`/`cuda_loader.cpp`（launch 与 cubin 加载）、`xla_ffi*.cpp`（JAX 桥）、`py.cpp`（Python C API 辅助）、`memory.cpp`/`stream_buffer.cpp`/`coroutine_util.cpp`（基础设施）。
- 衔接方式：纯 Python 编译器（前端/优化/后端）在 JIT 时产出 cubin 字节码，交给 `_cext` 这个 C++ 桥加载并经 `cuLaunchKernel` 发射到 GPU——`_cext` 是 Python 世界与 CUDA Driver 之间唯一的通道。

> 提示：这张树画完，你就拥有了后续 U5（前端）、U6（优化）、U7（后端）、U8（运行时）所有讲义的「总目录」。之后每篇讲义深入某个文件时，都可以回到这张树上定位。

## 6. 本讲小结

- cuTile 仓库顶层分家清晰：`src/` 是纯 Python 编译器源码，`cext/` 是 C++ 运行时桥接源码，`docs/`/`samples/`/`test/` 是文档/示例/测试，`experimental/` 是独立打包的实验性功能。
- `src/cuda/tile/` 的子包可归为「前端 / 优化 / 后端 / 运行时」四类：`_passes`（前端降级 + 优化 pass 并存）、`_ir`（贯穿三类的 IR 骨架）、`_bytecode`/`compilation`/`_cache`（后端）、`_execution`/`_dispatch_mode`/`_context`/`tune`/`jax`/`_cext`（运行时）。
- `pyproject.toml` 用显式 `include` 清单决定打包哪些子包（`_ir`、`_passes`、`_bytecode`、`compilation`、`tune`、`jax` 在列，`experimental/`、`test`、`samples` 不在），版本号从 `VERSION` 文件动态读取，单一事实来源。
- C++ 扩展构建是三层接力：`setup.py` 的 `BuildExtWithCmake` 拦截 `build_ext` → 顶层 `CMakeLists.txt` 找 Python/CTK/DLPack/XLA → `cext/CMakeLists.txt` 先编静态库再编 `_cext` MODULE。
- 可编辑构建用「符号链接」（`link="sym"`）把 `build/` 里的 `.so` 链回源码树，使 `pip install -e .` 只需一次、之后改 C++ 用 `make -C build` 即可生效。
- `cext`（`_cext` 扩展）是 Python 运行时与 CUDA Driver API 之间唯一的桥，`launch`/`TileDispatcher`/`TileContext`/`CallingConvention` 等 C++ 能力都经它暴露给上层。

## 7. 下一步学习建议

有了这张「仓库地图 + 构建链」的方位感，接下来有几条路：

- **想先把 API 用熟**：进入 **U2 执行模型与数据模型** 与 **U3 编写内核：用户 API 实战**，在 4.2 标注的「前端 / 用户 API」模块（`_stub.py`、`_datatype.py` 等）基础上系统学 `load/store`、算术、控制流、归约、matmul。
- **想先吃透顶层 API 全貌**：读 **u1-l4 顶层 API 全景：cuda.tile 公共接口**，把 `__init__.py` 导出的全部符号分类成速查表——它正好是本讲 4.2 里「前端 API」那一格的展开。
- **想直接看编译器内部**：在跑通几个内核后再进 **U5 编译前端**、**U6 优化 pass**、**U7 后端**，届时本讲的子包归类表就是你的导航——后续每一篇讲义的 `source_files` 都会落在这张地图的某个节点上。
