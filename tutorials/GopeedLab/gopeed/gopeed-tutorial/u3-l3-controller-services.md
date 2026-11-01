# u3-l3 Controller 与共享基础服务

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `internal/controller` 包在 Downloader 与 Fetcher 之间的**中介角色**：它把「只有 Downloader 才知道答案的问题」（全局配置是什么、代理用哪个、临时文件归谁管、日志写到哪里）打包成一个结构体，在 `fetcher.Setup()` 时注入给协议实现。
2. 掌握 `DefaultFileController.Touch` 的三步动作（建目录 → 建文件 → 按大小截断），理解「预分配文件大小」对多连接分块下载的意义。
3. 了解 `tempfiles.Scope` 如何用「登记—移除—统一关闭」三步管理临时文件，保证 Downloader 关闭时不泄漏、也不误删别人的文件。
4. 知道 `logger` 与 `httpclient` 两个基础设施包如何被 Controller 串联起来：Logger 从 Downloader 流向协议，GetProxy 的产物成为 httpclient 的传输层代理。

## 2. 前置知识

- **依赖注入（Dependency Injection）**：不把「全局信息」写成全局变量，而是由上层构造一个装满回调/资源的对象传给下层。Gopeed 的 `Controller` 就是这样一个「服务包」。协议实现（internal/protocol 下的各 Fetcher）永远不直接 import Downloader，它需要的一切都从 Controller 拿。
- **回调函数即接口**：Go 里 `GetConfig func(v any)` 这种函数字段，本质是一个只有单个方法的接口的简化写法。Downloader 把「读协议配置」的实现闭包塞进去，Fetcher 调用它时完全不知道背后是 bbolt 存储。
- **稀疏文件（sparse file）与 `os.Truncate`**：把文件长度直接设为 N 字节，多数文件系统（ext4/APFS/NTFS）并不会立刻写入 N 个零，只在索引上登记大小。多连接下载各写各的偏移区间时，必须先把文件「撑」到最终大小，否则后写的连接会越界失败。
- **zerolog**：Gopeed 使用的结构化日志库（`github.com/rs/zerolog`），以链式 API 和低分配著称。
- **imroc/req v3**：`internal/httpclient` 用它实现「浏览器指纹伪装」传输层，用于绕过部分站点的 TLS 指纹拦截。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [internal/controller/controller.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/controller/controller.go) | 定义 `Controller` 结构体、`FileController` 接口与默认实现 `DefaultFileController`（本讲主角） |
| [internal/tempfiles/scope.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/tempfiles/scope.go) | 临时文件作用域 `Scope`：Track/Remove/Close |
| [pkg/download/temp.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/temp.go) | Downloader 侧的临时目录初始化，创建全局唯一的 `Scope` |
| [internal/logger/logger.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/logger/logger.go) | 基于 zerolog 的 Logger 包装：写文件或 stdout |
| [internal/httpclient/client.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/httpclient/client.go) | HTTP 客户端工厂：native / 固定浏览器 / 自适应伪装三种传输层 |
| [pkg/download/downloader.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go) | `setupFetcher` 装配 Controller；`Close` 统一清理临时文件 |
| [pkg/download/model.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go) | `DownloaderConfig` 持有 `Controller` 字段 |
| [internal/protocol/http/fetcher.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go) | HTTP 协议对 Touch/TempFiles/GetConfig 的消费现场 |
| [internal/protocol/ftp/fetcher.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ftp/fetcher.go) | FTP 协议对 Touch/Logger 的消费现场（含 nil 防御写法） |

## 4. 核心概念与源码讲解

### 4.1 Controller 结构与回调注入

#### 4.1.1 概念说明

u3-l1 讲过 Fetcher 的生命周期契约：Downloader 调用 `fetcher.Setup(ctl)` 是 Fetcher 诞生的第一步。这个 `ctl` 参数就是 `controller.Controller`——一个「共享服务礼包」。

为什么需要它？因为协议实现处在 `internal/` 下，按 Go 的 internal 可见性规则，它**不能** import `pkg/download` 去读 Downloader 的配置；即使可以，直接依赖也会把「一个 Fetcher 对应一个 Downloader」的假设焊死，没法单独测试。Controller 把方向反过来：Downloader 把答案准备好，塞进 Controller，Fetcher 只管提问。

Controller 里装的问题清单：

| 字段 | 回答的问题 | 填充者 |
| --- | --- | --- |
| `TempFiles *tempfiles.Scope` | 我创建的临时文件归谁清理？ | Downloader 的全局 Scope |
| `TempDir string` | 临时文件应该放哪个目录？ | `cfg.TempDir`（绝对路径） |
| `ManagedProduction func(string) bool` | 这个 URL 是不是内部 Blob 资源？ | blob 注册表 |
| `GetConfig func(v any)` | 我的协议级全局配置是什么？ | `getProtocolConfig` |
| `GetProxy func(*base.RequestProxy) ...` | 这次请求该走哪个代理？ | 全局代理 + 任务级代理合成 |
| `Logger *zerolog.Logger` | 日志写到哪里？ | Downloader 的 core.log |
| `FileController`（内嵌接口） | 目标文件怎么创建？ | 默认 `DefaultFileController` |

