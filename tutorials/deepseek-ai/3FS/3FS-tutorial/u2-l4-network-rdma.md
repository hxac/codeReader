# 网络层：TCP 与 RDMA 传输

> 本讲属于「公共基础设施」单元（u2），承接 u2-l2（RPC 与 serde）。u2-l2 讲清楚了「一个 RPC 调用如何被打包成 `MessagePacket`、由 `ClientContext::sendAsync` 送出去」，但没有回答：这些字节究竟是怎么穿过网卡到对端的？本讲就补上这一段——`src/common/net/` 下的传输基础设施。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `net::Server` → `ServiceGroup` → `IOWorker` / `Processor` / `Listener` 的分层关系，以及为什么要把「接收连接」「读写 I/O」「派发处理」拆成三类组件。
- 看懂 `EventLoop` 如何用 `epoll` + `eventfd` 把多个连接的事件汇聚到一个线程里处理。
- 理解 `Transport` 是如何用同一套读写状态机同时驱动 `TcpSocket` 与 `IBSocket` 的（TCP / RDMA 的「统一抽象」）。
- 掌握 `IBSocket` 基于 InfiniBand 的零拷贝收发原理：QP / CQ / MR / WR / WC、`SEND/RECV` 走缓冲、`RDMA Read/Write` 绕过对端 CPU。
- 掌握 `RDMABuf` / `RDMABufPool` 的内存注册（`ibv_reg_mr`）与回收机制，以及 `RDMARemoteBuf`（`addr` + `rkey`）如何让本端直接读写对端内存。

## 2. 前置知识

在进入源码前，先用大白话建立几组直觉。

### 2.1 epoll 与 eventfd

Linux 的高并发 I/O 模型。`epoll_wait` 会阻塞，直到一批「感兴趣的 fd」中至少一个变得可读或可写，然后返回这些事件。`eventfd` 是一个极轻量的 fd，往里写一个整数就能让等待它的 `epoll_wait` 立刻醒来——3FS 用它实现「外部线程唤醒事件循环」。

### 2.2 InfiniBand / RDMA 的几个名词

RDMA（Remote Direct Memory Access）允许一台机器**直接读写另一台机器内存**，过程中对端 CPU 与操作系统内核完全不参与，因而延迟低、吞吐高。3FS 的存储热路径几乎全走 RDMA。需要记住几个 verbs 概念：

- **QP（Queue Pair）**：每个连接有一对队列——发送队列（SQ）与接收队列（RQ）。你往 SQ 投递「工作请求 WR」，硬件就替你把数据搬走。
- **CQ（Completion Queue）**：WR 完成后会产出「完成项 WC」放进 CQ。你轮询（`ibv_poll_cq`）CQ 就知道哪批数据搬完了。CQ 可以挂一个 `comp_channel`（一个 fd），它变得可读 = 「有完成事件」，这样就能被 `epoll` 监听。
- **MR（Memory Region）**：只有「注册过」的内存（`ibv_reg_mr`）才能被网卡直接访问，注册时会拿到一把本端钥匙 `lkey` 和一把远端钥匙 `rkey`。
- **两类操作**：
  - `SEND/RECV`：对端必须先 `RECV`，本端才能 `SEND`，数据落到对端预置的接收缓冲里（**有拷贝、需要对端 CPU 配合**）。
  - `RDMA Read/Write`：本端拿着对端的 `(addr, rkey)`，直接读写对端内存，**对端毫不知情**（这是零拷贝高吞吐的关键）。

> 一个关键事实：RDMA 的「建链」本身并不是 RDMA 操作——两端要先通过一条普通 TCP 连接交换 QP 编号、LID/GID、MTU 等信息（verbs 称为 connection manager 握手），然后才能修改 QP 状态进入收发。3FS 也是这么做的。

### 2.3 为什么需要「统一抽象」

RDMA 性能高但编程复杂；TCP 简单通用但慢。3FS 希望上层 RPC（serde）代码**只写一遍**，底下 TCP 与 RDMA 可换。这决定了 `net/` 的设计核心：定义一个抽象基类 `Socket`，`TcpSocket` 与 `IBSocket` 各自实现，再由同一个 `Transport` 用状态机驱动它们。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/common/net/Server.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Server.h) | 多服务组容器，是 `net::Server` 顶层对象。 |
| [src/common/net/Server.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Server.cc) | `setup/start/stopAndJoin` 生命周期实现。 |
| [src/common/net/ServiceGroup.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ServiceGroup.h) / [.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ServiceGroup.cc) | 一组「监听 + I/O + 处理」的组合单元。 |
| [src/common/net/EventLoop.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/EventLoop.h) / [.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/EventLoop.cc) | `epoll` 事件循环封装。 |
| [src/common/net/Socket.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Socket.h) | 抽象基类，TCP/RDMA 的统一接口。 |
| [src/common/net/Transport.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Transport.h) / [.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Transport.cc) | 包裹 `Socket`、用原子标志位驱动读写状态机。 |
| [src/common/net/IOWorker.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/IOWorker.h) / [.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/IOWorker.cc) | 管理所有 `Transport`、`EventLoop` 池与连接。 |
| [src/common/net/Processor.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Processor.h) | 把收到的消息反序列化、派发到协程处理。 |
| [src/common/net/Listener.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Listener.h) | 监听端口，接受 TCP/RDMA 连接。 |
| [src/common/net/ib/IBSocket.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.h) / [.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.cc) | RDMA socket 实现。 |
| [src/common/net/ib/RDMABuf.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/RDMABuf.h) / [.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/RDMABuf.cc) | RDMA 内存缓冲与池化。 |
| [src/common/net/ib/IBConnect.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBConnect.h) | 建链握手交换的 QP/LID/GID 等信息结构。 |
| [src/common/net/Client.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Client.h) | 客户端侧的对称结构（也含 IOWorker/Processor）。 |

---

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：①服务端分层模型 ②事件循环 ③统一传输层 `Transport` ④RDMA 收发 `IBSocket` ⑤RDMA 缓冲管理 `RDMABuf`。

### 4.1 服务端模型：Server / ServiceGroup / IOWorker / Processor / Listener 分层

#### 4.1.1 概念说明

一个 3FS 进程（如 meta、storage）会同时监听多组端口、跑多类 RPC 服务。`net::Server` 用「分组」来组织：每个 `ServiceGroup` 把 **四样东西** 捆绑成一个自洽的收发单元——

- `Listener`：负责「被动接受连接」；
- `IOWorker`：负责「在已建立的连接上做读写 I/O」；
- `Processor`：负责「把收到的字节解包成 RPC、派发到协程执行」；
- `serde::Services`：真正注册进来的业务服务（u2-l2 讲过）。

