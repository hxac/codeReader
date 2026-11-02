# 自动分类与目录决策

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `DownloadCategory` 模型的每个字段，以及内置分类种子与 legacy 迁移的兼容设计。
2. 说出 `categoryForResource` 的匹配规则：为什么只有「单文件资源」才会被路由，扩展名如何归一化后比较。
3. 区分「建议目录」（`ResolveResult.SuggestedDownloadDir`，给 UI 的提示）与「最终目录」（`decideDownloadDirForTask`，建任务时才决定）这两次分离的决策。
4. 讲清 `decideDownloadDirForTask` 的完整决策树，包括本轮新增的 TaskGroup 分支：分组任务跳过分类、直接落入预留的组目录。
5. 理解 `WhiteDownloadDirs` 白名单的校验时机、`filepath.Match` 模式匹配，以及「分类目录被拒则回退基础目录」的失败安全设计。

## 2. 前置知识

本讲建立在 u2-l3（全局配置体系）与 u4-l2（任务主链路 Resolve → Create → doStart）之上，先回顾几个关键概念：

- **配置的两层结构**：`DownloaderConfig` 是进程级配置（存储、协议管理器、白名单），不落盘；`DownloaderStoreConfig` 是用户级配置（下载目录、分类、开关），持久化到 bbolt 的 `config` 桶。分类（`Categories`）与分类开关（`AutoCategorize`）属于后者。
- **零值歧义问题**：Go 的 `bool` 零值是 `false`，无法区分「用户明确关闭」与「从未设置」。因此 `AutoCategorize` 用 `*bool` 三态表达，未设置即为关闭。
- **NeedsPreparation 一次性准备**（u4-l2）：任务第一次真正启动前，`prepareTask` 会决定下载目录、处理重名并落盘；此后任务保留已定目录，只在每次启动时重过白名单。目录决策正是发生在这一次性准备里。
- **TaskGroup 任务分组**（u4-l6，提交 1e36599 引入）：批量创建时可以先预留一个组目录，成员任务以 `ParentID` 关联到组，文件统一落入 `<父目录>/<组名>/`。本讲的目录决策函数新增了 `group` 参数来处理这条路径。
- **路径占位符**：下载目录支持 `%year%`、`%month%`、`%day%`、`%date%` 四个占位符，在 `initDownloadPath` 中展开为当前日期。白名单校验发生在展开之后。

一个容易混淆的点先说清楚：**自动分类只决定「目录」，不决定「文件名」**。文件名来自协议解析（如 HTTP 的 `Content-Disposition`）或 `Options.Name`，分类路由从不读取文件名以外的任何内容——连 `opts.Name` 的重命名也不参与匹配（后文 4.2.4 有测试佐证）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pkg/download/category.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category.go) | 本讲主角：分类匹配（`categoryForResource`）、目录决策（`decideDownloadDirForTask`）、建议目录（`suggestedDownloadDir`）以及与 Dart 侧对齐的归一化工具 |
| [pkg/base/model.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go) | `DownloadCategory` 模型、`DownloaderStoreConfig` 中的分类字段、内置分类种子、legacy 迁移、`AutoCategorizeEnabled`、`Options` 的旁路开关 |
| [pkg/download/downloader.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go) | 目录决策的全部调用点：`Resolve` 填充建议目录、`prepareTask` 决定最终目录、批量分组改写成员路径、`createTaskGroup` 预留组目录、白名单实现 |
| [pkg/download/model.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go) | `ResolveResult.SuggestedDownloadDir`、`TaskGroup`、`DownloaderConfig.WhiteDownloadDirs` |
| [pkg/download/category_test.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category_test.go) | 本讲的测试宝库：决策表驱动测试、白名单测试、分组白名单测试、建议目录测试、集成测试 |

## 4. 核心概念与源码讲解

### 4.1 DownloadCategory 模型与配置

#### 4.1.1 概念说明

「自动分类」要解决的问题很朴素：用户希望 `setup.exe` 自动进 `Program` 目录、`movie.mp4` 自动进 `Video` 目录，而不必每次手动选目录。模型上，一个分类就是「一组扩展名 → 一个目录」的映射：

```go
type DownloadCategory struct {
    Name       string   // 展示名，内置分类留空、用 NameKey 做 i18n
    Path       string   // 该分类的下载目录
    NameKey    string   // 内置分类的国际化 key（categoryMusic 等）
    IsBuiltIn  bool     // 是否内置分类
    IsDeleted  bool     // 内置分类被用户删除后的软删除标记
    Extensions []string // 路由到本分类的扩展名列表，不带前导点
}
```

两个设计值得注意：

1. **软删除**：内置分类被用户删除时不从列表移除，而是打 `IsDeleted` 标记——匹配时跳过它，但升级时能识别「这是被用户删过的」而不会重新种回来。
2. **双端同构**：分类列表在 Flutter UI 中编辑，在后端消费。两侧必须对「扩展名怎么归一化、空格怎么 trim」给出完全一致的答案，否则同一个文件在设置页预览进 A 类、实际下载进 B 类。后端为此专门复刻了 Dart 的字符串语义（见 4.2）。

