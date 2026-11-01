# DownloadEngine：事件驱动主循环

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐行解释 `DownloadEngine::run()` 主循环的每一步动作，说清 `waitData()` 与 `afterEachIteration()` 两个阶段的分工。
2. 掌握 `commands_` 与 `routineCommands_` 两条命令队列的调度规则：命令如何被弹出、按状态过滤、执行、销毁或自我重新排队。
3. 完整跟踪一次「超时命令」的 requeue 过程：从 `AbstractCommand::execute` 检测超时，到抛异常重试，再到新命令入队。
4. 理解 `DownloadEngineFactory::newDownloadEngine()` 如何根据选项装配引擎：事件轮询后端的选择、各周期命令（AutoSave、SeedLimit 相关的 halt 等）如何随选项注册。

## 2. 前置知识

本讲建立在 u1-l3（进程入口）之上，先回顾几个已建立的认知，再补充两个新概念。

- **启动链路回顾**：`main.cc` → `Platform` → `Context`（把 argv 装配成 `RequestGroup` 列表）→ `MultiUrlRequestInfo::execute()`。`execute()` 固定三段：`prepare()`（创建引擎）→ `run()`（主循环）→ `getResult()`（结算退出码）。本讲要钻进的就是中间那段 `run()`。
- **Command（命令）**：aria2 世界里最小的执行单元。它是一个只有 `virtual bool execute() = 0` 的对象，约定返回 `true` 表示「使命完成，可以销毁」，返回 `false` 表示「我已经把自己重新放回队列了，别管我」。第 4.2 节会看到这个约定如何被引擎严格执行。
- **Reactor 模式（反应器模式）**：网络程序的一种经典结构——单个线程 + I/O 多路复用（epoll/select 等），哪个 socket 可读可写就回调哪个处理逻辑。aria2 是这个模式的忠实实践者，只是把「回调函数」具体化成了「Command 对象」：命令把自己挂到某个 socket 的读/写事件上，事件到来时被引擎重新调度执行。
- **`global::wallclock()`**：全局共享的时钟对象。源码注释明确说明它在每轮循环 `executeCommand()` 之前被 reset 一次，全轮命令都读这个缓存时间，以减少 `gettimeofday` 系统调用次数（[src/wallclock.h:L41-L43](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/wallclock.h#L41-L43)）。
- **`global::globalHaltRequested` 状态机**：u1-l3 介绍过的 0~5 六个取值，控制 Ctrl-C 两段式停机。本讲会在 `afterEachIteration()` 里看到引擎如何消费它。

> 术语提示：本讲反复出现「迭代（iteration）」，指主循环 `while` 的一圈；「requeue（重新排队）」指命令执行完把自己（或替身）重新塞回队列继续等待下次调度。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/DownloadEngine.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.h) | 引擎类声明：两条命令队列、socket 池、DNS 缓存等成员与 `run()` 接口 |
| [src/DownloadEngine.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc) | 本讲主角：主循环、命令执行、统计、停机处理 |
| [src/DownloadEngineFactory.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc) | 装配车间：选事件后端、注册 routine 命令 |
| [src/Command.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Command.h) / [src/Command.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Command.cc) | 命令基类：五种 STATUS、`execute()` 接口与状态迁移 |
| [src/FillRequestGroupCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FillRequestGroupCommand.cc) | 最重要的 routine 命令：从等待队列往引擎里补充下载任务 |
| [src/TimeBasedCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TimeBasedCommand.cc) | 周期命令骨架：到点干活、否则把自己重新排队 |
| [src/AbstractCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc) | 所有协议命令的公共基类，本讲只看它的超时与 requeue 路径（细节在 u2-l3） |
| [src/MultiUrlRequestInfo.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc) | 工厂调用点：命令行模式下引擎诞生与 `run()` 启动处 |
| [src/aria2api.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/aria2api.cc) | libaria2 嵌入模式下 `run(oneshot)` 的另一种用法 |

## 4. 核心概念与源码讲解

### 4.1 DownloadEngine 主循环

#### 4.1.1 概念说明

`DownloadEngine` 是 aria2 的心脏。它不做任何具体的下载工作——不解析 HTTP、不读磁盘——它只做一件事：**不知疲倦地转圈**，每圈做三类事：

1. **等数据**（`waitData`）：问一次事件轮询器「现在哪些 socket 可读可写了？」没有事件就最多等 1 秒。
2. **跑命令**：把两条队列里的到期命令依次执行。
3. **收尾检查**（`afterEachIteration`）：处理 Ctrl-C 停机状态机等「一圈才检查一次」的事务。

`waitData` 与 `afterEachIteration` 是一圈的两个「括号」：前者管**输入**（这一圈该处理哪些 I/O），后者管**全局状态**（这一圈之后进程要不要改变运行姿态）。理解这个分工是本讲的第一个目标。

