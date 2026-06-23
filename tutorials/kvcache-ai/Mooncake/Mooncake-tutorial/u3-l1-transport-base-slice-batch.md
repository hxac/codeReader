# Transport 基类：Slice/Task/Batch 模型

## 1. 本讲目标

Mooncake Transfer Engine 支持十几种不同的底层传输协议（RDMA、TCP、NVLink、CXL、NVMe-oF、Ascend HCCL……）。如果上层每调用一次"传输"都要关心"用哪条物理链路、怎么拆分、怎么知道传完了"，代码会变得极其复杂。

本讲的目标是带你看懂这套抽象的"骨架"：`Transport` 基类。学完本讲后，你应该能够：

1. 说清 `TransferRequest` 的每个字段含义，以及 `READ` / `WRITE` 两种操作码的语义；
2. 解释一条传输请求是如何被**切分**成若干个 `Slice`、再**聚合**为 `TransferTask`、最终汇入 `BatchDesc` 的；
3. 理解 `Slice` 为什么用 `union` 承载各协议特有的元数据，从而支持"一批请求跨协议并行"；
4. 掌握 `markSuccess / markFailed` + 原子计数器 + `getTransferStatus` 的**完成追踪机制**，能判断一个 Task / Batch 是否完成。

> 本讲只读不写源码，所有引用都来自当前 HEAD `945f3e61`。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：传输是一段"内存到内存"的搬运。** 无论底层是 RDMA 还是 TCP，对上层而言都是"把本地一段地址 `source` 的 `length` 字节，写到目标 segment 的 `target_offset` 处"。这就是 `TransferRequest` 抽象出来的东西。

**直觉二：大块传输要拆成小块。** 硬件单次能搬运的数据量是有限的（RDMA 的一条 Work Request、TCP 的一次 send 都有上限）。所以一次搬运 1MB，底层往往会被切成 16 个 64KB 的小单元逐个发出。这个"小单元"就是 `Slice`。

**直觉三：要能并发、要能查进度。** 一次提交可能包含多个搬运请求（一个 batch），其中有的走 RDMA、有的走 TCP；每个请求又由很多 Slice 组成。我们需要一套"聚合 + 原子计数"的模型，让任何线程都能在任意时刻回答："这个请求传完了吗？传了多少字节？"

如果你还不熟悉 Mooncake 的整体分层（TransferEngine / MultiTransport / 具体 Transport / Metadata），建议先看依赖讲义 `u2-l1`。本讲聚焦最底层的"对象模型与完成追踪"。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `mooncake-transfer-engine/include/transport/transport.h` | **核心头文件**。定义 `Transport` 抽象基类，以及 `TransferRequest`、`Slice`、`TransferTask`、`BatchDesc` 四个关键结构体。本讲的主角。 |
| `mooncake-transfer-engine/include/multi_transport.h` | `MultiTransport` 的声明。它持有一组 `Transport`，负责"按协议选路 + 聚合 Batch"，是 `Transport` 基类的直接使用者。 |
| `mooncake-transfer-engine/src/multi_transport.cpp` | `MultiTransport` 的实现：`submitTransfer` 如何把一批 request 聚合成 task，`getTransferStatus` 如何判断完成。 |
| `mooncake-transfer-engine/src/transport/transport.cpp` | 基类的少量实现：`allocateBatchID / freeBatchID` 和线程级 Slice 缓存的获取入口。 |
| `mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp` | RDMA 传输的实现，是理解"Task 如何被切分成 Slice"的**权威范例**（其他协议结构相同）。 |
| `mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp` | RDMA 后台 worker，演示 `Slice::markSuccess / markFailed` 在硬件完成回调里如何被调用。 |
| `mooncake-transfer-engine/include/config.h` | 全局配置（`slice_size`、`fragment_limit`、`retry_cnt`、`slice_timeout` 等），决定切分粒度与超时。 |

## 4. 核心概念与源码讲解

我们按"由外到内、由抽象到机制"的顺序，拆成五个最小模块：

- 4.1 `Transport` 基类与 `TransferRequest` / `TransferStatus`
- 4.2 `Slice`：单段传输的最小单元
- 4.3 `TransferTask` 与 `BatchDesc`：聚合容器与生命周期
- 4.4 切分与聚合流程：Request → Slice → Task → Batch
- 4.5 完成追踪：原子计数与 `getTransferStatus`

---

### 4.1 Transport 基类与 TransferRequest / TransferStatus

#### 4.1.1 概念说明

`Transport` 是所有具体传输协议（RdmaTransport、TcpTransport、NvlinkTransport……）的**抽象基类**。它定义了一组统一的虚函数接口，让上层（`MultiTransport` / `TransferEngine`）无需关心底层协议细节。

它对外只暴露三个核心动作：

1. **分配 / 释放一个 Batch**（`allocateBatchID / freeBatchID`）——一次批量传输的容器；
2. **提交一批传输请求**（`submitTransfer` / `submitTransferTask`）——把请求塞进 Batch 并真正下发到硬件；
3. **查询某个传输的进度**（`getTransferStatus`）——返回是否完成、已传输字节数。

同时它定义了描述"一次传输请求"和"传输状态"的两个值类型：`TransferRequest` 和 `TransferStatus`。

#### 4.1.2 核心流程

上层使用 `Transport` 的典型生命周期是：

```
allocateBatchID(N)            // 申请一个最多容纳 N 个请求的 Batch
   ↓
submitTransfer(batch_id, reqs)// 提交若干 TransferRequest
   ↓
循环 getTransferStatus(...)   // 轮询/查询每个 task 的状态
   ↓
freeBatchID(batch_id)         // 全部完成后释放
```

`TransferRequest` 描述"搬什么、搬到哪"，是一个很小的 POD 结构：

