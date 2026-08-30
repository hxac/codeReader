# u4-l3 GUI 侧协议实现：librevnadriver 收发全链路

## 1. 本讲目标

前两讲我们分别看了固件端的 Communication 框架（u4-l1）和协议文档的逐包格式（u4-l2）。本讲换到 PC 端，从 GUI 视角把协议闭环走完。学完本讲，你应该能够：

1. 在 GUI 源码中定位「构造并发送每一种控制包」的函数：`setVNA`、`setSA`、`setSG`、`setIdle`、`sendWithoutPayload`，并说出它们把 `DeviceDriver` 的抽象设置翻译成 `Protocol::PacketInfo` 的字段映射规则。
2. 解释一条测量数据包从 USB/TCP 原始字节进入 GUI，到最终变成 `VNAmeasurementReceived` / `SAmeasurementReceived` 信号的完整路径，包括拆帧、CRC 校验、线程切换和 S 参数拼装。
3. 使用设备包日志工具（`DevicePacketLog` / `DevicePacketLogView` / SCPI 命令 `DEVice:PACKETLOG`）抓取并解读一次真实的收发会话。

## 2. 前置知识

本讲默认你已读过 u4-l1（固件端 Communication 与 Protocol）和 u3-l2（LibreVNA 驱动的两层结构）。为方便独立阅读，先复习几个关键概念：

- **PacketInfo：类型＋联合体的协议包**。`Protocol::PacketInfo` 是一个「1 字节类型 + union 载荷」的结构体，GUI 与固件共同编译同一份 `Protocol.hpp`，所以两端对每个字段的位置、大小、字节序的理解天然一致（u1-l2、u4-l1）。
- **五段式帧**。线上传输时，PacketInfo 被编码为 `0x5A 帧头 + 2 字节小端总长 + 1 字节类型 + 变长载荷 + CRC32` 的帧；`VNADatapoint` 例外——它的 CRC 恒为 0，以此换取高吞吐（u4-l2）。
- **Ack/Nack 应答制**。设备对主机发来的每条命令回 `Ack`（接受）或 `Nack`（拒绝）；测量数据包则由设备主动推送、无需应答。
- **Qt 信号与槽的连接类型**。`Qt::DirectConnection` 表示槽函数在发射信号的线程里立即执行；`Qt::QueuedConnection` 表示把调用投递到接收者所属线程的事件队列里排队执行。本讲的接收路径恰好两种都用到了，是观察线程模型的好例子。
- **单包在途（stop-and-wait）**。因为 `Ack` 包不带序号，GUI 无法把应答与具体哪条命令对应起来，所以发送队列一次只允许一个包「在路上」，收到应答（或超时）才发下一个。这是理解 `transmissionQueue` 的关键。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.h/.cpp` | 官方设备驱动的公共层：把模式设置翻译成协议包（发送方向），并把收到的包解释成测量信号（接收方向）。本讲主角。 |
| `Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.h/.cpp` | USB 传输后端：libusb 收发、传输队列、应答/超时状态机。 |
| `Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp` | TCP 传输后端：与 USB 版几乎对称的实现，用于对照。 |
| `Software/VNA_embedded/Application/Communication/Protocol.cpp` | 协议编解码：`EncodePacket` / `DecodeBuffer`，GUI 与固件共用（u4-l1 已精读，本讲只引用关键行）。 |
| `Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlog.h/.cpp` | 设备包日志的单例数据模型：有界环形缓冲、JSON 序列化。 |
| `Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlogview.cpp` | 包日志查看对话框：树形解码每个包的字段，支持保存/加载 `.vnalog` 文件。 |

回忆 u3-l2 的结论：`LibreVNADriver` 承担全部协议语义，`SendPacket` 是纯虚函数，字节收发下放给 USB/TCP 子类。本讲就是把这句话展开成代码。

## 4. 核心概念与源码讲解

### 4.1 模块一：包构造与发送

#### 4.1.1 概念说明

GUI 各模式（VNA/频谱仪/信号源）不直接接触协议，它们只调用 `DeviceDriver` 的抽象接口 `setVNA` / `setSA` / `setSG` / `setIdle`。`LibreVNADriver` 负责把这些「硬件无关的设置结构体」翻译成「硬件相关的协议包」：

- **发送方向的三层分工**：
  1. **翻译层**（`LibreVNADriver`）：`DeviceDriver::VNASettings` → `Protocol::PacketInfo`，处理单位换算、枚举映射、端口-阶段编排；
  2. **队列层**（USB/TCP 子类的 `SendPacket`）：把包连同超时与回调压入 `transmissionQueue`，维持单包在途；
  3. **编码层**（`startNextTransmission`）：调 `Protocol::EncodePacket` 序列化成字节帧，写入 USB 端点或 TCP socket。

- **为什么要翻译？** 上层设置用的是 SI 单位（Hz、dBm、秒）和 bool；协议包为了省带宽用的是整数刻度（cdbm＝dBm×100）、位域和 `uint16_t` 微秒。翻译层就是这道「物理单位 ↔ 线上格式」的换算闸门。

#### 4.1.2 核心流程

以「用户在 VNA 模式点击启动扫描」为例，发送链路是：

```text
VNA 模式 UI
  └─ DeviceDriver::setVNA(VNASettings, cb)          (硬件无关)
       └─ LibreVNADriver::setVNA                     (本讲 4.1.3)
            ├─ 校验 supports(Feature::VNA)
            ├─ 生成 portStageMapping（端口 → 激励阶段）
            ├─ 填充 PacketInfo{type=SweepSettings, ...}
            ├─ 记录 lastNonIdlePacket = p
            └─ SendPacket(p, 回调包装)               (纯虚，下放传输层)
                 └─ LibreVNAUSBDriver::SendPacket     (入队)
                      └─ startNextTransmission
                           ├─ Protocol::EncodePacket(p, buffer)
                           ├─ DevicePacketLog 记录（无 serial → "LibreVNA-GUI"）
                           ├─ libusb_bulk_transfer(EP 0x01)
                           └─ transmissionTimer.start(500ms)
                                ├─ 收到 Ack/Nack → receivedAnswer → transmissionFinished
                                └─ 超时 → transmissionTimeout → transmissionFinished(Timeout)
