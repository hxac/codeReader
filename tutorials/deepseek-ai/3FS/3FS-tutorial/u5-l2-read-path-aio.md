# 读路径：批量读与 AIO

## 1. 本讲目标

本讲深入 3FS storage 服务的**读数据路径**。读完本讲，你应当能够：

- 说清一个 `batchRead` 请求从「进入 RPC」到「数据经 RDMA 回传给客户端」经历的完整阶段。
- 理解 CRAQ 链式复制下，**读可以打链上任意 target** 的原因，以及 storage 如何用 `TargetMap` 把 `chainId` 解析成本地 target。
- 掌握 `BufferPool` 如何池化、按需分配 RDMA buffer，以及为什么读 SSD 时要做 4096 字节对齐（head/tail 裁剪）。
- 看懂 `AioReadWorker` / `BatchReadJob` 如何用 libaio 或 io_uring 把一批读「收集 → 提交 → 收割」地异步落盘。

本讲只讲**读**，写路径与 CRAQ 链式复制留给 [u5-l3](u5-l3-write-path-craq.md)，数据恢复留给 [u5-l5](u5-l5-data-recovery.md)。

## 2. 前置知识

在进入源码前，先用三段白话建立直觉。

**（1）为什么读要走这条「先读 SSD 再 RDMA 回写」的路？**
普通 RPC 回包是把数据拷进发送缓冲再序列化发出。但 3FS 一次 `batchRead` 动辄几十 MB，拷贝本身就会吃满 CPU 和内存带宽。因此 storage 采用**零拷贝**思路：先在本地申请一块「已注册（pinned + `ibv_reg_mr`）」的 RDMA buffer，让 AIO 直接把 SSD 数据读进这块 buffer，再用 RDMA Write（单边操作）把这块 buffer 原地推到客户端预留的远端 buffer 里。数据自始至终没有跨过用户态拷贝。

**（2）什么是 libaio / io_uring？**
两者都是 Linux 的异步 I/O 接口。libaio（`io_setup` / `io_submit` / `io_getevents`）是经典方案；io_uring（`io_uring_submit` / `io_uring_wait_cqes`）是更新的共享环形队列方案，且支持「预注册文件描述符」「预注册 buffer（fixed buffers）」，省掉每次 I/O 的内核地址校验，吞吐更高。3FS 两者都支持，默认按配置切换。

**（3）什么是直接 I/O（O_DIRECT）与对齐？**
绕过页缓存直接读写磁盘（O_DIRECT）时，内核要求 `偏移`、`长度`、`缓冲区地址` 三者都必须按块大小对齐（3FS 取 `kAIOAlignSize = 4096`）。但客户端请求的 `offset`、`length` 往往不是 4096 的整数倍。于是 storage 把读区间向**两侧扩展到对齐边界**再读，读回来后再把多读的「头」「尾」裁掉。

