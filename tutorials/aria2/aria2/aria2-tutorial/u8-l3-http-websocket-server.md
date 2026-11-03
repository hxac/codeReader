# 内嵌 HTTP 与 WebSocket 服务器

## 1. 本讲目标

前几单元里 aria2 一直是 HTTP 的「客户端」：向别人发请求、下载文件。加上 `--enable-rpc` 之后，aria2 摇身一变成为一个「HTTP 服务器」——监听端口、解析别人的请求、返回响应，甚至在同一条连接上升级为 WebSocket、主动把下载事件推送给客户端。webui-aria2、AriaNg 这些图形前端，靠的就是这套内嵌服务。

学完本讲，你应该能够：

1. 说清 RPC 服务器的完整装配链：`DownloadEngineFactory` 创建 `HttpListenCommand` → 绑定端口 → accept 连接 → 为每条连接派出命令链。
2. 解释 `HttpServerCommand` → `HttpServerBodyCommand` → `HttpServerResponseCommand` 三棒命令接力，以及 keep-alive 下这条链如何回到起点循环。
3. 跟踪一次 WebSocket 升级握手的每个判断分支（101/400/404/426），理解 `Sec-WebSocket-Accept` 的计算方式。
4. 描述一条系统通知（如 `aria2.onDownloadStart`）如何从 `RequestGroupMan` 出发、被序列化为 JSON、经 wslay 排队、最终写到客户端 socket——以及为什么推送方必须手动调用 `updateWriteCheck()`。

## 2. 前置知识

### HTTP 的服务器视角

u4-l2 里我们以客户端身份读过 HTTP。服务器视角的差别在于：

- **请求行 vs 状态行**：客户端发 `GET /jsonrpc HTTP/1.1`（请求行），服务器发 `HTTP/1.1 200 OK`（状态行）。同一个 `HttpHeaderProcessor` 解析器在 aria2 里有两个模式：`CLIENT_PARSER` 与 `SERVER_PARSER`，本讲用的是后者。
- **keep-alive（持久连接）**：HTTP/1.1 默认不关连接，响应发完可以继续在同一条连接上等下一个请求，省去重复建连与 TLS 握手的开销。
- **CORS 预检**：浏览器跨域发非简单请求前，会先发一个 `OPTIONS` 方法探测服务器是否允许；aria2 的 RPC 需要响应它，webui 才能从浏览器直连。

### WebSocket 协议速览（RFC 6455）

WebSocket 让一条 TCP 连接在握手后变成双向消息通道：

1. **升级握手**：客户端发一个普通 HTTP GET，带 `Upgrade: websocket`、`Connection: Upgrade`、`Sec-WebSocket-Key`（随机 16 字节的 Base64）、`Sec-WebSocket-Version: 13`。
2. **服务器应答 `101 Switching Protocols`**，并在 `Sec-WebSocket-Accept` 头里回敬一个「回执」：`Accept = Base64( SHA-1( 客户端Key ∥ "258EAFA5-E914-47DA-95CA-C5AB0DC85B11" ) )`，那个固定字符串是 RFC 6455 规定的 GUID，用来防止误把普通 HTTP 服务器当 WebSocket 用。
3. **握手后进入帧协议**：所有数据装在帧（frame）里，帧头含 FIN 位、操作码（opcode：0x1 文本、0x2 二进制、0x8 close、0x9 ping、0xA pong）、长度和掩码。客户端→服务器的帧必须掩码，服务器→客户端不掩码。文本/二进制是数据帧，ping/pong/close 是控制帧。

### wslay 库

[wslay](https://github.com/tatsuhiro-t/wslay) 是 aria2 作者自己写的 WebSocket 事件驱动库，只做协议层（组帧、解帧、掩码、控制帧自动应答），不做 I/O——你要给它注册 `recv_callback`/`send_callback` 等回调，由你提供 socket 读写。aria2 正是把 `SocketCore` 适配进这些回调。

### JSON-RPC 通知格式

RPC 请求有 `id` 字段、必须回应答；**通知（notification）没有 `id`，只有 `method` 和 `params`**。aria2 推送的事件形如：

```json
{"jsonrpc":"2.0","method":"aria2.onDownloadStart","params":[{"gid":"2089b05ecca3d829"}]}
```

### 承接前两讲

- **u2-l2**：EventPoll 的回调「只置位、不执行」——socket 就绪时命令被 `setStatusActive()` 唤醒，真正执行在 `executeCommand`。本讲大量命令靠注册读/写事件驱动，请带着这个模型读。
- **u4-l2**：`HttpHeaderProcessor` 的流式增量解析（`parse` → `getLastBytesProcessed` → `drain`）、`SocketBuffer` 的部分写、`SocketRecvBuffer` 共享读缓冲，全部在本讲复用。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/DownloadEngineFactory.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc) | `--enable-rpc` 时创建 `HttpListenCommand` 并绑定端口 |
| [src/HttpListenCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpListenCommand.cc) | 常驻监听命令：accept 新连接，为每条连接派出 `HttpServerCommand` |
| [src/HttpServer.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc) | 一条连接的服务端 HTTP 会话对象：收请求头/体、路由、构造响应、Basic 认证 |
| [src/HttpServerCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc) | 第一棒：读请求头，分流到 WebSocket 升级 / 401 / BodyCommand |
| [src/HttpServerBodyCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc) | 第二棒：收请求体，解析 JSON/XML-RPC 并执行 RPC 方法，生成响应文本 |
| [src/AbstractHttpServerResponseCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractHttpServerResponseCommand.cc) | 第三棒骨架：把响应写出去，写完回调 `afterSend()` |
| [src/HttpServerResponseCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerResponseCommand.cc) | 普通响应的 `afterSend`：keep-alive 则回到第一棒 |
| [src/WebSocketResponseCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketResponseCommand.cc) | 101 响应的 `afterSend`：创建 WebSocketSession，进入常驻交互命令 |
| [src/WebSocketSession.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc) | 单个 WebSocket 会话：wslay 回调适配、帧收发、JSON 增量解析 |
| [src/WebSocketInteractionCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketInteractionCommand.cc) | WebSocket 会话的常驻宿主命令 |
| [src/WebSocketSessionMan.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSessionMan.cc) | 会话集合管理 + 系统通知的构造与广播 |
| [src/Notifier.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Notifier.cc) | 全局事件分发单例：下载事件 → 所有监听器 |
| [src/RequestGroupMan.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc) | 下载事件的产生地（start/pause/complete/error/stop） |

## 4. 核心概念与源码讲解

### 4.1 监听入口：HttpListenCommand 与引擎装配

#### 4.1.1 概念说明

