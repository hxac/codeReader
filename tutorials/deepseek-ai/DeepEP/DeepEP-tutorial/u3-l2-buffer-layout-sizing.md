# 缓冲区内存布局与大小解析计算

## 1. 本讲目标

本讲承接 [u3-l1](u3-l1-topology-domains.md) 对「物理域 / 逻辑域」的认识，钻进 DeepEP V2 缓冲区的「内部结构」。

具体地，读完本讲你应该能够：

1. 画出 NCCL 对称内存窗口内 `[[[Workspace] GPU buffer] CPU buffer]` 这套三层嵌套的地址排布，并解释 workspace 为何要放在最前面、为何要按 2 MB 对齐。
2. 读懂 `deep_ep/include/deep_ep/common/layout.cuh` 里三个核心结构体 `WorkspaceLayout` / `TokenLayout` / `BufferLayout` 各自描述什么、字段怎么排、偏移怎么算。
3. 手算一次「直接模式 dispatch / combine 需要多少字节」，并理解 `calculate_buffer_size` 为什么对 dispatch 和 combine 两种布局「取最大值」再「按 2 MB 对齐」。
4. 能用 `ElasticBuffer.get_buffer_size_hint` 或 `_C.calculate_elastic_buffer_size` 验证你手算的量级。

本讲只讲「布局与尺寸的解析计算」，不涉及 dispatch / combine 内核内部如何往这些缓冲区里写数据（那是 U5/U6 的内容）。

## 2. 前置知识