#### 4.1.2 核心流程

装配发生在 `setupFetcher`，每个 Fetcher 被使用前都会拿到一个**新建**的 Controller：

```text
Downloader.setupFetcher(fm, fetcher)
  ├─ ctl := controller.NewController()        // 独立实例，字段是默认实现
  ├─ ctl.TempDir    = d.cfg.TempDir           // 来自 setupTempDir 的绝对路径
  ├─ ctl.TempFiles  = d.tempFiles             // 全下载器共享的 Scope
  ├─ ctl.ManagedProduction = blob.Production  // 内部资源判定
  ├─ ctl.GetConfig  = getProtocolConfig(fm.Name(), v)
  ├─ ctl.Logger     = &d.Logger.Logger        // 可能为 nil
  ├─ ctl.GetProxy   = 闭包：blob 旁路 + 三态代理
  └─ fetcher.Setup(ctl)                       // 注入完成
```

注意一个关键事实：`setupFetcher` 从头 `NewController()` 新建实例，**没有读取 `d.cfg.Controller`**。也就是说 `DownloaderConfig.Controller` 字段目前只在 `Init()` 里做非空兜底，用户注入进去的自定义 Controller 并不会传导到任何 Fetcher（本讲 4.1.4 的实践会验证这一点）。这是当前 HEAD 的真实行为，阅读时不要想当然。

#### 4.1.3 源码精读

先看 Controller 本体——一堆函数字段加一个内嵌接口：

