# u3-l2 FetcherManager 与协议路由：URL 如何选中协议

## 1. 本讲目标

上一讲（u3-l1）我们弄清了单个 Fetcher 的生命周期契约：每个下载任务对应一个 Fetcher 实例，由 Downloader 按固定时序驱动。但有一个问题被刻意留下了——**当一个 URL 交给 Downloader 时，它怎么知道该造一个 HTTP Fetcher 还是 BT Fetcher？**

学完本讲，你应该能够：

1. 说出 `FetcherManager` 接口每个方法的职责，理解它是「协议的工厂 + 说明书」。
2. 掌握 `SchemeFilter.Match` 三种匹配类型（url / file / base64）的精确语义，包括大小写处理与 query 剥离。
3. 解释 `DownloaderConfig.Init` 中管理器注册顺序为什么是**路由优先级**，以及 FTP 为什么必须排在最前。
4. 列出 `parseFm` / `buildFetcher` 在源码中的全部调用点，理解路由在整个任务生命周期中发生的时刻。

## 2. 前置知识

本讲需要以下背景（前几讲已建立，这里只做一句话回顾）：

- **Fetcher**：一个下载任务的协议执行者（internal/fetcher 包），有 Resolve/Start/Pause 等十个方法。本讲不关心这些方法的内部，只关心「谁来决定用哪个 Fetcher」。
- **URL vs 协议**：直觉上「`http://` 开头就是 HTTP 下载」，但 Gopeed 的路由比这复杂一点——`https://host/video.m3u8` 要走 HLS 而不是普通 HTTP，`ftp://host/x.torrent` 要走 FTP 而不是 BT。**同一个 URL 可能同时满足多个协议的匹配条件，必须有一个确定的优先级规则。**
- **前缀匹配的陷阱**：判断 scheme 不能只做 `strings.HasPrefix(uri, "ftp")`，否则 `ftps://` 会被 `ftp` 误伤（后面会看到源码用 `pattern+":"` 精确截断 scheme 边界）。
- **data URI**：形如 `data:application/x-bittorrent;base64,xxxx` 的 URI，把二进制内容（如种子文件）直接编码在 URL 里。前端「选择本地 .torrent 文件」时常用这种方式把文件内容传给后端。
- **表驱动测试（table-driven test）**：Go 社区最常用的测试组织方式——把「输入 + 期望输出」列成结构体切片，循环断言。本讲的实践任务就是写一个这样的测试。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [internal/fetcher/fetcher.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go) | 定义 `FetcherManager` 接口、`SchemeFilter` 与三种 `FilterType`——路由的「契约层」 |
| [pkg/download/model.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go) | `DownloaderConfig.Init()` 注册默认的五个管理器——路由的「注册表」 |
| [pkg/download/downloader.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go) | `parseFm` / `buildFetcher` / `setupFetcher` 及全部路由调用点——路由的「执行层」 |
| [pkg/download/ftp_test.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/ftp_test.go) | `TestTorrentNameIsFile`：锁定默认注册顺序的路由回归测试 |
| [internal/fetcher/fetcher_test.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher_test.go) | `TestSchemeFilter_Match`：SchemeFilter 的表驱动测试 |
| internal/protocol/{ftp,hls,http,bt,ed2k} 各自的 FetcherManager | `Filters()` 的五个真实实现——路由的「数据来源」 |
| [pkg/util/url.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/util/url.go) | `ParseSchema` / `ParseDataUri`：协议实现内部再次解析 scheme 的辅助函数 |

## 4. 核心概念与源码讲解

本讲的四个最小模块：**FetcherManager 接口** → **SchemeFilter.Match** → **DownloaderConfig.Init 注册顺序** → **parseFm/buildFetcher**。前两个是「规则」，后两个是「规则的使用方式」。

### 4.1 FetcherManager 接口：协议的工厂与说明书

#### 4.1.1 概念说明

如果说 Fetcher 是「一个下载任务的执行者」，那么 FetcherManager 就是「一种协议的门面」。它是一个**单例**（每种协议在整个 Downloader 里只有一个管理器实例），承担四类职责：

1. **身份**：`Name()` 返回协议名（如 `"http"`、`"bt"`），这个名字会同时用作任务上的 `task.Protocol` 标签和存储里的协议配置键（见 u2-l3 的 `ProtocolConfig`）。
2. **路由**：`Filters()` 声明「我接受什么样的 URL」——这是本讲的主角。
3. **工厂**：`Build()` 每次返回一个全新的 Fetcher 实例；`ParseName()` 在任务尚未解析（还不知道真实文件名）时从 URL 猜一个显示名；`AutoRename()` 声明该协议的产物是否允许在重名时自动改名。
4. **持久化契约**：`Store()` 把一个运行中的 Fetcher 序列化成可落盘的数据，`Restore()` 反向把它复活——这是断点续传跨进程的桥梁（详见 u4-l5）。

为什么要把「工厂」和「路由声明」放进同一个接口？因为 Downloader 需要的恰恰是这两件事的组合：「看到 URL → 判断归我管 → 造一个执行者」。接口即职责的分界线：**管理器是协议级的、无任务状态的；Fetcher 是任务级的、有状态的。**

#### 4.1.2 核心流程

一个管理器实例在 Downloader 的一生中会被这样使用：

```text
NewDownloader(cfg)
  └─ cfg.Init()                 ← 若调用方未提供 FetchManagers，注册默认五个
       ↓
Setup() 阶段
  ├─ 遍历 FetchManagers：initProtocolConfigs 读 DefaultConfig() 种默认配置
  ├─ 遍历 FetchManagers：实现了 StatefulFetcherManager 的注入状态存储
  └─ 恢复旧任务：parseFm(url) → assignFetcherManager(task)
       ↓
运行期（每个新任务）
  ├─ parseFm(url)               ← 按 Filters 顺序匹配，选中管理器
  ├─ fm.Build()                 ← 造 Fetcher
  ├─ setupFetcher(fm, fetcher)  ← 注入 Controller（配置读取、代理、日志…）
  └─ Save 时 fm.Store(fetcher) / 重启后 fm.Restore()
       ↓
Close() 阶段
  └─ 遍历 FetchManagers 逐个 fm.Close() 释放协议级资源
```

