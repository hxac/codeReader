# u5-l3 设备级校准与 Flash 存储

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 LibreVNA「设备级校准」保存的三类系数（源幅度表、接收机幅度表、TCXO 频率 ppm），并解释它与单元 9 将要学习的「GUI 级 SOLT 校准」在职责上的边界。
2. 跟踪固件从上电 `Cal::Load()` 读 flash、到把校准系数应用到激励功率 / 接收幅度 / 频率设定的完整路径。
3. 读懂 `Flash.cpp` 这颗外部 SPI NOR Flash 的驱动：页编程、扇区擦除、忙等待与回读校验。
4. 描述一次固件升级在 GUI 状态机与固件 `Firmware.cpp` 两侧如何配合完成，尤其是 `copy_flash()` 为什么必须在 RAM 中运行且「一个函数调用都不许有」。

本讲全部属于固件侧（`Software/VNA_embedded`），但会跨到 GUI 侧的三个对话框，把「PC 端如何把校准数据写进设备」这条链走通。

## 2. 前置知识

### 2.1 两级校准：设备说真话，GUI 修夹具

LibreVNA 的校准分两级，职责完全不同，不要混淆：

| | 设备级校准（本讲） | GUI 级校准（单元 9） |
|---|---|---|
| 代码位置 | 固件 `Cal.cpp` | GUI `Calibration/calibration.cpp` |
| 存储位置 | 设备板载 SPI Flash | PC 上的文件 |
| 修正对象 | 设备自身的偏差：输出功率不准、接收机增益不准、TCXO 频率偏差 | 测量系统的系统误差：定向电桥方向性、源匹配、负载匹配、隔离度等（SOLT 12 项误差模型） |
| 直观理解 | 让设备「说真话」：你让它输出 -10 dBm，它真的输出 -10 dBm | 把「夹具和桥」的影响从测量中扣除，得到 DUT 本身的 S 参数 |
| 是否依赖硬件 | 是，出厂/用户手动做一次，存在设备里 | 是，每次测量环境变化后都要重做 |

### 2.2 SPI NOR Flash 的物理约束

板载外部 Flash 是一颗标准 SPI NOR Flash（W25Q 类兼容），它有几个嵌入式开发必须知道的物理特性：

- **写只能把 bit 从 1 改成 0**。要把 0 改回 1，只能整块「擦除」（擦除后全为 0xFF）。
- **编程（写）以「页」为单位**，本驱动约定页大小 256 字节；**擦除以扇区/块为单位**（4 KB / 32 KB / 64 KB）。
- 每次编程或擦除前，必须先发 `WEL`（Write Enable Latch，命令 0x06）解锁写操作。
- 擦除和编程是异步的：命令发出后芯片内部自行忙碌，状态寄存器 bit0（BUSY）为 1 期间不可再操作，需要轮询。

这些约束直接决定了 `Cal::Save()`「先擦后写、按页对齐」的实现方式，也解释了为什么升级固件不会丢校准数据（见 4.3）。

### 2.3 TCXO 与 ppm

TCXO（温度补偿晶体振荡器）是全板的时钟基准。实际晶体频率与标称值之间有百万分之几（ppm）的偏差，且随温度漂移。校准时测出这个偏差存成一个 float，之后所有下发给 PLL 的频率都按该偏差反向修正。1 ppm 在 6 GHz 处对应 6 kHz 的频率误差——不修正的话，频谱模式和 VNA 模式的读数会整体偏移。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [Software/VNA_embedded/Application/Cal.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Cal.cpp) | 设备级校准的全部逻辑：数据结构、flash 存取、插值、收发校准点 |
| [Software/VNA_embedded/Application/Cal.hpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Cal.hpp) | 校准命名空间接口、flash 地址与容量约定、最多 64 个校准点 |
| [Software/VNA_embedded/Application/Drivers/Flash.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/Flash.cpp) | 外部 SPI NOR Flash 驱动：读、页编程、多档擦除、忙等待 |
| [Software/VNA_embedded/Application/Drivers/Flash.hpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/Flash.hpp) | Flash 类接口与页/扇区/块尺寸常量 |
| [Software/VNA_embedded/Application/Firmware.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Firmware.cpp) | 固件镜像校验（magic + CRC32）与 RAM 中自我更新的 `copy_flash()` |
| [Software/VNA_embedded/Application/Firmware.hpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Firmware.hpp) | `Firmware` 命名空间：1 MB 固件区约定与 `Info` 结构 |
| [Software/VNA_embedded/Application/App.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp) | 启动时 `Cal::Load()`；协议包到 Cal/Firmware 的分发 |
| [Software/VNA_embedded/Application/Hardware.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp) | `GetAmplitudeSettings()` 应用源幅度校准 |
| [Software/VNA_embedded/Application/Hardware.hpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.hpp) | flash 分区总图：固件区、校准区、设备配置区的地址接力 |
| [Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/amplitudecaldialog.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/amplitudecaldialog.cpp) | GUI 源/接收机幅度校准对话框（写入与读回的上行端） |
| [Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/frequencycaldialog.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/frequencycaldialog.cpp) | GUI 频率校准对话框（ppm 读写） |
| [Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/firmwareupdatedialog.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/firmwareupdatedialog.cpp) | GUI 固件升级状态机 |

## 4. 核心概念与源码讲解

