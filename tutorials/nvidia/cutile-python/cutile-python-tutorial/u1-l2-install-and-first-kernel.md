# 环境搭建与第一个内核

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `cuda-tile` 与 `cuda-tile[tileiras]` 两种安装方式的差别，并知道在什么环境下选哪一种。
- 用 `pip install -e .` 完成可编辑（editable）构建，并解释 C++ 扩展为什么用「符号链接」放进源码树。
- 在你自己的机器上跑通 `samples/quickstart/VectorAdd_quickstart.py`，看到 `✓ vector_add_example passed!`。
- 读懂 VectorAdd 内核的 **load–compute–store** 三段式结构，理解 `grid`、`ct.cdiv`、`ct.launch` 之间的关系。
- 把 `tile_size` 从 16 改成 32，手动重新计算 `grid`，并解释结果为何仍然正确。

本讲是「上手运行」单元的第二篇，承接 [u1-l1 项目总览](u1-l1-project-overview.md)：上一讲建立了「AST → HIR → Tile IR → 字节码 → cubin → cuLaunchKernel」的全链路直觉，本讲就把这条链路在你本地真正跑起来。

## 2. 前置知识

在开始前，最好先具备以下概念（不熟悉也没关系，本讲会顺带解释）：

- **虚拟环境（venv）**：Python 自带的隔离机制，让你为每个项目单独安装依赖，互不污染。命令是 `python3 -m venv env`。
- **pip 与可选依赖（extras）**：`pip install 包名[extra]` 中的 `[extra]` 表示「额外装一组相关的包」。本讲里 `cuda-tile[tileiras]` 就是这种写法。
- **C++ 扩展（C++ extension）**：cuTile 主体是纯 Python，但启动 GPU、桥接 CUDA 运行时那部分是用 C++ 写的，需要先编译成 `.so`（Linux）/`.dll`（Windows）才能被 Python `import`。
- **CMake**：一个跨平台的构建系统。cuTile 用 CMake 来编译它的 C++ 扩展。
- **符号链接（symlink）**：一种「快捷方式」文件，访问它会自动跳转到它指向的真实文件。可编辑构建正是靠它实现的。
- **tile 与 array（回顾 u1-l1）**：`array` 是宿主分配的全局显存（可变、带 strides），`tile` 是内核内不可变、每维为 2 的幂的编译期常量块。本讲会用到一个一维 tile。

还需要一点关于 GPU 并行的直觉：cuTile 内核由 **grid** 中的多个 **block** 并行执行，每个 block 处理一块数据（一个 tile）。我们用 `ct.bid(0)` 拿到「当前 block 的编号」，从而让每个 block 算自己那一块。

## 3. 本讲源码地图

本讲涉及的文件都偏「工程入口」与「示例」，而不是编译器内部：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/README.md) | 项目的第一入口：系统要求、PyPI 安装、从源码构建、运行测试。 |
| [docs/source/quickstart.rst](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/quickstart.rst) | 官方快速上手文档：前置条件、安装步骤、示例说明、运行命令、Nsight Compute。 |
| [samples/quickstart/VectorAdd_quickstart.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/quickstart/VectorAdd_quickstart.py) | 本讲要跑的第一个内核：向量加法。 |
| [pyproject.toml](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/pyproject.toml) | 包元数据：包名、Python 版本、`[tileiras]` 可选依赖到底装了什么。 |
| [setup.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/setup.py) | 驱动 CMake 构建 C++ 扩展，并实现「可编辑构建的符号链接」逻辑。 |
| [CMakeLists.txt](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/CMakeLists.txt) | 顶层 CMake：查找 CUDA Toolkit、抓取 DLPack、编译 `cext`。 |
| [cext/CMakeLists.txt](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/cext/CMakeLists.txt) | 真正把 C++ 源码编成 `_cext` 扩展模块的地方。 |

> 说明：`README.md`、`docs/source/quickstart.rst`、`samples/quickstart/VectorAdd_quickstart.py` 是本讲指定的核心文件；`pyproject.toml`、`setup.py`、两个 `CMakeLists.txt` 用来支撑「安装与构建」这两个最小模块，引用它们能让讲解落到实处。

## 4. 核心概念与源码讲解

