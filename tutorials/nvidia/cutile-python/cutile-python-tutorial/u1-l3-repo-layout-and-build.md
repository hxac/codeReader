# 源码目录结构与构建系统

## 1. 本讲目标

前两讲我们建立了对 cuTile Python 的整体定位，并让它在本地跑了起来。本讲换一个视角：**把整个仓库当成一张地图来读懂**。读完本讲，你应当能够：

1. 看清 `src/cuda/tile` 下 `_ir`、`_passes`、`_bytecode`、`compilation`、`tune`、`jax` 等子包各自负责什么，并能对应到「前端 / 优化 / 后端 / 运行时」四个阶段。
2. 理解 `pyproject.toml` 的「src 布局 + 包发现规则」如何把源码变成一个可 `import cuda.tile` 的包。
3. 理解 `setup.py` 如何通过一个自定义的 `BuildExtWithCmake` 命令去驱动 CMake，把 `cext/` 下的 C++ 源码编译成 Python 扩展模块 `_cext`。
4. 理解 C++ 扩展 `_cext` 在整个架构里扮演的「桥接」角色——它把 Python 世界和 CUDA 驱动 / tileiras 编译产物连接起来。

> 本讲定位在 u1-l1 建立的「`AST → HIR → Tile IR → 字节码 → cubin → cuLaunchKernel`」全链路之上。这趟链路的每一站都对应仓库里的一个子包，本讲就是给这趟链路画一张「目录地图」。

## 2. 前置知识

本讲是面向「想读源码」的入门读者，几乎不要求你写过 cuTile 内核，但有几个概念最好先有印象：

- **Python 包（package）与模块（module）**：一个目录里有 `__init__.py` 就是一个包；`import cuda.tile` 时，Python 解释器会去寻找 `cuda/tile/__init__.py` 并执行它。
- **构建后端（build backend）**：`pip install` 一个包时，pip 会调用某个「构建后端」（如 `setuptools`）把源码打包成可安装的产物。本项目用 `setuptools`。
- **src 布局（src layout）**：把 Python 源码放在 `src/` 子目录下，而不是仓库根目录。它的好处是：只有「安装之后」才能 `import`，避免「在仓库根目录随手 import 到未安装的代码」这种隐蔽 bug。
- **C++ 扩展模块（C extension）**：用 C/C++ 写、编译成 `.so`（Linux）/ `.dll`（Windows）/ `.dylib`（macOS），可以被 Python 直接 `import`，常用于性能关键或调用系统库（这里是 CUDA 驱动）的场景。
- **CMake**：一个跨平台的 C/C++ 构建系统生成器，读 `CMakeLists.txt`，生成 `Makefile` 或工程文件，再编译。
- **可编辑安装（editable install）**：`pip install -e .`，把「安装后的包」指向你的源码目录，于是改源码立刻生效，无需重装。

这些概念在源码精读里会再结合真实文件解释一遍。

## 3. 本讲源码地图

本讲聚焦在「目录布局」和「构建脚本」这两类文件，不进入具体编译逻辑。

| 文件 | 作用 |
| --- | --- |
| [pyproject.toml](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/pyproject.toml) | 项目元信息 + 构建后端声明 + Python 包发现规则。 |
| [setup.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/setup.py) | 声明一个 C++ 扩展 `cuda.tile._cext`，并用自定义命令 `BuildExtWithCmake` 调用 CMake。 |
| [CMakeLists.txt](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/CMakeLists.txt) | 顶层 CMake：找 Python 与 CUDA Toolkit、拉取 dlpack/XLA 头文件、进入 `cext/` 子目录构建。 |
| [cext/CMakeLists.txt](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/cext/CMakeLists.txt) | C++ 扩展的真实编译规则：先编静态库 `_cext_static`，再编成 Python 模块 `_cext`。 |
| [src/cuda/tile/\_\_init\_\_.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/__init__.py) | 顶层包入口：把所有公开 API 聚合导出，是画「模块依赖树」的出发点。 |
| [cext/module.cpp](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/cext/module.cpp) | C++ 扩展的初始化入口 `PyInit__cext`，把 `tile_kernel`、`cuda_helper` 等子模块挂载进来。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先鸟瞰仓库目录（4.1），再讲 Python 侧的包发现（4.2），最后讲 C++ 扩展的 CMake 构建（4.3）。其中 4.2、4.3 对应规格里要求覆盖的两个最小模块。

