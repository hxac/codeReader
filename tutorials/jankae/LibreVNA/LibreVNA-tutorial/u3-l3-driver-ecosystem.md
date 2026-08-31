# 驱动生态：第三方仪器与复合设备

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `getDrivers()` 注册表的工作方式，以及把一个新驱动接入 GUI 需要改动的全部位置。
2. 对比第三方驱动（Siglent SSA3000X、SNA5000A、Harogic B60）与官方 LibreVNA 驱动在「能力声明」上的差异——哪些功能显式返回 `false`/空，哪些功能靠「不覆写」而天然缺席。
3. 解释 CompoundDriver 如何把多台 LibreVNA 组合成一台虚拟多端口设备：连接聚合、能力聚合（`Info::subset`）、按点号缓冲合并测量数据、软件触发转发。
4. 评估为「另一台仪器」编写驱动的切入点和最小工作量，写出一份可落地的驱动骨架方案。

## 2. 前置知识

本讲建立在前两讲（u3-l1 设备驱动抽象、u3-l2 USB/TCP 驱动）之上。开始前请确认你理解以下概念：

- **SCPI（可编程仪器标准命令）**：一种文本命令协议，市面上的台式仪器（Siglent、R&S、Keysight 等）几乎都支持。例如 `*IDN?` 查询仪器身份、`:FREQ:STAR 1000000` 设置起始频率。第 10 单元会精读 GUI 自己的 SCPI 框架，本讲只需要把 SCPI 当作「往 TCP 连接里写文本行」的命令语言即可。
- **LXI 仪器的两个约定端口**：很多网络仪器在 TCP `5024` 端口提供带欢迎横幅的交互式会话，在 `5025` 端口提供纯 SCPI 数据通道。本讲的两个 Siglent 驱动都利用了这一约定。
- **能力协商（回顾 u3-l1）**：`DeviceDriver` 的配置类虚函数（`setVNA`、`setSA`、`setSG` 等）在基类中**默认返回 `false` 或空容器**，表示「本驱动不支持」；驱动通过覆写 + `Info::supportedFeatures` + `Info::Limits` 声明真实能力。上层据此做优雅降级。
- **S 参数的比值定义**：\( S_{ij} = b_i / a_j \)，即「端口 j 激励时，端口 i 接收到的出射波与入射波之比」。在驱动实现里它体现为「接收机读数 ÷ 参考通道读数」。这一公式是理解 CompoundDriver 合并逻辑的钥匙。
- **SAMeasurement 的线性单位约定（回顾 u3-l1）**：频谱测量值存线性电压，`1.0` 对应 0 dBm，因此驱动要做换算 \( v = 10^{\mathrm{dBm}/20} \)。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h/.cpp` | 驱动抽象基类；本讲重点是文件头注释给出的「新增驱动步骤」、`getDrivers()` 注册表、`connectDevice()` 的全局仲裁、`Info::subset()` 能力聚合 |
| `Software/PC_Application/LibreVNA-GUI/Device/devicetcpdriver.h/.cpp` | TCP 驱动的通用中间层：搜索地址列表的持久化与设置界面，供基于 IP 的第三方驱动复用 |
| `Software/PC_Application/LibreVNA-GUI/Device/SSA3000X/ssa3000xdriver.h/.cpp` | Siglent SSA3000X 频谱仪驱动（纯 SA + 跟踪源 + 信号源，无 VNA 功能） |
| `Software/PC_Application/LibreVNA-GUI/Device/SNA5000A/sna5000adriver.h/.cpp` | Siglent SNA5000A 系列矢量网络分析仪驱动（VNA + 信号源，无 SA 功能） |
| `Software/PC_Application/LibreVNA-GUI/Device/Harogic/harogicb60.h/.cpp` | Harogic B60 驱动：直接继承官方 USB 驱动、只换 USB VID/PID 的「最省事」接入方式 |
| `Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddriver.h/.cpp` | 复合驱动：把多台 LibreVNA 聚合成一台虚拟多端口设备 |
| `Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddevice.h/.cpp` | 复合设备的配置数据模型（名称、同步方式、成员序列号、端口映射） |
| `Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro` | 工程文件索引：新驱动文件必须在此登记才会参与编译 |

## 4. 核心概念与源码讲解

### 4.1 模块一：`getDrivers()` 驱动注册表

#### 4.1.1 概念说明

u3-l1 讲过「依赖倒置」：VNA/频谱仪/信号源三种模式只持有 `DeviceDriver*` 指针，不关心背后是哪台仪器。但 GUI 总得有个地方知道「这个世界上有哪些驱动可用」——这就是 `DeviceDriver::getDrivers()` 注册表。

devicedriver.h 的文件头注释直接给出了新增驱动的官方步骤清单：

> To add support for a new hardware device perform the following steps:
> - Derive from this class（继承本类）
> - Implement all pure virtual functions（实现所有纯虚函数）
> - Implement the virtual functions if the device supports the specific function（设备支持的虚函数才覆写）
> - Add the new driver to getDrivers()（把新驱动加进 getDrivers()）

见 [Device/devicedriver.h:4-12](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L4-L12)。这份注释是整个驱动生态的「总纲」，本讲的所有例子都是它的具体化。

注册表本身是一个**懒加载的函数级静态单例**：第一次调用时 new 出全部六个驱动实例，之后直接返回同一个 vector。没有任何宏、插件机制或动态加载——加驱动就是加一行 `push_back`。

#### 4.1.2 核心流程

```text
GUI 需要枚举设备（如启动、点击刷新）
    └─> 遍历 DeviceDriver::getDrivers() 返回的六个驱动实例
         ├─ 对每个驱动调用 GetAvailableDevices()（各驱动自己发现设备）
         └─ 汇总成「驱动 × 序列号」的设备列表显示给用户
用户选择某台设备连接
    └─> DeviceDriver::connectDevice(serial)
         ├─ 若旧的活动驱动存在且不是自己 → 先断开它（全局唯一活动设备）
         ├─ 调用虚函数 connectTo(serial)（由具体驱动实现）
         └─ 成功则记录静态 activeDriver
