# u2-l4 SocketCore 与异步 DNS：网络底座

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `SocketCore` 如何把平台差异巨大的 BSD socket / WinSock 统一成一个 C++ 类，并解释「非阻塞连接」的完整语义（`connect` 返回 `EINPROGRESS` 之后发生什么）。
2. 解释 `readData`/`writeData` 在非阻塞模式下遇到 `EAGAIN` 时如何通过 `wantRead_`/`wantWrite_` 向 EventPoll 层「留言」，以及 `SocketRecvBuffer` 为什么固定 16 KB。
3. 跟踪一次域名解析的完整链路：`InitiateConnectionCommand` → `AbstractCommand::resolveHostname` → `AsyncNameResolverMan`（IPv6/IPv4 双栈并发）→ `AsyncNameResolver`（c-ares 通道）→ EventPoll 的 `ADNSEvent` 回调，理解 `getaddrinfo` 是如何被「拆成事件」而不是被扔进线程的。
4. 解释 aria2 版 Happy Eyeballs（RFC 6555）：主连接选了 IPv6 时，`BackupIPv4ConnectCommand` 如何延迟 300 ms 并行发起 IPv4 备份连接，谁先成功用谁的 socket。

本讲是 u2-l1（DownloadEngine 主循环）与 u2-l2（EventPoll）的直接下游：前两讲讲了「引擎如何等事件、命令如何被调度」，本讲回答「最底层那批 fd 上到底发生了什么」。

## 2. 前置知识

### 2.1 阻塞与非阻塞 socket

默认情况下 socket 是阻塞的：`connect()` 会一直等到握手完成才返回，`recv()` 在没有数据时会睡眠。单线程事件驱动程序（aria2 就是）绝不能在任何 fd 上睡眠——一旦睡住，整个引擎的所有下载全部停摆。解决办法是把 fd 设为非阻塞（`fcntl(fd, F_SETFL, O_NONBLOCK)`），此后所有操作「要么立刻完成、要么立刻失败」：

- 非阻塞 `connect()` 几乎总是立刻返回 `-1`，errno 为 `EINPROGRESS`——这不是错误，而是「TCP 三次握手正在后台进行，等 socket 可写时再来问结果」。
- 非阻塞 `recv()`/`send()` 在暂无数据/暂不能发时返回 `-1`，errno 为 `EAGAIN`（Linux 上 `EWOULDBLOCK` 与之等价）——含义是「现在没有，稍后再试」。

「稍后再试」的时机由 I/O 多路复用（epoll/kqueue/select，即 u2-l2 的 EventPoll）通知。本讲要读的代码就是这两套语义在 aria2 里的封装。

### 2.2 DNS 与 getaddrinfo

把 `example.com` 变成 `93.184.216.34` 的过程叫域名解析。POSIX 标准接口是 `getaddrinfo()`：传入主机名，返回一个 `addrinfo` 链表（可能同时含 IPv6 `AF_INET6` 和 IPv4 `AF_INET` 地址）。问题是它是一个**阻塞**的系统调用——DNS 查询要走 UDP/TCP 问解析器，可能耗时数秒。在单线程引擎里直接调它，等于卡死所有下载。

aria2 有两条路：

- **同步路径**：`NameResolver` 直接包 `getaddrinfo`（构建时没找到 c-ares，或用户 `--async-dns=false` 时走这条）。
- **异步路径**：用 c-ares 库。c-ares 自己创建 UDP socket 发 DNS 查询、自己收响应，把「等响应」变成「等它那几个 socket 可读」——于是 DNS 查询就能塞进 EventPoll 的同一套事件循环里，**不需要任何额外线程**。这是本讲的重点。

### 2.3 Happy Eyeballs（RFC 6555）

双栈机器上，域名通常同时解析出 IPv6（AAAA 记录）和 IPv4（A 记录）。如果优先试 IPv6 而对端 IPv6 不通，要等 TCP 超时（几十秒）才能回退 IPv4，体验极差。RFC 6555 的建议是：先发起首选协议（IPv6）连接，**等待约 300 ms** 后若还没连上，就并行发起另一个协议族（IPv4）的连接，谁先成功用谁。aria2 用 `BackupIPv4ConnectCommand` 实现了这个策略。

### 2.4 与前讲的衔接

- u2-l2 讲过 EventPoll 的回调「只置位、不执行」：事件到达只是 `setStatusActive()` 唤醒命令，真正的读写发生在命令的 `execute()` 里。本讲的 `ADNSEvent` 和 `wantRead_`/`wantWrite_` 都是这个约定的具体实例。
- u2-l3 讲过命令的接力与 requeue。本讲的 `InitiateConnectionCommand` 在 DNS 尚未完成时返回空地址、`addCommandSelf()` 自我重排，就是那个机制在网络层的应用。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/SocketCore.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.h) / [src/SocketCore.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc) | 对 BSD socket/WinSock 的统一封装：创建、绑定、非阻塞连接、读写、SO_ERROR 查询；还内嵌 TLS/SSH 会话指针（本讲不展开） |
| [src/SocketRecvBuffer.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketRecvBuffer.h) / [src/SocketRecvBuffer.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketRecvBuffer.cc) | 16 KB 接收缓冲：一次 `recv` 尽量读满，上层按需 `drain`，避免小块系统调用 |
| [src/NameResolver.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/NameResolver.cc) | 同步解析：直接包 `callGetaddrinfo`（阻塞） |
| [src/AsyncNameResolver.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolver.h) / [src/AsyncNameResolver.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolver.cc) | 单个 c-ares 查询的封装：channel 生命周期、DNS socket 事件登记、`ares_process_fd` 驱动 |
| [src/AsyncNameResolverMan.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolverMan.h) / [src/AsyncNameResolverMan.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolverMan.cc) | 双栈解析管理器：同时发起 AAAA 与 A 查询、汇总状态、决定「A 成功就不等 AAAA」 |
| [src/Event.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Event.h) | u2-l2 已读；本讲关注其中的 `ADNSEvent`——把 c-ares 的 socket 塞进 EventPoll 的适配器 |
| [src/AbstractCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc) | `resolveHostname()`：DNS 缓存查询 + 异步/同步分流 + 结果缓存；`checkIfConnectionEstablished()`：连接完成判定 |
| [src/InitiateConnectionCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommand.cc) | 发起连接的命令基类：先解析域名，再创建下一个命令；也是 Happy Eyeballs 的发起点 |
| [src/BackupIPv4ConnectCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BackupIPv4ConnectCommand.cc) | IPv4 备份连接命令：延迟 300 ms 发起、成功后把 socket 移交主命令 |
| [src/ConnectCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConnectCommand.cc) | 等待连接完成的命令：判定 SO_ERROR、消费备份连接、取消备份连接 |

