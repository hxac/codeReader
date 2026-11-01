# u3-l4 实战：实现一个自定义协议 Fetcher

## 1. 本讲目标

前两讲我们把 `Fetcher` 接口契约和 `FetcherManager` 路由机制拆开讲透了，u3-l3 又讲解了协议实现能从 Controller 拿到哪些共享服务。本讲把这三块知识**拼装成一个能跑的东西**：从零写一个支持 `mock://` 前缀 URL 的最小协议，注册进 Downloader，打通 `resolve → create → download → done` 全流程。

学完本讲，你应该能够：

1. **独立实现 Fetcher 的全部十个接口方法**（Setup/Resolve/Start/Patch/Pause/Close/Stats/Meta/Progress/Wait），知道每个方法被 Downloader 的哪段代码调用、什么时候被调用。
2. **正确实现 Store/Restore 两段式持久化契约**，让任务在进程重启后可以恢复（这是很多「能跑但不可恢复」玩具实现最容易缺的一环）。
3. **理解 DoneCh 完成通知的握手语义**：容量为什么是 1、发送为什么必须非阻塞、`Wait()` 返回 nil 和返回 error 分别触发什么。
4. **理解 setupFetcher 注入 Controller 的确切时机**：Build 之后、Resolve/Start 之前，且每个 Fetcher 实例只被注入一次。
5. 知道一个真实限制：`Boot()` 单例无法注入自定义协议，必须自己 `NewDownloader`（本讲 4.4 会解释原因）。

本讲的「参考教材」有两个：正式协议里最简洁的 **ed2k 实现**（internal/protocol/ed2k），以及项目测试里现成的**最小 mock 实现** `generationTestManager`/`generationTestFetcher`（pkg/download/downloader_test.go）——后者证明了一个合法协议最小只需要约 70 行代码。

## 2. 前置知识

- **接口契约回顾（u3-l1）**：每个下载任务对应一个 Fetcher 实例；`FetcherMeta` 的 Req/Res/Opts 三元组是 Downloader 与 Fetcher 共享的数据黑板；Downloader 是任务状态的唯一写入者，Fetcher 只通过 `Wait()` 的返回值汇报结果。
- **协议路由（u3-l2）**：`parseFm` 按 `FetchManagers` 注册顺序做 first-match-wins 匹配；`SchemeFilter{Type: FilterTypeUrl, Pattern: "mock"}` 能让所有 `mock://` 开头的 URL（大小写不敏感）命中我们的管理器。
- **Controller 服务包（u3-l3）**：`ctl.Touch(name, size)` 一步完成「建父目录 → 建文件 → 截断到目标大小」，是协议创建目标文件的正规入口；`ctl.Logger` 可能为 nil。
- **Go 的 internal 可见性规则（u1-l3）**：`internal/fetcher`、`internal/controller` 只能被本 module 内的代码 import。所以自定义协议的练习代码**必须放在本仓库目录内**（建议仿照 `_examples/basic` 建一个 `_examples/mock`），不能在外部独立 module 里写。
- **带缓冲 channel 的非阻塞发送**：`select { case ch <- v: default: }` 在通道已满时直接跳过而不是阻塞。这是 DoneCh「恰好通知一次、绝不卡住下载协程」的关键写法。
- **`sync/atomic` 原子计数**：下载协程与 watch 轮询协程并发读写「已下载字节数」，用 `atomic.Int64` 避免加锁。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| [internal/fetcher/fetcher.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go) | Fetcher/FetcherManager 接口、DefaultFetcher 基类、Progress 类型 | 要实现的「合同原文」 |
| [internal/protocol/ed2k/fetcher.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ed2k/fetcher.go) | ed2k 协议的 Fetcher 与 FetcherManager 完整实现 | 正式协议中的最简范本 |
| [internal/protocol/ed2k/config.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ed2k/config.go) | ed2k 协议配置结构体 | DefaultConfig 返回什么的实例 |
| [pkg/download/model.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go) | DownloaderConfig 与默认协议注册表 | 注册入口 |
| [pkg/download/downloader.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go) | Downloader 主逻辑：parseFm/setupFetcher/doCreate/doStart/watch/restoreFetcher | 协议的「调用方」 |
| [pkg/download/downloader_test.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader_test.go) | `generationTestManager`/`generationTestFetcher` 最小 mock 协议；`recoveryTestManager` 演示 Store/Restore | 项目自带的最小参考实现 |
| [pkg/download/selection_test.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/selection_test.go) | 用 mock 管理器测试 Resolve→CreateWithOptions 全流程 | 验证方式的活例子 |
| [internal/protocol/hls/fetcher.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/hls/fetcher.go) | HLS Fetcher：内嵌 DefaultFetcher 的标准姿势、DoneCh 非阻塞发送现场 | 4.3 节主要引用 |
| [pkg/download/protocol_config.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_config.go) | 协议配置归一化 normalizeProtocol | 解释 DefaultConfig 不能返回 nil |

## 4. 核心概念与源码讲解

### 4.1 FetcherManager 骨架：九个方法的参考实现

#### 4.1.1 概念说明

u3-l2 讲过：FetcherManager 是**协议级单例门面**，Fetcher 才是**任务级执行者**。一个 Downloader 里每种协议只有一个 Manager 实例，但它 `Build()` 出来的 Fetcher 可以同时存在成百上千个。

FetcherManager 接口共九个方法，按职责分四组：

| 分组 | 方法 | 回答的问题 |
| --- | --- | --- |
| 身份 | `Name()` | 协议叫什么（决定 Task.Protocol 标签、配置分桶、`ProtocolConfig` 键名） |
| 路由 | `Filters()` | 哪些 URL 归我管 |
| 工厂 | `Build()` / `ParseName(u)` / `AutoRename()` | 怎么造 Fetcher；任务未解析时显示什么名字；要不要自动重名避让 |
| 配置与持久化 | `DefaultConfig()` / `Store(f)` / `Restore()` / `Close()` | 默认配置是什么；任务状态怎么存/怎么还原；管理器怎么释放资源 |

「最小骨架」就是给这九个方法各写一个最简单的合法实现。项目测试里的 `generationTestManager` 就是这么做的——一个只匹配 `generation://` 前缀、Resolve 永远返回固定单文件资源的协议，全部实现不到 20 行。

#### 4.1.2 核心流程

Downloader 在任务生命周期的六个时刻会用到 Manager（承接 u3-l2 的结论，这里标注到方法级）：

```text
解析阶段  Resolve(req)        → parseFm 选中 Manager → Build() 造 Fetcher → setupFetcher 注入
建任务    CreateDirect(req)   → 同上，另经 doCreate 记 task.Protocol = fm.Name()
落盘      saveTask(task)      → Manager.Store(fetcher) 序列化协议状态 → 写入 save 桶
改选项    Patch(id, ...)      → （不经过 Manager，直接打到 Fetcher.Patch）
恢复      restoreFetcher      → Manager.Restore() 拿模板 v + 构造器 f → f(meta, v) 重建 Fetcher
关闭      Downloader.Close()  → 遍历所有 Manager 调用 fm.Close()
```

