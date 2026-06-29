# 客户端连接：UdtConnection::connect 与 AsyncWrite

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `UdtConnection` 是什么、它和底层 `UdtSocket` 是什么关系，以及它为什么只是一个「薄壳（thin wrapper）」。
- 用 `UdtConnection::connect`（以及 `bind_and_connect`）写出一个能连上服务端的 UDT 客户端，并理解 `lookup_host` 的多地址解析行为。
- 看懂连接建立过程中那段「`loop { wait_for_connection() }`」等待握手完成的异步循环。
- 理解 `UdtConnection` 如何实现 `AsyncRead` / `AsyncWrite`，从而可以直接用 `tokio::io` 标准扩展（`AsyncReadExt` / `AsyncWriteExt`）里的 `read`、`write_all`、`flush`、`shutdown` 来收发数据。

本讲是**客户端视角**：我们从「主动去连别人」的 `UdtConnection` 入手，把建立连接和读写数据这条最常用的路径打通。服务端 `UdtListener` 的 `bind` / `accept` 留到下一讲（u2-l2）。

## 2. 前置知识

在进入源码前，先建立几个 Tokio 异步编程的基础概念。如果你已经熟悉，可以跳过本节。

- **`AsyncRead` / `AsyncWrite`**：Tokio 里异步「读」和「写」的两个核心 trait。凡是实现了它们的类型（比如 `tokio::net::TcpStream`、本讲的 `UdtConnection`），都能用 `AsyncReadExt::read` / `read_to_end` 和 `AsyncWriteExt::write_all` / `flush` / `shutdown` 这些**扩展方法**来收发字节。`UdtConnection` 让你能像用 `TcpStream` 一样用 UDT。
- **`Poll` 与 `Pending` / `Ready`**：异步函数底层是「轮询（poll）」。一次 `poll_xxx` 要么返回 `Poll::Ready(v)` 表示「这次就搞定了，结果是 `v`」，要么返回 `Poll::Pending` 表示「暂时没结果，等以后再叫我」。返回 `Pending` 时，被调用方通常要负责「在条件满足时唤醒调用方」。
- **`ToSocketAddrs` 与 `lookup_host`**：`"example.com:443"`、`(Ipv4Addr, u16)` 这类「地址描述」都实现了 `ToSocketAddrs` trait；`tokio::net::lookup_host` 会把它异步解析成一个 `SocketAddr` 迭代器——一个域名可能解析出多个 IP（既有 IPv4 又有 IPv6），所以要逐个尝试。
- **`Arc<T>`**：线程安全的引用计数指针，用于**共享所有权**。本讲里 `UdtConnection` 内部就持有一个 `Arc<UdtSocket>`（叫 `SocketRef`），所以连接对象可以被廉价地克隆、在多个任务间共享。
- **`Notify`**：Tokio 提供的「一次性通知」原语。一个任务 `notified().await` 阻塞等待，另一个任务调用 `notify_waiters()` 就能把它唤醒。它是本讲「连接完成」「有数据可读」等事件唤醒等待者的底层机制。
- **公共 API 边界**：回顾上一讲（u1-l4），`tokio_udt` 对外只导出 5 个类型，`UdtConnection` 是其中之一，分属「入口」组角色；其余模块都是私有的。

## 3. 本讲源码地图

本讲主要精读 `src/connection.rs`（这是 `UdtConnection` 的全部定义，只有约 167 行），并顺带涉及以下几个文件：

| 文件 | 作用 |
| --- | --- |
| [src/connection.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs) | `UdtConnection` 的定义：连接、收发、`AsyncRead`/`AsyncWrite` 实现都在这里。 |
| [src/lib.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/lib.rs) | crate 根，把 `UdtConnection` 通过 `pub use` 导出；README 风格的客户端示例也写在它的文档注释里。 |
| [src/socket.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs) | 底层 `UdtSocket`，`UdtConnection` 委托给它：`connect` / `send` / `recv` / `poll_recv` / `wait_for_connection` 等方法都在此。 |
| [src/udt.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs) | 全局单例 `Udt`，提供 `get()` / `new_socket()`；`SocketRef = Arc<UdtSocket>` 类型别名也定义在这里。 |

