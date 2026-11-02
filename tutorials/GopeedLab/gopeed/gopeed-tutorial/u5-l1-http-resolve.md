# HTTP Resolve：元数据探测与预取

## 1. 本讲目标

在前面的单元里，我们已经把 `Fetcher` 接口契约（u3-l1）和 `Downloader.Resolve → Create → doStart` 主链路（u4-l2）走通了。本讲深入其中最重要的协议实现——HTTP，只聚焦一件事：

> **当你交给 Gopeed 一个 HTTP 地址后，`internal/protocol/http` 的 Fetcher 是如何用一个探测请求摸清「这是个什么文件、能不能分块下载」，并且顺手把这个请求的响应体变成第一批下载进度的。**

学完本讲你应当能够：

1. 说清 HTTP Fetcher 的 `Resolve` 为什么发的是**不带 Range 头的 GET**，而不是 HEAD；
2. 逐字段解释探测响应如何填充 `base.Resource`（`Name`、`Size`、`Range`、`Files[0].Ctime`），特别是**文件名的三级回退顺序**和 **`Range` 字段对多连接下载可行性的决定作用**；
3. 解释 `asyncPrefetch` 如何在「解析完成、用户还没确认下载」的空窗期偷偷预取数据；
4. 描述 `stopPrefetchAndCopyData` / `commitResolveTakeover` / `completeFromResolveFallback` 三条「免二次请求」的预取接管路径。

## 2. 前置知识

### 2.1 下载的两阶段回顾

Gopeed 把一次下载拆成两个阶段（u2-l1）：

- **解析（Resolve）**：输入 `base.Request`（一个 URL），输出 `base.Resource`（文件名、大小、是否支持分块、文件列表）；
- **传输（Start）**：按解析结果建连接、写文件、汇报进度。

这两阶段之间隔着用户交互：UI 拿到 `Resource` 后展示「这是个 2GB 的 zip，要下载吗？」，用户点确认才 `Create` 任务。HTTP Fetcher 的巧思全在「如何让解析阶段的那个 HTTP 请求不浪费」。

### 2.2 HTTP 状态码与 Range 三件套

| 头 / 状态码 | 方向 | 含义 |
|---|---|---|
| `Range: bytes=0-99` | 请求 | 「我只要第 0~99 字节」 |
| `206 Partial Content` | 响应 | 「给你部分内容」，必带 `Content-Range` |
| `Content-Range: bytes 0-99/1024` | 响应 | 「给的是 0~99 字节，全文共 1024 字节」 |
| `Accept-Ranges: bytes` | 响应 | 「本站支持按字节分段取」 |
| `200 OK` | 响应 | 「给你全文」，带 `Content-Length` |

多连接分块下载的前提是服务器认 `Range` 头：`Resource.Range = true` 才会走慢启动多连接路径，否则只能单连接顺序下载——这是本讲反复出现的分叉点。

### 2.3 Content-Disposition 与文件名

响应头 `Content-Disposition: attachment; filename="report.pdf"` 是服务器「钦定」文件名的方式。真实世界的服务器五花八门：RFC 5987 的 `filename*=UTF-8''%E6%B5%8B.zip`、GBK 乱码、HTML 实体转义（`&amp;`）……Gopeed 为此写了一整套解析与回退逻辑。

### 2.4 一次性 URL

很多站点的下载链接是**签名一次性链接**（signed URL）：第一次请求返回文件，第二次请求 404。对这类资源，「探测用一个请求、下载再发一个请求」的方案会直接失败——探测请求的响应体必须被保留下来接着用。这是理解本讲设计的钥匙。

### 2.5 ETag 与 If-Range（本讲只记录，u5-l3 再用）

`ETag` 是资源版本的指纹。`If-Range` 请求头携带它问服务器「文件还是这个版本吗？是就给我 206 分段，不是就给我 200 全文」。本讲的 `Resolve` 只负责把可用的校验器**记下来**（存入 `f.ifRange`），真正拿它做断点校验是 u5-l3 的故事。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [internal/protocol/http/fetcher.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go) | HTTP Fetcher 主体：`Resolve`、`asyncPrefetch`、`doStart`、预取接管，本讲主战场 |
| [internal/protocol/http/helper.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/helper.go) | 请求/客户端构建、文件名解析、Range 头格式化、If-Range 校验器提取 |
| [internal/protocol/http/config.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/config.go) | HTTP 协议私有配置（UserAgent、Connections、UseServerCtime） |
| [internal/protocol/http/fetcher_manager.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_manager.go) | 协议管理器：默认配置（Connections=16）、Store/Restore |
| [internal/httpclient/client.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/httpclient/client.go) | 通用 HTTP 客户端工厂：代理、TLS、浏览器指纹伪装回退 |
| [pkg/protocol/http/model.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/protocol/http/model.go) | 对外模型：`ReqExtra`（method/header/body）、`OptsExtra`（Connections 等） |
| [internal/protocol/http/resolve_range_test.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/resolve_range_test.go) | Resolve 的表驱动测试，本讲多个实践的素材 |
| [internal/test/httptest.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/test/httptest.go) | 项目自带的各类「坏服务器」模拟器（一次性 URL、忽略 Range 等） |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**① Resolve 探测请求 → ② Resource 字段填充 → ③ asyncPrefetch 预取 → ④ 预取数据接管**。它们正好覆盖一个探测请求从发出到「变成下载进度」的一生。

### 4.1 模块一：Resolve 探测请求——一次不带 Range 的 GET

#### 4.1.1 概念说明

