# 多设备时间同步与时钟/PPS

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 USRP 里「频率同步」和「时间同步」是两件事，分别依赖 10 MHz 参考时钟与 1 PPS 秒脉冲。
- 区分 `set_time_now`、`set_time_next_pps`、`set_time_unknown_pps` 三个设时 API 的差别，并能判断该用哪一个。
- 读懂 `set_time_unknown_pps` 的「抓沿 + 同步设时 + 10 ms 偏差校验」两步算法。
- 知道 `set_clock_source` / `set_time_source` 如何选择参考源，以及它们是属性树（property_tree）的翻译层。
- 会用 `get_mboard_sensor` 读取 `ref_locked`、`gps_locked`、`gps_time` 等 GPSDO 传感器。

本讲承接 u2-l6「接收流与元数据」。在那里你看到每个数据包都带 `time_spec` 时间戳、首包可以用 `has_time_spec` 定时发送。要让这些时间戳在**多块板子之间可比**，前提就是本讲要解决的多设备时间对齐问题。

## 2. 前置知识

### 什么是「同步」，为什么 SDR 需要它

一台 USRP 内部有一个叫 **timekeeper（计时器）** 的硬件计数器：它跟着采样时钟 `f_s` 不断自增，用一个 64 位 tick 计数表达「现在是什么时刻」。软件读到的 `get_time_now()` 就是这个计数器换算出来的时间。

问题来了：如果你有两块 USRP 做相干接收（比如 MIMO、测向、TDD 上下行切换），它们各自的 timekeeper 是**各自独立、自由奔跑**的。如果不做任何处理，两块板子的时间会：

- **频率不一致**：两块板子的采样时钟来自各自的本地晶振，温度和制造误差让它们的频率有 ppm 级偏差，时间会越走越偏。
- **起点不一致**：两块板子上电时计数器从 0 开始数，但谁先上电、谁后上电完全随机，所以「同一时刻」读到的绝对时间对不上。

要消除这两种偏差，需要从外部注入两个共享信号：

| 信号 | 频率 | 作用 | 解决什么问题 |
|------|------|------|-------------|
| 10 MHz 参考时钟 | 10 MHz 正弦 | 频率基准 | 让所有板子的采样时钟同频（**频率同步**） |
| 1 PPS 秒脉冲 | 1 Hz 脉冲，上升沿对齐到整秒 | 时间基准/历元标记 | 让所有板子的 timekeeper 在同一上升沿锁存同一时刻（**时间同步**） |

一句话：**10 MHz 让大家走得一样快，1 PPS 让大家的表对到同一个起点。**

### GPSDO 是什么

**GPSDO**（GPS Disciplined Oscillator，GPS 驯服晶振）= GPS 接收机 + 受控温补晶振（TCXO/OCXO）。它用 GPS 卫星的原子钟信号长期校正本地晶振，对外输出：

- 稳定的 **10 MHz** 参考；
- 与 UTC 整秒对齐的 **1 PPS**；
- 当前的 **UTC 绝对时间**（GPS 解算出来的）。

装了 GPSDO 的 USRP（如带 GPSDO 的 X3x0、E3x0、N2x0）可以同时拿到频率参考、时间参考、绝对时间三样东西，是「单板自同步」的最省心方案。多块板子也可以共用一台 GPSDO 或 OctoClock 来分发这两个信号。

### 软件视角的三个层次

本讲涉及三类 API，它们正好对应三个抽象层次：

1. **参考源选择**：`set_clock_source` / `set_time_source` —— 决定 10 MHz 和 1 PPS 从哪里来。
2. **设时**：`set_time_now` / `set_time_next_pps` / `set_time_unknown_pps` —— 把一个已知时间写进 timekeeper。
3. **传感器读取**：`get_mboard_sensor` —— 查询参考锁定状态、GPS 锁定状态、GPS 时间。

下面三个最小模块就围绕这三层展开。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [host/examples/sync_to_gps.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/sync_to_gps.cpp) | 把 USRP 同步到 GPSDO 的完整示例：选源 → 等 10 MHz 锁 → 读 GPS 时间设时 → 多板对齐校验 |
| [host/examples/test_pps_input.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/test_pps_input.cpp) | 用 `set_time_unknown_pps(0.0)` 探测 1 PPS 信号是否存在（不存在就抛异常） |
| [host/examples/test_clock_synch.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/test_clock_synch.cpp) | 用 OctoClock（`multi_usrp_clock`）分发参考、同步多块 USRP 的时间，并随机抽样比对 |
| [host/include/uhd/usrp/multi_usrp.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/multi_usrp.hpp) | 上述所有方法（设时、选源、读传感器）的公共声明与文档注释 |
| [host/lib/usrp/multi_usrp.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp) | `multi_usrp_impl` 的实现：设时三件套、选源、传感器都是属性树的翻译层 |

