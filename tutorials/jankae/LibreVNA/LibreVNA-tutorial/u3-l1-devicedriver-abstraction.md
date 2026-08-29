# DeviceDriver：统一的设备驱动抽象

## 1. 本讲目标

学完本讲，你应该能够：

1. 列出 `DeviceDriver` 的全部纯虚接口，并按「设备发现 → 连接 → 能力查询 → 配置测量 → 接收数据」五个阶段归类。
2. 理解 `getDrivers()` 这个手写注册表的工作方式：懒加载单例、驱动名必须唯一、新增驱动要在源码里注册。
3. 解释 `VNAMeasurement` / `SAMeasurement` 两组数据结构如何把上层 GUI 与具体硬件彻底解耦——VNA 模式根本不知道连的是 USB 的 LibreVNA 还是 TCP 的第三方频谱仪。
4. 拿着一份「新驱动接入清单」，独立评估给一台新仪器写驱动需要实现哪些函数、哪些可以跳过。

本讲是第 3 单元「设备驱动层」的第一讲。前两讲（u2-l1、u2-l2）我们已知 AppWindow 在构造函数末尾做设备枚举、模式通过 `initializeDevice()` 拿到设备对象。本讲就钻进那个「设备对象」的类型本身。

## 2. 前置知识

阅读本讲前，建议你先理解下面几个概念（不熟悉也没关系，这里用一段话讲清）：

- **抽象基类（abstract base class）**：C++ 中把 `virtual ... = 0` 的函数称为纯虚函数，包含纯虚函数的类不能直接实例化，只能被继承。子类必须实现所有纯虚函数才能创建对象。它定义的是一份「契约」：你答应提供这些能力，我就能被所有人统一使用。
- **虚函数的默认实现**：与纯虚相对，普通虚函数可以在基类里给出默认实现。`DeviceDriver` 的技巧是：默认实现一律返回 `false` 或空容器——「我不支持这个功能」。子类只在设备真的支持某功能时才去覆写。这是一种**优雅降级**设计。
- **Qt 信号与槽（signals/slots）**：Qt 框架的对象间通信机制。驱动线程发出一个信号，订阅了该信号的任意对象（比如某个模式）的槽函数就会被调用。`DeviceDriver` 继承 `QObject` 就是为了用这套机制做「驱动 → GUI」的数据推送。
- **S 参数**：描述多端口网络入射波/反射波关系的复数矩阵，`S11` 是端口 1 的反射系数，`S21` 是端口 1→2 的传输系数。第 1 讲和 u1-l1 已介绍过，本讲只需要知道「S 参数名是 `S` 加两个端口编号的字符串」。
- **dBm 与线性幅度**：功率的对数单位。频谱仪测量结构中存的是**线性电压值**，换算关系为：

\[ P_{\mathrm{dBm}} = 10\log_{10}\left(\frac{P}{1\,\mathrm{mW}}\right), \qquad \text{线性值 } 1.0 \Leftrightarrow 0\,\mathrm{dBm} \]

- **`std::set` / `std::map`**：C++ 标准库的有序容器。本讲中「支持的功能集合」「激活的状态标志集合」都用 `std::set`，「S 参数名 → 复数值」用 `std::map<QString, ...>`。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h` | **本讲主角**。约 600 行的抽象基类定义：纯虚接口、`Feature`/`Flag`/`Info` 等能力描述类型、`VNASettings`/`VNAMeasurement` 等数据结构、全部信号声明 |
| `Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp` | 基类的非内联实现：`getDrivers()` 注册表、`connectDevice()` 连接仲裁、测量结构的换算函数、`Info` 的默认值与 `subset()` 合并 |
| `Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.h` | 官方设备的中间层驱动，继承 `DeviceDriver`。实践任务要用它对照「真实驱动覆盖了哪些接口」 |
| `Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.h` | USB 传输层驱动，继承 `LibreVNADriver`，补齐剩余四个纯虚函数 |
| `Software/PC_Application/LibreVNA-GUI/appwindow.cpp` / `appwindow.h` | 上层消费者：设备枚举菜单、连接仲裁、驱动生命周期 |
| `Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp` | 上层消费者示例：VNA 模式如何用 `supports()` 检查能力、如何订阅测量信号 |
| `Software/PC_Application/LibreVNA-GUI/preferences.cpp` | `getDrivers()` 的另一个调用方：把每个驱动的私有设置并入全局偏好 |
| `Software/PC_Application/LibreVNA-GUI/Tools/parameters.h` / `parameters.cpp` | `Sparam` 矩阵类的定义，`toSparam()` 的落点 |

> 一个小考古：`devicedriver.h` 文件开头写的头文件保护宏是 `DEVICEDRIVER_H`，但文件末尾的 `#endif` 注释却写着 `VIRTUALDEVICE_H`（[devicedriver.h:598](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L598)）。再翻 `appwindow.h` 还能看到一行被注释掉的 `// VirtualDevice *vdevice;`（[appwindow.h:127](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.h#L127)）。这印证了这段抽象在 2023 年的重构中曾临时叫 `VirtualDevice`，后来才定名 `DeviceDriver`——重构没有清干净尾巴，反而给我们留下了演化的证据。

## 4. 核心概念与源码讲解

### 4.1 DeviceDriver 接口总览

#### 4.1.1 概念说明

为什么需要 `DeviceDriver`？想象没有它的世界：VNA 模式直接 `include` USB 驱动头文件、直接调用 libusb 风格的函数。那么每接入一台新仪器（比如仓库里已经接入的 Siglent SSA3000X 频谱仪），VNA 模式、频谱仪模式、信号源模式、SCPI 命令树、状态栏……每一处都要加 `if (设备类型 == ...)` 分支。这不可维护。

