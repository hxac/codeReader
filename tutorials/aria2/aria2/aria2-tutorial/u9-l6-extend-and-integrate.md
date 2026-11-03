# 二次开发实战：扩展点与新功能接入

## 1. 本讲目标

本讲是整套手册的收官篇。前面八个单元我们一直在「读」aria2：读引擎、读命令链、读 BT、读 DHT、读 RPC。本讲反过来回答一个问题：**如果我改它，应该从哪里下手？**

读完本讲，你应该能够：

1. 画出 aria2 的五类主要扩展点（DownloadHandler、Notifier、RPC 方法、选项开关、引擎装配/周期命令），并说出每类扩展点对应的真实源码文件。
2. 独立走通「新选项 → 新行为」的完整改动路径：知道一个 `--my-debug-flag` 这样的布尔选项要动哪 5 个文件、每个文件里改什么、为什么缺一不可。
3. 评估一个新需求应该挂在哪一层：协议级插件（DownloadHandler）、事件通知（Notifier）、RPC 方法、还是响应体过滤器（StreamFilter）——特别是「新协议该写成独立命令链还是 StreamFilter」这道架构选择题。

本讲的三条主线：**扩展点盘点 → 选项驱动的功能开关 → Notifier 事件体系**，最后用一个综合实践把它们串成一次真实的二次开发。

## 2. 前置知识

本讲默认你已读完依赖讲义（u3-l3 任务创建流水线、u6-l2 BT 运行时装配、u8-l5 libaria2 嵌入），并具备以下概念。没有的话，这里用三段话补齐：

**扩展点（extension point）**：一个程序预留的「合法插口」。aria2 没有插件系统（没有动态加载 `.so` 的机制），所以它的扩展点都是**编译期插口**——你要改源码重新编译。扩展点的价值在于：它把「改哪里」从全库 950 个文件收敛到少数几个约定位置，改这些位置不需要理解整个引擎。

**选项即功能开关**：aria2 的能力几乎都能用 `--xxx=true/false` 打开关闭。回顾 u1-l5：选项以字符串键登记在 `prefs.cc`，经 `OptionHandlerFactory` 注册类型校验与帮助文本，运行期由 `Option::getAsBool(PREF_XXX)` 读取。因此「给 aria2 加一个可配置的新行为」==「注册一个新选项 + 在合适的代码点读它」。

**观察者模式（Observer）**：事件源不认识监听者，只维护一个监听器列表，事件发生时逐个调用。aria2 的 `Notifier` 是教科书式实现：任务管理器 `RequestGroupMan` 只管发事件，WebSocket 推送（u8-l3）和 libaria2 C 回调（u8-l5）都是挂在它上面的监听器。你要新增「下载完成时做某事」，正确姿势不是去改任务管理器，而是写一个新监听器。

一个贯穿本讲的判断框架——**改动离数据流越近，性能影响越直接；离数据流越远，侵入性越小**。StreamFilter 在字节流上（每块数据都过你的代码），DownloadHandler 在任务生命周期上（每个任务只触发一两次），Notifier 在事件总线上（只是通知，不影响下载）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
|---|---|---|
| [src/DownloadHandler.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadHandler.h) | DownloadHandler 基类：canHandle + Criteria 谓词 | 扩展点 1 的接口定义 |
| [src/PreDownloadHandler.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PreDownloadHandler.h) / [src/PostDownloadHandler.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PostDownloadHandler.h) | 下载前/下载后两个纯虚钩子 | 扩展点 1 的两个插口 |
| [src/download_handlers.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_handlers.cc) | 内置 handler 的懒加载单例仓库 | 「官方是怎么写插件的」样板 |
| [src/RequestGroup.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc) | handler 列表的装配与执行 | 扩展点 1 的调用方 |
| [src/Notifier.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Notifier.h) / [src/Notifier.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Notifier.cc) | 事件总线：监听器列表 + 广播 | 扩展点 2 本体 |
| [src/RequestGroupMan.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc) | 六种下载事件的产生点 | 扩展点 2 的事件源 |
| [src/WebSocketSessionMan.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSessionMan.cc) | 事件 → aria2.onXxx 通知的翻译官 | 监听器实例 A |
| [src/aria2api.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/aria2api.cc) | 事件 → C 回调的适配器 | 监听器实例 B |
| [src/RpcMethodFactory.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodFactory.cc) | RPC 方法名 → 方法对象的映射与缓存 | 扩展点 3 |
| [src/prefs.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.h) / [src/prefs.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.cc) / [src/usage_text.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/usage_text.h) / [src/OptionHandlerFactory.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc) | 选项键登记、帮助文本、类型校验注册 | 扩展点 4 的四件套 |
| [src/DownloadEngineFactory.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc) | 引擎总装：周期命令与 RPC 服务按选项条件装配 | 扩展点 5 |
| [src/DownloadEngine.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc) | 主循环与每轮迭代钩子 | 选项读取的挂载点 |
| [src/HaveEraseCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HaveEraseCommand.cc) | 最简周期命令样板 | 「新增周期行为」的模仿对象 |

## 4. 核心概念与源码讲解

### 4.1 扩展点盘点：五张插口地图

#### 4.1.1 概念说明

把 aria2 想成一条流水线：

```
命令行/RPC 输入
    │
    ▼
任务创建（download_helper，u3-l3）          ← 插口④选项开关：一切行为的参数来源
    │
    ▼
RequestGroup（任务容器，u3-l1）
    │  ← 插口①DownloadHandler：任务开始前改写自己 / 完成后派生新任务
    ▼
DownloadEngine 主循环（u2-l1）
    │  ← 插口⑤引擎装配/周期命令：往循环里加常驻服务
    ├── 命令链（HTTP/FTP/SFTP，u4）
    │       └── StreamFilter（u4-l6）       ← 字节流层的插口（本讲对比项）
    └── Notifier 事件总线                    ← 插口②：任务状态变化的通知
    │
    ▼
RPC 服务器（u8-l3/u8-l4）                    ← 插口③：新增外部控制方法
```

五类插口各有分工：

