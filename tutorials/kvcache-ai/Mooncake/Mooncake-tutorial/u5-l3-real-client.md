# RealClient：客户端架构与数据传输

## 1. 本讲目标

本讲是「Store 主题」的第三讲（依赖 u5-l1 总体架构）。在 u5-l1 里你已经知道：Store 把工作拆成**控制面（Master Service，管元数据）**和**数据面（Client，用 Transfer Engine 搬数据）**。本讲要把镜头推近，对准用户实际打交道的那个对象——**客户端**——讲清它内部到底由几层构成、每一层各自干什么，以及一次 `get`/`put` 的字节到底是怎么在网络上传过去的。

学完本讲你应该能够：

1. 画清 Mooncake Store 的**三层客户端模型**：`PyClient`（抽象接口）→ `RealClient`（真正的客户端实现，自带内存池与数据面编排）→ `Client`（数据面编排器）→ `MasterClient`（控制面 RPC 客户端），并说清 `DummyClient` 在这套模型里的位置与用途。
2. 说清 `RealClient` 如何调用 `MasterClient`（coro_rpc）与 Master 交互（`GetReplicaList` 等），以及如何调用 TransferEngine（TE）完成跨节点数据传输。
3. 掌握 put/get 的**零拷贝实现**（`put_from` / `get_into` / `register_buffer`）与**批量接口**（`batch_put_from` / `get_into_ranges`），理解为什么「注册过的内存 + 直接切片」能避免一次额外的 `memcpy`。
4. 能沿着一条完整的 `get` 调用链，从 `RealClient::get_into` 一路追到 `engine_.submitTransfer`，并解释零拷贝是如何避免额外内存拷贝的。

## 2. 前置知识

本讲默认你已经具备以下背景：

- **Store 总体架构（依赖 u5-l1）**：知道 Master Service（控制面）与 Client（数据面）的分工，知道一笔 `Put` 是「`PutStart` → TE 写入 → `PutEnd`」三段式。本讲会复用这条时序。
- **Transfer Engine 基础（依赖 u2-l6）**：TE 提供 `registerLocalMemory`（把一段本地内存登记进 TE，使其可被远端 RDMA/TCP 访问）、`submitTransfer`（提交一批 `TransferRequest`，含 READ/WRITE、源地址、目标段 id、偏移、长度）。本讲中「真正搬数据」的最后一公里都是 TE。
- **C++ 的 `tl::expected<T, ErrorCode>`**：Mooncake 用它做错误处理，相当于「要么是 `T`，要么是一个 `ErrorCode`」。本讲看到函数返回 `tl::expected<...>` 时，先理解「成功返回值，失败返回错误码」即可。
- **coro_rpc**：Mooncake 控制面与客户端之间用的同步语义 RPC 框架（基于 C++20 协程），客户端侧用 `send_request` 发起调用。

### 一个绕不开的易混淆点：到底有几个「Client」？

源码里名字带「Client」的类有四个，本讲必须把它们分清楚（否则读源码会晕）。先把结论摆出来，后面逐个展开：

| 类 | 头文件 | 角色 | 谁持有它 |
|---|---|---|---|
| `PyClient` | [pyclient.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/pyclient.h) | 纯抽象基类，定义对外的 `put/get/...` 虚接口 | 被 Python 绑定层以多态方式使用 |
| `RealClient` | real_client.h | `PyClient` 的**主力实现**：自带内存池、自挂载段、自起 RPC 服务，是「真正的客户端」 | 用户/推理引擎直接创建 |
| `DummyClient` | dummy_client.h | `PyClient` 的**轻量代理实现**：自己不搬数据，通过 RPC 转发给某个 `RealClient` | 同机多进程场景下的「子进程客户端」 |
| `Client` | client_service.h | `RealClient` 内部持有的**数据面编排器**，成员名 `client_` | `RealClient`（及 `PyClient` 基类） |
| `MasterClient` | master_client.h | `Client` 内部持有的**控制面 RPC 客户端**，成员名 `master_client_` | `Client` |

> 一句话记忆：**用户面对 `RealClient`/`DummyClient`（都继承自 `PyClient`）；`RealClient` 内部用一个 `Client` 做编排；`Client` 内部用一个 `MasterClient` 跟 Master 说话。** 数据搬运则由 `Client` 调 TransferEngine 完成。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲怎么用 |
|---|---|---|
| [mooncake-store/include/pyclient.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/pyclient.h) | `PyClient` 抽象基类、`ClientRequester`、共享内存/缓存辅助结构 | 讲三层模型的「共同接口」与 zero-copy 缓存契约 |
| [mooncake-store/include/real_client.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/real_client.h) | `RealClient` 类声明 | 讲 RealClient 的对外 API、内存池、`*_internal`/`*_dummy_helper` 两套方法 |
| [mooncake-store/src/real_client.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp) | `RealClient` 实现（5724 行，本讲只读关键片段） | 讲 setup、put、put_from、get_into、register_buffer、dummy shm 映射 |
| [mooncake-store/include/client_service.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/client_service.h) | `Client` 数据面编排器、`QueryResult` | 讲 `Get`/`Query`/`Put` 编排与三大成员 |
| [mooncake-store/src/client_service.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp) | `Client` 实现 | 讲 `Query`→`GetReplicaList`、`Get`→`TransferRead`、`TransferData` |
| [mooncake-store/include/master_client.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_client.h) | `MasterClient` 类声明 | 讲控制面 RPC 接口（GetReplicaList/PutStart/PutEnd…）与 client pool |
| [mooncake-store/src/master_client.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_client.cpp) | `MasterClient` 实现 | 讲 `invoke_rpc` 模板如何用 coro_rpc `send_request` |
| [mooncake-store/include/transfer_task.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/transfer_task.h) / `src/transfer_task.cpp` | `TransferSubmitter` | 讲「策略选择」与 `submitTransfer` 这最后一公里 |
| [mooncake-store/include/dummy_client.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/dummy_client.h) / `src/dummy_client.cpp` | `DummyClient` 实现 | 讲 DummyClient 为何「不搬数据也能工作」 |

---

## 4. 核心概念与源码讲解

### 4.1 三层客户端模型：PyClient / RealClient / DummyClient

#### 4.1.1 概念说明

Mooncake Store 的客户端被刻意设计成「一个抽象接口 + 两个实现」：