> 一句话定位：`UdtConnection` 是「面向用户的客户端句柄」，真正的协议逻辑都在它包着的那个 `UdtSocket`（及其背后的全局 `Udt` 引擎）里。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **`UdtConnection::connect`**：从地址解析到发出第一个握手包。
2. **`wait_for_connection`**：连接发起后，等待握手完成的异步轮询。
3. **`AsyncRead` / `AsyncWrite` 实现**：把底层同步 socket 桥接成标准异步 I/O。

### 4.1 模块一：`UdtConnection::connect`——从地址解析到发出握手

#### 4.1.1 概念说明

`UdtConnection` 在源码里结构极其简单，它只持有一个字段：

```rust
pub struct UdtConnection {
    socket: SocketRef,
}
```

参见 [connection.rs:10-12](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L10-L12)。这里的 `SocketRef` 是 [udt.rs:15](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L15) 定义的类型别名：

```rust
pub(crate) type SocketRef = Arc<UdtSocket>;
```

也就是说，**一个 `UdtConnection` 就是一个被 `Arc` 共享的 `UdtSocket` 的智能指针**。所有真正干活的方法（连接、发送、接收、关闭）都委托给这个底层 socket。`UdtConnection` 本身几乎不含逻辑，它存在的意义是：

- 提供一个「干净的用户 API」（构造函数 `connect` / `bind_and_connect`，以及 `AsyncRead`/`AsyncWrite`）；
- 把 `UdtSocket` 这种内部类型藏起来（`UdtSocket` 不是公共 API，见上一讲）。

`connect` 是客户端最常见的入口：传入一个地址（域名、`(IP, port)` 元组都可以）和一个可选的 `UdtConfiguration`（`None` 用默认值），它就帮你建好连接并返回一个 `UdtConnection`。

#### 4.1.2 核心流程

`connect` 实际委托给一个私有函数 `_bind_and_connect`，整体流程如下：

1. **创建底层 socket**：拿到全局单例 `Udt::get()` 的写锁，调用 `new_socket(Stream, config)` 创建一个流式 `UdtSocket`，再 `.clone()` 出一个 `Arc` 句柄（所有权从全局注册表共享出来）。
2. **解析地址**：用 `tokio::net::lookup_host` 把传入的地址描述异步解析成一组 `SocketAddr`。
3. **逐个尝试连接**：对每个解析出的地址调用 `socket.connect(addr, bind_addr)`，**第一个成功发出的就 `break`**；全部失败则把最后一个错误返回。
4. **等待握手完成**：进入一个 `loop`，反复 `wait_for_connection().await`，直到状态不再是 `Connecting`（变成 `Connected` 或 `Broken`）。
5. **返回连接**：用这个 socket 构造 `UdtConnection::new(socket)` 返回。

用伪代码表示：

```
fn connect(addr, config):
    socket = Udt::get().write().await.new_socket(Stream, config).clone()
    for a in lookup_host(addr).await?:       # 可能多个 IP
        if socket.connect(a, bind_addr).await.is_ok():
            break                              # 第一个成功就停
    loop:
        status = socket.wait_for_connection().await
        if status != Connecting: break         # 握手完成（或失败）
    return UdtConnection { socket }
```

> 注意：第 3 步的 `socket.connect` **只是发出了第一个握手包（`connection_type = 1`）并把状态置为 `Connecting`**，并不会当场完成握手。真正的握手往返是由接收 worker 在后台异步处理的，所以才有第 4 步的「等待」。握手的协议细节（SYN cookie 等）会在 u8-l1 详讲。

#### 4.1.3 源码精读

先看 `connect` 和 `bind_and_connect` 这两个公共入口，它们都只是把参数转给 `_bind_and_connect`：

[`connect` 与 `bind_and_connect`，connection.rs:19-32](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L19-L32) —— `connect` 不绑定本地地址（`bind_addr` 传 `None`），`bind_and_connect` 多接收一个 `bind_addr: SocketAddr`，让客户端可以**指定本机出口地址/端口**（比如多网卡环境下选哪张卡）。其余完全一样。

核心在 `_bind_and_connect`：

[_bind_and_connect 第 1 段：创建 socket，connection.rs:39-42](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L39-L42) —— 拿全局 `Udt::get()` 的写锁，`new_socket(SocketType::Stream, config)` 创建流式 socket，`.clone()` 复制 `Arc` 句柄。这里创建出来的是 `SocketType::Stream`（流式），对应 UDT 的可靠数据流服务；`Datagram` 模式（消息服务）不在本讲的公共 API 路径上。

