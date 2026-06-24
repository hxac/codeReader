# FUSE 守护进程与请求分发

## 1. 本讲目标

本讲承接 u7-l1（客户端核心 Meta / Storage / Mgmtd Client），聚焦 3FS 客户端的「外壳」——FUSE 守护进程（`hf3fs_fuse`）。学完本讲，你应当能够：

- 说清 FUSE 守护进程从 `main` 到阻塞在内核请求循环的完整启动过程；
- 解释一个 VFS 请求（如 `read`）如何被内核投递、经 `fuse_lowlevel_ops` 回调表分发到 `FuseOps`，再落到 `MetaClient` / `StorageClient`；
- 理解 `FuseClients` 这个聚合体如何把 mgmtd / meta / storage 三类客户端、inode 缓存、USRBIO 资源池装进同一个进程；
- 说透 FUSE 的性能瓶颈（内存拷贝、共享队列自旋锁、单次 IO 上限），并理解 3FS 为什么要在 FUSE 守护进程内部再实现一套「原生客户端」（USRBIO，见 u7-l3）。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**FUSE 是什么。** FUSE（Filesystem in Userspace）是 Linux 的一个内核模块。普通应用调用 `read()` 时，内核本应直接去磁盘取数据；而在 FUSE 挂载点上，内核会把这个 `read()` 请求「转交」给一个用户态进程（即 FUSE 守护进程）来处理，处理完再把结果交还内核、最终返回给应用。这样，文件系统的逻辑可以完全写在用户态，开发与升级都安全，代价是多了一次「内核 ↔ 用户态」的往返。

**请求的投递模型。** 内核把待处理的请求放进一个**多线程共享队列**（从 `/dev/fuse` 字符设备读取），守护进程的一组工作线程从该队列里取请求、回调处理函数。请求和处理结果之间用 `fuse_req_t` 这个不透明句柄配对——拿到请求后，**必须**调用 `fuse_reply_*` 系列函数之一回包，否则应用会一直阻塞。这是理解 `FuseOps.cc` 里每个回调「先处理、再 reply」套路的钥匙。

**low-level vs high-level API。** libfuse 提供两套接口。high-level API 以「路径名」为单位（更像普通文件操作），low-level API 以「inode 号」为单位、更贴近内核真实语义。3FS 的元数据本就以 inode 为核心（见 u4-l2），所以选用 **low-level API**（`fuse_lowlevel_ops`），避免路径名与 inode 之间反复转换。本讲后续提到的回调都属 low-level。

最后回顾 u7-l1 的结论：客户端进程内部持有 `MgmtdClientForClient`、`MetaClient`、`StorageClient` 三类子客户端；本讲要回答的问题是——**这三类客户端被谁装进同一个进程、VFS 请求又是怎么被路由到它们头上的**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/fuse/hf3fs_fuse.cpp](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/hf3fs_fuse.cpp) | FUSE 守护进程入口 `main`：加载配置、起 IB、初始化 `FuseClients`、进入主循环。 |
| [src/fuse/FuseClients.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseClients.h) | `FuseClients` 聚合体的定义：持有三类客户端、inode 缓存、USRBIO 资源池等。 |
| [src/fuse/FuseClients.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseClients.cc) | `FuseClients::init` / `stop`：客户端的构造顺序与停止顺序。 |
| [src/fuse/FuseMainLoop.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseMainLoop.h) / [src/fuse/FuseMainLoop.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseMainLoop.cc) | `fuseMainLoop`：拼装挂载参数、创建会话、挂载、进入多线程请求循环。 |
| [src/fuse/FuseOps.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.h) / [src/fuse/FuseOps.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc) | 所有 low-level 回调的实现（`hf3fs_lookup` / `hf3fs_read` …）及回调表 `hf3fs_oper`。 |
| [docs/design_notes.md](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md) | 官方对 FUSE 性能瓶颈与「原生客户端」动机的说明，是本讲 4.3 节的依据。 |

## 4. 核心概念与源码讲解

### 4.1 FUSE 请求处理

#### 4.1.1 概念说明

「FUSE 请求处理」要解决的问题是：内核不断产生 VFS 请求，守护进程如何**稳定、无遗漏**地把每个请求接住、处理掉、再回包。

3FS 用 libfuse3 的 low-level API，核心是一个 `fuse_lowlevel_ops` 结构体——它是一张「VFS 操作 → C 函数指针」的回调表。内核每送来一个请求，libfuse 内部根据请求类型查这张表，调用对应的 `hf3fs_xxx` 函数。每个函数的签名都由 libfuse 规定，第一个参数恒为 `fuse_req_t req`（请求句柄），函数内部处理完后必须调用某个 `fuse_reply_*(req, ...)` 回包。

一个微妙但关键的点：这些回调函数本身是**同步 C 函数签名**，但 3FS 的客户端（`MetaClient` / `StorageClient`）全是用 folly 协程写的（见 u2-l3）。于是 `FuseOps.cc` 里反复出现一个桥接模式——用 `folly::coro::blockingWait` 把协程「拍扁」成同步调用，在回调线程里阻塞等待协程完成。这是 FUSE 同步世界与 3FS 协程世界的边界。