## 4. 核心概念与源码讲解

### 4.1 SocketCore：BSD socket 的统一封装

#### 4.1.1 概念说明

aria2 要同时跑在 Linux/macOS/Windows（MinGW）上，而这三家的 socket API 有大量细节差异：错误码用 `errno` 还是 `WSAGetLastError()`、`EWOULDBLOCK` 与 `EAGAIN` 是否同值、`close` 还是 `closesocket`、非阻塞用 `fcntl` 还是 `ioctlsocket`。`SocketCore` 用一个类把这些差异全部吸收：文件开头的宏把平台差异翻译成统一的 `SOCKET_ERRNO`、`A2_EINPROGRESS`、`A2_WOULDBLOCK(e)`、`CLOSE(X)`，类内代码只写统一名字。

它解决三个问题：

1. **可移植**：上层协议代码（HTTP/FTP/BT）只面对 `SocketCore`，不碰平台宏。
2. **状态管理**：把「这个 fd 现在是阻塞还是非阻塞」「上次读写是否遇到 EAGAIN」记成成员变量。
3. **生命周期**：析构自动关闭 fd（RAII），杜绝泄漏。

#### 4.1.2 核心流程

一次客户端连接的时序：

```text
SocketCore soc;                     # 构造，sockfd_ = -1，blocking_ = true
soc.establishConnection(ip, port)   # 内部：
    ├─ callGetaddrinfo(ip, port)    # ip 通常是数字串 → 本地转换，非 DNS 查询
    ├─ 遍历 addrinfo 链表
    │     ├─ socket() + SO_REUSEADDR + (可选) bind 本地地址
    │     ├─ setNonBlockingMode()   # fcntl 设 O_NONBLOCK
    │     └─ connect() → 期望得到 -1 且 errno==EINPROGRESS
    └─ 记录 sockfd_
（命令链把 fd 注册进 EventPoll 等可写）
soc.getSocketError()                # SO_ERROR==0 表示三次握手完成
soc.writeData(...) / soc.readData(...)   # 非阻塞读写，EAGAIN → wantWrite_/wantRead_
soc.closeConnection()               # shutdown + close（析构也会调）
```

#### 4.1.3 源码精读

**成员字段**——理解这个类的钥匙是这几个私有成员：

