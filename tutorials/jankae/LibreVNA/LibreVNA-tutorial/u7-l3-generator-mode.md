# 信号发生器模式

## 1. 本讲目标

本讲走读 LibreVNA-GUI 中最简单的一种测量模式——信号发生器（Generator / Signal Generator）。学完后你应该能够：

1. 解释 Generator 模式如何维护「当前输出配置」：一个 `SignalgeneratorWidget` 持有全部状态，模式本身几乎零逻辑。
2. 完整跟踪一次频率（或功率）修改从 UI 控件到 `DeviceDriver::setSG()`、再到 USB 批量传输的调用链。
3. 说清楚 Generator 模式与 VNA 点频模式（零扫宽）的本质区别：一个只下行配置、无上行数据；一个仍是完整测量回路。
4. 对比 Generator 与 VNA 在「设置变更如何下发」上的不同取舍（直连 vs 100ms 防抖），并解释为什么连续拖动滑块时需要防抖或合并更新。

本讲是单元 7 的第三讲。前两讲（u7-l1 VNA 模式、u7-l2 频谱仪模式）建立了「Set* 槽 → Settings 结构 → 防抖 → ConfigureDevice → 驱动下发」的模板认知；本讲你会看到这个模板的**极简版**——正是因为它足够简单，反而最适合用来把「UI 事件到 USB 字节」这条链路一次性走透。

## 2. 前置知识

- **信号发生器（Signal Generator，SG）是什么**：一台只输出连续波（CW，即单一频率的正弦信号）的仪器，用户控制三件事：输出频率、输出功率（dBm）、从哪个端口输出。它不测量任何东西。
- **dBm 与 cdbm**：dBm 是以 1 毫瓦为基准的功率对数单位。协议中为避免浮点数，用「厘 dBm」（centi-dBm，即 dBm × 100）的整数表示，这与 u4-l3 讲过的 `cdbm` 约定一致。
- **Qt 信号与槽**：`emit SettingsChanged()` 会同步调用所有连接到该信号的槽（同线程下相当于直接函数调用）。本讲的整条下发链就是靠一个信号串起来的。
- **`SIUnitEdit`**：项目自定义的带 SI 前缀的单位输入框（如输入 `3.5G` 表示 3.5 GHz）。它有两个关键方法：`setValue()` 会发 `valueChanged` 信号，`setValueQuiet()` 只改数值不发信号——「安静地改」是 UI 编程中避免信号回环的标准手段。
- **`isActive` 与 `initializeDevice()`**：来自 u2-l2 的 Mode 体系。同一时刻只有一个活动模式；模式被激活时 `initializeDevice()` 会被调用，是模式向设备下发初始配置的时机。
- **单包在途（u3-l3/u4-l3）**：协议的 Ack 不带序号，因此驱动一次只允许一个包在途，其余包在 `transmissionQueue` 中排队。这个机制与本讲的「拖滑块」问题直接相关。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp) | `Generator` 模式类：极薄的粘合层，把控件信号接到驱动 |
| [Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp) | 信号源控件：全部 UI 状态、限幅、GUI 侧扫频定时器、JSON 持久化 |
| [Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.h) | 控件类声明：信号 `SettingsChanged`、槽 `setLevel/setFrequency/setPort` |
| [Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h) | `DeviceDriver::SGSettings` 数据结构与 `setSG()` 虚接口 |
| [Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp) | `LibreVNADriver::setSG()`：把 SGSettings 翻译成协议包（注意：此链接以正文中的正确链接为准） |
| [Software/VNA_embedded/Application/Communication/Protocol.hpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp) | `GeneratorSettings` 帧的线上格式定义（GUI 与固件同源编译） |
| [Software/PC_Application/LibreVNA-GUI/CustomWidgets/siunitedit.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/siunitedit.cpp) | `SIUnitEdit`：理解信号何时发出（回车/失焦/前缀键/滚轮） |
| [Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp) | 对照组：VNA 模式的 100ms 防抖定时器 |

固件侧的 `Generator.cpp`（设备端如何执行输出）已在 u5-l4 讲过，本讲只在结尾做衔接，不重复。

## 4. 核心概念与源码讲解

### 4.1 Generator 模式状态

#### 4.1.1 概念说明

回顾 u2-l2 的 Mode 体系：每种模式必须实现 `getType`、`initializeDevice`、`preset` 等接口，并且模式自己是「界面元素 + SCPI 子树 + JSON 持久化」的三合一容器。

Generator 模式是所有模式中状态最少的。VNA 模式有几十项设置组成的 `Settings` 结构、校准对象、平均器、TraceModel；而 Generator 模式类本身**一个设置字段都没有**——它的全部状态（频率、功率、端口、扫频参数）都住在中央控件 `SignalgeneratorWidget` 里。模式类只做三件事：

1. 创建控件并设为中央界面；
2. 把控件的 `SettingsChanged` 信号接到 `updateDevice()` 槽；
3. 用 `toJSON`/`fromJSON` 直接转发控件的持久化实现。

这是一种典型的「哑模式 + 智能控件」分工：当模式的界面就是一个控件时，模式退化为壳。