```

当前注册的六个驱动及其名字（`getDriverName()` 必须全局唯一，它是区分驱动的键）：

| 驱动类 | 名字 | 基类 | 一句话定位 |
| --- | --- | --- | --- |
| `LibreVNAUSBDriver` | `LibreVNA/USB` | `LibreVNADriver` | 官方设备，USB 直连 |
| `LibreVNATCPDriver` | `LibreVNA/TCP` | `LibreVNADriver` | 官方设备，网络连接（SSDP 发现） |
| `CompoundDriver` | `LibreVNA/Compound` | `DeviceDriver` | 多台官方设备的虚拟聚合体 |
| `SSA3000XDriver` | `SSA3000X` | `DeviceTCPDriver` | Siglent 频谱仪 |
| `SNA5000ADriver` | `SNA5000A` | `DeviceTCPDriver` | Siglent 矢网 |
| `HarogicB60` | `Harogic B60` | `LibreVNAUSBDriver` | 兼容官方协议的第三方 USB 设备 |

注意这个清单体现的三种接入姿势：从 `DeviceDriver` 直接继承（CompoundDriver）、从通用 TCP 中间层继承（两个 Siglent）、从现成的官方驱动继承只改身份（Harogic）。

#### 4.1.3 源码精读

注册表本体，六行 `push_back` 就是驱动生态的全部入口：

[Device/devicedriver.cpp:19-32](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L19-L32)

```cpp
std::vector<DeviceDriver *> DeviceDriver::getDrivers()
{
    static std::vector<DeviceDriver*> ret;
    if (ret.size() == 0) {
        // first function call
        ret.push_back(new LibreVNAUSBDriver);
        ret.push_back(new LibreVNATCPDriver);
        ret.push_back(new CompoundDriver);
        ret.push_back(new SSA3000XDriver);
        ret.push_back(new SNA5000ADriver);
        ret.push_back(new HarogicB60);
    }
    return ret;
}
```

这段代码做了两件事：懒加载（首次调用构造全部实例）+ 静态缓存（进程生命周期内实例常驻）。实例常驻是有意为之——驱动的 `specificSettings` 等状态要跨连接保留。

与注册表配套的是 u3-l1 提过的「全局唯一活动设备」仲裁，这里能看到完整实现：

[Device/devicedriver.cpp:34-49](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L34-L49)

```cpp
bool DeviceDriver::connectDevice(QString serial, bool isIndepedentDriver)
{
    if(!isIndepedentDriver) {
        if(activeDriver && activeDriver != this) {
            activeDriver->disconnect();
        }
    }
    if(connectTo(serial)) {
        if(!isIndepedentDriver) {
            activeDriver = this;
        }
        ...
```

u3-l1 只说了「`isIndepedentDriver` 为复合驱动旁路」，本讲 4.3 会看到 CompoundDriver 正是以 `connectDevice(s, true)` 的方式同时持有 N 个「子活动设备」——静态 `activeDriver` 只指向 CompoundDriver 自己，成员设备绕开仲裁。这就是这个布尔参数存在的全部理由。

最后是工程文件登记。回忆 u1-l3 的结论：「`.pro` 是工程的单一事实来源，未登记的文件不参与编译」。六个驱动的头文件与源文件全部列在：

[LibreVNA-GUI.pro:23-50](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L23-L50)（HEADERS 段）与 [LibreVNA-GUI.pro:195-222](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L195-L222)（SOURCES 段）。

因此「接入一个新驱动」的完整改动清单是**三处**：

1. 新建驱动类（继承 `DeviceDriver` / `DeviceTCPDriver` / 现成驱动）；
2. 在 `getDrivers()` 里 `push_back` 一行；
3. 在 `LibreVNA-GUI.pro` 的 HEADERS/SOURCES 里登记新文件。

#### 4.1.4 代码实践

**实践目标**：不写代码，纯走读——验证「注册表 → 工程文件」的对应关系，建立改动手感。

**操作步骤**：

1. 打开 [Device/devicedriver.cpp:19-32](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L19-L32)，抄下六个类名。
2. 用编辑器在 `LibreVNA-GUI.pro` 中搜索这六个类对应的 `.h`/`.cpp`，确认每个都被登记（例如搜 `harogic`、`ssa3000x`）。
3. 对每个类，找到它 `getDriverName()` 的实现位置，填出 4.1.2 的表格中「名字」一列（本文已给出答案，先自己找再对照）。
4. 思考题先记下来：`HarogicB60` 继承自 `LibreVNAUSBDriver`，而 `getDrivers()` 里 `LibreVNAUSBDriver` 和 `HarogicB60` **同时**注册——两个驱动实例会各自做 USB 枚举，这是否会有冲突？（答案见 4.2.3 的 Harogic 部分。）

**需要观察的现象**：`.pro` 中六个驱动文件一一对应；`getDriverName()` 分布在四处（两个 Siglent 驱动写在头文件内联，Compound 内联，官方 USB/TCP 驱动写在 cpp，Harogic 写在 cpp）。

**预期结果**：能独立说出「新增一个驱动要改哪三个地方」。本实践为纯源码走读，无需运行，也无需硬件。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `getDrivers()` 用「函数内 static vector」而不是全局变量或单例类的成员？

**答案**：函数级静态变量保证首次调用时才构造（懒加载），避免静态初始化顺序问题（驱动构造函数依赖 `Preferences::getInstance()` 等，全局静态对象的构造顺序不可控）；同时进程内只有一份，等价于单例，且不需要额外的访问接口。这是 C++ 中常见的「构造时即可用」单例写法。

**练习 2**：如果把新驱动类写好、加进了 `getDrivers()`，却忘了登记 `.pro`，会发生什么？

**答案**：编译直接失败——`devicedriver.cpp` 里 `#include` 不到新驱动的头文件（HEADERS 未登记本身不阻止 include，但 SOURCES 未登记意味着新类的成员函数没有编译产物），链接阶段报「undefined reference to vtable/成员函数」。这正是 u1-l3 强调 `.pro` 是「活的文件索引」的原因。

**练习 3**：两个驱动的 `getDriverName()` 返回了同一个字符串，会出现什么问题？

**答案**：驱动名被用作区分驱动的唯一键（头文件注释明确要求 "It must be unique across all implemented drivers"）。重名后，依赖名字查找驱动的代码（如保存的设置、按驱动名索引的界面）会命中错误的对象，行为未定义。Harogic 驱动构造函数里专门替换设置项名前缀（见 4.2.3），就是为了和父类驱动的设置键区分开。

### 4.2 模块二：第三方驱动两例——SSA3000X 与 SNA5000A

#### 4.2.1 概念说明

为什么要为别家仪器写驱动？因为 LibreVNA-GUI 的价值大半在「屏幕这边」：Trace 数据模型、Smith 图/瀑布图、Marker、校准、数学运算、SCPI 远程控制。任何一台能吐出测量数据的仪器，只要接上 `DeviceDriver` 这层适配器，就能白得整套 GUI。两个 Siglent 驱动就是最好的证明——一台纯频谱仪（SSA3000X）和一台纯矢网（SNA5000A），能力面完全不同，却都能插进同一个 GUI。

两个驱动有共同骨架，值得先抽象出来看：

- 都继承自 `DeviceTCPDriver`（而非直接继承 `DeviceDriver`），因为都是「在若干已知 IP 地址上找仪器」的网络设备；
- 发现流程相同：连 `5024` 端口读欢迎横幅 → 确认仪器型号前缀 → 发 `*IDN?` 拿序列号；
- 数据通道相同：连 `5025` 端口收发 SCPI；
- 数据回传方式相同：**轮询式**——驱动自己定时向仪器要迹线数据（`:TRAC? 1` 或 `:CALC:DATA:XAXIS?`），解析后转成 `DeviceDriver::SAMeasurement`/`VNAMeasurement` 发信号。这与官方 LibreVNA 驱动的「设备主动推二进制包」形成鲜明对比（u3-l2）。

两者的差异恰恰是本讲的学习重点——**同一套抽象下，能力声明如何各裁各地**：

| 能力 | SSA3000X | SNA5000A |
| --- | --- | --- |
| VNA 测量 | ❌（不覆写 `setVNA`，继承基类默认 `false`） | ✅（覆写，且支持 `VNAFrequencySweep`） |
| SA 测量 | ✅（含跟踪源 `SATrackingGenerator`） | ❌（不覆写 `setSA`） |
| 信号源 | ✅（跟踪源兼作 CW 源） | ✅（借 SA 模式的源控制实现） |
| 外部参考 | ❌（`setExtRef` 显式 `return false`） | ❌（同样显式 `return false`） |
| Limits 来源 | 硬编码在驱动里（按型号分支） | 连接时向仪器查询（`:SERVICE:` 命令） |
| 端口数 | 固定 1 | 连接时查询，2 或 4 |

「能力缺席」的两种写法都出现了：**隐式**（不覆写，继承 `devicedriver.h` 中默认返回 `false` 的实现，如 [Device/devicedriver.h:332](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L332) 的 `setVNA`）和**显式**（覆写但返回 `false` 或占位值，如两个驱动的 `setExtRef`）。隐式缺席是最省事的「不支持」——这也是基类默认值设计的意图。

#### 4.2.2 核心流程

**SSA3000X 的生命周期**（频谱仪，无 VNA 功能）：

```text
构造：创建 TraceDifferenceGenerator（点级去重）+ 单次触发的轮询定时器
  │
  ├─ GetAvailableDevices()：对每个搜索 IP 连 5024
  │     读横幅 "Welcome to the SCPI instrument 'Siglent SSA3..."
  │     发 *IDN? → 取第 3 字段为序列号 → 记录 序列号→IP 映射
  │
  ├─ connectTo(serial)：连 5025 数据口
  │     *IDN? 解析 4 字段（厂商,型号,序列号,固件）
  │     按型号定 maxFreq（SSA3032X→3.2G / SSA3021X→2.1G，其他报错）
  │     填 Info：features = {SA, SATrackingGenerator, Generator}，SA/Generator 各项 Limits
  │     *RST 复位仪器
  │
  ├─ setSA(s)：翻译成 SCPI（起止频率/RBW/窗/检波器/跟踪源开关与功率）
  │     启动 100ms 轮询定时器
  │
  ├─ extractTracePoints()（定时器触发）：
  │     发 ":TRAC? 1" → 等一行 CSV（100ms 超时）
  │     每点按位置线性插值出频率，dBm → 线性电压
  │     diffGen->newTrace()（只上报变化的点）→ emit SAmeasurementReceived
  │     重启定时器（循环轮询）
  │
  └─ setIdle()：停定时器 + *RST
```

**SNA5000A 的差异点**：

```text
connectTo：Limits 不硬编码，而是 queryInt() 向仪器查询
    ":SERVICE:PORT:COUNT?" → 端口数
    ":SERVICE:SWEEP:FREQENCY:MINIMUM?" / "...MAXIMUM?" → 频率范围（命令拼写原文如此）
    ":SERVICE:SWEEP:POINTS?" → 最大点数
setVNA：为每个激励端口开一条仪器迹线（:CALC:PARn:DEF Snm 强制激励留在该端口）
取数：traceReader 状态机——先问 ":CALC:DATA:XAXIS?" 拿频率轴，
    再逐个问 ":SENS:DATA:RAWD? Sij" 拿每个 S 参数的实部/虚部交错数组，
    凑齐后按点组装成 VNAPoint
特殊处理：SNA5000A 一次扫只激励一个端口（S11/S21 一扫、S12/S22 另一扫），
    未测到的参数表现为极小值（|值| < 1e-10 ≈ -196 dB）→ 驱动把这些不完整点整段丢弃
```

#### 4.2.3 源码精读

**（1）DeviceTCPDriver：TCP 驱动的通用中间层**

类注释写明了它的定位——如果你的设备要在特定 IP 地址上搜索，就继承它而不是 `DeviceDriver`：[Device/devicetcpdriver.h:8-18](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicetcpdriver.h#L8-L18)，类声明在 [Device/devicetcpdriver.h:20-44](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicetcpdriver.h#L20-L44)。

它做的事非常聚焦：把「搜索地址列表」变成驱动专属设置并持久化。构造函数向 `specificSettings` 注册一条逗号分隔的地址串（回忆 u2-l3 的 SettingDescription 机制）：[Device/devicetcpdriver.cpp:7-10](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicetcpdriver.cpp#L7-L10)

```cpp
DeviceTCPDriver::DeviceTCPDriver(QString driverName)
{
    specificSettings.push_back(Savable::SettingDescription(&searchAddressString, driverName+".searchAddressString", ""));
}
```

子类在 `GetAvailableDevices()` 里调用受保护的 `getSearchAddresses()` 拿到地址列表去逐个探测（[Device/devicetcpdriver.cpp:63-76](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicetcpdriver.cpp#L63-L76)）；`createSettingsWidget()` 则提供编辑地址列表的 UI（增删改 IP，[Device/devicetcpdriver.cpp:12-61](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicetcpdriver.cpp#L12-L61)）。注意设置键里带 `driverName` 前缀——SSA3000X 和 SNA5000A 的地址列表各自独立存储。

**（2）SSA3000X：设备发现与能力声明**

发现逻辑——探测 5024 欢迎横幅确认仪器类型，再用 `*IDN?` 拿序列号：

[Device/SSA3000X/ssa3000xdriver.cpp:28-60](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/SSA3000X/ssa3000xdriver.cpp#L28-L60)

```cpp
for(auto address : getSearchAddresses()) {
    sock.connectToHost(address, 5024);
    if(sock.waitForConnected(50)) {
        sock.waitForReadyRead(100);
        auto line = QString(sock.readLine());
        if(line.startsWith("Welcome to the SCPI instrument 'Siglent SSA3")) {
            ...
            sock.write("*IDN?\r\n");
            ...
            detectedDevices[fields[2]] = address;   // 序列号 → IP
            ret.insert(fields[2]);
```

连接成功后的能力声明是本讲的核心样本——features 只声明仪器真有的三项（`ExtRefIn` 那行被注释掉了，说明作者评估过、放弃了），Limits 按型号硬编码分支：

[Device/SSA3000X/ssa3000xdriver.cpp:108-138](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/SSA3000X/ssa3000xdriver.cpp#L108-L138)

```cpp
info.supportedFeatures.insert(DeviceDriver::Feature::SA);
info.supportedFeatures.insert(DeviceDriver::Feature::SATrackingGenerator);
info.supportedFeatures.insert(DeviceDriver::Feature::Generator);
//    info.supportedFeatures.insert(DeviceDriver::Feature::ExtRefIn);

double maxFreq = 0;
if(info.hardware_version == "SSA3032X") {
    maxFreq = 3200000000;
} else if(info.hardware_version == "SSA3021X") {
    maxFreq = 2100000000;
} ...
info.Limits.SA.ports = 1;
info.Limits.SA.maxRBW = 3000000;
info.Limits.SA.mindBm = -20;
...
```

`setSA` 把 GUI 的 `SASettings` 逐项翻译成 SCPI，其中有一处值得注意的「能力妥协」——仪器没有 Kaiser 窗，静默降级为 Hamming：

[Device/SSA3000X/ssa3000xdriver.cpp:191-207](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/SSA3000X/ssa3000xdriver.cpp#L191-L207)

```cpp
case SASettings::Window::Kaiser:
    windowName = "HAMMing"; // kaiser is not available
    break;
```

（另一处历史遗留：同函数 [ssa3000xdriver.cpp:184-189](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/SSA3000X/ssa3000xdriver.cpp#L184-L189) 中 `:FREQ:STAR` 被连续写了四次，功能无害但明显是未清理的调试残留，读代码时不要被它迷惑。）

**（3）SSA3000X：显式的「不支持」**

外部参考相关三个函数是「设备能力有限，接口返回 false/空」的标准样本：

[Device/SSA3000X/ssa3000xdriver.cpp:280-295](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/SSA3000X/ssa3000xdriver.cpp#L280-L295)

```cpp
QStringList SSA3000XDriver::availableExtRefInSettings()
{
    return {""};
}
...
bool SSA3000XDriver::setExtRef(QString option_in, QString option_out)
{
    Q_UNUSED(option_in)
    Q_UNUSED(option_out)
    return false;
}
```

注意细节：`availableExtRefInSettings()` 返回的是**含一个空串的列表**而不是空列表——UI 上会显示一个空白选项；真正拒绝动作的是 `setExtRef` 的 `return false`。而 VNA 能力则是纯隐式缺席——整个 `ssa3000xdriver.h`（[Device/SSA3000X/ssa3000xdriver.h:12-171](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/SSA3000X/ssa3000xdriver.h#L12-L171)）里找不到 `setVNA` 和 `availableVNAMeasurements` 的声明，继承基类默认实现即可。

**（4）SNA5000A：Limits 从仪器查询 + 隐式的「不支持 SA」**

SNA5000A 的能力声明更「活」——Limits 不硬编码，连接时问仪器自己：

[Device/SNA5000A/sna5000adriver.cpp:131-149](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/SNA5000A/sna5000adriver.cpp#L131-L149)

```cpp
info.supportedFeatures.insert(DeviceDriver::Feature::VNA);
info.supportedFeatures.insert(DeviceDriver::Feature::VNAFrequencySweep);
info.supportedFeatures.insert(DeviceDriver::Feature::Generator);

// Extract limits
info.Limits.VNA.ports = queryInt(":SERVICE:PORT:COUNT?");
info.Limits.VNA.minFreq = queryInt(":SERVICE:SWEEP:FREQENCY:MINIMUM?");   // 命令拼写原文如此
info.Limits.VNA.maxFreq = queryInt(":SERVICE:SWEEP:FREQENCY:MAXIMUM?");
info.Limits.VNA.maxPoints = queryInt(":SERVICE:SWEEP:POINTS?");
```

`queryInt`/`query`/`waitForLine` 是驱动私有的同步问答工具（[Device/SNA5000A/sna5000adriver.cpp:305-336](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/SNA5000A/sna5000adriver.cpp#L305-L336)、[Device/SNA5000A/sna5000adriver.cpp:533-545](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/SNA5000A/sna5000adriver.cpp#L533-L545)）：写一行命令、阻塞等一行应答、超时返回空串。

它声明的 S 参数清单按端口数分支（2 端口 4 项、4 端口 16 项）：

[Device/SNA5000A/sna5000adriver.cpp:183-193](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/SNA5000A/sna5000adriver.cpp#L183-L193)

SA 能力同样是隐式缺席——`sna5000adriver.h`（[Device/SNA5000A/sna5000adriver.h:12-192](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/SNA5000A/sna5000adriver.h#L12-L192)）中没有 `setSA`；而外部参考是显式拒绝：

[Device/SNA5000A/sna5000adriver.cpp:292-297](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/SNA5000A/sna5000adriver.cpp#L292-L297)

```cpp
bool SNA5000ADriver::setExtRef(QString option_in, QString option_out)
{
    Q_UNUSED(option_in)
    Q_UNUSED(option_out)
    return false;
}
```

**（5）SNA5000A：多扫描合并——第三方驱动里最有工程含金量的一段**

矢网测量 S 参数矩阵需要逐端口激励，SNA5000A 一屏扫只能激励一个端口，而 GUI 期望每个测量点携带全部 S 参数。驱动的处理在源码注释里写得很清楚：

[Device/SNA5000A/sna5000adriver.cpp:418-446](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/SNA5000A/sna5000adriver.cpp#L418-L446)

```cpp
/*
 * The SNA5000A performs the measurements in multiple sweeps ...
 * Values that are not measured yet are reported as very small values ...
 */
// threshold equals -196dB, we can safely assume that no real measurement will ever below that
constexpr double threshold = 1e-10;
int lastIndex = -1;
for(unsigned int i=0;i<traceReader.xaxis.size();i++) {
    for(auto d : traceReader.data) {
        if(abs(d.second[i*2]) < threshold && abs(d.second[i*2+1]) < threshold) {
            lastIndex = i;
            break;
        }
    }
    ...
```

从头扫描每个点，一旦发现某个 S 参数的实部虚部都小于阈值（即仪器还没测到它），就把该点之前的所有点判为「未完成」并丢弃，只上报完整段。代价是显示延迟，收益是 GUI 永远拿到完整数据点。这是「适配器驱动要替仪器擦屁股」的典型例子。

支撑它的是轮询状态机：状态 0 问频率轴，状态 1..n² 问每个 S 参数，全部到齐组装上报后回到状态 0：

[Device/SNA5000A/sna5000adriver.cpp:507-531](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/SNA5000A/sna5000adriver.cpp#L507-L531)

**（6）Harogic B60：第三种接入姿势**

Harogic 驱动展示了当第三方设备**兼容官方协议**时能有多省事——整个驱动只有十几行：

[Device/Harogic/harogicb60.h:6-17](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/Harogic/harogicb60.h#L6-L17) 声明它直接继承 `LibreVNAUSBDriver`；[Device/Harogic/harogicb60.cpp:3-16](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/Harogic/harogicb60.cpp#L3-L16) 是全部实现：

```cpp
HarogicB60::HarogicB60()
{
    validUSBIDs.clear();
    validUSBIDs.append({0x367F, 0x0200, "B60"});

    for(auto &s : specificSettings) {
        s.name.replace("LibreVNAUSBDriver", "HarogicB60Driver");
    }
}

QString HarogicB60::getDriverName()
{
    return "Harogic B60";
}
```

三件事：清空父类构造的 USB VID/PID 白名单换成自己的（这回答了 4.1.4 的思考题——两个驱动各自按自己的 VID/PID 枚举，不会抢同一台设备）；把继承来的设置键名里的前缀替换掉避免和父类驱动共用同一份设置；改驱动名。协议层一行不动，全盘复用。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：亲手找出两个驱动中「设备能力有限，接口返回 false/空」的实现各一处，并完成一份约 300 字的《为某台仪器设计驱动骨架》方案。

**操作步骤**：

1. 在 `ssa3000xdriver.cpp` 中定位一处显式不支持：打开 [Device/SSA3000X/ssa3000xdriver.cpp:290-295](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/SSA3000X/ssa3000xdriver.cpp#L290-L295)，记录 `setExtRef` 返回 `false`，以及上文 `availableExtRefInSettings()` 返回 `{""}`。再找一处隐式不支持：在整个文件里搜索 `setVNA`——搜不到，因为它继承 [Device/devicedriver.h:332](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.h#L332) 的默认 `return false`。
2. 在 `sna5000adriver.cpp` 中同样定位：[Device/SNA5000A/sna5000adriver.cpp:292-297](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/SNA5000A/sna5000adriver.cpp#L292-L297) 的 `setExtRef` 显式 `false`；隐式的是 `setSA`（头文件里没有声明）。
3. 梳理「不支持」在这个代码库里的三级表达，写成你的笔记：
   - 隐式：不覆写，继承基类默认 `false`/`{}`；
   - 显式：覆写并返回 `false` 或占位值（`{""}`、`{""}`）；
   - 降级：功能名义支持但换实现（Kaiser→Hamming），或过滤不可用数据（未完成点丢弃）。
4. 写 300 字方案。选一台你手边的仪器（或假想仪器，如「一台只有 USB 口的简易射频功率计」），回答五个问题：
   - 继承谁？（有 LAN+SCPI → `DeviceTCPDriver`；兼容官方协议 → 直接继承官方驱动；其他 → `DeviceDriver`）
   - `Info::supportedFeatures` 声明哪几项？
   - `Info::Limits` 各项填多少、是硬编码还是连接时查询？
   - `GetAvailableDevices()` 怎么发现设备、`connectTo()` 怎么确认「这真的是我的仪器」？（横幅？`*IDN?`？VID/PID？）
   - 测量数据怎么回来？（设备主动推？轮询查询？）需要哪些格式换算（dBm → 线性电压等）？

**需要观察的现象**：两个驱动的「能力缺口」分布完全不同——SSA3000X 缺整个 VNA 面，SNA5000A 缺整个 SA 面，但两者都不需要为此写任何「报错代码」，基类默认值 + features 集合自动让 UI 把对应模式置灰/拒绝配置。

**预期结果**：得到一张「能力缺席三写法」对照笔记 + 一份可据以动工的 300 字驱动方案（方案参考框架见本文 5. 综合实践，可先自己写再对照）。本实践为纯源码走读与写作，无需硬件；300 字方案属于设计文档，不涉及运行验证。

#### 4.2.5 小练习与答案

**练习 1**：SSA3000X 驱动把 `SAMeasurement::measurements["PORT1"]` 赋值为 `pow(10.0, p.dBm / 20.0)`（[Device/SSA3000X/ssa3000xdriver.cpp:12-18](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/SSA3000X/ssa3000xdriver.cpp#L12-L18)）。为什么是除以 20 而不是 10？

**答案**：`SAMeasurement` 的约定是**线性电压**（1.0 = 0 dBm），不是功率。dBm 以 1mW 为基准是功率量级：\( P_{\mathrm{dBm}} = 10\lg(P/1\mathrm{mW}) \)；而同一阻抗下功率正比于电压的平方，\( P \propto V^2 \)，所以 \( V/V_0 = 10^{\mathrm{dBm}/20} \)。若存的是线性功率才用 `/10`。这呼应 u3-l1 讲过的数据结构契约——单位错了 GUI 上所有幅度显示会差一倍。

**练习 2**：两个 Siglent 驱动的 `write()` 在写完命令后立刻 `dataSocket.readAll()`（[Device/SSA3000X/ssa3000xdriver.cpp:297-301](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/SSA3000X/ssa3000xdriver.cpp#L297-L301)）。这一行看似无用，作用是什么？

**答案**：清空接收缓冲里的「不速之客」。SCPI 仪器可能在任何时候输出异步消息（状态报告、错误队列提示等）；如果不清掉，这些字节会留在缓冲区里，被后续 `readLine()` 误当成查询应答，导致解析错位。`readAll()` 把残留字节丢弃，保证每次「问一句等一句」的同步语义干净。这也暴露了这类驱动的简化假设：串行阻塞式问答，不处理仪器主动上报。

**练习 3**：SSA3000X 与官方 LibreVNA 驱动（u3-l2）在「数据如何到达 GUI」上的根本区别是什么？各自的适用场景？

**答案**：官方驱动是**设备推送**——设备端固件扫完一个点就主动发二进制 `VNADatapoint` 包，USB/TCP 接收线程解码后发信号，实时性和带宽利用率高，但要求对设备协议有完全控制权。SSA3000X 是**主机轮询**——驱动每 100ms 用 QTimer 拉 `:TRAC? 1` 的 CSV 文本，简单通用（任何支持 SCPI 查询的仪器都能套用），但延迟高、吞吐低、还要靠 `TraceDifferenceGenerator` 做点级去重来弥补轮询重复上报。给「黑盒仪器」写驱动时轮询几乎是唯一选择，这也解释了为什么两套官方驱动之外的驱动全部走轮询路线。

### 4.3 模块三：CompoundDriver 组合逻辑

#### 4.3.1 概念说明

单台 LibreVNA 只有 2 个测试端口，只能测 2 端口 DUT 的 S 参数。想测 4 端口 DUT（差分线对、滤波器组）？买 4 端口矢网要花大钱。CompoundDriver 的思路是：**多台 LibreVNA + 软件聚合 = 一台虚拟多端口 VNA**。几台设备通过外部触发线（或纯软件同步）步调一致地扫描，驱动在 PC 端把各自的接收数据拼成完整的 S 参数矩阵。

CompoundDriver 是一个很好的「架构教科书」案例，因为它从 `DeviceDriver` **直接**继承（不继承 `LibreVNADriver`），却能把 `LibreVNADriver` 当作「子设备」来编排——抽象基类在这里同时扮演了两个角色：对外它是虚拟设备的驱动，对内它是官方驱动的消费者。这正是 u3-l1 依赖倒置的完整闭环。

配置数据模型 `CompoundDevice`（[Device/LibreVNA/Compound/compounddevice.h:11-36](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddevice.h#L11-L36)）只有四个字段，但 `portMapping` 是理解一切的钥匙：

```text
name          虚拟设备名（对 GUI 表现为一个"序列号"）
sync          同步方式：Disabled / GUI / ExternalTrigger
deviceSerials 成员设备的序列号列表
portMapping   虚拟端口 → (物理设备编号, 物理端口) 的映射表
              例如 4 端口 = [ {dev0,port0}, {dev0,port1}, {dev1,port0}, {dev1,port1} ]
```

也就是说，「虚拟端口 3」可能是「2 号序列号那台设备的端口 1」。所有 set* 函数做的都是同一件事：**把面向虚拟端口的配置，按 portMapping 翻译分发到各成员设备**；所有数据合并函数做的都是逆运算。

还有一点必须想清楚——**跨设备的相位没有意义**。S 参数是比值 \( S_{ij} = b_i / a_j \)，分子分母来自同一台设备的接收通道时，本振共享、相位基准一致；一旦激励在设备 A、接收在设备 B，两台设备的本振各自为政，相位差是随机的。所以默认实现只保留模值。

#### 4.3.2 核心流程

一次复合 VNA 扫描的全流程：

```text
[配置阶段]（设置界面中编辑，JSON 持久化）
  compoundJSONString --parseCompoundJSON()--> configuredDevices 列表

[发现] GetAvailableDevices()
  六个官方驱动的 USB/TCP 枚举结果取并集
  → 只有当某配置的全部成员序列号都在线时，才把该虚拟设备报为"可用"

[连接] connectTo(虚拟设备名)
  按名字找到配置 → 对每个成员序列号：
     new 一个全新的 LibreVNAUSBDriver 或 LibreVNATCPDriver 实例
     connectDevice(serial, isIndepedentDriver=true)  ← 绕过全局仲裁
  挂接成员驱动信号：InfoUpdated / passOnReceivedPacket / ConnectionLost / 日志

[能力聚合] updatedInfo()
  info = 成员0.getInfo(); info.subset(成员i.getInfo())  对每个 i
  端口数 = portMapping.size()（覆盖 subset 累加的结果）

[配置扫描] setVNA(s)
  建 portStageMapping：虚拟激励端口 → 激励阶段(stage)编号
  对每台成员设备：setSynchronization(sync, 是否主机) + 翻译 excitedPorts
     （成员设备的"端口 1/2"被编码为"在哪个 stage 激励"）
  等所有成员回调到齐 → 汇报成功

[数据合并] datapointReceivecd(dev, datapoint)   ← 每台设备各调一次
  compoundVNABuffer[pointNum][dev] = 数据副本（深拷贝）
  当某 pointNum 凑齐全部成员：
     对每个 (激励端口, stage)：
        ref   = 激励所在成员设备的参考通道读数
        对每个虚拟接收端口 i：
           input = 接收所在成员设备的测量通道读数
           S(i)(j) = input / ref
           若激励与接收不在同一台设备 → S = |S|（丢相位，除非 preservePhase）
     emit VNAmeasurementReceived(m)
     清理该点及更早的残留缓冲（防内存泄漏）

[同步] triggerReceived(dev, set)   ← 仅 GUI 同步模式
  成员设备报告触发 → 转发给列表中的下一台（环形），软件层模拟硬件触发链
```

`Info::subset` 的聚合规则本身值得单独记（端口**相加**，各 Limits 取「更窄的一侧」——频率范围取交集、最大点数取最小值、features 取交集）：这正是「组合体的能力 ≤ 最弱成员」的数学表达。

#### 4.3.3 源码精读

**（1）发现：全员到齐才算可用**

[Device/LibreVNA/Compound/compounddriver.cpp:48-73](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddriver.cpp#L48-L73)

```cpp
parseCompoundJSON();
std::set<QString> availableSerials;
for(auto d : drivers) {
    availableSerials.merge(d->GetAvailableDevices());
}
std::set<QString> ret;
for(auto cd : configuredDevices) {
    bool allAvailable = true;
    for(auto s : cd->deviceSerials) {
        if(availableSerials.count(s) == 0) {
            allAvailable = false;
            break;
        }
    }
    if(allAvailable) {
        ret.insert(cd->name);
    }
}
```

注意 `drivers` 是构造函数里创建的两个「侦察用」实例（USB + TCP 各一，[Device/LibreVNA/Compound/compounddriver.cpp:20-21](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddriver.cpp#L20-L21)），只用来枚举，真正连接时会另建实例。

**（2）连接：每台成员一个专属驱动实例**

[Device/LibreVNA/Compound/compounddriver.cpp:96-128](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddriver.cpp#L96-L128)

```cpp
for(auto s : activeDevice.deviceSerials) {
    LibreVNADriver *device = nullptr;
    for(unsigned int i=0;i<availableSerials.size();i++) {
        if(availableSerials[i].count(s) > 0) {
            if (i == 0) {
                device = new LibreVNAUSBDriver();
                break;
            } else if(i == 1) {
                auto tcp = new LibreVNATCPDriver();
                tcp->copyDetectedDevices(*static_cast<LibreVNATCPDriver*>(drivers[i]));
                device = tcp;
                break;
            }
        }
    }
    ...
    if(!device->connectDevice(s, true)) {   // ← isIndepedentDriver = true
        ... return false;
    } else {
        devices.push_back(device);
    }
}
```

三个细节：每个序列号**新建**驱动实例（混合 USB + TCP 成员也行）；TCP 实例通过 `copyDetectedDevices` 继承侦察结果免得二次发现；`connectDevice(s, true)` 用 4.1.3 看到的旁路参数绕开「全局唯一活动设备」仲裁——否则第二台成员设备一连接就会把第一台踢下线。连接后统一挂信号（[Device/LibreVNA/Compound/compounddriver.cpp:131-143](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddriver.cpp#L131-L143)），其中 `passOnReceivedPacket` 正是 u3-l2 埋下的伏笔——官方驱动专门提供的「原始包旁听」信号，让 CompoundDriver 能拿到成员设备的每个协议包。

**（3）能力聚合：subset 交集规则 + 端口数覆盖**

[Device/LibreVNA/Compound/compounddriver.cpp:619-634](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddriver.cpp#L619-L634)

```cpp
if(deviceInfos.size() == devices.size()) {
    // got infos from all devices
    info = devices[0]->getInfo();
    for(unsigned int i=1;i<devices.size();i++) {
        info.subset(devices[i]->getInfo());
    }
    // overwrite number of ports (not all physical ports may be configured for this compound device)
    info.Limits.VNA.ports = activeDevice.portMapping.size();
    ...
```

`subset` 的实现在基类（[Device/devicedriver.cpp:162-198](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L162-L198)）：端口数 `+=`、`minFreq` 取 `max`、`maxFreq` 取 `min`、features 用 `set_intersection`。最后端口数被 `portMapping.size()` 覆盖——虚拟端口数由配置决定，与成员数不必然相等（你可以用 2 台设备只配 3 个虚拟端口）。

**（4）配置分发：把「虚拟端口」翻译成「stage」**

`setVNA` 的前半段建立映射，后半段逐台翻译：

[Device/LibreVNA/Compound/compounddriver.cpp:292-350](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddriver.cpp#L292-L350)

```cpp
// create port->stage mapping
portStageMapping.clear();
for(unsigned int i=0;i<s.excitedPorts.size();i++) {
    portStageMapping[s.excitedPorts[i]] = i;
}
...
for(unsigned int i=0;i<devices.size();i++) {
    auto dev = devices[i];
    dev->setSynchronization(activeDevice.sync, i == 0);   // 第一台为同步主机
    auto devSetting = s;
    // indicate the number of stages
    devSetting.excitedPorts = std::vector<int>(s.excitedPorts.size(), 0);
    // activate the ports of this specific device at the correct stage
    auto p1Stage = CompoundDevice::PortMapping::findActiveStage(activeMapping, i, 0);
    if(p1Stage < s.excitedPorts.size()) {
        devSetting.excitedPorts[p1Stage] = 1;
    }
    auto p2Stage = CompoundDevice::PortMapping::findActiveStage(activeMapping, i, 1);
    if(p2Stage < s.excitedPorts.size()) {
        devSetting.excitedPorts[p2Stage] = 2;
    }
    success &= devices[i]->setVNA(devSetting, ...);
```

这段的巧妙之处在于**语义转译**：GUI 层的 `excitedPorts` 是「端口列表」，而成员驱动（u3-l2 讲过的 `portStageMapping` 机制）理解的 `excitedPorts` 是「stage → 该 stage 在哪个物理端口激励」的编码表。CompoundDriver 先把虚拟端口顺序编号为 stage（`portStageMapping`），再查 `findActiveStage`（[Device/LibreVNA/Compound/compounddevice.cpp:79-87](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddevice.cpp#L79-L87)，在映射表里线性查找某设备的某端口排在第几个虚拟端口）确认「这台设备的物理端口 1/2 应该在哪个 stage 开火」。所有成员共享同一个 stage 编号空间，同步机制保证大家在同一 stage 对齐。

`setSynchronization(activeDevice.sync, i == 0)` 的第一台被指定为主机（枚举定义见 [Device/LibreVNA/librevnadriver.h:165-173](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.h#L165-L173)）。同构的分发逻辑也出现在 `setSA`（跟踪源落在哪台成员上，[compounddriver.cpp:361-411](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddriver.cpp#L361-L411)，并校验各成员 SA 点数一致）和 `setSG`（只有拥有该虚拟端口的成员拿到非零端口号，[compounddriver.cpp:422-446](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddriver.cpp#L422-L446)）。

**（5）数据合并：按点号缓冲 + S 参数拼装**

这是 CompoundDriver 的心脏。[Device/LibreVNA/Compound/compounddriver.cpp:726-802](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddriver.cpp#L726-L802)，核心段：

```cpp
if(buf.size() == devices.size()) {
    // Got datapoints from all devices, can create merged VNA result
    VNAMeasurement m;
    ...
    for(auto map : portStageMapping) {
        // figure out which device had the stimulus for the port...
        auto stimulusDev = devices[activeDevice.portMapping[map.first-1].device];
        // ...and which device port was used for the stimulus...
        auto stimulusDevPort = activeDevice.portMapping[map.first-1].port;
        // ...grab the reference receiver data
        std::complex<double> ref = buf[stimulusDev]->getValue(map.second, stimulusDevPort, true);

        // for all ports of the compound device...
        for(unsigned int i=0;i<activeDevice.portMapping.size();i++) {
            auto inputDevice = devices[activeDevice.portMapping[i].device];
            auto inputPort = activeDevice.portMapping[i].port;
            std::complex<double> input = buf[inputDevice]->getValue(map.second, inputPort, false);
            if(!std::isnan(ref.real()) && !std::isnan(input.real())) {
                QString name = "S"+QString::number(i+1)+QString::number(map.first);
                auto S = input / ref;
                if(!preservePhase && (inputDevice != stimulusDev)) {
                    // can't use phase information when measuring across devices
                    S = abs(S);
                }
                m.measurements[name] = S;
            }
            ...
```

逐条对照公式 \( S_{ij} = b_i / a_j \)：外层循环遍历每个激励端口（`map.first` 是虚拟端口号，`map.second` 是它的 stage）；从激励所在设备取该 stage 的**参考通道**读数作分母 `ref`；内层遍历每个虚拟接收端口，从各自设备取该 stage 的**测量通道**读数作分子 `input`；`S = input / ref` 即得 \( S_{i,j} \)。当接收设备和激励设备不是同一台时（`inputDevice != stimulusDev`），除非用户勾选 `preservePhase`（构造函数里默认 `false`，[compounddriver.cpp:35-37](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddriver.cpp#L35-L37)），相位被 `abs(S)` 丢弃——这是物理约束，不是偷懒。

缓冲管理在函数头尾：进入时 `buf[dev] = new Protocol::VNADatapoint<32>(*data)` 深拷贝（成员驱动随后会释放原对象，[compounddriver.cpp:728-733](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddriver.cpp#L728-L733)）；凑齐上报后，从当前点号向**更早**的点号回溯删除（点号在扫描结尾会从 `VNApoints-1` 绕回 0，[compounddriver.cpp:786-800](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddriver.cpp#L786-L800)），清掉所有「永远凑不齐」的残缺缓冲，防止堆内存随扫描持续增长。频谱数据走完全对称的另一套缓冲（`compoundSABuffer`，[compounddriver.cpp:663-704](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddriver.cpp#L663-L704)）。

**（6）软件触发链与状态合并**

GUI 同步模式下，成员设备每报告一次触发，就把它转发给列表中的下一台，末台绕回首台成环：

[Device/LibreVNA/Compound/compounddriver.cpp:548-567](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddriver.cpp#L548-L567)

```cpp
if(activeDevice.sync == LibreVNADriver::Synchronization::GUI && triggerForwarding) {
    for(unsigned int i=0;i<devices.size();i++) {
        if(devices[i] == device) {
            if(i < devices.size() - 1) {
                devices[i+1]->sendWithoutPayload(set ? Protocol::PacketType::SetTrigger : Protocol::PacketType::ClearTrigger);
            } else {
                devices[0]->sendWithoutPayload(...);
            }
```

状态字段的合并规则（[compounddriver.cpp:636-661](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddriver.cpp#L636-L661)）同样体现「组合体取最保守值」：锁定位 `source_locked`/`LO1_locked` 用 `&=`（任一台失锁即报失锁）、告警位 `ADC_overload`/`unlevel` 用 `|=`（任一台告警即告警）、温度取 `max`。而 `setIdle` 会先给每台发 `ClearTrigger` 再逐台置闲（[compounddriver.cpp:448-467](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddriver.cpp#L448-L467)），避免触发链上残留挂起状态。

#### 4.3.4 代码实践

**实践目标**：源码阅读型实践——用纸笔推演「2 台 LibreVNA 组成 4 端口虚拟设备」时一个扫描点在 CompoundDriver 里的完整旅程，检验对 portMapping/stage/合并逻辑的理解。

**操作步骤**：

1. 设定场景：两台设备（序列号 A、B），`portMapping = [{dev0,p0},{dev0,p1},{dev1,p0},{dev1,p1}]`，即虚拟端口 1/2 在设备 A、3/4 在设备 B；VNA 模式激励全部 4 个端口。
2. 在纸上画出 `portStageMapping`：激励端口 `[1,2,3,4]` 依次编号，得到 `{1→0, 2→1, 3→2, 4→3}`。
3. 对设备 A（`i=0`）手工执行 [compounddriver.cpp:329-336](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddriver.cpp#L329-L336)：`findActiveStage(mapping, 0, 0)` 和 `findActiveStage(mapping, 0, 1)` 各返回几？设备 A 收到的 `devSetting.excitedPorts` 是什么？（`findActiveStage` 的定义在 [Device/LibreVNA/Compound/compounddevice.cpp:79-87](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddevice.cpp#L79-L87)。）对设备 B（`i=1`）重复一遍。
4. 推演合并：设某点号两台设备的数据都到齐了。对激励端口 1（stage 0），写出 `stimulusDev`、`ref` 的来源；再对接收端口 3，写出 `input` 的来源和 `S31` 的表达式；标注此时 `inputDevice != stimulusDev` 成立与否、`S31` 是否保留相位。对 `S11`、`S33` 重复。
5. 数一遍：一个完整扫描点最多会产出多少个 S 参数键？其中多少个保相位、多少个丢相位？

**需要观察的现象**（即你的推导应得到的结论，先推再对照）：设备 A 的 `excitedPorts` 为 `{1,2,0,0}`（物理端口 1 在 stage 0、物理端口 2 在 stage 1 激励），设备 B 为 `{0,0,1,2}`；`S11`、`S22`、`S33`、`S44`（激励与接收同台）保相位，`S13`、`S31` 等跨设备项只剩模值；4×4 共 16 个键。

**预期结果**：一张标注完整的「虚拟端口 ↔ (设备, 物理端口, stage)」对照表 + 每类 S 参数的相位保留判断。全部结论可由源码静态推出，无需硬件；如要上机验证需两台 LibreVNA（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `datapointReceivecd` 里要用 `new Protocol::VNADatapoint<32>(*data)` 做深拷贝存进缓冲，而不是直接存指针？

**答案**：`data` 指针指向成员驱动（`LibreVNADriver`）内部的对象，成员驱动解析完下一个包后可能复用或释放该内存。而复合设备的合并必须等到**所有**成员的同一 `pointNum` 都到齐——慢设备的包到达之前，快设备的包要在缓冲里存活任意长时间。深拷贝把数据的所有权拿到 CompoundDriver 手里，合并完成后再统一 `delete`（786-800 行的清理循环）。代价是每个点一次堆分配，换的是生命周期的确定性。

**练习 2**：`Info::subset` 里 `Limits.VNA.ports` 用加法（`+=`），但 CompoundDriver 随后又用 `portMapping.size()` 覆盖端口数。既然要覆盖，`subset` 里的加法还有意义吗？

**答案**：有，两处服务于不同场景。`subset` 是基类提供的通用聚合工具，端口相加是「N 台 2 端口设备理论上有 2N 个端口」的合理默认，任何其他复用 `subset` 的场合（当前只有 CompoundDriver）可以直接受益。CompoundDriver 覆盖是因为用户配置可能**故意不用满**所有物理端口（例如只用两台设备中的 3 个端口构成三端口测量），虚拟端口数以配置为准。顺序是「先算理论值，再用配置的实际值覆盖」——若没有覆盖步骤，`availableVNAMeasurements`（按 `info.Limits.VNA.ports` 生成 S 参数名）会报出用户没接线的端口。

**练习 3**：对比本模块的两处循环边界：`availableSAMeasurements`（[compounddriver.cpp:352-359](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddriver.cpp#L352-L359)）写 `for(i=1; i<=ports; i++)`，而 `availableSGPorts`（[compounddriver.cpp:413-420](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/Compound/compounddriver.cpp#L413-L420)）写 `for(i=1; i<ports; i++)`。你发现了什么？会造成什么后果？

**答案**：`availableSGPorts` 的循环上界少了等号，疑似 off-by-one：4 端口配置只会返回 `PORT1..PORT3`，漏掉最后一个端口，而 SA 版本是完整的 `PORT1..PORTn`。后果是 GUI 的信号源端口下拉列表看不到最后一个虚拟端口（`setSG` 本身按 `portMapping` 能正确处理任意端口，问题只出在「可选列表」）。这正是阅读真实项目代码的价值——生产代码也藏着边界错误，把「两个几乎相同的函数放在一起 diff」是发现这类问题的高效手法。若你打算向上游报告或修复，先在本地复现确认（待本地验证）。

## 5. 综合实践

把 4.2.4 写好的 300 字方案扩展成一份**可直接动工的驱动骨架设计文档**，以假想的「MiniSpec-1000」（一台局域网 SCPI 频谱仪，支持 `:FREQ:STAR/STOP`、`:TRAC? 0`、无跟踪源、无信号源）为例，你的文档应包含以下六节（示例答案附后，先自己写）：

1. **继承选择**：`DeviceTCPDriver`（网络仪器 + 需要搜索地址列表，直接复用 5024/5025 探测与设置界面）。
2. **七个纯虚函数的实现策略**：`getDriverName` 返回 `"MiniSpec1000"`；`GetAvailableDevices` 连 5024 验横幅前缀 + `*IDN?` 取序列号（照抄 SSA3000X 模式）；`connectTo` 连 5025、填 `Info`；`disconnect`/`getSerial`/`getInfo`/`getFlags` 平凡实现。
3. **能力面**：`supportedFeatures = {SA}`。不覆写 `setVNA`/`setSG`/`setExtRef`——隐式不支持；`availableSAMeasurements` 返回 `{"PORT1"}`。
4. **Limits**：按手册硬编码 `SA.maxFreq` 等（若仪器有查询命令则学 SNA5000A 用 `queryInt`）。
5. **数据回传**：QTimer 轮询 `:TRAC? 0`，CSV 解析、dBm→线性电压（\( 10^{\mathrm{dBm}/20} \)）、`TraceDifferenceGenerator` 去重、`emit SAmeasurementReceived`。
6. **接入**：`getDrivers()` 加一行、`.pro` 登记 `.h`/`.cpp`。

然后做三件验证（均可在**无硬件**条件下完成前两件）：

- 编译通过：`qmake6 && make`（构建方法见 u1-l3），新文件未登记 `.pro` 会链接失败，这本身就是一次对 4.1 结论的验证。
- 启动 GUI，打开设备菜单确认新驱动名出现在驱动列表中（`GetAvailableDevices` 返回空集不影响驱动名出现），偏好设置里出现它的地址编辑页。
- 有条件时（待本地验证）：把搜索地址指向一台真实 SCPI 仪器或本地用 `nc -l 5025` 伪造应答，观察连接流程走到哪一步。

骨架代码示意（**示例代码**，非仓库原有内容，仅展示最小结构）：

```cpp
// minispecdriver.h（示例代码）
class MiniSpecDriver : public DeviceTCPDriver {
public:
    QString getDriverName() override { return "MiniSpec1000"; }
    std::set<QString> GetAvailableDevices() override;
protected:
    bool connectTo(QString serial) override;
    void disconnect() override;
public:
    QString getSerial() override { return serial; }
    Info getInfo() override { return info; }
    std::set<Flag> getFlags() override { return {}; }
    QStringList availableSAMeasurements() override { return {"PORT1"}; }
    bool setSA(const SASettings &s, std::function<void(bool)> cb = nullptr) override;
    unsigned int getSApoints() override;
private:
    QString serial;
    Info info;
    // ... 数据 socket、轮询定时器等，参照 ssa3000xdriver.h 组织
};
```

最后用一段话回答收尾问题：**你的驱动工作量花在哪里？** 对照两个 Siglent 驱动你会发现，纯虚函数的实现大都是模板化的，真正花时间的是「能力翻译层」——把仪器的 SCPI 方言映射到 `SASettings`/`VNASettings`，以及处理数据格式与异常（超时、不完整扫描、单位换算）。这为你评估「给任何仪器写驱动」提供了一个经验法则：**发现与连接是模板，能力翻译是手艺**。

## 6. 本讲小结

- `getDrivers()` 是六行 `push_back` 的懒加载静态注册表；接入新驱动的全部改动 = 新类 + 一行注册 + `.pro` 登记，共三处。
- 「不支持」在这个代码库里有三级表达：不覆写（继承基类默认 `false`/空）、覆写后显式返回 `false`/占位值、名义支持但降级实现（Kaiser→Hamming、未完成点过滤）。
- 第三方驱动全部是**主机轮询**模式（QTimer + SCPI 文本查询 + `TraceDifferenceGenerator` 去重），与官方驱动的设备推送模式互为对照；发现机制利用 LXI 5024 横幅 + 5025 数据口约定。
- 第三种接入姿势是 Harogic 式的：协议兼容的设备直接继承官方驱动，只换 VID/PID 与身份，十几行完事。
- CompoundDriver 从 `DeviceDriver` 直接继承、把 `LibreVNADriver` 当子设备编排；`connectDevice(s, true)` 的旁路参数是它能同时持有 N 台活动设备的机制基础。
- 复合的核心算法：`portMapping` 把虚拟端口映射到（设备, 物理端口），`portStageMapping` 把激励端口编码为 stage；数据按 `pointNum` 缓冲、全员到齐后按 \( S_{ij} = \text{input}/\text{ref} \) 拼装，跨设备相位默认丢弃（`abs(S)`），能力聚合遵循「端口相加、Limits 取交集、状态取最保守」。

## 7. 下一步学习建议

本讲结束了设备驱动层（第 3 单元）的学习。接下来有两条路，按你的兴趣选择：

1. **向下钻进协议（第 4 单元，u4-l1）**：CompoundDriver 依赖的 `passOnReceivedPacket` 原始包、官方驱动的 `Protocol::VNADatapoint` 到底长什么样？第 4 单元进入固件侧的 `Communication.cpp` 与 `Protocol.hpp`，看两端如何共用一份协议定义。
2. **向上看消费端（第 7 单元，u7-l1）**：驱动上报的 `VNAMeasurement`/`SAMeasurement` 被 VNA 模式拿去做了什么？u7-l1 讲解模式层如何把 UI 设置翻译成本讲的 `setVNA` 调用，与你 4.3 学的配置分发首尾衔接。

若你已经手痒想写驱动，可以直接跳到毕业实战（u11-l3）：实现一个产生假数据的 DemoDriver，把本讲 5 节的骨架方案完整走一遍。
