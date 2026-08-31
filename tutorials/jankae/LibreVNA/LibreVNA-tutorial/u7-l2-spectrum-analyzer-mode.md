# 频谱分析仪模式

## 1. 本讲目标

学完本讲，你应该能够：

1. 对比 SA（Spectrum Analyzer，频谱分析仪）模式与 VNA 模式在「设置结构 → DeviceDriver 接口 → 测量数据结构」三层的差异，理解为什么两者同为"扫描"却需要两套完全不同的接口。
2. 跟踪 SA 模式的完整配置下行链路：工具栏控件 → `Set*` 槽 → `SASettings` 结构 → 防抖定时器 → `setSA()` → 驱动翻译成协议包。
3. 跟踪 SA 的数据上行链路：`SAmeasurementReceived` 信号 → 平均 → 归一化 → `TraceModel::addSAData()`。
4. 解释 RBW、检波器（Detector）、窗（Window）、跟踪源（TG）这些频谱仪特有参数的含义与传递方式。
5. 讲清楚归一化（Normalization）的两阶段状态机：先"测量参考"，后"逐点除法修正"，以及它的全部失效保护。

本讲前置：u7-l1（VNA 模式）。本讲不依赖硬件，所有实践均可通过纯源码阅读完成。

## 2. 前置知识

**频谱仪在测什么。** VNA 测的是 S 参数——入射波与反射/传输波之间的**比值**，因此天然是复数（含相位），并且需要参考接收机提供分母。频谱仪测的是**绝对功率**：某个频率上有多少信号能量，单位 dBm（相对 1 毫瓦的分贝值）。它不关心相位，也不需要参考通道做比值。这个"比值 vs 绝对值"的根本差异，决定了本讲所有代码差异的来源。

**RBW（Resolution Bandwidth，分辨率带宽）。** 频谱仪内部相当于一个中心频率可调的窄带滤波器，边扫边测。这个滤波器的宽度就是 RBW：RBW 越窄，能分辨出靠得越近的两个信号，噪声底也越低（进入带宽的噪声更少），但扫描更慢。它大致对应 VNA 的 IFBW（中频带宽），但 SA 中还叠加了下述"检波器"概念。

**检波器（Detector）。** 传统频谱仪每个显示点（bin）内部其实采样了很多次，检波器决定"一堆内部采样值如何压缩成屏幕上的一个点"：`+Peak` 取最大、`-Peak` 取最小、`Sample` 取其中一个、`Average` 取平均、`Normal` 交替取最大最小。测量噪声或脉冲信号时，检波器选择会显著改变显示结果。

**窗（Window）。** 设备内部用 DFT 把时域采样变换到频域。对有限长数据直接做 DFT 等于乘矩形窗，会产生频谱泄漏；Hann/Kaiser/Flat Top 等窗函数以加宽主瓣为代价压低旁瓣。u6-l4 已在 FPGA 侧精读过这套窗系数，本讲从 GUI 侧看同一个参数如何传下去。

**跟踪源（Tracking Generator，TG）。** 一个输出频率"跟踪"接收机调谐频率的信号源。把 TG 输出接到被测件输入、被测件输出接回频谱仪输入，频谱仪就变成了标量网络分析仪——能测滤波器的传输曲线，但只有幅度没有相位。

**归一化（Normalization）。** TG 信号经电缆、接头进入接收机，这条链路本身不平坦（电缆损耗随频率滚降、TG 输出有平坦度误差）。归一化的做法是：先接直通（或参考电缆）测一遍，把每个频率点每个通道的原始读数存为参考；之后所有测量值逐点除以参考再乘目标电平，链路响应就被"拍平"了。它与 u9 将讲的 SOLT 校准不同：归一化只修幅度标量，不修方向性、隔离度、也不涉及相位误差模型。

**VBW 的说明。** 传统频谱仪还有 VBW（Video Bandwidth，视频带宽）——检波之后的一级低通平滑。需要明确：LibreVNA 的 `SASettings` 中**没有 VBW 字段**，GUI 工具栏也没有 VBW 控件；等效的"事后平滑"功能由 GUI 侧的 Averaging（平均）与检波器选择承担。学习目标中提到 VBW，正是要让你建立"概念上有哪些频谱仪参数、LibreVNA 实际实现了哪些"的辨析能力——这也是阅读开源仪器代码时的重要习惯：以代码为准。

