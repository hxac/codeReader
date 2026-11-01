# HTTP 下载命令链：从发起到落盘的接力

## 1. 本讲目标

学完本讲，你应该能够：

1. 按顺序说出一次 HTTP 下载中命令链各棒命令的职责边界：谁建连接、谁发请求、谁判响应、谁搬数据。
2. 解释命令切换时 Socket、Request、Segment、HttpConnection 这些"接力棒"资源是如何一棒一棒传下去的。
3. 理解连接失败、响应异常时的三条出口：换地址重试、换 URI 重试、放弃整组下载。
4. 学会用 `--log` 日志把一次真实下载的命令接力过程"拍成慢动作"来验证源码理解。

本讲是「协议下载链路」单元的第一讲。我们将第一次完整地走通一条具体协议的下载路径，之后 FTP/SFTP、代理、流式过滤等讲义都会反复回到这条主干的骨架上。

## 2. 前置知识

本讲假设你已读过两篇前置讲义，这里只做最简回顾，不展开：

- **Command 生命周期（u2-l3）**：aria2 的所有工作都由 Command 对象完成。约定是 `execute()` 返回 `true` 表示命令生命周期结束（引擎将其销毁，资源在析构中释放）；返回 `false` 表示命令必须已经把自己重新排队（`addCommandSelf`）。`AbstractCommand::execute()` 是模板方法：先做超时判定等公共检查，再调用子类的 `executeInternal()` 钩子；子类抛出的异常在模板方法里被统一捕获并分流。
- **Segment 与 SegmentMan（u3-l4）**：文件被切成 piece（HTTP 下载默认 1MB，由 `--min-split-size`/`-k` 相关选项影响），单条连接在某个 piece 上持有一个 Segment 作为"写入游标"。SegmentMan 只记账：哪个 cuid 持有哪段、段完成了就 `completeSegment`、要退场就 `cancelSegment`。

再补充三个本讲反复出现的术语：

- **cuid**：Command UID，64 位整数。一条命令链从头到尾复用同一个 cuid，因此它是我们在日志里追踪"一次接力"的钥匙。
- **Request**：对一个 URI 的请求抽象，携带 URI、重定向历史、尝试次数、是否支持持久连接等信息。它会被"池化"（pool）回 FileEntry 以便复用。
- **HttpConnection**：一条已建立 TCP 连接上的"会话对象"，聚合了发送缓冲、接收缓冲（SocketRecvBuffer）和在途请求队列。命令链的中后段（Request/Response/Download 三棒）都靠它收发数据。

**一个关键的直觉**：HTTP 命令链像一场接力赛。每棒命令只跑自己那一小段，跑完就把"接力棒"（一组共享资源的 `shared_ptr`：Socket、Request、Segment、HttpConnection）通过**构造函数参数**交给下一棒，然后自己返回 `true` 退场。没有任何一棒命令会"阻塞地"完成整个下载——每一棒都只推进一小步就交棒，这正是单线程事件驱动引擎能同时跑几十条连接的原因。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/CreateRequestCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CreateRequestCommand.cc) | 第 0 棒：没有 URI 也没有 Socket 的"调度命令"，从 FileEntry 领一个 Request 并按协议发起下一棒 |
| [src/InitiateConnectionCommandFactory.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommandFactory.cc) | 按 URI 协议（http/https/ftp/sftp）选择具体的"建连命令" |
| [src/InitiateConnectionCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommand.cc) | 建连命令的公共基类：解析主机名、尝试多地址、Happy Eyeballs 备份连接的挂接点 |
| [src/HttpInitiateConnectionCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpInitiateConnectionCommand.cc) | HTTP 版建连命令：决定"复用连接池"还是"新建 TCP 连接" |
| [src/ConnectCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConnectCommand.cc) | 等待非阻塞 connect 完成、处理备份连接接管，然后经 ConnectChain 进入协议命令 |
| [src/HttpRequestConnectChain.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequestConnectChain.h) | 连接建立后组装 HttpConnection 并创建 HttpRequestCommand 的"交接脚本" |
| [src/HttpRequestCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequestCommand.cc) | 第 2 棒：组装并发送 HTTP 请求（含 TLS 握手、Range、Cookie、pipelining） |
| [src/HttpResponseCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc) | 第 3 棒：接收响应头，做状态码/长度/编码判定，决定下一棒去向 |
| [src/HttpDownloadCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpDownloadCommand.cc) | 第 4 棒的 HTTP 外壳：段结束后的连接回收与 pipelining 续棒 |
| [src/DownloadCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc) | 第 4 棒的主体：读 socket → StreamFilter → 写盘的"数据泵"，HTTP/FTP 共用 |
| [src/AbstractCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc) | 全体命令的基类：异常出口、重试（prepareForRetry）与换 URI（tryReserved） |
| [src/message.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h) | 日志消息模板，实践环节对照日志文本用 |

## 4. 核心概念与源码讲解

先给出全链路总览。一次普通的 HTTP 单连接下载，命令接力顺序是：

```text
CreateRequestCommand            （领 Request，选协议）
  └─> HttpInitiateConnectionCommand   （查连接池 / DNS / 发起 connect）
        ├─ 复用连接池 ──────────────> HttpRequestCommand
        └─ 新建连接 └─> ConnectCommand （等 connect 完成）
                            └─> HttpRequestConnectChain.run
                                  └─> HttpRequestCommand   （发请求，HTTPS 在此握手）
                                        └─> HttpResponseCommand （收响应头，做判定）
                                              ├─> HttpDownloadCommand （数据泵，自循环）
                                              │     └─ 段完成：发新请求 / 回收连接 / 退场
                                              └─> HttpSkipResponseCommand（3xx/4xx/5xx 时跳过响应体）
```

任何一棒抛出异常，都会回到 `AbstractCommand::execute()` 的异常出口，或换地址重来、或换 URI 重来、或终止整组下载（见 4.5）。下面逐模块精读。

### 4.1 HttpInitiateConnectionCommand：从 URI 到 TCP 连接

#### 4.1.1 概念说明

这一模块回答的问题是：**"给定一个 URI，怎么拿到一条可用的 TCP 连接？"**

它实际上由三个角色协同完成：

- **CreateRequestCommand**：链条真正的第 0 棒。它没有 Request 也没有 Socket，职责是"领任务"——从 FileEntry 的 URI 池里领出一个 Request，并按协议把接力交给对应的建连命令。
- **HttpInitiateConnectionCommand**：HTTP/HTTPS 版的建连命令。它先查 DNS 拿到 IP 地址，然后做一个关键决策：**连接池里有没有可以复用的持久连接？** 有就直接跳到发请求；没有就新建 TCP 连接。
- **ConnectCommand**：新建连接时的"等待员"。非阻塞 `connect()` 立即返回，真正的连接完成要等 socket 可写之后由 ConnectCommand 确认，确认成功才通过 ConnectChain 交接给协议命令。

为什么要把"建连"拆成 Initiate + Connect 两棒？因为非阻塞 connect 需要"发起"和"确认"两个时间点，而事件驱动引擎里每个时间点都要有一个命令来"认领"。拆开之后，DNS 超时（`--dns-timeout`）和连接超时（`--connect-timeout`）也能分别套在不同命令上计时。

#### 4.1.2 核心流程

