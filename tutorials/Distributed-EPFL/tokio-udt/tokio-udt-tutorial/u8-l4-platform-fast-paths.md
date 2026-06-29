# 平台快路径：sendmmsg / recvmmsg / timerfd

## 1. 本讲目标

本讲是「连接生命周期与平台优化」单元的最后一篇，也是全系列最后一篇技术讲义。前面我们已经把 UDT 的协议格式（u4）、数据通路（u5）、可靠性（u6）、拥塞控制（u7）和连接生命周期（u8 前三讲）都拆解完了。本讲收尾，回答一个工程性很强的问题：**在 UDT 这样的高速协议里，收发包的「最后一公里」——系统调用与定时器——是怎么榨干 Linux 性能的？**

学完本讲你应当能够：

1. 说清 `sendmmsg` / `recvmmsg` 相比「一个个 `send_to` / `recv_from`」省下了什么，以及它们在代码里通过哪两个函数被调用。
2. 读懂 tokio-udt 在 Linux 下用 `nix` 直接操作 raw fd（绕过 tokio 高层封装）的写法，以及它如何用 `cfg(target_os = "linux")` 提供「Linux 快路径 + 非 Linux 回退」两套实现。
3. 解释为什么发送调度不能用 `tokio::time::sleep`，而 Linux 下要换成 `tokio-timerfd`（timerfd）做高精度睡眠。
4. 理解收包缓冲为什么是 `mss * 100`（一次最多收 100 个包）这个设计选择。

---

## 2. 前置知识

进入本讲前，请确认你理解以下概念（前序讲义已建立）：

- **一个 multiplexer 持有一个 UDP socket、跑两个 worker**（u3-l3）：接收 worker 收包后按包头 `dest_socket_id` 分发；发送 worker 按发送时刻调度，并 spawn 一个子任务真正发包。本讲的快路径全部挂在这两个 worker 上。
- **接收 worker 的主循环**（u5-l2）：`receive_packets → 反序列化 → 按 socket_id 分发 → check_timers`，收包缓冲是 `vec![0u8; mss * 100]`。本讲要拆开「`receive_packets` 这一步在 Linux 上到底干了什么」。
- **`pkt_send_period` 约为微秒级**（u1-l2、u7-l2）：UDT 的发送周期初始约 1µs，在高速链路上两个相邻数据包的间隔是微秒量级。这是本讲「为什么需要高精度定时器」的出发点。
- **Rust 条件编译**：`#[cfg(target_os = "linux")]` 标注的项只在 Linux 上编译；`#[cfg(not(target_os = "linux"))]` 标注的项在其它平台（macOS、Windows 等）编译。本讲几乎每个函数都有这样一对「双胞胎」实现。

还需要补三个本讲专用的术语：

- **系统调用（syscall）**：用户态程序请求内核做事（比如发一个网络包）的入口。每次系统调用都有「陷入内核再返回」的固定开销（上下文切换），通常在几百纳秒到几微秒。包发得越密，这个开销占比越高。
- **raw fd（原始文件描述符）**：Linux/Unix 把一切资源（socket、timer、文件）都抽象成一个整数句柄，即 fd。`nix` 是 Rust 对 POSIX 系统调用的薄封装，能用 fd 直接调系统调用，比 tokio/std 的高层封装更贴近内核。
- **timerfd**：Linux 特有的机制——把「一个内核定时器」表示成一个可读的 fd。到点时 fd 变成可读，从而能无缝挂进 epoll 事件循环，和 socket 用同一套机制等待。

---

## 3. 本讲源码地图

本讲围绕三个「双实现」函数，分布在两个核心文件，外加一个依赖清单：

| 文件 | 角色 | 本讲用到什么 |
| --- | --- | --- |
| [src/multiplexer.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs) | multiplexer 全部实现 | `send_mmsg_to` 的 Linux / 非 Linux 双实现（批量发送） |
| [src/queue/rcv_queue.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs) | 接收分发队列 | `receive_packets` 的 Linux / 非 Linux 双实现（批量接收），以及 `sleep` 的双实现 |
| [src/queue/snd_queue.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs) | 发送调度队列 | `sleep_until` 的 Linux（timerfd）/ 非 Linux 双实现 |
| [src/socket.rs](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs) | `UdtSocket` | `send_data_packets`：唯一调用 `send_mmsg_to` 的地方 |
| [Cargo.toml](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/Cargo.toml) | 依赖清单 | `nix`、`socket2` 全平台依赖；`tokio-timerfd` 仅 Linux 依赖 |

