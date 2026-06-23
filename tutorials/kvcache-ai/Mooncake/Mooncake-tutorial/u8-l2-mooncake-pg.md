# Mooncake PG：torch.distributed 后端与弹性恢复

> 阶段：advanced　｜　依赖：`u8-l1`（Mooncake EP）
> 代码 HEAD：`1f7f71a18a9dc48e9901d8293c5c3625ba166939`

## 1. 本讲目标

Mooncake PG（Process Group，进程组）是把 Mooncake Transfer Engine 的 RDMA / NVLink 数据通道**包装成一个标准 `torch.distributed` 后端**的组件。有了它，PyTorch 训练 / 推理脚本里只要写 `dist.init_process_group(backend="mooncake", ...)`，`all_reduce`、`all_gather`、`send`/`recv` 这些集合通信调用就会走 Mooncake 的传输栈，而不是 NCCL / Gloo。

PG 在「能通信」之外，还解决了分布式系统里最棘手的一件事：**某个 rank 崩溃了怎么办**。传统做法是整个进程组（往往对应一整张推理服务）重启；PG 的目标是——**不重启整个服务，只换掉坏掉的那个 rank**，让它重新加入并恢复全集 合通信。这就是「弹性恢复（elastic recovery）」。

学完本讲你应该能够：

1. 说清 PG 是**如何把自己注册**成 `torch.distributed` 的 `"mooncake"` / `"mooncake-cpu"` 后端的，以及 `MooncakeBackend` 与 `MooncakeP2PShim` 这两个类的分工。
2. 解释 PG 支持哪些集合通信原语，以及一个关键的 `activeRanks[]` 掩码如何让集合操作**跳过失活 rank** 而不卡死。
3. 描述 rank 失效是如何被**检测、上报、传播**给所有存活 rank 的（连接轮询状态机 + per-peer epoch + 单线程轮询器）。
4. 手画一遍**两阶段弹性恢复协议**：新 rank `join_group` 上线 → 存活 rank 用 `get_peer_state` 轮询确认连通 → `recover_ranks` 激活；并解释 `maxWorldSize`（预留容量）在其中扮演的角色。

## 2. 前置知识

在进入源码之前，先把几个概念对齐。本讲默认你读过 `u8-l1`（Mooncake EP）或对 PyTorch 分布式有基本了解。

**进程组（Process Group）与 `torch.distributed`。** PyTorch 用 `dist.init_process_group(backend=...)` 在多个进程（每个通常绑一张 GPU，称为一个 rank）之间建立通信域。`backend` 决定了底层用什么库收发数据：`nccl`（GPU）、`gloo`（CPU）等。Mooncake PG 新增了 `mooncake`（GPU）和 `mooncake-cpu`（CPU）两个后端。每个后端在 C++ 层对应一个 `c10d::ProcessGroup` 的子类，PyTorch 会把 `dist.all_reduce(...)` 这样的调用翻译成对它的虚函数调用。

**ProcessGroup vs Backend（c10d 的两个抽象）。** 在 PyTorch 的 `c10d` 库里，`ProcessGroup` 是集合通信（all_reduce 等）的基类；`Backend` 是一个更轻量的抽象，专门用于**点对点** `send`/`recv` 的派发（`batch_isend_irecv` → `_get_backend` → `getBackend`）。Mooncake PG 的 `MooncakeBackend` 继承自 `ProcessGroup`，又额外注册了一个 `MooncakeP2PShim`（继承自 `Backend`）来满足点对点派发的需要。这一点后面会专门讲，这里先记住「它需要两个类」。

**rank / world_size / group_size。** `rank` 是某个进程在通信域里的编号（从 0 开始）；`world_size` 是参与通信的进程总数。PG 引入了几个**比 world_size 更细的量**：

- `meta_->size`：**容量（capacity）**，预分配的 rank 槽位上限（含未激活的预留槽）。
- `meta_->activeSize`：**可见规模**，对外由 `dist.get_world_size()` 返回。
- `activeRanks[]`：长度为 `size` 的布尔掩码，标记哪些槽位当前真正参与集合通信。

把「容量」「可见规模」「激活掩码」三者分开，是 PG 实现弹性的核心设计。

**Store（键值存储）。** `c10d::Store` 是 PyTorch 分布式用来交换小量带外元数据的接口（默认实现是基于 TCP 的 HashStore）。PG 用它来发布每个 rank 的「服务器名」「缓冲区地址」等握手信息——正式的批量数据走 RDMA，而谁连谁的**寻址信息**走 Store。本讲里你会反复看到 `server_name_<idx>_<rank>`、`buffer_<idx>_<rank>`、`extension_state_<idx>_<rank>` 这三个键。

**epoch（纪元）。** 在可重启的通信协议里，「epoch」是一个单调递增的计数器，用来区分「重启前」和「重启后」的消息。PG 给每个 peer 维护一个 epoch：一次 `resetPeerState` 会让它 `+1`，于是重启前还在网线上飞着的、带着旧 epoch 的控制消息，到达后被识别为「过期」而丢弃。这是避免脏数据的关键。

**BackoffWaiter（退避等待器）。** PG 大量使用异步轮询，需要一个「既别狂占 CPU、又别睡太久」的等待工具。`pg_utils.h` 里的 `BackoffWaiter` 提供三级退避：先忙等（`PAUSE`）→ 再 `yield` 让出 CPU → 最后指数退避 `sleep`。后面看源码时会多次遇到它。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 作用 |
| --- | --- |
| [mooncake-pg/include/mooncake_backend.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/include/mooncake_backend.h) | `MooncakeBackend`（继承 `ProcessGroup`）与 `MooncakeP2PShim`（继承 `Backend`）的声明；`MooncakeBackendOptions`（含 `maxWorldSize_`）；集合通信虚函数与弹性恢复接口 `joinGroup/getPeerState/recoverRanks/extendGroupSizeTo` 的签名。 |
| [mooncake-pg/src/mooncake_backend.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/mooncake_backend.cpp) | 上述类的实现：构造（引擎初始化、缓冲区注册、元数据发布）、各集合原语（都走 `MooncakeWorker::putTask*`）、以及弹性恢复的四件套（`joinGroup/waitForExtensionState/getPeerState/recoverRanks`）。 |
| [mooncake-pg/src/pg_py.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/pg_py.cpp) | pybind11 绑定。用 `__attribute__((constructor))` 在模块加载时把 `mooncake` / `mooncake-cpu` 注册成 `torch.distributed` 后端；并暴露 `join_group`/`get_peer_state`/`recover_ranks`/`extend_group_size_to` 等 Python 函数。 |
| [mooncake-pg/include/connection_poller.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/include/connection_poller.h) | `ConnectionContext`（每个后端一份的 per-peer 连接状态机）与 `ConnectionPoller`（全进程单例轮询线程）。定义了 `PeerConnectionState` 状态机与 Store 键名约定。 |
| [mooncake-pg/src/connection_poller.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/connection_poller.cpp) | 连接建立（warmup 握手）、失效检测与传播、`extendGroupSizeTo`/`setPollingLimitTo`、轮询主循环 `pollerLoop` 的实现。 |
| [mooncake-pg/include/p2p_proxy.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/include/p2p_proxy.h) | `P2PProxy`：基于 credit 的 RDMA 拉取协议（`send`/`recv` 的底层实现），含 `peer_epoch_[]`、`CreditSlot`/`AckSlot` 的双 token 一致性布局、`resetPeerState` 声明。 |
| [mooncake-pg/src/p2p_proxy.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/p2p_proxy.cpp) | `resetPeerState`（epoch 自增 + 请求重置）、`reportBrokenPeer`（标记 peer 失活并触发重连）等故障恢复逻辑。 |
| [mooncake-pg/include/pg_utils.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/include/pg_utils.h) | `BackoffWaiter` 三级退避等待器，被初始化握手、P2P `Work::wait`、轮询 drain 等多处复用。 |
| [mooncake-pg/include/mooncake_worker.cuh](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/include/mooncake_worker.cuh) | `TransferGroupMeta`（通信域元数据：`size`/`activeSize`/`activeRanks`/`peerConnected`/`segmentIDs`）、常量 `kMaxNumRanks=64`、`MooncakeWorker`（执行集合任务的线程）。 |
| [mooncake-pg/tests/test_pg_elastic.py](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/tests/test_pg_elastic.py) | 弹性恢复的端到端测试：`_extension_worker`（扩容）、`_replacement_recovery_worker`（替换崩溃 rank）、`_fault_detection_worker`（失效后存活 rank 继续通信）。 |
| [mooncake-wheel/mooncake/pg.py](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/mooncake/pg.py) | 面向用户的 `mooncake.pg` Python 模块入口：按 torch 版本后缀动态导入编译产物 `mooncake.pg_<torch_version>`。 |