**承接 u7-l1 的心智模型。** VNA 模式的配置链是「一切入口经 `Set*` 槽写入 `Settings` 结构 → `SettingsChanged` 防抖 100ms → `ConfigureDevice` 全量重配」。SA 模式复用了完全相同的骨架，只是 `Settings` 换成了 `DeviceDriver::SASettings`，下发函数换成了 `setSA()`。读 SA 代码时，你可以不断对照"这一步在 VNA 里对应哪个函数"。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.h` | SA 模式类声明：`SASettings settings`、`normalize` 结构体、全部 `Set*` 槽与信号 |
| `Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp` | 本讲主战场：工具栏搭建、配置下发、`NewDatapoint` 数据上行、归一化状态机、SCPI 命令树 |
| `Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h` | `SASettings`/`SAMeasurement` 数据结构、`setSA()`/`getSApoints()` 虚接口、`Feature` 枚举、`Info::Limits.SA` 能力上限 |
| `Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp` | 静态辅助 `DeviceDriver::SApoints()` 的默认值逻辑 |
| `Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp` | 官方驱动如何把 `SASettings` 翻译成协议包（含 RBW→UseDFT 的模式选择） |
| `Software/PC_Application/LibreVNA-GUI/averaging.cpp` | SA 测量值的平均（Mean/Median），线性域处理 |
| `Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp` | `addSAData()`：SA 数据进入 Trace 的最后一跳 |
| `Software/PC_Application/LibreVNA-GUI/VNA/vna.h` | 仅用于对照：VNA 模式的 `Set*` 槽清单 |

## 4. 核心概念与源码讲解

### 4.1 SA 扫描配置：SASettings 与设置下发链路

#### 4.1.1 概念说明

SA 模式的全部用户可配置状态收敛在一个结构体里：`DeviceDriver::SASettings settings`（`spectrumanalyzer.h:95`）。它是"唯一事实来源"——任何 UI 控件、SCPI 命令、setup 文件加载，最终都通过 `Set*` 槽写进它，再统一触发设备重配。

与 VNA 的 `VNASettings` 相比，`SASettings` 少了功率扫描（`dBmStart/dBmStop`）、点数（`points`）、对数扫描（`logSweep`）、激励端口列表（`excitedPorts`）、驻留时间（`dwellTime`），多了 RBW、窗、检波器和一整块跟踪源配置。少的那些之所以不存在，是因为 SA 不做 S 参数测量：没有"激励哪个端口"的概念（除非开 TG），点数也不由用户决定（见 4.1.3 末尾）。

#### 4.1.2 核心流程

配置下行的完整链路（与 u7-l1 的 VNA 链路逐段同构）：

```text
UI 控件 / SCPI / fromJSON
        │  valueChanged / lambda
        ▼
Set* 槽函数（SetStartFreq / SetRBW / SetWindow / SetTGEnabled ...）
        │  写 DeviceDriver::SASettings settings
        │  （可选）按 Info::Limits.SA 夹取超范围值
        ▼
ConstrainAndUpdateFrequencies() 或直接
        ▼
SettingsChanged()
        │  running == false 则直接返回（不浪费通信）
        ▼
configurationTimer（100ms 单次防抖，把连续拖动合并成一次重配）
        ▼
ConfigureDevice()
        ├── running: device->setSA(settings, 回调)
        │            average.reset() / clearLiveData() / setSpan()
        └── !running: device->setIdle(回调)