> `Udt::get()` 返回的是一个进程级单例（`OnceCell<RwLock<Udt>>`），所有 socket 都注册在它内部。这一行的含义在 u3-l1 会详细展开，这里只需知道「创建 socket 必须经过全局引擎」即可。

[_bind_and_connect 第 2 段：逐地址尝试，connection.rs:44-63](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L44-L63) —— `lookup_host(addrs).await?` 把地址解析成迭代器；对每个 `addr` 调用 `socket.connect(addr, bind_addr).await`，成功就 `connected = true; break`，失败就把错误存进 `last_err` 继续下一个。如果全都连不上，用最后一个错误（或「无法解析地址」）返回 `Err`。这正是「一个域名可能解析出多个 IP，逐个尝试」的标准写法。

底层 `UdtSocket::connect` 做了什么？它要求当前状态必须是 `Init`，否则报错；然后：

[UdtSocket::connect，socket.rs:1098-1143](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1098-L1143) —— 这段做四件事：(1) `self.open()` 把状态从 `Init` 改成 `Opened`；(2) `udt.update_mux(self, bind_addr)` 为该 socket 获取/创建一个共享的 UDP 多路复用器（multiplexer），并按需绑定本地 UDP 端口；(3) 把状态置为 `Connecting`，记录对端地址；(4) 构造一个 `HandShakeInfo { connection_type: 1, ... }` 的握手控制包并通过 `send_to` 发给对端。注意这里 `connection_type: 1` 表示「我是发起连接的客户端」，握手协议会围绕这个值展开（详见 u8-l1）。返回后状态停留在 `Connecting`。

[_bind_and_connect 第 3 段：等待握手完成，connection.rs:65-72](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L65-L72) —— 发出握手包后进入 `loop`，反复 `socket.wait_for_connection().await`，只要它返回的状态仍然是 `Connecting` 就继续等；一旦状态变成 `Connected`（成功）或 `Broken`（失败），就 `break` 并返回 `UdtConnection::new(socket)`。

最后，[`UdtConnection::new`，connection.rs:14-17](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L14-L17) 是 `pub(crate)` 的私有构造函数（用户不能直接 `new`，只能通过 `connect` 拿到连接），它只是把 `SocketRef` 包进结构体。

#### 4.1.4 代码实践

**实践目标**：亲手写一个客户端，用 `UdtConnection::connect` 连上仓库自带的 `udt_receiver`，并用 `write_all` 发送数据，观察 receiver 打印出吞吐。

**操作步骤**：

1. 在仓库根目录的一个终端里先启动服务端（它 `bind` 到 `0.0.0.0:9000` 并循环 `accept`，每秒打印收到的 MB/s）：

   ```bash
   cargo run --bin udt_receiver
   ```

2. 在仓库根目录新建一个临时示例文件 `examples/u2_client.rs`（`examples/` 目录下的文件会被 Cargo 自动当成 example，文件名即名字）：

   ```rust
   // 示例代码：examples/u2_client.rs
   use std::net::Ipv4Addr;
   use tokio::io::AsyncWriteExt; // write_all 来自这个 trait
   use tokio_udt::UdtConnection;

   #[tokio::main]
   async fn main() -> std::io::Result<()> {
       // 连到本机 9000 端口的 UDT 服务端，None 表示用默认配置
       let mut connection =
           UdtConnection::connect((Ipv4Addr::LOCALHOST, 9000), None).await?;
       // 像写 TcpStream 一样 write_all 一段字节
       connection.write_all(b"Hello UDT!").await?;
       println!("已发送一条消息，按 Ctrl+C 退出");
       // 给 receiver 一点时间收包，然后退出
       connection.flush().await.ok();
       Ok(())
   }
   ```

3. 在另一个终端运行它：

   ```bash
   cargo run --example u2_client
   ```

**需要观察的现象**：

- 客户端打印「已发送一条消息」，正常退出（无 panic、无 `Err`）。
- `udt_receiver` 那一侧没有报错；如果你愿意把 `write_all` 放进 `loop` 持续发送（像 README 客户端那样），会看到 receiver 每秒打印的吞吐数字上升。