**承接认知**：本讲假定你已学过 [u5-l1](u5-l1-storage-overview.md)，知道 storage 服务由 `Components` 聚合、`StorageOperator` 是数据面 RPC 的中央协调者、读写流量经 `readPool`/`updatePool` 等隔离协程池分流、`StorageTarget` 是 target 在单机的化身。本讲就是顺着 `StorageOperator::batchRead` 这条主线往下走。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/storage/service/StorageOperator.cc` / `.h` | 读路径总入口 `batchRead`，串联路由→分配→AIO→回传四阶段。 |
| `src/storage/service/TargetMap.h` | 读路由：`AtomicallyTargetMap::snapshot()` + `getByChainId()` 把链 id 解析成本地 target。 |
| `src/storage/service/BufferPool.cc` / `.h` | RDMA buffer 池：预注册、按需分配、大小池分级、4096 对齐。 |
| `src/storage/aio/BatchReadJob.cc` / `.h` | 一个批量读任务：聚合成 `AioReadJob`、对齐计算、完成同步、回传组装。 |
| `src/storage/aio/AioReadWorker.cc` / `.h` | AIO 执行线程：多线程事件循环，驱动 collect→submit→reap。 |
| `src/storage/aio/AioStatus.cc` / `.h` | libaio / io_uring 的具体实现。 |
| `src/storage/store/ChunkReplica.cc` / `StorageTarget.cc` | AIO 的「前/后处理」：取 chunk 元数据、设置 fd/offset/length、完成后校验版本。 |
| `src/fbs/storage/Common.h` | 数据结构定义：`ReadIO` / `BatchReadReq` / `IOResult`，以及 `kAIOAlignSize`。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：**读路由**、**RDMA buffer**、**异步批量读**。它们恰好对应 `batchRead` 里顺序执行的三个代码段。

### 4.1 读路由：从 chain 解析到 target

#### 4.1.1 概念说明

CRAQ（Chain Replication with Apportioned Queries）的一个核心性质是「**写全读任何（write-all, read-any）**」：写必须从链头沿链串行传播，但读可以打链上**任意一个**已经提交的副本。这带来一个直接结论——客户端做读时不必挑链头，storage 节点收到读请求后，只要这个 chunk 所属的 `chainId` 在本地有对应的 target，就能直接服务。

于是「读路由」要做的事很简单：给定客户端请求里的 `(chainId, chainVer)`，在本地 `TargetMap` 里查出对应的 `Target`，并校验这个 target 现在确实可读（public 状态在服务、local 状态已同步）。注意这里**没有跨网络转发**——不像写路径要把请求沿链传给后继，读完全在单机闭环。

一个 `BatchReadReq` 里装的是**一批** `ReadIO`，每个 IO 可能属于不同的 chain。为避免这一批读读到「半新半旧」的路由视图，storage 在批次开头取**一次** `TargetMap` 快照，整批复用。

#### 4.1.2 核心流程

```
batchRead 入口
  ├─ snapshot = targetMap.snapshot()        # 整批共用一份原子快照
  └─ for 每个 ReadIO:
       ├─ target = snapshot->getByChainId(vChainId, ignoreChainVer?)  # chainId → 本地 Target
       ├─ 校验 chain 版本（拒绝过期链，除非配置放行）
       ├─ 校验 target->upToDate()            # public=SERVING 且 local=UPTODATE
       └─ job.state().storageTarget = target->storageTarget.get()    # 绑定到具体存储对象