```text
CreateRequestCommand::executeInternal():
    选 FileEntry（按写入偏移找到对应文件）
    request = fileEntry.getRequest(URISelector, ...)   # 领 URI；没有则抛 DlAbortEx
    若 request 还在"睡觉"（503 后的冷却期）→ 归还并自排等待
    按 request 的协议调用 InitiateConnectionCommandFactory
        http/https → HttpInitiateConnectionCommand
        ftp/sftp   → FtpInitiateConnectionCommand（下一单元讲）

HttpInitiateConnectionCommand（继承 InitiateConnectionCommand）:
    executeInternal():
        hostname/port = 直连 ? URI 的主机 : 代理的主机
        ipaddr = resolveHostname(addrs, hostname, port)   # 异步 DNS，没好就自排
        createNextCommand(hostname, ipaddr, port, addrs, proxyRequest):
            若连接池 popPooledSocket 命中:
                直接创建 HttpRequestCommand（跳过 ConnectCommand）
            否则:
                establishConnection(addr, port)          # 非阻塞 connect
                创建 ConnectCommand，挂上 HttpRequestConnectChain
                若首选地址是 IPv6 → setupBackupConnection 发起 IPv4 备份连接赛跑
        异常(RecoverableException):
            markBadIPAddress(该地址) → 还有缓存地址就重建自己换地址试
            地址耗尽 → 继续上抛

ConnectCommand::executeInternal():
    若备份连接先成功 → swapSocket 换成备份 socket
    checkIfConnectionEstablished() 未完成 → 返回 true（等写事件再来）
    连接确认成功 → chain_->run(this, e)   # HttpRequestConnectChain 创建 HttpRequestCommand
```

#### 4.1.3 源码精读

