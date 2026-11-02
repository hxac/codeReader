# u5-l3 Range 校验、断点续传与降级策略

## 1. 本讲目标

上一讲（u5-l2）我们看到了 HTTP Fetcher 在「服务器完全正常」时的样子：chunk 切分、慢启动扩张、工作窃取。但真实网络远比这混乱——服务器会撒谎（声称支持 Range 却返回 200）、会出错（Content-Range 与请求不符）、会中途断线。本讲回答三个问题：

1. **校验**：Fetcher 如何用一个「法庭」级别的校验器判断 206 响应是否可信，防止把错位的字节写进文件？
2. **降级**：当服务器忽略 Range 请求恒返 200 时，何时可以安全地退化为单连接顺序下载，何时必须报错？
3. **续传**：传输中断后如何重连？暂停后如何恢复？文件总大小未知时（openEnded chunk）如何在运行中「发现」大小？

学完本讲，你应当能够：

- 说出 `downloadChunkOnce` 中 206 / 200 / 416 / 5xx 各状态码触发的处理分支；
- 解释 `fallbackToSequentialDownload` 允许降级的两个前置条件（`conn.ID == 0` 且仅有 1 条连接）以及为什么多连接时必须失败；
- 解释 openEnded chunk 如何通过第一个 `bytes=start-` 请求的 Content-Range 揭示文件总大小；
- 读懂 `resumeConnections` 的续传与重启决策表。

## 2. 前置知识

### 2.1 HTTP 断点续传的三组头