- **`PyClient`**（[pyclient.h:211](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/pyclient.h#L211)）：纯虚抽象基类，定义了所有对外 API（`put`、`put_from`、`get_into`、`get_buffer`、`register_buffer`、`batch_*`、`isExist`、`remove`……）。Python 绑定层只认 `PyClient*`，不关心背后是哪种实现。它还以**公有成员**的方式直接持有三个核心资源（[pyclient.h:371-374](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/pyclient.h#L371-L374)）：数据面编排器 `client_`、远端 RPC 请求器 `client_requester_`、文件存储 `file_storage_`、本地内存池 `client_buffer_allocator_`。这样 `RealClient` 和 `DummyClient` 都能复用它们。

- **`RealClient`**（[real_client.h:68](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/real_client.h#L68)）：主力实现。它会**真正初始化一个 `Client`**（也就是数据面编排器）、**挂载自己的内存段**到 Master、**创建本地内存池**（`ClientBufferAllocator`），因此它既能对外提供 `put/get`，又能作为「存储节点」把内存贡献给集群。它甚至内置一个 HTTP server 做健康检查、一个 IPC server 接收 Dummy 传来的共享内存 fd。

- **`DummyClient`**（[dummy_client.h:17](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/dummy_client.h#L17)）：极简实现。它**不初始化 `Client`、不挂载段、不直接调 TE**，而是把每一次 `put/get` 都通过 coro_rpc **转发给某个已经存在的 `RealClient`** 去执行。

为什么要分出 `DummyClient`？典型的 LLM 推理场景是「一台机器上跑很多个推理 worker 进程」。如果每个 worker 都各自初始化一个完整的 `RealClient`（各自挂载内存段、各自维护 TE、各自和 Master 建连），会有大量重复开销和资源竞争。Mooncake 的做法是：**机器上只起一个 `RealClient`（常驻、贡献内存、跑 TE），其余 worker 各自起一个 `DummyClient`**。Dummy 通过共享内存（shm）与 Real 共享数据缓冲区，通过 RPC 把「搬数据」的活儿外包给 Real。这样既省资源，又能复用 Real 那一套零拷贝路径。

两个实现还互斥：`RealClient` 的 `setup_dummy` 直接返回 -1（[real_client.h:89-94](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/real_client.h#L89-L94)），`DummyClient` 的 `setup_real` 同样返回 -1（[dummy_client.h:24-36](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/dummy_client.h#L24-L36)）。一个进程里你只能二选一。

#### 4.1.2 核心流程

三者的协作关系如下：

```text
        Python / 推理引擎
              │  (调用 PyClient 虚接口: put/get/...)
              ▼
   ┌──────────────────────┐         ┌──────────────────────┐
   │      RealClient      │  RPC     │     DummyClient      │
   │  (PyClient 实现)     │◀─────────│  (PyClient 实现)     │
   │                      │  转发    │  自己不搬数据         │
   │  持有: client_       │         │  通过 shm + RPC 复用  │
   │       buffer pool    │         │  Real 的内存与 TE     │
   │       master_client_ │         └──────────────────────┘
   └──────────┬───────────┘
              │  (数据面编排: Query/Get/Put + TE 提交)
              ▼
        Client  ──► TransferEngine(submitTransfer)  数据面
              │
              │  (控制面 RPC: GetReplicaList/PutStart/PutEnd)
              ▼
        MasterClient ──coro_rpc──► Master Service   控制面
```

关键点：`RealClient` 同时承担「客户端」和「存储节点」两种身份——它既发请求，又把自己的内存段挂到 Master 上供别人读。`DummyClient` 则纯粹是「客户端」，所有重活都外包。

#### 4.1.3 源码精读

先看 `PyClient` 的接口形态（节选）与它以公有成员持有的资源：

[pyclient.h:240-248](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/pyclient.h#L240-L248) 定义了 `put` 与 `register_buffer` 两个纯虚函数；[pyclient.h:247-248](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/pyclient.h#L247-L248) 是 zero-copy 读的核心接口 `get_into`。公有成员 `client_`（[pyclient.h:371](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/pyclient.h#L371)）就是数据面编排器 `Client` 的指针——`RealClient` 与 `DummyClient` 都继承它，但只有 `RealClient` 会真正 `new` 出来填上。

再看 `RealClient` 的构造与工厂方法。`create()` 是推荐的创建方式，它会把实例注册到全局 `ResourceTracker`，以便异常退出（SIGINT/SIGTERM）时能做清理（[real_client.cpp:608-612](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L608-L612)）：

```cpp
std::shared_ptr<RealClient> RealClient::create() {
    auto sp = std::shared_ptr<RealClient>(new RealClient());
    ResourceTracker::getInstance().registerInstance(sp);
    return sp;
}
```

`setup_real` 是用户最常用的初始化入口（[real_client.h:76-87](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/real_client.h#L76-L87)），参数包括本机 hostname、metadata server、global_segment_size（贡献给集群的内存）、local_buffer_size（本地缓冲池）、protocol（tcp/rdma）、master 地址等。它最终委托给 `setup_internal`（[real_client.cpp:628](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L628)），后者做三件关键的事：

1. **创建数据面编排器**：调用 `Client::Create(...)`（[real_client.cpp:678-686](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L678-L686)），注意它把 `{{"client_mode", "real"}}` 作为 label 传入，用于区分 Real/Dummy。若用户没指定端口，还会在一段端口区间内自动绑定并重试（[real_client.cpp:699-732](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L699-L732)），解决「端口被占导致元数据注册冲突」的问题。
2. **创建本地内存池并注册到 TE**：`ClientBufferAllocator::create(...)` 建 buffer pool，再 `client_->RegisterLocalMemory(...)` 把这段内存登记进 TE（[real_client.cpp:750-763](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L750-L763)）。这就是本地 zero-copy 缓冲区的来源。
3. **把 global segment 挂到 Master**：循环 `MountSegment`，若单段超过 `max_mr_size` 会切分成多段（[real_client.cpp:825-884](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L825-L884)）。这就是 Real 把自己变成「存储节点」的那一步。

`DummyClient` 的 `setup_dummy`（[dummy_client.cpp:452](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/dummy_client.cpp#L452)）则完全不同：它先 `connect(server_address)` 连上某个 RealClient 的 RPC 地址（[dummy_client.cpp:462](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/dummy_client.cpp#L462)），再用 `ShmHelper` 分配本地共享内存、通过 IPC 把 fd 传给 Real 让 Real `mmap` 并注册（详见 4.5 节）。Dummy 自身不挂段、不调 TE。

#### 4.1.4 代码实践

- **实践目标**：在不运行的前提下，靠源码确认「`RealClient` 和 `DummyClient` 是互斥的两种实现」，并定位 `RealClient` 的三类关键资源。
- **操作步骤**：
  1. 打开 [real_client.h:89-94](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/real_client.h#L89-L94)，确认 `RealClient::setup_dummy` 直接 `return -1`。
  2. 打开 [dummy_client.h:24-36](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/dummy_client.h#L24-L36)，确认 `DummyClient::setup_real` 直接 `return -1`。
  3. 打开 [pyclient.h:371-374](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/pyclient.h#L371-L374)，记录 `PyClient` 持有的四个公有成员。
- **需要观察的现象**：两个类对「对方的 setup 函数」都返回 -1；而 `PyClient` 的公有成员里 `client_` 默认是 `nullptr`，只有 `RealClient` 在 `setup_internal` 里会真正给它赋值。
- **预期结果**：你能用一句话回答「为什么一个进程不能既是 Real 又是 Dummy」——因为它们各自只实现了 `setup_real` 或 `setup_dummy` 中的一个，另一个被刻意禁用。
- 由于本实践为纯源码阅读型，**待本地验证**指的是：如果你本地能编译运行，可写一个最小程序分别 `setup_real` 成功、`setup_dummy` 返回 -1，验证上述结论。

#### 4.1.5 小练习与答案

- **练习 1**：如果一台机器上跑了 8 个推理进程，应该起几个 `RealClient`、几个 `DummyClient`？
  - **参考答案**：通常起 1 个 `RealClient`（常驻、贡献内存、维护 TE 与 Master 连接），其余 7 个进程各起 1 个 `DummyClient`，通过 shm + RPC 复用 Real 的资源。这样可以避免 8 套 TE/段/连接的重复开销。
- **练习 2**：`RealClient` 的 `create()` 为什么要注册到 `ResourceTracker`？
  - **参考答案**：为了让进程在收到 SIGINT/SIGTERM 或 `atexit` 时，能自动对这些实例调用 `tearDownAll`，释放挂载的段、注销 TE 内存、关闭 RPC server，避免资源泄漏（见 [real_client.cpp:459-520](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L459-L520)）。

---

### 4.2 MasterClient：控制面 RPC 客户端

#### 4.2.1 概念说明

`MasterClient`（[master_client.h:53](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_client.h#L53)）是 `Client`（数据面编排器）内部用来**和 Master Service 说控制面 RPC** 的客户端。它把 Master 暴露的每一个控制面接口（`GetReplicaList`、`PutStart`、`PutEnd`、`Remove`、`MountSegment`、`Ping`……）都封装成一个同名的 C++ 方法。数据面自己绝不直接碰 Master 的元数据存储，一律走 `MasterClient`。

它的底层是 coro_rpc 的**连接池** `coro_io::client_pools<coro_rpc::coro_rpc_client>`（[master_client.h:707-708](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_client.h#L707-L708)）。连接池负责复用 TCP/RDMA 连接、做超时与重试，这样上层每个 `MasterClient::XXX` 方法都只是一次「向池子借连接 → `send_request` → 还连接」的薄封装。

#### 4.2.2 核心流程

以一次 get 为例，控制面的全部交互其实只有**一次 RPC**：

```text
Client::Query(key)
   └─► master_client_.GetReplicaList(key, tenant_id)
          └─► invoke_rpc<&WrappedMasterService::GetReplicaList, GetReplicaListResponse>(key, tenant_id)
                 └─► pool->send_request( client.send_request<GetReplicaList>(key, tenant_id) )   # coro_rpc
                        └─► [网络] ──► MasterService::GetReplicaList ──► [网络] ──► GetReplicaListResponse{replicas, lease_ttl_ms}
```

返回的 `GetReplicaListResponse` 里有：一份**副本描述符列表**（每个 `Replica::Descriptor` 告诉你「这份数据在哪个 segment、什么地址、多大、memory/disk/local_disk 哪种类型」）和一个**租约** `lease_ttl_ms`（在这段时间内这份元数据有效，过期需重新查）。`Client::Query` 把它包成 `QueryResult`（[client_service.h:39-58](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/client_service.h#L39-L58)），并记录租约到期时间点。

#### 4.2.3 源码精读

`Client::Query` 直接调 `master_client_.GetReplicaList`，并把返回的 `lease_ttl_ms` 转成绝对到期时间点（[client_service.cpp:1022-1033](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp#L1022-L1033)）：

```cpp
tl::expected<QueryResult, ErrorCode> Client::Query(const std::string& object_key) {
    auto start_time = std::chrono::steady_clock::now();
    auto result = master_client_.GetReplicaList(object_key);
    if (!result) return tl::unexpected(result.error());
    return QueryResult(std::move(result.value().replicas),
                       start_time + std::chrono::milliseconds(result.value().lease_ttl_ms));
}
```

`MasterClient::GetReplicaList` 则是一行 `invoke_rpc`（[master_client.cpp:529-538](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_client.cpp#L529-L538)）。真正干活的是模板 `invoke_rpc`（[master_client.cpp:320-361](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_client.cpp#L320-L361)）：它从 `client_accessor_` 拿到连接池，用 `async_simple::coro::syncAwait` 把协程式的 `pool->send_request(...)` 同步化，再把 coro_rpc 的错误码翻译成 Mooncake 的 `ErrorCode`（超时→`RPC_TIMEOUT`，其他失败→`RPC_FAIL`）：

```cpp
auto pool = client_accessor_.GetClientPool();
// ...
return async_simple::coro::syncAwait(
    [&]() -> async_simple::coro::Lazy<tl::expected<ReturnType, ErrorCode>> {
        auto ret = co_await pool->send_request(
            [&](coro_io::client_reuse_hint, coro_rpc::coro_rpc_client& client) {
                return client.send_request<ServiceMethod>(std::forward<Args>(args)...);
            });
        // ... 错误翻译、指标记录 ...
        co_return result->result();
    }());
```

注意构造函数里对连接池的两个调优（[master_client.h:60-91](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_client.h#L60-L91)）：`host_alive_detect_duration = 0` 关掉了存活探测（防止 HA 切主后旧连接一直打无效地址刷日志，PR #1642）；并支持用环境变量 `MC_RPC_TIMEOUT_MS` / `MC_RPC_CONNECT_TIMEOUT_MS` 覆盖请求与连接超时。`MC_RPC_PROTOCOL=rdma` 时还会把 socket 配置换成 RDMA。

#### 4.2.4 代码实践

- **实践目标**：确认「数据面对 Master 的每一次访问都只通过 `MasterClient` 这一条路」，并理解 RPC 的可调参数。
- **操作步骤**：
  1. 在 [client_service.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp) 中搜索 `master_client_.`，统计 `Client` 一共通过它调用了哪些控制面方法（应能看到 `GetReplicaList`、`BatchGetReplicaList`、`PutStart`、`PutEnd`、`PutRevoke`、`Remove`、`MountSegment`、`Ping` 等）。
  2. 阅读构造函数 [master_client.h:55-91](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_client.h#L55-L91)，找到控制请求超时的环境变量名。
- **需要观察的现象**：`Client` 内部没有任何直接访问 etcd / Redis / 元数据存储的代码，全部经由 `master_client_`。
- **预期结果**：你得出结论「Master 是 Store 里唯一的元数据真相来源，客户端只能通过 `MasterClient` 的 RPC 去问它」。
- 超时类参数的真实效果**待本地验证**（可设 `MC_RPC_TIMEOUT_MS=1000` 观察慢请求是否更快报错）。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `invoke_rpc` 要用 `syncAwait` 把协程同步化，而不是直接返回协程？
  - **参考答案**：上层 `Client` / `RealClient` 的 API 都是普通同步函数（返回 `tl::expected`），不是协程。`syncAwait` 让上层无需改成协程就能复用 coro_rpc 的异步 IO，相当于「内部异步、对外同步」。
- **练习 2**：`GetReplicaListResponse` 里的 `lease_ttl_ms` 有什么用？
  - **参考答案**：它给客户端一个「元数据有效期」。在这段时间内，返回的副本列表是可信的，客户端可缓存复用（如 `batch_query` 的 `QueryResultCache`）；过期后若还要读，必须重新 `Query`，否则会拿到 `LEASE_EXPIRED`（见 [client_service.cpp:1127-1131](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp#L1127-L1131)）。租约是 Master 做并发安全（避免读到正在被迁移/删除的副本）的关键机制。

---

### 4.3 RealClient 的 put 与零拷贝

#### 4.3.1 概念说明

`RealClient` 提供两套写入 API，理解它们的区别是掌握「零拷贝」的关键：

- **`put(key, value)`**：value 是一段用户给的字节（`std::span<const char>`）。由于这块内存**不是** Store 管理的注册内存，RealClient 必须先从自己的 buffer pool 分配一块、把数据 `memcpy` 进去，再交给数据面。**有一次额外拷贝。**
- **`put_from(key, buffer, size)`**（[real_client.h:180-181](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/real_client.h#L180-L181)）：buffer 是一段**已经用 `register_buffer` 注册过、或位于 setup 时本地缓冲池内**的内存。RealClient 直接把它切成若干 `Slice`，交给数据面，**不做任何 memcpy**。这就是零拷贝写入。

「零拷贝」在这里的精确含义是：**用户数据所在的内存，就是 TE 最终要用 RDMA/TCP 直接搬走的那块内存**，中间没有一次进程内的字节拷贝。要做到这一点，这块内存必须满足两个条件：(1) 被 TE 注册过（`RegisterLocalMemory`），远端才能 RDMA 访问；(2) 地址、长度对 TE 已知（体现在 `Slice` 里）。

#### 4.3.2 核心流程

两套写入路径对比：

```text
put(key, value):                      put_from(key, buffer, size):   [buffer 已注册]
  ┌──────────────────┐                  ┌──────────────────┐
  │ user value span  │                  │ user buffer      │ ← 已是 TE 注册内存
  └────────┬─────────┘                  └────────┬─────────┘
           │ memcpy (一次拷贝)                    │ 直接切片，无拷贝
           ▼                                      ▼
   buffer_pool.allocate(size)            split buffer into Slices
           │                                      │
           └──────────► split_into_slices ◄───────┘
                          │
                          ▼
            client_->Put(key, slices, config)
                ├─ master_client_.PutStart(...)   # 控制面：分配目标副本
                ├─ TransferWrite(replica, slices) # 数据面：TE submitTransfer(WRITE)
                └─ master_client_.PutEnd(...)     # 控制面：提交可见
```

无论哪条路径，最终都汇入 `Client::Put`（[client_service.h:202-204](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/client_service.h#L202-L204)），它负责「问 Master 要目标位置（`PutStart`）→ 用 TE 写过去（`TransferWrite`）→ 告诉 Master 写完了（`PutEnd`）」这三段编排（u5-l1 已详述）。

#### 4.3.3 源码精读

**带拷贝的 `put_internal`**（[real_client.cpp:1672-1705](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L1672-L1705)）：先 `allocate`、再 `memcpy`、再 `split_into_slices`：

```cpp
auto alloc_result = client_buffer_allocator->allocate(value.size_bytes());
// ...
auto &buffer_handle = *alloc_result;
memcpy(buffer_handle.ptr(), value.data(), value.size_bytes());   // ← 这就是那次额外拷贝
std::vector<Slice> slices = split_into_slices(buffer_handle);
auto put_result = client_->Put(key, slices, config);
```

**零拷贝的 `put_from_internal`**（[real_client.cpp:3652-3688](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3652-L3688)）：直接在用户 buffer 上切片，没有 `memcpy`：

```cpp
// NOTE: The buffer address must resolve to Store-managed registered
// memory for zero-copy RDMA operations to work correctly
// ...
std::vector<mooncake::Slice> slices;
uint64_t offset = 0;
while (offset < size) {
    auto chunk_size = std::min(size - offset, kMaxSliceSize);     // 每 slice 不超过 kMaxSliceSize
    void *chunk_ptr = static_cast<char *>(buffer) + offset;
    slices.emplace_back(Slice{chunk_ptr, chunk_size});            // ← 只记指针和长度，不拷数据
    offset += chunk_size;
}
auto put_result = client_->Put(key, slices, config);
```

注意注释里反复强调的契约：**buffer 必须落在 Store 管理的注册内存里**，否则零拷贝 RDMA 会失败。那「Store 管理的注册内存」从哪来？两个来源：(1) setup 时建的本地 buffer pool（`local_buffer_region_`）；(2) 用户显式 `register_buffer` 注册的内存。`kMaxSliceSize` 定义在 [types.h:442-443](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/types.h#L442-L443)，约为一个 cachelib Slab 大小减 16 字节，切片就是为了让单个传输请求不超过底层分配粒度上限。

**`register_buffer_internal`**（[real_client.cpp:3030-3046](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3030-L3046)）：把任意用户内存变成「TE 注册内存」，并登记到 `registered_buffer_sizes_` 以便后续 zero-copy 读写时校验地址归属：

```cpp
auto result = client_->RegisterLocalMemory(buffer, size, kWildcardLocation, false, true);
// ...
registered_buffer_sizes_[buffer] = size;
```

> 这里 `RegisterLocalMemory(..., remote_accessible=false, update_metadata=true)`：`remote_accessible=false` 表示这块内存只是本地写/读用，不需要被远端 RDMA 读（写端的 buffer 由写者拥有）；`update_metadata=true` 会把这段内存登记进 TE 的元数据，让 TE 知道它的存在。

#### 4.3.4 代码实践

- **实践目标**：亲手对比 `put` 与 `put_from` 的源码差异，确认零拷贝省掉了哪一次 `memcpy`。
- **操作步骤**：
  1. 打开 [real_client.cpp:1672-1705](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L1672-L1705)（`put_internal`），找到第 1695 行的 `memcpy`。
  2. 打开 [real_client.cpp:3652-3688](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3652-L3688)（`put_from_internal`），确认其中**没有** `memcpy`，只有构造 `Slice`。
  3. 打开 [types.h:442-443](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/types.h#L442-L443)，记录 `kMaxSliceSize` 的来源。
- **需要观察的现象**：两条路径的代码结构几乎相同（都是「切 Slice → `client_->Put`」），唯一差别就是 `put_internal` 多了一次从用户内存到 buffer pool 的 `memcpy`。
- **预期结果**：你能画出本节 4.3.2 的对比图，并指出「零拷贝省掉的就是那次 `memcpy`」。
- 实际带宽收益**待本地验证**：用大块（如 100MB）分别 `put` 和 `put_from`，对比耗时，应能看到 `put_from` 明显更快（少一次大块内存拷贝）。

#### 4.3.5 小练习与答案

- **练习 1**：如果用户对一个**没有注册过**的普通 `malloc` 出来的 buffer 直接调用 `put_from`，会发生什么？
  - **参考答案**：`put_from` 本身不会立刻报错（它只是切片），但当 `Client::Put` → `TransferWrite` → TE `submitTransfer(WRITE)` 执行时，TE 发现源地址不在任何已注册内存区域内，RDMA 注册/访问会失败，导致传输错误。所以契约要求先 `register_buffer`。`get_into`/`get_into_ranges` 在入口用 `resolve_writable_buffer_region` 校验地址归属（[real_client.cpp:3075-3107](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3075-L3107)），不满足会直接返回错误。
- **练习 2**：为什么要把一个大 buffer 切成多个不超过 `kMaxSliceSize` 的 `Slice`，而不是一整块？
  - **参考答案**：底层 cachelib 的分配粒度（Slab）有上限，`kMaxSliceSize` 就是 `Slab::kSize - 16`；超过这个上限的单段在分配/注册时会出问题。切片还让 TE 可以把一个大传输拆成多个并发的小传输请求，提升带宽利用率。

---

### 4.4 RealClient 的 get 调用链与 TE submitTransfer

> 本节是本讲的**核心**，直接对应综合实践任务：追踪从 `GetReplicaList` 到 `submitTransfer` 再到数据落地的完整调用链，并解释零拷贝。

#### 4.4.1 概念说明

`RealClient` 的读接口也有「带拷贝」和「零拷贝」两套：

- **`get_buffer(key)`**（[real_client.h:273](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/real_client.h#L273)）：返回一个 `BufferHandle`，数据落在 RealClient 自己的 buffer pool 里。用户拿到的是 Store 管理的内存，需要再拷一次到自己的目标缓冲区（如果你要放进 GPU 之类）。
- **`get_into(key, buffer, size)`**（[real_client.h:127](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/real_client.h#L127)）：把数据**直接读进用户给定的已注册 buffer**。这是零拷贝读：TE 把远端字节直接写到 `buffer` 里，中间无额外拷贝（除非源副本是磁盘类型，需要经临时 CPU 缓冲中转——见下）。

无论哪种，读路径的骨架都是「**问 Master 要副本列表 → 选最优副本 → 让 TE 把那块数据搬进目标 buffer**」。

副本选择策略（`SelectBestReplica`，[real_client.cpp:290-326](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L290-L326)）优先级是：**本地 MEMORY > 任意 MEMORY > 本地 NOF_SSD > LOCAL_DISK > DISK**。优先本地是因为本地内存可以用进程内 `memcpy`（最快），跨节点才走 TE 的 RDMA/TCP。

#### 4.4.2 核心流程

一次 `get_into` 的完整调用链（控制面用 `→M` 标注，数据面用 `→D` 标注）：

```text
RealClient::get_into(key, buffer, size)
  → execute_timed_operation( get_into_range_internal(key, buffer, 0,0,size, size_is_buffer_capacity=true) )
       │
       ├─ resolve_ranged_read_metadata(key)
       │     └─ client_->Query(key)
       │          └─ master_client_.GetReplicaList(key)   →M  控制面 RPC，拿副本列表 + 租约
       │     └─ build_ranged_read_metadata_from_query_result(...)
       │          └─ SelectBestReplica(replicas, local_endpoints)   # 选最优副本
       │
       └─ execute_ranged_read(key, buffer, ..., metadata)
              │  (源副本是 MEMORY 且全量读时:)
              └─ client_->Get(key, filtered_qr, slices)
                   └─ FindFirstCompleteReplica(...)
                   └─ TransferRead(replica, slices)
                        └─ TransferData(replica, slices, READ)
                             └─ transfer_submitter_->submit(replica, slices, READ)   →D  策略选择
                                  ├─ selectStrategy: 本地? → LOCAL_MEMCPY
                                  │                 否则   → TRANSFER_ENGINE
                                  ├─ submitMemcpyOperation(...)        # 进程内 memcpy(目标buffer ← 源地址)
                                  └─ submitTransferEngineOperation(...)
                                       └─ submitTransfer(requests)
                                            └─ engine_.submitTransfer(batch_id, requests)   →D  TE 真正搬数据
                                                 └─ future->get()   # 等待传输完成，数据已落在 buffer
```

**零拷贝体现在哪里**：在 MEMORY 全量读路径里（[real_client.cpp:3187-3198](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3187-L3198)），目标 `slices` 是直接用**用户传入的 buffer 地址**构造的（`allocateSlices(slices, replica, buffer + dst_offset)`）。TE 的 `submitTransfer(READ)` 会把远端 segment 上的字节**直接 DMA 写到这个 buffer 地址**——既不经过 RealClient 的 buffer pool，也没有任何中间 `memcpy`（本地副本情形是直接 `memcpy` 进 buffer，也只有一次拷贝，且就是「落到目标」那一次，没有多余的中转）。

> 例外：若源副本是 **DISK**（本地文件）类型，文件 IO 无法直接写 GPU/任意地址，需要先读进临时 CPU buffer 再 `scatter` 到目标（[real_client.cpp:3158-3185](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3158-L3185)）；**LOCAL_DISK**（远端节点 SSD）则走 `batch_get_into_offload_object_internal` 这条 offload RPC 路径（[real_client.cpp:3145-3156](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3145-L3156)）。本讲聚焦最常见的 MEMORY 副本路径。

#### 4.4.3 源码精读

**(1) 入口 `get_into`**（[real_client.cpp:3301-3314](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3301-L3314)）：把「全量读」特化为一次 range read（`dst_offset=src_offset=0`，`size_is_buffer_capacity=true` 表示 size 是缓冲区容量，实际读全部对象）：

```cpp
int64_t RealClient::get_into(const std::string &key, void *buffer, size_t size) {
    auto result = execute_timed_operation<tl::expected<int64_t, ErrorCode>>(
        [&]() { return get_into_range_internal(key, buffer, 0, 0, size, true); },
        ...);   // 回调里调 ObserveTransferOperation 记录读延迟指标
    return to_py_ret(result);
}
```

**(2) `get_into_range_internal`**（[real_client.cpp:3283-3299](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3283-L3299)）：先解析元数据，再执行读：

```cpp
auto metadata_result = resolve_ranged_read_metadata(key);   // →M 查 Master
if (!metadata_result) return tl::unexpected(metadata_result.error());
return execute_ranged_read(key, buffer, dst_offset, src_offset, size,
                           metadata_result.value(), size_is_buffer_capacity);
```

**(3) `resolve_ranged_read_metadata` + `build_ranged_read_metadata_from_query_result`**（[real_client.cpp:3109-3118](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3109-L3118) 与 [3496-3527](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3496-L3527)）：这一步完成了**控制面查询 + 副本选择**，产出 `RangedReadMetadata{query_result, replica, total_size}`：

```cpp
// resolve_ranged_read_metadata:
return build_ranged_read_metadata_from_query_result(key, client_->Query(key));

// build_ranged_read_metadata_from_query_result:
auto local_endpoints = client_->GetLocalEndpoints();
const auto *best_replica = SelectBestReplica(replica_list, local_endpoints);
// ...
return RangedReadMetadata{.query_result = ..., .replica = *best_replica,
                          .total_size = calculate_total_size(replica)};
```

**(4) `Client::Get`**（[client_service.cpp:1081-1139](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp#L1081-L1139)）：这是数据面编排的核心。`FindFirstCompleteReplica` 选一个完整副本，可选地走本地 hot cache，然后调 `TransferRead` 把数据搬进 slices：

```cpp
Replica::Descriptor replica;
ErrorCode err = FindFirstCompleteReplica(query_result.replicas, replica);
// ... hot cache 重定向 ...
err = TransferRead(replica, slices);
// ... 释放 hot cache、频率准入、租约过期检查 ...
```

**(5) `TransferRead` → `TransferData` → `TransferSubmitter::submit`**：`Client::TransferData`（[client_service.cpp:3410-3440](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp#L3410-L3440)）把请求交给 `transfer_submitter_`，拿到一个 `TransferFuture`，再 `future->get()` 等待完成。`TransferSubmitter::submit`（[transfer_task.cpp:950-1005](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/transfer_task.cpp#L950-L1005)）根据副本类型分流：

```cpp
if (replica.is_memory_replica()) {
    if (op_code == READ) future = submitMemoryReadOperation(handle, slices, 0);
    else { strategy = selectStrategy(handle, slices); /* LOCAL_MEMCPY 或 TRANSFER_ENGINE */ }
} else if (replica.is_nof_replica()) { /* SPDK */ }
else { future = submitFileReadOperation(...); /* DISK */ }
```

**(6) 策略选择 `selectStrategy`**（[transfer_task.cpp:1317-1333](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/transfer_task.cpp#L1317-L1333)）：若 memcpy 没被禁用且是本进程的段（`isLocalTransfer`），就用 `LOCAL_MEMCPY`（直接进程内 memcpy，最快），否则用 `TRANSFER_ENGINE`（走 TE 的 RDMA/TCP）。注意 `isSameProcessEndpoint`（[transfer_task.cpp:1364-1388](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/transfer_task.cpp#L1364-L1388)）强调「同主机不同进程」不能 memcpy（虚拟地址空间不同，会 segfault），必须端点完全相同。

**(7) 最后一公里 `submitTransfer`**（[transfer_task.cpp:1132-1165](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/transfer_task.cpp#L1132-L1165)）：分配 batch id，调用 `engine_.submitTransfer(batch_id, requests)` 把这批 `TransferRequest` 真正提交给 TE，数据由此刻开始跨节点搬运；之后用 `TransferEngineOperationState` 轮询完成状态，`future->get()` 返回时数据已落在用户 buffer：

```cpp
BatchID batch_id = engine_.allocateBatchID(batch_size);
Status s = engine_.submitTransfer(batch_id, requests);   // ← TE 真正搬数据
// ...
auto state = std::make_shared<TransferEngineOperationState>(engine_, batch_id, batch_size);
return TransferFuture(state);
```

而 `TransferRequest` 的构造（在 `submitTransferEngineOperation`，[transfer_task.cpp:1167-1204](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/transfer_task.cpp#L1167-L1204)）清楚地展示了「源 = 用户 buffer 切片，目标 = 远端段地址」的对应关系：

```cpp
request.opcode = op_code;                                  // READ
request.source = static_cast<char*>(slice.ptr);            // 用户 buffer（本地）
request.target_id = seg;                                   // 远端段 handle
request.target_offset = base_address + offset;             // 远端段内偏移
request.length = slice.size;
```

对 READ 而言，TE 会把 `target_id` 段上 `[target_offset, target_offset+length)` 的字节搬进本地 `source`（即用户 buffer）——这就是零拷贝落地的瞬间。

#### 4.4.4 代码实践（综合实践任务的核心）

- **实践目标**：亲手沿着 `real_client.cpp` 里一次 `get_into` 的实现，从 `GetReplicaList` 追到 `submitTransfer`，并说清零拷贝避免了哪次拷贝。
- **操作步骤**（建议按顺序打开每个链接，在每个函数入口打个断点式的心智标记）：
  1. 入口：[real_client.cpp:3301](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3301) `get_into` → 调 `get_into_range_internal`。
  2. 控制面查询：[real_client.cpp:3109](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3109) `resolve_ranged_read_metadata` → `client_->Query` → [client_service.cpp:1022](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp#L1022) `master_client_.GetReplicaList`（这是控制面 RPC 的起点）。
  3. 副本选择：[real_client.cpp:3496](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3496) `build_ranged_read_metadata_from_query_result` → [real_client.cpp:290](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L290) `SelectBestReplica`。
  4. 数据面执行：[real_client.cpp:3120](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3120) `execute_ranged_read`（MEMORY 全量读分支在 [3187-3198](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3187-L3198)）→ `client_->Get` → [client_service.cpp:1081](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp#L1081) → `TransferRead`。
  5. 提交 TE：[client_service.cpp:3410](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp#L3410) `TransferData` → [transfer_task.cpp:950](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/transfer_task.cpp#L950) `submit` → [transfer_task.cpp:1167](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/transfer_task.cpp#L1167) `submitTransferEngineOperation` → [transfer_task.cpp:1132](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/transfer_task.cpp#L1132) `submitTransfer` → `engine_.submitTransfer`。
- **需要观察的现象**：
  - 控制面只出现一次（`GetReplicaList`），且发生在数据面之前；
  - 数据面传给 TE 的 `request.source` 就是用户传入的 `buffer` 地址（在 `execute_ranged_read` 里 `slices` 直接基于 `buffer + dst_offset` 构造，[real_client.cpp:3187-3189](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3187-L3189)）；
  - 全程没有把数据先拷进 RealClient 自己的 buffer pool 再拷给用户的那次「中转拷贝」。
- **预期结果**：你能口头复述这条链，并回答「零拷贝避免了什么」——避免了「远端字节 → RealClient buffer pool → 用户 buffer」这种两次拷贝，改为 TE 直接 DMA 写入用户 buffer（MEMORY 副本情形），或在本地副本时仅一次 memcpy 直达用户 buffer。
- 由于需要真实 Master + TE 环境才能跑通，**待本地验证**；本实践以源码追踪为主。

#### 4.4.5 小练习与答案

- **练习 1**：`get_into` 读 MEMORY 副本时，为什么不需要像 `get_buffer` 那样先从 buffer pool 分配？
  - **参考答案**：因为 `get_into` 的目标 buffer 是用户传入的、已注册的内存，TE 可以直接把字节写进去；而 `get_buffer` 没有用户提供目标，只能先从 RealClient 的 buffer pool 分配一块作为落地缓冲（[real_client.cpp:2577-2584](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L2577-L2584)），所以它返回的是 `BufferHandle`（Store 管理的内存）。
- **练习 2**：`selectStrategy` 在什么条件下选 `LOCAL_MEMCPY`？为什么不能对「同主机不同进程」用 memcpy？
  - **参考答案**：当 memcpy 未被禁用（`memcpy_enabled_` 为真）且源段属于本进程（`isLocalTransfer` 为真，端点完全相同）时选 `LOCAL_MEMCPY`，直接进程内 `memcpy` 最快。同主机不同进程虽然 IP 相同，但虚拟地址空间不同，直接 memcpy 对方进程的地址会段错误，所以 `isSameProcessEndpoint` 要求 ip:port 完全一致（[transfer_task.cpp:1364-1388](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/transfer_task.cpp#L1364-L1388)）。
- **练习 3**：`get_into_range_internal` 里如果 `Query` 返回 `OBJECT_NOT_FOUND` 会怎样？
  - **参考答案**：`resolve_ranged_read_metadata` 会把这个错误透传（[real_client.cpp:3499-3503](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3499-L3503)），`get_into_range_internal` 对 `OBJECT_NOT_FOUND`/`REPLICA_IS_NOT_READY` 且 `src_offset==0` 时只打一条 VLOG(1) 静默日志（[real_client.cpp:3288-3293](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3288-L3293)），最终 `get_into` 返回负数错误码。这避免了「对象不存在」刷满 ERROR 日志。

---

### 4.5 批量接口与 DummyClient 的零拷贝转发

#### 4.5.1 概念说明

**批量接口**：为了摊薄「每次 get/put 都要单独做一次控制面 RPC + TE 提交」的固定开销，`RealClient` 提供了大量 `batch_*` 与多缓冲区接口：

- `batch_put_from`（[real_client.h:213-216](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/real_client.h#L213-L216)）：多个 key 一次性写入。
- `batch_get_into` / `batch_get_into_multi_buffers`：多个 key 一次性读入（可分别读到不同 buffer）。
- `get_into_ranges`（[real_client.h:129-135](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/real_client.h#L129-L135)）：最强大的「按片段批量读」——对每个 buffer、每个 key、每个 `[src_offset, size)` 片段做 ranged read，三维嵌套。它还支持一个可选的 `QueryResultCache`，让你先用 `batch_query` 批量拿到副本元数据，再在后续多次 ranged read 里**复用**，避免重复查 Master（[real_client.cpp:3383-3397](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3383-L3397)）。这对 LLM 解码阶段「读很多 KV cache 切片」的访问模式特别友好。

**DummyClient 的零拷贝转发**：Dummy 自己没有 TE，怎么做到零拷贝？答案是「**共享内存地址翻译**」。Real 和 Dummy 之间共享一段 shm（Dummy 通过 IPC 把 fd 传给 Real，Real `mmap` 后再 `RegisterLocalMemory` 注册进 TE，见 [real_client.cpp:2073-2162](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L2073-L2162) `map_shm_internal_with_device`）。Dummy 持有的是「dummy 虚拟地址」，Real 持有的是「real 地址」，两者之间有一个固定的偏移 `shm_addr_offset`（[real_client.h:806-807](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/real_client.h#L806-L807)）。当 Dummy 调 `get_into` 时，它把 dummy 地址通过 RPC 发给 Real，Real 用 `map_dummy_buffer_to_real` 翻译成 real 地址，然后照常走 4.4 节那条 TE 链——数据直接落进 shm（Dummy 和 Real 都映射了它），Dummy 因此零拷贝地拿到了数据。

#### 4.5.2 核心流程

Dummy 的 get 转发链：

```text
DummyClient::get_into(key, buffer, size)
  └─ invoke_rpc<&RealClient::get_into_range_shm_helper>(key, dummy_buffer_addr, 0,0,size, client_id_)   →RPC→ Real
        （在 Real 侧）
        RealClient::get_into_range_shm_helper(...)
          ├─ map_dummy_buffer_to_real(...)     # dummy 地址 → real 地址（用 shm 偏移）
          └─ get_into_range_internal(...)      # 复用 4.4 的整条链，TE 把数据写进 shm
        （回到 Dummy 侧）
        buffer 已在共享 shm 中，Dummy 直接读到 ← 零拷贝
```

#### 4.5.3 源码精读

Dummy 的 `get_into`（[dummy_client.cpp:1019-1036](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/dummy_client.cpp#L1019-L1036)）只做一件事：把 buffer 地址转成 `uint64_t`，发 RPC 给 Real 的 `get_into_range_shm_helper`：

```cpp
int64_t DummyClient::get_into(const std::string& key, void* buffer, size_t size) {
    uint64_t buf_addr = reinterpret_cast<uint64_t>(buffer);
    auto result = invoke_rpc<&RealClient::get_into_range_shm_helper,
                             tl::expected<int64_t, ErrorCode>>(
        key, buf_addr, 0, 0, size, client_id_);
    // ... 指标记录 ...
    return to_py_ret(*result);
}
```

Real 侧的地址翻译 `map_dummy_buffer_to_real`（[real_client.cpp:359-374](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L359-L374)）：先试上次命中的 shm（缓存局部性），再遍历所有 mapped shm，用 `shm_addr_offset` 把 dummy 地址换成 real 地址：

```cpp
bool RealClient::map_dummy_buffer_to_real(const ShmContext &shm_ctx,
                                          uint64_t dummy_addr, size_t buf_size,
                                          const MappedShm *&last_hit_shm,
                                          void *&out_real) const {
    if (last_hit_shm && map_dummy_range_in_shm(*last_hit_shm, ...)) return true;
    for (const auto &shm : shm_ctx.mapped_shms) {
        if (map_dummy_range_in_shm(shm, dummy_addr, 0, buf_size, out_real)) {
            last_hit_shm = &shm;   // 缓存命中，加速下次
            return true;
        }
    }
    return false;
}
```

底层的换算在 `map_dummy_range_in_shm`（[real_client.cpp:339-357](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L339-L357)）：`out_real = dummy_addr + shm.shm_addr_offset + offset`，其中 `shm_addr_offset = real_base - dummy_base`（[real_client.cpp:2128-2129](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L2128-L2129)）。这就是 Dummy/Real 共享内存零拷贝的数学本质。

至于 `get_into_ranges` 的元数据复用，看 [real_client.cpp:3486-3494](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3486-L3494) 的 `batch_query`（直接转发 `client_->BatchQuery`，即一次 `BatchGetReplicaList` RPC 拿回所有 key 的副本列表），以及 `get_into_ranges_internal` 用 `metadata_cache` 缓存它们（[real_client.cpp:3375-3416](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3375-L3416)），同一个 key 的多个片段只查一次 Master。

#### 4.5.4 代码实践

- **实践目标**：确认「批量接口能复用元数据、减少 RPC」，并理解 Dummy/Real 的地址翻译。
- **操作步骤**：
  1. 阅读 [real_client.cpp:3383-3397](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3383-L3397)，看 `get_into_ranges_internal` 如何把 `query_result_cache` 里的元数据填进 `metadata_cache`。
  2. 阅读 [real_client.cpp:3406-3416](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3406-L3416)，看对每个 key 是否「找不到才查 Master」（`metadata_cache.find` + `emplace`）。
  3. 阅读 [dummy_client.cpp:1019](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/dummy_client.cpp#L1019) 与 [real_client.cpp:359](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L359)，确认 dummy 地址通过 `shm_addr_offset` 翻译成 real 地址。
- **需要观察的现象**：同一 key 的多个 ranged 片段共享一份 `RangedReadMetadata`；Dummy 的 buffer 地址与 Real 内的真实地址差一个固定偏移。
- **预期结果**：你能解释「为什么 `batch_query` + `get_into_ranges(query_result_cache=...)` 比 N 次单独 `get_into` 更快」——前者只发 1 次批量 RPC 拿元数据，后者要发 N 次。
- 实际加速比**待本地验证**（批量读 1000 个切片时对比两种用法的延迟与 RPC 次数）。

#### 4.5.5 小练习与答案

- **练习 1**：Dummy 调 `get_into` 后，数据落在谁的地址空间里？Dummy 怎么读到？
  - **参考答案**：数据落在 Real 注册进 TE 的那段 shm 里。由于 Dummy 和 Real 都 `mmap` 了同一个 fd（共享物理页），Dummy 在自己的 dummy 地址上就能直接读到（通过 `shm_addr_offset` 与 Real 地址对应同一块物理内存）。这就是「Dummy 不搬数据也能零拷贝拿到数据」的原因。
- **练习 2**：`get_into_ranges` 的三维嵌套 `vector`（buffer → key → fragment）分别对应什么？
  - **参考答案**：最外层是多个目标 buffer；中间层是每个 buffer 里要读的多个 key；最内层是每个 key 的多个 `[dst_offset, src_offset, size]` 片段。这种结构直接服务于「把多个对象的多个片段分别拼装到多个预分配 buffer」的解码场景。

---

## 5. 综合实践

**任务：画出一次「Dummy → Real → Master → TE」的完整 get 时序，并标注零拷贝与控制面/数据面边界。**

请按以下要求完成（纯源码阅读型，不要求运行）：

1. **画出时序图**：参与方有 4 个——`DummyClient`（推理 worker 进程）、`RealClient`（同机常驻进程）、`Master Service`（控制面，可能在远端）、`TransferEngine`（数据面，跨节点）。在图上标注：
   - 哪一步是控制面 RPC（`BatchGetReplicaList` / `GetReplicaList`），用虚线箭头；
   - 哪一步是数据面传输（`submitTransfer`），用粗实线箭头；
   - 哪一步是 Dummy/Real 之间的 RPC（`get_into_range_shm_helper`）和 IPC（传 shm fd）；
   - 数据最终落在 shm 的哪个地址。
2. **解释零拷贝**：用你自己的话写两段——
   - 为什么 Dummy 端是零拷贝（提示：shm 共享物理页 + 地址翻译）；
   - 为什么 Real 端对 MEMORY 副本是零拷贝（提示：TE 直接 DMA 写入用户 buffer，参考 4.4.3 的 `TransferRequest` 构造）。
3. **指出一次优化机会**：如果 Dummy 要连续读同一个 key 的 10 个片段，应该用哪两个 API 组合来避免 10 次控制面 RPC？给出函数名与调用顺序（提示：`batch_query` 产出 `QueryResultCache` → 传给 `get_into_ranges` 的 `query_result_cache` 参数；参考 [real_client.cpp:3383-3397](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L3383-L3397)）。

> 完成后，你应该能对着图把本讲 4.4 节的调用链完整复述一遍，并能向同事解释「Mooncake 客户端为什么在大块 KV cache 传输上能做到接近线速」——因为数据面全程零拷贝，控制面只在小元数据上开销，且可批量摊薄。

## 6. 本讲小结

- Mooncake Store 的客户端是**三层模型**：`PyClient`（抽象接口）有两个实现——`RealClient`（主力，自带内存池与 TE，既是客户端又是存储节点）和 `DummyClient`（轻量代理，通过 shm+RPC 复用 Real 的资源）；`RealClient` 内部还组合了数据面编排器 `Client` 和控制面 RPC 客户端 `MasterClient`。
- **控制面只走 `MasterClient`**：所有元数据访问（`GetReplicaList` 等）都经它用 coro_rpc 发出，底层是连接池，可用 `MC_RPC_TIMEOUT_MS` 等环境变量调优；数据面绝不直接碰元数据。
- **put/get 有「带拷贝」与「零拷贝」两套**：`put`/`get_buffer` 会经 buffer pool 多一次 `memcpy`；`put_from`/`get_into`/`register_buffer` 直接在已注册内存上切片，TE 直接搬，无额外拷贝。
- **一次 `get_into` 的完整链**：`get_into → get_into_range_internal → resolve_ranged_read_metadata(Query→GetReplicaList→SelectBestReplica) → execute_ranged_read → Client::Get → TransferRead → TransferSubmitter::submit(selectStrategy) → submitTransfer → engine_.submitTransfer`；策略上本地副本走 `LOCAL_MEMCPY`，跨节点走 `TRANSFER_ENGINE`。
- **批量接口**（`batch_*`、`get_into_ranges` + `QueryResultCache`）通过复用元数据、合并 TE 提交来摊薄固定开销，特别适合 LLM 解码阶段的多切片读。
- **DummyClient 的零拷贝**靠 shm 地址翻译：`real_addr = dummy_addr + shm_addr_offset`，TE 把数据写进 Real 注册的 shm，Dummy 因共享物理页而直接读到。

## 7. 下一步学习建议

- **深入 Master 内部**：本讲的 `MasterClient` 只是客户端侧的 RPC 封装。Master Service 如何分配副本、如何做租约与淘汰、如何处理 `PutStart/PutEnd` 的并发，请学习 u5-l2（master-service）。
- **深入 TransferEngine**：本讲到 `engine_.submitTransfer` 就停了。TE 内部如何把一批 `TransferRequest` 拆到具体 transport（RDMA/TCP/NVLink/Ascend）、如何做 batch 与完成事件，请回顾 u3 系列（transport）与 u2-l6（Python TE API）。
- **Local Hot Cache 与多级存储**：`Client::Get` 里出现了 `RedirectToHotCache`、`ShouldAdmitToHotCache`，以及 DISK/LOCAL_DISK 副本路径。如果你关心「读性能优化」与「SSD 卸载」，建议接着阅读 [client_service.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp) 中的 hot cache 与 `FileStorage` 相关代码，以及后续关于多级存储的讲义。
- **动手验证**：若有 RDMA 环境，建议跑 `mooncake-store/tests/` 下的 e2e 用例（如 `client_buffer_test.cpp`、`dummy_client_get_buffer_test.cpp`），对照本讲的调用链在日志里观察 `Using transfer strategy: LOCAL_MEMCPY/TRANSFER_ENGINE`（[client_service.cpp:3437](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_service.cpp#L3437)）这条输出，亲眼确认策略选择。