### 4.1 Cal 设备校准

#### 4.1.1 概念说明

`Cal` 是固件里的一个命名空间（不是类，全部是静态数据 + 自由函数），保存三类校准系数：

1. **源幅度校正表（Source）**：设备在指定频率点真正输出的功率与期望值的偏差（每端口一条），单位 0.01 dB。修正的是「我让设备输出 -10 dBm，它实际输出了 -12.3 dB」这类误差。
2. **接收机幅度校正表（Receiver）**：接收链路在指定频率点的增益偏差（每端口一条），同样 0.01 dB 步进。修正频谱模式下读到的电平。
3. **TCXO 频率修正（`TCXO_PPM_correction`）**：一个 float，晶体频率偏差的 ppm 值。

每张幅度表最多 64 个频率点，点与点之间线性插值。这些数据在出厂或用户手动校准时由 PC 写入，永久保存在板载 flash 里，「刷固件也不会丢」。

#### 4.1.2 核心流程

数据的完整生命周期：

```text
【出厂/用户校准，PC → 设备】
GUI AmplitudeCalDialog::SaveToDevice()
  └─ 逐点发 SourceCalPoint / ReceiverCalPoint 包（freq LSB=10Hz, 修正值 LSB=0.01dB）
       └─ 固件 App_Process 分发 → Cal::AddSourcePoint / AddReceiverPoint
            └─ addPoint() 写入静态表 cal
                 └─ 收到 pointNum == totalPoints-1（最后一个点）时自动 Cal::Save()
                      └─ 擦除校准扇区 → 按页写回 → 回读校验

【上电，设备自恢复】
App_Init() → Cal::Load()
  └─ 从 flash 读出整个 cal 结构
       ├─ version 不符（固件升级改了结构格式）→ SetDefault()，回默认
       └─ 校验通过 → 后续所有测量使用内存中的表

【测量时应用】
源幅度：HW::GetAmplitudeSettings() → Cal::SourceCorrection(freq) 线性插值 → 加到 cdbm 上
接收幅度：SpectrumAnalyzer 出结果时 → Cal::ReceiverCorrection(freq) → 乘到幅度上
频率：VNA/SA/Generator 设定频率时 → Cal::FrequencyCorrectionToDevice(freq) 反向修正
```