```

#### 4.1.3 源码精读

`batchRead` 的入口与「准备 target」阶段：

[src/storage/service/StorageOperator.cc:82-131](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L82-L131) —— `batchRead` 函数开头：先 `targetMap.snapshot()` 取整批共用的快照，再循环为每个 `ReadIO` 解析 target 并校验状态。

关键几行：

[src/storage/service/StorageOperator.cc:90](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L90) —— 取 `TargetMap` 快照（`atomic_shared_ptr` 无锁读取）。

[src/storage/service/StorageOperator.cc:103-106](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L103-L106) —— 用 `snapshot->getByChainId(...)` 按 `(chainId, chainVer)` 解析本地 target；第二个参数来自配置项 `batch_read_ignore_chain_version`，用于恢复期放行版本不匹配的链。

[src/storage/service/StorageOperator.cc:113-117](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L113-L117) —— `target->upToDate()` 校验：target 必须同时 public 状态可服务、local 状态已同步，否则报 `kTargetStateInvalid`。

`AtomicallyTargetMap` 提供快照与查询：

[src/storage/service/TargetMap.h:83](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/TargetMap.h#L83) —— `snapshot()` 返回 `atomic_shared_ptr<const TargetMap>` 的当前值，读侧无锁。

[src/storage/service/TargetMap.h:86-87](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/TargetMap.h#L86-L87) 与 [src/storage/service/TargetMap.h:35](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/TargetMap.h#L35) —— 两层 `getByChainId`：`TargetMap::getByChainId` 接收 `VersionedChainId` 并做链版本校验，返回 `const Target *`。

> 细节：读路径直接调用快照上的 `TargetMap::getByChainId`（返回裸指针），而写路径（`write`/`update`）调用 `AtomicallyTargetMap::getByChainId`（返回 `shared_ptr<const Target>`）。差异在于读路径已经持有了整批快照，target 生命周期由快照保证；写路径每次单独查询、需要 `shared_ptr` 保活。

`ReadIO` 携带的客户端远端 buffer 是后面 RDMA 回传的目标：

[src/fbs/storage/Common.h:309-315](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/storage/Common.h#L309-L315) —— `ReadIO` 含 `offset`、`length`、`key`（`GlobalKey = VersionedChainId + chunkId`）、`rdmabuf`（客户端预注册的远端 buffer，含 `addr` 与 `rkey`）。

#### 4.1.4 代码实践

**实践目标**：在源码上标注出「读路由」的全部落点，理解为何读无需跨网络转发。

**操作步骤（源码阅读型实践）**：

1. 打开 `src/storage/service/StorageOperator.cc`，定位 `batchRead`（第 82 行起）。
2. 在第 90 行 `snapshot` 处画一个标记：**「整批读的统一路由视图」**。
3. 跟进 `snapshot->getByChainId` 到 `src/storage/service/TargetMap.h:35`，确认它只查本地 `chainToTarget_` 映射（见 [TargetMap.h:76](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/TargetMap.h#L76) 的 `chainToTarget_` 字段），没有任何网络调用。
4. 对比第 113 行 `target->upToDate()` 与写路径：确认读只校验本机 target 状态，不转发。

**需要观察的现象**：你会看到从第 82 行到第 130 行的整段「准备 target」循环里，没有任何 `co_await` 触发网络 RPC——所有判断都基于本地内存里的快照。这正是「读打任意 target、单机闭环」的代码体现。

**预期结果**：能在源码上清晰指出「快照点（L90）→ 解析点（L106）→ 状态校验点（L113）→ 绑定点（L118）」四处。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `batchRead` 要在批次开头取一次快照，而不是每个 IO 各取一次？
**参考答案**：保证整批读看到一致的路由视图；同时减少对 `atomic_shared_ptr` 的原子读开销。若每个 IO 各取，可能在批次中途 target 状态翻转，导致同批读到不一致结果。

**练习 2**：`batch_read_ignore_chain_version` 配置项（默认 `false`）在什么场景下会被打开？
**参考答案**：在数据恢复/链成员变更期间，客户端持有的 `chainVer` 可能滞后于服务端，此时放行版本校验可避免读大面积失败；正常服务期保持 `false` 以拒绝过期读。

---

### 4.2 RDMA buffer：BufferPool 的分配与对齐

#### 4.2.1 概念说明

读要走零拷贝，就必须有一块「已注册」的 RDMA buffer 等着接收 SSD 数据。但 RDMA 注册（pin 内存 + 调用 `ibv_reg_mr`）很贵，不能每次读都注册。于是 `BufferPool` 在**启动时**一次性注册一大池 buffer，运行期只做「借出/归还」。

`BufferPool` 内部分两个池：

- **小 buffer 池**：默认 1024 个，每个 4 MB（`rdmabuf_size` / `rdmabuf_count`）。
- **大 buffer 池**：默认 64 个，每个 64 MB（`big_rdmabuf_size` / `big_rdmabuf_count`），给超大单次读兜底。

并发用 `folly::fibers::Semaphore` 限流：每个 buffer 槽位对应一个令牌，分配时拿令牌，归还时还令牌。这样即便大量协程并发读，也不会一次性把内存池掏空。

**对齐**：直接 I/O 要求 buffer 地址、读偏移、读长度都按 4096 对齐。但客户端 `offset` 通常不对齐。解决办法是：把读区间向**左**扩到对齐边界（多读一个「头 headLength」），向**右**补齐到对齐边界（多读一个「尾 tailLength」），读回来后再裁掉头尾。设块大小 \(A = 4096 \)，则：

\[
\text{headLength} = \text{offset} \bmod A
\]

\[
\text{alignedOffset} = \text{offset} - \text{headLength}
\]

\[
\text{tailLength} = (A - (\text{offset} + \text{length}) \bmod A) \bmod A
\]

\[
\text{alignedLength} = \text{length} + \text{headLength} + \text{tailLength}
\]

读完后回传时用 `subrange(headLength, length)` 把有效数据从对齐缓冲里抠出来。

#### 4.2.2 核心流程

```
启动期（Components::start）
  └─ rdmabufPool.init(procThreadPool)   # 并行注册大小两个 RDMA buffer 池
       └─ 每个 buffer 经 RDMABufPool::allocate() 完成 pin + reg_mr
       └─ alignBuffer() 把首地址裁到 4096 对齐
       └─ 切成 rdmabufSize 的片，登记进 freeIndex_

运行期（batchRead 准备 buffer）
  ├─ buffer = rdmabufPool.get()         # 拿一个 RAII 句柄（析构自动归还）
  └─ for 每个 job:
       ├─ buffer.tryAllocate(alignedLength)   # 快路径：try_wait，不阻塞
       └─ 失败则 co_await buffer.allocate(...)  # 慢路径：co_wait，必要时借大 buffer
