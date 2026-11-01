# u2-l3 DownloaderStoreConfig：全局配置体系

## 1. 本讲目标

上一讲（u2-l2）我们解剖了单个任务 `Task` 的结构与状态机；这一讲把镜头拉远，看**决定所有任务共同行为的全局配置** `DownloaderStoreConfig`。学完本讲，你应该能够：

- 说出 `DownloaderStoreConfig` 每个字段组（下载目录、`MaxRunning`、`ProtocolConfig`、代理、webhook、脚本、自动种子、解压、分类、API 服务等）的作用、类型与默认值；
- 理解 `Init()`（补齐默认值）与 `Merge()`（部分更新）背后的「零值歧义」问题，以及 `Merge` 在真实调用方 `cmd/server.go` 首次启动时的用法；
- 掌握协议配置归一化 `normalizeProtocol`：一份 `map[string]any` 如何变成各协议声明的配置形状、`state` 段为何被特殊保护；
- 通过 `MigrateLegacyExtraCategories` 学会编写一次「失败安全」的配置迁移。

## 2. 前置知识

阅读本讲需要以下概念，不熟悉的读者先看通俗解释：

- **配置序列化**：配置要写进磁盘（bbolt 键值库，见 u1-l1），所以结构体字段都带 json tag（如 `json:"maxRunning"`），Go 结构体 ↔ JSON 的双向转换是一切配置操作的基础。
- **零值歧义**：Go 里 `int` 零值是 `0`、指针零值是 `nil`、`bool` 零值是 `false`。如果「用户想把这个字段改成某个值」和「用户根本没提这个字段」共用同一个结构体表达，就必须约定**零值 = 没改**。这是本讲反复出现的主题。
- **指针字段表达「未设置」**：`*WebhookConfig` 为 `nil` 表示「这次没提供 webhook 配置」；非 `nil` 才表示「提供了（哪怕里面全是 false）」。`bool` 无法区分「没设」和「设为 false」，所以 `AutoCategorize` 用了 `*bool`。
- **部分更新（PATCH 语义）**：调用方只想改一两个字段时，后端拿「新配置 + 旧配置」逐字段比对，新配置里没提供的字段沿用旧值。`Merge` 就是这个动作。
- **深拷贝**：配置在内存中被并发读写，任何对外返回、对内修改前都要先复制一份。本项目统一用 `util.DeepClone`（基于 JSON 往返）完成。
- **配置迁移（migration）**：软件升级后配置结构变了（字段搬家、改名），需要在启动时把旧格式搬成新格式，且必须失败安全——数据不合法就原样保留，绝不崩溃、绝不丢配置。

承接 u2-l1：`Options` 是**单个任务**的意图表达；本讲的 `DownloaderStoreConfig` 是**全局默认值**——`Options` 没覆盖的地方都会回落到这里（例如 HTTP 连接数的三级回退：任务级 → 全局 `ProtocolConfig` → 协议默认值）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲使用方式 |
|---|---|---|
| [pkg/base/model.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go) | `DownloaderStoreConfig` 及全部子配置结构体、`Init`/`Merge`/`MigrateLegacyExtraCategories` 的定义 | 主教材，逐段精读 |
| [pkg/download/protocol_config.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_config.go) | 协议配置的归一化、state 保护、启动初始化 | 主教材，逐段精读 |
| [pkg/download/downloader.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go) | `Setup` 加载配置、`GetConfig`/`PutConfig` 读写入口 | 精读配置相关函数 |
| [pkg/download/model.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go) | `DownloaderConfig`（进程级配置，不落盘），内嵌 `DownloaderStoreConfig` | 区分两层配置 |
| [pkg/base/github_mirror.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/github_mirror.go) 与 [internal/githubmirror/config.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/githubmirror/config.go) | GitHub 镜像配置的类型别名与默认值/校验 | 字段组的实例 |
| [internal/fetcher/fetcher.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go) 与 [internal/fetcher/jobs.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/jobs.go) | `FetcherManager.DefaultConfig` 与 `ConfigNormalizer` 契约 | 归一化的输入端 |
| [internal/protocol/http/fetcher_manager.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_manager.go) 与 [internal/protocol/bt/trackers.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/bt/trackers.go) | HTTP/BT 协议的 `DefaultConfig`/`NormalizeConfig` 实现 | 归一化的真实例子 |
| [cmd/server.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/server.go) | 服务端启动：首次加载时 `Merge` 启动参数、决定默认下载目录 | `Merge` 的真实调用方 |
| [pkg/base/model_test.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model_test.go) | 迁移与开关的官方单元测试 | 实践环节的对照答案 |

## 4. 核心概念与源码讲解

### 4.1 DownloaderStoreConfig 字段组：两层配置的边界

#### 4.1.1 概念说明

Gopeed 的「配置」有两个层次，初学者最容易混淆：

