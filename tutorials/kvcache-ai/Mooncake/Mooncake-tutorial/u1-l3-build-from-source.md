# 从源码构建与安装 Mooncake

## 1. 本讲目标

学完本讲，你应该能够：

- 理解 Mooncake 从「拿到源码」到「装好可用」的完整三步流程：装依赖 → CMake 配置 → 编译安装。
- 读懂 `dependencies.sh` 脚本，知道它替你装了哪些系统包、哪些子项目、以及为什么需要 root 权限。
- 读懂顶层 `CMakeLists.txt` 与 `mooncake-common/common.cmake` 中的关键构建开关（`WITH_TE`、`WITH_STORE`、`WITH_EP`、`USE_ETCD`、`USE_CUDA` 等），理解每个开关会「打开或关闭哪一块代码」。
- 理解 `make install` 究竟把哪些产物（可执行文件、动态库、Python 扩展）放到了系统的哪个目录。
- 独立完成一次默认源码构建，并能通过对比 `.so` 文件列表验证不同开关的实际效果。

本讲是整个手册的「地基」：后面所有讲义（传输引擎、Store、EP）都假定你已经能把 Mooncake 编译出来。

## 2. 前置知识

在开始之前，用通俗的语言解释几个本讲会用到的概念。

**编译型项目与构建系统**
Mooncake 是一个 C++ 项目（夹杂 Go、Rust、Python 扩展）。C++ 源码不能直接运行，需要先用编译器（gcc/clang）编译成机器码，再由链接器拼装成可执行文件或库。手动管理「先编译哪个文件、链接哪些库」非常繁琐，所以项目用 **CMake** 这种「构建系统生成器」来描述依赖关系，再让 **make**（或 ninja）执行真正的编译命令。

**CMake 的两阶段工作方式**
CMake 分两个阶段工作：

1. **配置阶段（configure）**：执行 `cmake ..`。CMake 读取 `CMakeLists.txt`，根据各种 `option(...)` 开关决定「要不要编译某一块」，生成 `Makefile`。这一阶段会输出大量 `-- Found xxx` / `-- xxx support is enabled` 日志，是排查环境问题最重要的信息源。
2. **构建阶段（build）**：执行 `make -j`。make 按生成的 `Makefile` 调用编译器，产出二进制。

**静态库 vs 动态库（.a vs .so）**
- 静态库（`.a`）：编译时把库代码「复制」进最终产物，产物独立、体积大。
- 动态库（`.so`）：运行时才加载，多个程序共享一份，产物小但运行时要在 `LD_LIBRARY_PATH` 里找得到。
Mooncake 默认把 `transfer_engine`、`mooncake_store` 编成静态库（`BUILD_SHARED_LIBS=OFF`），而 Python 扩展模块和 `asio_shared` 一定是动态库。

**Python 扩展模块（pybind11）**
Mooncake 用 pybind11 把 C++ 类（如 `TransferEngine`、`MooncakeDistributedStore`）包装成 Python 可直接 `import` 的模块。编译产物是一个形如 `engine.cpython-311-x86_64-linux-gnu.so` 的文件，Python 把它当作一个 `.py` 一样 import。文件名里的 `cpython-311-...` 后缀由 Python 解释器版本决定，称为 `EXT_SUFFIX`。

**git submodule**
Mooncake 依赖一些第三方库（如 pybind11、yalantinglibs），它们以 git 子模块的形式放在 `extern/` 下。`git clone` 默认不下载子模块内容，需要 `git submodule update --init --recursive` 拉取——这正是 `dependencies.sh` 替你做的关键一步。

> 本讲承接 [u1-l2（项目整体结构）] 的内容，假定你已知 Mooncake 由 `mooncake-transfer-engine`、`mooncake-store`、`mooncake-ep`、`mooncake-integration` 等子目录组成。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [dependencies.sh](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/dependencies.sh) | 一键安装所有系统依赖、git 子模块、yalantinglibs、Go（以及可选的 SPDK）。是「环境准备」脚本。 |
| [CMakeLists.txt](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/CMakeLists.txt) | 顶层 CMake 入口。定义 `WITH_TE`/`WITH_STORE`/`WITH_EP`/`USE_NOF` 等开关，并决定要不要 `add_subdirectory` 进入各子项目。 |
| [mooncake-common/common.cmake](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-common/common.cmake) | 全局编译选项集合。定义 `USE_CUDA`/`USE_HIP`/`USE_ETCD`/`USE_REDIS`/`USE_HTTP` 等数十个开关与硬件 SDK 探测逻辑。 |
| [docs/source/getting_started/build.md](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/getting_started/build.md) | 官方构建指南文档。本讲的「自动构建」流程直接来源于此。 |
| [mooncake-store/src/CMakeLists.txt](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/CMakeLists.txt) | Store 的编译与安装目标定义（`mooncake_master`、`mooncake_client`、`mooncake_store` 库）。 |
| [mooncake-transfer-engine/src/CMakeLists.txt](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/CMakeLists.txt) | 传输引擎 `transfer_engine` 库的编译与链接定义。 |
| [mooncake-integration/CMakeLists.txt](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-integration/CMakeLists.txt) | 定义 Python 扩展 `engine`、`store` 两个 pybind11 模块及其 `install` 规则。 |

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：**构建文档与总流程**、**依赖脚本**、**CMake 构建选项**、**make install 与安装产物**。

