# 设备端通信架构：Communication 与 Protocol

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `Communication` 命名空间在固件中的角色：它是固件中**唯一**的协议入口与出口，USB 字节流进、`Protocol::PacketInfo` 出。
2. 画出一条命令从 USB 端点字节到 `VNA::Setup()` 等业务函数的完整路径，并区分其中「中断上下文」与「任务上下文」两段。
3. 对照 `Protocol.hpp` 识别二进制帧的 C 结构布局：帧头、长度、类型、payload、CRC32 五段式，以及 `PacketInfo` 中「类型 + union」的设计。
4. 解释 `PacketConstants.h` 中字段级常量与 `usb.c` 中三个 USB 端点的约定。
5. 澄清一个常见误解：**固件里没有 SCPI**。SCPI 文本协议止步于 GUI，GUI 与固件之间永远走二进制包——GUI 是两套协议之间的翻译网桥。

本讲是第 4 单元的第一讲，视角从上一单元的「GUI 侧驱动」（u3-l2）切换到「设备固件侧」，两端在 `DecodeBuffer`/`EncodePacket` 这对函数处汇合。

## 2. 前置知识

### 2.1 帧（frame）与粘包/半包

USB 批量传输一次给固件一块字节（全速模式下最多 64 字节）。协议数据是「一条条消息」嵌在这块字节里。于是必须回答两个问题：

- 一次收到的字节里可能含**多条**消息（粘包）；
- 一条消息也可能**跨**两次接收才凑齐（半包）。

解决方案是**自描述帧**：每条消息自带帧头、总长度和校验，接收方在字节流里自己找边界。你在 u3-l2 已经见过 GUI 侧的 `DecodeBuffer` 怎么做这件事——本讲会看到固件侧调用的是**同一份代码**。

### 2.2 CRC32 校验

CRC（循环冗余校验）把一段字节映射成一个 32 位指纹，用于检测传输误码。LibreVNA 用的是标准反射 CRC-32，生成多项式为：

\[ g(x) = x^{32} + x^{26} + x^{23} + x^{22} + x^{16} + x^{12} + x^{11} + x^{10} + x^{8} + x^{7} + x^{5} + x^{4} + x^{2} + x + 1 \]

其反射系数写成十六进制常数就是代码里的 \( \texttt{0xEDB88320} \)。初值与最终值都取反（代码里的 `crc = ~crc` 和 `return ~crc`）。

### 2.3 `#pragma pack(1)`、union 与字节序

- **pack(1)**：C 编译器默认会在结构体成员间插入填充字节以对齐；`#pragma pack(push, 1)` 关掉填充，让 `sizeof` 与字段逐字节相加一致——这是「结构体直接 memcpy 进帧」的前提。
- **union**：`PacketInfo` 里十几种 payload 结构共用同一段内存，省去为每种包各开一块缓冲。代价是同一时刻只有一种有意义。
- **字节序**：STM32（ARM Cortex-M4）和常见 PC（x86/ARM）都是小端，所以「把 uint16_t/uint32_t 直接 memcpy 进帧」隐含了一份两端字节序一致的契约。

### 2.4 FreeRTOS 的 ISR→任务交接

固件里 USB 收包发生在**中断上下文**。FreeRTOS 的惯例是：中断里只做最少的事（存数据、`xTaskNotifyFromISR` 唤醒任务），真正的业务逻辑在任务里跑。本讲的通信路径正是这个模式的教科书示例。

### 2.5 USB 批量端点

USB 设备通过「端点（endpoint）」收发数据。批量端点（bulk）适合大数据量、无固定时序的传输。端点地址的最高位区分方向：`0x8x` 是 IN（设备→主机），`0x0x` 是 OUT（主机→设备）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `Software/VNA_embedded/Application/Communication/Communication.h/.cpp` | 固件通信入口/出口：字节累积、拆帧、回调分发、发包 |
| `Software/VNA_embedded/Application/Communication/Protocol.hpp` | 协议「单一事实来源」：所有包类型与 payload 结构定义 |
| `Software/VNA_embedded/Application/Communication/Protocol.cpp` | `EncodePacket`/`DecodeBuffer`/`CRC32` 的实现，**被 GUI 一起编译** |
| `Software/VNA_embedded/Application/Communication/PacketConstants.h` | 帧字段偏移/长度、VNADatapoint 字段、固件分块大小等常量 |
| `Software/VNA_embedded/Application/Drivers/USB/usb.c` | USB 类驱动：三个端点、收包回调、发送环形 FIFO、日志通道 |
| `Software/VNA_embedded/Application/App.cpp` | 通信的使用方：注册回调、在任务里按包类型分发到各业务模块 |
| `Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro` | 证据：GUI 把固件的 `Protocol.hpp/.cpp` 编进自己工程 |

## 4. 核心概念与源码讲解

### 4.1 Communication 收发框架

#### 4.1.1 概念说明

`Communication` 不是一个类，而是一个**命名空间 + 一组静态函数**——整个固件只需要一条通信通道，没必要实例化。它的职责边界非常克制：

- **收**：把 USB 驱动丢过来的任意长度字节块，拆成一条条完整的 `Protocol::PacketInfo`，交给回调。
- **发**：把 `PacketInfo` 编码成字节，塞进 USB 发送 FIFO。

它**不理解任何业务语义**——不知道什么是扫描、什么是校准。业务分发完全交给注册回调的一方（`App.cpp`）。这种「传输与语义分离」让你可以替换业务层而不动通信层。

#### 4.1.2 核心流程

接收路径（中断上下文 → 任务上下文）：

