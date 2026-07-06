# 物理域与逻辑域：RDMA/NVLink rank 与 scaleout/scaleup

## 1. 本讲目标

本讲是「核心机制一」单元的第一讲。在前两个单元里，我们已经会创建 `ElasticBuffer`、调用 `dispatch`/`combine`，也看到构造函数末尾冒出了 `num_scaleout_ranks`、`num_scaleup_ranks`、`num_rdma_ranks`、`num_nvlink_ranks` 这一串拓扑属性。但当时我们只把它们当成「读出来摆着」的数字，并没有追问：

- 这些数字是怎么来的？谁来决定节点内有几张卡、集群里有几个节点？
- 为什么同一个 `world_size` 下，`num_scaleup_ranks` 既可能等于「每节点 GPU 数」，又可能等于「全局 rank 总数」？
- `allow_hybrid_mode` 这个开关到底改变了什么？为什么它和「多平面网络（multi-plane network）」挂钩？
- `is_scaleup_nvlink` 这个布尔值看似不起眼，为什么它会反过来影响缓冲区内存布局？

学完本讲，你应当能够：

1. **区分两套 rank 划分**：物理域（`num_rdma_ranks` / `num_nvlink_ranks`，描述硬件）与逻辑域（`num_scaleout_ranks` / `num_scaleup_ranks`，描述内核路由），并能说清它们的换算关系。
2. **讲清 `allow_hybrid_mode` 的作用**：它如何决定逻辑域的取值、如何切换 NCCL GIN 连接类型（RAIL vs FULL），以及为何「关闭它」对多平面/多轨道（multi-rail）网络更友好。
3. **掌握 `is_scaleup_nvlink` 的判定**：理解 `num_scaleup_ranks == num_nvl_ranks` 这个等式的含义，以及它如何决定对称内存走 `HybridElasticSymmetricMemory` 还是 `ElasticSymmetricMemory` 布局。

本讲只讨论「拓扑是如何被探测出来、又如何被映射成内核可用的逻辑域」，不涉及 dispatch/combine 内核内部如何使用这些域（那是 U5/U6 的内容）。

## 2. 前置知识

本讲承接 [u2-l2](u2-l2-elastic-buffer-ctor.md)，默认你已经了解：

- **rank / world_size / local_rank**：全局进程号、进程总数、节点内 GPU 序号（见 [u1-l4](u1-l4-run-first-test.md)）。
- **NVLink 与 RDMA 两条物理链路**：NVLink 用于节点内（intranode）GPU 互连，RDMA（InfiniBand/RoCE）用于节点间（internode）通信；README 用 `EP N x M` 表示「每节点 N rank、共 M 节点」（见 [u1-l1](u1-l1-project-overview.md)）。
- **ElasticBuffer 构造**：知道它通过 NCCL communicator 建立对称内存窗口，构造末尾会暴露四类拓扑属性（见 [u2-l2](u2-l2-elastic-buffer-ctor.md)）。
- **NCCL communicator 复用**：`ElasticBuffer` 不自己建 communicator，而是复用 PyTorch 的 NCCL communicator（见 [u2-l1](u2-l1-import-init.md)）。

下面补充三个本讲要用、但前面没细讲的小概念。

### 2.1 什么是「域（domain）」

分布式通信里，「域」就是「一组能直接互相通信、且通信特性相同的 rank」。把 `world_size` 切成若干域，是因为不同域里 rank 之间的「物理距离」不同：节点内 NVLink 一跳可达、带宽极大；跨节点 RDMA 要经过网卡和交换机、带宽小一个量级。把 rank 按物理距离分组，内核才能针对每一组用不同的发送方式（NVLink 直写 vs RDMA get/put），这正是 DeepEP 高性能的根基。

### 2.2 NCCL 的「team」概念

NCCL 内部把一个 communicator 的所有 rank 组织成若干 **team**。本讲最关键的两个 team 是：

- `ncclTeamWorld`：全部 rank，对应 `world_size`。
- `ncclTeamLsa`：**L**ocal **S**hared **A**ddress 域，即「能用 NVLink 共享寻址直达」的本地 rank 组，对应「单节点内的 GPU 数」。

DeepEP 不自己探测拓扑，而是直接问 NCCL 这两个 team 各有多少 rank——`num_nvl_ranks` 就来自 `ncclTeamLsa`。这是本讲最重要的「为什么物理域能算出来」的依据。

### 2.3 什么是「多平面/多轨道网络」

在大规模 GPU 集群里，为了把带宽摊到多张网卡和多条线路上，常把节点间的 RDMA 连接设计成「轨道式（railed）」：每张 GPU 在每张网卡上各占一条独立轨道，不同轨道的流量互不干扰，这就是 **multi-plane / multi-rail 网络**。这种网络下，「先按节点聚合、再在节点内转发」的两级通信（hybrid）能更自然地贴合轨道结构；而「每对 rank 直接 RDMA」的扁平通信（direct）则更适合任意连通的通用网络。`allow_hybrid_mode` 正是用来在两种风格间切换。

---

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 职责 | 本讲关注点 |
|---|---|---|
| [deep_ep/buffers/elastic.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py) | `ElasticBuffer` 的 Python 实现 | 构造末尾如何读取并暴露拓扑属性；`get_physical_domain_size` / `get_logical_domain_size` 包装方法 |
| [deep_ep/utils/envs.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/envs.py) | 模块级工具函数 | 不依赖 buffer 对象、只凭 `ProcessGroup` 就能查拓扑的便捷函数 |
| [csrc/kernels/backend/api.cuh](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/api.cuh) | C++ 侧 NCCL 后端接口声明 | `get_physical_domain_size` / `get_logical_domain_size` 声明、`NCCLSymmetricMemoryContext` 的物理/逻辑字段 |
| [csrc/kernels/backend/nccl.cu](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu) | C++ 侧 NCCL 后端实现 | 物理域/逻辑域推导的真正实现、GIN 连接类型选择、`is_scaleup_nvlink` 判定 |
| [csrc/kernels/backend/symmetric.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp) | 对称内存分配 | `allow_hybrid_mode` 与 `num_scaleup_ranks` 如何决定走哪种对称内存布局 |

调用方向（从上到下）：

```
Python: buffer.get_physical_domain_size() / get_logical_domain_size()
   └─ envs.py: get_physical_domain_size(group)  (模块级便捷函数)
        └─ _C.get_physical_domain_size(nccl_comm_handle)
             └─ C++ nccl.cu: get_physical_domain_size(nccl_comm)   ← 真正问 NCCL team
```