一句话概括：三个收发/定时操作，每个都有「Linux 原生系统调用」和「通用回退」两套实现，靠 `cfg` 在编译期二选一。

---

## 4. 核心概念与源码讲解

本讲拆为三个最小模块，正好对应规格要求的三个主题：

- **4.1** sendmmsg / recvmmsg：一次系统调用收发多个数据报
- **4.2** nix 操作 raw fd：直连内核与「双实现」桥接模式
- **4.3** timerfd sleep_until：Linux 高精度发送调度

### 4.1 sendmmsg / recvmmsg：一次系统调用收发多个数据报

#### 4.1.1 概念说明

UDT 在高速链路上的目标是每秒发几十万到上百万个 UDP 包。最朴素的写法是「每个包一次 `send_to` 系统调用」，但这样系统调用本身的开销会和有效载荷争抢 CPU。Linux 提供了两个批量系统调用来解决这个问题：

- **`sendmmsg`**（send multiple messages）：一次系统调用把**多个**数据报发到（通常是同一个）目的地。
- **`recvmmsg`**（receive multiple messages）：一次系统调用**一口气接收多个**数据报。

把「N 次系统调用」压成「1 次系统调用」，省下的是 N−1 次用户态↔内核态切换。在包密集的场景下，这是数量级的吞吐提升。这正是 tokio-udt 在 Linux 上选择 `sendmmsg` / `recvmmsg` 的根本原因。

需要强调一点：标准库和 tokio 的 `UdpSocket` 并没有暴露 `sendmmsg` / `recvmmsg`，它们只提供单包的 `send_to` / `recv_from`。要用批量系统调用，只能绕过这些高层封装，直接拿底层的 fd 调系统调用——这就是下一节（4.2）`nix` 登场的原因。

#### 4.1.2 核心流程

**发送侧（批量发）**：`UdtSocket::send_data_packets` 拿到一批数据包后，并不逐个发，而是交给 multiplexer 的 `send_mmsg_to` 一次发完：

```text
UdtSocket::send_data_packets(packets: Vec<UdtDataPacket>)
   └─► mux.send_mmsg_to(addr, packets.into_iter())
          │  Linux:   收集成 Vec<字节> → 构造 SendMmsgData 数组 → sendmmsg 一次发完
          └─ 非 Linux: for 每个包 { channel.send_to(包, addr) }   // 退化为逐包
```

**接收侧（批量收）**：接收 worker 分配一块 `mss * 100` 的大缓冲，调用 `receive_packets` 一次最多收回 100 个包：

```text
worker 主循环
   buf = vec![0u8; mss * 100]            // 复用的大缓冲
   loop {
     msgs = receive_packets(&mut buf)
       │  Linux:   把 buf 切成 100 个 mss 片 → recvmmsg 一次收 → 还原每个包的源地址
       └─ 非 Linux: for 每个 mss 片 { try_recv_from } // 退化为逐包，遇到 WouldBlock 即停
     若 msgs 为空：select { sleep(30µs) | readable() } 再试
     否则：逐包反序列化 → 按 dest_socket_id 分发
   }
```

#### 4.1.3 源码精读

**发送批量入口：`send_data_packets` 唯一调用 `send_m_msg_to` 的地方**。`UdtSocket` 攒好一批数据包后，取对端地址，把包迭代器交给 multiplexer（注意这里只发数据包，单包的控制包走另一条 `send_to`）：

[src/socket.rs:774-782](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L774-L782) —— `send_data_packets` 把 `Vec<UdtDataPacket>` 转成迭代器交给 `send_mmsg_to`。

**Linux 批量发送：`sendmmsg`**。把每个包先序列化成字节，再为每个字节缓冲构造一个 `SendMmsgData`（含目标地址 `addr`），最后一次 `sendmmsg` 发完；返回值是把每个报文实际写入的字节数累加起来：

[src/multiplexer.rs:106-145](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L106-L145) —— Linux 下 `send_mmsg_to`：`sendmmsg(sock_fd, &buffers, MSG_DONTWAIT)`，把多个数据报一次发出。

关键几行解读：

- `let data: Vec<_> = packets.map(|p| p.serialize()).collect();`（L116）：先把每个包序列化成字节向量。
- `buffers: Vec<SendMmsgData<...>>`（L118-L126）：为每个包准备一个 `SendMmsgData`，其 `iov`（I/O 向量）指向该包字节，`addr` 指向同一个目标地址。
- `sendmmsg(sock_fd, &buffers, MsgFlags::MSG_DONTWAIT)`（L132）：真正的一次性批量发送。
- `.into_iter().sum()`（L139-L140）：`sendmmsg` 返回「每个报文写了多少字节」的列表，这里求和得到总字节数。