---

## 4. 核心概念与源码讲解

本讲按四个最小模块拆分：**MooncakeBackend（后端注册）**、**集合通信原语**、**失效检测（连接轮询 + epoch）**、**弹性恢复（两阶段协议）**。前两个对应「能作为后端通信」，后两个对应「坏 rank 能被换掉」。

### 4.1 MooncakeBackend：注册为 torch.distributed 后端

#### 4.1.1 概念说明

要让 `dist.init_process_group(backend="mooncake")` 工作，需要做两件事：

1. **把名字 `"mooncake"` 注册**到 `torch.distributed` 的后端表里，并告诉它「这个名字对应哪个工厂函数、支持哪些设备（cpu / cuda）」。
2. **实现一个 `c10d::ProcessGroup` 子类**（`MooncakeBackend`），重写 `allreduce`/`allgather`/`send` 等虚函数，让 PyTorch 的调用真正落到 Mooncake 的传输栈上。

PG 用一个巧妙手段完成第 1 步：把注册逻辑放在一个 C++ **构造属性函数**（`__attribute__((constructor))`）里。这意味着**只要 `import` 了编译出的扩展模块，注册就自动发生**——不需要用户在脚本里额外调用任何注册 API。这是 pg_py.cpp 里最关键的设计。

而点对点通信（`dist.isend`/`dist.irecv`/`batch_isend_irecv`）走的是 PyTorch 的另一条派发路径，它要求 `getBackend()` 返回一个注册过的 `c10d::Backend` 实例。`MooncakeBackend` 继承的是 `ProcessGroup` 而不是 `Backend`，于是 PG 又造了一个极薄的 `MooncakeP2PShim`（继承 `Backend`），它什么都不做，只把 `send`/`recv` 转发回 `MooncakeBackend`。这就是为什么需要「两个类」。

#### 4.1.2 核心流程

后端注册与构造的流程：

```
import mooncake.pg   (或导入编译产物)
        │
        ▼
[模块加载] __attribute__((constructor)) 触发
        │  register_backend("mooncake", createMooncakeBackend, extended_api=True, devices=("cuda",))
        │  register_backend("mooncake-cpu", createMooncakeCpuBackend, extended_api=True, devices=("cpu",))
        ▼
dist.init_process_group(backend="mooncake", rank=.., world_size=..)
        │  PyTorch 查后端表 → 找到工厂函数
        ▼
createMooncakeBackend(distBackendOpts, backendOptions)
        │  → c10::make_intrusive<MooncakeBackend>(...)
        ▼
MooncakeBackend 构造函数：
   1. 初始化/复用 TransferEngine（engine_）
   2. 注册 send/recv 缓冲区 + CPU 同步区
   3. 创建 P2PProxy + ConnectionContext
   4. 发布本 rank 元数据到 Store（publishLocalPeerMetadata）
   5. 注册到 ConnectionPoller 单例，等待所有 peer 握手成功
   6. 注册 MooncakeP2PShim（setBackend）以便 P2P 派发能找到它
```

`createMooncakeBackend` 与 `createMooncakeCpuBackend` 是两个工厂函数，唯一区别是后者给 `MooncakeBackend` 构造函数传 `isCpu=true`。

#### 4.1.3 源码精读

**注册逻辑——这是「PG 如何成为后端」的答案：**

