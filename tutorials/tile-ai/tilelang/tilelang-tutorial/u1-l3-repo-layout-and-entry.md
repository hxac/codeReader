# 仓库目录结构与包入口

## 1. 本讲目标

学完本讲后，你应当能够：

- 画出 tilelang 仓库的整体目录地图，说清楚 `tilelang/`（Python 包）、`src/`（C++ 引擎）、`3rdparty`（依赖）、`docs`、`examples`、`testing` 各自的角色。
- 找到 Python 侧与 C++ 侧一一对应的子系统（如 `tilelang/transform` ↔ `src/transform`、`tilelang/tileop` ↔ `src/op`、`tilelang/cuda` ↔ `src/cuda`），建立「前后端镜像」的直觉。
- 解释 `import tilelang` 时发生了什么：轻量导入（light import）的判定、`TL_LIBS` 原生库搜索根的计算、`libinfo.find_lib_path` 跨平台定位 `libtilelang.so`、ctypes 加载。
- 定位三类公共入口：DSL 入口（`tilelang.language`）、编译入口（`tilelang.engine.lower`）、对外 API（`jit` / `compile` / `language` / `autotune` / `Profiler`）。

本讲承接 [u1-l2 安装与构建](u1-l2-installation-and-build.md)：上一讲解决了「源码如何变成可 import 的包」，本讲解决「import 之后，包里到底装了什么、C++ 引擎藏在哪里」。

## 2. 前置知识

- **Python 包（package）**：一个含有 `__init__.py` 的目录。`import tilelang` 实际执行的就是 `tilelang/__init__.py` 里的代码。
- **描述符（descriptor）**：实现了 `__get__` / `__set__` 的类，当它作为另一个类的属性时，访问该属性会被拦截。本讲会看到 `EnvVar` 描述符如何把「读环境变量」这件事集中管理。
- **原生库（native library）**：编译产物 `libtilelang.so`（Linux）/ `libtilelang.dylib`（macOS）/ `tvm_compiler.dll`（Windows 特例）。它由 `src/` 下的 C++ 编译而来，是真正的编译器引擎；Python 侧只是它的「壳」。
- **FFI（Foreign Function Interface）**：Python 与 C++ 之间的调用桥。tilelang 复用 TVM 的 `tvm_ffi` 机制，把 C++ 里 `TVM_REGISTER_GLOBAL("tl.xxx")` 注册的函数暴露给 Python。
- **数据流（data flow）回顾**：`Python DSL 函数 → TVM TIR → Pass 流水线 → host/device 拆分 → 设备代码生成 → Kernel Adapter → 可调用 kernel`。本讲会把这条链路「贴」到具体的目录上。

> 术语提示：「light import / 轻量导入」指跳过加载 C++ 引擎和重模块的精简 import 模式；「dev 构建」指直接从源码树运行（而非 pip 安装版）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/__init__.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py) | 包入口。计算版本号、初始化日志、加载 native 库、导出公共 API。 |
| [tilelang/env.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py) | 环境变量与路径的集中管理：`EnvVar` 描述符、`Environment` 类、`TL_LIBS` 计算、CUDA/ROCm 探测。 |
| [tilelang/libinfo.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/libinfo.py) | 跨平台定位 native 库：`find_lib_path` 按平台拼库名并在 `TL_LIBS` 中搜索。 |
| [tilelang/_ffi_api.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/_ffi_api.py) | FFI 初始化：调用 `tvm_ffi.init_ffi_api("tl", ...)` 把 C++ 侧 `tl.*` 注册函数挂到 Python。 |

辅助理解的目录：`tilelang/language/`、`tilelang/engine/`、`tilelang/jit/`、`tilelang/backend/`、`tilelang/transform/`、`tilelang/tileop/`、`src/`（C++ 引擎）、`3rdparty/`、`docs/`、`examples/`、`testing/`。

## 4. 核心概念与源码讲解

