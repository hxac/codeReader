# u8-l3 内嵌 HTTP 与 WebSocket 服务器

## 1. 本讲目标

`aria2c --enable-rpc` 一启动，aria2 就从「下载客户端」变成了「下载服务」：外部程序（Web UI、脚本、桌面面板）通过 JSON-RPC / XML-RPC 远程控制它，还能通过 WebSocket 实时接收 `aria2.onDownloadStart` 这类事件通知。本讲要回答的问题是：**这套 HTTP + WebSocket 服务器是怎么长在 aria2 里的？**

学完本讲，你应该能够：

1. 解释 RPC 监听体系如何复用 u2 学过的 DownloadEngine 事件循环与 Command 模型——服务器没有独立线程，只是几条普通命令。
2. 说出一次 RPC 请求经过的四类命令接力：`HttpListenCommand → HttpServerCommand → HttpServerBodyCommand → HttpServerResponseCommand`，以及 keep-alive 如何让连接回到循环起点。
3. 跟踪 WebSocket 升级握手（RFC 6455 的 `Sec-WebSocket-Accept` 计算）与升级后的帧收发循环（wslay 事件回调模型）。
4. 完整描述一条系统通知（如 `aria2.onDownloadStart`）从下载引擎出发、经 Notifier 单例、WebSocketSessionMan 广播、wslay 队列、最终写成 socket 字节的全部链路。

本讲只讲「传输层服务器」；RPC 方法本身的分发（`aria2.addUri` 的参数校验与执行）属于下一讲 u8-l4。

## 2. 前置知识

### 2.1 RPC：让 aria2 变成服务

RPC（Remote Procedure Call，远程过程调用）在这里特指：aria2 在本机端口（默认 6800）上监听 HTTP 请求，把请求体里的 JSON-RPC / XML-RPC 报文翻译成对内部下载引擎的调用，再把结果序列化成 HTTP 响应返回。aria2 同时支持三种传输：

| 传输方式 | 请求路径 | 特点 |
|---------|---------|------|
| HTTP POST（JSON-RPC） | `/jsonrpc` | 最常用，请求体是 JSON |
| HTTP GET（JSONP） | `/jsonrpc?params=...` | 供浏览器 `<script>` 调用，参数放在查询串 |
| HTTP POST（XML-RPC） | `/rpc` | 需要 libxml2/expat 编译支持 |
| WebSocket | `ws://host:6800/jsonrpc` | 双向：既能发请求，还能收服务器推送的通知 |

### 2.2 WebSocket：在一条 TCP 上双向说话

HTTP 是「一问一答」：客户端发请求、服务器答完就完。WebSocket（RFC 6455）的思路是：先用**一次普通 HTTP 请求**商量升级（`Upgrade: websocket`），服务器若同意就回 `101 Switching Protocols`，此后这条 TCP 连接不再说 HTTP，改说**帧（frame）协议**——双方都可随时发帧。帧分两类：

- **控制帧**：`ping`/`pong`（保活探测）、`close`（礼貌分手），载荷由协议栈自己消化；
- **数据帧**：`text`（文本，aria2 用它承载 JSON-RPC）、`binary`。

升级握手有一个防跨协议攻击的小数学：客户端随机给一个 `Sec-WebSocket-Key`，服务器必须回

\[
\text{Accept} = \text{Base64}\big(\text{SHA-1}(\text{Key} \,\Vert\, \text{``258EAFA5-E914-47DA-95CA-C5AB0DC85B11''})\big)
\]

其中那串 GUID 是 RFC 6455 规定的固定魔法字符串。客户端校验不通过就断连——这样一台「其实不是 WebSocket 服务器」的 HTTP 服务（或中间代理）不可能糊弄过关。

### 2.3 aria2 自己造服务器要解决的三个问题

1. **没有线程**。aria2 是单线程 Reactor（u2-l1/u2-l2）：所有 I/O 都由 `DownloadEngine::run()` 的事件循环驱动。服务器必须拆成一个个小步的 Command，每步只做非阻塞的一点点事。
2. **不能阻塞下载**。一个慢客户端发来 100MB 的请求体（或永远不读完响应），不能拖住引擎——所以每条连接都有 30 秒无进展超时、请求体大小受 `--rpc-max-request-size` 限制。
3. **解析器要复用**。HTTP 客户端那套 `HttpHeaderProcessor`（u4-l2 精读过：增量、免疫粘包半包）本来就是「逐字节喂」的状态机，服务器方向只需要换一个 `SERVER_PARSER` 模式。

