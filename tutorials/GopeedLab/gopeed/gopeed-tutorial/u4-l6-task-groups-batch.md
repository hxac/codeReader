# 任务分组与批量创建：TaskGroup 与批量 API

## 1. 本讲目标

提交 `1e36599`（feat(tasks): group extension folder downloads）为 Gopeed 引入了「任务分组」机制：一次批量请求可以把多个独立调度的任务收进一个**预先预留好的目录**，并以组为单位做暂停、继续和删除。学完本讲，你应该能够：

1. 解释 `TaskGroup` 模型、`task_group` 存储桶与 `Task.ParentID` 三者的对应关系，以及「落盘权威 + 运行时缓存」的双轨制设计。
2. 掌握 `CreateDirectBatchWithResult` 的三条批量规则：rid 优先、成员 opts 整体替换、部分成功。
3. 理解 `createTaskGroup` 如何在**任何任务创建之前**完成组名校验、白名单校验、目录预留与去重后缀。
4. 说明 Pause/Continue/Delete 中组 ID 如何经 `expandGroupTaskIDs` 展开到成员任务，以及 `cleanupEmptyGroups` / `cleanupTaskGroups` 在什么时机清掉空组。

## 2. 前置知识

本讲建立在三篇前置讲义之上，先复习它们的结论，再引入新概念。

**复习一：Task 与双轨制分组（u2-l2）。** `pkg/download.Task` 的公开字段 `ParentID` 会被持久化，是分组的**权威记录**；而私有字段 `group *TaskGroup` 只是运行时缓存指针，JSON 序列化即丢失，重启恢复时由 `Setup` 重新挂接。`MarshalJSON` 在展示层动态追加 `name` 与 `group` 字段。

**复习二：主链路与 rid 缓存（u4-l2）。** `Resolve` 会把 Fetcher 实例（而非 Resource）以 `rrId` 为键缓存进 `fetcherCache`；`createResolved` 以「取出即删除」的方式保证一个 rid 只有单一创建者，创建失败时归还缓存。批量接口里的 `rid` 指的就是这个缓存键。

**复习三：TaskFilter 批量操作（u4-l4）。** Pause/Continue/Delete 共享 `TaskFilter{IDs, Statuses, NotStatuses}` 过滤模型；Delete 是三段式——锁内摘除并登记 `deletingTasks`、锁外 `doDelete`、失败时回写 `bucketTask` 恢复。

**新概念：**

- **任务分组（TaskGroup）**：一组「独立调度、共享目录」的任务。组不是任务——它不出现在任务列表里，只是一条目录元数据；组内每个任务各自有自己的状态机、并发槽位与断点记录。
- **目录预留（reservation）**：普通任务的下载目录是在首次启动时的 `prepareTask` 一次性决策的（u4-l2）；而分组的目录必须在**批量创建的最开始**就确定并在文件系统上 `Mkdir` 占位，这样成员任务的路径才能全部落在这个目录之内。
- **部分成功（partial success）**：批量创建逐个成员尝试，只要有一个任务成功落盘，整个请求就算成功；全部失败才报错并回收预留的空组。
- **路径包含性检查（containment check）**：成员任务显式指定的路径必须能折算成组目录**内部**的相对路径，禁止「身在组里、文件却写到组外」。
- **白名单（white list）**：`DownloaderConfig.WhiteDownloadDirs` 是一组 glob 模式，配置之后所有下载目录（包括组目录）都必须命中其中之一；未配置时全部放行。

一个值得先建立的直觉：**组目录的去重发生在目录层面，任务文件的重命名发生在任务层面**。组目录 `album` 已存在时，新组会依次尝试 `album (1)`、`album (2)`……直到 `album (1000)`；而组内任务文件的重名仍由 `prepareTask` 的 `CheckDuplicateAndRename` 处理，两套机制互不干扰。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pkg/base/model.go` | `CreateTaskBatch` / `CreateTaskBatchItem` 批量请求模型 |
| `pkg/download/model.go` | `TaskGroup`、`CreateTaskBatchResult`、`Task.ParentID` 与 `group` 指针 |
| `pkg/download/downloader.go` | 批量创建、组目录预留、组 ID 展开、空组清理（本讲主战场） |
| `pkg/download/category.go` | `decideDownloadDirForTask` 中分组任务旁路自动分类的分支 |
| `pkg/api/service.go` | `/api/v1/tasks/batch` 路由与 handler |
| `_docs/task-batch-api.md` | 批量 API 的行为契约文档（接口语义的"说明书"） |
| `pkg/download/downloader_test.go` | 分组与批量的行为测试（千行级，是理解语义的最佳旁证） |

## 4. 核心概念与源码讲解

### 4.1 TaskGroup 模型与存储桶

#### 4.1.1 概念说明

一次「扩展文件夹下载」的典型场景是：浏览器扩展发现页面上有 20 张图片，用户点了「全部下载」。这 20 个 URL 各自是独立的 HTTP 任务（独立解析、独立并发、独立断点），但用户期望它们落在**同一个文件夹**里，并且能被**一次性**暂停或删除。

如果只靠现有机制会有两个矛盾：

1. 20 个任务独立做目录决策（自动分类会把 `.jpg` 与 `.mp3` 路由到不同分类目录），无法保证同目录；
2. 即使强行传同一个 `opts.path`，也无法按「这一批」整体操作——任务过滤只认任务 ID。

`TaskGroup` 就是为解决这两个矛盾引入的独立实体：它只有四个字段——ID、名字、路径、创建时间。组本身**不是任务**（不会出现在任务列表里），成员任务通过 `ParentID` 单向引用它。存储上，组元数据独立放在第九个存储桶 `task_group` 里，与任务骨架（`task` 桶）解耦，删除某个任务不需要改动组记录，清理组也不需要遍历任务桶。

#### 4.1.2 核心流程

`TaskGroup` 的生命周期横跨三个阶段：

```text
启动恢复阶段（Setup）
  storage.List(bucketTaskGroup) ──► d.groups[id] = group     # 全量加载到内存 map
  storage.List(bucketTask)       ──► 逐个任务：
        task.group = d.groups[task.ParentID]                  # 重建运行时指针
  cleanupEmptyGroups(所有组 ID)                               # 清掉无成员的残留组

批量创建阶段（CreateDirectBatchWithResult）
  createTaskGroup ──► Mkdir 预留目录 ──► 写 task_group 桶 ──► d.groups + creatingGroups
  每个成员任务 ──► doCreate ──► task.ParentID = group.ID ──► 落盘 task 桶