本讲围绕四个最小模块展开：**安装方式**、**从源码构建**、**第一个内核 VectorAdd**、**启动与验证**。

### 4.1 安装方式：cuda-tile 与 cuda-tile[tileiras]

#### 4.1.1 概念说明

cuTile 主体是纯 Python，但它依赖一个叫 **tileiras** 的后端编译器，而 tileiras 又依赖 CUDA Toolkit 里的 `ptxas` 和 `libnvvm`（见 [quickstart.rst:28-30](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/quickstart.rst#L28-L30)）。

于是产生两种安装策略：

1. **`cuda-tile[tileiras]`**：让 pip 直接把 tileiras 及其 CUDA 依赖装进当前 Python 虚拟环境。好处是不依赖系统级 CUDA Toolkit，换一台机器只要复制虚拟环境即可。适合「我只想快速试用」。
2. **`cuda-tile`（不带 extra）**：只装纯 Python 部分，tileiras 由你机器上**系统级安装的 CUDA Toolkit（13.1+）** 提供，cuTile 会自己去 CTK 目录里找它。适合「机器上已经有 CUDA Toolkit、不想重复装一份」。

一句话：带 `[tileiras]` 是「编译器进 Python 环境」，不带是「用系统的编译器」。

#### 4.1.2 核心流程

选择安装方式的决策流程：

1. 检查机器上是否已有系统级 CUDA Toolkit 13.1+。
2. 若没有，或希望完全自包含 → 用 `pip install cuda-tile[tileiras]`。
3. 若已有 → 用 `pip install cuda-tile`，cuTile 运行时自动定位 tileiras。
4. 安装后还需要一个能提供 GPU 张量的库（本讲用 CuPy）。

版本对齐很关键：tileiras、nvcc、nvvm 这几个包的主次版本（major.minor）必须一致（见 [quickstart.rst:41-42](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/quickstart.rst#L41-L42)），否则可能拼出不一致的工具链。

#### 4.1.3 源码精读

README 给出最简明的两条命令：

[README.md:61-76](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/README.md#L61-L76) —— 说明了「带 extra 装 tileiras 进 Python 环境」与「不带 extra、自己装 CUDA Toolkit 13.1+」两条路线。

`[tileiras]` 这个 extra 到底装了什么？看 `pyproject.toml`：

[pyproject.toml:41-42](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/pyproject.toml#L41-L42) —— 把 `[tileiras]` 映射到 `cuda-toolkit[tileiras,nvcc,nvvm]>=13.2,<13.4`，即一个**带 tileiras 子集的 CUDA Toolkit 元包**。注意版本区间被钉在 `>=13.2,<13.4`，这与 README 说的「tileiras 13.2」对应。

quickstart 文档给出更完整的安装命令与「按版本指定 tileiras」的写法：

[quickstart.rst:32-49](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/quickstart.rst#L32-L49) —— `pip install --upgrade cuda-tile[tileiras]` 是默认推荐；若想用特定版本（如 13.3），可用 `cuda-toolkit[tileiras,nvvm,nvcc]>=13.3`。

[quickstart.rst:52-58](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/quickstart.rst#L52-L58) —— 反过来，已有系统 CTK 时，`pip install cuda-tile` 即可，cuTile 会自动从 CTK 位置搜索 tileiras。

此外，Python 版本受 `pyproject.toml` 约束：

[pyproject.toml:22](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/pyproject.toml#L22) —— `requires-python = ">=3.10, <3.15"`，即支持 3.10 到 3.14（含 free-threading 的 3.14t）。

前置条件清单见 [quickstart.rst:19-23](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/quickstart.rst#L19-L23)：Linux x86_64/aarch64 或 Windows x86_64、计算能力 8.x–12.x 的 GPU、NVIDIA 驱动 r580+。

#### 4.1.4 代码实践

**实践目标**：在一个干净虚拟环境里安装 cuTile，并确认 tileiras 是否随包进入环境。

**操作步骤**：

```bash
python3 -m venv env
source env/bin/activate
pip install --upgrade cuda-tile[tileiras]
pip install cupy-cuda13x      # 示例需要 CuPy 提供张量
```

然后用下面命令（**示例代码**，仅用于核对）观察是否装到了 tileiras 相关组件：

```bash
pip list | grep -Ei "cuda-tile|tileiras|nvcc|nvvm"
```

**需要观察的现象**：列表里应同时出现 `cuda-tile` 与若干 `nvidia-*-tileiras`/`nvidia-*-nvcc`/`nvidia-*-nvvm` 组件。

**预期结果**：命令成功执行，依赖被解析安装。

> 待本地验证：实际包名与版本号取决于你 pip 解析时的镜像与平台 wheel 可用性；若选 `cuda-tile`（不带 extra），则不会出现 tileiras 组件，需要你自行确认系统 `nvcc --version` ≥ 13.1。

#### 4.1.5 小练习与答案

1. **问**：某台机器已通过 `apt` 装了完整的 CUDA Toolkit 13.2，你应该用哪条 pip 命令？为什么？
   **答**：用 `pip install cuda-tile`（不带 `[tileiras]`）。因为系统已有 CTK，cuTile 会自动从 CTK 位置找到 tileiras，无需在 Python 环境里重复装一份。

2. **问**：`cuda-tile[tileiras]` 里的 `[tileiras]` 在 `pyproject.toml` 里展开成什么？
   **答**：展开成 `cuda-toolkit[tileiras,nvcc,nvvm]>=13.2,<13.4`（见 [pyproject.toml:42](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/pyproject.toml#L42)），即一个包含 tileiras、nvcc、nvvm 的 CUDA Toolkit 子集元包。

3. **问**：为什么 quickstart 强调 tileiras/nvcc/nvvm 版本要一致？
   **答**：这三者共同构成编译工具链（tileiras 调 ptxas 与 libnvvm）。版本不匹配会导致工具链内部 ABI 或行为不一致，可能编译失败或生成错误 cubin。

### 4.2 从源码构建：可编辑安装与 C++ 扩展

#### 4.2.1 概念说明

当你想读源码、改源码、甚至贡献代码时，PyPI 安装就不够用了——你需要**从源码构建**。cuTile 95% 是 Python，但有一个 **C++ 扩展**（`_cext`），负责启动 GPU kernel、桥接 CUDA 运行时，必须先编译。

「**可编辑安装**（editable install）」指 `pip install -e .`：Python 部分直接指向源码目录（改了 `.py` 立即生效），C++ 扩展则被编译到 `build/` 目录，再用一个**符号链接**放进源码树。这样你只需编译一次 C++；之后只改 Python 代码无需重编，改了 C++ 也只需 `make -C build` 增量重编，非常快。

#### 4.2.2 核心流程

可编辑构建的执行流程（由 `setup.py` 的 `BuildExtWithCmake` 驱动）：

1. `pip install -e .` 触发 setuptools 的 `build_ext` 命令，被替换为 `BuildExtWithCmake`。
2. 确定构建目录 `build_dir`：可编辑模式下默认是项目根下的 `build/`（可用 `CUDA_TILE_CEXT_BUILD_DIR` 覆盖）。
3. 调用 `cmake -B build_dir <project_root> ...` 生成构建文件（同时查找 CUDA Toolkit、抓取 DLPack）。
4. 调用 `cmake --build build_dir`（Linux）或 `msbuild`（Windows）真正编译。
5. 编译产物（如 `lib_cext.so`）落在 `build_dir` 下。
6. 在源码包目录创建一个**符号链接**指向该产物，使 `import cuda.tile._cext` 生效。

随后，仅修改 C++ 源码时，重跑 `make -C build` 即可增量重编，符号链接自动指向新产物。

#### 4.2.3 源码精读

README 描述了依赖与「编辑模式只装一次、之后 `make -C build`」的设计意图：

[README.md:84-124](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/README.md#L84-L124) —— 列出 C++17 编译器、CMake 3.18+、Make、Python 3.10+ 开发头、CUDA Toolkit 13.1+ 等依赖；并明确「可编辑模式下编译产物放在 build 目录，再在源码目录建符号链接」，重编只需 `make -C build`。

真正的逻辑在 `setup.py`。`BuildExtWithCmake.run()` 决定构建目录并依次执行 cmake 与 make：

[setup.py:54-67](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/setup.py#L54-L67) —— `build_dir` 取自环境变量 `CUDA_TILE_CEXT_BUILD_DIR`；为空时，可编辑模式用 `项目根/build`，否则用 `self.build_temp`。随后调用 `self._cmake(...)` 与 `self._make(...)`。

关键的符号链接就在紧接着的循环里：

[setup.py:69-80](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/setup.py#L69-L80) —— 第 78 行 `link = "sym" if self.editable_mode else None`：可编辑模式创建**符号链接**（`sym`），非可编辑模式则**拷贝**（`None`）。`file_util.copy_file(..., link=link)` 负责把 `build_dir` 下的编译产物链接/拷贝到 setuptools 期望的扩展路径。

`_cmake` 把 DLPack 路径、XLA 路径、构建类型、Python 解释器等参数传给 CMake：

[setup.py:41-52](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/setup.py#L41-L52) —— 其中 `-DCMAKE_POLICY_VERSION_MINIMUM=3.5`、`-DPython_EXECUTABLE` 等确保 CMake 用正确的 Python 与策略。

顶层 `CMakeLists.txt` 负责找 CUDA Toolkit 并抓取 DLPack：

[CMakeLists.txt:70-96](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/CMakeLists.txt#L70-L96) —— `find_package(Python ...)` 与 `find_package(CUDAToolkit REQUIRED)`（第 71 行）声明了对 CUDA Toolkit 的硬依赖；第 82-91 行用 `FetchContent` 自动下载 DLPack（除非你用 `CUDA_TILE_CMAKE_DLPACK_PATH` 指定本地副本）；第 96 行 `add_subdirectory(cext)` 进入真正的扩展构建。

`cext/CMakeLists.txt` 把 C++ 源码编成 Python 可导入的模块：

[cext/CMakeLists.txt:47-77](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/cext/CMakeLists.txt#L47-L77) —— 先编一个静态库 `_cext_static`（第 47 行，聚合 `tile_kernel.cpp`、`cuda_loader.cpp` 等），再用它链接出 `_cext` 这个 `MODULE` 库（第 66 行），这正是 `import cuda.tile._cext` 实际加载的文件。

> 旁注：顶层 CMake 还有一个 `devinstall` 目标，用 `ln -fs` 把 `build/cext/lib_cext.so` 链接到源码树 `cuda/tile/_cext.so`（[CMakeLists.txt:73-75](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/CMakeLists.txt#L73-L75)），思路与 `setup.py` 的符号链接一致，是开发者手动重建链接的便捷入口。

#### 4.2.4 代码实践

**实践目标**：完成一次可编辑构建，亲眼看到源码树里的扩展符号链接，并体验「改 C++ 只需 `make -C build`」。

**操作步骤**：

```bash
# 1. 建虚拟环境
python3 -m venv env
source env/bin/activate

# 2. 可编辑安装（会触发 cmake + make，编译 _cext）
pip install -e .
```

构建完成后，查看源码树里的链接文件（**示例代码**）：

```bash
ls -l src/cuda/tile/ | grep cext
```

随后改一段 C++（比如在 `cext/` 下加一条注释），无需重跑 pip，直接：

```bash
make -C build
```

**需要观察的现象**：`src/cuda/tile/` 下出现 `_cext.so`（或类似），`ls -l` 显示它是**符号链接**（箭头 `->` 指向 `build/.../lib_cext.so`）。

**预期结果**：第二次起 `make -C build` 只重编少量文件，速度远快于首次。

> 待本地验证：链接的具体名称取决于平台（Linux 为 `lib_cext.so`，Windows 为 `_cext.dll`）；本环境无 GPU 与完整 CTK，无法在此实际编译，请在配有 CUDA Toolkit 13.1+ 的机器上验证。

#### 4.2.5 小练习与答案

1. **问**：为什么可编辑模式要「符号链接」而不是「拷贝」编译产物？
   **答**：符号链接让源码树里的 `_cext.so` 始终指向 `build/` 下最新的产物。这样你 `make -C build` 重编后无需再次 `pip install`，Python `import` 到的就是新版本（见 [setup.py:78](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/setup.py#L78)）。

2. **问**：`BuildExtWithCmake.run()` 如何决定构建目录？
   **答**：优先用环境变量 `CUDA_TILE_CEXT_BUILD_DIR`；为空时，可编辑模式用项目根下的 `build/`，非可编辑模式用 `self.build_temp`（见 [setup.py:55-60](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/setup.py#L55-L60)）。

3. **问**：顶层 CMake 对 CUDA Toolkit 是「可选」还是「必须」？
   **答**：必须。`find_package(CUDAToolkit REQUIRED)`（[CMakeLists.txt:71](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/CMakeLists.txt#L71)）带 `REQUIRED`，找不到会直接报错中止。

### 4.3 第一个内核 VectorAdd：load–compute–store

#### 4.3.1 概念说明

cuTile 内核几乎都遵循同一个三段式范式（见 [quickstart.rst:89-95](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/quickstart.rst#L89-L95)）：

1. **load**：从全局 array 把一块数据搬进来，变成不可变的 tile。
2. **compute**：在 tile 上做计算，得到新的 tile。
3. **store**：把结果 tile 写回全局 array。

VectorAdd（向量加法 `c = a + b`）就是这个范式的最小实例：每个 block 负责一段长度为 `tile_size` 的子向量，搬进来、相加、写回去。

内核用 `@ct.kernel` 装饰，函数体的写法看上去就是普通 Python，但它**不会立即执行**——`@ct.kernel` 把它标记为「tile 代码」，真正的执行发生在 host 端调用 `ct.launch` 时（回顾 u1-l1 的 JIT 链路）。

#### 4.3.2 核心流程

VectorAdd 内核内部的执行（在每个 block 上）：

1. `pid = ct.bid(0)`：拿到当前 block 在第 0 维的编号。
2. `a_tile = ct.load(a, index=(pid,), shape=(tile_size,))`：从数组 `a` 的第 `pid` 个 tile 处，加载长度为 `tile_size` 的一维 tile。
3. 同样加载 `b_tile`。
4. `result = a_tile + b_tile`：逐元素相加，得到 `result` tile。
5. `ct.store(c, index=(pid,), tile=result)`：把 `result` 写回数组 `c` 的第 `pid` 个 tile 处。

这里的 `tile_size` 不是普通参数，而是 `ct.Constant[int]`——一个**编译期常量**参数。它会被「烤进」生成的内核（决定 tile 的形状），每个不同的 `tile_size` 值会生成一份专门的 cubin（这点 u3-l5 会详讲，本讲只需知道：改它会重新编译出一个对应的内核）。

#### 4.3.3 源码精读

内核本体：

[samples/quickstart/VectorAdd_quickstart.py:15-28](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/quickstart/VectorAdd_quickstart.py#L15-L28) ——
- 第 15 行 `@ct.kernel` 把函数标记为 tile 内核；
- 第 16 行 `def vector_add(a, b, c, tile_size: ct.Constant[int])`：`a/b/c` 是 array，`tile_size` 是编译期常量；
- 第 18 行 `pid = ct.bid(0)`：当前 block 编号；
- 第 21-22 行两次 `ct.load`：`index=(pid,)` 定位「第几个 tile」，`shape=(tile_size,)` 说明 tile 是一维、长度为 `tile_size`；
- 第 25 行 `result = a_tile + b_tile`：逐元素加（tile 算术）；
- 第 28 行 `ct.store(c, index=(pid,), tile=result)`：写回。

quickstart 文档对这段三段式的解读：

[quickstart.rst:87-95](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/quickstart.rst#L87-L95) —— 明确「load tile → 计算 → store tile」是 cuTile 内核的通用结构；本例加载 `a_tile`、`b_tile`，相加得到 `result`，再 store 到输出向量。

> 对比：README 顶部的示例（[README.md:25-31](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/README.md#L25-L31)）用的是模块级常量 `TILE_SIZE = 16`，而不是 `ct.Constant[int]` 参数；本讲以 `samples/quickstart/` 下的真实文件为准，它更贴近工程实践（把 tile 大小做成常量参数）。

#### 4.3.4 代码实践

**实践目标**：在不运行的情况下，纯靠阅读源码预测内核行为，建立对 load/compute/store 的直觉。

**操作步骤**：

1. 打开 [samples/quickstart/VectorAdd_quickstart.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/quickstart/VectorAdd_quickstart.py)。
2. 在第 18-28 行旁，用笔标注：哪一行是 load、哪一行是 compute、哪一行是 store。
3. 假设 `tile_size = 16`、`pid = 2`，回答：`a_tile` 对应原数组 `a` 的哪些下标？

**需要观察的现象**：你能清楚说出 load 用到的 `index` 与 `shape` 各自的含义。

**预期结果**：`pid=2, tile_size=16` 时，`a_tile` 对应 `a` 的下标区间 `[2*16, 3*16) = [32, 48)`，即 tile 的起点为 `index * tile_size`，这正是 cuTile 用 `index` 以「tile 为单位」定位数据的语义（具体寻址由 `index × shape` 决定，详见 u2 数据模型与 u3-l1）。

> 待本地验证：精确的字节偏移与 strides 行为在 u2-l2 讲透；本讲只需建立「index 是 tile 编号、shape 是 tile 大小」的直觉。

#### 4.3.5 小练习与答案

1. **问**：`ct.load(a, index=(pid,), shape=(tile_size,))` 中 `index` 和 `shape` 分别表示什么？
   **答**：`shape=(tile_size,)` 表示要加载一个一维、长度为 `tile_size` 的 tile；`index=(pid,)` 表示从数组的第 `pid` 个这样的 tile 开始取（即以 tile 为粒度定位）。

2. **问**：为什么 `tile_size` 要声明成 `ct.Constant[int]`，而不能是普通 `int` 参数？
   **答**：因为 tile 的形状必须在**编译期**确定（每维须为 2 的幂）。`Constant[int]` 让该值在 JIT 时被嵌入生成的内核，从而决定 tile 形状；不同值会产生不同的 cubin。普通运行期 `int` 无法充当编译期形状。

3. **问**：`a_tile + b_tile` 会修改 `a_tile` 吗？
   **答**：不会。tile 是不可变的（回顾 u1-l1），算术运算会**产生一个新 tile** `result`，原 tile 不变。

### 4.4 启动内核：grid、cdiv 与 launch

#### 4.4.1 概念说明

内核写好后，还要在 host 端把它**启动**到 GPU 上。启动需要回答两个问题：

- **要开多少个 block？** 也就是 `grid`。向量长度是 `vector_size`，每个 block 处理 `tile_size` 个元素，所以 block 数量为 $\lceil \text{vector\_size} / \text{tile\_size} \rceil$。cuTile 提供了 `ct.cdiv(a, b)` 直接算这个「向上取整除法」。
- **怎么把内核和参数送上去？** 用 `ct.launch(stream, grid, kernel, args)`，其中 `stream` 是 CUDA 流（CuPy/PyTorch 都能提供），`args` 是传给内核的参数元组。

`grid` 是一个三元组 `(gx, gy, gz)`，表示三个维度上的 block 数量。一维问题只需 `(gx, 1, 1)`。

#### 4.4.2 核心流程

VectorAdd 的 host 端启动流程（`test()` 函数内）：

1. 设定 `vector_size = 2**12`（4096）、`tile_size = 2**4`（16）。
2. 计算 `grid = (ct.cdiv(vector_size, tile_size), 1, 1)`。
3. 用 CuPy 生成随机输入 `a`、`b`，分配输出 `c`。
4. `ct.launch(stream, grid, vector_add, (a, b, c, tile_size))` 启动内核。
5. 把 `c` 拷回 host，与 `a + b` 比较，断言近似相等。

block 数量的数学表达：

\[
\text{grid}_x = \left\lceil \frac{\text{vector\_size}}{\text{tile\_size}} \right\rceil = \text{ct.cdiv}(\text{vector\_size}, \text{tile\_size})
\]

代入默认值：\(\lceil 4096/16 \rceil = 256\)，即 `grid = (256, 1, 1)`，共 256 个 block 并行。

#### 4.4.3 源码精读

host 端准备与启动：

[samples/quickstart/VectorAdd_quickstart.py:31-46](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/quickstart/VectorAdd_quickstart.py#L31-L46) ——
- 第 33-34 行 `vector_size = 2**12`、`tile_size = 2**4`；
- 第 35 行 `grid = (ct.cdiv(vector_size, tile_size), 1, 1)`：用 `ct.cdiv` 算向上取整除法，得到一维 grid；
- 第 37-40 行用 CuPy 生成随机 `a`、`b` 与零初始化的 `c`（这些都是宿主张量，CuPy 通过 CUDA Array Interface 把它们交给 cuTile）；
- 第 43-46 行 `ct.launch(cp.cuda.get_current_stream(), grid, vector_add, (a, b, c, tile_size))`：在当前流上、用该 grid 启动内核，参数元组末尾把 `tile_size` 作为 `Constant` 传入。

结果验证：

[samples/quickstart/VectorAdd_quickstart.py:49-57](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/quickstart/VectorAdd_quickstart.py#L49-L57) —— 拷回 host 后用 `np.testing.assert_array_almost_equal(c_np, expected)` 断言 `c ≈ a + b`，通过则打印 `✓ vector_add_example passed!`。

运行方式与预期输出在 quickstart 文档里写得很明确：

[quickstart.rst:103-108](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/quickstart.rst#L103-L108) —— 命令 `python3 samples/quickstart/VectorAdd_quickstart.py`，成功时打印 `✓ vector_add_example passed!`。

> 旁注（性能工具，选学）：装了 Nsight Compute 后，可用 `ncu -o VecAddProfile --set detailed python3 VectorAdd_quickstart.py` 给该内核做 profile（[quickstart.rst:135-141](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/quickstart.rst#L135-L141)）。详细统计需要驱动 ≥ r580.126.09（Linux）。这部分会在 u8-l5 详讲。

#### 4.4.4 代码实践

**实践目标**：把 `tile_size` 从 16 改成 32，手动重算 `grid`，运行后确认结果仍正确，并理解 `grid` 的变化。

**操作步骤**：

1. 打开 [samples/quickstart/VectorAdd_quickstart.py:34](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/quickstart/VectorAdd_quickstart.py#L34)，把 `tile_size = 2**4` 改为 `tile_size = 2**5`（即 32）。
2. **先不动第 35 行的 `ct.cdiv`**，手算新的 `grid`：\(\lceil 4096/32 \rceil = 128\)，即 `grid = (128, 1, 1)`。
3. 运行 `python3 samples/quickstart/VectorAdd_quickstart.py`。

**需要观察的现象**：
- 程序仍打印 `✓ vector_add_example passed!`。
- 因为第 35 行用的是 `ct.cdiv(vector_size, tile_size)`，`tile_size` 一改，`grid` **自动**跟着变，无需手改 grid。

**预期结果**：tile 大小翻倍后，每个 block 处理 32 个元素，所需 block 数减半（256 → 128），但覆盖范围不变，结果仍正确。

> 待本地验证：本环境无 GPU 与 tileiras，无法实际运行；请在配好 cuTile 的机器上验证输出。注意：因 `tile_size` 是 `Constant[int]`，改成 32 会触发**重新编译**出一份新的 cubin（首次运行会稍慢，之后命中缓存）。

#### 4.4.5 小练习与答案

1. **问**：`vector_size = 4096`、`tile_size = 32` 时，`grid` 是多少？为什么？
   **答**：`ct.cdiv(4096, 32) = 128`，故 `grid = (128, 1, 1)`。因为 `cdiv` 是向上取整除法，4096 / 32 = 128 整除，所以正好 128 个 block。

2. **问**：如果 `vector_size = 5000`、`tile_size = 16`，`grid` 是多少？多出来的 block 会怎样？
   **答**：`cdiv(5000, 16) = ⌈312.5⌉ = 313`。第 313 个 block（`pid = 312`）的 tile 会超出数组末尾——这种越界由 `ct.load` 的 `padding_mode` 处理（用填充值补齐），u3-l1 会讲。

3. **问**：`ct.launch` 的参数顺序是什么？`tile_size` 为何放在参数元组末尾？
   **答**：`ct.launch(stream, grid, kernel, args)`。`args` 元组 `(a, b, c, tile_size)` 与内核签名 `def vector_add(a, b, c, tile_size)` 一一对应；`tile_size` 放末尾是因为它在签名里就是第 4 个参数（且类型是 `Constant[int]`）。

## 5. 综合实践

**任务**：在一台配好 GPU 的机器上，从零完成「安装 → 构建 → 运行 → 修改 → 验证」全流程，把本讲的四个模块串起来。

**步骤**：

1. **安装**：建虚拟环境，分别记录两条路线的差异——
   - 路线 A：`pip install cuda-tile[tileiras]`（编译器进环境）；
   - 路线 B：`pip install cuda-tile`（依赖系统 CTK）。
   任选其一，再 `pip install cupy-cuda13x`。
2. **从源码构建（可选，想读源码者做）**：`git clone` 仓库后 `pip install -e .`，用 `ls -l src/cuda/tile/ | grep cext` 确认扩展是符号链接。
3. **运行**：`python3 samples/quickstart/VectorAdd_quickstart.py`，确认看到 `✓ vector_add_example passed!`。
4. **修改**：把 `tile_size` 从 `2**4` 改成 `2**5`，再改成 `2**6`，每次运行前**手算** `grid`，并与程序实际启动的 block 数对照（可借助 Nsight 或日志）。
5. **解释**：写一段话说明——为什么改 `tile_size` 后 `grid` 会自动变化？为什么首次运行新 `tile_size` 会比第二次慢（提示：`Constant` 触发重编译 + JIT 缓存）？

**验收标准**：
- 三个 `tile_size` 取值（16/32/64）都能跑通且结果正确。
- 你能手算出对应的 `grid`：256 / 128 / 64。
- 能解释 `Constant` 参数对 JIT 缓存的影响。

> 待本地验证：本综合实践需要真实 GPU 与 tileiras 编译器，无法在本环境完成；请按上述步骤在目标机器上执行。

## 6. 本讲小结

- cuTile 有两条安装路线：`cuda-tile[tileiras]` 把后端编译器装进 Python 环境（其 extra 在 [pyproject.toml:42](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/pyproject.toml#L42) 展开为 `cuda-toolkit[tileiras,nvcc,nvvm]>=13.2,<13.4`）；`cuda-tile` 则复用系统 CUDA Toolkit 13.1+。
- 从源码构建用 `pip install -e .`，它通过 `setup.py` 的 `BuildExtWithCmake` 调用 CMake 编译 C++ 扩展 `_cext`，并在可编辑模式下用**符号链接**把产物链入源码树（[setup.py:78](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/setup.py#L78)），之后改 C++ 只需 `make -C build`。
- cuTile 内核遵循 **load–compute–store** 三段式；VectorAdd 用 `ct.load` 搬 tile、`+` 计算、`ct.store` 写回（[samples/quickstart/VectorAdd_quickstart.py:15-28](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/quickstart/VectorAdd_quickstart.py#L15-L28)）。
- `ct.bid(0)` 给出当前 block 编号；`index` 以「tile 为单位」定位数据，`shape` 决定 tile 大小。
- host 端用 `ct.cdiv` 算向上取整得到 `grid`，再用 `ct.launch(stream, grid, kernel, args)` 启动内核。
- 把 `tile_size`（`Constant[int]`）从 16 改成 32，`grid` 自动从 256 变为 128，结果仍正确；新值会触发重新编译并产生新的 cubin。

## 7. 下一步学习建议

本讲让你「跑起来了」，但很多概念只是点到为止。建议接下来：

- **u1-l3 源码目录结构与构建系统**：把 `setup.py`、CMake、`pyproject.toml` 的包发现规则讲透，看清 `src/cuda/tile` 下各子包属于前端/优化/后端/运行时中的哪一类。
- **u1-l4 顶层 API 全景**：系统过一遍 `cuda.tile` 公开 API（`kernel`/`launch`/`load`/`store`/`cdiv`/`bid`/`Constant` 等），建立一张速查表。
- **u2 执行模型与数据模型**：弄清 grid/block/执行空间，以及 array 的 strides、地址计算，回答本讲遗留的「`index × shape` 到底怎么映射到字节偏移」。
- **u3-l1 load/store 范式**：深入 `ct.load`/`ct.store` 的 `padding_mode`、越界处理等，把本讲的 `ct.cdiv` 越界问题讲清楚。

读完这些，你就能从「会跑示例」进阶到「独立写出自己的 load–compute–store 内核」。