### 4.1 仓库目录结构与 Python↔C++ 对应

#### 4.1.1 概念说明

tilelang 本质是一个**编译器**，但它同时拥有「用户面」（Python DSL）和「引擎面」（C++ 编译流水线）。这两面在仓库里分别住在两个目录：

- `tilelang/`：Python 包，面向用户。包含 DSL 语法糖、JIT 装饰器、编译入口的 Python 包装、缓存、profiler、工具链、各后端 language 扩展。
- `src/`：C++ 源码，面向引擎。包含 Pass 实现、tile op lowering、各后端代码生成、运行时、JIT 头模板。

理解 tilelang 的关键，是建立「**Python 子目录与 C++ 子目录镜像**」的心智模型：你在 Python 侧调用的某个高层 API，最终几乎都能在 `src/` 下找到对应的 C++ 实现。这是因为大部分重活（IR 变换、代码生成）都在 C++ 里做，Python 只负责拼装和调度。

仓库根目录还有几个辅助目录：

| 目录 | 角色 |
| --- | --- |
| `3rdparty/` | 依赖子模块：`tvm`（核心 IR 框架）、`cutlass`（NVIDIA 矩阵乘库）、`composable_kernel`（AMD 矩阵乘库）、`hip-headers`（ROCm 头）。 |
| `docs/` | 文档源（含 `get_started`、`programming_guides`、`compiler_internals`、`developer_guide`、`tools`、`tutorials`）。 |
| `examples/` | 大量可运行示例（`gemm/`、`flash_attention/`、`deepseek_mla/`、`elementwise/` 等）。 |
| `testing/` | 测试：`testing/python`（pytest 正确性/性能）、`testing/cpp`（C++ 单测）。 |
| `benchmark/`、`maint/`、`docker/`、`cmake/` | 性能基准、维护脚本、容器定义、CMake 辅助。 |

#### 4.1.2 核心流程：前后端镜像表

下表把 `tilelang/`（Python）与 `src/`（C++）的主要子系统一一对应。阅读源码时，按这张表在两侧来回跳转，就能看清「Python 调用 → C++ 落地」的全貌。

| 职责 | Python 侧 (`tilelang/`) | C++ 侧 (`src/`) | 说明 |
| --- | --- | --- | --- |
| DSL 语法 | `language/` | `op/`（部分） | DSL 入口；用户写的 `T.copy`/`T.gemm` 等，最终在 `src/op` 里有对应 op lowering。 |
| tile op 注册 | `tileop/` | `op/` | `tileop/gemm/registry.py` 与 `src/op/gemm.cc` 对应：Python 注册 → C++ dispatch。 |
| 编译入口 | `engine/` | （编排层，调用 transform/codegen） | `engine/lower.py` 是编译器总入口。 |
| Pass 体系 | `transform/` | `transform/` | `transform/`（39 个 `.cc` Pass）是编译流水线主体。 |
| 后端代码生成 | `backend/`、`cuda/`、`rocm/`、`metal/`、`cpu/`、`webgpu/` | `cuda/`、`rocm/`、`metal/`、`cpu/`、`webgpu/` | 每个硬件后端两侧都有同名目录。 |
| JIT 与适配器 | `jit/`（含 `adapter/`） | `runtime/`、`tl_templates/` | `adapter/` 把编译产物包成可调用对象；模板在 `src/tl_templates/{cpp,cpu,cuda,hip}`。 |
| 调优 | `autotuner/`、`carver/` | （主要是 Python） | autotune 配置搜索、carver 切分推荐。 |
| 工具链 | `tools/`、`utils/` | `support/` | lower_trace、pass_visualizer、plot_layout、Analyzer 等调试工具。 |

特别要注意三个「纯 Python」目录（没有对应 C++）：

