# VNA 模式：扫描设置与数据入口

## 1. 本讲目标

LibreVNA 的 GUI 有三种测量模式（VNA、频谱仪、信号源），其中 VNA 模式是功能最完整、链路最长的一个。学完本讲，你应该能够：

1. 说出 `VNA` 类的 `Settings` 结构里每个字段对应界面上的哪个控件，以及这些设置如何被设备能力（Limits）约束。
2. 解释「UI 控件 → `Set*` 槽函数 → `SettingsChanged` → 防抖定时器 → `ConfigureDevice` → `DeviceDriver::setVNA`」这条配置下发链的每一环。
3. 跟踪一个测量数据点从驱动信号 `VNAmeasurementReceived` 进入 `NewDatapoint`，经过平均、校准、去嵌入，最终写入 `TraceModel` 的完整路径。
4. 理解零扫宽（zero span）、分段扫描（segments）、激励端口（excitedPorts）这几个特殊机制的作用。

本讲只看 GUI 侧的 VNA 模式。协议包如何在 USB 上传输是 u4-l3 的内容，FPGA 如何用采样点数实现 IF 带宽是 u6-l3/u6-l4 的内容，这里只引用结论。

## 2. 前置知识

阅读本讲前，你需要理解以下概念（前几讲已建立，这里简要回顾）：

- **Mode 基类**（u2-l2）：三种测量模式共同继承 `Mode`，同一时刻只有一个模式处于激活态（`isActive`）。模式切换时旧模式 `deactivate()`、新模式 `activate()` 并调用 `initializeDevice()`。
- **DeviceDriver 抽象**（u3-l1）：模式层不直接碰 USB，只持有 `DeviceDriver` 指针。配置用「请求＋回调」（如 `setVNA(settings, cb)`），数据用 Qt 信号（如 `VNAmeasurementReceived`）推送。设备的最大频率、点数、IF 带宽等限制由 `DeviceDriver::getInfo().Limits.VNA` 提供。
- **S 参数与 VNAMeasurement**（u3-l1）：驱动上报的数据是硬件无关的 `DeviceDriver::VNAMeasurement`——一个以 `"S11"`、`"S21"` 等字符串为键的复数 map，数值是线性复数（不是 dB）。
- **IF 带宽的物理意义**（u6-l3/u6-l4 的结论）：LibreVNA 在 FPGA 内对 250 kHz 中频做单 bin 数字解调，IF 带宽由每个测量点累积的样本数决定：\( \text{IFBW} \approx f_s / N \)。带宽减半意味着每点采样样本数翻倍，测量更慢但噪声更低。

不需要硬件也能学完本讲——所有实践都是源码阅读型的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Software/PC_Application/LibreVNA-GUI/VNA/vna.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.h) | `VNA` 类声明：`Settings` 内嵌类、全部 `Set*` 槽、`ConfigureDevice` 等私有函数 |
| [Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp) | 本讲主体：构造函数装配工具栏、`NewDatapoint` 数据管线、`ConfigureDevice` 配置下发 |
| [Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h) | `DeviceDriver::VNASettings`（配置下行）与 `VNAMeasurement`（数据上行）的定义 |
| [Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp) | 数据管线终点 `addVNAData`，以及激励端口反馈 `PortExcitationRequired` |
| [Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp) | `ConfigureSweep` 调用链的下一站：`setVNA` 把抽象设置翻译成 `Protocol::SweepSettings` 包（衔接 u4-l3） |

## 4. 核心概念与源码讲解

### 4.1 VNA 类与扫描设置

#### 4.1.1 概念说明

`VNA` 类（继承 `Mode`）是 VNA 模式的「大脑」，它要解决三个问题：

1. **用户想测什么？**——把工具栏上零散的控件（起始频率、点数、IF 带宽……）收敛成一个单一的 `Settings` 结构体，作为唯一事实来源。
2. **设备能测什么？**——用 `DeviceDriver::getInfo().Limits.VNA` 里的上下限夹住（clamp）每个设置项，用户输入超范围时静默修正，而不是弹窗报错。
3. **设置何时生效？**——任何一项设置变化都不能立刻打断正在进行的扫描，需要一个统一的「设置已变化，稍后重新配置」通知机制。

这三个问题的答案分别对应 `settings` 成员、`ConstrainAndUpdateFrequencies`/各 `Set*` 槽里的夹取逻辑、以及 `SettingsChanged` + 防抖定时器。

#### 4.1.2 核心流程

一个设置项的完整生命周期：

