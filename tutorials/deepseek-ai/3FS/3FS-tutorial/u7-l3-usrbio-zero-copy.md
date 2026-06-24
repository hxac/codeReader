# 异步零拷贝 USRBIO API：Iov / Ior / IoRing

> 适用读者：已读过 [u7-l2 FUSE 守护进程](u7-l2-fuse-daemon.md)、[u7-l1 客户端核心](u7-l1-client-core.md)、[u2-l4 网络层 TCP/RDMA](u2-l4-network-rdma.md)。
> 代码版本：HEAD `22fca04`。

## 1. 本讲目标

FUSE 守护进程虽然把 3FS 变成了一个标准文件系统，但它有三个绕不开的瓶颈（见 u7-l2）：每次读写都要在内核与用户态之间拷贝数据、FUSE 的共享队列靠一把自旋锁串行化（实测约 40 万次 4KiB read/s 封顶）、单次 IO 上限 `max_read=1MB`。这些瓶颈对「读大量训练样本」这种吞吐密集型负载是致命的。

USRBIO（**Us**e**R** space **B**ased **IO**，基于用户态环的 IO）就是 3FS 为绕开这些瓶颈而设计的「原生客户端」API。本讲学完后你应当能：

1. 说清楚 **Iov**（数据共享内存）、**Ior**（控制环形队列）各自的职责与协作关系；
2. 画出「客户端 prep → submit → FUSE 批量执行 → wait 收割」的完整时序，并指出其中每一处零拷贝与跨进程同步点；
3. 解释 `io_depth` 三种取值对批处理粒度的影响，以及「一个 ring 也能并行」是怎么做到的；
4. 能写出一个最小 USRBIO 读程序骨架，并知道如何用 `io_depth` 和多 ring 调吞吐。

## 2. 前置知识

- **FUSE low-level 接口**：内核把 VFS 请求以 `fuse_req_t` 形式投递给守护进程，每次都要拷贝（u7-l2）。
- **RDMA Write 单边传输**：对端只要有一段「已注册（pin + `ibv_reg_mr`）」的内存，本端就能直接把数据写进去，CPU 不参与、不经内核（u2-l4、u7-l1 的 `IOBuffer`/`registerIOBuffer`）。
- **io_uring 模型**：Linux 的异步 IO 接口，核心是一个**提交环（SQ）**加一个**完成环（CQ）**，生产者往 SQ 压请求、消费者从 CQ 收结果。USRBIO 的 Ior 借鉴的就是它。
- **POSIX 共享内存与信号量**：`shm_open`/`mmap`（`MAP_SHARED`）让两个进程映射同一块物理内存；`sem_init(pshared=1)` / `sem_open` 让两个进程用同一个信号量同步。USRBIO 的「跨进程」全部建立在这两个原语上。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [`src/lib/api/UsrbIo.md`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/UsrbIo.md) | USRBIO 官方 API 参考文档，讲概念与函数签名 |
| [`src/lib/api/hf3fs_usrbio.h`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/hf3fs_usrbio.h) | 对用户暴露的 C 头文件：`hf3fs_iov`/`hf3fs_ior`/`hf3fs_cqe` 结构体与所有 `hf3fs_*` 函数声明 |
| [`src/lib/api/UsrbIo.cc`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/UsrbIo.cc) | 上述 C API 的实现（运行在**用户进程**里） |
| [`src/lib/common/Shm.h`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/common/Shm.h) / [`Shm.cc`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/common/Shm.cc) | 共享内存抽象 `ShmBuf`：建共享内存、NUMA 绑定、按块做 IB 注册 |
| [`src/fuse/IoRing.h`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/IoRing.h) / [`IoRing.cc`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/IoRing.cc) | **FUSE 侧**的环形队列实现：内存布局、`jobsToProc` 切批、`process` 执行 |
| [`src/fuse/IovTable.h`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/IovTable.h) / `IovTable.cc` | FUSE 侧对所有 Iov/IoRing 共享内存的登记表 |
| [`src/fuse/PioV.h`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/PioV.h) / [`PioV.cc`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/PioV.cc) | 把一批 IO 按文件布局切成 `ReadIO`/`WriteIO` 并交给 `StorageClient` |
| [`src/fuse/FuseClients.cc`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseClients.cc) | FUSE 侧驱动 IoRing 的 watcher / io worker 协程循环 |
| [`src/common/utils/AtomicSharedPtrTable.h`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/AtomicSharedPtrTable.h) | `AvailSlots` 槽位分配器，管理 IoArgs 的空闲下标 |

一个关键认知：**USRBIO 是「一个用户进程 + 一个 FUSE 守护进程」两个进程之间的协议**。`UsrbIo.cc` 跑在用户进程里负责「生产请求 + 收割结果」，`IoRing.*`/`FuseClients.cc` 跑在 FUSE 进程里负责「消费请求 + 真正打 storage」。两边靠共享内存传数据、靠信号量做唤醒。

## 4. 核心概念与源码讲解

### 4.1 Iov 共享内存：零拷贝数据缓冲

#### 4.1.1 概念说明

官方文档对 Iov 的定义是：