```

单位换算规则汇总（发送方向）：

| 上层字段（SI） | 协议字段（整数刻度） | 换算 |
| --- | --- | --- |
| `dBmStart/dBmStop`（double，dBm） | `cdbm_excitation_start/stop` | ×100 |
| `dwellTime`（double，秒） | `dwell_time` | ×1e6 并夹到 \([0, 65535]\) |
| 激励端口列表 `excitedPorts` | `stages`＋`port1..4Stage` | 阶段数−1；各端口所在阶段号 |
| bool 开关（如 `logSweep`） | 位域 | `? 1 : 0` |

#### 4.1.3 源码精读

**（1）setVNA：翻译 SweepSettings 包**

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp:480-532](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L480-L532)

这段是 VNA 扫描设置的完整翻译。要点逐条对应：

- L482-485：先做能力协商——设备还没上报 `DeviceInfo`（`supports(VNA)` 为假）时直接失败并打印调试信息；
- L486-488：没有激励端口等价于「什么都不测」，转走 `setIdle`；
- L491-494：构造 **portStageMapping**。`excitedPorts` 是本次扫描中轮流加激励的端口列表（如 `{1,2}`），映射记录「端口 → 它在第几个阶段被激励（从 0 数）」。双端口全激励时为 `{1→0, 2→1}`。这个映射在接收方向还要用（见 4.2.3）；
- L496-522：逐字段填充包。注意 L502-503 的 `* 100`（dBm→cdbm）、L504 的 `stages = excitedPorts.size() - 1`、L505-511 把驻留时间从秒换算成微秒并夹在 \([0, 2^{16}-1]\)（协议字段是 `uint16_t`，溢出会被静默截断，所以这里手动夹紧）、L516 的 `zerospan` 判定（起止频率与功率都相同即为零扫宽/点频模式，此时数据点以时间戳而非频率为横轴）、L517-520 用 `std::find` 查每个端口在激励列表中的下标填入 `portXStage`；
- L524-525：记下 `isIdle = false` 并把整个包存为 **lastNonIdlePacket**——这是「暂停-恢复」机制的存档点（见第（4）点）；
- L527-531：调 `SendPacket` 并把 `TransmissionResult` 回调包装成 `bool` 回调（`Ack` 才算成功）交给上层。

**（2）三个兄弟函数：setSA / setSG / setIdle**

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp:543-589](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L543-L589) 填充 `SpectrumAnalyzerSettings` 包。L556-561 把 SA 点数夹到最多 1001：当 span（Hz）小于 1001 时每 Hz 取一点（`span+1`），否则固定 1001 点。L569-571 是一个条件优化：仅在「无跟踪源＋用户开启 DFT＋RBW 足够小＋非零扫宽」时置 `UseDFT=1`，让设备端改用 FPGA 片上 DFT 降低数据量。

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp:600-611](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L600-L611) 信号源最简单：频率、功率（又一次 ×100 变 cdbm）、活动端口，三个字段填完直接发送，连回调都不需要。

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp:613-623](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L613-L623) 置空闲：先本地置 `isIdle=true` 再发空的 `SetIdle` 包。模式切换时 ModeHandler 调的正是它（u2-l2 的「先 deactivate」）。

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp:890-895](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L890-L895) 是「无载荷命令」的便捷封装：所有 `Request*` 类查询（如 `RequestDeviceInfo`）载荷为空，只需设置类型字段。

**（3）传输队列：单包在途的发送状态机（USB 版）**

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.h:64-75](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.h#L64-L75) 定义了队列的基本构件：`Transmission` 结构（包＋超时＋回调三件套）、互斥保护的 `transmissionQueue`、单次触发定时器 `transmissionTimer` 和「当前是否有包在途」标志 `transmissionActive`。

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp:264-277](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L264-L277) 是 `SendPacket` 的 USB 实现：加锁入队，若当前空闲则立即启动发送。注意它**永远返回 true**——这只表示「已入队」，不代表设备接受。

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp:361-388](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L361-L388) `startNextTransmission` 是编码与发送的现场：L371 调 `Protocol::EncodePacket` 把队头包序列化进 1024 字节栈缓冲（失败说明包超过缓冲或类型不认识）；L377-378 把**发出的包**也记入设备包日志（注意没传 serial，之后在日志视图里显示为来源 `LibreVNA-GUI`）；L379 用 `libusb_bulk_transfer` 同步写到端点 `0x01`；L385 启动超时定时器（默认 500ms）。

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp:228-262](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L228-L262) `transmissionFinished` 由三条路径汇入：收到 `Ack`、收到 `Nack`（都经 `receivedAnswer` 信号）或定时器超时（[librevnausbdriver.h:43-45](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.h#L43-L45) 的 `transmissionTimeout` 槽）。它弹出队头、调用用户回调、停表，然后循环尝试发下一个包；若编码或写出失败，则以 `InternalError` 回调并丢弃该包。L232-234 对「队列已空却收到应答」发出警告——这正是杂散 Ack（应答与命令对不上号）的征兆，也是该协议只能单包在途的原因。

**（4）lastNonIdlePacket：切参考源时的「暂停-恢复」**

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp:666-678](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L666-L678) 展示了 `lastNonIdlePacket` 的用途：扫描进行中切换内外参考源会导致频率校准失效、输出错误频率。所以驱动先 `SetIdle` 停机，再发 `Reference` 包，最后**重发存档的 lastNonIdlePacket** 恢复原测量。一个字段解决了「打断后如何继续」的问题。

**（5）TCP 版对照**

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp:311-324](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L311-L324) 与 [Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp:351-376](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L351-L376) 是 TCP 版的 `SendPacket` / `startNextTransmission`：队列、定时器、状态机逐行雷同，唯一区别是 L368 用 `dataSocket.write` 代替 `libusb_bulk_transfer`。这印证了 u3-l2 的分层设计——传输后端只换「最后一米」。

#### 4.1.4 代码实践（无硬件路径：调用链走读＋发送函数打点）

**实践目标**：不依赖设备，验证你能凭源码写出「启动一次 VNA 扫描」的完整发送调用链，并理解队列状态机的每个转移。

**操作步骤**：

1. 打开 `librevnadriver.cpp` 的 `setVNA`（L480 起），对照 4.1.2 的换算表，手算一个具体例子：`freqStart=1e9, freqStop=2e9, points=501, IFBW=1000, dBmStart=dBmStop=-10, excitedPorts={1,2}, dwellTime=0`。写出生成的 `SweepSettings` 包每个字段值。
2. 继续向下追：`SendPacket`（纯虚）→ USB 版 L264 → `startNextTransmission` L361 → `EncodePacket`。在纸上画出这次发送后 `transmissionQueue` / `transmissionActive` / `transmissionTimer` 三个状态量的取值。
3. （可选，修改属建议、请自行操作）在 `startNextTransmission` 的 L371 之后临时加一行 `qDebug() << "Sending packet type" << (int) t.packet.type;`，重新编译 GUI。无硬件时不会触发，但你可以把这行当作之后有设备时的观察点。
4. 回答状态机问题：若设备对 `SweepSettings` 回了 `Nack`，队列里还压着 3 个包，会发生什么？逐行读 L241-258 确认。

**需要观察的现象**：步骤 1 中你应得到 `cdbm_excitation_start = -1000`、`stages = 1`、`port1Stage = 0`、`port2Stage = 1`、`zerospan = false`、`fixedPowerSetting = 1`（因为两端功率相等且未开功率调整）。

**预期结果**：手算结果与 L496-522 的代码逻辑逐字段吻合；步骤 4 的结论是——`Nack` 只影响队头那一个包（回调收到 `Nack`），随后 L249-258 会继续尝试发送后续包，协议不会因一次拒绝而清空队列。

（本实践为源码走读型，无需运行设备；步骤 3 的打点效果待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SendPacket` 的 USB 实现总是返回 `true`，而 `setVNA` 的返回值又是什么含义？

