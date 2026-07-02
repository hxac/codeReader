# MLIR/LLVM 依赖与构建配置

## 1. 本讲目标

本讲解决一个问题：**CUDA Tile 是怎样“绑定”到某个具体的 MLIR/LLVM 版本上的？**

读完本讲，你应当能够：

1. 说清楚为什么 CUDA Tile 必须锁定到 MLIR/LLVM 的**具体一个 commit**，以及它用什么机制（兼容区间）来追踪上游的变化。
2. 在三种获取 LLVM 的方式（自动下载 / 本地源码 / 预编译库）中选出适合自己的那一种，并知道各自对应的 CMake 变量。
3. 理解 `find_package(MLIR)` 的位置、Python 绑定与 MLIR 的联动、`ccache` 与跨编译（cross-compile）这几个“容易踩坑”的配置细节。

本讲承接 u1-l2 讲过的“兼容区间”和“tblgen 必须最先构建”，把构建层的细节真正落到代码上。

---

## 2. 前置知识

在进入源码前，先用通俗语言建立三个心智模型。

### 2.1 CUDA Tile 不是独立语言，而是寄生在 MLIR 上的方言

CUDA Tile IR 的“操作”“类型”“字节码序列化”几乎全部是用 **MLIR 框架**实现的。它的 C++ 代码会直接 `#include` MLIR 的内部头文件、调用 MLIR 的内部 API（例如 `OpBuilder`、`Type`、`Dialect`）。这意味着：**MLIR 的内部 API 一旦改了，CUDA Tile 就可能编不过**。所以 CUDA Tile 和 MLIR 不是“松耦合依赖”，而是“强耦合共生”。

### 2.2 MLIR 是 LLVM 项目的一个子目录

MLIR 住在 `llvm-project` 仓库里（`llvm-project/mlir/`）。所以本讲里“获取 MLIR”和“获取 LLVM”其实是同一件事——拉取 `llvm-project` 的某个 commit。

### 2.3 上游 LLVM 跑得很快，经常“breaking”

LLVM/MLIR 是一个非常活跃的项目，经常出现不向后兼容的改动（API 重命名、头文件移动、TableGen 行为变化等）。一个“每天都能编”的开源库在这里不存在。这就是 CUDA Tile 设计“兼容区间”的根因。

> 术语提示：**commit hash** 是 git 里每个提交的唯一指纹（一长串十六进制字符），例如 `57109befac92...`。本讲里的“锁定到具体 commit”就是指用这个 hash 把上游版本钉死。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [CMakeLists.txt](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt) | 顶层构建脚本。里面有 LLVM/MLIR 配置块、`find_package(MLIR)`、Python 绑定校验、`ccache`、跨编译等全部联动逻辑。 |
| [cmake/IncludeLLVM.cmake](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/cmake/IncludeLLVM.cmake) | 真正“拿到 LLVM”的实现。定义了锁定的 commit hash、自动下载、本地源码、预编译库三个宏。 |
| [scripts/get-cuda-tile-version-for-llvm-hash.sh](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/scripts/get-cuda-tile-version-for-llvm-hash.sh) | 反向查询脚本：给你一个 LLVM commit，告诉你该用哪个 CUDA Tile 版本。 |
| [README.md](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md) | 用户文档。`Build Configuration Options` 和 `Keeping Compatibility with LLVM` 两节是本讲的人话版说明。 |

---

## 4. 核心概念与源码讲解

### 4.1 锁定到具体 commit：为什么需要兼容区间

#### 4.1.1 概念说明

如前置知识所述，CUDA Tile 强依赖 MLIR 的内部 API。如果允许用户用“任意”版本的 MLIR，那么几乎每个用户都会撞上不同的编译错误，维护者根本无法复现和修复。

于是 CUDA Tile 选择了一个朴素而有效的策略：**把上游 LLVM/MLIR 钉死在某一个具体 commit 上**。所有官方构建、测试、CI 都基于这一个 commit。用户要么用这一个 commit，要么用与它“在同一兼容区间内”的 commit。

“兼容区间”是 CUDA Tile 用来追踪上游 breaking commit 的机制：每当上游 LLVM 出现一个会破坏 CUDA Tile 的提交，维护者就发布一个新的 CUDA Tile patch 版本来修，于是这个 breaking commit 就成为两个兼容区间之间的“分界点”。

#### 4.1.2 核心流程

兼容区间的工作方式可以用一段伪代码描述：