#### 4.1.2 核心流程

一个 FUSE 守护进程从启动到处理请求，可画成下面这条主线：

```
main()                                  # hf3fs_fuse.cpp
  ├─ 解析配置 (FuseConfig)
  ├─ net::IBManager::start()            # 起 InfiniBand，为 RDMA 客户端铺路
  ├─ FuseClients::init()                # 装配 mgmtd/storage/meta 三类客户端（见 4.2）
  └─ fuseMainLoop()                     # FuseMainLoop.cc
       ├─ 拼装 fuse 挂载参数 (allow_other / max_read / fsname …)
       ├─ fuse_session_new(args, &ops)  # 把回调表 ops 绑给会话
       ├─ fuse_set_signal_handlers()
       ├─ fuse_session_mount()          # 挂载到 mountpoint，开始接内核请求
       └─ fuse_session_loop_mt()        # 多线程循环：N 个工作线程从内核共享队列取请求
              │
              ▼  每来一个请求，libfuse 查 ops 表回调
       hf3fs_xxx(req, ...)
         ├─ 解析 fuse_req_ctx(req) 拿到 uid/gid/pid
         ├─ 构造 UserInfo（带 fuseToken）
         ├─ withRequestInfo(req, <协程>)   # blockingWait 桥接
         │     └─ 调 MetaClient / StorageClient
         └─ fuse_reply_*(req, ...)         # 必须回包
```

几个关键术语先记下来，后面源码精读会对上：

- `fuse_session`：一次 FUSE 挂载的会话对象，持有回调表，是接请求的主体。
- `fuse_session_loop_mt`：多线程主循环；线程数受配置 `max_threads` 约束。
- `fuse_req_ctx(req)`：从请求句柄取出内核送来的调用上下文（uid/gid/pid）。
- `withRequestInfo`：把 `fuse_req_t` 包进 folly 请求上下文、并 `blockingWait` 协程的桥接器。

#### 4.1.3 源码精读

**入口 `main`。** 注意文件顶部有编译开关：定义了 `ENABLE_FUSE_APPLICATION` 时走另一套（`FuseApplication`，纳入统一的两阶段启动骨架，见 u2-l1）；默认情况下走 `#else` 分支，即本讲分析的「独立 FUSE 守护进程」。`main` 非常薄，只做四件事——配配置、起 IB、初始化客户端、进主循环：

