# TCP Transport：通用回退传输

## 1. 本讲目标

Mooncake Transfer Engine 支持 RDMA、NVLink、CXL、EFA、Ascend 等多种高性能传输。但并不是每台机器都有 RNIC 或 GPU，也不是所有环境都允许内核旁路。`TcpTransport` 就是那条"总能用"的**通用回退传输**：只要有 TCP/IP 协议栈，Mooncake 就能跑起来。

本讲带你深入 `TcpTransport` 的内部实现。学完后，你应该能够：

1. 说清 `TcpTransport` 的启动流程：它如何选端口、启动 handshake daemon、并把端口/segment 信息发布到 metadata；
2. 理解它基于 **asio 协程式异步 I/O** 的会话模型（`ServerSession` / `ClientSession`），以及按 chunk 分块收发的机制；
3. 掌握**客户端连接池**（connection pool）的设计：何时复用连接、如何清理空闲连接、如何用环境变量开关；
4. 理解 **TCP-only 模式下的本机 memcpy 优化**：为什么同进程本机两端不会再走 TCP loopback，而是直接 `memcpy`，以及 `isTcpOnly()` 如何驱动这一决策。

> 本讲只读不写源码，所有引用都来自当前 HEAD `945f3e61`。本讲依赖 `u3-l1`（Transport 基类的 Slice/Task/Batch 模型），如果你还不清楚 `TransferRequest`、`Slice`、`BatchID` 的含义，建议先读那一讲。

## 2. 前置知识

在进入源码前，先建立四个直觉。

**直觉一：TCP 是字节流，需要自己定"消息边界"。** TCP 不像 RDMA 那样一次 `post_send` 对应一次 `post_recv`，它只保证字节按序到达。所以 Mooncake 必须自己定义一个"会话头"（`SessionHeader`），先告诉对端"接下来要读/写多少字节、写到哪个地址"，然后再搬数据。