- `tilelang/language/`：DSL 语法糖。其中 `language/eager/`（builder）、`language/parser/`（AST 解析）、`language/overrides/` 负责「把用户写的 Python 函数变成 TVM TIR」。这是 u5 单元的核心。
- `tilelang/carver/`：tile 切分推荐框架，几乎是纯 Python（含 arch/template/roller）。
- `tilelang/autotuner/`：调优循环，纯 Python 编排（底层调 `jit.par_compile`）。

而三个「几乎纯 C++」的目录（Python 侧只是薄包装）：

- `src/transform/`：39 个 Pass，是编译质量的核心。
- `src/op/` 与 `src/cuda/op/` 等：tile op 的 lowering 实现。
- `src/tl_templates/`：被注入生成代码的 C++/CUDA/HIP 模板头（如 `reduce.h`、CuTe 调用）。

#### 4.1.3 源码精读：包入口如何编排各子系统

包入口 `tilelang/__init__.py` 通过一组 `from . import ...` 把这些子系统暴露出来。先看轻量导入开关——只有非轻量模式才执行重导入：

[文件路径:tilelang/__init__.py:159-167](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L159-L167) —— `if not env.is_light_import():` 之后，进入 `_lazy_load_lib()` 上下文，加载 native 库、导入 tvm、并按子系统导入各模块。

真正的「子系统导出清单」在这里：

[文件路径:tilelang/__init__.py:203-220](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L203-L220) —— 一次性导出 `analysis / transform / language / engine / tools / tileop`，以及各后端 `cpu / cuda / rocm / metal`。这张导入表正是「Python 侧子系统清单」的最权威来源：你在这里看到的每一个名字，都对应一个完整子目录。

这条导入链的最后一段还把调试钩子也接上：

[文件路径:tilelang/__init__.py:222-225](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L222-L225) —— 若 `TL_LOWER_TRACE` 开启，则在导入完成时自动启用 lower trace。这说明 `env`（环境变量层）在导入流程中起到「总开关」作用。

#### 4.1.4 代码实践：手工绘制对应表

1. **实践目标**：亲手把 Python↔C++ 镜像表填满，建立空间感。
2. **操作步骤**：
   - 列出 `tilelang/` 下所有一级子目录：`ls tilelang/`。
   - 列出 `src/` 下所有一级条目：`ls src/`。
   - 为每个 Python 子目录，到 `src/` 下找同名或同义的 C++ 目录，填进一张表。
3. **需要观察的现象**：哪些 Python 目录在 `src/` 下「找不到对应」（如 `language/`、`carver/`、`autotuner/`）？哪些 C++ 目录在 Python 侧只是薄包装（如 `transform/`）？
4. **预期结果**：得到一张类似 4.1.2 的表。注意 `tilelang/transform/`（只有 7 个文件）与 `src/transform/`（39 个 `.cc`）的体量差异——这正是「Python 薄包装 + C++ 重实现」的证据。
5. 待本地验证：具体文件数以你的 checkout 为准。

#### 4.1.5 小练习与答案

**练习 1**：用户调用 `T.gemm(...)` 时，最终会落到 `src/` 下的哪个文件？
**答案**：`src/op/gemm.cc`（注册与 dispatch）以及 `src/cuda/op/gemm.cc`（CUDA 后端的 lowering）。Python 侧对应 `tilelang/tileop/gemm/registry.py`。

**练习 2**：为什么 `tilelang/language/` 在 `src/` 下没有同名目录？
**答案**：因为 DSL 语法糖（`T.copy`、`T.gemm`、`T.alloc_shared` 等）的目的是把 Python 函数体翻译成 TVM TIR，这个「翻译」工作（eager builder、AST parser）天然是纯 Python；翻译完得到 TIR 后，才交给 `src/` 下的 C++ Pass 处理。

### 4.2 包入口与 native 库加载

#### 4.2.1 概念说明

`tilelang/__init__.py` 是整个包的「引导程序（bootstrap）」。它要做四件事，顺序很重要：