#### 4.1.3 源码精读

接口定义在 [internal/fetcher/fetcher.go:122-143](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L122-L143)，每个方法的注释都标明了用途：`Name` 是协议名；`Filters` 注册支持的 scheme；`Build` 返回新 fetcher；`ParseName` 是「任务未解析时的显示名，从请求 URL 解析」；`AutoRename` 声明重名时是否需要自动改名；`DefaultConfig` 返回协议默认配置；`Store`/`Restore` 负责序列化与复活；`Close` 在整个 Downloader 关闭时释放协议级资源。

注意 `Restore()` 的签名比较特殊（[internal/fetcher/fetcher.go:140](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L140)）：它一次性返回**空模板** `v` 和**构造函数** `f`。Downloader 先把 `v` 传给存储层反序列化填充，再调用 `f(meta, v)` 得到复活后的 Fetcher——这个两段式设计在 [pkg/download/downloader.go:1797-1820](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1797-L1820) 的 `restoreFetcher` 中被串起来：从 `bucketSave` 桶读出任务专属数据、构造 fetcher、再由 `setupFetcher` 补齐 Controller。若构造函数返回 nil（异常情况），则回退到普通的 `Build()`。

FTP 管理器是一个体量适中的完整范例。[internal/protocol/ftp/fetcher_manager.go:35-45](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ftp/fetcher_manager.go#L35-L45) 中 `Name()` 返回 `"ftp"`，`Filters()` 用三个 url 类型过滤器声明了 `FTP` / `FTPS` / `FTPES`（显式/隐式 TLS）三种 scheme；[internal/protocol/ftp/fetcher_manager.go:53-63](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ftp/fetcher_manager.go#L53-L63) 的 `ParseName` 取 URL 路径最后一段作为名字，取不到就退回主机名（且刻意不包含 userinfo，避免泄露 `user:pass@`）。

`ParseName` 的消费方在 [pkg/download/model.go:114-141](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L114-L141)：`Task.Name()` 的取名优先级是「自定义名 → 未解析时问管理器 → 资源名 → 首文件名」，猜不出则显示 `"unknown"`。五个协议的 `ParseName` 策略各不相同，正是「协议自知其名」的体现：

| 协议 | 策略 | 位置 |
| --- | --- | --- |
| http | `path.Base(url.Path)`，退化为主机名 | [internal/protocol/http/fetcher_manager.go:57-68](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_manager.go#L57-L68) |
| ftp | 同上，但先去掉路径末尾 `/` | [internal/protocol/ftp/fetcher_manager.go:53-63](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ftp/fetcher_manager.go#L53-L63) |
| hls | 取 `path.Base` 后**再去掉扩展名**（`video.m3u8` → `video`） | [internal/protocol/hls/fetcher_manager.go:34-45](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/hls/fetcher_manager.go#L34-L45) |
| bt | 磁力链接的 `dn` 参数；否则取 `xt`（infohash）最后一段 | [internal/protocol/bt/fetcher.go:925-941](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/bt/fetcher.go#L925-L941) |
| ed2k | 解析 ed2k 链接中的文件名字段 | [internal/protocol/ed2k/fetcher.go:407-413](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ed2k/fetcher.go#L407-L413) |

`AutoRename` 的消费方在 [pkg/download/downloader.go:1959-1966](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1959-L1966)：只有返回 true 的协议，任务准备阶段才会做「同名文件自动改名」。BT 返回 false（[internal/protocol/bt/fetcher.go:943-945](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/bt/fetcher.go#L943-L945)）——种子的内容与 infohash 绑定，落地文件名随意改会破坏做种一致性。

此外还有一个可选扩展接口 `StatefulFetcherManager`（[internal/fetcher/fetcher.go:145-149](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L145-L149)）：需要跨任务共享协议级状态（如 BT 的 tracker 订阅状态）的管理器实现它，Downloader 在 Setup 时注入 `ProtocolStateStore`（[pkg/download/downloader.go:209-217](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L209-L217)）。这属于 u6-l4 的话题，这里只需知道它挂在管理器而非 Fetcher 上——再次印证「管理器 = 协议级单例」。

#### 4.1.4 代码实践：给五个管理器做一张「身份证」登记表

**实践目标**：把抽象接口落到五个具体实现上，建立「看到协议名就能想起它的过滤器与策略」的检索表。

**操作步骤**：

1. 阅读五个 `Filters()` 实现：[ftp](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ftp/fetcher_manager.go#L39-L45)、[hls](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/hls/fetcher_manager.go#L21-L28)、[http](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_manager.go#L40-L51)、[bt](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/bt/fetcher.go#L904-L919)、[ed2k](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ed2k/fetcher.go#L394-L401)。
2. 在自己的笔记里补全下表（示例表格，「答案」见 4.1.5 练习 1）：

   | 协议名 | 过滤器（Type + Pattern） | ParseName 策略要点 | AutoRename |
   | --- | --- | --- | --- |
   | ftp | url: FTP/FTPS/FTPES | 路径末段，去尾部 `/` | true |
   | hls | … | … | … |
3. 运行现有测试确认环境可用：`go test ./internal/fetcher/ ./pkg/download/ -run 'TestSchemeFilter|TestTorrentNameIsFile' -v`。

**需要观察的现象**：测试全部通过；`TestTorrentNameIsFile` 的输出里能看到七条 URL 的路由结果。

**预期结果**：五份过滤器清单中，只有 BT 同时使用了全部三种 FilterType，只有 HLS 完全不使用 url 类型。

**待本地验证**：`go test` 的具体输出文本因版本而异，请以本机运行结果为准。

#### 4.1.5 小练习与答案

**练习 1**：补全 4.1.4 的登记表。

**参考答案**：

| 协议名 | 过滤器 | ParseName 要点 | AutoRename |
| --- | --- | --- | --- |
| ftp | url: `FTP`、`FTPS`、`FTPES` | 路径末段（去尾 `/`），退化主机名 | true |
| hls | file: `M3U8` | 路径末段再去扩展名 | true |
| http | url: `HTTP`、`HTTPS` | 路径末段，退化主机名 | true |
| bt | url: `MAGNET`；file: `TORRENT`；base64: `APPLICATION/X-BITTORRENT` | 磁力 `dn` 参数 → `xt` 的 infohash 段 | **false** |
| ed2k | url: `ED2K` | 解析链接内文件名 | true |

**练习 2**：为什么 `Restore()` 不直接返回 `Fetcher`，而要返回 `(空模板, 构造函数)` 两件套？

**参考答案**：因为存储层需要先拿到一个「正确类型的空指针」来反序列化（`storage.Get(bucketSave, task.ID, v)` 按 JSON 填充），填好之后才能连同 `FetcherMeta` 一起交给协议自己的构造函数组装 Fetcher。拆成两步让「如何从磁盘读」由 Downloader 统一处理，「读出来怎么变成 Fetcher」由协议自己决定，两者解耦。

**练习 3**：BT 的 `AutoRename()` 为什么返回 false？

**参考答案**：BT 任务的产物目录名来自种子元数据，做种时其他节点按元数据定位内容；若允许随意改名，重启恢复做种时可能对不上原始布局。HTTP/FTP/HLS/ED2K 下载的是「裸文件」，重名时追加序号对协议语义没有任何影响，所以返回 true。

### 4.2 SchemeFilter.Match：URL 的三种「指纹」识别

#### 4.2.1 概念说明

`SchemeFilter` 是路由的最小判定单元：一个 `Type`（怎么比）加一个 `Pattern`（比什么）。三种类型对应三种「URL 指纹」：

- **FilterTypeUrl**：按 **scheme 前缀**匹配。`http://github.com` 的指纹是 `http`。注意必须带上冒号边界——`ftp` 的过滤器不能误吞 `ftps://`。
- **FilterTypeFile**：按**文件扩展名后缀**匹配。`test.torrent` 的指纹是 `torrent`。它服务于「URL 本身指向一个元数据文件」的场景：`.m3u8` 指向播放列表、`.torrent` 指向种子。
- **FilterTypeBase64**：按 **data URI 的 MIME 类型**匹配。`data:application/x-bittorrent;base64,...` 的指纹是 `application/x-bittorrent`。它服务于「文件内容直接内嵌在 URL 里」的场景：前端读完本地种子文件后把内容 base64 进 URL 提交。

所有匹配都**大小写不敏感**（实现里把 URI 和 Pattern 都转大写再比）。file 类型还有一个容易忽略的细节：**先剥掉 `?query` 和 `#fragment` 再比后缀**，否则带签名参数的 CDN 地址（`video.m3u8?sign=abc`）会路由失败。

#### 4.2.2 核心流程

`Match(uri)` 的判定逻辑用伪代码表达：

```text
upper(uri), upper(pattern)     ← 统一大写
switch Type:
  url:    upper(uri) 以 upper(pattern) + ":" 开头？      ← 冒号锁定 scheme 边界
  file:   clean = uri 截断到第一个 "?" 或 "#"
          upper(clean) 以 "." + upper(pattern) 结尾？    ← 点号锁定扩展名边界
  base64: upper(uri) 以 "DATA:" + upper(pattern) + ";BASE64," 开头？
其他:    false
```

用集合语言描述整体路由（4.3、4.4 会用到）：设管理器按注册顺序排列为 \( fm_1, fm_2, \dots, fm_n \)，则 URL \( u \) 的归属为

\[
\text{route}(u) = \min_{i} \{\, fm_i \mid \exists\, f \in fm_i.\text{Filters}(),\ f.\text{Match}(u) = \text{true} \,\}
\]

即**第一个有过滤器命中的管理器获胜**；所有管理器都未命中时返回 `ErrUnSupportedProtocol`。这是一个纯粹的「首次命中胜出」（first-match-wins）模型，没有权重、没有打分——因此注册顺序就是优先级。

#### 4.2.3 源码精读

三种类型的常量定义与注释见 [internal/fetcher/fetcher.go:86-95](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L86-L95)（注释里的示例清楚地说明了 Pattern 应填什么）。

匹配实现在 [internal/fetcher/fetcher.go:102-120](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L102-L120)：先把 `uri` 和 `Pattern` 都 `ToUpper`（L103-104，实现大小写不敏感）；url 分支用 `patternUpper+":"` 做前缀判定（L107），冒号保证 `FTP` 不会匹配 `FTPS://`；file 分支先用 `strings.IndexAny(clean, "?#")` 把查询串和片段截掉（L111-114，注释明确举例 `https://example.com/video.m3u8?sign=abc` 仍应按扩展名路由），再用 `."+patternUpper"` 做后缀判定（L115），点号保证 `torrent` 不会匹配 `mytorrent`；base64 分支要求 `DATA:` + MIME + `;BASE64,` 的完整前缀（L117）。

与 `Match` 相对应的表驱动测试已存在于 [internal/fetcher/fetcher_test.go:22-114](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher_test.go#L22-L114)，覆盖了三类匹配各一正一反共六个用例——但**没有覆盖带 `?query` 的 file 匹配**，这正是本讲实践任务要补的缺口。

顺带一提，协议实现内部还会「再解析一次 scheme」，用的是另一组工具函数：[pkg/util/url.go:11-18](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/util/url.go#L11-L18) 的 `ParseSchema` 取第一个冒号前的段并大写（`index == 1` 的特判是为了避开 Windows 盘符 `d:`），[pkg/util/url.go:21-34](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/util/url.go#L21-L34) 的 `ParseDataUri` 用正则解出 data URI 的 MIME 和解码后的字节。BT 的 `addTorrent` 正是靠它们区分三种入口（[internal/protocol/bt/fetcher.go:746-785](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/bt/fetcher.go#L746-L785)）：`MAGNET` 走磁力解析，`FILE` 走本地文件打开，`DATA` 走 base64 解码，兜底分支直接 `os.Open(req.URL)`。路由（选管理器）与分流（协议内部选处理方式）是两个层次。

还有一个佐证「base64 指纹重要到被特殊对待」的细节：MCP 服务器在校验下载 URL 时硬编码了同一个前缀判断（[pkg/mcpserver/server.go:131](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/mcpserver/server.go#L131)）——同一份路由知识在不同入口被复制了一份，阅读时值得留意。

#### 4.2.4 代码实践：补全 SchemeFilter 的表驱动测试（含 ?query 用例）

**实践目标**：亲手为 `SchemeFilter.Match` 写一组覆盖三类匹配的表驱动测试，重点补上现有测试缺失的「带 query 的 file 匹配」用例。

**操作步骤**：

1. 在你的工作副本中新建 `internal/fetcher/scheme_filter_tutorial_test.go`（**示例代码**，仅供练习，勿提交回上游）：

   ```go
   package fetcher

   import "testing"

   // 示例代码：表驱动测试，覆盖三类匹配 + ?query 剥离行为
   func TestSchemeFilterMatchTutorial(t *testing.T) {
       tests := []struct {
           name    string
           filter  SchemeFilter
           uri     string
           want    bool
       }{
           // url：scheme 前缀 + 冒号边界
           {"url hit", SchemeFilter{FilterTypeUrl, "https"}, "HTTPS://github.com/a", true},
           {"url miss other scheme", SchemeFilter{FilterTypeUrl, "https"}, "ftp://github.com", false},
           {"url exact boundary", SchemeFilter{FilterTypeUrl, "ftp"}, "ftps://h/a", false},

           // file：扩展名后缀，先剥 ?query 与 #fragment
           {"file hit plain", SchemeFilter{FilterTypeFile, "m3u8"}, "https://h/v.m3u8", true},
           {"file hit with query", SchemeFilter{FilterTypeFile, "m3u8"}, "https://h/v.m3u8?sign=abc", true},
           {"file hit with fragment", SchemeFilter{FilterTypeFile, "torrent"}, "https://h/x.torrent#frag", true},
           {"file miss query only", SchemeFilter{FilterTypeFile, "m3u8"}, "https://h/v.m3u8.cgi?sign=abc", false},
           {"file miss no dot boundary", SchemeFilter{FilterTypeFile, "torrent"}, "d:/tmp/mytorrent", false},

           // base64：data URI 的 MIME 指纹
           {"base64 hit", SchemeFilter{FilterTypeBase64, "application/x-bittorrent"},
               "data:application/x-bittorrent;base64,QUJD", true},
           {"base64 miss mime", SchemeFilter{FilterTypeBase64, "application/x-bittorrent"},
               "data:application/javascript;base64,QUJD", false},
       }
       for _, tt := range tests {
           if got := tt.filter.Match(tt.uri); got != tt.want {
               t.Errorf("%s: Match(%q) = %v, want %v", tt.name, tt.uri, got, tt.want)
           }
       }
   }
   ```

2. 运行：`go test ./internal/fetcher/ -run TestSchemeFilterMatchTutorial -v`。
3. 再运行原有测试对照：`go test ./internal/fetcher/ -run TestSchemeFilter -v`。

**需要观察的现象**：所有用例通过；特别确认 `file hit with query` 为 true、`url exact boundary`（`ftp` 过滤器对 `ftps://`）为 false。

**预期结果**：10 个用例全部 PASS。若某个用例失败，说明你对某个边界的理解和实现不一致——这恰恰是最有价值的学习信号，回到 [internal/fetcher/fetcher.go:102-120](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L102-L120) 逐行核对。

**待本地验证**：请以本机 `go test` 实际输出为准。

#### 4.2.5 小练习与答案

**练习 1**：`SchemeFilter{FilterTypeUrl, "FTP"}` 会匹配 `ftpserver://h/x` 吗？

**参考答案**：不会。url 分支比对的前缀是 `"FTP:"`（Pattern 加冒号），而 `ftpserver://` 的第四个字符是 `s` 不是 `:`。冒号是 scheme 的精确边界，这是该实现避免前缀误伤的关键。

**练习 2**：为什么 file 分支要先截断 `?` 和 `#` 再比后缀？不截断会破坏哪个真实场景？

**参考答案**：CDN / 对象存储的媒体地址普遍带签名参数（如 `video.m3u8?sign=abc&expires=...`），后缀比对的是整个字符串，若不截断，`.m3u8` 后缀判断会被 query 挡住而失败，导致 HLS 源被退化成普通 HTTP 下载（甚至路由到别的协议）。源码注释（L109-110）明确以这个场景为例。

**练习 3**：base64 分支要求前缀 `DATA:` + Pattern + `;BASE64,` 三段俱全。如果传入 `data:application/x-bittorrent,QUJD`（非 base64 的 data URI），Match 结果是什么？BT 还能处理它吗？

**参考答案**：Match 返回 false（缺少 `;BASE64,` 段），该 URL 不会路由给 BT；没有任何管理器声明过这种指纹，最终 `parseFm` 返回 `ErrUnSupportedProtocol`。BT 的 `addTorrent` 里 `ParseDataUri` 的正则同样要求 `;base64,`，即使强行到达 BT 也解不出内容（返回 nil 数据）。

### 4.3 DownloaderConfig.Init 注册顺序：顺序即优先级

#### 4.3.1 概念说明

路由规则本身只有「first-match-wins」一句话，真正微妙的是**注册顺序**。默认注册表写在 `DownloaderConfig.Init()` 里，顺序是：

```text
1. ftp   （url: FTP/FTPS/FTPES）
2. hls   （file: M3U8）
3. http  （url: HTTP/HTTPS）
4. bt    （url: MAGNET；file: TORRENT；base64: APPLICATION/X-BITTORRENT）
5. ed2k  （url: ED2K）
```

这个顺序不是按协议流行度排的，而是按**过滤器特异性**排的：

- **FTP 必须最先**。BT 声明了 `file: TORRENT`，HLS 声明了 `file: M3U8`——如果它们排在 FTP 之前，`ftp://host/x.torrent` 会被 BT 抢走（当成种子），`ftp://host/v.m3u8` 会被 HLS 抢走（当成播放列表）。但用户给出 `ftp://` 时意图非常明确：**通过 FTP 传输这个文件**，哪怕它恰好叫 `.torrent`。
- **HLS 必须在 HTTP 之前**。HTTP 声明了 `url: HTTP/HTTPS`，会吞掉一切 http(s) 地址；而 `https://host/v.m3u8` 的正确处理方式是解析播放列表而非整段单文件下载。HLS 管理器源码里专门留了注释说明这一点（见 4.3.3）。
- **HTTP 在 BT 之前**。于是 `https://host/x.torrent` 路由给 HTTP——**下载的是 `.torrent` 文件本身**。要接着发起真正的 BT 下载，靠的是 HTTP 的 `OptsExtra.AutoTorrent`（下载完成后自动创建 BT 任务，见 u5-l4），而不是路由层直接跳转。这是很多人阅读时的意外点。
- **ed2k 的 `ed2k://` scheme 无冲突**，放最后无所谓。

一句话总结：**URL scheme 是强意图（用户指定了传输方式），扩展名是弱信号（只是内容格式的暗示）**；强意图的管理器要排在前，弱信号的管理器排在后，让弱信号只兜住「scheme 没有认领」的 URL。

#### 4.3.2 核心流程

```text
用户 URL
  ├─ ftp://  ftps://  ftpes://     ──────────────→ ftp    （scheme 强意图，最先）
  ├─ 其余 URL 且 *.m3u8 结尾        ──────────────→ hls    （扩展名，需早于 http）
  ├─ http:// https://               ──────────────→ http   （通用兜底之一）
  ├─ magnet:?… / *.torrent 结尾 /   ──────────────→ bt
  │  data:application/x-bittorrent;base64,…
  ├─ ed2k://                        ──────────────→ ed2k
  └─ 都不匹配                       ──────────────→ ErrUnSupportedProtocol
```

注意 `*.torrent` 结尾要在「非 http/https/ftp」的前提下才轮到 BT——因为 http、ftp 都排在它前面并且会先命中。

#### 4.3.3 源码精读

默认注册表在 [pkg/download/model.go:231-242](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L231-L242)。注意两点：其一，整段逻辑包在 `if len(cfg.FetchManagers) == 0` 里——**调用方可以完全替换注册表**（测试里大量使用这一点，只注册单个协议的管理器，见 [pkg/download/selection_test.go:25](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/selection_test.go#L25)）；其二，`Init()` 的其余部分（[pkg/download/model.go:227-250](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L227-L250)）还补齐了默认 Controller、350ms 刷新间隔和内存存储——这些是 u4-l1 的内容。

注册表上方的注释（[pkg/download/model.go:233-235](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L233-L235)）直接回答了本讲的核心问题：*"FTP matches by URL scheme only. It comes before the managers that match by file extension, so ftp://host/x.torrent or x.m3u8 is downloaded as a file over FTP."*（FTP 只按 URL scheme 匹配。它排在按扩展名匹配的管理器之前，因此 `ftp://host/x.torrent` 或 `x.m3u8` 会作为文件通过 FTP 下载。）

HLS 侧的对应注释在 [internal/protocol/hls/fetcher_manager.go:18-20](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/hls/fetcher_manager.go#L18-L20)：*"The manager must be registered BEFORE the http manager in the downloader config: protocol routing picks the first manager whose filter matches, and the http filter claims every http(s) URL."*（该管理器必须注册在 http 管理器之前：路由选择第一个过滤器命中的管理器，而 http 过滤器会认领所有 http(s) URL。）两侧注释互相呼应，说明顺序是**刻意的架构决策**而非巧合。

顺序契约由回归测试 [pkg/download/ftp_test.go:12-33](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/ftp_test.go#L12-L33) 的 `TestTorrentNameIsFile` 锁定，它用 `NewDownloader(nil)`（走默认注册表）逐条断言路由结果——这张表就是本讲最权威的「标准答案」：

| 输入 URL | 断言路由 | 胜出的过滤器 |
| --- | --- | --- |
| `ftp://h/x.torrent` | ftp | url `FTP`（排在 BT 的 file `TORRENT` 之前） |
| `ftp://h/v.m3u8` | ftp | url `FTP`（排在 HLS 的 file `M3U8` 之前） |
| `ftps://h/x.torrent` | ftp | url `FTPS` |
| `ftpes://h/a.bin` | ftp | url `FTPES` |
| `https://h/v.m3u8` | hls | file `M3U8`（HLS 排在 HTTP 之前） |
| `https://h/x.torrent` | http | url `HTTPS`（HTTP 排在 BT 之前，下载种子文件本身） |
| `magnet:?xt=urn:btih:` | bt | url `MAGNET` |

最后，路由失败的定义在 [pkg/download/downloader.go:58](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L58)：`ErrUnSupportedProtocol = errors.New("unsupported protocol")`，API 层多处用 `errors.Is` 识别它并转成对用户友好的错误（例如 [pkg/api/service.go:304](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/api/service.go#L304) 将其映射为 404）。

#### 4.3.4 代码实践：亲手打乱顺序，观察路由翻转

**实践目标**：用实验证明「注册顺序决定路由结果」，把 `ftp://h/x.torrent` 从 FTP 手中抢给 BT。

**操作步骤**：

1. 在你的工作副本中新建 `pkg/download/routing_tutorial_test.go`（**示例代码**，仅供练习）：

   ```go
   package download

   import (
       "testing"

       "github.com/GopeedLab/gopeed/internal/protocol/bt"
       "github.com/GopeedLab/gopeed/internal/protocol/ftp"
   )

   // 示例代码：把 bt 排在 ftp 之前，.torrent 后缀就会抢走 ftp:// URL
   func TestRegistrationOrderChangesRouting(t *testing.T) {
       reversed := NewDownloader(&DownloaderConfig{
           FetchManagers: []interface{} /* 替换为 fetcher.FetcherManager */ {
               new(bt.FetcherManager),
               new(ftp.FetcherManager),
           },
       })
       fm, err := reversed.parseFm("ftp://h/x.torrent")
       if err != nil {
           t.Fatal(err)
       }
       if fm.Name() != "bt" {
           t.Fatalf("expect bt wins when registered first, got %s", fm.Name())
       }
   }
   ```

   （提示：`FetchManagers` 的元素类型是 `fetcher.FetcherManager`，写测试时请把上面的占位注释换成正确的类型与 import。）

2. 运行：`go test ./pkg/download/ -run TestRegistrationOrderChangesRouting -v`。
3. 交换两个管理器的顺序再跑一次，观察断言失败信息。

**需要观察的现象**：BT 在前时 `ftp://h/x.torrent` 路由到 `bt`；FTP 在前时（即默认顺序）路由到 `ftp`——同一个 URL、同一组过滤器，仅顺序不同结果就翻转。

**预期结果**：两种顺序各得到一次符合预期的路由结果；默认顺序下与 `TestTorrentNameIsFile` 的断言一致。

**待本地验证**：示例代码中的类型占位需要你按编译器提示修正后再运行，以本机结果为准。

#### 4.3.5 小练习与答案

**练习 1**：如果不小心把 `http` 管理器挪到 `hls` 之前，最直接的用户可见后果是什么？

**参考答案**：所有 `https://…/xxx.m3u8` 的 HLS 源都会被 HTTP 管理器的 `url HTTPS` 过滤器先命中，退化成「把 m3u8 播放列表文本当作普通文件下载」，用户拿到的将是一个几 KB 的文本文件而不是完整视频。`TestTorrentNameIsFile` 中 `https://h/v.m3u8 → hls` 的断言会立刻失败拦截这种改动。

**练习 2**：`https://example.com/ubuntu.torrent` 会路由到哪个协议？如果用户想真正下载种子内容，机制是什么？

**参考答案**：路由到 **http**（`url HTTPS` 命中，且 http 注册在 bt 之前）。先下载 `.torrent` 文件本身；若启用了 HTTP `OptsExtra.AutoTorrent`（见 u5-l4），下载完成后会自动用该种子文件再创建一个 BT 任务，形成「HTTP → BT」的两段式链路，而不是路由层一次到位。

**练习 3**：为什么默认注册表要允许调用方整体替换（`if len(cfg.FetchManagers) == 0`），而不是永远写死五个？

**参考答案**：测试与嵌入场景都需要裁剪：单协议测试只注册一个管理器可以避免 BT 初始化网络、避免无关协议干扰断言（如 [pkg/download/selection_test.go:25](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/selection_test.go#L25)）；u3-l4 我们实现自定义协议时也要把自己的管理器插进这张表。默认值只是「常见场景的便利预设」。

### 4.4 parseFm / buildFetcher：路由发生的六个时刻

#### 4.4.1 概念说明

规则（Match）与注册表（Init）都就绪后，剩下的问题是**路由在什么时候真正执行**。执行者只有两个小函数：

- `parseFm(url)`：纯查询——按注册顺序找第一个过滤器命中的管理器，找不到报 `ErrUnSupportedProtocol`。
- `buildFetcher(url)`：查询 + 构造——先 `parseFm`，再 `fm.Build()` 造出 Fetcher，再 `setupFetcher` 注入 Controller。

关键认知是：**路由不是一次性的**。任务从创建到复活的全生命周期里，`parseFm` 会在多个时刻被反复调用。原因有二：一是不同入口（解析、直建、恢复、重试）各自需要确定管理器；二是任务的 URL 可能在中途变化（扩展改写、重试换源），每次都按「当时的 URL」重新路由，保证管理器归属始终与当前 URL 一致。

#### 4.4.2 核心流程

`parseFm` 的执行模型（双重循环 + 首个命中返回）：

```text
for fm in 注册顺序:            ← 外层：管理器优先级
    for filter in fm.Filters(): ← 内层：过滤器无优先级，任一命中即可
        if filter.Match(url): return fm
return ErrUnSupportedProtocol
```

六个调用时刻（均已被源码核实）：

| # | 调用点 | 所在函数 | 场景 |
| --- | --- | --- | --- |
| 1 | `buildFetcher` 内部 | [pkg/download/downloader.go:2302](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2302) | Resolve / createDirect 建新任务 |
| 2 | `Resolve` → `buildFetcher` | [pkg/download/downloader.go:559](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L559) | 解析阶段（扩展 onResolve 可能已改写 URL） |
| 3 | `createDirect` → `buildFetcher` | [pkg/download/downloader.go:620](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L620) | 跳过解析直接建任务 |
| 4 | `doCreate` | [pkg/download/downloader.go:1828](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1828) | 任务落库前，把 `task.Protocol = fm.Name()` 绑定到任务 |
| 5 | `initOptions` | [pkg/download/downloader.go:1894](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1894) | 用协议名决定是否保留 `opts.Size` 估算 |
| 6 | `assignFetcherManager` / `resetTaskFetcher` | [pkg/download/downloader.go:2293](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2293)、[L1753](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1753) | 重启恢复旧任务 / 出错重试重建 fetcher |

#### 4.4.3 源码精读

`parseFm` 本体在 [pkg/download/downloader.go:444-453](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L444-L453)：外层按 `d.cfg.FetchManagers` 顺序遍历，内层遍历该管理器的过滤器，命中即返回；全部落空返回哨兵错误 `ErrUnSupportedProtocol`。整个函数无锁、无副作用——它只读注册表，因此可以放心地在任何调用路径上重复执行。

`buildFetcher` 在 [pkg/download/downloader.go:2301-2309](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2301-L2309)：三步曲 `parseFm → fm.Build() → d.setupFetcher(fm, fetcher)`。`setupFetcher`（[pkg/download/downloader.go:455-487](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L455-L487)）组装一个 Controller 注入给 Fetcher：临时目录、配置读取闭包（按 `fm.Name()` 取该协议的配置）、代理决策闭包（任务级代理优先于全局）、日志器，最后调用 `fetcher.Setup(ctl)`。注意配置闭包捕获了 `fm`——**路由结果直接决定了之后这个 Fetcher 读哪份协议配置**，这就是 u2-l3 里 `ProtocolConfig` 按协议名分桶的消费端。

两个主要入口对 `buildFetcher` 的使用：`Resolve` 在扩展钩子 `triggerOnResolve` 之后调用（[pkg/download/downloader.go:545-569](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L545-L569)），所以扩展改写后的 URL 才是路由依据；解析出的 fetcher 存入 `fetcherCache` 供随后的 `Create` 复用。`createDirect` 则一步到位（[pkg/download/downloader.go:617-630](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L617-L630)）：造好 fetcher、把 `req` 塞进 `Meta().Req`、经 `initOptions` 规整选项后交给 `doCreate`。

`doCreate`（[pkg/download/downloader.go:1822-1873](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1822-L1873)）在落库前**再路由一次**（L1828），并把结果写成任务的两个持久字段：`task.fetcherManager = fm` 与 `task.Protocol = fm.Name()`（L1833-1835，`Protocol` 就是 UI 任务列表里显示的协议标签）。无论 fetcher 是刚 Build 的还是从 Resolve 缓存取来的，任务的协议归属都以创建时刻的 URL 重新裁定。

恢复与重试路径：`Setup` 加载历史任务时逐个 `assignFetcherManager`（[pkg/download/downloader.go:2292-2299](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2292-L2299)），之后由 `restoreFetcher` 调 `fm.Restore()` 复活（[pkg/download/downloader.go:1797-1820](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1797-L1820)）；任务出错重试时 `resetTaskFetcher` 同样按当前 URL 重新 `parseFm` 再重建（[pkg/download/downloader.go:1749-1758](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1749-L1758)）。

最后一个容易被忽略的调用点是 `initOptions`（[pkg/download/downloader.go:1889-1899](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1889-L1899)）：当用户提供了 `opts.Size`（大小估算）时，它调用 `parseFm` 拿协议名，交给 `httpSizeOptions` 判断是否保留——只有 http 协议的正数 Size 才有意义，其余协议（包括走 HTTP 传输的 HLS）的估算会被清零。**路由的结果不止决定「用哪个 fetcher」，还决定「哪些选项有效」**。

#### 4.4.4 代码实践：跟踪一条真实 URL 的路由调用链

**实践目标**：以 `resolve → create` 两段式创建一个任务，用日志确认路由在每个时刻的执行与结果。

**操作步骤**：

1. 在你的工作副本 `pkg/download/` 下新建 `routing_trace_tutorial_test.go`（**示例代码**，仅供练习），仿照 [pkg/download/ftp_test.go:14-33](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/ftp_test.go#L14-L33) 的写法，用 `NewDownloader(nil)` 依次对下列 URL 调 `parseFm` 并 `t.Logf` 打印协议名：
   - `data:application/x-bittorrent;base64,` + 任意 base64 字符串
   - `ed2k://|file|name|10485760|hash|/`
   - `unknown://example.com/a.mp4`
2. 运行：`go test ./pkg/download/ -run RoutingTrace -v`。
3. 用编辑器跳转（或 `grep -n "parseFm" pkg/download/downloader.go`）核对 4.4.2 表格中六个调用点的行号与所在函数。

**需要观察的现象**：前两个 URL 分别打印 `bt`（base64 指纹命中）与 `ed2k`；第三个返回的错误经 `errors.Is(err, ErrUnSupportedProtocol)` 判定为 true。

**预期结果**：六个调用点全部能在当前 HEAD 找到且函数名与表格一致；三类 URL 的路由结果如上。

**待本地验证**：`unknown://` 的错误信息原文以本机输出为准。

#### 4.4.5 小练习与答案

**练习 1**：`buildFetcher` 与 `parseFm` 的关系是什么？为什么不合并成一个函数？

**参考答案**：`buildFetcher = parseFm + Build + setupFetcher`（[pkg/download/downloader.go:2301-2309](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2301-L2309)）。不合并是因为存在「只要管理器、不要新 Fetcher」的场景：`doCreate` 只需把管理器和协议名绑定到任务（fetcher 已有），`assignFetcherManager` 恢复任务时同样只要管理器（Fetcher 由 `Restore` 复活）。查询与构造分离让两者各取所需。

**练习 2**：为什么 `Resolve` 要在 `triggerOnResolve`（扩展钩子）**之后**才调用 `buildFetcher`？

**参考答案**：扩展的 onResolve 钩子可以改写任务的 URL（例如解析出真实直链，见 u9-l3）。路由依据必须是改写后的最终 URL：如果先路由再跑钩子，扩展把 `https://a/page` 改写成 `magnet:?…` 后，任务却仍绑在 HTTP 管理器上就错了。先钩子后路由保证了「URL 最终定形 → 协议归属一次裁定」。

**练习 3**：`initOptions` 里那次 `parseFm` 的用途与其他五处有何本质不同？

**参考答案**：其他五处的目的是**选出管理器/构造 Fetcher**（影响控制流）；`initOptions` 这次只取协议名字符串用于**选项语义判定**（`httpSizeOptions` 决定 `opts.Size` 估算是否保留，[pkg/download/downloader.go:1877-1883](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1877-L1883)）——它影响的是数据而非控制流，是路由信息的一种「复用」。

## 5. 综合实践：一张路由表 + 一段顺序注释

把本讲四个模块串成一个可运行的验证作业（全部在你自己的工作副本中完成，**不要提交到上游**）：

1. **写表驱动测试**：完成 4.2.4 的 `TestSchemeFilterMatchTutorial`，确保覆盖 url / file / base64 三类匹配，且必须包含「带 `?query` 的 file 匹配」用例；跑通后再对照仓库自带的 [internal/fetcher/fetcher_test.go:22-114](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher_test.go#L22-L114) 检查自己漏了哪些边界。
2. **写路由顺序测试**：仿照 [pkg/download/ftp_test.go:14-33](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/ftp_test.go#L14-L33)，在默认注册表下追加三条断言：`data:application/x-bittorrent;base64,xxx → bt`、`ed2k://|file|a|1|hash|/ → ed2k`、`unknown://h/a → ErrUnSupportedProtocol`。
3. **写顺序颠倒实验**：完成 4.3.4 的 `TestRegistrationOrderChangesRouting`，亲眼看到 `ftp://h/x.torrent` 在「bt 在前」时被 BT 抢走。
4. **写下你的注释**：在你的测试文件顶部，用 5 到 8 行中文注释回答——`ftp://host/x.torrent` 为什么走 FTP 而不是 BT？答案应包含三个要素：(a) 路由是 first-match-wins（`parseFm` 双重循环）；(b) 默认注册顺序 ftp 在 bt 之前（[pkg/download/model.go:231-242](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L231-L242)）；(c) BT 的 `file TORRENT` 过滤器确实也能匹配这个 URL，但永远轮不到它——scheme 是强意图、扩展名是弱信号。

**验收标准**：三条命令 `go test ./internal/fetcher/ -run TestSchemeFilter -v`、`go test ./pkg/download/ -run 'Routing|TorrentNameIsFile' -v`、`go vet ./internal/fetcher/ ./pkg/download/` 全部通过，且你的注释能向另一位读者讲清顺序敏感性。

## 6. 本讲小结

- **FetcherManager 是协议级单例门面**：身份（Name）、路由声明（Filters）、工厂（Build/ParseName/AutoRename）、持久化契约（Store/Restore）四类职责集于一身；Fetcher 才是任务级的有状态执行者。
- **SchemeFilter 有三种指纹**：url（scheme 前缀 + 冒号边界）、file（剥 `?query`/`#fragment` 后的扩展名后缀）、base64（data URI 的 MIME 前缀），全部大小写不敏感。
- **路由是 first-match-wins**：`parseFm` 按注册顺序双重循环，首个命中者胜出，全不命中报 `ErrUnSupportedProtocol`——注册顺序就是优先级，无权重无打分。
- **顺序是刻意的架构决策**：FTP 最前（scheme 强意图优先于扩展名弱信号，`ftp://host/x.torrent` 必须走 FTP）、HLS 在 HTTP 前（`.m3u8` 不能被通用 HTTP 吞掉）、HTTP 在 BT 前（`https://…​.torrent` 先下载种子文件本身，AutoTorrent 再链式建 BT 任务）；该顺序由 `TestTorrentNameIsFile` 回归锁定。
- **路由贯穿任务全生命周期**：`parseFm` 在 Resolve、createDirect、doCreate（写 `task.Protocol`）、initOptions（决定 Size 估算有效性）、重启恢复、错误重试共六个时刻被调用，且永远以「当时的 URL」为准（扩展改写后才路由）。
- **路由结果向外辐射**：选中的 `fm.Name()` 同时决定协议配置分桶（setupFetcher 的配置闭包）、UI 协议标签（`task.Protocol`）与部分选项语义（`httpSizeOptions`）。

## 7. 下一步学习建议

本讲回答了「URL 如何选中协议」，接下来两条线自然展开：

1. **下一讲 u3-l3（Controller 与共享基础服务）**：`buildFetcher` 最后一步 `setupFetcher` 注入的 Controller 到底提供了什么？临时文件、配置读取、代理决策、日志如何被协议使用？这是理解协议实现如何「借用」Downloader 能力的关键。
2. **u3-l4（实战：实现一个自定义协议 Fetcher）**：把本讲的知识用起来——实现一个 `mock://` 协议的 FetcherManager 与 Fetcher，亲手把它插进 `FetchManagers` 注册表，验证你自己写的过滤器参与 first-match-wins 路由。
3. **延伸阅读**：带着「顺序敏感性」的眼光回头看 [pkg/download/model.go:227-250](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L227-L250) 的默认注册表与两处顺序注释（[model.go:233-235](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L233-L235)、[hls/fetcher_manager.go:18-20](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/hls/fetcher_manager.go#L18-L20)），再跑一遍 `TestTorrentNameIsFile`，确认你能预测每一行的断言值。
