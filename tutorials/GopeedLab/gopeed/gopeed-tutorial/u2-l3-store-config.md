# u2-l3 DownloaderStoreConfig：全局配置体系

## 1. 本讲目标

上一讲（u2-l2）我们看清了单个任务 `Task` 的结构与状态机。这一讲把镜头拉远，看**全局配置**——那个决定「所有任务共享什么行为」的东西：

- 说出 `DownloaderStoreConfig` 每个字段组（下载目录、MaxRunning、ProtocolConfig、代理、webhook、脚本、自动种子、解压、分类、API 服务……）的作用与默认值；
- 理解 `Init`（补齐默认值）与 `Merge`（部分更新）两个函数为什么长成那样——核心是 Go 结构体的「零值歧义」问题；
- 掌握协议配置归一化 `normalizeProtocol`：一份 `map[string]any` 如何变成各协议自己的配置形状，以及 `state` 段为何被特殊保护；
- 通过 `MigrateLegacyExtraCategories` 学会编写一次配置迁移：搬数据、兜底、清理旧键、报告是否落盘。

## 2. 前置知识

阅读本讲需要以下概念，不熟悉的读者先看这里的通俗解释：

- **配置的序列化**：配置要写进磁盘（bbolt 键值库，见 u1-l1），所以结构体字段都带 json tag（如 `json:"maxRunning"`），Go 结构体 ↔ JSON 双向转换是所有配置操作的基础。
- **零值歧义**：Go 中 `int` 零值是 `0`、指针零值是 `nil`。如果用户「想把 MaxRunning 改成某个值」和「根本没想改这个字段」都用同一个结构体表达，就必须约定：**零值 = 没改**。这是本讲反复出现的主题。
- **指针字段表达「未设置」**：`*WebhookConfig` 为 `nil` 表示「这次没提供 webhook 配置」；非 `nil` 才表示「提供了（哪怕里面全是 false）」。`bool` 本身无法区分「没设」和「设为 false」，所以 `AutoCategorize` 用了 `*bool`。
- **部分更新（PATCH 语义）**：调用方只想改一两个字段时，后端拿「新配置 + 旧配置」逐字段比对，新配置里没提供的字段沿用旧值。`Merge` 就是这个动作。
- **深拷贝**：配置在内存中会被并发读写，任何对外返回、对内修改前都要先复制一份。本项目用 `util.DeepClone`（基于 JSON 往返）完成。
- **配置迁移（migration）**：软件升级后配置结构变了（字段搬家、改名），需要在启动时把旧格式搬成新格式，且必须「失败安全」——数据不合法就原样保留，绝不能崩溃或丢配置。

承接前两讲：u2-l1 讲了 `Options` 是**单个任务**的意图表达；本讲的 `DownloaderStoreConfig` 是**全局默认值**——`Options` 没覆盖的地方，都会回落到这里（例如 HTTP 连接数的三级回退：任务级 → 全局 ProtocolConfig → 协议默认值）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲使用方式 |
|---|---|---|
| [pkg/base/model.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go) | 领域模型，`DownloaderStoreConfig` 及全部子配置结构、`Init`/`Merge`/`MigrateLegacyExtraCategories` 都在这里 | 主教材，逐段精读 |
| [pkg/download/protocol_config.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_config.go) | 协议配置的读取、归一化、state 保护、启动初始化 | 主教材，逐段精读 |
| [pkg/download/downloader.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go) | Downloader 主体：`Setup` 加载配置、`GetConfig`/`PutConfig` 读写入口 | 精读配置相关的几个函数 |
| [pkg/download/model.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go) | `DownloaderConfig`（进程级配置，不入库），内嵌 `DownloaderStoreConfig` | 区分两层配置 |
| [cmd/server.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/server.go) | 服务端启动：首次加载时合并启动参数、决定默认下载目录 | `Merge` 的真实调用方 |
| [pkg/base/model_test.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model_test.go) | `Init`/`Merge`/迁移的官方单元测试 | 实践与练习的依据 |
| [pkg/download/backend_services_test.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/backend_services_test.go) | 协议配置初始化的官方测试 | 4.4 的实践参考 |

## 4. 核心概念与源码讲解

### 4.1 DownloaderStoreConfig 字段组：两层配置的边界

#### 4.1.1 概念说明

Gopeed 的配置分两层，初学者最容易混淆：

- **`DownloaderConfig`（进程级，不落盘）**：`Storage` 用哪个、`RefreshInterval` 多少、注册了哪些 `FetchManagers`、`WhiteDownloadDirs` 白名单……这些由启动代码（`Boot()`、`cmd/web`）决定，进程重启就重新组装，**不属于用户设置**。
- **`DownloaderStoreConfig`（用户级，落盘）**：用户在设置界面改的东西——下载目录、最大并行数、代理、webhook……它必须能「恢复整个 Downloader 的行为」，所以名字里带 Store。

后者以前者**内嵌指针**的方式挂接：

[pkg/download/model.go:211-225](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L211-L225) 中 `DownloaderConfig` 的最后一个字段是 `*base.DownloaderStoreConfig`（匿名字段）。所以代码里既有 `d.cfg.RefreshInterval`（进程级）也有 `d.cfg.DownloadDir`（用户级，穿透内嵌直达）。`DownloaderConfig.Init()` 只补齐进程级默认值（Controller、五个协议管理器、350ms 刷新、内存存储），完全不碰用户配置。

#### 4.1.2 核心流程

配置的生命周期是一个闭环：