**答案**：`SendPacket` 返回 true 只表示「包已成功入队并启动发送流程」；真正的成败由异步回调 `TransmissionResult`（Ack/Nack/Timeout/InternalError）告知。`setVNA` 的布尔返回值表示「配置请求是否被发出且前置检查通过」（如能力校验失败会直接返回 false），最终结果仍经 `cb` 回调传递。

**练习 2**：把 `dwellTime` 设为 0.1 秒，`dwell_time` 字段会得到什么值？为什么代码要夹上限？

**答案**：0.1 × 1e6 = 100000 微秒，未超 65535？超了——100000 > 65535，会被夹到 65535（约 65.5ms）。因为协议字段是 16 位无符号整数，最大只能表示 65535；不夹紧就会溢出回绕成一个很小的值，设备端行为完全错误。

**练习 3**：`setExtRef` 为什么不能在扫描进行中直接发 `Reference` 包？

**答案**：切换参考源后，内部参考的频率校准不再适用，正在进行的扫描会输出错误频率（L671-674 注释）。所以驱动先 `SetIdle` 停机、切源、再重发 `lastNonIdlePacket` 恢复测量。

### 4.2 模块二：接收解析路径

#### 4.2.1 概念说明

接收方向要解决四个问题：

1. **字节从哪来**：USB 用 libusb 异机传输＋独立事件线程；TCP 用 `QTcpSocket::readyRead`。
2. **怎么拆帧**：一次传输可能包含半个包、一个包或多个包（粘包/半包），`Protocol::DecodeBuffer` 负责在字节流中同步、校验并还原 PacketInfo。
3. **在哪个线程处理**：拆帧在 libusb 事件线程（DirectConnection），协议解释在 GUI 线程（QueuedConnection）——一条包恰好经历一次线程跳变。
4. **怎么变成测量**：`VNADatapoint` 里存的是各接收机的**原始读数**（含参考通道），S 参数由 GUI 按「接收/参考」比值拼装——这是支持多机同步（CompoundDriver）的关键设计（u3-l2、u4-l2）。

