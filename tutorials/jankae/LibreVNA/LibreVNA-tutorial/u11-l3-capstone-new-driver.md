# 毕业实战：为 LibreVNA 编写一个新设备驱动

## 1. 本讲目标

这是本手册的最后一讲，也是一次「把前面所有知识串起来」的毕业设计。读完本讲并完成实践后，你应该能够：

1. 不看教程、只看 `devicedriver.h` 的接口约定，独立写出一个 `DeviceDriver` 子类的完整骨架。
2. 把新驱动注册进 `getDrivers()` 与 `.pro` 工程文件，让它在 GUI 的「连接到设备」菜单里出现。
3. 理解一个测量点从「驱动发出信号」到「迹线上出现一个点」的完整调用链，并掌握沿线排查问题的手段。
4. 用一个不接任何硬件的 `DemoDriver`（定时产生正弦形状的假 S 参数）验证以上全部环节。

本讲的实践零硬件依赖：一台装了 Qt6 的电脑就够了。

## 2. 前置知识

本讲是 advanced 层的收官，默认你已读过以下两讲（本讲大量复用其结论，不再重复推导）：

- **u3-l1 DeviceDriver：统一的设备驱动抽象**——七个纯虚函数是必须实现的契约；配置类虚函数默认返回 `false` 表示「不支持」；`Info::Limits` 声明能力上限；测量数据经 Qt 信号向上推送。
- **u8-l1 Trace 与 TraceModel**——Trace 按 X 升序存「x + 线性复数 y」，Live 来源的 Trace 靠 `liveParameter()`（如 `"S11"`）从测量数据里认领自己的那一份。

此外还会用到几个散落在前面各讲的结论，这里快速复习：

- **u7-l1**：VNA 模式的一切入口都汇入 `SettingsChanged`，防抖 100ms 后经 `ConfigureDevice` 把设置翻译成 `DeviceDriver::VNASettings` 调用 `setVNA()` 下发。
- **u2-l2**：连接设备后 `ModeHandler` 会激活当前模式并调用 `Mode::initializeDevice()`。
- **u3-l3**：SSA3000X 这个第三方驱动是「发现与连接是模板，能力翻译是手艺」的最佳范例，本讲直接拿它当参照实现。
- **u1-l2 / u1-l3**：`LibreVNA-GUI.pro` 是「活的文件索引」，没登记的 `.cpp/.h` 不参与编译。

几个 Qt 概念（初学者不熟悉的看这里）：

- **信号与槽（signal/slot）**：Qt 的事件回调机制。驱动 `emit VNAmeasurementReceived(m)`，GUI 侧连接到该信号的槽函数就会被调用。
- **QTimer**：定时器。`start(10)` 表示每 10 毫秒触发一次 `timeout()` 信号，我们用它伪造「设备持续吐数据」。
- **`std::set<QString>`**：有序集合，`GetAvailableDevices()` 的返回类型——用 set 是因为序列号天然不能重复，且有序能让菜单稳定显示。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| `Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h` | 驱动抽象基类 | 接口契约清单，文件头注释就是官方给的新增驱动步骤 |
| `Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp` | 驱动注册表与公共实现 | `getDrivers()` 是驱动的唯一注册点 |
| `Software/PC_Application/LibreVNA-GUI/Device/SSA3000X/ssa3000xdriver.cpp` | 第三方频谱仪驱动 | 我们的参照实现：怎么组织 connectTo / 定时器 / 发信号 |
| `Software/PC_Application/LibreVNA-GUI/appwindow.cpp` | GUI 主窗口 | 设备列表 UI 与连接流程 |
| `Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp` | VNA 模式 | 驱动信号的消费端：`NewDatapoint()` 与 `ConfigureDevice()` |
| `Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp` | Trace 容器 | 测量 map 的键如何匹配到 Trace |
| `Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro` | qmake 工程文件 | 新文件必须登记于此 |

新建文件（实践产物，仓库中原本不存在）：

- `Software/PC_Application/LibreVNA-GUI/Device/Demo/demodriver.h`
- `Software/PC_Application/LibreVNA-GUI/Device/Demo/demodriver.cpp`

## 4. 核心概念与源码讲解

本讲三个最小模块：**驱动骨架实现**、**注册与设备列表**、**联调与验证手段**。三者正好对应一个驱动的诞生三步：写出来 → 让它出现 → 让它跑对。

### 4.1 驱动骨架实现

#### 4.1.1 概念说明

一个驱动要「像一台设备」，至少要回答四个问题：

1. **你是谁？**——`getDriverName()` 返回驱动名（必须全局唯一）。
2. **外面有哪些设备？**——`GetAvailableDevices()` 枚举当前可连接的序列号。
3. **怎么连上/断开？**——`connectTo(serial)` / `disconnect()`，连接成功后 `getInfo()` 必须立刻有效。
4. **数据怎么来？**——硬件驱动靠 USB/TCP 收包，我们的演示驱动靠 `QTimer` 定时伪造，最终都收敛为同一个动作：`emit VNAmeasurementReceived(m)`。

