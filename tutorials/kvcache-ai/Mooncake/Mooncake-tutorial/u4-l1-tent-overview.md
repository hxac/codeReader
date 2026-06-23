# TENT 概述与设计动机

## 1. 本讲目标

本讲是 TENT 系列的第一篇。读完后你应当能够：

1. 说清楚 TENT（Transfer Engine NEXT）是什么，它要解决老版 Transfer Engine（TE）的哪些痛点。
2. 理解 TENT 的两个核心概念：**声明式传输**与**切片喷射（slice spraying）**。
3. 在源码里定位 TENT 的「门面」（`TransferEngine`），知道它是如何把请求转交给运行时（`TransferEngineImpl`）的。
4. 描述一次大对象传输，在传统 TE 和 TENT 中「数据如何被分发到多条路径」的处理差异。
5. 在仓库中找到 TENT 的设计文档与配置文件，能动手读、动手改。

> 本讲面向已经读过 **u2-l1（TransferEngine 基础）** 的读者。我们会频繁拿传统 TE 做对比，帮助你建立「TENT 改进了什么」的直觉。

## 2. 前置知识

### 2.1 为什么要「分发到多条路径」

在大型 AI 集群里，两台机器之间往往不止一条物理链路。例如一台机器可能有 4 张 RDMA 网卡（俗称 4 条 rail）。当要搬运一个很大的对象（比如几十 GB 的 KVCache）时，如果只走一条 rail：

- 这条 rail 会被独占，带宽用不满；
- 其它 rail 闲置，整体吞吐低。

所以引擎通常会把一个大对象**切成很多小片（slice）**，分别丢到不同的 rail 上并行传输。这个动作就叫 **striping（条带）/ spraying（喷射）**。

### 2.2 传统做法的局限：静态

传统 TE 的做法接近「静态」：

- 应用在初始化时**手动选择并安装**一个传输后端（`installTransport`，比如绑定到 RDMA）；
- 多条 rail 之间用**固定轮转（round-robin）**做条带。

这在「机器同构、链路质量稳定」时很好用。但现代集群里：

- 一台机器内部，有的 rail 在本地 NUMA 节点，有的在远端 NUMA 节点（跨 NUMA 访问更慢）；
- 链路质量会因拥塞、硬件复位而动态变化；
- 有的 peer 之间有 NVLink，有的只有 RDMA，有的只能走主机内存中转。

静态选择 + 静态条带，会导致：**慢链路拖垮整体尾延迟**。TENT 就是为了把这些「该用哪条路径、每个 slice 给哪个设备」的决策，从应用搬进运行时。

### 2.3 几个会反复出现的术语

| 术语 | 含义 |
|------|------|
| rail | 一条物理传输通道（如一张 RDMA 网卡） |
| slice | 把大传输切出来的一个小数据片 |
| NUMA tier | 按 NUMA 距离给设备分的优先级层（本地 / 近端 / 远端） |
| EWMA | 指数加权移动平均，用来平滑地估计某条链路的「实时带宽」 |
| transport（传输后端） | 一种具体搬运方式：RDMA、NVLink、SHM、TCP、GDS 等 |

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [`mooncake-transfer-engine/tent/include/tent/transfer_engine.h`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/transfer_engine.h) | TENT 对外的 C++ 门面类声明，以及 C 风格 API（`tent_*` 函数、`tent_request` 结构体） |
| [`mooncake-transfer-engine/tent/src/transfer_engine.cpp`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transfer_engine.cpp) | 门面的实现，全部是转发给 `TransferEngineImpl` 的薄封装 |
| [`mooncake-transfer-engine/tent/include/tent/runtime/transfer_engine_impl.h`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/runtime/transfer_engine_impl.h) | 真正的运行时类 `TransferEngineImpl` 声明 |
| [`mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp) | 运行时实现：接收请求、选传输后端、分发到各 transport |
| [`mooncake-transfer-engine/tent/include/tent/runtime/transport_selector.h`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/runtime/transport_selector.h) | 策略驱动的传输选择器 `TransportSelector` |
| [`mooncake-transfer-engine/tent/include/tent/transport/rdma/quota.h`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/transport/rdma/quota.h) | 切片喷射核心类 `DeviceSelector` 声明 |
| [`mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp) | `DeviceSelector` 实现：轮转 / EWMA / 多路分配 |
| [`mooncake-transfer-engine/tent/src/transport/rdma/rdma_transport.cpp`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/rdma_transport.cpp) | RDMA 传输后端：把请求切成 slice 并调用 `DeviceSelector` |
| [`mooncake-transfer-engine/tent/config/transfer-engine.json`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/config/transfer-engine.json) | TENT 的示例配置文件（声明式策略就在这里写） |
| [`docs/source/design/tent/overview.md`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/tent/overview.md) | TENT 设计概览文档 |
| [`docs/source/design/tent/slice-spraying.md`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/tent/slice-spraying.md) | 切片喷射专题文档 |

记忆线索：**门面（`TransferEngine`）→ 运行时（`TransferEngineImpl`）→ 选择器（`TransportSelector` 选传输类型 / `DeviceSelector` 选具体设备）**。本讲沿着这条链讲清楚。

## 4. 核心概念与源码讲解

### 4.1 TENT 门面：一个「声明式」的薄入口

#### 4.1.1 概念说明

「门面（Facade）」是面向对象设计里的一个经典模式：给一个复杂子系统提供一个简单的对外接口。TENT 的 `TransferEngine` 就是这样一个门面。

它的核心特征是**声明式**：

- 你只告诉它「**我要把哪段内存，搬到哪个 segment 的哪个偏移**」；
- 你**不**告诉它「用 RDMA 还是 NVLink」「用哪张网卡」「切成几片」。