| 插口 | 回答的问题 | 触发频率 | 典型需求 |
|---|---|---|---|
| ① DownloadHandler | 「这个任务开始前/结束后要变形吗」 | 每任务 1~2 次 | 内存下载 .torrent、下载完派生 BT 任务 |
| ② Notifier | 「任务状态变了要通知谁」 | 每任务 6 种事件 | 推送到钉钉、写数据库、联动脚本 |
| ③ RPC 方法 | 「外部程序怎么触发新能力」 | 按调用计 | 新增 aria2.xxx 方法 |
| ④ 选项开关 | 「这个行为要不要开、参数多少」 | 读一次或周期读 | 几乎所有功能的入口 |
| ⑤ 引擎装配/周期命令 | 「主循环里要不要多一个常驻服务」 | 每秒/每 N 秒 | 定时统计、监控上报 |

#### 4.1.2 核心流程

给你一个新需求时的决策流程（伪代码）：

```text
需求 X 到手：
若 X 是"下载某类内容时改变任务形态"      → 插口① 写 DownloadHandler
若 X 是"状态变化时要告知外部"            → 插口② 写 DownloadEventListener
若 X 是"让外部程序能触发/查询"           → 插口③ 在 RpcMethodFactory 注册方法
若 X 是"按用户配置开关的行为"            → 插口④ 注册选项 + 找读取点
若 X 是"周期性后台服务"                  → 插口⑤ 写 TimeBasedCommand 并在
                                          DownloadEngineFactory 装配
注意：真实需求往往组合多个插口，
例如"可开关的事件回声"= ④(开关) + ②(监听器) + ⑤(装配时机)。
```

#### 4.1.3 源码精读

五类插口各取一个「官方样板」速览，后续小节展开 ①②④。

**插口① 的接口核心**——`DownloadHandler` 基类只有两个方法：