**直觉二：异步 I/O 才能扛住高并发。** 如果每来一个传输请求就阻塞一个线程，连接数一多线程就爆了。Mooncake 用 [asio](https://think-async.com/Asio/)（Boost.Asio 的独立版本）的 **Proactor 模型**：所有 socket 上的 `async_read` / `async_write` 都注册到同一个 `io_context`，由一两个后台线程轮询，完成时回调。这就是"协程式 I/O"的含义——逻辑上像顺序代码，实际是事件回调链。

**直觉三：建连成本高，要复用。** 一次 TCP 传输若每次都 `connect` + `close`，DNS 解析 + 三次握手 + 慢启动会让小消息的延迟高得离谱。所以客户端维护一个**连接池**：同一对 (host, port) 的连接用完不关，放回池子，下次直接取用。

**直觉四：本机搬数据，memcpy 永远比走内核快。** 如果源和目的都在同一个进程的地址空间里（比如 Store client 读自己进程 mount 的 segment），走 TCP loopback 等于"用户态 → 内核态 → 用户态"白绕两圈，而一次 `memcpy` 直接搞定。但前提是**两端必须是同一个进程**（共享虚拟地址空间）；同一个 host 上的两个不同进程地址空间不同，memcpy 会段错误。

> 术语提示：**loopback** 指 `127.0.0.1` 网卡回环，数据仍要经过内核协议栈；**memcpy** 指纯用户态内存拷贝，不经过任何协议栈。两者都"不出机器"，但路径完全不同。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp` | **本讲主角**。`TcpTransport` 的全部实现：启动流程、asio 异步会话、连接池、`submitTransfer`/`startTransfer`。 |
| `mooncake-transfer-engine/include/transport/tcp_transport/tcp_transport.h` | `TcpTransport` 的声明，以及连接池的数据结构（`PooledConnection`、`ConnectionKey`、空闲超时常量）。 |
| `mooncake-transfer-engine/include/transfer_engine.h` | `TransferEngine::isTcpOnly()` 的对外声明（第 169-175 行）。 |
| `mooncake-transfer-engine/include/multi_transport.h` | `MultiTransport::isTcpOnly()` 的声明（第 58-64 行）。 |
| `mooncake-transfer-engine/src/multi_transport.cpp` | `isTcpOnly()` 与 `selectTransport()` 的实现，决定"只有 tcp 时"的判定与按协议选路。 |
| `mooncake-transfer-engine/src/transfer_engine.cpp` | `TransferEngine::isTcpOnly()` 的转发实现（第 611-618 行），含 TENT 模式下的特判。 |
| `mooncake-store/src/transfer_task.cpp` | **TCP-only 优化的核心**。`TransferSubmitter` 如何根据 `isTcpOnly()` 自动开启 memcpy、`selectStrategy` 如何在 `LOCAL_MEMCPY` 与 `TRANSFER_ENGINE` 间抉择。 |
| `mooncake-store/tests/client_tcp_local_memcpy_test.cpp` | 验证本机 memcpy 优化的端到端测试，是本讲"代码实践"的主要依据。 |

## 4. 核心概念与源码讲解

我们按"从启动到收发、再到优化"的顺序，拆成四个最小模块：

- 4.1 `TcpTransport` 的启动：端口、handshake daemon 与监听
- 4.2 协程 I/O 会话模型：`SessionHeader` / `ServerSession` / `ClientSession`
- 4.3 客户端连接池：复用、归还与空闲清理
- 4.4 TCP-only 本机 memcpy 优化：`isTcpOnly()` 与策略抉择

---

### 4.1 TcpTransport 的启动：端口、handshake daemon 与监听

#### 4.1.1 概念说明

任何一个传输协议要在 Mooncake 里可用，必须完成三件事：

1. **占用一个监听端口**，准备接收别的节点发来的数据；
2. **把自己的地址信息（IP + 端口）发布到 metadata**，让别人能查到"目标 segment 在哪台机器的哪个端口"；
3. **启动后台 I/O 线程**，真正去 accept 连接、读写数据。

`TcpTransport` 是 `Transport` 基类（见 `u3-l1`）的一个具体子类，`getName()` 返回 `"tcp"`。它在 `TransferEngineImpl::init` 里作为回退被安装：当没有 RDMA/UB 等硬件 transport 时，就装一个 tcp。

#### 4.1.2 核心流程

`install()` 是入口，它的执行顺序如下：

```text
install(local_server_name, meta, topo)
  ├── findAvailableTcpPort(sockfd)   // 让 OS 分配一个空闲 TCP 端口
  ├── allocateLocalSegmentID(port)   // 在 metadata 里登记本 segment，protocol="tcp"
  ├── startHandshakeDaemon()         // 启动握手 daemon（用于节点间交换 RPC 元信息）
  ├── updateLocalSegmentDesc()       // 把 segment 描述发布到 metadata（etcd / 等）
  ├── new TcpContext(port, validate) // 创建 acceptor，绑定端口并 listen
  └── thread_ = std::thread(worker)  // 启动后台 I/O 线程，跑 io_context.run()
```

其中 `worker()` 线程在一个循环里调用 `doAccept()` 注册接受回调，然后 `io_context.run()` 阻塞驱动所有异步操作；若 `run()` 异常退出，它会 `restart()` 后再来一轮。

handshake daemon 并不是 TCP 数据通道，而是 Mooncake 节点之间交换"我在哪个 RPC 端口、socket fd 是多少"等元信息的辅助通道。`TcpTransport` 复用了 metadata 层的 `startHandshakeDaemon`。

#### 4.1.3 源码精读

`install()` 的完整实现：选端口 → 登记 segment → 起 daemon → 发布 → 建 context → 起线程。

[TcpTransport::install (tcp_transport.cpp:645-685)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp#L645-L685) —— 这是 TCP transport 的启动主流程。注意第 679-681 行构造 `TcpContext` 时传入了一个 lambda `[this](addr, size){ return validateAddress(addr, size); }`，它会在**服务端收到写/读请求时被调用**，用于校验对端发来的目标地址是否落在本地已注册的 buffer 内（安全防护，防止对端写任意内存）。

`allocateLocalSegmentID` 把 `protocol` 设为 `"tcp"`，并记录 `tcp_data_port`。这个 protocol 字段非常关键——它就是 4.4 节里 `isTcpOnly()` 判定的依据。

[TcpTransport::allocateLocalSegmentID (tcp_transport.cpp:687-701)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp#L687-L701) —— 第 694-695 行设置 `desc->protocol = "tcp"`，第 697 行写入数据端口。

`worker()` 的实现：

[TcpTransport::worker (tcp_transport.cpp:821-833)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp#L821-L833) —— 单线程驱动 `io_context`，`catch` 住异常后 `restart()` 继续，保证一个偶发异常不会让整个 TCP 传输挂掉。

`TcpContext` 构造函数负责"绑定端口并 listen"，并且优先尝试 **IPv6 双栈**（一个 v6 socket 同时收 v4/v6 流量），失败再退回纯 IPv4：

[TcpContext 构造函数 (tcp_transport.cpp:555-582)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp#L555-L582) —— 第 563 行 `set_option(asio::ip::v6_only(false), ec)` 关键：它让 v6 监听 socket 同时接受 v4 连接（双栈）。`doAccept` 在接受连接后立刻 `set_option(no_delay(true))`（第 588 行）关闭 Nagle 算法，降低小消息延迟。

#### 4.1.4 代码实践：跟踪端口分配与启动日志

**实践目标**：确认 TCP transport 启动后确实监听了一个端口，并把它发布到了 metadata。

**操作步骤**：

1. 在本地以 TCP-only 方式启动一个最小 TransferEngine（无 RDMA 设备时，`init` 会自动回退到 tcp；详见 `transfer_engine_impl.cpp` 的 `installTransport("tcp", nullptr)` 调用）。
2. 用 `glog` 的 INFO 级别日志（默认 stderr 可见）。关注这一行：

[install 末尾的日志 (tcp_transport.cpp:678)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp#L678) —— `"TcpTransport: listen on port " << tcp_port`。

3. 用 `ss -ltnp | grep <进程>` 或 `netstat -ltnp` 查看该进程实际监听的端口，对照日志中的 `tcp_port` 是否一致。

**需要观察的现象**：日志打印的端口与 `ss` 看到的监听端口相同；并且监听地址若机器支持 IPv6，会是一个 `::`（双栈）socket。

**预期结果**：端口一致，证明 `findAvailableTcpPort` → `TcpContext` 绑定 → listen 链路打通。**待本地验证**（具体端口号随环境变化）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `doAccept` 接受连接后要设置 `no_delay(true)`？

> **答案**：`no_delay(true)` 即 TCP_NODELAY，关闭 Nagle 算法。Mooncake 的传输会频繁发送小块（会话头只有 17 字节），Nagle 会把它们攒包延迟发送，显著增加小消息延迟。关闭后每个 `async_write` 尽快发出。

**练习 2**：`worker()` 为什么要用 `while (running_)` 外面套 `try/catch`，并在 catch 里 `restart()`？

> **答案**：`io_context.run()` 在抛异常时会退出。若不重启，TCP 传输就永久停摆。外层循环 + `restart()` 让偶发异常（如某次 accept 失败）不至于让整个 transport 不可用。

---

### 4.2 协程 I/O 会话模型：SessionHeader / ServerSession / ClientSession

#### 4.2.1 概念说明

TCP transport 把每一次"单段传输"建模成一个**会话（Session）**。会话分两端：

- **服务端 `ServerSession`**：被动方。它先读一个 17 字节的 `SessionHeader`，知道对方要做什么操作（读还是写）、写到哪个地址、多少字节，然后据此 `readBody`（对方写过来，我读入内存）或 `writeBody`（对方要读，我把内存发出去）。
- **客户端 `ClientSession`**：主动方。它先 `writeHeader` 告诉服务端意图，再按 opcode 走 `writeBody`（写操作）或 `readBody`（读操作）。

会话头是一个固定的小结构体，用**小端序**在网络上传输，到端再做 `le64toh` / `htole64` 转换：

[SessionHeader (tcp_transport.cpp:53-57)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp#L53-L57) —— `size`（字节数）、`addr`（目标地址）、`opcode`（READ/WRITE）。这就是 TCP 自定义的"消息边界"。

#### 4.2.2 核心流程

以**客户端发起一次 WRITE** 为例，协程式回调链如下（箭头表示"完成后触发下一个 async 操作"）：

```text
ClientSession::initiate(buffer, dest_addr, size, WRITE)
  └── writeHeader()  --async_write(SessionHeader)-->
        └── [opcode==WRITE] writeBody()
              ├── 取 chunk = min(getChunkSize(), 剩余字节)
              ├── chunk==0 ? 完成(COMPLETED), 回调 on_finalize_/on_complete_
              └── async_write(chunk) --> 累加 transferred --> 递归 writeBody()

服务端对称：
ServerSession::start() -> readHeader() --async_read(SessionHeader)-->
  ├── validate_addr(addr, size)   // 安全校验
  └── [opcode==WRITE] readBody()  // 把对方发来的数据写入本地 addr
        └── async_read(chunk) --> 累加 --> 递归 readBody()
            └── chunk==0 ? start() 等待本连接的下一个请求
```

关键点：

1. **分块收发**：单次 `async_read/async_write` 不是一次搬完，而是每次搬 `getChunkSize()`（默认 64KB），递归调用自己直到搬完。这控制了单次 I/O 的内存占用。
2. **连接复用语义**：服务端搬完一段后，第 168-170 行不是关闭连接，而是再次 `start()`——**在同一条 TCP 连接上等待下一个会话头**。这正是连接池能工作的前提：一条连接可以串行承载多个会话。
3. **完成回调**：客户端搬完后，通过 `on_finalize_(status)` 上报 Slice 成功/失败（驱动 `u3-l1` 讲的原子计数），通过 `on_complete_()` 归还连接到池子（见 4.3）。

分块大小由环境变量控制：

[getChunkSize (tcp_transport.cpp:41-51)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp#L41-L51) —— 默认 64KB，可用 `MC_TCP_SLICE_SIZE` 覆盖。

#### 4.2.3 源码精读

**服务端读会话头并分流**：

[ServerSession::readHeader (tcp_transport.cpp:125-157)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp#L125-L157) —— 第 141 行 `local_buffer_ = (char*)(le64toh(header_.addr))` 把对端发来的地址转成本地指针；第 143-151 行做 `validate_addr` 安全校验；第 152-155 行按 opcode 分流到 `readBody`（WRITE：对方写给我，我读入）或 `writeBody`（READ：对方要读，我发出）。

**服务端写 body（回应 READ 请求，把本地数据发出去）**：

[ServerSession::writeBody (tcp_transport.cpp:159-226)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp#L159-L226) —— 第 164-165 行 `buffer_size = min(getChunkSize(), 剩余)`；第 166-171 行当 `buffer_size==0` 时 `start()` 等下一个请求（连接复用）；第 202 行 `async_write` 发出这一块，回调里累加 `total_transferred_bytes_` 后递归 `writeBody()`。（其中第 176-200 行是 GPU 路径：若 `addr` 落在显存，先 `cudaMemcpy` 到一块临时 DRAM buffer 再走 socket，因为 asio 不能直接发显存指针。）

**客户端发起**：

[ClientSession::initiate (tcp_transport.cpp:321-330)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp#L321-L330) —— 填好 `SessionHeader`（`htole64` 转小端），锁住会话互斥量（保证一条连接上同一时刻只有一个会话在跑），然后 `writeHeader()`。

> 注意 `session_mutex_` 的加/解锁方式：`initiate`/`start` 里 `lock()`，在会话**真正结束或出错**的回调里才 `unlock()`。这是一种"把锁当令牌"的用法——保证同一条 pooled 连接上的多个会话串行执行，不会出现两个会话头交错写到同一条 socket 上。

#### 4.2.4 代码实践：阅读测试理解会话行为

**实践目标**：理解一次 TCP WRITE 在源码层面的收发对应关系，而非凭空想象。

**操作步骤**：

1. 阅读 `ServerSession::readBody`（[tcp_transport.cpp:228-304](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp#L228-L304)）与 `ClientSession::writeBody`（[tcp_transport.cpp:464-552](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp#L464-L552)）。
2. 在一张纸上画出："客户端 WRITE 1MB 数据" 时，双方各自调用了多少次 `async_write` / `async_read`（设 `MC_TCP_SLICE_SIZE` 为默认 64KB）。
3. 尝试在启动前 `export MC_TCP_SLICE_SIZE=4096`（4KB），重新推算分块次数。

**需要观察的现象**：1MB ÷ 64KB = 16 次 body 收发（外加 1 次会话头）；改成 4KB 后变成 256 次。

**预期结果**：分块次数 = ⌈total / chunk⌉，验证你对递归收发模型的理解。这是一个纯源码阅读推演任务，无需运行；若想实测，可对一次传输打日志统计 `total_transferred_bytes_` 累加点数。**待本地验证**（实测点数）。

#### 4.2.5 小练习与答案

**练习 1**：为什么服务端在 `readBody` / `writeBody` 搬完一段后调用 `start()` 而不是直接 `return`？

> **答案**：调用 `start()` 让这条 TCP 连接继续等待下一个 `SessionHeader`，实现**连接复用**。若直接 return，连接会因为没有后续 read 而被对端关闭，连接池就失去意义。

**练习 2**：会话头里 `addr` 是对端"想当然"给的目标地址，服务端为什么要 `validate_addr`？

> **答案**：防止恶意或出错的对端指定任意内核/未授权地址造成越界写。`validateAddress`（[tcp_transport.cpp:1001-1014](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp#L1001-L1014)）检查 `(addr, size)` 是否完全落在某个已注册 buffer 区间内，否则拒绝该会话。

---

### 4.3 客户端连接池：复用、归还与空闲清理

#### 4.3.1 概念说明

如果没有连接池，每次 `startTransfer` 都要 `resolver.resolve` + `asio::connect`（DNS + 三次握手），传完立刻 `close`。对于高频小消息，建连开销会远大于数据搬运本身。

`TcpTransport` 的连接池按 **(host, port)** 为键缓存 socket：

- `getConnection(host, port)`：先在池里找一个"空闲且存活"的连接；没有就新建并放进池子。
- `returnConnection(host, port, socket)`：一次传输结束后把连接标记为空闲（`in_use=false`），下次可复用。
- `cleanupIdleConnections()`：定期清理空闲超过 60 秒的连接。

连接池**默认关闭**，需要 `MC_TCP_ENABLE_CONNECTION_POOL=1` 开启。这体现了"回退传输优先正确、性能可调"的设计取向。

#### 4.3.2 核心流程

```text
startTransfer(slice)
  ├── getConnection(meta_entry.ip, desc->tcp_data_port)
  │     ├── [pool 关闭] 每次 connect 新 socket，传完即 close
  │     └── [pool 开启]
  │           1) 锁池 -> cleanupIdleConnections() -> 找空闲存活连接 -> 命中则返回
  │           2) 未命中 -> 释放锁后 connect 新 socket（避免阻塞其他线程）
  │           3) 重新加锁 -> 二次检查（防止重复建连）-> 放入池子 -> 返回
  ├── new ClientSession(socket)
  │     on_finalize_ = 标记 slice 成功/失败
  │     on_complete_ = returnConnection(...) 或 close(...)
  └── session->initiate(...)   // 启动会话