### 4.1 仓库总体布局：目录到阶段的映射

#### 4.1.1 概念说明

u1-l1 我们说过，cuTile 把一个 `@ct.kernel` 函数编译成 cubin，要经过一条很长的流水线：

```
Python AST → HIR → Tile IR →（优化 pass）→ 字节码 →（tileiras）cubin → cuLaunchKernel
```

这条流水线不是写在一个巨型文件里，而是**按阶段拆成多个子包**。本讲最重要的收获，就是能看着目录名猜出它属于流水线的哪一站。我们把全部子包归到四类：

| 分类 | 含义 | 仓库位置 |
| --- | --- | --- |
| **前端 frontend** | 把 Python 代码解析成 IR | `src/cuda/tile/_passes/`（ast2hir、hir2ir）、`src/cuda/tile/_ir/`（IR 与 HIR 定义） |
| **优化 optimization** | 在 IR 上做变换 | `src/cuda/tile/_passes/`（dce、code_motion、loop_split、token_order 等） |
| **后端 backend** | 把 IR 变成可执行字节码/cubin | `src/cuda/tile/_bytecode/`、`src/cuda/tile/_ir2bytecode.py`、`src/cuda/tile/_compile.py` |
| **运行时 runtime** | 启动内核、缓存、桥接 GPU | `cext/`（C++ 扩展）、`src/cuda/tile/_context.py`、`src/cuda/tile/_cache.py`、`src/cuda/tile/_dispatch_mode.py` |

> 注意：`_passes/` 同时承担「前端（ast2hir/hir2ir）」和「优化（dce 等）」两类工作。这是 cuTile 的实际组织方式，不是我们分类不严谨。

仓库顶层还有一些与流水线无直接关系、但很重要的目录：

| 目录 | 作用 |
| --- | --- |
| `docs/` | Sphinx 文档源（`.rst`），即你看到的官方文档。 |
| `samples/` | 示例内核：`samples/quickstart/` 是快速入门，`samples/templates/` 是 MatMul/Attention/LayerNorm 等模板。 |
| `test/` | Python 测试套件，文件名通常就是被测特性（如 `test_dce.py`、`test_mma.py`）。 |
| `cext/` | C++ 扩展源码（含 `test/` 子目录的 C++ 单测）。 |
| `cmake/` | 自定义 CMake 模块（`FetchXLAHeaders.cmake`、`FindCUDAToolkit.cmake`）。 |
| `experimental/` | 实验性代码：`cuda-lang`（NVVM/SIMT 级实验语言）、`tile_experimental`（实验性 autotuner）。 |
| `internal/` | （通常不存在）NVIDIA 内部 overlay 目录，开源构建里会被 `DISABLE_INTERNAL` 跳过。 |

#### 4.1.2 核心流程

把目录读成地图，可以这样自上而下走：

1. **入口在 `src/cuda/tile/__init__.py`**：用户 `import cuda.tile as ct` 拿到的全部符号都从这里导出。
2. **顶层包里有一批「单文件模块」**，每个对应一个横切关注点：`_stub.py`（用户 API）、`_execution.py`（kernel 装饰器）、`_compile.py`（编译流水线）、`_datatype.py`（类型系统）、`_memory_model.py`（内存序）等。
3. **顶层包里有六个「子包目录」**，分别对应流水线的不同阶段（见 4.1.1 的表）。
4. **C++ 侧的 `cext/`** 不在 `src/` 里，而是单独成目录，由 CMake 编译后**链接（symlink）进** `src/cuda/tile/`，伪装成 `cuda.tile._cext`。
5. **`docs/`、`samples/`、`test/`、`experimental/`** 不参与安装，只是开发期资产。

#### 4.1.3 源码精读