用反射参数的语言说，第 \(j\) 端口激励时第 \(i\) 端口的 S 参数为：

\[ S_{ij} = \frac{b_i}{a_j} \approx \frac{V_{\text{port}i}^{\text{(stage } j\text{)}}}{V_{\text{ref},j}^{\text{(stage } j\text{)}}} \]

其中分子是第 \(i\) 个测量接收机在该阶段的读数，分母是第 \(j\) 端口参考接收机在同一阶段的读数。代码里就是 `input / ref`。

#### 4.2.2 核心流程

```text
设备 → USB EP 0x81 / TCP 19544 端口
  USBInBuffer 异步收字节（libusb 事件线程）
     └─ DataReceived 信号 [DirectConnection → 仍在事件线程]
          └─ ReceivedData(): do { DecodeBuffer(...) } while(handled_len > 0)
               ├─ 跳过帧前垃圾字节 → 记 InvalidBytes
               ├─ 帧不完整 → 返回 0，字节留在缓冲等下次
               ├─ CRC 校验（VNADatapoint 除外）
               └─ 得到 PacketInfo
          switch(packet.type):
               ├─ Ack/Nack    → emit receivedAnswer   → transmissionFinished（队列状态机）
               ├─ Set/ClearTrigger → emit receivedTrigger
               └─ 其他         → emit receivedPacket  [QueuedConnection → GUI 线程]
                    └─ LibreVNADriver::handleReceivedPacket
                         ├─ DeviceInfo  → 填 Info/能力/限制 → emit InfoUpdated
                         ├─ DeviceStatus → 存 lastStatus → emit StatusUpdated/FlagsUpdated
                         ├─ VNADatapoint → 按 portStageMapping 拼 S 参数 → emit VNAmeasurementReceived
                         └─ SpectrumAnalyzerResult → 填 PORT1/PORT2 → emit SAmeasurementReceived
```

#### 4.2.3 源码精读

**（1）连接建立：两条握手命令与信号接线**

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp:113-126](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L113-L126) 是理解线程模型的最佳入口：

- L113 启动独立接收线程（循环体见 [L279-286](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L279-L286)，就是不停调 `libusb_handle_events`）；
- L114-115 创建两个 `USBInBuffer`：数据走端点 `0x81`，日志走 `0x82`（通道隔离，u4-l1）；
- L116/L118 用 `Qt::DirectConnection` 连接 `DataReceived`——槽在 libusb 事件线程执行，拆帧不排队，吞吐优先；
- L120-121 用 `Qt::QueuedConnection` 连接 `receivedAnswer`/`receivedPacket`——协议解释被投递回 GUI 线程排队执行，避免跨线程触碰 UI；
- L125-126 连接一成功就发出 `RequestDeviceInfo` 和 `RequestDeviceStatus` 两条握手命令（经 4.1 的发送队列）。设备的回答将驱动 4.2.3（3）的 `DeviceInfo` 分支。

端点编号定义在 [librevnausbdriver.h:48-50](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.h#L48-L50)：出 `0x01`、入 `0x81`、日志入 `0x82`。TCP 版对应两个端口 19544/19545（[librevnatcpdriver.cpp:13-14](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L13-L14)）。

**（2）ReceivedData：拆帧循环**

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp:158-211](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L158-L211) 是接收侧的核心循环，`do...while(handled_len > 0)` 一次唤醒可能清掉多个包：

- L165 调 `Protocol::DecodeBuffer(buffer, received, &packet)`，返回「本次消费了多少字节」；
- L167-174 把**收到的包**（带 serial）或**无效字节**记入设备包日志；
- L175 `removeBytes(handled_len)` 只移走已处理字节——半包留下的尾巴等下次数据到达继续拼，这就是抗粘包/半包的全部秘密；
- L182-201 按类型分流：`Ack`/`Nack` 走 `receivedAnswer`（喂给 4.1 的发送状态机）；`SetTrigger`/`ClearTrigger` 走 `receivedTrigger`（多机同步的硬件触发，u3-l3）；**其余一律** `receivedPacket` 交给公共层解释。

`DecodeBuffer` 的内部逻辑在 [Software/VNA_embedded/Application/Communication/Protocol.cpp:28-93](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.cpp#L28-L93)（u4-l1 已逐段精读，这里只列 GUI 侧关心的行为）：

- L35-43 向前扫到 `0x5A` 帧头为止，前面的垃圾字节被消费并计入返回值（GUI 侧据此记 `InvalidBytes`）——**重同步**；
- L45-49 / L60-64 帧头或载荷不完整时返回当前偏移，若没有垃圾前缀则返回 0，字节原地等待——**半包**；
- L69-76 CRC32 不匹配则跳过 1 字节重新找帧头——**坏帧恢复**；
- L79-90 `VNADatapoint` 特殊处理：CRC 恒 0（豁免校验），且载荷是变长编码，需 `new VNADatapoint<32>` 再 `decode`——**这也解释了 4.2.3（4）里那句 `delete res` 的来历：这块堆内存是 DecodeBuffer 分配的**。

**（3）handleReceivedPacket：协议解释与 S 参数拼装**

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp:696-814](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L696-L814) 在 GUI 线程执行，是所有设备→GUI 数据的唯一汇聚点：

