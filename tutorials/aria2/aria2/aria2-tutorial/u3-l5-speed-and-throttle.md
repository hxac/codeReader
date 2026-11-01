# 速度统计与限速：SpeedCalc 与统计输出

## 1. 本讲目标

学完本讲，你应该能够：

- 解释 `SpeedCalc` 的 10 秒滑动窗口测速算法：数据如何按秒入槽、旧槽如何淘汰、速度如何计算。
- 跟踪一个字节数从 socket 读出后，经 `PeerStat`、`DownloadContext::NetStat` 汇入 `RequestGroupMan::NetStat` 的三级回流链路。
- 说明 `DownloadEngine::calculateStatistics()` 如何每轮驱动 `StatCalc`，`ConsoleStatCalc` 又如何节流到每秒一行地渲染控制台进度。
- 说出限速的两级上限（`--max-download-limit` 与 `--max-overall-download-limit`）和三个真正的执行点（`DownloadCommand`、`PeerInteractionCommand`/`DefaultBtInteractive`、`DefaultBtMessageDispatcher`），以及「取消读事件注册 → 命令退化为每秒一次轮询」这一核心手段。

一个需要先纠正的认知：**DownloadEngine 本体并不做限速**。它在主循环里只负责两件事——每轮调用 `calculateStatistics()` 刷新统计、每秒做一次全量命令唤醒。真正的限速检查发生在各协议的「数据消费命令」内部，靠摘除 EventPoll 读/写事件注册来减速。本讲会把这条真实链路完整拆开。

## 2. 前置知识

