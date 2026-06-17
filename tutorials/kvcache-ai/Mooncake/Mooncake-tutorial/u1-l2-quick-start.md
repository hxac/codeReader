# U1-L2 环境搭建与快速开始

> 本讲义指导读者从零开始搭建 Mooncake 运行环境（依赖 RDMA 驱动、CUDA、Python），通过 Python 包安装或源码编译的方式部署 Transfer Engine 与 Mooncake Store，运行 Hello World 示例验证环境正确性，为后续深入学习做好准备。

[⬅️ U1-L1 项目概览](u1-l1-project-overview.md) | [U2-L1 Transfer Engine 设计 ➡️](../unit-2/u2-l1-transfer-engine-design.md)

---

## 最小模块 1：依赖安装

### 概念说明

Mooncake 是一个高性能分布式 KVCache 存储，为追求极致性能，采用了大量底层系统技术。**依赖安装**是搭建 Mooncake 环境的第一步，主要解决三个问题：

1. **网络性能**：Mooncake 核心优势在于 RDMA 零拷贝网络传输，需要安装 RDMA 驱动与 SDK（如 Mellanox OFED）
2. **计算加速**：在 GPU 环境中支持 GPUDirect RDMA（绕过 CPU 内存的 GPU-GPU 直传），需要 CUDA 12.1+ 和相应驱动
3. **编译与运行**：C++ 编译工具链（gcc、cmake）和 Python 3.10+ 运行时

**为什么需要这些依赖？**

- **RDMA 驱动**：提供 `libibverbs` 等verbs API，让用户态程序直接访问网卡硬件，实现内核旁路（kernel bypass）
- **CUDA + GPUDirect**：让 RDMA 网卡直接读写 GPU 显存，避免 CPU 中转，降低延迟与 CPU 占用
- **Python 3.10+**：Mooncake 的 Python API 需要现代 Python 特性（类型提示、异步 I/O 等）

### 伪代码或流程

依赖安装流程可概括为：

```bash
# 1. 操作系统检测
detect_os()  # Ubuntu/Debian 或 CentOS/RHEL

# 2. 系统包安装
if OS in [Ubuntu, Debian]:
    apt_install([
        "build-essential", "cmake", "ninja-build",
        "libibverbs-dev",      # RDMA verbs 开发库
        "libgoogle-glog-dev",  # 日志库
        "libgtest-dev",        # 测试框架
        "libjsoncpp-dev",     # JSON 解析
        "libnuma-dev",         # NUMA 亲和性
        "libboost-all-dev",   # 网络与工具库
        "libgrpc++-dev",      # RPC 框架
        "protobuf-compiler-grpc"
    ])
elif OS in [CentOS, RHEL]:
    yum_install([
        "rdma-core-devel", "glog-devel",
        "jsoncpp-devel", "numactl-devel", ...
    ])

# 3. 初始化 Git 子模块
git_submodule_sync()
git_submodule_update()

# 4. 编译安装 yalantinglibs（异步框架）
build_yalantinglibs()

# 5. 安装 Go 1.25.9（Mooncake Store 需要）
install_go()

# 6. 可选：安装 SPDK（NVMe-oF SSD 池支持）
if args.with_spdk:
    build_spdk()
```

### 原理分析

#### RDMA 网络原理

Mooncake 使用 RDMA（Remote Direct Memory Access）实现零拷贝网络传输。传统 TCP/IP 通信需要四次数据拷贝（网卡 → 内核 → 用户 → 内核 → 网卡），而 RDMA 通过 **verbs API** 让用户态程序直接注册内存区域到网卡，实现：

- **发送端**：直接从用户内存（或 GPU 显存）读取数据到网卡
- **接收端**：网卡直接写入用户内存（或 GPU 显存）

RDMA 操作的核心是 **内存注册（Memory Registration）**：

```c
// 伪代码：注册内存到 RDMA 网卡
struct ibv_mr *mr = ibv_reg_mr(pd, buffer, length,
    IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE);
// 网卡现在可以直接读写这块内存，无需 CPU 参与
```

#### GPUDirect RDMA 原理

当使用 CUDA GPU 时，Mooncake 支持 **GPUDirect RDMA**，让 RDMA 网卡直接读写 GPU 显存：

\[ \text{传统路径} : \text{GPU} \xrightarrow{\text{PCIe}} \text{DRAM} \xrightarrow{\text{RDMA}} \text{网络} \]
\[ \text{GPUDirect} : \text{GPU} \xrightarrow{\text{PCIe + GPUDirect}} \text{网络} \]

这需要：
1. **CUDA 12.1+** 和支持 GPUDirect 的驱动
2. **`nvidia-peermem`** 内核模块（或 DMA-BUF 路径）
3. RDMA 网卡支持物理地址转换

#### NUMA 亲和性

在多 CPU 插槽服务器中，Mooncake 使用 **NUMA（Non-Uniform Memory Access）** 优化内存访问：

```c
// 伪代码：NUMA 亲和性分配
void *buf = numa_alloc_onnode(size, target_node);
// 确保内存分配在 RDMA 网卡所在的 CPU 节点，减少跨插槽访问延迟
```

### 代码实践

#### 1. 自动依赖安装脚本

Mooncake 提供了 `dependencies.sh` 脚本自动安装所有依赖：

