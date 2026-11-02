# u6-l1 BT 客户端、种子与磁力解析

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 Gopeed 为什么让**所有 BT 任务共享一个 torrent 客户端**，以及这个共享设计带来的生命周期与并发问题。
2. 说出 `initClient` 的「必须持锁调用」约定，以及提交 `cd19772` 新增的 `addTorrentSpec` 如何把锁的覆盖范围从「客户端初始化」延伸到「种子添加完成」，从而消除 `closeClient` 的竞态窗口。
3. 掌握 `addTorrent` 中种子文件（本地路径 / `file://` / `data:` URI）与磁力链接四条解析分支的差异，尤其是 InfoHash 的两种获取方式。
4. 解释 `torrentFileLayout` 与 `updateRes` 如何把种子元数据映射为 Gopeed 通用的 `Resource` 文件列表。

本讲是 BT 系列的第一讲。在动手之前，请确认你已学习 u3-l1（Fetcher 接口契约）与 u4-l2（Downloader 主链路），知道 `Resolve` 是 Downloader 在建任务前的资源探测步骤。

## 2. 前置知识

### 2.1 BitTorrent 的几个核心概念

- **种子文件（.torrent / metainfo）**：一个 bencode 编码的小文件，包含「info 字典」（文件名、文件列表、每片长度等）与 tracker 列表。**info 字典的 SHA-1 哈希就是 InfoHash**，它唯一标识一个资源。
- **InfoHash**：种子的「身份证」。两个内容相同的种子文件，即使 tracker 列表不同，InfoHash 也相同。
- **磁力链接（magnet URI）**：形如 `magnet:?xt=urn:btih:<InfoHash>&dn=<名字>` 的 URI。它**只携带 InfoHash 和可选的显示名**，不携带文件列表——文件列表（元数据）需要先从其他 peer 处下载回来。
- **tracker / DHT / PEX**：三种发现 peer 的途径。tracker 是中心服务器；DHT 是分布式哈希表（无需中心服务器）；PEX 是已连接 peer 之间互相交换 peer 列表。三者可以同时工作。
- **做种（seed）**：下载完成后继续上传给别人。

### 2.2 anacrolix/torrent 库