```text
主机 → USB OUT 端点 0x01（最多 64 字节/次，中断上下文）
  └─ usb.c: USBD_Class_DataOut → cb(...)              // cb 即 communication_usb_input
       └─ Communication::Input(buf, len)
            ├─ 追加进 inputBuffer[1024]，inputCnt += len   // 半包在这里等下一次
            └─ 循环: Protocol::DecodeBuffer(inputBuffer, inputCnt, &packet)
                 ├─ 未凑齐完整帧 → 返回 0，剩余字节留在缓冲区
                 ├─ 帧头前的杂散字节 → 跳过（重同步）
                 └─ 完整帧且 CRC 正确 → callback(packet)
                      └─ App.cpp: USBPacketReceived      // 只做两件事
                           1. recv_packet = p            // 拷贝
                           2. xTaskNotifyFromISR(FLAG_USB_PACKET)  // 唤醒任务

App_Process 任务被唤醒（任务上下文）
  └─ switch(recv_packet.type)
       ├─ SweepSettings      → VNA::Setup(...)
       ├─ SpectrumAnalyzerSettings → SA::Setup(...)
       ├─ Generator          → Generator::Setup(...)
       └─ ... 每个分支末尾几乎都回 Ack
            └─ Communication::Send / SendWithoutPayload
                 └─ Protocol::EncodePacket → usb_transmit
                      └─ 6144 字节环形 FIFO → USB IN 端点 0x81
```

注意 `do...while(handled_len > 0)` 循环正是处理**粘包**的手段：一次输入可能拆出多条帧；而「剩余字节前移」是处理**半包**的手段。

#### 4.1.3 源码精读