> [dependencies.sh 第 81-83 行](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/dependencies.sh#L81-L83) — 检查是否以 root 权限运行

```bash
if [ $(id -u) -ne 0 ]; then
    print_error "Require root permission, try sudo ./dependencies.sh"
fi
```

> [dependencies.sh 第 150-184 行](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/dependencies.sh#L150-L184) — 安装 Ubuntu/Debian 系统包

```bash
SYSTEM_PACKAGES="build-essential \
                 cmake \
                 ninja-build \
                 git \
                 wget \
                 unzip \
                 libibverbs-dev \           # RDMA verbs 开发库
                 libgoogle-glog-dev \       # 日志库
                 libgtest-dev \             # 测试框架
                 libjsoncpp-dev \           # JSON 解析
                 libunwind-dev \
                 libnuma-dev \              # NUMA 亲和性
                 libpython3-dev \
                 libboost-all-dev \
                 libssl-dev \
                 libgrpc-dev \
                 libgrpc++-dev \
                 libprotobuf-dev \
                 libyaml-cpp-dev \
                 protobuf-compiler-grpc \
                 libcurl4-openssl-dev \
                 libhiredis-dev \           # Redis 客户端
                 liburing-dev \             # libuv IO 库
                 libjemalloc-dev \          # 内存分配器
                 libmsgpack-dev \           # 序列化库
                 libzstd-dev \              # 压缩库
                 libasio-dev \              # 异步 I/O 框架
                 libxxhash-dev \
                 pkg-config \
                 patchelf \
                 libc6-dev \
                 libc-bin"

apt-get install -y $SYSTEM_PACKAGES
```

> [dependencies.sh 第 223-242 行](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/dependencies.sh#L223-L242) — 初始化 Git 子模块

```bash
print_section "Initializing Git Submodules"

# 检查 .gitmodules 文件是否存在
if [ -f "${REPO_ROOT}/.gitmodules" ]; then
    echo "Enter repository root: ${REPO_ROOT}"
    cd "${REPO_ROOT}"
    
    echo "Initializing git submodules..."
    git submodule sync --recursive
    git submodule update --init --recursive
```

**Git 子模块包括**：
- `extern/pybind11`：Python/C++ 绑定生成器
- `extern/yalantinglibs`：异步 HTTP/RPC 框架
- `extern/spdk`（可选）：NVMe-oF 存储框架

> [dependencies.sh 第 244-267 行](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/dependencies.sh#L244-L267) — 编译安装 yalantinglibs

```bash
print_section "Installing yalantinglibs"
cd "${REPO_ROOT}/extern/yalantinglibs"

mkdir -p build
cd build

echo "Configuring yalantinglibs..."
cmake .. -DBUILD_EXAMPLES=OFF -DBUILD_BENCHMARKS=OFF -DBUILD_UNIT_TESTS=OFF

echo "Building yalantinglibs (using $(nproc) cores)..."
cmake --build . -j$(nproc)

echo "Installing yalantinglibs..."
cmake --install .
```

> [dependencies.sh 第 283-354 行](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/dependencies.sh#L283-L354) — 安装 Go 1.25.9

```bash
print_section "Installing Go $GOVER"

install_go() {
    ARCH=$(uname -m)
    if [ "$ARCH" = "x86_64" ]; then
        ARCH="amd64"
    elif [ "$ARCH" = "aarch64" ]; then
        ARCH="arm64"
    fi
    
    GO_TARBALL="go$GOVER.linux-$ARCH.tar.gz"
    
    # 尝试多个下载镜像（官方、CN 镜像、阿里云）
    GO_DOWNLOAD_URLS=(
        "https://go.dev/dl/${GO_TARBALL}"
        "https://golang.google.cn/dl/${GO_TARBALL}"
        "https://mirrors.aliyun.com/golang/${GO_TARBALL}"
    )
    
    # 下载并安装 Go
    wget -O "${GO_TARBALL}" "${GO_DOWNLOAD_URLS[0]}"
    tar -C /usr/local -xzf "${GO_TARBALL}"
    
    # 添加到 PATH
    if ! grep -q "export PATH=\$PATH:/usr/local/go/bin" ~/.bashrc; then
        echo 'export PATH=$PATH:/usr/local/go/bin' >> ~/.bashrc
    fi
}

# 检查 Go 版本，安装或跳过
if command -v go &> /dev/null; then
    GO_VERSION=$(go version | awk '{print $3}')
    if [[ "$GO_VERSION" == "go$GOVER" ]]; then
        echo "Go $GOVER is already installed. Skipping..."
    else
        install_go
    fi
else
    install_go
fi
```

#### 2. 手动验证依赖

安装完成后，验证关键依赖：

```bash
# 验证 RDMA 驱动
$ ibv_devinfo
# 应显示 RDMA 网卡信息（如 mlx5_0）

# 验证 CUDA（如果使用 GPU）
$ nvidia-smi
# 应显示 GPU 信息和 CUDA 版本

# 验证 Python 版本
$ python --version
Python 3.10.x

# 验证 Go 版本
$ go version
go version go1.25.9 linux/amd64
```

### 练习题

1. **基础题**：为什么 Mooncake 需要 RDMA 网卡？TCP 协议能否满足需求？

2. **进阶题**：GPUDirect RDMA 相比传统 "GPU → DRAM → 网络" 路径有哪些优势？计算在延迟上能减少多少？（假设 PCIe 带宽 64 GB/s，延迟约 5μs；RDMA 网络带宽 200 Gbps，延迟约 1μs）

3. **实践题**：在一台新服务器上运行 `sudo bash dependencies.sh`，如果报错 "libibverbs-dev not found"，应该如何排查？

4. **开放题**：Mooncake 为什么选择 Go 语言实现 Mooncake Store 的 Master 服务？相比 C++ 有什么优势和劣势？

### 答案

**1. 基础题答案**：

Mooncake 需要 RDMA 网卡是因为：
- **性能**：RDMA 实现零拷贝传输，避免内核与 CPU 参与，降低延迟与 CPU 占用
- **带宽利用率**：在 8×400 Gbps RoCE 网络中，Mooncake 可达 **190 GB/s** 有效带宽，是 TCP 的 **4.6 倍**（见 [README.md 第 103 行](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/README.md#L103)）
- **GPUDirect 支持**：RDMA 网卡可直接读写 GPU 显存，绕过 CPU 内存

TCP 协议**可以**满足基本功能需求，Mooncake 也支持 TCP 作为回退协议，但在高吞吐、低延迟场景下性能差距显著。

**2. 进阶题答案**：

GPUDirect RDMA 优势：
- **减少拷贝**：从 2 次 PCIe 传输（GPU→DRAM→网卡）减少到 1 次（GPU→网卡）
- **降低延迟**：省去 DRAM 中转，减少约 5μs（PCIe 延迟）
- **降低 CPU 占用**：无需 CPU 参与 DMA 操作

延迟计算（传统 vs GPUDirect）：
- 传统路径：PCIe 读（5μs）+ DRAM 访问（0.1μs）+ RDMA 发送（1μs）≈ **6.1μs**
- GPUDirect：直接 RDMA 发送 ≈ **1μs**
- **节省约 5μs 延迟**，在频繁小数据传输中累积效应显著

**3. 实践题答案**：

排查步骤：
1. 检查 OS 版本：`cat /etc/os-release`
2. 检查是否启用了正确的软件源（特别是 Ubuntu 的 `restricted` 和 `universe` 源）
3. 手动搜索包：`apt-cache search libibverbs`
4. 如果是 CentOS，使用 `yum search rdma-core-devel`
5. 检查是否为 ARM 架构（某些包名可能不同）

**4. 开放题答案**：

Go 语言的优势：
- **并发模型**：Goroutine 轻量级并发，适合 Master 调度大量请求
- **开发效率**：内置 HTTP/JSON RPC，生态完善
- **内存安全**：GC 避免内存泄漏，适合长期运行服务
- **跨平台**：单一二进制文件，部署简单

劣势：
- **性能**：相比 C++ 有 GC 延迟和额外开销
- **精细控制**：无法手动管理内存和 CPU 亲和性

Mooncake 选择 Go 实现管理面（Master）、C++ 实现数据面（Transfer Engine）是**性能与开发效率的平衡**。

---

## 最小模块 2：编译构建

### 概念说明

**编译构建**是将 Mooncake 源码转换为可执行程序和库的过程。Mooncake 采用 **CMake + Make** 构建系统，支持：

- **多组件构建**：Transfer Engine、Mooncake Store、EP/PG、Python 绑定
- **可配置选项**：通过 CMake 选项控制功能（如 CUDA、RDMA、不同加速器）
- **交叉编译**：支持不同架构（x86_64、ARM64）和平台

**为什么需要从源码编译？**

1. **性能优化**：针对本地硬件编译，启用特定 CPU 指令集（如 AVX-512）
2. **功能定制**：启用/禁用特定功能（如 NVMe-oF、特定加速器支持）
3. **兼容性**：适配本地环境（RDMA 驱动版本、CUDA 版本）
4. **调试与开发**：获取调试符号、修改代码后重新构建

### 伪代码或流程

标准编译流程：

```bash
# 1. 环境准备
export CC=gcc
export CXX=g++
export CUDA_HOME=/usr/local/cuda  # 如果使用 CUDA

# 2. 创建构建目录
mkdir build && cd build

# 3. CMake 配置
cmake .. \
  -DCMAKE_BUILD_TYPE=Release \
  -DWITH_TE=ON \              # 构建 Transfer Engine
  -DWITH_STORE=ON \          # 构建 Mooncake Store
  -DWITH_EP=OFF \             # 不构建 EP/PG（高级功能）
  -DUSE_CUDA=ON \             # 启用 CUDA 支持（如有 GPU）
  -DBUILD_SHARED_LIBS=OFF     # 静态链接（推荐）

# 4. 编译
make -j$(nproc)               # 并行编译

# 5. 安装（可选）
sudo make install             # 安装到 /usr/local
```

### 原理分析

#### CMake 构建系统

CMake 是跨平台构建工具，**CMakeLists.txt** 定义构建规则。Mooncake 的 CMake 结构：

```
Mooncake/
├── CMakeLists.txt           # 根 CMakeLists（定义全局选项）
├── mooncake-common/         # 公共库（日志、配置、etcd 客户端）
├── mooncake-transfer-engine/  # Transfer Engine 核心库
├── mooncake-store/          # Mooncake Store 库
├── mooncake-integration/    # Python 绑定（pybind11）
└── mooncake-wheel/          # Python wheel 打包
```

CMake 配置阶段做：
1. **检测依赖**：检查是否安装了 CUDA、ROCm、MLU 等
2. **生成构建文件**：生成 `Makefile`（或 Ninja 构建文件）
3. **设置编译选项**：优化级别（`-O3`）、警告、宏定义

#### 编译选项解析

关键 CMake 选项：

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `WITH_TE` | `ON` | 构建 Transfer Engine |
| `WITH_STORE` | `ON` | 构建 Mooncake Store |
| `WITH_EP` | `OFF` | 构建 Mooncake EP（专家并行）和 PG（Process Group） |
| `USE_CUDA` | 自动检测 | 启用 CUDA 支持（GPUDirect RDMA） |
| `USE_MLU` | `OFF` | 启用 Cambricon MLU 支持 |
| `USE_NOF` | `OFF` | 启用 NVMe-oF SSD 池支持 |
| `BUILD_SHARED_LIBS` | `OFF` | 构建共享库（.so）而非静态库（.a） |

#### 链接与依赖关系

Mooncake 组件依赖关系：

```
mooncake-store 依赖 mooncake-transfer-engine
mooncake-transfer-engine 依赖 mooncake-common
mooncake-integration (Python) 依赖 mooncake-store 和 mooncake-transfer-engine
```

CMake 通过 `target_link_libraries()` 自动处理依赖：

```cmake
# mooncake-transfer-engine/CMakeLists.txt
target_link_libraries(engine
  PRIVATE
    mooncake_common
    ibverbs   # RDMA 库
    pthread   # 线程库
)
```

### 代码实践

#### 1. CMakeLists.txt 根配置

> [CMakeLists.txt 第 1-23 行](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/CMakeLists.txt#L1-L23) — 项目定义与选项

```cmake
cmake_minimum_required(VERSION 3.16)
project(mooncake CXX C)

# 全局配置标志
set(GLOBAL_CONFIG "true")

# 包含公共库的 CMake 模块
include(mooncake-common/FindJsonCpp.cmake)
include(mooncake-common/FindGLOG.cmake)
include(mooncake-common/common.cmake)

# 单元测试支持
if (BUILD_UNIT_TESTS)
  enable_testing()
endif()

# 组件构建选项
option(WITH_TE "build mooncake transfer engine and sample code" ON)
option(WITH_STORE "build mooncake store library and sample code" ON)
option(WITH_STORE_GO "build Go bindings for mooncake store" OFF)
option(WITH_P2P_STORE "build p2p store library and sample code" OFF)
option(WITH_RUST_EXAMPLE "build the Rust interface and sample code for the transfer engine" OFF)
option(WITH_STORE_RUST "build the Rust bindings for the Mooncake Store" ON)
option(WITH_EP "build mooncake with expert parallelism support" OFF)
option(USE_NOF "build mooncake store with NoF SSD pool support" OFF)
```

> [CMakeLists.txt 第 75-84 行](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/CMakeLists.txt#L75-L84) — 条件构建子目录

```cmake
# 根据 WITH_TE 选项决定是否构建 Transfer Engine
if (WITH_TE)
  add_subdirectory(mooncake-transfer-engine)
  include_directories(mooncake-transfer-engine/include)
endif()

# 根据 WITH_STORE 选项决定是否构建 Mooncake Store
if (WITH_STORE)
  message(STATUS "Mooncake Store will be built")
  add_subdirectory(mooncake-store)
  include_directories(mooncake-store/include)
endif()
```

> [CMakeLists.txt 第 94-172 行](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/CMakeLists.txt#L94-L172) — EP/PG 条件构建

```cmake
option(EP_USE_IDE "Enable intelligent indexing for IDEs" OFF)
if (WITH_EP)
  if (EP_USE_IDE)
    message(WARNING "EP_USE_IDE enabled. DO NOT USE IN PRODUCTION!")
    add_subdirectory(mooncake-ep)
    add_subdirectory(mooncake-pg)
  else ()
    message(STATUS "WITH_EP enabled: building Mooncake EP and PG Python extensions")
    if(USE_CUDA)
      find_package(CUDAToolkit REQUIRED)
      message(STATUS "Detected CUDA version: ${CUDAToolkit_VERSION}")
    endif()

    # EP_TORCH_VERSIONS: 要构建的 PyTorch 版本列表（分号分隔）
    if(NOT EP_TORCH_VERSIONS)
      set(EP_TORCH_VERSIONS "$ENV{EP_TORCH_VERSIONS}")
    endif()
    
    # TORCH_CUDA_ARCH_LIST: CUDA 架构列表（ forwarded to PyTorch extension build）
    if(NOT TORCH_CUDA_ARCH_LIST)
      set(TORCH_CUDA_ARCH_LIST "$ENV{TORCH_CUDA_ARCH_LIST}")
    endif()
    if(NOT TORCH_CUDA_ARCH_LIST)
      set(TORCH_CUDA_ARCH_LIST "8.0;9.0")
    endif()

    # 创建自定义目标构建 EP/PG 扩展
    add_custom_target(mooncake_ep_ext ALL
      COMMAND ${CMAKE_COMMAND} -E make_directory "${EP_PG_STAGING_DIR}"
      COMMAND ${CMAKE_COMMAND}
        "-DSOURCE_DIR=${CMAKE_CURRENT_SOURCE_DIR}/mooncake-ep"
        "-DEP_CUDA_MAJOR=${CUDAToolkit_VERSION_MAJOR}"
        "-DEP_CUDA_MINOR=${CUDAToolkit_VERSION_MINOR}"
        "-DEP_TORCH_VERSIONS=${_ep_torch_versions_pipe}"
        "-DTORCH_CUDA_ARCH_LIST=${_torch_cuda_arch_list_pipe}"
        "-DSTAGING_DIR=${EP_PG_STAGING_DIR}"
        "-DENGINE_SO_PATH=$<TARGET_FILE:engine>"
        -P "${CMAKE_CURRENT_SOURCE_DIR}/mooncake-ep/BuildEpExt.cmake"
      COMMENT "Building Mooncake EP Python extension(s)"
      DEPENDS engine
      VERBATIM
    )
  endif ()
endif()
```

#### 2. 编译与安装

> [README.md 第 263-279 行](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/README.md#L263-L279) — 标准编译流程

```bash
# 1. 克隆仓库
git clone https://github.com/kvcache-ai/Mooncake.git
cd Mooncake

# 2. 安装依赖
sudo bash dependencies.sh

# 3. 构建项目
mkdir build
cd build
cmake ..
make -j

# 4. 安装（可选，让 vLLM/SGLang 能找到 Mooncake）
sudo make install
```

**编译输出**：

在 `build/` 目录生成：
- `mooncake-transfer-engine/libengine.a`：Transfer Engine 静态库
- `mooncake-store/src/mooncake_master`：Master 服务可执行文件
- `mooncake-store/src/mooncake_client`：客户端可执行文件
- `mooncake-integration/engine.cpython-310-x86_64-linux-gnu.so`：Python 绑定

#### 3. 自定义编译示例

**启用 CUDA 支持**：

```bash
cmake .. -DUSE_CUDA=ON \
  -DCUDA_TOOLKIT_ROOT_DIR=/usr/local/cuda \
  -DCMAKE_CUDA_ARCHITECTURES="80;90"  # Ampere (A100) + Hopper (H100)
```

**启用 MLU 支持（Cambricon）**：

```bash
cmake .. -DUSE_MLU=ON \
  -DNEUWARE_ROOT=/usr/local/neuware
```

**启用 NVMe-oF SSD 池**：

```bash
# 首先安装 SPDK 依赖
sudo bash dependencies.sh --with-spdk

# 然后构建
cmake .. -DUSE_NOF=ON
make -j
```

### 练习题

1. **基础题**：为什么 Mooncake 需要用 CMake 而不是直接用 `gcc` 编译？

2. **进阶题**：在 CMake 配置阶段，`find_package(CUDAToolkit REQUIRED)` 的作用是什么？如果 CUDA 未安装会怎样？

3. **实践题**：编译 Mooncake 时，如何指定编译器为 clang 而不是 gcc？

4. **开放题**：Mooncake 为什么默认关闭 `WITH_EP`（专家并行）功能？在什么场景下应该启用？

### 答案

**1. 基础题答案**：

CMake 的优势：
- **跨平台**：同一套 CMakeLists.txt 可在 Linux、Windows、macOS 上生成不同的构建文件（Makefile、Visual Studio 项目）
- **依赖管理**：自动检测依赖库（CUDA、ROCm、MLU）的位置和版本
- **模块化**：通过 `add_subdirectory()` 组合多个子项目
- **IDE 支持**：生成 IDE 项目文件（如 CLion、VSCode）

直接用 `gcc` 需要：
- 手动编写大量编译命令（数十个 .cpp 文件）
- 手动处理头文件路径、库链接顺序
- 难以维护和跨平台

**2. 进阶题答案**：

`find_package(CUDAToolkit REQUIRED)` 的作用：
- **查找 CUDA**：在标准路径（`/usr/local/cuda`、`$CUDA_HOME`）查找 CUDA 安装
- **设置变量**：设置 `CMAKE_CUDA_ARCHITECTURES`、`CUDAToolkit_VERSION` 等变量
- **提供目标**：提供 `CUDA::cudart`、`CUDA::cuda_driver` 等 imported targets

如果 CUDA 未安装：
- **REQUIRED 存在时**：CMake 配置失败，报错 "Could not find CUDA"
- **REQUIRED 不存在时**：继续配置，但 CUDA 相关功能不可用

**3. 实践题答案**：

```bash
# 方法 1：环境变量
export CC=clang
export CXX=clang++
cmake ..

# 方法 2：CMake 命令行
cmake .. -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++
```

注意：clang 可能需要调整编译选项（如 `-stdlib=libc++` 而非 libstdc++）。

**4. 开放题答案**：

`WITH_EP` 默认关闭的原因：
- **依赖复杂**：需要 PyTorch、特定 CUDA 版本、编译 CUDA 扩展
- **高级功能**：EP/PG 用于大规模 MoE 推理和容错，普通用户不需要
- **编译时间长**：PyTorch 扩展编译耗时（5-10 分钟）

应启用场景：
- **MoE 推理**：使用 DeepSeek-MoE、Kimix-MoE 等稀疏模型
- **容错需求**：多机多卡环境需要 rank 故障检测与恢复
- **研究开发**：研究专家并行算法或分布式训练

---

## 最小模块 3：Python 包安装

### 概念说明

**Python 包安装**是通过 `pip install` 直接安装预编译好的 Mooncake wheel 包，避免从源码编译的复杂过程。这是**最快速上手 Mooncake 的方式**。

Mooncake 提供多个 Python 包变体：

| 包名 | 说明 | 依赖 |
|------|------|------|
| `mooncake-transfer-engine` | CUDA 版本（CUDA < 13.0） | CUDA 12.1+ |
| `mooncake-transfer-engine-cuda13` | CUDA 13.0/13.1 版本 | CUDA 13.0+ |
| `mooncake-transfer-engine-non-cuda` | 非 CUDA 版本 | 无 GPU 依赖 |
| `mooncake-transfer-engine-npu` | Ascend NPU 版本 | Ascend CANN |

**为什么有多个包？**

- **CUDA 版本差异**：CUDA 12.x 和 13.x 的 ABI 不兼容，需要分别编译
- **GPU vs CPU**：非 GPU 环境不需要 CUDA 依赖，减小安装体积
- **异构硬件**：Ascend NPU 使用不同的运行时（CANN 而非 CUDA）

### 伪代码或流程

Python 包安装流程：

```bash
# 1. 选择合适的包
if has_gpu and cuda_version >= "13.0":
    package = "mooncake-transfer-engine-cuda13"
elif has_gpu and cuda_version < "13.0":
    package = "mooncake-transfer-engine"
else:
    package = "mooncake-transfer-engine-non-cuda"

# 2. 安装包
pip install ${package} numpy pyzmq

# 3. 验证安装
python -c "from mooncake.engine import TransferEngine; print('Success!')"
```

### 原理分析

#### Wheel 打包机制

Wheel（`.whl`）是 Python 预编译包格式，包含：

- **编译好的扩展**：`.so` 文件（如 `engine.so`、`store.so`）
- **Python 代码**：`.py` 文件（如 `__init__.py`）
- **元数据**：`WHEEL`、`METADATA` 文件（版本、依赖、标签）

Wheel 标签示例：
```
mooncake_transfer_engine-0.9.0-cp310-cp310-linux_x86_64.whl
│                │     │    │     │    │     │
│                │     │    │     │    │     └─ 架构（x86_64）
│                │     │    │     │    └─────── 操作系统（linux）
│                │     │    │     └──────────── Python 版本（3.10）
│                │     │    └────────────────── 实现（CPython）
│                │     └─────────────────────── Python 版本（3.10）
│                └───────────────────────────── 版本号
└───────────────────────────────────────────── 包名
```

Mooncake 的 wheel 使用 **manylinux** 标签（PEP 600），保证在多数 Linux 发行版上兼容。

#### auditwheel 修复

预编译的 `.so` 文件依赖系统库（如 `libibverbs.so`），直接在不同机器上运行可能报错 "libxxx.so not found"。

**auditwheel** 工具通过以下方式解决：
1. **收集依赖**：扫描 `.so` 的 `DT_NEEDED` 字段，找出所有依赖库
2. **打包库**：将非标准库（如 `libasio.so`）复制到 wheel 内
3. **设置 RPATH**：修改 `.so` 的 `RPATH` 为 `$ORIGIN`，优先从 wheel 内查找

> [build_wheel.sh 第 303-391 行](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/scripts/build_wheel.sh#L303-L391) — auditwheel 修复配置

```bash
${AUDITWHEEL_CMD} repair ${OUTPUT_DIR}/*.whl \
    --exclude libcurl.so* \
    --exclude libfabric.so* \
    --exclude libefa.so* \
    --exclude libibverbs.so* \  # 排除标准 RDMA 库（系统提供）
    --exclude libmlx5.so* \
    --exclude libnuma.so* \
    --exclude libstdc++.so* \   # 排除标准 C++ 库（系统提供）
    --exclude libgcc_s.so* \
    --exclude libc.so* \        # 排除 libc（系统提供）
    # ... 更多排除项
    -w ${REPAIRED_DIR}/ --plat ${PLATFORM_TAG}
```

**排除标准库的原因**：
- 这些库在所有 Linux 系统上都存在
- 打包它们会增大 wheel 体积（从 ~10MB 增加到 ~100MB）
- 可能与系统版本冲突

#### Python 绑定原理

Mooncake 使用 **pybind11** 将 C++ 库暴露给 Python：

```cpp
// mooncake-integration/engine.cpp（伪代码）
#include <pybind11/pybind11.h>

PYBIND11_MODULE(engine, m) {
    // 暴露 TransferEngine 类
    py::class_<TransferEngine>(m, "TransferEngine")
        .def(py::init<>())
        .def("initialize", &TransferEngine::initialize)
        .def("register_memory", &TransferEngine::register_memory)
        .def("transfer_sync_write", &TransferEngine::transfer_sync_write);
}
```

编译生成 `engine.cpython-310-x86_64-linux-gnu.so` 后，Python 可以：

```python
from mooncake.engine import TransferEngine  # 导入 C++ 类

engine = TransferEngine()  # 调用 C++ 构造函数
engine.initialize(...)     # 调用 C++ 方法
```

### 代码实践

#### 1. PyPI 包安装

> [README.md 第 232-259 行](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/README.md#L232-L259) — Python 包安装指南

```bash
# CUDA < 13.0
pip install mooncake-transfer-engine

# CUDA >= 13.0
pip install mooncake-transfer-engine-cuda13

# 非 CUDA 系统
pip install mooncake-transfer-engine-non-cuda

# NPU 系统
pip install mooncake-transfer-engine-npu
```

> [docs/source/getting_started/quick-start.md 第 9-12 行](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/getting_started/quick-start.md#L9-L12) — 安装 CUDA 版本

```bash
# For CUDA-enabled systems:
pip install mooncake-transfer-engine numpy pyzmq
```

**重要提示**：
- CUDA 版本包包含 Mooncake-EP 和 GPU 拓扑检测，需要 CUDA 12.1+
- 非 CUDA 版本用于无 GPU 依赖的环境
- 如果遇到 "libxxx.so not found"，应卸载包并手动从源码构建

#### 2. 安装验证

安装完成后，验证 Python API：

```python
$ python
>>> from mooncake.engine import TransferEngine
>>> from mooncake.store import MooncakeDistributedStore
>>> print("Mooncake installed successfully!")
Mooncake installed successfully!
```

验证命令行工具（如果安装了 Mooncake Store）：

```bash
$ mooncake_master --help
Usage: mooncake_master [OPTIONS]

Options:
  --allocation_strategy STR     Allocation strategy (free_ratio_first, etc.)
  --enable_http_metadata_server BOOL  Enable HTTP metadata server
  --http_metadata_server_host STR     HTTP server host
  --http_metadata_server_port INT     HTTP server port
  --rpc_address STR            RPC server address
  --rpc_interface STR         Network interface for RPC
```

#### 3. 依赖问题处理

如果报错 "ImportError: libmooncake_common.so: cannot open shared object file"：

**原因**：wheel 内的库未正确设置 `RPATH`

**解决**：

```bash
# 方法 1：重新安装（可能是 pip 缓存问题）
pip uninstall mooncake-transfer-engine
pip install --no-cache-dir mooncake-transfer-engine

# 方法 2：设置 LD_LIBRARY_PATH
export LD_LIBRARY_PATH=$(python -c "import site; print(site.getsitepackages()[0])")/mooncake:$LD_LIBRARY_PATH

# 方法 3：从源码编译（参考最小模块 2）
```

### 练习题

1. **基础题**：为什么 Mooncake 要提供多个 Python 包变体（CUDA、non-CUDA、NPU）？不能提供单一包吗？

2. **进阶题**：Wheel 标签 `manylinux_2_31_x86_64` 中的 `2_31` 代表什么？如何检测系统的 glibc 版本？

3. **实践题**：在一台没有 root 权限的机器上安装 Mooncake，如何验证 RDMA 驱动是否正常工作？

4. **开放题**：auditwheel 修复时为什么要排除 `libibverbs.so` 等标准库？如果将它们打包进去会有什么后果？

### 答案

**1. 基础题答案**：

不能提供单一包的原因：
- **CUDA ABI 不兼容**：CUDA 12.x 和 13.x 的二进制接口不兼容，混用会崩溃
- **依赖链不同**：CUDA 版本依赖 `libcudart.so`，NPU 版本依赖 `libascendcl.so`
- **安装体积**：单一包会包含所有硬件的库，体积从 ~10MB 增加到 ~500MB
- **License 问题**：某些硬件驱动可能有额外 License 限制

类似的做法：
- PyTorch：`torch`（CUDA 11.8）、`torch`（CUDA 12.1）、`torch`（CPU）分别发布
- TensorFlow：`tensorflow`（CPU）、`tensorflow-gpu`（CUDA）

**2. 进阶题答案**：

`manylinux_2_31_x86_64` 的 `2_31` 代表：
- **glibc 版本**：2.31（Ubuntu 20.04 的 glibc 版本）
- **兼容性**：该 wheel 在 glibc ≥ 2.31 的系统上可运行（如 Ubuntu 20.04+、CentOS 8+）

检测系统 glibc 版本：
```bash
# 方法 1：getconf（推荐）
$ getconf GNU_LIBC_VERSION
glibc 2.31

# 方法 2：ldd
$ ldd --version
ldd (Ubuntu GLIBC 2.31-0ubuntu9) 2.31

# 方法 3：Python（packaging 库）
$ python -c "from packaging.tags import glibc_version_string; print(glibc_version_string())"
2.31
```

Mooncake 的 [setup.py 第 46-103 行](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-wheel/setup.py#L46-L103) 实现了自动检测。

**3. 实践题答案**：

```bash
# 1. 安装 Mooncake
pip install --user mooncake-transfer-engine

# 2. 验证 RDMA 驱动（无需 root）
$ ibv_devinfo
# 如果报错 "ibv_devinfo: command not found"，说明未安装 RDMA 用户态工具
# 如果显示网卡信息（mlx5_0、 roce0 等），说明 RDMA 正常

# 3. Python 脚本验证
$ python
>>> from mooncake.engine import TransferEngine
>>> engine = TransferEngine()
>>> # 以下代码会自动检测 RDMA 设备
>>> engine.initialize("localhost", "P2PHANDSHAKE", "rdma", "")
```

如果报错 "No RDMA device found"：
- 检查是否安装了 `rdma-core` 包：`dpkg -l | grep rdma-core`
- 检查网卡是否支持 RDMA：`lspci | grep Mellanox`
- 检查 RDMA 子系统：`ls /sys/class/infiniband/`

**4. 开放题答案**：

排除标准库的原因：

1. **体积增大**：
   - 仅 `libstdc++.so`、`libgcc_s.so` 就 ~5MB
   - 所有排除库合计 ~50MB
   - wheel 从 ~10MB 增加到 ~60MB

2. **版本冲突风险**：
   - 系统库版本可能低于 wheel 打包的版本
   - 例：wheel 带 `libstdc++.so.6.0.29`（GCC 12），系统只有 `libstdc++.so.6.0.28`（GCC 11）
   - 加载时可能报错 "version `GLIBCXX_3.4.29' not found"

3. **安全更新**：
   - 系统库会通过 OS 安全更新修复漏洞（如 CVE）
   - Wheel 打包的库无法更新，成为安全死角

**如果打包进去的后果**：
- 安装体积增大 5-6 倍
- 在旧系统上可能无法运行（依赖 newer glibc）
- 无法受益于系统库的安全更新

因此，auditwheel 只打包**非标准库**（如 `libasio.so`、`libmooncake_common.so`），标准库依赖系统提供。

---

## 最小模块 4：Hello World 示例

### 概念说明

**Hello World 示例**是验证 Mooncake 环境正确性的关键步骤。Mooncake 提供两个核心组件的示例：

1. **Transfer Engine 示例**：客户端-服务端模型，演示高吞吐数据传输
2. **Mooncake Store 示例**：分布式 KV 存储，演示 put/get 操作

这两个示例涵盖了 Mooncake 的核心功能：
- **RDMA 零拷贝传输**（Transfer Engine）
- **分布式 KV 缓存**（Mooncake Store）
- **Python API 使用**（内存注册、数据传输、元数据管理）

**为什么需要运行 Hello World？**

1. **验证安装**：确认 RDMA 驱动、CUDA、Python 包正确安装
2. **学习 API**：了解初始化、内存注册、数据传输的基本流程
3. **性能基准**：测试 RDMA 网络的实际带宽和延迟

### 伪代码或流程

#### Transfer Engine Hello World 流程

```python
# 服务端
def server():
    # 1. 初始化 ZMQ（发送 buffer 信息）
    zmq_socket = ZMQ_PUSH()
    zmq_socket.bind("tcp://*:5555")
    
    # 2. 初始化 Transfer Engine
    engine = TransferEngine()
    engine.initialize("localhost", "P2PHANDSHAKE", "rdma", "")
    
    # 3. 分配并注册内存
    buffer = np.zeros(1MB, dtype=np.uint8)
    ptr = buffer.ctypes.data
    engine.register_memory(ptr, len(buffer))
    
    # 4. 发送 buffer 信息给客户端
    zmq_socket.send_json({"session_id": "...", "ptr": ptr, "len": len(buffer)})
    
    # 5. 等待接收数据（客户端调用 transfer_sync_write）

# 客户端
def client():
    # 1. 接收服务端 buffer 信息
    zmq_socket = ZMQ_PULL()
    zmq_socket.connect("tcp://localhost:5555")
    buffer_info = zmq_socket.recv_json()
    
    # 2. 初始化 Transfer Engine
    engine = TransferEngine()
    engine.initialize("localhost", "P2PHANDSHAKE", "rdma", "")
    
    # 3. 分配并注册本地内存
    local_buffer = np.ones(1MB, dtype=np.uint8)
    local_ptr = local_buffer.ctypes.data
    engine.register_memory(local_ptr, len(local_buffer))
    
    # 4. 传输数据
    engine.transfer_sync_write(
        buffer_info["session_id"],  # 目标 session
        local_ptr,                    # 源地址
        buffer_info["ptr"],           # 目标地址
        min(len(local_buffer), buffer_info["len"])  # 传输长度
    )
```

#### Mooncake Store Hello World 流程

```python
# 1. 启动 Master 服务（独立进程）
mooncake_master --enable_http_metadata_server=true

# 2. Python 客户端
store = MooncakeDistributedStore()
store.setup(
    "localhost",                    # 本节点地址
    "http://localhost:8080/metadata",  # Master 元数据服务
    512*1024*1024,                  # segment 大小（512MB）
    128*1024*1024,                  # 本地 buffer（128MB）
    "tcp",                          # 传输协议
    "",                             # RDMA 设备（自动检测）
    "localhost:50051"               # Master RPC 地址
)

# 3. 存储 KV
store.put("hello_key", b"Hello, Mooncake Store!")

# 4. 检索 KV
data = store.get("hello_key")
print(data.decode())  # "Hello, Mooncake Store!"

# 5. 清理
store.close()
```

### 原理分析

#### Transfer Engine 初始化流程

Transfer Engine 的 `initialize()` 方法执行以下步骤：

1. **协议选择**：根据 `protocol` 参数选择传输层（rdma/tcp/efa）
2. **设备检测**：扫描 RDMA 设备（`ibv_devinfo`），选择最优网卡
3. **RPC 服务启动**：启动 RPC 线程（默认端口随机，通过 `get_rpc_port()` 获取）
4. **元数据注册**：向元数据服务器（etcd 或 P2PHANDSHAKE）注册本节点

P2PHANDSHAKE 模式（点对点握手）：
- 无需 etcd，客户端直接连接服务端 RPC 端口
- 适合单次测试或小规模部署

#### 内存注册原理

RDMA 要求**内存注册**（Memory Registration）后才可传输：

```c
// 伪代码：内存注册
struct ibv_mr *mr = ibv_reg_mr(
    pd,           # Protection Domain
    buffer,       # 内存起始地址
    length,       # 内存长度
    IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE  # 访问权限
);

# 返回的 mr->lkey 是本地 key（lkey），mr->rkey 是远程 key（rkey）
# 网卡使用 lkey/rkey 访问内存，而非虚拟地址
```

**Python 封装**：

```python
# Python 中，buffer 是 NumPy 数组
buffer = np.zeros(1024*1024, dtype=np.uint8)
ptr = buffer.ctypes.data  # 虚拟地址

# 注册后，引擎内部记录 ptr -> lkey/rkey 的映射
engine.register_memory(ptr, buffer.nbytes)
```

#### 数据传输原理

`transfer_sync_write()` 执行 RDMA WRITE 操作：

\[ \text{流程：} \text{客户端} \xrightarrow{\text{RDMA WRITE}} \text{服务端显存} \]

1. **查询 rkey**：客户端通过元数据服务获取服务端 buffer 的 `rkey`
2. **Post Send**：客户端向网卡提交 Work Request（WR）
3. **RDMA WRITE**：网卡直接写入服务端内存（无需 CPU 参与）
4. **Completion**：客户端轮询 Completion Queue（CQ），确认传输完成

**同步 vs 异步**：
- `transfer_sync_write()`：同步阻塞，等待传输完成
- `transfer_async_write()`：异步非阻塞，通过回调通知

#### Mooncake Store 架构

Mooncake Store 是三层架构：

```
┌─────────────────────────────────────────────────┐
│              Python Client                     │
│  MooncakeDistributedStore.put() / get()       │
└─────────────────┬───────────────────────────────┘
                  │ RPC (gRPC)
┌─────────────────▼───────────────────────────────┐
│           Mooncake Master                      │
│  • 元数据管理（segment 分配、replica 位置）      │
│  • HTTP 元数据服务（/metadata 端点）             │
└─────────────────┬───────────────────────────────┘
                  │ TCP/RDMA
┌─────────────────▼───────────────────────────────┐
│         Mooncake Store 节点                     │
│  • 本地存储管理（DRAM/NVMe）                    │
│  • Transfer Engine 传输层                       │
│  • Replication / Eviction                      │
└─────────────────────────────────────────────────┘
```

**Master 的作用**：
- **Segment 分配**：根据请求大小选择合适的 segment（如 512MB）
- **Replica 管理**：决定 KV 存储在哪些节点（默认 3 副本）
- **负载均衡**：`free_ratio_first` 策略优先选择空闲率高的 segment

**Segment**：
- 固定大小的存储块（如 512MB）
- 每个 segment 包含多个 KV 对
- Segment 可在不同节点间迁移（rebalance）

### 代码实践

#### 1. Transfer Engine Hello World

> [docs/source/getting_started/quick-start.md 第 30-100 行](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/getting_started/quick-start.md#L30-L100) — Transfer Engine 服务端代码

```python
import numpy as np
import zmq
from mooncake.engine import TransferEngine

def main():
    # 初始化 ZMQ context 和 socket（用于发送 buffer 信息）
    context = zmq.Context()
    socket = context.socket(zmq.PUSH)
    socket.bind("tcp://*:5555")  # 绑定 5555 端口

    HOSTNAME = "localhost"
    METADATA_SERVER = "P2PHANDSHAKE"  # 无需 etcd 的点对点模式
    PROTOCOL = "rdma"                 # 使用 RDMA 协议
    DEVICE_NAME = ""                  # 自动检测 RDMA 设备

    # 初始化服务端引擎
    server_engine = TransferEngine()
    server_engine.initialize(
        HOSTNAME,
        METADATA_SERVER,
        PROTOCOL,
        DEVICE_NAME
    )
    session_id = f"{HOSTNAME}:{server_engine.get_rpc_port()}"

    # 分配服务端内存（1MB buffer）
    server_buffer = np.zeros(1024 * 1024, dtype=np.uint8)
    server_ptr = server_buffer.ctypes.data
    server_len = server_buffer.nbytes

    # 注册内存到 RDMA 网卡
    if PROTOCOL == "rdma":
        ret_value = server_engine.register_memory(server_ptr, server_len)
        if ret_value != 0:
            print("Mooncake memory registration failed.")
            raise RuntimeError("Mooncake memory registration failed.")

    print(f"Server initialized with session ID: {session_id}")
    print(f"Server buffer address: {server_ptr}, length: {server_len}")

    # 通过 ZMQ 发送 buffer 信息给客户端
    buffer_info = {
        "session_id": session_id,
        "ptr": server_ptr,
        "len": server_len
    }
    socket.send_json(buffer_info)
    print("Buffer information sent to client")

    # 保持服务端运行
    try:
        while True:
            input("Press Ctrl+C to exit...")
    except KeyboardInterrupt:
        print("\nShutting down server...")
    finally:
        # 清理：注销内存
        if PROTOCOL == "rdma":
            ret_value = server_engine.unregister_memory(server_ptr)
            if ret_value != 0:
                print("Mooncake memory deregistration failed.")
                raise RuntimeError("Mooncake memory deregistration failed.")

        socket.close()
        context.term()

if __name__ == "__main__":
    main()
```

> [docs/source/getting_started/quick-start.md 第 105-183 行](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/getting_started/quick-start.md#L105-L183) — Transfer Engine 客户端代码

```python
import numpy as np
import zmq
from mooncake.engine import TransferEngine

def main():
    # 初始化 ZMQ context 和 socket（接收服务端 buffer 信息）
    context = zmq.Context()
    socket = context.socket(zmq.PULL)
    socket.connect(f"tcp://localhost:5555")

    # 等待服务端 buffer 信息
    print("Waiting for server buffer information...")
    buffer_info = socket.recv_json()
    server_session_id = buffer_info["session_id"]
    server_ptr = buffer_info["ptr"]
    server_len = buffer_info["len"]
    print(f"Received server info - Session ID: {server_session_id}")
    print(f"Server buffer address: {server_ptr}, length: {server_len}")

    # 初始化客户端引擎
    HOSTNAME = "localhost"
    METADATA_SERVER = "P2PHANDSHAKE"
    PROTOCOL = "rdma"
    DEVICE_NAME = ""

    client_engine = TransferEngine()
    client_engine.initialize(
        HOSTNAME,
        METADATA_SERVER,
        PROTOCOL,
        DEVICE_NAME
    )
    session_id = f"{HOSTNAME}:{client_engine.get_rpc_port()}"

    # 分配并初始化客户端 buffer（1MB，填充为 1）
    client_buffer = np.ones(1024 * 1024, dtype=np.uint8)
    client_ptr = client_buffer.ctypes.data
    client_len = client_buffer.nbytes

    # 注册内存到 RDMA 网卡
    if PROTOCOL == "rdma":
        ret_value = client_engine.register_memory(client_ptr, client_len)
        if ret_value != 0:
            print("Mooncake memory registration failed.")
            raise RuntimeError("Mooncake memory registration failed.")

    print(f"Client initialized with session ID: {session_id}")

    # 传输数据：客户端 -> 服务端（10 次）
    print("Transferring data to server...")
    for _ in range(10):
        ret = client_engine.transfer_sync_write(
            server_session_id,              # 目标 session ID
            client_ptr,                      # 源地址（客户端 buffer）
            server_ptr,                      # 目标地址（服务端 buffer）
            min(client_len, server_len)       # 传输长度
        )

        if ret >= 0:
            print("Transfer successful!")
        else:
            print("Transfer failed!")

    # 清理：注销内存
    if PROTOCOL == "rdma":
        ret_value = client_engine.unregister_memory(client_ptr)
        if ret_value != 0:
            print("Mooncake memory deregistration failed.")
            raise RuntimeError("Mooncake memory deregistration failed.")

    socket.close()
    context.term()

if __name__ == "__main__":
    main()
```

#### 2. Mooncake Store Hello World

> [docs/source/getting_started/quick-start.md 第 193-203 行](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/getting_started/quick-start.md#L193-L203) — 启动 Master 服务

```bash
mooncake_master \
  --enable_http_metadata_server=true \
  --http_metadata_server_host=0.0.0.0 \
  --http_metadata_server_port=8080
```

**可选参数**：
- `--allocation_strategy=free_ratio_first`：使用空闲率优先的分配策略
- `--rpc_address=10.0.0.1:50051`：指定 RPC 监听地址（默认自动检测）
- `--rpc_interface=eth0`：指定网络接口（容器环境中使用）

> [docs/source/getting_started/quick-start.md 第 217-245 行](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/getting_started/quick-start.md#L217-L245) — Mooncake Store Python 客户端

```python
from mooncake.store import MooncakeDistributedStore

# 1. 创建 store 实例
store = MooncakeDistributedStore()

# 2. 配置 store
store.setup(
    "localhost",               # 本节点地址
    "http://localhost:8080/metadata",  # HTTP 元数据服务
    512*1024*1024,             # 512MB segment 大小
    128*1024*1024,             # 128MB 本地 buffer
    "tcp",                     # 使用 TCP（生产环境用 RDMA）
    "",                        # 自动检测 RDMA 设备
    "localhost:50051"          # Master RPC 地址
)

# 3. 存储数据
store.put("hello_key", b"Hello, Mooncake Store!")

# 4. 检索数据
data = store.get("hello_key")
print(data.decode())  # Output: Hello, Mooncake Store!

# 5. 清理
store.close()
```

#### 3. 运行示例

**Transfer Engine 示例**：

```bash
# 终端 1：启动服务端
$ python server.py
Server initialized with session ID: localhost:45678
Server buffer address: 140735237091328, length: 1048576
Buffer information sent to client
Press Ctrl+C to exit...

# 终端 2：启动客户端
$ python client.py
Waiting for server buffer information...
Received server info - Session ID: localhost:45678
Server buffer address: 140735237091328, length: 1048576
Client initialized with session ID: localhost:45679
Transferring data to server...
Transfer successful!
Transfer successful!
...
```

**Mooncake Store 示例**：

```bash
# 终端 1：启动 Master
$ mooncake_master --enable_http_metadata_server=true
I20250617 10:00:00.000000 12345 main.cc:200] Master started on localhost:50051
I20250617 10:00:00.000000 12345 main.cc:210] HTTP metadata server on 0.0.0.0:8080

# 终端 2：运行 Python 客户端
$ python store_hello_world.py
Hello, Mooncake Store!
```

### 练习题

1. **基础题**：Transfer Engine 示例中为什么需要 ZMQ？不能直接在 `initialize()` 时传递地址吗？

2. **进阶题**：在 Mooncake Store 示例中，如果 Master 进程崩溃，已存储的 KV 数据会丢失吗？为什么？

3. **实践题**：修改 Transfer Engine 示例，测量传输 1GB 数据的吞吐量（使用 `time.time()` 计时）。

4. **开放题**：Mooncake Store 的 `free_ratio_first` 分配策略相比简单的 "round-robin" 有什么优势？在什么场景下效果更明显？

### 答案

**1. 基础题答案**：

需要 ZMQ 的原因：
- **RDMA 限制**：RDMA 传输需要知道目标内存的**虚拟地址**和 `rkey`，这些信息无法通过 RDMA 本身传递（先有鸡还是先有蛋问题）
- **控制面与数据面分离**：ZMQ 是控制面（传递元数据），RDMA 是数据面（传输数据）
- **灵活性**：ZMQ 支持多种模式（PUSH/PULL、REQ/REP），易于扩展

直接在 `initialize()` 传递地址的问题：
- `initialize()` 只配置本机，不知道远程 buffer 信息
- 需要**带外（out-of-band）通道**交换地址，ZMQ 就是这个通道

**2. 进阶题答案**：

Master 崩溃后，**数据不会丢失**，原因：
- **存储在 Store 节点**：KV 数据实际存储在 Store 节点的本地内存（DRAM/NVMe），而非 Master
- **Master 只管理元数据**：Master 只记录 "哪个 key 在哪些 segment"，segment 在 Store 节点上
- **Store 节点独立运行**：Store 节点可以继续服务已缓存的 KV（通过本地索引）

但会影响：
- **无法分配新 segment**：新 key 的 `put()` 会失败（无法联系 Master）
- **无法 rebalance**：segment 迁移、replica 管理停止
- **元数据不一致**：如果 Master 重启，需要从 Store 节点恢复元数据（当前版本可能不完全支持）

**高可用方案**：
- 部署多个 Master（使用 etcd 选主）
- Store 节点定期向 Master 心跳，Master 宕机后 Store 进入 "degraded mode"

**3. 实践题答案**：

```python
import time
import numpy as np

# 在客户端代码中修改
client_buffer = np.ones(1024*1024*1024, dtype=np.uint8)  # 1GB
client_ptr = client_buffer.ctypes.data
client_len = client_buffer.nbytes

# 注册内存（一次性）
client_engine.register_memory(client_ptr, client_len)

# 测量吞吐量
start = time.time()
ret = client_engine.transfer_sync_write(
    server_session_id, client_ptr, server_ptr, min(client_len, server_len)
)
end = time.time()

throughput_gbps = (client_len / (1024**3)) / (end - start)
print(f"Throughput: {throughput_gbps:.2f} GB/s")
```

预期结果：
- **RDMA 环境**：~10-25 GB/s（取决于网卡带宽，如 100 Gbps → ~12 GB/s）
- **TCP 环境**：~5-10 GB/s（受内核协议栈限制）

**4. 开放题答案**：

`free_ratio_first` 的优势：
- **平衡负载**：优先使用空闲率高的 segment，避免某些 segment 过满而其他 segment 空闲
- **减少碎片**：均匀分布 KV，降低内存碎片（大量小 KV 填充同一 segment 会浪费空间）
- **提高命中率**：在分层缓存（DRAM + NVMe）中，均匀分布有利于 LRU 淘汰策略

**Round-robin 的问题**：
- 不考虑 segment 实际使用情况，可能导致：
  - Segment A：95% 满（多为小 KV）
  - Segment B：50% 满（大 KV）
  - 新 key 分配到 A 时可能触发 eviction，而 B 有空闲空间

**场景对比**：

| 场景 | free_ratio_first | round-robin |
|------|------------------|-------------|
| 大小均匀的 KV | 相似 | 相似 |
| 大量小 KV + 少量大 KV | **优势明显**（避免大 KV 填满 segment） | 可能导致大 KV 聚集 |
| 频繁 eviction | **优势明显**（均匀淘汰） | 某些 segment 淘汰频繁 |
| 简单负载（如基准测试） | 相似 | 相似 |

Mooncake 默认使用 `free_ratio_first` 是因为**生产环境 KV 大小分布不均匀**（短 prompt vs 长文档）。

---

## 总结

本讲义覆盖了 Mooncake 环境搭建与快速开始的四个核心模块：

1. **依赖安装**：通过 `dependencies.sh` 自动安装 RDMA 驱动、CUDA、Go 等依赖，理解 RDMA、GPUDirect、NUMA 等底层技术原理
2. **编译构建**：使用 CMake + Make 从源码构建 Mooncake，了解 CMake 选项、链接关系、EP/PG 构建
3. **Python 包安装**：通过 `pip install` 快速安装预编译 wheel，理解 wheel 打包、auditwheel 修复、多包变体
4. **Hello World 示例**：运行 Transfer Engine 和 Mooncake Store 示例，验证环境正确性，学习 Python API

通过本讲义，读者应能够：
- 在新服务器上从零搭建 Mooncake 环境（依赖安装 → 编译或安装 Python 包）
- 理解 RDMA、GPUDirect、NUMA 等底层技术如何提升性能
- 运行并修改 Hello World 示例，测试 RDMA 网络的实际带宽
- 为后续深入学习 Transfer Engine 架构和 Mooncake Store 设计做好准备

下一步，建议学习 [U2-L1 Transfer Engine 设计](../unit-2/u2-l1-transfer-engine-design.md)，了解 Mooncake 核心组件的架构原理。
