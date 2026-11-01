# Command 体系：命令的生命周期与状态机

## 1. 本讲目标

上一讲（u2-l1）我们看到 `DownloadEngine::run()` 不断从队列里取 Command 执行，本讲钻进这些 Command 内部。学完本讲你应该能够：

1. 说出 `Command` 基类的状态位（STATUS_*）与 I/O 事件标志（readEvent_ 等）各自的作用，以及 `transitStatus()` 的「执行一次即降级」设计。
2. 跟踪 `AbstractCommand::execute()` 这个模板方法骨架的五道关卡，理解连接建立 → 发送请求 → 读取响应 → 下载数据四个阶段如何由不同 Command 接力完成。
3. 区分 Command 的三类出口：正常返回 `true` 自灭、`DlRetryEx` 可重试回路、`DlAbortEx`/`DownloadFailureException` 终止回路。
4. 理解 `HttpDownloadCommand` 这个「数据泵」为什么返回 `false` 反复自我重排，以及 socket 事件回调（只置位）与命令重排（addCommandSelf）如何配合。
5. 会用日志（必要时加一行打印）统计一次 HTTP 下载中各类命令的出现次数，亲手验证流水线的接力过程。

## 2. 前置知识

本讲建立在 u2-l1（DownloadEngine 主循环）和 u2-l2（EventPoll）之上，先回顾并补充几个概念：

- **命令队列与状态过滤**：`DownloadEngine` 有两条队列——`commands_`（任务命令）和 `routineCommands_`（周期命令）。每轮循环平时只用 `STATUS_ACTIVE` 过滤器执行命令，每隔约 1 秒做一次全量刷新（`STATUS_ALL`），唤醒睡眠中的命令检查超时（详见 u2-l1）。
- **事件只置位、不执行**：EventPoll 的回调（`CommandEvent`）被触发时只调用命令的 `readEventReceived()` 等方法设置布尔标志，真正的工作推迟到命令被 `executeCommand` 选中执行时才做（详见 u2-l2）。本讲要讲清「标志谁读、何时清」。
- **cuid**：每个 Command 构造时带一个 64 位整数编号（`cuid_t`），同一条下载流水线（同一连接上的接力命令们）共用同一个 cuid，因此日志里 `CUID#7` 就是同一条流水线的活动轨迹。
- **execute() 的返回值约定**（u2-l1 的核心约定，本讲反复用到）：
  - 返回 `true`：命令生命周期结束，`DownloadEngine` 用 `com.reset()` 销毁它；
  - 返回 `false`：命令必须**已经**通过 `addCommandSelf()`（内部 `e_->addCommand(std::unique_ptr<Command>(this))`）把自己重新塞回队列，引擎侧则 `com.release()` 放弃所有权。
- **异常三兄弟**：`DlRetryEx`（可重试错误，如超时、连接被拒）、`DlAbortEx`（不可恢复错误，如 DNS 解析失败）、`DownloadFailureException`（致命错误，整个任务组停机）。它们都携带 `error_code` 枚举值。
- **Segment（分段）**：把大文件按 `--min-split-size` 等规则切成的一段字节区间，由 `SegmentMan` 统一分配给各条连接，`segments_` 里存的就是当前命令「租借」到的分段（SegmentMan 细节在 u3-l4 专讲）。

还有一个理解本讲的钥匙：**aria2 的命令不是线程，而是一个个「待办事项对象」**。命令不阻塞等待网络，而是「有事可做就做一点，没事就把自己重新排队睡觉」。这就是所谓的状态机式异步编程。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/Command.h` / `src/Command.cc` | 命令基类：cuid、STATUS 状态位、四个 I/O 事件标志及其置位/清除 |
| `src/AbstractCommand.h` / `src/AbstractCommand.cc` | 所有流式下载命令（HTTP/FTP/SFTP 及代理变体）的公共基类，`execute()` 模板方法骨架、三个异常出口、读/写关注注册 |
| `src/DownloadCommand.cc` | 数据泵基类：从 socket 收数据、经 StreamFilter 写盘、判定 segment 完成 |
| `src/TimeBasedCommand.h` | Command 族的另一分支：周期命令（AutoSave、DHT 交互等）的三段式 execute |
| `src/DownloadEngine.cc` | 只看一处：`executeCommand()` 如何按状态过滤、执行、销毁/放回命令（u2-l1 已讲循环，这里看单条命令的调度细节） |
| `src/CreateRequestCommand.cc` | 重试回路的起点：从 URI 池取 Request、创建第一棒连接命令 |
| `src/HttpInitiateConnectionCommand.cc` / `src/HttpRequestCommand.cc` / `src/HttpResponseCommand.cc` | 四段接力的前三棒：建连接 → 发请求 → 处理响应头并创建数据泵（细节在 u4-l1 展开，本讲只看接力点） |

## 4. 核心概念与源码讲解

### 4.1 Command 基类：状态位与 I/O 事件标志

#### 4.1.1 概念说明

`Command` 是 aria2 中一切调度单元的根：它只有一个纯虚方法 `execute()`，外加两组数据——

1. **STATUS 状态位**：告诉 `DownloadEngine`「我该以什么频率被调度」。
2. **四个 I/O 事件标志**（`readEvent_`、`writeEvent_`、`errorEvent_`、`hupEvent_`）：EventPoll 回调的「留言板」，命令下次被调度时读取留言决定做什么。

#### 4.1.2 核心流程

STATUS 枚举的语义（数值递增，越靠后越「急」）：

```
STATUS_ALL(0)  ← 仅作为过滤器的哨兵值，不会被赋给命令
STATUS_INACTIVE(1)        睡眠：只在每秒一次的全量刷新时被执行
STATUS_ACTIVE(2)          活跃：每轮循环都执行
STATUS_REALTIME(3)        实时：常驻实时（如 c-ares DNS 轮询）
STATUS_ONESHOT_REALTIME(4) 一次性实时：下一轮立刻执行，之后被 transitStatus 降级
```

判断命令是否该被执行用的是**数值比较**：`statusFilter <= status_`。所以用 `STATUS_ACTIVE` 过滤时，ACTIVE/REALTIME/ONESHOT 都会命中；用 `STATUS_ALL`（0）过滤时人人命中。

配合的调度规则（引擎侧，见 4.1.3 源码）：

```
每轮 executeCommand(commands_, filter):
    取出队首命令 com
    若 statusMatch(filter) 不成立 → 放回队尾，跳过
    com->transitStatus()          # REALTIME 保持，其余降为 INACTIVE
    若 com->execute() 返回 true   # 命令自我了断
        销毁 com
    否则                          # 命令已把自己重新入队
        com->clearIOEvents()      # 清掉本轮事件留言
        com.release()             # 放弃所有权（队列里那份才是正主）