#### 4.1.2 核心流程

分类配置的生命周期：

```text
bbolt config 桶加载 DownloaderStoreConfig
        │
        ▼
MigrateLegacyExtraCategories()   ← Setup 中先于 Init 执行
  把 v2.0.0-beta 写在 extra.downloadCategories 的旧列表搬到顶层
  Categories 字段，内置条目按 NameKey 补回默认扩展名，删除 legacy 键
        │
        ▼
Init()
  若 Categories 为空且 DownloadDir 非空
  → 种入 4 个内置分类（Music/Video/Document/Program）
        │
        ▼
运行期：AutoCategorizeEnabled() 判定开关（*bool 三态，未设置即关闭）
```

为什么种子逻辑要判断 `DownloadDir != ""`？注释写明：这是为「从不运行 Flutter 启动流程的部署形态」（如 headless API 服务器）兜底——正常桌面端由 UI 侧完成首次种子，后端只在确实有下载目录时才补种，避免给空目录配置种出一批不可用的分类。

legacy 迁移则是典型的失败安全范本（u2-l3 讲过 `MigrateLegacyExtraCategories` 的写法），守卫前置（已有顶层 `Categories` 或没有 `Extra` 直接返回 false）、搬完即删键、返回是否变更让调用方决定持久化。

#### 4.1.3 源码精读

先看模型定义，见 [pkg/base/model.go:L434-L442](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L434-L442)——这就是 `DownloadCategory` 结构体本体，五个业务字段加一个软删除标记。

配置字段挂在 `DownloaderStoreConfig` 上，见 [pkg/base/model.go:L229-L230](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L229-L230)：`Categories` 是分类列表，`AutoCategorize` 是 `*bool` 开关（注释明确「unset 默认禁用」）。

内置分类的种子逻辑在 `Init()` 中，见 [pkg/base/model.go:L267-L276](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L267-L276)：仅当列表为空且 `DownloadDir` 非空时，按 `builtinSeedCategory` 种入四个分类。

内置扩展名表见 [pkg/base/model.go:L280-L287](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L280-L287)——注释强调它「镜像 Flutter 侧的 `kDefaultCategoryExtensions`，两边必须同步」。例如 `categoryProgram` 对应 `exe/msi/msix/apk/dmg/deb/rpm/pkg/appimage` 九种安装包格式。

`builtinSeedCategory` 构造器见 [pkg/base/model.go:L289-L297](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L289-L297)：`Path` 取 `filepath.Join(downloadDir, name)`，`Extensions` 做一次拷贝防止共享底层数组被外部修改。

legacy 迁移见 [pkg/base/model.go:L299-L337](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L299-L337)：JSON 序列化-反序列化往返把 `extra.downloadCategories`（`any` 类型）转成 `[]*DownloadCategory`；内置条目（`NameKey` 命中且扩展名列表为空）补回默认扩展名；全部搬完后 `delete(cfg.Extra, "downloadCategories")` 清除旧键。

三态开关的判定见 [pkg/base/model.go:L339-L344](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L339-L344)：`AutoCategorize != nil && *cfg.AutoCategorize`——nil 指针（从未保存过该字段的老配置）与显式 false 同样视为关闭。

`Merge`（部分更新）对这两个字段的处理见 [pkg/base/model.go:L380-L385](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L380-L385)：提交方未带 `Categories`/`AutoCategorize` 时保留旧值，与 u2-l3 讲的字段级合并规则一致。

#### 4.1.4 代码实践

**实践目标**：亲手验证「种子只在空列表时发生」与「legacy 迁移的搬运与补全」。

