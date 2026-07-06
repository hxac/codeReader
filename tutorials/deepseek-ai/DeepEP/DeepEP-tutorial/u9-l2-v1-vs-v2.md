# V1 vs V2 架构演进与对比

## 1. 本讲目标

本讲是「遗留 V1 与架构对比」单元的收尾。在 u9-l1 中我们已经逐行拆解了 V1 `Buffer` 的双缓冲、NVSHMEM/IPC 初始化与三类内核；本讲把视角拉高，**系统性对比 V1 与 V2 在五个维度上的架构演进**：

1. 通信后端：NVSHMEM → NCCL Gin；
2. 编译方式：安装期编译 → 运行时 JIT；
3. SM/QP 决策：`config_map` 查表（auto-tuning）→ 带宽建模解析式；
4. 缓冲区与句柄接口：双 buffer + tuple handle → 统一 `ElasticBuffer` + `EPHandle`；
5. 性能、SM 占用与可扩展性（EP2048）。

学完后你应当能够：

- 说清 V2 用 NCCL Gin 取代 NVSHMEM 的动机（header-only、复用 NCCL communicator、不再依赖 NVSHMEM 运行时）；
- 解释「auto-tuning → 解析式」与「双 buffer → ElasticBuffer」给用户接口带来的简化；
- 从性能、SM 占用、最大 EP 规模、低延迟支持四个角度，给出 V1/V2 的定量对比；
- 指出 V2 明确「不再支持」的特性，避免在迁移时踩坑。

## 2. 前置知识

阅读本讲前，建议你已经掌握（对应前置讲义摘要）：

- **EP/dispatch/combine 的含义**（u1-l1）：dispatch 把 token 发往目标专家所在 rank，combine 把专家输出按权重归约回原 rank。
- **NVLink（节点内）与 RDMA（节点间）两类物理链路**（u1-l1、u3-l1）。
- **V1 `Buffer` 的内部结构**（u9-l1）：`num_nvl_bytes` + `num_rdma_bytes` 双缓冲、NVSHMEM 分配 RDMA 对称显存、CUDA IPC handle 同步 NVLink buffer、intranode/internode/low_latency 三类内核、两步式 `get_dispatch_layout` + `dispatch(config=...)`。
- **V2 `ElasticBuffer` 的统一接口**（u2-l2、u2-l3）：`EPHandle` 路由元数据、一步式 `dispatch`、`get_theoretical_num_sms` 解析式 SM 计算。
- **JIT 编译系统**（u4 系列）：运行时把模板参数烘焙成常量再实例化内核。

几个本讲反复用到、值得先明确的术语：

- **后端（backend）**：底层用来做跨 GPU 通信的机制。V1 用 NVSHMEM 库 + CUDA IPC；V2 用 NCCL 内置的 **Gin**（GPU intra-node，NCCL 2.x 引入的对称内存/远端写后端）。
- **auto-tuning**：V1 用一张「rank 数 → Config」的查表给出 chunk 大小等调优参数，表里的值是作者在自己的集群上跑出来的经验值，注释里写着 `# TODO: automatically tune`。
- **解析式（analytical）**：V2 不查表，而是用带宽建模公式直接算出最优 SM/QP 数，无需预热运行。
- **QP（Queue Pair）**：RDMA 的发送/接收队列对，是 RDMA 通信的基本调度单元，数量影响门铃（doorbell）开销与并发度。

## 3. 本讲源码地图

本讲围绕「对比」展开，引用的真实源码文件如下：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md) | V2 官方说明：新特性、性能表、明确「不再支持」的特性 |
| [docs/legacy.md](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/docs/legacy.md) | V1 归档文档：性能数据、两步式接口示例、低延迟内核 |
| [deep_ep/buffers/legacy.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/legacy.py) | V1 `Buffer` 的 Python 封装：构造、`set_num_sms`、`get_dispatch_config`、两步式 dispatch |
| [deep_ep/buffers/elastic.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py) | V2 `ElasticBuffer`/`EPHandle`：一步式 dispatch、`get_theoretical_num_sms/qps` |
| [csrc/legacy/config.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/legacy/config.hpp) | V1 `Config` 结构体与缓冲区尺寸提示（chunked recv/send tokens） |
| [csrc/legacy/buffer.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/legacy/buffer.hpp) | V1 C++ 实现：NVSHMEM init/alloc/barrier、IPC handle 同步 |
| [csrc/elastic/buffer.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp) | V2 C++ 实现：`NCCLSymmetricMemoryContext` 创建、对称窗口布局 |

---

## 4. 核心概念与源码讲解

### 4.1 后端演进：NVSHMEM → NCCL Gin

#### 4.1.1 概念说明

EP 的 dispatch/combine 本质是 all-to-all：每个 rank 都要往其余 rank 的显存里写数据。要做到这一点，需要一种**让 GPU 内核直接写对端 rank 显存**的机制，这就是「对称内存（symmetric memory）+ 远端写」后端。

