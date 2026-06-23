# 异构传输：NVLink / NVMe-oF / EFA / CXL / Ascend / HIP / MUSA

## 1. 本讲目标

Mooncake Transfer Engine 之所以能在一个引擎里同时服务于 RDMA 集群、AWS EFA 云、GPU NVLink 直连、CXL 内存池、华为昇腾 NPU、AMD ROCm、摩尔线程 MUSA 等截然不同的硬件，靠的不是"为每种硬件写一套独立引擎"，而是**一套统一的 `Transport` 接口 + 每种协议各自的差异化实现**。这就是本讲要讲清楚的"异构传输"主题。

学完本讲后，你应该能够：

1. 说出仓库支持的**全部传输协议**（tcp / rdma / efa / nvmeof / nvlink / nvlink_intra / hip / barex / cxl / ascend / ub / maca / sunrise_link / ubshmem），以及它们各自的适用场景；
2. 理解所有协议都实现同一个 `Transport` 基类契约（`submitTransfer` / `getTransferStatus` / `registerLocalMemory` / `getName`），并各自维护**协议专属元数据**；
3. 看懂"协议字符串 → Transport 实例"的工厂（`MultiTransport::installTransport`）以及与之配套的**编译开关**（`USE_*` 宏）和**运行时依赖**；
4. 会读 `docs/source/getting_started/supported-protocols.md` 这份协议支持文档，并知道如何做设备探测（`ibv_devices` 等）；
5. 理解 **Ascend Direct 如何在同进程共存的多 TE 下按角色区分链路**：用一个瞬时标志 `ascend_store_te_init` 区分 Store TE 与 P2P TE，并据此从同一份 `ASCEND_GLOBAL_RESOURCE_CONFIG` JSON 里取不同的链路配置（Store=RoCE / P2P=HCCS）；
6. 理解 **CUDA 与 MUSA 在跨 GPU 内存可见性上的 fence 原语差异**：为什么 MUSA 的 `mc_fence()` 必须显式 `__threadfence_system()`，而 CUDA 的 `mc_fence()` 是空操作。

> 本讲只读不写源码，所有引用都来自当前 HEAD `1f7f71a1`。本讲是 `u3-l1`（Transport 基类与 Slice/Task/Batch 模型）的延续——如果你还不清楚 `submitTransfer` / `Slice` / `BatchDesc` 是什么，建议先读 `u3-l1`。

## 2. 前置知识

进入源码前，先建立几个直觉。

**直觉一：上层只认"协议字符串"，不认硬件细节。** 当用户初始化引擎时写 `protocol="rdma"` 或 `protocol="efa"`，引擎要做的第一件事就是"根据这个字符串，造出对应的那一个 Transport 对象"。这是一个经典的**工厂模式**：输入是字符串，输出是一个实现了统一接口的对象。

**直觉二：接口统一，元数据分化。** 所有协议都要回答同样三个问题——"怎么提交一批传输请求""怎么查完成状态""怎么把一段本地内存注册给硬件"。这构成了 `Transport` 基类的统一契约。但每种协议回答这三个问题的"材料"完全不同：RDMA 需要 `lkey/rkey`、NVMe-oF 需要 `cufile` 句柄、NVLink 需要 CUDA 流。这些"协议专属材料"就是每个 Transport 各自维护的元数据。

**直觉三：能被编译进来 ≠ 能被选中。** 一种协议要可用，要同时满足两层条件：(1) **编译期**——用对应的 `-DUSE_xxx=ON` 把它的源码编进二进制；(2) **运行期**——机器上真的有对应硬件和驱动。`supported-protocols.md` 这份文档就是在帮用户理清"每种协议需要什么硬件、什么编译开关、怎么探测设备"。

**直觉四：协议内部的"角色分化"。** 有些协议不止一种用法。昇腾 `ascend` 在同一个进程里常常被实例化成两个 TE：一个负责 KV cache 远端读写（走 RoCE），一个负责 NPU 间直连（走 HCCS）。它们共享同一个进程级环境变量 `ASCEND_GLOBAL_RESOURCE_CONFIG`，却要解析出**不同**的链路配置。这靠的不是新增协议字符串，而是一个"瞬时角色标志位"在配置解析时分流——本讲 4.7 会专门讲这个机制。

**直觉五：同一份 device 原语 API，两种 GPU 实现。** mooncake-ep 在 GPU 上做通信时定义了一套内存序原语（`mc_ld_acquire` / `mc_st_release` / `mc_fence`）。这套 API 在 NVIDIA CUDA 与摩尔线程 MUSA 上**函数名完全相同，实现却截然不同**——核心差异就在"跨 GPU 内存可见性是否需要显式 fence"。本讲 4.8 会专门讲。

如果你还不清楚 `Transport` / `MultiTransport` 的分层关系，建议先看 `u2-l4`（multi-transport）和 `u3-l1`（Transport 基类）。本讲聚焦"协议矩阵、统一接口、协议工厂与编译开关"，并深入两个代表性差异点（Ascend per-role 配置、MUSA fence 原语）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `mooncake-transfer-engine/include/transport/transport.h` | **`Transport` 抽象基类**。定义所有协议共享的虚函数契约，以及 `Slice` 内部那个承载各协议元数据的 `union`。统一接口的主角。 |
| `mooncake-transfer-engine/src/multi_transport.cpp` | `MultiTransport::installTransport`——**协议工厂**：把 `"rdma"` / `"nvmeof"` 等字符串 `new` 成对应的 Transport 对象，每个分支都被 `#ifdef USE_*` 包裹。 |
| `mooncake-transfer-engine/include/multi_transport.h` | `MultiTransport` 的声明，持有 `transport_map_`（字符串→Transport 的注册表）。 |
| `mooncake-transfer-engine/src/transport/CMakeLists.txt` | **编译开关总表**：`if(USE_NVMEOF) add_subdirectory(...)` 决定哪些协议的源码被编入。 |
| `mooncake-common/common.cmake` | **`option()` 定义表**：`USE_NVMEOF` / `USE_EFA` / `USE_CXL` 等 CMake 选项的默认值（多数默认 OFF）。 |
| `docs/source/getting_started/supported-protocols.md` | **协议支持文档**。逐个协议说明所需硬件、编译开关、配置示例与设备探测命令。本讲规格钦定的阅读对象。 |
| `mooncake-transfer-engine/src/transport/nvmeof_transport/nvmeof_transport.cpp` | NVMe-oF 传输实现。本讲用作"统一接口下差异化实现"的精读案例（`submitTransfer` / `registerLocalMemory`）。 |
| `mooncake-transfer-engine/src/transport/nvlink_transport/nvlink_transport.cpp` | NVLink 传输实现。案例的备选（用 CUDA `cudaMemcpy` 实现）。 |
| `mooncake-transfer-engine/src/transport/ascend_transport/ascend_direct_transport/ascend_direct_transport.cpp` | **Ascend Direct 传输实现**。`allocateLocalSegmentID` 里用 `IsRoceModeEnabled()` + `ascend_store_te_init` 决定本 TE 走 RoCE 还是 HCCS、是否用 fabric mem。4.7 的主角之一。 |
| `mooncake-transfer-engine/src/transport/ascend_transport/ascend_direct_transport/utils.cpp` | **Ascend 角色配置解析**。`ResolveAscendGlobalResourceConfig`（按角色取/弃 `store` 子键）与 `IsRoceModeEnabled`（判定 RoCE 链路）。4.7 的主角之二。 |
| `mooncake-transfer-engine/include/transport/ascend_transport/ascend_direct_transport/utils.h` | 上述两个函数的声明与**配置 schema 文档注释**（讲清"store 子键是 Store TE 的专用覆盖"）。 |
| `mooncake-transfer-engine/include/config.h` | `GlobalConfig` 结构体。新增的瞬时标志 `ascend_store_te_init`（+ `ascend_use_fabric_mem` / `ascend_agent_mode`）。 |
| `mooncake-store/src/client_service.cpp` | Store 入口 `Client::InitTransferEngine`。在这里用 RAII 守卫在安装 ascend transport 前后置位/复位 `ascend_store_te_init`。 |
| `mooncake-transfer-engine/include/transport/device/device_ops.cuh` | **device 内存序原语的分发头**：按 `MOONCAKE_EP_USE_MUSA` 宏二选一 include CUDA 或 MUSA 实现。4.8 的入口。 |
| `mooncake-transfer-engine/include/transport/device/cuda/cuda_ops.cuh` | **CUDA 版 device 原语**：PTX `ld.acquire.sys`/`st.release.sys`，`mc_fence()` 为空操作。 |
| `mooncake-transfer-engine/include/transport/device/musa/musa_ops.cuh` | **MUSA 版 device 原语**：普通 load/store + `__threadfence_system()`，`mc_fence()` 即 `__threadfence_system()`。4.8 的主角。 |
| `mooncake-transfer-engine/src/transport/device/` | **device 子目录**：GPU 直连（GPUDirect）相关的设备级传输适配（`p2p_device_transport`、`ibgda_device_transport`、`mlx5gda`），仅在 `USE_CUDA`/`USE_MUSA` 时编译。 |

## 4. 核心概念与源码讲解

我们按"先看全貌、再看契约、再看如何装配、再精读一个实例、最后深入两个差异点"的顺序，拆成八个最小模块：

- 4.1 异构 transport 一览：协议矩阵
- 4.2 Transport 统一接口：一份契约，N 种实现
- 4.3 协议工厂与编译开关：`installTransport` 与 `USE_*`
- 4.4 精读案例：NVMe-oF 如何实现 `submitTransfer` 与 `registerLocalMemory`
- 4.5 device 子目录：GPU 直连加速器
- 4.6 协议支持文档与设备探测
- 4.7 Ascend per-role 配置：同进程共存 TE 的角色分流
- 4.8 MUSA fence 原语：跨 GPU 内存可见性的实现差异

---

### 4.1 异构 transport 一览：协议矩阵

#### 4.1.1 概念说明

Mooncake 不是"RDMA 传输引擎"，而是"可插拔多协议传输引擎"。`mooncake-transfer-engine/src/transport/` 目录下，每种协议各占一个子目录，各自实现一套 `Transport`。下表把仓库**实际支持的全部协议**汇总在一起——它比 `supported-protocols.md` 的文档列表更全（文档主要覆盖常用协议，源码里还有 `ub`/`maca`/`sunrise_link`/`ubshmem` 等面向特定厂商的实现）。

#### 4.1.2 协议矩阵

| 协议字符串 | C++ 类 | 编译开关（默认） | 源码目录 | 运行时/硬件要求 | 适用场景 |
| --- | --- | --- | --- | --- | --- |
| `tcp` | `TcpTransport` | `USE_TCP`（**默认 ON**） | `tcp_transport/` | 标准网卡 | 通用、随处可用、开发测试 |
| `rdma` | `RdmaTransport` | 始终编译 | `rdma_transport/` | InfiniBand / RoCE / eRDMA HCA | 生产低时延、GPUDirect RDMA、多网卡聚合 |
| `efa` | `EfaTransport` | `USE_EFA`（OFF） | `efa_transport/` | AWS EFA 实例 + libfabric | AWS 云（p5e/p6-b200/p4d 等） |
| `nvmeof` | `NVMeoFTransport` | `USE_NVMEOF`（OFF） | `nvmeof_transport/` | NVMe-oF 存储 + GPUDirect Storage（cuFile） | NVMe 与 DRAM/VRAM 零拷贝、分层存储 |
| `nvlink` | `NvlinkTransport` | `USE_MNNVL`（OFF） | `nvlink_transport/` | NVIDIA MNNVL 硬件 | 跨节点 GPU↔GPU（Multi-Node NVLink） |
| `nvlink_intra` | `IntraNodeNvlinkTransport` | `USE_INTRA_NVLINK`（OFF） | `intranode_nvlink_transport/` | NVIDIA NVLink | 节点内 GPU↔GPU |
| `hip` | `HipTransport` | `USE_HIP`（OFF） | `hip_transport/` | AMD ROCm/HIP 运行时 + AMD GPU | AMD GPU 通信（IPC/Shareable handle） |
| `barex` | `BarexTransport` | `USE_BAREX`（OFF） | `barex_transport/` | RDMA 网卡（accl-barex） | 裸金属 RDMA 扩展（如 soe/solar 网卡） |
| `cxl` | `CxlTransport` | `USE_CXL`（OFF） | `cxl_transport/` | CXL 硬件 | CXL 内存池化/ disaggregation |
| `ascend` | `AscendDirectTransport` / `HcclTransport` / `HeterogeneousRdmaTransport` | `USE_ASCEND_DIRECT` / `USE_ASCEND` / `USE_ASCEND_HETEROGENEOUS`（均 OFF） | `ascend_transport/`（含 3 个子实现） | 华为昇腾 NPU + HCCL 运行时 | 昇腾 NPU 分布式推理（HCCL / 直接 ADXL / 异构 RDMA） |
| `ub` | `UbTransport` | `USE_UB`（OFF） | `kunpeng_transport/` | 鲲鹏 UB（含 urma/URMA） | 鲲鹏 Ultra-Bus 传输 |
| `maca` | `MacaTransport` | `USE_MACA`（OFF） | `maca_transport/` | MUXI GPU（MACA 运行时） | 摩尔线程系 MUXI GPU |
| `sunrise_link` | `SunriseLinkTransport` | `USE_SUNRISE`（OFF） | `sunrise_link/` | Sunrise GPU（Tang 运行时） | Sunrise GPU 互连 |
| `ubshmem` | `UBShmemTransport` | `USE_UBSHMEM`（OFF） | `ascend_transport/ubshmem_transport/` | 昇腾 NPU | 昇腾共享内存传输 |

