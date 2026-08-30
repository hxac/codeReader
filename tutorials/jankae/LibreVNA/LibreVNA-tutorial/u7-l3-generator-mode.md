# 信号发生器模式

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 Generator 模式作为 `Mode` 子类的「最小实现」到底小在哪里——它没有 Trace、没有平均、没有校准，只有一块控件加一条下发链路。
2. 解释 `SignalgeneratorWidget` 如何充当「当前输出配置」的唯一事实来源，以及 `getDeviceStatus()` 如何把界面状态聚合成 `DeviceDriver::SGSettings`。
3. 完整跟踪一次频率/功率修改：从 UI 事件 → `SettingsChanged` 信号 → `Generator::updateDevice()` → `DeviceDriver::setSG()` → 协议包 → USB 批量传输 → 固件 `Generator::Setup()`。
4. 说明为什么连续拖动电平滑块会产生一串协议包、驱动的「单包在途」队列如何兜底，以及为什么这里本应像 VNA 模式那样做防抖/合并。
5. 对比 Generator 模式与 VNA 模式的零扫宽（点频）模式：同样是「固定频率输出」，两者的数据流向完全相反。

## 2. 前置知识

本讲默认你已读过 u7-l1（VNA 模式）。这里补充几个本讲用到的概念：

- **连续波（CW，Continuous Wave）**：幅度和频率都不随时间调制的单频正弦信号。信号发生器模式输出的就是 CW——它只「发」不「收」。
- **dBm 与 cdbm**：dBm 是以 1 毫瓦为基准的功率对数单位，\( P_{\mathrm{dBm}} = 10\lg\dfrac{P}{1\,\mathrm{mW}} \)。协议里为了用整数传输，把 dBm 放大 100 倍存成 `int16_t`，单位记作 cdbm：\[ \text{cdbm} = \text{dBm} \times 100 \] 例如 −10 dBm 存为 −1000。
- **`Mode` 基类的两条钩子**（u2-l2 已讲）：模式被激活时 `Mode::activate()` 末尾会调用 `initializeDevice()`；被停用时 `Mode::deactivate()` 末尾会对设备 `setIdle()`。Generator 模式的「开机/关机」行为就挂在这两条钩子上。
- **单包在途（one packet in flight）**：u3-l2 讲过，官方驱动因为 Ack 不带序号，发送队列一次只允许一个包在途，收到 Ack 才发下一个。这个机制本讲会再次用到，它是理解「滑块洪泛」问题的关键。
- **防抖（debounce）**：一个会连续触发的事件（如拖动滑块）本不该每次都执行昂贵操作；常用手法是「事件只启动一个单次定时器，定时器到期才真正执行」，期间的新事件不断重置定时器，从而把一串事件合并成最后一次。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp/.h` | Generator 模式类：把 `SignalgeneratorWidget` 包装成一种 `Mode`，负责生命周期、SCPI、JSON 持久化、向设备下发 |
| `Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp/.h` | 信号源控件：频率/电平/端口/软扫频的全部界面状态与信号 |
| `Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.ui` | Qt Designer 界面文件：定义 `frequency`、`levelSpin`、`levelSlider`、`portBox`、`EnabledSweep` 等控件 |
| `Software/PC_Application/LibreVNA-GUI/mode.cpp`、`modehandler.cpp` | `Mode` 基类的 activate/deactivate 钩子；工厂 `createNew` 创建 Generator 实例 |
| `Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h/.cpp` | `SGSettings` 结构、`setSG()`/`setIdle()` 虚接口、`Info::Limits.Generator` 能力限制与默认值 |
| `Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp` | 官方驱动的 `setSG()`：把 `SGSettings` 翻译成协议包 |
| `Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp` | `SendPacket` 入队、`startNextTransmission` 单包在途发送 |
| `Software/VNA_embedded/Application/Communication/Protocol.hpp` | `GeneratorSettings` 包格式定义（GUI 与固件同源编译） |
| `Software/VNA_embedded/Application/Generator.cpp` | 固件端：收到 Generator 包后如何真正点亮射频链路（u5-l4 已讲，本讲只取几个事实做对照） |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**Generator 模式状态**、**信号源控件**、**下发链路**。

### 4.1 Generator 模式状态：一个 Mode 子类的最小实现

#### 4.1.1 概念说明

u2-l2 讲过，任何模式都必须实现 `Mode` 的若干纯虚函数。VNA 和频谱仪模式各自实现了厚厚一沓设置结构和数据上行链路；而 Generator 是三种内置模式里最简单的一个——它的全部「模式状态」就是中央控件 `SignalgeneratorWidget` 里那几个编辑框的值，模式类本身不保存任何数据。

这是一个值得记住的设计取向：**当状态足够简单时，让控件自己当唯一事实来源，模式类只做胶水**。对比 VNA 模式专门维护一个 `Settings` 结构体（u7-l1），Generator 模式的 `toJSON()` 直接转发给控件就是明证。

#### 4.1.2 核心流程

Generator 模式的生命周期可以画成：

```text
ModeHandler::createNew(Type::SG)
        │  new Generator(window, name)
        ▼
Generator 构造函数
        │  1. 创建 SignalgeneratorWidget 作为 central
        │  2. 从 QSettings/Preferences 恢复上次频率与电平
        │  3. setupSCPI() 挂 3 条 SCPI 命令
        │  4. finalize(central) 注册进中央 QStackedWidget
        │  5. connect(SettingsChanged → updateDevice)
        ▼
