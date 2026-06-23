# RL 训练数据流：权重与隐状态传输

## 1. 本讲目标

本讲是「集成篇」的进阶篇。在前面的讲义里，我们已经分别学会了「怎么用 `MooncakeDistributedStore` 读写张量」（[u5-l4](u5-l4-python-store-api.md)）和「怎么让 PyTorch 直接从 Mooncake 存储池分配显存」（[u9-l2](u9-l2-pytorch-allocator.md)）。本讲要把这两条线拧到一起，落到一个真实工程问题里：**分布式强化学习（RL）训练中，rollout（采样/推理）阶段与 training（训练）阶段之间，如何用 Mooncake Store 解耦地传递数据**。

学完本讲你应该能够：

1. 说清 RL 场景下「rollout ↔ training」的数据流为什么需要解耦，以及 Mooncake Store 在其中扮演的「共享黑板」角色。
2. 读懂 `mooncake-rl/examples/rl_samples.py` 这个示例，区分其中**真正经过 Store 的数据流**（rollout 样本的 `put_tensor` / `get_tensor`）与**被 mock 掉的部分**（权重同步、检查点）。
3. 掌握「训练侧客户端」与「rollout 侧客户端」分别 `setup()` 连到同一个 metadata/master、但用不同 hostname 和 RDMA 网卡的部署模式。
4. 理解张量在 Store 中的在线格式 `[TensorMetadata][data]`，以及 `put_tensor` / `get_tensor` 在 pybind11 层如何释放 GIL。
5. 说清 `async_store.py` 的异步包装机制（`run_in_executor` + `asyncio.gather`）能在 RL 数据流的哪些环节带来并发收益。

> **依赖前置**：本讲默认你已经读过 [u5-l4 Python Store API](u5-l4-python-store-api.md)，知道 `MooncakeDistributedStore`、`setup()`、`put_tensor`/`get_tensor`、`ReplicateConfig`、GIL 管理这些概念。如果对控制面/数据面还陌生，先看 [u5-l1 Store 架构](u5-l1-store-architecture.md)。

## 2. 前置知识

本讲默认你已经具备：

- **Python Store API（依赖 u5-l4）**：知道 `from mooncake.store import MooncakeDistributedStore`、`setup(local_hostname, metadata_server, global_segment_size, local_buffer_size, protocol, rdma_devices, master_server_addr, ...)` 这一套生命周期。
- **PyTorch 基础**：`torch.nn.Linear`、`state_dict`、`optimizer.step()`、张量的 `shape/dtype/contiguous`。
- **RL 基本直觉（非必需）**：知道一轮 RL 训练通常是「用当前策略采样（rollout）→ 拿样本更新策略（train）→ 把新策略同步回推理引擎」的循环。本讲的示例就是对这个循环的极简 mock。

### 一个关键直觉：RL 训练里的「rollout 阶段」和「train 阶段」是天然解耦的

读这一讲最容易卡住的是「为什么要用一个分布式 Store 来传 RL 数据，而不是直接函数调用」。先建立这个直觉：