> **两个要点**：
>
> 1. `rdma` 是**始终编译**的（见 4.3），`tcp` 默认 ON，其余全部默认 OFF——也就是说开箱即用的引擎只有 RDMA + TCP 两种协议。要用 NVMe-oF / EFA / CXL / Ascend 等，必须重新用对应 `-DUSE_xxx=ON` 编译。
> 2. `ascend` 这一个协议字符串背后对应**三个互斥的 C++ 实现**，由三个不同的编译开关二选一/三选一决定（见 4.3）。这是协议矩阵里最特殊的一行。

除了"可选协议字符串"，还有一批**GPU 厂商开关**不新增协议、而是改变 `rdma` 的数据通路：

- `USE_CUDA`（NVIDIA）、`USE_HIP`（AMD）、`USE_MUSA`（摩尔线程 MTHREADS）、`USE_MACA`（MUXI）、`USE_HYGON`（海光 DCU）、`USE_COREX`（天数智芯 Iluvatar）、`USE_MLU`（寒武纪 MLU）。

例如寒武纪 MLU 走的就是标准 `rdma` 数据通路（没有单独的 `mlu` 协议字符串），只需用 `-DUSE_MLU=ON` 开启 MLU 内存的拓扑发现与 DMA-BUF 注册。而摩尔线程 MUSA 在 `rdma` 数据通路之外，还为 mooncake-ep 提供了一整套 device 原语（见 4.8）。

#### 4.1.3 源码精读

"源码目录"这一列不是猜的，`transport/CMakeLists.txt` 把每个开关和子目录的对应关系写得清清楚楚：