**预期结果**：连接建立成功，客户端能无错地 `write_all`。读取回显数据的能力需要服务端「把数据写回来」，`udt_receiver` 只收不发，所以**读回显请配合下一讲（u2-l2）自己写的 echo 服务端**，见本讲模块三的实践。

> 说明：本实践依赖 `examples/` 自动发现机制和 `cargo run --bin udt_receiver`，这些在 u1-l2 已验证可用；具体打印的吞吐数值取决于本机性能，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`connect` 和 `bind_and_connect` 在行为上唯一的差别是什么？为什么需要 `bind_and_connect`？

> **答案**：差别仅在本地绑定地址——`connect` 内部 `bind_addr` 传 `None`（由多路复用器自动选本地端口），`bind_and_connect` 允许调用方显式指定 `bind_addr: SocketAddr`。需要它的典型场景是：多网卡机器上想强制让连接从某一张特定网卡（某个本地 IP）出去，或者想固定本地源端口。

**练习 2**：`_bind_and_connect` 里 `lookup_host` 解析出多个地址时，全部连接失败会怎样？成功一个后剩余地址还会再试吗？

> **答案**：成功一个就 `connected = true; break`，**剩余地址不再尝试**；如果全部失败，返回 `last_err`（即最后一个地址的错误），若 `last_err` 是 `None`（极端情况：一个地址都没解析出来）则返回「could not resolve address」错误。参见 [connection.rs:44-63](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L44-L63)。

### 4.2 模块二：`wait_for_connection`——等待握手完成

#### 4.2.1 概念说明

上一模块提到：`socket.connect` 只是发出握手包并把状态置为 `Connecting`，握手真正完成（收到对端的握手应答、双方协商好参数）是后台接收 worker 异步完成的。于是客户端在 `connect` 返回前必须「等」一下，等状态从 `Connecting` 变成别的值。

这个「等」不能是死循环 `while status == Connecting {}` 忙等（会吃满 CPU），也不能是 `sleep` 轮询（既浪费又延迟）。正确做法是：**注册一个 `Notify` 通知，然后 `await` 它**——谁让状态变了，谁就来唤醒我。`wait_for_connection` 就是干这件事的。

#### 4.2.2 核心流程

`wait_for_connection` 的逻辑可以用一段「先取状态、决定是否等」的判别式概括：

```
fn wait_for_connection():
    取当前 status（持锁）
    if status != Connecting:
        return status            # 已经不忙了（Connected / Broken / ...），直接返回
    else:
        future = connect_notify.notified()   # 拿到一个"等待通知"的 future
    drop 锁
    future.await                  # 被唤醒（状态可能已变）
    return 当前 status            # 再读一次状态返回
```

关键点：**先在持锁期间判断状态、决定要不要等，再放锁去 `await`**。这样能避免「刚放锁、状态就变了、通知已经发出去、自己却错过」的竞态——因为 `Notify::notified()` 是在持锁期间创建的，而唤醒方 `notify_waiters()` 也是在改状态（持同一把 `status` 锁的语义下）之后调用的，时序上不会漏掉。

谁会唤醒它？后台处理完对端握手应答的代码会把状态置成 `Connected`（或失败时置成 `Broken`），然后调用 `connect_notify.notify_waiters()`。这套「改状态 + notify」的协作在 u3-l2 状态机一讲会更系统地讲。

#### 4.2.3 源码精读

[wait_for_connection，socket.rs:1223-1235](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1223-L1235) —— 注意它的写法：用一个 `if let Some(notified) = { ... }` 块，**块的返回值决定要不要等**。块内持 `status` 锁：若状态已不是 `Connecting` 返回 `None`（不等）；否则创建 `self.connect_notify.notified()` 返回 `Some(future)`。块结束后锁已释放，再对 `Some(future)` 做 `future.await`，最后返回最新的 `self.status()`。这正好实现上面「先取状态、决定是否等、再等」的模式。

回到 `UdtConnection` 一侧，[_bind_and_connect 的等待循环，connection.rs:65-70](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L65-L70) 正是调用它的地方：`loop { status = socket.wait_for_connection().await; if status != Connecting { break } }`。注意这里**外层套了 `loop`**——因为单次 `wait_for_connection` 被唤醒后状态理论上可能仍是 `Connecting`（比如收到的是中间过程的握手、还没最终敲定），所以要循环直到状态确实离开 `Connecting`。

