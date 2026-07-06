# NCCL Gin 后端与对称内存上下文

## 1. 本讲目标

本讲深入 DeepEP V2 的「通信底座」——**NCCL Gin 后端**与**对称内存上下文（`NCCLSymmetricMemoryContext`）**。这是支撑 dispatch/combine/barrier/engram/AGRS 等所有内核的物理基础。

学完后你应当能够：

1. 说清楚 **NCCL Gin 后端**「header-only、复用已有 NCCL communicator」的轻量化设计意味着什么，以及它替代了 V1 的什么（NVSHMEM）。
2. 理解 **对称内存（symmetric memory）** 的含义：为什么每个 rank 要分配「形状一致、地址可比」的一块显存，并把它注册成一个跨 rank 的**窗口（window）**。
3. 读懂 `symmetric.hpp` 如何用 **CUDA VMM**（`cuMemCreate` / `cuMemMap` / `cuMemSetAccess`）分配出一块**可 GPUDirect RDMA** 的显存，并把它和一段 CPU 内存拼成一段连续虚拟地址。
4. 掌握 `get_sym_ptr` 的跨 rank 寻址原理：用「同一偏移量」从本 rank 窗口基址换算到对端 rank 窗口基址。
5. 看懂 `csrc/elastic/buffer.hpp` 构造函数里 `num_sym_bytes / num_gpu_bytes / num_cpu_bytes` 三者的换算关系，并能解释 workspace 为何要放在窗口最前并对齐 2MB。

> 本讲承接 u3-l1（物理域/逻辑域）与 u3-l2（缓冲区布局）。u3-l2 讲的是「窗口里装了什么、各段多大」，本讲讲的是「这块窗口本身是怎么分配、注册、并对所有 rank 暴露出可比地址的」。

## 2. 前置知识

### 2.1 为什么需要「对称内存」

普通多 GPU 编程里，rank A 想读 rank B 的显存，要走一次 `ncclSend/ncclRecv` 或 `all_reduce` 这样的**集合通信 API**——数据搬运由 NCCL 内部完成，你看不到远端地址。

但 DeepEP 的内核需要**自己直接读写对端的显存**：dispatch 内核要把 token 直接 `LDG.128` 写到对端 buffer 的某个槽位，AGRS 的 `all_gather` 要直接 `cudaMemcpyBatchAsync` 到对端槽位。这要求每个 rank 都能算出**对端某块数据在它自己 GPU 上对应的虚拟地址**。

要做到这一点，最简单的办法是**约定：所有 rank 分配一块大小、布局完全相同的缓冲区，注册成一个 NCCL 窗口**。这样「偏移量 N」在每个 rank 上指向的逻辑位置一致——本 rank 基址 + 偏移 N = 本 rank 数据；对端基址 + 同一偏移 N = 对端的对应数据。这就是**对称内存（symmetric memory）**。

### 2.2 GPUDirect RDMA 与 CUDA VMM

- **GPUDirect RDMA**：让 RDMA 网卡能**绕过 CPU 内存**，直接读写 GPU 显存。没有它，跨节点数据要先 `cudaMemcpy` 到 CPU 锁页内存，再走网卡，多一次拷贝。
- **CUDA VMM（Virtual Memory Management）**：CUDA Driver API 提供的一套**手动管理虚拟地址**的接口（`cuMemAddressReserve` / `cuMemCreate` / `cuMemMap` / `cuMemSetAccess`）。相比 `cudaMalloc`，它允许你**先预留一段虚拟地址区间，再把不同物理后端（GPU 显存 / CPU NUMA 内存）逐段映射进去**，拼成一段连续 VA。这正是 DeepEP 把 `[GPU 段][CPU 段]` 拼到一起所依赖的能力。
- **CUDA 分配粒度（allocation granularity）**：VMM 分配的物理块必须按设备要求的粒度对齐（通常 2MB）。DeepEP 全程用 `kNumAlignmentBytes = 2MB`，正好与之一致。

### 2.3 NCCL communicator 与窗口

- **`ncclComm_t`**：NCCL 的 host 侧 communicator 句柄，PyTorch 的 `dist.init_process_group(backend='nccl')` 也会创建一个。
- **`ncclDevComm_t`**：device 侧 communicator，内核里直接用，包含 NVLink/RDMA 域信息与 Gin 队列。
- **NCCL 窗口（window）**：NCCL 2.30+ 提供的能力——把一块对称内存注册给 communicator，之后所有 rank 都能通过窗口拿到彼此的设备指针。`ncclCommWindowRegister` 是**集合操作**（内部会跨 rank 做 bootstrap barrier）。
- **Gin**：NCCL 暴露给用户内核的**设备级 RDMA 通信原语**（Gin request / queue pair），让自定义 CUDA kernel 能直接发起 RDMA 读写。DeepEP V2 的 scaleout（跨节点）通信就是建立在 Gin 之上的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [csrc/kernels/backend/symmetric.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp) | **对称内存分配器**。用 CUDA VMM 分配可 GPUDirect RDMA 的显存，支持纯 GPU、GPU+CPU、hybrid（多 rank CPU 段拼接）三种分配策略。 |
| [csrc/kernels/backend/api.cuh](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/api.cuh) | **`NCCLSymmetricMemoryContext` 结构体声明**，以及 NCCL comm/window/拓扑查询的函数声明。 |
| [csrc/kernels/backend/nccl.cu](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu) | **`NCCLSymmetricMemoryContext` 的实现**：复用 NCCL communicator、配置 Gin、分配对称内存、注册窗口、计算跨 rank 指针。 |
| [csrc/elastic/buffer.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp) | **`ElasticBuffer` 构造函数**：把 workspace + GPU buffer + CPU buffer 的字节数算好，交给 `NCCLSymmetricMemoryContext` 建立窗口，并把 workspace 指针取出来。 |
| [deep_ep/include/deep_ep/common/compiled.cuh](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/compiled.cuh) | 编译期常量，例如 Gin 队列深度 `kGinQPDepth`。 |

