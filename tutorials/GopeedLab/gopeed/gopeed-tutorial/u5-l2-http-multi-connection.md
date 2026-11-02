# 多连接分块下载：chunk、connection 与慢启动

## 1. 本讲目标

上一讲（u5-l1）我们搞清了 HTTP Fetcher 的 `Resolve`：一次不带 Range 的 GET 摸清元数据、顺手预取数据。本讲进入 HTTP 实现的核心发动机——**多连接分块下载**。只聚焦一个问题：

> **当服务器支持 Range 时，Gopeed 是如何把一个文件切成若干区间、由少到多地建立连接、并在连接之间动态「偷工作」把带宽吃满的？**

学完本讲你应当能够：

1. 解释 `chunk` 的 `Begin`/`End`/`Downloaded`/`openEnded` 语义，以及「切一半」分裂算法为什么天然保证不重不漏；
2. 读懂 `connection` 的五态状态机与三种角色，说清每个状态由谁写入；
3. 走通 `downloadLoop` 的调度循环：`expansionCh` 信号驱动 `expandConnections` 逐批建连，直到 `stateSteady`；
4. 手推 `slowStartController` 的批次增长序列（1 → 2 → 4 → 16 → …平方增长、按上限截断），以及连接失败时批次如何「结算」；
5. 描述 `helpOtherConnection` 运行时工作窃取的判定条件（慢于 3 秒、剩余 ≥ 512KB）与「偷走后一半」的切分不变量。

## 2. 前置知识

### 2.1 为什么多连接能提速

单个 TCP 连接的吞吐受三个因素制约：TCP 拥塞窗口的增长速度、链路 RTT、以及**服务器对单连接的限速**（很多 CDN 单连接只给 2~5 MB/s）。多连接下载把文件按字节区间切开，每条连接独立跑自己的拥塞窗口，从而绕开单连接瓶颈。代价是：连接不是越多越好——建连有成本，服务器可能限连接数（返回 403），所以要「由少到多试探着加」。

### 2.2 Range 请求回顾（u5-l1）

| 要素 | 含义 |
|---|---|
| `Range: bytes=100-199` | 请求第 100~199 字节 |
| `206 Partial Content` + `Content-Range: bytes 100-199/1024` | 服务器确认给了这段，全文共 1024 字节 |
| `base.Resource.Range = true` | Gopeed 判定该资源可分块，才会走本讲的慢启动多连接路径 |

`Resource.Range = false` 时走的是另一条路：单连接顺序消费 Resolve 响应体（u5-l1 讲过的 `runConnectionWithResolveResp`），本讲只在分叉处提一句。

### 2.3 TCP 慢启动的类比

TCP 慢启动（slow start）指拥塞窗口从一个 MSS 开始，每收到一个 ACK 窗口加一，于是每个 RTT 窗口翻倍：1、2、4、8……Gopeed 把这个思想搬到了「连接数」上：**先建 1 条连接确认服务器肯配合，成功后下一批建 2 条，再成功建 4 条……一旦某批出现失败就停止增长**。这样对正常服务器快速爬到满连接，对挑剔的服务器（限连接、忽略 Range）不会一上来就打出一片失败请求。

### 2.4 工作窃取（work stealing）

分块下载的经典尾部难题：4 条连接各拿 1/4，若某条连接特别慢（远端冷、丢包多），其他连接全部完成后还得等它一个人跑完最后 1/4。解法是工作窃取：**先做完自己活儿的连接，去找「预计还要跑很久」的连接，偷走它剩余工作的一半**。Gopeed 的判定阈值定义在文件头部常量里：剩余时间超过 3 秒、剩余量超过 512KB 才值得偷。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [internal/protocol/http/fetcher.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go) | 本讲唯一主战场：chunk、connection、slowStartController、downloadLoop、expandConnections、helpOtherConnection 全在这里 |
| [internal/protocol/http/helper.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/helper.go) | 失败分类：哪些状态码计入失败次数（`shouldCountHTTPFailure`）、哪些豁免无限重试（5xx/429/408…） |
| [internal/protocol/http/fetcher_manager.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_manager.go) | 协议默认配置 `Connections: 16`、连接表快照 `Store` |
| [pkg/protocol/http/model.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/protocol/http/model.go) | 对外模型：`OptsExtra.Connections`（用户配置的连接数上限）、`StatsConnection`（UI 看到的每连接统计） |
| [internal/protocol/http/fetcher_test.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_test.go) | `TestFetcher_SlowStartExpansion`：验证最终连接数恰好等于配置值，本讲实践的样板 |
| [internal/test/httptest.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/test/httptest.go) | 测试服务器家族：低速服务器（按字节延迟）、连接数限制服务器（403）等 |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：**① chunk 区间模型 → ② connection 状态机 → ③ downloadLoop 与 expandConnections → ④ slowStartController 慢启动控制器 → ⑤ helpOtherConnection 运行时互助**。它们正好构成一条时间线：先把文件切成区间（①），给每条连接装上状态（②），由调度循环逐批拉起连接（③④），下载过程中再动态平衡（⑤）。

### 4.1 模块一：chunk 区间模型

#### 4.1.1 概念说明

`chunk` 是「一段连续字节区间」的账本：这段从哪开始（`Begin`）、到哪结束（`End`）、已经写入多少（`Downloaded`）。任何一个时刻，一个 HTTP 任务的全部活跃 chunk **不重不漏地覆盖文件中尚未完成的字节**——这是整个多连接体系的核心不变量，切分（expandConnections）与窃取（helpOtherConnection）都必须维持它。

有一个特殊形态：**开放式 chunk（open-ended）**。当资源声明支持 Range 但总大小未知（Resolve 阶段拿不到权威长度）时，第一条连接的 `End` 是 `nil`，它用一个 `Range: bytes=N-` 这样的请求边下边等服务器在 `Content-Range` 里告知总量，拿到后调用 `setEnd` 把区间封闭，后续才能继续切分。

#### 4.1.2 核心流程

