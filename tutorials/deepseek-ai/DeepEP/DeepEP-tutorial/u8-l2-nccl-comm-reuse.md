# NCCL communicator 复用与拓扑探测

## 1. 本讲目标

本讲聚焦 DeepEP V2 在拿到一个 `torch.distributed.ProcessGroup` 之后做的两件底层的事：

1. **获取一个可用的 NCCL communicator**：能复用 PyTorch 已经建好的就复用，不能复用就用 unique id 自己新建一个，并用一个轻量句柄 `NCCLCommHandle` 管理它的生命周期。
2. **从这个 communicator 探测网络拓扑**：推导物理域（`num_rdma_ranks` / `num_nvl_ranks`）、逻辑域（`num_scaleout_ranks` / `num_scaleup_ranks`）以及 NVLink 连通性标志 `is_scaleup_nvlink`。

学完本讲，你应该能够：

- 读懂 [`deep_ep/utils/comm.py`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/comm.py) 中 `get_nccl_comm_handle` 的三级回退逻辑（缓存 → 复用 PyTorch → 新建）。
- 解释 `NCCLCommHandle` 的 `managed` 字段如何决定「这个 comm 由谁销毁」。
- 说清 `EP_REUSE_NCCL_COMM` 与 `force_new_comm` 的取舍，特别是**为什么带 CPU buffer（`num_cpu_bytes > 0`）时一定要强制新建 communicator**。
- 读懂 [`csrc/kernels/backend/nccl.cu`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu) 如何从 NCCL 的 team/dev_comm 结构里把拓扑信息「问」出来。

本讲是 u3-l1（物理域与逻辑域）的「下钻」：u3-l1 讲的是这些域**是什么**，本讲讲的是这些域的数值**从哪里来、由谁探测**。

## 2. 前置知识

在进入源码前，先建立几个基础概念。本讲假设你已学过 u3-l1（拓扑域）与 u3-l4（NCCL Gin 后端与对称内存）。

### 2.1 NCCL communicator 是什么

NCCL（NVIDIA Collective Communications Library）是 NVIDIA 提供的 GPU 集合通信库。一次集合通信（如 `all_reduce`、`all_gather`）需要所有参与进程先「互相认识」——建好连接、协商拓扑、分配 ring/tree。这个「认识」的产物就是一个 **NCCL communicator**（`ncclComm_t`），它内部持有所有 peer 的连接、传输通道（NVLink/RDMA）与排名信息。

建立一个 communicator 很贵（要 bootstrap、握手、建连接），所以**复用**比重建划算。PyTorch 在 `dist.init_process_group(backend='nccl')` 时就已经为默认 group 建好了一个 NCCL communicator，DeepEP 自然想借来用。

### 2.2 unique id 与 `ncclCommInitRank`

NCCL 建立新 communicator 的标准方式是：

1. rank 0 调用 `ncclGetUniqueId` 得到一个 magic cookie（unique id）。
2. 把它 `all_gather` 给所有 rank。
3. 每个 rank 拿着同一个 unique id 调用 `ncclCommInitRank(comm, num_ranks, unique_id, my_rank)`，NCCL 内部完成握手。

这就是 DeepEP 在「无法复用」时的回退路径。

### 2.3 物理域、逻辑域、对称窗口（承接 u3-l1 / u3-l4）

- **物理域**：硬件事实。`num_nvl_ranks` 是「与我共享 NVLink 寻址域的 GPU 数」，`num_rdma_ranks = num_ranks / num_nvl_ranks`。
- **逻辑域**：路由策略。`allow_hybrid_mode` 决定把 `num_ranks` 投影成 `(num_scaleout_ranks, num_scaleup_ranks)`。
- **对称窗口**：通过 `ncclCommWindowRegister` 在 communicator 上注册的一块「每个 rank 对称布局」的内存，配合 Gin 后端做跨 rank 寻址（见 u3-l4）。