为什么要这样拆？因为这三件事的负载特征完全不同：建链是低频的、I/O 是高吞吐但 CPU 占用低的、业务处理是 CPU 密集的。把它们放进不同的线程池/执行器，能避免「慢业务把网卡收发也卡死」。

#### 4.1.2 核心流程

`Server` 的生命周期严格对称：

```text
构造：为每个配置项 groups[i] 创建一个 ServiceGroup（共用 tpg_ 或 independentTpg_）
  ↓
setup()  → 每个 group.setup()（Listener.setup，绑端口）
  ↓
start()  → beforeStart()
         → 每个 group.start()：
              processor.start → ioWorker.start（启动 EventLoop）→ checkConnectionsRegularly 后台协程 → listener.start（开始 accept）
         → afterStart()
  ↓
mainLoop 阻塞……
  ↓
stopAndJoin() → beforeStop → 每个 group.stopAndJoin → afterStop → 停 tpg
```

注意 `groups` 最多 4 组，由 `CONFIG_OBJ_ARRAY(groups, ServiceGroup::Config, 4)` 在配置层固定。

#### 4.1.3 源码精读

`Server` 顶层容器，配置里挂了线程池与最多 4 个服务组——[src/common/net/Server.h:L23-L27](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Server.h#L23-L27) 定义了 `thread_pool`、`independent_thread_pool`、`groups[4]`。这里的 `groups_` 是一个 `vector<unique_ptr<ServiceGroup>>`——[src/common/net/Server.h:L93-L95](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Server.h#L93-L95) 给出三个核心成员：主线程池 `tpg_`、独立线程池 `independentTpg_`、服务组列表 `groups_`。

构造时按配置逐个建组——[src/common/net/Server.cc:L11-L21](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Server.cc#L11-L21)：若该组 `use_independent_thread_pool=true` 则绑到 `independentTpg_`，否则共享 `tpg_`。这就是「关键业务用独立线程池隔离」的开关。

`start()` 的对称结构——[src/common/net/Server.cc:L35-L46](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Server.cc#L35-L46)：先记 `appInfo_`，调 `beforeStart`，逐组 `group->start()`，再调 `afterStart`。`beforeStart/afterStart/beforeStop/afterStop` 是 4 个虚函数钩子（定义在 [Server.h:L79-L88](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Server.h#L79-L88)），服务在自己的 `Server` 子类里覆写它们来「建客户端、注册 RPC」——这正是 u2-l1 讲过的两阶段骨架里 `net::Server` 接管后的那段。

再看 `ServiceGroup` 的内部组合——[src/common/net/ServiceGroup.h:L71-L81](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ServiceGroup.h#L71-L81) 把 `serdeServices_`、`processor_`、`ioWorker_`、`listener_` 聚到一起。它的 `Config` 默认网络类型是 RDMA——[src/common/net/ServiceGroup.h:L25](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ServiceGroup.h#L25) `CONFIG_ITEM(network_type, Address::RDMA)`，即「3FS 的服务默认就用 RDMA」。客户端对称地有一个 `force_use_tcp` 开关——[src/common/net/Client.h:L29](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Client.h#L29)，用来在排查时强制降级到 TCP。

构造时把四件套按依赖顺序串起来——[src/common/net/ServiceGroup.cc:L9-L18](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ServiceGroup.cc#L9-L18)：`processor_` 持有 `serdeServices_` 与 proc 线程池；`ioWorker_` 持有 `processor_` 与 io 线程池、conn 线程池；`listener_` 持有 `ioWorker_`。注意依赖方向是 `Listener → IOWorker → Processor → Services`，即外层依赖内层。

`ServiceGroup::start()`——[src/common/net/ServiceGroup.cc:L30-L38](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ServiceGroup.cc#L30-L38)：顺序是 `processor.start` → `ioWorker.start`（启动 EventLoop 线程）→ 起一个 `checkConnectionsRegularly` 后台协程（周期性清理过期连接）→ `listener.start`。停止顺序严格相反（[ServiceGroup.cc:L40-L46](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ServiceGroup.cc#L40-L46)）：先停 listener、再停 processor、最后停 ioWorker。

#### 4.1.4 代码实践

**目标**：用源码自带的 `describe()` 看清一个 `Server` 到底开了哪些组、监听了哪些地址。

**操作步骤**（源码阅读型实践）：

1. 打开 [src/common/net/Server.cc:L72-L86](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Server.cc#L72-L86)，阅读 `Server::describe()`：它会打印每个 group 的服务名列表（`serviceNameList()`）和监听地址列表（`addressList()`）。
2. 在仓库内搜索 `server_.describe()` 或 `net::Server` 的 `describe` 调用点，确认它在启动日志里被打印。
3. 对照 [ServiceGroup.h:L56-L59](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ServiceGroup.h#L56-L59)：`addressList()` 来自 `listener_`，`serviceNameList()` 来自配置 `services()`。

**需要观察的现象**：一个 storage 进程的启动日志里，应能看到多组 `Group 0/1/...`、每组下面挂了若干 `Service:`（如 `CoreService`、`StorageService`）和 `Listening: rdma://x.x.x.x:port`。

**预期结果**：能从日志反推出「这个进程开了几个 ServiceGroup、每组监听什么、用了 RDMA 还是 TCP」。

> 说明：本实践为源码阅读型，若手头没有运行中的 3FS 集群，可只完成「读 `describe()` 并画出组—服务—地址对照表」这一步，标注「待本地验证」日志部分。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ServiceGroup` 要把 `processor_`、`ioWorker_`、`listener_` 设计成「构造时互相引用」而不是全局单例？

> **答案**：因为一个进程可能有多个 `ServiceGroup`（最多 4 组），各自有独立的网络类型、线程池与服务集合。互相引用让 `Listener` 接到新连接后能直接交给本组的 `IOWorker`，`IOWorker` 收到消息后交给本组的 `Processor`，闭环在本组内，避免跨组竞争。

**练习 2**：`use_independent_thread_pool` 为 `true` 的服务组与为 `false` 的有什么区别？

> **答案**：前者绑到 `Server::independentTpg_`（专用线程池），后者共享 `Server::tpg_`（主线程池）。专用线程池用于把「关键/高优先级服务」与其他服务在 CPU 资源上隔离，避免互相拖累。

---

### 4.2 事件循环：EventLoop 与 epoll

#### 4.2.1 概念说明

`EventLoop` 是 3FS 网络层的「心脏」：一个线程跑一个 `epoll`，监听挂进来的所有 `EventHandler`（每个 `Transport` 都是一个 `EventHandler`）。当某个连接可读/可写时，`epoll_wait` 返回，`EventLoop` 就回调该 `EventHandler::handleEvents`。一个 `IOWorker` 持有一个 `EventLoopPool`（默认 1 个 `EventLoop`，即 1 个线程）。

#### 4.2.2 核心流程

```text
EventLoop::start():
  epoll_create → epfd
  eventfd      → eventfd_（用来被其他线程唤醒）
  把 eventfd_ 加入 epoll（EPOLLIN | EPOLLET，边缘触发）
  起一个 jthread 跑 loop()

loop():  while true:
  n = epoll_wait(epfd, events[64], -1)   # 阻塞等待
  for evt in events:
    if evt.data.ptr == nullptr:  # 是 eventfd_ 唤醒 → 读空它
    else: handler->handleEvents(evt.events)
  顺便处理 deleteQueue（延迟删除已 remove 的 handler）
```

「延迟删除」是个细节：`remove` 时并不立刻销毁对象（因为 `loop` 线程可能正持有弱引用），而是把迭代器丢进 `deleteQueue_`，攒到一定量或在下次循环里统一清理。

#### 4.2.3 源码精读

`EventHandler` 抽象接口——[src/common/net/EventLoop.h:L30-L40](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/EventLoop.h#L30-L40)：只要提供 `fd()`（要监听的 fd）和 `handleEvents(uint32_t)`（事件回调）就能挂进来。`Transport` 正是继承了它（见 4.3）。

`start()` 创建 epoll 与 eventfd——[src/common/net/EventLoop.cc:L13-L41](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/EventLoop.cc#L13-L41)：注意第 29 行把 eventfd 用 `EPOLLET`（边缘触发）加入 epoll，并在第 38 行起后台线程跑 `loop`。

主循环 `loop()`——[src/common/net/EventLoop.cc:L115-L160](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/EventLoop.cc#L115-L160)：`epoll_wait` 最多一次取 64 个事件（`kMaxEvents`）；第 134 行判 `evt.data.ptr == nullptr` 即 eventfd 唤醒，第 137 行把 eventfd 读空；第 142-145 行把 `data.ptr` 转回 `HandlerWrapper` 并回调 `handleEvents`；第 149-156 行处理 `deleteQueue_`（每次最多清 `kDeleteQueueWakeUpLoopThreshold=128` 个，防止一次循环被删除拖太久）。

注册与注销——[src/common/net/EventLoop.cc:L62-L90](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/EventLoop.cc#L62-L90) `add()` 用 `epoll_ctl(EPOLL_CTL_ADD, handler->fd(), interestEvents)` 把 fd 挂进 epoll，`data.ptr` 指向 `HandlerWrapper`（弱引用 `handler`）；[L92-L113](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/EventLoop.cc#L92-L113) `remove()` 用 `EPOLL_CTL_DEL` 摘除并把迭代器丢进 `deleteQueue_`，攒够 128 个就 `wakeUp()` 主动唤醒循环去清。

`EventLoopPool` 把多个 `EventLoop` 轮询分配——[src/common/net/EventLoop.cc:L183-L186](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/EventLoop.cc#L183-L186)：`add()` 时随机挑一个 `EventLoop`，把连接散到不同事件循环线程上以均衡负载。

#### 4.2.4 代码实践

**目标**：搞清「一个新连接被 accept 后，是怎么挂到 EventLoop 上、事件如何回流」。

**操作步骤**：

1. 读 [src/common/net/IOWorker.cc:L28-L44](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/IOWorker.cc#L28-L44) `addTcpSocket`：构造 `TcpSocket` → 包成 `Transport` → 放进 `pool_` → `eventLoopPool_.add(transport, EPOLLIN|EPOLLOUT|EPOLLET)`。RDMA 路径 `addIBSocket`（[L46-L58](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/IOWorker.cc#L46-L58)）完全对称。
2. 注意 `interestEvents` 都是 `EPOLLIN | EPOLLOUT | EPOLLET`（边缘触发、读写都监听）。
3. 追踪 `EventLoop` 回调到 `Transport::handleEvents`（见 4.3.3）。

**需要观察的现象**：同一个 `EventLoop` 线程名（如 `SvrEL0`）会出现在多个连接的处理堆栈里。

**预期结果**：能画出 `accept → addTcpSocket/addIBSocket → eventLoopPool_.add → epoll_wait → handleEvents` 的链路。**待本地验证**：用 `gdb -p <pid>` attach 到 storage 进程，对 EventLoop 线程查 `bt`，确认它停在 `epoll_wait`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 3FS 用 `EPOLLET`（边缘触发）而不是水平触发？这对 `handleEvents` 的实现有什么要求？

> **答案**：边缘触发效率更高（只在状态变化时通知一次）。代价是 `handleEvents` 必须一次性把数据读/写到底（读到 `EAGAIN`），否则会「漏掉」后续数据。这就是 4.3 里 `doRead`/`doWrite` 用 `while(true)` 循环、并在读不到时 `Suspend` 的原因。

**练习 2**：`eventfd` 在这套机制里解决了什么问题？

> **答案**：`epoll_wait` 是阻塞的，外部线程（如连接线程、配置热更新线程）需要唤醒它时，往 eventfd 写一个值即可让 `epoll_wait` 立刻返回，从而处理 `deleteQueue_` 或新注册的 fd。

---

### 4.3 统一传输层：Transport 与 TCP/RDMA 共用读写状态机

#### 4.3.1 概念说明

`Transport` 是「TCP 与 RDMA 统一抽象」的核心。它做两件事：

1. 持有一个 `Socket*`（可能是 `TcpSocket` 也可能是 `IBSocket`），所有 I/O 都通过 `Socket` 的虚函数完成；
2. 用一组**原子标志位**驱动一个读写状态机，决定何时启动 `doRead`/`doWrite`、何时挂起等待下一次 epoll。

因为上层（`Processor`、serde）只跟 `Transport` 打交道、从不直接碰 `Socket`，所以「换 TCP 还是 RDMA」对业务完全透明。

#### 4.3.2 核心流程

读路径（`doRead`）与写路径（`doWrite`）是对称的事件驱动循环：

```text
epoll 通知 → Transport::handleEvents:
   socket->poll(epollEvents) → 得到 readable/writable 掩码
   把对应 flag 置位（kReadAvailableFlag / kWriteAvailableFlag + kNewWaked）
   若该方向没有在跑的任务 → IOWorker::startReadTask / startWriteTask

doRead(error):
   while true:
     n = socket->recv(readBuff)            # 一次最多读一块
     n==0 → tryToSuspend(挂起等下次 epoll) 或 Retry 或 Fail
     把读到的字节拼成完整 MessageWrapper
     一旦凑齐 ≥1 条完整消息 → ioWorker_.processMsg(batch, transport)  # 交给 Processor

doWrite(error):
   while true:
     从 mpscWriteList_（多生产者无锁队列）取出待写项，拼到 inWritingList_
     writeAll(): 转 iovec[64] → socket->send(iov, len)   # 批量写
     写不完 → Suspend；写完且无新消息 → Suspend
```

`send()`（异步入口）把上层给的 `WriteList` 丢进 `MPSCWriteList`，必要时触发一次 `startWriteTask`。

#### 4.3.3 源码精读

`Transport` 继承 `EventHandler`，并持有 `Socket`——[src/common/net/Transport.h:L22-L46](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Transport.h#L22-L46)：第 104 行 `std::unique_ptr<Socket> socket_` 就是「TCP 或 RDMA」的多态句柄；第 44-45 行 `isTCP()/isRDMA()` 直接看 `serverAddr_` 的类型，业务据此区分（如 `Processor` 用来选 RDMA/TCP 两套服务表）。

`Transport::create` 是 TCP/RDMA 的分叉点——[src/common/net/Transport.cc:L75-L82](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Transport.cc#L75-L82)：`addr.isTCP()` 建 `TcpSocket`，`addr.isRDMA()` 建 `IBSocket`，其余返回 `nullptr`。这就是「同一个 `Transport` 类、两种底层」的实现方式。`connect`（[L84-L98](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Transport.cc#L84-L98)）里 RDMA 的细节值得注意：它**先临时建一条 TCP `ClientContext`**（第 86-89 行）再去调 `ibSocket->connect`——这正是「RDMA 建链走 TCP 旁路」在代码里的体现。

`Socket` 抽象基类——[src/common/net/Socket.h:L14-L39](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Socket.h#L14-L39)：`describe/peerIP/fd/poll/recv/send/flush/check` 这组虚函数就是 TCP 与 RDMA 必须共同实现的契约。`kEventReadableFlag`/`kEventWritableFlag`（第 26-27 行）是 `poll` 返回的事件掩码。

`handleEvents`——[src/common/net/Transport.cc:L344-L373](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Transport.cc#L344-L373)：先 `socket_->poll(epollEvents)`（注意：对 RDMA，这里的「poll」其实是去轮询 CQ，见 4.4.3），再把 readable/writable 转成内部 flag，按需启动读写任务。第 367-372 行的判断保证「同一时刻每个方向最多一个任务在跑」。

`doRead` 主循环——[src/common/net/Transport.cc:L185-L252](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Transport.cc#L185-L252)：循环 `socket_->recv`，校验消息大小（第 227 行 `kMessageMaxSize`），凑齐完整消息后第 244 行 `ioWorker_.processMsg(std::move(msgWrapper), shared_from_this())` 把整批交给 `Processor`。第 208 行 `tryToSuspend` 处理「读到 0 字节（缓冲空）」的三种结局：挂起、重试、失败。

`doWrite` 与 `writeAll`——[src/common/net/Transport.cc:L254-L325](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Transport.cc#L254-L325)：从无锁队列取数据 → `inWritingList_` → `writeAll` 用 `iovec[64]` 批量 `socket_->send`。第 320 行 `result.value() < expectedWriteSize` 表示没写完，挂起等下次可写事件。

任务投递策略 `IOWorker::startReadTask/startWriteTask`——[src/common/net/IOWorker.cc:L118-L136](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/IOWorker.cc#L118-L136)：由配置 `read_write_tcp_in_event_thread` / `read_write_rdma_in_event_thread`（[IOWorker.h:L26-L27](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/IOWorker.h#L26-L27)）决定是在 EventLoop 线程里直接做、还是丢到独立 CPU 线程池。默认 `false`，即 I/O 与事件循环分离，避免慢 I/O 卡住其他连接的事件分发。

#### 4.3.4 代码实践

**目标**：验证「`Transport` 不知道也不关心底下是 TCP 还是 RDMA」。

**操作步骤**：

1. 读 `doRead`（[Transport.cc:L185-L252](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Transport.cc#L185-L252)）与 `doWrite`（[L254-L298](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Transport.cc#L254-L298)），数一数它们各自直接调用了几次 `socket_->`（`recv`/`send`/`poll`/`flush`），确认没有出现任何 `TcpSocket` 或 `IBSocket` 的专属逻辑。
2. 把 `socket_->recv`、`socket_->send`、`socket_->poll`、`socket_->flush` 在 `TcpSocket.cc` 与 `IBSocket.cc` 里的实现各找一处，对比它们底层分别调用了什么系统调用 / verbs 调用。

**需要观察的现象**：`doRead`/`doWrite` 的控制流完全一致，差异只在 `socket_->recv` 内部——TCP 走 `::readv`/`::recvmsg`，RDMA 走「从预填的接收缓冲里 memcpy + 重新 post RECV」（见 4.4.3）。

**预期结果**：能写出一句话结论——「`Transport` 是与传输无关的状态机；传输差异被封装在 `Socket` 的 5 个虚函数里」。

#### 4.3.5 小练习与答案

**练习 1**：`Transport` 用 `std::atomic<uint32_t> flags_`（[Transport.h:L116](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Transport.h#L116)）记录读写状态，为什么不用一把普通互斥锁？

> **答案**：因为读写状态机要被 EventLoop 线程、I/O 线程、`send` 的调用方线程并发触达，但每次只改少数几个 bit、且对「同一方向是否已有任务」要做原子的「检查并设置」。用原子位操作 + `compare_exchange`（见 `tryToSuspend`，[Transport.cc:L163-L183](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Transport.cc#L163-L183)）可以无锁完成，避免高频 I/O 路径上的锁竞争。注意 RDMA 的真正收发数据仍受 `std::mutex mutex_`（[Transport.h:L113](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Transport.h#L113)）保护。

**练习 2**：`Transport::send` 返回 `std::optional<WriteList>`（[Transport.h:L52](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Transport.h#L52)），返回非空代表什么？

> **答案**：代表连接已失效（命中 `kInvalidatedFlag`），这批数据没能投递、需要上层重试。看 [Transport.cc:L111-L113](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Transport.cc#L111-L113)，失效时把队列里取出并交给调用方 `extractForRetry()`。

---

### 4.4 RDMA 传输：IBSocket 的零拷贝收发

#### 4.4.1 概念说明

`IBSocket` 是 `Socket` 的 RDMA 实现。它的 `fd()` 返回的不是普通 socket fd，而是 **CQ 的 completion channel fd**（`channel_->fd`）——所以它能被同一套 `epoll` 机制监听：网卡完成一次 RDMA 操作 → CQ 产出 WC → completion channel 可读 → epoll 唤醒。

`IBSocket` 的收发分两条路：

- **控制面 / 小消息**：用 `SEND/RECV`。本端把数据拷进预注册的 `sendBufs_`，`postSend`（`IBV_WR_SEND`）；对端在预注册的 `recvBufs_` 上 `postRecv` 接收。RPC 的 `MessagePacket` 走这条路。为了不让对端 RECV 缓冲耗尽，接收方会**周期性回 ACK 信用**（用 `ImmData`，immediate data 捎带）。
- **数据面 / 大块数据**：用 `RDMA Read/Write`。本端拿到对端给的 `RDMARemoteBuf`（`addr` + `rkey`），直接 `rdmaRead`/`rdmaWrite`，对端 CPU 完全不参与。storage 的批量读、链式复制写主要走这条路。

建链：两端先经 TCP 旁路交换 `IBConnectReq`/`IBConnectRsp`（含 `qp_num`、`lid`/`gid`、`mtu` 等，见 [IBConnect.h:L123-L168](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBConnect.h#L123-L168)），然后各自 `ibv_modify_qp` 让 QP 走 `INIT → RTR → RTS`，状态机定义在 [IBSocket.h:L236-L243](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.h#L236-L243)（`INIT/CONNECTING/ACCEPTED/READY/CLOSE/ERROR`）。

#### 4.4.2 核心流程

**poll（被 EventLoop 回调）**——经典的「取事件 → 轮询 → 重新请求通知」三段式，避免漏 WC：

```text
poll():
  cqGetEvent()        # ibv_get_cq_event，取一个 completion channel 事件
  cqPoll(events)      # ibv_poll_cq 批量取 WC，分发给 onSended/onRecved/onRDMAFinished...
  cqRequestNotify()   # ibv_req_notify_cq，重新 armed
  cqPoll(events)      # 再 poll 一次（armed 与 poll 之间到达的 WC 不会丢）
  根据 send 缓冲回收情况置 kEventWritableFlag
```

**send（小消息，SEND/RECV）**：从 `sendBufs_` 取一块缓冲 → memcpy 数据进去 → 填满一块就 `postSend`（投递 `IBV_WR_SEND`）。缓冲有限，靠对端 ACK 信用（`ImmData::ack`）回收。

**recv（小消息）**：从 `recvBufs_` 取已收到的数据 memcpy 给上层 → 每用完一块 `postRecv` 重新挂一块接收缓冲 → 攒到 `buf_ack_batch` 个就 `postAck` 给对端补信用。

**rdmaRead/rdmaWrite（数据面）**：构造 `RDMAReqBatch` → `rdmaPostWR` 用 `ibv_post_send` 投递 `IBV_WR_RDMA_READ/WRITE`（SGE 用本端 `lkey`、目的用对端 `raddr+rkey`）→ `co_await ctx.baton` 等 `onRDMAFinished` 把 WC 回填。

#### 4.4.3 源码精读

`IBSocket` 类骨架与配置——[src/common/net/ib/IBSocket.h:L81-L135](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.h#L81-L135)：第 134 行 `int fd() const override { return channel_->fd; }` 是关键——被 epoll 监听的就是 completion channel。`Config`（[L83-L120](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.h#L83-L120)）暴露了一堆 verbs 参数：`max_rdma_wr`（SQ 里 RDMA WR 上限）、`max_sge`（每个 WR 最多几个散布段）、`send_buf_cnt`/`buf_size`（小消息缓冲数量与大小）、`buf_ack_batch`/`buf_signal_batch`（批 ACK / 批信号量）。

QP/CQ 句柄成员——[src/common/net/ib/IBSocket.h:L518-L523](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.h#L518-L523)：`channel_`（comp channel）、`cq_`（完成队列）、`qp_`（队列对）就是 verbs 的三大句柄。

RDMA 读写的对外接口——[src/common/net/ib/IBSocket.h:L155-L167](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.h#L155-L167)：`rdmaRead/rdmaWrite` 接收一个 `RDMARemoteBuf`（对端内存句柄）和若干 `RDMABuf`（本端缓冲），底层转成 `ibv_wr_opcode` 调 `rdma`。批量版 `RDMAReqBatch`（[L186-L226](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.h#L186-L226)）支持一次 `post()` 投递多条请求，分摊 `ibv_post_send` 开销。

`poll` 的三段式——[src/common/net/ib/IBSocket.cc:L329-L362](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.cc#L329-L362)：`cqGetEvent`（第 337 行）→ `cqPoll`（第 340 行）→ `cqRequestNotify`（第 341 行）→ 再 `cqPoll`（第 344 行）。最后第 347-353 行回收对端 ACK 过的 send 缓冲并置 `kEventWritableFlag`。

`cqPoll` 批量取 WC——[src/common/net/ib/IBSocket.cc:L364-L389](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.cc#L364-L389)：每轮 `ibv_poll_cq` 最多 16 个（`kPollCQBatch`），失败的 WC 走 `wcError` 并把 QP 置 `ERROR`，成功的走 `wcSuccess` 按 WR 类型分发。

WC 分发——[src/common/net/ib/IBSocket.cc:L477-L503](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.cc#L477-L503)：`SEND`→`onSended`、`RECV`→`onRecved`、`ACK`→`onAckSended`、`RDMA_LAST`→`onRDMAFinished`、`CLOSE`→置 CLOSE 态。WR 类型编码在 `WRId`（[IBSocket.h:L282-L337](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.h#L282-L337)）里，把一个 64 位 `wr_id` 同时塞「类型 + 附加数据（如 RDMA context 指针 / 接收缓冲下标）」。

`onRecved` 处理到达的小消息与信用——[src/common/net/ib/IBSocket.cc:L517-L549](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.cc#L517-L549)：若带 `IBV_WC_WITH_IMM`（immediate data），第 534 行解析 `ImmData`（可能是 ACK 或 CLOSE，见 [IBSocket.h:L339-L378](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.h#L339-L378)）；否则第 545 行把收到的缓冲下标 push 进 `recvBufs_` 并置 `kEventReadableFlag`，让 `Transport::doRead` 后续来 memcpy。

`send`（小消息）——[src/common/net/ib/IBSocket.cc:L611-L652](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.cc#L611-L652)：从 `sendBufs_` 取空闲块，第 630 行 `memcpy` 数据进去，填满一块（第 636 行）就 `postSend` 投递。第 644-649 行：若缓冲不够（`total<wanted`），记录「等缓冲」开始时间——这是 RDMA 流控的体现。

`recv`（小消息）与信用回补——[src/common/net/ib/IBSocket.cc:L666-L706](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.cc#L666-L706)：第 683 行每用完一块接收缓冲就 `postRecv` 重新挂；第 688 行攒满 `buf_ack_batch` 个就 `postAck` 告诉对端「我又有空闲接收缓冲了」。

`rdmaPost` 协程——[src/common/net/ib/IBSocket.cc:L1011-L1030](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.cc#L1011-L1030)：投递后 `co_await ctx.baton`，等 `onRDMAFinished`（WC 回来）调 `ctx.finish()`（`baton.post()`）才继续。这是把 verbs 的异步完成「协程化」的标准手法。

`rdmaPostWR` 构造 WR 链——[src/common/net/ib/IBSocket.cc:L1032-L1115](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.cc#L1032-L1115)：第 1059-1060 行写对端 `remote_addr`/`rkey`，第 1070-1072 行写本端 `sge.addr`/`length`/`lkey`（来自 `RDMABuf::getMR`），第 1090 行 `ibv_post_send`。只有链尾那一个 WR 带 `IBV_SEND_SIGNALED`（第 1079 行），所以一批只产生一个 WC，减少 CQ 压力。

#### 4.4.4 代码实践

**目标**：验证「`IBSocket` 通过 completion channel fd 与 epoll 对接」这一关键事实。

**操作步骤**：

1. 在 [IBSocket.h:L134](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.h#L134) 确认 `fd()` 返回 `channel_->fd`。
2. 追这条链：`IOWorker::addIBSocket`（[IOWorker.cc:L46-L58](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/IOWorker.cc#L46-L58)）→ `eventLoopPool_.add(transport, EPOLLIN|EPOLLOUT|EPOLLET)` → epoll 监听的就是这个 channel fd → `Transport::handleEvents`（[Transport.cc:L344](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Transport.cc#L344)）→ `socket_->poll`（即 `IBSocket::poll`，[IBSocket.cc:L329](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.cc#L329)）→ `cqPoll`。

**需要观察的现象**：RDMA 的事件源不是「socket fd 可读」，而是「completion channel 可读 = 有 WC 到达」。

**预期结果**：能说清「epoll → channel fd 可读 → cqPoll 取 WC → 按 WRType 分发」这条 RDMA 的事件回流路径。**待本地验证**：用 `cat /proc/<pid>/fdinfo/<fd>` 看 channel fd 的引用，或用 `perf trace` 观察 `epoll_wait` 返回后紧跟 `ibv_poll_cq`（实际是 `ioctl` 到verbs）。

#### 4.4.5 小练习与答案

**练习 1**：`cqPoll` 里为什么要在 `cqRequestNotify`（`ibv_req_notify_cq`）前后**各 poll 一次**（[IBSocket.cc:L340 与 L344](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.cc#L340)）？

> **答案**：`ibv_req_notify_cq` 是「武装」CQ——下次有 WC 时通过 completion channel 通知。但「武装」到「武装完成」之间如果有 WC 到达，会错过通知。所以标准做法是先 poll（取走已到的）→ 武装 → 再 poll（取走武装期间到达的），保证不丢。

**练习 2**：`rdmaPostWR` 为什么只有链尾的 WR 带 `IBV_SEND_SIGNALED`（[IBSocket.cc:L1079](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.cc#L1079)）？

> **答案**：每个 signaled WR 都会在 CQ 产生一个 WC。一批 RDMA 操作只关心「整批是否完成」，所以只在最后一个 WR 上请求 signal，一批只产生一个 WC，大幅减少 CQ 占用与 poll 开销。`RDMA_LAST` 这个 WRType（[IBSocket.h:L289](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.h#L289)）就是用来识别这个「收尾 WC」并唤醒协程的。

**练习 3**：RDMA 建链时为什么需要一条 TCP 旁路？代码里体现在哪？

> **答案**：因为 verbs 的 QP 还没进入 READY 之前不能传 RDMA 数据，两端必须先交换 `qp_num`/`lid`/`gid`/`mtu` 等参数（`IBConnectReq`/`IBConnectRsp`，[IBConnect.h:L139-L168](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBConnect.h#L139-L168)）。`Transport::connect`（[Transport.cc:L85-L91](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Transport.cc#L85-L91)）正是先临时建一条 TCP `ClientContext`，再调 `ibSocket->connect` 完成这次握手。

---

### 4.5 RDMA 缓冲管理：RDMABuf / RDMABufPool 与内存注册

#### 4.5.1 概念说明

要让网卡直接访问某段内存，必须先「钉住」（pin，防止被换出）并「注册」成 MR（`ibv_reg_mr`），拿到 `lkey`（本端用）与 `rkey`（告诉对端用）。这是一笔不小的开销，所以 3FS 把缓冲**池化**：

- `RDMABuf`：一段已注册的内存（带 `Inner`，持有指针、容量、每个 IB 设备的 `mr`）。可整体用、也可 `subrange`/`takeFirst` 切片用。
- `RDMARemoteBuf`：对端某段内存的「远端句柄」——`addr` + `length` + 每个设备一把 `rkey`。本端拿到它就能 `rdmaRead`/`rdmaWrite` 对端那块内存。
- `RDMABufPool`：固定大小缓冲的池，用信号量限流，用 `freeList_` 回收。

零拷贝的极致形态：`RDMABuf::createFromUserBuffer` 直接把**用户已有的内存**注册成 MR（不分配新内存），这样上层（如 storage 的读结果缓冲）的数据可以原地对网卡可见。

#### 4.5.2 核心流程

分配：

```text
RDMABufPool::allocate(timeout):
  sem_.try_wait() / co_wait()          # 限流：池容量是信号量初值
  若 freeList 非空 → 取一块 Inner 复用
  否则 → RDMABuf::allocate:
           Inner.allocateMemory()   # memalign 页对齐分配
           Inner.registerMemory()   # 对每个 IBDevice 调 ibv_reg_mr
```

回收：`RDMABuf` 的 `shared_ptr` 用自定义删除器 `Inner::deallocate`——析构时不释放内存，而是把 `Inner*` 还回池的 `freeList_` 并 `sem_.signal()`。

转远端句柄：`RDMABuf::toRemoteBuf()` 产出 `(addr, length, rkeys[])`，序列化后随 RPC 发给对端（`RDMARemoteBuf` 是 serde 可序列化的，见 [RDMABuf.h:L354-L434](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/RDMABuf.h#L354-L434)）。

#### 4.5.3 源码精读

`RDMARemoteBuf`——[src/common/net/ib/RDMABuf.h:L40-L134](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/RDMABuf.h#L40-L134)：第 64 行 `getRkey(devId)` 按本端使用的 IB 设备取出对端那把对应的 `rkey`（一台机器可能有多个 IB 设备，所以 `rkeys_` 是按 `devId` 索引的数组）。第 73-97 行的 `advance/subtract/subrange` 让远端句柄也能像本地缓冲一样切片。

`RDMABuf`——[src/common/net/ib/RDMABuf.h:L138-L316](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/RDMABuf.h#L138-L316)：第 213 行 `getMR(dev)` 取本端 `mr`（供 `IBSocket::rdmaPostWR` 拿 `lkey`，[IBSocket.cc:L1063](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.cc#L1063)）；第 220 行 `toRemoteBuf()` 把自己变成可发给对端的 `RDMARemoteBuf`。`Inner`（[L241-L291](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/RDMABuf.h#L241-L291)）持有真正的指针与 `mr` 数组。

注册时的访问权限——[src/common/net/ib/RDMABuf.h:L243-L244](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/RDMABuf.h#L243-L244)：`kAccessFlags = LOCAL_WRITE | REMOTE_WRITE | REMOTE_READ | RELAXED_ORDERING`。`REMOTE_READ/REMOTE_WRITE` 允许对端 RDMA 读写本块；`RELAXED_ORDERING` 是为弱序内存模型（如 PCIe 的某些场景）性能优化。

`RDMABufPool`——[src/common/net/ib/RDMABuf.h:L318-L349](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/RDMABuf.h#L318-L349)：`sem_`（信号量，初值 `bufCnt`）是池容量闸门，`freeList_` 是回收队列。

内存分配与注册——[src/common/net/ib/RDMABuf.cc:L93-L127](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/RDMABuf.cc#L93-L127)：第 96 行 `memalign(页大小, capacity)` 页对齐分配（对齐对 RDMA 性能重要）；第 118 行对**每一个** `IBDevice` 调 `dev->regMemory(ptr_, capacity_, kAccessFlags)` 注册（因为本机可能有多块网卡，数据可能从任意一块出入）。回收在 [RDMABuf.cc:L50-L63](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/RDMABuf.cc#L50-L63)：析构时对每个设备 `deregMemory`，并 `memory::deallocate` 还内存。

池的分配与回收——[src/common/net/ib/RDMABuf.cc:L146-L175](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/RDMABuf.cc#L146-L175)：`allocate` 先 `sem_.try_wait`/`co_wait`（第 148-157 行），有空闲就复用（第 161-165 行），否则新建（第 168 行）；`deallocate`（第 171-175 行）把 `Inner*` push 回 `freeList_` 并 `sem_.signal()`——这就是「注册一次、反复使用」的关键，避免每次传输都重新 `ibv_reg_mr`。

用户内存零拷贝注册——[src/common/net/ib/RDMABuf.cc:L42-L48](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/RDMABuf.cc#L42-L48)：`createFromUserBuffer` 只 `registerMemory` 不 `allocateMemory`（`userBuffer_=true`，[RDMABuf.h:L256-L261](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/RDMABuf.h#L256-L261)），析构时也不释放内存（[RDMABuf.cc:L59](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/RDMABuf.cc#L59)）。

#### 4.5.4 代码实践

**目标**：在真实业务里找到「本端把 `RDMABuf` 变成 `RDMARemoteBuf` 发给对端、对端用它做 RDMA」的调用点。

**操作步骤**：

1. 看 storage 客户端如何把本地读缓冲注册成远端句柄——[src/client/storage/StorageClientImpl.cc:L697](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L697) `iobuf.toRemoteBuf()`：客户端把一块本地 `IOBuf`（已注册的 RDMA 内存）转成远端句柄，随 batchRead 请求发给 storage 节点。
2. 看 storage 节点收到请求后如何用它做 RDMA Write 回传——[src/storage/aio/BatchReadJob.cc:L80-L81](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/BatchReadJob.cc#L80-L81)：`batch.add(job.readIO().rdmabuf, localbuf)` 把客户端给的远端句柄与本端读到的数据加进一个 `rdmaWriteBatch()`，最后 `post` 出去（`IBV_WR_RDMA_WRITE`）。

**需要观察的现象**：数据从 storage 节点的 SSD 读出后，**直接 RDMA Write 进客户端内存**，客户端 CPU 只负责等完成通知，全程零拷贝。

**预期结果**：能标注出两个回收点——①客户端侧 `RDMABuf`（`IOBuf` 背后）用完析构回池；②storage 侧 `localbuf` 用完回收。**待本地验证**：在 storage 节点 `monitor` 指标里找 `storage.rdma_write.count` / `storage.rdma_write.bytes`（[BatchReadJob.cc:L9-L11](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/BatchReadJob.cc#L9-L11)）随读流量增长。

#### 4.5.5 小练习与答案

**练习 1**：`RDMARemoteBuf` 里的 `rkeys` 为什么是一个「按 `devId` 索引的数组」而不是单个 `rkey`？

> **答案**：因为同一块内存在**每个 IB 设备**上注册时会拿到**不同**的 `rkey`（`rkey` 是设备相关的）。发起 RDMA 的本端要知道自己用的是哪块网卡，才能选对应的 `rkey`。`getRkey(devId)`（[RDMABuf.h:L64-L71](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/RDMABuf.h#L64-L71)）做的就是这个选择。

**练习 2**：`RDMABufPool` 用 `folly::fibers::Semaphore`（`sem_`）限流，而不是简单地 `new`，好处是什么？

> **答案**：①控制已注册内存总量（MR 会占用网卡片上资源与 pin 住的物理页），防止内存膨胀；②当池耗尽时，`allocate` 协程会挂起等待（[RDMABuf.cc:L148-L157](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/RDMABuf.cc#L148-L157)），形成自然背压，而不是失败；③复用已注册缓冲，省掉反复 `ibv_reg_mr`/`dereg_mr` 的开销。

---

## 5. 综合实践

> 本任务是讲义规格指定的代码实践：**绘制从 RPC 请求到达 IBSocket 到被 Processor 协程处理的调用链，标注 RDMA buffer 的注册与回收点**。我们把任务聚焦到「storage 节点收到一个 batchRead 请求并回传数据」这条最典型的链路上。

### 5.1 实践目标

把本讲 5 个模块串成一张图，验证你对以下链条的整体理解：

```text
网卡 RDMA 收到一条请求消息
  → completion channel 可读
  → EventLoop(epoll) 醒来 → Transport::handleEvents
  → IBSocket::poll → cqPoll → onRecved(置 kEventReadableFlag)
  → Transport::doRead → socket_->recv(IBSocket::recv, memcpy 出消息体 + postRecv 回补)
  → IOWorker::processMsg → Processor::processMsg
  → Processor::unpackSerdeMsg → 反序列化 MessagePacket → 按 (serviceId,methodId) 找服务
  → Processor::tryToProcessSerdeRequest → 进入协程池执行 processSerdeRequest
  → CallContext::handle → 业务 handler（如 batchRead）
  → handler 内：用请求里携带的 RDMARemoteBuf + 本端 RDMABuf(localbuf) 发起 rdmaWriteBatch().post()
  → ibv_post_send → WC 完成 → onRDMAFinished → 唤醒协程
```

### 5.2 操作步骤

请按顺序打开下列源码点，在每个点上用一句话写下「它在这一链条里做了什么」：

1. **事件入口**：[IBSocket.h:L134](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.h#L134)（`fd()` 返回 channel fd）→ [EventLoop.cc:L132-L145](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/EventLoop.cc#L132-L145)（epoll 回调 `handleEvents`）。
2. **取 WC**：[IBSocket.cc:L329-L362](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.cc#L329-L362)（`poll`）→ [L517-L549](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.cc#L517-L549)（`onRecved` 置可读）。
3. **读出消息**：[Transport.cc:L185-L252](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Transport.cc#L185-L252)（`doRead`）→ [L244](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Transport.cc#L244) `processMsg`。
4. **解包派发**：[Processor.h:L85-L156](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Processor.h#L85-L156)（`unpackMsg` → `unpackSerdeMsg` → `tryToProcessSerdeRequest`）。
5. **进协程**：[Processor.h:L158-L170](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/Processor.h#L158-L170)（`processSerdeRequest` 协程，`CallContext::handle`）。
6. **业务里发起 RDMA 回传**：[BatchReadJob.cc:L80-L81](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/aio/BatchReadJob.cc#L80-L81)（`batch.add(rdmabuf, localbuf)`）→ `post` → [IBSocket.cc:L1032-L1090](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.cc#L1032-L1090)（`rdmaPostWR` 构造 WR 并 `ibv_post_send`）→ [L560-L564](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.cc#L560-L564)（`onRDMAFinished` 唤醒）。

### 5.3 需要标注的「注册与回收点」

在图上用 ★ 标出 RDMA buffer 的生命周期：

- **注册点（reg）**：[RDMABuf.cc:L107-L127](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/RDMABuf.cc#L107-L127) `Inner::registerMemory`（对每个 IBDevice `ibv_reg_mr`）；客户端把 `IOBuf` 转远端句柄 [StorageClientImpl.cc:L697](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L697) `iobuf.toRemoteBuf()`。
- **回补点（recv 侧）**：[IBSocket.cc:L683-L696](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/IBSocket.cc#L683-L696) 每用完一块接收缓冲 `postRecv` 重新挂、攒批 `postAck`。
- **回收点（pool）**：[RDMABuf.cc:L171-L175](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/RDMABuf.cc#L171-L175) `RDMABufPool::deallocate` 把 `Inner*` 还回 `freeList_` 并 `sem_.signal()`；最终释放时 [RDMABuf.cc:L50-L63](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/ib/RDMABuf.cc#L50-L63) `deregMemory`。

### 5.4 预期产出与结果

- 一张包含上述 6 步 + 3 类 ★ 标记的调用链图（手绘或文本时序图均可）。
- 能用一句话回答：**「为什么这条链路里 storage 节点的 CPU 几乎不碰数据本身？」**——因为请求小消息走 `SEND/RECV`（`IBSocket::send/recv`，memcpy + postRecv 回补），而响应大块数据走 `RDMA_WRITE`（直接写进客户端注册过的 `RDMARemoteBuf`），全程没有内核态拷贝、没有对端 CPU 介入。

> 说明：本综合实践为源码阅读型。若要在运行集群上验证，可对 storage 进程 attach `gdb`，在 `Processor::processSerdeRequest` 与 `IBSocket::rdmaPostWR` 各下断点，发一次 batchRead 触发，观察命中顺序。日志/断点验证部分标注「待本地验证」。

## 6. 本讲小结

- `net::Server` 用「服务组」分层：每个 `ServiceGroup` 把 `Listener`（建链）+ `IOWorker`（I/O）+ `Processor`（派发）+ `serde::Services`（业务）捆成一个自洽单元，最多 4 组，默认走 RDMA（`network_type=Address::RDMA`）。
- `EventLoop` 是 `epoll`+`eventfd` 的封装，每个 `IOWorker` 一个池；连接被 accept 后挂进 epoll，事件回调 `Transport::handleEvents`。延迟删除保证线程安全。
- `Transport` 是 **TCP/RDMA 的统一抽象**：同一套原子标志位读写状态机，通过 `Socket` 的虚函数（`recv/send/poll/flush`）驱动底层；`doRead` 凑齐消息交 `Processor`，`doWrite` 用 `iovec[64]` 批量写。
- `IBSocket` 的 `fd()` 是 **CQ 的 completion channel**，所以 RDMA 能复用同一套 epoll；小消息走 `SEND/RECV`（带 ACK 信用流控），大块数据走 `RDMA Read/Write`（只有链尾 WR 请求 signal，一批只产一个 WC）。
- `RDMABuf`/`RDMABufPool` 把「钉住 + 注册」的内存池化，`toRemoteBuf` 把本地缓冲变成可发给对端的 `(addr, rkey)` 句柄，`createFromUserBuffer` 支持用户内存零拷贝注册；这是 storage 读结果能直接 RDMA 写进客户端内存的基础。
- RDMA 建链本身不走 RDMA：先经 TCP 旁路交换 `IBConnectReq/Rsp`（QP 号、LID/GID、MTU），再 `ibv_modify_qp` 把 QP 推到 `READY`。

## 7. 下一步学习建议

- **横向**：回到 u2-l1（服务骨架），对比 `net::Server::start` 在 `TwoPhaseApplication` 的「阶段二」里是如何被调用的，把「骨架 → 网络层」接缝看清。
- **纵向（推荐）**：进入 u3（mgmtd）或 u5（storage）的总览篇，看一个具体服务如何 `addSerdeService` 注册自己的 RPC，以及它的 `ServiceGroup` 配置（线程池、网络类型）如何写在 `*_main.toml` 里。
- **深入 RDMA**：若你想吃透 `IBSocket`，建议接着读 `src/common/net/ib/IBConnect.cc`（建链握手的状态机实现）与 `IBDevice.cc`（设备/MR/PD/CQ 的 verbs 封装），并对照 `configs/` 下任意一个 `*_main.toml` 里 `io_worker.ibsocket` 段的参数（`max_rdma_wr`、`send_buf_cnt`、`buf_ack_batch` 等）理解它们对吞吐与时延的影响。
- **零拷贝闭环**：学完 u5-l2（读路径）与 u7-l3（USRBIO）后，再回头看本讲的「综合实践」，你会看到一条从用户态共享内存 → 客户端 `RDMABuf` → storage `RDMA_WRITE` 的完整零拷贝链路。