主循环的存活条件是「两条命令队列任一非空」。下载全部结束、所有命令都返回 `true` 自我销毁后，循环自然退出——aria2 没有「退出线程」这种东西，退出也是队列排空的结果。

#### 4.1.2 核心流程

把 [run()](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L158-L185) 翻译成伪代码：

```text
run():
    注册析构哨兵（正常退出时把 globalHaltRequested 置 5）
    while commands_ 非空 或 routineCommands_ 非空:
        # ---- 阶段一：等数据 ----
        if commands_ 非空:
            waitData()                 # eventPoll_->poll(超时=0 或 refreshInterval_)

        # ---- 阶段二：执行命令 ----
        noWait_ = false                # 先假设下一圈需要等待
        wallclock().reset()            # 刷新全局缓存时钟
        calculateStatistics()          # 进度统计与限速（u3-l5 详讲）
        if 距上次全量刷新 >= refreshInterval_ - 10ms:
            executeCommand(commands_, STATUS_ALL)      # 唤醒所有命令（含睡眠者）
        else:
            executeCommand(commands_, STATUS_ACTIVE)   # 只跑有事件就绪的命令
        executeCommand(routineCommands_, STATUS_ALL)   # routine 命令每圈全跑

        # ---- 阶段三：收尾检查 ----
        afterEachIteration()           # Ctrl-C 状态机：1→2 / 3→4
        if noWait_ 且 oneshot 模式:
            return 1                   # libaria2 的单步模式先回家
    onEndOfRun()                       # 移除已停止任务、关闭文件、保存会话
    return 0
```

「全量刷新闸门」的判断条件是一个小算式：记距上次刷新经过的时间为 \( t \)，则闸门开启当且仅当

\[ t + 10\,\text{ms} \;\geq\; \Delta_{\text{refresh}} \]