```text
chunk { Begin, End *int64, Downloaded }
  ├─ remain()    = End - Begin + 1 - Downloaded   // 还剩多少字节没下
  ├─ openEnded() = (End == nil)                    // 总大小尚未揭晓
  ├─ setEnd(e)   把 nil End 封闭为 e
  ├─ newChunk(b, e)          封闭区间
  └─ newOpenEndedChunk(b)    开放区间
```

两个关键派生量：

- 剩余量：\[ \mathrm{remain} = \mathrm{End} - \mathrm{Begin} + 1 - \mathrm{Downloaded} \]
- 当前写入位置（即续传时的 Range 起点）：\[ \mathrm{pos} = \mathrm{Begin} + \mathrm{Downloaded} \]

#### 4.1.3 源码精读

**chunk 结构与方法**。[internal/protocol/http/fetcher.go:L100-L129](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L100-L129)：`End` 是 `*int64` 而非 `int64`，注释点明「直到某个开放区间响应揭晓总大小之前都是 nil」；`remain()` 直接按闭区间长度计算。

**开放式区间的封闭**。下载主循环里，当一条 open-ended 连接收到第一个 206 响应时，[resolveOpenEndedRange（L1712-L1736）](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1712-L1736) 会先 `file.Truncate(totalSize)` 预分配、发布权威大小，再 `conn.Chunk.setEnd(totalSize - 1)` 封闭区间——注释强调必须**先发布大小、后发响应就绪信号**，否则慢启动可能在大小未知时就扩张。这块的响应校验细节留给 u5-l3。

**切分不变量**。无论 expandConnections 还是 helpOtherConnection，切分点都取「受害者当前剩余量的一半」：

\[ \mathrm{split} = \mathrm{End}_{\text{old}} - \left\lfloor \frac{\mathrm{remain}_{\text{old}}}{2} \right\rfloor \]

新连接拿走 \([\mathrm{split}+1, \mathrm{End}_{\text{old}}]\)，受害者收缩为 \([\mathrm{Begin}_{\text{old}}, \mathrm{split}]\)。由于受害者已写位置 \(\mathrm{pos} = \mathrm{End}_{\text{old}} - \mathrm{remain}_{\text{old}} + 1\)，而切分仅当 \(\mathrm{remain}_{\text{old}} \ge 1\,\mathrm{MB}\)（见 4.3/4.5 的门槛）才发生，所以：

\[ \mathrm{split} - \mathrm{pos} = \left\lfloor \frac{\mathrm{remain}_{\text{old}}}{2} \right\rfloor - 1 \ge 512\,\mathrm{KB} - 1 > 0 \]

即**切分点永远在受害者已写位置的前方**——受害者还没写到的字节才被偷走，因此既不会重复写（无重叠），也不会漏写（无空洞）。这个不等式是理解「并发切分为什么安全」的钥匙。

#### 4.1.4 代码实践

**实践目标**：用最小程序验证切分算法的不变量。

1. 新建一个独立目录下的 `main.go`（**示例代码**，非项目源码）：

```go
package main

import "fmt"

type chunk struct {
	Begin, End, Downloaded int64
}

func (c *chunk) remain() int64 { return c.End - c.Begin + 1 - c.Downloaded }

func main() {
	// 模拟受害者：1GB 区间已下 100MB
	victim := &chunk{Begin: 0, End: 1<<30 - 1, Downloaded: 100 << 20}
	oldEnd := victim.End
	split := oldEnd - victim.remain()/2
	thief := &chunk{Begin: split + 1, End: oldEnd}      // 偷后一半
	victim.End = split                                   // 受害者收缩
	pos := victim.Begin + victim.Downloaded
	fmt.Printf("受害者: [%d,%d] 已写到 %d\n", victim.Begin, victim.End, pos)
	fmt.Printf("窃取者: [%d,%d]\n", thief.Begin, thief.End)
	fmt.Printf("切分点在已写位置前方: %v（差 %d 字节）\n", split > pos, split-pos)
	fmt.Printf("两段剩余之和 = %d（等于切分前剩余 %d）\n",
		victim.remain()+thief.remain(), 1<<30-1-100<<20+1-(100<<20)+100<<20-0)
}
```

2. `go run main.go` 运行。

**需要观察的现象**：`split > pos` 为 true 且差值约 449MB（剩余 924MB 的一半减一）；两段剩余量之和保持不变。

**预期结果**：切分只搬运「未写」的字节，账本总量守恒。若把 `Downloaded` 改大到接近 `End`，差值会小于 0——这正是代码里「剩余量不足 1MB 不切分」门槛存在的原因。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `End` 用 `*int64` 而不是给个 0 值？
**答案**：0 是合法的文件偏移（空文件时 `End = -1`，首字节时 `End = 0`），无法与「未知」区分；`nil` 指针是明确的第三态「总大小尚未揭晓」，`openEnded()` 一个判空就够。

**练习 2**：`chunk.Downloaded` 与 `connection.Downloaded` 有何区别？
**答案**：前者是「当前区间内已写字节」，用于计算 remain 与续传起点，切分/窃取时会被重置；后者是「这条连接一生累计写入」，用于进度统计（`Progress()` 汇总的就是它），切分后不清零。见 [internal/protocol/http/fetcher.go:L1551-L1553](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1551-L1553)——两者在同一把锁下同步累加。

**练习 3**：一条 open-ended 连接拿到总量后，为什么必须先 `setEnd` 再发慢启动扩张信号？
**答案**：扩张的下一步是切分受害者，切分要求 `End` 非 nil（`expandConnections` 会跳过 openEnded 的连接）。若先发信号，慢启动可能在大小区间未知时尝试切分，只能空转；先封闭区间可让下一批连接立刻按已知总量瓜分剩余空间。

### 4.2 模块二：connection 状态机

#### 4.2.1 概念说明

`connection` 是一条 HTTP 连接的全部运行时状态：它负责哪段 chunk、跑到哪了、失败几次、速度多少。注意它**不是** `net.Conn` 的包装——每次重试都是一条全新的 HTTP 请求，`connection` 只是逻辑上的「下载车道」，同一车道可以先后发出多个请求。