[UdtStatus 枚举与 is_alive，socket.rs:1271-1287](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1271-L1287) —— 七种状态分别是 `Init`、`Opened`、`Listening`、`Connecting`、`Connected`、`Broken`、`Closing`、`Closed`。客户端这条路径经历的状态迁移是 `Init → Opened → Connecting → Connected`（成功）或 `... → Broken`（失败）。`is_alive()` 把 `Broken / Closing / Closed` 视为「已死」，其余视为「还活着」，这个判定在后面读写数据时的错误处理里会反复用到。

#### 4.2.4 代码实践

**实践目标**：通过阅读 + 加日志，直观体会「`connect` 返回后状态已经离开 `Connecting`」这件事。

**操作步骤**：

1. 阅读 [connection.rs:65-72](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L65-L72) 与 [socket.rs:1223-1235](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1223-L1235)，确认「等待循环」与「`Notify` 通知」的协作关系。
2. （可选，源码阅读型）在 `examples/u2_client.rs` 里，`connect` 返回后打印连接的 `socket_id` 与（如果能访问到的）状态线索：

   ```rust
   // 示例代码（接在 connect 之后）
   println!("已连接，本地 socket_id = {}", connection.socket_id());
   ```

   `socket_id()` 来自 [connection.rs:93-95](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L93-L95)，它返回底层 socket 的 id。虽然公共 API 不直接暴露 `status`，但你能看到 `connect().await` 成功返回本身，就说明 `wait_for_connection` 的循环已经退出了（状态离开了 `Connecting`）。

**需要观察的现象**：`connect().await` 返回 `Ok` 时，连接确实已建立（能立刻 `write_all`）；若对端不存在/拒绝，连接最终会进入 `Broken`（这一路径的完整判定在 u7-l3 的 EXP 定时器一讲）。

**预期结果**：正常情况 `Ok(UdtConnection)`；对端不可达时该 `await` 会一直挂起直到超时判定 `Broken`（**完整超时行为待本地验证 / 留待 u7-l3**）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `wait_for_connection` 不能写成 `while self.status() == Connecting {}`（忙等）？

> **答案**：那是**自旋忙等**——会持续占满一个 CPU 核做无意义的循环，既浪费电又拖累同核其他任务。正确做法是「无事可做时让出执行权、等事件来了再被唤醒」，这正是 `connect_notify.notified().await` 提供的：`await` 会让出当前任务，直到有人 `notify_waiters()` 才被调度回来。

**练习 2**：`_bind_and_connect` 里那个 `loop` 和 `wait_for_connection` 内部的 `await` 是两层等待，它们各自的作用是什么？

> **答案**：内层 `connect_notify.notified().await` 负责把当前任务挂起、等一次「状态可能变了」的通知（避免忙等）；外层 `loop` 负责「被唤醒后检查状态，若仍是 `Connecting` 就再等一次」，保证最终返回时状态确实离开了 `Connecting`。两者配合：内层管「怎么等」，外层管「等到什么时候算完」。

### 4.3 模块三：`AsyncRead` / `AsyncWrite`——把 socket 桥接到标准异步 I/O

#### 4.3.1 概念说明

`UdtConnection` 之所以好用，是因为它实现了 `AsyncRead` 和 `AsyncWrite`。这意味着你可以直接用 `tokio::io` 的扩展方法：

- `connection.write_all(b"...").await` —— 发送一段字节（来自 `AsyncWriteExt`）；
- `connection.read(&mut buf).await` —— 读一段字节到 `buf`（来自 `AsyncReadExt`）；
- `connection.flush().await` —— 等待发送缓冲排空；
- `connection.shutdown().await` —— 关闭连接。

底层 `UdtSocket` 本身有一组**同步的**方法（`send` / `recv` / `poll_recv` / `snd_buffer_is_empty`），它们返回普通的 `Result` 或 `Poll`，不是 `async`。`UdtConnection` 的活儿就是把这些同步方法「包装」成符合 `AsyncRead` / `AsyncWrite` trait 形状的 `poll_xxx` 方法。

这里有一个**很有意思的设计**：当一次 `poll` 返回 `Pending`（暂时没法完成）时，`UdtConnection` 没有走「把当前 waker 注册到某个等待列表里」的传统路子，而是 **`tokio::spawn` 一个独立任务去 `await` 对应的 `Notify`，等条件满足了再调用 `waker.wake()` 唤醒上层**。我们先把这套机制讲清楚，深入的取舍分析留到 u8-l3。