其中 \( \Delta_{\text{refresh}} \) 默认为 1 秒（`DEFAULT_REFRESH_INTERVAL`），10ms 是 `A2_DELTA_MILLIS` 容差（[src/a2time.h:L72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/a2time.h#L72)）。所以每秒约有那么一圈会以 `STATUS_ALL` 执行命令——这是「睡眠中的命令」唯一的醒来的机会（详见 4.2）。

#### 4.1.3 源码精读

**主循环本体**。[src/DownloadEngine.cc:L158-L185](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L158-L185)：`run` 的循环体按顺序完成「等数据 → 刷新时钟与统计 → 按 1 秒闸门选择全量/活跃过滤执行命令 → 执行 routine 命令 → 收尾检查」。注意 `noWait_` 在每圈开头被重置为 `false`，命令若想让下一圈立即继续（比如读缓冲区里还有数据没消化完），必须在本圈里显式调用 `setNoWait(true)`。

**等待数据**。[src/DownloadEngine.cc:L187-L200](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L187-L200)：`waitData` 把 `noWait_` 翻译成 poll 超时——`noWait_` 为真时超时为 0（立即返回，一轮接一轮连轴转），否则等待至多 `refreshInterval_`（默认 1 秒）。这就是 aria2 空闲时 CPU 占用几乎为零的原因：没有事件就阻塞在 `eventPoll_->poll()` 里睡觉。

```cpp
void DownloadEngine::waitData()
{
  struct timeval tv;
  if (noWait_) {
    tv.tv_sec = tv.tv_usec = 0;      // 有事要干，一秒都不等
  }
  else {
    // ... 把 refreshInterval_ 换算成 tv
  }
  eventPoll_->poll(tv);              // 真正的阻塞点（u2-l2 详讲）
}
```

**收尾检查与两段式停机**。[src/DownloadEngine.cc:L244-L264](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L244-L264)：`afterEachIteration` 检查信号处理器写入的 `globalHaltRequested`。第一次 Ctrl-C（值 1）走优雅停机：`requestHalt()` 让所有任务停机，并把状态推进到 2，同时 `setNoWait(true)` + `setRefreshInterval(0ms)` 让引擎全速排空队列；第二次 Ctrl-C（值 3）走紧急停机 `requestForceHalt()`。这两个开关让「善后阶段」不再浪费任何等待时间。

```cpp
if (global::globalHaltRequested == 1) {
  A2_LOG_NOTICE(_("Shutdown sequence commencing..."
                  " Press Ctrl-C again for emergency shutdown."));
  requestHalt();
  global::globalHaltRequested = 2;
  setNoWait(true);
  setRefreshInterval(std::chrono::milliseconds(0));
  return;
}
```

其中 `requestHalt()` 只做两件事：抬高 `haltRequested_`、让 `RequestGroupMan::halt()` 通知所有任务（[src/DownloadEngine.cc:L266-L270](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L266-L270)）。

**循环退出后的善后**。[src/DownloadEngine.cc:L237-L242](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L237-L242)：`onEndOfRun` 依次移除已停止的任务、关闭所有已打开文件、保存会话状态。此后控制权回到 `MultiUrlRequestInfo::getResult()`（u1-l3 已讲）。

**oneshot 模式**。命令行模式调用 `e_->run()` 一直跑到全部完成（[src/MultiUrlRequestInfo.cc:L348-L366](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L348-L366) 中 `execute` 的 `e_->run()`）；而 libaria2 嵌入模式传 `oneshot=true`，每圈最多等一次事件就返回 1，把控制权还给宿主程序的 GUI 事件循环（[src/aria2api.cc:L154-L157](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/aria2api.cc#L154-L157)）。同一个引擎，两种驱动节奏。

#### 4.1.4 代码实践

**实践目标**：不改一行代码，仅凭日志验证「两段式停机」确实由 `afterEachIteration` 驱动，并观察停机后队列全速排空的现象。

**操作步骤**：

1. 用 u1-l2 的方法构建出 `src/aria2c`。
2. 启动一个足够慢的下载（例如限速 `--max-download-limit=50K` 拖长过程），并把日志写入文件：
   `./src/aria2c --max-download-limit=50K -l=aria2.log --log-level=info <某个较大的URL>`
3. 下载中途按一次 Ctrl-C，观察终端与日志；再启动一次，这次连按两次 Ctrl-C。

**需要观察的现象**：第一次按 Ctrl-C 后，日志里应出现 `Shutdown sequence commencing... Press Ctrl-C again for emergency shutdown.`（NOTICE 级别），且 `.aria2` 控制文件被保存；第二次按会出现 `Emergency shutdown sequence commencing...`。

**预期结果**：两条日志分别对应 `afterEachIteration` 中 `globalHaltRequested == 1` 与 `== 3` 的分支；停机后引擎不再等待（`setRefreshInterval(0)`），几秒内进程退出并打印下载结果摘要。网络与具体下载行为**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `waitData()` 在 `commands_` 为空时会被跳过？跳过后引擎岂不是在空转？

**答案**：`commands_` 为空说明没有任何任务命令需要等 I/O（例如全部下载都在等待队列里、或只剩 routine 命令）；此时 poll 只会白等。循环仍由 `routineCommands_`（如 `FillRequestGroupCommand`）驱动，它们每圈执行、到点把新任务的命令灌进 `commands_`，因此不是空转，而是「无任务时让 routine 命令推动调度」。

**练习 2**：`noWait_` 在哪里被置回 `false`？为什么不能由命令自己置回？

**答案**：在每圈循环开头由 `run()` 统一置 `false`（[src/DownloadEngine.cc:L165](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L165)）。`noWait_` 是全局节流开关：若由某个命令置回，其他命令在本圈的意愿会被覆盖。让命令只表达「我还需要立即再跑一次」，由引擎在下一圈开头统一清零，语义才正确。

**练习 3**：libaria2 的 `run(RUN_ONCE)` 返回 1 和 0 分别意味着什么？

**答案**：看 [src/DownloadEngine.h:L179-L183](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.h#L179-L183) 的注释与 `run` 实现：`oneshot=true` 且本轮 `noWait_` 为假（即这一轮没有紧急工作）时返回 1，表示「还有下载要处理，请宿主稍后再调我」；循环自然结束则执行 `onEndOfRun()` 后返回 0，表示全部完成。

### 4.2 命令队列调度：commands_、routineCommands_ 与超时 requeue

#### 4.2.1 概念说明

引擎手里有两条队列（声明在 [src/DownloadEngine.h:L162-L169](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.h#L162-L169)）：

| 队列 | 存放内容 | 执行节奏 | 典型成员 |
| --- | --- | --- | --- |
| `commands_` | 任务命令：真正干下载活的命令 | 每圈按状态过滤执行 | HTTP/FTP 命令链、BT peer 命令、RPC 监听命令 |
| `routineCommands_` | 例行命令：全局管家 | 每圈无条件（STATUS_ALL）执行 | FillRequestGroupCommand、AutoSaveCommand、TimedHaltCommand 等 |

每个命令还带一个**状态标签**（[src/Command.h:L46-L52](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Command.h#L46-L52)）：`STATUS_ALL`（无意义占位）、`STATUS_INACTIVE`（睡眠中，没挂任何 I/O 事件或等待超时）、`STATUS_ACTIVE`（已挂上 I/O 事件、随时可能就绪）、`STATUS_REALTIME`（每圈都要跑）、`STATUS_ONESHOT_REALTIME`。调度时的过滤规则只有一行：`statusFilter <= status_`（[src/Command.h:L90-L93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Command.h#L90-L93)）——数值上 `ALL(0) < INACTIVE(1) < ACTIVE(2)`，所以「用 ACTIVE 过滤」会放行 ACTIVE 及以上，「用 ALL 过滤」放行一切。

为什么要区分？**省 CPU**。一个正在等服务器响应的命令是 INACTIVE 的，它只依赖「超时」这一种唤醒条件，每秒被全量刷新闸门叫醒一次检查超时就够了；而挂了 socket 读事件的命令是 ACTIVE 的，poll 报告事件就绪时才需要执行。于是引擎在 99% 的圈里只执行 ACTIVE 命令，每秒才有一圈把所有人都叫醒。

#### 4.2.2 核心流程

`executeCommand` 的调度规则（对两条队列通用）：

```text
executeCommand(queue, filter):
    max = queue 当前长度            # 先拍照：本轮只处理存量
    循环 max 次:
        com = queue 队首出队
        if com 的状态不匹配 filter:
            com 清空 I/O 事件标记
            com 重新压回队尾          # 原地休眠，等下一圈
            continue
        com.transitStatus()          # ACTIVE → INACTIVE（REALTIME 除外）
        if com.execute():
            销毁 com                 # 返回 true：使命完成
        else:
            com.clearIOEvents()
            com.release()            # 返回 false：命令已自我 requeue，放弃所有权
```

一条「等待数据」的命令典型的 requeue 循环是：

```text
命令挂读事件(setStatusActive) → 引擎 poll 到可读 → executeCommand 选中它
  → transitStatus 变回 INACTIVE → execute() 内部 read 后发现数据没读完
  → addCommandSelf() 把自己塞回 commands_ → return false → 引擎 release
  → 下一圈 poll 又发现可读 → ……（直到数据读完返回 true 被销毁）
```

而一条**超时命令**的完整路径（本讲学习目标之一）：

```text
1. 命令挂上读/写事件，checkPoint_ 记录当前 wallclock，进入等待
2. 引擎每圈执行它：shouldProcess() 为假（事件没到）
3. 检查 checkPoint_ 距今 >= timeout_？
   ├─ 否：addCommandSelf() 重新入队，return false（继续睡，每秒至少被
   │       STATUS_ALL 闸门唤醒一次来做这个检查）
   └─ 是：标记 ServerStat 为 error、把该 IP 记为坏地址
           抛出 DL_RETRY_EX2(EX_TIME_OUT, TIME_OUT)
4. 异常被 execute() 内的 catch(DlRetryEx) 捕获：tryCount+1、重置 URI
5. 未超过 --max-tries → prepareForRetry(0)：
   新建 CreateRequestCommand 入队 + setNoWait(true)，返回 true
   → 旧命令销毁，新命令从连接池/DNS 重新出发
   超过 --max-tries → 记录失败结果，onAbort() 收尾
```

#### 4.2.3 源码精读

**调度核心 `executeCommand`**。[src/DownloadEngine.cc:L118-L139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L118-L139)：注意三个细节——`max` 先拍照所以本轮 requeue 的命令要等下一圈；不匹配过滤器的命令被压回队尾存活；`execute()` 返回 false 时 `release()` 故意放弃 `unique_ptr` 所有权（命令在 `execute()` 里已通过 `addCommandSelf` 之类的调用把自己交给了队列）。

```cpp
size_t max = commands.size();
for (size_t i = 0; i < max; ++i) {
  auto com = std::move(commands.front());
  commands.pop_front();
  if (!com->statusMatch(statusFilter)) {
    com->clearIOEvents();
    commands.push_back(std::move(com));   // 不匹配：回队尾睡觉
    continue;
  }
  com->transitStatus();
  if (com->execute()) {
    com.reset();                          // true → 销毁
  }
  else {
    com->clearIOEvents();
    com.release();                        // false → 命令自己已 requeue
  }
}
```

**状态迁移**。[src/Command.cc:L50-L58](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Command.cc#L50-L58)：`transitStatus` 在执行前把 ACTIVE 打回 INACTIVE——命令必须在每次 `execute()` 里重新证明自己「有事件就绪」并重新调用 `setStatusActive()`。REALTIME 命令（如 `FillRequestGroupCommand`，构造函数里 `setStatusRealtime()`，见 [src/FillRequestGroupCommand.cc:L49-L53](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FillRequestGroupCommand.cc#L49-L53)）不受影响，每圈必跑。

**routine 命令的两副面孔**。第一副：`FillRequestGroupCommand`（[src/FillRequestGroupCommand.cc:L57-L97](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FillRequestGroupCommand.cc#L57-L97)）——每圈检查 `queueCheckRequested()` 标志，调用 `rgman->fillRequestGroupFromReserver(e_)` 把等待队列里的 `RequestGroup` 变成命令塞进 `commands_`；只要还有下载没完成，它就在函数末尾 `e_->addRoutineCommand(std::unique_ptr<Command>(this))` 自我续命（[L83](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FillRequestGroupCommand.cc#L83)），`downloadFinished()` 时才返回 `true` 退出。**主循环的退出时机正是由这类 routine 命令集体返回 true 决定的**。第二副：`TimeBasedCommand` 骨架（[src/TimeBasedCommand.cc:L55-L79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TimeBasedCommand.cc#L55-L79)）——`checkPoint_` 到点才调子类的 `process()`，否则直接把自己 requeue，AutoSave、HaveErase 等周期命令都靠它实现「每 N 秒干活」。

```cpp
// TimeBasedCommand::execute 的节拍器
if (checkPoint_.difference(global::wallclock()) >= interval_) {
  checkPoint_ = global::wallclock();
  process();                 // 到点：子类的实际工作
  if (exit_) return true;
}
postProcess();
if (routineCommand_) {
  e_->addRoutineCommand(std::unique_ptr<Command>(this));  // 自我续命
}
```

**任务命令的 requeue 与超时**。[src/AbstractCommand.cc:L306-L344](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L306-L344)：这是所有协议命令共享的尾部逻辑。`shouldProcess()` 为假（I/O 未就绪，判定逻辑在 [L141-L177](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L141-L177)，检查读/写事件标记与接收缓冲）且未超时的时候，走 [L343](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L343) 的 `addCommandSelf()` 重新入队：

```cpp
if (checkPoint_.difference(global::wallclock()) >= timeout_) {
  // 超时：标记 ServerStat 错误、把连接过的 IP 记为坏地址（DNS 缓存降级）
  throw DL_RETRY_EX2(EX_TIME_OUT, error_code::TIME_OUT);
}
addCommandSelf();   // 定义在 L941-L944：e_->addCommand(unique_ptr<Command>(this))
return false;       // 告诉引擎「我已自己排队」
```

超时抛出的 `DlRetryEx` 被同函数的 catch 接住（[L364-L396](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L364-L396)）：`tryCount+1`、判断 `--max-tries`，未达上限则 `prepareForRetry(0)`。[src/AbstractCommand.cc:L438-L470](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L438-L470)：`prepareForRetry` 把当前 `Request` 放回池子，新建一个 `CreateRequestCommand` 入队；参数 `wait != 0` 时还会把它设成 `STATUS_INACTIVE`——让新命令先睡，等每秒一次的全量刷新闸门叫醒，这就是「延迟重试」的实现（配合 `--retry-wait` 的场景在 u4-l1 展开）。

**入队接口**。以上所有 requeue 最终都汇入 [src/DownloadEngine.cc:L578-L588](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L578-L588) 的两个 `addCommand` 重载（单个/批量）与 [L299-L302](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L299-L302) 的 `addRoutineCommand`。

#### 4.2.4 代码实践

**实践目标**：做一次纯源码阅读实践，把「INACTIVE 命令如何被 1 秒闸门唤醒并 requeue」的时序亲手画出来。

**操作步骤**：

1. 打开 [src/DownloadEngine.cc:L168-L177](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L168-L177)，确认闸门两个分支的过滤器差异。
2. 打开 [src/AbstractCommand.cc:L251-L252](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L251-L252)（`if (shouldProcess())` 分支）与 [L306-L344](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L306-L344)，找出：命令在哪个调用里挂读/写事件并 `setStatusActive()`（提示：在 `setReadCheckSocket`/`setWriteCheckSocket` 相关代码里，可全局搜索）；超时判定用到的 `checkPoint_` 在哪里被刷新。
3. 画出时序图：`run → waitData(poll) → executeCommand(ACTIVE 过滤) → AbstractCommand::execute → addCommandSelf → 下一圈 →（1 秒后）executeCommand(ALL 过滤) → 超时检查`。

**需要观察的现象**：时序图里应能标出「同一命令在同一秒内可能被执行多次（事件就绪）」与「睡眠命令每秒只被执行一次（闸门）」两类节奏。

**预期结果**：能指出 `transitStatus` 把 ACTIVE 打回 INACTIVE 的时机在 `execute()` 调用**之前**，因此命令若想保持 ACTIVE 必须在每次执行中重新挂事件。纯阅读即可完成；如想用日志佐证，可在 `addCommandSelf` 处加 `A2_LOG_INFO` 打印 CUID 后重编译，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`executeCommand` 为什么先把 `commands.size()` 存进 `max`，而不是写 `while (!commands.empty())`？

**答案**：因为命令在 `execute()` 内可能把自己或新命令 requeue 回本队列。先拍照 `max` 保证本轮只处理循环开始时的存量命令，新入队的命令（包括 requeue 的自己）排到队尾等下一圈，避免「命令反复自我续命」导致同一圈内饿死其他命令、甚至死循环。

**练习 2**：`execute()` 返回 false 后引擎调用 `com.release()` 放弃所有权，如果命令忘记把自己 requeue 会怎样？

**答案**：裸指针再无人持有也无人销毁，内存泄漏。这是 `execute()` 返回 false 的协议约定：**返回 false 的命令必须已经把自己的所有权转移给某条队列（或别的容器）**。这也是 `TimeBasedCommand::execute`、`FillRequestGroupCommand::execute` 末尾都有一句 `addXXXCommand(std::unique_ptr<Command>(this))` 的原因。

**练习 3**：一个命令执行中被 `STATUS_ACTIVE` 过滤器跳过、被压回队尾时，它的状态可能是什么？它还活着吗？

**答案**：通常是 `STATUS_INACTIVE`（睡眠者，比如等待重试的 `CreateRequestCommand`），也可能是尚未到点的 REALTIME 之外的其它低优先级状态。它完全存活——`unique_ptr` 被移回队列，只是本圈不执行；每秒一次的 `STATUS_ALL` 闸门仍会执行它。被销毁的唯一途径是 `execute()` 返回 true。

### 4.3 DownloadEngineFactory：按选项装配引擎

#### 4.3.1 概念说明

`DownloadEngine` 自己不读选项、不挑后端，装配工作全部交给 `DownloadEngineFactory::newDownloadEngine(op, requestGroups)`。它是「选项 → 引擎形态」的翻译器，翻译三样东西：

1. **事件轮询后端**：`--event-poll` 选项 + 编译期宏（u1-l2 讲过的探测通道）共同决定用 libuv/epoll/kqueue/port/poll/select 中的哪一个。
2. **管理器三件套**：`RequestGroupMan`（任务队列，容量 = `--max-concurrent-downloads`）、`FileAllocationMan`（预分配队列）、`CheckIntegrityMan`（校验队列）。
3. **routine 命令清单**：哪些周期命令存在完全由选项决定——这就是学习目标里「按选项组装引擎」的含义。`--auto-save-interval=0` 就没有 AutoSaveCommand；`--stop` 未设就没有 TimedHaltCommand。

#### 4.3.2 核心流程

`newDownloadEngine` 的装配顺序（[src/DownloadEngineFactory.cc:L148-L223](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc#L148-L223)）：

```text
1. createEventPoll(op)             # 按 --event-poll 与编译宏选后端，失败抛异常
2. new DownloadEngine(eventPoll)   # 引擎构造（生成 20 字节随机会话 ID）
3. 装配 RequestGroupMan（容量=max-concurrent-downloads）+ initWrDiskCache()
4. 装配 FileAllocationMan / CheckIntegrityMan
5. 无条件注册 4 条 routine 命令：
   FillRequestGroupCommand / FileAllocationDispatcherCommand
   / CheckIntegrityDispatcherCommand / EvictSocketPoolCommand(30s)
6. 条件注册：
   --auto-save-interval>0   → AutoSaveCommand(该间隔)
   --save-session-interval>0 → SaveSessionCommand(该间隔)
   → HaveEraseCommand(10s)  （BT have 消息广播缓存清理，无条件）
   --stop>0                 → TimedHaltCommand(该秒数)
   --stop-with-process      → WatchProcessCommand(pid)
7. --enable-rpc=true         → HttpListenCommand 绑定端口入 commands_（非 routine）
```

#### 4.3.3 源码精读

**事件后端选择**。[src/DownloadEngineFactory.cc:L86-L146](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc#L86-L146)：`createEventPoll` 读 `PREF_EVENT_POLL` 字符串，按 `HAVE_LIBUV → HAVE_EPOLL → HAVE_KQUEUE → HAVE_PORT_ASSOCIATE → HAVE_POLL → select` 的顺序逐个匹配编译宏+选项值；每个后端构造后先 `good()` 体检，失败抛 `DL_ABORT_EX` 并提示 `Try --event-poll=select`。这是 u1-l2「configure 探测→宏→运行时分支」链条在引擎里的落点。

**引擎与管理器装配**。[src/DownloadEngineFactory.cc:L151-L162](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc#L151-L162)：从选项读出 `MAX_CONCURRENT_DOWNLOADS`，把命令行阶段攒下的 `requestGroups` 整体移交给 `RequestGroupMan`，并初始化写盘缓存（`--disk-cache`，u5-l6 详讲）。注意任务从此归引擎所有——这就是 u1-l3 里「RequestGroups will be transferred to DownloadEngine」注释的含义。

**routine 命令注册**。[src/DownloadEngineFactory.cc:L163-L195](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc#L163-L195)：

| 注册条件 | 命令 | 周期 | 作用（后续讲义） |
| --- | --- | --- | --- |
| 无条件 | FillRequestGroupCommand | 每圈 | 从等待队列补充任务（u3-l1） |
| 无条件 | FileAllocationDispatcherCommand | 每圈 | 驱动文件预分配队列（u5-l4） |
| 无条件 | CheckIntegrityDispatcherCommand | 每圈 | 驱动校验队列（u5-l5） |
| 无条件 | EvictSocketPoolCommand | 30s | 清理超时的连接池 socket |
| `--auto-save-interval > 0` | AutoSaveCommand | 该间隔 | 定期保存 `.aria2` 控制文件（u6-l11） |
| `--save-session-interval > 0` | SaveSessionCommand | 该间隔 | 定期保存会话文件 |
| 无条件 | HaveEraseCommand | 10s | 清理 BT have 广播记录（u6-l5） |
| `--stop > 0` | TimedHaltCommand | 一次性 | 到点请求停机（[src/TimedHaltCommand.cc:L63-L76](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TimedHaltCommand.cc#L63-L76)） |
| `--stop-with-process` 定义 | WatchProcessCommand | 每圈 | 监视进程退出即停机 |

**RPC 监听走的是另一条队列**。[src/DownloadEngineFactory.cc:L196-L221](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc#L196-L221)：`--enable-rpc` 时为 IPv4/IPv6 各建一个 `HttpListenCommand`，绑定 `--rpc-listen-port` 后用 `addCommand`（不是 `addRoutineCommand`）进入 `commands_`——它是「常驻任务命令」，靠 socket 事件驱动而非每圈轮询（u8-l3 详讲）。

**调用点**。命令行模式下工厂在 `MultiUrlRequestInfo::prepare()` 中被调用（[src/MultiUrlRequestInfo.cc:L205-L207](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L205-L207)），随后 `prepare` 还会给引擎装上 StatCalc（进度输出，[L282](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L282)）、Cookie/Netrc/TLS 上下文等外设，最后 `execute()` 里的 `e_->run()`（[L356](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L356)）点燃主循环——本讲 4.1 的循环由此启动。

#### 4.3.4 代码实践

**实践目标**：验证「routine 命令清单随选项变化」，体会工厂的按需装配。

**操作步骤**：

1. 重新阅读 [src/DownloadEngineFactory.cc:L172-L195](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc#L172-L195)，抄一张「选项 → routine 命令」对照表。
2. 运行 `./src/aria2c --stop=3 -d ./tmp-download <某个URL>`：让 `--stop=3` 注册 TimedHaltCommand。
3. 对照 [src/TimedHaltCommand.cc:L63-L76](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TimedHaltCommand.cc#L63-L76)，理解 3 秒后发生了什么：`process()` 打印 NOTICE 日志并调用 `requestHalt()`。

**需要观察的现象**：无论下载是否完成，进程都会在约 3 秒时打印类似「timeout」的 NOTICE 消息（`MSG_TIME_HAS_PASSED`）并进入停机收尾。

**预期结果**：把 `--stop=3` 去掉重跑，进程不会再在 3 秒停机——证明 TimedHaltCommand 确实因该选项才被注册。具体日志文本与退出码**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`--event-poll=epoll` 在一台没有 epoll 的系统上会怎样？

**答案**：`HAVE_EPOLL` 未定义时，`createEventPoll` 中 epoll 分支根本没被编译进去，选项值匹配不到任何分支，最终落到 `assert(0)` 之前——实际上选项校验层（u1-l5 的 OptionHandler 取值范围）通常会更早拦截非法值；而若编译进了 epoll 但运行时初始化失败，则 `good()` 返回假，抛出 `DL_ABORT_EX`，提示用户改用 `--event-poll=select`（[src/DownloadEngineFactory.cc:L101-L109](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc#L101-L109)）。

**练习 2**：为什么 `HttpListenCommand` 用 `addCommand` 进入 `commands_`，而 AutoSaveCommand 用 `addRoutineCommand`？

**答案**：`HttpListenCommand` 挂着监听 socket 的读事件，属于事件驱动——有连接进来才需要执行，放 `commands_` 由 poll 唤醒；AutoSaveCommand 没有任何 socket，只依赖时间，放 `routineCommands_` 每圈被 `executeCommand(routineCommands_, STATUS_ALL)` 执行并靠 `TimeBasedCommand` 的 `checkPoint_` 判断到点与否。

**练习 3**：删去 `FillRequestGroupCommand` 的注册，下载还能开始吗？

**答案**：不能（对于超出并发上限的任务）。首批任务虽可能在 `prepare` 阶段被其他路径启动，但等待队列里的 `RequestGroup` 全靠 `FillRequestGroupCommand::execute` 调用 `fillRequestGroupFromReserver` 补进引擎（[src/FillRequestGroupCommand.cc:L63-L82](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FillRequestGroupCommand.cc#L63-L82)），没有它，队列检查标志永远无人消费，后续任务永远不会启动。

## 5. 综合实践

给主循环装上「心电图」：在 `DownloadEngine::run` 里打印每轮两条队列的长度，观察一次完整下载中命令数量的消长曲线。

**实践目标**：把 4.1 的循环节奏、4.2 的队列调度、4.3 的装配清单在一个实验里全部可视化。

**操作步骤**：

1. 按 u1-l2 完成构建（`autoreconf -i && ./configure && make -j$(nproc)`）。
2. 编辑 `src/DownloadEngine.cc` 的 `run()`，在 `while` 循环体第一行（`waitData()` 调用之前）插入一行日志。**以下为示例代码（非项目原有代码）**：

   ```cpp
   // 示例代码：观察两条命令队列的长度变化
   A2_LOG_INFO(fmt("HEARTBEAT commands=%zu routine=%zu", commands_.size(),
                   routineCommands_.size()));
   ```

   `fmt` 已在该文件包含（`#include "fmt.h"`），无需新增头文件。
3. 重新 `make -j$(nproc)`，产出新的 `src/aria2c`。
4. 跑一次小文件下载，按任务要求关闭彩色输出，并把 INFO 日志落盘：

   ```bash
   ./src/aria2c --enable-color=false -l=hb.log --log-level=info \
       -x2 -s2 -d ./tmp-download <某个几MB的URL>
   ```
5. 提取曲线数据：

   ```bash
   grep -o 'HEARTBEAT commands=[0-9]* routine=[0-9]*' hb.log | head -100
   ```

**需要观察的现象**：

- `routine` 数字从头到尾基本恒定（默认配置约 5：Fill + FileAllocationDispatcher + CheckIntegrityDispatcher + EvictSocketPool + HaveErase），验证 4.3 的装配清单。
- `commands` 呈三段式：启动阶段从 0 快速攀升（每条连接的命令链入队）；稳定下载阶段维持在与活跃连接数相当的水平（受 `-x`/`-s` 影响）；收尾阶段单调下降到 0，随后整个进程退出——两条队列同时清空，主循环因 `while` 条件不成立而结束。
- 用 `-l --log-level=info` 时日志量很大，可改用 `--log-level=notice` 对照确认心跳行只在 info 级别出现。

**预期结果**：能拿一条「时间 × 队列长度」的折线（可把数字粘贴进表格软件画图），并用自己的话解释每个台阶对应下载的哪个阶段（建立连接、传输、收尾）。实验结束后请把示例代码删掉并重新编译，保持工作区干净。运行结果依赖网络环境，**待本地验证**。

## 6. 本讲小结

- `DownloadEngine::run` 是一个「等数据（`waitData`/poll）→ 执行命令（两条队列、状态过滤）→ 收尾检查（`afterEachIteration`）」的无限循环，退出条件是两条队列同时为空。
- 每秒一次的「全量刷新闸门」（`t + 10ms >= refreshInterval_`）让 `commands_` 交替以 `STATUS_ACTIVE` / `STATUS_ALL` 两种过滤器执行：平时只跑事件就绪的命令，每秒叫醒一次睡眠者检查超时。
- `executeCommand` 的协议：`execute()` 返回 true 即销毁；返回 false 则命令必须已自我 requeue（`addCommandSelf`/`addRoutineCommand(this)`），引擎 `release()` 放弃所有权。
- 超时命令的路径：`shouldProcess()` 为假 → 未超时则 requeue 续睡；超时则抛 `TIME_OUT` → `catch(DlRetryEx)` → `prepareForRetry` 生成 `CreateRequestCommand` 重新出发，`wait != 0` 时新命令以 `STATUS_INACTIVE` 入队实现延迟重试。
- `routineCommands_` 是引擎的管家班子：`FillRequestGroupCommand` 每圈补充任务（也是循环退出的裁定者），`TimeBasedCommand` 系按周期干活。
- `DownloadEngineFactory::newDownloadEngine` 完成「选项 → 引擎形态」的翻译：事件后端（`--event-poll` × 编译宏）、三大管理器、以及随选项条件注册的 routine 命令清单；RPC 监听则作为常驻任务命令进入 `commands_`。

## 7. 下一步学习建议

本讲把主循环当作黑盒调用了 `eventPoll_->poll(tv)`，下一讲 **u2-l2（EventPoll：I/O 多路复用的多后端抽象）** 就拆开这个黑盒：epoll/kqueue/port/select 四套后端如何用统一接口注册命令与 socket、事件如何标记回命令对象。之后 **u2-l3（Command 体系）** 将深入 `AbstractCommand::execute` 的完整状态机（本讲只借用了它的超时分支），把 INITIATE→REQUEST→RESPONSE→DOWNLOAD 的接力讲透。建议阅读顺序：先 u2-l2 再 u2-l3，最后带着这两讲的知识回看本讲的「综合实践」日志，你会认出每一行心跳背后的命令身份。