1. 操作步骤：
   - 在仓库根目录运行 `go test ./pkg/download/ -run 'TestDownloader_SetupMigratesLegacyExtraCategories' -v`，这个测试（[pkg/download/category_test.go:L609](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category_test.go#L609) 起）完整复现了迁移路径。
   - 再写一个独立的迷你测试（示例代码，可放于自己的练习目录，勿提交进仓库）：

     ```go
     cfg := &base.DownloaderStoreConfig{DownloadDir: "/tmp/dl"}
     cfg.Init()
     fmt.Println(len(cfg.Categories)) // 4：Music/Video/Document/Program

     cfg2 := &base.DownloaderStoreConfig{DownloadDir: "/tmp/dl"}
     cfg2.Init()
     cfg2.Categories[0].IsDeleted = true
     cfg2.Init() // 第二次 Init 不会重新种入
     fmt.Println(cfg2.Categories[0].IsDeleted) // 仍为 true
     ```
2. 需要观察的现象：第二次 `Init` 后被软删除的分类没有被「复活」，说明种子条件 `len(cfg.Categories) == 0` 是幂等守卫。
3. 预期结果：两段输出分别为 `4` 与 `true`。（示例代码，请以本地实际运行为准。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `IsDeleted` 用软删除而不是直接从切片里删掉元素？

**答案**：分类列表由 Flutter 端编辑并整体提交。如果后端物理删除内置分类，下次 UI 加载时会认为「内置分类不存在」而重新种入，用户的删除操作永远无法生效；软删除标记让删除意图跨会话保留，同时匹配逻辑（4.2 的 `matchDownloadCategory`）只需跳过该条目。

**练习 2**：一个 v2.0.0-beta 升级上来的配置里，内置分类 `Video` 的 `Extensions` 是空的，迁移后它还能匹配 `mp4` 吗？

**答案**：能。`MigrateLegacyExtraCategories` 对 `NameKey` 命中内置扩展名表且扩展名列表为空的条目，会按 `builtinCategoryExtensions[category.NameKey]` 补回默认列表（见 [pkg/base/model.go:L326-L328](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L326-L328)）。而用户自建文件夹（`NameKey` 为空）保持空列表，直到用户在设置里编辑。

---

### 4.2 categoryForResource：匹配规则

#### 4.2.1 概念说明

`categoryForResource` 回答一个问题：「这个已解析的资源应该路由到哪个分类？」它是纯函数（不写任何状态），被两个消费方共享：目录决策（4.3）与建议目录（4.4）。

三条硬规则决定了它的行为：

1. **开关与列表前置检查**：`AutoCategorize` 关闭或 `Categories` 为空，直接返回 nil。
2. **只有单文件资源参与路由**：条件是 `res.Name == "" && len(res.Files) == 1`。文件夹资源（BT 种子、FTP 目录）保持自身目录布局，绝不逐文件匹配——即使后来只选中了其中一个文件（u2-l1 讲过 `SelectFiles`），因为决策发生在选文件之后的 `prepareTask` 时刻，但资源形态（有无根名）不会因选择而改变。
3. **扩展名精确匹配**：取文件扩展名（小写、去点），在分类列表中按顺序找第一个「已启用且扩展名列表包含它」的分类，first-match-wins。

#### 4.2.2 核心流程

```text
categoryForResource(cfg, res)
  ├─ 开关关 或 无分类 ──────────────► nil
  ├─ res == nil / res.Name != "" / 文件数 != 1 ► nil
  └─ matchDownloadCategory(cfg, res.Files[0].Name)
        ├─ fileExtension(name)
        │    反斜杠归一为斜杠 → path.Ext → asciiLower → 去掉前导点
        │    "dir.setup/Setup.EXE" → "exe"（目录段中的点不算扩展名）
        ├─ 无扩展名 ─────────────────► nil
        └─ 顺序遍历 cfg.Categories：
             跳过 nil / IsDeleted / Path 为空白（dartTrimSpace）的条目
             normalizeCategoryExtensions(Extensions) 含 ext 则返回该分类
```

匹配优先级即配置列表顺序：若两个分类都声明了 `zip`，排前者胜出——UI 端因此允许通过拖拽排序来表达优先级。

归一化三件套是本模块最「较真」的部分，全部为了与 Dart 侧逐字节一致：

- `asciiLower`：只小写 ASCII 字母。Go 的 `unicode.ToLower` 与 Dart 的 `toLowerCase` 在某些码位上结果不同，若各用各的，同一个土耳其语字符文件名会在两端匹配到不同分类。
- `dartTrimSpace`：复刻 Dart `String.trim()` 的空白集（`unicode.IsSpace` 加 BOM `0xFEFF`），比 Go 的版本宽。
- `isECMAScriptSpace`：复刻 JS 正则的 `\s` 空白类，用于切分扩展名列表——UI 端就是用这个类 split 的。

#### 4.2.3 源码精读

入口函数见 [pkg/download/category.go:L17-L25](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category.go#L17-L25)：注释明确「仅单文件资源被路由；文件夹与多文件资源保持目录布局，绝不逐文件匹配，哪怕只选中其中一个」。三个前置短路后把 `res.Files[0].Name` 交给 `matchDownloadCategory`。

核心匹配循环见 [pkg/download/category.go:L122-L138](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category.go#L122-L138)：先取扩展名（空则不匹配），再顺序遍历分类，跳过 `nil`、`IsDeleted`、路径空白的条目，命中即返回；空扩展名列表永不匹配。

扩展名列表归一化见 [pkg/download/category.go:L144-L163](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category.go#L144-L163)：`normalizeCategoryExtensions` 按逗号与 ECMAScript 空白切分、trim、小写、去前导点、按输入顺序去重——所以存储值 `"exe, msi"` 与 `["exe","msi"]` 行为完全一致。

扩展名提取见 [pkg/download/category.go:L169-L172](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category.go#L169-L172)：`fileExtension` 先把 Windows 分隔符替换为 `/` 再取 `path.Ext`，保证「目录段中的点」不会误当扩展名。

Dart 语义复刻的三件套见 [pkg/download/category.go:L178-L193](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category.go#L178-L193)：`dartTrimSpace` 与 `isECMAScriptSpace` 的注释都点明了动机——两侧语言的字符表不一致会导致同一文件选不同分类，所以后端照抄 UI 的语义。`asciiLower` 见 [pkg/download/category.go:L99-L117](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category.go#L99-L117)。

#### 4.2.4 代码实践

**实践目标**：验证归一化规则与「重命名不参与匹配」。

1. 操作步骤：
   - 运行现成测试：`go test ./pkg/download/ -run 'TestNormalizeCategoryExtensions|TestSharedCategoryMatchingRules' -v`，分别见 [pkg/download/category_test.go:L326](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category_test.go#L326) 与 [pkg/download/category_test.go:L712](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category_test.go#L712)。
   - 阅读集成测试 `TestDownloader_CreateDirectDoesNotMatchTheRename`（[pkg/download/category_test.go:L447-L473](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category_test.go#L447-L473)）：服务器返回的文件名是 `setup.data`，但 `opts.Name` 改成 `renamed.exe`，且分类表只含 `exe`。
2. 需要观察的现象：测试断言任务最终停在基础目录 `downloadDir`、产物是 `renamed.exe`——说明分类匹配用的是**资源解析出的文件名**（`.data`），而不是用户重命名（`.exe`）。
3. 预期结果：测试全部通过；若想复现，把分类扩展名换成 `data` 再跑 `TestDownloader_CreateDirectAutoCategorize`（[pkg/download/category_test.go:L421-L445](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category_test.go#L421-L445)），文件就会落入分类目录。

#### 4.2.5 小练习与答案

**练习 1**：资源是 BT 种子解析出的文件夹（`res.Name = "bundle"`，含 `setup.exe` 与 `readme.txt` 两个文件），分类表含 `exe`。任务会被路由到 Program 目录吗？

**答案**：不会。`categoryForResource` 要求 `res.Name == ""`，文件夹资源在第一道短路就被拒绝；测试用例「a multi file resource keeps its layout」（[pkg/download/category_test.go:L136-L140](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category_test.go#L136-L140)）与「a single file inside a folder resource is not routed」（L147-L152）覆盖了这两种形态。

**练习 2**：分类配置存储为 `Extensions: []string{".MP3, flac "}`，一个 `song.MP3` 会被路由吗？

**答案**：会。`normalizeCategoryExtensions` 把每项按逗号/空白切分、去前导点并小写，实际参与匹配的是 `["mp3", "flac"]`，`song.MP3` 的扩展名经 `asciiLower` 也是 `mp3`，精确命中。

**练习 3**：为什么 `matchDownloadCategory` 要跳过 `Path` 为空白的分类，而不是用配置校验在写入时就拒绝？

**答案**：分类列表由 UI 整体提交，后端 `PutConfig` 无法保证每条分类都有合法路径（用户可能新建了分类还没填目录）；跳过无效条目让「半成品配置」不至于让整个功能失效，而白名单回退（4.4）进一步兜住「路径存在但被拒」的情形。

---

### 4.3 decideDownloadDirForTask 与分组目录

#### 4.3.1 概念说明

目录决策被刻意拆成两次：

- **建议目录（`suggestedDownloadDir`）**：`Resolve` 阶段计算，填进 `ResolveResult.SuggestedDownloadDir` 返回给 UI——「如果你现在创建，文件会去这里」。它只读不写，不展开占位符失败就返回空串。
- **最终目录（`decideDownloadDirForTask`）**：建任务后的 `prepareTask` 阶段计算，真正写进 `task.Meta.Opts.Path`。两次调用之间用户可能改配置、改选项，所以最终目录**永远重新决策**，不信任建议。

`decideDownloadDirForTask` 是本轮（1e36599 任务分组）改造的核心：新增第三个参数 `group *TaskGroup`。分组任务有一条专属快速路径——**跳过自动分类，直接使用批量创建时预留的组目录**，且组路径在创建时已经定型，这里只做包含性校验与白名单校验，不再展开占位符（组目录名可能字面含有 `%year%`，二次展开会让校验与实际目录错位，测试用例「literal group name」专门覆盖这一点）。

旁路机制有两个，注意它们都是 `Options` 字段（[pkg/base/model.go:L148-L151](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L148-L151)）：

- `SkipAutoCategorize`：跳过分类路由，任务留在基础目录（自己的 `Path` 或全局 `DownloadDir`）。
- `AsDefaultPath`：创建成功后把本次目录记为全局默认下载目录。但它有个反直觉的联动：`prepareDefaultPath`（[pkg/download/downloader.go:L1802-L1815](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1802-L1815)）要求**同时**满足 `SkipAutoCategorize && AsDefaultPath` 才生效——注释写明理由：「只有用户显式点名的目录才能成为全局默认；一个沿用分类决策的任务，它的目录不属于用户，后端不能把它变成全局默认」。测试 `TestDownloader_CreateWithOptionsKeepsTheDefaultDirectory`（[pkg/download/category_test.go:L531-L559](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category_test.go#L531-L559)）验证了这一点。

#### 4.3.2 核心流程

```text
decideDownloadDirForTask(opts, res, group)
  │
  ├─ base = trim(opts.Path)；空则取 cfg.DownloadDir
  ├─ base 仍为空 → 报错（拒绝回落到进程工作目录）
  │
  ├─【分组分支】group != nil 且 opts.SkipAutoCategorize
  │     └─ initDownloadPathForTask(base, group)
  │          组路径已定型：只做 Rel 包含性检查 + 组根白名单校验
  │
  └─【普通路径】target = base
        ├─ 未设 SkipAutoCategorize 且 categoryForResource 命中
        │     → target = trim(category.Path)
        ├─ dir, err = initDownloadPath(target)   // 展开占位符 + 白名单
        │     err 且 target != base
        │       → 警告日志 + 回退 initDownloadPath(base)
        └─ 返回 dir
```

分组路径的「预先改写」发生在批量创建时（见 4.3.3 第 4 段）：成员任务的 `opts.Path` 已被改写为组目录内的相对拼接结果、且被强制 `SkipAutoCategorize = true`，所以走到 `prepareTask` 时命中上面的分组分支——注意条件是 `group != nil && opts.SkipAutoCategorize`，两个条件都由批量入口保证。

#### 4.3.3 源码精读

目录决策本体见 [pkg/download/category.go:L38-L73](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category.go#L38-L73)：基础目录三级兜底（opts → 配置 → 报错）、分组快速路径（L55-L57）、分类路由（L59-L63）、白名单失败回退（L64-L68）。旧签名 `decideDownloadDir` 只是传 `nil` group 的薄包装，见 [pkg/download/category.go:L34-L36](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category.go#L34-L36)。

建议目录见 [pkg/download/category.go:L79-L93](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category.go#L79-L93)：与创建决策共享 `categoryForResource` 与 `initDownloadPath`，但任何一步出错只返回空串（不提示），注释强调「只报告 decideDownloadDir 的决定，不展开目录、不写任何东西」。`ResolveResult` 上的字段注释见 [pkg/download/model.go:L22-L30](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L22-L30)——「它是给 UI 的提示，create 调用决定最终路径」。

建议目录的两个填充点都在 `Resolve` 里：扩展短路产出资源时见 [pkg/download/downloader.go:L573-L577](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L573-L577)，协议 Fetcher 解析后见 [pkg/download/downloader.go:L592-L596](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L592-L596)。两条路径都会调用 `d.suggestedDownloadDir(...)`。

最终目录的调用点是 `prepareTask`，见 [pkg/download/downloader.go:L2312-L2324](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2312-L2324)：先深拷贝 `Meta`（决策不污染原始元数据，失败重试还能重来），再 `decideDownloadDirForTask(preparedMeta.Opts, preparedMeta.Res, task.group)`，把结果写进 `preparedMeta.Opts.Path`。分组任务的 `task.group` 是运行时从 `ParentID` 挂接的指针（u4-l6 的双轨制）。已准备过的任务不再决策目录，但每次启动仍要过一次白名单，见 [pkg/download/downloader.go:L2429-L2448](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2429-L2448)（`else if` 分支调用 `initDownloadPathForTask` 复检）。

批量创建时对成员路径的预先改写见 [pkg/download/downloader.go:L707-L729](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L707-L729)：成员显式给了 `opts.Path` 时，先展开占位符、相对路径拼到组父目录、`filepath.Rel` 求相对位置并要求 `filepath.IsLocal`（挡住 `..` 逃逸）；然后统一 `opts.Path = filepath.Join(group.Path, relative)` 并**强制 `SkipAutoCategorize = true`**——这一行就是「分组任务绕过分类」的第一现场。批量请求模型 `CreateTaskBatch` 的 `Group/GroupName` 字段见 [pkg/base/model.go:L202-L207](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L202-L207)。

组目录的预留发生在任何成员任务创建之前，见 `createTaskGroup` [pkg/download/downloader.go:L757-L826](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L757-L826)：父目录走 `initOptions` 补默认值并展开占位符；组名清洗（拒绝路径分隔符、Windows 非法字符、结尾点、`.`/`..`）；去重循环从无后缀到 ` (1000)` 依次尝试 `os.Mkdir`，每个候选都先过白名单（`isDownloadPathAllowed(candidate)`），并在内存 `d.groups` 中查重；成功后写 `task_group` 桶、登记 `creatingGroups`。`TaskGroup` 模型本体见 [pkg/download/model.go:L32-L38](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L32-L38)。

分组快速路径的实现见 [pkg/download/downloader.go:L2270-L2284](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2270-L2284)：`initDownloadPathForTask` 对普通任务（`group == nil`）退化为 `initDownloadPath`；对分组任务用 `filepath.Rel(group.Path, path)` 加 `filepath.IsLocal` 做包含性检查（成员必须在组目录内），再校验**组根**的白名单——注意校验的是 `group.Path` 而非成员路径本身，因为组根在预留时已逐候选验证过，成员路径只需证明没跑出组外。

#### 4.3.4 代码实践

**实践目标**：观察「分组任务绕过分类、直接落入预留组目录」的完整链路。

1. 操作步骤：
   - 阅读 [pkg/download/category_test.go:L237-L267](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category_test.go#L237-L267) 的 `TestDecideGroupDownloadDirWhiteList`，六个子用例分别覆盖：组根、组内嵌套目录、普通嵌套任务（要报错）、组外成员（要报错）、组权限被收回（要报错）、字面 `%year%` 组名。
   - 用 Boot API 风格写一个最小程序（示例代码）：配置 `AutoCategorize=true`、给 `mp4` 配一个分类目录，然后用 `CreateDirectBatchWithResult` 提交 `Group: true, GroupName: "album"`、成员为两个 `mp4` 的批量（或直接对运行中的服务 `POST /api/v1/tasks/batch`，请求格式见 `_docs/task-batch-api.md`）。
2. 需要观察的现象：返回的 `parentId` 非空；两个成员任务的 `meta.opts.path` 都等于 `<父目录>/album`，而不是分类目录；磁盘上出现预留的 `album` 目录；即使批量成员中混入一个 `exe`，它也留在组目录。
3. 预期结果：组内所有文件落在组目录；若再删光组内任务，空组目录与 `task_group` 元数据被清理（u4-l6 讲过 `cleanupEmptyGroups`）。若在 CI 环境外手工运行，请以本地实际输出为准（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么分组分支要求 `opts.SkipAutoCategorize` 为真，而不是只判断 `group != nil`？

**答案**：批量入口在改写成员路径时已统一强制 `SkipAutoCategorize = true`（[pkg/download/downloader.go:L728](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L728)），所以正常分组任务必然满足；但函数是防御式编写的——若未来有调用方带着 group 却没设跳过标志，条件不满足时会走普通分类路径，分类可能把成员路由到组目录之外，破坏「组的文件都在一个目录里」的承诺。显式双条件让这个前提在代码层面可见。

**练习 2**：`suggestedDownloadDir` 与 `decideDownloadDir` 之间用户把 `AutoCategorize` 关掉了，创建任务会发生什么？

**答案**：最终目录按当时的配置重新决策——开关关闭后 `categoryForResource` 返回 nil，任务落在基础目录；建议目录只是历史快照，不影响结果。这正是「提示与决策分离」的意义。

**练习 3**：分组任务路径校验为什么不再展开占位符（`initDownloadPathForTask` 的注释说 "without expanding placeholders again"）？

**答案**：组根目录在 `createTaskGroup` 预留时已经展开过占位符并真实建了目录（[pkg/download/downloader.go:L762](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L762)）；成员路径是相对组根拼接的。若此时二次展开，字面名为 `%year%` 的组目录会被替换成 `2026`，导致校验的路径与磁盘实际目录错位——测试用例「literal group name」专门守护这一行为。

---

### 4.4 decideDownloadDir 白名单

#### 4.4.1 概念说明

白名单（`WhiteDownloadDirs`）是**进程级安全边界**：它不存于用户配置，而由启动方（`cmd/web`/`cmd/api` 的命令行参数、Docker 环境等）注入 `DownloaderConfig`，用于服务端部署场景——管理员声明「下载只允许落在这些目录」，任何来自 API 的任务目录、分类目录、组目录都必须通过它。

要点：

- **未配置即全放行**：`len(WhiteDownloadDirs) == 0` 时 `isDownloadPathAllowed` 恒真，桌面端因此不受影响。
- **glob 模式匹配**：每项用 `filepath.Match` 比较，支持 `*` 通配——`/data/*` 允许 `/data` 下的一层子目录。
- **校验时机在占位符展开之后**：`initDownloadPath` 先 `ReplacePathPlaceholders` 再校验，所以 `%year%` 目录按展开后的真实路径判定。
- **失败安全分级**：分类目录被拒 → 回退基础目录（任务仍可创建，只损失分类功能）；基础目录本身被拒 → 直接报错（没有更底层的安全目录可退）。

#### 4.4.2 核心流程

```text
initDownloadPath(path)
  ├─ path = ReplacePathPlaceholders(path)   // %year%/%month%/%day%/%date%
  └─ isDownloadPathAllowed(path)?
        是 → 返回 path
        否 → 错误 "download directory is not in white list"

isDownloadPathAllowed(path)
  ├─ WhiteDownloadDirs 为空 → true（未启用）
  └─ 任一 pattern 满足 filepath.Match(pattern, path) → true

消费侧的错误处理：
  decideDownloadDirForTask：分类目录 err 且 target != base → 回退 base 再试
  suggestedDownloadDir：err → 返回 ""（宁可不提示，也不提示一个到不了的目录）
  prepareTask：最终 err 直接返回 → 任务带着 NeedsPreparation 标记失败，可重试
```

#### 4.4.3 源码精读

白名单实现见 [pkg/download/downloader.go:L2261-L2268](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2261-L2268)（`initDownloadPath`：展开占位符 + 校验）与 [pkg/download/downloader.go:L2286-L2298](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2286-L2298)（`isDownloadPathAllowed`：空列表放行、逐模式 `filepath.Match`）。字段定义见 [pkg/download/model.go:L239](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L239)，位于 `DownloaderConfig`（进程级、不落盘）而非 `DownloaderStoreConfig`。

「分类目录被拒回退基础目录」见 [pkg/download/category.go:L64-L68](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category.go#L64-L68)：先打 Warn 日志说明回退原因，再对 `base` 走一次 `initDownloadPath`——若基础目录也被拒，err 向上传播，`prepareTask` 返回错误，任务保持 `NeedsPreparation` 待重试。

建议目录侧的静默处理见 [pkg/download/category.go:L86-L92](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category.go#L86-L92)：分类命中但目录过不了白名单时返回空串。配套测试见 [pkg/download/category_test.go:L310-L323](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category_test.go#L310-L323)。

白名单三分支的完整测试见 [pkg/download/category_test.go:L179-L235](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category_test.go#L179-L235)：分类目录被拒回退基础目录（L191-L202）、基础目录本身被拒报错（L204-L212）、分类路径的等价拼写（`/dir/.`）仍回退到允许写法（L214-L223）、空目录报错（L225-L234）。

`AsDefaultPath` 与白名单的联动见 [pkg/download/downloader.go:L1802-L1815](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1802-L1815)：候选默认目录也要先过 `initDownloadPath`（含白名单），被拒则不落盘；随后的 `persistDefaultPathBestEffort`（[pkg/download/downloader.go:L1817-L1824](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1817-L1824)）以「尽力而为」的方式写配置，失败只记 Warn 不影响任务。

组目录预留时的白名单校验见 [pkg/download/downloader.go:L790-L792](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L790-L792)：每个去重候选（含 ` (1)`、` (2)` 后缀）都要逐个过 `isDownloadPathAllowed`，任何一个被拒则整个批量直接失败——组是原子的，不能出现「组目录建在白名单外」的半成品。

#### 4.4.4 代码实践

**实践目标**：验证白名单的三种失败形态与回退顺序。

1. 操作步骤：
   - 运行 `go test ./pkg/download/ -run 'TestDecideDownloadDirWhiteList|TestDecideGroupDownloadDirWhiteList' -v`。
   - 想手动复现（示例代码，参考测试 helper [pkg/download/category_test.go:L47-L51](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category_test.go#L47-L51) 的 `newUnitCategoryDownloader`）：

     ```go
     d := NewDownloader(nil)
     d.cfg.DownloaderStoreConfig = newTestCategoryConfig() // 分类含 exe → ./downloads/Program
     d.cfg.WhiteDownloadDirs = []string{"./downloads"}     // 只放行基础目录

     // ① 分类目录 ./downloads/Program 不在白名单 → 回退 ./downloads
     dir, err := d.decideDownloadDir(&base.Options{Path: "./downloads"}, singleFileResource("setup.exe"))
     // ② 基础目录本身被拒 → err 非 nil
     _, err = d.decideDownloadDir(&base.Options{Path: "./elsewhere", SkipAutoCategorize: true}, singleFileResource("setup.exe"))
     ```
2. 需要观察的现象：① 中 `dir` 等于 `./downloads` 且 `err == nil`（回退成功，分类只是被跳过）；② 中 `err` 为「download directory is not in white list」。
3. 预期结果：与测试断言一致；若把白名单改成 `[]string{"./downloads/*"}`，① 会直接路由到 `./downloads/Program`，因为 `*` 匹配了一层子目录。（示例代码，待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：为什么分类目录被拒选择「回退」而基础目录被拒选择「报错」？

**答案**：白名单要保护的是「用户/管理员显式声明的目录」。分类目录是后端推导的（用户没有逐任务点名），被拒只损失分类功能，回退到任务的基础目录既安全又不影响下载；基础目录若也被拒，说明任务的显式意图本身就违反安全策略，没有更底层的安全目录可退，只能失败并保留 `NeedsPreparation` 供修正后重试。

**练习 2**：`WhiteDownloadDirs` 配了 `/downloads/*`，任务目录 `/downloads/Program/Setup.exe`（注意是文件全路径）能通过校验吗？

**答案**：不能。`filepath.Match("/downloads/*", "/downloads/Program/Setup.exe")` 中 `*` 不跨路径分隔符，两个路径段数不同直接不匹配。而且校验对象本来就不是文件全路径——`decideDownloadDirForTask` 只对**目录**调用 `initDownloadPath`，文件名是在其后由 `FolderPath/SingleFilepath` 拼出来的。要放行两层需写 `/downloads/*/*`。

**练习 3**：占位符目录 `/downloads/%year%` 与白名单 `/downloads/2026`，任务会落在哪里？

**答案**：`initDownloadPath` 先展开 `%year%` 得到 `/downloads/2026`，再过白名单——恰好匹配则放行，任务正常落入；若白名单写的是 `/downloads/*` 同样匹配。若校验先于展开，`/downloads/%year%` 与任何日期模式都无法匹配，这就是「先展开后校验」的原因（见 [pkg/download/downloader.go:L2262-L2266](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2262-L2266)）。

---

## 5. 综合实践

把本讲四条主线串成一个完整的验证实验。准备：本地编译 CLI 或 `go run` 一个使用 `NewDownloader` 的程序（参考 u3-l4 的自定义 Downloader 构造方式），存储用 `MemStorage` 即可。

1. **配置两个自定义分类**：通过 `PutConfig` 写入 `AutoCategorize=true`、`DownloadDir=/tmp/dl`，分类表为 `Audio(/tmp/dl/Audio, [flac,wav])` 与 `Video(/tmp/dl/Video, [mkv])`——两个目录都不预先创建。
2. **验证建议目录**：对一个返回 `song.flac` 的 HTTP 地址调用 `Resolve`（可用 `internal/test` 的 httptest 服务器，或任一公网直链），确认 `ResolveResult.SuggestedDownloadDir == "/tmp/dl/Audio"`。
3. **验证最终目录**：接 `CreateWithOptions(rr.ID, &base.Options{Path: "/tmp/dl"})`，等任务 done 后检查 `task.Meta.Opts.Path == "/tmp/dl/Audio"` 且文件确实写在那里——两次决策独立计算却结论一致。
4. **验证 SkipAutoCategorize**：再次下载 `song.flac`，这次带 `SkipAutoCategorize: true, Path: "/tmp/keep"`，确认文件落在 `/tmp/keep`。
5. **验证 AsDefaultPath 的守卫**：再下载一次带 `AsDefaultPath: true` 但**不带** `SkipAutoCategorize`，任务落入 Audio 目录后检查 `GetConfig().DownloadDir`——它必须仍是 `/tmp/dl`，没有被分类目录「劫持」为全局默认。
6. **验证分组旁路**：调用 `CreateDirectBatchWithResult(&base.CreateTaskBatch{Opts: &base.Options{Path: "/tmp/dl"}, Group: true, GroupName: "album", Reqs: ...})`，成员混入 `a.flac` 与 `b.m4a`（后缀不在任何分类），确认所有成员都落在 `/tmp/dl/album/`，返回的 `parentId` 非空；再给白名单设 `[]string{"/tmp/dl"}` 重试一次，观察 `createTaskGroup` 因候选目录 `/tmp/dl/album` 不在白名单而整个批量报错。
7. **收尾**：删光 album 组的任务，确认空组目录与 `task_group` 桶中的元数据被清理（对应 u4-l6 的 `cleanupEmptyGroups`）。

第 2-5 步可参照现成测试 [pkg/download/category_test.go:L503-L529](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category_test.go#L503-L529) 的写法；第 6 步可参照 `_docs/task-batch-api.md` 的请求格式改用 REST 调用。全部步骤未在本环境实际运行，标注为待本地验证。

## 6. 本讲小结

- **模型与配置**：`DownloadCategory` 是「扩展名列表 → 目录」的映射，支持内置分类（`NameKey` + 软删除）与用户分类；`AutoCategorize` 用 `*bool` 三态表达「未设置即关闭」，内置分类只在空列表且目录非空时由 `Init` 种入，legacy 的 `extra.downloadCategories` 由 `MigrateLegacyExtraCategories` 在 `Setup` 中先迁移再初始化。
- **匹配规则**：`categoryForResource` 只路由「单文件资源」（`res.Name == ""` 且恰好一个文件），扩展名经 `asciiLower`/`fileExtension` 归一化后与 `normalizeCategoryExtensions` 的产物精确比较，first-match-wins；三件 Dart 语义复刻（`asciiLower`、`dartTrimSpace`、`isECMAScriptSpace`）保证前后端对同一文件给出同一答案，且重命名（`opts.Name`）不参与匹配。
- **两次分离的决策**：`suggestedDownloadDir` 在 `Resolve` 时给 UI 提示（只读、出错即空串），`decideDownloadDirForTask` 在 `prepareTask` 时真正定目录（写进 `Opts.Path`），最终目录永远按当时配置重新决策。
- **分组旁路**：批量入口改写成员路径进组目录并强制 `SkipAutoCategorize`，`decideDownloadDirForTask` 的分组分支跳过分类、只做组内包含性与组根白名单校验，且不再二次展开占位符（守护字面 `%year%` 组名）；组根在 `createTaskGroup` 预留时已逐候选过白名单并真实建目录。
- **白名单**：`WhiteDownloadDirs` 是进程级注入的安全边界，空即放行、`filepath.Match` glob 匹配、校验在占位符展开之后；失败安全分级——分类目录被拒回退基础目录，基础目录被拒才报错（任务保持 `NeedsPreparation` 可重试）。
- **AsDefaultPath 守卫**：只有 `SkipAutoCategorize && AsDefaultPath` 同时成立，用户显式点名的目录才会成为全局默认，分类目录永远不会劫持全局配置。

## 7. 下一步学习建议

- 下一讲 u10-l2「归档自动解压与多卷处理」承接「下载完成之后」的下游链路：本讲的分类目录是文件落地的终点，而 `AutoExtract` 在 done 之后接手，把文件解压到同类目录体系。
- 回看 u4-l6「任务分组与批量创建」补全分组上下文：本讲只覆盖目录侧（`createTaskGroup` 预留与成员改写），批量语义（rid 优先、部分成功、空组清理）在该讲展开。
- 想深入白名单的生产来源，读 `cmd/web/flags.go` 与 `docker-compose.yml` 中下载目录相关参数，看服务端部署如何把 `WhiteDownloadDirs` 注入 `DownloaderConfig`（u8-l6 部署形态讲过入口差异）。
- 建议顺手阅读 [pkg/download/category_test.go](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/category_test.go) 全文：它同时示范了表驱动单测（决策矩阵）与 httptest 集成测试（全链路断言）两种风格，是给本模块补测试的最佳模板。