[SocketCore.h:L67-L86](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.h#L67-L86)：`sockType_`（SOCK_STREAM/SOCK_DGRAM）、`sockfd_`（描述符，`-1` 表示未打开）、`blocking_`（当前阻塞状态）、`wantRead_`/`wantWrite_`（上次 EAGAIN 的方向留言）。`protocolFamily_`、`bindAddrs_` 等是静态成员，由 `--protocol-family`、`--interface` 之类的全局选项一次性设置，影响本进程所有连接。

**非阻塞连接的建立**：

[SocketCore.cc:L427-L509](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L427-L509) 是 `establishConnection`。关键几步：

- [L434-L437](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L434-L437)：调 `callGetaddrinfo` 把 `host` 转成 `addrinfo` 链表。注意：走到这里时 `host` 通常已经是 `resolveHostname()` 产出的**数字 IP**（见 4.3 节），`getaddrinfo` 对数字串只做本地文本转换（配合 `AI_NUMERICHOST` 类语义），不产生 DNS 网络查询——真正会阻塞的域名解析早在命令链上游完成了。
- [L443-L460](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L443-L460)：遍历链表逐个尝试：`socket()` → `make_fd_cloexec`（防止 exec 时泄漏 fd）→ `SO_REUSEADDR` → 若配置了 `--interface` 则先 `bind` 本地地址。
- [L488-L494](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L488-L494)：先把 socket 切成非阻塞（`setNonBlockingMode()`），可选设 `TCP_NODELAY`，然后 `connect()`。判定条件是 `connect(...) == -1 && SOCKET_ERRNO != A2_EINPROGRESS` 才算失败——**返回 -1 且 errno 是 EINPROGRESS 正是期望结果**，表示握手在后台进行；其他 errno（如 EHOSTUNREACH、ECONNREFUSED）才关闭 fd 换下一个地址。
- [L502-L504](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L502-L504)：源码里留了一个 TODO：非阻塞 connect 在这一步无法知道最终成败，如果后面握手失败，本应回头试链表里的下一个地址，目前没有做。

**阻塞/非阻塞切换**：

[SocketCore.cc:L596-L632](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L596-L632)：`setNonBlockingMode`/`setBlockingMode`。POSIX 路径是经典的 `fcntl(F_GETFL)` → 加/去 `O_NONBLOCK` → `fcntl(F_SETFL)`，两步都写成 `while(... == -1 && errno == EINTR)` 循环以扛住信号打断；Windows 路径换成 `ioctlsocket(FIONBIO)`。切换后同步更新 `blocking_` 标志。aria2 的下载主链路里，连接一旦建立**始终保持非阻塞**，等待全部交给 EventPoll。

**连接完成判定**：

[SocketCore.cc:L1256-L1271](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L1256-L1271)：`getSocketError()` 读 `SO_ERROR`。非阻塞 connect 的标准收尾：fd 变为可写后，用 `getsockopt(SOL_SOCKET, SO_ERROR)` 问内核「三次握手到底成没成」；0 表示成功，非 0 返回对应错误串。上层 `AbstractCommand::checkIfConnectionEstablished()`（[AbstractCommand.cc:L835-L866](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L835-L866)）就靠它：失败则把该 IP 在 DNS 缓存里标记为坏地址（`markBadIPAddress`），若还有别的候选地址就重新创建 `InitiateConnectionCommand` 换地址再连。

**辅助原语**：`create()`（[SocketCore.cc:L195-L218](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L195-L218)，服务端/UDP 路径用）与 `closeConnection()`（[SocketCore.cc:L634-L655](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L634-L655)：先关 TLS/SSH 会话再 `shutdown(SHUT_WR)` + `close`，析构函数也会调它，实现 RAII）。`isWritable(timeout)`/`isReadable(timeout)`（[SocketCore.cc:L666-L754](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L666-L754)）是独立的 `poll`/`select` 等待原语，供**没有接入 EventPoll 的代码路径**（RPC 服务器、BT 监听、FTP 数据连接探测等，参见 `HttpListenCommand.cc:72`、`FtpNegotiationCommand.cc:749`）做零超时探测用，不要与主链路混淆。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到非阻塞 connect 的 `EINPROGRESS` 行为，理解「connect 返回后连接尚未建立」。
2. **操作步骤**：
   - 写一个 30 行左右的小程序（示例代码，非项目源码）：`socket` → `fcntl` 设 `O_NONBLOCK` → `connect` 一个远端地址（例如 93.184.216.34:80），立刻打印返回值与 `errno`；然后 `poll` 等待 `POLLOUT`，再 `getsockopt(SO_ERROR)` 打印结果。
   - 对照 [SocketCore.cc:L494-L501](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L494-L501) 的判定条件，把你的小程序输出与它逐项对应。
3. **需要观察的现象**：`connect` 立刻返回 -1、errno 为 `EINPROGRESS`（115 on Linux）；几十毫秒后 `poll` 报告可写、`SO_ERROR` 为 0。
4. **预期结果**：你会看到「connect 返回」与「连接建立」之间确实存在一个可等待的窗口——这正是 EventPoll 里 `write_event_` 驱动 `ConnectCommand` 的原因（回顾 u2-l2）。
5. 运行结果**待本地验证**（不同网络环境下 EINPROGRESS 窗口长短不同）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `establishConnection` 要在 `connect` **之前**调用 `setNonBlockingMode()`，而不是之后？

**答案**：如果先在阻塞模式下 `connect`，调用会一直睡到握手完成或超时，单线程引擎在这期间无法处理任何其他命令——非阻塞的意义正是把「等待」从同步睡眠变成事件通知。先切非阻塞，`connect` 才会立刻返回 `EINPROGRESS`，等待动作交还给 EventPoll。

**练习 2**：`SocketCore.cc` 文件头部（L71-L100）定义的 `SOCKET_ERRNO`、`A2_EINPROGRESS`、`CLOSE(X)` 各解决什么问题？

**答案**：它们抹平 POSIX 与 WinSock 的差异——Windows 下 socket 错误不写 `errno` 而要用 `WSAGetLastError()`（`SOCKET_ERRNO`）；Windows 没有 `EINPROGRESS`，非阻塞 connect 的「进行中」对应 `WSAEWOULDBLOCK`（`A2_EINPROGRESS`）；关闭函数分别是 `close` 与 `closesocket`（`CLOSE`）。统一之后，`establishConnection` 等函数体只需写一份。

### 4.2 非阻塞读写与接收缓冲：wantRead/wantWrite 与 SocketRecvBuffer

#### 4.2.1 概念说明

非阻塞 socket 上调用 `send`/`recv`，内核缓冲区满（send）或空（recv）时返回 `EAGAIN`。上层命令此刻该做什么？它需要知道两件事：**这次读写没成功**，以及**下次该等什么事件**。`SocketCore` 用 `wantRead_`/`wantWrite_` 两个布尔把「下次该等什么」记录在 socket 对象上，交给上层（`CommandEvent`，见 u2-l2）去 EventPoll 注册对应方向的事件。这就是一种反压（backpressure）信号：数据泵在 4.1 讲的接力里只管尽力搬，搬不动就留言挂起。

`SocketRecvBuffer` 则是读方向的优化：每次系统调用都有固定开销，与其让解析代码一次要几个字节就 `recv` 几个字节，不如一次读 16 KB 进用户态缓冲，让 HTTP 头解析器等在缓冲上「切」数据。

#### 4.2.2 核心流程

```text
writeData(buf, len):
    先清零 wantRead_/wantWrite_
    send(sockfd_, ...)
    ├─ 成功 → 返回写入字节数
    ├─ EAGAIN → wantWrite_ = true, 返回 0      # 下次等可写
    └─ 其他错误 → 抛 DL_RETRY_EX
readData(buf, len):
    同样先清零标志
    recv(sockfd_, ...)
    ├─ 成功 → len = 读到的字节数
    ├─ EAGAIN → wantRead_ = true, len = 0      # 下次等可读
    └─ 其他错误 → 抛 DL_RETRY_EX

SocketRecvBuffer::recv():
    剩余容量 = buf_ 末尾 - last_
    socket_->readData(last_, 剩余容量)   # 尽量读满 16 KB
    last_ += n
drain(n): pos_ += n；若 pos_==last_ 则整个缓冲清空复用
```

#### 4.2.3 源码精读

**writeData 的 EAGAIN 语义**：

[SocketCore.cc:L799-L839](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L799-L839)：进入函数第一件事就是把 `wantRead_`/`wantWrite_` 清零（L802-L803，头文件注释 L252 也写明了这个约定：「本方法在做任何事之前先重置两个标志」）。普通路径 `send()` 用 `while(...==-1 && EINTR)` 扛信号打断；返回 -1 时若不是 `A2_WOULDBLOCK` 就抛 `DL_RETRY_EX`（可重试异常，回顾 u2-l3 的异常出口），是 EAGAIN 则 `wantWrite_ = true; ret = 0`——**把 EAGAIN 翻译成「返回 0 + 留言」而不是报错**。`secure_` 分支（TLS）同理，只是 EAGAIN 判定换成 `TLS_ERR_WOULDBLOCK`，且方向要问 TLS 会话（TLS 握手期间写数据可能需要先读）。

**readData 与方向判定**：

[SocketCore.cc:L841-L901](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L841-L901)：结构与 writeData 对称：SSH 会话 → 普通 socket → TLS 会话三层分支；普通路径 `recv()` 的 EAGAIN 映射为 `wantRead_ = true; ret = 0`。头文件 [SocketCore.h:L244-L283](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.h#L244-L283) 的注释详细解释了阻塞/非阻塞两种模式下的行为差异。`wantRead()`/`wantWrite()` 两个内联访问器（[SocketCore.cc:L1273-L1275](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L1273-L1275)）供上层命令决定注册读事件还是写事件。

**16 KB 接收缓冲**：

[SocketRecvBuffer.h:L49-L76](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketRecvBuffer.h#L49-L76)：核心是 `std::array<unsigned char, 16_k> buf_`（16 KB，`16_k` 是项目自定义的字面量后缀）加 `pos_`/`last_` 两个指针构成的可读区间；`getBuffer()`/`getBufferLength()`/`bufferEmpty()` 让解析器零拷贝地查看已有数据。[SocketRecvBuffer.cc:L52-L73](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketRecvBuffer.cc#L52-L73)：`recv()` 把剩余容量交给 `socket_->readData`（注意 `readData` 的 len 是引用入参，返回时被改写为实际读取数），`drain(n)` 前移 `pos_`，消费到尾则 `truncateBuffer()` 把两个指针一起归零——整个缓冲是一个无需搬移数据的滑动窗口。

#### 4.2.4 代码实践

1. **实践目标**：在日志里观察 `SocketRecvBuffer` 的读取节奏。
2. **操作步骤**：
   - 在 [SocketRecvBuffer.cc:L59-L60](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketRecvBuffer.cc#L59-L60) 之间加一行 `A2_LOG_DEBUG(fmt("recv %zu bytes, buffered=%zu", n, getBufferLength()));`，重新编译。
   - 运行 `./src/aria2c -l log.txt --log-level=debug <某个 HTTP URL>`，统计一次下载中该日志出现的次数与每次的 n 值分布。
3. **需要观察的现象**：n 常常接近 16 KB（快网络）或较小且伴随 `Buffer full` 调试行（消费速度跟不上时）。
4. **预期结果**：系统调用次数 ≈ 文件大小 / 平均 n，远小于「按解析器小步要字节」的方案；`drain` 之后缓冲被整体复用，没有数据搬移。
5. 具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `readData`/`writeData` 每次进入都要先把 `wantRead_`/`wantWrite_` 清零，而不是「保持上次的状态」？

**答案**：这两个标志表达的是「**上一次**调用因 EAGAIN 失败后，**下一次**应该等待的方向」。每次新调用都会产生新的结果（可能这次就成功了，或这次换了个方向阻塞），旧留言随之失效；若不清零，上层会按过期信息向 EventPoll 注册错误方向的事件，命令永远等不到唤醒。

**练习 2**：`SocketRecvBuffer::recv()` 在缓冲满时直接返回 0（L54-L58），不调 `readData`。这意味着什么？

**答案**：意味着「用户态缓冲还有 16 KB 没被消费」。此时 socket 里即使有数据也不读，相当于对上游限流；消费方（如 HTTP 头解析器）必须先 `drain` 腾出空间，读取才能继续。这是把「解析速度」与「网络速度」解耦的关键一环。

### 4.3 异步 DNS：从 getaddrinfo 到 AsyncNameResolverMan

#### 4.3.1 概念说明

这是本讲的核心模块。aria2 把域名解析异步化的思路**不是开线程**，而是把「一次 getaddrinfo 调用」拆成三个可被事件循环驱动的动作：

1. **发起**：`ares_getaddrinfo()` 立刻返回，c-ares 在内部创建 UDP socket 发出 DNS 查询报文。
2. **等待**：c-ares 通过构造时注册的 `sock_state_cb` 回调告诉 aria2「我现在需要监听哪些 fd 的读/写」，aria2 把这些 fd 注册进 EventPoll——和业务 socket 走同一套多路复用。
3. **驱动**：fd 可读时（DNS 响应到了），EventPoll 回调里调 `ares_process_fd()`，c-ares 解析报文，最终触发用户回调 `callback()` 写入结果并置 `STATUS_SUCCESS`。

在这套机制外面，`AsyncNameResolverMan` 再包一层**双栈策略**：同时发起 AAAA（IPv6）与 A（IPv4）两个查询，谁先成功用谁；并且「A 记录成功就不等 AAAA」——因为现实网络里不少 DNS 服务器会丢弃 AAAA 查询导致长时间超时，反之 A 查询几乎总有响应。

而命令链的入口是 `AbstractCommand::resolveHostname()`：它先查 DNS 缓存，未命中才走上面的异步流程；解析完成后把所有地址写回缓存，供后续连接（包括 4.4 的备份连接）复用。

#### 4.3.2 核心流程

```text
InitiateConnectionCommand::executeInternal()
    └─ resolveHostname(addrs, hostname, port)          # AbstractCommand.cc
        ├─ 是数字 IP？ → 直接返回
        ├─ 查 DNS 缓存（DownloadEngine::findAllCachedIPAddresses）
        │     命中 → 打 "DNS cache hit" 日志，返回缓存地址
        ├─ [异步路径] PREF_ASYNC_DNS 为 true：
        │     ├─ 未启动 → AsyncNameResolverMan::startAsync(hostname, e, this)
        │     │     ├─ ipv6_ 开 → AsyncNameResolver(AF_INET6).resolve()  # index 0
        │     │     ├─ ipv4_ 开 → AsyncNameResolver(AF_INET).resolve()   # index 1
        │     │     └─ 两个都 addNameResolverCheck → EventPoll 注册 ADNSEvent
        │     ├─ getStatus()==0（进行中）→ 返回空串
        │     │     → 命令 addCommandSelf() 自我重排，等下轮再问
        │     ├─ getStatus()==-1（全败）→ 抛 NAME_RESOLVE_ERROR
        │     └─ getStatus()==1（成功）→ getResolvedAddress 汇总双栈结果
        ├─ [同步路径] 否则：NameResolver::resolve() 阻塞调 getaddrinfo
        └─ cacheIPAddress 把结果全部写入缓存 → 返回首选地址

EventPoll 侧（等待阶段）：
    c-ares socket 可读/可写 → ADNSEvent::processEvents()
        ├─ resolver->process(readfd, writefd) → ares_process_fd(channel,...)
        │     → c-ares 解析响应 → callback() 填结果、置 STATUS_SUCCESS
        └─ command->setStatusActive()  # 唤醒等待中的 InitiateConnectionCommand
```

#### 4.3.3 源码精读

**入口：resolveHostname**。

[AbstractCommand.cc:L759-L823](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L759-L823)：

- [L763-L766](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L763-L766)：`util::isNumericHost` 命中则直接把字面量当地址用，完全跳过 DNS。
- [L768-L774](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L768-L774)：查 `DownloadEngine` 的 DNS 缓存（`cacheIPAddress` 的写入端在 [DownloadEngine.cc:L538-L548](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L538-L548)，底层是 `DNSCache`，按 hostname+port 键存地址列表并支持把坏地址标记 `markBad`）。命中时打 `MSG_DNS_CACHE_HIT` 日志。
- [L777-L805](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L777-L805)：异步分支（整段被 `#ifdef ENABLE_ASYNC_DNS` 包住——只有构建时探测到 c-ares 才存在，`--async-dns` 选项默认 true，见 [OptionHandlerFactory.cc:L87-L95](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L87-L95)，Android 构建例外默认 false）。状态机三态：`-1` 失败（同时给 ServerStat 记错误）；`0` 进行中——**返回空字符串**；`1` 成功——取地址继续。
- [L808-L815](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L808-L815)：同步兜底分支，`NameResolver` 直连阻塞版。
- [L816-L822](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L816-L822)：打出 `MSG_NAME_RESOLUTION_COMPLETE` 日志并把全部结果写入缓存。

**调用方如何消化「进行中」**：[InitiateConnectionCommand.cc:L74-L92](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommand.cc#L74-L92) 中，`ipaddr.empty()` 时 `addCommandSelf(); return false;`——命令把自己重新排回队列（u2-l3 的 requeue 约定），等 c-ares 的事件把它再次唤醒。该命令构造时 `setTimeout(PREF_DNS_TIMEOUT)`（[InitiateConnectionCommand.cc:L65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommand.cc#L65)，即 `--dns-timeout`，默认 30 秒、范围 1-60，注册于 [OptionHandlerFactory.cc:L232](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L232)）——整个 DNS 阶段受 u2-l3 讲过的超时机制保护，超时抛 `TIME_OUT` 走重试。

**单查询封装 AsyncNameResolver**（c-ares 适配层）：

- 状态机：[AsyncNameResolver.h:L59-L64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolver.h#L59-L64) 定义 `STATUS_READY/QUERYING/SUCCESS/ERROR` 四态。
- 构造与 channel：[AsyncNameResolver.cc:L115-L130](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolver.cc#L115-L130)：`ares_init_options` 创建 channel 并注册 `sock_state_cb`；若用户设置了 `--async-dns-server` 则用 `ares_set_servers_csv` 覆盖系统 DNS。每个 `AsyncNameResolver` 实例独享一个 channel，析构 `ares_destroy`。
- 发起查询：[AsyncNameResolver.cc:L134-L143](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolver.cc#L134-L143)：`resolve()` 只是把状态置 `QUERYING` 并调 `ares_getaddrinfo(channel, name, nullptr, &hints, callback, this)`——**立刻返回**，查询在 c-ares 内部异步进行；`hints.ai_family` 决定这一路查 A 还是 AAAA。
- socket 状态回调：[AsyncNameResolver.cc:L82-L113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolver.cc#L82-L113)：`handle_sock_state(fd, read, write)` 是 c-ares 的「监听需求清单」：c-ares 想读某 fd 就置 `EVENT_READ`、想写就置 `EVENT_WRITE`，都不要了就从 `socks_` 列表删除。这个列表就是随后注册进 EventPoll 的依据。
- 完成回调：[AsyncNameResolver.cc:L47-L71](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolver.cc#L47-L71)：c-ares 解析完响应后调 `callback()`，用 `getnameinfo(NI_NUMERICHOST)` 把每个 `sockaddr` 转回数字串塞进 `resolvedAddresses_`，全部成功置 `STATUS_SUCCESS`；失败置 `STATUS_ERROR` 并记 `ares_strerror` 文本。
- 驱动接口：[AsyncNameResolver.cc:L186-L195](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolver.cc#L186-L195)：`process(readfd, writefd)` 一行 `ares_process_fd(channel_, ...)`——告诉 c-ares「这两个 fd 有事件了，你去收/发吧」。另有基于 `fd_set` 的旧接口 `getFds/process(fd_set*)`（L145-L182）。

**双栈管理 AsyncNameResolverMan**：

- [AsyncNameResolverMan.cc:L68-L84](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolverMan.cc#L68-L84)：`startAsync` 先 IPv6 后 IPv4 地创建 resolver（注释写明顺序是为了让 `getResolvedAddress()` 里 IPv6 地址排在前），并打出 `MSG_RESOLVING_HOSTNAME`（"Resolving hostname %s"）日志——**这就是 DNS 阶段的开始日志行**。
- [AsyncNameResolverMan.cc:L86-L94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolverMan.cc#L86-L94) 与 [L117-L125](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolverMan.cc#L117-L125)：每个 resolver 经 `DownloadEngine::addNameResolverCheck`（[DownloadEngine.cc:L283-L295](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L283-L295)）转交给 `eventPoll_->addNameResolver`，即注册成 `ADNSEvent`；`resolverCheck_` 位图防止重复注册/漏注销，`reset()`（[L192-L200](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolverMan.cc#L192-L200)）在命令退出时注销并销毁两个 resolver。
- [AsyncNameResolverMan.cc:L145-L179](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolverMan.cc#L145-L179)：`getStatus()` 的聚合策略是本模块的精华：统计成功/失败数，只要**有一个 IPv4 查询成功**就返回 1（不等 IPv6）；只有**全部**失败才返回 -1；否则 0。L165-L169 的注释解释了原因：DNS 服务器可能丢弃 AAAA 查询，等它就要吃长超时；而 A 查询几乎总会有响应，所以不做反向的「AAAA 成功就不等 A」。
- [AsyncNameResolverMan.cc:L96-L107](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolverMan.cc#L96-L107)：`getResolvedAddress` 按索引顺序拼接结果（IPv6 在前、IPv4 在后），正好与 `startAsync` 的创建顺序呼应。
- [AsyncNameResolverMan.cc:L202-L221](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolverMan.cc#L202-L221)：`configureAsyncNameResolverMan` 在命令构造时调用（[AbstractCommand.cc:L109](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L109)）：探测本机是否配置了 IPv4/IPv6 地址（`net::getIPv4AddrConfigured` 等），**没有 IPv6 地址就把 ipv6_ 关掉**——避免对着不可能成功的 AAAA 查询白等；`--disable-ipv6` 同样关闭 ipv6_；`--async-dns-server` 传入 `servers_`。

**EventPoll 侧的适配器 ADNSEvent**：[Event.h:L126-L179](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Event.h#L126-L179)。`processEvents(events)`（L148-L168）把 EventPoll 的抽象事件位翻译成 `readfd/writefd` 两个参数调 `resolver_->process()`，最后 `command_->setStatusActive()` 唤醒等待的命令——与 u2-l2 讲过的「只置位、不执行」完全一致。各 EventPoll 后端的 `addNameResolver`（如 [EpollEventPoll.cc:L222](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/EpollEventPoll.cc#L222)）把 `ADNSEvent` 挂到对应 fd 的 `SocketEntry` 上。

**同步兜底 NameResolver**：[NameResolver.cc:L50-L69](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/NameResolver.cc#L50-L69)，一次 `callGetaddrinfo`（[SocketCore.cc:L1442-L1454](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L1442-L1454)，全局 `ai_flags` 默认值的薄封装）加结果转数字串，失败抛 `NAME_RESOLVE_ERROR`。走这条路时 DNS 等待期间整个引擎停转——这就是异步 DNS 被列为 aria2 特性、且默认开启的原因。

#### 4.3.4 代码实践（本讲主实践任务）

1. **实践目标**：在真实运行中抓到「DNS 解析开始」与「结束」两条日志，并把它们精确对应到本节源码；用自己的话说明 getaddrinfo 被异步化的三个动作。
2. **操作步骤**：
   - 先确认你的 aria2 带异步 DNS：`./src/aria2c --version`，Features 行应包含 `Async DNS`（否则以下日志行为会退化成同步阻塞，见「预期结果」末尾）。
   - 直连下载一个域名（避开代理干扰；若必须走 `--all-proxy`，被解析的将是代理主机名，结论同样成立）：

     ```bash
     ./src/aria2c -l dns.log --log-level=info -d /tmp https://example.com/
     ```

   - 在 `dns.log` 里搜 `Resolving hostname` 与 `Name resolution complete`（两者文本定义在 [message.h:L60-L65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L60-L65)）。也留意 `DNS cache hit`——同一域名第二次下载时，`resolveHostname` 会直接命中缓存跳过 c-ares。
   - 换用 `--async-dns=false` 再跑一次，对比两条日志的时间差与下载启动延迟。
3. **需要观察的现象**：`Resolving hostname example.com`（来自 [AsyncNameResolverMan.cc:L82-L83](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolverMan.cc#L82-L83)，即 `startAsync`）在前；数百毫秒后出现 `Name resolution complete: example.com -> <IP 列表>`（来自 [AbstractCommand.cc:L816-L817](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L816-L817)）。IP 列表若同时含 IPv6 与 IPv4 地址，就是双栈两路查询都被汇总的证据；只有 IPv4 时，说明 `configureAsyncNameResolverMan` 关掉了 IPv6 路（或 AAAA 查询失败但 A 成功）。
4. **预期结果**：你能把两条日志分别钉到 `startAsync`（发起 `ares_getaddrinfo`，同时 EventPoll 里多出 c-ares 的 UDP fd）和 `resolveHostname` 的成功分支（`getStatus()` 已变 1，期间 c-ares 的 socket 事件由 `ADNSEvent::processEvents → ares_process_fd → callback` 链路驱动）。用 `--async-dns=false` 对比时，两条日志之间引擎完全阻塞，且 `--version` 无 Async DNS 特性的二进制只能走这条路。
5. 日志的具体时间差依网络而定，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：aria2 的异步 DNS 为什么不干脆开一个工作线程调 `getaddrinfo`？

**答案**：开线程会引入锁、生命周期与撤销难题（下载被取消时如何终止一个卡在内核里的线程？）。c-ares 方案让 DNS 查询本身变成「几个普通 socket 上的读写」，天然融入已有的单线程 EventPoll——取消一个查询只需销毁 resolver、注销事件，无任何线程同步问题；代价是引入 c-ares 这个编译期依赖（没有它就退回同步路径）。

**练习 2**：`AsyncNameResolverMan::getStatus()` 里 `(success && ipv4Success) || success == numResolver_` 这个条件为什么要写成两部分？

**答案**：后半部分 `success == numResolver_` 是「全部查询都成功」的常规情形；前半部分是特例——只要 IPv4（A 记录）那一路成功，哪怕 AAAA 还挂着没回，也立刻判成功。因为部分 DNS 服务器会丢弃 AAAA 查询，等它只会白白吃掉超时；而 A 查询响应可靠，所以不做反向的短路（注释见 [AsyncNameResolverMan.cc:L165-L169](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolverMan.cc#L165-L169)）。

**练习 3**：`InitiateConnectionCommand` 构造函数把超时设为 `--dns-timeout` 而不是 `--connect-timeout`，这说明什么？

**答案**：这条命令的生命周期只覆盖「解析域名 + 创建下一条命令」阶段（见 [InitiateConnectionCommand.cc:L65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommand.cc#L65)）；连接等待的 60 秒（`--connect-timeout`）是下一条命令 `ConnectCommand`/`BackupIPv4ConnectCommand` 的超时。u2-l3 讲过「命令接力」，这里正是每个阶段各自携带自己的超时参数的实例。

### 4.4 Happy Eyeballs 回退：BackupIPv4ConnectCommand

#### 4.4.1 概念说明

4.3 节的解析结果把 IPv6 地址排在列表前面（`getResolvedAddress` 的拼接顺序），`resolveHostname` 返回的首选地址通常是 IPv6。若对端 IPv6 其实不通，主连接要等 `--connect-timeout`（默认 60 秒）才失败。`BackupIPv4ConnectCommand` 就是 RFC 6555 的 aria2 实现：**主命令决定连接 IPv6 地址的同时，悄悄多派一条命令，延迟约 300 ms 后向缓存里的第一个 IPv4 地址发起第二条 TCP 连接；两条连接赛跑，先成功者胜**。

三个角色：

- `InitiateConnectionCommand::createBackupIPv4ConnectCommand`——决定「要不要备份」并派生命令；
- `BackupIPv4ConnectCommand`——独立参赛的备份连接；
- `ConnectCommand`——主连接的持有者，负责消费或取消备份。

它们之间通过一个共享的 `BackupConnectInfo`（含 `cancel` 标志、`ipaddr`、`socket` 三个字段）通信——注意这是 `shared_ptr` 挂起的裸共享状态，两条例并发命令读写它，但因为引擎是单线程的（u2-l1），不需要任何锁。

#### 4.4.2 核心流程

```text
InitiateConnectionCommand::createNextCommand（选定了 IPv6 首选地址 addr）
    └─ setupBackupConnection(hostname, addr, port, connectCommand)
        └─ createBackupIPv4ConnectCommand:
            ├─ addr 不是 IPv6？→ 返回空，不做备份
            └─ 在 DNS 缓存里找第一个 IPv4 地址
                 → 创建 BackupIPv4ConnectCommand 入队
                 → 返回 BackupConnectInfo 给 ConnectCommand

BackupIPv4ConnectCommand::execute()（每轮被调度）:
    ├─ 下载已结束 / 已 halt → 退出
    ├─ info_->cancel（主连接赢了）→ 打日志退出
    ├─ socket_ 已建立且可写:
    │     ├─ SO_ERROR==0 → 备份赢了：socket 移交 info_，
    │     │     mainCommand 置 ONESHOT_REALTIME + setNoWait 立即唤醒 → 退出
    │     └─ SO_ERROR!=0 → 退出
    ├─ socket_ 还没建：距启动 ≥300ms 才 establishConnection + 注册写检查
    └─ 否则 addCommandSelf() 等下一轮

ConnectCommand::executeInternal():
    ├─ info_->ipaddr 非空（备份先赢）→ markBadIPAddress(IPv6) +
    │     改写 Request 地址 + swapSocket(备份 socket)
    ├─ checkIfConnectionEstablished：SO_ERROR 判定主连接
    │     └─ 主连接成功 → info_->cancel = true（叫停备份）
    └─ 进入协议命令链
```

#### 4.4.3 源码精读

**发起点**：[InitiateConnectionCommand.cc:L131-L163](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommand.cc#L131-L163)。[L139-L142](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommand.cc#L139-L142) 用 `inetPton(AF_INET6, ipaddr)` 判定首选地址是否 IPv6，不是就直接返回空 `info`——**只有首选 IPv6 时才做 IPv4 备份**（反方向不做，理由与 4.3 的 A/AAAA 策略一致：IPv4 连通性几乎总是有）。[L144-L160](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommand.cc#L144-L160) 从 DNS 缓存（`findAllCachedIPAddresses`）里挑第一个 IPv4 地址，创建 `BackupIPv4ConnectCommand`（用引擎新分配的 CUID）入队。[L165-L174](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommand.cc#L165-L174) 的 `setupBackupConnection` 把返回的 `info` 交给主命令 `ConnectCommand` 持有。

**备份命令**：[BackupIPv4ConnectCommand.cc:L50-L67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BackupIPv4ConnectCommand.cc#L50-L67) 构造时记录启动时刻并把超时设为 `--connect-timeout`（默认 60 秒，[OptionHandlerFactory.cc:L835](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L835)）；析构（[L69-L76](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BackupIPv4ConnectCommand.cc#L69-L76)）注销写事件检查。

核心状态机在 [BackupIPv4ConnectCommand.cc:L78-L146](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BackupIPv4ConnectCommand.cc#L78-L146) 的 `execute()`：

- [L117-L135](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BackupIPv4ConnectCommand.cc#L117-L135)：socket 尚未创建时，只有距 `startTime_` 超过 **300 ms**（[L118-L122](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BackupIPv4ConnectCommand.cc#L118-L122)，RFC 6555 的推荐值）才真正 `establishConnection`（对 IPv4 数字地址）并注册写事件检查。源码注释坦承：由于 DownloadEngine 的周期刷新机制，实际间隔通常接近 1 秒而非精确 300 ms。
- [L89-L115](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BackupIPv4ConnectCommand.cc#L89-L115)：socket 可写后读 `getSocketError()`：为空（连接成功）则把 `ipaddr` 与 **socket 本体**移入共享的 `info_`，随后 `mainCommand_->setStatus(STATUS_ONESHOT_REALTIME)` 加 `e_->setNoWait(true)` 让主命令在引擎下一轮立即被执行（不进下一轮 poll 等待）——这是跨命令「插队唤醒」的手法；非空则记录失败退出。
- [L142-L144](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BackupIPv4ConnectCommand.cc#L142-L144)：没有终局就 `addCommandSelf()` 自我重排，等下一轮调度再检查——再次印证 u2-l3 的「返回 false 必须已重排」约定。

**消费与取消**：[ConnectCommand.cc:L87-L113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConnectCommand.cc#L87-L113) 的 `executeInternal()`：

- [L89-L101](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConnectCommand.cc#L89-L101)：若备份已抢先成功（`info_->ipaddr` 非空）：把原 IPv6 地址 `markBadIPAddress` 进 DNS 缓存、改写 `Request` 的连接地址记录、`swapSocket` 换上备份的 socket——对上层协议命令而言就像主连接本来就连到了 IPv4，完全无感。
- [L102-L110](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConnectCommand.cc#L102-L110)：主连接自身先完成（`checkIfConnectionEstablished` 通过）则 `info_->cancel = true` 叫停备份命令（备份命令下轮 `execute` 在 [BackupIPv4ConnectCommand.cc:L84-L88](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BackupIPv4ConnectCommand.cc#L84-L88) 看到 cancel 打日志退出）。
- 另外 `ConnectCommand` 析构（[ConnectCommand.cc:L63-L68](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConnectCommand.cc#L63-L68)）也会置 `cancel`——主命令因任何原因死亡都不能留下孤儿备份连接。

#### 4.4.4 代码实践

1. **实践目标**：在日志中完整复现一次 IPv6→IPv4 回退（或确证它没有发生），并与源码状态机对齐。
2. **操作步骤**：
   - 选一个同时有 AAAA/A 记录的域名（例如很多 CDN 站点），在双栈机器上运行：

     ```bash
     ./src/aria2c -l he.log --log-level=info -d /tmp https://<双栈域名>/
     grep -E "Searching IPv4|Issue backup connection|Backup connection|Use backup connection" he.log
     ```

   - 日志文本分别对应 [InitiateConnectionCommand.cc:L143](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommand.cc#L143)、[L155-L157](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommand.cc#L155-L157)、[BackupIPv4ConnectCommand.cc:L84-L108](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BackupIPv4ConnectCommand.cc#L84-L108)、[ConnectCommand.cc:L89-L91](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConnectCommand.cc#L89-L91)。
   - 若你的网络 IPv6 通畅，只会看到 `Issue backup connection` 而**没有**后续成功/失败行（备份命令被 cancel 静默终止时打印 `Backup connection canceled`）；想强制触发回退，可用 `--interface` 绑定一个 IPv6 不通的出口，或挑一个 IPv6 半通的站点。
3. **需要观察的现象**：`Searching IPv4 address for backup connection attempt` 只在首选地址为 IPv6 时出现；一旦出现 `Use backup connection address`，说明备份赢了，主命令换用了备份 socket。
4. **预期结果**：能画出两条例并发的时间线：主连接（IPv6）与 300 ms 后出发的备份连接（IPv4），终局只有一条 socket 存活。是否触发依赖网络环境，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`BackupConnectInfo` 被两条并发命令共享读写，为什么不需要互斥锁？

**答案**：因为 aria2 引擎是单线程事件驱动的（u2-l1）：`BackupIPv4ConnectCommand::execute` 与 `ConnectCommand::executeInternal` 永远不会同时执行，只会先后被主循环调度。所谓「并发」是**逻辑并发**（两条连接同时在内核里握手），不是多线程执行——共享状态天然串行访问。

**练习 2**：备份连接成功后，为什么除了移交 socket 还要 `mainCommand_->setStatus(STATUS_ONESHOT_REALTIME)` 和 `e_->setNoWait(true)` 两个动作？

**答案**：主命令 `ConnectCommand` 当时可能已因没有事件而随引擎一起阻塞在 EventPoll 的 `poll()` 里。置 `ONESHOT_REALTIME` 让它下次调度时必被执行（不排队等超时刷新），`setNoWait(true)` 则让 DownloadEngine 本轮不进入长等待、立刻再跑一圈——两者合起来保证「备份赢了」这个消息被尽快消费，回退延迟不被调度机制放大（见 [BackupIPv4ConnectCommand.cc:L100-L101](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BackupIPv4ConnectCommand.cc#L100-L101)）。

**练习 3**：如果把首选地址改成 IPv4（例如 `--disable-ipv6` 之后），`createBackupIPv4ConnectCommand` 会怎样？

**答案**：[InitiateConnectionCommand.cc:L139-L142](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommand.cc#L139-L142) 的 `inetPton(AF_INET6, ipaddr)` 对 IPv4 字符串返回 -1，函数直接返回空 `info`，不会创建任何备份命令——Happy Eyeballs 只做「IPv6 优先、IPv4 兜底」这一个方向。

## 5. 综合实践

**任务：为一次 HTTPS 下载绘制「网络底座时间线」并把每条日志钉到源码行。**

1. 准备：构建带 c-ares 的 aria2（`aria2c --version` 确认 Features 含 `Async DNS`），选一个双栈域名。
2. 运行：

   ```bash
   ./src/aria2c -l timeline.log --log-level=info -d /tmp https://<双栈域名>/bigfile
   ```

   再跑第二遍（同域名），然后跑一遍 `--async-dns=false`，最后跑一遍 `--disable-ipv6`。
3. 从四次日志中各提取与本讲相关的行，按时间排成一张表，左列是日志原文，右列填它的产生位置（提示：`Resolving hostname` → `AsyncNameResolverMan::startAsync`；`Name resolution complete` / `DNS cache hit` → `AbstractCommand::resolveHostname`；`Searching IPv4 address…`/`Issue backup connection…` → `InitiateConnectionCommand::createBackupIPv4ConnectCommand`；`Backup connection …` → `BackupIPv4ConnectCommand::execute`；`Use backup connection address` → `ConnectCommand::executeInternal`）。
4. 回答三个问题并写进笔记：
   - 第二遍为什么没有 `Resolving hostname`？（DNS 缓存命中路径）
   - `--async-dns=false` 时 `Resolving` 与 `complete` 之间引擎在做什么？谁来承受阻塞？（同步 `NameResolver`，整条命令链所在的主线程）
   - `--disable-ipv6` 之后哪些日志消失了？对应源码里的哪个判断？（`configureAsyncNameResolverMan` 关 ipv6_，以及 `createBackupIPv4ConnectCommand` 的 IPv6 判定）
5. 交付物：一页时间线笔记 + 一张「日志文本 ↔ 源码行」对照表。所有时序数值**待本地验证**，不同网络差异很大，这正是做这张表的乐趣所在。

## 6. 本讲小结

- `SocketCore` 用文件头一组宏（`SOCKET_ERRNO`/`A2_EINPROGRESS`/`CLOSE`）吸收 POSIX/WinSock 差异，向上提供统一的连接、读写与错误查询接口；`establishConnection` 的要点是「先切非阻塞再 connect」，把 `EINPROGRESS` 当作正常返回。
- 非阻塞读写的 EAGAIN 被 `readData`/`writeData` 翻译成「返回 0 + `wantRead_`/`wantWrite_` 留言」，由上层据此向 EventPoll 注册读/写事件——这是 u2-l2「只置位、不执行」约定在网络层的落点；`SocketRecvBuffer` 用 16 KB 滑动窗口摊薄系统调用开销。
- 异步 DNS 不靠线程：c-ares 把一次解析拆成「`ares_getaddrinfo` 发起 → `sock_state_cb` 报告待监听 fd → `ADNSEvent::processEvents` 里 `ares_process_fd` 驱动 → `callback` 收结果」四步，全部跑在既有的事件循环里；`AsyncNameResolverMan` 双栈并发 AAAA/A，且 A 成功即不等 AAAA 以规避 AAAA 黑洞。
- 命令链入口 `AbstractCommand::resolveHostname` 统一处理「数字 IP 直通 → DNS 缓存 → 异步/同步解析 → 回写缓存」；查询进行中命令自我 requeue，整个 DNS 阶段受 `--dns-timeout`（默认 30 秒）保护。
- Happy Eyeballs（RFC 6555）由 `InitiateConnectionCommand`（判定与派生）、`BackupIPv4ConnectCommand`（300 ms 延迟赛跑）、`ConnectCommand`（消费或取消）三方通过共享 `BackupConnectInfo` 协作完成，单线程模型让这个共享状态免锁。

## 7. 下一步学习建议

- 下一讲 u3-l1 将进入任务层（`RequestGroup`/`RequestGroupMan`），看多个下载任务如何共享本讲与 u2 讲的这套引擎与网络底座；建议先回顾 u2-l3 的命令接力图，因为 `InitiateConnectionCommand` 创建的下一条命令正是 `HttpInitiateConnectionCommand`（u4-l1 的主角）。
- 想继续深挖本讲主题的读者：读 [src/EpollEventPoll.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/EpollEventPoll.cc) 中 `addNameResolver`/`deleteNameResolver` 的实现，看 `ADNSEvent` 如何挂到 `SocketEntry` 上；再看 [src/DNSCache.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DNSCache.h) 的 `markBad`/`getGoodAddr`，理解坏地址标记如何与 `checkIfConnectionEstablished` 的换地址重连联动。
- BT 侧同样的底座有另一组使用者：`PeerInitiateConnectionCommand`（[src/PeerInitiateConnectionCommand.cc:L80](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInitiateConnectionCommand.cc#L80) 也调 `establishConnection`）与 `DHTEntryPointNameResolveCommand`（用同一套 `AsyncNameResolverMan` 做 DHT 自举解析），学完 u6/u7 后回头对照，会发现本讲的机制被原样复用。