### 4.1 构建文档与总流程

#### 4.1.1 概念说明

官方在 `docs/source/getting_started/build.md` 里给出了「最省事的从源码构建」路径，称为 **Automatic Build（自动构建）**。它把繁琐的环境准备压缩成一个脚本，把编译压缩成三条命令。本节先建立这条主线，后续三节再逐段深入它背后到底发生了什么。

#### 4.1.2 核心流程

自动构建的主线只有三步：

```text
1. bash dependencies.sh          # 装系统依赖 + 子模块 + yalantinglibs + Go
2. mkdir build && cd build
   cmake .. && make -j            # 配置 + 编译
3. sudo make install              # 安装到系统（bin / lib / Python 包目录）
```

`build.md` 还给出了推荐的环境基线：

- OS：Ubuntu 22.04 LTS+
- cmake：3.20.x
- gcc：9.4+

#### 4.1.3 源码精读

文档把这三步写得非常直白，见 [build.md:23-46](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/getting_started/build.md#L23-L46)，其中：

- 第 33 行 `bash dependencies.sh`：触发依赖脚本（见 4.2）。
- 第 38-41 行 `mkdir build / cd build / cmake .. / make -j`：标准 CMake「外部构建」（out-of-source build）。在 `build/` 目录里配置，源码目录保持干净。
- 第 45 行 `sudo make install`：把编译产物拷到系统目录（见 4.4）。`sudo` 是因为默认安装到 `/usr/local` 与系统 Python 包目录，需要写权限。

文档还单独给出了「带 NVMe-oF SSD Pool」的变体 [build.md:48-64](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/getting_started/build.md#L48-L64)：先 `bash dependencies.sh --with-spdk` 装 SPDK，再 `cmake .. -DUSE_NOF=ON`。它示范了「依赖脚本有额外开关、CMake 也有对应开关」这一贯穿全讲的模式。

> 文档的「Manual Build（手动构建）」[build.md:66-207](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/getting_started/build.md#L66-L207) 适合不能联网装包、或要为特殊硬件（CUDA / MUSA / MLU / Ascend …）定制环境的人。本讲聚焦自动构建，手动构建可留作进阶阅读。

#### 4.1.4 代码实践

**实践目标**：把官方三步流程在干净环境里跑通一次，建立「成功构建长什么样」的直觉。

**操作步骤**（建议在一个全新的 Ubuntu 22.04 虚拟机或 Docker 容器里做，保证环境干净）：

1. 克隆仓库并进入：
   ```bash
   git clone https://github.com/kvcache-ai/Mooncake.git
   cd Mooncake
   ```
2. 装依赖（需要 root，全程联网）：
   ```bash
   sudo bash dependencies.sh
   ```
3. 配置并编译：
   ```bash
   mkdir build && cd build
   cmake ..
   make -j$(nproc)
   ```
4. 安装：
   ```bash
   sudo make install
   ```

**需要观察的现象**：

- 第 2 步会打印大量 `=== ... ===` 彩色分段标题，最后以 `All dependencies have been successfully installed!` 结尾。
- 第 3 步 `cmake ..` 会打印类似 `-- Mooncake Store will be built`、`-- Http as metadata server support is enabled` 的状态行；`make` 会持续输出编译进度。
- 第 4 步结束后，能在终端键入 `mooncake_master --help` 看到帮助输出，且 `python3 -c "import mooncake; print(mooncake.__file__)"` 能打印出包路径。

**预期结果**：`build/` 目录下出现可执行文件与 `.so`；系统里出现 `mooncake_master` 命令与 `mooncake` Python 包。如果某一步失败，**先看 `cmake ..` 的状态行**——绝大多数环境问题（缺包、SDK 路径错）都会在这里暴露。

> 待本地验证：实际编译耗时与是否启用 GPU 强相关。CPU-only 默认构建在 8 核机器上通常需要 10～30 分钟；首次 `dependencies.sh` 因要下载并编译 yalantinglibs、Go，还会更久。

#### 4.1.5 小练习与答案

**练习 1**：为什么官方推荐「外部构建」（`mkdir build && cd build`）而不是直接在仓库根目录 `cmake .`？

**参考答案**：外部构建把所有生成物（`Makefile`、`.o`、`.so`）隔离在 `build/` 里，源码树保持干净，方便用 `git status` 查看真实改动，也便于 `rm -rf build` 完全重来。

**练习 2**：`make` 与 `make install` 的职责有什么区别？

**参考答案**：`make` 只在 `build/` 目录里产出二进制（不碰系统）；`make install` 才把这些产物按 CMake 里写的 `install(...)` 规则拷贝到系统目录（`/usr/local/bin`、`/usr/local/lib`、Python 包目录），所以后者需要 `sudo`。

---

### 4.2 依赖脚本 dependencies.sh

#### 4.2.1 概念说明

`dependencies.sh` 是一个 bash 脚本，目标是「让用户只敲一条命令就把构建所需的一切准备好」。它做了四件大事：

1. 装系统级软件包（编译器、CMake、RDMA、jsoncpp、asio 等几十个 `-dev` 包）。
2. 初始化 git 子模块（pybind11、yalantinglibs 等）。
3. 从源码编译并安装 yalantinglibs（一个阿里开源的 C++ 基础库，CMake 里用 `find_package(yalantinglibs)` 找它）。
4. 安装 Go 工具链（Store 的 etcd/K8s 高可用后端是 Go 写的，需要 Go 编译成 `.so`）。

可选地，用 `--with-spdk` 还会编译 SPDK（用于 NVMe-oF SSD Pool）。

#### 4.2.2 核心流程

脚本的执行顺序大致如下（伪代码）：

```text
要求 root 权限，否则报错退出
解析命令行参数: -y/--yes  --with-spdk  -h/--help
打印将安装的组件清单 → 等待用户确认（除非 -y）
detect_os()              # 读 /etc/os-release 判断发行版
更新包列表 (apt-get update / yum makecache)
按发行版安装 SYSTEM_PACKAGES
初始化 git 子模块 (submodule sync + update --init --recursive)
编译并安装 yalantinglibs
安装 Go 1.25.9
(可选) 克隆并编译 SPDK v23.01.1
打印安装完成摘要
```

#### 4.2.3 源码精读

**(1) 强制 root**
脚本一上来就检查权限：[dependencies.sh:81-83](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/dependencies.sh#L81-L83)。因为 `apt-get install` / `yum install` 必须 root，yalantinglibs 也要 `cmake --install` 到 `/usr/local`。

**(2) 命令行参数**
脚本支持三个开关：[dependencies.sh:88-106](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/dependencies.sh#L88-L106)

- `-y` / `--yes`：跳过确认，适合在 CI / Docker 里无人值守运行。
- `--with-spdk`：额外编译 SPDK。
- `-h` / `--help`：打印用法。

**(3) 安装系统包（Ubuntu 为例）**
这是脚本最长、也最关键的一段：[dependencies.sh:151-182](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/dependencies.sh#L151-L182)。每一个包都对应 Mooncake 编译时的一处依赖，例如：

| 包名 | 对应用途 |
| --- | --- |
| `build-essential` / `cmake` / `ninja-build` | 编译器与构建工具本身 |
| `libibverbs-dev` | RDMA（InfiniBand/RoCE）动词层，传输引擎核心 |
| `libgoogle-glog-dev` | 日志库 glog |
| `libjsoncpp-dev` | JSON 解析 |
| `libboost-all-dev` | cachelib 内存分配器依赖 |
| `libgrpc-dev` / `libprotobuf-dev` | gRPC/protobuf（部分元数据通信） |
| `libhiredis-dev` | Redis 客户端（`USE_REDIS`/`STORE_USE_REDIS`） |
| `liburing-dev` | io_uring（Store 异步文件 I/O，自动探测） |
| `libasio-dev` | asio 异步 I/O（`asio_shared` 动态库） |
| `libxxhash-dev` | xxHash（Store 校验和） |
| `patchelf` | 调整 `.so` 的 RPATH（wheel 打包用） |

> 注意：CentOS/RHEL 系走 `yum` 分支 [dependencies.sh:187-216](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/dependencies.sh#L187-L216)，包名换成 `-devel` 后缀（如 `rdma-core-devel`、`gtest-devel`）。

**(4) 初始化 git 子模块**
[dependencies.sh:227-242](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/dependencies.sh#L227-L242)：先检查 `.gitmodules` 是否存在，再执行 `git submodule sync --recursive` 与 `git submodule update --init --recursive`。没有这一步，`extern/pybind11`、`extern/yalantinglibs` 都是空目录，CMake 配置阶段会直接失败。

**(5) 编译安装 yalantinglibs**
yalantinglibs 没有发行版包，只能从子模块源码编译：[dependencies.sh:245-267](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/dependencies.sh#L245-L267)。关键三行是：

```bash
cmake .. -DBUILD_EXAMPLES=OFF -DBUILD_BENCHMARK=OFF -DBUILD_UNIT_TESTS=OFF   # 配置，关掉它的示例/测试
cmake --build . -j$(nproc)                                                    # 编译
cmake --install .                                                             # 装到 /usr/local
```

这一步成功后，顶层 CMake 里的 `find_package(yalantinglibs CONFIG REQUIRED)`（[common.cmake:432](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-common/common.cmake#L432)）才能找到它。

**(6) 安装 Go**
固定版本 `GOVER=1.25.9`（[dependencies.sh:26](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/dependencies.sh#L26)），见 [dependencies.sh:283-347](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/dependencies.sh#L283-L347)。脚本会：先探测架构（x86_64→amd64，aarch64→arm64）；依次尝试 `go.dev`、`golang.google.cn`、阿里云三个镜像下载；解压到 `/usr/local/go`；并把 `/usr/local/go/bin` 写进 `~/.bashrc`。如果检测到受限网络（走了国内镜像），还会自动设置 `GOPROXY`。这一步只为后续 `USE_ETCD` / `STORE_USE_ETCD` / `STORE_USE_K8S_LEASE` 时用 Go 编译 c-shared 库做准备（见 4.3）。

#### 4.2.4 代码实践

**实践目标**：在不实际改动系统的情况下，通读脚本并验证它对发行版的识别逻辑。

**操作步骤**：

1. 阅读脚本帮助，确认你了解它能做什么：
   ```bash
   bash dependencies.sh --help
   ```
2. 阅读脚本里 `detect_os()` 函数 [dependencies.sh:66-79](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/dependencies.sh#L66-L79)，理解它如何用 `/etc/os-release` 判断发行版。
3. 在你自己的机器上**只跑识别逻辑**，不真正装包。可以用 `OS_RELEASE_FILE` 环境变量指向你自己的 `os-release` 文件来模拟不同发行版（脚本把它做成可配置：[dependencies.sh:27](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/dependencies.sh#L27)）。

**需要观察的现象**：

- `--help` 输出三个选项的说明。
- 改 `OS_RELEASE_FILE` 指向一份 `ID=centos` 的文件后，脚本后续会走 `yum` 分支而非 `apt` 分支。

**预期结果**：能口头复述脚本在 Ubuntu 与 CentOS 上分别用哪个包管理器、装哪些关键包。待本地验证：若你的环境无 root，可用 `docker run -it --rm ubuntu:22.04 bash` 之类容器来安全尝试。

#### 4.2.5 小练习与答案

**练习 1**：为什么脚本要求 root，却不提供「免 root 模式」？

**参考答案**：它要用 `apt`/`yum` 装系统包、把 yalantinglibs `cmake --install` 到 `/usr/local`，这些都需要写系统目录的权限。要做免 root 构建，需要手动把依赖装到用户目录并设置 `CMAKE_PREFIX_PATH`，这正是「Manual Build」章节覆盖的场景。

**练习 2**：如果克隆仓库时忘了加 `--recursive`，`dependencies.sh` 的哪一段会补救？

**参考答案**：[dependencies.sh:233-236](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/dependencies.sh#L233-L236) 的 `git submodule sync --recursive` + `git submodule update --init --recursive`，会自动把子模块内容拉全。

---

### 4.3 CMake 构建选项

#### 4.3.1 概念说明

CMake 用 `option(NAME "描述" 默认值)` 定义**构建开关**。开关的本质是一个布尔型 CMake 变量，在配置阶段用 `-D开关=ON/OFF` 传入。CMakeLists.txt 里随后用 `if(开关) ... endif()` 决定：

- 要不要 `add_subdirectory(某子目录)`（即要不要编译那一整块）。
- 要不要 `add_compile_definitions(宏)`（即编译器是否看到某段 `#ifdef 宏` 包起来的代码）。
- 要不要链接额外的库（如开 `USE_CUDA` 就链 `cudart`）。

Mooncake 的开关分散在两处：**顶层 `CMakeLists.txt`** 控制「编译哪几个子项目」；**`mooncake-common/common.cmake`** 控制「为哪种硬件/元数据后端编译」。两处都会被 CMake 配置阶段读到。

#### 4.3.2 核心流程

配置阶段，CMake 大致按下面顺序处理开关：

```text
读取顶层 CMakeLists.txt
  → include(common.cmake)            # 注册硬件/元数据类 option
  → 顶层 option(WITH_TE/WITH_STORE/WITH_EP/USE_NOF ...)
  → if(USE_ETCD) add_compile_definitions(USE_ETCD) ...   # 注入编译宏
  → if(WITH_TE)   add_subdirectory(mooncake-transfer-engine)
  → if(WITH_STORE) add_subdirectory(mooncake-store)
  → add_subdirectory(mooncake-integration)   # Python 扩展总在这里
```

关键直觉：**`WITH_*` 决定「建不建这个子项目」，`USE_*` 决定「子项目里要不要某项能力」**。

#### 4.3.3 源码精读

**(1) 顶层四大组件开关**
[CMakeLists.txt:15-22](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/CMakeLists.txt#L15-L22) 定义了最常碰的开关：

| 开关 | 默认 | 含义 |
| --- | --- | --- |
| `WITH_TE` | ON | 编译传输引擎及示例 |
| `WITH_STORE` | ON | 编译 Store 库及示例（含 `mooncake_master`/`mooncake_client`） |
| `WITH_STORE_RUST` | ON | 编译 Store 的 Rust 绑定（**依赖 `WITH_STORE=ON`**） |
| `WITH_EP` | OFF | 编译 EP（专家并行）+ PG 的 Python 扩展，需 CUDA + PyTorch |
| `WITH_STORE_GO` / `WITH_P2P_STORE` / `WITH_RUST_EXAMPLE` | OFF | Go 绑定 / P2P Store / TE 的 Rust 示例 |
| `USE_NOF` | OFF | 编译 NVMe-oF SSD Pool 支持（需 SPDK） |

这些开关真正生效的地方是 `add_subdirectory`：[CMakeLists.txt:75-92](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/CMakeLists.txt#L75-L92)。例如 `WITH_STORE=OFF` 时根本不会进入 `mooncake-store/` 目录，自然也就不会产出 `mooncake_master` 和 `store` Python 模块。开关之间还有依赖校验，例如 `WITH_STORE_RUST=ON` 时若 `WITH_STORE=OFF` 会直接 `FATAL_ERROR`（[CMakeLists.txt:87-89](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/CMakeLists.txt#L87-L89)）。

**(2) etcd / Redis 元数据后端开关**
`USE_ETCD` 与 `STORE_USE_ETCD` 是**两个相互独立的开关**，初学者极易混淆：

- `USE_ETCD`（[common.cmake:118](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-common/common.cmake#L118)）：给**传输引擎**做元数据服务器。开启后顶层会 `add_compile_definitions(USE_ETCD)`（[CMakeLists.txt:32-40](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/CMakeLists.txt#L32-L40)），并用 Go 编译 `libetcd_wrapper.so`（见 4.4）。
- `STORE_USE_ETCD`（[CMakeLists.txt:41-44](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/CMakeLists.txt#L41-L44)）：给 **Store** 做主备高可用故障切换。开它不需要开 `USE_ETCD`。

类似地，`USE_REDIS`（传输引擎元数据）与 `STORE_USE_REDIS`（Store 故障切换）也是分开的（[common.cmake:120](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-common/common.cmake#L120) 与 [CMakeLists.txt:45-48](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/CMakeLists.txt#L45-L48)）。CMake 还设置了互斥校验：`STORE_USE_K8S_LEASE` 与 `STORE_USE_ETCD` 不能同时开（[CMakeLists.txt:49-58](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/CMakeLists.txt#L49-L58)），因为两者都要用 Go 编 c-shared 库、会冲突。

**(3) 硬件加速开关（common.cmake）**
`common.cmake` 集中定义了针对不同加速硬件的开关，全部默认 OFF：[common.cmake:71-89](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-common/common.cmake#L71-L89)。例如：

```cmake
option(USE_CUDA "option for enabling gpu features for NVIDIA GPU" OFF)
option(USE_HIP  "option for enabling gpu features for AMD GPU" OFF)
option(USE_MLU  "option for enabling Cambricon MLU features" OFF)
option(USE_MUSA "option for enabling gpu features for MTHREADS GPU" OFF)
```

开启某硬件后，CMake 会做两件事：注入编译宏 + 链接对应运行时。以 CUDA 为例：[common.cmake:164-172](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-common/common.cmake#L164-L172) 里 `add_compile_definitions(USE_CUDA)` 让源码里 `#ifdef USE_CUDA` 的 GPUDirect RDMA 分支被编译，并 `link_directories(/usr/local/cuda/lib64)` 让链接器找到 `libcudart`。文档 [build.md:256](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/getting_started/build.md#L256) 特别提醒：**即使走 TCP 协议、只要传 GPU 显存（如 vLLM 分离式推理的 KV cache），也必须开 `USE_CUDA`**。

> 一个隐藏行为：`USE_NVMEOF` 和 `USE_MNNVL` 一旦开启会**自动连带打开 `USE_CUDA`**（[common.cmake:150-162](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-common/common.cmake#L150-L162)），因为它们本质依赖 CUDA 运行时。

**(4) 构建行为类开关**
还有一类开关不改变「编什么功能」，而改变「怎么编」：

- `BUILD_SHARED_LIBS`（CMake 内置变量，默认 OFF）：把 `transfer_engine`/`mooncake_store` 编成动态库而非静态库（文档 [build.md:290](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/getting_started/build.md#L290)）。
- `BUILD_UNIT_TESTS`（默认 ON）/[`BUILD_EXAMPLES`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-common/common.cmake#L67-L69)（默认 ON）：是否编译单元测试与示例程序。只想快速出库时关掉它们能显著缩短编译时间。
- `ENABLE_ASAN`（[common.cmake:30-37](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-common/common.cmake#L30-L37)）：开启 AddressSanitizer，用于排查内存错误。

文档在 [build.md:254-294](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/getting_started/build.md#L254-L294) 给出了所有「高级编译选项」的权威清单，遇到拿不准的开关应回到这里查证。

#### 4.3.4 代码实践

**实践目标**：体会「开关直接改变 CMake 配置输出」，学会从配置日志判断开关是否生效。

**操作步骤**：

1. 在仓库根目录准备一个干净的构建目录：
   ```bash
   mkdir build && cd build
   ```
2. 故意关掉单元测试与示例，开启 CUDA（如有 CUDA 环境）：
   ```bash
   cmake .. -DBUILD_UNIT_TESTS=OFF -DBUILD_EXAMPLES=OFF -DUSE_CUDA=ON
   ```
3. 仔细阅读 `cmake ..` 打印的状态行。

**需要观察的现象**：

- 当 `USE_CUDA=ON` 时，日志里应出现 `-- CUDA support is enabled`（来自 [common.cmake:166](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-common/common.cmake#L166)）。
- `WITH_STORE=ON`（默认）时，日志里有 `-- Mooncake Store will be built`（[CMakeLists.txt:81](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/CMakeLists.txt#L81)）。

**预期结果**：你能把日志里的每一行 `-- xxx enabled` 都对应回某个 `-D` 开关。这是日后排查「为什么某个功能没编进去」的核心技能。若本机无 CUDA，把 `-DUSE_CUDA=ON` 去掉即可，本实践的目的在于「读日志」而非必须开 CUDA。

#### 4.3.5 小练习与答案

**练习 1**：`USE_ETCD` 和 `STORE_USE_ETCD` 有什么区别？开 `STORE_USE_ETCD` 一定要先开 `USE_ETCD` 吗？

**参考答案**：`USE_ETCD` 服务于传输引擎的元数据服务器；`STORE_USE_ETCD` 服务于 Store 的主备高可用。二者独立，文档 [build.md:288](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/getting_started/build.md#L288) 明确写「Enabling `-DSTORE_USE_ETCD` does **not** depend on `-DUSE_ETCD`」。但它们都依赖 Go 工具链（由 `dependencies.sh` 安装）。

**练习 2**：为什么 `STORE_USE_K8S_LEASE` 和 `STORE_USE_ETCD` 不能同时开启？

**参考答案**：两者都要用 Go 的 `-buildmode=c-shared` 在同一进程里编译出 c-shared 库，会冲突，故 CMake 在 [CMakeLists.txt:51-56](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/CMakeLists.txt#L51-L56) 直接 `FATAL_ERROR` 阻止。

**练习 3**：只想编译 Store 的库、不要传输引擎，应该怎么配置？

**参考答案**：`cmake .. -DWITH_TE=OFF -DWITH_STORE=ON`。注意此时 `WITH_STORE_RUST` 仍为 ON 但其依赖 `WITH_STORE=ON` 已满足；若你也不需要 Rust 绑定，可再加 `-DWITH_STORE_RUST=OFF`。

---

### 4.4 make install 与安装产物

#### 4.4.1 概念说明

`make install` 按 CMake 里写的 `install(TARGETS ... DESTINATION ...)` 规则，把编译产物拷到系统目录。Mooncake 默认安装前缀是 `/usr/local`，所以：

- 可执行文件 → `/usr/local/bin`
- 动态库 → `/usr/local/lib`
- Python 扩展与脚本 → 系统 Python 的 `site-packages/mooncake/`

理解「哪些产物会装、装到哪」的关键，是去各 `CMakeLists.txt` 里找 `install(...)` 语句。注意：很多 `install(TARGETS ... lib)` 被 `if(BUILD_SHARED_LIBS)` 包住——默认静态库模式下**不会**把 `.a` 装到 `lib`，因为静态库已经被链进最终产物了。

#### 4.4.2 核心流程

`make install` 触发后，CMake 按下列目标逐个拷贝：

```text
mooncake_master, mooncake_client   → bin/                       （WITH_STORE 时）
engine.<EXT_SUFFIX>                → site-packages/mooncake/    （WITH_TE 时）
store.<EXT_SUFFIX>                 → site-packages/mooncake/    （WITH_STORE 时）
libasio.so (asio_shared)           → lib/ 和 site-packages/mooncake/
transfer_engine / mooncake_store   → lib/                       （仅 BUILD_SHARED_LIBS 时）
libetcd_wrapper.so                 → lib/                       （USE_ETCD 时）
若干 *.py 脚本                     → site-packages/mooncake/
```

其中 `EXT_SUFFIX` 形如 `.cpython-311-x86_64-linux-gnu.so`，由 [mooncake-integration/CMakeLists.txt:34-37](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-integration/CMakeLists.txt#L34-L37) 用 `sysconfig.get_config_var('EXT_SUFFIX')` 从 Python 取得。

#### 4.4.3 源码精读

**(1) 可执行文件：mooncake_master / mooncake_client**
[mooncake-store/src/CMakeLists.txt:357](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/CMakeLists.txt#L357) 把这两个二进制装到 `bin`。它们只在 `WITH_STORE=ON` 时才会被编译（因为整个 `mooncake-store` 子目录由 `WITH_STORE` 把守）。`mooncake_master` 是 Store 的中心管理进程，是运行 Mooncake Store 时第一个要启动的服务。

**(2) Python 扩展：engine / store**
这两个是 pybind11 模块：

- `engine`：[mooncake-integration/CMakeLists.txt:46-48](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-integration/CMakeLists.txt#L46-L48)，由 `WITH_TE` 把守，包装传输引擎。
- `store`：[mooncake-integration/CMakeLists.txt:104-107](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-integration/CMakeLists.txt#L104-L107)，由 `WITH_STORE` 把守，包装 Store。

安装规则在 [mooncake-integration/CMakeLists.txt:190-198](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-integration/CMakeLists.txt#L190-L198)：`store` 进 `mooncake/` 包、`engine` 和 `asio_shared` 进 `mooncake/` 包。两者都设了 `INSTALL_RPATH "$ORIGIN"`，意思是「在自身所在目录找依赖的 `.so`」，所以 `engine.so` 能从同目录加载 `libasio.so`。

**(3) 动态库：asio_shared / transfer_engine / mooncake_store**
`asio_shared` 是少数**无条件**安装到 `lib` 的库：[mooncake-common/src/CMakeLists.txt:71](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-common/src/CMakeLists.txt#L71)，其产物名为 `libasio.so`（`OUTPUT_NAME "asio"`，[同文件:43-49](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-common/src/CMakeLists.txt#L43-L49)）。而 `transfer_engine`（[mooncake-transfer-engine/src/CMakeLists.txt:14-16](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/CMakeLists.txt#L14-L16)）与 `mooncake_store`（[mooncake-store/src/CMakeLists.txt:266-268](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/CMakeLists.txt#L266-L268)）只在 `BUILD_SHARED_LIBS=ON` 时才装到 `lib`——默认情况下它们是静态库，已被链进 `engine`/`store`/`mooncake_master`，无需单独安装。

**(4) etcd 的 Go 动态库**
当 `USE_ETCD=ON`（非 legacy）时，会触发一段 Go 命令把 `etcd_wrapper.go` 编成 `libetcd_wrapper.so`：[mooncake-common/etcd/CMakeLists.txt:1-20](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-common/etcd/CMakeLists.txt#L1-L20)。关键命令是 `go build -buildmode=c-shared -o libetcd_wrapper.so etcd_wrapper.go`，它同时产出一个 `.so`（给 C++ 链接）和一个 `.h`（给 C++ include）。这个 `.so` 会被装到 `lib`。**这也解释了为什么 `USE_ETCD` 必须先装好 Go**（4.2 的第 4 件事）。

#### 4.4.4 代码实践

**实践目标**：动手验证「不同开关下安装产物列表确实不同」。

**操作步骤**：

1. 做一次默认构建并安装，然后列出系统里新增的 Mooncake 产物：
   ```bash
   cd build
   cmake .. && make -j$(nproc) && sudo make install
   ls /usr/local/bin/mooncake_*                      # 应看到 mooncake_master, mooncake_client
   ls $(python3 -c "import sys; print([s for s in sys.path if 'packages' in s][0])")/mooncake/*.so
   ```
2. 清理后做一次「关掉 Store」的构建，重新安装，再列一遍：
   ```bash
   rm -rf build && mkdir build && cd build
   cmake .. -DWITH_STORE=OFF -DWITH_STORE_RUST=OFF && make -j$(nproc) && sudo make install
   ls /usr/local/bin/mooncake_*                      # 预期：没有 mooncake_master/mooncake_client
   ```
3. （可选，需 Go）做一次「开 etcd」的构建，对比 `lib` 目录：
   ```bash
   rm -rf build && mkdir build && cd build
   cmake .. -DUSE_ETCD=ON && make -j$(nproc) && sudo make install
   ls /usr/local/lib/libetcd_wrapper.so              # 预期：出现该文件
   ```

**需要观察的现象**：

| 构建 | `/usr/local/bin` | `site-packages/mooncake/*.so` | `/usr/local/lib` |
| --- | --- | --- | --- |
| 默认（WITH_STORE+WITH_TE） | `mooncake_master`, `mooncake_client` | `engine.*.so`, `store.*.so`, `libasio.so` | `libasio.so` |
| `-DWITH_STORE=OFF` | **无** mooncake 二进制 | 只有 `engine.*.so`、`libasio.so`，**无** `store.*.so` | `libasio.so` |
| `-DUSE_ETCD=ON` | 同默认 | 同默认 | 额外多出 `libetcd_wrapper.so` |

**预期结果**：你能亲眼看到「关 `WITH_STORE` 让 `store.*.so` 和两个二进制消失」「开 `USE_ETCD` 让 `libetcd_wrapper.so` 出现」。如果观察到的不一致，回到对应子目录的 `CMakeLists.txt` 检查 `install()` 规则。待本地验证：`*.so` 的确切文件名（含 `cpython-3xx` 后缀）取决于你的 Python 版本。

#### 4.4.5 小练习与答案

**练习 1**：默认构建后，`/usr/local/lib` 里**没有** `libtransfer_engine.so`，这是不是出了问题？

**参考答案**：不是。默认 `BUILD_SHARED_LIBS=OFF`，`transfer_engine` 是静态库，已链进 `engine.so`/`mooncake_master` 等产物，故不单独安装（见 [mooncake-transfer-engine/src/CMakeLists.txt:14-16](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/CMakeLists.txt#L14-L16) 的 `if(BUILD_SHARED_LIBS)`）。想要 `.so`，加 `-DBUILD_SHARED_LIBS=ON`。

**练习 2**：为什么 `engine.so` 和 `store.so` 都设置 `INSTALL_RPATH "$ORIGIN"`？

**参考答案**：`$ORIGIN` 告诉动态链接器「在被加载的 `.so` 自身所在目录里找依赖」。这样 `engine.so` 就能从同目录的 `mooncake/` 包里找到 `libasio.so`，无需用户手动设置 `LD_LIBRARY_PATH`（见 [mooncake-integration/CMakeLists.txt:49](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-integration/CMakeLists.txt#L49)）。

**练习 3**：`mooncake_master` 这个可执行文件由哪些源码链接而成？它依赖 Store 还是传输引擎？

**参考答案**：见 [mooncake-store/src/CMakeLists.txt:271-293](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/CMakeLists.txt#L271-L293)：`mooncake_master` 由 `master.cpp` 链接，依赖 `mooncake_store`、`mooncake_common`、`asio_shared` 等；注意 `transfer_engine` 是 `PRIVATE` 链给 `mooncake_store` 而非直接给 `mooncake_master`（注释 [同文件:246-248](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/CMakeLists.txt#L246-L248) 解释了为什么）。

---

## 5. 综合实践

**任务**：在一个干净环境（推荐 Docker 容器或虚拟机）中，完成两次构建并对比编译产物的 `.so` 列表，直观印证「CMake 开关 = 产物差异」。

**步骤**：

1. 准备环境（容器示例）：
   ```bash
   docker run -it --cap-add=SYS_PTRACE --security-opt seccomp=unconfined ubuntu:22.04 bash
   # 容器内：
   apt-get update && apt-get install -y git sudo
   git clone https://github.com/kvcache-ai/Mooncake.git && cd Mooncake
   ```
2. 跑依赖脚本（无人值守）：
   ```bash
   bash dependencies.sh -y
   source ~/.bashrc          # 让 Go 进 PATH
   ```
3. **构建 A（默认）**：
   ```bash
   mkdir buildA && cd buildA && cmake .. && make -j$(nproc)
   find . -name '*.so' | sort > /tmp/soA.txt
   find . -type f -executable -not -name '*.sh' | sort > /tmp/exeA.txt
   cd ..
   ```
4. **构建 B（关 Store + 关 TE 的 Rust，开 etcd）**：
   ```bash
   mkdir buildB && cd buildB && cmake .. -DWITH_STORE=OFF -DWITH_STORE_RUST=OFF -DUSE_ETCD=ON && make -j$(nproc)
   find . -name '*.so' | sort > /tmp/soB.txt
   cd ..
   ```
5. 对比两次的 `.so` 列表：
   ```bash
   diff /tmp/soA.txt /tmp/soB.txt
   ```

**需要观察的现象与预期结果**：

- 相对于构建 A，构建 B 应**缺少** `store.cpython-*.so`（因为 `WITH_STORE=OFF`），但**多出** `libetcd_wrapper.so`（因为 `USE_ETCD=ON`，由 Go 编译）。
- 构建 B 也不会有 `mooncake_master`、`mooncake_client` 两个可执行文件。
- 把 diff 的每一行差异，对应回本讲 4.3/4.4 里引用的某条 `install()` 或 `add_subdirectory` 规则——能完整解释清楚，说明你已真正掌握构建系统。

> 待本地验证：实际 `.so` 文件名中的 Python 标签取决于容器内默认 Python 版本；若网络受限，`dependencies.sh` 会自动切换到国内镜像，耗时与默认路径不同。若 `USE_ETCD=ON` 报 Go 相关错误，确认已 `source ~/.bashrc` 让 `go` 可用。

## 6. 本讲小结

- 从源码构建 Mooncake 的主线只有三步：`bash dependencies.sh` → `cmake .. && make -j` → `sudo make install`，定义在 [build.md](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/getting_started/build.md)。
- `dependencies.sh` 统一负责系统包、git 子模块、yalantinglibs 源码编译与 Go 安装，需 root；`-y` 适合 CI、`--with-spdk` 用于 NVMe-oF。
- CMake 开关分两类：顶层 `WITH_TE`/`WITH_STORE`/`WITH_EP`/`USE_NOF` 控制「建哪个子项目」；`common.cmake` 里 `USE_CUDA`/`USE_ETCD`/`USE_REDIS` 等控制「子项目里启用哪项能力」。
- `USE_ETCD` 与 `STORE_USE_ETCD`（以及 `USE_REDIS` 与 `STORE_USE_REDIS`）是两对**相互独立**的开关，分属传输引擎与 Store。
- `make install` 把 `mooncake_master`/`mooncake_client` 装到 `bin`、把 `engine`/`store`/`asio_shared` 装进 Python `mooncake` 包；`transfer_engine`/`mooncake_store` 仅在 `BUILD_SHARED_LIBS=ON` 时才装到 `lib`。
- 排查任何构建问题的第一动作是**细读 `cmake ..` 的状态行**，那里直接告诉你「哪些功能被启用、哪些依赖没找到」。

## 7. 下一步学习建议

- 掌握构建之后，下一讲（依赖 `u1-l2` 的后续章节）建议进入**传输引擎**：先读 `mooncake-transfer-engine/` 的目录结构与示例 `transfer_engine_bench`，尝试运行一次端到端传输。
- 若你的重点是 **Store**，建议接着学习 `mooncake_master` 的启动参数与 Store 的 Python API（`mooncake.store.MooncakeDistributedStore`）。
- 进阶可阅读 `scripts/build_wheel.sh`（wheel 打包脚本），理解 `patchelf` 调整 RPATH、`audittool` 处理 CUDA fatbin 的过程——它是本讲「安装产物」逻辑在发布场景下的延伸。
- 想为特殊硬件构建时，回到 [build.md 的 Advanced Compile Options](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/getting_started/build.md#L254-L294) 查阅对应 SDK 的环境变量与 `-D` 开关。