**非 Linux 回退：逐包 `send_to`**。把 `sendmmsg` 退化成一个循环，语义对齐（返回总字节数），但失去批处理收益：

[src/multiplexer.rs:147-159](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L147-L159) —— 非 Linux 下 `send_mmsg_to`：`for data in packets { channel.send_to(&data, addr) }`。

**Linux 批量接收：`recvmmsg`**。先把大缓冲切成 100 个 `mss` 大小的片，每片装一个收到的包；一次 `recvmmsg` 填充多个片，并附带每个包的源地址：

[src/queue/rcv_queue.rs:73-119](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L73-L119) —— Linux 下 `receive_packets`：`recvmmsg(fd, &mut recv_mesg_data, MSG_DONTWAIT, None)`，并把每个报文的 `SockaddrStorage` 还原成 `SocketAddr`。

关键几行解读：

- `let bufs = buf.chunks_exact_mut(self.mss as usize);`（L81）：把传入的大缓冲切成固定 `mss` 大小的可变切片。
- `recv_mesg_data: Vec<RecvMmsgData<_>>`（L82-L87）：每片对应一个 `RecvMmsgData`，其 `iov` 指向该片。
- `recvmmsg(self.channel.as_raw_fd(), &mut recv_mesg_data, ...)`（L90-L95）：一次性接收，最多填满所有片。
- 后续 `.map(|msg| { ... 还原 v4/v6 地址 ...; (msg.bytes, socket_addr) })`（L102-L116）：把内核返回的每个报文（字节数 + 源地址）整理成 `(usize, SocketAddr)`。

**非 Linux 回退：逐包 `try_recv_from`**。逐片尝试接收，遇到 `WouldBlock` 就提前结束（说明内核缓冲已经空了）：

[src/queue/rcv_queue.rs:121-135](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L121-L135) —— 非 Linux 下 `receive_packets`：循环 `try_recv_from`，`WouldBlock` 即 `break`。

**为什么是 100 个包？** 接收 worker 的缓冲分配写在主循环开头：

[src/queue/rcv_queue.rs:137-138](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L137-L138) —— `let mut buf = vec![0_u8; self.mss as usize * 100];`

这块缓冲在循环外只分配一次、之后反复复用，大小固定为 `mss * 100`（默认 `mss=1500` 时约 146 KB）。`chunks_exact_mut(mss)` 会把它切成正好 100 片，所以一次 `recvmmsg` 最多收 100 个包。100 是「批处理收益」与「单次系统调用耗时/内存占用」之间的折中——再大收益递减，再小批处理优势体现不出来。

#### 4.1.4 代码实践

**实践目标**：用对比的方式理解「批量」与「逐包」在调用结构上的差异。

**操作步骤**（源码阅读型实践）：

1. 打开 [src/multiplexer.rs:106-159](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L106-L159)，把 Linux 版与非 Linux 版的 `send_m_msg_to` 并排看。
2. 数一下：要发 N 个包，Linux 版调用 `sendmmsg` 几次？非 Linux 版调用 `send_to` 几次？
3. 打开 [src/queue/rcv_queue.rs:73-135](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L73-L135)，做同样的对比（`recvmmsg` vs `try_recv_from`）。
4. 在 [src/queue/rcv_queue.rs:138](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L138) 处确认缓冲大小，计算：`mss=1500` 时一次 `recvmmsg` 最多收多少个包、缓冲多大。

**需要观察的现象 / 预期结果**：

- 发 N 个包：Linux 版 1 次系统调用（`sendmmsg`）；非 Linux 版 N 次（`send_to`）。
- 收包：Linux 版一次最多 100 个（受 `mss*100` 缓冲限制）；非 Linux 版逐个收直到 `WouldBlock`。
- `mss=1500` → 缓冲 150000 字节（≈146 KB），最多 100 包。

> 说明：本实践的结论可由静态阅读直接得出，无需运行。若想定量感受吞吐差异，可在 Linux 与 macOS 上分别跑 `cargo run --release --bin udt_sender`（u1-l2）对比吞吐，但具体数值「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：发送侧的 `send_m_msg_to` 在 Linux 版里先把每个包 `serialize()` 成字节再批量发。为什么不能直接把 `UdtPacket` 对象的引用交给 `sendmmsg`？