[internal/controller/controller.go:L13-L28](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/controller/controller.go#L13-L28) 定义 `Controller` 结构体与内嵌的 `FileController` 接口。每个字段都是一个「Downloader 才能回答的问题」：临时文件作用域、临时目录、Blob 判定、配置读取、代理解析、日志器，最后嵌入 `FileController` 接口使 `ctl.Touch(...)` 可以直接调用。

[internal/controller/controller.go:L33-L41](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/controller/controller.go#L33-L41) 是 `NewController()` 默认构造：`GetConfig` 为空操作、`GetProxy` 直接委托给 `requestProxy.ToHandler()`、`FileController` 用默认实现。这个默认构造主要服务于「脱离 Downloader 单测一个 Fetcher」的场景。

再看 Downloader 这一侧的装配现场：

[pkg/download/downloader.go:L455-L487](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L455-L487) 是 `setupFetcher` 的全部逻辑：新建 Controller 后逐字段覆盖默认值，最后 `fetcher.Setup(ctl)` 完成注入。注意 `Logger` 字段只在 `d.Logger != nil` 时赋值——所以 Fetcher 拿到的 `ctl.Logger` 完全可能是 nil，协议代码必须判空（见 4.4）。

其中 `GetProxy` 闭包最复杂，值得单独读：

[pkg/download/downloader.go:L466-L485](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L466-L485) 实现代理决策链：① 若请求 URL 是已注册的 Blob 回环地址，一律不走代理；② 任务未指定代理时用全局 `d.cfg.Proxy`；③ 任务指定 `none` 强制直连；④ 指定 `custom` 用任务自己的代理；⑤ 其余（`follow`）回落全局。这正是 u2-l1 讲过的 RequestProxy 三态在运行期的落点。

[pkg/download/downloader.go:L1501-L1514](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1501-L1514) 是 `GetConfig` 闭包背后的实现：从存储读出全局配置，按协议名（FetcherManager 的 `Name()`）取出 `ProtocolConfig[name]`，经 `util.MapToStruct` 反序列化到协议自己的 config 结构体。失败只记警告并返回 false，协议随后用默认配置兜底。

最后确认 `DownloaderConfig.Controller` 的现状：

[pkg/download/model.go:L211-L230](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L211-L230) 中 `Controller` 是 `DownloaderConfig` 的第一个字段，但 `Init()` 只在它为 nil 时填一个默认实例；全文检索可以确认 `setupFetcher` 并不读取它。换句话说，当前代码里这个字段是一个「预留的注入点」，尚未接线。

#### 4.1.4 代码实践

**实践 A：追踪一次 HTTP 下载中 Touch 的调用点（源码阅读型）**

1. **实践目标**：搞清楚「目标文件是哪段代码创建的」，为实践 B 的自定义 FileController 找准挂点。
2. **操作步骤**：
   - 在仓库根目录执行 `grep -rn "\.Touch(" internal/`，应得到两个协议实现里的调用点（HTTP 与 FTP）。
   - 打开 [internal/protocol/http/fetcher.go:L850-L866](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L850-L866)：`Start` 里先 `os.Stat(name)`，文件**不存在**才走 `f.ctl.Touch(name, f.knownSize())`；文件**已存在**（断点续传）则 `os.OpenFile(name, os.O_RDWR, os.ModeAppend)` 保留内容继续写。也就是说 Touch 只在「全新任务」时被调用。
   - 对照 [internal/protocol/ftp/fetcher.go:L310-L328](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ftp/fetcher.go#L310-L328)：FTP 的 `openFiles` 在「新计划」或「文件丢失」两种情况下对每个选中文件调用 `f.ctl.Touch`，且文件丢失时会把这个文件的分段进度清零重下。
3. **需要观察的现象**：两个协议都在「文件尚不存在」这个分支上调用 Touch，语义是「创建并定长」；续传路径完全绕开 Touch。
4. **预期结果**：能画出 HTTP 下载第一秒的文件操作序列——`os.Stat` 失败 → `Touch`（内含 MkdirAll/Create/Truncate）→ 之后所有连接用 `WriteAt` 写各自区间。
5. 结论可直接从源码得出，无需运行。

**实践 B：验证 DownloaderConfig.Controller 是否真的会传导（源码阅读型）**

1. **实践目标**：亲手确认 4.1.2 提到的「cfg.Controller 不接线」事实，养成「不信结论信代码」的习惯。
2. **操作步骤**：执行 `grep -n "cfg.Controller" pkg/download/*.go`。只会命中 `model.go` 的 Init 兜底两行；`setupFetcher` 中搜索 `NewController` 则能看到每次都新建。
3. **需要观察的现象**：没有任何一行代码把 `d.cfg.Controller` 赋给局部 `ctl` 或拷贝其字段。
4. **预期结果**：确认「向 `DownloaderConfig.Controller` 塞自定义 FileController 不会影响任何 Fetcher」。若未来想把这里接通，最小改动是在 `setupFetcher` 里让新 ctl 继承 `d.cfg.Controller.FileController`——这是改动建议，不是现有行为。
5. 待本地验证的部分见 4.2.4 实践。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Controller` 的字段大多是函数/回调，而不是直接放配置值？
**答案**：配置是动态的（用户随时可能改全局代理、改协议配置），Fetcher 需要的是「每次问到都拿到最新答案」；同时 Downloader 读配置要加 `configLock`、要走存储，这些细节不该泄漏给协议层。回调把「何时读、怎么读」留在 Downloader，Fetcher 只保留「问」的动作。

**练习 2**：`setupFetcher` 为每个 Fetcher 新建一个 Controller 实例，而不是共享一个，有什么好处？
**答案**：Controller 里的字段目前虽然都是无状态闭包，但接口形态（尤其 `FileController`）允许有状态实现；每 Fetcher 一份实例避免了协议之间通过共享 Controller 意外串扰，也让未来按任务定制服务成为可能。共享的部分（`TempFiles`、`Logger`）通过指针/闭包引用同一底层对象。

**练习 3**：协议实现能反过来调用 `pkg/download` 的导出函数吗？
**答案**：不能。协议实现在 `internal/protocol/...`，Go 的 internal 包可见性规则只允许其父目录树内的包 import 它，且这里的问题方向相反——`pkg/download` import 协议实现，若协议再 import `pkg/download` 会形成循环导入。Controller 正是打破这个循环的中介。

### 4.2 DefaultFileController.Touch 与文件预分配

#### 4.2.1 概念说明

`FileController` 是 Controller 内嵌的单方法接口：

```go
type FileController interface {
    Touch(name string, size int64) (file *os.File, err error)
}
```

它回答的问题是：「请把下载目标文件准备好，长度为 size，返回可写句柄」。之所以做成接口而不是写死的工具函数，是因为「如何创建文件」存在可替换的空间（例如未来换成稀疏感知的 `fallocate`、或者加磁盘配额检查）；测试里也可以换成记录型实现（本讲实践）。

「预分配」的动机：HTTP 多连接下载时，连接 A 写 0~10MB，连接 B 写 10MB~20MB。如果文件初始长度为 0，连接 B 的 `WriteAt` 在部分平台上会失败或产生语义混乱。先把文件截断到最终大小，文件系统只在元数据里登记长度（稀疏文件），几乎零 IO 成本，却让所有偏移立刻可写。

#### 4.2.2 核心流程

```text
Touch(name, size)
  ├─ dir := filepath.Dir(name)
  ├─ os.MkdirAll(dir, ModePerm)   // 多文件资源的目录树可能尚不存在
  ├─ file = os.Create(name)        // 创建（或截断为 0）目标文件
  └─ if size > 0:
        os.Truncate(name, size)    // 登记最终长度（稀疏）
  返回 file（注意：Truncate 后返回的还是 Create 时的句柄，偏移 0）
```

调用时机（由协议决定，见 4.1.4 实践 A）：

- HTTP：`Start` 中 `os.Stat` 发现目标文件不存在 → `Touch(name, knownSize())`；未知大小资源传 0，之后按需扩展。
- FTP：`openFiles` 对「新计划」或「续传中丢失」的每个文件调用 `Touch(name, res.Files[i].Size)`。

#### 4.2.3 源码精读

[internal/controller/controller.go:L43-L59](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/controller/controller.go#L43-L59) 是 `DefaultFileController.Touch` 的完整实现：三步走——`MkdirAll` 保证父目录存在（BT/FTP 多文件任务的子目录常不存在）、`os.Create` 建文件、`size > 0` 时 `os.Truncate` 把长度登记为 size。返回的 `*os.File` 供调用方用 `WriteAt` 按偏移写入。

[internal/protocol/http/fetcher.go:L850-L869](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L850-L869) 展示 HTTP 的消费现场与续传分支的分野：注释「Open or create target file first (needed for prefetch copy)」说明这个文件还得承接 Resolve 阶段预取数据的拷贝（u5-l1 会展开）。文件创建后被存进 `f.file`，由 `fileMu` 保护。

[internal/protocol/ftp/fetcher.go:L300-L328](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ftp/fetcher.go#L300-L328) 是 FTP 版消费现场，函数头注释写明三种情形：全新计划创建并定长、续传保留内容、文件丢失从头再来。丢失重来的分支还顺手把该文件对应分段的 `Downloaded` 清零，防止进度条虚高。

#### 4.2.4 代码实践

**实践：实现一个「记录每次文件创建」的自定义 FileController 并注入验证**

由于 4.1.4 已确认 `DownloaderConfig.Controller` 不接线，正确的注入方式是项目测试同款：**直接构造 Controller 再调用 `fetcher.Setup(ctl)`**。参考 [internal/protocol/ftp/fetcher_test.go:L40-L47](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ftp/fetcher_test.go#L40-L47) 的 `testController` 与 [internal/protocol/ftp/fetcher_test.go:L736-L740](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ftp/fetcher_test.go#L736-L740) 的 Logger 注入写法。

以下为**示例代码**（不是仓库原有文件），可保存为 `internal/protocol/http/touch_log_practice_test.go` 后运行：

```go
package http

import (
	"fmt"
	"os"
	"testing"

	"github.com/GopeedLab/gopeed/internal/controller"
	"github.com/GopeedLab/gopeed/internal/protocol/http" // 若同包测试则删除此行
)

// loggingFileController 在标准实现外打印每次创建
type loggingFileController struct {
	inner controller.FileController
}

func (l *loggingFileController) Touch(name string, size int64) (*os.File, error) {
	fmt.Printf("[Touch] creating %s (size=%d)\n", name, size)
	return l.inner.Touch(name, size) // 复用默认实现完成真正工作
}

func TestLoggingFileController(t *testing.T) {
	ctl := controller.NewController() // TempFiles 等为默认零值
	ctl.FileController = &loggingFileController{inner: &controller.DefaultFileController{}}

	fm := new(FetcherManager)
	f := fm.Build().(Fetcher) // 注意：同包测试时类型断言写 *Fetcher
	f.Setup(ctl)
	// 接下来按 u5 的测试方式 Resolve 一个本地 httptest 地址并 Start，
	// 观察标准输出中出现 "[Touch] creating ..." 一行。
}
```

1. **实践目标**：证明「替换 FileController 即可拦截所有目标文件创建」，并亲眼看到 Touch 的调用参数。
2. **操作步骤**：把示例放进 `internal/protocol/http` 包的同名 `_test.go`（同包则无需 import 该包）；参照同目录现有测试（如 `fetcher_test.go` 里基于 `internal/test` 的 httptest 用例）补一个本地下载；`go test ./internal/protocol/http/ -run TestLoggingFileController -v`。
3. **需要观察的现象**：标准输出打印创建路径与 size；size 与响应 `Content-Length` 一致（支持 Range 时）。
4. **预期结果**：Touch 恰好被调用一次（新任务）；把任务删除重下可再触发一次。若你改为向 `DownloaderConfig.Controller` 注入同样实现（经 `download.NewDownloader` 走完整链路），则**不会**看到任何输出——这验证了 4.1.4 的结论。
5. 示例中的包导入细节因测试文件放置位置而异，**待本地验证**后微调。

#### 4.2.5 小练习与答案

**练习 1**：`Touch` 为什么先 `os.Create` 再 `os.Truncate`，而不是一步 `os.OpenFile(name, O_RDWR|O_CREATE|O_TRUNC, ...)`？
**答案**：`O_TRUNC` 只是把长度截为 0，仍需第二次调用登记目标长度；两步写法把「创建」与「定长」解耦，`size <= 0`（未知大小资源）时可以直接跳过定长。`os.Create` 本身等价于 `O_RDWR|O_CREATE|O_TRUNC`，语义清晰。

**练习 2**：续传时 HTTP 走 `os.OpenFile(name, os.O_RDWR, os.ModeAppend)` 而不是 Touch，若此时磁盘上的文件比记录的 size 小会发生什么？
**答案**：写偏移仍在旧文件长度范围内时可以工作，但超出物理长度的 `WriteAt` 会把中间空洞填零，造成「假装下载完成」的损坏文件。所以 u5-l3 会讲到 HTTP fetcher 对 Range 响应的严格校验与进度一致性检查；FTP 的对策则是检测到文件丢失时清零该文件分段进度重下。

**练习 3**：为什么把 `FileController` 设计成接口而不是 Controller 上的一个函数字段（如 `Touch func(...)`）？
**答案**：单方法接口可以让实现方携带自己的字段与状态（如本实践的 `inner`），并且能被整体替换、嵌套装饰；函数字段虽然也能做到，但接口让「这是一组文件操作能力」的意图更明确，将来增加 `Remove`/`Move` 等方法不用改 Controller 结构体形状。

### 4.3 tempfiles.Scope：临时文件的作用域管理

#### 4.3.1 概念说明

下载过程中会产生「中间文件」：HTTP Resolve 阶段的预取数据、HLS 的分片暂存等。它们的特点是：**用完即弃、异常时最容易遗留**。如果进程崩溃或任务出错，这些文件就成了垃圾。

Gopeed 的解法是「登记制」：Downloader 拥有一个 `tempfiles.Scope`，任何组件创建临时文件后立刻 `Track(path)` 登记；正常用完 `Remove(path)`（删文件+销账）；Downloader 关闭时 `Close()` 把没销账的路径全部删除。关键约束写在包注释里——临时目录**可能与操作系统或其他应用共享**，所以初始化时绝不清理目录内容，关闭时也只删「自己登记过的路径」。

#### 4.3.2 核心流程

```text
Downloader.Setup
  └─ setupTempDir: TempDir 为空则用 os.TempDir()
     → 转绝对路径 → MkdirAll(0700) → d.tempFiles = &Scope{}

HTTP Resolve 预取（消费方）
  os.CreateTemp(TempDir, "gopeed-prefetch-*")
  → Scope.Track(path)          // 登记
  → ...写入预取数据...
  → Start 时拷贝进目标文件
  → cleanupPrefetchFile: Scope.Remove(path)  // 销账

Downloader.Close
  └─ closeArr 末尾: storage.Close, tempFiles.Close
       Close: 置 closed → 遍历 paths 逐个 RemoveAll
       （此后再来 Track 的路径会被立即删除并报 ErrClosedPipe）
```

三重防御值得注意：`Track` 在 `closed` 之后到达时，**当场删除**新路径并返回错误，杜绝「关闭后泄漏」；`Remove`/`Close` 对 nil receiver 容忍（nil Scope 时 Remove 退化为直接 `os.RemoveAll`，Track 直接放行），所以协议代码无需判空就能调用；`Close` 逐个删除并聚合错误，单个失败不中断。

#### 4.3.3 源码精读

[internal/tempfiles/scope.go:L1-L16](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/tempfiles/scope.go#L1-L16) 包注释直陈设计动机：不要求私有父目录、不扫描系统 temp 根目录，只追踪「本下载器拥有的路径」。`Scope` 由一把互斥锁、一个 path 集合与 closed 标志构成。

[internal/tempfiles/scope.go:L20-L34](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/tempfiles/scope.go#L20-L34) 是 `Track`：nil 容忍、关闭后到达的路径立即删除并以 `io.ErrClosedPipe` 报错——这是「关闭后防泄漏」的关键一行。

[internal/tempfiles/scope.go:L35-L46](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/tempfiles/scope.go#L35-L46) 是 `Remove`：删文件、销账，nil Scope 时退化为直接删除。

[internal/tempfiles/scope.go:L47-L63](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/tempfiles/scope.go#L47-L63) 是 `Close`：置位 closed 后遍历删除全部登记路径，`errors.Join` 聚合每条失败。

[pkg/download/temp.go:L10-L26](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/temp.go#L10-L26) 是 Downloader 侧初始化：确定绝对路径、建目录、创建全局 Scope。注释再次强调「初始化从不清空目录、关闭只删自己创建的路径」。

消费现场在 HTTP 预取：

[internal/protocol/http/fetcher.go:L601-L624](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L601-L624) 创建预取临时文件：优先用 `ctl.TempDir`（先 `MkdirAll(0700)`），`os.CreateTemp(tempDir, "gopeed-prefetch-*")` 生成唯一名，随后 `f.ctl.TempFiles.Track(tmpFile.Name())` 登记；Track 失败则立刻关闭并删除该文件。

[internal/protocol/http/fetcher.go:L705-L718](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go#L705-L718) 是销账端 `cleanupPrefetchFile`：关闭句柄后经 `ctl.TempFiles.Remove` 删除（ctl 为 nil 时退化为裸 `os.Remove`），再清空字节数等复用状态。

[pkg/download/downloader.go:L1239-L1248](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1239-L1248) 把 `d.tempFiles.Close` 排进关闭序列的**最后一站**（在暂停全部任务、关闭各协议管理器、blob、存储之后），保证轮到它时不会再有组件新创建临时文件。

#### 4.3.4 代码实践

**实践：观察一次 HTTP 下载的临时文件生命周期（运行型）**

1. **实践目标**：亲眼看到 `gopeed-prefetch-*` 临时文件的出现与消失，理解 Scope 的登记—销账节奏。
2. **操作步骤**：
   - 准备一个本地 HTTP 服务，例如 `python3 -m http.server 18923`（在其目录放一个几十 MB 的文件）。
   - 仿照 `_examples/basic/main.go`（u1-l4 讲过）写一个小程序，`DownloaderConfig` 的 `TempDir` 显式指定为一个空目录（如 `/tmp/gopeed-practice`）。
   - 下载期间另开终端：`watch -n 0.2 'ls -la /tmp/gopeed-practice'`。
3. **需要观察的现象**：Resolve 阶段出现名为 `gopeed-prefetch-*` 的文件且大小持续增长；任务完成（或开始传输）后该文件消失，目录变空。
4. **预期结果**：临时文件在任务收尾时被 `cleanupPrefetchFile → TempFiles.Remove` 销账删除；若中途 `kill -9` 进程，文件残留（Scope 在进程内，无法善后），重启后旧残留**不会**被新 Downloader 误删——因为它从未被新 Scope 登记。
5. 进程被强杀后的残留行为**待本地验证**（不同平台 CreateTemp 位置一致，但观察点在文件系统）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Scope.Close` 之后 `Track` 要「立即删除路径」而不是直接报错了事？
**答案**：调用方（协议协程）可能还没感知到关闭，它创建临时文件后会继续写入。若只报错不删，文件就永久留在共享临时目录里。当场删除 + 返回 `ErrClosedPipe` 既清理了垃圾，又让调用方立刻失败并走收尾逻辑。

**练习 2**：`Track`/`Remove`/`Close` 都对 nil receiver 做了处理，这为调用方省掉了什么？
**答案**：省掉了 `if ctl != nil && ctl.TempFiles != nil` 这类防御代码。协议代码可以放心写 `f.ctl.TempFiles.Track(...)`；nil 时 Track 是 no-op、Remove 退化为直接删文件，语义都合理。

**练习 3**：Downloader 的 `Close` 为什么把 `tempFiles.Close` 放在关闭序列最后，甚至晚于 storage？
**答案**：顺序是「先停产生者，再清理产物」。前面要先暂停全部任务、关闭各协议管理器，确保没有协程还会创建新临时文件；最后 Scope.Close 兜底删除所有未销账路径。若放在前面，之后产生的临时文件将无人清理（好在该场景会被 Track 的 closed 分支当场删除——双重保险）。

### 4.4 共享基础设施：logger 与 httpclient

#### 4.4.1 概念说明

Controller 的 `Logger` 与 `GetProxy` 两个字段分别对接两个基础设施包：

- **internal/logger**：把 zerolog 包一层，决定「写文件还是 stdout」。Downloader 在构造时创建两个独立日志器——`core.log`（核心/协议）与 `extension.log`（JS 扩展），前者经 Controller 分发给 Fetcher。
- **internal/httpclient**：HTTP 客户端工厂。`GetProxy` 的返回值（一个 `func(*http.Request) (*url.URL, error)`）正是它的 `TransportOptions.Proxy`；它还实现了三种「浏览器指纹伪装」模式，用于对付基于 TLS/JA3 指纹拦截下载器的站点。

#### 4.4.2 核心流程

Logger 链路：

```text
NewDownloader(ProductionMode, StorageDir)
  ├─ d.Logger          = NewLogger(ProductionMode, StorageDir/logs/core.log)
  ├─ d.ExtensionLogger = NewLogger(ProductionMode, StorageDir/logs/extension.log)
  └─ ProductionMode ? logPanic(logs目录) : 无
setupFetcher: ctl.Logger = &d.Logger.Logger   // 只取内嵌的 zerolog.Logger
协议侧: logger() 判 nil → warn/error 静默或输出
```

httpclient 链路：

```text
协议构建 HTTP 客户端（以 http/helper.go buildClientWithTimeout 为例）
  TransportOptions.Proxy = f.ctl.GetProxy(f.meta.Req.Proxy)
  Impersonation{ Mode: auto, Session: 任务级会话 }
       ↓
httpclient.NewClient
  ├─ mode native/"":  标准 http.Transport（ForceAttemptHTTP2）
  ├─ mode fixed:      指定浏览器指纹的 req v3 传输层
  └─ mode auto:       adaptiveTransport——先 native，
                      遇 403 / cf-mitigated:challenge 时
                      用浏览器指纹重试并缓存选择结果
```

#### 4.4.3 源码精读

[internal/logger/logger.go:L11-L18](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/logger/logger.go#L11-L18) 定义 `Logger`：内嵌 `zerolog.Logger` 外加持有的日志文件句柄；`CLose()` 关闭文件（方法名拼写与标准 Close 不同，保留历史原样）。

[internal/logger/logger.go:L21-L52](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/logger/logger.go#L21-L52) 是 `NewLogger(logFile bool, logPath)`：logFile 为真时建目录并以追加模式打开文件，否则写 stdout；输出经 `zerolog.ConsoleWriter`（无色彩、带时间戳）格式化。注意 Downloader 传入的第一个参数是 `cfg.ProductionMode`——**开发模式日志走 stdout，生产模式才落文件**。

[pkg/download/downloader.go:L163-L168](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L163-L168) 是两个日志器的创建现场，并设置 `zerolog.ErrorStackMarshaler`（错误自动带堆栈）与生产模式的 `logPanic`（panic 也记入日志目录）。

协议侧的判空防御以 FTP 为范本：

[internal/protocol/ftp/fetcher.go:L122-L133](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ftp/fetcher.go#L122-L133) 的 `logger()` 直接返回 `f.ctl.Logger`（可能为 nil），`warn()` 先判 nil 再输出——这就是 Controller 注释「It may be nil, in which case fetchers log nothing」的含义。测试注入见 [internal/protocol/ftp/fetcher_test.go:L734-L740](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/ftp/fetcher_test.go#L734-L740)：`zerolog.New(&logs)` 写入内存 buffer，塞给 `ctl.Logger` 后即可断言日志内容。

[pkg/base/model.go:L75-L93](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L75-L93) 是任务级代理 `RequestProxy` 与 `ToHandler`：只有 `custom` 模式且 scheme/host 齐全才返回 `http.ProxyURL(...)`，否则 nil（nil 对 http.Transport 意味着直连）。三态常量定义在 [pkg/base/model.go:L68-L72](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L68-L72)。全局代理则是 [pkg/base/model.go:L221](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L221) 的 `DownloaderStoreConfig.Proxy`。

[internal/protocol/http/helper.go:L268-L293](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/helper.go#L268-L293) 展示 `GetProxy` 与 httpclient 的衔接点：`TransportOptions.Proxy = f.ctl.GetProxy(f.meta.Req.Proxy)`，同一处还传入拨号超时、`InsecureSkipVerify`（任务的 `SkipVerifyCert`）与 `ImpersonationAuto` 模式。

[internal/httpclient/client.go:L99-L135](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/httpclient/client.go#L99-L135) 是 `NewClient` 工厂：按伪装模式分派 native / 固定浏览器 / 自适应三种传输层，组装出标准 `*http.Client`。

[internal/httpclient/client.go:L203-L245](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/httpclient/client.go#L203-L245) 是自适应传输层的核心 `RoundTrip`：先走 native；判定需要回退（见 [L289-L298](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/httpclient/client.go#L289-L298)：出错、403、或 `Cf-Mitigated: challenge` 响应头）且请求幂等可重放时，按 User-Agent 猜浏览器、用对应指纹传输层重试，并把成功/失败结论缓存进 `ImpersonationSession`——同一站点后续请求直接复用，避免每次都「先撞墙再绕路」。这也解释了 u3-l1 中看到 HTTP Fetcher 持有任务级 `impersonationSession` 的原因。

#### 4.4.4 代码实践

**实践：对比「开发模式 stdout 日志」与「生产模式文件日志」（运行型）**

1. **实践目标**：直观理解 `NewLogger` 的第一个参数如何由 `ProductionMode` 决定日志去向，以及协议日志真的经由 Controller 流出。
2. **操作步骤**：
   - 仿照 u1-l4 的 Boot 示例写一个小程序（或直接改 `_examples/basic` 的副本，**示例代码**）：用 `download.NewDownloader(&download.DownloaderConfig{ProductionMode: false, ...})` 显式构造（Boot 链式 API 也接受 production 开关），下载一个本地文件。
   - 观察终端：应有 zerolog 格式的时间戳日志行。
   - 把 `ProductionMode` 改为 true、`StorageDir` 指向空目录，重跑；随后查看 `<StorageDir>/logs/core.log` 与 `extension.log` 是否生成。
3. **需要观察的现象**：false 时日志在 stdout；true 时 stdout 安静、logs 目录出现两个文件。
4. **预期结果**：与 `NewLogger` 的 if/else 分支一一对应；`extension.log` 由 JS 扩展引擎使用（u9 会展开），本实验里应为空文件。
5. 依赖本地网络与目录权限，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`ctl.Logger` 传的是 `*zerolog.Logger`（指针），为什么不是 `logger.Logger`（包装类型）？
**答案**：协议侧只需要「打日志」能力，不需要（也不应该）关闭日志文件——那是 Downloader 的生命周期职责。传内嵌类型的指针既缩小了能力面，又避免拷贝 zerolog.Logger 结构体。

**练习 2**：`GetProxy` 闭包里为什么要单独处理 Blob URL（直接返回 nil 走直连）？
**答案**：Blob 资源（u9-l4 详述）是扩展在进程内存中产出的，经本机回环地址暴露给 HTTP fetcher。给回环地址套上外部代理不仅毫无意义，还会把内网/本机地址泄露给代理服务器甚至直接失败，所以注册表中的确切 URL 一律旁路代理。

**练习 3**：`adaptiveTransport` 为什么把回退结论缓存到 `ImpersonationSession`，而不是每次都试？
**答案**：回退本身有成本（一次注定失败的 native 请求 + 请求体重放）。按「去掉 fragment 的 URL」为键缓存站点级结论后，同一站点只有第一个请求付学费；`disableFallback` 还能记住「浏览器指纹也救不了」的站点，避免反复撞墙。

## 5. 综合实践

**任务：为一次 HTTP 下载建立完整的「文件操作审计」**

把本讲三个动手点串成一条链：

1. 复用 4.2.4 的 `loggingFileController`（记录目标文件创建与预分配大小）。
2. 再写一个装饰型的 Scope 思路：在 `Track`/`Remove` 无法替换的情况下（Scope 是具体类型不是接口），退而求其次——在你的测试 Controller 上保留 `TempDir` 指向独立目录，下载后检查该目录是否归零，间接验证 Track/Remove 配对。
3. 用 4.4.3 的测试注入手法（`zerolog.New(&logs)`）给 `ctl.Logger` 接一个内存日志器。
4. 跑一次本地 httptest 下载，收集三路证据：`[Touch]` 输出行、临时目录前后对比、日志 buffer 中的协议日志。
5. 产出一张时间线表：`os.Stat 失败 → Touch(预分配) → prefetch 临时文件创建+Track → 数据拷贝 → Remove 销账 → 任务完成`。

完成后你应当能回答：**HTTP 一次成功下载到底触碰了几个文件、谁创建的、谁清理的**——这正是下一讲 u3-l4（自定义协议 Fetcher）里你要亲手实现的全部文件职责。

## 6. 本讲小结

- `Controller` 是 Downloader 与 Fetcher 之间的中介服务包：临时文件作用域、临时目录、配置读取、代理解析、日志器、文件创建能力，全部在 `setupFetcher` 装配并经 `fetcher.Setup(ctl)` 注入。
- `DefaultFileController.Touch` 三步走：MkdirAll 父目录 → os.Create → `os.Truncate` 预分配长度；仅在新任务/文件缺失时调用，续传路径用 `OpenFile(O_RDWR)` 绕开它。
- `tempfiles.Scope` 用登记—销账—统一关闭管理临时文件：`Track` 在关闭后到达会当场删除防泄漏，`Close` 在 Downloader 关闭序列最后一站兜底清理，且只删自己登记过的路径。
- `DownloaderConfig.Controller` 字段目前只做 `Init()` 兜底、不参与 `setupFetcher` 装配——自定义 FileController 的正确注入点是测试同款的 `controller.NewController()` + `fetcher.Setup(ctl)`。
- 协议日志经 `ctl.Logger`（可能为 nil，协议须判空）；`GetProxy` 闭包实现 Blob 旁路 + 任务级三态代理决策，其产物直接成为 `httpclient` 传输层的 Proxy。
- `internal/httpclient` 的自适应伪装传输层在 403/Cloudflare challenge 时按站点缓存浏览器指纹回退结论，与 `GetProxy` 一样是「Downloader 决策、协议消费」的共享基础设施。

## 7. 下一步学习建议

- 下一讲 **u3-l4 实战：实现一个自定义协议 Fetcher**：你将亲手实现 `Setup(ctl)` 之后的一切，本讲的 Controller 消费方式（GetConfig 拿默认配置、Touch 建文件、TempFiles 管临时文件、DoneCh 汇报完成）正是你要模仿的骨架。
- 精读 HTTP 预取与 Touch 的交互时，提前翻阅 [internal/protocol/http/fetcher.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher.go) 中 `asyncPrefetch`/`stopPrefetchAndCopyData`（u5-l1 的主题），验证本讲的临时文件链路。
- 想理解 `Close` 全序列（本讲只看了末尾两站）就去读 [pkg/download/downloader.go:L1224-L1258](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1224-L1258)，u11-l2 会系统梳理锁与优雅关闭。
- `ImpersonationSession` 的站点级缓存是任务级状态，它与 `bucketProtocolState`（u6-l4）同属「跨任务共享的协议状态」话题，可对照阅读。
