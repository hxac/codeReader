# SocketCore 与异步 DNS：网络底座

## 1. 本讲目标

前两讲我们看清了 DownloadEngine 的事件循环（u2-l1）、EventPoll 的多路复用抽象（u2-l2）和 Command 的生命周期（u2-l3）。但它们都停留在「命令与事件」这一层——真正把字节写到网线上的是 socket，真正把域名变成 IP 地址的是 DNS。本讲下探到 aria2 的网络最底层，学完后你应当能够：

1. 解释 SocketCore 如何把 BSD socket 包装成对象，特别是**非阻塞连接**（`connect` 返回 `EINPROGRESS` 后怎么办）与 `wantRead_`/`wantWrite_` 标志位契约。
2. 跟踪一次域名解析的完整路径：`AbstractCommand::resolveHostname` → DNS 缓存 → `AsyncNameResolverMan` 双栈（IPv6+IPv4）并行解析 → c-ares 异步回调，并理解「拿到 IPv4 结果就不再等 IPv6」的取舍。
3. 描述 Happy Eyeballs（RFC 6555）回退策略：主连接走 IPv6 的同时，`BackupIPv4ConnectCommand` 延迟 300ms 发起 IPv4 备份连接，谁先成功用谁。

## 2. 前置知识

### 2.1 BSD socket API 最小集

Unix 世界里所有网络通信都从这几个系统调用出发：

| 调用 | 作用 |
| --- | --- |
| `socket(family, type, protocol)` | 创建一个套接字文件描述符（fd） |
| `connect(fd, addr, len)` | 发起 TCP 连接 |
| `send(fd, buf, len, 0)` / `recv(fd, buf, len, 0)` | 收发数据 |
| `bind(fd, addr, len)` | 把套接字绑到本地地址/端口 |
| `getsockopt(fd, SOL_SOCKET, SO_ERROR, ...)` | 查询套接字上滞留的错误码 |
| `close(fd)` | 关闭 |

Windows 的 WinSock 也有同名函数（`closesocket`、`WSASend` 等），aria2 用一组宏（`CLOSE`、`SOCKET_ERRNO`、`A2_EWOULDBLOCK`）抹平两者差异，见 [src/SocketCore.cc:71-100](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L71-L100)。

### 2.2 阻塞与非阻塞：EWOULDBLOCK 与 EINPROGRESS

默认情况下 socket 是**阻塞**的：`recv` 在没有数据时会一直睡眠，`connect` 在握手完成前不返回。这对单线程事件驱动程序是灾难——一个慢连接会卡住整个下载引擎。解决办法是把 fd 设为非阻塞（`fcntl` 加 `O_NONBLOCK`），此后：

- `send`/`recv` 无进展时立即返回 -1 并把 errno 置为 `EWOULDBLOCK`（Windows 是 `WSAEWOULDBLOCK`）——「现在做不了，稍后再试」。
- `connect` 无法立即完成时返回 -1 且 errno 为 `EINPROGRESS`——连接已在后台进行，稍后通过 fd 变为「可写」来得知结果，再用 `getsockopt(SO_ERROR)` 查询成败。

### 2.3 DNS 与 getaddrinfo

域名解析最常用的接口是 `getaddrinfo(host, service, hints, &res)`：输入域名，输出一条 `addrinfo` 链表（同一域名可能对应多个 IPv4/IPv6 地址）。问题在于**它是阻塞的**——一次解析可能花几秒，期间整个事件循环都停摆。把阻塞调用变异步有两条路：扔到线程池，或者用异步 DNS 库。aria2 的选择是 c-ares（编译期 `ENABLE_ASYNC_DNS` / `HAVE_LIBCARES`），由它在内部自建 UDP socket 与 DNS 服务器对话。

### 2.4 与前几讲的衔接

回忆 u2-l2 的结论：EventPoll 的事件回调「只置位、不执行」，真正干活的是 Command；其中 `ADNSEvent` 专门服务 c-ares 的 socket。本讲就从被置位的 `wantRead_`/`wantWrite_` 和 `ADNSEvent` 背后的实现讲起。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/SocketCore.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc) / `src/SocketCore.h` | 对 BSD socket 的面向对象封装：建连、非阻塞读写、错误查询、多播、TLS/SSH 会话挂载点 |
| [src/SocketRecvBuffer.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketRecvBuffer.cc) / `src/SocketRecvBuffer.h` | 16KB 固定接收缓冲，所有协议命令共用的读缓冲 |
| [src/NameResolver.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/NameResolver.cc) | 同步版域名解析（无 c-ares 或 `--async-dns=false` 时的退路） |
| [src/AsyncNameResolver.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolver.cc) | 对 c-ares channel 的包装：发起查询、接收回调、暴露 c-ares socket 的事件需求 |
| [src/AsyncNameResolverMan.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolverMan.cc) | 管理最多两个解析器（IPv6 + IPv4）的并发查询与状态归并 |
| [src/BackupIPv4ConnectCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BackupIPv4ConnectCommand.cc) | Happy Eyeballs 的 IPv4 备份连接命令 |
| 辅助：`src/AbstractCommand.cc`（`resolveHostname`）、`src/InitiateConnectionCommand.cc`、`src/ConnectCommand.cc`、`src/HttpInitiateConnectionCommand.cc`、`src/DownloadEngine.cc` | 把上述底座接入命令链与事件循环的胶水代码 |

## 4. 核心概念与源码讲解

### 4.1 SocketCore：BSD socket 的统一封装

#### 4.1.1 概念说明

aria2 的一切网络活动——HTTP/FTP/SFTP 控制连接、BT peer 连接、DHT 的 UDP socket、RPC 的监听 socket——都通过 `SocketCore` 这一个类进行。它解决三个问题：