本讲要回答：这些 `num_*_ranks` 到底是哪个函数、从 communicator 的哪个字段里读出来的？

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [`deep_ep/utils/comm.py`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/comm.py) | Python 侧。定义 `NCCLCommHandle` 与 `get_nccl_comm_handle`，负责 communicator 的「复用 / 新建 / 缓存 / 销毁」全生命周期。 |
| [`csrc/kernels/backend/nccl.cu`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu) | C++ 侧。封装 `create_nccl_comm`/`destroy_nccl_comm`，并实现 `get_physical_domain_size` / `get_logical_domain_size` 与 `NCCLSymmetricMemoryContext` 构造函数（拓扑探测发生在这里）。 |
| [`csrc/kernels/backend/api.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/api.cuh) | C++ 头文件。声明上述函数，并定义 `NCCLSymmetricMemoryContext` 结构体（把物理/逻辑拓扑字段集中存放）。 |
| [`deep_ep/buffers/elastic.py`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py) | 调用方。`ElasticBuffer.__init__` 里 `force_new_comm=num_cpu_bytes > 0` 这一行是本讲实践的核心。 |
| [`csrc/kernels/backend/symmetric.hpp`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp) | 对称内存分配器。CPU buffer 触发的 `NCCL_ELASTIC_BUFFER_REGISTER` 在这里设置。 |
| [`deep_ep/utils/envs.py`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/envs.py) | 把 C++ 的 `get_physical_domain_size` / `get_logical_domain_size` 暴露成 Python 工具函数，内部也调用 `get_nccl_comm_handle`。 |

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：**4.1 NCCL communicator 的复用、缓存与生命周期**（NCCL 复用），**4.2 从 communicator 探测拓扑**（拓扑探测）。

### 4.1 NCCL communicator 的复用、缓存与生命周期

#### 4.1.1 概念说明

DeepEP 的所有跨 rank 通信（dispatch / combine / barrier / engram / pp / agrs）都需要一个 NCCL communicator 作为底座——既用它注册对称窗口、创建 Gin device communicator，也（在 V2 里）直接复用它的传输通道。

但 DeepEP 并不想每次构造 `ElasticBuffer` 都新建一个 communicator，因为：

- **贵**：`ncclCommInitRank` 要全 rank bootstrap、握手、建 NVLink/RDMA 连接，开销在百毫秒到秒级。
- **已经有一个了**：用户调 `dist.init_process_group('nccl')` 时，PyTorch 已经建好了一个 NCCL communicator，连接都热乎着。

于是 DeepEP 的策略是「**三级回退**」：

1. **命中缓存**：同一个 ProcessGroup 之前问过就直接返回旧句柄。
2. **复用 PyTorch 的 communicator**：从 PyTorch 后端对象上把裸 `ncclComm_t` 指针「借」出来用（不归 DeepEP 销毁）。
3. **自己新建**：用 unique id 走 `ncclCommInitRank` 建一个全新的、归 DeepEP 销毁的 communicator。

围绕这套策略，DeepEP 用一个极简的包装类 `NCCLCommHandle` 把「裸指针 + 谁负责销毁」打包在一起，并用一个进程级字典 `_storage` 做缓存。

#### 4.1.2 核心流程

`get_nccl_comm_handle(group, force_new_comm)` 的决策流程（伪代码）：

```
if (not force_new_comm) and (group in _storage):       # ① 命中缓存
    return _storage[group]

backend = group._get_backend(cuda)
if (not force_new_comm) and hasattr(backend, '_comm_ptr')   # ② 复用 PyTorch
        and EP_REUSE_NCCL_COMM == 1:
    handle = NCCLCommHandle(backend._comm_ptr(), managed=False)
    _storage[group] = handle
    return handle

# ③ 自己新建（all_gather unique id + ncclCommInitRank）
uid = all_gather(_C.get_local_nccl_unique_id())
key = time.time_ns() if force_new_comm else group
handle = NCCLCommHandle(_C.create_nccl_comm(uid[0], size, rank), managed=True)
_storage[key] = handle
return handle
```

三个关键点：

- **`managed` 字段决定销毁责任**：`managed=False`（复用 PyTorch）时，`__del__` 不会销毁——PyTorch 自己管；`managed=True`（自建）时，`__del__` 调 `ncclCommAbort` 销毁。
- **`force_new_comm` 同时绕过缓存与复用**：它让函数直接跳到第③步。注意此时缓存 key 用 `time.time_ns()`（时间戳）而不是 `group`，这样**多次强制新建互不覆盖**，每个都拿到独立的 communicator。
- **`EP_REUSE_NCCL_COMM` 是复用总开关**：默认 `'1'`（复用），设为 `'0'` 则跳过第②步、永远自建。

#### 4.1.3 源码精读

先看包装类 `NCCLCommHandle`。它只是「裸指针 + managed 标志 + 一个销毁函数指针」的三元组：

[comm.py:L11-L37](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/comm.py#L11-L37) —— `NCCLCommHandle`：`managed=True` 时析构调用 `_C.destroy_nccl_comm`（底层是 `ncclCommAbort`），`managed=False` 时什么都不做。这一行 `managed` 标志就是「生命周期归属」的全部秘密。

进程级缓存就是一个模块全局字典：

[comm.py:L39-L39](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/comm.py#L39-L39) —— `_storage = dict()`：key 通常是 ProcessGroup，value 是 `NCCLCommHandle`。

核心函数 `get_nccl_comm_handle`：

[comm.py:L42-L75](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/comm.py#L42-L75) —— 三级回退的完整实现。逐段对应：

- **第①段缓存**（L57-L58）：`not force_new_comm and group in _storage` 才命中。注意 `force_new_comm` 会刻意绕过缓存，保证每次都拿到全新 comm。
- **第②段复用 PyTorch**（L61-L64）：`group._get_backend(torch.device('cuda'))` 拿到 PyTorch 的 NCCL 后端对象；新版本 PyTorch 提供 `_comm_ptr()` 方法返回裸 `ncclComm_t` 指针。`hasattr(backend, '_comm_ptr')` 是对新旧 PyTorch 的兼容判断，`EP_REUSE_NCCL_COMM` 默认 `'1'`。`managed=False` 表示「借来的，不归我管」。
- **第③段自建**（L66-L75）：rank 0 用 `_C.get_local_nccl_unique_id()`（底层 `ncclGetUniqueId`）生成 cookie，`all_gather_object` 分发给所有 rank，再各自 `_C.create_nccl_comm`（底层 `ncclCommInitRank`）。`key = time.time_ns() if force_new_comm else group` 这一行很关键——强制新建时用纳秒时间戳作 key，多次调用互不覆盖。

对应的 C++ 实现在 nccl.cu：

[nccl.cu:L20-L41](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L20-L41) —— `get_local_unique_id` 与 `create_nccl_comm`：标准 NCCL 三件套（`ncclGetUniqueId` / `ncclCommInitRank`）。`create_nccl_comm` 把 `ncclComm_t` 强转成 `int64_t` 返回给 Python，这就是 `_comm_ptr()` / `_storage` 里存的那个「裸指针整数」。开启 `EP_BUFFER_DEBUG` 时会打印 `New NCCL host communicator created (rank/num_ranks)`，这是**观察是否新建的唯一可视化信号**。

销毁函数：

[nccl.cu:L43-L47](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L43-L47) —— `destroy_nccl_comm` 用 `ncclCommAbort`（而非 `ncclCommDestroy`）。`Abort` 会强制中断在途操作，适合 DeepEP 这种「确定要拆掉」的场景。

清空缓存的入口（程序退出或测试 teardown 时用）：

[comm.py:L78-L83](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/comm.py#L78-L83) —— `destroy_all_managed_nccl_comm`：清空 `_storage`。注意它只清字典；真正销毁依赖各 `NCCLCommHandle` 的 `__del__`（且仅 `managed=True` 的会被销毁）。这是测试间避免 comm 泄漏的兜底。

#### 4.1.4 代码实践

> **本实践为本讲的主任务**（对应大纲的 practice_task）：阅读 `get_nccl_comm_handle` 与调用方，解释「带 CPU buffer 时为何 `force_new_comm`」，并讨论复用 PyTorch communicator 的利弊。

**实践目标**：把 `force_new_comm=num_cpu_bytes > 0` 这一行（[elastic.py:L301](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L301)）彻底搞懂。

**操作步骤**：

1. 打开 [elastic.py:L285-L301](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L285-L301)，观察 `num_cpu_bytes > 0` 分支在创建 comm **之前**做了哪些环境变量准备：`NCCL_SYM_REUSE_SYSMEM_HANDLES`（[L282](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L282)，多平面场景）与 `NCCL_WIN_STRIDE`（[L298](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L298)，超大 buffer 时把 NCCL 对称窗口步长放大到 4 GiB）。
2. 打开 [symmetric.hpp:L291-L317](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp#L291-L317)，确认 `num_cpu_bytes > 0` 会走 `ElasticSymmetricMemory` / `HybridElasticSymmetricMemory`，并在 [L314-L315](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/symmetric.hpp#L314-L315) 设置 `NCCL_ELASTIC_BUFFER_REGISTER=1`。
3. 打开 [nccl.cu:L82-L101](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L82-L101)，看清 DeepEP 会在 communicator 上做的「重操作」：`ncclDevCommCreate` 建 Gin device communicator、申请最多 129 个 exclusive QP、`ncclCommWindowRegister` 注册一大块含 CPU 段的对称窗口。
4.（可选，需多 GPU 环境）跑 `tests/elastic/test_ep.py` 时 `export EP_BUFFER_DEBUG=1`，对比「纯 GPU buffer」与「带 CPU buffer（构造时传 `num_cpu_bytes>0`）」两种情况下，日志里 `New NCCL host communicator created` 是否出现。

**需要观察的现象**：`EP_BUFFER_DEBUG=1` 下，带 CPU buffer 的构造会打印 `New NCCL host communicator created (rank/num_ranks)`；纯 GPU buffer 且 PyTorch 版本够新时**不**打印（走复用路径）。

**预期结果 / 参考答案**（带 CPU buffer 为何 `force_new_comm`）：

| 角度 | 解释 |
|---|---|
| **① 隔离：避免污染 PyTorch 的 communicator** | 带 CPU buffer 时 DeepEP 要在这条 comm 上注册一块**含 CPU 段的弹性对称窗口**、并把 NCCL 对称窗口 VA 步长放大（`NCCL_WIN_STRIDE`，4 GiB 对齐），还要建一个占用十几个到上百个 QP 的 Gin device communicator（`ncclDevCommCreate`）。这些都是**对 communicator 的有状态改造**。PyTorch 的 comm 仍被它自己的 `all_reduce`/`all_gather` 持续使用，把上述重操作挂到共享 comm 上存在相互干扰的风险。新建一条 DeepEP 独占的 comm 是保守且正确的隔离方式。 |
| **② 生命周期归属** | `force_new_comm=True` 走第③段，`managed=True`，DeepEP 在 buffer 析构时可以独立 `ncclCommAbort` 它而不动 PyTorch 的 comm。若复用（`managed=False`），DeepEP 不能销毁，window/Gin 的拆除虽由 C++ `finalize()` 负责，但底层 ncclComm 的生命周期仍绑死在 PyTorch 身上，耦合脆弱。 |
| **③ 配置生效时机** | `NCCL_WIN_STRIDE` / `NCCL_SYM_REUSE_SYSMEM_HANDLES` 在 [elastic.py:L282/L298](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L282-L298) 于 comm 创建**之前**设置，它们配置 NCCL 对称窗口/VA 子系统的行为；PyTorch 的 comm 在 `init_process_group` 早就建好了，早于这些变量，无法回头套用。新建一条能保证这些设置在 comm 初始化时已就位（源码注释 [L296-L297](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L296-L297) 也承认这套 env-var 方案是 fragile 的过渡做法）。 |

> 说明：角度①中「具体哪种干扰」属于 NCCL 内部实现细节，**精确的失败现象待本地验证**；代码层面能确定的是 DeepEP 用 `force_new_comm` 做了防御性隔离，并把 comm 的创建时机安排在相关 env-var 设置之后。

**复用 PyTorch communicator 的好处**（`num_cpu_bytes == 0` 时）：

- 省掉一次 `ncclCommInitRank` 的全 rank bootstrap 与连接建立（百毫秒~秒级），冷启动更快。
- 复用 PyTorch 已建好的 NVLink/RDMA 传输，少占连接、文件描述符与 QP 资源。
- 减少 NCCL communicator 总数，降低 NCCL 内部 ring/tree 状态开销。

**复用的潜在风险**：

- **耦合**：DeepEP 在共享 comm 上建 Gin device communicator 与对称窗口，与 PyTorch 自身集合通信共享同一条 comm 的内部状态；极端情况下可能相互影响（这正是带 CPU buffer 时选择不复用的原因）。
- **版本依赖**：依赖较新 PyTorch 暴露的 `_comm_ptr()` API；旧版本没有该属性会回退到自建（见 `hasattr` 判断）。
- **无生命周期所有权**：`managed=False`，DeepEP 不能销毁；若 PyTorch 先于 DeepEP 销毁 comm，DeepEP 侧会留下悬垂指针。故有 `EP_REUSE_NCCL_COMM=0` 这个「强制不复用」的逃生开关。

#### 4.1.5 小练习与答案

**练习 1**：若同一进程内用同一个 `group` 连续构造两个**纯 GPU** 的 `ElasticBuffer`，会创建几条 NCCL communicator？

**答案**：1 条。第一次走第②段（复用 PyTorch）并写入 `_storage[group]`；第二次走第①段缓存命中，直接返回同一个句柄。两次 `managed` 都是 `False`（借 PyTorch 的）。

**练习 2**：把 `EP_REUSE_NCCL_COMM` 设为 `0`，但 `force_new_comm=False`，会走哪一段？`managed` 是什么？

**答案**：走第③段（自建）。因为第②段的 `int(os.getenv('EP_REUSE_NCCL_COMM', '1'))` 为 0，条件不成立。此时 `key = group`（不是时间戳），`managed=True`——DeepEP 自己建、自己销毁，但仍会进缓存，后续同 group 调用会命中。

**练习 3**：为什么 `force_new_comm=True` 时缓存 key 要用 `time.time_ns()` 而不是 `group`？

**答案**：强制新建的本意是「我要一条全新的、独占的 comm」。若仍用 `group` 作 key，第二次强制新建会覆盖第一次的句柄，导致第一条 comm 泄漏（其 `NCCLCommHandle` 不再被 `_storage` 引用，可能被 GC 销毁，但语义混乱）。用纳秒时间戳保证每次强制新建都落到不同 key，多条独占 comm 共存。

### 4.2 从 communicator 探测拓扑：物理域、逻辑域与 NVLink 连通性

#### 4.2.1 概念说明

u3-l1 已经讲过物理域 / 逻辑域的**定义**，本讲回答它们的**数值从哪来**。答案是：DeepEP 不自己探测硬件，而是**问 NCCL**——NCCL 在建 communicator 时已经探测过一遍拓扑，DeepEP 只是把 NCCL 内部结构体里的字段读出来。

NCCL 内部把 rank 组织成若干「team」：

- `ncclTeamWorld(comm)`：整张通信图，`.nRanks` 就是 `num_ranks`。
- `ncclTeamLsa(comm)`：**LSA（Local Shareable Address）team**，即「能通过 NVLink 共享寻址域互访」的 rank 集合，`.nRanks` 就是节点内 NVLink 域大小 `num_nvl_ranks`。

于是有恒等式：

\[
\text{num\_ranks} = \text{num\_rdma\_ranks} \times \text{num\_nvl\_ranks}, \qquad \text{num\_rdma\_ranks} = \text{num\_ranks} / \text{num\_nvl\_ranks}
\]

注意这里有**两个探测入口**，结果一致但调用时机不同：

- **轻量入口** `get_physical_domain_size` / `get_logical_domain_size`（[nccl.cu:L49-L60](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L49-L60)）：直接读 `ncclTeamWorld` / `ncclTeamLsa`，**不需要构造 buffer**，供 `get_buffer_size_hint` 这类「先算尺寸再决定要不要建」的场景用（见 [envs.py:L116-L142](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/envs.py#L116-L142)）。
- **重量入口** `NCCLSymmetricMemoryContext` 构造函数（[nccl.cu:L62-L140](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L62-L140)）：在真正建对称内存时，从 Gin device communicator（`ncclDevComm_t`）的 `lsaSize` 字段再读一遍，并把所有拓扑字段一次性算好存进 context。

#### 4.2.2 核心流程

`NCCLSymmetricMemoryContext` 构造函数里拓扑探测的流程：

```
1. 复用传入的 ncclComm_t（不重建）。
2. 若 num_ranks>1 且未禁用 Gin：
   a. ncclCommQueryProperties 查 NCCL 支持的 Gin 类型
      （hybrid 看 railedGinType，direct 看 ginType）；
   b. 断言对应 Gin 类型 != NONE（否则报「网络配置问题」）；
   c. 填充 ncclDevCommRequirements（QP 数、队列深度、SL、信号数、连接类型）；
   d. ncclDevCommCreate 创建 device communicator。
3. 从 dev_comm 读拓扑：
   num_nvl_ranks  = dev_comm.lsaSize
   nvl_rank_idx   = dev_comm.lsaRank
   num_rdma_ranks = num_ranks / num_nvl_ranks
   rdma_rank_idx  = rank_idx / num_nvl_ranks
4. 投影到逻辑域（由 allow_hybrid_mode 决定）：
   hybrid:  num_scaleout_ranks = num_rdma_ranks, num_scaleup_ranks = num_nvl_ranks
   direct:  num_scaleout_ranks = 1,               num_scaleup_ranks = num_ranks
5. is_scaleup_nvlink = (num_scaleup_ranks == num_nvl_ranks)
6. 用算好的拓扑分配对称内存、注册窗口。
```

#### 4.2.3 源码精读

轻量探测函数：

[nccl.cu:L49-L60](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L49-L60) —— `get_physical_domain_size`：`num_ranks = ncclTeamWorld(comm).nRanks`，`num_nvl_ranks = ncclTeamLsa(comm).nRanks`，断言 `num_ranks % num_nvl_ranks == 0`，返回 `(num_rdma_ranks, num_nvl_ranks)`。`get_logical_domain_size` 在此基础上按 `allow_hybrid_mode` 投影：开启返回 `(num_rdma_ranks, num_nvl_ranks)`，关闭返回 `(1, num_ranks)`。这两个函数**不需要 Gin、不需要窗口**，纯读 NCCL team，所以能用在 buffer 构造之前的尺寸估算里。

重量探测在 `NCCLSymmetricMemoryContext` 构造函数里。先看 Gin 类型查询与 device communicator 创建：

[nccl.cu:L82-L101](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L82-L101) —— 这里出现了**拓扑探测的副产品：Gin 类型校验**。`ncclCommQueryProperties` 问 NCCL「这条 comm 的网络支持哪种 Gin」：hybrid 模式要求 `railedGinType != NONE`（rail-optimized，对应多平面/多轨道网络），direct 模式要求 `ginType != NONE`。若不支持，断言失败并给出明确提示——这正解释了 u3-l1 提到的「模式与网络不匹配时构造期 GIN 断言报错」。随后 `reqs.ginConnectionType` 在 hybrid 下取 `NCCL_GIN_CONNECTION_RAIL`、direct 下取 `NCCL_GIN_CONNECTION_FULL`，与 u3-l1 的逻辑域绑定关系一一对应。

真正读拓扑的两行：

[nccl.cu:L103-L107](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L103-L107) —— `num_nvl_ranks = dev_comm.lsaSize, nvl_rank_idx = dev_comm.lsaRank`：从 Gin device communicator 直接读 LSA 域大小与本 rank 在 LSA 域内的序号；`num_rdma_ranks = num_ranks / num_nvl_ranks, rdma_rank_idx = rank_idx / num_nvl_ranks`：导出 RDMA 域。两条 `EP_HOST_ASSERT` 校验「物理 rank == rdma_rank × nvl_size + nvl_rank」的整除分解成立，保证后续所有跨 rank 寻址的算术不出错。

逻辑域投影与 NVLink 标志：

[nccl.cu:L109-L117](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L109-L117) —— `allow_hybrid_mode` 为真时逻辑域等于物理域 `(num_rdma, num_nvl)`；为假时退化为扁平 `(1, num_ranks)`。最后一行 `is_scaleup_nvlink = num_scaleup_ranks == num_nvl_ranks`：判定「scaleup 这一段是否恰好落在 NVLink 域内」——hybrid 下恒为真，多节点 direct 下为假（因为此时 `num_scaleup_ranks = num_ranks > num_nvl_ranks`）。这个标志在 u3-l1 里决定对称内存走 `HybridElasticSymmetricMemory`（拼接多段 CPU）还是 `ElasticSymmetricMemory`。

这些字段全部集中存放在 `NCCLSymmetricMemoryContext` 结构体里：

[api.cuh:L46-L92](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/api.cuh#L46-L92) —— 结构体把 `num_scaleout_ranks/num_scaleup_ranks`（逻辑）、`num_rdma_ranks/num_nvl_ranks`（物理）、`is_scaleup_nvlink`、`comm/dev_comm/window/mapped_window_ptr`（NCCL 句柄）集中存放，是后续所有 dispatch/combine/barrier 内核获取拓扑与对称指针的统一入口。Python 侧 `ElasticBuffer` 暴露的 `num_scaleout_ranks` 等属性（[elastic.py:L549-L559](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L549-L559)）最终都源自这里。

#### 4.2.4 代码实践

**实践目标**：在单机 8 卡上亲眼看到「物理域 / 逻辑域是从 NCCL communicator 问出来的」，并验证 `is_scaleup_nvlink` 在单机下的取值。

**操作步骤**：

1. 在单机 8 卡环境跑一个最小脚本，用 `deep_ep.utils.envs` 提供的工具函数（无需构造 buffer）：

   ```python
   # 示例代码：仅用于演示调用，非项目原有脚本
   import torch.distributed as dist
   from deep_ep.utils.envs import get_physical_domain_size, get_logical_domain_size

   # 假设 init_dist 已完成，group 为 default group
   group = dist.group.WORLD
   print("physical (rdma, nvl):", get_physical_domain_size(group))
   print("logical  (so, su) hybrid:", get_logical_domain_size(group, allow_hybrid_mode=True))
   print("logical  (so, su) direct :", get_logical_domain_size(group, allow_hybrid_mode=False))
   ```

2. 对照 [envs.py:L116-L142](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/envs.py#L116-L142)，确认这两个 Python 函数内部都调用 `get_nccl_comm_handle(group).get()` 拿到裸 comm 指针后，转交 C++ 的 `get_physical_domain_size` / `get_logical_domain_size`。
3. 构造一个真实的 `ElasticBuffer`，打印其 `num_rdma_ranks`、`num_nvlink_ranks`、`num_scaleout_ranks`、`num_scaleup_ranks`（[elastic.py:L549-L559](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L549-L559)），与步骤 1 的结果对比。

**需要观察的现象**：单机 8 卡（`num_ranks=8`，单节点）下：

- `get_physical_domain_size` → `(1, 8)`（`num_rdma_ranks=1`，`num_nvl_ranks=8`）。
- `get_logical_domain_size(hybrid=True)` → `(1, 8)`；`(hybrid=False)` → `(1, 8)`。单节点下两者相同（因为 `num_rdma_ranks=1`），这正是 u3-l1 强调的「单机看不出 hybrid 差别」。
- 构造 buffer 后 `is_scaleup_nvlink == True`（`num_scaleup_ranks==8 == num_nvl_ranks==8`）。

**预期结果**：单机 8 卡下，无论 `allow_hybrid_mode` 取何值，`(num_scaleout_ranks, num_scaleup_ranks)` 都是 `(1, 8)`；`is_scaleup_nvlink` 恒为 `True`。要看到非平凡的 hybrid 结果（`num_scaleout_ranks > 1`），必须有多节点（`num_rdma_ranks > 1`）环境——**待本地多节点环境验证**。

#### 4.2.5 小练习与答案

**练习 1**：`get_physical_domain_size` 与 `NCCLSymmetricMemoryContext` 构造函数都读 `num_nvl_ranks`，它们分别从 NCCL 的哪个结构读？为什么需要读两次？

**答案**：前者从 `ncclTeamLsa(comm).nRanks`（host 侧 team）读，不需要 Gin、不需要窗口，供 buffer 构造前的尺寸估算用；后者从 `dev_comm.lsaSize`（device 侧 Gin communicator）读，是真正建对称内存时的权威值。读两次是因为「估算尺寸」（轻量、早）与「真正建内存」（重量、晚）是两个阶段，且后者依赖 Gin device communicator 的存在。

**练习 2**：多节点 direct 模式（`allow_hybrid_mode=False`，2 节点每节点 8 卡）下，`is_scaleup_nvlink` 是什么？为什么？

**答案**：`False`。此时 `num_scaleup_ranks = num_ranks = 16`，而 `num_nvl_ranks = 8`，`16 != 8`，所以 `is_scaleup_nvlink = False`。语义上：direct 模式把所有 16 个 rank 都放进 scaleup 逻辑域，但其中只有同节点的 8 个能走 NVLink，跨节点的得走 RDMA，故 scaleup 不全是 NVLink。

**练习 3**：若 NCCL 探测到的网络不支持 hybrid 所需的 `railedGinType`，代码会在哪一行、以什么方式失败？

**答案**：在 [nccl.cu:L88-L91](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L88-L91) 的 `EP_HOST_ASSERT` 失败，提示「NCCL GIN is unavailable ... such as `allow_hybrid_mode=0` in multi-plane network」。这是 host 侧断言，发生在 `NCCLSymmetricMemoryContext` 构造期（即 `ElasticBuffer` 构造期），早于任何 kernel 启动。

## 5. 综合实践

把本讲两个最小模块串起来，完成一个「拓扑与 comm 来源」端到端观察任务（需单机多卡；多节点结论待本地验证）：

1. 用 `init_dist` 拉起单机 8 卡（参考 u1-l4），分别用 `get_physical_domain_size` / `get_logical_domain_size` 打印拓扑，记录数值。
2. **不开** CPU buffer 构造一个 `ElasticBuffer`（`num_cpu_bytes=0`），`EP_BUFFER_DEBUG=1` 下观察日志**没有** `New NCCL host communicator created`（说明走了复用路径），打印其 `num_nvlink_ranks` 等属性，验证与步骤 1 一致。
3. **另开**一个带 CPU buffer 的 `ElasticBuffer`（构造时令 `num_cpu_bytes>0`），观察日志**出现** `New NCCL host communicator created (rank/8)`（说明 `force_new_comm` 生效、走了自建路径）。
4. 把 `EP_REUSE_NCCL_COMM` 设为 `0` 重复步骤 2，观察即便纯 GPU buffer 也会打印 `New NCCL host communicator created`（说明复用被禁用、回退自建）。
5. 用一句话总结：「DeepEP 的拓扑数值来自 NCCL team/dev_comm 的字段；comm 来源由 `EP_REUSE_NCCL_COMM` 与 `force_new_comm` 共同决定，带 CPU buffer 时一定自建。」

> 若无多卡环境，步骤 1-4 的运行现象为「待本地验证」；可改为纯源码阅读：画出从 `ElasticBuffer.__init__` → `get_nccl_comm_handle` → `create_nccl_comm` / `_comm_ptr` → `NCCLSymmetricMemoryContext`（读 `dev_comm.lsaSize`）的调用链，并标注每段 `managed` 的取值。

## 6. 本讲小结

- DeepEP 获取 NCCL communicator 走**三级回退**：缓存命中 → 复用 PyTorch 的 `_comm_ptr()` → 用 unique id 自建，由 [`comm.py:get_nccl_comm_handle`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/comm.py#L42-L75) 统一调度。
- `NCCLCommHandle.managed` 决定销毁责任：复用时 `False`（PyTorch 管），自建时 `True`（DeepEP 用 `ncclCommAbort` 销毁）；进程级缓存是模块字典 `_storage`。
- `force_new_comm=num_cpu_bytes > 0`（[elastic.py:L301](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L301)）：带 CPU buffer 时强制自建，目的是**隔离**（不把含 CPU 段的弹性窗口 + 重型 Gin 改造挂到 PyTorch 共享 comm 上）、**掌握生命周期**、并在 `NCCL_WIN_STRIDE` 等设置就位后再建 comm。
- `EP_REUSE_NCCL_COMM=0` 可全局禁用复用、强制自建，是排查复用风险的逃生开关。
- 拓扑数值**来自 NCCL**：轻量入口读 `ncclTeamWorld/ncclTeamLsa`（[nccl.cu:L49-L60](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L49-L60)），重量入口读 Gin `dev_comm.lsaSize`（[nccl.cu:L103-L107](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L103-L107)），再按 `allow_hybrid_mode` 投影成逻辑域。
- `is_scaleup_nvlink = (num_scaleup_ranks == num_nvl_ranks)`：hybrid 下恒真，多节点 direct 下为假，决定对称内存走 hybrid 还是普通弹性分配。

## 7. 下一步学习建议

- **承接 u3-l4**：本讲只到「communicator 与拓扑字段就位」，对称内存如何用 CUDA VMM 建起来、`ncclCommWindowRegister` 如何注册窗口，详见 u3-l4（NCCL Gin 后端与对称内存上下文）。
- **顺读 u8-l3（环境变量体系）**：本讲提到的 `EP_BUFFER_DEBUG`、`EP_REUSE_NCCL_COMM`、`EP_DISABLE_GIN`、`EP_OVERRIDE_RDMA_SL`、`NCCL_WIN_STRIDE` 等都属于 DeepEP 的环境变量体系，u8-l3 会系统梳理四大类变量。
- **顺读 u8-l1（PTX 原语）**：communicator 与拓扑解决「连得上、找得到」的问题；真正在内核里搬数据用的 TMA / mbarrier / fence.proxy 原语在 u8-l1。
- **延伸阅读**：想深究 NCCL team 与 LSA 的内部结构，可阅读 NCCL 源码中 `ncclTeamLsa`/`ncclTeamWorld` 的定义；想验证多节点拓扑结论，建议在 2 节点 × 8 卡环境重复 4.2.4 的实践。