Gopeed 没有从零实现 BT 协议，而是使用 [`anacrolix/torrent`](https://github.com/anacrolix/torrent) 这个 Go 库。本讲会频繁接触它的三个类型：

| 类型 | 含义 |
| --- | --- |
| `torrent.Client` | BT 客户端。管理监听端口、DHT 节点、所有种子的连接。**创建成本高**（要绑定端口、启动 DHT）。 |
| `torrent.TorrentSpec` | 「添加一个种子」的请求单：可以来自磁力 URI，也可以来自 metainfo。 |
| `torrent.Torrent` | 添加之后得到的种子句柄，每个任务一个，用于控制下载、查询 piece 状态。 |

### 2.3 并发预备知识：check-then-act 竞态

「先检查条件、再执行动作」如果不在同一个临界区里，两个协程就可能交错执行，导致动作建立在过期条件之上。本讲的核心修复正是把「检查 client 是否存在 → 初始化 → 添加种子」合并进同一个临界区。另外还要理解 Go 的一个细节：`go f(x)` 的参数 `x` 在 **go 语句执行时求值**（值快照），而 `go func() { f(x) }()` 的闭包在**协程真正运行时**才读取 `x`（变量引用）——两者在 `x` 是可变全局变量时行为完全不同。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [internal/protocol/bt/fetcher.go](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go) | BT Fetcher 主体：共享客户端生命周期、addTorrent 解析分支、Resource 映射、FetcherManager。 |
| [internal/protocol/bt/fetcher_test.go](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher_test.go) | 本讲的实践素材：解析测试、ParseName 表驱动测试、`cd19772` 新增的三个竞态回归测试。 |
| [internal/protocol/bt/config.go](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/config.go) | BT 协议配置：tracker 订阅、做种参数、监听端口、DHT/PEX 开关。 |
| [internal/protocol/bt/dns_cache_resolver.go](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/dns_cache_resolver.go) | tracker 域名解析缓存，附带一个由 `closeCtx` 控制的后台刷新协程。 |
| [internal/protocol/bt/testdata/test.torrent](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/testdata/test.torrent) | 多文件测试种子（根名 `test`，3 个文件），无需网络即可解析。 |
| [pkg/util/url.go](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/util/url.go) | `ParseSchema` / `ParseDataUri` 等工具函数，addTorrent 分支的判据。 |
| [pkg/download/model.go](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/model.go) | `DownloaderConfig.Init` 中五个协议管理器的默认注册顺序。 |

## 4. 核心概念与源码讲解

### 4.1 共享 BT 客户端：initClient / closeClient 生命周期

#### 4.1.1 概念说明

HTTP Fetcher 每个任务自建连接、互不干扰（见 u5 系列）；BT 的处境完全不同：一个 `torrent.Client` 要绑定监听端口、维护 DHT 节点表，多个任务挂在同一个客户端上还能共享 peer 与连接。因此 Gopeed 的 BT 实现采用了**进程级单例客户端**：`internal/protocol/bt` 包里有几个包级变量，全进程只有一份。

由此产生两个必须回答的问题：

1. **谁来建、谁来关？** 建是懒加载（第一次需要时），关是进程退出时统一关。
2. **配置什么时候生效？** 客户端只建一次，那么「建的时候读到的配置」就终身有效——改配置必须等客户端重建后才生效。

而每个 `Fetcher`（每个任务一个）只持有自己的 `torrent.Torrent` 句柄，任务关闭时只「丢掉」自己的种子，**不动共享客户端**。

#### 4.1.2 核心流程

```text
第一个 BT 任务 Resolve
        │
        ▼
addTorrentSpec（持 lock，见 4.2）
        │
        ├─ client == nil ──► initClient：构建 cfg ──► torrent.NewClient
        │                     └─ 创建 closeCtx/closeFunc
        │                     └─ go dnsResolver.Run(closeCtx)
        │
        └─ client != nil ──► 直接复用
后续任务：同样走 addTorrentSpec，client 已存在则直接添加种子

进程退出 / Downloader.Close
        │
        ▼
FetcherManager.Close ──► closeClient（持 lock）
        ├─ closeFunc()        取消 closeCtx（dnsResolver 协程退出）
        ├─ client.Close()     关闭客户端（所有种子随之关闭）
        └─ client/closeCtx/closeFunc 置 nil（下次可重建）
```

单任务关闭（`Fetcher.Close`）只做 `torrent.Drop()`，不影响共享客户端——这是「任务生命周期」与「客户端生命周期」的分离。

#### 4.1.3 源码精读

先看包级共享状态，注意这五 个变量全进程一份，`lock` 保护它们的读写：

[internal/protocol/bt/fetcher.go:29-35](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L29-L35) 声明了五个共享变量：`cfg`（客户端配置）、`client`（客户端实例）、`lock`（保护它们的互斥锁）、`closeCtx/closeFunc`（客户端级别的取消对，用于停掉 DNS 刷新协程）。

`initClient` 构建 anacrolix 客户端：

[internal/protocol/bt/fetcher.go:70-95](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L70-L95) 是初始化逻辑：第 70 行的注释 `// initClient must be called with lock held.` 是 `cd19772` 加上的**调用契约**——函数自己不再加锁，由调用方保证持锁。函数体依次：已存在则直接返回（L72-74）；设置 `Seed = true`（下载完自动做种）、Bep20 peer 标识（形如 `-GP0140-`，由 [parseBep20](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L990-L997) 把版本号补零生成）、监听端口、DHT/PEX 开关（L76-82，全部读自 `f.config`，也就是 **Setup 时注入的配置快照**）；把 `DnsCacheResolver` 挂到 `TrackerDialContext`（L84-85）；创建客户端（L86）；最后创建 `closeCtx/closeFunc` 并启动 DNS 刷新协程（L91-93）。

第 92-93 行的注释与写法是 `cd19772` 的修复之一：

[internal/protocol/bt/fetcher.go:91-93](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L91-L93) 注释说明：`closeClient` 可能在协程真正运行前就把 `closeCtx` 重置，所以把它**作为参数传入**（`go dnsResolver.Run(closeCtx)`）。对比旧写法 `go func() { dnsResolver.Run(closeCtx) }()`：闭包捕获的是变量引用，协程运行时才读 `closeCtx`，若期间 `closeClient` 已把它置为 `nil`，`Run` 内部的 `<-ctx.Done()` 会在 nil 接口上直接 panic（见 [dns_cache_resolver.go:38-49](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/dns_cache_resolver.go#L38-L49)，`Run` 是 `select <-ctx.Done()` 与定时 `resolver.Refresh` 的循环）；而 `go f(x)` 的参数在 go 语句处求值，协程拿到的一定是「为本次客户端创建的那个 context」。

`closeClient` 做对称的清理：

[internal/protocol/bt/fetcher.go:881-898](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L881-L898) 先 `closeFunc()` 取消 context（DNS 协程退出），再 `client.Close()` 关闭客户端（注意它只在 `client != nil` 时动作，并对 `closeFunc == nil` 也安全——这让「没有客户端时调用关闭」不报错，这正是回归测试 `TestFetcher_CloseWithoutClient` 验证的行为），最后把三个变量置 nil，允许下次懒加载重建。

共享客户端的关闭入口在协议管理器上，而管理器的 `Close` 由 Downloader 在关闭序列里统一调用：

[internal/protocol/bt/fetcher.go:986-988](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L986-L988) `FetcherManager.Close` 直接转发到 `closeClient`。而 [pkg/download/downloader.go:1575-1584](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/downloader.go#L1575-L1584) 显示 Downloader 关闭时会把每个 `FetcherManager` 的 `Close` 收进关闭清单依次执行——这回答了「谁来关」。

与之对照，单个任务的关闭非常克制：

[internal/protocol/bt/fetcher.go:227-246](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L227-L246) `Fetcher.Close` 用 `closeOnce` 保证幂等，只取消任务自己的 `torrentDropCtx`、调用 `safeDrop()`（`torrent.Drop()`，带 recover 防御 anacrolix 库的已知 panic）——全程不碰共享客户端。

「配置只在客户端构建时读取」在配置结构上有明确注释：

[internal/protocol/bt/config.go:26-36](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/config.go#L26-L36) `ListenPort`、`DisableDHT`、`DisablePEX` 的注释写着 "they are read when the shared client is built"——修改这三项后，需要客户端重建（`closeClient` → 下次 `initClient`）才生效。做种参数（`SeedKeep/SeedRatio/SeedTime`）与 tracker 配置则是每次 `addTorrent`/做种循环实时读取的，两者生效时机不同。

#### 4.1.4 代码实践

1. **实践目标**：验证「DHT/PEX 配置只在客户端构建时读取」——即先关掉客户端，再改配置，新客户端才会带上新配置。
2. **操作步骤**：阅读并运行测试 `TestFetcher_PeerDiscoveryDisabledOnClient`：

   ```bash
   go test -v -run 'TestFetcher_PeerDiscoveryDisabledOnClient' ./internal/protocol/bt/
   ```

   对应源码 [internal/protocol/bt/fetcher_test.go:472-486](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher_test.go#L472-L486)。注意测试的第一行就是 `closeClient()`——先把共享客户端拆掉，再用 `disableDht/disablePex` 配置构造 Fetcher 并 Resolve，最后断言 `cfg.NoDHT` 为 true 且 `client.DhtServers()` 数量为 0。
3. **需要观察的现象**：如果删掉开头的 `closeClient()`（不要真的改源码，只在脑中推演，或拷贝一份测试改名实验），当测试套件之前已有测试初始化过客户端时，断言会失败——因为旧客户端带着旧配置还活着。
4. **预期结果**：测试原样通过（该测试在 CI 中运行）；推演结论：这三个配置项的生效前提是「共享客户端在本配置之后重建」。（实际运行输出待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Fetcher.Close()` 不顺手把 `client` 也关了？
**答案**：客户端是进程级共享的，其他正在下载/做种的任务还挂着它；单个任务关闭只应 `Drop` 自己的种子。共享客户端的关闭权在 `FetcherManager.Close`（由 Downloader 关闭序列统一调用），此时所有任务都已停止。

**练习 2**：`initClient` 里 `cfg.TrackerDialContext = dnsResolver.DialContext` 与 `go dnsResolver.Run(closeCtx)` 分别解决什么问题？
**答案**：前者把 tracker 域名解析接到带内存缓存的 resolver 上（缓存 DNS 结果，拨号时先查缓存再拨 IP，见 [dns_cache_resolver.go:18-36](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/dns_cache_resolver.go#L18-L36)）；后者启动一个每 5 分钟强制刷新缓存的协程，其生命周期与客户端绑定，由 `closeCtx` 取消。

**练习 3**：`TestFetcher_CloseWithoutClient` 里「restored task」子测试为什么要在 `client == nil` 的前提下调用 `Close`？
**答案**：模拟「任务从磁盘恢复但从未启动」的场景——恢复路径只重建 Fetcher 骨架（见 u4-l5 惰性恢复），客户端尚未初始化，此时用户直接删除任务触发 `Close`，必须不报错。`closeClient` 对 nil 的防御保证了这一点。

### 4.2 addTorrentSpec 持锁窗口：构建与关闭之间的竞态修复（cd19772）

#### 4.2.1 概念说明

这是本讲最重要的一节。旧代码（上一 HEAD `1e36599`）的结构是：

- `initClient` **自己加锁**：进来先 `lock.Lock()`；
- `addTorrent` 在函数开头调用 `f.initClient()`（初始化完就释放锁了）；
- 解析完种子后，**不持锁**调用 `client.AddTorrentSpec(spec)`。

问题在于：从「client 初始化完成」到「种子添加完成」之间存在一段**无锁窗口**。此时另一个协程（例如 Downloader 关闭、或另一个测试任务触发 `FetcherManager.Close`）调用 `closeClient()`：它会 `client.Close()` 并把 `client` 置 nil。于是 `AddTorrentSpec` 要么作用在已关闭的客户端上（报错），要么直接 nil 解引用（panic）。

修复思路不是「给 AddTorrentSpec 也加锁」这么简单，而是**重新划分临界区**：把锁的边界从「初始化」拉宽到「初始化 + 添加种子」一个原子区间。为此 `initClient` 改为「必须持锁调用」的约定形式，新增 `addTorrentSpec` 作为唯一对外入口。

#### 4.2.2 核心流程

旧代码的竞态时序（修复前）：

```text
协程 A（Resolve 某任务）                      协程 B（closeClient）
─────────────────────────────                ─────────────────────────────
addTorrent:
  initClient()  ← 自己加锁，建完 client 即放锁
  解析种子文件、合并 tracker（耗时 I/O）
                                              lock.Lock()
                                              closeFunc(); client.Close()
                                              client = nil
                                              lock.Unlock()
  client.AddTorrentSpec(spec)  ← 无锁！
    → client 为 nil 或已关闭 → panic / 报错
```

新代码的串行化时序（修复后）：

```text
协程 A                                      协程 B
─────────────────────────────              ──────────────────────
addTorrentSpec:
  lock.Lock()
    initClient()      ← 约定：调用方已持锁
    client.AddTorrentSpec(spec)
  lock.Unlock()
                                            closeClient: lock.Lock() ← 被阻塞
                                            只能等 A 的「建+加」完整结束后再关

→ 两种合法交错：
  ① 先加后关：种子已添加成功，随后被关闭 → addTorrent 在 GotInfo
     等待处收到 torrent.Closed()，返回被妥善处理的错误
     "torrent closed during resolution"
  ② 先关后加：client 为 nil → initClient 用（可能的）新配置重建客户端
```

注意修复**没有**（也无法）消灭所有错误返回：种子添加成功之后、元数据等待期间客户端仍可能被关闭，这条路径以明确的错误字符串暴露给上层（测试也据此区分「可接受的关闭」与「意外错误」）。

#### 4.2.3 源码精读

新函数及其设计意图直接写在注释里：

[internal/protocol/bt/fetcher.go:97-107](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L97-L107) `addTorrentSpec` 的注释（L97-98）说明它「从客户端初始化到种子添加完成一直持有 lock，使 closeClient 无法在中间关闭或重置客户端」。函数体：持锁（L100-101）→ 在锁内调用 `initClient`（L103-105，这就是 4.1 节那条「must be called with lock held」契约的唯一生产调用方）→ 锁内完成 `client.AddTorrentSpec(spec)`（L106）。返回的 `bool` 表示「种子是否是新添加的」——若共享客户端里已有同 InfoHash 的活跃种子则返回 false。

配套的调用方改动在 `addTorrent` 末尾：

[internal/protocol/bt/fetcher.go:832-838](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L832-L838) 旧代码是先在函数开头 `initClient`、末尾裸调 `client.AddTorrentSpec(spec)`；新代码删掉了开头的初始化，把末尾换成 `f.addTorrentSpec(spec)`——初始化被推迟到「一切解析工作就绪」之后，顺带带来一个行为改进：**解析失败（例如种子文件不存在）不再触发共享客户端的初始化**。

「添加成功但随后被关」的受控错误：

[internal/protocol/bt/fetcher.go:845-855](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L845-L855) 添加完成后等待元数据的三路 `select`：`GotInfo()`（拿到元数据，继续）、`torrentDropCtx.Done()`（本任务被暂停/关闭）、`torrent.Closed()`（共享客户端被关，返回 `errors.New("torrent closed during resolution")`）。这个字符串会被回归测试「豁免」。

用 git 可以直接对照修复前后的差异：

```bash
git show 1e36599:internal/protocol/bt/fetcher.go | sed -n '67,105p'
```

你会看到旧版 `initClient` 开头的 `lock.Lock(); defer lock.Unlock()`、一个在函数末尾被立即覆盖的 `if closeCtx == nil {...}` 守卫（已被删除的冗余逻辑），以及捕获变量的 `go func() { dnsResolver.Run(closeCtx) }()`。

#### 4.2.4 代码实践

1. **实践目标**：运行 `cd19772` 附带的三个竞态回归测试，逐个说清它们复现的场景，并解释持锁窗口如何消除竞态。
2. **操作步骤**：

   ```bash
   go test -race -v \
     -run 'TestFetcher_CloseAfterInit|TestFetcher_CloseWithoutClient|TestFetcher_ResolveWhileClientCloses' \
     ./internal/protocol/bt/
   ```

   三个测试的源码：
   - [internal/protocol/bt/fetcher_test.go:75-92](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher_test.go#L75-L92) `TestFetcher_CloseAfterInit`；
   - [internal/protocol/bt/fetcher_test.go:94-120](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher_test.go#L94-L120) `TestFetcher_CloseWithoutClient`；
   - [internal/protocol/bt/fetcher_test.go:122-149](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher_test.go#L122-L149) `TestFetcher_ResolveWhileClientCloses`。
3. **需要观察的现象与场景解读**：
   - `CloseAfterInit`：100 次「持锁 initClient → closeClient」循环。它同时校验两件事——新调用约定（测试自己 `lock.Lock()` 后再调 `initClient`，若 `initClient` 还像旧版那样内部加锁会**自锁死**）与高频建/关之下 DNS 协程拿到正确 context（旧闭包写法下 `closeCtx` 可能已被置 nil）。
   - `CloseWithoutClient`：两个子场景——「恢复出来但从未启动的任务」直接 Close（客户端为 nil），以及「解析失败的任务」Close（新代码下解析失败根本不会初始化客户端）。两者都必须无错误返回。
   - `ResolveWhileClientCloses`：两个协程并发地对两个种子各做 10 轮「Resolve → f.Close → closeClient」。注释点明被容忍的错误只有 `"torrent closed during resolution"`——即 4.2.2 时序图中的合法交错①（添加成功后被关）；除此之外的任何错误（如旧代码下的 nil 解引用、非法内存访问）都会让测试失败。
4. **预期结果**：三个测试全部通过，`-race` 不报警。若想进一步体会差异，可在临时 worktree 中 checkout 旧提交 `1e36599`，对旧代码跑同一组测试并观察失败形态（做完记得删掉 worktree，不要改动工作区）。（实际运行输出待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：为什么不把 `lock` 的范围进一步扩大到「等待 GotInfo」？那样不是更安全吗？
**答案**：不行。磁力链接的 `GotInfo` 需要从 peer 下载元数据，可能耗时数秒甚至失败。持锁等待会把 `closeClient` 与所有其他任务的 `addTorrentSpec` 全部阻塞在一个长 I/O 上，共享客户端反而变成全局瓶颈。正确划分是：**锁只保护内存状态的原子转移**（建客户端 + 登记种子），网络等待放在锁外并用 `select` 处理关闭事件。

**练习 2**：`addTorrentSpec` 返回的 `added == false` 意味着什么？上层如何处理？
**答案**：共享客户端中已存在同 InfoHash 的活跃种子（anacrolix 按 InfoHash 去重）。上层在 [fetcher.go:839-842](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L839-L842) 将 `f.torrent` 置 nil 并返回错误 `"torrent is already active"`——即同一种子不允许在进程内同时下载两份。

**练习 3**：`go dnsResolver.Run(closeCtx)` 与 `go func() { dnsResolver.Run(closeCtx) }()` 在本场景下的本质区别是什么？
**答案**：前者的实参在 go 语句执行时求值，协程获得的是当时那个 context 的**值**；后者的闭包捕获的是**变量**，协程运行时才读取。`closeClient` 会把包级变量 `closeCtx` 置 nil/换新，若协程迟到一步，闭包版本就会把 nil 或错误的 context 传给 `Run`（nil 上调用 `Done()` 直接 panic）。

### 4.3 Resolve 与 addTorrent：种子文件和磁力链接的四条解析分支

#### 4.3.1 概念说明

回忆 u3-l1：`Resolve` 的职责是**资源发现**——不开始下载，只产出 `Resource`（有什么文件、多大）。BT 的资源发现分两条本质不同的路：

- **种子文件路径**：元数据就在本地（文件里），解析是纯本地的 I/O，InfoHash 由库从 info 字典计算得出，文件列表立即可得。
- **磁力链接**：本地只有一个 InfoHash 和显示名，`Resource` 的文件列表必须等 peer 把元数据传回来（`GotInfo`）之后才能构造——这一步需要真实网络。

`addTorrent` 还要处理种子内容的三种「容器」：本地路径、`file://` URL、`data:` URI（base64 内嵌种子，扩展安装场景常用）。合起来就是四条读取分支 + 一条磁力分支。

#### 4.3.2 核心流程

`addTorrent` 的伪代码：

```text
addTorrent(req):
    读取协议配置, 解析 ReqExtra(可选 Trackers)
    schema = ParseSchema(req.URL)          # 冒号前缀大写化；无冒号 → ""
    if schema == "MAGNET":
        spec = TorrentSpecFromMagnetUri(url)      # 分支①：哈希来自 xt 参数
    else:
        switch schema:
        case "FILE":  打开 file:// 路径的本地文件  # 分支②
        case "DATA":  解码 data:...;base64 内容    # 分支③
        default:      直接 os.Open(req.URL)        # 分支④：相对/绝对本地路径
        metaInfo = metainfo.Load(reader)          # 容忍 "expected EOF"（上游库问题）
        info = metaInfo.UnmarshalInfo()           # 私有种子标记在此读取
        spec = TorrentSpecFromMetaInfoErr(metaInfo)
    if 非私有种子:
        合并去重 tracker = ReqExtra.Trackers ∪ 配置.effectiveTrackers()
    spec.Storage = deferredFileStorage             # 先不落盘，Start 时再绑定目录
    torrent, added = addTorrentSpec(spec)          # 持锁窗口（4.2 节）
    if !added: 返回 "torrent is already active"
    torrent.DisallowDataDownload()                 # Resolve 阶段禁止下数据
    select GotInfo / 任务取消 / 客户端关闭           # 磁力在此等元数据
    torrentReady = true
```

之后 `Resolve` 调用 `updateRes()`（4.5 节）把种子元数据翻译成 `Resource`。

#### 4.3.3 源码精读

`Resolve` 的骨架：

[internal/protocol/bt/fetcher.go:109-124](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L109-L124) 持 `f.mu`（任务级锁，与包级 `lock` 是两把不同的锁）；若此前已解析过（重复 Resolve 或 Start 补挂），先 `safeDrop` 旧种子再重来；核心两步 `addTorrent` + `updateRes`。

分支判据来自工具函数：

[pkg/util/url.go:11-18](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/util/url.go#L11-L18) `ParseSchema` 取第一个冒号之前的部分并大写；无冒号或单字符前缀（Windows 盘符 `C:`）返回空串——所以 `"./testdata/test.torrent"` 走 default 分支。磁力分支与三个种子读取分支：

[internal/protocol/bt/fetcher.go:757-788](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L757-L788) `MAGNET` 分支（L760-764）调用 `torrent.TorrentSpecFromMagnetUri`——**InfoHash 直接从 `xt=urn:btih:...` 参数解析，零网络、零本地文件**；`FILE` 分支（L767-778）解析 `file://` URL 并处理 Windows 盘符前导斜杠的怪癖；`DATA` 分支（L779-781）用 [ParseDataUri](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/util/url.go#L20-L34) 解出 base64 种子内容；default 分支（L782-787）把 URL 当本地路径直接 `os.Open`。

种子文件统一进入 metainfo 解析：

[internal/protocol/bt/fetcher.go:790-809](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L790-L809) `metainfo.Load` 带 "expected EOF" 容错（上游库 issue #992 的热修复注释）；`UnmarshalInfo` 检出私有种子标记（私有种子禁止追加外部 tracker，见 L811-812 的条件）；最后 `TorrentSpecFromMetaInfoErr` 把 metainfo 变成 spec——**这条路径上 InfoHash 由库对 info 字典做哈希计算得出**，与磁力的「从 URI 读取」形成对照。

tracker 合并与添加：

[internal/protocol/bt/fetcher.go:811-844](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L811-L844) 用 map 对「请求级 Trackers + 配置订阅的 effectiveTrackers」去重后塞进 spec；`spec.Storage` 挂上 `deferredFileStorage`（目录绑定推迟到 Start，Resolve 阶段不写盘）；然后调用 4.2 节的 `addTorrentSpec`；`DisallowDataDownload()` 确保解析阶段只拿元数据不搬数据。

等待元数据的三路 select：

[internal/protocol/bt/fetcher.go:845-855](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L845-L855) 注释解释了为什么 tracker 必须在等待元数据**之前**加入 spec：磁力链接缺元数据时，外部 tracker 可能是发现「能提供元数据的 peer」的唯一途径。

补充一个跨重启的细节：恢复的任务并不在 Restore 时重新解析，而是推迟到 `Start`：

[internal/protocol/bt/fetcher.go:135-139](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L135-L139) `Start` 发现 `torrentReady` 为 false 时重新走 `addTorrent(f.meta.Req)`——这正是 u4-l5「惰性恢复」在 BT 上的落点。

#### 4.3.4 代码实践

1. **实践目标**：亲手解析仓库自带的 `testdata/test.torrent`，打印生成的 `Resource` 文件列表与大小；再对比磁力链接上 InfoHash 的获取方式。
2. **操作步骤**：`internal/` 包不能被模块外引用，所以实践以包内测试的形式进行。新建 `internal/protocol/bt/resolve_practice_test.go`（这是你自己的练习文件，不属于项目源码）：

   ```go
   package bt

   import (
     "encoding/json"
     "testing"

     "github.com/GopeedLab/gopeed/pkg/base"
   )

   func TestPractice_ResolveTorrent(t *testing.T) {
     f := buildFetcher(t) // 复用 fetcher_test.go 里的构造器
     if err := f.Resolve(&base.Request{URL: "./testdata/test.torrent"}); err != nil {
       t.Fatal(err)
     }
     res := f.Meta().Res
     out, _ := json.MarshalIndent(res, "", "  ")
     t.Logf("Resource:\n%s", out)
     for i, file := range res.Files {
       t.Logf("file[%d] name=%q path=%q size=%d", i, file.Name, file.Path, file.Size)
     }
   }
   ```

   ```bash
   go test -v -run TestPractice_ResolveTorrent ./internal/protocol/bt/
   ```

3. **需要观察的现象**：日志中的文件列表、总大小、Hash 字段。磁力对照：阅读 [internal/protocol/bt/fetcher_test.go:243-289](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher_test.go#L243-L289) 的 `TestFetcherManager_ParseName`，注意磁力 URI 不 Resolve 也能提取显示名（`dn` 参数优先，退化为 `xt` 里的哈希串）——因为 InfoHash 就写在 URI 里，不需要任何网络。
4. **预期结果**：`test.torrent` 是多文件种子，期望输出与现有断言 [internal/protocol/bt/fetcher_test.go:196-216](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher_test.go#L196-L216) 一致：根名 `test`、总大小 `107484864`、三个文件 `c.txt`（子目录 `path`，98501754）、`b.txt`（8904996）、`a.txt`（78114）、Hash `ccbc92b0cd8deec16a2ef4be242a8c9243b1cedb`。若把 URL 换成磁力链接做完整 Resolve，则会在 `GotInfo` 处等待 peer 提供元数据，需要真实网络且耗时不可控（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`https://example.com/x.torrent` 这个 URL 会被 BT Fetcher 的 `addTorrent` 四条分支命中吗？
**答案**：不会直接命中。路由阶段（u3-l2）HTTP 管理器排在 BT 之前，`.torrent` 文件会被 HTTP 协议当作普通文件下载；下载完成后由 HTTP OptsExtra 的 `AutoTorrent` 联动（u5-l4）再链式创建 BT 任务。BT 自己的四条分支处理的是本地路径、`file://`、`data:` 与 `magnet:`。

**练习 2**：私有种子（private torrent）在 tracker 合并时被跳过，为什么？
**答案**：私有种子依赖自家 tracker 做准入与统计，向其暴露额外 peer 来源（公共 tracker/DHT/PEX）会破坏其私密性与统计模型，因此 [fetcher.go:811-812](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L811-L812) 只对非私有种子追加外部 tracker。

**练习 3**：`data:application/x-bittorrent;base64,...` 形式的请求从哪来？
**答案**：常见于扩展系统——扩展脚本在内存里拿到种子字节流，不想落盘临时文件，就编码成 data URI 直接交给下载器。现有测试 [TestFetcher_Resolve_DataUri_Torrent](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher_test.go#L25-L54) 把 ubuntu 种子转成 data URI 后 Resolve，断言与原文件解析结果（含 Hash）完全一致。

### 4.4 Filters 匹配规则：哪些 URL 会交给 BT

#### 4.4.1 概念说明

FetcherManager 的 `Filters` 是协议的「路由声明」（u3-l2 讲过匹配算法与注册顺序）。BT 声明了三类指纹，分别对应三种入口形态：`magnet:` scheme、`.torrent` 扩展名、`data:` URI 中的 `application/x-bittorrent` MIME。理解这三类过滤与注册顺序的配合，就能回答「一个 URL 最终归谁管」。

#### 4.4.2 核心流程

```text
URL → parseFm 按注册顺序遍历管理器 → 每个管理器依次试自己的 Filters
  ftp   ：仅 scheme 匹配（ftp://）
  hls   ：仅扩展名匹配（.m3u8）
  http  ：scheme（http/https）+ 扩展名兜底
  bt    ：① url 类型   前缀 magnet:
         ② file 类型   扩展名 .torrent（先剥 ?query/#fragment）
         ③ base64 类型 data URI 的 MIME == application/x-bittorrent
  ed2k  ：scheme（ed2k://）
首个命中者胜出（first-match-wins）；全不命中 → ErrUnSupportedProtocol
```

#### 4.4.3 源码精读

BT 的路由声明：

[internal/protocol/bt/fetcher.go:907-922](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L907-L922) 三条 `SchemeFilter`：`FilterTypeUrl + MAGNET`（scheme 强意图）、`FilterTypeFile + TORRENT`（扩展名弱信号）、`FilterTypeBase64 + APPLICATION/X-BITTORRENT`（data URI 的 MIME）。三类匹配的具体语义（大小写不敏感、扩展名先剥 query 等）在 u3-l2 已精读，此处不重复。

注册顺序即优先级：

[pkg/download/model.go:251-262](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/pkg/download/model.go#L251-L262) `DownloaderConfig.Init` 的默认注册顺序为 ftp → hls → http → bt → ed2k，注释（L253-255）解释了 ftp 必须排在按扩展名匹配的协议之前：`ftp://host/x.torrent` 应作为 FTP 文件下载，而不是被 BT 抢走。BT 排在 http 之后，因此 `https://.../x.torrent` 先归 HTTP。

磁力链接的「解析前显示名」：

[internal/protocol/bt/fetcher.go:928-944](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L928-L944) `ParseName` 为磁力链接提供无网络的名字提取：优先 `dn` 参数，其次取 `xt` 冒号列表的最后一段（即哈希），都没有则返回空串。这让 UI 在元数据到达前就能显示一个有意义的名字（u4-l2 讲过 Downloader 用它生成任务名）。

另外注意 [AutoRename](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L946-L948) 返回 false：BT 资源自带根名（torrent 的 info name），不需要 HTTP 那种基于 URL/响应头的重命名。

#### 4.4.4 代码实践

1. **实践目标**：建立「URL → 协议」的快速判断能力，并用现有测试验证 `ParseName`。
2. **操作步骤**：

   ```bash
   go test -v -run 'TestFetcherManager_ParseName' ./internal/protocol/bt/
   ```

   然后对下面 5 个 URL 写出你会预测的接管协议与理由（答案见练习 1）：
   `magnet:?xt=urn:btih:abc...`、`ftp://mirror.example.org/big.torrent`、`https://example.org/big.torrent`、`/home/me/ubuntu.torrent`、`data:application/x-bittorrent;base64,...`。
3. **需要观察的现象**：表驱动测试的四行用例（损坏 URL、有 dn、无 dn、非标准 magnet）与 [fetcher_test.go:243-289](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher_test.go#L243-L289) 中的期望值一一对应。
4. **预期结果**：测试通过；`dn` 存在时返回文件名，仅 `xt` 时返回 40 位哈希串，非标准 magnet（`xxt=`）返回空串。

#### 4.4.5 小练习与答案

**练习 1**：给出实践步骤中 5 个 URL 的接管协议。
**答案**：① `magnet:` → BT（url 过滤命中）；② `ftp://.../big.torrent` → FTP（ftp 排在 bt 前，且按 scheme 匹配优先）；③ `https://.../big.torrent` → HTTP（http 排在 bt 前）；④ `/home/me/ubuntu.torrent` → BT（无 scheme，http 的 scheme 过滤不中，扩展名 `.torrent` 命中 BT 的 file 过滤）；⑤ data URI → BT（base64 过滤匹配 MIME）。

**练习 2**：`magnet://!@#%github.com` 这种损坏 URL 为什么 `ParseName` 返回空串而不是报错？
**答案**：`url.Parse` 对该串实际解析成功但 Query 中既无 `dn` 也无 `xt`（见测试 "broken url" 用例），于是走到兜底的空串；ParseName 的契约是「尽力而为的名字提示」，失败不应影响后续流程。

### 4.5 torrentFileLayout 与 updateRes：种子元数据映射为 Resource

#### 4.5.1 概念说明

`Resource` 是协议无关的通用模型（u2-l1），而 BT 的文件布局有几个独特之处需要一个专门的映射函数：

- 种子可以有**根名**（info name，多文件种子表现为一层包裹目录），也可以**没有根名**（anacrolix 的 `NoName` 约定）；
- v2 种子（BEP 52 的 file tree）可能以「单层命名目录」作为根，而不是显式的名字包装；
- 同一套布局既要用于构造 `Resource`（给用户看），又要用于**实际落盘的文件路径**（`Start` 时绑定存储），两处必须完全一致，否则「看到的文件」和「写出的文件」对不上。

`torrentFileLayout` 就是这个「info + 文件 → (根目录, 完整相对路径)」的唯一权威实现，两个使用点共享它。

#### 4.5.2 核心流程

```text
torrentFileLayout(info, file):
    parts = file.BestPath()                     # 文件在种子内的路径片段
    if info.BestName() != NoName:               # 有根名的种子
        root = info.BestName()                  # （仅当文件确有路径时）
        parts = [BestName] + parts              # 根名作为最外层目录
    else if v2 种子 && file tree 恰好一个子目录:
        root = 那个子目录名                      # v2 的隐式根
    return root, filepath.Join(parts...)        # name 是含根的完整相对路径

updateRes():
    Resource{ Range: true, Hash: InfoHash, Files: [...] }
    Resource.Name = torrentFileLayout(第一个文件).root
    每个文件: Name=Base(DisplayPath)  Path=Dir(DisplayPath)  Size=Length
    CalcSize(nil) 全量统计；InitSelectFiles 展开默认全选
```

关键点：`Resource.Name`（根名）只取自**第一个文件**的布局结果——多文件种子全体文件共享同一个根；单文件种子布局无根（`NoName`），`Resource.Name` 为空，这与 u3-l4 讲过的约定一致（单文件资源的名字放在 `Files[0].Name`，根名字段留空）。

#### 4.5.3 源码精读

映射函数本体：

[internal/protocol/bt/fetcher.go:520-539](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L520-L539) 注释（L520-521）点明它要「匹配文件存储的布局，包括无根种子的 NoName 约定」，并提醒 anacrolix 的 `File.Path()` 总会前置 BestName 而存储层可能省略——所以路径推导必须自己来。L523-529 处理有根名种子；L531-537 处理 v2 单目录根的特例；L538 返回 `filepath.Join` 的完整相对路径。

落盘侧的使用点（保证「所见即所写」）：

[internal/protocol/bt/fetcher.go:145-151](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L145-L151) `Start` 绑定文件存储时的 `FilePathMaker` 直接调用 `torrentFileLayout` 决定每个文件在磁盘上的相对路径——与 `updateRes` 用的是同一个函数，这是「Resource 展示与存储布局一致」的结构性保证。

Resource 的构造：

[internal/protocol/bt/fetcher.go:656-678](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L656-L678) `updateRes`：`Range: true`（BT 天然按 piece 断点续传）；`Hash` 来自 `InfoHash().String()`——对种子文件路径，这个哈希由库计算自 info 字典；对磁力路径，它就是 URI 里 `xt` 的那串（两者殊途同归）。文件条目的 `Name/Path` 取自 `DisplayPath()` 的 Base/Dir，`Size` 取 `Length`。最后 `CalcSize(nil)` 汇总总大小、`InitSelectFiles` 把「未选择」展开为全选下标。

对照 4.3.4 的实践输出可验证：`test.torrent` 的 info name 是 `test`（成为 `Resource.Name`），三个文件中 `c.txt` 位于子目录 `path`，其余两个在根下——与 [fetcher_test.go:196-216](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher_test.go#L196-L216) 的期望完全一致；而单文件的 ubuntu 种子期望值（[fetcher_test.go:40-50](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher_test.go#L40-L50)）中 `Resource.Name` 为空、`Files[0].Name = "ubuntu-22.04-live-server-amd64.iso"`，正是 `NoName` 无根布局的体现。

#### 4.5.4 代码实践

1. **实践目标**：用一单一双两个种子验证 `torrentFileLayout` 的「有根/无根」两种行为。
2. **操作步骤**：在 4.3.4 的实践测试里再加一个用例，Resolve 单文件种子 `./testdata/ubuntu-22.04-live-server-amd64.iso.torrent` 并同样打印 `Resource`；对比两者输出。
3. **需要观察的现象**：单文件种子 `res.Name` 为空、`Files` 只有一项且大小等于总大小（1466714112）；多文件种子 `res.Name = "test"`、总大小为三文件之和。
4. **预期结果**：输出与上述现有断言一致；据此用自己的话总结规则：「`Resource.Name` 是否为空取决于种子的 info name 是否构成目录包裹层」。（实际输出待本地验证。）

#### 4.5.5 小练习与答案

**练习 1**：为什么 `updateRes` 只用第一个文件的布局取根名？如果某文件列表为空会发生什么？
**答案**：所有文件共享同一个 info 根，任取一个即可推出根名，实现上取第一个最简单；代码在取根名前检查了 `len(files) > 0`（L663-665），空列表时 `res.Name` 保持零值，不会越界。

**练习 2**：假如 `Start` 的 `FilePathMaker` 不复用 `torrentFileLayout` 而是另行拼路径，最坏后果是什么？
**答案**：无根种子（`NoName`）场景下，anacrolix 的 `File.Path()` 会前置 BestName 而存储约定可能不前置，两侧路径就会差一层目录：用户在 Resource 里看到的文件与磁盘上实际写出的文件对不上，断点续传与 piece 校验也会错位。复用同一函数把这个错误类别从设计上消灭。

## 5. 综合实践

把本讲四个模块串成一个实验，产出一份一页纸的「BT 解析与竞态分析笔记」：

1. **解析观察**：完成 4.3.4 与 4.5.4 的两个实践测试，把多文件种子与单文件种子的 `Resource` 输出贴进笔记，标注：根名来源、每个文件的 `Name/Path/Size`、总大小、Hash。
2. **磁力对照**：写一个磁力 URI（可从任意公开种子站取得），只用 `FetcherManager.ParseName`（不 Resolve）提取名字，并回答：这个 URI 的 InfoHash 从哪来？若做完整 Resolve，会在 `addTorrent` 的哪一行阻塞、等什么事件？（对照 [fetcher.go:847-854](https://github.com/GopeedLab/gopeed/blob/d69874e13eddd0dcaa77f2f5dd307e44346ab9e3/internal/protocol/bt/fetcher.go#L847-L854) 回答。）
3. **竞态分析**：运行 4.2.4 的三个回归测试（带 `-race`），在笔记中为每个测试画一张两协程交错时序图（参照 4.2.2 的画法），标出旧代码的竞态点与 `addTorrentSpec` 持锁窗口如何把交错收敛为两种合法顺序。
4. **自查问题**：解释为什么「锁保护建客户端+添加种子」而不能覆盖「等待元数据」；解释为什么 `Fetcher.Close` 与 `closeClient` 是两个层次的操作。

## 6. 本讲小结

- BT 全进程共享一个 `torrent.Client`（包级变量 + `lock`），懒加载创建；单任务关闭只 `Drop` 自己的种子，共享客户端由 `FetcherManager.Close → closeClient` 在 Downloader 关闭序列中统一销毁。
- `cd19772` 之后 `initClient` 遵循「必须持锁调用」约定，新增的 `addTorrentSpec` 把临界区从「初始化」延伸到「种子添加完成」，消灭了 `closeClient` 在建种与关闭之间的竞态；`go dnsResolver.Run(closeCtx)` 以实参传值替代闭包捕获变量，避免协程读到被重置的 context。
- `addTorrent` 有磁力与三类种子容器共四条读取分支：磁力的 InfoHash 从 URI `xt` 参数解析（元数据需等 peer 提供），种子文件的 InfoHash 由库对 info 字典计算（元数据本地即得）；私有种子不合并外部 tracker。
- BT 的路由声明是三类指纹：`magnet:` scheme、`.torrent` 扩展名、data URI 的 `application/x-bittorrent` MIME；注册顺序 ftp→hls→http→bt→ed2k 决定了 `https://.../x.torrent` 归 HTTP、`ftp://.../x.torrent` 归 FTP。
- `torrentFileLayout` 是「种子元数据 → 磁盘相对路径」的唯一权威映射，同时服务 `updateRes`（用户可见的 Resource）与 `Start` 的存储绑定（实际落盘），根名是否存在决定 `Resource.Name` 是否为空。

## 7. 下一步学习建议

- 下一讲 **u6-l2（文件选择与 Patch）**：本讲看到 `updateRes` 末尾的 `InitSelectFiles` 全选展开，下一讲分析运行中 `Patch` 如何重算大小、`CancelPieces`/`Download` 如何切换选择，以及未选中文件的清理。
- 之后可按顺序阅读 **u6-l3（做种与统计）**、**u6-l4（tracker 订阅与协议定时任务，`effectiveTrackers` 的完整实现）**、**u6-l5（BT 会话存储与恢复，`fetcherData` 与 piece 完成状态持久化）**。
- 想巩固并发直觉的读者，可以结合 u4-l4（runGeneration 代数）与 u11-l2（锁体系清单）对比本讲的竞态修复手法：一个是「扩大临界区 + 明确调用契约」，一个是「代数核对过期回调」，体会不同场景下的取舍。