……（用户切换到该模式）
Mode::activate()
        └─ 末尾调用 Generator::initializeDevice()
                ├─ 不支持 Generator 特性 → 弹错误框，返回
                └─ updateDevice() → 首次下发当前配置
……（用户切走）
Generator::deactivate()
        ├─ 把频率/电平写入 QSettings（供下次启动恢复）
        └─ Mode::deactivate() → 设备 setIdle()，停止输出
```

任何一次界面改动则走最短路径：

```text
控件值变化 → SettingsChanged 信号 → Generator::updateDevice()
           → 设备已连接且模式激活？ → setSG(当前配置)
```

#### 4.1.3 源码精读

**工厂入口**。三种模式由同一个 switch 分发创建，Generator 对应 `Type::SG`：

- [modehandler.cpp:38-46](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/modehandler.cpp#L38-L46)：`createNew` 按 `Mode::Type` 分发，`Type::SG` 分支 `new Generator(aw, name)`。

**构造函数**。整段只有 20 行，做完了上文流程图的 5 件事：

- [generator.cpp:7-28](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L7-L28)：构造函数。注意 [第 10 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L10) 创建 `SignalgeneratorWidget`，把 `AppWindow` 指针一路传下去（控件随后要用它查询设备能力）；[第 15-22 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L15-L22) 决定初始值：偏好设置勾选了 `RememberSweepSettings` 就读 QSettings 里上次退出时保存的值，否则用 `pref.Startup.Generator` 的默认值；[第 27 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L27) 是整个模式的「神经中枢」——控件的 `SettingsChanged` 信号直连 `updateDevice` 槽。

**两条生命周期钩子**：

- [generator.cpp:40-47](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L40-L47)：`initializeDevice()`。激活模式时由 `Mode::activate()` 调用（见 [mode.cpp:83-85](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L83-L85)）。先做能力协商：设备不支持 `Feature::Generator` 就弹错误框并直接返回（u3-l1 讲过的优雅降级）；支持则调用 `updateDevice()` 把当前界面配置立即下发——这就是「切到信号源模式，输出立刻按界面设置点亮」的原因。
- [generator.cpp:30-38](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L30-L38)：`deactivate()`。先把当前频率/电平存进 QSettings，再调用 `Mode::deactivate()`；后者末尾会对设备 `setIdle()`（[mode.cpp:117-119](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L117-L119)），停止所有信号输出。也就是说：**只要切走模式，输出必然关闭**，界面值只是被记住，不会被偷偷保持。

**下发函数**。全模式唯一的设备写入口：

- [generator.cpp:79-86](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L79-L86)：`updateDevice()`。两个守卫：未连接设备、或模式未激活（`isActive != true`）就直接返回；否则一行 `window->getDevice()->setSG(central->getDeviceStatus())` 完成下发。注意这里拿到的设备指针类型是 `DeviceDriver*`——模式层完全不知道背后是 USB 还是 TCP、是 LibreVNA 还是第三方仪器（u3-l1 的依赖倒置）。

**其余接口都是「一行转发」**，正好印证「最小实现」：

- [generator.cpp:56-67](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L56-L67)：`toJSON()/fromJSON()` 直接委托给 `central`（控件自己实现 `Savable`，见 4.2）。
- [generator.cpp:74-77](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L74-L77)：`deviceInfoUpdated()` 转发调用控件同名函数（设备信息变化时重建端口复选框、刷新电平上下限，见 4.2.3）。
- [generator.cpp:69-72](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L69-L72)：`preset()` 是空函数——信号源没有需要预设的复杂状态。

**SCPI 面**。三条命令对应三个可设置项，全部通过改控件来生效（改控件 → `SettingsChanged` → `updateDevice`，复用同一条链路）：

- [generator.cpp:88-123](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L88-L123)：`setupSCPI()`。`GENerator:FREQuency`（[90-100 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L90-L100)）、`GENerator:LVL`（[101-111 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L101-L111)）、`GENerator:PORT`（[112-122 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L112-L122)）。每条都带设置回调和查询回调；`PORT` 在设置侧额外校验不超过 `Limits.Generator.ports`。前缀 `GENerator` 来自 `Mode` 构造时传入的 SCPI 节点名（[generator.cpp:8](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L8)）。

#### 4.1.4 代码实践

**实践：验证「模式切换 = 输出开关」**

1. **实践目标**：确认 Generator 模式的输出生命周期完全由模式激活/停用驱动，并理解 QSettings 记忆机制。
2. **操作步骤**：
   - 有硬件：连接设备，切到 Signal Generator 模式，设一个频率（如 1 GHz）和电平，观察设备状态栏/输出口；再切到 VNA 模式再切回来，观察输出是否被关闭又恢复。
   - 无硬件（源码阅读型实践）：对照 [mode.cpp:43-88](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L43-L88) 的 `activate()` 和 [mode.cpp:90-121](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L90-L121) 的 `deactivate()`，在纸上列出「切入模式」和「切出模式」各自触发的 Generator 类函数（提示：切入触发 `activate → initializeDevice → updateDevice → setSG`；切出触发 `deactivate（存 QSettings）→ setIdle`）。
3. **需要观察的现象**（有硬件时）：切入模式瞬间输出出现在界面设定值上；切出模式后输出消失；重新切入时输出值与上次退出时一致（来自 QSettings，见 [generator.cpp:15-18](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L15-L18) 与 [generator.cpp:32-36](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L32-L36)）。
4. **预期结果**：两种路径都能得出「Generator 没有自己的状态机，它的『状态』就是控件值 + 模式激活标志 `isActive`」这一结论。硬件行为待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`Generator::updateDevice()` 里的条件写作 `isActive != true`，而 `Mode` 基类里有现成的 `isActive` 布尔成员。为什么这个守卫是必要的？去掉会发生什么？

<details><summary>参考答案</summary>

守卫保证只有当前活动模式才能操作设备。`SettingsChanged` 在构造、加载 setup（`fromJSON`）、SCPI 命令等场景都会触发，而这些时刻模式可能并未激活（例如 GUI 启动时加载默认 setup）。若去掉守卫，一个未激活的模式会抢着对设备下发 Generator 包，与当前活动模式（如 VNA 扫描）的配置互相覆盖，设备会在两套配置间来回震荡。

</details>

**练习 2**：SCPI 命令 `GENerator:PORT` 的设置回调为什么要把参数与 `Limits.Generator.ports` 比较，而 `GENerator:FREQuency` 却没有做类似比较？

<details><summary>参考答案</summary>

频率和电平的钳制放在了控件层：`setFrequency`/`setLevel` 内部会按 `Limits.Generator.minFreq/maxFreq`、`mindBm/maxdBm` 夹取（见 4.2.3），所以 SCPI 侧无需重复。而端口没有经过控件的 `setPort` 钳制路径中的范围检查语义（`setPort` 只检查复选框数量并可能直接返回），SCPI 回调便自己做了上界校验，超出返回 `Error`。这是「钳制放在哪一层」的不一致示例——两种做法都能工作，但读者写新代码时应统一策略。

</details>

### 4.2 信号源控件：SignalgeneratorWidget

#### 4.2.1 概念说明

`SignalgeneratorWidget` 是信号源模式的全部界面，也是「当前输出配置」的唯一事实来源。它继承 `QWidget` 与 `Savable`（u2-l3 讲过的 toJSON/fromJSON 契约），对外只暴露三样东西：

1. 信号 `SettingsChanged()`——「界面上的输出配置变了」；
2. 方法 `getDeviceStatus()`——把界面状态聚合成一个 `DeviceDriver::SGSettings`；
3. 三个公共槽 `setFrequency/setLevel/setPort`——供模式类、SCPI、JSON 加载反向写入。

界面由 `.ui` 文件定义，控件分四组：

| 分组（GroupBox 标题） | 控件 | 含义 |
|---|---|---|
| Frequency | `frequency`（SIUnitEdit） | 中心频率（也是不扫频时的输出频率） |
| Level Control | `levelSpin`（QDoubleSpinBox）＋ `levelSlider`（QSlider） | 输出电平（dBm），两控件联动 |
| Enable | `portBox` 内动态生成的 `Port 1`、`Port 2`… 复选框 | 选择输出端口，互斥 |
| Sweep | `EnabledSweep`（QCheckBox）、`span`、`steps`、`dwell`、`current` | GUI 级步进软扫频 |

控件定义见 [signalgenwidget.ui:73-214](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.ui#L73-L214)。

这里有一个容易误解的点要提前澄清：**Sweep 组不是设备的硬件扫频**。VNA 模式把整条扫描曲线预编程进 FPGA 自主执行（u5-l4、u6-l2）；而信号源的「扫频」只是一个 Qt 定时器：每隔 `dwell` 秒把输出频率挪一格。设备每次收到的仍然是单频点 Generator 包。

#### 4.2.2 核心流程

界面状态到设备配置的聚合规则（`getDeviceStatus`）：

```text
SGSettings:
    freq ← EnabledSweep 勾选 ? current : frequency
    dBm  ← levelSpin 的值
    port ← 默认 0（全部关闭）
           遍历端口复选框，勾选的第 i 个 → port = i+1