#### 4.1.2 核心流程

Generator 模式的生命周期：

```text
构造:  new SignalgeneratorWidget → 读 Preferences/QSettings 恢复上次频率/功率
       → setupSCPI() 注册 3 条命令 → finalize(central) 挂入中央 QStackedWidget
       → connect(SettingsChanged → updateDevice)

激活:  ModeHandler 调用 activate() → initializeDevice()
       → 检查设备 supports(Feature::Generator) → updateDevice() 下发当前配置

运行:  用户改 UI → 控件发 SettingsChanged → updateDevice() → setSG()

切走:  deactivate() → 把频率/功率写入 QSettings → Mode::deactivate()（内部会让设备 setIdle）
```

注意与 VNA 模式的一个关键差异：**Generator 没有任何上行数据路径**。它不连接 `VNAmeasurementReceived` 之类的信号，不写 TraceModel，`setAveragingMode` 是空实现。整条链路只有「PC → 设备」的下行方向。

#### 4.1.3 源码精读

构造函数，状态初始化与信号连接：

[Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp:7-28](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L7-L28)

这段代码做了四件事：创建中央控件（第 10 行）；若偏好设置了「记住上次设置」则从 `QSettings` 恢复频率/功率，否则用 `Preferences` 里的启动默认值（第 15-22 行）；注册 SCPI 命令；`finalize(central)` 把控件登记为模式的中央界面。**第 27 行是全模式的主动脉**：`connect(central, &SignalgeneratorWidget::SettingsChanged, this, &Generator::updateDevice)`——注意这是**直连，没有任何防抖定时器**，与 VNA 模式形成鲜明对比（详见 4.3.4）。

激活设备时的能力检查与首次下发：

[Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp:40-47](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L40-L47)

`initializeDevice()` 在模式被激活时由 ModeHandler 触发。它先检查 `supports(DeviceDriver::Feature::Generator)`——这是 u3-l1 讲过的能力协商机制，纯频谱仪驱动（如 SSA3000X）会在这里被拦下并弹错误框；能力通过则调用 `updateDevice()` 把当前配置推给设备。这就是「切到 Generator 标签页，设备立刻开始输出」的代码出处。

下发槽与守卫条件：

[Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp:79-86](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L79-L86)