```

#### 4.2.3 源码精读

`BufferPool::Config` 定义两个池的容量：

[src/storage/service/BufferPool.h:23-28](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/BufferPool.h#L23-L28) —— 小池 `4_MB * 1024`、大池 `64_MB * 64`。

启动期初始化与对齐裁剪：

[src/storage/service/BufferPool.cc:29-51](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/BufferPool.cc#L29-L51) —— `init()`：分别用 `initBuffers` 注册小池与大池，再把每个 buffer 的 `(ptr, size)` 收集进 `iovecs_`（供 io_uring 预注册 fixed buffer 用）。

[src/storage/service/BufferPool.cc:17-25](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/BufferPool.cc#L17-L25) —— `alignBuffer()`：把 buffer 首地址向前进位到 4096 对齐，保证 AIO 落盘时缓冲地址合规。

分配的快/慢两条路径：

[src/storage/service/BufferPool.cc:102-119](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/BufferPool.cc#L102-L119) —— `Buffer::tryAllocate(size)`：用 `semaphore_.try_wait()` 非阻塞拿令牌，成功则从空闲片里切出 `size` 字节，返回 `RDMABuf`；失败返回 `kRDMANoBuf`（调用方据此转慢路径）。

[src/storage/service/BufferPool.cc:121-141](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/BufferPool.cc#L121-L141) —— `Buffer::allocate(size)`：协程版，`co_await semaphore_.co_wait()` 阻塞等令牌；若 `size > rdmabufSize_` 则改借大池（`bigSemaphore_` + `allocateBig`）。

调用方在 `batchRead` 里的实际用法：

[src/storage/service/StorageOperator.cc:139-155](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L139-L155) —— **buffer 分配点**：先 `tryAllocate`（快），失败才 `co_await allocate`（慢），分配结果存进 `job.state().localbuf`。

对齐的头尾计算在每个 `AioReadJob` 构造时完成：

[src/storage/aio/BatchReadJob.cc:16-22](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/BatchReadJob.cc#L16-L22) —— 构造函数按上面的公式算出 `headLength`、`tailLength`。

[src/storage/aio/BatchReadJob.h:60-61](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/BatchReadJob.h#L60-L61) —— `alignedOffset()` / `alignedLength()` 暴露对齐后的偏移与长度，供 AIO 与元数据前处理使用。

对齐常量：

[src/fbs/storage/Common.h:80](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/storage/Common.h#L80) —— `kAIOAlignSize = 4096`。

#### 4.2.4 代码实践

**实践目标**：算出一个具体读请求的对齐布局，并在源码上确认裁剪点。

**操作步骤（推演型实践）**：

1. 假设客户端请求 `offset = 8192 + 100 = 8292`，`length = 5000`（均非 4096 对齐）。
2. 套用公式手工计算：
   - `headLength = 8292 mod 4096 = 100`
   - `alignedOffset = 8292 - 100 = 8192`
   - 请求终点 `= 8292 + 5000 = 13292`，`13292 mod 4096 = 1004`
   - `tailLength = (4096 - 1004) mod 4096 = 3092`
   - `alignedLength = 5000 + 100 + 3092 = 8192`（恰好 2 个块，对齐）
3. 打开 [BatchReadJob.cc:16-22](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/BatchReadJob.cc#L16-L22) 对照你的手算结果。
4. 打开 [BatchReadJob.cc:33](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/BatchReadJob.cc#L33) 与 [BatchReadJob.cc:80](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/BatchReadJob.cc#L80) —— 确认读回来后用 `subrange(headLength, length)` 把有效数据从对齐缓冲里抠出。

**需要观察的现象**：`alignedLength` 总是 4096 的整数倍，且 `alignedOffset` 也总被 4096 整除——这正是 O_DIRECT 的硬性要求。

**预期结果**：手算值与源码逻辑一致；理解「多读的头尾在回传时被丢弃」。

> 说明：以上数值为示例推演，未在真实集群运行；公式来自源码 [BatchReadJob.cc:20-21](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/BatchReadJob.cc#L20-L21)。

#### 4.2.5 小练习与答案

**练习 1**：为什么要把 buffer 池分成「小池 + 大池」两级，而不是一个统一池？
**参考答案**：绝大多数读 ≤ 4 MB，用小池（数量多、令牌多）可支持高并发；偶发的超大读借大池（64 MB）兜底，避免为大请求预留大量小片、也避免单个大请求挤占小池令牌导致小请求饿死。

**练习 2**：`tryAllocate` 失败后为什么直接 `co_await allocate` 而不是报错？
**参考答案**：`tryAllocate` 用 `try_wait` 非阻塞拿令牌，失败只代表「此刻没有空闲片」，不代表系统不可用。转 `allocate`（`co_wait`）挂起协程等令牌即可，协程挂起不阻塞线程，整体吞吐不受影响。

---

### 4.3 异步批量读：AioReadWorker 与 BatchReadJob

#### 4.3.1 概念说明

buffer 就绪后，真正读 SSD 的活交给 `AioReadWorker`。它的设计是**典型的批量异步 I/O 模式**：

- 把一批 `AioReadJob` 收集起来（collect），一次性提交给内核（submit），再批量收割完成事件（reap）。
- 批量提交能摊薄系统调用开销、让 SSD 并行处理，是高 IOPS 的关键。
- 多个 worker 线程（默认 32）各自跑独立的事件循环，分摊磁盘队列压力。

`BatchReadJob` 是「一批读」的逻辑聚合体：内含若干 `AioReadJob`、一把用于等待全部完成的 `Baton`、一个完成计数器。当最后一个 job 完成，`finish()` 投递 `Baton`，等待方（`batchRead` 协程）被唤醒，进入回传阶段。

为了让单次 AIO 批次不过大（避免内核队列爆掉），`batchRead` 会按 `batch_read_job_split_size`（默认 1024）把整批切成多个子批次分别入队。

每个 `AioReadJob` 在被 AIO 真正提交前，还要先做**元数据前处理**：根据 `chunkId` 查出 chunk 在磁盘文件里的实际位置（`readFd`、`readOffset`）、校验 `commitVer == updateVer`（只读已提交版本，除非请求带 `ALLOW_READ_UNCOMMITTED`）。完成后还要做**后处理**：再次校验版本没被并发写改掉，并按需计算校验和。

> libaio 与 io_uring 的差异：io_uring 在启动时把全部 target 的 fd 与全部 buffer 都**预注册**进 ring（`io_uring_register_files` / `io_uring_register_buffers`），运行期每个读只用一个整数索引（`fdIndex`、`bufferIndex`）就能发起 I/O，省掉内核每次的权限/地址校验。这就是 `BufferPool::iovecs()` 和 `storageTargets.fds()` 在启动时要一起传给 `aioReadWorker.start()` 的原因。

#### 4.3.2 核心流程

```
batchRead
  ├─ 按 splitSize 切子批次
  └─ co_await aioReadWorker.enqueue(子批次迭代器)   # ★ AIO 提交点（入口）