要摸清一个 HTTP 资源的元数据，直觉方案是发一个 `HEAD` 请求——只拿响应头不要体。Gopeed 偏偏发**不带 Range 头的普通 GET**，原因有二：

1. **一次性 URL**：HEAD 拿到的元信息没有响应体可用，正式下载还得再发一次 GET，第二次请求就 404 了。GET 探测则可以把响应体留着直接用。
2. **预取**：GET 的响应体本身就是文件开头的真实数据，解析期间读它等于「提前开始下载」。

代价是必须小心管理这个响应体的生命周期：它比 `Resolve` 函数活得久，要配专门的取消函数和关闭时机。

此外，这个探测请求还兼任**测速探针**：从发起到收到响应的耗时被记录为 `maxConnTime`，作为后续下载连接「多快算卡死」的自适应超时基线。

#### 4.1.2 核心流程

```text
Resolve(req)
  ├─ ParseReqExtra：把 req.Extra 解析成 *fhttp.ReqExtra（method/header/body）
  ├─ buildRequest：构建【不带 Range】的 GET（若有 redirectURL 用重定向后的地址）
  ├─ buildClient：构建带 cookie jar、代理、TLS、浏览器伪装的 http.Client
  ├─ client.Do → 记录连接耗时到 maxConnTime
  ├─ 状态码守门：只接受 200 / 206，否则返回 RequestError 并置 stateError
  └─ （后续模块）解析头 → 填 Resource → 决定是否保留响应体、是否预取
```

#### 4.1.3 源码精读

**入口：解析 Extra 并进入 resolving 态**。[internal/protocol/http/fetcher.go:L419-L431](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L419-L431) 中，`Resolve` 先用泛型函数 `ParseReqExtra` 把 `any` 类型的 `req.Extra` 规整成 HTTP 的 `ReqExtra`（Method/Header/Body），随后构建请求——注意注释里点明的关键决策：**不带 Range 头**，这样响应体才能被复用于下载（对一次性 URL 至关重要）；且因为响应体比 `Resolve` 函数活得久，读超时需要自己的 cancel 函数。