```

步进扫频的推进算法（`timerEvent`，每 `dwell` 秒一次）：

\[ f_{k+1} = f_k + \frac{\text{span}}{\text{steps}}, \qquad \text{若 } f_{k+1} > f_{\text{center}} + \frac{\text{span}}{2} \text{ 则回绕到 } f_{\text{center}} - \frac{\text{span}}{2} \]

即从 `frequency − span/2` 出发，以 `span/steps` 为步长走 `steps` 步扫完整个 span，然后回绕循环。

频率与 span 的互锁（防止扫频范围越过设备频率限制）：

- 用户改 `frequency`：先把值夹进 `[minFreq, maxFreq]`；若 `frequency − span/2 < 0` 则把 span 缩小；若 `frequency + span/2 > maxFreq` 也把 span 缩小；最后刷新 `current` 显示。
- 用户改 `span`：夹进 `[0, maxFreq − minFreq]`；若下边沿 `< 0` 则抬高中心频率；若上边沿 `> maxFreq` 则压低中心频率。

#### 4.2.3 源码精读

**三个槽是「外部写入」的唯一入口**：

- [signalgenwidget.cpp:240-250](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L240-L250)：`setFrequency()`。夹取到设备频率范围后调用 `ui->frequency->setValue(...)`。注意它用的是 `setValue` 而不是 `setValueQuiet`——`SIUnitEdit::setValue` 在值变化时会发出 `valueChanged` 信号（[siunitedit.cpp:53-60](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/siunitedit.cpp#L53-L60)），于是构造函数里挂的钳制/互锁 lambda 会被再次触发，并最终 `emit SettingsChanged()`。也就是说 `setFrequency` 的通知是「借道」编辑框信号完成的。
- [signalgenwidget.cpp:222-238](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L222-L238)：`setLevel()`。夹取到 `[mindBm, maxdBm]` 后，先对 spinbox 和滑块 `blockSignals(true)`，分别设值，再解除阻塞，最后手动调用 `SettingsChanged()`。这里的 `blockSignals` 是防止「设 spinbox 触发它的 valueChanged → 又调 setLevel」的回环；手动补一次 `SettingsChanged` 则保证通知不丢。对比 `setFrequency` 的「借道信号」，这是同一问题的两种解法。
- [signalgenwidget.cpp:252-264](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L252-L264)：`setPort()`。`port == 0` 全部取消勾选（协议语义：0 = 关闭所有端口）；否则勾选第 `port−1` 个复选框——勾选动作触发复选框的 `toggled` lambda，完成互斥并通知。

**状态聚合**：

- [signalgenwidget.cpp:135-150](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L135-L150)：`getDeviceStatus()`。按 4.2.2 的规则填 `SGSettings`。`SGSettings` 本体只有三个字段（[devicedriver.h:425-433](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L425-L433)）：`freq`（Hz）、`dBm`、`port`（从 1 起，0 表示全关）。扫频参数（span/steps/dwell）**不进入** `SGSettings`——它们是纯 GUI 概念，设备永远只看到单频点。

**频率编辑框的钳制与互锁**：

- [signalgenwidget.cpp:35-49](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L35-L49)：`frequency` 的 `valueChanged` lambda。`SIUnitEdit` 在用户滚轮/方向键步进时每一步都会调用 `setValue` 并发出一次 `valueChanged`（步进路径见 [siunitedit.cpp:146-160](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/siunitedit.cpp#L146-L160)），所以连续操作会连续触发本 lambda，末尾的 `emit SettingsChanged()` 也会连发——这是 4.3 讨论防抖的源头之一。写回用 `setValueQuiet`（不发声，[siunitedit.cpp:167-175](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/CustomWidgets/siunitedit.cpp#L167-L175)），避免「钳制后的回写」再触发一次 `valueChanged` 造成递归。
- [signalgenwidget.cpp:51-71](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L51-L71)：`span` 的 `valueChanged` lambda，实现 4.2.2 所列的互锁。
- [signalgenwidget.cpp:83-92](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L83-L92)：`dwell` 的 `valueChanged` lambda。夹到 `[0.01, 60]` 秒，然后用 `startTimer(newval*1000)` 重启定时器并更新 `m_timerId`。

**软扫频引擎**：

- [signalgenwidget.cpp:29](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L29)：构造时 `startTimer(1000)` 起一个 1 秒的 Qt 定时器（与默认 dwell = 1 s 对应，[第 28 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L28)）。
- [signalgenwidget.cpp:122-133](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L122-L133)：重写的 `timerEvent()`。先核对 `event->timerId() == m_timerId`（过滤无关定时器），勾选了 `EnabledSweep` 才推进：`current += span/steps`，越过上边沿就回绕到下边沿，`setValueQuiet` 写回后直接调用 `SettingsChanged()` 触发一次下发。**每一步扫频 = 一次完整的配置下发**，理解这点后你就明白为什么 dwell 最小被限制为 10 ms——下发本身要占用一次 USB 往返。

**端口复选框的动态生成与互斥**：

- [signalgenwidget.cpp:185-209](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L185-L209)：`deviceInfoUpdated()`。按 `info.Limits.Generator.ports` 删旧建新端口复选框；每个复选框的 `toggled` lambda 里，若自身被勾选，则对其他复选框 `blockSignals(true)` + `setChecked(false)` + 解除阻塞——保证任何时刻至多一个端口被勾选（单源输出），且取消他人时不触发多余的信号。函数末尾按设备能力刷新滑块/Spinbox 的上下限（[216-219 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L216-L219)）。该函数在设备连接、断开、信息更新时由 `Generator::deviceInfoUpdated()` 转发调用，所以换一台能力不同的仪器，界面限制会自动跟着变。

**无设备时的行为**：控件大量调用 `DeviceDriver::getInfo(window->getDevice())`，当指针为空时它返回一个默认 `Info()`（[devicedriver.h:485-491](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L485-L491)），其 Generator 限制是 2 端口、0–100 GHz、−100~+30 dBm（[devicedriver.cpp:147-151](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L147-L151)）。所以不接硬件也能打开信号源页面正常操作界面，只是 `updateDevice()` 会因未连接而提前返回。

**持久化**：

- [signalgenwidget.cpp:152-166](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L152-L166)：`toJSON()`。保存聚合后的三字段（frequency/power/port）加一个 sweep 子对象（span/steps/dwell/enabled）。
- [signalgenwidget.cpp:168-183](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L168-L183)：`fromJSON()`。前三项走三个槽（会触发钳制与通知），sweep 四项直接 `setValue`/`setChecked`（同样触发各自的 `valueChanged`/`toggled` lambda）。缺项时以当前值为默认，符合 u2-l3 讲过的向后兼容策略。

#### 4.2.4 代码实践

**实践：手工解析一份信号源的 setup 文件**

1. **实践目标**：把 4.2 的聚合规则和 JSON 字段对应起来，验证「控件即状态」。
2. **操作步骤**：
   - 启动 GUI（无硬件即可），切到 Signal Generator 模式，把频率设为 1 GHz、电平 −10 dBm，勾选 Port 2，勾选 Sweep、span 100 MHz、steps 50、dwell 2 s。
   - 用菜单保存工作区为 `.setup` 文件（u2-l3 讲过 Setup 保存机制），用文本编辑器打开，找到 `GENerator`（或模式名对应的）节点。
   - 对照 [signalgenwidget.cpp:152-166](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L152-L166) 逐字段核对：`frequency` 是否为 1000000000、`power` 为 −10、`port` 为 2、`sweep.span` 为 100000000、`sweep.steps` 为 50、`sweep.dwell` 为 2、`sweep.enabled` 为 true。
3. **需要观察的现象**：保存的 JSON 与界面值一一对应；注意 `frequency` 存的是**中心频率**而非当前扫频点。
4. **预期结果**：字段完全吻合。若把 `power` 手工改成 200（超出默认上限）再加载，界面应显示钳制后的值（因为 `fromJSON` 走 `setLevel`，会按 `Limits.Generator.maxdBm` 夹取）。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`setLevel` 里为什么必须 `blockSignals`，而 `setFrequency` 里却看不到 `blockSignals`？

<details><summary>参考答案</summary>

因为两个控件的信号回环风险不同。`levelSpin` 的 `valueChanged` 直连 `setLevel`（[signalgenwidget.cpp:94](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L94)）：若 `setLevel` 里直接 `setValue(level)`，会再次触发 `valueChanged` → 又调 `setLevel`。虽然值相同时 Qt 不再发信号（`QDoubleSpinBox::setValue` 值不变时不发），终止了递归，但为了语义明确、避免多余的信号风暴，代码选择阻塞信号后手动发一次 `SettingsChanged`。`setFrequency` 则是刻意「借道」`frequency` 的 `valueChanged` lambda 来完成钳制互锁和通知，所以不需要也不能阻塞信号。

</details>

**练习 2**：阅读 [signalgenwidget.cpp:83-92](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L83-L92)：用户连续多次修改 dwell 时间后，程序里存在几个活跃的 Qt 定时器？`timerEvent` 会混乱吗？

<details><summary>参考答案</summary>

每次修改 dwell 都 `startTimer(newval*1000)` 并把新 ID 存进 `m_timerId`，但**旧定时器从未被 `killTimer` 终止**，所以修改 N 次后存在 N+1 个活跃定时器（构造时 1 个 + 每次修改 1 个）。`timerEvent` 不会行为混乱，因为它只响应 `timerId() == m_timerId` 的那个事件，旧定时器的事件被过滤掉；代价是无意义的周期唤醒。改进方法：`startTimer` 之前先 `killTimer(m_timerId)`。

</details>

**练习 3**：`getDeviceStatus()` 里 `s.port` 的赋值写法是「遍历所有复选框，勾选者覆盖 s.port = i+1」。如果复选框互斥逻辑失效（两个端口同时被勾选），结果会是什么？

<details><summary>参考答案</summary>

会取**编号最大**的勾选端口（后者覆盖前者），编号小的被静默忽略。这是一个依赖 UI 互斥不变量才能正确的实现；若要更稳健，可以在聚合时发现多个勾选就直接返回 0 或断言。对照固件端 [Generator.cpp:11-27](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Generator.cpp#L11-L27)：`activePort == 0` 时固件关断全部源链路，所以「没有勾选任何端口」在设备侧的语义就是无输出。

</details>

### 4.3 下发链路：从滑块到 USB 包

#### 4.3.1 概念说明

这一节把前两个模块串起来，回答本讲的核心问题：**一次「把电平拖到 −10 dBm」到底走过了哪些代码？**

链路的本质是一个「翻译链条」：每一层只做一次单位/格式转换，谁也不懂对方的细节：

```text
UI 事件（int，滑块刻度 ×0.01）
  → double dBm（signalgenwidget）
  → DeviceDriver::SGSettings（double，硬件无关）
  → Protocol::GeneratorSettings（定点数：cdbm = dBm×100，uint64 Hz）
  → 字节帧（EncodePacket：0x5A 帧头 + 长度 + 类型 + payload + CRC32）
  → USB 批量传输（libusb）
  → 固件 Generator::Setup()（恢复成 cdbm，分频段配置 Si5351C/MAX2871）