1. **算版本号**：决定 `tilelang.__version__`（开发版用 VERSION 文件 / version_provider，安装版用 importlib.metadata）。
2. **初始化日志**：装一个 Tqdm 友好的 handler，避免日志破坏进度条。
3. **加载 native 库**：通过 `libinfo.find_lib_path` 找到 `libtilelang.so`，用 ctypes 载入，让 `tvm_ffi` 能调用 C++ 引擎。
4. **导出公共 API**：把 `jit`、`compile`、`language`、`Profiler`、`autotune`、`lower` 等挂到 `tilelang` 命名空间。

其中有个关键分支：**轻量导入（light import）**。某些场景（如 `python -m tilelang.autodd` 自动求导 CLI）只需要 Python 环境、不需要加载 C++ 引擎。此时跳过步骤 3 和大部分步骤 4，让 import 极快、且能在没有 GPU/没有编译产物的机器上跑通。

#### 4.2.2 核心流程：import 的判定与加载

用伪代码描述 `__init__.py` 的主干：

```
计算 __version__（开发版 / 安装版 / dev 兜底）
from .env import env                      # 先导入环境层（见 4.3）

if env.is_light_import():
    跳过日志初始化、跳过 native 库加载、跳过重模块导入
    仅安装 pass_diff_hook
else:
    初始化 logger
    with _lazy_load_lib():                # 预载 torch、设置 RTLD_LAZY
        导入 env 的 cache 控制 API
        导入 libinfo
        （Windows：补 DLL 搜索路径）
        导入 tvm
        if env.SKIP_LOADING_TILELANG_SO == "0":
            _LIB, _LIB_PATH = _load_tile_lang_lib()   # ctypes.CDLL(find_lib_path("tilelang"))

    导出 jit/compile/Profiler/language/engine/transform/... 公共 API
    if env.get_lower_trace_mode(): 启用 lower trace
```

native 库的定位由 `libinfo.find_lib_path("tilelang")` 完成，它跨平台拼出库名，再在 `TL_LIBS`（由 `env.py` 计算的一组搜索根）里逐个查找。命中后用 `ctypes.CDLL` 载入。

#### 4.2.3 源码精读

**（a）轻量导入开关**：`env.is_light_import()` 决定整条重导入链是否执行。

[文件路径:tilelang/__init__.py:100-106](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L100-L106) —— 即使在重导入之前，`from .env import env` 总是会执行（环境层必须最先就绪）；随后用 `if not env.is_light_import()` 守卫日志初始化。

[文件路径:tilelang/env.py:512-521](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L512-L521) —— `is_light_import()` 当前等价于「是否在 `python -m tilelang.autodd` 下运行」。这是一个扩展点：未来若有其它「只需最小环境」的脚本，只需让此函数返回 True。

**（b）native 库加载**：

[文件路径:tilelang/__init__.py:183-190](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L183-L190) —— `_load_tile_lang_lib` 调 `libinfo.find_lib_path("tilelang")` 拿到 `.so` 路径并 `ctypes.CDLL` 载入；并由 `SKIP_LOADING_TILELANG_SO` 控制（默认 `"0"` 即加载）。这个开关允许「无 GPU / 无库」机器也能 import（stub 模式）。

**（c）跨平台定位库名**：