RPC 服务器的第一块积木是「监听 socket」。aria2 把它做成一个**常驻自循环命令** `HttpListenCommand`：注册监听 socket 的读事件（有新连接待 accept 时监听 socket「可读」），每次被唤醒就 accept 一条新连接、为它创建第一棒命令，然后把自己重新排回队列。这复用了 u2-l1 学过的 DownloadEngine 调度模型——服务器不另起线程，而是把「等连接」当成事件循环里的一类普通命令。

#### 4.1.2 核心流程

```text
DownloadEngineFactory::newDownloadEngine（--enable-rpc=true 时）
  ├─ 对 AF_INET（、AF_INET6，未禁 IPv6 时）各建一个 HttpListenCommand
  ├─ 逐个调 bindPort(port)：
  │    bind → beginListen → addSocketForReadCheck（注册读事件）
  │    全部 family 都失败 → 抛 "Failed to setup RPC server."
  └─ 成功者 addCommand 进入命令队列

HttpListenCommand::execute（每次监听 socket 可读时被唤醒）
  ├─ 引擎要收工？→ return true（自我销毁）
  ├─ serverSocket_->isReadable(0)?
  │    ├─ acceptConnection() → 设 TCP_NODELAY → 打日志
  │    ├─ e_->setNoWait(true)          # 让本轮循环别休眠，立刻处理新命令
  │    └─ addCommand(HttpServerCommand(新socket, secure))
  └─ addCommand(this); return false    # 常驻自循环
```

`isReadable(0)` 是**主动探测**（超时 0 的轮询式检查），不是阻塞等待——命令被唤醒的时机由 EventPoll 读事件决定（u2-l2 的「只置位、不执行」），这里只是确认「现在确实可读」。

#### 4.1.3 源码精读