**参考答案**：`sendmmsg` 是内核系统调用，只认连续的原始字节（通过 `iov` / `IoSlice` 指向字节缓冲），不认识 Rust 的 `UdtPacket` 类型。必须先把每个包序列化成线上格式的字节流，内核才能按 UDT 协议把正确字节发出去。

**练习 2**：非 Linux 的 `receive_packets` 遇到 `WouldBlock` 就 `break` 提前结束循环；而 Linux 的 `recvmmsg` 一次调用就返回。二者在「能收几个包」上的上限分别由什么决定？

**参考答案**：非 Linux 版上限是「内核接收缓冲里当前有几个包」（收一个少一个，空了就 `WouldBlock`）；Linux 版上限是「`min(内核缓冲里的包数, 100)`」——因为缓冲只有 100 片，`recvmmsg` 一次最多填满 100 片，多余的包留到下一轮循环再收。

---

### 4.2 nix 操作 raw fd：直连内核与「双实现」桥接模式

#### 4.2.1 概念说明

上一节我们说要用 `sendmmsg` / `recvmmsg`，但 tokio 的 `UdpSocket` 没有这两个方法。怎么办？答案是**绕过 tokio，直接拿底层 fd 调系统调用**。tokio-udt 用 [`nix`](https://docs.rs/nix) 这个 crate 来做这件事——它是 POSIX 系统调用的薄封装，能让你用 Rust 安全地写出「`sendmmsg(fd, ...)`」这样的代码。

这里有两个要点：

1. **拿 fd**：tokio 的 `UdpSocket` 暴露了 `as_raw_fd()`（来自 `std::os::unix::io::AsRawFd`），返回底层整数的 fd。把这个 fd 交给 `nix::sys::socket::sendmmsg` / `recvmmsg`，就能对该 socket 发起系统调用。
2. **与 tokio 事件循环协作**：直接调系统调用是「同步」的，可能阻塞。tokio-udt 用 `UdpSocket::try_io(Interest::READABLE|WRITABLE, closure)` 这个桥：它先确保 epoll 注册了对应兴趣，然后在闭包里执行非阻塞的系统调用；若返回 `EWOULDBLOCK`，`try_io` 会把它转成 `WouldBlock` 错误，提示调用方「现在没准备好，等会儿再来」。

这套机制只在 Unix/Linux 上成立（fd、`as_raw_fd` 都是 Unix 概念）。因此整个设计自然落到「Linux 快路径 + 非 Linux 回退」的双实现模式：Linux 用 `nix` 直连内核；其它平台用 tokio 原生的 `send_to` / `try_recv_from`，虽然慢一点但能跑。

#### 4.2.2 核心流程

「双实现」在代码里是一个固定套路：同一个函数名，两个 `cfg` 版本，签名与返回类型完全一致。编译器根据目标平台只编入其中一个：

```text
#[cfg(target_os = "linux")]      //  只在 Linux 编译
fn receive_packets(...) -> Result<Vec<(usize, SocketAddr)>> { recvmmsg via nix }

#[cfg(not(target_os = "linux"))] //  只在非 Linux 编译
fn receive_packets(...) -> Result<Vec<(usize, SocketAddr)>> { try_recv_from 循环 }
```

调用方（worker）完全不用关心平台差异，统一 `self.receive_packets(&mut buf)` 即可。本讲涉及三个这样的双实现函数：

| 函数 | 所在文件 | Linux 实现 | 非 Linux 实现 |
| --- | --- | --- | --- |
| `send_m_msg_to` | multiplexer.rs | `sendmmsg`（4.1 已讲） | `send_to` 循环 |
| `receive_packets` | rcv_queue.rs | `recvmmsg`（4.1 已讲） | `try_recv_from` 循环 |
| `sleep_until` | snd_queue.rs | `tokio_timerfd::Delay`（4.3 讲） | `tokio::time::sleep_until` |

还有一个细节差异值得记住：`send_m_msg_to` 是 `async fn`（它先 `writable().await` 等待可写再发包），而 `receive_packets` 是普通同步 `fn`（它直接 `try_io` 试收，收不到就返回空，由 worker 外层去 `select` 等待）。

#### 4.2.3 源码精读

**拿 fd 并调系统调用**。发送与接收都用同一套「`as_raw_fd()` 拿 fd → 交给 nix 系统调用」的模式：

[src/multiplexer.rs:112-114](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L112-L114) —— 发送侧导入 `sendmmsg` 与 `AsRawFd`，准备用 fd 直发。

[src/multiplexer.rs:128-143](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L128-L143) —— `writable().await?` 等待可写后，`try_io(WRITABLE, || { let sock_fd = self.channel.as_raw_fd(); sendmmsg(sock_fd, ...) })`，用 fd 直发并把 `EWOULDBLOCK` 转成 `WouldBlock`。

[src/queue/rcv_queue.rs:75-80](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L75-L80) —— 接收侧同样导入 `recvmmsg` 与 `AsRawFd`。

[src/queue/rcv_queue.rs:89-101](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L89-L101) —— `try_io(READABLE, || recvmmsg(self.channel.as_raw_fd(), ...))`，把 `EWOULDBLOCK` 转成 `WouldBlock`。

**EWOULDBLOCK 处理是关键**。`sendmmsg` / `recvmmsg` 用了 `MSG_DONTWAIT` 标志，是非阻塞的：内核缓冲满（发送）或空（接收）时不阻塞，而返回 `EWOULDBLOCK`。代码把它统一映射成 tokio 的 `WouldBlock` 错误种类，让上层用 epoll 事件来等待，而不是傻等：

[src/multiplexer.rs:133-138](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L133-L138) —— 发送侧把 `EWOULDBLOCK` 转成 `WouldBlock`。

[src/queue/rcv_queue.rs:96-101](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L96-L101) —— 接收侧同样处理。

**非 Linux 回退不需要 nix**。回退版直接用 tokio 的 `UdpSocket::send_to` / `try_recv_from`，靠 `ErrorKind::WouldBlock` 判停：

[src/queue/rcv_queue.rs:126-132](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L126-L132) —— 回退版用 `try_recv_from`，`WouldBlock` 即 `break`。

**依赖清单的对应**。`nix` 与 `socket2` 是全平台依赖（`socket2` 在 multiplexer 创建 UDP socket 时用，见 u3-l3）；而 `tokio-timerfd` 只在 Linux 引入：

[Cargo.toml:19-20](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/Cargo.toml#L19-L20) —— `socket2 = "0.4.4"`、`nix = "0.24.2"` 是全平台依赖。

[Cargo.toml:23-24](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/Cargo.toml#L23-L24) —— `[target.'cfg(target_os="linux")'.dependencies]` 下 `tokio-timerfd = "0.2"`，仅在 Linux 引入。

> 备注：源码注释提到接收侧的 v4/v6 地址转换是临时实现，等 `nix` 新版本（>0.24.2）内置这些转换后会简化，见 [src/queue/rcv_queue.rs:224-236](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L224-L236)。

#### 4.2.4 代码实践

**实践目标**：体会「拿 fd → 调 nix → 处理 EWOULDBLOCK」这条链。

**操作步骤**（源码阅读型实践）：

1. 在 [src/multiplexer.rs:128-143](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/multiplexer.rs#L128-L143) 中找到 `as_raw_fd()`、`sendmmsg(...)`、`EWOULDBLOCK → WouldBlock` 三处，把它们用箭头串成一句：「先（等待可写）→ 拿（fd）→ 调（sendmmsg）→ 把（EWOULDBLOCK）转成（WouldBlock）」。
2. 对比 [src/queue/rcv_queue.rs:89-101](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L89-L101)，确认接收侧是同一套套路，只是 `Interest` 换成 `READABLE`。
3. 思考：为什么 `send_m_msg_to` 要先 `writable().await?` 再 `try_io`，而 `receive_packets` 没有先 `readable().await`、而是直接 `try_io`？（提示：看 worker 主循环里 `msgs` 为空时做了什么——见 [src/queue/rcv_queue.rs:154-158](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L154-L158)）。

**预期结果**：

- 第 1、2 步能画出对称的两条链（发送用 `WRITABLE`、接收用 `READABLE`）。
- 第 3 步：发送侧「一次性要把一批包尽快发出去」，所以主动 `await` 可写；接收侧是「能收就收、收不到就算了」的轮询风格，由 worker 外层用 `select { sleep | readable }` 来控制重试节奏，避免接收函数自身阻塞。

> 说明：本实践为源码阅读型，结论可由阅读直接得出，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 tokio-udt 要用 `nix` + raw fd，而不是给 `tokio::net::UdpSocket` 加一个 `sendmmsg` 方法？

**参考答案**：`sendmmsg` / `recvmmsg` 是 Linux 特有的批量系统调用，tokio 作为跨平台库不会在公共 API 里暴露平台专属功能。要在 Linux 上拿到批处理收益，只能绕过 tokio 的高层封装，直接用 fd 调系统调用；`nix` 正是为此提供的薄封装。用 `cfg(target_os = "linux")` 把它包起来，就能保证跨平台编译。

**练习 2**：`sendmmsg` 和 `recvmmsg` 都传了 `MSG_DONTWAIT` 标志。如果不用这个标志、改成阻塞调用，会和 tokio 的事件循环产生什么冲突？

**参考答案**：tokio 的任务跑在异步运行时上，一个任务阻塞会霸占整个 worker 线程，卡住其它任务（这是 tokio 明确禁止的「阻塞异步任务」反模式）。`MSG_DONTWAIT` 让系统调用在内核没准备好时立即返回 `EWOULDBLOCK` 而不阻塞，再配合 `try_io` 把它转成 `WouldBlock`，任务就能交出控制权、等 epoll 通知后再试，从而和异步运行时正确协作。

---

### 4.3 timerfd sleep_until：Linux 高精度发送调度

#### 4.3.1 概念说明

发送 worker 需要按「发送时刻」精确调度数据包（u3-l3、u5-l1）：当下一批发送时刻还没到，它要睡眠等待到那个时刻。UDT 的发送周期 `pkt_send_period` 在高速链路上是**微秒级**（初始约 1µs，见 u1-l2、u7-l2）。这就引出一个尖锐的问题：**睡眠的精度够吗？**

`tokio::time::sleep` / `sleep_until` 是基于 tokio **时间轮（timer wheel）** 的纯用户态实现。时间轮为了在「海量定时器」和「精度」之间权衡，通常采用较粗的桶粒度——它非常适合大量「毫秒级」的定时器（比如超时、心跳），但对**微秒级**的定时力不从心：定时器可能显著晚到（被粗化到最近的桶），导致相邻数据包的实际间隔远大于期望的 `pkt_send_period`，吞吐上不去、节奏被打乱。

Linux 提供了 **timerfd**：把一个内核**高精度定时器（hrtimer）**暴露成一个可读的 fd。它的关键优势是：

1. **精度高**：底层是内核 hrtimer，精度可达纳秒级，能可靠地表达微秒级间隔。
2. **与 epoll 无缝集成**：定时器到点时 fd 变成「可读」，和 socket 事件走同一套 epoll 等待机制。`tokio-timerfd` 把它包装成一个 `Future`，到期时 `poll` 返回 `Ready`。

代价是：每个 `tokio_timerfd::Delay` 会创建一个 fd（一次 `timerfd_create` + `timerfd_settime` 系统调用）。但发送 worker 在任意时刻**最多只有一个**在等的 `sleep_until`（它是一个单线程式的 `select` 循环），所以「每来一次等待就建一个 fd」的成本是有界的、可接受的，换来的是微秒级精度——这笔交易在 UDT 这种高速协议里非常划算。

> 严谨说明：tokio 时间轮的确切粒度取决于运行时配置和负载，上述「微秒级定时会被粗化」是原理性说明；不同 tokio 版本/配置下具体延迟量级「待本地验证」，但「timerfd 精度优于纯用户态时间轮」这一方向性结论是成立的。

#### 4.3.2 核心流程

发送 worker 的主循环里，当队首节点还没到发送时刻，就 `select` 等待「到点」或「被 notify 提前唤醒」（比如有新数据要发）：

```text
snd_queue.worker 主循环
  loop {
    看队首节点 timestamp：
      已到点  → 取出节点，取数据包发送
      未到点  → select { sleep_until(ts) | notify.notified() }   ◄── 本节重点
      队列空  → notify.notified().await
  }

sleep_until(ts)：            //  双实现
  Linux:    tokio_timerfd::Delay::new(ts)   //  内核 hrtimer，µs 级精度
  非 Linux: tokio::time::sleep_until(ts)    //  用户态时间轮，ms 级粒度
```

#### 4.3.3 源码精读

**双实现 `sleep_until`**。签名完全一致（吃一个 `tokio::time::Instant`），实现按平台二选一：

[src/queue/snd_queue.rs:161-172](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L161-L172) —— `sleep_until` 的 Linux 版用 `tokio_timerfd::Delay::new(instant.into_std())`，非 Linux 版用 `tokio::time::sleep_until(instant)`。

关键解读：

- Linux 版 `tokio_timerfd::Delay::new(instant.into_std())`（L162-L163）：把 tokio 的 `Instant` 转成 `std::time::Instant`，建一个基于 timerfd 的 `Delay`，`.await` 它会在内核 hrtimer 到点时完成。
- 非 Linux 版 `tokio::time::sleep_until(instant)`（L170-L171）：用 tokio 原生时间轮，跨平台但精度较粗。

**worker 里的调用点**。队首未到点时，`sleep_until` 与 `notify.notified()` 二选一等待——要么睡到点，要么被新数据/重排唤醒：

[src/queue/snd_queue.rs:101-106](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L101-L106) —— `Err(Some(ts))` 分支：`select! { _ = Self::sleep_until(ts) => {} _ = self.notify.notified() => {} }`。

**接收侧的 `sleep` 也是双实现**。`rcv_queue` 里那个 30µs 的「空收补眠」同样按平台区分导入哪个 `sleep`：

[src/queue/rcv_queue.rs:13-16](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L13-L16) —— 非 Linux 导入 `tokio::time::sleep`，Linux 导入 `tokio_timerfd::sleep`。

[src/queue/rcv_queue.rs:154-158](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L154-L158) —— `select! { _ = sleep(UDP_RCV_TIMEOUT) => () _ = self.channel.readable() => () }`，其中 `UDP_RCV_TIMEOUT = 30µs`（[src/queue/rcv_queue.rs:19](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L19)）。

> 备注：发送侧用 `sleep_until`（精确到点），接收侧用 `sleep`（固定时长补眠）。两者在 Linux 下都走 `tokio_timerfd`，正是为了让这些 30µs ~ 微秒级的等待不被时间轮粗化。

#### 4.3.4 代码实践

**实践目标**：理解「同一签名、两套实现」如何让上层代码与平台无关，以及为何发送调度要精度优先。

**操作步骤**（源码阅读型实践）：

1. 打开 [src/queue/snd_queue.rs:161-172](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L161-L172)，确认 `sleep_until` 的 Linux 版与非 Linux 版**函数签名完全相同**。
2. 看 [src/queue/snd_queue.rs:101-106](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/snd_queue.rs#L101-L106)，确认 worker 调用 `Self::sleep_until(ts)` 时**不关心**底层是哪种实现——这就是「双实现 + 统一签名」的好处。
3. 结合 u7-l2 的 `RateControl`：发送周期 `pkt_send_period` 是怎么进入 `next_data_packets` 计算出的「下一个发送时刻」的？（提示：相邻两次发送的 timestamp 间隔≈`pkt_send_period`，而 `sleep_until` 要精确睡到这个 timestamp。）
4. （可选，需 Linux 环境）写一个最小对照：分别用 `tokio::time::sleep(Duration::from_micros(50))` 和 `tokio_timerfd::Delay` 各睡 50µs 共 1000 次，用 `Instant` 测总耗时，比较谁更接近期望的 50ms。**具体数值待本地验证**。

**预期结果**：

- 第 1、2 步确认：上层 `worker` 完全平台无关，平台差异被封装在 `sleep_until` 内部。
- 第 4 步（若运行）：`timerfd` 版总耗时更接近 50ms（精度高），`tokio::time::sleep` 版往往明显偏大（被粗化）。该定量结论「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：发送 worker 任意时刻最多只有「一个」`sleep_until` 在等。为什么这个事实让「每个 `Delay` 要建一个 fd」的代价变得可以接受？

**参考答案**：因为只有一个待定时的 `Delay`，fd 的创建/销毁频率等于「发送调度循环的等待次数」，是个有界的、串行的开销；不会出现「海量并发定时器各占一个 fd」那种 fd 膨胀问题。用有界的 fd 开销换取微秒级精度，对高速 UDT 发送是划算的。

**练习 2**：如果某天 tokio 把时间轮精度提升到纳秒级且零开销，`sleep_until` 的 Linux 版还有必要用 timerfd 吗？

**参考答案**：理论上就没必要了——既然用户态定时器已能满足精度，可以直接用 `tokio::time::sleep_until`，省掉 timerfd 的 fd 创建开销。这也说明：timerfd 这条快路径是针对「当前 tokio 时间轮在微秒级不够精确」这一现状的工程权衡，而非协议本身的硬性要求。

---

## 5. 综合实践

把三个最小模块串起来，完成一次「全链路平台快路径追踪」。

**任务**：以「一次批量数据发送 → 对端批量接收」为主线，画出 Linux 下从应用层 `write` 到内核系统调用的完整快路径，并标注每一步用到的平台优化；再画出非 Linux 的回退路径作对比。

**建议步骤**：

1. **发送链（Linux）**：`UdtConnection::poll_write` → `UdtSocket::send`（攒数据进 `SndBuffer`）→ `snd_queue.worker` 取包，未到点时 `sleep_until`（**timerfd**，4.3）→ 到点后 spawn 子任务调 `send_data_packets` → `send_m_msg_to`（**sendmmsg**，4.1）→ `try_io(WRITABLE)` + `as_raw_fd` + nix（4.2）→ 内核 `sendmmsg`。
2. **接收链（Linux）**：对端 `rcv_queue.worker` 用 `mss*100` 缓冲调 `receive_packets`（**recvmmsg**，4.1）→ `try_io(READABLE)` + `as_raw_fd` + nix（4.2）→ 收到一批包 → 按 `dest_socket_id` 分发 → `process_packet`。
3. **回退链（非 Linux）**：把上面三处快路径分别替换为 `tokio::time::sleep_until`、`send_to` 循环、`try_recv_from` 循环，其余逻辑不变。
4. 在图上用三种颜色分别标出：批量系统调用（4.1）、raw fd 桥接（4.2）、高精度定时（4.3）涉及的代码位置，并写出每个位置的「文件:行号」。

**预期产出**：一张对比图 + 一张「Linux 三处快路径 vs 非 Linux 三处回退」的对照表。关键对照点：

| 操作 | Linux 快路径 | 非 Linux 回退 | 收益 |
| --- | --- | --- | --- |
| 批量发送 | `sendmmsg`（1 次系统调用） | `send_to` 循环（N 次） | 省 N−1 次系统调用 |
| 批量接收 | `recvmmsg`（≤100 包/次） | `try_recv_from` 循环 | 同上 |
| 调度睡眠 | `tokio_timerfd::Delay` | `tokio::time::sleep_until` | 微秒级精度 |

---

## 6. 本讲小结

- tokio-udt 在 Linux 上用 `sendmmsg` / `recvmmsg` 把「逐包系统调用」压成「一次系统调用收发多包」，省下大量用户态↔内核态切换开销；接收缓冲 `mss*100` 决定了 `recvmmsg` 一次最多收 100 个包。
- 这些批量系统调用 tokio 的 `UdpSocket` 不提供，所以用 `nix` 拿底层 raw fd（`as_raw_fd()`）直连内核，并通过 `try_io(Interest)` 与 epoll 协作，把 `EWOULDBLOCK` 转成 `WouldBlock` 实现非阻塞。
- 三个关键函数（`send_m_msg_to` / `receive_packets` / `sleep_until`）都采用 `#[cfg(target_os = "linux")]` + `#[cfg(not(...))]` 的「同一签名、两套实现」模式，上层调用方完全平台无关。
- 发送调度对定时精度要求极高（`pkt_send_period` 微秒级），`tokio::time::sleep` 的时间轮粒度太粗，故 Linux 改用基于内核 hrtimer 的 `tokio_timerfd`；非 Linux 回退到 `tokio::time::sleep_until`，能跑但精度较低。
- 这套快路径是纯工程优化：它不改变 UDT 协议语义，只让同样的协议在 Linux 上跑得更快；`nix` / `socket2` 是全平台依赖，`tokio-timerfd` 仅 Linux 引入。

---

## 7. 下一步学习建议

至此，tokio-udt 学习手册的技术讲义全部完成。建议按以下方向收尾：

1. **横向串读全系列**：回到 u1-l3 的模块地图，对照本讲确认「平台快路径」属于「数据通路」与「并发/性能」的交叉点——它是 u3-l3（multiplexer）、u5-l1/u5-l2（收发通路）、u7-l2（RateControl 调速）这些上层机制赖以高效执行的底层支撑。
2. **动手做一次性能实验**：在 Linux 上跑 `cargo run --release --bin udt_sender`（u1-l2），观察吞吐；若有 macOS 环境，对比同链路下的吞吐差异，体会本讲三处快路径的累积效果（数值「待本地验证」）。
3. **延伸阅读**：man 页 `sendmmsg(2)` / `recvmmsg(2)` / `timerfd_create(2)`；以及 `tokio-timerfd`、`nix` crate 文档，理解它们提供的更多原语。
4. **二次开发方向**：若要在新平台（如 Windows）上获得类似批处理收益，可研究该平台的批量收发原语（如 Windows 的 `WSASendMsg` 配合 `MSG_WAITALL` 等），按本讲的「双实现」套路新增第三套 `cfg` 分支。