[文件路径:tilelang/libinfo.py:13-47](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/libinfo.py#L13-L47) —— `find_lib_path` 按平台拼库名：Linux/FreeBSD 为 `lib{name}.so`，macOS 为 `lib{name}.dylib`，Windows 为 `{name}.dll`。注意 Windows 对 `tilelang` 的特例：

[文件路径:tilelang/libinfo.py:29-32](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/libinfo.py#L29-L32) —— Windows 把 tilelang 的原生注册对象链接进 `tvm_compiler.dll` 而非单独的 `tilelang.dll`，所以 `find_lib_path("tilelang")` 在 Windows 实际找的是 `tvm_compiler.dll`。

**（d）公共 API 导出**：这是用户最常打交道的部分。

[文件路径:tilelang/__init__.py:192-198](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L192-L198) —— 导出 `jit`、`JITKernel`、`compile`、`par_compile`（JIT 层），以及 `Profiler`、`TensorSupplyType` 等。这是「编译入口」面向用户的门面。

[文件路径:tilelang/__init__.py:210-213](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L210-L213) —— 导出 `language.dtypes`（DSL 类型）、`autotune`（调优装饰器）、`PassConfigKey`（Pass 配置枚举）、以及 `lower`（编译器总入口）和若干 `register_*_postproc`（后端后处理钩子）。注意 `tilelang.language` 在 [第 206 行](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L203-L209) 作为子模块导入——这就是 **DSL 入口** `import tilelang.language as T` 的来源。

**（e）FFI 初始化**：native 库加载后，Python 怎么调用里面的函数？

[文件路径:tilelang/_ffi_api.py:1-6](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/_ffi_api.py#L1-L6) —— `tvm_ffi.init_ffi_api("tl", __name__)` 把 C++ 里所有 `TVM_REGISTER_GLOBAL("tl.xxx")` 注册的函数挂到这个模块。于是 `tilelang.transform._ffi_api`、`tilelang.tileop._ffi_api` 等子包各自调一次 `init_ffi_api`，就能拿到对应命名空间下的 C++ 函数。这就是「Python 薄包装调用 C++ 实现」的底层管道。

#### 4.2.4 代码实践：追踪 native 库从哪来

1. **实践目标**：亲眼看到你 `import tilelang` 时加载的那个 `.so` 文件路径。
2. **操作步骤**：
   - 在 Python 里执行：
     ```python
     import tilelang
     from tilelang import libinfo
     # 触发一次查找（库通常已被 __init__ 加载）
     print(libinfo.find_lib_path("tilelang"))
     ```
   - 在 shell 里确认该文件存在并属于 tilelang：`ls -l <打印出的路径>`。
   - 设置 `SKIP_LOADING_TILELANG_SO=1` 后再 `import tilelang`，观察是否还能 `import`（能 import，但跑 kernel 会失败）。
3. **需要观察的现象**：路径指向 `…/tilelang/lib/libtilelang.so`（安装版）还是 `…/build/lib/libtilelang.so`（dev 版）？`SKIP_LOADING_TILELANG_SO=1` 时 import 是否仍成功？
4. **预期结果**：安装版指向包内 `lib/`；dev 版指向仓库 `build/lib/`。跳过加载后 import 仍成功（证明 native 库不是 import 的硬依赖），但任何编译调用会因 `_LIB` 未初始化而报错。
5. 待本地验证：具体路径与平台相关（Windows 应得到 `tvm_compiler.dll`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `from .env import env` 必须在 `if not env.is_light_import()` 之前执行？
**答案**：因为判断「是否轻量导入」本身就需要 `env` 对象（`env.is_light_import()`）。环境层是最基础的依赖，必须最先就绪，才能用它来决定后续重导入是否发生。

**练习 2**：在 Windows 上，`import tilelang` 实际加载的 DLL 文件名是什么？为什么不是 `tilelang.dll`？
**答案**：是 `tvm_compiler.dll`。因为 Windows 构建把 tilelang 的原生注册对象链接进了 `tvm_compiler.dll`，没有单独的 `tilelang.dll`；`libinfo.find_lib_path` 对 `name == "tilelang"` 做了特例处理（见 `libinfo.py:29-32`）。

### 4.3 env.py：环境变量与路径的集中管理

#### 4.3.1 概念说明

一个真实的项目会用几十个环境变量（缓存目录、CUDA 路径、调试开关、超时……）。如果到处写 `os.environ.get(...)`，会出现：默认值散落各处、改不动、测试难覆盖、新人不知道有哪些开关。`tilelang/env.py` 用两个抽象解决它：

- **`EnvVar` 描述符**：把「一个环境变量的 key + 默认值」封装成一个对象。读它时优先返回强制覆盖（测试用），否则实时读 `os.environ`，否则返回默认值。所有变量的定义集中在 `Environment` 类里，一眼可见全貌。
- **`Environment` 类 + 全局 `env` 实例**：把所有 `EnvVar` 收拢成一个类，实例化为模块级 `env = Environment()`，全包共享。

`env.py` 还承担两件「物理」工作：

- **计算 `TL_LIBS`**：native 库的搜索根列表。安装版指向包内 `lib/`，dev 版指向仓库 `build/lib` 与 `build/tvm`。
- **探测 CUDA/ROCm 与依赖路径**：`_find_cuda_home`、`_find_rocm_home`，以及 CUTLASS / Composable Kernel / TVM / 模板的 include 路径初始化。

#### 4.3.2 核心流程

**`EnvVar` 描述符的读写规则**：

```
读 env.XXX:
    if 存在强制覆盖 _forced_value:  返回 _forced_value     # 测试/调试
    elif XXX in os.environ:         返回 os.environ[XXX]   # 实时读
    else:                           返回 default           # 可能是可调用对象

写 env.XXX = value:
    仅记下 _forced_value（默认不写回 os.environ）
```

注意「读」是**动态**的——每次访问都重新查 `os.environ`，所以进程运行中 `export` 改值能立即生效。

**`TL_LIBS` 的推导**（决定 native 库在哪）：

```
TL_ROOT = tilelang 包目录的绝对路径
if 包内存在 3rdparty/ 目录:           # pip 安装版：依赖打包在包里
    TL_LIBS = [<TL_ROOT>/lib]
else:                                 # dev 版：从源码树运行
    DEV = True
    TL_LIBS = [<repo>/build/lib, <repo>/build/tvm]
把 TL_LIBS 中的目录加入 sys.path（仅暴露 lib 目录，避免污染）
```

#### 4.3.3 源码精读

**（a）`TL_LIBS` 的两种来源**：

[文件路径:tilelang/env.py:47-64](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L47-L64) —— `TL_ROOT` 取包目录；通过「包内是否有 `3rdparty/`」区分安装版与 dev 版。安装版 `TL_LIBS = [TL_ROOT/lib]`；dev 版指向 `build/lib` 与 `build/tvm`，并打印 warning。这段代码直接回答了 u1-l2 遗留的问题：libinfo 到底在哪些根里找库。

**（b）`EnvVar` 描述符**：集中管理环境变量的核心机制。

[文件路径:tilelang/env.py:286-309](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L286-L309) —— `get()` 体现「强制覆盖 > os.environ > 默认值」的三级优先级；`__get__`/`__set__` 让它可作为类属性被自然读写。默认值支持可调用对象（用于依赖其它配置的延迟计算）。

**（c）`Environment` 类：所有开关的清单**。这里只看几类典型变量，体会「集中定义」的好处。

[文件路径:tilelang/env.py:360-372](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L360-L372) —— 内核构建相关：`TILELANG_PRINT_ON_COMPILATION`（编译时打印 kernel 名）、`TILELANG_DISABLE_CACHE`（禁用缓存，测试/调试用）、`TILELANG_CLEANUP_TEMP_FILES`（清理临时文件）、`TILELANG_COMPILE_TIMEOUT_SECONDS`（NVCC 超时）等。

[文件路径:tilelang/env.py:374-386](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L374-L386) —— 调试相关：`TILELANG_PASS_DIFF`（Pass IR diff）、`TL_LOWER_TRACE`（lower trace）、`TILELANG_PASS_PROFILE`（Pass 计时）。这些开关会在后续 u6（Pass 体系）、u9（调试工具）讲义里反复用到。

[文件路径:tilelang/env.py:394-401](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L394-L401) —— 编译默认值与 TVM 集成：`TILELANG_DEFAULT_TARGET`（默认 target，默认 `"auto"`）、`TILELANG_DEFAULT_EXECUTION_BACKEND`、`SKIP_LOADING_TILELANG_SO`（控制 native 库加载）。

**（d）从字符串到语义的方法**：`Environment` 不只是存值，还提供解读方法。

[文件路径:tilelang/env.py:477-491](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L477-L491) —— `get_lower_trace_mode()` 把 `TL_LOWER_TRACE` 的字符串（`"0"`/`"on"`/`"terminal"`/`"html"`/`"both"`）归一成 `None`/`"terminal"`/`"html"`/`"both"`。这类「字符串→语义」的归一方法是 `Environment` 的典型职责。

**（e）全局实例与缓存控制 API**：

[文件路径:tilelang/env.py:524-530](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L524-L530) —— `env = Environment()` 是全包共享的单例；`enable_cache`/`disable_cache`/`is_cache_enabled` 作为模块级函数导出，背后委派给 `env` 与 `CacheState`。所以 `tilelang.enable_cache()` 与 `tilelang.env.enable_cache()` 是同一回事。

**（f）CUDA/ROCm 与依赖路径探测**：

[文件路径:tilelang/env.py:141-190](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L141-L190) —— `_find_cuda_home` 按四级优先级猜 CUDA 安装路径：环境变量 `CUDA_HOME`/`CUDA_PATH` → PATH 上的 `nvcc` → pip 包 `nvidia-cuda-nvcc` → 平台默认路径。CUTLASS / Composable Kernel / TVM / 模板的 include 路径在 [env.py:610-632](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L610-L632) 初始化，缺则 warning。

#### 4.3.4 代码实践：环境变量速查与强制覆盖

1. **实践目标**：学会用 `env` 对象查询/覆盖环境变量，理解 `EnvVar` 描述符的行为。
2. **操作步骤**：
   ```python
   import tilelang
   from tilelang.env import env

   # 1. 查询：实时读 os.environ
   print("默认 target =", env.get_default_target())
   print("cache 启用？", env.is_cache_enabled())

   # 2. 强制覆盖（不污染真实环境）
   env.TILELANG_PRINT_ON_COMPILATION = "0"
   print("强制后 =", env.TILELANG_PRINT_ON_COMPILATION)

   # 3. 真实环境变量生效（动态读）
   import os
   os.environ["TILELANG_VERBOSE"] = "1"
   print("verbose =", env.get_default_verbose())   # 应为 True
   ```
3. **需要观察的现象**：步骤 2 的赋值是否真的改了 `os.environ`？（没有，只改了 `_forced_value`。）步骤 3 的 `export` 是否无需重新 import 就生效？（是，因为 `EnvVar.get` 每次实时读。）
4. **预期结果**：强制覆盖只影响当前 `env` 对象、不写回 `os.environ`；真实环境变量动态生效。这验证了 4.3.2 描述的读写规则。
5. 待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：你想为某个单元测试强制关闭 kernel 缓存，又不想影响其它测试。应该用 `os.environ["TILELANG_DISABLE_CACHE"] = "1"` 还是 `env.TILELANG_DISABLE_CACHE = "1"`？
**答案**：后者更安全。`env.XXX = value` 只在当前 `env` 对象上设 `_forced_value`，不污染 `os.environ`，测试结束后可清除；而写 `os.environ` 是进程级全局副作用。

**练习 2**：在 dev 版（源码树运行）下，`TL_LIBS` 包含哪两个目录？
**答案**：`<repo>/build/lib` 与 `<repo>/build/tvm`（见 `env.py:62`）。安装版则只有 `<包>/lib`。

## 5. 综合实践

把本讲三个模块串起来，做一次「import 全链路追踪」：

1. **画目录地图**：用 `ls` 浏览 `tilelang/` 与 `src/`，按 4.1.2 的格式填出属于你 checkout 的 Python↔C++ 对应表，并标注哪些是「纯 Python」、哪些是「C++ 重实现」。
2. **找三类入口**：在 [tilelang/__init__.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py) 中分别定位：
   - **DSL 入口**：`tilelang.language`（`import tilelang.language as T` 的来源）。
   - **编译入口**：`tilelang.engine.lower`（从 `.engine` 导入）。
   - **对外 API**：`jit` / `compile` / `par_compile`（从 `.jit` 导入）、`Profiler`、`autotune`。
   记录每个导出对应的源码行号。
3. **追 native 库**：写一段脚本，打印 `libinfo.find_lib_path("tilelang")` 的结果，并判断当前是安装版（`…/tilelang/lib/`）还是 dev 版（`…/build/lib/`）。再用 `SKIP_LOADING_TILELANG_SO=1` 验证「import 不依赖 native 库」。
4. **读一个环境开关**：选 `TILELANG_PRINT_ON_COMPILATION`，分别用 `env.XXX =` 与 `os.environ[...] =` 两种方式覆盖，用 `env.is_print_on_compilation_enabled()` 观察差异，验证 `EnvVar` 描述符的三级优先级。

完成后，你应当能用一句话回答：tilelang 的 Python 包是壳、C++ 引擎在 `src/`、两者通过 `tvm_ffi` 相连，而 `import` 引导过程由 `__init__.py` 编排、配置由 `env.py` 集中管理。

## 6. 本讲小结

- tilelang 仓库分两面：`tilelang/`（Python 包，用户面）与 `src/`（C++ 引擎，重实现面），二者通过 `tvm_ffi` 的 `tl.*` 注册函数相连。
- Python 侧与 C++ 侧存在系统级镜像：`transform↔transform`、`tileop↔op`、`cuda↔cuda`、`backend↔backend`；而 `language/`、`carver/`、`autotuner/` 是纯 Python，`transform/`（39 个 Pass）等是 C++ 重实现。
- `tilelang/__init__.py` 是引导程序：算版本→初始化日志→（非轻量模式）加载 native 库→导出公共 API。`is_light_import()` 控制是否跳过重导入。
- native 库定位靠 `libinfo.find_lib_path` 跨平台拼名 + 在 `env.TL_LIBS` 中搜索；Linux 为 `libtilelang.so`，Windows 特例为 `tvm_compiler.dll`。
- `env.py` 用 `EnvVar` 描述符 + `Environment` 类集中管理全部环境变量与路径（`TL_LIBS`、CUDA/ROCm、CUTLASS/CK、缓存与调试开关），并实例化为全局 `env` 单例。
- 三类入口的落点：DSL 入口 `tilelang.language`、编译入口 `tilelang.engine.lower`、对外 API `jit/compile/Profiler/autotune`，均在 `__init__.py` 的导入清单中。

## 7. 下一步学习建议

- 下一讲 [u1-l4 第一个 Kernel：Quickstart GEMM 实跑](u1-l4-quickstart-gemm.md) 会用本讲定位的入口（`@tilelang.jit`、`tilelang.language`）跑通第一个真实 kernel，把「包结构」变成「能跑的代码」。
- 想深入了解 DSL 如何变成 TIR，可预习 `tilelang/language/eager/`（builder）与 `tilelang/language/parser/`，这会在 u5 单元展开。
- 想了解 Pass 体系如何消费本讲提到的 `env` 调试开关（`TILELANG_PASS_DIFF`、`TL_LOWER_TRACE`），可先扫一眼 `tilelang/transform/pass_config.py`，u6 单元会精读。
- 建议先把本讲的「Python↔C++ 对应表」存档，后续每读一个子系统，都在表上画钩，作为贯穿全手册的导航图。
