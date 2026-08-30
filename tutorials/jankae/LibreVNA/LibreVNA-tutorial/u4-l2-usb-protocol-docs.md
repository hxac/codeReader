# u4-l2 USB 协议逐包解析（结合协议文档）

## 1. 本讲目标

上一讲（u4-l1）我们搞清楚了固件端 Communication 的收发架构：字节怎么进来、帧怎么拆、包怎么分发。本讲把镜头拉近到**包的内容本身**——每个字段叫什么、占几字节、位序如何、语义是什么。

LibreVNA 仓库的一大优点是：协议不是"只存在于代码里的潜规则"，而是有成文的 LaTeX 文档。学完本讲你应该能够：

1. 按文档描述一次**扫描设置包（SweepSettings）**与**测量数据包（VNADatapoint / SpectrumAnalyzerResult）**的端点、字段布局与字节序。
2. 对照 `PacketConstants.h`、`Protocol.hpp` 与 GUI 侧 `librevnadriver.cpp`，**验证文档与代码的一致性**——并且能识别"文档落后于代码"的地方。
3. 理解 **USB_protocol 与 Device_protocol 两层协议的划分**：v12 是纯 USB 时代的历史快照，v13 是传输无关（USB/以太网）的现行"设备协议"，而代码已经悄悄走到 v14。

## 2. 前置知识

- **协议文档（.tex/.pdf）**：仓库用 LaTeX 描述协议，编译后的 PDF 与 `.tex` 源码并排放在 `Documentation/DeveloperInfo/` 下。读 `.tex` 的好处是可以进 git 历史、可以 diff。
- **字节序（endianness）**：一个多字节整数（比如 8 字节的频率值）在内存里的排列顺序。LibreVNA 协议统一使用**小端（little-endian）**——低字节在前。STM32（ARM）与 PC（x86）恰好都是小端架构，这是协议能用 `memcpy` 整体序列化结构体的前提（u4-l1 已讲过 `#pragma pack(1)` + union 的手法）。
- **位域图怎么读**：文档里的位域图（tikz 绘制）**最左边是最高位（MSB）、最右边是最低位（bit 0）**，和常见寄存器手册一致。而 C 结构体里先声明的位域在 GCC 小端环境下占据**较低位**。所以"文档图从左往右"与"结构体从上往下"经常是**反着的**，本讲 4.2.3 会给一个完美对上的实例。
- **CRC32**：循环冗余校验，一种把任意长度字节串映射为 4 字节"指纹"的算法。LibreVNA 用的是反射式 CRC32（多项式 `0xEDB88320`，即标准 CRC-32/ISO-HDLC 的反射形式）。
- 承接 u4-l1 的结论：帧为"0x5A 帧头 + 2 字节长度 + 1 字节类型 + 变长 payload + 4 字节 CRC32"的五段式自描述结构；`PacketInfo` 用"类型 + union"整体 memcpy 序列化；高吞吐的 VNADatapoint 豁免 CRC。本讲不再重复推导，直接在字段级使用这些结论。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [Documentation/DeveloperInfo/USB_protocol_v12.tex](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/USB_protocol_v12.tex) | 协议文档第 12 版（历史快照，仅 USB 传输） |
| [Documentation/DeveloperInfo/Device_protocol_v13.tex](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex) | 协议文档第 13 版（现行，传输无关：USB + 以太网） |
| [Software/VNA_embedded/Application/Communication/PacketConstants.h](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/PacketConstants.h) | 帧字段偏移/长度、VNADatapoint 字段长度与位偏移的常量 |
| [Software/VNA_embedded/Application/Communication/Protocol.hpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp) | 当前代码的协议定义（`Version = 14`），GUI 与固件共同编译（u1-l3） |
| [Software/VNA_embedded/Application/Communication/Protocol.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.cpp) | CRC32、DecodeBuffer 拆帧、EncodePacket 组帧 |
| [Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp) | GUI 侧：构造下行包、解释上行包、协议版本协商 |

> 同目录下还有编译好的 `USB_protocol_v12.pdf`、`Device_protocol_v13.pdf`，不想读 LaTeX 源码时可以直接看 PDF。

## 4. 核心概念与源码讲解

### 4.1 USB 端点与包格式

#### 4.1.1 概念说明

先解决一个容易困惑的问题：**仓库里为什么有两份协议文档？**

- `USB_protocol_v12.tex`（v12）：早期文档，协议 = USB 专属。它写的 VID 是 `0x0483`（STMicro 的厂商号）。
- `Device_protocol_v13.tex`（v13）：把同一套包格式从 USB 中抽象出来，改名"设备协议"，新增了**以太网传输**（TCP 19544 数据口 / 19545 调试口 + SSDP 发现），并把 VID 换成 `0x1209`（pid.codes 开源厂商号）。

