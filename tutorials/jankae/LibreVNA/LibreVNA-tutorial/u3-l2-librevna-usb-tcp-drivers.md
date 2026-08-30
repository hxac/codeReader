# LibreVNA 驱动：USB 与 TCP 两条通道

## 1. 本讲目标

上一讲（u3-l1）我们看清了 `DeviceDriver` 这个抽象基类定下的"契约"。本讲进入契约的最重要实现者——官方 LibreVNA 驱动。它其实不是一个类，而是**三个类组成的两层结构**：

- `LibreVNADriver`：公共层，只关心"协议内容"（发什么包、收到的包如何变成测量数据），完全不关心字节怎么到达；
- `LibreVNAUSBDriver`：传输层之一，用 libusb 走 USB 批量传输；
- `LibreVNATCPDriver`：传输层之二，用两条 TCP 连接走网络（配合设备端固件的 TCP 桥接）。

学完本讲，你应该能够：

1. 跟踪 USB 路径下"设备发现 → 打开 → 接收线程 → 解析成包"的完整调用链；
2. 跟踪 TCP 路径下基于 SSDP（UDP 多播）的设备发现与双 socket 数据收发；
3. 说出两条路径在哪一层汇合（`SendPacket` / `receivedPacket`），以及 `USBInBuffer` 这类接收缓冲工具存在的理由；
4. 独立画出"一包原始字节 → `DeviceDriver::VNAMeasurement` → Qt 信号"的全链路序列图，并标注文件与行号。

## 2. 前置知识

本讲假设你已读过 u3-l1（`DeviceDriver` 抽象）和 u2-l1（AppWindow 启动），另外补充几个背景概念：