#### 4.3.2 核心流程

四个 `poll_xxx` 各自的流程：

- **`poll_write(buf)`**：调用 `socket.send(buf)`。
  - 成功 → `Ready(Ok(buf.len()))`；
  - 失败且是 `OutOfMemory`（发送缓冲满了）→ `spawn` 一个任务等 `wait_for_next_ack_or_empty_snd_buffer`，再 `wake`，返回 `Pending`；
  - 其他错误 → `Ready(Err(...))`。
- **`poll_read(buf)`**：调用 `socket.poll_recv(buf)`。
  - `Ready` → 透传结果；
  - `Pending` → `spawn` 一个任务等 `wait_for_data_to_read`，再 `wake`，返回 `Pending`。
- **`poll_flush`**：查 `snd_buffer_is_empty()`；空了 → `Ready(Ok(()))`；否则 `spawn` 等缓冲排空，`Pending`。
- **`poll_shutdown`**：若已 `Closed` 直接 `Ready(Ok(()))`；否则 `spawn` 一个任务去 `socket.close()`，完成后再 `wake`，返回 `Pending`。

公共模式可以概括为：

```
poll_xxx:
    尝试一次同步操作
    if 能立即出结果: return Ready(结果)
    else:                                       # 需要 Pending
        waker = cx.waker().clone()
        socket = self.socket.clone()            # Arc，可安全 move 进任务
        tokio::spawn(async move {
            socket.wait_for_<对应事件>().await   # 阻塞等待条件成立
            waker.wake()                        # 条件成立，唤醒上层重新 poll
        })
        return Pending
```

#### 4.3.3 源码精读

[poll_write，connection.rs:119-137](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L119-L137) —— 先 `socket.send(buf)`。注意它**成功时返回 `Ok(buf_len)` 而不是 `Ok(实际写入)`**：因为底层 `send` 要么把整段 `buf` 接收进发送缓冲（详见 `SndBuffer::add_message`，u5-l1 讲切片），要么报错，没有「部分写入」概念，所以直接返回请求的长度。当错误是 `ErrorKind::OutOfMemory`（发送缓冲满）时，进入 `spawn + wake` 的 Pending 分支，等的是 `wait_for_next_ack_or_empty_snd_buffer`——即「收到下一个 ACK（腾出窗口/缓冲）或发送缓冲变空」。

[poll_read，connection.rs:98-117](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L98-L117) —— 委托给 `socket.poll_recv(buf)`。`Ready` 直接透传；`Pending` 时 `spawn` 一个等 `wait_for_data_to_read` 的任务，再 `wake`。

[poll_flush，connection.rs:139-152](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L139-L152) —— `flush` 的语义是「确保之前 `write` 的数据都发出去/被确认」。这里用 `snd_buffer_is_empty()` 判断发送缓冲是否已清空：空了就算 flush 完成；否则 `spawn` 等缓冲排空。这告诉我们：**UDT 的 `flush` 等待的是发送缓冲里待发数据被搬空，而不是「对端已收到」的端到端确认**（后者更接近「可靠交付保证」，UDT 在此并未额外提供）。

[poll_shutdown，connection.rs:154-166](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L154-L166) —— 已 `Closed` 直接返回；否则 `spawn` 一个调用 `socket.close()` 的任务（关闭会发送 `Shutdown` 包、迁移状态到 `Closing`，详见 u8-l2），完成后再 `wake`。注意 `close()` 是 `async`，所以在独立任务里 `await` 它。

底层那几个被等待的方法，本质都是「持锁检查条件，条件不满足就 `Notify::notified().await`」：

- [wait_for_data_to_read，socket.rs:1205-1221](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1205-L1221) —— 若状态已不 alive 直接返回（不等），否则若已有数据可读直接返回，否则等 `rcv_notify`。
- [wait_for_next_ack_or_empty_snd_buffer，socket.rs:1237-1248](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1237-L1248) —— 若发送缓冲已空直接返回，否则等 `ack_notify`。