- [src/DownloadHandler.cc:L42-L51](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadHandler.cc#L42-L51)：`canHandle` 的全部逻辑是 `!criteria_ || criteria_->match(requestGroup)`——没设判定条件就永远匹配，设了就交给谓词对象。这是「策略模式 + 空对象」的组合。

**插口② 的接口核心**——监听器只有一个纯虚函数：

- [src/Notifier.h:L49-L52](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Notifier.h#L49-L52)：`DownloadEventListener::onEvent(DownloadEvent event, const RequestGroup* group)`。实现这一个函数，你就接上了全部六种事件。

**插口③ 的注册表**——RPC 方法名清单与工厂：

- [src/RpcMethodFactory.cc:L53-L94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodFactory.cc#L53-L94)：`rpcMethodNames` 向量列出全部约 40 个方法名（BT/Metalink 方法被 `#ifdef` 条件裁剪）。新增 RPC 方法 = 在这里加一行名字 + 在 `createMethod` 的 if 链里加一个分支（见 [src/RpcMethodFactory.cc:L116-L267](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodFactory.cc#L116-L267)），查不到的名字兜底为单例 `NoSuchMethodRpcMethod`（[L270-L288](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodFactory.cc#L270-L288)，进程级缓存 `cache` 避免重复构造）。

**插口④ 的四件套**（4.4 节以 `--bt-detach-seed-only` 为样板全链路精读）。

**插口⑤ 的装配车间**——`newDownloadEngine` 按选项条件挂命令：

- [src/DownloadEngineFactory.cc:L163-L183](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc#L163-L183)：无条件注册 `FillRequestGroupCommand`、两个 Dispatcher、`EvictSocketPoolCommand`（30 秒）、`HaveEraseCommand`（10 秒）。
- [src/DownloadEngineFactory.cc:L172-L195](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc#L172-L195)：`AutoSaveCommand`、`SaveSessionCommand`、`TimedHaltCommand`、`WatchProcessCommand` 全部「有选项才注册」——**这就是选项开关驱动引擎形态的现场**。
- [src/DownloadEngineFactory.cc:L196-L221](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc#L196-L221)：`--enable-rpc` 为真时逐地址族创建 `HttpListenCommand` 并绑端口，失败抛 `DL_ABORT_EX`。整个 RPC 服务器（u8-l3 的 HTTP/WebSocket 服务）就是从这个 if 长出来的。

#### 4.1.4 代码实践

**实践目标**：不写代码，先练「需求 → 插口」的判断力，并核实每张插口对应的真实文件。

**操作步骤**：

1. 打开 [src/download_handlers.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_handlers.cc)、[src/Notifier.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Notifier.h)、[src/RpcMethodFactory.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodFactory.cc)、[src/DownloadEngineFactory.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc) 四个文件。
2. 对下面 5 个假想需求，各自判断应动用哪个插口（可多选），并写出你要改的文件名：
   - a. 「下载完成后把文件信息写入一个 SQLite 库」
   - b. 「新增 `aria2.myStats` RPC 方法返回自定义统计」
   - c. 「每 60 秒把当前下载速度上报到监控接口」
   - d. 「让 `.myspecial` 扩展名的文件下载到内存而不是磁盘」
   - e. 「上面 c 的上报行为要能用 `--stats-report=false` 关掉」

**需要观察的现象**：你在判断时会发现 e 依附于 c——选项开关很少独立存在，它总是「修饰」另一个插口。

**预期结果**（参考答案见 4.1.5）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 aria2 不把 StreamFilter 也算作「协议级插件」的同类？

**答案**：StreamFilter 工作在**响应体字节流**上（每个 16KB 块都流经它，见 u4-l6），属于数据面；DownloadHandler 工作在**任务生命周期**上（每任务触发一两次），属于控制面。两者的性能预算完全不同：前者的每块开销会直接拖慢下载速度，后者几乎免费。混为一谈会导致把高频逻辑错放到低频插口或反之。

**练习 2**：4.1.4 中 5 个需求的插口选择。

**答案**：
- a → 插口②：实现 `DownloadEventListener`，在 `EVENT_ON_DOWNLOAD_COMPLETE` 时写库；注册时机可仿照 [src/aria2api.cc:L134-L139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/aria2api.cc#L134-L139)。
- b → 插口③：改 `RpcMethodFactory.cc` 两处（名字清单 + `createMethod` 分支），再写一个 `RpcMethod` 子类。
- c → 插口⑤：写一个 `TimeBasedCommand` 子类（仿 [src/HaveEraseCommand.cc:L44-L76](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HaveEraseCommand.cc#L44-L76)），在 `DownloadEngineFactory::newDownloadEngine` 注册。
- d → 插口①：写一个 `PreDownloadHandler`，配一个匹配 `.myspecial` 扩展名的 `ContentTypeRequestGroupCriteria`，`execute` 里换成内存 `DiskWriterFactory`（仿 [src/MemoryPreDownloadHandler.h:L43-L54](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MemoryPreDownloadHandler.h#L43-L54)）。
- e → 插口④修饰⑤：注册 `--stats-report` 选项，装配与命令内部各读一次。

**练习 3**：`DownloadHandler::canHandle` 为什么把「无 criteria」设计为永远匹配？

**答案**：空对象模式的典型用法——不设判定条件的 handler（如 `BtPostDownloadHandler`）表示「无条件接管」，判定条件是可选的增强而不是必填项。这样 `RequestGroup::postDownloadProcessing` 的循环（见 4.2.3）不需要区分「有条件/无条件」两种 handler，代码路径归一。

### 4.2 DownloadHandler：任务生命周期上的协议级插件

#### 4.2.1 概念说明

回顾 u3-l3 建立的认知：一个任务（RequestGroup）的下载结果可能「是另一个任务的原料」。.torrent 文件下载完才能派生真正的 BT 数据任务；magnet 拿到 metadata 后才能拼出 torrent（u6-l9）。这条「任务生任务」的链子，挂接点就是 DownloadHandler。

它分两个互补的钩子：

- **PreDownloadHandler（下载前）**：在协议命令链拿到响应头、确定文件类型之后执行，**改写当前任务自身**——典型动作是「别写磁盘了，写内存」。`--follow-torrent=mem` 就是它实现的。
- **PostDownloadHandler（下载后）**：任务成功完成后执行，**产出下一批任务**。`--follow-torrent=true` 的派生逻辑就是它实现的。

注意与 u3-l3 讲过的 `--follow-xxx` 三态（`true|mem|false`）对应关系：`mem` 挂 pre-handler，`true`/`mem` 都挂 post-handler。

#### 4.2.2 核心流程

```text
【pre 路径】HTTP 响应头到达 / FTP、SFTP 协商确定文件名
    └─ RequestGroup::preDownloadProcessing()
         └─ 遍历 preDownloadHandlers_：
              canHandle(this) 为真 → execute(this) 改写本任务（换写入器等）→ 结束

【post 路径】任务下载完成且无需校验
    └─ RequestGroupMan 收割任务时调用 group->postDownloadProcessing(nextGroups)
         └─ 遍历 postDownloadHandlers_：
              canHandle(this) 为真 → getNextRequestGroups(nextGroups, this)
         └─ nextGroups 非空 → insertReservedGroup(0, ...) 插到等待队列最前面
```

两个钩子都是「首个匹配者胜出，至多执行一个」。

#### 4.2.3 源码精读

**执行侧**（调用方）：

- [src/RequestGroup.cc:L1043-L1062](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1043-L1062)：`preDownloadProcessing` 遍历 `preDownloadHandlers_`，首个 `canHandle` 通过者执行 `execute` 后 return；`RecoverableException` 只记日志不致命。pre-handler 的三个触发点分别在 HTTP（[src/HttpResponseCommand.cc:L270](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L270)）、FTP（[src/FtpNegotiationCommand.cc:L400](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FtpNegotiationCommand.cc#L400)）、SFTP（[src/SftpNegotiationCommand.cc:L209](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SftpNegotiationCommand.cc#L209)）——正是「拿到 Content-Type / 文件名」的三个时刻。
- [src/RequestGroup.cc:L1064-L1082](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1064-L1082)：`postDownloadProcessing` 同构，钩子签名换成 `getNextRequestGroups(groups, this)`，产出收集到出参向量。
- [src/RequestGroupMan.cc:L391-L398](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L391-L398)：收割完成任务的现场——`postDownloadProcessing(nextGroups)` 之后，若产出非空则 `insertReservedGroup(0, nextGroups)` 插到等待队列**头部**（0 号位），保证派生任务最先被 `-j` 调度。

**装配侧**（谁往列表里放东西）：

- [src/RequestGroup.cc:L1084-L1116](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1084-L1116)：`initializePreDownloadHandler` / `initializePostDownloadHandler` 按 `--follow-torrent`、`--follow-metalink` 的取值决定挂哪些 handler。**这就是你接入自己 handler 的第一入口**：要么在这里按你的新选项条件 push，要么用公开的 [addPreDownloadHandler / addPostDownloadHandler，src/RequestGroup.cc:L1137-L1145](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1137-L1145) 从外部注入。

**插件侧**（官方样板）：

- [src/download_handlers.cc:L101-L111](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_handlers.cc#L101-L111)：`getBtPreDownloadHandler` 用函数内静态 `unique_ptr` 做懒加载单例，并给 handler 挂上 `ContentTypeRequestGroupCriteria`（匹配 .torrent 扩展名或 BT Content-Type）。同文件的 [L72-L89](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_handlers.cc#L72-L89)（Metalink pre/post）、[L121-L128](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_handlers.cc#L121-L128)（magnet 的 UTMetadata post）完全同构。**抄这个文件就是你写新插件的起点**。
- [src/MemoryPreDownloadHandler.h:L43-L54](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MemoryPreDownloadHandler.h#L43-L54)：pre-handler 的 `execute` 五连改：换内存 `DiskWriterFactory`、关文件预分配、关本地文件预检查、打 `inMemoryDownload` 标记、并发命令数压到 1。看清楚：**pre-handler 的本质是「在数据流动之前改任务配置」**。
- [src/ContentTypeRequestGroupCriteria.cc:L52-L72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ContentTypeRequestGroupCriteria.cc#L52-L72)：判定谓词的实现：单文件任务才考虑；先按扩展名（大小写不敏感后缀匹配），再按服务器下发的 Content-Type 精确匹配。

#### 4.2.4 代码实践

**实践目标**：亲手观察 pre/post 两个钩子的开关效果，验证「任务生任务」的链子。

**操作步骤**：

1. 准备一个 .torrent 文件（任何公开种子均可）。
2. 第一次运行：
   ```bash
   ./src/aria2c --follow-torrent=false your.torrent
   ```
3. 第二次运行（换个保存目录或先删除产物）：
   ```bash
   ./src/aria2c --follow-torrent=true your.torrent -d /tmp/bt-follow
   ```
4. 两次都加 `-l=log.txt --log-level=debug`。
5. 在日志中搜索 `Finding PostDownloadHandler`（对应 [src/RequestGroup.cc:L1067](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1067) 的调试日志）与 `Adding ... RequestGroups as a result of PostDownloadHandler`（对应 [src/RequestGroupMan.cc:L394](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L394)）。

**需要观察的现象**：第一次运行只得到 `your.torrent` 这个小文件本身；第二次运行 .torrent 下载完后日志出现派生记录，随后开始真正的数据下载。第二次日志中 `Finding PostDownloadHandler` 出现在任务完成时刻。

**预期结果**：`--follow-torrent=false` 时 `initializePostDownloadHandler` 不挂 BT handler（[src/RequestGroup.cc:L1100-L1116](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1100-L1116) 的两个 if 都不成立），派生不发生。具体输出依赖所测种子，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 post-handler 产出的任务要 `insertReservedGroup(0, ...)` 插队首而不是队尾？

**答案**：派生任务与母任务在语义上是一个整体（用户请求的就是最终数据），且母任务刚完成、用户正在等结果；插队首保证 `-j` 有空位时派生任务最先获得执行。队尾则可能被大量排队任务压住，体验上像「下载完了却不动」。

**练习 2**：如果我想让「下载完的 .zip 文件自动解压」也走 PostDownloadHandler，合适吗？

**答案**：接口上可行（`getNextRequestGroups` 里做任何事都行），但语义上不合适：post-handler 的契约是「产出后续下载任务」，解压是本地数据加工，更适合挂 Notifier 的 `EVENT_ON_DOWNLOAD_COMPLETE`（4.3 节）或现成的 `--on-download-complete` 钩子（[src/RequestGroupMan.cc:L255-L274](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L255-L274) 的 `executeHookByOptName`）。契约错位会让后来者难以理解代码。

**练习 3**：`MemoryBencodePreDownloadHandler`（BT 的 pre-handler 实体）与通用的 `MemoryBufferPreDownloadHandler` 有何区别？

**答案**：两者的差异只有一个模板参数。`bittorrent::MemoryBencodePreDownloadHandler`（[src/download_handlers.cc:L104-L105](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_handlers.cc#L104-L105) 使用）是 `typedef MemoryPreDownloadHandler<BencodeDiskWriterFactory>` 的类型别名，而通用版 `MemoryBufferPreDownloadHandler` 是 `typedef MemoryPreDownloadHandler<ByteArrayDiskWriterFactory>`（[src/MemoryBufferPreDownloadHandler.h:L43-L44](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MemoryBufferPreDownloadHandler.h#L43-L44)）——模板骨架 [src/MemoryPreDownloadHandler.h:L43-L54](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MemoryPreDownloadHandler.h#L43-L54) 完全共用，模板参数决定「内存里以什么格式存」（BT 场景存 bencode 以便后续解析回 torrent 字典，magnet 元数据场景直接存原始字节）。这是 C++ 模板做「同流程不同载体」复用的标准手法：写新 pre-handler 时先问「换个 DiskWriterFactory 是不是就够了」。

### 4.3 Notifier 事件体系：一条总线和三个乘客

#### 4.3.1 概念说明

u3-l1 提过任务的 GID 与状态流转，u8-l3 见过 WebSocket 推送的 `aria2.onDownloadStart`，u8-l5 用过 libaria2 的 C 回调——这三件事在源码里是同一个起点的三条支线：**RequestGroupMan 产生事件 → Notifier 单例广播 → 各监听器自行翻译**。

事件一共六种，定义在公共头文件 [src/includes/aria2/aria2.h:L105-L131](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/includes/aria2/aria2.h#L105-L131)：`EVENT_ON_DOWNLOAD_START/PAUSE/STOP/COMPLETE/ERROR` 加 `EVENT_ON_BT_DOWNLOAD_COMPLETE`（BT 下载完成、可能继续做种）。

这个设计的价值：**事件源与消费者彻底解耦**。RequestGroupMan 完全不知道 WebSocket 存在；你新增一个监听器（比如发钉钉通知），RequestGroupMan 一行不用改。

#### 4.3.2 核心流程

```text
【产生】任务状态变化（启动/暂停/停止/完成/出错）
    │  RequestGroupMan 等三处调用 notifyDownloadEvent(EVENT_X, group)
    ▼
【广播】Notifier（SingletonHolder 单例）
    │  for (listener : listeners_) listener->onEvent(event, group)
    ▼
【消费】三个现成乘客 + 你的新乘客：
    ├─ WebSocketSessionMan → json::encode → 推给所有 WebSocket 会话（aria2.onXxx）
    ├─ ApiCallbackDownloadEventListener → 调用宿主程序注册的 C 函数指针
    └─ （扩展位）你的 MyEventListener → 任意逻辑
```

时序上注意：事件在**引擎线程内同步执行**（u8-l5 强调过 run 循环无内部线程），监听器里不能做阻塞操作，否则整个下载停摆。

#### 4.3.3 源码精读

**总线本体**（不到 20 行有效代码）：

- [src/Notifier.h:L54-L70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Notifier.h#L54-L70)：`Notifier` 持有裸指针向量 `listeners_`（不拥有监听器，生命周期由注册者管理），`addDownloadEventListener` 只是 push_back。
- [src/Notifier.cc:L45-L56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Notifier.cc#L45-L56)：`notifyDownloadEvent` 顺序遍历调用。没有异常保护、没有异步队列——**同步、简单、快**。

**事件源三处**：

- [src/RequestGroupMan.cc:L242-L249](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L242-L249)：文件局部辅助函数，先判空单例（「让单元测试更容易」的注释值得注意——测试环境不建 Notifier，这行判空就是为可测试性留的缝，你写新事件源时也要这样）。
- [src/RequestGroupMan.cc:L255-L285](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L255-L285)：`executeStopHook` 是任务停止的统一出口：先按结果码选 `--on-download-stop/complete/error` 外部钩子（`executeHookByOptName` 起子进程），再发 COMPLETE/ERROR/STOP 三种事件——**外部命令钩子与事件总线在这里并肩工作**。
- [src/RequestGroupMan.cc:L582](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L582)：任务被提升为活动时发 `EVENT_ON_DOWNLOAD_START`。
- BT 特有的 `EVENT_ON_BT_DOWNLOAD_COMPLETE` 有两个触发点：种子数据校验后进入做种前（[src/BtCheckIntegrityEntry.cc:L80-L85](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtCheckIntegrityEntry.cc#L80-L85)，受 `--bt-enable-hook-after-hash-check` 开关控制）与做种中数据再度完备时（[src/DefaultPieceStorage.cc:L499-L506](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L499-L506)，随后调用 `enableSeedOnly`，即 4.4 节选项样板的行为端）。

**三个乘客的注册与翻译**：

- 单例创建：[src/MultiUrlRequestInfo.cc:L181](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L181)（`prepare()` 第一步，CLI 模式）；libaria2 模式复用同一 `prepare()`。
- 乘客 A（WebSocket）：[src/MultiUrlRequestInfo.cc:L209-L215](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L209-L215) 在 `--enable-rpc` 时创建 `WebSocketSessionMan` 并注册；翻译逻辑在 [src/WebSocketSessionMan.cc:L97-L125](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSessionMan.cc#L97-L125)：`getMethodName` 把枚举映射为 `aria2.onDownloadStart` 等方法名，`addNotification`（[L68-L84](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSessionMan.cc#L68-L84)）组装 JSON-RPC 通知并逐会话入队、`updateWriteCheck()` 注册写事件（u8-l3 讲过这个易漏步骤）。
- 乘客 B（C 回调）：[src/aria2api.cc:L134-L139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/aria2api.cc#L134-L139)：宿主程序在 `SessionConfig::downloadEventCallback` 填了回调，`sessionNew` 就包一个 `ApiCallbackDownloadEventListener` 注册进总线。

**你要新增乘客的完整动作**就三步：继承 `DownloadEventListener` 实现 `onEvent` → 在合适的初始化点（CLI 模式仿 L209 的位置，库模式在 `sessionNew` 后）→ `addDownloadEventListener(this)`。

#### 4.3.4 代码实践

**实践目标**：端到端跟踪一个事件的旅程，从产生点到 WebSocket 推送。

**操作步骤**：

1. 启动带 RPC 的 aria2：
   ```bash
   ./src/aria2c --enable-rpc --rpc-secret=secret123 -d /tmp/rpc-test -l=log.txt --log-level=debug
   ```
2. 用 curl 添加一个下载（任一小文件 URL）：
   ```bash
   curl http://localhost:6800/jsonrpc -d '{"jsonrpc":"2.0","id":"1","method":"aria2.addUri","params":["token:secret123",["https://example.com/"]]}'
   ```
3. 打开日志，搜索 `Adding WebSocketSession`（会话建立）与通知相关行为。
4. 源码侧对照三段代码走一遍调用链：[src/RequestGroupMan.cc:L582](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L582)（START 事件产生）→ [src/Notifier.cc:L50-L56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Notifier.cc#L50-L56)（广播）→ [src/WebSocketSessionMan.cc:L121-L125](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSessionMan.cc#L121-L125)（翻译成 `aria2.onDownloadStart`）。
5. （可选，需本机有 wscat 或浏览器 WebSocket 客户端）连 `ws://localhost:6800/jsonrpc` 并发送带 `aria2` 前缀的订阅握手，观察通知是否到达。

**需要观察的现象**：curl 返回 GID 后，日志中任务进入活动状态；如果你接了 WebSocket 客户端，会先收到 `aria2.onDownloadStart`，下载结束后收到 `aria2.onDownloadComplete`。

**预期结果**：三个事件名与 [src/WebSocketSessionMan.cc:L88-L93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSessionMan.cc#L88-L93) 的字符串常量一一对应。第 5 步依赖外部工具，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`Notifier::listeners_` 存裸指针而不是 `shared_ptr`，有什么隐患与好处？

**答案**：隐患是监听器对象必须活得比 Notifier 久或及时注销，否则悬空指针；好处是零所有权开销、注册者保留完全的生命周期控制权（如 `WebSocketSessionMan` 由 `DownloadEngine` 持有，见 [src/MultiUrlRequestInfo.cc:L211-L213](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L211-L213)）。aria2 的对象生命周期大多挂在 `DownloadEngine` 这棵树上，天然满足前件。另外注意 [src/MultiUrlRequestInfo.cc:L293](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L293) 与 [L344](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L344) 在收尾时 `SingletonHolder<Notifier>::clear()`——单例和引擎一起销毁，顺序正确。

**练习 2**：为什么 `EVENT_ON_BT_DOWNLOAD_COMPLETE` 不并入 `EVENT_ON_DOWNLOAD_COMPLETE`？

**答案**：BT 任务「下载完成」有两个语义不同的时刻：数据齐了（可能继续做种）与彻底结束。RPC 客户端（如前端界面）需要在数据齐时就开始展示/播放，所以单独事件；[src/BtCheckIntegrityEntry.cc:L80-L85](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtCheckIntegrityEntry.cc#L80-L85) 还给它配了独立开关 `--bt-enable-hook-after-hash-check`。事件粒度跟随业务语义，不迁就实现方便。

**练习 3**：如果你在监听器里执行了 `sleep(10)`，会发生什么？

**答案**：整条广播链在引擎线程里同步执行（`notifyDownloadEvent` 是普通函数调用），sleep 期间事件循环停摆：所有下载、RPC 响应、keepalive 全部冻结 10 秒，BT peer 可能因超时断连。正确做法是把耗时操作投入外部队列/线程，监听器内只做投递（参考 u8-l5 的双队列纪律）。

### 4.4 选项驱动的功能开关与引擎装配：五文件走通「新选项 → 新行为」

#### 4.4.1 概念说明

u1-l5 讲过选项系统的读侧（OptionParser、OptionHandler、父链优先级），本讲补上**写侧**：站在贡献者角度，把一个选项从无到有加进 aria2 要动哪些文件。我们全程以现有选项 `--bt-detach-seed-only` 为样板（做种任务不计入 `-j` 并发数），它是教科书级的「小选项撬动大行为」案例。

同时把 u6-l2 埋的线收掉：`enableSeedOnly` 的行为端读的正是这个选项。

为什么「注册选项」要动 4 个文件？因为 aria2 把选项的**键名**（prefs）、**声明**（prefs.h 的 extern）、**帮助文本**（usage_text.h）、**类型校验与元数据**（OptionHandlerFactory）分散治理。好处是各管各的：帮助文本改错字不用碰逻辑；坏处是新手容易漏改——漏掉 OptionHandlerFactory 时选项会被当作未知选项拒绝（错误码 28，见 u1-l5），漏掉 usage_text.h 时 `--help` 里出现空描述。

#### 4.4.2 核心流程

一个布尔选项的五文件链路（也是本讲主实践的地图）：

```text
① src/prefs.h        声明：extern PrefPtr PREF_MY_DEBUG_FLAG;
② src/prefs.cc       定义：PrefPtr PREF_MY_DEBUG_FLAG = makePref("my-debug-flag");
③ src/usage_text.h   文本：#define TEXT_MY_DEBUG_FLAG _(" --my-debug-flag...")
④ src/OptionHandlerFactory.cc  注册：BooleanOptionHandler(PREF_..., TEXT_..., A2_V_FALSE)
⑤ 行为文件            读取：option->getAsBool(PREF_MY_DEBUG_FLAG) 决定做什么
                      （周期性行为挂在 DownloadEngine 主循环或新周期命令上；
                       命令在 DownloadEngineFactory::newDownloadEngine 装配）
```

配套机制两条：

- **取值表示**：布尔选项在 Option 内部存的是字符串 `"true"`/`"false"`（[src/prefs.h:L74-L75](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.h#L74-L75) 的 `A2_V_TRUE/A2_V_FALSE`），`getAsBool` 读取时才解释——这就是 u1-l5 说的「一切皆字符串」。
- **标签体系**：注册时要 `addTag`，标签枚举在 [src/help_tags.h:L42-L59](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/help_tags.h#L42-L59)（TAG_BASIC/TAG_HTTP/TAG_BITTORRENT…），决定选项出现在 `--help` 的哪个分组、能否被 `--rpc` 改全局配置等。

#### 4.4.3 源码精读

样板选项 `--bt-detach-seed-only` 的五处真身：

1. **声明**：[src/prefs.h:L514-L515](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.h#L514-L515)——`extern PrefPtr PREF_BT_DETACH_SEED_ONLY;`，上方注释 `// values: true |false` 是文档惯例。
2. **定义**：[src/prefs.cc:L564](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.cc#L564)——`PrefPtr PREF_BT_DETACH_SEED_ONLY = makePref("bt-detach-seed-only");`。`makePref` 走进程级单例 `PrefFactory`（[src/prefs.cc:L47-L98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.cc#L47-L98)）：登记键名→对象的映射并**顺序发放整数 ID**，这个 ID 就是 u1-l5 讲的 Option 数组下标。新增选项的 ID 自动排在现有全部之后，天然无冲突。
3. **帮助文本**：[src/usage_text.h:L1059-L1069](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/usage_text.h#L1059-L1069)——`TEXT_BT_DETACH_SEED_ONLY` 宏，`_()` 包裹以支持翻译。注意格式惯例：首行 ` --xxx[=true|false]\n`，描述从第 30 列开始，翻页宽度对齐 man 手册。
4. **注册**：[src/OptionHandlerFactory.cc:L1462-L1468](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1462-L1468)——四件套齐全：
   ```cpp
   OptionHandler* op(new BooleanOptionHandler(
       PREF_BT_DETACH_SEED_ONLY, TEXT_BT_DETACH_SEED_ONLY, A2_V_FALSE,
       OptionHandler::OPT_ARG));
   op->addTag(TAG_BITTORRENT);
   handlers.push_back(op);
   ```
   `A2_V_FALSE` 是默认值；`OPT_ARG` 表示命令行上可写 `--bt-detach-seed-only`（省略即 true）也可写 `=false`。没调用 `setInitialOption`/`setChangeGlobalOption` 意味着它既不能逐任务覆盖也不能 RPC 热改——这些元数据开关按需添加（对照同文件 [L1470-L1478](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1470-L1478) 的 `--bt-enable-hook-after-hash-check`，它三个开关全开）。
5. **行为读取点**：[src/RequestGroup.cc:L1325-L1337](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1325-L1337)——`enableSeedOnly()` 开头一行 `if (seedOnly_ || !option_->getAsBool(PREF_BT_DETACH_SEED_ONLY)) return;`：选项关着就直接返回，任务照旧占 `-j` 名额；开着则打 `seedOnly_` 标记、`decreaseNumActive()` 释放名额、`requestQueueCheck()` 触发调度器补位（u3-l1 的脏标记机制）。

**周期行为的两个挂载点**（第⑤步的行为端选址）：

- 轻量做法：直接改 [src/DownloadEngine.cc:L244-L264](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L244-L264) 的 `afterEachIteration`（主循环每圈末尾调用，见 [L158-L184](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L158-L184) 的 run 循环）——注意**必须自加时间闸门**，否则每秒打印成百上千行。
- 正统做法：写一个 `TimeBasedCommand` 子类。样板 [src/HaveEraseCommand.cc:L44-L76](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HaveEraseCommand.cc#L44-L76) 全文不足 40 行：构造函数把间隔（10 秒）交给基类；`preProcess` 在下载全完或要求停机时 `enableExit()` 自我了断；`process` 做实际工作（清理 5 秒前已广播的 piece 广告）。基类 `TimeBasedCommand` 负责「每 interval 秒才醒来」的节律（u2-l3 讲过 INACTIVE 睡眠机制），你完全不用碰计时。然后在 [src/DownloadEngineFactory.cc:L182-L183](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc#L182-L183) 旁边加一行 `e->addRoutineCommand(...)` 并可用你的新选项做条件（仿 [L172-L181](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc#L172-L181) 的 `if (op->getAsInt(...) > 0)` 模式）。若新建 `.h/.cc` 文件，还需把文件名加进 `src/Makefile.am` 的源码清单（u1-l2 讲过条件编译组织）。

**架构专题：新协议该写成独立命令链还是 StreamFilter？** 用决策表回答（承接 u4-l1/u4-l6）：

| 维度 | 独立命令链（Http*/Ftp*/Sftp* 模式） | StreamFilter（ChunkedDecodingStreamFilter 模式） |
|---|---|---|
| 适用前提 | 全新的传输协议（自己的握手、命令、数据通道） | 已有协议的**响应体编码层**变化（压缩、加密容器、编码封装） |
| 侵入面 | 新增 4~6 个 Command 类 + ProtocolDetector 注册 | 新增 1 个 StreamFilter 子类 + 装配点一行 |
| 与引擎关系 | 直接用 EventPoll、SocketCore、连接池 | 完全不碰 socket，只消费字节 |
| 分段下载 | 自己管理（可支持） | 通常被迫禁用（gzip 即如此，u4-l6） |
| 判据 | 「字节流的**含义**变了」 | 「字节流的**包装**变了，去掉包装后还是原文件」 |

例：支持一种 hypothetical `myscheme://` 要写命令链；支持一种新的响应体加密容器写 StreamFilter。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：新增布尔选项 `--my-debug-flag`，打开后引擎周期性打印一行调试信息——完整走一遍设计→实现→验证。

**操作步骤**：

1. **① 声明**：在 [src/prefs.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.h) 的 Advanced 区（如 `PREF_BT_LOAD_SAVED_METADATA` 声明之后，[L521](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.h#L521) 附近）加：
   ```cpp
   // values: true | false
   extern PrefPtr PREF_MY_DEBUG_FLAG;
   ```
2. **② 定义**：在 [src/prefs.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.cc) 对应位置（如 [L570](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.cc#L570) 之后）加：
   ```cpp
   PrefPtr PREF_MY_DEBUG_FLAG = makePref("my-debug-flag");
   ```
3. **③ 帮助文本**：在 [src/usage_text.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/usage_text.h) 任意 `TEXT_` 宏之后加（**示例代码**）：
   ```cpp
   #define TEXT_MY_DEBUG_FLAG                                          \
     _(" --my-debug-flag[=true|false]\n"                               \
       "                              Print periodic debug message to\n" \
       "                              the log.")
   ```
4. **④ 注册**：在 [src/OptionHandlerFactory.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc) 任意 handler 块后加（**示例代码**）：
   ```cpp
   {
     OptionHandler* op(new BooleanOptionHandler(PREF_MY_DEBUG_FLAG,
                                                TEXT_MY_DEBUG_FLAG, A2_V_FALSE,
                                                OptionHandler::OPT_ARG));
     op->addTag(TAG_ADVANCED);
     handlers.push_back(op);
   }
   ```
   然后在文件顶部 `#include "prefs.h"` 已有的前提下直接编译。
5. **⑤ 行为端**：选轻量做法——在 [src/DownloadEngine.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc) 的 `afterEachIteration`（[L244](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L244)）函数体开头加（**示例代码**，含时间闸门）：
   ```cpp
   namespace { Timer myDebugLastPrint{Timer::zero()}; } // 文件顶部匿名命名空间
   // afterEachIteration() 内：
   if (option_ && option_->getAsBool(PREF_MY_DEBUG_FLAG) &&
       myDebugLastPrint.difference(global::wallclock()) >= 5_s) {
     myDebugLastPrint = global::wallclock();
     A2_LOG_NOTICE("my-debug-flag: engine iteration alive");
   }
   ```
   需确认文件已包含 `prefs.h` 与 `wallclock.h`（DownloadEngine.cc 已含后者）。`Timer`、`global::wallclock()` 的用法仿 [src/DownloadEngine.cc:L168-L171](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L168-L171) 与 [src/HaveEraseCommand.cc:L62-L64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HaveEraseCommand.cc#L62-L64)。匿名命名空间里的 `Timer` 静态对象在本文件内的写法请以编译器反馈为准微调（**待本地验证**）。
6. **编译与验证**（u1-l2 的构建流程）：
   ```bash
   make -j$(nproc)
   ./src/aria2c --my-debug-flag -d /tmp/dbg https://example.com/ -l=mylog.txt --log-level=notice
   ./src/aria2c --help="#all" | grep -A2 my-debug
   ./src/aria2c --my-debug-flag=maybe https://example.com/   # 预期报错码 28
   ```
7. **写清单**：把改动过的文件与「为什么必须动它」整理成一页笔记（模板见「预期结果」末尾）。

**需要观察的现象**：

- 第二条命令在 `--help` 输出中能看到你的选项描述（标签为 `#advanced` 分组）。
- 第一条命令运行期间，`mylog.txt` 每约 5 秒出现一行 `my-debug-flag: engine iteration alive`，下载结束后停止。
- 第三条命令以错误码 28 退出，报非法取值——这正是 u1-l5 讲的 `BooleanOptionHandler` 校验在拦截你。

**预期结果**：「新增一个功能要改哪几个文件」清单：

| 文件 | 改什么 | 不改的后果 |
|---|---|---|
| src/prefs.h | `extern PrefPtr` 声明 | 编译错误（行为文件引用不到） |
| src/prefs.cc | `makePref("...")` 定义 + 自增 ID | 链接错误 |
| src/usage_text.h | `TEXT_` 帮助文本宏 | `--help` 描述为空或编译错误 |
| src/OptionHandlerFactory.cc | `BooleanOptionHandler` 注册 + 标签 | `--my-debug-flag` 被判未知选项（错误码 28） |
| src/DownloadEngine.cc | 读选项 + 时间闸门打印 | 选项存在但无任何行为 |
| （可选）src/Makefile.am | 新命令类的源文件清单 | 仅当第⑤步新建文件时需要 |

编译输出与运行行为**待本地验证**（本环境不执行编译）。

#### 4.4.5 小练习与答案

**练习 1**：如果把第⑤步的打印改成放在 `DownloadEngine::run` 循环里每次迭代无条件执行（不加时间闸门），会有什么后果？

**答案**：主循环一圈常小于 1 毫秒（u2-l1 的 refreshInterval 机制），日志会以每秒数千行的速度膨胀，I/O 本身反噬下载性能，且 NOTICE 级别还会刷满控制台。周期性输出必须限频——这正是 `TimeBasedCommand` 存在的意义：基类用 INACTIVE 睡眠+每秒唤醒实现节律（u2-l3），把命令的执行频率从「每圈」降到「每 interval」。

**练习 2**：`--bt-detach-seed-only` 为什么没调用 `setInitialOption(true)`，而 `--bt-enable-hook-after-hash-check` 调了？

**答案**：`setInitialOption` 决定选项能否**逐任务设置**（input-file 缩进选项行、RPC addUri 的 options 参数、任务派生时继承，见 u3-l3）。做种脱离并发计数是进程级策略，逐任务设置没有意义；而「哈希校验后是否触发钩子」不同种子可能不同（比如自动化测试种子不想跑钩子），故开放逐任务。注册时漏配这个元数据是新手常见 bug：选项能全局生效却无法 per-download 覆盖。

**练习 3**：一个 colleague 提议「支持 HTTP 响应体的 age 加密容器，每块数据用前块哈希做密钥」。命令链还是 StreamFilter？

**答案**：StreamFilter。判据套 4.4.3 的决策表：传输层还是 HTTP（握手、头部、状态码都不变），变的只是响应体的**包装**——解开后就是原文件字节流。它天然流式（前块哈希做密钥意味着严格顺序依赖，StreamFilter 的链式 transform 正是为此设计）。但要注意两点代价：分段下载需禁用（每段无法独立解密，比 gzip 的理由更硬）；且容器可能要求知道最终校验和做收尾，需在 `finished()` 判定上做文章（u4-l6 讲过整链 finished 的语义）。

## 5. 综合实践

**任务：做一个「可开关的事件回声」功能 `--event-echo`**——把本讲三个模块串成一次真实开发。

需求：新增布尔选项 `--event-echo`（默认 false）。打开时，aria2 把每种下载事件以一行 NOTICE 日志回显（形如 `event-echo: onDownloadStart GID#xxxx`）；要求不修改 `RequestGroupMan` 与 `Notifier` 的任何代码。

分步实施：

1. **选项侧（模块 4.4）**：按 4.4.4 的五步注册 `--event-echo`（标签建议 `TAG_ADVANCED`）。
2. **监听器侧（模块 4.3）**：写一个小类继承 `DownloadEventListener`（接口见 [src/Notifier.h:L49-L52](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Notifier.h#L49-L52)），`onEvent` 里把枚举转成可读名（映射表仿 [src/WebSocketSessionMan.cc:L97-L118](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSessionMan.cc#L97-L118) 的 `getMethodName`）后 `A2_LOG_NOTICE` 打印 GID（用 `GroupId::toHex(group->getGID())`）。
3. **装配侧（模块 4.1 插口⑤）**：在 [src/MultiUrlRequestInfo.cc:L181](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L181) 单例创建之后、按选项值条件注册你的监听器（写法仿 [L209-L215](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L209-L215)）。监听器对象的生命周期要想清楚：谁持有它才能活得比引擎久（参考 4.3.5 练习 1）。
4. **验证**：
   ```bash
   ./src/aria2c --event-echo -d /tmp/echo https://example.com/ -l=echo.log --log-level=notice
   ./src/aria2c -d /tmp/echo2 https://example.com/ -l=echo2.log --log-level=notice  # 对照组
   ```
   预期第一份日志出现 Start 与 Complete 两行回声（顺序对应 [src/RequestGroupMan.cc:L582](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L582) 与 [L276-L284](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L276-L284)），第二份没有任何回声。**待本地验证**。
5. **复盘**（写进笔记）：
   - 你一共改了几个文件？哪些是「插口约定」强制的，哪些是你的设计自由？
   - 如果要求回声改成推送到外部 HTTP 接口，哪几步不变、哪几步要换？（提示：只换监听器内部实现，总线与注册机制零改动——这就是解耦的回报。）
   - 如果要求事件名可运行期配置，你会把它做成选项还是 RPC 方法？

完成这个实践，你就拥有了给 aria2（乃至任何事件驱动 C++ 项目）安全下刀的手感。

## 6. 本讲小结

- aria2 的可扩展面收敛为五类编译期插口：**DownloadHandler**（任务前/后变形）、**Notifier**（事件总线）、**RPC 方法**（外部控制）、**选项开关**（行为参数）、**引擎装配/周期命令**（常驻服务）；真实需求通常是它们的组合。
- DownloadHandler 是「首个匹配者胜出」的协议级插件：pre 钩子在响应头到达时**改写当前任务**（如转内存下载），post 钩子在完成时**派生新任务**并插队首；`canHandle` = 可选 Criteria 谓词，无谓词即无条件匹配。
- Notifier 是同步广播的观察者总线：`RequestGroupMan` 等三处产生六种事件，WebSocket 推送与 libaria2 C 回调都只是挂在总线上的乘客；新增「下载完成做某事」的正确姿势是加监听器而非改事件源，且监听器内严禁阻塞。
- 「新选项→新行为」是五文件链路：prefs.h 声明、prefs.cc 定义（`makePref` 自动发 ID）、usage_text.h 帮助文本、OptionHandlerFactory 注册（类型校验+标签+元数据开关）、行为文件 `getAsBool` 读取；周期性行为挂 `afterEachIteration`（须自加限频）或正统的 `TimeBasedCommand` 并在 `DownloadEngineFactory` 装配。
- 新协议的架构选型有判据：传输语义变了写**独立命令链**（Http*/Ftp*/Sftp* 模式），仅响应体包装变了写 **StreamFilter**（代价是常须禁用分段下载）。
- 二次开发的第一美德是「照抄官方样板」：写插件抄 `download_handlers.cc`，写周期命令抄 `HaveEraseCommand.cc`，写监听器抄 `WebSocketSessionMan.cc`——插口的价值就是让模仿成为正确的方法论。

## 7. 下一步学习建议

本讲是手册最后一篇，此后没有「下一讲」，给你三条继续深挖的路线：

1. **顺插口做一次真实贡献**：到 aria2 的 GitHub Issues 找标着 feature 或 good first issue 的条目，用本讲的五文件清单评估改动面，走一遍 fork → 分支 → `build_test.sh` 验证 → PR 的完整流程（贡献规范见仓库 `README.rst` 与 `docs/`）。
2. **横向对照其他下载器**：把本画的「五插口地图」套到 curl、wget、axel 上做对比阅读——哪些设计是 aria2 特有的（如 Command 接力、BitfieldMan 位图），哪些是事件驱动程序的通解（Reactor、观察者、工厂），能反过来加深对 aria2 架构取舍的理解。
3. **回头重读三条主链**：带着二次开发视角重走 `main.cc → Context → MultiUrlRequestInfo::prepare → DownloadEngineFactory::newDownloadEngine → DownloadEngine::run`（u1-l3、u2-l1、本讲），此时每个对象在你眼里应该已经是「可拆可换的零件」而不是黑盒——这是源码阅读从「看懂」到「能改」的分水岭。