批量操作阶段（Pause/Continue/Delete）
  组 ID ──► expandGroupTaskIDs ──► 成员任务 ID 列表 ──► 复用 TaskFilter 批量语义
  Delete 收尾 ──► cleanupTaskGroups ──► 删 task_group 桶记录 + 删内存 map + 清理目录
```

关键点：`ParentID` 与 `group` 指针是同一份关系的两种形态——前者随任务 JSON 序列化进 bbolt（权威、跨重启），后者只活在内存里（供 `decideDownloadDirForTask`、`MarshalJSON` 等运行时路径零成本读取）。u2-l2 把它总结为「双轨制」，本讲你会看到它在源码中的两处落点：恢复时挂接、创建时登记。

#### 4.1.3 源码精读

先看模型本身，注意 `TaskGroup` 的注释点明了它的本质——「被独立调度的任务共享的目录」：

[pkg/download/model.go:L32-L38](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L32-L38)：定义 `TaskGroup{ID, Name, Path, CreatedAt}` 四个字段；ID 复用 `NewTask().ID` 的 nanoid 生成器（只是借 ID，不代表组是任务）。

[pkg/download/downloader.go:L36-L44](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L36-L44)：存储桶常量里新增 `bucketTaskGroup = "task_group"`，紧跟 `bucketTask` 之后——分组的持久化与任务骨架平级。

[pkg/download/downloader.go:L122-L126](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L122-L126)：`Downloader` 上三个分组相关的内存态字段：`groups`（组元数据缓存）、`creatingGroups`（正在批量创建中的组 ID 集合）、`deletingTasks`（正在删除的任务）。它们与 `tasks` 共用 `d.lock` 保护。

[pkg/download/downloader.go:L186](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L186)：`Setup` 建桶时把 `bucketTaskGroup` 加进九个桶的列表。

[pkg/download/downloader.go:L230-L237](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L230-L237)：恢复流程第一步——从 `task_group` 桶 `List` 出全部组，装进 `d.groups`。注释强调「分组元数据独立于 task/fetcher 恢复记录」。

[pkg/download/downloader.go:L247-L267](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L247-L267)：恢复任务时 `task.group = d.groups[task.ParentID]` 把运行时指针挂回去；最后把所有组 ID 交给 `cleanupEmptyGroups` 清理（重启后组还在但成员全没了的残留场景）。

再看展示层如何把组信息带给前端：

[pkg/download/model.go:L161-L173](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L161-L173)：`Task.MarshalJSON` 用别名类型避免递归序列化，动态追加 `name` 与 `group` 字段——`group` 指针非空时，任务详情里就会带完整的组元数据。

批量 API 的响应模型也在这一文件里：

[pkg/download/model.go:L40-L44](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/model.go#L40-L44)：`CreateTaskBatchResult{TaskIDs, ParentID}`。普通批量省略 `ParentID`（`omitempty`），分组批量则必填——前端靠它知道「这批任务属于哪个组」。

[pkg/download/downloader.go:L2173-L2208](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2173-L2208)：`doCreate` 中 `task.ParentID = groupID` 在构建任务骨架时写入；随后在 `d.lock` 临界区内 `task.group = d.groups[groupID]`，若组已不存在直接返回 `ErrTaskNotFound`——这是一个防御性校验，正常流程中 `creatingGroups` 守护（见 4.4）会保证组不会被并发删除。

#### 4.1.4 代码实践

**实践目标**：验证「组元数据独立持久化、重启后不丢、组不是任务」三个断言。

1. **操作步骤**：阅读测试 `TestDownloader_TaskGroupMetadataAndRestore`（[pkg/download/downloader_test.go:L4070-L4110](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4070-L4110)），重点看三段：
   - L4078-4082：断言 `result.ParentID` 非空、`d.GetTask(result.ParentID) == nil`（组 ID 查不到任务）；
   - L4084-4092：断言每个成员 `task.ParentID == result.ParentID`、`task.group != nil`、`Meta.Opts.Path` 等于组目录，且 JSON 序列化结果包含 `"group":`；
   - L4094-4103：`Close()` 后用同一个 `storageDir` 重建 Downloader，断言成员任务的 `group.Path` / `group.Name` / `ParentID` 全部还在。
2. **运行方式**：`go test ./pkg/download/ -run 'TestDownloader_TaskGroupMetadataAndRestore' -v`。
3. **需要观察的现象**：测试通过；重建后组元数据来自 `task_group` 桶而非任务记录。
4. **预期结果**：PASS。该测试使用 `NewBoltStorage(storageDir)` 走真实的 bbolt 往返，等价于一次「进程重启」。
5. 下载完成时间等依赖真实网络的行为与本实践无关，此处无需「待本地验证」标注。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TaskGroup` 不存「成员任务 ID 列表」，而是让任务存 `ParentID`？

**答案**：单向引用让删除零成本——删除一个任务不需要改动组记录；清理空组时只需扫描 `d.tasks` 里是否还有任务引用该组（见 4.4.3 的 `cleanupTaskGroups`），避免维护双向关联的一致性问题。组记录因此可以做到极小且不可变（只有元数据）。

**练习 2**：`Task.MarshalJSON` 输出的 `group` 字段在什么情况下为空？

**答案**：`t.group` 指针为 `nil` 时（`omitempty`）。两种情况：任务不属于任何组；或属于组但运行时指针未挂接（例如旧版本落盘的任务没有 `ParentID`）。重启恢复路径 `task.group = d.groups[task.ParentID]` 会在 `Setup` 时把指针补上。

**练习 3**：组的 ID 和任务的 ID 用的是同一个生成器吗？这样安全吗？

**答案**：是，`createTaskGroup` 里 `group.ID` 直接用 `NewTask().ID`（nanoid）。安全，因为「组不是任务」——组 ID 永远不会进 `task` 桶，`GetTask(组ID)` 返回 nil；而在 `expandGroupTaskIDs` 的语义里，一个 ID 要么在任务列表里命中任务，要么作为 `ParentID` 命中组，两类 ID 混在同一个 `filter.IDs` 里也不会歧义。

### 4.2 CreateDirectBatchWithResult：rid 优先、opts 整体替换与部分成功

#### 4.2.1 概念说明

批量入口的请求模型在 `pkg/base/model.go`：