1. **可移植**：用宏屏蔽 POSIX 与 WinSock 的差异（错误码、close 函数、非阻塞设置方式）。
2. **非阻塞友好**：所有读写方法遇 `EWOULDBLOCK` 不抛异常，而是返回 0 并置 `wantRead_`/`wantWrite_` 标志，把「等什么事件」的决定权交还调用方。
3. **可叠加安全层**：同一套 `readData`/`writeData` 接口，底下可能是裸 socket、TLS 会话（`ENABLE_SSL`）或 SSH 会话（`HAVE_LIBSSH2`），调用方无感。

注意 `SocketCore` 持有不少**静态（进程级）状态**：`protocolFamily_`（地址族偏好，`AF_UNSPEC` 起）、`bindAddrs_`（`--interface` 绑定的本地地址）、接收缓冲大小等，见 [src/SocketCore.cc:130-137](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L130-L137)。其中 `protocolFamily_` 在启动阶段被设置：`--disable-ipv6` 时 Context 会把它定为 `AF_INET`（[src/Context.cc:222](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L222)），之后所有解析与建连都倾向 IPv4。

#### 4.1.2 核心流程

一次出站 TCP 连接的时序：

```text
establishConnection(host, port)
  ├── closeConnection()                 # 释放旧 fd
  ├── callGetaddrinfo(host, ...)        # host 通常是已解析好的 IP 字符串，立即返回
  ├── 遍历 addrinfo 链表（每个候选地址）：
  │     ├── socket() + FD_CLOEXEC + SO_REUSEADDR
  │     ├── (可选) bind() 到 --interface 指定的本地地址
  │     ├── setNonBlockingMode()        # fcntl 加 O_NONBLOCK
  │     ├── (可选) TCP_NODELAY
  │     └── connect()
  │           ├── 立即成功 → 完成
  │           └── -1 且 errno==EINPROGRESS → 返回，等 fd 可写
  └── 全部失败 → 抛 DL_ABORT_EX
```

连接结果的确认不在 SocketCore 里做，而由上层命令在收到**写事件**后调用 `getSocketError()`（`SO_ERROR`）完成：空字符串表示连接成功，否则是失败原因。

读写接口的统一约定：

```text
writeData(data, len) / readData(data, len)
  ├── 先清 wantRead_ = wantWrite_ = false
  ├── 成功 → 返回字节数（readData 经引用参数 len 带出）
  └── EWOULDBLOCK → 返回 0，并置位：
        裸 socket:  recv 卡 → wantRead_；send 卡 → wantWrite_
        TLS 会话:   由 checkDirection() 决定 wantRead_ 或 wantWrite_
                    （TLS 重握手时"写"也可能需要等"读"）
```

#### 4.1.3 源码精读

**对象构造与初始状态。** 两个构造函数都走 `init()`，默认阻塞模式、无 TLS 状态，见 [src/SocketCore.cc:156-174](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L156-L174)——这行代码建立 `blocking_`、`wantRead_`、`wantWrite_` 三个核心标志的初值。

**建立连接。** [src/SocketCore.cc:427-509](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L427-L509) 是 `establishConnection` 全文。这段代码做了六件事：先 `closeConnection` 释放旧 fd；用 `callGetaddrinfo` 把 host 字符串转成 `addrinfo` 链（第 434 行，注意调用方传入的通常是**已经解析好的 IP 字符串**，数字地址不会触发 DNS 查询，所以这里的 getaddrinfo 是即时返回的）；然后对每个候选地址依次 `socket()`（444）、设 `SO_REUSEADDR`（452）、按需 `bind` 到 `--interface` 指定的本地地址（462-479，多接口时还会在 480-486 轮换 `bindAddrsList_`）；关键在 [src/SocketCore.cc:488-501](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L488-L501)：先 `setNonBlockingMode()` 再 `connect()`，且只有当 `connect` 失败**且**错误码不是 `A2_EINPROGRESS` 时才换下一个地址——`EINPROGRESS` 表示「连接正在后台进行」，这正是非阻塞连接的核心技巧。

**非阻塞切换。** [src/SocketCore.cc:596-632](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L596-L632)：POSIX 路径用 `fcntl(F_GETFL)` / `fcntl(F_SETFL, flags | O_NONBLOCK)`（EINTR 自动重试），Windows 路径用 `ioctlsocket(FIONBIO)`；`setBlockingMode` 则反向清除该位。

**带标志位的写。** [src/SocketCore.cc:799-839](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L799-L839)（`writeData`）：开头清两个标志；裸 socket 分支里 `send` 遇 `A2_WOULDBLOCK` 时不抛异常，而是 `wantWrite_ = true; ret = 0`（813-818），意思是「缓冲区满了，请等 fd 可写再叫我」；TLS 分支委托 `tlsSession_->writeData`，靠 `checkDirection()` 区分该等读还是等写（821-836）——这是裸 socket 不会出现的情形：TLS 握手/重握手期间，写操作可能需要先读入对端数据。

**带标志位的读。** [src/SocketCore.cc:841-901](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L841-L901)（`readData`）：优先级为 SSH 会话 > TLS > 裸 socket；裸分支 `recv` 遇 `A2_WOULDBLOCK` 则 `wantRead_ = true` 并把带出的 `len` 归 0（866-880）。注意返回值走引用参数 `len`，函数本身是 `void`。

**查询连接结果。** [src/SocketCore.cc:1256-1271](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L1256-L1271)（`getSocketError`）：`getsockopt(SO_ERROR)`，返回空串即成功——上层命令据此判定非阻塞 connect 的最终结果。