哪份与现实一致？看代码：固件的 [usbd_desc.c:L66](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/USB/Core/Src/usbd_desc.c#L66) 定义 `USBD_VID 0x1209`，而 GUI 的 [librevnausbdriver.cpp:L23-L25](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L23-L25) 同时接受新旧两组 VID/PID（`0x0483/0x564e`、`0x0483/0x4121`、`0x1209/0x4121`）以兼容老固件。**结论：v13 是现行文档，v12 是历史。**

端点布局两版文档一致：

- **Endpoint 0x01**（host→device）：协议数据下行
- **Endpoint 0x81**（device→host）：协议数据上行
- **Endpoint 0x82**（device→host）：纯 ASCII 调试日志，与协议通道隔离（u4-l1 讲过固件侧 usb.c 的实现）

#### 4.1.2 核心流程

每个包（无论走 USB 还是 TCP）都是同一五段式帧（固定开销 8 字节）：

```
┌────────┬──────────┬────────┬─────────────┬──────────┐
│ 0x5A   │ Length   │ Type   │ Payload     │ CRC32    │
│ 1字节  │ 2字节LE  │ 1字节  │ 0..N字节    │ 4字节LE  │
└────────┴──────────┴────────┴─────────────┴──────────┘
  帧头      总长度      包类型    内容视类型     校验覆盖前面
                     决定解释      全部字节
```

拆帧流程（u4-l1 已走读，这里只列骨架）：

1. 在字节流中搜索 `0x5A` 帧头（重同步，抗粘包/半包）；
2. 读小端 16 位长度，做合理性检查；
3. 收齐整帧后核对 CRC32——**唯一的例外是 VNADatapoint，其 CRC 恒为 0**；
4. 按 Type 字段把 payload 解释成对应结构体。

为什么数据包敢免 CRC？固件作者在编码函数里留了量化注释：**一次 CRC 计算约需 18 µs，是编码发送一个数据点耗时的大头**，高 IF 带宽下会成为吞吐瓶颈，故跳过。

#### 4.1.3 源码精读

v13 文档对 USB 接口与以太网接口的正式定义（含 VID/PID、端点、TCP 端口、单连接约定）：

- [Device_protocol_v13.tex:L176-L184](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L176-L184) —— USB 接口：`0x1209/0x4121`，三个 bulk 端点 0x01/0x81/0x82，0x82 专走 ASCII 调试信息。
- [Device_protocol_v13.tex:L186-L192](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L186-L192) —— 以太网接口：TCP 19544（数据）/19545（调试），每个服务器只支持单连接，新连接会顶掉旧连接。
- [Device_protocol_v13.tex:L196-L205](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L196-L205) —— 设备发现：SSDP，响应 M-SEARCH `ssdp:all` 或 `urn:schemas-upnp-org:device:LibreVNA:1`（对应 u3-l2 讲过的 GUI 侧 TCP 发现）。

帧结构的文档定义（v12 与 v13 逐字相同，仅"USB protocol"改为"device protocol"）：

- [Device_protocol_v13.tex:L208-L220](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L208-L220) —— 五个字段逐一列举，并明确"All values in the device protocol are little-endian"。长度字段的语义是**含帧头与 CRC 的总字节数**。

代码侧，这些"文档语言"被翻译成常量：

- [PacketConstants.h:L10-L25](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/PacketConstants.h#L10-L25) —— `PCKT_HEADER_DATA = 0x5A`；各字段偏移/长度用"前一偏移 + 前一长度"链式推导（`PCKT_COMBINED_HEADER_LEN = 4`，`PCKT_EXCL_PAYLOAD_LEN = 8`，即固定开销 8 字节，与文档口径一致）。

拆帧与组帧中字节的实际读法：

- [Protocol.cpp:L52-L58](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.cpp#L52-L58) —— 长度按 `data[1] | data[2]<<8` 读出：低字节在前，这就是"小端"在代码里的样子；随后做长度上限（`sizeof(PacketInfo)*2`）与下限（8）的合理性检查。
- [Protocol.cpp:L66-L78](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.cpp#L66-L78) —— CRC 同样按低字节在前拼成 `uint32_t` 再比较；CRC 失败只丢掉 1 个字节重新找帧头（而不是丢整段缓冲），最大限度保留后续合法帧。
- [Protocol.cpp:L146-L152](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.cpp#L146-L152) —— VNADatapoint 的 CRC 豁免在**发送端**的实现：直接写 `crc = 0x00000000`，注释给出"CRC 约 18 µs、是数据点编码发送耗时大头"的理由。
- [Device_protocol_v13.tex:L1271-L1273](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L1271-L1273) —— 同一豁免在**文档端**的正式声明（红色警示框）：该包 CRC 恒为 0x00000000。

#### 4.1.4 代码实践

**实践目标**：不碰硬件，在纸面上把一个 SweepSettings 包"组装"出来，从而内化帧结构与固定开销。

**操作步骤**：

1. 打开 [Protocol.hpp:L155-L184](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L155-L184)，按声明顺序累加每个字段的宽度（注意 `pack(1)` 下无填充，位域按 8 位/16 位分组各占 1/2 字节）：
   - `f_start`(8) + `f_stop`(8) + `points`(2) + `if_bandwidth`(4) + `cdbm_excitation_start`(2) = 24
   - 8 个 `uint8_t` 位域共 8 位 = 1 字节 → 25
   - 6 个 `uint16_t` 位域共 16 位 = 2 字节 → 27
   - `cdbm_excitation_stop`(2) → 29，`dwell_time`(2) → **31 字节 payload**
2. 帧总长 = 31 + `PCKT_EXCL_PAYLOAD_LEN`(8) = **39 字节**，长度字段两字节小端为 `27 00`。
3. 假设起始频率 10 MHz（\(10{,}000{,}000 = \text{0x00989680}\)），写出帧的前 12 字节（**示例数据**，仅供核对）：

```
5A 27 00 02 | 80 96 98 00 00 00 00 00 | 80 96 98 ...
│  │  │  │   └── f_start（小端 UINT64） ─────────────┘
│  │  │  └── Type=02（SweepSettings）
│  │  └── 长度高字节
│  └── 长度低字节（39=0x27）
└── 帧头
```

**需要观察的现象**：无需运行，重点体会"长度 = 含头含尾的总长"和"低字节在前"两条规则如何落到具体字节。

**预期结果**：payload 31 字节、帧总长 39 字节。此为静态推导，**待本地验证**（若手头有设备，可用 4.2.4 / 第 5 节的包日志路径抓真实帧核对；v13 文档给出的 SweepSettings 是 29 字节——差的 2 字节正是 4.3 节要讲的版本差异）。

#### 4.1.5 小练习与答案

**练习 1**：如果 payload 里恰好出现一个 `0x5A` 字节，接收方会不会把它误当帧头？
**答案**：不会造成错乱。拆帧只在"寻找帧头"阶段搜索 0x5A；一旦进入帧内，长度字段决定何时到帧尾。只有当流真正失步（比如 CRC 失败丢 1 字节）时才会重新搜索，此时可能"撞上" payload 里的 0x5A，但随后长度/CRC 校验几乎必然失败，于是再丢 1 字节继续找——这是自描述帧 + 校验和的标准重同步策略。

**练习 2**：为什么只有 VNADatapoint 免 CRC，而 SweepSettings 不免？
**答案**：吞吐不对称。SweepSettings 一场扫描只发一次，18 µs 无关痛痒；VNADatapoint 每个扫描点发一次、且高 IF 带宽下点间隔本身只有几百 µs 量级，CRC 占比过高。控制路径要绝对可靠（错了设备进错状态），数据路径丢了顶多一个点，下周期的数据会覆盖它。

**练习 3**：一个 SpectrumAnalyzerResult（v13/v14 布局）帧的长度字段值是多少？
**答案**：payload = 4×float(16) + UINT64(8) + UINT16(2) = 26 字节，总长 26+8 = 34 = 0x22，小端两字节 `22 00`。

### 4.2 设备层命令语义

#### 4.2.1 概念说明

v13 文档定义了 31 种包类型（编号 2–32；代码里还有 33/34，见 4.3）。按用途可分四类：

| 类别 | 方向 | 代表 | 特点 |
|---|---|---|---|
| **设置类** | H→D | SweepSettings(2)、SpectrumAnalyzerSettings(13)、Generator(12)、SetIdle(20) | 每种设置包同时是"模式切换"指令——发 SweepSettings 就进入 VNA 模式 |
| **请求类** | H→D | RequestDeviceInfo(15)、RequestDeviceStatus(26) 等 | 空载荷，触发一个（或一串）应答包 |
| **数据类** | D→H | VNADatapoint(27)、SpectrumAnalyzerResult(14)、DeviceStatus(25) | 高频上行，是测量的主体 |
| **维护类** | H↔D | FirmwarePacket(6)、SetTrigger(28)、SourceCalPoint(18) 等 | 固件升级（u1-l4）、多机同步、校准数据读写（u5-l3） |

确认协议是**单向应答**式：设备对每个成功处理的命令回 `Ack(7)`、失败回 `Nack(10)`；而主机从不 Ack 设备。若命令还会触发数据（如 SweepSettings 触发一连串 VNADatapoint），数据跟在 Ack 之后。

本讲重点是两个"主角包"：

- **SweepSettings（扫描设置）**：不仅携带频率/点数/IF 带宽，还携带 **stage（阶段）编排**——一次完整双端口 S 参数测量分两个阶段：阶段 0 激励打在端口 1、阶段 1 打在端口 2。
- **VNADatapoint（测量数据）**：设备**不**直接给 S 参数，而是给一组"接收机原始复数读数 + 内容描述掩码"，S 参数由**主机**拼装。

为什么要这样设计？文档说得很直白：多台 LibreVNA 同步测量时，参考接收机与端口接收机可能分布在**不同设备**上，除法只能由看到全部数据的主机来做。这也是 u3-l2 讲过的 `portStageMapping` 的协议侧根源。

#### 4.2.2 核心流程

一次 VNA 测量的典型包序列：

```
主机                                          设备
 │ ── RequestDeviceInfo(15) ──────────────────▶ │
 │ ◀────────────────────── DeviceInfo(5) ────── │   (含 ProtocolVersion，见 4.3)
 │ ── SweepSettings(2) ───────────────────────▶ │   (频率/点数/IFBW/功率/stage 编排)
 │ ◀──────────────────────────────── Ack(7) ──── │
 │ ◀──────── VNADatapoint(27) ×N 个扫描点 ────── │   (CRC=0，接收机读数+掩码)
 │ ── SetIdle(20) ────────────────────────────▶ │   (停止)
```

VNADatapoint 的 payload 是"12 字节头 + 三个等长数组"：

- 头部：Frequency(UINT64) + PowerLevel(INT16) + PointNumber(UINT16) = 12 字节；
- 数组 A：x 个 FLOAT（各读数实部）；数组 B：x 个 FLOAT（虚部）；数组 C：x 个 UINT8（每读数一个描述掩码）；
- **x 不显式传输**，由总长反推：

\[ array\_length = \frac{payload\_size - 12}{4 + 4 + 1} = \frac{payload\_size - 12}{9} \]

描述掩码的位分配（MSB→LSB）：`Stage[7:5] | Ref[4] | P4[3] | P3[2] | P2[1] | P1[0]`。LibreVNA 1.0 是三接收机架构（端口 1、端口 2、共享参考），完整双端口扫描每点产生 6 个读数，掩码分别为 `0x01, 0x02, 0x13, 0x21, 0x22, 0x33`。

S 参数拼装（以 S21 为例，激励在阶段 0 打在端口 1）：

\[ S_{21} = \frac{端口2接收机读数(阶段0)}{端口1参考接收机读数(阶段0)} \]

#### 4.2.3 源码精读

**包类型总表**（文档对 32 种包的编号、方向、应答关系的完整索引）：

- [Device_protocol_v13.tex:L251-L281](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L251-L281) —— 每行一种包：编号、名称、方向（H→D / D→H / 双向）、语义、会触发哪种应答（上标 c 表示会触发多个）。例如第 2 行：SweepSettings，H→D，应答 27 号（VNADatapoint，多次）。
- [Device_protocol_v13.tex:L284-L286](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L284-L286) —— Ack 语义与"主机从不 Ack"的约定。

**SweepSettings 三方对照**：

- [Device_protocol_v13.tex:L309-L316](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L309-L316) —— 文档字段表：f_start(8)/f_stop(8)/points(2)/IF_bandwidth(4)/cdbm_start(2)/Configuration(1)/Stages(2)/cdbm_stop(2)。注意 v13 把"配置位图"压缩为 1 字节、把 stage 编排独立成 2 字节位图（v12 是混在一个 16 位字里的）。
- [Device_protocol_v13.tex:L380-L398](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L380-L398) —— Stages 位图：P4/P3/P2/P1 各 3 位的"该端口何时被激励"，加上 3 位的 stage 总数（实际数 = 该值+1）。
- [Protocol.hpp:L155-L184](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L155-L184) —— 固件/GUI 共用的结构体定义。对照点：文档的 `cdbm_excitation_start` 对应 `cdbm_excitation_start`（注释"in 1/100 dbm"），文档 Stages 位图对应结构体尾部 6 个 `uint16_t` 位域；`stages:3` 语义即"stage 数减一"。
- [librevnadriver.cpp:L491-L523](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L491-L523) —— GUI 把硬件无关的 `DeviceDriver::VNASettings` 翻译成协议包：`portStageMapping[端口]=阶段号`（L491-L494）记录"哪个端口在第几阶段被激励"，随后 `stages = excitedPorts.size()-1`（L504，即"数减一"）、`port1Stage..port4Stage` 用 `find` 算出各端口阶段（L517-L520）、dBm 乘 100 变 cdbm（L502-L503）。

**VNADatapoint 三方对照**：

- [Device_protocol_v13.tex:L1290-L1295](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L1290-L1295) —— 字段表：三个变长数组，长度需从包总长推断（[L1299](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L1299) 明说"必须由总包长推断"）。
- [Device_protocol_v13.tex:L1302-L1313](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L1302-L1313) —— 描述掩码位域图（MSB 左：Stage 占 bit7-5、Ref bit4、P4-P1 bit3-0）。
- [Device_protocol_v13.tex:L1337-L1342](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L1337-L1342) —— 官方给出的完整双端口 6 读数掩码表（0x01/0x02/0x13/0x21/0x22/0x33）。
- [Device_protocol_v13.tex:L1348-L1375](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L1348-L1375) —— 文档手把手演示主机拼 S21 的五步流程，含数组长度公式与比值定义。
- [PacketConstants.h:L34-L47](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/PacketConstants.h#L34-L47) —— 代码对同一 payload 的度量：`DPNT_FREQ_LEN=8`、`DPNT_POW_LVL_LEN=2`、`DPNT_PNT_NUM_LEN=2`、实部/虚部各 4、描述 1；位偏移 `DPNT_CONF_P1_OFFSET=0 … DPNT_CONF_REF_OFFSET=4, DPNT_CONF_STAGE_OFFSET=5`——与文档位域图逐一吻合。
- [Protocol.hpp:L17-L23](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L17-L23) —— `Source` 枚举给出掩码"源"位的权值：Port1=0x01、Port2=0x02、Port3=0x04、Port4=0x08、Reference=0x10。
- [Protocol.hpp:L37-L46](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L37-L46) —— 掩码的**生成**端：`descr_values[i] = stage << DPNT_CONF_STAGE_OFFSET | sourceMask`，一行代码就是位域图的全部语义。
- [Protocol.hpp:L65-L79](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L65-L79) —— 掩码的**解析**端：`decode()` 第一行就是文档公式 `(size - 12) / 9` 的代码形态（9 = 4+4+1，由三个 `DPNT_*_LEN` 常量拼出）。
- [Protocol.hpp:L81-L97](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L81-L97) —— `getValue(stage, port, reference)`：按"stage 匹配 + 端口/参考位同时置位"检索读数，找不到返回 NaN——这正是 GUI 判断"该 S 参数本点缺失"的依据。
- [librevnadriver.cpp:L764-L796](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L764-L796) —— GUI 拼装 S 参数的循环：对 `portStageMapping` 每个激励端口取参考读数（L778），再遍历所有端口取入射读数（L780），两者都非 NaN 时执行 `m.measurements["Sij"] = input / ref`（L781-L784）——文档 L1348 起的示例流程在这里落地。

**SpectrumAnalyzerResult 三方对照**（也作为第 5 节综合实践的样板）：

- [Device_protocol_v13.tex:L1006-L1011](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L1006-L1011) —— 文档：4 个端口各 1 个 FLOAT（线性刻度，1.0 即 50Ω 上 1 mW = 0 dBm）+ UINT64 频率/时间（零扫宽时为时间）+ UINT16 点号。
- [Protocol.hpp:L495-L511](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L495-L511) —— 结构体 `port1..port4` 四个 float + `frequency/us` 匿名 union + `pointNum`，与文档逐字段一致（union 就是"零扫宽存时间"的实现手法，u3-l1 讲过）。
- [librevnadriver.cpp:L798-L810](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L798-L810) —— GUI 解包：只取 `PORT1`/`PORT2` 两个键——协议为多端口设备预留了 4 端口位宽，但官方 LibreVNA 硬件 `num_ports=2`，多余字段忽略。

顺带验证一个"文档位域图 ↔ C 位域声明反序"的完美案例——SpectrumAnalyzerSettings 的 Configuration（v13 文档 16 位、代码两个位域字节）：

- 文档 [Device_protocol_v13.tex:L926-L941](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L926-L941)：MSB→LSB 依次是保留/SM/syncMode/TGP/ASC/TGE/ARC/DFT/Detector/SID/Window；
- 代码 [Protocol.hpp:L470-L493](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L470-L493)：小端 GCC 下先声明的位域占低位，于是 `WindowType:2` 落在 bit1-0、`syncMaster:1` 落在 bit14——**与文档图完全互补对齐**。读这类代码时把结构体位域"从下往上"对着文档图"从右往左"看即可。

#### 4.2.4 代码实践

**实践目标**：用代码里的枚举与移位规则，手工复现文档给出的 6 个掩码，再外推一个文档没写的掩码——证明"文档表格不是背出来的，是算出来的"。

**操作步骤**：

1. 规则只有一条：\( mask = (stage \ll 5)\,|\,sourceMask \)，其中 sourceMask 按 [Protocol.hpp:L17-L23](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L17-L23) 取 Port1=1、Port2=2、Reference=0x10（可叠加）。
2. 逐个计算完整双端口扫描的 6 个读数掩码。
3. 外推：若做三阶段扫描（多机同步场景），阶段 2 上端口 1 接收机的掩码是多少？
4. 与 [Device_protocol_v13.tex:L1337-L1342](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L1337-L1342) 的表格逐行比对。

**需要观察的现象 / 预期结果**（纯静态计算，可自行验证）：

| 读数 | 计算 | 掩码 | 文档值 |
|---|---|---|---|
| 阶段0·端口1接收机 | (0<<5)\|0x01 | 0x01 | 0x01 ✓ |
| 阶段0·端口2接收机 | (0<<5)\|0x02 | 0x02 | 0x02 ✓ |
| 阶段0·参考（两端口位同置） | (0<<5)\|0x10\|0x01\|0x02 | 0x13 | 0x13 ✓ |
| 阶段1·端口1接收机 | (1<<5)\|0x01 | 0x21 | 0x21 ✓ |
| 阶段1·端口2接收机 | (1<<5)\|0x02 | 0x22 | 0x22 ✓ |
| 阶段1·参考 | (1<<5)\|0x13 | 0x33 | 0x33 ✓ |
| 阶段2·端口1接收机（外推） | (2<<5)\|0x01 | **0x41** | 文档未列 |

注意第三行：**参考接收机读数的掩码同时带两个端口位**——三接收机架构里参考通道是共享的，文档用"多端口位同置"表达"这一份参考读数对两个端口都有效"，而 GUI 侧 `getValue` 的"掩码包含全部指定位"匹配逻辑（[Protocol.hpp:L91-L93](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L91-L93)）恰好能吃到这种叠加位。

#### 4.2.5 小练习与答案

**练习 1**：为什么 S 参数的除法放在主机而不是设备端做？
**答案**：多设备同步时，端口接收机读数与参考接收机读数可能来自**不同的物理设备**，任何单台设备都看不到完整分子分母；只有汇聚了所有 VNADatapoint 的主机能配对求比值（文档 [L1346](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L1346) 原文理由；u3-l3 的 CompoundDriver 正是利用了这一点）。

**练习 2**：协议里 SpectrumAnalyzerResult 留了 4 个端口字段，GUI 为什么只填 PORT1/PORT2？
**答案**：协议面向整个产品家族（代码里 hardwareVersion 还有 0xD0/0xE0/0xFE/0xFF 等变体，[librevnadriver.cpp:L816-L826](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L816-L826)），位宽按最大公约数预留；具体设备实际端口数由 DeviceInfo 的 `num_ports` 报告（[librevnadriver.cpp:L728](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L728)），官方 LibreVNA 为 2。

**练习 3**：哪些包没有 payload？代码在哪里体现？
**答案**：Ack/Nack/ClearFlash/PerformFirmwareUpdate/SetIdle/各类 Request/SetTrigger/ClearTrigger/StopStatusUpdates/StartStatusUpdates/InitiateSweep。体现在 [Protocol.cpp:L114-L132](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.cpp#L114-L132) 的 `EncodePacket` switch：这些 case 共用"`// no payload`"分支，payload_size 保持 0，帧即 8 字节固定开销。

### 4.3 协议版本管理

#### 4.3.1 概念说明

协议靠什么不变成"两头各说各话"？三重机制：

1. **同源编译**：GUI 与固件编译**同一份** `Protocol.hpp`（u1-l3 讲过的 .pro 机制），两端结构体布局天然一致；
2. **版本号协商**：设备在 DeviceInfo 包里上报 `ProtocolVersion`，主机与自己的期望值比对；
3. **成文文档**：`.tex` 文档冻结每个版本的语义，供第三方实现与人类阅读。

但机制 3 是**手工维护**的，必然滞后。当前仓库的真实状态是：

- 代码：[Protocol.hpp:L13](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L13) `static constexpr uint16_t Version = 14;`
- 文档：最新只到 `Device_protocol_v13.tex`（标题 [L163](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L163)）。

**文档落后代码一个版本**。这不是缺陷事故，而是开源项目的常态——它恰好构成一个绝佳的教学案例：学会"以代码为准、以文档为地图"的核对方法（即本讲综合实践）。

此外，v13 引入了另一个维度的"版本"：**硬件版本**。ManualStatus/ManualControl/DeviceStatus/DeviceConfig 四种包的 payload 按 DeviceInfo 里上报的 `hardware_version` 取 union 的不同分支（文档 [L403](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L403) 起多处出现"Content varies according to the hardware version"）。**协议版本管时间轴，硬件版本管产品谱系**，两个正交维度不要混淆。

#### 4.3.2 核心流程

版本协商发生在连接建立后第一步：

```
连接成功
  │
  ├─▶ 发送 RequestDeviceInfo(15)
  │        ◀── DeviceInfo(5)：ProtocolVersion = 设备编译时的 Protocol::Version
  │
  ├─▶ GUI 比对 packet.info.ProtocolVersion ≠ Protocol::Version(14)?
  │        是 ─▶ 弹窗警告 + 建议固件升级（可一键打开升级对话框）
  │        否 ─▶ 继续
  │
  └─▶ 记录 hardwareVersion，后续按 union 分支解释状态包/配置包
```

要点：版本不匹配**不阻断**连接（尽量向前兼容），只强提醒——因为结构体布局差异往往只在尾部追加字段，旧读新/新读旧常能凑合，但固件升级是被强烈建议的。

#### 4.3.3 源码精读

**版本号的写入与读出**：

- [Protocol.hpp:L13](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L13) —— 代码期望的版本：14。
- [Device_protocol_v13.tex:L769](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L769) —— 文档规定 DeviceInfo 偏移 0 的 UINT16 即 ProtocolVersion（v13 时写 13），并注明"若上报其他值请查阅对应版本文档"。
- [librevnadriver.cpp:L705-L717](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L705-L717) —— GUI 的协商逻辑：读到 DeviceInfo 先存 `protocolVersion`，与 `Protocol::Version` 不等则弹问询框，用户同意即打开 `FirmwareUpdateDialog`。
- [Device_protocol_v13.tex:L752](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L752) —— 文档明确要求：设备枚举完成后**第一件事**就应是 RequestDeviceInfo，以确保使用正确的协议版本。

**v13 文档 → v14 代码的三处实质差异**（这是"文档考古"的现成答案，先自己找再看这里）：

| 差异点 | v13 文档 | v14 代码 | 代码证据 |
|---|---|---|---|
| SweepSettings 尾部 | 止于 `cdbm_excitation_stop`，payload 29 字节 | 追加 `dwell_time`（UINT16，驻留时间 µs），payload 31 字节 | [Protocol.hpp:L183-L184](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L183-L184) |
| DeviceInfo 尾部 | 止于 `NumPorts`（偏移 54），payload 55 字节 | 追加 `limits_maxDwellTime`（UINT16），payload 57 字节 | [Protocol.hpp:L218-L219](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L218-L219) |
| 包类型表 | 止于 32 号 InitiateSweep | 新增 33 号 PerformAction、34 号 ResetDeviceConfiguration | [Protocol.hpp:L607-L608](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L607-L608) |

GUI 已经在消费这些 v14 新元素（证明它们不是死代码）：

- [librevnadriver.cpp:L505-L511](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L505-L511) —— setVNA 填 `dwell_time`（秒转 µs 并夹在 0..65535）；
- [librevnadriver.cpp:L723](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L723) 与 [L736](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L736) —— 能力声明 `Feature::VNADwellTime`、上限 `limits_maxDwellTime`；
- [librevnadriver.cpp:L213-L228](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L213-L228) —— 菜单动作"Run Internal Alignment"发送 33 号 PerformAction 包（`Action::InternalAlignment`，见 [Protocol.hpp:L564-L571](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L564-L571)）。

**v12 → v13 的协议大迁徙**（理解"两层协议划分"的钥匙）：

| 维度 | USB_protocol_v12 | Device_protocol_v13 |
|---|---|---|
| 定位 | 协议=USB | 传输无关"设备协议"：USB + 以太网 TCP 19544/19545 + SSDP 发现（[v13:L186-L205](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L186-L205)） |
| USB VID | 0x0483（[v12:L171](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/USB_protocol_v12.tex#L171)） | 0x1209（[v13:L177](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L177)，与固件 usbd_desc.c 一致） |
| 23/24 号包 | RequestAcquisitionFrequencySettings / AcquisitionFrequencySettings | 更名 RequestDeviceConfig / DeviceConfig，payload 改为按硬件版本的 union |
| Manual*/DeviceStatus | 名称带 V1 后缀，单一硬件 | 去后缀，按 hardware_version 分支 |
| SweepSettings 位图 | 16 位 Configuration 混装 stage | 8 位 Configuration + 独立 16 位 Stages，扩展到 4 端口 |
| SpectrumAnalyzerResult | 2 端口、单位"mW" | 4 端口、线性电压（1.0 = 50Ω 上 1 mW） |
| DeviceInfo | 无 NumPorts | 偏移 54 增加 NumPorts（[v13:L786](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L786)） |
| VNADatapoint | 有格式定义 | 额外补充了主机拼 S21 的示例流程（[v13:L1348-L1375](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L1348-L1375)） |

#### 4.3.4 代码实践

**实践目标**：亲手完成一次"文档考古"——只用肉眼与 grep，找出 v13 文档没写、v14 代码却存在的协议元素。

**操作步骤**：

1. 打开 [Device_protocol_v13.tex:L251-L281](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L251-L281) 的包类型表，记下最大编号。
2. 打开 [Protocol.hpp:L573-L609](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L573-L609) 的 `PacketType` 枚举，逐项与表格对号，找出表格没有的编号。
3. 对 SweepSettings / DeviceInfo：把文档字段表的最后几个偏移与结构体尾部声明对齐，找出"结构体比文档多出来的尾部字段"。
4. 对每个发现，在 GUI 代码里 grep 该字段名，确认它真的被使用（防止是预留死代码）。

**需要观察的现象**：文档表格止于 32 号；枚举多出 33/34；两个结构体各多一个尾部 UINT16；GUI 能 grep 到 dwell_time、limits_maxDwellTime、PerformAction 的使用点。

**预期结果**：与 4.3.3 的差异表一致（三处差异 + 使用证据）。全程静态阅读即可完成，无需硬件、无需编译。

#### 4.3.5 小练习与答案

**练习 1**：设备上报 ProtocolVersion=13 而 GUI 期望 14，连接还能用吗？会发生什么？
**答案**：能用。GUI 只弹警告框建议升级固件（[librevnadriver.cpp:L708-L717](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L708-L717)），不中断。能凑合的原因是 v13→v14 的变化都是**尾部追加**：旧设备发的 DeviceInfo 少 2 字节，union 里多出的两字节读到的是未初始化/越界边缘数据——恰好 union 尺寸按最大分支分配，不至于崩，但驻留时间上限等新功能不可用。

**练习 2**：协议版本（ProtocolVersion）与硬件版本（hardware_version）各管什么？
**答案**：协议版本管**时间轴**——同一产品线上固件/GUI 协议格式的演进，决定包的布局；硬件版本管**产品谱系**——LibreVNA 家族里不同硬件（代码里有 1/0xD0/0xE0/0xFE/0xFF 等，见 [librevnadriver.cpp:L816-L826](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L816-L826)）各自有哪些寄存器/接收机/状态量，决定 Manual*、DeviceStatus、DeviceConfig 等 union 包取哪个分支。二者正交。

**练习 3**：如果让你为 v14 补写文档，最小改动是什么？
**答案**：不必重写全文。在 v13 基础上：① SweepSettings 字段表追加一行"offset 29, len 2, UINT16, dwell_time"；② DeviceInfo 追加"offset 55, len 2, UINT16, MaxDwellTime"；③ 包类型表追加 33 PerformAction（含 Action 枚举与 128 字节附加信息，见 [Protocol.hpp:L568-L571](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L568-L571)）与 34 ResetDeviceConfiguration 两行；④ 标题与 ProtocolVersion 说明改为 14。尾部追加式演进正是这套文档能"增量维护"的原因。

## 5. 综合实践

**任务**（本讲规格指定的核心实践）：任选一个**测量数据包**类型，在**文档（.tex 源码）、固件（Protocol.hpp / PacketConstants.h）、GUI（librevnadriver.cpp）**三处各找到它的定义，写一段一致性核对记录，覆盖字段、大小、端序三个维度。

下面给出 **SpectrumAnalyzerResult 的完整核对记录作为样板**，然后请你用同样的格式独立完成 **VNADatapoint** 的核对。

### 5.1 样板：SpectrumAnalyzerResult 一致性核对记录

| 核对项 | 文档（Device_protocol_v13.tex） | 固件/GUI 共用代码（Protocol.hpp） | GUI 消费（librevnadriver.cpp） | 结论 |
|---|---|---|---|---|
| 类型编号 | 14（[L263](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L263)） | `SpectrumAnalyzerResult = 14`（[L588](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L588)） | switch 分支 `case SpectrumAnalyzerResult`（[L798](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L798)） | 一致 |
| 端口字段 | 偏移 0/4/8/12 各 4 字节 FLOAT，4 端口（[L1006-L1009](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L1006-L1009)） | `float port1..port4`（[L496-L499](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L496-L499)） | 仅取 port1/port2 填 `PORT1`/`PORT2`（[L806-L807](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L806-L807)） | 一致；GUI 按设备实有 2 端口裁剪 |
| 频率/时间 | 偏移 16，UINT64，零扫宽时为起始以来的时间（[L1010](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L1010)） | 匿名 union `frequency` / `us`（[L500-L509](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L500-L509)） | `zerospan ? m.us : m.frequency`（[L801-L805](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L801-L805)） | 一致；union 即"一字段两义"的实现 |
| 点号 | 偏移 24，UINT16（[L1011](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L1011)） | `uint16_t pointNum`（[L510](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L510)） | `m.pointNum = packet.spectrumResult.pointNum`（[L800](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L800)） | 一致 |
| payload 大小 | 4×4+8+2 = 26 字节 | sizeof = 26（pack(1) 下按声明累加） | — | 一致（4.1.5 练习 3 已算帧总长 34） |
| 端序 | 全协议小端（[L220](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L220)） | `memcpy` 整体序列化，无逐字段换序（[Protocol.cpp:L155](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.cpp#L155)） | 依赖两端同为小端主机 | 一致（隐含约束：ARM/x86 均小端） |
| 单位语义 | 线性，1.0 = 50Ω 上 1 mW（0 dBm）（[L1006](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/Device_protocol_v13.tex#L1006)） | 代码不注释单位（约定在文档） | 存入 `SAMeasurement.measurements`（线性，u3-l1 讲过 1.0=0dBm） | 一致 |

### 5.2 你的任务：对 VNADatapoint 产出同款记录

按下表逐格填写（文献定位已在 4.2.3 给全，这里只留空格）：

| 核对项 | 文档（v13 .tex 行号） | 代码（Protocol.hpp / PacketConstants.h 行号） | GUI（librevnadriver.cpp 行号） | 结论 |
|---|---|---|---|---|
| 类型编号 27 | 待填 | 待填 | 待填 | |
| 头部三字段（12 字节） | 待填 | 待填 | 待填 | |
| 三个变长数组的长度约定 | 待填（公式） | 待填（decode 第一行） | — | |
| 描述掩码位分配 | 待填 | 待填（两处：枚举 + 常量） | 待填（getValue 匹配逻辑） | |
| payload 大小（以双端口 6 读数为例） | 待填 | 待填（requiredBufferSize） | — | |
| CRC 约定 | 待填 | 待填（Encode/Decode 两处） | — | |
| 端序 | 待填 | 待填 | — | |

**操作步骤**：

1. 先自己填，再与 4.1.3、4.2.3 的行号对照订正；
2. 对"payload 大小"一行实际算一次：\( 12 + 6 \times 9 = 66 \) 字节 payload，帧总长 \( 66 + 8 = 74 \)，长度字段小端为 `4A 00`；
3. 有条件的话（需要硬件，**待本地验证**）：在 GUI 设备菜单打开 "View Packet Log"（动作注册见 [librevnadriver.cpp:L234-L239](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnadriver.cpp#L234-L239)），启动一次 VNA 扫描，把抓到的 VNADatapoint 字节与你推算的 74 字节帧比对，重点看第 4 字节（类型 27 = 0x1B）和末 4 字节（应全 0）。

**预期结果**：三处定义在字段、大小、端序上完全一致；唯一"文档没写全"的是 v14 新增内容，而 VNADatapoint 本身 v13 与代码没有布局差异（变长结构稳定）。

## 6. 本讲小结

- **两份文档两层协议**：`USB_protocol_v12` 是纯 USB 时代的历史快照（VID 0x0483）；`Device_protocol_v13` 是现行的传输无关"设备协议"（VID 0x1209 + 以太网 TCP 19544/19545 + SSDP）。帧格式两版完全相同：0x5A + 2 字节小端总长 + 1 字节类型 + 变长 payload + 4 字节 CRC32，固定开销 8 字节。
- **端点分工**：0x01 下行协议数据、0x81 上行协议数据、0x82 独走 ASCII 调试日志；VNADatapoint 是唯一 CRC 恒为 0 的包，为吞吐牺牲校验（CRC 约 18 µs）。
- **命令语义四类**：设置类（兼模式切换）、请求类（空载荷触发应答）、数据类（高频上行）、维护类；设备对命令回 Ack/Nack，主机从不 Ack。SweepSettings 的 stage 编排 + VNADatapoint 的"接收机读数 + 掩码"把 S 参数拼装留给主机，这是多机同步架构的必然选择。
- **掩码是算出来的**：\( mask = stage \ll 5 \,|\, sourceMask \)，Source 枚举 Port1=0x01…Reference=0x10 可叠加；参考接收机读数带多个端口位是三接收机共享架构的标志。
- **协议版本三重保险**：GUI/固件同源编译同一份 Protocol.hpp、DeviceInfo 的 ProtocolVersion 协商（不等只警告不阻断）、.tex 成文文档；当前文档停在 v13 而代码已是 v14（多了 dwell_time、limits_maxDwellTime、包类型 33/34），"文档落后代码"提醒我们核对要以代码为准。
- **正交的两个版本维度**：ProtocolVersion 管时间轴（包布局演进），hardware_version 管产品谱系（Manual*/DeviceStatus/DeviceConfig 按 union 分支）。

## 7. 下一步学习建议

- **下一讲 u4-l3（GUI 侧协议实现）**：本讲看懂了"包长什么样"，下一讲跟踪 GUI 如何构造、发送、排队、解析这些包——`SendPacket` 的单包在途队列、`DecodeBuffer` 的线程切换都在那一讲展开。
- **回头补 u3-l2**：如果你对 USB/TCP 两条传输通道如何承载同一套包还有疑问，重读 LibreVNADriver 的公共层/传输层拆分会让本讲的帧格式落地更稳。
- **向前看 u5 与 u6**：设备收到 SweepSettings 之后发生什么，是单元 5（固件模式状态机）的故事；采样与 DFT 的位级时序在单元 6，配套文档是同目录的 `FPGA_protocol.tex`。
- **阅读建议**：直接读 `Documentation/DeveloperInfo/Device_protocol_v13.pdf`（编译版）通读一遍包类型表，比从代码反推快得多；之后把 `Protocol.hpp` 当作"唯一真相源"放在手边。