AioReadWorker 每个工作线程 run():
  while true:
    ├─ it = queue_.dequeue()              # 取一个子批次（阻塞等待）
    ├─ do:
    │    ├─ status.collect()              # 对每个 job: aioPrepareRead 取元数据 + 填 iocb/SQE
    │    ├─ status.submit()               # io_submit / io_uring_submit
    │    └─ while inflight: status.reap() # io_getevents / io_uring_wait_cqes
    └─ until 子批次全部完成

每个 IO 完成回调 setReadJobResult:
  ├─ 裁剪长度（减 headLength，夹到 length 与 chunkLen 内）
  ├─ job.setResult(length) → 算校验和 + aioFinishRead 再校验版本
  └─ batch.finish(job)                   # 计数++，全完成则 Baton.post()

batchRead 被唤醒后（回传阶段）
  ├─ SEND_DATA_INLINE   → copyToRespBuffer（数据内联进 RPC 回包）
  └─ 否则               → addBufferToBatch + writeBatch.post()  # ★ RDMA 回传
                          （受 per-IB-device 信号量 max_concurrent_rdma_writes 限流）
```

#### 4.3.3 源码精读

**入口：切分子批次并入队**——

[src/storage/service/StorageOperator.cc:162-169](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L162-L169) —— **AIO 入队点**：按 `batch_read_job_split_size` 切片，每个子批次 `co_await components_.aioReadWorker.enqueue(...)`。（注：上面 157–161 行的 `BYPASS_DISKIO` 是测试/探活用的旁路，直接返回 `length` 不读盘。）

[src/storage/service/StorageOperator.cc:171-174](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L171-L174) —— `co_await batch.complete()` 等待全部 job 完成（`Baton`）。

**`AioReadWorker` 的多线程事件循环**——

[src/storage/aio/AioReadWorker.h:26-50](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/AioReadWorker.h#L26-L50) —— 配置：`num_threads` 默认 32、`queue_size` 4096、`max_events` 512、`min_complete` 128、`ioengine` 默认 libaio（`enable_io_uring` 为 true 时按 `useIoUring()` 随机/强制选 io_uring）。

[src/storage/aio/AioReadWorker.cc:60-94](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/AioReadWorker.cc#L60-L94) —— `run()`：取子批次 → `collect → submit → reap` 循环，直到子批次全部完成（`hasUnfinishedBatchReadJob()` 为假）。

**collect / submit / reap 的 libaio 实现**——

[src/storage/aio/AioStatus.cc:86-106](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/AioStatus.cc#L86-L106) —— `AioStatus::collect()`：对每个 job 调 `storageTarget->aioPrepareRead(job)` 取元数据，再用 `io_prep_pread(iocb, readFd, localbuf.ptr(), readLength, readOffset)` 填一个 `iocb`，把 job 指针存进 `iocb->data` 作回调上下文。

[src/storage/aio/AioStatus.cc:108-151](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/AioStatus.cc#L108-L151) —— `AioStatus::submit()`：`io_submit` 一次性提交积累的 `iocb`，处理 `-EAGAIN`（重试）、`-EBADF`（坏 fd）等错误。

[src/storage/aio/AioStatus.cc:153-173](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/AioStatus.cc#L153-L173) —— `AioStatus::reap()`：`io_getevents` 批量收割完成事件，对每个事件调 `setReadJobResult(event.data, event.res)`。

**io_uring 实现（用 fixed file / fixed buffer）**——

[src/storage/aio/AioStatus.cc:214-243](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/AioStatus.cc#L214-L243) —— `IoUringStatus::collect()`：`io_uring_prep_read_fixed(sqe, fdIndex, buf, len, off, bufferIndex)`，带 `IOSQE_FIXED_FILE` 标志，全程用整数索引引用预注册的 fd 与 buffer。

[src/storage/aio/AioStatus.cc:181-212](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/AioStatus.cc#L181-L212) —— `IoUringStatus::init()`：`io_uring_register_files` + `io_uring_register_buffers` 把 fd 与 buffer 预注册进 ring（注册的数据来自 `aioReadWorker.start(fds, iovecs)`，见 [Components.cc:54](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.cc#L54)）。

**完成回调与长度裁剪**——

[src/storage/aio/AioStatus.cc:27-55](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/AioStatus.cc#L27-L55) —— `setReadJobResult()`：`res >= 0` 时按 `min(min(max(0, res - headLength), length), max(0, chunkLen - offset))` 把 AIO 返回的对齐长度裁成客户端要的有效长度，再交给 `job.setResult(length)`。

[src/storage/aio/BatchReadJob.cc:24-63](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/BatchReadJob.cc#L24-L63) —— `AioReadJob::setResult()`：按 `checksumType` 计算校验和（整 chunk 读则直接复用 chunk 元数据里的 checksum），调 `storageTarget->aioFinishRead(*this)` 再次校验版本未被并发写改掉，最后 `batch_.finish(this)`。

[src/storage/aio/BatchReadJob.cc:115-121](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/BatchReadJob.cc#L115-L121) —— `BatchReadJob::finish()`：完成计数到齐后 `baton_.post()`，唤醒等待方。

**元数据前/后处理（chunk 位置解析 + 版本校验）**——

[src/storage/store/ChunkReplica.cc:38-76](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/ChunkReplica.cc#L38-L76) —— `ChunkReplica::aioPrepareRead()`：`store.get(chunkId)` 取元数据，校验 `commitVer == updateVer`（未提交且不允许读未提交则报 `kChunkNotCommit`），设置 `readLength = alignedLength`、`readFd = view.directFD()`、`readOffset = meta.innerOffset + alignedOffset()`。

[src/storage/store/StorageTarget.cc:289-304](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/StorageTarget.cc#L289-L304) —— `StorageTarget::aioPrepareRead/aioFinishRead` 根据 `useChunkEngine()` 在 Rust chunk engine 与旧 C++ `ChunkReplica` 间二选一（chunk engine 详见 [u6-l1](u6-l1-chunk-engine-overview.md)）。

**回传阶段：RDMA 写回客户端**——

[src/storage/service/StorageOperator.cc:176-226](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L176-L226) —— 回传分支：`SEND_DATA_INLINE` 走 `copyToRespBuffer`（数据塞进 RPC 回包）；否则走 RDMA——`batch.addBufferToBatch(writeBatch)` 把每个 job 的本地缓冲加进一次 RDMA 写批，受 per-IB-device 信号量（`max_concurrent_rdma_writes` 默认 256）限流后 `writeBatch.post()` 真正发起 RDMA Write。

[src/storage/aio/BatchReadJob.cc:74-94](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/BatchReadJob.cc#L74-L94) —— `addBufferToBatch()`：对每个成功 job，取 `localbuf.subrange(headLength, length)`（裁掉对齐头尾），调 `batch.add(job.readIO().rdmabuf, localbuf)`——即「把本地缓冲 RDMA Write 到客户端请求里携带的远端 `rdmabuf`」。

[src/storage/service/StorageOperator.h:36-40](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.h#L36-L40) —— 限流与切分相关配置：`batch_read_job_split_size`、`max_concurrent_rdma_writes`、`max_concurrent_rdma_reads`。

#### 4.3.4 代码实践

**实践目标**：把一个 `batchRead` 请求的完整生命周期画成时序图，标注「buffer 分配点」与「AIO 提交点」。

**操作步骤（源码跟踪型实践）**：

1. 从 [StorageOperator.cc:82](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L82) 出发，依次标记六个阶段：
   - ① 路由（L90–L130）
   - ② **buffer 分配点**（L140–L154）
   - ③ 切分入队（L163–L169）
   - ④ 等待完成（L171–L174）
   - ⑤ 组装回传批（L186–L188）
   - ⑥ **RDMA 提交点**（L216–L218）
2. 跟进 ③：进入 [AioReadWorker.cc:60](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/AioReadWorker.cc#L60) 的 `run()`，确认 collect/submit/reap 三步对应 [AioStatus.cc:86](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/AioStatus.cc#L86)、[:108](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/AioStatus.cc#L108)、[:153](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/AioStatus.cc#L153)。
3. 跟进 collect 内部：[ChunkReplica.cc:38](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/ChunkReplica.cc#L38) 的 `aioPrepareRead` 把 `chunkId` 解析成 `(readFd, readOffset, readLength)`。
4. 画出从「客户端 ReadIO」到「客户端远端 rdmabuf 收到数据」的箭头链。

**需要观察的现象**：数据本身只经历「SSD → 本地 RDMA buffer → 客户端 RDMA buffer」三跳，回给客户端的 RPC 里只有 `IOResult`（长度/版本/校验和等元数据），不含数据本体（除非 `SEND_DATA_INLINE`）。

**预期结果**：得到一张完整的时序图，能指出 buffer 在哪里分配（L140–154）、AIO 在哪里提交（AioStatus 的 `submit`）、RDMA 回写在哪里发起（L216–218）。

> 说明：此为源码阅读型实践，需真实集群（storage + mgmtd + FUSE client + IB 网卡）才能端到端运行；本任务以代码跟踪为准。

#### 4.3.5 小练习与答案

**练习 1**：`batch_read_job_split_size`（默认 1024）起什么作用？设成 1 会怎样？
**参考答案**：把整批读切成子批次分多次入队，控制单次 AIO 批次大小，避免内核 AIO 队列（`max_events` 默认 512）过载。设成 1 则退化为「每个读单独入队」，完全丧失批量提交的摊销优势，IOPS 会显著下降。

**练习 2**：为什么 `setResult` 里要做两次版本相关校验（`aioPrepareRead` 一次、`aioFinishRead` 一次）？
**参考答案**：读是异步的，从「取元数据」到「I/O 完成」之间可能有并发写把该 chunk 推进到新版本。前处理校验保证读的是已提交版本；后处理（`aioFinishRead`，见 [ChunkReplica.cc:79-100](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/ChunkReplica.cc#L79-L100)）再确认读到的数据归属的版本与发起时一致，否则报 `kChunkNotCommit`，让客户端重试——避免把「读到一半被改」的脏数据交给上层。

**练习 3**：io_uring 比 libaio 快，为什么默认 `ioengine` 仍是 libaio？
**参考答案**：io_uring 依赖较新的内核与驱动稳定性；libaio 更成熟、兼容性更好。3FS 把选择权交给配置（`enable_io_uring` + `ioengine`，见 [AioReadWorker.h:30-49](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/AioReadWorker.h#L30-L49)），还支持 `random`（每个请求随机二选一）便于 A/B 对比。生产环境可按内核情况切换。

---

## 5. 综合实践

把三个最小模块串起来，完成下面这个**贯穿性源码阅读任务**：

**任务：为一个 `batchRead` 请求编写「执行轨迹说明书」。**

假设客户端发起一个含 3 个 `ReadIO` 的 `batchRead`（分别命中 chain A/B/C，其中 chain C 的请求 offset 不对齐），请按下面的模板填写，每一项都要给出对应的源码行号（永久链接）：

| 阶段 | 做了什么 | 关键源码位置 | 本讲模块 |
| --- | --- | --- | --- |
| 1. 取路由快照 | 整批共用一份 `TargetMap` 快照 | StorageOperator.cc:90 | 读路由 |
| 2. 解析 3 个 target | `getByChainId` × 3，校验 `upToDate` | StorageOperator.cc:103-117 | 读路由 |
| 3. 分配 buffer | 对齐长度 → `tryAllocate`/`allocate`，chain C 多算 head/tail | StorageOperator.cc:139-155；BatchReadJob.cc:20-21 | RDMA buffer |
| 4. 切分入队 | 按 `split_size` 切子批次，`aioReadWorker.enqueue` | StorageOperator.cc:163-169 | 异步批量读 |
| 5. AIO 执行 | collect（取元数据）→ submit（io_submit/io_uring_submit）→ reap | AioStatus.cc:86/108/153 | 异步批量读 |
| 6. 完成回调 | 裁剪长度、算校验和、再校验版本、`finish` | BatchReadJob.cc:24-63 | 异步批量读 |
| 7. 等待与回传 | `batch.complete()` 唤醒 → `addBufferToBatch` + `post()` RDMA 写回 | StorageOperator.cc:171-226 | 异步批量读 |

**进阶思考**（不必写代码）：

- 如果第 5 步的 `reap` 返回某个 IO 的 `res = -EIO`，错误会沿 `setReadJobResult`（[AioStatus.cc:52](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/AioStatus.cc#L52)）→ `setResult` → 该 IO 的 `IOResult.lengthInfo` 传播，而同批其他 IO 不受影响。请确认这一点，并思考客户端如何识别单个 IO 的失败。
- 如果运行期把 `batch_read_job_split_size` 从 1024 改成 4，监控指标 `storage.io_submit.size`（[AioStatus.cc:121](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/AioStatus.cc#L121)）会如何变化？（预期：单次 submit 的 IO 数变小，submit 调用更频繁。）

> 说明：以上为源码阅读与推演任务，无需运行真实集群即可完成；若要在真实集群观察监控指标，需部署 monitor_collector + ClickHouse（见 [u8-l3](u8-l3-monitor-and-analytics.md)）。

## 6. 本讲小结

- **读路由**：`batchRead` 在开头取一次 `TargetMap` 快照，对每个 `ReadIO` 用 `getByChainId` 解析本地 target 并校验 `upToDate`；CRAQ 的「读任意副本」让读全程单机闭环、不跨网络转发。
- **RDMA buffer**：`BufferPool` 启动期预注册大小两个池（默认 `4MB×1024` / `64MB×64`），运行期 `tryAllocate` 快路径 + `allocate` 慢路径按需借出；直接 I/O 要求 4096 对齐，靠 head/tail 扩展 + 读后裁剪实现。
- **异步批量读**：`AioReadWorker` 多线程（默认 32）跑 collect→submit→reap 事件循环，libaio 与 io_uring 双实现（后者用 fixed file/buffer 提速）；整批按 `split_size` 切子批次，完成计数到齐后 `Baton` 唤醒。
- **零拷贝回传**：数据从 SSD 进本地 RDMA buffer，再由 RDMA Write 单边推到客户端预留 buffer；回包 RPC 只含 `IOResult` 元数据。
- **一致性保护**：AIO 前后两次校验 chunk 版本（`aioPrepareRead` / `aioFinishRead`），防止读到「读一半被改」的脏数据。
- **可观测**：`storage.io_submit.size`、`storage.aio.batch_latency`、`storage.aio_align.total_*_length` 等指标贯穿读路径，便于调优。

## 7. 下一步学习建议

- 想看「写」是怎么沿链串行传播、committed/pending 双版本如何维护？请继续 [u5-l3 写路径与 CRAQ 链式复制](u5-l3-write-path-craq.md)，它和本讲共用 `StorageOperator` 与 `BufferPool`，但走的是 `ReliableUpdate`/`ReliableForwarding` 这条链。
- 想理解 `getByChainId` 里的 chain 版本校验与「前驱/后继」判定细节？看 [u5-l4 TargetMap 与链路由](u5-l4-targetmap-routing.md)。
- 想知道 chunk 在磁盘上的元数据、`(readFd, readOffset)` 是怎么算出来的、Rust chunk engine 如何接管 `aioPrepareRead`？看 [u6-l1 Chunk Engine 总览与 C++/Rust FFI](u6-l1-chunk-engine-overview.md) 与 [u6-l3 Chunk 元数据与 RocksDB](u6-l3-chunk-meta-rocksdb.md)。
- 想从客户端视角看「为什么读能打任意 target、buffer 是怎么在客户端侧注册并随请求带过来的」？看 [u7-l1 客户端核心](u7-l1-client-core.md) 与 [u7-l3 USRBIO 零拷贝 API](u7-l3-usrbio-zero-copy.md)。