```go
type CreateTaskBatch struct {
    Reqs      []*CreateTaskBatchItem `json:"reqs"`
    Opts      *Options               `json:"opts"`      // 批级默认选项
    Group     bool                   `json:"group"`     // 是否建组
    GroupName string                 `json:"groupName"` // 组名（目录名）
}

type CreateTaskBatchItem struct {
    Rid  string   `json:"rid,omitempty"` // Resolve 返回的资源 ID
    Req  *Request `json:"req"`           // 直接请求
    Opts *Options `json:"opts"`          // 成员级选项
}
```

三条核心规则（与 `_docs/task-batch-api.md` 的契约一一对应）：

1. **rid 优先**：成员同时带 `rid` 和 `req` 时，`rid` 胜出，`req` 被忽略。rid 复用 `Resolve` 阶段的探测成果（已知的文件列表、大小、Range 支持），避免二次网络请求；`req` 则走延迟解析（`doStart` 时才 Resolve）。
2. **成员 opts 整体替换**：成员自带 `opts` 时**完全采用**成员的，不从批级 `opts` 继承任何字段；成员没有 `opts` 才用批级的。注意是「整体替换」而非「逐字段合并」——成员 `opts` 里没写 `path` 就是空路径，不会继承批级的 `path`。
3. **部分成功**：逐个成员尝试，一个成员失败（无效 rid、不支持协议、路径非法……）只记录错误继续下一个；只要有一个任务成功落盘，请求就成功返回。全部失败才返回聚合错误，并回收预留的空组。

为什么 opts 选择「整体替换」而不是合并？因为 `Options` 里存在无法合并的语义字段（`SelectFiles` 是下标集合、`Extra` 是协议私有结构），逐字段合并会让「成员想覆盖一个字段」与「成员想清空一个字段」无法区分。整体替换的规则简单到不需要文档解释副作用。

#### 4.2.2 核心流程

`CreateDirectBatchWithResult` 的骨架（省略错误处理）：

```text
输入: CreateTaskBatch{Reqs, Opts, Group, GroupName}

0. 校验 Reqs 非空
1. prepareDefaultPath(批级 Opts)            # AsDefaultPath 场景记录默认目录候选
2. if Group:
     group = createTaskGroup(GroupName, Opts.Path)   # 见 4.3
     result.ParentID = group.ID
     defer: delete(creatingGroups, group.ID); cleanupEmptyGroups([group.ID])
3. for item in Reqs:
     a. opts = item.Opts ?? 批级Opts ?? &Options{}    # 整体替换 + Clone 隔离
     b. if Group:
          解析成员相对路径 → 组内包含性检查            # 见下
          opts.Path = filepath.Join(group.Path, relative)
          opts.SkipAutoCategorize = true              # 分组任务旁路自动分类
     c. switch:
          item == nil        → 报错"task request or rid is required"
          item.Rid != ""     → createResolved(rid, opts, groupID)   # rid 优先
          item.Req != nil    → createDirect(req, opts, groupID)
          其他               → 报错
     d. 成功 → result.TaskIDs += taskID；失败 → failures += err
4. if len(TaskIDs) == 0: 返回 errors.Join(failures)   # 全部失败
5. persistDefaultPathBestEffort; 返回 result
```

成员路径的折算规则（组模式下）值得单独画清楚。设批级 `opts.path`（父目录）为 \( P \)，组目录为 \( P/G \)：

```text
成员情况                              relative        最终 opts.Path
─────────────────────────────────────────────────────────────────
未自带 opts（继承批级）或路径为空      "."             P/G            （落组目录本身）
自带 opts，相对路径 "nested"          "nested"        P/G/nested
自带 opts，绝对路径 P/sub/x           "sub/x"         P/G/sub/x      （保留相对结构）
自带 opts，绝对路径 P 之外（如 /etc）  "../etc/..."    报错：task path is outside
                                                      the group directory
```

实现技巧是「先升后降」：把成员路径折算成相对父目录 \( P \) 的绝对路径，再用 `filepath.Rel(P, requested)` 求出相对量，最后拼到组目录 \( P/G \) 下。这样相对路径与父目录内绝对路径统一成一种处理，`filepath.IsLocal(relative)` 一并拦住了 `..` 逃逸（`IsLocal` 对以 `..` 开头的相对路径返回 false）。

#### 4.2.3 源码精读