- L698 先 `emit passOnReceivedPacket(packet)`——无条件转发给 CompoundDriver 旁听（u3-l3），让多机聚合不必劫持后续逻辑；
- L700-702 `skipOwnPacketHandling` 为真时到此为止（复合驱动的子驱动用）；
- L705-758 `DeviceInfo` 分支：L707-717 协议版本号比对，不一致就弹窗建议升级固件（对应 u4-l2 讲过的「协议版本管时间轴」）；L719-750 把设备上报的能力与限制搬进 `DeviceDriver::Info`（端口数、频率/功率/IFBW/RBW 限值，注意 cdbm 又 ÷100 变回 dBm）；L756 发 `InfoUpdated` 信号通知 UI；
- L759-763 `DeviceStatus` 分支：整包存入 `lastStatus`，发 `StatusUpdated`/`FlagsUpdated`（状态栏与锁定/过载旗标的数据源，见 `getFlags` L305 起）；
- L764-797 `VNADatapoint` 分支，本讲最精彩的一段：
  - L766 取出指向堆上 datapoint 的指针；L769-774 零扫宽时横轴用 `us`（微秒时间戳），否则用 `frequency` 与 `cdBm/100`；
  - L775-793 双层循环拼 S 参数：外层遍历 `portStageMapping`（4.1.3 建立的「激励端口→阶段」映射），`ref = getValue(stage, port-1, true)` 取该端口**参考接收机**读数（末参数 true 表示参考通道）；内层对每个物理端口 `i` 取 `input = getValue(stage, i-1, false)`（测量通道），二者都非 NaN 时记 \( S_{ij} = \text{input}/\text{ref} \)（L784）——正是 4.2.1 的公式。`captureRawReceiverValues` 开关打开时（L786-791）额外导出 `RawPortXStageY` 原始读数，供设备级校准对话框使用；
  - L794 `delete res` 释放 DecodeBuffer 分配的 datapoint——**所有权交接**：谁 new 谁 delete，这里由接收方兜底；
  - L795 `emit VNAmeasurementReceived(m)`——数据就此进入模式层/TraceModel（u7、u8 的故事）；
- L798-810 `SpectrumAnalyzerResult` 分支简单得多：两个端口的线性电压值直接塞进 `PORT1`/`PORT2` 键，发 `SAmeasurementReceived`。