先看模块的静态状态与接口（[Software/VNA_embedded/Application/Communication/Communication.cpp:8-15](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Communication.cpp#L8-L15)）：`inputBuffer` 是 1024 字节的接收累积缓冲，`callback` 是唯一的分发目标，类型在头文件里定义（[Software/VNA_embedded/Application/Communication/Communication.h:11-17](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Communication.h#L11-L17)）——一个接受 `PacketInfo` 的普通函数指针。

接收的核心是 `Input()`（[Software/VNA_embedded/Application/Communication/Communication.cpp:18-43](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Communication.cpp#L18-L43)）：

```cpp
void Communication::Input(const uint8_t *buf, uint16_t len) {
    if (inputCnt + len < sizeof(inputBuffer)) {
        memcpy(&inputBuffer[inputCnt], buf, len);
        inputCnt += len;
    }
    Protocol::PacketInfo packet;
    uint16_t handled_len;
    do {
        handled_len = Protocol::DecodeBuffer(inputBuffer, inputCnt, &packet);
        if (handled_len == inputCnt) {
            inputCnt = 0;                      // 缓冲全部消费完
        } else {
            uint16_t remaining = inputCnt - handled_len;
            memmove(inputBuffer, &inputBuffer[handled_len], remaining);
            inputCnt = remaining;              // 剩余字节前移，等待续包
        }
        if(packet.type != Protocol::PacketType::None) {
            if(callback) {
                callback(packet);              // 完整帧才上报
            }
        }
    } while (handled_len > 0);
}
```

三个细节值得咀嚼：缓冲满时新数据被**静默丢弃**（guard 不成立就走下去，没有 else 分支）；`PacketType::None` 表示「这次没有完整帧」，不上报；`memmove` 而非 `memcpy`，因为源和目标重叠。

发送则薄得多（[Software/VNA_embedded/Application/Communication/Communication.cpp:45-62](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Communication.cpp#L45-L62)）：在栈上开 512 字节输出缓冲，`EncodePacket` 编码后交给 `usb_transmit`。被注释掉的 CDC 代码是历史遗留——早年曾尝试用 USB 串口类通信。

C 与 C++ 的桥在 [Software/VNA_embedded/Application/Communication/Communication.cpp:64-66](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Communication.cpp#L64-L66)：USB 驱动是纯 C 的，只能回调 C 函数，所以 `communication_usb_input` 用 `extern "C"`（声明见 [Communication.h:21-26](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Communication.h#L21-L26)）包一层再转发给 C++ 的 `Input`。

回调注册与 USB 初始化在 `App_Init`（[Software/VNA_embedded/Application/App.cpp:59-71](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L59-L71)）：`usb_init(communication_usb_input)` 把 C 回调交给 USB 栈，`Communication::SetCallback(USBPacketReceived)` 把分发目标定成 App 的处理函数。

回调本体刻意做到最简（[Software/VNA_embedded/Application/App.cpp:46-51](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L46-L51)）：

```cpp
static void USBPacketReceived(const Protocol::PacketInfo &p) {
    recv_packet = p;                    // 中断上下文：只拷贝
    BaseType_t woken = false;
    xTaskNotifyFromISR(handle, FLAG_USB_PACKET, eSetBits, &woken);
    portYIELD_FROM_ISR(woken);          // 若唤醒了更高优先级任务，立即切换
}
```

为什么不在回调里直接 `switch` 处理？因为它运行在 USB 中断里——在那里跑 `VNA::Setup()`（会去配置 PLL、等 FPGA）会长时间占住中断，破坏实时性。真正的分发在 `App_Process` 的任务循环里（[Software/VNA_embedded/Application/App.cpp:119-131](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L119-L131)）：`xTaskNotifyWait` 睡到有事件，再按 `recv_packet.type` 逐 case 处理，例如 `SweepSettings` 分支调 `VNA::Setup(recv_packet.settings)` 并回 Ack。

还有一个巧妙的小机制——Ack 抑制（[Software/VNA_embedded/Application/Communication/Communication.cpp:68-80](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Communication.cpp#L68-L80)）：`BlockNextAck()` 让紧随其后的一次 Ack「记账注销」而不真正发送。它配合 [App.cpp:329-334](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L329-L334) 使用：当 `HW::TimedOut()` 发现通信超时（比如主机短暂卡顿），固件会把**最后一条测量命令重放**给自己（`USBPacketReceived(last_measure_packet)`）以重启操作——重放会再次走到「回 Ack」的分支，但对主机来说这条命令早已确认过，于是先用 `BlockNextAck()` 把多余的 Ack 吞掉。

#### 4.1.4 代码实践：跟踪一次收发的「上下文切换点」

1. **实践目标**：在源码层面确认收包路径上「中断上下文」和「任务上下文」的分界线，理解为什么解码在 ISR、业务在任务。
2. **操作步骤**：
   - 打开 `Software/VNA_embedded/Application/Drivers/USB/usb.c`，找到 `USBD_Class_DataOut`（[usb.c:206-214](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/USB/usb.c#L206-L214)），确认它调用 `cb(...)` 后立刻 `USBD_LL_PrepareReceive` 挂起下一次接收。再向上追：该函数只被 HAL 的 `HAL_PCD_IRQHandler` 调用，而后者在 `USB_LP_IRQHandler`/`USB_HP_IRQHandler`（[usb.c:292-299](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/USB/usb.c#L292-L299)）里，即中断服务程序。
   - 沿 `cb` 的赋值（`usb_init`，[usb.c:226-235](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/USB/usb.c#L226-L235)）→ `App_Init` 的 `usb_init(communication_usb_input)` → `Communication::Input` → `callback(packet)` → `USBPacketReceived` 画出完整链条。
   - 用两种颜色标注：中断里执行的部分（`Input`/`DecodeBuffer`/拷贝/通知）与任务里执行的部分（`App_Process` 的 switch）。
3. **需要观察的现象**：纯源码走读，无运行现象；重点观察 `USBD_Class_DataOut` 的函数体有多短——ISR 里每一行代码都在拖慢所有低优先级中断。
4. **预期结果**：得到一张标注了上下文边界的调用链图，分界点是 `xTaskNotifyFromISR`。若想用真机验证（在 `Input` 与 `App_Process` 各加一行 `LOG_DEBUG`，观察日志是否成对出现），需要按 u1-l4 搭好 STM32CubeIDE 工具链，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果主机连续快速发送命令，`inputBuffer`（1024 字节）被填满会发生什么？之后如何恢复？
**答案**：`Input` 的 guard `inputCnt + len < sizeof(inputBuffer)` 不成立，新数据被静默丢弃，没有任何错误回报。恢复依赖 `DecodeBuffer` 的重同步能力：缓冲里残留的字节被逐帧消费后，只要后续数据里再次出现帧头 `0x5A` 且长度、CRC 合法，解析就回到正轨。极端情况下可能丢掉若干条命令（主机等 Ack 超时后会重发）。

**练习 2**：为什么 `USBPacketReceived` 里是 `recv_packet = p`（拷贝）而不是存指针？
**答案**：回调参数 `p` 指向的是 `Input` 栈上的局部变量 `packet`（对 `VNADatapoint` 还指向堆对象），回调返回后即失效；而消费发生在另一个上下文（App 任务）且时机不确定，必须把值拷入静态的 `recv_packet` 才安全。

**练习 3**：`Communication::Send` 里 `outputBuffer` 只有 512 字节，最大合法包能塞下吗？
**答案**：能。`EncodePacket` 在 `payload_size + PCKT_EXCL_PAYLOAD_LEN > destsize` 时直接返回 0 拒绝编码（防溢出）；实际最大的常规包远小于 512，而 32 值的 `VNADatapoint` 共 308 字节（见 4.3 的计算）也在范围内。

### 4.2 Protocol.hpp 帧定义

#### 4.2.1 概念说明

`Protocol.hpp` 是整个 USB 协议的**单一事实来源**：帧内每种消息对应一个 C 结构体，外加一个枚举给每个结构体编号。它最不寻常的地方在于**同一份文件被两端编译**——GUI 工程文件里明晃晃地写着（[Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro:2-3](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L2-L3) 把 `Protocol.hpp`/`PacketConstants.h` 列入头文件，[LibreVNA-GUI.pro:175](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L175) 把 `Protocol.cpp` 列入源文件）。于是「两端结构布局是否一致」这个分布式系统里最经典的坑，被编译器天然消灭：改一处，两端一起变。

文件顶部还有协议版本号（[Software/VNA_embedded/Application/Communication/Protocol.hpp:13](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L13)）`static constexpr uint16_t Version = 14;`——它会随 `DeviceInfo` 包报告给 GUI，用于连接时的兼容性检查。

帧格式定义在 `Protocol.cpp` 的注释里（[Software/VNA_embedded/Application/Communication/Protocol.cpp:5-12](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.cpp#L5-L12)）：

```text
+--------+---------+--------+=====================+==========+
| 0x5A   | len (2B, 小端) | type(1B) |  payload (变长)  | CRC32(4B) |
+--------+---------+--------+=====================+==========+
|<-------------- len = 4 字节头 + payload + 4 字节 CRC -------------->|
```

各字段的偏移与长度不是魔法数字，全部集中在 `PacketConstants.h`（下一节展开）。

#### 4.2.2 核心流程

**解码**（`DecodeBuffer`，[Protocol.cpp:28-93](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.cpp#L28-L93)）是一个**每次调用最多拆一帧**的函数，返回本次消费的字节数：

```text
DecodeBuffer(buf, len, info):
  1. 从头扫描直到找到 0x5A          —— 重同步：丢弃帧头前的杂散字节
  2. 剩余 < 4 字节?                  —— 头都不完整，返回，等更多数据
  3. 读 length；length < 8 或 > 2*sizeof(PacketInfo)? —— 疑似误码，丢 1 字节重试
  4. 剩余 < length?                  —— 帧未收全，返回，等更多数据
  5. 读 type 与帧尾 CRC32
     ├─ 普通包: 重算 CRC32 比对，不符则丢掉帧头字节重同步
     │           相符 → memcpy(info, &data[3], length-7)   ← 类型+payload 整体拷入结构体
     └─ VNADatapoint: 要求 CRC 恒为 0（见下），在堆上 new VNADatapoint<32> 再逐字段 decode
  6. 返回 data - buf + length        —— 本帧总消费量
```

第 5 步的 `memcpy(info, &data[PCKT_TYPE_OFFSET], length - 7)` 是整个协议最「胆大」也最优雅的一步：它把帧里「类型字节 + payload」**原样拷进 `PacketInfo` 结构体的对应位置**。这之所以成立，全靠三件事同时为真：`#pragma pack(1)` 消除填充、两端小端字节序、两端编译的是**同一份头文件**。

**编码**（`EncodePacket`，[Protocol.cpp:95-161](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.cpp#L95-L161)）是镜像过程：switch 按类型查 payload 大小（无 payload 的命令类型归在一组，payload 记 0）→ 写 `0x5A` 与总长 → `memcpy(&dest[3], &packet, payload_size + 1)` 把「类型 + 结构体」整体拷出 → 算 CRC32 写到帧尾。

**一个刻意的例外**：`VNADatapoint`（VNA 测量数据点）**跳过 CRC**，帧尾恒写 0（[Protocol.cpp:147-152](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.cpp#L147-L152)），代码注释直言：算一次 CRC 约 18µs，是编码发送一个数据点的主要耗时。这是典型的**吞吐换校验**取舍——测量点是唯一的高吞吐流（一次扫描成百上千点），而 USB 批量传输在链路层本就自带差错校验，应用层 CRC32 属于额外的端到端保险。解码端也用同一份代码，自然知道「见到 type 27 就检查 CRC 位是否为 0」，约定永不错位。

#### 4.2.3 源码精读

**包类型枚举**是协议的「目录页」（[Software/VNA_embedded/Application/Communication/Protocol.hpp:573-609](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L573-L609)）。几个关键取值：

```cpp
enum class PacketType : uint8_t {
    None = 0,
    //Datapoint = 1, // Deprecated, replaced by VNADatapoint
    SweepSettings = 2,
    ...
    DeviceInfo = 5,
    Ack = 7,
    ...
    RequestDeviceInfo = 15,
    ...
    VNADatapoint = 27,
    ...
    ResetDeviceConfiguration = 34,
};
```

规律很清晰：**命令与其应答各自占用一个编号**。例如 `RequestDeviceInfo = 15`（主机→设备的无 payload 命令）与 `DeviceInfo = 5`（设备→主机、携带 `DeviceInfo` 结构体的应答）成对。`Datapoint = 1` 被注释废弃、编号不复用，是协议演进的兼容性礼貌——老编号永远留坟，新含义用新号。

**`PacketInfo`：类型 + union**（[Software/VNA_embedded/Application/Communication/Protocol.hpp:611-635](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L611-L635)）：

```cpp
using PacketInfo = struct _packetinfo {
    PacketType type;
    union {
        SweepSettings settings;
        ReferenceSettings reference;
        GeneratorSettings generator;
        DeviceStatus status;
        DeviceInfo info;
        ...
        VNADatapoint<32> *VNAdatapoint;   // 唯一的指针成员
    };
};
```

除了 `VNAdatapoint` 是指针（变长数据不适合值语义；注释写明解码时堆分配、由调用者负责释放），其余成员都是值类型的结构体。整个 union 被 `#pragma pack(push, 1)`（[Protocol.hpp:15](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L15) 与 [L637](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L637) 成对包裹）包住，无任何填充。

**典型 payload 之一：`SweepSettings`**（[Software/VNA_embedded/Application/Communication/Protocol.hpp:155-184](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L155-L184)），GUI 下发一次 VNA 扫描的全部设置：

```cpp
using SweepSettings = struct _sweepSettings {
    uint64_t f_start;
    uint64_t f_stop;
    uint16_t points;
    uint32_t if_bandwidth;
    int16_t cdbm_excitation_start;   // 单位 1/100 dBm
    uint8_t standby:1;
    ...                              // 若干单比特标志 + 2bit 同步模式
    uint16_t stages:3;               // 各端口的激励 stage 编码
    ...
    int16_t cdbm_excitation_stop;
    uint16_t dwell_time;             // 单位 µs
};
```

注意两个工程习惯：物理量用**定点小整数**传输（功率用 1/100 dBm 的 int16，驻留时间用 µs），避免浮点序列化的精度与格式问题；位域把十几个开关量压进三四个字节。

**典型 payload 之二：`DeviceInfo`**（[Software/VNA_embedded/Application/Communication/Protocol.hpp:200-220](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L200-L220)）：协议版本、固件三段版本号、硬件版本字符，以及一整组 `limits_*`（最小/最大频率、IF 带宽、点数、功率、RBW……）。它就是 u3-l1 讲过的 `DeviceDriver::Info::Limits` 的原始来源——GUI 拿到这个包后就知道这台设备「能做什么」，从而完成能力协商。

**CRC32 实现**（[Software/VNA_embedded/Application/Communication/Protocol.cpp:15-26](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.cpp#L15-L26)）是教科书式的逐位反射算法：初值取反、每字节异或进低位、8 次右移迭代、最终再取反。嵌入式侧不查表（省 RAM），用时间换空间。

#### 4.2.4 代码实践：在 PC 上亲手编码一帧

这是本讲的主实践。`Protocol.cpp` 只依赖 `<cstdint>`/`<cstring>`，**不需要任何硬件和 Qt** 就能在你的电脑上编译运行——这正是「协议代码两端同源」带来的可测性红利。

1. **实践目标**：亲手生成一个真实帧的字节，逐字节核对 5 段式布局；同时验证 `SweepSettings` 的推算尺寸。
2. **操作步骤**：

   在仓库**外**的任意目录新建 `protodemo.cpp`（**示例代码**，非项目文件；请勿写进仓库）：

   ```cpp
   // 示例代码：编码一个 SweepSettings 帧并十六进制打印
   #include "Protocol.hpp"
   #include <cstdio>

   int main() {
       printf("sizeof(SweepSettings) = %zu\n", sizeof(Protocol::SweepSettings));
       printf("sizeof(PacketInfo)    = %zu\n", sizeof(Protocol::PacketInfo));

       Protocol::PacketInfo p;
       p.type = Protocol::PacketType::SweepSettings;
       p.settings = {};
       p.settings.f_start = 100000000;      // 100 MHz... 以 Hz 计? 见 Protocol.hpp 注释, 此处仅示意
       p.settings.f_stop  = 6000000000;
       p.settings.points  = 501;
       p.settings.if_bandwidth = 1000;

       uint8_t buf[512];
       auto len = Protocol::EncodePacket(p, buf, sizeof(buf));
       printf("frame length = %u\nhex:", len);
       for (int i = 0; i < len; i++) {
           printf(" %02x", buf[i]);
       }
       printf("\n");
       return 0;
   }
   ```

   在仓库根目录执行（g++ 或 clang++，需 C++17）：

   ```bash
   g++ -std=c++17 -I Software/VNA_embedded/Application/Communication \
       protodemo.cpp Software/VNA_embedded/Application/Communication/Protocol.cpp \
       -o protodemo
   ./protodemo
   ```

3. **需要观察的现象**：输出的十六进制序列。
4. **预期结果**：第一个字节是 `5a`（帧头常量 `PCKT_HEADER_DATA`）；第 2–3 字节是小端总长度，等于 \( 4 + \text{payload} + 4 \)；第 4 字节是 `02`（`SweepSettings` 的枚举值）；随后是 payload，其中能辨认出 `f_start`/`f_stop` 的小端字节（例如 501 = `f5 01`）；最后 4 字节是 CRC32。按结构体逐字段累加（8+8+2+4+2+1+2+2+2）可推算 `sizeof(SweepSettings) = 31`、帧总长 39——请用程序输出验证这个推算，具体 CRC 字节值**待本地验证**。
5. **进阶**（可选）：把编码出的字节再喂给 `Protocol::DecodeBuffer`，检查能否还原出 `points = 501`。编码→解码闭环成功，就等于在你机器上证明了协议代码的自洽性。

#### 4.2.5 小练习与答案

**练习 1**：主机发来的帧里 length 字段损坏成了 0xFFFF，`DecodeBuffer` 会怎样？
**答案**：`length > sizeof(PacketInfo) * 2` 的护栏命中，函数置 `type = None` 并**只返回 1**（丢弃一个字节），外层 `Input` 循环会带着偏移一字节的缓冲再次尝试——用渐进式丢字节实现重同步，而不是丢弃整个缓冲。

**练习 2**：为什么 `Protocol::Version` 定义成 `constexpr` 而不是宏？
**答案**：`constexpr` 有类型、有作用域、参与名字查找，且同一定义被两端编译时取值必然一致；宏只是文本替换，容易被不同翻译单元或编译参数影响。这与其「单一事实来源」的定位一致。

**练习 3**：如果给协议新增一种包（比如 `RequestTemperature`），最少要改哪几处？
**答案**：`Protocol.hpp` 里加 payload 结构（若需要）＋ `PacketType` 枚举新值＋ `PacketInfo` union 新成员；`Protocol.cpp` 的 `EncodePacket` switch 加一行 payload 大小（无 payload 则归入空组）；两端业务代码各自处理。**不需要**碰 `Communication`、`usb.c` 或任何帧拆分逻辑——层次隔离的价值就在这里。

### 4.3 PacketConstants 常量与 USB 端点约定

#### 4.3.1 概念说明

先澄清一个容易混淆的点：`PacketConstants.h` 管的是**帧内字段**的偏移与长度，而 **USB 端点**的定义在 `usb.c` 里。两者合起来才是完整的「传输约定」。

- `PacketConstants.h`：帧头常量、各字段偏移/长度、`VNADatapoint` 变长字段的尺寸、固件升级分块大小。它是 `Protocol.hpp/.cpp` 唯一 include 的项目头文件（[Protocol.hpp:7-9](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L7-L9)），同样被 GUI 编译——所以「字段在哪一字节」这件事也是两端同源的。
- `usb.c`：三个批量端点的地址、收发缓冲、发送环形 FIFO，以及一条**独立于数据流之外的日志通道**。

#### 4.3.2 核心流程

**帧字段布局的常量表达**（[Software/VNA_embedded/Application/Communication/PacketConstants.h:10-25](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/PacketConstants.h#L10-L25)）：

```cpp
static constexpr uint8_t PCKT_HEADER_DATA = 0x5A;     // 帧头标志字节
static constexpr uint8_t PCKT_HEADER_OFFSET = 0;       // 帧头偏移 0，长 1
static constexpr uint8_t PCKT_LENGTH_OFFSET = 1;       // 长度字段偏移 1，长 2
static constexpr uint8_t PCKT_TYPE_OFFSET = 3;         // 类型字段偏移 3，长 1
static constexpr uint8_t PCKT_PAYLOAD_OFFSET = 4;      // payload 从第 4 字节起
static constexpr uint8_t PCKT_CRC_LEN = 4;             // 帧尾 CRC 4 字节
static constexpr uint8_t PCKT_COMBINED_HEADER_LEN = 4; // 头部合计
static constexpr uint8_t PCKT_EXCL_PAYLOAD_LEN = 8;    // 除 payload 外的固定开销
```

偏移层层推导（`LENGTH_OFFSET = HEADER_OFFSET + HEADER_LEN`），没有一处裸数字。`Protocol.cpp` 里所有字节操作都引用这些名字——这就是为什么读 `DecodeBuffer` 时不会看到 `data[3]` 这种魔法下标。

**`VNADatapoint` 的变长编码**依赖另一组常量（[PacketConstants.h:31-47](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/PacketConstants.h#L31-L47)）：频率 8 字节、功率 2、点号 2、实部 4、虚部 4、描述字节 1。据此可算出含 \( n \) 个复数值的数据点 payload 长度：

\[ L_{\text{payload}}(n) = \underbrace{8+2+2}_{\text{头}} + n \times \underbrace{(4+4+1)}_{\text{每个值}} = 12 + 9n \]

整帧长 \( L_{\text{frame}}(n) = 12 + 9n + 8 = 20 + 9n \)。上限 32 个值时为 308 字节，小于 `Send` 的 512 字节栈缓冲。`encode()` 的逐字段 memcpy 顺序与此一一对应（[Protocol.hpp:48-64](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L48-L64)），`decode()` 则**反推**值个数（[Protocol.hpp:65-79](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L65-L79)）：\( n = (\text{size} - 12) / 9 \)——变长帧不需要显式长度字段，总长隐含了元素数。

描述字节里的位域约定也在同一处：bit0–3 是端口掩码、bit4 是参考接收机、bit5–7 是 stage 编号（`DPNT_CONF_STAGE_OFFSET = 5`），与 `Source` 枚举（[Protocol.hpp:17-23](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L17-L23)）的取值配套：`addValue` 里 `stage << 5 | sourceMask`（[Protocol.hpp:37-46](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L37-L46)）。这正对应 u3-l2 见过的 `portStageMapping`：一个数据点里同时携带多个端口/stage 的接收电平，GUI 端按比值算 S 参数。

**USB 端点布局**（[Software/VNA_embedded/Application/Drivers/USB/usb.c:8-10](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/USB/usb.c#L8-L10)）：

```cpp
#define EP_DATA_IN_ADDRESS   0x81   // 数据：设备 → 主机
#define EP_DATA_OUT_ADDRESS  0x01   // 数据：主机 → 设备
#define EP_LOG_IN_ADDRESS    0x82   // 日志：设备 → 主机（独立通道）
```

三个都是批量端点（配置描述符里 `bmAttributes = 0x02`，见 [usb.c:88-111](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/USB/usb.c#L88-L111)），全速 64 字节包。**日志走独立端点**是本架构最值得学习的决定之一：固件的 `LOG_INFO` 文本经 `usb_log`（[usb.c:279-290](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/USB/usb.c#L279-L290)）发往 0x82，绝不混入 0x81 的协议字节流——于是 `DecodeBuffer` 永远不必担心「日志文本里出现 0x5A」这类假帧头干扰（App 侧把日志重定向到 USB 的挂接见 [App.cpp:69-70](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L69-L70) 的 `Log_SetRedirect(usb_log)`）。TCP 传输时（u3-l2）数据与日志分走两条 TCP 连接，是同一思想在另一介质上的复刻。

发送侧是一个 6144 字节的**环形 FIFO**（[usb.c:22-27](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/USB/usb.c#L22-L27)）：`Communication::Send` 调 `usb_transmit`（[usb.c:236-277](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/USB/usb.c#L236-L277)）只是把字节拷进 FIFO（临界区用 `__disable_irq` 保护读写指针），实际 USB 传输由 `USBD_Class_DataIn` 完成一次后接着搬下一段（[usb.c:176-205](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/USB/usb.c#L176-L205)）。生产者（App 任务）与消费者（USB 中断）由此解耦：测量数据可以一股脑压进 FIFO，不必等每次传输完成。FIFO 满、或传输卡死超过 100ms（`connection_okay`）时清空重来。

#### 4.3.3 源码精读（端点初始化）

端点的打开与首次接收挂起在类初始化里（[Software/VNA_embedded/Application/Drivers/USB/usb.c:130-138](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/USB/usb.c#L130-L138)）：三个端点按批量类型打开后，**立即**对 OUT 端点 `USBD_LL_PrepareReceive` 挂起 `usb_receive_buffer`——USB 接收是被动的，必须先「备好篮子」主机才能往里放东西。此后每收完一包，`USBD_Class_DataOut`（[usb.c:206-214](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/USB/usb.c#L206-L214)）在回调处理完后又立刻重新挂起，形成连续接收。

#### 4.3.4 代码实践：手算一个数据点帧的长度

1. **实践目标**：用 \( L_{\text{frame}}(n) = 20 + 9n \) 公式预测真实帧长，再用 4.2.4 的方法验证。
2. **操作步骤**：
   - 手算：一次双端口 VNA 测量，每个点需要 S11、S21、S12、S22 四个值 → \( n = 4 \)，payload \( = 12 + 36 = 48 \)，整帧 \( = 56 \) 字节。
   - 修改 `protodemo.cpp`（示例代码）：构造 `Protocol::VNADatapoint<32> dp;`，调用 `dp.addValue(...)` 四次，再 `p.type = PacketType::VNADatapoint; p.VNAdatapoint = &dp;` 编码打印。
3. **需要观察的现象**：帧长是否恰为 56；帧尾 4 字节是否全零（VNADatapoint 豁免 CRC）。
4. **预期结果**：长度 56、CRC 字节 `00 00 00 00`。若想看真机数据：有设备时用 GUI 的数据包日志功能（`devicepacketlog`，u4-l3 会展开）抓一次扫描，核对实拍帧长与本公式一致，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`FW_CHUNK_SIZE = 256`（[PacketConstants.h:31](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/PacketConstants.h#L31)）被谁使用？改大它安全吗？
**答案**：它定义 `FirmwarePacket` 结构里 `data[FW_CHUNK_SIZE]`（[Protocol.hpp:514-517](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L514-L517)），即固件升级时一个 USB 包携带的 flash 数据块大小。理论上两端同编译会一起变，但它同时影响 `FirmwarePacket` 在 `PacketInfo` union 中的占比、flash 写入节奏与 u1-l4 讲过的 `AssembleFirmware.py` 打包约定——那是 Python 侧的另一份隐式契约，**不能只改一处**。

**练习 2**：为什么日志要独占端点 0x82，而不是复用数据端点、在协议里加一种「日志包」？
**答案**：独立端点让协议字节流保持「纯净」，解析器无需区分协议帧与文本；也避免高吞吐日志挤占数据流的 FIFO 与带宽。代价是多占一个端点和一个发送状态标志。这是「通道隔离优于类型区分」的典型取舍。

**练习 3**：`usb_transmit` 在拷贝 FIFO 前后用 `__disable_irq()`/`__enable_irq()` 包住，保护的是什么？
**答案**：读写指针与 FIFO 电平（`usb_transmit_fifo_level`、`usb_transmit_read_index`）会被 App 任务（生产者）和 USB 中断里的 `USBD_Class_DataIn`（消费者）同时触碰，读-改-写序列必须原子；短暂关中断是最简单的临界区手段。

### 4.4 二进制协议与 SCPI：两套入口的真实位置

#### 4.4.1 概念说明

学习目标里说「固件内同时存在二进制协议与 SCPI 文本协议两套入口」——严格讲这句话**需要修正**，而修正本身就是本讲最有价值的发现之一：

- 在 `Software/VNA_embedded` 整个固件源码树里 Grep `SCPI`，**一个匹配都没有**。固件只懂二进制协议。
- SCPI 命令树实现在 **GUI 侧**（`scpi.cpp`/`scpi.h`，u10-l1 会精读）。GUI 监听 TCP 端口，接收 `:DEVice:INFo:FWREVision?` 这类文本命令，在进程内查缓存或向设备发**二进制包**取数，再把结果拼成文本回给客户端。

所以正确的架构图景是：**SCPI 止于 GUI，GUI 是 SCPI 世界与二进制协议世界之间的翻译网桥**。设备永远不知道 SCPI 的存在。这个设计让瘦客户端（Python 脚本、labview）能用标准仪器语言驱动整套系统，又让 USB 链路保持紧凑高效的二进制格式。

#### 4.4.2 核心流程

以「查询固件版本」为例的完整翻译链：

```text
Python/SCPI 客户端
  └─ TCP: ":DEVice:INFo:FWREVision?"
       └─ GUI scpi.cpp 命令树命中 FWREVision 回调 (appwindow.cpp:745)
            └─ device->getInfo().firmware_version     ← 读 GUI 本地缓存
                 （缓存的来源：连接时驱动发的二进制包——）
                 librevnausbdriver.cpp:125  sendWithoutPayload(RequestDeviceInfo)
                   → USB: [5A 0C 00 0F | CRC32]        ← 无 payload 命令, type=15
                     → 固件 App.cpp:160 case → 回 DeviceInfo 包 (type=5, payload=DeviceInfo 结构体)
                          → GUI 解析后填入 DeviceDriver::Info 缓存
       └─ TCP: "1.2.3" 之类文本应答
```

关键点：SCPI 查询命中的是**缓存**，不会每条查询都惊动设备——真正穿 USB 的是连接建立时那一对 `RequestDeviceInfo`/`DeviceInfo` 二进制包。

#### 4.4.3 源码精读

GUI 侧 SCPI 命令树的装配（[Software/PC_Application/LibreVNA-GUI/appwindow.cpp:538-539](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L538-L539)）创建 `DEVice` 节点，其下的 `INFo` 子节点（[appwindow.cpp:743-758](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L743-L758)）：

```cpp
auto scpi_info = new SCPINode("INFo");
scpi_dev->add(scpi_info);
scpi_info->add(new SCPICommand("FWREVision", nullptr, [=](QStringList){
    if(device) {
        return device->getInfo().firmware_version;   // 读缓存, 不发 USB 包
    } ...
}));
```

而缓存的生产者是驱动层：USB 驱动连接时发无 payload 的 `RequestDeviceInfo`（[Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp:125](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L125)），收到的 `DeviceInfo` 包经 `DecodeBuffer` 解出（[librevnausbdriver.cpp:165](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L165)）——与固件用的是同一个函数（GUI 发送侧 `EncodePacket` 在 [librevnausbdriver.cpp:371](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Device/LibreVNA/librevnausbdriver.cpp#L371)）。

固件侧的应答分支（[Software/VNA_embedded/Application/App.cpp:160-167](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L160-L167)）：

```cpp
case Protocol::PacketType::RequestDeviceInfo: {
    Communication::SendWithoutPayload(Protocol::PacketType::Ack);
    Protocol::PacketInfo p;
    p.type = Protocol::PacketType::DeviceInfo;
    p.info = HW::Info;              // 编译期固化的设备信息+能力上限
    Communication::Send(p);
}
```

#### 4.4.4 代码实践：完成「命令 → 结构体 → 固件处理函数」对照表

这就是本讲义规格指定的实践任务，答案先行——**「`:DEVice:INFO?` 类查询对应的二进制命令结构」是无 payload 的 `RequestDeviceInfo` 包（type 15），应答才是携带 `Protocol::DeviceInfo` 结构体的 `DeviceInfo` 包（type 5）；SCPI 文本本身只存在于 GUI 侧，不穿透到固件**。请自己动手验证并扩展：

1. **实践目标**：建立 SCPI 需求 ↔ 二进制包 ↔ 固件处理代码三层的映射能力，这是后续排错（"为什么这个 SCPI 命令没生效"）的核心技能。
2. **操作步骤**：
   - 打开 [Protocol.hpp:573-609](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L573-L609) 的 `PacketType` 枚举，挑三个命令；
   - 对每个命令在 [App.cpp:125-303](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L125-L303) 的 switch 里找到对应 case 与被调函数；
   - 整理成表（下面的样例三行可直接核对，再自己补两行）。
3. **需要观察的现象**：纯文档产出。
4. **预期结果**（样例）：

   | 二进制命令（PacketType） | 携带的 payload 结构体 | 固件处理（App.cpp case → 业务函数） |
   |---|---|---|
   | `SweepSettings` (= 2) | `Protocol::SweepSettings`（[Protocol.hpp:155](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L155)） | [App.cpp:126-131](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L126-L131) → `VNA::Setup()` |
   | `RequestDeviceInfo` (= 15) | 无 payload | [App.cpp:160-167](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L160-L167) → 装配 `HW::Info` 回发 `DeviceInfo` 包 |
   | `SpectrumAnalyzerSettings` (= 13) | `Protocol::SpectrumAnalyzerSettings`（[Protocol.hpp:470](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L470)） | [App.cpp:153-159](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L153-L159) → `SA::Setup()` |
   | `Generator` (= 12) | `Protocol::GeneratorSettings`（[Protocol.hpp:192](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L192)） | [App.cpp:146-152](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L146-L152) → `Generator::Setup()` |
   | `FrequencyCorrection` (= 22) | `Protocol::FrequencyCorrection`（[Protocol.hpp:529](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Protocol.hpp#L529)） | [App.cpp:262-265](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L262-L265) → `Cal::setFrequencyCal()` |

   注意规律：**几乎每个分支都以 Ack/Nack 收尾**，这是 u3-l2 讲过的「单包在途」发送队列的设备端依据——主机靠 Ack 确认命令送达，才发下一条。

#### 4.4.5 小练习与答案

**练习 1**：既然固件没有 SCPI，那么 `:VNA:FREQuency:STARt 1GHz` 这条 SCPI 命令是怎样变成设备动作的？
**答案**：GUI 的 SCPI 树命中后更新 VNA 模式的设置对象（u7-l1 的 `VNA` 类），触发配置更新；`librevnadriver` 把整套设置打包成一个 `SweepSettings` 二进制帧发往设备；固件在 `App_Process` 的对应 case 里调 `VNA::Setup()` 并回 Ack。SCPI 的「单参数」语义被折叠成二进制协议的「整包配置」语义。

**练习 2**：GUI 编译固件的 `Protocol.cpp` 有什么坏处？
**答案**：两个仓库组件产生构建耦合（固件头文件路径变化会弄坏 GUI 构建）；嵌入式风格的代码（裸 memcpy、位域）进入桌面工程需要保持编译器兼容；`DecodeBuffer` 里堆分配 `VNADatapoint` 的路径也要纳入 GUI 的内存管理。相较之下，收益（协议两端零漂移）被作者判定为大于代价。

**练习 3**：设备收到一个 `PacketType::VNADatapoint`（type 27）包会发生什么？
**答案**：`App_Process` 的 switch 没有 `VNADatapoint` 分支，落入 `default` 回 `Nack`（[App.cpp:299-302](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L299-L302)）。该类型本是设备→主机方向的测量数据，主机不该发；协议层不禁止，但业务层用 Nack 婉拒——层间职责划分的又一个体现。

## 5. 综合实践

**任务：做一个「迷你协议监视器」，把本讲四个模块串成闭环。**

在 4.2.4 的 `protodemo.cpp` 基础上扩展（**示例代码**，放在仓库外）：

1. **编码三种包**：`SweepSettings`（points=501）、`RequestDeviceInfo`（无 payload，用 `SendWithoutPayload` 的逻辑手动置 `p.type` 后编码）、手工填充的 `VNADatapoint<4>`（4 个值）。
2. **拼接成一条字节流**：把三帧首尾相接，中间**故意插入 3 个杂散字节**（如 `0x00 0x31 0x5A`——注意最后一个就是假帧头），再模拟 USB 的 8 字节分片：每次只喂 8 字节给一个循环调用 `Protocol::DecodeBuffer` 的解码器（仿照 `Communication::Input` 写，含剩余字节前移）。
3. **验证**：解码器应能（a）容忍分片（半包跨片重组）；（b）跳过杂散字节后仍解出全部三帧；（c）对 `VNADatapoint` 帧报告 CRC 为零的豁免约定。打印每帧的 type 与关键字段（如 points、频率、每个复数值的描述字节）。
4. **对照源码复盘**：你的解码器与 [Communication.cpp:18-43](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Communication/Communication.cpp#L18-L43) 有几处不同？`inputBuffer` 满时的丢弃行为你处理了吗？

完成后你就拥有了一个不依赖任何硬件的协议一致性测试台——它同时是固件、GUI 两端共享代码的行为验证，也是将来抓包分析（u4-l3 的 `devicepacketlog`）的离线解读器。运行结果**待本地验证**（预期三帧全部正确解出，杂散字节被跳过且最多损失 1 字节重同步开销）。

## 6. 本讲小结

- `Communication` 是固件唯一的协议进出口：收方向「USB 中断 → 字节累积 → `DecodeBuffer` 拆帧 → 回调」，发方向「`EncodePacket` 编码 → 6144 字节环形 FIFO → IN 端点」；业务分发被刻意推迟到 App 任务（`xTaskNotifyFromISR` 交接）。
- 帧是五段式自描述结构：`0x5A` 帧头 + 2 字节小端总长 + 1 字节类型 + 变长 payload + 4 字节 CRC32；`PacketInfo = 类型 + union`，配 `#pragma pack(1)` 后「结构体整体 memcpy」即完成序列化。
- 协议代码**两端同源**：GUI 的 `.pro` 直接编译固件的 `Protocol.hpp/.cpp`，两端布局、字节序、CRC 约定由同一份代码保证；`Protocol::Version`（当前 14）用于兼容性协商。
- 高吞吐的 `VNADatapoint` 豁免 CRC（帧尾恒 0，编码省 18µs/点），是「吞吐换校验」的显式取舍；变长 payload 长度 \( 12+9n \)，元素个数由总长隐含。
- USB 层三个批量端点：0x01 OUT 收命令、0x81 IN 发数据、0x82 IN 专走日志——日志与协议字节流物理隔离，解析器永不被日志文本干扰。
- 固件里**没有 SCPI**：SCPI 命令树在 GUI（TCP 入口），查询命中 GUI 缓存；缓存由连接时的 `RequestDeviceInfo`/`DeviceInfo` 等二进制包填充。GUI 是两种协议间的翻译网桥。

## 7. 下一步学习建议

- **下一讲 u4-l2（USB 协议逐包解析）**：拿 `Documentation/DeveloperInfo/USB_protocol_v12.tex` 与 `Device_protocol_v13.tex` 两份正式协议文档，逐类核对本讲从代码推出的帧格式，体会「文档 vs 代码」的异同与版本演进（注意文档版本号 v12/v13 与代码 `Version = 14` 的差异）。
- **u4-l3（GUI 侧协议实现）**：换到对岸看 `librevnadriver` 如何构造/发送这些包、用 `DecodeBuffer` 拆包，并用 `devicepacketlog` 抓真实会话——与固件侧收到的内容逐字节对上。
- **u5-l1（固件启动流程）**：本讲的 `App_Init`/`App_Process` 属于固件主任务；下一单元从 `main.c` 出发补全 FreeRTOS 任务全貌。
- 延伸阅读：`Communication.cpp` 里被注释的 CDC 代码、`usb.c` 的 WCID/WinUSB 描述符（[usb.c:114-128](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/USB/usb.c#L114-L128)），分别是通信演化和 Windows 免驱接入的历史痕迹。