---

## 4. 核心概念与源码讲解

本讲按「概念 → 物理域推导 → 逻辑域/hybrid 映射 → is_scaleup_nvlink 落到内存布局」的顺序，拆成四个最小模块。

### 4.1 两套 rank 划分：物理域与逻辑域的概念

#### 4.1.1 概念说明

DeepEP 同时维护**两套**对 `world_size` 的划分，初学者最容易把它们混为一谈：

**物理域（physical domain）——描述硬件连了什么**

| 字段 | 含义 | 直觉 |
|---|---|---|
| `num_nvlink_ranks` | 一个 NVLink 域里的 rank 数 | 一个节点里有几张 GPU |
| `num_rdma_ranks` | 跨节点（RDMA）方向上的 rank 数 | 集群里有几个节点 |

二者满足 \(\,num\_ranks = num\_rdma\_ranks \times num\_nvlink\_ranks\)。例如 `EP 8 x 2`（每节点 8 卡、2 节点）对应 `num_nvlink_ranks=8`、`num_rdma_ranks=2`、`num_ranks=16`。README 性能表里的 `EP 8` 就是单节点 8 卡（`num_rdma_ranks=1`），`EP 8 x 2`/`EP 8 x 4` 则是多节点。

物理域是**客观事实**，由硬件和 NCCL 探测决定，用户和 `allow_hybrid_mode` 都改不了它。

**逻辑域（logical domain）——描述内核按什么方式路由**

| 字段 | 含义 | 直觉 |
|---|---|---|
| `num_scaleout_ranks` | 「向外扩展」方向的 rank 数 | 跨节点那一跳 |
| `num_scaleup_ranks` | 「向上集中」方向的 rank 数 | 节点内那一跳 |

逻辑域是**策略选择**，由 `allow_hybrid_mode` 决定如何从物理域「投影」过来。同一个 `EP 8 x 2`，逻辑域可以有两种取值（详见 4.3）。

> 一句话区分：**物理域问「硬件长什么样」，逻辑域问「内核打算怎么发数据」**。物理域是输入，逻辑域是输出。

#### 4.1.2 核心流程

一个 `ElasticBuffer` 在构造时，拓扑信息的产生流程是：

1. 复用 / 创建 NCCL communicator（[u2-l1](u2-l1-import-init.md)、[u2-l2](u2-l2-elastic-buffer-ctor.md) 已讲）。
2. C++ 侧 `NCCLSymmetricMemoryContext` 构造函数问 NCCL：`lsaSize`（NVLink 域大小）是多少？
3. 由 `num_nvl_ranks` 反推 `num_rdma_ranks = num_ranks / num_nvl_ranks`——得到**物理域**。
4. 按 `allow_hybrid_mode` 把物理域映射成**逻辑域**（scaleout/scaleup）。
5. 计算 `is_scaleup_nvlink`，并据此选择对称内存布局。
6. Python 侧构造函数末尾把这些值读出来，作为 `self.num_*` 属性暴露给用户。

伪代码：

```
num_ranks      = world_size
num_nvl_ranks  = nccl.lsaSize              # 来自 NCCL team 探测
num_rdma_ranks = num_ranks / num_nvl_ranks # 物理域

if allow_hybrid_mode:
    num_scaleout_ranks, num_scaleup_ranks = num_rdma_ranks, num_nvl_ranks
else:
    num_scaleout_ranks, num_scaleup_ranks = 1, num_ranks            # 逻辑域

is_scaleup_nvlink = (num_scaleup_ranks == num_nvl_ranks)
```

#### 4.1.3 源码精读

C++ 侧把物理域/逻辑域字段集中放在 `NCCLSymmetricMemoryContext` 结构体里，并明确分成 Logical / Physical 两组，这正好对应本节的两套划分：

[deep_ep/buffers/elastic.py — `ElasticBuffer.__init__` 末尾读取并暴露拓扑属性](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L356-L362)。这段代码先把逻辑域读出来、用它算 `scaleout_rank_idx`/`scaleup_rank_idx`，再读物理域：

```python
# Logical rank indices
self.num_scaleout_ranks, self.num_scaleup_ranks = self.get_logical_domain_size()
self.scaleout_rank_idx = self.rank_idx // self.num_scaleup_ranks
self.scaleup_rank_idx  = self.rank_idx %  self.num_scaleup_ranks

# Physical rank indices
self.num_rdma_ranks, self.num_nvlink_ranks = self.get_physical_domain_size()
```

注意一个细节：`scaleout_rank_idx` 和 `scaleup_rank_idx` 是**从逻辑域**算出来的（用 `num_scaleup_ranks` 做整除/取模），而 `num_rdma_ranks`/`num_nvlink_ranks` 只是「读出来摆着」。这暗示了**内核路由实际依赖的是逻辑域，物理域更多是诊断/建模用途**（[u3-l3](u3-l3-sm-qp-analytical.md) 的 SM 建模会用物理域）。