`DeviceDriver` 的解法是经典的**依赖倒置**：定义一个与传输方式（USB/TCP/SCPI over LAN）、与仪器品牌都无关的抽象基类，让所有模式只认这个基类。仓库作者把「如何新增一台设备」直接写在了文件头注释里：

> To add support for a new hardware device perform the following steps:
> - Derive from this class（继承本类）
> - Implement all pure virtual functions（实现所有纯虚函数）
> - Implement the virtual functions if the device supports the specific function（设备支持哪个功能就实现哪个虚函数）
> - Add the new driver to getDrivers()（把新驱动加进 getDrivers()）

这四步就是整个驱动子系统的设计纲要（[devicedriver.h:L4-L12](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L4-L12)）。

接口设计上有一个贯穿始终的约定值得先记住：

- **纯虚函数 = 必须实现**（身份、发现、连接、状态）；
- **带默认实现的虚函数 = 按能力实现**，默认值统一是 `false` / 空容器 / `nullptr`，语义是「本设备不支持」；
- **信号（signals）= 驱动主动推送**，上层被动接收。

#### 4.1.2 核心流程

一个驱动实例的完整生命周期，以及各接口在其中的位置：

```text
应用启动
  │
  ├─ 首次调用 getDrivers() ──────────► 创建全部驱动实例（一次性注册表）
  │
  ├─ GetAvailableDevices() ─────────► 每个驱动报告自己发现的设备序列号
  │        （构建 Device 菜单，见 appwindow.cpp 的 UpdateDeviceList）
  │
  ├─ 用户选中某序列号 ──► connectDevice(serial)
  │        ├─ 若已有别的活动驱动，先让旧驱动 disconnect()
  │        └─ 调用子类的 connectTo(serial)
  │              └─ 成功后：getInfo() 必须立即有效（能力/限制/固件版本）
  │
  ├─ 模式激活（u2-l2 的 activate → initializeDevice）
  │        ├─ supports(Feature::VNA) 之类的能力检查
  │        └─ setVNA(...) / setSA(...) / setSG(...) / setIdle(...)
  │              └─ 设备开始工作，逐点发回数据：
  │                 emit VNAmeasurementReceived(m) / SAmeasurementReceived(m)
  │
  ├─ 异常：emit ConnectionLost() ───► 上层调 disconnectDevice()
  │
  └─ 应用退出（closeEvent）─────────► delete 所有驱动实例
```

三个「推送型」状态信号穿插其间：`InfoUpdated()`（能力变化）、`FlagsUpdated()`（过载/失锁等标志变化）、`StatusUpdated()`（状态栏文本变化）。

#### 4.1.3 源码精读

**（1）类骨架与注册入口。** `DeviceDriver` 继承 `QObject` 以获得信号槽能力，构造函数为空，析构函数非内联（负责清理驱动私有的 QAction，见 [devicedriver.cpp:L12-L17](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L12-L17)）：

- [devicedriver.h:L24-L35](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L24-L35)：类声明与静态 `getDrivers()`。注意 `getDrivers()` 是 **static**——注册表属于整个类而不是某个实例。

**（2）七个纯虚函数：驱动的「身份证」。** 逐个看签名和注释：

| 纯虚函数 | 位置 | 语义 |
| --- | --- | --- |
| `QString getDriverName()` | [devicedriver.h:L41](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L41) | 返回驱动名。注释明确要求**全体系唯一**，用于标识驱动（例如偏好设置页的页面名） |
| `std::set<QString> GetAvailableDevices()` | [devicedriver.h:L46](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L46) | 列出当前能连到的设备序列号 |
| `bool connectTo(QString serial)` | [devicedriver.h:L55](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L55) | 按序列号连接。注意它是 **protected**——外部不能直接调，必须走 `connectDevice()` 公共入口（后面 4.3 详述） |
| `void disconnect()` | [devicedriver.h:L59](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L59) | 断开连接；未连接时无副作用 |
| `QString getSerial()` | [devicedriver.h:L66](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L66) | 返回已连接设备的序列号（未连接返回空串） |
| `Info getInfo()` | [devicedriver.h:L138](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L138) | 返回设备信息。注释约定：**`connectTo()` 一返回，本函数的结果就必须有效**；结果变化时要发 `InfoUpdated()` 信号 |
| `std::set<Flag> getFlags()` | [devicedriver.h:L173](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L173) | 返回当前激活的状态标志集合 |

**（3）能力描述三件套：`Feature` / `Info` / `Flag`。**

`Feature` 是设备「会做什么」的枚举（[devicedriver.h:L68-L85](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L68-L85)）：VNA 侧有 `VNA`、`VNAFrequencySweep`、`VNAPowerSweep`、`VNAZeroSpan`、`VNALogSweep`、`VNADwellTime` 六项；另有 `Generator`、`SA`、`SATrackingGenerator`、`SATrackingOffset`、`ExtRefIn`、`ExtRefOut`。配合非虚便捷函数 `supports()`（[devicedriver.h:L150](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L150)）一行代码即可查询。

`Info`（[devicedriver.h:L87-L128](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L87-L128)）不只说「会不会」，还说「能做到什么程度」：固件/硬件版本字符串、`supportedFeatures` 集合，以及按 VNA/Generator/SA 三组分节的 `Limits`——端口数、频率上下限、IF 带宽（或 RBW）上下限、最大点数、激励电平上下限、最大驻留时间。UI 用这些值来限定输入框的范围。

