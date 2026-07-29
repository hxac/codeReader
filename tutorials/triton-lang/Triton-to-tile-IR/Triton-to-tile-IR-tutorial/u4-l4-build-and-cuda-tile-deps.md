# 构建系统与 cuda-tile 依赖管理

## 1. 本讲目标

本讲是「调优、测试、容错与构建」单元的收尾篇，聚焦**构建期**：当读者执行 `pip install .` 时，到底发生了什么。

学完后你应该能够：

- 说清构建时 NVIDIA `cuda-tile` 这个外部仓库是**从哪里、按什么版本**被拉下来、又被打了哪些补丁。
- 说清 `setup.py` 是如何把 `tileir` 注册成一个 in-tree 后端（entry point）的，以及为何源码树里找不到 `python/triton/backends/tileir/` 目录。
- 列出 `TritonTileIR` 这个 C++ pybind 插件最终链接了哪些静态库，并解释这些库的来源。

本讲只读不写代码，重点是理清「Python 打包脚本 → CMake 配置 → 外部依赖克隆/补丁/构建 → 静态库链接」这一整条链路。

## 2. 前置知识

阅读本讲前，请先建立以下认知（已在前置讲义中铺垫）：

- **in-tree 后端**（[u1-l3](u1-l3-repo-structure.md)）：Triton 允许第三方后端放在 `third_party/<名字>/` 下，构建期自动注册。`tileir` 与 `nvidia`、`amd` 并列，但磁盘上不存在 `python/triton/backends/tileir/`，它是 `setup.py` 构建期建立的符号链接。
- **后端发现与 entry point**（[u2-l1](u2-l1-backend-selection.md)）：运行期 `triton.backends` entry point 让后端「可见」，`ENABLE_TILE` 让其「被启用」。
- **C++ 插件 `TritonTileIR`**（[u3-l1](u3-l1-pass-plugin-skeleton.md)）：`tileir` Python 模块来自 C++ 插件，源文件是 `third_party/tileir/triton_tileir.cc`，经 `init_triton_tileir` 注册。

本讲会用到几个 CMake / 打包术语，先解释清楚：

| 术语 | 含义 |
|------|------|
| **CMake configure 期** | 运行 `cmake -S . -B build`，执行 `CMakeLists.txt`、探测依赖、生成构建文件。`execute_process` 在此期运行。 |
| **CMake build 期** | 运行 `cmake --build`，实际编译链接。 |
| **静态库（`.a`）** | 一组目标文件（`.o`）的归档，链接时被「吸收」进最终产物，运行期不再依赖。 |
| **entry point** | setuptools 的元数据条目，声明一个名字与一个 Python 模块的映射，运行期被 `importlib.metadata` 读取。 |
| **symlink（符号链接）** | 一种文件系统指针，让一个路径指向另一个目录，使源码无需复制即可在 import 路径上出现。 |
| **CUDA Tile IR / cuda-tile** | NVIDIA 维护的、定义 `cuda_tile` 方言及其 bytecode 的上游项目，随 CUDA 13.3 以 tag `v13.3.0` 发布。 |

一个关键事实：本仓库（Triton 3.7）使用的 LLVM 版本，**比** NVIDIA `cuda-tile` v13.3.0 锁定的 LLVM **更新**。两者之间存在多处 MLIR/LLVM API 差异，这正是本讲反复出现的「补丁（patch）」的全部由来。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 职责 |
|------|------|
| [CMakeLists.txt](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/CMakeLists.txt)（仓库根） | 顶层 CMake：定义 `TRITON_CODEGEN_BACKENDS` 选项、`add_triton_plugin` 宏、遍历后端做 `add_subdirectory`、把后端名拼成 `TRITON_BACKENDS_TUPLE` 编译期宏。 |
| [setup.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py) | Python 打包入口：枚举后端、生成 entry point、创建后端符号链接、把后端名透传给 CMake。 |
| [third_party/tileir/CMakeLists.txt](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/CMakeLists.txt) | tileir 后端的 CMake：克隆/补丁/构建 cuda-tile、设置 include/lib 路径、注册 `TritonTileIR` 插件并链接静态库。 |
| [third_party/tileir/scripts/patch_bytecode_utils.sh](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/scripts/patch_bytecode_utils.sh) | 对克隆下来的 cuda-tile 源码做 sed 补丁，弥合跨版本 LLVM API 差异。 |
| [third_party/tileir/scripts/build_cuda_tile.sh](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/scripts/build_cuda_tile.sh) | 用 cmake 构建 cuda-tile 并 `install` 到本地目录。 |
| [third_party/tileir/scripts/patch_cuda_tile_i1_bytecode_compat.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/scripts/patch_cuda_tile_i1_bytecode_compat.py) | 处理 i1 dense elements 在跨版本 LLVM 间 bytecode 布局差异的兼容补丁。 |
| [python/src/main.cc](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/src/main.cc) | `libtriton` 的 pybind 入口，`INIT_BACKEND` 宏按后端名展开出 `init_triton_tileir`。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：① cuda-tile 的克隆、补丁与构建；② setup.py 的后端注册与 entry point；③ TritonTileIR 插件的链接与静态库。