**（4）TCP 版对照**

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp:219-257](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnatcpdriver.cpp#L219-L257) 的 `ReceivedData` 与 USB 版逐行对应：L221 先 `readAll()` 追加进 `QByteArray` 当缓冲，L227 同样调 `DecodeBuffer`，L237 同样只移除已处理字节。区别仅在字节来源（socket 信号天然在 GUI 线程，无需 DirectConnection 的线程跳变）与少了对 trigger 包的两行调试输出。

#### 4.2.4 代码实践（无硬件路径：线程路径标注＋异常流分析）

**实践目标**：凭源码确认「一条 VNADatapoint 从 USB 字节到 TraceModel 入口共经历几站、几次线程切换、几次堆分配」，并分析三种损坏流的恢复行为。

**操作步骤**：

1. 画出接收序列图，纵轴为站点、横轴为时间，至少包含：`USBHandleThread`（libusb 事件线程）→ `DecodeBuffer` → `receivedPacket`（跨线程点，标注 Queued）→ `handleReceivedPacket`（GUI 线程）→ `VNAmeasurementReceived`。每一站标注文件名与行号（参考 4.2.2 流程图）。
2. 数一数堆分配：`DecodeBuffer` L88 的 `new VNADatapoint<32>` 与 `handleReceivedPacket` L794 的 `delete res` 各一次，确认它们配对。
3. 异常流分析（对照 [Protocol.cpp:28-93](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.cpp#L28-L93)）：
   - 流量只含帧的前 5 个字节（半帧头）；
   - 流量以 3 个垃圾字节开头后跟一个完整帧；
   - 一个 CRC 损坏的完整帧。
   分别写出 `DecodeBuffer` 的返回值、`packet.type` 的值，以及 GUI 侧日志（`addPacket` 还是 `addInvalidBytes`）记录的内容。
4. （可选打点）在 `handleReceivedPacket` 的 L795 前临时加 `qDebug() << "VNA point" << m.pointNum;`，供将来有设备时观察点号推进。

**需要观察的现象**：本实践为纸面推演；步骤 3 的三种情形应当分别得出「返回 0、type=None、不记日志（字节留存）」「返回 3、type=None、记 3 字节 InvalidBytes，随后正常解码帧」「返回 1、type=None、记 1 字节 InvalidBytes，从下一字节重新找帧头」。

**预期结果**：你能不看资料复述两站线程（事件线程拆帧、GUI 线程解释）和一次配对的堆分配；三种异常流的行为与 Protocol.cpp 的返回值语义一一对应。

（步骤 4 的运行输出待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `DataReceived` 用 DirectConnection 而 `receivedPacket` 用 QueuedConnection？

**答案**：`DataReceived` 的槽（拆帧）在 libusb 事件线程发射点直接执行，避免每个 USB 传输都往事件队列里塞消息、拖慢吞吐；拆完帧之后只把「一个完整的 PacketInfo」用 QueuedConnection 投递回 GUI 线程做协议解释，保证所有触碰 UI/模式状态的代码都在 GUI 线程串行执行。粗活在线程里干、细活排队回主线程。

**练习 2**：S 参数为什么不由设备端算好再传，而要 GUI 用 `input/ref` 拼？

**答案**：两个原因。其一，多机同步（CompoundDriver）时激励端口与测量端口可能分属两台设备，只有汇聚点（GUI）能看到两边的读数，比值必须在汇聚点算；其二，参考读数与测量读数同包上报，还支持 `captureRawReceiverValues` 这类原始值导出，供设备级校准使用（u3-l3、u4-l2）。

**练习 3**：`handleReceivedPacket` 开头为什么要先 `emit passOnReceivedPacket(packet)` 再检查 `skipOwnPacketHandling`？

**答案**：顺序保证旁听者（CompoundDriver）永远能看到每一个原始包，即便本驱动自己选择不处理（`skipOwnPacketHandling=true` 时子驱动只充当数据源）。转发是义务，处理是可选。

### 4.3 模块三：包日志工具

#### 4.3.1 概念说明

调试一个二进制协议，最痛苦的是「看不见线上发生了什么」。LibreVNA 在驱动里内置了一个全双工抓包器：

- **记录点有两个**：发送方向在 `startNextTransmission`（不传 serial），接收方向在 `ReceivedData`（传 serial）。两路都进同一个日志，用「来源」列区分方向。
- **有界环形缓冲**：日志总大小受偏好设置限制（默认 10 MB），满了就从最老的一条开始丢弃——抓包器自己不能把内存吃爆。
- **三种消费方式**：GUI 对话框树形浏览（`DevicePacketLogView`）、保存/加载 `.vnalog` JSON 文件、SCPI 查询 `DEVice:PACKETLOG` 直接把 JSON 吐给脚本。

它相当于内置的「协议 Wireshark」，且与 GUI 的协议实现零偏差（记录的就是 `PacketInfo` 本身，不是重新猜测的字节）。

#### 4.3.2 核心流程

```text
发送：startNextTransmission ──┐
                              ├─→ DevicePacketLog::addPacket/addInvalidBytes
接收：ReceivedData ───────────┘         │
                                        ├─ LogEntry 深拷贝（VNADatapoint 单独复制）
                                        ├─ addEntry：超预算则从队首逐条淘汰
                                        └─ emit entryAdded
消费：
  DevicePacketLogView（菜单 Device → View Packet Log）
     ├─ updateTree：逐条 getEntry 直到越界异常为止
     ├─ addEntry：按包类型解码字段成子树
     └─ Reset / Save(.vnalog) / Open(.vnalog)
  SCPI: DEVice:PACKETLOG? → toJSON().dump()
```

#### 4.3.3 源码精读

**（1）数据模型：单例＋有界队列**

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlog.h:14-31](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlog.h#L14-L31)：`DevicePacketLog` 继承 QObject 与 Savable，`getInstance()` 是 Meyers 单例——全进程一份，USB/TCP/复合驱动共用。接口就三个：`addPacket`、`addInvalidBytes`、`reset`。

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlog.cpp:11-16](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlog.cpp#L11-L16)：构造时从偏好设置读上限 `Debug.USBlogSizeLimit`，默认值 10000000 字节（见 [preferences.h:399](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.h#L399) 的描述表）。

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlog.cpp:30-44](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlog.cpp#L30-L44) `addPacket`：深拷贝一份 `PacketInfo` 存档。L38-39 的特判至关重要——若包类型是 `VNADatapoint`，还要再深拷贝一份 datapoint 对象。原因回到 4.2.3：`PacketInfo` 里的 `VNAdatapoint` 只是个指针，指向 DecodeBuffer 分配的堆内存，而调用方随后就会 `delete` 它（librevnadriver.cpp L794）；不拷贝，日志里就剩悬空指针。[devicepacketlog.cpp:109-127](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlog.cpp#L109-L127) 的拷贝构造函数同理。

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlog.cpp:87-97](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlog.cpp#L87-L97) `addEntry`：互斥保护下累计占用（每条的近似代价由 [devicepacketlog.h:55-67](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlog.h#L55-L67) 的 `storageSize()` 估算，VNADatapoint 额外计 `sizeof(VNADatapoint<32>)`），`while` 循环从 `deque` 首部淘汰直到回到预算内，最后 `emit entryAdded` 通知视图。收发两侧都可能从非 GUI 线程调用（发送在 GUI 线程、接收拆帧在 libusb 线程），所以互斥锁不可省。

**（2）序列化：`.vnalog` 文件格式**

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlog.cpp:129-154](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlog.cpp#L129-L154) `LogEntry::toJSON`：每条记录输出四个字段——`type`（"Packet" 或 "InvalidBytes"）、`timestamp`（UTC 毫秒）、`serial`、`data`。`data` 是**逐字节的原始内存映像**：Packet 条目按 `sizeof(Protocol::PacketInfo)` 把整个结构体按字节塞进 JSON 数组，VNADatapoint 再单列一个 `datapoint` 数组；InvalidBytes 条目则直接存坏字节。这个「按内存映像存」的选择让文件格式无需随协议字段演进，代价是人不能直接读，必须靠视图解码（或自己写脚本按 `Protocol.hpp` 的布局解析）。`fromJSON`（[L156-181](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlog.cpp#L156-L181)）是逆操作，供加载文件。

**（3）视图：从字节到人类可读**

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlogview.cpp:19-75](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlogview.cpp#L19-L75) 对话框构造：接线三个按钮——Reset（清空日志并刷新）、Save（把 `toJSON()` 紧凑写出为 `.vnalog`）、Open（读文件、`fromJSON`、刷新树）。

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlogview.cpp:82-102](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlogview.cpp#L82-L102) 的 `updateTree` 用了一个「循环直到抛异常」的遍历模式：`for(i=0;;i++) addEntry(log.getEntry(i))`，靠 `getEntry` 越界抛 `runtime_error`（[devicepacketlog.cpp:77-85](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlog.cpp#L77-L85)）终止循环——因为遍历期间新条目可能还在并发涌入，用异常当游标比先取 size 更稳妥。

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlogview.cpp:104-158](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlogview.cpp#L104-L158) `addEntry` 负责人类可读化：L108 决定「来源」列——serial 非空显示设备序列号，为空显示 `LibreVNA-GUI`（这正是发送侧记录不传 serial 的用意：一眼分清方向）；L113-118 的 `packetNames` 表把类型编号映射为名字，35 个条目正好覆盖类型 0-34（与 u4-l2 说的「代码已到 v14、新增类型 33/34」对上）；L125-158 定义了添加子节点的五个小工具 lambda。

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlogview.cpp:160-177](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/devicepacketlogview.cpp#L160-L177) 以 `SweepSettings` 为例解码字段：起止频率、点数、IF 带宽、功率（注意 ÷100 换回 dBm——与 4.1 的 ×100 互逆）、同步模式、抑制峰、固定功率、对数扫描、阶段数与端口阶段。一个值得注意的观察：该列表**尚未展示 `dwell_time`**（v14 新增字段），说明视图解码滞后于协议演进——读日志时要记得这类字段可能「存在但未显示」。

**（4）入口：菜单项与 SCPI 命令**

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp:234-239](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L234-L239) 创建 `View Packet Log` 动作并加入驱动专属动作表；设备连接后这些动作被插入主窗口的 Device 菜单（[appwindow.cpp:410-412](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L410-L412)）。所以操作路径是：**连接设备 → 菜单 Device → View Packet Log**。

[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp:299-302](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L299-L302) 注册了 SCPI 查询 `DEVice:PACKETLOG`：返回 `log.toJSON().dump()`——配合 u10 将学的 TCP 远程控制，脚本也能直接取走整包日志做离线分析。

#### 4.3.4 代码实践

**分支 A（有设备）：抓取一次「启动 VNA 扫描」的完整会话**

1. **实践目标**：拿到从连接到出数的完整包序列，并解读前三个关键包。
2. **操作步骤**：
   - 编译并启动 GUI（u1-l3 的流程），连接设备；
   - 菜单 Device → View Packet Log 打开日志窗口，点 Reset 清空；
   - 切到 VNA 模式，设置一个小扫描（如 1-2 GHz、101 点），点启动扫描，等迹线出现；
   - 回到日志窗口观察（视图不会自动刷新，重新打开对话框即可触发 `updateTree`）；
   - 点 Save 保存 `.vnalog` 备份。
3. **需要观察的现象**：日志中应出现来源交替的条目——`LibreVNA-GUI` 发出的 `SweepSettings`（Type 2），设备序列号发出的 `Ack`（Type 7），随后设备源源不断上报 `VNADatapoint`（Type 27）；展开 SweepSettings 条目应能看到你设置的频率、点数、IFBW、阶段编排。
4. **预期结果**：启动扫描后的前三个包依次为：① SweepSettings（源 LibreVNA-GUI）② Ack（源为序列号）③ VNADatapoint（源为序列号）。若第②步是 Nack 或迟迟没有数据，则说明配置被设备拒绝或传输中断——日志本身就是排障证据。（具体序号与内容待本地验证。）

**分支 B（无设备）：手工构造并加载一个 `.vnalog` 文件**

1. **实践目标**：验证你理解了日志文件格式与加载路径，全程不需要设备。
2. **操作步骤**：
   - 用文本编辑器新建 `test.vnalog`，内容如下（示例文件，非项目原有资产）：

     ```json
     [{"data":[85,90,1,2,255],"serial":"","timestamp":1700000000000,"type":"InvalidBytes"},
      {"data":[85,90,1,2,255],"serial":"20354236E09D","timestamp":1700000000123,"type":"InvalidBytes"}]
     ```

   - 启动 GUI（无硬件即可，导入一个 Touchstone 示例测量保持程序忙碌）；
   - 打开日志窗口（若 Device 菜单未连接设备时没有该入口，可先连接任一驱动失败后再打开，或直接阅读下方「预期结果」推演），点 Open 加载 `test.vnalog`。
3. **需要观察的现象**：树中出现两条 `Invalid bytes` 条目，来源列分别为 `LibreVNA-GUI`（serial 为空）和 `20354236E09D`；状态栏显示条目数与占用字节数。
4. **预期结果**：与 `LogEntry::fromJSON`（devicepacketlog.cpp L156-181）的逻辑一致：type 字符串非 "Packet" 即按 InvalidBytes 解析，`data` 数组逐字节入队，serial 空串在视图里显示为 GUI。若加载失败，检查 JSON 是否为数组、文件扩展名是否为 `.vnalog`。（菜单入口在无设备时的可见性待本地验证；`fromJSON` 的解析行为可直接从代码确认。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `addPacket` 要对 `VNADatapoint` 做第二次深拷贝，而其他包类型不用？

**答案**：`PacketInfo` 用 union 存各类型载荷，多数类型是「按值嵌入」的结构体，拷贝 `PacketInfo` 即拷贝全部内容；唯独 `VNAdatapoint` 是指针，指向 `DecodeBuffer` 里 `new` 出来的堆对象，且接收路径在 `handleReceivedPacket` 末尾就会 `delete` 它。不单独深拷贝，日志条目会持有悬空指针。

**练习 2**：日志如何区分一个包是 GUI 发出的还是设备上报的？

**答案**：靠 serial 参数。接收侧 `addPacket(packet, serial)` 带上设备序列号；发送侧 `startNextTransmission` 里 `log.addPacket(t.packet)` 用默认空 serial。视图里空 serial 显示为 `LibreVNA-GUI`，非空显示序列号。

**练习 3**：`.vnalog` 文件为什么存「原始内存映像的字节数组」而不是直接存解码后的字段？

**答案**：内存映像让序列化与协议字段解耦——协议加字段（如 v14 的 `dwell_time`）不需要改日志格式，加载端用同一份 `Protocol.hpp` 编译就能正确解释；代价是人不可直接读、且跨协议版本加载可能错位。视图层的 `packetNames`/字段解码滞后（尚未显示 dwell_time）恰好说明两层是独立演进的。

## 5. 综合实践：「一张包的旅行卡」

把本讲三个模块串成一个任务：为**一个 SweepSettings 包**制作「旅行卡」，覆盖它的一生。

1. **构造站**：填卡片的「出生信息」。取 4.1.4 步骤 1 的手算结果，写出 `setVNA` 生成的完整 `PacketInfo`（类型、每个字段值），并注明哪些字段来自单位换算（×100、×1e6）、哪些来自端口编排（`portStageMapping`）。
2. **发送站**：画出它经过 `SendPacket` → `transmissionQueue` → `startNextTransmission` → `EncodePacket` → `libusb_bulk_transfer` 的路径，标注每一步所在文件与行号，以及它被记入包日志的确切位置（提示：发送侧记录不带 serial）。
3. **应答站**：写出设备回 `Ack` 后 `transmissionFinished` 的完整状态转移（弹队头→回调→停表→发下一个→队列空则 `transmissionActive=false`），并回答：若 500ms 内没有 Ack，哪个槽函数兜底？
4. **对照站（接收方向镜像）**：设备随后上报的第一个 `VNADatapoint` 走了哪些站？与发送方向相比多了哪两步（DecodeBuffer 拆帧、S 参数拼装）？在卡片背面画出它的路径，标出线程切换点和 `new/delete` 配对。
5. **验证站**：有设备的话用 4.3.4 分支 A 抓真实会话核对旅行卡；没有设备则与源码逐行核对。把完成的旅行卡保存下来——u10 学 SCPI/TCP 时你会再次需要它来对照「脚本发的命令在底层变成了哪些包」。

预期产出：一张双向标注的调用链图＋一份字段换算表。全部内容均可仅凭源码完成，不需要硬件（真实抓包结果待本地验证）。

## 6. 本讲小结

- **发送是三层流水线**：`LibreVNADriver` 的 `setVNA/setSA/setSG/setIdle` 负责把 SI 单位的抽象设置翻译成整数刻度的 `PacketInfo`（dBm×100、秒×1e6 夹 16 位、端口列表→阶段编排）；USB/TCP 子类的 `SendPacket` 入队；`startNextTransmission` 编码并写出。
- **单包在途状态机**：因为 Ack 不带序号，`transmissionQueue` 一次只允许一个包在途，`transmissionTimer`（默认 500ms）兜底超时，`transmissionFinished` 统一处理 Ack/Nack/Timeout/InternalError 四种结局。
- **接收是拆帧＋线程跳变**：libusb 事件线程里 `DecodeBuffer` 完成重同步、半包留存、CRC 校验（VNADatapoint 豁免），`receivedPacket` 以 QueuedConnection 跳回 GUI 线程由 `handleReceivedPacket` 解释——粗活在线程、细活在主线程。
- **S 参数在 GUI 拼**：`VNADatapoint` 上报原始接收机读数，`handleReceivedPacket` 按 `portStageMapping` 取 `input/ref` 比值得到 \( S_{ij} \)，这是多机同步与原始值导出的共同基础；`delete res` 与 `DecodeBuffer` 的 `new` 构成所有权配对。
- **包日志是内置协议分析器**：`DevicePacketLog` 单例在收发两侧同时记录（serial 区分方向），有界淘汰防膨胀，`.vnalog` 以内存映像存字节、视图按 `Protocol.hpp` 布局解码，SCPI `DEVice:PACKETLOG` 可直接导出 JSON。
- `lastNonIdlePacket` 用一个字段的代价实现了「切参考源时暂停-恢复测量」的事务语义。

## 7. 下一步学习建议

本讲走完了 GUI 与设备之间的协议闭环。接下来的两条路：

- **向上看数据去哪了**：u7-l1（VNA 模式）将追 `VNAmeasurementReceived` 信号之后的旅程——扫描设置如何从 UI 生成、测量如何进入 TraceModel；本讲的 `setVNA` 正是那条链的最后一站。
- **向右看远程控制**：u10-l1/u10-l2（SCPI 框架与 TCP 集成）会把本讲的 `DEVice:PACKETLOG` 这类命令放进完整的命令树，并教你用 `--no-gui` 把 GUI 变成无头协议网桥。
- 若想巩固协议本身，建议重读 [Protocol.hpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp) 中 `SweepSettings` 与 `VNADatapoint` 的定义，对照本讲 4.1/4.2 的字段表逐一确认——两端同源编译的那份合同，值得亲眼读一遍。