`Flag`（[devicedriver.h:L155-L164](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L155-L164)）是运行时的异常状态：`Overload`（输入过载）、`Unlocked`（PLL 失锁）、`Unlevel`（达不到设定输出幅度）、`ExtRef`（正在使用外部参考）。配套便捷函数 `asserted()`（[devicedriver.h:L187](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L187)）。

**（4）带默认实现的虚函数：不支持就返回 false。** 这是本接口最值得学习的模式。看四个配置函数的默认实现（全部一行）：

- [devicedriver.h:L332](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L332)：`setVNA(...)` 默认 `return false;`
- [devicedriver.h:L410](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L410)：`setSA(...)` 默认 `return false;`
- [devicedriver.h:L448](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L448)：`setSG(...)` 默认 `return false;`
- [devicedriver.h:L458](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L458)：`setIdle(...)` 默认 `return false;`

同样模式的还有 `availableVNAMeasurements()`（默认空表，[devicedriver.h:L324](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L324)）、`availableSAMeasurements()`（[devicedriver.h:L403](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L403)）、`availableSGPorts()`（[devicedriver.h:L442](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L442)）、外参考相关三件（[devicedriver.h:L464](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L464)、[devicedriver.h:L470](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L470)、[devicedriver.h:L478](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L478)）、`getStatus()` 默认空串（[devicedriver.h:L196](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L196)）、`createSettingsWidget()` 默认 `nullptr`（[devicedriver.h:L227](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L227)）、`updateFirmware()` 默认 `false`（[devicedriver.h:L563](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L563)）。

于是「写一个只支持频谱仪的驱动」完全合法：`setVNA` 不覆写即自动返回 `false`，VNA 模式据此提示不支持。

**（5）上层如何消费这套契约。** VNA 模式激活时先做能力检查，再订阅数据信号（[vna.cpp:L783-L797](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L783-L797)）：第 783 行检查 `Feature::VNA`，不支持则弹错误框并返回；第 787-794 行按 `VNALogSweep` / `VNAZeroSpan` / `VNAFrequencySweep` / `VNAPowerSweep` 逐项启用或禁用 UI 控件；第 797 行把驱动的 `VNAmeasurementReceived` 信号连到自己的 `NewDatapoint` 槽（`Qt::UniqueConnection` 防止重复连接——模式可能反复激活）。注意全程只出现 `DeviceDriver*`，没有任何具体驱动类型——这就是解耦的直接证据。

#### 4.1.4 代码实践

**实践：编写你的「新驱动接入清单」**（源码阅读型实践，无需硬件，约 40 分钟）

1. **实践目标**：不借助本讲义正文，只凭 `devicedriver.h` 的文件头注释与函数声明，产出一份可执行的新驱动开发清单，并用 `librevnadriver.h` 验证清单的正确性。

2. **操作步骤**：

   a. 打开 [devicedriver.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h)，从头到尾扫一遍，把所有虚函数抄进三栏表格：
   - A 栏「必须实现」（`= 0` 结尾的纯虚函数）；
   - B 栏「按能力实现」（有默认实现、且默认值表示「不支持」的虚函数）；
   - C 栏「无需实现」（非虚的便捷函数，如 `supports()`、`asserted()`）。

   b. 给 B 栏每一项标注「它对应 `Feature` 枚举里的哪一项，或哪类设备才会实现」。提示：对照 `Feature` 枚举（L68-L85）与 `Info::Limits` 的三个分节（VNA/Generator/SA）。

   c. 打开 [librevnadriver.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.h)，搜索 `override`，逐项核对你的表格：真实驱动覆盖了哪些？有没有「必须实现」却没出现在 `LibreVNADriver` 里的？（这是个关键陷阱，见下方「预期结果」。）

   d. 再打开 [librevnausbdriver.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.h) 搜索 `override`，回答：四个「缺失」的必须实现项到哪里去了？

3. **需要观察的现象**：
   - `LibreVNADriver` 覆盖了几乎所有 B 栏函数（`setVNA` L87、`setSA` L104、`setSG` L125、`setIdle` L135、`setExtRef` L155、`getStatus` L57、`createSettingsWidget` L69、`registerTypes` L163、`updateFirmware` L182……），说明官方设备是「全功能设备」。
   - 但 `getDriverName`、`GetAvailableDevices`、`connectTo`、`disconnect` 四个纯虚函数在 `LibreVNADriver` 里**一个都没有**——它自己仍然是抽象类，把「设备发现与连接」继续下放给传输层子类。