`updateDevice()` 只有两行逻辑：守卫 + 下发。守卫条件是「设备已连接**且**本模式处于活动状态」——`isActive` 是 Mode 基类的保护成员（[Software/PC_Application/LibreVNA-GUI/mode.h:61](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.h#L61)）。这保证后台模式修改设置不会抢夺当前活动模式的设备控制权。真正下发的是 `setSG(central->getDeviceStatus())`：从控件取一份 `SGSettings` 快照，交给驱动。

退出时保存状态：

[Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp:30-38](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L30-L38)

`deactivate()` 把频率和功率写入 `QSettings`（全局持久化，u2-l3 讲过的第一级），再调用 `Mode::deactivate()` 完成基类的标准退出流程（布局保存、设备转 idle）。与构造函数第 15-22 行配合，构成「记住上次输出」功能。

SCPI 远程命令：

[Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp:88-123](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L88-L123)

`setupSCPI()` 在 `GENerator` 命名空间下注册三条命令：`FREQuency`（Hz）、`LVL`（dBm）、`PORT`（从 1 起，0 关闭全部输出）。每条命令都是「命令回调 + 查询回调」成对出现（u10-l1 会展开这个框架）。值得注意的是这些回调**不是直接调驱动**，而是调用 `central->setFrequency()` 等控件槽——远程命令与本地 UI 走完全相同的路径，天然保证两条入口行为一致。`PORT` 命令还检查了端口上限（第 114 行），越界返回 Error。

持久化直接委托：

[Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp:56-67](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L56-L67)

`toJSON`/`fromJSON` 一行转发给控件。这印证了 4.1.1 的判断：模式无自有状态，SaveSetup 保存的工作区内容就是控件的字段。

#### 4.1.4 代码实践

**实践目标**：验证「Generator 模式的全部状态都在控件里」这一论断，并理解模式激活时机。

**操作步骤**（纯源码阅读，无需硬件）：

1. 打开 [generator.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.h)，数一数成员变量：只有一个 `SignalgeneratorWidget *central`。
2. 对比 [VNA/vna.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.h) 的成员列表（Settings 结构、校准、平均器、TraceModel……），感受数量级差异。
3. 在 `generator.cpp` 中搜索 `measurementReceived` 或 `TraceModel`——你会一无所获，确认本模式无上行数据。

**需要观察的现象 / 预期结果**：`Generator` 类没有任何设置字段、没有任何数据接收连接。若后续想给它加功能（比如新的输出参数），改动点几乎必然落在 `SignalgeneratorWidget` 而不是 `Generator` 类。无硬件时本实践为纯阅读，结论可直接从代码结构得出；「激活即输出」的行为**待本地验证**（需真机）。

#### 4.1.5 小练习与答案

**练习 1**：SCPI 命令 `GENerator:PORT 5` 发给一台只有 2 个端口的 LibreVNA，会发生什么？

**答案**：返回 Error。见 [generator.cpp:112-122](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L112-L122)，命令回调里 `newval > Limits.Generator.ports` 时直接返回 `SCPI::Result::Error`，不会触碰控件。

**练习 2**：为什么 `updateDevice()` 要检查 `isActive`？如果不检查，什么场景会出问题？

**答案**：防止后台模式干扰前台。典型场景：用户在 VNA 模式测量时，通过 SCPI 发送 `GENerator:FREQuency 1GHz`——命令回调改的是控件值并触发 `SettingsChanged`。若无 `isActive` 守卫，这台正在做 VNA 扫描的设备会立刻被切到信号源输出，扫描被打断。有守卫时，这次修改只停留在控件里，等用户真正切换到 Generator 模式时才由 `initializeDevice()` → `updateDevice()` 生效。

**练习 3**：`Generator::preset()` 是空实现，而 VNA 的 `preset()` 会重置大量状态。这合理吗？

**答案**：合理但值得推敲。`resetSettings()`（[generator.cpp:49-54](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L49-L54)）承担了实际的重置工作（1 GHz、0 dBm、端口关闭），`preset()` 在 GUI「Preset」菜单路径上目前不做额外的事。模式状态本来就只有三个字段，空实现是极简主义的自然结果。

### 4.2 信号源控件

#### 4.2.1 概念说明

`SignalgeneratorWidget` 是一个「自带全部业务逻辑的 QWidget」：它同时是 UI（.ui 文件描述的表单）、状态容器、限幅器和持久化对象（继承 `Savable`）。

界面上的控件（来自 signalgenwidget.ui）：

| 控件 | 含义 |
|---|---|
| `frequency`（SIUnitEdit） | 中心频率 / CW 输出频率 |
| `levelSpin`（QDoubleSpinBox）+ `levelSlider`（QSlider） | 输出功率，双控件联动 |
| `portBox` 内动态生成的 QCheckBox | 输出端口选择（互斥） |
| `EnabledSweep`（QCheckBox） | 是否启用 GUI 侧扫频 |
| `span` / `steps` / `dwell` / `current`（SIUnitEdit） | 扫频的跨度 / 点数 / 驻留时间 / 当前点 |

「GUI 侧扫频」值得特别解释：LibreVNA 的信号源**没有硬件扫频**。VNA 模式的扫频由 FPGA 自主完成（u6-l2），而 Generator 的「扫频」完全是软件模拟——一个 `QTimer` 每隔 dwell 秒把 `current` 频率向前推一格，每推一格就下发一次新的单点输出配置。设备始终以为自己工作在点频模式。

#### 4.2.2 核心流程

**频率变更的完整处理**（用户在 `frequency` 输入框敲入新值并回车）：

```text
SIUnitEdit 解析文本（回车/失 foco/前缀键触发）
  → emit valueChanged(newval)
  → lambda: 夹取到 [Generator.minFreq, Generator.maxFreq]
  → setValueQuiet 写回限幅后的值（不再发信号）
  → 调整 span 使扫描窗不越界
  → current = frequency - span/2（扫描起点跟随）
  → emit SettingsChanged()
  → Generator::updateDevice() → setSG()
```

**功率路径的防回环**：`levelSpin` 和 `levelSlider` 是同一个值的两种表现形式。`setLevel()` 更新它们时用 `blockSignals` 把两个控件都静音，避免「spin 改了 → 发信号 → 改 slider → slider 又发信号」的死循环，最后统一发一次 `SettingsChanged`。

**GUI 侧扫频的步进**：

\[ f_{next} = f_{current} + \frac{span}{steps} \]

若 \( f_{next} > f_{center} + span/2 \) 则回绕到 \( f_{center} - span/2 \)。驻留时间限制在 0.01–60 秒。

#### 4.2.3 源码精读

频率输入的限幅与联动：

[Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp:35-49](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L35-L49)

用户输入新频率后：第 36-40 行把它夹取到设备 `Limits.Generator` 的 `[minFreq, maxFreq]` 区间（u3-l1 讲过的能力协商数据，静默夹取、不弹窗）；第 41 行用 `setValueQuiet` 写回——注意这里**必须**用 quiet 版本，否则会再次触发本 lambda 造成递归；第 42-45 行收缩 span 保证扫描窗不超出设备上限；第 46-47 行把扫描起点 `current` 挪到 `frequency - span/2`；第 48 行发出唯一的 `SettingsChanged`。整段代码的模式是「先安静地修正所有关联控件，最后发一次信号」。

功率双控件联动：

[Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp:94-97](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L94-L97)

spinbox 的 `valueChanged` 直接连到 `setLevel` 槽；slider 的 `valueChanged`（整数，单位 0.01 dB）经 lambda 除以 100 后也进 `setLevel`。两个控件殊途同归。

`setLevel` 的限幅与防回环：

[Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp:222-238](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L222-L238)

先把功率夹到设备的 `[mindBm, maxdBm]`（第 225-230 行）；然后第 231-236 行对两个控件 `blockSignals(true)` → 写值 → `blockSignals(false)`，这是 Qt 中「多控件表示同一数据」的标准防回环手法；第 237 行统一发 `SettingsChanged()`。在 Qt 中调用信号名即等于 emit。

程序化设置频率——刻意走 UI 信号路径：

[Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp:240-250](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L240-L250)

注意第 249 行用的是 `setValue()`（发信号版本）而不是 `setValueQuiet()`。这意味着 `setFrequency` 会触发 4.2.3 第一段讲的那个 lambda——限幅、span 调整、current 跟随、发 `SettingsChanged` 全部自动复用。SCPI 的 `FREQuency` 命令、JSON 加载、构造函数的初始值设置走的都是这条路。**代价**是 `setFrequency` 本身不直接发信号：若新值与旧值相同，`SIUnitEdit::setValue` 内部的 `if(value != _value)` 判断（[siunitedit.cpp:53-60](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/siunitedit.cpp#L53-L60)）会拦截，什么都不发生。

GUI 侧扫频定时器：

[Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp:122-133](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L122-L133)

`timerEvent` 是 QObject 的定时器回调。仅当 `EnabledSweep` 勾选时，每拍把 `current` 加 `span/steps`，越界回绕，然后 `setValueQuiet` + 手动调用 `SettingsChanged()`（这里直接调用信号函数，与 emit 等价）。设备视角：每拍收到一个 `Generator` 包，频率跳一格——这就是「软件扫频」的全部真相。100 点、1 秒驻留的扫频意味着每秒一次 USB 配置包，与 VNA 模式 FPGA 每秒数千点的硬件扫描完全不是一个量级。

状态快照的组装：

[Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp:135-150](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L135-L150)

`getDeviceStatus()` 是控件对外的唯一读接口：扫频开启时上报 `current`（此刻的实际输出点），否则上报 `frequency`；功率取 spinbox；端口默认 0（**全部关闭**），遍历勾选框，`s.port = i+1`。注意这个循环的写法：若多个框被勾选（正常情况下互斥逻辑会阻止），**最后勾选的胜出**。端口为 0 时设备不输出任何信号——所以新连接设备后必须手动勾选端口才有输出。

设备信息变化时重建端口与量程：

[Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp:185-220](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L185-L220)

`deviceInfoUpdated()` 在设备连接/信息刷新时被 `Generator::deviceInfoUpdated()` 转发调用：删掉旧的端口勾选框，按 `info.Limits.Generator.ports` 重新生成（LibreVNA 是 2 个），并为每个框连接互斥逻辑（勾选一个时屏蔽信号地取消其他框，第 197-208 行）；同时把 spinbox 和 slider 的上下限设为设备的 `[mindBm, maxdBm]`（第 216-219 行，slider 以 0.01 dB 为分辨率所以乘 100）。这保证了 UI 量程永远与实际设备能力同步——不同驱动接入时无需改控件代码。

JSON 持久化：

[Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp:152-183](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L152-L183)

`toJSON` 导出频率、功率、端口和整个 sweep 子对象；`fromJSON` 用 `j.value(key, 默认值)` 的形式读取，缺项时保持当前值——这是 u2-l3 讲过的「声明式缺省回退」在手写代码中的体现，保证旧版本保存的 setup 文件（没有 sweep 字段）仍能加载。

#### 4.2.4 代码实践

**实践目标**：确认「扫频开着时，输出频率取自 `current` 而非 `frequency`」以及互斥端口逻辑。

**操作步骤**：

1. 阅读 [signalgenwidget.cpp:135-150](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L135-L150) 的 `getDeviceStatus()`，回答：`EnabledSweep` 未勾选时 `current` 字段的值参与上报吗？
2. 阅读第 197-208 行的端口互斥 lambda，画出「勾选 Port2 时 Port1 被取消」的信号流向，解释为什么取消其他框时要 `blockSignals(true)`。
3. 有硬件时：设置中心 1 GHz、span 100 MHz、steps 10、dwell 1 s，勾选 Enable sweep 和 Port1，用另一台接收设备（或 LibreVNA 自己的频谱仪模式 + 另一根天线/直连）观察输出每秒跳 10 MHz 一格。

**需要观察的现象**：步骤 3 中输出频率按 950 MHz → 960 MHz → … → 1050 MHz → 回绕的阶梯变化，每格停留 1 秒。

**预期结果**：步骤 1 的答案：不参与，`s.freq` 取 `ui->frequency->value()`。步骤 2：不 `blockSignals` 的话，取消 Port1 会触发它自己的 toggled(false) 信号，又发一次 `SettingsChanged`，造成一次多余的设备重配。步骤 3 的行为**待本地验证**（需硬件与接收手段）。

#### 4.2.5 小练习与答案

**练习 1**：dwell 输入框允许 0.01–60 秒（[signalgenwidget.cpp:83-92](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L83-L92)）。仔细读这段代码，你能发现一个与定时器有关的小瑕疵吗？

**答案**：第 90 行 `m_timerId = startTimer(newval*1000)` 直接覆盖了成员变量，但**从未调用 `killTimer` 杀掉旧定时器**。每次修改 dwell 都会新建一个定时器，旧定时器继续以旧周期触发 `timerEvent`，只是被第 124 行的 `event->timerId() == m_timerId` 过滤掉。行为上正确（只有最新定时器生效），但旧定时器会一直空转产生被丢弃的事件，属于轻微的资源泄漏。构造函数第 29 行的 `startTimer(1000)` 同理。

**练习 2**：为什么 `getDeviceStatus()` 里端口循环写成 `if(portCheckboxes[i]->isChecked()) s.port = i+1;` 而不是 checked 就 break？

**答案**：写成 if 不 break，语义是「最后一个勾选的框获胜」。由于 `deviceInfoUpdated()` 里的互斥逻辑保证同时最多一个框被勾选，正常情况下循环最多命中一次，两种写法等价。但若互斥逻辑被绕过（例如程序化 `setChecked` 未走 toggled 信号路径），这个写法让最后勾选者获胜，行为确定。同时注意 `port == 0`（无勾选）在协议里表示关闭所有输出，`setPort(0)`（第 257-260 行）就是靠全部取消勾选实现的。

**练习 3**：`fromJSON` 里为什么用 `setFrequency(j.value(...))` 而不是直接 `ui->frequency->setValue(...)`？

**答案**：`setFrequency` 内部走 `setValue()` → `valueChanged` → 构造函数里的限幅 lambda → `SettingsChanged`。这保证了加载 setup 文件时：一是数值被夹取到当前设备量程内（文件可能来自另一台能力不同的设备），二是加载完成后设备自动收到一次新配置，无需额外代码。代价是链路变长、行为不那么直观。

### 4.3 下发链路

#### 4.3.1 概念说明

下发链路要回答的问题是：一次 UI 操作如何变成设备上的射频输出。这条链共六站：

```text
QSlider/QDoubleSpinBox/SIUnitEdit（UI 控件）
  → SignalgeneratorWidget 限幅与联动（setLevel/setFrequency lambda）
  → SettingsChanged 信号（控件的统一出口）
  → Generator::updateDevice()（模式层守卫：连接 + isActive）
  → DeviceDriver::setSG(SGSettings)（硬件无关接口，虚函数）
  → LibreVNADriver::setSG（翻译成 Protocol::GeneratorSettings 包）
  → LibreVNAUSBDriver::SendPacket（编码 + libusb 批量传输）
  → 设备回 Ack → transmissionFinished 取下一个排队包
```

其中前三站在 GUI 线程同步完成；`SendPacket` 之后进入 u3-l2 讲过的「单包在途」排队机制。

#### 4.3.2 核心流程

**数据结构的逐层变换**——同一个「输出配置」在三站有三种形态：

| 层 | 类型 | 频率 | 功率 | 端口 |
|---|---|---|---|---|
| 控件层 | UI 控件值 | `double` Hz | `double` dBm | 勾选框组 |
| 驱动接口层 | `DeviceDriver::SGSettings` | `double freq` | `double dBm` | `int port`（0=关） |
| 协议层 | `Protocol::GeneratorSettings` | `uint64_t` Hz | `int16_t cdbm` | `uint8_t activePort:3` |

单位换算只有一处：`cdbm_level = dBm * 100`（厘 dBm），把浮点 dBm 变成 16 位整数。频率在协议里是 64 位整数赫兹，`double` 的 53 位尾数在 6 GHz 量级仍精确到赫兹以下，无损。

**端到端时序**（用户拖动功率滑块一格）：

```text
t0   slider valueChanged(107)                [GUI 线程]
t0   setLevel(1.07) → 夹幅 → blockSignals 写两控件 → SettingsChanged
t0   Generator::updateDevice() → 守卫通过
t0   getDeviceStatus() 组装 {freq, 1.07dBm, port=1}
t0   LibreVNADriver::setSG → 填 PacketInfo{Generator} → lastNonIdlePacket=p
t0   USBDriver::SendPacket → 入队 transmissionQueue → startNextTransmission
t0   EncodePacket → libusb_bulk_transfer(EP_Out)     [离开 GUI 视野]
t1   设备固件 Communication 收包 → Generator::Setup → HW::SetOutput...
t2   设备回 Ack → transmissionFinished → 队列为空 → transmissionActive=false
```

从 t0 到 USB 提交全部在同一个函数调用栈里同步完成；之后靠 Ack 事件驱动。

#### 4.3.3 源码精读

硬件无关的数据契约：

[Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h:425-448](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L425-L448)

`SGSettings` 只有三个字段：频率、dBm、端口（注释明确约定「端口从 1 起，0 关闭全部输出」）。下面第 448 行是 `setSG` 虚函数，默认实现返回 false——这是 u3-l1 讲过的「不支持」表达：不实现信号源能力的驱动（如纯频谱仪 SSA3000X）不必写任何代码，天然返回失败。

驱动侧的翻译：

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp:600-611](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L600-L611)

`LibreVNADriver::setSG` 把 `SGSettings` 逐字段填进 `Protocol::PacketInfo`：包类型 `Generator`（枚举值 12，见 [Protocol.hpp:586](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L586)）；`cdbm_level = s.dBm * 100` 完成唯一的单位换算；`activePort` 原样传递（0 即无输出）；`applyAmplitudeCorrection = true` 让设备应用 u5-l3 讲过的板级源幅度校准表。第 609 行 `lastNonIdlePacket = p` 记住这份配置，供 u4-l3 讲过的「切参考源时暂停-恢复」机制使用。最后 `SendPacket(p)`——这是 `LibreVNADriver` 的纯虚函数（[librevnadriver.h:180](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.h#L180)），由 USB/TCP 子类各自实现字节收发（u3-l2 的两层驱动结构）。

协议帧的线上格式：

[Software/VNA_embedded/Application/Communication/Protocol.hpp:192-198](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L192-L198)

`GeneratorSettings` 总共 12 字节：64 位频率 + 16 位 cdbm + 一个位域字节（3 位端口 + 1 位幅度校准使能 + 4 位保留）。这个结构体被 GUI 和固件共同编译（u1-l3 讲过的同源编译机制），两端不可能对格式产生分歧。加上 u4-l1 讲过的五段式帧头（0x5A + 长度 + 类型 + payload + CRC32），一整个 `Generator` 包线上约 20 字节。

USB 出口与排队：

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp:264-277](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L264-L277)

`SendPacket` 把包连同超时和回调封装成 `Transmission` 入队；若当前没有在途包则立即启动下一次传输。因为 Ack 不带序号（u4-l2），同一时刻只允许一个包在途——这是理解下面防抖问题的钥匙。

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp:228-262](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L228-L262)

