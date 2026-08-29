# LibreVNA 驱动：USB 与 TCP 两条通道

## 1. 本讲目标

上一讲（u3-l1）我们读完了 `DeviceDriver` 这个"抽象合同"。本讲进入官方设备的**真实实现**，学完后你应该能够：

1. 说清两层继承的分工：`LibreVNADriver` 负责"协议逻辑"（把设置翻译成协议包、把协议包翻译成测量数据），`LibreVNAUSBDriver` / `LibreVNATCPDriver` 只负责"搬运字节"。
2. 跟踪 USB 后端的完整路径：libusb 设备枚举 → 打开并 claim 接口 → 独立事件线程 → 异步接收 → 解析分发。
3. 跟踪 TCP 后端的完整路径：SSDP 组播发现 → 双 TCP 连接（数据/日志）→ `readyRead` 信号驱动接收。
4. 解释 `USBInBuffer` 这类接收缓冲工具解决什么问题（异步回调、粘包、部分帧）。
5. 独立画出「一包原始字节 → `DeviceDriver::VNAMeasurement` → 发出信号」的调用序列图，并标注每个函数所在的文件与行号。

## 2. 前置知识

### 2.1 libusb 的同步与异步传输

libusb 是跨平台的用户态 USB 库（不需要内核驱动）。本项目用到它的两种用法：

- **同步批量传输**（bulk transfer）：调用 `libusb_bulk_transfer()` 后阻塞直到完成。本项目用它**发送**命令包。
- **异步传输**（async transfer）：先 `libusb_alloc_transfer()` 分配、`libusb_fill_bulk_transfer()` 填参、`libusb_submit_transfer()` 提交，然后**立刻返回**；数据到达时 libusb 回调你注册的函数。本项目用它**接收**数据。
- 异步模式必须有人不断调用 `libusb_handle_events()` 来"泵"事件，否则回调永远不会触发——这就是 USB 驱动里那个独立接收线程存在的唯一原因。

### 2.2 USB 端点（endpoint）