```text
用户改控件（SIUnitEdit / QSpinBox / QCheckBox）
   │  Qt 信号 valueChanged / toggled
   ▼
VNA::SetXxx(value)                    ← 唯一入口，构造函数/SCPI/加载 setup 也走这里
   ├─ 用 Limits.VNA 夹取 value
   ├─ 写入 settings 对应字段
   ├─ emit xxxChanged(value)          ← 回写 UI（setValueQuiet，避免信号回环）
   └─ SettingsChanged()
        ├─ 若 !running：直接返回（没在扫描，无需通知设备）
        ├─ configurationTimer.start(delay)   ← 防抖：100ms 内的连续修改合并为一次
        ├─ changingSettings = true           ← 期间到达的旧数据点将被丢弃
        └─ ResetLiveTraces()（若 resetTraces）
（100ms 后）
QTimer::timeout → ConfigureDevice()   ← 见 4.3
```

`Settings` 结构里的字段与界面控件、驱动字段的对应关系：

| Settings 字段 | 界面控件 | 含义 / 备注 |
| --- | --- | --- |
| `sweepType` | Sweep 工具栏下拉框 | `Frequency`（频率扫描）或 `Power`（功率扫描） |
| `Freq.start / stop` | Start/Center/Stop/Span 编辑框 | 频率扫描的起止频率（Hz） |
| `Freq.excitation_power` | Acquisition 工具栏 Level | 激励电平（dBm），频率扫描时全程恒定 |
| `Freq.logSweep` | Log 复选框 | 对数扫描（需设备 `VNALogSweep` 特性） |
| `Power.start / stop / frequency` | Power 扫描专用 From/To/at | 功率扫描的起止电平与固定频率 |
| `npoints` | Points 数字框 | 每段扫描的点数（受 `maxPoints` 限制） |
| `bandwidth` | IF BW 编辑框 | IF 带宽（Hz），受 `minIFBW`/`maxIFBW` 夹取 |
| `dwellTime` | Dwell time 编辑框 | 每点驻留时间（s），需设备 `VNADwellTime` 特性 |
| `excitedPorts` | （无直接控件，自动推导） | 本轮扫描需要激励哪些端口 |
| `segments / activeSegment` | （无控件，自动计算） | 点数超过硬件上限时把扫描拆成多段 |
| `zerospan` | 「0」按钮触发 | 起止相同 → 零扫宽，X 轴从频率变成时间 |
| `firstPointTime` | — | 零扫宽时第一个点的时间戳，用于把 `us` 归零 |

#### 4.1.3 源码精读

**① Settings 结构体——VNA 模式的唯一事实来源。** 所有扫描参数集中在这一个结构里，默认值为 1–6 GHz、501 点、1 kHz IF 带宽：