`transmissionFinished` 在收到 Ack/超时后被调用：出队刚完成的包、执行回调，然后循环尝试发送队列中的下一个包，直到队列空则清 `transmissionActive` 标志。注意队列是**先进先出、不合并**的——排队期间的每一个中间配置都会被真正发送。

#### 4.3.4 为什么连续拖动滑块需要防抖或合并更新

这是本讲实践任务的核心问题，代码给出的答案比想象中有趣：**Generator 模式其实没有防抖，而 VNA 模式有**。把两者对照着读就能看懂设计取舍。

VNA 模式的防抖：

[Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp:82-85](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L82-L85) 与 [Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp:1098-1113](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1098-L1113)

VNA 的 `SettingsChanged` 不直接配置设备，而是 `configurationTimer.start(delay)` 启动一个单次定时器（默认 delay=100ms，见 [vna.h:137](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.h#L137) 的默认参数）。100ms 内的连续变更不断重置定时器，只有停下来 100ms 后才真正下发**最终值**。这就是教科书式的防抖（debounce）。

为什么 VNA 必须这么做：

1. **每次重配代价高昂**。一次 VNA 配置变更会让设备中止当前扫描、重启扫描，并 `ResetLiveTraces()` 清空活迹线（第 1110-1112 行）、打断平均进程。拖动滑块产生 50 次中间值就意味着 50 次扫描重启，测量永远无法完成。
2. **中间值毫无意义**。用户拖滑块只关心松手时的最终功率，途经值是噪声。
3. **USB 往返有限**。单包在途 + 每包一个 Ack 往返，突发 50 个包要排队串行发出，纯属浪费带宽。

而 Generator 模式选择直连（[generator.cpp:27](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L27)），它能承受的理由：

1. **包极小且幂等**。整个 `Generator` 包约 20 字节，配置不依赖先前状态，发一百次中间值除了占用一点 USB 带宽没有任何副作用——没有会被打断的扫描，没有会被清空的数据。
2. **控件层已经天然过滤了大部分突发**。`SIUnitEdit` 只在回车、失焦、敲前缀键时解析提交（[siunitedit.cpp:19-21](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/siunitedit.cpp#L19-L21)、[siunitedit.cpp:107-109](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/siunitedit.cpp#L107-L109)），敲 `3.5G` 不会逐键触发；滚轮（第 114-162 行）和功率滑块（每 0.01 dB 一档）才是真正的突发源。
3. **驱动队列兜底**。单包在途的 `transmissionQueue` 把突发串行化，GUI 线程从不会因等待 Ack 而阻塞（`SendPacket` 入队即返回）。

所以准确的结论是：**防抖的必要性正比于「一次多余下发的代价」**。VNA 的一次多余下发会毁掉正在进行的测量，必须防抖；Generator 的一次多余下发几乎无代价，直连反而更简单、响应更快（改完立刻生效，不用等 100ms）。这是同一个代码库里对同一种问题（UI 突发变更）的两种刻意不同的答案。

至于「合并更新」（coalescing，只保留最新值丢弃中间值）：当前 `transmissionQueue` 并未实现——队列 FIFO 发送所有中间包。如果要做到极致，可以在入队时丢弃队列中尚未发送的同类型 `Generator` 包、只留最新一个；对这个场景是安全的，因为 Generator 配置是全量快照而非增量。

#### 4.3.5 代码实践

**实践目标**：从功率滑块出发，手工追踪到 USB 批量传输，写出完整调用栈；并用 VNA 对照解释防抖取舍。

**操作步骤**（纯源码阅读，无需硬件即可完成 1-4 步）：

1. 从 [signalgenwidget.cpp:95-97](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L95-L97) 的 slider lambda 开始，沿函数调用逐行走到 `libusb_bulk_transfer`，把每一站的「文件:函数:行号」抄成一张表。
2. 在每一站标注：这一步做了什么变换（int→double？double→cdbm？结构体→字节流？），以及发生在哪个线程。
3. 打开 [vna.cpp:1098-1113](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1098-L1113)，对比两份 `SettingsChanged` 的去向，用自己的话写 3-5 句结论。
4. （可选，需硬件 + 编译环境）在 `Generator::updateDevice()` 里临时加一行 `qDebug() << "setSG" << s.freq << s.dBm << s.port;`，快速拖动功率滑块，观察一行日志对应一格滑块移动——验证「无防抖、每格一次下发」。

**需要观察的现象**：步骤 4 中拖动滑块经过 N 格，日志精确出现 N 行 setSG（其中部分可能因人眼/事件合并略有出入，但量级一致）。

**预期结果**：步骤 1-2 的参考调用栈（应与你抄写的一致）：

```text
1  QSlider::valueChanged(int)                          [signalgenwidget.cpp:95]
2  lambda → setLevel(value/100.0)                      [signalgenwidget.cpp:96-97]
3  SignalgeneratorWidget::setLevel(double)             [signalgenwidget.cpp:222]
     夹幅 [mindBm,maxdBm] → blockSignals 写 spin+slider → SettingsChanged()
4  SignalgeneratorWidget::SettingsChanged 信号          [signalgenwidget.h:27]
5  Generator::updateDevice()                           [generator.cpp:79]  (直连,generator.cpp:27)
     守卫: getDevice()!=null && isActive
6  SignalgeneratorWidget::getDeviceStatus()            [signalgenwidget.cpp:135]
     组装 DeviceDriver::SGSettings{freq,dBm,port}
7  DeviceDriver::setSG(SGSettings)  ←虚函数分发         [devicedriver.h:448]
8  LibreVNADriver::setSG                               [librevnadriver.cpp:600]
     填 PacketInfo{type=Generator, cdbm=dBm*100, activePort}
     lastNonIdlePacket=p → SendPacket(p)
9  LibreVNAUSBDriver::SendPacket                       [librevnausbdriver.cpp:264]
     入队 transmissionQueue → startNextTransmission()
10 LibreVNAUSBDriver::startNextTransmission            [librevnausbdriver.cpp:361]
     Protocol::EncodePacket → libusb_bulk_transfer(EP_Data_Out)
----- 线程边界：以上同步于 GUI 线程；以下由 libusb 事件线程驱动 -----
11 设备回 Ack → transmissionFinished                  [librevnausbdriver.cpp:228]
     出队 → 下一个包或置 transmissionActive=false
```

步骤 3 的参考结论：VNA 把 SettingsChanged 导入 100ms 单次定时器实现防抖，因为每次重配会重启扫描、清空迹线、打断平均，中间值代价极高；Generator 直连不防抖，因为约 20 字节的幂等配置包几乎无代价，且 SIUnitEdit 只在提交时发信号、驱动队列串行化兜底。防抖的必要性正比于单次多余下发的代价。

步骤 4 **待本地验证**（需硬件）。

#### 4.3.6 小练习与答案

**练习 1**：Generator 模式与 VNA 零扫宽（点频）模式都能让设备在单一频率上连续输出。它们的本质区别是什么？

**答案**：三条。①**数据方向**：VNA 零扫宽仍是完整测量回路——接收链全开，设备按时间点上报 `VNAMeasurement`（X 轴变为时间），GUI 侧有 Trace、图、marker；Generator 只有下行配置，无任何上行数据。②**设备端路径**：VNA 零扫宽走固件 `VNA::Setup` 的扫描机制（只是单点，u5-l4 讲过零扫宽与普通扫描的分叉只在 `MeasurementDone` 填包字段）；Generator 走 `Generator::Setup`，固件用 FPGA 硬件覆盖长期钉住射频控制线，无测量中断。③**输出端口语义**：Generator 可显式选端口（含全关），VNA 的输出端口由测量设置决定。

**练习 2**：如果不修改 `Generator`/`SignalgeneratorWidget`，只改驱动，能给 Generator 模式加上硬件扫频吗？

**答案**：不能。GUI 侧的「扫频」是 `timerEvent` 逐点下发单点配置实现的（[signalgenwidget.cpp:122-133](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L122-L133)），span/steps/dwell 这些概念根本不会传给设备——`SGSettings` 只有 freq/dBm/port 三个字段。硬件扫频需要扩展 `DeviceDriver::SGSettings` 数据契约、`setSG` 语义和协议 `GeneratorSettings` 帧，属于跨 GUI/固件两端的接口变更。

**练习 3**：`setLevel` 里如果忘了 `blockSignals`，会发生什么？

**答案**：`ui->levelSpin->setValue(level)` 会触发 `levelSpin` 的 `valueChanged(double)`，它直连 `setLevel`（[signalgenwidget.cpp:94](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L94)），形成 `setLevel → setValue → valueChanged → setLevel` 的递归。实际上递归会在数值收敛后停止（`SIUnitEdit`/spinbox 的 setValue 对相同值不再发信号，且两控件写的都是同一个夹幅后的值），因此大概率表现为多余的几次重复调用而非死循环；但每次递归都发 `SettingsChanged`，意味着一次拖动产生指数级/多倍的设备配置包。这正是 Qt 多控件同步必须配合 `blockSignals` 的原因。

## 5. 综合实践

**任务：给 Generator 模式画一张「一次功率修改的全链路时序图」，并为它写一份防抖改进方案。**

这个任务把本讲三个模块串起来：

1. **画时序图**（覆盖 4.1/4.2/4.3）：横轴为时间，纵轴为参与方（用户、levelSlider、SignalgeneratorWidget、Generator 模式、LibreVNADriver、USB 队列、设备）。画出「拖动滑块 5 格」的完整消息序列：5 次 `valueChanged`、5 次 `setLevel`（含夹幅与 blockSignals）、5 个 `Generator` 包入队、逐个 Ack 出队。标注每条消息旁的文件:行号。
2. **写改进方案**（覆盖 4.3.4）：假设要求「快速拖动时设备只收到最终值」，设计一个最小改动。提示两个方向任选其一：
   - 方向 A（模式层防抖）：仿照 VNA 的 `configurationTimer`，在 `Generator` 类里加一个 `QTimer` 单次定时器，把 `updateDevice()` 改为重启定时器、超时再下发。写出需要改动的构造函数连接代码与新增槽的伪代码。
   - 方向 B（驱动层合并）：在 `LibreVNADriver::setSG` 入队前检查 `transmissionQueue` 中是否已有未发送的 `Generator` 类型包，若有则替换而非追加。说明为什么对 `Generator` 包安全（全量快照、无序号依赖）、对 `SweepSettings` 包不安全（配置变更需设备确认重启扫描）。
3. **验证设计**：无硬件时对照源码逐行检查方案是否会破坏三个既有行为——SCPI 命令的即时生效、`initializeDevice()` 的首次下发、`lastNonIdlePacket` 的记录。有硬件时实现方向 A 并用 4.3.5 步骤 4 的 qDebug 方法对比改进前后的日志行数。

**预期结果**：时序图能清楚展示「5 次入队、逐个发送」与「GUI 线程同步完成到 USB 提交」两个关键事实；方案能说明防抖层放在哪一层各有什么代价（模式层简单但所有驱动受益、驱动层精准但要小心包类型判断）。改进后的行为**待本地验证**（需硬件）。

## 6. 本讲小结

- **Generator 是「哑模式 + 智能控件」**：`Generator` 类自身零状态，全部输出配置（频率/功率/端口/扫频参数）住在 `SignalgeneratorWidget`，模式只做信号接线和 SCPI/JSON 转发。
- **单信号主动脉**：控件所有变更收敛到唯一的 `SettingsChanged` 信号，直连 `Generator::updateDevice()`，经 `isActive` 守卫后调 `DeviceDriver::setSG()`。
- **数据三级形态**：UI 控件值 → `DeviceDriver::SGSettings`（double、硬件无关）→ `Protocol::GeneratorSettings`（12 字节，cdbm=dBm×100，uint64 Hz），最后一站由 USB 子类编码进五段式帧批量传输。
- **「扫频」是软件模拟**：`timerEvent` 按 dwell 秒把当前频率推进 span/steps，每步就是一次普通的单点 `setSG`，设备始终工作在点频模式。
- **防抖的取舍**：VNA 用 100ms 单次定时器防抖（重配会重启扫描、清迹线，代价高）；Generator 直连不防抖（约 20 字节幂等包近乎无代价，`SIUnitEdit` 提交式发信号 + 驱动单包在途队列兜底）。防抖必要性正比于单次多余下发的代价。
- **与 VNA 零扫宽的区别**：零扫宽是「输出 + 完整测量回路 + 按时间上报」；Generator 是「纯输出、无上行数据、固件用硬件覆盖钉住射频控制线」。

## 7. 下一步学习建议

- **下一讲 u7-l4（平均、数据分级与流式输出）**会补上本讲故意跳过的上行方向——Generator 没有数据上报，而平均与流式正是 VNA/SA 两模式数据汇合后的处理。
- 想深入「防抖与配置调度」的对照，重读 [vna.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp) 中 `SettingsChanged` → `configurationTimer` → `ConfigureDevice` 的完整路径，注意 `resetTraces` 与 `changingSettings` 两个标志的用法。
- 想看设备端如何执行本讲下发的包，回到 u5-l4 的固件 `Generator.cpp`（`Software/VNA_embedded/Application/Generator.cpp`），对照 FPGA 硬件覆盖机制理解「无测量回路的输出」在固件侧意味着什么。
- 远程控制 Generator（`GENerator:FREQuency/LVL/PORT`）的命令树细节将在 u10-l1/u10-l2 的 SCPI 框架与 TCP 集成两讲展开，届时可用 `nc` 连上 SCPI 端口实操本讲见到的三条命令。