[api.cuh 中 `NCCLSymmetricMemoryContext` 的字段分组](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/api.cuh#L53-L64)：可以看到 `Logical` 与 `Physical` 两组字段是分开声明、一一对应的（`scaleout_rank_idx ↔ rdma_rank_idx`、`scaleup_rank_idx ↔ nvl_rank_idx`），还有那个关键的 `is_scaleup_nvlink`：

```cpp
// Logical
int num_scaleout_ranks, num_scaleup_ranks;
int scaleout_rank_idx, scaleup_rank_idx;

// Physical
int num_rdma_ranks, num_nvl_ranks;
int rdma_rank_idx, nvl_rank_idx;
bool is_scaleup_nvlink;
```

#### 4.1.4 代码实践

**实践目标**：在还没有构造 buffer 的情况下，直接凭一个 `ProcessGroup` 查出物理域，验证「它只依赖硬件」。

**操作步骤**：

1. 复制 [tests/elastic/test_ep.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py) 里 `init_dist` 的拉起方式（单机 8 卡，`WORLD_SIZE=1` 节点、`num_local_ranks=8`）。
2. 在构造任何 `ElasticBuffer` **之前**，调用模块级便捷函数：

   ```python
   from deep_ep.utils.envs import get_physical_domain_size
   print(get_physical_domain_size(group))
   ```

3. 观察输出。

**需要观察的现象**：单机 8 卡环境下应输出 `(1, 8)`，即 `num_rdma_ranks=1`、`num_nvlink_ranks=8`。

**预期结果**：`num_rdma_ranks * num_nvlink_ranks == world_size` 恒成立（这里 \(1 \times 8 = 8\)）。如果你在双节点 8 卡环境下跑，应得到 `(2, 8)`。

> 待本地验证：单机环境的 `(1, 8)` 与你机器的实际 NVLink 拓扑一致；部分 PCIe GPU（如成对 NVLink 的 A100）可能 `num_nvlink_ranks` 较小，参见 [envs.py:check_nvlink_connections](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/envs.py#L145-L180) 里的 PCIe GPU 只支持 EP2 的注释。

#### 4.1.5 小练习与答案

**练习 1**：`EP 8 x 4` 拓扑下，`num_rdma_ranks`、`num_nvlink_ranks`、`num_ranks` 各是多少？

**答案**：每节点 8 卡 → `num_nvlink_ranks=8`；4 个节点 → `num_rdma_ranks=4`；全局 `num_ranks = 8 × 4 = 32`。

**练习 2**：物理域和逻辑域，哪一个是 `allow_hybrid_mode` 能改变的？为什么？

**答案**：只能改变**逻辑域**。物理域由硬件（NVLink/RDMA 实际连通性）经 NCCL team 探测决定，是客观事实；逻辑域是「如何把物理域投影成发送策略」的人为选择，所以 `allow_hybrid_mode` 只影响逻辑域。

---

### 4.2 物理域的推导：NCCL team 与 get_physical_domain_size

#### 4.2.1 概念说明

4.1 说了物理域「由 NCCL 探测」，但具体探测什么？答案就是 2.2 节引入的 **NCCL team**。DeepEP 不读 `nvidia-smi`、不扫 PCI 拓扑，而是直接问 NCCL 两个 team 的大小：

- `ncclTeamWorld(comm).nRanks` → 全部 rank = `num_ranks`。
- `ncclTeamLsa(comm).nRanks` → NVLink 域 = `num_nvl_ranks`。

「LSA」= Local Shared Address，即「本地能共享寻址」的 rank 组——在一个紧耦合的 NVLink 节点内，GPU 之间能直接 load/store 彼此显存（经 NCCL 的 LSA 抽象），所以 NCCL 把它们归进同一个 LSA team。这个 team 的大小，正是「单节点 GPU 数」。

有了 `num_ranks` 和 `num_nvl_ranks`，`num_rdma_ranks` 就是一个除法：

\[
\text{num\_rdma\_ranks} = \frac{\text{num\_ranks}}{\text{num\_nvl\_ranks}}
\]

之所以敢整除，是因为大规模 GPU 集群几乎总是「每个节点的 GPU 数相同」。

#### 4.2.2 核心流程

`get_physical_domain_size` 的执行过程极其简短，本质是「两次 team 查询 + 一次整除 + 一次断言」：

```
comm = (ncclComm_t)nccl_comm                       # 把 int64 句柄还原回 NCCL communicator
num_ranks     = ncclTeamWorld(comm).nRanks          # 全局 rank 数
num_nvl_ranks = ncclTeamLsa(comm).nRanks            # NVLink 域大小
assert num_ranks % num_nvl_ranks == 0               # 整除性保护
return (num_ranks / num_nvl_ranks, num_nvl_ranks)   # (num_rdma_ranks, num_nvl_ranks)
```

注意返回顺序是 `(num_rdma_ranks, num_nvlink_ranks)`——跨节点方向在前、节点内方向在后。这与逻辑域返回 `(num_scaleout_ranks, num_scaleup_ranks)` 的顺序保持一致（「外/上」配对），方便上下对照。

#### 4.2.3 源码精读

[nccl.cu:get_physical_domain_size — 真正向 NCCL team 提问的地方](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L49-L54)：这是物理域推导的全部真相，只有 6 行：

```cpp
std::tuple<int, int> get_physical_domain_size(const int64_t& nccl_comm) {
    const auto comm = reinterpret_cast<ncclComm_t>(nccl_comm);
    const int num_ranks = ncclTeamWorld(comm).nRanks, num_nvl_ranks = ncclTeamLsa(comm).nRanks;
    EP_HOST_ASSERT(num_ranks % num_nvl_ranks == 0);
    return {num_ranks / num_nvl_ranks, num_nvl_ranks};
}
```

[api.cuh 中这两个函数的声明](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/api.cuh#L41-L43)：注意逻辑域版本比物理域版本多了一个 `allow_hybrid_mode` 参数——这已经预告了 4.3 节的核心：物理域不需要任何策略输入，逻辑域需要。

```cpp
std::tuple<int, int> get_physical_domain_size(const int64_t& nccl_comm);
std::tuple<int, int> get_logical_domain_size(const int64_t& nccl_comm, const bool& allow_hybrid_mode);
```

值得特别一提的是：`NCCLSymmetricMemoryContext` 构造函数里有**另一处**也算了 `num_nvl_ranks`，而且来源不同——它来自 `dev_comm.lsaSize`（设备端 communicator），见 [nccl.cu 构造函数 L104-L107](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L103-L107)。也就是说，DeepEP 实际上有两条获取 `num_nvl_ranks` 的路径：一条是 host 侧的 `ncclTeamLsa`（用于 `get_physical_domain_size` 这个查询 API），一条是 device 侧的 `dev_comm.lsaSize`（用于构造对称内存上下文，这是 buffer 真正建窗口时走的路径）。两者在一致的 NCCL 环境下应当相等。

[envs.py 中模块级包装 get_physical_domain_size](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/envs.py#L116-L127)：它只做一件事——从 `ProcessGroup` 取出 NCCL communicator 句柄，转交 C++：

```python
def get_physical_domain_size(group: dist.ProcessGroup) -> Tuple[int, int]:
    return _C.get_physical_domain_size(get_nccl_comm_handle(group).get())
```

[elastic.py 中实例方法包装](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L549-L557) 则委托给 `self.runtime`（C++ `ElasticBuffer` 对象），后者内部存的也是同一个 communicator。所以模块级函数和实例方法返回的结果一致。

#### 4.2.4 代码实践

**实践目标**：验证「物理域只问 NCCL team、与 buffer 是否构造无关」。

**操作步骤**：

1. 写一个最小脚本（参考 [u1-l4](u1-l4-run-first-test.md) 的 `init_dist`）拉起单机 8 卡 NCCL 集群，得到 `group`。
2. 在**构造 `ElasticBuffer` 之前**调用 `get_physical_domain_size(group)`，打印结果。
3. 再构造一个 `ElasticBuffer`，调用 `buffer.get_physical_domain_size()`，打印结果。
4. 对比两次输出。

**需要观察的现象**：两次输出完全相同（都是 `(1, 8)`）。

**预期结果**：因为两者底层查的是同一个 NCCL communicator 的同一个 team，结果必然一致。这验证了「物理域是 communicator 的固有属性，不依赖 buffer」。

> 待本地验证：若你的集群 NCCL 把节点内 8 卡切成了不同的 LSA 域（例如 NUMA 分裂导致 LSA 不全连通），`num_nvl_ranks` 可能小于 8，此时构造 buffer 时的断言可能触发——这是一个值得记录的诊断信号。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接用 `torch.cuda.device_count()` 当作 `num_nvlink_ranks`？

**答案**：`device_count()` 只反映「本进程能看到几张卡」，无法知道这些卡之间是否真的 NVLink 全连通，更无法处理 `CUDA_VISIBLE_DEVICES` 限制、多进程分裂等情形。而 `ncclTeamLsa` 是 NCCL 在真实通信建立后探测出的「能共享寻址的本地 rank 组」，能正确反映 DeepEP 实际可用的 NVLink 域。

**练习 2**：`get_physical_domain_size` 的返回顺序是 `(num_rdma_ranks, num_nvlink_ranks)`。如果某个调用者误把它当成 `(num_nvlink_ranks, num_rdma_ranks)` 来用，在什么拓扑下会「看起来没错」而掩盖 bug？

**答案**：在 `num_rdma_ranks == num_nvlink_ranks` 的拓扑下（例如每节点 2 卡、共 2 节点 → `(2, 2)`），两个分量相等，顺序写反也不会暴露。这也是为什么 4.1.3 强调要对照「外/上」配对的语义来读返回值。

---

### 4.3 逻辑域与 hybrid 模式：allow_hybrid_mode 如何决定映射

#### 4.3.1 概念说明

逻辑域是本讲的核心。同一个物理域（比如 `EP 8 x 2`：`num_rdma_ranks=2, num_nvl_ranks=8`），在两种模式下会得到完全不同的逻辑域：

| 模式 | `allow_hybrid_mode` | `num_scaleout_ranks` | `num_scaleup_ranks` | 通信风格 |
|---|---|---|---|---|
| **Hybrid（两级）** | `True` | `= num_rdma_ranks` (=2) | `= num_nvl_ranks` (=8) | 先 RDMA 发到目标节点，再在节点内 NVLink 转发 |
| **Direct（扁平）** | `False` | `= 1` | `= num_ranks` (=16) | 把所有 rank 视作一个扁平集合，直接寻址 |

这就是 4.1.2 伪代码里那个 `if allow_hybrid_mode` 分支的语义。理解它的关键是想清楚「`num_scaleout_ranks=1`」意味着什么——**没有跨节点那一跳**，所有 rank 都被当成「scaleup（向上集中）」一域，于是内核走扁平的 direct 路径，每对 rank 之间用一条直连的 RDMA/NVLink 通道。

> 直觉记忆：**hybrid = 「节点是中转站」（两级投递）；direct = 「所有人点对点」（一跳直达）**。

那么为什么要有两种模式？关键在于 2.3 节的**多平面/多轨道网络**：

- **多平面网络（multi-plane）**：每个节点的 8 张卡分别接到 8 张不同的网卡（轨道），不同轨道的流量物理隔离。在这种网络上，如果让每对 rank 都直接 RDMA（direct），流量会跨轨道乱窜，难以利用轨道隔离的带宽；而 hybrid「先按节点聚合到某一张卡的网口、再在节点内 NVLink 分发」，天然贴合「每轨一条网卡」的结构。
- **通用网络**：如果节点间是任意连通的（非轨道式），direct 的点对点直达更简单、延迟更低。

所以 `allow_hybrid_mode` 的默认值是 `True`（[elastic.py:240](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L240)），但 README 与构造函数文档都明确指出：**在多平面网络上，你可能需要 `allow_hybrid_mode=0`（走 direct RDMA 内核）**。

#### 4.3.2 核心流程

逻辑域的推导在 C++ 侧有两个**入口**，但用的是同一套算术：

**入口 A：查询函数 `get_logical_domain_size`**（先调 `get_physical_domain_size` 再分支）

```
(num_rdma_ranks, num_nvl_ranks) = get_physical_domain_size(comm)
if allow_hybrid_mode:
    return (num_rdma_ranks, num_nvl_ranks)        # (scaleout, scaleup)
else:
    return (1, num_rdma_ranks * num_nvl_ranks)     # (1, num_ranks)
```

**入口 B：构造函数 `NCCLSymmetricMemoryContext`**（已经在构造时算过物理域，直接复用）

除了算 `num_scaleout_ranks`/`num_scaleup_ranks`，构造函数还顺手算了对应的 `*_rank_idx`（本 rank 在逻辑域里的下标）：

```
if allow_hybrid_mode:
    num_scaleout_ranks, num_scaleup_ranks = num_rdma_ranks, num_nvl_ranks
    scaleout_rank_idx, scaleup_rank_idx   = rdma_rank_idx, nvl_rank_idx
else:
    num_scaleout_ranks, num_scaleup_ranks = 1, num_ranks
    scaleout_rank_idx, scaleup_rank_idx   = 0, rank_idx
```

注意 `*_rank_idx` 的取法：hybrid 模式下逻辑下标直接复用物理下标（`scaleout↔rdma`、`scaleup↔nvl`），而 direct 模式下 `scaleout_rank_idx` 恒为 0、`scaleup_rank_idx` 就是全局 `rank_idx`——因为 direct 把所有 rank 压成一个 scaleup 域。

#### 4.3.3 源码精读

[nccl.cu:get_logical_domain_size — 逻辑域推导的全部算术](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L56-L60)：注意它**复用**了 `get_physical_domain_size`，逻辑域是建立在物理域之上的：

```cpp
std::tuple<int, int> get_logical_domain_size(const int64_t& nccl_comm, const bool& allow_hybrid_mode) {
    const auto [num_rdma_ranks, num_nvl_ranks] = get_physical_domain_size(nccl_comm);
    return {allow_hybrid_mode ? num_rdma_ranks : 1,
            allow_hybrid_mode ? num_nvl_ranks : num_rdma_ranks * num_nvl_ranks};
}
```

[nccl.cu 构造函数中 scaleout/up 域与下标的计算](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L109-L117)：这是 buffer 真正建窗口时走的路径，比查询函数多了 `*_rank_idx` 的计算，并紧接着算出 `is_scaleup_nvlink`（4.4 节细讲）：

```cpp
// Calculate scaleout/up domain size
if (allow_hybrid_mode) {
    num_scaleout_ranks = num_rdma_ranks, num_scaleup_ranks = num_nvl_ranks;
    scaleout_rank_idx = rdma_rank_idx, scaleup_rank_idx = nvl_rank_idx;
} else {
    num_scaleout_ranks = 1, num_scaleup_ranks = num_ranks;
    scaleout_rank_idx = 0, scaleup_rank_idx = rank_idx;
}
is_scaleup_nvlink = num_scaleup_ranks == num_nvl_ranks;
```

**hybrid 模式与 NCCL GIN 连接类型的绑定**——这是 `allow_hybrid_mode` 影响「网络友好性」的真正落点。GIN（Group Init eXchange）是 NCCL 2.30+ 提供的对称内存直连抽象，DeepEP V2 用它取代了 V1 的 NVSHMEM。NCCL 的 GIN 有两种连接类型，构造函数按 `allow_hybrid_mode` 二选一：

[nccl.cu 构造函数：按 hybrid 模式选 GIN 类型与连接方式](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L84-L101)。最关键的三处：

```cpp
// (1) GIN 类型断言：hybrid 要 railed，direct 要任意 ginType
EP_HOST_ASSERT(
    (allow_hybrid_mode ? props.railedGinType : props.ginType) != NCCL_GIN_TYPE_NONE and
    "NCCL GIN is unavailable. This is usually due to a network configuration issue, "
    "such as `allow_hybrid_mode=0` (disable direct RDMA kernels) in multi-plane network.");

// (2) 连接类型：hybrid=RAIL（按轨道），direct=FULL（全连接）
reqs.ginConnectionType = allow_hybrid_mode ? NCCL_GIN_CONNECTION_RAIL: NCCL_GIN_CONNECTION_FULL;
```

读这段代码时要注意一个**容易读反**的点：错误信息里写的是 ``such as `allow_hybrid_mode=0` ... in multi-plane network``，字面像是「在多平面网络里关掉 hybrid」——结合 4.3.1 的分析，它的准确含义是「**当前选的模式所需的 GIN 类型不可用**」。具体而言：

- 选 hybrid（`allow_hybrid_mode=1`）需要 NCCL 能提供 `railedGinType`（轨道式 GIN）；如果网络不是轨道式、`railedGinType == NONE`，断言失败 → 这时你要改成 `allow_hybrid_mode=0` 走 direct。
- 选 direct（`allow_hybrid_mode=0`）需要最基本的 `ginType != NONE`；如果连这个都没有（如纯 NVLink 单节点但 `num_ranks>1` 又被强行建 GIN），也会失败。

这条断言是「拓扑与模式不匹配」时的第一手诊断信息，调试时务必看清自己处于哪个分支。

[elastic.py 中 allow_hybrid_mode 的文档说明](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L209-L211)：官方文档把「对多平面/多轨道网络更友好」明确写在 hybrid 模式的语义里：

> Hybrid mode uses hierarchical RDMA + NVLink communication to achieve higher bandwidth, and is more friendly to multi-plane/multi-rail networks.

**hybrid 模式还显著改变 QP（队列对）分配**：[elastic.py:328-L335](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L328-L335)。hybrid 模式鼓励每个 channel（以及 notify warps）各占一个独立 QP，所以默认分配 65（fast-atomic NIC）或 129 个 QP；direct 模式只需 17 个：

```python
if self.allow_hybrid_mode:
    num_allocated_qps = 65 if check_fast_rdma_atomic_support() else 129
else:
    num_allocated_qps = 17
```

QP 数量差异的根源是：hybrid 的两级通信（多个 channel 并发 scaleout + scaleup）需要更多并发队列，而 direct 的扁平通信队列需求少。QP 分配的解析式细节留到 [u3-l3](u3-l3-sm-qp-analytical.md)。

#### 4.3.4 代码实践

**实践目标**：在同一集群上，对比 `allow_hybrid_mode=True` 与 `False` 时逻辑域的取值，亲手验证 4.3.1 的表格。

**操作步骤**：

1. 单机 8 卡拉起 `group`。
2. 用模块级函数分别查询：

   ```python
   from deep_ep.utils.envs import get_logical_domain_size
   print("hybrid  :", get_logical_domain_size(group, allow_hybrid_mode=True))
   print("direct  :", get_logical_domain_size(group, allow_hybrid_mode=False))
   ```

3. （可选）多节点环境下重复一遍。

**需要观察的现象**：

- 单机 8 卡（`num_rdma_ranks=1, num_nvl_ranks=8`）：
  - `allow_hybrid_mode=True`  → `(1, 8)`（scaleout=1、scaleup=8）
  - `allow_hybrid_mode=False` → `(1, 8)`（scaleout=1、scaleup=8）
  - **两者相同！** 因为单节点时 `num_rdma_ranks=1`，hybrid 的 `(1, num_nvl_ranks)` 与 direct 的 `(1, num_ranks)` 在 `num_ranks == num_nvl_ranks` 时相等。
- 双节点 16 卡（`EP 8 x 2`，`num_rdma_ranks=2, num_nvl_ranks=8`）：
  - `allow_hybrid_mode=True`  → `(2, 8)`
  - `allow_hybrid_mode=False` → `(1, 16)`

**预期结果**：单机时两种模式逻辑域相同（这正是为什么单机测试看不出 hybrid 的差别，[u1-l4](u1-l4-run-first-test.md) 的 SO 带宽恒为 0）；只有多节点才能看到 `(2,8)` 与 `(1,16)` 的分野。

> 待本地验证：单机环境的 `(1,8)/(1,8)` 一致性；多节点环境的 `(2,8)` vs `(1,16)`。注意：实际**构造** `ElasticBuffer(allow_hybrid_mode=False)` 在多平面网络上才更可能成功，在通用轨道网络上 hybrid 才是默认最优——查询逻辑域不会触发 GIN 断言，但真正建 buffer 会。

#### 4.3.5 小练习与答案

**练习 1**：为什么单机 8 卡时，`allow_hybrid_mode` 取 True 或 False 对 `num_scaleup_ranks` 没有影响？

**答案**：单机时 `num_rdma_ranks=1`。hybrid 给出 `num_scaleup_ranks = num_nvl_ranks = 8`；direct 给出 `num_scaleup_ranks = num_ranks = 8`（因为 `num_ranks = num_nvl_ranks`），两者相等。只有当 `num_rdma_ranks > 1`（多节点）时，`num_nvl_ranks` 才会小于 `num_ranks`，两种模式才会分开。

**练习 2**：构造函数里那条 GIN 断言的报错信息提到 `` `allow_hybrid_mode=0` ... in multi-plane network``。请解释：如果你在**轨道式（railed）网络**上错误地设了 `allow_hybrid_mode=0`，会发生什么？

**答案**：`allow_hybrid_mode=0` 选 direct，需要 `props.ginType != NONE`。在轨道式网络上 `ginType` 大概率为 `NONE`（只有 `railedGinType` 可用），于是断言失败、buffer 构造抛出异常。修复方法是改回 `allow_hybrid_mode=True`（默认值），让 DeepEP 用 `railedGinType` + `NCCL_GIN_CONNECTION_RAIL`。

**练习 3**：hybrid 模式默认分配 65 或 129 个 QP，direct 模式只分配 17 个。请从「两级通信 vs 扁平通信」的角度解释为什么 hybrid 需要更多 QP。

**答案**：hybrid 模式下，scaleout（RDMA）和 scaleup（NVLink）两级转发会划分出多个并发 channel，每个 channel 各占一个独立 QP 以避免队列排队、提升并发吞吐；外加 1 个供 notify warps。direct 模式没有两级转发，所有 rank 在一个扁平域里点对点直达，少量 QP 即可复用，所以只要 17 个。

---

### 4.4 is_scaleup_nvlink：拓扑判定如何影响缓冲区布局

#### 4.4.1 概念说明

4.3 看到构造函数末尾有这样一行看似平淡的赋值：

```
is_scaleup_nvlink = (num_scaleup_ranks == num_nvl_ranks)
```

它回答的问题是：**「逻辑上的 scaleup 域，是否恰好等于物理上的 NVLink 域？」**

- **hybrid 模式**：`num_scaleup_ranks = num_nvl_ranks`，所以 `is_scaleup_nvlink = True`——scaleup 走的就是 NVLink。
- **direct 模式**：`num_scaleup_ranks = num_ranks`，只有当 `num_ranks == num_nvl_ranks`（即单节点）时才为 True；多节点 direct 模式下 scaleup 域跨越了多个 NVLink 域，所以 `is_scaleup_nvlink = False`。

为什么这个布尔量重要？因为它（连同 `allow_hybrid_mode` 和 `num_scaleup_ranks`）决定了**对称内存走哪种布局**，进而决定了 CPU 段如何被映射、Engram/PP 等特性能否工作。

DeepEP 的对称内存有三种实现（见 [symmetric.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp)）：

| 类 | 适用场景 | CPU 段布局 |
|---|---|---|
| `GPUSymmetricMemory` | 纯 GPU 缓冲（`num_cpu_bytes==0`） | 无 CPU 段 |
| `ElasticSymmetricMemory` | direct 模式 + 有 CPU 段 | GPU + 单段 CPU |
| `HybridElasticSymmetricMemory` | **hybrid 模式 + 有 CPU 段** | GPU + 「每 scaleup rank 一段 CPU」拼接 |

关键差别在 `HybridElasticSymmetricMemory`：它把**本节点内所有 scaleup rank 的 CPU 段**连续映射进本 rank 的虚拟地址空间，布局为 `[GPU VRAM (前)] [CPU rank0 | CPU rank1 | ... | CPU rank(N-1) (后)]`，其中 \(N = \text{num\_scaleup\_ranks}\)。这样内核就能像访问一块连续显存一样，用统一偏移访问节点内任意 peer 的 CPU 段（用于 Engram 的 RDMA get 拉取，见 [u7-l2](u7-l2-engram.md)）。

#### 4.4.2 核心流程

`is_scaleup_nvlink` 与缓冲区布局的联动流程：

1. 构造函数算出 `num_scaleup_ranks`（依赖 `allow_hybrid_mode`）。
2. 算 `is_scaleup_nvlink = (num_scaleup_ranks == num_nvl_ranks)`。
3. 把 `allow_hybrid_mode`、`num_scaleup_ranks`、`scaleout_rank_idx` 一起传给 `symmetric::alloc`。
4. `symmetric::alloc` 根据 `allow_hybrid_mode`（在有 CPU 段时）选择 `HybridElasticSymmetricMemory` 或 `ElasticSymmetricMemory`。
5. `HybridElasticSymmetricMemory` 用 `num_scaleup_ranks` 决定要拼接几段 CPU、用 `scaleout_rank_idx` 定位「本节点对应哪一组 CPU 句柄」。

总字节数也随之不同：

\[
\text{num\_bytes}_{\text{hybrid}} = \text{num\_gpu\_bytes} + \text{num\_cpu\_bytes} \times \text{num\_scaleup\_ranks}
\]

即 hybrid 模式下，缓冲区总大小会随 scaleup 域（节点内 GPU 数）线性放大——这是为什么 [u2-l2](u2-l2-elastic-buffer-ctor.md) 强调「带 CPU 段的 hybrid buffer 占用更大」。

#### 4.4.3 源码精读

[nccl.cu:117 — is_scaleup_nvlink 的全部判定](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L109-L117)：注意它紧跟在 scaleout/up 域计算之后，用刚算出的 `num_scaleup_ranks` 与物理域的 `num_nvl_ranks` 比较：

```cpp
is_scaleup_nvlink = num_scaleup_ranks == num_nvl_ranks;
```

[nccl.cu 构造函数把布局参数传给 symmetric::alloc](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L119-L124)：注意传的是 `allow_hybrid_mode`、`num_scaleup_ranks`、`scaleout_rank_idx`，而**不是** `is_scaleup_nvlink` 本身——`is_scaleup_nvlink` 主要作为上下文信息保留在 context 里供后续/host 侧判断，真正决定布局的开关是 `allow_hybrid_mode`：

```cpp
this->symmetric_memory = symmetric::alloc(
    num_bytes - num_cpu_bytes, num_cpu_bytes,
    allow_hybrid_mode, num_scaleup_ranks, scaleout_rank_idx,
    cpu_comm);
```

[symmetric.hpp:alloc — 按 allow_hybrid_mode 选择布局](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp#L291-L317)：在有 CPU 段（`num_cpu_bytes > 0`）时，分支选择 hybrid 还是 elastic 布局：

```cpp
if (num_cpu_bytes > 0) {
    if (allow_hybrid_mode) {
        result = std::make_shared<HybridElasticSymmetricMemory>(
            cpu_comm, num_gpu_bytes, num_cpu_bytes,
            num_scaleup_ranks, scaleout_rank_idx);
    } else {
        result = std::make_shared<ElasticSymmetricMemory>(num_gpu_bytes, num_cpu_bytes);
    }
} else {
    result = std::make_shared<GPUSymmetricMemory>(num_gpu_bytes);
}
```

[symmetric.hpp:HybridElasticSymmetricMemory — 用 num_scaleup_ranks 拼接 CPU 段](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp#L191-L211)：可以看到总字节数里 CPU 部分乘以了 `num_scaleup_ranks`，并且要为每个 scaleup rank 准备一个 `cpu_handle`：

```cpp
this->num_bytes = num_gpu_bytes + num_cpu_bytes * num_scaleup_ranks;
// ...
cpu_handles(num_scaleup_ranks)
```

[Python 侧 num_max_local_ranks 也按 allow_hybrid_mode 分支](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L285-L291)：这是 Python 侧提前为 NCCL 虚拟地址空间预留容量用的，hybrid 时按「最大本地 rank 数」放大，direct 时退化为 1（因为 direct 不拼接多段 CPU）：

```python
num_max_local_ranks = int(os.getenv('EP_NUM_MAX_LOCAL_RANKS', 16)) if allow_hybrid_mode else 1
num_registered_bytes = num_gpu_bytes + num_cpu_bytes * num_max_local_ranks + (1 << 32)
```

把 4.4 串起来：**`allow_hybrid_mode` → `num_scaleup_ranks` → `is_scaleup_nvlink` → `HybridElasticSymmetricMemory` 拼接 N 段 CPU → 缓冲区总字节随节点内 GPU 数放大**。这就是「一个布尔开关最终改变内存布局」的完整链条。

#### 4.4.4 代码实践

**实践目标**：在 `EP_BUFFER_DEBUG=1` 下构造 buffer，把 `num_scaleup_ranks`、`is_scaleup_nvlink`、缓冲区字节数三者串起来观察。

**操作步骤**：

1. 设 `export EP_BUFFER_DEBUG=1`。
2. 单机 8 卡，构造两个**带 CPU 段**的 buffer（Engram 场景才会用 CPU 段，可参考 [tests/elastic/test_engram.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_engram.py)），分别用 `allow_hybrid_mode=True` 和 `False`：

   ```python
   buf_hybrid = ElasticBuffer(group, num_bytes=..., num_cpu_bytes=..., allow_hybrid_mode=True)
   buf_direct = ElasticBuffer(group, num_bytes=..., num_cpu_bytes=..., allow_hybrid_mode=False)
   print(buf_hybrid.num_scaleup_ranks, buf_direct.num_scaleup_ranks)
   ```

3. 观察 debug 输出里的 `Initializing EP elastic buffer with X bytes (cpu: Y)`。

**需要观察的现象**：

- `buf_hybrid.num_scaleup_ranks == 8`（单机 8 卡的 NVLink 域大小）。
- `buf_direct.num_scaleup_ranks == 8`（单机时 direct 与 hybrid 相同，因为 `num_ranks == num_nvl_ranks`）。
- 由于单机两者 `num_scaleup_ranks` 相同，注册字节数也相同。要看到差异需在**多节点**重复：hybrid 的 `num_scaleup_ranks=8`、direct 的 `num_scaleup_ranks=16`，于是 hybrid 注册的 CPU 段总量是 direct 的一半（按每段等大估算）。

**预期结果**：单机两者一致；多节点 hybrid 的 CPU 段总量 \(\propto num\_nvl\_ranks\)，direct 的 \(\propto num\_ranks\)。这与 `is_scaleup_nvlink` 在「多节点 direct」下为 False、在 hybrid 下为 True 完全对应。

> 待本地验证：多节点环境下两种模式的注册字节数比值。注意 Engram/CPU 段属于实验特性（[u7-l2](u7-l2-engram.md)），若环境不便构造，可只做源码阅读型观察：对照 [symmetric.hpp:211](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp#L191-L211) 的 `num_bytes` 公式手算两种模式的字节数。

#### 4.4.5 小练习与答案

**练习 1**：在 `EP 8 x 2`（双节点 16 卡）下，分别对 `allow_hybrid_mode=True/False` 求 `is_scaleup_nvlink`。

**答案**：
- hybrid：`num_scaleup_ranks = num_nvl_ranks = 8` → `is_scaleup_nvlink = True`。
- direct：`num_scaleup_ranks = num_ranks = 16 ≠ 8` → `is_scaleup_nvlink = False`。

**练习 2**：为什么 `HybridElasticSymmetricMemory` 要把「本节点所有 scaleup rank 的 CPU 段」拼进自己的虚拟地址空间，而不是像 direct 那样只放一段？

**答案**：hybrid 的两级通信模型里，scaleup 域内的 rank 互相用 NVLink 直访彼此显存/CPU 段（例如 Engram 用 RDMA get 拉取节点内 peer 的 CPU 存储）。把节点内所有 peer 的 CPU 段在本地 VA 里连续排布，内核就能用一个统一的基地址 + 偏移寻址任意 peer，避免为每个 peer 单独查表/换地址。direct 模式不做节点内二级转发，只需要自己的单段 CPU。

**练习 3**：`is_scaleup_nvlink` 这个字段被算出来并存进 context，但 `symmetric::alloc` 收到的参数里并没有它（收的是 `allow_hybrid_mode`）。这是不是意味着 `is_scaleup_nvlink` 没用？

**答案**：不是。`is_scaleup_nvlink` 主要服务于**后续内核/host 侧的条件判断与诊断**（例如内核里决定是否能直接用 NVLink 写 scaleup 域、调试时打印拓扑诊断），而**布局选择**这个具体决策当前由 `allow_hybrid_mode` 直接驱动（因为 hybrid 布局本就要求 scaleup 域 == NVLink 域）。两者高度相关但不完全等价：`is_scaleup_nvlink` 是「事实判断」，`allow_hybrid_mode` 是「策略选择」，保留前者便于在策略与事实不一致时给出诊断。

---

## 5. 综合实践

把本讲四个模块串起来，设计一个**「拓扑侦探」**小任务：在不实际跑 dispatch 的前提下，仅凭查询 API 把一个未知集群的拓扑完全摸清，并预测各模式下的行为。

**任务目标**：给定一个 NCCL `ProcessGroup`（未知节点数、未知每节点 GPU 数），用本讲学到的 API 推断出全部拓扑事实，并预测 `allow_hybrid_mode` 两种取值下的逻辑域、GIN 连接类型、QP 分配数、`is_scaleup_nvlink`、CPU 段拼接段数。

**操作步骤**：

1. 拉起分布式环境（参考 [u1-l4](u1-l4-run-first-test.md) 的 `init_dist`），得到 `group`，记录 `group.size()`。
2. 查物理域并推导节点数：

   ```python
   from deep_ep.utils.envs import get_physical_domain_size, get_logical_domain_size
   num_rdma_ranks, num_nvlink_ranks = get_physical_domain_size(group)
   num_nodes = num_rdma_ranks
   gpus_per_node = num_nvlink_ranks
   print(f"集群: {num_nodes} 节点 x {gpus_per_node} 卡/节点 = {group.size()} rank")
   ```

3. 查两种模式的逻辑域：

   ```python
   so_hyb, su_hyb = get_logical_domain_size(group, allow_hybrid_mode=True)
   so_dir, su_dir = get_logical_domain_size(group, allow_hybrid_mode=False)
   ```

4. 把下表填完（**先手算，再用代码验证**）：

   | 量 | hybrid 模式 | direct 模式 |
   |---|---|---|
   | `num_scaleout_ranks` | ? | ? |
   | `num_scaleup_ranks` | ? | ? |
   | GIN 连接类型 | `NCCL_GIN_CONNECTION_RAIL` | `NCCL_GIN_CONNECTION_FULL` |
   | 默认 QP 数 | 65 或 129 | 17 |
   | `is_scaleup_nvlink` | ? | ? |
   | `HybridElastic` CPU 段拼接数 | `num_scaleup_ranks` | 不适用（走 `ElasticSymmetricMemory`） |

5. 反向校验：用 `group.size() == num_rdma_ranks * num_nvlink_ranks` 验证整除性；用 `num_scaleup_ranks_hybrid == num_nvlink_ranks` 验证 `is_scaleup_nvlink` 在 hybrid 下必为 True。

**需要观察的现象**：手算与代码输出完全一致；单节点时两模式的逻辑域相同（`num_rdma_ranks=1` 导致），多节点时分开。

**预期结果**：以 `EP 8 x 2` 为例，填表应为：

| 量 | hybrid | direct |
|---|---|---|
| scaleout / scaleup | (2, 8) | (1, 16) |
| `is_scaleup_nvlink` | True | False |
| QP 数 | 65/129 | 17 |

> 待本地验证：在你自己的集群上填出这张表。若构造真实 buffer 时遇到 GIN 断言失败，对照 4.3.3 的「分支诊断」判断是该用 hybrid 还是 direct。

---

## 6. 本讲小结

- DeepEP 同时维护**物理域**（`num_rdma_ranks`/`num_nvlink_ranks`，硬件事实）和**逻辑域**（`num_scaleout_ranks`/`num_scaleup_ranks`，路由策略）两套 rank 划分；前者是输入、后者是输出。
- 物理域直接来自 NCCL team：`num_nvl_ranks = ncclTeamLsa(comm).nRanks`，`num_rdma_ranks = num_ranks / num_nvl_ranks`，与 buffer 是否构造无关。
- `allow_hybrid_mode` 决定物理域到逻辑域的映射：hybrid 给出 `(num_rdma, num_nvl)` 两级通信；direct 给出 `(1, num_ranks)` 扁平通信。
- hybrid 模式绑定 `NCCL_GIN_CONNECTION_RAIL` + `railedGinType`，对多平面/多轨道网络友好；direct 绑定 `NCCL_GIN_CONNECTION_FULL` + `ginType`；模式与网络不匹配时构造期 GIN 断言会明确报错。
- 单节点（`num_rdma_ranks=1`）下两种模式逻辑域相同，这是单机测试看不出 hybrid 差别、SO 带宽恒为 0 的根因。
- `is_scaleup_nvlink = (num_scaleup_ranks == num_nvl_ranks)`：hybrid 下恒 True、多节点 direct 下为 False；它（经 `allow_hybrid_mode`）决定对称内存走 `HybridElasticSymmetricMemory`（拼接 N 段 CPU）还是 `ElasticSymmetricMemory`，从而影响缓冲区总字节数。

## 7. 下一步学习建议

本讲只解决了「拓扑域是什么、怎么算」，还没回答两个紧邻的问题：

1. **「这些域到底要多大的缓冲区？」** —— 逻辑域确定后，dispatch/combine 的 send/recv 布局如何按域大小累加、如何对齐 2 MB，是下一讲 [u3-l2 缓冲区内存布局与大小解析计算](u3-l2-buffer-layout-sizing.md) 的主题。
2. **「hybrid 模式到底要开多少 SM、多少 QP？」** —— `num_scaleout_ranks`/`num_scaleup_ranks` 如何进入带宽建模、解析式地推出最优 SM 数与 QP 数，见 [u3-l3 SM 与 QP 数量的解析式计算](u3-l3-sm-qp-analytical.md)。

如果你想提前看「拓扑域在内核里怎么被用」，可以跳到 [u5-l2 Hybrid Dispatch：scaleout + scaleup 两级通信](u5-l2-hybrid-dispatch.md)，但建议先完成 u3-l2/u3-l3，把布局与 SM 建模补齐，再看内核会顺畅得多。另外，[u3-l4 NCCL Gin 后端与对称内存上下文](u3-l4-nccl-gin-symmetric.md) 会更深入地讲本讲提到的 `NCCLSymmetricMemoryContext` 与 CUDA VMM 对称内存，是本讲 4.4 的自然延伸。