WebSocket 帧协议本身很繁琐（分片、掩码、控制帧穿插），aria2 没有手写，而是内嵌了 **wslay**（作者同一个人的纯 C 事件驱动 WebSocket 库，作为子源码树放在 `deps/wslay/`，默认编译开启，见 [configure.ac:74](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L74) 与 [configure.ac:995-1006](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/configure.ac#L995-L1006)，可用 `--disable-websocket` 裁掉）。wslay 只管「帧的编解码与收发调度」，真正的 socket 读写通过回调函数交给宿主——这正好和 aria2 的 SocketCore 无缝对接。

### 2.4 与前面讲义的衔接

- u2-l1/u2-l2：DownloadEngine 主循环、EventPoll「只置位不执行」、Command 的 `execute()` 返回值约定（true 销毁 / false 须自排）。
- u2-l3：Command 状态位（INACTIVE/ACTIVE/REALTIME/ONESHOT_REALTIME）。
- u4-l2：`HttpHeaderProcessor`、`HttpHeader`、`SocketBuffer` 部分写——本讲的服务器代码是它们的「镜像」。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [src/DownloadEngineFactory.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc) | `--enable-rpc` 时创建 HttpListenCommand 并绑定端口 |
| [src/MultiUrlRequestInfo.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc) | 引擎就绪后创建 WebSocketSessionMan 并注册为 Notifier 监听器 |
| [src/HttpListenCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpListenCommand.cc) | 监听 socket 的常驻守护命令：accept 新连接 |
| [src/HttpServer.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc) | 服务端 HTTP 编解码器：收请求头/体、生成响应 |
| [src/HttpServerCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc) | 单连接第一棒：读请求头、鉴权、识别 WebSocket 升级 |
| [src/HttpServerBodyCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc) | 第二棒：收完请求体、执行 RPC、准备响应 |
| [src/AbstractHttpServerResponseCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractHttpServerResponseCommand.cc) | 第三棒骨架：把发送缓冲写空，然后调 afterSend 钩子 |
| [src/HttpServerResponseCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerResponseCommand.cc) | afterSend 实现：keep-alive 则回到第一棒 |
| [src/WebSocketResponseCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketResponseCommand.cc) | WebSocket 分支的 afterSend：创建会话对象与常驻命令 |
| [src/WebSocketSession.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc) | wslay 上下文封装：五个回调 + 增量 JSON 解析 + 出站队列 |
| [src/WebSocketInteractionCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketInteractionCommand.cc) | WebSocket 连接的常驻守护命令 |
| [src/WebSocketSessionMan.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSessionMan.cc) | 会话登记簿 + 通知广播器 |
| [src/Notifier.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Notifier.cc) | 下载事件的多播中枢（观察者模式） |
| [src/JsonDiskWriter.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/JsonDiskWriter.h) | 「假装是磁盘」的请求体容器：写入即解析 |

## 4. 核心概念与源码讲解

### 4.1 监听与装配：HttpListenCommand 守护循环

#### 4.1.1 概念说明

一台服务器要有人「守着」监听 socket，把新到的连接分发给处理器。在 aria2 里这个角色不是线程，而是一条**永生的普通 Command**：`HttpListenCommand`。它每轮循环做一件极小的事——「如果有新连接就 accept 并派生一条 `HttpServerCommand`」，然后把自己重新排队。这与 BT 的 `PeerListenCommand`（u6-l3）是同一个套路。

装配发生在引擎工厂里：`DownloadEngineFactory::newDownloadEngine` 在选项 `--enable-rpc` 打开时创建监听命令。注意一个细节：aria2 会为 IPv4 和 IPv6 **各建一条**监听命令，只要任意一族绑定成功即可，全失败才抛 `Failed to setup RPC server`。

#### 4.1.2 核心流程

```text
DownloadEngineFactory::newDownloadEngine (--enable-rpc)
  ├─ 对 families = [AF_INET, (AF_INET6)] 逐个：
  │    new HttpListenCommand(cuid, e, family, secure)
  │    └─ bindPort(--rpc-listen-port)
  │         ├─ --rpc-listen-all → AI_PASSIVE（绑所有网卡），否则只绑回环
  │         ├─ bind + beginListen（listen 队列建立）
  │         └─ addSocketForReadCheck(监听socket, 自己)   ← 挂进 EventPoll
  │    成功 → e->addCommand(监听命令)
  └─ 一族都没成功 → 抛 DL_ABORT_EX

HttpListenCommand::execute（每轮事件循环）
  ├─ 引擎要退出？（downloadFinished/isHaltRequested）→ return true 销毁
  ├─ 监听 socket 可读？
  │    ├─ acceptConnection() → 新 SocketCore
  │    ├─ setTcpNodelay(true)          ← RPC 是小报文往返，禁用 Nagle
  │    └─ addCommand(new HttpServerCommand(newCUID, e, socket, secure))
  │         └─ setNoWait(true)          ← 提醒主循环别在 poll 里睡觉
  └─ addCommand(this); return false     ← 自排，永生
```

`secure` 参数来自 `--rpc-secure`：打开时这条连接要先做 TLS 握手（证书由 `MultiUrlRequestInfo::prepare` 装进 `SocketCore::setServerTLSContext`），后面 4.3 会看到握手的位置。

#### 4.1.3 源码精读

先看装配点。[DownloadEngineFactory.cc:196-221](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc#L196-L221) 这段代码在 `--enable-rpc` 时先警告「既没 `--rpc-secret` 也没 `--rpc-user`」的不安全配置，然后循环 IPv4/IPv6 两族创建监听命令并绑定端口，一族成功即 `ok=true`，全失败抛异常终止启动：

- [DownloadEngineFactory.cc:208-217](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc#L208-L217)：`familiesLength` 在 `--disable-ipv6` 时退化为 1，即只试 IPv4；每个 `HttpListenCommand` 绑定成功后才 `addCommand` 进入引擎调度。

再看监听命令本体。[HttpListenCommand.cc:92-116](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpListenCommand.cc#L92-L116) 是 `bindPort`：新建 `SocketCore`，按 `--rpc-listen-all` 决定是否加 `AI_PASSIVE` 旗标（决定绑 0.0.0.0 还是 127.0.0.1），`bind` + `beginListen` 之后通过 `addSocketForReadCheck` 把监听 socket 注册进 EventPoll 的读关注集——这正是 u2-l2 讲的「SocketEntry 订阅表」，事件到来时回调会把本命令置为 ACTIVE：

- [HttpListenCommand.cc:100-107](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpListenCommand.cc#L100-L107)：绑定成功打出 `IPv%d RPC: listening on TCP port %u` 的 NOTICE 日志，并完成读关注注册。

[HttpListenCommand.cc:66-90](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpListenCommand.cc#L66-L90) 是 `execute` 主循环，做了四件事：退出检查、`isReadable(0)` 非阻塞探测、accept 并派生 `HttpServerCommand`（新 CUID、`setTcpNodelay` 关闭 Nagle 算法以降低小报文延迟）、无条件自排（[HttpListenCommand.cc:88-89](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpListenCommand.cc#L88-L89)）。accept 抛 `RecoverableException`（比如 fd 用尽）只记 DEBUG 日志继续活——监听者不能因为一次失败就死。

还有一个容易忽略的装配点：WebSocket 的会话管理器并不是工厂创建的，而是在 `MultiUrlRequestInfo::prepare` 里、引擎建好之后才挂上去。[MultiUrlRequestInfo.cc:209-215](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L209-L215) 创建 `WebSocketSessionMan` 交给引擎（`setWebSocketSessionMan`），并把它注册为全局 `Notifier` 单例的下载事件监听器——这一行就是 4.5 节整条通知链的「发车站」。未编译 WebSocket 时则用同名空实现 `NullWebSocketSessionMan` 兜底（[MultiUrlRequestInfo.cc:68-72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L68-L72)）。

#### 4.1.4 代码实践

**实践目标**：观察监听命令的启动日志与 accept 行为，验证「两族监听、一族成功即可」。

1. 操作步骤：
   ```bash
   ./src/aria2c --enable-rpc --rpc-listen-port=6800 --log=rpc.log --log-level=info
   ```
   （若尚未编译，参考 u1-l2；`--enable-rpc` 是默认开启的特性，一般无需额外开关。）
2. 在另一终端用 `curl -v http://localhost:6800/jsonrpc -d '{}'` 触发一次连接。
3. 再试 `--rpc-listen-all` 重启一次，对比日志；最后故意占用端口（另开一个进程先绑 6800）再启动 aria2，观察失败路径。
4. 需要观察的现象：`rpc.log` 中应出现 `IPv4 RPC: listening on TCP port 6800`（NOTICE 级），每次 curl 出现 `RPC: Accepted the connection from 127.0.0.1:xxxxx`（INFO 级）；端口被占时应看到 `failed to bind` 的 ERROR 日志且进程以错误退出。
5. 预期结果：日志行与 [HttpListenCommand.cc:77-78](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpListenCommand.cc#L77-L78)（accepted）和 [HttpListenCommand.cc:108](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpListenCommand.cc#L108)（listening）一一对应；IPv6 族在你的机器上可能成功也可能失败，两种结果都符合「一族成功即继续」的设计。具体日志文本以本地运行为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`HttpListenCommand::execute` 每轮都会执行（它没有设置任何 STATUS），既已注册了 EventPoll 读关注，为什么还要在 execute 里再调 `serverSocket_->isReadable(0)`？

**答案**：命令注册读关注后，事件到来只是把命令状态置 ACTIVE 唤醒它尽快执行（u2-l2 的「只置位不执行」）；而命令被调度执行时数据是否真的可读要以 `isReadable(0)` 的即时探测为准，两者是「唤醒」与「确认」的关系。若不可读则跳过 accept 只自排，避免空转 accept 报错。

**练习 2**：为什么 aria2 对 IPv4/IPv6 两族「一族成功即继续」，而不是任一族失败就整体退出？

**答案**：RPC 监听是附属功能，IPv6 在很多环境不可用（无地址、内核关闭等），若因此连 IPv4 也不干活就过度惩罚了用户；反之两族全失败说明端口被占或权限问题，此时 `ok=false` 才抛 `Failed to setup RPC server`（[DownloadEngineFactory.cc:218-220](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc#L218-L220)）。

### 4.2 HttpServer：服务端 HTTP 编解码器

#### 4.2.1 概念说明

`HttpServer` 不是命令，而是一个**纯编解码器对象**：它持有连接 socket 的收发缓冲，对外提供「喂请求字节 / 取请求语义 / 生成响应字节」三组方法，本身不知道 EventPoll 与命令调度的存在。这层分离让同一个类既能服务普通 RPC，也能先应答 WebSocket 升级再被会话对象接管。

它与 u4-l2 精读的客户端 HTTP 栈是**同一套积木搭反方向**：

- 收：`SocketRecvBuffer`（16KB 共享读缓冲，u2-l4 讲过 pos/last 双指针）+ `HttpHeaderProcessor`（同一状态机类，构造参数换成 `SERVER_PARSER` 模式）。
- 发：`SocketBuffer`（部分写缓冲队列，u4-l1/u4-l2 讲过）。

「请求体存哪」是个巧妙设计：RPC 请求体是 JSON/XML 文本，aria2 用 `JsonDiskWriter`/`XmlRpcDiskWriter` 当容器——名字带 DiskWriter（实现 DiskWriter 接口），实际一个字节都不落盘，写入的字节直接流进**增量解析器**（见 [src/JsonDiskWriter.h:45](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/JsonDiskWriter.h#L45) 对 `ValueBaseDiskWriter<JsonParser>` 的类型别名，以及 [src/ValueBaseDiskWriter.h:72-77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBaseDiskWriter.h#L72-L77) 中 `writeData` 直接喂 `parser_.parseUpdate` 的实现）。请求体收完之时，ValueBase 树也恰好解析完。

#### 4.2.2 核心流程

```text
receiveRequest()（增量，可能被调用多次）
  recv 到缓冲 → headerProcessor_->parse(buffer, len)
  ├─ 头部未收全：drain 已消费字节，return false
  └─ 头部收全：
       lastRequestHeader_ = getResult()
       drain；bodyConsumed_ = 0
       setupResponseRecv()   → 决定 reqType_ 与 lastBody_（见下表）
       解析 Content-Length → lastContentLength_（非法则抛异常断连）
       扫描 Accept-Encoding 是否含 gzip → acceptsGZip_
       return true

setupResponseRecv() 的路由表：
  GET  /jsonrpc → RPC_TYPE_JSONP，lastBody_=null（参数在查询串）
  POST /jsonrpc → RPC_TYPE_JSON，  lastBody_=JsonDiskWriter
  POST /rpc     → RPC_TYPE_XML，   lastBody_=XmlRpcDiskWriter（需编译开关）
  其他          → RPC_TYPE_NONE，  return -1（之后回 404）

receiveBody()（增量）
  缓冲取 min(剩余长度, Content-Length-已消费) 字节
  → lastBody_->writeData(...)（即喂增量 JSON 解析器）
  → 收满 Content-Length 返回 true

feedResponse(status, headers, body, contentType)
  拼 "HTTP/1.1 <status 原因短语>\r\n Date/Content-Length/Expires/
     Cache-Control: no-cache" + 可选 Content-Type / Access-Control-Allow-Origin /
     Content-Encoding: gzip / Connection: close → push 进 SocketBuffer
```

#### 4.2.3 源码精读

[HttpServer.cc:63-77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L63-L77) 构造函数把三件套接线：`socketRecvBuffer_`、`socketBuffer_`、以及**SERVER_PARSER 模式**的 `headerProcessor_`。初始 `keepAlive_=true`、`acceptsGZip_=false`。

[HttpServer.cc:175-227](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L175-L227) `receiveRequest`：缓冲空则先 `recv()`，对端关闭（recv 返回 0 且非 EWOULDBLOCK）抛 `EX_EOF_FROM_PEER` 断连；随后把缓冲增量交给 `headerProcessor_->parse`——和客户端解析响应头一模一样的「喂多少算多少」。头部完整后走 [HttpServer.cc:185-221](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L185-L221)：取结果、drain 精确字节数（`getLastBytesProcessed`）、调 `setupResponseRecv` 定路由、校验 `Content-Length`（负数或非数字直接抛异常）、扫描 `Accept-Encoding` 里的 `gzip`。

[HttpServer.cc:229-249](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L229-L249) `receiveBody`：每次最多搬 `lastContentLength_ - bodyConsumed_` 字节进 `lastBody_->writeData`（[HttpServer.cc:243-245](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L243-L245)），凑满即 true。注意 `lastBody_` 为 null（JSONP 的 GET 请求）时只是 drain 不落地——GET 本来就没有体。

[HttpServer.cc:369-400](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L369-L400) `setupResponseRecv` 的路由逻辑。一个值得注意的细节是 `if (reqType_ != RPC_TYPE_JSON)` 的判断：keep-alive 连接上连续多个同类请求时**复用**上一次创建的 DiskWriter（反正 `reset()` 过了），不重复构造。

[HttpServer.cc:266-299](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L266-L299) `feedResponse` 拼响应头：状态行的原因短语来自 [HttpServer.cc:82-172](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L82-L172) 的 `getStatusString` 查表（这张表里专门有 `101 Switching Protocols` 和 `426 Upgrade Required`，为 WebSocket 准备）；`supportsPersistentConnection()` 为假时补 `Connection: close`。头与体都 push 进 `socketBuffer_`，真正发送由 4.3 的响应命令驱动。

[HttpServer.cc:321-343](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L321-L343) `authenticate` 实现 HTTP Basic 认证（`--rpc-user`/`--rpc-passwd`，已不推荐，推荐 `--rpc-secret` 令牌鉴权——那是 RPC 方法层的事，见 u8-l4）：解码 base64 拆出用户名密码后，与存好的 **HMAC 摘要**比对（[HttpServer.cc:341-342](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L341-L342)），密码明文从不驻留；进程内随机 HMAC 密钥在 [HttpServer.cc:345-367](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L345-L367) 的 `setUsernamePassword` 里创建。

[HttpServer.cc:301-312](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L301-L312) `feedUpgradeResponse` 是 WebSocket 专用出口：`101 Switching Protocols` + `Upgrade` + `Connection: Upgrade` + 调用方追加的 `Sec-WebSocket-Accept` 头。

#### 4.2.4 代码实践

**实践目标**：用 curl 验证路由表、响应头形状与鉴权失败路径。

1. 操作步骤：
   ```bash
   # ① 正常 JSON-RPC POST
   curl -sv http://127.0.0.1:6800/jsonrpc \
     -d '{"jsonrpc":"2.0","id":"q","method":"aria2.getVersion","params":[]}'
   # ② 不存在的路径
   curl -sv http://127.0.0.1:6800/nothing -d '{}'
   # ③ JSONP GET（params 是 base64url 编码的请求数组，此处直接省略验证格式）
   curl -sv "http://127.0.0.1:6800/jsonrpc?params=W3sianNvbnJwYyI6ICIyLjAiLCAiaWQiOiAicXdlciIsICJtZXRob2QiOiAiYXJpYTIuZ2V0VmVyc2lvbiJ9XQ%3D%3D"
   # ④ Basic 认证：重启服务端加 --rpc-user=a2 --rpc-passwd=pw 后不带凭据访问
   curl -sv http://127.0.0.1:6800/jsonrpc -d '{}'
   curl -sv -u a2:pw http://127.0.0.1:6800/jsonrpc -d '{"jsonrpc":"2.0","id":"1","method":"aria2.getVersion","params":[]}'
   ```
2. 需要观察的现象：① 的响应头含 `Date`、`Content-Length`、`Expires`、`Cache-Control: no-cache`、`Content-Type: application/json-rpc`；② 得到 404；③ 响应体被 `callback(...)` 包裹、`Content-Type` 变为 `text/javascript`；④ 第一次返回 `401` 且带 `WWW-Authenticate: Basic realm="aria2"`，第二次返回 200。
3. 预期结果：与 `feedResponse`（[HttpServer.cc:266-299](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L266-L299)）、`setupResponseRecv`（[HttpServer.cc:369-400](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L369-L400)）、401 分支（[HttpServerCommand.cc:201-210](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L201-L210)）四处源码逐条对上（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `receiveRequest` 里解析 `Content-Length` 失败要抛异常而不是回 400？

**答案**：头部已消费进 `lastRequestHeader_`、双方对「接下来有多少字节请求体」的认知已经无法对齐——此时若继续用这条连接，下一个「请求」会从请求体的中间字节开始解析，整个流永久错位。抛 `DL_ABORT_EX` 让上层命令直接销毁连接是最干净的处理；而「路径不存在」只影响语义不影响成帧，所以可以优雅地回 404 再关连接。

**练习 2**：`JsonDiskWriter` 姓「Disk」却为什么不落盘？这继承了哪一讲的思想？

**答案**：因为它的基类 `ValueBaseDiskWriter` 把 `writeData` 实现为「喂给增量 ValueBase 解析器」（[src/ValueBaseDiskWriter.h:72-77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBaseDiskWriter.h#L72-L77)），磁盘只是接口形状、不是行为。这是 u5-l1 讲过的「DiskWriter 是注入工厂即可改变目的地」的延伸——magnet 元数据用 `ByteArrayDiskWriter` 进内存，RPC 请求体用 `JsonDiskWriter` 进解析器，同一接口三种归宿。

### 4.3 命令循环：四棒接力与 keep-alive 回环

#### 4.3.1 概念说明

一条客户端连接的生命周期由四类命令接力（可与 u4-l1 的 HTTP 下载命令链对照——结构同构，方向相反）：

1. **HttpServerCommand**（读头）：收请求头，做 TLS 握手（`--rpc-secure`）、Basic 鉴权、识别 WebSocket 升级；按路由分派下一棒。
2. **HttpServerBodyCommand**（读体+执行）：收完请求体，增量解析已经完成，`finalize` 后把 ValueBase 树交给 RPC 方法执行，生成响应字节。
3. **HttpServerResponseCommand / WebSocketResponseCommand**（写回）：把 `SocketBuffer` 里的响应字节尽量写出去，写空则触发 `afterSend` 钩子。
4. **回环**：普通请求且 keep-alive 成立 → `afterSend` 再造一个 `HttpServerCommand`（**复用同一个 HttpServer 对象**）等下一个请求；WebSocket 请求 → `afterSend` 创建会话（4.4 节）。

每棒都遵守 u2-l3 的约定：`execute()` 返回 true 即销毁；返回 false 前必须把自己重新 `addCommand`。每条命令自带 30 秒无进展超时，防止慢客户端占着调度位。

#### 4.3.2 核心流程

```text
HttpServerCommand::execute
  ├─ 退出检查（引擎结束）
  ├─ 无数据可读？→ 30s 超时则销毁，否则自排等待
  ├─ secure 时：tlsAccept() 未完成 → 自排等下轮（非阻塞握手）
  ├─ receiveRequest() 未收全头 → 自排
  ├─ authenticate() 失败（且非 OPTIONS 预检）→ 401 + 关连接 → ResponseCommand
  ├─ 头含 Upgrade: websocket 且 Connection: upgrade？
  │    ├─ ENABLE_WEBSOCKET：
  │    │    websocketHandshake() == 101？
  │    │      是 → 算 Accept key → feedUpgradeResponse → WebSocketResponseCommand
  │    │      否 → feedResponse(400/404/426) → HttpServerResponseCommand
  │    └─ 未编译 → 400
  ├─ Content-Length > --rpc-max-request-size？→ 记日志，销毁（弃连接）
  └─ 否则 → HttpServerBodyCommand

HttpServerBodyCommand::execute
  ├─ receiveBody() 未收满 → 自排（30s 超时同上）
  ├─ OPTIONS → CORS 预检 200 响应
  ├─ 按 reqType_ 分派：
  │    XML  → XmlRpcDiskWriter finalize → getMethod(name)->execute → toXml
  │    JSON/JSONP → JsonDiskWriter finalize → Dict 单调用 / List 批量调用
  │    其他 → 404
  └─ sendJsonRpcResponse：RPC 错误码→HTTP 状态（1/-32600→400，-32601→404，
       其余→500；未授权响应经 1 秒 DelayedCommand 延发）→ ResponseCommand

AbstractHttpServerResponseCommand::execute（骨架）
  ├─ sendResponse() 部分写
  ├─ 发送缓冲空 → afterSend() → return true 销毁
  └─ 否则 → updateReadWriteCheck + 自排（30s 超时）

HttpServerResponseCommand::afterSend
  └─ supportsPersistentConnection()？
       是 → new HttpServerCommand(复用同一个 HttpServer)   ← keep-alive 回环
       否 → 什么都不做，连接随命令销毁而关闭
```

#### 4.3.3 源码精读

[HttpServerCommand.cc:62-85](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L62-L85) 第一构造函数（新连接用）：初始 `STATUS_ONESHOT_REALTIME` 保证尽快跑第一轮；注册读关注；把 RPC 用户名密码、`--rpc-allow-origin-all`、gzip 能力（依 `HAVE_ZLIB` 编译宏）灌进 HttpServer。末尾 [HttpServerCommand.cc:84](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L84) 的 `checkSocketRecvBuffer()`（[HttpServerCommand.cc:108-117](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L108-L117)）检查「内核/共享缓冲里是否已有字节」——有则升 REALTIME 并 `setNoWait(true)`，让 pipelining 客户端连发的第二个请求立刻被处理而不等 poll 睡眠。[HttpServerCommand.cc:87-98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L87-L98) 是第二构造函数（keep-alive 复用用）：直接接收现成的 `HttpServer` 共享指针。

[HttpServerCommand.cc:171-278](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L171-L278) `execute` 全景。几个关键分支：

- [HttpServerCommand.cc:182-192](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L182-L192)：secure 连接先 `tlsAccept()`，握手是多次非阻塞小步，未完成就自排重来——这是 u2-l4「非阻塞操作拆步」思想在服务端的重演。
- [HttpServerCommand.cc:201-210](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L201-L210)：鉴权失败回 401。注意 `getMethod() != "OPTIONS"` 的豁免——CORS 预检请求（浏览器跨域前的自动探测）不要求凭据。
- [HttpServerCommand.cc:246-253](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L246-L253)：请求体超过 `--rpc-max-request-size`（默认 2M）时**直接 return true 弃连接**——同样因为流已无法对齐，回 413 反而要多读多写。
- [HttpServerCommand.cc:260-269](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L260-L269)：30 秒无进展超时。

[HttpServerBodyCommand.cc:175-343](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L175-L343) `execute`：体收满后先手工切出 `reqPath` 与 `query`（[HttpServerBodyCommand.cc:188-193](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L188-L193)，JSONP 的 callback 就藏在 query 里）；OPTIONS 走 CORS 预检应答（[HttpServerCommand.cc:195-219](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L195-L219)）；随后按 `getRequestType()` 三路分派。JSON 路径（[HttpServerBodyCommand.cc:254-312](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L254-L312)）：`dw->finalize()` 收尾增量解析、`getResult()` 取 ValueBase；顶层是 `Dict` 就单调用 `rpc::processJsonRpcRequest`，是 `List` 就批量循环，都不是则回 `-32600 Invalid Request`，解析失败回 `-32700 Parse error`。XML 路径（[HttpServerBodyCommand.cc:222-253](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L222-L253)）：`rpc::getMethod(req.methodName)` 从工厂取方法对象执行（u8-l4 的主角，这里只当黑盒）。

[HttpServerBodyCommand.cc:103-134](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L103-L134) `sendJsonRpcResponse`：JSON 序列化 + RPC 错误码到 HTTP 状态码的映射表。而 [HttpServerBodyCommand.cc:147-159](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L147-L159) `addHttpServerResponseCommand` 的 `delayed` 参数是**防爆破攻击**：未授权的响应包一层 1 秒 `DelayedCommand` 再发，让穷举密码的脚本每秒最多试一次。

发送骨架 [AbstractHttpServerResponseCommand.cc:98-135](https://github.com/aria2/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractHttpServerResponseCommand.cc#L98-L135)：`sendResponse()` 就是 `SocketBuffer::send()` 尽量写；写空后调虚函数 `afterSend` 然后销毁自己；没写完按 `wantRead/wantWrite` 调整 EventPoll 注册再自排。注意构造函数（[AbstractHttpServerResponseCommand.cc:50-62](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractHttpServerResponseCommand.cc#L50-L62)）默认就注册了**写关注**——响应几乎总有字节可发。

keep-alive 回环在 [HttpServerResponseCommand.cc:56-64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerResponseCommand.cc#L56-L64)：`supportsPersistentConnection()` 成立时用**同一个 `HttpServer` 对象**（其 `headerProcessor_` 已 clear、DiskWriter 已 reset）再造 `HttpServerCommand`，且 `getCuid()` 原样传递——所以日志里同一条 keep-alive 连接的所有请求共享同一个 CUID，这是读日志时辨别「连接」与「请求」的小技巧。

#### 4.3.4 代码实践

**实践目标**：亲眼看到 keep-alive 回环与 CUID 不变。

1. 操作步骤：
   ```bash
   ./src/aria2c --enable-rpc --rpc-listen-port=6800 --log=rpc.log --log-level=info
   # curl 对同一主机的多个 URL 自动复用连接：
   curl -sv http://127.0.0.1:6800/jsonrpc \
        -d '{"jsonrpc":"2.0","id":"1","method":"aria2.getVersion","params":[]}' \
        -d '{"jsonrpc":"2.0","id":"2","method":"aria2.tellActive","params":[]}' \
     2>&1 | grep -E 'Re-using|Connected'
   grep -E 'Persist connection|Accepted the connection' rpc.log
   ```
2. 需要观察的现象：curl 输出显示只建立了一次 TCP 连接（第二条请求 `Re-using existing connection`）；`rpc.log` 中只有一次 `Accepted the connection`，但有两个 `Persist connection`（INFO 级）与两条 `HTTP Server received request`。
3. 预期结果：与 [HttpServerResponseCommand.cc:59-63](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerResponseCommand.cc#L59-L63) 的回环逻辑吻合；两次请求的 CUID 相同。若 curl 版本行为不同（每次新建连接），可改用 `--keepalive-time` 或换 Python `http.client` 复用连接验证（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `AbstractHttpServerResponseCommand` 是模板方法模式的骨架，`HttpServerResponseCommand` 与 `WebSocketResponseCommand` 却都只有十几行？

**答案**：发送的公共部分（部分写、超时、注册调整、写空判定）全部在骨架的 `execute` 里；两兄弟唯一的差异是「响应发完之后干什么」，被提炼成 `afterSend` 虚函数：普通响应在 keep-alive 时回环到 `HttpServerCommand`，WebSocket 响应则创建会话命令。这与 u2-l3 `AbstractCommand::executeInternal` 钩子、u4-l1 各协议命令链是同一设计语言。

**练习 2**：请求体超限（`--rpc-max-request-size`）时为什么直接销毁连接而不是回 413？

**答案**：回 413 意味着还要把剩余请求体读完（或精准关闭），而服务器已决定拒绝，继续读只是浪费带宽与调度；直接 return true 销毁命令、连接随之关闭，客户端会看到连接中断。代码里还贴心地打了一条提示用户调大选项的 INFO 日志（[HttpServerCommand.cc:248-251](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L248-L251)）。

### 4.4 WebSocket 升级与会话：wslay 事件驱动模型

#### 4.4.1 概念说明

WebSocket 的前半生是 HTTP：升级请求就是普通 GET，由 4.3 的第一棒识别；`101` 响应发完的那一刻起，这条连接改由**会话对象** `WebSocketSession` 与**常驻命令** `WebSocketInteractionCommand` 接管。

`WebSocketSession` 封装一个 **wslay 事件上下文**。wslay 是「事件驱动」的 WebSocket 库：它不管 socket，宿主通过五个回调把真实 I/O 接进来：

| 回调 | 方向 | aria2 里的实现 |
|------|------|---------------|
| `recv_callback` | wslay→宿主：要读字节 | 包 `SocketCore::readData`，EWOULDBLOCK 翻译成 `WSLAY_ERR_WOULDBLOCK` |
| `send_callback` | wslay→宿主：要写字节 | 包 `SocketCore::writeData`，同样翻译阻塞语义 |
| `on_frame_recv_start_callback` | wslay→宿主：帧头到了 | 控制帧则置 `ignorePayload_` |
| `on_frame_recv_chunk_callback` | wslay→宿主：一块载荷到了 | 非控制帧则喂增量 JSON 解析器 |
| `on_msg_recv_callback` | wslay→宿主：整条消息完整了 | `parseFinal` 收尾 → 执行 RPC → 把响应排进出站队列 |

理解 wslay 的关键是「**队列 + 泵**」：`addTextMessage` 只是把帧描述符排进 wslay 的出站队列（不写 socket）；真正的写发生在命令调用 `wslay_event_send`（泵）时，它逐帧调 `send_callback`。同理 `wslay_event_recv`（泵）驱动收方向。两个泵分别由 `onReadEvent`/`onWriteEvent` 包裹，被 `WebSocketInteractionCommand` 在 socket 可读/可写时调用。

#### 4.4.2 核心流程

```text
升级握手（HttpServerCommand::execute 内）
  客户端 GET /jsonrpc + Upgrade: websocket + Connection: Upgrade
       + Sec-WebSocket-Key: <随机24字节base64> + Sec-WebSocket-Version: 13
  ├─ websocketHandshake(头)：
  │    非 GET 或缺 Key → 400
  │    Version != 13   → 426（并回 Sec-WebSocket-Version: 13 告知正确版本）
  │    路径非 /jsonrpc  → 404
  │    否则             → 101
  └─ 101：Accept = Base64(SHA1(Key + GUID)) → feedUpgradeResponse
           → WebSocketResponseCommand 发送

WebSocketResponseCommand::afterSend（101 发完）
  ├─ new WebSocketSession(socket, e)      ← 初始化 wslay 服务端上下文
  ├─ new WebSocketInteractionCommand(cuid, session, e, socket)
  └─ session->setCommand(command)          ← 会话记下宿主命令（供通知推送用）

WebSocketInteractionCommand::execute（常驻循环）
  ├─ isHaltRequested → 销毁
  ├─ onReadEvent/onWriteEvent 任一失败 → 记日志销毁（对端 EOF 或协议错）
  ├─ finish()（既不想读也不想写）→ 销毁
  └─ updateWriteCheck + 自排

收一条 RPC 请求的路径：
  socket 可读 → wslay_event_recv → recv_callback 取字节 → 帧回调链
  → onFrameRecvChunk: parseUpdate（增量 JSON，超 --rpc-max-request-size 截断）
  → onMsgRecv: parseFinal → Dict/List 分派 processJsonRpcRequest
  → addResponse: toJson → addTextMessage(入 wslay 出站队列, 未授权则延迟 1s)
  → wslay 想写 → updateWriteCheck 注册写关注 → 下轮 wslay_event_send 写出
```

#### 4.4.3 源码精读

握手校验与密钥计算都在 `HttpServerCommand.cc` 的匿名命名空间里。[HttpServerCommand.cc:137-152](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L137-L152) `websocketHandshake` 按上文顺序返回 400/426/404/101；[HttpServerCommand.cc:125-133](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L125-L133) `createWebSocketServerKey` 就是 2.2 节的公式：拼接 RFC 6455 魔法 GUID 后取 SHA-1 再 base64。

升级分支本体在 [HttpServerCommand.cc:211-244](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerCommand.cc#L211-L244)：`fieldContains(UPGRADE, "websocket") && fieldContains(CONNECTION, "upgrade")` 是大小写不敏感的「头字段包含某 token」判定（`HttpHeader` 提供，u4-l2 讲过白名单存储）；101 时算 key、`feedUpgradeResponse` 排响应、派生 `WebSocketResponseCommand`；非 101 或未编译 WebSocket（`ENABLE_WEBSOCKET` 宏）则走普通响应分支。注意这一切发生在**鉴权之后**——升级请求同样要过 Basic 认证（如果配置了 `--rpc-user`）。

[WebSocketResponseCommand.cc:55-64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketResponseCommand.cc#L55-L64) `afterSend` 是「HTTP 世界」与「WebSocket 世界」的交接点：101 字节全部写出后创建会话与常驻命令，并 `wsSession->setCommand(command.get())` 存下裸指针——4.5 节的通知广播就靠它反向找到命令去开写关注。

[WebSocketSession.cc:213-232](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc#L213-L232) 构造函数：清零回调结构体、挂五个回调、`wslay_event_context_server_init`（服务端角色意味着**自动处理掩码校验、自动回 pong、自动响应 close**）、`wslay_event_config_set_no_buffering(wsctx_, 1)` 让控制帧载荷不进 wslay 内部缓冲——因此 [WebSocketSession.cc:136-143](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc#L136-L143) 的帧开始回调要置 `ignorePayload_`，让 [WebSocketSession.cc:146-156](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc#L146-L156) 的分块回调跳过 ping/pong 载荷，不污染 JSON 解析器。

两个 I/O 回调体现「异常世界到错误码世界」的翻译：[WebSocketSession.cc:59-82](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc#L59-L82) `sendCallback` 与 [WebSocketSession.cc:86-113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc#L86-L113) `recvCallback` 都把 SocketCore 的 `wantRead/wantWrite` 标志（u2-l4 讲的非阻塞语义）翻译成 `WSLAY_ERR_WOULDBLOCK`（可恢复，wslay 稍后再试）或 `WSLAY_ERR_CALLBACK_FAILURE`（致命，wslay 走关闭流程）。

消息完整回调 [WebSocketSession.cc:160-211](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc#L160-L211) `onMsgRecvCallback`：非控制帧才处理；`parseFinal(nullptr, 0)` 收尾增量解析（字节早在分块回调里喂完了）；随后 Dict→单调用、List→批量、失败→`-32700`/`-32600` 的分派逻辑与 `HttpServerBodyCommand` 逐行同构——RPC 语义层被两条传输复用。结果经 [WebSocketSession.cc:116-133](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc#L116-L133) `addResponse` 序列化后进 [WebSocketSession.cc:282-299](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc#L282-L299) `addTextMessage`：`delayed` 为真（未授权）时包一层 `TextMessageCommand`+`DelayedCommand` 延迟 1 秒（[WebSocketSession.cc:263-280](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc#L263-L280)），否则 `wslay_event_queue_msg(WSLAY_TEXT_FRAME)` 直接入队。

[WebSocketSession.cc:311-322](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc#L311-L322) `parseUpdate` 用 `receivedLength_` 累计并对照 `--rpc-max-request-size` 截断超大请求——WebSocket 没有头部长度可查，限制只能在解析层做。

常驻命令 [WebSocketInteractionCommand.cc:87-110](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketInteractionCommand.cc#L87-L110) `execute` 只做两件事：驱动两个泵、判断生死。注意它的退出条件只有 `isHaltRequested()`（[WebSocketInteractionCommand.cc:89](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketInteractionCommand.cc#L89)），不像 HTTP 命令还查 `downloadFinished()`——RPC-only 场景下引擎可以长期空转，WebSocket 会话必须比「下载全部完成」活得更久。构造/析构（[WebSocketInteractionCommand.cc:51-71](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketInteractionCommand.cc#L51-L71)）负责在 `WebSocketSessionMan` 登记与注销自己，这正是会话集合与会话生命周期绑定的机制。

#### 4.4.4 代码实践

**实践目标**：完成一次 WebSocket 升级握手并在文本帧里调用 RPC。

1. 操作步骤：
   ```bash
   ./src/aria2c --enable-rpc --rpc-listen-port=6800 --log=ws.log --log-level=debug
   # 方式一：wscat（Node.js，npm install -g wscat）
   wscat -c ws://127.0.0.1:6800/jsonrpc
   # 连上后在交互提示符里输入：
   {"jsonrpc":"2.0","id":"1","method":"aria2.getVersion","params":[]}
   # 方式二：无 wscat 时用 Python（pip install websockets）：
   python3 - <<'EOF'
   import asyncio, websockets, json
   async def main():
       async with websockets.connect("ws://127.0.0.1:6800/jsonrpc") as ws:
           await ws.send(json.dumps({"jsonrpc":"2.0","id":"1",
                                     "method":"aria2.getVersion","params":[]}))
           print(await ws.recv())
   asyncio.run(main())
   EOF
   ```
2. 需要观察的现象：客户端收到一条 JSON 文本帧，含 `result.version`（与 `aria2c --version` 一致）和 `id:"1"`；`ws.log`（debug 级）中能找到 `HTTP Server received request`（升级 GET）与 `HTTP Server sends upgrade response`（101 应答，含 Sec-WebSocket-Accept）。
3. 预期结果：响应帧与 [WebSocketSession.cc:160-211](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc#L160-L211) 的处理路径对应：onMsgRecv → parseFinal → processJsonRpcRequest → addResponse → addTextMessage → 下一轮 `wslay_event_send` 写出。若把 URL 换成 `ws://127.0.0.1:6800/rpc`，应在日志看到 404 分支（websocketHandshake 的路径校验）。命令输出以本地环境为准（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：客户端发来一个 `ping` 控制帧，aria2 的代码里有哪一行处理它？

**答案**：一行都没有——pong 回复由 wslay 内部自动完成（`wslay_event_context_server_init` 创建的服务端上下文的内置行为）。aria2 代码只做了「别把 ping 载荷喂给 JSON 解析器」的隔离：帧开始回调置 `ignorePayload_`（[WebSocketSession.cc:136-143](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc#L136-L143)）。`onMsgRecvCallback` 里对控制帧回 `-32600` 的分支实际是对「控制帧出现在消息回调」这一异常情况的防御。

**练习 2**：为什么 `WebSocketSession::addTextMessage` 不直接写 socket，而是先入 wslay 队列？

**答案**：两个原因。其一，socket 可能不可写（内核缓冲满），直接写会阻塞或失败；入队后由命令层的写关注驱动，等 socket 可写时 `wslay_event_send` 再写，符合单线程 Reactor 的非阻塞纪律。其二，通知可能从任意命令的执行路径里产生（见 4.5），而「写 socket」必须集中在本会话自己的命令里做，队列正好解耦了「生产」与「发送」两端。

### 4.5 通知广播：WebSocketSessionMan 与 Notifier 事件链

#### 4.5.1 概念说明

WebSocket 相对 HTTP 轮询的杀手锏是**服务器主动推送**。aria2 定义了六种下载事件（与手册 `JSON-RPC over WebSocket` 一节对应）：

`aria2.onDownloadStart`、`aria2.onDownloadPause`、`aria2.onDownloadStop`、`aria2.onDownloadComplete`、`aria2.onDownloadError`、`aria2.onBtDownloadComplete`。

事件的生产者是下载引擎各处（任务启动、暂停、停止、完成、出错、BT 下载完成），消费者是所有在线 WebSocket 会话。中间靠两级分发：

- **Notifier（单例）**：观察者模式的中枢，任何「关心下载事件」的组件（RPC 的会话管理器、libaria2 的用户回调 u8-l5）都可以注册为 `DownloadEventListener`。
- **WebSocketSessionMan（每引擎一个）**：持有当前所有 WebSocket 会话的集合，把一个事件扇出成 N 份 JSON 文本、塞进每个会话的 wslay 出站队列，并「戳」一下各会话的宿主命令让它去写。

通知报文是**没有 `id` 字段**的 JSON-RPC 通知形态（客户端不得回应），形如：

```json
{"jsonrpc":"2.0","method":"aria2.onDownloadStart","params":[{"gid":"2089b05ecca3d829"}]}
```

#### 4.5.2 核心流程

以 `aria2.onDownloadStart` 为例的完整链路（生产 → 扇出 → 落字节）：

```text
① 生产：RequestGroupMan::fillRequestGroupFromReserver
      任务从等待队列升为活动 → notifyDownloadEvent(EVENT_ON_DOWNLOAD_START, group)
② 本地包装（RequestGroupMan.cc:242-249）
      → SingletonHolder<Notifier>::instance()->notifyDownloadEvent(event, group)
③ 多播：Notifier::notifyDownloadEvent
      for (listener : listeners_) listener->onEvent(event, group)
④ 扇出：WebSocketSessionMan::onEvent → addNotification(method, group)
      构造 Dict{jsonrpc, method, params:[{gid}]} → json::encode
      for (session : sessions_)
        ├─ session->addTextMessage(msg, false)     ← 入该会话的 wslay 出站队列
        └─ session->getCommand()->updateWriteCheck() ← 戳命令开写关注
⑤ 落字节：本轮或下一轮事件循环
      EventPoll 报告该 socket 可写 → WebSocketInteractionCommand 被唤醒
      → execute → wsSession_->onWriteEvent() → wslay_event_send
      → send_callback → SocketCore::writeData  ← 字节真正上线
```

关键理解：**④ 只排队不发送**。产生事件的可能是任何命令（例如补任务的 `FillRequestGroupCommand`），此刻直接写别人的 socket 会破坏「每个命令只碰自己 socket」的纪律；排队 + 戳写关注把真正的发送留给会话自己的命令在下一轮完成。

#### 4.5.3 源码精读

事件生产端的代表性点位：任务启动在 [RequestGroupMan.cc:582](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L582)（`fillRequestGroupFromReserver` 内，恰好在执行 `--on-download-start` 钩子之后）；任务收尾的三种结局（完成/出错/停止）在 [RequestGroupMan.cc:277-283](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L277-L283)；此外 BT 完成事件还有 `DefaultPieceStorage.cc:503` 与 `BtCheckIntegrityEntry.cc:83` 两个源头。它们都汇入 [RequestGroupMan.cc:242-249](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L242-L249) 的小包装函数，再进 Notifier 单例。

[Notifier.cc:45-56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Notifier.cc#L45-L56)：注册与多播总共十几行——监听器列表就是一个 `vector`，`notifyDownloadEvent` 逐个调 `onEvent`。注册动作发生在 4.1 节讲过的 [MultiUrlRequestInfo.cc:211-213](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L211-L213)。

扇出核心 [WebSocketSessionMan.cc:68-84](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSessionMan.cc#L68-L84) `addNotification`：用 u9-l2 将学到的 ValueBase `Dict`/`List` 手工搭出通知对象（`gid` 经 `GroupId::toHex` 转成 16 进制串），`json::encode` 序列化后遍历 `sessions_` 逐个入队并戳命令。会话集合的增删由 `WebSocketInteractionCommand` 的构造/析构驱动（[WebSocketSessionMan.cc:54-66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSessionMan.cc#L54-L66)），所以集合里永远是「活着且仅活着」的会话。[WebSocketSessionMan.cc:86-125](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSessionMan.cc#L86-L125) 是六种事件名常量与 `DownloadEvent` 枚举到方法名的 switch 映射，`onEvent` 只是两者的一层转发。

被「戳」的一方是 [WebSocketInteractionCommand.cc:73-85](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketInteractionCommand.cc#L73-L85) `updateWriteCheck`：判断条件是 `socket_->wantWrite() || wsSession_->wantWrite()`——后者即 `wslay_event_want_write`（出站队列非空），通知入队后它立刻变真，命令随即注册写关注，被 EventPoll 在下轮唤醒后走 4.4 的发送路径。

#### 4.5.4 代码实践

**实践目标**：端到端验证一条 `aria2.onDownloadStart` 通知的诞生与送达，并把它逐跳映射到源码。

1. 实践目标：观察「事件 → Notifier → 扇出 → wslay 队列 → socket 字节」的全链路。
2. 操作步骤：
   ```bash
   # 终端 1：服务端
   ./src/aria2c --enable-rpc --rpc-listen-port=6800 --log=ws.log --log-level=debug
   # 终端 2：WebSocket 订阅者
   wscat -c ws://127.0.0.1:6800/jsonrpc     # 或用 4.4.4 的 Python 脚本
   # 终端 3：HTTP 触发者——添加一个注定快速失败的下载（域名不存在）
   curl -s http://127.0.0.1:6800/jsonrpc -d '{
     "jsonrpc":"2.0","id":"1","method":"aria2.addUri",
     "params":[["http://no-such-host.invalid/big.iso"]]}'
   ```
   等几秒（DNS 失败重试耗尽），观察 wscat 终端。
3. 需要观察的现象：wscat 陆续收到三条**无 id** 的通知：`aria2.onDownloadStart`（addUri 生效）、`aria2.onDownloadError`（解析失败）；再触发一次 `aria2.remove` 可看到 `aria2.onDownloadStop`。`ws.log` 里同一时间线附近能看到引擎处理任务的常规日志。
4. 预期结果：把收到的每条通知对照 [WebSocketSessionMan.cc:86-94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSessionMan.cc#L86-L94) 的常量表与 4.5.2 的五步流程，写出「这条 JSON 在哪一行被构造、在哪一行入队、被哪个命令写上 socket」的答案：构造在 [WebSocketSessionMan.cc:71-79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSessionMan.cc#L71-L79)、入队在 [WebSocketSessionMan.cc:80-82](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSessionMan.cc#L80-L82) 调用的 `addTextMessage`（[WebSocketSession.cc:295-298](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc#L295-L298)）、写出由 [WebSocketInteractionCommand.cc:92](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketInteractionCommand.cc#L92) 的 `onWriteEvent` 驱动。通知时机受 DNS 重试影响，具体条数可能多一条 Start/少一条 Error，属正常（待本地验证）。

#### 4.5.5 小练习与答案

**练习 1**：如果同一时刻有两个 WebSocket 客户端在线，`addNotification` 会发两份报文吗？JSON 会编码几次？

**答案**：发两份（每个会话各自 `addTextMessage` 入队），但 JSON 只编码一次——`json::encode` 在循环之前完成（[WebSocketSessionMan.cc:79-82](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSessionMan.cc#L79-L82)），循环内复用同一个 `msg` 字符串。扇出的是文本，不是对象。

**练习 2**：通知报文为什么没有 `id` 字段？如果客户端对通知回了一条响应，会发生什么？

**答案**：JSON-RPC 规范里没有 `id` 的是 notification，语义上是「单方面告知、不期望回应」。客户端若回了，服务端 `onMsgRecvCallback` 仍会把它当普通请求解析执行——只是它不是合法方法调用，多半得到 `-32600`/错误响应；协议上不禁止，但属于客户端实现错误。手册也明确写了「client must not respond to it」。

**练习 3**：`WebSocketSessionMan` 与 `Notifier` 的职责为何不合并成一个类？

**答案**：Notifier 是进程级通用事件总线，可能挂多个不同类型的监听器（libaria2 的 `DownloadEventListener` 回调也挂在这里，见 u8-l5）；WebSocketSessionMan 是「一种特定消费者」——它知道会话集合、知道怎么把事件变成 JSON 文本帧。合并会让通用总线依赖具体传输，也堵死了第二个消费者同时挂载的可能。

## 5. 综合实践

**任务：给「RPC 服务器全景」写一份带证据的调用链档案。**

用一个正在运行的 aria2 与两个客户端，把本讲三条主线串起来验证：

1. **准备**：
   ```bash
   ./src/aria2c --enable-rpc --rpc-listen-port=6800 --rpc-secret=mysecret \
               --log=lab.log --log-level=debug
   ```
2. **第一幕（命令循环与 keep-alive）**：用 `curl` 的两个 `-d` 参数在同一连接上连发两次 `aria2.getVersion`（secret 令牌放 `params` 第一个元素，形如 `"params":["token:mysecret", []]`——令牌校验属于 RPC 方法层，细节留给 u8-l4，这里照抄即可）。从 `lab.log` 摘出：一次 `Accepted the connection`、两次 `HTTP Server received request`、两次 `Persist connection`，并确认两轮 CUID 相同。
3. **第二幕（升级与会话）**：保持 curl 断开，用 4.4.4 的 Python 脚本连上 WebSocket 并打印收到的每条消息前 80 字符；在 `lab.log` 找到 `HTTP Server sends upgrade response` 行，抄录其中的 `Sec-WebSocket-Accept` 值，用 `echo -n "<你抓包看到的Key>+258EAFA5-E914-47DA-95CA-C5AB0DC85B11" | openssl dgst -sha1 -binary | base64` 验证能复现它（Key 需从升级请求头抓取，可用 `curl -v` 或 Wireshark；抓不到就用 Python `websockets` 的调试输出）。
4. **第三幕（通知扇出）**：WebSocket 保持连接，用 curl `addUri` 添加一个本地小文件下载（可先用 `python3 -m http.server` 起一个静态服务）和一个必失败 URI；把收到的每条通知按 `方法名 → 触发源码行 → WebSocketSessionMan 中的构造点 → 写出命令` 四列填成表格。
5. **交付物**：一份 Markdown 笔记，包含三幕的证据（日志行、报文、计算结果）与表格；每条证据标注对应源码永久链接。若某步无法本地执行（如无 openssl/wscat），写明替代方法与「待本地验证」。

这个任务完成后，你就亲手走过了「一条 HTTP 请求的进出」与「一条服务器推送的出生」两条完整路径。

## 6. 本讲小结

- RPC 服务器完全寄生在 DownloadEngine 的事件循环上：`HttpListenCommand` 是 accept 守护命令，IPv4/IPv6 各一条、一族成功即运行，无任何额外线程。
- `HttpServer` 是纯编解码器，复用客户端 HTTP 栈的 `HttpHeaderProcessor`（SERVER_PARSER 模式）、`SocketRecvBuffer`、`SocketBuffer`；请求体经 `JsonDiskWriter`（写入即解析的伪磁盘）直接流进增量 JSON 解析器。
- 一次 RPC 请求是 `HttpServerCommand`（读头+鉴权+升级识别）→ `HttpServerBodyCommand`（读体+执行 RPC）→ `ResponseCommand`（写回）的三棒接力；keep-alive 时 `afterSend` 用同一个 `HttpServer` 对象与同一 CUID 回到第一棒，未授权响应统一延迟 1 秒发送以防爆破。
- WebSocket 前半程是 HTTP：握手校验（方法/Key/版本 13/路径）与 `Sec-WebSocket-Accept` 计算都在 `HttpServerCommand.cc`；`101` 发完由 `WebSocketResponseCommand::afterSend` 交接给 `WebSocketSession`（wslay 上下文 + 五回调）与常驻的 `WebSocketInteractionCommand`。
- wslay 是「队列 + 泵」模型：收方向 `wslay_event_recv` 驱动帧回调链逐块喂 JSON 解析器，发方向 `addTextMessage` 只入队、由写关注唤醒 `wslay_event_send` 经 `send_callback` 落字节；控制帧由 wslay 自动应答且载荷被隔离在解析器之外。
- 通知链是 `RequestGroupMan（生产）→ Notifier 单例（多播）→ WebSocketSessionMan（JSON 编码一次、逐会话入队+戳命令）→ 会话命令写关注 → wslay_event_send`；「排队不发送」保证了任何命令都能安全地产生通知而不越权碰别人的 socket。

## 7. 下一步学习建议

本讲把「传输层」讲完了：HTTP 请求/响应的成帧、升级握手、WebSocket 帧收发。下一讲 **u8-l4 RPC 方法分发：XML-RPC 与 JSON-RPC** 将打开本讲反复当黑盒的 `rpc::processJsonRpcRequest` / `rpc::getMethod`：方法工厂如何按名字分发、`aria2.addUri` 等方法如何校验参数、`--rpc-secret` 令牌在 `authorize` 的哪一步被检查、`RpcResponse` 如何被序列化回 ValueBase。建议提前浏览 [src/RpcMethod.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethod.cc) 与 [src/RpcMethodImpl.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc)，并把 u9-l2（ValueBase）作为并行阅读材料。若你更关心嵌入视角，u8-l5（libaria2）会展示同一套引擎如何以 C API 暴露、其事件回调如何复用本讲的 Notifier 总线。
