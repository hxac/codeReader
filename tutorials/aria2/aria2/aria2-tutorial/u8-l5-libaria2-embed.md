# libaria2：把 aria2 嵌入你的程序

## 1. 本讲目标

前几讲里，aria2 始终以两种「进程形态」出现：命令行 `aria2c`，或者开了 `--enable-rpc` 的服务进程。本讲讲第三种形态：**libaria2**——把整套下载引擎作为库链接进你自己的程序，用一组 C++ 函数直接驱动它。学完本讲，你应该能够：

1. 说出 libaria2 的 API 分层（库初始化 → 会话 → run 循环 → 任务操作/查询）与各自的源码位置。
2. 解释 `sessionNew`/`run`/`sessionFinal` 的阻塞模型，特别是 `RUN_ONCE` 返回值 1 与 0 的含义，以及 `keepRunning` 为何能让引擎「永不退出」。
3. 跟踪事件回调 `onDownloadComplete` 的完整链路：从引擎内部的事件产生点，经 `Notifier` 单例，最终进入你注册的 C 函数指针。
4. 对比「会话 API」与「RPC API」两种集成方式的适用场景。
5. 读懂官方两个示例（命令行批下载器与 wxWidgets GUI），并能动手改写。

## 2. 前置知识

- **库模式 vs 独立模式**：u1-l3 讲过，`MultiUrlRequestInfo::execute()` 的骨架是 `prepare() → run() → getResult()`。libaria2 没有新引擎，它只是把这三步拆开，交给宿主程序自己调用。`Context` 构造函数第一个参数 `standalone` 为 `false` 即库模式（见 [src/Context.h:54](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.h#L54)）。
- **DownloadEngine 主循环**：u2-l1 讲过 `run()` 循环的退出条件是 `commands_` 与 `routineCommands_` 两条队列同时为空。本讲会反复用到这个结论来解释 `RUN_ONCE` 的返回值。
- **事件循环是单线程的**：所有命令都在调用 `run()` 的那个线程里执行。libaria2 没有内部线程，这一点决定了「回调里不能做重活」「跨线程要自己排队」两条纪律。
- **Notifier**：u8-l3 讲过 RPC 模式下系统通知经 `Notifier` 单例分发到 WebSocket 会话。libaria2 的事件回调挂的是同一棵树上的另一个监听器。
- **C 函数指针回调**：C++ 里最朴素的观察者模式——把函数地址和一截 `void*` 交给库，库在合适的时机调用它。没有虚函数、没有 lambda 捕获，但跨线程、跨语言都好使。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/includes/aria2/aria2.h` | **对外唯一公开头文件**。所有 API 声明与详尽注释，装到系统后以 `<aria2/aria2.h>` 被引用 |
| `src/aria2api.h` / `src/aria2api.cc` | API 实现：`Session` 结构、库/会话生命周期、`run`、`addUri` 等任务操作、`DownloadHandle` 查询 |
| `src/ApiCallbackDownloadEventListener.cc` | 把引擎内部的 `DownloadEventListener` 接口适配成用户 C 回调的薄适配器 |
| `src/Notifier.cc` | 事件广播器：持有监听器列表，逐个 `onEvent` |
| `src/RequestGroupMan.cc` | 大多数下载事件的**产生点**（START/PAUSE/COMPLETE/ERROR/STOP） |
| `src/DefaultPieceStorage.cc` | BT 专属事件 `EVENT_ON_BT_DOWNLOAD_COMPLETE` 的产生点 |
| `src/DownloadEngine.cc` | `run(bool oneshot)`：库模式直接复用的主循环 |
| `src/KeepRunningCommand.cc` | `keepRunning` 配置背后的「永动机」命令 |
| `examples/libaria2ex.cc` | 单线程批下载示例（本讲主线） |
| `examples/libaria2wx.cc` | wxWidgets GUI 示例：下载线程 + UI 线程的双队列架构 |

## 4. 核心概念与源码讲解

### 4.1 aria2 API 总览：定位、类型与库/会话生命周期

#### 4.1.1 概念说明

libaria2 的定位写在公开头文件的开头注释里，值得逐句读：

> The aim of this library is provide same functionality available in RPC methods… we can take advantage of the direct, no latency, access to the aria2 core… this library is not meant to be the fine-grained, customizable, complete HTTP/FTP/BitTorrent library. If you are looking for such library for HTTP/FTP access, consider libcurl.

翻译成设计决策就是三条：

1. **功能对齐 RPC**：`addUri`、`changeOption`、`tellStatus`……你在 RPC 里能干的，会话 API 里基本都有同名操作，连选项校验规则都复用 u1-l5 讲过的 OptionHandler 体系。
2. **进程内直连**：不经过 HTTP/WebSocket、不有序列化开销，函数调用直达 `DownloadEngine`。
3. **不是协议库**：它给你的是「一个下载管理器」，不是「一个 HTTP 客户端」。想精细控制 HTTP 就去用 libcurl。

一个容易误解的点：这个 API 常被称作「C API」，但它实际是 **C++ API**——签名里满是 `std::vector<std::string>`、`std::string`（例如 `KeyVals` 就是 `std::vector<std::pair<std::string, std::string>>`，见 [src/includes/aria2/aria2.h:98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/includes/aria2/aria2.h#L98)）。宿主程序必须和 libaria2 用兼容的 C++ 编译器与标准库链接。

生命周期自顶向下分三层：

```text
进程级：libraryInit() ······················· libraryDeinit()
            │                                   ▲
会话级：sessionNew(options, config) ······· sessionFinal(session)
            │                                   ▲
循环级：run(session, mode) ←── 宿主反复调用 ──┘
```

每层都是「先Init后Final」的严格配对；**会话是进程级单例**，头文件明确写着 "only one Session object can be created per process"（[src/includes/aria2/aria2.h:205-206](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/includes/aria2/aria2.h#L205-L206)），两个示例源码里也都留了同义注释。

#### 4.1.2 核心流程

`libraryInit → sessionNew → （addUri / run / 查询）→ sessionFinal → libraryDeinit` 的装配流程：

```伪代码
libraryInit():
    初始化控制台(库模式)
    platform = new Platform()      # u1-l3：幂等初始化 OpenSSL/c-ares/libssh2 等
    关闭日志的控制台输出           # 库模式不打扰宿主的 stderr

sessionNew(options, config):
    session.context = new Context(standalone=false, 0, nullptr, options)
        # options 被当作「命令行参数的一部分」走同一套选项解析
    若 config.useSignalHandler == false: 关闭引擎内置信号处理
    reqinfo.prepare()              # ★ 与 CLI 的 execute() 第一棒完全相同：
                                   #   创建 DownloadEngine、装配 cookie/netrc/TLS…
    若 config.keepRunning:
        RequestGroupMan.setKeepRunning(true)
        引擎命令队列追加 KeepRunningCommand   # 让队列永不为空 → run 不退场
    若 config.downloadEventCallback:
        new ApiCallbackDownloadEventListener(...) 挂到 Notifier 单例
    返回 session（失败返回 nullptr）

sessionFinal(session):
    rv = reqinfo.getResult()       # ★ 与 CLI 的 execute() 第三棒相同
    delete session                 # 析构 Context，回收整套引擎
    return rv                       # 等价于 aria2c 的退出码
```

#### 4.1.3 源码精读

`Session` 结构本身就是「库模式的全部状态」——一个上下文加一个监听器，别无长物：

- [src/aria2api.h:49-54](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/aria2api.h#L49-L54)：`Session` 只有两个成员——`shared_ptr<Context>`（u1-l3 讲过的总装产物，内含 `reqinfo` 与 `DownloadEngine`）和 `unique_ptr<ApiCallbackDownloadEventListener>`（4.4 节的事件适配器）。
- [src/aria2api.cc:71-74](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/aria2api.cc#L71-L74)：`Session` 构造函数把 `false` 传给 `standalone`、空的 `argc/argv` 传进来——**库模式没有命令行**，宿主给的 `KeyVals options` 会被当成命令行参数的延伸去解析，所以 `dir=/tmp`、`max-connection-per-server=5` 这类写法照单全收。
- [src/aria2api.cc:90-102](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/aria2api.cc#L90-L102)：`libraryInit` 创建 `Platform`（u1-l3 讲过的 RAII 初始化器），并调用 `LogFactory::setConsoleOutput(false)`——库不该抢宿主的终端，日志只写文件。
- [src/aria2api.cc:110-145](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/aria2api.cc#L110-L145)：`sessionNew` 全文。三个看点：L124 调 `reqinfo->prepare()`——这就是 CLI `execute()` 的第一棒，证明「同一套引擎」不是口号；L129-133 处理 `keepRunning`（下节展开）；L134-139 把用户回调包成监听器挂进 `Notifier` 单例。`prepare()` 内部有一行 `SingletonHolder<Notifier>::instance(make_unique<Notifier>())`（[src/MultiUrlRequestInfo.cc:181](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L181)）——`Notifier` 单例正是在这里诞生的，所以挂监听器必须放在 `prepare()` 之后。
- [src/aria2api.cc:147-152](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/aria2api.cc#L147-L152)：`sessionFinal` 调 `getResult()` 取回**等价于 aria2c 退出码**的最终结果（error_code 枚举，0 = 全部成功），再 `delete session`。

#### 4.1.4 代码实践

**实践目标**：验证「一个进程只能有一个会话」与 `KeyVals` 选项的透传。

1. 抄下 [examples/libaria2ex.cc:36-37](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/examples/libaria2ex.cc#L36-L37) 的编译命令：`g++ -Wall -O2 -g -std=c++11 -o libaria2ex libaria2ex.cc -laria2`（需先构建并安装好 libaria2）。
2. 把示例中 `aria2::sessionNew(aria2::KeyVals(), config)` 的第一个参数改为 `aria2::KeyVals{{"dir", "/tmp"}, {"max-connection-per-server", "3"}}`。
3. 运行下载一个多镜像 URL，观察文件是否落在 `/tmp`。

**需要观察的现象**：文件出现在 `/tmp` 而非当前目录；日志（若配 `log=/tmp/a.log`）里连接数为 3。**预期结果**：`KeyVals` 与命令行选项完全同构。运行结果**待本地验证**（取决于本机是否安装了 libaria2）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `libraryInit` 要把 `LogFactory::setConsoleOutput(false)` 关掉，而 aria2c 不关？
**答案**：aria2c 独占终端，日志打印到控制台是特性；库嵌在宿主程序里，擅自写 stderr 会污染宿主输出，所以库模式默认只写日志文件，由宿主通过 `log`/`log-level` 选项自行开启。

**练习 2**：如果调用 `sessionNew` 两次会怎样？
**答案**：第二次创建的 `Context` 内部会再走一遍 `reqinfo->prepare()`，但头文件明确声明一个进程只允许一个 Session——端口绑定、`Notifier` 单例、全局 `Platform` 等都是进程级资源，第二个会话的行为未定义，应当避免。

### 4.2 任务操作与状态查询：addUri 与 DownloadHandle

#### 4.2.1 概念说明

会话建好后就进入「操作 + 查询」阶段。操作面（`addUri`/`addTorrent`/`addMetalink`/`removeDownload`/`pauseDownload`/`unpauseDownload`/`changeOption`…）与 RPC 方法一一对应；查询面统一收敛到一个类型：**`DownloadHandle`**——下载项的只读视图，通过 GID 获取。`A2Gid` 是 64 位整数（[src/includes/aria2/aria2.h:91](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/includes/aria2/aria2.h#L91)），u3-l1 讲过它是任务的持久标识，`gidToHex` 把它转成 RPC 里那个 16 位十六进制串。

`DownloadHandle` 的关键设计是**一个接口、两个实现**，分别对应任务的两种生存状态：

| 实现 | 包装的对象 | 何时返回 | `getStatus()` 的语义 |
| --- | --- | --- | --- |
| `RequestGroupDH` | 活着的 `RequestGroup`（含实时 `TransferStat` 快照） | 任务在等待/活动/暂停队列里 | ACTIVE / PAUSED / WAITING |
| `DownloadResultDH` | 已完结的 `DownloadResult`（u3-l2 讲过的只读快照） | 任务已收割进结果列表 | COMPLETE / REMOVED / ERROR |

#### 4.2.2 核心流程

`addUri` 的流程（其它 add* 同构）：

```伪代码
addUri(session, &gid, uris, options, position):
    requestOption = 引擎全局 Option 的副本          # 任务选项继承全局
    apiGatherRequestOption(requestOption, options):
        对每个 (name, value):
            查 OptionHandler；不存在或不带 initial-option 标志 → 静默忽略
            否则 handler.parse() 校验并写入        # 复用 u1-l5 校验通道
    createRequestGroupForUri(..., ignoreForceSeq=true, ignoreLocalPath=true)
        # ★ u3-l3 讲过的同一条任务创建流水线，CLI/RPC/库三端共用
    addRequestGroup: position>=0 ? 插到等待队列指定位置 : 追加到队尾
    gid 出参 = result.front()->getGID()
    return 0（选项非法返回 -1）
```

查询流程：

```伪代码
dh = getDownloadHandle(session, gid)
    先在 RequestGroupMan.findGroup(gid) 找活任务 → new RequestGroupDH
    找不到再 findDownloadResult(gid) 找结果 → new DownloadResultDH
    都没有 → nullptr
... 读 dh->getStatus() / getTotalLength() / getFiles() 等 ...
deleteDownloadHandle(dh)      # 手动释放，没有 RAII
```

#### 4.2.3 源码精读

- [src/aria2api.cc:209-217](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/aria2api.cc#L209-L217)：`apiGatherRequestOption` 用 `OptionHandler::getInitialOption` 做白名单过滤——这正是 u3-l3 讲过的「条目级选项白名单」：只有允许出现在 input-file 条目里的选项才能作为 per-request 选项传入，其余**静默忽略**（不是报错）。
- [src/aria2api.cc:265-289](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/aria2api.cc#L265-L289)：`addUri` 全文。L279 调用的 `createRequestGroupForUri` 与 CLI 处理 `-i` 文件、RPC 的 `aria2.addUri` 走的是**同一个** download_helper 工厂（u3-l3）；两个 `ignore*` 参数是库场景的特殊处理——跳过参数化 URI 展开、跳过本地路径嗅探。
- [src/aria2api.cc:253-263](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/aria2api.cc#L253-L263)：`addRequestGroup` 按 `position` 决定插入等待队列的位置——这就是 RPC `aria2.addUri(..., position)` 参数的落点。
- [src/aria2api.cc:658-677](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/aria2api.cc#L658-L677)：`RequestGroupDH` 的构造与 `getStatus()`——把 u3-l1 讲过的 `STATE_ACTIVE` 与 `isPauseRequested()` 翻译成 API 层的 `DOWNLOAD_ACTIVE/PAUSED/WAITING`。注意构造时 `ts(group->calculateStat())` 抓的是**瞬时快照**，速度类读数此后不再变化。
- [src/aria2api.cc:803-813](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/aria2api.cc#L803-L813)：`DownloadResultDH::getStatus()` 把退出码三分：`FINISHED → COMPLETE`、`REMOVED → REMOVED`、其余一律 `ERROR`。
- [src/aria2api.cc:870-887](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/aria2api.cc#L870-L887)：`getDownloadHandle` 的两级查找与 `deleteDownloadHandle`。返回裸指针、要求手动 delete，是为了保持 C 风格 ABI 的简单。

#### 4.2.4 代码实践

**实践目标**：用源码阅读验证「白名单静默忽略」行为。

1. 阅读 [src/aria2api.cc:196-206](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/aria2api.cc#L196-L206) 的 `apiGatherOption`，注意 `continue` 分支没有任何日志。
2. 对照 u1-l5 讲过的 `OptionHandlerFactory.cc`，确认 `max-connection-per-server` 带初始选项标志而 `rpc-listen-port` 之类不带。
3. （可选，待本地验证）写 10 行小程序：`addUri` 时传入 `{{"rpc-listen-port","7000"}}`，再 `dh->getOption("rpc-listen-port")` 查回——预期取不到该值，因为「上下文不可接受的选项」在入口就被忽略了。

**预期结果**：能说出「per-task 选项过滤规则与 `-i` 输入文件的条目选项完全一致」这一结论。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `RequestGroupDH::getDownloadSpeed()` 返回的是构造瞬间的速度，而连续两次调用同一个 handle 不会更新？
**答案**：`TransferStat ts` 在构造函数里只求值一次（[src/aria2api.cc:659-662](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/aria2api.cc#L659-L662)）。要刷新读数必须 `deleteDownloadHandle` 后重新 `getDownloadHandle`——示例程序每 500ms 正是这么做的。

**练习 2**：任务刚因校验失败结束时，`getDownloadHandle` 返回哪种实现？`getErrorCode()` 值是多少？
**答案**：返回 `DownloadResultDH`（任务已收割进 `DownloadResults`）；`getErrorCode()` 返回 `dr->result`，即 u9-l3 将讲的 error_code 枚举值——校验失败为 32（`CHECKSUM_ERROR`）。

### 4.3 session/run 循环：阻塞模型与两种运行模式

#### 4.3.1 概念说明

`run` 是库模式的心跳。两种模式的语义（[src/includes/aria2/aria2.h:225-253](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/includes/aria2/aria2.h#L225-L253)）：

- **`RUN_DEFAULT`**：阻塞直到「没有下载可做」，返回 0。适合批下载器——扔进一堆 URI，一口气跑完。
- **`RUN_ONCE`**：做一轮事件轮询（当前实现约 1 秒超时）就返回。返回 1 表示「还有活没干完，请再调我」；返回 0 表示全部结束。适合 GUI/服务程序——宿主每轮在返回间隙干自己的事（处理消息队列、刷新界面）。

而「没有下载可做」的判据，就是 u2-l1 讲过的主循环退出条件：`commands_` 与 `routineCommands_` 两条队列**同时为空**。这里藏着一个关键问题：一个下载完的批处理任务，队列自然会排空；但一个「随时可能收到新任务的常驻服务」队列永远不会自然排空——它需要一个永不离场的命令占位，这就是 `keepRunning` 与 `KeepRunningCommand` 的分工。

#### 4.3.2 核心流程

```伪代码
aria2::run(session, mode):
    return engine.run(mode == RUN_ONCE)      # 一行转发

DownloadEngine::run(oneshot):                # u2-l1 的主循环 + oneshot 扩展
    while (commands_ 或 routineCommands_ 非空):
        waitData()                            # 事件轮询（约 1 秒超时）
        executeCommand(commands_)             # 按状态过滤执行
        executeCommand(routineCommands_)
        afterEachIteration()                  # Ctrl-C 状态机等
        if oneshot and !noWait_: return 1     # ★ RUN_ONCE 的一轮到此为止
    onEndOfRun()
    return 0                                  # ★ 队列排空：真的结束了

KeepRunningCommand::execute():               # 永动机
    if 引擎已被要求停机: return true           # 自我销毁，放行退出
    把自己重新塞回命令队列; return false       # 队列永不为空

shutdown(session, force):
    engine.requestHalt() 或 requestForceHalt() # 置停机标志
    engine.setNoWait(true)                     # 跳过下一次轮询等待
    # KeepRunningCommand 下一轮看到停机标志自我销毁
    # → 队列排空 → run 返回 0 → 宿主的 RUN_ONCE 循环自然break
```

停机的因果链是本模块最精巧的部分：`shutdown()` 本身不杀线程，它只是置标志；真正退出靠 `KeepRunningCommand` 察觉标志后不再自我重排，让队列漏空，`run` 才返回 0。

#### 4.3.3 源码精读

- [src/aria2api.cc:154-158](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/aria2api.cc#L154-L158)：库的 `run` 只有一行——取 `DownloadEngine` 并转发 `mode == RUN_ONCE` 布尔值。**主循环没有为库模式复制一份**，u2-l1 精读的正是同一段代码。
- [src/DownloadEngine.cc:158-185](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L158-L185)：`DownloadEngine::run(bool oneshot)`。对比 u2-l1，多出的三处都与 oneshot 有关：L160 的 `GlobalHaltRequestedFinalizer` 在 oneshot 模式下**不会**在析构时把 `globalHaltRequested` 置 5（因为宿主稍后还会再调 run，不能残留「强制退出」状态）；L179-181 的 `if (!noWait_ && oneshot) return 1` 是 RUN_ONCE 的返回点；循环自然走完落到 L184 返回 0。
- [src/aria2api.cc:129-133](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/aria2api.cc#L129-L133)：`sessionNew` 里 `keepRunning` 的装配——先 `setKeepRunning(true)`（防止任务清空时引擎自作主张收尾），再追加一条 `KeepRunningCommand` 进队列。
- [src/KeepRunningCommand.cc:41-56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/KeepRunningCommand.cc#L41-L56)：`KeepRunningCommand` 全文仅十几行，构造时 `setStatusRealtime()`（u2-l3 讲过的实时状态位，保证每轮都执行）；`execute()` 检查 `isHaltRequested()`——未停机就 `addCommand` 把自己塞回去并返回 false（u2-l3 的约定：返回 false 必须已自我重排）。
- [src/aria2api.cc:160-173](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/aria2api.cc#L160-L173)：`shutdown`。L171 的 `setNoWait(true)` 注释解释了缘由：跳过下一次轮询超时，避免队列里只剩 KeepRunningCommand 时白等 1 秒。
- [examples/libaria2ex.cc:106-110](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/examples/libaria2ex.cc#L106-L110)：批下载器的循环骨架——`run(RUN_ONCE)` 返回非 1 即 break。注意它**没有**设 `keepRunning`：批任务队列终会排空。
- [examples/libaria2wx.cc:383-388](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/examples/libaria2wx.cc#L383-L388)：GUI 下载线程的对照写法——`config.keepRunning = true`，同样的 `run(RUN_ONCE)` 死循环，退出只能靠 `ShutdownJob` 触发 `aria2::shutdown`。

#### 4.3.4 代码实践

**实践目标**：用两个示例对照验证 `keepRunning` 对 `run` 返回值的影响。

1. 阅读 [examples/libaria2ex.cc:87-110](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/examples/libaria2ex.cc#L87-L110)：默认 `SessionConfig`（`keepRunning=false`），下载全部完成后 `run(RUN_ONCE)` 返回 0，循环退出。
2. 在 ex 示例的 `SessionConfig config;` 后加一行 `config.keepRunning = true;` 重新编译。
3. 重新运行同一个下载，观察程序行为。

**需要观察的现象**：改完后文件下载完了，但进程**不再退出**——`run(RUN_ONCE)` 永远返回 1，需要 Ctrl-C（触发内置信号处理 → `shutdown(false)`）才能结束。**预期结果**：一行配置就把「批处理器」变成「常驻服务」，印证 `KeepRunningCommand` 占位机制。运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`RUN_ONCE` 模式下宿主两次 `run` 调用之间的间隙里，socket 上的数据会不会丢？
**答案**：不会。数据滞留在内核 socket 接收缓冲区（u2-l4 讲过的 SocketRecvBuffer 之下的层级），下一轮 `run` 的 `waitData()` 会立即报告可读事件并处理；只是「处理延迟」等于宿主的间隙时长。

**练习 2**：`shutdown(session, false)` 与 `shutdown(session, true)` 最终分别让 `run` 怎样返回？对应 u1-l3 讲过的哪种停机语义？
**答案**：`false → requestHalt()` 是优雅停机（等当前命令收尾、保存 .aria2 控制文件），`true → requestForceHalt()` 是强制停机；两者都让 `KeepRunningCommand` 在下一轮自我销毁、队列排空，`run` 返回 0。对应命令行「首按 Ctrl-C 优雅停止、再按强制退出」的两段式语义。

**练习 3**：为什么 `GlobalHaltRequestedFinalizer` 在 oneshot 模式不把 `globalHaltRequested` 置 5？
**答案**：置 5 表示「进程级强制退出已敲定」。RUN_ONCE 下 `run` 会被反复调用，若第一次返回时残留该状态，后续调用会被误判为强制停机而无法继续正常下载；RUN_DEFAULT 下 `run` 只调用一次且返回即终结，置 5 才是安全收尾。

### 4.4 事件监听器：从引擎事件点到你的回调函数

#### 4.4.1 概念说明

任务完成时怎么通知宿主？轮询 `getDownloadHandle` 固然可行，但 libaria2 提供了更优雅的**推模式**：`SessionConfig.downloadEventCallback` 注册一个函数指针，引擎在事件发生的精确时刻回调它。六种事件（[src/includes/aria2/aria2.h:105-131](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/includes/aria2/aria2.h#L105-L131)）：

| 事件 | 含义 | 产生位置 |
| --- | --- | --- |
| `EVENT_ON_DOWNLOAD_START` | 任务被提升为活动 | `RequestGroupMan::fillRequestGroup` |
| `EVENT_ON_DOWNLOAD_PAUSE` | 任务被暂停 | `RequestGroupMan::removeStoppedGroup` |
| `EVENT_ON_DOWNLOAD_STOP` | 任务被移除（REMOVED） | `RequestGroupMan` 的 stop hook |
| `EVENT_ON_DOWNLOAD_COMPLETE` | 任务完成（FINISHED） | `RequestGroupMan` 的 stop hook |
| `EVENT_ON_DOWNLOAD_ERROR` | 任务失败（其它错误码） | `RequestGroupMan` 的 stop hook |
| `EVENT_ON_BT_DOWNLOAD_COMPLETE` | BT 下载完（可能继续做种） | `DefaultPieceStorage` |

注意 BT 的特殊性：piece 全齐时先发 `EVENT_ON_BT_DOWNLOAD_COMPLETE`（此时任务还在做种、尚未「完结」），等做种结束整组收割时才发 `EVENT_ON_DOWNLOAD_COMPLETE`。这正对应 u6-l10 讲的「下载完成 ≠ 任务结束」。

**回调线程纪律**：回调在调用 `run()` 的线程里同步执行。回调里不能阻塞、不能再入 `run`；在多线程程序里，回调若想触碰 UI，必须经队列转发（wx 示例的 `NotifyQueue` 就是范本）。

#### 4.4.2 核心流程

注册与触发的完整链路：

```伪代码
【注册期：sessionNew】
config.downloadEventCallback = 我的函数; config.userData = &我的上下文
→ new ApiCallbackDownloadEventListener(session, 我的函数, userData)
→ Notifier 单例.listeners_.push_back(监听器)

【触发期：run() 内部，某轮循环】
引擎判定某任务完结（executeStopHook）
→ RequestGroupMan.notifyDownloadEvent(EVENT_ON_DOWNLOAD_COMPLETE, group)
→ Notifier::notifyDownloadEvent: for 每个 listener: listener->onEvent(event, group)
→ ApiCallbackDownloadEventListener::onEvent:
      callback_(session_, event, group->getGID(), userData_)
→ ★ 进入宿主的 C 函数：switch(event) { ... }
   此时可直接 getDownloadHandle(session, gid) 查详情
```

同一条 `Notifier` 广播线上还挂着别的监听器——u8-l3 讲过 RPC 模式的 `WebSocketSessionMan` 也监听同一批事件并翻译成 `aria2.onDownloadComplete` 通知。**一个事件源，多种消费者**，这正是 Notifier 单例存在的意义。

#### 4.4.3 源码精读

- [src/aria2api.cc:134-139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/aria2api.cc#L134-L139)：注册点——`sessionNew` 里创建适配器并 `addDownloadEventListener` 挂进 `Notifier` 单例。回调为空则整个监听器都不创建，零开销。
- [src/ApiCallbackDownloadEventListener.cc:40-52](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ApiCallbackDownloadEventListener.cc#L40-L52)：适配器全文。`onEvent` 一行核心：把引擎内部 `(event, RequestGroup*)` 签名翻译成 C 回调的 `(session, event, gid, userData)` 四参签名——`RequestGroup*` 不出库，只放出 GID。
- [src/Notifier.cc:45-56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Notifier.cc#L45-L56)：广播器——保存监听器列表，`notifyDownloadEvent` 简单 for 循环逐个调用，同步、有序、无过滤。
- [src/RequestGroupMan.cc:255-285](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L255-L285)：`executeStopHook`——COMPLETE/ERROR/STOP 三事件的总产生点。L272-274 先执行 `--on-download-complete` 等外部钩子脚本，L276-284 按退出码三分发：`FINISHED → COMPLETE`，`IN_PROGRESS/REMOVED 之外 → ERROR`，其余（即 REMOVED）→ `STOP`。
- [src/RequestGroupMan.cc:580-582](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L580-L582)：`EVENT_ON_DOWNLOAD_START` 的产生点——任务从等待队列被提升为活动时（u3-l1 的 `fillRequestGroup` 路径）。
- [src/RequestGroupMan.cc:445-452](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L445-L452)：`EVENT_ON_DOWNLOAD_PAUSE` 的产生点——暂停的任务被移出活动队列时。
- [src/DefaultPieceStorage.cc:499-507](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L499-L507)：`EVENT_ON_BT_DOWNLOAD_COMPLETE` 的产生点——u5-l2 讲过 `DefaultPieceStorage` 判定下载完成的时刻；发完事件紧接着 `enableSeedOnly()`（u6-l10 的做种切换）。
- [examples/libaria2ex.cc:43-75](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/examples/libaria2ex.cc#L43-L75)：宿主侧回调范本——switch 只关心 COMPLETE/ERROR，其余 `return 0`；拿到 gid 后立即 `getDownloadHandle` 取文件路径打印，**用完必 `deleteDownloadHandle`**。L62-67 的判空注释值得注意：完成事件发生时文件名理论上已定，但回调里仍防御了 path 为空的情形。
- [examples/libaria2wx.cc:366-375](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/examples/libaria2wx.cc#L366-L375)：跨线程转发的范本——`Notification` 抽象让下载线程把「该干什么」打包扔进 `NotifyQueue`，UI 线程在 wxTimer 里取出执行，避免回调直接碰 GUI。

#### 4.4.4 代码实践

**实践目标**：数清一次 BT 下载里两类「完成」事件的先后。

1. 找一个种子文件，改写 ex 示例的回调：为全部六种事件各加一行 `std::cerr << 事件名 << std::endl;`（在 [examples/libaria2ex.cc:46-55](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/examples/libaria2ex.cc#L46-L55) 的 switch 里补 case）。
2. 用 `aria2::addTorrent(session, nullptr, "x.torrent", {})` 替换 addUri 部分。
3. 运行并记录事件顺序（配合 `--seed-time=0` 选项放进 sessionNew 的 KeyVals，让做种立刻结束）。

**需要观察的现象**：`START →（下载中）→ BT_DOWNLOAD_COMPLETE → COMPLETE` 的序列；若不加 `seed-time=0`，最后两个事件之间会隔整个做种期。**预期结果**：亲手验证「piece 齐 → BT 完成事件 → 做种 → 收割 → COMPLETE」两段式。运行结果**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：回调里能调用 `addUri` 吗？有什么风险？
**答案**：技术上可以——回调就发生在引擎线程的 `run()` 内部，`addUri` 只是往 `RequestGroupMan` 的等待队列追加任务并触发队列检查，不需要再入 `run`。风险在于回调处于引擎两轮命令执行之间，此时做任何耗时操作都会拖住整个事件循环；稳妥做法是把「要加的新任务」排队，回到宿主自己的 RUN_ONCE 循环间隙再调 `addUri`（wx 示例的 JobQueue 模式）。

**练习 2**：`EVENT_ON_DOWNLOAD_STOP` 与 `EVENT_ON_DOWNLOAD_ERROR` 的互斥条件是什么？
**答案**：按 [src/RequestGroupMan.cc:276-284](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L276-L284)：`FINISHED → COMPLETE`；结果不是 `IN_PROGRESS` 也不是 `REMOVED → ERROR`；`REMOVED → STOP`。即「被用户移除」走 STOP，「自己失败」走 ERROR。

**练习 3**：不注册回调的进程里，`Notifier` 广播会不会白跑一趟？
**答案**：libaria2 场景下监听器列表为空，for 循环零次迭代，几乎无开销；但 RPC 模式下同一广播会驱动 WebSocket 通知（u8-l3），所以广播调用本身无条件执行，省的只是每个监听器的处理。

### 4.5 两个官方示例的架构对照与会话 API vs RPC API

#### 4.5.1 概念说明

两个示例代表 libaria2 的两种典型宿主形态：

| | `libaria2ex.cc` | `libaria2wx.cc` |
| --- | --- | --- |
| 形态 | 单线程批下载器 | 多线程 GUI |
| `keepRunning` | false（默认） | true |
| `run` 返回 0 的时机 | 全部下载完 | 仅当收到 `shutdown` |
| 新任务来源 | argv，一次性加完 | UI 线程经 `JobQueue` 随时投递 |
| 状态回显 | RUN_ONCE 间隙直接打印 | `NotifyQueue` 转发给 UI 线程 |
| 退出方式 | 队列自然排空 | 关窗 → `ShutdownJob` → `shutdown(true)` |

wx 示例的核心教益是**线程纪律**：libaria2 不是线程安全的，所有 API 调用都收敛到下载线程；UI 线程与下载线程之间用两条互斥队列（`JobQueue` 下行、`NotifyQueue` 上行）通信，锁的粒度恰好是「一次 push/pop」。

会话 API 与 RPC API 的选型：

| 维度 | 会话 API（libaria2） | RPC API（u8-l3/u8-l4） |
| --- | --- | --- |
| 部署 | 链接进同一进程 | 独立 aria2c 进程/容器 |
| 语言 | 必须 C++（编译器/标准库需兼容） | 任意能说 HTTP/JSON 或 XML 的语言 |
| 延迟与开销 | 函数调用级，无序列化 | 本地回环或网络往返 + JSON/XML 编解码 |
| 事件 | C 回调，同步推送 | WebSocket 通知 `aria2.onXxx` |
| 隔离性 | aria2 崩溃即宿主崩溃 | 进程隔离，可独立升级重启 |
| 典型场景 | 桌面下载器内嵌、游戏资源预取、CLI 工具 | Web 前端面板、远程调度、多客户端共享 |

#### 4.5.2 核心流程

wx 示例的双队列数据流：

```伪代码
UI 线程                                     下载线程 downloaderJob()
──────                                     ─────────────────────
Add URI 对话框确认                            for(;;):
  jobq_.push(AddUriJob)          ──JobQueue──▶   run(session, RUN_ONCE) 返回 1
                                               while jobq 非空: job.execute(session)
wxTimer 每 900ms                              每 900ms: 汇总各 gid 的 DownloadHandle
  while notifyq 非空:              ◀─NotifyQueue─ push(DownloadStatusNotification)
    nt->notify(this) 渲染进度
关闭窗口                                     push(ShutdownJob)
  jobq_.push(ShutdownJob)        ──JobQueue──▶   shutdown(session, force=true)
  downloaderThread_.join()                      → run 返回 0 → 跳出 for
                                               sessionFinal(session)
                                               push(ShutdownNotification) → UI 真正关窗
```

#### 4.5.3 源码精读

- [examples/libaria2ex.cc:77-104](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/examples/libaria2ex.cc#L77-L104)：批下载器初始化——`libraryInit` → 构造 `SessionConfig` 并只改一个成员 `downloadEventCallback` → `sessionNew` → 循环 `addUri`（每个 URI 一个独立任务，出参 gid 这里传 `nullptr` 表示不关心）。
- [examples/libaria2ex.cc:105-144](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/examples/libaria2ex.cc#L105-L144)：主循环——`run(RUN_ONCE)` 驱动引擎；每 500ms 用 `getGlobalStat` 打印全局吞吐、用 `getActiveDownload` + `getDownloadHandle` 逐任务打印进度，演示「RUN_ONCE 间隙做查询」的标准姿势。L111-112 的注释点明：这里也是运行中追加 URI/查询的正确时机。
- [examples/libaria2ex.cc:145-147](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/examples/libaria2ex.cc#L145-L147)：收尾——`sessionFinal` 的返回值直接作为进程退出码（它就是 aria2c 的 exit status）。
- [examples/libaria2wx.cc:70-98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/examples/libaria2wx.cc#L70-L98)：`SynchronizedQueue`——`std::queue` 加互斥锁的最小封装，注释说明「单消费者前提下 empty/pop 分离才安全」，是理解示例并发假设的钥匙。
- [examples/libaria2wx.cc:101-123](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/examples/libaria2wx.cc#L101-L123)：两种 `Job`——`ShutdownJob::execute` 调 `aria2::shutdown`；`AddUriJob::execute` 调 `aria2::addUri`。Job 模式把「任意 API 调用」变成可排队的一等公民。
- [examples/libaria2wx.cc:377-429](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/examples/libaria2wx.cc#L377-L429)：`downloaderJob` 全文——下载线程的完整生命周期。注意 L424 `sessionFinal` 之后还要 push 一条 `ShutdownNotification`（L427），因为终端 Ctrl-C 走的是内置信号处理 → shutdown 路径，UI 线程需要被通知「下载线程真的退了」才能安全关窗。
- [examples/libaria2wx.cc:272-280](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/examples/libaria2wx.cc#L272-L280)：关窗处理——`join` 等待下载线程完成优雅停机（保存控制文件、序列化会话）后才 `Destroy()`，注释明确指出这一步的意义。

#### 4.5.4 代码实践

**实践目标**：把 wx 示例的双队列架构迁移到无 GUI 的生产者-消费者场景。

1. 只读练习：对照 [examples/libaria2wx.cc:213](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/examples/libaria2wx.cc#L213)（构造函数里 `std::thread downloaderThread_(downloaderJob, ...)` 启动下载线程）与 [examples/libaria2wx.cc:272-280](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/examples/libaria2wx.cc#L272-L280)，画出三条时间线：UI 线程、下载线程、aria2 引擎内部（同在下载线程）。
2. 标出每次锁竞争发生的位置（`jobq_.push/pop`、`notifyq_.push/pop`），确认锁从不嵌套。
3. 思考题落笔：若宿主已有自己的事件循环（如 glib、Node.js addon），应该选 ex 的「RUN_ONCE 内联」模式还是 wx 的「独立线程」模式？

**预期结果**：能画出双队列时序图并说明「所有 aria2 API 调用都在下载线程」这一不变量如何被两条队列维护。

#### 4.5.5 小练习与答案

**练习 1**：wx 示例中若 UI 线程直接调用 `aria2::addUri`（不走 JobQueue）会有什么问题？
**答案**：libaria2 非线程安全：`addUri` 会写 `RequestGroupMan` 的等待队列并触发队列检查，与下载线程正在进行的队列操作构成数据竞争，可能损坏容器或产生不可复现的崩溃。必须经 `JobQueue` 把调用转移到下载线程。

**练习 2**：为什么 wx 示例的 `downloaderJob` 用 `config.keepRunning = true`，而 ex 示例不需要？
**答案**：GUI 是常驻程序，空闲时（无任何任务）也要让 `run(RUN_ONCE)` 持续返回 1 以维持心跳，等待 UI 随时投递新任务——这靠 `KeepRunningCommand` 占住队列实现；ex 是一次性批处理，队列自然排空即该退出，占位反而让进程挂着不走。

**练习 3**：若要把 aria2 嵌进一个 Python 服务，选会话 API 还是 RPC API？为什么？
**答案**：通常选 RPC API。会话 API 是 C++ 接口，跨语言绑定成本高且受编译器/ABI/线程模型制约；RPC 用 HTTP+JSON 即可接入任意语言，代价只是序列化与一次本地回环往返。只有当宿主本身是 C++ 且需要极限低延迟（如每秒成千上万次查询）时，会话 API 才是更优解。

## 5. 综合实践

**任务**：把 `examples/libaria2ex.cc` 改造成一个「三任务、事件驱动、自动退出」的小下载器。这个任务贯穿本讲全部三个最小模块：会话生命周期、run 循环阻塞模型、事件回调。

**要求**：

1. 命令行不再接收 URI，硬编码 3 个下载 URI（建议一个正常 HTTP、一个故意 404 的 URL、再一个正常 URL，以便同时观察 COMPLETE 与 ERROR）。
2. 在回调中打印**每一种**收到的事件：事件名 + `gidToHex(gid)` + 首文件路径或首个 URI（参照 [examples/libaria2ex.cc:43-75](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/examples/libaria2ex.cc#L43-L75) 的写法扩展 switch）。
3. 用一个静态计数器（经 `userData` 传入更佳）统计「已终结的任务数」（收到 COMPLETE 或 ERROR 即算终结）；在主循环的 RUN_ONCE 间隙检查，计数达到 3 时调用 `aria2::shutdown(session, false)` 而不是死等。
4. `sessionFinal` 返回值原样作为 `main` 返回值，验证 404 任务会让退出码非 0（对照 u9-l3 的 error_code：`HTTP_SERVICE_UNAVAILABLE`/404 相关错误码）。

**参考骨架**（示例代码，非项目原有）：

```cpp
struct Ctx { int finished = 0; };

int cb(aria2::Session* session, aria2::DownloadEvent ev,
       aria2::A2Gid gid, void* ud) {
  auto* ctx = static_cast<Ctx*>(ud);
  // 打印事件名与 gid；COMPLETE/ERROR 时 ++ctx->finished
  return 0;
}

int main() {
  aria2::libraryInit();
  aria2::SessionConfig cfg;
  Ctx ctx;
  cfg.downloadEventCallback = cb;   // 注册回调（4.4）
  cfg.userData = &ctx;
  aria2::Session* s = aria2::sessionNew(aria2::KeyVals(), cfg);
  for (auto& u : {u1, u2_bad, u3})  // 三个 URI（4.2 addUri）
    aria2::addUri(s, nullptr, {u}, aria2::KeyVals());
  for (;;) {                         // RUN_ONCE 循环（4.3）
    if (aria2::run(s, aria2::RUN_ONCE) != 1) break;
    if (ctx.finished == 3) aria2::shutdown(s, false);
  }
  int rv = aria2::sessionFinal(s);   // rv 即退出码
  aria2::libraryDeinit();
  return rv;
}
```

**验证要点**：3 个 START 事件；2 个 COMPLETE + 1 个 ERROR；`shutdown` 后循环在一次 RUN_ONCE 内退出；进程退出码非 0。编译命令沿用示例文件头注释：`g++ -Wall -O2 -g -std=c++11 -o mydown mydown.cc -laria2`。完整运行结果**待本地验证**（需要本机已构建并安装 libaria2；若尚未安装，回到 u1-l2 的构建流程，确认 configure 输出里库目标已启用）。

## 6. 本讲小结

- libaria2 与 aria2c 共用同一颗心脏：`sessionNew` 内部调用的 `reqinfo->prepare()`、`run` 转发的 `DownloadEngine::run`、`sessionFinal` 调用的 `getResult()`，正是 CLI `execute()` 三段骨架的拆开形态；任务创建更直接复用 u3-l3 的 download_helper 工厂。
- 会话是进程级单例；生命周期三层配对：`libraryInit/libraryDeinit`（Platform 与日志）、`sessionNew/sessionFinal`（Context 与引擎）、`run`（可反复调用的心跳）。
- `RUN_ONCE` 每轮返回 1 表示「还有活」，返回 0 表示两条命令队列已排空；`keepRunning` 靠一条自我重排的 `KeepRunningCommand` 占住队列，把批处理器变成常驻服务，`shutdown` 置停机标志令其自我销毁、队列漏空、`run` 归零。
- 查询面是「一接口两实现」的 `DownloadHandle`：活任务包装 `RequestGroup`（实时快照），完结任务包装 `DownloadResult`；用完必须 `deleteDownloadHandle`。
- 事件链路：引擎在 `RequestGroupMan`（START/PAUSE/COMPLETE/ERROR/STOP）与 `DefaultPieceStorage`（BT 完成）产生事件 → `Notifier` 单例广播 → `ApiCallbackDownloadEventListener` 适配成 C 回调四参签名；回调在 `run` 线程同步执行，跨线程需自行排队（wx 示例的双队列是范本）。
- 选型口诀：同进程 C++ 且要低延迟选会话 API；跨语言/跨进程/要隔离选 RPC API——两者功能对齐，都通向同一套引擎。

## 7. 下一步学习建议

- **u9-l6（二次开发实战）** 是本讲的自然续篇：libaria2 的「选项驱动一切」意味着给 aria2 加新能力后，宿主程序只需在 `KeyVals` 里传新选项即可享用；那一讲会走完「注册选项→改变行为」的全流程。
- 想继续深挖事件体系，可精读 `src/Notifier.h` 与 u8-l3 的 `WebSocketSessionMan`，观察同一个 `Notifier` 单例如何同时服务 C 回调与 WebSocket 通知两种消费者。
- 想理解 `run()` 每一轮内部发生了什么，回到 u2-l1 的 `DownloadEngine` 主循环与 u2-l2 的 `EventPoll`；库模式没有任何特殊路径，那里就是全部真相。
- 若你打算真的写一个内嵌 aria2 的程序，建议通读 [src/includes/aria2/aria2.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/includes/aria2/aria2.h) 全文——它的注释就是最权威的 API 文档，本讲只覆盖了最核心的三分之一。