---

### 4.1 cuda-tile 的克隆、补丁与构建

#### 4.1.1 概念说明

`cuda_tile` 方言（CUDA Tile IR）并非本仓库原创，而是 NVIDIA 在 [github.com/NVIDIA/cuda-tile](https://github.com/NVIDIA/cuda-tile) 维护的上游项目。它定义了 `cuda_tile` 方言、其 bytecode 读写器（Writer/Reader），以及把 bytecode 编译成 cubin 的 `tileiras`（见 [u2-l7](u2-l7-tileiras-invocation.md)）。

本仓库需要消费 cuda-tile 提供的「头文件 + 静态库」，才能：

- 在 `triton_tileir.cc` 里 `#include "cuda_tile/..."` 头文件并调用其 API；
- 链接 `libCudaTileBytecodeWriter.a` 等静态库，把 IR 序列化成 bytecode 交给 `tileiras`。

问题在于：本仓库走 Triton 3.7 的 LLVM，而 cuda-tile v13.3.0 锁定的是稍旧的 LLVM。直接构建 cuda-tile 会因 API 漂移而编译失败。于是构建系统采用「**锁定版本 + 在构建副本上打补丁**」的策略——签出的 cuda-tile 源码本身保持不变，所有适配补丁只施加到构建目录里那份干净的拷贝上。

#### 4.1.2 核心流程

构建 cuda-tile 的完整因果链（全部在 CMake **configure 期**用 `execute_process` 同步完成）：

```text
1. 探测：CUDA_TILE_INSTALL_DIR 环境变量是否已指向现成安装？
     ├─ 是 → HAVE_CUDA_TILE_INSTALL=ON，直接复用其 include/lib（开发者自带预编译版）
     └─ 否 → 进入「克隆 + 补丁 + 构建」分支
2. 克隆：git clone --depth 1 NVIDIA/cuda-tile 到 <build>/tileir_src
3. 锁版本：git fetch 该 pinned commit 并 checkout FETCH_HEAD，再 checkout tag v13.3.0
4. 补丁：patch_bytecode_utils.sh（sed 重命名 + 调 Python i1 补丁）
5. 构建：build_cuda_tile.sh（cmake -S -B + cmake --build --target install）
6. 落地：安装到 <build>/tileir_src/build/install，回填 CUDA_TILE_* 变量
```

注意第 2 步有一个微妙之处：`--depth 1` 只拉取默认分支的 tip，并不一定包含第 3 步要的 pinned commit，所以脚本先 shallow clone，再 `git fetch --depth 1 origin <commit>` 单独取那个 commit，最后 `git checkout FETCH_HEAD`。紧接着又 `git checkout v13.3.0` 切到 tag。

> 说明：这段克隆逻辑同时使用了 pinned commit 和 tag `v13.3.0` 两种方式。`v13.3.0` 是面向用户的稳定 tag，pinned commit 用于保证补丁与具体源码状态精确匹配。最终生效的是 tag `v13.3.0`（checkout 在后），脚本里也有明确注释「Track specific tag to avoid breaking change conflict with patch」。

#### 4.1.3 源码精读

**探测分支与克隆** —— 见 [third_party/tileir/CMakeLists.txt:16-56](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/CMakeLists.txt#L16-L56)：

- [CMakeLists.txt:16-22](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/CMakeLists.txt#L16-L22)：若环境变量 `CUDA_TILE_INSTALL_DIR` 已设置，直接复用，设 `HAVE_CUDA_TILE_INSTALL=ON`。
- [CMakeLists.txt:25](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/CMakeLists.txt#L25)：否则把克隆目标放在 `${CMAKE_CURRENT_BINARY_DIR}/tileir_src`，刻意放在**构建树**而非源码树，避免污染源码。
- [CMakeLists.txt:38-49](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/CMakeLists.txt#L38-L49)：pinned commit `2e5ccba66fb3afdba34b26cf358418283027c248`，clone → fetch 该 commit → checkout FETCH_HEAD，随后 `git checkout v13.3.0`。

**补丁调用** —— 见 [CMakeLists.txt:58-66](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/CMakeLists.txt#L58-L66)：执行 `patch_bytecode_utils.sh`，失败即 `FATAL_ERROR` 中止配置。

**构建调用** —— 见 [CMakeLists.txt:68-75](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/CMakeLists.txt#L68-L75)：通过 `cmake -E env` 注入 `LLVM_SYSPATH` 与 `LLVM_EXTERNAL_LIT`，执行 `build_cuda_tile.sh`。

**补丁内容** —— [patch_bytecode_utils.sh:43-100](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/scripts/patch_bytecode_utils.sh#L43-L100)（注：此处省略 base，正式链接见下）共施加六类改动，逐条对应一个 LLVM API 差异：

| # | 目标文件 | 改动 | 原因 |
|---|---------|------|------|
| 1 | `BytecodeGenUtilities.cpp` | `getArgToOperandOrAttribute`→`getArgToOperandAttrOrProp`，`OperandOrAttribute`→`OperandAttrOrProp` | MLIR bytecode API 重命名 |
| 2 | `Ops.td` | `std::nullopt`→`ValueRange{}` 等 | `std::nullopt` 不再适用 |
| 3 | `CudaTile.cpp` | `std::nullopt`→`llvm::ArrayRef<mlir::NamedAttribute>{}` | attributes 参数类型变化 |
| 4 | 全局 `.cpp/.h/.td` | `DenseIntOrFPElementsAttr`→`DenseTypedElementsAttr` | Triton 3.7 的 LLVM 重命名该 Attr |
| 5（Python） | `BytecodeWriter.cpp` / `BytecodeReader.cpp` | i1 dense elements 用稳定 packed-bits 格式 | i1 raw 布局跨版本不一致 |
| 6 | `BytecodeReader.cpp` | `scope_exit`→`make_scope_exit`，`isValidRawBuffer` 取 2 参重载 | LLVM 内部 API 变化 |

其中第 4、5 项最值得展开。

第 4 项是**全局 sed 重命名**，见 [patch_bytecode_utils.sh:72-77](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/scripts/patch_bytecode_utils.sh#L72-L77)：注释明确写道「Triton 3.7's LLVM renamed `DenseIntOrFPElementsAttr` to `DenseTypedElementsAttr`。保持 TileIR 13.3 pin 不变，转而桥接这份复制的构建源码，而不是改动已发布的 TileIR 源码。」

第 5 项交给 Python 脚本 [patch_cuda_tile_i1_bytecode_compat.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/scripts/patch_cuda_tile_i1_bytecode_compat.py) 处理，见 [patch_bytecode_utils.sh:79-81](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/scripts/patch_bytecode_utils.sh#L79-L81)。其原理是：`DenseElementsAttr<i1>`（布尔密集属性）在不同 LLVM 版本里 raw 存储布局不同——旧版用紧凑的按位打包（packed bits），新版用每个元素一个字节。Triton 端写出 bytecode、tileiras 端读入 bytecode，两端必须对齐同一种布局，否则 i1 数据会被误读。脚本的 docstring（[patch_cuda_tile_i1_bytecode_compat.py:1-7](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/scripts/patch_cuda_tile_i1_bytecode_compat.py#L1-L7)）点明意图：「checked-out 的 TileIR 源码保持不变；此兼容桥只施加到干净的构建副本，待 bytecode 边界在 Triton 与 tileiras 所用 LLVM 版本间稳定后即可移除。」

它分两半处理：

- **Writer 侧**（[patch_cuda_tile_i1_bytecode_compat.py:15-71](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/scripts/patch_cuda_tile_i1_bytecode_compat.py#L15-L71)）：新增 `packI1DenseElements`，把 i1 元素按位打包成稳定格式后再写入，绕开 LLVM 版本相关的 raw 布局。
- **Reader 侧**（[patch_cuda_tile_i1_bytecode_compat.py:74-128](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/scripts/patch_cuda_tile_i1_bytecode_compat.py#L74-L128)）：新增 `shouldUnpackLegacyPackedI1RawData`/`unpackLegacyPackedI1RawData`，在读取时识别旧式打包布局并归一化。

**构建脚本** —— [build_cuda_tile.sh:16-31](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/scripts/build_cuda_tile.sh#L16-L31)：

- 第 16 行 `: "${LLVM_SYSPATH:?...}"` 要求必须提供 LLVM 系统路径（与 Triton 共用同一份 LLVM，保证 ABI 一致）。
- 第 27-29 行 `cmake -S -B`，关键参数 `-DCUDA_TILE_USE_LLVM_INSTALL_DIR=${LLVM_SYSPATH}` 让 cuda-tile 复用 Triton 的 LLVM。
- 第 31 行 `cmake --build --target install`，产物落到 `${REPO_ROOT}/build/install`。

构建成功后，回到 [CMakeLists.txt:77-90](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/CMakeLists.txt#L77-L90)，把安装路径回填到 `CUDA_TILE_INSTALL_DIR`/`CUDA_TILE_INCLUDE_DIRS`/`CUDA_TILE_LIBRARY_PATH`，并 `FORCE` 写入 CMake CACHE 和环境变量，供下游 include 与链接使用。

#### 4.1.4 代码实践

**实践目标**：在不真正联网构建的前提下，通过阅读脚本画出 cuda-tile 的「来源→版本→补丁→产物」表。

**操作步骤**：

1. 打开 [third_party/tileir/CMakeLists.txt:36-49](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/CMakeLists.txt#L36-L49)，记录 pinned commit 与最终 tag。
2. 打开 [patch_bytecode_utils.sh](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/scripts/patch_bytecode_utils.sh)，逐条列出六类补丁。
3. 打开 [build_cuda_tile.sh](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/scripts/build_cuda_tile.sh)，确认构建用的 LLVM 来源与 install 目标。

**需要观察的现象**：补丁脚本里有若干 `if [[ -f ... ]]` 守卫（如 [patch_bytecode_utils.sh:46](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/scripts/patch_bytecode_utils.sh#L46)）；全局重命名（第 4 项）**没有**文件守卫，是无条件 `find ... -exec sed`。

**预期结果**（参考答案）：

| 维度 | 取值 |
|------|------|
| 来源仓库 | `https://github.com/NVIDIA/cuda-tile` |
| 克隆方式 | `git clone --depth 1` + `git fetch --depth 1 origin <commit>` + `git checkout FETCH_HEAD` |
| pinned commit | `2e5ccba66fb3afdba34b26cf358418283027c248` |
| 最终生效 tag | `v13.3.0` |
| 克隆落点 | `${CMAKE_CURRENT_BINARY_DIR}/tileir_src`（构建树，不污染源码） |
| install 落点 | `${CUDA_TILE_SOURCE_DIR}/build/install` |
| 补丁数量 | 6 类（BytecodeGenUtilities / Ops.td / CudaTile.cpp / 全局 Attr 重命名 / i1 Python 兼容 / BytecodeReader + Dialect.td） |

> 待本地验证：若你在本地执行 `pip install .`，可在 `<build>/tileir_src` 下看到克隆下来的 cuda-tile 源码及其 `.bak` 备份（`patch_in_place` 在 [patch_bytecode_utils.sh:11-13](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/scripts/patch_bytecode_utils.sh#L11-L13) 会先 `cp` 一份 `.bak`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么克隆用 `--depth 1` 之后还要 `git fetch --depth 1 origin <commit>`？直接 `git clone` 拿默认分支不行吗？

**参考答案**：`--depth 1` 只浅克隆默认分支的最新一个 commit；pinned commit `2e5ccba...` 不一定是默认分支 tip，可能取不到。所以先浅克隆建仓，再针对该 commit 做一次浅 fetch 并 checkout，既保持仓库轻量又能精确取到指定提交。

**练习 2**：补丁脚本刻意强调「checked-out 的 TileIR 源码保持不变，补丁只施加到构建副本」。这样设计的好处是什么？

**参考答案**：保持上游 cuda-tile 仓库的纯净与可追溯（升级 tag 时冲突小、可 diff），把跨版本兼容性隔离在构建期；一旦 Triton 与 tileiras 所用 LLVM 的 bytecode 边界稳定，直接删除这些补丁即可，不影响任何源码。

---

### 4.2 setup.py 如何把 tileir 注册为 in-tree 后端

#### 4.2.1 概念说明

要让 `import triton` 之后能发现 `tileir` 后端，需要三件事协同：

1. **包发现**：`triton.backends.tileir` 这个 Python 包路径必须存在（哪怕是符号链接）。
2. **entry point 注册**：setuptools 元数据里要有一条 `triton.backends` 组下的 `tileir = triton.backends.tileir` 条目，运行期由 [u2-l1](u2-l1-backend-selection.md) 的 `_discover_backends()` 读取。
3. **CMake 透传**：后端名要进入 CMake 的 `TRITON_CODEGEN_BACKENDS`，这样 C++ 侧的 `add_subdirectory(third_party/tileir)` 与 `init_triton_tileir` 才会被启用。

`setup.py` 用一个 `BackendInstaller` + 一组 `package_*` / `entry_point` / `symlink` 辅助函数把这三件事一次完成。它对 `nvidia`、`amd`、`tileir` **一视同仁**——tileir 没有任何特殊待遇，只是 `third_party` 下多了一个目录。

#### 4.2.2 核心流程

```text
setup.py 启动
  │
  ├─ backends = BackendInstaller.copy(["nvidia","amd","tileir"])
  │     └─ 对每个后端调用 prepare()：断言 third_party/<name>/backend 存在
  │        且含 compiler.py / driver.py，构造 Backend 数据类
  │
  ├─ CMakeBuild.build_extension() 把后端名透传给 CMake：
  │     -DTRITON_CODEGEN_BACKENDS=nvidia;amd;tileir
  │
  ├─ get_package_dirs() / get_packages()：把 triton.backends.tileir 等纳入打包
  │
  ├─ get_entry_points()：生成 triton.backends 组的 entry point
  │
  └─ plugin_develop/plugin_install 等 cmdclass：
        add_link_to_backends() 创建符号链接
        python/triton/backends/tileir -> third_party/tileir/backend
```

关键结论：磁盘上没有 `python/triton/backends/tileir/` 实体目录，它是构建期由 `update_symlink` 建立的符号链接，指向 `third_party/tileir/backend`。这正是 [u1-l3](u1-l3-repo-structure.md) 所说的「源码树里找不到属正常」的根因。

#### 4.2.3 源码精读

**后端枚举** —— [setup.py:375](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py#L375)：

```python
backends = [*BackendInstaller.copy(["nvidia", "amd", "tileir"]), *BackendInstaller.copy_externals()]
```

`tileir` 与 `nvidia`、`amd` 并列出现在硬编码列表里——这是 tileir 成为 in-tree 后端的**唯一声明点**。

**prepare 断言** —— [setup.py:59-97](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py#L59-L97)：对每个后端，断言 `third_party/<name>/backend` 存在，且其中必含 `compiler.py` 与 `driver.py`（[setup.py:91-92](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py#L91-L92)）；同时探测可选的 `language/`、`tools/` 子目录（tileir 当前没有顶层 `language/`，故为 `None`）。返回的 `Backend` 数据类记下源目录与安装目录。

**CMake 透传** —— [setup.py:284-285](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py#L284-L285)：

```python
"-DTRITON_CODEGEN_BACKENDS=" + ';'.join([b.name for b in backends if not b.is_external]),
```

非外部后端名以分号拼接传给 CMake，值为 `nvidia;amd;tileir`。这一行是 Python 打包与 CMake 构建之间的**唯一桥接点**之一。

**包路径与 entry point** —— [setup.py:378-402](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py#L378-L402)（`get_package_dirs`）把 `triton.backends.<name>` 映射到 `backend_dir`；[setup.py:510-518](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py#L510-L518)（`get_entry_points`）生成 entry point：

```python
entry_points["triton.backends"] = [f"{b.name} = triton.backends.{b.name}" for b in backends]
```

对 tileir 即产出 `tileir = triton.backends.tileir`，写入 wheel 元数据。

**符号链接创建** —— [setup.py:427-432](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py#L427-L432)（`add_link_to_backends`）对每个后端调用 `update_symlink(backend.install_dir, backend.backend_dir)`，其中 `install_dir` 为 `python/triton/backends/tileir`、`backend_dir` 为 `third_party/tileir/backend`。`update_symlink` 的实现见 [setup.py:174-185](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py#L174-L185)：若已存在则先删除再 `symlink_to`。

这套机制对不同安装方式有不同触发点（见 [setup.py:466-498](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py#L466-L498)）：`develop`/`editable_wheel`（即可编辑安装 `pip install -e .`）调用 `add_links(external_only=False)` 创建**全部**符号链接；`install`/`bdist_wheel`/`egg_info` 用 `external_only=True`，只处理外部插件，in-tree 后端则靠打包进 wheel 的实体文件。

> 补充：wheel 内还会捆绑 CUDA 依赖（`tileiras` 等）。[copy_wheel_deps.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/scripts/copy_wheel_deps.py) 依据 [wheel_deps.json](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/scripts/wheel_deps.json) 把 `cuda_dep_${arch}` 目录复制进 `third_party/tileir/backend`，使 `CUDA_HOME` 能指向 `backend/cuda_dep`（这正是 [u1-l2](u1-l2-install-and-run.md) 里「wheel 内嵌 tileiras/ptxas，无需另装 CTK」的实现）。

#### 4.2.4 代码实践

**实践目标**：验证「后端注册的三个出口」确实都为 tileir 生成了对应条目。

**操作步骤**：

1. 在 [setup.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py) 中定位三处：第 375 行的枚举、第 284 行的 CMake 透传、第 517 行的 entry point。
2. 若你已用 `pip install -e .` 做过可编辑安装，检查符号链接：
   - 预期 `python/triton/backends/tileir` 是一个指向 `../../third_party/tileir/backend` 的符号链接。

**需要观察的现象**：`ls -l python/triton/backends/` 应能看到 `tileir ->` 形式的链接条目，而非普通目录。

**预期结果**：三个出口一致产出 `tileir`：

| 出口 | 产物 |
|------|------|
| CMake 透传 | `-DTRITON_CODEGEN_BACKENDS=nvidia;amd;tileir` |
| entry point | `triton.backends` 组下 `tileir = triton.backends.tileir` |
| 符号链接（可编辑安装） | `python/triton/backends/tileir` → `third_party/tileir/backend` |

> 待本地验证：符号链接是否存在取决于是否执行过安装命令；纯源码 checkout 不会有该链接。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `"tileir"` 从 [setup.py:375](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py#L375) 的列表里删掉，会发生什么？请分别说明对 CMake、entry point、运行期的影响。

**参考答案**：CMake 不再收到 `tileir`，于是 `add_subdirectory(third_party/tileir)` 不执行、`init_triton_tileir` 不注册、C++ 插件不构建；entry point 元数据里没有 `tileir`；运行期 `_discover_backends()` 找不到该后端，即便 `ENABLE_TILE=1` 也无法选中 `TileIRDriver`。一个列表元素牵动三条链路。

**练习 2**：为什么 `develop`（可编辑安装）和 `install`（普通安装）对 in-tree 后端的处理不同（前者建符号链接，后者不建）？

**参考答案**：可编辑安装下，源码随时在变，符号链接让 `import triton.backends.tileir` 始终读到最新源码；普通安装则把后端文件作为包数据打进 wheel/site-packages，不再依赖源码树，故无需链接。

---

### 4.3 TritonTileIR 插件的链接与静态库

#### 4.3.1 概念说明

[u3-l1](u3-l1-pass-plugin-skeleton.md) 已建立：`tileir` Python 模块来自 C++ 插件 `TritonTileIR`，源文件 `triton_tileir.cc`，通过 pybind 暴露 `tileir.passes.*` 等函数。本模块回答：**这个插件编译时链接了哪些库，它们从哪里来？**

库分两类：

- **in-tree 库**：本仓库自己用 `add_triton_library` 构建的库，如 `TritonToTileIR`、`TritonTileIRTransforms`、`Utils`。
- **cuda-tile 静态库**：第 4.1 节构建 cuda-tile 后产出的 `.a` 归档，如 `libCudaTileTransforms.a`、`libCudaTileBytecodeWriter.a`。

插件把它们静态链接进最终的 `libtriton`，使得运行期无需额外 `.so` 即可调用 cuda-tile 的 API（如写出 bytecode）。

#### 4.3.2 核心流程

```text
add_triton_plugin(TritonTileIR  triton_tileir.cc
                  LINK_LIBS TritonToTileIR TritonTileIRTransforms)   # in-tree 库
    │
    ├─ target_link_libraries(... Python3::Module pybind11::headers)   # Python 绑定
    │
    └─ target_link_libraries(... ${CUDA_TILE_LIBRARY_PATH}/libXxx.a)  # cuda-tile 静态库 ×3
            libCudaTileTransforms.a
            libCudaTileBytecodeWriter.a
            libCudaTileBytecodeCommon.a
```

其中 in-tree 库 `TritonToTileIR` / `TritonTileIRTransforms` 自身又**传递性地**链接了 `libCudaTileDialect.a` 等静态库（见其各自的 CMakeLists），所以最终 `libtriton` 实际吸收的 cuda-tile 符号比插件直接声明的三个更多。

#### 4.3.3 源码精读

**插件注册与直接链接** —— [third_party/tileir/CMakeLists.txt:112-117](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/CMakeLists.txt#L112-L117)：

```cmake
if(TRITON_BUILD_PYTHON_MODULE AND HAVE_CUDA_TILE_INSTALL)
  add_triton_plugin(TritonTileIR ${CMAKE_CURRENT_SOURCE_DIR}/triton_tileir.cc
                    LINK_LIBS TritonToTileIR TritonTileIRTransforms)
  target_link_libraries(TritonTileIR PRIVATE Python3::Module pybind11::headers)
  target_link_libraries(TritonTileIR PRIVATE ${CUDA_TILE_LIBRARY_PATH}/libCudaTileTransforms.a)
  target_link_libraries(TritonTileIR PRIVATE ${CUDA_TILE_LIBRARY_PATH}/libCudaTileBytecodeWriter.a)
  target_link_libraries(TritonTileIR PRIVATE ${CUDA_TILE_LIBRARY_PATH}/libCudaTileBytecodeCommon.a)
elseif(TRITON_BUILD_PYTHON_MODULE)
  message(STATUS "Skip building TritonTileIR Python plugin: CUDA_TILE_INSTALL_DIR not detected")
endif()
```

两个守卫条件值得注意：

- `TRITON_BUILD_PYTHON_MODULE`：仅在构建 Python 绑定时（由 [setup.py:281](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/setup.py#L281) 设为 `ON`）。
- `HAVE_CUDA_TILE_INSTALL`：第 4.1 节里 cuda-tile 成功获取/构建后置 `ON`。两者同时满足才构建插件；否则打印「Skip」并跳过——这意味着如果没有可用的 cuda-tile 安装，整个 TileIR 后端的 C++ 侧会被静默跳过（只留一条 STATUS 日志）。

**`add_triton_plugin` 宏** —— 定义在根 [CMakeLists.txt:264-268](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/CMakeLists.txt#L264-L268)：

```cmake
function(add_triton_plugin name)
  set_property(GLOBAL APPEND PROPERTY TRITON_PLUGINS ${name})
  add_triton_object(${name} ${ARGN})
endfunction()
```

它把插件名追加进全局 `TRITON_PLUGINS` 属性（之后会被并入 `libtriton` 的 `TRITON_LIBRARIES`，见 [CMakeLists.txt:347-352](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/CMakeLists.txt#L347-L352)），并调用 `add_triton_object` 构造目标、解析 `LINK_LIBS`。

**in-tree 库对 cuda-tile 的传递依赖**：

- [lib/TritonToTileIR/CMakeLists.txt:11-21](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/CMakeLists.txt#L11-L21)：`TritonToTileIR` 以 `PUBLIC` 链接 `libCudaTileDialect.a` 与 `libCudaTileTransforms.a`。
- [lib/Transform/CMakeLists.txt:11-22](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/CMakeLists.txt#L11-L22)：`TritonTileIRTransforms` 以 `PUBLIC` 链接 `libCudaTileDialect.a`。
- [lib/Utils/CMakeLists.txt:6-10](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Utils/CMakeLists.txt#L6-L10)：`Utils` 链接 `libCudaTileDialect.a`。

由于是 `PUBLIC`，这些静态库会被传递到链接 `TritonToTileIR`/`TritonTileIRTransforms` 的 `TritonTileIR` 插件，最终并入 `libtriton`。

**C++ 入口与 INIT_BACKEND 串联** —— 回顾 [u3-l1](u3-l1-pass-plugin-skeleton.md)：根 [CMakeLists.txt:405-414](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/CMakeLists.txt#L405-L414) 把 `TRITON_CODEGEN_BACKENDS` 拼成 `TRITON_BACKENDS_TUPLE=(nvidia,amd,tileir)` 并 `add_compile_definitions`；[main.cc:37](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/src/main.cc#L37) 的宏 `#define INIT_BACKEND(name) init_triton_##name(m.def_submodule(#name));` 与 [main.cc:63](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/src/main.cc#L63) 的 `FOR_EACH_P(INIT_BACKEND, TRITON_BACKENDS_TUPLE)` 共同把它展开成 `init_triton_tileir(m.def_submodule("tileir"))`——即 `triton_tileir.cc` 里实现的那个 pybind 注册函数。这就是「后端名从 setup.py 一路贯穿到 C++ 入口函数名」的完整链路。

#### 4.3.4 代码实践

**实践目标**：列出 `TritonTileIR` 插件最终链接的全部 cuda-tile 静态库，并区分直接链接与传递链接。

**操作步骤**：

1. 打开 [third_party/tileir/CMakeLists.txt:112-117](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/CMakeLists.txt#L112-L117)，抄下三个直接 `target_link_libraries` 的 `.a`。
2. 打开 [lib/TritonToTileIR/CMakeLists.txt](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/CMakeLists.txt) 与 [lib/Transform/CMakeLists.txt](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/CMakeLists.txt)，记下它们 `PUBLIC` 链接的 `.a`。
3. 在 `triton_tileir.cc` 顶部确认这些库对应的头文件 include（如 `cuda_tile/Bytecode/Writer/BytecodeWriter.h`，见 [triton_tileir.cc:36](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L36)）。

**需要观察的现象**：插件直接链接 3 个 `.a`；in-tree 库又以 `PUBLIC` 引入 `libCudaTileDialect.a`（被多个库重复声明，但静态库重复链接会被链接器去重）。

**预期结果**（参考答案）：

| 类别 | 静态库名称 | 来源 |
|------|-----------|------|
| 直接链接 | `libCudaTileTransforms.a` | 插件 CMakeLists 显式声明 |
| 直接链接 | `libCudaTileBytecodeWriter.a` | 插件 CMakeLists 显式声明 |
| 直接链接 | `libCudaTileBytecodeCommon.a` | 插件 CMakeLists 显式声明 |
| 传递链接（经 TritonToTileIR / TritonTileIRTransforms / Utils） | `libCudaTileDialect.a` | 各 in-tree 库 `PUBLIC` 链接 |

此外插件还链接 in-tree 的 `TritonToTileIR`、`TritonTileIRTransforms`（经 `add_triton_plugin` 的 `LINK_LIBS`），以及 `Python3::Module`、`pybind11::headers`。

> 待本地验证：构建后可用 `nm libtriton*.so | grep -i cudatile` 或查看 `build.ninja` 里 `TritonTileIR` 目标的链接行，确认上述 `.a` 确实被吸收。

#### 4.3.5 小练习与答案

**练习 1**：[third_party/tileir/CMakeLists.txt:112](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/CMakeLists.txt#L112) 的 `if` 同时要求 `TRITON_BUILD_PYTHON_MODULE` 与 `HAVE_CUDA_TILE_INSTALL`。如果只满足前者、不满足后者，会发生什么？为什么这样设计？

**参考答案**：会进入 `elseif` 分支，打印「Skip building TritonTileIR Python plugin: CUDA_TILE_INSTALL_DIR not detected」并跳过插件构建。这样设计是为了在没有 cuda-tile 可用时（比如只构建纯 MLIR 工具 `triton-cuda-tile-opt`、或未联网拉取 cuda-tile 的环境）不阻断整体配置，让其他目标仍可构建。

**练习 2**：为什么 in-tree 库（如 `TritonToTileIR`）对 cuda-tile 静态库用 `PUBLIC` 链接而非 `PRIVATE`？

**参考答案**：因为 `TritonToTileIR` 暴露的头文件类型/符号本身依赖 cuda-tile 的类型（如 `cuda_tile::ModuleOp`），任何链接 `TritonToTileIR` 的目标（包括 `TritonTileIR` 插件）都需要 cuda-tile 的符号参与链接解析。用 `PUBLIC` 才能把 `libCudaTileDialect.a` 等传递到下游，避免链接期出现未定义符号。

---

## 5. 综合实践

把三个模块串起来，绘制一张「**一次 `pip install .` 的完整构建数据流**」图，并标注每个环节对应的源码位置与产物。

要求：

1. 从用户敲下 `pip install .` 开始，依次画出：
   - `setup.py` 枚举后端 → 透传 `TRITON_CODEGEN_BACKENDS` → 创建 entry point 与符号链接。
   - CMake configure：根 CMake 遍历后端做 `add_subdirectory(third_party/tileir)`。
   - tileir CMake：探测 cuda-tile → 克隆（commit + tag）→ 补丁（6 类）→ 构建 → 回填路径。
   - 插件构建：`add_triton_plugin(TritonTileIR ...)` 链接 in-tree 库 + 3 个 cuda-tile 静态库。
   - C++ 入口：`TRITON_BACKENDS_TUPLE` → `INIT_BACKEND` 宏 → `init_triton_tileir`。
2. 在每个节点旁标注**真实文件路径与行号**（用永久链接形式）。
3. 用一句话回答：如果构建机器断网（`TRITON_OFFLINE_BUILD` 但未提供 `CUDA_TILE_INSTALL_DIR`），这条链路会在哪一步失败？为什么？

**参考答案要点**：

- 断网点在 [third_party/tileir/CMakeLists.txt:39-43](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/CMakeLists.txt#L39-L43) 的 `git clone`，因无法访问 `github.com/NVIDIA/cuda-tile` 而失败，随后 `CUDA_TILE_GIT_CLONE_RESULT` 非零触发 `FATAL_ERROR`。
- 规避方法：提前在外部构建好 cuda-tile，通过 `CUDA_TILE_INSTALL_DIR` 环境变量指向其安装目录，走 [CMakeLists.txt:16-22](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/CMakeLists.txt#L16-L22) 的「复用现成安装」分支，跳过克隆/补丁/构建。

## 6. 本讲小结

- cuda-tile 是 NVIDIA 上游项目，构建期从 `github.com/NVIDIA/cuda-tile` 按 pinned commit `2e5ccba...` 与 tag `v13.3.0` 克隆到构建树，刻意不污染源码树。
- 因为 Triton 3.7 的 LLVM 比 cuda-tile v13.3.0 更新，`patch_bytecode_utils.sh` 施加 6 类补丁（含全局 `DenseIntOrFPElementsAttr` 重命名与 i1 bytecode 布局兼容）弥合 API 差异，补丁只作用于构建副本、源码保持纯净。
- `setup.py` 把 `tileir` 与 `nvidia`/`amd` 一视同仁地注册为 in-tree 后端，通过 `-DTRITON_CODEGEN_BACKENDS` 透传给 CMake、通过 `triton.backends` entry point 暴露给运行期、通过符号链接让 `python/triton/backends/tileir` 指向 `third_party/tileir/backend`。
- `TritonTileIR` 插件直接链接三个 cuda-tile 静态库（`libCudaTileTransforms.a`/`libCudaTileBytecodeWriter.a`/`libCudaTileBytecodeCommon.a`），并经 in-tree 库传递性地引入 `libCudaTileDialect.a`。
- 后端名 `tileir` 从 `setup.py` 列表 → `TRITON_CODEGEN_BACKENDS` → `TRITON_BACKENDS_TUPLE` 编译期宏 → `INIT_BACKEND` 宏 → `init_triton_tileir` 函数名，是一条贯穿 Python 与 C++ 的单一命名链。
- 构建容错点：`HAVE_CUDA_TILE_INSTALL` 未就绪时插件被静默跳过（仅 STATUS 日志），不阻断其他目标；断网且无预装时则在 `git clone` 处 `FATAL_ERROR`。

## 7. 下一步学习建议

- 回到 [u4-l1](u4-l1-opt-tool-and-lit-tests.md)：用本讲建立的「构建产物位置」认知，去 `build` 目录里找到 `triton-cuda-tile-opt` 可执行文件与 lit 测试，亲手跑一个 [u4-l1](u4-l1-opt-tool-and-lit-tests.md) 描述的 lit/FileCheck 用例。
- 阅读 [third_party/tileir/CMakeLists.txt:95-101](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/CMakeLists.txt#L95-L101) 里为 lit 测试设置的 `TRITON_CUDA_TILE_*` 变量，理解构建期如何把工具路径注入测试配置。
- 若对跨版本 LLVM 兼容感兴趣，可对比 [patch_cuda_tile_i1_bytecode_compat.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/scripts/patch_cuda_tile_i1_bytecode_compat.py) 的 Writer/Reader 两半，体会「写出端用稳定格式、读入端做归一化」这一跨版本数据兼容的经典手法。
- 至此整套手册（u1–u4）的编译链路、Pass 体系、调优测试、容错与构建已闭环；建议以一个真实 dot 类内核为线索，从 `@triton.jit` 出发重走一遍 AST→TTIR→cuda_tile→bytecode→cubin→启动的全链路，把所有讲义串联验收。