顶层包入口里，你能直接看到「公开 API 来自哪些模块」，这就是画依赖树的起点。看 [src/cuda/tile/\_\_init\_\_.py:7](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/__init__.py#L7)：

```python
from cuda.tile._cext import launch
```

这一行说明：**用户能调用的 `ct.launch`，其实实现在 C++ 扩展 `_cext` 里**。Python 这层只是个壳。这是「运行时桥接」最直接的证据。

再看分类导出（节选自 [src/cuda/tile/\_\_init\_\_.py:58-174](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/__init__.py#L58-L174)）：

```python
from cuda.tile._stub import (        # 用户 API：load/store/算术/归约/...
    Array, Constant, Tile, TiledView, load, store, add, sum, mma, ...
)
from cuda.tile._context import compiler_timeout
from cuda.tile import tune
from cuda.tile._execution import (function, kernel)
import cuda.tile.compilation as compilation
```

可以读出：`_stub` 是用户 API、`tune`/`compilation` 是两个独立子包、`_execution` 给装饰器。第 5 节的综合实践会让你把这张依赖树完整画出来。

#### 4.1.4 代码实践

> **实践：用 `ls` 自行核对目录分类。**
>
> 1. **目标**：验证 4.1.1 表里的分类和你机器上的实际目录是否一致。
> 2. **步骤**：在仓库根目录执行 `ls -1 src/cuda/tile/`，逐个对照表格；再执行 `ls -1 src/cuda/tile/_passes/` 看「优化 pass」和「前端」文件混在一起的样子。
> 3. **需要观察的现象**：`_passes/` 下既有 `ast2hir.py`、`hir2ir.py`（前端），也有 `dce.py`、`code_motion.py`、`loop_split.py`、`token_order.py`（优化），印证「同一子包承担两类工作」。
> 4. **预期结果**：目录清单与本讲表格一致；`_bytecode/`、`compilation/`、`tune/`、`jax/` 四个子包都存在。
> 5. 这些命令是只读的，可以安全运行；若你的环境目录有差异，以实际仓库为准。

#### 4.1.5 小练习与答案

**练习 1**：有人说「`_ir/` 是后端，因为它输出 IR」。这个说法对吗？

> **答案**：不准确。`_ir/` 定义的是 Tile IR（以及 HIR）的**数据结构本身**（`IRContext`、`Builder`、`Var`、`Operation`、`type.py`），它被前端、优化、后端三阶段**共同使用**。真正「后端」是把 IR 序列化成字节码的 `_bytecode/` 和 `_ir2bytecode.py`。

**练习 2**：`samples/templates/MatMul.py` 里的内核，会经过哪些子包才变成 cubin？

> **答案**：用户代码经 `_stub.py` 的 API 写出 → `_execution.py` 的 `kernel` 装饰器捕获 → `_compile.py` 编译时调用 `_passes/`（ast2hir→hir2ir，再做 dce 等优化）→ `_bytecode/` 生成字节码 → tileiras 生成 cubin → `cext/`（`_cext`）通过 CUDA 驱动 `cuLaunchKernel`。

### 4.2 Python 包发现：setuptools 的 src 布局

（对应最小模块：**setuptools package find**）

#### 4.2.1 概念说明

「我写了一堆 `.py`，`pip` 怎么知道哪些要装、装成什么名字？」这就是**包发现（package discovery）**要解决的问题。本项目用 setuptools 的 `src` 布局：

- 所有 Python 源码放在 `src/` 下，包的「根」是 `src/cuda/`，主包是 `src/cuda/tile/`。
- `pyproject.toml` 里用 `[tool.setuptools.packages.find]` 告诉 setuptools：**从 `src/` 目录开始找包**，并且只挑白名单里列出的那些。

这种做法的好处是：包名（`cuda.tile`）和它在磁盘上的位置（`src/cuda/tile/`）解耦，仓库根目录保持干净。

#### 4.2.2 核心流程

1. `pip install` 读 `pyproject.toml`，得知构建后端是 `setuptools.build_meta`。
2. setuptools 读 `[tool.setuptools.packages.find]`，在 `where = ["src"]` 下扫描带 `__init__.py` 的目录。
3. 只有 `include` 白名单里列出的包（如 `cuda.tile._ir`、`cuda.tile._passes` …）会被打包；其余目录（如 `test`、`samples`）不会进入发行包。
4. 版本号动态来自 `src/cuda/tile/VERSION` 这个文本文件（`dynamic = ["version"]`）。
5. 安装后，`import cuda.tile` 就能命中 `src/cuda/tile/__init__.py`。

#### 4.2.3 源码精读

先看构建后端声明与版本来源 [pyproject.toml:5-10](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/pyproject.toml#L5-L10)、[pyproject.toml:50-51](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/pyproject.toml#L50-L51)：

```toml
[build-system]
build-backend = "setuptools.build_meta"
requires = [ "setuptools==80.10.2", "wheel" ]

[tool.setuptools.dynamic]
version = {file = "src/cuda/tile/VERSION"}
```

这两段说明：构建用 setuptools（锁版本 80.10.2），版本号直接读 `src/cuda/tile/VERSION`（内容是 `9.9.99` 这种纯文本）。这是「动态版本」模式。

接着是本模块的核心——包发现规则 [pyproject.toml:53-64](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/pyproject.toml#L53-L64)：

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

读懂这几点：

- `where = ["src"]`：从 `src/` 目录起步扫描。
- `include` 是白名单：**只列了 8 个包**。注意它**没有列** `cuda.tile.experimental`（在 `experimental/` 下，根本不在 `src/`），也没有列 `test`、`samples`——所以这些不会被打包发行。这正好印证了 4.1 节「`experimental/` 是实验性、不随主包安装」的说法。
- `cuda.tile._ir` 等以下划线开头的包是**内部包**，但因为被 `include` 了，会随发行包一起安装（只是约定不对外稳定）。

还有一个细节，把 `VERSION` 文件作为包数据打包 [pyproject.toml:66-67](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/pyproject.toml#L66-L67)：

```toml
[tool.setuptools.package-data]
"cuda.tile" = ["VERSION"]
```

没有这一行，动态版本读取在「已安装环境」里会找不到 `VERSION` 文件。

#### 4.2.4 代码实践

> **实践：验证「只有白名单包会被安装」。**
>
> 1. **目标**：亲眼看到 `include` 白名单的过滤效果。
> 2. **步骤**：
>    - 在已 `pip install -e .` 的环境里，执行 `python -c "import cuda.tile._ir; print('ok')"`，应能成功。
>    - 再执行 `python -c "import cuda.tile.experimental"`（或尝试 `import test`），预期失败——因为它们既不在 `src/`，也不在白名单。
>    - 如果想看构建时到底扫描到哪些包，可在仓库根目录临时运行 `python -m setuptools.discover` 或 `python setup.py --name` 观察元信息（待本地验证，命令可能随 setuptools 版本略有差异）。
> 3. **需要观察的现象**：白名单内子包可导入，白名单外（如 `experimental`）不可导入。
> 4. **预期结果**：`import cuda.tile._ir` 成功；`import cuda.tile.experimental` 报 `ModuleNotFoundError`。
> 5. 若你尚未安装包，可先执行 `pip install -e .`（参见 u1-l2）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `cuda.tile._ir` 从 `include` 里删掉，会发生什么？

> **答案**：发行包里不再包含 `_ir` 子包，安装后 `import cuda.tile._ir` 会失败。但由于 `src/` 布局下开发时（可编辑安装）常直接指向源码目录，行为可能因安装方式不同而异。正确做法是保留白名单。

**练习 2**：为什么版本号要单独放在 `VERSION` 文件里、用 `dynamic` 读取，而不是直接写 `version = "x.y.z"`？

> **答案**：把版本号外置成纯文本文件，便于 CI、`docs/conf.py`、`_version.py` 等多处用同一份来源读取（单一真相源，single source of truth），避免多处维护版本号导致不一致。

### 4.3 C++ 扩展构建：setup.py → CMake → cext

（对应最小模块：**cext CMake build**）

#### 4.3.1 概念说明

cuTile 不仅要编译用户的内核，还要**亲自调用 CUDA 驱动启动内核**（`cuLaunchKernel`）、做 JAX 的 XLA FFI 桥接。这些事 Python 干不了，必须用 C++ 写，编译成一个 Python 扩展模块 `_cext`。于是出现一个「鸡生蛋」的问题：

- Python 侧的 `cuda.tile` 由 setuptools 打包；
- C++ 侧的 `_cext` 要由 CMake 编译；
- 但用户只想敲一次 `pip install`。

解决办法是：**让 setuptools 在编译扩展时，去调用 CMake**。`setup.py` 里自定义一个 `build_ext` 子类 `BuildExtWithCmake`，把「编译 C++ 扩展」这件事整个委托给 CMake，再把 CMake 产出的 `.so` 链接/拷贝到 setuptools 期望的位置。

#### 4.3.2 核心流程

完整的 C++ 扩展构建链路：

```
pip install .
  └─ setuptools 走 build_ext 阶段
       └─ BuildExtWithCmake.run()
            ├─ 1. 决定 build_dir（editable → ./build；普通 → build_temp）
            ├─ 2. _cmake()：cmake -B <build_dir> <root>（配置）
            ├─ 3. _make()：cmake --build <build_dir>（编译）
            └─ 4. 把产出的 lib_cext.so 链接/拷贝到 cuda/tile/_cext.so
                 （editable 用 symlink，普通 install 用 copy）
```

CMake 侧（`CMakeLists.txt` → `cext/CMakeLists.txt`）：

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

#### 4.3.3 源码精读

**第一步：`setup.py` 声明扩展并注册自定义命令。** 见 [setup.py:97-104](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/setup.py#L97-L104)：

```python
setup(
    ext_modules=[
        Extension("cuda.tile._cext", []),   # 名为 cuda.tile._cext，无源文件（交给 CMake）
    ],
    cmdclass=dict(
        build_ext=BuildExtWithCmake,         # 用自定义命令替换默认 build_ext
    )
)
```

注意 `Extension("cuda.tile._cext", [])` 第二个参数是空列表——**源文件列表是空的**，因为真正的源文件由 CMake 管理。setuptools 这里只负责「登记有这么一个扩展」，实际编译交给 `BuildExtWithCmake`。

**第二步：`BuildExtWithCmake` 在配置阶段调用 CMake。** 见 [setup.py:41-52](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/setup.py#L41-L52)：

```python
def _cmake(self, build_dir, build_type, dlpack_path, xla_path):
    cmake_cmd = ["cmake", "-B", build_dir, project_root,
                 f"-DDLPACK_PATH={dlpack_path}",
                 f"-DXLA_PATH={xla_path}",
                 f"-DCMAKE_BUILD_TYPE={build_type}",
                 f"-DPython_EXECUTABLE={sys.executable}",
                 "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"]
    if self.disable_internal:
        cmake_cmd.append("-DDISABLE_INTERNAL=1")
    if self.enable_dev_features:
        cmake_cmd.append("-DENABLE_DEV_FEATURES=1")
    self.spawn(cmake_cmd)
```

`-B build_dir` 指定构建目录，`project_root` 是顶层 `CMakeLists.txt` 所在目录。两个用户选项 `-D` 直接透传给 CMake：`DISABLE_INTERNAL`（开源构建跳过 `internal/`）、`ENABLE_DEV_FEATURES`（开发期特性，如 dump）。

**第三步：编译。** 见 [setup.py:30-37](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/setup.py#L30-L37)：

```python
def _make(self, build_dir, build_type, parallel):
    if is_windows:
        self.spawn(["msbuild", f"{build_dir}/cuda-tile-python.sln", ...])
    else:
        self.spawn(["cmake", "--build", build_dir, "--parallel", str(parallel)])
```

非 Windows 用 `cmake --build`，Windows 用 `msbuild`。

**第四步：把产物链接进源码树（可编辑安装的关键）。** 见 [setup.py:54-80](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/setup.py#L54-L80)，关键几行：

```python
def run(self):
    build_dir = os.getenv("CUDA_TILE_CEXT_BUILD_DIR")
    if build_dir is None or build_dir == "":
        if self.editable_mode:
            build_dir = os.path.join(project_root, "build")   # editable → ./build
        else:
            build_dir = self.build_temp
    ...
    for ext in self.extensions:
        src_dir = _get_csrc_dir(ext.name)          # "cuda.tile._cext" → "cext"
        ext_name = _get_build_lib_filename(ext.name)  # → "lib_cext.so"
        ...
        link = "sym" if self.editable_mode else None
        file_util.copy_file(ext_build_path, ext_path, update=1, link=link, ...)
```

最后这段是「桥接」的精髓：

- `_get_csrc_dir` 把扩展名 `cuda.tile._cext` 映射到构建目录里的 `cext` 子目录（见 [setup.py:83-86](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/setup.py#L83-L86)）。
- `_get_build_lib_filename` 给出真实文件名 `lib_cext.so`（见 [setup.py:89-94](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/setup.py#L89-L94)）。
- `link = "sym"`：**可编辑安装时用符号链接**，把 `build/cext/lib_cext.so` 链到 `src/cuda/tile/_cext.so`。于是你只改 C++、跑 `make -C build` 增量重编，链接立即指向新 `.so`，**不用重装 Python 包**。这正是 u1-l2 提到「改 C++ 只需 `make -C build`」的底层原因。

**第五步：顶层 CMake 的准备。** 见 [CMakeLists.txt:70-71](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/CMakeLists.txt#L70-L71)、[CMakeLists.txt:78-96](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/CMakeLists.txt#L78-L96)：

```cmake
find_package(Python REQUIRED COMPONENTS Interpreter Development)
find_package(CUDAToolkit REQUIRED)
...
if (DLPACK_PATH)
    set(dlpack_INCLUDE_DIR "${DLPACK_PATH}/include/dlpack")
else()
    include(FetchContent)
    FetchContent_Declare(dlpack GIT_REPOSITORY https://github.com/dmlc/dlpack.git GIT_TAG v1.1)
    FetchContent_MakeAvailable(dlpack)
endif()
include(cmake/FetchXLAHeaders.cmake)
fetch_xla_headers()
add_subdirectory(cext)
```

读懂：构建必须找到 Python 开发头和 CUDA Toolkit；dlpack 与 XLA 头文件默认从网上拉取（除非你提供本地路径，这是为离线/内网构建留的口子）；最后 `add_subdirectory(cext)` 进入真正的扩展构建。

顶层 CMake 还提供了一个手动的开发期快捷命令 `devinstall`（[CMakeLists.txt:73-75](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/CMakeLists.txt#L73-L75)），就是直接 `ln -fs` 把 `.so` 链进源码树——和上面 `link="sym"` 等价的手动版本。

**第六步：cext 真正的编译规则。** 见 [cext/CMakeLists.txt:47-76](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/cext/CMakeLists.txt#L47-L76)：

```cmake
# 先编静态库，便于复用给多个目标
add_library(_cext_static STATIC
    coroutine_util.cpp cuda_loader.cpp cuda_helper.cpp memory.cpp
    py.cpp stream_buffer.cpp tile_kernel.cpp xla_ffi.cpp xla_ffi_py.cpp)

# 再编成 Python 扩展模块（MODULE）
add_library(_cext MODULE module.cpp)
target_link_libraries(_cext PUBLIC _cext_static)
```

要点：

- **静态库先行**：把九个 `.cpp` 编成 `_cext_static` 静态库，是为了让「Python 扩展」和「C++ 单测」**复用同一份编译产物**（cext 目录下还有 `test_stream_buffer`、`test_hash_map`、`test_vec` 三个 C++ 测试可执行文件，见 [cext/CMakeLists.txt:101-126](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/cext/CMakeLists.txt#L101-L126)）。
- **MODULE 类型**：`add_library(_cext MODULE ...)` 的 `MODULE` 关键字表示「可作为插件加载的共享库」，这正是 Python C 扩展需要的形态（而非普通 `SHARED`）。
- `module.cpp` 是扩展入口，里面定义了 `PyInit__cext`（见 [cext/module.cpp:37-59](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/cext/module.cpp#L37-L59)），依次调用 `tile_kernel_init`、`cuda_helper_init`、`coroutine_util_init`、`xla_ffi_init` 把各子模块挂进 Python 模块对象。`_cext.pyi` 就是这些 C++ 符号的 Python 类型存根。

#### 4.3.4 代码实践

> **实践：跑通一次可编辑构建，亲手摸到那个 symlink。**
>
> 1. **目标**：验证「`pip install -e .` 之后，`src/cuda/tile/_cext.so` 是一个指向 `build/` 的符号链接」。
> 2. **步骤**（承接 u1-l2，假设已具备 CUDA Toolkit 13.1+ 与 nvcc）：
>    - 在仓库根目录执行 `pip install -e .`，观察终端里出现 `cmake -B build ...` 和 `cmake --build build ...` 两段输出。
>    - 安装完成后执行 `ls -l src/cuda/tile/_cext.so`，看它是不是符号链接（`->` 指向 `build/cext/lib_cext.so` 之类）。
>    - 执行 `python -c "import cuda.tile._cext as c; print(c.dev_features_enabled())"`，确认扩展可加载。
> 3. **需要观察的现象**：`_cext.so` 显示为 `l`（link）类型；`import` 成功并打印 `True` 或 `False`（取决于是否启用开发特性）。
> 4. **预期结果**：symlink 存在；`dev_features_enabled()` 返回布尔值（开源默认构建通常是 `False`）。
> 5. 如果你只改了 C++，后续只需 `cmake --build build`（或 `make -C build`）增量重编，链接自动指向新产物，无需重装——这正是 editable symlink 的设计意图。

#### 4.3.5 小练习与答案

**练习 1**：为什么先编 `_cext_static` 静态库，而不是直接把所有 `.cpp` 编进 `_cext` 模块？

> **答案**：因为同一份 C++ 代码要被「Python 扩展 `_cext`」和「若干 C++ 单测可执行文件（`test_stream_buffer` 等）」共用。先编静态库，再让各目标链接它，能避免重复编译、保证一致性。这是 CMake 里很常见的「object/static library 复用」模式。

**练习 2**：`pip install -e .`（editable）和 `pip install .`（普通）在 C++ 扩展产物处理上有什么区别？

> **答案**：editable 模式下，`setup.py` 用 `link="sym"` 把 `.so` **符号链接**进源码树（`build_dir` 落在 `./build`），改 C++ 重编即可生效；普通模式下用 `link=None` **拷贝** `.so` 到 `build_temp`，每次都要重装才能更新。前者为开发，后者为发行。

**练习 3**：`internal/` 目录在开源仓库里通常不存在，构建会失败吗？

> **答案**：不会。[CMakeLists.txt:99-108](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/CMakeLists.txt#L99-L108) 用 `if(EXISTS ... internal/CMakeLists.txt)` 守卫，存在才 `add_subdirectory(internal)`，否则打印「Building without internal/」并跳过。`setup.py` 还提供 `--disable-internal` 选项主动设 `DISABLE_INTERNAL=1`。

## 5. 综合实践

把本讲三节的知识串起来，完成一张**模块依赖树**（这也是规格指定的实践任务）。

**任务**：从 [src/cuda/tile/\_\_init\_\_.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/__init__.py) 出发，画出一张依赖树，把 `src/cuda/tile/` 下每个文件/子目录归类到「前端 / 优化 / 后端 / 运行时」之一，并标注「这个模块在 `__init__.py` 里被谁导入」。

**操作步骤**：

1. 打开 `src/cuda/tile/__init__.py`，把所有 `from cuda.tile.xxx import ...` 的来源模块列出来。
2. 用 `ls -1 src/cuda/tile/` 拿到完整目录清单，给每个模块打标签，参考下表：

   | 模块 / 子包 | 归类 | 在 `__init__.py` 中的角色 |
   | --- | --- | --- |
   | `_stub.py` | 前端（用户 API 表面） | 导出 `load/store/算术/归约/Tile/Array/Constant` 等全部用户 API |
   | `_execution.py` | 前端（装饰器） | 导出 `kernel`、`function` |
   | `_annotated_function.py` / `_compiler_options.py` / `_by_target.py` | 前端（参数注解、编译选项） | 被 `_execution.py` 使用 |
   | `_passes/` | 前端 + 优化 | `ast2hir`/`hir2ir` 是前端；`dce` 等是优化 |
   | `_ir/` | 前端 + 优化 + 后端（共享数据结构） | IR/HIR 定义、类型系统 |
   | `_datatype.py` / `_numeric_semantics.py` / `_memory_model.py` | 前端（语义定义） | 导出 `DType`、`RoundingMode`、`MemoryOrder` 等 |
   | `_compile.py` | 后端（编译流水线编排） | `compile_tile` 总入口 |
   | `_ir2bytecode.py` + `_bytecode/` | 后端（序列化） | 字节码生成 |
   | `_cext`（C++ 扩展） | 运行时（桥接） | 导出 `launch` |
   | `_context.py` / `_cache.py` / `_dispatch_mode.py` | 运行时 | 上下文、磁盘缓存、分派模式 |
   | `compilation/` | 后端 + 运行时（AOT 导出） | `export_kernel`、签名、name mangling |
   | `tune/` | 运行时（自动调优） | `exhaustive_search` |
   | `jax/` | 运行时（互操作） | `cutile_call` 接入 JAX |
   | `_debug.py` / `_exception.py` | 横切（调试 / 异常） | dump 与错误类型 |

3. 用箭头表示依赖，例如：`__init__.py → _cext.launch`（运行时）、`_execution.kernel → _compile.compile_tile`（前端→后端）。
4. 在树上用颜色或标注区分「前端 / 优化 / 后端 / 运行时」四类。

**预期结果**：得到一张完整的 `cuda.tile` 内部依赖图，并能解释「为什么 `_cext` 是运行时桥接」「为什么 `_passes/` 横跨前端和优化」。这张图将是你阅读后续 u5（编译前端）、u6（优化 pass）、u7（后端）讲义时的导航地图。

## 6. 本讲小结

- cuTile 把编译流水线按阶段拆成子包：`_ir/`（IR 定义）、`_passes/`（前端 + 优化）、`_bytecode/` 与 `_ir2bytecode.py`（后端序列化）、`cext/` 与 `_context.py` 等（运行时桥接）。
- `pyproject.toml` 用 src 布局 + `[tool.setuptools.packages.find]` 白名单，决定哪些子包会被发行；`experimental/`、`test`、`samples` 不在白名单，不随主包安装。
- 版本号通过 `dynamic` 从 `src/cuda/tile/VERSION` 读取，是单一真相源。
- C++ 扩展 `_cext` 由 `setup.py` 的 `BuildExtWithCmake` 委托 CMake 构建：配置（`cmake -B`）→ 编译（`cmake --build`）→ 链接产物进源码树。
- 可编辑安装用符号链接（`link="sym"`）把 `lib_cext.so` 链到 `src/cuda/tile/_cext.so`，因此改 C++ 只需 `make -C build` 增量重编。
- `_cext` 是整个项目的运行时桥梁：Python 侧的 `ct.launch` 实际调用 C++ 扩展里的 `launch`，再由它驱动 CUDA。

## 7. 下一步学习建议

有了这张目录地图，你已经能定位流水线上每一站的源码位置。下一步：

- 想先会用、再懂原理：进入 **U3 编写内核：用户 API 实战**，从 `u3-l1 load/store` 开始，结合 `_stub.py` 写出真正的内核。
- 想先吃透顶层 API 分类：读 **u1-l4 顶层 API 全景：cuda.tile 公共接口**，把 `__init__.py` 导出的几百个符号分类成速查表。
- 想直接看编译器内部：跳到 **U5 编译前端**，从 `u5-l1 kernel 装饰器` 和 `u5-l2 compile_tile 流水线` 入手，对应到本讲的 `_execution.py` 与 `_compile.py`。

无论选哪条路，建议随时回到本讲的「目录地图」对照定位——后续每一篇讲义的 `source_files` 都会落在这张地图的某个节点上。