调用方向（自下而上）：`ElasticBuffer` 构造 → `NCCLSymmetricMemoryContext` 构造 → `symmetric::alloc`（VMM 分配）→ `ncclCommWindowRegister`（注册窗口）→ `ncclGetLsaDevicePointer`（取出跨 rank 指针表）。

## 4. 核心概念与源码讲解

### 4.1 NCCL Gin 后端：复用 communicator 的轻量化设计

#### 4.1.1 概念说明

DeepEP V1 用 **NVSHMEM** 做对称内存与 RDMA——这需要单独初始化一个 NVSHMEM world、单独分配堆、单独交换 IPC handle，笨重且与 PyTorch 的 NCCL communicator **并行存在**，资源重复。

V2 改用 **NCCL Gin 后端**。README 对它的描述是三条：**header-only（轻量）、能复用已有 NCCL communicator、把 dispatch/combine 内核需要的 RDMA 队列直接交给用户 kernel**（见 [README.md:14-16](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L14-L16)）。

- **header-only**：DeepEP 直接 `#include <nccl.h>` 和 `#include <nccl_device.h>`（见 [api.cuh:7-8](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/api.cuh#L7-L8)），Gin 的设备侧实现都在 NCCL 的头文件里，DeepEP 不额外发布 `.so`。
- **复用 communicator**：构造 `NCCLSymmetricMemoryContext` 时传入的是一个已有的 `nccl_comm`（int64 句柄），它**可能就是 PyTorch 的 NCCL communicator**（见 u8-l2 的复用机制），而不是新建一个独立的通信世界。
- **Gin**：NCCL 提供的设备级 RDMA 原语（`ncclGinRequest_t`、QP/queue pair），让自定义 kernel 能像发指令一样发起跨节点 RDMA 读/写。

#### 4.1.2 核心流程

`NCCLSymmetricMemoryContext` 构造函数中，与「复用 communicator + 配置 Gin」相关的步骤：

1. **接收一个已有 communicator 句柄**，强转回 `ncclComm_t`（不新建）。
2. 查询 NCCL 支持的 Gin 类型（`railedGinType` / `ginType`），若为 `NCCL_GIN_TYPE_NONE` 则断言失败（通常是网络配置问题，如多平面网络下误用 direct 模式）。
3. 填写 `ncclDevCommRequirements_t`：要多少个 QP、QP 深度、流量类（SL）、信号数、连接类型（RAIL/FULL）。
4. 调 `ncclDevCommCreate` 得到 device 侧 communicator `dev_comm`，从中读出真实的 NVLink 域大小（`lsaSize`）。
5. 之后才分配对称内存、注册窗口（见 4.3）。

#### 4.1.3 源码精读

复用已有 communicator——只是强转，不 `ncclCommInit`：

[nccl.cu:75-76](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L75-L76) 把传入的 int64 句柄直接当作 `ncclComm_t` 使用。这个句柄在 Python 侧由 `get_nccl_comm_handle` 提供（u8-l2 会讲它如何优先复用 PyTorch 的 communicator）。

配置 Gin 需求的核心片段：

[nccl.cu:83-101](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L83-L101) 逐字段含义：

- `ginContextCount = num_allocated_qps`：申请多少个 QP（direct 模式默认 17，hybrid 默认 65 或 129，见 u2-l2）。
- `ginExclusiveContexts = true`：每个 context 独占，避免并发互相干扰。
- `ginQueueDepth = kGinQPDepth`：每个队列能容纳多少个在途 RDMA 请求。`kGinQPDepth` 是编译期常量 `1024`，见 [compiled.cuh:84](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/compiled.cuh#L84)。
- `ginTrafficClass = sl_idx`：RDMA 服务级（Service Level），由 `EP_OVERRIDE_RDMA_SL` 控制，用于流量隔离（见 u8-l3）。
- `ginSignalCount = num_ranks + 2 * 2`：自定义 RDMA barrier 需要的信号量数（每个对端 1 个，加 2×2 的冗余，对应 barrier 的 sequential/并行两种模式，见 u7-l1）。
- `ginConnectionType`：hybrid 用 `NCCL_GIN_CONNECTION_RAIL`（多平面/多轨道友好），direct 用 `NCCL_GIN_CONNECTION_FULL`——这条与 u3-l1 讲的逻辑域选择完全对应。

注意第 84 行 `num_ranks > 1 and get_env("EP_DISABLE_GIN", 0) == 0` 这个守卫：当 `EP_DISABLE_GIN=1` 时（[README.md:343](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L343)），跳过整个 Gin 配置，回退到非 Gin 路径。这是排障开关。

device communicator 创建后，**真实的 NVLink 域大小才确定下来**：

[nccl.cu:104-107](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L104-L107) 用 `dev_comm.lsaSize`（LSA = Local Shareable Address，即 NVLink 共享寻址域）回填物理域大小，再按 u3-l1 的规则投影到逻辑域。

#### 4.1.4 代码实践

**实践目标**：观察 Gin 配置过程与 `EP_DISABLE_GIN` 的效果。

**操作步骤**（源码阅读 + 本地观察）：

1. 打开 [nccl.cu:62-101](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L62-L101)，确认「复用 communicator」就是一行强转，没有任何 `ncclCommInitRank`。
2. 在单机 8 卡环境，设置 `EP_BUFFER_DEBUG=1` 后跑一次 `tests/elastic/test_ep.py`。
3. 观察输出里 `EP NCCL device communicator has <N> allocated QPs` 这一行（来自 [nccl.cu:79-80](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L79-L80)），记录 N。
4. 在构造 `ElasticBuffer` 时分别传 `num_allocated_qps=0`（自动）和显式值，对比 N 的变化。
5. （选做）设置 `EP_DISABLE_GIN=1` 重跑，观察是否仍能正常完成 dispatch（多节点时大概率断言失败或回退）。

**需要观察的现象**：`EP_BUFFER_DEBUG` 输出的 NCCL 版本号、QP 数量；以及 `num_ranks==1`（单进程）时 Gin 分支被跳过、不会打印 QP 行。

**预期结果**：自动模式下，单节点 direct（`allow_hybrid_mode=False`）应得到 17 个 QP；`allow_hybrid_mode=True` 得到 65 或 129（取决于是否启用 fast RDMA atomic）。单进程（`num_ranks==1`）时由于第 84 行守卫，不创建 Gin。

> 待本地验证：QP 数量与 fast-atomic 支持的具体取值，受硬件与 NCCL 版本影响。

#### 4.1.5 小练习与答案

**练习 1**：为什么 DeepEP 选择复用 PyTorch 的 NCCL communicator，而不是像 V1 那样新建一个独立通信世界？

**参考答案**：复用避免了在同一组 GPU 上同时维护 NCCL 与 NVSHMEM 两套通信资源（communicator、QP、显存堆）的重复开销；header-only 意味着不引入新的外部动态库依赖；也让 DeepEP 的通信域天然与用户的 `dist.ProcessGroup` 对齐，省去二次握手。

**练习 2**：`EP_DISABLE_GIN=1` 在什么场景下有用？

**参考答案**：当 Gin 后端因网络配置（如多平面网络下误用 direct 模式，或 NCCL 版本不支持）而无法初始化时，用它做排障回退开关，强制走非 Gin 路径以定位问题是否出在 Gin 配置上。

---

### 4.2 对称内存分配：CUDA VMM 与 GPUDirect RDMA

#### 4.2.1 概念说明

`symmetric.hpp` 是一个**纯内存分配器**，它的任务只有一个：**分配一块所有 rank 形状一致、且可被 RDMA 网卡和所有 peer GPU 直接访问的内存**。

它提供三个类，对应三种场景（由 [symmetric.hpp:291-317](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp#L291-L317) 的 `alloc` 工厂按需选择）：

| 类 | 场景 | 布局 |
| --- | --- | --- |
| `GPUSymmetricMemory` | 纯 GPU（`num_cpu_bytes==0`） | 调 `ncclMemAlloc`，一块显存 |
| `ElasticSymmetricMemory` | GPU + 本地 CPU（direct 或单 rank） | `[GPU 段][本地 CPU 段]` 拼成连续 VA |
| `HybridElasticSymmetricMemory` | hybrid 多节点 | `[GPU 段][rank0 CPU | rank1 CPU | ...]`，把**所有同节点 rank 的 CPU 段**都映射进本 rank 的 VA |

三者都继承自基类 `SymmetricMemory`，对外暴露统一的 `ptr / num_bytes / num_gpu_bytes / num_cpu_bytes`（[symmetric.hpp:113-121](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp#L113-L121)）。

#### 4.2.2 核心流程

以 `ElasticSymmetricMemory`（GPU+CPU 混合分配）为例，VMM「三件套」流程：

1. **探测设备**：`DeviceContext` 拿到当前 GPU 序号与 NUMA 节点（[symmetric.hpp:22-31](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp#L22-L31)）。
2. **构造分配属性**：`gpu_alloc_prop()` 标记为「设备显存 + 可 GPUDirect RDMA」；`cpu_alloc_prop()` 标记为「NUMA 本地主机内存」（[symmetric.hpp:33-69](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp#L33-L69)）。
3. **预留虚拟地址区间**：`cuMemAddressReserve` 预留 `num_gpu_bytes + num_cpu_bytes` 的 VA。
4. **创建物理块并映射**：`cuMemCreate` 建 GPU 物理块 → `cuMemMap` 映射到 VA 前 `num_gpu_bytes`；CPU 段同理映射到 VA 后半。
5. **设置访问权限**：`cuMemSetAccess` 让**所有能 peer 访问本 GPU 的 GPU**（以及 CPU NUMA 节点）都对此 VA 有读写权限。
6. **FABRIC 回退**：若设备支持 `CU_MEM_HANDLE_TYPE_FABRIC` 但当前进程不允许，自动降级到 POSIX FD 句柄类型（`cumem_create_with_fallback`，[symmetric.hpp:73-87](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp#L73-L87)）。

数学上，整个窗口的字节数满足：

\[
\text{num\_bytes} = \text{num\_gpu\_bytes} + \text{num\_cpu\_bytes}
\]

而 hybrid 模式因为要把同节点所有 rank 的 CPU 段都映射进来：

\[
\text{num\_bytes}_{\text{hybrid}} = \text{num\_gpu\_bytes} + \text{num\_cpu\_bytes} \times \text{num\_scaleup\_ranks}
\]

这就是 u3-l1 提到的「hybrid 对称内存总字节随节点内 GPU 数放大」的来源（[symmetric.hpp:211](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp#L211)）。

#### 4.2.3 源码精读

**分配属性——关键在于 `gpuDirectRDMACapable` 与粒度校验**：

[symmetric.hpp:37-68](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp#L37-L68) 中：

- 第 59 行 `EP_HOST_ASSERT(flag and "GPUDirect RDMA with CUDA VMM is not supported")`：若设备不支持 GPUDirect RDMA，直接断言。这是跨节点 RDMA 的硬性前提。
- 第 60 行 `prop.allocFlags.gpuDirectRDMACapable = 1`：标记这块显存可被 RDMA 网卡直接访问——**这一行是 DeepEP 跨节点零拷贝的根基**。
- 第 64-67 行校验 `kNumAlignmentBytes % num_granularity_bytes == 0`：确保 2MB 对齐是设备粒度的整数倍。

**VMM 三件套——`ElasticSymmetricMemory` 构造函数**：

[symmetric.hpp:162-176](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp#L162-L176)：先 `cuMemAddressReserve` 预留总 VA，再分别 `cuMemCreate + cuMemMap` 把 GPU 段（前 `num_gpu_bytes`）和 CPU 段（后 `num_cpu_bytes`）映射进去，最后 `set_access` 给所有 peer GPU 授权。注意第 172 行 `cuMemMap(addr + num_gpu_bytes, ...)`——**CPU 段紧接在 GPU 段后面**，两段在虚拟地址上连续，但物理后端完全不同（显存 vs NUMA 内存）。

**跨进程共享 CPU 段——`HybridElasticSymmetricMemory`**：

[symmetric.hpp:223-249](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp#L223-L249)：同节点的每个 rank 都是一个独立进程，各自的 CPU 段物理内存在各自进程地址空间里。要把它「拼」到本 rank 的对称窗口里，必须跨进程传递文件描述符（FD）。这里用 `pidfd_open` / `pidfd_getfd`（Linux 系统调用）从兄弟进程「借」FD，再 `cuMemImportFromShareableHandle` 导入对应的物理内存句柄，映射到本 rank VA 的对应位置。`create_cpu_handle`（[symmetric.hpp:271-288](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp#L271-L288)）则是反向操作：创建本地 CPU 段并导出为 `(pid, fd)` 句柄，供 `dist.all_gather_object` 分发给同节点其他 rank（见 [elastic.py:337-342](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L337-L342)）。

**工厂函数与一个隐式副作用**：

[symmetric.hpp:291-317](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp#L291-L317) 的 `alloc` 按 `num_cpu_bytes` 与 `allow_hybrid_mode` 选择三个类之一。注意第 314-315 行：**只要分配结果可能是 CPU 后端的 `ElasticSymmetricMemory`，就 `setenv("NCCL_ELASTIC_BUFFER_REGISTER", "1")`**——这会指示 NCCL 在窗口注册时把 CPU 段也纳入可寻址范围（Engram 的 CPU 存储就依赖于此）。

#### 4.2.4 代码实践

**实践目标**：理解「纯 GPU」与「GPU+CPU」两条分配路径的切换条件。

**操作步骤**（源码阅读型）：

1. 读 [symmetric.hpp:299-310](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp#L299-L310)，画出 `alloc` 的三分支决策树（条件：`num_cpu_bytes > 0`？`allow_hybrid_mode`？）。
2. 对照 [symmetric.hpp:124-140](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp#L124-L140)（`GPUSymmetricMemory` 用 `ncclMemAlloc`）与 [symmetric.hpp:145-186](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp#L145-L186)（`ElasticSymmetricMemory` 用 VMM），找出二者在「分配方式」与「能否拼 CPU 段」上的根本差异。
3. 回答：一个只跑 dispatch/combine（不跑 Engram）的 `ElasticBuffer`，`num_cpu_bytes` 是多少？它走的是哪个分支？

**需要观察的现象**：决策树中 `num_cpu_bytes == 0` 时直接选 `GPUSymmetricMemory`，根本不会触碰 VMM API。

**预期结果**：不使用 Engram 时 `num_cpu_bytes=0`，走 `GPUSymmetricMemory`，即 `ncclMemAlloc` 路径；一旦传入 `num_cpu_bytes>0`（如 Engram 的 CPU 存储），才走 VMM 拼接路径，并触发 `NCCL_ELASTIC_BUFFER_REGISTER=1`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 GPU 段必须设置 `gpuDirectRDMACapable = 1`，而 CPU 段不需要？

**参考答案**：跨节点 RDMA 网卡要直接读写 GPU 显存，必须用支持 GPUDirect RDMA 的方式分配（VMM 的 `gpuDirectRDMACapable` 标志），否则网卡无法 pin/映射这块显存。CPU NUMA 内存本身就是主机内存、天然可被网卡经 PCIe/PIN 访问，不需要该标志。

**练习 2**：`HybridElasticSymmetricMemory` 为什么要把**同节点其他 rank 的 CPU 段**也映射进本 rank 的 VA？

**参考答案**：Engram 的 RDMA-get 拉取要直接从远端 rank 的 CPU 段读数据。若只映射本地 CPU 段，本 rank 的 GPU 无法用「偏移量」寻址到对端 CPU 段；把同节点所有 rank 的 CPU 段按固定顺序拼接进 VA 后，`offset = num_gpu_bytes + peer_rank * num_cpu_bytes` 就能定位到任意 peer 的 CPU 存储，对称寻址才成立。

---

### 4.3 对称内存窗口注册与跨 rank 寻址

#### 4.3.1 概念说明

`symmetric.hpp` 只负责「分配一块符合条件的内存」。但要让它真正成为**跨 rank 对称窗口**，还差两步，这两步在 `NCCLSymmetricMemoryContext` 构造函数里完成：

1. **`ncclCommWindowRegister`**：把这块内存注册成 NCCL 窗口。注册是**集合操作**——NCCL 内部会跨所有 rank 做 bootstrap barrier，交换彼此的窗口地址，并约定统一的「窗口内偏移」语义。
2. **`ncclGetLsaDevicePointer`**：从窗口里取出**本 rank 自己的设备指针**（`mapped_window_ptr`，作为偏移基准），以及**所有 NVLink peer 的设备指针表**（`nvl_window_ptrs`）。

注册完成后，`get_sym_ptr` 就能用「同一偏移」做跨 rank 寻址。

#### 4.3.2 核心流程

`NCCLSymmetricMemoryContext` 构造函数（[nccl.cu:62-140](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L62-L140)）后半段：

1. 调 `symmetric::alloc` 得到对称内存 `symmetric_memory`（4.2 已讲）。
2. `raw_window_ptr = symmetric_memory->ptr`，把 `num_gpu_bytes`/`num_cpu_bytes` 回填到 context。
3. `ncclCommWindowRegister(comm, raw_window_ptr, num_bytes, &window, NCCL_WIN_STRICT_ORDERING)`：注册窗口，**严格排序**模式保证窗口内写操作的可见顺序。
4. `ncclGetLsaDevicePointer(window, 0, nvl_rank_idx, &mapped_window_ptr)`：取本 rank 指针。
5. 循环对每个 NVLink peer `i` 调 `ncclGetLsaDevicePointer(window, 0, i, &nvl_window_ptrs[i])`：取所有 peer 指针。

跨 rank 寻址 `get_sym_ptr(ptr, dst_rank_idx)`（[nccl.cu:142-145](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L142-L145)）用「偏移换算」：

\[
\text{offset} = \text{ptr} - \text{mapped\_window\_ptr}
\]
\[
\text{dst\_ptr} = \text{nvl\_window\_ptrs}[\text{dst\_rank\_idx}] + \text{offset}
\]

#### 4.3.3 源码精读

**窗口注册——一行集合操作**：

[nccl.cu:129-132](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L129-L132)：`ncclCommWindowRegister` 用 `NCCL_WIN_STRICT_ORDERING` 注册。注释明确「它是 collective 的，内部已经 bootstrapBarrier，所以注册之后不需要再显式 barrier」——这解释了 u2-l2 里「构造末尾三同步」为什么不需要额外加一次 window barrier。

**取指针表——本 rank + 所有 NVLink peer**：

[nccl.cu:133-139](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L133-L139)：`mapped_window_ptr` 是本 rank 窗口基址；`nvl_window_ptrs` 是长度为 `num_nvl_ranks` 的数组，存每个 NVLink peer 的窗口基址。注意它只覆盖 **NVLink（LSA）域**，不覆盖 RDMA 域——所以 `get_sym_ptr` **只能寻址节点内的 NVLink peer**。跨节点的 scaleout 通信不走 `get_sym_ptr`，而是通过 Gin request（`ncclGinRequest_t`）发起，见 u5-l2/u7-l2。

**跨 rank 寻址——三行算术**：

[nccl.cu:142-145](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L142-L145)：

```cpp
const auto offset = static_cast<uint8_t*>(ptr) - static_cast<uint8_t*>(mapped_window_ptr);
return static_cast<uint8_t*>(nvl_window_ptrs[dst_rank_idx]) + offset;
```

这能成立的**前提**正是 4.2 的对称分配：所有 rank 的窗口大小、内部布局完全一致，所以「我窗口里偏移 N 的位置」和「peer 窗口里偏移 N 的位置」指向逻辑上对应的数据。

**它在哪被用**：AGRS 的 `all_gather`（[buffer.hpp:475](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L475)）和 `destroy_agrs_session`（[buffer.hpp:414](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L414)）都用 `get_sym_ptr` 把本 rank 的信号/数据槽地址换算成对端地址，然后直接 `cudaMemcpyBatchAsync` 或写信号量。这就是「直接写对端显存」的实现。

**析构——逆序释放**：

[nccl.cu:147-154](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L147-L154)：`finalize` 先 `ncclCommWindowDeregister` 注销窗口、`symmetric_memory.reset()`（触发 VMM 的 `cuMemUnmap`/`cuMemRelease`/`cuMemAddressFree`），再 `ncclDevCommDestroy`。`ElasticBuffer::destroy`（[buffer.hpp:152-166](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L152-L166)）会先做一次 barrier 保证所有在途通信完成，再调用它。

#### 4.3.4 代码实践

**实践目标**：追踪一次 `all_gather` 中 `get_sym_ptr` 的调用，理解跨 rank 直写的实现。

**操作步骤**（源码阅读型）：

1. 打开 [buffer.hpp:438-524](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L438-L524)（`all_gather`）。
2. 定位 [buffer.hpp:474-475](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L474-L475)：`dst_ptr = nccl_context->get_sym_ptr(math::advance_ptr(buffer, offset[j] + x.nbytes() * rank_idx), dst_rank_idx)`。
3. 解释：第一个参数是「本 rank 在 buffer 里的目标槽位地址」（相对 `mapped_window_ptr` 的偏移），返回值是「对端 rank 同一偏移的地址」。
4. 确认 [buffer.hpp:489](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L489) 的 `cudaMemcpyBatchAsync` 用这些 `dst_ptrs` 一次性把数据发到所有对端——**不走 NCCL 集合 API，而是直接 memcpy 到对端显存地址**。

**需要观察的现象**：`all_gather` 的目标地址全部来自 `get_sym_ptr`，源地址是本地 tensor；`cudaMemcpyBatchAsync` 配合 `cudaMemcpyFlagPreferOverlapWithCompute` 实现批量、可重叠的拷贝。

**预期结果**：能清楚说明「AGRS 的 all_gather 之所以能零拷贝直写对端，靠的就是 4.2 对称分配 + 4.3 窗口注册 + `get_sym_ptr` 偏移换算」这条链路。

> 待本地验证：跨节点（`num_nvl_ranks < num_ranks`）时 `get_sym_ptr` 仅对 NVLink peer 有效，RDMA peer 的 all_gather 行为——AGRS 当前断言 `num_nvl_ranks == num_ranks`（见 [buffer.hpp:386](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L386)），即只支持节点内。

#### 4.3.5 小练习与答案

**练习 1**：`get_sym_ptr` 为什么只对 NVLink（LSA）域的 peer 有效，不能用于任意 rank？

**参考答案**：`nvl_window_ptrs` 只收集了 `num_nvl_ranks` 个 NVLink peer 的窗口基址（[nccl.cu:137-139](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L137-L139)），下标越界即出错。跨节点的 RDMA peer 不在这张表里，其通信由 Gin request 完成，不走对称指针直写。

**练习 2**：注册窗口时为什么选 `NCCL_WIN_STRICT_ORDERING`？

**参考答案**：DeepEP 内核依赖窗口内写操作按发起顺序对其他 rank 可见（例如先写数据、再写到达信号量）。严格排序保证这一约定，避免重排导致「信号先到、数据未到」的竞态。

---

### 4.4 把 workspace+buffer 装进对称窗口：ElasticBuffer 构造函数

> 此模块是综合模块，把 4.1~4.3 串到 `buffer.hpp` 构造函数里，并直接回答本讲的实践任务。

#### 4.4.1 概念说明

`ElasticBuffer` 构造函数（[buffer.hpp:81-140](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L81-L140)）是「把所有字节数算对、再交给 `NCCLSymmetricMemoryContext`」的组装车间。这里有一组容易混淆的「字节数」，必须厘清：

| 名称 | 定义域 | 含义 |
| --- | --- | --- |
| `num_buffer_bytes` | 用户传入 | GPU buffer + CPU buffer（**不含 workspace**） |
| `num_gpu_buffer_bytes` | 派生 | `num_buffer_bytes - num_cpu_buffer_bytes` |
| `num_cpu_buffer_bytes` | 用户传入 | CPU buffer（如 Engram 存储） |
| `num_workspace_bytes` | 派生 | workspace，向上对齐 2MB |
| `num_sym_bytes` | 派生 | `num_workspace_bytes + num_buffer_bytes`（**传给 context 的总字节**） |

而在 `NCCLSymmetricMemoryContext` 内部（对称内存视角），又有：

| 名称 | 含义 |
| --- | --- |
| `num_gpu_bytes` | `num_workspace_bytes + num_gpu_buffer_bytes`（窗口的 GPU 段，含 workspace） |
| `num_cpu_bytes` | `num_cpu_buffer_bytes`（窗口的 CPU 段） |

最终内存布局（[buffer.hpp:21](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L21) 注释）：

```
[[[ Workspace ] GPU buffer ] CPU buffer ]
 ^                                                ^
 mapped_window_ptr                                窗口末尾
```

#### 4.4.2 核心流程

1. 校验对齐：`num_buffer_bytes` 与 `num_cpu_buffer_bytes` 都必须是 2MB 倍数（[buffer.hpp:98-100](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L98-L100)）。
2. 算 workspace 字节：`WorkspaceLayout::get_num_bytes()` 向上对齐 2MB（[buffer.hpp:103-105](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L103-L105)）。
3. `num_sym_bytes = num_workspace_bytes + num_buffer_bytes`，传给 `NCCLSymmetricMemoryContext` 构造，CPU 字节传 `num_cpu_buffer_bytes`（[buffer.hpp:107-114](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L107-L114)）。
4. 在 context 内部，`symmetric::alloc(num_sym_bytes - num_cpu_buffer_bytes, num_cpu_buffer_bytes, ...)`——即 GPU 段 = workspace + GPU buffer，CPU 段 = CPU buffer（[nccl.cu:121-124](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L121-L124)）。
5. 校验回填一致：`num_workspace_bytes + num_gpu_buffer_bytes == num_gpu_bytes` 且 `num_cpu_buffer_bytes == num_cpu_bytes`（[buffer.hpp:116-118](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L116-L118)）。
6. `workspace = mapped_window_ptr`（窗口最前），`buffer = workspace + num_workspace_bytes`（[buffer.hpp:126-129](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L126-L129)）。
7. `cudaMemset(workspace, 0, num_workspace_bytes)`：**workspace 必须清零**（[buffer.hpp:130](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L130)）。

字节数关系总公式：

\[
\text{num\_sym\_bytes} = \underbrace{\text{num\_workspace\_bytes}}_{\text{GPU 段前部}} + \underbrace{\text{num\_gpu\_buffer\_bytes}}_{\text{GPU 段后部}} + \underbrace{\text{num\_cpu\_buffer\_bytes}}_{\text{CPU 段}}
\]

\[
\text{num\_gpu\_bytes} = \text{num\_workspace\_bytes} + \text{num\_gpu\_buffer\_bytes}
\]

#### 4.4.3 源码精读

**workspace 为何放在最前并对齐 2MB**——三个理由：

1. **`mapped_window_ptr` 就是窗口基址**。把 workspace 放在最前（[buffer.hpp:126](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L126)），意味着 workspace 的地址 = 窗口基址，跨 rank 的 `get_sym_ptr` 寻址最直接、最可预测（信号量、计数器等都落在 workspace 里，见 u3-l2 的 `WorkspaceLayout`）。
2. **2MB 对齐 = CUDA VMM 分配粒度**（4.2 已述）。窗口基址必须按粒度对齐，RDMA 大块传输与 TMA 批量拷贝才能高效；workspace 占据对齐的前部，保证紧随其后的 `buffer` 也落在干净的对齐边界上。
3. **workspace 必须清零**（[buffer.hpp:130](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L130)）：workspace 里的计数器、信号量、链表头都假定初值为 0（如 dispatch 的 `psum_num_recv_tokens`、AGRS 的信号量）。注释 [buffer.hpp:32](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L32) 明确「for all workspace, we must keep them as zeros」。

**为什么 `num_bytes` 不含 workspace**（[buffer.hpp:20](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L20) 注释）：用户关心的是「我的 dispatch/combine 数据要占多大」，workspace 是 DeepEP 的内部控制平面开销，由 `WorkspaceLayout::get_num_bytes()` 自动加上。这样 `get_buffer_size_hint` / `calculate_elastic_buffer_size`（u3-l2）返回的「用户视角」字节就只计 buffer，干净直观。

#### 4.4.4 代码实践

**实践目标**（本讲指定实践任务）：在 `buffer.hpp` 构造函数中找到 `NCCLSymmetricMemoryContext` 的创建处，梳理 `num_sym_bytes / num_gpu_bytes / num_cpu_bytes` 三者关系，并解释 workspace 为何放在最前并对齐 2MB。

**操作步骤**：

1. 打开 [buffer.hpp:81-140](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L81-L140)，定位构造函数。
2. 找到 [buffer.hpp:110-114](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L110-L114)：
   - `num_sym_bytes = num_workspace_bytes + num_buffer_bytes`
   - 传给 `NCCLSymmetricMemoryContext` 的 `num_bytes=num_sym_bytes`、`num_cpu_bytes=num_cpu_buffer_bytes`。
3. 追踪进 [nccl.cu:121-124](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L121-L124)：context 调 `symmetric::alloc(num_sym_bytes - num_cpu_buffer_bytes, num_cpu_buffer_bytes, ...)`，即 GPU 段 = `num_sym_bytes - num_cpu_buffer_bytes` = workspace + GPU buffer，CPU 段 = `num_cpu_buffer_bytes`。
4. 回到 [buffer.hpp:116-118](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L116-L118) 的两条断言，验证：`num_workspace_bytes + num_gpu_buffer_bytes == num_gpu_bytes` 与 `num_cpu_buffer_bytes == num_cpu_bytes`。
5. 记录 [buffer.hpp:126-130](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L126-L130)：`workspace = mapped_window_ptr`（窗口最前），紧接清零。

**需要观察的现象**：用户传入的 `num_bytes` 经过「加 workspace → 拆 GPU/CPU 段 → context 再拆给 symmetric::alloc → 回填校验」四步，最终保证 `[[[ Workspace ] GPU buffer ] CPU buffer]` 布局成立。

**预期结果**：能写出三者的完整等式：

\[
\text{num\_sym\_bytes} = \text{num\_workspace\_bytes} + \text{num\_buffer\_bytes}
\]
\[
\text{num\_gpu\_bytes} = \text{num\_sym\_bytes} - \text{num\_cpu\_buffer\_bytes} = \text{num\_workspace\_bytes} + \text{num\_gpu\_buffer\_bytes}
\]
\[
\text{num\_cpu\_bytes} = \text{num\_cpu\_buffer\_bytes}
\]

并解释 workspace 放最前（= 窗口基址，便于跨 rank 对称寻址）、对齐 2MB（= VMM 粒度，利于 TMA/RDMA）、必须清零（计数器/信号量初值为 0）三点理由。

#### 4.4.5 小练习与答案

**练习 1**：用户传 `num_bytes=64MB`、`num_cpu_bytes=8MB`，构造函数里 `num_gpu_buffer_bytes`、`num_sym_bytes`、context 的 `num_gpu_bytes` 分别是多少？（假设 workspace 对齐后是 2MB）

**参考答案**：`num_gpu_buffer_bytes = 64 - 8 = 56MB`；`num_sym_bytes = 2 + 64 = 66MB`；context 的 `num_gpu_bytes = num_sym_bytes - num_cpu_bytes = 66 - 8 = 58MB`（= workspace 2MB + GPU buffer 56MB）。

**练习 2**：如果 workspace 不清零，最先出错的会是什么场景？

**参考答案**：dispatch 的 CPU 同步轮询（u5-l4）会读到 `host_workspace` 里未初始化的计数，AGRS/barrier 的信号量也会从随机值起步，导致误判「数据已到」或死锁。所以构造时 `cudaMemset(workspace, 0, ...)` 与 `std::memset(host_workspace, 0, ...)`（[buffer.hpp:130,135](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L130)）是必须的。

## 5. 综合实践

**任务**：画出 DeepEP V2「对称窗口」从无到有的完整建立链路，并用一次 Engram 或 AGRS 的调用验证它确实工作。

**步骤**：

1. **画链路图**（把本讲四个模块串起来），标注每一步对应的源码位置：
   - `ElasticBuffer.__init__`（Python）→ `_C.ElasticBuffer` 构造
   - 算 `num_workspace_bytes`（对齐 2MB）→ `num_sym_bytes`
   - `NCCLSymmetricMemoryContext` 构造：复用 `nccl_comm` → `ncclDevCommCreate`（配 Gin）→ 读 `lsaSize` 定拓扑
   - `symmetric::alloc`：按 `num_cpu_bytes`/`allow_hybrid_mode` 选 `GPU`/`Elastic`/`HybridElastic` → VMM 分配（`gpuDirectRDMACapable`）
   - `ncclCommWindowRegister`（集合，含 bootstrap barrier）→ `ncclGetLsaDevicePointer`（取 `mapped_window_ptr` 与 `nvl_window_ptrs`）
   - 回填 workspace/buffer 指针 + 清零
2. **运行验证**：在单机 8 卡跑 `tests/elastic/test_agrs.py`（依赖 `get_sym_ptr` 直写）或 `tests/elastic/test_engram.py`（依赖 CPU 段 + RDMA get）。设置 `EP_BUFFER_DEBUG=1`，确认输出里出现 `EP NCCL device communicator has <N> allocated QPs`。
3. **对照分析**：在 `test_agrs.py` 的 `all_gather` 调用处打断点/加打印，确认目标地址都来自 `get_sym_ptr`，而非 NCCL 集合 API。
4. **写一段说明**：用本讲的术语解释「为什么 AGRS 能在不用 `dist.all_gather` 的情况下，把本 rank 的数据复制到所有对端的对应槽位」。

**预期产出**：一张链路图 + 一段说明，要点是「对称分配（同形）→ 窗口注册（约定偏移语义）→ `get_sym_ptr`（偏移换算）→ 直写对端显存」。

> 待本地验证：`test_agrs.py` 与 `test_engram.py` 的具体输出格式与是否需要多节点环境（Engram 的 RDMA get 至少需要两个 scaleout rank 才能体现跨节点拉取；AGRS 仅支持节点内）。

## 6. 本讲小结

- **NCCL Gin 后端**是 V2 取代 NVSHMEM 的轻量底座：header-only（直接 include NCCL 头）、复用已有 `ncclComm_t`（不新建通信世界）、通过 `ncclDevCommCreate` 把 RDMA 队列（QP）直接交给自定义内核。
- **对称内存**的核心约定：所有 rank 分配大小/布局一致的缓冲区并注册成窗口，使「同一偏移量」在每个 rank 指向逻辑对应的数据。
- `symmetric.hpp` 用 **CUDA VMM**（`cuMemAddressReserve`/`cuMemCreate`/`cuMemMap`/`cuMemSetAccess`）分配可 **GPUDirect RDMA** 的显存，并能把 GPU 段与 CPU 段（甚至多 rank 的 CPU 段）拼成一段连续 VA。
- 窗口注册（`ncclCommWindowRegister`，集合操作、含 bootstrap barrier）+ `ncclGetLsaDevicePointer`（取本 rank 与所有 NVLink peer 的基址表）共同支撑了 `get_sym_ptr` 的**偏移换算**式跨 rank 寻址。
- `get_sym_ptr` 只对 **NVLink（LSA）域** peer 有效；跨节点 scaleout 通信走 Gin request，不走对称指针直写。
- `ElasticBuffer` 构造函数把 `[[[ Workspace ] GPU buffer ] CPU buffer]` 装进窗口：`num_sym_bytes = workspace + buffer`，workspace 放最前（= 窗口基址）、对齐 2MB（= VMM 粒度）、必须清零（计数器/信号量初值）。

## 7. 下一步学习建议

- **u5-l1 / u5-l2（Dispatch 内核链路）**：看 dispatch 内核如何用本讲建立的 `window` / `dev_comm` 经 NVLink 直写对端 buffer、经 Gin request 发起跨节点 RDMA。
- **u7-l1（Barrier）**：看 `launch_barrier` 如何复用本讲的对称窗口信号量（`ginSignalCount`）做全 rank GPU 级同步。
- **u7-l2（Engram）**：看 RDMA-get 如何直接从对端的 **CPU 段**（4.2 的 hybrid 对称内存）拉取数据。
- **u7-l4（AGRS）**：看 `all_gather` 如何用 `get_sym_ptr` + `cudaMemcpyBatchAsync` 实现零拷贝节点内聚合。
- **u8-l2（NCCL communicator 复用）**：深入 Python 侧 `get_nccl_comm_handle`，理解本讲「复用」的那个 `nccl_comm` 句柄从哪来、何时强制新建。