引擎装配侧，[src/DownloadEngineFactory.cc:196-221](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc#L196-L221) 在 `--enable-rpc` 时先给出安全警告（既没 `--rpc-secret` 也没 `--rpc-user`），随后对 IPv4/IPv6 两个地址族各建一个监听命令：

- [src/DownloadEngineFactory.cc:208-217](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc#L208-L217)：`families[] = {AF_INET, AF_INET6}`，`--disable-ipv6` 时只试 IPv4；某族 `bindPort` 成功即 `addCommand`。两个都失败就抛异常，整个引擎初始化中止——RPC 模式下端口是命根子。
- `secure` 参数来自 `--rpc-secure`，一路透传给每条连接（是否走 TLS 由它决定）。

绑定与 accept 在 [src/HttpListenCommand.cc:92-116](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpListenCommand.cc#L92-L116)：

```cpp
int flags = 0;
if (e_->getOption()->getAsBool(PREF_RPC_LISTEN_ALL)) {
  flags = AI_PASSIVE;                 // --rpc-listen-all 才绑 0.0.0.0，否则仅本机
}
serverSocket_->bind(nullptr, port, family_, flags);
serverSocket_->beginListen();
e_->addSocketForReadCheck(serverSocket_, this);
```

`AI_PASSIVE` 决定了对外暴露还是仅本机可达，这是 RPC 服务的第一道安全边界。

accept 循环在 [src/HttpListenCommand.cc:66-90](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpListenCommand.cc#L66-L90)：

```cpp
if (serverSocket_->isReadable(0)) {
  std::shared_ptr<SocketCore> socket(serverSocket_->acceptConnection());
  socket->setTcpNodelay(true);        // RPC 是小包请求-应答，禁用 Nagle 降低延迟
  ...
  e_->setNoWait(true);
  e_->addCommand(make_unique<HttpServerCommand>(e_->newCUID(), e_, socket, secure_));
}
...
e_->addCommand(std::unique_ptr<Command>(this));  // 自我重排
return false;
```

注意结尾正是 u2-l3 的约定：`execute()` 返回 false 前必须把自己重排回队列。accept 失败只记 DEBUG 日志不退出——单条 accept 异常不该拖垮整个 RPC 服务。

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认监听命令的绑定行为与 `--rpc-listen-all` 的差别。
2. **操作步骤**：
   - 编译 aria2 后运行：`./src/aria2c --enable-rpc --rpc-secret=test123 -l rpc.log --log-level=info`；
   - 另开终端执行 `ss -tlnp | grep 6800`，记下绑定的地址；
   - Ctrl-C 停止，改用 `./src/aria2c --enable-rpc --rpc-secret=test123 --rpc-listen-all=true` 再查一次；
   - 打开 `rpc.log`，找 `IPv4 RPC: listening on TCP port 6800` 一行（对应 [HttpListenCommand.cc:108](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpListenCommand.cc#L108) 的 `A2_LOG_NOTICE`）。
3. **需要观察的现象**：默认只绑 `127.0.0.1:6800`；加 `--rpc-listen-all` 后变为 `0.0.0.0:6800`。日志中每次客户端连入出现 `RPC: Accepted the connection from ...`（[HttpListenCommand.cc:77-78](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpListenCommand.cc#L77-L78)）。
4. **预期结果**：两处观察都与源码分支一一对应。（ss 工具名在 Linux 为 `ss`，macOS 可用 `netstat -an | grep 6800`；若日志级别不合适看不到 NOTICE 行，请确认 `--log-level=info`。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 aria2 用「命令 + EventPoll 读事件」而不是一个专门的线程循环 `accept()`？
**答案**：aria2 是单线程 Reactor 架构（u2-l1/u2-l2）：所有 I/O 复用同一个事件循环，RPC 连接、下载连接、DHT 共享一个线程。线程化 accept 需要引入锁与跨线程同步，而命令化只需注册读事件，与其他命令自然协调。

**练习 2**：`--disable-ipv6` 时 `familiesLength` 为 1，如果不禁用且机器不支持 IPv6 会发生什么？
**答案**：IPv6 那次 `bindPort` 失败，仅在 [HttpListenCommand.cc:111-114](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpListenCommand.cc#L111-L114) 记 ERROR 日志并返回 false；只要 IPv4 成功，`ok` 仍为 true，不会抛异常。这就是「逐族尝试、至少一个成功」的设计。

### 4.2 HttpServer：服务端 HTTP 会话对象

#### 4.2.1 概念说明

`HttpServer` 是「一条连接的服务端 HTTP 状态」：持有 socket、读缓冲、写缓冲和头部解析器，职责是**收请求头 → 判定请求类型 → 收请求体 → 构造响应文本**。它本身不是 Command，而是被三棒命令共享的会话对象——keep-alive 的关键就在于此：同一个 `HttpServer` 跨请求复用，解析器、缓冲都不必重建。

它与 u4-l2 客户端侧的 `HttpConnection` 是镜像关系：`HttpConnection` 发请求收响应，`HttpServer` 收请求发响应；两边共用同一个 `HttpHeaderProcessor` 类，只是构造时传入不同模式。

#### 4.2.2 核心流程

```text
HttpServer（构造：SERVER_PARSER 模式的解析器）
  │
  ├─ receiveRequest()      # 请求头阶段（返回 true=头收完）
  │    ├─ recv 填充 SocketRecvBuffer（EOF 抛异常）
  │    ├─ headerProcessor_->parse(...)   # 流式增量解析（u4-l2 同款）
  │    ├─ 成功 → drain 消费量，setupResponseRecv() 路由
  │    │        解析 Content-Length；扫描 Accept-Encoding 记录是否收 gzip
  │    └─ 未收完 → drain 后返回 false（等下次数据）
  │
  ├─ receiveBody()         # 请求体阶段（返回 true=体收完）
  │    └─ 按 Content-Length 截取，writeData 进 lastBody_（DiskWriter）
  │
  ├─ feedResponse(...)     # 组「状态行+通用头+自定义头+空行+体」推入 SocketBuffer
  └─ feedUpgradeResponse() # 组 101 Switching Protocols 头（无体）
```

路由规则（`setupResponseRecv`）是 RPC 的「URL 分发表」：

| 方法 + 路径 | reqType | body 去向 |
|---|---|---|
| `GET /jsonrpc` | `RPC_TYPE_JSONP` | 无 body（参数在 query 里） |
| `POST /jsonrpc` | `RPC_TYPE_JSON` | `json::JsonDiskWriter`（增量解析 JSON） |
| `POST /rpc`（编译启用 XML-RPC 时） | `RPC_TYPE_XML` | `rpc::XmlRpcDiskWriter` |
| 其他 | `RPC_TYPE_NONE` | 无，最终回 404 |

#### 4.2.3 源码精读

构造函数见 [src/HttpServer.cc:63-77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L63-L77)，三件套与客户端侧完全同构：`SocketRecvBuffer`（共享读缓冲）、`SocketBuffer`（部分写缓冲）、`HttpHeaderProcessor`，唯一差别是传了 `HttpHeaderProcessor::SERVER_PARSER`（[HttpServer.cc:67-68](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L67-L68)），让解析器按「请求行」而非「状态行」起头。

请求头接收在 [src/HttpServer.cc:175-227](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L175-L227)：

- [HttpServer.cc:177-181](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L177-L181)：缓冲空则 `recv()`；读到 0 字节且并非 EWOULDBLOCK，说明对端关闭，抛 `EX_EOF_FROM_PEER`。
- [HttpServer.cc:183-188](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L183-L188)：增量 `parse`；头收全后 `getResult` 存入 `lastRequestHeader_`，再按 `getLastBytesProcessed()` 精确 `drain`——这正是 u4-l2 讲过的「解析进度外置」设计，天然支持半包。
- [HttpServer.cc:193-204](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L193-L204)：`Content-Length` 解析失败或为负直接抛异常断连。
- [HttpServer.cc:207-220](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L207-L220)：拆 `Accept-Encoding`，客户端声明收 gzip 则记 `acceptsGZip_ = true`，供响应侧决定是否压缩。

请求体接收在 [src/HttpServer.cc:229-249](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L229-L249)：按 `min(缓冲长度, ContentLength - 已消费)` 截段，写入 `lastBody_`（一个 `DiskWriter`——这里是**内存盘写入器**，JSON/XML 请求体在收的过程中被增量解析，不是落盘）。body 收满返回 true。

URL 路由在 [src/HttpServer.cc:369-400](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L369-L400) 的 `setupResponseRecv`，即上面的分发表。注意 [HttpServer.cc:381-384](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L381-L384) 的 `if (reqType_ != RPC_TYPE_JSON)` 判断：keep-alive 下连续 POST /jsonrpc 时**不重建** `JsonDiskWriter`，复用上一个请求的写入器（用完会 `reset()`）。

响应构造在 [src/HttpServer.cc:261-299](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L261-L299) 的 `feedResponse`：拼状态行（`getStatusString` 查表，[HttpServer.cc:82-172](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L82-L172)）、`Date`/`Expires`/`Cache-Control: no-cache`、可选的 `Content-Type`、CORS 头 `Access-Control-Allow-Origin`（`--rpc-allow-origin-all` 时为 `*`）、gzip `Content-Encoding`，最后 `Connection: close`（仅当不支持持久连接时，[HttpServer.cc:291-293](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L291-L293)）。头与体先后 `pushStr` 进 `SocketBuffer`，真正的发送由第三棒命令驱动。

Basic 认证在 [src/HttpServer.cc:321-343](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L321-L343)：解 `Authorization: Basic <base64>`，按 `:` 切出用户名口令，与 `setUsernamePassword` 预存的 **HMAC 摘要**做常数时间比较（[HttpServer.cc:341-342](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L341-L342)）——内存里不保存明文口令，比较用时与内容无关，防时序侧信道。未配置 `--rpc-user` 时 `username_` 为空、`authenticate()` 恒真，HTTP 层不设防（`--rpc-secret` 的令牌鉴权在 RPC 方法层，见 u8-l4）。

keep-alive 判定在 [src/HttpServer.cc:445-448](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L445-L448)：`keepAlive_ && 请求头 isKeepAlive()`——服务器侧没主动关 + 客户端头允许，两者都要成立。

#### 4.2.4 代码实践

1. **实践目标**：用 curl 验证 URL 路由表与错误响应。
2. **操作步骤**：
   ```bash
   ./src/aria2c --enable-rpc --rpc-secret=test123
   # A. 打错的路径
   curl -v http://localhost:6800/wrongpath -d '{}'
   # B. 正确路径、非法 JSON
   curl -v http://localhost:6800/jsonrpc -d '{bad json'
   # C. 普通调用对照
   curl http://localhost:6800/jsonrpc -d '{"jsonrpc":"2.0","id":"1","method":"aria2.getVersion","params":["token:test123"]}'
   ```
3. **需要观察的现象**：A 返回 HTTP 404（`RPC_TYPE_NONE` → [HttpServerBodyCommand.cc:313-316](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L313-L316) 的 `default` 分支）；B 返回 HTTP 200，body 是 `{"error":{"code":-32700,"message":"Parse error."},...}`（[HttpServerBodyCommand.cc:275-283](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L275-L283)）；C 正常返回版本信息。响应头里都有 `Cache-Control: no-cache`。
4. **预期结果**：三条响应分别命中分发表的不同格子。具体 JSON 错误文本以本地运行为准（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `receiveRequest` 里读到 0 字节会抛异常，而客户端 `DownloadCommand` 读到 0 字节多数时候只是正常收尾？
**答案**：服务器视角下，客户端发完请求前连接不应断开；`recv()==0` 且非 EWOULDBLOCK 意味着对端异常断连（EOF），请求不完整只能作废，抛 `EX_EOF_FROM_PEER` 让宿主命令退出销毁。客户端则由 `Content-Length` 明确知道何时收完，EOF 在收满后才出现，属正常。

**练习 2**：`acceptsGZip_` 为 true 时响应一定压缩吗？
**答案**：不一定。`feedResponse` 里的判断是 `supportsGZip()`（[HttpServer.cc:288-290](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L288-L290)），它是「客户端接受 gzip 且本进程编译了 zlib（`enableGZip()` 被调）」的与运算；两者缺一都不加 `Content-Encoding` 头。

### 4.3 命令循环：三棒接力与 keep-alive

#### 4.3.1 概念说明

一条 HTTP 连接的生命周期由三类命令接力，形态与 u4-l1 的下载命令链同构，只是方向反转：

1. **HttpServerCommand（第一棒）**：收请求头，做认证、判断 WebSocket 升级、检查请求体大小，然后把连接交给第二棒。
2. **HttpServerBodyCommand（第二棒）**：收请求体，解析并执行 RPC 方法（调 `rpc::processJsonRpcRequest` 等），把响应文本塞进 `HttpServer` 的发送缓冲，交给第三棒。
3. **HttpServerResponseCommand（第三棒）**：把缓冲写空；写完后若 keep-alive 成立，**用同一个 `HttpServer` 对象再造一个第一棒命令**，回到循环起点。

三棒共享 `httpServer_`（`shared_ptr`）、socket 与 cuid，所以日志里一条连接的请求全程同 cuid，方便追踪。

#### 4.3.2 核心流程

```text
HttpServerCommand::execute
  ├─（无数据可读且无缓冲）→ 30 秒空闲超时退出；否则自旋重排
  ├─ secure 模式：socket_->tlsAccept()（TLS 握手，未完成则重排等待）
  ├─ httpServer_->receiveRequest() 为 false → 头未收全，重排等待
  ├─ authenticate() 失败且非 OPTIONS
  │     → 401 + WWW-Authenticate，交给 HttpServerResponseCommand，结束
  ├─ 头含 Upgrade: websocket 且 Connection: upgrade
  │     → 走 4.4 的 WebSocket 升级分支，结束
  └─ 普通请求：Content-Length > --rpc-max-request-size → 直接退出
        → addCommand(HttpServerBodyCommand)，结束（return true）

HttpServerBodyCommand::execute
  ├─ receiveBody() 为 false → 重排等待（同样 30 秒超时）
  ├─ 方法是 OPTIONS → 回 CORS 预检响应（200 + Allow-* 头）
  └─ 按 reqType 分支：
       XML → XmlRpcDiskWriter::finalize → getMethod(methodName)->execute
       JSON/JSONP → JsonDiskWriter::finalize 出 ValueBase
                    Dict 单调用 / List 批量调用 / 其他 → -32600
                    → sendJsonRpcResponse（HTTP 码映射）→ 第三棒

HttpServerResponseCommand（骨架 AbstractHttpServerResponseCommand::execute）
  ├─ httpServer_->sendResponse()（SocketBuffer 部分写，可能一次写不完）
  ├─ 缓冲未空 → updateReadWriteCheck + 自旋重排（30 秒超时）
  └─ 缓冲已空 → afterSend()：
        keep-alive 成立 → addCommand(HttpServerCommand(复用 httpServer_))  ← 回到起点
        否则 → 什么都不做，连接自然销毁
```

#### 4.3.3 源码精读

第一棒构造与执行在 [src/HttpServerCommand.cc:62-85](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L62-L85) 与 [src/HttpServerCommand.cc:171-278](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L171-L278)。两个构造函数值得对比：第一个（新连接）创建全新 `HttpServer` 并注入 RPC 用户口令、CORS、gzip 配置；第二个（[HttpServerCommand.cc:87-98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L87-L98)，keep-alive 回环）只接收现成的 `httpServer_`——这正是连接复用的落点。执行体的关键分支：

- [HttpServerCommand.cc:182-192](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L182-L192)：`--rpc-secure` 时先完成 TLS 服务端握手（`tlsAccept` 幂等，握手未完则重排等写事件）。
- [HttpServerCommand.cc:201-210](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L201-L210)：Basic 认证失败回 `401`，同时 `disableKeepAlive()` 强制关连接——失败请求不复用。
- [HttpServerCommand.cc:246-253](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L246-L253)：`--rpc-max-request-size`（默认 2M）在进入第二棒**之前**拦截，超大请求直接丢弃连接，防止恶意大 body 占内存。

第二棒执行在 [src/HttpServerBodyCommand.cc:175-343](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L175-L343)：

- [HttpServerBodyCommand.cc:195-219](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L195-L219)：`OPTIONS` 预检分支——只有请求带 `Origin` 与 `Access-Control-Request-Method` 且服务器开了 CORS 时才回 `Access-Control-Allow-*` 头，否则回光秃秃的 200。预检**不经过认证**（见第一棒 [HttpServerCommand.cc:201-203](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L201-L203) 对 OPTIONS 的豁免）。
- [HttpServerBodyCommand.cc:254-311](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L254-L311)：JSON 分支。`JsonDiskWriter::finalize()` 产出 `ValueBase` 树；顶层是 `Dict` 走单调用，是 `List` 则逐项批量调用（JSON-RPC batch），两者都不是（如裸字符串）回 `-32600 Invalid Request.`；JSONP 模式（GET）则从 query 里取 `params` 参数解析。RPC 方法的分发与执行细节属于下一讲 u8-l4，本讲只需知道调用点在 `rpc::processJsonRpcRequest(jsondict, e_)`（[HttpServerBodyCommand.cc:286](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L286)）。

响应码映射在 [src/HttpServerBodyCommand.cc:103-134](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L103-L134) 的 `sendJsonRpcResponse`：RPC 成功码 0 → HTTP 200；方法执行错 1 → 400；`-32600` → 400；`-32601`（方法不存在）→ 404；其余 → 500。另外 [HttpServerBodyCommand.cc:147-159](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L147-L159) 有个防爆破细节：**未授权**的响应包一层 `DelayedCommand` 延迟 1 秒才发送，拖慢口令暴力尝试的节奏。

第三棒骨架在 [src/AbstractHttpServerResponseCommand.cc:98-135](https://github.com/aria2/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractHttpServerResponseCommand.cc#L98-L135)：

```cpp
ssize_t len = httpServer_->sendResponse();   // SocketBuffer::send()，部分写
...
if (httpServer_->sendBufferIsEmpty()) {
  afterSend(httpServer_, e_);                // 模板方法：子类决定下一步
  return true;
}
else { /* 超时检查；否则 updateReadWriteCheck + 自旋重排 */ }
```

普通响应的 `afterSend` 在 [src/HttpServerResponseCommand.cc:56-64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerResponseCommand.cc#L56-L64)：keep-alive 成立时用**第二个构造函数**创建新的 `HttpServerCommand`——循环闭合。模板方法模式让 WebSocket 的 101 响应能复用同一副发送骨架而替换「下一步」（见 4.4）。

#### 4.3.4 代码实践

1. **实践目标**：观察 keep-alive 回环与 `Persist connection` 日志。
2. **操作步骤**：
   ```bash
   ./src/aria2c --enable-rpc --rpc-secret=test123 -l rpc.log --log-level=info
   # curl 一条连接连发两个请求（URL 写两个即可复用连接）：
   curl -v http://localhost:6800/jsonrpc http://localhost:6800/jsonrpc \
        -H 'Content-Type: application/json' \
        -d '{"jsonrpc":"2.0","id":"1","method":"aria2.getVersion","params":["token:test123"]}' \
        -d '{"jsonrpc":"2.0","id":"2","method":"aria2.tellVersion","params":["token:test123"]}'
   ```
   然后打开 `rpc.log` 搜 `Persist connection` 与 `HTTP Server received request`。
3. **需要观察的现象**：一次 TCP 连接（curl 输出只出现一次 `Connected to ...`）内日志出现两个 `HTTP Server received request`，两者之间夹一行 `Persist connection`——对应 `afterSend` → 新 `HttpServerCommand` 的回环。
4. **预期结果**：请求头日志里第二段请求与第一段的 cuid 相同（连接级共享 `HttpServer` 与 cuid）。若 curl 版本行为不同导致每次都新建连接，可改用 `curl --keepalive` 或检查 curl 的连接复用输出（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `HttpServerBodyCommand` 构造时要 `setStatus(STATUS_ONESHOT_REALTIME)`（[HttpServerBodyCommand.cc:80](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L80)）？
**答案**：注释写明是为了处理 `Content-Length == 0` 的请求（如不带体的 GET/JSONP）。REALTIME 状态让命令下一轮立即执行而不必等 socket 事件；ONESHOT 表示执行一次后自动降级回 INACTIVE——后续若 body 未收完，靠读事件再唤醒。

**练习 2**：三棒命令里哪一棒「死亡率」最高？为什么？
**答案**：第二、三棒都是即抛即弃（`execute` 返回 true 销毁），但真正频繁销毁重建的是「每一棒」——与传统服务器「一个线程跟一条连接走完全程」不同，aria2 每个请求阶段都换命令，同一时刻一条连接只有一棒存活。`HttpServer` 对象才是跨请求的长寿者。

### 4.4 WebSocket：升级握手与 wslay 会话

#### 4.4.1 概念说明

WebSocket 连接的前半生是 HTTP：第一棒发现 `Upgrade: websocket` 头后不再走 BodyCommand，而是回一个 `101 Switching Protocols`；后半生交给 wslay——aria2 把 socket 读写适配成 wslay 回调，wslay 负责帧协议（解析帧头、处理掩码、自动应答 ping、组装完整消息），每收到一条完整文本消息就回调 aria2 去 `processJsonRpcRequest`。宿主命令 `WebSocketInteractionCommand` 取代了三棒接力，成为这条连接的**常驻自循环**大脑，直到 close 帧或 EOF。

#### 4.4.2 核心流程

```text
升级判定（HttpServerCommand::execute）
  头含 Upgrade: websocket 且 Connection: upgrade？
    ├─ websocketHandshake(header)：
    │     非 GET 或无 Sec-WebSocket-Key        → 400
    │     Sec-WebSocket-Version != "13"        → 426（回 Sec-WebSocket-Version: 13）
    │     路径不是 /jsonrpc                    → 404
    │     否则                                  → 101
    ├─ 101：serverKey = Base64(SHA1(clientKey + GUID))
    │       feedUpgradeResponse("websocket", "Sec-WebSocket-Accept: serverKey")
    │       → WebSocketResponseCommand（把 101 发出去）
    │         afterSend：new WebSocketSession(socket, e)
    │                    new WebSocketInteractionCommand(session, e, socket)
    │                    session->setCommand(command)；addCommand  ← 进入常驻循环
    └─ 400/404/426：普通 HttpServerResponseCommand 发完即止

WebSocketInteractionCommand::execute（每次读/写事件唤醒）
  ├─ onReadEvent()  = wslay_event_recv：
  │     经 recvCallback 从 SocketCore 读字节 → 解帧
  │     → onFrameRecvStart（控制帧？置 ignorePayload）
  │     → onFrameRecvChunk（数据帧载荷 parseUpdate 增量喂 JSON 解析器）
  │     → onMsgRecvCallback（消息完整）：
  │          parseFinal 出 ValueBase → Dict 单调用 / List 批量
  │          → addResponse → session->addTextMessage（wslay_event_queue_msg 入队）
  ├─ onWriteEvent() = wslay_event_send：把队列里的消息经 sendCallback 写 socket
  ├─ 任一返回 -1 → 会话终止（closeSent/closeReceived 决定日志措辞）
  ├─ finish()（既不想读也不想写）→ 正常结束
  └─ 否则 updateWriteCheck + 自旋重排
```

#### 4.4.3 源码精读

握手裁定是三个纯函数式的检查，见 [src/HttpServerCommand.cc:137-152](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L137-L152)：注意升级**只接受 `/jsonrpc` 路径**，`/rpc`（XML-RPC）没有 WebSocket 形态。

Accept 密钥计算在 [src/HttpServerCommand.cc:125-133](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L125-L133)：

```cpp
std::string src = clientKey;
src += "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";   // RFC 6455 GUID
unsigned char digest[20];
message_digest::digest(digest, sizeof(digest), MessageDigest::sha1().get(),
                       src.c_str(), src.size());
return base64::encode(&digest[0], &digest[sizeof(digest)]);
```

就是 RFC 6455 规定的 `Base64(SHA1(key ∥ GUID))`，复用了 u5-l5 的 `MessageDigest` 门面。

101 响应在 [src/HttpServerCommand.cc:216-224](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L216-L224) 分两步：`feedUpgradeResponse` 组头（[src/HttpServer.cc:301-312](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L301-L312)，只有 `Upgrade`/`Connection`/`Sec-WebSocket-Accept` 三个头、无 body），然后把发送任务交给 `WebSocketResponseCommand`。它继承第三棒骨架，只覆写 `afterSend`（[src/WebSocketResponseCommand.cc:55-64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketResponseCommand.cc#L55-L64)）：101 写完后**不再回到 HTTP 循环**，而是创建 `WebSocketSession` 与 `WebSocketInteractionCommand`。注意 `wsSession->setCommand(command.get())`——会话里留一个裸指针指回宿主命令，供通知推送时反向调用 `updateWriteCheck()`（见 4.5）。

会话构造与 wslay 接线在 [src/WebSocketSession.cc:213-232](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc#L213-L232)：注册 5 个回调后 `wslay_event_context_server_init`，并调 `wslay_event_config_set_no_buffering(wsctx_, 1)`——控制帧的载荷不进 wslay 内部缓冲（ping/pong 的 payload 反正没用）。

两个 I/O 回调是 SocketCore 与 wslay 的粘合层：

- [src/WebSocketSession.cc:86-114](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc#L86-L114) `recvCallback`：调 `socket->readData`，读到 0 字节时按 u2-l4 的约定区分——`wantRead/wantWrite` 置位说明只是 EWOULDBLOCK，翻译成 `WSLAY_ERR_WOULDBLOCK`（wslay 会稍后再试）；否则是对端断了，报 `WSLAY_ERR_CALLBACK_FAILURE` 终止会话。
- [src/WebSocketSession.cc:59-83](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc#L59-L83) `sendCallback`：同样把非阻塞写失败翻译成 WOULDBLOCK。

**增量 JSON 解析**是这段代码的亮点：[src/WebSocketSession.cc:136-157](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc#L136-L157) 的两个帧回调里，`onFrameRecvStartCallback` 检查操作码，控制帧置 `ignorePayload`；`onFrameRecvChunkCallback` 对数据帧的每个分片调 `parseUpdate`——JSON 解析器（u9-l2 的 `ValueBaseJsonParser`）边收边解析，**无需先把整条消息攒进内存**。`parseUpdate` 里还有一道闸（[src/WebSocketSession.cc:311-322](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc#L311-L322)）：累计长度超过 `--rpc-max-request-size` 就把后续字节喂 0 长度（解析必然失败），防止超大帧耗尽内存。

消息完整回调 [src/WebSocketSession.cc:160-211](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc#L160-L211) 的 `onMsgRecvCallback`：非控制帧 `parseFinal` 收尾拿到 `ValueBase`，`Dict` 单调用、`List` 批量调用、其余 `-32600`——与 HTTP 的 JSON 分支逻辑一一对应，只是结果不进 HTTP 响应而是 `addResponse` → `addTextMessage`；按源码，收到控制帧也会回一条 `-32600`（ping/pong 的协议级应答由 wslay 内部处理，不经此分支）。

发送排队在 [src/WebSocketSession.cc:282-299](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc#L282-L299)：`addTextMessage(msg, delayed)` 默认直接 `wslay_event_queue_msg` 入队（真正写出要等下一次 `onWriteEvent`）；`delayed=true` 则包一层 `TextMessageCommand` + `DelayedCommand` 延迟 1 秒（[src/WebSocketSession.cc:263-280](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc#L263-L280)），用途与 4.3 的 HTTP 侧一致——未授权响应拖 1 秒，防口令爆破。

常驻宿主 [src/WebSocketInteractionCommand.cc:87-110](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketInteractionCommand.cc#L87-L110)：

```cpp
if (wsSession_->onReadEvent() == -1 || wsSession_->onWriteEvent() == -1) {
  /* 记日志：WebSocket session terminated */
  return true;                    // 会话出错，销毁（析构里 removeSession）
}
if (wsSession_->finish()) { return true; }   // 不想读也不想写 → 正常收尾
updateWriteCheck();
e_->addCommand(std::unique_ptr<Command>(this));  // 自旋重排
return false;
```

构造/析构（[src/WebSocketInteractionCommand.cc:51-71](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketInteractionCommand.cc#L51-L71)）完成向 `WebSocketSessionMan` 的注册与注销——会话集合的生命周期与宿主命令严格绑定。注意它构造时**没有** `setStatus(REALTIME)`：默认 INACTIVE，完全靠 socket 读/写事件唤醒（u2-l2 模型），没有数据就安静睡觉，不占 CPU。

#### 4.4.4 代码实践

1. **实践目标**：亲手完成一次升级握手并观察 wslay 层的应答行为。
2. **操作步骤**：
   ```bash
   ./src/aria2c --enable-rpc --rpc-secret=test123 -l rpc.log --log-level=info
   npm install -g wscat          # 没有 node 环境可改用浏览器扩展或 websocat
   wscat -c ws://localhost:6800/jsonrpc
   # 连上后发送一段非法 JSON：
   > {oops
   # 再发一次合法调用：
   > {"jsonrpc":"2.0","id":"1","method":"aria2.getVersion","params":["token:test123"]}
   ```
3. **需要观察的现象**：第一条 `{oops` 收到 `{"error":{"code":-32700,"message":"Parse error."}...}` 文本帧；第二条收到带 `"version"` 的正常应答。日志里能看到握手期间的 HTTP 请求记录与 `HTTP Server sends upgrade response`（DEBUG 级）。
4. **预期结果**：升级握手成功（wscat 不报错即 101 正常），两种应答分别对应 `onMsgRecvCallback` 的解析失败分支与正常 `Dict` 分支。若 wscat 不可用，用 `curl --include --no-buffer -H "Connection: Upgrade" -H "Upgrade: websocket" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" -H "Sec-WebSocket-Version: 13" http://localhost:6800/jsonrpc` 也能看到 101 与 `Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=`（RFC 6455 样例值，可自行验证 SHA1 计算）。（待本地验证）

#### 4.4.5 小练习与答案

**练习 1**：客户端用 `Sec-WebSocket-Version: 8` 握手，aria2 回什么？
**答案**：`websocketHandshake` 返回 426，并且 [HttpServerCommand.cc:226-228](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L226-L228) 会带上 `Sec-WebSocket-Version: 13` 头，告诉客户端「我只支持 13，请升级后重来」。

**练习 2**：`WebSocketInteractionCommand` 为什么不像 HTTP 三棒那样频繁销毁重建？
**答案**：WebSocket 是有状态的长会话——wslay 上下文（帧重组状态、发送队列）挂在 `WebSocketSession` 上，而会话又由宿主命令持有并注册进 `WebSocketSessionMan`。若像 HTTP 那样按消息换命令，发送队列与注册关系都要跟着搬迁。常驻自循环（构造注册、析构注销）让生命周期与连接严格相等。

**练习 3**：aria2 收到客户端的二进制帧（opcode 0x2）会怎么处理？
**答案**：按源码走非控制帧分支，照样把载荷喂给 JSON 解析器（`onMsgRecvCallback` 里没有 opcode 过滤，仅注释 TODO「Only process text frame」）；二进制载荷几乎不可能解析成合法 JSON，最终回 `-32700 Parse error.`。

### 4.5 通知推送：WebSocketSessionMan 与 Notifier 事件链

#### 4.5.1 概念说明

前两节解决「客户端问我答」；本节解决「服务器主动说」。aria2 把下载生命周期事件（开始/暂停/停止/完成/出错/BT 完成）定义为枚举 `DownloadEvent`（[src/includes/aria2/aria2.h:105-131](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/includes/aria2/aria2.h#L105-L131)），事件发生地（`RequestGroupMan` 等）只管向全局单例 `Notifier` 广播；`WebSocketSessionMan` 作为监听器之一，把事件翻译成 JSON-RPC 通知格式，扇出给所有在线 WebSocket 会话。**生产者不认识消费者**——同一个 Notifier 也服务 libaria2 的 C 回调（u8-l5）与 RPC 通知。

#### 4.5.2 核心流程

```text
事件产生（都在 DownloadEngine::run 的某一轮里）
  RequestGroupMan::fillRequestGroup       → EVENT_ON_DOWNLOAD_START   (L582)
  RequestGroupMan 暂停处理                → EVENT_ON_DOWNLOAD_PAUSE  (L451)
  DownloadEngine::calculateStatistics
    → RequestGroupMan::removeStoppedGroup → EVENT_ON_DOWNLOAD_COMPLETE / ERROR / STOP
  DefaultPieceStorage / BtCheckIntegrityEntry → EVENT_ON_BT_DOWNLOAD_COMPLETE
        │
        ▼
  SingletonHolder<Notifier>::notifyDownloadEvent(event, group)
        │  遍历 listeners_
        ▼
  WebSocketSessionMan::onEvent(event, group)          # 注册于 MultiUrlRequestInfo::prepare
        │  getMethodName: EVENT_ON_DOWNLOAD_START → "aria2.onDownloadStart"
        ▼
  addNotification(method, group)
        │  构造 {"jsonrpc":"2.0","method":...,"params":[{"gid":...}]}
        │  json::encode 序列化
        ▼  对每个在线 session：
  session->addTextMessage(msg, false)   # wslay_event_queue_msg 仅入队！
  session->getCommand()->updateWriteCheck()  # 手动让 EventPoll 开始关心「可写」
        ▼
  （本轮或下一轮循环）宿主命令被写事件唤醒
  → onWriteEvent → wslay_event_send → sendCallback → socket->writeData → 客户端
```

关键认知：**通知的构造发生在「别的命令」的执行现场**（比如 `FillRequestGroupCommand` 把任务提为 active 时），此刻 WebSocket 宿主命令可能正在 EventPoll 里沉睡、且多半没注册写事件。所以 `addNotification` 必须显式调 `updateWriteCheck()` 把写事件注册上，否则消息在 wslay 队列里干等，直到下次客户端恰好发来请求才被捎带发出。

#### 4.5.3 源码精读

事件源侧，[src/RequestGroupMan.cc:240-249](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L240-L249) 是薄封装（判空只为单测方便）；三处典型触发点：

- [src/RequestGroupMan.cc:580-582](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L580-L582)：任务从等待队列提升执行时发 `EVENT_ON_DOWNLOAD_START`（与 `--on-download-start` 钩子紧挨着）。
- [src/RequestGroupMan.cc:276-284](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L276-L284)：任务收尾时按结果码三选一——`FINISHED` → COMPLETE，非 `IN_PROGRESS/REMOVED` → ERROR，其余（被 remove）→ STOP。这段位于 `executeStopHook`，由每轮主循环 [src/DownloadEngine.cc:230-239](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L230-L239) 的 `calculateStatistics → removeStoppedGroup` 驱动（主循环骨架见 [src/DownloadEngine.cc:158-178](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L158-L178)）。
- [src/RequestGroupMan.cc:451](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L451)：暂停处理中的任务发 PAUSE。

分发单例 [src/Notifier.cc:45-56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Notifier.cc#L45-L56)：`addDownloadEventListener` 收指针、`notifyDownloadEvent` 顺序调用每个 `listener->onEvent(event, group)`。注意 aria2 主流程不删监听器——`WebSocketSessionMan` 的生命周期与引擎相等，不存在悬垂问题。

注册发生在 [src/MultiUrlRequestInfo.cc:209-215](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L209-L215)（u1-l3 讲过的 `prepare()` 内、引擎创建之后）：

```cpp
if (option_->getAsBool(PREF_ENABLE_RPC)) {
  e_->setWebSocketSessionMan(make_unique<rpc::WebSocketSessionMan>());
  SingletonHolder<Notifier>::instance()->addDownloadEventListener(
      e_->getWebSocketSessionMan().get());
}
```

翻译与广播是本模块的心脏，见 [src/WebSocketSessionMan.cc:68-84](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSessionMan.cc#L68-L84)：

```cpp
auto dict = Dict::g();
dict->put("jsonrpc", "2.0");
dict->put("method", method);
auto eventSpec = Dict::g();
eventSpec->put("gid", GroupId::toHex((group->getGID())));
auto params = List::g();
params->append(std::move(eventSpec));
dict->put("params", std::move(params));
std::string msg = json::encode(dict.get());
for (auto& session : sessions_) {
  session->addTextMessage(msg, false);
  session->getCommand()->updateWriteCheck();
}
```

四步：`Dict` 组树（u9-l2 的 ValueBase）→ `json::encode` 序列化 → 每会话入队 → 每会话手动开写监听。事件名映射表在 [src/WebSocketSessionMan.cc:86-125](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSessionMan.cc#L86-L125)：六个 `DownloadEvent` 枚举值一一对应 `aria2.onDownloadStart` 等方法名，`onEvent` 只做一次转发。会话集合的增删（[src/WebSocketSessionMan.cc:54-66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSessionMan.cc#L54-L66)）由宿主命令的构造/析构驱动（4.4 已读），集合用 `std::set<shared_ptr>`——同一会话不会重复注册。

把链条串起来：一条 `aria2.onDownloadStart` 从 `fillRequestGroup` 出生，到客户端屏幕，中间恰好经过「Notifier 单例 → 会话管理器 → wslay 队列 → EventPoll 写事件 → socket」五跳，全程单线程、无锁。

#### 4.5.4 代码实践

1. **实践目标**：完整观察一条系统通知的诞生与送达（本讲规格指定的核心实践）。
2. **操作步骤**：
   ```bash
   # 终端 1：服务器
   ./src/aria2c --enable-rpc --rpc-secret=test123 -l rpc.log --log-level=info
   # 终端 2：订阅者
   wscat -c ws://localhost:6800/jsonrpc
   # 终端 3：触发一次下载（注意 dir 按需修改）
   curl http://localhost:6800/jsonrpc -d '{"jsonrpc":"2.0","id":"1",\
     "method":"aria2.addUri","params":["token:test123",["https://speed.hetzner.de/10MB.bin"]]}'
   ```
3. **需要观察的现象**：不做任何轮询，终端 2 的 wscat 会自动打印：
   ```json
   {"jsonrpc":"2.0","method":"aria2.onDownloadStart","params":[{"gid":"..."}]}
   ...（下载结束后）...
   {"jsonrpc":"2.0","method":"aria2.onDownloadComplete","params":[{"gid":"..."}]}
   ```
   gid 与终端 3 中 `aria2.addUri` 应答里的 gid 一致。
4. **预期结果**：对照源码核对链路——`onDownloadStart` 对应 [RequestGroupMan.cc:582](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L582)（任务提升时），`onDownloadComplete` 对应 [RequestGroupMan.cc:277](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L277)（`removeStoppedGroup` 收尾时）；两条通知均由 [WebSocketSessionMan.cc:79-83](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSessionMan.cc#L79-L83) 的循环推送。若再开第二个 wscat 连接，两个客户端应同时收到同样内容（扇出语义）。（URL 可达性与具体 gid 待本地验证）

#### 4.5.5 小练习与答案

**练习 1**：把 `addNotification` 里的 `session->getCommand()->updateWriteCheck()` 删掉，会发生什么？
**答案**：消息仍会进 wslay 发送队列，但宿主命令可能没有注册写事件、也未被唤醒的契机（客户端不发请求、socket 无数据时），通知会滞留队列，直到该会话因读事件或其它写事件被唤醒才补发——表现为「通知延迟甚至迟迟不到」。这正是该行存在的理由。

**练习 2**：HTTP（非 WebSocket）客户端能收到这些通知吗？
**答案**：不能。通知只扇出给 `WebSocketSessionMan::sessions_` 里的 WebSocket 会话；HTTP 是请求-应答模型，只能靠客户端主动轮询 `aria2.tellStatus`/`aria2.tellActive`。这也是 webui 优先选 WebSocket 传输的原因。

**练习 3**：`EVENT_ON_BT_DOWNLOAD_COMPLETE` 与 `EVENT_ON_DOWNLOAD_COMPLETE` 为何要分开？
**答案**：BT 下载「下载完成」后通常还要做种（u6-l10）。`onBtDownloadComplete` 表示数据块全部到手、可能继续 seeding；只有种子任务最终收尾（含做种结束）才走 `onDownloadComplete`。两事件的触发点也不同：前者在 [src/DefaultPieceStorage.cc:503](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L503) 等处，后者在 `RequestGroupMan` 的收尾路径。

## 5. 综合实践

**任务：给通知链画一张带源码坐标的完整时序图。**

以 4.5.4 的三终端环境为实验台，完成一次 `addUri` 并收到 `onDownloadStart`/`onDownloadComplete` 后，画出从「curl 发出 addUri」到「wscat 屏幕打印通知」的端到端时序图，必须标注以下每一跳的源码位置与所属角色：

1. addUri 的 HTTP 请求进入 `HttpListenCommand::execute`（accept，[src/HttpListenCommand.cc:72-82](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpListenCommand.cc#L72-L82)）；
2. 三棒接力处理该请求（4.3，标注三棒各自的关键行号）；
3. `aria2.addUri` 创建的 RequestGroup 在某轮 `fillRequestGroup` 中被提升，触发 [RequestGroupMan.cc:582](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L582)；
4. `Notifier` 单例分发（[src/Notifier.cc:50-56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Notifier.cc#L50-L56)）→ `WebSocketSessionMan::onEvent` → `addNotification`（构造 Dict、`json::encode`、入队、`updateWriteCheck`）；
5. 宿主命令被写事件唤醒（[src/WebSocketInteractionCommand.cc:87-110](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketInteractionCommand.cc#L87-L110)）→ `wslay_event_send` → `sendCallback` → `socket->writeData`（[src/WebSocketSession.cc:59-83](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc#L59-L83)）。

进阶（可选）：在 `WebSocketSessionMan::addNotification` 的 `json::encode` 之后加一行 `A2_LOG_INFO(fmt("WS notify: %s", msg.c_str()))`，重新编译后重跑实验，验证日志行与 wscat 收到消息一一对应——这能证明「入队」与「写出」确实是两个时间点。

**预期产出**：一张时序图（手绘或 mermaid）加一段 200 字以内的链路说明，说明中要回答一个问题：为什么第 5 步不会与第 4 步发生在同一瞬间？（答：入队只改 wslay 内部队列，实际写出要等宿主命令被 EventPoll 写事件唤醒，中间可能隔一次主循环。）

## 6. 本讲小结

- RPC 服务器不是独立线程，而是事件循环里的命令：`HttpListenCommand` 常驻 accept，每条连接由 `HttpServerCommand → BodyCommand → ResponseCommand` 三棒接力，keep-alive 通过**复用同一个 `HttpServer` 对象**在第三棒 `afterSend` 处回到第一棒。
- `HttpServer` 是服务端 HTTP 会话状态机：`SERVER_PARSER` 模式复用 u4-l2 的流式解析器；`setupResponseRecv` 按「方法+路径」把请求路由成 JSON/JSONP/XML 三型，body 落入内存盘写入器边收边解析。
- WebSocket 的前半生是 HTTP：`Upgrade`+`Connection` 头触发升级判定（101/400/404/426），`Sec-WebSocket-Accept = Base64(SHA1(key ∥ GUID))`；后半生交给 wslay——aria2 把 `SocketCore` 适配成 recv/send 回调，文本帧载荷**增量**喂给 JSON 解析器，无需整消息缓存。
- `WebSocketResponseCommand` 复用第三棒发送骨架但替换 `afterSend`：101 写完即创建 `WebSocketSession` + 常驻的 `WebSocketInteractionCommand`，模板方法模式让两种「下一步」共享同一副发送代码。
- 系统通知走「生产者 → `Notifier` 单例 → `WebSocketSessionMan` 扇出」链路：事件源在 `RequestGroupMan` 的提升/暂停/收尾三处；管理器把 `DownloadEvent` 翻译成 `aria2.onXxx` 方法名、组装 JSON-RPC 通知、逐会话入队，并**手动调 `updateWriteCheck()`**——因为推送发生在别的命令现场，必须主动让 EventPoll 开始关心这条 socket 的可写事件。

## 7. 下一步学习建议

本讲只讲到「RPC 请求被解析成 `ValueBase`、交给 `processJsonRpcRequest`」为止。下一讲 **u8-l4「RPC 方法分发：XML-RPC 与 JSON-RPC」**将打开这个盒子：`RpcMethod` 基类的 authorize/execute 三段流程、`RpcMethodFactory` 按方法名实例化、`--rpc-secret` 令牌鉴权在哪一行检查，以及 `aria2.addUri`/`tellStatus` 等方法的具体实现。

如果你更关心嵌入用法，也可以跳读 **u8-l5「libaria2」**：它展示同一套引擎如何脱离 RPC 服务器、以 C API 形式被宿主程序驱动，其中的 `ApiCallbackDownloadEventListener` 正是 `Notifier` 的另一个消费者。

复习建议：重读 u2-l2 的「事件回调只置位不执行」与 u2-l3 的「命令返回值约定」，本讲所有命令的行为几乎都是这两条规则的应用题。