USB 设备的每个方向的数据通道叫端点，用地址区分。本项目用了三个（定义在 [librevnausbdriver.h:48-50](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.h#L48-L50)）：

| 端点地址 | 方向 | 用途 |
|---|---|---|
| `0x01`（EP_Data_Out） | PC → 设备 | 发送控制/配置包 |
| `0x81`（EP_Data_In） | 设备 → PC | 接收测量与应答包 |
| `0x82`（EP_Log_In） | 设备 → PC | 接收设备日志（文本行） |

地址最高位为 1 表示 IN（设备→主机）。所以"数据"和"日志"走两条完全独立的流。

### 2.3 TCP、UDP 组播与 SSDP

- **TCP** 是面向连接的字节流协议：没有"消息边界"，一次 `readAll()` 可能读到半条消息或三条半消息——这与 USB 批量传输一样存在**粘包/半包**问题，所以 TCP 驱动也需要接收缓冲。
- **UDP 组播**：向组播地址（如 `239.255.255.250:1900`）发包，同一局域网内所有加入该组的设备都能收到。
- **SSDP**（Simple Service Discovery Protocol）是 UPnP 用的发现协议：客户端周期性向组播地址发 `M-SEARCH` 请求，设备单播回应答，答文里带 `LOCATION`、`ST` 等字段。LibreVNA 复用它来发现"通过 TCP 提供 LibreVNA 服务的设备"。

### 2.4 Qt 跨线程信号槽

- `Qt::DirectConnection`：槽函数在**发射信号的线程**里直接执行（像普通函数调用）。
- `Qt::QueuedConnection`：槽函数被投递到**接收者所属线程**的事件队列里排队执行（需要该线程有事件循环）。
- USB 接收回调发生在 libusb 的事件线程里，而 GUI 对象活在主线程——理解这两种连接方式的差别，是看懂本讲代码中 `connect(...)` 第五个参数的关键。

### 2.5 协议帧格式（回顾 + 补充）

GUI 与固件共用同一份 [Protocol.hpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp)。帧格式在 [Protocol.cpp:5-12](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.cpp#L5-L12) 的注释里写明：

```
1 字节帧头(0x5A) | 2 字节总长度 | 1 字节包类型 | 变长负载 | 4 字节 CRC32
```

各字段偏移由 [PacketConstants.h:10-25](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/PacketConstants.h#L10-L25) 定义。有一个值得注意的特例：`VNADatapoint` 包（测量数据）的 CRC 恒为 0，不做校验——测量数据量大，省掉逐包 CRC 换取吞吐（见 [Protocol.cpp:69-90](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.cpp#L69-L90)）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [Device/LibreVNA/librevnadriver.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.h) / [.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp) | **公共层**：实现 `DeviceDriver` 的全部语义接口（setVNA/setSA/setSG/...），做"设置⇄协议包"的双向翻译；不含任何 USB/TCP 代码 |
| [Device/LibreVNA/librevnausbdriver.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.h) / [.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp) | **USB 后端**：libusb 枚举/连接/收发线程 |
| [Device/LibreVNA/librevnatcpdriver.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.h) / [.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp) | **TCP 后端**：SSDP 发现 + 双 TCP 连接收发 |
| [Util/usbinbuffer.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.h) / [.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.cpp) | **USB 异步接收缓冲**：一个端点一个实例，负责提交异步传输、累积数据、通知上层 |
| [VNA_embedded/.../Protocol.hpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp) / [.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.cpp) | 帧编解码：`DecodeBuffer()` / `EncodePacket()`，GUI 与固件共同编译 |

两条传输后端都在 [devicedriver.cpp:19-32](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/devicedriver.cpp#L19-L32) 的 `getDrivers()` 里注册（u3-l1 讲过的懒加载单例），驱动名分别是 `"LibreVNA/USB"` 与 `"LibreVNA/TCP"`。

## 4. 核心概念与源码讲解

### 4.1 LibreVNADriver 公共层：不关心字节的协议引擎

#### 4.1.1 概念说明

回忆 u3-l1 的结论：驱动继承分两层。`LibreVNADriver` 是中间层，它实现了 `DeviceDriver` 的全部"语义"接口——`setVNA()`、`setSA()`、`setSG()`、`setIdle()`……但它**不知道设备在哪**：

- 它把 `DeviceDriver::VNASettings` 这样的高层结构翻译成 `Protocol::PacketInfo` 协议包；
- 它把收到的协议包翻译成 `DeviceDriver::VNAMeasurement` 并发出信号；
- 它把"怎么把包送出去"声明为**纯虚函数** `SendPacket()`，交给传输后端实现。

这就是一个教科书式的**策略模式/模板方法**：协议逻辑写一遍，传输通道插两次。

#### 4.1.2 核心流程

**发送方向**（上层 → 设备）：

```text
模式层调用 setVNA(settings)
    ├─ 检查 supports(Feature::VNA)
    ├─ 生成 portStageMapping（端口→激励阶段映射）
    ├─ 填一个 Protocol::PacketInfo（type = SweepSettings）
    └─ SendPacket(p, 回调)          ← 纯虚，落到 USB/TCP 子类
         └─ 子类：入队 → 编码 → 写端点/套接字 → 等Ack → 触发回调
```

**接收方向**（设备 → 上层），子类解析出完整帧后发出 `receivedPacket` 信号（队列化连接），公共层在 `handleReceivedPacket()` 中按包类型分发：

```text
handleReceivedPacket(packet)
    ├─ DeviceInfo            → 填 info / 校验协议版本 → emit InfoUpdated()
    ├─ DeviceStatus          → 存 lastStatus → emit StatusUpdated()/FlagsUpdated()
    ├─ VNADatapoint          → 组装 VNAMeasurement → emit VNAmeasurementReceived(m)
    └─ SpectrumAnalyzerResult→ 组装 SAMeasurement → emit SAmeasurementReceived(m)
```

注意 `skipOwnPacketHandling` 标志：置位时公共层只把包通过 `passOnReceivedPacket` 信号转发（给 CompoundDriver 用），自己不做上述翻译——这是组合驱动"截胡"原始包的钩子（[librevnadriver.cpp:696-702](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L696-L702)）。

#### 4.1.3 源码精读

**① 纯虚的传输接口与信号契约**。公共层给子类规定了两件事：必须实现 `SendPacket`，必须遵守应答信号。

[librevnadriver.h:176-197](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.h#L176-L197)：`passOnReceivedPacket`（给复合驱动）、纯虚 `SendPacket`、`receivedAnswer`/`receivedPacket`/`receivedTrigger` 三个信号，以及 `handleReceivedPacket` 槽。`TransmissionResult` 枚举（Ack/Nack/Timeout/InternalError）就是子类回报发送结果的统一语言。

**② setVNA：高层设置 → 协议包的翻译现场**。

[librevnadriver.cpp:490-516](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L490-L516)（节选）：

```cpp
// create port->stage mapping
portStageMapping.clear();
for(unsigned int i=0;i<s.excitedPorts.size();i++) {
    portStageMapping[s.excitedPorts[i]] = i;
}

Protocol::PacketInfo p = {};
p.type = Protocol::PacketType::SweepSettings;
p.settings.f_start = s.freqStart;
p.settings.points = s.points;
p.settings.if_bandwidth = s.IFBW;
p.settings.cdbm_excitation_start = s.dBmStart * 100;   // dBm → 1/100 dBm
...
zerospan = (s.freqStart == s.freqStop) && (s.dBmStart == s.dBmStop);
```

两个细节值得停下来看：

- **单位换算**：GUI 内部用 dBm（浮点），协议用 `cdbm`（1/100 dBm 的整数），所以乘 100。定点整数传输是嵌入式协议的常见选择，避免两端浮点格式差异。
- **portStageMapping**：多端口测量是"分时"进行的——第 0 阶段激励端口 1、第 1 阶段激励端口 2……这个 map 记住"哪个端口在第几阶段被激励"，接收方向翻译数据时要反过来查它。
- **zerospan**：起止频率相同即为零扫宽（点频）模式，此时 X 轴不再是频率而是时间——这个布尔值在接收方向决定取 `frequency` 还是 `us` 字段。

随后 [librevnadriver.cpp:527-531](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L527-L531) 把包交给传输层，并把"发送成功"折叠成回调布尔值（Ack 才算成功）。

**③ handleReceivedPacket 的 VNADatapoint 分支——本讲最重要的一段**。

[librevnadriver.cpp:764-796](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L764-L796)（节选）：

```cpp
case Protocol::PacketType::VNADatapoint: {
    VNAMeasurement m;
    Protocol::VNADatapoint<32> *res = packet.VNAdatapoint;
    m.pointNum = res->pointNum;
    m.Z0 = 50.0;
    if(zerospan) {
        m.us = res->us;
    } else {
        m.frequency = res->frequency;
        m.dBm = (double) res->cdBm / 100;
    }
    for(auto map : portStageMapping) {
        complex<double> ref = res->getValue(map.second, map.first-1, true);
        for(unsigned int i=1;i<=info.Limits.VNA.ports;i++) {
            complex<double> input = res->getValue(map.second, i-1, false);
            if(!std::isnan(ref.real()) && !std::isnan(input.real())) {
                QString name = "S"+QString::number(i)+QString::number(map.first);
                m.measurements[name] = input / ref;
            }
            ...
        }
    }
    delete res;
    emit VNAmeasurementReceived(m);
}
```

这段代码在做的事，用射频语言说就是：**S 参数是反射/传输波与入射波的比值**。设备端测得的 `input`（某端口的接收波）除以 `ref`（参考通道的入射波）就是 \( S_{ij} = \dfrac{b_i}{a_j} \)：

- 外层循环遍历"哪个端口在被激励"（`map.first` 是激励端口，决定 S 参数的第二个下标 j）；
- 内层循环遍历"在哪个端口接收"（`i` 是第一个下标）；
- `res->getValue(stage, port, reference)` 在最多 32 组 (实部, 虚部, 描述字节) 里按 stage 与源掩码找到匹配的复数，找不到返回 NaN——所以 `isnan` 检查是"这组数据里确实有这个测量"的判断；
- `packet.VNAdatapoint` 是 `DecodeBuffer` 在堆上 `new` 出来的（见 4.2.3 ④），因此这里必须 `delete res`，内存管理责任在注释里有言在先（[Protocol.hpp:630-631](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L630-L631)）。

`getValue` 本体在 [Protocol.hpp:81-97](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L81-L97)：描述字节的低 5 位是"源掩码"（Port1..Port4 + Reference 位，见 [Protocol.hpp:17-23](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L17-L23)），高位是阶段号；线性扫描匹配即返回。

**④ DeviceInfo 分支：能力协商的入口**。[librevnadriver.cpp:705-757](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L705-L757)：先比对协议版本（不一致就弹窗建议升级固件），再按 `hardware_version` 分支填 `info.supportedFeatures` 与各模式 Limits，最后 `emit InfoUpdated()`。u3-l1 讲过的"能力协商"，数据源头就在这里。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：亲手推演"单端口 vs 双端口激励"下，一条 `VNADatapoint` 会变成哪些 S 参数。
2. **操作步骤**：
   - 假设 `info.Limits.VNA.ports = 2`；
   - 场景 A：`s.excitedPorts = {1}`。读 [librevnadriver.cpp:491-494](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L491-L494)，写出 `portStageMapping` 的内容（应为 `{1→0}`）；
   - 场景 B：`s.excitedPorts = {2, 1}`，写出 `portStageMapping`（应为 `{2→0, 1→1}`）；
   - 对两个场景分别代入 [librevnadriver.cpp:775-785](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L775-L785) 的双重循环，列出 `m.measurements` 里最终出现的键名。
3. **需要观察的现象**：场景 A 只产生 S11、S21 两个键；场景 B 产生 S12、S11、S22、S21 四个键（`std::map` 按端口序遍历，顺序为 S12/S11 → S22/S21）。
4. **预期结果**：理解"S 参数的第二个下标由激励端口决定、第一个下标由接收端口决定"，以及为什么**只激励一个端口时设备永远回不出 S12/S22**。
5. 本实践为纯代码推演，无需硬件，结论可直接从代码逻辑推出；若想验证，可在有设备时用 GUI 的 Manual Control（仅激励一个端口）观察可用 Trace 列表（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `LibreVNADriver` 不自己实现 `SendPacket`，而要声明成纯虚？
**答案**：`SendPacket` 的本质是"把编码后的字节送到设备"，这与传输介质强相关（USB 端点 or TCP 套接字）。声明为纯虚（[librevnadriver.h:180](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.h#L180)）强制每个后端自己实现搬运，而协议内容（包类型、字段）完全复用公共层，符合"逻辑与传输分离"。

**练习 2**：`setSA()` 里 `SApoints` 是怎么算的？为什么要存下这个数？
**答案**：[librevnadriver.cpp:556-563](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L556-L563)：频宽 ≥ 1001 或 ≤ 0 时取上限 1001 点，否则逐 Hz 一点。`getSApoints()` 供上层（如频谱仪模式）知道一个扫描周期有多少点，用于判断"一次扫描是否完成"。

**练习 3**：`lastNonIdlePacket` 成员是做什么用的？
**答案**：`setVNA/setSA/setSG` 都会把刚发的包存进它（如 [librevnadriver.cpp:524-525](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L524-L525)）。`setExtRef()` 在测量进行中切换基准源时，需要先停机（SetIdle）、切源、再重发"上一次的非空闲配置"（[librevnadriver.cpp:667-678](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L667-L678)）——因为带频率校准的内部源切换会导致瞬时不准确的频率。

### 4.2 USB 驱动：libusb 枚举、连接与收发线程

#### 4.2.1 概念说明

`LibreVNAUSBDriver` 回答三个问题：**设备在哪**（枚举）、**怎么独占它**（打开 + claim 接口）、**字节怎么进出**（一个事件线程 + 两个接收缓冲 + 一个发送队列）。

USB 的发现不靠广播，靠**总线枚举**：libusb 列出系统里所有 USB 设备，逐一读描述符，用 VID/PID + 产品字符串过滤出"自己的"设备，序列号作为唯一身份。

#### 4.2.2 核心流程

```text
【发现】GetAvailableDevices()
    libusb_init → SearchDevices(收集所有序列号) → libusb_exit

【连接】connectTo(serial)
    SearchDevices(匹配序列号后中止搜索并保留句柄)
    → libusb_claim_interface(0)          独占接口
    → 启动 USBHandleThread（泵 libusb 事件）
    → 创建 dataBuffer/logBuffer（提交首批异步接收）
    → 连接信号、发 RequestDeviceInfo + RequestDeviceStatus

【接收】libusb 事件线程
    libusb_handle_events → USBInBuffer::Callback → emit DataReceived(DirectConnection)
    → ReceivedData() 在事件线程中解码循环 → emit receivedPacket(QueuedConnection)
    → 主线程 handleReceivedPacket → emit VNAmeasurementReceived

【发送】SendPacket()：入队 → startNextTransmission()
    EncodePacket → libusb_bulk_transfer(同步发送) → 启动超时定时器
    收到 Ack/Nack 或超时 → transmissionFinished → 出队 → 发下一个
```

#### 4.2.3 源码精读

**① 认设备的"白名单"**。[librevnausbdriver.cpp:23-25](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L23-L25)：

```cpp
validUSBIDs.append({0x0483, 0x564e, "VNA"});   // ST 官方 VID + 早期 PID
validUSBIDs.append({0x0483, 0x4121, "VNA"});   // ST VID + 新 PID
validUSBIDs.append({0x1209, 0x4121, "VNA"});   // pid.codes 开源 VID
```

`0x0483` 是 STMicroelectronics 的厂商 ID，`0x1209` 是开源硬件常用的 pid.codes。注意第三个字段是**产品字符串**，后续还要与设备的 iProduct 描述符比对才算命中。

**② SearchDevices：三重过滤的枚举器**。[librevnausbdriver.cpp:288-359](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L288-L359)：

- [293-316](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L293-L316)：遍历设备列表，比对 VID/PID；
- [320-334](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L320-L334)：`libusb_open` 逐个试开；失败时若 `ignoreOpenError` 为 false，提示用户最典型的两个原因——**Linux 缺 udev 规则**、**Windows 已被别的实例占用**；
- [336-351](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L336-L351)：读字符串描述符，产品字符串与白名单一致才调用 `foundCallback`；回调返回 false 表示"就是它，别再搜了"，此时**不关闭句柄**（调用方接手），其余情况关闭句柄继续。

`GetAvailableDevices()`（[41-59](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L41-L59)）用一个"只收集序列号"的 lambda 调它；`connectTo()` 用一个"匹配即中止"的 lambda 调它——同一个枚举器服务两种用法，这是回调式 API 的漂亮用法。

**③ connectTo：连接生命周期**。[librevnausbdriver.cpp:61-129](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L61-L129)，关键四拍：

```cpp
// 1. 独占 USB 接口（失败则抛异常）
int ret = libusb_claim_interface(m_handle, 0);              // L99
// 2. 起事件线程（没有它异步回调不会触发）
m_receiveThread = new std::thread(&LibreVNAUSBDriver::USBHandleThread, this);  // L113
// 3. 每个接收端点一个缓冲，构造时即提交第一批异步传输
dataBuffer = new USBInBuffer(m_handle, EP_Data_In_Addr, 65536);   // L114
logBuffer  = new USBInBuffer(m_handle, EP_Log_In_Addr, 65536);    // L115
// 4. 握手：主动询问设备信息与状态
sendWithoutPayload(Protocol::PacketType::RequestDeviceInfo);      // L125
sendWithoutPayload(Protocol::PacketType::RequestDeviceStatus);    // L126
```

信号连接的线程语义（[116-121](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L116-L121)）：`DataReceived → ReceivedData` 用 **DirectConnection**（解码就发生在 libusb 事件线程里，避免数据竞争——缓冲区只有在这个回调里才允许被消费）；`receivedPacket → handleReceivedPacket` 用 **QueuedConnection**（把翻译工作搬回主线程，GUI 对象线程安全）。`TransferError → ConnectionLost` 则是拔线时通知上层断连。

`disconnect()`（[131-156](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L131-L156)）按相反顺序拆除：先 setIdle、删缓冲（析构会取消未完成的传输）、释放接口、关句柄、join 事件线程、销毁 context。

**④ ReceivedData：解码循环**。[librevnausbdriver.cpp:158-211](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L158-L211)（节选）：

```cpp
do {
    handled_len = Protocol::DecodeBuffer(dataBuffer->getBuffer(),
                                         dataBuffer->getReceived(), &packet);
    if(handled_len > 0) {
        auto &log = DevicePacketLog::getInstance();
        if(packet.type != Protocol::PacketType::None) {
            log.addPacket(packet, serial);       // 写入包日志（调试用）
        } else {
            log.addInvalidBytes(...);            // 无效字节也记录
        }
    }
    dataBuffer->removeBytes(handled_len);        // 从缓冲中移除已消费字节
    switch(packet.type) {
    case Protocol::PacketType::Ack:  emit receivedAnswer(TransmissionResult::Ack);  break;
    case Protocol::PacketType::Nack: emit receivedAnswer(TransmissionResult::Nack); break;
    case Protocol::PacketType::SetTrigger:   emit receivedTrigger(this, true);  break;
    case Protocol::PacketType::ClearTrigger: emit receivedTrigger(this, false); break;
    default: emit receivedPacket(packet);       // 其余交给公共层翻译
    }
} while (handled_len > 0);
```

`DecodeBuffer`（[Protocol.cpp:28-93](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.cpp#L28-L93)）每次调用**最多解析一帧**，返回本次消费的字节数（0 表示数据不够、等下次；正数表示消费了多少）。所以外面套 `do...while` 把缓冲里挤着的所有帧都榨干——这就是 USB/TCP 两条通道共用的**粘包处理范式**：`decode → remove → loop`。

Ack/Nack/Trigger 三类包被 USB 层"就地消化"（它们关系到发送队列的状态机，属于传输层私事），其余类型统一转交公共层。

**⑤ 发送队列：单飞 + 超时**。`SendPacket`（[264-277](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L264-L277)）只入队并在空闲时启动发送；真正干活的是 `startNextTransmission`（[361-388](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L361-L388)）：`EncodePacket` 编码（[371](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L371)）→ 同步 `libusb_bulk_transfer` 发到 `0x01` 端点（[379](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L379)）→ 启动单次超时定时器（[385](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L385)）。

任何时刻**最多只有一个包在等待应答**（单飞，stop-and-wait）：收到 Ack/Nack（`transmissionFinished`，[228-262](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L228-L262)）或定时器到点（`transmissionTimeout`，[43-45](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L43-L45)）才出队发下一个。这样应答与请求的对应关系永远明确，不需要序列号。

**⑥ 事件线程**。[librevnausbdriver.cpp:279-286](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L279-L286)：`while(connected) libusb_handle_events(m_context);`——全部异步传输的回调都由这个线程驱动，包括接收完成与取消。

#### 4.2.4 代码实践

1. **实践目标**：验证 USB 枚举的三重过滤逻辑，并理解 udev 权限问题（Linux 用户最常见的"连不上"原因）。
2. **操作步骤**：
   - 有硬件：运行 `lsusb`，在输出中找 VID:PID 为 `0483:564e`、`0483:4121` 或 `1209:4121` 的行；
   - 再运行 `lsusb -d 0483:4121 -v 2>/dev/null | grep -i -A1 "iProduct\|iSerial"`（按实际 VID:PID 替换），确认产品字符串确实是 `VNA`，并记下序列号；
   - 无硬件：通读 [librevnausbdriver.cpp:288-359](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L288-L359)，在纸上回答：如果一台设备 VID/PID 命中但产品字符串是别的，会发生什么？（答案：被跳过，且不报错。）
3. **需要观察的现象**：`lsusb` 能看到设备 ≠ GUI 能打开设备；Linux 下若未安装 udev 规则，`libusb_open` 会失败，GUI 弹出的正是 [librevnausbdriver.cpp:324-331](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L324-L331) 那条提示。
4. **预期结果**：能说出"VID/PID → 产品字符串 → 序列号"三层各自排除了什么干扰。
5. 无硬件时本实践为 `lsusb` 观察其他 USB 设备 + 代码走读；`lsusb -v` 的输出因系统而异（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `connectTo` 里要先起 `USBHandleThread` 再 `new USBInBuffer`？
**答案**：`USBInBuffer` 构造函数最后一步就是 `libusb_submit_transfer`（[usbinbuffer.cpp:19](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.cpp#L19)）。若事件线程尚未运行，传输虽然提交成功，但完成回调无人泵送，数据永远到不了。先起线程保证第一批传输立刻有人处理。

**练习 2**：`transmissionFinished` 开头为什么检查队列空并打印 "stray Ack?"？
**答案**：[librevnausbdriver.cpp:232-235](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L232-L235)。Ack 没有携带"我在应答哪个包"的信息（单飞协议不需要），如果队列已空却来了 Ack，说明状态失步（比如超时后设备才回 Ack），此时直接丢弃并留警告，避免把下一个包的应答错配。

**练习 3**：日志端点（`ReceivedLog`，[213-226](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L213-L226)）为什么不用 `Protocol::DecodeBuffer` 而用 `memchr` 找 `\n`？
**答案**：数据端点走的是二进制协议帧（0x5A 帧头 + CRC），日志端点走的是**文本行协议**——固件直接 printf 风格输出，一行一条。按 `\n` 切分即可，`emit LogLineReceived(line)` 把每行交给上层显示。同一条 USB 连接上并行两种应用层协议，靠端点隔离。

### 4.3 USBInBuffer：一个端点一个接收缓冲

#### 4.3.1 概念说明

`USBInBuffer` 是对"**一个接收端点的异步接收循环**"的封装。它要解决三件事：

1. **持续接收**：USB 异步传输是一次性的，完成后必须重新提交才能收下一批——它把这个"提交-完成-再提交"循环自动化；
2. **积累与消费**：一次传输可能只到半帧、也可能好几帧挤在一起——它把字节攒在内部缓冲里，等上层来解析；
3. **线程纪律**：数据只在 libusb 回调线程里被追加/消费，它用 `inCallback` 标志把这一约定**强制**下来。

#### 4.3.2 核心流程

```text
构造: 分配 buffer → alloc_transfer → fill_bulk_transfer(填好端点/回调) → submit
      ↓ (数据到达, 由事件线程的 handle_events 驱动)
Callback(transfer):
    status == COMPLETED 且 actual_length > 0
        → received_size += actual_length   追加到已有数据之后
        → inCallback = true; emit DataReceived(); inCallback = false
    重新填 buffer 指针/长度(总长减去未消费部分, 按 512 向下取整) → resubmit
上层(ReceivedData): 循环 DecodeBuffer → removeBytes(handled)
    removeBytes: 仅允许在回调内调用(否则抛异常);
                未消费数据 memmove 到缓冲区头部
```

#### 4.3.3 源码精读

**① 构造即提交**。[usbinbuffer.cpp:9-20](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.cpp#L9-L20)：

```cpp
USBInBuffer::USBInBuffer(libusb_device_handle *handle, unsigned char endpoint, int buffer_size)
    : ...
{
    buffer = new unsigned char[buffer_size];
    transfer = libusb_alloc_transfer(0);
    libusb_fill_bulk_transfer(transfer, handle, endpoint, buffer, buffer_size,
                              CallbackTrampoline, this, 0);
    libusb_submit_transfer(transfer);
}
```

`CallbackTrampoline`（[100-104](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.cpp#L100-L104)）是静态函数——libusb 的 C 回调不能直接是成员函数，于是用 `user_data` 存 `this` 再转发，这是 C 库集成的标准手法（"蹦床函数"）。

**② 回调：追加、通知、再提交**。[usbinbuffer.cpp:57-98](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.cpp#L57-L98)，核心三段：

```cpp
case LIBUSB_TRANSFER_COMPLETED:
case LIBUSB_TRANSFER_TIMED_OUT:
    if(transfer->actual_length > 0) {
        received_size += transfer->actual_length;   // L70 追加(不覆盖未消费数据)
        inCallback = true;
        emit DataReceived();                        // L72 DirectConnection→上层解码
        inCallback = false;
    }
    break;
...
// Resubmit the transfer                          // L93
transfer->buffer = &buffer[received_size];         // L94 从未消费数据之后继续放
transfer->length = buffer_size - received_size;    // L95 剩余空间
transfer->length = (transfer->length / 512) * 512; // L96 按 512 取整
libusb_submit_transfer(transfer);                  // L97
```

两个设计点：

- **追加而非覆盖**（L70 + L94）：上层一次没消费完的"半帧"留在缓冲前部，新数据接在后面——这就是"积累半帧等下次拼齐"的实现。
- **错误即终止**（[76-88](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.cpp#L76-L88)）：`NO_DEVICE`（拔线）或 `ERROR/OVERFLOW/STALL` 时释放传输、发 `TransferError`，**不再重新提交**；只有正常完成（以及取消，见下）才继续。取消分支（[59-65](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.cpp#L59-L65)）与析构函数配合：析构时 `libusb_cancel_transfer` 并用条件变量最多等 100ms 让回调走完取消分支，避免析构后回调还摸已删除的对象。

**③ removeBytes：把"只能回调内消费"写成硬约束**。[usbinbuffer.cpp:38-50](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.cpp#L38-L50)：

```cpp
void USBInBuffer::removeBytes(int handled_bytes) {
    if(!inCallback) {
        throw runtime_error("Removing of bytes is only allowed from within receive callback");
    }
    if(handled_bytes >= received_size) {
        received_size = 0;
    } else {
        memmove(buffer, &buffer[handled_bytes], received_size - handled_bytes);
        received_size -= handled_bytes;
    }
}
```

因为回调刚返回就要重填 `transfer->buffer` 指针，若消费发生在别的线程、与重提交并发，指针和长度就会失配。所以用 `inCallback` 标志 + 异常把调用时机钉死在回调内——配合驱动里 `DataReceived → ReceivedData` 的 DirectConnection，整套接收路径单线程化，不需要额外锁。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：搞清楚三个"魔法行为"的动机——为什么按 512 取整？为什么 `received_size` 可能大于 0 时还能继续提交？为什么 `removeBytes` 要抛异常？
2. **操作步骤**：
   - 通读 [usbinbuffer.cpp:57-98](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.cpp#L57-L98)，画出一次"收到 700 字节、上层只消费 300 字节"后，`received_size`、`transfer->buffer`、`transfer->length` 各是多少；
   - 思考：若 L96 不按 512 取整、缓冲剩余空间是 300 字节，会发生什么？（提示：批量传输的完成条件是"填满请求长度 **或** 到一个短包"；请求太小时设备一个最大包都装不下，会产生零长或异常完成。）
3. **需要观察的现象**：纸面推演 `received_size = 400`（700−300），`transfer->buffer = &buffer[400]`，`transfer->length = (65536−400)/512*512 = 65024`。
4. **预期结果**：能说出"取整是为了让每次请求都留足整数个最大包的空间；抛异常是把跨线程误用变成显式崩溃而不是数据损坏"。L96 的具体取值（512）与设备端最大包长的精确对应关系代码未注释说明（待确认，可对照 USB 协议文档 v12 的端点描述）。
5. 本实践为纯推演，无需硬件。

#### 4.3.5 小练习与答案

**练习 1**：`USBInBuffer` 与 `ReceivedData` 之间为什么必须用 `Qt::DirectConnection`？
**答案**：`removeBytes` 只允许在回调内调用（抛异常强制），而"回调内"就是发射 `DataReceived` 的那个 libusb 事件线程。DirectConnection 让 `ReceivedData` 在同一线程同步执行，消费动作天然处于 `inCallback = true` 的区间内；若换成队列化连接，消费发生在主线程，直接触发异常。

**练习 2**：析构函数里那个条件变量等待（[22-36](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Util/usbinbuffer.cpp#L22-L36)）防的是什么竞态？
**答案**：`libusb_cancel_transfer` 是异步的——真正的取消完成发生在事件线程的回调里。若不等待就 `delete[] buffer`，事件线程可能还在执行 `Callback` 并访问已释放的缓冲。条件变量等回调走到取消分支（`cv.notify_all`）后再删，最多等 100ms 超时并警告。

**练习 3**：一条 16 字节的半帧还差 8 字节才完整时，`DecodeBuffer` 返回什么？缓冲会怎样？
**答案**：返回 0（数据不足时不消费，见 [Protocol.cpp:60-64](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.cpp#L60-L64)），`removeBytes(0)` 不改变缓冲，`do...while` 因 `handled_len == 0` 退出；半帧留在缓冲里等下一次数据到达拼接。

### 4.4 TCP 驱动：SSDP 发现与双 socket 通道

#### 4.4.1 概念说明

`LibreVNATCPDriver` 面向的是"设备不在 USB 总线上、而在网络里"的场景（例如设备接在一台小主机上再通过网络共享）。它要回答与 USB 版同样的三个问题，但答案完全不同：

- **设备在哪**：USB 能枚举总线，TCP 没有总线——于是借用 SSDP 组播做发现；
- **怎么独占**：没有 claim 接口，就是向设备的两个固定端口各建一条 TCP 连接；
- **字节怎么进出**：不需要 libusb 事件线程，`QTcpSocket` 的 `readyRead` 信号天然跑在 Qt 事件循环上。

SSDP 发现条目带有 `max-age`（存活期），过期未刷新就从列表剔除——网络设备可能随时下线，发现列表必须"会腐烂"。

#### 4.4.2 核心流程

```text
【后台发现（构造时启动，永不停止）】
构造函数: 对每个以太网/WiFi/虚拟网卡建一个 UDP socket 并加入 239.255.255.250 组播组
ssdpTimer(1000ms) → SSDRequest(): 向组播地址发 M-SEARCH（ST=urn:schemas-upnp-org:device:LibreVNA:1）
                          ↓ 设备单播回应
SSDPreceived(): 解析 LOCATION/ST/LibreVNA-serial/CACHE-CONTROL
              → addDetectedDevice()（按序列号去重更新）
GetAvailableDevices(): 返回 detectedDevices 的序列号集合
pruneDetectedDevices(): 每次发 M-SEARCH 前剔除超龄条目

【连接】connectTo(serial):
在 detectedDevices 中找到地址 → 连 DataPort(19544)/LogPort(19545) 两条 TCP
→ waitForConnected(1000) → 绑 readyRead → 发 RequestDeviceInfo/Status

【接收】dataSocket.readyRead → ReceivedData(): readAll 追加到 QByteArray
        → 同样的 DecodeBuffer/remove 循环 → 同样的信号分发
```

#### 4.4.3 源码精读

**① 协议常量**。[librevnatcpdriver.cpp:12-16](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L12-L16)：

```cpp
static const QString service_name = "urn:schemas-upnp-org:device:LibreVNA:1";
static constexpr int DataPort = 19544;   // 测量/控制数据
static constexpr int LogPort  = 19545;   // 日志
static auto SSDPaddress = QHostAddress("239.255.255.250");
static constexpr int SSDPport = 1900;    // SSDP 标准端口
```

数据与日志用**两条 TCP 连接**——和 USB 用两个端点完全同构：二进制协议帧与文本日志各行其道，互不干扰。

**② 构造函数：为每块网卡建一个组播 socket**。[librevnatcpdriver.cpp:25-48](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L25-L48)：遍历所有网络接口，只保留以太网/WiFi/虚拟/未知类型，每个接口 `bind` 一个 UDP socket、设置组播出口接口、加入组播组；然后 `ssdpTimer` 每 1000ms 触发一次 `SSDRequest`。注意这些发生在**驱动构造时**而非连接时——发现是常驻后台行为（驱动在 `getDrivers()` 注册时就被构造了）。

**③ M-SEARCH 与应答解析**。`SSDRequest`（[157-174](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L157-L174)）拼出标准 SSDP 报文并从每个组播 socket 发出；`SSDPreceived`（[176-217](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L176-L217)）逐行解析：

```cpp
if(lines[0] != "HTTP/1.1 200 OK") { continue; }        // 只认 SSDP 应答
for(QString l : lines) {
    if(l.startsWith("LOCATION:"))            { location = l.split(" ")[1]; }
    else if(l.startsWith("ST:"))             { st = l.split(" ")[1]; }
    else if(l.startsWith("LibreVNA-serial:")){ serial = l.split(" ")[1]; }  // 私有扩展字段
    else if(l.startsWith("CACHE-CONTROL:"))  { max_age = l.split("=")[1]; }
}
```

`LibreVNA-serial:` 是本项目对 SSDP 的私有扩展头。`addDetectedDevice`（[326-337](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L326-L337)）按序列号去重（已存在则覆盖刷新），`pruneDetectedDevices`（[339-349](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L339-L349)）剔除 `responseTime` 距今超过 `maxAgeSeconds` 的条目——设备下线后最多 `max-age` 秒就从设备列表消失。

**④ connectTo：与 USB 版逐行对应**。[librevnatcpdriver.cpp:74-130](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L74-L130)：

```cpp
dataSocket.connectToHost(devInfo.address, DataPort);   // L96  相当于 libusb_open
logSocket.connectToHost(devInfo.address, LogPort);     // L97
if(!dataSocket.waitForConnected(1000) || !logSocket.waitForConnected(1000)) { ... }  // L100
...
connect(&dataSocket, &QTcpSocket::readyRead, this, &LibreVNATCPDriver::ReceivedData, ...);  // L118
sendWithoutPayload(Protocol::PacketType::RequestDeviceInfo);   // L126 与 USB 版相同的握手
```

错误处理差异：TCP 版连接失败**返回 false**，USB 版**抛异常**（[librevnausbdriver.cpp:94](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L94)）——上层 `connectDevice` 需要同时容忍两种风格。断连检测也不一样：TCP 用 `errorOccurred → ConnectionLost`（[111-112](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L111-L112)），USB 用 `USBInBuffer::TransferError → ConnectionLost`。

**⑤ ReceivedData：与 USB 版只差两行**。[librevnatcpdriver.cpp:219-257](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L219-L257)（对比 4.2.3 ④）：

```cpp
dataBuffer.append(dataSocket.readAll());                 // L221 代替 USBInBuffer 的异步回调
handled_len = Protocol::DecodeBuffer((uint8_t*) dataBuffer.data(), dataBuffer.size(), &packet);  // L227
...
dataBuffer.remove(0, handled_len);                       // L237 QByteArray 版 removeBytes
```

解码循环、包日志、`switch` 分发**逐行相同**（唯一差别是 TCP 版的 switch 少了 `None` 分支，效果一样：`None` 落入 default 会被 `emit receivedPacket(packet)` 转发，公共层对 `None` 不做任何事）。缓冲从手工管理的 `USBInBuffer` 换成了 `QByteArray` 的 `append`/`remove`——Qt 替你做了 memmove。`ReceivedLog`（[259-273](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L259-L273)）同样与 USB 版逐行对应。

**⑥ 发送队列：整段复制**。`SendPacket`（[311-324](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L311-L324)）、`transmissionFinished`（[275-309](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L275-L309)）、`startNextTransmission`（[351-376](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L351-L376)）与 USB 版几乎逐字相同，唯一的实质差异在 `startNextTransmission` 里：

```cpp
auto ret = dataSocket.write((char*) buffer, length);   // L368 代替 libusb_bulk_transfer
```

还有一个耐人寻味的细节：TCP 头文件里声明了 `std::thread *m_receiveThread`（[librevnatcpdriver.h:108](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.h#L108)），但 `.cpp` 中只在构造函数置过一次 `nullptr`、从未启动——QTcpSocket 的事件驱动模型根本不需要它，这是个从 USB 版"遗传"下来的闲置成员。

#### 4.4.4 代码实践

1. **实践目标**：看懂一条完整的 SSDP M-SEARCH 报文，并验证 `SSDPreceived` 能正确解析一个手工构造的应答。
2. **操作步骤**：
   - 读 [librevnatcpdriver.cpp:157-174](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L157-L174)，把 M-SEARCH 报文完整抄写出来（就是那个字符串字面量加 `service_name`）；
   - 有 LibreVNA 网络环境：运行 `tcpdump -i any -n port 1900 -A` 观察 1 秒一次的 M-SEARCH 与设备应答；
   - 无设备：在纸上构造一条合法应答并逐行代入 [librevnatcpdriver.cpp:190-215](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L190-L215) 的解析循环，写出 `location/st/serial/max_age` 四个变量的终值；
   - 思考：应答中若缺 `CACHE-CONTROL` 行，解析结果如何？（提示：`max_age` 初值是 `"2"`，即 [188 行](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L188)的默认值，条目 2 秒后被剔除。）
3. **需要观察的现象**：M-SEARCH 每 1 秒重发；应答里的 `LibreVNA-serial:` 头携带序列号；停止设备端服务后条目在 max-age 秒内消失。
4. **预期结果**：能默写 M-SEARCH 的四个必填头（HOST/MAN/MX/ST）并说明 `ST` 的值如何把 LibreVNA 的应答与其他 UPnP 设备区分开。
5. 无网络设备时为纸面推演；tcpdump 观察需本地环境（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 TCP 驱动不需要 `USBHandleThread` 那样的接收线程？
**答案**：`QTcpSocket` 是 `QObject`，数据到达时在**其所属线程的事件循环**里发 `readyRead` 信号——驱动对象活在主线程，主线程的 Qt 事件循环天然就是"泵"。libusb 是裸 C 库没有事件循环，必须自己起线程泵 `libusb_handle_events`。

**练习 2**：`copyDetectedDevices`（[librevnatcpdriver.h:51-53](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.h#L51-L53)）是给谁用的？
**答案**：给 `CompoundDriver`（复合驱动，下一讲 u3-l3 详讲）。它需要把多台设备的发现列表汇总成自己的设备列表，这个接口让"新构造的驱动实例"直接继承"正在后台发现的老实例"的成果，避免发现列表清空后重新等 1 秒。

**练习 3**：TCP 版 `disconnect()`（[132-149](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L132-L149)）比 USB 版多做了哪三件事？为什么不需要 join 线程？
**答案**：多了 `transmissionTimer.stop()`、`transmissionQueue.clear()`、`transmissionActive = false`（清发送状态）和 `dataSocket.flush()`（把未写出的数据冲出去再关）。不需要 join，因为没有自建线程；也不用取消异步传输，因为 `QTcpSocket` 关闭即由 Qt 收尾。

### 4.5 两条通道对比：同一协议逻辑的两副躯壳

#### 4.5.1 概念说明

把 4.2–4.4 并排看，会发现一个清晰的分层：**凡是"协议"的部分只有一份，凡是"传输"的部分各有一份**。这个模块用一张对照表把差异收拢，然后画出贯穿本讲的调用序列图（也就是综合实践的预演）。

#### 4.5.2 核心流程：能力对照表

| 关注点 | USB（librevnausbdriver） | TCP（librevnatcpdriver） |
|---|---|---|
| 驱动名 | `"LibreVNA/USB"`（[36-39](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L36-L39)） | `"LibreVNA/TCP"`（[59-62](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L59-L62)） |
| 发现机制 | libusb 总线枚举 + VID/PID + 产品字符串（[288-359](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L288-L359)） | SSDP 组播 M-SEARCH + max-age 老化（[157-217](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L157-L217)） |
| 独占方式 | `libusb_claim_interface(0)` | 向固定端口建 TCP 连接（数据 19544/日志 19545） |
| 收数据的驱动源 | libusb 事件线程泵 `handle_events` | Qt 事件循环的 `readyRead` 信号 |
| 接收缓冲 | `USBInBuffer`（手工 memmove，512 对齐重提交） | `QByteArray`（append/remove） |
| 发送 | 同步 `libusb_bulk_transfer` 到端点 0x01 | `dataSocket.write()` |
| 断连检测 | `USBInBuffer::TransferError` | `QTcpSocket::errorOccurred` |
| 连接失败 | 抛 `std::runtime_error` | 返回 false |
| 发送队列/超时/解码循环/信号分发 | **完全相同**（代码近乎复制） | **完全相同** |
| 协议翻译（handleReceivedPacket、setVNA...） | **继承同一份**（librevnadriver.cpp） | **继承同一份** |

#### 4.5.3 源码精读：接收方向的完整调用序列

把三个模块串起来，**USB 路径**「一包原始字节 → VNAMeasurement → 信号」的全序列如下（每步标注文件:行号）：

```text
[libusb 事件线程]
1  libusb_handle_events()                          librevnausbdriver.cpp:283
2  → USBInBuffer::CallbackTrampoline()             usbinbuffer.cpp:100
3  → USBInBuffer::Callback()  追加 actual_length   usbinbuffer.cpp:57,70
4  → emit DataReceived()  (DirectConnection)       usbinbuffer.cpp:72
5  → LibreVNAUSBDriver::ReceivedData()             librevnausbdriver.cpp:158
6  → Protocol::DecodeBuffer()  解出一帧            librevnausbdriver.cpp:165 → Protocol.cpp:28
7  → dataBuffer->removeBytes(handled_len)          librevnausbdriver.cpp:175 → usbinbuffer.cpp:38
8  → (VNADatapoint) emit receivedPacket(packet)    librevnausbdriver.cpp:199  (QueuedConnection)
   ───────────── 线程边界：队列投递到主线程 ─────────────
9  LibreVNADriver::handleReceivedPacket()          librevnadriver.cpp:696
10 → VNADatapoint 分支：new 出的对象已由 DecodeBuffer 创建   librevnadriver.cpp:766 (Protocol.cpp:88)
11 → getValue(stage, port, ref) 取参考/接收复数    librevnadriver.cpp:778-780 → Protocol.hpp:81
12 → m.measurements["Sij"] = input / ref           librevnadriver.cpp:784
13 → delete res; emit VNAmeasurementReceived(m)    librevnadriver.cpp:794-795
   ↓ (DeviceDriver 基类信号，u3-l1 讲过)
14 各模式/TraceModel 的槽函数消费 m
```

**TCP 路径**只有入口不同，第 6 步起完全一致：

```text
[主线程 Qt 事件循环]
1' dataSocket 有数据 → emit readyRead               (Qt 内部)
2' LibreVNATCPDriver::ReceivedData()                librevnatcpdriver.cpp:219
3' dataBuffer.append(readAll())                     librevnatcpdriver.cpp:221
4' Protocol::DecodeBuffer()                         librevnatcpdriver.cpp:227 → Protocol.cpp:28
5' dataBuffer.remove(0, handled_len)                librevnatcpdriver.cpp:237
6' emit receivedPacket(packet)                      librevnatcpdriver.cpp:253  (QueuedConnection)
   ── 之后与 USB 路径第 9-14 步逐行相同 ──
```

两条路径在 `receivedPacket` 信号处**汇流**——这正是分层设计的收益：协议翻译层完全不知道字节来自 USB 还是 TCP，甚至将来换成 WebSocket 也不用改一行 `librevnadriver.cpp`。

#### 4.5.4 代码实践

1. **实践目标**：亲手验证"汇流点"——两条路径的分发 switch 是否真的逐行等价。
2. **操作步骤**：并排打开 [librevnausbdriver.cpp:182-201](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L182-L201) 与 [librevnatcpdriver.cpp:238-255](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L238-L255)，用 diff 工具或肉眼逐 case 对比，列出所有差异。
3. **需要观察的现象**：差异只有两处——USB 版多一个 `case None: break;`，以及 USB 版在 switch 前多了两条触发（SetTrigger/ClearTrigger）的 `qDebug`。
4. **预期结果**：确认 `Ack/Nack/SetTrigger/ClearTrigger → 就地消化；其余 → receivedPacket` 的结构完全一致，"汇流点"名副其实。
5. 纯代码对比，无需硬件。

#### 4.5.5 小练习与答案

**练习 1**：既然 USB/TCP 的发送队列、解码循环几乎相同，为什么作者不把 `SendPacket/transmissionFinished/startNextTransmission` 上提到 `LibreVNADriver`，只留一个"写字节"的纯虚接口？
**答案**：确实可以，那样能消除大段复制（也是明显的重构机会）。现状更像是**权衡后的务实选择**：队列本身无需多态、两份代码各自独立演化（TCP 版注释掉的调试输出与 USB 版略不同）。识别"可上提的重复"正是读驱动代码的附加收获——你在评审别人代码时也应能指出这一点。
**练习 2**：一条 `VNADatapoint` 从设备到 GUI，跨了几个线程（USB 路径）？
**答案**：两个——libusb 事件线程（第 1-7 步：收字节、解帧）与主线程（第 9-14 步：翻译与消费），中间靠 `receivedPacket` 的 QueuedConnection 投递跨越线程边界。
**练习 3**：若把第 8 步的 QueuedConnection 改成 DirectConnection，最可能先出什么问题？
**答案**：`handleReceivedPacket` 会在 libusb 事件线程里执行，进而 `emit VNAmeasurementReceived` 直连或默认连接到 GUI 对象的槽也会在事件线程跑——Qt GUI 类只能在主线程操作，轻则报警告重则崩溃；同时与主线程可能的并发访问（如 `info`、`lastStatus`）产生数据竞争。

## 5. 综合实践

**任务：绘制并标注双通道完整调用序列图**（本讲指定的实践任务）。

1. **实践目标**：不看本讲义 4.5.3 的成品，独立画出 USB 与 TCP 两条路径下「收到一包原始字节 → 解析成 `DeviceDriver::VNAMeasurement` → 发出信号」的调用序列图，每个函数标注文件与行号。
2. **操作步骤**：
   - 准备一张大纸或绘图工具，画两条竖直生命线：左"libusb 事件线程"、右"主线程"（TCP 版则只有主线程一条）；
   - 从 4.2.3 ④ / 4.4.3 ⑤ 的解码循环出发，向上追"字节从哪来"（`USBInBuffer::Callback` 或 `readyRead`），向下追"信号到哪去"（`handleReceivedPacket` 的 VNADatapoint 分支 → `VNAmeasurementReceived`）；
   - 在每条箭头上标 `文件名:起-止行号`，并在跨越线程边界的那条箭头上特别标注连接类型（QueuedConnection）；
   - 画完后对照 4.5.3 的参考图自评，补上漏掉的 `removeBytes`、`DevicePacketLog`、`delete res` 等细节；
   - 进阶（可选）：在图上用另一种颜色画出**发送方向**——`setVNA()` → `SendPacket()` → `startNextTransmission()` → 字节写出 → Ack 回来 → `transmissionFinished()` → 回调被调用，形成完整的请求-应答闭环。
3. **需要观察的现象**：两条路径的图在前几步完全不同（异步传输回调 vs readyRead），在中后段**汇合成同一条**——这个"汇流点"就是 `receivedPacket` 信号。
4. **预期结果**：一张能当作驱动物理设计的速查表的双通道序列图；你能指着图讲清"为什么换传输层不用动协议层"。
5. 本实践为纯阅读与绘图，无需硬件、无需编译，所有行号以 HEAD `c4276df` 为准。

## 6. 本讲小结

- `LibreVNADriver` 是"协议引擎"：实现 `DeviceDriver` 的全部语义接口，把高层设置翻译成 `Protocol::PacketInfo`、把协议包翻译成测量数据；传输被抽象为纯虚 `SendPacket()`，字节搬运完全交给子类。
- S 参数在 GUI 侧由一行除法算出：`m.measurements["Sij"] = input / ref`（`接收波/入射波`），端口语义由 `portStageMapping` 提供，`getValue()` 按"阶段+源掩码"从数据点里取复数。
- USB 驱动的骨架是：三重过滤枚举（VID/PID → 产品字符串 → 序列号）→ claim 接口 → 一个泵 `libusb_handle_events` 的事件线程 + 每端点一个 `USBInBuffer` + 单飞发送队列（Ack/Nack/超时驱动出队）。
- `USBInBuffer` 封装"提交-完成-追加-通知-重提交"循环；`inCallback` 标志 + 异常把消费时机强制在回调线程内，配 DirectConnection 实现无锁的单线程接收。
- TCP 驱动用 SSDP 组播（每秒 M-SEARCH、私有 `LibreVNA-serial:` 头、max-age 老化）解决"网络里没有总线"的发现问题，用双 TCP 连接复刻 USB 双端点的数据/日志分流；解码循环与发送队列和 USB 版近乎逐行相同。
- 两条路径在 `receivedPacket` 信号处汇流，`handleReceivedPacket` 之后的翻译逻辑只有一份——这是"逻辑与传输分离"分层设计的直接收益，也是给其他仪器写驱动时的模板。

## 7. 下一步学习建议

- **u3-l3 驱动生态**：看第三方驱动（SSA3000X/SNA5000A）如何在"能力有限"的设备上实现同一套接口，以及 `CompoundDriver` 如何用本讲的 `passOnReceivedPacket` 钩子与 `copyDetectedDevices` 把多台 LibreVNA 组合成多端口虚拟设备——那是对本讲分层设计的最有力印证。
- **u4-l1 设备端通信架构**：本讲只看了协议的 GUI 半边；下一单元进入固件侧的 `Communication.cpp`，看 `DecodeBuffer` 的对偶物如何在 STM32 上分发同样的包。
- **顺手读**：[Device/LibreVNA/devicepacketlog.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlog.cpp)——本讲两处提到的包日志单例，是抓"设备到底发了什么"的第一工具，配合 GUI 菜单里的 "View Packet Log" 使用。