[`mooncake-transfer-engine/include/transport/transport.h:60-71`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transport/transport.h#L60-L71) — 定义 `TransferRequest`，其中 `enum OpCode { READ, WRITE }`。

```cpp
struct TransferRequest {
    enum OpCode { READ, WRITE };
    OpCode opcode;            // 方向：READ 从远端读，WRITE 往远端写
    void *source;             // 本地源地址
    SegmentID target_id;      // 目标 segment 的 ID
    uint64_t target_offset;   // 目标 segment 内的偏移
    size_t length;            // 要搬运的字节数
    int advise_retry_cnt = 0; // 建议的重试次数（由调用方提示）
    int transport_hint = 0;   // 指定使用的 transport（TENT 专用）
};
```

几个要点：

- `READ` / `WRITE` 的语义是**以本地视角**定义的。`WRITE` = 把本地 `source` 写到远端 `target_id@target_offset`；`READ` = 把远端 `target_id@target_offset` 的内容读到本地 `source`。源地址永远在本地。
- `target_id` 是 segment 的逻辑 ID，而非物理地址。真正的远端物理地址会在切分阶段由 metadata 解析（`target_offset` + 远端 buffer 基址）。
- `length` 可以很大（MB 甚至 GB 级），它会被切成很多 Slice，所以这个结构本身很小、很轻。

传输状态用两个类型刻画：

[`mooncake-transfer-engine/include/transport/transport.h:73-86`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transport/transport.h#L73-L86) — `TransferStatusEnum` 七态枚举与 `TransferStatus` 结构。

```cpp
enum TransferStatusEnum {
    WAITING,    // 进行中
    PENDING,    // 已排队
    INVALID,    // 参数非法
    CANCELED,   // 已取消
    COMPLETED,  // 成功完成
    TIMEOUT,    // 超时
    FAILED      // 失败
};
struct TransferStatus {
    TransferStatusEnum s;
    size_t transferred_bytes;
};
```

最常用的三态是 `WAITING`（还在传）、`COMPLETED`（全部成功）、`FAILED`（有 slice 失败）。`TIMEOUT` 是本讲 4.5 会讲到的"软超时"判定结果。

#### 4.1.3 源码精读

基类的虚函数接口定义在：

[`mooncake-transfer-engine/include/transport/transport.h:355-377`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transport/transport.h#L355-L377) — 三个核心虚函数 `allocateBatchID`、`submitTransfer`、`getTransferStatus`。

注意三个细节：

1. `submitTransfer` 是**纯虚函数**（`= 0`），意味着每个具体 Transport 必须自己实现"如何把 request 下发到硬件"。基类不假设任何协议行为。
2. `submitTransferTask` 有一个默认实现，直接返回 `NotImplemented`：

   [`mooncake-transfer-engine/include/transport/transport.h:366-370`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transport/transport.h#L366-L370) — `submitTransferTask` 的默认实现。这是 `MultiTransport` 实际调用的入口（见 4.4），允许 Transport 按"已经切好的 Task 列表"批量提交，从而支持跨协议聚合。

3. 基类还藏着一个**关键的句柄约定**：`BatchID` 本质上是 `BatchDesc*` 的整型重解释，而不是查表得到的序号。

   [`mooncake-transfer-engine/include/transport/transport.h:102-104`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transport/transport.h#L102-L104) — `toBatchDesc` 直接把 `BatchID` 重解释为 `BatchDesc&`。

   ```cpp
   static inline BatchDesc &toBatchDesc(BatchID id) {
       return *reinterpret_cast<BatchDesc *>(id);
   }
   ```

   注释明确说明：**为了热路径性能，故意跳过任何 map 查找**，直接把整数当指针用。代价是——只要句柄还在用，底层 `BatchDesc` 对象就必须存活。这也是 `freeBatchID` 必须等所有 task 完成才能释放的原因（见 4.3）。

#### 4.1.4 代码实践

**实践目标**：用 grep 在源码里数一下，基类一共声明了多少个纯虚函数（`= 0`），体会"哪些是子类必须实现的协议行为，哪些是基类有默认实现的辅助动作"。

**操作步骤**：

1. 在仓库根目录执行搜索（只看 `transport.h` 内的 `= 0`）。
2. 把找到的纯虚函数分成两组：`public` 区的对外接口 vs `private` 区的内存注册接口。

**需要观察的现象**：你会看到 `submitTransfer`、`getTransferStatus`、`registerLocalMemory`、`unregisterLocalMemory`、`registerLocalMemoryBatch`、`unregisterLocalMemoryBatch`、`getName` 都是纯虚的——这些是"每种协议必须各显神通"的点；而 `allocateBatchID`、`submitTransferTask`、`OpenChannel`、`CheckStatus` 都有默认实现，子类可以不重写。

**预期结果**：纯虚函数集中体现"协议相关"与"内存管理相关"两类行为；Batch 分配等"通用容器管理"则由基类统一兜底。

> 待本地验证：具体命令是否可用取决于你本地的 ripgrep 安装。可以用 Grep 工具或 `rg "= 0" mooncake-transfer-engine/include/transport/transport.h`。

#### 4.1.5 小练习与答案

**练习 1**：`TransferRequest` 里既有 `source` 又有 `target_offset`，为什么没有 `source_offset`？

**参考答案**：因为 `source` 本身就是一个**绝对指针**（`void*`），已经指向了本地缓冲区的起始字节，调用方需要哪一段就直接给出该段的地址即可，不需要额外的偏移字段。而远端没有指针（进程间不能共享指针），只能用"segment ID + 段内偏移"的逻辑坐标来定位，所以才有 `target_id` + `target_offset`。

**练习 2**：`getTransferStatus` 的注释说"This function shall not be called again after completion"（完成后不要再调用），为什么？

**参考答案**：完成判定会把 `task.is_finished` 置位、并在某些路径下推进批量完成计数；完成后再调用既无意义，也可能在事件驱动完成（`USE_EVENT_DRIVEN_COMPLETION`）路径下重复递增 `completed_slice_count` 等原子计数，造成状态错乱。

---

### 4.2 Slice：单段传输的最小单元

#### 4.2.1 概念说明

`Slice` 是整个传输引擎里**真正下发到硬件的最小单元**。一条 RDMA Work Request、一次 TCP send、一个 NVLink拷贝，背后都对应一个 `Slice`。

它解决两个问题：

1. **大块拆分**：把一个 `TransferRequest`（可能 MB 级）按 `slice_size`（默认 64KB）切成多片，分批送入硬件队列；
2. **协议无关的统一外壳 + 协议特化的内核**：外壳字段（源地址、长度、opcode、所属 task……）所有协议通用；而内核（远端地址、lkey/rkey、QP 深度指针……）每个协议各不相同。

第二个问题正是 `Slice` 设计的精妙之处——它用 **C++ `union`** 把所有协议的特化字段叠在同一块内存里，一个 `Slice` 既能当 RDMA slice 用，也能当 TCP slice 用，互不干扰。

#### 4.2.2 核心流程

一个 `Slice` 的生命周期：

```
getSliceCache().allocate()   // 从线程本地缓存分配（避免频繁 new/delete）
   ↓
填充 source_addr/length/opcode/rdma.* 等字段
   ↓
submitPostSend(...)           // 被投递到硬件（如 RDMA 的发送队列）
   ↓
后台 worker 等到硬件完成事件
   ↓
slice->markSuccess() 或 slice->markFailed()   // 更新所属 task 的原子计数
   ↓
（slice 由 task 析构时统一归还缓存）
```

`Slice` 内部的协议特化字段是个 union：

[`mooncake-transfer-engine/include/transport/transport.h:121-171`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transport/transport.h#L121-L171) — `Slice` 的 `union`，并列了 `rdma` / `ub` / `local` / `tcp` / `nvmeof` / `cxl` / `hccl` / `ascend_direct` / `ubshmem` 等协议分支。

例如 RDMA 分支持有 RDMA 才需要的 `dest_rkey`、`source_lkey`、`qp_depth`（队列深度指针）：

```cpp
struct {                          // rdma 分支
    uint64_t dest_addr;
    uint32_t source_lkey;
    uint32_t dest_rkey;
    int lkey_index;
    int rkey_index;
    volatile int *qp_depth;       // 指向队列深度计数器，用于反压
    uint32_t retry_cnt;
    uint32_t max_retry_cnt;
    RdmaEndPoint *endpoint;
} rdma;
```

而 `tcp` 分支极简，只需要远端地址：

```cpp
struct { uint64_t dest_addr; } tcp;
```

因为 union 是**同一块内存**，所以一个 Slice 同时只能"是"某一种协议的 slice，但它**不需要为每种协议单独定义一个类**。这就是支持"一批请求里混着多种协议"的关键——每个 Slice 自带协议元数据，互不影响。

#### 4.2.3 源码精读

外壳字段定义在 union 之前：

[`mooncake-transfer-engine/include/transport/transport.h:108-119`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transport/transport.h#L108-L119) — `Slice` 的协议无关字段。

```cpp
struct Slice {
    enum SliceStatus { PENDING, POSTED, SUCCESS, TIMEOUT, FAILED };
    void *source_addr;
    size_t length;
    TransferRequest::OpCode opcode;
    SegmentID target_id;
    std::string peer_nic_path;     // 对端 NIC 路径（用于选 endpoint）
    SliceStatus status;
    TransferTask *task;            // 反向指针：这个 slice 属于哪个 task
    std::vector<uint32_t> dest_rkeys;
    bool from_cache;               // 是否来自线程本地缓存（用于计数）
    union { ... };                 // 协议特化字段
```

> 注意 `task` 这个反向指针——它是完成追踪的关键：每个 slice 完成时，要靠它找到自己归属的 task 去累加计数。

完成回调 `markSuccess` / `markFailed` 是 Slice 的核心方法：

[`mooncake-transfer-engine/include/transport/transport.h:174-188`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transport/transport.h#L174-L188) — `markSuccess` 与 `markFailed`。

```cpp
void markSuccess() {
    status = Slice::SUCCESS;
    __atomic_fetch_add(&task->transferred_bytes, length, __ATOMIC_RELAXED);
    __atomic_fetch_add(&task->success_slice_count, 1, __ATOMIC_RELAXED);
    check_batch_completion(false);
}
void markFailed() {
    status = Slice::FAILED;
    __atomic_fetch_add(&task->failed_slice_count, 1, __ATOMIC_RELAXED);
    check_batch_completion(true);
}
```

要点：

- 用的是 GCC 内建 `__atomic_fetch_add`（`relaxed` 序），因为多个 worker 线程会**并发**地完成同一个 task 的不同 slice，必须原子累加。
- `markSuccess` 同时累加"成功 slice 数"和"已传输字节"——所以 `transferred_bytes` 是实时增长的，调用方可以在传输过程中查到中间进度。
- 累加完后调用 `check_batch_completion`，进入 4.5 讲的事件驱动完成逻辑。

**谁调用 markSuccess / markFailed？** 是后台硬件完成线程。以 RDMA 为例，worker 从完成队列拿到结果后回调：

[`mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp:274`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp#L274) — `#ifdef USE_FAKE_POST_SEND` 路径下，所有 slice 直接 `markSuccess()`，用于不依赖真实 RDMA 硬件的测试。

真实路径里，成功在 `worker_pool.cpp:382` 调用 `slice->markSuccess()`，失败（如建连失败、所有 rail 不可用）在多处调用 `slice->markFailed()`（如 `worker_pool.cpp:382`、`worker_pool.cpp:418` 等）。这印证了"完成是异步、并发发生的"。

#### 4.2.4 代码实践

**实践目标**：直观感受"Slice 是硬件级最小单元、且 union 让它协议无关"。

**操作步骤**：

1. 打开 `transport.h` 第 108–171 行，数一下 `union` 里一共并列了多少种协议分支。
2. 对比 `rdma` 分支和 `tcp` 分支的字段数差异，思考"为什么 TCP 那么少"。
3. 在 `worker_pool.cpp` 里搜索 `markSuccess` 和 `markFailed`，统计它们各被调用了多少次、分布在哪些错误分支。

**需要观察的现象**：union 分支数 ≥ 9（rdma/ub/local/tcp/nvmeof/cxl/hccl/ascend_direct/ubshmem）；`markFailed` 的调用点远多于 `markSuccess`，说明硬件传输有大量失败分支（建连失败、endpoint 失效、所有 rail 不可用……）需要逐一兜底。

**预期结果**：你会理解 Slice 的"统一外壳 + 多协议内核"是一次以内存重叠（union）换取代码统一的权衡——省去了 9 套并行的类层次。

> 待本地验证：具体分支数与调用点数量请以本地 grep 结果为准。

#### 4.2.5 小练习与答案

**练习 1**：既然 `Slice` 用 union 承载多协议字段，为什么不在外面套一个 `enum { RDMA, TCP, ... } kind` 字段来记录"当前是哪种协议"？

**参考答案**：因为一个 Slice **自创建起就明确归属某一个 Transport**（由切分它的 `submitTransferTask` 决定），它的生命周期内不会从 RDMA 切换成 TCP。协议信息由"创建它的 Transport 类型"隐式确定，不需要在每个 slice 里冗余存储。union 只是让"不同协议的 Slice 共用同一种类型"，而不是让"一个 Slice 在运行时切换协议"。

**练习 2**：`markSuccess` 用的是 `__ATOMIC_RELAXED`，会不会导致调用方读到"slice 都成功了但 transferred_bytes 还没更新"的不一致？

**参考答案**：在 relaxed 序下，单看 `transferred_bytes` 确实不能保证和 `success_slice_count` 同时可见。但完成判定用的是"`success + failed == slice_count`"这个由多个原子计数联合推导的条件，而最终的可见性由 `getTransferStatus` 路径上的 acquire/release（见 4.5）或轮询语义来保证。也就是说，"是否完成"的判定是可靠的，中间字节的瞬时读数可能略有滞后，但对正确性无影响。

---

### 4.3 TransferTask 与 BatchDesc：聚合容器与生命周期

#### 4.3.1 概念说明

有了 Slice（最小单元）和 TransferRequest（用户意图），中间还需要两层聚合：

- **`TransferTask`**：一个 TransferRequest 对应一个 Task。Task 持有这个请求被切出的所有 Slice，并用一组**原子计数器**追踪它们的完成情况。
- **`BatchDesc`**：一个 Batch（批）容纳多个 Task。Batch 是用户申请/释放的单位，也是一次性提交一组请求的容器。

层级关系是：

```
BatchDesc  ──包含 N 个──▶  TransferTask  ──包含 M 个──▶  Slice
 (一批)                   (一个请求)               (一个硬件单元)
```

- 一个 Batch 里的不同 Task 可以走**不同协议**（一个 batch 混合 RDMA + TCP）；
- 一个 Task 里的所有 Slice 走**同一种协议**（因为它们由同一个 Transport 切出）。

#### 4.3.2 核心流程

`TransferTask` 的关键字段是一组原子计数器，构成完成判定的"分子"：

[`mooncake-transfer-engine/include/transport/transport.h:289-326`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transport/transport.h#L289-L326) — `TransferTask` 结构。

```cpp
struct TransferTask {
    volatile uint64_t slice_count = 0;          // 该 task 一共有多少个 slice
    volatile uint64_t success_slice_count = 0;  // 成功完成的 slice 数
    volatile uint64_t failed_slice_count = 0;   // 失败的 slice 数
    volatile uint64_t transferred_bytes = 0;    // 已传输字节数
    volatile bool is_finished = false;
    uint64_t total_bytes = 0;
    BatchID batch_id = 0;                       // 归属的 batch
    Transport *transport_ = nullptr;            // 处理它的 transport（用于轮询）
    const TransferRequest *request = nullptr;   // 原始请求（用于切分时取源地址等）
    std::vector<Slice *> slice_list;            // 持有所有 slice（析构时归还缓存）
    ~TransferTask() {
        for (auto &slice : slice_list)
            Transport::getSliceCache().deallocate(slice);
    }
};
```

完成判定的"分子级"公式（一个 Task 完成当且仅当所有 slice 都有了终态）：

\[
\text{task 完成} \iff \text{success\_slice\_count} + \text{failed\_slice\_count} = \text{slice\_count}
\]

而成功与否取决于分子里有没有失败：

\[
\text{task 状态} = \begin{cases} \text{FAILED} & \text{若 } \text{failed\_slice\_count} > 0 \\ \text{COMPLETED} & \text{否则（全部成功）} \end{cases}
\]

`BatchDesc` 则是更外层的聚合，并持有用于"事件驱动完成通知"的同步原语：

[`mooncake-transfer-engine/include/transport/transport.h:328-349`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transport/transport.h#L328-L349) — `BatchDesc` 结构。

```cpp
struct BatchDesc {
    BatchID id;
    size_t batch_size;                         // 最大可容纳 task 数
    std::vector<TransferTask> task_list;       // 实际的 task 列表
    void *context;                             // 给具体 transport 存私有数据
    int64_t start_timestamp;
    std::atomic<bool> has_failure{false};
    std::atomic<bool> is_finished{false};      // 整批完成标志（wait 谓词）
    std::atomic<uint64_t> finished_transfer_bytes{0};
#ifdef USE_EVENT_DRIVEN_COMPLETION
    std::atomic<uint64_t> finished_task_count{0};
    std::mutex completion_mutex;
    std::condition_variable completion_cv;     // 用来唤醒等待整批完成的线程
#endif
};
```

注意 `task_list` 是 `std::vector<TransferTask>`（按值持有，不是指针），这意味着 Task 的内存由 Batch 统一管理；而 Slice 是指针（`std::vector<Slice*>`），因为 Slice 要在 worker 线程里跨线程访问、且走对象缓存。

#### 4.3.3 源码精读

Batch 的分配在基类与 `MultiTransport` 里几乎一样，关键在于"id 就是指针"：

[`mooncake-transfer-engine/src/multi_transport.cpp:78-91`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/multi_transport.cpp#L78-L91) — `MultiTransport::allocateBatchID`。

```cpp
MultiTransport::BatchID MultiTransport::allocateBatchID(size_t batch_size) {
    auto batch_desc = new BatchDesc();
    if (!batch_desc) return ERR_MEMORY;
    batch_desc->id = BatchID(batch_desc);   // ← 把指针值当作 id
    batch_desc->batch_size = batch_size;
    batch_desc->task_list.reserve(batch_size);
    ...
    return batch_desc->id;
}
```

`freeBatchID` 则强校验"所有 task 都完成才允许释放"，否则返回 `BatchBusy`：

[`mooncake-transfer-engine/src/multi_transport.cpp:93-108`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/multi_transport.cpp#L93-L108) — `MultiTransport::freeBatchID`。

```cpp
Status MultiTransport::freeBatchID(BatchID batch_id) {
    auto& batch_desc = *((BatchDesc*)(batch_id));
    for (size_t task_id = 0; task_id < task_count; task_id++) {
        if (!batch_desc.task_list[task_id].is_finished) {
            return Status::BatchBusy("BatchID cannot be freed until all tasks are done");
        }
    }
    delete &batch_desc;
    ...
}
```

这条约束和 4.1 讲的"BatchID 是裸指针"是配套的——只要还有人持有句柄去查状态，`BatchDesc` 就绝不能被 delete，否则就是悬垂指针。

#### 4.3.4 代码实践

**实践目标**：观察"Task 按值存于 vector、Slice 按指针存于 vector"这一设计，并验证 freeBatchID 的忙约束。

**操作步骤**：

1. 阅读 `transport.h:328-349`，确认 `task_list` 类型是 `vector<TransferTask>`。
2. 阅读 `TransferTask` 的析构（`transport.h:322-325`），看它如何把 `slice_list` 里的每个 slice 归还到 `getSliceCache()`。
3. 思考：如果 `task_list` 改成 `vector<TransferTask*>`（指针），会带来什么额外负担？

**需要观察的现象**：Task 析构会自动触发其所有 Slice 的缓存回收；Batch 析构（`delete &batch_desc`）会连锁析构整个 `task_list`，从而一次性回收所有 slice。

**预期结果**：理解"Batch 析构 = Task 析构 = Slice 回收"的级联关系，以及为什么必须在所有 task 完成后才能 free。

#### 4.3.5 小练习与答案

**练习 1**：`freeBatchID` 为什么不直接强制释放，而要返回 `BatchBusy` 错误？

**参考答案**：因为可能还有后台 worker 线程正持有该 batch 里某些 slice 的指针、即将调用 `markSuccess`。如果此时强行 `delete`，worker 会访问已释放的 `task`（通过 `slice->task` 反向指针），造成 use-after-free。返回错误让调用方"等完成再释放"是最稳妥的做法。

**练习 2**：`TransferTask::transport_` 字段的注释说它"用于在 `getTransferStatus()` 里委托 transport 做协议相关的完成轮询"。如果删掉这个字段会怎样？

**参考答案**：那么 `MultiTransport::getTransferStatus` 就无法知道某个 task 该让哪个 transport 去轮询（例如 NVLink 异步传输需要调 `cudaStreamQuery` 才能更新 slice 状态）。没有它，那些"需要主动 poll 才会推进状态"的协议就永远停留在 WAITING。下一节会看到它正是 `getTransferStatus` 的关键分支条件。

---

### 4.4 切分与聚合流程：Request → Slice → Task → Batch

#### 4.4.1 概念说明

前面三节分别认识了"零件"。本节把它们串成一条完整的流水线，回答本讲最核心的问题：

> **一批 `TransferRequest` 是怎么被切分成 Slice、聚合为 Task、再汇入 Batch 的？**

这条流水线分两段：

1. **聚合段（MultiTransport::submitTransfer）**：把每个 request 绑定到一个新 Task，并**按协议把 task 分组**，分别投给对应的 Transport。
2. **切分段（具体 Transport::submitTransferTask）**：每个 Transport 拿到一组 Task 后，按 `slice_size` 把每个 task 的 request 切成多个 Slice，投递到硬件。

第二段是"跨协议并行"的落点：同一次 `submitTransfer` 里，RDMA Transport 和 TCP Transport 会**各自独立地**处理属于自己的 task，互不阻塞。

#### 4.4.2 核心流程

聚合段的伪代码：

```
submitTransfer(batch_id, entries):
    为每个 entry 在 batch_desc.task_list 里新建一个 TransferTask
    对每个 entry，selectTransport(entry) 选出 transport
    按 transport 分组：submit_tasks[transport].push_back(&task)
    for 每个 (transport, task 子集):
        transport->submitTransferTask(task 子集)   # 各协议并行处理
```

切分段（以 RDMA 为例）的伪代码：

```
submitTransferTask(task_list):
    for 每个 task:
        request = task.request
        for offset in [0, request.length) step slice_size:
            slice = 从缓存分配
            是否合并最后一片 = (剩余字节 <= slice_size + fragment_limit)
            slice.length = 合并 ? 剩余字节 : slice_size
            填充 slice.rdma.*（lkey/rkey/dest_addr…）
            task.slice_list.push_back(slice)
            task.slice_count += 1
            攒够 watermark 就 flush 一次（submitPostSend）
        # 循环末尾若合并了最后一片则 break
    把剩余 slice 全部 submitPostSend
```

切分时有一个减少 slice 数量的优化——**尾部合并**。朴素切分会产生 \(\lceil L / S \rceil\) 个 slice（其中 \(L\) = 请求长度，\(S\) = `slice_size`），最后一个往往是凑不满一片的"小尾巴"。Mooncake 的做法是：如果当前位置到结尾的剩余字节数不超过 \(S + F\)（\(F\) = `fragment_limit`），就让当前 slice 直接吃掉全部剩余字节并提前结束循环。

\[
n_{\text{slice}} \approx \left\lceil \frac{L}{S} \right\rceil, \quad \text{尾部} \le F \text{ 时可少一个 slice}
\]

默认 \(S = 65536\)（64KB）、\(F = 16384\)（16KB），即"最后一个 slice 允许最多 80KB"，避免为几 KB 的尾巴额外发一条硬件请求。

#### 4.4.3 源码精读

**聚合段**在 `MultiTransport::submitTransfer`：

[`mooncake-transfer-engine/src/multi_transport.cpp:110-149`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/multi_transport.cpp#L110-L149) — 一批 request 聚合成 task 并按 transport 分组。

```cpp
Status MultiTransport::submitTransfer(BatchID batch_id,
                                      const std::vector<TransferRequest>& entries) {
    auto& batch_desc = *((BatchDesc*)(batch_id));
    if (batch_desc.task_list.size() + entries.size() > batch_desc.batch_size) {
        return Status::TooManyRequests("Exceed the limitation of batch capacity");
    }
    size_t task_id = batch_desc.task_list.size();
    batch_desc.task_list.resize(task_id + entries.size());   // 每个请求一个 task

    std::unordered_map<Transport*, std::vector<TransferTask*>> submit_tasks;
    for (auto& request : entries) {
        Transport* transport = nullptr;
        auto status = selectTransport(request, transport);   // 按协议选路
        if (!status.ok()) return status;
        auto& task = batch_desc.task_list[task_id];
        task.batch_id = batch_id;
        task.transport_ = transport;                         // 记下归属 transport
        task.request = &request;
        ++task_id;
        submit_tasks[transport].push_back(&task);            // 分组
    }
    for (auto& entry : submit_tasks) {
        auto status = entry.first->submitTransferTask(entry.second);  // 各自下发
        ...
    }
    return overall_status;
}
```

关键点：

1. **容量校验**：`task_list.size() + entries.size() > batch_size` 直接拒绝，防止越界。
2. **一个 request 一个 task**：`task_id` 随每个 request 递增。
3. **按 transport 分组下发**：`submit_tasks` 是个 map，把同一 transport 的 task 收集到一起，再一次性调 `submitTransferTask`。这就是"一批请求跨协议并行"的实现——RDMA 和 TCP 的 task 被分别投递，互不干扰。

`selectTransport` 根据目标 segment 声明的协议选 transport：

[`mooncake-transfer-engine/src/multi_transport.cpp:442-464`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/multi_transport.cpp#L442-L464) — `selectTransport`：从目标 segment desc 读取 `protocol`，在已安装的 transport_map 里查找。

```cpp
auto target_segment_desc = metadata_->getSegmentDescByID(entry.target_id);
auto proto = target_segment_desc->protocol;
if (!transport_map_.count(proto)) {
    return Status::NotSupportedTransport("Transport " + proto + " not installed");
}
transport = transport_map_[proto].get();
```

**切分段**在 `RdmaTransport::submitTransferTask`（其他协议结构相同）：

[`mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp:519-625`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp#L519-L625) — 按 `slice_size` 切分、尾部合并、攒批投递。

```cpp
const size_t kBlockSize = globalConfig().slice_size;     // 默认 64KB
const size_t kFragmentSize = globalConfig().fragment_limit; // 默认 16KB
const size_t kSubmitWatermark = globalConfig().max_wr * globalConfig().num_qp_per_ep;
for (size_t index = 0; index < task_list.size(); ++index) {
    auto& task = *task_list[index];
    auto& request = *task.request;
    for (uint64_t offset = 0; offset < request.length; offset += kBlockSize) {
        Slice* slice = getSliceCache().allocate();        // 线程本地缓存分配
        ...
        bool merge_final_slice =
            request.length - offset <= kBlockSize + kFragmentSize;   // 尾部合并判定
        slice->source_addr = (char*)request.source + offset;
        slice->length = merge_final_slice ? request.length - offset : kBlockSize;
        slice->opcode = request.opcode;
        slice->rdma.dest_addr = request.target_offset + offset;
        slice->task = &task;
        ...
        task.slice_list.push_back(slice);
        task.total_bytes += slice->length;
        __sync_fetch_and_add(&task.slice_count, 1);       // slice 计数 +1
        ...
        if (nr_slices >= kSubmitWatermark) {              // 攒够就 flush
            for (auto& entry : slices_to_post)
                entry.first->submitPostSend(entry.second);
            slices_to_post.clear();
            nr_slices = 0;
        }
        if (merge_final_slice) break;                     // 合并后提前结束
    }
}
for (auto& entry : slices_to_post)                        // 剩余的 flush
    if (!entry.second.empty()) entry.first->submitPostSend(entry.second);
```

要点逐条对应：

- `slice_size` 决定切分粒度（越小并发度越高、但硬件请求越多）；`fragment_limit` 决定尾部合并阈值。
- 每个 slice 的 `source_addr` / `dest_addr` 都按 `offset` 平移，保证各片在源和目的上对齐。
- `task.slice_count` 用 `__sync_fetch_and_add` 原子递增——因为不同 slice 会被分发到不同 device/context，完成顺序无序。
- `kSubmitWatermark = max_wr * num_qp_per_ep`（默认 256 × 2 = 512）是一个**流控水位**：攒到这么多 slice 就先 flush 一次，避免一次性塞爆硬件发送队列。这是"切分"与"实际下发"之间的批量缓冲。

#### 4.4.4 代码实践

**实践目标**：手工模拟一次切分，验证你对 slice 数量和尾部合并的理解。

**操作步骤**：

1. 假设一个 WRITE 请求 `length = 200000` 字节，`slice_size = 65536`，`fragment_limit = 16384`。
2. 在纸上逐步走 `offset = 0, 65536, 131072, ...`，每步判断 `merge_final_slice` 是否为真、当前 slice 的 `length` 是多少。
3. 数出最终的 slice 总数，以及每个 slice 的字节范围。

**需要观察的现象**：

| 步骤 | offset | 剩余 = 200000 − offset | 合并判定 (剩余 ≤ 65536+16384=81920)? | slice.length |
| --- | --- | --- | --- | --- |
| 1 | 0 | 200000 | 否 | 65536 |
| 2 | 65536 | 134464 | 否 | 65536 |
| 3 | 131072 | 68928 | 是（68928 ≤ 81920） | 68928（吃掉全部剩余并 break） |

**预期结果**：最终只有 **3 个 slice**，长度分别是 65536、65536、68928。注意第 3 个 slice 超过了 64KB（达到 ~67KB），这正是尾部合并的效果——它把原本会出现的"第三片 64KB + 第四片 4928 字节的碎尾巴"合并成了一片。`task.slice_count = 3`，`task.total_bytes = 200000`。

> 这是一个纯推理实践，无需运行；若要验证，可在 `submitTransferTask` 里临时加日志打印每个 slice 的 `length`（属于"源码阅读 + 修改参数观察"型实践，注意不要提交该改动）。

#### 4.4.5 小练习与答案

**练习 1**：同一次 `submitTransfer` 提交了 3 个 request，分别指向 RDMA、TCP、RDMA 三个目标 segment。最终会产生几次 `submitTransferTask` 调用？每次的 task 数是几？

**参考答案**：2 次。因为 `submit_tasks` 按 transport 分组：两个 RDMA task 合成一组、一个 TCP task 单独一组，所以 RDMA Transport 的 `submitTransferTask` 收到 2 个 task，TCP Transport 收到 1 个 task。调用次数 = 涉及的不同 transport 数（这里是 2）。

**练习 2**：`kSubmitWatermark` 的作用是"攒批 flush"。如果把它设得非常大（比如不 flush），会有什么风险？

**参考答案**：一次性把所有 slice 攒在内存里再下发，一是内存峰值变高（大量 Slice 对象 + 待发送描述符），二是可能超过硬件发送队列深度（`max_wr`）导致 `submitPostSend` 失败。水位机制保证"边切边发"，让队列深度和内存占用都受控。

---

### 4.5 完成追踪：原子计数与 getTransferStatus

#### 4.5.1 概念说明

Slice 是异步、并发完成的——10 个 slice 可能由 4 个 worker 线程几乎同时收尾。怎么可靠地回答"这个 task 完成了吗？这个 batch 完成了吗？传了多少字节？"

Mooncake 的方案是**原子计数 + 轮询/事件两种完成语义**：

- **原子计数**：每个 slice 完成时 `markSuccess/markFailed` 原子累加 task 的 `success/failed_slice_count`；
- **轮询完成**：调用方反复调 `getTransferStatus`，用 `success + failed == slice_count` 判定；
- **事件驱动完成**（编译选项 `USE_EVENT_DRIVEN_COMPLETION`）：最后一个 slice 完成时，由它直接唤醒等待整批完成的线程，避免轮询。

此外还有一个"软超时"`slice_timeout`：如果某个 slice 投出后很久没完成，`getTransferStatus` 会把状态改判为 `TIMEOUT`。

#### 4.5.2 核心流程

事件驱动完成的核心在 `Slice::check_batch_completion`（`markSuccess/markFailed` 末尾调用）：

```
check_batch_completion(is_failed):
    若 is_failed：batch.has_failure = true
    prev = task.completed_slice_count++   # 原子递增"已完成 slice 计数"
    if prev + 1 == task.slice_count:      # 我是这个 task 的最后一个完成者
        task.is_finished = true
        prev_task = batch.finished_task_count++   # 整批里又完成一个 task
        if prev_task + 1 == batch.batch_size:     # 我是整批最后一个 task
            加锁 { batch.is_finished = true (release) }
            batch.completion_cv.notify_all()      # 唤醒等待者
```

关键巧思：**"最后一个完成者"由原子计数自动选举**。`fetch_add` 返回的是旧值，所以只有把 `completed_slice_count` 从 `slice_count-1` 推到 `slice_count` 的那个线程，会看到 `prev+1 == slice_count`，由它来发布完成。这样无需任何额外锁就能精确地只通知一次。

注意内存序的精细设计（见源码注释）：

- 中间的 `fetch_add` 都是 `relaxed`——它们不负责发布数据；
- 真正发布可见性的是最后对 `batch_desc.is_finished` 的 `release` store，与等待方在 `getBatchTransferStatus` 里的 `acquire` load 配对。这保证了"等待方看到 `is_finished==true` 时，之前所有 relaxed 累加写入都对其可见"。

#### 4.5.3 源码精读

事件驱动完成的实现：

[`mooncake-transfer-engine/include/transport/transport.h:193-245`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transport/transport.h#L193-L245) — `Slice::check_batch_completion`，用 `completed_slice_count` 选举最后完成者，逐层发布 task 与 batch 的完成。

```cpp
inline void check_batch_completion(bool is_failed) {
#ifdef USE_EVENT_DRIVEN_COMPLETION
    auto &batch_desc = toBatchDesc(task->batch_id);
    if (is_failed)
        batch_desc.has_failure.store(true, std::memory_order_relaxed);

    uint64_t prev_completed = __atomic_fetch_add(
        &task->completed_slice_count, 1, __ATOMIC_RELAXED);

    if (prev_completed + 1 == task->slice_count) {        // task 最后一个 slice
        __atomic_store_n(&task->is_finished, true, __ATOMIC_RELAXED);

        auto prev = batch_desc.finished_task_count.fetch_add(
            1, std::memory_order_relaxed);
        if (prev + 1 == batch_desc.batch_size) {          // batch 最后一个 task
            {
                std::lock_guard<std::mutex> lock(batch_desc.completion_mutex);
                batch_desc.is_finished.store(true, std::memory_order_release);
            }
            batch_desc.completion_cv.notify_all();
        }
    }
#endif
}
```

`getTransferStatus` 则是上层查询入口，有两条路径：

[`mooncake-transfer-engine/src/multi_transport.cpp:195-261`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/multi_transport.cpp#L195-L261) — `MultiTransport::getTransferStatus`。

```cpp
Status MultiTransport::getTransferStatus(BatchID batch_id, size_t task_id,
                                         TransferStatus& status) {
    auto& task = batch_desc.task_list[task_id];
    // 软超时检查：slice 投出超过 slice_timeout 秒未完成 → TIMEOUT
    auto checkSliceTimeout = [&](const TransferTask& t) -> bool {
        if (globalConfig().slice_timeout <= 0) return false;
        ...
        for (auto& slice : t.slice_list) {
            if (ts > 0 && current_ts - ts > kPacketDeliveryTimeout)
                return true;
        }
        return false;
    };
    // 主路径：委托 task 所属 transport 做协议相关轮询（如 NVLink 的 cudaStreamQuery）
    if (task.transport_) {
        auto ret = task.transport_->getTransferStatus(batch_id, task_id, status);
        if (!ret.ok()) return ret;
        if (status.s == WAITING && checkSliceTimeout(task))
            status.s = TIMEOUT;
        return Status::OK();
    }
    // 回退路径：直接看原子计数（无 transport 指针的遗留路径）
    ...
    if (success_slice_count + failed_slice_count == task.slice_count) {
        status.s = failed_slice_count ? FAILED : COMPLETED;
        task.is_finished = true;
    } else {
        status.s = checkSliceTimeout(task) ? TIMEOUT : WAITING;
    }
    return Status::OK();
}
```

这就是"如何判断完成"的完整答案：

1. 委托给 task 所属 transport（`task.transport_`）的 `getTransferStatus`，让协议自己先推进状态（有些协议必须主动 poll 才会更新 slice 状态，如 4.3 练习所述）；
2. transport 内部用回退路径同样的判定式 `success + failed == slice_count` 来定 COMPLETED / FAILED；
3. 在此基础上叠加软超时 `TIMEOUT`。

回退路径（无 transport 指针）的判定式与各具体 Transport 内部一致，例如 RDMA：

[`mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp:656-679`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp#L656-L679) — `RdmaTransport::getTransferStatus`（单 task 重载），与回退路径完全同构。

```cpp
if (success_slice_count + failed_slice_count == task.slice_count) {
    if (failed_slice_count) status.s = FAILED;
    else                    status.s = COMPLETED;
    task.is_finished = true;
} else {
    status.s = WAITING;
}
```

整批完成查询 `getBatchTransferStatus` 还有一个快速路径：先看 `batch.is_finished` 的 acquire load，若已置位则直接返回，不必逐 task 轮询：

[`mooncake-transfer-engine/src/multi_transport.cpp:263-275`](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/multi_transport.cpp#L263-L275) — `getBatchTransferStatus` 的快速路径，与 4.5 事件驱动的 release store 配对。

```cpp
if (batch_desc.is_finished.load(std::memory_order_acquire) || task_count == 0) {
    status.s = COMPLETED;
    status.transferred_bytes =
        batch_desc.finished_transfer_bytes.load(std::memory_order_relaxed);
    return Status::OK();
}
```

这里的 `acquire` 正好配对 `check_batch_completion` 末尾的 `release` store——这就是前文说的"可见性由 acquire/release 配对保证"。

#### 4.5.4 代码实践

**实践目标**：跟踪"最后一个 slice 完成时如何唤醒等待者"，理解原子选举与 acquire/release 配对。

**操作步骤**：

1. 阅读 `transport.h:193-245`（`check_batch_completion`），标注出三个原子操作的作用：`completed_slice_count`、`finished_task_count`、`is_finished`。
2. 阅读 `multi_transport.cpp:269`（`getBatchTransferStatus` 的 acquire load），找到它配对的 release store 在 `transport.h:236-237`。
3. 思考：若把 `is_finished` 的 release 改成 relaxed，等待方可能在看到 `is_finished==true` 时却读到旧的 `finished_transfer_bytes`（即 0）。请确认 `finished_transfer_bytes` 在 release 之前是否已经写入。

**需要观察的现象**：完成发布严格遵循"先写数据（relaxed 的累加/赋值）→ 最后一次 release store 标志位"的顺序；等待方"acquire load 标志位 → 读数据"，二者配对保证不读到半成品状态。

**预期结果**：你能画出"slice 完成 → task 完成 → batch 完成"的三级原子选举链路，并解释为什么中间步骤可以 relaxed、只有末端需要 release。

> 待本地验证：若想运行验证，可启用 `USE_EVENT_DRIVEN_COMPLETION` 编译选项后跑 `mooncake-transfer-engine/tests/rdma_loopback_test.cpp` 之类的回环测试，观察完成通知是否在最后一条 slice 收尾后发出。

#### 4.5.5 小练习与答案

**练习 1**：`check_batch_completion` 里，为什么判断"我是 task 的最后一个完成者"用的是 `prev_completed + 1 == slice_count`，而不是 `completed_slice_count == slice_count`？

**参考答案**：因为多个线程并发完成 slice 时，若直接读 `completed_slice_count` 再比较，会有"多个线程都读到等于 slice_count"的竞态，导致重复发布。而 `__atomic_fetch_add` 返回旧值 `prev`，"把计数从 slice_count−1 推到 slice_count"的线程**唯一**地满足 `prev+1 == slice_count`，从而保证只有它进入发布分支。这是无锁选举的标准技巧。

**练习 2**：软超时 `slice_timeout` 默认是 `-1`（关闭）。如果开启（设为正数秒），会把状态改判为 `TIMEOUT`，这会真的取消传输吗？

**参考答案**：不会。`checkSliceTimeout` 只是**改判 `getTransferStatus` 返回的状态枚举**（从 `WAITING` 改为 `TIMEOUT`），它并不主动中止底层传输。底层 slice 仍可能在之后完成并调用 `markSuccess`。`TIMEOUT` 更多是给上层一个"等太久了，可以认为失败并重试"的决策信号，是否真正放弃由上层根据这个状态自行决定。

---

## 5. 综合实践

把本讲的知识串起来，完成下面这个"对象关系 + 完成判定"的综合任务（这正是本讲规格指定的实践）。

**任务**：画出一批 `TransferRequest` 如何被切分为 Slice、聚合为 Task、再汇入 Batch 的**对象关系图**，并解释 `getTransferStatus` 如何判断完成。

**步骤 1：构造一个具体场景**

假设调用方执行：

```cpp
// 示例代码（非项目原有，仅用于说明对象关系）
BatchID b = engine->allocateBatchID(4);            // 批容量 4
std::vector<TransferRequest> reqs(3);
reqs[0] = {WRITE, src0, seg_rdma,  0,      200000}; // 200KB → RDMA 段
reqs[1] = {READ,  src1, seg_tcp,   4096,    50000}; //  50KB → TCP 段
reqs[2] = {WRITE, src2, seg_rdma2, 0,      100000}; // 100KB → 另一 RDMA 段
engine->submitTransfer(b, reqs);
```

**步骤 2：画出对象关系图**

依据 4.4 的源码逻辑，关系图应如下（`slice_size=64KB`、`fragment_limit=16KB`）：

```
BatchDesc(b)  batch_size=4, task_list 容量扩到 3
│
├── Task[0] (reqs[0], transport_=RdmaTransport)
│     slice_count=3   (按 4.4.4 的推算：65536 / 65536 / 68928)
│     ├── Slice[0][0]  rdma.*   task=&Task[0]
│     ├── Slice[0][1]  rdma.*   task=&Task[0]
│     └── Slice[0][2]  rdma.*   task=&Task[0]
│
├── Task[1] (reqs[1], transport_=TcpTransport)
│     slice_count=1   (50000 ≤ 81920，单 slice 直接合并吃完并 break)
│     └── Slice[1][0]  tcp.*    task=&Task[1]
│
└── Task[2] (reqs[2], transport_=RdmaTransport)
      slice_count=2   (65536 / 34464：第二片剩余 34464 ≤ 81920，合并)
      ├── Slice[2][0]  rdma.*   task=&Task[2]
      └── Slice[2][1]  rdma.*   task=&Task[2]
```

投递分组：`submit_tasks` 里有两个分组——`RdmaTransport → [Task[0], Task[2]]`、`TcpTransport → [Task[1]]`，分别调用各自的 `submitTransferTask`，两组**互不阻塞**（这就是跨协议并行）。

**步骤 3：解释 getTransferStatus 如何判断完成**

以查询 `Task[0]` 为例：

1. `MultiTransport::getTransferStatus` 取出 `Task[0]`，发现 `task.transport_` 非空（`RdmaTransport`），委托给它（`multi_transport.cpp:228-231`）。
2. `RdmaTransport::getTransferStatus` 读取原子计数：当 `success_slice_count + failed_slice_count == slice_count`（=3）时判定为终态——`failed>0` 则 `FAILED`，否则 `COMPLETED`，并置 `is_finished=true`（`rdma_transport.cpp:669-677`）。
3. 这 3 个 slice 是被后台 worker **并发**调用 `slice->markSuccess()` 推进的，每次 `markSuccess` 原子累加 `success_slice_count` 和 `transferred_bytes`（`transport.h:174-181`）。
4. 若启用了事件驱动完成，第 3 个完成的 slice 会在 `check_batch_completion` 里进一步选举发布 task/batch 完成（`transport.h:193-245`）。
5. 若 `slice_timeout > 0` 且某 slice 超时未归，状态会被叠加改判为 `TIMEOUT`（`multi_transport.cpp:234-237`）。

**步骤 4：验证你的图**

- 检查每个 Task 的 `slice_count` 是否等于它 `slice_list.size()`；
- 检查所有 Task 的 slice 之和是否等于"按 4.4.2 尾部合并规则"手算的值；
- 检查每个 Slice 的 `task` 反向指针都正确指向所属 Task。

**预期结果**：你得到一张"1 个 Batch → 3 个 Task → 共 6 个 Slice（3+1+2），跨 2 种协议"的关系图，并能说清"完成 = 所有 slice 走完终态 + 原子计数求和等于 slice_count"。

> 待本地验证：若想用真实代码验证对象数量，可在 `RdmaTransport::submitTransferTask` 的循环里临时 `LOG(INFO) << "slice len=" << slice->length;`，跑一次回环传输后统计输出条数（注意这只是观察手段，勿提交该日志改动）。

## 6. 本讲小结

- `Transport` 是所有协议的抽象基类，对外只暴露 **分配/释放 Batch、提交请求、查询状态** 三类接口；`BatchID` 是 `BatchDesc*` 的裸指针重解释，热路径零查表。
- `TransferRequest` 用 `READ`/`WRITE` 描述一次"本地↔远端 segment"的搬运意图，源地址是绝对指针、目标是 segment ID + 偏移。
- `Slice` 是下发到硬件的最小单元，用 **union** 承载 9+ 种协议的特化字段，外壳统一、内核各异，从而支持一批请求混合多协议。
- 层级是 `BatchDesc → TransferTask → Slice`：一个请求一个 Task、一个 Task 切成多个 Slice；Task 按值存于 Batch，Slice 按指针存于 Task。
- 切分由具体 Transport 的 `submitTransferTask` 完成，按 `slice_size`（默认 64KB）切，并用 `fragment_limit`（默认 16KB）做**尾部合并**减少碎片；`MultiTransport::submitTransfer` 按 transport 分组下发，实现跨协议并行。
- 完成追踪靠**原子计数**：slice 完成时 `markSuccess/markFailed` 累加 `success/failed_slice_count`，`getTransferStatus` 用 `success + failed == slice_count` 判定 COMPLETED/FAILED；事件驱动模式下由"最后一个完成者"经原子选举发布 batch 完成并唤醒等待者，可见性由 release/acquire 配对保证。

## 7. 下一步学习建议

本讲建立的是"对象模型与完成追踪"的骨架。建议下一步：

1. **挑一个具体 Transport 精读**：推荐从 `RdmaTransport` 入手（`rdma_transport.cpp` + `rdma_endpoint.cpp` + `worker_pool.cpp`），看 `submitPostSend` 如何把 slice 变成真实的 RDMA Work Request、worker 如何从完成队列（CQ）回收并回调 `markSuccess`。这会把本讲的 `Slice::markSuccess` 接到真实硬件回路上。
2. **阅读 metadata / segment 机制**：本讲反复提到"目标 segment 的 protocol 决定选路"。下一讲可以进入 `transfer_metadata.h`，看 `SegmentDesc`、`BufferDesc`（含 `lkey`/`rkey`）是如何被注册和交换的，理解 `slice->rdma.dest_rkey` 这类字段从何而来。
3. **对比事件驱动 vs 轮询完成**：如果你对并发感兴趣，可以围绕 `USE_EVENT_DRIVEN_COMPLETION` 这个编译开关，对比开启/关闭时 `getBatchTransferStatus` 的行为差异，深入理解本讲 4.5 的 acquire/release 配对设计。