> 关于 `spawn + wake` 这个模式的优缺点（相比把 waker 注册进 socket 内部）——例如每次 `Pending` 都会创建一个新任务的潜在开销——属于较深入的异步运行时话题，留到 u8-l3 集中讨论。本讲只需理解它能正确实现 `AsyncRead`/`AsyncWrite` 的语义即可。

#### 4.3.4 代码实践

**实践目标**：完整体验「发送 + 读回显」的客户端闭环。本实践需要配合**下一讲（u2-l2）**实现的 echo 服务端，因此现在先写好客户端，下一讲写好服务端后即可联调。

**操作步骤**：

1. 把 `examples/u2_client.rs` 扩展为「发一段数据，再读回显」的版本（注意要 `use` 两个扩展 trait）：

   ```rust
   // 示例代码：examples/u2_client.rs（echo 版）
   use std::net::Ipv4Addr;
   use tokio::io::{AsyncReadExt, AsyncWriteExt}; // read / write_all 都来自扩展 trait
   use tokio_udt::UdtConnection;

   #[tokio::main]
   async fn main() -> std::io::Result<()> {
       let mut connection =
           UdtConnection::connect((Ipv4Addr::LOCALHOST, 9000), None).await?;

       let out = b"ping";
       connection.write_all(out).await?;     // 写
       connection.flush().await?;            // flush 见上文语义

       let mut buf = [0u8; 4];
       let n = connection.read(&mut buf).await?;   // 读回显
       println!("收到 {} 字节: {:?}", n, &buf[..n]);
       assert_eq!(&buf[..n], out);

       connection.shutdown().await.ok();
       Ok(())
   }
   ```

2. 下一讲（u2-l2）你会写一个把读到的数据原样写回的 echo listener。写好后，先启动 echo 服务端，再 `cargo run --example u2_client`。

**需要观察的现象**：客户端打印「收到 4 字节: [112, 105, 110, 103]」（即 `ping` 的 ASCII），断言通过。这说明 `write_all` → 对端 echo → `read` 这条双向通路打通，`AsyncRead`/`AsyncWrite` 都工作正常。

**预期结果**：读到与发送完全相同的字节。若服务端不是 echo（比如直接用 `udt_receiver` 只收不发），`read` 会一直 `Pending`（`poll_read` 内部 `spawn` 的等待任务不会结束），表现为客户端卡在 `read().await`——这恰好验证了「无数据时 `poll_read` 返回 `Pending`」的行为。**完整联调结果待 u2-l2 完成后本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：`poll_write` 在底层 `send` 返回 `OutOfMemory` 时返回 `Pending`。请解释「为什么会 `OutOfMemory`」以及「等的是什么」。

> **答案**：UDT 发送受**拥塞窗口**和**发送缓冲容量**双重限制（详见 u6-l1）。当待发数据超过允许的「在途」量，`SndBuffer::add_message` 会返回 `OutOfMemory`（这里指发送缓冲/窗口已满，不是内存不够）。`poll_write` 此时返回 `Pending`，并 `spawn` 一个任务等 `wait_for_next_ack_or_empty_snd_buffer`——即等收到下一个 ACK（对端确认了数据、窗口前移、缓冲腾出空间）后再唤醒上层重试写入。参见 [connection.rs:124-133](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L124-L133)。

**练习 2**：`UdtConnection::poll_write` 成功时返回 `Ok(buf.len())`（请求长度），而不是某个「实际写入长度」。这隐含了 UDT 发送的什么特性？如果你一次 `write_all` 一段 10 MB 的数据，会发生什么？

> **答案**：隐含 UDT 流式 `send` 是「**整段接收进发送缓冲**」，没有部分写入——要么全收、要么（缓冲满时）报错让上层等。一次 `write_all` 10 MB 时，数据会被 `SndBuffer::add_message` **按 payload 切成很多个数据包**（u5-l1 讲切片），所以「一次 write」对应「很多个 UDT 数据包」。`write_all` 内部会循环调用 `poll_write` 直到全部写完，遇到 `Pending` 就正常挂起等待。

**练习 3**：`poll_flush` 用 `snd_buffer_is_empty()` 判断是否完成。结合这一定义，UDT 的 `flush` 保证的是「数据已到达对端」吗？

> **答案**：**不是**。`snd_buffer_is_empty()` 只表示发送缓冲里「待发/待重传」的数据已被发送队列搬空（发出去了），并不保证对端已确认收到。UDT 的可靠性（ACK/重传）在更底层运作（u6 单元），`flush` 在此只是「发完」，不等价于 TCP 语义上的端到端交付保证。