```

`transitStatus()` 的「执行一次即降级到 INACTIVE」是个精心设计：命令如果想持续活跃，必须在 `execute()` 里**每轮重新激活自己**（如 `setStatusActive()`）或依赖事件标志唤醒；否则自动睡去，由每秒一次的全量刷新兜底。这防止了任何命令变成忙等循环。

#### 4.1.3 源码精读

Command 的状态与事件标志定义：

- [src/Command.h:L44-L62](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Command.h#L44-L62) —— `Command` 类声明：构造参数是 `cuid_t`；`STATUS` 枚举列出五个状态；四个布尔事件标志是私有成员。
- [src/Command.h:L82-L93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Command.h#L82-L93) —— `setStatusActive()` 等内联状态迁移函数，以及关键的 `statusMatch()`：`statusFilter <= status_`，用枚举数值大小实现「过滤器以下全部命中」。

事件标志的置位与清除：

- [src/Command.cc:L62-L76](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Command.cc#L62-L76) —— `readEventReceived()` 等四个方法只做一件事：把对应标志置 true（EventPoll 回调调用它们）；`clearIOEvents()` 把四个标志全部复位。
- [src/Command.h:L64-L71](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Command.h#L64-L71) —— `readEventEnabled()` 等受保护访问器，供子类在 `executeInternal()` 里查询「这轮我为什么被叫醒」。

`transitStatus` 的降级逻辑：

- [src/Command.cc:L50-L58](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Command.cc#L50-L58) —— 只有 `STATUS_REALTIME` 保持不变，其余状态一律降为 `STATUS_INACTIVE`。命令构造时初始状态就是 `STATUS_INACTIVE`（[src/Command.cc:L40-L48](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Command.cc#L40-L48)）。

引擎侧的单条命令调度（u2-l1 讲过循环，这里是循环体）：

- [src/DownloadEngine.cc:L118-L139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L118-L139) —— `executeCommand()`：`statusMatch` 过滤（L125）、`transitStatus` 降级（L130）、`execute()` 返回 true 则 `com.reset()` 销毁（L131-133）、返回 false 则 `clearIOEvents()` 后 `com.release()` 放弃所有权（L134-137）。注意 `release()` 不是泄漏——返回 false 的约定是命令已经 `addCommandSelf()` 把自己交回了队列。
- [src/DownloadEngine.cc:L168-L177](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L168-L177) —— 平时用 `STATUS_ACTIVE` 过滤 `commands_`（L175），每秒全量刷新用 `STATUS_ALL`（L172）；`routineCommands_` 每轮都是 `STATUS_ALL`（L177）——这就是周期命令被降级后仍能持续运行的原因。

Command 族的另一分支——周期命令：

- [src/TimeBasedCommand.h:L45-L95](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TimeBasedCommand.h#L45-L95) —— `TimeBasedCommand` 不走 `executeInternal()` 那套模板，而是 `preProcess() / process() / postProcess()` 三段式；`exit_` 标志（L53-60 的注释）说明「任务完成想自我删除就 `enableExit()`」；`routineCommand_` 决定重新入队到哪条队列。
- [src/TimeBasedCommand.cc:L55-L79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TimeBasedCommand.cc#L55-L79) —— `execute()`：每次都跑 `preProcess`，只有距上次超过 `interval_` 才跑 `process()`（L61-67），最后把自己重新加回 `routineCommands_` 或 `commands_` 并返回 false（L72-78）。

#### 4.1.4 代码实践（不改代码，用现成日志观察状态机）

aria2 在 `AbstractCommand::execute()` 开头本来就有一条 debug 日志，打印四个事件标志（4.2.3 会看到），我们可以直接用它观察「置位 → 消费 → 清除」的节奏。

1. **实践目标**：不用改任何代码，从日志中看到同一 cuid 的事件标志如何变化，验证「事件只置位、执行后清除」。
2. **操作步骤**：
   ```bash
   ./src/aria2c -d /tmp/a2obs -o test.html --log-level=debug -l /tmp/a2obs/log.txt \
       https://example.com/
   grep -n "socket: read:" /tmp/a2obs/log.txt | head -40
   ```
3. **需要观察的现象**：同一个 `CUID#n` 的行里，`read:0` 与 `read:1` 交替出现；`read:1` 的下一行通常是该命令处理数据的日志（如 HTTP 响应解析、写盘），再往后同一 cuid 又回到 `read:0`。
4. **预期结果**：`read:1` 只出现在「socket 真有数据到来、EventPoll 回调置位之后的那一轮执行」；每轮执行完毕 `clearIOEvents()` 复位，因此不会连续出现大段 `read:1`（除非数据持续到达）。具体交替次数取决于网络与文件大小，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `executeCommand()` 里 `com->execute()` 返回 false 时必须调用 `com.release()` 而不是 `delete com`？

**答案**：返回 false 的约定是：命令在 `execute()` 内部已经通过 `addCommandSelf()`（即 `e_->addCommand(std::unique_ptr<Command>(this))`）把自己重新放回了队列，队列里的 `unique_ptr` 是新的所有者。此处若再 `delete`（或让局部 `unique_ptr` 析构）会造成双重释放；`release()` 只是放弃旧所有权。

**练习 2**：`CreateRequestCommand` 构造函数里调用了 `setStatus(Command::STATUS_ONESHOT_REALTIME)`，这个状态会让它什么时候被执行？执行一次后变成什么状态？