4. **预期结果**：`librevnausbdriver.h` 的 L23-L38 恰好补齐这四个函数（`getDriverName` / `GetAvailableDevices` / `connectTo` / `disconnect`）。由此得出结论：**驱动继承是两层的**——`DeviceDriver`（抽象契约）→ `LibreVNADriver`（协议逻辑，与传输无关）→ `LibreVNAUSBDriver` / `LibreVNATCPDriver`（传输后端）。这正是下一讲 u3-l2 的主题。另外注意 `LibreVNADriver` 还自己新增了一个纯虚函数 `SendPacket`（[librevnadriver.h:L180](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.h#L180)）——抽象类可以在继承链中间层继续「加码」。

#### 4.1.5 小练习与答案

**练习 1**：`setVNA()` 为什么不设计成纯虚函数，逼着每个驱动都实现？

**答案**：因为驱动生态里存在只支持部分功能的设备（例如纯频谱仪、纯信号源）。若设为纯虚，写一个频谱仪驱动也必须提供一个假的 `setVNA`。默认返回 `false` 的虚函数让「不支持」成为零成本默认行为，配合 `Feature`/`supports()` 查询，上层在调用前就能知道结果。这是「能力查询 + 优雅降级」的组合拳。

**练习 2**：`connectTo()` 为什么是 `protected`，而 `connectDevice()` 是 `public`？

**答案**：直接调用 `connectTo()` 会绕过全局的连接仲裁。`connectDevice()` 里维护着一个静态的 `activeDriver`：连接新设备前先把旧的活动驱动断开（见 [devicedriver.cpp:L34-L49](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L34-L49)）。把 `connectTo` 设为 protected 强制所有外部调用者走这条受控路径，保证同一时刻只有一个「正主」驱动占用设备。

**练习 3**：如果一台新设备的 `getInfo()` 在 `connectTo()` 返回后的第一次调用里还没填好固件版本，违反了什么约定？

**答案**：违反了 `getInfo()` 的文档注释约定（[devicedriver.h:L130-L138](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L130-L138)）：返回值必须在 `connectTo()` 返回后立即有效。上层（模式激活、UI 限值设定）在连接成功的下一个事件循环里就会读取它，读到空值会导致 UI 限值错乱甚至崩溃。允许「之后变化」，但变化时必须补发 `InfoUpdated()` 信号。

---

### 4.2 测量数据结构与信号

#### 4.2.1 概念说明

驱动抽象要成立，光有接口还不够，还得让**数据**也抽象。`VNAMeasurement` 和 `SAMeasurement` 就是两份「硬件无关的测量结果」。

设计上最关键的一点：测量值不放在固定字段里，而是放在 `std::map` 里，键是**字符串名字**。VNA 测量的键形如 `"S11"`、`"S21"`（注释明确说也允许任意其他名字，[devicedriver.h:L317-L324](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L317-L324)），SA 测量的键形如 `"PORT1"`。驱动先通过 `availableVNAMeasurements()` 声明自己会产出哪些名字，上层再据此创建 Trace。这样，一个 1 端口设备和 2 端口设备、甚至将来 8 端口复合设备（`maximumSupportedPorts = 8`，[devicedriver.h:L483](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L483)）都无需改动数据结构本身。

两个结构都为「零扫宽（zero span）」做了特殊设计：X 轴不再是频率而是时间。为此用了一个 `union` 让 `frequency`/`dBm` 与 `us`（微秒时间戳）**共享同一块内存**——这是 C 风格的节约手法，但也意味着读代码时必须知道当前处于哪种模式，不能同时读两者。

单位约定要格外留意：

- `VNAMeasurement::measurements` 存**线性复数**（实部/虚部），不是 dB（[devicedriver.h:L294-L297](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L294-L297)）；
- `SAMeasurement::measurements` 存**线性电压幅度**，`1.0` 即 `0 dBm`（[devicedriver.h:L389-L392](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L389-L392)）。

数据从驱动到 GUI 走 Qt 信号这条**推送**通道：驱动每完成一个测量点就 `emit` 一次，谁订阅谁处理。因为信号可能跨线程（驱动的接收线程 → GUI 线程），自定义类型需要 `Q_DECLARE_METATYPE` 注册（[devicedriver.h:L595-L596](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L595-L596)），驱动还可以在 `registerTypes()` 里补充注册自己的私有类型（[devicedriver.h:L493-L499](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L493-L499)）。

#### 4.2.2 核心流程

以一次 VNA 扫描为例，数据从硬件到屏幕的路径：

```text
设备（USB/TCP 原始字节）
  → 驱动接收线程解析出 Protocol 包          （u3-l2 / u4-l3 详述）
  → 驱动组装 DeviceDriver::VNAMeasurement：
        pointNum = 当前点号
        frequency/dBm 或 us（零扫宽）
        measurements["S11"] = 线性复数 ...
  → emit VNAmeasurementReceived(m)          （驱动侧终点）
  → VNA::NewDatapoint(m)                    （vna.cpp:797 连接）
  → 校准/去嵌入修正（u9 单元）
  → TraceModel 追加数据点                    （u8 单元）
  → 各 TracePlot 重绘
```

SA 路径完全对称：`emit SAmeasurementReceived(m)`（[devicedriver.h:L417-L422](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L417-L422)）。

配置方向则是「拉」：模式调用 `setVNA(VNASettings, cb)`（[vna.cpp:L2042](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L2042)），注意第二个参数是**回调**——异步配置完成后驱动调用它，VNA 模式在回调里做 `ResetLiveTraces()`（清空旧曲线）。配置类接口都是这种「请求 + 回调确认」风格，适配 USB 传输的异步性。

`VNAMeasurement` 还自带三个换算工具，供校准和数学模块使用：

- `toSparam()`：map → 矩阵；
- `fromSparam(S, portMapping)`：矩阵 → map，支持端口重排；
- `interpolateTo(to, a)`：两点线性插值。

其中插值的数学就是标准的线性插值，对每个分量做

\[ x_a = x_0\,(1-a) + x_1\,a, \qquad a\in[0,1] \]

#### 4.2.3 源码精读

**（1）`VNAMeasurement` 的字段与 union**（[devicedriver.h:L274-L314](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L274-L314)）：`pointNum` 从 0 开始编号；`Z0` 记录特征阻抗（通常 50 Ω），校准与阻抗再归一化要用到；union 里非零扫宽用 `frequency`+`dBm`，零扫宽用 `us`。`toSparam`/`fromSparam`/`interpolateTo` 只有声明，实现在 .cpp。

**（2）`toSparam()` 的实现**（[devicedriver.cpp:L66-L98](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L66-L98)）：当调用者不指定端口数（`ports == 0`）时，扫描所有键，从名字里抠出两个数字——`m.first.mid(1,1)` 是第 2 个字符（响应端口 to），`mid(2,1)` 是第 3 个字符（激励端口 from），取最大值作为矩阵阶数。然后构造 `Sparam` 矩阵并逐键填入。注意 `Sparam(n)` 的构造函数会把矩阵**零初始化**（[parameters.cpp:L120-L123](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Tools/parameters.cpp#L120-L123) 中 `Eigen::MatrixXd::Zero`），所以 map 里缺席的 S 参数在矩阵中就是 0。不以 `S` 开头的键直接跳过（L71-73、L89-91 的两处 `continue`）。

**（3）`fromSparam()` 的端口重排**（[devicedriver.cpp:L100-L116](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L100-L116)）：头文件里的注释（[devicedriver.h:L300-L311](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L300-L311)）给了个好例子——4 端口 S 参数矩阵想映射到 2 端口测量上，调用 `fromSparam(S, {2,4})` 表示「测量的端口 1 取矩阵的端口 2，测量的端口 2 取矩阵的端口 4」，于是矩阵的 S22 被存成测量的 "S11"。这个功能是为复合设备（把多台 2 端口机器拼成多端口，u3-l3 详述）准备的。

**（4）`interpolateTo()`**（[devicedriver.cpp:L118-L131](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L118-L131)）：对 `frequency`、`dBm`、`Z0` 和每个测量值做线性插值；若对方测量里缺少同名键，抛出 `std::runtime_error`。它服务于 Marker 插值（u8-l3）等需要「在两个采样点之间取值」的场景。

**（5）`SASettings` 的两个枚举**（[devicedriver.h:L341-L374](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L341-L374)）：`Window`（None/Kaiser/Hann/FlatTop）和 `Detector`（PPeak/NPeak/Sample/Normal/Average）——回顾 u1-l1：FPGA 片上加窗 + DFT 正是这些参数的硬件落点；跟踪源相关字段（`trackingGenerator`/`trackingPort`/`trackingOffset`/`trackingPower`）对应 `Feature::SATrackingGenerator`/`SATrackingOffset` 两项能力。`getSApoints()`（[devicedriver.h:L416](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L416)）单独存在是因为 SA 的点数由设备端根据 RBW/Span 自行决定，GUI 需要事后询问（静态包装版本见 [devicedriver.cpp:L57-L64](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L57-L64)，无活动驱动时兜底返回 1001）。

**（6）`SGSettings`**（[devicedriver.h:L425-L433](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L425-L433)）：信号源配置只有频率、电平、端口三个字段，端口编号从 1 开始，**设为 0 表示关闭全部输出**——一个用哨兵值表达「关」的小惯例。

#### 4.2.4 代码实践

**实践：纸面推演 `toSparam()`**（纸笔 + 源码对照，无需硬件，约 20 分钟）

1. **实践目标**：确认你真的读懂了 map → 矩阵的转换规则，而不只是「感觉懂了」。

2. **操作步骤**：

   a. 假设某个驱动发出这样一条测量（示例代码，非项目原有）：

   ```cpp
   DeviceDriver::VNAMeasurement m;
   m.pointNum = 5;
   m.frequency = 1e9;
   m.Z0 = 50.0;
   m.measurements["S11"] = {0.1, 0.2};   // 0.1 + 0.2j
   m.measurements["S13"] = {0.0, 0.1};
   m.measurements["S31"] = {0.3, 0.0};
   m.measurements["S33"] = {0.9, 0.0};
   m.measurements["TEMP"] = {42.0, 0.0}; // 故意混入的非 S 参数键
   auto S = m.toSparam();                // 不传 ports，自动推断
   ```

   b. 手工执行 [devicedriver.cpp:L66-L98](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L66-L98) 的算法，写出：矩阵阶数是多少？`S.get(1,3)`、`S.get(3,3)`、`S.get(1,2)` 各是多少？`"TEMP"` 键如何被处理？

   c. 再用 `fromSparam` 反向验证：调用 `m2.fromSparam(S, {1,3})`，写出 `m2.measurements` 里会更新哪些键、各自取矩阵的哪个元素。

3. **需要观察的现象 / 预期结果**：
   - 端口推断：出现的最大编号是 3（来自 S13/S31/S33），故 `ports = 3`，得到 3×3 矩阵；
   - `S.get(1,3) = 0.1j`（来自 "S13"），`S.get(3,3) = 0.9`，`S.get(1,2) = 0`（map 中缺席，矩阵零初始化所致）；
   - `"TEMP"` 不以 `S` 开头，两轮循环中都被 `continue` 跳过，不报错也不进矩阵；
   - `fromSparam(S, {1,3})` 只会更新 m2 中**已存在**的键（L111 的 `if(measurements.count(name))`）：即 "S11" ← S.get(1,1)、"S13" ← S.get(1,3)、"S31" ← S.get(3,1)、"S33" ← S.get(3,3)。
   
   全部推演结果「待本地验证」：如果你想跑真代码，可以把这段逻辑抄进 u1-l3 编译好的 `LibreVNA-Test` 测试工程里做成一个用例（u11-l1 会讲怎么做）。

4. **附加观察点**：注意 union 的隐患——上面示例设置了 `m.frequency` 就**绝不能再读 `m.us`**，两者是同一块内存。写驱动时零扫宽与非零扫宽是互斥路径。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `VNAMeasurement::measurements` 用 `std::map<QString, std::complex<double>>` 而不是固定的 `complex S11, S12, S21, S22` 四个成员？

**答案**：端口数因设备而异：LibreVNA 是 2 端口（4 个 S 参数），复合驱动可到 8 端口（64 个 S 参数），某些简易设备可能只有 S11。用字符串键的 map 让数据结构对端口数完全开放，配合 `availableVNAMeasurements()` 的「名字清单」约定，上层按清单创建 Trace 即可，无需为每种端口数定义新结构。

**练习 2**：SA 的测量值为什么规定「1.0 = 0 dBm」的线性电压，而不是直接存 dBm 数值？

**答案**：线性域的加减平均才有物理意义。频谱仪的 Average 检波、多次扫描平均（u7-l4 的 averaging）都需要在对数域取指数后再平均才正确；存储线性值让平均、归一化（SA Normalization 要做除法）等运算直接进行，只在最终显示时才转 dB。VNA 复数同理——校准（u9）是复数矩阵运算，必须在线性复数域完成。

**练习 3**：驱动为什么必须 `emit VNAmeasurementReceived`，而不是让 GUI 定时来调 `getMeasurement()` 之类的轮询接口？

**答案**：测量数据是异步到达的（USB/TCP 收包线程驱动），到达时刻不可预测。信号机制把「有新数据」的事件即时推送给任意多个订阅者（VNA 模式、流式服务器、包日志……），解耦了生产者与消费者，也天然支持跨线程（配 `Qt::QueuedConnection` 时需要 `Q_DECLARE_METATYPE` 注册类型）。轮询则会引入延迟与空转。

---

### 4.3 驱动注册与枚举

#### 4.3.1 概念说明

接口再好，也得有个地方「登记所有驱动」。很多项目用插件动态加载或宏自动注册，LibreVNA 选择了最朴素直接的方案：**手写的静态注册表函数 `getDrivers()`**。新增驱动 = 修改这个函数加一行 `push_back`——正是文件头注释的第四步。

注册表的设计要点：

- **懒加载单例**：函数内 `static` 局部变量，第一次调用时构造全部驱动，之后原样返回同一批指针。驱动实例全程序只有一份，从创建活到程序退出。
- **驱动名唯一**：`getDriverName()` 的注释要求全体系唯一，因为偏好设置页直接用驱动名生成设置页的对象名（[preferences.cpp:L160-L166](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.cpp#L160-L166)），重名会导致设置串页。
- **静态 `activeDriver` 全局唯一**：应用同一时刻只「认」一个活动驱动，由 `connectDevice()` 维护。

`connectDevice()` 还带一个初看费解的参数 `isIndepedentDriver`（注意这是源码里的真实拼写）：复合驱动（CompoundDriver）内部要替它管理的每台子设备建立连接，但这些子连接**不该抢走**全局的 `activeDriver` 主位——于是传 `true` 跳过仲裁逻辑（[compounddriver.cpp:L120](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddriver.cpp#L120) 中就是 `device->connectDevice(s, true)`）。

#### 4.3.2 核心流程

`getDrivers()` 的调用方遍布全程序，可归为四类：

```text
┌─ 偏好设置（preferences.cpp）
│    • load/store/setDefault：读/写每个驱动的 driverSpecificSettings()
│    • 构建设置对话框：调每个驱动的 createSettingsWidget()
│
├─ 设备枚举与连接（appwindow.cpp）
│    • UpdateDeviceList()：遍历驱动×序列号，生成 Device 菜单
│    • ConnectToDevice()：找到拥有该序列号的驱动，connectDevice()
│    • closeEvent()：程序退出时 delete 全部驱动实例
│
├─ 各测量模式（通过 window->getDevice() 拿到的指针）
│    • supports() 能力检查、setVNA/setSA/setSG 配置
│
└─ 复合驱动内部（compounddriver.cpp）
     • 以 isIndepedentDriver=true 连接各子设备
```

枚举菜单的形成过程（[appwindow.cpp:L905-L917](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L905-L917)）：清空菜单 → 双层循环「每个驱动的每个序列号」生成一个 `DeviceEntry{driver, serial}` → 若命令行指定了 `--device` 序列号则过滤不匹配项。连接时的驱动定位（[appwindow.cpp:L338-L344](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L338-L344)）：遍历所有驱动，谁的 `GetAvailableDevices()` 包含目标序列号就由谁负责 `connectDevice(serial)`（[appwindow.cpp:L382-L383](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L382-L383)）——序列号是跨驱动的全局命名空间。

#### 4.3.3 源码精读

**（1）注册表本体**（[devicedriver.cpp:L19-L32](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L19-L32)）：`static std::vector<DeviceDriver*> ret;` + `if (ret.size() == 0)` 判空后依次 `new` 六个驱动——官方 LibreVNA 的 USB 与 TCP 两个传输后端、把多台 LibreVNA 组合成多端口设备的 `CompoundDriver`、Siglent SSA3000X、Siglent SNA5000A、Harogic B60 三个第三方驱动（对应头文件包含见 [devicedriver.cpp:L3-L8](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L3-L8)）。要接入你的设备，就在这里加一行。

**（2）连接仲裁**（[devicedriver.cpp:L34-L49](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L34-L49)）：`connectDevice()` 先看 `activeDriver` 是否另有其人，是则让旧的先 `disconnect()`（L37-39）；再调子类的 `connectTo(serial)`，成功才把自己登记为新 `activeDriver`（L41-44）。断开侧的 `disconnectDevice()`（[devicedriver.cpp:L51-L55](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L51-L55)）则是 `disconnect()` + 清空 `activeDriver`。

**（3）驱动私有扩展的四个挂载点**（[devicedriver.h:L567-L589](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L567-L589)）：基类提供四个 protected 容器，子类**在构造函数里**填充——`specificActions`（出现在 Device 菜单里的动作）、`specificSettings`（并入全局偏好持久化，例如 LibreVNADriver 的 `harmonicMixing`、`SAUseDFT` 等七个开关，见 [librevnadriver.h:L226-L232](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.h#L226-L232)）、`specificSCPIcommands` / `specificSCPInodes`（挂到 `:DEV` SCPI 节点下，u10 单元会用到）。这是 u2-l3 讲过的 `SettingDescription` 描述表机制在驱动层的应用。

**（4）临时 SCPI 挂载与控制权信号**（[devicedriver.h:L501-L557](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L501-L557)）：除了构造时静态挂载，驱动还能在运行时发 `addSCPICommand`/`removeSCPICommand`/`addSCPINode`/`removeSCPINode` 信号临时增删 SCPI 命令（设备断开时自动撤销）；`ConnectionLost()` 通知上层意外掉线（注释明确说驱动只管发信号，善后由应用调 `disconnectDevice()` 完成，对应 [appwindow.cpp:L494-L499](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L494-L499) 的 `DeviceConnectionLost` 槽）；`acquireControl()`/`releaseControl()` 让驱动可以独占设备（固件升级对话框这类场景）。

**（5）生命周期的终点**（[appwindow.cpp:L290-L292](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L290-L292)）：`closeEvent` 里遍历 `getDrivers()` 逐个 `delete`——与懒加载首尾呼应，注册表中的实例确实是「与程序同寿」的。

#### 4.3.4 代码实践

**实践：清点 `getDrivers()` 的全部调用点并分类**（源码检索型实践，无需硬件，约 15 分钟）

1. **实践目标**：用一次真实的代码检索，验证 4.3.2 节画的调用方分布图，体会「注册表单例」被全程序共享的含义。

2. **操作步骤**：

   a. 在 `Software/PC_Application/LibreVNA-GUI` 目录下检索：

   ```bash
   grep -rn "getDrivers()" Software/PC_Application/LibreVNA-GUI --include="*.cpp"
   ```

   b. 对每个命中行，判断它属于四类中的哪一类：偏好设置 / 设备枚举与连接 / 模式配置 / 复合驱动内部。注意有一处特殊的：[appwindow.cpp:L290](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L290) 在 `closeEvent` 里是销毁。

   c. 追一条完整链路：从 GUI 菜单「Device → Connect to」某序列号出发，依次经过 `UpdateDeviceList()`（appwindow.cpp:906 起）→ `ConnectToDevice()`（appwindow.cpp:331 起）→ `connectDevice()`（devicedriver.cpp:34）→ 子类 `connectTo()`，把每一步的文件与行号记成清单。

3. **需要观察的现象**：
   - 命中行数在 10 处上下，全部集中在 `preferences.cpp` 与 `appwindow.cpp` 两个文件（模式代码通过 `window->getDevice()` 拿现成指针，不直接碰注册表）；
   - 这两个文件里没有任何 `new XXXDriver`——注册表之外的上层代码从不自行实例化驱动。唯一的例外藏在驱动层内部：`CompoundDriver` 会为自己的每台成员设备额外 new 一个 USB/TCP 驱动实例（[compounddriver.cpp:L103-L106](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddriver.cpp#L103-L106)），那些实例不进注册表、由复合驱动自建自管。

4. **预期结果**：你会得到一张「一个注册表、两类直接消费者」的清晰图景：preferences.cpp 消费的是驱动的**设置面**，appwindow.cpp 消费的是驱动的**连接面**；其余所有代码消费的都是抽象接口。如果你在 u1-l3 编译过 GUI，可以顺手启动它（不接硬件），观察 Device 菜单为空或仅含 TCP/演示设备的现象，与 `UpdateDeviceList()` 的逻辑互相印证——此项「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`getDrivers()` 用函数内 `static` 局部变量实现单例，相比在 `main()` 一开始就创建所有驱动，有什么好处和代价？

**答案**：好处是懒加载——程序启动路径（u2-l1 讲过 AppWindow 构造函数很「厚」）不会为用不到的驱动白付初始化成本，且不需要额外的初始化顺序协调（首次调用时必然已就绪）。代价是所有驱动在**第一次**调用时仍会被一次性全部构造（包括你从未插过的第三方仪器驱动），且 `if (ret.size() == 0)` 的判空依赖「只在 GUI 线程调用」的隐含约定，并无锁保护；对桌面单线程 GUI 而言可接受。

**练习 2**：两个驱动如果都声称发现了序列号 `"12345"`，`ConnectToDevice()` 会发生什么？

**答案**：遍历顺序即优先级——`getDrivers()` 向量中排在前面的驱动先被检查（[appwindow.cpp:L338-L344](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L338-L344) 的 for 循环命中即连），排在前面的 USB 驱动会赢。所以「序列号全局不冲突」是各驱动之间的君子协定，接口本身不强制（比如 LibreVNA 的 USB 与 TCP 驱动发现同一台实体设备时就靠 USB 在前解决）。

**练习 3**：为什么 `CompoundDriver` 给子设备建连接时要传 `isIndepedentDriver = true`？

**答案**：`CompoundDriver` 自己才是向 GUI 注册的「一台设备」。它内部替每台成员 LibreVNA 建立的连接若走默认仲裁，第一条子连接就会把自己登记为全局 `activeDriver`，把 `CompoundDriver` 本尊挤下台，后续连接还会互相踢。传 `true` 让子连接完全绕开 `activeDriver` 的登记与抢占（[devicedriver.cpp:L36-L44](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L36-L44)），只建立传输通道。

## 5. 综合实践

**综合任务：产出一份可直接照做的《新驱动接入清单 v1.0》**

把 4.1.4 的表格实践扩展成一份完整文档（建议直接写在你的笔记里，不要放进仓库），必须包含以下五部分：

1. **接口矩阵表**：行 = `devicedriver.h` 中的全部虚函数，列 = 「纯虚/默认实现」「所属阶段（发现/连接/能力/配置/数据/杂项）」「不实现时的语义」。纯虚函数至少 7 项、默认实现函数至少 12 项，逐一给出 `devicedriver.h` 行号。
2. **数据契约卡**：`VNAMeasurement` 与 `SAMeasurement` 各一张，写明字段含义、单位（线性复数 / 线性电压、1.0 = 0 dBm）、union 的两种互斥形态、键名约定（`availableVNAMeasurements()` 返回的名字必须与 map 键一致）。
3. **注册步骤**：从 `getDrivers()`（[devicedriver.cpp:L19](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L19)）出发列出新增驱动需要动的全部位置，并回答：新驱动应放进 `LibreVNA-GUI.pro` 的哪个列表？（提示：回顾 u1-l3 讲的「.pro 是活的文件索引」。）
4. **对照验证**：用 `librevnadriver.h` + `librevnausbdriver.h` 的 `override` 清单验证矩阵表，特别说明「四个纯虚函数被推迟到传输层子类实现」与「中间层新增纯虚 `SendPacket`」这两个继承链事实。
5. **能力决策模拟**：假设你要支持一台「只能做频谱分析、没有跟踪源、没有外参考」的虚拟仪器，从矩阵表勾选：哪些函数必须实现、哪些覆写后返回真实值、哪些干脆不覆写（用默认 `false`/空表表达不支持）？再对照仓库里现成的第三方驱动（`Device/SSA3000X/` 目录）看真实作者是怎么选的，比较异同。

完成标准：拿着这份清单，一个没读过 `devicedriver.h` 的同事也能准确说出「写一个最小可用驱动最少要实现哪 7 个函数、哪 1 个注册动作」。u11-l3 的毕业实战（实现一个 DemoDriver）将直接检验这份清单——现在写得越认真，那时越轻松。

## 6. 本讲小结

- `DeviceDriver` 是全 GUI 唯一的设备抽象：七个纯虚函数（`getDriverName` / `GetAvailableDevices` / `connectTo` / `disconnect` / `getSerial` / `getInfo` / `getFlags`）构成必须实现的「身份证」，配置类虚函数一律默认返回 `false`/空，让「不支持」零成本表达。
- 能力协商靠三件套：`Feature` 枚举 + `supports()` 查「会不会」，`Info::Limits` 查「能做到什么范围」，`Flag` + `FlagsUpdated()` 报运行异常；`getInfo()` 必须在 `connectTo()` 返回后立即有效。
- 测量数据同样是抽象的：`VNAMeasurement`（线性复数 map，键如 `"S11"`）与 `SAMeasurement`（线性电压 map，键如 `"PORT1"`，1.0 = 0 dBm），通过 `VNAmeasurementReceived` / `SAmeasurementReceived` 信号推送，`union` 兼容零扫宽的时间轴，`toSparam`/`fromSparam`/`interpolateTo` 提供矩阵换算与插值。
- 配置是「请求 + 回调」的异步风格（`setVNA(s, cb)`），数据是信号推送风格——方向相反的两条通道。
- `getDrivers()` 是手写懒加载注册表（六个驱动：USB、TCP、Compound、SSA3000X、SNA5000A、Harogic），驱动名必须全体系唯一；静态 `activeDriver` 保证全局只有一个活动设备，`isIndepedentDriver` 为复合驱动的子连接开旁门。
- 驱动私有扩展有四个挂载点（菜单动作 / 持久化设置 / SCPI 命令 / SCPI 节点），全部在构造函数里填进 protected 容器即可被框架接管。

## 7. 下一步学习建议

本讲只回答了「契约长什么样」，还没回答「契约是怎么被履行的」。建议按顺序继续：

1. **下一讲 u3-l2《LibreVNA 驱动：USB 与 TCP 两条通道》**：钻进 `librevnadriver.cpp` / `librevnausbdriver.cpp` / `librevnatcpdriver.cpp`，看「收到一包原始字节 → 解析 → 组装 `VNAMeasurement` → 发信号」的完整实现，本讲的四个纯虚函数在那里全部落地。
2. **u3-l3《驱动生态》**：对比三个第三方驱动如何用「默认返回 false」表达设备能力边界，以及 `CompoundDriver` 如何用 `Info::subset()`（本讲 4.2 出现过的 [devicedriver.cpp:L162-L198](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L162-L198)，端口数相加、频率区间取交集、Feature 取交集）合并多台设备的能力。
3. **提前翻一处**：`VNA::initializeDevice()`（vna.cpp:781 起）和 `ConfigureSweep`（vna.cpp:2042 附近）是上层消费本讲接口的最佳范本，读 u7-l1 前值得先自行走读一遍。
4. **远期**：u11-l3 毕业实战将要求你亲手实现一个 `DemoDriver`——把本讲综合实践产出的《新驱动接入清单》留着，那时直接照单施工。