- **V1 用 NVSHMEM**：NVSHMEM 是 NVIDIA 的 PGAS 对称内存库，提供 `nvshmem_malloc` 分配对称显存、`nvshmem_ptr` 跨 rank 寻址、IBGDA（InfiniBand GPUDirect Async）通道让 GPU 内核直接发起 RDMA。它的能力强（支持低延迟纯 RDMA），但**重**：需要单独安装、有自己的初始化协议（unique id 广播）、占用一组 NCCL 之外的通信资源。节点内 NVLink buffer 则用更轻的 **CUDA IPC handle**（本 rank `cudaMalloc`+导出，peer `open_mem_handle` 导入）。
- **V2 用 NCCL Gin**：Gin 是 NCCL 2.x 内置的对称内存后端。DeepEP V2 直接**复用已有的 NCCL communicator**，在其上建立对称内存窗口（GPU+CPU），跨 rank 寻址用 `gin.get_sym_ptr`。它是 header-only 的，不需要 NVSHMEM 运行时。

README 在 New features 里把这一点列为头号变化：

> **NCCL Gin backend** — Header-only & lightweight, Able to reuse existing NCCL communicators（[README.md:14-16](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L14-L16)）

#### 4.1.2 核心流程

**V1 初始化（重）**：Python 侧先编排一长串 `NVSHMEM_*` 环境变量（IBGDA、QP 数、team 上限、显存粒度……），广播 root unique id，C++ `sync` 里完成 `nvshmem::init` → `nvshmem::alloc` → `nvshmem::barrier`，见 [deep_ep/buffers/legacy.py:104-135](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/legacy.py#L104-L135) 与 [csrc/legacy/buffer.hpp:255-284](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/legacy/buffer.hpp#L255-L284)。NVLink buffer 另走 IPC handle 路径，两套机制并存。

**V2 初始化（轻）**：Python 拿到 `ProcessGroup` 后调 `get_nccl_comm_handle` 复用/自建 NCCL communicator，C++ 构造函数里只新建一个 `NCCLSymmetricMemoryContext` 即可，所有 rank 共享同一套 NCCL 资源，没有 NVSHMEM 那套环境变量与 unique id 广播，见 [csrc/elastic/buffer.hpp:107-114](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L107-L114)。

#### 4.1.3 源码精读

V1 NVSHMEM 初始化的核心片段（构造时由 root 生成 unique id，所有 rank 用它 init，再 alloc + barrier）：

```cpp
// csrc/legacy/buffer.hpp:255-268（节选）
// Initialize NVSHMEM
auto nvshmem_rank = low_latency_mode ? rank : rdma_rank;
auto num_nvshmem_ranks = low_latency_mode ? num_ranks : num_rdma_ranks;
EP_HOST_ASSERT(nvshmem_rank == nvshmem::init(root_unique_id, nvshmem_rank, num_nvshmem_ranks, ...));
rdma_buffer_ptr = nvshmem::alloc(num_rdma_bytes, LEGACY_NUM_BUFFER_ALIGNMENT_BYTES);
```

对应永久链接：[csrc/legacy/buffer.hpp:255-284](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/legacy/buffer.hpp#L255-L284)（NVSHMEM init/alloc/barrier）。这段代码说明 V1 必须先完成 NVSHMEM 集群握手才能用 RDMA buffer。

V2 则把对称内存交给 NCCL Gin，构造函数里一行就建立上下文：

```cpp
// csrc/elastic/buffer.hpp:107-114（节选）
// Create NCCL symmetric memory context
// Symmetric memory layout: [[[Workspace] GPU buffer] CPU buffer]
const auto num_sym_bytes = num_workspace_bytes + num_buffer_bytes;
this->nccl_context = std::make_shared<nccl::NCCLSymmetricMemoryContext>(
    nccl_comm, cpu_comm, num_ranks, rank_idx,
    num_sym_bytes, num_cpu_buffer_bytes,
    allow_hybrid_mode, sl_idx, num_allocated_qps);
```

对应永久链接：[csrc/elastic/buffer.hpp:107-118](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L107-L118)。`nccl_comm` 就是 Python 通过 `get_nccl_comm_handle` 传进来的、复用自 PyTorch 的 NCCL communicator——这是「轻量」的根本来源。

#### 4.1.4 代码实践

**实践目标**：用「源码阅读 + 文档对照」体会两套后端的依赖差异。

**操作步骤**：

1. 打开 [docs/legacy.md:54-56](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/docs/legacy.md#L54-L56)，确认 V1「Download and install NVSHMEM dependency」是硬依赖；而 [README.md:82-84](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L82-L84) 中 V2 把 NVSHMEM 降级为「仅 legacy 方法需要」。
2. 在 [deep_ep/buffers/legacy.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/legacy.py) 中搜索 `NVSHMEM_`，数一数 V1 在构造时设置了多少个 NVSHMEM 环境变量（如 `NVSHMEM_IB_ENABLE_IBGDA`、`NVSHMEM_QP_DEPTH`、`NVSHMEM_MAX_TEAMS` 等）。
3. 在 [deep_ep/buffers/elastic.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py) 中确认 V2 构造函数里没有任何 `NVSHMEM_` 字样，取而代之的是 `get_nccl_comm_handle`。

**需要观察的现象**：V1 构造路径里穿插大量 NVSHMEM 环境变量编排与 unique id 广播；V2 构造路径干净，只有 NCCL communicator 句柄与对称内存上下文。

**预期结果**：你会直观感受到「header-only、复用 communicator」意味着删掉了整条 NVSHMEM 运行时依赖链。本步骤为纯源码阅读，无需运行，故标记「待本地验证」仅针对性能侧的量化结论。

#### 4.1.5 小练习与答案

**练习 1**：V1 为什么节点内 NVLink buffer 不走 NVSHMEM 而走 CUDA IPC handle？
**参考答案**：节点内 NVLink 通信不需要 RDMA 协议栈，CUDA IPC handle（导出/导入显存句柄）足够轻、足够快；NVSHMEM 只在需要跨节点 RDMA（或低延迟纯 RDMA）时才引入，以避免每个 buffer 都背上 NVSHMEM 的初始化开销。参见 u9-l1 对 IPC handle 路径的讲解。

**练习 2**：NCCL Gin「能复用已有 NCCL communicator」对用户有什么实际好处？
**参考答案**：用户已有的 `torch.distributed` ProcessGroup 背后的 NCCL communicator 可以直接被 DeepEP 复用，无需为 EP 单独建立一套通信集群与生命周期管理；同时也便于和其它 NCCL 集合通信操作共存于同一进程。

---

### 4.2 编译方式演进：安装期编译 → 运行时 JIT

#### 4.2.1 概念说明

DeepEP 想把 SM 数、rank 数、hidden 字节数、专家数、top-k 等十多个**运行时才确定的参数**烘焙成 C++ 模板常量，换取寄存器/共享内存/循环展开的极致优化。这带来一个矛盾：参数运行时才确定，但模板特化最好在编译期。

- **V1：安装期编译**。所有内核源码都在 `csrc/legacy/` 下，`pip install` 时由 `setup.py` 一次性编进 `deep_ep._C.so`。优点是安装后即用；缺点是内核里只能用运行时变量，无法把上述参数编译期化，优化空间受限。
- **V2：运行时 JIT**。真正的内核模板放在 `deep_ep/include/impls/*.cuh`（如 `dispatch.cuh`），作为 header-only 随包发布；首次调用时，JIT 子系统按运行时参数生成一份 `.cu`、实例化特定模板、编译成 `.cubin` 并加载（参见 u4 系列）。

README 把「Fully JIT」列为第一条新特性，并强调「no CUDA compilation during installation」（[README.md:3](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L3)、[README.md:13](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L13)）。

#### 4.2.2 核心流程

V2 JIT 的「生成—编译—加载—缓存」端到端流程（细节见 u4-l1～u4-l4）：

```
dispatch(num_sms, num_ranks, hidden, ...)
   └─> LaunchRuntime::generate_impl   // 用 fmt::format 把参数填进模板尖括号
         └─> reinterpret_cast<void*>(&dispatch_impl<...>)  // ODR-use 强制实例化
               └─> Compiler::build    // 内容寻址缓存命中？
                     ├─ 命中: 直接返回 KernelRuntime
                     └─ 未命中: 编到 tmp 目录 → fsync → 原子 rename → cuobjdump 取符号 → 加载 cubin
```

V1 没有这条链路：内核在安装时已经编好，运行时直接调用。

#### 4.2.3 源码精读

V2 的「真内核」与「启动器」分居两处，正是 JIT 架构的体现。本讲不深入内核细节（那是 u5/u6 的任务），只指出**文件位置**这一最直观的差异：

- 真正的 dispatch 内核模板在 `deep_ep/include/deep_ep/impls/dispatch.cuh`（header-only，随包发布、运行时实例化）；
- 启动器 `launch_dispatch`（负责 generate + build + launch）在 `csrc/kernels/elastic/dispatch.hpp`（安装期编译进 `_C.so`）。

而 V1 的内核与启动器都在 `csrc/legacy/` 下、安装期一起编译。这条「启动器在 csrc、真内核在 include」的分工，是 V2 选择 JIT 的直接结果（u1-l2 已建立此认知）。

#### 4.2.4 代码实践

**实践目标**：从文件落点验证「安装期编译 vs 运行时 JIT」的差异。

**操作步骤**：

1. 用 `git ls-files csrc/legacy/ csrc/elastic/ csrc/kernels/elastic/ deep_ep/include/deep_ep/impls/` 列出文件（可在本地仓库执行）。
2. 观察：V1 的内核源（如 `csrc/legacy/` 下的 dispatch/combine 实现）全部在 `csrc/`，会被 `setup.py` 编进 `_C.so`；V2 的真内核 `dispatch.cuh`/`combine.cuh` 等在 `deep_ep/include/`，是 header-only。
3. 设置 `EP_JIT_DEBUG=1` 跑一次 `tests/elastic/test_ep.py`（本地有 Hopper 8 卡时），观察首次 dispatch 触发的 nvcc 编译命令；第二次跑应命中缓存无编译。

**需要观察的现象**：V2 首次运行会有一次 JIT 编译耗时（随后命中磁盘缓存）；V1 不存在运行时编译。

**预期结果**：理解「为何 V2 安装快（无 CUDA 编译）、首次运行略慢（JIT）、后续快（缓存）」。若无 GPU 环境，步骤 3 标记「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么把参数编译期化能提升性能？
**参考答案**：编译期常量让 nvcc 能精确计算共享内存布局、完全展开循环、配合 `__launch_bounds__` 与 `--register-usage-level` 压低寄存器占用，从而在更少 SM 上达成同等带宽（u4-l2 详述）。

**练习 2**：JIT 会不会让每次调用都重新编译？
**参考答案**：不会。JIT 用「内核签名 + 源码 + 头文件哈希」做内容寻址缓存（两级：进程内 `KernelRuntimeCache` + 磁盘目录），同输入必命中；只有参数或头文件变化才重编（u4-l3）。

---

### 4.3 SM/QP 决策演进：auto-tuning config → 解析式计算

#### 4.3.1 概念说明

dispatch/combine 内核要用多少 SM、多少 RDMA QP，直接决定性能与对计算流的抢占。两代决策方式截然不同：

- **V1：查表 auto-tuning**。`Buffer.get_dispatch_config(num_ranks)` 从一张 `config_map: num_ranks → Config` 的硬编码表里取值。`Config` 含 5 个字段（SM 数、NVLink/RDMA 的 chunked send/recv token 数）。表里的值是作者在自家集群上经验调出的，注释里写着 `# TODO: automatically tune`——换集群、换规模就得重跑测试重填表。
- **V2：解析式计算**。`ElasticBuffer.get_theoretical_num_sms(num_experts, num_topk)` 用带宽建模公式直接算出最优 SM 数，无需任何预热；`get_theoretical_num_qps(num_sms)` 再由 SM 数推出 QP 数。

README 把这一点列为 EPv2 的关键改进：「Analytical SM & QP count calculation — no more auto-tuning needed」（[README.md:20](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L20)）。

#### 4.3.2 核心流程

**V1 决策流程**：

```
Buffer.set_num_sms(24)                       # 全局静态 SM 数（默认 20）
config = Buffer.get_dispatch_config(group.size())   # 查 config_map 表
_buffer.dispatch(..., config=config)         # 把 Config 传进去
```

**V2 决策流程**：

```
num_sms = buffer.get_theoretical_num_sms(num_experts, num_topk)   # 解析式
num_qps = buffer.get_theoretical_num_qps(num_sms)                  # 解析式
_buffer.dispatch(..., num_sms=num_sms)            # 也可每次调用覆盖
```

V2 的 SM 建模思路（u3-l3 详述）：以 epilogue 读总量 \(V\) 为归一化单位，累加 `sm_read/sm_write/rdma_traffic/nvlink_traffic` 四个流量分数，找到流量/带宽比最大的瓶颈链路，令「HBM 搬运时间 = 链路传输时间」，解出 SM 数。其中 \(V\) 与真实 token 数会被约掉，再用一个均衡门控下的「期望 top-k」估算跨 rank 流量。

期望 top-k 的组合数公式（均衡门控下，一个 token 跨多少个 rank）：

\[
\mathrm{E}[\text{topk}] = G \cdot \left(1 - \frac{\binom{E - E/G}{K}}{\binom{E}{K}}\right)
\]

其中 \(E\) 为专家总数、\(G\) 为分组数、\(K\) 为 top-k。该公式仅对均衡门控成立，对 DeepSeek-V3 的 group-limited gate 不适用（代码注释明确声明）。

#### 4.3.3 源码精读

V1 的查表（注意 `# TODO: automatically tune` 与按 `num_ranks` 取值，且只覆盖到 160 rank）：

```python
# deep_ep/buffers/legacy.py:245-260（节选）
# TODO: automatically tune
config_map = {
    2:  Config(Buffer.num_sms, 24, 256, 6, 128),
    8:  Config(Buffer.num_sms, 6, 256, 6, 128),
    16: Config(Buffer.num_sms, 36, 288, 20, 128),
    ...
    160: Config(Buffer.num_sms, 28, 720, 12, 128),
}
assert num_ranks in config_map, f'Unsupported number of EP ranks: {num_ranks}'
return config_map[num_ranks]
```

对应永久链接：[deep_ep/buffers/legacy.py:232-260](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/legacy.py#L232-L260)。`Config` 的 5 个字段定义见 [csrc/legacy/config.hpp:24-51](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/legacy/config.hpp#L24-L51)（`num_sms` + 4 个 chunked token 上限）。这张表说明：V1 的最优参数是「按 rank 数查表」得到的经验值，rank 数不在表里就直接断言报错。

V2 的解析式（选瓶颈链路、解出 SM 数、再按 `prefer_overlap_with_compute` 决定是否压到很少 SM）：

```python
# deep_ep/buffers/elastic.py:808-825（节选）
# Found the bounded one
if self.num_scaleout_ranks > 1 and (rdma_traffic / rdma_gbs) > (nvlink_traffic / nvlink_gbs):
    bounded_traffic, bounded_gbs = rdma_traffic, rdma_gbs
else:
    bounded_traffic, bounded_gbs = nvlink_traffic, nvlink_gbs

num_sms = num_device_sms  # No traffic, e.g., EP=1
if bounded_traffic > 0:
    num_sms = max(
        bounded_gbs / bounded_traffic * sm_read / sm_read_gbs,
        bounded_gbs / bounded_traffic * sm_write / sm_write_gbs,
    )
num_sms = align(max(4, math.ceil(num_sms * 1.25)), 2)
num_sms = num_sms if self.prefer_overlap_with_compute else max(num_sms, 64)
```

对应永久链接：[deep_ep/buffers/elastic.py:728-834](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L728-L834)。整段没有任何查表，只依赖带宽（`rdma_gbs/nvlink_gbs`、`sm_read_gbs/sm_write_gbs`）与拓扑（`num_scaleout_ranks` 等），所以换规模无需重调。

QP 决策同理：V2 直接模式用 `min(num_sms, 8)+1` 省 doorbell、hybrid 模式用 `num_sms*16+1` 给每 channel 独立 QP，最终被构造期 `num_allocated_qps`（direct 17 / hybrid 65 或 129）封顶，见 [deep_ep/buffers/elastic.py:836-853](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L836-L853)。

#### 4.3.4 代码实践

**实践目标**：对比「查表」与「解析式」两种决策方式的可扩展性。

**操作步骤**：

1. 在 [deep_ep/buffers/legacy.py:245-258](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/legacy.py#L245-L258) 中数 `config_map` 覆盖了哪些 `num_ranks`，并尝试回答：如果集群是 EP 200，V1 会怎样？
2. 阅读 [deep_ep/buffers/elastic.py:770-778](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L770-L778) 的 `get_expected_topk`，确认 V2 对任意 `num_ranks` 都能算出 SM 数（只要门控均衡）。
3. （本地有 8 卡 Hopper 时）设 `EP_BUFFER_DEBUG=1`，调用 `buffer.get_theoretical_num_sms(num_experts, num_topk)`，观察打印的 `sm_read/sm_write/rdma_traffic/nvlink_traffic/num_sms`。

**需要观察的现象**：V1 表里没有的 rank 数会触发 `assert`；V2 对任意 rank 数都返回一个合法 SM 数。

**预期结果**：体会「解析式」消除了「换规模就重调表」的工程负担。步骤 3 标记「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：V1 的 `Config` 为什么需要 4 个 chunked token 字段，而 V2 的 SM 决策里看不到它们？
**参考答案**：V1 内核不是 JIT 的，chunk 大小只能作为运行时参数从 `Config` 传入，故需在表里调；V2 把 SM 数等编译期化、chunk 概念被 channel/共享内存布局取代，且 SM 数由带宽建模直接解出，不再需要用户层的 chunk 调优参数。

**练习 2**：`get_theoretical_num_sms` 的注释说「不适用于 group-limited gate」，为什么？
**参考答案**：期望 top-k 公式假设门控均衡（每个 token 跨 rank 数服从组合概率）；group-limited gate 会人为限制每 token 命中的分组，打破均衡假设，流量分数不再成立，故解析式失效。

---

### 4.4 缓冲区与句柄接口演进：双 buffer + tuple handle → 统一 ElasticBuffer + EPHandle

#### 4.4.1 概念说明

这是用户感知最直接的一层变化。

- **V1：双 buffer + tuple handle + 两步式调用**。构造 `Buffer(group, num_nvl_bytes, num_rdma_bytes)` 要分别指定 NVLink 与 RDMA 两块缓冲；每次 dispatch **必须先** `get_dispatch_layout(...)` 算出布局张量，**再** `dispatch(..., num_tokens_per_rank=..., config=..., ...)`。返回的 `handle` 是一个普通 tuple，且 intranode 与 internode 的 tuple 结构不同，用户不能拼错。低延迟走另一套 `low_latency_dispatch`，与 normal 内核接口完全不通用。
- **V2：统一 ElasticBuffer + EPHandle + 一步式调用**。构造 `ElasticBuffer(group, num_max_tokens_per_rank=..., hidden=..., num_topk=...)` 一块缓冲包打天下（尺寸自动算）；dispatch 一步完成，内部自动决定 SM/QP；返回的 `handle` 是有类型的 `EPHandle` 对象，字段有文档、有语义，且可作为 cached handle 复用。high-throughput 与（曾经的）low-latency 统一在同一接口下。

README 的表述是：「High-throughput and low-latency APIs unified into a single `ElasticBuffer` interface, with a new GEMM layout」（[README.md:18](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L18)）。

#### 4.4.2 核心流程

**V1 训练前向 dispatch（两步，handle 是 tuple）**——来自 [docs/legacy.md:165-181](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/docs/legacy.md#L165-L181)：

```
# 第一步：算布局
num_tokens_per_rank, num_tokens_per_rdma_rank, num_tokens_per_expert, is_token_in_rank, previous_event = \
    _buffer.get_dispatch_layout(topk_idx, num_experts, previous_event=..., async_finish=True, ...)
# 第二步：真正 dispatch，把布局与 config 都传回去
recv_x, recv_topk_idx, recv_topk_weights, num_recv_tokens_per_expert_list, handle, event = \
    _buffer.dispatch(x, ..., num_tokens_per_rank=..., is_token_in_rank=..., num_tokens_per_expert=...,
                     config=config, previous_event=..., async_finish=True, allocate_on_comm_stream=True)
```

**V2 训练前向 dispatch（一步，handle 是 EPHandle）**——来自 [README.md:190-204](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L190-L204)：

```
recv_x, recv_topk_idx, recv_topk_weights, handle, event = _buffer.dispatch(
    x, topk_idx=topk_idx, topk_weights=topk_weights,
    num_experts=num_experts, num_max_tokens_per_rank=num_max_tokens_per_rank,
    expert_alignment=expert_alignment, num_sms=_num_comm_sms,
    async_with_compute_stream=True)
```

#### 4.4.3 源码精读

V1 dispatch 的签名长且分模式（注意它根据 `handle is not None` 与 `num_rdma_ranks>1` 走 intranode/internode/cached 多条分支，返回 tuple 里 `handle` 也是 tuple），见 [deep_ep/buffers/legacy.py:322-405](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/legacy.py#L322-L405)。其中 cached 模式要用户自己解包 tuple：

```python
# deep_ep/buffers/legacy.py:386-388（节选）— handle 是裸 tuple，用户须记住字段顺序
rank_prefix_matrix, channel_prefix_matrix, recv_channel_prefix_matrix, recv_src_idx, is_token_in_rank, send_head = handle
```

V2 的 `EPHandle` 则是一个有完整 docstring 的类，字段如 `recv_src_metadata`、`psum_num_recv_tokens_per_expert`、`dst_buffer_slot_idx`、`token_metadata_at_forward`、`channel_linked_list` 都有明确语义，且 dispatch 返回类型标注就是 `EPHandle`，见 [deep_ep/buffers/elastic.py:25-96](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L25-L96) 与 dispatch 签名 [deep_ep/buffers/elastic.py:855-923](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L855-L923)。

缓冲区构造的对比同样鲜明：

- V1 `Buffer.__init__` 需 `num_nvl_bytes` + `num_rdma_bytes` 两段，且要先算 `get_dispatch_config`/`get_combine_config` 取 buffer size hint（[deep_ep/buffers/legacy.py:33-136](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/legacy.py#L33-L136)）；
- V2 `ElasticBuffer.__init__` 直接吃 MoE 设置（`num_max_tokens_per_rank/hidden/num_topk`），由 `_C.calculate_elastic_buffer_size` 自动算出对齐字节（[deep_ep/buffers/elastic.py:228-367](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L228-L367)）。

#### 4.4.4 代码实践

**实践目标**：把 V1 的两步式调用改写成 V2 的一步式，直观体会接口简化。

**操作步骤**：

1. 阅读 [docs/legacy.md:156-181](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/docs/legacy.md#L156-L181) 的 V1 `dispatch_forward`：先 `get_dispatch_layout` 再 `dispatch(config=...)`。
2. 阅读 [README.md:177-204](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L177-L204) 的 V2 `dispatch_forward`：单次 `_buffer.dispatch(...)`。
3. 在纸上把 V1 的两步合并：V2 把「布局计算」内化进了 dispatch 内核的 notify warps（u5-l1），把「config」换成了 `num_sms`，把「tuple handle」换成了 `EPHandle`。

**需要观察的现象**：V2 的用户代码行数明显更少，且没有任何「字段顺序」心智负担。

**预期结果**：理解「接口统一」= 更少样板代码 + 类型安全的 handle + 训练/解码/低延迟共用同一对象。本步为源码阅读型实践，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：V1 的 `handle` 为什么是 tuple 而不是类？
**参考答案**：历史原因——V1 内核安装期编译，handle 只是给 C++ runtime 回传布局张量的容器，用 tuple 最省事；但代价是 intranode/internode tuple 结构不同、字段顺序无文档，易错。V2 用 `EPHandle` 类正是为了补上类型安全与文档（u2-l3）。

**练习 2**：V2 的 cached handle 复用，相比 V1 的 `handle` 复用多了什么？
**参考答案**：V2 cached handle 跳过的是「布局重算 + CPU 同步 + 张量重分配」（`num_notify_warps=0`、`reuse_slot_indices=true`），且 `EPHandle` 作为整体回传，用户无需手动解包；V1 的 handle 复用只是省掉 `get_dispatch_layout`，但 tuple 仍要用户自己管。

---

### 4.5 性能、SM 占用与可扩展性对比

#### 4.5.1 概念说明

前三节讲的是「怎么实现」，本节给出「效果如何」的定量对比，分四个维度：

1. **峰值性能**：V2 最高 1.3× V1。
2. **SM 占用**：V2 最多省到 1/4；V3 风格训练从 24 SM 降到 4–6 SM。
3. **最大 EP 规模**：V2 支持到 EP2048；V1 受 `config_map` 与 `LEGACY_NUM_MAX_*` 常量限制。
4. **低延迟 RDMA**：V1 有纯 RDMA 0-SM 低延迟内核；V2 **明确不再支持**。

#### 4.5.2 核心流程（数据对照）

**性能与 SM 占用**（README 原文）：

> Comparing with V1, **V2 achieves up to 1.3x peak performance, while saving up to 4x SM count**.（[README.md:55](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L55)）
>
> For V3-like legacy training, SM usage reduced from 24 to 4 - 6 while maintaining equivalent or better performance.（[README.md:22](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L22)）

**可扩展性**：

> Larger scale-up & scale-out domain support (up to EP2048).（[README.md:19](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L19)）

对照 V1：`config_map` 只列到 160 rank（[deep_ep/buffers/legacy.py:245-258](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/legacy.py#L245-L258)），且 C++ 层有 `LEGACY_NUM_MAX_NVL_PEERS==8`、`LEGACY_NUM_MAX_RDMA_PEERS` 等硬上限（[csrc/legacy/buffer.hpp:23](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/legacy/buffer.hpp#L23)）。

**低延迟 RDMA 的取舍**（V2 明确不再支持）：

> 0 SM RDMA low-latency EP is no longer supported.（[README.md:30](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L30)）

而 V1 的低延迟内核在 [docs/legacy.md:28-39](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/docs/legacy.md#L28-L39) 有完整性能表（EP8 dispatch 77 us、combine 114 us，一直测到 EP256）。这是迁移时最重要的「功能损失」。

**缓冲区代价**（V2 更大）：

> Buffer size consumption is larger than V1.（[README.md:29](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L29)）

V2 的对称窗口布局是 `[[[Workspace] GPU buffer] CPU buffer]`，workspace 按 `kNumMaxRanks=1024`、`kNumMaxExperts=2048` 等上限常量固定预留、且 dispatch/combine 取最大值并对齐 2 MB（u3-l2），故比 V1 的双 buffer 更费显存。

#### 4.5.3 源码精读

性能数字本身出自 README 两张表：V2 在 [README.md:45-51](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L45-L51)（SM90/SM100，EP 8×2 到 EP 8），V1 normal 内核在 [docs/legacy.md:21-26](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/docs/legacy.md#L21-L26)（H800，intranode 153/158 GB/s、internode 到 EP64）。注意两代测试配置不同（V2 用 8K tokens/top8，V1 用 4096 tokens/top4 groups+top8），不能简单逐行相减，但 README 给出的「1.3× 峰值、4× SM 节省」是作者在同口径下的结论。

SM 占用差异的代码根因：V2 有 `prefer_overlap_with_compute` 开关，开启时把 SM 压到「刚够带宽」的下限（[deep_ep/buffers/elastic.py:824](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L824) 的 `num_sms if self.prefer_overlap_with_compute else max(num_sms, 64)`）；V1 的 `Buffer.num_sms` 是全局静态变量、默认 20，没有「按需压低」机制（[deep_ep/buffers/legacy.py:31](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/legacy.py#L31) 与 [deep_ep/buffers/legacy.py:153-163](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/legacy.py#L153-L163)）。

#### 4.5.4 代码实践

**实践目标**：用环境变量亲测 V2 的 SM 节省与 `prefer_overlap_with_compute` 的作用。

**操作步骤**：

1. 阅读上面引用的两张性能表，记录 V1 internode EP16（43 GB/s）与 V2 SM90 EP 8×2（90 GB/s）的数字。
2. （本地 8 卡 Hopper）跑 `tests/elastic/test_ep.py`，分别用默认（`prefer_overlap_with_compute=True`）与构造时 `prefer_overlap_with_compute=False` 创建 buffer，观察 `get_theoretical_num_sms` 返回值（设 `EP_BUFFER_DEBUG=1`）。
3. 对照测试输出的实际带宽与 SM 数，验证「省 SM 不掉带宽」。

**需要观察的现象**：`prefer_overlap_with_compute=True` 时 `num_sms` 较小（如 4–6 量级），`False` 时被抬到至少 64。

**预期结果**：直观看到 V2「以解析式压低 SM」的效果。无 GPU 环境则步骤 2–3 标记「待本地验证」，步骤 1 的数据阅读可独立完成。

#### 4.5.5 小练习与答案

**练习 1**：V2 既然性能更好、SM 更省，为什么缓冲区反而更大？
**参考答案**：V2 的对称窗口要兼容 EP2048 与 hybrid 两级通信，workspace 按 `kNumMaxRanks/kNumMaxExperts` 上限常量固定预留，且 dispatch/combine 分时复用取最大值并对齐 2 MB；这些「为可扩展性与统一接口」的设计代价就是更费显存（u3-l2）。

**练习 2**：如果你的业务强依赖 V1 的 0-SM 低延迟 RDMA 内核，迁移到 V2 会遇到什么？
**参考答案**：V2 明确「0 SM RDMA low-latency EP is no longer supported」（[README.md:30](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L30)）。你需要么继续用 V1 `Buffer` 的 `low_latency_dispatch/combine`，么改用 V2 的高吞吐内核（配合 `async_with_compute_stream` 做通信-计算重叠），但后者不是「0 SM」。可在实验分支（如 README 的 Eager/Hybrid-EP）寻找替代方案。

---

## 5. 综合实践

**任务**：写一份 V1 vs V2 六维对比表，并标注 V2「不再支持」的特性。

请按下表填空（答案见下方「参考答案」，建议先自己写再对照）：

| 维度 | V1（Legacy） | V2（Elastic） |
| --- | --- | --- |
| 通信后端 | ① | ② |
| 编译方式 | ③ | ④ |
| SM/QP 决策 | ⑤ | ⑥ |
| 缓冲区接口 | ⑦ | ⑧ |
| 最大 EP 规模 | ⑨ | ⑩ |
| 低延迟 RDMA 支持 | ⑪ | ⑫ |

并在表下用一句话写出 V2 文档中明确「不再支持」的特性。

**操作步骤**：

1. 通读 [README.md:8-31](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L8-L31) 的 New features 与 Notes。
2. 对照 [docs/legacy.md:15-39](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/docs/legacy.md#L15-L39) 的 V1 性能与 [README.md:45-55](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L45-L55) 的 V2 性能。
3. 用本讲 4.1～4.5 的源码引用支撑每一格的结论。

**参考答案**：

| 维度 | V1（Legacy） | V2（Elastic） |
| --- | --- | --- |
| 后端 | NVSHMEM（RDMA）+ CUDA IPC（NVLink），依赖 NVSHMEM 运行时 | NCCL Gin，header-only，复用 NCCL communicator |
| 编译 | 安装期编译进 `_C.so`（`csrc/legacy/`） | 运行时 JIT（真内核在 `deep_ep/include/impls/*.cuh`） |
| SM/QP | `config_map: num_ranks → Config` 查表（auto-tuning，注释 `TODO: automatically tune`） | `get_theoretical_num_sms/qps` 带宽建模解析式，无需预热 |
| 缓冲区接口 | `Buffer(num_nvl_bytes, num_rdma_bytes)` 双缓冲 + 两步式 `get_dispatch_layout`+`dispatch(config=...)` + tuple handle | `ElasticBuffer(num_max_tokens_per_rank, hidden, num_topk)` 单缓冲 + 一步式 `dispatch(num_sms=...)` + `EPHandle` |
| 最大 EP 规模 | `config_map` 只到 160 rank，C++ 受 `LEGACY_NUM_MAX_NVL_PEERS==8` 等上限约束 | 最高 EP2048 |
| 低延迟 RDMA | 有 0-SM 纯 RDMA `low_latency_dispatch/combine`（IBGDA，hook 重叠） | **不再支持** 0-SM RDMA 低延迟 EP |

V2 明确「不再支持」的特性：**0 SM RDMA low-latency EP**（[README.md:30](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L30)）；此外 V2 的缓冲区显存占用比 V1 更大（[README.md:29](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L29)），虽非「不支持」但是重要迁移代价。

---

## 6. 本讲小结

- **后端**：V2 用 header-only 的 NCCL Gin 取代 NVSHMEM，复用已有 NCCL communicator，删掉了整条 NVSHMEM 运行时依赖链。
- **编译**：V1 安装期编译；V2 改为运行时 JIT，把 SM 数、rank 数、hidden 等烘焙成模板常量换取极致优化，靠两级缓存摊薄首次编译开销。
- **SM/QP**：V1 用 `config_map` 查表（auto-tuning）；V2 用带宽建模解析式 `get_theoretical_num_sms/qps`，无需预热、任意规模可算。
- **接口**：V1 双 buffer + 两步式调用 + tuple handle；V2 统一 `ElasticBuffer` + 一步式 `dispatch` + 类型化 `EPHandle`（可作 cached handle 复用）。
- **性能**：V2 最高 1.3× 峰值、最多 1/4 SM（V3 风格训练 24 → 4–6 SM），代价是缓冲区显存占用更大。
- **取舍**：V2 支持到 EP2048，但**不再支持 0-SM RDMA 低延迟 EP**——这是从 V1 迁移时最需要注意的功能损失。

## 7. 下一步学习建议

- 若你关注**低延迟路径的去留**：阅读 README 的 Experimental branches（[README.md:402-429](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L402-L429)），其中 Eager、Hybrid-EP、nvDev（CFT）等分支正在用新机制补回低延迟能力。
- 若你要**实际迁移代码**：以本讲综合实践的对比表为检查清单，逐项把 V1 的 `get_dispatch_layout`+`dispatch(config=...)` 改写成 V2 的一步式 `dispatch(num_sms=...)`，并把 tuple handle 替换为 `EPHandle`。
- 若你想**深入 V2 内部**：回到 u5（dispatch 内核链路）与 u6（combine 内核链路），看 JIT 生成的 `dispatch_impl`/`combine_impl` 如何在 NVLink/RDMA 上搬数据；再配合 u8（PTX/NCCL/环境变量）理解底层原语。
- 若你想**理解缓冲区为何变大**：复习 u3-l2 的 `WorkspaceLayout/TokenLayout/BufferLayout` 与 `calculate_buffer_size` 取最大值对齐 2 MB 的设计。