```text
启动 Setup()
  ├─ storage.Get(bucketConfig, "config")  → 读出旧配置（或判定 FirstLoad）
  ├─ MigrateLegacyExtraCategories()       → 旧格式迁移（见 4.5）
  ├─ initProtocolConfigs()                → Init 补默认值 + 协议配置归一化（见 4.2/4.4）
  └─ 写回 bucketConfig
运行期
  ├─ GetConfig()   → 深拷贝一份给调用方（REST/Frontend/CLI）
  ├─ PutConfig(v)  → 完整写入：校验→归一化→落盘→热更新通知（见 4.4）
  └─ persistDefaultPath() → 仅改 DownloadDir 一个字段的内部写入
关机 Close()
  └─ 配置早已在每次写入时落盘，无需额外保存
```

存储位置：bbolt 的 `config` 桶、键 `"config"`，见常量 [pkg/download/downloader.go:49](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L49)（`bucketConfig = "config"`）。也就是说**整个全局配置是「一桶一键」的单文档存储**，不是按字段拆分的键值对。

#### 4.1.3 源码精读

主结构体（省略部分注释）：

[pkg/base/model.go:212-231](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L212-L231) 定义了全部字段：

```go
// DownloaderStoreConfig is the config that can restore the downloader.
type DownloaderStoreConfig struct {
	FirstLoad bool `json:"-"` // 首次初始化标志，不序列化

	DownloadDir                string                 `json:"downloadDir"`    // 默认下载目录
	MaxRunning                 int                    `json:"maxRunning"`     // 最大同时运行任务数
	ProtocolConfig             map[string]any         `json:"protocolConfig"` // 各协议专属配置
	GithubMirror               *GithubMirrorConfig    `json:"githubMirror"`
	Extra                      map[string]any         `json:"extra"`
	Proxy                      *DownloaderProxyConfig `json:"proxy"`
	Webhook                    *WebhookConfig         `json:"webhook"`
	Script                     *ScriptConfig          `json:"script"`
	AutoTorrent                *AutoTorrentConfig     `json:"autoTorrent"`
	Archive                    *ArchiveConfig         `json:"archive"`
	Categories                 []*DownloadCategory    `json:"categories"`
	AutoCategorize             *bool                  `json:"autoCategorize,omitempty"`
	API                        *APIServerConfig       `json:"api"`
	AutoStartTasks             bool                   `json:"autoStartTasks"`
	AutoDeleteMissingFileTasks bool                   `json:"autoDeleteMissingFileTasks"`
}
```

各字段组速查（默认值来自 4.2 的 `Init`）：

