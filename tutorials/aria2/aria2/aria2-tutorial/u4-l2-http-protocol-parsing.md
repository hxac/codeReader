# HTTP 协议实现：HttpConnection 与报文解析

## 1. 本讲目标

上一讲（u4-l1）我们走通了 HTTP 下载的「命令接力链」，知道每棒命令推进一小步。本讲下潜一层，拆开接力棒本身，聚焦三个问题：

1. **HttpConnection** 如何在一条 TCP 连接上组织请求的发送与响应的接收，并支撑 HTTP pipelining（在一条连接上连发多个请求、按序收响应）。
2. **HttpHeaderProcessor** 如何用一个流式状态机，把可能分几次到达的响应头部增量地解析出来；**HttpHeader** 又如何只挑「感兴趣的头部」存进一张可重复键的表。
3. **HttpRequest** 求值（`createRequest()`）如何把 Segment、鉴权、Cookie、压缩偏好等状态「翻译」成一行行 HTTP 请求报文文本。

学完本讲，你应该能：读懂 `-l --log-level=info` 日志里的 `CUID#x - Requesting:` 与 `CUID#x - Response received:` 两段文本各自来自哪里；说清楚 aria2 对 `Location`、`Content-Encoding`、`Transfer-Encoding`、`Content-Length`、`Connection` 等关键响应头的处理位置；并解释 persistent connection（keep-alive）的复用条件。

## 2. 前置知识

- **HTTP 报文结构**：HTTP/1.1 报文 = 起始行（请求行 `GET /path HTTP/1.1` 或状态行 `HTTP/1.1 200 OK`）+ 若干头部字段（`Name: Value` 每行一个）+ 一个空行（`\r\n`）+ 可选的实体 body。头部与 body 之间**只隔一个空行**，这是解析器判断「头结束了、后面全是 body」的唯一依据。
- **为什么必须「流式/增量」解析**：TCP 是字节流，一次 `recv()` 可能只拿到半个头部，也可能拿到「完整头部 + 一段 body」。解析器必须能：今天喂 100 字节记住进度，明天再喂 200 字节接着解析，并且精确报告「本次消耗了几个字节」（剩下的字节属于 body，不能动）。这正是 u2-l4 讲过的 `SocketRecvBuffer`（16KB 共享读缓冲）的搭档。
- **状态机**：把「正在解析什么」编码为一个整数状态（当前读到方法名？状态码？字段名？字段值？），每读一个字节根据当前状态决定动作和状态迁移。它不回退、不重扫，天然适合增量输入。
- **`std::multimap<int, std::string>`**：允许同一个键出现多次的有序映射。HTTP 里 `Set-Cookie`、`Link`、`Digest` 等字段可以重复出现多次，用 multimap 才能一条不丢。
- **承接 u4-l1**：请先记住命令链 `CreateRequest → HttpInitiate → (Connect) → HttpRequest → HttpResponse → HttpDownload`。`HttpRequestCommand` 调 `HttpConnection::sendRequest`，`HttpResponseCommand` 调 `HttpConnection::receiveResponse`——本讲就是这两行的展开。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/HttpConnection.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpConnection.cc) | 一条连接上的 HTTP 会话对象：发送请求文本、按序接收响应头部、维护在途请求队列 |
| [src/HttpHeaderProcessor.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeaderProcessor.cc) | 流式状态机解析器，CLIENT/SERVER 两种模式，增量解析起始行 + 头部字段 |
| [src/HttpHeader.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeader.cc) | 解析结果的容器：multimap 存「感兴趣的头部」、`Content-Range` 解析、keep-alive 判定 |
| [src/HttpRequest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc) | 请求报文「求值器」：`createRequest()` 拼出完整请求文本（Range/鉴权/Cookie/压缩等） |
| [src/Request.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Request.cc) | 单个 URI 的运行时状态：原始 URI vs 当前 URI、重定向计数、keep-alive/pipelining 开关 |
| [src/HttpResponse.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc) | 响应头的「消费端」：状态码校验、重定向、TE/CE 过滤器选择（本讲第 4 模块引用） |
| test/HttpHeaderProcessorTest.cc、test/HttpRequestTest.cc、test/HttpHeaderTest.cc | 对应单元测试，是理解边界行为的最佳实验材料 |

## 4. 核心概念与源码讲解

### 4.1 HttpConnection：一条连接上的请求-响应会话

#### 4.1.1 概念说明

`HttpConnection` 是「一条已建立连接 + 它上面的 HTTP 会话」的封装。它解决三个问题：

1. **发送**：把 `HttpRequest::createRequest()` 生成的文本写进 socket（经 `SocketBuffer` 做部分写缓冲，见 u2-l4）。
2. **接收**：从 `SocketRecvBuffer` 增量解析响应头部，解析完一个就产出一个 `HttpResponse` 对象。
3. **配对（pipelining 的根基）**：HTTP pipelining 允许在收到响应前连发多个请求。`HttpConnection` 用一个 deque `outstandingHttpRequests_`（在途请求队列）按 FIFO 顺序把「第 i 个请求」与「第 i 个响应」配对——HTTP/1.1 规定响应必须按请求顺序返回，所以不需要额外的匹配协议。

#### 4.1.2 核心流程

```
sendRequest(httpRequest):
  文本 = httpRequest->createRequest()      // 求值，见 4.3
  日志打印（抹掉机密头）
  socketBuffer.pushStr(文本); send()       // 可能只发出去一部分，剩余留缓冲
  在途队列.push_back(新 HttpRequestEntry)  // entry = 请求 + 专属解析器

receiveResponse():                         // 每个 ResponseCommand 周期调一次
  在途队列空 -> 抛异常（说明状态机乱了）
  缓冲空 -> recv()；读到 0 字节且无未完成操作 -> 抛 EOF 异常（重试出口）
  proc = 队列.front() 的解析器
  ok = proc.parse(整个接收缓冲)            // 增量喂入
  若未完成: drain(proc.lastBytesProcessed); 返回 nullptr（下轮再来）
  若完成:
    状态码是 1xx -> drain + 重置该 entry 的解析器 + 返回 nullptr（跳过临时响应）
    否则 -> 组装 HttpResponse{头部, 对应的 HttpRequest}, drain, 队列.pop_front()
```