## 5. 综合实践

把本讲三个模块串起来：写一个**会发也会收的 UDT 客户端**，并把它的行为对应到本讲讲过的源码点。

任务：

1. 创建 `examples/u2_client.rs`，实现：用 `UdtConnection::connect` 连接 → `write_all` 发送 `b"hello"` → `flush` → `read` 读回显 → `shutdown`。（代码可直接用 4.3.4 给出的版本。）
2. 在代码每个关键步骤旁，**用注释标注它对应 `connection.rs` 的哪个方法、底层 `socket.rs` 的哪个方法**。例如：

   ```rust
   // connect → _bind_and_connect (connection.rs:34) → UdtSocket::connect (socket.rs:1098)
   //         → wait_for_connection (socket.rs:1223) 的等待循环
   let mut connection = UdtConnection::connect((Ipv4Addr::LOCALHOST, 9000), None).await?;

   // write_all → poll_write (connection.rs:119) → UdtSocket::send (socket.rs:984)
   connection.write_all(b"hello").await?;
   ```

3. 联调：配合下一讲（u2-l2）的 echo 服务端运行，确认能收到 `hello` 回显。
4. 进阶观察：在 `write_all` 之后、`read` 之前，试着 `println!` 打印 `connection.socket_id()`，确认连接已建立（`socket_id` 是个 `u32`，来自 [connection.rs:93-95](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L93-L95)）。

通过这个练习，你会亲手把「`connect` 建连 → `wait_for_connection` 等握手 → `AsyncRead`/`AsyncWrite` 收发」这条客户端主路径跑通，并能把每一步对应回源码。

## 6. 本讲小结

- `UdtConnection` 是一个**薄壳**：内部只有一个 `SocketRef`（即 `Arc<UdtSocket>`），所有逻辑都委托给底层 socket。它存在的目的是提供干净的用户 API 和实现 `AsyncRead`/`AsyncWrite`。
- `connect` / `bind_and_connect` 的核心是 `_bind_and_connect`：经全局 `Udt` 引擎 `new_socket` 建底层 socket → `lookup_host` 解析多地址 → 逐个 `socket.connect` 尝试（第一个成功即止）→ `loop wait_for_connection` 等握手完成。
- `UdtSocket::connect` 只发出第一个握手包（`connection_type = 1`）并把状态置为 `Connecting`，握手往返在后台异步完成。
- `wait_for_connection` 用「持锁判状态 + `connect_notify.notified().await`」避免忙等，外层 `loop` 保证返回时状态确实离开了 `Connecting`。
- `UdtConnection` 通过 `poll_write`/`poll_read`/`poll_flush`/`poll_shutdown` 实现 `AsyncRead`/`AsyncWrite`；遇到 `Pending` 时统一采用「`spawn` 一个任务等 `Notify`、条件满足后 `waker.wake()`」的模式。
- 因此你可以**像用 `TcpStream` 一样**用 `read` / `write_all` / `flush` / `shutdown` 操作 UDT 连接；`flush` 只保证「发完」（发送缓冲清空），不等价于「对端已确认」。

## 7. 下一步学习建议

- **紧接着学 u2-l2（服务端监听：`UdtListener::bind` 与 `accept`）**：把本讲客户端缺的 echo 服务端补上，完成「客户端 ↔ 服务端」的双向联调。两讲配合后，你能跑通一个完整的 UDT 收发闭环。
- **想理解连接建立的协议细节**：本讲只讲到「发出握手包、等状态变 `Connected`」。完整的握手往返、SYN cookie 防伪造、MSS 协商，在 **u8-l1（连接建立与握手：SYN cookie）**。
- **想理解收发背后的可靠性与窗口**：本讲 `poll_write` 遇到的 `OutOfMemory`、`flush` 的发送缓冲清空，其底层机制（发送队列调度、发送缓冲切片、拥塞窗口）在 **第 5、6 单元（数据通路、可靠性与发送主流程）**。
- **想深究 `spawn + wake` 模式**：本讲只介绍了它能正确工作；这种实现相对「把 waker 注册进 socket」的优点与开销，在 **u8-l3（异步桥接：AsyncRead/AsyncWrite 与 Notify）** 集中分析。