在单机、单进程的玩具 RL 里，rollout 和 train 就是同一个进程里的两个函数，数据用 Python 对象直接传。但真实的分布式 RL（本讲示例改编自 [THUDM/slime](https://github.com/THUDM/slime)）长这样：

| | rollout 阶段 | train 阶段 |
|---|---|---|
| 谁在跑 | 多个**推理引擎**（rollout engine），用当前权重做采样/生成 | 多个**训练 worker**（training actor），用样本算梯度更新权重 |
| 硬件偏好 | 偏好**吞吐**，常部署在推理框架（如 SGLang）上 | 偏好**显存带宽/互联**，部署在训练框架（如 Megatron/DeepSpeed）上 |
| 产出物 | 大量样本 / 隐状态 / KV-cache / logits | 更新后的权重 |

由于两侧**部署在不同进程、不同机器、甚至不同集群**，样本和权重都不能再走进程内调用，必须有一个**双方都能访问的共享介质**。Mooncake Store 就是这个介质——它本质是一块「跨机器、用 RDMA 直传、按 key 索引」的分布式内存，rollout 把样本 `put_tensor` 进去，train 用同一个 key `get_tensor` 取出来。

一句话：**Store 在 RL 里是 rollout 与 training 之间的「共享黑板」，用 key 解耦生产者和消费者**。本讲的所有细节都是这句话的展开。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 | 本讲怎么用 |
|---|---|---|
| [mooncake-rl/examples/rl_samples.py](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-rl/examples/rl_samples.py) | Mooncake 在 RL 场景的**示例脚本**，极简 mock 了 rollout manager + train group 的循环 | 本讲的主战场：数据流的全部「业务侧」代码都在这里 |
| [mooncake-integration/store/store_py.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp) | `MooncakeDistributedStore` 的 pybind11 绑定实现 | 解释 `setup`/`put_tensor`/`get_tensor` 在 C++ 层到底干了什么、张量在线格式、GIL 管理 |
| [mooncake-integration/store/async_store.py](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/async_store.py) | 异步 Store 包装器 `MooncakeDistributedStoreAsync` | 讲清「把同步阻塞调用扔进线程池」的机制，以及它在 RL 数据流里的并发优化点 |
| [scripts/test_async_store.py](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/test_async_store.py) | 异步 Store 的功能测试，大量使用 `asyncio.gather` | 作为「异步并发拉取 TP 分片」的真实可运行范例 |

调用链记忆：**`rl_samples.py`（业务）→ `MooncakeDistributedStore`（Python 入口）→ `MooncakeStorePyWrapper`（GIL 薄封装，store_py.cpp）→ `RealClient`（控制面 + 数据面）→ Master + Transfer Engine**。本讲主要停在「业务 → Python 入口 → Wrapper」这一层，底层 RealClient 的细节在 [u5-l3](u5-l3-real-client.md)。

## 4. 核心概念与源码讲解

### 4.1 RL 数据流全景：rollout ↔ training 的解耦

#### 4.1.1 概念说明

先看示例文件第一行的注释，它点明了整个示例的存在意义：

```python
# This is a dummy RL training example for demonstrating the usage of Mooncake Store
# in transmission of data between rollout engines and training engines when distributed
```

—— [mooncake-rl/examples/rl_samples.py:1-2](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-rl/examples/rl_samples.py#L1-L2)：明确说这是「演示在 rollout engine 与 training engine 之间传数据的 dummy 示例」。

这个示例把一轮 RL 训练拆成了**两组角色**：

- **`RolloutManage` / `RolloutEngine`**：扮演 rollout 侧。它负责「生成样本」（`generate`），把样本写进 Store；也负责「评估」（`eval`），从 Store 读回样本算分。
- **`TrainGroup` / `TrainActor`**：扮演 training 侧。它负责「从 Store 取样本训练」（`train`），并（在真实场景里）把更新后的权重再分发回 rollout 侧。

这两组角色**各自创建独立的 Store 客户端**，通过**同一个 metadata/master** 看到同一份共享数据。下图是示例 `train()` 主循环里真实发生的数据流（带 ★ 的是真正经过 Store 的环节，其余是 mock）：

#### 4.1.2 核心流程

一轮 rollout（主循环里 `for rollout_id in range(...)`）的时序：

```
            Rollout 侧                              Training 侧
            ──────────                              ───────────
RolloutManage.generate(rollout_id)
  └─ 每个 engine 产出 sample {obs, action, reward}
  └─ ★ rollout_client.put_tensor(key=str(id), samples)   ──┐
                                                            │
                          Mooncake Store（共享黑板）  ◀──── ┘
                          key="3" -> [TensorMetadata][data]
                                                            │
                                          TrainGroup.train(id, key) ◀──── ┘
                                            └─ ★ training_client.get_tensor(key)
                                            └─ 每个 actor 做一次前向/反向（算 dummy MSE loss）
  ┌─ actor_model.update_weights()    [mock：仅打印，真实场景应把新权重 put 回 Store]
  │
RolloutManage.eval(rollout_id)
  └─ ★ rollout_client.get_tensor(key)   读回样本算分
```

几个要点：

1. **key 就是 `str(rollout_id)`**：rollout 侧用这个 key 写、training 侧用同一个 key 读——Store 的「按 key 索引」天然解耦了生产者和消费者，双方不需要互相持有引用。
2. **真正过 Store 的只有样本数据**（`put_tensor` / `get_tensor`）。权重同步（`update_weights`）和检查点（`save_model`）在这个 dummy 示例里**没有走 Store**，本讲 4.4 节会专门讲清「哪些是 mock、真实方案应该长什么样」。
3. **两侧行为不对称**：rollout 侧既写（`generate`）又读（`eval`）；training 侧只读（`train`）。这符合 RL 的直觉——样本是 rollout 产出的，权重是 training 产出的。

#### 4.1.3 源码精读

主循环在 `train()` 函数里，它把上面那张图按顺序串起来：

```python
# create training engine group
actor_model = create_actor_group(args)
# create the rollout manager, with engines inside.
rollout_manager = create_rollout_manager(args)
...
# train loop.
for rollout_id in range(args.start_rollout_id, args.num_rollout):
    rollout_data_ref = rollout_manager.generate(rollout_id)   # ① rollout 侧 put_tensor
    actor_model.train(rollout_id, rollout_data_ref)           # ② training 侧 get_tensor + 训练
    ...
    actor_model.update_weights()                               # ③ 权重同步（mock）
    ...
    rollout_manager.eval(rollout_id)                           # ④ eval 读回样本
```

—— [mooncake-rl/examples/rl_samples.py:335-388](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-rl/examples/rl_samples.py#L335-L388)：`train()` 是整个示例的「导演」，把 `generate → train → update_weights → eval` 编排成一个循环。注意 `generate` 返回的就是 key 字符串 `rollout_data_ref`，training 侧靠这个 ref 去 Store 取数据。

工厂函数 `create_actor_group` / `create_rollout_manager` 只是薄封装：

—— [mooncake-rl/examples/rl_samples.py:322-333](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-rl/examples/rl_samples.py#L322-L333)：分别返回 `TrainGroup(args)` 和 `RolloutManage(args)`，这两个构造函数里会各自 `setup()` 一个 Store 客户端（见 4.2 节）。

#### 4.1.4 代码实践

**实践目标**：不启动任何 Store 服务，纯靠「源码阅读」画出 4.1.2 那张数据流图，并标注哪些环节真正经过 Store。

**操作步骤**：

1. 打开 [rl_samples.py 的 `train()` 函数](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-rl/examples/rl_samples.py#L335-L388)。
2. 对循环体里 4 个调用（`generate` / `train` / `update_weights` / `eval`）逐一「点进去」看实现：
   - `generate` → [第 291-309 行](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-rl/examples/rl_samples.py#L291-L309)，找到 `put_tensor`。
   - `train` → [第 132-154 行](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-rl/examples/rl_samples.py#L132-L154)，找到 `get_tensor`。
   - `update_weights` → [第 125-130 行](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-rl/examples/rl_samples.py#L125-L130)，注意它只有一行 `print`。
   - `eval` → [第 311-320 行](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-rl/examples/rl_samples.py#L311-L320)，找到 `get_tensor`。
3. 在纸上（或文本里）画一张四列表：`调用 | 主体 | 是否调 Store 方法 | 具体 Store 方法`。

**需要观察的现象**：你会清楚地看到，只有 `generate`（put）和 `train`/`eval`（get）真正碰 Store；`update_weights` 完全是空壳。

**预期结果**：

| 调用 | 主体 | 经过 Store？ | Store 方法 |
|---|---|---|---|
| `generate` | RolloutManage | ✅ | `put_tensor` |
| `train` | TrainGroup | ✅ | `get_tensor` |
| `update_weights` | TrainGroup | ❌（mock） | 无 |
| `eval` | RolloutManage | ✅ | `get_tensor` |

> 这个练习纯阅读，不需要运行，因此「无待本地验证项」。

#### 4.1.5 小练习与答案

**练习 1**：示例里 rollout 侧和 training 侧是通过什么「约定」找到同一份数据的？

**参考答案**：通过 **key**。rollout 侧 `generate` 把样本存到 `key = str(rollout_id)`，training 侧 `train` 拿到的 `rollout_data_ref` 就是这个 key 字符串，再用它 `get_tensor`。Store 的「按 key 索引」是这个解耦的关键，双方不需要互相传对象引用。

**练习 2**：示例把 `update_weights` 留成了空壳。如果要让权重同步也走 Store，从「谁写、谁读、用什么 key」的角度，你会怎么设计？

**参考答案**：training 侧是权重的**生产者**，rollout 侧是**消费者**。所以应当由 training 侧 `put_tensor(weight_key, new_state_dict_tensor)`，rollout 侧在下一轮 `generate` 之前 `get_tensor(weight_key)` 加载新权重。key 可以用带版本号的固定键（如 `"policy_weights"`，配合 `upsert` 覆盖）。4.4 节会给出基于 `put_tensor_with_tp` 的张量并行真实方案。

---

### 4.2 双客户端初始化：训练侧与 rollout 侧的 setup

#### 4.2.1 概念说明

要让上面那张数据流图成立，两侧必须先各自「连上网」——也就是各自构造一个 `MooncakeDistributedStore` 并调用 `setup()`。这个示例最值得学的地方是：**它示范了「同一个 metadata/master、两个不同客户端」的部署形态**。

- 训练侧客户端（`TrainGroup.__init__` 里）：`local_hostname="localhost:12345"`、RDMA 网卡 `erdma_1`。
- rollout 侧客户端（`RolloutController.__init__` 里）：`local_hostname="localhost:12346"`、RDMA 网卡 `erdma_0`。
- 两者连到**同一个** metadata server（`http://localhost:8080/metadata`）和**同一个** master（`localhost:50051`）。

这正是分布式 RL 的真实形态：训练集群和推理集群各跑各的客户端进程，但只要它们指向**同一组元数据服务**，就能看到同一片共享存储。

#### 4.2.2 核心流程

`setup()` 的参数序列（沿用 [u5-l4](u5-l4-python-store-api.md) 的定义）：

```
setup(local_hostname,          # 本客户端的「身份地址」，集群内唯一
      metadata_server,         # 元数据服务器（对象 key→副本位置的查询入口）
      global_segment_size,     # 贡献进全局池的段大小（本节点当 store server 的「货架空」）
      local_buffer_size,       # 本地 buffer 池大小（自己的「周转箱」）
      protocol,                # "tcp" / "rdma" / ...
      rdma_devices,            # 用哪块 RDMA 网卡，如 "erdma_1"、"mlx5_0"
      master_server_addr)      # Master 服务地址（控制面）
```

两侧客户端初始化流程对比：

```
TrainGroup.__init__                           RolloutController.__init__
  ├─ 创建 N 个 TrainActor                       ├─ 初始化数据源游标(epoch/sample_index)
  ├─ training_client = MooncakeDistributedStore() ├─ rollout_client = MooncakeDistributedStore()
  └─ training_client.setup(                     └─ rollout_client.setup(
        "localhost:12345",                            "localhost:12346",
        "http://localhost:8080/metadata",             "http://localhost:8080/metadata",  # 同一个
        512MB, 128MB,                                 512MB, 128MB,
        "rdma",                                       "rdma",
        "erdma_1",                                    "erdma_0",                          # 不同网卡
        "localhost:50051")                            "localhost:50051")                  # 同一个 master
```

两侧都开了 **512MB 全局段、128MB 本地 buffer**——在这个 toy 规模下足够装下 mock 样本。注意「两个客户端用两块不同网卡（`erdma_1` vs `erdma_0`）」是有意的：在同一台物理机上跑两个客户端时，让它们走不同网卡可以避免网卡争用，这也是真实部署里常见的做法。

#### 4.2.3 源码精读

训练侧客户端的创建（注意它落在 `TrainGroup.__init__` 里）：

```python
# init Mooncake store client
self.training_client = MooncakeDistributedStore()
# RDMA initialization
self.training_client.setup("localhost:12345", 
                           "http://localhost:8080/metadata", 
                           512*1024*1024, 
                           128*1024*1024, 
                           "rdma", 
                           "erdma_1", # or other NIC like mlx5_1
                           "localhost:50051")
```

—— [mooncake-rl/examples/rl_samples.py:97-106](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-rl/examples/rl_samples.py#L97-L106)：训练侧客户端，hostname `localhost:12345`，网卡 `erdma_1`。

rollout 侧客户端的创建（落在 `RolloutController.__init__` 里）：

```python
# init Mooncake store client
self.rollout_client = MooncakeDistributedStore()
# RDMA initialization
self.rollout_client.setup("localhost:12346", 
                          "http://localhost:8080/metadata", 
                          512*1024*1024, 
                          128*1024*1024, 
                          "rdma", 
                          "erdma_0", # or other NIC like mlx5_0 
                          "localhost:50051")
```

—— [mooncake-rl/examples/rl_samples.py:231-240](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-rl/examples/rl_samples.py#L231-L240)：rollout 侧客户端，hostname `localhost:12346`，网卡 `erdma_0`。注意它和训练侧**共享** metadata（`http://localhost:8080/metadata`）与 master（`localhost:50051`），这才是它们能互通的根因。

再看 C++ 侧，`setup` 在 pybind11 里注册的位置参数版本：

```cpp
.def(
    "setup",
    [](MooncakeStorePyWrapper &self, const std::string &local_hostname,
       const std::string &metadata_server,
       size_t global_segment_size = 1024 * 1024 * 16,
       size_t local_buffer_size = 1024 * 1024 * 16,
       const std::string &protocol = "tcp",
       const std::string &rdma_devices = "",
       const std::string &master_server_addr = "127.0.0.1:50051",
       ...) {
        auto real_client = self.init_real_client();          // 创建 RealClient 并注册到 ResourceTracker
        ...
        return real_client->setup_real(local_hostname, metadata_server,
                                       global_segment_size, local_buffer_size,
                                       protocol, rdma_devices,
                                       master_server_addr, ...);
    },
    py::arg("local_hostname"), py::arg("metadata_server"),
    py::arg("global_segment_size"), py::arg("local_buffer_size"),
    py::arg("protocol"), py::arg("rdma_devices"),
    py::arg("master_server_addr"), ...)
```

—— [mooncake-integration/store/store_py.cpp:2019-2050](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L2019-L2050)：`setup` 的参数顺序与示例里**按位置传参**的顺序一一对应。注意默认协议是 `tcp`、默认网卡是空串，而示例显式用了 `"rdma"` + 具体网卡名。`init_real_client()` 会把客户端注册到 `ResourceTracker`（保证进程退出时能回收资源）。

#### 4.2.4 代码实践

**实践目标**：理解「同一 metadata/master、不同 hostname/网卡」的部署形态，并能在本机用 `tcp` 协议复刻一个最小双客户端互通（不依赖 RDMA 硬件）。

**操作步骤**：

1. 启动一组元数据服务（master + metadata server）。如果你已经按 [u1-l5](u1-l5-first-transfer-metadata.md) 跑过 `mooncake_master` + metadata server，可复用。
2. 写一个最小脚本（**示例代码**，不是仓库原有文件），开两个客户端验证互通：

```python
# 示例代码：验证「同 metadata、双客户端」互通
from mooncake.store import MooncakeDistributedStore

trainer = MooncakeDistributedStore()
trainer.setup("127.0.0.1:12345", "http://127.0.0.1:8080/metadata",
              256*1024*1024, 64*1024*1024,
              "tcp", "", "127.0.0.1:50051")

import torch
trainer.put_tensor("hello_rl", torch.arange(8, dtype=torch.float32))

rollout = MooncakeDistributedStore()
rollout.setup("127.0.0.1:12346", "http://127.0.0.1:8080/metadata",
              256*1024*1024, 64*1024*1024,
              "tcp", "", "127.0.0.1:50051")

print(rollout.get_tensor("hello_rl"))   # 同一个 metadata，所以能读到 trainer 写的 key
```

3. 对比示例里 `erdma_1` / `erdma_0` 的写法，把它改成你本机真实网卡名（用 `rdma link` 或 `ibv_devinfo` 查看），或保持 `tcp`。

**需要观察的现象**：第二个客户端（rollout）能读到第一个客户端（trainer）写入的张量，尽管它们 hostname 不同。

**预期结果**：打印 `tensor([0., 1., 2., 3., 4., 5., 6., 7.])`。

> **待本地验证**：上述脚本能否跑通取决于你是否已启动 master（`127.0.0.1:50051`）和 metadata server（`127.0.0.1:8080`）。无 RDMA 时务必用 `protocol="tcp"`、`rdma_devices=""`。若报 `Error from etcd client` / 连接拒绝，先排查这两个服务是否就绪（可借助 `mooncake-troubleshoot` 技能）。

#### 4.2.5 小练习与答案

**练习 1**：示例里训练侧和 rollout 侧用了**不同的 hostname**（`12345` vs `12346`）和**不同的网卡**（`erdma_1` vs `erdma_0`），但必须用**同一个** metadata/master。如果两侧连到了不同的 metadata server，会发生什么？

**参考答案**：两侧会看到**两份互相隔离**的 key 空间。rollout 侧 `put_tensor("3", ...)` 写进的是它那个 metadata 管辖的存储；training 侧去另一个 metadata 查 `"3"`，查不到，`get_tensor` 返回 `None`（见 4.3 节 `if samples is None` 的判断）。所以「同一个 metadata/master」是双方互通的前提。

**练习 2**：`global_segment_size=512MB` 和 `local_buffer_size=128MB` 分别控制什么？为什么不能都设得很大或很小？

**参考答案**：`global_segment_size` 是本节点**贡献进全局资源池**的段大小（扮演 store server 时给别人放副本的「货架空」）；`local_buffer_size` 是本客户端**自己的周转箱**（读写时临时缓冲）。设太小会写不下数据 / 频繁分配；设太大则占用内存/显存。这两个参数在 [u5-l4](u5-l4-python-store-api.md) 4.1 节有更详细的「货架 vs 周转箱」比喻。

---

### 4.3 rollout 样本数据流：put_tensor → get_tensor 的真实链路

#### 4.3.1 概念说明

本节讲示例里**唯一真正经过 Store 的完整数据流**：rollout 侧 `put_tensor` 写入样本、training 侧 `get_tensor` 取回样本。

需要先澄清一个关键点（**这是诚实阅读本示例必须知道的**）：

> ⚠️ **dummy 示例的类型不匹配**：`RolloutManage.generate` 里传给 `put_tensor` 的 `rollout_samples` 实际是一个 `list[dict]`（每个 dict 是 `{rollout_id, obs, action, reward}`），而**不是** torch 张量。但 C++ 侧的 `put_tensor` 会校验「输入必须是 PyTorch tensor」，见 [store_py.cpp 的 `extract_tensor_info`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L99-L106)。因此**这个示例按字面跑、用真实 client 时，`put_tensor` 会因类型校验失败而返回错误码**。它的价值在于展示「谁写、谁读、用什么 key」的**架构模式**，而非一个端到端可运行的训练脚本。

在本讲其余部分，当我们讨论「样本/隐状态」时，默认你已理解：**真实场景下，`put_tensor` 的第二个参数应当是真正的 torch 张量**（例如把 obs 堆叠成 `torch.Tensor`、或直接传激活/隐状态/logits 张量）。本节的源码精读会同时讲清「示例想表达的意图」和「C++ 层真实的张量在线格式」。

#### 4.3.2 核心流程

样本在 Store 中的写入与读取，本质是把一个 torch 张量序列化成 `[TensorMetadata][data]` 两段、再还原回来。这条链路在 [u5-l4 4.2 节](u5-l4-python-store-api.md)已经讲过字节/张量 API，这里聚焦它在 RL 数据流里的角色。

```
写入侧（RolloutManage.generate）：
  list[sample]  ──(示例意图)──▶  torch.Tensor
                                     │
                       put_tensor(key, tensor)
                                     │  C++: extract_tensor_info() 提取 shape/dtype/ptr
                                     │       拼成 [TensorMetadata][data] 两个 span
                                     │       释放 GIL → store_->put_parts(key, values)
                                     ▼
                    Mooncake Store:  key -> [TensorMetadata][raw bytes]

读取侧（TrainGroup.train）：
                    Mooncake Store:  key -> [TensorMetadata][raw bytes]
                                     │  C++: 释放 GIL → store_->get_buffer(key)
                                     │       buffer_to_tensor() 解析 metadata
                                     │       用 numpy 重建 → torch.from_numpy
                                     ▼
                              torch.Tensor  ──▶  逐 actor 做前向/反向
```

张量在线格式（详解见 [u5-l4](u5-l4-python-store-api.md)）：

\[ \text{buffer} = \underbrace{\text{TensorMetadata}}_{\text{头部：dtype/ndim/shape/offset}} \;\|\; \underbrace{\text{raw data bytes}}_{\text{payload：张量的连续内存}} \]

之所以要带一个 metadata 头，是因为 Store 数据面只搬「裸字节」，读取侧必须从头部恢复出 `dtype` 和 `shape` 才能重建张量。

#### 4.3.3 源码精读

**写入侧**——`RolloutManage.generate`，把每个 engine 产出的 sample 收集后 `put_tensor`：

```python
def generate(self, rollout_id: int) -> str:
    rollout_samples = []
    for engine in self.rollout_engines:
        sample = engine.generate(rollout_id)
        rollout_samples.append(sample)
    
    key = str(rollout_id)
    self.controller.rollout_client.put_tensor(key, rollout_samples)   # ★ 写入 Store
    print(f"[RolloutManager] Generated rollout {rollout_id}: {rollout_samples}")
    return key
```

—— [mooncake-rl/examples/rl_samples.py:291-309](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-rl/examples/rl_samples.py#L291-L309)：rollout 侧把样本以 `key=str(rollout_id)` 写入 Store，并把这个 key 作为 `rollout_data_ref` 返回给主循环。注意 4.3.1 提到的类型问题：这里的 `rollout_samples` 是 `list[dict]`，真实跑时应改为张量。

单个样本的产出（`RolloutEngine.generate`）：

```python
def generate(self, rollout_id: int):
    data = torch.randint(0, 100, (4,), dtype=torch.int32).tolist()
    action = random.randint(0, 9)
    reward = random.uniform(-1.0, 1.0)
    sample = {
        "rollout_id": rollout_id,
        "obs": data,        # observation（示例里是 4 个随机 int）
        "action": action,
        "reward": reward,
    }
    return sample
```

—— [mooncake-rl/examples/rl_samples.py:185-200](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-rl/examples/rl_samples.py#L185-L200)：一个样本就是 `(obs, action, reward)` 三元组。在真实 RL 里，这里的 `obs`/中间产物会被换成真正的隐状态/激活/logits 张量——那才是 Store 高带宽直传的价值所在。

**读取侧**——`TrainGroup.train`，用 key 从 Store 取回样本并训练：

```python
def train(self, rollout_id: int, rollout_key: str):
    samples = self.training_client.get_tensor(rollout_key)   # ★ 从 Store 读取
    if samples is None:
        print(f"[TrainGroup] Rollout {rollout_id} not found in store")
        return

    losses = []
    for actor, sample in zip(self.actor_handlers, samples):
        loss = actor.train(sample)
        losses.append(loss)
    ...
```

—— [mooncake-rl/examples/rl_samples.py:132-154](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-rl/examples/rl_samples.py#L132-L154)：training 侧靠 `rollout_key` 取数据。注意 `if samples is None` 的分支——这正是 4.2.5 练习里「key 不存在」时的真实表现。

单个 actor 的「训练步」——一个 dummy MSE loss（注意它是**真实计算**的，不是 mock）：

```python
obs = torch.tensor(samples["obs"], dtype=torch.float32).unsqueeze(0)  # shape [1, dim]
action = samples["action"]
reward = torch.tensor([samples["reward"]], dtype=torch.float32)
logits = self.model(obs)                       # 前向
pred = logits[0, action % logits.shape[1]]
loss = (pred - reward).pow(2).mean()           # dummy MSE
self.optimizer.zero_grad()
loss.backward()
self.optimizer.step()
```

—— [mooncake-rl/examples/rl_samples.py:48-63](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-rl/examples/rl_samples.py#L48-L63)：损失写成公式是

\[ \mathcal{L} = \left(\hat{y}_{\text{action}} - r\right)^{2} \]

其中 \(\hat{y}_{\text{action}}\) 是模型对所采取 action 的预测值，\(r\) 是 reward。虽然语义是「玩具」，但前向/反向/`optimizer.step()` 都是真的。

现在下探到 C++，看 `put_tensor` 到底怎么把张量塞进 Store。先看类型校验（这就是 4.3.1 提到的「dummy 示例会失败」的原因）：

```cpp
if (!(tensor.attr("__class__").attr("__name__").cast<std::string>()
          .find("Tensor") != std::string::npos)) {
    LOG(ERROR) << "Input " << (key_name.empty() ? "" : "for " + key_name)
               << " is not a PyTorch tensor";
    return info;   // info.valid() 为 false
}
```

—— [mooncake-integration/store/store_py.cpp:99-106](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L99-L106)：`extract_tensor_info` 一进来就检查对象类名是否含 `"Tensor"`。`list[dict]` 不满足，于是返回无效 info，`put_tensor_impl` 据此返回 `INVALID_PARAMS` 错误码。

再看写入主体 `put_tensor_impl`——「持 GIL 提取元数据，释放 GIL 做传输」的典型模式：

```cpp
int put_tensor_impl(const std::string &key, pybind11::object tensor,
                    const ReplicateConfig &config) {
    // Validation & Metadata extraction (GIL Held)
    auto info = extract_tensor_info(tensor, key);
    if (!info.valid()) return to_py_ret(ErrorCode::INVALID_PARAMS);

    // Prepare spans: [TensorMetadata 头][data payload]
    std::vector<std::span<const char>> values;
    values.emplace_back(reinterpret_cast<const char *>(&info.metadata),
                        sizeof(TensorMetadata));
    append_tensor_payload_span(values, info.data_ptr, info.tensor_size);

    // Store (GIL Released)
    py::gil_scoped_release release_gil;
    int ret = store_->put_parts(key, values, config);
    ...
}
```

—— [mooncake-integration/store/store_py.cpp:674-693](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L674-L693)：把张量拆成「metadata 头 + payload」两段 `span`，**释放 GIL 后**才调 `store_->put_parts`。释放 GIL 是关键——这样其它 Python 线程（比如另一个 rollout engine）在传输阻塞期间仍能跑。

`put_tensor` 公开方法是对 `put_tensor_impl` 的薄封装，并多一道「client 初始化」检查：

—— [mooncake-integration/store/store_py.cpp:695-703](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L695-L703)：`put_tensor` 检查 `is_client_initialized() && !use_dummy_client_`，然后调 `put_tensor_impl(key, tensor, ReplicateConfig{})`（默认无副本配置）。

读取侧 `get_tensor`：

```cpp
pybind11::object get_tensor(const std::string &key) {
    if (!is_client_initialized()) { ... return pybind11::none(); }

    std::shared_ptr<BufferHandle> buffer_handle;
    {
        py::gil_scoped_release release_gil;
        buffer_handle = store_->get_buffer(key);     // 释放 GIL 拉数据
    }
    // Metadata parsing must happen with GIL held
    return buffer_to_tensor(buffer_handle.get(), NULL, 0);   // 重建 torch 张量
}
```

—— [mooncake-integration/store/store_py.cpp:494-507](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L494-L507)：`get_tensor` 同样是「释放 GIL 拉字节、持 GIL 解析重建」。`buffer_to_tensor` 负责把 `[TensorMetadata][data]` 还原成 `torch.from_numpy(...)`（[store_py.cpp:162-283](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L162-L283)）。

Python 绑定注册处（确认这两个方法确实挂在 `MooncakeDistributedStore` 上）：

—— [mooncake-integration/store/store_py.cpp:2235-2257](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L2235-L2257)：`get_tensor` / `batch_get_tensor` / `put_tensor` / `batch_put_tensor` 的绑定注册。

#### 4.3.4 代码实践

**实践目标**：亲手验证「rollout 写一个张量、training 用同一个 key 读回」这条链路，并确认 GIL 释放带来的并发收益。

**操作步骤**：

1. 准备两个已 `setup` 好的客户端（可复用 4.2.4 的脚本里 `trainer` / `rollout` 两个实例）。
2. 用 **torch 张量**（而非 dict）复刻 `generate → train` 的样本流（**示例代码**）：

```python
# 示例代码：用真实张量复刻 generate→train 的数据流
import torch
from mooncake.store import MooncakeDistributedStore

# rollout 侧：扮演 RolloutManage.generate
rollout = MooncakeDistributedStore()
rollout.setup("127.0.0.1:12346", "http://127.0.0.1:8080/metadata",
              256*1024*1024, 64*1024*1024, "tcp", "", "127.0.0.1:50051")

rollout_id = 3
# 关键修正：用 torch 张量代替示例里的 list[dict]
batch_obs = torch.randint(0, 100, (4, 4), dtype=torch.int32)   # [num_engines, obs_dim]
rollout.put_tensor(str(rollout_id), batch_obs)

# training 侧：扮演 TrainGroup.train
trainer = MooncakeDistributedStore()
trainer.setup("127.0.0.1:12345", "http://127.0.0.1:8080/metadata",
              256*1024*1024, 64*1024*1024, "tcp", "", "127.0.0.1:50051")

samples = trainer.get_tensor(str(rollout_id))
print("取回:", samples.shape, samples.dtype)   # 应为 torch.Size([4,4]) int32
```

3. （进阶）验证 GIL 释放：在一个 Python 线程里跑一个长 `put_tensor`（比如 100MB 张量），同时在主线程跑一个纯 Python 计数循环。观察计数循环是否被卡住。

**需要观察的现象**：

- 步骤 2：training 侧能取回与写入**同形状、同 dtype、同数值**的张量。
- 步骤 3：若 `put_tensor` 真的释放了 GIL，主线程的计数循环在传输期间仍在推进（打印会继续）；反之若不释放 GIL，计数循环会停顿。

**预期结果**：步骤 2 打印 `torch.Size([4, 4]) torch.int32` 且数值一致；步骤 3 观察到计数循环未长时间卡死。

> **待本地验证**：能否跑通取决于 master/metadata 是否就绪。步骤 3 的「GIL 释放」现象取决于张量大小和网络延迟，本机 tcp 回环上效果可能不明显，建议用较大张量（≥几十 MB）放大观察。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `put_tensor_impl` 要先「持 GIL 提取元数据」、再「释放 GIL 做传输」，而不是反过来？

**参考答案**：提取元数据时需要访问 Python 对象（`tensor.shape`、`tensor.data_ptr()` 等），这些操作**必须持有 GIL**；而 `store_->put_parts` 是纯 C++ 的网络/传输阻塞调用，**不需要 GIL**。释放 GIL 期间，其它 Python 线程能继续跑——这是 Python 绑定性能的关键。如果反过来（持 GIL 做传输），整个 Python 进程会被一次传输阻塞。

**练习 2**：`TrainGroup.train` 里有 `if samples is None: ... return`。什么情况下 `get_tensor` 会返回 `None`？

**参考答案**：当 key 在 Store 中不存在时。底层 `get_buffer(key)` 找不到对象返回空 handle，`buffer_to_tensor` 据此返回 `py::none()`（见 [store_py.cpp:494-507](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L494-L507)）。在 RL 数据流里，这通常意味着 rollout 侧还没写完（生产者/消费者时序错配）或两侧连到了不同的 metadata（见 4.2.5）。

---

### 4.4 权重分发与检查点：示例中的 mock 与真实方案

#### 4.4.1 概念说明

4.1 节那张图里，`update_weights()` 和 `save_model()` 在本示例里**完全没有走 Store**——它们要么是空壳 `print`，要么用 `torch.save` 落到本地文件。本节要讲清两件事：

1. **示例里的 mock 现状**：`update_weights` / `init_weight_update_connections` 只打印；检查点和数据集状态用本地 `torch.save`/`torch.load`。
2. **真实方案应该长什么样**：权重分发应该走 Store 的张量并行（TP）接口 `put_tensor_with_tp` / `get_tensor_with_tp`，按 TP rank 把权重分片成 `key_tp_<rank>` 多个 key；检查点可以借助 Store 的 `save_tensor_to_safetensor` / `load_tensor_from_safetensor` 持久化。

为什么要讲「真实方案」？因为 RL 训练里，**权重大小通常远大于样本**（一个几十 B 参数的策略模型，权重动辄几十上百 GB），这正是 Mooncake Store + RDMA 直传的核心收益场景。示例把它们 mock 掉只是为了聚焦「数据流模式」，真实工程必须补上。

另外，Store 在 C++ 层已经为这类 RL/训练数据**内置了对象类型枚举**，说明这些用例是被一等公民对待的：

```cpp
py::enum_<ObjectDataType>(m, "ObjectDataType")
    .value("KVCACHE", ObjectDataType::KVCACHE)
    .value("TENSOR", ObjectDataType::TENSOR)
    .value("WEIGHT", ObjectDataType::WEIGHT)        // ← 权重
    .value("SAMPLE", ObjectDataType::SAMPLE)        // ← rollout 样本
    .value("ACTIVATION", ObjectDataType::ACTIVATION)// ← 隐状态/激活
    .value("GRADIENT", ObjectDataType::GRADIENT)
    .value("OPTIMIZER_STATE", ObjectDataType::OPTIMIZER_STATE)
    ...
```

—— [mooncake-integration/store/store_py.cpp:1727-1738](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L1727-L1738)：`ObjectDataType` 把 `WEIGHT` / `SAMPLE` / `ACTIVATION` / `GRADIENT` 都列为一等类型——RL 训练里的各类张量都有对应语义。这些类型可通过 `ReplicateConfig.data_type` 在写入时打标，便于 Master 做按类型的调度/复制策略。

#### 4.4.2 核心流程

**示例现状（mock）**：

```
权重同步：   TrainGroup.update_weights()        → 仅 print("[TrainGroup] Weights updated")
检查点：     TrainActor.save_model(rollout_id)  → torch.save(state_dict, "model_{id}.pth")
数据集状态： RolloutController.save/load        → torch.save / torch.load 本地 .pt 文件
```

**真实方案（走 Store）**——权重分发的 TP 分片流程：

```
training 侧（权重生产者）：
  new_state_dict = {name: param for name, param in model.named_parameters()}
  for name, tensor in new_state_dict.items():
      put_tensor_with_tp("weights/" + name, tensor, tp_size=N, split_dim=d)
                            │
                            ▼  C++ 把张量沿 split_dim 切成 N 片，分别存为
                            ▼  "weights/{name}_tp_0", "_tp_1", ..., "_tp_{N-1}"
rollout 侧（权重消费者，每个 rank 各取自己那片）：
  for name in param_names:
      shard = get_tensor_with_tp("weights/" + name, tp_rank=rank, tp_size=N)
      load_into_engine(name, shard)
```

TP key 的命名规则在 C++ 里固定：

```cpp
std::string get_tp_key_name(const std::string &base_key, int rank) const {
    return base_key + "_tp_" + std::to_string(rank);
}
```

—— [mooncake-integration/store/store_py.cpp:419-421](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L419-L421)：TP 分片的 key 就是 `base_key + "_tp_" + rank`。这让「写入侧一次性切 N 片、读取侧每个 rank 取自己那片」成为可能，正好匹配训练侧（TP 维度完整权重）与推理侧（每个 rank 只需自己那个分片）的不对称需求。

#### 4.4.3 源码精读

**示例的 mock 实现**——`update_weights` 全是空壳：

```python
def init_weight_update_connections(self, rollout_manager):
    """Establish connection with rollout manager for weight synchronization."""
    print("[TrainGroup] Connected to rollout manager")

def update_weights(self):
    """Update model weights.
    In real training, this would sync parameters from trainer to rollout engines."""
    print("[TrainGroup] Weights updated")
```

—— [mooncake-rl/examples/rl_samples.py:119-130](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-rl/examples/rl_samples.py#L119-L130)：注意 `update_weights` 的 docstring 自己也写明「In real training, this would sync parameters from trainer to rollout engines」——作者明确告诉你这里是 mock。

检查点保存走的是本地 `torch.save`（**不过 Store**）：

```python
def save_model(self, rollout_id: int):
    torch.save(self.model.state_dict(), f"model_{rollout_id}.pth")
    print(f"[TrainActor] Model saved to model_{rollout_id}.pth")
```

—— [mooncake-rl/examples/rl_samples.py:69-74](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-rl/examples/rl_samples.py#L69-L74)：`TrainActor.save_model` 用 `torch.save` 写本地 `.pth`，由 `TrainGroup.save_model`（[第 156-164 行](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-rl/examples/rl_samples.py#L156-L164)）逐 actor 调用。这是「检查点管理」的本地文件方案。

数据集游标状态（`epoch_id`/`sample_index`）的 load/save 同样是本地文件：

—— [mooncake-rl/examples/rl_samples.py:242-271](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-rl/examples/rl_samples.py#L242-L271)：`RolloutController.load/save` 用 `torch.load`/`torch.save` 维护 `global_dataset_state_dict_{rollout_id}.pt`，用于断点续训。

现在看**真实方案**依赖的 C++ 接口——`put_tensor_with_tp` 的绑定与实现：

```cpp
.def("put_tensor_with_tp", &MooncakeStorePyWrapper::put_tensor_with_tp,
     py::arg("key"), py::arg("tensor"), py::arg("tp_rank") = 0,
     py::arg("tp_size") = 1, py::arg("split_dim") = 0,
     "Put a PyTorch tensor into the store, split into shards for "
     "tensor parallelism.\n"
     "The tensor is chunked immediately and stored as separate keys "
     "(e.g., key_tp_0).")
```

—— [mooncake-integration/store/store_py.cpp:2237-2243](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L2237-L2243)：`put_tensor_with_tp` 把一个完整张量**立刻切分**成 `tp_size` 片，分别存成 `key_tp_0..key_tp_{N-1}`。这正是「训练侧持完整权重、推理侧按 rank 取分片」所需要的。

读取侧 `get_tensor_with_tp`：

```cpp
pybind11::object get_tensor_with_tp(const std::string &key, int tp_rank = 0,
                                    int tp_size = 1, int split_dim = 0) {
    if (tp_size <= 1) return get_tensor(key);
    return get_tensor(get_tp_key_name(key, tp_rank));   // 直接取 "key_tp_{rank}"
}
```

—— [mooncake-integration/store/store_py.cpp:475-479](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L475-L479)：每个 rollout rank 只需 `get_tensor("key_tp_" + rank)` 取回自己的分片，无需拉取整个权重。

#### 4.4.4 代码实践

**实践目标**：把示例里被 mock 的 `update_weights`，替换成一个「真正走 Store 的 TP 权重分发」的最小实现，并验证每个 rank 取到正确的分片。

**操作步骤**：

1. 准备一个 `trainer` 客户端（权重生产者）和 `tp_size` 个 `rollout` 客户端（消费者），全部连同一个 metadata。
2. 写一个最小 TP 权重分发脚本（**示例代码**，对照 4.4.2 的流程）：

```python
# 示例代码：用 put_tensor_with_tp / get_tensor_with_tp 复刻「权重分发」
import torch
from mooncake.store import MooncakeDistributedStore

# training 侧：把一个完整权重切成 TP 分片写入
trainer = MooncakeDistributedStore()
trainer.setup("127.0.0.1:12345", "http://127.0.0.1:8080/metadata",
              256*1024*1024, 64*1024*1024, "tcp", "", "127.0.0.1:50051")

W = torch.arange(8, dtype=torch.float32).view(4, 2)   # 假装是某一层权重
tp_size, split_dim = 2, 0
trainer.put_tensor_with_tp("layer0/weight", W, tp_size=tp_size, split_dim=split_dim)

# rollout 侧：每个 rank 只取自己那一片
for rank in range(tp_size):
    cli = MooncakeDistributedStore()
    cli.setup(f"127.0.0.1:{13000+rank}", "http://127.0.0.1:8080/metadata",
              256*1024*1024, 64*1024*1024, "tcp", "", "127.0.0.1:50051")
    shard = cli.get_tensor_with_tp("layer0/weight", tp_rank=rank,
                                   tp_size=tp_size, split_dim=split_dim)
    print(f"rank {rank} 取到:\n{shard}")

# 验证：拼回应等于原权重
expected = W.chunk(tp_size, split_dim)
# 分别对比每个 rank 的 shard 与 expected[rank]
```

3. 对照 `test_async_store.py` 里 `test_02_tp_single_tensor` 的断言写法（[第 324-357 行](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/test_async_store.py#L324-L357)），用 `torch.equal(shard, expected[rank])` 校验每个分片正确。

**需要观察的现象**：trainer 一次 `put_tensor_with_tp` 后，Store 里出现 `layer0/weight_tp_0`、`layer0/weight_tp_1` 两个 key；每个 rank 只读到对应的那一片。

**预期结果**：rank 0 取到 `W` 的前一半行，rank 1 取到后一半；`torch.cat` 拼回后等于原 `W`。

> **待本地验证**：依赖 master/metadata 就绪。无 RDMA 时用 `tcp`。完整可运行的 TP 测试可参考 [scripts/test_async_store.py](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/test_async_store.py) 的 `test_02_tp_single_tensor` / `test_03_tp_batch`。

#### 4.4.5 小练习与答案

**练习 1**：示例用本地 `torch.save` 存检查点（`model_{rollout_id}.pth`）。如果要改成「检查点也存进 Store」，你会用哪个现成接口？它的好处是什么？

**参考答案**：可以用 `save_tensor_to_safetensor`（把 Store 里的张量导出成 safetensors 文件）和 `load_tensor_from_safetensor`（从 safetensors 读回并写进 Store）。好处是：检查点可在**集群任意节点**通过 Store 访问，不必依赖共享文件系统；且 Store 的多副本（`ReplicateConfig`）能天然提供检查点的可用性冗余。这两个接口见 [store_py.cpp:1605-1696](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L1605-L1696)。

**练习 2**：为什么权重分发用 `put_tensor_with_tp`（按 rank 切片存多个 key），而不是让训练侧把完整权重 `put_tensor` 一次、所有 rollout rank 都 `get_tensor` 读完整权重？

**参考答案**：因为推理侧（rollout engine）通常**每个 TP rank 只需要自己那个分片**。若每个 rank 都读完整权重，会造成 \(N\) 倍的冗余网络读取（每个 rank 拉了它用不到的 \(N-1\) 份），且需要每个 rank 自己再切分。`put_tensor_with_tp` 在写入侧**一次性切好**、存成 `key_tp_<rank>`，每个 rank 只拉自己那片，网络流量从 \(N \times |W|\) 降到 \(|W|\)，是典型的「写入时多做一些、读取时省 N 倍」权衡。

---

### 4.5 async_store 异步优化点：把阻塞传输扔进线程池

#### 4.5.1 概念说明

`rl_samples.py` 用的是**同步** `MooncakeDistributedStore`——`put_tensor` / `get_tensor` 都是阻塞调用，调一次等一次。这在「单个 rollout engine 串行产出样本」的 toy 场景下没问题，但在真实 RL 里，你往往希望**多个 rollout engine 并行产出、多个权重分片并行拉取、传输与计算重叠**。

`mooncake-integration/store/async_store.py` 提供的 `MooncakeDistributedStoreAsync` 就是为此而生。它的核心思想非常朴素：

> **不要改写 Store 的 C++ 实现，而是把每一个同步方法包成一个 coroutine，扔进默认线程池（`run_in_executor`）里跑。这样在 asyncio 事件循环里，多个 `async_put_tensor` / `async_get_tensor` 就能用 `asyncio.gather` 并发起来。**

注意一个前提：底层 C++ 调用**本来就释放了 GIL**（4.3.3 讲过），所以多个线程池 worker 可以真正并行地跑传输，不会因为 GIL 退化成串行。这是「异步包装能带来真实并发收益」的根本原因。

#### 4.5.2 核心流程

`MooncakeDistributedStoreAsync` 继承自同步的 `MooncakeDistributedStore`，自己**不定义任何 `async_` 方法**，而是用 `__getattr__` 动态生成：

```
调用 self.async_put_tensor(key, tensor)
        │
        ▼
__getattr__("async_put_tensor")
        │  ① 去掉 "async_" 前缀 → "put_tensor"
        │  ② 检查同步方法是否存在且可调用
        │  ③ _make_async_wrapper(self.put_tensor) 生成一个 coroutine
        │  ④ setattr 缓存，下次直接命中
        ▼
wrapper(*args, **kwargs):
        │  loop = asyncio.get_running_loop()
        │  func = functools.partial(put_tensor, *args, **kwargs)
        │  return await loop.run_in_executor(None, func)   # 扔进默认 ThreadPoolExecutor
        ▼
   线程池里执行同步 put_tensor（已释放 GIL）
```

于是业务侧可以这么用并发：

```python
# 多个 rollout engine 并行写样本
await asyncio.gather(*[
    store.async_put_tensor(f"sample_{i}", samples[i])
    for i in range(num_engines)
])

# 多个 TP 权重分片并行拉取（这是 RL 权重分发的最大收益点）
shards = await asyncio.gather(*[
    store.async_get_tensor_with_tp("layer0/weight", tp_rank=r, tp_size=N)
    for r in range(N)
])
```

#### 4.5.3 源码精读

整个异步包装器只有 32 行，全靠 `__getattr__` + `run_in_executor`：

```python
class MooncakeDistributedStoreAsync(MooncakeDistributedStore):
    def __getattr__(self, name: str):
        if not name.startswith("async_"):
            raise AttributeError(...)
        sync_method_name = name[6:]                          # 去掉 "async_" 前缀
        if not hasattr(self, sync_method_name):
            raise AttributeError(...)
        sync_method = getattr(self, sync_method_name)
        if not callable(sync_method):
            raise AttributeError(...)
        async_method = self._make_async_wrapper(sync_method)
        setattr(self, name, async_method)                    # 缓存：下次直接命中
        return async_method
```

—— [mooncake-integration/store/async_store.py:5-22](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/async_store.py#L5-L22)：`__getattr__` 只在「访问不存在的属性」时触发。任何 `async_xxx` 调用都会被翻译成「包一层同步 `xxx`」，并以同名属性缓存到实例上，后续调用零开销。

包装器的实现：

```python
def _make_async_wrapper(self, sync_method):
    @functools.wraps(sync_method)
    async def wrapper(*args, **kwargs):
        loop = asyncio.get_running_loop()
        func = functools.partial(sync_method, *args, **kwargs)
        return await loop.run_in_executor(None, func)        # 默认线程池
    return wrapper
```

—— [mooncake-integration/store/async_store.py:24-31](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/async_store.py#L24-L31)：`run_in_executor(None, func)` 的第一个参数 `None` 表示用**默认的 `ThreadPoolExecutor`**。`functools.wraps` 保留了原方法的元信息（名字、docstring）。

这个异步包装是 wheel 的正式组件（不是临时脚本）——打包时会被复制进 wheel：

—— [scripts/build_wheel.sh:48](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/build_wheel.sh#L48)：`cp mooncake-integration/store/async_store.py mooncake-wheel/mooncake/async_store.py`，所以安装后可直接 `from mooncake.async_store import MooncakeDistributedStoreAsync`（见 [scripts/test_async_store.py:16](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/test_async_store.py#L16)）。

现在看「异步并发」在真实测试里的样子——多个 TP 分片用 `asyncio.gather` 并发拉取：

```python
# Launch all gets in parallel
tasks = []
for rank in range(tp_size):
    tasks.append(self.store.async_get_tensor_with_tp(
        key, tp_rank=rank, tp_size=tp_size, split_dim=split_dim))

slices = await asyncio.gather(*tasks)    # 所有 rank 的分片并发拉取
...
reconstructed = torch.cat(slices, dim=split_dim)
```

—— [scripts/test_async_store.py:346-357](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/test_async_store.py#L346-L357)：这是「RL 权重分发的异步优化点」最直接的范例——`tp_size` 个 `get_tensor_with_tp` 同时发出、`gather` 等全部完成。同步写法只能串行 `for rank in range(tp_size): get_tensor_with_tp(...)`，耗时是各分片之和；异步写法耗时逼近**最慢的那个分片**。

异步 setup 的注意事项（测试里 setup 仍是同步调用，因为初始化只需一次）：

—— [scripts/test_async_store.py:127-139](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/test_async_store.py#L127-L139)：`_do_setup()` 直接同步调 `GLOBAL_STORE.setup(...)`，注释说明「setup 只需做一次初始化，所以同步执行」。这提醒我们：**异步包装的价值在于「高频、可并发」的数据搬运，而非一次性的初始化**。

#### 4.5.4 代码实践

**实践目标**：对比「同步串行拉取 N 个 TP 分片」与「`asyncio.gather` 并发拉取」的耗时差异，直观感受异步优化点。

**操作步骤**：

1. 复用 4.4.4 里 `put_tensor_with_tp` 写入的 `layer0/weight`（`tp_size=2`）。如果想放大差异，把 `tp_size` 调大到 4~8、权重张量调大（如 `torch.randn(2048, 2048)`）。
2. 写两个计时对比脚本（**示例代码**）：

```python
# 示例代码：同步串行 vs 异步并发，拉取 TP 权重分片
import asyncio, time, torch
from mooncake.async_store import MooncakeDistributedStoreAsync

store = MooncakeDistributedStoreAsync()
store.setup("127.0.0.1:12346", "http://127.0.0.1:8080/metadata",
            512*1024*1024, 128*1024*1024, "tcp", "", "127.0.0.1:50051")

W = torch.randn(2048, 2048, dtype=torch.float32)
tp_size, split_dim = 4, 0
store.put_tensor_with_tp("W", W, tp_size=tp_size, split_dim=split_dim)

# 同步串行（用底层同步方法）
t0 = time.perf_counter()
for r in range(tp_size):
    _ = store.get_tensor_with_tp("W", tp_rank=r, tp_size=tp_size, split_dim=split_dim)
print("串行耗时:", time.perf_counter() - t0)

# 异步并发
async def fetch_all():
    tasks = [store.async_get_tensor_with_tp("W", tp_rank=r, tp_size=tp_size,
                                            split_dim=split_dim) for r in range(tp_size)]
    t0 = time.perf_counter()
    await asyncio.gather(*tasks)
    print("并发耗时:", time.perf_counter() - t0)

asyncio.run(fetch_all())
```

3. 多跑几轮取稳定值；尝试增大 `tp_size` 和张量大小，观察并发收益是否变大。

**需要观察的现象**：并发耗时 < 串行耗时；`tp_size` 越大、张量越大，差距越明显（因为并发版的墙钟时间逼近最慢单片，串行版是各片之和）。

**预期结果**：并发耗时约为串行的 \(1/\text{tp\_size}\) 量级（受线程池大小和网卡带宽上限制约，实际会更偏高）。

> **待本地验证**：tcp 回环上差异可能不明显（回环延迟极低）。在真实 RDMA + 跨节点 + 大权重场景下，并发收益才显著。注意默认 `ThreadPoolExecutor` 的线程数有限，`tp_size` 极大时不会线性提速。

#### 4.5.5 小练习与答案

**练习 1**：`MooncakeDistributedStoreAsync` 自己一个 `async_` 方法都没定义，为什么 `store.async_put_tensor(...)` 能用？

**参考答案**：因为它重写了 `__getattr__`。访问 `async_put_tensor` 时，Python 找不到这个属性，就回调 `__getattr__("async_put_tensor")`，它去掉 `async_` 前缀得到 `put_tensor`，用 `_make_async_wrapper` 包一层 coroutine，再 `setattr` 缓存到实例上。这是一种「按需动态生成 + 缓存」的元编程技巧，避免为几十个方法手写一一对应的 async 版本。

**练习 2**：为什么 `run_in_executor` 能带来「真实并发」，而不会因为 GIL 退化成串行？

**参考答案**：因为底层同步方法（如 `put_tensor`/`get_tensor`）在 C++ 层已经 `py::gil_scoped_release` 释放了 GIL（4.3.3 节）。多个线程池 worker 分别调用它们时，真正占用的「网络/传输」阶段不持有 GIL，于是多个传输可以真正并行。如果底层方法持 GIL 不放，`run_in_executor` 就只是「换线程串行」，拿不到并发收益。

---

## 5. 综合实践

把本讲的四个最小模块（双客户端 setup、样本数据流、权重/检查点、异步优化）串成一个**完整的、真正能跑的「迷你 RL 数据流」**。

**任务**：基于 `rl_samples.py` 的架构，写一个**修正版**迷你脚本，完成以下闭环（全部走 Store、用真实张量）：

1. **双客户端初始化**：开一个 `trainer` 客户端（端口 `12345`）和一个 `rollout` 客户端（端口 `12346`），连同一个 metadata/master（4.2）。
2. **rollout 产出 → Store**：rollout 侧生成一个 batch 的 obs 张量（**torch 张量，不是 dict**），`put_tensor(str(rollout_id), obs)`（4.3）。
3. **Store → training 取回 + 训练**：trainer 侧 `get_tensor(str(rollout_id))` 取回，跑一个 `nn.Linear` 前向 + MSE 反向（复刻 [rl_samples.py:48-63](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-rl/examples/rl_samples.py#L48-L63) 的 dummy loss）（4.3）。
4. **权重分发（替代 mock 的 `update_weights`）**：训练一步后，把 `model.weight` 用 `put_tensor_with_tp` 切成 `tp_size=2` 片写入；rollout 侧用 `get_tensor_with_tp` 各取一片，`torch.cat` 验证能拼回原权重（4.4）。
5. **异步并发（加分项）**：把第 4 步改成 `MooncakeDistributedStoreAsync` + `asyncio.gather` 并发拉取两个分片，对比与串行的耗时（4.5）。

**验收标准**：

- 第 2、3 步：trainer 取回的张量与 rollout 写入的**数值完全一致**（`torch.equal`）。
- 第 4 步：rollout 侧两个 rank 的分片拼回后等于训练后的 `model.weight`。
- 第 5 步（加分）：并发耗时 ≤ 串行耗时。

> **待本地验证**：整个任务依赖一组就绪的 master + metadata server。无 RDMA 时全用 `protocol="tcp"`。若没有多机环境，所有客户端可跑在同一进程内、用不同 `local_hostname` 端口区分（这正是 `rl_samples.py` 的做法）。这一步是把「架构模式」落成「可运行代码」的关键练习，务必亲手跑通一次。

## 6. 本讲小结

- `rl_samples.py` 是一个**演示用 dummy 示例**，它把 RL 训练拆成 rollout 侧（`RolloutManage`/`RolloutEngine`）和 training 侧（`TrainGroup`/`TrainActor`），用 Mooncake Store 当二者间的「共享黑板」。
- **真正经过 Store 的只有样本数据流**：rollout 侧 `put_tensor(str(rollout_id), ...)` 写、training 侧 `get_tensor(key)` 读（`eval` 也读）。key = `str(rollout_id)` 是双方解耦的约定。
- **权重同步和检查点在示例里是 mock**：`update_weights` 仅 `print`、检查点用本地 `torch.save`。真实方案应分别走 `put_tensor_with_tp`/`get_tensor_with_tp`（TP 分片权重）和 `save_tensor_to_safetensor`/`load_tensor_from_safetensor`（持久化）。
- 两侧用**同一个 metadata/master、不同 hostname 和 RDMA 网卡**各自 `setup()` 一个客户端——这是分布式 RL「训练集群与推理集群解耦」的标准形态。
- `put_tensor`/`get_tensor` 在 pybind11 层都是「持 GIL 提取/重建、释放 GIL 做传输」，张量在线格式为 `[TensorMetadata][data]`；并且 `put_tensor` 会**校验输入必须是 torch 张量**（示例用 `list[dict]` 是 illustrative-only，真实跑会失败）。
- `async_store.py` 用 `__getattr__` + `run_in_executor` 把同步方法动态包成 coroutine，配合 `asyncio.gather` 能在「多 rollout 并行产出 / 多 TP 分片并行拉取」处带来真实并发收益——前提是底层 C++ 已释放 GIL。

## 7. 下一步学习建议

- **想跑通端到端的 RL 数据流**：按第 5 节综合实践，把 dummy 示例里被 mock 的 `update_weights`、被错用的 `list[dict]` 补成真实张量 + TP 权重分发，亲手跑通一次闭环。
- **想深入权重/样本的副本与持久化**：读 [u5-l5 段与副本模型](u5-l5-segment-replica-model.md)，理解 `ReplicateConfig`（`replica_num`/`with_soft_pin`/`preferred_segments`）如何让权重多副本、`save_tensor_to_safetensor` 如何把 Store 里的张量落盘。
- **想理解异步传输的底层（不只是线程池包装）**：Transfer Engine 本身支持 CUDA 流触发的异步传输，见 [u2-l6 Python TE API](u2-l6-python-te-api.md) 和 [u3-l1 transport base/slice/batch](u3-l1-transport-base-slice-batch.md)。`async_store.py` 是「Python 层 asyncio 并发」，TE 的流触发是「硬件层异步」，两者正交可叠加。
- **想看 Store 在批量/并发下的工程实践**：精读 [scripts/test_async_store.py](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/test_async_store.py) 里 `test_03_tp_batch`、`test_07_put_get_into_with_tp`，它们给出了 `asyncio.gather` + `register_buffer` + 零拷贝 `get_into` 的组合范式，是真实 RL 权重分发的高性能写法。
- **想看 PyTorch 显存如何直接从 Store 池分配**：继续读 [u9-l2 PyTorch Allocator 集成](u9-l2-pytorch-allocator.md)，把「Store 传张量」和「Store 当显存分配器」两件事打通，能进一步省掉 `put_tensor` 时的 host 中转拷贝。
