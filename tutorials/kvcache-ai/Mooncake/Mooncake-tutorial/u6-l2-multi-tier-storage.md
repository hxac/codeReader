# 多级存储：DRAM / SSD / NVMe-oF

> 单元 6 · 第 2 讲
> 依赖：建议先学完 [u5-l5 Segment 与 Replica 数据模型](u5-l5-segment-replica-model.md) 与 [u6-l1 缓冲区分配与内存池](u6-l1-store-allocator.md)，了解 Segment / Replica / ReplicaType、以及 DRAM 段内的 Buffer Allocator 后再读本讲。

## 1. 本讲目标

u6-l1 解决的是「DRAM 段内怎么切 buffer」和「一次写 N 个副本挑哪些 DRAM 段」。但当 DRAM 装不下时，KV cache 必须能「下沉」到更慢但更大的介质上，读取时再「回填」回来。本讲就讲这条**下沉/回填**的存储层次。

具体地，本讲回答三个问题：

1. **层次**：Mooncake 把存储分成几层？数据怎么从 DRAM 流到本地 SSD、再到分布式/远端 SSD？
2. **文件后端**：同一块本地 SSD，既可以用 POSIX 系统调用读写，也可以用 io_uring 异步读写——两者的 I/O 路径有什么本质差异？分布式文件系统（hf3fs/3FS）和 SPDK（NVMe-oF）又分别处在哪一层？
3. **StorageBackend**：本地 SSD 上数据到底按什么格式落盘？`Bucket / FilePerKey / OffsetAllocator / Distributed` 四种后端的布局与取舍是什么？

学完本讲，你应当能够：

1. 画出 Mooncake 的 **L1（DRAM/VRAM）→ L2（本地 SSD）→ L3（分布式 FS / NVMe-oF 远端 SSD）** 多级层次，并指出每层对应的 `ReplicaType` 与代码模块。
2. 说清 `PosixFile` 与 `UringFile` 两条 I/O 路径的差异（同步 `pread/pwrite` vs 线程本地 io_uring 环 + 固定缓冲），以及开启 `O_DIRECT` 后的对齐处理。
3. 描述 `StorageBackendInterface` 的统一抽象，以及 `BucketStorageBackend`（默认）、`OffsetAllocatorStorageBackend`、`StorageBackendAdaptor`(FilePerKey)、`DistributedStorageBackend` 四种实现各自的落盘格式与适用场景。

## 2. 前置知识