> **Iov**: A large shared memory region for zero-copy read/write operations, shared between the user and FUSE processes, with InfiniBand (IB) memory registration managed by the FUSE process. In the USRBIO API, all read data will be read into Iov, and all write data should be written to Iov by user first.
> —— [UsrbIo.md:7](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/UsrbIo.md#L7)

翻译成三个要点：

1. **Iov 是一块大共享内存**，用户进程和 FUSE 进程都 `mmap` 它，物理上是同一页。
2. **IB 内存注册由 FUSE 进程负责**：FUSE 进程把 Iov 注册成 RDMA 可访问内存（pin + `ibv_reg_mr`），这样远端 storage 就能用 RDMA Write 单边把数据直接写进这块内存。
3. **数据流只走一次内存**：读时数据从 SSD → storage 节点 → RDMA Write → Iov，用户进程直接在 Iov 里读，**全程零拷贝、不经内核**；写时用户先把数据写进 Iov，FUSE 再用这块已注册内存发给 storage。

这就绕开了 FUSE 的第一、第三个瓶颈（内核拷贝、单次 1MB 上限）——因为数据根本不走 FUSE 的 read/write 回调，而是走共享内存 + RDMA。

C 头文件里的 `hf3fs_iov` 结构体非常薄，本质上就两个东西：一块内存基址 `base`，和一个不透明句柄 `iovh`（实际是 `ShmBuf*`）：

```c
struct hf3fs_iov {
  uint8_t *base;        // mmap 出来的共享内存基址
  hf3fs_iov_handle iovh; // 实际是 ShmBuf*，由用户进程持有
  char id[16];           // 这块 shm 的 UUID（16 字节），跨进程标识
  char mount_point[256];
  size_t size;
  size_t block_size;     // 非 0 时按块切分注册，优化 IB 注册耗时
  int numa;
};
```
—— [hf3fs_usrbio.h:18-27](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/hf3fs_usrbio.h#L18-L27)

#### 4.1.2 核心流程

创建并使用一块数据 Iov 的完整生命周期：

```
用户进程                                FUSE 进程
─────────                              ─────────
hf3fs_iovcreate
  ├─ shm_open + ftruncate + mmap        （此时 FUSE 还不知道）
  │   = ShmBuf::mapBuf
  ├─ symlink /dev/shm/hf3fs-iov-<uuid>
  │        → <挂载点>/3fs-virt/iovs/<uuid>[.b blkSize]
  └─ iov->base = 映射基址
                                        （用户访问该 symlink 触发 lookup）
                                        IovTable::addIov
                                          ├─ mmap 同一块 shm（同一物理页）
                                          ├─ 按 block_size 逐块
                                          │   registerIOBuffer → ibv_reg_mr
                                          │   = ShmBuf::registerForIO
                                          └─ 记入 iovs 表（按 uuid 索引）

# 读时：
ReadIO.data = iov.base + off           （用户把缓冲指向 Iov）
                                        storage 用 RDMA Write 把数据写进
                                        这块已注册内存 → 用户零拷贝拿到

hf3fs_iovdestroy → shm_unlink + munmap   deregisterForIO → ibv_dereg_mr
```

注意「symlink 进 `3fs-virt/iovs/`」这一步是**跨进程发现的桥梁**：用户进程无权直接碰 FUSE 的内部状态，于是把共享内存的 `/dev/shm` 文件软链接到 3FS 挂载点下的虚拟目录 `3fs-virt/iovs/`。FUSE 守护进程本就在处理这个挂载点的所有 lookup，于是能感知到这个链接、拿到 `/dev/shm` 真实路径、`mmap` 同一块内存并做 IB 注册。文件名后缀还编码了属性（`.b<blockSize>` 等），FUSE 侧解析它（见 4.2.3）。

#### 4.1.3 源码精读

**① 建共享内存：`ShmBuf::mapBuf`** 用标准 POSIX 共享内存，owner 进程负责 `ftruncate` 定大小：

```cpp
auto fd = shm_open(path.c_str(), O_RDWR | (owner_ ? O_CREAT | O_EXCL : 0), 0666);
...
if (owner_) { ftruncate(fd, size); }              // 只有创建者定大小
bufStart = (uint8_t *)mmap(nullptr, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, off);
```
—— [Shm.cc:149-175](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/common/Shm.cc#L149-L175)

`MAP_SHARED` 是零拷贝的关键：两个进程映射同一物理页，一边写另一边立即可见。若指定了 `numa`，还会 `numa_tonode_memory` 把这块内存绑到对应 NUMA 节点（[Shm.cc:28-30](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/common/Shm.cc#L28-L30)），减少跨 socket 访问。

**② 软链接到 3fs-virt/iovs**：用户侧 `hf3fs_iovcreate_general` 把 shm 路径软链接进挂载点的虚拟目录，文件名编码属性：

```cpp
auto target = hf3fs::Path("/dev/shm") / p;   // p = /hf3fs-iov-<uuid>
auto link = fmt::format("{}/3fs-virt/iovs/{}{}...",
                        hf3fs_mount_point, shm->id.toHexString(),
                        block_size ? fmt::format(".b{}", block_size) : ...);
auto lres = symlink(target.c_str(), link.c_str());
```
—— [UsrbIo.cc:146-159](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/UsrbIo.cc#L146-L159)

**③ FUSE 侧感知并做 IB 注册**：`ShmBuf::registerForIO` 按 `block_size` 把整块内存切成多段，每段独立调 `StorageClient::registerIOBuffer`（内部 `ibv_reg_mr`），结果存进 `memhs_`（每块一个 `atomic_shared_ptr<IOBuffer>`）：

```cpp
for (size_t i = 0; i < memhs_.size(); ++i) {
  auto res = sc.registerIOBuffer(bufStart + blockSize * i,
                                 std::min(size - blockSize * i, blockSize));
  ...
  memhs_[i].store(std::make_shared<storage::client::IOBuffer>(std::move(*res)));
}
```
—— [Shm.cc:101-117](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/common/Shm.cc#L101-L117)

> 为什么有 `block_size` 参数？IB 注册（pin 内存）是耗时操作。把一块大内存切成多个不超过 `block_size` 的小块分别注册，可以并行化、降低单次注册延迟；代价是 **单次 IO 不能跨越块边界**（[Shm.h:90-92](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/common/Shm.h#L90-L92) 的 `ShmBufForIO::memh` 会校验）。`block_size=0` 表示整块一个 mr。

**④ 登记表**：FUSE 侧用 `IovTable` 管理所有 Iov，核心是一个按槽位索引的并发安全表：

```cpp
robin_hood::unordered_map<Uuid, int> shmsById;          // uuid → 槽位下标
std::unique_ptr<AtomicSharedPtrTable<lib::ShmBuf>> iovs; // 槽位 → ShmBuf
```
—— [IovTable.h:32-33](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/IovTable.h#L32-L33)

执行 IO 时，FUSE 用请求里携带的 `bufId`（即 Iov 的 uuid）在这张表里查回 `ShmBuf`，进而拿到对应的 `IOBuffer`（RDBA mr 句柄）——见 4.3.3 的 `lookupBufs`。

#### 4.1.4 代码实践（源码阅读型）

**目标**：验证「数据 Iov 会被 IB 注册，而 ring 的控制内存不会被注册」这一设计。

**步骤**：

1. 打开 [IovTable.cc:222](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/IovTable.cc#L222) 附近，找到 `if (!iovaRes->isIoRing) { ... registerForIO ... }`，注释写明 `io ring bufs don't need to be registered for ib io`。
2. 思考：ring 控制内存里只放 IoArgs/IoCqe/IoSqe 和信号量，本身不被 RDMA 传输，所以无需注册；真正被 storage RDMA 写入的是**数据 Iov**。

**预期观察**：理解为什么 3FS 把「控制平面（ring）」和「数据平面（Iov）」分成两块独立共享内存——控制平面小而频繁、走信号量唤醒；数据平面大而需要 IB 注册、走 RDMA。两者解耦后，注册耗时只落在数据 Iov 上。

#### 4.1.5 小练习与答案

**练习 1**：如果一块 1 GiB 的 Iov 用 `block_size=0` 创建，相比 `block_size=64 MiB`，IB 注册阶段会有什么差异？

**答案**：`block_size=0` 时整块 1 GiB 作为单个 mr 一次性 `ibv_reg_mr`，注册延迟集中、单次较长；`block_size=64 MiB` 时切成 16 块分别注册，可并行、单次延迟低，但之后任何一次 IO 都不得跨越 64 MiB 块边界（否则 `ShmBufForIO::memh` 返回 `kInvalidArg`）。这是「注册耗时」与「IO 对齐约束」的权衡。

**练习 2**：为什么 Iov 的 IB 注册放在 FUSE 进程做，而不是用户进程自己做？

**答案**：因为真正发起 RDMA 传输、持有 RDMA 连接的是 FUSE 进程里的 `StorageClient`（u7-l1）。mr 必须注册在发起 RDMA 请求的进程地址空间里，且 `rkey` 要随请求带给 storage 节点。用户进程一般没有 IB 设备上下文，统一由 FUSE 注册、`rkey` 由 FUSE 在组包时填入，架构上更简单。

---

### 4.2 Ior 环形通信：io_uring 式的提交/完成模型

#### 4.2.1 概念说明

Iov 解决了「数据怎么零拷贝传递」，但「请求本身怎么递交、结果怎么回收」还需要一个通道——这就是 **Ior**：

> **Ior**: A small shared memory ring for communication between user process and FUSE process. The usage of Ior is similar to Linux io-uring, where the user application enqueues read/write requests, and the FUSE process dequeues these requests for completion.
> —— [UsrbIo.md:9](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/UsrbIo.md#L9)

Ior 是一块**小**共享内存，里面摆着三个环形数组 + 四个游标 + 两个信号量，模拟 io_uring 的 SQ/CQ 双环：

| 组成 | 含义 | 谁写 | 谁读 |
| --- | --- | --- | --- |
| `IoArgs[]`（ringSection） | 每个 IO 的参数：缓冲 id/偏移、文件 inode/偏移/长度、userdata | 用户 prep 时写 | FUSE process 时读 |
| `IoSqe[]`（sqeSection） | 提交环：每项是一个 `index`，指向某个 IoArgs 槽 | 用户 addSqe 推进 sqeHead | FUSE jobsToProc 读 |
| `IoCqe[]`（cqeSection） | 完成环：每项是 `{index, result, userdata}` | FUSE addCqe 推进 cqeHead | 用户 wait 时读 |

C 头文件里的 `hf3fs_ior` 同样很薄，`iovh` 指向这块控制共享内存（注意：`hf3fs_ior` 的第一个成员就是 `struct hf3fs_iov iov`——**Ior 本身就建立在一块 Iov 共享内存之上**，这块小 Iov 容纳环形控制结构）：

```c
struct hf3fs_ior {
  struct hf3fs_iov iov;   // 这块「控制 Iov」容纳 SQ/CQ 环 + 信号量
  hf3fs_ior_handle iorh;  // 实际是 Hf3fsIorHandle*（含 IoRing 对象 + submit 信号量）
  char mount_point[256];
  bool for_read;          // 一个 ring 只能读或只能写，不能混
  int io_depth;           // 批处理粒度，见 4.3
  int priority; int timeout; uint64_t flags;
};
```
—— [hf3fs_usrbio.h:32-49](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/hf3fs_usrbio.h#L32-L49)

完成项 `hf3fs_cqe` 就是 `{index, result, userdata}`（[hf3fs_usrbio.h:51-56](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/hf3fs_usrbio.h#L51-L56)），`result>0` 为读/写字节数，`<0` 为 `-errno`。

> ⚠️ 重要约束（来自 [hf3fs_usrbio.h:148-151](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/hf3fs_usrbio.h#L148-L151) 与 [UsrbIo.md:240](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/UsrbIo.md#L240)）：**同一个 ring，prep/submit 只能由一个线程调用，wait 也只能由一个线程调用**（这两者可以是不同线程）。因为 prep 不加锁，多线程 prep 会让批次错乱。多线程应用应**每线程一个 ring**。

#### 4.2.2 核心流程

一次完整读的时序（横轴为时间，两进程靠信号量互相唤醒）：

```
用户进程 (prep 线程)          FUSE 进程 (watcher)         FUSE 进程 (io worker)
─────────────────────         ──────────────────          ─────────────────────
hf3fs_prep_io × N
  slots.alloc → idx           (在 sem_timedwait
  IoArgs[idx] = {bufId,       (  submitSem 上阻塞)
    bufOff,fileIid,fileOff,   (
    ioLen,userdata}           (
  addSqe(idx): sqeHead++      (
                              (
hf3fs_submit_ios              (
  sem_post(submitSem) ──────► 醒来, jobsToProc:
                                按 io_depth 从 SQ 切出
                                若干 job(连续段), 入 iojqs
                                                            取一个 job
                                                            IoRing::process(spt,toProc)
                                                              把 IoArgs 翻译成 ReadIO
                                                              PioV.executeRead
                                                                StorageClient.batchRead
                                                                  (storage RDMA Write→数据 Iov)
                                                              addCqe × toProc: cqeHead++
                                                              sem_post(cqeSem) ──┐
hf3fs_wait_for_ios                                                      (            │
  sem_timedwait(cqeSem) ◄──────────────────────────────────────────────┘            │
  从 CQE 拷结果, cqeTail++                                                          │
  slots.dealloc(cqe.index)                                                         │
  sem_post(submitSem) ──► (通知有 CQE 空位/新槽可用)
```

注意三个零拷贝/免内核的设计点：① IO 参数走共享内存环，不经 `read`/`write` 系统调用；② 数据本身走数据 Iov 的 RDMA，不经内核；③ 唤醒用 POSIX 信号量（`submitSem` 唤生产、`cqeSem` 唤消费），而非内核 FUSE 队列。唯一的目的地是「storage 节点 → 数据 Iov」这一跳 RDMA。

#### 4.2.3 源码精读

**① 环的内存布局**：`IoRing` 构造函数把传入的 `buf`（控制 Iov 的基址）按固定偏移切成四段游标 + 三段环形数组 + 一个信号量。所有游标用 `std::atomic_ref<int32_t>` 跨进程原子访问：

```cpp
sqeHead_((int32_t *)buf),                              // 4 个游标各占 ringMarkerSize()
sqeTail_((int32_t *)(buf + ringMarkerSize())),
cqeHead_((int32_t *)(buf + ringMarkerSize() * 2)),
cqeTail_((int32_t *)(buf + ringMarkerSize() * 3)),
ringSection((IoArgs *)(buf + ringMarkerSize() * 4)),   // IoArgs[entries]
cqeSection((IoCqe *)(ringSection + entries)),          // IoCqe[entries]
sqeSection((IoSqe *)(cqeSection + entries)),           // IoSqe[entries]
slots(entries - 1),                                    // 预留 1 个槽区分满/空
...
auto sem = (sem_t *)(sqeSection + entries);
if (owner) { sem_init(sem, 1, 0); }                    // cqeSem, pshared=1
```
—— [IoRing.h:93-119](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/IoRing.h#L93-L119)

`bytesRequired`/`ioRingEntries` 给出容量与字节数的换算（[IoRing.h:60-71](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/IoRing.h#L60-L71)），用户侧 `hf3fs_ior_size(entries)` 就调它来算要建多大的控制共享内存（[UsrbIo.cc:292](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/UsrbIo.cc#L292)）。

> **谁 `sem_init`？** `owner` 默认 `true`。FUSE 侧 `IoRingTable::addIoRing` 建对象时用默认 owner，于是 **FUSE 负责初始化这个跨进程无名信号量**；用户侧 `hf3fs_iorwrap` 显式传 `owner=false`（[UsrbIo.cc:398](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/UsrbIo.cc#L398)），只 mmap、不重复 init。两个进程共享同一个位于共享内存里的信号量。

**② 用户侧 prep（生产 SQ）**：`hf3fs_prep_io` 取一个空闲 IoArgs 槽、填参数、再把槽号压进 SQE 环：

```cpp
auto idx = ring.slots.alloc();        // 取空闲 IoArgs 下标
if (!idx) { return -EAGAIN; }         // 环满
auto &args = ring.ringSection[*idx];
memcpy(args.bufId, iov->id, ...);     // 数据 Iov 的 uuid（注意：不是控制 Iov）
args.bufOff = p - iov->base;          // 在数据 Iov 内的偏移
args.fileIid = regfd->iid.u64();      // 已注册 fd 对应的 inode id
args.fileOff = off; args.ioLen = len; args.userdata = userdata;
ring.addSqe(*idx, userdata);          // 压入 SQE 环, sqeHead 原子 +1
return *idx;
```
—— [UsrbIo.cc:644-669](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/UsrbIo.cc#L644-L669)

这里体现了「数据 Iov」与「控制 Iov」的解耦：`prep_io` 收 `ior`（控制环）和独立的 `iov`（数据缓冲），把数据缓冲的 uuid/偏移写进 IoArgs，FUSE 侧执行时再用这个 uuid 去查真正的 `ShmBuf`。

> **`hf3fs_reg_fd`**（[UsrbIo.cc:558-596](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/UsrbIo.cc#L558-L596)）：Linux fd 只在用户进程内有意义，FUSE 不知道它对应哪个 inode。注册时 `statx` 取出 inode id、`dup` 一份保活，返回一个 `<=0` 的「USRBIO 专用 fd」，之后 `prep_io` 同时接受原始 fd 或这个专用 fd。这就是文档说的「让 prep 接口更像 io_uring 的 `liburing` 对应物」。

**③ submit（唤醒 FUSE）**：`hf3fs_submit_ios` 只是 `sem_post(submitSem)` 一下——它只是一个**提示**。文档明确：FUSE 也会周期性扫描，即使不 submit 请求也可能已开始执行（[UsrbIo.md:202](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/UsrbIo.md#L202)）。`submitSem` 是**命名信号量**（`sem_open`），按优先级有 3 个，路径通过 `3fs-virt/iovs/submit-ios*` 软链接暴露给用户进程（[IoRing.h:217-228](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/IoRing.h#L217-L228)、[UsrbIo.cc:332-370](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/UsrbIo.cc#L332-L370)）。

**④ FUSE 侧收割结果回填 CQE**：执行完后 `addCqe` 把 `{index, result, userdata}` 压进 CQE 环、推进 `cqeHead`，再 `sem_post(cqeSem)` 唤醒用户（[IoRing.cc:259-271](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/IoRing.cc#L259-L271)）。其中 `result` 若为负会被转成 `-errno`（`StatusCode::toErrno`）。

**⑤ 用户侧 wait（消费 CQE）**：`hf3fs_wait_for_ios` 在 `cqeSem` 上 `sem_timedwait`，醒来后从 CQE 环逐项拷出、用 CAS 推进 `cqeTail`（防多消费者竞争）、`slots.dealloc` 释放 IoArgs 槽，并 `sem_post(submitSem)` 通知 FUSE「有空位了」（[UsrbIo.cc:706-737](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/UsrbIo.cc#L706-L737)）。`min_results`/`abs_timeout` 控制最少返回数与超时。

**⑥ 槽位分配器 `AvailSlots`**：IoArgs 的空闲下标用一个集合 + 单调计数器管理，`alloc` 优先复用已释放的槽、否则递增；`dealloc` 时若释放的是末尾则直接回退计数器、否则入 `free` 集合（[AtomicSharedPtrTable.h:10-49](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/AtomicSharedPtrTable.h#L10-L49)）。这样 **SQE 环（提交顺序）与 IoArgs 槽（数据载体）解耦**：一个 Sqe 只存 `index`，槽可乱序复用。

#### 4.2.4 代码实践（源码阅读型）

**目标**：跟踪「一个 prep 出来的 IO，它的 `userdata` 是如何原样回到用户手里的」。

**步骤**：

1. 在 [UsrbIo.cc:655](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/UsrbIo.cc#L655) 看 `args.userdata = userdata;`，同时 `addSqe(*idx, userdata)`（[UsrbIo.cc:658](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/UsrbIo.cc#L658)）。
2. 在 [IoRing.cc:259-265](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/IoRing.cc#L259-L265) 看 `addCqe(sqe.index, result, sqe.userdata)`——userdata 从 Sqe 原样搬到 Cqe。
3. 在 [UsrbIo.cc:720](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/UsrbIo.cc#L720) 看 `cqes[filled].userdata = cqe.userdata;`——原样回填给用户。

**预期观察**：`userdata` 是用户任意指针（如「这是第几个训练样本」），全程不被 FUSE 解引用，只作搬运，因此用户可用它在 wait 后把结果对回原始请求。这是 io_uring 风格异步 API 的标准用法。

#### 4.2.5 小练习与答案

**练习 1**：为什么 SQE 环要预留 1 个槽（`entries - 1` 可用）？

**答案**：环形缓冲区分满与空的需要。若全部 `entries` 都可用，则「head 追上 tail（满）」与「head==tail（空）」无法区分。预留 1 槽后，`(head+1) % entries == tail` 表示满、`head == tail` 表示空。见 [IoRing.h:60-65](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/IoRing.h#L60-L65) 与 `addSqe` 的判满 [IoRing.h:133-137](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/IoRing.h#L133-L137)。

**练习 2**：`submit_ios` 只是「提示」，那不调用它请求也会执行吗？为什么这样设计？

**答案**：会。FUSE 的 watcher 除了在 `submitSem` 上等待，还会周期性主动扫描所有 ring（见 4.3.3 `watch` 循环与 `jitter`）。即使应用忘了 submit 或 submit 的 `sem_post` 丢失，请求最终也会被发现执行。这样降低了「必须精确配对 submit」的心智负担，也容忍信号量的偶尔竞态。

---

### 4.3 批处理与并行：io_depth 与多 worker 执行

#### 4.3.1 概念说明

到目前为止，USRBIO 已能零拷贝递交请求。但要喂饱 storage 的并发，还得解决「多少个请求攒成一批一起发」「能不能并行」。这就引出本讲最后一个、也是最影响吞吐的参数 **`io_depth`**，以及「一个 ring 多 worker 并行」的设计。

`io_depth` 的三种语义（综合 [UsrbIo.md:37-38](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/UsrbIo.md#L37-L38) 与 [hf3fs_usrbio.h:38-44](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/hf3fs_usrbio.h#L38-L44)）：

| `io_depth` | 语义 | 适用场景 |
| --- | --- | --- |
| `== 0` | 不控批，FUSE 一发现就尽量全发（`toProc = min(sqes, cqeAvail)`） | 通用、尽快排空 |
| `> 0` | 攒够恰好 `io_depth` 个才发一批；不够就等 | 「正好读一个训练 batch」——凑齐再下发，避免零散请求 |
| `< 0` | 最多 `-io_depth` 个一批；若不足则最多等 `timeout` 凑数 | 请求太多、想限单批规模又有一定攒批 |

并行方面，源码注释一针见血：

> we allow multiple io workers to process the same ioring, but different ranges ... so 1 ioring can be used to submit ios processed in parallel
> —— [IoRing.h:49-52](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/IoRing.h#L49-L52)

也就是说，`jobsToProc` 一次可以从 SQ 切出**多个不重叠的连续段（job）**，分别丢给多个 io worker 协程并发执行——所以「同一个 ring 也能并行」。配合「多 ring 给多线程」，并行度可以很高。

#### 4.3.2 核心流程

**切批（`jobsToProc`）** 在 FUSE 侧完成，伪代码（简化自 [IoRing.cc:15-65](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/IoRing.cc#L15-L65)）：

```
sqes       = SQ 中待处理数 = (sqeHead - sqeProcTail) % entries
cqeAvail   = CQE 空位数 = entries-1 - processing - cqeCount
while sqes > 0 and jobs.size() < maxJobs:
  if io_depth > 0:
     toProc = io_depth
     if toProc > sqes or toProc > cqeAvail: break   # 凑不齐 / 没处放结果，先不发
  else:
     toProc = min(sqes, cqeAvail)
     if io_depth < 0:
        toProc = min(toProc, -io_depth)              # 单批上限
        if toProc < -io_depth and timeout>0:         # 没凑够，等一个 timeout 窗口
           ... 首次记录 lastCheck 后 break，到点才发
  记 job = {ior, sqeProcTail=spt, toProc}
  spt = (spt + toProc) % entries
  sqeProcTails_.push_back(spt)   # 标记这段在途
  processing += toProc
```

两个关键约束：

- **`cqeAvail` 守门**：发出去的请求必须有地方放结果。若 CQE 环快满了（用户没及时 wait），就暂停发新批，避免覆盖未取走的结果——这就是 `toProc > cqeAvail` 时 `break`。
- **乱序完成保序推进**：多个 job 并发执行，后切的 job 可能先完成。`sqeProcTails_` 是一个 deque 记录所有在途段的尾界，`sqeDoneTails_` 记已完成段。只有当**最前面（最老）那段**完成时才推进真正的 `sqeTail`，并顺带把连续已完成的后继段一起推进（[IoRing.cc:234-257](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/IoRing.cc#L234-L257)）。这样「提交按序、完成可乱序、对外可见的 tail 单调」三者并存。

**执行（`process`）** 把一段 job 翻译成底层 `ReadIO`/`WriteIO` 后批量下发：

```
for i in [spt, spt+toProc):
  sqe = sqeSection[idx]; args = ringSection[sqe.index]
  inode = lookupFiles(...)            # fileIid → RcInode
  buf   = lookupBufs(...)             # bufId → ShmBuf，再 memh → IOBuffer(mr)
  PioV.addRead(i, inode, off, len, buf.ptr, memh)   # 内部按 chunk 切成 ReadIO
PioV.executeRead → StorageClient.batchRead          # 一次性批量发往 storage
addCqe(每个, result) → sem_post(cqeSem)
```
—— [IoRing.cc:105-207](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/IoRing.cc#L105-L207)

`PioV` 负责把「用户视角的一段文件 IO」按文件布局（chunkSize、stripe、chain）切成对 storage 的 `ReadIO`（[PioV.cc:98-130](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/PioV.cc#L98-L130) 的 `chunkIo`），最终 `StorageClient::batchRead` 并发打 storage（[PioV.cc:132-140](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/PioV.cc#L132-L140)）。这正是 u7-l1 讲过的 IO 合并/分流流水线，这里只是入口换成了 IoRing 而非 FUSE read 回调。

#### 4.3.3 源码精读

**① watcher 发现请求**：FUSE 每个 priority 档有一个 watcher 协程，在对应 `submitSem` 上等，醒来后扫所有该 priority 的 ring，`jobsToProc(maxJobsPerRing)` 切批入队，循环到再切不出 job 为止：

```cpp
void FuseClients::watch(int prio, std::stop_token stop) {
  while (!stop.stop_requested()) {
    ... sem_timedwait(iors.sems[prio].get(), &ts) ...   // 等 submitSem（或超时主动扫）
    do {
      gotJobs = false;
      auto n = iors.ioRings->slots.nextAvail.load();
      for (int i = 0; i < n; ++i) {
        auto ior = iors.ioRings->table[i].load();
        if (ior && ior->priority == prio) {
          auto jobs = ior->jobsToProc(config->max_jobs_per_ioring());
          for (auto &&job : jobs) { gotJobs = true; iojqs[prio]->enqueue(std::move(job)); }
        }
      }
    } while (gotJobs);
  }
}
```
—— [FuseClients.cc:369-401](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseClients.cc#L369-L401)

**② io worker 执行并接力**：worker 从 `iojqs` 取 job 调 `process`，执行完若同 ring 还有积压，就地再切一批续上（避免来回跑 watcher）：

```cpp
co_await job.ior->process(job.sqeProcTail, job.toProc, *storageClient, ...);
...
auto jobs = job.ior->jobsToProc(1);   # 续切一批
if (!jobs.empty()) { job = jobs.front(); iojqs[0]->try_enqueue(job); }
```
—— [FuseClients.cc:335-353](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseClients.cc#L335-L353)

**③ 两个 lookup 回调**：`process` 通过两个 `std::function` 把 `fileIid`/`bufId` 解析成真实对象。`lookupBufs` 用 `bufId` 在 `iovs.shmsById` 查 `ShmBuf`，并校验偏移不越界（[FuseClients.cc:296-333](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseClients.cc#L296-L333)）——这就是 4.1 里「数据 Iov 按 uuid 登记」的消费点。

**④ 监控埋点**：`process` 顶部声明了一组 `LatencyRecorder`/`DistributionRecorder`，分别记录 prepare/submit/complete 各段时延，以及 `io_depth`、`total_bytes`、`distinct_files`、`distinct_bufs` 分布（[IoRing.cc:75-87](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/IoRing.cc#L75-L87)）。调优时可在监控里直接看 `usrbio.piov.io_depth`、`usrbio.piov.bw` 等指标（与 u8-l3 监控讲义呼应）。

> **flags 小贴士**：`hf3fs_iorcreate4` 的 `flags` 支持 `HF3FS_IOR_ALLOW_READ_UNCOMMITTED`（允许读到尚未完全 commit 的数据，换延迟优势）与 `HF3FS_IOR_FORBID_READ_HOLES`（遇到 hole 不零填充而报错），见 [hf3fs_usrbio.h:122-123](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/hf3fs_usrbio.h#L122-L123)，在 `process` 里对应 `readOpt.set_allowReadUncommitted` 与 `finishIo(!FORBID...)`（[IoRing.cc:189-206](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/IoRing.cc#L189-L206)）。

#### 4.3.4 代码实践（可运行型 / 待本地验证）

**目标**：写一个最小 USRBIO 读程序，通过对比 `io_depth=0`、`io_depth>0`、`io_depth<0` 与「多 ring」四种配置，理解批处理与并行对吞吐的影响。

**前置**：需要一个已挂载的 3FS（挂载点如 `/hf3fs/mnt`）和编译好的 `libhf3fs`（见 u7-l4）。下面骨架改编自官方示例 [UsrbIo.md:242-275](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/UsrbIo.md#L242-L275)。

```c
// 示例代码：最小 USRBIO 读
#include <hf3fs_usrbio.h>
#include <fcntl.h>
#include <time.h>

constexpr uint64_t NUM_IOS = 1024;
constexpr uint64_t BLOCK   = (32 << 20);  // 32 MiB / IO

int main() {
    const char *mp = "/hf3fs/mnt";

    // 1) 建 Ior：entries=并发数, for_read=true, io_depth 在此调参
    struct hf3fs_ior ior;
    hf3fs_iorcreate4(&ior, mp, NUM_IOS, /*for_read=*/true,
                     /*io_depth=*/0 /*试着改成 64 或 -64*/, 0, -1, 0);

    // 2) 建数据 Iov：要装得下 NUM_IOS*BLOCK 字节
    struct hf3fs_iov iov;
    hf3fs_iovcreate(&iov, mp, NUM_IOS * BLOCK, /*block_size=*/0, -1);

    // 3) 打开并注册 fd
    int fd = open("/hf3fs/mnt/example.bin", O_RDONLY);
    hf3fs_reg_fd(fd, 0);

    // 4) prep 一批读，每个 IO 落在 iov 的不同段
    for (int i = 0; i < NUM_IOS; i++)
        hf3fs_prep_io(&ior, &iov, /*read=*/true,
                      iov.base + i * BLOCK, fd, i * BLOCK, BLOCK, (void *)(long)i);
    hf3fs_submit_ios(&ior);

    // 5) 收割
    struct hf3fs_cqe cqes[NUM_IOS];
    hf3fs_wait_for_ios(&ior, cqes, NUM_IOS, NUM_IOS, nullptr);

    hf3fs_dereg_fd(fd); close(fd);
    hf3fs_iovdestroy(&iov);
    hf3fs_iordestroy(&ior);
    return 0;
}
```

**操作步骤与观察**：

1. 先用 `io_depth=0` 跑，记录总耗时；改为 `io_depth=64` 再跑——后者会「攒满 64 才发」，单批更大、对 storage 的并发更饱满，通常吞吐更高但首 IO 延迟略升。
2. 改为 `io_depth=-64, timeout=10`（`hf3fs_iorcreate4` 第 6 参）：观察它「最多 64 一批、不足时等 10ms 凑数」的行为。
3. 多线程对比：开 4 个线程，**每线程各建一个独立 ring**（不要共享！）跑同样负载，对比「单 ring 串行 prep」与「4 ring 并行 prep」的总耗时。
4. 结合监控指标 `usrbio.piov.io_depth` 与 `usrbio.piov.bw`（见 4.3.3 ④）观察实际批深与带宽。

**预期结果 / 待本地验证**：在有真实 3FS 集群与 RDMA 环境时，`io_depth>0` 与多 ring 通常能显著提升稳态吞吐；具体数值依赖 SSD 数量与 client↔storage 对分带宽，**待本地验证**。本实践若无集群，则退化为「源码阅读型」：重点验证 4 个配置点（`io_depth`、`for_read`、`block_size`、多 ring）分别对应源码中哪一处分支。

#### 4.3.5 小练习与答案

**练习 1**：`io_depth=8`，但用户一次只 prep 了 3 个请求就 `submit_ios`，会发生什么？

**答案**：不会立即下发。`jobsToProc` 中 `io_depth>0` 分支要求 `toProc <= sqes` 且凑够 `io_depth`，3<8 故 `break`，这 3 个请求会等在那，直到凑够 8 个、或被 watcher 的周期扫描机制兜底（4.2 的「submit 只是提示」）。这正是「读一个完整训练 batch」想要的「凑齐再发」语义。

**练习 2**：为什么「一个 ring 多 worker 并行」还要强调「多线程应用应每线程一个 ring」？两者不矛盾吗？

**答案**：不矛盾。并行有两层：① **执行并行**（FUSE 侧多 worker 同时跑同一 ring 的不同段）——这是 FUSE 内部行为，对用户透明；② **prep 并行**（用户侧多线程同时 prep）。prep 不加锁（[hf3fs_usrbio.h:148-151](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/hf3fs_usrbio.h#L148-L151)），多线程 prep 同一 ring 会让 `slots.alloc`/`addSqe` 错乱、批次混合。所以用户侧要并行就开多个 ring，而每个 ring 内部的执行并行由 FUSE 自动保证。

**练习 3**：若用户 `wait_for_ios` 很慢，CQE 环被填满，系统如何避免覆盖未取走的结果？

**答案**：`jobsToProc` 用 `cqeAvail = entries-1-processing-cqeCount` 守门，当 `toProc > cqeAvail` 时直接 `break` 不再发新批（[IoRing.cc:22-29](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/IoRing.cc#L22-L29)）。即「消费不动就暂停生产」，形成反压（backpressure），保证结果不丢。

## 5. 综合实践

把三个模块串起来：实现一个**多 ring、可调 io_depth 的 USRBIO 读压测骨架**，并解释每一处设计对应本讲哪个概念。

要求：

1. **数据平面**：用 `hf3fs_iovcreate` 建一块足够大的数据 Iov，说明它为什么会被 FUSE 做 IB 注册、数据如何零拷贝回到用户（对应 4.1）。
2. **控制平面**：为每个工作线程建一个独立 `hf3fs_ior`（`hf3fs_iorcreate4`），`for_read=true`，`io_depth` 作为命令行参数（对应 4.2、4.3）。
3. **fd 注册**：`open` + `hf3fs_reg_fd`，说明为什么必须注册（对应 4.2.3 的 reg_fd）。
4. **生产-消费**：每线程循环 `prep_io → submit_ios`，主线程或同线程 `wait_for_ios` 收割，统计吞吐。
5. **调参对比**：固定总数据量，分别测 `(io_depth=0, 单 ring)`、`(io_depth=64, 单 ring)`、`(io_depth=64, 4 ring)` 三组，记录吞吐与 `usrbio.piov.*` 监控指标。
6. **画时序图**：任取一组配置，画出「用户 prep/submit → submitSem → watcher → iojqs → process → batchRead → addCqe → cqeSem → wait」的完整跨进程时序，标注每个共享内存段与信号量。

**验收**：能口头解释清楚「为什么 `io_depth>0` 能提升吞吐、为什么多 ring 能再提升、反压如何防止丢结果」三点，即说明三个最小模块均已掌握。运行数据**待本地验证**（需真实 3FS 集群 + RDMA）。

## 6. 本讲小结

- USRBIO 是 3FS 为绕开 FUSE 三大瓶颈（内核拷贝、共享队列自旋锁、单次 1MB 上限）而设计的**用户态原生客户端 API**，本质是「用户进程 + FUSE 进程」两进程间的共享内存 + 信号量协议。
- **Iov** 是大块数据共享内存，由 FUSE 进程做 IB 注册（`registerIOBuffer`→`ibv_reg_mr`），storage 用 RDMA Write 单边写入，实现**数据零拷贝**；控制内存（ring）与数据内存（Iov）分离，只有后者需注册。
- **Ior** 是小块控制共享内存，内含 IoArgs/IoCqe/IoSqe 三环 + 四个原子游标 + 两个信号量，模仿 io_uring 的 SQ/CQ 模型；prep 产 SQ、FUSE 消费、FUSE 产 CQE、用户 wait 消费。
- 跨进程同步：`submitSem`（命名信号量，按 priority 分 3 档）唤醒 FUSE 生产；`cqeSem`（无名信号量，FUSE 侧 `sem_init`）唤醒用户收割；`submit_ios` 仅是提示，FUSE 还会周期扫描兜底。
- **`io_depth` 三态**（0=不控批、>0=凑齐 N 才发、<0=最多 N 一批+timeout 凑数）控制批处理粒度；`jobsToProc` 用 `cqeAvail` 反压、用 `sqeProcTails_/sqeDoneTails_` 保「提交有序、完成可乱序、tail 单调」。
- **并行**有两层：FUSE 多 worker 可并发跑同一 ring 的不同段（执行并行，对用户透明）；用户多线程则应每线程一个 ring（prep 不加锁）。
- `process` 把 IoArgs 经 `PioV` 按文件布局切成 `ReadIO`/`WriteIO`，最终走 `StorageClient::batchRead/batchWrite`，复用 u7-l1 的 IO 合并/分流流水线，全程埋点 `usrbio.piov.*` 指标。

## 7. 下一步学习建议

- **[u7-l4 Python 绑定 hf3fs](u7-l4-python-bindings.md)**：看 `hf3fs`/`hf3fs_utils` 如何用 pybind11 把本讲的 C API 包成 Python 可用的 Iov/Ior，供训练 Dataloader 直接零拷贝读样本——这是 USRBIO 最常见的实际入口。
- **回看 [u7-l1 客户端核心](u7-l1-client-core.md) 的 `StorageClient::batchRead` 与 `IOBuffer`/`registerIOBuffer`**：本讲 `PioV::executeRead` 最终调用的就是它，理解 USRBIO 如何复用而非重写数据面。
- **[u8-l3 监控](u8-l3-monitor-and-analytics.md)**：本讲提到的 `usrbio.piov.io_depth`、`usrbio.piov.bw`、`fuse.iov.*` 等指标都经 monitor_collector 上报 ClickHouse，调优 USRBIO 吞吐时是第一手数据。
- **延伸阅读**：对照 Linux `io_uring` 的 SQ/CQE 设计，体会 USRBIO「把 io_uring 从『内核↔用户』搬到『FUSE↔用户』」的巧妙之处；并思考若没有 RDMA，这套零拷贝还能否成立（提示：退化成 FUSE 进程内拷贝，回到 u7-l2 的瓶颈）。