[VNA/vna.h:L55-L84](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.h#L55-L84)
这段代码定义了 `VNA::Settings` 内嵌类。注意两点：频率扫描和功率扫描各自有独立的子结构（`Freq`/`Power`），因为两种扫描类型关心的「自变量」不同（频率扫描扫频率、功率恒定，功率扫描反之）；`segments` 的注释明确说明当点数超过硬件支持时，扫描必须拆成多段执行。

**② UI 控件与 Set\* 槽的双向绑定。** 以 IF 带宽编辑框为例：

[VNA/vna.cpp:L465-L471](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L465-L471)
这段代码创建 Acquisition 工具栏上的「IF BW」`SIUnitEdit`（支持输入 `1k` 这类带单位后缀的文本），`connect` 的两个方向分别是：用户输入 → `SetIFBandwidth`（下行），以及 `IFBandwidthChanged` 信号 → `setValueQuiet` 回写显示（上行）。`Quiet` 后缀的意思是回写时不再触发 `valueChanged`，避免「控件→槽→信号→控件」的无限回环。本讲涉及的所有控件（Start/Stop/Span/Points/Level/Dwell……）都采用完全相同的两行 connect 模式。

**③ SetIFBandwidth——约束、写入、通知三步走。**

[VNA/vna.cpp:L1340-L1350](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1340-L1350)
这段代码是 IF 带宽的唯一合法入口：先把值夹进设备的 `[minIFBW, maxIFBW]` 区间，再写入 `settings.bandwidth`，发信号刷新 UI，最后调用 `SettingsChanged()` 请求重新配置设备。用户在界面输入超范围值时不会报错，只会被静默抬到边界。值得一提的是这两个边界并非硬编码，而是固件按采样率与样本数推导后经设备信息包上报的（[Hardware.hpp:L37-L42](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.hpp#L37-L42) 定义 800 kHz 采样率与 16–130944 样本上下限，[Hardware.hpp:L81-L82](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.hpp#L81-L82) 据此算出约 6 Hz–50 kHz），这正是 u6-l3 结论 \( \text{IFBW} \approx f_s/N \) 在设备能力协商上的体现。

**④ SettingsChanged——防抖与「设置变更中」门闩。**

[VNA/vna.cpp:L1098-L1113](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1098-L1113)
这段代码是所有设置变化的汇聚点。三个关键动作：(1) 若 `running == false` 直接返回——设备本来就在空闲，改设置不需要通信；(2) 启动单次 `configurationTimer`（默认 100ms），期间若再次调用 `SettingsChanged`，定时器重新计时，于是用户连续拖动频率旋钮产生的一串修改最终只触发一次设备重配置，这就是防抖；(3) 置 `changingSettings = true`，让随后到达的、属于旧设置的数据点在 `NewDatapoint` 入口被丢弃。

定时器本身的接线在构造函数里：

[VNA/vna.cpp:L82-L85](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L82-L85)
这段代码把 `configurationTimer` 配置为单次触发，超时后调用 `ConfigureDevice`。`configurationTimerResetTraces` 成员用来把「是否清空迹线」这个标志安全地传递过定时器边界。

**⑤ 零扫宽与频率约束。**

[VNA/vna.cpp:L1667-L1691](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1667-L1691)
这段代码（`ConstrainAndUpdateFrequencies`）统一夹取频率并推导 `zerospan` 标志：频率扫描时 start==stop、或功率扫描时起止功率相同，都算零扫宽。零扫宽时频率不再变化，数据点的 X 轴含义从频率切换为时间（见 4.2）。开头对 `logSweep` 强制 start ≥ 1 Hz，是因为对数轴无法表示 0。

**⑥ 分段扫描——突破单次扫描点数上限。**

[VNA/vna.cpp:L1317-L1338](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1317-L1338)
这段代码（`SetPoints`）处理点数上限：LibreVNA 硬件单次扫描最多 4501 点——这个数字来自固件常量 `FPGA::MaxPoints`（[FPGA.hpp:L9](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.hpp#L9)），本质是 FPGA 扫描配置存储器的容量限制（见 u6-l1），经设备信息包上报为 `Limits.VNA.maxPoints`。若用户在偏好设置里允许分段扫描（`allowSegmentedSweep`），点数上限放宽到 65535，超出硬件容量的部分由 `segments` 记录段数，之后在 `ConfigureDevice` 里逐段下发（见 4.3）、在 `NewDatapoint` 里把段偏移加回点号（见 4.2）。

#### 4.1.4 代码实践

**实践目标**：不运行程序，纯靠读代码整理出「设置项 → 入口槽 → 设备约束」对照表，验证你对配置入口唯一性的理解。

**操作步骤**：

1. 打开 [VNA/vna.cpp:L436-L497](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L436-L497)（Acquisition 工具栏），数一数有几个控件，每个控件 connect 到哪个 `Set*` 槽。
2. 对每个槽（`SetSourceLevel` L1274、`SetPoints` L1317、`SetIFBandwidth` L1340、`SetAveraging` L1352、`SetDwellTime` L1286），找到它引用了 `Limits.VNA` 的哪个字段做夹取。
3. 用 `grep -n "SettingsChanged()" vna.cpp` 统计有多少处调用，确认所有设置变化最终都汇入这一个函数。

**需要观察的现象**：`SetAveraging` 与其他槽不同——它**不**调用 `SettingsChanged`。因为平均 purely 是 GUI 侧行为（`average.setAverages`），设备根本不知道平均的存在，无需重新配置。

**预期结果**：得到一张约 8 行的对照表；`grep` 应显示十余处 `SettingsChanged()` 调用，全部来自各 `Set*` 槽与 `ConstrainAndUpdateFrequencies`。若你发现某个槽漏掉了约束或 `SettingsChanged`，说明读漏了——回去重读该函数。

#### 4.1.5 小练习与答案

**练习 1**：用户在 IF BW 框里输入 `50k`，但设备上限是 30 kHz。界面上最终显示什么？`settings.bandwidth` 是多少？设备收到的 IFBW 是多少？

**答案**：`SetIFBandwidth` 把 50000 夹到 `maxIFBW = 30000`，随后 `emit IFBandwidthChanged(30000)` 让编辑框静默回显 `30k`；`settings.bandwidth = 30000`；设备收到的也是 30000 Hz。三处永远一致，因为都源自同一次夹取后的值。

**练习 2**：为什么 `SetSweepType`（L1137-L1145）里比较 `settings.sweepType != sw` 之后才执行动作，而 `SetIFBandwidth` 没有类似判断？

**答案**：`SetIFBandwidth` 无条件重写 `settings.bandwidth` 再 `SettingsChanged` 是幂等的——同样的带宽重配一次没有副作用（代价只是多一次 100ms 防抖后的重配置）。而 `SetSweepType` 切换时要触发工具栏显隐、校准菜单可用性等一串连带动作（`sweepTypeChanged` 信号），重复触发会造成不必要的 UI 抖动，所以先用不等判断挡住重复设置。

**练习 3**：零扫宽按钮（`bZero`）的处理链是什么？为什么说零扫宽下「X 轴变成了时间」？

**答案**：`SetZeroSpan`（L1221-L1226）取当前中心频率，令 start = stop = center；`ConstrainAndUpdateFrequencies` 随即推导出 `zerospan = true`。零扫宽时频率不再扫描，每个数据点携带的是相对于第一个点的时间戳（`VNAMeasurement::us`），`NewDatapoint` 据此把数据类型设为 `TraceMath::DataType::TimeZeroSpan`，绘图 X 轴即时间（单位秒）。这相当于把 VNA 当作固定频率的功率/反射监测仪使用。

### 4.2 数据接收路径：NewDatapoint

#### 4.2.1 概念说明

配置下行只是半条链路，另外半条是数据上行：驱动从 USB 收到协议包、拼装成 `DeviceDriver::VNAMeasurement`（u3-l2/u4-l3 讲过这段），然后通过信号抛出。`VNA::NewDatapoint` 是这个信号在 VNA 模式里的接收端，也是整个 GUI 中最繁忙的函数之一——501 点、多端口、连续扫描意味着它每秒可能被调用上百次。

它要按顺序完成五件事：**过滤**（旧设置的数据不要）、**平均**（多圈扫描平滑噪声）、**定型**（决定这个点的 X 轴是频率、功率还是时间）、**修正**（校准与去嵌入）、**分发**（写入 TraceModel 与流式服务器）。顺序不能乱：平均必须在修正之前（否则平均的是被分段修正过的数据），原始数据必须在任何修正之前流出（校准测量需要原始值）。

#### 4.2.2 核心流程

```text
DeviceDriver::VNAmeasurementReceived(m)          （Qt 信号，已切回 GUI 线程）
   ▼
VNA::NewDatapoint(m)
   ├─ ① 守卫：!isActive → 丢弃；changingSettings → 丢弃（旧设置残余点）
   ├─ ② 扫描耗时统计（pointNum==0 且 lastPoint>0 → 打印上一圈用时）
   ├─ ③ emit newRawDatapoint(m)                  ← 未经平均的原始点（校准测量对话框等用）
   ├─ ④ 单次扫描模式：平均已满 → Stop() 返回
   ├─ ⑤ 分段偏移：m.pointNum += pointsPerSegment × activeSegment
   ├─ ⑥ m_avg = average.process(m)                ← 多圈平均
   ├─ ⑦ 定型：zerospan→TimeZeroSpan / 频率扫描→Frequency / 功率扫描→Power
   ├─ ⑧ window->addStreamingData(Raw)             ← 流式服务器：原始档
   ├─ ⑨ cal.correctMeasurement(m) → addStreamingData(Calibrated)
   ├─ ⑩ traceModel.addVNAData(m, type, deembedded=false)   ← 进入迹线！
   ├─ ⑪ (可选) deembedding.Deembed(m) → addStreamingData(Deembedded)
   │        → traceModel.addVNAData(m, type, deembedded=true)
   ├─ ⑫ 丢点检测：pointNum != lastPoint+1 → qWarning
   └─ ⑬ 段结束 → activeSegment 回绕，SettingsChanged 触发下一段
```

#### 4.2.3 源码精读

**① 信号连接在哪里建立——`initializeDevice`。**

[VNA/vna.cpp:L781-L797](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L781-L797)
这段代码是模式激活时对设备的初始化：先检查设备是否支持 VNA 特性（不支持则弹窗并退出，这是 u3-l1 讲过的能力协商），再按特性开关对数扫描、零扫宽、功率扫描等 UI 入口，最后把驱动的 `VNAmeasurementReceived` 信号连到自己的 `NewDatapoint` 槽。`Qt::UniqueConnection` 保证模式反复激活（比如 VNA→频谱仪→VNA 来回切换）不会产生重复连接——否则一个数据点会触发多次 `NewDatapoint`。

**② 守卫与原始数据转发。**

[VNA/vna.cpp:L959-L985](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L959-L985)
这段代码是 `NewDatapoint` 的开头：两个守卫（非活动模式丢弃、`changingSettings` 期间丢弃——4.1 里那个门闩在这里生效，防止旧设置的最后几个点污染新设置的迹线）；扫耗时统计只在「新圈第一个点」打印一次；`emit newRawDatapoint(m)` 把**未平均**的点转发出去（校准测量流程依赖它拿第一手数据）；单次扫描模式下平均到位即自动 `Stop()`。

**③ 分段偏移、平均与数据定型。**

[VNA/vna.cpp:L987-L1034](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L987-L1034)
这段代码完成三件事：(1) 分段扫描时，设备上报的点号是**段内**点号，加上 `pointsPerSegment × activeSegment` 才还原成全扫描点号；(2) `average.process` 做多圈平均（u7-l4 详述）；(3) 按零扫宽/扫描类型决定 `TraceMath::DataType`——这决定了迹线 X 轴的含义。零扫宽分支还把 `us` 减去 `firstPointTime`，让第一点从 0 秒开始计时。

**④ 校准、去嵌入与写入 TraceModel。**

[VNA/vna.cpp:L1036-L1069](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1036-L1069)
这段代码是数据管线的「修正与分发」段，顺序极有讲究：先流出 Raw 档 → 校准修正 `cal.correctMeasurement` 就地改写 `m_avg` → 流出 Calibrated 档 → **写入 TraceModel**（此刻迹线拿到的是校准后数据）→ 若去嵌入激活，再做一次夹具修正、流出 Deembedded 档并以 `deembedded=true` 二次写入 TraceModel（同一批 Trace 同时维护校准/去嵌入两套数据，绘图层可切换显示）。`window->addStreamingData` 的三档分级是 u7-l4 与 u10-l3 的内容，这里只需知道它把数据按处理深度分档推给 TCP 流式客户端。

**⑤ 终点站：TraceModel::addVNAData。**

[Traces/tracemodel.cpp:L286-L323](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp#L286-L323)
这段代码遍历所有 Live 且未暂停的 Trace：按数据类型填 X 值（频率 / 功率 / 时间），用 Trace 自己声明的参数名（如 `"S11"`，即 `liveParameter()`）到 `d.measurements` 这个 map 里查 Y 值——map 里没有这个键（比如该端口本轮未被激励）就跳过该 Trace。这就是「一条 Trace = S 参数矩阵的一个元素」的对接点。u8-l1 会从 Trace 视角再讲一遍。

**⑥ 反向通道：哪些端口需要激励？**

[Traces/tracemodel.cpp:L229-L240](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp#L229-L240)
这段代码（`PortExcitationRequired`）回答「测量 S21 需要激励哪个端口」：解析参数名第 3 个字符（`S21` 的 `1`，即激励端口）。每当 Trace 增删或暂停状态变化，`TraceModel` 发出 `requiredExcitation` 信号：

[VNA/vna.cpp:L1363-L1376](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1363-L1376)
这段代码（`ExcitationRequired`）是信号接收端：比较「TraceModel 需要的激励端口集合」与 `settings.excitedPorts` 现状，不一致就 `SettingsChanged()` 重新配置设备。由此形成一个闭环——删掉 S11 迹线后，设备下次扫描就不再激励端口 1，扫描时间几乎减半（少一个 stage）。

#### 4.2.4 代码实践

**实践目标**：用日志插桩（不运行也行，纯走读）验证数据管线的执行顺序。

**操作步骤**：

1. 读 [VNA/vna.cpp:L1078-L1090](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1078-L1090)，理解丢点检测（`pointNum != lastPoint+1` 打 Warning）与段回绕逻辑。
2. 若你本地能编译 GUI：在 `NewDatapoint` 的 ③⑥⑨⑩ 四处各加一行 `qDebug() << "stage x, point" << m.pointNum;`（改动仅限本地实验，不要提交），启动后连接设备（或用 u11-l3 的 DemoDriver 思路造假数据），观察控制台输出顺序。
3. 无法编译则走读：假设 501 点、averages=10，推算「收到第 1 个点」时 ③⑥⑨⑩ 各自会发生什么（提示：`average.process` 前 9 圈返回的数据仍在收敛，`average.settled()` 为 false）。

**需要观察的现象**：日志按 ③→⑥→⑨→⑩ 严格顺序出现；每点一条，无交错。

**预期结果**：确认管线顺序为「原始转发 → 平均 → 校准 → 写 TraceModel」。实验后记得还原插桩。若走读，第 1 个点时 `average.process` 返回的是第一圈原始值（平均器未满），`cal.correctMeasurement` 正常修正，`addVNAData` 正常写入。**（插桩运行部分待本地验证）**

#### 4.2.5 小练习与答案

**练习 1**：为什么 `emit newRawDatapoint(m)` 在 `average.process` 之前？

**答案**：校准测量（`StartCalibrationMeasurements` 流程）需要的是每一圈的原始数据，校准件测量取的是最后一圈（`average.currentSweep() == averages` 时才 `cal.addMeasurements`），但校准对话框等外部消费者可能要观察原始波动；更重要的是平均会改变数值语义，把未经污染的原始点尽早转发出去，让下游各自决定怎么处理。

**练习 2**：如果设备一次扫描 501 点但 GUI 漏收了第 200 点，会发生什么？用户能感知吗？

**答案**：`NewDatapoint` 末尾的检测（L1078-L1081）发现 `pointNum(201) != lastPoint(199)+1`，打印 `qWarning`「missed points」；迹线上第 201 个采样点的 X 坐标仍按点号换算的频率正常落位，所以图上出现一个小空隙，GUI 无弹窗。这是「尽力上报」的设计：USB 丢包不致命，下一圈扫描会补上该点的新测量。

**练习 3**：分段扫描（segments=3）时，TraceModel 看到的点号是段内的还是全扫描的？

**答案**：全扫描的。`NewDatapoint` 在写入前已把段内点号加上 `pointsPerSegment × activeSegment` 偏移（L996），因此对 TraceModel 而言分段是透明的；只有段切换瞬间（L1083-L1090 的 `SettingsChanged(false, 0)`）设备会短暂重配置。

### 4.3 ConfigureSweep 调用链

#### 4.3.1 概念说明

`ConfigureDevice` 是 VNA 模式与设备之间唯一的「施工通道」：Run/Stop 按钮走它，防抖定时器超时走它，校准测量启动走它，段切换也走它。它做一次完整的翻译——把 GUI 语义的 `VNA::Settings` 翻译成硬件语义的 `DeviceDriver::VNASettings`，两个结构体长得很像但有微妙差异：

- GUI 的 `Settings` 按扫描类型分成 `Freq`/`Power` 两个子结构；驱动的 `VNASettings` 统一为 `freqStart/freqStop + dBmStart/dBmStop`——**用「起止都相同」表达恒定量**：频率扫描时 `dBmStart == dBmStop`，功率扫描时 `freqStart == freqStop`。零扫宽则是两者都相同。
- GUI 记录的是全扫描范围；驱动收到的是**当前段**的范围与点数。
- GUI 没有 stage 概念；驱动层的 `excitedPorts` 列表顺序即 stage 编号（u3-l2/u4-l3 讲过 `portStageMapping`）。

#### 4.3.2 核心流程

```text
触发源（任一）：
  Run()/Stop() ｜ configurationTimer 超时 ｜ 校准测量 ｜ 段切换
   ▼
VNA::ConfigureDevice(resetTraces, cb)
   ├─ configurationTimer.stop()                  ← 防抖结束，接管配置权
   ├─ running?
   │    ├─ 是：组装 DeviceDriver::VNASettings s
   │    │     ├─ s.IFBW = settings.bandwidth
   │    │     ├─ s.excitedPorts ←（三选一：全部端口偏好 ｜ 去嵌入测量所需 ｜ TraceModel 所需）
   │    │     ├─ 段裁剪：npoints/segment，start/stop 经 Util::Scale 映射到段范围
   │    │     ├─ 按扫描类型填 freq/dBm 起止（恒定量两侧填同值）
   │    │     └─ s.dwellTime
   │    ├─ device->setVNA(s, 回调)
   │    │     回调：ResetLiveTraces → cb → changingSettings=false
   │    │           → lastStart=now, lastPoint=-1（重新计时）
   │    └─ emit sweepStarted()
   │    └─ 否（Stop）：device->setIdle(回调) → emit sweepStopped()
   ▼
LibreVNADriver::setVNA（u4-l3 已讲）：
   s.IFBW → p.settings.if_bandwidth
   s.dBm×100 → cdbm_excitation_start/stop
   excitedPorts.size()-1 → stages；端口→portNStage
   组装 Protocol::PacketType::SweepSettings 包 → SendPacket → USB/TCP
```

#### 4.3.3 源码精读

**① 组装 VNASettings——三处端口来源与段裁剪。**

[VNA/vna.cpp:L1974-L2009](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1974-L2009)
这段代码是 `ConfigureDevice` 的上半场：若在运行，先按需清空迹线、重新置 `changingSettings = true`，然后填 `s.IFBW`，再按优先级确定激励端口列表——偏好设置 `alwaysExciteAllPorts`（全激励，测量最稳但最慢）＞ 去嵌入测量所需端口 ＞ TraceModel 实际需要的端口（最快）。随后 `traceModel.setSpan` 让绘图知道 X 轴范围，分段时把全扫描的点号区间线性映射（`Util::Scale`）成本段的频率/功率区间。

**② 按扫描类型填起止值——「恒定量两侧同值」约定。**

[VNA/vna.cpp:L2010-L2040](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L2010-L2040)
这段代码完成翻译的最后一步：频率扫描时频率取段范围、功率两侧同填 `excitation_power`；功率扫描时频率固定为 `Power.frequency`、功率扫段范围；对数扫描标志仅频率扫描有意义。此刻 `s` 已是硬件视角的完整扫描描述。

**③ 下发与回调——配置生效的边界。**

[VNA/vna.cpp:L2041-L2072](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L2041-L2072)
这段代码调用 `window->getDevice()->setVNA(s, ...)`（无设备或未激活则发 `sweepStopped` 并退出），回调里做四件事：再清一次迹线、执行外部回调（校准测量用它开启 `calMeasuring`）、**解除 `changingSettings` 门闩**（从这一刻起新数据点才被接纳）、重置扫描计时基准。停止分支则调 `setIdle` 让设备休眠。注意 `emit sweepStarted()` 在 `setVNA` 返回后立即发出——它表示「命令已发出」，而回调才表示「设备确认」，两者之间设备可能还在切换配置。

**④ 驱动侧的翻译——与 u4-l3 衔接。**

[Device/LibreVNA/librevnadriver.cpp:L480-L520](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L480-L520)
这段代码是 `LibreVNADriver::setVNA`：构造 `Protocol::PacketType::SweepSettings` 包，`s.IFBW` 直填 `if_bandwidth`，dBm×100 变厘dB（`cdbm`），`excitedPorts.size()-1` 变 stage 数，驻留时间换算成微秒并夹到 16 位上限，每个端口的 stage 号写入 `portNStage` 字段。零扫宽在此被显式判定（`zerospan = (freqStart==freqStop) && (dBmStart==dBmStop)`）。之后包经 u4-l3 讲过的发送队列走 USB/TCP，固件 `VNA::Setup`（u5-l4）接收后开始预编程 FPGA。

**⑤ Run/Stop 的极简实现。**

[VNA/vna.cpp:L1961-L1972](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1961-L1972)
这段代码显示 Run/Stop 只是「设置 running 标志 + 一次 `ConfigureDevice`」：running=true 走 setVNA 分支，running=false 走 setIdle 分支。所有复杂度都已被 `ConfigureDevice` 吸收。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：回答规格问题——**把 IF 带宽减半，沿哪条调用链影响设备端配置？** 写出途经的每个函数名。

**操作步骤**（无硬件，纯源码走读）：

1. 从 UI 开始：在 [VNA/vna.cpp:L465-L469](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L465-L469) 确认 IF BW 编辑框的 `valueChanged` 连到 `VNA::SetIFBandwidth`。
2. 逐函数走读，抄下每一站的文件:行号（下方「预期结果」给出参考答案）。
3. 走到 `LibreVNADriver::setVNA` 后停住，用 u4-l2/u5-l4/u6-l3 的知识口头补完最后三站（协议包 → 固件 → FPGA 样本数）。
4. 有硬件的读者加做：连接设备，把 IF BW 从 1 kHz 改到 500 Hz，截图 S21 迹线噪声层的变化；再观察状态栏扫描时间大约翻倍。

**需要观察的现象**（有硬件时）：迹线噪声层明显下移（理论上噪声功率正比于带宽，减半带宽约降 3 dB 噪底，\( P_n = kTB \)，\( B \) 减半则 \( P_n \) 减半）；同时每点测量时间约翻倍，扫描变慢。两个现象同源：FPGA 用更多样本解调同一点（\( \text{IFBW} \approx f_s/N \)，u6-l3）。

**预期结果**——完整调用链（参考答案）：

```text
1. SIUnitEdit "IF BW"                     vna.cpp:L465-471   用户输入 500（Hz）
2. SIUnitEdit::valueChanged               （Qt 信号）
3. VNA::SetIFBandwidth(500)               vna.cpp:L1340-1350 夹取→settings.bandwidth=500
4. VNA::SettingsChanged()                 vna.cpp:L1098-1113  configurationTimer.start(100ms)
                                                             changingSettings=true
5. QTimer::timeout → lambda               vna.cpp:L82-85     100ms 防抖后触发
6. VNA::ConfigureDevice()                 vna.cpp:L1974-2072 组装 VNASettings
     └─ s.IFBW = settings.bandwidth       vna.cpp:L1984
7. DeviceDriver::setVNA(s, cb)            devicedriver.h:L332（虚接口）
8. LibreVNADriver::setVNA(s, cb)          librevnadriver.cpp:L480-520
     └─ p.settings.if_bandwidth = 500     librevnadriver.cpp:L501
9. （后续，u4-l3 已讲）SendPacket → USB/TCP 传输 → 固件 Communication 分发
10.（后续，u5-l4 已讲）固件 VNA::Setup → FPGA::SetSweep
11.（后续，u6-l3 已讲）FPGA Sampling 以 NSAMPLES×16 样本/点解调，
    IFBW 减半 ⇒ 样本数翻倍 ⇒ 测量更慢、噪声更低
```

关键洞察：第 4→6 步之间的 **100ms 防抖定时器**意味着用户快速连续改带宽不会对设备产生任何中间配置；第 6 步 `ConfigureDevice` 是**全量重配**——哪怕只改了带宽，起止频率、点数、功率也会随 `s` 一并重发，这换来的是「设备状态永远 = settings 结构」这一不变量。

#### 4.3.5 小练习与答案

**练习 1**：`ConfigureDevice` 里 `emit sweepStarted()` 为什么不放在 `setVNA` 的回调里？

**答案**：`setVNA` 的回调要等到设备 Ack 才执行（可能几十毫秒后），而工具栏的 Run 按钮图标、SCPI 的 `:VNA:ACQuisition:RUN?` 查询等 UI/协议状态应立即反映用户意图，不等设备确认。所以「已请求」与「已确认」分成两个时刻，后者由回调里的 `changingSettings = false` 标志。

**练习 2**：频率扫描时 `s.dBmStart` 和 `s.dBmStop` 为什么必须相等？驱动层谁依赖这个约定？

**答案**：频率扫描的 自变量是频率，功率保持恒定，「恒定量」在统一结构里用两侧同值表达（[vna.cpp:L2029-L2030](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L2029-L2030)）。驱动侧 `LibreVNADriver::setVNA` 用 `s.dBmStart != s.dBmStop` 判断是否固定功率（`fixedPowerSetting` 字段，L513），并参与零扫宽判定（L516）。若两侧不等，设备会误以为要做功率扫描。

**练习 3**：501 点、IF BW 1 kHz 时扫描一圈 10 秒。改成 100 kHz IF BW、501 点后大约多久？为什么不是线性缩短 100 倍？

**答案**：IF BW 扩大 100 倍使每点积分时间缩短约 100 倍，但每点还有与带宽无关的固定开销：PLL 换频与锁定等待（u6-l2 的 settling，满量程可达 10.24ms）、USB 传输、MCU 调度。带宽大时这些固定开销占主导，所以实际扫描时间由「点数 ×（settling + 测量时间）」决定，远大于单纯按带宽比例的估算。**（具体倍数待本地验证）**

## 5. 综合实践

**任务：给「改一次中心频率」写出全链路时序报告。**

不看答案，独立完成以下追踪，然后对照验证：

1. 用户在 Center 框输入 `1G`，写出直到设备收到新配置的完整函数调用链（提示：与 4.3.4 的 IF 带宽链几乎同构，但 `SetCenterFreq` L1165-L1181 的特殊之处是**保持 span 不变**地平移 start/stop，并处理越过频率上下限的两种边界情况——找出这两处代码）。
2. 说明这次修改会导致哪些连带效应：`ConstrainAndUpdateFrequencies` 发出的 4 个信号（L1686-L1689）分别刷新哪些控件？若当前处于零扫宽会怎样（对照 L1684-L1685 的推导）？校准标签的颜色为什么会重新计算（提示：`UpdateCalWidget` → `getCalInterpolation` L691-L707，扫描范围跑出校准覆盖区间时标签变黄/变红）？
3. 最后用一段话解释：为什么修改中心频率后正在显示的迹线会被清空重测，而不是保留旧数据？（定位 `SettingsChanged(true)` 的默认参数与 `ResetLiveTraces` L2074-L2084。）

**验收标准**：你能不看讲义默写出「控件 → SetCenterFreq → ConstrainAndUpdateFrequencies → SettingsChanged → (100ms) → ConfigureDevice → setVNA」这条链，并解释链上任何一站删掉会发生什么（例如删掉防抖：连续拖动滑块会向设备狂发配置包；删掉 `changingSettings`：新旧扫描的点会混在同一条迹线里）。

## 6. 本讲小结

- **`Settings` 结构是唯一事实来源**：所有 UI 控件、SCPI 命令、setup 文件加载、偏好设置都收敛到 `Set*` 槽这一组入口，写入前一律用 `Limits.VNA` 夹取，设备状态永远等于 `settings` 的快照。
- **防抖 + 门闩**：`SettingsChanged` 用 100ms 单次定时器合并连续修改，用 `changingSettings` 标志丢弃配置切换期间到达的旧数据点——这两者共同保证了「一次修改只产生一次干净的重配置」。
- **`NewDatapoint` 是一条顺序严格的管线**：守卫 → 原始转发 → 分段偏移 → 平均 → 数据定型（频率/功率/时间）→ 校准 → 写 TraceModel →（可选）去嵌入二次写入，同时按 Raw/Calibrated/Deembedded 三档供给流式服务器。
- **激励端口是算出来的**：TraceModel 依据现存 Live 迹线反向推导需要激励的端口（`requiredExcitation` 闭环），删掉不要的 S 参数迹线能实质缩短扫描时间。
- **`ConfigureDevice` 完成两个世界的翻译**：GUI 的 Freq/Power 双子结构 → 驱动的统一 `freqStart/Stop + dBmStart/Stop`（恒定量两侧同值）；分段扫描与零扫宽都在这里落地。

## 7. 下一步学习建议

- **u7-l2（频谱分析仪模式）**：对照本讲读 `SpectrumAnalyzer/spectrumanalyzer.cpp`，找出它与 VNA 模式共享的设计模式（`Set*` 槽 + `SettingsChanged` + `ConfigureDevice` 三件套几乎复制了一份），重点比较 `SAMeasurement` 与 `VNAMeasurement` 的差异。
- **u7-l4（平均与流式输出）**：本讲只用了 `average.process` 一行，下一讲深入 `averaging.cpp` 的 Mean/Median 策略与 `addStreamingData` 的分档逻辑。
- **u8-l1（Trace 与 TraceModel）**：从数据的消费者视角重看本讲的终点 `addVNAData`，理解 Trace 如何存储、通知视图刷新。
- 想动手的读者可以提前做 u11-l3 的 DemoDriver 实验：实现一个产生假 `VNAMeasurement` 的虚拟设备，就能在没有硬件的情况下把本讲的整条接收管线跑起来观察。