> 阅读建议：先看 `multi_usrp.hpp` 的文档注释建立「契约」认知，再对照 `multi_usrp.cpp` 看实现，最后用三个 example 验证理解。

## 4. 核心概念与源码讲解

### 4.1 时间同步：把同一时刻写进所有板子的计时器

#### 4.1.1 概念说明

「时间同步」的目标是：**让多块板子的 timekeeper 在某一个 PPS 上升沿，同时锁存同一个时间值。**

UHD 提供三个设时 API，按「是否跨板同步」排序：

| API | 何时生效 | 跨板是否同步 | 适用场景 |
|-----|---------|------------|---------|
| `set_time_now(time, mboard)` | 调用后立刻 | **否**（串行设置，逐板写） | 单板、或不在乎偏差的调试 |
| `set_time_next_pps(time, mboard)` | 下一个 PPS 上升沿 | 是（前提：共享 PPS 且留足提前量） | 已知 PPS 沿、能读到绝对时间（如 GPSDO） |
| `set_time_unknown_pps(time)` | 下一个 PPS 上升沿 | 是（自动抓沿，最稳妥） | 主机无法查询 PPS 沿、只想保证同步 |

为什么要专门搞一个 `set_time_unknown_pps`？因为 `set_time_next_pps` 有一个隐藏陷阱：它把命令「排」到下一个 PPS 沿执行。如果你在距离下一个 PPS 沿**太近**的时刻调用，由于命令从主机经网络/PCIe 传到设备有延迟，不同板子可能一个赶上了这个沿、另一个错过了只能等下一个沿——结果各板时间差了**整整 1 秒**。`set_time_unknown_pps` 的两步法就是为消除这个隐患而设计的。

#### 4.1.2 核心流程

`set_time_unknown_pps` 的算法分三步（实现见 4.1.3）：

```text
输入：希望锁存的目标时间 t

第 1 步：抓 PPS 沿（catch the edge）
    t0 = get_time_last_pps()          # 记录当前"上一个 PPS 锁存的时间"
    while get_time_last_pps() == t0:  # 反复读，直到它发生变化
        sleep(1ms)                     #   → 说明刚刚有一个 PPS 上升沿到过
    （超过 1.1 秒还没变 → 抛异常："可能没有 PPS 信号"）

第 2 步：同步设时（synchronously）
    set_time_next_pps(t, ALL_MBOARDS)  # 所有板子都排到"下一个"沿执行
    sleep(1s)                          # 等那一个沿真正过去

第 3 步：校验偏差
    for 每块板子 m >= 1:
        若 get_time_now(m) 与 get_time_now(0) 相差 > 10ms：
            打印告警（说明这一块没对齐）
```

为什么第 1 步要先「抓沿」？因为抓到沿的**瞬间**，到「下一个 PPS 沿」之间正好还剩将近 1 整秒的余量。此时立刻调用 `set_time_next_pps`，命令有充足时间在下一个沿之前送达所有板子，从而保证它们都在**同一个**沿锁存——杜绝「差 1 秒」的隐患。

为什么第 3 步阈值是 10 ms？这是控制包往返时间（RTT）的容差。主机是**串行**逐板查询 `get_time_now` 的，每查一块就经过一次网络往返，所以即使板子时间完全一致，读回来的值也会有几毫秒的查询时延差。10 ms 既大于 RTT（不至于误报），又小于 1 秒（能抓出真正的「差 1 秒」故障）。数学上即要求：

\[
\lvert t_m - t_0 \rvert \le \Delta_{\text{RTT}}, \quad \Delta_{\text{RTT}} = 10\text{ ms}
\]

#### 4.1.3 源码精读

**三个设时 API 的声明与文档**，位于公共头文件，重点看注释里强调的差别：