[src/fuse/hf3fs_fuse.cpp:39-81](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/hf3fs_fuse.cpp#L39-L81) —— 入口 `main`，依次起 IB、构造 `AppInfo`、调用 `FuseClients::init`、最后进入 `fuseMainLoop`。其中 `SCOPE_EXIT { d.stop(); }` 保证异常退出时也能优雅停止客户端。

关键几句：

```cpp
auto ibResult = net::IBManager::start(hf3fsConfig.ib_devices());   // 起 RDMA
auto &d = getFuseClientsInstance();
if (auto res = d.init(appInfo, hf3fsConfig.mountpoint(),
                      hf3fsConfig.token_file(), hf3fsConfig); !res) { ... }
return fuseMainLoop(argv[0], hf3fsConfig.allow_other(),
                    hf3fsConfig.mountpoint(),
                    hf3fsConfig.io_bufs().max_buf_size(),
                    hf3fsConfig.cluster_id());
```

`getFuseClientsInstance()` 返回的是一个全局单例（实现在 FuseOps.cc，见下文），整个守护进程共享这一个 `FuseClients` 对象。

**主循环 `fuseMainLoop`。** 它把 3FS 的回调表 `ops` 交给 libfuse，挂载后进入多线程循环：

[src/fuse/FuseMainLoop.cc:78-103](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseMainLoop.cc#L78-L103) —— 创建会话、设信号处理、挂载，最后用 `fuse_session_loop_mt` 跑多线程循环。要点：

```cpp
d.se = fuse_session_new(&args, &ops, sizeof(ops), NULL);   // 会话绑定回调表
fuse_set_signal_handlers(d.se);
fuse_session_mount(d.se, opts.mountpoint);                  // 真正挂载
...
fuse_loop_cfg_set_idle_threads(config, d.maxIdleThreads);
fuse_loop_cfg_set_max_threads(config, d.maxThreads);
ret = fuse_session_loop_mt(d.se, config);                   // 多线程取请求
```

注意 `d.maxThreads` 来自配置（默认 256，实际会被逻辑核数的一半封顶，见 4.2.3）。`d.se` 这个 `fuse_session *` 被存进 `FuseClients`，因为后续回包之外的「主动失效通知」（如 `fuse_lowlevel_notify_inval_inode`）也要用到它。

**回调表 `hf3fs_oper`。** 这是「请求分发」的字面真相——一张函数指针表，把每个 low-level 操作绑到 `FuseOps.cc` 里对应的 `hf3fs_xxx` 函数：

[src/fuse/FuseOps.cc:2580-2615](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc#L2580-L2615) —— 用 C99 指定初始化器（`.lookup = hf3fs_lookup`）逐项绑定，libfuse 据此把内核请求派发给对应函数；`getFuseOps()` 返回这张表的引用。

```cpp
const fuse_lowlevel_ops hf3fs_oper = {
    .init = hf3fs_init,  .destroy = hf3fs_destroy,
    .lookup = hf3fs_lookup,  .forget = hf3fs_forget,
    .getattr = hf3fs_getattr, .setattr = hf3fs_setattr,
    .open = hf3fs_open,  .read = hf3fs_read,  .write = hf3fs_write,
    .release = hf3fs_release,  .fsync = hf3fs_fsync,
    .create = hf3fs_create,  .readdirplus = hf3fs_readdirplus,
    .ioctl = hf3fs_ioctl,   /* … 目录/链接/xattr … */
};
const fuse_lowlevel_ops &getFuseOps() { return hf3fs_oper; }
```

注释掉的 `.readdir` 说明 3FS 只实现了 `readdirplus`（带 inode 信息的列目录），更省一次 `lookup` 往返。

**会话初始化 `hf3fs_init`。** 这是挂载后内核回调的第一个函数，用来协商能力与上限：

[src/fuse/FuseOps.cc:321-348](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc#L321-L348) —— 协商 writeback cache、splice 读写等能力，并把 `max_read` / `max_write` / `max_readahead` 设成 `io_bufs().max_buf_size()`（默认 1MB），即**单次 FUSE IO 的最大字节数**。这个上限正是 4.3 节性能瓶颈之一。

**协程桥接 `withRequestInfo`。** 每个 `hf3fs_xxx` 里反复出现的套路，是把协程拍扁成同步调用的关键：

[src/fuse/FuseOps.cc:85-89](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc#L85-L89) —— `withRequestInfo` 先用 `RequestInfo::set(req)` 把 `fuse_req_t` 注入 folly 请求上下文（用于取消、日志、埋点），再 `blockingWait` 等待协程完成。

```cpp
auto withRequestInfo(fuse_req_t req, Awaitable &&awaitable) {
  auto guard = RequestInfo::set(req);
  return folly::coro::blockingWait(std::forward<Awaitable>(awaitable));
}
```

其中 `RequestInfo::canceled()`（[src/fuse/FuseOps.cc:74-79](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc#L74-L79)）会调用 `fuse_req_interrupted(req)`，把内核的请求中断信号透传给协程，使长耗时操作可被取消。

**读请求处理 `hf3fs_read`。** 这是「请求处理」最典型的例子，也是综合实践要追踪的主线。函数先把尚未刷盘的写缓冲冲掉（保证读到最新数据），再从 RDMA buffer 池借一块缓冲，构造 `PioV` 执行器发起读，最后 `fuse_reply_buf` 回包：

[src/fuse/FuseOps.cc:1473-1550](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc#L1473-L1550) —— `hf3fs_read` 全流程。核心几步：

```cpp
auto memh = IOBuffer(folly::coro::blocking_wait(d.bufPool->allocate())); // 借 RDMA buffer
std::vector<ssize_t> res(1);
PioV ioExec(*d.storageClient, config.chunk_size_limit(), res);
ioExec.addRead(0, inode, 0, off, size, memh.data(), memh);               // 拆 IO
auto retExec = withRequestInfo(req,
    ioExec.executeRead(userInfo, d.config->storage_io().read()));        // 阻塞驱动 StorageClient
ioExec.finishIo(true);
...
fuse_reply_buf(req, (char *)memh.data(), res[0]);                        // 回包：把数据拷回内核
```

注意：FUSE 读路径里**数据要拷贝**——`storage` 用 RDMA Write 把数据填进 `memh`（守护进程的内存），`fuse_reply_buf` 再把这块内存**拷贝**进内核，内核最终拷给应用。这两次拷贝正是 4.3 节「内存拷贝开销」的来源。`PioV` 是 3FS 自研的批量 IO 执行器（USRBIO 也复用它，见 u7-l3），它内部驱动 `StorageClient::batchRead`，但那已是 u7-l1 的内容，本讲只把它当成「读的黑盒」。

#### 4.1.4 代码实践

**实践目标：** 在不运行集群的前提下，纯靠源码追踪「内核 `read()` → StorageClient」的调用链，列出沿途每一步。

**操作步骤：**

1. 打开 [src/fuse/FuseOps.cc:1473](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc#L1473) 的 `hf3fs_read`。
2. 找到 `inodeOf(*fi, ino)`（L1479），理解它如何从 `fi->fh`（即 `FileHandle`，见 4.2.3）拿到 `RcInode`。
3. 找到 `d.bufPool->allocate()`（L1511），这是借 RDMA buffer 的入口，回看 `FuseClients::init` 里 `bufPool` 的创建（[src/fuse/FuseClients.cc:86](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseClients.cc#L86)）。
4. 跟进 `ioExec.executeRead(...)`——它在 `PioV` 内部会调用 `d.storageClient` 的批量读。此处只需确认它最终落到 u7-l1 讲过的 `StorageClient::batchRead`。
5. 找到最后的 `fuse_reply_buf`（L1548），确认数据是如何回包的。

**需要观察的现象 / 预期结果：** 你应当能在一张图里画出：`应用 read()` → `/dev/fuse` → libfuse 工作线程 → `hf3fs_read` → `PioV` → `StorageClient` → storage 服务，再原路把数据 `fuse_reply_buf` 回去。**结论待本地验证**：若能在测试集群挂载后用 `strace -e read` 观察应用、并用 `perf top` 观察 `hf3fs_fuse` 进程，会看到大量时间花在内核的 FUSE 队列与拷贝上。

#### 4.1.5 小练习与答案

**练习 1：** 为什么每个 `hf3fs_xxx` 回调里几乎都出现 `withRequestInfo(req, ...)`，而不是直接 `co_await`？

**参考答案：** 因为 libfuse 的回调是**同步 C 函数签名**，没有协程上下文；而 3FS 客户端全是协程。`withRequestInfo` 用 `blockingWait` 把协程拍扁成同步调用，使回调线程阻塞等待结果，拿到结果后再 `fuse_reply_*` 回包。同时它还把 `fuse_req_t` 注入 folly 请求上下文，用于日志埋点与中断传播。

**练习 2：** 回调表 `hf3fs_oper`（[FuseOps.cc:2580](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc#L2580)）里 `.readdir` 被注释掉了，只保留 `.readdirplus`，这样做的收益是什么？

**参考答案：** `readdirplus` 在列目录的同时一并返回每个子项的 inode 与属性，省去了对每个子项再发一次 `lookup` 的往返；3FS 元数据本就以 inode 为单位（u4-l2），用 `readdirplus` 能显著降低列目录场景下的元数据 RPC 次数。

### 4.2 客户端聚合（FuseClients）

#### 4.2.1 概念说明

`FuseClients` 是 3FS FUSE 守护进程的「**中央聚合体**」。一个 FUSE 进程要同时和 mgmtd（拿路由与配置）、meta（做文件元数据操作）、storage（读写数据）三类服务打交道，还要维护 inode 缓存、USRBIO 资源池、周期同步任务等。把这些全部塞进一个结构体，既是为了集中管理生命周期，也是因为各回调函数需要一个全局可访问的对象（回忆 `getFuseClientsInstance()` 返回的就是它）。

理解 `FuseClients` 的关键是「**装配顺序**」和「**共享状态**」：

- **装配顺序**：三类客户端之间存在依赖——`storageClient` 和 `metaClient` 都需要 `mgmtdClient` 提供路由信息，`metaClient` 还需要 `storageClient`（写回数据）。所以初始化必须 `mgmtd → storage → meta`，停止顺序则相反。
- **共享状态**：内核用 inode 号引用文件，但 3FS 内部用 `InodeId`；为了不每次 `read`/`write` 都去 meta 查 inode，`FuseClients` 维护了一张 inode 缓存（`inodes`），并用引用计数管理生命周期，对应内核的 `lookup`/`forget`。

#### 4.2.2 核心流程

`FuseClients::init` 的装配顺序与依赖关系：

```
FuseClients::init()
  ├─ net::Client                       # 底层网络客户端（TCP/RDMA），所有 stub 的载体
  ├─ mgmtdClient                       # 控制面客户端
  │    ├─ refreshRoutingInfo()         # 拉一次集群路由
  │    └─ establishClientSession()     # 续客户端会话（带重试）
  ├─ storageClient = create(... mgmtdClient)   # 依赖 mgmtd 路由
  └─ metaClient = MetaClient(... mgmtdClient, storageClient)  # 依赖 mgmtd + storage
```

而 inode 缓存的运转则由内核的 `lookup`/`forget` 驱动：

```
内核 lookup  →  FuseOps::add_entry()   →  inodes[id] 引用计数 +1（首次则插入）
内核 forget  →  FuseOps::remove_entry() → 引用计数 - n，归零则从 inodes 移除
```

#### 4.2.3 源码精读

**聚合体本体。** `FuseClients` 把三类客户端、inode 缓存、USRBIO 资源、会话指针都收拢在一起：

[src/fuse/FuseClients.h:195-198](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseClients.h#L195-L198) —— 四个核心成员：`net::Client`（底层网络）、`mgmtdClient`、`storageClient`、`metaClient`。

```cpp
std::unique_ptr<net::Client> client;
std::shared_ptr<client::MgmtdClientForClient> mgmtdClient;
std::shared_ptr<storage::client::StorageClient> storageClient;
std::shared_ptr<meta::client::MetaClient> metaClient;
```

此外还有 inode 缓存与并发保护 [src/fuse/FuseClients.h:211-213](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseClients.h#L211-L213)（`inodes` map + `inodesMutex`）、USRBIO 资源 [src/fuse/FuseClients.h:227-228](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseClients.h#L227-L228)（`iovs` / `iors`）、以及会话指针 `fuse_session *se`（[src/fuse/FuseClients.h:223](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseClients.h#L223)），供回包与失效通知使用。

**装配顺序。** `init` 严格按依赖关系构造，下面只摘关键行：

[src/fuse/FuseClients.cc:92-137](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseClients.cc#L92-L137) —— `net::Client` → `mgmtdClient` → 刷路由 + 建会话 → `storageClient` → `metaClient`。

```cpp
client = std::make_unique<net::Client>(fuseConfig.client());      // 1. 底层网络
...
mgmtdClient = std::make_shared<client::MgmtdClientForClient>(...); // 2. 控制面
folly::coro::blockingWait(mgmtdClient->start(...));
folly::coro::blockingWait(mgmtdClient->refreshRoutingInfo(false));
RETURN_ON_ERROR(establishClientSession(*mgmtdClient));             //    拉路由 + 续会话
storageClient = storage::client::StorageClient::create(
    clientId, fuseConfig.storage(), *mgmtdClient);                 // 3. 数据面，依赖 mgmtd
metaClient = std::make_shared<meta::client::MetaClient>(
    clientId, fuseConfig.meta(), ..., mgmtdClient, storageClient, true);  // 4. 元数据，依赖前两者
```

注意 `metaClient` 构造时同时传入 `mgmtdClient` 和 `storageClient`（与 u7-l1 讲的「meta 内部以 shared_ptr 同时持有 mgmtd 与 storage」完全对应）。`stop()`（[src/fuse/FuseClients.cc:178-216](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseClients.cc#L178-L216)）则严格反向销毁：`meta → storage → mgmtd → client`，体现「先停止上层使用者、再停底层依赖」的资源释放原则。

**inode 缓存与引用计数。** 内核 inode 号与 3FS `InodeId` 之间有固定映射（根目录与 GC 根除外）：

[src/fuse/FuseOps.cc:175-193](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc#L175-L193) —— `real_ino` / `linux_ino` 做 `fuse_ino_t ↔ InodeId` 转换，其中 `FUSE_ROOT_ID` 映射到 `InodeId::root()`，`FUSE_ROOT_ID+1` 映射到 GC 根目录（u4-l5）。

每个缓存项是 `RcInode`，带引用计数与「动态属性」（本地记录的写入进度、hint 长度等）：

[src/fuse/FuseClients.h:67-144](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseClients.h#L67-L144) —— `RcInode`：`refcount` 对齐内核 `lookup`/`forget`，`DynamicAttr` 记录本地写位置（`written`/`synced`/`fsynced`）、`truncateVer`、`hintLength` 等，是 FUSE 侧维护文件长度最终一致性的簿记（对应 u4-l5 的长度更新机制）。

`add_entry` / `remove_entry` 就是这张缓存的增删（[src/fuse/FuseOps.cc:231-282](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc#L231-L282)）：`lookup` 时 `refcount++`，`forget`（[FuseOps.cc:401-409](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc#L401-L409)）时按 `nlookup` 减少，归零才真正移除。

**文件句柄。** `open` 时创建 `FileHandle` 存进 `fi->fh`，后续 `read`/`write`/`release` 直接取用：

[src/fuse/FuseClients.h:146-154](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseClients.h#L146-L154) —— `FileHandle` 持有 `rcinode`、是否 `O_DIRECT`、以及写打开时分配的 `sessionId`（对应 u4-l5 的 FileSession）。`hf3fs_open`（[FuseOps.cc:1418-1471](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc#L1418-L1471)）对写打开会调 `metaClient->open(...)` 建会话，并在 `release`（[FuseOps.cc:1737-1774](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc#L1737-L1774)）时调 `close` 销毁。

#### 4.2.4 代码实践

**实践目标：** 验证「装配顺序」与「停止顺序」严格相反，并解释为什么不能打乱。

**操作步骤：**

1. 读 `FuseClients::init`（[src/fuse/FuseClients.cc:50-176](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseClients.cc#L50-L176)），列出 `client / mgmtdClient / storageClient / metaClient` 四者的构造先后。
2. 读 `FuseClients::stop`（[src/fuse/FuseClients.cc:178-216](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseClients.cc#L178-L216)），列出它们的 `reset()` 先后。
3. 对比两份顺序，确认是严格逆序。

**需要观察的现象 / 预期结果：** init 顺序是 `client → mgmtd → storage → meta`，stop 顺序是 `meta → storage → mgmtd → client`。**预期结论**：因为 `metaClient` 内部持有 `storageClient` 与 `mgmtdClient` 的 `shared_ptr`，必须先停 `metaClient` 释放这些引用，否则后两者无法真正析构；同理 `storageClient` 依赖 `mgmtdClient` 路由，须先停 storage。这是 C++ 资源所有权的标准约束。

#### 4.2.5 小练习与答案

**练习 1：** `FuseClients` 里 `inodes` 这张 map 为什么用引用计数（`refcount`）而不是直接缓存固定数量？

**参考答案：** 因为内核 FUSE 协议本身用 `lookup`/`forget` 配对管理 inode 生命周期：`lookup` 一次就给引用计数 +1，`forget` 时内核一次性归还 `nlookup` 次。引用计数让缓存项的生命周期与内核的真实需求精确对齐——只要内核还可能再用这个 inode，就保留；内核 `forget` 归零后才移除，既避免频繁查 meta，又不会无限膨胀缓存。

**练习 2：** 为什么 `metaClient` 的构造参数里同时要传 `mgmtdClient` 和 `storageClient`？

**参考答案：** meta 操作（如 `close`/`fsync`）需要向 storage 查询真实文件长度做对账（u4-l5 的 `updateLength=true` 路径），同时所有客户端都需要 mgmtd 提供的路由信息才能找到 meta/storage 实例。因此 `MetaClient` 内部以 `shared_ptr` 同时持有这两者（u7-l1），构造时必须把它们传进来。

### 4.3 性能瓶颈与原生客户端的动机

#### 4.3.1 概念说明

FUSE 给了 3FS 一个「低接入门槛」的 POSIX 接口，但它在高性能场景下有三个固有限制。理解这三个限制，才能理解 3FS 为什么要在 FUSE 守护进程**内部**再实现一套「原生客户端」（USRBIO，详见 u7-l3）。

1. **内存拷贝开销**：守护进程无法直接访问应用的内存。一次 `read` 要把数据从 storage 经 RDMA 填进守护进程内存，再由 `fuse_reply_buf` 拷进内核，内核再拷给应用——数据至少多搬了一两趟，消耗内存带宽、抬高延迟。
2. **原始的多线程支持（共享队列自旋锁）**：内核把请求放进一个**被自旋锁保护的多线程共享队列**，守护进程的工作线程从这里取请求。锁争用使得 FUSE 的处理能力**不能随线程数线性扩展**——官方基准显示 FUSE 大约只能处理 40 万次/秒的 4KiB 读，继续加并发只会让锁争用加剧，`perf` 显示大量 CPU 耗在内核态自旋锁上。
3. **单次 IO 大小受限**：low-level 协商出来的 `max_read`/`max_write`（3FS 设成 1MB）封顶了单次请求的字节数，这对网络文件系统极不友好——一次大读要被拆成多次 FUSE 往返。

#### 4.3.2 核心流程

3FS 的取舍可以用一句话概括：**不把客户端做成内核模块（开发/升级/排障代价太高），而是在 FUSE 守护进程里内嵌一个绕过 VFS 的原生客户端**。

```
普通应用 ──read()──▶ 内核 VFS ──▶ /dev/fuse 共享队列(自旋锁) ──▶ FUSE 守护进程 ──▶ storage
                       ▲ 拷贝                                       ▲ 拷贝          (受 max_read 限制)
                       │                                            │
性能敏感应用 ──USRBIO API──▶ 直接投递到守护进程的 Ior 环(无内核队列、零拷贝 Iov) ──▶ storage
```

下半部分就是 u7-l3 要讲的 USRBIO：应用通过 `Iov`（共享内存，零拷贝）+ `Ior`（类 io_uring 环形通信）把请求**直接**交给守护进程内的原生客户端，既绕开了内核共享队列的自旋锁，又消除了内核↔用户态的拷贝，还突破了 `max_read` 的单次大小限制。本讲只交代「为什么需要它」，实现细节留给 u7-l3。

#### 4.3.3 源码精读

**官方对瓶颈的表述。** 这是最权威、最直接的依据，本节几乎逐句对应：

[src/lib/api/UsrbIo.md:4](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/UsrbIo.md#L4) —— USRBIO 是「直接把 IO 请求提交到 FUSE 进程内的 3FS IO 队列，从而绕过 FUSE 自身的限制，例如对网络文件系统极不友好的单次 IO 大小上限」。

[docs/design_notes.md:29-31](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L29-L31) —— 两条核心限制，直接引用：

> - *Memory copy overhead* 守护进程无法访问应用内存，内核与用户态之间的数据搬运消耗内存带宽、抬高端到端延迟。
> - *Primitive multi-threading support* FUSE 把请求放进一个**自旋锁保护的多线程共享队列**……由于锁争用，FUSE 的 IO 处理能力无法随线程数扩展。基准结果：FUSE 大约只能处理 **400K 4KiB reads/s**，再加并发也不改善，`perf` 显示内核态自旋锁消耗了大量 CPU。

[docs/design_notes.md:37-41](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L37-L41) —— 为什么不做成内核模块、而是选「在 FUSE 守护进程内实现原生客户端」：内核模块开发难、bug 难诊断、升级需停所有进程甚至重启机器；故选用户态原生客户端，元数据操作仍走 FUSE（保证 POSIX 一致性），数据 IO 走异步零拷贝 API。

**瓶颈在代码里的体现。** 单次 IO 上限就在 `hf3fs_init` 协商的能力里：

[src/fuse/FuseOps.cc:342-345](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc#L342-L345) —— `max_read` / `max_write` / `max_readahead` 都被设成 `io_bufs().max_buf_size()`（默认 1MB，见 [FuseConfig.h:74](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseConfig.h#L74)）。任何超过 1MB 的单次 `read` 都会被内核拆成多段。

```cpp
conn->max_readahead = d.config->io_bufs().max_buf_size();
conn->max_read  = d.config->io_bufs().max_buf_size();
conn->max_write = d.config->io_bufs().max_buf_size();   // 单次 FUSE IO 上限 = 1MB
```

**缓解旋钮（仅缓解，无法根治）。** `hf3fs_init` 还会尽量开启 libfuse 的几项优化：writeback cache（让内核代缓冲写）、splice 读/写/搬移（用 `splice` 系统调用在内核与用户态间零拷贝搬数据，但仍限于 FUSE 框架内）：

[src/fuse/FuseOps.cc:325-340](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc#L325-L340) —— 在内核能力允许时打开 `FUSE_CAP_WRITEBACK_CACHE` 与三项 `SPLICE` 能力。

而工作线程数受配置约束（这是 FUSE 侧唯一的并发旋钮，但受共享队列锁制约，加大也收益有限）：

[src/fuse/FuseConfig.h:34-35](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseConfig.h#L34-L35) —— `max_idle_threads`(-1) / `max_threads`(256)，最终 `FuseClients::init` 会用「逻辑核数的一半」对其封顶（[FuseClients.cc:80-85](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseClients.cc#L80-L85)），并传给 `fuse_loop_cfg_set_max_threads`。

#### 4.3.4 代码实践

**实践目标：** 用源码与文档说清「共享队列自旋锁为何限制吞吐」，并能指出代码里有哪些旋钮只能缓解、无法根治。

**操作步骤：**

1. 精读 [docs/design_notes.md:31](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L31)，记下两个量化结论：≈400K 4KiB reads/s、`perf` 显示内核态自旋锁占比高。
2. 回到 [src/fuse/FuseMainLoop.cc:96-103](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseMainLoop.cc#L96-L103)，确认 `fuse_session_loop_mt` 用的是 libfuse 自带的多线程循环——也就是说，**守护进程对「内核侧共享队列」无能为力**，能调的只有工作线程数。
3. 检查 `max_threads` 旋钮（[FuseConfig.h:35](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseConfig.h#L35)）与封顶逻辑（[FuseClients.cc:80-85](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseClients.cc#L80-L85)），理解为何加大它收益有限。
4. 对比 [src/lib/api/UsrbIo.md:4](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/UsrbIo.md#L4)：USRBIO「直接提交到 3FS 的 IO 队列」——这个队列是 `Ior`（u7-l3），**不是**内核共享队列。

**需要观察的现象 / 预期结果：** 你应当能得出结论——FUSE 吞吐瓶颈的根因在内核侧的自旋锁保护队列，守护进程只能靠 splice/writeback/线程数去「缓解」拷贝与并发问题，无法根治；唯一的根治办法是让数据 IO 绕过 VFS，即 USRBIO。**结论待本地验证**：在挂载的集群上对同一文件分别用 FUSE `read` 与 USRBIO 做小随机读压测，对比 IOPS 与 `perf` 中自旋锁占比。

#### 4.3.5 小练习与答案

**练习 1：** 假设把 `max_threads` 从 256 调到 1024，FUSE 的 4KiB 随机读 IOPS 会成比例提升吗？为什么？

**参考答案：** 不会成比例提升。瓶颈在内核侧那个被自旋锁保护的共享请求队列——工作线程越多，取请求时的锁争用越激烈，`perf` 会显示大量 CPU 耗在内核态自旋上。官方基准已表明 FUSE 在 ≈400K 4KiB reads/s 附近封顶，再加并发只会加剧锁争用。这正是要引入 USRBIO（每环一个独立队列、无全局自旋锁）的原因。

**练习 2：** 既然内核模块（VFS module）能彻底避开这些瓶颈，3FS 为什么不这么做？

**参考答案：** 出于工程稳健性（见 [design_notes.md:39](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L39)）：内核模块开发难度大、bug 难诊断甚至可能直接宕机且不留日志；升级时必须停掉所有使用该文件系统的进程，否则要重启机器。3FS 选择折中——元数据操作仍走 FUSE（保 POSIX 一致性、易迁移），仅把性能敏感的数据 IO 用用户态原生客户端（USRBIO）加速。

## 5. 综合实践

**任务：** 完整追踪一次 FUSE `read` 请求「从应用到 StorageClient」的全路径，并用一句话点出该路径上限制吞吐的瓶颈点。

**操作步骤：**

1. **应用层**：某进程对挂载点下的文件调用 `read(fd, buf, n)`。
2. **内核层**：VFS 发现是 FUSE 挂载，把请求放入 `/dev/fuse` 的共享队列（被自旋锁保护）。
3. **libfuse 层**：`fuse_session_loop_mt`（[FuseMainLoop.cc:102](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseMainLoop.cc#L102)）的某个工作线程取到请求，按 `hf3fs_oper` 表（[FuseOps.cc:2580](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc#L2580)）回调 `hf3fs_read`。
4. **FuseOps 层**：`hf3fs_read`（[FuseOps.cc:1473](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc#L1473)）经 `inodeOf` 取 `RcInode`，借 RDMA buffer，构造 `PioV` 并 `withRequestInfo` 阻塞驱动 `StorageClient::batchRead`（u7-l1）。
5. **回包层**：storage 用 RDMA Write 把数据填进守护进程的 `memh`，`fuse_reply_buf`（[FuseOps.cc:1548](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc#L1548)）把它拷进内核，内核再拷给应用。

**需要观察的现象 / 预期结果：**

- 画出上述五层调用图，标注每层涉及的源码位置。
- 指出三个瓶颈点：① 步骤 2 的**内核共享队列自旋锁**（限制 IOPS 扩展，≈400K 4KiB reads/s 封顶）；② 步骤 5 的**两次内存拷贝**（守护进程↔内核↔应用，吃带宽、抬延迟）；③ `max_read=1MB` 的**单次 IO 上限**（[FuseOps.cc:344](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc#L344)）。
- 用一句话回答综合实践的第二个问题：**FUSE 共享队列自旋锁之所以限制吞吐，是因为所有工作线程都要争抢同一把内核态锁来取请求，锁争用随线程数上升而急剧加剧，使处理能力无法随并发线性扩展，CPU 大量耗在内核态自旋而非真正干活；这正是 USRBIO 用每应用一个独立 `Ior` 环来替代它的根本动机。**

**预期运行结果：待本地验证**（需可用的 6 节点测试集群与挂载点）。在本地验证时，可用 `fio` 做 4KiB 随机读压测，同时 `perf top -p <hf3fs_fuse pid>` 观察内核态自旋锁符号（如 `fuse_*` / `__raw_spin_lock*`）的占比，定性印证瓶颈。

## 6. 本讲小结

- FUSE 守护进程 `hf3fs_fuse` 的入口很薄：`main`（[hf3fs_fuse.cpp:39](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/hf3fs_fuse.cpp#L39)）只做「配配置 → 起 IB → `FuseClients::init` → `fuseMainLoop`」四件事。
- 请求分发的真相是回调表 `hf3fs_oper`（[FuseOps.cc:2580](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc#L2580)）：libfuse 据此把内核请求派发给 `hf3fs_xxx`；每个回调用 `withRequestInfo` + `blockingWait` 把协程拍扁成同步调用，再 `fuse_reply_*` 回包。
- `FuseClients`（[FuseClients.h:179](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseClients.h#L179)）是中央聚合体，按 `client → mgmtd → storage → meta` 装配、逆序停止，并维护 inode 缓存（`RcInode` 引用计数，对齐内核 `lookup`/`forget`）。
- 读路径 `hf3fs_read`（[FuseOps.cc:1473](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc#L1473)）借 RDMA buffer、用 `PioV` 驱动 `StorageClient`，最后 `fuse_reply_buf` 回包——数据在此处发生拷贝。
- FUSE 三大瓶颈：内存拷贝、共享队列自旋锁（≈400K 4KiB reads/s 封顶，见 [design_notes.md:31](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L31)）、`max_read`=1MB 单次上限（[FuseOps.cc:344](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc#L344)）。
- 3FS 不做成内核模块（升级/排障代价高），而是在 FUSE 守护进程内嵌「原生客户端」USRBIO，让数据 IO 绕开 VFS——这是 u7-l3 的主题。

## 7. 下一步学习建议

- **必读下一篇 u7-l3（USRBIO 零拷贝 API）**：本讲反复提到的 `Iov` / `Ior` / `io_depth` / `PioV` 都在那里展开，它是 FUSE 瓶颈的直接解药。建议先复习本讲 4.3 节再进入。
- **回看 u7-l1**：本讲把 `StorageClient::batchRead` 当成黑盒，读完 u7-l3 后可回头补全「`PioV` → `StorageClient`」这一段的真实批量与选 target 逻辑。
- **延伸阅读**：对照 [src/lib/api/UsrbIo.md](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/lib/api/UsrbIo.md) 与 [docs/design_notes.md](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md) 的「Asynchronous zero-copy API」一节，把「为什么用 io_uring 风格的环形队列」这一设计动机彻底吃透。
- **可选**：若对内核侧 FUSE 实现好奇，可阅读 design_notes.md 脚注 [^1] 指向的 Linux 源码 `fs/fuse/file.c`，理解「Linux 5.x 不支持对同一文件的并发写」这条限制的内核根因。