- **瞬时速度 vs 平均速度**：瞬时速度回答「刚刚这 10 秒平均每秒传了多少」，用滑动窗口计算，是限速判断的依据；平均速度回答「整个会话平均每秒多少」，用总字节除以总时长，是结果报告用的。aria2 对两者分开实现。
- **滑动窗口**：只保留最近 N 秒内的数据样本，更老的样本被淘汰。窗口内的总字节数除以「窗口首样本时刻到现在」的时长，即得窗口平均速度。
- **事件驱动与轮询的退化**：u2-l1/u2-l2 讲过，命令靠向 EventPoll 注册读事件被「按需唤醒」。如果一个命令把自己的读事件注册取消（`disableReadCheckSocket`），它就再也收不到 I/O 通知，只能等 DownloadEngine 每秒一次的 `STATUS_ALL` 全量唤醒——这正是 aria2 限速的实现手法：**不是丢弃数据，而是让数据留在内核缓冲区里没人读**。
- **两级限速选项**：`--max-download-limit`（作用于单个 RequestGroup 任务）与 `--max-overall-download-limit`（作用于全进程所有任务之和），上传侧还有 `--max-upload-limit` / `--max-overall-upload-limit`。它们的键名登记在 [src/prefs.cc:191-193](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.cc#L191-L193) 与 [src/prefs.cc:467-469](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.cc#L467-L469)。
- 建议先完成 u2-l1（DownloadEngine 主循环）与 u2-l3（Command 生命周期），本讲大量复用「状态位」「requeue」「每秒全量刷新闸门」这些概念。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/SpeedCalc.h` / `src/SpeedCalc.cc` | 滑动窗口测速器，一切速度数字的源头 |
| `src/NetStat.h` / `src/NetStat.cc` | 对 SpeedCalc 的封装：下载/上传两把测速器 + 会话字节数 + 状态机 |
| `src/TransferStat.h` | 速度与字节数的快照结构体（RPC、控制台共用的「读数」） |
| `src/PeerStat.cc` | 每服务器/每 peer 的统计封装，内部同样持有一个 NetStat |
| `src/DownloadContext.cc` | `updateDownload` 等入口，把任务级字节回流到全局 NetStat |
| `src/RequestGroup.cc` / `src/RequestGroupMan.cc` | 两级限速的判定函数与选项读取 |
| `src/DownloadCommand.cc` | HTTP/FTP/SFTP 流式下载命令：限速的第一执行点 |
| `src/PeerInteractionCommand.cc` / `src/DefaultBtInteractive.cc` / `src/DefaultBtMessageDispatcher.cc` | BT 侧限速的另外两个执行点 |
| `src/AbstractCommand.cc` | `setReadCheckSocket` / `disableReadCheckSocket` 等事件注册开关 |
| `src/DownloadEngine.cc` | 主循环对 `calculateStatistics()` 的调用点 |
| `src/StatCalc.h` / `src/ConsoleStatCalc.h` / `src/ConsoleStatCalc.cc` | 统计输出接口与控制台进度行渲染器 |
| `src/OutputFile.h` / `src/console.cc` | 输出介质抽象（stdout/stderr/Windows 控制台/空设备） |
| `src/MultiUrlRequestInfo.cc` | `getStatCalc` 工厂：按选项装配 ConsoleStatCalc 或 NullStatCalc |
| `test/SpeedCalcTest.cc` | SpeedCalc 的最小单元测试（仅验证 update 不崩溃） |

## 4. 核心概念与源码讲解

### 4.1 SpeedCalc：滑动窗口测速器

#### 4.1.1 概念说明

`SpeedCalc` 回答一个问题：**「刚刚平均每秒传了多少字节？」** 它是 aria2 中一切速度数字的唯一来源——控制台的 `DL:`/`UL:`、限速判断、`--lowest-speed-limit` 慢速踢除、`--optimize-concurrent-downloads` 的并发调优，读的全部是它（或其包装 NetStat）的输出。

它的核心数据结构非常朴素：

- `timeSlots_`：一个 `deque<pair<Timer, size_t>>`，每个元素是「时刻 → 该秒内传输的字节数」。注意粒度是**秒**：同一秒内的多次 `update` 会合并进同一个槽。
- `bytesWindow_`：窗口内字节总数的增量维护值（避免每次求和遍历）。
- `accumulatedLength_`：自 `start_` 以来的累计总字节，供平均速度使用。
- `maxSpeed_`：历史最大瞬时速度。

#### 4.1.2 核心流程

写入侧（`update(bytes)`）：

```
每秒至多一个槽：
  若距最后一个槽不足 1 秒 → 累加到尾槽的 second
  否则                     → 尾部追加新槽 (now, bytes)
同步维护 bytesWindow_ += bytes、accumulatedLength_ += bytes
```

读取侧（`calculateSpeed()`）：

```
1. removeStaleTimeSlot(now)：从队首弹出所有「距 now 超过 10 秒」的旧槽，
   每弹一个就把其字节数从 bytesWindow_ 扣掉
2. 若窗口已空 → 返回 0
3. elapsed = now - 队首槽时刻（毫秒，下限 1）
4. speed = bytesWindow_ * 1000 / elapsed
```

写成公式即：

\[
\text{speed} = \frac{B_{\text{window}} \times 1000}{t_{\text{now}} - t_{\text{oldest}}\ (\text{ms})}
\]

注意分母是「队首槽到现在」的实际时长而不是写死的 10 秒——所以下载刚开始的几秒，窗口虽然只有 2 秒数据，算出来的依然是这 2 秒的真实平均，不会被人为主拉低。

#### 4.1.3 源码精读

窗口长度常量是 10 秒，定义在匿名命名空间里：[src/SpeedCalc.cc:44-46](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SpeedCalc.cc#L44-L46)（`constexpr auto WINDOW_TIME = 10_s;`）。

旧槽淘汰：`removeStaleTimeSlot` 从队首（最老的槽）开始，只要该槽距 now 超过 `WINDOW_TIME` 就把它的字节从 `bytesWindow_` 中减掉并弹出——[src/SpeedCalc.cc:59-68](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SpeedCalc.cc#L59-L68)。由于 `timeSlots_` 按时间有序，这个「从队首弹到不超时为止」的循环是均摊 O(1) 的。

瞬时速度计算（窗口字节 ÷ 窗口实际跨度，同时顺手更新 `maxSpeed_`）：[src/SpeedCalc.cc:70-86](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SpeedCalc.cc#L70-L86)。其中 `elapsed <= 0` 时钳为 1，防止同毫秒内除零。

按秒入槽的写入逻辑：[src/SpeedCalc.cc:114-128](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SpeedCalc.cc#L114-L128)——距尾槽满 1 秒才开新槽，否则合并，这保证了窗口内最多约 11 个元素，内存与遍历成本都可控。

平均速度：[src/SpeedCalc.cc:130-144](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SpeedCalc.cc#L130-L144) 用 `accumulatedLength_ / 总时长`，并且**时长不足 4 毫秒时直接返回 0**，注释直言「太短的平均速度是垃圾数据」。

类成员一览见 [src/SpeedCalc.h:46-73](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SpeedCalc.h#L46-L73)。另有一个 `calculateNewestSpeed(seconds)`（[src/SpeedCalc.cc:88-112](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SpeedCalc.cc#L88-L112)）：从**最新**一端往回取指定秒数内的槽求和再除以跨度，用于「最近 N 秒」这种更短视的读数。

#### 4.1.4 代码实践

**实践目标**：亲手验证滑动窗口的数学行为，而不是只看公式。

1. 阅读 [test/SpeedCalcTest.cc:22-26](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/SpeedCalcTest.cc#L22-L26)——它只调了一次 `update(1000)`，是「冒烟级」用例。仿照它的写法（示例代码，非项目原有）：

   ```cpp
   // 示例代码：放在测试里跑，或临时加进 SpeedCalcTest.cc 验证后还原
   SpeedCalc calc;
   calc.update(1000);              // 第 0 秒：1000 字节
   // 睡 2 秒后（可用 usleep 或测试框架的等待）：
   calc.update(3000);              // 第 2 秒：3000 字节
   int spd = calc.calculateSpeed();
   // 预期约为 4000B / 2s ≈ 2000B/s 上下（取决于实际间隔）
   ```

2. 对照源码推导：`timeSlots_` 此时有两个槽，`bytesWindow_ = 4000`，`elapsed ≈ 2000ms`，代入公式验证你的观测。
3. 思考实验（不必改码）：如果连续 11 秒每秒 `update(1000)`，第 12 秒调用 `calculateSpeed()` 时队首那一个 10 秒前的槽会被 `removeStaleTimeSlot` 弹掉吗？弹掉后 `bytesWindow_` 是多少？

**需要观察的现象**：槽的合并与淘汰如何让 `speed` 在流量停止后 1~2 秒内自然回落到 0（队首槽随时间推移逐个过期，最终 `timeSlots_` 为空、返回 0）。

**预期结果**：能用自己的话讲清「为什么 aria2 显示的 DL 速度最多滞后约 10 秒、且不会出现除零」。若无法在本地编译运行，标注「待本地验证」即可。

#### 4.1.5 小练习与答案

**练习 1**：`calculateSpeed` 的分母为什么用「队首槽到现在」而不是固定 10 秒？
**答案**：因为窗口是「最多保留 10 秒」而不是「固定 10 秒」。启动初期或流量断续时窗口内可能只有 2~3 秒的数据，用实际跨度才能得到这 2~3 秒的真实平均；若硬除 10 秒会系统性低估速度，限速判断也会随之失真。

**练习 2**：`update` 为什么要「同一秒合并进尾槽」？
**答案**：控制元素数量上界。下载命令每轮循环搬运 16KB 级别的数据，一秒内可能 `update` 数百次；若每次都开新槽，窗口内元素数与遍历成本将正比于调用次数。合并后窗口最多约 11 个槽，`removeStaleTimeSlot` 的均摊弹出也保持 O(1)。

**练习 3**：`calculateAvgSpeed` 为什么在时长 ≤ 4ms 时返回 0？
**答案**：样本期太短时，任何微小抖动都会被除法放大成荒谬的瞬时读数（源码注释原话是「the average speed is rubbish」），返回 0 是更安全的哨兵值。

### 4.2 NetStat、PeerStat 与字节回流链路：谁在喂 SpeedCalc

#### 4.2.1 概念说明

`SpeedCalc` 只是算盘，还需要有人把「刚读了多少字节」喂给它。aria2 有三层统计口径，层层汇聚：

1. **PeerStat（每服务器/每 peer）**：`DownloadCommand` 里每个连接对应一个 `PeerStat`，内部持有一个 `NetStat`。它服务于慢速踢除（`--lowest-speed-limit`）、ServerStat 测速沉淀（u3-l1 讲过的换源决策）。
2. **DownloadContext::NetStat（每任务）**：一个 RequestGroup 的总口径，`--max-download-limit` 用它判断。
3. **RequestGroupMan::NetStat（全局）**：所有任务之和，`--max-overall-download-limit` 用它判断，控制台紧凑模式里的全局 `[DL:xxB UL:xxB]` 也读它。

`NetStat` 就是「下载 + 上传两把 SpeedCalc，外加会话字节数与 IDLE/ACTIVE 状态」的打包，见 [src/NetStat.h:111-120](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/NetStat.h#L111-L120)。`TransferStat`（[src/TransferStat.h:42-68](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TransferStat.h#L42-L68)）则是某一时刻的只读快照：两个速度加三个字节数，控制台、RPC `tellStatus`、下载结果报告消费的都是它。

#### 4.2.2 核心流程

一次 HTTP 下载数据搬运的字节回流（对应 u4-l1 的 DownloadCommand 数据泵）：

```
DownloadCommand::executeInternal
  ├─ getSocketRecvBuffer()->drain(bufSize)         消费了 bufSize 字节
  ├─ peerStat_->updateDownload(bufSize)            → PeerStat 的 NetStat（口径 1）
  └─ getDownloadContext()->updateDownload(bufSize) → 任务级 NetStat（口径 2）
        └─ rgman->getNetStat().updateDownload(bytes) → 全局 NetStat（口径 3）
```

`NetStat::downloadStop()` 在会话结束时补算一次平均速度并转入 IDLE（[src/NetStat.cc:117-128](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/NetStat.cc#L117-L128)）；`toTransferStat()` 把当前读数打包成 `TransferStat`（[src/NetStat.cc:130-138](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/NetStat.cc#L130-L138)）。

#### 4.2.3 源码精读

数据泵里的两条 `update` 调用：[src/DownloadCommand.cc:196-198](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L196-L198)——先 `drain(bufSize)` 消费接收缓冲，然后同时喂给 per-peer 与任务级两个口径。

任务级向全局的转发：[src/DownloadContext.cc:304-311](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadContext.cc#L304-L311)，`updateDownload` 更新自身 `netStat_` 后，通过 `ownerRequestGroup_->getRequestGroupMan()` 把同一份数字再累加进全局 NetStat。上传侧的 `updateUploadSpeed` / `updateUploadLength` 同理（[src/DownloadContext.cc:313-329](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadContext.cc#L313-L329)）。BT 做种时 BT 侧调用的是 `updateUploadSpeed`（只进速度计）与 `updateUploadLength`（只进字节计）。

NetStat 各方法对 SpeedCalc 的薄封装：[src/NetStat.cc:54-98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/NetStat.cc#L54-L98)（`calculateDownloadSpeed` 一行转调 `downloadSpeed_.calculateSpeed()`；`updateDownload` 同时累加 `sessionDownloadLength_`）。

PeerStat 的同构封装：[src/PeerStat.cc:70-72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerStat.cc#L70-L72)。任务级快照的组装在 `RequestGroup::calculateStat`（[src/RequestGroup.cc:988-997](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L988-L997)，BT 场景还会把启动前的历史上传量加上）。

#### 4.2.4 代码实践

**实践目标**：用日志亲眼看一次「一字节三份账」。

1. 在 [src/DownloadCommand.cc:197](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L197) 与 [src/DownloadContext.cc:309](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadContext.cc#L309) 各加一行 `A2_LOG_INFO`（打印 cuid/字节数），重新编译（此为本地实验性修改，验证后请还原，勿提交）。
2. 用 `-x2 -s2` 下载一个文件，观察同一批字节如何先后出现在两条日志里。
3. 对照控制台 `DL:` 读数与日志时间戳，确认它约等于 10 秒窗口内所有 `bufSize` 之和 ÷ 窗口跨度。

**需要观察的现象**：两个连接（两个 cuid）的日志交错出现；任务级口径的数值是两者之和。

**预期结果**：能画出「连接 → PeerStat、连接 → 任务 NetStat → 全局 NetStat」的扇形汇聚图；若未本地编译，写明「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 PeerStat 和 DownloadContext 要分别记账，而不是只留全局一份？
**答案**：三层口径服务三个决策：PeerStat 支撑 `--lowest-speed-limit` 的单连接慢速踢除与 ServerStat 镜像测速；任务级 NetStat 支撑 `--max-download-limit` 的单任务限速；全局 NetStat 支撑 `--max-overall-download-limit` 与紧凑进度行。只留一份的话这三个粒度的决策都无法做。

**练习 2**：`NetStat::updateUploadSpeed` 与 `updateUploadLength` 为什么拆成两个方法，而下载只有一个 `updateDownload`？
**答案**：BT 上传的「速度」与「计入分享率的字节量」口径不同：做种限速时可能需要只累计速度而不把字节算进分享率，反之亦然，所以拆开让调用方自由组合；下载侧两者天然一致，一个方法同时更新即可（见 [src/NetStat.cc:81-98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/NetStat.cc#L81-L98)）。

### 4.3 StatCalc 与 ConsoleStatCalc：进度行是怎么画出来的

#### 4.3.1 概念说明

`StatCalc` 是一个单方法接口：`calculateStat(const DownloadEngine*)`（[src/StatCalc.h:44-49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/StatCalc.h#L44-L49)）。DownloadEngine 每轮主循环都调它，把它当成「周期性回调的钩子」。控制台进度行只是这个钩子的一个实现：

- `NullStatCalc`：`--quiet` 时用，什么都不做。
- `ConsoleStatCalc`：正常模式，每秒渲染一次进度行。

输出介质则被抽象成 `OutputFile`（`write`/`flush`/`printf`/`supportsColor` 四个虚函数，[src/OutputFile.h:48-64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OutputFile.h#L48-L64)），由 `global::cout()` 提供全局句柄。这样同一套渲染代码可以写向 POSIX 的 stdout、Windows 控制台、或者空设备。

#### 4.3.2 核心流程

触发链路（自上而下）：

```
DownloadEngine::run()               每轮主循环
  └─ calculateStatistics()           [DownloadEngine.cc:230]
       └─ statCalc_->calculateStat(this)
            └─ ConsoleStatCalc::calculateStat
                 1. 1 秒节流闸门（cp_ 距上次不足 1000ms 直接返回）
                 2. TTY：ioctl(TIOCGWINSZ) 取列宽，先打印 "\r<空格>\r" 清行
                 3. 到达 --summary-interval → 输出多行 Progress Summary
                 4. 单任务 → printProgress；多任务 → printProgressCompact（最多 5 个）
                 5. 追加 [FileAlloc:..] / [Checksum:..] 阶段读数
                 6. TTY：\r 覆盖式单行（按列宽截断）；非 TTY：追加换行
```

装配链路：`MultiUrlRequestInfo::getStatCalc` 按选项决定 `--quiet`（NullStatCalc）还是 ConsoleStatCalc，并把 `--summary-interval`、`--enable-color`、`--human-readable`、`--show-console-readout`、`--truncate-console-readout` 灌进去（[src/MultiUrlRequestInfo.cc:131-144](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L131-L144)），随后经 `e_->setStatCalc(...)` 注入引擎（[src/MultiUrlRequestInfo.cc:282](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L282)）。

#### 4.3.3 源码精读

主循环的调用点：[src/DownloadEngine.cc:158-185](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L158-L185)，第 167 行每轮（不只是一次一秒）调用 `calculateStatistics()`；其实现只是转调 `statCalc_`：[src/DownloadEngine.cc:230-235](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L230-L235)。「每秒一次」的节流由 ConsoleStatCalc 自己负责。

1 秒节流闸门：[src/ConsoleStatCalc.cc:290-296](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConsoleStatCalc.cc#L290-L296)，距上次不足 1000ms（加 `A2_DELTA_MILLIS` 容差）直接返回——注意这与主循环的 1 秒全量唤醒是两套独立的计时器。

TTY 检测与清行：[src/ConsoleStatCalc.cc:303-320](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConsoleStatCalc.cc#L303-L320)，`isatty` 判定在构造函数完成（[src/ConsoleStatCalc.cc:270-288](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConsoleStatCalc.cc#L270-L288)，同时选择人类可读（`util::abbrevSize`，如 1.2MiB）还是纯字节数格式化器）。

单任务进度行 `printProgress`：[src/ConsoleStatCalc.cc:169-204](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConsoleStatCalc.cc#L169-L204)。ETA 的算法就在这里：

\[
\text{ETA} = \frac{L_{\text{total}} - L_{\text{completed}}}{v_{\text{download}}}
\]

（仅当总长已知且当前速度大于 0 时计算，[src/ConsoleStatCalc.cc:174-178](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConsoleStatCalc.cc#L174-L178)；速度来自 4.2 节的 `TransferStat`。）行内依次输出 GID 缩写、`已下/总量(%)`、`CN:连接数`、BT 的 `SD:种子数`、`DL:`、`UL:` 与 `ETA:`。

多任务紧凑模式 `printProgressCompact`：[src/ConsoleStatCalc.cc:136-166](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConsoleStatCalc.cc#L136-L166)。先输出全局 `[DL:xxB UL:xxB]`——这正是 `RequestGroupMan::getNetStat()`（第 140-142 行）的读数；随后最多列 5 个任务（`MAX_ITEM`），超出部分折叠成 `(+N)`。

周期性多行摘要 `printProgressSummary`：[src/ConsoleStatCalc.cc:238-267](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConsoleStatCalc.cc#L238-L267)，受 `--summary-interval` 控制（0 关闭），每个任务一行进度加一行 `FILE:` 路径（[src/ConsoleStatCalc.cc:207-236](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConsoleStatCalc.cc#L207-L236)）。

单/多任务分流与文件分配/校验阶段附加读数：[src/ConsoleStatCalc.cc:337-386](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConsoleStatCalc.cc#L337-L386)。最终落盘分支在 [src/ConsoleStatCalc.cc:387-402](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConsoleStatCalc.cc#L387-L402)：TTY 下用 `\r` 覆盖同一行（可按列宽截断），**非 TTY 下每秒追加一行带换行**——这就是把 aria2 输出重定向到文件时会看到一长串进度行的原因。

输出介质的装配：[src/console.cc:53-67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/console.cc#L53-L67)——`suppress`（`--quiet`）时全部换成 `NullOutputFile`；Windows 用 `WinConsoleFile`，其余平台用 `BufferedFile(stdout)`。全局句柄取用见 [src/console.cc:75-77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/console.cc#L75-L77)，`redirectStdoutToStderr`（[src/console.cc:69-73](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/console.cc#L69-L73)）用于 RPC 模式把进度行挪到 stderr、给 RPC 输出腾出 stdout。

#### 4.3.4 代码实践

**实践目标**：把「控制台行为 ↔ 源码分支」一一对上。

1. 编译好的 aria2（u1-l2）下载任一大文件，观察 TTY 下的单行覆盖式进度。
2. 同一命令重定向到文件：`./src/aria2c <URL> -o f.bin > progress.log 2>&1`，之后 `less progress.log`，确认每秒一整行——对应非 TTY 分支（第 399-401 行）。
3. 加 `--summary-interval=5`，观察每 5 秒多出一段 `*** Download Progress Summary ***` 多行摘要。
4. 加 `-j2`（配合 `-i` 输入文件放两个任务），观察切换到紧凑模式 `[DL:xxB][#gid xxB/yyB(%)...]` 与 `(+1)` 折叠。
5. 加 `--show-console-readout=false`，确认进度行消失但摘要仍会输出（两者由不同开关控制，[src/ConsoleStatCalc.cc:322-336](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConsoleStatCalc.cc#L322-L336)）。

**需要观察的现象**：清行序列 `\r<空格>\r` 在部分终端里可见残影；ETA 随速度波动而跳动——因为它用的是 10 秒窗口速度而非平均速度。

**预期结果**：能对每次输出差异指出对应的源码分支；无法本地运行时标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 DownloadEngine 每轮都调 `calculateStat`，而进度行每秒才出一行？
**答案**：主循环频率取决于 I/O 事件（可能每秒上千轮），把节流下放到 StatCalc 实现里（`cp_` 闸门）可以让接口保持「每轮给你一次刷新机会」的简单语义，NullStatCalc 等空实现也无需关心频率。

**练习 2**：`--quiet` 在两层同时生效：StatCalc 换成 NullStatCalc、输出介质换成 NullOutputFile。为什么要两处都换？
**答案**：StatCalc 负责的是进度行这个「渲染器」，但日志、下载结果摘要等仍可能直接用 `global::cout()`；把介质换成 NullOutputFile 是兜底，保证任何路径的输出都被吞掉。

**练习 3**：紧凑模式的全局 `DL:` 与单任务行的 `DL:` 数值何时会相等？
**答案**：只有一个活跃任务且该任务无其他连接口径差异时（更准确地说：当全局 NetStat 的窗口字节全部来自该任务时）两者近似相等；多任务并发时全局值是各任务之和，必然更大。

### 4.4 限速的实施：两级上限与三处执行点

#### 4.4.1 概念说明

先把本讲规格里的一处表述校正掉：大纲实践任务里提到的 `shouldIncreaseConcurrency` **在当前源码中不存在**（已全文检索确认）。与「按速度调整并发」最接近的真实机制是 `RequestGroupMan::optimizeConcurrentDownloads()`（`--optimize-concurrent-downloads`），本节末尾会讲它。同时，「DownloadEngine 在两个位置实施限速」的准确表述是：**限速判定有两个层级，执行点散布在三个数据消费命令里**；DownloadEngine 只提供被限速命令「苏醒」的每秒全量唤醒。

两级判定：

- 任务级：`RequestGroup::doesDownloadSpeedExceed()`——`--max-download-limit`，见 [src/RequestGroup.cc:1279-1283](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1279-L1283)，阈值在构造函数从选项读入（[src/RequestGroup.cc:143-144](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L143-L144)，同时读入 `--max-upload-limit`）。
- 全局级：`RequestGroupMan::doesOverallDownloadSpeedExceed()`——`--max-overall-download-limit`，见 [src/RequestGroupMan.cc:986-990](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L986-L990)，阈值同样构造时读入（[src/RequestGroupMan.cc:116-119](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L116-L119)）。两者都是「限值为 0 视为不限制」，且任一层超限即触发。

三处执行点：

1. `DownloadCommand::executeInternal`（HTTP/FTP/SFTP 流式下载）
2. `PeerInteractionCommand::executeInternal` + `DefaultBtInteractive::receiveMessages`（BT 下载）
3. `DefaultBtMessageDispatcher::sendMessagesInternal`（BT 上传/piece 发送）

#### 4.4.2 核心流程

以 `DownloadCommand` 为例的限速状态机（每个命令实例每轮自查）：

```
executeInternal() 入口：
  if 全局超限 or 任务超限:
      addCommandSelf()               # 重新排队（此刻已被 transitStatus 降为 INACTIVE）
      disableReadCheckSocket()       # 摘掉 EventPoll 读事件注册
      disableWriteCheckSocket()      # 摘掉写事件注册
      return false                   # 不销毁、不再搬数据
  else:
      setReadCheckSocket(getSocket()) # 恢复读事件，回到事件驱动模式
      ... 正常搬运一块数据 ...
```

被摘掉事件注册的命令进入「休眠」：EventPoll 不再通知它，DownloadEngine 的 `waitData` 会一直等到 1 秒刷新闸门到期，用 `executeCommand(commands_, STATUS_ALL)` 唤醒所有 INACTIVE 命令（[src/DownloadEngine.cc:168-176](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L168-L176)；`waitData` 的超时正是 `refreshInterval_`，[src/DownloadEngine.cc:187-200](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L187-L200)）。醒来后重新计算窗口速度：仍超限就继续睡，降到限值以下就恢复读事件、继续搬运。由于 TCP/内核接收缓冲的存在，数据只是暂时没人读，发送方最终会因拥塞控制减速——这正是用户看到的「限速生效」。

#### 4.4.3 源码精读

第一执行点（流式下载）：[src/DownloadCommand.cc:133-143](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L133-L143)——`executeInternal` 的最开头就是两级判定，超限则「自排队 + 双摘除 + 返回」；未超限在第 144 行恢复 `setReadCheckSocket`。

摘除/注册的实现（u2-l2 的 EventPoll 契约在此被使用）：[src/AbstractCommand.cc:531-540](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L531-L540) 的 `disableReadCheckSocket` 调 `e_->deleteSocketForReadCheck`（即 `EventPoll::deleteEvents(EVENT_READ)`）；[src/AbstractCommand.cc:542-562](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L542-L562) 的 `setReadCheckSocket` 反向注册并处理目标 socket 变化的换绑。写侧对应 [src/AbstractCommand.cc:575-583](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L575-L583)。

`addCommandSelf` 只是把自己塞回队列（[src/AbstractCommand.cc:941-944](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L941-L944)）——结合 u2-l3 讲过的 `transitStatus()`（REALTIME 一次即降 INACTIVE），被限速命令自然落到 INACTIVE 档，等每秒全量唤醒。

第二执行点（BT 下载）：[src/PeerInteractionCommand.cc:348-357](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L348-L357)——同样的两级判定，超限时 `disableReadCheckSocket()` 并 `setNoCheck(true)`（`noCheck` 为真时命令绕过 I/O 事件门槛每轮都执行，见 [src/AbstractCommand.h:222-224](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.h#L222-L224)）；真正的急刹车在消息接收循环内部：[src/DefaultBtInteractive.cc:301-313](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L301-L313)，`receiveMessages` 的 while 循环**每收一条消息前**都复查一次限速，超限立即 break，不再从 socket 读后续 piece 消息。

第三执行点（BT 上传）：[src/DefaultBtMessageDispatcher.cc:86-106](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L86-L106)——发送队列扫描时，凡 `isUploading()` 的消息（典型如 piece 响应）在上传总量或任务上传超限时被放回队首暂缓发送（`tempQueue` 再插回），非上传消息（have、keepalive 等）不受影响；与之配合，[src/PeerInteractionCommand.cc:363-373](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L363-L373) 在有待发消息且未超上传限时才注册写事件。

顺带的两个「速度消费者」：

- 慢速踢除：`checkLowestDownloadSpeed`（[src/DownloadCommand.cc:302-315](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L302-L315)）用 **PeerStat** 的窗口速度与 `--lowest-speed-limit` 比较，超过 `startupIdleTime_`（默认 10 秒预热）仍过慢就抛 `TOO_SLOW_DOWNLOAD_SPEED` 换源——这就是 u3-l1 讲 URISelector 换源时的数据来源。
- 并发调优：`RequestGroupMan::optimizeConcurrentDownloads`（[src/RequestGroupMan.cc:1063-1105](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L1063-L1105)）以全局窗口速度为输入，5 秒内取历史峰值、超 5 秒向当前值对分收敛（上限不超过 `--max-overall-download-limit`），再按对数公式换算出建议的并行任务数：

  \[
  j = \left\lceil a + b \cdot \log_{10}\frac{v \times 8}{10^6} \right\rceil,\quad 1 \le j \le j_{\max}
  \]

  结果在 `fillRequestGroupFromReserver` 取任务时生效（[src/RequestGroupMan.cc:519-520](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L519-L520)）。

#### 4.4.4 代码实践

**实践目标**：验证 `--max-download-limit=500K` 的实际曲线，并写一段「源码推导笔记」解释超限时究竟发生了什么。

1. 准备一个足够大（几百 MB）的文件与够快的源，运行：

   ```console
   $ ./src/aria2c <URL> -o big.bin --max-download-limit=500K --summary-interval=5
   ```

2. 每 5 秒抄录一次 `DL:` 读数与 `(%)`，画成简表。预期曲线：未限速阶段冲高 → 超过 500KiB/s 后 10 秒窗口读数见顶 → 回落并在 500KiB/s 附近小幅震荡（因为窗口速度一旦低于限值读事件立即恢复，稍一搬运又超限，形成「搬运一秒 → 睡一秒」的锯齿）。
3. 写源码推导笔记，回答三个问题（答案都可在本节引用的行号里找到）：
   - 超限那一刻，`DownloadCommand::executeInternal` 做了哪三件事？（自排队、摘读事件、摘写事件）
   - 被限速的命令靠什么机制「醒来」再检查一次速度？（DownloadEngine 的 1 秒刷新闸门 + `waitData` 的 `refreshInterval_` 超时）
   - 为什么限速不会丢数据？（数据滞留内核/TCP 接收缓冲，靠拥塞控制反压发送方；aria2 侧只是不读）
4. 进阶对比：换 `--max-overall-download-limit=500K` 加 `-j2` 两个任务，观察全局限速下**所有**任务的命令同时被压制；再对比 `--optimize-concurrent-downloads=true` 前后并行任务数的变化。

**需要观察的现象**：限速读数不会精确钉在 500KiB/s，而是在其附近波动——请用「10 秒窗口 + 每秒轮询唤醒」的机制解释这个偏差。

**预期结果**：一份含实测曲线表与三问三答的推导笔记；若本地无法编译运行，标注「待本地验证」并只完成源码推导部分。

#### 4.4.5 小练习与答案

**练习 1**：aria2 的限速是「丢弃/整形流量」还是「暂停读取」？依据是什么？
**答案**：暂停读取。超限分支（[src/DownloadCommand.cc:139-142](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L139-L142)）没有任何丢包或延迟注入逻辑，只是取消 EventPoll 事件注册并自排队；反压交给内核缓冲与 TCP 拥塞控制完成。

**练习 2**：为什么 `receiveMessages` 在循环内每条消息都复查限速，而不是循环外查一次？
**答案**：BT 的 piece 消息一次可能大量堆积在接收缓冲里，若只在循环外判断，一轮就可能读入几十 MB 绕过限速；循环内复查保证每读一条消息都有一次刹车机会（[src/DefaultBtInteractive.cc:305-309](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L305-L309)）。

**练习 3**：`--max-upload-limit` 对 HTTP 下载为什么基本无感，对 BT 做种却有效？
**答案**：上传限速的执行点只有 BT 的消息发送队列（[src/DefaultBtMessageDispatcher.cc:92-98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L92-L98)）与 PeerInteractionCommand 的写事件注册（[src/PeerInteractionCommand.cc:363-373](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L363-L373)）；HTTP/FTP 客户端几乎没有上行流量，只有 BT 做种才持续向外发 piece。

**练习 4**：`--optimize-concurrent-downloads` 与 `--max-overall-download-limit` 都以全局速度为输入，作用有何不同？
**答案**：前者是**调参**——按速度对数地调整并行任务数上限（[src/RequestGroupMan.cc:1093-1105](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L1093-L1105)），并把限值当作速度估计的封顶；后者是**硬限**——超限就暂停一切下载命令的读取。两者可同时生效。

## 5. 综合实践

**任务：给「限速锯齿」拍照留证，并输出一份可归档的机制笔记。**

1. **基线**：不限速下载一个大文件 60 秒，每 5 秒记录 `DL:`（`--summary-interval=5`），得到基线速度表。
2. **限速**：用 `--max-download-limit=500K`（或按你带宽取 1/4）重复，得到限速速度表；把两张表并排画在同一张时间轴上（手绘或电子表格均可）。
3. **定位**：在限速运行的日志（`-l=aria2.log --log-level=info`）中找到连接创建与停止的时间线；对照本讲行号，标注出「哪一秒命令被摘除了读事件、哪一秒被每秒唤醒重新检查」。若日志粒度不足，可在 [src/DownloadCommand.cc:135-143](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L135-L143) 的超限分支临时加一条 `A2_LOG_INFO`（验证后还原）。
4. **机制笔记**：用 200~400 字回答——「从 `update(bufSize)` 到 `disableReadCheckSocket()`，一个字节走完了哪些对象？为什么最终的锯齿周期约为 1 秒？」（提示：唤醒周期 = `DEFAULT_REFRESH_INTERVAL` = 1 秒，[src/DownloadEngine.cc:93-95](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L93-L95)。）
5. **可选加做**：`--max-overall-download-limit` + `-j2`、`--optimize-concurrent-downloads=true` 各跑一轮，把三张曲线放进同一份笔记，说明三级机制（任务限、全局限、并发调优）叠加时的行为差异。

## 6. 本讲小结

- `SpeedCalc` 用「按秒入槽、10 秒滑窗」的 deque 计算**瞬时速度**（窗口字节 × 1000 ÷ 窗口实际毫秒跨度），用累计字节 ÷ 总时长计算**平均速度**；前者供限速与显示，后者供结果报告。
- 字节回流是三级扇形汇聚：`DownloadCommand` 同时喂 `PeerStat`（每连接，供慢速踢除与 ServerStat）与 `DownloadContext::NetStat`（每任务，供 `--max-download-limit`），后者再把同一份数字累加进 `RequestGroupMan::NetStat`（全局，供 `--max-overall-download-limit` 与紧凑进度行）。
- `DownloadEngine::calculateStatistics()` 每轮调用 `StatCalc::calculateStat`，但输出节流由 `ConsoleStatCalc` 自己用 1 秒闸门完成；单任务详细行/多任务紧凑行/周期性多行摘要/非 TTY 追加行各有独立分支，输出介质被 `OutputFile` + `global::cout()` 抽象隔离。
- 限速 = 两级判定（任务级 + 全局级）× 三处执行点（DownloadCommand、PeerInteractionCommand/DefaultBtInteractive、DefaultBtMessageDispatcher），手段是**摘除 EventPoll 读/写事件注册**，命令退化为 INACTIVE、靠 DownloadEngine 每秒一次的全量唤醒复查；数据不丢，滞留在内核缓冲由 TCP 反压。
- 大纲中提到的 `shouldIncreaseConcurrency` 在当前源码不存在；真实的「按速度调并发」是 `RequestGroupMan::optimizeConcurrentDownloads()` 的对数调参公式，并把 `--max-overall-download-limit` 作为速度估计的封顶。

## 7. 下一步学习建议

- **u4-l1（HTTP 下载命令链）**：本讲的 `DownloadCommand` 只是四段接力的最后一段，下一讲完整看 `HttpInitiateConnectionCommand → HttpRequestCommand → HttpResponseCommand → HttpDownloadCommand` 如何交接。
- **u4-l6（流式过滤）**：`DownloadCommand::executeInternal` 中被本讲略过的 `streamFilter_->transform` 是数据落盘前的关键一环，chunked/gzip 过滤器在那里挂接。
- **u5-l6（写盘缓存）**：`SinkStreamFilter` 与 `WrDiskCache` 如何把限速暂停期间堆积的 piece 数据批量落盘。
- 若想继续深挖统计侧，可读 `src/PeerStat.cc` 与 `src/ServerStatMan.cc`，看每连接测速如何沉淀为跨会话的镜像质量数据（衔接 u3-l1 的 URISelector 三种策略）。