这四个问题的答案就是 `DeviceDriver` 的七个纯虚函数。官方在头文件注释里直接给出了新增驱动的四步清单：

> To add support for a new hardware device perform the following steps:
> - Derive from this class
> - Implement all pure virtual functions
> - Implement the virtual functions if the device supports the specific function
> - Add the new driver to getDrivers()

#### 4.1.2 核心流程

一个驱动实例的完整生命周期：

```text
AppWindow 启动
   └─ DeviceDriver::getDrivers()          首次调用时 new 出所有驱动（含 DemoDriver）
         │
         ▼  用户在菜单点选 "Demo-0001"
AppWindow::ConnectToDevice(serial, driver)
   └─ driver->connectDevice(serial)
         └─ connectTo(serial)             填好 info / serial，emit InfoUpdated()
         └─ DeviceDriver::activeDriver = this
         │
         ▼  ModeHandler 激活 VNA 模式
VNA::initializeDevice()
   └─ connect(VNAmeasurementReceived → NewDatapoint)   ← 数据管道在此接通
   └─ SettingsChanged(true) ──100ms 防抖──▶ VNA::ConfigureDevice()
         └─ driver->setVNA(settings, cb)  驱动开始产数（DemoDriver 在这里启动 QTimer）
               │  QTimer 每 10ms
               ▼
         emit VNAmeasurementReceived(m)   → GUI 侧 NewDatapoint(m)
               │
               ▼  用户断开
AppWindow::DisconnectDevice()
   └─ driver->disconnectDevice() ──▶ disconnect()（QTimer 停止）
```

关键点：**驱动是「被动」的**。它从不主动决定何时测量——GUI 通过 `setVNA()` 叫它开始、通过 `setIdle()` 叫它停止，它只负责在收到指令后把数据源源不断地「推」上来。

#### 4.1.3 源码精读

**(1) 接口契约清单。** 七个纯虚函数集中在基类声明里：

[Device/devicedriver.h:L41-L66](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L41-L66) 规定了 `getDriverName`（驱动身份，须全局唯一）、`GetAvailableDevices`（枚举序列号）、`connectTo`/`disconnect`（protected，只能经 `connectDevice()` 间接触发，见 4.2.3）和 `getSerial`（连接后返回序列号）。

[Device/devicedriver.h:L131-L138](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L131-L138) 要求 `getInfo()` 的返回值在 `connectTo()` 返回后**立即有效**——这是 GUI 紧接着就要读 `Limits` 来夹取设置范围的硬性时序。

[Device/devicedriver.h:L173](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L173) 与 [Device/devicedriver.h:L155-L164](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L155-L164)：`getFlags()` 返回状态标志集合（过载、失锁、unlevel、外参考）。演示设备永远健康，返回空集合即可。

**(2) 能力声明：`Info` 的默认值。** 基类构造函数给了一份「无限能力」的默认值：

[Device/devicedriver.cpp:L133-L160](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L133-L160) 默认 `minFreq=0`、`maxFreq=100GHz`、`maxPoints=65535`、端口数 2。注意默认值**不含任何 Feature**——`supportedFeatures` 是空集合，所以新驱动必须至少插入 `Feature::VNA`，否则 VNA 模式会弹「不支持」。

[Device/devicedriver.h:L68-L85](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L68-L85) 是 Feature 枚举全集。GUI 对每个特性都有对应的 UI 闸门（见 4.3.3），声明得越少，界面越简洁、也越不容易骗用户。

**(3) 数据结构：一个测量点长什么样。**

[Device/devicedriver.h:L274-L314](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L274-L314) 定义 `VNAMeasurement`：`pointNum`（点号，从 0 起）、`Z0`、一个区分零扫宽/普通扫描的 union（`frequency`+`dBm` 或 `us`），以及核心的 `measurements`——以字符串为键的 map。**键名必须与 `availableVNAMeasurements()` 返回的名字一致**，且值是线性复数（不是 dB）。union 的坑：填了 `frequency` 再读 `us` 得到的是同一块内存的另一种解释，所以零扫宽与否要与 GUI 下发的设置严格对齐。

**(4) 参照实现：SSA3000X 的构造函数。**