[host/include/uhd/usrp/multi_usrp.hpp:259-300](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/multi_usrp.hpp#L259-L300) —— `set_time_now` 与 `set_time_next_pps` 的声明。注意 `set_time_now` 注释里明确写「serially for multiple timekeepers, so times across multiple timekeepers will not be synchronized」（多计时器串行设置，不会同步）；`set_time_next_pps` 注释里警告「Make sure to not call this shortly before the next PPS edge」（不要在临近 PPS 沿时调用，否则各板可能差整 1 秒，并建议改用 `set_time_unknown_pps`）。

[host/include/uhd/usrp/multi_usrp.hpp:302-322](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/multi_usrp.hpp#L302-L322) —— `set_time_unknown_pps` 的声明。注释把它定义为「PPS 沿未知时的两步同步」，明确「Step1: wait for the last pps time to transition to catch the edge」「Step2: set the time at the next pps」。

**实现层**，`multi_usrp_impl` 里这三个方法都是对属性树节点的写入（呼应 u2-l4「multi_usrp 是属性树翻译层」）：

[host/lib/usrp/multi_usrp.cpp:470-490](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L470-L490) —— `set_time_now` / `set_time_next_pps` 的实现。它们都遵循「若指定单板就写该板节点（`mb_root(mboard)/"time/now"` 或 `/"time/pps"`），否则用 `for` 循环逐板写」的套路。**正是这个 `for` 循环导致 `set_time_now` 跨板不同步**——第 0 块和第 1 块的写入相隔一次属性树事务。

[host/lib/usrp/multi_usrp.cpp:492-525](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L492-L525) —— **本讲的核心代码：`set_time_unknown_pps` 的完整实现**。逐段对应上面 4.1.2 的三步：

- `494-505`：第 1 步抓沿。`end_time` 设为「现在 + 1100 ms」作为超时上限；反复 `sleep(1ms)` 轮询 `get_time_last_pps()`，一旦它和起始值 `time_start_last_pps` 不同就跳出；超时则抛 `runtime_error("Board 0 may not be getting a PPS signal!")`。
- `507-509`：第 2 步同步设时。`set_time_next_pps(time_spec, ALL_MBOARDS)` 排到下一个沿，随后 `sleep(1s)` 等沿过去。
- `511-525`：第 3 步校验。对每块板子 `m>=1` 比较 `get_time_now(m)` 与 `get_time_now(0)`，差超过 `time_spec_t(0.01)`（10 ms）就打印 WARNING。

**多板对齐校验的另一种写法**，来自示例程序，它用「整秒比对」替代 10 ms 容差：

[host/examples/sync_to_gps.cpp:146-177](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/sync_to_gps.cpp#L146-L177) —— 当所有板子都已各自同步到 GPS 后，这段先用 `while (time_last_pps == get_time_last_pps())` 等一个新 PPS 沿，再 `sleep(200ms)` 确保所有设备都看到了该沿，然后逐板比较 `get_time_last_pps(m)` 是否与板 0 **完全相等**（整数秒级匹配）。这里用 `==` 严格相等而非容差，因为 `get_time_last_pps` 返回的是整秒锁存值，对齐后应当完全一致。

#### 4.1.4 代码实践

**实践目标**：通过阅读 `set_time_unknown_pps` 的实现，亲手追踪它「抓沿」用的那个超时常量与校验用的容差常量。

**操作步骤（源码阅读型）**：

1. 打开 [host/lib/usrp/multi_usrp.cpp:492](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L492)，定位 `set_time_unknown_pps` 的函数体。
2. 找到第 1 步里 `end_time` 的计算式（`std::chrono::milliseconds(1100)`）。问自己：为什么是 1100 ms 而不是恰好 1000 ms？
3. 找到第 3 步校验里的 `time_spec_t(0.01)`，确认它就是 4.1.2 说的 10 ms RTT 容差。
4. 思考：如果只有 1 块板子，第 3 步的 `for (m = 1; ...)` 循环会发生什么？

**需要观察的现象 / 预期结果**：

- 1100 ms 略大于 1 秒，是为了在「最坏情况下要等一个完整 PPS 周期（≈1000 ms）」之外再留一点处理余量；若恰好卡 1000 ms，可能在沿刚发生、计数器尚未刷新前误判超时。
- 1 块板子时 `m` 从 1 开始的循环条件直接不成立，循环体不执行——单板不需要跨板校验，这是正确的退化行为。

> 待本地验证：若有真实多板硬件，可在调用 `set_time_unknown_pps` 后立刻读 `get_time_now(0)` 与 `get_time_now(1)`，观察二者差值是否稳定落在 10 ms 以内。

#### 4.1.5 小练习与答案

**练习 1**：为什么文档强烈建议「不要在临近 PPS 沿时调用 `set_time_next_pps`」？如果违反，最坏后果是什么？

> **参考答案**：`set_time_next_pps` 把设时命令排到「下一个」PPS 沿执行。若调用时刻离该沿太近，命令到达不同板子的时间差可能跨越这个沿——有的板子赶上本沿、有的错过只能等下一个沿，导致各板时间**恰好差整 1 秒**。这种偏差很隐蔽（差的是整数秒，时间戳看起来都合理），所以文档推荐用 `set_time_unknown_pps` 自动规避。

**练习 2**：`set_time_now` 和 `set_time_next_pps` 的实现里都有 `if (mboard != ALL_MBOARDS) { ...写单板...; return; }` 后面跟一个 `for` 循环。这两段是「二选一」还是「都会执行」？为什么这样组织？

> **参考答案**：是二选一。指定单板时走 `if` 分支写完即 `return`；只有传 `ALL_MBOARDS` 时才会落到 `for` 循环逐板调用自身（递归到单板分支）。这样把「单板写入」与「广播到所有板」的两种语义统一在同一个函数里，避免重复代码。

**练习 3**：`set_time_unknown_pps` 第 3 步校验只比较「板 m 与板 0」，而不同时比较「板 m 与板 m-1」。这样能发现「板 1 偏了、但板 2 没偏」吗？

> **参考答案**：能。因为所有板子最终都对齐到板 0 这一个基准；任何一块板只要和板 0 偏差超过 10 ms 都会被它自己那一轮循环抓出来。统一以板 0 为参照系，比相邻两两比较更直接、也足以定位到出问题的具体板子。

### 4.2 PPS 与参考时钟：选对频率源和时间源

#### 4.2.1 概念说明

4.1 讲的是「怎么把时间写进去」，本模块讲「写进去之前，必须先把参考源选对」。

参考源有两类，由两个对称的 API 控制：

- `set_clock_source(source, mboard)`：选 **10 MHz** 频率参考的来源。典型取值：`"internal"`（板载 TCXO）、`"external"`（前面板 SMA 输入的外部 10 MHz）、`"gpsdo"`（板载 GPSDO）、`"mimo"`（从 MIMO 扩展口级联的上一块板子）。
- `set_time_source(source, mboard)`：选 **1 PPS** 时间参考的来源。取值集合与上面类似。

多板同步的硬性前提：**所有板子必须使用同一个物理来源的 10 MHz 和同一个物理来源的 1 PPS**（典型做法是全部设为 `"external"` 并共用一台 GPSDO/OctoClock 分发，或全部设为 `"gpsdo"` 各自带 GPSDO、或第一块 `"internal"`/`"external"`、后续 `"mimo"` 级联）。

一个常被忽略的副作用：**切换 clock source 会复位 timekeeper**。因此正确顺序永远是「先选源、再设时」，且选源期间不能有流式传输。

#### 4.2.2 核心流程

一次标准的多板参考配置流程：

```text
for 每块板子 m:
    set_clock_source(参考源, m)   # 10 MHz 来自哪里
    set_time_source(参考源, m)   # 1 PPS 来自哪里
# —— 至此所有板子同频、共享 PPS ——
set_time_unknown_pps(目标时间)   # 再统一设时（4.1）
```

软件如何「感知」一个 PPS 沿发生了？答案是 4.1 出现过的 `get_time_last_pps()`：它返回「最近一次 PPS 上升沿锁存的 timekeeper 值」。**这个值只在 PPS 沿到来时才跳变**，所以轮询它是否变化，就是软件层「抓 PPS 沿」的标准手法——`set_time_unknown_pps` 第 1 步、`sync_to_gps` 的对齐校验都用了这一招。

#### 4.2.3 源码精读

**两个选源 API 的声明与文档**，注释里反复强调一个关键注意点：

[host/include/uhd/usrp/multi_usrp.hpp:409-410](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/multi_usrp.hpp#L409-L410) —— `set_time_source` 声明。

[host/include/uhd/usrp/multi_usrp.hpp:460-477](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/multi_usrp.hpp#L460-L477) —— `set_clock_source` 声明，紧跟着一段 `\b Note`：重配时钟源会影响 FPGA 时钟、影响计时、影响依赖时钟的块，因此「strongly recommended to configure clock and time source before doing anything else」（务必在任何操作之前配置），且「setting the device time should be done after calling this」「there should be no ongoing streaming operation while reconfiguring」（设时要在选源之后、选源期间不能有流）。这正是 4.2.1「先选源再设时」的官方依据。

**实现层**，选源同样是属性树翻译（印证 u2-l4），并兼容新老两种属性树结构：

[host/lib/usrp/multi_usrp.cpp:631-650](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L631-L650) —— `set_clock_source` 实现。它优先写新式节点 `mb_root(mboard)/"clock_source/value"`；若该节点不存在（RFNoC 等较新设备改用统一的 `sync_source`），就改写 `sync_source/value` 这个 `device_addr_t` 节点里的 `clock_source` 键；两者都没有就抛异常。`set_time_source`（[577-596](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L577-L596)）结构完全对称。

**示例：选源 + 设时的最小组合**，`test_pps_input` 用三行就把参考和设时配好：

[host/examples/test_pps_input.cpp:66-74](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/test_pps_input.cpp#L66-L74) —— 先按命令行可选地 `set_time_source(time_source)`（第 67 行），再调用 `set_time_unknown_pps(time_spec_t(0.0))`（第 74 行）。注意这里故意把目标时间设成 `0.0`：本例的**目的不是设对绝对时间，而是验证 PPS 信号是否存在**——若没有 PPS，`set_time_unknown_pps` 第 1 步抓沿会在 1.1 秒后抛异常（见 4.1.3），程序从而得知「PPS 没接好」。这是把设时 API 反过来当「PPS 探测器」用的巧妙技巧。

#### 4.2.4 代码实践

**实践目标**：用 `test_pps_input` 的思路，写一段「先选源、再用设时 API 探测 PPS」的最小调用序列。

**操作步骤（源码阅读 + 伪代码）**：

1. 阅读 [host/examples/test_pps_input.cpp:59-74](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/test_pps_input.cpp#L59-L74)，确认顺序是「构造设备 → `sleep(1s)` 让设备稳定 → 可选 `set_time_source` → `set_time_unknown_pps(0.0)`」。
2. 写出对应的多板伪代码（示例代码，非项目原代码）：

```cpp
// 示例代码：多板先选外部参考，再统一设时
auto usrp = uhd::usrp::multi_usrp::make(args);
for (size_t m = 0; m < usrp->get_num_mboards(); ++m) {
    usrp->set_clock_source("external", m);  // 10 MHz 走前面板 SMA
    usrp->set_time_source("external", m);   // 1 PPS 走前面板 SMA
}
usrp->set_time_unknown_pps(uhd::time_spec_t(0.0));  // 设为 0 并顺便验证 PPS 存在
```

3. 思考：如果把上面两行 `set_*_source` 放到 `set_time_unknown_pps` **之后**，会发生什么？

**需要观察的现象 / 预期结果**：

- 放到后面会出问题：切换 clock source 会复位 timekeeper（4.2.1、4.2.3 的官方注释都这么说），于是刚设好的时间被清掉，后续 PPS 沿对到的时间就是错的。
- 正确顺序永远是 **选源 → 设时**。

> 待本地验证：若有硬件，运行 `./test_pps_input --args <dev>` 分别在「接了 PPS」和「没接 PPS」两种情况下观察：前者打印 `Success!`，后者应在约 1.1 秒后抛 `runtime_error`。

#### 4.2.5 小练习与答案

**练习 1**：`set_clock_source` 的实现里有一个 `if/else if/else` 三分支，分别处理哪三种情况？

> **参考答案**：① 新式属性树有独立 `clock_source/value` 节点 → 直接写字符串；② 否则若有统一的 `sync_source/value`（`device_addr_t`）节点 → 改写其中的 `clock_source` 键；③ 两者都没有 → 抛 `runtime_error("Can't set clock source on this device.")`。这体现了 UHD 兼容「老式分离属性」与「新式 RFNoC 统一 sync_source」两套设备。

**练习 2**：为什么 `get_time_last_pps()` 可以当成「PPS 沿探测器」来用？

> **参考答案**：因为它返回的是「最近一次 PPS 上升沿锁存的 timekeeper 整秒值」。没有 PPS 时这个值永不变化；一旦有 PPS 沿，它就会跳变到新的整秒。所以「轮询它是否变化」等价于「等待一个 PPS 沿」。`set_time_unknown_pps` 抓沿、`sync_to_gps` 对齐校验都依赖这一点。

### 4.3 GPSDO 传感器：读取锁定状态与绝对时间

#### 4.3.1 概念说明

选好源、设好时之后，还需要**确认这些操作真的成功了**。USRP 用「传感器（sensor）」机制把硬件状态暴露给软件。GPSDO 相关的传感器是一组主板级传感器，通过两个 API 访问：

- `get_mboard_sensor_names(mboard)`：返回这块主板支持的传感器名字列表。
- `get_mboard_sensor(name, mboard)`：返回一个 `sensor_value_t` 对象，它既能 `.to_bool()`（转布尔，用于状态判断），也能 `.to_int()`（转整数，用于读 GPS 时间），还能取 `.value`（原始字符串）。

常见的 GPSDO 相关传感器：

| 传感器名 | 类型 | 含义 |
|---------|------|------|
| `ref_locked` | bool | 是否已锁定到 10 MHz 参考（无论内部/外部/GPSDO）。**频率同步成功的判据** |
| `gps_locked` | bool | GPS 是否已定位解算（拿到有效 UTC）。GPSDO 驯服有效的前提 |
| `gps_time` | int | GPS 报告的当前整秒时间（Unix/UTC 历元）|

此外，Ettus **OctoClock** 是一台独立的参考分发设备（最多给 8 台 USRP 分发 10 MHz/PPS），它在 UHD 里是另一个设备类 `multi_usrp_clock`，有自己的传感器（`gps_detected`、`using_ref`）和自己的 `get_time()`。

#### 4.3.2 核心流程

「同步到 GPSDO」的完整闭环（来自 `sync_to_gps`）：

```text
for 每块板子 m:
    1) 选源：set_clock_source("gpsdo", m); set_time_source("gpsdo", m)
    2) 等 10 MHz 锁：轮询 get_mboard_sensor("ref_locked", m).to_bool() 直到 true（最多 30 秒）
    3) 查 GPS 锁：get_mboard_sensor("gps_locked", m).to_bool()
    4) 读 GPS 绝对时间并设时：
       t = get_mboard_sensor("gps_time", m).to_int()
       set_time_next_pps(t + 1.0, m)        # 注意 +1，见 4.3.4
# 5) 多板对齐校验（4.1.3 已讲）
```

这里第 4 步用了 `set_time_next_pps` 而不是 `set_time_unknown_pps`，是因为此时主机**已经能读到 GPS 的绝对时间**（PPS 沿语义已知），可以直接精确设时；`+1.0` 的来历见下面练习。

#### 4.3.3 源码精读

**传感器 API 的声明**：

[host/include/uhd/usrp/multi_usrp.hpp:599-607](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/multi_usrp.hpp#L599-L607) —— `get_mboard_sensor` 与 `get_mboard_sensor_names`，返回值是 `sensor_value_t` / 传感器名字列表。

**示例 1：sync_to_gps 的 GPSDO 闭环**——这是本模块最完整的一段真实代码，分四小段读：

[host/examples/sync_to_gps.cpp:71-72](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/sync_to_gps.cpp#L71-L72) —— 第 1 步选源：把这块板的 10 MHz 和 1 PPS 都切到板载 GPSDO（`"gpsdo"`）。

[host/examples/sync_to_gps.cpp:79-98](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/sync_to_gps.cpp#L79-L98) —— 第 2 步等 10 MHz 锁。先用 `get_mboard_sensor_names` 判断该板**有没有** `ref_locked` 这个传感器（不同型号传感器集合不同，必须先探测），再最多轮询 30 次、每次 `sleep(1s)`、读 `get_mboard_sensor("ref_locked", mboard).to_bool()`；超时未锁则 `exit(EXIT_FAILURE)`。

[host/examples/sync_to_gps.cpp:105-105](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/sync_to_gps.cpp#L105-L105) —— 第 3 步查 GPS 锁：`get_mboard_sensor("gps_locked", mboard).to_bool()`，没锁只打 WARNING 不退出（时间不准但流程继续）。

[host/examples/sync_to_gps.cpp:116-118](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/sync_to_gps.cpp#L116-L118) —— 第 4 步读 GPS 时间并设时。`gps_time` 取整秒，构造成 `time_spec_t`，调用 `set_time_next_pps(gps_time + 1.0, mboard)`。随后 [124 行](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/sync_to_gps.cpp#L124) `sleep(2s)` 等设时生效（注释说明 N 系列在最后一个 PPS 处有已知刷新问题，所以等 2 秒而非 1 秒）。

**示例 2：OctoClock（multi_usrp_clock）的传感器**——独立参考分发设备走的是另一套 API：

[host/examples/test_clock_synch.cpp:68-76](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/test_clock_synch.cpp#L68-L76) —— `multi_usrp_clock::make(clock_args)` 创建 OctoClock 设备，然后查两个传感器：`get_sensor("gps_detected").value != "false"` 确认检测到 GPSDO，`get_sensor("using_ref").value == "internal"` 确认用的是内部参考。注意这里用 `.value`（原始字符串 `"false"`/`"internal"`）而非 `.to_bool()`。

[host/examples/test_clock_synch.cpp:105-106](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/test_clock_synch.cpp#L105-L106) —— `clock_time = clock->get_time()` 从 OctoClock 读整秒时间，再 `usrp->set_time_next_pps(time_spec_t(clock_time + 1))` 同步到所有 USRP。注意这里没有逐板传 mboard，而是用默认 `ALL_MBOARDS`，让所有板子在同一个 PPS 沿统一设时。

#### 4.3.4 代码实践

**实践目标**：对照 `sync_to_gps.cpp`，亲手把「读 GPS 时间设时」的 `+1.0` 来历解释清楚。

**操作步骤（源码阅读型）**：

1. 打开 [host/examples/sync_to_gps.cpp:116-118](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/sync_to_gps.cpp#L116-L118)，看到 `set_time_next_pps(gps_time + 1.0, mboard)`。
2. 回到 4.1.3 引用的 `set_time_next_pps` 文档（[multi_usrp.hpp:280-282](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/multi_usrp.hpp#L280-L282)），原文写「the time spec supplied should correspond to the next pulse (i.e. current time + 1 second)」。
3. 推理：`gps_time` 是**现在这一秒**读到的 GPS 时间；`set_time_next_pps` 设的是**下一个 PPS 沿**要锁存的值，那个沿对应的是下一秒，所以要 `+1.0`。

**需要观察的现象 / 预期结果**：

- 如果漏掉 `+1.0`，USRP 的时间会比真实 UTC **慢整 1 秒**，且因为是整数秒偏差，看起来「合理」却很难发现。
- 数学上，设时点 `t_now` 与锁存点 `t_next_pps` 满足 \( t_{\text{next\_pps}} = t_{\text{now}} + 1 \)，所以应写入的目标值正是 \( \text{gps\_time} + 1 \)。

> 待本地验证：若有带 GPSDO 的设备，运行 `./sync_to_gps --args <dev>`，观察输出中 `USRP time` 与 `GPSDO time` 两行是否相等，以及最后是否打印 `SUCCESS: USRP time synchronized to GPS time`。

#### 4.3.5 小练习与答案

**练习 1**：`sync_to_gps` 在轮询 `ref_locked` 之前，为什么要先用 `get_mboard_sensor_names` 判断该传感器是否存在？直接调用会怎样？

> **参考答案**：不同型号主板的传感器集合不同，某些板子没有 `ref_locked` 传感器。直接 `get_mboard_sensor("ref_locked", ...)` 在不支持的板上会抛异常（通常是 key_error）。所以示例先用 `std::find` 在传感器名字列表里探测，存在才轮询，不存在则打印 `ref_locked sensor not present on this board.` 优雅降级——这正是 u1-l5 讲过的「对能力差异优雅降级」写法。

**练习 2**：`sync_to_gps` 里 `gps_locked` 没锁只打 WARNING 不退出，但 `ref_locked` 没锁却 `exit(EXIT_FAILURE)`。为什么两者处置不同？

> **参考答案**：`ref_locked` 是「10 MHz 频率锁定」，没锁意味着采样时钟根本没稳定，后续一切收发时间基准都不可信，属于硬故障，必须退出。`gps_locked` 是「GPS 定位」，没锁只是说明 GPSDO 还没被卫星驯服、绝对时间暂时不准，但本地 10 MHz 和 1 PPS 仍可能已锁定并可用（只是时间不是真实 UTC），所以只警告、不中断流程。

**练习 3**：`test_clock_synch` 查 OctoClock 传感器用的是 `.value`（字符串），而 `sync_to_gps` 查 USRP 传感器用的是 `.to_bool()`。这两种用法各自适用什么场景？

> **参考答案**：`.to_bool()` 适用于明确的布尔状态传感器（如 `ref_locked`/`gps_locked`，取值 `true`/`false`），直接得到 `bool` 便于条件判断。`.value` 取原始字符串，适用于取值不是简单布尔的传感器——OctoClock 的 `using_ref` 取值是 `"internal"`/`"external"` 这样的枚举字符串，只能按字符串比较。两者是同一 `sensor_value_t` 对象的不同访问方式。

## 5. 综合实践

**任务**：对照 [host/examples/sync_to_gps.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/sync_to_gps.cpp)，写出一份「N 块带 GPSDO 的 USRP 完成时间对齐」的关键步骤序列，**每一步标注对应的 UHD API 调用**，并说明如果某一步失败会影响后续哪一步。

请按下表填空（左侧步骤顺序已给出，右侧 API 留给你补全，可对照源码核对）：

| 步骤 | 目的 | 对应 API 调用（你填） |
|------|------|---------------------|
| 1 | 构造设备 | `multi_usrp::make(args)` |
| 2 | 逐板选 GPSDO 为频率源 | ? |
| 3 | 逐板选 GPSDO 为时间源 | ? |
| 4 | 探测并等待 10 MHz 锁定 | ? |
| 5 | 查询 GPS 是否定位 | ? |
| 6 | 读 GPS 绝对时间 | ? |
| 7 | 在下一个 PPS 沿把时间写入每块板 | ? |
| 8 | 等待 2 秒让设时生效 | `std::this_thread::sleep_for(2s)` |
| 9 | 多板整秒级对齐校验 | ? |

**参考答案**（填好后与源码逐行对照）：

| 步骤 | API |
|------|-----|
| 2 | `usrp->set_clock_source("gpsdo", m)` |
| 3 | `usrp->set_time_source("gpsdo", m)` |
| 4 | `usrp->get_mboard_sensor_names(m)` + `usrp->get_mboard_sensor("ref_locked", m).to_bool()` |
| 5 | `usrp->get_mboard_sensor("gps_locked", m).to_bool()` |
| 6 | `usrp->get_mboard_sensor("gps_time", m).to_int()` |
| 7 | `usrp->set_time_next_pps(gps_time + 1.0, m)` |
| 9 | 轮询 `usrp->get_time_last_pps()` 抓沿 → 比较 `usrp->get_time_last_pps(m)` 是否各板相等 |

**故障传播分析**（把知识串起来）：

- 步骤 4 失败（ref_locked 一直 false）→ 10 MHz 没锁，采样时钟不稳 → 步骤 7 设的时间会被不稳定的时钟驱动而漂移，必须中止（`sync_to_gps` 正是 `exit(EXIT_FAILURE)`）。
- 步骤 5 失败（gps_locked false）→ 步骤 6 读到的 `gps_time` 不是真实 UTC → 时间能对齐但绝对值不准，可继续但带 WARNING。
- 步骤 7 漏写 `+1.0` → 全板一致地慢 1 秒，对齐校验（步骤 9）**测不出来**（各板仍相等），属于隐蔽错误。

## 6. 本讲小结

- USRP 的「同步」分两层：**10 MHz 参考时钟**做频率同步（`set_clock_source`），**1 PPS 秒脉冲**做时间同步（`set_time_source` + 设时 API）；多板必须共享同一物理来源的两个信号。
- 设时三件套按同步性递增：`set_time_now`（立刻、跨板不同步）→ `set_time_next_pps`（下个 PPS 沿、需留提前量）→ `set_time_unknown_pps`（自动抓沿、最稳妥）。
- `set_time_unknown_pps` 的两步法是本讲核心：**第 1 步用 `get_time_last_pps` 抓 PPS 沿（1.1 秒超时）→ 第 2 步 `set_time_next_pps` 同步设时 → 第 3 步用 10 ms RTT 容差校验各板偏差**。
- `set_clock_source` / `set_time_source` 都是属性树翻译层（兼容 `clock_source/value` 与统一 `sync_source/value` 两种结构），且**切换 clock source 会复位 timekeeper**，所以顺序永远是「先选源、再设时、选源期间不流式」。
- GPSDO 状态通过主板传感器暴露：`ref_locked`（10 MHz 锁，硬判据）、`gps_locked`（GPS 定位）、`gps_time`（绝对 UTC 整秒）；用 `get_mboard_sensor` 读，`.to_bool()` / `.to_int()` / `.value` 三种取值方式各有所长。
- `set_time_next_pps` 的目标值要写成「下一秒」，所以从 GPS 读时间设时时要 `gps_time + 1.0`；OctoClock（`multi_usrp_clock`）是独立的参考分发设备，有自己的 `get_time()` 与传感器。

## 7. 下一步学习建议

- **进入 RFNoC 架构**：本讲一直停在 `multi_usrp` 高层 API。RFNoC 设备（X410 等）的时间/参考管理改由 `mb_controller` 统一接管，下一步请学 u3-l1「RFNoC 架构与 rfnoc_graph 会话」和 u3-l4「mb_controller 主板控制器」，对比 `mb_controller::get_timekeeper()` 与本讲的 `get_time_now()`。
- **深入底层设时实现**：想看「设时命令如何真正下发到 FPGA timekeeper」，可继续阅读 u4-l4「MPMD 设备实现」，理解属性树节点 `time/pps`、`time/now` 背后的 control packet 通路。
- **动手验证**：若有带 GPSDO 的硬件，依次运行 `test_pps_input`、`sync_to_gps`、`test_clock_synch` 三个示例，对照本讲的步骤序列观察输出，把「待本地验证」的部分补齐。
- **延伸阅读**：`multi_usrp.hpp` 中 `set_sync_source` / `get_time_synchronized` 等尚未展开的便利 API，可作为本讲之外的自行扩展练习。