```

频率的四要素（Start/Center/Stop/Span）互相耦合：改中心保持跨度、改跨度保持中心。任何一种改法都要保证 `[freqStart, freqStop]` 落在设备 `Limits.SA.[minFreq, maxFreq]` 区间内，且不能让 `TG频率 = 信号频率 + trackingOffset` 越界。

#### 4.1.3 源码精读

**数据结构：`SASettings` 与 `SAMeasurement`（对照 VNA）。**

[Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h:L341-L374](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L341-L374) 定义了 SA 的设置契约：频率两项 + RBW + 窗枚举 + 检波器枚举 + 跟踪源四项（使能、端口、偏移、功率）。注意注释明确说明"零扫宽时 freqStart 与 freqStop 相等"。

[Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h:L375-L393](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L375-L393) 是测量数据结构 `SAMeasurement`，与 `VNAMeasurement`（[devicedriver.h:L274-L297](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L274-L297)）的三处关键差异：

| | VNAMeasurement | SAMeasurement |
|---|---|---|
| 测量值类型 | `map<QString, complex<double>>`（含相位） | `map<QString, double>`（纯实数） |
| 值的语义 | 线性 S 参数比值 | 线性电压幅度，**1.0 即 0 dBm** |
| 键名示例 | `S11`、`S21` | `PORT1`、`PORT2` |
| X 轴 union | `frequency + dBm`（功率扫描）/ `us`（零扫宽） | `frequency` / `us`（零扫宽） |

"1.0 = 0dBm"这个线性标度是全篇的隐藏主角：后面归一化的除法、平均的加法、以及显示时的 `20*log10()` 都建立在线性电压域上。

**三个工具栏 = 三组设置入口。**

构造函数（[spectrumanalyzer.cpp:L44-L340](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L44-L340)）搭建了三个工具栏，恰好对应 SA 的三组语义：

- **Sweep 工具栏**（L82-L171）：Run/Stop、Single、Start/Center/Stop/Span 四个 `SIUnitEdit`、Full span、Zoom in/out、Zero span。每个编辑框都是双向连接——`valueChanged` 进 `Set*` 槽，反向信号（如 `startFreqChanged`）回写控件，保证四要素永远显示一致。
- **Acquisition 工具栏**（L174-L227）：RBW 编辑框、窗下拉框（None/Kaiser/Hann/Flat Top）、检波器下拉框（+Peak/-Peak/Sample/Normal/Average）、平均次数与 Reset 按钮。下拉框的**列表顺序就是枚举值**：`addItem("None")` 在索引 0，对应 `Window::None = 0`（[devicedriver.h:L343-L349](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L343-L349)），所以信号槽里直接 `(DeviceDriver::SASettings::Window) index` 强转（[spectrumanalyzer.cpp:L191-L193](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L191-L193)）——一个隐式契约，改枚举顺序或下拉框顺序之一都会悄悄出错。
- **Tracking Generator 工具栏**（L229-L283）：使能、端口选择、电平、偏移，以及归一化的 Enable/To/Measure 三件套（4.3 详述）。

**防抖与设备配置。**

[spectrumanalyzer.cpp:L70-L73](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L70-L73) 创建单次 100ms 定时器；[spectrumanalyzer.cpp:L597-L608](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L597-L608) 的 `SettingsChanged()` 先检查 `running`（停扫状态下改设置不触发任何通信），再启动定时器并 `ResetLiveTraces()` 清掉旧数据——这是"设置变了，旧点必须扔"原则的 SA 版本。

[spectrumanalyzer.cpp:L898-L941](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L898-L941) 是 `ConfigureDevice()` 的全貌。运行分支调用 `window->getDevice()->setSA(settings, cb)`，回调里做两件事：清 `changingSettings` 标志（允许接收新数据点，见 4.2.3），以及检查归一化是否因 span 改变而失效（4.3.3）。随后 `average.reset(DeviceDriver::SApoints())`、`traceModel.clearLiveData()`、`traceModel.setSpan(...)` 三个动作重建数据容器。停止分支则 `setIdle()`。

**能力协商与超范围夹取。**

SA 模式在设备层面声明了三个 Feature（[devicedriver.h:L78-L81](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L78-L81)）：`SA` 本体、`SATrackingGenerator`、`SATrackingOffset`。[spectrumanalyzer.cpp:L350-L371](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L350-L371) 的 `initializeDevice()` 逐一检查：不支持 SA 直接弹错误框并返回；不支持 TG 就禁用整个 TG 工具栏；不支持 TG 偏移就把 offset 控件锁死为 0——注意这是**第三级降级**：同一台设备的 TG 可用而 TG 偏移不可用。

数值夹取发生在两个层面。RBW 在 `SetRBW()`（[spectrumanalyzer.cpp:L722-L732](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L722-L732)）里被夹到 `[Limits.SA.minRBW, maxRBW]`，并回发 `RBWChanged` 信号把夹取后的值**写回控件**（用户输入 1 Hz，控件会自己跳回最小值）——这是"设备上限决定 UI 显示"的直接证据。TG 电平同理（[spectrumanalyzer.cpp:L789-L801](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L789-L801)）。频率则在 `ConstrainAndUpdateFrequencies()`（[spectrumanalyzer.cpp:L1219-L1250](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L1219-L1250)）统一夹取，该函数还额外约束 TG 偏移：因为 TG 实际输出频率是 \( f_{TG} = f + \text{offset} \)，它必须连同测量频率一起落在设备频率范围内，越界时偏移被裁剪并弹警告框（L1231-L1243）。

**驱动侧翻译：setSA() 如何变成协议包。**

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp:L543-L589](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L543-L589) 是官方驱动的 `setSA()`。几个值得逐行读的细节：

- **点数由驱动决定，用户无感知**（L556-L563）：`maxSApoints = 1001`；span ≥ 1001 Hz 时固定 1001 点，span 更小时用 `span+1` 个点（点距 1 Hz）。所以 `SASettings` 里根本没有 points 字段——SA 的"每点带宽"是隐含的。
- **RBW 触发设备内部模式切换**（L568-L571）：`UseDFT` 位仅在"未开 TG + 驱动偏好允许 + RBW 足够窄 + 非零扫宽"时置 1。也就是说，同一个 RBW 数值会让设备在 u6-l3 讲过的"单 bin 采样解调"与 u6-l4 讲过的"96 bin DFT"两条硬件路径之间切换。RBW 不只是一个滤波器宽度，它是模式开关。
- **Signal ID**（L566）来自驱动的设备偏好（对应固件侧 u5-l4 讲过的信号识别：同频点多组本振配置各测一次取最小），GUI 模式层完全看不到它。
- **单位换算**（L574）：`trackingPower * 100` 把 dBm 变 cdbm（百分之一 dBm），这是 u4-l3 已总结的协议整数化惯例；`trackingGeneratorPort - 1`（L577）则把 GUI 的"端口从 1 数"换成协议的"从 0 数"。

最后 `getSApoints()` 的默认值兜底在 [Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp:L57-L64](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L57-L64)：没有活动设备时返回 1001，与驱动侧上限一致——GUI 在无设备状态下也能预建 1001 点的容器。

#### 4.1.4 代码实践

**实践：跟踪一次 RBW 修改的完整调用链。**

1. **实践目标**：不靠运行程序，仅靠源码确认"用户在 RBW 框输入 10 Hz"之后代码走过的每一步，体会"设置 → 夹取 → 防抖 → 重配"四段式。
2. **操作步骤**：
   - 在 [spectrumanalyzer.cpp:L176-L182](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L176-L182) 找到 RBW 编辑框的 `valueChanged` 连接，确认目标槽是 `SetRBW`。
   - 通读 `SetRBW()`（L722-L732），写下夹取逻辑使用的两个 `Info::Limits.SA` 字段名。
   - 沿 `SettingsChanged()` → `configurationTimer` → `ConfigureDevice()` → `setSA()` 追到 [librevnadriver.cpp:L564](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L564)，确认 RBW 值被赋给协议包的哪个字段。
   - 查一查 LibreVNA 驱动实际填的 `minRBW` 是多少（提示：在 `librevnadriver.cpp` 中搜索 `minRBW`，构造 `Info` 的代码里有硬编码值）。
3. **需要观察的现象**：纸上调用链应共有 6 站左右，且中途没有任何一条"直接发 USB 包"的捷径——所有设置变化都必须经过防抖定时器。
4. **预期结果**：得到一条形如 `eBandwidth:valueChanged → SetRBW(夹取) → SettingsChanged → (100ms) → ConfigureDevice → setSA → SpectrumAnalyzerSettings.RBW` 的链。minRBW 的具体数值以你 grep 到的代码为准（待本地验证：若设备实际行为与代码常量不符，说明固件版本与 GUI 有差异）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SASettings` 没有 `points` 字段而 `VNASettings` 有？