- **ReplicaType（副本类型）**：Mooncake 用一个枚举标记一份副本存在哪种介质上。读懂它就懂了层次划分：

  [mooncake-store/include/allocator.h:21-27](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/allocator.h#L21-L27) —— `MEMORY`（DRAM/VRAM 副本）、`LOCAL_DISK`（本地 SSD 副本）、`NOF_SSD`（NVMe-oF 远端 SSD 副本）、`DISK`/`ALL`。

- **KV cache 对象**：一段不定长字节，带一个 key。本讲聚焦「它落盘时的字节布局」，而不是对象语义本身。
- **Offload（下沉）/ Load（回填）/ Promotion（晋升）**：offload = DRAM → SSD（内存吃紧时把冷对象写盘）；load = SSD → DRAM（命中 SSD 副本时读回）；promotion = 把 SSD 上的热对象主动搬回 DRAM（L2→L1 晋升）。
- **posix 文件 I/O**：`open/read/write/preadv/pwritev/close`，进程发起系统调用、内核同步完成。简单通用，但每次系统调用有上下文切换开销，单线程难以打满 NVMe 队列深度。
- **io_uring**：Linux 的异步 I/O 接口。用户态与内核共享一对环形队列（SQ 提交、CQ 完成），可一次提交多个 I/O 请求、再批量收割结果，减少系统调用次数、暴露 NVMe 队列深度 > 1。
- **O_DIRECT**：绕过页缓存直接读写设备。要求**缓冲区地址、长度、文件偏移**三者都对齐（通常 4096 字节），换取零拷贝、低延迟。
- **分布式文件系统（DFS）/ 3FS（hf3fs）**：跨节点共享的文件系统，可把对象写到远端节点挂载的存储上。3FS 是 DeepSeek 开源的高性能分布式 FS。
- **NVMe-oF（NVMe over Fabrics）**：把 NVMe SSD 通过网络（RDMA/TCP）暴露成「远端 SSD」。Mooncake 用 **SPDK** 作为 NVMe-oF 客户端访问这类远端 SSD 池。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [mooncake-store/include/storage_backend.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/storage_backend.h) | `StorageBackendInterface` 抽象、`StorageBackendType` 枚举、`FileStorageConfig`/`BucketBackendConfig`/`FilePerKeyConfig`、`Bucket`/`OffsetAllocator`/`StorageBackendAdaptor`(FilePerKey) 三个本地后端类声明、`CreateStorageBackend` 工厂声明。 |
| [mooncake-store/include/file_interface.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/file_interface.h) | `StorageFile` 抽象基类、`PosixFile`（同步 POSIX）与 `UringFile`（io_uring）两种文件后端声明。 |
| [mooncake-store/src/posix_file.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/posix_file.cpp) | `PosixFile` 实现：基于 `::write/::read/pwritev/preadv` 的同步 I/O。 |
| [mooncake-store/src/uring_file.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/uring_file.cpp) | `UringFile` 与 `SharedUringRing`：每线程一个 io_uring 环、固定缓冲注册、批量读、`O_DIRECT` 对齐。 |
| [mooncake-store/src/storage_backend.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/storage_backend.cpp) | 四种后端的实现，以及 `create_file` / `OpenFile`（按 `use_uring` 选择 Posix/Uring）、`CreateStorageBackend` 工厂。 |
| [mooncake-store/src/file_storage.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/file_storage.cpp) | `FileStorage`：连接 client 与 StorageBackend 的顶层协调者，含心跳 offload、`BatchLoad`、`AllocateBatch`（O_DIRECT 暂存缓冲分配）。 |
| [mooncake-store/include/storage/distributed/fs_adapter.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/storage/distributed/fs_adapter.h) | `FileSystemAdapter` 抽象——把 DFS（3FS/CephFS/JuiceFS…）的 I/O 差异隔离在适配器背后。 |
| [mooncake-store/src/storage/distributed/distributed_storage_backend.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/storage/distributed/distributed_storage_backend.cpp) | `DistributedStorageBackend`：把对象写到 DFS（通过 `FileSystemAdapter`），用哈希分桶布局、不支持本地淘汰。 |
| [mooncake-store/src/hf3fs/hf3fs_file.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/hf3fs/hf3fs_file.cpp) | `ThreeFSFile`：3FS 的 `StorageFile` 实现，是 hf3fs 适配器背后的真实 I/O。 |
| [mooncake-store/include/spdk/spdk_wrapper.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/spdk/spdk_wrapper.h) | SPDK NVMe-oF 客户端封装——驱动 **NOF_SSD** 远端 SSD 池这一层。 |
| [docs/source/design/ssd-offload.md](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/design/ssd-offload.md) / [docs/source/deployment/ssd-offload.md](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/ssd-offload.md) | SSD offload 的设计与部署文档（本讲综合实践的重要依据）。 |

## 4. 核心概念与源码讲解

### 4.1 多级存储层次：L1 / L2 / L3

#### 4.1.1 概念说明

Mooncake 不是把数据只放一个地方，而是按介质速度/容量/成本排成层次，热数据在快介质，冷数据下沉到慢介质：

| 层次 | 介质 | `ReplicaType` | 谁负责存取 | 典型延迟 |
| --- | --- | --- | --- | --- |
| **L1** | DRAM / GPU VRAM | `MEMORY` | Buffer Allocator + AllocationStrategy（u6-l1） | ~µs（本地）/ RDMA（远端 DRAM） |
| **L2** | 本地 NVMe SSD | `LOCAL_DISK` | `FileStorage` + 本地 `StorageBackend`（posix/io_uring） | ~10µs–ms |
| **L3a** | 分布式文件系统 | （DFS 适配器） | `DistributedStorageBackend` + `FileSystemAdapter`（hf3fs/3FS） | ~ms（跨节点） |
| **L3b** | NVMe-oF 远端 SSD 池 | `NOF_SSD` | SPDK NVMe-oF 客户端（独立 transfer 路径） | ~ms（网络 + NVMe） |

L1 是「分布式内存 KV cache」本身（Transfer Engine / RDMA 可直接拉远端 DRAM）；当 DRAM 吃紧，master 通过心跳指挥 Real Client 把对象 **offload 到 L2 本地 SSD**；L3 则是更进一步的「跨节点/远端」容量层，既可以是挂载到本机的分布式 FS（hf3fs），也可以是经 NVMe-oF 访问的远端 SSD 池。

需要特别澄清一点（避免误解源码）：**L2 本地 SSD 上的「文件后端」只有 `PosixFile` 与 `UringFile` 两种 `StorageFile` 实现**；`hf3fs` 是 L3 分布式 FS 的适配器（`ThreeFSFile`），而 **SPDK 并不是 `FileStorage` 的本地文件后端**，它驱动的是 L3b 的 **NVMe-oF 远端 SSD 池**（`NOF_SSD`），走的是与本地 `StorageBackend` 完全不同的 transfer 路径。本讲重点放在 L2（本地 SSD，posix/io_uring）与 L3a（分布式 FS），L3b 仅作层次说明。

#### 4.1.2 核心流程

**Offload（L1 → L2）**由 Real Client 内的**心跳线程**驱动，与应用写路径无关：

```
心跳线程  ──OffloadObjectHeartbeat──▶  master
            ◀── {key→size} 待下沉对象 ──
            BatchQuerySegmentSlices  ▶  本地 DRAM 段  (拿到 {key→Slice})
            [可选淘汰: PrepareEviction → 通知 master → FinalizeEviction 删旧文件]
            storage_backend_->BatchOffload(slices)  ▶  写入本地 SSD
            NotifyOffloadSuccess  ▶  master 为该对象加 LOCAL_DISK 副本描述符
```

**Load（L2 → L1）**在一次 `Get` 内存未命中、但存在 LOCAL_DISK 副本时触发：

```
请求方 ──BatchGet──▶ master  ▶  返回 LOCAL_DISK(rpc_addr) 副本
        ──batch_get_offload_object──▶ 持有 SSD 的目标 client
                目标 client: AllocateBatch(在 ClientBuffer 分 O_DIRECT 对齐暂存位)
                             storage_backend_->BatchLoad ▶ 从 SSD 读入暂存区
        ◀── {batch_id, pointers[], te_addr} ──
        ──Transfer Engine(RDMA/TCP)──▶ 把暂存区数据零拷贝拉到应用内存(VRAM/DRAM)
        ──release_offload_buffer──▶ 归还暂存位
```

**Promotion（L2 → L1，热对象主动晋升）**：心跳线程还会拉取 master 选出的「晋升候选」，把热对象从 SSD 读出来、经 Transfer Engine 写回新分配的 DRAM 副本，使后续读命中 L1。参见 `ProcessPromotionTasks`。

#### 4.1.3 源码精读

**顶层协调者 `FileStorage`** —— 构造时按配置创建 StorageBackend，并在开启 io_uring 时把暂存缓冲注册为固定缓冲：

[mooncake-store/src/file_storage.cpp:177-220](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/file_storage.cpp#L177-L220) —— 构造 `FileStorage`：`CreateStorageBackend(config_)` 决定走哪种后端；`use_uring` 时调用 `UringFile::register_global_buffer` 把 `ClientBuffer` 注册进 io_uring（L2 读零拷贝的关键前置）。

**Init 扫盘恢复** —— 启动时把磁盘上既有对象的元数据回灌 master，实现 L2 副本的「重启可见」：

[mooncake-store/src/file_storage.cpp:283-304](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/file_storage.cpp#L283-L304) —— `Init` 中 `storage_backend_->ScanMeta(...)` 扫描磁盘已有对象，回调里把 `transport_endpoint` 设为本机 RPC 地址并 `NotifyOffloadSuccess`。

**O_DIRECT 暂存缓冲分配** —— `AllocateBatch` 为每个 key 在 `ClientBuffer` 里分配**超额**且对齐的暂存位，正是为 L2 `read_aligned` 的零拷贝读取铺路：

[mooncake-store/src/file_storage.cpp:935-980](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/file_storage.cpp#L935-L980) —— `alloc_size = align_up(data_size,4096) + 2*4096`，并把返回指针向上对齐到 4096；slice 只记录真实 `data_size`，缓冲背后「多出来」的空间留给 O_DIRECT 的对齐读尾。

**心跳驱动 offload + promotion** —— 同一个心跳 tick 里先下沉、再晋升：

[mooncake-store/src/file_storage.cpp:669-684](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/file_storage.cpp#L669-L684) —— `Heartbeat` 第二步 `OffloadObjects`，随后 `(void)ProcessPromotionTasks()` 推进 L2→L1 晋升（best-effort，失败不阻断 offload）。

#### 4.1.4 代码实践

**实践目标**：跟着 `FileStorage` 的生命周期，把「一个对象如何从 DRAM 下沉到 SSD、又如何在 Get 时回填」串成一条可指认的调用链。

**操作步骤（源码阅读型）**：

1. 从构造到就绪：读 [file_storage.cpp:177-220](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/file_storage.cpp#L177-L220)（创建后端 + 注册固定缓冲）→ [file_storage.cpp:234-321](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/file_storage.cpp#L234-L321)（Init：注册内存、Init 后端、扫盘回灌、起心跳线程）。
2. 下沉链：`Heartbeat` → `OffloadObjectHeartbeat` → `OffloadObjects` → `storage_backend_->BatchOffload` →（后端落盘）→ `NotifyOffloadSuccess`。注意 [file_storage.cpp:489-518](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/file_storage.cpp#L489-L518) 的 D2H staging：若 slice 在 GPU 上，先用 `PinnedBufferPool`（见 u6-l1）拷到主机内存再落盘。
3. 回填链：`BatchGet` → `AllocateBatch`（分对齐暂存位）→ `BatchLoad` → `storage_backend_->BatchLoad` → 读 SSD 入暂存区，最终由 Transfer Engine 拉走。

**需要观察的现象**：在日志中应能看到 `action=client_buffer_gc_thread_started`、`Successfully registered buffer with UringFile`（开 io_uring 时）、以及扫盘回灌的 key 计数。

**预期结果**：你能用一句话说出「offload 由心跳线程异步驱动，load 由请求方 RPC 触发目标 client 读盘，二者共用 `ClientBuffer` 这个 O_DIRECT 暂存区」。若无法本地跑起集群，标注「待本地验证」，但读完上述调用链即可理解。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `FileStorage::AllocateBatch` 要给每个 key 分配比 `data_size` 更大、且对齐到 4096 的缓冲？

> **答案**：L2 读路径在开 io_uring 时走 `read_aligned`（O_DIRECT），要求缓冲地址、读长度、文件偏移都对齐 4096。但对象真实偏移未必对齐，于是读取范围会向左/向右扩展到对齐边界（见 4.2 的 `align_down`/`align_up`）。多分配 `2*4096` 正是容纳这段「对齐读尾」与「指针前移」所需。

**练习 2**：master 重启后，L2 上已落盘的对象还可用吗？

> **答案**：对 `Bucket`/`FilePerKey` 后端可用。Real Client 的 `Init` 会 `ScanMeta` 把磁盘对象重新 `NotifyOffloadSuccess` 回灌 master（[file_storage.cpp:283-304](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/file_storage.cpp#L283-L304)）。`OffsetAllocator` 后端是例外——它在 Init 时 `O_TRUNC` 截断数据文件并清空内存元数据，重启后旧对象不可恢复。

---

### 4.2 文件后端：posix 与 io_uring

#### 4.2.1 概念说明

无论选哪种 `StorageBackend`，最终都要落到「怎么读写一个文件」。Mooncake 把文件 I/O 抽象成 `StorageFile`，并给出两种**可替换**的实现：

- **`PosixFile`**：标准 POSIX 同步 I/O。`write` 循环调 `::write`、`read` 循环调 `::read`，scatter/gather 走 `pwritev`/`preadv`。简单、可移植，没有额外依赖。
- **`UringFile`**：基于 Linux io_uring 的异步 I/O。核心是 `SharedUringRing`：**每个线程一个 io_uring 环**，线程内无锁、可批量提交多个 SQE 暴露 NVMe 队列深度，再把 `ClientBuffer` 注册为**固定缓冲**走 `read_fixed` 零拷贝路径。

两者由配置开关 `use_uring` 在运行时选择（见 `create_file` / `OpenFile`）。分布式 FS（3FS）的 `ThreeFSFile` 是第三种 `StorageFile`，但它属于 L3 适配器，不在 L2 本地路径上。

> 关于 spec 中提到的「spdk」：SPDK 在 Mooncake 里**不是** `StorageFile` 后端，而是 NVMe-oF 远端 SSD 池（`NOF_SSD`，L3b）的访问客户端（见 [spdk_wrapper.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/spdk/spdk_wrapper.h)）。L2 本地 SSD 的文件后端只有 posix 与 io_uring 两种。

#### 4.2.2 核心流程

**posix 读一个对象**（`PosixFile::read` / `vector_read`）：

```
buffer.resize(length)
loop { n = ::read(fd, ptr, length - read_bytes);  // 同步系统调用，可能阻塞
       if EINTR continue; if 0 break(EOF) }
```

scatter/gather 版直接 `::preadv(fd, iov, iovcnt, offset)` 一次系统调用完成。每次调用都付出「用户态→内核→设备→内核→用户态」的同步往返。

**io_uring 读一个对象**（`SharedUringRing::read` → `submit_rw`）：

```
remaining = len
while remaining > 0:
    cs = calc_chunk(remaining, QUEUE_DEPTH)        # 把大读切成 ~QUEUE_DEPTH 个对齐块
    n   = min(块数, 32)
    for i in n:  sqe = get_sqe(); prep_read_fixed(sqe,...) if 命中注册缓冲 else prep_read(...)
    collect(n)  # io_uring_submit_and_wait(n)，一次性收割 n 个 CQE
    remaining -= 已读
```

关键差异点：

1. **线程本地环、零锁**：`thread_local SharedUringRing`，不同线程的 I/O 完全并行，无互斥。
2. **批量提交**：一次 `submit_and_wait(k)` 提交并等待 k 个完成，设备看到队列深度 > 1，吞吐远高于逐个 `read`。
3. **固定缓冲零拷贝**：读目标若落在全局注册的 `ClientBuffer` 内，用 `prep_read_fixed`，省去内核对每次 I/O 的 `get_user_pages` 开销。
4. **O_DIRECT 对齐**：读路径默认开 `O_DIRECT`，要求地址/长度/偏移对齐 4096；写路径不开（避免对齐填充浪费与 meta 解析错乱）。

#### 4.2.3 源码精读

**抽象基类 `StorageFile`** —— 统一的 `write/read/vector_write/vector_read` 接口，两种实现都继承它：

[mooncake-store/include/file_interface.h:57-127](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/file_interface.h#L57-L127) —— `StorageFile` 纯虚接口；含 `fd_`、`filename_`、`error_code_` 与文件锁 RAII。

**PosixFile 同步写/读** —— 注意 `EINTR` 重试与「写满为止」循环：

[mooncake-store/src/posix_file.cpp:40-69](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/posix_file.cpp#L40-L69) —— `PosixFile::write`：循环 `::write`，遇 `EINTR` 重试，写不满即报错。
[mooncake-store/src/posix_file.cpp:104-132](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/posix_file.cpp#L104-L132) —— `vector_write`/`vector_read`：直接 `::pwritev`/`::preadv`，单次系统调用完成 gather/scatter。

**UringFile 之上的薄封装** —— `UringFile::read` 把工作交给线程本地环：

[mooncake-store/src/uring_file.cpp:110-121](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/uring_file.cpp#L110-L121) —— `SharedUringRing::read/write`：先 `ensure_buf_registered()`，命中注册缓冲则用 fixed 版本，否则普通版，统一转 `submit_rw`。

**切块 + 批量收割** —— io_uring 吞吐的核心：

[mooncake-store/src/uring_file.cpp:273-327](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/uring_file.cpp#L273-L327) —— `submit_rw`：用 `calc_chunk` 把剩余字节切成 2 的幂次块（下限 4096），一次最多填 `QUEUE_DEPTH`(32) 个 SQE，再 `collect(n)` 批量收割。
[mooncake-store/src/uring_file.cpp:246-270](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/uring_file.cpp#L246-L270) —— `collect`：`io_uring_submit_and_wait(expected)` 后遍历 CQE 累加字节、出错置错。

**批量独立读** —— `batch_read` 一次提交最多 32 个不同偏移的读，最大化队列深度：

[mooncake-store/src/uring_file.cpp:143-173](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/uring_file.cpp#L143-L173) —— `batch_read`：按 `QUEUE_DEPTH` 分批，每批逐个 `prep_read_fixed`/`prep_read`，再 `collect`。

**固定缓冲注册** —— `ClientBuffer` 进程级注册、各线程懒注册：

[mooncake-store/src/uring_file.cpp:663-696](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/uring_file.cpp#L663-L696) —— `register_global_buffer`：先 `madvise(MADV_NOHUGEPAGE)`（避免 THP 长期 pin 失败），再把 base/size 原子发布到 `g_buf`，并立即在当前线程注册。
[mooncake-store/src/uring_file.cpp:71-96](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/uring_file.cpp#L71-L96) —— `ensure_buf_registered`：每个线程首次 I/O 时从 `g_buf` 取 base/size 调 `io_uring_register_buffers`；失败则置 `buf_register_failed_` 不再重试，自动退化为非固定缓冲 I/O。

**运行时选择** —— 按 `use_uring` 与读/写模式选实现：

[mooncake-store/src/storage_backend.cpp:753-790](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/storage_backend.cpp#L753-L790) —— `StorageBackend::create_file`：`use_uring_ && Read` 时加 `O_DIRECT` 并返回 `UringFile`（`use_direct_io=true`）；否则 `PosixFile`。注释明确**写路径不开 O_DIRECT**。

#### 4.2.4 代码实践

> 本节实践即本讲主任务的一部分，详见 [第 5 节 综合实践](#5-综合实践)。要点预告：开 `MOONCAKE_OFFLOAD_USE_URING=true` 后，读路径走 `read_aligned`（零拷贝入对齐暂存区），写路径仍走缓冲；对比 posix 则读为 `::preadv`、写为 `::pwritev`，全程同步系统调用。

#### 4.2.5 小练习与答案

**练习 1**：`SharedUringRing` 为什么用 `thread_local` 而不是进程级单例？

> **答案**：进程级单例需要一把全局锁保护 SQ/CQ，多线程并发 I/O 时锁争用成为主要延迟（设计注释指出旧的全局环每次读 > 1ms）。`thread_local` 让每个线程独占一个环、线程内无锁，并发 I/O 完全并行；线程内再靠批量提交暴露 NVMe 队列深度。

**练习 2**：`register_global_buffer` 为什么要先 `madvise(MADV_NOHUGEPAGE)`？

> **答案**：固定缓冲注册会让内核用 `FOLL_LONGTERM` 长期 pin 这些页。若该区间被透明大页（THP, 2MB）覆盖，内核必须先把它拆成 4KB 页才能 pin，在内存碎片/大页紧张时会失败（ENOMEM）。`MADV_NOHUGEPAGE` 强制区间用 4KB 页，让 pin 可靠。注册失败不致命——会退化为非固定缓冲 I/O（[uring_file.cpp:82-88](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/uring_file.cpp#L82-L88)）。

**练习 3**：为什么 `create_file` 只在读路径加 `O_DIRECT`，写路径不加？

> **答案**：O_DIRECT 写要求长度对齐到 4096，会迫使 bucket 数据/元数据补齐，既浪费盘空间又会破坏 meta 文件的定长解析。读路径则受益于绕过页缓存（低延迟、零拷贝入注册缓冲），且 `AllocateBatch` 已为对齐读预留了超额缓冲，故只在读路径开。

---

### 4.3 StorageBackend：四种落盘实现

#### 4.3.1 概念说明

`StorageBackendInterface` 是 L2/L3 落盘的统一抽象：`BatchOffload`（写一批对象）、`BatchLoad`（按 key 读回）、`IsExist`、`IsEnableOffloading`、`ScanMeta`（扫盘回灌元数据）。`FileStorage` 只依赖这个接口，不关心具体布局。

`CreateStorageBackend` 按 `storage_backend_type` 产出四种实现：

| 后端 | 枚举 | 落盘格式 | 元数据 | 适用场景 |
| --- | --- | --- | --- | --- |
| **Bucket**（默认） | `kBucket` | 多对象合并进一个 `.bucket` 数据文件 + `.meta` 元数据文件；bucket id 单调递增（时间戳+序号） | 内存 map + 磁盘 meta 文件，重启可恢复；支持 FIFO/LRU 淘汰 | 通用、大规模（推荐） |
| **FilePerKey** | `kFilePerKey` | 每个 key 一个文件（两级哈希分目录） | 扫盘即得，重启可恢复 | 调试、小规模 |
| **OffsetAllocator** | `kOffsetAllocator` | 单个大文件 `kv_cache.data`，记录格式 `[key_len:u32][value_len:u32][key][value]`，offset 由分配器管理 | 1024 分片内存 map；**重启截断、不可恢复** | 高并发小对象、不要持久 |
| **Distributed** | `kDistributed` | 写到分布式 FS（hf3fs/3FS），key 经 XXH64 哈希分到 `hash_bucket_count` 个目录 | 由 DFS 管理，不支持本地淘汰 | 跨节点共享容量（L3a） |

#### 4.3.2 核心流程

**Bucket 后端的 Offload 流程**（`BucketStorageBackend::BatchOffload`）：

```
1. bucket_id = BucketIdGenerator::NextId()       # 单调递增
2. BuildBucket(id, batch_object) → 拼装 iovec[] + BucketMetadata(各 key 的 offset/key_size/data_size)
3. PrepareEviction(所需空间)                       # 若设了 max_total_size：两阶段淘汰
     Phase1: 锁内从 buckets_/object_bucket_map_ 摘掉旧 bucket，收集待删 keys
     通知 master(evicted_keys) via eviction_handler → BatchEvictDiskReplica
     Phase2: FinalizeEviction 等 inflight_reads_==0 后删 .bucket/.meta 文件
4. WriteBucket(id, bucket, iovs)                   # 写数据文件(+datasync) 再写 meta 文件
5. complete_handler(keys, metadatas) → NotifyOffloadSuccess
6. 锁内查重后提交 object_bucket_map_/buckets_/lru_index_
```

落盘的 `.bucket` 数据文件内部布局（由 `BuildBucket` 决定，顺序拼接）：

```
[ key0 | value0_slices... | key1 | value1_slices... | ... ]
        BucketObjectMetadata{offset, key_size, data_size} 记录每个 key 的定位
```

注意：`offset` 指向「key 起始位置」，读取时 `actual_offset = offset + key_size` 跳过 key 直达 value。

**Bucket 后端的 Load 流程**（`BatchLoad`）：

```
1. 锁内（读锁）：key → object_bucket_map_ → bucket_id → buckets_ → BucketMetadata
   校验 data_size == dest_slice.size；按 bucket 分组读计划
   每个 bucket 建一个 BucketReadGuard(inflight_reads_++)，并（LRU 模式）更新 last_access_ns_
2. 释放锁后做 I/O（文件受 guard 保护，淘汰不会删到正在读的 bucket）
   对每个 key：actual_offset = offset + key_size
     UringFile: read_aligned(dest_slice.ptr, aligned_size, aligned_offset)  # 对齐读，零拷贝
                batch_object[key].ptr = dest_slice.ptr + offset_in_buffer   # 指针前移，无 memcpy
     PosixFile: vector_read(&iov, 1, actual_offset)
3. guard 析构 inflight_reads_--
```

**OffsetAllocator 后端**：单个预分配大文件 + 一个线程安全的 offset 分配器；写入是「分配 offset → gather 写 `[header|key|value]` → 在该 key 所属分片(1024 之一)加锁更新 map」；读取按 offset 直接 `vector_read`。

**Distributed 后端**：`GetObjectPath` 用 `XXH64(key) % hash_bucket_count` 选目录、`EscapeFilename(key)` 做文件名，再委托 `FileSystemAdapter`（如 `Hf3fsAdapter`）的 `VectorWriteFile`/`ReadFile` 落到 DFS。

#### 4.3.3 源码精读

**统一抽象与类型枚举**：

[mooncake-store/include/storage_backend.h:158-163](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/storage_backend.h#L158-L163) —— `StorageBackendType { kFilePerKey, kBucket, kOffsetAllocator, kDistributed }`。
[mooncake-store/include/storage_backend.h:249-290](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/storage_backend.h#L249-L290) —— `StorageBackendInterface`：`BatchOffload/BatchLoad/IsExist/IsEnableOffloading/ScanMeta` 等纯虚方法。

**工厂** —— 按枚举 + 环境变量构造具体后端：

[mooncake-store/src/storage_backend.cpp:3246-3297](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/storage_backend.cpp#L3246-L3297) —— `CreateStorageBackend`：`kBucket`→`BucketStorageBackend`、`kFilePerKey`→`StorageBackendAdaptor`、`kOffsetAllocator`→`OffsetAllocatorStorageBackend`、`kDistributed`→`DistributedStorageBackend`（DFS 适配器当前仅支持 `hf3fs`，需编译 `USE_3FS`）。

**配置来源** —— 环境变量驱动 `storage_backend_type` 与 `use_uring`：

[mooncake-store/src/file_storage.cpp:41-95](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/file_storage.cpp#L41-L95) —— `FileStorageConfig::FromEnvironment`：`MOONCAKE_OFFLOAD_STORAGE_BACKEND_DESCRIPTOR` 选后端，`MOONCAKE_OFFLOAD_USE_URING`（兼容 `MOONCAKE_USE_URING`）选文件后端。

**Bucket 拼装** —— 决定 `.bucket` 文件的字节布局：

[mooncake-store/src/storage_backend.cpp:1956-1986](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/storage_backend.cpp#L1956-L1986) —— `BuildBucket`：对每个对象把 `key` 与各 slice 顺序塞进 `iovs`，记录 `BucketObjectMetadata{storage_offset, key_size, data_size}` 与对外 `StorageObjectMetadata`，累加 `storage_offset`。

**Bucket 写盘 + 写序保证** —— 数据文件 `datasync` 后才写 meta，崩溃不留「有效 meta 指向半截数据」：

[mooncake-store/src/storage_backend.cpp:1988-2127](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/storage_backend.cpp#L1988-L2127) —— `WriteBucket`：UringFile 路径先把所有 iov 拷进对齐缓冲（优先用预分配的 `aligned_io_buffer_`），`write_aligned` 后 `datasync()`，再 `StoreBucketMetadata`；meta 失败则删数据文件防孤儿。非 UringFile 走 `vector_write`。

**Bucket 读盘零拷贝** —— `actual_offset = offset + key_size`，对齐后 `read_aligned` 直接读进暂存区并仅前移指针：

[mooncake-store/src/storage_backend.cpp:1487-1526](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/storage_backend.cpp#L1487-L1526) —— 读每个 key：`aligned_offset=align_down(actual_offset,4096)`、`aligned_end=align_up(data_end,4096)`，`read_aligned(dest_slice.ptr, aligned_size, aligned_offset)`，再 `ptr += offset_in_buffer` 指向真实数据起点，全程无 memcpy。

**Bucket 文件路径与打开** —— `.bucket`/`.meta` 后缀；按 `use_uring` 选 Uring/Posix：

[mooncake-store/src/storage_backend.cpp:2550-2564](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/storage_backend.cpp#L2550-L2564) —— `GetBucketDataPath`/`GetBucketMetadataPath`：`<id>.bucket` / `<id>.meta`。
[mooncake-store/src/storage_backend.cpp:2566-2600](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/storage_backend.cpp#L2566-L2600) —— `OpenFile`：`use_uring && Read` 返回 `UringFile(...,true)`，否则 `PosixFile`。`GetOrOpenFile` 还会**缓存读模式文件句柄**（避免热 bucket 反复 open/close）。

**OffsetAllocator 单文件预分配 + 不可恢复**：

[mooncake-store/src/storage_backend.cpp:2710-2811](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/storage_backend.cpp#L2710-L2811) —— `Init`：`O_RDWR|O_CREAT|O_TRUNC` 打开 `kv_cache.data`，`fallocate`/`ftruncate` 预分配 `capacity_`（=`total_size_limit`，**无安全余量**），建 `OffsetAllocator(0, capacity_)`。`O_TRUNC` 决定了重启不可恢复。
[mooncake-store/src/storage_backend.cpp:2815-2990](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/storage_backend.cpp#L2815-L2990) —— `BatchOffload`：`allocator_->allocate(record_size)` 得 offset → gather 写 `[key_len|value_len|key|value]` → 仅锁该 key 所属分片更新 map；分配句柄用 `shared_ptr` 引用计数，最后读者释放才回收物理区间。

**Distributed 后端哈希分桶 + DFS 委托**：

[mooncake-store/src/storage/distributed/distributed_storage_backend.cpp:276-282](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/storage/distributed/distributed_storage_backend.cpp#L276-L282) —— `GetObjectPath`：`XXH64(key) % hash_bucket_count` 选 `{root}/{:02x}/` 目录，`EscapeFilename(key)` 作文件名。
[mooncake-store/src/storage/distributed/distributed_storage_backend.cpp:137-188](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/storage/distributed/distributed_storage_backend.cpp#L137-L188) —— `BatchOffload`：构造 iovec 后 `fs_adapter_->VectorWriteFile(path, ...)`；`eviction_handler` 被忽略（DFS 自管空间，[distributed_storage_backend.cpp:149-153](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/storage/distributed/distributed_storage_backend.cpp#L149-L153)）。

**DFS 适配器抽象** —— 把 3FS/CephFS/JuiceFS 差异藏起来：

[mooncake-store/include/storage/distributed/fs_adapter.h:26-110](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/storage/distributed/fs_adapter.h#L26-L110) —— `FileSystemAdapter`：`WriteFile/ReadFile/VectorWriteFile/VectorReadFile/DeleteFile/FileExists/ListFiles/Init/Shutdown/GetName`；`DistributedStorageBackend` 只依赖此接口。
[mooncake-store/src/hf3fs/hf3fs_file.cpp:10-32](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/hf3fs/hf3fs_file.cpp#L10-L32) —— `ThreeFSFile`：3FS 的 `StorageFile`，析构 `hf3fs_dereg_fd` + `close`，写失败删损坏文件。

#### 4.3.4 代码实践

**实践目标**：对比四种后端在同一 `StorageBackendInterface` 下的不同落盘格式，能在磁盘上指认出「这个后端写出来的文件长什么样」。

**操作步骤（源码阅读 + 配置对照型）**：

1. 对照 [docs/source/deployment/ssd-offload.md:144-181](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/ssd-offload.md#L144-L181) 的「Storage Backends」小节，与上表逐一对应。
2. **Bucket**：在存储目录下应有成对的 `<timestamp>-<seq>.bucket` / `.meta`。结合 `BuildBucket`（[storage_backend.cpp:1956-1986](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/storage_backend.cpp#L1956-L1986)）理解 `.bucket` 内 `[key|value...]` 的顺序拼接。
3. **OffsetAllocator**：目录下是单个 `kv_cache.data`（[storage_backend.cpp:2704-2709](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/storage_backend.cpp#L2704-L2709) 的 `GetDataFilePath`）。注意它是 `O_TRUNC` 创建的——重启即空。
4. **Distributed**：目录结构是 `{root}/00..ff/<escaped_key>`，共 `hash_bucket_count`（默认 256）个目录（[distributed_storage_backend.cpp:122-131](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/storage/distributed/distributed_storage_backend.cpp#L122-L131)）。

**需要观察的现象**：切换 `MOONCAKE_OFFLOAD_STORAGE_BACKEND_DESCRIPTOR` 后，存储目录的文件形态完全不同（成对 bucket/meta vs 单大文件 vs 哈希目录树）。

**预期结果**：你能复述「`StorageBackendInterface` 统一了写/读/扫接口，布局差异完全封装在各实现内部」。实际切换需编译相应的 DFS/uring 选项并部署集群，若不便可标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：Bucket 后端读取时 `actual_offset = offset + key_size`，为什么要在 offset 上再加 key_size？

> **答案**：`.bucket` 文件里每个对象区是「先 key 后 value」顺序拼接（见 `BuildBucket`），`offset` 指向的是 key 起点。应用只要 value，所以读到 value 必须跳过 key，于是读取偏移 = `offset + key_size`。key 本身只用于校验/定位，不读入业务缓冲。

**练习 2**：`OffsetAllocatorStorageBackend` 的淘汰/重启语义和 Bucket 有何根本不同？

> **答案**：它没有「按 bucket 淘汰」的概念——空间由 offset 分配器管理，对象写入时分配 offset、释放时（引用计数归零）回收物理区间，靠 `IsEnableOffloading` 判断是否还能写。更关键的是 Init 时 `O_TRUNC` 截断数据文件并清空 1024 个分片 map，**重启后旧对象不可恢复**，这与 Bucket/FilePerKey 的扫盘恢复正相反。

**练习 3**：`DistributedStorageBackend` 为什么忽略 `eviction_handler`？

> **答案**：分布式 FS 的容量由 DFS 自身管理（多节点共享、可能配额），本地后端那种「按 max_total_size 淘汰旧 bucket」的模型不适用。代码显式 `LOG_FIRST_N(WARNING,1)` 提示不支持淘汰并忽略回调（[distributed_storage_backend.cpp:149-153](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/storage/distributed/distributed_storage_backend.cpp#L149-L153)）。

---

## 5. 综合实践

> 本任务对应本讲规格指定的代码实践：**阅读 SSD offload 部署文档与 `uring_file.cpp`，说明开启本地 SSD offload 后一个对象的 LOCAL_DISK 副本如何写入与读取，并比较 posix 与 io_uring 后端的 I/O 路径差异。**

### 任务一：跟着文档部署一次（或读懂部署）

依据 [docs/source/deployment/ssd-offload.md](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/ssd-offload.md) 与 [docs/source/design/ssd-offload.md](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/design/ssd-offload.md)：

1. 关键开关：`master` 与 `mooncake_client` 都要 `--enable_offload=true`；设 `MOONCAKE_OFFLOAD_FILE_STORAGE_PATH`、`MOONCAKE_OFFLOAD_STORAGE_BACKEND_DESCRIPTOR=bucket_storage_backend`，并故意把 `--global_segment_size` 设得小于写入量以**触发 offload**（[ssd-offload.md:283-285](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/ssd-offload.md#L283-L285)）。
2. 把 `MOONCAKE_OFFLOAD_USE_URING` 分别设 `false` / `true` 跑两遍。

> 若本地无 NVMe/无编译环境，本步可标注「待本地验证」，但应能复述每个开关的作用。

### 任务二：写清「一个对象的 DISK 副本如何写入」

请结合源码写出 LOCAL_DISK 副本的**写入**全链路（以 Bucket 后端为例）：

1. 心跳线程拿到待下沉对象 → `OffloadObjects` →（GPU 数据先 D2H staging，[file_storage.cpp:489-518](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/file_storage.cpp#L489-L518)）→ `BucketStorageBackend::BatchOffload`（[storage_backend.cpp:1278-1377](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/storage_backend.cpp#L1278-L1377)）。
2. `BuildBucket` 把对象拼成 `[key|value...]` 顺序（[storage_backend.cpp:1956-1986](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/storage_backend.cpp#L1956-L1986)）。
3. `WriteBucket` 写 `<id>.bucket`（UringFile 走 `write_aligned` + `datasync`；PosixFile 走 `vector_write`），再写 `<id>.meta`（[storage_backend.cpp:1988-2127](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/storage_backend.cpp#L1988-L2127)）。
4. `complete_handler` → `NotifyOffloadSuccess` → master 为对象登记 `LOCAL_DISK` 副本（`transport_endpoint`=本机 RPC 地址）。

### 任务三：写清「一个对象的 DISK 副本如何读取」

1. 请求方 `BatchGet` 命中 `LOCAL_DISK(rpc_addr)` → 向目标 client 发 `batch_get_offload_object`。
2. 目标 client `FileStorage::BatchGet` → `AllocateBatch` 分对齐暂存位（[file_storage.cpp:935-980](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/file_storage.cpp#L935-L980)）→ `BatchLoad`。
3. `BucketStorageBackend::BatchLoad`：建 `BucketReadGuard`（保护文件不被淘汰删除），按 `actual_offset = offset + key_size` 读取（[storage_backend.cpp:1396-1546](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/storage_backend.cpp#L1396-L1546)）。
4. 数据进暂存区 → Transfer Engine 零拷贝拉到应用内存 → `release_offload_buffer` 归还暂存位。

### 任务四：比较 posix 与 io_uring 两条 I/O 路径

请填写下表（读路径，对象在 `<id>.bucket` 内偏移 `offset`）：

| 维度 | PosixFile（`use_uring=false`） | UringFile（`use_uring=true`） |
| --- | --- | --- |
| 环形队列 | 无 | 每线程一个 `thread_local` `SharedUringRing` |
| 提交方式 | `::preadv(fd, iov, 1, actual_offset)` 单次同步系统调用 | 切块 → 填最多 32 个 SQE → `io_uring_submit_and_wait` 批量收割 |
| O_DIRECT | 否（走页缓存） | 是（仅读路径），需地址/长度/偏移对齐 4096 |
| 缓冲命中优化 | 无 | 命中 `ClientBuffer` 时 `prep_read_fixed`（固定缓冲，省 pin 开销） |
| 跨线程并发 | 各线程独立系统调用，天然并行 | 各线程独立环，**零锁**并行 |
| 数据拷贝 | 读入 `dest_slice.ptr` | `read_aligned` 直接读入对齐暂存区，仅 `ptr += offset_in_buffer` 前移指针（零拷贝） |

依据：[posix_file.cpp:119-132](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/posix_file.cpp#L119-L132) vs [uring_file.cpp:143-173](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/uring_file.cpp#L143-L173)、[uring_file.cpp:246-270](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/uring_file.cpp#L246-L270)，以及读盘零拷贝 [storage_backend.cpp:1487-1526](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/storage_backend.cpp#L1487-L1526)。

### 交付物

- 一段「写入链路」叙述（含 `.bucket`/`.meta` 的产生顺序与 `datasync` 的写序保证）。
- 一段「读取链路」叙述（含 `BucketReadGuard`、`actual_offset`、暂存区与 Transfer Engine）。
- 上面那张 posix vs io_uring 对照表，并标注哪一项是「开 io_uring 后吞吐提升的主要来源」（答：线程本地环零锁 + 批量提交暴露队列深度 + 固定缓冲零拷贝）。

**参考验证**：你的叙述应与 [ssd-offload.md:122-128](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/ssd-offload.md#L122-L128) 描述的 Load 五步一致；io_uring 部分应与 [ssd-offload.md:200-243](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/design/ssd-offload.md#L200-L243) 的「io_uring File I/O」设计一致。

## 6. 本讲小结

- Mooncake 是**多级存储**：L1=DRAM/VRAM（`MEMORY`，u6-l1）、L2=本地 SSD（`LOCAL_DISK`，本讲主角）、L3=分布式 FS（hf3fs/3FS）与 NVMe-oF 远端 SSD 池（`NOF_SSD`，SPDK 驱动）。
- L2 的**下沉/回填**由 Real Client 内的心跳线程异步驱动（offload/promotion），读取则由请求方 RPC 触发目标 client 读盘，二者共用 O_DIRECT 对齐的 `ClientBuffer` 暂存区。
- L2 的**文件后端**只有 `PosixFile`（同步 `preadv/pwritev`）与 `UringFile`（io_uring）两种 `StorageFile`；`use_uring` 在运行时切换。SPDK 不属于 L2 文件后端，它驱动的是 L3b 的 NVMe-oF 远端 SSD。
- `UringFile` 的吞吐优势来自三点：**每线程一个 io_uring 环（零锁）**、**批量提交暴露 NVMe 队列深度**、**`ClientBuffer` 注册为固定缓冲走 `read_fixed` 零拷贝**；读路径开 `O_DIRECT`，写路径不开。
- `StorageBackendInterface` 统一抽象落盘；`Bucket`（多对象合文件、可淘汰、可恢复，推荐）、`FilePerKey`（一 key 一文件）、`OffsetAllocator`（单大文件、1024 分片、**重启不可恢复**）、`Distributed`（DFS 委托、哈希分桶、不淘汰）各有取舍。
- Bucket 后端用 `BucketReadGuard`(inflight 计数) + 两阶段淘汰保证「淘汰不删正在读的文件、且 master 先于文件被通知」，并用 `datasync` 保证「数据先于 meta 落盘」的崩溃一致性。

## 7. 下一步学习建议

- 本讲聚焦 L2 落盘格式与文件后端，但**心跳如何选对象下沉、master 如何维护 LOCAL_DISK 副本视图、promotion 如何调度**属于控制面，建议结合 [u5-l2 Master Service](u5-l2-master-service.md) 与 `master_service.cpp` 中的 offload/promotion 心跳处理继续阅读。
- 想深入 io_uring 在更大压力下的表现，可阅读 [docs/source/performance/ssd-offload-benchmark-results.md](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/performance/ssd-offload-benchmark-results.md)，并在 `uring_file.cpp` 的 `vector_read` 处观察吞吐日志。
- 对 L3b NVMe-oF 远端 SSD 池感兴趣，可精读 [nvmf-ssd-deployment-guide.md](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/nvmf-ssd-deployment-guide.md) 与 [spdk_wrapper.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/spdk/spdk_wrapper.h)，理解 `NOF_SSD` 副本为何独立于本地 `StorageBackend` 走单独 transfer 路径。
- L3a 分布式 FS 的具体 I/O（3FS 的 `USRBIOResourceManager` 线程资源、chunk 写）可继续阅读 [hf3fs_file.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/hf3fs/hf3fs_file.cpp) 与 [hf3fs_resource_manager.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/hf3fs/hf3fs_resource_manager.cpp)。