```

同时要回答实践任务里的第二个问题：为什么这种「每次变化立即下发」的设计在连续拖动时需要防抖或合并。

#### 4.3.2 核心流程

以「拖动电平滑块」为例的完整调用栈（括号内为文件与行号）：

```text
QSlider::valueChanged(int)                        [signalgenwidget.cpp:95-97]
 └─ SignalgeneratorWidget::setLevel(double)        [signalgenwidget.cpp:222-238]
     ├─ 按 [mindBm, maxdBm] 钳制
     ├─ blockSignals → 同步 spinbox/滑块 → 解除阻塞
     └─ SettingsChanged()                          （直连，无防抖）
         └─ Generator::updateDevice()              [generator.cpp:79-86]
             ├─ 守卫：设备已连接且模式激活
             └─ DeviceDriver::setSG(SGSettings)     ← 多态分派
                 └─ LibreVNADriver::setSG(s)        [librevnadriver.cpp:600-611]
                     ├─ 填 PacketInfo{type=Generator, frequency, cdbm_level=s.dBm*100, activePort=s.port}
                     ├─ isIdle=false; lastNonIdlePacket=p   （供切参考源时恢复）
                     └─ SendPacket(p)               [librevnausbdriver.cpp:264-277]
                         ├─ 入队 transmissionQueue
                         └─ 若无在途包 → startNextTransmission()   [librevnausbdriver.cpp:361-388]
                             ├─ EncodePacket 编码成字节帧
                             ├─ DevicePacketLog 记录（u4-l3 的包日志）
                             └─ libusb_bulk_transfer(EP_Data_Out)
                                  … USB 往返 …
                             收到 Ack → transmissionFinished()      [librevnausbdriver.cpp:228-262]
                                 ├─ 出队，触发回调
                                 └─ 队列非空 → 继续发下一个