- **`DownloaderConfig`（进程级，不落盘）**：用哪个 `Storage`、`RefreshInterval` 多少、注册了哪些协议管理器 `FetchManagers`、下载目录白名单 `WhiteDownloadDirs`……这些由启动代码（`Boot()`、`cmd/web`）决定，重启即重组，**不属于用户设置**。
- **`DownloaderStoreConfig`（用户级，落盘）**：用户在设置界面改动的一切——下载目录、最大并行数、代理、webhook……它的注释是「the config that can restore the downloader」，即凭这份配置就能恢复整个 Downloader 的行为，所以名字里带 Store。

后者以前者**内嵌指针**的方式挂接。[pkg/download/model.go:L211-L225](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L211-L225) 中 `DownloaderConfig` 的最后一个字段是匿名的 `*base.DownloaderStoreConfig`，于是代码里既有 `d.cfg.RefreshInterval`（进程级）也有 `d.cfg.DownloadDir`（用户级，穿透内嵌直达）。[DownloaderConfig.Init()](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/model.go#L227-L250) 只补进程级默认值（Controller、ftp/hls/http/bt/ed2k 五个管理器、350ms 刷新、内存存储），完全不碰用户配置。

#### 4.1.2 核心流程

配置在生命周期中的位置：

```text
进程启动 NewDownloader(cfg)
  └─ cfg.Init()                 # 只补进程级默认值
Setup()
  ├─ storage.Get(bucketConfig, "config")   # 读用户级配置，读不到 → FirstLoad=true
  ├─ MigrateLegacyExtraCategories()        # 旧格式迁移（见 4.4）
  ├─ initProtocolConfigs()                 # Init() 补默认值 + 协议配置归一化（见 4.2/4.3）
  └─ 落盘 → 后续运行期间 GetConfig/PutConfig 读写（见 4.2）
```

#### 4.1.3 源码精读

结构体定义在 [pkg/base/model.go:L213-L231](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L213-L231)，逐字段说明如下（默认值来自 `Init()`，见 4.2）：

| 字段（json tag） | 类型 | 作用 | 默认值 | 谁在消费 |
|---|---|---|---|---|
| `FirstLoad`（`json:"-"`） | `bool` | 标记「配置是本次新建而非从存储读出」，**不序列化** | 首次为 true | [cmd/server.go:L30](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/server.go#L30) 判断是否注入启动默认值 |
| `downloadDir` | `string` | 默认下载目录 | 首次启动后由服务端决定（`~/Downloads` 或容器内 `../Downloads`），库形态为空 | 新任务的目录决策（第 4 单元） |
| `maxRunning` | `int` | 最大同时运行任务数 | `5` | 调度环节（[downloader.go:L578 notifyRunning](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L578)，详见 u4-l4） |
| `protocolConfig` | `map[string]any` | 按协议名索引的专属配置（键为 `fm.Name()`，如 `http`/`bt`） | 空 map，随后被归一化填充 | `getProtocolConfig`（见 4.3） |
| `githubMirror` | `*GithubMirrorConfig` | GitHub 下载镜像规则 | `Enabled:true` + 4 个内置镜像 | 下载加速（u10-l4） |
| `extra` | `map[string]any` | 杂项键值袋（如 `themeMode`），核心不解释内容 | `nil`（Init 不创建） | UI/端侧自行读写 |
| `proxy` | `*DownloaderProxyConfig` | 全局代理（含系统代理） | 全零值结构体 | [ToHandler](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L451-L463) 生成 http 代理函数 |
| `webhook` | `*WebhookConfig` | 下载完成后回调的 URL 列表 | `Enable:false` | u10-l3 |
| `script` | `*ScriptConfig` | 下载完成后执行的外部脚本 | `Enable:false` | u10-l3 |
| `autoTorrent` | `*AutoTorrentConfig` | 下载 `.torrent` 后自动创建 BT 任务 | 两个开关均 false | HTTP 完成钩子 |
| `archive` | `*ArchiveConfig` | 下载后自动解压归档 | 两个开关均 false | u10-l2 |
| `categories` | `[]*DownloadCategory` | 按扩展名路由的分类目录 | 首次且目录已定时种入 4 个内置分类 | [category.go:L18](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/category.go#L18) |
| `autoCategorize` | `*bool` | 分类路由总开关，**nil 即关闭** | `nil` | [AutoCategorizeEnabled](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L339-L341) |
| `api` | `*APIServerConfig` | 内嵌 REST/MCP 服务器配置 | `nil`（未启用） | 第 8 单元 |
| `autoStartTasks` | `bool` | 启动时自动续跑未完成任务 | `false` | [ContinueOnStartup](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L916-L921) |
| `autoDeleteMissingFileTasks` | `bool` | 自动删除文件已丢失的任务 | `false` | [downloader.go:L408](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L408) 的清理守卫 |

几个子配置结构体值得单独看一眼。[pkg/base/model.go:L408-L429](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L408-L429) 集中定义了 `WebhookConfig`、`ScriptConfig`、`AutoTorrentConfig`、`ArchiveConfig`——它们都是「一个 Enable 开关 + 一份载荷」的扁平结构。[APIServerConfig](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L389-L405) 额外有自己的 `Init()`（Network 缺省 `tcp`、Address 缺省 `127.0.0.1:9999`）。[DownloadCategory](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L432-L439) 用 `NameKey` 标识内置分类（空 `Name` 交给前端做 i18n），`Extensions` 是不带点号的扩展名列表。

`githubMirror` 的类型其实是别名：[pkg/base/github_mirror.go:L6-L11](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/github_mirror.go#L6-L11) 把 `GithubMirrorConfig` 直接等价到 `internal/githubmirror.Config`，既保住公开类型名，又把实现留在 internal（与 u1-l3 的分层原则一致）。默认值在 [internal/githubmirror/config.go:L21-L28](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/githubmirror/config.go#L21-L28)：启用状态 + 1 个 jsdelivr + 3 个 ghProxy 内置镜像；[Validate](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/githubmirror/config.go#L29-L43) 限制镜像不超过 32 个、URL 必须是合法 http(s) 且类型只允许 `jsdelivr`/`ghProxy`。

#### 4.1.4 代码实践

**实践目标**：亲眼看到一份「首次启动」的默认配置长什么样。

**操作步骤**（示例代码，非项目原有文件）：

1. 在仓库里新建临时测试文件 `pkg/download/zz_u2l3_practice_test.go`，内容：

```go
package download

import (
	"encoding/json"
	"testing"
)

func TestU2L3DumpDefaultConfig(t *testing.T) {
	d := NewDownloader(nil) // 进程级配置全默认：内存存储 + 5 个协议管理器
	if err := d.Setup(); err != nil {
		t.Fatal(err)
	}
	defer d.Close()

	cfg, err := d.GetConfig()
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.MarshalIndent(cfg, "", "  ")
	t.Logf("default config:\n%s", raw)
	t.Logf("first load: %v", d.IsFirstLoad())
}
```

2. 运行 `go test ./pkg/download/ -run TestU2L3DumpDefaultConfig -v`。
3. 观察完后删除该临时文件（或留在本地分支，不要提交）。

**需要观察的现象**：完整 JSON 里每个字段组的形状；`protocolConfig` 里五个协议各自的键；`IsFirstLoad` 的值。

**预期结果**（依据 [Init()](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L233-L275) 与归一化逻辑推导）：`maxRunning` 为 5；`githubMirror.enabled` 为 true 且含 4 个内置镜像；`protocolConfig.http` 形如 `{"userAgent":"Mozilla/5.0 ...","connections":16,"useServerCtime":false}`（默认值来自 [http/fetcher_manager.go:L74-L79](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_manager.go#L74-L79)）；`categories` 为 `null`——因为此时 `downloadDir` 还是空串，`Init` 不种分类（详见 4.2）；`IsFirstLoad` 为 true。完整输出格式待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `AutoCategorize` 用 `*bool`，而 `MaxRunning` 用普通 `int`？

答案：`bool` 的零值 `false` 与「未设置」无法区分，分类开关需要三态（未设/开/关），所以用指针——[AutoCategorizeEnabled](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L339-L341) 明确「nil 即关闭」，让旧配置文件里没有该字段时保持关闭。`MaxRunning` 借助「0 = 未设置」的约定（`Init`/`Merge` 都把 0 当未提供），代价是无法表达「最大并行数为 0」——这恰好无意义，可接受。

**练习 2**：`FirstLoad` 为什么标 `json:"-"`？

答案：它描述的是「本次进程内配置是新建的」这一瞬时事实，不属于要持久化的状态。[IsFirstLoad 的注释](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1390-L1397)指出：`GetConfig` 返回的深拷贝里它永远是 false，所以必须通过 `IsFirstLoad()` 查询而非读字段。

**练习 3**：`Extra` 这个 `map[string]any` 存在的意义是什么？

答案：承载 UI/端侧的杂项键值（如 `themeMode`），核心不理解其语义、原样存取。历史上它还承载过 `downloadCategories`（v2.0.0-beta 的分类列表），这正是 4.4 迁移的来源。

### 4.2 Init 与 Merge：默认值补齐与部分更新

#### 4.2.1 概念说明

`Init()` 解决「新建配置时字段残缺」：把空缺字段填上默认值，它是**幂等**的（重复调用不会破坏已有值）。`Merge(before)` 解决「调用方只提供了一部分字段」：接收者 `cfg` 是新提交的部分配置，凡是零值字段都从 `before` 回填。两者共同对抗 Go 的零值歧义：

\[ \text{merged}(f) = \begin{cases} \text{new}(f), & \text{new}(f) \neq \text{零值} \\ \text{before}(f), & \text{否则} \end{cases} \]

注意 `Merge` 的单位是**字段级**：指针字段以 `nil` 为「未提供」，标量以零值为「未提供」；一旦提供了指针（哪怕指向全零结构体），整个子配置按新值算。

#### 4.2.2 核心流程

`Init()` 的补齐顺序（[pkg/base/model.go:L233-L275](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L233-L275)）：

```text
MaxRunning == 0        → 5
ProtocolConfig == nil  → 空 map
GithubMirror == nil    → DefaultGithubMirrorConfig()（4 个内置镜像）
Proxy/Webhook/Script/AutoTorrent/Archive == nil → 各自的全零值结构体
len(Categories)==0 且 DownloadDir != "" → 种入 Music/Video/Document/Program 四个内置分类
（DownloadDir == "" 时不种——目录还没定，分类路径无处挂靠）
```

`Merge(before)` 的流程（[pkg/base/model.go:L343-L387](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L343-L387)）：先处理 `before == nil` 直接返回自身；再逐字段「零值则取旧值」——标量 `DownloadDir`/`MaxRunning` 与 map `ProtocolConfig`/`Extra` 在前，各指针子配置在后，最后返回接收者自身（链式风格）。

#### 4.2.3 源码精读

内置分类的种子逻辑在 [pkg/base/model.go:L264-L273](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L264-L273)，注释点明动机：为「从不走 Flutter 启动流程的部署」（如 headless API 服务器）预置分类。扩展名映射表 [builtinCategoryExtensions](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L279-L284) 的注释要求与 Flutter 侧的 `kDefaultCategoryExtensions` 保持同步——这是跨端联动的经典维护点；[builtinSeedCategory](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L286-L294) 用 `filepath.Join(downloadDir, name)` 生成路径并深拷贝扩展名切片，避免共享底层数组。

`Merge` 的真实调用方在服务端首次启动。[cmd/server.go:L25-L37](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/server.go#L25-L37)：先 `GetConfig()` 拿到刚初始化的默认配置，若 `IsFirstLoad()` 且启动参数带了 `DownloadConfig`（来自配置文件/环境变量/命令行，见 u1-l2），就执行 `cfg.DownloadConfig.Merge(downloadCfg)`——启动参数里**显式给出的字段**覆盖默认值，其余保持默认，然后 `PutConfig` 落盘。紧接着 [cmd/server.go:L39-L61](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/server.go#L39-L61) 解决默认下载目录（Docker 内取存储目录旁的 `Downloads`，否则用户主目录的 `Downloads`），目录确定后再调一次幂等的 `Init()` 补种分类——注释明确写着「Init is idempotent and leaves an existing category list untouched」。

还要澄清一个易错点：运行期的 `PutConfig` **不做 Merge**。它只对 `GithubMirror` 为 nil 和缺失的协议条目做两处兜底（见 4.2.4 的实践与 4.3 的精读），所以调用方必须先 `GetConfig` 改字段再整体提交——官方测试 [downloader_test.go:L817-L825](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader_test.go#L817-L825) 展示的正是这个「读-改-写」三步。

#### 4.2.4 代码实践

**实践目标**：亲手模拟一次「部分更新」，验证零值字段被旧值回填。

**操作步骤**（示例代码）：新建临时测试 `pkg/base/zz_u2l3_merge_practice_test.go`：

```go
package base

import (
	"encoding/json"
	"testing"
)

func TestU2L3MergePractice(t *testing.T) {
	// 1) 模拟存量配置：下载目录 /data、webhook 已开启，其余走 Init 默认值
	before := (&DownloaderStoreConfig{
		DownloadDir: "/data",
		Webhook:     &WebhookConfig{Enable: true, URLs: []string{"http://127.0.0.1:9000/hook"}},
	}).Init()

	// 2) 模拟 UI 只想把 MaxRunning 改成 8
	patch := &DownloaderStoreConfig{MaxRunning: 8, ProtocolConfig: map[string]any{"http": map[string]any{"connections": 4}}}

	// 3) Merge：patch 中的零值字段全部回填旧值
	merged := patch.Merge(before)

	out, _ := json.MarshalIndent(map[string]any{
		"downloadDir": merged.DownloadDir,
		"maxRunning":  merged.MaxRunning,
		"webhook":     merged.Webhook,
		"http":        merged.ProtocolConfig["http"],
	}, "", "  ")
	t.Logf("merged:\n%s", out)
}
```

运行 `go test ./pkg/base/ -run TestU2L3MergePractice -v`，观察后删除临时文件。

**需要观察的现象**：`MaxRunning` 是否为 8；`DownloadDir` 是否被回填为 `/data`；`Webhook` 是否仍是开启状态；`protocolConfig` 是否整个被 patch 替换。

**预期结果**（依据 [Merge 源码](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L343-L387) 推导）：`maxRunning=8`、`downloadDir=/data`、`webhook` 保留旧值（patch 未提供）；`protocolConfig` 因为 patch 显式提供了 map，**整个替换**为只含 `http` 的新 map（`bt` 等其他协议条目不回填——map 级替换而非按键合并，这是 Merge 的粒度边界）。这正是 PutConfig 在 [downloader.go:L1428-L1431](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1428-L1431) 为缺失协议条目单独兜底的原因。

#### 4.2.5 小练习与答案

**练习 1**：调用 `Merge(nil)` 会发生什么？

答案：直接返回接收者自身，不报错（[pkg/base/model.go:L344-L346](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L344-L346)）。这让 `cmd/server.go` 无需先判断存储里有没有旧配置就能调用。

**练习 2**：`Init` 里种内置分类为什么要求 `DownloadDir != ""`？首次启动时目录为空，分类岂不是永远种不上？

答案：分类路径要挂在下载目录下（`filepath.Join(downloadDir, name)`），目录未定时无法生成。首次启动时目录由 [cmd/server.go:L39-L54](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/server.go#L39-L54) 在核心启动后确定，确定后于 L59 再调一次幂等的 `Init()` 补种——两段式初始化。

**练习 3**：用户想把 webhook 从「开启」改回「关闭」，提交 `Webhook:&WebhookConfig{Enable:false}` 会被 `Merge` 用旧值覆盖吗？

答案：不会。`Merge` 只在指针为 `nil` 时回填（[L365-L367](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L365-L367)）；指向 `{Enable:false}` 的非 nil 指针表示「显式提供了这份子配置」，按新值生效。零值歧义只在字段级（nil/0），不深入结构体内部。

### 4.3 协议配置归一化：normalizeProtocol 与 state 保护

#### 4.3.1 概念说明

`ProtocolConfig` 是 `map[string]any`：键是协议名（`http`、`bt`……），值是**形状未知**的任意 JSON 数据。但每个协议真正想要的是自己声明的强类型配置（如 HTTP 的 `config{UserAgent, Connections, UseServerCtime}`）。归一化 `normalizeProtocol` 就是「翻译官」：以协议的 `DefaultConfig()` 为底板，把用户数据覆盖上去，产出一份**形状确定、缺省齐全、通过校验**的配置。它带来三个保证：

1. **默认值齐全**——用户只写了 `connections`，`userAgent` 仍是协议默认值而不是空串；
2. **校验前置**——非法值在写配置时就报错，而不是下载任务运行到一半才炸；
3. **形状稳定**——存储里永远是完整结构，读取方（`getProtocolConfig`）可以放心做 `MapToStruct`。

此外 BT 这类协议的配置里有一段 `state`（tracker 订阅的抓取结果等），由后端定时任务写入，**不属于用户可写数据**，归一化之外还有一层专门的剥离与回填保护。

#### 4.3.2 核心流程

`normalizeProtocol(fm, v)`（[pkg/download/protocol_config.go:L36-L72](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_config.go#L36-L72)）：

```text
v (任意值)
  → json.Marshal 得到 raw
  → target = fm.DefaultConfig()            # 协议声明的默认形状（指针）
  → DefaultConfig 返回的是值类型？→ 反射 New 一个指针、把默认值灌进去
  → json.Unmarshal(raw, target)            # 用户值逐字段覆盖默认值
  → fm 实现了 ConfigNormalizer？→ 再走 fm.NormalizeConfig(序列化后的 target)
  → 返回归一化结果
```

写路径 `PutConfig` 中每个协议都要过三关（[pkg/download/downloader.go:L1426-L1446](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1426-L1446)）：

```text
value = next.ProtocolConfig[name]（缺失则用旧值）
  → preserveProtocolState(value, old)     # 剥掉客户端提交的 state、回填旧 state
  → normalizeProtocol(fm, value)          # 默认值兜底 + 协议自定义校验
  → 与旧值比较 JSON 字节，不同则记入 changedProtocols
落盘 storage.Put(bucketConfig,"config",next) → 切换内存配置 → 通知 protocolJobs.changed(name)
```

启动路径 `initProtocolConfigs`（[pkg/download/protocol_config.go:L154-L172](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_config.go#L154-L172)）更简单：先 `cfg.Init()` 与镜像校验，再对每个注册的协议管理器做一次归一化并落盘——保证存储里永远有全部协议的完整条目。

#### 4.3.3 源码精读

- **契约端**：[FetcherManager 接口](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go#L122-L143) 要求每个协议实现 `DefaultConfig() any`（L135-L136）；可选接口 [ConfigNormalizer](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/jobs.go#L69-L71) 让协议「拥有校验和派生字段」的权力。若协议不暴露配置，归一化直接报错「protocol does not expose configuration」（[protocol_config.go:L42-L44](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_config.go#L42-L44)）。
- **值类型默认的处理**：[protocol_config.go:L46-L57](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_config.go#L46-L57) 的注释说明管理器返回的默认值可能是类型指针、也可能是值或 map；`json.Unmarshal` 需要可寻址目标，所以值类型先 `reflect.New` 出指针、把默认值序列化再灌入，最后返回时又 `Elem()` 还原为值（L68-L70）。
- **真实例一（HTTP）**：[http/fetcher_manager.go:L74-L79](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_manager.go#L74-L79) 返回 `&config{UserAgent: DefaultUserAgent, Connections: 16}`；配置结构体在 [http/config.go:L6-L10](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/config.go#L6-L10)。HTTP 不实现 `ConfigNormalizer`，走默认归一化分支。
- **真实例二（BT）**：[bt/trackers.go:L67-L104](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/bt/trackers.go#L67-L104) 是完整的 `NormalizeConfig`：校验端口/分享率/做种时间范围（L72-L77）、tracker 订阅数不超过 64、URL 必须合法（L75-L84）、去重（L78/L85），并清理 `State` 中已取消订阅的残留条目（L91-L104）——校验与派生逻辑收归协议自己。
- **state 保护**：[preserveProtocolState](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_config.go#L76-L95) 把提交数据里所有大小写变体的 `state` 键删掉，再从旧配置回填——注释强调「ignores all client-supplied state, including null or malformed state values」。后端唯一的 state 写入通道是 [updateProtocolState](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_config.go#L108-L150)，它同样走 `normalizeProtocol` 并整体落盘。
- **读路径**：[readProtocolConfig](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_config.go#L96-L104) 与 [getProtocolConfig](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1501-L1514) 都是「取配置 → `MapToStruct` 灌入调用方给的目标结构」，因为归一化保证了形状，这一步不会缺字段。

#### 4.3.4 代码实践

**实践目标**：验证归一化的三个保证——默认值兜底、未知协议拒绝、state 不可被客户端改写。

**操作步骤**（示例代码）：在临时测试 `pkg/download/zz_u2l3_practice_test.go` 里追加：

```go
func TestU2L3NormalizePractice(t *testing.T) {
	d := NewDownloader(nil)
	if err := d.Setup(); err != nil {
		t.Fatal(err)
	}
	defer d.Close()

	// 1) 只提交 http.connections，看其余字段是否被默认值补齐
	cfg, _ := d.GetConfig()
	cfg.ProtocolConfig = map[string]any{
		"http": map[string]any{"connections": 4},
	}
	if err := d.PutConfig(cfg); err != nil {
		t.Fatal(err)
	}
	after, _ := d.GetConfig()
	t.Logf("http after: %v", after.ProtocolConfig["http"])

	// 2) 提交一个不存在的协议名，看是否被拒绝
	bad, _ := d.GetConfig()
	bad.ProtocolConfig["nope"] = map[string]any{}
	t.Logf("unknown protocol err: %v", d.PutConfig(bad))
}
```

运行 `go test ./pkg/download/ -run TestU2L3NormalizePractice -v`。

**需要观察的现象**：第 1 步后 `http` 条目的完整内容；第 2 步 `PutConfig` 返回的错误信息。

**预期结果**：`http` 条目为 `map[connections:4 userAgent:Mozilla/5.0 ... useServerCtime:false]`——只写一个字段，`userAgent` 仍被 [DefaultConfig](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/protocol/http/fetcher_manager.go#L74-L79) 兜底；第 2 步返回 `unsupported protocol: nope`（[protocolManager](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_config.go#L14-L21) 在 [PutConfig:L1420-L1425](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1420-L1425) 被逐键调用）。进阶观察：给 `bt` 条目塞一个伪造的 `state` 再 PutConfig，随后 GetConfig 会发现 state 原封不动（[preserveProtocolState](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_config.go#L76-L95) 生效）；官方用例见 [protocol_state_test.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/protocol_state_test.go)（待本地验证具体输出）。

#### 4.3.5 小练习与答案

**练习 1**：`normalizeProtocol` 为什么先 `Marshal` 再 `Unmarshal` 到 `DefaultConfig` 上，而不是逐字段反射合并？

答案：JSON 往返是「任意 map → 强类型结构」最通用、最不容易出错的深合并方式：用户没提供的字段天然保留底板上的默认值，类型不匹配会立刻报错；逐字段反射则要处理嵌套结构、切片、指针等各种情况，代码复杂且易错。代价是多两次序列化，但配置读写是低频操作，可接受。

**练习 2**：为什么 PutConfig 检测到协议配置变化后要调用 `protocolJobs.changed(name)`？

答案：有些协议配置背后挂着定时任务（BT 的 tracker 订阅刷新，见 u6-l4）。配置变了（比如换了订阅 URL），对应任务需要立即感知重算，而不是等下一个周期。[downloader.go:L1453-L1457](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L1453-L1457) 通过比较新旧 JSON 字节判断「哪些协议真的变了」，只通知变更者。

**练习 3**：`getProtocolConfig(name, v)` 为什么不直接把 `ProtocolConfig[name]` 断言成协议配置类型？

答案：存储里的值是 `map[string]any`（JSON 反序列化产物），而且协议真正的配置结构体（如 http 包的 `config`）是 internal 包的未导出类型，外部无法断言。`MapToStruct` 按 json tag 灌入调用方自己声明的目标结构，正是靠归一化保证了字段齐全，这一步才安全。

### 4.4 配置迁移实战：MigrateLegacyExtraCategories

#### 4.4.1 概念说明

v2.0.0-beta 版本曾把下载分类放在 `Extra["downloadCategories"]` 里；后来分类升级为顶层字段 `Categories` 并支持扩展名路由。于是出现经典的迁移问题：老用户升级后，配置文件里还是旧格式，如何无感搬到新格式？[MigrateLegacyExtraCategories](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L296-L334) 是一份范本，它的设计要点：

- **只在确有可搬数据时动手**：顶层已有分类、`Extra` 为空、没有 legacy 键、数据损坏——统统返回 false「无事发生」；
- **内置分类恢复默认扩展名**：旧列表早于扩展名路由机制，内置条目按 `NameKey` 找回默认扩展名；用户自建目录保持空列表，等用户在设置里编辑；
- **原子收尾**：搬移成功后删除 legacy 键，其余 `Extra` 条目（如 `themeMode`）原样保留；
- **返回 bool 而非 error**：调用方只关心「配置是否变了、要不要落盘」。

#### 4.4.2 核心流程

```text
MigrateLegacyExtraCategories():
  len(Categories) > 0 或 Extra == nil      → false（已有新格式 / 无处可搬）
  Extra["downloadCategories"] 不存在        → false
  Marshal → Unmarshal 成 []DownloadCategory 失败 → false（原样保留，失败安全）
  逐条搬移：
      nil 条目跳过
      内置条目(NameKey 命中) 且扩展名为空 → 补默认扩展名
  结果为空                                  → false
  Categories = 结果；delete(Extra, "downloadCategories") → true
```

调用时机在 [Setup](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L195-L207)：**先迁移、后 `initProtocolConfigs`（内含 `Init`）**，返回 true 时直接 `storage.Put` 落盘。顺序不能反——若先 `Init`，空分类会被种入内置分类，迁移的守卫 `len(Categories) > 0` 就会误判「用户已有分类」而放弃搬移。注释还解释了为何不走 `PutConfig`：此刻配置尚未初始化，`PutConfig` 的校验会失败。

#### 4.4.3 源码精读

- 迁移主体 [pkg/base/model.go:L302-L334](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L302-L334)：L303 第一道守卫；L306-L317 取出 legacy 键并做 `Marshal → Unmarshal` 的类型转换（`any` 里的 `[]any` 转成 `[]*DownloadCategory`）；L318-L327 逐条搬移并按 [builtinCategoryExtensions](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L279-L284) 补扩展名；L331-L333 赋值、删键、返回 true。
- 官方测试 [pkg/base/model_test.go:L272-L377](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model_test.go#L272-L377) 覆盖四个场景：内置+自建混合搬移且 `Init` 之后不被覆盖（L273-L313）、顶层已有分类时绝不动手（L315-L331）、缺失/空/损坏列表均为 no-op（L333-L358）、nil 条目跳过（L360-L376）。写自己的迁移时，这份测试就是行为清单。
- 类似的「可选开关」兼容设计还可参考 [AutoCategorizeEnabled 的测试](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model_test.go#L259-L270)：旧配置没有 `AutoCategorize` 字段时保持关闭——「nil 即关闭」与「迁移失败安全」是同一种对旧数据的宽容。

#### 4.4.4 代码实践

**实践目标**：写测试复现「legacy 分类搬移到顶层字段」的全过程。

**操作步骤**（示例代码）：新建临时测试 `pkg/base/zz_u2l3_migrate_practice_test.go`：

```go
package base

import "testing"

func TestU2L3MigratePractice(t *testing.T) {
	cfg := &DownloaderStoreConfig{
		DownloadDir: "/downloads",
		Extra: map[string]any{
			"downloadCategories": []any{
				map[string]any{"name": "", "path": "/old/Music", "isBuiltIn": true, "nameKey": "categoryMusic"},
				map[string]any{"name": "Games", "path": "/old/Games"},
				nil, // 模拟损坏条目
			},
			"themeMode": "dark",
		},
	}

	if !cfg.MigrateLegacyExtraCategories() {
		t.Fatal("migration should report a change")
	}
	if len(cfg.Categories) != 2 {
		t.Fatalf("categories = %d, want 2 (nil entry skipped)", len(cfg.Categories))
	}
	if len(cfg.Categories[0].Extensions) == 0 {
		t.Fatal("built-in category must regain default extensions")
	}
	if len(cfg.Categories[1].Extensions) != 0 {
		t.Fatal("custom category keeps its empty list")
	}
	if _, ok := cfg.Extra["downloadCategories"]; ok {
		t.Fatal("legacy key must be cleared")
	}
	if cfg.Extra["themeMode"] != "dark" {
		t.Fatal("unrelated extra entries must survive")
	}

	// 迁移后 Init 不得覆盖搬移结果
	cfg.Init()
	if len(cfg.Categories) != 2 {
		t.Fatalf("categories after Init = %d, want 2", len(cfg.Categories))
	}
}
```

运行 `go test ./pkg/base/ -run TestU2L3MigratePractice -v`，随后对照官方用例 `go test ./pkg/base/ -run TestDownloaderStoreConfig_MigrateLegacyExtraCategories -v`，最后删除临时文件。

**需要观察的现象**：迁移返回值、`Categories` 的条目数与内容、`Extra` 中 legacy 键是否消失、`themeMode` 是否保留、再次 `Init` 是否覆盖。

**预期结果**：全部断言通过——迁移返回 true；`nil` 条目被跳过后剩 2 条；内置的 `categoryMusic` 拿回默认扩展名（mp3/flac/wav 等，见 [builtinCategoryExtensions](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L279-L284)）；自建 `Games` 保持空扩展名；`themeMode` 保留；幂等的 `Init` 不覆盖已有分类。官方测试（L272-L377）验证过同样的行为。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `Setup` 中的迁移挪到 `initProtocolConfigs` 之后会发生什么？

答案：`initProtocolConfigs` 内部先执行 `cfg.Init()`；在 `DownloadDir` 非空的存量配置上，空的 `Categories` 会被种入 4 个内置分类，于是迁移的第一道守卫 `len(cfg.Categories) > 0`（[L303](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L303)）误判为「用户已有分类」，直接返回 false——老用户的自建分类永远搬不过来，legacy 键也一直留在 `Extra` 里。

**练习 2**：迁移函数为什么对「malformed legacy list」返回 false 而不是报错？

答案：迁移必须失败安全。数据损坏时（如 `downloadCategories` 是个字符串），报错会阻断启动，删除数据会丢用户配置；返回 false 表示「无事发生」，legacy 键原样保留（[L349-L357 的测试](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model_test.go#L349-L357) 明确要求 malformed 键不被清除），用户仍可在设置界面手动处理。

**练习 3**：迁移把内置分类的扩展名「找回来」这一步为什么必要？

答案：beta 版的分类早于「扩展名路由」机制，旧数据里的内置分类没有扩展名列表；若不补上，迁移后内置分类永远匹配不到任何文件，等于静默失效。按 `NameKey` 从 `builtinCategoryExtensions` 恢复（[L323-L325](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/base/model.go#L323-L325)），且只在该分类扩展名为空时才补，不覆盖用户已有的自定义列表。

## 5. 综合实践

**任务：写一个「配置体检」测试，走完配置的完整生命周期。**

在 `pkg/download/zz_u2l3_lifecycle_practice_test.go`（示例代码，做完删除）里完成：

1. **落盘启动**：用 `NewBoltStorage(t.TempDir())` 作为存储（构造函数见 [pkg/download/storage.go:L139](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/storage.go#L139)），连同 `StorageDir` 一起传入 `DownloaderConfig`，创建 Downloader 并 `Setup`，确认 `IsFirstLoad()` 为 true，打印默认配置 JSON。
2. **模拟首启注入**：模仿 [cmd/server.go:L30-L60](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/cmd/server.go#L30-L60)：构造一份只含 `MaxRunning: 3` 和 `DownloadDir: <临时目录>` 的新配置，先 `Merge(旧配置)` 再 `PutConfig`，然后把目录填入后调用一次 `Init()` 并再次 `PutConfig` 补种分类。
3. **重启验证持久化**：`Close()` 第一个 Downloader，用同一个目录新建 `NewBoltStorage` 再起一个 Downloader 并 `Setup`——验证 `IsFirstLoad()` 为 false、`MaxRunning` 仍为 3、内置分类已存在（说明配置确实来自磁盘而非内存）。
4. **注入 legacy 数据再重启**：把 `Categories` 置 nil、`Extra["downloadCategories"]` 塞入两个旧条目后 `PutConfig`；再次 Close 并重启，验证迁移自动发生：`Categories` 恢复且 `Extra` 中 legacy 键被清除（对应 [Setup:L199-L203](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/pkg/download/downloader.go#L199-L203) 的路径）。

**预期结果**：四个阶段分别验证了「默认值 → 首启合并与种子 → 持久化恢复 → 自动迁移」整条链路；步骤 3 中 bbolt 文件在 `Close` 后释放锁、可被第二个实例打开（锁行为细节待本地验证，如遇占用可确认已先 `Close`）。完成后删除临时测试文件。

## 6. 本讲小结

- 配置分两层：`DownloaderConfig`（进程级：存储、刷新间隔、协议管理器）不落盘；`DownloaderStoreConfig`（用户级）以匿名指针内嵌其中，存进 `bucketConfig` 桶，凭它可恢复整个 Downloader。
- `Init()` 幂等补默认值（MaxRunning=5、4 个内置镜像、各子配置结构体、目录确定后种 4 个内置分类）；`Merge()` 做字段级部分更新，零值 = 未提供，真实调用方是服务端首启的参数注入。
- `ProtocolConfig` 是 `map[string]any`，由 `normalizeProtocol` 以各协议 `DefaultConfig()` 为底板做 JSON 往返合并，可选 `ConfigNormalizer`（如 BT）追加校验与派生；`state` 段由 `preserveProtocolState` 剥离回填，后端经 `updateProtocolState` 独占写入。
- 运行期 `PutConfig` 不做 Merge，调用方须「GetConfig → 改 → PutConfig」整体提交；写库成功后只对真正变更的协议触发 `protocolJobs.changed`。
- `MigrateLegacyExtraCategories` 是配置迁移范本：守卫前置、失败安全、搬移后清键、返回 bool 供调用方决定落盘；它在 `Setup` 中先于 `Init` 执行，顺序不可颠倒。

## 7. 下一步学习建议

本讲补齐了领域模型的最后一块——配置。第 3 单元将进入架构脊柱：建议先读 [internal/fetcher/fetcher.go](https://github.com/GopeedLab/gopeed/blob/fc0a1e0ef97ff5ec7f2dc875ff28925fe06b28ed/internal/fetcher/fetcher.go) 的 `Fetcher`/`FetcherManager` 接口（本讲已提前认识 `DefaultConfig` 这一个方法），理解「协议接入层」如何被 `DownloaderConfig.Init` 注册、又如何消费本讲的 `ProtocolConfig`；随后带着「配置在 `Setup` 中如何被加载、任务如何据此排队」的问题进入 u4-l1（Downloader 的结构与初始化）。BT 协议配置的 `state` 与定时任务细节留待 u6-l4 展开。
