# 仓库结构与构建系统

## 1. 本讲目标

本讲承接 [u1-l1](u1-l1-project-overview.md)，在已经知道「3FS 是什么、由哪些组件组成」之后，回答一个更落地的问题：**这一大坨代码放在哪里、怎么把它编译成可以运行的服务程序？**

读完本讲，你应当能够：

1. 说出 `src/` 下每个一级子目录的职责，拿到任何一个 `.cpp` 能快速判断它属于哪个组件。
2. 独立完成一次 CMake 配置与构建，知道为什么必须传 `-DSHUFFLE_METHOD=...`，以及它锁定了什么。
3. 解释 Rust 写的 chunk engine 是如何被「嵌」进 C++ 构建里的——`cargo` 与 `cmake` 是怎么协作的。
4. 搞清楚 3FS 的依赖来源：哪些来自 git 子模块、哪些来自系统包、哪些需要单独安装（libfuse / FoundationDB / Rust）。

本讲是一切后续源码阅读的前置：先能把项目编译跑起来，才有条件去打断点、改参数、看输出。

## 2. 前置知识

在开始之前，最好对以下概念有个大致印象（不熟练没关系，本讲会结合源码再解释）：

- **构建系统（Build System）**：把人类写的源码翻译成机器能跑的二进制程序的「流水线」。3FS 的 C++/C 部分用 **CMake**，Rust 部分用 **Cargo**，两者再缝在一起。
- **CMake**：C/C++ 项目里最常用的「构建脚本生成器」。它本身不编译代码，而是根据你写的 `CMakeLists.txt` 生成 Makefile 或 Ninja 文件，再调用编译器。
- **Cargo 与 Cargo workspace**：Rust 的官方构建工具与包管理器。一个 *workspace* 可以把多个互相独立的 Rust crate（相当于「包」）放在同一个 `Cargo.toml` 下统一构建。
- **git 子模块（submodule）**：在一个 git 仓库里嵌套引用另一个 git 仓库的特定 commit。3FS 用它来引入 folly、rocksdb 等第三方库。
- **FFI（Foreign Function Interface）**：不同语言之间互相调用函数的机制。3FS 用 Rust 库 [cxx](https://cxx.rs/) 让 C++ 安全地调用 Rust 写的 chunk engine。

## 3. 本讲源码地图

本讲围绕「构建如何发生」展开，涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [CMakeLists.txt](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/CMakeLists.txt) | 顶层 CMake 脚本：定义项目、`SHUFFLE_METHOD`、编译标准、引入全部第三方库、最后进入 `src/`。 |
| [src/CMakeLists.txt](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/CMakeLists.txt) | 列出 3FS 自身的 18 个一级模块，按顺序 `add_subdirectory`。 |
| [Cargo.toml](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/Cargo.toml) | Rust workspace 根：声明 3 个成员 crate 与统一的 `rust-version`。 |
| [cmake/Target.cmake](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/cmake/Target.cmake) | 定义 `target_add_bin` 等宏，决定二进制输出到 `build/bin`。 |
| [cmake/AddCrate.cmake](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/cmake/AddCrate.cmake) | `add_crate` 宏：让 CMake 在构建时先跑 `cargo build`，再把产物当成 C++ 静态库链接。 |
| [src/storage/CMakeLists.txt](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/CMakeLists.txt) | storage 服务的构建：`add_crate(chunk_engine)` + 产出 `storage_main`。 |
| [src/storage/chunk_engine/Cargo.toml](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/Cargo.toml) | chunk engine 这个 Rust crate 的清单（依赖、crate 类型）。 |
| [.gitmodules](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/.gitmodules) | 声明 14 个 git 子模块（folly、rocksdb 等）。 |
| [README.md](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/README.md) | 官方构建说明：依赖安装、`cmake` 命令、`SHUFFLE_METHOD` 解释。 |
| [patches/apply.sh](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/patches/apply.sh) | 拉取子模块后给 rocksdb / folly 打补丁的脚本。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 CMake 构建**：从顶层脚本一路走到 `build/bin` 下的二进制。
- **4.2 SHUFFLE_METHOD**：构建时那个必填参数到底锁定了什么（独立成节，因为它是 3FS 最容易踩的构建坑）。
- **4.3 Rust crate**：Cargo workspace 与 chunk engine 如何嵌入 C++ 构建。
- **4.4 依赖与子模块**：第三方库来自哪里、怎么被组织。

### 4.1 CMake 构建：从配置到二进制

#### 4.1.1 概念说明

3FS 的主体是用 **C++20** 写的（少量 C 与汇编）。CMake 的核心思想是「**先配置（configure），再构建（build）**」两个阶段：

- **配置阶段**（`cmake -S . -B build ...`）：读 `CMakeLists.txt`，检查编译器、依赖是否齐全，生成 `build/` 目录里的构建文件（Makefile 或 `compile_commands.json`）。
- **构建阶段**（`cmake --build build -j 32`）：真正调用编译器，把源码编成静态库与可执行文件。

3FS 的所有「最终程序」（`storage_main`、`meta_main`、`mgmtd_main`、`hf3fs_fuse_main`、`admin_cli` 等）都会被统一放进 `build/bin/`。

#### 4.1.2 核心流程

顶层 CMake 脚本的执行顺序可以概括为：

```text
1. project(3FS)            # 命名项目、声明语言为 C/CXX
2. 选定构建类型             # RelWithDebInfo / Debug / Release / MinSizeRel
3. 校验 SHUFFLE_METHOD      # 必填，否则 FATAL_ERROR（见 4.2）
4. 设定语言标准与编译选项   # C++20、-Wall -Wextra -Werror、架构指令集
5. 逐个 add_subdirectory(third_party/*)   # 编译 folly/rocksdb/... 等依赖
6. find_package(Boost) 等   # 找系统里已装好的库
7. add_subdirectory(src)    # 进入 3FS 自己的 18 个模块
8. add_subdirectory(tests / benchmarks)
```

第 7 步是关键转折点：从这里开始进入 3FS 自己的代码。`src/CMakeLists.txt` 把组件逐个挂上来：

```text
src/
├── fbs/            # FlatBuffers schema 与自动生成的桩代码
├── common/         # 公共基础设施：网络、协程、serde、配置、KV 接口
├── core/           # 服务启动骨架（TwoPhaseApplication / ServerLauncher）
├── fdb/            # FoundationDB 客户端封装
├── meta/           # 元数据服务  → meta_main
├── storage/        # 存储服务（含 Rust chunk engine） → storage_main
├── mgmtd/          # 集群管理服务 → mgmtd_main
├── client/         # 客户端库 + admin_cli 工具
├── fuse/           # FUSE 守护进程 → hf3fs_fuse_main
├── lib/            # USRBIO 原生 API 与 Python 绑定
├── kv/             # KVStore 抽象（LevelDB/RocksDB/MemDB 后端）
├── memory/ tools/ stubs/ analytics/ monitor_collector/ migration/ simple_example/
```

这些目录与 [u1-l1](u1-l1-project-overview.md) 讲的四大组件一一对应：`mgmtd/` = cluster manager、`meta/` = metadata service、`storage/` = storage service、`client/` + `fuse/` + `lib/` = client。

#### 4.1.3 源码精读

顶层脚本先声明项目与语言，并默认使用带调试信息的优化构建：

[CMakeLists.txt:1-9](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/CMakeLists.txt#L1-L9) — 项目名为 `3FS`，语言为 `C CXX`；若未指定 `CMAKE_BUILD_TYPE`，默认设为 `RelWithDebInfo`（既开 `-O3` 又保留调试符号）。

接着固定语言标准，这是「3FS 用的是 C++20」这件事的源头：

[CMakeLists.txt:74-81](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/CMakeLists.txt#L74-L81) — `CMAKE_CXX_STANDARD 20` 且 `CMAKE_CXX_STANDARD_REQUIRED ON`，意味着编译器必须支持 C++20（协程、concept 等特性都依赖于此）；同时开启 `CMAKE_EXPORT_COMPILE_COMMANDS`，为 `clangd`/IDE 生成 `compile_commands.json`。

针对编译器与 CPU 架构，脚本做了分支处理：

[CMakeLists.txt:83-106](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/CMakeLists.txt#L83-L106) — Clang 需要加 `-fcoroutines-ts`、用 `lld` 链接器并链接 `libatomic`；GCC 则用 `-fcoroutines`。x86_64 平台开启 `-msse4.2 -mavx2` 指令集（用于 SIMD 加速），aarch64（ARM）则用 `-march=armv8-a+crc` 并配合 `compiler-rt`/`libgcc` 解决 `__muloti4` 等符号缺失问题。

依赖就绪后，脚本最后才进入 3FS 自身：

[CMakeLists.txt:206-208](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/CMakeLists.txt#L206-L208) — `add_subdirectory(src)` / `tests` / `benchmarks`，分别编译主程序、测试与基准。

进入 `src/` 后，18 个模块按固定顺序挂载：

[src/CMakeLists.txt:1-19](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/CMakeLists.txt#L1-L19) — 全文就是 18 行 `add_subdirectory(...)`，顺序即依赖顺序：`fbs`（生成代码）最先，因为它被 `common` 等依赖；`common`/`core`/`fdb` 等基础设施在前，`meta`/`storage`/`mgmtd` 等业务服务在后。

最终二进制落在哪？由一个公共宏决定：

[cmake/Target.cmake:40-53](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/cmake/Target.cmake#L40-L53) — `target_add_bin(NAME, MAIN_FILE)` 宏负责创建可执行目标，并通过 `set_target_properties(... RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")` 把所有二进制统一输出到 `<构建目录>/bin`。如果你用 `-B build` 配置，那就是 `build/bin/`；它还会为非 Debug 构建开启 IPO（过程间优化，即 LTO）。

以 storage 服务为例，它就是用这个宏产出的：

[src/storage/CMakeLists.txt:1-5](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/CMakeLists.txt#L1-L5) — 第 1 行 `add_crate(chunk_engine)` 把 Rust 引擎接进来（见 4.3）；第 3 行把若干静态库聚合成 `storage` 库；第 5 行 `target_add_bin(storage_main "storage.cpp" storage jemalloc)` 编出最终的 `storage_main`。下表列出用同样方式产出的主要二进制：

| 二进制（位于 `build/bin/`） | 源入口 | 所属组件 |
| --- | --- | --- |
| `storage_main` | `src/storage/storage.cpp` | storage service |
| `meta_main` | `src/meta/meta.cpp` | metadata service |
| `mgmtd_main` | `src/mgmtd/mgmtd.cpp` | cluster manager |
| `hf3fs_fuse_main` | `src/fuse/hf3fs_fuse.cpp` | FUSE 客户端守护进程 |
| `admin_cli` | `src/client/bin/admin_cli.cc` | 运维管理 CLI |
| `hf3fs-admin` | `src/tools/admin.cc` | 另一套管理工具 |
| `monitor_collector_main` | `src/monitor_collector/monitor_collector.cpp` | 指标采集上报 |
| `simple_example_main` | `src/simple_example/main.cpp` | 教学用最小服务模板 |

#### 4.1.4 代码实践

> **实践目标**：不真正编译，只通过阅读 `src/CMakeLists.txt` 与 `cmake/Target.cmake`，预测一次成功构建后 `build/bin/` 下会出现哪些二进制，并验证你的判断。

1. 打开 [src/CMakeLists.txt:1-19](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/CMakeLists.txt#L1-L19)，记下 18 个子目录。
2. 用 `Grep` 在仓库里搜索 `target_add_bin(`（限定 `**/CMakeLists.txt`），把每一处 `target_add_bin(NAME ...)` 的第一个参数 `NAME` 收集起来。
3. 对照上表，确认每个 `NAME` 对应的 `.cpp` 入口（即宏的第二个参数）。
4. **需要观察的现象**：搜索结果里的 `NAME` 数量、与上表的对应关系。
5. **预期结果**：你会得到约 10 个可执行目标，且它们的输出目录都由 `cmake/Target.cmake` 里的 `RUNTIME_OUTPUT_DIRECTORY` 统一指向 `build/bin`。
6. 如果想在本地真正验证，需在有完整依赖的环境执行 4.1.1 的两阶段命令（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 3FS 默认构建类型是 `RelWithDebInfo` 而不是 `Release`？

> **答案**：`RelWithDebInfo` 在开 `-O3` 优化的同时保留调试符号（见顶层脚本对 `CMAKE_CXX_FLAGS_RELWITHDEBINFO` 的设置）。对一个线上跑分布式存储、又需要排查问题的系统，保留符号便于用 `gdb`/`perf` 分析崩溃与性能，因此作为默认。

**练习 2**：`src/CMakeLists.txt` 里 `add_subdirectory` 的顺序重要吗？为什么 `fbs` 排在最前面？

> **答案**：重要。`fbs` 用 FlatBuffers schema 生成 C++ 桩代码（`*-generated`），而 `common`、`meta` 等模块会 `#include` 这些生成头文件，所以 `fbs` 必须先构建。CMake 的子目录顺序基本就是依赖顺序。

### 4.2 SHUFFLE_METHOD：为什么构建必须锁死洗牌算法

#### 4.2.1 概念说明

`SHUFFLE_METHOD` 是 3FS 构建时**唯一一个必填参数**。如果你在配置时不传它，CMake 会直接报致命错误并停下来。它解决的是一个很隐蔽但很致命的问题：**不同编译器版本的 `std::shuffle` 算法不一样，会导致二进制互不兼容。**

3FS 在分配数据放置位置时会用到 `std::shuffle`（带固定随机种子）。如果集群里有的节点用 g++10 编译、有的用 g++11 编译，它们对同一个输入算出的「打乱结果」不同，于是数据放错地方、读写错位。所以必须在构建期把洗牌算法**锁死成某一个具体实现**，保证整个集群行为一致。

#### 4.2.2 核心流程

```text
用户传 -DSHUFFLE_METHOD=<method>
        │
        ├── 合法值校验：只能是 g++10 / g++11 / stdshuffle
        │     └── 非法或为空 → message(FATAL_ERROR) 中止配置
        │
        └── 映射成编译宏（compile definition）
              g++10       → USE_GCC10_SHUFFLE
              g++11       → USE_GCC11_SHUFFLE
              stdshuffle  → USE_STD_SHUFFLE
                    │
                    └── 源码里用 #ifdef 选择对应的具体洗牌函数实现
```

三种方法的含义：

- `g++10`：使用 g++10 标准库里的 shuffle 实现（对应旧集群）。
- `g++11`：使用 g++11 及以后版本的 shuffle 实现。
- `stdshuffle`：直接用当前编译器的 `std::shuffle`（不推荐用于跨版本兼容）。

#### 4.2.3 源码精读

顶层脚本首先定义合法取值，并把 `SHUFFLE_METHOD` 声明为一个缓存变量：

[CMakeLists.txt:30-32](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/CMakeLists.txt#L30-L32) — `SHUFFLE_METHODS` 列出三个合法值；`SHUFFLE_METHOD` 默认为空字符串（注释里特别写了 `REQUIRED`）。

接着做强制校验——为空或非法都直接终止：

[CMakeLists.txt:34-45](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/CMakeLists.txt#L34-L45) — 若 `SHUFFLE_METHOD` 为空，`message(FATAL_ERROR ...)` 会打印错误并中止整个配置；若值不在白名单里同样中止。这就是为什么「不传这个参数就配不过」。

最后把选项翻译成预处理宏，交给源码去选择实现：

[CMakeLists.txt:48-54](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/CMakeLists.txt#L48-L54) — `stdshuffle`→`USE_STD_SHUFFLE`，`g++10`→`USE_GCC10_SHUFFLE`，`g++11`→`USE_GCC11_SHUFFLE`。C++ 源码中会根据这些宏 `#ifdef` 选择具体调用哪一个洗牌实现。

README 给出了官方解释与选择建议：

[README.md:114-117](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/README.md#L114-L117) — 明确说明：已部署的集群要沿用当初部署时所用编译器版本对应的方法；新集群 `g++10` 或 `g++11` 任选其一，但**一旦部署就必须长期保持同一个选择**。

官方的完整构建命令（注意最后一行就是 `-DSHUFFLE_METHOD`）：

[README.md:106-112](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/README.md#L106-L112) — `cmake -S . -B build -DCMAKE_CXX_COMPILER=clang++-14 ... -DSHUFFLE_METHOD=<method>` 后接 `cmake --build build -j 32`。

#### 4.2.4 代码实践

> **实践目标**：亲手触发一次「未设置 SHUFFLE_METHOD」的错误，理解它的强制性。

1. 在一个干净目录尝试只配置、不传该参数（**示例命令**，不会改动源码）：
   ```bash
   cmake -S . -B build_test -DCMAKE_CXX_COMPILER=clang++-14 -DCMAKE_C_COMPILER=clang-14
   ```
2. **需要观察的现象**：CMake 输出里应出现 `[ERROR]: SHUFFLE_METHOD is not set!` 并中止。
3. **预期结果**：配置失败，`build_test/` 不会生成有效的构建文件。
4. 再补上 `-DSHUFFLE_METHOD=g++11` 重新配置，错误消失。
5. 如果当前环境缺编译器/依赖，命令可能在更早的阶段失败——这一点标注为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：一个已经用 `g++10` 部署运行的 3FS 集群，运维想升级编译器到 g++12 并重新编译，能直接换 `SHUFFLE_METHOD=g++11` 吗？

> **答案**：不能直接换。换洗牌算法会导致新旧二进制对同一输入算出不同的数据放置，造成数据错位。正确做法见 README：已部署集群必须沿用最初部署时的方法（此处应继续用 `g++10`），保持全集群一致；要切换方法通常涉及数据迁移或全量重建。

**练习 2**：`SHUFFLE_METHOD=stdshuffle` 为什么不被推荐用于生产？

> **答案**：`stdshuffle` 表示「用当前编译器自带的 `std::shuffle`」，其结果依赖编译器/标准库版本。一旦集群里节点编译器版本不一致，或日后升级编译器，洗牌结果就会变，破坏兼容性。显式选 `g++10`/`g++11` 才是把算法钉死成与版本无关的固定实现。

### 4.3 Rust crate：chunk_engine 与 Cargo workspace

#### 4.3.1 概念说明

3FS 不全是 C++：存储服务里最核心、最热的 **chunk engine**（负责物理块分配与 chunk 元数据管理）是用 **Rust** 写的。于是构建系统必须解决一个跨语言问题：**怎么让 CMake 编 C++ 的同时，也用 Cargo 编 Rust，再把 Rust 产物当成一个 C++ 静态库链接进来？**

3FS 的方案是：

- 用一个 **Cargo workspace** 统一管理几个 Rust crate。
- chunk engine 编译成 **静态库（`.a`）**，并用 [cxx](https://cxx.rs/) 生成跨语言桥接头文件。
- CMake 里的 `add_crate()` 宏把「跑 cargo」包装成一个 C++ 静态库目标，于是对 C++ 侧而言，chunk engine 就是一个普通链接库。

#### 4.3.2 核心流程

```text
cmake --build build
        │
        ├── 触发 add_crate(chunk_engine) 依赖的 cargo_build_all 目标
        │     └── 在 src/storage/chunk_engine/ 下执行 cargo build --release
        │           ├── 编出 target/release/libchunk_engine.a
        │           └── 经 cxxbridge 生成 target/cxxbridge/chunk_engine/src/{cxx.rs.h,cxx.rs.cc}
        │
        ├── 把上面两个产物注册成一个名为 chunk_engine 的 CMake「静态库」目标
        │
        └── storage_main 链接 chunk_engine → C++ 与 Rust 合体为同一个二进制
```

关键点：Rust 的产物路径是**仓库根的 `target/`**（因为 cargo 在 workspace 根运行），而不是 `build/`。

#### 4.3.3 源码精读

先看 Rust 侧的 workspace 总入口：

[Cargo.toml:1-11](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/Cargo.toml#L1-L11) — workspace 有 3 个成员：`src/client/trash_cleaner`、`src/storage/chunk_engine`、`src/lib/rs/hf3fs-usrbio-sys`；其中 `default-members` 只含前两个，意味着直接跑 `cargo build` 默认只编这两个。

workspace 统一了元信息与最低 Rust 版本：

[Cargo.toml:13-17](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/Cargo.toml#L13-L17) — `rust-version = "1.85.0"`（MSRV，最低支持版本），`edition = "2021"`。

还有一个专门给 CMake 用的 release profile：

[Cargo.toml:19-22](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/Cargo.toml#L19-L22) — `profile.release-cmake` 继承自 `release`，额外开 `debug = true`（保留符号）与 `lto = true`（链接期优化，跨 crate 内联，提升性能）。

chunk engine 自己的清单说明它要编成静态库并依赖 cxx：

[src/storage/chunk_engine/Cargo.toml:8-9](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/Cargo.toml#L8-L9) — `crate-type = ["lib", "staticlib"]`，`staticlib` 就是给 C++ 链接用的 `.a`。

[src/storage/chunk_engine/Cargo.toml:14-15](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/Cargo.toml#L14-L15) 与 [src/storage/chunk_engine/Cargo.toml:37-38](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/Cargo.toml#L37-L38) — 运行期依赖 `cxx = "1"`（FFI 桥接），构建期依赖 `cxx-build = "1"`（生成桥接头）。此外还依赖 `rocksdb`（RocksDB 的 Rust 绑定），对应 C++ 侧也编了 rocksdb。

CMake 这边如何「调用 cargo」是关键，全在 `AddCrate.cmake`：

[cmake/AddCrate.cmake:1-13](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/cmake/AddCrate.cmake#L1-L13) — 根据构建类型决定 `cargo build` 还是 `cargo build --release`，并定义一个 `cargo_build_all` 顶层自定义目标，在仓库根目录跑 cargo。

[cmake/AddCrate.cmake:15-33](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/cmake/AddCrate.cmake#L15-L33) — `add_crate(NAME)` 宏：声明产物是 `target/<debug|release>/lib<NAME>.a` 与 `target/cxxbridge/<NAME>/src/cxx.{rs.h,rs.cc}`；用 `add_custom_command` 跑 cargo 生成它们；再 `add_library(<NAME> STATIC ...)` 把它们包装成一个 C++ 静态库目标，链接 `pthread dl`，并把 `cxxbridge` 目录加入头文件搜索路径；最后 `add_dependencies(${NAME} cargo_build_all)` 保证 C++ 构建依赖 cargo 先完成。

最后，storage 服务把这个 Rust 库接进自己的链接链：

[src/storage/CMakeLists.txt:1-5](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/CMakeLists.txt#L1-L5) — 第 1 行 `add_crate(chunk_engine)`，第 3 行 `target_add_lib(storage ... chunk_engine ...)` 把它和其它 C++ 库一起编进 `storage` 静态库，于是 `storage_main` 天然就含 Rust 引擎。

#### 4.3.4 代码实践

> **实践目标**：理清「C++ 调用 Rust」的边界产物，不实际编译也能画出数据流。

1. 阅读 [cmake/AddCrate.cmake:15-33](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/cmake/AddCrate.cmake#L15-L33)，找出 Rust 侧产出的三类文件：静态库、C++ 头文件、C++ 桥接源文件。
2. 在仓库里搜索 `target/cxxbridge`、`libchunk_engine.a` 这两个字符串出现在哪些构建脚本中。
3. **需要观察的现象**：这些路径只在 `AddCrate.cmake` 中被引用，说明跨语言边界被很好地收口在一个宏里。
4. **预期结果**：你能画出 `cargo build` → `libchunk_engine.a` + `cxx.rs.h` → `add_library(chunk_engine STATIC)` → `storage_main` 的链路。
5. 若本地已装 Rust 工具链，可在 `src/storage/chunk_engine/` 下单独跑 `cargo build --release`（待本地验证），观察 `target/release/libchunk_engine.a` 是否生成。

#### 4.3.5 小练习与答案

**练习 1**：为什么 chunk engine 的 `crate-type` 要同时含 `lib` 和 `staticlib`？

> **答案**：`lib` 给 Rust 内部/测试/benchmark 用（普通 Rust crate 形式）；`staticlib` 才会产出可供 C++ 链接的 `libchunk_engine.a`。两者并存，让它既能被 Rust 自身使用，又能被 C++ 当作静态库链接。

**练习 2**：CMake 构建时，Rust 的产物为什么出现在仓库根的 `target/` 而不是 `build/`？

> **答案**：因为 `AddCrate.cmake` 让 cargo 在 workspace 根（`PROJECT_SOURCE_DIR`）运行，cargo 默认把产物写到工作区根的 `target/`。CMake 的 `build/` 只管 C++ 产物，两者目录相互独立，再由 `add_crate` 宏在配置期把 Rust 产物路径显式「指」给 C++ 目标。

### 4.4 依赖与子模块：third_party、系统包与 patches

#### 4.4.1 概念说明

一个像 3FS 这样的系统依赖大量第三方库，它们的来源分三类：

1. **git 子模块（third_party/）**：以源码形式随仓库分发，构建时一起编译。包括 folly（Facebook 的 C++ 基础库）、rocksdb、leveldb、fmt、zstd 等。
2. **系统包**：通过 `apt`/`yum` 安装的库，如 Boost、libuv、OpenSSL、libaio 等，构建时用 `find_package` / `find_library` 去系统路径找。
3. **外部安装的运行期依赖**：libfuse（FUSE 文件系统接口）、FoundationDB（事务型 KV）、Rust 工具链——它们有自己的版本要求，需单独装。

#### 4.4.2 核心流程

```text
首次获取代码：
  git clone ...
  git submodule update --init --recursive   # 拉取 third_party/ 下全部子模块
  ./patches/apply.sh                          # 给 rocksdb / folly 打补丁

构建期依赖解析：
  third_party/*  → add_subdirectory()       # 当作 CMake 子项目一起编
  Boost/libuv    → find_package/find_library
  libfuse/FDB    → 运行期动态链接（需系统预装）
```

#### 4.4.3 源码精读

`.gitmodules` 声明了 14 个子模块（节选）：

[.gitmodules:1-39](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/.gitmodules#L1-L39) — 列出 googletest、folly、leveldb、rocksdb、scnlib、pybind11、clickhouse-cpp、fmt、toml11、jemalloc、mimalloc、zstd、liburing、gtest-parallel 共 14 个子模块及其上游仓库。

顶层 CMake 把这些子模块逐个当作子项目编译（节选关键几行）：

[CMakeLists.txt:113-172](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/CMakeLists.txt#L113-L172) — 用一连串 `add_subdirectory("third_party/xxx" EXCLUDE_FROM_ALL)` 编译 fmt、zstd、googletest、folly、leveldb、rocksdb、scnlib、pybind11、toml11、mimalloc、clickhouse-cpp、liburing。`EXCLUDE_FROM_ALL` 表示除非被依赖，否则不默认编进 `all` 目标。每两个之间穿插 `store_compile_flags()/restore_compile_flags()`，是为了在编第三方库时**临时关闭** 3FS 自己严格的 `-Werror`（folly 等无法在 `-Werror` 下干净编译）。

注意 `jemalloc` 不是 `add_subdirectory`，而是由专门脚本处理：

[CMakeLists.txt:201](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/CMakeLists.txt#L201) — `include(cmake/Jemalloc.cmake)`，以不同方式接入 jemalloc 内存分配器（meta/storage 二进制显式链接它，见 4.1.3 表格后的说明）。

系统包则在配置期用 find 机制定位：

[CMakeLists.txt:188-193](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/CMakeLists.txt#L188-L193) — `find_package(Boost REQUIRED COMPONENTS filesystem system program_options)`、`find_library(LIBUV_LIBRARY NAMES libuv1)`；并固定 `FDB_VERSION 7.1.5-ibe`。Boost 用静态库（`Boost_USE_STATIC_LIBS ON`）。

获取代码后的「打补丁」步骤来自 README 与脚本：

[README.md:62-66](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/README.md#L62-L66) — 明确要求克隆后执行 `git submodule update --init --recursive` 再 `./patches/apply.sh`。

[patches/apply.sh:1-40](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/patches/apply.sh#L1-L40) — 用 `git apply` 给 `third_party/rocksdb` 打两个补丁、给 `third_party/folly` 打一个补丁；脚本用 `--reverse --check` 先判断是否已应用，做到幂等（重复执行不会出错）。这些补丁是 3FS 对上游库做的小修改，必须打上才能正常构建。

README 还列出系统依赖与外部依赖的版本要求：

[README.md:68-99](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/README.md#L68-L99) — 分别给出 Ubuntu 20.04 / 22.04、openEuler、OpenCloudOS/TencentOS 的 `apt`/`yum`/`dnf` 安装命令；并要求 libfuse ≥ 3.16.1、FoundationDB ≥ 7.1、Rust 最低 1.75、推荐 1.85。

#### 4.4.4 代码实践

> **实践目标**：把依赖「归类」，看清楚每个依赖属于哪一类来源。

1. 打开 [.gitmodules](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/.gitmodules)，数一数子模块总数，记下名字。
2. 在顶层 [CMakeLists.txt](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/CMakeLists.txt) 中搜索 `find_package(` 与 `find_library(`，列出靠系统包提供的依赖。
3. 对照 README 的外部依赖清单（libfuse / FDB / Rust），区分出哪些是「系统装、运行期用」的。
4. **需要观察的现象**：三类依赖的边界——子模块在 `third_party/`，系统包在 `find_*`，外部依赖在 README 单列。
5. **预期结果**：得到一张三列表格，例如 `folly=子模块`、`Boost=系统包`、`libfuse=外部安装`。
6. 阅读补丁脚本，验证它的幂等性设计（先 `--reverse --check` 再 apply）。

#### 4.4.5 小练习与答案

**练习 1**：为什么顶层 CMake 在编译第三方库时要先 `store_compile_flags()` 再 `restore_compile_flags()`？

> **答案**：3FS 对自身代码启用了 `-Wall -Wextra -Werror`（见顶层脚本第 174 行），但 folly、rocksdb 等上游库无法在这种严格告警下干净编译。`store/restore` 这对宏临时保存、再恢复编译选项，使第三方库用宽松选项编译、3FS 自己代码用严格选项编译，互不干扰。

**练习 2**：`./patches/apply.sh` 为什么要用 `git apply --reverse --check` 先做检查？

> **答案**：为了让脚本可重复执行（幂等）。`--reverse --check` 测试「补丁能否被反向应用」，若能，说明补丁已经应用过，就跳过；否则才真正 `git apply`。这样无论跑多少次都不会因「补丁已存在」而报错。

## 5. 综合实践

> **任务**：把本讲四个模块串起来，完成一次「纸面构建演练 + 产物清单盘点」。如果你有符合条件的机器，可以真正执行；否则做源码阅读型实践。

**步骤**：

1. **依赖准备**（对应 4.4）：按 [README.md:62-66](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/README.md#L62-L66) 执行子模块拉取与打补丁；确认 libfuse / FoundationDB / Rust 已装。
2. **配置**（对应 4.1、4.2）：执行官方命令，**务必带上** `-DSHUFFLE_METHOD`：
   ```bash
   cmake -S . -B build -DCMAKE_CXX_COMPILER=clang++-14 -DCMAKE_C_COMPILER=clang-14 \
         -DCMAKE_BUILD_TYPE=RelWithDebInfo -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
         -DSHUFFLE_METHOD=g++11
   ```
3. **构建**（对应 4.1、4.3）：`cmake --build build -j 32`。构建过程中会先触发 `cargo_build_all` 编 Rust chunk engine（产物在仓库根 `target/`），再编 C++。
4. **盘点产物**：构建成功后：
   - 列出 `build/bin/` 下的所有可执行文件，对照 4.1.3 的表格逐一确认（应含 `storage_main`、`meta_main`、`mgmtd_main`、`hf3fs_fuse_main`、`admin_cli` 等）。
   - 列出 `target/release/libchunk_engine.a` 与 `target/cxxbridge/chunk_engine/src/cxx.rs.h` 是否存在，验证 Rust→C++ 边界产物。
5. **反思**：写一段话回答——「如果我把这次构建的 `SHUFFLE_METHOD` 从 `g++11` 改成 `g++10` 再重新部署到一个已经在跑 `g++11` 的集群，会发生什么？」（参考 4.2 的答案）。

**预期结果**：你得到一份 `build/bin` 产物清单 + 一份 Rust 产物清单，并能解释 `SHUFFLE_METHOD` 的兼容性约束。若环境不全无法真正编译，请把第 1～3 步标注为「待本地验证」，并完成第 4～5 步的源码阅读型分析。

## 6. 本讲小结

- 3FS 主体是 **C++20**，用 **CMake** 构建；顶层 `CMakeLists.txt` 先校验环境与 `SHUFFLE_METHOD`、编第三方库，再经 `src/CMakeLists.txt` 进入 18 个一级模块。
- 所有最终二进制由 `cmake/Target.cmake` 的 `target_add_bin` 宏统一输出到 **`build/bin/`**（如 `storage_main`、`meta_main`、`mgmtd_main`、`hf3fs_fuse_main`）。
- **`SHUFFLE_METHOD` 是必填参数**，用于锁死 `std::shuffle` 的具体实现，避免不同编译器版本导致数据放置不一致；一旦集群部署就不能换。
- 存储核心 **chunk engine 用 Rust 编写**，经 Cargo workspace 编成静态库，再由 `cmake/AddCrate.cmake` 的 `add_crate` 宏用 cxx 桥接进 C++ 构建。
- 依赖分三类：**git 子模块**（`third_party/`，含 folly/rocksdb 等 14 个）、**系统包**（Boost/libuv 等，`find_package`）、**外部依赖**（libfuse/FoundationDB/Rust，需单独装）。
- 子模块拉取后必须跑 `./patches/apply.sh` 给 rocksdb/folly 打补丁，脚本设计为幂等。

## 7. 下一步学习建议

能看懂构建、（理想情况下）能编出二进制后，下一步建议：

- 下一讲 [u1-l3 部署一个测试集群与 admin_cli](u1-l3-deploy-and-admin-cli.md)：把本讲编出的二进制真正跑起来，组成一个最小集群。
- 想理解服务程序怎么启动，可先读 `src/simple_example/main.cpp`（教学模板）与 `src/core/app/ServerLauncher.h`，对应 [u2-l1 服务骨架](u2-l1-service-skeleton.md)。
- 想提前了解 Rust chunk engine 内部，可在编出 `libchunk_engine.a` 后直接进入 `src/storage/chunk_engine/src/` 阅读，对应 [u6 Chunk Engine 单元](u6-l1-chunk-engine-overview.md)。