状态机五态：`connNotStarted → connConnecting → connDownloading → connCompleted / connFailed`。其中 `connConnecting` 涵盖「发请求、等响应头」阶段，`connDownloading` 是「响应头已验证、正在读 body 写文件」。`connFailed` 不是终态——计数失败会带着 `retryTimes++` 回到重试循环，只有 403、重试耗尽、Range 完整性错误才真正停在这个状态。

三种角色（`connectionRole`）：`roleResolve`（解析连接，现在的实现里连接 0 兼任）、`rolePrimary`（第一条工作连接，从 Resolve 预取处接管）、`roleWorker`（后续扩张出的连接）。角色影响两处逻辑：只有连接 0 能触发 Resolve 预取接管（u5-l1）和顺序降级（u5-l3），`runConnection` 中 `conn.Role == rolePrimary || conn.ID == 0` 会首次关闭 `primaryReadyCh`。

#### 4.2.2 核心流程

```text
connNotStarted ──runConnection 进入──▶ connConnecting（发 HTTP 请求）
connConnecting ──响应验证通过────────▶ connDownloading（读 body、WriteAt）
connDownloading ─chunk 干完──────────▶ connCompleted（或转入互助，见 4.5）
connDownloading ─出错───────────────▶ connFailed
  ├─ 403 / 重试≥3 / Range完整性错误：停在 connFailed（terminal）
  └─ 其他：退避 1s,2s,…,5s 后重新请求（回到 connConnecting）
```

#### 4.2.3 源码精读

**状态与角色常量**。[internal/protocol/http/fetcher.go:L82-L98](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L82-L98) 定义五态与三角色；[internal/protocol/http/fetcher.go:L65-L76](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L65-L76) 是 Fetcher 自身的八态（本讲用到 `stateSlowStart`/`stateSteady`，其余在 u5-l1/u5-l3 出现过）。

**connection 结构**。[internal/protocol/http/fetcher.go:L131-L151](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L131-L151)：前 7 个字段是「成绩单」（ID、角色、状态、chunk、累计下载、是否完成），`failed/exited/retryTimes/lastErr` 是失败簿，`speed/lastSpeedCheck/lastSpeedDownload` 三件组是测速窗口（供工作窃取决策），`ctx/cancel` 让单条连接可被独立取消（暂停与窃取重启都用它）。

**测速窗口**。读循环每 500ms 更新一次速度：[internal/protocol/http/fetcher.go:L1554-L1566](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1554-L1566)，\[ v = \frac{D_{\text{now}} - D_{\text{last}}}{t_{\text{now}} - t_{\text{last}}} \]。

**状态写入点**。`State` 只在持有 `connMu` 时写：`runConnection` 入口置 `connConnecting`（[L1185-L1188](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1185-L1188)）、响应验证通过置 `connDownloading`（[L1482-L1485](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1482-L1485)）、干完置 `connCompleted`（[L1216-L1221](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1216-L1221)）、各失败分支置 `connFailed`。