**请求构建：优先重定向地址 + 默认 UA**。[internal/protocol/http/helper.go:L122-L172](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/helper.go#L122-L172) 的 `buildRequestWithURL` 做三件事：若存在 `redirectURL`（上次请求被 30x 跳转后的最终地址）则用它代替原始 URL；从 `ReqExtra` 取 Method/Header/Body（缺省 GET）；若用户没设 User-Agent，则填入协议配置里的默认 UA（Chrome 风格，定义在 [internal/protocol/http/config.go:L3-L4](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/config.go#L3-L4)，默认值注入见 [internal/protocol/http/fetcher_manager.go:L74-L79](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_manager.go#L74-L79)）。`ReqExtra` 的字段定义在 [pkg/protocol/http/model.go:L9-L13](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/protocol/http/model.go#L9-L13)。

**客户端构建：代理 + TLS + 浏览器伪装**。[internal/protocol/http/helper.go:L268-L293](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/helper.go#L268-L293) 的 `buildClientWithTimeout` 调用 `httpclient.NewClient`，把任务级代理（`f.ctl.GetProxy`，Controller 注入，见 u3-l3）、`InsecureSkipVerify` 证书选项、拨号/TLS 超时和 **ImpersonationAuto** 模式全部组装进去。[internal/httpclient/client.go:L99-L135](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/httpclient/client.go#L99-L135) 的 `NewClient` 按伪装模式选择传输层；auto 模式下的 [adaptiveTransport.RoundTrip（L210-L245）](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/httpclient/client.go#L210-L245) 先用原生 Go 传输层发请求，一旦命中 [shouldFallback（L289-L298）](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/httpclient/client.go#L289-L298) 判定的「403 或 Cloudflare challenge」，就按 UA 换用对应浏览器指纹（Chrome/Firefox/Safari）重试并缓存结论——这就是 u3-l3 提过的「自适应伪装」，探测请求正是它的第一个受益者。

**发请求与状态码守门**。[internal/protocol/http/fetcher.go:L442-L463](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L442-L463)：`client.Do` 前后记录连接耗时（`updateMaxConnTime` 只增不减）；随后初始化 `res := &base.Resource{Range: false, Files: []}`，并把非 200/206 的响应挡在门外——关闭响应体、取消请求、置 `stateError`、返回 `NewRequestError(code)`。状态码常量（`HttpCodeOK=200`、`HttpCodePartialContent=206`）与所有头名常量集中定义在 [pkg/base/constants.go:L15-L30](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/constants.go#L15-L30)。

#### 4.1.4 代码实践：跑一遍探测响应的表驱动测试

1. **实践目标**：亲眼看到 `Resolve` 对各种「奇怪响应」的分类处理，而不只看代码。
2. **操作步骤**：在仓库根目录执行：

   ```bash
   go test ./internal/protocol/http/ -run 'TestResolveResponseSize' -v
   ```

3. **需要观察的现象**：`-v` 输出里每个子测试一行结果，子测试名来自 [internal/protocol/http/resolve_range_test.go:L24-L36](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/resolve_range_test.go#L24-L36) 的表：`full`、`partial_prefix`、`partial_nonzero`、`unknown_total`、`malformed_range`、`chunked_partial`、`unknown_full` 等 13 个用例。
4. **预期结果**：全部 PASS。测试用 `httptest.NewServer` 直接 `WriteHeader(206)` 模拟「不发 Range 却收到 206」的站点，断言 `Resolve` 推导出的 `Res.Size` 与期望一致。
5. 若你本地未配置 Go 1.25+ 环境，此步「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把探测请求改成 HEAD，功能上最直接破坏的是本讲提到的哪个场景？
**答案**：一次性 URL。HEAD 只返回元信息、无响应体，正式下载必须再发一次 GET；而一次性链接第二次请求返回 404，下载必然失败。GET 探测则可保留响应体无缝接管。

**练习 2**：`buildRequestWithURL` 里的 `f.redirectURL` 是什么时候第一次被赋值的？
**答案**：在 `Resolve` 解析完响应后——[fetcher.go:L539](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L539) 用 `resp.Request.URL.String()`（Go 的 http.Client 自动跟随 30x 后的最终地址）写入。此后所有下载连接优先请求它，省去重复跳转。

**练习 3**：为什么 `Resolve` 的请求要配独立的 `reqCancel`，而不是复用 fetcher 的 `f.cancel`？
**答案**：因为探测响应体的生命周期超出 `Resolve` 函数——它要被预取或 `Start` 继续消费。读超时/取消必须绑定「这一个请求」，见 [fetcher.go:L429-L431](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L429-L431) 的注释与 `resolveRespCancel` 字段（[L305-L308](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L305-L308)）；而 `f.cancel` 属于下载阶段的 `doStart` 生命周期。

### 4.2 模块二：Resource 字段填充——Range、Size 与文件名三级回退

#### 4.2.1 概念说明

探测响应到手后，`Resolve` 要把它翻译成一个 `base.Resource`。四个字段的推导各有玄机：

- **`Range`**（bool）：服务器是否支持分块。它**决定多连接是否可行**——`true` 才走慢启动多连接，`false` 只能单连接顺序下载；
- **`Size`**（int64）：优先级是 206 的 `Content-Range` 总长 > 200 的 `Content-Length` > 用户提示值（`Opts.Size`）；同时区分「权威大小」与「显示用估计值」；
- **`Name`**：`Content-Disposition` > URL 路径末段 > 主机名，最后再按 `Content-Type` 补扩展名；
- **`Files[0].Ctime`**：服务器的 `Last-Modified` 时间，供下载完成后回写文件时间戳。

#### 4.2.2 核心流程

```text
拿到 200/206 响应后：
① Range 判定：Accept-Ranges: bytes 或 Content-Range 以 bytes 开头 → res.Range = true
② Size 推导：
   206 → 解析 Content-Range "bytes s-e/total" → Size = total，Range = true
         （并检查分段起点 s：s == 0 才允许把响应体当文件前缀复用）
   200 → Size = Content-Length（若存在）
③ 权威性标记：206 或 ContentLength >= 0 → known；否则 Size 只是显示值 sizeHint()
④ 记录 If-Range 校验器（强 ETag 或 距 Date ≥ 1s 的 Last-Modified）
⑤ 文件名：Content-Disposition → URL 路径 base（PathUnescape 解码）→ 主机名
         → 若无有效扩展名，按 Content-Type 补一个
⑥ 记录 redirectURL，决定是否保留响应体
```

#### 4.2.3 源码精读

**Range 判定**。[internal/protocol/http/fetcher.go:L465-L470](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L465-L470)：两个信号任一命中即 `res.Range = true`——响应头 `Accept-Ranges: bytes`，或 `Content-Range` 头以 `bytes` 开头（后者意味着服务器已经用 206 回应了）。这个布尔值稍后在 [startResolveDownload（L1002-L1032）](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1002-L1032) 成为分岔点：`Range=false` 时只建一条连接直接顺序消费保留的响应体；`Range=true` 时进入慢启动逐批扩张连接（u5-l2 专题）。

**206 与 200 的分叉**。[internal/protocol/http/fetcher.go:L473-L494](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L473-L494)：206 路径调用 `parseRangeResponse` 解析 `Content-Range`，拿到 `(start, end, total)` 后 `Size = total`、`Range = true`，并计算 `reuseResolveBody = start == 0`——注释点破原因：预取是「从 0 开始的连续前缀」，若服务器给的分段起点不在 0，这段数据没法当文件开头用，只能关掉响应体走正常 Range 路径。200 路径则简单取 `Content-Length`。`parseRangeResponse` 本体在 [L1668-L1695](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1668-L1695)，它严格校验格式：`bytes` 单位、`s-e/total` 结构、`end >= start`、`total > end`、Content-Length 与分段长度一致，任何一条不满足都归为 `errInvalidRangeResponse`（未知总长 `*` 也被拒绝——分段长度不能冒充资源大小）。

**大小权威性**。[internal/protocol/http/fetcher.go:L495-L501](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L495-L501)：`known` 为真的条件是 206 或 `resp.ContentLength >= 0`（Go 里 -1 表示无 Content-Length，如 chunked 传输）。`known=false` 时 `Size` 退化为 `sizeHint()`（用户在 `Opts.Size` 给的显示值），后续连接得边下边发现真实大小（openEnded chunk，u5-l3 详述）。`sizeKnown` 字段（[L327-L329](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L327-L329) 的注释）专门区分「权威长度」与「显示估计」，连 0 也可能是权威的。

**If-Range 校验器提取**。[internal/protocol/http/fetcher.go:L503-L511](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L503-L511) 解析 `Last-Modified` 存入 `Ctime`，并调用 [extractIfRangeValidator（internal/protocol/http/helper.go:L34-L54）](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/helper.go#L34-L54) 挑出「够强」的校验器存进 `f.ifRange`：有 ETag 时必须是强 ETag（带引号且字符合法，[L56-L68](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/helper.go#L56-L68)）；否则 `Last-Modified` 必须比响应 `Date` 早至少 1 秒——刚生成的文件没有稳定的修改时间，用它做 If-Range 会误判。

**文件名三级回退**。[internal/protocol/http/fetcher.go:L519-L533](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L519-L533)：

1. `Content-Disposition` 存在则用 [parseFilename（internal/protocol/http/helper.go:L300-L316）](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/helper.go#L300-L316) 解析——内部先试 RFC 5987 的 `filename*=`，再试标准 `mime.ParseMediaType` 的 `filename=`，最后手动兜底；解码链覆盖 HTML 实体、RFC 2047 编码字、URL 编码和 GBK（常见于中文站点）；
2. 否则取 URL 路径末段，并用 `url.PathUnescape` 解码（特意不用 QueryUnescape，`%2B` 应解码成 `+` 而非空格）；
3. 仍为空或是 `/`、`.` 时用主机名；
4. 最后 [appendFilenameExtension（helper.go:L322-L338）](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/helper.go#L322-L338) 检查：文件名没有像样的扩展名时，按 `Content-Type` 推导一个补上（某些图床的路径段是假扩展名）。

**收尾**。[internal/protocol/http/fetcher.go:L535-L552](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L535-L552)：`FileInfo` 入列、`f.meta.Res = res` 发布；`redirectURL` 记录跳转终地址；随后在 `resolveRespLock` 保护下决定响应体去留——`reuseResolveBody` 为真就把 `resp` 与 `reqCancel` 存入 `f.resolveResp`（**不关闭**，注释再次强调这对一次性 URL 至关重要），否则立刻关闭并标记 `prefetchDone`。

#### 4.2.4 代码实践：把测试表翻译成「响应 → 字段」矩阵

1. **实践目标**：把 4.2.2 的推导规则落到具体输入输出。
2. **操作步骤**：阅读 [internal/protocol/http/resolve_range_test.go:L17-L36](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/resolve_range_test.go#L17-L36) 的表驱动用例，手工填出下面矩阵（`invalid` 列表示 `Resolve` 应返回 `errInvalidRangeResponse`）：

   | 用例名 | 状态码 | Content-Length | Content-Range | 期望 Size | invalid |
   |---|---|---|---|---|---|
   | full | 200 | 801571015 | — | 801571015 | 否 |
   | partial_prefix | 206 | 472321339 | bytes 0-472321338/801571015 | 801571015 | 否 |
   | partial_nonzero | 206 | 100000 | bytes 100000-199999/801571015 | 801571015 | 否 |
   | unknown_total | 206 | 472321339 | bytes 0-472321338/* | 0 | 是 |
   | unknown_full | 200 | （无） | — | 0 | 否 |

3. **需要观察的现象**：`partial_nonzero` 虽然 Size 正确，但因 `start != 0`，`Resolve` 不会保留其响应体预取（对照 [fetcher.go:L483-L486](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L483-L486)）；`unknown_full` 是 chunked 200，`known=false`，Size 退化为 hint（0）。
4. **预期结果**：矩阵填完后与 `go test ./internal/protocol/http/ -run 'TestResolveResponseSize' -v` 的通过情况互相印证。
5. 纯阅读型实践，无需运行环境。

#### 4.2.5 小练习与答案

**练习 1**：一个服务器返回 200 且带 `Accept-Ranges: bytes` 与 `Content-Length: 1024`，`Resource` 会是什么样？后续能开多连接吗？
**答案**：`Range=true`、`Size=1024`、`known=true`。能开多连接——`startResolveDownload` 走慢启动分支，后续连接各自带 `Range: bytes=s-e` 请求分段。

**练习 2**：为什么 `parseRangeResponse` 拒绝 `bytes 0-9/*` 这种未知总长的 Content-Range？
**答案**：206 描述的是「一个分段」而非整个资源，分段长度（10 字节）不能冒充资源总长（[L1666-L1668](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1666-L1668) 的函数注释写明 Unknown totals are rejected）。把它当 Size 会导致进度百分比、完成判定全错。

**练习 3**：URL 为 `https://cdn.example.com/a%2Bb%20c`（无 Content-Disposition、Content-Type: text/plain），最终文件名是什么？
**答案**：走第二级回退取路径末段 `a%2Bb%20c`，经 `PathUnescape` 解码为 `a+b c`；它没有扩展名（`path.Ext("a+b c")` 为空），`appendFilenameExtension` 会按 `text/plain` 补成 `a+b c.txt`（具体扩展名取决于 Go 的 mime 表，`.txt`，待本地验证）。

### 4.3 模块三：asyncPrefetch 预取——解析阶段不浪费带宽

#### 4.3.1 概念说明

`Resolve` 返回后到用户确认下载前，有一段可能长达数秒的「空窗期」。对支持 Range 的资源，Gopeed 会在后台开一个协程**继续读那个探测响应的体**，把数据写进临时文件——这就是 `asyncPrefetch`。等用户点「下载」，这些字节已经落盘，直接拼接进目标文件，等于下载提前开始了。

预取的启动条件很克制：只有 `res.Range == true` **且** `reuseResolveBody == true`（响应是文件从 0 开始的连续前缀）才启动。不支持 Range 的资源不预取——因为它的响应体将在 `Start` 时被那条唯一连接直接顺序消费，无需暂存。

#### 4.3.2 核心流程

```text
Resolve 成功收尾：
  if res.Range && reuseResolveBody:
      prefetchStopCh = make(...)        // 停止信号
      resolveFallback = true            // 「原始响应仍可作为兜底」标记
      go asyncPrefetch()

asyncPrefetch 协程：
  取出 f.resolveResp（探测响应）
  在 TempDir（空则系统临时目录）创建 gopeed-prefetch-* 临时文件
  ctl.TempFiles.Track 登记（Downloader 关闭时兜底清理）
  循环：每读 32KB → 写临时文件 → prefetchSize += n
        每轮先查 prefetchStopCh（Start/Pause 来叫停）
  EOF 或出错 → prefetchDone = true，延迟关闭响应体
```

#### 4.3.3 源码精读

**启动点**。[internal/protocol/http/fetcher.go:L554-L572](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L554-L572)：`Resolve` 置 `stateResolved`、`close(f.resolvedCh)`（唤醒可能已在等待的 `doStart`，即 u4-l2 讲过的「延迟解析」路径），然后才判断 `res.Range && reuseResolveBody` 并 `go f.asyncPrefetch()`。同一片段还处理 `startPending`：如果 `Start` 抢在解析完成前被调用，这里补发 `doStart`。

**预取协程本体**。[internal/protocol/http/fetcher.go:L577-L655](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L577-L655)。三个细节值得咀嚼：

- [L585-L599](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L585-L599)：先取出响应，`defer` 里无论正常结束、出错还是被叫停都关闭响应体——注释解释了为什么：哪怕关闭被拒绝，也不能让服务器傻等一个没人读的连接。
- [L601-L624](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L601-L624)：临时文件建在 `ctl.TempDir`（Controller 注入的下载器临时目录，为空则落到系统临时目录），命名前缀 `gopeed-prefetch-*`，并通过 `ctl.TempFiles.Track` 登记——这正是 u3-l3 讲过的 tempfiles 作用域机制：万一进程异常，Downloader 关闭时也能兜底删除。
- [L626-L654](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L626-L654)：32KB 缓冲的读写循环，每轮先 `select` 检查 `prefetchStopCh`（非阻塞），`prefetchSize` 用原子加累计字节数；EOF 视为正常完成。

相关字段集中在 [fetcher.go:L310-L317](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L310-L317)：`prefetchFile/Path/Size/Done/Err/StopCh`，以及 `resolveFallback`——「预取仍存活、原始响应仍可兜底」的标记，它是模块四的主角。

**停止与物化**。[stopPrefetchAndCopyData（fetcher.go:L657-L703）](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L657-L703)：幂等地 `close(prefetchStopCh)`（select 防重复关闭），最多轮询等 10 秒（1000 次 × 10ms）预取协程退出；若一个字节都没取到就直接清理返回 0；否则把临时文件 `Seek(0)` 后逐块 `WriteAt` 到**目标文件偏移 0**——预取数据从「临时文件」物化为「正式下载文件的开头」。随后的 [cleanupPrefetchFile（L705-L724）](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L705-L724) 关文件、经 `TempFiles.Remove` 销账、`prefetchSize` 归零（注释说明：字节计数只在临时文件还活着时可复用，Pause 可能在首次 Start 前丢弃该文件，留着计数会让下次 Start 跳过从未复制过的字节）、`resolveFallback` 置 false。

#### 4.3.4 代码实践：追踪预取状态的全部读写点

1. **实践目标**：建立「谁在什么时机读写预取状态」的完整地图。
2. **操作步骤**：在仓库根目录执行：

   ```bash
   grep -n "prefetch\|resolveFallback\|resolveDataPos" internal/protocol/http/fetcher.go | grep -v "^.*//"
   ```

   然后把命中行按函数分组（asyncPrefetch / stopPrefetchAndCopyData / cleanupPrefetchFile / doStart / commitResolveTakeover / completeFromResolveFallback / Pause / expandConnections）。
3. **需要观察的现象**：`prefetchSize` 只在 `asyncPrefetch`（Add）、`stopPrefetchAndCopyData`（Load）、`cleanupPrefetchFile`（Store(0)）三处变动；`resolveFallback` 的所有翻转都是 `CompareAndSwap` 或先置 false 再操作，保证只有一方赢得「接管权」。
4. **预期结果**：得到一张「字段 × 函数」表，能看出预取生命周期被 `prefetchStopCh`（叫停）、`prefetchDone`（完成标志）、`resolveFallback`（所有权）三根线串起来。
5. 纯阅读型实践，无需运行环境。

#### 4.3.5 小练习与答案

**练习 1**：为什么预取要写临时文件，而不是直接写目标文件？
**答案**：因为 `Resolve` 阶段还不知道最终下载目录与文件名（目录决策、查重改名发生在 `doStart` 的 `prepareTask`，见 u4-l2）。目标文件路径要到 `Start` 才确定；且查重改名依赖「磁盘上尚无该文件」，提前写目标文件会破坏 `CheckDuplicateAndRename` 的判定。

**练习 2**：预取协程把响应体读到 EOF 之后，`resolveFallback` 还是 true 吗？这意味着什么？
**答案**：仍是 true（只有 `cleanupPrefetchFile`、`commitResolveTakeover`、`completeFromResolveFallback` 等接管路径才会翻 false）。这意味着即使预取已读完整个文件，首个 Range 连接失败时仍可走 `completeFromResolveFallback`，用已预取的完整数据直接判定任务完成——原始响应始终是兜底权威。

**练习 3**：`TempFiles.Track` 登记的临时文件，如果用户中途删除了任务，谁来删它？
**答案**：`cleanupPrefetchFile` 经 `ctl.TempFiles.Remove` 销账删除；若这条路径没走到（异常路径），tempfiles 作用域的 `Close` 会在作用域结束（任务删除/Downloader 关闭）时兜底清理——这正是 u3-l3 讲的 Track/Remove/Close 三段式。

### 4.4 模块四：预取数据接管——免二次请求的三条路径

#### 4.4.1 概念说明

`Start` 被调用后，预取数据必须无缝「变成」下载进度，且任何情况下都不能让同一个字节被当成两种来源。Gopeed 用一个 `resolveFallback` 原子布尔做**所有权仲裁**：谁把它从 true 换成 false，谁就负责处置预取数据。由此产生几条路径：

- **路径 A（小文件）**：`Start` 时预取已覆盖全文件 → 不建任何连接，直接校验、判 done；
- **路径 B（正常接管）**：首个 Range 连接以预取偏移为起点；其 206 响应**验证通过后**才停预取，重叠字节丢弃；
- **路径 C（Range 失灵兜底）**：首个 Range 请求失败或服务器拒绝分段 → 放弃分段方案，等原始响应（预取流）读完，整任务转为单连接完成；
- **路径 D（暂停抢先）**：`Pause` 在首次接管前发生 → 立即物化预取前缀并推进连接起点，保证断点安全。

#### 4.4.2 核心流程

```text
doStart（Range 资源分支）:
  resolveComplete = 已知大小 && 预取已完 && 无错 && 预取 >= 总大小
  if resolveFallback && !resolveComplete:
      只记账 prefetchedBytes（预取继续跑，等首个 Range 响应接管）
  else:
      prefetchedBytes = stopPrefetchAndCopyData()   // 停止并物化
  resolveDataPos = prefetchedBytes

downloadLoop → expandConnections 首批:
  if 预取 >= 总大小: 校验 checksum → done（路径 A）
  else: 首连接 chunk 从 prefetched 开始（路径 B 起点）

首连接收到 206 且校验通过 → commitResolveTakeover:
  停预取并物化 → overlap = prefetched - requestedStart → 丢弃响应开头 overlap 字节

首连接失败/被拒（403、5xx 豁免码等）→ completeFromResolveFallback:
  等预取读完 → 校验大小 → 整任务标记单连接完成（路径 C）

Pause 在首次接管前 → 物化前缀并推进 conn.Chunk.Begin（路径 D）
```

#### 4.4.3 源码精读

**doStart 的接管决策**。[internal/protocol/http/fetcher.go:L871-L893](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L871-L893)：目标文件打开后（`Touch` 预分配，u3-l3），先算 `resolveComplete`——四个条件分别是大小已知、预取协程已结束、无错误、字节数达标。若 `resolveFallback` 仍为 true 且预取未完，**故意不停预取**，只把当前 `prefetchSize` 记到 `prefetchedBytes`，注释点破动机：Range 请求要从一个稳定快照起步，接管成功时会丢弃预取多跑的重叠部分；反之（预取已完或兜底已失效）立即 `stopPrefetchAndCopyData` 物化。随后 `resolveDataPos` 记录「文件前缀已有多少字节」。

**首批连接从预取偏移起步**。[internal/protocol/http/fetcher.go:L1049-L1099](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1049-L1099)：首批（尚无连接）时读取 `resolveDataPos`。路径 A 在 [L1054-L1075](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1054-L1075)：`totalSize > 0 && prefetched >= totalSize` 即预取已拿全——关文件、验 checksum、置 done、发 `doneCh`，全程零下载连接（小文件在用户点确认前可能已经下完了！）。路径 B 在 [L1077-L1090](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1077-L1090)：首连接的 chunk 起点设为 `prefetched`（大小未知时用 openEnded chunk 让它边下边发现总长），`conn.Downloaded = prefetched` 使进度条瞬间包含预取字节。

**验证后的正式接管**。[commitResolveTakeover（internal/protocol/http/fetcher.go:L1599-L1619）](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1599-L1619)：只有 0 号连接能执行，且用 `resolveFallback.CompareAndSwap(true, false)` 保证恰好一次。它调用 `stopPrefetchAndCopyData` 停预取并物化，然后做关键的**重叠计算**：

```go
overlap := prefetched - requestedStart
conn.Chunk.Downloaded = overlap   // 预取跑过头的那段，从 Range 响应体开头丢弃
```

原理：Range 请求发出时的起点是快照 `requestedStart`，而预取协程在此期间还在前进，物化时可能已到更靠后的 `prefetched`。多出来的 `overlap` 字节已经在目标文件里了，Range 响应体开头同样这截就要丢掉（丢弃逻辑在 [downloadChunkOnce 的 responseBytesToDiscard，L1430-L1434](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1430-L1434) 接住返回值）。若 `prefetched < requestedStart` 或超出总长则判为非法，直接报错。函数注释把这层意图写得很清楚：**只在首个 Range 响应验证通过之后才停掉存活的 Resolve 下载**。

**Range 失灵时的兜底**。[completeFromResolveFallback（internal/protocol/http/fetcher.go:L1625-L1664）](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1625-L1664)：当首个 Range 请求失败（且错误码不在 5xx/429 等豁免名单，判断见 [runConnection 内 L1235-L1239](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1235-L1239)），同样用 CAS 赢得所有权，然后：轮询等预取读完（每 10ms，连接被取消则放弃）；校验 `prefetchErr` 与字节数；物化；最后把任务改写成「一条已完成的总连接」——`conn.Chunk = newChunk(0, prefetched-1)`、`Res.Range = false`、`Completed = true`。也就是说：服务器不配合分段也没关系，原始响应从头读到尾，任务照样完成。大小未知时还顺带用实际字节数把资源大小定下来（[L1648-L1651](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1648-L1651)）。

**暂停抢先物化**。[internal/protocol/http/fetcher.go:L2466-L2490](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2466-L2490)：`Pause` 若发现预取临时文件还在（首个 Range 响应尚未验证），先 `resolveFallback.Store(false)` 再物化前缀，并把首连接的 `Chunk.Begin` 推进到最终复制偏移——注释解释了为什么：首批连接是按更早的预取快照分配的，必须校正起点，落盘的续传状态才安全（否则恢复后会重下或跳段）。

#### 4.4.4 代码实践：跟踪一次「起点非 0」的下载

1. **实践目标**：验证 `reuseResolveBody = start == 0` 分支与正常预取分支的行为差异。
2. **操作步骤**：阅读并运行 [TestDownloadPartialResolve（internal/protocol/http/resolve_range_test.go:L76-L120）](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/resolve_range_test.go#L76-L120)：

   ```bash
   go test ./internal/protocol/http/ -run 'TestDownloadPartialResolve' -v
   ```

   该测试的服务器**无论请求是否带 Range 都回 206**，且 `Content-Range` 的起点跟随请求的 Range 头（无 Range 时为 `start`）。
3. **需要观察的现象**：`start=0` 与 `start=100000` 两组子测试都通过、文件内容完整。对照源码分析：`start=0` 时探测响应被保留并可预取；`start=100000` 时 `reuseResolveBody=false`，`Resolve` 在 [L547-L551](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L547-L551) 立即关闭响应体并置 `prefetchDone`，下载完全依赖后续带 `Range: bytes=0-...` 的请求（无预取）。
4. **预期结果**：4 个子测试（2 起点 × 2 连接数）全 PASS；能口头解释两种起点下数据来源的差异。
5. 若本地没有 Go 环境，「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：路径 A（预取覆盖全文件）成立时，`expandConnections` 里为什么还要先关文件再发 `doneCh`？
**答案**：[L1058-L1073](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1058-L1073)：`verifyChecksum` 要重新打开文件读哈希，且 `Wait()` 返回后调用方（watch 循环）可能立刻操作文件；先释放句柄避免占用与竞态，这也是 `onDownloadComplete` 的同一约定。

**练习 2**：`commitResolveTakeover` 中若 `prefetched < requestedStart` 会怎样？为什么这算错误？
**答案**：返回 `resolve prefetch advanced to invalid offset` 错误（[L1605-L1607](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1605-L1607)）。因为物化的前缀比 Range 请求的起点短，意味着目标文件在 `[prefetched, requestedStart)` 区间没有数据来源——这段既不在预取里，也不会出现在从 `requestedStart` 开始的 Range 响应里，文件必然有洞，只能报错重试而不是静默产出坏文件。

**练习 3**：`completeFromResolveFallback` 为什么要把 `f.meta.Res.Range` 改成 false？
**答案**：此路径的结论是「这个源实际上无法用分段可靠取数」（首个 Range 请求失败），任务已改为单条总连接完成。把 `Range` 置 false 让完成判定、Stats、以及 Store 落盘的状态（fetcherData 会保存 Range 快照，见 [fetcher_manager.go:L99-L110](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_manager.go#L99-L110)）都按顺序下载语义处理，恢复后不会再次尝试多连接。

## 5. 综合实践

把规格里的任务落地：**用本地假服务器分别制造 200、206、重定向、带 Content-Disposition 四类响应，亲眼看着 `Resource` 各字段被填充，并解释预取如何免二次请求**。

### 5.1 实践目标

- 用公开 API（`pkg/download`）在进程内完成 Resolve，打印四类响应推导出的 `Resource`；
- 在临时目录里抓到 `gopeed-prefetch-*` 预取文件，验证「解析阶段就在下载」。

### 5.2 操作步骤

1. 在仓库**外面**新建一个实验目录（避免污染仓库），例如 `/tmp/http-resolve-lab`，然后：

   ```bash
   cd /tmp/http-resolve-lab
   go mod init httpresolvelab
   # go.mod 中追加 replace，指向本地仓库
   go mod edit -replace github.com/GopeedLab/gopeed=<你的 gopeed 仓库绝对路径>
   go get github.com/GopeedLab/gopeed@latest
   ```

2. 创建 `main.go`（**示例代码**，非项目原有文件）：

   ```go
   package main

   import (
       "fmt"
       "net/http"
       "net/http/httptest"
       "time"

       "github.com/GopeedLab/gopeed/pkg/base"
       "github.com/GopeedLab/gopeed/pkg/download"
   )

   func main() {
       payload := make([]byte, 1024*1024) // 1MB
       srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
           switch r.URL.Path {
           case "/plain200": // 200 + Content-Length，无 Accept-Ranges
               w.Header().Set("Content-Length", fmt.Sprint(len(payload)))
               w.WriteHeader(200)
               w.Write(payload)
           case "/partial206": // 不发 Range 也回 206（Content-Range 起点 0）
               w.Header().Set("Content-Length", fmt.Sprint(len(payload)))
               w.Header().Set("Content-Range", fmt.Sprintf("bytes 0-%d/%d", len(payload)-1, len(payload)))
               w.WriteHeader(206)
               w.Write(payload)
           case "/redirect": // 302 到 plain200
               http.Redirect(w, r, "/plain200", http.StatusFound)
           case "/disposition": // 钦定文件名 + 声明支持 Range
               w.Header().Set("Content-Disposition", `attachment; filename="年报.pdf"`)
               w.Header().Set("Accept-Ranges", "bytes")
               w.Header().Set("Content-Length", fmt.Sprint(len(payload)))
               w.WriteHeader(200)
               w.Write(payload)
           }
       }))
       defer srv.Close()

       d := download.NewDownloader(nil) // 内存存储 + 默认五协议
       if err := d.Setup(); err != nil {
           panic(err)
       }

       for _, p := range []string{"/plain200", "/partial206", "/redirect", "/disposition"} {
           rr, err := d.Resolve(&base.Request{URL: srv.URL + p})
           if err != nil {
               fmt.Printf("%-14s resolve err: %v\n", p, err)
               continue
           }
           res := rr.Res
           fmt.Printf("%-14s name=%q size=%d range=%v ctime=%v files=%d\n",
               p, res.Name, res.Size, res.Range,
               res.Files[0].Ctime != nil, len(res.Files))
       }

       // 给后台预取协程一点时间写盘，然后去临时目录找预取文件
       time.Sleep(2 * time.Second)
       fmt.Println("提示：执行 ls /tmp/gopeed-prefetch-* 或在下载器临时目录中查找预取文件")
   }
   ```

3. 运行 `go run .`。

### 5.3 需要观察的现象

1. 四行输出中 `Resource` 字段的差异（见 5.4 的预期表）；
2. `Resolve` 返回后进程并未退出时，预取协程仍在后台读取 `/disposition` 与 `/partial206` 的响应体——在系统临时目录（或下载器临时目录）里能找到 `gopeed-prefetch-*` 前缀的临时文件，且其大小在增长；
3. 对照源码解释：探测请求（不带 Range 的 GET）的响应体没有被丢弃，而是被 `asyncPrefetch` 持续消费写入临时文件；将来 `Start` 时经 `stopPrefetchAndCopyData`/`commitResolveTakeover` 物化到目标文件开头，首批连接从预取偏移起步——**同一个字节只被请求一次，没有任何「再发一次 GET 重头下」**。

### 5.4 预期结果

| 路径 | name | size | range | 说明 |
|---|---|---|---|---|
| `/plain200` | `plain200` | 1048576 | false | 无 Disposition、无 Accept-Ranges → 文件名回退到 URL 末段 |
| `/partial206` | `partial206` | 1048576 | true | 206 + Content-Range（起点 0）→ Range=true，Size 取 total |
| `/redirect` | `plain200` | 1048576 | false | 文件名取**跳转后**地址的末段；redirectURL 记录终地址 |
| `/disposition` | `年报.pdf` | 1048576 | true | Content-Disposition 钦定文件名；Accept-Ranges 开启分块 |

具体打印值以本地运行为准（「待本地验证」）：例如 `/plain200` 的名字取决于路径末段与扩展名补全规则，`Ctime` 因测试服务器未发 `Last-Modified` 而为 nil。

### 5.5 思考题（选做）

如果把 `/disposition` 的 handler 换成项目自带的 [StartTestOneTimeServer（internal/test/httptest.go:L313-L336）](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/test/httptest.go#L313-L336)（第一次请求后一律 404），本讲的哪条设计保证了下载仍能完成？（提示：响应体被保留 + `runConnectionWithResolveResp`，见 [fetcher.go:L1837-L1863](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1837-L1863) 的注释。）

## 6. 本讲小结

- HTTP `Resolve` 发的是**不带 Range 的 GET**：一次请求同时完成元数据探测与「可复用的响应体」获取，这是一次性 URL 能下载成功的前提；连接耗时还被留作下载阶段的自适应超时基线。
- `Resource` 填充规则：`Range` 由 `Accept-Ranges`/`Content-Range` 判定并**决定多连接是否可行**；`Size` 按「206 总长 > Content-Length > 用户 hint」回退，并用 `sizeKnown` 区分权威值与显示值；文件名按「Content-Disposition → URL 末段（PathUnescape）→ 主机名」三级回退再补扩展名。
- `asyncPrefetch` 只对「支持 Range 且响应是 0 起点连续前缀」的资源启动：把探测响应体持续写入 `gopeed-prefetch-*` 临时文件（tempfiles 作用域管理），解析的空窗期变成下载的提前量。
- 预取接管由 `resolveFallback` 的原子 CAS 仲裁，四条路径全覆盖：小文件预取即完成（零连接 done）、首个 206 验证通过后 `commitResolveTakeover`（丢弃 overlap 重叠）、Range 失灵时 `completeFromResolveFallback` 兜底单连接完成、`Pause` 抢先时物化前缀保证续传安全。

## 7. 下一步学习建议

本讲只讲到「首批连接起步」，下一讲 **u5-l2《多连接分块下载：chunk、connection 与慢启动》** 将展开 `expandConnections` 之后的世界：chunk 的切分与偷取（`helpOtherConnection`）、慢启动控制器的批次扩张算法。之后 **u5-l3《Range 校验、断点续传与降级策略》** 会回收本讲埋下的两颗种子——`ifRange` 校验器的实际使用与「服务器忽略 Range 回 200」的降级路径（`fallbackToSequentialDownload`）。如果你对 `f.ifRange` 的强 ETag 判定意犹未尽，可以先读 [internal/protocol/http/helper.go:L34-L68](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/helper.go#L34-L68) 配合 RFC 9110 的 If-Range 一节。