Gopeed 的 HTTP 实现围绕 RFC 7233 的三个头展开，它们都在 [pkg/base/constants.go:14-31](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/constants.go#L14-L31) 中定义为常量（`HttpCodeOK = 200`、`HttpCodePartialContent = 206`、`HttpHeaderRange`、`HttpHeaderContentRange`、`HttpHeaderIfRange` 等）：

| 请求头/响应头 | 方向 | 含义 |
|---|---|---|
| `Range: bytes=100-199` | 客户端→服务器 | 「我只想要第 100~199 字节」 |
| `206 Partial Content` + `Content-Range: bytes 100-199/1024` | 服务器→客户端 | 「给你这 100 字节，全文共 1024 字节」 |
| `If-Range: "etag-x"` | 客户端→服务器 | 「如果文件还是 etag-x 那个版本，就给我 Range；如果变了，直接给我全新全文（200）」 |

`If-Range` 是断点续传的安全锁：如果服务器上的文件在两次请求之间被修改过，带上 `If-Range` 的 Range 请求会得到 200（新全文）而不是 206（旧文件的片段），客户端就知道旧数据作废、必须从零开始。

### 2.2 三类「坏服务器」画像

本讲反复对照三类故障服务器，它们都能在测试基础设施 `internal/test/httptest.go` 中找到真实实现：

| 画像 | 行为 | 对应测试服务器 |
|---|---|---|
| 忽略 Range 恒返 200 | 响应头声明 `Accept-Ranges: bytes`，但收到 `Range` 请求仍返回 200 全文 | `StartTestIgnoredRangeServer` |
| 206 响应内容不合法 | Content-Range 与请求区间不符、返回多于区间的数据等 | `StartTestRangeBugServer` |
| 传输中途断开/超时 | 连接建立后响应体中断或长时间不吐数据 | `StartTestTimeoutOnceServer`、`StartTestFailThenRecoverServer`（416 版本） |

### 2.3 与前两讲的衔接

u5-l1 讲过：Resolve 阶段用一次不带 Range 的 GET 探测元数据并保留响应体做预取；u5-l2 讲过：chunk 以 `Begin/End/Downloaded` 记账，connection 是逻辑车道。本讲把这些零件放进「出错怎么办」的语境——所有校验与恢复逻辑都发生在 `downloadChunkOnce`（每次连接的每轮请求）和 `runConnection`（每条连接的重试外壳）里。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [internal/protocol/http/fetcher.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go) | 本讲主战场：`downloadChunkOnce`、`validateRangeResponse`、降级与恢复函数、`resumeConnections` |
| [internal/protocol/http/helper.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/helper.go) | 错误分类（哪些状态码豁免重试计数）、If-Range 校验器提取、Range 头格式化、重定向过期回退 |
| [pkg/base/constants.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/constants.go) | 状态码与 HTTP 头常量 |
| [internal/test/httptest.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/test/httptest.go) | 各类「坏服务器」测试替身 |
| [internal/protocol/http/fetcher_test.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_test.go) | 验证本讲每个行为均有对应测试 |

## 4. 核心概念与源码讲解

### 4.1 downloadChunkOnce：一次请求的完整生命周期

#### 4.1.1 概念说明

`downloadChunkOnce` 是 HTTP Fetcher 的心脏：它为当前 connection 的 chunk 发起**一次** HTTP 请求（不含重试——重试属于外层 `runConnection` 的职责），并对响应做三件事：

1. **分类**：根据状态码把响应路由到「验证」「降级」「报错」分支；
2. **防护**：在读响应体的过程中持续校验长度、丢弃重叠字节、尊重并发切分后的新边界；
3. **记账**：把读到的字节经 `WriteAt` 落到目标文件的正确偏移上。

设计动机：多连接下载的本质是「多个区间拼成一个文件」，任何一字节错位都会产出损坏的文件。所以这个函数宁可失败也不接受语义模糊的响应——三个哨兵错误表达了它的态度（见 [internal/protocol/http/fetcher.go:47-59](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L47-L59)）：

- `errRangeRequestIgnored`：服务器忽略了 Range 请求（返回 200 全文）；
- `errInvalidRangeResponse`：响应与请求语义不符（Content-Range 错位、长度不符等）；
- `errIncompleteResponse`：响应体比承诺的短（传输中断）。

前两者属于「Range 完整性错误」（`isTerminalRangeError`），是**终态失败**——重试同样的请求大概率还是错，因为问题出在服务器行为而非网络抖动；`errIncompleteResponse` 则可以安全重试，重连后从 chunk 剩余部分继续。

#### 4.1.2 核心流程

`downloadChunkOnce` 的整体决策树：

```text
构建请求（带上当前 chunk 的 Range 与可能的 If-Range）
        │ 发送，得到 resp
        ▼
┌─ 状态码 ∉ {200, 206}？
│    ├─ 有重定向 URL 且状态码 ∈ {401,403,404,410}（疑似链接过期）
│    │      → tryFallbackToOriginalURL：用原始 URL 重发一次
│    └─ 否则 → 返回 RequestError(code)，交给 runConnection 分类重试
│
├─ 带 Range 请求 && 得到 200（服务器忽略 Range）
│    ├─ conn.ID==0 且 Resolve 预取还活着 → 停预取、拷贝已下载前缀
│    ├─ 是 If-Range 恢复探测（resumeProbe）且资源已变
│    │      → restartSequentialDownload：旧前缀作废，从字节 0 消费此全文
│    └─ 否则 → fallbackToSequentialDownload
│         ├─ 单连接且 conn.ID==0 → 降级为顺序模式，从 0 重下
│         └─ 多连接已扩张 → 返回 errRangeRequestIgnored（终态）
│
├─ 带 Range 请求 && 得到 206
│    ├─ validateRangeResponse 严格校验 Content-Range/Content-Length
│    ├─ chunk 是 openEnded（总大小未知）→ resolveOpenEndedRange 发现大小
│    ├─ conn.ID==0 → commitResolveTakeover 接管 Resolve 预取（丢弃重叠字节）
│    └─ resumeProbe 成功 → 恢复 Range 模式，前缀计数保留续传
│
└─ 不带 Range（顺序模式）&& 200 → 直接读，校验长度一致性
        │
        ▼ 进入读循环：逐块 Read → 校验长度上限 → 丢弃 overlap →
          重读 chunk 边界 → WriteAt → 更新 Downloaded
        │
        ▼ EOF 时：实际字节数 ≠ 期望字节数？
             带 Range 且偏短 → errIncompleteResponse（可重试）
             其他不符       → errInvalidRangeResponse（终态）
```

读循环里有一条贯穿全程的**覆盖不变量**：任意时刻，所有活跃 chunk 的区间并集恰好覆盖「未完成字节」，不重不漏：

\[ \bigcup_i [\mathrm{Begin}_i,\ \mathrm{End}_i] \;\cup\; \{\text{已完成字节}\} \;=\; [0,\ \mathrm{Size}) \]

切分（u5-l2）改写区间端点，读循环每次写入前重新读取边界（见 4.1.3 第 5 段），两者配合使这条等式在并发下始终成立。

#### 4.1.3 源码精读

**① 锁内快照与重启决策**（[internal/protocol/http/fetcher.go:1310-1341](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1310-L1341)）：函数开头在 `connMu` 锁内确定本次请求的「身份」——是普通 Range 请求、顺序重启，还是 If-Range 恢复探测：

```go
sequentialSizeUnknown := f.sequentialSizeUnknown
hasSequentialPrefix := f.hasSequentialPrefixLocked(conn)
resumeProbe := f.canProbeSequentialResumeLocked(conn)
if sequentialSizeUnknown || (hasSequentialPrefix && !resumeProbe) {
    f.resetConnectionForRestart(conn)   // 不可续传的前缀 → 从字节 0 重启
    ...
}
```

顺序模式（服务器不支持 Range）下的续传规则很朴素：**没有 If-Range 安全校验器就没有「从中间继续」这回事**，只能从零重启。只有当 `resumeProbe` 成立（存在安全校验器且前缀有效）时才允许带着 `If-Range` 请求剩余部分。

**② Range / If-Range 请求头构造**（[internal/protocol/http/fetcher.go:1352-1358](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1352-L1358)）：只有 Range 模式或恢复探测才发 `Range` 头；`If-Range` 只在探测、或校验器已锁定（`rangeValidatorPinned`）、或 Resolve 兜底还在时携带：

```go
if rangeMode || resumeProbe {
    httpReq.Header.Set(base.HttpHeaderRange, formatRangeHeader(rangeStart, rangeEnd))
    if ifRange != "" {
        httpReq.Header.Set(base.HttpHeaderIfRange, ifRange)
    }
}
```

`formatRangeHeader` 在 [internal/protocol/http/helper.go:235-240](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/helper.go#L235-L240)：`end == nil` 时输出 `bytes=N-`（开放式，4.4 节的主角）。

**③ 非 200/206 状态码与重定向过期回退**（[internal/protocol/http/fetcher.go:1368-1393](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1368-L1393)）：先包装成 `RequestError(code)`；若下载地址是重定向产物且状态码疑似「链接过期」（`isRedirectExpiredError`：401/403/404/410，见 [internal/protocol/http/helper.go:191-205](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/helper.go#L191-L205)），则调用 `tryFallbackToOriginalURL`（[internal/protocol/http/helper.go:209-233](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/helper.go#L209-L233)）用**原始 URL** 重发一次请求（保留 Range 与 If-Range 头），成功则顺路更新重定向地址。这解决了「签名下载链接中途过期」的真实场景。

**④ 三大分支的状态码分发**（[internal/protocol/http/fetcher.go:1397-1447](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1397-L1447)）：`rangeRequested && 200` 走降级（4.3 节展开）；`rangeRequested && 206` 走校验（4.2 节展开）+ openEnded 解析（4.4 节）+ 预取接管（u5-l1 已讲）；两分支代码骨架：

```go
if rangeRequested && resp.StatusCode == base.HttpCodeOK {
    if conn.ID == 0 && f.resolveFallback.CompareAndSwap(true, false) {
        f.stopPrefetchAndCopyData()          // 全文响应即权威，停掉 Resolve 预取
    }
    if resumeProbe {
        f.restartSequentialDownload(conn, extractIfRangeValidator(resp.Header))
    } else if err := f.fallbackToSequentialDownload(conn, extractIfRangeValidator(resp.Header)); err != nil {
        return fmt.Errorf("%w: expected 206 Partial Content, got 200 OK", err)
    }
}
if rangeRequested && resp.StatusCode == base.HttpCodePartialContent {
    expectedResponseLength, responseTotal, err = validateRangeResponse(resp, rangeStart, rangeEnd, f.knownSize())
    if err != nil { resp.Body.Close(); return err }
    if openEndedRange {
        if err := f.resolveOpenEndedRange(conn, responseTotal); err != nil { ... }
    }
    responseBytesToDiscard, err = f.commitResolveTakeover(conn, rangeStart)
    ...
}
```

注意降级失败时错误被 `errRangeRequestIgnored` 包装返回——`%w` 保证 `errors.Is` 能穿透识别。

**⑤ 读循环的三重防护**（[internal/protocol/http/fetcher.go:1496-1592](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1496-L1592)）：

```go
if expectedResponseLength >= 0 && responseBytesRead > expectedResponseLength {
    return fmt.Errorf("%w: response body exceeds expected length %d", ...)
}
...
if responseBytesToDiscard > 0 {          // 丢弃与 Resolve 预取重叠的字节
    discard := min(int64(len(data)), responseBytesToDiscard)
    data = data[discard:]
    responseBytesToDiscard -= discard
}
...
f.connMu.Lock()
if f.meta.Res.Range {
    remain := conn.Chunk.remain()         // 写入前重读边界：尊重并发切分
    if remain < int64(len(data)) {
        data = data[:remain]              // 超额数据截断（u5-l2 工作窃取的另一半）
        finished = true
    }
}
writeOffset = conn.Chunk.Begin + conn.Chunk.Downloaded
f.connMu.Unlock()
```

EOF 时的终审（[internal/protocol/http/fetcher.go:1582-1587](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1582-L1587)）：实际读到的字节数少于期望——带 Range 的请求说明传输被截断，判 `errIncompleteResponse`（可重试续传）；其余不符判 `errInvalidRangeResponse`（终态）。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「服务器返回多于 Range 区间的数据」如何被校验器拒绝。

1. 打开 [internal/protocol/http/fetcher_test.go:2118-2131](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_test.go#L2118-L2131)，阅读 `TestFetcher_DownloadOnBugFileServer`：单连接时它能下完（顺序模式吞掉多余数据），4 连接时 `Wait()` 必须返回含 `errInvalidRangeResponse` 的错误。
2. 打开坏服务器实现 [internal/test/httptest.go:679-703](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/test/httptest.go#L679-L703)：`StartTestRangeBugServer` 通过 `modifyEnd` 回调把每个 Range 的结尾多给 50 字节。
3. 运行：

```bash
go test ./internal/protocol/http/ -run 'TestFetcher_DownloadOnBugFileServer' -v
```

**需要观察的现象**：测试输出中两个子场景的结果差异。

**预期结果**：`downloadNormal(listener, 1, t)`（单连接）通过；4 连接场景断言 `Wait()` 返回的错误字符串包含 `invalid HTTP range response`。若断言失败，说明校验逻辑被改动，多余字节被静默写入——那正是这段代码要防的文件损坏。完整事件层面（Downloader 视角）的表现待本地验证（可用 u1-l4 的 Boot API 小工具复现）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `errIncompleteResponse` 可以重试而 `errRangeRequestIgnored` 是终态失败？

**答案**：`errIncompleteResponse` 表示响应体比 Content-Range 承诺的短——通常是网络中断或服务器提前断开，chunk 的 `Downloaded` 记账仍然有效，重连后请求 `Begin+Downloaded` 之后的区间即可续传；而 `errRangeRequestIgnored` / `errInvalidRangeResponse` 表示**服务器对 Range 语义的实现与客户端预期不符**，同样的请求再发一次还会得到同样的错响应，重试无意义。[internal/protocol/http/fetcher.go:53-59](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L53-L59) 的 `isTerminalRangeError` 只收录这两个完整性错误，`runConnection` 在 [internal/protocol/http/fetcher.go:1240-1250](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1240-L1250) 命中后直接把连接置为 `connFailed` 终态。

**练习 2**：`downloadChunkOnce` 读循环中，为什么每次写文件前要重新 `f.connMu.Lock()` 读一遍 `conn.Chunk` 的边界？

**答案**：因为 u5-l2 的工作窃取（`helpOtherConnection`）与慢启动扩张会在别的协程里修改本连接 chunk 的 `End`（被偷走后半段）甚至 `Begin`（变成帮助者）。如果不重读，就可能把已经分给别人的字节再写一遍。重读后 `remain` 变小，多余数据在 `data = data[:remain]` 处被截断，保证覆盖不变量不被破坏。见 [internal/protocol/http/fetcher.go:1523-1535](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1523-L1535)。

### 4.2 validateRangeResponse：206 的严格校验

#### 4.2.1 概念说明

当客户端请求 `Range: bytes=1024-2047`，一个行为正确的服务器应返回 `206` + `Content-Range: bytes 1024-2047/4096` + `Content-Length: 1024`。但现实中有服务器返回错位的区间、错误的总量、甚至与 Content-Length 矛盾的响应。`parseRangeResponse` 负责把 `Content-Range` 头解析成 `(start, end, total)` 三元组并做格式与一致性校验；`validateRangeResponse` 在此之上再核对「服务器给的区间 == 客户端请求的区间」。任何一步不符都返回 `errInvalidRangeResponse`。

为什么如此严格？因为 206 响应的每个字节都会被 `WriteAt` 到 `Begin+Downloaded` 偏移上——如果服务器返回的是 `bytes 0-1023` 而客户端请求的是 `bytes 1024-2047`，这 1024 字节会被写到错误位置，静默毁掉整个文件。**宁可信不过，不可写错位。**

#### 4.2.2 核心流程

`parseRangeResponse` 的校验清单（对 `Content-Range: bytes start-end/total`）：

1. 恰好两个空格分隔的字段，第一字段大小写不敏感等于 `bytes`；
2. 第二字段恰好含一个 `/`，左侧 `start-end` 恰好含一个 `-`；
3. 三个数字均可解析，且满足 \(0 \le \text{start} \le \text{end} < \text{total}\)；
4. 响应 `Content-Length`（若存在）等于 \(\text{end} - \text{start} + 1\)。

注意第 4 条的数学含义：206 响应体的期望长度由 Content-Range 唯一确定，这个值会作为 `expectedResponseLength` 传给读循环，成为 4.1.3 第 ⑤ 段「超过期望长度即报错」的依据。

`validateRangeResponse` 追加两条与**本次请求**的对照：

- `start == requestedStart` 且 `end == requestedEnd`（开放式请求时 `end == total-1`）；
- 已知资源大小时 `total == totalSize`。

一个重要细节：**未知总大小（`total` 为 `*`）会被拒绝**——「一段的长度」不能冒充「全文的大小」，开放式请求的总量发现走的是 4.4 节的专门路径。

#### 4.2.3 源码精读

[internal/protocol/http/fetcher.go:1668-1695](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1668-L1695) 是 `parseRangeResponse`，逐层剥开 `Content-Range`：

```go
fields := strings.Fields(contentRange)
if len(fields) != 2 || !strings.EqualFold(fields[0], base.HttpHeaderBytes) {
    return 0, 0, 0, fmt.Errorf("%w: malformed Content-Range %q", errInvalidRangeResponse, contentRange)
}
...
if startErr != nil || endErr != nil || totalErr != nil || start < 0 || end < start || total <= end {
    return 0, 0, 0, fmt.Errorf("%w: malformed Content-Range %q", errInvalidRangeResponse, contentRange)
}
expectedLength := end - start + 1
if resp.ContentLength >= 0 && resp.ContentLength != expectedLength {
    return 0, 0, 0, fmt.Errorf("%w: Content-Length %d does not match range length %d", ...)
}
```

[internal/protocol/http/fetcher.go:1697-1710](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1697-L1710) 是 `validateRangeResponse`，把服务器响应对照请求意愿：

```go
if start != requestedStart || (requestedEnd != nil && end != *requestedEnd) || (requestedEnd == nil && end != total-1) {
    return 0, 0, fmt.Errorf("%w: Content-Range %d-%d does not match requested range", errInvalidRangeResponse, start, end)
}
if totalSize > 0 && total != totalSize {
    return 0, 0, fmt.Errorf("%w: Content-Range total %d does not match resource size %d", ...)
}
```

这段代码在 `downloadChunkOnce` 的调用点见 [internal/protocol/http/fetcher.go:1417-1423](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1417-L1423)：任何校验失败都关掉响应体并立即返回错误，绝不让可疑字节进入写循环。

#### 4.2.4 代码实践

**实践目标**：用现成的表驱动测试复现 9 种非法 206 响应，并亲手追加一个新用例。

1. 阅读 [internal/protocol/http/fetcher_test.go:1727-1810](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_test.go#L1727-L1810) `TestFetcher_RejectsInvalidPartialContent` 的用例表：缺 Content-Range、单位写成 `items`、区间写法残缺、总量未知（`*`）、区间错位（请求 1024-2047 却返回 0-1023）、总量翻倍、Content-Length 不符……
2. 运行：

```bash
go test ./internal/protocol/http/ -run 'TestFetcher_RejectsInvalidPartialContent' -v
```

3. （可选，在读者自己的分支上）向用例表追加一项 `{"reversed range", "bytes 2047-1024/4096", rangeLength, int(rangeLength), false, true}`。

**需要观察的现象**：所有 `wantError: true` 的用例全部通过；`-v` 输出中没有任何用例把非法响应当作有效。

**预期结果**：追加的「区间倒置」用例也会被 `parseRangeResponse` 的 `end < start` 检查拒绝（[fetcher.go:1687](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1687)），测试通过。若你暂时不想改测试文件，仅执行第 1、2 步即可，预期所有子用例 PASS。

#### 4.2.5 小练习与答案

**练习 1**：为什么「总量未知」（`bytes 1024-2047/*`）被 `parseRangeResponse` 拒绝，而 4.4 节的 openEnded 请求却能拿到合法 206？

**答案**：`parseRangeResponse` 服务的是**有明确边界的区间请求**——调用方已知 `requestedEnd`，需要 total 来核对资源大小一致性，`*` 使核对无法进行。openEnded 场景下客户端请求的是 `bytes=N-`（无上界），此时 total 是客户端**唯一想从响应中获取的新信息**，它由 `resolveOpenEndedRange` 消费（要求 `total > 0`），并不需要与已知大小核对（大小本来就未知）。

**练习 2**：`validateRangeResponse` 的第三个参数 `totalSize` 为 0 时跳过总量核对，什么时候会是 0？

**答案**：`f.knownSize()` 在资源大小未知（`sizeKnown` 为 false 且 Res.Size 为提示值或 0）时返回 0，见 [internal/protocol/http/fetcher.go:385-390](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L385-L390)。此时没有权威大小可对照，只校验区间与请求一致；总量随后由 openEnded 路径（4.4 节）或首个完整响应确立。

### 4.3 顺序回退与 If-Range 重启

#### 4.3.1 概念说明

「声称支持 Range 却对 Range 请求返回 200 全文」的服务器并不罕见（一些 CDN 与动态生成内容的站点如此）。对此 `fallbackToSequentialDownload` 的策略是：**如果能安全退化为单连接顺序下载，就退化；否则失败**。

安全的前提有两个（见 [internal/protocol/http/fetcher.go:1751-1753](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1751-L1753)）：

- `conn.ID == 0`——降级只能发生在慢启动的第一条连接上（它请求的区间从预取末尾开始，之前的字节全在预取/前缀里，重启后由 200 全文覆盖）；
- `len(f.connections) == 1`——还没有其他连接在别的区间上并行下载。

为什么多连接时必须失败？假设连接 A 在下 `[0, 5MB)`、连接 B 在下 `[5MB, 10MB)`，此时某个新连接的 Range 请求得到 200 全文——这份全文从字节 0 开始，与 A、B 正在写的区间交错，若就地消费必然互相覆盖。代码注释直言：「若服务器在并行连接启动后改变行为，宁可失败也不冒拼出损坏文件的风险」。

降级之后世界变简单了：`Res.Range = false`，任务退化为单连接从 0 顺序下载，并且置位 `rangeReprobeEligible`——**留一扇门**：以后重试/续传时用 `If-Range` 探测服务器是否恢复了 Range 支持，恢复了就升回多连接模式。

#### 4.3.2 核心流程

```text
fallbackToSequentialDownload(conn)：      # 仅 conn.ID==0 且连接数==1 时可走
    Res.Range = false                     # 声明放弃 Range 模式
    rangeReprobeEligible = true           # 标记「可再探测」
    rangeValidatorPinned = false
    ifRange = extractIfRangeValidator(本次 200 响应头)   # 从新响应取校验器
    resetConnectionForRestart(conn)       # chunk 归零，从字节 0 重下
    resolveDataPos = 0

resumeProbe（后续每次请求前的探测决策，canProbeSequentialResumeLocked）：
    条件 = 顺序模式 && 单连接 && 前缀 0 < Downloaded < Size && 有 If-Range 校验器 && 大小已知
    成立 → 本次请求带 Range: bytes=<Downloaded>-<Size-1> + If-Range
        ├─ 服务器回 206 → 资源未变：恢复 Range 模式，保留前缀计数，续传+可再扩张
        └─ 服务器回 200 → 资源已变：restartSequentialDownload，旧前缀作废，从 0 消费新全文
```

`extractIfRangeValidator`（[internal/protocol/http/helper.go:34-54](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/helper.go#L34-L54)）只接受「强」校验器：强 ETag（`isStrongETag`，[helper.go:56-68](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/helper.go#L56-L68)），或满足「响应 Date 比 Last-Modified 至日晚 1 秒」的 Last-Modified——弱 ETag（`W/"..."`）或刚生成文件的 Last-Modified 不能证明资源未变，一律不用于 If-Range。

#### 4.3.3 源码精读

**① 降级决策**（[internal/protocol/http/fetcher.go:1744-1762](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1744-L1762)）：

```go
func (f *Fetcher) fallbackToSequentialDownload(conn *connection, ifRange string) error {
	f.connMu.Lock()
	defer f.connMu.Unlock()
	if !f.meta.Res.Range {
		return nil                        // 已是顺序模式，无事可做
	}
	if conn.ID != 0 || len(f.connections) != 1 {
		return errRangeRequestIgnored     // 多连接场景：终态失败
	}
	f.meta.Res.Range = false
	f.rangeReprobeEligible = true
	...
}
```

**② 前缀可续传判定**（[internal/protocol/http/fetcher.go:2113-2128](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2113-L2128)）：`hasSequentialPrefixLocked` 要求「顺序模式 + 可再探测 + 单连接 + `0 < Downloaded < Size`」；`canProbeSequentialResumeLocked` 在此之上要求「大小已知 + 存在 If-Range 校验器」。

**③ 探测成功的恢复**（[internal/protocol/http/fetcher.go:1435-1446](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1435-L1446)）：206 验证通过后，把保留的前缀计数换算回标准 chunk：

```go
if resumeProbe {
    f.connMu.Lock()
    f.meta.Res.Range = true
    f.rangeReprobeEligible = false
    f.rangeValidatorPinned = true
    conn.Chunk.Begin = 0
    conn.Chunk.setEnd(f.knownSize() - 1)
    f.connMu.Unlock()
}
```

关键在**不重置** `conn.Chunk.Downloaded`：前缀已下载的字节数被保留，读循环的写偏移 `Begin + Downloaded` 恰好落在前缀末尾，无缝续传。`downloadLoop` 的恢复路径（[internal/protocol/http/fetcher.go:949-962](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L949-L962)）还会因此专门为「有可恢复前缀」的任务保留扩张循环——探测成功即可重新切分并行。

**④ 资源已变的重启**（[internal/protocol/http/fetcher.go:1767-1777](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1767-L1777)）：`restartSequentialDownload` 与降级几乎同构，语义差别在注释里：「旧前缀不再可信，但这个响应体可以从字节 0 安全覆盖它」。配合 `resizeSequentialResource`（[fetcher.go:1782-1808](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1782-L1808)，按新长度 Truncate 文件并发布新大小）与 `beginUnknownSizeSequentialResource`（[fetcher.go:1814-1835](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1814-L1835)，未知长度时清空文件、记录 `sequentialSizeUnknown` 强制下次从零）。

#### 4.3.4 代码实践

**实践目标**：验证「忽略 Range 恒返 200」的服务器会被降级为单连接顺序下载且文件完好。

1. 阅读坏服务器 [internal/test/httptest.go:377-412](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/test/httptest.go#L377-L412)：`StartTestIgnoredRangeServer` 设置 `Accept-Ranges: bytes` 但 `WriteHeader(http.StatusOK)`。
2. 阅读测试 [internal/protocol/http/fetcher_test.go:532-561](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_test.go#L532-L561)：无论配置 1 还是 4 连接，断言降级后 `f.meta.Res.Range == false`、Stats 只剩 1 条连接、文件 MD5 与源一致。
3. 运行：

```bash
go test ./internal/protocol/http/ -run 'TestFetcher_DownloadIgnoredRangeFallsBackToSequential|TestFetcher_DownloadContinue_NoRangeRestart' -v
```

**需要观察的现象**：`connections_1` 与 `connections_4` 两个子测试都通过——4 连接配置在慢启动第一条连接就撞上 200，此时连接数还是 1，恰好满足降级条件。

**预期结果**：两个测试全部 PASS。`TestFetcher_DownloadContinue_NoRangeRestart`（[fetcher_test.go:466-511](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_test.go#L466-L511)）进一步展示纯顺序服务器（从不声明 Range）中途 Pause/Start 后 `Downloaded` 从零干净重启、最终 MD5 一致。

#### 4.3.5 小练习与答案

**练习 1**：如果慢启动已经扩张到 3 条连接，第 4 条新连接的 Range 请求得到 200，会发生什么？任务最终状态是什么？

**答案**：`fallbackToSequentialDownload` 在 [fetcher.go:1751-1753](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1751-L1753) 命中 `len(f.connections) != 1` 返回 `errRangeRequestIgnored`；该错误是终态 Range 错误，`runConnection` 把连接置 `connFailed`；`onDownloadComplete` 的裁决（[fetcher.go:2230-2238](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2230-L2238)）明确「完整性错误优先于字节计数完成」，即使其余连接凑够了字节数，任务仍以 error 收场。这是刻意为之：服务器行为已不可信，凑齐的字节也可能是错位的。

**练习 2**：`rangeReprobeEligible` 这面旗子在整个生命周期里被谁升起、被谁降下？

**答案**：升起于 `fallbackToSequentialDownload` / `restartSequentialDownload`（降级或资源已变重启时）；降下于探测成功（[fetcher.go:1440](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1440) 恢复 Range 模式时）、`completeFromResolveFallback`（用 Resolve 流完成时）以及 `prepareFullRedownload`（校验和不匹配强制全量重下时）。此外每个非 Range 的 200 响应都会刷新 `ifRange` 校验器（[fetcher.go:1448-1456](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1448-L1456)），保证后续探测用的是最新版本的校验器。

### 4.4 openEnded：运行中发现文件总大小

#### 4.4.1 概念说明

有些服务器声明 `Accept-Ranges: bytes` 却不给 `Content-Length`（例如 chunked 传输的动态内容）。Resolve 阶段拿不到权威大小，`sizeKnown` 为 false，u5-l2 的 chunk 切分便无从谈起——连「切多少」都不知道。

Gopeed 的解法是 openEnded chunk：`chunk.End` 是 `*int64`（[fetcher.go:100-104](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L100-L104)），`nil` 表示「终点未知」。第一条连接用**开放式 Range 请求** `bytes=N-`（无上界）探路，服务器返回的 `206` + `Content-Range: bytes N-(total-1)/total` 会一次性揭示总大小——于是 `End` 被填上、文件被 Truncate 到目标长度、资源大小被发布，慢启动随后照常扩张切分。

#### 4.4.2 核心流程

```text
Resolve：无 Content-Length && Range 支持 → Res.Size = 用户提示(可能为 0)，sizeKnown=false
doStart/expandConnections：首连接分到 newOpenEndedChunk(prefetched)
    ↓ Range: bytes=<prefetched>-
downloadChunkOnce：206 → validateRangeResponse（requestedEnd==nil，只要求 end==total-1）
    ↓ total 揭示
resolveOpenEndedRange(total)：
    file.Truncate(total)          # 预分配文件到真实长度
    setResourceSizeLocked(total, known=true)   # 发布权威大小
    conn.Chunk.setEnd(total - 1)  # 开放 chunk 变成普通 chunk
    ↓ 此后扩张/切分/工作窃取照常进行
```

校验上的配合：`validateRangeResponse` 对 `requestedEnd == nil` 的请求只要求 `end == total - 1`（服务器把「从 N 到结尾」如实交代），大小核对因 `totalSize == 0` 而跳过。若服务器返回的 total 非法（≤ 0），`resolveOpenEndedRange` 报 `errInvalidRangeResponse`。

#### 4.4.3 源码精读

**① openEnded chunk 的定义与构造**（[internal/protocol/http/fetcher.go:100-129](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L100-L129)）：

```go
type chunk struct {
	Begin      int64
	End        *int64 // nil until an open-ended Range response reveals the total size
	Downloaded int64
}
func (c *chunk) openEnded() bool { return c.End == nil }
```

**② 首连接的分配**（[internal/protocol/http/fetcher.go:1077-1090](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1077-L1090)）：`expandConnections` 在 `totalSize == 0` 时给首连接 `newOpenEndedChunk(prefetched)`，否则普通闭区间：

```go
connChunk := newOpenEndedChunk(prefetched)
if totalSize > 0 {
    connChunk = newChunk(prefetched, totalSize-1)
}
```

**③ 大小发现与发布**（[internal/protocol/http/fetcher.go:1712-1736](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1712-L1736)）：`resolveOpenEndedRange` 把发现、预分配、发布放进同一个 `connMu` 临界区，注释点明时机——必须在「响应就绪信号触发慢启动扩张」**之前**完成，否则新连接会拿着旧的大小快照去切分：

```go
f.connMu.Lock()
f.fileMu.Lock()
...
err := f.file.Truncate(totalSize)
...
f.setResourceSizeLocked(totalSize, true)
conn.Chunk.setEnd(totalSize - 1)
f.connMu.Unlock()
```

**④ 测试佐证**（[internal/protocol/http/fetcher_test.go:1289-1398](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_test.go#L1289-L1398)）：`TestFetcher_UnknownSizeRangeUsesOpenEndedWorker` 搭了一个「无 Content-Length + 支持 Range」的服务器，断言恰好发出 1 次 `bytes=N-` 开放式请求、随后至少 2 次 Range 请求（扩张证据）、最终 `Res.Size` 等于真实长度且文件字节级一致。

#### 4.4.4 代码实践

**实践目标**：观察开放式 Range 请求如何发现大小并触发扩张。

1. 阅读上述测试中服务器的实现（[fetcher_test.go:1304-1356](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_test.go#L1304-L1356)）：无 Range 头时流式返回 200（模拟无长度），带 `bytes=N-` 后缀的请求返回 206 并附完整 `Content-Range`。
2. 运行：

```bash
go test ./internal/protocol/http/ -run 'TestFetcher_UnknownSizeRange' -v
```

（会匹配到 `...UsesOpenEndedWorker`、`...RetriesShortPartialBody`、`...FallsBackToResolve`、`...RemainsOpenEndedAfterPause` 四个测试。）

**需要观察的现象**：`hint=0/3/8MB` 三个子测试都通过——无论用户提示多少，运行中发现的大小都会覆盖它。

**预期结果**：全部 PASS。其中 `RetriesShortPartialBody` 展示开放响应体被截断时以 `errIncompleteResponse` 重试，`RemainsOpenEndedAfterPause` 展示暂停后 chunk 仍是 openEnded、续传时再次发现大小。

#### 4.4.5 小练习与答案

**练习 1**：为什么 openEnded 场景下 `resolveOpenEndedRange` 要先 `file.Truncate(totalSize)`？

**答案**：一是**预分配**：把目标文件一次性伸展到真实长度，后续 `WriteAt` 落在已分配范围内；二是**发布大小必须在扩张之前**——首连接的 206 响应就绪会触发慢启动控制器的 `onConnectSuccess`（[fetcher.go:1492-1494](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1492-L1494)），进而唤醒 `downloadLoop` 扩张，新连接的切分依据是 `knownSize()`；若大小尚未发布，扩张会因无区间可切而提前进入稳态或失败。代码注释明确写道「Publish the discovered extent before the response-ready signal can expand the slow-start batch」。

**练习 2**：`validateRangeResponse` 对开放式请求（`requestedEnd == nil`）校验 `end == total - 1`，如果服务器对 `bytes=100-` 返回 `Content-Range: bytes 100-500/1000`（只给到中间），会怎样？

**答案**：`end(500) != total-1(999)`，判 `errInvalidRangeResponse` 终态失败。开放式请求的语义是「从 N 到资源结尾」，服务器擅自截短等于变更了语义；Fetcher 不猜测服务器意图，只接受如实交代总量的响应。

### 4.5 resumeConnections：断点续传与重连策略

#### 4.5.1 概念说明

断线与暂停最终都汇聚到同一个问题：**下次启动时，每条连接是「续传」还是「重启」？** `resumeConnections` 在每次 Start（含暂停后恢复、错误后重试）时遍历连接表做裁决；`runConnection` 则是单条连接的重试外壳，负责错误分类、重试计数与退避。两者合起来构成完整的恢复策略：

- **可续传**：Range 模式下 chunk 的 `Downloaded` 天然是断点，直接请求剩余区间；
- **必须重启**：顺序模式且无安全校验器（`hasSequentialPrefixLocked` 为 false）时 `resetConnectionForRestart` 归零；
- **探测后续传**：顺序模式有 If-Range 校验器时保留前缀计数，交给 4.3 节的 resumeProbe；
- **放弃**：已完成、或失败且（403 / 重试耗尽）的连接不再重启。

#### 4.5.2 核心流程

`resumeConnections` 的决策表（[internal/protocol/http/fetcher.go:2139-2178](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2139-L2178)）：

| 连接状态 | 决策 |
|---|---|
| `Completed` / `connCompleted` | 跳过（无需重跑） |
| `connFailed` 且 lastErr 是 403 | 跳过（服务器连接数限制，重试无益） |
| `connFailed` 且 `retryTimes >= 3` | 跳过（重试耗尽） |
| 其余，无顺序前缀可续 | `resetConnectionForRestart` 归零后重启 |
| 其余，有前缀 + If-Range 校验器 | 保留 `Downloaded`，重启后走 resumeProbe |
| 其余（Range 模式） | chunk 原样保留，断点即 `Begin+Downloaded` |

`runConnection` 的重试策略（[internal/protocol/http/fetcher.go:1199-1297](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1199-L1297)）按错误类别分四档：

| 错误类别 | 判定 | 处理 |
|---|---|---|
| 暂停/取消 | `errors.Is(err, context.Canceled)` | 立即退出，不计数 |
| Range 完整性错误 | `isTerminalRangeError`（4.1 节两个哨兵） | 连接终态失败 |
| 403 | `RequestError{403}` | 连接终态失败（服务器连接数上限） |
| 其他 HTTP 错误（416、404 等非豁免码） | `shouldCountHTTPFailure` | `retryTimes++`，达 3 次终态；否则退避后重试 |
| 网络错误 / 5xx / 429 / 408 / 440 / 499 | 豁免码或非 HTTP 错误 | **无限重试**（退避 1s、2s…封顶 5s） |

豁免清单见 [internal/protocol/http/helper.go:78-89](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/helper.go#L78-L89)：5xx 与 429/408/440/499 属于「暂时性」错误，限流与服务器抖动不该消耗重试额度。测试基础设施特意用 416 而非 500 来构造「会耗尽重试的失败」——[internal/test/httptest.go:634-675](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/test/httptest.go#L634-L675) 的注释明确解释了这个选择。

#### 4.5.3 源码精读

**① 重试外壳的关键分支**（[internal/protocol/http/fetcher.go:1232-1296](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1232-L1296)）：

```go
if errors.Is(err, context.Canceled) {
    return                                  // 暂停：干净退出
}
...
if isTerminalRangeError(err) {
    conn.lastErr = err
    conn.State = connFailed
    conn.failed = true                       // 终态，不再重试
    ...
    return
}
if shouldCountHTTPFailure(err) {
    ...
    conn.retryTimes++
    if retryTimes >= 3 {
        conn.State = connFailed              // 计数耗尽，终态
        return
    }
}
retryDelay := time.Second * time.Duration(retries+1)   // 退避：1s,2s,3s... 上限 5s
if retryDelay > 5*time.Second { retryDelay = 5 * time.Second }
retries++
time.Sleep(retryDelay)
```

**② 续传裁决**（[internal/protocol/http/fetcher.go:2143-2170](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2143-L2170)）：

```go
for _, conn := range f.connections {
    if conn.Completed || conn.State == connCompleted {
        continue                              // 已完成：跳过
    }
    if conn.State == connFailed && conn.failed {
        if re := extractRequestError(conn.lastErr); re != nil && re.Code == 403 {
            continue                          // 服务器连接上限：跳过
        }
        if conn.retryTimes >= 3 {
            continue                          // 重试耗尽：跳过
        }
    }
    if !f.hasSequentialPrefixLocked(conn) {
        f.resetConnectionForRestart(conn)     // 无法安全续传：归零重启
    }
    conn.ctx, conn.cancel = context.WithCancel(f.ctx)
    conn.State = connNotStarted
    conn.failed = false                       // 清除失败标记，重新给机会
    toResume = append(toResume, conn)
}
```

**③ 错误后重启的连接重置**（[internal/protocol/http/fetcher.go:817-848](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L817-L848)）：`doStart` 在 `stateError` 下重试时，只重置「未完成」的连接；校验和失败（`checksumFailed`）则重置**全部**连接并清空文件——「Completed 只代表下载循环跑完了，不代表字节是对的」。

**④ 完成裁决中完整性错误的优先级**（[internal/protocol/http/fetcher.go:2227-2238](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2227-L2238)）：即使 `totalDownloaded >= Res.Size`（字节凑齐），只要任何连接带着终态 Range 错误退出，任务仍判 error——损坏的「完成」不算完成。

#### 4.5.4 代码实践

**实践目标**：验证「传输中途断开（读超时）后自动重连续传」与「暂停后恢复」两条路径。

1. 阅读 [internal/protocol/http/fetcher_test.go:2047-2087](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_test.go#L2047-L2087) `TestFetcher_DownloadResponseBodyReadTimeout`：服务器第一次响应故意拖延超过 `readTimeout`，之后恢复正常。
2. 阅读暂停续传 helper [fetcher_test.go:3174-3197](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_test.go#L3174-L3197) `downloadContinue`：Start → 50ms → Pause → 50ms → Start → Wait → MD5 对比。
3. 运行：

```bash
go test ./internal/protocol/http/ -run 'TestFetcher_DownloadResponseBodyReadTimeout|TestFetcher_DownloadContinue$|TestFetcher_RetryAfterError' -v
```

**需要观察的现象**：三个测试分别覆盖「读超时后重连」「暂停恢复」「错误后重启」。

**预期结果**：全部 PASS。`TestFetcher_RetryAfterError`（[fetcher_test.go:2662](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_test.go#L2662)）使用 416 构造 3 次计数失败后服务器恢复、再次 `Start()` 成功——注意 416 走的是「非豁免 HTTP 错误」计数档而非终态档。`TestFetcher_DownloadContinue$` 用 `$` 锚定避免误匹配 `..._NoRangeRestart`。

#### 4.5.5 小练习与答案

**练习 1**：为什么 5xx 与 429 不计入 `retryTimes`，而 416 计入？

**答案**：5xx（服务器内部错误）、429（限流）、408/499（超时/客户端断开）是**暂时性**故障，稍后重试通常自愈，若计入 3 次上限会让慢服务器上的任务轻易失败，所以 [helper.go:78-89](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/helper.go#L78-L89) 把它们列入豁免清单，允许带退避的无限重试；416（Range Not Satisfiable）则往往反映**请求区间与服务器状态不一致**（如文件已变、offset 越界），属于需要计数限制的错误，3 次仍失败就放弃该连接。

**练习 2**：暂停恢复（Pause→Start）时，Range 模式的连接既不走 `resetConnectionForRestart` 也不走 resumeProbe，它是怎么续上的？

**答案**：Range 模式下 chunk 的 `(Begin, End, Downloaded)` 三元组在暂停期间原样保留，`hasSequentialPrefixLocked` 因 `Res.Range == true` 直接返回 false，于是连接既不归零也无前缀探测；重启后 `downloadChunkOnce` 用 `rangeStart = Begin + Downloaded` 发起 Range 请求，从断点继续。只有**顺序模式**才需要「归零或 If-Range 探测」的抉择，因为顺序响应永远从字节 0 开始、没有区间可指名。`downloadLoop` 的恢复入口在 [fetcher.go:934-962](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L934-L962)：`isResume`（连接表非空）时走 `resumeConnections`，且仅当存在可恢复的顺序前缀时才进入扩张循环（探测成功可升回多连接）。

## 5. 综合实践

把本讲的三类「坏服务器」串成一个实验室，逐类记录 Fetcher 的行为矩阵。以下为**示例代码**（不是仓库原有文件），建议放在读者自己的分支上（例如 `internal/protocol/http/badserver_lab_test.go`），不修改任何产品代码：

```go
package http

// 示例代码：三类坏服务器实验室（参考 internal/test/httptest.go 的实现风格）
import (
	"fmt"
	gohttp "net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/GopeedLab/gopeed/pkg/base"
	"github.com/GopeedLab/gopeed/pkg/protocol/http"
)

// 1) 忽略 Range 恒返 200（也可直接用 test.StartTestIgnoredRangeServer）
func newIgnoreRangeServer(t *testing.T, payload []byte) *httptest.Server {
	return httptest.NewServer(gohttp.HandlerFunc(func(w gohttp.ResponseWriter, r *gohttp.Request) {
		w.Header().Set("Accept-Ranges", "bytes")
		w.Header().Set("Content-Length", fmt.Sprint(len(payload)))
		w.WriteHeader(gohttp.StatusOK) // 无论是否带 Range，永远 200 全文
		w.Write(payload)
	}))
}

// 2) 206 与 200 混杂：前 okBefore 次 Range 请求回正确 206，之后回 200
func newMixedServer(t *testing.T, payload []byte, okBefore int32) *httptest.Server {
	var served atomic.Int32
	return httptest.NewServer(gohttp.HandlerFunc(func(w gohttp.ResponseWriter, r *gohttp.Request) {
		w.Header().Set("Accept-Ranges", "bytes")
		w.Header().Set("ETag", `"mixed-v1"`)
		if rh := r.Header.Get(base.HttpHeaderRange); rh == "" {
			w.Header().Set("Content-Length", fmt.Sprint(len(payload)))
			w.WriteHeader(gohttp.StatusOK)
			w.Write(payload)
			return
		}
		if served.Add(1) > okBefore { // 从第 okBefore+1 个 Range 请求开始捣乱
			w.Header().Set("Content-Length", fmt.Sprint(len(payload)))
			w.WriteHeader(gohttp.StatusOK)
			w.Write(payload)
			return
		}
		var start, end int64
		fmt.Sscanf(strings.TrimPrefix(rh, "bytes="), "%d-%d", &start, &end)
		w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", start, end, len(payload)))
		w.Header().Set("Content-Length", fmt.Sprint(end-start+1))
		w.WriteHeader(gohttp.StatusPartialContent)
		w.Write(payload[start : end+1])
	}))
}

// 3) 传输中途断开：写出 payload 的前一半后 panic 断连
func newBreakMidwayServer(t *testing.T, payload []byte) *httptest.Server {
	return httptest.NewServer(gohttp.HandlerFunc(func(w gohttp.ResponseWriter, r *gohttp.Request) {
		w.Header().Set("Accept-Ranges", "bytes")
		if rh := r.Header.Get(base.HttpHeaderRange); rh == "" {
			w.WriteHeader(gohttp.StatusOK)
			w.(gohttp.Flusher).Flush()
			w.Write(payload[:len(payload)/2])
			panic("midway break") // 连接被服务端撕掉
		}
		var start, end int64
		fmt.Sscanf(strings.TrimPrefix(rh, "bytes="), "%d-%d", &start, &end)
		w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", start, end, len(payload)))
		w.WriteHeader(gohttp.StatusPartialContent)
		w.(gohttp.Flusher).Flush()
		w.Write(payload[start : start+(end-start+1)/2]) // 每段都只给一半
		panic("midway break")
	}))
}

func TestLab_BadServers(t *testing.T) {
	payload := make([]byte, 4*1024*1024)
	for i := range payload {
		payload[i] = byte(i % 251)
	}
	cases := []struct {
		name string
		srv  func() *httptest.Server
		conn int
	}{
		{"ignore_range", func() *httptest.Server { return newIgnoreRangeServer(t, payload) }, 4},
		{"mixed_206_200", func() *httptest.Server { return newMixedServer(t, payload, 2) }, 4},
		{"break_midway", func() *httptest.Server { return newBreakMidwayServer(t, payload) }, 2},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			srv := c.srv()
			defer srv.Close()
			os.Remove(test.DownloadFile)
			t.Cleanup(func() { os.Remove(test.DownloadFile) })

			f := buildFetcher()
			f.Meta().Opts = &base.Options{
				Name: test.DownloadName, Path: test.Dir,
				Extra: &http.OptsExtra{Connections: c.conn},
			}
			if err := f.Resolve(&base.Request{URL: srv.URL + "/" + test.BuildName}); err != nil {
				t.Fatal(err)
			}
			defer f.Close()
			if err := f.Start(); err != nil {
				t.Fatal(err)
			}
			err := f.Wait() // 记录这里返回的 err —— 这就是该坏服务器的“任务事件结局”
			stats := f.Stats().Snapshot.(*http.Stats)
			t.Logf("wait_err=%v range_mode=%v connections=%d", err, f.meta.Res.Range, len(stats.Connections))
			for i, sc := range stats.Connections {
				t.Logf("  conn[%d] downloaded=%d retry=%d failed=%v", i, sc.Downloaded, sc.RetryTimes, sc.Failed)
			}
		})
	}
}
```

操作步骤与观察记录表：

1. **实践目标**：分别为三类坏服务器记录「任务结局（Wait 的 err）+ Range 模式终值 + 连接统计」，并与 4.3/4.5 节的源码结论互相印证。
2. **操作步骤**：将示例代码存为新测试文件后运行 `go test ./internal/protocol/http/ -run 'TestLab_BadServers' -v`；再分别改 `okBefore`（混杂阈值）与 `Connections`（1 对 4）重跑对比。
3. **需要观察的现象**：
   - 忽略 Range：任务应成功（降级为顺序模式），`range_mode=false`、连接数 1；
   - 206/200 混杂：取决于捣乱发生时是否已扩张到多连接——单连接期撞上会降级重启（慢但成功），扩张后撞上则 `err` 含 `server ignored HTTP range request`；
   - 中途断开：连接反复以短读失败重试，最终若凑齐字节则成功、否则以 incomplete/invalid 收场。
4. **预期结果**：三类行为均能对应到 4.3（降级条件）、4.1（终态 vs 可重试错误）、4.5（重试计数与豁免）的解释。混杂与断开场景的确切结局对时序敏感，**待本地验证**后填入你的记录表。
5. **事件序列扩展**（可选）：用 u1-l4 学过的 Boot API 写一个小下载器指向这些服务器（把服务器 URL 换成本地端口），在 `Listener` 里打印 `start/progress/error/done/finally` 的到达顺序——这会补齐 Fetcher 层看不到的 Downloader 层事件视角。

## 6. 本讲小结

- **downloadChunkOnce 是「一次请求 + 全套防护」**：状态码分发（206 验证 / 200 降级 / 其他分类重试）、读循环三重防护（长度上限、overlap 丢弃、写入前重读边界）、EOF 终审（短读判 incomplete 可重试，其余不符判 invalid 终态）。
- **三个哨兵错误定义了信任边界**：`errRangeRequestIgnored` 与 `errInvalidRangeResponse` 是终态 Range 完整性错误，重试无益；`errIncompleteResponse` 只是网络中断，可续传重试。
- **降级有严格的安全条件**：只有慢启动首连接（`conn.ID==0`）且尚无并行连接时才允许退化为顺序下载；多连接后服务器行为突变宁可任务失败也不冒拼坏文件的风险。降级后 `rangeReprobeEligible` 留下 If-Range 探测的复活通道。
- **openEnded chunk 解决「大小未知」**：首连接用 `bytes=N-` 开放式请求，从首个 206 的 Content-Range 揭示 total，在同一临界区完成 Truncate、发布大小、封闭 chunk，然后慢启动照常扩张。
- **恢复策略按连接逐条裁决**：已完成/403/重试耗尽的跳过，顺序模式无校验器的归零重启，有 If-Range 的保留前缀探测；5xx/429 豁免重试计数，403 与完整性错误立即终态，其余 HTTP 错误 3 次计数封顶。
- **完整性优先于完成**：`onDownloadComplete` 中终态 Range 错误压过「字节凑齐」，校验和失败则强制全量重下——一切设计都服从「不出损坏文件」这条底线。

## 7. 下一步学习建议

本讲补齐了 HTTP Fetcher 的容错面，下一讲 **u5-l4 完成判定、校验和与 HTTP OptsExtra** 收拢 HTTP 篇的最后一环：`checkCompletion`/`onDownloadComplete` 的收尾顺序、md5/sha1/sha256 校验失败后的全量重下路径（本讲 4.5.3 第 ③ 段已埋下伏笔），以及 `OptsExtra` 中 AutoTorrent/AutoExtract 如何把下载完成事件链接到 BT 与解压 subsystem。建议顺带阅读 [internal/protocol/http/fetcher.go:2287-2330](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2287-L2330)（`verifyChecksum`）作为预习。