这些「怎么做」的细节，全部藏在门面背后（`TransferEngineImpl` 及其选择器里）。对比之下，传统 TE 的 API 是**命令式**的——应用要自己 `installTransport` 安装后端、自己管理拓扑。

门面本身应当「瘦」。让我们看看它是不是真的只做转发。

#### 4.1.2 核心流程

一次 TENT 传输从用户视角只有几步（声明式）：

```
1. 构造 engine（传 Config 或配置文件路径，一步到位）
2. allocateLocalMemory / registerLocalMemory（准备本地缓冲区）
3. openSegment（拿到远端 segment 的 handle）
4. allocateBatch + submitTransfer（提交一批 Request）
5. getTransferStatus 轮询直到 COMPLETED
6. freeBatch / closeSegment / freeLocalMemory（清理）
```

而门面内部，每个公共方法都只有一行：**转发给 `impl_`**。运行时（`impl_`）才负责真正干活：

```
TransferEngine::submitTransfer(batch, requests)
        │  转发
        ▼
TransferEngineImpl::submitTransfer(...)
        │  resolveTransport() → TransportSelector 选传输类型
        ▼
按传输类型把请求分桶，调用 transport->submitTransferTasks()
```

#### 4.1.3 源码精读

**门面类持有唯一的实现指针（PIMPL 模式）。** `TransferEngine` 把所有状态都藏在 `impl_` 里，对外只暴露值类型语义的接口：