频率修正的数学：设晶体实际频率比标称高 \(p\) ppm（\(p = \(TCXO\_PPM\_correction\)），要让输出真正等于目标频率 \(f\)，应把下发给 PLL 的设定值改为

\[ f_{set} = f \cdot (1 - p \times 10^{-6}) \approx f - f \cdot p \times 10^{-6} \]

代码采用右边这个近似式（减法），因为乘法形式在 float 精度下有舍入问题，而结果的分辨率本来就是 1 Hz，近似误差可忽略——这是源码注释里明确解释过的取舍。

#### 4.1.3 源码精读

**① 数据结构与 flash 布局约定**

[Software/VNA_embedded/Application/Cal.cpp:L13-L31](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Cal.cpp#L13-L31)：定义校准结构。`version` 是格式版本号（当前 0x0001），结构变更时递增；`CorrectionTable` 里频率以 10 Hz 为 LSB、修正值以 0.01 dB 为 LSB 的 int16 存储——用定点数而不用 float，既省空间又避免浮点序列化的字节序问题。结尾的 `static_assert(sizeof(cal) <= Cal::flash_size, ...)` 在编译期保证结构不会超出 flash 预留区，这是一个很值得学习的防御性写法。

[Software/VNA_embedded/Application/Cal.hpp:L10-L12](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Cal.hpp#L10-L12)：三个关键常量——`maxPoints = 64`（每张表最多 64 点）、`flash_address = Firmware::maxSize`（即 1 MB，紧跟在固件区后面）、`flash_size = 8192`（预留两个 4 KB 扇区）。

**② 上电加载与容错**

[Software/VNA_embedded/Application/Cal.cpp:L33-L47](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Cal.cpp#L33-L47)：`Cal::Load()` 一次性读出整个结构，然后做两层校验：版本号不符（比如新固件改了结构格式，旧数据无法解释）或表为空（`usedPoints == 0`）都回退到 `SetDefault()`。也就是说：**校准数据损坏的最坏结果是回到「无修正」状态，设备仍能工作**，只是精度退回出厂未校准水平。

[Software/VNA_embedded/Application/App.cpp:L102-L102](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L102-L102)：`Cal::Load()` 在 `App_Init` 中的调用点——位于 FPGA 配置完成之后、`HW::Init()` 之前，保证硬件初始化时就能用上校准数据。

[Software/VNA_embedded/Application/Cal.cpp:L61-L69](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Cal.cpp#L61-L69)：`SetDefault()` 的默认值是「1 个点、100 MHz、修正量为 0」——零修正，即透传。

**③ 保存：先擦后写、按页对齐**

[Software/VNA_embedded/Application/Cal.cpp:L49-L59](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Cal.cpp#L49-L59)：`Cal::Save()` 先 `eraseRange` 擦掉整个预留区（两个扇区），再把 `sizeof(cal)` 向上取整到 256 字节页边界后写入。这里体现 2.2 节讲的 NOR Flash 约束：写之前必须擦；而 `Flash::write` 只接受页对齐的地址和长度。

注意一个工程风险点：擦除和写入之间如果断电，校准数据会丢失——但下次上电 `Load()` 的容错逻辑会把设备带回默认状态，不会变砖。

**④ 插值取修正值**

[Software/VNA_embedded/Application/Cal.cpp:L71-L97](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Cal.cpp#L71-L97)：`InterpolateCorrection()` 先把频率除以 10 对齐到表的 LSB，然后分三种情况：低于第一点取第一点、高于最后一点取最后一点（**两端外推为钳位，不外插**）、落在中间则线性插值。插值权重：

\[ \alpha = \frac{f - f_{i-1}}{f_i - f_{i-1}}, \quad c = c_{i-1}(1-\alpha) + c_i\,\alpha \]

**⑤ 测量时的三个应用点**

- [Software/VNA_embedded/Application/Hardware.cpp:L272-L280](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L272-L280)：`HW::GetAmplitudeSettings()` 在 `applyCorrections` 为真时，把 `Cal::SourceCorrection(freq)` 的结果**加**到目标 cdbm 上（0.01 dB 步进正好与 cdbm 单位一致），再继续 u5-l2 讲过的功率档位/衰减器分解。
- [Software/VNA_embedded/Application/SpectrumAnalyzer.cpp:L463-L467](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L463-L467)：频谱结果上报前，把 `Cal::ReceiverCorrection()` 的 dB 修正换算成线性乘子乘到幅度上：\( \text{amp} \times 10^{\frac{c/100}{20}} \)（c 的 LSB 是 0.01 dB，所以先除 100）。
- [Software/VNA_embedded/Application/Cal.cpp:L153-L172](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Cal.cpp#L153-L172)：频率修正对。`FrequencyCorrectionToDevice` 只在使用内部参考时生效（外接参考时假定它本来就是准的）。`FrequencyCorrectionFromDevice` 是反方向（设备 → GUI），**当前固件中没有调用点**，属于为对称性保留的接口——读代码时不要因为它存在就推断有对应行为。

调用 `FrequencyCorrectionToDevice` 的三处：VNA 扫描配置 [VNA.cpp:L227](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L227)、VNA 停走恢复 [VNA.cpp:L448](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L448)、SA [SpectrumAnalyzer.cpp:L60](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L60)、Generator [Generator.cpp:L29](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Generator.cpp#L29)——所有「把频率变成硬件设定」的地方都必须过这道修正。

**⑥ 与 PC 的协议接口**

[Software/VNA_embedded/Application/App.cpp:L237-L265](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L237-L265)：六个协议包在固件侧的落点——`RequestSourceCal`/`RequestReceiverCal` 触发整表上报，`SourceCalPoint`/`ReceiverCalPoint` 逐点写入，`RequestFrequencyCorrection`/`FrequencyCorrection` 读写 ppm。每包处理完都回 Ack（沿用 u4-l1 的分发框架）。

[Software/VNA_embedded/Application/Cal.cpp:L129-L143](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Cal.cpp#L129-L143)：`addPoint()` 有两个细节值得注意：越界的 `pointNum` 直接丢弃（防御 PC 发来的坏数据）；**当 `pointNum == totalPoints - 1`，即收到最后一个点时，自动触发 `Cal::Save()` 落盘**——PC 端不需要单独的「保存」命令，发完最后一点就等于保存。

[Software/VNA_embedded/Application/Cal.cpp:L107-L119](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Cal.cpp#L107-L119)：反方向上报时逐点打包发送，`totalPoints` 字段让 PC 知道何时收齐。

#### 4.1.4 代码实践：跟踪「PC 写校准数据」的完整函数链

1. **实践目标**：列出 Cal.cpp 保存的全部校准系数类别，并写出 GUI 按下「Save to device」后数据经过的每一级函数，直到 flash 落盘。
2. **操作步骤**：
   - 打开 [Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp:L192-L211](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L192-L211)，确认三个菜单项 `Source Calibration` / `Receiver Calibration` / `Frequency Calibration` 分别创建哪个对话框（`SourceCalDialog`、`ReceiverCalDialog` 都是 `AmplitudeCalDialog` 的子类，见 [sourcecaldialog.h:L15](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/sourcecaldialog.h#L15)，区别只是 `pointType()` 返回的包类型）。
   - 精读 [amplitudecaldialog.cpp:L228-L245](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/amplitudecaldialog.cpp#L228-L245) 的 `SaveToDevice()`，注意 `p.frequency / 10.0`（Hz → 10 Hz LSB）和 `totalPoints`/`pointNum` 的填法。
   - 按下面的空表逐级填写函数名（这就是本实践的产出）：

   ```text
   GUI 侧：
   AmplitudeCalDialog::SaveToDevice()                     (amplitudecaldialog.cpp:228)
     → LibreVNADriver::SendPacket(info)                   (u4-l3 讲过的发送队列)
       → USB/TCP 子类编码为五段式帧并发送
   设备侧：
   Communication 收包 → DecodeBuffer 拆帧
     → App_Process 分发 case SourceCalPoint/ReceiverCalPoint   (App.cpp:245-252)
       → Cal::AddSourcePoint / Cal::AddReceiverPoint     (Cal.cpp:145/149)
         → addPoint() 写内存表，末点触发 Cal::Save()      (Cal.cpp:129-143)
           → Flash::eraseRange + Flash::write            (Flash.cpp)
   ```

   - 补充读回方向：`LoadFromDevice()`（[amplitudecaldialog.cpp:L216-L226](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/amplitudecaldialog.cpp#L216-L226)）与固件的 `SendCorrectionTable` 如何配对。
3. **需要观察的现象**：`SaveToDevice` 是一个 for 循环逐点发包，而设备端只有收到**最后一个点**才写 flash；对照固件 `addPoint()` 里 `p.pointNum == p.totalPoints - 1` 的判断，理解这条「末点即保存」的隐式协议约定。
4. **预期结果**：得到一张完整的三列对照表（步骤 / 函数 / 文件:行号）。若手头有设备，还可在 GUI 里实际打开 Source Calibration 对话框点 Save，用 DevicePacketLog（u4-l3）抓包验证每点一个 `SourceCalPoint` 包。无设备则纯代码走读即可完成，**无需本地验证硬件路径**。

#### 4.1.5 小练习与答案

**练习 1**：`Cal.cpp` 里一共保存几类校准系数？各自的物理含义和单位是什么？

答案：三类——① 源幅度校正表（两个端口各一条曲线，int16，LSB = 0.01 dB，修正设备实际输出功率与设定值的偏差）；② 接收机幅度校正表（同格式，修正接收链路增益偏差，用于频谱模式电平读数）；③ TCXO 频率修正（float，单位 ppm，修正晶体频率偏差）。此外每张表还有一个 `usedPoints` 计数与版本号 `version`，属于元数据而非系数。

**练习 2**：为什么 `Cal::Load()` 检查 `version` 而不是直接使用 flash 里的数据？

答案：因为校准结构的二进制布局由固件代码定义。如果新固件修改了结构（字段增删、大小变化），flash 里旧数据的字节排列就不再匹配新布局，直接解释会得到错乱数据。版本不符时回退 `SetDefault()`（零修正），设备仍可用。这和 u4-l2 讲的「协议版本协商」是同一思想在持久化数据上的应用。

**练习 3**：把频率校准 ppm 设为 +2.0 后，GUI 要求输出 1 GHz，固件实际下发给 PLL 的频率是多少？

答案：按 `FrequencyCorrectionToDevice` 的公式 \( f - f \cdot p \times 10^{-6} \)：\( 10^9 - 10^9 \times 2 \times 10^{-6} = 999{,}998{,}000 \) Hz。因为晶体实际振荡频率偏高 2 ppm，必须把设定值调低，输出才真正落在 1 GHz。

### 4.2 Flash 读写驱动

#### 4.2.1 概念说明

`Flash` 类（`Drivers/Flash.cpp`）封装板载外部 SPI NOR Flash 的全部访问。u5-l2 讲过 SPI1 总线在 FPGA 与两颗 MAX2871 之间分时复用，外部 Flash 其实也挂在这条总线上——实例化于 [HW_HAL.cpp:L7](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/HW_HAL.cpp#L7)，用 `FLASH_CS_Pin` 做片选，靠片选区分总线上的不同设备。

这颗 Flash 是设备的「硬盘」：1 MB 固件镜像、8 KB 校准数据、4 KB 设备配置都住在里面。它同时是 NOR Flash 命令集的标准教科书实现，读懂它等于读懂一大类嵌入式存储驱动。

#### 4.2.2 核心流程

一次「擦除后写入」的完整时序（`Cal::Save()` 实际走过的路径）：

```text
eraseRange(start, len)
  ├─ 地址/长度按 4KB 扇区对齐检查
  ├─ 循环：优先 64KB 块擦 → 次选 32KB 块擦 → 兜底 4KB 扇区擦
  │    每次擦除 = EnableWrite(0x06) + 擦除命令(0xD8/0x52/0x20+3字节地址)
  │              + WaitBusy 轮询状态寄存器 BUSY 位（vTaskDelay(1) 让出 CPU）
  └─ 全部擦完返回 true

write(address, len, src)          # len 必须是 256 的倍数
  └─ 循环每次一页(256B)：
       EnableWrite(0x06)
       CS 低 → 发 0x02 + 3字节地址 → 发 256 字节数据 → CS 高
       WaitBusy(20ms)             # 等页编程完成
       read(address, 256, buf) 回读
       memcmp(src, buf, 256)      # 逐页回读校验，不一致即失败
```

「优先擦大块」不是炫技：块擦除命令数少、总耗时低，也减少命令开销；但代价是擦掉的范围更大，所以 `eraseRange` 会检查 `remaining >= Block64Size` 且地址对齐才用大块，从大到小贪心降级。

#### 4.2.3 源码精读

**① 设备在不在：JEDEC ID 探测**

[Software/VNA_embedded/Application/Drivers/Flash.cpp:L11-L28](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/Flash.cpp#L11-L28)：`isPresent()` 发 0x9F 读 JEDEC ID（3 字节：厂商、类型、容量），只要厂商字节（`recv[1]`）落在白名单 {0xEF（Winbond 等）, 0x68, 0x9D} 中就认为芯片存在。上电时 [App.cpp:L75-L78](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L75-L78) 用它做硬件自检，失败亮 LED 错误码 1。

**② 读：两段式设计是自我更新的伏笔**

[Software/VNA_embedded/Application/Drivers/Flash.cpp:L30-L35](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/Flash.cpp#L30-L35) 与 [L146-L157](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/Flash.cpp#L146-L157)：普通读是 `initiateRead`（发 0x03 + 地址）+ `HAL_SPI_Receive` + 拉高 CS。注意 `initiateRead` 被单独拆成公有接口——发完命令后 CS 保持低电平、时钟继续就能流出数据。4.3 节的 `copy_flash()` 正是利用这一点：它不能调用任何 HAL 函数，于是提前用 `initiateRead` 把读命令发好，再亲自去裸打 SPI 寄存器收字节。

**③ 写：页对齐 + 忙等 + 回读校验**

[Software/VNA_embedded/Application/Drivers/Flash.cpp:L37-L75](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/Flash.cpp#L37-L75)：`write()` 开头就拒绝非页对齐的地址/长度；每页写完 `WaitBusy(20)` 后**回读 256 字节用 `memcmp` 校验**。存储校准数据与固件镜像都关乎设备能否正常工作，写错一字节都不行，所以驱动层直接内置了校验——这是嵌入式存储驱动的良好实践。

**④ 擦除家族与忙等待**

[Software/VNA_embedded/Application/Drivers/Flash.cpp:L85-L144](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/Flash.cpp#L85-L144)：`eraseChip`（0x60 全片）、`eraseSector`（0x20，4 KB）、`erase32Block`（0x52）、`erase64Block`（0xD8）四个擦除档位，全部遵循「对齐地址 → EnableWrite → 命令+地址 → WaitBusy」模板。

[Software/VNA_embedded/Application/Drivers/Flash.cpp:L159-L177](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/Flash.cpp#L159-L177)：`WaitBusy()` 轮询状态寄存器 1（命令 0x05）的 bit0。关键细节：循环里 `vTaskDelay(1)` 让出 CPU——因此**这个驱动只能在任务上下文调用，不能在中断里用**（FreeRTOS 红线，见 u5-l1）。擦除超时给到 25 秒（大块擦除确实很慢），页编程只给 20 ms。

**⑤ 分区总图**

[Software/VNA_embedded/Application/Hardware.hpp:L139-L140](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.hpp#L139-L140) 与 [Firmware.hpp:L15-L15](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Firmware.hpp#L15-L15)、[Cal.hpp:L11-L12](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Cal.hpp#L11-L12) 三个文件的地址常量拼出整颗 flash 的分区：

| 区域 | 起始地址 | 大小 | 定义处 |
|---|---|---|---|
| 固件镜像（bitstream + MCU 固件） | 0x000000 | 1 MB | `Firmware::maxSize` |
| 设备校准数据 | 0x100000（1 MB） | 8 KB | `Cal::flash_address/flash_size` |
| 设备配置（IF 频率等） | 0x102000 | 4 KB | `HW::flash_address/flash_size` |

三个区域地址首尾相接、各自用常量表达依赖（`Cal::flash_address = Firmware::maxSize`，`HW::flash_address = Firmware::maxSize + Cal::flash_size`），改任何一处布局其余自动跟随。

#### 4.2.4 代码实践：整理 Flash 命令速查表

1. **实践目标**：把 `Flash.cpp` 用到的每条芯片命令整理成速查表，并验证分区算术。
2. **操作步骤**：
   - 通读 [Flash.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/Flash.cpp)，按下表填写（命令码已在代码中以立即数出现）：

   | 命令码 | 名字 | 使用它的函数 | 行号 |
   |---|---|---|---|
   | 0x9F | 读 JEDEC ID | `isPresent` | L14 |
   | 0x03 | 读数据 | `initiateRead` | L150 |
   | 0x02 | 页编程 | `write` | L48 |
   | 0x06 | 写使能（WEL） | `EnableWrite` | L80 |
   | 0x05 | 读状态寄存器 1 | `WaitBusy` | L162 |
   | 0x60 | 全片擦除 | `eraseChip` | L89 |
   | 0x20 | 4KB 扇区擦除 | `eraseSector` | L101 |
   | 0x52 | 32KB 块擦除 | `erase32Block` | L113 |
   | 0xD8 | 64KB 块擦除 | `erase64Block` | L130 |

   - 用计算器验证：`Cal::flash_address = 1048576`（1 MB），`HW::flash_address = 1048576 + 8192 = 1056768`；再核对 `Cal::Save()` 擦除的 8192 字节恰好是 2 个 4096 扇区（满足 `eraseRange` 的扇区对齐前置条件）。
3. **需要观察的现象**：所有**修改型**操作（写/擦）都先调 `EnableWrite()`，而读操作不需要；所有修改型操作之后都跟 `WaitBusy()`。
4. **预期结果**：一张 9 行的命令表加上三行分区算术。这是纯代码阅读实践，**待本地验证**的部分仅在于：若有逻辑分析仪/示波器接上 FLASH_CS 与 SPI1，可实测一次 `Cal::Save()` 的擦除+写入波形时序。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Flash::write()` 要求地址和长度都必须是 256 的倍数？`Cal::Save()` 是如何配合的？

答案：NOR Flash 的一次页编程以 256 字节页为单位，跨页需要拆成多次命令。驱动干脆只接受整页操作，把对齐责任交给调用者。`Cal::Save()` 在 [Cal.cpp:L53-L57](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Cal.cpp#L53-L57) 把 `sizeof(cal)` 向上取整到下一个页边界再调用 `write`，未用的尾部填充的是擦除后的 0xFF。

**练习 2**：`WaitBusy()` 里为什么有 `vTaskDelay(1)`？去掉它行不行？

答案：`vTaskDelay(1)` 让出 CPU 一个 tick，避免忙等空转占用 defaultTask。功能上去掉后轮询仍能工作，但会饿死同优先级任务、拉高功耗；更重要的是它表明该驱动隐含「只能在任务上下文调用」的约束——在中断上下文调用 `vTaskDelay` 会触发 FreeRTOS 断言（u5-l1 讲过的优先级红线）。

**练习 3**：固件升级会擦掉校准数据吗？依据是哪行代码？

答案：不会。升级擦除走 [App.cpp:L206](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L206) 的 `eraseRange(0, Firmware::maxSize)`，只擦 0 到 1 MB 的固件区；校准区起始地址就是 `Firmware::maxSize`，在擦除范围之外。

### 4.3 Firmware 升级状态机

#### 4.3.1 概念说明

固件升级是「两端状态机」协作的典范：

- **GUI 侧**（`FirmwareUpdateDialog`）：一个 6 状态的对话式状态机，每发一个命令就等 Ack 推进，QTimer 超时兜底。
- **设备侧**（`App.cpp` 的三个包处理 + `Firmware.cpp`）：接收分片写入 flash，最后自己把自己的固件换掉。

u1-l4 已经从构建侧讲过 `combined.vnafw` 文件的 24 字节头（magic "VNA!"、FPGA/MCU 各自起址与长度、链式 CRC32）。本讲从运行侧看这些字节如何被消费。`Firmware.cpp` 里 `Header` 结构与 AssembleFirmware.py 生成的头部逐字节对应——又一次「两端同源」的设计。

#### 4.3.2 核心流程

一次完整升级的时序：

```text
GUI                                    设备
── ClearFlash ────────────────────────► 擦除固件区 [0,1MB)，保留校准/配置
◄───────────────────────── Ack ───────
── FirmwarePacket(addr=0, 256B) ─────► Flash::write 页写入+回读校验
◄───────────────────────── Ack ───────
   （循环 file_size/256 次，每包就是 FW_CHUNK_SIZE=256 字节）
── PerformFirmwareUpdate ─────────────► GetFlashContentInfo()：
◄───────────────────────── Ack ───────   ① 校验 magic/起址/长度
                                        ② 链式 CRC32 校验全部镜像
                                        ③ 与 0x8000000 起的运行中固件逐字节比对
                                        vTaskDelay(100) 让 Ack 发完
                                        PerformUpdate():
                                          关中断 → 解锁内部 Flash
                                          → initiateRead 启动外部 flash 读
                                          → copy_flash()（RAM 中执行，永不返回）
 disconnectDevice()                        整片擦内部 Flash
 轮询 GetAvailableDevices()                逐 8 字节 SPI 收数 → 编程内部 Flash
 直到设备重新枚举 ──►                    软件复位，新固件启动
 重新连接 → 完成
```

GUI 状态机的 6 个状态定义在 [firmwareupdatedialog.h:L52-L59](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/firmwareupdatedialog.h#L52-L59)：`Idle → ErasingFLASH → TransferringData → TriggeringUpdate → WaitingForReboot → WaitBeforeInitializing → Idle`。

#### 4.3.3 源码精读

**① GUI 状态机：Ack 驱动 + 超时兜底**

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/firmwareupdatedialog.cpp:L70-L124](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/firmwareupdatedialog.cpp#L70-L124)：`on_bStart_clicked()` 先做文件体检——文件大小必须是 `FW_CHUNK_SIZE` 的整数倍、头 4 字节 magic 必须匹配（协议版本不符时降级为向用户确认），然后发 `ClearFlash` 并启动 20 秒超时。

[firmwareupdatedialog.cpp:L180-L219](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/firmwareupdatedialog.cpp#L180-L219)：`receivedAck()` 是状态机的推进器——`ErasingFLASH` 收到 Ack 就开始传数据；`TransferringData` 每收一个 Ack 发下一分片（[sendNextFirmwareChunk，L234-L241](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/firmwareupdatedialog.cpp#L234-L241)，`address` 就是文件内偏移，天然页对齐）；传完发 `PerformFirmwareUpdate`；收到最后一个 Ack 后主动断开连接、轮询设备重新枚举（[timerCallback，L147-L178](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/firmwareupdatedialog.cpp#L147-L178)）。任一环节 Nack 或超时都走 `abortWithError` 复位。

**② 设备侧三个包的落点**

[Software/VNA_embedded/Application/App.cpp:L201-L235](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L201-L235)（`#ifdef HAS_FLASH` 保护）：`ClearFlash` 先把设备切到 Idle 再擦固件区；`FirmwarePacket` 把 256 字节分片写入指定地址（驱动自带回读校验，失败回 Nack）；`PerformFirmwareUpdate` 先重新 `GetFlashContentInfo()` 校验，Ack 之后刻意 `vTaskDelay(100)` 让应答发完，再进入不可返回的 `PerformUpdate`。

**③ 镜像校验：头部 + 链式 CRC + 与运行中固件比对**

[Software/VNA_embedded/Application/Firmware.cpp:L23-L74](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Firmware.cpp#L23-L74)：`GetFlashContentInfo()` 分三步——① 头部合理性（magic、起址非空、大小不超上限，[L11-L12](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Firmware.cpp#L11-L12) 的 `FPGA_MAXSIZE`/`CPU_MAXSIZE` 兜底）；② 以 128 字节为块流式计算链式 CRC32（初值 `UINT32_MAX`，与 u1-l4 的 Python 组包端一致），与头部 `crc` 字段比对；③ 把外部 flash 里的 CPU 镜像与 `0x8000000`（STM32 内部 flash 基址）起的**正在运行的固件**逐字节比较，不同则置 `CPU_need_update`。

[App.cpp:L79-L92](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L79-L92)：上电自举流程。值得如实指出：`CPU_need_update` 分支里的 `Firmware::PerformUpdate(...)` 调用在**当前源码中是被注释掉的**（[App.cpp:L81-L84](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L81-L84)），即上电时并不自动刷新 MCU 固件，MCU 侧更新只由 `PerformFirmwareUpdate` 包显式触发；而 FPGA bitstream 的加载（`FPGA::Configure`）是上电必经路径，校验失败分别亮 LED 错误 2/3。

**④ `copy_flash()`：为什么一个函数调用都不许有**

[Software/VNA_embedded/Application/Firmware.cpp:L76-L160](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Firmware.cpp#L76-L160)：这是全仓库最「硬核」的 80 行。

- **为什么放 RAM**（`__attribute__((noinline, section(".data")))`，[L76](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Firmware.cpp#L76)）：它要**整片擦除 MCU 内部 flash**，而代码本身就在内部 flash 里——若在 flash 上执行，擦除一开始 CPU 就取不到指令了。放进 `.data` 段（RAM）才能边擦边跑。
- **为什么禁止函数调用**（源码注释 [L78-L85](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Firmware.cpp#L78-L85) 明言 `!NO FUNCTION CALLS AT ALL ARE ALLOWED IN HERE!`）：任何被调函数都可能位于将被覆盖的 flash 区域。所以它直接操作寄存器：禁 cache → `MER1` 整片擦 → 置 `PG` 位 → 裸打 `spi->DR` 寄存器逐字节收数（每 8 字节拼成两个 32 位字编程进内部 flash）→ 清错误标志。
- **为什么不能返回、只能复位**（[L149-L159](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Firmware.cpp#L149-L159)）：函数的返回地址在栈上，指向旧固件的某个地址——旧固件已被新固件覆盖，返回过去执行的是错误代码。因此最后写 `SCB->AIRCR` 触发软件系统复位，让 CPU 干净地从新固件的向量表启动。

[Software/VNA_embedded/Application/Firmware.cpp:L162-L182](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Firmware.cpp#L162-L182)：`PerformUpdate()` 是它的三行序曲——`__disable_irq()`（接下来没人能应答中断了）、`HAL_FLASH_Unlock()`、`HWHAL::flash.initiateRead(info.CPU_image_address)` 把外部 flash 的读命令发好（4.2 节埋下的伏笔），然后交棒。结尾的 `__builtin_unreachable()` 告诉编译器后面是死代码。

#### 4.3.4 代码实践：走读升级会话并标注安全点

1. **实践目标**：把一次固件升级拆成 GUI/设备两侧的步骤表，标出所有「防变砖」的设计点。
2. **操作步骤**：
   - 对照 [firmwareupdatedialog.cpp:L180-L219](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/firmwareupdatedialog.cpp#L180-L219) 与 [App.cpp:L201-L235](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L201-L235)，写一张两列时序表（左 GUI 动作，右设备动作）。
   - 单独列一栏「安全点」，至少找出这五个：① 每个分片写入都有 flash 驱动层回读校验；② `PerformFirmwareUpdate` 触发时设备端重跑完整的 magic+CRC 校验；③ `ClearFlash` 只擦固件区，校准与设备配置幸存；④ Ack 发出后 `vTaskDelay(100)` 才自杀式更新，保证 GUI 收到应答；⑤ GUI 的每一步都有 QTimer 超时兜底，Nack/超时即中止。
   - 思考题自答：升级中途拔线会怎样？（分片阶段：flash 固件区不完整，但 MCU 仍在跑内存中的旧固件，重传即可；`copy_flash` 阶段极短，若恰在其中断电，内部 flash 可能半新半旧——这正是头部+CRC 校验存在的意义，下次上电 `GetFlashContentInfo` 会判无效并亮错误码，仍可用 JTAG/SWD 救回。）
3. **需要观察的现象**：GUI 状态文本依次出现 `Erasing device memory...` → `Transferring firmware...`（进度条按 256 字节步进）→ `Triggering device update...` → `Rebooting device...` → `...device enumerated, update complete`，与状态机一一对应。
4. **预期结果**：一张双列时序表 + 至少 5 个安全点清单。纯代码走读即可完成；有设备与 `combined.vnafw` 时可实测一轮（风险自负，建议先确认文件与硬件版本匹配），否则标注**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`copy_flash()` 为什么不能有任何函数调用，也为什么不能正常 return？

答案：函数调用会跳转到位于内部 flash 的其它函数，而本函数正在整片擦除/覆盖内部 flash，那些代码随时失效；正常 return 则会弹出栈上的旧返回地址——它指向已被新固件覆盖的区域。所以它只能自包含地操作寄存器，结束时用 `SCB->AIRCR` 触发软件复位，从新固件向量表重新启动。

**练习 2**：`GetFlashContentInfo()` 的第三个校验（外部 flash 镜像与运行中固件比对）有什么用途？既然上电不会自动执行 `PerformUpdate`，它还有意义吗？

答案：它产生 `CPU_need_update` 标志，表达「外部 flash 里有一份与当前运行版本不同的 CPU 固件」。当前源码中消费该标志的上电自动更新调用被注释（App.cpp:L81-L84），所以此刻它主要作为状态查询/未来扩展接口存在——但这正是读源码要如实区分「能力」与「已接线的行为」的例子。

**练习 3**：升级包分片大小 256 字节是谁和谁之间的约定？在哪里能看到两边同源？

答案：是 GUI 组包端与设备 `Flash::write` 页大小之间的约定，常量是 `PacketConstants.h` 里的 `FW_CHUNK_SIZE`。GUI 的 [firmwareupdatedialog.cpp:L4](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/firmwareupdatedialog.cpp#L4) 直接 `#include "../../VNA_embedded/Application/Communication/PacketConstants.h"`——GUI 直接包含固件侧头文件，物理上不可能不同源（u1-l4 讲过的三语言隐式契约在这里闭环）。

## 5. 综合实践

**任务：绘制 LibreVNA 板载 Flash 的完整分区与生命周期图。**

不依赖硬件，仅凭本讲三个源文件 + `Hardware.hpp`，产出一张图文结合的「Flash 全景图」，要求包含：

1. **分区层**：按地址从低到高画出固件区（0–1 MB）、校准区（1 MB–1 MB+8 KB）、设备配置区（其后 4 KB），标注每区的定义常量、写入者、读取者。
2. **数据层**：在校准区内画出 `Calibration` 结构的内存布局（version → Source 表 → Receiver 表 → TCXO_PPM_correction），标出定点 LSB（10 Hz / 0.01 dB）与 `maxPoints=64` 的容量算术：`sizeof(CorrectionTable) = 1 + (4+2+2)×64 = 513` 字节（忽略对齐填充，实际以 `static_assert` 编译期保证不超 8 KB）。
3. **事件层**：在时间轴上标注五类事件分别触碰哪些分区——上电自举（读固件区）、固件升级（擦+写固件区）、幅度校准保存（擦+写校准区）、频率校准保存（擦+写校准区）、日常测量（什么都不写，只读 RAM 中的校准表）。
4. **验证**：用一句话回答——「刷固件后需要重新做设备校准吗？」（不需要，校准区在擦除范围之外；但若校准结构版本号变了，`Load()` 会主动丢弃旧校准。）

完成后，你手里这张图就是固件侧存储子系统的「一页纸文档」，可以直接补充进团队 wiki。

## 6. 本讲小结

- 设备级校准（`Cal.cpp`）保存三类系数：源幅度表、接收机幅度表（各最多 64 点、0.01 dB 步进、10 Hz 频率步进、线性插值）与 TCXO 频率 ppm；它让设备「说真话」，与修正测量夹具误差的 GUI 级 SOLT 校准（单元 9）职责正交。
- 校准数据住在板载 SPI NOR Flash 的独立分区（1 MB 起始、8 KB），上电 `Cal::Load()` 带版本校验与空表容错，最坏退回零修正而不变砖；PC 端经 `SourceCalPoint`/`ReceiverCalPoint`/`FrequencyCorrection` 包逐点写入，「收到最后一个点」即自动落盘。
- `Flash.cpp` 是教科书级 NOR Flash 驱动：页编程 + 多档擦除 + WEL 解锁 + 忙等待 + 逐页回读校验；`WaitBusy` 里的 `vTaskDelay` 决定了它只能在任务上下文使用。
- `eraseRange(0, Firmware::maxSize)` 只擦固件区——固件升级天然保留校准与设备配置，这是分区设计的直接收益。
- 固件升级是两端状态机：GUI 六状态以 Ack 推进、超时兜底；设备端三个包（ClearFlash/FirmwarePacket/PerformFirmwareUpdate）完成「擦 → 分片写入 → 校验 → 自我更新」。
- `copy_flash()` 因「擦的是自己脚下的 flash」而必须在 RAM 运行、禁止一切函数调用、以软件复位收尾——这是本仓库对嵌入式自我更新最难一课的最简解法。

## 7. 下一步学习建议

下一讲 **u5-l4 设备端三大模式**将走进 `VNA.cpp` / `SpectrumAnalyzer.cpp` / `Generator.cpp` 的主循环——本讲已为它备好三块垫脚石：`FrequencyCorrectionToDevice` 在扫描配置中的调用点、`GetAmplitudeSettings` 的源校准入口、以及「校准后的接收数据如何离开设备」。建议先复习 u5-l2 的硬件门面再进入下一讲。

若你想横向扩展本讲内容，推荐两条支线：① 回头重读 u1-l4 的 `AssembleFirmware.py`，把 24 字节头部的每个字段与 `Firmware.cpp` 的 `Header` 结构逐一对齐，体会「两端同源」；② 提前翻一眼 GUI 的 `Calibration/calibration.cpp`（单元 9 的主角），对照本讲建立「设备级 vs GUI 级」校准的完整心智地图。