**答案**：VNA 的点数直接决定扫描的频率网格，用户需要用它权衡速度与分辨率（且受 `Limits.VNA.maxPoints` 限制）；SA 的点数由驱动根据 span 自动决定（`setSA()` 中 `min(span+1, 1001)`），点距固定为 1 Hz 或 span/1000，用户没有独立的点数需求，GUI 只能通过 `getSApoints()` 查询。

**练习 2**：窗下拉框与 `Window` 枚举之间靠什么保持一致？这种设计有什么风险？

**答案**：靠"下拉框 addItem 的顺序 == 枚举整数值"这一隐式契约，槽里直接 `(Window) index` 强转。风险是修改任何一侧的顺序（例如在枚举中间插入新窗型）而不同步另一侧，会导致用户选 Kaiser 实际下发 Hann，且编译器不会报任何错。

**练习 3**：`SetTGOffset()` 修改的是 TG 偏移，为什么它要调用 `ConstrainAndUpdateFrequencies()`（一个名字里带"Frequencies"的函数）？

**答案**：因为 TG 的实际输出频率是测量频率加偏移，偏移变化会改变"可达频率范围"的判定：`ConstrainAndUpdateFrequencies()` L1232-L1239 检查 `freqStop + trackingOffset ≤ maxFreq` 与 `freqStart + trackingOffset ≥ minFreq`，越界时反方向裁剪偏移本身并弹警告。偏移与测量频率是耦合约束，必须一起收口。

### 4.2 SAMeasurement 数据流：从设备信号到 Trace

#### 4.2.1 概念说明

SA 的数据上行从 `DeviceDriver::SAmeasurementReceived(SAMeasurement)` 信号开始（[devicedriver.h:L417-L422](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L417-L422)）。u3-l1 已总结过"数据经 Qt 信号推送，配置采用请求＋回调"的驱动契约，本模块看 SA 侧的接收端如何消费这个信号。

SA 的每个测量点只携带：点号 `pointNum`、X 值（`frequency` 或零扫宽时的 `us`）、每个端口一个线性幅度。与 VNA 相比它少了整整一层"拼装"——VNA 驱动要拿接收读数除以参考读数才能算出 S 参数，SA 的测量值设备端就是最终幅度。因此 SA 的上行管线更短，但多出两个 SA 专属环节：**平均**（在显示前抑制噪声起伏）和**归一化**（4.3）。

值得先建立的一个数量级感觉：一次扫描最多 1001 点、每点一个 `map` 两条 double，连续扫描时数据以"每点一个信号"的粒度涌入 GUI 线程，所以接收函数里的每个操作都在热路径上——你会看到代码为此做了不少防御（丢点检测、marker 只在扫描末尾更新）。

#### 4.2.2 核心流程

`NewDatapoint(m)` 的处理序列：

```text
SAmeasurementReceived(m)
        ▼
NewDatapoint(m)
  ├─ 守卫 1：模式未激活 → 丢弃
  ├─ 守卫 2：changingSettings → 丢弃（旧设置的迟到点）
  ├─ 守卫 3：单次扫描已完成（average.getLevel()==averages）→ Stop() 并丢弃
  ├─ m_avg = average.process(m)        ← 多圈滚动平均（线性域）
  ├─ 零扫宽：把 us 时间戳归零（首点记 firstPointTime）
  ├─ 流式输出 Raw
  ├─ [归一化测量中] 最后一圈逐点收集参考值 → portCorrection
  ├─ [归一化激活] m /= portCorrection[点];  m *= 10^(Level/20)
  │                流式输出 Normalized
  ├─ traceModel.addSAData(m_avg, settings)   ← 分发给各 Live Trace
  └─ 末点（pointNum == SApoints()-1）：更新平均计数 + marker
```

#### 4.2.3 源码精读

**三个守卫。**

[spectrumanalyzer.cpp:L513-L527](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L513-L527) 是 `NewDatapoint` 的入口。三个守卫各挡一种竞态：非活动模式（其他 Mode 激活时设备可能仍在回报）、`changingSettings`（新 `setSA` 已发出但旧扫描的点还在路上——`ConfigureDevice` 置位、`setSA` 回调清零，见 L902/L907）、以及单次扫描模式下的"收满即停"（`average.getLevel() == averages` 表示平均队列已满一圈，再收就是第二次扫描了）。

**平均：线性域的 Mean/Median。**