[`mooncake-transfer-engine/src/transport/CMakeLists.txt:23-86`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transport/CMakeLists.txt#L23-L86) — 每个协议子目录都被 `if(USE_xxx)` 条件编译；最后 `USE_CUDA OR USE_MUSA` 触发 device 子目录并链接 libmlx5。

```cmake
if (USE_NVMEOF)
  add_subdirectory(nvmeof_transport)
  target_sources(transport PUBLIC $<TARGET_OBJECTS:nvmeof_transport>)
endif()
...
if (USE_EFA)
  add_subdirectory(efa_transport)
  target_sources(transport PUBLIC $<TARGET_OBJECTS:efa_transport>)
  target_link_libraries(transport PRIVATE fabric)   # EFA 额外链 libfabric
endif()
```

注意 EFA 那行多了 `target_link_libraries(transport PRIVATE fabric)`——这是"运行时依赖"在构建系统里的直接体现：用 EFA 就必须链接 libfabric。类似地，各厂商开关会引入对应的 CUDA/HIP/MUSA 头文件与库。

这些 `USE_*` 选项的定义（含默认值）集中在：

[`mooncake-common/common.cmake:71-89`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-common/common.cmake#L71-L89) — 各协议的 `option()` 定义。

```cmake
option(USE_CUDA "option for enabling gpu features for NVIDIA GPU" OFF)
option(USE_MUSA "option for enabling gpu features for MTHREADS GPU" OFF)
option(USE_NVMEOF "option for using NVMe over Fabric" OFF)
option(USE_TCP "option for using TCP transport" ON)
option(USE_ASCEND "option for using npu with HCCL" OFF)
option(USE_ASCEND_DIRECT "option for using ascend npu with adxl engine" OFF)
option(USE_MNNVL "option for using Multi-Node NVLink transport" OFF)
option(USE_CXL "option for using CXL protocol" OFF)
option(USE_EFA "option for using AWS EFA transport" OFF)
```

#### 4.1.4 代码实践

**实践目标**：核对"协议矩阵表"与源码完全一致，体会"文档列表 vs 实际编译列表"的差异。

**操作步骤**：

1. 打开 `transport/CMakeLists.txt`，数一下 `if(USE_xxx) add_subdirectory(...)` 一共有多少个，对照本表逐行验证。
2. 打开 `common.cmake` 第 71–89 行，确认每个开关的默认值（哪些 ON、哪些 OFF）。
3. 对照 `supported-protocols.md` 第 7–18 行的 Quick Reference 表，标出"文档列了但本表也有"的协议，以及"本表有但文档没列"的协议（`ub`/`maca`/`sunrise_link`/`ubshmem`）。

**需要观察的现象**：CMake 里的条件编译分支数 ≥ 14；其中默认 ON 的只有 `USE_TCP`，`rdma` 无条件编译；文档表覆盖了 10 种常用协议，但仓库实际还多出几种面向特定厂商的实现。

**预期结果**：你会理解"协议矩阵"的权威来源是构建系统（`CMakeLists.txt` + `common.cmake`）和工厂函数（`installTransport`），而 `supported-protocols.md` 是面向用户的"精选子集 + 使用说明"。

> 待本地验证：具体分支数以本地 `grep` 结果为准，可用 `rg "add_subdirectory" mooncake-transfer-engine/src/transport/CMakeLists.txt`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `rdma` 不在"默认 OFF"的协议列表里，也不需要 `if(USE_RDMA)` 包裹？

**参考答案**：因为 `rdma_transport` 是引擎的基础传输，在 `transport/CMakeLists.txt:3` 里被**无条件** `add_subdirectory` 并编入 `transport` 目标；它没有对应的 `USE_RDMA` 开关，永远可用。这也呼应了 `supported-protocols.md` 的"推荐生产使用 RDMA"定位。

**练习 2**：寒武纪 MLU 没有 `mlu` 协议字符串，怎么用？

**参考答案**：MLU 复用标准 `rdma` 数据通路，只是用 `-DUSE_MLU=ON` 开启 MLU 内存的拓扑发现与 DMA-BUF 注册。也就是说协议字符串仍写 `rdma`，差异只体现在"内存如何被注册成 RDMA MR"这一内部细节上，对上层接口透明。

---

### 4.2 Transport 统一接口：一份契约，N 种实现

#### 4.2.1 概念说明

14 种协议、3 种昇腾变体，之所以能被同一个 `MultiTransport` 统一调度，是因为它们全部继承自同一个抽象基类 `Transport`，并实现同一组虚函数。这就是"统一接口"。

这组接口回答了任何一种传输都必须回答的几个问题：

1. **你叫什么？** —— `getName()` 返回协议字符串（如 `"nvmeof"`），用于注册和选路。
2. **怎么提交一批请求？** —— `submitTransfer(batch_id, entries)`，把用户意图下发到硬件。
3. **怎么查完成状态？** —— `getTransferStatus(batch_id, task_id, status)`。
4. **怎么把本地内存注册给硬件？** —— `registerLocalMemory(addr, length, ...)` / `unregisterLocalMemory(...)` / 批量版本。
5. **怎么初始化？** —— `install(...)`，绑定 server 名、metadata、拓扑。

基类把这 5 类里"协议相关"的部分设为**纯虚函数**（`= 0`），强制每个子类各自实现；而"协议无关"的部分（如 `allocateBatchID`、`submitTransferTask` 的默认实现）由基类兜底。这正是多态的标准用法——**统一调用方代码，分化被调方实现**。

#### 4.2.2 核心流程

上层使用任意一种协议的生命周期都是同一个模板（与 `u3-l1` 一致）：

```
MultiTransport::installTransport("nvmeof", topo)   # 工厂造出 NVMeoFTransport
   ↓
transport->install(name, meta, topo)               # 初始化协议资源
   ↓
transport->registerLocalMemory(buf, len, ...)       # 把内存注册给 cuFile
   ↓
batch_id = transport->allocateBatchID(N)            # 分配 batch
transport->submitTransfer(batch_id, reqs)           # 下发请求
transport->getTransferStatus(batch_id, i, st)       # 轮询完成
transport->freeBatchID(batch_id)                    # 释放
```

无论是 NVMe-oF 还是 NVLink，调用方写的都是上面这套代码——区别只在"第 1 步传的字符串"和"每一步内部用了什么协议资源"。这就是统一接口的价值。

**原理补充：为什么用"虚函数 + union"而不是"深类层次"？**

Mooncake 的差异化实现有两层：

- **行为差异**（submitTransfer 怎么下发）用**虚函数**解决——每种协议一个子类，重写同名方法；
- **数据差异**（一个 Slice 需要哪些协议字段）用 **`Slice` 内部的 `union`** 解决——把所有协议的元数据叠在同一块内存。

这种"虚函数处理行为 + tagged-union 处理数据"的组合，避免了为每种协议定义一条平行的类继承树（那样会有 14 套几乎重复的 Batch/Task 管理）。`Slice` 这个 union 没有显式的 `kind` 标签——因为一个 Slice 自创建起就明确归属某一个 Transport（见 `u3-l1` 4.2.5），协议信息由"谁创建它"隐式确定，不需要在每个对象里冗余存储。

#### 4.2.3 源码精读

基类的纯虚契约集中在两段。**对外接口**：

[`mooncake-transfer-engine/include/transport/transport.h:363-378`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/transport/transport.h#L363-L378) — `submitTransfer` 与 `getTransferStatus` 是纯虚函数。

```cpp
virtual Status submitTransfer(
    BatchID batch_id, const std::vector<TransferRequest> &entries) = 0;

virtual Status getTransferStatus(BatchID batch_id, size_t task_id,
                                 TransferStatus &status) = 0;
```

**内存注册接口**（私有纯虚，由 `TransferEngine` 这个 friend 调用）：

[`mooncake-transfer-engine/include/transport/transport.h:405-420`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/transport/transport.h#L405-L420) — `registerLocalMemory` 系列与 `getName` 都是纯虚。

```cpp
virtual int registerLocalMemory(void *addr, size_t length,
                                const std::string &location,
                                bool remote_accessible,
                                bool update_metadata = true) = 0;
virtual int unregisterLocalMemory(void *addr, bool update_metadata = true) = 0;
virtual int registerLocalMemoryBatch(...) = 0;
virtual int unregisterLocalMemoryBatch(...) = 0;
virtual const char *getName() const = 0;
```

注意 `getName()`——这是每个协议的"自报家门"。例如 NVMe-oF 的实现：

[`mooncake-transfer-engine/include/transport/nvmeof_transport/nvmeof_transport.h:101`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/transport/nvmeof_transport/nvmeof_transport.h#L101) — `getName()` 返回 `"nvmeof"`。

```cpp
const char *getName() const override { return "nvmeof"; }
```

而承载"数据差异"的 union，把每种协议需要的字段并列出来——这就是"统一外壳 + 多协议内核"的落点（`u3-l1` 4.2 已详述）：

[`mooncake-transfer-engine/include/transport/transport.h:121-172`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/transport/transport.h#L121-L172) — `Slice` 的 `union`，并列 `rdma` / `ub` / `local` / `tcp` / `nvmeof` / `cxl` / `hccl` / `ascend_direct` / `ubshmem` 等分支。

```cpp
union {
    struct { uint64_t dest_addr; uint32_t source_lkey, dest_rkey;
             volatile int *qp_depth; RdmaEndPoint *endpoint; ... } rdma;
    struct { void *dest_addr; } tcp;
    struct { uint64_t offset; int cufile_desc; uint64_t start;
             const char *file_path; } nvmeof;
    struct { uint64_t dest_addr; void *handle; int64_t start_time;
             int32_t engine_id; } ascend_direct;
    struct { void *dest_addr; } cxl;
    ...
};
```

可以看到：TCP 只需要一个远端地址；RDMA 需要 lkey/rkey/队列深度指针/endpoint；NVMe-oF 需要 cufile 描述符与文件路径；昇腾直接传输需要 handle/engine_id。**字段多寡直接反映协议复杂度**——这正是"各自维护协议元数据"的含义。

#### 4.2.4 代码实践

**实践目标**：用统一接口的视角，对比两种协议实现 `getName` 与 `registerLocalMemory` 的差异。

**操作步骤**：

1. 打开 `nvmeof_transport.h:101` 与 `nvmeof_transport.cpp:225-233`，看 NVMe-oF 的 `getName()` 和 `registerLocalMemory()`（后者调 `cuFileBufRegister`）。
2. 打开 `nvlink_transport.cpp:306` 起的 `registerLocalMemory`，看 NVLink 如何用 `cudaPointerGetAttributes` 探测内存类型。
3. 对比两者：同一个函数签名，内部调用的却是完全不同的运行时 API（cuFile vs CUDA）。

**需要观察的现象**：两个 `registerLocalMemory` 的函数签名一字不差（都来自基类纯虚），但函数体里出现的库函数完全不同——NVMe-oF 是 `cuFile*`，NVLink 是 `cuda*`。

**预期结果**：你会直观理解"统一接口、差异化实现"——上层调用的都是 `transport->registerLocalMemory(...)`，多态在运行期把它分发到正确的协议实现。

#### 4.2.5 小练习与答案

**练习 1**：`submitTransfer` 是纯虚函数，但基类还提供了 `submitTransferTask` 的默认实现（返回 `NotImplemented`）。为什么这两者要分开？

**参考答案**：`submitTransfer` 是"接收原始 `TransferRequest` 并自己切分"的入口（各协议直接实现）；而 `submitTransferTask` 是"接收已经切好的 `TransferTask` 列表"的入口，是 `MultiTransport::submitTransfer` 按 transport 分组后**实际调用**的方法（见 `u3-l1` 4.4）。两者分开，让协议可以选择"自己切分"或"复用统一切分逻辑"，提供了灵活性。

**练习 2**：`registerLocalMemory` 等内存注册函数为什么是 `private` 的纯虚函数，而不是 `public`？

**参考答案**：因为内存注册是与 segment 生命周期强绑定的管理操作，应当由 `TransferEngine`（`Transport` 的 `friend`）在注册/注销 segment 时统一驱动，而不是让任意调用方随时对任意地址调用。把接口设为 `private` + `friend`，既保留了多态分发，又收拢了调用入口，避免内存注册状态被外部打乱。

---

### 4.3 协议工厂与编译开关：`installTransport` 与 `USE_*`

#### 4.3.1 概念说明

矩阵表里的"协议字符串"是怎么变成一个活生生的 Transport 对象的？答案是工厂函数 `MultiTransport::installTransport(proto, topo)`：它是一长串 `if (proto == "xxx") transport = new XxxTransport();`，每个分支再用 `#ifdef USE_xxx` 包起来。

这个设计有两层筛选，正好对应 4.1.3 提到的"能编译 ≠ 能选中"：

1. **编译期筛选**：`#ifdef USE_xxx`——如果没开对应开关，这个 `else if` 分支根本不存在于二进制里，传这个字符串会直接得到 `nullptr`。
2. **运行期匹配**：`if (proto == "xxx")`——即便编进来了，也要用户传对字符串才会被 `new` 出来。

两层都通过后，对象才被造出来、`install` 进 `transport_map_`，供后续 `getTransport(proto)` 取用。

#### 4.3.2 核心流程

```
installTransport("nvmeof", topo):
    transport = nullptr
    if (proto == "rdma")        transport = new RdmaTransport();     // 无条件
    #ifdef USE_NVMEOF
    else if (proto == "nvmeof") transport = new NVMeoFTransport();   // 需编译开关
    #endif
    ...（其余每个协议一个 #ifdef 分支）...
    if (!transport) { LOG(ERROR) "Unsupported transport"; return nullptr; }
    transport->install(name, meta, topo);    // 初始化协议资源
    transport_map_[proto] = transport;       // 注册进 map，供 getTransport 取用
    return transport;
```

关键在于：**`#ifdef` 决定分支是否存在**，**`if` 决定分支是否命中**。未开启的协议，连"不命中"的机会都没有——它在预处理阶段就被删掉了。

#### 4.3.3 源码精读

工厂函数完整结构（节选关键分支）：

[`mooncake-transfer-engine/src/multi_transport.cpp:309-396`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/multi_transport.cpp#L309-L396) — `installTransport`：协议字符串 → Transport 实例，每个分支被 `#ifdef USE_*` 包裹。

```cpp
Transport* MultiTransport::installTransport(const std::string& proto,
                                            std::shared_ptr<Topology> topo) {
    Transport* transport = nullptr;
    if (std::string(proto) == "rdma") {            // ← 无 #ifdef，始终可用
        transport = new RdmaTransport();
    }
#ifdef USE_UB
    else if (std::string(proto) == "ub") { transport = new UbTransport(); }
#endif
#ifdef USE_NVMEOF
    else if (std::string(proto) == "nvmeof") { transport = new NVMeoFTransport(); }
#endif
#ifdef USE_ASCEND_DIRECT
    else if (std::string(proto) == "ascend") { transport = new AscendDirectTransport(); }
#endif
#ifdef USE_ASCEND
    else if (std::string(proto) == "ascend") { transport = new HcclTransport(); }
#endif
#ifdef USE_ASCEND_HETEROGENEOUS
    else if (std::string(proto) == "ascend") { transport = new HeterogeneousRdmaTransport(); }
#endif
    ... // nvlink / nvlink_intra / hip / cxl / efa / barex / maca / sunrise_link / ubshmem ...
    if (!transport) {
        LOG(ERROR) << "Unsupported transport " << proto << ", please rebuild Mooncake";
        return nullptr;
    }
    ...
}
```

三个值得注意的细节：

1. **`rdma` 分支没有 `#ifdef`**——印证了 4.1.5 练习 1 的结论：RDMA 始终可用。
2. **`ascend` 字符串对应三个互斥分支**——`USE_ASCEND_DIRECT` / `USE_ASCEND` / `USE_ASCEND_HETEROGENEOUS` 三个开关各自把同一个 `"ascend"` 字符串绑到不同的 C++ 类。因为它们是 `else if` 链，实际命中的是"在编译期被启用、且排在最前面"的那一个。所以协议矩阵里 `ascend` 那一行有三个类，由编译开关三选一决定。
3. **失败提示是 `please rebuild Mooncake`**——明确告诉用户："不是运行时找不到，而是你编译时没开这个开关"。这是把"编译期筛选"的真相直接抛给用户。

`barex` 分支后面还有一段额外的 NIC 过滤逻辑（只挑 `soe`/`solar` 网卡），见 `multi_transport.cpp:402-413`，说明某些协议在工厂里还会做**拓扑相关的裁剪**，而不只是单纯 `new` 一个对象。

#### 4.3.4 代码实践

**实践目标**：用工厂函数验证"编译开关决定协议可用性"。

**操作步骤**：

1. 在 `multi_transport.cpp:309-396` 里，统计每个 `else if` 分支前是否有 `#ifdef`，把"有 `#ifdef` 的协议"和"无 `#ifdef` 的协议"分成两组。
2. 对"有 `#ifdef` 的协议"，回到 `common.cmake:71-89` 找到它对应的 `option` 名称和默认值。
3. 思考：如果用户用默认配置编译（什么开关都不加），然后调 `installTransport("nvmeof", ...)`，会发生什么？

**需要观察的现象**：只有 `rdma` 分支没有 `#ifdef`；其余每个分支都成对出现 `#ifdef USE_xxx ... #endif`。默认配置下 `USE_NVMEOF=OFF`，所以 `"nvmeof"` 分支被预处理删除，调用会落到 `if (!transport)` 分支，打印 "Unsupported transport nvmeof, please rebuild Mooncake" 并返回 `nullptr`。

**预期结果**：你会建立"协议可用 = 编译开关 ON + 运行时有硬件 + 传对字符串"的完整心智模型。

> 待本地验证：可在默认配置构建后，写一个最小 C 程序调 `multi_transport_install_transport` 传 `"nvmeof"`，观察是否返回 `nullptr` 与对应错误日志。

#### 4.3.5 小练习与答案

**练习 1**：`ascend` 字符串有三个 `else if` 分支，为什么不会"三个都命中"？

**参考答案**：因为它们是 `if/else if` 链，命中第一个匹配后就 `break` 出整个判断（C++ 的 `else if` 语义）。又因为三个分支用的是同一个字符串 `"ascend"`，真正能进入哪个分支，取决于**编译期**哪些 `USE_ASCEND_*` 宏被定义——只有被 `#ifdef` 保留下来、且在链中位置最靠前的那个分支会被编译进二进制。所以运行期只会命中一个。

**练习 2**：为什么工厂用 `if (std::string(proto) == "rdma")` 而不是 `if (proto == "rdma")`？

**参考答案**：`proto` 的类型是 `const std::string&`，直接 `proto == "rdma"` 本来就能工作（`std::string` 重载了 `== const char*`）。这里显式构造 `std::string(proto)` 更多是写法习惯/历史遗留，在功能上等价。这是一个不影响行为的细节，不必深究。

---

### 4.4 精读案例：NVMe-oF 如何实现 `submitTransfer` 与 `registerLocalMemory`

#### 4.4.1 概念说明

光讲矩阵和接口太抽象。本节用一个完整案例——**NVMe-oF 传输**——展示"统一接口下的差异化实现"到底长什么样。NVMe-oF（NVMe over Fabrics）借助 NVIDIA 的 GPUDirect Storage（cuFile 库），让 NVMe 存储与 DRAM/VRAM 之间直接 DMA，绕过 CPU，实现零拷贝。

它同样实现了 `u3-l1` 讲的那套 `Transport` 契约，但"材料"完全不同：

- **注册内存**：不是注册成 RDMA MR，而是调 `cuFileBufRegister`，让 cuFile 知道这段缓冲区可被存储直接 DMA；
- **提交传输**：不是发 RDMA Work Request，而是把每次读写描述成一条 `CUfileIOParams_t`，攒进 cuFile 的批量描述符池，最后 `submitBatch`；
- **查完成**：不是读 RDMA 完成队列，而是问 `CUFileDescPool` 每个 slice 的 cuFile 事件状态。

#### 4.4.2 核心流程

NVMe-oF 的 `submitTransfer` 流程（把目标 segment 当成"一串 NVMe 文件 buffer"）：

```
submitTransfer(batch_id, entries):
    取出 batch 的 NVMeoFBatchDesc（含 cuFile 描述符池下标 desc_idx_）
    for 每个 request:
        通过 metadata 取目标 segment 的 SegmentDesc（断言其 protocol == "nvmeof"）
        for 该 segment 的每个 nvmeof buffer（按 offset 顺序排布）:
            if request 与该 buffer 区间有重叠:
                计算重叠切片 (slice_start, slice_len, file_offset)
                addSliceToTask(...)            # 建一个 Slice，记 file_path/start
                取/建该文件的 CuFileContext → CUfileHandle_t fh
                addSliceToCUFileBatch(...)      # 把切片描述成 CUfileIOParams_t 入池
    desc_pool_->submitBatch(desc_idx_)          # 一次性提交给 cuFile
```

关键差异在于：**目标不是一段连续远端内存，而是一组按 offset 拼接的 NVMe 文件**。所以切分逻辑是"用 request 的 `[target_offset, target_offset+length)` 去和每个 buffer 的区间求交集"，凡是重叠的部分各成一个 slice——这是 NVMe-oF 特有的"按文件 buffer 切片"逻辑。

内存注册则极简：

```
registerLocalMemory(addr, length, ...):
    cuFileBufRegister(addr, length, 0)      # 注册给 cuFile
unregisterLocalMemory(addr, ...):
    cuFileBufDeregister(addr)               # 注销
```

#### 4.4.3 源码精读

**内存注册**——一行 cuFile 调用：

[`mooncake-transfer-engine/src/transport/nvmeof_transport/nvmeof_transport.cpp:225-239`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transport/nvmeof_transport/nvmeof_transport.cpp#L225-L239) — `registerLocalMemory` / `unregisterLocalMemory` 直接调用 cuFile。

```cpp
int NVMeoFTransport::registerLocalMemory(void *addr, size_t length,
                                         const std::string &location,
                                         bool remote_accessible, bool update_metadata) {
    (void)remote_accessible; (void)update_metadata;
    CUFILE_CHECK(cuFileBufRegister(addr, length, 0));   // ← 注册给 GPUDirect Storage
    return 0;
}

int NVMeoFTransport::unregisterLocalMemory(void *addr, bool update_metadata) {
    (void)update_metadata;
    CUFILE_CHECK(cuFileBufDeregister(addr));
    return 0;
}
```

对比 NVLink 的同名函数（用 CUDA 探测内存类型，见 `nvlink_transport.cpp:306-318`）——**同样的函数签名，截然不同的运行时 API**。这就是 4.2 讲的"统一接口、差异化实现"。

**提交传输**——先断言目标协议确实是 nvmeof，再按文件 buffer 切片：

[`mooncake-transfer-engine/src/transport/nvmeof_transport/nvmeof_transport.cpp:113-204`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transport/nvmeof_transport/nvmeof_transport.cpp#L113-L204) — `submitTransfer`：按目标 segment 的 `nvmeof_buffers` 切片，逐片转成 cuFile 批量请求。

```cpp
Status NVMeoFTransport::submitTransfer(BatchID batch_id,
                                       const std::vector<TransferRequest> &entries) {
    auto &batch_desc = *((BatchDesc *)(batch_id));
    auto &nvmeof_desc = *((NVMeoFBatchDesc *)(batch_desc.context));  // 私有上下文
    ...
    for (auto &request : entries) {
        auto &desc = segment_desc_map.at(request.target_id);
        assert(desc->protocol == "nvmeof");          // ← 自检目标协议
        uint64_t segment_start = request.target_offset;
        uint64_t current_offset = 0;
        for (auto &buffer_desc : desc->nvmeof_buffers) {        // 逐个文件 buffer
            if (overlap(segment_start, request.length, current_offset, buffer_desc.length)) {
                // 计算重叠切片
                uint64_t slice_start = std::max(segment_start, current_offset);
                uint64_t slice_end   = std::min(segment_end, current_offset + buffer_desc.length);
                const char *file_path = buffer_desc.local_path_map[local_server_name_].c_str();
                addSliceToTask(source_addr, slice_len, file_offset, request.opcode, task, file_path);
                // 取/建该文件的 cuFile 句柄
                auto buf_key = std::make_pair(target_id, buffer_id);
                if (!segment_to_context_.count(buf_key))
                    segment_to_context_[buf_key] = std::make_shared<CuFileContext>(file_path);
                CUfileHandle_t fh = segment_to_context_.at(buf_key)->getHandle();
                addSliceToCUFileBatch(source_addr, file_offset, slice_len,
                                      nvmeof_desc.desc_idx_, request.opcode, fh);
            }
            current_offset += buffer_desc.length;
        }
    }
    desc_pool_->submitBatch(nvmeof_desc.desc_idx_);   // 一次性提交给 cuFile
    return Status::OK();
}
```

三个要点：

1. **`assert(desc->protocol == "nvmeof")`**（`nvmeof_transport.cpp:146`）——这是一种防御：如果某个 request 的目标 segment 声明的协议不是 nvmeof，却进了 NVMeoFTransport，说明选路出了问题，直接断言失败。
2. **私有上下文 `batch_desc.context`**（类型 `NVMeoFBatchDesc*`）——这是 `BatchDesc` 留给具体 transport 存私有数据的字段（`transport.h:332` 的 `void *context`）。NVMe-oF 用它存 cuFile 描述符池下标和 task→slice 映射。**每个协议往 `context` 里塞自己的东西**，这正是"各自维护协议元数据"的又一体现。
3. **`addSliceToCUFileBatch`** 把切片翻译成 cuFile 的 `CUfileIOParams_t`（`CUFILE_BATCH` 模式），统一攒进 `desc_pool_`，最后 `submitBatch` 一次下发。RDMA 是"切片→Work Request"；NVMe-oF 是"切片→cuFile IO Params"——殊途同归。

**完成查询**也走 cuFile 自己的事件机制：

[`mooncake-transfer-engine/src/transport/nvmeof_transport/nvmeof_transport.cpp:77-104`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transport/nvmeof_transport/nvmeof_transport.cpp#L77-L104) — `getTransferStatus`：从 `CUFileDescPool` 取每个 slice 的 cuFile 事件状态，映射回 `Transport::TransferStatusEnum`。

它用 `from_cufile_transfer_status`（`nvmeof_transport.cpp:44`）把 cuFile 自己的状态枚举（`CUFILE_COMPLETE` 等）翻译回 `Transport` 基类定义的统一七态枚举（`COMPLETED` 等）。**对外仍是统一的 `TransferStatus`，对内各用各的原生状态。**

#### 4.4.4 代码实践

**实践目标**：对照精读一个具体协议，验证"它确实完整实现了基类契约，且内部用协议原生 API"。

**操作步骤**（本讲规格指定的实践——选 NVMe-oF）：

1. 定位源码目录：`mooncake-transfer-engine/src/transport/nvmeof_transport/`，确认它由 `nvmeof_transport.cpp` + `cufile_context.cpp` + `cufile_desc_pool.cpp` 组成。
2. 读 `nvmeof_transport.cpp:225-233` 的 `registerLocalMemory`：它调的是 `cuFileBufRegister`（cuFile 原生 API），而非 `ibv_reg_mr`（RDMA）。
3. 读 `nvmeof_transport.cpp:113-204` 的 `submitTransfer`：它的"切分对象"是 `desc->nvmeof_buffers`（一组文件 buffer），切片结果通过 `addSliceToCUFileBatch` 转成 `CUfileIOParams_t`，最后 `submitBatch`。
4. 读 `nvmeof_transport.cpp:77-104` 的 `getTransferStatus`：状态来自 `CUFileDescPool`，再经 `from_cufile_transfer_status` 映射回统一枚举。
5. 在 `nvmeof_transport.h:101` 确认 `getName()` 返回 `"nvmeof"`——这是它被工厂（`installTransport`）和选路（`selectTransport`）识别的依据。

**需要观察的现象**：NVMe-oF 的四个核心方法（`getName`/`submitTransfer`/`getTransferStatus`/`registerLocalMemory`）全都 override 了基类的纯虚函数；函数体内出现的全是 `cuFile*` 系列调用，没有 `ibv_*`、没有 `cuda*`。

**预期结果**：你会得出结论——"实现 `Transport` 契约"=把这四五个虚函数填上协议原生 API；填好之后，它就能像 RDMA/TCP 一样被 `MultiTransport` 统一调度，对上层完全透明。

> 待本地验证：完整运行 NVMe-oF 需要 NVMe-oF 存储 + GPUDirect Storage 环境。若无该硬件，本实践为"源码阅读型实践"——按上述步骤读源码、对照 cuFile API 文档理解即可。

#### 4.4.5 小练习与答案

**练习 1**：NVMe-oF 的 `submitTransfer` 里，为什么要遍历 `desc->nvmeof_buffers` 求"重叠区间"，而不是像 RDMA 那样直接按固定 `slice_size` 切？

**参考答案**：因为 NVMe-oF 的目标不是一个连续内存段，而是"一串按 offset 拼接的 NVMe 文件"，每个文件各有自己的 `file_path` 和 `CUfileHandle_t`。request 的目标区间可能跨越多个文件，所以必须逐文件求交集，每个落在不同文件上的重叠段单独成一个 slice（携带各自的 file_path 与句柄）。这是 NVMe-oF 数据模型（多文件 buffer）决定的切分逻辑。

**练习 2**：`batch_desc.context` 这个 `void*` 字段，NVMe-oF 拿来存 `NVMeoFBatchDesc`。如果 RDMA 也想用它存私有数据，会冲突吗？

**参考答案**：不会。因为一个 `BatchDesc` 归属于**一个** Transport（由谁 `allocateBatchID` 决定），它只会被该协议的代码读写 `context`。不同协议的 batch 互不相干。`void *context`（`transport.h:332`）就是基类留给"每个协议往里塞私有上下文"的通用口子，由各协议自行解释其真实类型（NVMe-oF 解释成 `NVMeoFBatchDesc*`）。

---

### 4.5 device 子目录：GPU 直连加速器

#### 4.5.1 概念说明

`mooncake-transfer-engine/src/transport/device/` 是一个特殊的存在——它**不是**一个可被 `installTransport` 选中的独立协议字符串，而是为 GPU 直连（GPUDirect）提供"设备级"的加速适配。它包含：

- `p2p_device_transport.cpp`：P2P（peer-to-peer）设备传输，`P2pDeviceTransportImpl` 继承自 `P2pTransport`；
- `ibgda_device_transport.cpp` + `mlx5gda.cpp`：基于 mlx5 的 **IBGDA**（InfiniBand GPUDirect Async）加速路径——让 GPU 直接驱动网卡的 QP 生命周期，进一步降低 CPU 参与。

这块只在 `USE_CUDA` 或 `USE_MUSA` 时编译，属于"GPU 厂商开关改变 `rdma` 数据通路"的高级加速，而非新增协议。

> 注意：device 目录下还有一组 **device 内存序原语头**（`device_ops.cuh` + `cuda/cuda_ops.cuh` + `musa/musa_ops.cuh`），它们是 mooncake-ep 在 GPU kernel 内部用的，与传输层的 IBGDA/P2P 是两码事——本讲 4.8 会专门讲这组原语。

#### 4.5.2 核心流程

device 子目录与协议矩阵的关系：

```
普通 RDMA 数据通路：  CPU 驱动 ibverbs QP
   └─ 开启 GPUDirect RDMA（USE_CUDA）：GPU 显存可直接作为 MR，但仍由 CPU 驱动 QP

device/ 的 IBGDA 通路：GPU 直接驱动 mlx5 QP（mlx5gda.cpp 管理 QP 生命周期）
   └─ 进一步卸载 CPU，适合极高吞吐场景
```

也就是说，`device/` 是在 `rdma` 协议"之下"做硬件级加速，而不是并列的新协议。

#### 4.5.3 源码精读

device 的编译条件与组成清楚地写在它自己的 CMakeLists 里：

[`mooncake-transfer-engine/src/transport/device/CMakeLists.txt:8-13`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transport/device/CMakeLists.txt#L8-L13) — `p2p_device_transport.cpp` 总是编入；IBGDA/mlx5gda 仅在 CUDA/MUSA 时追加。

```cmake
set(DEVICE_TRANSPORT_SOURCES p2p_device_transport.cpp)
if(USE_CUDA OR USE_MUSA)
    list(APPEND DEVICE_TRANSPORT_SOURCES ibgda_device_transport.cpp mlx5gda.cpp)
endif()
add_library(device_transport OBJECT ${DEVICE_TRANSPORT_SOURCES})
```

它被上层 `transport/CMakeLists.txt` 在 `USE_CUDA OR USE_MUSA` 时纳入（`transport/CMakeLists.txt:81-86`），并额外链接 `mlx5`（因为 `mlx5gda.cpp` 直接调用 libmlx5 的 DevX 符号）：

[`mooncake-transfer-engine/src/transport/CMakeLists.txt:81-86`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transport/CMakeLists.txt#L81-L86) — device 子目录仅在 GPU 厂商开关开启时编译，并链接 libmlx5。

`P2pDeviceTransportImpl` 的类定义确认了它与 `P2pTransport` 的继承关系：

[`mooncake-transfer-engine/src/transport/device/p2p_device_transport.cpp:31`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transport/device/p2p_device_transport.cpp#L31) — `class P2pDeviceTransportImpl : public P2pTransport`。

#### 4.5.4 代码实践

**实践目标**：区分"独立协议子目录"与"加速器适配子目录 device/"。

**操作步骤**：

1. 在 `transport/` 下用 `ls -d */` 列出所有子目录，确认 `device/` 与其他协议目录（`nvmeof_transport/`、`efa_transport/` 等）并列。
2. 在 `multi_transport.cpp:309-396` 的 `installTransport` 里搜索 `"device"`——确认**没有**以 device 为名的协议分支。
3. 读 `transport/CMakeLists.txt:81-86`，确认 `device/` 是被 `USE_CUDA OR USE_MUSA` 触发，而非某个 `USE_DEVICE` 协议开关。

**需要观察的现象**：`installTransport` 里没有任何 `proto == "device"` 或 `proto == "ibgda"` 分支；`device/` 是被 GPU 厂商开关间接触发的辅助模块。

**预期结果**：你会明确——`device/` 不出现在协议矩阵表里，因为它是 `rdma` 数据通路的硬件加速层，而非可选协议。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `ibgda_device_transport.cpp` 和 `mlx5gda.cpp` 要被"直接编进 `device_transport` OBJECT 库"，而不是单独建静态库？

**参考答案**：见 `device/CMakeLists.txt:1-7` 的注释——为了让 `mlx5gda_*` 符号直接流入 `transfer_engine`，使那些用手写 ldflags 链接 `transfer_engine` 的消费者（如 Go 版 p2p-store / mooncake-store，它们绕过 CMake 的 `target_link_libraries` 传播）也能看到这些符号。如果建成独立静态库，这些符号可能不会被自动链接进去。

**练习 2**：`device/` 与 `nvlink_transport/` 都涉及 GPU，它们的定位有什么不同？

**参考答案**：`nvlink_transport/` 是一个**独立协议**（协议字符串 `"nvlink"`，由 `USE_MNNVL` 开关控制，经 `installTransport` 注册），解决的是 GPU↔GPU 的数据搬运；而 `device/` 不是独立协议，它是 `rdma` 数据通路的**硬件加速层**（IBGDA/mlx5gda），让 GPU 更直接地驱动网卡。前者是"新的传输目标"，后者是"既有传输的加速实现"。

---

### 4.6 协议支持文档与设备探测

#### 4.6.1 概念说明

面对这么多协议，用户怎么知道"我该用哪个、要装什么、怎么探测硬件"？答案是仓库自带的协议支持文档 `docs/source/getting_started/supported-protocols.md`。它不是代码，却是理解"协议矩阵怎么落地"的钥匙——它把每种协议的硬件要求、编译开关、配置示例、设备探测命令写成了一张可操作的速查表。

这份文档的价值在于：它把"能编译"和"能用"之间的鸿沟填平了——告诉你每种协议在运行现场需要什么。

> **文档覆盖提醒**：`supported-protocols.md` 是"精选子集 + 使用说明"，**并不覆盖全部协议**。例如本讲后面要讲的两个特性——Ascend per-role 配置（4.7）和 MUSA device fence 原语（4.8）——在这份文档里都还没有专门条目，只能回到源码（`ascend_direct_transport/utils.cpp`、`musa_ops.cuh`）理解。读源码矩阵/实现比读文档更权威。

#### 4.6.2 核心流程

选协议的决策流程（文档第 317–328 行的 "Choosing the Right Protocol" 表）：

```
我要做什么？
├─ 开发/测试        → tcp（无需特殊硬件）
├─ 生产推理         → rdma（最佳时延吞吐）
├─ AWS 云 EFA 实例  → efa（需 USE_EFA + libfabric）
├─ 多层存储         → rdma + nvmeof
├─ AMD GPU 集群     → rdma + hip
├─ 寒武纪 MLU 集群  → rdma（+ USE_MLU）
└─ 昇腾 NPU 集群    → rdma + ascend
```

设备探测则是一组 OS 层命令，帮助你确认运行时硬件是否就位。

#### 4.6.3 源码精读

文档开头的 Quick Reference 表把常用协议一图流汇总：

[`docs/source/getting_started/supported-protocols.md:7-18`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/getting_started/supported-protocols.md#L7-L18) — 协议速查表：硬件要求 / 用途 / Python API 支持度。

文档明确标注了几个关键事实，与源码完全吻合：

1. **RDMA 回退**：检测不到 RDMA HCA 时自动回退 TCP（`supported-protocols.md:74`）。
2. **EFA 构建要求**：`cmake .. -DUSE_EFA=ON -DUSE_CUDA=ON`，且传 GPU 内存时必须带 `USE_CUDA`，否则回退 TCP 会在 GPU buffer 上报 "Bad address"（`supported-protocols.md:151-154`）——这正是 4.3 讲的"编译开关决定可用性"的运行期后果。
3. **NVLink (MNNVL)**：用 `USE_MNNVL=ON` 编译；当 `protocol="rdma"` 且存在 RDMA 网卡时，必须显式 `export MC_FORCE_MNNVL=true` 才会用 MNNVL 而非 RDMA（`supported-protocols.md:196-204`）——说明协议选择不仅看字符串，还看运行时环境变量与硬件探测结果。
4. **设备探测命令**：`ibv_devices` / `ibv_devinfo` 列出 RDMA 设备（`supported-protocols.md:104-108、334-338`）。

配置层面，协议既可用 JSON 配置文件的 `"protocol"` 字段指定（`supported-protocols.md:271-295`），也可用环境变量 `MOONCAKE_PROTOCOL` / `MOONCAKE_DEVICE`（`supported-protocols.md:297-315`）。

#### 4.6.4 代码实践

**实践目标**：把"协议矩阵"与"用户文档"对齐，列出每种 transport 的运行时与编译开关。

**操作步骤**：

1. 打开 `supported-protocols.md`，逐节阅读 tcp / rdma / efa / nvmeof / nvlink / nvlink_intra / hip / barex / cxl / ascend 各自的 "Requirements" 与 "Build Requirements"。
2. 对每个协议，记下三列：(a) 编译开关（回 `common.cmake:71-89` 核对）；(b) 运行时/硬件要求；(c) 配置方式（JSON 字段 / 环境变量）。
3. 运行 `ibv_devices`（若有 RDMA 环境），观察本机有哪些 HCA；对照 `supported-protocols.md:104-108` 的示例输出。
4. 把"文档列的协议"与本讲 4.1.2 矩阵表交叉比对，标出文档未覆盖的协议，并记录"文档尚未覆盖、需读源码"的特性（Ascend per-role、MUSA fence）。

**需要观察的现象**：文档为常用协议都给出了"硬件要求 + 编译开关 + 配置示例"三件套；EFA 的 `-DUSE_CUDA=ON` 是硬性附加条件；MLU 没有独立协议字符串，复用 rdma；而 Ascend 的 per-role 链路选择、MUSA 的 fence 原语在文档里没有条目。

**预期结果**：你得到一张"协议 → 编译开关 + 运行时依赖 + 配置方式"的完整对照表（本讲 4.1.2 已给出权威版本，本实践是让你亲手从文档+源码再验证一遍），并清楚知道文档的边界在哪。

> 待本地验证：`ibv_devices` 需要 rdma-core 工具与真实 RDMA 硬件；若无，可只做文档阅读部分。

#### 4.6.5 小练习与答案

**练习 1**：用户用默认配置编译（未开任何 `USE_*`），在 AWS p5e 实例上跑 `protocol="efa"`，会发生什么？

**参考答案**：会失败。因为默认 `USE_EFA=OFF`，`installTransport` 里 `"efa"` 分支被 `#ifdef` 删除，调用返回 `nullptr` 并打印 "Unsupported transport efa, please rebuild Mooncake"。必须在 AWS 上重新 `cmake .. -DUSE_EFA=ON -DUSE_CUDA=ON`（`supported-protocols.md:151`）并确保 libfabric 可被找到（`common.cmake`）才能用。

**练习 2**：`supported-protocols.md` 的协议列表（10 种）比源码实际支持的（14 种）少，这种"文档滞后于代码"会带来什么风险？

**参考答案**：用户可能不知道仓库还支持 `ub`/`maca`/`sunrise_link`/`ubshmem` 等协议，从而错失可用能力；或者反过来，文档读者以为某协议"官方支持"，但实际它面向特定厂商、缺乏通用文档；更进一步，像 Ascend per-role 链路、MUSA fence 这种源码已实现但文档未写的特性，用户只看文档会完全错过。这正是为什么 4.1 强调"协议矩阵的权威来源是构建系统（`CMakeLists.txt` + `common.cmake`）和工厂函数（`installTransport`），文档是精选子集"。读源码矩阵比读文档更可靠。

---

### 4.7 Ascend per-role 配置：同进程共存 TE 的角色分流

#### 4.7.1 概念说明

昇腾 `ascend` 协议有一个在其它协议里见不到的难题：**同一个进程里常常同时存在两个用途不同的 TransferEngine**。在 Mooncake Store 的典型部署里：

- **Store TE**：负责 KV cache 的远端读写。它的数据要走 **RoCE** 链路（NPU 内存 ↔ RoCE 网卡 ↔ 远端节点，类似 D2H）。
- **P2P TE**：负责 NPU 之间的直连拷贝。它的数据走 **HCCS** 链路（片间高速互连，类似 D2D）。

问题来了：这两条链路的配置（adxl/HCCL 的通信资源配置）都来自**同一个进程级环境变量** `ASCEND_GLOBAL_RESOURCE_CONFIG`，它是一段 JSON。如果两个 TE 各自盲目读取同一份 JSON，就无法区分"这份配置是给 Store 的 RoCE 用，还是给 P2P 的 HCCS 用"。

Mooncake 的解法（提交 `#2499`）很巧妙：不新增协议字符串，而是

1. 在 `GlobalConfig` 里加一个**瞬时标志位** `ascend_store_te_init`，表示"当前正在安装的 TE 是不是 Store TE"；
2. Store 入口（`Client::InitTransferEngine`）在安装 ascend transport 之前把这个标志置 `true`，装完（RAII 析构）立刻复位为 `false`；
3. 配置 JSON 里允许有一个可选的 **`store` 子键**，作为"Store TE 专用覆盖"；
4. 解析时 `ResolveAscendGlobalResourceConfig` 根据标志位决定取哪一份：

| 场景 | 标志位 | 解析结果 |
| --- | --- | --- |
| 无 `store` 子键（旧式配置） | 任意 | 原样返回整段 JSON（向后兼容） |
| 有 `store` 子键 + Store TE | `true` | 只返回 `store` 子对象（RoCE 配置） |
| 有 `store` 子键 + P2P TE | `false` | 返回**删掉 `store` 子键**后的默认配置（HCCS 配置） |

这样，同一份 `ASCEND_GLOBAL_RESOURCE_CONFIG` 就能在同进程的两个 TE 里被"按角色"解析成不同的链路配置。

> **关键前提**：这个标志位是"进程全局 + 瞬时"的，它假设**同一进程内的 TE 初始化是串行的**（Store 先装、复位，再装 P2P）。代码注释明确写了 "Assumes TE inits are serialized within the process"。

#### 4.7.2 核心流程

```
Store 入口  Client::InitTransferEngine(local_hostname, ..., protocol="ascend"):
    StoreTeInitGuard guard;                  # RAII：析构时把 ascend_store_te_init 复位为 false
    if (protocol == "ascend")
        globalConfig().ascend_store_te_init = true
    ↓
    transfer_engine_->installTransport("ascend", nullptr)
        ↓ new AscendDirectTransport()->install(...)
        ↓ allocateLocalSegmentID():
              roce_mode_      = IsRoceModeEnabled()                      # 解析当前角色的链路
              use_fabric_mem_ = ascend_use_fabric_mem && ascend_store_te_init   # 仅 Store TE 可用 fabric mem
    ↓ guard 析构：globalConfig().ascend_store_te_init = false   ← 复位
    ↓
（之后另一个 P2P TE 再调 installTransport("ascend")）
    ascend_store_te_init 此时已是 false → IsRoceModeEnabled 走 HCCS 分支

IsRoceModeEnabled():
    1. 先看环境变量 HCCL_INTRA_ROCE_ENABLE == 1 ？（是 → RoCE 模式）
    2. 否则 ResolveAscendGlobalResourceConfig(ASCEND_GLOBAL_RESOURCE_CONFIG) 得到"当前角色有效配置"，
       再用 HasRoceProtocolDescInGlobalResourceConfig() 判断其 protocol_desc 是否含 "roce:*"
```

核心是 `ResolveAscendGlobalResourceConfig` 这一步：它把"同一份进程级 JSON"按 `ascend_store_te_init` 切成两份，从而让 Store TE 看到的是 RoCE 配置、P2P TE 看到的是 HCCS 配置。

#### 4.7.3 源码精读

**① 瞬时标志位的定义**——带着详尽的设计注释：

[`mooncake-transfer-engine/include/config.h:88-94`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/config.h#L88-L94) — `ascend_store_te_init`：进程全局、瞬时、由 Store 入口在装 ascend transport 前后置位/复位。

```cpp
// Transient flag scoped to a single TE init: set true by the Store entry
// (Client::InitTransferEngine) before installing the ascend transport, and
// reset to false right after. Lets ascend_direct distinguish a Store-init
// TE from a normal/P2P TE so each can resolve its own
// ASCEND_GLOBAL_RESOURCE_CONFIG (e.g. Store=RoCE, P2P=HCCS). Assumes TE
// inits are serialized within the process.
bool ascend_store_te_init = false;
```

**② Store 入口置位/复位**——用 RAII 守卫保证"任何退出路径都会复位"：

[`mooncake-store/src/client_service.cpp:634-648`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp#L634-L648) — `Client::InitTransferEngine`：protocol 是 ascend 时把标志置 true，靠 `StoreTeInitGuard` 析构复位。

```cpp
ErrorCode Client::InitTransferEngine(...) {
    // TEs created through the Store entry (Client::Create -> InitTransferEngine)
    // are tagged so ascend_direct can resolve a per-role link config
    // (e.g. Store=RoCE/D2H, P2P=HCCS/D2D). ...
    struct StoreTeInitGuard {
        ~StoreTeInitGuard() { globalConfig().ascend_store_te_init = false; }
    } store_te_init_guard;
    if (protocol == "ascend") {
        globalConfig().ascend_store_te_init = true;
    }
    ... // 随后 installTransport("ascend", nullptr)
}
```

> 注意：这里**只有走 Store 入口创建的 TE 才会被置位**。独立的 P2P TE（不经 `Client::InitTransferEngine`、直接由上层 `TransferEngine::installTransport` 创建）标志位保持默认的 `false`，自然就走 HCCS 分支——这正是"角色分流"的落点。

**③ ascend transport 据此决定链路与 fabric mem**：

[`mooncake-transfer-engine/src/transport/ascend_transport/ascend_direct_transport/ascend_direct_transport.cpp:152-166`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transport/ascend_transport/ascend_direct_transport/ascend_direct_transport.cpp#L152-L166) — `allocateLocalSegmentID`：`roce_mode_` 由 `IsRoceModeEnabled()` 决定；`use_fabric_mem_` 再 AND 上 `ascend_store_te_init`。

```cpp
agent_mode_ = globalConfig().ascend_agent_mode;
roce_mode_ = IsRoceModeEnabled();
// Only a Store-init TE may use fabric mem; gate on ascend_store_te_init so
// a P2P/HCCS TE does not inherit a Store TE's fabric flag left in the
// process-global config.
use_fabric_mem_ = globalConfig().ascend_use_fabric_mem &&
                  globalConfig().ascend_store_te_init;
LOG(INFO) << "[AscendTE] init local segment, te is created for store="
          << (globalConfig().ascend_store_te_init ? "true" : "false")
          << ", roce_mode=" << (roce_mode_ ? "true" : "false")
          << ", use_fabric_mem=" << (use_fabric_mem_ ? "true" : "false") ...;
```

注释点明了为什么要 AND `ascend_store_te_init`：**防止 P2P/HCCS TE 误继承 Store TE 留在进程全局配置里的 fabric 标志**（因为 `ascend_use_fabric_mem` 是个持久开关，而角色判断是瞬时的）。

**④ 配置按角色切分**——本模块的核心逻辑：

[`mooncake-transfer-engine/src/transport/ascend_transport/ascend_direct_transport/utils.cpp:106-130`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transport/ascend_transport/ascend_direct_transport/utils.cpp#L106-L130) — `ResolveAscendGlobalResourceConfig`：有 `store` 子键时，Store TE 取子对象、P2P TE 取"删掉 store 后"的默认配置。

```cpp
std::string ResolveAscendGlobalResourceConfig(const char* config_str) {
    ...  // 解析 config_str 为 JSON root
    if (!root.isObject() || !root.isMember(kStoreConfigKey)) {
        // Plain (legacy) config without a "store" override: pass verbatim.
        return std::string(config_str);            // ① 无 store 子键 → 原样（向后兼容）
    }
    if (globalConfig().ascend_store_te_init) {
        return SerializeCompactJson(root[kStoreConfigKey]);  // ② Store TE → 只取 store 子对象
    }
    Json::Value normal = root;                                // ③ P2P TE → 删掉 store 子键
    normal.removeMember(kStoreConfigKey);
    return SerializeCompactJson(normal);
}
```

**⑤ 判定是否 RoCE 链路**——把上面四步串起来：

[`mooncake-transfer-engine/src/transport/ascend_transport/ascend_direct_transport/utils.cpp:132-161`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transport/ascend_transport/ascend_direct_transport/utils.cpp#L132-L161) — `IsRoceModeEnabled`：先看 `HCCL_INTRA_ROCE_ENABLE`，再看角色化后的 `ASCEND_GLOBAL_RESOURCE_CONFIG` 是否含 `roce:*`。

```cpp
bool IsRoceModeEnabled() {
    ... // 1) 先看 HCCL_INTRA_ROCE_ENABLE==1
    char* global_resource_config = std::getenv("ASCEND_GLOBAL_RESOURCE_CONFIG");
    std::string resolved =
        ResolveAscendGlobalResourceConfig(global_resource_config);  // ← 角色切分在这一步
    LOG(INFO) << "[AscendTE] resolved ASCEND_GLOBAL_RESOURCE_CONFIG ...";
    if (HasRoceProtocolDescInGlobalResourceConfig(resolved.c_str())) {
        return true;   // 2) 角色化后的配置含 roce:* → RoCE 模式
    }
    return false;      // 否则 → HCCS 模式
}
```

`HasRoceProtocolDescInGlobalResourceConfig`（同文件 `utils.cpp:79-104`）在 JSON 的 `comm_resource_config.protocol_desc` 里查找 `"roce:..."` 字符串或数组元素。**所以"Store=RoCE / P2P=HCCS"不是硬编码，而是由配置 JSON 的内容 + 角色标志共同决定的**：只要 Store 的 `store` 子键里写了 `roce:`、默认配置里不写（或写别的），分流就自动成立。

这套 schema 在头文件里有完整文档注释：

[`mooncake-transfer-engine/include/transport/ascend_transport/ascend_direct_transport/utils.h:82-97`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/transport/ascend_transport/ascend_direct_transport/utils.h#L82-L97) — `ResolveAscendGlobalResourceConfig` 的 schema 说明：顶层是默认配置，`store` 子对象是 Store TE 的覆盖。

#### 4.7.4 代码实践

**实践目标**（本讲规格指定的实践第一部分）：阅读 `supported-protocols.md` 与 `ascend_direct_transport.cpp` / `utils.cpp`，说明同进程内 Store TE 与 P2P TE 如何通过 `ascend_store_te_init` 与 `ASCEND_GLOBAL_RESOURCE_CONFIG` 的 `store` 子键各自解析 RoCE / HCCS 链路配置。

**操作步骤**：

1. 先在 `supported-protocols.md` 里检索 `ascend` / `HCCS` / `per-role` —— 你会发现**文档里没有 per-role 链路选择的说明**（这正是"文档滞后于代码"的实例）。记下这一点。
2. 打开 `config.h:88-94`，读 `ascend_store_te_init` 的注释，理解"瞬时、进程全局、需串行初始化"三个约束。
3. 打开 `client_service.cpp:634-648`，追踪标志的生命周期：`StoreTeInitGuard` 在函数入口构造、`if (protocol=="ascend")` 置位、函数返回（任意路径）时析构复位。
4. 打开 `ascend_direct_transport.cpp:152-166`，确认 `roce_mode_` 来自 `IsRoceModeEnabled()`，`use_fabric_mem_` 被 `ascend_store_te_init` 二次约束。
5. 打开 `utils.cpp:106-130`，逐分支推演 `ResolveAscendGlobalResourceConfig`：构造一段示例 JSON（顶层含 `comm_resource_config.protocol_desc`，外加一个 `store` 子键覆盖），分别代入 `ascend_store_te_init=true/false`，手算两种返回结果。
6. 打开 `utils.cpp:132-161`，串起完整调用：`IsRoceModeEnabled` → `ResolveAscendGlobalResourceConfig` → `HasRoceProtocolDescInGlobalResourceConfig`。

**需要观察的现象**：同一份 `ASCEND_GLOBAL_RESOURCE_CONFIG`，Store TE 解析后能看到 `store` 子键里的 `roce:*`（返回 `true`→RoCE），P2P TE 解析后该子键被删掉、看不到 `roce:*`（返回 `false`→HCCS）。日志里会打印 `[AscendTE] resolving link config, te is created for store=true/false` 与 `resolved ASCEND_GLOBAL_RESOURCE_CONFIG ...` 两条，可直接对照。

**预期结果**：你能用一句话讲清——"标志位 `ascend_store_te_init` 在配置解析时充当'角色标签'，让同进程的 Store TE 与 P2P TE 从同一份 JSON 里分别取走属于自己的链路配置（Store 取 `store` 子键=RoCE，P2P 取默认=HCCS），从而不必为两种角色各自维护两份环境变量。"

> 待本地验证：完整运行需昇腾 NPU + HCCL/adxl 环境。无硬件时为"源码阅读型实践"——按步骤读源码 + 手算 JSON 分支即可。若想看日志，可在有环境时设 `ASCEND_GLOBAL_RESOURCE_CONFIG='{"comm_resource_config":{"protocol_desc":"roce:v1"},"store":{"comm_resource_config":{"protocol_desc":"roce:v2"}}}'`，分别以 Store / 独立 TE 启动，观察两条 `[AscendTE]` 日志的 `resolved` 值差异。

#### 4.7.5 小练习与答案

**练习 1**：为什么 `use_fabric_mem_` 要写成 `ascend_use_fabric_mem && ascend_store_te_init`，而不是直接用 `ascend_use_fabric_mem`？

**参考答案**：因为 `ascend_use_fabric_mem` 是一个**持久**的进程全局开关（由 `ASCEND_ENABLE_USE_FABRIC_MEM` 环境变量置位后就一直为 true），而 fabric mem 是 **Store TE 专属**的能力。如果不 AND 上瞬时的 `ascend_store_te_init`，那么同进程里后初始化的 P2P/HCCS TE 会"继承"这个持久标志、错误地启用 fabric mem——这正是 `ascend_direct_transport.cpp:154-158` 注释警告的情形。AND 上角色标志，把"持久开关"收窄成"Store TE 才生效"。

**练习 2**：为什么用 RAII 守卫（`StoreTeInitGuard`）复位标志，而不是在函数末尾手动写一行 `globalConfig().ascend_store_te_init = false;`？

**参考答案**：因为 `InitTransferEngine` 有**多条提前返回路径**（各种错误分支都会 `return ErrorCode::...`）。如果手动复位，必须保证每条 return 前都写一遍，极易遗漏——一旦某条错误路径漏掉复位，标志位会"卡在 true"，导致同进程后续的 P2P TE 被误判成 Store TE。RAII 守卫利用析构保证"无论从哪条路径退出，都会复位一次"，从机制上杜绝了遗漏。

**练习 3**：如果同一进程里两个 TE 的初始化**没有串行**（比如两个线程同时 `installTransport("ascend")`），这个机制会出什么问题？

**参考答案**：标志位是进程全局的单变量，并发初始化会产生竞态：线程 A 置 true、线程 B 置 false、A 读到 false……最终两个 TE 可能都拿到错误的角色。这就是为什么 `config.h:92` 的注释明确写 "Assumes TE inits are serialized within the process"——这是该设计的**前置假设**，调用方必须保证 TE 初始化串行。

---

### 4.8 MUSA fence 原语：跨 GPU 内存可见性的实现差异

#### 4.8.1 概念说明

mooncake-ep（Expert Parallelism）在 GPU 上做 device 端通信时，需要一组**内存序原语**：acquire load（读到数据时确保之前的写都已可见）、release store（写完数据时确保对他人可见）、system fence（全系统内存屏障）等。为了让上层 kernel 代码与具体 GPU 厂商解耦，Mooncake 定义了一套**统一的 C++ 内联函数名**：

| 统一 API | 作用 |
| --- | --- |
| `mc_ld_acquire` / `mc_ld_acquire_u64` | acquire 语义的加载 |
| `mc_st_release` / `mc_st_release_u32` / `mc_st_release_u64` | release 语义的存储 |
| `mc_atomic_add_release` | release 语义的原子加 |
| `mc_fence` | 系统级内存屏障 |
| `mc_bar_sync` | CTA/命名 barrier 同步 |
| `mc_grid_sync` | grid 级同步 |
| `mc_fence_barrier_fence` | "fence + barrier + fence" 组合 |

这套 API 在 **NVIDIA CUDA** 和 **摩尔线程 MUSA** 上**函数名完全相同，但实现截然不同**。`device_ops.cuh` 用一个宏 `MOONCAKE_EP_USE_MUSA` 二选一地 include 对应实现。本节聚焦两个最关键的差异：

**差异一：跨 GPU 内存可见性（fence）。**

- **CUDA**：硬件通过 NVLink **自动**保证 P2P 可见性，且 PTX 提供原生的 `.acquire.sys` / `.release.sys`（system 作用域）指令。因此系统级屏障 `mc_fence()` 在 CUDA 上是**空操作**——硬件已经帮你做好了。
- **MUSA**：MTLink 跨 GPU 可见性**不自动**保证，MUSA 也没有 PTX 风格的 acquire/release 指令。因此只能用"普通 load/store + 显式 `__threadfence_system()`"来模拟，`mc_fence()` 在 MUSA 上就是 `__threadfence_system()`。

**差异二：grid 级同步。**

- **CUDA**：`cooperative_groups::this_grid().sync()` 真正做 grid 同步（SEND 和 RECV 可以在同一个 cooperative kernel 里跑）。
- **MUSA**：不支持 cooperative grid sync；但 host 端的 MUSA 路径**总是把 SEND 和 RECV 拆成两次独立的 kernel launch**（`return_recv_hook=true`），所以二者本就不在同一个 kernel 里、不需要 grid 同步，`mc_grid_sync()` 是安全的 no-op。

**差异三：CTA barrier 是否隐含 fence。**

- **CUDA**：`__syncthreads()` 隐含内存屏障，所以 `mc_fence_barrier_fence()` 退化为单个 `__syncthreads()`。
- **MUSA**：`__syncthreads()` **不**隐含内存屏障，所以 `mc_fence_barrier_fence()` 必须是 `fence + barrier + fence` 三件套。

#### 4.8.2 核心流程

编译期，`device_ops.cuh` 按宏选择实现，上层 kernel 调用的都是同一套 `mc_*` 名字：

```
device_ops.cuh:
    #ifdef MOONCAKE_EP_USE_MUSA
        #include "musa/musa_ops.cuh"      // MUSA 实现
    #else
        #include "cuda/cuda_ops.cuh"      // CUDA 实现
    #endif

kernel 代码（与厂商无关）:
    val = mc_ld_acquire(&flag);     // acquire load —— CUDA 用 PTX，MUSA 用 volatile+threadfence
    ... 处理数据 ...
    mc_st_release(&done, 1);        // release store —— CUDA 用 PTX，MUSA 用 threadfence+store+threadfence
    mc_fence();                     // system fence —— CUDA 空操作，MUSA=__threadfence_system()
```

核心洞见：**跨 GPU 可见性这件事，CUDA 由硬件 + PTX 指令保证，所以代码里"看不见"fence；MUSA 没有这层硬件/指令保证，所以必须在软件里显式补 fence。** 这正是"同一份 API、两套实现"的原因。

#### 4.8.3 源码精读

**① 分发头**——按宏二选一：

[`mooncake-transfer-engine/include/transport/device/device_ops.cuh:8-11`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/transport/device/device_ops.cuh#L8-L11) — `MOONCAKE_EP_USE_MUSA` 决定 include 哪套实现。

```cpp
#ifdef MOONCAKE_EP_USE_MUSA
#include "transport/device/musa/musa_ops.cuh"
#else
#include "transport/device/cuda/cuda_ops.cuh"
#endif
```

**② CUDA：`mc_fence()` 是空操作**——硬件已保证 NVLink P2P 可见性：

[`mooncake-transfer-engine/include/transport/device/cuda/cuda_ops.cuh:128-132`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/transport/device/cuda/cuda_ops.cuh#L128-L132) — CUDA 的 `mc_fence()` 为空函数体。

```cpp
// System-level memory fence: no-op on CUDA (hardware guarantees P2P
// visibility through NVLink without explicit fences).
__device__ __forceinline__ void mc_fence() {}
```

而 CUDA 的 acquire/release 用的是 PTX 原生指令（`.sys` 作用域 + `.L1::no_allocate` 缓存提示）：

[`mooncake-transfer-engine/include/transport/device/cuda/cuda_ops.cuh:14-18`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/transport/device/cuda/cuda_ops.cuh#L14-L18) — CUDA 用 PTX `ld.acquire.sys.global`。

```cpp
__device__ __forceinline__ int mc_ld_acquire(const int* ptr) {
    int ret;
    asm volatile("ld.acquire.sys.global.s32 %0, [%1];" : "=r"(ret) : "l"(ptr));
    return ret;
}
```

**③ MUSA：`mc_fence()` 是 `__threadfence_system()`**——必须显式屏障才能跨 MTLink 可见：

[`mooncake-transfer-engine/include/transport/device/musa/musa_ops.cuh:125-130`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/transport/device/musa/musa_ops.cuh#L125-L130) — MUSA 的 `mc_fence()` 即 `__threadfence_system()`。

```cpp
// System-level memory fence: MUSA requires explicit __threadfence_system()
// for cross-GPU (MTLink) visibility.  CUDA hardware guarantees this without
// explicit fences, so mc_fence() is a no-op there.
__device__ __forceinline__ void mc_fence() { __threadfence_system(); }
```

MUSA 的 acquire load 没有原生指令，只能 `volatile` 读 + `__threadfence_system()`：

[`mooncake-transfer-engine/include/transport/device/musa/musa_ops.cuh:24-28`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/transport/device/musa/musa_ops.cuh#L24-L28) — MUSA acquire = volatile load + `__threadfence_system()`。

```cpp
__device__ __forceinline__ int mc_ld_acquire(const int* ptr) {
    int ret = *const_cast<volatile const int*>(ptr);
    __threadfence_system();
    return ret;
}
```

**④ release store 的"MUSA 双 fence"**——store 前后都要屏障。对比两版：

CUDA（一条 PTX release 指令搞定）：

```cpp
// cuda_ops.cuh:29-33
asm volatile("st.release.sys.global.L1::no_allocate.s32 [%0], %1;" :: "l"(ptr), "r"(val));
```

MUSA（前后各一个 `__threadfence_system()`）：

[`mooncake-transfer-engine/include/transport/device/musa/musa_ops.cuh:39-43`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/transport/device/musa/musa_ops.cuh#L39-L43) — MUSA release = fence + store + fence。

```cpp
__device__ __forceinline__ void mc_st_release(const int* ptr, int val) {
    __threadfence_system();
    *const_cast<volatile int*>(ptr) = val;
    __threadfence_system();
}
```

> 为什么 MUSA 的 release 要"前后双 fence"？因为没有原生 release 指令，只能靠 fence 把"之前的写不能越过 store（前 fence）"和"这个 store 对其它 GPU 可见（后 fence）"两件事都手动钉死。当前 HEAD 相对旧版还**新增了 store 之后那个 trailing fence**——这是修正确性问题（确保跨 MTLink 可见），见 musa_ops.cuh 头部的"已知 SDK 4.3.3 编译器缺陷"注释。

**⑤ grid sync 与 barrier 的差异**：

CUDA 用 cooperative groups，且 `__syncthreads()` 隐含 fence：

```cpp
// cuda_ops.cuh:124-126
__device__ __forceinline__ void mc_grid_sync() {
    cooperative_groups::this_grid().sync();
}
// cuda_ops.cuh:139 —— mc_fence_barrier_fence 退化为单个 __syncthreads()
__device__ __forceinline__ void mc_fence_barrier_fence() { __syncthreads(); }
```

MUSA 既无 cooperative grid sync（host 拆 kernel launch，所以 no-op 安全），`__syncthreads()` 也不隐含 fence：

[`mooncake-transfer-engine/include/transport/device/musa/musa_ops.cuh:119-141`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/transport/device/musa/musa_ops.cuh#L119-L141) — MUSA `mc_grid_sync` 空操作、`mc_fence_barrier_fence` 为 fence+barrier+fence。

```cpp
__device__ __forceinline__ void mc_grid_sync() {}            // host 已拆 SEND/RECV，无需 grid 同步
...
__device__ __forceinline__ void mc_fence_barrier_fence() {
    mc_fence();            // fence
    mc_bar_sync(0, 0);     // barrier (__syncthreads())
    mc_fence();            // fence —— MUSA 的 __syncthreads 不隐含 fence，故两侧都要补
}
```

musa_ops.cuh 顶部的注释把这几条差异（无 acquire/release 指令、无 named barrier、无 cooperative grid sync、`atomicAdd_system` 有编译器 bug 需改用 block-scope + fence）一次性列清楚——这正是"为 MUSA 维护一套等价但不同实现"的工程代价。

#### 4.8.4 代码实践

**实践目标**（本讲规格指定的实践第二部分）：对照 `musa_ops.cuh` 与 `cuda_ops.cuh`，解释 MUSA 为何需要显式 `__threadfence_system()` 而 CUDA 的 `mc_fence` 是空操作。

**操作步骤**：

1. 打开 `device_ops.cuh:8-11`，确认 `MOONCAKE_EP_USE_MUSA` 这一个宏决定了 include 哪套实现，理解"上层 kernel 调 `mc_fence()` 时，编译期就被替换成对应厂商的实现"。
2. 打开 `cuda_ops.cuh:128-132`，读 `mc_fence()` 的空函数体与其注释——"CUDA hardware guarantees P2P visibility through NVLink without explicit fences"。
3. 打开 `musa_ops.cuh:125-130`，读 `mc_fence() { __threadfence_system(); }` 与注释——"MUSA requires explicit `__threadfence_system()` for cross-GPU (MTLink) visibility"。
4. 并排对比 `mc_ld_acquire`（CUDA `cuda_ops.cuh:14-18` 的 PTX `ld.acquire.sys` vs MUSA `musa_ops.cuh:24-28` 的 volatile+threadfence）与 `mc_st_release`（CUDA 一条 PTX vs MUSA `musa_ops.cuh:39-43` 的双 threadfence）。
5. 读 `musa_ops.cuh:1-13` 的文件头注释，把"MUSA 没有 acquire/release 指令、用 threadfence 模拟"这条主线和"atomicAdd_system 有编译器 bug、cooperative grid sync 不可用、named barrier 不可用"这几条支线对应起来。

**需要观察的现象**：CUDA 侧每个 `mc_*` 几乎都映射到一条 PTX 指令（acquire/release/sys/no_allocate 都靠指令后缀表达），`mc_fence` 甚至是空的；MUSA 侧每个 `mc_*` 都要靠 `__threadfence_system()` 手工拼出等价语义，函数体明显更"重"。这正是"硬件/指令差异 → 软件补丁差异"的直接体现。

**预期结果**：你能用一句话讲清——"跨 GPU 内存可见性在 CUDA 上由硬件 + PTX 的 `.acquire.sys`/`.release.sys` 指令保证，所以 `mc_fence()` 无事可做；MUSA 没有这层保证，也没有等价指令，只能用显式 `__threadfence_system()` 在软件层面强制跨 MTLink 可见，于是 `mc_fence()` 必须实打实地发一个系统屏障。"

> 待本地验证：这是 device 端代码，完整运行需对应 GPU（CUDA 或 MUSA SDK 4.3.3+）。无硬件时为"源码阅读型实践"——并排对照两个 `.cuh` 文件即可完成。若想看宏切换效果，可分别用 `-DUSE_CUDA=ON` 与 `-DUSE_MUSA=ON`（mooncake-ep 侧 `MOONCAKE_EP_USE_MUSA`）配置编译，观察 `device_ops.cuh` 实际 include 的那一套。

#### 4.8.5 小练习与答案

**练习 1**：MUSA 的 `mc_st_release` 为什么在 store 前后**各**放一个 `__threadfence_system()`，而不是只在后面放一个？

**参考答案**：release 语义要保证两件事：(a) 本线程在 store **之前**的所有写，不能在 store 之后才被其它 GPU 看到（需要"前 fence"防止重排把旧写排到 store 之后）；(b) 这个 store 本身要尽快对其它 GPU 可见（需要"后 fence"把 store 刷出去）。CUDA 用一条 `st.release.sys` 指令同时表达这两件事；MUSA 没有等价指令，只能用两个显式 fence 分别钉住 (a) 和 (b)。

**练习 2**：MUSA 的 `mc_grid_sync()` 是空操作，会不会导致 SEND/RECV 之间的同步丢失？

**参考答案**：不会，因为 host 端的 MUSA 路径把 SEND 和 RECV 拆成了**两次独立的 kernel launch**（注释里的 `return_recv_hook=true`）。两次 launch 之间天然有一次"host 侧同步"（host 等前一个 kernel 完成再 launch 下一个），所以 device 端根本不需要 grid 级同步——`mc_grid_sync()` 设成 no-op 正好与 host 端的这种调度方式匹配。CUDA 则允许 SEND/RECV 在同一个 cooperative kernel 里，所以必须真正做 grid sync。

**练习 3**：为什么 MUSA 的 `mc_atomic_add_release` 用"block-scope `atomicAdd` + `__threadfence_system()`"而不是直接用 `atomicAdd_system`？

**参考答案**：因为 `musa_ops.cuh:8-9` 的注释指出 MUSA SDK 4.3.3 的 `atomicAdd_system` / `atomicCAS_system` 会触发编译器的无限 SelectionDAG 循环（一个已知 bug）。绕开办法就是改用 block 作用域的 `atomicAdd`，再用 `__threadfence_system()` 补上跨 GPU 可见性。这是"为特定厂商 SDK 的 bug 打补丁"的典型例子，也说明 MUSA 实现里藏了不少这类工程性 workaround。

---

## 5. 综合实践

把本讲的核心模块串起来，完成下面这个综合任务（本讲规格指定的实践）。

**任务**：选定昇腾 `ascend` 与 MUSA 两条线，分别完成一次"从文档/配置 → 编译开关 → 源码 → 行为"的全程跟踪，重点落在本讲新增的两个差异点上：(1) 同进程 Store TE 与 P2P TE 的角色化链路解析；(2) CUDA 与 MUSA 的跨 GPU fence 实现差异。

**步骤 1：对照 supported-protocols.md，确认文档边界（对应 4.6）**

打开 `supported-protocols.md`，分别检索 `ascend`/`HCCS`/`per-role` 与 `musa`/`mthreads`/`MTLink`。你会发现：文档对 ascend 有基础说明，但**没有 per-role 链路选择**的条目；对 MUSA 则基本没有 device fence 层面的说明。把这两点记为"文档未覆盖、必须读源码"的特性。

**步骤 2：跟踪 Ascend per-role 链路解析（对应 4.7）**

按这条链路走一遍，并填表：

| 环节 | 文件:行 | 关键代码 |
| --- | --- | --- |
| 置位角色标志 | `client_service.cpp:634-648` | `if (protocol=="ascend") ascend_store_te_init=true; StoreTeInitGuard` |
| 读取链路模式 | `ascend_direct_transport.cpp:152-166` | `roce_mode_=IsRoceModeEnabled(); use_fabric_mem_ && ascend_store_te_init` |
| 角色化切配置 | `utils.cpp:106-130` | 有 `store` 子键：Store TE 取子对象 / P2P TE 删掉子键 |
| 判定 RoCE | `utils.cpp:132-161` | `HCCL_INTRA_ROCE_ENABLE` 或角色化配置含 `roce:*` |

构造一段示例 `ASCEND_GLOBAL_RESOURCE_CONFIG`（顶层默认 + `store` 子键覆盖），手算 Store TE（`ascend_store_te_init=true`）与独立 P2P TE（`false`）各自得到的"有效配置"，说明前者解析出 RoCE、后者解析出 HCCS。

**步骤 3：对照 MUSA 与 CUDA 的 fence 原语（对应 4.8）**

并排打开 `musa/musa_ops.cuh` 与 `cuda/cuda_ops.cuh`，对每个 `mc_*` 原语填表：

| 原语 | CUDA 实现 | MUSA 实现 | 差异根因 |
| --- | --- | --- | --- |
| `mc_fence` | `{}`（空） | `__threadfence_system()` | CUDA 硬件保证 NVLink P2P 可见；MUSA 需显式屏障 |
| `mc_ld_acquire` | PTX `ld.acquire.sys` | volatile load + `__threadfence_system()` | MUSA 无 acquire 指令 |
| `mc_st_release` | PTX `st.release.sys` | `__threadfence_system()`×2 包夹 store | MUSA 无 release 指令，需双 fence |
| `mc_grid_sync` | `cooperative_groups::this_grid().sync()` | `{}`（空） | MUSA host 拆 SEND/RECV 为两次 launch |
| `mc_fence_barrier_fence` | 单 `__syncthreads()` | fence + `__syncthreads()` + fence | MUSA 的 `__syncthreads` 不隐含 fence |

**步骤 4：用一句话串起两个差异点的共性**

**预期结果**：你能说出——"无论是 Ascend 的 per-role 链路，还是 MUSA 的 fence 原语，Mooncake 的做法都是'对上层暴露统一接口（同一个 `protocol="ascend"` / 同一组 `mc_*` 名字），把厂商/角色差异藏进内部实现（瞬时角色标志切配置 / 编译期宏选 device 原语）'。这正是本讲'统一接口、差异化实现'主线在两个具体场景下的落地。"

> 待本地验证：本综合实践以"源码阅读 + 对照手算"为主，无需特殊硬件。若要运行验证：Ascend per-role 需昇腾 NPU + HCCL/adxl 环境（设 `ASCEND_GLOBAL_RESOURCE_CONFIG` 含 `store` 子键，分别以 Store / 独立 TE 启动看 `[AscendTE]` 日志）；MUSA fence 需 MUSA SDK 4.3.3+（`-DUSE_MUSA=ON`，对照 CUDA `-DUSE_CUDA=ON` 编译）。

## 6. 本讲小结

- 仓库实际支持 **14 种协议字符串**（tcp/rdma/efa/nvmeof/nvlink/nvlink_intra/hip/barex/cxl/ascend/ub/maca/sunrise_link/ubshmem），比 `supported-protocols.md` 文档列表更全；其中 `rdma` 始终编译、`tcp` 默认 ON，其余默认 OFF。
- 所有协议共享同一个 `Transport` 基类契约——纯虚的 `submitTransfer` / `getTransferStatus` / `registerLocalMemory` 系列 / `getName`；"统一接口、差异化实现"是异构传输的基石。
- 差异化体现在两处：**行为**靠虚函数（各协议各写各的函数体）、**数据**靠 `Slice` 内部的 `union`（各协议各用各的元数据分支，如 `nvmeof`/`hccl`/`ascend_direct`）。
- 协议字符串到对象的装配在工厂 `MultiTransport::installTransport`：每个 `else if (proto=="xxx")` 分支被 `#ifdef USE_xxx` 包裹——**编译开关决定分支是否存在，运行时字符串决定是否命中**；`ascend` 一个字符串对应三个互斥实现。
- **Ascend per-role 配置**（新增）：同进程的 Store TE 与 P2P TE 共享同一份 `ASCEND_GLOBAL_RESOURCE_CONFIG`，靠瞬时标志 `ascend_store_te_init`（Store 入口置位、RAII 复位）+ 可选 `store` 子键，让两者各自解析出 RoCE（Store）与 HCCS（P2P）链路；`use_fabric_mem_` 也被该标志二次收窄，防止 P2P TE 误继承。
- **MUSA fence 原语**（新增）：同一套 `mc_*` device 原语在 CUDA/MUSA 上实现不同——CUDA 靠硬件 + PTX `acquire/release.sys` 保证跨 NVLink 可见，`mc_fence()` 为空操作；MUSA 无此保证也无等价指令，`mc_fence()` 即 `__threadfence_system()`、release 需双 fence；grid sync 在 MUSA 上因 host 拆 kernel launch 而安全地 no-op。
- 协议落地三要素：**编译开关 ON + 运行时有硬件 + 传对字符串**；设备探测用 `ibv_devices` 等命令，选型参考 `supported-protocols.md` 的 "Choosing the Right Protocol" 表；但文档是精选子集，per-role / MUSA fence 等特性须读源码。

## 7. 下一步学习建议

本讲建立的是"协议矩阵 + 统一接口 + 工厂装配 + 两个差异点"的全景。建议下一步：

1. **精读一两个具体协议的完整实现**：从 `RdmaTransport`（`rdma_transport.cpp` + `rdma_endpoint.cpp` + `worker_pool.cpp`，见 `u3-l2`）入手，看 `submitTransfer` 如何把 Slice 变成真实 Work Request、worker 如何从 CQ 回收；再对比 `TcpTransport`（`u3-l3`）或本讲的 NVMe-oF，体会"同一套契约、不同硬件回路"。
2. **深入 Ascend Direct 的执行器**：本讲 4.7 讲了配置解析与角色分流，下一步可以读 `ascend_direct_transport/transfer_executor_base.cpp` 与 `context_manager`，看 `roce_mode_`/`use_fabric_mem_` 如何影响 ADXL engine 的建链与内存注册，把"配置 → 执行器行为"这条链补全。
3. **跟踪一次端到端选路**：从 `engine.initialize(protocol=...)` 出发，跟踪到 `MultiTransport::installTransport` → `transport->install` → `selectTransport`（按目标 segment 的 `protocol` 字段在 `transport_map_` 里查），把"协议字符串"到"实际下发"的链路完整走一遍。这会把本讲 4.3 和 `u3-l1` 4.4 串起来。
4. **对照 CUDA/MUSA 把 device 原语走通**：如果你对 GPU 通信感兴趣，可以读 `device/cuda/cuda_ops.cuh` 与 `device/musa/musa_ops.cuh` 的全部 `mc_*` 原语，再到 mooncake-ep 的 kernel（`mooncake_ep_kernel.cu`）里看这些原语在 SEND/RECV kernel 中如何被调用，理解 4.8 提到的"host 拆 kernel launch → grid sync 可 no-op"在 EP 层是怎样落地的。