```
上游 LLVM 时间线（每个 * 是一个 commit）：
  ... * * * [A] * * [B] * * [C] * * [D] * -> main
            ↑           ↑           ↑
            breaking    breaking    breaking

CUDA Tile 发布节奏：
  v13.1.0  -> 适配到 A 之前的版本
  v13.1.1  -> 修了 A，适配 [A, B)
  v13.1.3  -> 修了 B，适配 [B, C)
  v13.1.x  -> 修了 C，适配 [C, D)
```

也就是说，**每个 CUDA Tile 版本对应 LLVM 主干上一段左闭右开的区间** `[起点, 下一个breaking点)`。给定一个 LLVM commit，我们只要找到“最后一个起早于或等于它的 CUDA Tile 版本”，就知道该用它了。

注意：CUDA Tile 的**版本号**和 **LLVM commit** 是两套不同的东西，不要混淆：

- CUDA Tile 版本号 `Major.Minor.Patch`：`Major.Minor` 对齐 CUDA Toolkit，`Patch` 是开源发布序号。
- LLVM commit hash：钉在 `cmake/IncludeLLVM.cmake` 里的一行。

（另外，还有一个“字节码版本”如 13.1/13.3，那是 `.tilebc` 文件格式的版本，属于 u7 的内容，本讲不展开。）

#### 4.1.3 源码精读

锁定的具体位置在 `cmake/IncludeLLVM.cmake` 第 29 行：

```cmake
set(LLVM_GIT_REPO "https://github.com/llvm/llvm-project.git")
set(LLVM_BUILD_COMMIT_HASH 57109befac92811d2253109242ca6fa69c961fb2)
```

这行就是把整个项目钉死的“锚点”。当前 HEAD（`e01244d`）锁定的就是 `57109befac92...`，与最近一次提交信息 `[LLVM-FIX] Breaking commit 57109befac92` 完全对应。