[`mooncake-transfer-engine/tent/include/tent/transfer_engine.h:324-326`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/transfer_engine.h#L324-L326) —— 私有成员只有一个 `impl_`：

```cpp
   private:
    std::unique_ptr<TransferEngineImpl> impl_;
```

这种「指针指向实现」（PIMPL, Pointer to IMPLementation）的好处是：头文件不必包含 `TransferEngineImpl` 的全部定义，编译解耦，且门面可以禁止拷贝（见头文件中 `delete` 掉的拷贝构造与赋值）。

**构造一步到位。** 门面提供三个构造函数，对应三种配置来源：默认路径/环境变量、配置文件路径、直接传 `Config` 对象。三者最终都构造一个 `TransferEngineImpl`：

[`mooncake-transfer-engine/tent/src/transfer_engine.cpp:23-36`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transfer_engine.cpp#L23-L36) —— 三个构造函数，全部把活交给 `TransferEngineImpl`：

```cpp
TransferEngine::TransferEngine()
    : impl_(std::make_unique<TransferEngineImpl>()) {}

TransferEngine::TransferEngine(const std::string config_path) {
    auto conf = std::make_shared<Config>();
    auto status = conf->loadFile(config_path);
    if (!status.ok()) {
        LOG(WARNING) << "Failed to read config file " << config_path;
    }
    impl_ = std::make_unique<TransferEngineImpl>(conf);
}

TransferEngine::TransferEngine(std::shared_ptr<Config> conf)
    : impl_(std::make_unique<TransferEngineImpl>(conf)) {}
```

注意第一个构造函数无参，配置全部来自默认路径或环境变量——这是「零配置」的常见入口。

**每个公共方法都是一行转发。** 以最关键的 `submitTransfer` 为例：

[`mooncake-transfer-engine/tent/src/transfer_engine.cpp:132-135`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transfer_engine.cpp#L132-L135) —— 门面把整批请求原样转给运行时：

```cpp
Status TransferEngine::submitTransfer(
    BatchID batch_id, const std::vector<Request>& request_list) {
    return impl_->submitTransfer(batch_id, request_list);
}
```

整份 `transfer_engine.cpp` 的几十个方法**无一例外**都是这种「`return impl_->xxx(...)`」的转发。这说明门面确实「瘦」，真正逻辑在 `TransferEngineImpl`。

**声明式请求结构体。** 用户提交的 `Request` 描述「搬什么」，而不是「怎么搬」：

[`mooncake-transfer-engine/tent/include/tent/transfer_engine.h:38-46`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/transfer_engine.h#L38-L46) —— C 风格 API 里的 `tent_request`，字段只有「搬什么/搬到哪/多大/优先级」：

```cpp
struct tent_request {
    int opcode;          // READ 或 WRITE
    void* source;        // 本地缓冲区地址
    tent_segment_id_t target_id;   // 目标 segment
    uint64_t target_offset;        // 目标偏移
    uint64_t length;     // 字节数
    int priority;        // 0=HIGH 1=MEDIUM 2=LOW
    int transport_hint;  // TRANSPORT_UNSPEC=交给策略；否则钉死在某 transport
};
```

其中 `transport_hint` 默认为 `TRANSPORT_UNSPEC`（值为 0），表示「我不指定，交给运行时策略」。这就是声明式的体现：用户**可以**通过 `transport_hint` 强制指定，但**默认**完全放手。C++ 版 `Request` 同理（见 `cpp-api.md` 第 184-196 行）。

**与 TE 对照：传输管理从公共 API 里消失了。** 在 TENT 门面里你找不到 `installTransport` / `uninstallTransport` / `getTransport`。传输后端被装在哪？答案在运行时的私有字段里：

[`mooncake-transfer-engine/tent/include/tent/runtime/transfer_engine_impl.h:241-248`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/runtime/transfer_engine_impl.h#L241-L248) —— 运行时内部用一个数组持有所有 transport，外加一个选择器：

```cpp
std::shared_ptr<Config> conf_;
std::shared_ptr<ControlService> metadata_;
std::shared_ptr<Topology> topology_;
std::unique_ptr<TransportSelector> transport_selector_;
bool available_;

std::array<std::shared_ptr<Transport>, kSupportedTransportTypes>
    transport_list_;
```

也就是说，「哪些 transport 可用、用哪个」完全是运行时的内部状态，用户碰不到。

#### 4.1.4 代码实践

**实践目标**：通过阅读门面源码，验证「门面只做转发」，并找到转发背后真正的实现类。

**操作步骤**：

1. 打开 [`mooncake-transfer-engine/tent/src/transfer_engine.cpp`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transfer_engine.cpp)。
2. 统计：整个文件里有多少个方法体只有 `return impl_->...` 一行？
3. 在同目录下搜索 `TransferEngineImpl::submitTransfer` 的实现（在 `src/runtime/transfer_engine_impl.cpp`），看看「门面背后」真正做了多少事。

**需要观察的现象**：

- 门面文件里几乎所有方法都是一行转发；
- `submitTransfer` 的真正实现 `TransferEngineImpl::submitTransfer` 有上百行（本讲 4.2.3 会精读），包含合并请求、选传输、分桶、调用各 transport 等。

**预期结果**：你会直观感受到「门面很瘦、运行时很厚」。这是声明式 API 的典型代价——简单对外，复杂对内。

**待本地验证**：若你在本机编译了 TENT（`-DUSE_TENT=ON`），可以写一个最小程序，构造 `TransferEngine`、调用 `available()`，观察门面能否一步初始化成功。若没有 RDMA 环境，初始化可能返回 `available()==false`，这属于正常现象。

#### 4.1.5 小练习与答案

**练习 1**：门面类为什么要把 `impl_` 设为 `std::unique_ptr<TransferEngineImpl>`，而不是直接持有 `TransferEngineImpl impl_;` 成员？

> **参考答案**：直接持有成员要求头文件完整包含 `TransferEngineImpl` 的定义，会暴露运行时内部细节、拉长编译依赖、增大 ABI 耦合。用 `unique_ptr` 前置声明（forward declare）即可，实现「编译防火墙」与信息隐藏，这正是 PIMPL 模式的目的。

**练习 2**：用户既不调用 `installTransport`，那 RDMA 后端是何时被加载进 `transport_list_` 的？（提示：看 `transfer_engine_impl.h` 里 `loadTransports()` 这个私有方法。）

> **参考答案**：由运行时在 `construct()` 阶段调用 `loadTransports()`，根据 `Config` 中 `transports.rdma.enable` 等开关自动加载。用户完全无感——这正是「动态传输选择」的一部分。

---

### 4.2 设计概览：TENT 为什么这样设计

#### 4.2.1 概念说明

TENT 全称 **Transfer Engine NEXT**，是经典 Mooncake Transfer Engine 的继任者。它聚焦一个目标：**高效、可靠地搬运数据，而不要求应用去管理传输细节**。

设计文档把动机归结为现代集群的「异构 + 动态」：

- **异构**：同一批传输里，有的 peer 之间有 NVLink，有的只有 RDMA，有的只能走主机内存；
- **动态**：链路质量会因拥塞、复位、瞬时故障而变化。

在静态选择 + 静态条带下，这两个特点会直接变成两个问题（见 `overview.md` 第 14-18 行）：

1. 传输无法适应连通性的变化；
2. 慢链路主导尾延迟、压低有效带宽。

TENT 的解法是把更多决策**搬进运行时**。文档归纳为三大设计选择。

#### 4.2.2 核心流程：三大设计选择

TENT 建立在三个设计选择之上（对应 `overview.md` 第 21-48 行）：

```
┌─────────────────────────────────────────────────────────────┐
│ 选择一：动态传输选择（Dynamic Transport Selection）           │
│   应用只描述「搬什么」，运行时决定「用哪个后端、走哪条路径」    │
│   没有直达路径时，自动构造中转（如经主机内存）                 │
├─────────────────────────────────────────────────────────────┤
│ 选择二：带遥测的细粒度调度（Fine-Grained Scheduling）         │
│   大传输切成小 slice，每个 slice 独立调度                     │
│   用完成时间 / 队列深度等简单遥测决定 slice 去哪              │
│   慢路径自然分到更少 slice                                    │
├─────────────────────────────────────────────────────────────┤
│ 选择三：运行时内故障处理（In-Runtime Failure Handling）       │
│   局部失败不暴露给应用                                        │
│   路径变慢/不可用 → 暂停往它调度，重试 slice                  │
│   路径恢复 → 自动加回                                         │
└─────────────────────────────────────────────────────────────┘
```

架构层面（`overview.md` 第 49-60 行），TENT 由这些部分组成：

- 一个**声明式 API**（4.1 讲的门面）；
- 一个 **segment 抽象**，代表数据存放位置；
- 一组**可插拔传输后端**（RDMA、NVLink、共享内存等）；
- 一个**运行时**，集中做路径选择、调度、故障处理；
- 一条用**工作线程 + 无锁队列**实现的低开销数据通路。

关键原则是：**传输后端尽量小、只管搬数据；调度和策略决策集中到运行时。**

#### 4.2.3 源码精读

设计选择二「细粒度调度」在运行时里的入口是 `TransferEngineImpl::submitTransfer`。让我们看它如何把一批声明式请求「翻译」成对各 transport 的实际调用。

**第一步：每个请求选一个传输类型。** 合并请求后，对每个合并后的请求调用 `resolveTransport`，得到 `transport` 类型和 `device_mask`（允许使用的设备位掩码）：

[`mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp:1265-1273`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1265-L1273) —— 运行时为每个请求挑选 transport，挑不到就标记并跳过：

```cpp
auto select_result = resolveTransport(merged_request, 0);
task.type = select_result.transport;
task.device_mask = select_result.device_mask;
if (task.type == UNSPEC) {
    LOG(WARNING) << "Unable to find registered buffer for request: "
                 << printRequest(merged_request);
    merged_task_id_map[merged_task_id] = task;
    continue;
}
```

`resolveTransport` 本身很薄，它调用 `getTransportType`，并在失败时让元数据失效重试一次：

[`mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp:1207-1216`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1207-L1216) —— 选不到就刷新远端 segment 缓存再选一次（容忍元数据过期）：

```cpp
SelectionResult TransferEngineImpl::resolveTransport(const Request& req,
                                                     int transport_index,
                                                     bool invalidate_on_fail) {
    auto result = getTransportType(req, transport_index);
    if (result.transport == UNSPEC && invalidate_on_fail) {
        metadata_->segmentManager().invalidateRemote(req.target_id);
        result = getTransportType(req, transport_index);
    }
    return result;
}
```

**第二步：按传输类型分桶，分别交给对应后端。** 请求被装进 `classified_request_list[type]`，然后循环调用每个 transport 的 `submitTransferTasks`：

[`mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp:1309-1330`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1309-L1330) —— 运行时把请求按类型分发，并给 RDMA 传上 `device_mask`：

```cpp
for (size_t type = 0; type < kSupportedTransportTypes; ++type) {
    if (classified_request_list[type].empty()) continue;
    auto& transport = transport_list_[type];
    auto& sub_batch = batch->sub_batch[type];

    // 给 RDMA 后端设置 device_mask（策略限定可用设备）
    if (type == RDMA && !task_id_list[type].empty()) {
        sub_batch->device_mask =
            batch->task_list[task_id_list[type][0]].device_mask;
    }

    auto status = transport->submitTransferTasks(
        sub_batch, classified_request_list[type]);
    ...
}
```

注意 `device_mask` 从选择器一路透传到 RDMA 后端——这就是「策略限定的设备集合」进入数据通路的入口，4.3 会看到它如何参与切片喷射。

**策略长什么样？** `TransportSelector` 由配置中的 `policy` 数组驱动。示例配置里定义了两条策略：

[`mooncake-transfer-engine/tent/config/transfer-engine.json:93-105`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/config/transfer-engine.json#L93-L105) —— 内存类 segment 优先 NVLink→RDMA→SHM；文件类优先 GDS→io_uring→RDMA：

```json
"policy": [
    {
        "name": "default_memory",
        "segment_type": "memory",
        "devices": ["mlx5_0", "mlx5_2"],
        "transports": ["nvlink", "rdma", "shm"]
    },
    {
        "name": "file_storage",
        "segment_type": "file",
        "transports": ["gds", "io_uring", "rdma"]
    }
]
```

策略规则的字段含义在 `transport_selector.h` 的 `SelectionPolicy` 结构体里有明确定义：

[`mooncake-transfer-engine/tent/include/tent/runtime/transport_selector.h:90-120`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/runtime/transport_selector.h#L90-L120) —— 一条策略可按 segment 类型、内存类型、大小、优先级过滤，并列出允许的设备和传输偏好顺序：

```cpp
struct SelectionPolicy {
    std::string name;
    SegmentType segment_type;                 // File 或 Memory
    std::optional<bool> same_machine;         // 本机 or 远端
    std::optional<std::string> local_memory_pattern;   // "cuda"/"cpu"/"*"
    std::optional<std::string> remote_memory_pattern;
    std::optional<uint64_t> min_size;         // 大小区间过滤
    std::optional<uint64_t> max_size;
    std::optional<int> priority;              // 优先级精确匹配
    std::vector<std::string> devices;         // 允许的设备名
    std::vector<TransportType> transports;    // 传输偏好（按序尝试）
};
```

这正是「策略驱动」的落地：运行时按这些规则，为每个请求产出 `{transport 类型, device_mask}`。

#### 4.2.4 代码实践

**实践目标**：阅读 `overview.md`，把三大设计选择对应到源码里的具体函数/类。

**操作步骤**：

1. 读 [`docs/source/design/tent/overview.md`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/tent/overview.md) 第 21-60 行。
2. 建立下表，把每个设计选择映射到源码位置（左列文档，右列源码）：

| 文档里的设计选择 | 对应源码 |
|------|------|
| 动态传输选择 | `TransportSelector::select`、`TransferEngineImpl::resolveTransport` |
| 带遥测的细粒度调度 | `DeviceSelector::allocate`（切片喷射，见 4.3） |
| 运行时内故障处理 | `TransferEngineImpl::resubmitTransferTask`（见 `failover.md`） |

**需要观察的现象**：你会发现文档是「概念地图」，源码是「实现地图」，两者一一对应。

**预期结果**：能复述「TENT 把传输选择、调度、故障处理三件事都搬进了运行时」这句话，并指出每件事的源码入口。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `resolveTransport` 在第一次选不到 transport（返回 `UNSPEC`）时，要先 `invalidateRemote` 再重试一次？

> **参考答案**：远端 segment 的拓扑信息在本地有缓存。peer 可能刚刚注册/更新了缓冲区，本地缓存过期就会选不到。`invalidateRemote` 强制让缓存失效、重新拉取最新元数据，于是第二次 `getTransportType` 能拿到正确的传输候选。这是一种「容忍元数据最终一致」的容错设计。

**练习 2**：`overview.md` 说「传输后端刻意保持小巧、只专注搬数据」。请结合 `submitTransfer` 的代码，说明「调度」和「搬数据」的职责是如何分开的。

> **参考答案**：`TransferEngineImpl`（运行时）负责调度——它做请求合并、`resolveTransport` 选类型、分桶、决定 `device_mask`；而每个 transport（如 `RdmaTransport::submitTransferTasks`）只负责「接收已经分好类的请求、把它们搬走」。调度逻辑集中在运行时，后端不参与策略决策。

---

### 4.3 切片喷射：把 slice 智能地洒到多条 rail

#### 4.3.1 概念说明

「切片喷射（slice spraying）」是设计选择二「细粒度调度」在 RDMA 多 rail 场景下的具体实现。核心组件是 `DeviceSelector`。

朴素做法（也是 TENT 的 baseline 模式）是**轮转（round-robin）**：在最高优先级设备层里轮流挑设备。它确定性高、开销低，但**无法适应负载**——某条 rail 一旦变慢，它照样往上塞 slice。

TENT 的 **smart 模式**用三个手段改进：

1. **NUMA 感知的设备选择**：按 NUMA 距离给设备分层，越远惩罚越大；
2. **EWMA 带宽估计**：每条 rail 维护一个平滑的「实测带宽」估计，慢链路估计值会下降；
3. **动态多路分配**：大传输把 slice 按各设备的「吸引力」加权分配，慢设备自然分到更少 slice。

下面把这套机制讲清楚。

#### 4.3.2 核心流程

`DeviceSelector` 有两种模式，由 `smart_selection_enabled_` 切换：

**Baseline 模式（`enable_smart_scheduling = false`）**：

```
对每个请求：
  1. 找到第一个非空的设备层（优先本地 NUMA）
  2. 在该层内 round-robin 挑设备
  3. 忽略更低优先级的层
```

**Smart 模式（`enable_smart_scheduling = true`）**：

```
对每个请求的每个候选设备：
  1. 预测完成时间：
        predicted_time = (inflight_bytes + slice_bytes) / ewma_bandwidth

  2. 施加 NUMA 惩罚：
        score = predicted_time × numa_tier_weights[tier]

  3. 选 score 最小的设备：
        - 单 slice：只选最优设备
        - 多 slice：按权重 1/score 在多个设备间分配

  4. 传输完成时更新 EWMA：
        ewma = α × ewma + (1 − α) × observed
        其中 α = bandwidth_learning_rate（默认 0.01）
```

评分公式背后有直觉：`predicted_time` 越小说明「这台设备很快就能搬完」；乘以 NUMA 惩罚后，远端 NUMA 设备的分数被放大，变得「不那么有吸引力」。最终选 score 小的。

**EWMA 的数学含义。** EWMA（指数加权移动平均）给新观测更大权重时，估计就更「灵敏」；给旧值更大权重时，估计就更「平滑」。TENT 的定义是：

\[ \text{ewma}_{\text{new}} = \alpha \cdot \text{ewma}_{\text{old}} + (1-\alpha) \cdot \text{observed} \]

这里 \(\alpha\) 是**旧值**的权重（注意这与某些教材的约定相反，文档 `slice-spraying.md` 第 99-106 行专门提醒了这一点）。因此：

- \(\alpha\) 越小（接近 0）→ 新观测权重越大 → **适应越快**；
- \(\alpha\) 越大（接近 1）→ 旧值权重越大 → **适应越慢**。

为防止估计值跑飞，更新后会做裁剪：

\[ \text{ewma} \leftarrow \mathrm{clamp}\bigl(\text{ewma},\; m_{\min}\cdot B_{\text{theo}},\; m_{\max}\cdot B_{\text{theo}}\bigr) \]

其中 \(B_{\text{theo}}\) 是设备理论带宽，\(m_{\min}=0.1\)、\(m_{\max}=10.0\) 是默认乘数。

**多路分配的小心机：探针模式。** 如果一个设备长期分不到 slice，它的 EWMA 就永远不会被更新（因为更新发生在传输完成时）。为避免「EWMA 饥饿」，每 100 次分配里有 1 次走 round-robin，强制给所有设备喂样本。这是个很实用的工程细节。

#### 4.3.3 源码精读

切片喷射的核心在 `quota.h` / `quota.cpp`（注意头文件名叫 `quota.h`，但类是 `DeviceSelector`，注释里写明 `TENT_SELECTOR_H`）。

**`DeviceSelector` 类的两种模式与调度参数。** 头文件顶部的注释把整套公式说得很清楚：

[`mooncake-transfer-engine/tent/include/tent/transport/rdma/quota.h:40-59`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/transport/rdma/quota.h#L40-L59) —— 注释直接给出评分公式与 EWMA 更新规则：

```cpp
/**
 * @brief DeviceSelector implements NIC selection with two modes:
 *
 * 1. Baseline mode (smart_selection_enabled=false): Simple round-robin
 * 2. Smart mode (smart_selection_enabled=true): EWMA-based selection
 *
 * Selection formula:
 *     predicted_time = (inflight + slice_bytes) / ewma_bandwidth
 *
 * EWMA update:
 *     ewma_bandwidth <- alpha * ewma_bandwidth + (1 - alpha) * observed
 */
```

每个设备维护三个原子量：在途字节 `inflight_bytes`、EWMA 带宽 `ewma_bandwidth_bps`、累计字节 `total_bytes`（注意结构体里用 `padding` 把它们隔开，避免多核 false sharing）：

[`mooncake-transfer-engine/tent/include/tent/transport/rdma/quota.h:69-79`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/transport/rdma/quota.h#L69-L79) —— `DeviceInfo` 用原子量 + 缓存行填充，安全地记录在途字节与 EWMA 带宽：

```cpp
struct DeviceInfo {
    int dev_id;
    double bw_gbps;
    int numa_id;
    uint64_t padding0[5];
    std::atomic<uint64_t> inflight_bytes{0};
    uint64_t padding1[7];
    std::atomic<double> ewma_bandwidth_bps{50e9};
    uint64_t padding2[7];
    std::atomic<uint64_t> total_bytes{0};
    ...
};
```

**调度参数（可配置）。** `SchedulingParams` 把 NUMA 惩罚、学习率等都做成可调参数：

[`mooncake-transfer-engine/tent/include/tent/transport/rdma/quota.h:155-162`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/transport/rdma/quota.h#L155-L162) —— NUMA 层惩罚 `[1.0, 5.0, 10.0]`，EWMA 学习率默认 `0.01`：

```cpp
struct SchedulingParams {
    // NUMA tier penalties (rank 0 = local, should be smallest)
    double numa_tier_weights[Topology::DevicePriorityRanks] = {1.0, 5.0, 10.0};

    // EWMA bandwidth learning rate (0.0 = full adaptation, 1.0 = no learning)
    double bandwidth_learning_rate = 0.01;
    ...
};
```

**`allocate`：模式分发的总入口。** 同一个 `allocate` 方法，先判断模式，再走不同分支：

[`mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp:52-91`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp#L52-L91) —— 关闭 smart 时走 baseline：找首个非空设备层、在该层 round-robin：

```cpp
Status DeviceSelector::allocate(uint64_t total_length, uint32_t num_slices,
                                uint64_t slice_bytes, ...) {
    ...
    if (!smart_selection_enabled_) {
        // Baseline mode: consistent with original TE behavior
        thread_local uint64_t tl_rr_counter = 0;
        for (size_t rank = 0; rank < Topology::DevicePriorityRanks; ++rank) {
            ...
            if (tl_eligible.empty()) continue;
            // 在首个非空层里 round-robin
            for (uint32_t i = 0; i < num_slices; ++i) {
                int dev_id = tl_eligible[tl_rr_counter % tl_eligible.size()];
                tl_rr_counter++;
                slice_dev_ids.push_back(dev_id);
                ...
            }
            return Status::OK();
        }
        return Status::DeviceNotFound("no eligible devices");
    }
    ...
}
```

注意 baseline 分支注释明确写「consistent with original TE behavior」——这就是传统 TE 的行为，可作对比基准。

smart 模式的分发在同一个函数后半段：

[`mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp:93-108`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp#L93-L108) —— smart 模式按 slice 数量走单路或多路，多路时每 100 次有 1 次探针：

```cpp
std::vector<DeviceSelector::Candidate> tl_candidates;
Status status = buildCandidates(entry, slice_bytes, device_mask,
                                tl_candidates, priority);
if (!status.ok()) return status;
if (num_slices == 1) {
    selectSinglePath(tl_candidates, num_slices, total_length, slice_dev_ids);
} else {
    // Probe mode: every 100th call uses round-robin
    thread_local uint64_t tl_call_count = 0;
    bool probe_mode = ((++tl_call_count % 100) == 0);
    selectMultiPath(tl_candidates, num_slices, total_length, slice_dev_ids,
                    probe_mode);
}
return Status::OK();
```

**评分公式：`buildCandidates`。** 这是 smart 模式的核心——为每个候选设备算 score：

[`mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp:131-147`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp#L131-L147) —— predicted_time 乘以 NUMA 惩罚再加抖动，得到 score：

```cpp
auto add_candidate = [&](int dev_id, size_t rank) {
    auto& dev = devices_[dev_id];
    uint64_t inflight = dev.getInflightBytes();
    double ewma_bw = dev.getEwmaBandwidth();
    double predicted_time =
        static_cast<double>(inflight + slice_bytes) / ewma_bw;
    double rank_penalty = sched_params_.numa_tier_weights[rank];
    double score = predicted_time * rank_penalty;
    score += (SimpleRandom::Get().next(10) * sched_params_.score_jitter_range);
    ...
};
```

`score_jitter_range`（默认 `1e-9`）是一个极小的随机抖动，作用是打破「多个设备 score 完全相等时总是选同一个」的确定性，避免某台设备被饿死或被过载。

**多路加权分配：`selectMultiPath`。** 正常模式下，每个设备分到的 slice 数正比于权重 \(w_i = 1/(\text{score}_i + \epsilon)\)：

[`mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp:224-260`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp#L224-L260) —— 正常模式：按 1/score 加权分配，剩余 slice 给最优设备：

```cpp
// Normal mode: weighted distribution based on inverse score
double total_weight = 0.0;
...
for (size_t i = 0; i < candidates.size(); ++i) {
    double w = 1.0 / (candidates[i].score + sched_params_.score_epsilon);
    total_weight += w;
    ...
}
uint32_t remaining_slices = num_slices;
for (size_t i = 0; i < candidates.size(); ++i) {
    double w = 1.0 / (candidates[i].score + sched_params_.score_epsilon);
    uint32_t assigned = static_cast<uint32_t>((w / total_weight) * num_slices);
    ...  // 把 assigned 个 slice 钉到该设备，累加 inflight 与 total_bytes
}
if (remaining_slices > 0) {
    // 余数（因取整丢失的 slice）补给最优设备
    ...
}
```

探针模式则简单得多——round-robin 喂样本：

[`mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp:214-223`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp#L214-L223) —— 探针模式：轮流给每个设备分一个 slice，确保 EWMA 都被更新：

```cpp
if (probe_mode) {
    for (uint32_t i = 0; i < num_slices; ++i) {
        const Candidate& c = candidates[i % candidates.size()];
        slice_dev_ids.push_back(c.dev_id);
        devices_[c.dev_id].addInflight(slice_bytes);
        devices_[c.dev_id].total_bytes.fetch_add(slice_bytes,
                                                 std::memory_order_relaxed);
    }
}
```

**EWMA 更新：`release`。** slice 传输完成时被调用，用实测带宽刷新 EWMA 并裁剪：

[`mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp:299-314`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp#L299-L314) —— 传输完成后用 observed = length/latency 更新 EWMA，并裁剪到理论带宽的 [0.1×, 10×]：

```cpp
// Update EWMA bandwidth: new = α × old + (1-α) × observed
double observed_bw = static_cast<double>(length) / latency;
double current_ewma = dev.getEwmaBandwidth();
double alpha = sched_params_.bandwidth_learning_rate;
double new_ewma = alpha * current_ewma + (1.0 - alpha) * observed_bw;

// Clamp to [min_multiplier, max_multiplier] of theoretical bandwidth
double theoretical_bw = dev.getTheoreticalBandwidth();
new_ewma = std::max(
    sched_params_.ewma_min_multiplier * theoretical_bw,
    std::min(sched_params_.ewma_max_multiplier * theoretical_bw, new_ewma));
dev.ewma_bandwidth_bps.store(new_ewma, std::memory_order_relaxed);
```

**上游：RDMA 后端如何切 slice 并调用 `DeviceSelector`。** 切片发生在 `RdmaTransport::submitTransferTasks`。它先按 block 大小算出 slice 数（上限 `max_slice_count`，对 CUDA 或 WRITE 减半），只在「slice 数足够多」时才走聚合分配：

[`mooncake-transfer-engine/tent/src/transport/rdma/rdma_transport.cpp:396-411`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/rdma_transport.cpp#L396-L411) —— 大请求被切成至多 64（CUDA/WRITE 为 32）个 slice：

```cpp
size_t max_slice_count = 64;
if (type == MTYPE_CUDA || opcode == Request::WRITE)
    max_slice_count = 32;
...
uint64_t num_slices = (request.length + base_block - 1) / base_block;
num_slices = std::max<uint64_t>(
    1, std::min<uint64_t>(num_slices, max_slice_count));
```

[`mooncake-transfer-engine/tent/src/transport/rdma/rdma_transport.cpp:424-443`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/rdma_transport.cpp#L424-L443) —— 仅当 slice 数 ≥ max_slice_count/2 时，才交给 `DeviceSelector` 做聚合喷射分配：

```cpp
std::vector<int> slice_dev_ids;
// Only if a single request is enough, we perform aggregated allocation
if (num_slices >= max_slice_count / 2) {
    ...
    auto device_selector = workers_->getDeviceSelector();
    if (device_selector) {
        auto status = device_selector->allocate(
            request.length, static_cast<uint32_t>(num_slices),
            block_size, source_location, slice_dev_ids,
            request.priority, batch->device_mask);
        ...
    }
}
```

注意这里把 `batch->device_mask`（运行时策略算出的设备掩码）传给了 `DeviceSelector::allocate`，于是「策略限定设备」与「EWMA 自适应分配」在这一个调用里汇合。这也是 `slice-spraying.md` 第 129-163 行那张流程图的真实落点。

#### 4.3.4 代码实践

**实践目标**：阅读 `slice-spraying.md`，再回到源码，验证文档里的算法描述与代码完全一致。

**操作步骤**：

1. 读 [`docs/source/design/tent/slice-spraying.md`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/tent/slice-spraying.md) 第 44-62 行（Smart Mode 伪代码）和第 113-128 行（多路分配）。
2. 打开 `quota.cpp`，对照 `buildCandidates`（131-147 行）和 `selectMultiPath`（224-260 行）。
3. 用纸笔推演：假设两台设备 A、B，score 分别为 2.0、4.0，num_slices = 6，算一下正常模式下 A、B 各分到几个 slice。

**需要观察的现象**：

- 文档伪代码里 `predicted_time = (inflight + slice_bytes) / ewma_bandwidth` 与 `buildCandidates` 第 135-136 行逐字对应；
- 文档说「multi-path：proportional to device capacity」，代码里用的就是 `(w / total_weight) × num_slices`，其中 `w = 1/(score + ε)`。

**预期结果**（推演）：权重 \(w_A = 1/2 = 0.5\)，\(w_B = 1/4 = 0.25\)，总权重 0.75。则

\[ \text{assigned}_A = \lfloor (0.5/0.75) \times 6 \rfloor = \lfloor 4 \rfloor = 4,\quad \text{assigned}_B = \lfloor (0.25/0.75) \times 6 \rfloor = \lfloor 2 \rfloor = 2 \]

恰好分完，剩余 0。score 更小（更快）的 A 分到更多 slice，符合直觉。

**待本地验证**：若你想看真实运行时的分配结果，可在测试里调用 `device_selector_->printTrafficStats()`（`quota.cpp` 第 319-331 行），它会打印每个设备的累计字节、EWMA 带宽、在途字节。在没有真实 RDMA 网卡的环境下，这部分需要构造 mock topology 才能跑通，属于「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`score_jitter_range`（默认 `1e-9`）这么小，几乎不影响 score 大小，那它存在的意义是什么？

> **参考答案**：当两个设备的 `predicted_time × penalty` 完全相等时，若没有抖动，`std::sort` 会因为实现细节稳定地偏向某个 dev_id，导致总是把 slice 给同一台设备、另一台饿死。极小的随机抖动能打破这种「平局确定性」，让等价设备被均匀采样，配合探针模式避免 EWMA 饥饿。

**练习 2**：为什么探针模式用 round-robin，而不是「把所有 slice 都喂给最久没被采样的那台设备」？

> **参考答案**：round-robin 实现极简、无状态（只需一个模计数器），且在一个请求内就能给所有候选设备各喂一个样本，足以触发 EWMA 更新。它只在 1% 的请求上发生，对正常吞吐几乎无影响，是一种「最低成本的保活」策略。

**练习 3**：把 `bandwidth_learning_rate` 从默认 `0.01` 调到 `0.9`，EWMA 行为会怎么变？

> **参考答案**：\(\alpha=0.9\) 意味着旧值权重 0.9、新观测权重仅 0.1，EWMA 变化非常迟缓——链路真实变慢后，估计带宽要很久才跟得上，调度会「反应迟钝」；好处是抗瞬时抖动。文档把这种情形归为「slower adaptation, more stable」。反之 \(\alpha\) 太小（如 0.001）则反应快但更 volatile。

## 5. 综合实践

**任务**：写一段对比说明——同样一次大对象传输，传统 `TransferEngineImpl`（TE）与 TENT 在「如何把数据分发到多条路径」上的处理差异。

**建议产出形式**：一段 200-400 字的中文说明 + 一张对比表。请结合本讲读到的真实源码行号来支撑你的论断（不要泛泛而谈）。

**操作步骤**：

1. **重温两份文档**：
   - [`docs/source/design/tent/overview.md`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/tent/overview.md)（尤其第 8-19 行的「问题」与第 33-39 行的「细粒度调度」）；
   - [`docs/source/design/tent/slice-spraying.md`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/tent/slice-spraying.md)（尤其第 8-19 行的 baseline 缺陷）。

2. **定位传统 TE 的对照点**：传统 TE 的多 rail 行为在 TENT 里被显式保留为 baseline 模式——`quota.cpp` 第 62-91 行的注释明说「consistent with original TE behavior」。你可以把这段当作「传统 TE 行为」的代表。

3. **定位 TENT 的改进点**：smart 模式（`quota.cpp` 第 93-108 行）、评分（131-147 行）、多路加权（224-260 行）、EWMA 更新（299-314 行）。

4. **撰写对比表**（示例骨架，请补全）：

| 维度 | 传统 TE / TENT baseline | TENT smart |
|------|------|------|
| 设备选择策略 | 在首个非空 NUMA 层 round-robin（`quota.cpp:62-91`） | 按 `predicted_time × NUMA 惩罚` 评分择优（`quota.cpp:131-147`） |
| 是否感知负载 | 否，静态轮转 | 是，靠 `inflight_bytes` 与 EWMA |
| 慢链路影响 | 慢链路照拿 slice，拖垮尾延迟 | score 变大、分到的 slice 自然变少 |
| 多路分配 | 均匀轮转 | 按 `1/score` 加权（`quota.cpp:224-260`） |
| 带宽估计 | 无 | EWMA，完成时更新（`quota.cpp:299-314`） |

**需要观察的现象**：写完对比后，你应该能用一句话回答——「TENT 的关键转变，是把『分发到多条路径』从一个**静态、对称**的轮转动作，变成了一个**基于实时遥测、非对称**的优化问题」。

**预期结果**：你的说明里至少包含 3 个带行号的源码引用，且能解释「为什么 TENT 能避免慢链路主导尾延迟」。

## 6. 本讲小结

- TENT（Transfer Engine NEXT）是经典 TE 的继任者，目标是让应用**只声明「搬什么」**，把「怎么搬、走哪条路径」交给运行时。
- TENT 门面 `TransferEngine` 是一个 PIMPL 风格的薄封装，所有方法都转发给 `TransferEngineImpl`；它**移除了** `installTransport` 等 transport 管理 API。
- 三大设计选择：动态传输选择、带遥测的细粒度调度、运行时内故障处理——分别对应 `TransportSelector`、`DeviceSelector`、`resubmitTransferTask`。
- 切片喷射由 `DeviceSelector` 实现，有 baseline（round-robin）和 smart（EWMA 评分）两种模式；评分公式 `predicted_time = (inflight + slice_bytes) / ewma_bandwidth`，再乘 NUMA 惩罚。
- 多路分配按 `1/score` 加权，并用「每 100 次 1 次探针」避免 EWMA 饥饿；EWMA 在传输完成时按 \(\alpha\) 学习率更新并裁剪。
- RDMA 后端 `submitTransferTasks` 负责切 slice，仅当 slice 数足够多时调用 `DeviceSelector::allocate` 做聚合喷射，把运行时的 `device_mask` 与自适应分配汇合。

## 7. 下一步学习建议

学完本讲「是什么 + 为什么」之后，建议按下面的顺序深入「怎么实现」：

1. **`docs/source/design/tent/transport-selector.md`**：精读策略匹配的细节（`matchesPolicy`、`isTransportAvailable`），理解一条请求是如何被多条 `SelectionPolicy` 过滤的。
2. **`docs/source/design/tent/qos.md`**：了解 `priority` 与 `getDevicePriority` 如何在设备层做 QoS 过滤（本讲 4.3 看到 `buildCandidates` 里调了它）。
3. **`docs/source/design/tent/failover.md`**：理解 `resubmitTransferTask` 与 `max_failover_attempts` 如何实现设计选择三「运行时内故障处理」。
4. **动手读测试**：`mooncake-transfer-engine/tent/tests/transport_selector_test.cpp`、`engine_failover_e2e_test.cpp`，用测试断言反推行为，比只读文档更扎实。
5. **下一讲**（建议）：聚焦 `TransferEngineImpl` 的完整数据通路——从 `submitTransfer` 到 `getTransferStatus` 的状态机流转，把本讲提到的 `TaskInfo`、`SubBatch`、`progressBatch` 串成一条线。