[pkg/base/model.go:L202-L213](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/base/model.go#L202-L213)：批量请求模型 `CreateTaskBatch` 与 `CreateTaskBatchItem` 的字段定义。

[pkg/api/service.go:L51-L55](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service.go#L51-L55)：路由表中 `POST /api/v1/tasks/batch` 注册在单任务 `POST /api/v1/tasks` 之后——REST 层只是薄壳。

[pkg/api/service.go:L175-L188](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/api/service.go#L175-L188)：handler `createTaskBatch` 只做两件事：解码进 `base.CreateTaskBatch`、校验 `reqs` 非空后直接调 `CreateDirectBatchWithResult`。返回值是 `CreateTaskBatchResult` 对象（**替代了旧的字符串数组响应**），外层 `code/msg/data` 信封不变。

[pkg/download/downloader.go:L654-L662](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L654-L662)：兼容层 `CreateDirectBatch` 保留旧的 Go API 签名（只返回任务 ID 列表），内部转调 `CreateDirectBatchWithResult`——需要组元数据的调用方（API 层）用新方法。

[pkg/download/downloader.go:L664-L672](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L664-L672)：入口三行——空批次直接报错 `batch requires at least one task`；`prepareDefaultPath` 只在批级 `opts` 显式 `AsDefaultPath + SkipAutoCategorize` 时才产生默认目录候选（详见 [pkg/download/downloader.go:L1802-L1815](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1802-L1815)，普通批量传空 opts 时它直接返回空串）。

[pkg/download/downloader.go:L673-L690](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L673-L690)：分组分支——建组、立刻填 `result.ParentID`，然后注册 `defer`：批量结束时解除 `creatingGroups` 标记并尝试清理空组。这个 defer 覆盖所有 return 路径，包括「全部失败」的提前 return，保证预留的目录不会泄漏（4.4.3 详解）。

[pkg/download/downloader.go:L692-L705](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L692-L705)：opts 选择与隔离。注意 `opts.Clone()`——成员之间、成员与批级之间不共享 `Options` 指针，后面对 `opts.Path` / `SkipAutoCategorize` 的改写不会污染调用方传入的对象（`httpSizeOptions` 的注释也点明「batch tasks may share options」所以要 Clone）。

[pkg/download/downloader.go:L706-L729](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L706-L729)：组内路径折算的完整实现，对照 4.2.2 的表格逐行读：`relative` 初值 `"."`；只有「成员显式带 opts 且路径非空」才走折算（`item != nil && item.Opts != nil && opts.Path != ""`）；继承批级 opts 的成员直接落组目录。最后两行 `opts.Path = filepath.Join(group.Path, relative)` 与 `opts.SkipAutoCategorize = true` 是分组任务的两个不变式：路径在组内、跳过自动分类。

[pkg/download/downloader.go:L730-L741](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L730-L741)：成员分派的 switch——`item.Rid != ""` 分支排在 `item.Req != nil` 之前，这就是「rid 优先」在代码里的全部体现；`nil` 成员和空成员都报 `task request or rid is required`。

[pkg/download/downloader.go:L868-L884](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L868-L884)：`createResolved` 的「单一创建者」机制（u4-l2 已讲）：取出即删缓存、失败归还。与批量结合后有一个重要推论——**rid 被成功消费后不可重放**，全失败的批量里未被消费的 rid 仍然有效（成员报错发生在消费之前时）。

[pkg/download/downloader.go:L742-L752](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L742-L752)：部分成功的收口——`taskID != ""` 进结果，否则进 `failures`；`len(result.TaskIDs) == 0` 时用 `errors.Join` 聚合所有错误返回（Go 1.20+ 的多错误包装，错误信息里能看到每个成员的失败原因）。

[pkg/download/downloader_test.go:L4801-L4842](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4801-L4842)：`TestDownloader_BatchMixedResolvedAndDirect` 是本模块规则的最佳实证：批量混合 `{Rid: "missing"}`（无效 rid）、`{Rid: 有效, Req: 干扰, Opts: SelectFiles[1]}`（验证 rid 优先且选择保留）、`{Req: 直接请求}`、`{}`（空成员），断言恰好成功 2 个、`SelectFiles` 不丢、有效 rid 被消费、再次提交时已消费的 rid 失败但有效 req 仍被访问。

[pkg/download/downloader_test.go:L4844-L4869](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4844-L4869)：`TestDownloader_BatchAllFailedAndResolvedSelection` 验证全失败路径：返回错误、失败的 rid 未被消费、`d.groups` 清空、父目录下预留的组目录被删（`os.ReadDir(parent)` 为空）。

#### 4.2.4 代码实践

**实践目标**：通过 REST API 亲手触发「混合批量 + 部分成功」，观察响应结构与 rid 优先规则。

1. **操作步骤**：
   1. 准备一个本地文件服务（避免依赖外网）：`mkdir /tmp/batchsrc && cd /tmp/batchsrc && echo hello-a > a.txt && echo hello-b > b.txt && python3 -m http.server 8000`；
   2. 启动开发用 API 服务：在仓库根目录 `go run ./cmd/api`（监听 `127.0.0.1:9999`，bbolt 存储，无 token，见 [cmd/api/main.go:L12-L23](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/cmd/api/main.go#L12-L23)）；
   3. 先解析一个资源拿 rid：
      `curl -s -X POST http://127.0.0.1:9999/api/v1/resolve -d '{"req":{"url":"http://127.0.0.1:8000/a.txt"}}'`，记下 `data.id`；
   4. 提交混合批量（故意放一个无效 rid 和一个空成员制造失败）：
      ```bash
      curl -s -X POST http://127.0.0.1:9999/api/v1/tasks/batch -d '{
        "reqs": [
          {"rid": "not-exist"},
          {"rid": "<上一步的 rid>", "req": {"url":"http://127.0.0.1:8000/b.txt"}},
          {"req": {"url": "http://127.0.0.1:8000/b.txt"}, "opts": {"name": "b-renamed.txt"}}
        ]
      }'
      ```
   5. `curl -s http://127.0.0.1:9999/api/v1/tasks` 查看任务列表。
2. **需要观察的现象**：批量响应的 `data` 是对象 `{"taskIds": ["...", "..."]}`（无 `parentId`）；任务列表里恰好两个新任务，其中一个名字是 `b-renamed.txt`（成员 opts 整体替换生效）；`a.txt` 与 `b.txt` 各被下载一次。
3. **预期结果**：HTTP 200 且 `code=0`——尽管有一个成员失败；两个任务最终 `done`。若把三个成员全部改成无效输入，则返回 `code!=0` 且错误信息包含全部失败原因（`errors.Join` 的多行错误）。
4. 「任务最终状态为 done」依赖本地文件服务正常工作，属待本地验证项；响应结构与部分成功行为由源码与 `TestDownloader_BatchMixedResolvedAndDirect`（[pkg/download/downloader_test.go:L4801-L4842](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4801-L4842)）直接保证。

#### 4.2.5 小练习与答案

**练习 1**：成员 A 写了 `{"rid": "r1", "opts": {"name": "x"}}`，批级 `opts` 是 `{"path": "/dl", "selectFiles": [0]}`。A 最终的 name 和 path 是什么？

**答案**：name 是 `x`，path 是**空**（随后 `initOptions` 会用全局默认下载目录回填）——因为成员 opts 整体替换，`path` 不继承批级；`selectFiles` 也不继承，但 `createResolved` 里 `InitSelectFiles` 会把空选择展开为全选下标。

**练习 2**：为什么批量全部失败时要专门删掉预留的组目录，而部分成功时不需要任何补偿？

**答案**：部分成功时组已有成员任务，目录本来就该保留；全部失败时组没有任何任务，`defer` 里的 `cleanupEmptyGroups` 会发现该组无成员引用而把它连同（空的）目录一起清掉——「不留孤儿目录」。这条路径由 `TestDownloader_BatchAllFailedAndResolvedSelection` 的 `os.ReadDir(parent)` 空断言守护。

**练习 3**：同一个 rid 提交两次批量，第二次会发生什么？

**答案**：第一次成功消费后 `fetcherCache` 中该 rid 已被删除，第二次走 `createResolved` 返回 `invalid resource id`。若该成员还有效 `req` 字段也没用——rid 优先只看 rid 非空与否，不会回退到 req。

### 4.3 createTaskGroup：组名校验、目录预留与去重后缀

#### 4.3.1 概念说明

`createTaskGroup(name, parentPath)` 要在**任何成员任务创建之前**产出一个真实存在、路径唯一、白名单合规的目录。它解决四层问题：

1. **父目录归一化**：`parentPath` 来自用户输入（批级 `opts.path`），可能为空、含占位符（`%year%` 等）、是相对路径——先经 `initOptions` + `ReplacePathPlaceholders` + `filepath.Abs` 归一；
2. **组名清洗**：组名会直接成为目录名的最后一段，必须挡住路径注入（`/`、`\`、`\0`、绝对名）和 Windows 非法字符（`:*?"<>|`、结尾句点），再经 `SafeFilename` 兜底；
3. **唯一性**：目标目录可能已被另一个组预留（内存查重）、也可能已存在于文件系统（`Mkdir` 返回 `ErrExist`）——两者都用「去重后缀」解决：依次尝试 `name`、`name (1)`、`name (2)`……直到 `name (1000)`；
4. **白名单**：每个候选目录都要通过 `isDownloadPathAllowed`（glob 匹配 `WhiteDownloadDirs`），不合规直接报错而不是换下一个。

去重后缀的上界 \( 1000 \) 由常量 `maxTaskGroupDirSuffix` 定义，超过即报 `too many duplicate task group directories`——一个防御性上界，避免病态场景下无限循环。

与任务文件重命名的对比（承接第 2 节的直觉）：

| | 组目录（createTaskGroup） | 任务文件（prepareTask） |
| --- | --- | --- |
| 时机 | 批量创建最开始 | 任务首次启动时（`NeedsPreparation`） |
| 去重方式 | `name (n)` 后缀，n ≤ 1000 | `CheckDuplicateAndRename` |
| 失败策略 | 尝试下一个候选 | 任务保持未准备态可重试 |
| 保护锁 | `d.lock`（组表查重） | `checkDuplicateLock`（查重到建文件原子段） |

#### 4.3.2 核心流程

```text
createTaskGroup(name, parentPath):
  1. parentOpts = initOptions({Path: parentPath})   # 空路径回填全局默认目录
  2. parentPath = ReplacePathPlaceholders(TrimSpace(...))
     parentPath == "" → 报错 "download directory is empty"
     parentPath = filepath.Abs(parentPath)
  3. name 清洗:
     含 / \ \0 或是绝对名 → "invalid task group name"
     含 : * ? " < > | 或以 . 结尾 → "invalid task group name"
     name = SafeFilename(name); name ∈ {"", ".", ".."} → 报错
  4. d.lock.Lock()（持锁到函数返回）
  5. for suffix in 0..1000:
       candidate = suffix==0 ? P/name : "P/name (suffix)"
       白名单不过 → 直接报错（不换下一个）
       内存中已有组占用该 Path → continue
       os.MkdirAll(P) 成功后 os.Mkdir(candidate):
         ErrExist → continue（文件系统已占用）
         其他错误 → 返回
       dir = candidate; break
     dir 仍为空 → "too many duplicate task group directories"
  6. group = {ID: NewTask().ID, Name: filepath.Base(dir), Path: dir}
     storage.Put(bucketTaskGroup, group.ID, group)   # 失败则删目录回滚
  7. d.groups[group.ID] = group
     d.creatingGroups[group.ID] = struct{}{}          # 创建中标记
  return group
```

注意第 6 步的顺序：**先写库、再登记内存**，写库失败会 `os.Remove(dir)` 回滚已建的目录——预留动作是「库+目录」的原子对，不会出现目录建了但没有元数据的孤儿。

另一个细节：组的 `Name` 存的是 `filepath.Base(dir)`，即带后缀的最终目录名。也就是说如果 `album` 被占用、实际目录是 `album (1)`，那么组名就是 `album (1)`——展示层看到的名字与磁盘目录严格一致。

#### 4.3.3 源码精读

[pkg/download/downloader.go:L755-L779](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L755-L779)：常量 `maxTaskGroupDirSuffix = 1000` 与函数前半段——父目录归一化与组名三层清洗。注意 `strings.ContainsAny(name, "/\\\x00")` 拦路径分隔符与 NUL，第二组拦 Windows 保留字符。

[pkg/download/downloader.go:L781-L814](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L781-L814)：持锁后的候选目录循环。三个去重来源依次判断：白名单（不过即失败）、`d.groups` 内存查重（`existing.Path == candidate` 则换下一个）、文件系统 `Mkdir` 的 `ErrExist`（换下一个）。`MkdirAll(parentPath)` 先保证父目录存在。

[pkg/download/downloader.go:L815-L825](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L815-L825)：循环结束的三种出口——`dir == ""` 报太多重复；写 `bucketTaskGroup` 失败则 `os.Remove(dir)` 回滚；成功后登记 `d.groups` 与 `creatingGroups`。`creatingGroups` 的作用在 4.4 揭晓：它让并发的 `Delete` 拒绝删除「正在批量创建中」的组。

[pkg/download/downloader.go:L2270-L2284](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2270-L2284)：`initDownloadPathForTask`——分组任务启动时的路径校验不复用普通的 `initDownloadPath`（那会再次展开占位符、做白名单），而是以**组目录**为基准做包含性检查 + 只校验组目录的白名单。注释点明「组路径在创建时已定稿」。

[pkg/download/downloader.go:L2288-L2298](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L2288-L2298)：`isDownloadPathAllowed` 的 glob 语义：`filepath.Match(dir, path)`，未配置白名单时全放行。组目录候选在上一段代码里逐个过这道门。

[pkg/download/downloader_test.go:L4677-L4691](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4677-L4691)：`TestDownloader_TaskGroupRejectsEmptyParentAndWindowsNames` 覆盖非法组名与空父目录的拒绝路径；[pkg/download/downloader_test.go:L4263-L4291](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4263-L4291)（`TestDownloader_TaskGroupConcurrentNames`）验证并发同名批量各自拿到不同后缀目录。

#### 4.3.4 代码实践

**实践目标**：观察去重后缀与组名清洗的实际行为。

1. **操作步骤**（接 4.2.4 的环境，文件服务与 cmd/api 保持运行）：
   1. 提交第一个分组批量：
      ```bash
      curl -s -X POST http://127.0.0.1:9999/api/v1/tasks/batch -d '{
        "group": true, "groupName": "album",
        "opts": {"path": "/tmp/batchdl"},
        "reqs": [{"req": {"url": "http://127.0.0.1:8000/a.txt"}}]
      }'
      ```
   2. 不删任务，再提交一个**同名** `groupName: "album"` 的分组批量（成员换成 `b.txt`）；
   3. `ls /tmp/batchdl` 查看目录；
   4. 再提交一个非法组名（如 `"groupName": "al/bum"` 或 `"album."`）的分组批量。
2. **需要观察的现象**：第一次批量后出现 `/tmp/batchdl/album/a.txt`；第二次批量后出现 `/tmp/batchdl/album (1)/b.txt`（去重后缀生效）；非法组名返回错误 `invalid task group name`，且 `/tmp/batchdl` 下没有新目录。
3. **预期结果**：与上述一致；两次响应的 `data.parentId` 不同（两个独立的组）。
4. 目录后缀与非法名拒绝由源码直接保证；本机文件系统权限异常（如 `/tmp/batchdl` 不可写）属待本地验证项。

#### 4.3.5 小练习与答案

**练习 1**：如果两个并发的分组批量同时用 `groupName: "album"`，会发生什么？为什么不会都拿到同一个目录？

**答案**：`createTaskGroup` 的候选循环整体持有 `d.lock`，内存查重（`d.groups` 里 `Path` 相同则 continue）对并发请求串行生效：第一个请求登记 `P/album` 后，第二个请求在内存查重时看到占用，滑到 `P/album (1)`。文件系统层面的 `Mkdir ErrExist` 是兜底而非主防线。`TestDownloader_TaskGroupConcurrentNames` 即验证此场景。

**练习 2**：为什么白名单不通过时是**直接报错**，而不是像 `ErrExist` 那样尝试下一个候选？

**答案**：候选之间只差一个后缀，都在同一个父目录下——白名单是按路径 glob 匹配的，`P/album` 不合规通常意味着整个父目录 `P` 不合规，换后缀无济于事；而目录已存在是「这个具体名字被占了」，换后缀合理。两类失败的语义不同，处理策略也不同。

**练习 3**：组目录预留成功但 `storage.Put(bucketTaskGroup, ...)` 失败，系统会留下什么状态？

**答案**：什么都不留——代码立即 `os.Remove(dir)` 删掉刚建的目录并返回错误，批量在最外层直接失败返回（连 `creatingGroups` 都不会登记）。`TestDownloader_TaskGroupStorageFailures`（[pkg/download/downloader_test.go:L4292-L4323](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4292-L4323)）用故障注入存储验证了这类失败路径。

### 4.4 组 ID 展开与空组清理：批量操作与生命周期收尾

#### 4.4.1 概念说明

组在文件系统与存储里是真实实体，但对调度器完全透明：`MaxRunning` 并发、wait 队列、断点恢复都不认识组，只认识任务。因此「按组操作」的全部实现就是一步**翻译**——把过滤条件里的组 ID 展开成成员任务 ID，之后完全复用 u4-l4 讲过的 TaskFilter 批量语义：

```text
PUT    /api/v1/tasks/{groupId}/pause     ┐
PUT    /api/v1/tasks/pause?id={groupId}  │  filter.IDs = expandGroupTaskIDs(filter.IDs)
PUT    /api/v1/tasks/{groupId}/continue  │  → [t1, t2, t3...]（组内全部成员）
DELETE /api/v1/tasks/{groupId}           ┘  → 走既有 Pause/Continue/Delete
```

展开算法很简单：遍历内存任务表，任务 ID 命中**或**任务的 `ParentID` 命中都算选中。源码注释点明了一个设计取舍——展开**只依赖 `ParentID`，不依赖 `d.groups` 元数据**：即使组元数据意外丢失（比如桶损坏），只要任务的 `ParentID` 还在，按组操作依然正确。

清理是分组的「死亡判定」。两个入口、一套核心：

- `cleanupEmptyGroups(ids)`：包装函数，报错只记日志不向上抛。调用时机两处——`Setup` 启动时（清残留）与批量创建的 `defer`（清全失败批次）。
- `cleanupTaskGroups(ids, forceGroups)`：核心函数。先从候选集合里**剔除仍有成员任务（或正在删除中的任务）引用的组**，剩下的才是空组；空组再按是否在 `forceGroups` 里决定目录处理方式——普通清理用 `removeEmptyGroupDirectories`（只删空目录树、保留文件与软链接），强制清理用 `os.RemoveAll`（连文件一起删）。

三个内存态标记在此汇合，各管一类竞态：

| 标记 | 防护的竞态 | 行为 |
| --- | --- | --- |
| `creatingGroups` | 批量创建期间 Delete 并发删组 | `Delete` 见到创建中的组直接报 `task group is being created` |
| `deletingTasks` | Delete 锁外执行期间清理误判 | 清理时把 deleting 中任务的 `ParentID` 视为仍被引用 |
| `runGeneration`（u4-l4） | 暂停与完成同时发生 | 与分组无直接关系，但同属批量操作的竞态防线 |

#### 4.4.2 核心流程

`Delete` 中组相关的完整时序（u4-l4 讲过三段式骨架，这里聚焦组增量）：

```text
Delete(filter, force):
  [锁内]
  1. 收集 groups：
     filter 为空/空过滤 → 全部组
     filter.IDs 非空    → 原 ID 里命中 d.groups 的进 groups
                          copyFilter.IDs = expandGroupTaskIDsLocked(filter.IDs)
  2. 任一组在 creatingGroups → 报错返回（不许删创建中的组）
  3. 摘除任务：登记 deletingTasks、从 tasks/waitTasks 移除
     被删任务的 ParentID 并入 groupIDs（候选清理集合）
  [锁外]
  4. 逐任务 doDelete；失败则回写 bucketTask + 追加回 tasks/waitTasks（u4-l4）
  5. 解除 deletingTasks 标记
  6. if !force: groups = nil        # 普通删除不做强制目录删除
  7. cleanupTaskGroups(groupIDs, groups)
  8. notifyRunning()（腾出并发槽位唤醒 wait 队列）

cleanupTaskGroups(ids, forceGroups):
  candidates = ids − {仍有任务引用的 ParentID} − {deletingTasks 中任务的 ParentID}
  for id in candidates:
    id 在 creatingGroups → 跳过
    forceGroups[id] 存在 → os.RemoveAll(group.Path)    # 强制：连文件删
    否则                 → removeEmptyGroupDirectories(group.Path)  # 只删空目录
    storage.Delete(bucketTaskGroup, id)
    delete(d.groups, id)
```

一个容易误读的点：**按组 ID 删除（force=true）才会 `RemoveAll` 组目录**；删除组内单个任务（即使 force=true）只把该任务的文件删掉，组目录走 `removeEmptyGroupDirectories`——空了才删、有文件就保留。这保证了「删一个成员不影响其他成员的文件」。

#### 4.4.3 源码精读

[pkg/download/downloader.go:L830-L848](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L830-L848)：`expandGroupTaskIDsLocked` / `expandGroupTaskIDs`——遍历 `d.tasks`，`selected[task.ID] || selected[task.ParentID]` 即命中。注释「Group IDs expand through parentId, even when metadata is unavailable」点明只依赖 `ParentID` 的取舍。

[pkg/download/downloader.go:L962-L1000](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L962-L1000)：`Pause` 开头三行——`filter.IDs = d.expandGroupTaskIDs(filter.IDs)`，展开后为空则 `ErrTaskNotFound`；之后完全是 u4-l4 的既有逻辑（排除终态、清 wait 队列、逐个 doPause）。`Continue`（[pkg/download/downloader.go:L1026-L1033](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1026-L1033)）与 `ContinueBatch`（[pkg/download/downloader.go:L1131-L1136](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1131-L1136)）是同样的三行。

[pkg/download/downloader.go:L1156-L1186](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1156-L1186)：`Delete` 的锁内段——组收集（全量或按 ID 命中）、`expandGroupTaskIDsLocked` 展开副本过滤器、`creatingGroups` 拒删。注意展开用的是 `copyFilter`（副本），原始 `filter.IDs` 保留组 ID 用于 `d.groups` 命中——一份输入两种用途。

[pkg/download/downloader.go:L1187-L1218](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1187-L1218)：锁内摘除——`deletingTasks` 登记、`tasks` / `waitTasks` 过滤、被删任务的 `ParentID` 并入 `groupIDs`。

[pkg/download/downloader.go:L1219-L1252](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1219-L1252)：锁外删除与收尾——失败恢复（u4-l4 详解）；`if !force { groups = nil }` 把强制删除限定在「过滤条件直接命中组」的场景；最后 `cleanupTaskGroups` + `notifyRunning`。

[pkg/download/downloader.go:L1256-L1260](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1256-L1260)：`cleanupEmptyGroups` 只在出错时记日志（`cleanup empty task group failed`），不向上抛——它出现在 `Setup` 与批量 `defer` 两条路径上，失败不应中断主流程。

[pkg/download/downloader.go:L1262-L1304](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1262-L1304)：`cleanupTaskGroups` 核心。两个 `delete(candidates, task.ParentID)` 分别剔除「仍有成员」与「删除中任务仍引用」的组；`forceGroups[id] != nil` 分支 `RemoveAll`，否则 `removeEmptyGroupDirectories`；最后删桶记录与内存 map。

[pkg/download/downloader.go:L1306-L1325](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader.go#L1306-L1325)：`removeEmptyGroupDirectories`——后序遍历删空目录：先递归子目录，再 `os.Remove(root)`（目录非空时 Remove 失败、静默保留）。注释强调不碰文件与软链接。

[pkg/download/downloader_test.go:L4367-L4401](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4367-L4401)：`TestDownloader_TaskGroupDeletePersistence` 验证删除组后 `groups` 清空、任务不残留；[pkg/download/downloader_test.go:L4402-L4427](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4402-L4427)（`TestDownloader_TaskGroupConcurrentDelete`）与 [pkg/download/downloader_test.go:L4479-L4524](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4479-L4524)（`TestDownloader_TaskGroupDeleteFailureRestoresQueue`）覆盖删除竞态与失败恢复。

#### 4.4.4 代码实践

**实践目标**：验证「组 ID 展开到成员」与「空组清理」两个行为。

1. **操作步骤**（接 4.3.4 环境，此时 `/tmp/batchdl` 下已有 `album` 与 `album (1)` 两个组）：
   1. 提交一个三成员的分组批量（`groupName: "cleanup-test"`），记下响应里的 `parentId`；
   2. 用组 ID 一次性暂停：`curl -s -X PUT "http://127.0.0.1:9999/api/v1/tasks/<groupId>/pause"`，然后 `curl -s http://127.0.0.1:9999/api/v1/tasks` 看三个成员的状态；
   3. 用组 ID 一次性删除：`curl -s -X DELETE "http://127.0.0.1:9999/api/v1/tasks/<groupId>"`（默认 force=false）；
   4. `ls /tmp/batchdl/cleanup-test` 观察目录。
2. **需要观察的现象**：步骤 2 后组内**所有成员**（包括 wait 队列中的）都变 `pause`——组 ID 被展开成了成员任务 ID；步骤 3 后任务列表里三个成员全部消失；步骤 4 中目录仍存在且文件还在（非 force 删除不删文件，`removeEmptyGroupDirectories` 只删空目录，目录里有文件就保留）。
3. **预期结果**：与上述一致。若步骤 3 改为 `?force=true`，则目录连同文件被 `RemoveAll` 整体删除（待本地验证——请确认目录里没有你需要的文件再试）。
4. 组状态清理（`task_group` 桶记录删除）无法从文件系统直接观察，可通过重启 `cmd/api` 后 `GET /api/v1/tasks` 不再出现该组任何任务、且无「幽灵目录重建」来侧面验证。

#### 4.4.5 小练习与答案

**练习 1**：批量创建还在进行中（组在 `creatingGroups`），此时并发的 `DELETE /api/v1/tasks/<groupId>` 会成功吗？

**答案**：不会。`Delete` 在锁内检查每个候选组，命中 `creatingGroups` 即返回 `task group is being created`。这防止了「组刚预留、成员还没落盘」的窗口期把组连根拔掉，导致后续 `doCreate` 的组校验失败。

**练习 2**：`Delete` 结束时对 `force=false` 把 `groups` 置为 nil，为什么被删任务引用的组仍可能被清理掉？

**答案**：被删任务的 `ParentID` 已并入 `groupIDs` 候选集合；`cleanupTaskGroups` 剔除的只是「**仍有**任务引用」的组——任务已从 `d.tasks` 摘除，若组内再无其他成员，它就是空组候选，走 `removeEmptyGroupDirectories` 清理。`groups=nil` 只是不做 `RemoveAll` 强删，不影响空组判定。

**练习 3**：组内两个任务已完成下载、一个还在等待队列，按组删除（force=false）后磁盘上留下什么？

**答案**：三个任务记录与组记录全部删除；两个已完成任务的**文件保留**（force=false 的 `doDelete` 不删文件，且 `removeEmptyGroupDirectories` 不碰文件），组目录因非空而保留——一个只含文件的「孤儿目录」。要连文件一起删需 `force=true` 按组删除，此时 `RemoveAll` 把目录整体清掉。

## 5. 综合实践

把四个模块串成一个完整闭环：**建组 → 混合成员 → 部分成功 → 按组操作 → 空组清理**。

**任务**：模拟「扩展文件夹下载」——一次分组批量抓取本地文件服务上的多个文件，混合 rid 与 req 成员、给部分成员单独设置 opts，然后按组完成暂停与删除全生命周期。

1. **准备**：
   - `mkdir -p /tmp/batchsrc && cd /tmp/batchsrc`，放入 `1.jpg 2.jpg 3.mp3 notes.txt`（内容随意），`python3 -m http.server 8000`；
   - 仓库根目录 `go run ./cmd/api`；
   - `mkdir /tmp/gopeed-batch-lab` 作为父目录。
2. **解析两个资源拿 rid**：对 `1.jpg`、`2.jpg` 各发一次 `POST /api/v1/resolve`，记下两个 `data.id`（r1、r2）。
3. **提交分组批量**（故意设计每个成员考验一条规则）：
   ```bash
   curl -s -X POST http://127.0.0.1:9999/api/v1/tasks/batch -d '{
     "group": true, "groupName": "ext-folder",
     "opts": {"path": "/tmp/gopeed-batch-lab"},
     "reqs": [
       {"rid": "r1"},
       {"rid": "r2", "req": {"url": "http://127.0.0.1:8000/3.mp3"},
        "opts": {"selectFiles": [0]}},
       {"rid": "expired-or-typo"},
       {"req": {"url": "http://127.0.0.1:8000/notes.txt"},
        "opts": {"path": "docs"}}
     ]
   }'
   ```
   预期：响应 `data.parentId` 非空、`data.taskIds` 恰有 3 个——成员 1 走 rid；成员 2 rid 优先（req 被忽略，任务是 `2.jpg` 而非 `3.mp3`）；成员 3 失败但不影响整批；成员 4 的相对路径 `docs` 折算进组目录。
4. **验证目录布局**：`find /tmp/gopeed-batch-lab -type f`，预期看到 `ext-folder/1.jpg`、`ext-folder/2.jpg`、`ext-folder/docs/notes.txt`——成员要么落组目录本身、要么落组目录内的相对子目录。
5. **按组暂停与继续**：`PUT /api/v1/tasks/<parentId>/pause` 后查任务列表确认全组成员状态变化；再 `PUT /api/v1/tasks/<parentId>/continue` 恢复。
6. **删除全组**：`DELETE /api/v1/tasks/<parentId>?force=true`，然后 `ls /tmp/gopeed-batch-lab`——组目录连同文件整体消失；`GET /api/v1/tasks` 无残留任务。
7. **对照测试**：跑 `go test ./pkg/download/ -run 'TestDownloader_TaskGroup|TestDownloader_Batch' -v`，把你的观察与 `TestDownloader_TaskGroupExtensionBatchRestore`（[pkg/download/downloader_test.go:L4525-L4655](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L4525-L4655)）、`TestDownloader_TaskGroupRelativeMemberPath`（[pkg/download/downloader_test.go:L5043-L5056](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/pkg/download/downloader_test.go#L5043-L5056)）等测试的断言逐条对照。

下载完成态依赖本地文件服务稳定，属待本地验证项；所有结构性断言（taskIds 数量、parentId、目录布局、清理行为）均与源码及上述测试一一对应。

## 6. 本讲小结

- **TaskGroup 是目录元数据而非任务**：四个字段、独立 `task_group` 桶、成员任务以 `ParentID` 单向引用（落盘权威），`task.group` 指针只是运行时缓存（恢复时由 `Setup` 挂接、`MarshalJSON` 时透出给前端）。
- **批量三条规则**：rid 优先（`item.Rid != ""` 分支在前）、成员 opts 整体替换（不与批级合并，且 `Clone` 隔离）、部分成功（`errors.Join` 聚合失败、`CreateTaskBatchResult` 统一响应对象）。
- **组目录先于任务预留**：`createTaskGroup` 在锁内完成组名三层清洗、白名单校验、`name (n)` 去重后缀与 `Mkdir` 占位，先写库再登记内存，失败回滚目录；分组任务启动时以组目录为基准做包含性检查，且 `SkipAutoCategorize` 旁路自动分类。
- **组对调度器透明**：按组操作 = `expandGroupTaskIDs` 把组 ID 翻译成成员任务 ID，之后完全复用 TaskFilter 的批量语义；展开只依赖 `ParentID`，不依赖组元数据。
- **清理有两把刷子**：`removeEmptyGroupDirectories` 只删空目录树（保留文件），`os.RemoveAll` 仅在按组 ID 强制删除时使用；`creatingGroups` / `deletingTasks` 两组内存标记分别防住「删创建中的组」与「删除中被误判为空组」的竞态。

## 7. 下一步学习建议

本讲补全了任务生命周期的最后一块拼图。接下来：

1. **u8-l1（pkg/api 服务层）**：本讲的 REST 实践只用了路由表的一角，下一讲系统讲解 `RouteSpec.Match`、`Service.Dispatch` 与进程内调用方式，理解批量接口如何被 REST/FFI/测试三端共享。
2. **u10-l1（自动分类与目录决策）**：`decideDownloadDirForTask` 的另一个分支——本讲只看了 `group != nil && SkipAutoCategorize` 的旁路，下一讲完整讲分类路由、建议目录与白名单回退。
3. **u11-l3（Flutter 前端）**：前端如何消费 `CreateTaskBatchResult`——`create_task_batch.dart` 模型、pending download 流程与任务卡片的分组展示，看分组能力如何端到端落到 UI。
4. **延伸阅读**：`_docs/task-batch-api.md`（[_docs/task-batch-api.md:L1-L53](https://github.com/GopeedLab/gopeed/blob/1e36599d52bc50c3aea479d348f3e9ccd43a448e/_docs/task-batch-api.md#L1-L53)）是批量 API 的权威契约，通读一遍并对照本讲源码，检验自己能否预判每条规则对应的代码行。