- [cmake/IncludeLLVM.cmake:28-29](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/cmake/IncludeLLVM.cmake#L28-L29)：声明 LLVM 仓库地址和锁定的 commit hash。本行就是 README 多处链接指向的 `cmake/IncludeLLVM.cmake#L29`。

README 的 `Keeping Compatibility with LLVM` 一节用文字和表格讲清楚了这套机制：

- [README.md:363-366](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L363-L366)：说明 CUDA Tile 要求 LLVM 在“特定兼容 commit”，并解释 breaking commit 会触发新的 patch 版本，从而形成兼容区间。
- [README.md:377-381](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L377-L381)：用一个示例表格展示区间。注意这是**示例**（README 写的是 “For example”），不是当前最新状态。当前主干已经发布到 13.3.x（见 `git log` 里的 `[Release] CUDA Tile IR 13.3.0`）。

反向查询脚本 `get-cuda-tile-version-for-llvm-hash.sh` 就是“兼容区间”机制的程序化体现。它的核心思路是：遍历 CUDA Tile 的所有版本 tag，从每个 tag 的 `cmake/IncludeLLVM.cmake` 里读出该版本锁定的 LLVM commit，再判断输入 commit 落在哪个区间。

- [scripts/get-cuda-tile-version-for-llvm-hash.sh:64-67](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/scripts/get-cuda-tile-version-for-llvm-hash.sh#L64-L67)：`get_llvm_commit_from_tag` 用 `sed` 从指定 tag 的 `cmake/IncludeLLVM.cmake` 里抠出 `LLVM_BUILD_COMMIT_HASH` 的值——这正是“每个 CUDA Tile 版本都自带它锁定的 LLVM commit”这一约定被脚本利用的地方。
- [scripts/get-cuda-tile-version-for-llvm-hash.sh:94-120](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/scripts/get-cuda-tile-version-for-llvm-hash.sh#L94-L120)：`find_compatible_version` 遍历所有 tag，凡是“tag 的 LLVM commit 是输入 commit 的祖先或相等”就更新候选，最终选出最大（最新）的一个。

#### 4.1.4 代码实践

**实践目标**：亲手确认“当前 CUDA Tile 锁定了哪个 LLVM commit”，并理解脚本是如何读到它的。

**操作步骤**：

1. 在仓库根目录执行（只读 git 命令）：

   ```bash
   # 1. 查看当前 cmake 里锁定的 hash
   grep LLVM_BUILD_COMMIT_HASH cmake/IncludeLLVM.cmake

   # 2. 看最近几次提交，体会"breaking commit -> 新 patch 版本"的节奏
   git log --oneline -8
   ```

2. 阅读脚本第 25 行的 `FIRST_COMPATIBLE_LLVM_COMMIT`，对照 README 中 `v13.1.0` 的 `(LLVM 81b576e66)`，确认它们指的是同一个 commit。

**需要观察的现象**：

- 第 1 步应输出 `57109befac92811d2253109242ca6fa69c961fb2`。
- `git log` 里应能看到形如 `[LLVM-FIX] Breaking commit 57109befac92 - 2026-06-13` 的提交，commit 短 hash 与锁定的 hash 前缀一致。

**预期结果**：你能用自己的话回答——“因为 CUDA Tile 强依赖 MLIR 内部 API，所以它必须把 LLVM 钉在 `57109befac92...` 这一个 commit 上；上游每出一个 breaking commit，CUDA Tile 就发一个新 patch 版本来修，形成兼容区间。”

> 本实践只做读取与观察，不修改任何文件，安全可执行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 CUDA Tile 不直接写 `find_package(LLVM)`（不锁版本）让用户用自己的 LLVM？

**参考答案**：因为 CUDA Tile 直接使用了 MLIR 的大量内部 API 和内部头文件，这些 API 在不同 LLVM 版本之间会变化（重命名、删除、行为变化）。如果不锁版本，用户会撞上各种无法复现的编译错误，维护者也难以排查。锁定到具体 commit 后，官方构建、测试、CI 都在同一基线上，问题可复现、可修复。

**练习 2**：README 的兼容区间表格只列到 `v13.1.3`，这说明当前主干只支持到 13.1 吗？

**参考答案**：不是。README 的表格是 “For example” 的示例，用来解释机制。当前主干已经发布了 13.2.0、13.3.0（见 `git log`）。判断当前实际支持范围应当看 `cmake/IncludeLLVM.cmake` 里锁定的 LLVM commit 以及 `git tag`，而不是看那个示例表格。

---

### 4.2 三种获取 LLVM 的方式：自动下载 / 本地源码 / 预编译库

#### 4.2.1 概念说明

既然必须用某个具体 commit 的 LLVM，那“怎么拿到这个 LLVM”就有几种选择，对应不同的使用场景：

| 方式 | CMake 变量 | 是否编译 LLVM | 适用场景 | 速度 |
|------|-----------|--------------|---------|------|
| 1. 自动下载（默认） | 都不设 | 是，从 GitHub 拉取后编译 | 第一次接触、想最省事 | 最慢（要下载 + 全量编译 LLVM） |
| 2. 本地源码 | `CUDA_TILE_USE_LLVM_SOURCE_DIR` | 是，但用你本地已有的源码 | 已 clone 了 llvm-project、想省下载 | 中（省下载，但仍要编译） |
| 3. 预编译库 | `CUDA_TILE_USE_LLVM_INSTALL_DIR` | 否，直接链接现成的库 | 有现成 LLVM 安装、CI 复用、跨编译 | 最快 |

其中方式 2 和方式 3 是**互斥**的：你不能同时指定本地源码和预编译库。

#### 4.2.2 核心流程

顶层 CMake 的分派逻辑（伪代码）：

```
if 定义了 INSTALL_DIR 且 定义了 SOURCE_DIR:
    报错（两者不能同时设）
elif 定义了 INSTALL_DIR:
    调用 configure_pre_installed_llvm()   # 方式 3
else:
    调用 configure_llvm_from_sources()    # 方式 1 或 2
        if 定义了 SOURCE_DIR:
            用用户给的源码目录              # 方式 2
        else:
            download_llvm_sources()        # 方式 1（默认）
```

注意分派粒度：顶层只区分“预编译库” vs “从源码构建”两条路；而“从源码构建”内部再细分为“用本地源码”还是“下载”。这是初学者容易看漏的一层嵌套。

#### 4.2.3 源码精读

顶层分派在 `CMakeLists.txt` 的 LLVM/MLIR 配置块里：

- [CMakeLists.txt:64-70](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L64-L70)：互斥校验。同时设置两个变量会直接 `FATAL_ERROR`。
  ```cmake
  if (DEFINED CUDA_TILE_USE_LLVM_INSTALL_DIR AND
      DEFINED CUDA_TILE_USE_LLVM_SOURCE_DIR)
    message(FATAL_ERROR "Either CUDA_TILE_USE_LLVM_INSTALL_DIR or "
            "CUDA_TILE_USE_LLVM_SOURCE_DIR may be set, but not both")
  endif()
  ```
- [CMakeLists.txt:74-78](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L74-L78)：核心分派。`INSTALL_DIR` 走预编译，其余走源码构建。
  ```cmake
  if (CUDA_TILE_USE_LLVM_INSTALL_DIR)
    configure_pre_installed_llvm()
  else()
    configure_llvm_from_sources()
  endif()
  ```

“从源码构建”内部的二级分派，在 `cmake/IncludeLLVM.cmake` 里：

- [cmake/IncludeLLVM.cmake:67-74](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/cmake/IncludeLLVM.cmake#L67-L74)：有 `SOURCE_DIR` 就用本地源码，否则下载。这就是方式 2 与方式 1（默认）的分界。
  ```cmake
  if (CUDA_TILE_USE_LLVM_SOURCE_DIR)
    set(LLVM_SOURCE_DIR ${CUDA_TILE_USE_LLVM_SOURCE_DIR})
  else()
    download_llvm_sources()
    set(LLVM_SOURCE_DIR ${CUDA_TILE_BINARY_DIR}/${FETCHCONTENT_SOURCE_DIR})
  endif()
  ```

方式 1 的下载由 `download_llvm_sources` 宏完成，用的是 CMake 的 `FetchContent`，`GIT_TAG` 就是 4.1 节那个锁定的 hash：

- [cmake/IncludeLLVM.cmake:41-51](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/cmake/IncludeLLVM.cmake#L41-L51)：`fetchContent_Declare` 用 `GIT_TAG ${LLVM_BUILD_COMMIT_HASH}` 锁定下载的版本，`fetchContent_MakeAvailable` 触发下载。

不管走哪条路，最终目的都是得到两套 CMake 目录：`LLVM_CMAKE_DIR` 和 `MLIR_CMAKE_DIR`，供后面的 `find_package(MLIR)` 使用。方式 1/2 在源码构建后从 build 目录算出，方式 3 直接从安装目录取：

- [cmake/IncludeLLVM.cmake:116-122](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/cmake/IncludeLLVM.cmake#L116-L122)：源码构建路径下计算 cmake 目录。
- [cmake/IncludeLLVM.cmake:151-154](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/cmake/IncludeLLVM.cmake#L151-L154)：预编译路径下直接指向安装目录的 `lib/cmake/{llvm,mlir}`。

README 的 `Build Configuration Options` 一节把这三条路翻译成了具体命令：

- [README.md:74-103](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L74-L103)：三种方式的说明与对应 `cmake` 命令示例。

#### 4.2.4 代码实践

**实践目标**：在不真正跑全量构建的前提下，把三种方式的命令配置和源码分派对上号。

**操作步骤**：

1. 打开 [README.md:74-103](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L74-L103)，记录三种方式各自的命令。
2. 对照 [CMakeLists.txt:74-78](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L74-L78) 与 [cmake/IncludeLLVM.cmake:67-74](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/cmake/IncludeLLVM.cmake#L67-L74)，画一张表把“命令里设的变量 → 走哪个宏 → 是否下载/编译 LLVM”三列填满。
3. （可选，待本地验证）若本机已装好 ninja 与编译器，可尝试方式 1 的最小配置：
   ```bash
   cmake -G Ninja -S . -B build -DCMAKE_BUILD_TYPE=Release
   ```
   配置阶段（注意只是 configure，不要 build）观察日志里是否出现 `Downloading LLVM sources ... @57109befac92...`。

**需要观察的现象**：第 2 步的表格应能体现“二级分派”——顶层只二分，源码分支内部再二分。第 3 步配置日志会显示从 GitHub 拉取 LLVM 的进度。

**预期结果**：你能准确说出“什么都不设 = 自动下载（最慢）”“设 `CUDA_TILE_USE_LLVM_SOURCE_DIR` = 本地源码”“设 `CUDA_TILE_USE_LLVM_INSTALL_DIR` = 预编译库（最快）”，并知道后两者互斥。

> 第 3 步若本机无网络或无编译器，标注「待本地验证」即可，前两步纯阅读不受影响。

#### 4.2.5 小练习与答案

**练习 1**：用户同时传了 `-DCUDA_TILE_USE_LLVM_INSTALL_DIR=/a` 和 `-DCUDA_TILE_USE_LLVM_SOURCE_DIR=/b`，会发生什么？

**参考答案**：会触发 [CMakeLists.txt:64-70](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L64-L70) 的 `FATAL_ERROR`，配置直接失败，提示两者不能同时设置。

**练习 2**：为什么“自动下载”是最慢的，而“预编译库”是最快的？

**参考答案**：自动下载不仅要从 GitHub 拉取整个 `llvm-project` 源码，还要在本机**全量编译** LLVM 和 MLIR（这是个体量巨大的 C++ 项目）。预编译库方式则既不下载也不编译，直接链接别人已经编好的 `.so/.a`，所以最快。本地源码方式省去了下载，但仍需编译，速度居中。

---

### 4.3 find_package(MLIR) 与 Python 绑定的顺序依赖

#### 4.3.1 概念说明

拿到 LLVM 之后，CMake 还要做两件事：

1. **`find_package(MLIR)`**：让 CUDA Tile 能链接到 MLIR 的库、使用 MLIR 的 CMake 模块（`AddMLIR`、`TableGen` 等）。这一步必须在 LLVM 配置完成、`LLVM_CMAKE_DIR`/`MLIR_CMAKE_DIR` 都就位之后。
2. **Python 绑定校验**：CUDA Tile 的 Python 绑定依赖 MLIR 自身的 Python 绑定（`MLIR_ENABLE_BINDINGS_PYTHON`）。这里有一个**顺序陷阱**——校验必须在 `find_package(MLIR)` 之后。

为什么要“之后”？因为 `find_package(MLIR)` 会从它找到的那个 MLIR 里**回填** `MLIR_ENABLE_BINDINGS_PYTHON` 的值。如果你在校验之前就读这个变量，拿到的可能是你本地缓存的旧值，而不是 MLIR 实际提供的状态，从而误判。

#### 4.3.2 核心流程

```
configure_llvm_from_sources() 或 configure_pre_installed_llvm()
        ↓ 得到 LLVM_CMAKE_DIR / MLIR_CMAKE_DIR
find_package(MLIR REQUIRED CONFIG PATHS ${MLIR_CMAKE_DIR})
        ↓ MLIR 的 CMake 变量被回填（含 MLIR_ENABLE_BINDINGS_PYTHON）
include(TableGen / AddLLVM / AddMLIR)
        ↓
if CUDA_TILE_ENABLE_BINDINGS_PYTHON:
    if NOT MLIR_ENABLE_BINDINGS_PYTHON:   # 必须在 find_package 之后
        FATAL_ERROR
```

#### 4.3.3 源码精读

- [CMakeLists.txt:80-92](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L80-L92)：`find_package(MLIR)` 与加载 MLIR 的 CMake 模块。`NO_DEFAULT_PATH` 强制只在我们指定的目录找，避免误链到系统里别的 MLIR。
  ```cmake
  list(APPEND CMAKE_MODULE_PATH ${LLVM_CMAKE_DIR})
  list(APPEND CMAKE_MODULE_PATH ${MLIR_CMAKE_DIR})
  find_package(MLIR REQUIRED CONFIG PATHS ${MLIR_CMAKE_DIR} NO_DEFAULT_PATH)

  include(TableGen)
  include(AddLLVM)
  include(AddMLIR)
  ```

- [CMakeLists.txt:102-118](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L102-L118)：Python 绑定校验。注意它**特意排在 `find_package(MLIR)` 之后**，文件里有专门注释解释原因。
  ```cmake
  # These checks need to happen after `find_package(MLIR)` as some MLIR CMake
  # variables can be overridden.
  if(NOT MLIR_ENABLE_BINDINGS_PYTHON)
    message(FATAL_ERROR "CUDA Tile IR Python bindings require MLIR Python bindings enabled")
  endif()
  ```

`MLIR_ENABLE_BINDINGS_PYTHON` 这个值在不同路径下的来源也不同，这正是 README 反复提醒的点：

- 从源码构建时（方式 1/2），`configure_llvm_from_sources` 会主动把 CUDA Tile 的开关**透传**给 MLIR：[cmake/IncludeLLVM.cmake:93-94](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/cmake/IncludeLLVM.cmake#L93-L94)。
  ```cmake
  set(MLIR_INCLUDE_TESTS OFF CACHE BOOL "")
  set(MLIR_ENABLE_BINDINGS_PYTHON ${CUDA_TILE_ENABLE_BINDINGS_PYTHON} CACHE BOOL "")
  ```
  所以从源码构建时，只要你开了 `CUDA_TILE_ENABLE_BINDINGS_PYTHON=ON`，MLIR 的 Python 绑定会被自动打开，无需额外操心。

- 用预编译库时（方式 3），MLIR 已经编好了，CUDA Tile 没法影响它。所以你必须**自己保证**当初编这个 LLVM 时带了 `-DMLIR_ENABLE_BINDINGS_PYTHON=ON`，否则会在 [CMakeLists.txt:111-113](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L111-L113) 报错。

README 把这条注意事项写在了 Python Bindings 小节：

- [README.md:117-119](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L117-L119)：从源码构建会自动启用 MLIR Python 绑定；用预编译库时必须自己保证它当时是带 `MLIR_ENABLE_BINDINGS_PYTHON=ON` 编译的。

#### 4.3.4 代码实践

**实践目标**：理解“校验必须在 find_package 之后”这条顺序约束的必要性。

**操作步骤**：

1. 阅读 [CMakeLists.txt:80-118](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L80-L118)，找到 `find_package(MLIR)` 与 Python 校验两段，确认它们的先后。
2. 做一个思想实验（不改动源码）：假设把 `if(NOT MLIR_ENABLE_BINDINGS_PYTHON)` 这段挪到 `find_package(MLIR)` **之前**，在“方式 3 + 一个其实带 Python 绑定的预编译 LLVM”场景下，会发生什么误判？
3. （待本地验证）若已有预编译 LLVM，尝试两种配置对比：先用一个**没带** Python 绑定的预编译 LLVM 配置 `CUDA_TILE_ENABLE_BINDINGS_PYTHON=ON`，观察是否在配置阶段报 `CUDA Tile IR Python bindings require MLIR Python bindings enabled`。

**需要观察的现象**：第 2 步应意识到——在 `find_package` 之前读 `MLIR_ENABLE_BINDINGS_PYTHON`，读到的是缓存值，可能为空或为旧值，导致即使 MLIR 实际支持 Python 绑定也被误判为不支持（或反之）。

**预期结果**：你能解释“为什么 CMakeLists 里要专门写一行注释强调顺序”——因为 `find_package(MLIR)` 会覆盖/回填 `MLIR_ENABLE_BINDINGS_PYTHON`，校验必须等它落地。

#### 4.3.5 小练习与答案

**练习 1**：用户用预编译 LLVM，开了 `CUDA_TILE_ENABLE_BINDINGS_PYTHON=ON`，但配置报错 `CUDA Tile IR Python bindings require MLIR Python bindings enabled`。原因最可能是什么？

**参考答案**：预编译 LLVM 是别人/自己之前编好的，CUDA Tile 无法改变它的状态。最可能的原因是当初编这个 LLVM 时没有带 `-DMLIR_ENABLE_BINDINGS_PYTHON=ON`。解决办法是重新编译 LLVM 时加上该选项，或改用“从源码构建”方式（它会自动透传这个开关，见 [cmake/IncludeLLVM.cmake:94](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/cmake/IncludeLLVM.cmake#L94)）。

**练习 2**：为什么 `find_package(MLIR)` 要加 `NO_DEFAULT_PATH`？

**参考答案**：为了防止 CMake 在系统默认路径下找到**另一个** MLIR（比如系统包管理器装的、或别的项目残留的），从而链错版本。CUDA Tile 强依赖特定 commit 的 MLIR，必须只从我们刚配置好的 `${MLIR_CMAKE_DIR}` 里找。

---

### 4.4 ccache 与跨编译的联动配置

#### 4.4.1 概念说明

最后看两个“配置联动”细节，它们都和 LLVM 息息相关：

- **ccache**：一个 C/C++ 编译缓存工具，能让你重复编译时跳过未变化的翻译单元，大幅提速。CUDA Tile 开启 ccache 后，会把它**透传给 LLVM 的构建**，否则 LLVM 那一大坨代码重编一次还是慢。
- **跨编译（cross-compilation）**：在一种 CPU 上编出给另一种 CPU/系统运行的产物（比如在 x86 上编 ARM 版）。CUDA Tile 的代码生成工具 `cuda-tile-tblgen` 必须在**宿主机**上运行（它要在配置/构建阶段跑），所以跨编译时需要一套“宿主工具 + 目标库”的双轨配置。

这两个细节的共同点是：它们都不是 CUDA Tile 自己的新功能，而是对 LLVM/MLIR 已有机制的“桥接”。

#### 4.4.2 核心流程

ccache 联动：

```
CUDA_TILE_ENABLE_CCACHE=ON
    ↓ CMakeLists 把 ccache 设为 C/C++ 编译启动器（compiler launcher）
configure_llvm_from_sources() 时检测到该开关
    ↓ 设置 LLVM_CCACHE_BUILD=ON，透传给 LLVM 构建
LLVM 也用 ccache → 全局加速
```

跨编译联动：

```
CMAKE_CROSSCOMPILING=ON
    ↓
CUDA_TILE_USE_NATIVE_LLVM_INSTALL_DIR 必须定义（用宿主机的预编译 LLVM）
    ↓
llvm_create_cross_target(CUDA_TILE NATIVE ...)  建一个 native 子构建
    ↓ 用它产出可在宿主运行的 cuda-tile-tblgen
注意：configure_llvm_from_sources 在跨编译时直接 FATAL_ERROR（不允许跨编译时还从源码编 LLVM）
```

#### 4.4.3 源码精读

ccache：

- [CMakeLists.txt:32-42](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L32-L42)：找到 `ccache` 并设为编译启动器。
  ```cmake
  if(CUDA_TILE_ENABLE_CCACHE)
    find_program(CCACHE_PROGRAM ccache)
    if(CCACHE_PROGRAM)
      set(CMAKE_C_COMPILER_LAUNCHER ${CCACHE_PROGRAM})
      set(CMAKE_CXX_COMPILER_LAUNCHER ${CCACHE_PROGRAM})
    endif()
  endif()
  ```
- [cmake/IncludeLLVM.cmake:88-90](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/cmake/IncludeLLVM.cmake#L88-L90)：把开关透传给 LLVM 构建（设 `LLVM_CCACHE_BUILD`）。
  ```cmake
  if(CUDA_TILE_ENABLE_CCACHE)
    set(LLVM_CCACHE_BUILD ON CACHE BOOL "")
  endif()
  ```
- [README.md:121-133](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L121-L133)：用户文档，明确“从源码构建 LLVM 时该设置会自动透传”。

跨编译：

- [CMakeLists.txt:124-144](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L124-L144)：跨编译块。它要求 `CUDA_TILE_USE_NATIVE_LLVM_INSTALL_DIR`，并通过 `llvm_create_cross_target` 建一个 native 子构建来产出宿主工具（`cuda-tile-tblgen`）。注释也点明了“这主要是 `cuda-tile-tblgen` 需要宿主 tablegen 可执行文件”。
- [cmake/IncludeLLVM.cmake:57-60](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/cmake/IncludeLLVM.cmake#L57-L60)：跨编译时禁止“从源码构建 LLVM”。这也解释了为什么跨编译**必须**走预编译库路径。
  ```cmake
  macro(configure_llvm_from_sources)
    if (CMAKE_CROSSCOMPILING)
      message(FATAL_ERROR "Cross-compilation is not supported when building LLVM from sources")
    endif()
  ```

#### 4.4.4 代码实践

**实践目标**：验证 ccache 是否被正确透传到 LLVM 构建。

**操作步骤**：

1. 阅读 [CMakeLists.txt:32-42](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L32-L42) 与 [cmake/IncludeLLVM.cmake:88-90](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/cmake/IncludeLLVM.cmake#L88-L90)，确认两个开关名一致（都是 `CUDA_TILE_ENABLE_CCACHE`）。
2. （待本地验证，需本机装了 ccache）配置时加 ccache：
   ```bash
   cmake -G Ninja -S . -B build \
     -DCMAKE_BUILD_TYPE=Release \
     -DCUDA_TILE_ENABLE_CCACHE=ON
   ```
   构建一次后，删除 build 目录里的某个 `.o`（或 `ninja -t clean` 后）再构建，用 `time` 对比有无 ccache 时的耗时。

**需要观察的现象**：开启 ccache 后第二次构建应明显更快（命中缓存）。配置日志里 LLVM 那一段会体现 ccache 被启用。

**预期结果**：你能说出“CUDA Tile 的 `CUDA_TILE_ENABLE_CCACHE` 通过设置编译启动器和 `LLVM_CCACHE_BUILD` 两处，同时加速了 CUDA Tile 自身和 LLVM 的重编”。

> 本实践依赖本机环境与较长的首次编译时间，若条件不足，纯阅读前两步即可，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么跨编译时 `configure_llvm_from_sources` 会直接 `FATAL_ERROR`？

**参考答案**：从源码构建 LLVM 需要在构建过程中运行一些“宿主工具”（比如 LLVM 自己的 tablegen、配置探测程序）。跨编译时目标是另一种平台，无法直接运行这些宿主工具，要让 LLVM 的构建系统正确处理 host/target 双轨非常复杂。CUDA Tile 选择直接禁止这条路径，强制跨编译走预编译库（`CUDA_TILE_USE_LLVM_INSTALL_DIR` / `CUDA_TILE_USE_NATIVE_LLVM_INSTALL_DIR`）。

**练习 2**：如果只设了 `CMAKE_C_COMPILER_LAUNCHER=ccache`（CUDA Tile 这一层），但没设 `LLVM_CCACHE_BUILD`，会有什么问题？

**参考答案**：CUDA Tile 自己的代码能享受 ccache 加速，但 LLVM/MLIR 那一大堆代码不能。而全量构建里 LLVM 编译时间占大头，所以加速效果会大打折扣。这正是 [cmake/IncludeLLVM.cmake:88-90](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/cmake/IncludeLLVM.cmake#L88-L90) 要把开关透传到 LLVM 构建的原因。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个“为不同场景选配构建命令”的练习。

**任务背景**：假设你团队有三个同事，分别在三种环境下想构建 CUDA Tile：

- 同事 A：完全新手，机器能联网，不在意首次编译慢。
- 同事 B：已经 clone 了 `llvm-project`，并且 checkout 到了 CUDA Tile 锁定的那个 commit。
- 同事 C：公司的 CI 集群里有一个预编译好的、带 Python 绑定的 LLVM 安装，路径是 `/opt/llvm-cuda-tile`，需要跨架构（aarch64）产物。

**请你**：

1. 为 A、B、C 三人各写一条 `cmake` 配置命令（参考 [README.md:74-103](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L74-L103)）。
2. 指出 C 同事的命令里必须额外提供哪个变量（提示：跨编译相关，见 [CMakeLists.txt:124-144](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L124-L144)），并解释为什么 C 不能像 A 那样自动下载 LLVM。
3. 如果三人都要 Python 绑定和测试，给谁的命令最简单、给谁的命令最容易踩“Python 绑定未启用”的坑？为什么？

**参考要点**：

1. A：`cmake -G Ninja -S . -B build -DCMAKE_BUILD_TYPE=Release -DCUDA_TILE_ENABLE_BINDINGS_PYTHON=ON -DCUDA_TILE_ENABLE_TESTING=ON`（什么都不设，自动下载）。
   B：在 A 的基础上加 `-DCUDA_TILE_USE_LLVM_SOURCE_DIR=/path/to/llvm-project`。
   C：用 `-DCUDA_TILE_USE_LLVM_INSTALL_DIR=/opt/llvm-cuda-tile`，并配合交叉工具链与 `-DCUDA_TILE_USE_NATIVE_LLVM_INSTALL_DIR=...`。
2. C 必须额外提供 `CUDA_TILE_USE_NATIVE_LLVM_INSTALL_DIR`（指向宿主 x86 的预编译 LLVM），用来在 native 子构建里产出可在宿主运行的 `cuda-tile-tblgen`。C 不能自动下载，因为 [cmake/IncludeLLVM.cmake:57-60](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/cmake/IncludeLLVM.cmake#L57-L60) 在跨编译时禁止从源码构建 LLVM。
3. A 最简单（自动透传 Python 绑定开关）；C 最容易踩坑——预编译 LLVM 是否带 Python 绑定不由 CUDA Tile 控制，若 `/opt/llvm-cuda-tile` 当初没带 `MLIR_ENABLE_BINDINGS_PYTHON=ON`，会在 [CMakeLists.txt:111-113](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt#L111-L113) 报错。

---

## 6. 本讲小结

- CUDA Tile 强依赖 MLIR 的内部 API，因此把 LLVM/MLIR **锁定到具体一个 commit**（当前为 `57109befac92...`，见 `cmake/IncludeLLVM.cmake:29`）；上游每个 breaking commit 触发一个新 patch 版本，形成**兼容区间**。
- 获取 LLVM 有三种方式：**自动下载**（默认，最慢）、**本地源码**（`CUDA_TILE_USE_LLVM_SOURCE_DIR`）、**预编译库**（`CUDA_TILE_USE_LLVM_INSTALL_DIR`，最快）；后两者互斥。
- 分派是**二级嵌套**的：顶层只分“预编译 vs 源码构建”，源码分支内部再分“本地源码 vs 下载”。
- `find_package(MLIR)` 必须在 LLVM 配置之后；Python 绑定校验又必须在 `find_package(MLIR)` **之后**，因为该步会回填 `MLIR_ENABLE_BINDINGS_PYTHON`。
- 从源码构建时 Python 绑定开关会自动透传给 MLIR；用预编译库时必须自己保证 MLIR 当初带 Python 绑定编译。
- `ccache` 通过 `CUDA_TILE_ENABLE_CCACHE` 同时作用到 CUDA Tile 与 LLVM（`LLVM_CCACHE_BUILD`）；跨编译禁止从源码构建 LLVM，必须走预编译库 + `CUDA_TILE_USE_NATIVE_LLVM_INSTALL_DIR`。

---

## 7. 下一步学习建议

本讲把“怎么把 LLVM 拿到手并链上”讲完了。一旦 `find_package(MLIR)` 成功、`cuda-tile-tblgen` 被最先构建出来，下一步就该看：

- **u2-l2（CudaTile 方言定义）**：进入 `include/cuda_tile/Dialect/CudaTile/IR/Dialect.td`，看 CUDA Tile 怎样在 MLIR 之上定义自己的方言。这是理解后续所有操作/类型/属性的前提。
- **u2-l3（TableGen 与代码生成）**：本讲多次提到的 `cuda-tile-tblgen` 到底怎么把 `.td` 变成 `.inc`，以及它为什么“必须最先构建”。
- 若你对构建工程化更感兴趣，可以先跳到 **u10-l1（C API 集成）** 看 README “Integrating CUDA Tile Into Your Project” 那一节对应的两种集成方式，再回头看本讲的联动配置会更有体会。