**谁消费 wantRead_/wantWrite_？** 以 HTTP 发送请求为例，[src/HttpRequestCommand.cc:130-131](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequestCommand.cc#L130-L131) 的 `setReadCheckSocketIf(getSocket(), getSocket()->wantRead())` 与 `setWriteCheckSocketIf(getSocket(), getSocket()->wantWrite())` 正是把这两个标志翻译成 EventPoll 事件注册的代码——SocketCore 说「我想读/想写」，命令就向引擎登记对应的关注事件，闭环回到 u2-l2 讲的事件模型。

#### 4.1.4 代码实践：用 strace 亲眼看见 EINPROGRESS

1. **实践目标**：验证 aria2 的 connect 确实是非阻塞的，并观察 `O_NONBLOCK` → `connect` → `EINPROGRESS` → `SO_ERROR` 的完整序列。
2. **操作步骤**（需要可访问外网的环境）：
   ```bash
   strace -o /tmp/aria2-trace.log -e trace=socket,connect,fcntl,getsockopt \
     ./src/aria2c -x1 http://speedtest.tele2.net/1MB.zip -d /tmp/a2test
   grep -n -E 'O_NONBLOCK|EINPROGRESS|SO_ERROR' /tmp/aria2-trace.log
   ```
3. **需要观察的现象**：`fcntl(fd, F_SETFL, O_RDWR|O_NONBLOCK)` 出现在 `connect` 之前；`connect` 返回 `-1 EINPROGRESS`；稍后出现一次 `getsockopt(SOL_SOCKET, SO_ERROR, ...)`。
4. **预期结果**：三条证据齐全，说明源码里 `setNonBlockingMode`（SocketCore.cc:490）与 `EINPROGRESS` 容忍（SocketCore.cc:494-495）在真实运行中生效；`SO_ERROR` 返回 0 表示连接成功。若你的环境走了代理或 IPv6 直连很快，某些步骤可能一闪而过，可换一个较慢的站点重试。**待本地验证**（strace 输出随内核版本与网络环境略有差异）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `establishConnection` 里对 `connect` 返回 `EINPROGRESS` 不算失败，而其他错误码就要换下一个地址？

**答案**：非阻塞 socket 上 TCP 三次握手无法立即完成时，内核会让 `connect` 返回 -1 并置 `EINPROGRESS`，表示「请求已受理，正在后台进行」；真正的结果要等 fd 可写后再查 `SO_ERROR`。其他错误码（如 `ECONNREFUSED`、`ENETUNREACH`）意味着这个地址已确定不可用，所以源码在 [SocketCore.cc:494-501](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L494-L501) 关掉 fd 并 `continue` 尝试下一个 `addrinfo`。

**练习 2**：`writeData` 在什么情况下会置 `wantRead_` 而不是 `wantWrite_`？

**答案**：在 TLS 会话上写入遇 `TLS_ERR_WOULDBLOCK` 时，由 `tlsSession_->checkDirection()` 判断（[src/SocketCore.cc:828-833](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L828-L833)）：TLS 库可能需要先**读入**对端数据（如重握手记录）才能继续写，此时等待的是读事件而非写事件。裸 socket 分支则只会置 `wantWrite_`。

### 4.2 SocketRecvBuffer：16KB 接收缓冲

#### 4.2.1 概念说明

非阻塞 `recv` 每次能读多少字节完全随缘（取决于对端发送节奏与本机缓冲），而协议解析（HTTP 头、BT 消息长度前缀）需要「攒够一段再看」。`SocketRecvBuffer` 就是一块挂在 `SocketCore` 上的 16KB 固定缓冲，用两个指针管理已用区间：

- `pos_`：已被消费者吃掉的前沿；
- `last_`：已从 socket 读入的后沿；
- `[pos_, last_)` 之间的 `getBufferLength()` 字节就是当前可解析的数据。

#### 4.2.2 核心流程

```text
recv()   : 从 last_ 起把缓冲剩余空间一次读满（readData 内部遇 EWOULDBLOCK 返回 0 并置 wantRead_）
drain(n) : 消费者确认吃掉 n 字节 → pos_ += n；若恰好 pos_ == last_ 则 truncateBuffer() 整体归零
bufferEmpty() ⇔ pos_ == last_
```

#### 4.2.3 源码精读

缓冲定义在 [src/SocketRecvBuffer.h:63-76](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketRecvBuffer.h#L63-L76)——`std::array<unsigned char, 16_k> buf_` 即 16KB（`16_k` 是 aria2 自带的字面量后缀，`a2functional.h` 提供）。`recv()` 的实现只有 10 行：[src/SocketRecvBuffer.cc:52-62](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketRecvBuffer.cc#L52-L62)，这段代码先算剩余容量（满则记一条 "Buffer full" 调试日志并返回 0），再调用 `socket_->readData(last_, n)`——注意 `n` 是入参出参，带回实际读取字节数，`last_` 随之前移。`drain()` 在 [src/SocketRecvBuffer.cc:64-71](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketRecvBuffer.cc#L64-L71)：只移动 `pos_` 不搬移数据，当区间清空时调用 `truncateBuffer` 把两个指针一起拨回数组开头，形成一块可循环复用的环形区域（线性版本）。

#### 4.2.4 代码实践：源码阅读型——找出谁在消费这块缓冲

1. **实践目标**：确认 SocketRecvBuffer 是所有协议命令共享的读缓冲入口。
2. **操作步骤**：在仓库根目录执行
   ```bash
   grep -rn "getSocketRecvBuffer()->recv()" src/ | head -20
   grep -rn "SocketRecvBuffer(" src/ | grep -v test | head
   ```
3. **需要观察的现象**：你会看到 `AbstractCommand`、`HttpConnection`、`PeerConnection`（BT）、`HttpServer`（RPC）等大量调用点；构造则集中在 `AbstractCommand` 与 `HttpServer`。
4. **预期结果**：得出结论——HTTP/FTP/BT/RPC 四条命令链都经由同一块 16KB 缓冲读取据，`drain` 的调用方（如 HTTP 头解析器）按消费量推进 `pos_`。这是纯阅读实践，无需编译运行。

#### 4.2.5 小练习与答案

**练习 1**：`recv()` 返回 0 有哪两种含义？调用方如何区分？

**答案**：一是缓冲已满（[SocketRecvBuffer.cc:54-58](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketRecvBuffer.cc#L54-L58) 的 "Buffer full" 分支），二是 `SocketCore::readData` 遇 `EWOULDBLOCK` 把 `n` 置 0（SocketCore.cc:876-879）。调用方用 `socket->wantRead()` 区分：为真说明是「暂时无数据，等读事件」；对端关闭则表现为 `readData` 返回 0 且 `wantRead()` 为假（可对照 [src/HttpServer.cc:178-180](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L178-L180) 中 `recv() == 0 && !wantRead() && !wantWrite()` 的 EOF 判定写法）。

**练习 2**：为什么 `drain(n)` 之后还要专门 `truncateBuffer`，而不是每次都把剩余数据往前搬？

**答案**：搬移是 O(n) 内存复制。aria2 的策略是只在缓冲**恰好被吃空**（`pos_ == last_`）时才把两个指针一起归零（[SocketRecvBuffer.cc:68-70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketRecvBuffer.cc#L68-L70)），平时让 `pos_` 单向前进，用「浪费头部空间」换取零拷贝；只要消费速度跟得上，缓冲会在每轮清空后自然复位。

### 4.3 异步 DNS：NameResolver、AsyncNameResolver 与 AsyncNameResolverMan

#### 4.3.1 概念说明

aria2 有两条解析路径，由编译期（是否链接 c-ares）和运行期（`--async-dns`，默认 `true`）共同决定：

- **同步路径**：`NameResolver::resolve` 直接调 `getaddrinfo`，一次调用阻塞到拿到结果为止。实现简单，但在解析期间整个单线程引擎停摆。
- **异步路径**：`AsyncNameResolver` 包装一个 c-ares channel。`resolve()` 只是向 channel 提交查询后立即返回；c-ares 用**自己的 socket**（通常是 UDP 53）与 DNS 服务器对话，并回调通知「我现在需要监听哪些 fd 的读写」；aria2 把这些 fd 注册进 EventPoll，事件到来时调用 `ares_process_fd` 驱动 c-ares 状态机前进，最终由回调把结果写回。

在这之上，`AsyncNameResolverMan` 再做两件事：

1. **双栈并行**：对同一域名**同时**发起 AF_INET6 和 AF_INET 两个查询（各自独立 resolver），谁先回来用谁，避免单栈猜测错误导致整站不可达。
2. **状态归并**：把两个 resolver 的状态压缩成三值——进行中 0 / 成功 1 / 全部失败 -1——交给上层命令轮询。

还要认识一位配角：**DNS 缓存**。`DownloadEngine` 持有 `dnsCache_`，同一 `(hostname, port)` 的解析结果会被缓存，还会通过 `markBad` 把连不上的地址降级，这既是性能优化也是「多地址轮流试错」的基础。

#### 4.3.2 核心流程

异步解析的完整闭环（承接 u2-l2 的 ADNSEvent）：

```text
InitiateConnectionCommand::executeInternal
  └── AbstractCommand::resolveHostname(addrs, hostname, port)
        ├── isNumericHost? → 直接返回（IP 无需解析）
        ├── 查 DNS 缓存 → 命中则直接用，log "DNS cache hit"
        └── 异步分支（--async-dns=true 且编译启用）：
              ├── 未启动 → AsyncNameResolverMan::startAsync
              │     ├── resolver[0] = AsyncNameResolver(AF_INET6)  # IPv6 在前
              │     ├── resolver[1] = AsyncNameResolver(AF_INET)
              │     └── ares_getaddrinfo 提交两个查询（立即返回）
              ├── getStatus() == 0（未完成）→ 返回空串
              │     └── InitiateConnectionCommand 见空串 → addCommandSelf 自旋等待
              ├── getStatus() == -1（全败）→ 抛 NAME_RESOLVE_ERROR
              └── getStatus() == 1（成功）→ 取地址、写入 DNS 缓存、返回首选 IP

c-ares 侧的事件桥：
  ares(sock_state_cb: "我要监听 fd X 的读/写")
    → AsyncNameResolver::handle_sock_state → 维护 socks_ 表
    → DownloadEngine::addNameResolverCheck → EventPoll::addNameResolver (ADNSEvent)
  DNS 应答到达 → ADNSEvent 触发命令
    → AsyncNameResolver::process(readfd, writefd) → ares_process_fd
    → c-ares 完成 → callback() → status_ = SUCCESS/ERROR, resolvedAddresses_ 填充
```

状态归并的关键取舍（源码注释原文意）：**只要 IPv4 查询成功就不再等 IPv6**——因为现实中不少 DNS 服务器会丢弃 AAAA 查询，等它只会白白超时；反过来 A 查询几乎不会被丢，所以不存在对称的「拿到 IPv6 就不等 IPv4」。

#### 4.3.3 源码精读

**同步退路。** [src/NameResolver.cc:50-69](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/NameResolver.cc#L50-L69)：`resolve` 一次 `callGetaddrinfo` 拿到链表，遍历并用 `getNumericNameInfo` 把每个 `sockaddr` 转成 IP 字符串塞进 `resolvedAddresses`，失败抛 `NAME_RESOLVE_ERROR`。这就是无 c-ares 构建下唯一的解析通道。

**c-ares 包装的构造与发起。** [src/AsyncNameResolver.cc:115-130](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolver.cc#L115-L130)：构造时 `ares_init_options` 注册 `sock_state_cb`（c-ares 借此告诉宿主「我的 socket 需要什么事件」），可选 `ares_set_servers_csv` 覆盖 DNS 服务器（对应 `--async-dns-server` 选项）。`resolve()` 在 [src/AsyncNameResolver.cc:134-143](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolver.cc#L134-L143)：把状态置 `STATUS_QUERYING` 后调 `ares_getaddrinfo(channel_, name, ...)`——这个调用**不阻塞**，只是登记查询；查询的地址族由 `hints.ai_family` 决定，正是双栈设计的支点。

**完成回调。** [src/AsyncNameResolver.cc:47-71](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolver.cc#L47-L71)：c-ares 拿到 DNS 应答后异步调用这个自由函数 `callback`，它把 `ares_addrinfo` 链逐个转成字符串存入 `resolvedAddresses_` 并置 `STATUS_SUCCESS`；失败或空结果置 `STATUS_ERROR`。注意回调发生在 `ares_process_fd` 调用栈内，也就是主线程事件循环里——没有任何额外线程。

**事件需求上报。** [src/AsyncNameResolver.cc:82-113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolver.cc#L82-L113)：`handle_sock_state` 把 c-ares 的通知翻译成 `EventPoll::EVENT_READ`/`EVENT_WRITE` 并在 `socks_` 向量里增删改对应 fd 的条目。而 [src/AsyncNameResolver.cc:186-195](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolver.cc#L186-L195) 的 `process(readfd, writefd)` 是反向通道：事件真的发生后，宿主调 `ares_process_fd` 让 c-ares 读报文、推进状态机、必要时触发上面的 callback。

**双栈编排。** [src/AsyncNameResolverMan.cc:68-94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolverMan.cc#L68-L94)：`startAsync` 先放 IPv6 resolver（索引 0）再放 IPv4（索引 1），注释点明用意——`getResolvedAddress`（96-107）按索引顺序收集，IPv6 地址因此排在列表前面，成为首选；随后打出 INFO 日志 `MSG_RESOLVING_HOSTNAME`（文案 `"CUID#%<id> - Resolving hostname %s"`，见 [src/message.h:60](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L60)）。`startAsyncFamily` 里每个 resolver 建好就立刻 `resolve` 并经 `setNameResolverCheck` 注册进引擎。

**状态归并。** [src/AsyncNameResolverMan.cc:145-179](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolverMan.cc#L145-L179)：`getStatus` 统计两个 resolver 的 SUCCESS/ERROR 数，核心判断在第 170 行 `(success && ipv4Success) || success == numResolver_`——拿到 IPv4 成功**或**全部成功都返回 1；全部失败返回 -1；否则 0。165-169 行的注释解释了不等 IPv6 的原因（DNS 服务器可能丢弃 AAAA 查询导致长超时）。

**与命令链的集成。** [src/AbstractCommand.cc:759-823](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L759-L823) 的 `resolveHostname` 是两条路径的分水岭：数字主机名直接短路（763-766）；DNS 缓存命中直接用并记 "DNS cache hit" 日志（768-774）；异步分支里 `getStatus()==0` 时返回**空字符串**（793-794），调用方 [src/InitiateConnectionCommand.cc:88-92](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommand.cc#L88-L92) 见到空串就 `addCommandSelf()` 把自己重新排队、`return false`——命令自旋等待 DNS，事件循环得以继续服务其他连接；成功路径（796-822）把所有结果 `cacheIPAddress` 进 DNS 缓存再返回 `findCachedIPAddress` 的首选值，并打出 `"Name resolution complete: %s -> %s"` 日志（文案见 [src/message.h:61-62](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L61-L62)）。

**引擎侧桥接与配置。** [src/DownloadEngine.cc:283-294](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L283-L294)：`addNameResolverCheck`/`deleteNameResolverCheck` 只是转发给 `eventPoll_->addNameResolver/deleteNameResolver`——正是 u2-l2 讲过的 ADNSEvent 注册口。DNS 缓存的四个操作在 [src/DownloadEngine.cc:531-554](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L531-L554)，全部委托 `dnsCache_` 的 `find/put/markBad/remove`。最后，[src/AsyncNameResolverMan.cc:202-221](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolverMan.cc#L202-L221) 的 `configureAsyncNameResolverMan` 决定双栈开关：本机没有 IPv4 地址就 `setIPv4(false)`（避免必然失败的查询），没有 IPv6 地址**或** `--disable-ipv6` 就 `setIPv6(false)`；地址探测来自 `net::checkAddrconfig`（[src/SocketCore.cc:1583-1721](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketCore.cc#L1583-L1721)，用 `getifaddrs`/`GetAdaptersAddresses` 枚举网卡并排除回环与链路本地地址）。

#### 4.3.4 代码实践：从日志里找到一次 DNS 解析的起止

1. **实践目标**：在真实下载的日志中定位 DNS 解析的开始与结束行，并把它们逐一对应到本节源码。
2. **操作步骤**：
   ```bash
   # 先确认二进制带异步 DNS（Features 列表应包含 "Async DNS"）
   ./src/aria2c --version | grep -o 'Async DNS'

   ./src/aria2c -d /tmp/a2test -o 1MB.zip \
     -l /tmp/a2-dns.log --log-level=info \
     http://speedtest.tele2.net/1MB.zip

   grep -nE 'Resolving hostname|Name resolution complete|DNS cache hit' /tmp/a2-dns.log
   ```
3. **需要观察的现象**：
   - `CUID#7 - Resolving hostname speedtest.tele2.net`（来自 `AsyncNameResolverMan::startAsync`，[AsyncNameResolverMan.cc:82-83](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolverMan.cc#L82-L83)）；
   - 随后 `CUID#7 - Name resolution complete: speedtest.tele2.net -> 90.130.70.73, 2a01:b280::73`（来自 `AbstractCommand::resolveHostname`，[AbstractCommand.cc:816-817](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L816-L817)）。若你的网络只有 IPv4，列表里就只有 IPv4 一项——正说明本机无 IPv6 时 `configureAsyncNameResolverMan` 关掉了 IPv6 查询。
4. **预期结果**：两行日志之间即一次完整异步解析。对照源码回答核心问题——`getaddrinfo` 是如何被异步化的：**aria2 不再直接调用阻塞的 `getaddrinfo`，而是把查询交给 c-ares（`ares_getaddrinfo` 立即返回），c-ares 自建 UDP socket 与 DNS 服务器对话，其 socket 的读写事件经 `handle_sock_state` → `EventPoll::addNameResolver` 进入主事件循环；应答到达后 `ares_process_fd` 驱动状态机，在 `callback` 中把结果写回 `resolvedAddresses_`**。期间发起查询的命令只是周期性被重新调度、轮询 `getStatus()`，事件循环从未阻塞。若想进一步验证，可加 `--async-dns=false` 重跑：此时日志里只剩 "Name resolution complete" 而没有 "Resolving hostname"，证明前者走 `NameResolver` 同步路径。**待本地验证**（日志中的 CUID 编号与解析到的地址以实际输出为准）。

#### 4.3.5 小练习与答案

**练习 1**：`AsyncNameResolverMan::getStatus()` 返回 0/1/-1 分别对应什么？上层命令对每种返回值各做什么？

**答案**：0 = 至少一个 resolver 仍在查询中；1 = 成功（拿到 IPv4 结果或全部成功）；-1 = 全部失败。上层 `AbstractCommand::resolveHostname`（782-804 行）对 -1 抛 `NAME_RESOLVE_ERROR` 异常（同时给 ServerStat 记错误），对 0 返回空字符串让 `InitiateConnectionCommand` 自旋重排，对 1 取出地址继续建连。

**练习 2**：为什么 `startAsync` 要把 IPv6 的 resolver 放在数组的第 0 位？

**答案**：`getResolvedAddress` 按索引 0→1 顺序收集地址（[AsyncNameResolverMan.cc:96-107](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AsyncNameResolverMan.cc#L96-L107)），IPv6 在前意味着当两个查询都成功时，IPv6 地址排在结果列表首位，成为 `findCachedIPAddress` 返回的首选连接地址——这体现了「IPv6 优先，IPv4 兜底」的双栈策略，也为下一节的 Happy Eyeballs 埋下伏笔。

**练习 3**：DNS 缓存在这套机制里扮演什么角色？

**答案**：`resolveHostname` 开头先查缓存（768-774 行），命中则完全跳过 c-ares；解析成功后又把全部结果写回缓存（818-820 行）。缓存条目还支持 `markBad`（连接失败时降级该地址）与 `remove`（全部失败时整条删除），配合 `InitiateConnectionCommand::executeInternal` 的重试逻辑实现「同一域名的多个地址轮流试错」。

### 4.4 Happy Eyeballs 回退：BackupIPv4ConnectCommand

#### 4.4.1 概念说明

「IPv6 优先」有个现实烦恼：不少网络的 IPv6 路由是坏的（能解析出 AAAA 记录却连不上），如果只试 IPv6，用户要等完整的连接超时（默认 `--connect-timeout=60` 秒）才会退回 IPv4。RFC 6555「Happy Eyeballs」给出的方案是：**优先发起 IPv6 连接，同时延迟一小段时间（推荐 300ms）再发起一个 IPv4 备份连接，两者赛跑，谁先成功用谁**。

aria2 的实现是一对协作对象：

- `BackupIPv4ConnectCommand`：一个独立调度的小命令，负责延迟发起并监视 IPv4 备份连接；
- `ConnectCommand`（主连接命令）：持有 `BackupConnectInfo` 共享状态，在自己的 `executeInternal` 里检查「备份连接是否已经赢了」。

`BackupConnectInfo` 是两者之间的信使，只有三个字段：`cancel`（主连接赢了，通知备份退出）、`ipaddr`/`socket`（备份赢了，把成果移交给主命令）。

#### 4.4.2 核心流程

```text
InitiateConnectionCommand（DNS 完成，首选地址 addr 是 IPv6）
  ├── HttpInitiateConnectionCommand::createNextCommand
  │     ├── establishConnection(addr /*IPv6*/, port)   # 主连接出发
  │     ├── new ConnectCommand(...)
  │     └── setupBackupConnection(hostname, addr, port, connectCommand)
  │           └── createBackupIPv4ConnectCommand:
  │                 若 addr 是 IPv6 → 从 DNS 缓存里挑一个 IPv4 地址
  │                 → new BackupIPv4ConnectCommand(新 CUID, ipv4, port, info, mainCommand)
  │                 → 引擎排队，返回 info 给 ConnectCommand

赛跑阶段（两条命令并行调度）：
  BackupIPv4ConnectCommand::execute
    ├── 首次执行：距 startTime 不足 300ms → 什么都不做，重排自己
    ├── 满 300ms → establishConnection(ipv4) + 注册写事件监听
    ├── 写事件到来 → getSocketError() 为空？
    │     ├── 是（备份赢）：info->ipaddr / info->socket 落盘，
    │     │        mainCommand->setStatus(STATUS_ONESHOT_REALTIME) 立即唤醒主命令
    │     └── 否：命令退出，比赛结束
    └── info->cancel 为真（主连接已赢）→ 直接退出

  ConnectCommand::executeInternal
    ├── 发现 info->ipaddr 非空（备份赢了）：
    │     markBadIPAddress(IPv6 地址) → 改写 Request 的连接信息 → swapSocket 接管备份 socket
    ├── 否则照常等主连接写事件 → checkIfConnectionEstablished(SO_ERROR)
    └── 主连接成功 → info->cancel = true 取消备份
```

#### 4.4.3 源码精读

**发起侧。** HTTP 命令链在 `createNextCommand` 里先对首选地址调用 `establishConnection`（[src/HttpInitiateConnectionCommand.cc:85](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpInitiateConnectionCommand.cc#L85) 的调用点位于 71-102 行的函数体内，两种代理方式分别在 101 与 133 行调用 `setupBackupConnection`）。`setupBackupConnection` 与 `createBackupIPv4ConnectCommand` 在 [src/InitiateConnectionCommand.cc:131-174](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommand.cc#L131-L174)：这段代码先用 `inetPton(AF_INET6, ...)` 判断首选地址确实是 IPv6（140 行，IPv4 首选时直接返回空指针、不搞备份），再从 DNS 缓存 `findAllCachedIPAddresses` 里找第一个 IPv4 地址，创建 `BackupIPv4ConnectCommand`（领取独立的新 CUID）压入引擎队列，并把 `BackupConnectInfo` 返回给主命令持有。

**备份命令本体。** [src/BackupIPv4ConnectCommand.cc:78-146](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BackupIPv4ConnectCommand.cc#L78-L146) 的 `execute` 是一个典型的自旋命令，按优先级处理四类情形：任务已结束/暂停 → 退出（81-83）；`info_->cancel` 为真（主连接赢了）→ 记日志退出（84-87）；socket 已创建且写事件就绪（89-115）→ 用 `getSocketError()` 判定结果，成功时执行移交三部曲——`info_->ipaddr = ipaddr_`、`info_->socket.swap(socket_)`、`mainCommand_->setStatus(STATUS_ONESHOT_REALTIME)` 加 `setNoWait(true)` 立即唤醒主命令（97-101）；socket 尚未创建（117-136）→ 检查是否已过 300ms 初始延迟，[src/BackupIPv4ConnectCommand.cc:117-135](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BackupIPv4ConnectCommand.cc#L117-L135) 中 121-122 行的 `startTime_.difference(...) >= std::chrono::milliseconds(300)` 即 RFC 6555 的推荐值（118-120 行的 TODO 注释坦承：受 DownloadEngine 状态刷新间隔影响，实际延迟通常接近 1 秒）；未到 300ms 则 `addCommandSelf` 重排（142-144）。顺带一个精确的源码观察：最后的 `else if (timeoutCheck_...)` 超时分支（137-141 行）在当前控制流里**不可达**——因为 `else if (!socket_)` 已在它之前覆盖了 `socket_` 为空的全部情形；`timeout_`/`timeoutCheck_` 字段（构造函数 61-63 行读取 `--connect-timeout`）因此实际不参与判定，阅读时不必为它分心。

**主命令侧的裁决。** [src/ConnectCommand.cc:87-113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConnectCommand.cc#L87-L113) 的 `executeInternal`：一进来先看 `backupConnectionInfo_->ipaddr` 是否非空（89-101）——非空说明备份连接已经赢了，于是 `markBadIPAddress` 把 IPv6 主地址在 DNS 缓存里记为劣迹地址，改写 `Request` 的连接信息为 IPv4，再 `swapSocket(backupConnectionInfo_->socket)` 无缝换上备份 socket；否则照常走 `checkIfConnectionEstablished`（102-106，内部查 `SO_ERROR`，失败时按 u2-l3 讲过的重试/换地址逻辑处理，见 [src/AbstractCommand.cc:835-865](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L835-L865)）；主连接一旦成功，立刻 `backupConnectionInfo_->cancel = true` 通知备份命令自行退场（107-110）。此外 `ConnectCommand` 的析构函数（[src/ConnectCommand.cc:63-68](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConnectCommand.cc#L63-L68)）也会置 `cancel`，保证主命令先死时备份命令不会拿着悬空指针继续跑——`BackupConnectInfo` 用 `shared_ptr` 共享正是为了延长这份状态的生命周期。

#### 4.4.4 代码实践：触发并观察一次备份连接

1. **实践目标**：在日志中捕捉 BackupIPv4ConnectCommand 的生命周期（创建 → 300ms 延迟 → 成功或取消）。
2. **操作步骤**：找一个同时有 A 与 AAAA 记录、且你所在网络 IPv6 可用的域名（如 `ipv6.test-ipv6.org` 或大型 CDN 站点），运行：
   ```bash
   ./src/aria2c -d /tmp/a2test -o page.html \
     -l /tmp/a2-he.log --log-level=info \
     https://www.google.com/
   grep -nE 'backup|Backup|Searching IPv4' /tmp/a2-he.log
   ```
3. **需要观察的现象**：可能出现的关键日志（均出自本节源码）——`Searching IPv4 address for backup connection attempt`（[InitiateConnectionCommand.cc:143](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommand.cc#L143)）、`Issue backup connection command CUID#..., addr=...`（155-157）、`Backup connection canceled`（[BackupIPv4ConnectCommand.cc:85-86](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BackupIPv4ConnectCommand.cc#L85-L86)）或 `Backup connection to ... established`（94-96）、以及主命令侧的 `Use backup connection address ...`（[ConnectCommand.cc:90-91](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConnectCommand.cc#L90-L91)）。
4. **预期结果**：IPv6 通畅时，你大概率只看到「Issue backup connection command」随后紧跟「Backup connection canceled」——主连接在备份尚未完成握手时就赢了，命令被干净地取消；只有当 IPv6 路由劣化（可临时用防火墙丢掉 v6 出站包模拟）才会看到 established 与 `Use backup connection address` 的接管序列。若日志里完全没有这些行，说明首选地址是 IPv4（`inetPton(AF_INET6, ...)` 判定失败），根本不会创建备份命令。**待本地验证**（能否触发取决于本机网络的双栈状况）。

#### 4.4.5 小练习与答案

**练习 1**：为什么备份命令要等 300ms 才发起连接，而不是与 IPv6 主连接同时出发？

**答案**：Happy Eyeballs 的意图是「IPv6 优先但快速回退」。同时出发会白白产生大量 IPv4 连接，增加服务端负担；等一小段时间（RFC 6555 建议的 \( t \approx 300\,\mathrm{ms} \)）通常足以让健康的 IPv6 路由完成握手，只有病态网络才真正需要备份。源码见 [BackupIPv4ConnectCommand.cc:118-122](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BackupIPv4ConnectCommand.cc#L118-L122)。

**练习 2**：备份连接成功后，为什么要把 `mainCommand_->setStatus(STATUS_ONESHOT_REALTIME)` 和 `e_->setNoWait(true)` 一起调用？

**答案**：主连接命令（ConnectCommand）此刻可能正以 INACTIVE 状态在队列里沉睡等待自己的写事件；备份成功只是往 `info` 里写了结果，主命令并不知道。`setStatus(STATUS_ONESHOT_REALTIME)` 强制它下一轮立即被调度执行（配合 `noCheck()` 返回真跳过对 IPv6 主 socket 的检查），`setNoWait(true)` 则让 DownloadEngine 的 `waitData` 不必等 I/O 事件、立刻进入下一轮命令执行——两者合力把「备份赢了」的消息以最小延迟送达主命令。见 [BackupIPv4ConnectCommand.cc:97-102](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BackupIPv4ConnectCommand.cc#L97-L102)。

**练习 3**：如果 `BackupIPv4ConnectCommand` 在 `mainCommand_` 析构之后仍被调度，会发生什么？aria2 如何规避？

**答案**：裸指针 `mainCommand_` 会悬空，解引用是未定义行为。规避手段是引用所有权方向反过来：备份命令退出（包括被 `info_->cancel` 打断）先于主命令消亡的保证并不存在，于是 `ConnectCommand` 的析构函数主动置 `backupConnectionInfo_->cancel = true`（[ConnectCommand.cc:63-68](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConnectCommand.cc#L63-L68)），备份命令在**下一次**被调度时看到 cancel 就直接返回 true 销毁自己、不再触碰 `mainCommand_`；而 `info` 本身由 `shared_ptr` 共享持有，生命周期长于双方，不存在悬空。

## 5. 综合实践

把本讲四块知识串成一条时间线。任务：下载一个双栈域名的文件，用日志还原「域名 → 两个 DNS 查询 → 地址排序 → IPv6 主连接 + IPv4 备份 → 裁决」的全过程。

```bash
mkdir -p /tmp/a2test
./src/aria2c -d /tmp/a2test -o probe.bin \
  -l /tmp/a2-full.log --log-level=info \
  https://speed.cloudflare.com/__down?bytes=1000000
```

然后按顺序完成：

1. **DNS 阶段**：`grep -nE 'Resolving hostname|Name resolution complete|DNS cache hit' /tmp/a2-full.log`，抄下解析结果列表，回答：列表第一项是 IPv4 还是 IPv6？为什么（提示：`startAsync` 的 resolver 顺序与 `getResolvedAddress` 的收集顺序）？
2. **连接阶段**：`grep -nE 'Connecting to|backup|Backup' /tmp/a2-full.log`，判断本次下载是否产生了备份连接命令、它的 CUID 是多少、最终是 established 还是 canceled。
3. **裁决阶段**：对照 4.4.3 的两个源码位置（`ConnectCommand.cc:89-101` 的移交分支与 `107-110` 的取消分支），判断本次走的是哪条分支，并用日志行号佐证。
4. **写一份时序笔记**：把上述日志行按时间排序，画成「InitiateConnectionCommand（自旋等 DNS）→ ConnectCommand(IPv6) ∥ BackupIPv4ConnectCommand(IPv4, 延迟 300ms) → 胜者继续 → 后续 HTTP 命令」的时序图。
5. **进阶（可选）**：加 `--disable-ipv6` 重跑一次，对比日志差异——预期 IPv6 resolver 不再创建（`configureAsyncNameResolverMan` 的 `setIPv6(false)`），也就没有任何备份连接。**待本地验证**。

## 6. 本讲小结

- **SocketCore** 是全项目唯一的 socket 封装：非阻塞连接的三段式（`setNonBlockingMode` → `connect` 容忍 `EINPROGRESS` → 写事件后查 `SO_ERROR`），读写遇 `EWOULDBLOCK` 不抛异常而是置 `wantRead_`/`wantWrite_` 标志，把「等什么事件」交回调用方，由命令翻译成 EventPoll 注册。
- **SocketRecvBuffer** 用 `pos_`/`last_` 两个指针管理一块 16KB 固定缓冲，`recv` 尽量读满、`drain` 按消费推进、吃空才整体归零，是所有协议命令共享的读缓冲。
- **异步 DNS** 分三层：`NameResolver`（同步退路）、`AsyncNameResolver`（c-ares channel 包装，查询经 `ares_getaddrinfo` 立即返回，其 socket 事件经 `handle_sock_state`/`ADNSEvent` 汇入主事件循环，结果在 `callback` 回写）、`AsyncNameResolverMan`（IPv6+IPv4 双栈并行，拿到 IPv4 成功即不等 IPv6）。
- **命令与 DNS 的耦合方式是「自旋 + 三值状态」**：`resolveHostname` 未完成时返回空串，`InitiateConnectionCommand` 见空串 `addCommandSelf` 重排自己，事件循环全程不阻塞；结果进 DNS 缓存，`markBad` 支持多地址轮流试错。
- **Happy Eyeballs** 由 `BackupIPv4ConnectCommand` 实现：首选 IPv6 时延迟 300ms 发起 IPv4 备份，两者赛跑；备份赢则经 `BackupConnectInfo` 移交 socket 并以 `STATUS_ONESHOT_REALTIME` 唤醒主命令，主连接赢则置 `cancel` 让备份退场。

## 7. 下一步学习建议

网络底座已经就位，下一讲 u3-l1「RequestGroup：一次下载任务的容器」将回到任务层，看多个连接、多个 URI 如何被组织成一个下载任务；而如果你想继续沿协议链路深入，u4-l1「HTTP 下载命令链」会把本讲的 `SocketCore::establishConnection` 之后的故事讲完——`ConnectCommand` 成功后如何交给 `HttpRequestCommand` 发出第一个请求。另外两个值得顺路一看的源码：`src/NameResolveCommand.cc`（BT/DHT 场景下独立的 DNS 命令，同样是 startAsync+自旋的模式）和 `src/DownloadEngine.h` 中的 DNS 缓存封装 `DNSCache`。