- **libusb**：一个跨平台的用户态 USB 库。应用程序不用写内核驱动，直接通过它枚举设备、claim 接口、提交"批量传输"（bulk transfer）。LibreVNA GUI 只依赖 Qt 和 libusb 两个外部库（见 u1-l3）。
- **USB 端点（Endpoint）**：USB 设备对外暴露若干单向"管道"，用地址编号。LibreVNA 用了三个：`0x01`（主机→设备，发命令）、`0x81`（设备→主机，回测量数据）、`0x82`（设备→主机，回日志文本）。
- **异步传输与事件循环**：libusb 的典型用法是提交一个传输请求，然后在一个专门线程里反复调用 `libusb_handle_events()`；传输完成时 libusb 回调你的函数。回调发生在**libusb 的事件线程**，不是 Qt 主线程——这是本讲最重要的线程知识点。
- **Qt 信号槽的连接类型**：`Qt::DirectConnection` 表示"在发射信号的线程里直接调用槽"；`Qt::QueuedConnection` 表示"把调用投递到接收者所属线程的事件队列里"。本讲会看到两种都用上了，而且是有意为之。
- **SSDP**：Simple Service Discovery Protocol，UDP 多播（`239.255.255.250:1900`）上的服务发现协议，是 UPnP 的一部分。家里局域网的设备互相"打招呼"常用它。TCP 驱动借它来找网络上的 LibreVNA。
- **协议帧格式**：`Protocol.hpp`/`Protocol.cpp` 定义的二进制帧为「1 字节帧头 `PCKT_HEADER_DATA` + 2 字节长度 + 1 字节类型 + 载荷 + 4 字节 CRC32」，详见 [Protocol.cpp:5-12](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.cpp#L5-L12)。本讲只用它，逐字段拆解留给单元 4。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Device/LibreVNA/librevnadriver.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.h) | 公共层类声明：`SendPacket` 纯虚接口、`TransmissionResult` 枚举、测量信号 |
| [Device/LibreVNA/librevnadriver.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp) | 公共层实现：把 `setVNA/setSA/setSG` 翻译成协议包；`handleReceivedPacket` 把协议包翻译回测量数据 |
| [Device/LibreVNA/librevnausbdriver.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.h) / [.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp) | USB 传输层：libusb 枚举/连接、接收线程、发送队列 |
| [Device/LibreVNA/librevnatcpdriver.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.h) / [.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp) | TCP 传输层：SSDP 发现、双 TCP socket 收发 |
| [Util/usbinbuffer.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.h) / [.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.cpp) | 通用工具：封装"一块 USB 接收缓冲 + 循环提交的异步批量传输" |
| [Device/devicedriver.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp) | `getDrivers()` 注册表：两个传输驱动在这里各占一行 |

三个类如何注册进 GUI 的设备列表，见 [devicedriver.cpp:19-32](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L19-L32)——USB 和 TCP 驱动是**两个并列的独立驱动**（驱动名分别是 `"LibreVNA/USB"` 与 `"LibreVNA/TCP"`），在设备下拉框里表现为两类不同的"设备源"。

## 4. 核心概念与源码讲解

### 4.1 LibreVNADriver 公共层：只谈协议，不谈管道

#### 4.1.1 概念说明

`LibreVNADriver` 解决的问题是：**同一个协议，两种传输方式**。USB 插线能用，网络桥接也能用，但"扫描设置怎么填、测量数据怎么读"这部分逻辑完全一样。于是它把这部分逻辑上提，把"字节怎么发出去"下放成一个纯虚函数：

```cpp
virtual bool SendPacket(const Protocol::PacketInfo& packet,
                        std::function<void(TransmissionResult)> cb = nullptr,
                        unsigned int timeout = 500) = 0;
```

见 [librevnadriver.h:180](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.h#L180)。注意 `SendPacket` 是**异步**的：调用立刻返回，结果（Ack/Nack/超时）稍后通过回调 `cb` 报告。结果的取值就是 [librevnadriver.h:15-20](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.h#L15-L20) 的 `TransmissionResult` 枚举：`Ack`（设备确认）、`Nack`（设备拒绝）、`Timeout`（超时无响应）、`InternalError`（本地发送失败）。

公共层与传输层的分工边界可以概括成一张表：

| 关注点 | 公共层 `LibreVNADriver` | 传输层 USB / TCP 子类 |
| --- | --- | --- |
| 设备发现（`GetAvailableDevices`） | 不实现 | 各自实现（USB 枚举 / SSDP 多播） |
| 连接生命周期（`connectTo`/`disconnect`） | 不实现 | 各自实现 |
| 字节进出设备 | 纯虚 `SendPacket` 交给子类 | `libusb_bulk_transfer` / `QTcpSocket::write` |
| 配置翻译（`setVNA`/`setSA`/`setSG`/`setIdle`） | ✅ | 继承直接用 |
| 收包解释（`handleReceivedPacket`） | ✅ | 子类只负责把字节解析成包再转发 |

#### 4.1.2 核心流程

**发送方向**（上层模式 → 设备）：

```text
Mode::initializeDevice()
  └─ DeviceDriver::setVNA(settings, cb)            ← 上层只认抽象接口
       └─ LibreVNADriver::setVNA()
            ├─ 把 VNASettings 逐字段抄进 Protocol::SweepSettings
            ├─ 记录 portStageMapping / zerospan / lastNonIdlePacket
            └─ SendPacket(p, 回调包装)              ← 纯虚，落到 USB 或 TCP 子类
                 └─ 入队 transmissionQueue，空闲则立即发送
设备回 Ack/Nack ─→ transmissionFinished() ─→ 触发 cb(Ack?) ─→ 上层模式得到通知
```

**接收方向**（设备 → 上层模式）：

```text
传输层收到字节 ─→ Protocol::DecodeBuffer() 解出一个 PacketInfo
  ├─ Ack/Nack       ─→ emit receivedAnswer(...)   → 传输层的发送队列消费
  ├─ Set/ClearTrigger ─→ emit receivedTrigger(...)
  └─ 其余（DeviceInfo/DeviceStatus/VNADatapoint/SAResult...）
                    ─→ emit receivedPacket(packet) [队列连接，切回 GUI 线程]
                         └─ LibreVNADriver::handleReceivedPacket()
                              ├─ DeviceInfo  → 填 Info/Limits → emit InfoUpdated()
                              ├─ DeviceStatus → 存 lastStatus → emit StatusUpdated()/FlagsUpdated()
                              ├─ VNADatapoint → 组装 VNAMeasurement → emit VNAmeasurementReceived(m)
                              └─ SpectrumAnalyzerResult → 组装 SAMeasurement → emit SAmeasurementReceived(m)
```

#### 4.1.3 源码精读

**① 配置翻译：`setVNA`**

[librevnadriver.cpp:480-532](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L480-L532) 把 u3-l1 学过的硬件无关结构 `DeviceDriver::VNASettings` 翻译成固件认识的 `Protocol::SweepSettings` 包。关键几行：

```cpp
p.type = Protocol::PacketType::SweepSettings;
p.settings.f_start = s.freqStart;
...
p.settings.cdbm_excitation_start = s.dBmStart * 100;   // dBm → 厘dBm，定点化
```

三个值得注意的细节：

- [L486-488](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L486-L488)：`excitedPorts` 为空时不发扫描设置，而是转 `setIdle`——"什么都不测"也是一种设备状态。
- [L491-494](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L491-L494)：建立 `portStageMapping`（端口→激励阶段）。一次多端口扫描分多个"阶段"（stage），每个阶段由一个端口输出激励、所有端口同时接收；这张表记录"哪个端口在第几阶段激励"，接收时要用它反查。
- [L516](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L516)：`zerospan = (s.freqStart == s.freqStop) && (s.dBmStart == s.dBmStop);` 起止频率相同即"零扫宽"，此时 X 轴从频率变成时间——这个标志会决定后面接收端填 `m.frequency` 还是 `m.us`。

最后 [L527-531](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L527-L531) 调用 `SendPacket`，并把 `TransmissionResult` 收敛成上层期望的 `bool` 回调。`setSA`（[L543-589](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L543-L589)）、`setSG`（[L600-611](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L600-L611)）、`setIdle`（[L613-623](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L613-L623)）是同一套模式的变体，可自行对照阅读。无载荷命令（如 `RequestDeviceInfo`）走便捷封装 [sendWithoutPayload, L890-895](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L890-L895)。

**② 收包解释：`handleReceivedPacket`**

这是接收方向的公共终点，[librevnadriver.cpp:696-814](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L696-L814)。四种包的处理：

- **DeviceInfo**（[L705-757](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L705-L757)）：先比对协议版本，不一致会弹窗建议升级固件（[L707-717](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L707-L717)——这正是 u1-l2 说的"两端共同编译 Protocol.hpp"在运行时的兜底）；然后把设备的端口数、频率/功率/点数限制逐项抄进 `info.Limits`（[L728-752](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L728-L752)），最后 `emit InfoUpdated()`。注意 `harmonicMixing` 打开时 maxFreq 改用 `limits_maxFreqHarmonic`（[L730](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L730)），这就是设置页里"谐波混频扩展到 18GHz"开关的落点。
- **DeviceStatus**（[L759-763](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L759-L763)）：原样存 `lastStatus`，发两个信号。状态栏温度读数（`getStatus`，[L360-402](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L360-L402)）和 `getFlags()` 里的 Unlocked/Unlevel/Overload（[L305-358](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L305-L358)）都从这份缓存读。
- **VNADatapoint**（[L764-796](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L764-L796)）：本讲的主线。固件送上来的一个数据点包含"各阶段、各端口接收机的原始读数"，驱动负责把它们换算成 S 参数：

  \[ S_{ij} = \frac{b_i}{a_j} = \frac{\text{第 } j \text{ 端口激励时的接收读数 } i}{\text{第 } j \text{ 端口激励时的参考通道读数}} \]

  对应代码就是 [L775-793](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L775-L793) 的双重循环：外层遍历 `portStageMapping`（谁在激励），内层遍历所有接收端口，`m.measurements[name] = input / ref;`（[L784](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L784)）。`ref`/`input` 若为 NaN（该阶段没测）则跳过；若驱动设置 `captureRawReceiverValues`，还会额外输出未经归一化的原始接收值（[L786-791](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L786-L791)）。组装完 `emit VNAmeasurementReceived(m)`（[L795](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L795)）——这个信号就是 u3-l1 说的"数据经 Qt 信号推送"的推送点。
- **SpectrumAnalyzerResult**（[L798-810](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L798-L810)）：更简单，直接把两个端口的线性电压值装进 `m.measurements["PORT1"]/["PORT2"]`，`emit SAmeasurementReceived(m)`。

另外注意函数开头 [L698](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L698) 的 `emit passOnReceivedPacket(packet);` 和 [L700-702](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L700-L702) 的 `skipOwnPacketHandling`：这是给 `CompoundDriver`（把多台设备组合成虚拟多端口机，u3-l3 讲）留的"旁听原始包"后门。

**③ 构造函数：驱动专属的菜单与 SCPI**

[librevnadriver.cpp:114-303](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L114-L303) 的构造函数向基类的 `specificActions` 注册了 Manual Control、Configuration、Firmware Update、三类设备校准、Packet Log 等右键菜单项，并按硬件版本（`0x01`/`0xD0`/`0xE0`/`0xFE`/`0xFF` 对应 LibreVNA、HAR0、SAP1、P2、PT 五种 jankae 家设备）控制可见性（[L242-247](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L242-L247)）。其中 "View Packet Log"（[L234-239](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L234-L239)）与 SCPI 命令 `DEVice:PACKETLOG`（[L299-302](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L299-L302)）都读 `DevicePacketLog` 单例——后面 USB/TCP 的收发路径里你会反复看到往这个日志里塞记录的调用。

#### 4.1.4 代码实践

**实践目标**：亲手核对"配置翻译"这张表，确认上层设置与协议字段一一对应。

**操作步骤**：

1. 打开 [librevnadriver.cpp:496-522](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L496-L522)，抄下 `setVNA` 中所有 `p.settings.xxx = s.yyy;` 赋值行。
2. 为每一行补上"单位换算/语义"备注，例如 `cdbm_excitation_start = s.dBmStart * 100` 备注为「dBm×100 → 厘dBm 定点数」。
3. 特别留意三行不是简单赋值的：`dwell_us` 的钳位（L505-511）、`fixedPowerSetting`（L513）、`portXStage` 的 `find(...) - begin()`（L517-520），写下你对它们含义的推断。
4. 用同样方法扫一遍 `setSA` 里的 `UseDFT` 判定（[L569-571](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L569-L571)）：什么条件下 GUI 会请求设备改用 DFT 方式取频谱数据？

**需要观察的现象**：纯源码阅读，无运行现象。

**预期结果**：你会得到一张约 20 行的翻译表；`UseDFT` 的答案应是"未开跟踪源 + 驱动设置允许 + RBW 不超过阈值 + 非零扫宽"四个条件同时满足。

#### 4.1.5 小练习与答案

**练习 1**：`setExtRef`（[L625-681](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L625-L681)）在设备"非空闲"时为什么要先 `SetIdle` 再切参考再重发 `lastNonIdlePacket`？

**答案**：代码注释（[L671-674](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L671-L674)）写明：扫描进行中切换参考时钟，若设备端存在频率校准，会产生错误频率。所以顺序是"停下 → 切参考 → 用 `lastNonIdlePacket` 恢复原测量"。`lastNonIdlePacket` 在每次 `setVNA/setSA/setSG` 里被更新（如 [L524-525](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L524-L525)），就是为这里的"恢复"准备的。

**练习 2**：`VNAmeasurementReceived` 信号携带的 `VNAMeasurement` 里，`measurements` 为什么用 `QString` 做 key 而不是固定数组下标？

**答案**：这是 u3-l1 讲过的设计——端口数因设备而异（LibreVNA 是 2 端口，CompoundDriver 可拼出更多），用 `"S11"`、`"S21"` 这类字符串键可以让上层（Trace、校准模块）完全不知道端口数量，第三方驱动也能用同样的容器返回自己的参数名。构造 key 的代码就在 [L783](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L783)。

**练习 3**：如果设备固件比 GUI 旧、协议版本不一致，用户会在什么时候、以什么形式得知？

**答案**：连接建立后驱动主动发送 `RequestDeviceInfo`（见 4.2/4.3 的 `connectTo` 末尾），设备回 `DeviceInfo` 包，`handleReceivedPacket` 比对 `ProtocolVersion != Protocol::Version` 后弹窗询问是否立即升级固件（[L707-717](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L707-L717)）。也就是说版本协商不是连接握手的一部分，而是连上后第一条"问答"驱动的。

### 4.2 USB 驱动：libusb、接收线程与 USBInBuffer

#### 4.2.1 概念说明

USB 是 LibreVNA 的主通道。`LibreVNAUSBDriver` 要解决四件事：

1. **发现**：扫总线，找出"VID/PID + 产品字符串"都匹配的设备，读出序列号；
2. **连接**：`libusb_claim_interface` 独占接口，启动事件线程，创建两个接收缓冲，主动请求设备信息；
3. **收**：三个端点里两个是输入（数据 `0x81`、日志 `0x82`，定义在 [librevnausbdriver.h:48-50](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.h#L48-L50)），需要有人不停地"提交异步传输→等完成→再提交"；
4. **发**：协议要求一问一答（发了命令要等 Ack），所以发送必须排队，同一时刻只有一个包"在途"。

第 3 件事被抽成了通用工具 `USBInBuffer`（[Util/usbinbuffer.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.h)）：它代表"绑定了某个输入端点的一块接收缓冲"，内部持有 libusb transfer 对象并自动循环提交。驱动只管在 `DataReceived` 信号里消费数据、调用 `removeBytes()` 声明"这些字节我处理完了"。

#### 4.2.2 核心流程

**设备发现与连接（`GetAvailableDevices` / `connectTo` / `SearchDevices`）**：

```text
GetAvailableDevices()
  └─ libusb_init(临时ctx) → SearchDevices(收集序列号, ignoreOpenError=true) → libusb_exit
connectTo(serial)
  ├─ SearchDevices(..., ignoreOpenError=false)   ← 找到目标序列号即中止，保留句柄
  ├─ libusb_claim_interface(handle, 0)           ← 独占接口，失败则报错抛异常
  ├─ new std::thread(USBHandleThread)            ← libusb 事件线程
  ├─ new USBInBuffer(handle, 0x81, 65536)        ← 数据端点
  ├─ new USBInBuffer(handle, 0x82, 65536)        ← 日志端点
  ├─ connect(DataReceived → ReceivedData, DirectConnection)   ★ 关键
  ├─ connect(TransferError   → ConnectionLost)
  ├─ connect(receivedPacket  → handleReceivedPacket, QueuedConnection) ★ 关键
  ├─ sendWithoutPayload(RequestDeviceInfo)       ← 连接后第一问
  └─ sendWithoutPayload(RequestDeviceStatus)
```

`SearchDevices`（[librevnausbdriver.cpp:288-359](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L288-L359)）的筛选条件是三重过滤：VID/PID 命中白名单 → `libusb_open` 成功 → 产品字符串等于 `"VNA"`。白名单有三组 ID（[L23-25](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L23-L25)）：`0483:564e`（ST 官方 VID 的早期固件）、`0483:4121`（ST VID）、`1209:4121`（pid.codes 开源 VID，"A" 的十六进制 0x41/0x21 恰是 "A!" 的 ASCII——一个有品味的开源产品号）。

**接收路径（本讲主线）**：

```text
[libusb 事件线程]  USBHandleThread: while(connected) libusb_handle_events()
    └─ USBInBuffer::Callback(transfer)          usbinbuffer.cpp:57
         ├─ received_size += actual_length
         └─ emit DataReceived()  ──(DirectConnection, 仍在 libusb 线程)──▶
    LibreVNAUSBDriver::ReceivedData()           librevnausbdriver.cpp:158
         ├─ Protocol::DecodeBuffer(buffer, received, &packet)   ← 解一帧
         ├─ DevicePacketLog 记录
         ├─ dataBuffer->removeBytes(handled_len) ← 消费掉已解析字节
         ├─ Ack/Nack → emit receivedAnswer(...)   → 发送队列消费
         └─ 其他    → emit receivedPacket(packet) ──(QueuedConnection)──▶
    [GUI 线程] LibreVNADriver::handleReceivedPacket(packet)   librevnadriver.cpp:696
         └─ VNADatapoint → 组装 VNAMeasurement → emit VNAmeasurementReceived(m)
```

线程视角总结成一句话：**解析原始字节发生在 libusb 线程（DirectConnection），解释协议包发生在 GUI 线程（QueuedConnection）**，两次 `connect` 分别在 [librevnausbdriver.cpp:116](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L116) 和 [L121](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L121)。

**发送路径（一问一答的队列）**：

```text
SendPacket(p, cb, timeout)          librevnausbdriver.cpp:264
  ├─ transmissionQueue.enqueue({p, timeout, cb})
  └─ 若无在途包 → startNextTransmission()    L361
       ├─ Protocol::EncodePacket(p → 1024字节栈缓冲)
       ├─ DevicePacketLog.addPacket(p)
       ├─ libusb_bulk_transfer(handle, 0x01, buffer, length, ...)   ← 同步阻塞写
       └─ transmissionTimer.start(timeout)    ← 超时保险
设备回 Ack/Nack → ReceivedData → emit receivedAnswer
  → (Queued) transmissionFinished(result)    librevnausbdriver.cpp:228
       ├─ dequeue + 调用 cb(result)
       ├─ transmissionTimer.stop()
       └─ 队列非空则 startNextTransmission() 继续下一包
```

#### 4.2.3 源码精读

**① 连接建立**：[librevnausbdriver.cpp:61-129](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L61-L129)。`SearchDevices` 的回调（[L77-88](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L77-L88)）返回 `false` 表示"就是它，中止搜索并保留句柄"——这是 `SearchDevices` 注释里约定的协议（[头文件 L55-57](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.h#L55-L57)）。claim 失败的报错（[L100-110](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L100-L110)）直接提示了两个最常见原因：Linux 缺 udev 规则、Windows 已被另一个实例占用——排查连不上设备时先看这里。**顺序细节**：接收线程必须先于两个 `USBInBuffer` 启动（[L113-115](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L113-L115)），因为 `USBInBuffer` 构造函数最后一行就提交了传输（[usbinbuffer.cpp:19](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.cpp#L19)），若无人 `handle_events` 它永远完成不了。

**② 接收数据拆包**：[librevnausbdriver.cpp:158-211](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L158-L211)。`do...while(handled_len > 0)` 循环每次调 `Protocol::DecodeBuffer` 解出**一帧**（[L165](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L165)）——一次 USB 传输里可能粘着好几帧、也可能只有半帧，`DecodeBuffer` 的返回值是"本次消费的字节数"，半帧时返回 0 且 `packet.type = None`，等下次数据到达凑齐（其内部逻辑见 [Protocol.cpp:28-93](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.cpp#L28-L93)：跳过帧头前的杂字节、校验长度与 CRC32、`VNADatapoint` 因体积大被固件豁免了 CRC——尾 4 字节恒为 0，见 [L79-90](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.cpp#L79-L90)）。解析出的帧先记入 `DevicePacketLog`（[L168-174](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L168-L174)，无效字节也记，便于排查协议失步），再 `removeBytes` 腾出缓冲（[L175](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L175)），最后按类型分流（[L182-201](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L182-L201)）。

**③ USBInBuffer 的循环提交**：[usbinbuffer.cpp:57-98](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.cpp#L57-L98)。这是本模块最精巧的 40 行：

- [L66-75](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.cpp#L66-L75)：传输完成且有数据时，`received_size` 累加，然后 `emit DataReceived()`。注意 `inCallback` 标志位在发射前置真、发射后置 false——`removeBytes()` 靠它保证"只允许在回调内被调用"（[L40-42](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.cpp#L40-L42) 抛异常）。这不是多线程保护（单线程事件模型下不需要），而是**调用时机**保护：缓冲只有在回调栈内是静止的。
- [L94-97](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.cpp#L94-L97)：重新提交传输时，目标地址是 `&buffer[received_size]`（**追加**在未消费数据之后，所以缓冲同时承担"半帧暂存"职责），长度向下对齐到 512 的倍数——USB 高速批量传输以 512 字节包为单元，不对齐会让末尾零头每次都短包。`removeBytes`（[L38-50](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.cpp#L38-L50)）消费后用 `memmove` 把剩余字节挪回首部，缓冲就这样无限滚动。
- [L76-87](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.cpp#L76-L87)：`NO_DEVICE`（拔线）与 `ERROR/OVERFLOW/STALL` 分支释放 transfer 并 `emit TransferError()`——驱动把它连到 `ConnectionLost`（[librevnausbdriver.cpp:117](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L117)），这就是"设备被拔出后 GUI 弹窗提示"的起点。
- 静态蹦床函数 `CallbackTrampoline`（[L100-104](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.cpp#L100-L104)）：libusb 是 C API，只认普通函数指针，靠 `transfer->user_data` 里存的对象指针转回成员函数——C 库回调接入 C++ 类的标准手法。

**④ 断开与收尾**：[librevnausbdriver.cpp:131-156](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L131-L156)。顺序是 `setIdle`（让设备停扫）→ 删两个缓冲（析构里 `libusb_cancel_transfer` 并等条件变量，[usbinbuffer.cpp:22-36](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.cpp#L22-L36)）→ `connected = false`（让事件线程的 while 退出）→ 释放接口、关句柄 → `join` 线程。删缓冲在置 `connected=false` 之前，保证 cancel 生效时 `handle_events` 还在跑。

#### 4.2.4 代码实践

**实践目标**：验证"VID/PID → 驱动识别"这一环，并体会回调线程约束。

**操作步骤**：

1. 在终端运行 `lsusb`（Linux；macOS 可用 `system_profiler SPUSBDataType`，Windows 用设备管理器）。搜索输出中是否出现 `0483:4121`、`0483:564e` 或 `1209:4121`。
2. 对照 [librevnausbdriver.cpp:306-316](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L306-L316)：即使 VID/PID 命中，代码还会再读产品字符串比对 `"VNA"`（[L336-346](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L336-L346)）。想一想：为什么 ST 的 VID `0483` 下有无数设备，仅凭 PID 不够吗？这个双重过滤防的是什么？
3. 源码走读（无硬件也能做）：假设你在 `ReceivedData` 之外的地方（比如某个按钮的槽）调用 `dataBuffer->removeBytes(10)`，读 [usbinbuffer.cpp:38-42](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.cpp#L38-L42)，预测会发生什么。
4. （可选，需硬件 + 已按 u1-l3 配好 udev 规则）连接设备后打开设备菜单里的 "View Packet Log"（入口见 [librevnadriver.cpp:234-239](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L234-L239)），观察连接后最先出现的两个包是不是 `RequestDeviceInfo`/`RequestDeviceStatus` 的请求与应答。

**需要观察的现象**：步骤 1 只需终端输出；步骤 4 若无硬件则**待本地验证**。

**预期结果**：步骤 2 的答案是——ST VID + 任意自选 PID 仍可能撞上其他 ST 开发板产品，再比对产品字符串 `"VNA"` 才能确证是本设备固件；步骤 3 会抛出 `runtime_error("Removing of bytes is only allowed from within receive callback")`，因为此时 `inCallback == false`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ReceivedData` 与 `DataReceived` 的连接必须用 `Qt::DirectConnection`（[L116](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L116)），而 `receivedPacket` 与 `handleReceivedPacket` 用 `Qt::QueuedConnection`（[L121](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L121)）？

**答案**：`DataReceived` 由 libusb 事件线程发射，而 `USBInBuffer` 属于该线程正在使用的对象；若用队列连接，`ReceivedData` 会被推迟到 GUI 线程执行，期间 `USBInBuffer::Callback` 已经重新提交传输、`received_size` 可能继续增长，`removeBytes` 的"回调栈内缓冲静止"前提被打破，`memmove` 与新数据写入会交错竞争。所以解析+消费必须在 libusb 线程内同步完成。而 `handleReceivedPacket` 会触碰 `Info`、`lastStatus`、各种 Qt 对象并最终驱动 GUI，这些必须在 GUI 线程执行，于是跨线程的那一跳用队列连接完成——传递的 `Protocol::PacketInfo` 也因此在 [librevnadriver.cpp:683-688](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L683-L688) 用 `qRegisterMetaType` 注册过。

**练习 2**：`startNextTransmission`（[L361-388](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L361-L388)）里发送用的是**同步**的 `libusb_bulk_transfer`，它运行在哪个线程？会卡住 GUI 吗？

**答案**：`startNextTransmission` 只有两个调用者：`SendPacket`（谁调用就在谁的线程，通常是 GUI 线程）和 `transmissionFinished`（队列连接，GUI 线程）。所以 bulk 写发生在 GUI 线程。同步 bulk transfer 在数据立刻可写时几乎不阻塞（libusb 内部有缓冲），但理论上长传输会短暂卡 GUI——这是用"简单同步写"换来的取舍；读方向才是高频大流量，所以读完全放到了独立线程+异步传输。

**练习 3**：传输队列为什么必须"一次只允许一个在途包"？

**答案**：因为应答不携带序号。设备回的 `Ack/Nack` 无法指明"确认的是哪个包"，若同时发两个包，收到一个 Ack 时不知道该弹出队列里的哪一个。代码把这一约定写死在流程里：`SendPacket` 只在 `!transmissionActive` 时调用 `startNextTransmission`（[L273-275](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L273-L275)），`transmissionFinished` 弹出队首并继续下一个（[L249-261](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L249-L261)）；超时由 `transmissionTimer` 兜底（[L43-45](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.h#L43-L45) 定义，[L119](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L119) 连接）。"stray Ack"（队列空却收到 Ack）也会被显式警告（[L232-234](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L232-L234)）。

### 4.3 TCP 驱动：SSDP 发现与双 socket 传输

#### 4.3.1 概念说明

`LibreVNATCPDriver` 面向的场景是：设备不插在 PC 的 USB 口上，而是通过某种网络桥接（例如单板机上跑一个把 USB 转网络的守护进程）接入局域网。它的类结构与 USB 版几乎对称（对比 [librevnausbdriver.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.h) 与 [librevnatcpdriver.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.h)：连成员名 `transmissionQueue/transmissionTimer/m_receiveThread` 都一致），差异全在"怎么找到设备"和"字节走哪条管道"：

- **发现**：不再扫总线，而是 SSDP 多播——驱动每秒向 `239.255.255.250:1900` 发 `M-SEARCH`，网络上的 LibreVNA 桥接应答自己的 IP 和序列号，驱动维护一张带过期时间的 `detectedDevices` 表；
- **传输**：不是"一个接口三个端点"，而是**两条 TCP 连接**——数据走 `19544` 端口、日志走 `19545` 端口（[librevnatcpdriver.cpp:13-14](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L13-L14)）。连接上之后，**字节流里跑的仍然是同一套 `Protocol` 帧**，这正是公共层 `LibreVNADriver` 能被两边复用的原因。

#### 4.3.2 核心流程

**设备发现（SSDP 生命周期）**：

```text
构造函数：为每个可用网络接口建 QUdpSocket，加入多播组          L25-45
ssdpTimer(1000ms) → SSDRequest()：向多播组发 M-SEARCH 报文      L157-174
   报文: M-SEARCH * HTTP/1.1 / HOST:239.255.255.250:1900
         / MAN:"ssdp:discover" / MX:1 / ST:urn:...:LibreVNA:1
设备应答 → SSDPreceived()：解析 LOCATION/ST/LibreVNA-serial/
   CACHE-CONTROL 字段 → addDetectedDevice()（同序列号则替换）  L176-217, L326-337
pruneDetectedDevices()：超过 max-age 未刷新的条目删除          L339-349
GetAvailableDevices()：返回表中所有序列号                      L64-72
```

**连接与接收**：

```text
connectTo(serial)
  ├─ 在 detectedDevices 里查序列号 → 得到 IP；查不到直接 false   L81-93
  ├─ dataSocket.connectToHost(ip, 19544); logSocket → 19545     L96-97
  ├─ waitForConnected(1000)，任一失败即报错返回                  L100-106
  ├─ errorOccurred → ConnectionLost（断线感知）                  L111-112
  ├─ readyRead → ReceivedData / ReceivedLog                     L118-119
  ├─ receivedPacket → handleReceivedPacket（Queued）             L122
  └─ RequestDeviceInfo / RequestDeviceStatus                    L126-127

[Qt 主线程] dataSocket 有数据 → ReceivedData()                  L219-257
  ├─ dataBuffer.append(dataSocket.readAll())   ← QByteArray 累积
  ├─ Protocol::DecodeBuffer(...)  ← 与 USB 完全相同的拆包循环
  └─ 同样的 switch 分流 → emit receivedPacket / receivedAnswer
```

#### 4.3.3 源码精读

**① SSDP 发现三件套**：[librevnatcpdriver.cpp:157-174](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L157-L174) 的 `SSDRequest` 拼出标准 SSDP `M-SEARCH` 报文，搜索目标（ST）是自定义服务类型 `urn:schemas-upnp-org:device:LibreVNA:1`（[L12](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L12)）。应答解析在 [L176-217](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L176-L217)：逐行找 `LOCATION:`（设备 IP）、`LibreVNA-serial:`（序列号）、`CACHE-CONTROL: max-age=N`（有效期）。四字段缺一则丢弃该应答。`pruneDetectedDevices`（[L339-349](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L339-L349)）在每次发搜索前剔除过期条目——所以设备断电后，下拉框里的它最多再活 `max-age` 秒。这套"周期性宣告 + 老化淘汰"和 mDNS/Bonjour 是同一思想。

**② 连接建立**：[librevnatcpdriver.cpp:74-130](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L74-L130)。与 USB 版逐行对照会发现结构完全同构：查设备 → 建管道 → 连信号 → 发 `RequestDeviceInfo`/`RequestDeviceStatus`。两个易被忽略的差别：

- TCP 版连接失败**返回 false**（[L100-106](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L100-L106)），USB 版 claim 失败则 `throw std::runtime_error`（[librevnausbdriver.cpp:94/109](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L90-L110)）——上层对两种驱动失败的容错路径并不相同。
- TCP 版的构造函数会**为每个网络接口**建一个多播 socket（[L25-45](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L25-L45)，只留 Ethernet/Wifi/Virtual/Unknown 四类），意味着驱动对象一出生（`getDrivers()` 首次调用时，见 [devicedriver.cpp:24-25](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L24-L25)）就开始每秒发多播包，与是否使用 TCP 驱动无关。

**③ 接收与发送**：`ReceivedData`（[L219-257](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L219-L257)）与 USB 版（[librevnausbdriver.cpp:158-211](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L158-L211)）核心循环逐行相似：`QByteArray dataBuffer` 取代了 `USBInBuffer`，`dataSocket.readAll()` 追加、`dataBuffer.remove(0, handled_len)` 消费。`startNextTransmission`（[L351-376](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L351-L376)）同样先 `EncodePacket` 进 1024 字节栈缓冲、记包日志，只是最后一步换成 `dataSocket.write(...)`（[L368](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L368)）。发送队列、超时定时器、`transmissionFinished`（[L275-309](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L275-L309)）与 USB 版语义一致——TCP 是字节流，同样存在粘包/半包，同样需要一问一答排队。

**④ 两通道对照总表**（复习用）：

| 维度 | USB 驱动 | TCP 驱动 |
| --- | --- | --- |
| 驱动名 | `"LibreVNA/USB"`（[L36-39](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L36-L39)） | `"LibreVNA/TCP"`（[L59-62](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L59-L62)） |
| 发现机制 | libusb 枚举 VID/PID + 产品串 | SSDP 多播 + 过期表 |
| 数据/日志通道 | 端点 `0x81` / `0x82`（同一接口） | TCP `19544` / `19545`（两条连接） |
| 接收缓冲 | `USBInBuffer`（自管理，512 对齐） | `QByteArray`（Qt 自带） |
| 接收执行线程 | libusb 事件线程（DirectConnection） | Qt 主线程（readyRead） |
| 接收线程 `m_receiveThread` | 启动（`USBHandleThread`） | 声明但从不启动（仅构造置 null，[L22](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L22)） |
| 发送原语 | `libusb_bulk_transfer`（同步） | `QTcpSocket::write`（异步） |
| 连接失败处理 | 抛 `std::runtime_error` | 返回 false |
| 帧格式/发送队列/初始两问 | 完全相同（都在公共层或复用其逻辑） | 同左 |

#### 4.3.4 代码实践

**实践目标**：不依赖硬件，用抓包思维验证 SSDP 发现逻辑，并对两个 `ReceivedData` 做"找不同"。

**操作步骤**：

1. **找不同**：并排打开 [librevnausbdriver.cpp:182-201](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L182-L201) 与 [librevnatcpdriver.cpp:238-255](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L238-L255) 的 switch 语句，找出 case 分支集合的差异。
2. 追踪这个差异的后果：多出来的（或少掉的）`case PacketType::None` 会让 TCP 版对"无效帧"做什么？沿着 `receivedPacket` → [handleReceivedPacket, librevnadriver.cpp:696-814](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L696-L814) 走一遍，确认最终是否有可观察的行为差异（提示：注意 [L698](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L698) 的 `passOnReceivedPacket`）。
3. **SSDP 报文观察（可选）**：启动 GUI（可加 `--no-gui`，TCP 驱动对象在 `getDrivers()` 时就已构造并开始发 SSDP），另开终端执行 `tcpdump -ni any udp port 1900`（或 Wireshark 过滤 `ssdp`），观察每秒一个 `M-SEARCH` 与报文内容，与 [L159-167](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L159-L167) 拼的字符串逐行核对。

**需要观察的现象**：步骤 1-2 是纯代码推理；步骤 3 依赖本机网络环境与 tcpdump 权限，若无条件则**待本地验证**（即使局域网没有设备，也至少能看到自己发出的 M-SEARCH）。

**预期结果**：步骤 1 的答案见下面练习 1；步骤 3 应看到 `ST: urn:schemas-upnp-org:device:LibreVNA:1` 与代码常量一致。

#### 4.3.5 小练习与答案

**练习 1**：两个 `ReceivedData` 的 switch 有什么分支差异？后果是什么？

**答案**：USB 版有显式的 `case Protocol::PacketType::None: break;`（[librevnausbdriver.cpp:195-196](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L195-L196)），TCP 版没有。于是 TCP 版中 type 为 `None`（无效/半帧数据）的包落入 `default` 分支，也会 `emit receivedPacket(packet)`。追踪下去：`handleReceivedPacket` 开头无条件 `emit passOnReceivedPacket(packet)`（对 CompoundDriver 可见），随后 switch 无 `None` case、default 什么都不做——所以对 GUI 无可观察影响，只是 `passOnReceivedPacket` 多发了一个空包。这是一个阅读真实代码时很有价值的发现：两个"应该一样"的实现存在轻微不对称，且恰好无害。

**练习 2**：`connectTo` 里 `waitForConnected(1000)` 是阻塞调用，为什么 TCP 驱动敢在（可能的）GUI 线程里这么写？

**答案**：它只发生在用户明确点击"连接"的一瞬间，且最多阻塞 1 秒，属于可接受的一次性代价；换来的是同步、简单的错误处理（两个 socket 任一失败就统一走失败分支，[L100-106](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L100-L106)）。相比之下，常态化的数据接收没有用任何 `waitFor...`，全部走 `readyRead` 信号，不会卡界面。

**练习 3**：TCP 字节流同样会粘包/半包，`DecodeBuffer` 返回"已消费字节数 + type=None"的机制是如何配合 `QByteArray` 解决这个问题的？

**答案**：`ReceivedData` 每次把 `readAll()` 追加进 `dataBuffer`（[L221](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L221)），然后循环调 `DecodeBuffer`：能解出完整帧就返回帧长、`remove(0, handled_len)` 消费；帧不完整返回 0、循环结束，剩余字节留在 `dataBuffer` 里等下一次 `readyRead` 追加。`DecodeBuffer` 自己还会跳过帧头 `PCKT_HEADER_DATA` 之前的杂散字节（[Protocol.cpp:34-43](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.cpp#L34-L43)），从而具备失步自恢复能力。USB 路径的 `USBInBuffer` + `removeBytes` 是同一策略的 libusb 版本。

## 5. 综合实践

本讲的综合实践就是任务书里的核心作业：**画出两条通道下"一包原始字节 → `DeviceDriver::VNAMeasurement` → 发出信号"的调用序列图**。这是对 4.1–4.3 的总检验。

**实践目标**：不看讲义正文，仅凭源码独立完成两条链路，并给出每一步的函数名、文件、行号和执行线程。

**操作步骤**：

1. 准备一张三列的表：`步骤 | 函数（文件:行号） | 运行线程`。
2. 先做 USB 链路，从"libusb 事件线程里 transfer 完成"开始，到"`VNAmeasurementReceived` 信号被 VNA 模式的某个槽接收"结束（最后一步的接收者可在 [vna.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp) 中用 Grep 搜 `VNAmeasurementReceived` 确认，属于 u7-l1 的内容，这里只需找到连接语句即可）。
3. 再做 TCP 链路，起点换成"`dataSocket` 有可读数据"。
4. 标出两条链路中**线程发生切换的那一行 connect 语句**。
5. 用两种颜色区分"传输层私有代码"与"公共层代码"，直观看到两条链路在哪个函数汇合。

**参考答案（USB 链路）**：

| 步骤 | 函数（文件:行号） | 线程 |
| --- | --- | --- |
| 1 | `USBHandleThread` 循环：`libusb_handle_events` — [librevnausbdriver.cpp:279-286](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L279-L286) | libusb 事件线程 |
| 2 | `USBInBuffer::CallbackTrampoline` → `Callback`，累加 `received_size`，`emit DataReceived` — [usbinbuffer.cpp:100-104](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.cpp#L100-L104)、[L57-75](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.cpp#L57-L75) | libusb 事件线程 |
| 3 | `LibreVNAUSBDriver::ReceivedData`（DirectConnection）— [librevnausbdriver.cpp:158](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L158) | libusb 事件线程 |
| 4 | `Protocol::DecodeBuffer` 解出一帧 VNADatapoint — [librevnausbdriver.cpp:165](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L165)，实现在 [Protocol.cpp:28-93](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.cpp#L28-L93) | 同上 |
| 5 | `DevicePacketLog::addPacket` + `removeBytes` — [librevnausbdriver.cpp:168-175](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L168-L175) | 同上 |
| 6 | `emit receivedPacket(packet)`（default 分支）— [librevnausbdriver.cpp:197-200](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L197-L200)；连接建于 [L121](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L121)（QueuedConnection）★线程切换点 | 投递到 GUI 线程 |
| 7 | `LibreVNADriver::handleReceivedPacket`，VNADatapoint 分支 — [librevnadriver.cpp:696](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L696)、[L764-796](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L764-L796) | GUI 线程 |
| 8 | 双重循环按 `portStageMapping` 计算 `input/ref` 填 `m.measurements` — [L775-793](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L775-L793) | GUI 线程 |
| 9 | `emit VNAmeasurementReceived(m)` — [L795](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L795) | GUI 线程 |

**参考答案（TCP 链路）的差异点**（其余步骤 4-9 完全相同）：

| 步骤 | 函数（文件:行号） | 线程 |
| --- | --- | --- |
| 1' | 内核通知 `dataSocket` 可读，Qt 发射 `readyRead`（连接建于 [librevnatcpdriver.cpp:118](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L118)） | GUI 线程（无独立接收线程） |
| 2' | `LibreVNATCPDriver::ReceivedData`：`dataBuffer.append(dataSocket.readAll())` — [L219-221](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L219-L221) | GUI 线程 |
| 6' | `emit receivedPacket(packet)` — [L251-254](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L251-L254)；虽然也是 QueuedConnection（[L122](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L122)），但两端同在 GUI 线程，**无实际线程切换**（变为一次事件循环内的延迟调用） | GUI 线程 |

**需要观察的现象**：本实践为源码阅读型，产出物是你自己的序列图表；若想用真实数据验证，需连接设备并打开 "View Packet Log" 对照（**待本地验证**）。

**预期结果**：两条链路在 `emit receivedPacket` 处汇入同一个公共层函数 `handleReceivedPacket`；USB 链路有一次真实的线程切换（DirectConnection → QueuedConnection），TCP 链路全程在 GUI 线程。能独立推导出这两点，说明本讲目标达成。

## 6. 本讲小结

- LibreVNA 官方驱动是**两层结构**：`LibreVNADriver` 公共层持有全部协议语义（配置翻译 `setVNA/setSA/setSG`、收包解释 `handleReceivedPacket`），仅把 `SendPacket` 留成纯虚；USB/TCP 两个子类只负责"字节怎么进出"。
- 连接生命周期的固定剧本：发现 → 连接（claim 接口 / 双 TCP socket）→ 连接信号 → 主动发 `RequestDeviceInfo` + `RequestDeviceStatus` 两问；`DeviceInfo` 应答填充 `Info/Limits` 并触发协议版本协商。
- **接收方向**：原始字节经 `Protocol::DecodeBuffer` 拆帧（帧头同步、长度校验、CRC32、VNADatapoint 豁免 CRC），按类型分流：Ack/Nack 喂发送队列，其余经 `receivedPacket`（队列连接）进入 `handleReceivedPacket`，在那里 `input/ref` 相除得到 S 参数并 `emit VNAmeasurementReceived`。
- **USB 特有**：`USBInBuffer` 把"缓冲 + 循环提交的异步批量传输"封装成通用工具，`removeBytes` 只能在回调栈内调用；解析在 libusb 事件线程（DirectConnection），协议解释在 GUI 线程（QueuedConnection）。
- **TCP 特有**：SSDP 多播每秒 `M-SEARCH`、`max-age` 老化的 `detectedDevices` 表；数据/日志是 19544/19545 两条连接；帧格式与发送队列逻辑与 USB 完全一致——这就是公共层抽象的价值。
- 传输队列"一问一答、单包在途"的约束源于 Ack 不带序号；超时由 `transmissionTimer` 兜底。

## 7. 下一步学习建议

- **u3-l3（驱动生态）**：看第三方驱动（SSA3000X、SNA5000A）如何继承同一套 `DeviceDriver` 契约但走完全不同的协议，以及 `CompoundDriver` 如何利用本讲出现的 `passOnReceivedPacket` 信号把多台 LibreVNA 拼成多端口设备。
- **单元 4（通信协议）**：本讲只把 `DecodeBuffer`/`EncodePacket` 当黑盒用了。u4-l1 将进入固件侧 `Communication.cpp` 看这些包如何被分发处理，u4-l2 用仓库自带的 `USB_protocol_v12.tex`/`Device_protocol_v13.tex` 文档逐包核对字段。
- **动手方向**：若想加深理解，可尝试给 `ReceivedData` 的 `DecodeBuffer` 返回 0（半帧）与返回 `data-buf+1`（CRC 失败跳帧头）两种情况各写一段跟踪日志（仅阅读推演即可，不必真改代码），再对照 [Protocol.cpp:28-93](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.cpp#L28-L93) 验证你的推演。