固件侧：Communication 分发 → Generator::Setup(g)    [Generator.cpp:11-65]
 └─ activePort==0 全关；否则频率修正、按波段选 Si5351C/MAX2871、FPGA::OverwriteHardware 钉住射频开关
```

#### 4.3.3 源码精读

**驱动侧的翻译**。`SGSettings` → 协议包只用了 7 行：

- [librevnadriver.cpp:600-611](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L600-L611)：`setSG()`。`p.generator.cdbm_level = s.dBm * 100` 完成 dBm→cdbm 的定点化；`activePort` 原样传递；`applyAmplitudeCorrection = true` 表示启用设备级幅度校准（u5-l3 讲过的 flash 校准表）。还把包存进 `lastNonIdlePacket`——u4-l3 讲过的暂停-恢复机制：切外部参考等需要暂时 idle 的操作完成后，靠它重发最后一个非 idle 配置，输出得以恢复。
- 协议结构见 [Protocol.hpp:192-198](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L192-L198)：`GeneratorSettings` 只有 `uint64_t frequency`、`int16_t cdbm_level` 和两个位域（`activePort:3`、`applyAmplitudeCorrection:1`），包类型枚举值为 12（[Protocol.hpp:586](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L586)）。注意 `int16_t cdbm_level` 的表示范围是 ±327.67 dBm，远超设备实际能力，所以真正的范围约束在 GUI 层由 `Limits.Generator` 完成。
- `DeviceDriver` 基类的默认实现直接 `return false`（[devicedriver.h:448](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L448)）——不覆写即「不支持」，u3-l1 讲过的隐式降级。纯频谱仪驱动 SSA3000X 覆写了它（跟踪源），纯矢网 SNA5000A 亦然。

**发送队列与单包在途**：

- [librevnausbdriver.cpp:264-277](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L264-L277)：`SendPacket()` 只做入队，若当前没有在途包则立即启动发送。**它不会因为队列已满而拒绝**——来者不拒，全部排进 `transmissionQueue`。
- [librevnausbdriver.cpp:361-388](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L361-L388)：`startNextTransmission()` 取队头编码后 `libusb_bulk_transfer` 同步发出，并启动超时定时器。
- [librevnausbdriver.cpp:228-262](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L228-L262)：`transmissionFinished()` 在收到 Ack（或超时/Nack）后出队一个包，再尝试发下一个；队列空了才把 `transmissionActive` 置 false。

**为什么 Generator 需要（却没做）防抖**。把三个事实放在一起看：

1. **触发是洪泛的**：拖动滑块时 `QSlider::valueChanged` 每过一个刻度发一次（[signalgenwidget.cpp:95-97](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L95-L97)，滑块刻度是 0.01 dB，见 [第 234 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L234) 的 `level*100.0`）；频率编辑框的滚轮步进同样每格发一次。一次拖动轻松产生几十个 `SettingsChanged`。
2. **每个包的代价是一次完整 USB 往返**：单包在途机制下，队列按「收到上一个 Ack」的节奏逐包流出。中间值毫无用处——用户只关心松手时的最终值——但它们仍会被逐个发到设备，设备也就逐个执行一遍 `Generator::Setup()`（固件里那可是关断源、重配 PLL、重写 FPGA 开关的完整流程，[Generator.cpp:29-65](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Generator.cpp#L29-L65)）。
3. **对照 VNA 模式**：VNA 的 `SettingsChanged` 默认带 100 ms 延迟防抖——它启动的是一个单次 `QTimer`，期间的新事件只是重置定时器，超时后才调用一次 `ConfigureDevice`（[vna.cpp:82-85](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L82-L85)、[vna.cpp:1098-1113](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1098-L1113)，默认 delay 声明在 [vna.h:137](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.h#L137)）。VNA 重配的代价更高（整条扫描重编程、丢点），所以它必须防抖；Generator 的单包较便宜，作者选择了「立即响应 + 队列排队」的折中。

后果与改进方向：拖动越快，队列积压越多，真实输出更新滞后于界面；极端时界面已停，设备还在逐包「回放」中间值。改进可以照抄 VNA 的单次定时器防抖，或让 `SendPacket` 对同类包做「合并」（丢弃队列中尚未发出的旧 Generator 包），或干脆把滑块改为 `tracking(false)`/松手才提交。

**多设备组合下的下发**。u3-l3 讲过的 CompoundDriver 对 `setSG` 做了端口映射分发，可作这条链路「多态」的注脚：

- [compounddriver.cpp:422-439](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddriver.cpp#L422-L439)：把虚拟端口 `s.port` 经 `portMapping` 翻译成「某台设备的某个物理端口」，对每台设备各发一个 `setSG`（非目标设备的 port 置 0 = 关输出）。Generator 模式对此毫不知情——这就是抽象层的价值。

**与 VNA 零扫宽（点频）模式的对比**。两者都让设备「固定在一个频率上」，但方向完全相反：

| 维度 | Generator 模式 | VNA 零扫宽模式 |
|---|---|---|
| 目的 | 只输出 CW，不测量 | 单频点**连续测量** S 参数随时间的变化 |
| 设备配置包 | `Generator`（类型 12），无测量语义 | `SweepSettings`，`zerospan` 标志在 GUI 侧由 `start == stop` 推出（[vna.cpp:1684](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1684)） |
| 上行数据 | 无 | 持续接收 `VNADatapoint`，X 轴类型切到时间 `TimeZeroSpan`，时间戳相对首个点归零（[vna.cpp:1010-1019](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1010-L1019)） |
| 扫频能力 | GUI 定时器软扫频（span/steps/dwell） | FPGA 硬件自主扫描（零扫宽是其退化情形） |
| 模式状态 | 只有控件值 | `Settings` 结构 + TraceModel + 平均器 + 校准 |

一句话：**Generator 是「只写」模式——链路只有下行；VNA 零扫宽是「写一次、读不停」的模式**。当你要的只是给被测件喂一个信号，用 Generator；当你要观察信号喂下去之后 DUT 的响应随时间的漂移，用零扫宽。

#### 4.3.4 代码实践

**实践：手动跟踪一次频率修改的完整调用栈，并解释防抖问题**

1. **实践目标**：不借助调试器，纯靠读码写出「用户在频率框输入 2.5 GHz 回车」从 UI 到 `libusb_bulk_transfer` 的每一级函数；理解为何连续操作需要防抖/合并。
2. **操作步骤**：
   - 从 [signalgenwidget.cpp:35-49](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L35-L49) 的 `frequency` valueChanged lambda 入手（用户编辑提交后 `SIUnitEdit` 解析文本并 `setValue`，进而发出该信号）。
   - 依次向下追：`emit SettingsChanged()` → [generator.cpp:27](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L27) 的 connect → [generator.cpp:79-86](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L79-L86) `updateDevice` → [librevnadriver.cpp:600-611](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L600-L611) `setSG` → [librevnausbdriver.cpp:264-277](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L264-L277) `SendPacket` → [librevnausbdriver.cpp:361-388](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L361-L388) `startNextTransmission`。
   - 把每一级整理成「函数（文件:行）→ 做了什么转换」的表格，标注单位变化：Hz（double）→ Hz（uint64）＋ cdbm（int16）→ 字节帧。
   - 再读 [vna.cpp:82-85](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L82-L85) 与 [vna.h:137](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.h#L137)，写出 VNA 的防抖机制，然后回答：Generator 若照抄，应把 `QTimer` 放在 `Generator` 类还是 `SignalgeneratorWidget` 类？连接关系怎么改？
   - （可选，有硬件）打开设备包日志（u4-l3 讲过的 DevicePacketLog，存 `.vnalog`），快速拖动电平滑块一遍，导出日志数一数队列里排了几个 Generator 包。
3. **需要观察的现象**：调用栈共 6 级；VNA 的防抖是「单次 QTimer + 100 ms 默认延迟」；包日志里同一时刻出现一串类型 12 的包（待本地验证）。
4. **预期结果**：能独立写出下表；能说出「防抖应加在 Generator::updateDevice 之前（例如用 QTimer 把 updateDevice 改为定时器触发），这样 SCPI 和控件两条入口都能受益」。

| 层 | 函数（文件:行） | 转换/动作 |
|---|---|---|
| 控件 | `frequency` valueChanged lambda（signalgenwidget.cpp:35-49） | 钳制＋span 互锁，`emit SettingsChanged()` |
| 模式 | `Generator::updateDevice`（generator.cpp:79-86） | 守卫后调用 `setSG(getDeviceStatus())` |
| 驱动抽象 | `DeviceDriver::setSG`（devicedriver.h:448） | 多态分派到具体驱动 |
| 协议翻译 | `LibreVNADriver::setSG`（librevnadriver.cpp:600-611） | `SGSettings` → `PacketInfo`（dBm×100） |
| 传输抽象 | `LibreVNADriver::SendPacket`（librevnausbdriver.cpp:264-277） | 入队；空转则启动发送 |
| USB | `startNextTransmission`（librevnausbdriver.cpp:361-388） | `EncodePacket` 编码 → `libusb_bulk_transfer` |

#### 4.3.5 小练习与答案

**练习 1**：为什么 `LibreVNADriver::setSG` 里要执行 `lastNonIdlePacket = p`？结合 u4-l3 的「暂停-恢复」机制说明。

<details><summary>参考答案</summary>

驱动在某些操作（如切换外部参考）前必须让设备暂时 idle（发 `SetIdle` 包），操作完成后再恢复测量/输出。`lastNonIdlePacket` 记录了进入 idle 前最后一个「干活」的配置包，恢复时直接重发它即可。对 Generator 而言，这意味着切换参考源之后输出会自动回到之前的频率/电平/端口，模式层无需参与。TCP 驱动与 u3-l3 的 CompoundDriver（`lastNonIdleSettings`）都有对应机制。

</details>

**练习 2**：`DeviceDriver::availableSGPorts()`（[devicedriver.h:442](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L442)）看起来正是「端口名列表」的接口，但 Generator 界面并没有调用它。阅读官方驱动实现 [librevnadriver.cpp:591-598](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L591-L598)，指出两点值得注意的事实。

<details><summary>参考答案</summary>

其一，GUI 代码中没有任何 `availableSGPorts()` 的调用点（只有声明与各驱动实现），界面实际用的是 `info.Limits.Generator.ports` 自己拼 `Port N` 复选框——这是一个「预留但未接线」的接口。其二，该实现的循环是 `for(i=1; i<ports; i++)`，对 `ports == 2` 的 LibreVNA 只返回 `{"PORT1"}`，漏掉了 PORT2；若哪天启用它，边界条件应改为 `i<=ports`（或按意图改用 `Limits` 语义核对）。

</details>

**练习 3**：[generator.cpp:36](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L36) 把电平存进 QSettings 时用了 `static_cast<unsigned long long>((double) settings.dBm)`。这对负电平（例如 −10 dBm）意味着什么？

<details><summary>参考答案</summary>

`unsigned long long` 无法表示负数，把 −10.0 转换到无符号 64 位整型在 C++ 中是未定义行为（典型实现会回绕成一个约 1.8×10¹⁹ 的巨大值）。下次启动读到这个巨大值交给 `setLevel` 后会被上限 `maxdBm` 钳住——结果是记忆功能对负电平失效（恢复成最大电平而不是保存的电平），而不是崩溃。修复只需去掉转换、直接以 double 存入 QSettings（QVariant 原生支持）。实际恢复行为待本地验证（取决于平台对 UB 的处理）。

</details>

## 5. 综合实践

**给 Generator 模式补一个 VNA 式的防抖，并用包日志验证**

这个任务把本讲三个模块全部串起来：

1. **阅读**：对照 [vna.cpp:82-85](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L82-L85)、[vna.cpp:1098-1113](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1098-L1113)，写出 VNA 防抖的三个要素：单次定时器、`start(delay)` 重置语义、超时回调里才真正 `ConfigureDevice`。
2. **设计**：在 `Generator` 类（[generator.h:30-35](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.h#L30-L35)）中增加一个 `QTimer configurationTimer`，把 [generator.cpp:27](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L27) 的直连改为「`SettingsChanged` → 启动定时器 → 超时 → `updateDevice()`」。想清楚：`initializeDevice()` 里的首次下发要不要也走定时器？（建议不走——首包应立即发。）
3. **权衡**：dwell 最小 10 ms 的软扫频每步都靠 `SettingsChanged` 下发（[signalgenwidget.cpp:122-133](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/signalgenwidget.cpp#L122-L133)）。如果你把防抖延迟设成 100 ms，扫频会变成什么样？（每步都要等 100 ms 定时器到期才下发，实际步进周期变成 max(dwell, 100 ms)——说明防抖参数必须小于最小 dwell，或让扫频路径旁路防抖。）
4. **验证**：有硬件则用包日志对比改动前后「快速拖一次滑块」产生的类型 12 包数量；无硬件则在 `updateDevice()` 加一行 `qDebug()`，用 SCPI 端口（u10-l2 的 TCP 方式）连发 20 次 `GENerator:LVL -10` 观察输出行数变化。
5. **结论**：写 200 字总结「立即下发 vs 防抖下发」的取舍——延迟、包量、扫频节奏三个维度。

注意：这是练习性修改，请在自己的分支上做，不要提交到主仓库；本课程不修改源码。

## 6. 本讲小结

- Generator 是三种内置模式中的「最小实现」：没有自己的状态结构，`SignalgeneratorWidget` 的控件值就是唯一事实来源，模式类只负责生命周期胶水、SCPI 三条命令和 JSON 转发（[generator.cpp:7-28](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Generator/generator.cpp#L7-L28)）。
- 模式切换即输出开关：激活时 `Mode::activate → initializeDevice → setSG` 点亮输出，停用时先存 QSettings 再 `setIdle` 关断（[mode.cpp:83-85](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L83-L85)、[mode.cpp:117-119](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/mode.cpp#L117-L119)）。
- 状态聚合靠 `getDeviceStatus()`：扫频开启取 `current` 否则取 `frequency`，端口复选框互斥且勾选者映射为 `port = i+1`，`port == 0` 语义为全关；span/steps/dwell 不进 `SGSettings`，那是纯 GUI 软扫频。
- 下发链路是一条翻译链：UI 值 → `SGSettings`（double）→ `GeneratorSettings`（cdbm = dBm×100、uint64 Hz）→ 五段式帧 → USB 批量传输 → 固件 `Generator::Setup()`；模式层只认 `DeviceDriver*`，背后 USB/TCP/复合驱动随意替换。
- Generator **没有** VNA 那样的 100 ms 防抖：每次滑块刻度都入队一个包，靠「单包在途 + Ack 逐包放行」的队列兜底；连续拖动会产生无用的中间包并让输出滞后于界面——这正是实践任务要求你补防抖的原因。
- 与 VNA 零扫宽的本质区别是数据方向：Generator 只写不读；零扫宽是写一次配置、持续读回带时间戳的 S 参数（X 轴变时间）。

## 7. 下一步学习建议

- **下一讲 u7-l4（平均、数据分级与流式输出）**会补上 Generator 模式刻意缺席的「上行链路」：VNA/SA 的平均器、Raw/Calibrated/Deembedded 数据分级与 StreamingServer。学完后三种模式的收发全景就完整了。
- 想深挖「单包在途」的另一半，回头重读 u3-l2 的 `DecodeBuffer`/`receivedPacket` 路径，并对照本讲的 `transmissionFinished`（[librevnausbdriver.cpp:228-262](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L228-L262)），体会收发两个方向的线程模型差异。
- 想看固件端如何消费 Generator 包，重读 u5-l4 的 [Generator.cpp:11-65](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Generator.cpp#L11-L65)：`activePort == 0` 全关、低波段走 Si5351C、高波段走 MAX2871、FPGA 硬件覆盖钉住射频开关——GUI 侧一个 12 字节的包，落到硬件是一整套时序动作。
- 如果你在综合实践里实现了防抖，可以继续挑战：给 `SendPacket` 增加「同类型包合并」（丢弃队列中未发出的旧 Generator 包），并思考为什么这个优化对 Ack/Nack 类包绝不能做。