「drain」即把已被解析器消费的字节从接收缓冲里清掉，未被消费的字节（响应 body 的开头）留在缓冲里，交给后续的 `HttpDownloadCommand` 直接读取——零拷贝式的衔接。

#### 4.1.3 源码精读

每个在途请求自带一个专属解析器——[HttpConnection.cc:64-85](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpConnection.cc#L64-L85)：`HttpRequestEntry` 的构造函数创建 `HttpHeaderProcessor::CLIENT_PARSER` 模式的解析器；`resetHttpHeaderProcessor()` 用于收到 1xx 临时响应后重新开始解析真正响应的头部；`popHttpRequest()` 在响应完成时把请求对象的所有权移交给 `HttpResponse`（后续校验 Range、取 Cookie 存储都要用它）。

发送侧，[HttpConnection.cc:125-146](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpConnection.cc#L125-L146)：`sendRequest` 先以 `MSG_SENDING_REQUEST`（日志文本 `CUID#%d - Requesting:\n%s`，见 [src/message.h:47](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L47)）打印请求，再把文本压入 `socketBuffer_` 并 `send()`，最后把请求挂进在途队列。两个公开变体 `sendRequest()`/`sendProxyRequest()` 分别调用 `createRequest()` 与 `createProxyRequest()`（后者生成 CONNECT 隧道请求，见 4.3.3）。打印前会经过 [HttpConnection.cc:99-123](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpConnection.cc#L99-L123)：逐行检查，把 `Authorization:`、`Proxy-Authorization:`、`Cookie:`、`Set-Cookie:` 四类敏感头替换成 `<snip>`——所以日志里看不到你的密码和 Cookie，这是刻意的隐私设计。

接收侧核心在 [HttpConnection.cc:148-184](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpConnection.cc#L148-L184)，几个关键点：

- L153-158：缓冲为空时先 `recv()`；返回 0 且 socket 上没有挂起的读写操作即为对端关闭，抛 `EX_GOT_EOF` 可重试异常——这正对应 u4-l1 讲的 `DlRetryEx` 重试出口。
- L160-162：把**整个**接收缓冲喂给队首请求的解析器。解析器内部自己知道从哪个状态继续（增量解析），返回 `true` 表示「本次到达了头部末尾」。
- L163-164：完整收到头部后，同样先打印（`CUID#%d - Response received:\n%s`，[src/message.h:48](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L48)），敏感头照例打码。
- L166-170：状态码以 1 开头（100 Continue 等 1xx 临时响应）时，drain 掉它、重置解析器、返回 `nullptr`——上层命令只需继续等下一轮真正的响应。
- L172-179：正常响应组装 `HttpResponse`（cuid + 头部 + 对应请求），drain 掉已消费字节，队首出队。
- L182-183：头部没收全时也要 drain 掉已消费的部分并返回 `nullptr`，`HttpResponseCommand::executeInternal`（[src/HttpResponseCommand.cc:144-154](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L144-L154)）收到 `nullptr` 就把自己重新排队等下一个读事件。

辅助接口 [HttpConnection.cc:186-201](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpConnection.cc#L186-L201)：`isIssued()` 遍历在途队列查某 segment 是否已有未完请求（避免同一数据段重复发出）；`sendBufferIsEmpty()`/`sendPendingData()` 暴露发送缓冲状态，`HttpRequestCommand::executeInternal`（[src/HttpRequestCommand.cc:126](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequestCommand.cc#L126)）先确认上一棒的数据发干净了才发起下一棒。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `HttpConnection` 打印的请求与响应原文，验证「敏感头打码」与「分次到达」。

1. 操作步骤：
   - 编译好 aria2（见 u1-l2），执行：
     `./src/aria2c -l http.log --log-level=info -d /tmp/u4l2 http://example.com/`
   - 打开 `http.log`，搜索 `Requesting:` 与 `Response received:`。
2. 需要观察的现象：
   - `Requesting:` 块里有哪些头部？有没有 `Authorization:`（没有，因为 example.com 不需要鉴权；若你带 `--http-user=x --http-passwd=y` 再试，会看到 `Authorization: <snip>`）。
   - `Response received:` 块的原始状态行与头部；`Transfer-Encoding: chunked` 是否出现。
3. 预期结果：两段日志都能找到，且与你用 `curl -v http://example.com/` 看到的报文一致。若日志级别不够（默认 notice），`Requesting:` 不会出现——它走的是 `A2_LOG_INFO`。
4. 本实践只读日志、不改码，可直接执行；不同站点的头部差异属正常现象。

#### 4.1.5 小练习与答案

1. **练习**：为什么 `outstandingHttpRequests_` 是 deque 且只在 `front()` 上配对、`pop_front()`？
   **答案**：HTTP/1.1 pipelining 保证响应按请求发出顺序返回；FIFO 队列的队首永远是「最早发出、尚未收到响应」的请求，队首的解析器就该解析下一个到达的响应，天然配对，无需 ID 匹配。
2. **练习**：`receiveResponse()` 中，若解析器返回 `false`（头部没收全），为什么还要执行 `drain(proc->getLastBytesProcessed())`？
   **答案**：状态机可能已消费了缓冲中的全部字节但仍未到头部末尾（比如刚收齐 300 字节还没有空行）；把已消费字节清掉、把 `lastBytesProcessed` 之后的字节（若有）留在缓冲，避免下一轮重复解析同一段文本。已消费的字节被累计进了 `headers_`（见 4.2.3）。
3. **练习**：收到 `HTTP/1.1 100 Continue` 后 aria2 会把它当成最终响应吗？
   **答案**：不会。[HttpConnection.cc:166-170](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpConnection.cc#L166-L170) 判断 `getStatusCode() / 100 == 1` 即重置解析器并返回 `nullptr`，继续等真正的响应。

### 4.2 HttpHeader 与 HttpHeaderProcessor：流式状态机解析

#### 4.2.1 概念说明

这一对类是「解析器 + 结果容器」的关系：

- **HttpHeaderProcessor**：纯语法层。逐字节推进状态机，识别起始行（客户端模式解析状态行，服务端模式解析请求行）与 `name: value` 字段行，直到空行。**同一份代码被 HTTP 客户端（下载）和内嵌 HTTP 服务器（RPC，u8-l3）共用**，靠 `CLIENT_PARSER` / `SERVER_PARSER` 两种模式区分起始行语法——这就是它的「可重用设计」：协议语法只有一份，两端都能用。
- **HttpHeader**：语义容器。只保存「感兴趣的头部」——一张白名单（`InterestingHeader` 枚举）之外的头部**不入表**，只随 `headers_` 原文保留用于日志。白名单机制让后续代码用整数键（而非字符串比较）快速取头，也控制了内存。

#### 4.2.2 核心流程

状态全集见 [HttpHeaderProcessor.cc:50-75](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeaderProcessor.cc#L50-L75)，可分四段：

```
服务端模式: PREV_METHOD→METHOD→PREV_PATH→PATH→PREV_REQ_VERSION→REQ_VERSION
客户端模式: PREV_RES_VERSION→RES_VERSION→PREV_STATUS_CODE→STATUS_CODE
                              →PREV_REASON_PHRASE→REASON_PHRASE
字段区(两模式共用): PREV_EOL→PREV_FIELD_NAME→FIELD_NAME
                              →PREV_FIELD_VALUE→FIELD_VALUE (循环，直到空行)
结束: PREV_EOH→HEADERS_COMPLETE
```

客户端解析 `Content-Encoding: gzip\r\n` 一行的状态迁移示例：

```
PREV_FIELD_NAME --'C'--> FIELD_NAME          (扫描字段名直到 ':')
FIELD_NAME      --':'--> PREV_FIELD_VALUE    (名字转小写、查白名单得整数键)
PREV_FIELD_VALUE-'g'--> FIELD_VALUE          (白名单命中→getText 收值；未命中→ignoreText 跳过)
FIELD_VALUE     --'\r'--> PREV_EOL           (值收集完毕，暂存不写表)
PREV_EOL        --'\n'--> PREV_FIELD_NAME    (遇到下一字段名/空行时，上一个字段才 put 进表)
PREV_FIELD_NAME --'\n'--> HEADERS_COMPLETE   (空行 = 头部结束)
```

注意两点设计：字段值遇到 `\r\n` 时**不立即入表**，要等看到下一行的第一个字符——这是为了支持「obs-fold」折行头部（值以空白开头的续行，[HttpHeaderProcessor.cc:327-335](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeaderProcessor.cc#L327-L335) 里称为 Evil Multi-line header field）；以及 `STATUS_CODE` 状态强制三位数字并换算成整数（L262-291）。

#### 4.2.3 源码精读

构造与模式选择：[HttpHeaderProcessor.cc:77-84](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeaderProcessor.cc#L77-L84)——客户端模式从 `PREV_RES_VERSION`（等 `HTTP/1.1`）起步，服务端模式从 `PREV_METHOD`（等 `GET`）起步。

主循环骨架：[HttpHeaderProcessor.cc:140-146](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeaderProcessor.cc#L140-L146)——每字节一次 `switch(state_)`；辅助扫描函数 `getToken/getFieldNameToken/getText/ignoreText`（[HttpHeaderProcessor.cc:88-138](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeaderProcessor.cc#L88-L138)）在状态内部一口气吃掉「非分隔符」的连续字节并返回结束下标，让状态机既可逐字节也可按 token 推进。

字段区两个核心状态：[HttpHeaderProcessor.cc:327-376](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeaderProcessor.cc#L327-L376)。`PREV_FIELD_NAME` 承担「上一字段何时入表」的裁决（L337-344：只有白名单命中的字段才 `result_->put(lastFieldHdKey_, strip(buf_))`）；遇到 `\n` 直接 `HEADERS_COMPLETE`（L345-348，容忍只有 LF 的非规范服务器），遇到 `\r` 则进入 `PREV_EOH`。`FIELD_NAME` 遇 `:` 时把名字转小写并用 `idInterestingHeader()` 查白名单（L368-372）。字段值状态 [HttpHeaderProcessor.cc:378-419](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeaderProcessor.cc#L378-L419)：白名单外走 `ignoreText`（值不落内存，但 `headers_` 原文里仍有）；`\n` 回到 `PREV_FIELD_NAME` 开下一个字段。[HttpHeaderProcessor.cc:421-428](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeaderProcessor.cc#L421-L428) 的 `PREV_EOH` 要求 `\r` 后必须紧跟 `\n`，否则报缺失 LF。

收尾三件事在 [HttpHeaderProcessor.cc:435-463](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeaderProcessor.cc#L435-L463)：

1. L440-442 防护：字段名超 1024 字节或值缓冲超 8KB 抛「Too large HTTP header」（对应测试 `testBeyondLimit`）。
2. L444-445 把本次喂入的**已消费**字节追加进 `headers_`（日志用的原文）；`lastBytesProcessed_ = i` 告诉调用方 drain 多少。
3. L458-461 语义修正：若同时出现 `Transfer-Encoding` 与 `Content-Length`/`Content-Range`，**删掉后两者**（RFC 7230 规定 TE 优先，同时出现多为请求走私类异常）。

复位接口 [HttpHeaderProcessor.cc:477-486](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeaderProcessor.cc#L477-L486)：`clear()` 把一切归零并新建 `HttpHeader`，供连接复用/1xx 重置场景。

容器侧，[HttpHeader.cc:48-78](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeader.cc#L48-L78) 是 multimap 的薄封装：`put/defined/find/findAll/equalRange/remove`，`find` 找不到返回空串 `A2STR::NIL`（调用方用 `empty()` 判断，无异常路径）。白名单两处必须同步维护：枚举在 [HttpHeader.h:74-99](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeader.h#L74-L99)，按字典序排序的名单数组与二分查找在 [HttpHeader.cc:223-261](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeader.cc#L223-L261)（`std::lower_bound` 要求数组有序，新增头部时必须保持排序）。

两个重要的语义方法：

- `getRange()`：[HttpHeader.cc:87-165](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeader.cc#L87-L165) 把 `Content-Range`（形如 `bytes 100-199/200`）或仅有 `Content-Length` 的头翻译成 `Range(start, end, entityLength)`。它对多种残缺形态做了容忍：无 `bytes` 单位、`bytes=` 写法、`*` 通配（416 场景，返回空 Range）、缺 `Content-Range` 时退化为 `Range(0, clen-1, clen)`。数值非法则抛 `DL_ABORT_EX`（换镜像出口），超过 `a2_off_t` 上限抛不可恢复异常。断点续传一致性的判定公式为：响应区间与请求区间满足 \( s_{resp} = s_{req} \) 且（ \( e_{req} = 0 \) 或 \( e_{resp} = e_{req} \)）且（\( L_{req} = 0 \) 或 \( L_{resp} = L_{req} \)），对应 [src/HttpRequest.cc:121-130](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L121-L130) 的 `isRangeSatisfied`。
- `isKeepAlive()`：[HttpHeader.cc:215-220](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeader.cc#L215-L220)——复用条件是 `Connection` **不等于** `close` 且（版本是 `HTTP/1.1` 或 `Connection` 等于 `keep-alive`）。即：HTTP/1.1 默认长连接，除非对方明说 close；HTTP/1.0 默认断开，除非对方明说 keep-alive。`fieldContains()`（[HttpHeader.cc:195-213](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeader.cc#L195-L213)）还支持按逗号拆分值再匹配（供 RPC 的 CORS 头判断用）。

**重要响应头处理位置总表**（学完 4.1-4.4 后回看此表）：

| 响应头 | 解析后存于 | 消费位置与行为 |
| --- | --- | --- |
| `Content-Length` / `Content-Range` | `CONTENT_LENGTH`/`CONTENT_RANGE` | [HttpHeader.cc:87](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeader.cc#L87) `getRange()` 定文件长度与区间；与 TE 同现时被删除（[HttpHeaderProcessor.cc:458](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeaderProcessor.cc#L458)） |
| `Transfer-Encoding: chunked` | `TRANSFER_ENCODING` | [HttpResponse.cc:188-199](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L188-L199) 生成 ChunkedDecodingStreamFilter（u4-l6 详解） |
| `Content-Encoding: gzip/deflate` | `CONTENT_ENCODING` | [HttpResponse.cc:211-222](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L211-L222) 生成 GZip 过滤器；是否启用解压还要看请求侧开关（4.4.3） |
| `Location` | `LOCATION` | 3xx 时 [HttpResponse.cc:145-175](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L145-L175) 判定重定向并 `Request::redirectUri` |
| `Connection` | `CONNECTION` | [HttpHeader.cc:215-220](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeader.cc#L215-L220) 判定是否池化复用 |
| `Set-Cookie`（可重复） | `SET_COOKIE` | [HttpResponse.cc:134-143](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L134-L143) `retrieveCookie` 逐条入库（u4-l3 详解） |
| `Content-Disposition` | `CONTENT_DISPOSITION` | [HttpResponse.cc:115-132](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L115-L132) 提取建议文件名 |
| `Last-Modified` | `LAST_MODIFIED` | [HttpResponse.cc:271-274](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L271-L274) `--remote-time` 时回写文件时间 |
| `Link`（Metalink/HTTP，可重复） | `LINK` | [HttpResponse.cc:355-384](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L355-L384) 解析镜像列表（u8-l2 详解） |
| `Digest`（RFC 3230，可重复） | `DIGEST` | [HttpResponse.cc:388-434](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L388-L434) 解析校验和（u5-l5 详解） |

#### 4.2.4 代码实践

**实践目标**：用现成测试当「规格说明书」，验证状态机对病态头部的容忍度。

1. 操作步骤：打开 [test/HttpHeaderProcessorTest.cc:16-28](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/HttpHeaderProcessorTest.cc#L16-L28) 查看注册的用例，重点阅读 `testGetHttpResponseHeader_nameStartsWs`（字段名以空白开头的折行头部）与 `testGetHttpResponseHeader_teAndCl`（TE 与 CL 同现）两个用例的构造数据与断言。
2. 需要观察的现象：测试喂给 `parse()` 的原始字节串长什么样（例如把一个大头部切成两段分两次 `parse` 调用）。
3. 预期结果：你会发现测试正是以「分两次喂入」的方式驱动增量解析，并且断言 `Content-Length` 在 TE 存在时被移除、折行值被拼回同一字段。若本地已构建，可运行 `./src/aria2c --version` 确认环境后用 `make check` 或直接跑测试二进制验证（具体命令待本地验证，取决于 configure 是否启用了测试）。
4. 本实践为源码阅读型，不修改任何代码。

#### 4.2.5 小练习与答案

1. **练习**：为什么白名单外的头部值不走 `getText` 而走 `ignoreText`？丢掉这些值安全吗？
   **答案**：`getText` 把值追加进 `buf_` 占内存，`ignoreText` 只前进不拷贝。白名单外的头 aria2 语义上用不到，丢值不影响行为；且 L440 的 8KB 上限只约束单个白名单字段的值缓冲，忽略策略避免恶意服务器用巨量无用头撑爆内存。原文仍完整保留在 `headers_` 供日志与 RPC 显示。
2. **练习**：`HttpHeaderProcessor` 如何做到「同一份代码既解析响应又解析请求」？
   **答案**：构造参数 `ParserMode` 决定初始状态（[HttpHeaderProcessor.cc:77-84](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeaderProcessor.cc#L77-L84)）：CLIENT_PARSER 从状态行起步、SERVER_PARSER 从请求行起步；进入字段区后两种模式走完全相同的代码。RPC 内嵌服务器（u8-l3）就是用 SERVER_PARSER 复用它。
3. **练习**：若服务器返回 `Content-Range: bytes */12345`，`getRange()` 返回什么、上层会怎样？
   **答案**：[HttpHeader.cc:135-142](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeader.cc#L135-L142) 检测到 `*` 返回空 `Range()`（通常伴随 416），随后 `isRangeSatisfied` 校验失败抛 `CANNOT_RESUME` 可中止异常，触发换 URI/丢弃连接池（u4-l1 的 `DlAbortEx` 出口）。

### 4.3 HttpRequest 求值：从 segment 到请求报文

#### 4.3.1 概念说明

「求值」指 `createRequest()`：把下载状态（要哪个字节区间、谁在鉴权、有哪些 Cookie、是否接受压缩）在**发送那一刻**快照成 HTTP/1.1 请求文本。`HttpRequest` 本身不持有 socket，它是纯函数式的报文生成器 + 请求侧上下文（关联的 `Request`、`FileEntry`、`Segment`、`Option`、`CookieStorage`、`AuthConfigFactory`）。选择「发送时求值」而不是提前缓存文本，是因为重定向、连接复用、pipelining 都会改变上下文，报文必须每次现算。

#### 4.3.2 核心流程

```
createRequest():
  1. authConfig = 工厂按 (Request, Option) 现算鉴权   // 见 u4-l3
  2. 请求行: "GET " + (代理? 绝对URI : dir+file+query) + " HTTP/1.1\r\n"
  3. 组装内置头部列表 builtinHds（约 20 项预留）:
     User-Agent / Accept[+Metalink 类型] / Accept-Encoding(条件) /
     Host / Pragma+Cache-Control(条件) / Connection: close(条件) /
     Range(条件) / Connection: Keep-Alive(代理+条件) /
     Proxy-Authorization(条件) / Authorization(条件) /
     Referer(条件) / Cookie(条件) / If-Modified-Since(条件) / Want-Digest(条件)
  4. 逐项写入：若用户 --header 里已有同名字段则跳过内置项（用户优先）
  5. 追加用户自定义头 + 空行
```

#### 4.3.3 源码精读

**字节区间的确定**。[HttpRequest.cc:87-110](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L87-L110)：`getStartByte()` 把 segment 的写入位置经 `FileEntry::gtoloff`（piece 内偏移 → 文件全局偏移，见 u3-l4）换算成起始字节；`getEndByte()` 只有两种情况非零——开启 pipelining 时取 segment 末尾（这样每个流水线请求自带精确区间），或 `endOffsetOverride_` 被设置（连接池按「已请求过的区间」切下一片时使用，u4-l1 的连接复用依赖它）。普通非 pipelining 下载里请求只带 `Range: start-`（开区间到文件尾），由 `getEndByte()==0` 表达。

**Range 头的生成条件**：[HttpRequest.cc:201-215](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L201-L215)——仅当 segment 有长度且（pipelining 开启 或 起始/结束字节非零，即不是从 0 开始的整文件下载）才发 `Range`；结尾字节是否写死同样看 pipelining/override。注释点明 FTP-over-HTTP 代理不支持 end byte，恰好那两种情况都为假。

**鉴权与代理**：[HttpRequest.cc:221-229](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L221-L229) 代理请求带用户名时生成 `Proxy-Authorization: Basic base64(user:pass)`；`authConfig_` 非空（来源：URI 内嵌凭据、`--http-user/--http-passwd`、netrc，u4-l3 讲优先级）时生成 `Authorization: Basic ...`。代理场景下 FTP 目标还会把用户名注入 URI（[HttpRequest.cc:150-157](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L150-L157)）。CONNECT 隧道报文则是独立的 [HttpRequest.cc:289-316](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L289-L316)（`CONNECT host:port HTTP/1.1`），隧道建立后走普通请求链（u4-l4 详解）。

**压缩偏好**：[HttpRequest.cc:182-192](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L182-L192)——仅当编译带 zlib（`HAVE_ZLIB`）**且** `acceptGzip_` 为真才发 `Accept-Encoding: deflate, gzip`。而 `acceptGzip_` 来自选项 `--http-accept-gzip`（默认 false），接线处在 [HttpRequestCommand.cc:104-109](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequestCommand.cc#L104-L109)。同一段的 `Pragma/Cache-Control: no-cache` 由 `--http-no-cache`（默认 false）控制，`Host` 头在非 80/443 端口时附加 `:port`（[HttpRequest.cc:133-141,193](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L133-L141)）。

**Cookie**：[HttpRequest.cc:233-246](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L233-L246) 按「主机 + 路径 + 安全标志」从 `CookieStorage` 检索匹配 Cookie 拼成 `Cookie:` 头（域名匹配规则见 u4-l3）。

**Want-Digest 与用户头优先**：[HttpRequest.cc:250-267](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L250-L267) 按编译期哈希支持情况发 `Want-Digest`（请求服务端回 `Digest` 校验和）；[HttpRequest.cc:268-285](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L268-L285) 是合并规则：内置头逐项输出前先查 `--header` 给出的列表（大小写不敏感前缀匹配），用户给过的同名字段**抑制**内置版本，随后用户头原样追加——所以 `--header` 是彻底覆盖而非叠加。

**Request：URI 的运行时状态**。`HttpRequest` 的很多字段直接转发自 `Request`（[HttpRequest.cc:374-402](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L374-L402)）。`Request` 维护两个 URI：`uri_`（用户最初给的，用于测速归属与 URI 选择）与 `currentUri_`（重定向后的实际目标，[Request.cc:149-160](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Request.cc#L149-L160) 的 `parseUri` 用 `uri::parse` 填充 `UriStruct`，井号 fragment 会被剥掉）。重定向解析 [Request.cc:103-147](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Request.cc#L103-L147) 三分支：`//host/path` 网络路径引用补当前协议；绝对 URI 校验 scheme 合法字符后直接采用；相对路径经 `uri::joinUri` 与当前 URI 拼接——即使服务器违反 RFC 只给相对地址也能活。重定向计数 `redirectCount_` 上限 20（[Request.h:175](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Request.h#L175)）。keep-alive/pipelining 的三态标志 `supportsPersistentConnection_/keepAliveHint_/pipeliningHint_` 见 [Request.h:62-75](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Request.h#L62-L75)，其中「能力」（服务器是否支持）与「意愿」（本端是否启用，来自 `--enable-http-keep-alive/--enable-http-pipelining`，在 [InitiateConnectionCommandFactory.cc:63-68](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommandFactory.cc#L63-L68) 设置 hint）分离，`isKeepAliveEnabled()`（[Request.h:117-134](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Request.h#L117-L134)）要求两者同时为真。

#### 4.3.4 代码实践

**实践目标**：通过选项开关观察 `createRequest()` 输出的差异，验证「求值」随上下文变化。

1. 操作步骤（对比三组日志）：
   - `./src/aria2c -l a.log --log-level=info -d /tmp/u4l2 URL1`
   - `./src/aria2c -l b.log --log-level=info --http-accept-gzip=true -d /tmp/u4l2 URL1`
   - `./src/aria2c -l c.log --log-level=info --header="Accept: text/html" -d /tmp/u4l2 URL1`
2. 需要观察的现象：`Requesting:` 块中 `Accept-Encoding:` 是否出现/消失；第三组里内置 `Accept: */*` 是否被 `Accept: text/html` 取代。
3. 预期结果：默认无 `Accept-Encoding`；加 `--http-accept-gzip=true` 后出现 `Accept-Encoding: deflate, gzip`（需编译带 zlib，可用 `aria2c --version` 的 Features 是否含 `gzip` 判断）；用户头生效且同内置名不重复。完整对比结果待本地验证。
4. 若 URL 指向已下载一半的文件（目录里留有部分文件与控制文件），续传时还能观察到 `Range: <起始字节>-` 的出现。

#### 4.3.5 小练习与答案

1. **练习**：普通 `-x2 -s2` 下载中两个连接发出的 `Range` 有何不同？为什么结尾字节通常不写？
   **答案**：两个连接的 segment 起点不同（SegmentMan 分配，u3-l4），故 `Range: A-` 与 `Range: B-` 起点不同；结尾不写是因为非 pipelining 模式 `getEndByte()==0`（[HttpRequest.cc:96-110](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L96-L110)），开区间请求到文件尾，服务器以 `Content-Range` 告知实际区间，避免本端对文件总长度的假设写死在请求里。
2. **练习**：`Connection: close` 什么时候会被 aria2 主动写进请求？
   **答案**：[HttpRequest.cc:198-200](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequest.cc#L198-L200)：`--enable-http-keep-alive=false`（`keepAliveHint` 为假）且未开 pipelining 时。开 keep-alive 时不写，让 HTTP/1.1 默认语义生效；走代理且开 keep-alive 时反而写 `Connection: Keep-Alive`（L216-220，兼容旧代理）。
3. **练习**：重定向后 `Request::getUri()` 与 `getCurrentUri()` 返回值有何不同？为什么测速要绑定前者？
   **答案**：`getUri()` 恒为命令行/任务清单里的原始 URI，`getCurrentUri()` 是重定向后的。`initPeerStat`（[Request.cc:166-177](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Request.cc#L166-L177)）用原始 URI 的 host 建测速记录并注释了原因：URI 选择器（u3-l1）在原始 URI 集合上工作，ServerStat 也按原始 host 累积，若用重定向后的 host 归属会把 CDN 域名误当独立镜像。

### 4.4 响应头的消费与 persistent connection 复用

#### 4.4.1 概念说明

解析出 `HttpHeader` 只是第一步，「消费」发生在 `HttpResponse` 与 `HttpResponseCommand`：校验状态码与区间一致性、决定重定向去向、挑选 StreamFilter、决定连接是否回池复用。本模块把 4.2.3 的总表串成完整链路，并给出 keep-alive 复用的精确条件——这是本讲第三个学习目标。

#### 4.4.2 核心流程

```
HttpResponseCommand 拿到 HttpResponse 后:
  1. validateResponse()     // 状态码语义校验
  2. retrieveCookie()       // Set-Cookie 入库
  3. req->supportsPersistentConnection(头部的 isKeepAlive())   // 能力更新
  4. 304? -> 标记完成 + poolConnection
  5. 3xx? -> HttpSkipResponseCommand 跳过 body -> processResponse()
            -> isRedirect? -> redirectCount<20? -> Request::redirectUri
            -> prepareForRetry(0)（必要时先 poolConnection）
  6. 200/206: 首响应定型文件名/长度
     - 若需 gzip 解压 -> 走"长度未知"路径 handleOtherEncoding（禁分段、禁预分配）
     - 否则 -> handleDefaultEncoding（可分段）
     - 两者都会: getTransferEncodingStreamFilter([getContentEncodingStreamFilter])
       组装过滤链 -> 创建 HttpDownloadCommand
```

#### 4.4.3 源码精读

状态码校验：[HttpResponse.cc:71-113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L71-L113)。200/206 且无 `Transfer-Encoding` 时用 4.2.3 的公式核对响应区间与请求区间，不一致抛 `CANNOT_RESUME`（丢弃断点信息换镜像重来）；300/301/302/303/307/308 必须带 `Location` 否则按协议错误中止；304 必须确实发过条件请求。

重定向判定与执行：[HttpResponse.cc:145-175](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L145-L175)。真正的跳转发生在跳完响应体之后——[HttpSkipResponseCommand.cc:191-213](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpSkipResponseCommand.cc#L191-L213)：`processResponse()` 检查重定向计数（≥20 抛 `HTTP_TOO_MANY_REDIRECTS`），调 `processRedirect()`（内部就是 4.3.3 的 `Request::redirectUri`，并打出 `Redirecting to ...` 通知日志），再 `prepareForRetry(0)` 回到 CreateRequestCommand 换个 URI 重新走链。跳过 body 的过程中若服务器保持连接，[HttpSkipResponseCommand.cc:166-173](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpSkipResponseCommand.cc#L166-L173) 会先 `poolConnection()` 把 socket 存回引擎连接池，供重定向后的请求直接复用。

压缩响应的两难处理：[HttpResponseCommand.cc:336-349](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L336-L349) `shouldInflateContentEncoding()` 要求**请求侧 `acceptGZip()` 为真**（即 aria2 自己声明过接受 gzip）且编码为 gzip/deflate 才解压——服务器单方面塞 gzip 时 aria2 保存原始压缩字节。选择解压则进入 [HttpResponseCommand.cc:278-291](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L278-L291)：解压后长度未知（`Content-Length` 是压缩长度），所以强制走 `handleOtherEncoding` 流式路径、把文件长度清零（禁分段下载），并且 [HttpResponseCommand.cc:516-532](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L516-L532) `decideFileAllocation` 检测到 GZip 过滤器时关闭文件预分配（解压后大小未知，u5-l4）。过滤器实例化在 [HttpResponse.cc:188-222](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L188-L222)：TE 命中 `chunked` 生成 Chunked 过滤器、CE 命中 gzip/deflate 生成 GZip 过滤器，两者可叠加成链（外层 chunked、内层 gzip）。

**连接复用的完整条件链**（学习目标 3 的答案）：

1. 服务器侧能力：响应头满足 [HttpHeader.cc:215-220](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeader.cc#L215-L220)（无 `Connection: close` 且 HTTP/1.1 或显式 `keep-alive`），由 [HttpResponseCommand.cc:160-166](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L160-L166) 写回 `Request::supportsPersistentConnection`；
2. 本端意愿：`--enable-http-keep-alive`（默认开）设 `keepAliveHint`，见 [InitiateConnectionCommandFactory.cc:63-68](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InitiateConnectionCommandFactory.cc#L63-L68)；
3. 时机正确：响应体被**完整**消费后才调用 `poolConnection()`（[HttpResponseCommand.cc:561-567](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L561-L567)，内部再判一次能力标志），把 socket 交给 `DownloadEngine::poolSocket`，并连带记录已请求过的区间（配合 `endOffsetOverride_`，避免下个连接重复请求同一段）。

#### 4.4.4 代码实践

**实践目标**：观察「服务器单方面 gzip」与「协商 gzip」两种结果，验证 4.4.3 的判定逻辑。

1. 操作步骤：找任一返回 gzip 内容的服务端点，分别以
   `./src/aria2c -d /tmp/u4l2 -o raw.gz URL` 与 `./src/aria2c --http-accept-gzip=true -d /tmp/u4l2 -o inflated URL` 下载同一 URL。
2. 需要观察的现象：两个落盘文件的大小差异；日志中第二次是否没有总长度显示（流式路径）且不走分段。
3. 预期结果：第一个保存的是压缩字节（可用 `file` 命令识别为 gzip）；第二个是解压后的内容且更大。若服务器只在收到 `Accept-Encoding` 时才压缩，则两次结果相同——这本身也验证了请求侧开关的作用。具体站点行为待本地验证。

#### 4.4.5 小练习与答案

1. **练习**：为什么 aria2 不对「服务器擅自 gzip」的响应做解压？
   **答案**：`shouldInflateContentEncoding` 要求 `acceptGZip()` 为真（[HttpResponseCommand.cc:346-348](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L346-L348)）。客户端未声明接受就收到压缩体属于异常场景（也可能是 `.tgz` 等本来就是压缩格式的文件被误标），保守地按原始字节落盘，交由用户判断。
2. **练习**：`Transfer-Encoding: chunked` 且带 `Content-Length: 100` 的响应，aria2 认为体长多少？
   **答案**：不定长。解析收尾时 TE 已把 `Content-Length` 删除（[HttpHeaderProcessor.cc:451-461](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeaderProcessor.cc#L451-L461)），长度改由 Chunked 过滤器解包后自然得出，200/206 的区间校验也会因 TE 存在而跳过（[HttpResponse.cc:77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L77)）。
3. **练习**：连接池里的 socket 何时会被再次利用？复用时请求报文有何特别之处？
   **答案**：`HttpInitiateConnectionCommand`（u4-l1）按 (协议, 主机, 端口, 代理) 查池命中时直接跳过建连两棒；复用连接上第一个请求的 `Range` 起点来自池中记录的 `endOffsetOverride`（`getEndByte()` 非零分支），从上次请求过的区间之后接着要，避免与别的连接争抢同一段数据。

## 5. 综合实践

按讲义规格完成主实践：**自建一个返回 `Location` 跳转与 `Content-Encoding: gzip` 的小型 HTTP 服务，用 aria2 下载并开日志，对照源码说明 aria2 对这两个头分别做了什么。**

**步骤**：

1. 在临时目录（如 `/tmp/u4l2-server`）新建 `server.py`（**示例代码**，非 aria2 项目自带；注意 aria2 默认拒绝 `localhost/127.0.0.1` 以外无域名场景并无此限制，可直接用回环地址）：

   ```python
   # 示例代码：极简 HTTP 服务，两个端点：
   #   /redirect  -> 302 + Location: /data.gz（相对地址，验证 joinUri 分支）
   #   /data.gz   -> Content-Encoding: gzip，body 是 gzip 压缩后的文本
   import gzip, http.server, socketserver

   PAYLOAD = b"hello aria2 http protocol lecture\n" * 200
   GZ = gzip.compress(PAYLOAD)

   class H(http.server.BaseHTTPRequestHandler):
       protocol_version = "HTTP/1.1"      # 提供 keep-alive 语义
       def do_GET(self):
           if self.path == "/redirect":
               self.send_response(302)
               self.send_header("Location", "/data.gz")
               self.send_header("Content-Length", "0")
               self.end_headers()
           elif self.path == "/data.gz":
               self.send_response(200)
               self.send_header("Content-Encoding", "gzip")
               self.send_header("Content-Length", str(len(GZ)))
               self.send_header("Connection", "keep-alive")
               self.end_headers()
               self.wfile.write(GZ)
           else:
               self.send_response(404)
               self.send_header("Content-Length", "0")
               self.end_headers()

   with socketserver.TCPServer(("127.0.0.1", 8000), H) as httpd:
       httpd.serve_forever()
   ```

2. 启动服务：`python3 /tmp/u4l2-server/server.py`（保持前台）。
3. 下载并留日志：
   `./src/aria2c -l gz.log --log-level=info -d /tmp/u4l2 http://127.0.0.1:8000/redirect`
4. 再关闭 gzip 协商对照一次（预期落盘为压缩字节）：
   直接下载 `http://127.0.0.1:8000/data.gz`（默认不带 `Accept-Encoding`，aria2 不解压，保存原始 gzip 字节；文件名 `data.gz`）。

**需要观察并回答的三个问题**（对照源码写结论）：

1. **`Location` 的旅程**：日志里按时间顺序找三行——`CUID#x - Requesting: /redirect`（`MSG_SENDING_REQUEST`，[HttpConnection.cc:128-129](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpConnection.cc#L128-L129)）、`CUID#x - Redirecting to http://127.0.0.1:8000/data.gz`（`MSG_REDIRECT`，[HttpResponse.cc:168-169](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L168-L169)）、再次 `Requesting: /data.gz`。注意相对地址 `/data.gz` 被补全成绝对 URI——这正是 [Request.cc:136-144](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Request.cc#L136-L144) 的 `uri::joinUri` 分支在起作用。
2. **`Content-Encoding: gzip` 的旅程**：响应头先被 `HttpHeaderProcessor` 的 `FIELD_NAME` 状态识别、小写化、命中白名单 `CONTENT_ENCODING` 入表（[HttpHeaderProcessor.cc:368-372](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpHeaderProcessor.cc#L368-L372)）；随后 `getContentEncodingStreamFilter()` 生成 GZip 过滤器（[HttpResponse.cc:211-222](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L211-L222)）；因为这是解压下载，`shouldInflateContentEncoding` 为真时长度信息被忽略、走 `handleOtherEncoding` 流式路径（[HttpResponseCommand.cc:278-291](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L278-L291)）。**重要**：步骤 3 默认没带 `--http-accept-gzip=true`，`acceptGZip()` 为假，所以即使服务器给了 gzip，aria2 也会原样保存——请补跑一次 `--http-accept-gzip=true` 对比落盘大小，这一对对照正是 [HttpResponseCommand.cc:346-348](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L346-L348) 判定条件的活教材。
3. **连接复用**：302 跳转后第二次 `Requesting` 是否复用了同一条连接（日志中两次请求间没有新的 DNS/连接日志，且服务端 `BaseHTTPRequestHandler` 在同一进程线程内连续处理）——对应 [HttpSkipResponseCommand.cc:166-173](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpSkipResponseCommand.cc#L166-L173) 的 `poolConnection()` 与 4.4.3 的复用条件链。

**预期结果**：日志能完整回答三问；带 `--http-accept-gzip=true` 时得到约 5KB 的解压文本（`PAYLOAD` 约 27×200 字符），不带时得到几百字节的 gzip 文件。服务脚本与端口行为待本地验证（python3 标准库环境差异可能导致端口占用问题，可换端口）。

## 6. 本讲小结

- `HttpConnection` 是单连接会话对象：发送侧经 `SocketBuffer` 部分写、接收侧把整个 `SocketRecvBuffer` 增量喂给队首请求的专属 `HttpHeaderProcessor`，FIFO 在途队列是 pipelining 的配对基础；1xx 临时响应被透明跳过。
- `HttpHeaderProcessor` 是双模式流式状态机（客户端/服务端共用），逐字节推进、`lastBytesProcessed` 精确汇报消费量；收尾时做头部大小防护与「TE 覆盖 CL/CR」的语义修正；`clear()`/`resetHttpHeaderProcessor()` 支撑复用。
- `HttpHeader` 只存白名单头部（multimap 允许 `Set-Cookie`/`Link`/`Digest` 重复），`getRange()` 负责把 `Content-Range`/`Content-Length` 容错地翻译成区间，`isKeepAlive()` 给出复用判定的服务器侧半边。
- `HttpRequest::createRequest()` 是发送时刻的求值：Range 区间来自 Segment（经 `gtoloff`），鉴权/Cookie/Want-Digest 按需生成，`--header` 同名用户头彻底压制内置头。
- 重定向 = 跳完 body 后 `Request::redirectUri`（支持相对地址拼接）+ 计数上限 20 + 复用连接重试；gzip 解压要求「本端请求过 + 编码匹配」双条件，且解压下载自动放弃分段与预分配。
- 连接复用条件链：服务器头允许（无 close 且 HTTP/1.1 或显式 keep-alive）∧ 本端开启 keep-alive ∧ 响应体完整消费后才回池。

## 7. 下一步学习建议

- **u4-l3（认证与 Cookie）**：本讲多次出现的 `AuthConfigFactory`、`CookieStorage::criteriaFind` 将被完整拆解，补齐 `--http-user`/netrc/cookies.txt 的优先级链。
- **u4-l4（代理与隧道）**：把本讲的 `createProxyRequest()`（CONNECT）与 `Proxy-Authorization` 放进完整代理命令链。
- **u4-l6（流式过滤）**：本讲只讲了过滤器「何时被选中」，chunked 状态机与 gzip 解压的字节级行为在那一讲展开。
- 想巩固解析器，可精读 [test/HttpHeaderProcessorTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/HttpHeaderProcessorTest.cc) 与 [test/HttpRequestTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/HttpRequestTest.cc) 中对 `createRequest()` 输出文本的逐字符断言——它们是报文格式最精确的规格说明。