**对外呈现**。[internal/protocol/http/fetcher.go:L2520-L2570](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2520-L2570) 的 `Stats()` 把每条连接映射成 [pkg/protocol/http/model.go:L105-L115](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/protocol/http/model.go#L105-L115) 的 `StatsConnection`（Downloaded/Total/Completed/Failed/RetryTimes）；其中 `Total` 不是「这条连接负责的区间长」，而是 `averageConnectionTotal`（[L2577-L2586](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2577-L2586)）按当前连接数把总大小均摊、余数进位——因为慢启动下连接数在涨，均摊分母让各车道贡献可横向比较。这些统计经 `FetcherManager.Store` 的连接表快照（[internal/protocol/http/fetcher_manager.go:L81-L100](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_manager.go#L81-L100)）原样落盘，供断点恢复（u4-l5）。

顺带一个诚实的观察：`Fetcher.resolveConn` 字段在 `Progress()`/`checkCompletion()` 里都被判空检查，但当前生产路径从不赋值（只有测试给它赋值）——解析连接如今直接以连接 0 的身份活在 `f.connections` 里，这个字段是防御性的遗迹。

#### 4.2.4 代码实践

**实践目标**：借现成测试观察一条连接的「成绩单」。

1. 运行：`go test ./internal/protocol/http/ -run 'TestFetcher_SlowStartExpansion/MaxConns5' -v`。
2. 阅读测试源码 [internal/protocol/http/fetcher_test.go:L2392-L2412](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_test.go#L2392-L2412)：它断言 `len(stats.Connections)` 恰好等于 `maxConns`，并用 `t.Logf` 打出每条连接的 Downloaded/Completed。

**需要观察的现象**：日志里 5 条连接逐一列出，各自 `Downloaded` 约为 200MB/5 = 40MB（有整除余数与预取偏移造成的偏差），`Completed` 全为 true。

**预期结果**：连接数不多不少正好 5；各连接下载量之和等于文件总大小 200MB（`internal/test/httptest.go` 的 `BuildSize`，见 [L24-L27](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/test/httptest.go#L24-L27)）。精确数值分布**待本地验证**（取决于切分时的剩余量与测速抖动）。

#### 4.2.5 小练习与答案

**练习 1**：`connFailed` 到底是不是终态？
**答案**：不是。`runConnection` 的重试循环里，计数失败（非豁免 4xx）`retryTimes++` 后只要小于 3 就退避重试（回到 `connConnecting`）；只有 403（服务器限连接数）、`retryTimes >= 3`、Range 完整性错误三类才让连接真正停在 `connFailed` 退出循环。见 [internal/protocol/http/fetcher.go:L1260-L1295](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1260-L1295)。

**练习 2**：为什么每条连接要有独立的 `ctx/cancel`，而不是全任务一个？
**答案**：`Pause()` 取消任务级 `f.ctx`（连接的 ctx 都是它的子 ctx，会级联取消）；但单条连接的场景——比如切分后重发请求、窃取后换 chunk 重启——需要局部重置。恢复下载时 [resumeConnections（L2166）](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2166) 会给每条连接 `context.WithCancel(f.ctx)` 换新 ctx，旧周期的取消痕迹不影响新周期。

**练习 3**：`StatsConnection.Total` 为什么用「总大小均摊」而不是真实 chunk 长度？
**答案**：慢启动期间连接数持续增长、chunk 又被切来切去，真实区间长度是高频变化值；均摊分母（当前连接数）在单次快照内是恒定的，UI 拿同一把尺子就能比较各车道快慢。注释还点明余数进位是为了避免「没到配额却显示 100%」。

### 4.3 模块三：downloadLoop 调度循环与 expandConnections 扩张

#### 4.3.1 概念说明

`downloadLoop` 是每个下载周期唯一的「调度协程」：它不搬数据，只决定「现在该有几条连接在干活」。它的一生是：

1. **开局**：新任务从 `startResolveDownload` 进入——支持 Range 就发起第一批扩张；不支持就单连接顺序下载并原地等完成（本讲不展开）。
2. **恢复**：断点续传走 `resumeConnections`，把存量连接重新拉起。
3. **慢启动循环**：阻塞等待两个信号——`ctx.Done()`（暂停/取消，直接退出）或 `expansionCh`（某批连接全部拿到响应，该扩张了）。每次醒来先 `checkCompletion()`（字节可能已经够了，别再建连接），再 `expandConnections()`；若控制器说「到顶了」（`getNextBatchSize() == 0` 或无可切分的工作），Fetch 切到 `stateSteady`，调度协程转入 `waitForCompletion` 等 `wg.Wait()` 收尾。

`expandConnections` 是唯一建连接的地方，两种模式：**第一批**——没有存量连接时，连接 0 从预取偏移出发拿走剩余全部（大小未知则 open-ended）；**后续批**——为本批每个名额找「剩余量最大且 > 1MB」的受害者，偷其后一半给新连接。

#### 4.3.2 核心流程

```text
doStart（u5-l1 尾部）
  ├─ newSlowStartController(maxConns)          # maxConns 来自 OptsExtra.Connections
  └─ go downloadLoop
        ├─ 新任务：startResolveDownload
        │     ├─ Range=false → 单连接 + waitForCompletion（返回）
        │     └─ Range=true  → expandConnections（第一批=1 条）
        ├─ 恢复：resumeConnections（可能先为顺序前缀补 1 个批次）
        └─ for { select
              ├─ ctx.Done()            → return（暂停）
              └─ expansionCh 到来：
                    ├─ checkCompletion()=true → waitForCompletion → 结束
                    ├─ expandConnections()（下一批 / 切 steady）
                    └─ 已是 stateSteady  → waitForCompletion（wg.Wait + onDownloadComplete）
```

`expandConnections` 后续批的切分循环：

```text
minSplitSize = 512KB（慢启动期速度不稳，用固定门槛）
for 每个本批名额:
    受害者 = 剩余量最大 且 remain > 2×minSplitSize 且 非 openEnded 且 未完成/未失败的连接
    若无受害者: break（工作太碎，不硬拆）
    split = victim.End - victim.remain()/2
    新连接 chunk = [split+1, victim.End]；victim.End 收缩为 split
commitBatch(len(新连接))；逐个 go runConnection(新连接)
```

#### 4.3.3 源码精读

**启动入口**。[internal/protocol/http/fetcher.go:L898-L913](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L898-L913)：`doStart` 尾部从 `OptsExtra` 取连接上限、构造慢启动控制器、建任务级 ctx 与 `downloadLoopDone` 生命周期通道，置 `stateSlowStart` 后 `go f.downloadLoop(downloadLoopDone)`。连接上限的三级回退（任务级 → 协议配置 → 1）在 [initOptions（L743-L767）](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L743-L767)，协议默认值 16 来自 [internal/protocol/http/fetcher_manager.go:L74-L79](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_manager.go#L74-L79)。

**downloadLoop 主体**。[internal/protocol/http/fetcher.go:L918-L1000](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L918-L1000)。三个细节值得停顿：

- [L936-L948](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L936-L948)：新任务先**快照** `startedWithRanges := f.meta.Res.Range` 再启动连接——因为第一条连接的请求在飞行途中可能发现服务器忽略 Range，把整个任务切到顺序模式；若不快照，循环可能把那条连接的扩张信号误当成「该建新连接」。
- [L964-L999](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L964-L999)：慢启动 `for-select`，醒来后按 `rangeMode` → `checkCompletion` → `expandConnections` → steady 判定的顺序处理；非 Range 模式下信号只用于「唯一连接退出后转入收尾」。
- [L985-L989](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L985-L989)：扩张前先 `checkCompletion()`——预取 + 首批连接可能已经把字节凑齐，此时建新连接纯属浪费。

**第一批分配**。[internal/protocol/http/fetcher.go:L1048-L1100](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1048-L1100)：若预取已覆盖全文件，直接校验 checksum 并发 `doneCh`（L1050-L1075 的快捷出口）；否则连接 0 拿 `[prefetched, totalSize-1]`（未知大小则 open-ended），`commitBatch(1)` 后 `go f.runConnection(conn)`。注意 `conn.Downloaded = prefetched`——预取量记在连接 0 头上，`Progress()` 才能立刻反映预取进度。

**后续批切分**。[internal/protocol/http/fetcher.go:L1102-L1167](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1102-L1167)：整个扫描-切分过程持 `connMu`（与下载循环的读写互斥）；受害者筛选条件在 [L1113-L1125](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1113-L1125)（跳过已完成/失败/openEnded，`remain > minSplitSize*2` 即 1MB）；切分三行在 [L1133-L1137](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1133-L1137)。找不到可切对象时 [L1154-L1157](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1154-L1157) 直接转 `stateSteady`——文件快下完时不再硬拆出碎片连接。

**收尾**。[waitForCompletion（L2180-L2186）](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2180-L2186) `wg.Wait()` 后，仅当本周期 ctx 未取消才调 `onDownloadComplete()`——被暂停的周期不抢发完成信号（u5-l3 再展开完成判定与校验和）。

#### 4.3.4 代码实践

**实践目标**：数清 `expansionCh` 这个调度心跳的所有信号来源。

1. 在 [internal/protocol/http/fetcher.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go) 中搜索 `expansionCh <-` 与 `signalExpansion`。
2. 为每个发送点写一句「谁、在什么情况下发」。

**需要观察的现象**：共 3 个发送点、2 类触发方。

**预期结果**（答案）：

| 发送点 | 触发者 | 时机 |
|---|---|---|
| [onConnectSuccess L186](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L186) | 下载连接 | 本批全部连接都成功拿到响应（`batchReady >= batchPending`） |
| [onConnectFailed L207](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L207) | 下载连接 | `batchPending` 被失败减到 0（全失败也结算批次，防死等） |
| [signalExpansion L255-L260](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L255-L260) | `runConnection` 的 defer（[L1180-L1182](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1180-L1182)） | 降级为顺序的连接退出后唤醒循环转入收尾 |

三处都是容量 1 的 channel 上的非阻塞发送——信号是幂等的心跳，丢了不补、多了不存。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `expandConnections` 里第一批是 1 条而不是按 `Connections` 配置一次拉满？
**答案**：慢启动思想——先用 1 条连接验证「服务器真的按 Range 返回 206」，确认后才成批加码。对忽略 Range、限连接数、一次性 URL 的服务器，一次拉满会打出一片注定失败或互相污染的请求（u5-l3 的顺序降级要求 `len(f.connections) == 1` 才安全）。

**练习 2**：`checkCompletion()`（[L2332-L2368](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2332-L2368) 判定「字节数达标」或「全部连接完成」两条路各为什么需要？
**答案**：字节数达标（`totalDownloaded >= Size`）覆盖「连接因 403 失败但别的连接会补完它的块」的场景；全部完成覆盖大小未知（Size=0）时只能看连接状态的情形。两条路都能在扩张信号到来时提前终结慢启动循环。

**练习 3**：恢复下载（`resumeConnections`，[L2139-L2178](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2139-L2178)）后为什么还能继续慢启动扩张？
**答案**：恢复的存量连接被逐条 `go runConnection` 拉起，它们成功拿到 206 响应时同样调 `onConnectSuccess`，把恢复前 `doStart` 里新建的控制器批次结算掉，`expansionCh` 照常心跳，`downloadLoop` 的慢启动循环因此继续扩到上限——恢复与全新下载共享同一条扩张路径。

### 4.4 模块四：slowStartController 慢启动控制器

#### 4.4.1 概念说明

`slowStartController` 是个不到百行的小状态机，回答一个问题：**下一批建几条连接、什么时候允许建**。它的记账三件套：

- `totalLaunched`：已发射总数，逼近 `maxConnections` 的进度条；
- `batchPending`/`batchReady`：当前批次的「等待响应数 / 已成功数」，批次结清才允许下一批；
- `nextBatchSize`：下一批的目标规模，增长规律是 **1 → 2 → 之后平方**（2→4→16→256…），被上限截断。

批次增长公式（`commitBatch`，[L236-L253](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L236-L253)）：

\[ b_{0} = 1,\quad b_{1} = \min(2, M),\quad b_{n+1} = \begin{cases} M & \text{若 } b_n > M / b_n \text{（先封顶防溢出）} \\ b_n^2 & \text{否则} \end{cases} \]

且每批实际发射量再被剩余名额 \(\min(b_n,\, M - \mathrm{totalLaunched})\) 截断。默认 `M = 16` 时批次序列是 **1 → 2 → 4 → 9**（最后一批被剩余名额截断）。

「失败收缩」体现在两处：一是 `getNextBatchSize` 里 `paused` 为真直接返回 0（该字段注释写明为 429 预留，**当前生产代码从不置位**，属于预留钩子）；二是批次结算不区分成败——`onConnectFailed` 把 `batchPending` 减到 0 同样触发扩张信号，让全失败的批次不至于卡死循环。真正「不再加连接」的收缩由上层完成：403 等终态失败让连接退出，`expandConnections` 找不到健康受害者时转 steady。

#### 4.4.2 核心流程

```text
                 ┌─────────────── downloadLoop ───────────────┐
                 │                                             │
  commitBatch(n) │ 记账: totalLaunched+=n, pending=n, ready=0  │
                 │ 增长: nextBatchSize ← 2 或 nextBatchSize²   │
                 ▼                                             │
  getNextBatchSize() ── 0 ──▶ stateSteady（到顶）              │
        │ n>0                                                 │
        ▼                                                     │
  发射 n 条连接 ──成功──▶ onConnectSuccess: ready++            │
        │                    ready >= pending → 发信号 ────────┤
        └──失败──▶ onConnectFailed: pending--                  │
                             pending == 0 → 发信号 ────────────┘
```

#### 4.4.3 源码精读

**结构与构造**。[internal/protocol/http/fetcher.go:L157-L174](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L157-L174)：一把 `mu` 保护全部字段；`expansionCh` 容量 1，天然合并重复信号。

**成功结算**。[internal/protocol/http/fetcher.go:L176-L192](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L176-L192)：`onConnectSuccess` 在**响应头验证通过、进入 `connDownloading`** 时被调用（调用点 [L1492-L1494](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1492-L1494)）——「成功」指服务器认了这次 Range 请求，与 body 是否传完无关。所以批次节奏由 **RTT + 服务器接受度**决定，与文件多大、传多慢无关。

**失败结算**。[internal/protocol/http/fetcher.go:L194-L211](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L194-L211)：注释明说这同时处理「全部成功」和「全部失败」两种结清方式。注意一个混合批的巧思：批 4 条、3 成 1 败时，`pending` 被减成 3，`ready(3) >= pending(3)` 成立，同样发信号。

**读取下一批**。[internal/protocol/http/fetcher.go:L213-L234](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L213-L234)：`getNextBatchSize` 只做两件事——`paused` 短路、`min(nextBatchSize, remaining)` 截断。返回 0 由调用方解释为「转 steady」。

**提交批次（增长核心）**。[internal/protocol/http/fetcher.go:L236-L253](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L236-L253)：三条分支各自带注释——`nextBatchSize == 1` 时直接跳到 2（因为 \(1^2 = 1\)，平方永远长不大）；`nextBatchSize > maxConnections/nextBatchSize` 时先封顶（防 int 平方溢出）；否则平方。注意每个批次发射后**立即**预置下一批规模，`batchPending/batchReady` 重置为本批。

**测试对照**。[internal/protocol/http/fetcher_test.go:L2328-L2341](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_test.go#L2328-L2341) 的测试注释写着 "Expansion pattern: 1 -> 2 -> 4 -> 8 -> 16..."（翻倍），但代码是平方——以 M=8 为例，代码的真实发射序列是 1、2、4、1（最后一批被剩余名额 1 截断），而不是 1、2、4、8。测试断言的「最终恰好 maxConns 条」仍然成立（总数守恒），只是注释滞后于实现。**读代码，别只读注释**——这本身就是个好练习。

**失败如何反馈到控制器**。`runConnection` 的失败路径（[L1240-L1295](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1240-L1295)）里，Range 完整性错误、403、每次计数失败都调 `onConnectFailed()`；哪些状态码「计数」由 [internal/protocol/http/helper.go:L78-L98](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/helper.go#L78-L98) 决定：5xx、429、408、440、499 豁免（无限重试不计数），其余 4xx（如 416）计数、3 次封顶。退避节奏 1s、2s、3s…封顶 5s。

#### 4.4.4 代码实践

**实践目标**：不经网络，直接驱动控制器验证批次序列。

在 `internal/protocol/http/` 下新建 `slowstart_controller_test.go`（**示例代码**，读者本地练习用）：

```go
package http

import "testing"

func TestSlowStartControllerBatchSequence(t *testing.T) {
	for _, M := range []int{4, 5, 8, 9, 16} {
		s := newSlowStartController(M)
		var batches []int
		for {
			n := s.getNextBatchSize()
			if n == 0 {
				break
			}
			batches = append(batches, n)
			s.commitBatch(n)
			for i := 0; i < n; i++ { // 模拟本批全部成功
				s.onConnectSuccess()
			}
		}
		t.Logf("M=%2d 批次序列: %v 合计 %d", M, batches, s.totalLaunched)
	}
}
```

运行：`go test ./internal/protocol/http/ -run TestSlowStartControllerBatchSequence -v`。

**需要观察的现象**：每个 M 的批次序列与合计。

**预期结果**：M=4 → `1 2 1`；M=5 → `1 2 2`；M=8 → `1 2 4 1`；M=9 → `1 2 4 2`；M=16 → `1 2 4 9`；合计恒等于 M。

#### 4.4.5 小练习与答案

**练习 1**：手推 M=16 的完整批次序列。
**答案**：批 1 发射 1（total=1，next：1→2）；批 2 发射 2（total=3，next：2≤16/2 不封顶，平方→4）；批 3 发射 4（total=7，next：4>16/4=4 不成立……注意 4>4 为假，走平方→16）；批 4 `min(16, 16-7)=9`（total=16，next：16>16/16=1 封顶→16）；再取为 0 → steady。序列 **1、2、4、9**。

**练习 2**：`onConnectSuccess` 的「成功」为什么不算 body 传输完成？这带来什么批次节奏特性？
**答案**：它在响应头验证通过时调用（`connDownloading` 置位处），此刻 body 可能要传几十秒。因此批与批之间的间隔只由 RTT 与服务器接受度决定——多连接是「秒级」铺开的，不必等任何一条连接传完自己的份。

**练习 3**：一批 4 条连接 2 成 2 败，控制器状态如何演化？下一批发多少？
**答案**：2 次成功后 `ready=2 < pending=4` 不发信号；2 次失败各把 `pending` 减一，第二次失败后 `pending=2`，此时 `ready(2) >= pending(2)` 在**下一次成功**时成立，或若后续也失败则 `pending` 减到 0 时由 `onConnectFailed` 发信号。无论哪种方式批次结清，`getNextBatchSize` 由 `totalLaunched` 决定，不受成败影响——失败的收缩交给上层（终态失败连接退出、无受害者可切则 steady）。

### 4.5 模块五：helpOtherConnection 运行时互助

#### 4.5.1 概念说明

慢启动解决「开局铺多少连接」，工作窃取解决「收尾平衡」：**一条连接干完自己的 chunk 后不退出，而是去偷最慢连接的一半剩余工作**。判定条件（常量在 [L31-L40](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L31-L40)）：

- 受害者剩余时间 \(\mathrm{remain}/v > 3\) 秒（速度未知时视为「需要帮助」）；
- 受害者剩余量 ≥ 512KB（不偷碎片）；
- 受害者非 openEnded、未完成、未失败、不是自己。

它与 `expandConnections` 的切分同构但主体不同：**扩张是「新连接偷存量连接的后一半」，互助是「空闲连接偷最慢连接的后一半」**。切分点计算与 4.1 的不变量完全一致，故不重不漏。

被偷的一方如何发现？它的读循环每次写文件前都会在 `connMu` 下**重新读自己的 chunk 边界**：`End` 被收缩后，若本次读到的数据超过剩余量就截断写入并结束；若剩余量已是 0 则直接返回——飞行中响应体里的多余字节随 `resp.Body.Close()` 丢弃。这是「并发修改区间」与「流式读取」和平共处的协议。

#### 4.5.2 核心流程

```text
runConnection 循环:
  err = downloadChunkOnce(当前 chunk)
  ├─ err == nil:
  │    ├─ Range 模式且 helpOtherConnection(conn) 成功:
  │    │      chunk 已换成偷来的新区间 → 清零重试计数 → continue（发新请求）
  │    └─ 否则: conn.Completed=true, connCompleted → 退出
  └─ err != nil: （失败路径见 4.4，略）

helpOtherConnection(helper):
  持 connMu 扫描全部连接:
    候选 = 非 helper、未完成/失败、非 openEnded、remain ≥ 512KB、remain/v > 3s
    取 remain/v 最大者（v 未知按"需要帮助"处理）
  无候选 → false（真干完了）
  helper.Begin = victim.End - victim.remain()/2
  helper.End   = victim.End；helper.Downloaded = 0
  victim.End   = helper.Begin - 1
```

受害者的配合（`downloadChunkOnce` 读循环内，持 `connMu`）：

```text
remain = conn.Chunk.remain()          # 每次写入前重读，尊重并发收缩
若 remain <= 0      → return nil      # 剩余全被偷走，直接结束
若 remain < len(data) → data = data[:remain]，写完即 finished
```

#### 4.5.3 源码精读

**窃取参数**。[internal/protocol/http/fetcher.go:L31-L40](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L31-L40)：`stealThresholdSeconds = 3`、`stealMinChunkSize = 512KB`，注释即文档。每连接 64KiB 复用读缓冲的 `connReadBufferSize`（[L42-L45](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L42-L45)）也在此声明。

**成功路径上的互助调用**。[internal/protocol/http/fetcher.go:L1214-L1230](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1214-L1230)：`downloadChunkOnce` 返回 nil 后，`f.meta.Res.Range && f.helpOtherConnection(conn)` 为真则带着新 chunk 进入下一轮循环（并清零 `retries`/`retryTimes`——换新区间视作新开始）；只有偷不到活儿才置 `connCompleted` 退出。这就是「连接先干完自己的一份，再帮最慢的同事」。

**窃取实现**。[internal/protocol/http/fetcher.go:L2039-L2088](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2039-L2088)：注释开头就写明策略「偷最慢连接一半的工作」。候选过滤在 [L2048-L2075](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2048-L2075)，剩余时间按实测速度估计、速度为 0（尚未经历第一个 500ms 测速窗）时保守地视为需要帮助（[L2061-L2068](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2061-L2068)）；四行原子切分在 [L2082-L2087](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2082-L2087)。

**读循环对收缩的配合**。[internal/protocol/http/fetcher.go:L1520-L1572](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1520-L1572)：每次拿到一批数据都在 `connMu` 内重读 `remain`（注释点明「尊重并发的切分」），按需截断 `data`、计算 `writeOffset = Begin + Downloaded`、`WriteAt` 落盘、再回锁内累加两个 `Downloaded` 并更新测速窗。受害者超额读到的字节被静默丢弃——它们将由 helper 重新请求并写入，账面依然守恒。

**互助对进度统计的影响**。[internal/protocol/http/fetcher.go:L2542-L2549](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L2542-L2549)：任务成功时把所有未失败连接强制标记 Completed——注释解释了原因：工作窃取与「按总字节数判完成」可能让某条 helper 的内部状态停在非终态，虽然整个传输已经结束。

#### 4.5.4 代码实践

**实践目标**：亲手画（或用日志验证）一次 4 连接下载的区间演化图。

1. 阅读并运行综合实践（第 5 节）的观察测试，取 `Connections=4`。
2. 对照 4.3/4.5 的算法，在纸上按批次画出每条连接的 `[Begin, End)` 占用，标注哪一步是「扩张切分」、哪一步是「运行时窃取」。

**需要观察的现象**：扩张阶段连接 0 的区间被连续切两次；运行期最先完成的连接会去偷当时最慢的连接。

**预期结果**：演化图应与第 5 节的参考图一致（批次 1→2→1）；运行期窃取发生的确切时刻与对象**待本地验证**（取决于测速窗的抖动）。

#### 4.5.5 小练习与答案

**练习 1**：受害者的 HTTP 响应还在流式传输，它的 `End` 却被收缩了，多读的字节去哪了？会不会造成文件损坏？
**答案**：读循环在锁内发现 `remain < len(data)` 后把 `data` 截断到 `remain` 并标记 finished，多余的缓冲字节直接丢弃，函数返回后 `defer resp.Body.Close()` 关掉响应。被丢弃区间属于 helper 的新 chunk，会由 helper 重新请求写入。由于切分点永远在受害者已写位置之前（4.1 的不等式），不存在「两边都写过」或「没人写」的字节，文件不会损坏。

**练习 2**：为什么 `expandConnections` 慢启动期用固定 512KB 门槛，而 `helpOtherConnection` 用「3 秒 + 512KB」双门槛？
**答案**：慢启动期连接刚建、测速窗（500ms）尚不可信，只能按剩余量切；运行期互助的目标是缩短**墙钟时间**，所以用速度估计剩余时间（>3 秒才值得偷），512KB 下限防止切出碎片请求。见 [L1104-L1105](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1104-L1105) 的注释「慢启动期速度未稳，用固定最小值」。

**练习 3**：一条连接换 chunk 互助后为什么要把 `retries`/`retryTimes` 清零？
**答案**：互助成功说明网络与服务器都在正常工作，之前的失败属于旧区间/旧请求的历史；若不清零，新 chunk 上偶发一两次失败就可能凑满 3 次上限，让健康的连接过早退出。见 [L1224-L1228](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1224-L1228)。

## 5. 综合实践

**任务**：写一个「慢启动观察器」测试，统计不同 `Connections` 配置下**实际建立的连接数与批次节奏**，并画出 4 连接下载的 chunk 占用/互助示意图。

**第 1 步：写观察测试**。在 `internal/protocol/http/` 下新建 `slowstart_observe_test.go`（**示例代码**，仿照 [TestFetcher_SlowStartExpansion](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_test.go#L2328-L2417) 与 [buildConfigFetcher](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_test.go#L3300-L3308)）：

```go
package http

import (
	"testing"
	"time"

	"github.com/GopeedLab/gopeed/pkg/base"
	fhttp "github.com/GopeedLab/gopeed/pkg/protocol/http"
	"github.com/GopeedLab/gopeed/internal/test"
)

func TestSlowStartObserve(t *testing.T) {
	for _, conns := range []int{4, 8, 16} {
		conns := conns
		t.Run(test.ToJson(conns), func(t *testing.T) { // ToJson 仅作子测试名
			listener := test.StartTestLowSpeedServer(200 * time.Nanosecond)
			defer func() { listener.Close(); time.Sleep(50 * time.Millisecond) }()

			f := buildConfigFetcher(config{Connections: conns})
			f.Meta().Opts = &base.Options{
				Name: test.DownloadName, Path: test.Dir,
				Extra: &fhttp.OptsExtra{Connections: conns},
			}
			if err := f.Resolve(&base.Request{URL: "http://" + listener.Addr().String() + "/" + test.BuildName}); err != nil {
				t.Fatal(err)
			}
			if err := f.Start(); err != nil {
				t.Fatal(err)
			}

			// 采样协程：每 100ms 记录一次「已建立的连接数」
			stop := make(chan struct{})
			done := make(chan []int, 1)
			go func() {
				var seq []int
				for {
					select {
					case <-stop:
						done <- seq
						return
					case <-time.After(100 * time.Millisecond):
						seq = append(seq, len(f.Stats().Snapshot.(*fhttp.Stats).Connections))
					}
				}
			}()

			if err := f.Wait(); err != nil {
				t.Fatal(err)
			}
			close(stop)
			t.Logf("Connections=%d 连接数增长序列: %v", conns, <-done)

			stats := f.Stats().Snapshot.(*fhttp.Stats)
			if len(stats.Connections) != conns {
				t.Errorf("最终连接数 = %d, want %d", len(stats.Connections), conns)
			}
			var total int64
			for i, c := range stats.Connections {
				t.Logf("conn[%d] Downloaded=%d Total=%d Completed=%v Retry=%d",
					i, c.Downloaded, c.Total, c.Completed, c.RetryTimes)
				total += c.Downloaded
			}
			if total < test.BuildSize {
				t.Errorf("总下载量 %d < 文件大小 %d", total, test.BuildSize)
			}
		})
	}
}
```

运行：`go test ./internal/protocol/http/ -run TestSlowStartObserve -v`。

**第 2 步：解读节奏**。`连接数增长序列` 应呈阶梯状：4 连接为 `1 → 3 → 4`（批 1 一条、批 2 两条、批 3 一条），8 连接为 `1 → 3 → 7 → 8`，16 连接为 `1 → 3 → 7 → 16`。阶梯出现的绝对时刻取决于 RTT 与采样周期，**待本地验证**；若服务器响应快到阶梯挤在同一采样点，可把延迟调大（如 `500*time.Nanosecond`）拉开节奏。

**第 3 步：画 4 连接示意图**。以文件总长 N（测试服务器为 200MB）、预取量忽略不计：

```text
批次1（1条）   conn0 [0 ────────────────────────────── N-1]

批次2（+2条）  第1刀切剩余量最大的 conn0：conn1 偷走后一半
               conn0 [0 ──────────── N/2-1]
               conn1 [N/2 ───────────────────── N-1]
               第2刀 victim 仍是 conn0（与 conn1 剩余相等，遍历序在前者胜出）
               conn0 [0 ──── N/4-1]
               conn2 [N/4 ─── N/2-1]        conn1 [N/2 ─────── N-1]

批次3（+1条）  victim = conn1（剩余量最大 = N/2）
               conn1 [N/2 ───────── 3N/4-1]
               conn3 [3N/4 ─────────────── N-1]

终局布局（连接 ID 与文件位置错位，这是切分顺序的自然结果）：
  |◄──── conn0 ────►|◄──── conn2 ───►|◄──── conn1 ────►|◄──── conn3 ────►|
  0                N/4              N/2               3N/4               N

运行期互助（示意）：若 conn2 最先干完自己的 N/4，它会扫描
remain/v > 3s 的连接（此时大概率是 conn1 或 conn3 中较慢者），
偷走其后一半，自身区间跳到该连接的 [mid+1, End]；
被偷连接的读循环在下一次写入前发现 End 收缩，截断并提前收工。
```

**判定完成**：测试通过（最终连接数恰好等于配置、总下载量达标）+ 示意图能向别人讲清「三批、四刀（扩张三刀 + 可能的互助若干刀）、每刀的不变量」。

## 6. 本讲小结

- **chunk 是账本**：`Begin/End/Downloaded` 三字段维护「不重不漏覆盖未完成字节」的核心不变量；切分点取受害者剩余量的一半，数学上保证永远落在其已写位置之前，并发切分因此安全。
- **connection 是车道不是 socket**：五态状态机（NotStarted→Connecting→Downloading→Completed/Failed）全部在 `connMu` 下写入；403、重试耗尽、Range 完整性错误才是终态失败，5xx/429 等豁免错误无限退避重试（1s 起步、5s 封顶）。
- **downloadLoop 是唯一调度者**：每个下载周期一个调度协程，靠容量 1 的 `expansionCh` 心跳驱动扩张；醒来先 `checkCompletion` 再 `expandConnections`，到顶或无活可切就转 `stateSteady` 等收尾。
- **慢启动按批平方增长**：批次序列 1 → 2 → 4 → 16 → …（先 bootstrap 到 2，之后平方，双重截断防溢出防超发），默认上限 16 时为 1、2、4、9；「成功」指响应头验证通过，所以铺连接只花几个 RTT。
- **失败即刻结算批次**：`onConnectFailed` 把 `batchPending` 减到 0 同样触发扩张信号，全失败的批次不会卡死循环；真正的收缩由上层完成（终态失败退出、无受害者可切转 steady）。
- **helpOtherConnection 是收尾平衡器**：干完自己 chunk 的连接去偷「预计还需 3 秒以上且剩余 ≥ 512KB」的最慢连接的后一半；受害者在读循环里每次写入前重读边界、截断超额数据，实现无锁冲突的协作。

## 7. 下一步学习建议

本讲把「 happy path（服务器规规矩矩返回 206）」的多连接机制讲完了，下一讲 **u5-l3《Range 校验、断点续传与降级策略》** 专门对付不规矩的世界：`downloadChunkOnce` 主循环里我们只看了写文件的后半段，前半段的 `validateRangeResponse`/`parseRangeResponse` 严格校验、200 回退顺序下载（`fallbackToSequentialDownload`）、If-Range 断点校验与 `resumeConnections` 恢复路径都在那里。建议先精读 [internal/protocol/http/fetcher.go:L1299-L1477](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L1299-L1477)（`downloadChunkOnce` 的响应处理半段），再用 [internal/test/httptest.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/test/httptest.go) 里现成的 `StartTestIgnoredRangeServer`、`StartTestRangeBugServer`、`StartTestConLimitServer` 做对照实验。