[spectrumanalyzer.cpp:L529-L532](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L529-L532) 调用 `average.process(m)`。SA 版本在 [Software/PC_Application/LibreVNA-GUI/averaging.cpp:L50-L67](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/averaging.cpp#L50-L67)：把 `double` 测量值装进 `complex<double>` 走通用管线，出来后取 `.real()` 丢掉虚部。通用管线（[averaging.cpp:L102-L177](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/averaging.cpp#L102-L177)）按 `pointNum` 索引一个 deque，每个点独立维护"最近 averages 圈"的滚动队列——**跨圈对齐**靠的正是测量点里携带的 `pointNum`。Mean 模式求算术平均，Median 模式（L142-L168）按幅度排序取中位数，对脉冲干扰更鲁棒；SA 场景下两者都作用在线性电压上，`20*log10` 之后才变成 dB 的平均（功率平均与幅度平均在数学上不等价，这是理解"SA 平均会改变噪声底统计"的钥匙）。

**零扫宽的时间轴。**

[spectrumanalyzer.cpp:L534-L542](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L534-L542)：当 `freqStart == freqStop`（`SetZeroSpan()` 在 [spectrumanalyzer.cpp:L677-L682](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L677-L682) 把 start=stop=center 制造这一状态），`SAMeasurement` 的 union 换用 `us` 字段。首点（`pointNum == 0`）记录绝对时间戳 `firstPointTime` 并把本点归零，后续点减去它——X 轴从此是"距扫描开始的秒数"，频谱仪变成一台时域功率记录仪（AM 解调、瞬态捕捉都靠它）。

**进入 Trace 与性能防御。**

[spectrumanalyzer.cpp:L581-L591](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L581-L591) 收尾三件事：`traceModel.addSAData` 分发数据；扫描末点才 `UpdateAverageCount()` + `markerModel->updateMarkers()`（marker 插值计算开销大，从"每点"降到"每扫描"一次）；以及静态 `lastPoint` 变量做**丢点检测**——SA 协议不重传，`qWarning` 把缺口报给日志，这是现场排查"曲线有洞"问题的第一现场。

`addSAData` 本体在 [Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp:L325-L350](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp#L325-L350)：遍历所有 Live 且未暂停的 Trace，用 Trace 的 `liveParameter()`（如 `"PORT1"`）去 `m.measurements` 这个 map 里查值——**查不到就跳过该 Trace**（L343-L345 的 `continue`）。这行代码解释了一个常见现象：换了一台只有 1 个 SA 端口的设备后，`PORT2` 迹线不会报错也不会更新，只是静止不动。零扫宽与非零扫宽在此分叉：前者按 `index`（点号）插入、`td.x = us/1e6`；后者 `td.x = frequency`。

**SA 专属流式输出。**

[spectrumanalyzer.cpp:L544](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L544) 与 L577 分别以 `SADataType::Raw` / `Normalized` 两级调用 `addStreamingData`——SA 的流式通道与 VNA 的三级（Raw/Calibrated/Deembedded）不同，恰好只有"归一化前/后"两级，因为 SA 管线上没有校准与去嵌入环节。u7-l4 会展开 StreamingServer 细节。

#### 4.2.4 代码实践

**实践：无硬件走读零扫宽数据路径，验证 union 的两副面孔。**

1. **实践目标**：确认同一段代码在零扫宽与非零扫宽下分别读 `us` 与 `frequency`，并理解为什么这样写是安全的（或危险的）。
2. **操作步骤**：
   - 读 [devicedriver.h:L379-L388](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L379-L388)，注意 `frequency` 与 `us` 是同一内存位置的 union。
   - 找出所有读取该 union 的分支：`NewDatapoint` L534-L542（判据 `settings.freqStart == settings.freqStop`）、`TraceModel::addSAData` L333-L339（同一判据，但用的是**传入的 settings** 而非测量本身）、`librevnadriver.cpp` 上行组包处的 zerospan 标志（在 `handleReceivedPacket` 中，可搜索 `zerospan`）。
   - 思考：判据依赖外部 settings 而非数据自带标记，如果设备在 span 恰好为 0 时上报的字段与 GUI 判定不一致会发生什么？
3. **需要观察的现象**：三处判据全部回溯到同一个事实源（`freqStart == freqStop`，驱动侧记为 `zerospan` 标志）。
4. **预期结果**：能写出一段 5 行以内的分析，指出"union + 外部判据"模式的前提是配置变更与新数据之间没有竞态窗口，而 4.1 讲的 `changingSettings` 守卫正是堵这个窗口的机制。（待本地验证：如有硬件，可开零扫宽、看信号后按 Zoom Out，观察有无一帧数据 X 轴类型错乱。）

#### 4.2.5 小练习与答案

**练习 1**：SA 的平均在什么域进行？为什么 `Averaging::process(SAMeasurement)` 要先转 `complex<double>` 再取 `.real()`？

**答案**：在线性电压域。转复数是为了复用与 VNA 完全相同的平均容器与 Mean/Median 算法（`averaging.cpp` 只实现了一份基于 `complex<double>` 的管线），SA 分支最后取实部。代价是一次无意义的虚部搬运，收益是少维护一套模板代码。

**练习 2**：`NewDatapoint` 里为什么 marker 更新放在"扫描末点"而不是每个点都更新？

**答案**：marker 的峰值搜索/插值需要遍历整条 Trace，每点触发会造成 \( O(N) \) 操作 × \( N \) 点 = \( O(N^2) \) 的每扫描复杂度；末点触发（L583-L586）把它降为每扫描一次 \( O(N) \)。对连续扫描的 1001 点而言是三个数量级的差别。

**练习 3**：丢点警告（L587-L591）用了函数内 `static unsigned int lastPoint`。这个静态变量在哪些情况下会产生误报？

**答案**：任何"点号不连续但并非丢点"的场合：配置变更后新扫描从点 0 重新开始（`lastPoint` 还停在上一次扫描的末点，如 1000 → 新扫描点 1 会报"missed"）；以及两台 SA 端口数不同的设备切换时点数变化。它只是个诊断提示，不参与任何控制逻辑。

### 4.3 跟踪源与归一化

#### 4.3.1 概念说明

跟踪源让频谱仪能测"传输幅度"，但 TG + 电缆 + 接收机这条链路的频率响应不平坦。归一化用两遍测量解决这个问题：第一遍（测量阶段）在直通状态下记录每个频率点、每个端口的原始读数作为参考向量 `portCorrection`；第二遍起（应用阶段）每个新到的点做逐点除法并缩放到目标电平。

数学上，设第 \( n \) 点的测量值为 \( m[n] \)（线性电压），参考值 \( r[n] \)，目标电平 \( L \) dBm，则显示值：

\[
m_{\text{norm}}[n] \;=\; \frac{m[n]}{r[n]} \cdot 10^{L/20}
\]

取对数即可见其 dB 语义是逐点减法：\( 20\lg m_{\text{norm}} = 20\lg m - 20\lg r + L \)。系数用 \( 20 \) 而非 \( 10 \)，因为操作对象是电压幅度比而非功率比。理想直通下 \( m[n] = r[n] \)，曲线被拍平到 \( L \) dBm 一条直线；之后接入被测滤波器，曲线就是滤波器的插入损耗。

关键工程约束：**参考向量是按点号索引的**，所以一旦 span 或点数变化，旧参考立刻作废。代码里为此布了四道失效保险（见 4.3.3）。

#### 4.3.2 核心流程

归一化是一个双状态子系统（`normalize` 结构体，[spectrumanalyzer.h:L115-L129](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.h#L115-L129)）：

```text
[空闲] ──Measure 按钮 / EnableNormalization(true) 且无有效参考──▶ [measuring=true]
   ▲                                                              │ SettingsChanged()
   │                                                              ▼ 触发新一轮扫描
   │                                              每点检查：average.currentSweep()==averages?
   │                                                              │ 是：最后一圈，
   │                                                              │ portCorrection[param].push_back(值)
   │                                                              │
   └─── 末点(pointNum==SApoints()-1)：measuring=false，            ▼
        记录 f_start/f_stop/points，EnableNormalization(true)
[active=true]
   │ 每点：m /= portCorrection[param][pointNum]; m *= 10^(Level/20)
   │
   └─ 任一失效条件命中 ──▶ EnableNormalization(false)
        ① ConfigureDevice 回调发现 span/点数已变 → 禁用 + 提示
        ② TG 被关闭 → 禁用（没有 TG 就没有归一化对象）
        ③ 换设备(deviceInfoUpdated) → ClearNormalization 连参考一起删
        ④ EnableNormalization(true) 时校验设置不匹配 → 自动重新进入测量阶段
```

#### 4.3.3 源码精读

**测量阶段：MeasureNormalization()。**

[spectrumanalyzer.cpp:L813-L836](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L813-L836)：先清旧参考并按 `availableSAMeasurements()`（官方驱动返回 `PORT1..PORTn`，见 [librevnadriver.cpp:L534-L541](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L534-L541)）给每个通道建空向量；置 `measuring = true`；进度对话框只在 `window->showGUI()` 时创建（L824，这正是 c4276df 提交为 `--no-gui` 模式补的守卫之一，承接 u2-l1 讲过的无头两层机制）；最后 `SettingsChanged()` 强制重启扫描——**测量参考必须从第一圈平均开始**，否则参考里混入半旧数据。

**采样时机：为什么只在"最后一圈"收集。**

[spectrumanalyzer.cpp:L546-L569](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L546-L569) 是收集逻辑，判据 `average.currentSweep() == averages`（`currentSweep()` 定义在 [averaging.cpp:L78-L85](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/averaging.cpp#L78-L85)，返回第一点的队列深度，即当前是第几圈）。选最后一圈意味着**参考值本身就是平均后的稳定值**，与后续应用归一化的数据（同样经过平均，注意 L546 拿的是 `m_avg` 而非原始 `m`）在统计上一致。收集从点 0 开始（`portCorrection.size() > 0 || m_avg.pointNum == 0` 防止从中间接入），末点收尾时把当前 span 与点数快照进 `normalize.f_start/f_stop/points`——这三项就是日后校验参考有效性的"指纹"。进度条百分比公式（L565）用圈数与点号加权，`--no-gui` 下跳过（L566-L568）。

**应用阶段：一行除法。**

[spectrumanalyzer.cpp:L571-L578](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L571-L578)：`corr = pow(10.0, normalize.Level->value() / 20.0)` 预计算电平因子，然后每个通道 `m.second /= portCorrection[m.first][m_avg.pointNum]; m.second *= corr;`——公式 \[ ...\] 的直接翻译。注意除法用的是**点号索引**而非频率索引，这正是参考与当前扫描必须同点数、同 span 的原因。归一化后的数据以 `SADataType::Normalized` 再送一份给流式服务器。

**启用校验与四道保险。**

`EnableNormalization()`（[spectrumanalyzer.cpp:L845-L866](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L845-L866)）在启用前核对指纹（保险④，不匹配自动转入测量阶段）。保险①在 `ConfigureDevice` 的 `setSA` 回调（[spectrumanalyzer.cpp:L909-L916](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L909-L916)）——放在回调而非 `SettingsChanged` 里，是因为此刻设备已确认接受新设置， span 变更已成事实，弹出的 InformationBox 告知用户归一化被自动关闭。保险②在 `SetTGEnabled()`（L769-L772）：TG 关了，归一化的物理前提消失。保险③在 `deviceInfoUpdated()`（[spectrumanalyzer.cpp:L1312-L1322](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L1312-L1322)）：换设备直接 `ClearNormalization()`，同时按新设备的 SA 端口数重建 TG 端口下拉框。

**持久化：参考向量随 setup 存盘。**

[toJSON 的 L413-L425](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L413-L425) 把指纹、目标电平与整个 `portCorrection`（每通道一个 double 数组）序列化进 setup JSON；[fromJSON 的 L480-L505](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L480-L505) 恢复时先核对每个修正向量的长度等于 `normalize.points`（防止手改文件后越界访问），全部一致才 `EnableNormalization(true)`。所以归一化测量做完一次、存进 setup，下次开机不必重测——前提是别动 span。

#### 4.3.4 代码实践

**实践：手工推演一次归一化（无硬件）。**

1. **实践目标**：用假想数据走通"测量 → 应用"两个阶段，验证你对点号索引与线性域除法的理解。
2. **操作步骤**：
   - 设想设备只有 PORT1，`averages = 1`，`SApoints() = 5`，扫描频率 1~5 GHz，TG 电平 -10 dBm，目标电平 `Level = 0`。
   - 假设直通测量阶段 5 个点的线性读数为 `{0.30, 0.32, 0.35, 0.33, 0.31}`（对应约 -10.5、-9.9、-9.1、-9.6、-10.2 dBm，链路略有起伏）。
   - 按 [spectrumanalyzer.cpp:L571-L577](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L571-L577) 的公式手算应用阶段每个点的输出值。
   - 再把一个插入损耗 3 dB 的假想滤波器串入（各点读数全部减半），重算输出。
3. **需要观察的现象**：第一遍输出应近似全 1.0（即 0 dBm 直线，残留起伏来自参考本身的波动）；第二遍输出应近似 \( 10^{-3/20} \approx 0.708 \)。
4. **预期结果**：两组共 10 个数值，第二遍与第一遍之比恒为 0.5——因为除以参考消掉了链路响应，只剩被测件。这就是"归一化拍平系统、暴露 DUT"的数值演示。（纯手算，无需设备。）

#### 4.3.5 小练习与答案

**练习 1**：归一化与 VNA 的 SOLT 校准（u9 主题）最本质的区别是什么？

**答案**：归一化是**单次比值修正**——只存一条参考曲线、只做幅度除法，无法分离方向性、源匹配、负载匹配等系统误差，也没有相位误差模型；SOLT 是**误差模型求解**——用多个已知标准件的测量解出误差网络参数，再对任意测量做矩阵反演。归一化相当于 VNA 里的"响应（直通）归一化"，是 SOLT 的一个极小子集。

**练习 2**：如果用户在归一化激活时把平均次数从 1 改成 10，代码会发生什么？参考会失效吗？

**答案**：不会失效。保险①只检查 `f_start/f_stop/points`（span 与点数），平均次数不在指纹里——因为参考在测量阶段就是按当时的 `averages` 平均出来的（收集发生在 `currentSweep() == averages` 的最后一圈），应用阶段的数据也经过同样的平均器，两侧统计一致。但要注意改平均会触发 `SettingsChanged` → `ResetLiveTraces()` → `average.reset()`，数据会重新积累。

**练习 3**：为什么 `MeasureNormalization()` 里要显式调用 `SettingsChanged()`，即使设置可能一个字都没变？

**答案**：归一化参考必须从一次**完整的新扫描**的第一圈开始收集。若不重启扫描，收集可能从扫描中段开始，`portCorrection` 前半段缺失或混入旧数据。`SettingsChanged()` 触发 `ConfigureDevice()` → `average.reset()` + `clearLiveData()`，保证点 0 从零开始、`currentSweep()` 计数从头累积，收集判据才成立。

## 5. 综合实践

**任务：编制「VNA 模式 vs SA 模式：设置项 → DeviceDriver 接口」对照表，并分析不支持场景。** 这是本讲规格指定的实践，纯源码阅读即可完成，是检验你是否真正吃透两条配置链的试金石。

**步骤：**

1. **收集两侧的设置项清单。**
   - SA 侧：从 [spectrumanalyzer.cpp:L54-L83](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L54-L83) 的私有槽列表（`SetStartFreq` 到 `SetNormalizationLevel`）逐个登记，注明每个槽写入 `SASettings` 的哪个字段。
   - VNA 侧：同样从 [Software/PC_Application/LibreVNA-GUI/VNA/vna.h:L96-L123](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.h#L96-L123) 的槽列表登记（`SetSweepType`、`SetStartFreq`、`SetLogSweep`、`SetStartPower`、`SetPoints`、`SetIFBandwidth`、`SetDwellTime` 等）。

2. **编制对照表。** 建议按下面骨架填充（答案不唯一，关键是"每行都要能在两边代码里指出函数与字段"）：

   | 语义 | VNA 设置项 / 槽 | SA 设置项 / 槽 | 共同点 | 差异点 |
   |---|---|---|---|---|
   | 扫描起点 | `SetStartFreq` → `freqStart` | `SetStartFreq` → `freqStart` | 同名槽、同夹取逻辑 | — |
   | 带宽 | `SetIFBandwidth` → `IFBW` | `SetRBW` → `RBW` | 都受设备 Limits 夹取 | IFBW 决定接收机带宽；RBW 还会切换设备内部采样/DFT 模式 |
   | 点数 | `SetPoints` → `points` | **无**（驱动在 `setSA` 内决定） | — | SA 点数不可配 |
   | 功率/电平 | `SetSourceLevel`/`SetStartPower`/`SetStopPower` | `SetTGLevel`（仅 TG） | 都夹到 `[mindBm, maxdBm]` | VNA 电平是激励、可扫描；SA 电平属于 TG |
   | 激励端口 | `excitedPorts`（由 Trace 反推） | `SetTGPort` | 端口从 1 数 | VNA 可多端口同时；SA 只有一个 TG 端口 |
   | 窗 / 检波器 | 无 | `SetWindow` / `SetDetector` | — | DFT 域专属 |
   | 零扫宽 | `SetZeroSpan` | `SetZeroSpan` | union 复用 `us` | VNA 还有功率扫描第二形态 |
   | 数据后处理 | 校准 + 去嵌入 + 平均 | 归一化 + 平均 | 都在 GUI 线程 | 层级不同 |

3. **分析"不支持"的三种表达。** 结合 u3-l1/u3-l3 已建立的能力协商框架，在代码里各找一个实例：
   - **隐式不覆写**：`DeviceDriver::setSA()` 默认实现返回 `false`（[devicedriver.h:L410](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L410)）。纯矢网驱动（如 SNA5000A）不覆写它，进入 SA 模式即被 `initializeDevice` 的 `supports(Feature::SA)` 检查拦下。
   - **Feature 粒度降级**：设备支持 `SA` 但不支持 `SATrackingOffset` 时，[spectrumanalyzer.cpp:L361-L364](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L361-L364) 只锁 offset 控件，TG 其余功能照常。
   - **Limits 数值夹取**：RBW、TG 电平、频率超出 `Info::Limits.SA` 时被裁剪并回写 UI（4.1.3 已精读）。

4. **验证方式**：把你表格里的每一行拿去代码里反查——按住 Ctrl 点开槽函数，确认它写入的字段与表格一致。若某行查不到（例如你写了"SA 有 VBW 设置"），修正它并注明"代码中不存在"。

**预期产出**：一张 10 行以上的对照表 + 三条"不支持"机制各一个带文件行号的实例。这份表也是你日后为其他仪器写驱动（u11-l3 毕业实战）时的接口设计检查清单。

## 6. 本讲小结

- SA 与 VNA 共用同一套配置骨架（`Set*` 槽 → settings 结构 → 100ms 防抖 → `ConfigureDevice`），但契约不同：`SASettings` 无点数/功率扫描/对数扫描，多 RBW/窗/检波器/TG 四块；`SAMeasurement` 是"map<端口名, 线性电压>"，1.0 即 0 dBm，不含相位。
- SA 点数对用户不可见：驱动在 `setSA()` 里按 `min(span+1, 1001)` 自行决定，GUI 经 `getSApoints()` 查询（无设备时默认 1001）。
- RBW 不只是滤波器宽度：在官方驱动里，足够窄的 RBW（且未开 TG、非零扫宽）会把设备切到 DFT 模式（`UseDFT` 位），对应 FPGA 两条不同的片上处理路径。
- 数据上行 `NewDatapoint` 有三重守卫（模式未激活 / changingSettings / 单扫完成），随后依次做线性域平均（Mean/Median，按 pointNum 跨圈对齐）、零扫宽时间归零、归一化、分发 `TraceModel::addSAData`；marker 更新与丢点检测各有性能/诊断考量。
- 归一化是双状态机：测量阶段只在"最后一圈平均"逐点收集参考（`currentSweep() == averages`），应用阶段逐点 `m /= ref; m *= 10^(L/20)`；参考按点号索引，因此 span/点数变化触发四道保险（回调检查、TG 关闭、换设备、启用校验）。
- VBW 在 LibreVNA 的 SA 中不存在，等效平滑由 Averaging 与检波器承担——以代码为准，不臆造仪器参数。

## 7. 下一步学习建议

- **u7-l3（信号发生器模式）**：三个模式中最小的一个，正好用本讲的"设置链路"框架快速攻克，并对比 `SGSettings` 这份最简契约。
- **u7-l4（平均、数据分级与流式输出）**：本讲两次路过的 `average` 与 `addStreamingData` 在那一讲展开——VNA 的三级（Raw/Calibrated/Deembedded）与 SA 的两级（Raw/Normalized）将并列对比。
- **u9-l2（校准求解器）**：带着"归一化只是幅度除法"的认知去看 12 项误差模型如何用多个标准件解出误差网络，理解两者差距的本质。
- **延伸阅读**：`spectrumanalyzer.cpp` 的 `SetupSCPI()`（L950-L1212）把本讲所有设置暴露为 `:SA:FREQuency:SPAN`、`:SA:ACQuisition:RBW`、`:SA:TRACKing:NORMalize:MEASure` 等命令，可提前翻看，为 u10（SCPI 与远程控制）热身。