```

注意第 2 步"释放锁后再 connect"是一个重要的并发优化：DNS 解析和 TCP 握手可能耗时几十到几百毫秒，若持锁等待会阻塞所有其他线程的 `getConnection`。

#### 4.3.3 源码精读

**连接池的数据结构**（声明在头文件）：

[PooledConnection 与连接池成员 (tcp_transport.h:44-59, 118-147)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.h#L44-L59) —— `PooledConnection` 持有 socket、host、port、`last_used` 时间戳、`in_use` 标志；第 135-138 行 `connection_pool_` 是 `unordered_map<ConnectionKey, deque<PooledConnection>>`，每个 (host,port) 对应一个 deque；第 147 行 `kConnectionIdleTimeout{60}` 是 60 秒空闲超时。

**构造函数读环境变量开关**：

[TcpTransport 构造函数 (tcp_transport.cpp:604-615)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp#L604-L615) —— 仅当 `MC_TCP_ENABLE_CONNECTION_POOL` 被设为非 `0/false/no` 时才 `enable_connection_pool_ = true`；头文件第 115-116 行默认值是 `false`。

**getConnection 的两阶段加锁**：

[getConnection (tcp_transport.cpp:835-935)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp#L835-L935) —— 第 838-854 行是 pool 关闭的快速路径；第 858-887 行第一阶段（持锁）做清理 + 找空闲连接；第 889-905 行**释放锁后** connect；第 907-934 行第二阶段重新加锁，二次检查避免重复建连（第 914-927 行若发现别的线程已加入可用连接，就关掉自己刚建的、复用别人的）。

**returnConnection**：

[returnConnection (tcp_transport.cpp:937-966)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp#L937-L966) —— 第 949-951 行：socket 仍 open 就置 `in_use=false` 并刷新 `last_used`；已断开就从池中删除。

**清理空闲连接**：

[cleanupIdleConnections (tcp_transport.cpp:968-999)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp#L968-L999) —— 遍历每个连接，`!in_use` 且空闲时长超过 `kConnectionIdleTimeout`（60s）就 close 并 erase。

**startTransfer 中如何把归还/关闭接到会话完成回调**：

[startTransfer 的 on_complete_ 分支 (tcp_transport.cpp:1057-1070)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp#L1057-L1070) —— pool 开启时 `on_complete_` 调 `returnConnection`；关闭时直接 `socket->close()`。

#### 4.3.4 代码实践：开关连接池并观察连接数

**实践目标**：直观感受连接池对"建连次数"的影响。

**操作步骤**：

1. 准备一个能在两节点间反复发起小消息 TCP 传输的测试（可基于 `mooncake-transfer-engine/tests` 下现有 TCP 测试，或 Store 的 TCP client）。
2. 场景 A：不设 `MC_TCP_ENABLE_CONNECTION_POOL`（池关闭）。用 `ss -tn state established '( sport = :<对端端口> )'` 或 `ss -tan | wc -l` 观察每轮传输前后连接的建立/关闭。
3. 场景 B：`export MC_TCP_ENABLE_CONNECTION_POOL=1`。连续发起多轮传输，观察已建立连接数是否稳定（不再每次新建）。
4. 停止传输后等待 >60 秒，再次观察连接是否被 `cleanupIdleConnections` 回收。

**需要观察的现象**：场景 A 每轮传输都出现新的 TIME_WAIT/连接建立痕迹；场景 B 连接数稳定在一组持久 ESTABLISHED 上，停传 60s 后消失。

**预期结果**：证明连接池确实复用了连接、空闲超时确实触发回收。**待本地验证**（具体连接数随并发量变化）。

#### 4.3.5 小练习与答案

**练习 1**：`getConnection` 为什么要在 connect 前后加锁两次，而不是一直持锁？

> **答案**：connect 涉及 DNS 解析和三次握手，可能耗时很长。一直持锁会让所有其他线程的 `getConnection` 阻塞。两阶段加锁让耗时的网络操作在锁外进行，只在访问共享 `connection_pool_` 时持锁，显著提高并发度。代价是可能多建一条连接，第二阶段的二次检查（第 914-927 行）负责消除这种重复。

**练习 2**：连接池默认关闭（`enable_connection_pool_ = false`）。在 pool 关闭时，一次传输结束 socket 怎么处理？

> **答案**：见 [tcp_transport.cpp:1062-1069](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp#L1062-L1069)，`on_complete_` 直接 `socket->close()`，每次传输一条新连接、用完即弃。

---

### 4.4 TCP-only 本机 memcpy 优化：isTcpOnly() 与策略抉择

#### 4.4.1 概念说明

前面三节讲的都是"跨节点用 TCP 搬数据"。但 Mooncake Store 的 client 经常会读**自己进程 mount 的 segment**（比如一个推理进程把自己本地 segment 上的 KVCache 读出来）。这时如果还走 TCP transport，数据路径是：

```text
源 buffer (本进程) → 用户态写 socket → 内核协议栈(loopback) → 内核读 socket → 目的 buffer (本进程)
```

绕了两次内核态切换，纯属浪费。直接 `memcpy` 一步到位：

```text
源 buffer → memcpy → 目的 buffer   (纯用户态，无系统调用)
```

但这个优化**有严格前提**：源和目的必须在**同一个进程**的地址空间。同一台机器上的两个不同进程虽然共享 IP，但虚拟地址空间不互通，对另一个进程的地址做 memcpy 会段错误。Mooncake 用 `isTcpOnly()` 来判断"当前是否值得开启这个优化"：

- 当**只装了 tcp transport**（没有 RDMA/NVLink 等）时，本机走 TCP loopback 的收益最低、memcpy 的相对收益最高 → 自动开启 memcpy；
- 当有 RDMA 等更高效传输时，本机两端之间可能本来就有更优路径，且全局行为更复杂 → 默认不自动开启（可由 `MC_STORE_MEMCPY` 手动覆盖）。

这个优化的代码不在 transfer engine 里，而在 **Mooncake Store 的 client 侧**（`transfer_task.cpp`），因为"要不要 memcpy"是存储层根据 replica 是否在本地来决策的。

#### 4.4.2 核心流程

判定与决策链路：

```text
TransferEngine::isTcpOnly()                       // 对外接口
  └── TransferEngineImpl::isTcpOnly()
        └── MultiTransport::isTcpOnly()
              └── return transport_map_.size()==1 && 只含 "tcp"
                    （见 multi_transport.cpp:512-514）