- **对称内存（symmetric memory）**：每个 rank 在自己 GPU/CPU 的「同一个本地偏移」上开出一块同样大的内存，再借助 NCCL Gin 窗口把所有 rank 的这块内存「拼」成一张全局可见的表。这样 `本地偏移 + rank_idx × stride` 就能寻址到任意 rank 的对应位置。详见 [u3-l1](u3-l1-topology-domains.md) 与 [u3-l4](u3-l4-nccl-gin-symmetric.md)。
- **TMA（Tensor Memory Access）**：Hopper 上的一种异步批量拷贝引擎，要求数据按 32 字节对齐。本讲里你会反复看到 `kNumTMAAlignBytes = 32` 这个对齐粒度。
- **mbarrier**：Hopper 共享内存里的异步屏障原语，本质是一个 8 字节的 64 位原子计数。
- **BF16 / FP8**：BF16 占 2 字节；FP8（`__nv_fp8_e4m3`）占 1 字节，并伴随 scaling factor（SF）。SF 在 DeepEP 里以 `sf_pack_t`（4 字节，`float` 与 `ue8m0x4` 的 union）为单位打包。
- **direct 模式 vs hybrid 模式**：单节点（`num_scaleout_ranks == 1`）走 direct；多节点（`num_scaleout_ranks > 1`）走 scaleout（RDMA）+ scaleup（NVLink）两级 hybrid。两种模式下缓冲区要为不同的「收发组合」预留空间。
- **workspace**：DeepEP 把「数据面」（真正搬运的 token）和「控制面」（计数器、信号、屏障）分开。控制面统一放在 workspace 里，必须始终为零。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`csrc/elastic/buffer.hpp`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp) | `ElasticBuffer` 的 C++ 实现：构造函数里拼装对称窗口；`get_dispatch_buffer_size` / `get_combine_buffer_size` / `calculate_buffer_size` 三个静态方法是本讲的「尺寸公式」来源。 |
| [`deep_ep/include/deep_ep/common/layout.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh) | 三个布局结构体 `WorkspaceLayout` / `TokenLayout` / `BufferLayout`，定义了「一个 token 长什么样」「一整块 buffer 长什么样」以及各种寻址指针。 |
| [`deep_ep/include/deep_ep/common/ptx.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh) | 提供 `kNumTMAAlignBytes = 32` 与 `mbarrier`（8 字节）等底层常量。 |
| [`deep_ep/include/deep_ep/common/compiled.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/compiled.cuh) | 定义 `sf_pack_t`（4 字节）、`kNumAlignedSFPacks = 4`、`kNumMaxChannels = 1024`。 |
| [`csrc/kernels/backend/symmetric.hpp`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp) | 定义 `kNumAlignmentBytes = 2097152`（2 MB），即整个对称窗口的统一对齐粒度。 |
| [`csrc/kernels/elastic/dispatch.hpp`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp) | `get_dispatch_token_layout` 工厂函数（dispatch 的 token 含 metadata）。 |
| [`csrc/kernels/elastic/combine.hpp`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/combine.hpp) | `get_combine_token_layout` 工厂函数（combine 的 token 不含 src metadata）。 |
| [`deep_ep/buffers/elastic.py`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py) | Python 侧：`__init__` 调 `_C.calculate_elastic_buffer_size`；`get_buffer_size_hint` 是它的「不构造就能算尺寸」的对外静态方法。 |

## 4. 核心概念与源码讲解

### 4.1 对称窗口的整体内存布局：`[[[Workspace] GPU buffer] CPU buffer]`

#### 4.1.1 概念说明

DeepEP 在每个 rank 上向 NCCL 申请的「对称内存窗口」是一段**连续的虚拟地址**，被划分成三段（注意方括号是从外到内嵌套的）：

```
[[[ Workspace ]  GPU buffer ]  CPU buffer]
└─ 最外层：整段对称窗口（num_sym_bytes = workspace + buffer）
     ├─ Workspace       （控制面：计数/信号/屏障，必须清零，按 2 MB 对齐）
     ├─ GPU buffer      （数据面：真正收发的 token，在 GPU 显存里）
     └─ CPU buffer      （可选：用于 Engram 等「远端可 RDMA 拉取」的主存段）
```

理解这套嵌套的关键有两点：

1. **workspace 永远在最前**，并且单独按 2 MB 对齐。这样它后面的 `buffer` 起始地址天然落在 2 MB 边界上，有利于 TMA / RDMA 的大块传输（HCA、NIC 都喜欢大页、大块对齐）。
2. **用户给的 `num_bytes` 只含「GPU buffer + CPU buffer」，不含 workspace**。workspace 的字节数由 `WorkspaceLayout::get_num_bytes()` 单独算出，构造时**额外**拼到窗口最前面。

#### 4.1.2 核心流程

构造 `ElasticBuffer` 时，C++ 侧的尺寸拼装流程是：

```text
num_buffer_bytes        = 用户传入（GPU+CPU，不含 workspace），必须 >0 且 2MB 对齐
num_cpu_buffer_bytes    = 用户传入（CPU 段），必须 <= num_buffer_bytes 且 2MB 对齐
num_gpu_buffer_bytes    = num_buffer_bytes - num_cpu_buffer_bytes
num_workspace_bytes     = align(WorkspaceLayout::get_num_bytes(), 2MB)
num_sym_bytes           = num_workspace_bytes + num_buffer_bytes        # 交给 NCCL 的总窗口
num_cpu_bytes(NCCL)     = num_cpu_buffer_bytes                          # 其中 CPU 段大小
```

随后 `workspace` 指向窗口起点，`buffer` 指向 `workspace + num_workspace_bytes`。

#### 4.1.3 源码精读

类成员注释直接写明了这套布局（[csrc/elastic/buffer.hpp:L20-L25](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L20-L25)）：

```cpp
// Buffer bytes = GPU buffer + CPU buffer (excludes workspace)
// Memory layout: [[[Workspace] GPU buffer] CPU buffer]
int64_t num_buffer_bytes;
int64_t num_gpu_buffer_bytes;
int64_t num_cpu_buffer_bytes;
```

构造函数里的尺寸校验与拼装（[csrc/elastic/buffer.hpp:L97-L114](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L97-L114)）：

```cpp
// Check buffer bytes alignment (2 MB)
EP_HOST_ASSERT(num_buffer_bytes > 0 and num_buffer_bytes % symmetric::kNumAlignmentBytes == 0);
EP_HOST_ASSERT(num_cpu_buffer_bytes >= 0 and num_cpu_buffer_bytes % symmetric::kNumAlignmentBytes == 0);
EP_HOST_ASSERT(num_cpu_buffer_bytes <= num_buffer_bytes);
num_gpu_buffer_bytes = num_buffer_bytes - num_cpu_buffer_bytes;

// Workspace is aligned to 2 MB so that it sits cleanly at the front of the GPU segment
const auto num_workspace_bytes = math::align<int64_t>(
    layout::WorkspaceLayout::get_num_bytes(), symmetric::kNumAlignmentBytes);

// Symmetric memory layout: [[[Workspace] GPU buffer] CPU buffer]
// sym.num_bytes = workspace + buffer, sym.num_cpu_bytes = CPU buffer
const auto num_sym_bytes = num_workspace_bytes + num_buffer_bytes;
this->nccl_context = std::make_shared<nccl::NCCLSymmetricMemoryContext>(
    nccl_comm, cpu_comm, num_ranks, rank_idx,
    num_sym_bytes, num_cpu_buffer_bytes, ...);
```

随后把 `workspace`、`buffer` 指针落到正确的偏移上，并把 workspace 清零（[csrc/elastic/buffer.hpp:L117-L130](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L117-L130)）：

```cpp
EP_HOST_ASSERT(num_workspace_bytes + num_gpu_buffer_bytes == nccl_context->num_gpu_bytes);
EP_HOST_ASSERT(num_cpu_buffer_bytes == nccl_context->num_cpu_bytes);
...
workspace = this->nccl_context->mapped_window_ptr;
...
buffer = static_cast<uint8_t*>(workspace) + num_workspace_bytes;
CUDA_RUNTIME_CHECK(cudaMemset(workspace, 0, num_workspace_bytes));
```

> 注意这里的两个断言：NCCL 实际分配出的 `num_gpu_bytes` 必须等于 `workspace + gpu_buffer`，`num_cpu_bytes` 必须等于 `cpu_buffer`。这是在「校验 NCCL 给回来的对称窗口真的按我们设想的三段排布」。

#### 4.1.4 代码实践

1. **目标**：用 `EP_BUFFER_DEBUG=1` 观察构造时打印的 `num_bytes` 与 `(cpu: ...)`，体会「用户给的 num_bytes 不含 workspace」。
2. **操作**：在 `tests/elastic/test_ep.py` 运行命令前加环境变量 `EP_BUFFER_DEBUG=1`。
3. **观察**：会看到形如 `Initializing EP elastic buffer with N bytes (cpu: 0) at rank EP ...` 的输出（打印点在 [deep_ep/buffers/elastic.py:L311-L313](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L311-L313)）。
4. **预期**：打印的 `N` 就是 `num_buffer_bytes`（GPU+CPU，不含 workspace）。真正向 NCCL 注册的窗口会比它大一个 `num_workspace_bytes`（约几十 KB，向上对齐到 2 MB 后是 2 MB）。
5. 该步需要多卡环境，**待本地验证**。

#### 4.1.5 小练习与答案

**练习**：如果把 `num_bytes` 故意传一个「不是 2 MB 倍数」的值（例如 `100`），构造时会怎样？

**答案**：会在 [buffer.hpp:L98](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L98) 的 `EP_HOST_ASSERT(num_buffer_bytes % symmetric::kNumAlignmentBytes == 0)` 处直接抛出 host 断言失败。`kNumAlignmentBytes = 2097152`（[symmetric.hpp:L16](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp#L16)）。Python 侧因此提供了 `get_buffer_size_hint` / `calculate_elastic_buffer_size`，返回值天然 2 MB 对齐，避免用户踩坑。

---

### 4.2 WorkspaceLayout：固定位置的「控制平面」工作区

#### 4.2.1 概念说明

`WorkspaceLayout` 描述的是 4.1 里最前面的 **workspace 段**。它是「控制平面」：dispatch/combine 的 notify warps 在这里写「我给每个 rank / 每个 expert 发了几个 token」的计数，barrier 在这里翻转信号，AGRS/PP 在这里放收发信号。

它有一个很重要的设计哲学：**位置固定、与具体设置无关**。源码注释写得很直白——「We want to fix the layout position for all settings, so that one buffer can be reused for all cases」（[layout.cuh:L17-L19](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L17-L19)）。也就是说，无论你是 8 rank 还是 2048 rank、是 EP64 还是 EP2048，workspace 内每个字段的偏移都按「上限常量」预留好，这样同一个 buffer 能复用于各种场景。

#### 4.2.2 核心流程

`WorkspaceLayout::get_num_bytes()` 把下列控制面字段累加（[layout.cuh:L43-L80](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L43-L80)）：

```text
固定上限常量：
  kNumMaxRanks           = 1024
  kNumMaxExperts         = 2048
  kNumMaxExpertsPerRank  = 256
  kNumMaxInflightAGRS    = 32
  kNumMaxChannels        = 1024          # 来自 compiled.cuh 的 deep_ep 命名空间
  kNumBarrierSignalBytes = 16

累加项（概览）：
  + 纯 NVLink barrier 信号             16 B
  + notify 归约工作区                  (kNumMaxRanks + kNumMaxExperts) * 8 B
  + scaleup 计数（rank/expert × send/recv）  (kNumMaxRanks + kNumMaxExperts) * 8 * 2 B
  + scaleup 原子 sender 计数           kNumMaxRanks * 4 B
  + scaleout 计数（rank/expert × send/recv） (kNumMaxRanks + kNumMaxExperts) * 4 * 2 B
  + scaleout channel 元数据            kNumMaxRanks * kNumMaxChannels * 8 B
  + channel→scaleup 聚合尾             kNumMaxRanks * kNumMaxChannels * 4 B
  + PP prev/next 计数                  2 * 2 * 8 B
  + AGRS 信号                          (kNumMaxInflightAGRS + 1) * kNumMaxRanks * 4 B
```

由于全部按上限常量取值，`get_num_bytes()` 实际上**与运行时的 rank 数、expert 数无关**，是一个固定大小（约几十 KB）。构造时再向上对齐到 2 MB（见 4.1）。

#### 4.2.3 源码精读

固定上限常量定义（[layout.cuh:L17-L24](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L17-L24)）：

```cpp
// We want to fix the layout position for all settings,
// so that one buffer can be reused for all cases
static constexpr int kNumMaxRanks = 1024;
static constexpr int kNumMaxExperts = 2048;
static constexpr int kNumMaxExpertsPerRank = 256;
static constexpr int kNumMaxInflightAGRS = 32;
static constexpr int kNumBarrierSignalBytes = 16;
```

`get_num_bytes()` 的逐项累加（[layout.cuh:L43-L80](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L43-L80)）摘录前几项：

```cpp
static int64_t get_num_bytes() {
    int64_t num_bytes = 0;
    num_bytes += kNumBarrierSignalBytes;
    num_bytes += (kNumMaxRanks + kNumMaxExperts) * sizeof(int64_t);   // notify reduction workspace
    num_bytes += kNumMaxRanks * sizeof(int64_t) * 2;                  // scaleup rank send/recv
    num_bytes += kNumMaxExperts * sizeof(int64_t) * 2;                // scaleup expert send/recv
    ...
}
```

每个字段的实际指针都由一个 `get_xxx_ptr()` 方法按「前一项末尾 + 对齐」推出。例如 notify 归约工作区紧跟着 barrier 信号（[layout.cuh:L90-L92](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L90-L92)）：

```cpp
__forceinline__ __device__ __host__ int64_t* get_notify_reduction_workspace_ptr() const {
    return math::advance_ptr<int64_t>(workspace, kNumBarrierSignalBytes);
}
```

> 之所以强调「必须清零」(见 [buffer.hpp:L32](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L32) 与构造里的 `cudaMemset(workspace, 0, ...)`），是因为这些计数/信号会被「read-modify-write」式地原子更新，初值必须为 0，且 barrier 的 phase 翻转也依赖确定初值。

#### 4.2.4 代码实践

1. **目标**：体会「workspace 大小与 rank 数无关」。
2. **操作**：阅读 [layout.cuh:L43-L80](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L43-L80) 的 `get_num_bytes()`，确认里面每一项都只用到 `kNumMax*` 常量，没有用到构造时传入的 `num_scaleout_ranks` / `num_scaleup_ranks`。
3. **观察**：手动把所有项加起来（量级在几十 KB）。
4. **预期**：得到一个与具体拓扑无关的固定值；再除以 2 MB 向上取整就是 1，所以对齐后的 `num_workspace_bytes` 恒为 2 MB。
5. 本步为纯源码阅读，无需运行。

#### 4.2.5 小练习与答案

**练习**：dispatch 的 CPU 同步模式（`do_cpu_sync=True`）要轮询读「每个 scaleup rank 收到了几个 token」。这个计数落在 workspace 的哪个字段？

**答案**：落在 `get_scaleup_rank_count_ptr<false>()` 指向的区域（recv 侧的 rank 计数），它本质是 `get_scaleup_rank_expert_count_ptr<false>()`（[layout.cuh:L94-L104](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L94-L104)）。host 侧在 [buffer.hpp:L1024-L1031](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1024-L1031) 通过映射到 host 的 `host_workspace` 轮询它（注意是另一份 host workspace，不是 GPU workspace）。

---

### 4.3 TokenLayout：单个 token 的四段打包布局

#### 4.3.1 概念说明

`TokenLayout` 回答的是「**一个 token 在缓冲区里长什么样**」。DeepEP 把一个 token 打包成至多四段（每段都按 32 字节 `kNumTMAAlignBytes` 对齐）：

```text
| hidden 数据 | SF (scaling factor) | metadata | mbarrier(可选) |
```

- **hidden**：token 的隐藏向量原始字节，`num_hidden_bytes = hidden * elem_size`。
- **SF**：FP8 模式下的 scaling factor，按 `sf_pack_t`（4 字节）打包，`num_sf_bytes = num_sf_packs * sizeof(sf_pack_t)`；BF16 时为 0。
- **metadata**：路由元数据，包括 top-k 索引（int32）、top-k 权重（float32），以及可选的「源 rank / 源 token 全局索引 + 链表指针」（仅 `with_metadata=true` 时才有）。
- **mbarrier**：仅当模板参数 `kWithMBarrier=true` 时追加一个 8 字节的 mbarrier（再按 32 字节对齐），用于内核内部 TMA 完成同步。

> 注意：**同一个 token 在 dispatch 与 combine 里的「打包内容」不同**。dispatch 要把路由信息一起寄过去（`with_metadata=true`），combine 不需要再寄源信息（`with_metadata=false`）。这就是为什么 `get_dispatch_token_layout` 和 `get_combine_token_layout` 是两个不同工厂。

#### 4.3.2 核心流程

metadata 的字节数公式（[layout.cuh:L194-L195](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L194-L195)）：

\[
\text{num\_metadata\_bytes} = \text{num\_topk}\cdot(\underbrace{4}_{\text{int 索引}}+\underbrace{4}_{\text{float 权重}}) + \begin{cases}(1+\text{num\_topk})\cdot 4, & \text{with\_metadata} \\ 0, & \text{否则}\end{cases}
\]

其中 `with_metadata` 时多出的 `(1+num_topk)*4` 字节是：1 个「源 token 全局索引」+ num_topk 个「目标槽位索引」（看 `get_src_token_global_idx_ptr` 与 `get_linked_list_idx_ptr` 的关系，[layout.cuh:L238-L244](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L238-L244)）。

单个 token 总字节数（每段都向上对齐到 32 字节，[layout.cuh:L201-L208](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L201-L208)）：

\[
\text{per\_token} = \text{align}(\text{hidden},32)+\text{align}(\text{sf},32)+\text{align}(\text{metadata},32)+\text{align}(\text{mbarrier?},32)
\]

#### 4.3.3 源码精读

构造函数与 metadata 字节数（[layout.cuh:L186-L199](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L186-L199)）：

```cpp
TokenLayout(const int& num_hidden_bytes, const int& num_sf_bytes,
            const int& num_topk, const bool& with_metadata, void* base = nullptr) :
    num_hidden_bytes(num_hidden_bytes),
    num_sf_bytes(num_sf_bytes),
    with_metadata(with_metadata),
    num_topk(num_topk),
    num_metadata_bytes(num_topk * (sizeof(int) + sizeof(float)) +
                       (with_metadata ? (1 + num_topk) * sizeof(int) : 0)),
    base(base) {
    EP_STATIC_ASSERT(sizeof(int) == sizeof(float), "Invalid size assumption");
    EP_UNIFIED_ASSERT(num_hidden_bytes % ptx::kNumTMAAlignBytes == 0);
}
```

> 注意末尾的断言：**hidden 字节数本身必须已经是 32 的倍数**。这是因为 hidden 段是 TMA 拷贝的主体，不能靠「段间对齐」补救——所以上层会要求 `hidden * elem_size` 是 32 倍数（dispatch 入口 [buffer.hpp:L755](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L755) 断言 `(x.size(1) * x.element_size()) % sizeof(int4) == 0`，`int4` 即 16 字节，再叠加上层约定）。

每段指针逐段递增（以 sf / metadata 为例，[layout.cuh:L222-L228](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L222-L228)）：

```cpp
__forceinline__ __device__ __host__ sf_pack_t* get_sf_ptr() const {
    return math::advance_ptr<sf_pack_t>(base, math::align(num_hidden_bytes, ptx::kNumTMAAlignBytes));
}
__forceinline__ __device__ __host__ int* get_metadata_ptr() const {
    return math::advance_ptr<int>(get_sf_ptr(), math::align(num_sf_bytes, ptx::kNumTMAAlignBytes));
}
```

两个工厂函数体现 dispatch / combine 的差异（[dispatch.hpp:L136-L139](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L136-L139) 与 [combine.hpp:L109-L112](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/combine.hpp#L109-L112)）：

```cpp
// dispatch：带 SF、带 metadata
static layout::TokenLayout get_dispatch_token_layout(...) {
    return layout::TokenLayout(hidden * elem_size, num_sf_packs * sizeof(sf_pack_t), num_topk, /*with_metadata=*/true);
}
// combine：不带 SF、不带 src metadata
static layout::TokenLayout get_combine_token_layout(...) {
    return layout::TokenLayout(hidden * elem_size, 0, num_topk, /*with_metadata=*/false);
}
```

#### 4.3.4 代码实践

1. **目标**：手算一个 BF16、`hidden=7168`、`num_topk=6`、dispatch（`with_metadata=true`）的 token 字节数。
2. **操作**：按上面公式代入（`num_sf_bytes=0`）。
3. **计算**：
   - hidden = 7168 × 2 = 14336 B
   - metadata = 6 × 8 + 7 × 4 = 48 + 28 = 76 B
   - `per_token`(无 mbarrier) = align(14336,32) + align(0,32) + align(76,32) = 14336 + 0 + 96 = **14432 B**
4. **预期**：dispatch token = 14432 B；combine token（`with_metadata=false`，metadata=6×8=48）= 14336 + align(48,32)=64 → **14400 B**。可见 dispatch 因为多带路由元数据，每个 token 比 combine 多 32 B。
5. 本步为纯计算，可与 4.5 的总量计算串联验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `align(76, 32) = 96` 而不是 76？

**答案**：`math::align(a, b)` 默认做「向上取整对齐」`ceil_div(a,b)*b = ((a+b-1)/b)*b`（[math.cuh:L16-L18](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/math.cuh#L16-L18)）。`ceil_div(76,32) = (76+31)/32 = 3`，`3*32 = 96`。每段都要凑成 32 字节整倍，是为了让下一段起始地址满足 TMA 的 32 字节对齐要求。

**练习 2**：FP8 dispatch 时 SF 段大约多大？

**答案**：`num_sf_packs ≈ ceil_div(hidden, 32)`（见 [buffer.hpp:L672](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L672)），每 pack 4 字节，所以 `num_sf_bytes ≈ ceil_div(hidden,32)*4`。代码里也断言这块 SF 字节不会超过 hidden 主体（[buffer.hpp:L660](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L660)）。

---

### 4.4 BufferLayout：rank × token 的二维缓冲区与偏移寻址

#### 4.4.1 概念说明

`BufferLayout` 回答的是「**一整块收/发缓冲区怎么由 token 组合而成**」。它把 `TokenLayout` 在两个维度上展开：

- **num_ranks**：这块缓冲区为几个 rank 预留槽位（**注意：这里的 num_ranks 是「逻辑槽位数」，不一定等于世界规模**，下面 4.5 会看到它如何被复用为各种含义）。
- **num_max_tokens_per_rank**：每个 rank 槽位里能放几个 token。

整块缓冲区大小就是：

\[
\text{bytes} = \text{num\_ranks} \times \text{num\_max\_tokens\_per\_rank} \times \text{per\_token}
\]

`BufferLayout` 还提供两个常用切片：`get_rank_buffer(rank_idx)` 取某个 rank 的那一行；`get_channel_buffer<...>(channel_idx)` 在 hybrid 模式下按 channel 切片。

#### 4.4.2 核心流程

```text
get_num_bytes_per_token()  = TokenLayout::get_num_bytes<kWithMBarrier>()
get_num_bytes_per_rank()   = num_max_tokens_per_rank * per_token
get_num_bytes()            = num_max_tokens_per_rank * per_token * num_ranks

get_rank_buffer(i)   = BufferLayout(num_ranks=1, base + per_rank * i)
get_channel_buffer<...>(c) = BufferLayout(base + per_token * kNumTokensPerChannel * c)
```

#### 4.4.3 源码精读

模板参数 `kWithMBarier` 决定单个 token 是否追加 mbarrier 槽（[layout.cuh:L251-L266](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L251-L266)）：

```cpp
template <bool kWithMBarrier>
struct BufferLayout {
    TokenLayout token_layout;
    int num_ranks;
    int num_max_tokens_per_rank;
    void* base;
    ...
};
```

三层字节数（[layout.cuh:L268-L281](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L268-L281)）：

```cpp
int64_t get_num_bytes_per_token() const { return token_layout.get_num_bytes<kWithMBarrier, int64_t>(); }
int64_t get_num_bytes_per_rank()  const { return num_max_tokens_per_rank * get_num_bytes_per_token(); }
int64_t get_num_bytes()           const { return get_num_bytes_per_rank() * num_ranks; }
```

按 rank 切片（[layout.cuh:L288-L293](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L288-L293)）：

```cpp
BufferLayout get_rank_buffer(const int& rank_idx) const {
    return BufferLayout(token_layout, 1, num_max_tokens_per_rank,
                        static_cast<int8_t*>(base) + get_num_bytes_per_rank() * rank_idx);
}
```

> **关键观察**：缓冲区尺寸公式只取决于「num_ranks × num_max_tokens_per_rank × per_token」。在 4.5 你会看到，direct / hybrid 模式下「send / recv 各需要几份这样的 BufferLayout」才是决定总尺寸的核心。

#### 4.4.4 代码实践

1. **目标**：用 `get_rank_buffer` 理解「对端 rank 的数据落在哪」。
2. **操作**：阅读 [layout.cuh:L288-L303](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/layout.cuh#L288-L303)，注意 `get_channel_buffer` 里特意写了「Do not use `num_max_tokens_per_rank / kNumTokensPerChannel` as the false stride」。
3. **思考**：为什么 channel 切片时只搬动 `base`，而保持 `num_ranks / num_max_tokens_per_rank` 不变？
4. **预期**：因为 channel 是「在 token 维度上再切一刀」，rank 维度不变；保留原 `num_ranks` 作为「假步长」是为了让后续 `get_rank_buffer` 仍能正确跨 channel 寻址。这是个巧妙的复用。
5. 本步为源码阅读型实践。

#### 4.4.5 小练习与答案

**练习**：一块 `BufferLayout(num_ranks=8, num_max_tokens_per_rank=4096, per_token=14432)` 的缓冲区，rank 5 的数据从偏移多少开始？

**答案**：`get_num_bytes_per_rank() = 4096 * 14432 = 59,117,568 B`，rank 5 起始偏移 = `5 * 59,117,568 = 295,587,840 B`。这就是对端 rank 5 写入本 rank 接收缓冲区的起点。

---

### 4.5 calculate_buffer_size：dispatch / combine 取最大值并对齐 2 MB

#### 4.5.1 概念说明

`calculate_buffer_size` 是最终的「尺寸决策函数」，对外由 pybind 暴露为 `_C.calculate_elastic_buffer_size`（[buffer.hpp:L1367](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1367)），Python 侧 `ElasticBuffer.__init__`（[elastic.py:L306-L309](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L306-L309)）和 `get_buffer_size_hint`（[elastic.py:L402-L406](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L402-L406)）都调用它。

它的核心思想：

1. 分别算出 **dispatch** 与 **combine** 在当前拓扑下各自需要的缓冲区字节数；
2. 取两者**最大值**（因为同一个 buffer 既要发 dispatch 又要做 combine，必须装得下更大的那个）；
3. 把结果**向上对齐到 2 MB**。

为什么取最大而不是求和？因为 dispatch 和 combine 在时间上不会同时占用数据缓冲区（它们串行执行，且每次执行前会 barrier），所以「同一块内存，分时复用」，取最大即可。

#### 4.5.2 核心流程

```text
calculate_buffer_size(...):
    探测拓扑 → (num_rdma_ranks, num_nvl_ranks) → (num_scaleout_ranks, num_scaleup_ranks)
    is_scaleup_nvlink = (num_scaleup_ranks == num_nvl_ranks)
    num_dispatch_bytes = get_dispatch_buffer_size(...)
    num_combine_bytes = get_combine_buffer_size(...)
    return align(max(num_dispatch_bytes, num_combine_bytes), 2MB)
```

**direct 模式（`num_scaleout_ranks == 1`）的 dispatch**（[buffer.hpp:L594-L600](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L594-L600)）：

```text
send_layout = BufferLayout(token, ranks = is_scaleup_nvlink ? 0 : 1, num_max_tokens_per_rank)
recv_layout = BufferLayout(token, ranks = num_ranks, num_max_tokens_per_rank)
dispatch_bytes = send_layout.bytes + recv_layout.bytes
```

含义：
- **recv** 要为「全部 num_ranks 个对端」各预留 `num_max_tokens_per_rank` 个 token 槽（最坏情况：所有 token 都路由到本 rank）。
- **send** 在纯 NVLink 直连时为 0（`is_scaleup_nvlink=true`，token 直接从源张量经 NVLink 写入对端缓冲，无需本地中转段）；仅在非 NVLink 的扁平直连时预留 1 个 rank 的中转段。

**direct 模式的 combine**（[buffer.hpp:L623-L633](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L623-L633)）：

```text
num_tokens_in_layout = allow_multiple_reduction ? min(num_ranks, num_topk) : num_topk
send_layout = BufferLayout(token, ranks = is_scaleup_nvlink ? 0 : num_ranks,
                           num_max_tokens_per_rank * (allow_multiple_reduction ? 1 : num_topk))
recv_layout = BufferLayout(token, ranks = num_tokens_in_layout, num_max_tokens_per_rank)
combine_bytes = send_layout.bytes + recv_layout.bytes
```

含义：
- combine 的 recv 端只需为 `num_tokens_in_layout` 份归约预留空间（开启 `allow_multiple_reduction` 时归约次数受 `min(num_ranks, num_topk)` 限制）。
- send 端的 `num_max_tokens_per_rank * num_topk` 因子：单次归约（`allow_multiple_reduction=false`）要假设 `do_expand=True` 的最坏情况，每 token 展开 num_topk 份。

**hybrid 模式**（`num_scaleout_ranks > 1`）则累加三块：scaleup_recv + scaleout_send + scaleout_recv（[buffer.hpp:L601-L613](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L601-L613) 与 [buffer.hpp:L634-L649](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L634-L649)）。注意 scaleout 的 recv/send 维度里出现了 `num_max_tokens_per_rank + kNumMaxChannels`：这里多出的 `kNumMaxChannels`（`ElasticBuffer` 类里的 `= 8*160 = 1280`，[buffer.hpp:L57-L59](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L57-L59)）是给「channel 切分」留的余量。

#### 4.5.3 源码精读

总入口（[buffer.hpp:L652-L686](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L652-L686)）：

```cpp
static int64_t calculate_buffer_size(const int64_t& nccl_comm,
                                     const int& num_max_tokens_per_rank, const int& hidden,
                                     int num_topk, const bool& use_fp8_dispatch,
                                     const bool& allow_hybrid_mode,
                                     const bool& allow_multiple_reduction) {
    EP_HOST_ASSERT(num_max_tokens_per_rank > 0 and hidden > 0);
    EP_HOST_ASSERT(math::ceil_div(hidden, 32) * sizeof(float) <= hidden);
    // NOTES: there are lots of `kNumTopk <= 32` restrictions, so we use 32 to calculate token size
    num_topk = num_topk == 0 ? 32 : num_topk;

    // Topology
    const auto [num_rdma_ranks, num_nvl_ranks] = nccl::get_physical_domain_size(nccl_comm);
    const auto [num_scaleout_ranks, num_scaleup_ranks] = nccl::get_logical_domain_size(nccl_comm, allow_hybrid_mode);
    const auto is_scaleup_nvlink = num_scaleup_ranks == num_nvl_ranks;

    // Dispatch size
    const auto elem_size = use_fp8_dispatch ? sizeof(__nv_fp8_e4m3) : sizeof(nv_bfloat16);
    const auto num_sf_packs = use_fp8_dispatch ? math::ceil_div(hidden, 32) : 0;
    const auto num_dispatch_bytes = get_dispatch_buffer_size(...);

    // Combine layout
    const auto num_combine_bytes = get_combine_buffer_size(...);

    // Return the maximum of those layouts, aligned to 2 MB
    return math::align(std::max(num_dispatch_bytes, num_combine_bytes), symmetric::kNumAlignmentBytes);
}
```

> 两个易被忽略的细节：
> 1. **`num_topk == 0` 时按 32 计算**（[buffer.hpp:L663](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L663)）。因为内核里多处有 `num_topk <= 32` 的限制，干脆按上限 32 预留，保证 buffer 万一被复用到更大 top-k 时也够。这也是为什么 `get_buffer_size_hint` 允许传 `num_topk=0`。
> 2. **取 `max` 而非相加**：dispatch 与 combine 分时复用同一块 buffer，所以只要装得下更大的那个。

#### 4.5.4 代码实践（对应总实践任务）

**目标**：给定 `num_max_tokens_per_rank=4096`、`hidden=7168`、`num_topk=6`、单机 8 卡（`num_ranks=8`）、BF16，手算直接模式 dispatch 所需 `send + recv` 字节数，并推算最终 buffer 尺寸，再用 `get_buffer_size_hint` 核对量级。

**单机 8 卡拓扑**：`num_rdma_ranks=1, num_nvl_ranks=8`；`allow_hybrid_mode=True` 时逻辑域 `num_scaleout_ranks=1, num_scaleup_ranks=8`（[u3-l1](u3-l1-topology-domains.md) 已说明单机两模式相同）→ direct 模式；`is_scaleup_nvlink = (8==8) = true`。

**第一步：算 dispatch 的 per_token（4.3 已算）= 14432 B**。

**第二步：算 dispatch 总字节**（对照 [buffer.hpp:L594-L600](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L594-L600)）：

```text
send_layout: num_ranks = is_scaleup_nvlink ? 0 : 1 = 0  →  0 B
recv_layout: num_ranks = 8, num_max_tokens_per_rank = 4096
           = 4096 * 14432 * 8 = 472,940,544 B  ≈ 451.4 MiB
num_dispatch_bytes = 0 + 472,940,544 = 472,940,544 B
```

**第三步：算 combine 总字节**（取默认 `allow_multiple_reduction=True`，对照 [buffer.hpp:L623-L633](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L623-L633)）：

```text
combine per_token (with_metadata=false) = 14400 B
num_tokens_in_layout = min(8, 6) = 6
send_layout: num_ranks = is_scaleup_nvlink ? 0 : num_ranks = 0  →  0 B
recv_layout: num_ranks = 6, num_max_tokens_per_rank = 4096
           = 4096 * 14400 * 6 = 353,894,400 B  ≈ 337.5 MiB
num_combine_bytes = 0 + 353,894,400 = 353,894,400 B
```

**第四步：取最大并对齐 2 MB**：

```text
max(472,940,544, 353,894,400) = 472,940,544
align(_, 2^21):  ceil_div(472,940,544, 2,097,152) = 226  →  226 * 2,097,152 = 473,956,352 B ≈ 452 MiB
```

**结论**：这组配置下 dispatch 占主导，最终 `num_buffer_bytes ≈ 473,956,352 B（约 452 MiB）`。

**核对量级**（需多卡环境，**待本地验证**）：

```python
import torch, torch.distributed as dist
from deep_ep import ElasticBuffer
# 初始化 group 后：
hint = ElasticBuffer.get_buffer_size_hint(
    group, num_max_tokens_per_rank=4096, hidden=7168, num_topk=6,
    use_fp8_dispatch=False, allow_hybrid_mode=True, allow_multiple_reduction=True)
print(hint, "bytes =", hint / (1024**2), "MiB")
```

**预期**：`hint` 应为 473,956,352（即 452 MiB 左右），与手算一致。若与手算有出入，先检查 `allow_multiple_reduction` 与 `num_topk` 是否一致。

> 提示：`get_buffer_size_hint` 与真正 `ElasticBuffer(...)` 用的 `num_bytes` 来自同一个 `_C.calculate_elastic_buffer_size`，所以二者必然相等——这正是 `get_buffer_size_hint` 的用途：**先算好尺寸、再决定要不要切 CPU 段、最后才构造**。

#### 4.5.5 小练习与答案

**练习 1**：把 `num_topk` 从 6 改成 0 调 `get_buffer_size_hint`，结果会变大还是变小？

**答案**：会**变大**。因为 [buffer.hpp:L663](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L663) 把 `num_topk==0` 当作 32 处理，per_token 的 metadata 段从 `6*8+7*4=76` 涨到 `32*8+33*4=388`，整体预留更保守。

**练习 2**：为什么单机 8 卡下 dispatch 的 send 段是 0，而多节点 direct（假想的 `is_scaleup_nvlink=false`）要预留 1 个 rank 的 send 段？

**答案**：单机 scaleup 走 NVLink 对称内存，dispatch 内核把 token 直接从源张量写到对端缓冲区对应 rank 槽，本 rank 不需要单独的「发送暂存区」。而当 scaleup 不是 NVLink（扁平地走 RDMA 直连）时，需要先把待发数据收拢到一块本地连续暂存区，再由 RDMA 发出，因此预留 `num_ranks=1` 的 send 段（见 [buffer.hpp:L596-L597](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L596-L597) 的 `is_scaleup_nvlink ? 0 : 1`）。

**练习 3**：hybrid 模式下 scaleout_recv 的 token 维度为何是 `num_max_tokens_per_rank + kNumMaxChannels` 而不是 `num_max_tokens_per_rank`？

**答案**：hybrid 模式把每个 rank 的 token 再按 channel 切分发送，最坏情况下每个 channel 都要分到一个 token，所以每 scaleout rank 的 token 槽要额外多留 `kNumMaxChannels`（类内常量 `8*160=1280`）个 token 的余量，避免 channel 化时越界（[buffer.hpp:L607-L609](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L607-L609)）。

## 5. 综合实践

**任务**：写一个小脚本，对比「手算尺寸」「`get_buffer_size_hint` 尺寸」「真实构造出的 `ElasticBuffer.num_bytes`」三者是否一致，并解释三者关系。

步骤：

1. 选一组 MoE 配置（例如 `num_max_tokens_per_rank=4096, hidden=7168, num_topk=6, BF16`，单机 8 卡）。
2. 按 4.5 的公式手算 `num_dispatch_bytes` 与 `num_combine_bytes`，取 max 后对齐 2 MB，得到 `manual_bytes`。
3. 调 `ElasticBuffer.get_buffer_size_hint(...)` 得到 `hint_bytes`。
4. 真正构造 `buf = ElasticBuffer(group, num_max_tokens_per_rank=..., hidden=..., num_topk=..., ...)`，读 `buf.num_bytes` 得到 `actual_bytes`。
5. 断言 `manual_bytes == hint_bytes == actual_bytes`。

预期与解释：

- 三者应**完全相等**，因为它们最终都走 `_C.calculate_elastic_buffer_size` → `calculate_buffer_size`。
- `get_buffer_size_hint` 的价值在于「不构造就能算」，方便你提前规划显存（例如决定切多少 CPU 段给 Engram）。
- 如果你刻意构造时再额外指定 `num_cpu_bytes>0`，`num_bytes`（=GPU+CPU）仍等于 hint，但其中一部分会被划为 CPU 段，可用 Engram 等功能（见 [u7-l2](u7-l2-engram.md)）。

> 本实践需要单机 8 卡（或任意多卡）Hopper 环境。在无 GPU 环境下，可退化为「只做第 1、2 步手算 + 阅读源码核对公式」的源码阅读型实践，相关运行结果标注「待本地验证」。

## 6. 本讲小结

- DeepEP 的对称内存窗口是 **`[[[Workspace] GPU buffer] CPU buffer]`** 三段嵌套；workspace 在最前、单独 2 MB 对齐、且必须清零；用户给的 `num_bytes` 只含 GPU+CPU，**不含 workspace**。
- **`WorkspaceLayout`** 是「控制平面」，所有字段按 `kNumMaxRanks=1024 / kNumMaxExperts=2048` 等上限常量**固定位置**预留，使同一块 buffer 能复用于任意配置，其大小与运行时拓扑无关。
- **`TokenLayout`** 描述单个 token 的四段打包（hidden / SF / metadata / 可选 mbarrier），每段按 `kNumTMAAlignBytes=32` 向上对齐；dispatch 带 metadata，combine 不带。
- **`BufferLayout`** 把 token 在 `num_ranks × num_max_tokens_per_rank` 两维展开，总量 = `num_ranks × num_max_tokens_per_rank × per_token`。
- **`calculate_buffer_size`** 分别算 dispatch / combine 字节，**取最大值**（分时复用），再**对齐 2 MB**；`num_topk==0` 时按 32 保守预留。
- 单机 BF16、4096 token、hidden 7168、topk 6 的配置下，dispatch 主导，buffer 约 **452 MiB**。

## 7. 下一步学习建议

- 本讲只算了「需要多大」，但没讲「开多少 SM、多少 QP」——那是 [u3-l3 SM 与 QP 数量的解析式计算](u3-l3-sm-qp-analytical.md) 的主题，建议紧接着读。
- 想搞清楚这套对称窗口是怎么「凭 NCCL communicator 复用」建立起来的，看 [u3-l4 NCCL Gin 后端与对称内存上下文](u3-l4-nccl-gin-symmetric.md)。
- 想看 buffer 真正被 dispatch 内核「写满」的过程，进入 U5，从 [u5-l1 直接模式 Dispatch](u5-l1-direct-dispatch.md) 开始；本讲的 `recv_layout = BufferLayout(num_ranks, ...)` 会与 dispatch 内核的 notify / dispatch warp 划分直接对应。