| 字段组 | 作用 | 默认值 / 说明 |
|---|---|---|
| `DownloadDir` | 所有任务的默认保存目录 | 空字符串；首次启动由 [cmd/server.go:39-54](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/server.go#L39-L54) 决定：Docker 内为 `${exe目录}/Downloads`，否则 `${用户主目录}/Downloads` |
| `MaxRunning` | 同时 running 的任务上限 | 5；超出的任务停在 wait 队列（见 u2-l2） |
| `ProtocolConfig` | 按协议名索引的配置表，如 `{"http": {...}, "bt": {...}}` | 空 map；内容见 4.4 |
| `Proxy` | 全局代理（系统代理 / 自定义 scheme+host+账号） | 空结构体（不启用） |
| `Webhook` / `Script` | 下载完成后回调 HTTP 地址 / 执行外部脚本 | 均不启用 |
| `AutoTorrent` | 下载 `.torrent` 文件后自动创建 BT 任务、可选删除种子文件 | 均不启用 |
| `Archive` | 下载完成后自动解压归档、可选删原包 | 均不启用 |
| `Categories` + `AutoCategorize` | 按扩展名把文件路由到分类目录；开关默认关（`*bool` 为 nil） | 首次 Init 种入 Music/Video/Document/Program 四个内置分类 |
| `API` | 内嵌 REST 服务的开关、地址、token、MCP 开关 | nil（不启动） |
| `AutoStartTasks` / `AutoDeleteMissingFileTasks` | 启动时自动续跑未完成任务 / 自动删除文件已丢失的任务 | 均 false |
| `Extra` | 前端自由使用的杂项键值袋 | nil；历史上曾放分类列表（见 4.5） |

三个读写入口：

- [pkg/download/downloader.go:1384-1388](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1384-L1388) `GetConfig()`：读锁 + `util.DeepClone` 深拷贝后返回。**绝不返回内部指针**，否则调用方一改就穿透到运行中的 Downloader。
- [pkg/download/downloader.go:1390-1397](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1390-L1397) `IsFirstLoad()`：注意注释里的坑——`FirstLoad` 带 `json:"-"` 不参与序列化，而 `DeepClone` 走 JSON，所以 `GetConfig()` 返回的副本里 `FirstLoad` 永远是 false；想知道是否首次加载必须单独调这个方法。
- [pkg/download/downloader.go:1399-1459](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1399-L1459) `PutConfig(v)`：唯一的对外完整写入口，细节在 4.4.3 展开。

加载侧：[pkg/download/downloader.go:182-194](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L182-L194) 从桶里取配置，取不到就构造一个只带 `FirstLoad: true` 的空配置——**空配置 + Init = 全默认值**，这就是「第一次启动」的判定方式。

#### 4.1.4 代码实践

**实践：把默认配置打印出来**

1. 实践目标：直观看到「一个刚出生的 Downloader 配置长什么样」。
2. 操作步骤：在仓库根目录新建练习目录 `configlab/`（与 `cmd/` 平级，纯练习产物，不要提交），创建 `main.go`：

   ```go
   // 示例代码：configlab/main.go —— 打印首次初始化的全局配置
   package main

   import (
       "encoding/json"
       "fmt"

       "github.com/GopeedLab/gopeed/pkg/base"
       "github.com/GopeedLab/gopeed/pkg/download"
   )

   func main() {
       d := download.NewDownloader(&download.DownloaderConfig{
           StorageDir: "/tmp/gopeed-configlab",
           TempDir:    "/tmp/gopeed-configlab-tmp",
       })
       defer d.Close()
       if err := d.Setup(); err != nil {
           panic(err)
       }
       cfg, err := d.GetConfig()
       if err != nil {
           panic(err)
       }
       raw, _ := json.MarshalIndent(cfg, "", "  ")
       fmt.Println(string(raw))
       fmt.Println("FirstLoad =", d.IsFirstLoad())
   }
   ```

   运行：`go run ./configlab`。
3. 需要观察的现象：输出 JSON 里每个字段组的形态；`FirstLoad` 单独打印的值。
4. 预期结果：包含 `maxRunning: 5`、`protocolConfig` 里 http/bt 等五个协议的完整默认配置（http 里 `connections: 16`、bt 里 `seedRatio: 1` 等）、`githubMirror` 的四个内置镜像；`DownloadDir` 为空（它由 cmd/server.go 在首次加载时补，库形态不会补）；`FirstLoad = true`。具体输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`DownloaderConfig.RefreshInterval` 和 `DownloaderStoreConfig.MaxRunning` 都是数字，为什么一个不用序列化、一个必须序列化？

答案：`RefreshInterval` 是**进程级**参数——由启动代码（`Boot()` 链或服务端入口）每次启动时给定，不是用户设置，改了也不该影响下次启动；`MaxRunning` 是**用户级**设置，必须在重启后保持，所以放进会落盘的 `DownloaderStoreConfig`。

**练习 2**：为什么 `GetConfig()` 要深拷贝，而 `IsFirstLoad()` 只返回一个 bool？

答案：配置结构体含指针与 map，若直接返回内部引用，外部修改会绕过 `configLock` 并发保护、直接污染运行中的 Downloader 状态；bool 是值类型，拷贝无意义，`IsFirstLoad` 在读锁内取值即安全。

**练习 3**：`Extra map[string]any` 这样的「杂项袋」字段有什么隐患？

答案：类型不安全（调用方要自己断言类型）、结构演进不可控（键名没有契约）、还给「懒得加字段」的代码开后门。本项目正在偿还这笔债：分类配置曾经塞在 `Extra.downloadCategories`，后来提升为顶层 `Categories` 字段，于是需要一次迁移（4.5），而 `GithubMirror` 的迁移则被明确拒绝（`initProtocolConfigs` 的注释：本版本不从旧布局导入设置）。

### 4.2 Init：默认值的「补齐」而不是「重置」

#### 4.2.1 概念说明

`Init()` 的职责只有一个：**字段是空的就填默认值，非空的绝不动**。这叫幂等补齐（idempotent seeding）。它与「重置为出厂设置」完全不同——后者是 `&DownloaderStoreConfig{}` 加 `Init()`。

为什么要幂等？因为 `Init` 会在多个时机被反复调用：`initProtocolConfigs` 启动时调一次；[cmd/server.go:59-60](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/server.go#L59-L60) 首次加载确定目录后又调一次（注释明确说「Init is idempotent and leaves an existing category list untouched」）。若 Init 会覆盖已有值，第二次调用就会吃掉用户配置。

#### 4.2.2 核心流程

```text
Init()
  ├─ MaxRunning == 0        → 5
  ├─ ProtocolConfig == nil  → 空 map
  ├─ GithubMirror == nil    → DefaultGithubMirrorConfig()（4 个内置镜像）
  ├─ Proxy/Webhook/Script   → 各自空结构体（= 全部关闭）
  ├─ AutoTorrent == nil     → {Enable:false, DeleteAfterDownload:false}
  ├─ Archive == nil         → {AutoExtract:false, DeleteAfterExtract:false}
  └─ Categories 为空 且 DownloadDir != ""
       → 种入 4 个内置分类（Music/Video/Document/Program，
         路径 = DownloadDir/分类名，扩展名取自 builtinCategoryExtensions）
```

注意最后一个条件：**DownloadDir 为空就不种分类**。因为分类目录要挂在下载目录下面，目录还没定，种了也是错的路径。这也是为什么 `cmd/server.go` 必须先定目录、再补调一次 `Init`。

#### 4.2.3 源码精读

[pkg/base/model.go:233-275](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L233-L275) 是 `Init` 主体，每个字段一个 `if nil/0` 判断，例如：

```go
if cfg.MaxRunning == 0 {
    cfg.MaxRunning = 5
}
...
// Seed the built-in categories for deployments that never run the flutter
// startup flow, e.g. headless api servers.
if len(cfg.Categories) == 0 && cfg.DownloadDir != "" {
    cfg.Categories = []*DownloadCategory{
        builtinSeedCategory("categoryMusic", "Music", cfg.DownloadDir),
        ...
    }
}
```

[pkg/base/model.go:277-294](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L277-L294) 定义分类扩展名映射表 `builtinCategoryExtensions` 和 `builtinSeedCategory`。注释很关键：**这张表要和 Flutter 侧的 `kDefaultCategoryExtensions` 保持同步**——Go 与 Dart 两份默认值，靠人工约定对齐，这是跨语言双端项目的常见债。

首次加载的完整编排：[cmd/server.go:30-61](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/server.go#L30-L61) 三步走：① 若启动参数带了 `DownloadConfig`，用 `Merge` 叠到存储配置上（4.3 详解）；② `DownloadDir` 仍为空则按 Docker/桌面两种环境补默认目录；③ 再调一次 `Init()` 种分类并 `PutConfig` 落盘。

另一个值得看的子配置默认值：[pkg/base/model.go:397-405](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L397-L405) `APIServerConfig.Init()` 把网络补成 `tcp`、地址补成 `127.0.0.1:9999`——注意它默认只监听本机回环，这是安全默认值的典型做法。

#### 4.2.4 代码实践

**实践：观察「已有值不被 Init 覆盖」**

1. 实践目标：验证 Init 的幂等性。
2. 操作步骤：在 `configlab/main.go` 里追加（示例代码）：

   ```go
   cfg.MaxRunning = 3
   cfg.Init()
   fmt.Println("after 1st Init, MaxRunning =", cfg.MaxRunning) // 3，不被覆盖
   cfg.MaxRunning = 0
   cfg.Init()
   fmt.Println("after 2nd Init, MaxRunning =", cfg.MaxRunning) // 5，补默认
   ```

3. 需要观察的现象：两次 `Init` 的输出差异。
4. 预期结果：`3` 与 `5`（依据 [pkg/base/model_test.go:139-162](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model_test.go#L139-L162) 的官方用例「Init Archive」：预先设置的字段原样保留）。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`AutoCategorize` 为什么是 `*bool` 而 `AutoStartTasks` 是 `bool`？

答案：`AutoCategorize` 需要区分三态——「从未设置（旧配置文件里没这字段）」「显式开」「显式关」。`bool` 只有两种值，无法表达「没设置」，所以用 `*bool`：nil 即没设置，且 [pkg/base/model.go:339-341](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L339-L341) `AutoCategorizeEnabled()` 把 nil 解释为「关」，保证旧配置升级后行为不变。`AutoStartTasks` 本身默认就是 false，没有三态需求，普通 `bool` 足够。

**练习 2**：如果用户把 `DownloadDir` 从 `/a` 改成 `/b`，已存在的四个内置分类路径会自动跟着变吗？

答案：不会。`Init` 只在 `len(Categories) == 0` 时种分类，已有分类原样保留；分类的 `Path` 是创建时写死的绝对路径。改全局目录后旧分类仍指向旧目录，需要用户在设置里逐个改（或删掉分类让其重新种子化）。

### 4.3 Merge：为什么需要「部分更新」

#### 4.3.1 概念说明

设想服务端首次启动：命令行参数里只带了下载目录（`-d /data`），其余什么都没说。如果直接 `PutConfig(参数配置)`，那么参数配置里所有 nil/0 字段都会把存储里的用户设置冲掉——webhook 没了、代理没了。**部分更新**要求：新配置中「没提供」的字段沿用旧值。

`Merge(beforeCfg)` 就是这个动作：接收方（自己，`cfg`）字段为空时，从 `beforeCfg`（旧配置）借值填上。注意方向——**它是「新配置向旧配置借」，返回补齐后的新配置**，不修改旧配置。

#### 4.3.2 核心流程

```text
Merge(before)
  若 before == nil → 原样返回（无可借）
  对每个字段：
    DownloadDir   == ""    → 用 before 的
    MaxRunning    == 0     → 用 before 的
    ProtocolConfig == nil  → 用 before 的（map 级整体替换，不深合并！）
    Extra / GithubMirror / Proxy / Webhook / Script / AutoTorrent /
    Archive / Categories / AutoCategorize / API
                        == nil  → 用 before 的
  返回 cfg（链式，通常接 PutConfig）
```

两个容易踩的细节：

1. **粒度是「字段组」而非「字段内」**：`Webhook == nil` 才借旧的；只要你传了 `&WebhookConfig{Enable:true}`，那么 `URLs` 就以你传的为准（哪怕你本意只想改 Enable）。这是「整体替换、组间合并」的策略。
2. **`MaxRunning == 0` 被当作「没设置」**，意味着用户无法把并行数设为 0（也没有「无限」档）——这是零值歧义的代价，官方测试 [pkg/base/model_test.go:560-618](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model_test.go#L560-L618) 里的「Merge AutoTorrent Override/No Override」两组用例正好演示了这两个方向。

#### 4.3.3 源码精读

[pkg/base/model.go:343-387](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L343-L387) 全文就是一串同构的 if：

```go
func (cfg *DownloaderStoreConfig) Merge(beforeCfg *DownloaderStoreConfig) *DownloaderStoreConfig {
	if beforeCfg == nil {
		return cfg
	}
	if cfg.DownloadDir == "" {
		cfg.DownloadDir = beforeCfg.DownloadDir
	}
	if cfg.MaxRunning == 0 {
		cfg.MaxRunning = beforeCfg.MaxRunning
	}
	if cfg.ProtocolConfig == nil {
		cfg.ProtocolConfig = beforeCfg.ProtocolConfig
	}
	... // Extra/GithubMirror/Proxy/Webhook/Script/AutoTorrent/
	... // Archive/Categories/AutoCategorize/API 同构
	return cfg
}
```

真实调用方：[cmd/server.go:32-36](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/server.go#L32-L36)。注意它只在 `IsFirstLoad()` 为真时才 Merge——**正常运行时不允许命令行参数悄悄改用户设置**，只有第一次（存储里还没有任何用户配置）才用启动参数打底。代码里还留着 `// TODO Use PatchConfig`，说明作者认为这条路径的语义以后应换成专门的「补丁配置」接口。

要分清 `Merge` 与 `PutConfig` 的分工：`Merge` 是 pkg/base 上的**纯函数**，只做字段搬运，不碰存储；`PutConfig` 是 Downloader 的**完整写路径**（校验、归一化、落盘、热通知），它的入参应当已经是「补齐后的完整配置」。二者串联才是一次安全的部分更新：`newCfg.Merge(old) → PutConfig(newCfg)`。

#### 4.3.4 代码实践

**实践：模拟一次部分更新**（本讲综合实践的前半部分，这里先做最小版）

1. 实践目标：亲眼看到「只提供一个字段，其余沿用旧值」。
2. 操作步骤（示例代码，可放进 `configlab/main_test.go`）：

   ```go
   // 示例代码：configlab/main_test.go
   package main

   import (
       "testing"

       "github.com/GopeedLab/gopeed/pkg/base"
   )

   func TestMergePartialUpdate(t *testing.T) {
       old := &base.DownloaderStoreConfig{
           DownloadDir: "/data/old",
           MaxRunning:  8,
           Webhook:     &base.WebhookConfig{Enable: true, URLs: []string{"http://localhost:9000/hook"}},
           Proxy:       &base.DownloaderProxyConfig{Scheme: "http", Host: "127.0.0.1:7890"},
       }
       // 用户这次只想改 MaxRunning 和 http 连接数
       next := &base.DownloaderStoreConfig{
           MaxRunning: 2,
           ProtocolConfig: map[string]any{
               "http": map[string]any{"connections": 32},
           },
       }
       merged := next.Merge(old)

       t.Logf("DownloadDir=%q MaxRunning=%d", merged.DownloadDir, merged.MaxRunning)
       t.Logf("Webhook=%+v", merged.Webhook)
       t.Logf("Proxy=%+v", merged.Proxy)
       t.Logf("ProtocolConfig=%v", merged.ProtocolConfig)
   }
   ```

   运行：`go test -v ./configlab -run TestMergePartialUpdate`。
3. 需要观察的现象：`next` 里没提供的字段来自哪里；`next` 里提供了的字段是否原样保留。
4. 预期结果：`DownloadDir=/data/old`（借旧）、`MaxRunning=2`（新值生效）、`Webhook` 与 `Proxy` 为旧对象（借旧）、`ProtocolConfig` 为新 map。注意 `merged.Webhook` 与 `old.Webhook` 是**同一个指针**——Merge 只搬运引用不做拷贝，这就是后续 `PutConfig` 要再 DeepClone 的原因之一。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：调用方想「只关闭 webhook，但保留原来的 URL 列表」，用 `Merge` 能做到吗？

答案：不能。`Merge` 的粒度是字段组：只要传了非 nil 的 `Webhook`，整个 `WebhookConfig` 都以新的为准，`URLs` 会丢。正确做法是先 `GetConfig()` 拿到完整配置，改其中 `Webhook.Enable`，再整体 `PutConfig`——这正是 REST 的 updateConfig 接口实际采用的模式。

**练习 2**：`Merge` 里为什么没有对 `FirstLoad`、`AutoStartTasks`、`AutoDeleteMissingFileTasks` 做 if 判断？

答案：`FirstLoad` 是运行期标志（`json:"-"`），不属于可合并的持久化内容；后两个是普通 `bool`，零值 false 本身就是合法目标值，若按「零值=没设置」规则合并，用户就永远无法把它们改回 false——对 bool 字段，Merge 干脆放弃三态，改用 `*bool`（如 `AutoCategorize`）才有资格参与合并。

### 4.4 协议配置归一化 normalizeProtocol

#### 4.4.1 概念说明

`ProtocolConfig` 的类型是 `map[string]any`——键是协议名（`http`、`bt`、`ftp`、`hls`、`ed2k`），值是从 JSON 反序列化出来的**无类型嵌套 map**。但每个协议内部有自己的强类型配置结构（BT 的做种参数、HTTP 的 UserAgent）。于是在「存储的松散 JSON」和「协议的强类型结构」之间需要一个翻译官，这就是 `normalizeProtocol`：

- 以协议的 `DefaultConfig()` 为**模板**（而不是空白结构），把用户 JSON 叠加到模板上；
- 产出的配置**永远包含全部字段的默认值**，用户没写的字段自动补齐；
- 若协议实现了可选接口 `ConfigNormalizer`，再把结果交给协议做**校验与派生**（拒绝非法值、计算派生字段）。

为什么以默认值为模板如此重要？看 BT 的例子：[internal/protocol/bt/fetcher.go:947-963](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/bt/fetcher.go#L947-L963) 的 `DefaultConfig()` 返回了 tracker 订阅源、做种参数、以及一段 `ConfigState[state]`（tracker 抓取结果等后端自有状态）。用户 JSON 里通常只有两三个键（如 `{"listenPort": 6881}`），直接反序列化到空结构会得到一堆零值（`SeedRatio: 0`、`SeedTime: 0`），语义完全跑偏；先铺默认值再叠加，才能「用户写什么改什么，其余保持协议推荐值」。

#### 4.4.2 核心流程

启动时的初始化（[pkg/download/protocol_config.go:152-172](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_config.go#L152-L172)）：

```text
initProtocolConfigs()
  ├─ cfg.Init()                       # 补全局默认值（4.2）
  ├─ cfg.GithubMirror.Validate()      # 镜像列表合法性（数量≤32、URL 形态）
  └─ for 每个已注册 FetcherManager fm:
       value = cfg.ProtocolConfig[fm.Name()]  # 可能缺 → 空 map
       normalized = normalizeProtocol(fm, value)
       cfg.ProtocolConfig[fm.Name()] = normalized
  └─ storage.Put(bucketConfig, "config", cfg)  # 写回
```

用户更新配置时（`PutConfig`，[pkg/download/downloader.go:1417-1446](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1417-L1446)）对每个协议做四件事：

```text
1. 协议名必须已注册（protocolManager(name) 查得到），否则拒绝
2. 缺失的协议配置 → 用内存里的现值补上（用户没提 = 不动）
3. preserveProtocolState：剥掉用户提交里的 "state" 段，回填后端旧 state
4. normalizeProtocol 归一化 + 校验
之后：与旧值 JSON 对比，变化者记入 changedProtocols → 落盘 → protocolJobs.changed(name)
     （最后一步是热更新：BT 的 tracker 定时任务等立即感知新配置，无需重启）
```

消费端：协议 Fetcher 并不直接读 `ProtocolConfig`，而是通过 Controller 注入的回调。[pkg/download/downloader.go:455-462](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L455-L462) `setupFetcher` 把 `ctl.GetConfig = func(v any) { d.getProtocolConfig(fm.Name(), v) }` 塞进控制器；[pkg/download/downloader.go:1501-1510](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1501-L1510) `getProtocolConfig` 每次 `GetConfig()`（拿到最新深拷贝）再把该协议的 map 灌进协议给的容器 `v`。**协议每次读配置都走全局最新值**，这就是配置能热生效的原因。

#### 4.4.3 源码精读

归一化本体 [pkg/download/protocol_config.go:36-72](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_config.go#L36-L72)：

```go
func normalizeProtocol(fm fetcher.FetcherManager, v any) (any, error) {
	raw, err := json.Marshal(v)          // ① 用户值 → JSON
	target := fm.DefaultConfig()          // ② 拿协议默认模板
	if target == nil {
		return nil, errors.New("protocol does not expose configuration")
	}
	valueDefault := reflect.TypeOf(target).Kind() != reflect.Pointer
	if valueDefault {                     // ③ 模板是值类型时先转成指针，
		pointer := reflect.New(reflect.TypeOf(target)) //   保证 unmarshal 落在可寻址对象上
		...
	}
	if err = json.Unmarshal(raw, target); err != nil { // ④ 用户 JSON 叠加到默认模板
		return nil, err
	}
	if normalizer, ok := fm.(fetcher.ConfigNormalizer); ok { // ⑤ 可选协议校验
		raw, err = json.Marshal(target)
		...
		return normalizer.NormalizeConfig(raw)
	}
	...
	return target, nil
}
```

三处契约值得记住：

- `DefaultConfig() any` 是 `FetcherManager` 接口的正式成员：[internal/fetcher/fetcher.go:135-136](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L135-L136)——**每个协议都必须自报默认配置**，HTTP 报的是 `{UserAgent: DefaultUserAgent, Connections: 16}`（[internal/protocol/http/fetcher_manager.go:74-79](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_manager.go#L74-L79)），这也正是 u1-l4 讲过的「连接数三级回退」的第三级来源。
- `ConfigNormalizer` 是可选接口：[internal/fetcher/jobs.go:69-72](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/jobs.go#L69-L72)（注释：owns protocol validation and derived fields）。BT 实现了它：[internal/protocol/bt/trackers.go:67-90](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28ed/../blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/bt/trackers.go#L67-L90) 校验端口范围（0-65535）、做种参数非负、订阅源数量 ≤64 且必须是合法 http(s) URL、tracker 列表去重。
- `state` 段保护 [pkg/download/protocol_config.go:74-95](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_config.go#L74-L95) `preserveProtocolState`：把用户提交配置里所有名为 `state` 的键（连 `State` 大小写变体也按 `strings.EqualFold` 折叠掉）删除，再回填后端旧值。**配置里的 `state` 是后端私有领地**（如 BT 定时任务抓回来的 tracker 缓存），用户请求永远改不到它；后端自己写 state 的唯一通道是 [pkg/download/protocol_config.go:106-150](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_config.go#L106-L150) `updateProtocolState`（先整体克隆配置、只改 state、归一化、落盘——注释强调回调里的突变不能泄漏到事务之外）。

#### 4.4.4 代码实践

**实践：验证归一化会补全协议默认值**

1. 实践目标：确认「用户只写一个键，落盘配置却包含协议全量默认值」，且非法值会被拒绝。
2. 操作步骤：先跑通 4.1.4 的 `configlab`，然后参考官方测试 [pkg/download/backend_services_test.go:136-166](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/backend_services_test.go#L136-L166) 写一个练习：给 `ProtocolConfig` 只塞 `{"http": {"connections": 3}}`，`Setup()` 后重新 `GetConfig()` 并打印 `protocolConfig.http` 整段；再试着 `PutConfig` 一个 `{"bt": {"listenPort": 70000}}`（非法端口）观察返回的错误。
3. 需要观察的现象：http 配置是否被补成含 `userAgent`、`connections` 的完整对象；非法端口是否让 `PutConfig` 返回 "invalid BT configuration"。
4. 预期结果：是——归一化补全默认值，BT 的 `NormalizeConfig` 拒绝越界端口（依据上述源码与官方测试断言）。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `normalizeProtocol` 不直接 `json.Unmarshal(raw, &目标零值结构)`，而要先把 `DefaultConfig()` 当模板？

答案：零值结构反序列化后，用户没写的字段全是零值：BT 的 `SeedRatio` 会变 0（等于永远不做种）、`AutoUpdateTrackers` 会变 false。以默认值为模板叠加，才能保证「未提及 = 协议推荐值」，也让配置文件天然前向兼容——新增字段自动获得默认值，老配置无需改。

**练习 2**：`preserveProtocolState` 为什么要用 `strings.EqualFold` 检查键名？

答案：防御两种绕过：① Go 的 `encoding/json` 在匹配结构体字段时本身不区分大小写，提交 `"State"` 也能灌进结构体；② 客户端可能因大小写差异意外携带该键。统一按不区分大小写剥除，保证用户提交永远无法覆盖后端私有 state，state 只能由 `updateProtocolState` 这一内部通道写入。

**练习 3**：修改 BT 配置后不重启进程，tracker 定时任务会用新配置吗？

答案：会。`PutConfig` 在落盘后对比新旧协议配置的 JSON，把变化协议的名字交给 `d.protocolJobs.changed(name)`（[pkg/download/downloader.go:1441-1457](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1441-L1457)），唤醒对应协议的调度循环；细节在 u6-l4 展开。

### 4.5 MigrateLegacyExtraCategories：一次配置迁移的样板

#### 4.5.1 概念说明

v2.0.0-beta 时期，下载分类存放在 `Extra.downloadCategories`（杂项袋里）；正式版把它提升为顶层字段 `Categories`。老用户升级后配置不能丢，于是需要迁移：把旧列表搬到顶层、清理旧键、落盘。

一次合格的迁移要满足：**条件明确**（已有顶层分类就不动）、**兜底完备**（JSON 不合法就整体放弃、原样保留）、**数据修复**（内置分类在旧版本里没有扩展名列表，按 `NameKey` 找回默认表）、**可判定**（返回 bool 告诉调用方「配置变了，需要落盘」）。这个函数把四条全占了，是以后自己写迁移的最佳模板。

#### 4.5.2 核心流程

```text
MigrateLegacyExtraCategories() bool
  ├─ 已有 Categories 或 Extra 为 nil        → false（无事可做）
  ├─ Extra["downloadCategories"] 不存在      → false
  ├─ Marshal → Unmarshal 反序列化成 []*DownloadCategory
  │    失败 → false（格式坏，旧键原样保留，绝不半途写坏配置）
  ├─ 逐条修复：内置分类(NameKey 命中映射表)且扩展名为空
  │    → 补回默认扩展名列表；用户自建分类保持空列表
  ├─ 结果为空列表 → false
  ├─ cfg.Categories = 迁移结果；delete(cfg.Extra, "downloadCategories")
  └─ return true（调用方负责落盘）
```

调用时机在 `Setup` 里，且**必须在 `Init` 之前**：[pkg/download/downloader.go:195-207](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L195-L207)。注释讲了两层原因：其一，`Init` 只在 `Categories` 为空时种子化，若先跑 `Init`，旧分类还没搬进来就先被四个内置分类占了位，迁移会因「已有分类」而拒绝执行；其二，此时配置尚未经过 `Init`/校验，不能走 `PutConfig`（它会校验一份未初始化的配置），所以直接 `storage.Put` 落盘。

#### 4.5.3 源码精读

[pkg/base/model.go:296-334](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L296-L334)：

```go
func (cfg *DownloaderStoreConfig) MigrateLegacyExtraCategories() bool {
	if len(cfg.Categories) > 0 || cfg.Extra == nil {
		return false
	}
	raw, ok := cfg.Extra["downloadCategories"]
	if !ok {
		return false
	}
	data, err := json.Marshal(raw)
	...
	var legacy []*DownloadCategory
	if err := json.Unmarshal(data, &legacy); err != nil {
		return false // 坏数据：整体放弃，键保留
	}
	categories := make([]*DownloadCategory, 0, len(legacy))
	for _, category := range legacy {
		if category == nil {
			continue // 跳过空条目
		}
		if extensions, exist := builtinCategoryExtensions[category.NameKey]; exist && len(category.Extensions) == 0 {
			category.Extensions = append([]string(nil), extensions...) // 内置分类找回默认扩展名
		}
		categories = append(categories, category)
	}
	if len(categories) == 0 {
		return false
	}
	cfg.Categories = categories
	delete(cfg.Extra, "downloadCategories")
	return true
}
```

配套测试覆盖了五种边界：正常迁移（内置找回扩展名、自建保留空、无关 `Extra.themeMode` 不受影响、迁移后 `Init` 不会覆盖）、已有顶层分类时不迁移且旧键保留、缺失/空列表/坏格式一律 no-op、列表中的 nil 条目被跳过——见 [pkg/base/model_test.go:272-377](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model_test.go#L272-L377)。

对照反例：`GithubMirror` 同样存在旧布局（曾放 `Extra.githubMirror`），但 [pkg/download/protocol_config.go:152-154](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_config.go#L152-L154) 的注释明确声明「This release intentionally does not import settings from older config layouts」——官方测试 [pkg/download/backend_services_test.go:168-199](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/backend_services_test.go#L168-L199) 甚至断言旧 `Extra` 里的镜像/tracker 设置**不会**影响新配置。**迁移是逐案决策，不是自动全迁**：分类（用户手工建的数据）值得迁，镜像列表（服务端可再生的默认值）不值得。

#### 4.5.4 代码实践

**实践：复现一次迁移**（本讲综合实践的后半部分）

1. 实践目标：亲手驱动旧格式 → 新格式的搬移，并验证三个边界。
2. 操作步骤（示例代码，加入 `configlab/main_test.go`）：

   ```go
   // 示例代码：复现 legacy 分类迁移
   func TestMigrateLegacyCategories(t *testing.T) {
       cfg := &base.DownloaderStoreConfig{
           DownloadDir: "/downloads",
           Extra: map[string]any{
               "downloadCategories": []any{
                   map[string]any{"name": "", "path": "/old/Music", "isBuiltIn": true, "nameKey": "categoryMusic"},
                   map[string]any{"name": "Games", "path": "/old/Games"},
               },
               "themeMode": "dark",
           },
       }
       if !cfg.MigrateLegacyExtraCategories() {
           t.Fatal("期望发生迁移")
       }
       if len(cfg.Categories) != 2 {
           t.Fatalf("categories = %d, want 2", len(cfg.Categories))
       }
       if len(cfg.Categories[0].Extensions) == 0 {
           t.Fatal("内置分类应找回默认扩展名")
       }
       if _, ok := cfg.Extra["downloadCategories"]; ok {
           t.Fatal("旧键应被清理")
       }
       if cfg.Extra["themeMode"] != "dark" {
           t.Fatal("无关 Extra 键应保留")
       }
   }
   ```

   运行：`go test -v ./configlab -run TestMigrateLegacyCategories`。
3. 需要观察的现象：顶层 `Categories` 出现两条记录；第一条（内置）拿到 mp3/flac 等扩展名，第二条（自建 Games）扩展名为空；旧键消失而 `themeMode` 还在。
4. 预期结果：测试通过（断言与官方用例 [pkg/base/model_test.go:273-313](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model_test.go#L273-L313) 一致）。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么迁移函数返回 `bool` 而不是自己写存储？

答案：职责分离。`MigrateLegacyExtraCategories` 定义在 `pkg/base`（纯模型层），那里没有 Storage 依赖；是否落盘由调用方（`Downloader.Setup`）决定并执行。返回 bool 让调用方能准确判断「这次启动改没改配置」，避免每次启动都多一次无谓写盘。

**练习 2**：如果把 `Setup` 中迁移与 `Init` 的调用顺序对调，会发生什么？

答案：`Init` 先看到 `Categories` 为空且 `DownloadDir` 可能为空（首启目录未定）——即使目录非空，`Init` 也会先种入 4 个内置分类；随后迁移因 `len(cfg.Categories) > 0` 直接返回 false，旧分类永远搬不过来，用户自定义分类丢失。

**练习 3**：坏格式的旧数据（如 `"downloadCategories": "not-a-list"`）迁移失败后，旧键为什么必须保留？

答案：保留键意味着问题可被人工修复（用户可自己改存储文件或未来版本再迁移）；若迁移失败还删键，等于把损坏数据直接销毁，违背「迁移绝不丢数据」的底线。官方测试专门断言了 `malformed.Extra` 中的键原样存在。

## 5. 综合实践

把本讲四个模块串成一个「配置实验台」。在仓库根目录创建 `configlab/`（练习产物，不要提交），包含 `main.go` 与 `main_test.go`：

**任务 A：默认配置画像 + 部分更新模拟**

1. `main.go` 完成 4.1.4 的实践：`NewDownloader → Setup → GetConfig`，打印完整 JSON 与 `IsFirstLoad()`。
2. 接着在 `main.go` 里模拟「用户只想改两个设置」（示例代码）：

   ```go
   // 示例代码：走真实的 PutConfig 部分更新路径
   old, _ := d.GetConfig()
   next := &base.DownloaderStoreConfig{
       MaxRunning: 2,
       ProtocolConfig: map[string]any{
           "http": map[string]any{"connections": 32},
       },
   }
   if err := d.PutConfig(next.Merge(old)); err != nil { // 先补齐再整体写入
       panic(err)
   }
   after, _ := d.GetConfig()
   fmt.Println("MaxRunning:", after.MaxRunning)                 // 2（新值）
   fmt.Println("Webhook  :", after.Webhook != nil)              // 保持原状
   raw, _ := json.Marshal(after.ProtocolConfig["http"])
   fmt.Println("http cfg :", string(raw))                       // connections=32 且补全默认值
   ```

   注意这里验证的是完整链路：`Merge`（pkg/base 纯函数）→ `PutConfig`（校验 + `preserveProtocolState` + `normalizeProtocol` + 落盘）→ `GetConfig`（深拷贝读回）。观察 http 协议段是否被归一化补全了 `userAgent` 等默认字段。

**任务 B：迁移测试套件**

3. 把 4.5.4 的迁移测试扩充为四个子测试（可参考官方用例）：正常迁移、已有顶层分类不迁移、坏格式 no-op、`Init` 在迁移后不覆盖。
4. 运行 `go test -v ./configlab`，把每个子测试的输出贴进自己的笔记，并注明对应源码行为。

**验收标准**：能口头回答——`Merge` 与 `PutConfig` 各自负责什么？`normalizeProtocol` 为什么以 `DefaultConfig()` 为模板？`state` 段为何用户改不到？迁移为什么必须发生在 `Init` 之前？（所有命令的运行结果均待本地验证。）

## 6. 本讲小结

- 全局配置分两层：进程级 `DownloaderConfig`（Storage/刷新间隔/协议管理器，不落盘）内嵌用户级 `DownloaderStoreConfig`（一桶一键整体落盘于 bbolt 的 `config` 桶）。
- `Init` 是幂等的「补齐默认值」：MaxRunning→5、各子配置 nil→空结构体、首次且有下载目录时种入 4 个内置分类；已有值绝不被覆盖。
- `Merge` 解决部分更新：新配置的字段组为 nil/""/0 视为「没提供」，向旧配置借值；粒度是字段组整体替换，`*bool` 型字段（`AutoCategorize`）才具备三态资格。
- `normalizeProtocol` 以协议的 `DefaultConfig()` 为模板叠加用户 JSON，保证未提及字段取协议推荐值；`ConfigNormalizer` 可选接口负责校验（BT 端口/去重等）；用户提交中的 `state` 段被 `preserveProtocolState` 剥离，后端私有状态只有 `updateProtocolState` 一条写通道。
- `PutConfig` 是唯一完整写路径：DeepClone → 镜像校验 → 协议名校验 → state 保护 → 归一化 → 落盘 → `protocolJobs.changed` 热通知。
- `MigrateLegacyExtraCategories` 是迁移样板：条件明确、坏数据整体放弃、内置分类找回默认扩展名、返回 bool 交由调用方落盘；迁移必须在 `Init` 种子化之前执行。

## 7. 下一步学习建议

配置是「横切」所有模块的基础，接下来两条路都通：

- **顺层向下（推荐）**：进入第三单元 u3-l1「Fetcher 接口」，你会再次遇到本讲的 `DefaultConfig()`——它原来是 `FetcherManager` 接口的正式成员，本讲的 `normalizeProtocol` 正是消费这个契约的地方；理解了配置如何被 `setupFetcher → ctl.GetConfig → getProtocolConfig` 注入协议，再去读协议实现会非常顺。
- **横向延伸**：想看配置如何暴露成 HTTP 接口，可先读 [pkg/api/routes.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/api/routes.go) 中 `getConfig`/`updateConfig` 两个路由（u8-l1 会系统讲解）；想看分类配置如何真正参与目录决策，直接跳 u10-l1 的 `categoryForResource` 与 `decideDownloadDir`。