TransferSubmitter 构造时（store 侧）：
  MC_STORE_MEMCPY 未设？  --> memcpy_enabled_ = engine_.isTcpOnly()   // 自动判定
  MC_STORE_MEMCPY 已设？  --> 按用户值 true/false

每次提交传输 selectStrategy(handle, slices)：
  memcpy_enabled_ == false ? --> TRANSFER_ENGINE（走 TcpTransport）
  isLocalTransfer(handle) ?  --> LOCAL_MEMCPY（直接 memcpy）
                                └── isSameProcessEndpoint:
                                      要求 handle 的 transport endpoint
                                      与本进程 endpoint 完全相等
                                      （同 host 不同进程 → false，避免段错误）
  否则                       --> TRANSFER_ENGINE
```

注意 `isSameProcessEndpoint`（[transfer_task.cpp:1364-1388](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/transfer_task.cpp#L1364-L1388)）的注释明确指出：**同 host 不够，必须是同进程**——这正是本讲前置知识"直觉四"在源码里的体现。

#### 4.4.3 源码精读

**isTcpOnly 的判定**：

[MultiTransport::isTcpOnly (multi_transport.cpp:512-514)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/multi_transport.cpp#L512-L514) —— `transport_map_.size() == 1 && transport_map_.count("tcp") == 1`：当且仅当只安装了 tcp 这一个 transport 时返回 true。

对外声明与转发：

[isTcpOnly 声明 (transfer_engine.h:169-175)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_engine.h#L169-L175) —— 注释清楚说明了意图：TCP-only 时本机传输优先 memcpy 而非 TCP loopback。

[TransferEngine::isTcpOnly (transfer_engine.cpp:611-618)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine.cpp#L611-L618) —— 注意 TENT 模式下直接返回 `false`（新一代引擎自身已处理 loopback 拷贝，不需要这里自动开启）。

**Store 侧自动开启 memcpy**：

[TransferSubmitter 构造函数的 memcpy 自动判定 (transfer_task.cpp:917-944)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/transfer_task.cpp#L917-L944) —— 第 921-927 行：`MC_STORE_MEMCPY` 未设时 `memcpy_enabled_ = engine_.isTcpOnly()`，并打印"TCP-only environment, memcpy enabled"或"non-TCP transport available, memcpy disabled"。

**策略抉择**：

[selectStrategy (transfer_task.cpp:1317-1333)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/transfer_task.cpp#L1317-L1333) —— `memcpy_enabled_` 关 → 恒走 `TRANSFER_ENGINE`；开 → 看是否 `isLocalTransfer`。

[isLocalTransfer / isSameProcessEndpoint (transfer_task.cpp:1364-1394)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/transfer_task.cpp#L1364-L1394) —— 关键安全判定：只有当 handle 的 transport endpoint 与本进程 endpoint **完全相等**才返回 true；仅 IP 相同（同 host 不同进程）会被 VLOG 标记并拒绝 memcpy。

**真正的 memcpy 执行**：

[submitMemcpyOperation (transfer_task.cpp:1089-1130)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/transfer_task.cpp#L1089-L1130) —— 按 READ/WRITE 决定 `src`/`dest`，组装 `MemcpyTask` 投递到 worker pool。

[MemcpyWorkerPool::workerThread 的实际拷贝 (transfer_task.cpp:626-689)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/transfer_task.cpp#L626-L689) —— 第 660-661 行：两端都不在 GPU 时直接 `std::memcpy(op.dest, op.src, op.size)`；命中 GPU 指针时走 `gpu_staging::CopyAuto`。第 581 行注释说明只有 1 个 worker（memcpy 受限于内存带宽，多线程无益）。

**端到端测试**（实践依据）：

[P2PLocalReplicaUsesLocalMemcpy (client_tcp_local_memcpy_test.cpp:297-325)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/tests/client_tcp_local_memcpy_test.cpp#L297-L325) —— 本地 replica 的 Get 操作，断言策略为 `"LOCAL_MEMCPY"`（第 324 行）。

[P2PRemoteReplicaOnSameTcpHostUsesTransferEngine (client_tcp_local_memcpy_test.cpp:360-392)](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/tests/client_tcp_local_memcpy_test.cpp#L360-L392) —— 同一台 host 上**两个不同进程**（`127.0.0.1:18001` 与 `127.0.0.1:18002`）之间，断言策略为 `"TRANSFER_ENGINE"`（第 391 行），证明"同 host 不同进程"不会错误地走 memcpy。

#### 4.4.4 代码实践：运行测试并测量两种方式延迟

**实践目标**：亲眼看到"本机同进程 → memcpy，同机不同进程 → TCP"，并量化两者延迟差。

**操作步骤**：

1. 编译并运行现成的测试（这是最可靠的依据，无需自己搭环境）：

   ```bash
   # 在 build 目录下（CMake 配置时确保编译了 mooncake-store 的测试）
   ctest -R TcpLocalMemcpyAutoEnable --verbose
   ```

   关注日志里 `Using transfer strategy: LOCAL_MEMCPY`（本地 replica）与 `Using transfer strategy: TRANSFER_ENGINE`（同机不同进程）的分别出现，对应 [client_tcp_local_memcpy_test.cpp:324](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/tests/client_tcp_local_memcpy_test.cpp#L324) 与 [第 391 行](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/tests/client_tcp_local_memcpy_test.cpp#L391)。

2. 对照源码解释现象：
   - 本地 replica：`handle.transport_endpoint_ == 本进程 endpoint` → `isSameProcessEndpoint` true → `LOCAL_MEMCPY`。
   - 同机两进程：IP 都是 `127.0.0.1` 但 endpoint（含端口/进程标识）不同 → `isSameProcessEndpoint` false → `TRANSFER_ENGINE` → 走 `TcpTransport` 的 loopback。
3. **测量延迟差**（可选）：基于上述测试的 setup，在 `Get` 前后用 `std::chrono::steady_clock` 计时，分别测量"本地 replica（memcpy）"与"同机不同进程（TCP loopback）"各 N 次的平均单次延迟。为了强制对比，可在场景一中 `export MC_STORE_MEMCPY=0` 关掉 memcpy，让本机两端也走 TCP，再与开启时对比。

**需要观察的现象**：测试通过，日志出现两种策略；延迟上 memcpy 路径显著低于 TCP loopback 路径（小消息尤其明显，因为 TCP 路径含系统调用与协议栈开销）。

**预期结果**：memcpy 延迟 < TCP loopback 延迟。具体数值**待本地验证**——取决于消息大小、CPU 与内核版本；典型情况小消息（KB 级）memcpy 可比 loopback 快一个数量级。

> 说明：本实践优先复用仓库自带的 `client_tcp_local_memcpy_test.cpp`，它已经把"自动判定 + 策略捕获"做成了断言，是最权威的依据。延迟测量部分若不便实测，至少完成步骤 1-2 的源码对照。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `isSameProcessEndpoint` 要求"完整 endpoint 相等"而不是"IP 相等"？

> **答案**：memcpy 要求源和目的地址在**同一个虚拟地址空间**。同一台机器的两个进程共享 IP 但地址空间独立，对另一进程地址做 memcpy 会段错误。"完整 endpoint 相等"才能唯一确定是同一个进程（含端口等标识），从而安全地 memcpy。见 [transfer_task.cpp:1366-1371](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/transfer_task.cpp#L1366-L1371) 的注释。

**练习 2**：假设一个集群既有 RDMA 又装了 tcp（作为回退），`isTcpOnly()` 返回什么？`MC_STORE_MEMCPY` 未设时 memcpy 会自动开启吗？

> **答案**：`transport_map_` 里同时有 `"rdma"` 和 `"tcp"`，size==2，`isTcpOnly()` 返回 false；`MC_STORE_MEMCPY` 未设时 `memcpy_enabled_ = false`，本机两端默认走 `TRANSFER_ENGINE`（RDMA 体系下通常有更优的本机路径，且行为更可控）。用户仍可 `export MC_STORE_MEMCPY=1` 强制开启。

**练习 3**：MemcpyWorkerPool 为什么只用 1 个 worker 线程？

> **答案**：见 [transfer_task.cpp:580-581](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/transfer_task.cpp#L580-L581) 注释：memcpy 是**内存带宽受限**操作，多线程并行拷贝会争抢带宽，不仅不会更快反而增加缓存争用，因此单线程足够。

---

## 5. 综合实践：跑通 TCP-only 并解释本机传输为何不走 loopback

把本讲四个模块串起来，完成下面这个综合任务。

**任务背景**：你在一台**没有 RDMA 网卡**的机器上部署 Mooncake Store。一个推理进程把自己本地 segment 上的 KVCache 通过 Store API 反复读出。你需要解释"为什么这里的数据搬运没有经过 TCP"，并量化这种优化的收益。

**步骤**：

1. **确认 TCP-only**：启动 Store client 时观察日志。若 `MC_STORE_MEMCPY` 未设，应看到 `selectStrategy` 链路上的 INFO 日志——`MultiTransport::isTcpOnly()` 返回 true（因为只装了 tcp），`TransferSubmitter` 打印 `"TCP-only environment, memcpy enabled"`（[transfer_task.cpp:924-927](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/transfer_task.cpp#L924-L927)）。

2. **触发一次本机读**：用 Store API（参见 `u1-l4` Python 快速上手或 `mooncake-store/tests`）对一个位于**本地 segment** 的 object 做 `Get`。

3. **验证走的策略**：开启 `glog` 的 `VLOG(1)`（`export GLOG_v=1`），观察日志出现 `Using transfer strategy: LOCAL_MEMCPY`（[client_service.cpp:3023](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/client_service.cpp#L3023) 一带）。对照 4.4 节解释：因为 `handle` 的 endpoint 与本进程相等，`selectStrategy` 选择了 memcpy 而非 TcpTransport。

4. **对照实验——强制走 TCP loopback**：`export MC_STORE_MEMCPY=0` 后重跑同一次本机读。此时 `selectStrategy` 恒返回 `TRANSFER_ENGINE`（[transfer_task.cpp:1321-1325](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/transfer_task.cpp#L1321-L1325)），数据真的经过 `TcpTransport` 的 `ServerSession`/`ClientSession` 走 loopback。

5. **测延迟差**：对步骤 3（memcpy）与步骤 4（TCP loopback）分别计时 N 次（如 1000 次），报告平均延迟。

**交付物**：

- 一段话解释：TCP-only 时本机同进程两端为何走 memcpy 而非 TCP loopback（结合 `isTcpOnly`、`isSameProcessEndpoint`、内存带宽三个角度）；
- 一张延迟对比表（memcpy vs TCP loopback，不同消息大小如 4KB/64KB/1MB）；
- 说明 `MC_STORE_MEMCPY=0` 与默认值（自动）下的行为差异。

**预期结论**：TCP-only + 本地 replica → `LOCAL_MEMCPY`，延迟显著低于走 loopback 的 `TRANSFER_ENGINE`；这是 Mooncake 在无 RDMA 环境下保证本机访问性能的关键优化。延迟数值**待本地验证**。

> 备选（若不便搭 Store）：直接运行 `ctest -R TcpLocalMemcpyAutoEnable --verbose`，它已用断言固化了"本地→LOCAL_MEMCPY、同机不同进程→TRANSFER_ENGINE"的结论，可作为最小可信验证。

## 6. 本讲小结

- `TcpTransport` 是 Mooncake 的通用回退传输：只要有 TCP/IP 栈就能用，在无 RDMA 等硬件时由 `TransferEngineImpl::init` 自动安装（`installTransport("tcp")`）。
- 启动流程 `install` 完成"选端口 → 登记 segment(protocol=tcp) → 起 handshake daemon → 发布 metadata → 建 TcpContext 监听 → 起 worker 线程"，IPv6 双栈优先、`TCP_NODELAY` 关 Nagle。
- 收发基于 asio 协程式异步 I/O：`SessionHeader`（17B，含 size/addr/opcode）定边界，`ServerSession`/`ClientSession` 用 `async_read/async_write` 按 `MC_TCP_SLICE_SIZE`（默认 64KB）分块递归收发，一条连接可串行承载多个会话。
- 客户端**连接池**默认关闭（`MC_TCP_ENABLE_CONNECTION_POOL=1` 开启），按 (host,port) 复用 socket，两阶段加锁避免建连阻塞，60s 空闲自动回收。
- **TCP-only 本机 memcpy 优化**：当 `MultiTransport::isTcpOnly()` 为真（仅装了 tcp）时，Store 的 `TransferSubmitter` 自动开启 memcpy；本机**同进程**两端用 `LOCAL_MEMCPY`（直接 `std::memcpy`，单 worker）替代 TCP loopback，延迟更低；同 host 不同进程仍走 `TRANSFER_ENGINE` 以避免段错误。

## 7. 下一步学习建议

- **对比 RDMA transport**：建议阅读 `u3-l2`（或 RDMA 相关讲义）中 `RdmaTransport` 的实现，对比"协程式 async I/O"与"硬件完成队列 + worker pool"两种并发模型的差异，理解为什么 RDMA 在大块传输上远胜 TCP。
- **深入 metadata 与 handshake**：`startHandshakeDaemon` 与 `TransferMetadata` 决定了节点如何发现彼此的端口与 segment。可阅读 `transfer_metadata.cpp`、`transfer_metadata_plugin`，理解 etcd / P2P 两种元信息交换模式。
- **Store 的传输策略层**：本讲 4.4 节的 `TransferSubmitter` 还支持 NoF（NVMe-oF）、文件读等策略，建议继续阅读 `transfer_task.cpp` 全貌，理解 Store 如何在"内存 replica / 磁盘 replica / NoF"之间统一抽象。
- **新一代 TENT 引擎**：`isTcpOnly()` 在 TENT 模式下返回 false（[transfer_engine.cpp:612-615](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_engine.cpp#L612-L615)），因为 TENT 自身已处理 loopback。第 4 单元会专门讲解 TENT，届时可回看本讲对比两套实现。