**第 0 棒领任务**。[src/CreateRequestCommand.cc:68-93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CreateRequestCommand.cc#L68-L93) 中，命令先确定 FileEntry，再调用 `getFileEntry()->getRequest(...)` 领取 URI。注意最后那个三目表达式：文件长度未知且开了 `--use-head`（或 dry-run）时用 `METHOD_HEAD`，否则用 `METHOD_GET`——这就是"第一次先用 HEAD 探大小、之后全部用 GET"的实现位置。

领到 Request 后，[src/CreateRequestCommand.cc:121-126](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CreateRequestCommand.cc#L121-L126) 把接力交给工厂。工厂在 [src/InitiateConnectionCommandFactory.cc:56-71](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommandFactory.cc#L56-L71) 按 protocol 字符串分发：`http`/`https` 走 HttpInitiateConnectionCommand，并在此时根据 `--enable-http-keep-alive`/`--enable-http-pipelining` 给 Request 打上 keepAlive/pipelining 提示标记（这两个标记会在 4.3、4.4 反复出现）。

**基类做 DNS 与多地址尝试**。[src/InitiateConnectionCommand.cc:87-99](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommand.cc#L87-L99)：`resolveHostname` 没解析完就 `addCommandSelf` 返回 `false` 自排（异步 DNS 的轮询模式，细节见 u2-l4）；解析完调用虚函数 `createNextCommand`，把产出的下一棒设为 `STATUS_ONESHOT_REALTIME` 立即执行。

[src/InitiateConnectionCommand.cc:100-120](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommand.cc#L100-L120) 是**连接失败的第一次兜底**：`createNextCommand` 抛出 `RecoverableException` 时，把刚失败的 IP 地址标记为坏地址（`markBadIPAddress`），只要 DNS 缓存里还有别的地址，就重建一个同样的 InitiateConnectionCommand 换下一个地址重试；地址耗尽才把异常上抛给 AbstractCommand 的异常出口。

**HTTP 子类的四路决策**。核心在 [src/HttpInitiateConnectionCommand.cc:71-148](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpInitiateConnectionCommand.cc#L71-L148) 的 `createNextCommand`，它是一个 2×2 决策矩阵：{走代理, 直连} × {池里有连接, 新建}：

- **直连 + 池命中**（[L119-L121](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpInitiateConnectionCommand.cc#L119-L121) 判定，[L136-L146](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpInitiateConnectionCommand.cc#L136-L146) 执行）：`popPooledSocket` 从引擎的连接池里借出一条 keep-alive 连接，当场新建 `HttpConnection`（内含新的 SocketRecvBuffer）并直接创建 **HttpRequestCommand**——跳过 ConnectCommand，因为连接早就建好了。
- **直连 + 新建**（[L122-L135](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpInitiateConnectionCommand.cc#L122-L135)）：打印 `CUID#.. - Connecting to addr:port` 日志，`createSocket()` + `establishConnection()` 发起非阻塞连接，创建 **ConnectCommand** 并通过 `setControlChain` 挂上 `HttpRequestConnectChain`——这个"脚本"规定了连接确认成功后该创建什么命令。最后 `setupBackupConnection` 在首选地址为 IPv6 时挂一个 IPv4 备份连接命令（Happy Eyeballs，见 u2-l4）。
- **走代理**的两路（[L76-L117](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpInitiateConnectionCommand.cc#L76-L117)）结构完全相同，区别是要按 `resolveProxyMethod` 决定挂 `HttpProxyRequestConnectChain`（GET 方式代理）还是 `HttpProxyRequestConnectChain`（CONNECT 隧道，https 必走隧道）。这部分留到 u4-l4 展开。

**等待员确认连接**。[src/ConnectCommand.cc:87-113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConnectCommand.cc#L87-L113) 的 `executeInternal` 三步走：

1. [L89-L101](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConnectCommand.cc#L89-L101)：若 IPv4 备份连接先完成了（`backupConnectionInfo_->ipaddr` 非空），把主命令的 socket 换成备份的（`swapSocket`），并把原 IPv6 地址标记为坏地址。
2. [L102-L106](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConnectCommand.cc#L102-L106)：`checkIfConnectionEstablished` 检查 `SO_ERROR`——非阻塞 connect 的标准确认动作（见 u2-l4 的三段式）。执行时机由构造函数 [L58-L60](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConnectCommand.cc#L58-L60) 的 `setWriteCheckSocket` 决定：命令订阅了写事件，而"socket 变为可写"正是非阻塞 connect 完成（无论成败）的信号，所以 `executeInternal` 被调用时 `SO_ERROR` 必有答案。该函数三种结局：连接成功返回 `true`；本地址失败但 DNS 缓存里还有别的地址 → 它**已经**排好了一个新的 InitiateConnectionCommand 去换地址，并返回 `false`，于是 ConnectCommand 走 `if (!...) return true` 退场销毁（接力已被别人接走）；地址耗尽 → 抛 `DL_RETRY_EX` 进入 4.5 的重试体系。
3. [L107-L111](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConnectCommand.cc#L107-L111)：连接确认成功，取消备份连接（置 `cancel`），然后 `chain_->run(this, getDownloadEngine())` 执行交接脚本并返回 `true` 退场。

**交接脚本**只有十几行，在 [src/HttpRequestConnectChain.h:50-62](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequestConnectChain.h#L50-L62)：新建 `SocketRecvBuffer` 和 `HttpConnection` 包住这条 socket，用**同一个 cuid** 创建 `HttpRequestCommand`，继承代理请求，设为实时一次性命令立即执行。注意 `HttpConnection` 是在这里（以及连接池命中分支里）诞生的——它从诞生起就属于这条命令链的中后段，而不是全局共享的。

#### 4.1.4 代码实践

**实践目标**：在日志中辨认"建连阶段"的两种形态——新建连接与连接池复用。

**操作步骤**（示例命令，请在你自己的机器上验证）：

1. 本地起一个支持 keep-alive 的 HTTP 服务（最简单的办法：`python3 -m http.server 8000`，在某个目录里放一个约 10MB 的文件）。
2. 第一次下载：`./src/aria2c http://127.0.0.1:8000/big.bin -d /tmp/a2test --log=first.log --log-level=info --allow-overwrite=true`
3. 紧接着第二次下载同一文件（换个保存名或加 `--allow-overwrite=true`）：观察第二条命令是否出现 `Connecting to` 日志。

**需要观察的现象**：日志中形如 `CUID#7 - Connecting to 127.0.0.1:80` 的行（文本模板见 [src/message.h:45](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L45)）。若服务器支持 keep-alive 且第二次请求成功从连接池借到连接，则该次请求的日志里**没有** `Connecting to` 行，直接出现 `Requesting:`。

**预期结果**：同一主机连续两次下载时，第二次应命中 `popPooledSocket` 快速路径。具体是否复用取决于服务器是否允许持久连接，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `HttpInitiateConnectionCommand::createNextCommand` 的直连分支里，连接池命中的路径不需要创建 ConnectCommand？

**答案**：ConnectCommand 的职责是"等待非阻塞 connect 完成并确认成功"。连接池里的 socket 是上一条命令链用完主动归还的、已确认可用的持久连接，不存在"等待连接建立"的阶段，所以可以直接进入发请求阶段（创建 HttpRequestCommand），只是仍需新建一个 HttpConnection 会话对象来包装这条复用的 socket。

**练习 2**：`InitiateConnectionCommand::executeInternal` 捕获 `RecoverableException` 后重建的是什么命令？为什么不是直接重试 `createNextCommand`？

**答案**：重建的是一个新的 InitiateConnectionCommand（经 `InitiateConnectionCommandFactory::createInitiateConnectionCommand`）。因为 DNS 解析结果和"当前该试哪个地址"的选择逻辑都在这个命令的执行流程里——重建它可以完整地重走"查缓存中下一个好地址 → 发起连接"的路径，并重新计时 DNS 超时；直接重试 `createNextCommand` 则拿到的还是同一个（可能已失败的）地址。

**练习 3**：`ConnectCommand` 构造函数为什么调用 `setWriteCheckSocket` 而不是 `setReadCheckSocket`？

**答案**：非阻塞 connect 完成的信号是 socket 变为**可写**（u2-l4 讲过的三段式：设非阻塞 → connect 容忍 EINPROGRESS → 写事件到来后查 SO_ERROR）。所以 ConnectCommand 订阅写事件；等它交接出 HttpRequestCommand 后，下一棒才转为订阅写事件发请求、再由读事件驱动收响应。

### 4.2 HttpRequestCommand：把请求写进 socket

#### 4.2.1 概念说明

这一棒回答：**"怎么把'我要下载这个文件的这一段'翻译成合法的 HTTP 请求报文并发出去？"**

它的输入是接力棒（Request、Socket、HttpConnection），外加从 SegmentMan 领到的 Segment（可能没有——第一次请求文件长度未知时无段可言）。它做三件事：

1. **HTTPS 专属**：如果是 https 协议，先完成 TLS 握手（`tlsConnect`）。
2. **组装 HttpRequest**：把 URI、Range（从 Segment 推导）、User-Agent、Cookie、认证信息、`Accept-Encoding: gzip` 等选项统统装进一个 HttpRequest 对象。
3. **发送并交棒**：经 `HttpConnection::sendRequest` 写入 socket（可能一次写不完），发送缓冲清空后创建 HttpResponseCommand 等响应。

值得强调的是 pipelining：开管道时，一条命令可以**连发多个**请求（每个未完成的 segment 一个），响应会按序回来，由同一个 HttpConnection 的在途请求队列对账。

#### 4.2.2 核心流程

```text
HttpRequestCommand::executeInternal():
    若发送缓冲为空:
        https 且未握手 → tlsConnect()；未完成则按 wantRead/wantWrite 自排
        若本命令没有 segment（首个请求，长度未知）:
            组装不带 Range 的 HttpRequest
            （--conditional-get 开启时附 If-Modified-Since）
            sendRequest()
        否则（有 segment）:
            对每个尚未发出请求的 segment:
                计算 endOffset（该请求覆盖到的文件偏移终点）
                组装带 Range 的 HttpRequest → sendRequest()
    否则:
        sendPendingData()   # 把上次没写完的继续写

    发送缓冲清空？
        是 → addCommand(HttpResponseCommand)，return true 退场
        否 → 按 wantRead/wantWrite 自排，return false
```

#### 4.2.3 源码精读

**构造与超时**：[src/HttpRequestCommand.cc:65-77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequestCommand.cc#L65-L77)。超时用 `--connect-timeout`（默认 60 秒）——注意语义上它管的是"从发起建连到请求发送完成"这段；同时禁用读检查、只订阅写事件，因为这一棒的兴趣是"能不能把请求写出去"。

**组装请求的工厂函数**：[src/HttpRequestCommand.cc:82-120](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequestCommand.cc#L82-L120) 的匿名命名空间函数 `createHttpRequest` 把十几项配置灌进 HttpRequest：User-Agent、`--header` 追加头、CookieStorage、AuthConfigFactory、`--http-accept-gzip`、`--http-no-cache`、endOffset 覆盖等。这个对象就是"请求的完整意图"，下一棒 HttpResponseCommand 判定响应时还要反过来问它（比如比对 Range 是否被满足）。

**TLS 握手**：[src/HttpRequestCommand.cc:127-136](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequestCommand.cc#L127-L136)。`tlsConnect` 返回 `false` 表示握手数据还没交互完，按 `wantRead`/`wantWrite` 决定订阅哪个事件后自排；这也是非阻塞 TLS 的标准姿势——握手可能要往返多个 RTT，每个往返之间命令都要退场让引擎干别的。

**两个发送分支**：[src/HttpRequestCommand.cc:137-173](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequestCommand.cc#L137-L173) 处理"无 segment"的首请求（长度未知，不带 Range，`--conditional-get` 时附 If-Modified-Since 实现增量下载）；[L174-L195](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequestCommand.cc#L174-L195) 处理有 segment 的情况：遍历所有 segment，`isIssued` 防重复发送，[L177-L188](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequestCommand.cc#L177-L188) 计算 `endOffset`——注意它不是"本 segment 的终点"，而是借 `getNextUsedIndex` 把请求的 Range 终点延伸到相邻已占用段的边界，目的是让服务器一次多给点数据（pipelining 时的优化）。FTP over HTTP 代理不支持 end byte，故有协议判断。

**发送的底层**：[src/HttpConnection.cc:125-134](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpConnection.cc#L125-L134)。`sendRequest` 打印 `CUID#.. - Requesting:\n<完整请求报文>` 日志（敏感信息会被 `eraseConfidentialInfo` 抹掉），把报文压入 socket 发送缓冲并尝试发送，最后把 HttpRequest 挂进 `outstandingHttpRequests_` 在途队列。报文如何从 HttpRequest 变成字符串（`createRequest()`）属于 u4-l2 的内容。

**交棒或自排**：[src/HttpRequestCommand.cc:200-211](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequestCommand.cc#L200-L211)。发送缓冲清空（`sendBufferIsEmpty`）就创建 **HttpResponseCommand** 并返回 `true`；没清空就 `sendPendingData` 已在前面续写过、此处按 socket 的读写意愿自排返回 `false`，下轮再来。注意 HttpResponseCommand 与 HttpRequestCommand 共享同一个 `httpConnection_`——在途请求队列就在里面，这是 pipelining 对账的基础。

#### 4.2.4 代码实践

**实践目标**：亲眼看到 aria2 发出的原始 HTTP 请求报文，并验证 Range 头的出现条件。

**操作步骤**：

1. 继续用 4.1.4 的本地服务器，先正常下载一次让 aria2 记住文件不存在：`./src/aria2c http://127.0.0.1:8000/big.bin -d /tmp/a2test -o range.bin --log=plain.log --log-level=info`
2. 开多连接分段下载：`./src/aria2c http://127.0.0.1:8000/big.bin -d /tmp/a2test -o range2.bin -x2 -s2 --log=range.log --log-level=info`
3. 在两份日志中分别检索 `Requesting:`，对比请求头差异。

**需要观察的现象**：`Requesting:` 后跟完整请求文本（模板见 [src/message.h:47](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L47)）。重点看 `Range:` 头：单连接首次请求通常没有 Range（或 `bytes=0-`）；`-x2 -s2` 时应能看到两个不同 cuid 各自发出的请求携带互不重叠的 `Range: bytes=A-B`。

**预期结果**：`-x2 -s2` 的日志中出现两个 cuid，各有一条 `Requesting:`，Range 起点不同；`Connection: close` 是否出现取决于 keep-alive 选项与服务器。**待本地验证**（不同服务器实现可能差异）。

#### 4.2.5 小练习与答案

**练习 1**：`sendRequest` 之后请求已经"发出"了吗？为什么还需要 `outstandingHttpRequests_` 队列？

**答案**：请求被压入发送缓冲并尽力写出，但可能因内核缓冲满而只写出一部分（此时走 `sendPendingData` 续写）。`outstandingHttpRequests_` 记录"哪些请求已经发出但还没收到响应"，作用有二：一是 pipelining 时按序把响应与请求配对；二是 receiveResponse 时知道该用哪个 HttpRequest 来解释这个响应（如比对 Range）。

**练习 2**：为什么 HTTPS 的 TLS 握手放在 HttpRequestCommand 里而不是 ConnectCommand 里？

**答案**：分层考虑——ConnectCommand 只负责"TCP 层连通"这一所有协议共享的事务，且它还要兼顾备份连接的接管与取消；TLS 是 HTTP(S) 协议层的事务，放在协议命令里可以让不同协议自行决定（例如 FTP 的 TLS 就在 FTP 协商命令里做）。同时HttpRequestCommand 的 `--connect-timeout` 计时天然覆盖"TCP + TLS"整个建连期。

**练习 3**：`executeInternal` 末尾如果发送缓冲没清空，命令自排时为什么还要 `setReadCheckSocketIf(getSocket(), getSocket()->wantRead())`？发请求不是只关心"可写"吗？

**答案**：这是为 TLS 握手准备的：TLS 握手阶段可能需要**读**对端数据（wantRead 为真），也可能需要写（wantWrite 为真），下一轮要继续 `tlsConnect`。对纯 HTTP 而言 wantRead 通常为假，此调用等于无害的空操作。

### 4.3 HttpResponseCommand：响应头的裁判

#### 4.3.1 概念说明

这一棒回答：**"服务器答应了没有？答应给多少？给的是哪种形式？接下来该谁上场？"**

它是整条链里**判定逻辑最重**的一棒。收到响应头后，它依次裁定：

1. **持久连接与管道**：对端支持 keep-alive 吗？允许几个在途请求？
2. **状态码分流**：304（文件没变，直接判完成）、3xx/4xx/5xx（交给 HttpSkipResponseCommand 跳过响应体并处理重定向/记错）、2xx（进入正常下载路径）。
3. **首响应定型**：如果这是本任务第一个响应（还没初始化 PieceStorage），要从头里确定文件长度、文件名、Content-Type，然后走"定长分段下载"或"不定长流式下载"两条路。
4. **创建数据泵**：最终绝大多数 2xx 路径都会经 `createHttpDownloadCommand` 造出 HttpDownloadCommand，并把 Transfer-Encoding/Content-Encoding 的 StreamFilter 链装上去。

注意它的"循环"属性：若响应头还没到齐，它自排等待；pipelining 时一个连接上有多个响应，它会**逐个**被创建/执行（每个响应对应一次交棒）。

#### 4.3.2 核心流程

```text
HttpResponseCommand::executeInternal():
    httpResponse = httpConnection->receiveResponse()
    没收到完整头 → 自排等读事件，return false
    validateResponse()          # 状态码合法性，可能抛异常
    retrieveCookie()            # Set-Cookie 入库
    记录对端是否支持持久连接 / 计算管道深度上限

    状态码 == 304 ?  → 标记所有 piece 完成、回收连接、退场
    状态码 >= 300 ?  → skipResponseBody() 创建 HttpSkipResponseCommand（跳体+重定向）退场

    若本任务还没有 PieceStorage（首响应）:
        顺手处理 Metalink/HTTP 的 Link/Digest 头（若启用）
        totalLength = 实体长度；确定落盘文件名
        长度已知且无需解压 → handleDefaultEncoding()
            建 PieceStorage/控制文件、领 0 号 segment、
            可能先经过校验/预分配"停车场"，把 HttpDownloadCommand 压入其队列
        长度未知或需解压 → handleOtherEncoding()
            从 0 偏移开写，直接创建 HttpDownloadCommand
    否则（分段下载中的后续响应）:
        校验总长度一致 → 直接创建 HttpDownloadCommand（装好过滤器）
```

#### 4.3.3 源码精读

**等待响应头**：[src/HttpResponseCommand.cc:146-154](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L146-L154)。`receiveResponse()` 返回 `nullptr` 说明头还没到齐（它内部用 HttpHeaderProcessor 流式解析，见 u4-l2；解析完成的判定见 [src/HttpConnection.cc:160-170](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpConnection.cc#L160-L170)，1xx 临时响应会被跳过继续等最终响应）。构造函数里的 `checkSocketRecvBuffer()`（[L139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L139)）已顺手订阅了读事件。

**持久连接判定**：[src/HttpResponseCommand.cc:160-173](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L160-L173)。把"对端是否支持持久连接"写回 Request 对象（依据响应里的 `Connection` 头与 HTTP 版本），并按 `--max-http-pipelining` 设置管道深度。这个判定决定了 4.4 里连接会不会被回池。

**304 快速完成**：[src/HttpResponseCommand.cc:180-206](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L180-L206)。`--conditional-get` 场景下服务器说"没变过"，于是初始化 PieceStorage、`markAllPiecesDone()`，打印"Download has already completed"，回收连接，任务直接完成——完全不需要数据泵上场。

**3xx 及以上**：[src/HttpResponseCommand.cc:241-246](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L241-L246) 调 `skipResponseBody`，其实现（[L488-L514](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L488-L514)）创建 **HttpSkipResponseCommand** 把响应体读掉丢掉（重定向 Location 的跟随、404 计数都在那里面处理，本讲不展开）。

**首响应定型**：[src/HttpResponseCommand.cc:259-292](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L259-L292)。`!getPieceStorage()` 为真意味着 PieceStorage 还没建——文件长度、文件名（`determineFilename` 综合 Content-Disposition 与 URI）、Content-Type 都在这里定型，然后 `preDownloadProcessing()`（处理 `--allow-overwrite`/自动改名）。接着二选一：

- **handleDefaultEncoding**（[L351-L399](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L351-L399)）：长度已知的正路。建控制文件对象、`adjustFilename`、`initPieceStorage`；`--dry-run` 到此为止；随后 `createCheckIntegrityEntry` 决定是否需要先校验/预分配（这两个是经过 SequentialPicker 串行的"停车场"阶段，见 u3-l1）；若响应从文件头开始且未开管道，就把 HttpDownloadCommand 作为 `pushNextCommand` 压进校验条目的后续队列——**数据泵在校验/预分配完成后才真正上场**；否则取消 segment、归还 Request 重新来过。
- **handleOtherEncoding**（[L401-L486](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L401-L486)）：长度未知（或需要解压）的流式路径。从零偏移顺序写，直接创建 HttpDownloadCommand。零长度文件的若干特判也在这里。

**创建数据泵的统一入口**：[src/HttpResponseCommand.cc:536-559](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L536-L559) 的 `createHttpDownloadCommand`。除了构造 HttpDownloadCommand，它还做四件事：设置启动闲置时间（`--startup-idle-time`）与最低速度限制（`--lowest-speed-limit`）；若启用了文件预分配但过滤器里有 gzip 解压（解压后大小未知）则关闭预分配；装上 StreamFilter 链（Transfer-Encoding 在外层、Content-Encoding 在内层，组装逻辑见 [L88-L126](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L88-L126)）；最后让 URISelector 对新命令做调优（feedback 策略记录 server stats）。

**连接回收**：[src/HttpResponseCommand.cc:561-567](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L561-L567) 的 `poolConnection`——只要对端支持持久连接就把 socket 还给引擎连接池，供 4.1 的快速路径复用。

#### 4.3.4 代码实践

**实践目标**：对照日志确认"首响应定型"动作——文件名与长度是何时确定的、状态码如何分流。

**操作步骤**：

1. 下载一个可指定文件名的资源：`./src/aria2c http://127.0.0.1:8000/big.bin -d /tmp/a2test -o named.bin --log=resp.log --log-level=info`
2. 打开日志检索 `Response received:`（模板见 [src/message.h:48](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L48)），阅读完整的响应头。
3. 再触发一次 404：`./src/aria2c http://127.0.0.1:8000/no-such-file -d /tmp/a2test --log=404.log --log-level=info`
4. 对比两份日志里 `Response received:` 之后的走向。

**需要观察的现象**：成功下载的日志里，`Response received:`（含 `200 OK` 与 `Content-Length`）之后应出现文件创建相关的通知行，随后进入数据搬运；404 的日志里 `Response received:` 显示 `404 File not found`，之后出现 `Download aborted`/重试相关行，而**没有**任何数据泵日志。

**预期结果**：404 会触发 `increaseAndValidateFileNotFoundCount`（[src/HttpResponseCommand.cc:242-244](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L242-L244)），多次 404 后整组放弃；日志细节**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`handleDefaultEncoding` 里为什么要 `getSegmentMan()->getSegmentWithIndex(getCuid(), 0)` 抢一个 0 号 segment，而不是让数据泵自己去领？

**答案**：源码注释写得很清楚：**持有 Request 对象的命令在 PieceStorage 初始化之后必须持有 segment**（这是 AbstractCommand::execute 的前置约定——它每轮都会用 segment 刷新状态）。另外注释还解释了一个协议层原因：如果开了管道，这个没有 Range 的响应会把整个实体都发过来，无法用于分段下载，必须关掉连接重来——代码里对 `isPipeliningEnabled` 的判断就是在处理这个边界。

**练习 2**：`receiveResponse()` 可能返回 `nullptr` 的两种情况分别是什么？

**答案**：① 响应头还没到齐：HttpHeaderProcessor::parse 返回 false，命令自排等读事件；② 收到的是 1xx 临时响应（如 100 Continue）：[src/HttpConnection.cc:166-170](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpConnection.cc#L166-L170) 会重置处理器返回 nullptr，命令继续等最终响应。

**练习 3**：为什么 gzip 解压响应（`shouldInflateContentEncoding` 为真）必须走 `handleOtherEncoding` 而不能分段下载？

**答案**：响应头里的 Content-Length 是**压缩后**的字节数，而 aria2 需要按**解压后**的偏移来切 segment、写磁盘——两者的偏移空间对不上，分段下载的 Range 与写盘位置都会错位。所以只能放弃分段，从 0 偏移起整个流式下载（源码注释见 [src/HttpResponseCommand.cc:336-349](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L336-L349)），并且文件预分配也要因此关闭（解压后大小未知）。

### 4.4 HttpDownloadCommand 与 DownloadCommand：数据泵与收尾

#### 4.4.1 概念说明

最后一棒是**数据泵**：把响应体从 socket 搬到磁盘。它分为两层：

- **DownloadCommand（基类）**：与协议无关的"泵体"。每次 `executeInternal` 读一块数据、经过 StreamFilter 链（chunked 解码、gzip 解压，最底层是 SinkStreamFilter 负责真正写盘）、推进 segment 游标、判断本段是否完成。HTTP 与 FTP 的下载数据搬运共用这个基类。
- **HttpDownloadCommand（HTTP 外壳）**：只 override 了"段结束之后怎么办"（`prepareForNextSegment`）——HTTP 特有的事务：开管道时继续在同一连接上发下一个请求；支持 keep-alive 时把连接回池；检测服务器其实不支持管道的情况。

这一棒的循环形态与前几棒不同：它**自己排自己**（`addCommandSelf`）反复执行，直到段完成或出错，像一台缝纫机踏板循环。速度统计（PeerStat）、实时分块校验（piece hash）、最低速度限制也都嵌在这个循环里。

#### 4.4.2 核心流程

```text
HttpDownloadCommand::executeInternal()   [= DownloadCommand::executeInternal]
    全局/单任务限速超了？→ 摘掉读写事件订阅，自排（退化为每秒轮询），return false
    领取本命令的 segment（getSegments().front()）

    若接收缓冲空:
        recv() == 0 且不再可读可写 → eof = true     # 服务器关了连接
    若 !eof:
        bufSize = 本段还差多少 与 缓冲里有多少 的最小值（仅 SinkFilter 时）
        streamFilter->transform(diskAdaptor, segment, buffer, bufSize)
            # 过滤链逐层处理：chunked 解码 → gzip 解压 → SinkStreamFilter 写盘
        drain(bufSize)                      # 消费掉已处理字节
        peerStat->updateDownload(bufSize)   # 测速记账
        dctx->updateDownload(bufSize)       # 进度记账

    判定 segmentPartComplete:
        段写满 / 写到文件末尾 / (无长度且 eof) / 过滤链 finished
    未完成且 eof → 抛 DL_RETRY_EX(EX_GOT_EOF)     # 意外断流，走重试
    完成:
        需要 piece hash 校验？→ 校验（失败则清段重试）
        否则 completeSegment(cuid, segment)       # 通知 SegmentMan 记账
        checkLowestDownloadSpeed()                # 低于 --lowest-speed-limit 抛 DlAbortEx
        return prepareForNextSegment()            # 交棒或再战一段
    未完成:
        checkLowestDownloadSpeed()
        自排，return false                        # 下个读事件继续搬
```

`prepareForNextSegment`（HTTP 版在外壳，通用版在基类）的决策：

```text
HttpDownloadCommand::prepareForNextSegment():
    任务整体未完成且开了管道？→ 创建新 HttpRequestCommand 继续发请求（同连接）
    对端支持 keep-alive 且响应数据处理干净？→ poolSocket 把连接还回连接池
    怀疑服务器不支持管道（Range 终点对不上）？→ prepareForRetry(0) 重来
    其余 → DownloadCommand::prepareForNextSegment():
        任务完成？→ 归还 Request、回填总长度、（有整文件校验和时）压入校验条目、
                   setNoWait 让引擎立刻发现任务完成，return true 退场
        只有一条 segment？→ 尝试领 index+1 的下一段继续用本连接下载（写了一半的不能要）
        领不到 / 段不干净？→ prepareForRetry(0)：取消段、归还 Request、CreateRequestCommand
```

#### 4.4.3 源码精读

**泵的初始化**：[src/DownloadCommand.cc:76-106](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L76-L106)。构造时若开了 `--realtime-chunk-checksum` 且算法可用，就预建 MessageDigest 准备边下边校验 piece hash；创建 PeerStat 并登记进 SegmentMan（u3-l5 讲过它如何汇入测速）；最关键的是 [L101-L104](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L101-L104)：过滤器链的**最底层**在这里诞生——`SinkStreamFilter`（负责把字节写进 DiskAdaptor/写盘缓存的那个）。之后 HttpResponseCommand 装上的 chunked/gzip 过滤器会通过 `installDelegate` **套在它外面**（[L410-L420](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L410-L420)，`sinkFilterOnly_` 标记随之更新——它决定后续判段逻辑走哪个分支）。

**限速闸门**：[src/DownloadCommand.cc:135-144](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L135-L144)。超限时**摘掉** EventPoll 的读写注册（`disableReadCheckSocket`）再自排——命令退化为每秒被刷新闸门唤醒一次的轮询者，数据滞留在内核缓冲里靠 TCP 反压服务器。这是 u3-l5 所讲「限速靠暂停事件订阅」的具体执行点。

**读—滤—写一循环**：[src/DownloadCommand.cc:150-198](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L150-L198)。缓冲空才 recv（[L150-L162](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L150-L162) 的注释解释了为何不能抢读：pipelining 时缓冲里可能已躺着下一个响应的头）；`transform` 是过滤器链的统一入口（仅 SinkFilter 时按"本段还差多少"截取 bufSize，避免把别的段数据写进去）；随后 drain + 双份记账（连接级 PeerStat、任务级 DownloadContext）。

**判段与 EOF**：[src/DownloadCommand.cc:200-235](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L200-L235)。`segmentPartComplete` 的判定分两套（纯 SinkFilter 链看游标是否写满；有解码过滤器时还要等 `streamFilter_->finished()`——chunked 编码的结尾标记 `0\r\n\r\n` 不算实体数据，但必须读完）。**段没完成却收到 EOF 是异常**，抛 `DL_RETRY_EX(EX_GOT_EOF)` 走重试路径（4.5）。

**段完成的收尾**：[src/DownloadCommand.cc:237-287](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L237-L287)。开实时校验时先验 piece hash（[L383-L401](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L383-L401) 的 `validatePieceHash`：对则 `completeSegment`，错则清段重下）；否则直接 `completeSegment`（[L403-L408](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L403-L408) 会先 flush 写盘缓存再通知 SegmentMan）。随后 `checkLowestDownloadSpeed`（低于 `--lowest-speed-limit` 且过了启动期就抛 DlAbortEx 放弃该 URI），最后交棒 `prepareForNextSegment`。

**基类的下一段决策**：[src/DownloadCommand.cc:317-381](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L317-L381)。任务完成分支里值得注意的是 [L330-L338](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L330-L338)：没有分块哈希但设置了整文件校验和时，压一个 `ChecksumCheckIntegrityEntry` 进校验队列；以及 [L341-L342](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L341-L342) 的 `setNoWait(true)`——让 DownloadEngine 不等 1 秒刷新、立刻发现任务完成。未完成分支尝试**无缝续段**：领 index+1 的下一段、若干净（`getWrittenLength() == 0`）就 `addCommandSelf` 继续用这条连接下载（省一次往返！）；领不到或段不干净就 `prepareForRetry(0)` 重新建连。

**HTTP 外壳的收尾**：[src/HttpDownloadCommand.cc:74-128](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpDownloadCommand.cc#L74-L128)。三件事按序判定：管道续棒（[L77-L88](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpDownloadCommand.cc#L77-L88)，同 cuid 新建 HttpRequestCommand，注意请求早已在 4.2 的 pipelining 分支里发出去过，这里是响应处理完成后的接续）；连接回池（[L90-L107](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpDownloadCommand.cc#L90-L107)，条件是过滤器链已收尾或写到了请求的终点偏移——`getRequestEndOffset` 从响应的 Range 头取回，见 [L130-L137](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpDownloadCommand.cc#L130-L137)）；管道怀疑检查（[L114-L125](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpDownloadCommand.cc#L114-L125)：请求时假设支持管道，但响应 Range 终点只盖住了当前段——说明服务器没按管道语义办事，得重新协商）。最后落到基类决策。

#### 4.4.4 代码实践

**实践目标**：观察数据泵的"段完成"节奏，验证无缝续段行为。

**操作步骤**：

1. 准备一个大于 4MB 的文件（保证默认 1M 的 piece 下有多个 piece），用**单连接**下载：`./src/aria2c http://127.0.0.1:8000/big.bin -d /tmp/a2test -o pump.bin --log=pump.log --log-level=info`
2. 在日志中检索 `The download for one segment completed successfully`（模板见 [src/message.h:42-43](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L42-L43)）。
3. 对比该日志行前后的 `Requesting:` 出现次数。

**需要观察的现象**：如果 aria2 走了"无缝续段"路径（领 index+1 段继续下载），段完成日志行之间**不会**出现新的 `Requesting:` 行——同一条连接上不需要新请求；只有当段领取失败或任务结束需要重新建连时才会出现新的 `Connecting to`/`Requesting:`。

**预期结果**：单连接多段下载中，首个 `Requesting:` 之后可能直接连续出现多条段完成日志；具体节奏取决于 `--max-connection-per-server`、`--min-split-size` 与服务器行为，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`DownloadCommand` 为什么是 HTTP 和 FTP 共用的基类？共用部分与协议相关部分的边界在哪里？

**答案**：数据泵的本质（读 socket → 过滤 → 按 segment 写盘 → 判段完成 → 测速/限速/校验）与"字节从哪种协议来"无关，所以 `executeInternal` 的整个循环体、以及"任务完成后压入校验条目""无缝续段"等收尾都在基类。协议差异集中在两端：怎么拿到数据流（HTTP 由 HttpConnection 收响应体，FTP 由数据连接读）和段结束后的连接事务（`prepareForNextSegment` 的 HTTP 版处理管道与连接池，FTP 版则要回到控制连接协商下一个 RETR）。

**练习 2**：`sinkFilterOnly_` 为 false（装了 chunked/gzip 过滤器）时，判段为什么要额外要求 `streamFilter_->finished()`？

**答案**：chunked 编码在实体数据之后还有结束块（`0\r\n\r\n` 及可能的 trailer），gzip 流也有自己的收尾字节。这些"尾巴"不计入 Content-Length 也不属于任何 segment，若不读完就关闭/回池连接，残留字节会污染连接池里这条连接的后续使用（下一个响应会读到垃圾）。所以必须等过滤器链报告 finished 才算本响应真正读干净。

**练习 3**：`prepareForNextSegment` 里领下一段时，为什么 `nextSegment->getWrittenLength() > 0` 的段不能要、宁可重建连接？

**答案**：本连接上即将到来的数据是从下一段的**起点**开始的，而一个已被写过一部分的段意味着别的连接（或之前某次尝试）已经写了它的前半段——把新数据从头写会覆盖已有数据造成文件损坏（源码注释：writing incoming data at nextSegment->getWrittenLength() corrupts file）。干净段才能安全地从头接流。

### 4.5 失败出口：重试与镜像切换

#### 4.5.1 概念说明

前四模块都假设一帆风顺。现实中任何一棒都可能抛异常，而 aria2 对异常有**三级响应**，全部集中在 `AbstractCommand::execute()` 的 catch 块里：

| 异常类型 | 语义 | 处置 | 典型来源 |
| --- | --- | --- | --- |
| `DlRetryEx` | 这条路暂时走不通，值得再试 | `prepareForRetry`：还段、还 Request、重建 CreateRequestCommand | 超时、EOF 早到、连接失败、分块校验失败 |
| `DlAbortEx` | 这个 URI 没戏了，换下一个 | `tryReserved`：让 RequestGroup 领新 URI 建新命令 | 404、重定向过多、速度过慢 |
| `DownloadFailureException` | 整个任务没戏了 | `setHaltRequested(true)` 停掉整个 RequestGroup | 致命错误 |

与"换 URI"配合的两个背景机制：

- **多地址重试**：一个域名可能解析出多个 IP。连接失败时先 `markBadIPAddress` 换同域名的下一个地址（4.1 已见），地址耗尽才升级为换 URI。
- **镜像切换**：`tryReserved` 最终落到 `RequestGroup::createNextCommand`，由 URISelector（inorder/feedback/adaptive，见 u3-l1）从 FileEntry 的 URI 池里挑下一个镜像。失败经历还会沉淀进 ServerStat 影响后续选择。

#### 4.5.2 核心流程

```text
AbstractCommand::execute() 的 catch 分流:
    catch DlAbortEx:
        记错误码、addURIResult（URI 失败档案）
        onAbort()（关闭 socket 等）
        tryReserved() → requestGroup->createNextCommand(commands, e, 1)
                        # 领新 URI（可能换镜像）→ 新 CreateRequestCommand
        return true
    catch DlRetryEx:
        req->addTryCount(); 达到 --max-tries → 降级为上面的 abort 路径
        503 → 给 Request 设置 --retry-wait 的唤醒时间
        prepareForRetry(0)
    catch DownloadFailureException:
        setHaltRequested(true) → 整组停机

prepareForRetry(wait):
    cancelSegment(cuid)          # 归还段（SegmentMan 记账回收）
    req 重置持久连接标记；poolRequest 归还 Request
    创建 CreateRequestCommand（wait>0 时置 INACTIVE 睡到唤醒时间）

连接失败的两个专门处理点:
    Initiate 阶段:  markBadIPAddress → 有好地址就重建自己（4.1）
    Connect 阶段:   checkIfConnectionEstablished → 同样 markBadIPAddress；
                    地址耗尽 → ServerStat.setError（给这个服务器记过）
                    + 抛 DL_RETRY_EX 进入上面的重试体系
```

#### 4.5.3 源码精读

**catch 分流**：[src/AbstractCommand.cc:346-363](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L346-L363)（DlAbortEx：记录 `addURIResult`——这个 URI 的失败档案会在最终报告里显示；`onAbort` 收尾；`tryReserved` 换 URI）；[src/AbstractCommand.cc:364-397](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L364-L397)（DlRetryEx：`addTryCount` 达到 `--max-tries` 就降级为 abort；`HTTP_SERVICE_UNAVAILABLE` 时按 `--retry-wait` 设置唤醒时间——配合 4.1 里 CreateRequestCommand 的"睡觉检查"）；[src/AbstractCommand.cc:398-413](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L398-L413)（DownloadFailureException：整组停机）。

**tryReserved**：[src/AbstractCommand.cc:415-436](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L415-L436)。注意开头的保护：单文件任务且长度未知且 URI 已耗尽时**不再**创建新命令——避免无限循环（长度未知意味着可能有在途请求，URI 空了就没必要再试）。否则调 `requestGroup_->createNextCommand(commands, e_, 1)` 只要一条命令。

**prepareForRetry**：[src/AbstractCommand.cc:438-469](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L438-L469)。四步：取消段（有 PieceStorage 时）；重置 Request 的持久连接/管道标记并 `poolRequest` 归还；`recognizeSegmentFor` 让 SegmentMan 重新认识该文件的段布局；创建 **CreateRequestCommand**——注意重试不是回到本命令，而是回到链条**第 0 棒**重新走一遍（可能这次会换地址、换连接、甚至换 URI）。

**连接确认失败的处理**：[src/AbstractCommand.cc:835-866](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L835-L866)（`checkIfConnectionEstablished`，由 4.1 的 ConnectCommand 调用）。`getSocketError()` 拿到 `SO_ERROR`：无错即连接成功；有错则 `markBadIPAddress`，还有缓存地址就重建 InitiateConnectionCommand 换地址（返回 false 让 ConnectCommand 退场）；地址耗尽时**给服务器记账**（`ServerStat::setError`，feedback/adaptive URISelector 之后会避开它）并抛 `DL_RETRY_EX` 升级进重试体系。

**数量控制的另一端**：换 URI 建新命令时，一次该建几条？[src/RequestGroup.cc:846-883](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L846-L883)：长度未知时只允许 1 条流命令；长度已知时新命令数 = min(piece 数, `-s` 并发上限 − 已有流命令数)。这是 u3-l1 结论在命令层的落点。

#### 4.5.4 代码实践

**实践目标**：亲手触发三种失败，观察 aria2 的三级响应在日志里的样子。

**操作步骤**：

1. **DNS 失败**：`./src/aria2c http://no-such-host-xyz.invalid/f.bin -d /tmp/a2test --log=dnsfail.log --log-level=info`
2. **连接拒绝**：`./src/aria2c http://127.0.0.1:9/f.bin -d /tmp/a2test --log=connfail.log --log-level=info`（端口 9 通常无人监听）
3. **限制重试次数**：在上面命令后加 `--max-tries=2 --retry-wait=1`。
4. 阅读三份日志，检索 `Restarting the download`（[src/message.h:50](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L50)）、`Download aborted`（[L49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L49)）与 `times attempted`（[L52-L53](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L52-L53)）。

**需要观察的现象**：连接拒绝的日志里应能看到 `Connecting to 127.0.0.1:9` 之后紧跟失败信息与 `Restarting the download`；同一 cuid（或新 cuid）反复出现建连尝试；加 `--max-tries=2` 后尝试两次即出现 `2 times attempted, but no success. Download aborted.`，进程以非零退出码结束。

**预期结果**：默认 `--max-tries=5`。DNS 失败通常直接走 abort 路径（没有地址可换）；连接拒绝先换地址（本例只有一个回环地址，无可换）再计数重试。具体日志措辞与退出码（可用 `echo $?` 查看，对应 `src/error_code.h` 的枚举）**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `prepareForRetry` 创建的是 CreateRequestCommand（第 0 棒）而不是直接重建出错的那个命令？

**答案**：两个原因。① 状态可能已时过境迁：出错时持有的 segment、连接、Request 标记（管道深度、持久连接支持）都可能失效，从头走一遍第 0 棒可以重新做全部决策（换地址、换 URI、重新领段）；② 重试期间 URISelector 可能换镜像、RequestGroup 的并发预算可能变化，这些决策属于第 0 棒的职责。这也让每棒命令保持"无内部重试循环"的简单性。

**练习 2**：`tryReserved` 与 `prepareForRetry` 都会创建新命令，本质区别是什么？

**答案**：`tryReserved`（abort 路径）**先归还失败的 URI 再领新 URI**——失败 URI 被记入 `addURIResult` 档案，不再使用；`prepareForRetry`（retry 路径）把当前 Request **池化回** FileEntry（`poolRequest`），同一个 URI 之后还可能被再次领出使用（比如服务器临时抽风）。一个是"换人"，一个是"再给同一个人一次机会"（受 `--max-tries` 计数限制）。

**练习 3**：连接失败时 aria2 依次尝试的"换"的粒度从细到粗是什么？

**答案**：换 IP 地址（同域名多地址，`markBadIPAddress`，4.1 的 Initiate 阶段与 4.5 的 Connect 阶段两处）→ 换 URI/镜像（`tryReserved`，URISelector 依 ServerStat 挑选，服务器失败会 `setError` 降权）→ 放弃整组（`DownloadFailureException` → `setHaltRequested`）。粒度从细到粗，尽量以最小代价恢复下载。

## 5. 综合实践

把本讲所有内容串成一张**接力图**。这是本讲的主实践，也是检验你是否真读懂命令链的试金石。

**任务**：下载一个 HTTP 文件，从日志中还原出完整的命令接力过程，画出接力图并标注每棒的交棒物。

**步骤**：

1. 准备实验环境：本地某目录放一个约 10MB 的文件，`python3 -m http.server 8000` 起服务（本地服务可控、可重复、不占用外网带宽）。
2. 单连接基线：

   ```bash
   ./src/aria2c http://127.0.0.1:8000/big.bin \
     -d /tmp/a2test -o base.bin \
     --log=base.log --log-level=info
   ```

3. 多连接对照（重复第 2 步，改 `-o split.bin` 并加 `-x2 -s4 -k1M`，日志存 `split.log`）。
4. 在 `base.log` 中按时间顺序检索并摘录以下日志行各至少一条（均为 INFO 级，模板见 [src/message.h:42-53](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L42-L53)）：
   - `Connecting to ...`（4.1 HttpInitiateConnectionCommand）
   - `Requesting:`（4.2 HttpRequestCommand）
   - `Response received:`（4.3 HttpResponseCommand）
   - `The download for one segment completed successfully`（4.4 DownloadCommand）
5. 以 cuid 为主键把这些行串成时间线，画出类似第 4 节开头的接力图；在每条边上标注交棒物（Socket / Request / Segment / HttpConnection 中哪些被传递）。
6. 在 `split.log` 中重复第 5 步，此时应有**两个以上 cuid**，观察：两个 cuid 的 `Requesting:` 里 Range 是否互不重叠；段完成行的归属分布；是否出现连接池复用（第二条连接无 `Connecting to`）。
7. 挑战题：给本地服务器换成只支持 HTTP/1.0 的实现（或加 `--enable-http-pipelining` 对比开关前后），观察管道相关行为差异。

**验收标准**：你能指着接力图上每一棒说出——它对应哪个源码文件的哪个函数、它消费了什么资源、它交出了什么资源、它在什么条件下会抛出哪类异常走 4.5 的哪条出口。

**注意**：本实践的所有观察结论依赖具体服务器行为与 aria2 编译选项，日志样例**待本地验证**。

## 6. 本讲小结

- 一次 HTTP 下载是一场命令接力：`CreateRequestCommand`（领 URI）→ `HttpInitiateConnectionCommand`（DNS、连接池判定、发起连接）→ `ConnectCommand`（确认非阻塞连接完成）→ `HttpRequestCommand`（TLS 握手 + 发请求）→ `HttpResponseCommand`（响应头裁判）→ `HttpDownloadCommand/DownloadCommand`（数据泵自循环），cuid 全程不变，是日志追踪的钥匙。
- 接力棒是一组共享资源：Socket（连接）、Request（URI 及其状态）、Segment（写入游标）、HttpConnection（会话缓冲与在途请求队列），全部经**构造函数参数**传递；连接池命中时可跳过中间两棒直接进入发请求。
- HttpResponseCommand 是判定最重的一棒：304 直接判完成、3xx+ 交给 HttpSkipResponseCommand、首响应负责定型文件名/长度并决定走定长分段（可能先经校验/预分配停车场）还是不定长流式路径，最后统一经 createHttpDownloadCommand 装上 StreamFilter 链创建数据泵。
- 数据泵 DownloadCommand 与协议无关：限速靠摘事件订阅实现、判段要区分纯 SinkFilter 与带解码过滤器两种口径、段完成后 HTTP 外壳决定管道续棒/连接回池/重新协商，基类则尝试无缝续段（领下一段免重建连接）。
- 失败有三级出口：DlRetryEx → prepareForRetry 回到第 0 棒重试同一 URI（受 --max-tries 限制）；DlAbortEx → tryReserved 换 URI/镜像（ServerStat 记账降权）；DownloadFailureException → 整组停机。连接失败先在同域名的多个 IP 间轮换，粒度从细到粗。

## 7. 下一步学习建议

沿着本讲打开的几扇门继续：

1. **u4-l2 HTTP 协议实现**：本讲把 `HttpConnection::sendRequest/receiveResponse` 当黑盒用了，下一讲拆开它——HttpRequest 如何求值出 Range/鉴权/压缩头、HttpHeaderProcessor 的流式解析状态机、HttpHeader 的存取。
2. **u4-l3 认证与 Cookie**：本讲 4.2 里 `createHttpRequest` 灌进去的 CookieStorage、AuthConfigFactory 的具体来源与优先级。
3. **u4-l4 代理与隧道**：本讲 4.1 刻意略过的代理两分支（GET 改写与 CONNECT 隧道）及 HttpProxyRequestConnectChain/FtpTunnel 命令链。
4. **u4-l6 流式过滤**：本讲 4.4 中 `transform` 背后的 StreamFilter 链（chunked 状态机、gzip 解压）的完整实现。
5. 若想先横向对比，可直接跳到 **u4-l5 FTP 与 SFTP**：观察 FtpNegotiationCommand 如何用完全相同的 AbstractCommand 骨架实现另一套协商语义，加深对本讲"模板方法 + 命令接力"模式的理解。