[Device/SSA3000X/ssa3000xdriver.cpp:L9-L21](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/SSA3000X/ssa3000xdriver.cpp#L9-L21) 展示了两个惯用法：在构造函数里把「数据加工回调」装配好（此处是 `TraceDifferenceGenerator`，只对变化的点发信号）；把 `QTimer` 的 `timeout` 连到产数函数。DemoDriver 会原样借用第二个惯用法。

**(5) 参照实现：setSA 的收尾。**

[Device/SSA3000X/ssa3000xdriver.cpp:L176-L239](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/SSA3000X/ssa3000xdriver.cpp#L176-L239)（重点看 L233-L238）：`setSA` 先做未连接检查，翻译设置，然后**启动定时器、调用回调、返回 true**。这个「必须调用 `cb`」的约定来自基类注释（[Device/devicedriver.h:L327-L332](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L327-L332)）——GUI 侧靠这个回调复位迹线、清除 `changingSettings` 标志（见 4.3.3），忘记调用会导致下一次设置变更被吞。

**(6) 参照实现：定时产数。**

[Device/SSA3000X/ssa3000xdriver.cpp:L303-L337](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/SSA3000X/ssa3000xdriver.cpp#L303-L337) 的 `extractTracePoints()`：被定时器唤醒 → 向仪器要数据 → 解析 → `diffGen->newTrace(trace)` 发信号 → `traceTimer.start(100)` 自续。最后一行「干完活再重启动定时器」是**单触发定时器自循环**模式（构造函数里 `setSingleShot(true)`），好处是不会重入。DemoDriver 用更简单的周期定时器即可。

#### 4.1.4 代码实践

**实践目标**：不看任何参考答案，凭 `devicedriver.h` 的注释独立写出 DemoDriver 的头文件。

1. 打开 [Device/devicedriver.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h)，先只读 L4-L12 的注释和所有 `= 0` 的声明。
2. 新建 `Device/Demo/demodriver.h`，写出类骨架：继承 `DeviceDriver`，声明七个纯虚函数的 override，加一个 `QTimer dataTimer` 成员和 `QString serial`、`Info info` 成员。
3. 对照本讲 5.2 节的参考实现检查：函数签名、返回类型、`override` 关键字是否全部一致。

**需要观察的现象**：纯靠读声明就能写出骨架——这正是「接口即契约」的含义；接口注释写得好，使用者就不必读实现。

**预期结果**：头文件能通过编译（此时 `.cpp` 还没写，链接会报 undefined reference，属正常）。若你手边没有编译环境，此步作为「纸面练习」，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `connectTo`/`disconnect` 是 protected，而 `connectDevice`/`disconnectDevice` 是 public？

**答案**：public 的 `connectDevice()` 并不只是转发——它还承担全局仲裁：断开旧的 `activeDriver`、连接成功后把自己设为 `activeDriver`（见 4.2.3 的源码）。若允许外部直接调 `connectTo`，就可能绕过「同一时刻只有一个活动设备」的约束（`isIndepedentDriver` 旁路是给 CompoundDriver 这类复合驱动专用的例外）。

**练习 2**：`VNAMeasurement::measurements` 里存 S11 = 0.1 + 0.1j，GUI 上 Smith 图会显示在哪里？如果驱动误存了 −20（即 dB 值）会怎样？

**答案**：map 存的是**线性复数**，0.1+0.1j 直接按复平面坐标绘制，落在 Smith 图原点右上方、半径约 0.141 的位置。若误存 −20，GUI 仍当线性值处理，Smith 图上会得到模为 20 的点——远超单位圆，Smith 图（默认 `edgeReflection` 不放大时）根本画不出来，XY 图上则会看到荒谬的 +26 dB。这是新驱动最常见的 bug 之一。

**练习 3**：`getInfo()` 为什么不能等设备慢慢回报、必须在 `connectTo()` 返回时就有效？

**答案**：`AppWindow::ConnectToDevice` 成功后立刻激活模式，`VNA::initializeDevice()` 马上读 `supports()` 和 `Limits` 来决定启用哪些 UI、夹取设置范围。如果此时 `Info` 还没填好，模式会把驱动当成「啥都不支持」处理。所以协议是：`connectTo` 内部同步完成能力协商（真实驱动通常在这里同步读一次设备信息），之后变化才通过 `InfoUpdated()` 信号通知。

### 4.2 注册与设备列表

#### 4.2.1 概念说明

写好的驱动只是一段没人引用的代码。要让它出现在 GUI 里，需要三处登记，缺一不可：

1. **`getDrivers()` 注册表**加一行——GUI 枚举驱动全靠这个函数，没有别的发现机制。
2. **`.pro` 工程文件**登记 `.h`/`.cpp`——没登记的文件不参与编译（u1-l3 的结论）。
3. **`GetAvailableDevices()` 返回序列号**——设备列表菜单按「驱动 × 序列号」生成条目。

这一模块讲清这条链，顺带理解设备列表 UI 的生成逻辑。

#### 4.2.2 核心流程

```text
GUI 启动 / 用户点"更新设备列表"
   └─ AppWindow::UpdateDeviceList()
        └─ for 每个 driver in DeviceDriver::getDrivers():
             └─ for 每个 serial in driver->GetAvailableDevices():
                  追加 DeviceEntry{driver, serial}
        └─ 为每个 entry 在 menuConnect_to 里建一个 QAction
             └─ 点击 → ConnectToDevice(serial, driver)

ConnectToDevice(serial, driver)
   ├─ 若已连接：先 DisconnectDevice()
   ├─ for 每个 d in getDrivers()（跳过非指定驱动）:
   │     └─ if d->GetAvailableDevices().count(serial):   ← 靠序列号认领
   │          connect 一批驱动信号（InfoUpdated/ConnectionLost/...）
   │          └─ if d->connectDevice(serial): device = d
   └─ 失败则弹错误框
```

注意 `GetAvailableDevices()` 会被**反复调用**（列表刷新、连接尝试各调一轮），所以它必须廉价、无副作用。

#### 4.2.3 源码精读

**(1) 注册表本体。**

[Device/devicedriver.cpp:L19-L32](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L19-L32) 是懒加载单例：首次调用 `new` 出全部六个驱动并塞进静态 vector，之后直接返回。新增驱动 = 加一个 include（[Device/devicedriver.cpp:L3-L8](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L3-L8)）加一行 `ret.push_back(new DemoDriver);`。顺序决定菜单里的出现顺序（列表按 vector 遍历序生成）。

**(2) 设备列表 UI 的生成。**

[appwindow.cpp:L906-L950](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L906-L950) 的 `UpdateDeviceList()`：清空 `menuConnect_to` → 双重循环收集 `DeviceEntry` → 每个条目建一个可勾选的 `QAction`（放进互斥的 `deviceActionGroup`）→ `triggered` 时调 `ConnectToDevice`。两个细节值得注意：

- L916 会按命令行 `--device` 参数过滤条目——脚本化场景可以强制只显示指定序列号。
- L923-L931：已连接但枚举不到的设备也会补进列表（官方 USB 驱动断电后可能出现这种状态）。

**(3) 连接的仲裁与信号装配。**

[appwindow.cpp:L338-L389](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L338-L389) 是 `ConnectToDevice` 的核心段：遍历驱动、用 `GetAvailableDevices().count(serial)` 判断归属（L343）；命中后先把 `InfoUpdated`、`LogLineReceived`、`ConnectionLost`、`StatusUpdated`、`FlagsUpdated` 及 SCPI 增删信号连上（L345-L380），再调 `connectDevice`（L382）。**这批 connect 是 AppWindow 主动做的**——驱动作者只管 `emit`，不必操心谁来听。

**(4) `connectDevice` 的全局仲裁。**

[Device/devicedriver.cpp:L34-L55](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L34-L55)：连接前先断开旧的 `activeDriver`，成功后登记新值。DemoDriver 因此天然获得「连上 Demo 就自动断开其他设备」的行为，一行代码都不用写。

**(5) 工程文件登记。**

[LibreVNA-GUI.pro:L47-L48](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L47-L48)（HEADERS）与 [LibreVNA-GUI.pro:L219-L220](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L219-L220)（SOURCES）是 SSA3000X 驱动的登记处，照葫芦画瓢各加一行 `Device/Demo/demodriver.h/.cpp` 即可。改完 `.pro` 需重跑 qmake（u1-l3 讲过：`.pro` 是工程单一事实来源）。

#### 4.2.4 代码实践

**实践目标**：完成注册三连，让 Demo 设备出现在菜单里。

1. 在 `devicedriver.cpp` 顶部加 `#include "Demo/demodriver.h"`（相对 `Device/` 目录），在 `getDrivers()` 里加 `ret.push_back(new DemoDriver);`。
2. 在 `LibreVNA-GUI.pro` 的 `HEADERS += \` 与 `SOURCES += \` 列表各加一行。
3. 重新 `qmake6 && make`，启动 GUI，点开菜单 Device → Connect to。

**需要观察的现象**：列表里出现 `Demo : Demo-0001` 这样的条目（条目文本由 `DeviceEntry::toString()` 拼成「驱动名 : 序列号」）。

**预期结果**：不接任何硬件，条目稳定出现。若没出现，按顺序排查：`.pro` 是否重跑 qmake → `GetAvailableDevices()` 是否真的 insert 了序列号 → 驱动名是否与现有驱动撞名。本步骤依赖本讲 5.2 的代码，需在本地编译环境执行；无环境则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：把 `getDrivers()` 里的注册行删掉，编译依然通过、GUI 也正常，这是为什么？

**答案**：注册表是唯一的驱动发现点，而它靠懒加载静态 vector 维护；没有任何编译期机制强制驱动被注册。这就是「注册三连」最隐蔽的一环——忘注册不会报错，只会让你的驱动在菜单里沉默缺席。

**练习 2**：两个驱动的 `getDriverName()` 都返回 `"Demo"`，会发生什么？

**答案**：接口注释（[Device/devicedriver.h:L38-L41](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L38-L41)）要求驱动名全局唯一。代码里设备条目显示「驱动名 : 序列号」，撞名会让用户无法区分；更严重的是序列号认领（`GetAvailableDevices().count(serial)`）按「序列号 ∈ 某驱动的列表」判断，若两驱动都报同一序列号，先遍历到的驱动会抢走连接。驱动名是面向用户与 `.setup` 持久化的身份，务必唯一。

**练习 3**：为什么 `UpdateDeviceList()` 里已连接的设备即使 `GetAvailableDevices()` 枚举不到也要补进列表？

**答案**：见 [appwindow.cpp:L923-L931](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L923-L931) 的注释——有些状态下驱动「已连接但枚举不到自己」，例如 USB 设备断电后。补进列表保证了菜单勾选状态与实际连接状态一致，用户也能从菜单看清当前连着什么。

### 4.3 联调与验证手段

#### 4.3.1 概念说明

驱动出现在菜单里只是第一步。连上后点 Run，数据能不能一路走到迹线上，才是真正的考验。这一模块把「驱动 emit 的那个点」到「屏幕上的一个点」之间的每一站列出来，每一站都是一个可观测、可打断点的检查点。掌握了这条链，任何「没数据/数据错」的问题都能二分定位。

#### 4.3.2 核心流程

一个 `VNAMeasurement` 从驱动到屏幕的完整旅程：

```text
DemoDriver::generateDatapoint()
  emit VNAmeasurementReceived(m)
        │  （Qt::UniqueConnection，直连，GUI 线程内）
        ▼
VNA::NewDatapoint(m)                          [vna.cpp:L959]
  ├─ 守卫 1：!isActive → 丢弃（当前不是 VNA 模式）
  ├─ 守卫 2：changingSettings → 丢弃（旧设置的残留点）
  ├─ pointNum==0 且 lastPoint>0 → 判定"新扫描开始"
  ├─ 点号越界（>= settings.npoints）→ 丢弃并告警
  ├─ average.process()                         多圈平均
  ├─ cal.correctMeasurement()                  校准修正
  ├─ traceModel.addVNAData(m, type, false)     → 写入各 Live Trace
  │     └─ for 每条 live trace:
  │          if d.measurements.count(trace.liveParameter())   ← 键名匹配！
  │               t->addData(...)                               → dataChanged → 图重绘
  └─ pointNum == npoints-1 → 扫描完成，更新 Marker
```

反向的控制链（决定 `setVNA` 何时被调）：

```text
Trace 增删/参数变更 → TraceModel::requiredExcitation → VNA::ExcitationRequired
                                                    → SettingsChanged → 100ms 防抖 → ConfigureDevice
```

#### 4.3.3 源码精读

**(1) 数据管道的接通点。**

[VNA/vna.cpp:L781-L798](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L781-L798) 的 `initializeDevice()`：先检查 `Feature::VNA`（L783），再在 L797 把驱动的 `VNAmeasurementReceived` 连到自己的 `NewDatapoint`，**`Qt::UniqueConnection` 防止重复连接**（模式每次激活都会跑这里）。如果你的驱动数据出不来，第一个检查点就是这里有没有被走到——前提是 `supports(Feature::VNA)` 为真。

**(2) 消费端的三重守卫。**

[VNA/vna.cpp:L959-L1005](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L959-L1005)：`NewDatapoint` 开头两个早退（L961-L969）解释了「驱动明明在发数据、迹线却不动」的多数情况——要么当前活动模式不是 VNA，要么正在切换设置。L1002-L1005 的点号越界检查则要求**驱动发的点号必须与 GUI 下发的 `settings.points` 严格一致**：DemoDriver 的点号循环周期必须用 `setVNA` 收到的 `s.points`，而不是自己写死。

**(3) 键名匹配：数据认领的现场。**

[Traces/tracemodel.cpp:L286-L323](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp#L286-L323) 的 `addVNAData`：逐条 Live Trace 用 `liveParameter()` 当键去测量 map 里查（L310），查不到就跳过该 Trace（L312-L314，**不报错**——这是「有 Trace 无数据」最安静的失败模式）。所以 `availableVNAMeasurements()` 返回的名字、`measurements` map 的键、用户建 Trace 时选的参数，三者必须逐字相同。官方驱动的命名实现可参考 [Device/LibreVNA/librevnadriver.cpp:L460-L478](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L460-L478)：按端口数生成 `S11、S12、…`。

**(4) 下行控制的终点：`setVNA` 在哪被调。**

[VNA/vna.cpp:L1974-L2059](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1974-L2059) 的 `ConfigureDevice()`：组包 `VNASettings`（L1983-L2040，含 excitedPorts 的推导与 start/stop 填充），L2042 调 `window->getDevice()->setVNA(s, cb)`。**回调 `cb` 里做的事**（L2043-L2053）：复位迹线、清 `changingSettings`、重置点号统计——再次印证 4.1.3 的结论：驱动必须调用 `cb`。

**(5) 激励端口的反向推导。**

[Traces/tracemodel.cpp:L229-L242](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/tracemodel.cpp#L229-L242) 的 `PortExcitationRequired()`：扫描参数名第 3 个字符（`param[2]`，即 S**1**1 的激励口），有 Live Trace 需要就返回真。注意它只认 `S??` 形式的参数名——这也是命名要对齐的又一个理由。`VNA::ConfigureDevice` 在 [VNA/vna.cpp:L1985-L2003](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1985-L2003) 据此填充 `excitedPorts`（偏好设置 `alwaysExciteAllPorts` 可越过该机制）。

**(6) 建 Trace 的入口。**

[Traces/traceeditdialog.cpp:L133](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Traces/traceeditdialog.cpp#L133)：新建 Trace 对话框的「Live 参数」下拉框直接由 `DeviceDriver::getActiveDriver()->availableVNAMeasurements()` 填充。所以驱动声明了什么参数名，用户就能选到什么参数名——一个函数同时决定 UI 与数据匹配，这就是三处对齐能自动成立的原因。

#### 4.3.4 代码实践

**实践目标**：沿数据链设置三个「观测站」，验证 Demo 数据真的在流动。

1. **观测站 A（驱动侧）**：在 `generateDatapoint()` 里加一行 `qDebug() << "Demo point" << m.pointNum << m.frequency;`。
2. **观测站 B（模式侧）**：`NewDatapoint` 已有现成日志——[VNA/vna.cpp:L1078-L1080](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1078-L1080) 会在点号不连续时告警；再借 [VNA/vna.cpp:L972-L978](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L972-L978) 的「Sweep took N milliseconds」观察扫描周期。
3. **观测站 C（GUI 侧）**：连接 Demo 设备 → 新建 Trace，Source 选 Live、参数选 S11 → 新建一个 XY 图并勾选该 Trace → 点工具栏 Run 启动扫描。

**需要观察的现象**：终端里 A 站每 10ms 打一行；B 站周期性打印扫描耗时且**没有**「missed points」告警；C 站迹线每扫一圈向右刷新一次，呈现 4 个完整周期的正弦包络。

**预期结果**：三站全绿说明数据链贯通。若 A 有输出而迹线不动：先查 Trace 参数名与 map 键名（观测站 C 的下拉框内容就是 `availableVNAMeasurements()` 的直接显示），再查是否处在非活动模式/设置切换窗口（守卫 1、2）。调试日志的查看方式：GUI 从终端启动即可见 qDebug 输出。本实践依赖 5.2 的代码，需本地编译运行。

#### 4.3.5 小练习与答案

**练习 1**：迹线完全不动，`generateDatapoint()` 的 qDebug 却在持续打印。给出一个三步排查顺序。

**答案**：① 查模式守卫——当前活动模式是否是 VNA（切到频谱仪模式时 `isActive` 为假，L961-L964 直接丢弃）；② 查键名——Trace 的 `liveParameter()` 是否与 map 键逐字一致（`addVNAData` L310-L314 静默跳过不匹配的 Trace）；③ 查点号——发出的 `pointNum` 是否小于 GUI 下发的 `settings.points`（L1002-L1005 越界丢弃并告警）。

**练习 2**：驱动作者忘了在 `setVNA` 里调用回调 `cb`，用户会看到什么症状？

**答案**：`changingSettings` 永远为真（它在 [VNA/vna.cpp:L1981](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1981) 置位，只在 cb 里清零，见 L2043-L2053），于是后续所有测量点都被守卫 2 丢弃——典型症状是「第一次扫描正常，改任何设置后就再也出不来数据」。

**练习 3**：为什么 DemoDriver 用 QTimer 产数不会遇到线程问题，而真实 USB 驱动却要小心？

**答案**：`getDrivers()` 首次被调用发生在 GUI 线程（AppWindow 构造/设备列表更新），DemoDriver 及其 QTimer 成员都属 GUI 线程，`timeout` 到 `generateDatapoint` 是同线程直连，`emit` 后槽同步执行。真实驱动（如 u3-l2 的 USB 驱动）的收包发生在 libusb 事件线程，跨线程发信号就必须是队列连接，而 `VNAMeasurement` 这类自定义类型要跨线程排队传递，需先 `qRegisterMetaType`（这正是基类提供 `registerTypes()` 虚函数的原因，[Device/devicedriver.h:L493-L499](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L493-L499)；类型本身已在 [Device/devicedriver.h:L595-L596](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L595-L596) 用 `Q_DECLARE_METATYPE` 声明）。

## 5. 综合实践

### 5.1 任务描述

实现一个完整的 **DemoDriver**——一台不存在的虚拟双端口 VNA：

- `getDriverName()` 返回 `"Demo"`；
- `GetAvailableDevices()` 永远返回虚拟序列号 `"Demo-0001"`（演示设备「永远在线」）；
- 连接后由 `setVNA()` 启动 QTimer，逐点产生 `measurements["S11"]` 为正弦形状复数、`measurements["S21"]` 为平坦复数的测量点；
- 注册后不接任何硬件，GUI 里即可选中 Demo 设备、建 S11 迹线、看到滚动刷新的正弦波。

### 5.2 参考实现

以下代码为本讲**示例代码**（仓库中不存在），目录 `Software/PC_Application/LibreVNA-GUI/Device/Demo/`：

`demodriver.h`：

```cpp
#ifndef DEMODRIVER_H
#define DEMODRIVER_H

#include "Device/devicedriver.h"

#include <QTimer>

class DemoDriver : public DeviceDriver
{
    Q_OBJECT
public:
    DemoDriver();

    virtual QString getDriverName() override { return "Demo"; }
    virtual std::set<QString> GetAvailableDevices() override;
    virtual QString getSerial() override { return serial; }
    virtual Info getInfo() override { return info; }
    virtual std::set<Flag> getFlags() override { return {}; }
    virtual QStringList availableVNAMeasurements() override;

    virtual bool setVNA(const VNASettings &s, std::function<void(bool)> cb = nullptr) override;
    virtual bool setIdle(std::function<void(bool)> cb = nullptr) override;

protected:
    virtual bool connectTo(QString serial) override;
    virtual void disconnect() override;

private slots:
    void generateDatapoint();

private:
    QString serial;
    Info info;
    QTimer dataTimer;
    VNASettings settings = {};
    unsigned int pointNum = 0;
};

#endif // DEMODRIVER_H
```

`demodriver.cpp`：

```cpp
#include "demodriver.h"

#include <QtMath>
#include <complex>

DemoDriver::DemoDriver()
{
    // 能力声明：一台 100kHz..6GHz 的双端口 VNA（见 devicedriver.cpp:133 的默认值）
    info = Info();
    info.firmware_version = "0.1-demo";
    info.hardware_version = "virtual";
    info.supportedFeatures.insert(Feature::VNA);
    info.supportedFeatures.insert(Feature::VNAFrequencySweep);
    info.Limits.VNA.ports = 2;
    info.Limits.VNA.minFreq = 100000;
    info.Limits.VNA.maxFreq = 6000000000;
    info.Limits.VNA.maxPoints = 1001;

    connect(&dataTimer, &QTimer::timeout, this, &DemoDriver::generateDatapoint);
}

std::set<QString> DemoDriver::GetAvailableDevices()
{
    // 演示设备永远在线；真实驱动在这里做 USB/TCP 枚举
    return {"Demo-0001"};
}

bool DemoDriver::connectTo(QString serial)
{
    if(GetAvailableDevices().count(serial) == 0) {
        return false;
    }
    this->serial = serial;
    emit InfoUpdated();
    return true;
}

void DemoDriver::disconnect()
{
    setIdle();
    serial.clear();
}

QStringList DemoDriver::availableVNAMeasurements()
{
    // 该列表同时决定 Trace 对话框的可选项与数据 map 的键名，务必与产数端一致
    return {"S11", "S12", "S21", "S22"};
}

bool DemoDriver::setVNA(const VNASettings &s, std::function<void(bool)> cb)
{
    settings = s;
    pointNum = 0;
    dataTimer.start(10);   // 每 10ms 一个点
    if(cb) {
        cb(true);          // 契约：配置完成后必须回调（vna.cpp:2043 依赖它清 changingSettings）
    }
    return true;
}

bool DemoDriver::setIdle(std::function<void(bool)> cb)
{
    dataTimer.stop();
    if(cb) {
        cb(true);
    }
    return true;
}

void DemoDriver::generateDatapoint()
{
    if(settings.points < 2) {
        return;            // 防除零；也避免与 GUI 的 npoints 不一致
    }
    VNAMeasurement m;
    m.pointNum = pointNum;
    m.Z0 = 50.0;
    m.frequency = settings.freqStart
                + (settings.freqStop - settings.freqStart) * pointNum / (settings.points - 1);
    m.dBm = settings.dBmStart;

    // S11：沿扫描走出 4 个周期的正弦（模长 0.3，起点在正实轴）
    double angle = 2.0 * M_PI * pointNum / settings.points * 4.0;
    m.measurements["S11"] = std::complex<double>(0.3 * cos(angle), 0.3 * sin(angle));
    // S21：近乎理想的直通（模长 0.99，带一点固定相移）
    m.measurements["S21"] = std::complex<double>(0.95, 0.05);
    // 演示设备不区分激励端口，把另外两个参数也填上
    m.measurements["S12"] = m.measurements["S21"];
    m.measurements["S22"] = m.measurements["S11"];

    pointNum = (pointNum + 1) % settings.points;   // 点号必须在 [0, points) 内循环

    emit VNAmeasurementReceived(m);
}
```

### 5.3 操作步骤

1. 创建目录 `Device/Demo/`，写入上述两个文件（头文件 include 路径按仓库惯例写相对 `LibreVNA-GUI/` 的形式，与其他驱动的写法保持一致即可）。
2. 完成 4.2.4 的注册三连：`devicedriver.cpp` 的 include 与 `getDrivers()`、`.pro` 的 HEADERS/SOURCES。
3. `qmake6 && make` 重新编译并启动 GUI。
4. 菜单 Device → Connect to → `Demo : Demo-0001`。
5. 切到 VNA 模式，新建 Trace：Source = Live、Parameter = S11；再新建一个 XY 图（Y 轴选线性幅度或 dB），勾选该 Trace。
6. 点工具栏 Run 启动扫描，观察迹线滚动刷新。

### 5.4 预期结果与检查点

| 检查点 | 预期 |
| --- | --- |
| 设备菜单 | 出现 `Demo : Demo-0001` 条目 |
| Trace 对话框 Live 参数下拉框 | 恰好是 S11/S12/S21/S22（来自 `availableVNAMeasurements()`） |
| Run 之后 | XY 图上 S11 呈 4 个周期的正弦包络，S21 是平线；终端周期打印 "Sweep took N milliseconds" |
| 切到频谱仪模式再切回 | S11 迹线重新出现（`initializeDevice` 重连信号，UniqueConnection 不重复） |
| 断开设备 | 迹线停止刷新，菜单勾选清除 |

### 5.5 进阶挑战（选做）

1. **补全 SA 能力**：插入 `Feature::SA`、实现 `setSA`/`getSApoints`/`availableSAMeasurements`，让频谱仪模式也出假数据（参照 [Device/SSA3000X/ssa3000xdriver.cpp:L176-L244](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/SSA3000X/ssa3000xdriver.cpp#L176-L244)），注意 SA 的值是线性电压（1.0 即 0 dBm）。
2. **加噪声**：在 S11 上叠加小幅随机扰动，验证 GUI 侧平均功能（u7-l4）能把它压下去——`average.process()` 对线性复数做相干平均，信噪比应提升约 10·lgN dB。
3. **加一条驱动专属 SCPI 命令**：在构造函数里往 `specificSCPIcommands` 塞一条 `:DEVice:DEMO:MODe?`（参照 [Device/devicedriver.h:L579-L583](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L579-L583) 的说明），用 `nc localhost 19542` 验证（u10-l2）。

## 6. 本讲小结

- 一个驱动的诞生只需四步（官方注释原文）：继承 `DeviceDriver` → 实现全部纯虚函数 → 按能力选配虚函数 → 注册进 `getDrivers()`；再加上 `.pro` 登记共五个触点。
- 驱动是被动服务者：GUI 经 `setVNA`/`setIdle` 驱使它，它经 `VNAmeasurementReceived` 推数据；`setVNA` 的 `cb` 回调是隐藏契约，不调用会导致后续数据全被 `changingSettings` 守卫吞掉。
- 设备列表由「`getDrivers()` × `GetAvailableDevices()`」双重循环生成，序列号是设备认领的唯一凭据，驱动名是持久化身份，两者都必须唯一。
- 三处字符串必须逐字一致：`availableVNAMeasurements()` 的返回值、`measurements` map 的键、Trace 的 `liveParameter()`；不一致时的失败是**静默**的（`addVNAData` 直接跳过该 Trace）。
- 数据链上有天然的三观测站：驱动 `emit` 前、`VNA::NewDatapoint` 的守卫与告警日志、Trace 图刷新；沿线二分即可定位绝大多数「没数据/数据错」问题。
- 回望整个手册的三层数据链：FPGA 采样并算 DFT（单元 6）→ 固件按协议打包上报（单元 4、5）→ GUI 驱动拆包拼装成 `VNAMeasurement`（单元 3）→ VNA 模式平均校准后写入 TraceModel（单元 7、8）——`DemoDriver` 证明这一切的终点只是一个结构体加一个信号，接口抽象让整条链的任何一端都可以被替换。

## 7. 下一步学习建议

本讲结业后，三条继续深挖的路线：

1. **做真驱动**：找一台支持 SCPI 的仪器（任何 LXI 频谱仪/矢网），以 [Device/SSA3000X/ssa3000xdriver.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/SSA3000X/ssa3000xdriver.cpp) 为骨架接入 `DeviceTCPDriver`，把 DemoDriver 的四个问题换成真实 TCP 收发即可。
2. **向设备端走**：如果你有兴趣点亮整条链的另一端，按单元 5（固件）→ 单元 6（FPGA）的顺序重读，用 `AssembleFirmware.py` 与固件升级流程（u1-l4、u5-l3）刷自己的镜像，对照 `FPGA_protocol.tex` 理解 MCU-FPGA 命令。
3. **给项目贡献**：单元 11 的前两讲（u11-l1 测试、u11-l2 工具箱）加上本讲的能力，你已具备向上游提交 PR 的完整技能栈——为一个新仪器写驱动并配上 `LibreVNA-Test` 风格的用例，是最容易被社区接受的方向。