**答案**：`STATUS_ONESHOT_REALTIME` 数值为 4，大于 `STATUS_ACTIVE`(2)，所以哪怕引擎走的是平时轮（ACTIVE 过滤），`statusMatch` 也成立，它下一轮立即执行。执行前 `transitStatus()` 把非 REALTIME 状态统一降为 `STATUS_INACTIVE`，因此它若想再次被尽快执行必须重新自我激活（见 [src/CreateRequestCommand.cc:L55-L66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CreateRequestCommand.cc#L55-L66)）。

**练习 3**：`TimeBasedCommand` 的命令被 `transitStatus()` 降级成 INACTIVE 之后，为什么还能每轮都被执行？

**答案**：它们多数以 `routineCommand=true` 构造，被重新加回 `routineCommands_` 队列；而 `DownloadEngine::run()` 对 `routineCommands_` 每轮都用 `STATUS_ALL` 过滤执行（[src/DownloadEngine.cc:L177](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L177)），`STATUS_ALL`(0) 与任何状态比较都成立，不受降级影响。

### 4.2 AbstractCommand 状态机：execute() 模板方法骨架

#### 4.2.1 概念说明

`AbstractCommand` 是 HTTP/FTP/SFTP 及其代理变体等**所有流式下载命令**的公共基类。它解决的问题是：这些命令无论处在哪个协议阶段，都有一套完全相同的「家务事」——

- 检查任务是否已结束、自己租借的 segment 是否被别人回收；
- 决定「这一轮我有没有事可做」（`shouldProcess()`）；
- 没有 I/O 事件时做超时与错误判定；
- 统一处理三类异常出口；
- 管理对 socket 的读/写关注注册（与 EventPoll 的对接）；
- 维护 RequestGroup 的连接/命令计数。

协议差异被压缩到一个纯虚函数 `executeInternal()` 里，由子类实现——这是典型的**模板方法模式**：`execute()` 是固定骨架，`executeInternal()` 是变化点。

#### 4.2.2 核心流程

`AbstractCommand::execute()` 的五道关卡（伪代码）：

```
execute():
  关卡0  若 RequestGroup 已完成/已请求停止      → return true（自灭）
         若 Request 被要求移除                  → prepareForRetry(0)
  关卡1  从 SegmentMan 取回属于自己的 segment：
         - 若曾被分配现在没了（被回收）          → prepareForRetry(0)
         - 每 10 秒探测一次更快的镜像源          → useFasterRequest(换源重建) 
  关卡2  shouldProcess()？本轮有活干吗：
         读关注的 socket 有读事件/缓冲有数据？写关注可写？DNS 有结果？
         或根本没注册任何关注（noCheck()）？
     ├─ 有活 → 补充 segment（没有可用 segment 则 prepareForRetry）
     │         → return executeInternal()      ← 协议逻辑在这里
     └─ 没活 → 若 errorEvent                   → 抛 DlRetryEx（网络错误）
               若 距上次活动 ≥ timeout          → 标坏 IP，抛 TIME_OUT
               否则 addCommandSelf(); return false（回去睡觉）
  异常出口（包住以上全部）:
    DlAbortEx              → onAbort() + tryReserved() → return true
    DlRetryEx              → tryCount+1；超 max-tries 则同 abort；
                             否则 prepareForRetry(0)（新建 CreateRequestCommand）
    DownloadFailureException → 整组 setHaltRequested(true) → return true
```

`prepareForRetry(wait)` 是重试回路的关键：取消自己的 segment → 把 Request 还回池子 → 新建一个 `CreateRequestCommand` 加入引擎 → 自己返回 true 销毁。注意新旧命令**共用同一个 cuid**，所以日志里同一条流水线的编号不变。

#### 4.2.3 源码精读

构造与析构——资源与计数的对称管理：

- [src/AbstractCommand.cc:L77-L111](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L77-L111) —— 构造函数：若传入的 socket 已打开则立刻注册读关注（L100-102）；随后 `increaseStreamConnection / increaseStreamCommand / increaseNumCommand` 三连计数（L103-107）——`RequestGroup` 靠这些计数判断「本任务是否还有活着的命令」。
- [src/AbstractCommand.cc:L113-L125](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L113-L125) —— 析构函数对称地解除读/写关注、递减计数。**这就是「返回 true 即销毁」安全的原因**：所有清理都挂在析构上，命令随时可以「死」。

`shouldProcess()`——判断本轮有没有活干：

- [src/AbstractCommand.cc:L141-L177](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L141-L177) —— 依次检查：读关注 socket 的 `readEventEnabled()` 或接收缓冲非空（L143-155）、写关注可写（L157-159）、异步 DNS 有进展（L161-169）、都没注册则无条件执行（L170-174），最后兜底 `noCheck()`（L176）——子类可覆盖它强制执行（见 4.3 的 `DownloadCommand`）。

`execute()` 主体：

- [src/AbstractCommand.cc:L179-L196](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L179-L196) —— 开头那条 debug 日志（L181-184，4.1.4 实践用的就是它）；关卡 0：任务已完成/已请求停止直接 `return true`（L186-188），Request 被要求移除则 `prepareForRetry(0)`（L190-196）。
- [src/AbstractCommand.cc:L200-L249](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L200-L249) —— 关卡 1：`getInFlightSegment` 刷新自己租借的 segment；被回收则 `prepareForRetry(0)`（L204-219）；每 10 秒（`serverStatTimer_` 判定，L236）借 `findFasterRequest` 找更快的镜像源，找到就 `useFasterRequest` 换源重建连接命令（L223-248，实现见 [L127-L139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L127-L139)，会 `setNoWait(true)` 让引擎不睡）。
- [src/AbstractCommand.cc:L251-L304](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L251-L304) —— 关卡 2：`shouldProcess()` 成立则先补 segment——单文件下载用 `while (segments_.size() < maxSegments)` 逐个 `sm->getSegment()`（L263-271），拿不到时若所有 segment 都被「忽略」则 `setRefreshInterval(0)` 后自灭（L272-285），否则 `prepareForRetry(1)` 睡一秒再试（L287）；多文件下载走批量分支（L290-301）；最后 `return executeInternal()`（L303）——协议逻辑的入口。
- [src/AbstractCommand.cc:L306-L344](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L306-L344) —— 没活干的三种去向：`errorEventEnabled()` 说明内核报告了 socket 错误，给 ServerStat 记 error 后抛 `DlRetryEx`（L306-314）；超过 `timeout_` 秒无进展，把当前 IP 标记为坏地址、必要时清缓存，抛 `TIME_OUT` 异常（L316-341）；都不是则 `addCommandSelf()` 重新排队并 `return false`（L343-344）——回去睡觉等事件。

三个异常出口：

- [src/AbstractCommand.cc:L346-L363](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L346-L363) —— `DlAbortEx`（不可恢复）：记录 URIResult 与 lastErrorCode，`onAbort()` 清理（移除 URI、取消 segment，见 [L472-L529](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L472-L529)，其中还包括「服务器不支持断点续传就从头重下」的兜底逻辑），再 `tryReserved()` 尝试用下一个 URI 重建命令（[L415-L436](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L415-L436)），最后 `return true` 自灭。
- [src/AbstractCommand.cc:L364-L397](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L364-L397) —— `DlRetryEx`（可重试）：`addTryCount / resetUri`（L369-371）；若达到 `--max-tries` 上限则走与 abort 相同的收尾（L373-387）；HTTP 503 会给 Request 设置 `wakeTime` 冷却（L389-394）；否则 `prepareForRetry(0)` 立即重试（L396）。
- [src/AbstractCommand.cc:L398-L412](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L398-L412) —— `DownloadFailureException`（致命）：`setHaltRequested(true)` 让**整个 RequestGroup** 停机，`setRefreshInterval(0)` 让引擎立刻醒来善后。

`prepareForRetry` 与读/写关注注册：

- [src/AbstractCommand.cc:L438-L470](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L438-L470) —— 取消 segment（L440-442）、`poolRequest` 把 Request 还回池子（L450）、新建 `CreateRequestCommand`（L458-459）；`wait == 0` 时 `setNoWait(true)` 让引擎零延迟进入下一轮（L460-462），`wait != 0` 时把新命令置为 `STATUS_INACTIVE` 睡到下一次全量刷新（L463-467，注释说明不直接用 wait 值计时）；末尾 `return true`——自己销毁，接力棒交给新命令。
- [src/AbstractCommand.cc:L542-L562](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L542-L562) —— `setReadCheckSocket()`：向引擎注册「我想在这个 socket 可读时被叫醒」，内部调 `e_->addSocketForReadCheck(socket, this)`；换目标 socket 时先注销旧的再注册新的。写关注版本 `setWriteCheckSocket` 在 [L585-L605](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L585-L605)。**这就是 socket 完成回调与命令重排的对接点**：EventPoll 只认「socket + Command」，事件到达置标志，命令重排后下一轮读到标志再干活。
- [src/AbstractCommand.cc:L930-L944](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L930-L944) —— `checkSocketRecvBuffer()`：若接收缓冲还有未消费数据，把自己升级为 `STATUS_ONESHOT_REALTIME` 并 `setNoWait(true)`——让引擎「不等待事件、立刻下一轮」继续消费缓冲，这是数据泵高吞吐的秘诀之一；`addCommandSelf()` 则是「把自己重新排队」的标准动作。

#### 4.2.4 代码实践（触发超时与重试出口）

1. **实践目标**：亲手触发 `DlRetryEx` 超时出口与 `max-tries` 终止出口，在日志里看到 `prepareForRetry` 产生的 `CreateRequestCommand` 回路。
2. **操作步骤**：
   ```bash
   # 强制 1 秒超时 + 最多尝试 2 次，目标是一个不可达/极慢的地址
   ./src/aria2c -d /tmp/a2obs --timeout=1 --max-tries=2 --retry-wait=1 \
       --log-level=info -l /tmp/a2obs/timeout-log.txt \
       "http://10.255.255.1:81/bigfile" ; echo "exit=$?"
   grep -nE "Timeout|Restarting download|Maximum number of retries|No URI available|CUID#" /tmp/a2obs/timeout-log.txt | head -30
   ```
3. **需要观察的现象**：同一 CUID 下出现「超时 → Restarting download（MSG_RESTARTING_DOWNLOAD）」两次；第二次之后出现达到最大尝试次数的日志（MSG_MAX_TRY）与下载中止（MSG_DOWNLOAD_ABORTED）；退出码非 0。
4. **预期结果**：每次 `Timeout.` 对应 [L316-L341](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L316-L341) 抛出的 `TIME_OUT`；每次 `Restarting download` 对应 `DlRetryEx` 出口；第二次达到 `--max-tries=2` 走 [L373-L387](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L373-L387) 的 abort 收尾。具体日志措辞与行数**待本地验证**（不同网络环境下可能先触发连接失败而非超时，两者最终都汇入 `DlRetryEx` 出口，观察结论不变）。

#### 4.2.5 小练习与答案

**练习 1**：`prepareForRetry(0)` 和 `prepareForRetry(1)` 的实际区别是什么？参数 1 会让命令睡 1 秒吗？

**答案**：不会精确睡 1 秒。`wait == 0` 时调用 `e_->setNoWait(true)`，引擎本轮结束后不进入事件等待、立刻执行下一轮，新命令近似「立即」重试；`wait != 0` 时把新建的 `CreateRequestCommand` 置为 `STATUS_INACTIVE`，它要等到下一次（最多约 1 秒后的）`STATUS_ALL` 全量刷新才被执行。真正的等待由引擎的刷新节奏实现，而不是命令自己计时（源码注释明确说明了这一点）。

**练习 2**：一个命令在 `executeInternal()` 里抛出 `DlRetryEx` 之后，它的 segment 和 Request 分别去哪了？

**答案**：异常被 `execute()` 的 catch 块接住后调用 `prepareForRetry(0)`：`getSegmentMan()->cancelSegment(getCuid())` 把租借的 segment 归还（取消），`fileEntry_->poolRequest(req_)` 把 Request 放回 FileEntry 的请求池供下次取用；然后新建 `CreateRequestCommand` 重新走一遍「取 URI → 建连接」流程。

**练习 3**：为什么 `AbstractCommand` 把 `executeInternal()` 设为纯虚、却把 `noCheck()` 提供为可覆盖的虚函数？

**答案**：`execute()` 骨架对所有流式协议命令通用（关卡 0/1/2、超时、异常出口都一样），唯一不同的协议行为收敛到 `executeInternal()`；而 `noCheck()` 默认 false（有注册关注才执行），个别子类需要「无条件执行」的旁路——例如 `DownloadCommand` 在设置了最低速度限制时要每轮测速（见 4.3），因此覆盖 `noCheck()` 返回 true。

### 4.3 DownloadCommand 数据泵：executeInternal 的读写循环

#### 4.3.1 概念说明

`DownloadCommand` 是 `AbstractCommand` 最重要的子类（HTTP 的 `HttpDownloadCommand`、FTP 的 `FtpDownloadCommand` 都只是薄封装）：它在「连接已建立、响应头已处理」之后登场，职责是**把 socket 里的字节搬运到磁盘**，直到当前 segment 完成。它像一个水泵：每轮事件循环「抽一点、写一点」，抽不完就把自己重新排队下一轮继续——所以我们叫它**数据泵**。

#### 4.3.2 核心流程

`DownloadCommand::executeInternal()` 伪代码：

```
executeInternal():
  若 全局/单任务限速已超                       → 关闭读写关注，addCommandSelf，return false（睡）
  注册读关注 socket
  若 接收缓冲为空:
      eof = (recv() == 0 且 socket 无读写需求)   # 读一块进缓冲
  若 未 eof:
      计算 bufSize（不超过 segment 剩余/文件末尾）
      streamFilter_->transform(diskAdaptor, segment, buffer, bufSize)  # 写盘（可叠加 chunked/gzip 过滤链）
      drain(bufSize)；更新 peerStat 与 DownloadContext 计数
  判定 segmentPartComplete:
      segment->complete() 或 写到文件末尾 或 (长度未知且 eof) …
  若 未完成 且 eof                             → 抛 DlRetryEx(EX_GOT_EOF)   # 连接被掐断
  若 完成:
      可选 piece 哈希实时校验（坏 → 清 segment，抛 DlRetryEx 重下这个 piece）
      return prepareForNextSegment()
        ├─ 任务整体完成 → poolRequest + 推整文件校验 entry + setNoWait → return true
        ├─ 同一连接可续下一段（index+1 的 segment 干净）→ addCommandSelf，return false
        └─ 否则 → prepareForRetry(0) 回到 CreateRequestCommand
  否则:
      setWriteCheckSocketIf(...)；checkSocketRecvBuffer()；addCommandSelf；return false
```

三个出口全部继承/复用 4.2 的骨架：完成（true）、异常（catch 块）、自循环（false）。

#### 4.3.3 源码精读

构造与析构——测速与过滤器：

- [src/DownloadCommand.cc:L76-L106](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L76-L106) —— 构造：若开了实时 chunk 校验则创建 `MessageDigest`（L88-94）；初始化 `PeerStat` 测速对象并注册到 SegmentMan（L97-99）；创建 `SinkStreamFilter` 作为写盘过滤链的末端（L101-104）；末尾 `checkSocketRecvBuffer()`（L105）——若缓冲已有数据立刻把自己升级为一次性实时命令。
- [src/DownloadCommand.cc:L108-L112](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L108-L112) —— 析构：停止测速、向 SegmentMan 汇报「我见过的最快 peer」。

限速让路与读取：

- [src/DownloadCommand.cc:L133-L148](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L133-L148) —— 开头的限速检查：全局或单任务速度超限时**主动关闭读写关注**并 `addCommandSelf` 返回 false 睡觉（L135-143），把带宽让给别人（限速机制在 u3-l5 详解）；随后注册读关注、取 `getSegments().front()` 作为当前工作段。

读取与 EOF 判定（含一个经典的 pipelining 陷阱）：

- [src/DownloadCommand.cc:L150-L162](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L150-L162) —— 只有**接收缓冲为空**时才 `recv()`；`eof` 的判定是「读到 0 字节且 socket 既无待读也无待写」。L151-159 的长注释解释了为什么缓冲非空时绝不能读 socket：HTTP pipelining 下缓冲里可能还躺着第二个响应，此时贸然读会拿到 EOF 而把第二个响应晾在缓冲里。

写盘与计数：

- [src/DownloadCommand.cc:L163-L198](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L163-L198) —— `streamFilter_->transform(...)` 把接收缓冲写入 `diskAdaptor`（经由 segment 定位；过滤链上还可挂 chunked 解码、gzip 解压，见 u4-l6），`bufSize` 被限制在 segment 剩余量与文件末尾之内（L166-182）；随后 `drain(bufSize)` 消费缓冲、`peerStat_->updateDownload` 与 `updateDownload` 累计速度与进度（L196-198）。

segment 完成判定与三条岔路：

- [src/DownloadCommand.cc:L200-L235](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L200-L235) —— 综合判定 `segmentPartComplete`（segment 写满 / 到达文件末尾 / 长度未知但收到 EOF / 过滤链 finished 等，L200-231）；**未完成却 EOF** 说明连接中途被掐断，抛 `DL_RETRY_EX(EX_GOT_EOF)`（L233-235）——交给 4.2 的 `DlRetryEx` 出口重试。
- [src/DownloadCommand.cc:L237-L287](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L237-L287) —— 完成分支：若启用实时 piece 哈希则校验（`validatePieceHash` 在 [L383-L401](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L383-L401)：哈希不符就 `segment->clear` + `cancelSegment` + 抛 `DlRetryEx` 重下该 piece），通过则 `completeSegment`（L274，会先 flush 写盘缓存）；末尾 `checkLowestDownloadSpeed()` 测最低速，然后 `return prepareForNextSegment()`（L284-286）。
- [src/DownloadCommand.cc:L317-L381](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L317-L381) —— `prepareForNextSegment()` 三岔路：任务整体完成→`poolRequest`、可选推入整文件校验 entry、`setNoWait + setRefreshInterval(0)` 让引擎立刻收尾（L319-343）；单 segment 且下一格（index+1）干净可用→`addCommandSelf` 返回 false，**同一条连接继续下一段**（L358-375）；否则 `prepareForRetry(0)` 回到 `CreateRequestCommand`（L365-370、L378）。
- [src/DownloadCommand.cc:L288-L294](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L288-L294) —— 未完成分支：按需注册写关注（`shouldEnableWriteCheck` 看 TLS 等场景是否要写，[L297-L300](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L297-L300)）、`checkSocketRecvBuffer()`、`addCommandSelf()`、`return false`——**数据泵的自循环**。
- [src/DownloadCommand.cc:L422-L424](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L422-L424) —— 覆盖 `noCheck()`：设置了最低速度限制时返回 true，让骨架无条件进入 `executeInternal()` 以便测速（呼应 4.2.3 的 `shouldProcess` 兜底分支）。

#### 4.3.4 代码实践（源码阅读型：追踪一次多段下载）

1. **实践目标**：从日志观察「同一条连接连续下载多个 segment」，验证 `prepareForNextSegment` 的「继续下一段」分支真实存在。
2. **操作步骤**：
   ```bash
   # 找一个几十 MB 的 HTTP 直链文件；切成 4 段、每段最小 1M
   ./src/aria2c -d /tmp/a2obs -s4 -k1M -x1 --log-level=info -l /tmp/a2obs/seg-log.txt \
       "https://example.com/bigfile.bin"
   grep -nE "Segment.*download completed|CUID#|Download complete" /tmp/a2obs/seg-log.txt | head -50
   ```
   同时对照源码 [src/DownloadCommand.cc:L346-L375](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L346-L375) 阅读 `getSegmentWithIndex(getCuid(), index+1)` 这条路径。
3. **需要观察的现象**：日志中「segment 下载完成」（MSG_SEGMENT_DOWNLOAD_COMPLETED）按 index 递增出现；若连接被复用，这些行共享同一 CUID。
4. **预期结果**：同一 CUID 连续完成多个 index 相邻的 segment，对应「下一格干净→addCommandSelf 继续」分支；当下一格已被其他连接占用（`getWrittenLength() > 0`）时会走 `prepareForRetry(0)`，日志出现重新取 URI/建连接的行。分段的切分与分配策略本身在 u3-l4 详讲；具体 index 序列**待本地验证**（取决于服务器是否支持 Range 与并发连接行为）。

#### 4.3.5 小练习与答案

**练习 1**：数据泵明明可以把 socket 读到 EOF 一次搬完，为什么每轮只搬一块就 `addCommandSelf` 返回 false？

**答案**：aria2 是单线程 Reactor 模型，一条连接长时间独占引擎会让其他任务（其他下载、RPC、DHT）饿死。数据泵每轮只消费当前缓冲里的一段数据就把控制权交回事件循环，引擎借此在多条连接之间公平轮转；同时「返回 false + 重新排队」也让限速（超速时关闭读写关注睡觉）成为可能。

**练习 2**：`prepareForNextSegment()` 里为什么「下一格 segment 的 `getWrittenLength() > 0` 时必须 `prepareForRetry(0)` 关闭连接」，而不是把数据写进去？

**答案**：源码注释（L365-369）指出：若下一格已有别人写入的数据，本连接的响应体从 segment 头开始，直接写会在 `writtenLength` 处覆盖/错位，损坏文件。所以宁可关连接重走一次请求流程，让 SegmentMan 重新分配干净的段。

**练习 3**：实时 piece 哈希校验失败后，aria2 会放弃整个文件吗？

**答案**：不会。`validatePieceHash`（L383-401）只清空当前 segment（`segment->clear`）、取消租借（`cancelSegment`）并抛 `DlRetryEx`（"Invalid checksum index=..."），由 4.2 的重试出口重建请求，**只重下这个 piece**；只有 `--max-tries` 耗尽才会升级为 abort。

### 4.4 接力全景：INITIATE → REQUEST → RESPONSE → DOWNLOAD

#### 4.4.1 概念说明

前三个模块分别看了基类、骨架和数据泵，现在把一次 HTTP 下载的**完整命令接力**串起来。关键认知：四个阶段不是四个状态，而是**四个（组）不同的 Command 类**，前一个命令在自己 `execute()` 的末尾（或异常出口）创建后一个命令并把自己销毁，socket、Request、segment 等资源通过构造参数传递。同一个 cuid 贯穿整条流水线。

#### 4.4.2 核心流程

```
CreateRequestCommand            「取号机」：从 URI 池取出一个 Request
   │ InitiateConnectionCommandFactory（CreateRequestCommand.cc L121-126）
   ▼
HttpInitiateConnectionCommand   「发令员」：查连接池、发起非阻塞 connect（DNS 在其基类中解析）
   ├─ 池中有可用连接 ──────────────→ 直接创建 HttpRequestCommand（复用连接）
   └─ 无 → ConnectCommand 等待连接建立（含 TLS 握手，u2-l4/u4-l1 详述）
   ▼
HttpRequestCommand              「写信人」：生成并发送 HTTP 请求（含 Range/鉴权头）
   │ 发送缓冲清空 → 创建 HttpResponseCommand，return true（HttpRequestCommand.cc L200-205）
   ▼
HttpResponseCommand             「拆信人」：解析响应头、校验总长度、处理重定向/错误码
   │ 创建 HttpDownloadCommand（HttpResponseCommand.cc L317-323, L536-542）
   ▼
HttpDownloadCommand(≈DownloadCommand)  「水泵」：每轮搬一块数据写盘（4.3）
   ├─ segment 完成、同连接续段 → addCommandSelf 自循环
   ├─ segment 完成、需换段/换源 → prepareForRetry(0)
   │        └──────→ 新的 CreateRequestCommand（回到顶部，cuid 不变）
   ├─ 超时/EOF 异常 → DlRetryEx 出口 → prepareForRetry(0)（同上）
   └─ DNS 失败等 → DlAbortEx 出口 → tryReserved() 换下一个 URI 重建命令
```

#### 4.4.3 源码精读

- [src/CreateRequestCommand.cc:L68-L127](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CreateRequestCommand.cc#L68-L127) —— 取号机：`getFileEntry()->getRequest(...)` 按 URISelector 选出 Request（L82-93）；无 URI 可用抛 `DlAbortEx`（L94-109）；Request 还在冷却（wakeTime 未到）就放回池子、自己重睡（L110-119）；否则 `InitiateConnectionCommandFactory::createInitiateConnectionCommand` 创建第一棒（L121-126）。它在构造时就 `setStatus(STATUS_ONESHOT_REALTIME)`（[L55-L66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CreateRequestCommand.cc#L55-L66)），保证下一轮立即执行——重试回路不迟到。
- [src/HttpInitiateConnectionCommand.cc:L71-L148](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpInitiateConnectionCommand.cc#L71-L148) —— 发令员的两条路：连接池（keep-alive 复用）有货 → 直接造 `HttpRequestCommand` 并把池中 socket 传给它（L136-146）；没货 → `establishConnection` 发起非阻塞连接，造 `ConnectCommand` 盯着连接完成（L122-135）。代理场景（L76-116）多一层 `HttpProxyRequestConnectChain`/`HttpRequestConnectChain` 的控制链，细节留到 u4-l4。
- [src/HttpRequestCommand.cc:L123-L212](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequestCommand.cc#L123-L212) —— 写信人：发送缓冲空则构造并发送请求（HTTPS 先做 `tlsConnect`，L127-135；分段请求带 Range 的 endOffset，L174-195）；发送未完成就注册读写关注 + `addCommandSelf` 等下一轮（L206-211）；发送完毕 → `addCommand(make_unique<HttpResponseCommand>(...))` 并 `return true` 完成交棒（L200-205）。
- [src/HttpResponseCommand.cc:L304-L326](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L304-L326) —— 拆信人末段：`validateTotalLength` 校验长度（L305），总长未知先 truncate 再装过滤链（L309-319），最后 `createHttpDownloadCommand` 造出数据泵并交棒（L317-323）；工厂函数在 [src/HttpResponseCommand.cc:L536-L542](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L536-L542)。重定向、304、错误码等分支会改为创建别的命令或抛异常（u4-l1 逐条展开）。
- 三类出口速查表（承接 4.2/4.3）：

| 出口 | 触发条件 | 当前命令的下场 | 接班人 |
| --- | --- | --- | --- |
| 正常完成 | 阶段职责完成 / 任务结束 | `return true` 销毁 | 下一阶段命令（或无） |
| 可重试 | `DlRetryEx`：超时、EOF、piece 校验失败、连接失败… | 销毁 | 新 `CreateRequestCommand`（cuid 不变，wait=0 立即 / wait≠0 睡到下轮刷新） |
| 终止 | `DlAbortEx`：DNS 失败、无 URI、达到 max-tries；`DownloadFailureException`：致命 | 销毁 | `tryReserved()` 换 URI 的新命令，或整组 `setHaltRequested` 停机 |

#### 4.4.4 代码实践（本讲主实践：给命令流水线拍 X 光片）

这是本讲指定的动手任务：在 `AbstractCommand::execute()` 里加一行日志，统计四类命令各出现多少次。

1. **实践目标**：用日志证实「四段接力 + 数据泵自循环」的模型——前三棒各执行一次，`HttpDownloadCommand` 执行 N 次。
2. **操作步骤**：
   1. 由于 `status_` 是 `Command` 的私有成员，先在 `src/Command.h` 的 public 区临时加一个访问器（**示例代码，非项目原有代码**，验证完记得撤销）：
      ```cpp
      STATUS currentStatus() const { return status_; }  // 示例代码
      ```
   2. 在 `src/AbstractCommand.cc` 的 `execute()` 开头（原有 `A2_LOG_DEBUG("...socket: read:...")` 那行之后）插入（**示例代码**）：
      ```cpp
      #include <typeinfo>  // 放到文件头部 include 区
      // —— 以下放在 execute() 开头 ——
      std::string seginfo = "-";
      if (!segments_.empty()) {
        seginfo = fmt("index=%lu written=%" PRId64 "/%" PRId64,
                      static_cast<unsigned long>(segments_.front()->getIndex()),
                      segments_.front()->getWrittenLength(),
                      segments_.front()->getLength());
      }
      A2_LOG_INFO(fmt("PIPE CUID#%" PRId64 " cmd=%s status=%d segs=%zu %s",
                      getCuid(), typeid(*this).name(),
                      static_cast<int>(currentStatus()), segments_.size(),
                      seginfo.c_str()));
      ```
   3. 重新编译（构建方法见 u1-l2，通常是 `make -j$(nproc)`）。
   4. 跑一次小文件 HTTP 下载并统计：
      ```bash
      ./src/aria2c -d /tmp/a2obs -o pipe.txt --log-level=info -l /tmp/a2obs/pipe-log.txt \
          https://example.com/
      for t in CreateRequestCommand InitiateConnectionCommand ConnectCommand \
               HttpRequestCommand HttpResponseCommand HttpDownloadCommand; do
          printf "%-28s %s\n" "$t" "$(grep -c "$t" /tmp/a2obs/pipe-log.txt)"
      done
      grep "PIPE" /tmp/a2obs/pipe-log.txt | head -40
      ```
3. **需要观察的现象**：
   - `CreateRequestCommand / HttpInitiateConnectionCommand / HttpRequestCommand / HttpResponseCommand` 通常各 1 次（若走非阻塞连接，`ConnectCommand` 也会出现若干次，因为连接建立期间它反复自我重排）；
   - `HttpDownloadCommand`（在日志里显示为 `HttpDownloadCommand`，`typeid` 名称可能是带命名空间的修饰形式，如 `N5aria219HttpDownloadCommandE`，grep 时用子串即可）出现次数明显最多，且这些行的 `index` 相同、`written` 单调增长——正是数据泵自循环；
   - 各命令 `status` 字段多数为 2（ACTIVE）或 4（ONESHOT_REALTIME，缓冲有数据时）。
4. **预期结果**：计数大致为 `HttpDownloadCommand` 的次数 ≈ 文件字节量 / 每轮搬运量，远大于其他命令的 1~数次；若服务器发送重定向或连接被复用，前几棒的计数会相应增加。具体数值依赖网络与文件大小，**待本地验证**。验证完成后请撤销两处示例代码改动，保持源码干净。

#### 4.4.5 小练习与答案

**练习 1**：日志里同一 CUID 从 `CreateRequestCommand` 一直用到 `HttpDownloadCommand`，这个编号在哪里分配、为什么能跨命令传递？

**答案**：cuid 由 `RequestGroup` 在创建命令时分配并作为构造参数传入 `Command(cuid)`；所有接力命令（包括 `prepareForRetry` 新建的 `CreateRequestCommand`，见 [src/AbstractCommand.cc:L458-L459](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L458-L459) 用的是 `getCuid()`）都透传同一个值。它充当「逻辑连接/流水线」的标识，方便在日志里追踪一条流水线的完整生命周期。

**练习 2**：如果服务器不支持 keep-alive（每个响应都 `Connection: close`），四段接力的形态会发生什么变化？

**答案**：连接池（`popPooledSocket`）永远取不到可用连接，每个 segment 都要完整走一遍「Initiate → Connect → Request → Response → Download」，`CreateRequestCommand` 的执行次数会从 1 次变成接近 segment 数；这正是 `prepareForRetry` 里 `req_->supportsPersistentConnection(true)` 重置（[src/AbstractCommand.cc:L443-L448](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L443-L448) 的注释）要应对的情形。

**练习 3**：`HttpDownloadCommand` 完成一个 segment 后，哪些情况下会**不新建任何命令**而继续下载？

**答案**：当 `getSegments().size() == 1`、当前 segment 已完成、请求终点未到、且下一格 `getSegmentWithIndex(cuid, index+1)`（或 `getCleanSegmentIfOwnerIsIdle`）拿到的 segment `writtenLength == 0` 时，直接 `addCommandSelf()` 返回 false，用同一条连接继续请求并下载下一段（[src/DownloadCommand.cc:L345-L375](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L345-L375)）。

## 5. 综合实践

**任务：画出一次双连接分段下载的完整命令时序图。**

1. 准备一个支持 Range 的 HTTP 直链大文件（几十 MB），执行：
   ```bash
   ./src/aria2c -d /tmp/a2obs -o dual.bin -x2 -s2 -k1M \
       --log-level=debug -l /tmp/a2obs/dual-log.txt "https://example.com/bigfile.bin"
   ```
2. 用 `grep "CUID#" /tmp/a2obs/dual-log.txt | less` 通读日志，按时间顺序挑出**两条 cuid 流水线**，对每一条标注：
   - 每一棒命令的类名（Initiate/Connect/Request/Response/Download）与出现顺序；
   - 每一棒的退出方式：正常完成（return true）、自循环（addCommandSelf 后再出现）、还是异常出口（超时/EOF/重试日志行）；
   - 数据泵阶段 `index` 与 `written` 的推进（可结合 4.4.4 的插桩日志，二者选一即可）。
3. 画成时序图（手绘或 mermaid 均可），并用三种颜色/标记区分三类出口。
4. 回答两个问题：
   - 两条流水线各自的 segment `index` 是否重叠？为什么？（提示：SegmentMan 的分配互斥，u3-l4 展开）
   - 日志里出现了几次 `CreateRequestCommand`？结合本文解释次数 ≥ 2 且可能更多的原因（初始两条 + 换段失败重试 + prepareForRetry 回路）。

预期产物：一张时序图 + 两段文字解释。结果与网络环境相关，**待本地验证**；若无法获得支持 Range 的目标，可改用 `-s1` 单连接版本，重点观察一条流水线的接力与自循环。

## 6. 本讲小结

- `Command` 基类用「STATUS 状态位 + 四个 I/O 事件标志」把调度频率与唤醒原因数据化；`statusMatch` 按枚举数值过滤，`transitStatus` 让命令执行一次即降级为 INACTIVE，想保持活跃必须每轮重新激活。
- `execute()` 返回 true 即销毁（资源清理都在析构里）、返回 false 必须已 `addCommandSelf` 重排——这是贯穿全引擎的所有权约定。
- `AbstractCommand::execute()` 是模板方法骨架：终局检查 → segment 刷新/换源 → `shouldProcess` 分流 → 超时/错误判定 → `executeInternal()` 协议钩子；三类异常出口（Retry/Abort/Fatal）决定命令的最终去向。
- `prepareForRetry` 是重试回路的心脏：归还 segment、把 Request 放回池子、新建同 cuid 的 `CreateRequestCommand`；`tryReserved` 则在 abort 时尝试下一个 URI。
- 一次 HTTP 下载是四组命令的接力：CreateRequest → Initiate(→Connect) → Request → Response → Download，socket/Request/segment 经构造参数传递，cuid 贯穿始终。
- `DownloadCommand` 数据泵每轮「读一块、写一块、重新排队」，靠 `checkSocketRecvBuffer` 的 ONESHOT_REALTIME 升级实现零等待连续搬运；限速超限时主动关闭读写关注睡觉。

## 7. 下一步学习建议

- **u2-l4（SocketCore 与异步 DNS）**：本讲的 `establishConnection`、`tlsConnect`、`recv`、读写关注注册最终都落到 SocketCore 与异步解析器，下一讲深入网络底座。
- **u3-l4（SegmentMan 与分段调度）**：本讲反复出现 `getSegment / cancelSegment / getSegmentWithIndex`，分段如何切分、互斥与回收是下一单元的重点。
- **u4-l1（HTTP 命令链）**：本讲只看了四段接力的「交接点」，每一段内部的协议细节（重定向、Range 计算、认证头、连接池）在 HTTP 单元逐棒精读。
- 建议先完成第 5 节综合实践再往下走：亲手从日志里辨认出一条流水线后，后续所有协议命令链（FTP、BT 的 Peer 命令）都会显得似曾相识——它们全部复用本讲的骨架。