Store/Restore 是「两段式」契约，这是本讲最难也最重要的一块：

- `Store(f)` 返回一个**可 JSON 序列化的值**（Downloader 会把它原样写进 `save` 桶，键为任务 ID）；返回 `nil` 表示「本协议没有需要持久化的任务级状态」，Downloader 会顺手删掉 save 桶里的旧记录。
- `Restore()` 返回**一对值**：`v` 是一个「空模板」（Downloader 用 `storage.Get(bucketSave, taskID, v)` 把落盘数据反序列化进去），`f` 是构造器（拿回填好的 `v` 和任务的 `meta`，组装出一个 Fetcher 实例）。`v` 为 nil 时 Downloader 跳过读取直接调 `f(meta, nil)`。

#### 4.1.3 源码精读

先看合同原文：

[internal/fetcher/fetcher.go:L123-L143](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L123-L143) 定义 FetcherManager 接口的九个方法。注意 `Store` 与 `Restore` 的签名注释只有一句话，真正的语义要读 Downloader 侧的调用代码（见下）。

再看 ed2k 这个正式范本的 Manager 部分：

[internal/protocol/ed2k/fetcher.go:L390-L417](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ed2k/fetcher.go#L390-L417) 是身份、路由、工厂三组方法的最小实现：`Name()` 返回常量 `"ed2k"`；`Filters()` 声明一个 `FilterTypeUrl` 类型、Pattern 为 `"ED2K"` 的过滤器（注意 Pattern 写的是大写 scheme，`Match` 内部会把 URL 和 Pattern 都转大写再比前缀）；`Build()` 返回 `&Fetcher{manager: fm}`——新 Fetcher 反向持有 Manager 的引用，是为了之后能取到共享的 goed2k 客户端；`ParseName` 从 ed2k 链接里解出文件名；`AutoRename()` 返回 true，表示同名文件要自动改名。

[internal/protocol/ed2k/fetcher.go:L419-L427](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ed2k/fetcher.go#L419-L427) 的 `DefaultConfig()` 返回带默认值的 `*config`（配置结构体在 [internal/protocol/ed2k/config.go:L9-L15](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ed2k/config.go#L9-L15)，就是五个 JSON 字段）。**DefaultConfig 绝不能返回 nil**：[pkg/download/protocol_config.go:L41-L44](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_config.go#L41-L44) 里 `normalizeProtocol` 遇到 nil 会直接报 `"protocol does not expose configuration"`，而 `Setup()` 期间的 [initProtocolConfigs](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_config.go#L154-L172) 会对**每一个注册的 Manager** 做这件事——nil 默认配置会让整个 Downloader 起不来。没有配置可写就返回 `map[string]any{}`。

[internal/protocol/ed2k/fetcher.go:L429-L440](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ed2k/fetcher.go#L429-L440) 是 ed2k 的 Store/Restore：`Store` 返回 `nil, nil`（任务级状态为空——ed2k 的续传数据由 goed2k 客户端自己通过 `ProtocolStateStore` 渠道持久化，那是另一个可选接口 `StatefulFetcherManager` 的事）；`Restore` 返回 `nil` 模板 + 一个闭包构造器，闭包把 Manager 和传入的 meta 组装成新 Fetcher。这是「无任务级状态」协议的最简合法写法。

Downloader 侧的存取现场，是理解两段式的钥匙：

[pkg/download/downloader.go:L489-L514](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L489-L514) `saveTask`：先调 `task.fetcherManager.Store(task.fetcher)` 拿序列化值，非 nil 就写 `save` 桶、nil 就删 `save` 桶里该任务的旧记录；随后把 Task 快照写进 `task` 桶。注意 saveTask 在任务启动、暂停、完成等多个时机都会被调用（检查点机制，u4-l3 详讲）。

[pkg/download/downloader.go:L1797-L1820](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1797-L1820) `restoreFetcher`：`v, f := task.fetcherManager.Restore()`；`v != nil` 时用 `storage.Get(bucketSave, task.ID, v)` 反序列化；然后 `task.fetcher = f(task.Meta, v)`；若构造器返回 nil 则退回 `fm.Build()`；最后**必定**调用 `d.setupFetcher(...)` 并把 meta 三元组（Req/Res/Opts）里缺失的部分从 Task 回填——这就是「重启后 Fetcher 拿回记忆」的完整路径。

最后看项目自带的 mock 参考：

[pkg/download/downloader_test.go:L110-L126](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader_test.go#L110-L126) `generationTestManager` 的九个方法实现，每个都是一行或几行：Name 是 `"generation"`、Filters 匹配 `generation://`、Build 造 Fetcher、ParseName 返回固定名、AutoRename 为 false、DefaultConfig 返回空 map、Store 返回 nil、Restore 返回 nil 模板 + 构造器、Close 返回 nil。

[pkg/download/downloader_test.go:L191-L222](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader_test.go#L191-L222) `recoveryTestManager`/`recoveryTestFetcher` 则演示了**带状态的** Store/Restore：`Store` 返回 `f.(*recoveryTestFetcher).data`（一个 `*recoveryTestData` 结构体），`Restore` 返回空模板 `&recoveryTestData{}` 和一个把 `value.(*recoveryTestData)` 塞回新 Fetcher 的闭包。对照 ed2k 的 nil 版本，这两种写法覆盖了几乎所有协议的需要。

#### 4.1.4 代码实践

**实践：写出 mock 协议的 Manager 骨架**

1. **实践目标**：亲手实现九个方法，体会「哪些可以有默认实现、哪些必须认真写」。
2. **操作步骤**：
   - 在仓库根目录新建 `_examples/mock/main.go`（`_` 前缀让 `go build ./...` 忽略它，与 `_examples/basic` 同理；且必须在 module 内才能 import internal 包）。
   - 写入以下**示例代码**（本讲 4.2/4.3/4.4 会逐步补全 Fetcher 部分和 main 函数）：

   ```go
   package main

   import (
       "strings"

       "github.com/GopeedLab/gopeed/internal/fetcher"
       "github.com/GopeedLab/gopeed/pkg/base"
   )

   // mockManager 是协议级单例：一个 Downloader 只需要一个实例。
   type mockManager struct{}

   func (m *mockManager) Name() string { return "mock" }

   // 只认 mock:// 前缀（大小写不敏感），这是 scheme 强意图路由。
   func (m *mockManager) Filters() []*fetcher.SchemeFilter {
       return []*fetcher.SchemeFilter{
           {Type: fetcher.FilterTypeUrl, Pattern: "mock"},
       }
   }

   func (m *mockManager) Build() fetcher.Fetcher {
       return &mockFetcher{manager: m, meta: &fetcher.FetcherMeta{}}
   }

   // 任务未 Resolve 时的显示名：mock://hello.txt -> hello.txt
   func (m *mockManager) ParseName(u string) string {
       return strings.TrimPrefix(u, "mock://")
   }

   // 教学协议不做重名避让，保持文件名原样。
   func (m *mockManager) AutoRename() bool { return false }

   // DefaultConfig 不能返回 nil，否则 normalizeProtocol 直接报错。
   func (m *mockManager) DefaultConfig() any { return map[string]any{} }

   // 第一版：无任务级持久化状态（综合实践会升级为带状态版本）。
   func (m *mockManager) Store(f fetcher.Fetcher) (any, error) { return nil, nil }
   func (m *mockManager) Restore() (any, func(*fetcher.FetcherMeta, any) fetcher.Fetcher) {
       return nil, func(meta *fetcher.FetcherMeta, _ any) fetcher.Fetcher {
           return &mockFetcher{manager: m, meta: meta}
       }
   }

   func (m *mockManager) Close() error { return nil }
   ```

3. **需要观察的现象**：此刻代码还不能编译（`mockFetcher` 未定义），这是预期内的——骨架先行，下一节补 Fetcher。
4. **预期结果**：与 [downloader_test.go:L110-L126](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader_test.go#L110-L126) 的 generationTestManager 逐行对照，你的实现应当只差 Name/Pattern/ParseName 三处字符串。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `DefaultConfig()` 改成返回 `nil`，程序会在哪一步、报什么错？

**答案**：在 `NewDownloader` 之后调用 `Setup()` 时。`Setup` → `initProtocolConfigs` → `normalizeProtocol(fm, value)`，[protocol_config.go:L41-L44](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_config.go#L41-L44) 判断 `target == nil` 返回 `"protocol does not expose configuration"`，随后 [initProtocolConfigs](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_config.go#L160-L169) 把它包装成 `"mock config: ..."` 返回，`Setup()` 失败。

**练习 2**：`Store` 返回 `nil` 与返回一个空结构体指针，Downloader 的行为有什么区别？

**答案**：返回 nil 时 [saveTask](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L495-L505) 会**删除** `save` 桶中该任务的旧记录（保持存储干净）；返回非 nil 空结构体则会把 `"{}`" 这样的 JSON 写进 save 桶。恢复侧对应地，`Restore` 返回 nil 模板时 `restoreFetcher` 跳过 `storage.Get`，返回非 nil 模板时会尝试读取。

**练习 3**：为什么 `Build()` 造出的 Fetcher 要反向持有 `manager` 字段的引用？

**答案**：因为 Fetcher 实例是任务级的，但协议的共享资源（ed2k 的 goed2k 客户端、BT 的 torrent Client）属于 Manager 单例。ed2k 的 `getClient()`（[internal/protocol/ed2k/fetcher.go:L331-L336](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ed2k/fetcher.go#L331-L336)）就是通过 `f.manager.initClient(...)` 拿到全局唯一客户端。mock 协议没有共享资源，持有引用只是为了对称。

### 4.2 Fetcher 的 Resolve 与 Start：让协议真的写出文件

#### 4.2.1 概念说明

Fetcher 十个方法里，真正定义「这个协议下载什么、怎么下载」的是 Resolve 和 Start，二者职责严格分离：

- **Resolve(req)：只做资源发现，不动磁盘不建连接**。输入 Request（URL 为准），输出写进 `meta.Res` 的 Resource。Downloader 在两个时机会调它：用户显式调 `Downloader.Resolve()` 预览资源时；以及 `CreateDirect` 建任务后 `doStart` 发现 `task.Meta.Res == nil` 时（解析后置）。所以 **Resolve 必须幂等**——同一 URL 调两次结果一致。
- **Start()：开始真正传输**。此时 meta 三元组（Req/Res/Opts）已全部就位、目录已由 prepareTask 决定好，Fetcher 要做的只有：创建目标文件、启动传输协程、尽快返回 nil。**Start 返回 error 表示「启动失败」，传输中途的失败要经 DoneCh 上报**（见 4.3），这是新手最易混淆的一点。

Resource 的构造规则（决定任务在 UI 里是「单文件」还是「文件夹」）：

- 单文件资源：`Name` 留空，`Files` 恰好一个元素，文件名给 `Files[0].Name`。任务最终路径用 `meta.SingleFilepath()` 算。
- 文件夹/多文件资源：`Name` 为文件夹名，`Files` 列出全部文件。任务路径用 `meta.FolderPath()` 算。

ed2k 是单文件协议，`buildResource` 给出了单文件构造的最小样例；`Size` 必须给（进度百分比、速度、完成判定都依赖它），`Range` 只对 HTTP 分块有意义，其他协议填 false 即可。

#### 4.2.2 核心流程

一次 `CreateDirect("mock://hello.txt")` 中，与 Fetcher 有关的关键时序（简化自 doStart，完整版见 u4-l2）：

```text
CreateDirect(req, opts)
  └─ buildFetcher(url)          parseFm 选中 mockManager → fm.Build() → setupFetcher 注入 Controller
  └─ fetcher.Meta().Req = req   先挂请求
  └─ initOptions(opts)          补默认 Path / SelectFiles
  └─ doCreate                   建 Task、task.Protocol="mock"、NeedsPreparation=true、落盘、入队/启动
       └─ doStart (异步 handler, 持 task.lock)
            ├─ fetcher.Meta().Req/Res/Opts = task.Meta.*   ← meta 黑板回填
            ├─ task.Meta.Res == nil ? → fetcher.Resolve(req)  ← 解析后置路径
            ├─ prepareTask         决定下载目录（分类路由 + 白名单校验），NeedsPreparation 清零
            ├─ fetcher.Start()     ← 本节主角：创建文件 + 启动传输协程
            ├─ captureTaskStats    存一份启动时统计（Stats() 可返回 nil）
            ├─ saveTask            Manager.Store + Task 快照落盘
            └─ emit(start) + go watch(task)   ← 交给 4.3 的 Wait 循环
```

注意 setupFetcher 的注入时机在 **Build 之后、任何业务方法之前**，且每个 Fetcher 实例恰好一次（Build 路径在 buildFetcher 里，Restore 路径在 restoreFetcher 末尾）。

#### 4.2.3 源码精读

**Resolve 范本**。ed2k 的实现：

[internal/protocol/ed2k/fetcher.go:L70-L80](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ed2k/fetcher.go#L70-L80) `Resolve`：解析 ed2k 链接 → 失败直接返回错误（Downloader 会把它作为 resolve 失败上报）→ 成功则挂 `meta.Req = req`、`meta.Res = buildResource(link)`，三行结束，没有任何 IO。

[internal/protocol/ed2k/fetcher.go:L464-L476](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ed2k/fetcher.go#L464-L476) `buildResource`：单文件资源的标准构造——`Name` 不设、`Size` 取链接里的文件大小、`Range: false`、`Hash` 填链接哈希、`Files` 一个元素。对照 [pkg/base/model.go:L96-L105](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L96-L105) 的 Resource 字段注释逐个理解。

再看 mock 版 Resolve 的「法定最简形」：

[pkg/download/downloader_test.go:L140-L154](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader_test.go#L140-L154) `generationTestFetcher.Resolve`：支持注入错误和阻塞（测试竞态用），正常路径就是 `f.meta.Req = req` + 构造一个 `Files: [{Name: "generation.bin", Size: 1}]` 的 Resource。

**Start 范本**。ed2k 的 Start 稍复杂（要先确保共享客户端、算目标路径、找/建 transfer）：

[internal/protocol/ed2k/fetcher.go:L82-L129](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ed2k/fetcher.go#L82-L129) 值得关注三个细节：① 第 87 行 `if f.meta.Res == nil` 的兜底——Start 必须容忍「没经过 Resolve 直接 Start」的恢复路径；② 第 96-99 行自己算 `SingleFilepath()` 并 `os.MkdirAll` 建父目录；③ 全函数没有任何「等待下载完成」的逻辑，把 transfer 交给客户端后台跑，立刻返回。

mock 协议不需要自己 MkdirAll——u3-l3 讲过的 `Touch` 一步到位：

[internal/controller/controller.go:L43-L59](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/controller/controller.go#L43-L59) `DefaultFileController.Touch`：MkdirAll 建父目录 → `os.Create` 建文件 → `os.Truncate` 把文件撑到目标大小（稀疏预分配）。协议侧通过内嵌接口直接写 `f.Ctl.Touch(path, size)` 调用。

**调用方约束**。Start 被调用的现场在 doStart 的 handler 里：

[pkg/download/downloader.go:L2045-L2093](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2045-L2093) 依次完成 meta 回填（L2045-L2049）、解析后置（L2053-L2060）、prepareTask 目录决策（L2062-L2081）、`fetcher.Start()`（L2090）。你的 Start 可以假定：Opts.Path 已是最终目录、Opts.Name 已是最终文件名（AutoRename 为 true 时 prepareTask 已完成改名）。

**Progress 的语义**：

[internal/fetcher/fetcher.go:L177-L186](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L177-L186) `Progress` 是 `[]int64`——**每个已选中文件一个元素，值是该文件已下载字节数**，顺序与 SelectFiles 一致。watch 循环每 350ms（默认 RefreshInterval）读一次它算速度与总进度。mock 是单文件协议，永远返回 `fetcher.Progress{n}`，参考 [internal/protocol/ed2k/fetcher.go:L282-L288](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ed2k/fetcher.go#L282-L288)。

#### 4.2.4 代码实践

**实践：实现 mockFetcher 的全部传输方法**

1. **实践目标**：Resolve 返回固定 Resource；Start 用 goroutine 模拟分块写入，真实地在磁盘上产出一个文件。
2. **操作步骤**：在 `_examples/mock/main.go` 中追加以下**示例代码**：

   ```go
   import (
       "context"
       "os"
       "sync/atomic"
       "time"

       "github.com/GopeedLab/gopeed/internal/controller"
       "github.com/GopeedLab/gopeed/internal/fetcher"
       "github.com/GopeedLab/gopeed/pkg/base"
   )

   const mockSize int64 = 4096 // 固定资源大小

   // mockFetcher 是任务级实例：一个下载任务一个。
   type mockFetcher struct {
       fetcher.DefaultFetcher        // 提供 Ctl/DoneCh 与 Wait()（4.3 详讲）
       manager      *mockManager
       meta         *fetcher.FetcherMeta
       ctx          context.Context
       cancel       context.CancelFunc
       written      atomic.Int64 // 已写入字节数，下载协程写、watch 协程读
   }

   // Setup 在 Build 之后、Resolve/Start 之前被 setupFetcher 调用。
   func (f *mockFetcher) Setup(ctl *controller.Controller) {
       // 关键一步：初始化内嵌基类的 Ctl 与 DoneCh，Wait() 依赖 DoneCh。
       f.DefaultFetcher.Setup(ctl)
       if f.meta == nil {
           f.meta = &fetcher.FetcherMeta{}
       }
       f.ctx, f.cancel = context.WithCancel(context.Background())
   }

   // Resolve 只做资源发现：不动磁盘、不发请求，且必须幂等。
   func (f *mockFetcher) Resolve(req *base.Request) error {
       if !strings.HasPrefix(req.URL, "mock://") {
           return errors.New("invalid mock url")
       }
       f.meta.Req = req
       // 单文件资源：Name 留空，文件信息放 Files。
       f.meta.Res = &base.Resource{
           Size:  mockSize,
           Range: false,
           Files: []*base.FileInfo{{Name: f.manager.ParseName(req.URL), Size: mockSize}},
       }
       return nil
   }

   // Start 创建目标文件并启动传输协程，必须立刻返回。
   func (f *mockFetcher) Start() error {
       // 容忍「未经 Resolve 直接 Start」的恢复路径（同 ed2k L87）。
       if f.meta.Res == nil {
           if err := f.Resolve(f.meta.Req); err != nil {
               return err
           }
       }
       file, err := f.Ctl.Touch(f.meta.SingleFilepath(), f.meta.Res.Size)
       if err != nil {
           return err
       }
       go f.download(file)
       return nil
   }

   // download 模拟分块写入：每 10ms 写 512 字节，写完向 DoneCh 发结果。
   func (f *mockFetcher) download(file *os.File) {
       defer file.Close()
       buf := make([]byte, 512)
       for i := range buf {
           buf[i] = byte('a' + i%26)
       }
       var off int64
       for off < mockSize {
           select {
           case <-f.ctx.Done(): // Pause/Close 时静默退出，不上报错误
               return
           default:
           }
           n := int64(len(buf))
           if off+n > mockSize {
               n = mockSize - off
           }
           if _, err := file.WriteAt(buf[:n], off); err != nil {
               select { // 非阻塞上报失败，写法同 HLS supervise
               case f.DoneCh <- err:
               default:
               }
               return
           }
           off += n
           f.written.Store(off)
           time.Sleep(10 * time.Millisecond) // 模拟慢速网络，让进度可见
       }
       select { // 成功：向 DoneCh 发 nil（恰好一次，4.3 详讲）
       case f.DoneCh <- nil:
       default:
       }
   }

   // Patch：任务未启动前允许改 URL/名称；运行中拒绝改（同 ed2k 的思路）。
   func (f *mockFetcher) Patch(req *base.Request, opts *base.Options) error {
       if req != nil && req.URL != "" {
           if err := f.Resolve(req); err != nil {
               return err
           }
       }
       if opts != nil {
           if opts.Name != "" {
               f.meta.Opts.Name = opts.Name
           }
           if opts.Path != "" {
               f.meta.Opts.Path = opts.Path
           }
       }
       return nil
   }

   // Pause：取消传输协程即可，任务状态由 Downloader 管理。
   func (f *mockFetcher) Pause() error {
       if f.cancel != nil {
           f.cancel()
       }
       return nil
   }

   // Close：Pause 超集，释放全部资源。
   func (f *mockFetcher) Close() error { return f.Pause() }

   func (f *mockFetcher) Meta() *fetcher.FetcherMeta { return f.meta }

   // Stats 返回 nil 是合法的：captureTaskStats 对 nil 直接跳过。
   func (f *mockFetcher) Stats() *fetcher.Stats { return nil }

   // Progress 每个选中文件一个元素；mock 永远单文件。
   func (f *mockFetcher) Progress() fetcher.Progress {
       return fetcher.Progress{f.written.Load()}
   }
   ```

   （import 块请与 4.1.4 的合并，另需 `"errors"`。）

3. **需要观察的现象**：`go build ./_examples/mock` 应当编译通过（main 函数下一节补，可先临时加一个空的 `func main() {}`）。对照 `generationTestFetcher`（[downloader_test.go:L128-L185](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader_test.go#L128-L185)），你的版本多了真实的文件写入与 context 取消，其余结构一致。
4. **预期结果**：编译零错误；此时还不能运行，等待 4.4 的 main。
5. 运行验证属于 4.4 的实践，此处**待本地验证**的只有编译。

#### 4.2.5 小练习与答案

**练习 1**：如果 Resolve 里把 `Res.Name` 设成 `"hello"`（非空）而 Files 也照旧，任务行为会怎么变？

**答案**：Resource 变成「文件夹语义」。[FetcherMeta.RootDirPath/FolderPath](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L56-L84) 会把任务路径算成 `<Path>/hello/`；而我们的 Start 用的是 `SingleFilepath()`（取 `Files[0].Name` 拼路径），文件仍会写到 `<Path>/hello.txt`，于是「目录决策」与「实际写入」不一致。**单文件协议必须保持 Name 为空**，两个路径计算方法要配套使用。

**练习 2**：为什么 Start 里的传输逻辑必须放 goroutine，直接在 Start 里写完文件再返回不行吗？

**答案**：语法上行，语义上完全错。doStart 的 handler 持有 `task.lock` 且 `fetcher.Start()` 是同步调用（[downloader.go:L2090](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2090)），同步写 4096 字节没问题，但真实协议传输动辄几分钟：期间任务无法响应 Pause（Pause 也要拿 task.lock）、watch 循环启动被推迟、`emit(start)` 事件迟迟不发。契约要求 Start 只负责「启动」，完成与否经 DoneCh 异步汇报。

**练习 3**：`Progress()` 返回的切片长度和什么保持一致？

**答案**：和 `Opts.SelectFiles` 展开后的已选文件数一致（未选择即全部文件）。[pkg/download/model.go:L95-L99](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L95-L99) 的 `FileRuntimeStatus` 注释明确写了 "Fetchers return progress in SelectFiles order"。

### 4.3 DoneCh 完成通知：Wait 的握手协议

#### 4.3.1 概念说明

Fetcher 与 Downloader 之间最重要的同步原语是**完成通知**。契约是：

- Downloader 的 watch 循环阻塞在 `fetcher.Wait()` 上；
- Fetcher 在下载**结束**（成功或失败）时向 DoneCh 发送**恰好一个**值：`nil` 表示成功，非 nil error 表示失败；
- 暂停**不发通知**——被暂停的任务的 watch 协程就停在 `Wait()` 里，等下次 Continue 后 `Start()` 再跑完、再次发送时被唤醒。

`DefaultFetcher` 基类把这个模式标准化了：

- `Setup` 创建 `DoneCh = make(chan error, 1)`——**容量恰好 1**：一次运行最多缓存一个结果，第二个结果用非阻塞发送丢弃；
- `Wait()` 就是 `return <-f.DoneCh`。

容量 1 + 非阻塞发送的组合拳保证两件事：发送方**永远不会因为没人接收而卡死**（下载协程卡死会连累任务锁和整个调度）；一次运行**最多通知一次**（重复通知会让 watch 循环把同一个任务标记两次完成）。代价是「丢通知」——而这正是想要的：如果 watch 已经离开（任务被暂停/删除），迟到的结果本来就不该再被消费。

watch 循环拿到结果后的分派逻辑（伪代码）：

```text
for {
    err := fetcher.Wait()            // 阻塞等结果
    if err != nil {                  // 失败
        if 任务已 pause/wait { return }        // 暂停导致的取消，安静退出
        doOnError(task, err)                  // 状态置 error，发 error 事件
        if 任务仍 running { continue }        // ← 关键：循环回去再 Wait()（协议可自愈重试）
        return
    }
    // 成功
    if markTaskDone(task) == false { return } // 只有 running 代数能落 done
    saveTask; emit(done); emit(finally); notifyRunning()
    return
}
```

#### 4.3.2 核心流程

DoneCh 上的三种典型时间线：

```text
A. 成功完成
   Start() ─→ (传输协程工作) ─→ DoneCh <- nil ─→ Wait() 返回 nil
        ─→ markTaskDone: status=done, runGeneration++ ─→ emit(done/finally)

B. 传输失败
   Start() ─→ (出错) ─→ DoneCh <- err ─→ Wait() 返回 err
        ─→ doOnError: status=error ─→ emit(error)
        ─→ 若协议自愈（任务仍 running）→ 继续 for 循环再 Wait()

C. 暂停→继续
   Pause() ─→ (不发 DoneCh，watch 协程停在 Wait())
   Continue() ─→ doStart ─→ fetcher.Start() 再次运行
        ─→ 新一轮结束 ─→ DoneCh <- 结果 ─→ 沉睡的 Wait() 被唤醒
        ─→ markTaskDone 检查「当前状态是否还是 running 且代数一致」再落 done
```

#### 4.3.3 源码精读

基类实现：

[internal/fetcher/fetcher.go:L160-L174](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L160-L174) `DefaultFetcher`：三个导出字段 `Ctl`/`Meta`/`DoneCh`，`Setup` 只做两件事——记录 Controller、创建容量 1 的 DoneCh；`Wait` 一行——阻塞读 DoneCh。内嵌它即可免费获得符合契约的 Wait。

非阻塞发送的教科书现场在 HLS：

[internal/protocol/hls/fetcher.go:L140-L156](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/hls/fetcher.go#L140-L156) HLS Fetcher 内嵌 `fetcher.DefaultFetcher`，`Setup` 第一行调 `f.DefaultFetcher.Setup(ctl)`——注释明确写着「初始化 Ctl 和 DoneCh，Wait() 依赖它」。这是我们 mockFetcher 采用的同款姿势。

[internal/protocol/hls/fetcher.go:L557-L585](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/hls/fetcher.go#L557-L585) `supervise`：监督协程等所有分片 worker 结束后判定结果，最后四行是精髓——`select { case f.DoneCh <- err: default: }`。两条静默退出分支（ctx 被取消即暂停中）**根本不发送**，对应上面时间线 C；只有真正的成功/失败才非阻塞发送，且函数注释强调 "exactly once per run"。

消费端的完整逻辑：

[pkg/download/downloader.go:L1547-L1567](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1547-L1567) watch 循环头部：`Wait()` 返回 error 时，若任务已被置为 pause/wait 则直接 return（取消场景）；否则走 `doOnError`，并且**如果任务仍是 running 就 `continue` 回到循环顶部再次 `Wait()`**——这给了「协议自己重启传输」的自愈空间，我们的 mock 在 4.3.4 会利用这个分支。

[pkg/download/downloader.go:L1583-L1599](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1583-L1599) 成功分支：`markTaskDone` 返回 false 就退出（任务已不处于 running 代数，比如此刻恰好被删除/暂停），true 则落盘快照、`emit(done)`、`emit(finally)`（合成事件，紧跟 done 且恰好一次——u1-l4 讲过）、`notifyRunning()` 唤醒等待队列中的下一个任务。

[pkg/download/downloader.go:L2236-L2264](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2236-L2264) `markTaskDone`：双锁（task.lock + statusLock）保护下检查 `Status == running` 才迁移到 done——这就是 u3-l1 反复强调的「Fetcher 只汇报、Downloader 裁决」的落点。

另一个合法变体——不用 DoneCh 自己实现 Wait：

[internal/protocol/ed2k/fetcher.go:L290-L329](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ed2k/fetcher.go#L290-L329) ed2k 没有内嵌 DefaultFetcher，它的 `Wait` 订阅客户端的进度事件流，在 `waitCtx.Done()`（Close 取消）、通道关闭、transfer 被移除或 Finished 时返回 nil。而 [pkg/download/downloader_test.go:L135-L139 与 L185](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader_test.go#L135-L139) 的 mock 用自己的 `done chan error`（容量 2，因为测试要重复 Start）。**只要满足「阻塞直到有结果、nil 成功 err 失败、暂停时不返回」的语义，实现方式自由**；内嵌 DefaultFetcher 是最省事的路径。

#### 4.3.4 代码实践

**实践：触发并观察失败分支与自愈循环**

1. **实践目标**：亲眼看到 `DoneCh <- err` 如何变成 error 事件，以及 watch 循环的 `continue` 分支。
2. **操作步骤**：
   - 给 mockFetcher 加一个失败开关：URL 里带 `fail` 字样（如 `mock://fail.txt`）时，download 协程在写到一半时执行 `select { case f.DoneCh <- errors.New("mock disk corrupted"): default: }; return`（替换成功发送）。
   - 在 4.4 的 main 里把失败任务和成功任务各建一个，Listener 里分别打印 `error`、`finally` 事件的 `event.Err`。
3. **需要观察的现象**：
   - 失败任务收到 `EventKeyError`，`event.Err` 为 `mock disk corrupted`，任务最终状态 `error`；
   - 成功任务只收到 `done` + `finally`，不经过 error；
   - `finally` 事件恰好在 `done`/`error` 之后立刻到达一次。
4. **预期结果**：与 [watch 的 L1547-L1567](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1547-L1567) 逻辑一一对应。若想观察到 `continue` 自愈分支，可把失败逻辑改成「第一次失败、重试成功」（在 fetcher 里记一个 `retried atomic.Bool`），此时最终事件是 `done` 而非 `error`。
5. 以上运行结果**待本地验证**（依赖 4.4 的完整程序）。

#### 4.3.5 小练习与答案

**练习 1**：把 DoneCh 容量改成 2、发送改成裸的 `f.DoneCh <- nil`，什么场景下会出问题？

**答案**：暂停—继续场景。第一次运行结束时若 watch 协程恰好不在等待（例如暂停后立即完成），结果滞留在通道里；Continue 后第二次 Start 又发一个结果，通道里积压两个旧值，watch 醒来先消费到**过期的第一个结果**，可能把一个已被暂停又重启的任务错误地标记为 done。容量 1 + 非阻塞发送保证「一次运行至多一个在途结果」。项目测试的 mock 用容量 2 是因为它明确配合测试节奏重复 Start（[downloader_test.go:L137](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader_test.go#L137)），生产协议（HLS）一律容量 1。

**练习 2**：为什么暂停时不向 DoneCh 发送 nil（「反正也停了」）？

**答案**：因为 `Wait()` 返回 nil 的语义是「下载成功完成」，watch 会走 `markTaskDone` 把任务置为 done。虽然 `markTaskDone` 有 running 代数守卫大概率会拦住（返回 false 后 watch 退出），但这依赖守卫的正确性，且若暂停后用户立刻 Continue、新一轮还没 Start，旧 nil 可能把任务直接「完成」。正确做法是暂停时静默离开（HLS supervise 的取消分支、我们 mock 的 `ctx.Done()` 分支都是直接 return 不发送）。

**练习 3**：`Wait()` 的第二个返回值不存在——它只有 `error`。Downloader 怎么区分「成功」和「取消」？

**答案**：靠任务状态而不是返回值。取消场景下 Downloader **先**把状态置为 pause/wait（preparePause 里 `updateStatus` + `runGeneration++`），所以即使 Wait 之后醒来，[L1550-L1553](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1550-L1553) 检查到状态不是 running 就安静退出；成功场景下状态仍是 running，才走 done 分支。状态是唯一的裁决依据。

### 4.4 注册与验证：把 mock:// 接进 Downloader

#### 4.4.1 概念说明

「注册」在 Gopeed 里没有注册函数、没有 init() 魔法——**注册就是把 Manager 实例放进 `DownloaderConfig.FetchManagers` 切片**。两条规则：

1. **切片顺序就是协议优先级**（first-match-wins）。`DownloaderConfig.Init()` 只在切片**为空**时才填五个默认协议（ftp→hls→http→bt→ed2k）；你传入自己的切片就会**完全替换**默认表。想让 HTTP 等内置协议与 mock 并存，必须手动把五个默认 Manager 和你的 mockManager 一起放进切片（它们都是 internal 包，module 内可自由 import）。
2. **`Boot()` 无法注入自定义协议**。`Boot()` 内部是包级单例 `defaultDownloader = NewDownloader(nil)`，`Init()` 给它装好了默认五个协议，而 `Downloader` 的 `cfg` 字段不可导出、也没有任何导出的 Setter——从包外**没有任何途径**给它加 Manager。所以自定义协议必须走 `NewDownloader(&DownloaderConfig{FetchManagers: ...})` 自己构造，功能上与 Boot 完全等价（`Boot().Create` 做的也只是 `defaultDownloader.CreateDirect`，见 [L2791-L2797](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2791-L2797)）。

#### 4.4.2 核心流程

```text
mockManager{} 放进 FetchManagers
  → NewDownloader(cfg)   cfg.Init() 跳过默认注册（切片非空）、其余兜底照做
  → d.Setup()            建存储桶、加载配置、initProtocolConfigs 归一化 mock 配置、
  │                      加载历史任务（含 assignFetcherManager 重新路由）、启动后台任务
  → d.CreateDirect(&base.Request{URL:"mock://hello.txt"}, &base.Options{Path: dir})
  │     └─ parseFm 命中 mock → Build → setupFetcher → Resolve(后置) → Start → watch
  → d.Listener(fn) 捕获 start/progress/done/finally 事件
  → d.Close()      关闭所有 Manager（fm.Close()）与存储
```

#### 4.4.3 源码精读

注册表与替换语义：

[pkg/download/model.go:L211-L250](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L211-L250) `DownloaderConfig.Init()`：L231 的 `if len(cfg.FetchManagers) == 0` 是替换语义的直接证据；L232-L241 的注释解释了默认顺序里 FTP 排最前的原因（scheme 强意图优先于扩展名弱信号，u3-l2 详讲过）。

路由与工厂：

[pkg/download/downloader.go:L444-L453](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L444-L453) `parseFm`：双重循环按注册顺序找第一个 Filter 命中的 Manager，找不到返回 `ErrUnSupportedProtocol`。`mock://x` 会命中我们的 `FilterTypeUrl` + `Pattern: "mock"`（[SchemeFilter.Match](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L102-L120) 把两边都转大写后比 `MOCK:` 前缀）。

[pkg/download/downloader.go:L2301-L2309](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2301-L2309) `buildFetcher`：parseFm → `fm.Build()` → `d.setupFetcher(fm, fetcher)`。这就是「Build 之后立刻注入 Controller」的现场（u3-l3 的 4.1 逐字段讲过 setupFetcher 装了什么）。

建任务的完整链路：

[pkg/download/downloader.go:L1822-L1873](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1822-L1873) `doCreate`：再一次 parseFm（L1828，以**当时**的 URL 为准）、`task.Protocol = fm.Name()`（L1835，UI 分组标签由此而来）、`task.NeedsPreparation = true`（L1841）、落盘后按 MaxRunning 决定立即 doStart 还是进 waitTasks 队列（L1862-L1869）。

Boot 单例的限制（本讲的重要「防坑」结论）：

[pkg/download/downloader.go:L2768-L2805](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L2768-L2805) `defaultDownloader` 包级变量 + `Boot()` 只是 `Setup()` 它并返回收集器；`boot.Create` 最终调 `defaultDownloader.CreateDirect`。整条链路上没有给 `cfg.FetchManagers` 增删成员的导出入口。

[pkg/download/downloader.go:L140-L170](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L140-L170) `NewDownloader`：nil 配置兜底为空配置后调 `cfg.Init()`——这就是默认五协议的装配点；同时初始化锁、logger、fetcherCache。练习程序要给 `StorageDir` 一个真实目录（日志文件会写在 `<StorageDir>/logs/core.log`），`Storage` 缺省为内存存储。

验证的活例子：

[pkg/download/selection_test.go:L11-L64](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/selection_test.go#L11-L64) `TestCreateResolvedFileSelection`：`NewDownloader(&DownloaderConfig{FetchManagers: []fetcher.FetcherManager{manager}})` → `Setup()` → `d.cfg.MaxRunning = 0`（让任务停在 wait 队列便于断言）→ `Resolve` → `CreateWithOptions` → 检查 Task 的 SelectFiles 与落盘数据。这就是「注册 mock 协议做验证」的官方姿势，我们的 main 函数照此办理。

#### 4.4.4 代码实践

**实践：完整跑通 mock:// 下载**

1. **实践目标**：用 Boot 等价的链路（NewDownloader + CreateDirect + Listener）完成一次 mock:// 下载，验证文件真实产出、事件按序到达。
2. **操作步骤**：在 `_examples/mock/main.go` 补上 main（**示例代码**）：

   ```go
   func main() {
       dir, err := os.MkdirTemp("", "mock-dl")
       if err != nil {
           panic(err)
       }

       d := download.NewDownloader(&download.DownloaderConfig{
           Storage:         download.NewMemStorage(),
           StorageDir:      dir,
           TempDir:         dir,
           FetchManagers:   []fetcher.FetcherManager{&mockManager{}},
           RefreshInterval: 100, // 加快事件频率，便于观察
       })
       if err := d.Setup(); err != nil {
           panic(err)
       }
       defer d.Close()

       finallyCh := make(chan error, 1)
       d.Listener(func(event *download.Event) {
           if event.Task != nil {
               fmt.Printf("[event] %-8s status=%-7s downloaded=%d/%d\n",
                   event.Key, event.Task.Status,
                   event.Task.Progress.Downloaded, event.Task.Meta.Res.Size)
           }
           if event.Key == download.EventKeyFinally {
               finallyCh <- event.Err
           }
       })

       taskId, err := d.CreateDirect(
           &base.Request{URL: "mock://hello.txt"},
           &base.Options{Path: filepath.Join(dir, "out")},
       )
       if err != nil {
           panic(err)
       }
       fmt.Println("task id:", taskId)

       if err := <-finallyCh; err != nil {
           fmt.Printf("download fail: %v\n", err)
           os.Exit(1)
       }
       data, err := os.ReadFile(filepath.Join(dir, "out", "hello.txt"))
       if err != nil {
           panic(err)
       }
       fmt.Printf("download ok: %d bytes\n", len(data))
   }
   ```

   然后运行：

   ```bash
   go run ./_examples/mock
   ```

3. **需要观察的现象**：
   - 终端先打 `task id: <nanoid>`，随后一串 `progress` 事件，`downloaded` 从 0 涨到 4096，最后 `done` 与 `finally` 各一次；
   - `<dir>/out/hello.txt` 被创建，大小恰好 4096 字节，内容是 `abcdefghijklmnopqrstuvwxyz...` 循环填充；
   - `<dir>/logs/` 下出现 `core.log`（NewDownloader 建的日志）。
4. **预期结果**：`download ok: 4096 bytes`。若把 `FetchManagers` 里换成 `{&mockManager{}, new(btProtocol.FetcherManager), ...}` 五个内置管理器补齐，则 `mock://` 与磁力链接可以在同一个 Downloader 里并存。
5. 本实践在编写本讲义时**未实际运行**，命令与现象为「待本地验证」；若编译报错，优先检查 import 是否合并完整（`context/errors/fmt/os/path/filepath/strings/sync/atomic/time` + 内部包五个）。

#### 4.4.5 小练习与答案

**练习 1**：把 `CreateDirect` 连调三次（MaxRunning 保持默认 5），三个任务的启动顺序如何？如果先把 `d.cfg.MaxRunning` 改成 1 呢？

**答案**：默认 5 时三个任务几乎同时进 running，各自 40 毫秒左右完成。MaxRunning=1 时第一个立即 running，后两个进 wait 队列（[doCreate L1862-L1869](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1862-L1869)），每个任务 done 后 `notifyRunning()`（[L1595](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1595)）唤醒下一个——事件日志里会看到严格的串行节奏。注意运行期直接改 `d.cfg.MaxRunning` 是测试代码的惯用手法（如 [downloader_test.go:L584](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader_test.go#L584)），正式配置应走 `PutConfig`。

**练习 2**：任务完成后重启程序（换一个新的 main 进程、同一个 Storage），任务列表里还会看到 mock 任务吗？

**答案**：本节版本看不到——`NewMemStorage` 是内存存储，进程退出数据即消失。即使换成 BoltStorage，第一版 mockManager 的 `Store` 返回 nil（无任务级状态），重启后任务会出现在列表里（Task 本身总落盘）且状态回落 pause（[Setup L240-L242](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L240-L242)），但 Continue 后文件会**从零重写**——因为没实现状态持久化。补齐它正是综合实践的任务。

**练习 3**：为什么练习程序要显式传 `StorageDir`，不传会怎样？

**答案**：`NewDownloader` 会往 `<StorageDir>/logs/core.log` 写日志（[L163-L165](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L163-L165)），StorageDir 为空字符串时日志写到 `/logs/core.log`，在多数系统上会因无权限而 panic 或静默失败。生产入口（cmd/server.go）总是先确定存储目录再建 Downloader（u2-l3 讲过目录确定后种默认分类的顺序依赖）。

## 5. 综合实践

**任务：把 mock 协议升级为「可暂停、可跨重启续传」的完整协议。**

参考 `recoveryTestManager`（[downloader_test.go:L191-L222](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader_test.go#L191-L222)）的 Store/Restore 模式，按以下步骤改造 `_examples/mock`：

1. **定义持久化状态**：给 mockFetcher 加 `written int64` 字段的持久化版本——`type mockState struct { Written int64 }`。
2. **实现 Store**：`func (m *mockManager) Store(f fetcher.Fetcher) (any, error) { mf := f.(*mockFetcher); return &mockState{Written: mf.written.Load()}, nil }`。
3. **实现 Restore**：返回 `&mockState{}` 模板；构造器里把 `v.(*mockState).Written` 存进新 Fetcher，并让 `download()` 从该偏移继续 `WriteAt`，同时 `written.Store(start)` 初始化进度。
4. **换持久化存储**：main 里改用 `download.NewBoltStorage(filepath.Join(dir, "data.db"))`（构造函数见 [pkg/download/storage.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/storage.go)），Downloader 的 StorageDir 指向同一 dir。
5. **模拟中断**：把 mockSize 加大到 40960（约 0.8 秒），启动下载后约 0.3 秒时 Ctrl+C 杀掉进程。
6. **重启验证**：再跑一次程序，改为「列出任务并 Continue」模式：`d.GetTasks()` 找到 pause 状态的 mock 任务，调 `d.Continue(&download.TaskFilter{IDs: []string{id}})`，监听 finally。
7. **通过标准**：
   - 重启后任务存在且状态为 pause（而非 ready/done）；
   - Continue 后 progress 从接近中断点的字节数继续增长，而不是从 0 开始；
   - 最终文件仍是完整的 40960 字节（Touch 预分配保证了文件长度，续传只是把缺的区间补上）；
   - 用 `cmp`/`sha256sum` 对比「一次下完」与「中断续传」两个版本的文件内容完全一致。

这个实践把本讲四个模块全部串起来：Manager 骨架（Store/Restore 升级）、Resolve/Start（偏移续写）、DoneCh（暂停静默退出 + 重启后再通知）、注册与验证（BoltStorage + 跨进程恢复）。做完它，你就拥有了一个协议该有的全部「器官」，接下来读 HTTP/BT 的源码时，你会知道自己看到的每个复杂结构对应哪个器官的强化版。

## 6. 本讲小结

- **FetcherManager 骨架 = 九个方法四组职责**（身份/路由/工厂/配置与持久化），项目测试里的 `generationTestManager` 证明最小实现不到 20 行；`DefaultConfig()` 返回 nil 会让整个 Downloader `Setup()` 失败。
- **Resolve 只做资源发现（幂等、无 IO），Start 只负责启动传输**；单文件资源 Name 必须留空，路径用 `SingleFilepath()` 算；目标文件用 `f.Ctl.Touch(path, size)` 一步创建（建目录+建文件+稀疏预分配）。
- **DoneCh 完成通知 = 容量 1 + 非阻塞发送 + 恰好一次**：nil 成功、err 失败、暂停静默退出不发；watch 循环消费结果，错误后若任务仍 running 会回到循环再次 Wait（自愈窗口）。
- **Store/Restore 是两段式契约**：Store 返回可序列化状态（nil 表示无状态并清旧记录）；Restore 返回「空模板 + 构造器」，Downloader 负责读盘回填、回填后必调 setupFetcher 并补齐 meta 三元组。
- **注册就是把 Manager 放进 `FetchManagers` 切片**，切片顺序即协议优先级；传入自定义切片会整体替换默认五协议；`Boot()` 单例无法注入自定义协议，必须 `NewDownloader`。
- **setupFetcher 的注入时机固定**：Build/Restore 之后、任何业务方法之前，每 Fetcher 恰好一次。

## 7. 下一步学习建议

本讲完成了协议接入层（第三单元）的收官：你现在有能力给 Gopeed 添加一个新协议了。接下来的路线：

1. **u4-l1（Downloader 的结构体与初始化）**：本讲多次「路过」doCreate/doStart/watch，第四单元会把 Downloader 本体的字段、锁、存储桶逐一拆开精读。
2. **u4-l5（持久化与断点恢复）**：综合实践只做了最小续传，正式的检查点保存策略（shouldSaveTaskCheckpoint）、restoreTask 的完整路径在那一讲展开。
3. **u5-l1/l2（HTTP 协议深潜）**：把本讲的 mock 与 HTTP Fetcher 对照阅读——Resolve 的探测请求与 asyncPrefetch、Start 的多连接分块与慢启动控制器，你会看到同一套契约在真实网络协议下的完整形态。
4. **u11-l1（测试体系）**：本讲大量引用的 `generationTestManager` 是测试替身的典范，第十一单元会系统讲解如何为自定义协议补测试（Resolve、断点续传、错误分支）。