[mooncake-pg/src/pg_py.cpp:28-47](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/pg_py.cpp#L28-L47) — 这段是模块加载时自动执行的构造函数。它 `import torch.distributed`，拿到 `Backend.register_backend`，然后注册两个名字：`"mooncake-cpu"` 绑定到 `createMooncakeCpuBackend`、设备声明为 `("cpu",)`；`"mooncake"` 绑定到 `createMooncakeBackend`、设备声明为 `("cuda",)`（若编译时开了 `MOONCAKE_EP_USE_MUSA` 则是 `("musa",)`）。两个都传了 `extended_api=true`——这是 PyTorch 「扩展后端 API」的开关，允许后端接收 `pg_options` 等额外参数。

工厂函数本身非常薄：

[mooncake-pg/src/pg_py.cpp:12-26](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/pg_py.cpp#L12-L26) — `createMooncakeBackend` 把 PyTorch 传来的 `distBackendOpts`（含 store、rank、group_size、global_ranks_in_group）和 Mooncake 专属的 `backendOptions` 原样转交给 `MooncakeBackend` 构造函数，并用 `c10::make_intrusive` 包成 `c10d::ProcessGroup` 智能指针返回。

**两个类的分工：**

[mooncake-pg/include/mooncake_backend.h:33-57](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/include/mooncake_backend.h#L33-L57) — `MooncakeP2PShim` 的声明。类注释把来龙去脉讲得很清楚：PyTorch 的 P2P 派发（`batch_isend_irecv`、`isend`、`irecv`）要求 `getBackend()` 返回一个注册过的 `c10d::Backend`；而 `MooncakeBackend` 继承自 `ProcessGroup`（不是 `Backend`），所以额外注册这个 shim 到 `ProcessGroup` 的 `deviceTypeToBackend_` 映射里。shim 只持有一个**非拥有**指针 `owner_`，把 P2P 路径会调到的几个操作（`send`/`recv`/`getBackendName`/`supportsCoalescing`）转发回 `MooncakeBackend`。

`MooncakeBackend` 自身继承 `ProcessGroup`，并重写了全套集合原语：

[mooncake-pg/include/mooncake_backend.h:105-152](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/include/mooncake_backend.h#L105-L152) — 这里集中声明了 PG 支持的所有操作：点对点 `send`/`recv`，集合 `broadcast`/`allreduce`/`allgather`/`_allgather_base`/`_reduce_scatter_base`/`alltoall`/`barrier`/`reduce`/`gather`/`scatter`。注释明确「Point-to-point send/recv ... Only single-tensor ops are supported」——PG 的 P2P 一次只搬一个张量。

shim 在构造函数末尾被挂上去：

[mooncake-pg/src/mooncake_backend.cpp:460-468](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/mooncake_backend.cpp#L460-L468) — 构造函数结尾处，创建一个指向 `this` 的 `MooncakeP2PShim`，用 `setBackend(deviceType, BackendType::CUSTOM, shim)` 注册，并 `setDefaultBackend(CUSTOM)`。注意 shim 持有的是非拥有裸指针——它由 `ProcessGroup` 的 backend map 持有，而 `MooncakeBackend` 一定比 shim 活得久，所以安全。

#### 4.1.4 代码实践

**实践目标**：在不运行多机的情况下，亲手「看见」`"mooncake"` 被注册进了 `torch.distributed`。

**操作步骤**：

1. 确认已按仓库说明编译安装 `mooncake-pg`（产出 `mooncake.pg_<torch版本>` 扩展）。
2. 在 Python 里执行（CPU 即可，无需多卡）：

```python
# 示例代码：仅用于观察后端注册，不做真正的分布式初始化
import torch.distributed as dist
import mooncake  # 触发 pg_py.cpp 的 constructor，完成注册

# Backend._plugins 是 register_backend 写入的内部表
print("mooncake" in dist.Backend._plugins)        # 预期 True（或对应 cuda 变体）
print("mooncake-cpu" in dist.Backend._plugins)    # 预期 True
```

**需要观察的现象**：导入 `mooncake` 之后，`"mooncake"` / `"mooncake-cpu"` 出现在 PyTorch 的后端表里。这验证了 pg_py.cpp 的 `__attribute__((constructor))` 确实在 import 时跑了——你**没有**调用任何显式的注册函数。

**预期结果**：两个布尔值都为 `True`。若为 `False`，说明编译产物未被正确导入（检查 `mooncake.pg_<torch_version>` 模块是否存在，见 `mooncake/pg.py` 的版本后缀逻辑）。

> 说明：`dist.Backend._plugins` 的确切属性名随 PyTorch 版本可能略有差异。如果上面访问报错，可改为 `print(dist.is_backend_available("gloo"))` 这类公开 API 做对照，或直接 `grep` 注册函数是否被调用。**待本地验证**取决于你的 torch 版本。

#### 4.1.5 小练习与答案

**练习 1**：为什么 PG 需要 `MooncakeP2PShim` 这个额外的类，而不能只用 `MooncakeBackend`？

> **参考答案**：因为 PyTorch 的点对点派发路径（`batch_isend_irecv` → `_get_backend` → `getBackend`）要求返回一个**注册过的 `c10d::Backend` 实例**，而 `MooncakeBackend` 继承自 `ProcessGroup`（不是 `Backend`），无法直接满足。shim 是一个继承 `Backend` 的薄壳，持有一个指向 `MooncakeBackend` 的非拥有指针，仅把 `send`/`recv` 等转发回去，从而让 P2P 路径能找到合法的 `Backend` 对象。

**练习 2**：用户脚本里并没有写 `register_backend(...)`，注册是怎么发生的？

> **参考答案**：注册逻辑在 `pg_py.cpp` 的 `__attribute__((constructor))` 函数里，它会在扩展模块被 `import` 时自动执行。所以只要 `import mooncake`（进而导入编译产物），`"mooncake"` / `"mooncake-cpu"` 就被注册进 `torch.distributed`。

---

### 4.2 集合通信原语与 activeRanks 掩码

#### 4.2.1 概念说明

PG 的集合通信原语（`allreduce`、`allgather`、`broadcast` 等）在实现上有一个高度统一的模式：**它们都不直接调 RDMA，而是提交一个「任务」给 `MooncakeWorker` 线程**，再由 worker 用 Transfer Engine 完成「本 rank 缓冲区 ↔ 各 peer 缓冲区」的搬运。每个原语只需提供两个 lambda：一个负责把用户张量拷进发送缓冲（`tensorToBuffer`），一个负责把接收缓冲拷回用户张量（`bufferToTensor`）。

这种设计的好处是**集合语义和数据搬运解耦**：worker 知道「要把每个 rank 的 0/1 号缓冲区两两对齐」，而 reduce / gather 的具体数学运算（求和、取最大）由 `bufferToTensor` 里的 `launchReduceKernel` / `launchReduceCpu` 完成。

本模块的真正重点是 **`activeRanks[]` 掩码**。考虑一个 4 rank 的组，其中 rank 3 崩溃了。如果 `allreduce` 仍然傻等 rank 3 的数据，它就会永远卡住。PG 的做法是：把 rank 3 在 `activeRanks[]` 里置为 `false`，于是集合操作在循环各 rank 时 `if (!meta_->activeRanks[j]) continue;` 直接跳过它——存活 rank 之间照常完成 reduce，不卡死。这就是「失效后继续通信」的底层机制。

#### 4.2.2 核心流程

一次 `allreduce` 的执行（以 GPU 为例）：

```
dist.all_reduce(tensor, op=SUM)
        │
        ▼
MooncakeBackend::allreduce(tensors, opts)
        │  tensorToBuffer: cudaMemcpyAsync(tensor -> send_buffer)
        │  bufferToTensor: cudaMemsetAsync(tensor,0) + launchReduceKernel(..., activeRanksDevice)
        ▼
worker_->putTaskCuda(ALLREDUCE, tensorSize, root=0, meta_, connection_ctx_, stream, ...)
        │  （任务入队，返回 c10d::Work）
        ▼
MooncakeWorker 线程：
   - 用 meta_->segmentIDs[] 找到每个 peer 的远程缓冲区地址
   - 通过 Transfer Engine 把各 peer 的 send_buffer 拉到本地 recv_buffer
   - 仅对 activeRanks[j]==true 的 rank 做 reduce
        │
        ▼
work->wait()  →  bufferToTensor 把结果写回 tensor
```

关键：reduce 内核里循环的次数是 `meta_->size`（容量），但**只有 `activeRanks[j]==true` 的槽位才真正参与累加**。`activeSize` 则是对外的可见 world_size。

#### 4.2.3 源码精读

先看承载所有集合语义的元数据结构：

[mooncake-pg/include/mooncake_worker.cuh:39-58](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/include/mooncake_worker.cuh#L39-L58) — `TransferGroupMeta`。注意三个关键字段的注释区分：`size` 是「capacity: number of slots allocated (incl. inactive)」，`activeSize` 是「visible group size: number of ranks that participate」。`activeRanks`（主机侧 bool 数组）与 `activeRanksDevice`（GPU 映射指针，由 `cudaHostAllocMapped` 得到）是同一个掩码的两侧——CPU 侧逻辑和 GPU 内核各用一份。`peerConnected[]` 记录哪些 peer 已握手连通。`segmentIDs[]`/`segmentInfos[]` 存每个 peer 的远程段信息。容量上限由 `kMaxNumRanks = 64` 约束（第 30 行）。

`activeRanks` 如何让 reduce 跳过失活 rank：

[mooncake-pg/src/mooncake_backend.cpp:632-651](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/mooncake_backend.cpp#L632-L651) — GPU 版 `allreduce` 的 `bufferToTensor` lambda：先 `cudaMemsetAsync` 把输出清零，再调 `launchReduceKernel(tensor, pos, realSize, src, meta_->size, opts.reduceOp, meta_->activeRanksDevice, enq_stream)`。注意 reduce 内核拿到的是 `activeRanksDevice`（GPU 指针），内核内部会按这个掩码决定哪些 rank 参与累加。CPU 版（第 620-631 行）同理，传 `meta_->activeRanks`。

`activeRanks` 对 `_allgather_base` 的保护——这就是测试里所谓的「overflow path」：

[mooncake-pg/src/mooncake_backend.cpp:725-734](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/mooncake_backend.cpp#L725-L734) — GPU 版 `_allgather_base` 的 `bufferToTensor`：循环 `for (int j = 0; j < meta_->size; ++j)`，但**第一行就是 `if (!meta_->activeRanks[j]) continue;`**。想象 max_world_size=4、当前只有 3 个 active rank：若没有这个 continue，代码会去读 slot 3（一个属于已失活 rank 的位置），写进一个按 3 元素分配的输出缓冲——这就是越界。测试 `test_allgather_reduce_scatter_recovery` 专门覆盖这个场景。

对外可见规模由 `getSize()` 决定：

[mooncake-pg/include/mooncake_backend.h:101](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/include/mooncake_backend.h#L101) — `int getSize() const override { return meta_ ? meta_->activeSize : size_; }`。所以 `dist.get_world_size()` 返回的是 `activeSize`（当前参与通信的可见规模），而不是 `size`（预留容量）。这一点对理解扩容后 world_size 的变化至关重要。

最后看一个把「连通 rank 数」转成集合操作的实用函数：

[mooncake-pg/src/mooncake_backend.cpp:1152-1168](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/mooncake_backend.cpp#L1152-L1168) — `getNumSyncedRanks()` 把「本端已连通的 peer 数 `getTotalConnectedPeers()`」做成一个张量，跑一次 **MIN** allreduce，取最小值。结果就是「所有 rank 都同意的最小连通数」——一个全局一致的「有多少 peer 真正在线」的口径。它复用了 `allreduce`，正好印证了「集合原语 = 提交任务 + 两个 lambda」的模式。

#### 4.2.4 代码实践

**实践目标**：通过阅读测试，理解 `activeRanks` 掩码如何让「3 个存活 rank + 1 个崩溃 rank」的组仍能正确跑 `_allgather_base` 而不越界。

**操作步骤**：

1. 打开 [mooncake-pg/tests/test_pg_elastic.py:514-572](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/tests/test_pg_elastic.py#L514-L572)（`_allgather_reduce_scatter_recovery_worker`）。
2. 重点读第 535 行 `_run_allgather_reduce_scatter(device, ctx.world_size, logical_rank)`（崩溃前，4 rank）与第 544 行 `_run_allgather_reduce_scatter(device, ctx.world_size - 1, logical_rank)`（崩溃后，3 rank）。
3. 对照 [mooncake-pg/tests/test_pg_elastic.py:395-435](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/tests/test_pg_elastic.py#L395-L435) 里 `_run_allgather_reduce_scatter` 的断言：输出缓冲 `output_t` 的大小是 `active_world_size`，且期望 `output_t[j] == j+1`。

**需要观察的现象**：崩溃后测试用的是 `ctx.world_size - 1 = 3` 作为 `active_world_size`，而 `max_world_size` 仍是 4。这意味着 C++ 侧循环上限 `meta_->size = 4`，但 `activeRanks[3] == false`，所以第 4 行的 `continue` 把 slot 3 跳过——输出缓冲（大小 3）不会被越界写。

**预期结果**：断言 `output_t[j] == j+1`（对 j=0,1,2）全部通过，即 3 个存活 rank 的 allgather 正确。这是 `activeRanks` 掩码存在的直接价值。**待本地验证**（需要可运行的 CPU/GPU 环境执行 `python -m unittest test_pg_elastic`）。

#### 4.2.5 小练习与答案

**练习 1**：`meta_->size`、`meta_->activeSize`、`activeRanks[]` 三者分别是什么？`dist.get_world_size()` 返回哪个？

> **参考答案**：`size` 是预留容量（含未激活槽，上限 `kMaxNumRanks=64`）；`activeSize` 是当前可见规模（真正参与通信的连续 rank 数）；`activeRanks[]` 是长度为 `size` 的逐槽掩码。`dist.get_world_size()` 走 `getSize()` → 返回 `activeSize`。

**练习 2**：如果删掉 `_allgather_base` 里 `if (!meta_->activeRanks[j]) continue;` 这一行，`test_allgather_reduce_scatter_recovery` 会怎样？

> **参考答案**：当 3 个存活 rank + 1 个崩溃 rank（max_world_size=4）时，循环会迭代到 `j=3`，去读写属于已失活 rank 的 slot 3，而输出缓冲是按 3 个 active rank 分配的——导致越界读写，结果错误甚至崩溃。这就是该测试被称为「overflow path」的原因。

---

### 4.3 失效检测：连接轮询状态机与 epoch

#### 4.3.1 概念说明

「弹性恢复」的前提是**先能发现 rank 坏了**。PG 的失效检测不是靠心跳超时这种粗粒度机制，而是靠**连接层的 per-peer 状态机** + **传输层的 per-peer epoch**：

- **连接层（`ConnectionPoller` / `ConnectionContext`）**：每个后端持有一个 `ConnectionContext`，它为每个 peer 维护一台状态机（`WAITING_STORE → WAITING_WARMUP_TRANSFER / WAITING_PEER_WARMUP → CONNECTED`）。一个全局单例 `ConnectionPoller` 跑一条线程，**串行地**轮询所有 context，驱动状态机前进。一旦某个 peer 的传输报错或被显式标记失活，状态机会把 `peerConnected[peer]` 翻成 `false`，并退回 `WAITING_STORE` 准备重连。
- **传输层（`P2PProxy`）**：点对点传输用「credit + epoch」协议。每个 peer 一个 `peer_epoch_[]` 原子计数器。一旦某个 peer 被判定为坏（比如传输超时），`resetPeerState` 让该 peer 的 epoch `+1`，于是**重启前还在路上的、带旧 epoch 的控制消息（CreditSlot / AckSlot）到达后被识别为过期而丢弃**。

这里有一个非常精妙的设计：`ConnectionPoller` 用**单线程串行**处理所有 context，所以失效信号能在所有 backend 之间一致传播——注释里详细说明了「A 检测到失败 → Global 置 false → B 观察到 Local/Global 不一致 → B 也置 false」，从而保证「一个 rank 掉线，所有 rank 都掉线」。

#### 4.3.2 核心流程

peer 连接状态机的正常握手与失效回退：

```
新 peer（或刚重连的 peer）
        │
        ▼
WAITING_STORE
   带退避地查 Store 里 server_name/buffer 键是否就绪
   就绪 → openSegment + 读对端 SegmentInfo → 转 WARMUP
        │
        ├── pollingRank <= rank_ : WAITING_WARMUP_TRANSFER（我主动写 warmup 给对端）
        └── pollingRank >  rank_ : WAITING_PEER_WARMUP（等对端写 warmup 给我）
        │
        ▼   warmup RDMA write 成功 / 收到对端 warmup
CONNECTED
   meta_->peerConnected[pollingRank] = true
   global_peerConnected_[globalRank] = true
   totalConnectedPeers_++
        │
        │  ⚠ 某处传输失败 / reportBrokenPeer
        ▼
（在 CONNECTED 分支里检测到 Local/Global 不一致）
   global/local peerConnected 都置 false
   activeRanks[pollingRank] = false          ← 让集合通信跳过它
   删除 Store 里该 peer 的三个键
   重置 warmup 区
   p2p_proxy_->resetPeerState(pollingRank)   ← epoch+1，丢弃旧消息
        │
        ▼
退回 WAITING_STORE，准备重连（等替换进程重新发布元数据）
```

epoch 如何防住过期消息——一个 credit 协议的时序：

```
正常会话 epoch=G：sender 写 CreditSlot{epoch=G, seq=...}
        │
   peer 崩溃 → resetPeerState → epoch = G+1
        │
   网线上还有 epoch=G 的旧 CreditSlot 漂过来
        │
   接收方 tryLoad() 读出 epoch=G，与本地 peer_epoch_=G+1 不等
        │
        ▼
识别为「stale」，丢弃，不消费该 credit（避免往已回收的 RecvPool 偏移写数据）
```

epoch 与 sequence 被打包进一个 64 位 `ControlToken`：

\[
\text{token} = (\text{epoch} \ll 32) \;|\; (\text{sequence} \;\&\; \text{0xFFFFFFFF})
\]

用一个原子字同时承载两者，可避免撕裂读（torn read）。`kInvalidControlToken`（全 1）表示「槽为空/未发布」。

#### 4.3.3 源码精读

**失效的「根因」入口——传输报错时谁来打响第一枪：**

[mooncake-pg/src/p2p_proxy.cpp:318-328](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/p2p_proxy.cpp#L318-L328) — `reportBrokenPeer(peer_rank)`。它在 P2P 传输超时 / 失败时被调用（见 p2p_proxy.cpp 第 1046、1239 行的调用点）：先 `resetPeerState(peer_rank)`（epoch 自增、请求重置收发 lane、唤醒 worker），再把 `meta_->peerConnected[peer_rank] = false`、`meta_->activeRanks[peer_rank] = false`、并把张量掩码同步置 0。打 `LOG(ERROR)` 标记 peer broken。

**epoch 自增 + 请求重置：**

[mooncake-pg/src/p2p_proxy.cpp:284-304](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/p2p_proxy.cpp#L284-L304) — `resetPeerState(peer_rank)`。注释解释得很到位：bump `peer_epoch_` 让「重启前还在路上的旧会话 slot」被识别为 stale 而跳过；然后置 `reset_send_req_`/`reset_recv_req_` 请求 worker 在下一个轮询周期重置该 peer 的收发 lane（`performSendReset`/`performRecvReset` 会清空 lane 并重置控制环），最后唤醒 send/recv worker。

epoch 的声明与读写（acquire/release 语义）：

[mooncake-pg/include/p2p_proxy.h:335-342](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/include/p2p_proxy.h#L335-L342) — `getEpoch` 用 `acquire`、`setEpoch` 用 `release`。`peer_epoch_` 是 `std::array<std::atomic<uint32_t>, kMaxNumRanks>`（第 586 行），per-peer 独立。

**连接状态机的「CONNECTED → 失效 → 回退」分支（全讲最关键的一段）：**

[mooncake-pg/src/connection_poller.cpp:390-473](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/connection_poller.cpp#L390-L473) — `CONNECTED` 分支。第 417-418 行判断 happy path（local 与 global 都 true）。一旦到达第 432 行以后，说明「至少有一侧报告了失败」：第 433-435 行把 `global_peerConnected_` 与 `meta_->peerConnected` 都置 false，并把 `activeRanks[pollingRank]=false`（含张量同步）——**这一步直接让集合通信跳过该 rank**（呼应 4.2）。接着第 441-452 行删除 Store 里该 peer 的三个键（server_name / buffer / extension_state），第 455-458 行清零 warmup 区，第 461 行 `p2p_proxy_->resetPeerState(pollingRank)` bump epoch，最后第 464 行退回 `WAITING_STORE` 准备重连。

同文件第 396-415 行的注释解释了为什么这套逻辑能保证「一个掉线、全部掉线」：单线程串行处理 + local/global 双视图的一致性传播。

**握手起步——WAITING_STORE 分支：**

[mooncake-pg/src/connection_poller.cpp:245-331](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/connection_poller.cpp#L245-L331) — 带退避地（`check_store_backoff_ms` 从 8ms 翻倍到 1024ms）查 Store 里 `server_name_<idx>_<rank>` 与 `buffer_<idx>_<rank>` 是否就绪；就绪则 `openSegment`、读对端 `SegmentInfo`，再按 `pollingRank` 与 `rank_` 的大小关系决定「我主动发 warmup」还是「等对端发 warmup」。MNNVL 集群（`skip_warmup_`）跳过 warmup，因为 fabric 已保证连通且 CPU 堆缓冲对跨节点 NVLink 写不可见。

**单线程轮询主循环：**

[mooncake-pg/src/connection_poller.cpp:565-628](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/connection_poller.cpp#L565-L628) — `pollerLoop()`。它维护一份 `local_contexts`（带版本号的线程局部缓存，避免频繁加锁）和一份 `zombie_contexts`（已移除但仍在收尾的 context）。每轮：重新加载 context 列表（若版本号变了）、对每个 context 调 `ctx->poll()`（驱动各 peer 状态机）、回收 zombie、若无事可做则按 `kConnectingIdleSleepMs=50` / `kAllConnectedIdleSleepMs=200` 休眠等待唤醒。单线程串行是失效一致性的根基。

退避等待器（被 drain、握手、P2P wait 复用）：

[mooncake-pg/include/pg_utils.h:119-130](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/include/pg_utils.h#L119-L130) — `BackoffWaiter::step()`：先忙等 `PAUSE`（`spin_limit` 次）→ `std::this_thread::yield()`（`yield_limit` 次）→ 指数 `sleep_for`（翻倍直到 `max_sleep`）。`wait_for(timeout, pred)` 在超时与谓词之间反复 `step()`。

#### 4.3.4 代码实践

**实践目标**：用一个**只读源码**的方式，完整追踪「rank 1 崩溃 → 存活 rank 不卡死」的失效检测链路。

**操作步骤**：

1. 读测试 [mooncake-pg/tests/test_pg_elastic.py:575-600](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/tests/test_pg_elastic.py#L575-L600)（`_fault_detection_worker`）：rank 1（`BROKEN_RANK`）在第一次 all_reduce 后 `os._exit(0)`；存活 rank `broken_exited.wait()` 后**再做一次 all_reduce**，断言「不应卡死」。
2. 追踪存活 rank 为何不卡死：
   - 当 rank 1 进程退出，它的 TCP/RDMA 连接断开，存活 rank 与 rank 1 的下一次传输会失败 → `P2PProxy` 调 `reportBrokenPeer(1)`（[p2p_proxy.cpp:318](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/p2p_proxy.cpp#L318)）。
   - 这把 `meta_->activeRanks[1] = false`（同函数第 322 行）。
   - 下一次 all_reduce 的 reduce 内核看到 `activeRanks[1]==false`，跳过 rank 1（[mooncake_backend.cpp:632-651](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/mooncake_backend.cpp#L632-L651)），于是不等 rank 1，正常完成。

**需要观察的现象**：测试的断言（`test_failed_rank`，第 695-712 行）只要求「存活 rank 数 == world_size - 1」并完成——它**不要求** rank 1 复活。这正是「失效检测」阶段：先证明能继续跑，下一节才讲如何把它换回来。

**预期结果**：3 个 survivor 各自记录 `role=survivor`，至少 1 个 broken 记录。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`resetPeerState` 为什么要 `peer_epoch_++`？不加会怎样？

> **参考答案**：为了让重启前「还在路上」的、带旧 epoch 的控制 slot（CreditSlot/AckSlot）在到达后被识别为 stale 而丢弃。若不加 epoch，旧会话的 credit 可能指引发送方把数据写到「已被回收/重分配给新会话」的 RecvPool 偏移，造成数据错乱。

**练习 2**：为什么失效信号能保证「一个 rank 掉线，所有 rank 都掉线」？

> **参考答案**：因为 `ConnectionPoller` 是单线程、串行处理所有 `ConnectionContext`。当 backend A 在 `CONNECTED` 分支把 `global_peerConnected_[r]` 置 false 后，随后被处理的 backend B 会观察到「local=true, global=false」的不一致，于是也把自己的 local 置 false。单线程串行保证了这个传播没有竞态，且失效信号在 initiator 被再次处理前就已传播给所有其他 context。

---

### 4.4 弹性恢复：两阶段协议（peer-state 轮询 + recover_ranks）

#### 4.4.1 概念说明

失效检测让存活 rank 能「带伤继续」，但真正的弹性恢复是**把坏掉的 rank 换成一个新进程，让它重新加入并恢复全规模通信**，而整个服务不重启。PG 把这件事拆成清晰的两阶段协议：

> **阶段一：传输就绪（transport readiness）。** 新进程（替换 rank）以 `is_extension=True` 模式启动，调用 `join_group()`：发布自己的元数据到 Store、与所有存活 peer 完成 warmup 握手。**此时它已经「物理连通」，但逻辑上仍未参与集合通信**（`activeRanks` 里它还是 false，处于「local-only」状态）。
>
> **阶段二：逻辑激活（logical activation）。** 存活 rank 用 `get_peer_state([新rank])` **轮询**，确认大家都观察到新 rank 已连通；然后调用 `recover_ranks([新rank])` 把它在 `activeRanks` 里置 true、扩大 `activeSize`，并把一份 `ExtensionState`（含 `activeRanks` 掩码、各 peer 的 `p2pEpochs`、`taskCount`）写到 Store，供新 rank 读取对齐。

为什么分两阶段？因为**「物理连通」和「逻辑成员资格」是两件事**。如果新 rank 一连通就立刻参与 all_reduce，而此刻存活 rank 的 `activeRanks` 还没更新，就会出现「有人把它算进去、有人没算」的不一致，导致 reduce 结果错误甚至死锁。两阶段协议用一次 **MIN allreduce 达成共识**（`get_peer_state`），保证「所有存活 rank 都同意它连上了」之后，才统一激活。

**`maxWorldSize`（预留容量）的角色**：它让这一切**不需要动态扩容元数据**。启动时存活 rank 用 `world_size=N, max_world_size=M`（M 是最终可能达到的规模），于是 `local2global_rank_map_`、`activeRanks[]`、`activeRanksTensor` 一开始就按 M 预留，未用的槽位初始化为 inactive。替换进程占用**同一个逻辑 rank 编号**（比如坏掉的 rank 3，新进程仍是 rank 3），所以无需任何元数据重排——只要把它从 inactive 翻成 active。`ConnectionPoller` 还会通过 `setPollingLimitTo(M)` **提前轮询预留槽**，这样存活 rank 不必调用 `extendGroupSizeTo` 就能「观察到」joiner 上线。这就是 maxWorldSize 的预留作用。

#### 4.4.2 核心流程

替换一个崩溃 rank（比如 rank 3）的完整时序，参与者：存活 rank 0/1/2、替换进程（占逻辑 rank 3）：

```
[前置] 所有 rank 启动时 world_size=4, max_world_size=4；
       local2global_rank_map / activeRanks / activeRanksTensor 均按 4 预留
       ConnectionPoller 的 pollingLimit_=4（已覆盖预留槽）

rank 3 崩溃（os._exit）→ 4.3 节的失效检测把它 activeRanks[3]=false、退回 WAITING_STORE

─── 阶段一：传输就绪（替换进程主导） ───
替换进程启动：init_process_group(rank=3, world_size=4, is_extension=True, max_world_size=4)
   ├ isExtension → 构造时 setLocalOnlyActiveRanks()：只有 rank 3 自己 active（local-only）
   ├ ConnectionContext 以 isDummy_=true 构造（暂不参与轮询）
   └ replace 进程调用 join_group():
        ├ setDummy(false)
        ├ publishLocalPeerMetadata()：写 server_name_3 / buffer_3 到 Store
        ├ ConnectionPoller::registerContext(connection_ctx_)
        ├ waitUntilAllConnected()：与 0/1/2 完成 warmup 握手
        └ waitForExtensionState()：阻塞，等存活 rank 写 extension_state_3 键

─── 阶段一被存活 rank 观察到 ───
存活 rank 的 ConnectionPoller 在 CONNECTED 分支发现 rank 3 重新连通：
   peerConnected[3]=true, global_peerConnected_[3]=true

─── 阶段二：逻辑激活（存活 rank 主导） ───
存活 rank 轮询：
   get_peer_state([3])   → 读 local peerConnected[3]，跑 MIN allreduce 达成共识
                          （若有 rank 的 activeRanks 在 allreduce 期间变化则重试）
   返回 true ⟹ 全体同意 rank 3 已连通
存活 rank 调用：
   recover_ranks([3])    → activeRanks[3]=true；activeSize 扩到 4；
                           序列化 ExtensionState{activeRanks, p2pEpochs, taskCount}
                           写入 Store 的 extension_state_3 键

─── 阶段一收尾 ───
替换进程的 waitForExtensionState() 读到 extension_state_3：
   对齐 activeRanks / p2pEpochs / taskCount → 退出阻塞

─── 全员恢复 ───
此时 activeRanks=[1,1,1,1]，world_size(=activeSize)=4，
下一次 all_reduce 全员参与，恢复正常。
```

注意 `get_peer_state` 和 `recover_ranks` 都是**只在存活 rank 之间**调用的集合操作（替换进程此刻还是 local-only，不参与）。

#### 4.4.3 源码精读

**先看 maxWorldSize 在构造时的预留：**

[mooncake-pg/src/mooncake_backend.cpp:197-204](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/mooncake_backend.cpp#L197-L204) — 从 options 取 `max_size`（默认等于 size），并 `TORCH_CHECK(max_size >= size)`。第 245-248 行把 `[size, max_size)` 区间的 `local2global_rank_map_` 也填好（预留槽）。

[mooncake-pg/src/mooncake_backend.cpp:367-369](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/mooncake_backend.cpp#L367-L369) — 若 `max_size != size`，调 `connection_ctx_->setPollingLimitTo(max_size)`，让轮询器提前覆盖预留槽，从而能观察到 joiner。

[mooncake-pg/src/mooncake_backend.cpp:390-401](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/mooncake_backend.cpp#L390-L401) — 注释说明：`meta_->size` **故意初始化为 max_world_size**，这样存活 rank 之后能用 `recoverRanks()` 激活 joiner 而无需 `extendGroupSizeTo`；未激活槽由 `activeRanks`/`activeRanksTensor` 掩码遮蔽。`activeSize` 初始化为实际成员数 size。

[mooncake-pg/src/mooncake_backend.cpp:411-418](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/mooncake_backend.cpp#L411-L418) — 把 `[size, max_size)` 的 `activeRanks[i]` 初始化为 false（预留为 inactive）。

`setPollingLimitTo` 的语义（轮询范围可大于 groupSize）：

[mooncake-pg/src/connection_poller.cpp:132-140](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/connection_poller.cpp#L132-L140) — 校验 `pollingLimit >= groupSize` 后写入 `pollingLimit_`。注释（见头文件第 66-68 行）说：它可比 `groupSize_` 大，从而让已有 rank 不调 `extendGroupSizeTo()` 也能观察到 joiner。

**阶段一：替换进程的 `joinGroup()`：**

[mooncake-pg/src/mooncake_backend.cpp:1292-1303](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/mooncake_backend.cpp#L1292-L1303) — `joinGroup()`：先 `TORCH_CHECK` 仅 extension backend 可用；`setDummy(false)` 脱离哑状态；`publishLocalPeerMetadata()` 把 server_name/buffer 写进 Store；若尚未注册则 `ConnectionPoller::registerContext`；`waitUntilAllConnected()` 等 warmup 握手完成；最后 `waitForExtensionState()` **阻塞**，直到存活 rank 写来 extension_state 键。

[mooncake-pg/src/mooncake_backend.cpp:1110-1150](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/mooncake_backend.cpp#L1110-L1150) — `waitForExtensionState()`：用 `BackoffWaiter::constantSleep(50ms)` 轮询 Store 里 `extension_state_<idx>_<rank>` 键是否出现；出现后反序列化 `ExtensionState`，把 `taskCount`、各 peer 的 `p2pEpochs`（通过 `p2p_proxy_->setEpoch` 设进去——这样 joiner 与存活方对齐 epoch，避免旧消息误判）、`activeRanks` 全部对齐，并据 activeRanks 重算 `activeSize`。这是 joiner「拿到全局一致视图」的时刻。

ExtensionState 的序列化（自定义紧凑二进制：rank 数 + bitmap + p2pEpochs + taskCount）：

[mooncake-pg/src/mooncake_backend.cpp:49-88](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/mooncake_backend.cpp#L49-L88) — `serialize`：用 bitmap（每 rank 1 bit，向上取整到字节）紧凑存 `activeRanks`，再依次存每个 peer的 `p2pEpochs`（uint32）和 `taskCount`。`deserialize`（第 90-130 行）做严格长度校验后还原。

**阶段二：存活 rank 的 `getPeerState()`（共识轮询）：**

[mooncake-pg/src/mooncake_backend.cpp:1211-1254](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/mooncake_backend.cpp#L1211-L1254) — `getPeerState(ranks)`：外层 `while(true)`。每轮把本地 `peerConnected[rank]` 打包成张量，**备份当前 `activeRanks`**，跑一次 **MIN allreduce**；然后检查 allreduce 前后 `activeRanks` 是否变化——**若变化说明恢复过程中有 rank 状态在变，需重试**（保证一致性）；无变化则把 allreduce 结果（最小值非 0 即 true）作为该 peer 是否全体一致「已连通」返回。这正是「阶段一被观察到」的探测点。

**阶段二：存活 rank 的 `recoverRanks()`（激活）：**

[mooncake-pg/src/mooncake_backend.cpp:1256-1290](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/mooncake_backend.cpp#L1256-L1290) — `recoverRanks(ranks)`：对每个 rank `TORCH_CHECK(peerConnected[rank])`（必须先物理连通才允许激活），置 `activeRanks[rank]=true`；若某 rank 超出当前 `activeSize` 边界则扩大之；`syncActiveRanksTensor` 同步张量掩码；收集当前各 peer 的 `p2pEpochs`，构造 `ExtensionState`，序列化后**为每个被恢复的 rank 写一份** `extension_state_<idx>_<rank>` 到 Store——这正是 joiner `waitForExtensionState` 在等的东西。

**Python 侧的入口绑定：**

[mooncake-pg/src/pg_py.cpp:82-93](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/pg_py.cpp#L82-L93) — `recoverRanks` / `joinGroup` 的 Python 包装：把 `c10d::ProcessGroup` 智能指针 `static_intrusive_pointer_cast` 成 `MooncakeBackend` 再调用。`getPeerState`（第 75-80 行）、`extendGroupSizeTo`（第 68-73 行）同理。

最后看一个真实使用这套协议的测试：

[mooncake-pg/tests/test_pg_elastic.py:603-677](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/tests/test_pg_elastic.py#L603-L677) — `_replacement_recovery_worker`：坏 rank（`BROKEN_RANK=1`）`os._exit(0)`；存活 rank `wait_until(lambda: pg.get_peer_state(backend, [BROKEN_RANK])[0])` 轮询直到替换进程连通，再 `pg.recover_ranks(backend, [BROKEN_RANK])`；替换进程（`proc_rank == world_size`）以 `is_extension=True` 启动、`pg.join_group(backend)` 完成。注释（第 638-651 行）把「先 get_peer_state 确认连通、再 recover_ranks 激活」的两阶段顺序写得很明确。

#### 4.4.4 代码实践

**实践目标**：描述一个 rank 崩溃后被替换进程重新加入的全过程，把 `join_group` / `get_peer_state` / `recover_ranks` / `maxWorldSize` 四件事串起来。

**操作步骤**（源码阅读 + 调用链跟踪）：

1. 打开 [mooncake-pg/tests/test_pg_elastic.py:603-677](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/tests/test_pg_elastic.py#L603-L677)，按「broken 退出 → survivor 轮询 → replacement join」三段读。
2. 在源码里画出这条调用链，并标注每一步落在哪个文件：
   - 替换进程：`pg.join_group(backend)` → [mooncake_backend.cpp:1292](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/mooncake_backend.cpp#L1292) `joinGroup()` → `publishLocalPeerMetadata` + `registerContext` + `waitUntilAllConnected` + `waitForExtensionState`。
   - 存活 rank：`pg.get_peer_state(backend, [1])` → [mooncake_backend.cpp:1211](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/mooncake_backend.cpp#L1211)（MIN allreduce 共识）。
   - 存活 rank：`pg.recover_ranks(backend, [1])` → [mooncake_backend.cpp:1256](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/mooncake_backend.cpp#L1256)（置 activeRanks[1]=true + 写 extension_state_1）。
3. 回答：为什么替换进程要占用**同一个逻辑 rank 编号（1）**，而不是用新编号？参考 `maxWorldSize` 的预留作用（4.4.3 前几条源码）。

**需要观察的现象**：调用链呈现清晰的「替换进程先就绪（join_group 阻塞在 waitForExtensionState）→ 存活方共识（get_peer_state 返回 true）→ 存活方激活（recover_ranks 写 extension_state）→ 替换进程解除阻塞」的因果关系。`maxWorldSize` 使全程无需重排 `activeRanks`/`local2global_rank_map_`。

**预期结果**：测试 `test_recovery`（第 714-737 行）断言 survivor 数 == world_size - 1、replacement 数 == 1、broken 记录 ≥ 1；最终全 4 rank 的 all_reduce 正常完成。**待本地验证**（需可跑 `python -m unittest test_pg_elastic.TestMooncakePGElasticCPU` 的环境）。

#### 4.4.5 小练习与答案

**练习 1**：把两阶段顺序反过来——存活 rank 先 `recover_ranks` 再 `get_peer_state`，会发生什么？

> **参考答案**：会出错。`recoverRanks` 第一行就 `TORCH_CHECK(meta_->peerConnected[rank])`，要求该 rank **已经物理连通**。若还没握手成功就调 `recover_ranks`，check 会失败抛异常。更本质地，逻辑激活必须在传输就绪之后，否则会出现「逻辑成员已激活但物理上还没连上」的死锁/错误。所以必须先 `get_peer_state` 确认连通（阶段一完成），再 `recover_ranks` 激活（阶段二）。

**练习 2**：`waitForExtensionState` 里为什么要通过 `ExtensionState` 把 `p2pEpochs` 传给 joiner？

> **参考答案**：为了让 joiner 与存活方的 per-peer epoch 对齐。存活方在历史故障中可能已经多次 `resetPeerState` 把某些 peer 的 epoch 推到了较大值；joiner 新启动时 epoch 从 0 开始。若不对齐，joiner 可能误把存活方发出的「当前 epoch」credit 当成 stale 丢弃，或反之。`ExtensionState` 把当前的 `p2pEpochs` 快照传给 joiner，joiner 用 `setEpoch` 设进去，双方站在同一基线上。

**练习 3**：`maxWorldSize` 的「预留」具体预留了哪些数据结构？不设它会怎样？

> **参考答案**：预留了 `local2global_rank_map_`（`[size, max_size)` 段）、`meta_->size`（设为 max_size）、`activeRanks[]` / `activeRanksTensor`（按 max_size 分配，预留槽初始化为 inactive），以及 `ConnectionPoller` 的 `pollingLimit_`（设为 max_size 以提前轮询预留槽）。不设（即 max_size == size）时，要替换/扩容就必须先调 `extendGroupSizeTo` 动态扩大这些结构（见 [mooncake_backend.cpp:1170-1209](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/src/mooncake_backend.cpp#L1170-L1209)），且存活 rank 无法在 joiner 上线前就「观察」到它，两阶段协议会更繁琐。

---

## 5. 综合实践

**任务**：在不运行多机的前提下，写一份「PG 弹性恢复时序图」文档，把本讲四个模块的知识串成一条完整故事线，并标注每一步对应的源码位置。

**具体要求**：

1. **场景设定**：4 个 rank（0/1/2/3），`max_world_size=4`，rank 3 在某次 all_reduce 后崩溃，由一个新进程替换并恢复。
2. **画出六个阶段**，每个阶段写明「谁在做、调了什么函数/状态、改了哪个数据结构、对应源码行」：
   - (a) 启动预留：4 rank 初始化，`activeRanks=[1,1,1,1]`，`size=activeSize=4`，`pollingLimit=4`。
   - (b) 正常通信：一次 all_reduce 全员参与（4.2）。
   - (c) 失效检测：rank 3 崩溃 → `reportBrokenPeer` → `activeRanks[3]=false`、`peer_epoch_[3]++`、连接状态机退回 `WAITING_STORE`（4.3）。
   - (d) 带伤继续：存活 0/1/2 跑 all_reduce，reduce 内核跳过 `activeRanks[3]==false`（4.2/4.3）。
   - (e) 阶段一·传输就绪：替换进程 `join_group` → 发布元数据 + warmup + `waitForExtensionState` 阻塞（4.4）。
   - (f) 阶段二·逻辑激活：存活方 `get_peer_state([3])` 共识 → `recover_ranks([3])` 写 extension_state → 替换进程解除阻塞 → 全员 all_reduce 恢复（4.4）。
3. **回答三个反思题**（写在文档末尾）：
   - 为什么阶段二必须用一次 allreduce 达成共识，而不是各 rank 各自判断？
   - 若把 `max_world_size` 设成 3（等于初始 size），阶段一/二哪里会出问题？
   - epoch 机制在哪两个阶段之间起到了「安全垫」作用？

**评判标准**：时序图能自洽地解释「不重启整个服务、只换一个 rank」如何实现；每个箭头都能在源码里找到出处；反思题答案用到 `activeRanks`、`pollingLimit`、`ExtensionState`、`peer_epoch_` 等本讲概念。

> 这是一个**源码阅读型综合实践**，不需要运行环境。完成后你应当能用一张图向同事讲清 Mooncake PG 的弹性恢复全貌。

## 6. 本讲小结

- PG 通过 `pg_py.cpp` 的 `__attribute__((constructor))` 在 `import` 时把 `"mooncake"`/`"mooncake-cpu"` 注册成 `torch.distributed` 后端；`MooncakeBackend`（继承 `ProcessGroup`）承载集合通信，`MooncakeP2PShim`（继承 `Backend`）只为满足 P2P 派发而存在，二者是「两个类」的来源。
- 集合原语统一走 `MooncakeWorker::putTask*` + 两个 lambda（`tensorToBuffer`/`bufferToTensor`）；`activeRanks[]` 掩码让 reduce / allgather 在 `for j<size` 循环里 `continue` 跳过失活 rank，`getSize()` 返回的是 `activeSize`（可见规模），而非 `size`（预留容量）。
- 失效检测分两层：连接层的 per-peer 状态机（`WAITING_STORE → WARMUP → CONNECTED`，单线程 `ConnectionPoller` 串行处理保证失效信号全传播）+ 传输层的 per-peer epoch（`resetPeerState` 自增，让重启前的 stale 控制槽被丢弃）。`reportBrokenPeer` 是失效的第一枪。
- 弹性恢复是两阶段协议：**阶段一**替换进程 `join_group` 完成传输就绪并阻塞在 `waitForExtensionState`；**阶段二**存活 rank 用 `get_peer_state`（MIN allreduce 共识）确认连通，再用 `recover_ranks` 激活（置 `activeRanks` + 写 `ExtensionState`，含 `p2pEpochs`/`taskCount`）。
- `maxWorldSize` 的预留作用：让 `local2global_rank_map_`/`activeRanks`/`activeRanksTensor`/`pollingLimit_` 一开始就按峰值规模分配，替换进程占用**同一逻辑 rank 编号**，全程无需动态扩容元数据，存活 rank 也能提前「观察」到 joiner。

## 7. 下一步学习建议

- **跑一遍弹性测试**：在具备 CPU 或多卡 GPU 的环境执行 `python -m unittest mooncake-pg/tests/test_pg_elastic.py -v`，重点观察 `_replacement_recovery_worker` 与 `_extension_worker` 的日志输出，把本讲的时序图与真实日志对照。
- **横向对比 Mooncake EP 的容错**：回到 `u8-l1`，对比 EP 的 `active_ranks`（推理侧、超时绕过）与 PG 的 `activeRanks`（训练/通信侧、两阶段恢复）的异同——两者共享「用掩码跳过坏 rank」的思想，但 EP 是无状态绕过，PG 是有状态恢复。
- **深入 P2P 协议**：本讲只点到 `P2PProxy` 的 epoch 与 credit。建议通读 [mooncake-pg/include/p2p_proxy.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/include/p2p_proxy.h) 顶部那张完整的「receiver 驱动 credit 拉取协议」注释图，以及 `CreditSlot`/`AckSlot` 的 header-footer 双 token 一致性布局，理解 PG 点对点传输的高性能与可靠性如何兼得。
- **阅读 benchmark**：[mooncake-pg/benchmark/pgbench.py](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-pg/benchmark/pgbench.py) 展示了 PG 各集合原语的压测用法，可用来验证你对 `getSize()`/`activeRanks` 行为的理解。
- **关注子组（subgroup）扩展**：`test_extension_with_subgroups` 演示了在多个不相交子组上并行做弹性扩展（`new_group` + 每组独立的 `MooncakeBackendOptions`），这是把弹性恢复用于真实 MoE/多通信域场景的进阶用法，值得作为下一个研究课题。
