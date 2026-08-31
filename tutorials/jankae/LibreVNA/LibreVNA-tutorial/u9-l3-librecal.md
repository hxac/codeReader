# LibreCAL：电子自动化校准件

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 LibreCAL 这个"电子校准件"在硬件上做什么（端口切换到 Open/Short/Load/Through），以及它在软件上为什么是 GUI 里**第二条独立的 USB 通路**。
2. 读懂 `USBDevice` 的设备发现、连接与"按行收发"协议，理解它与主设备驱动（u3-l2）在线程模型上的差异。
3. 列出 LibreCAL 固件理解的 SCPI 风格命令集，并解释系数集（CoefficientSet）的快/慢两种读取路径。
4. 跟踪 `LibreCALDialog` 的自动校准状态机：自动端口识别 → 加载系数 → 填充校准件 → 逐项测量 → SOLT 求解，并说明它与人工校准共用哪一根信号管线。
5. 评估电子校准件相对传统机械校准件的风险与收益。

## 2. 前置知识

### 2.1 机械校准件与电子校准件

u9-l1 讲过，SOLT 校准需要依次把被校端接上**已知**的 Open、Short、Load、Through 标准件。传统做法是一套机械件（校准套件），靠人手逐个拧到端口上——双端口校准要换七八次件，费时且容易接错。

**电子校准件（E-Cal）**把这些标准件做进一个盒子里，内部用开关把每个端口切换到不同的终端（开路、短路、匹配负载），甚至把两个端口直通连接。电脑通过 USB 发一条命令即可完成"换件"。LibreCAL 就是 LibreVNA 作者配套的开源电子校准件，它的固件在另一个仓库（github.com/jankae/LibreCAL），本仓库只包含 **GUI 侧的支持代码**。

### 2.2 SCPI 风格的文本命令

LibreCAL 固件说的是一种 SCPI（可编程仪器的标准命令）风格的文本协议：主机发送一行命令（以 `\r\n` 结尾），设备回一行应答。命令名有长、短两种形式，例如 `:TEMPerature:STABLE?` 中大写部分是短形式，补上小写就是完整的长形式；以 `?` 结尾表示查询。这套语法与 GUI 内部 SCPI 框架（u10-l1 会精读）同宗，但这里是 **GUI 作为"主机"去命令另一台仪器**。

### 2.3 需要回顾的旧知识

- **u9-l1/u9-l2**：校准件模型（系数描述 vs Touchstone 测量文件描述，测量优先）、12 项误差模型与 `Calibration::compute`。
- **u3-l2**：libusb 的异步接收、`USBInBuffer` 环形缓冲、`DecodeBuffer` 拆帧。
- **u8-l4**：Touchstone 文件格式，尤其是两端口文件的参数顺序 `S11 S21 S12 S22`。
- **u2-l2**：`ModeHandler` 的 activate/deactivate，本讲会看到 `acquireControl` 如何"冻结"当前模式。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/usbdevice.h/.cpp` | `USBDevice`：LibreCAL 的 libusb 传输层——发现、连接、按行收发 |
| `Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.h/.cpp` | `CalDevice`：把 SCPI 文本协议包装成 C++ API，管理端口切换与系数集读写 |
| `Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.h/.cpp/.ui` | `LibreCALDialog`：一键校准对话框，自动校准状态机的宿主 |
| `Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp` | 菜单入口 + `VNA::StartCalibrationMeasurements`（测量执行的另一半） |
| `Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp/.h` | `Calibration`：被对话框驱动的主角，`measurementsUpdated` 信号回路 |
| `Software/PC_Application/LibreVNA-GUI/appwindow.cpp` | `acquireControl`/`releaseControl` 的全局处理（暂停/恢复当前模式） |

三个类的分工可以概括为一句话：**`USBDevice` 管字节，`CalDevice` 管命令，`LibreCALDialog` 管流程**。

## 4. 核心概念与源码讲解

### 4.1 LibreCAL USB 设备抽象（USBDevice）

#### 4.1.1 概念说明

LibreCAL 是一台**独立的 USB 设备**：它有自己的 MCU 与固件，和 LibreVNA 主设备并列插在电脑上。所以 GUI 需要在主设备驱动之外再开一条 USB 通路，而且这条通路不经过 `DeviceDriver` 抽象——LibreCAL 不是测量仪器，只是"可编程的校准件"，直接用 libusb 对话最简单。

`USBDevice` 承担三件事：

1. **发现**：枚举所有 USB 设备，按 VID/PID 初筛，再读产品字符串确认识别 LibreCAL，收集序列号。
2. **连接**：`libusb_claim_interface` 独占接口 2，启动 libusb 事件线程，创建接收缓冲。
3. **行协议**：提供阻塞式的 `Cmd()`（发命令、等空应答）与 `Query()`（发查询、等一行应答）。

#### 4.1.2 核心流程

发现与连接的流程：

```text
libusb_get_device_list
  └─ 逐个设备：读描述符 → VID/PID 匹配 {0x0483,0x4122} 或 {0x1209,0x4122}？
       └─ libusb_open → 读产品字符串 == "LibreCAL"？
            └─ 读序列号字符串 → 回调 foundCallback(handle, serial)
                 └─ 回调返回 false 则中止搜索（已找到要连的设备）
连接：claim_interface(handle, 2)
  └─ 启动 USBHandleThread（libusb_handle_events 循环）
  └─ 创建 USBInBuffer(端点 0x83, 64KB)，DataReceived → ReceivedData (DirectConnection)
  └─ 循环 receive(10ms) 清空设备里残留的旧数据
```

收发的线程模型：

```text
[libusb 事件线程]                         [调用者线程（通常是 GUI 线程）]
USBInBuffer 收到批量数据
  → ReceivedData()                        Query(":PORT? 1")
      memchr 找 '\n' 拆行                   ├─ flushReceived() 清空旧行
      → lineBuffer.append(line)            ├─ send(命令 + "\r\n")  ← bulk OUT 端点 0x03
      → cv.notify_one() ──────────────→    └─ receive() 在条件变量上阻塞
                                            ← 被唤醒，取走 lineBuffer 首行
```

这与主设备驱动（u3-l2）形成有意思的对照：主设备驱动"字节解析在事件线程、协议解释经 QueuedConnection 切回 GUI 线程"；而 LibreCAL 通路干脆**全程同步阻塞**——`Query` 最多等 timeout 毫秒，拿到一行才返回。因为 LibreCAL 的命令都是低频控制命令（切端口、读系数），阻塞 2 秒以内的代价可以接受，换来的是极简的编程模型。

#### 4.1.3 源码精读

**USB ID 表**：两个 VID——`0x0483` 是 STMicroelectronics（LibreCAL 用 STM32），`0x1209` 是 pid.codes 开源硬件 VID；PID 统一为 `0x4122`。[usbdevice.cpp:14-21](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/usbdevice.cpp#L14-L21)

**构造函数**：先 `libusb_init`，再调用 `SearchDevices` 查找目标序列号（为空则连第一台找到的）。回调里记下句柄并返回 `false` 中止搜索。[usbdevice.cpp:31-42](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/usbdevice.cpp#L31-L42) 找不到设备时直接 `throw std::runtime_error`——注意只有指定了序列号才弹错误框，枚举场景（`GetDevices`）静默返回空。[usbdevice.cpp:44-53](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/usbdevice.cpp#L44-L53)

**独占接口与启动接收**：claim 接口 2 失败会提示"可能已连接到该设备"（Linux 上则提示缺 udev 规则）。[usbdevice.cpp:57-68](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/usbdevice.cpp#L57-L68) 随后启动事件线程、创建 64KB 的 `USBInBuffer`（端点 `LIBUSB_ENDPOINT_IN | 0x03`），并以 `Qt::DirectConnection` 连接数据信号——保证拆行发生在 libusb 事件线程里，行进队后立刻唤醒等待者。[usbdevice.cpp:72-74](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/usbdevice.cpp#L72-L74) 最后用 10ms 超时循环把设备里可能残留的旧应答排空。[usbdevice.cpp:76-78](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/usbdevice.cpp#L76-L78)

**设备识别的核心判断**：读到产品字符串后，必须严格等于 `"LibreCAL"` 才算命中，然后读序列号交给回调。同一个 VID/PID 下可能挂着其它设备，字符串过滤是第二道闸。[usbdevice.cpp:192-207](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/usbdevice.cpp#L192-L207)

**发送**：把命令的 Latin-1 字节加上 `\r\n` 一起从 bulk OUT 端点 0x03 发出，同时清空行缓冲（新命令意味着旧应答作废）。[usbdevice.cpp:217-231](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/usbdevice.cpp#L217-L231)

**接收**：在互斥锁下等条件变量，超时（默认 2000ms）返回 false；成功则取走行缓冲首行。[usbdevice.cpp:233-248](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/usbdevice.cpp#L233-L248)

**拆行**：`ReceivedData` 用 `memchr` 反复找 `\n`，每行长度 `handled_len - 1` 恰好把行尾的 `\r` 剥掉，行入队后从缓冲移除 `\r\n` 两个字节。[usbdevice.cpp:250-270](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/usbdevice.cpp#L250-L270)

**两种上层原语**：`Cmd` 要求应答为空串（命令成功回空行）；`Query` 返回应答行本身；两者失败都发 `communicationFailure` 信号。[usbdevice.cpp:92-121](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/usbdevice.cpp#L92-L121) 枚举入口 `GetDevices` 复用同一套搜索但 `ignoreOpenError=true`（枚举时打不开设备不算错误），返回序列号集合。[usbdevice.cpp:124-142](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/usbdevice.cpp#L124-L142)

#### 4.1.4 代码实践

**实践目标**：验证"没有 LibreCAL 硬件时，发现层如何优雅退化为空"。

**操作步骤**：

1. 按 u1-l3 的方法编译并启动 GUI（无需连接任何设备）。
2. 进入 VNA 模式，菜单选择 `Calibration → Electronic Calibration`（对应 [vna.cpp:129-133](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L129-L133) 的菜单项）。
3. 观察对话框：设备下拉框应为空，状态标签显示 "Not connected to a LibreCAL device"（红色）。
4. 对照代码解释你看到的一切：下拉框内容来自 [librecaldialog.cpp:87-90](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L87-L90) 的 `USBDevice::GetDevices()`；状态文字来自 [librecaldialog.cpp:220-224](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L220-L224)。

**需要观察的现象**：对话框正常打开、不崩溃、Start 按钮禁用。

**预期结果**：`GetDevices` 返回空 `std::set` → 下拉框无条目 → `device` 为 `nullptr` → `updateCalibrationStartStatus` 走"未连接"分支并禁用 Start。若你手头没有编译环境，此步**待本地验证**；代码路径本身可完整走读确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Cmd()` 期望空响应，而 `Query()` 返回一行？

**答案**：LibreCAL 固件的约定是：不产生数据的命令（如 `:PORT 1 OPEN`）成功时回一个空行；查询（`:PORT? 1`）回一行数据。`Cmd` 收到空串即视为成功（[usbdevice.cpp:97-99](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/usbdevice.cpp#L97-L99)），非空或超时都算通信失败。

**练习 2**：`GetDevices()` 枚举时用 `ignoreOpenError=true`，构造函数连接时用 `false`，为什么？

**答案**：枚举只是"看一眼有哪些设备"，某台设备被别的进程占用不应打断整个列表（[usbdevice.cpp:134-137](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/usbdevice.cpp#L134-L137)）；而明确要连接某台设备却打不开时，用户需要知道原因（udev 规则缺失、重复连接），所以要弹错误框（[usbdevice.cpp:180-189](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/usbdevice.cpp#L180-L189)）。

**练习 3**：如果设备回的行只以 `\n` 结尾、不带 `\r`，`ReceivedData` 会怎样？

**答案**：会出问题。`handled_len - 1` 无条件地假设 `\n` 前有一个 `\r`，缺 `\r` 时会把行内最后一个有效字符一起剥掉（[usbdevice.cpp:258-259](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/usbdevice.cpp#L258-L259)）。这是一个隐式的协议约定：设备必须回 CRLF 行尾。

### 4.2 电子校准件协议（CalDevice）

#### 4.2.1 概念说明

`CalDevice` 在 `USBDevice` 之上定义了 LibreCAL 的**语义**：端口是什么标准、温度是否稳定、里面存了哪些系数集。

硬件模型：LibreCAL 有 `:PORTS?` 报告的若干端口（实物为 4 个），每个端口可被命令切换到五种状态之一——`Open`、`Short`、`Load`、`Through n`（与端口 n 直通）、`None`（断开）。这就是"电子换件"的全部含义。此外设备内置加热器，把标准件恒温在工作点以降低漂移，GUI 需要等待 `:TEMPerature:STABLE?` 返回 `TRUE` 才建议开始校准。

**系数集（CoefficientSet）**是 LibreCAL 的核心资产：出厂或用户标定的每个标准件的 S 参数以 Touchstone 数据的形式存在设备 flash 里，按 `P1_OPEN`、`P12_THROUGH` 这样的命名组织，多套系数可以共存（至少有一套 `FACTORY`）。注意这正好对应 u9-l1 讲过的"标准件的两种描述之一——测量文件描述"：LibreCAL 的标准件**全部以实测 Touchstone 为准**，不用寄生参数模型。

#### 4.2.2 核心流程

构造时的握手序列：

```text
*IDN?          → 应答必须以 "LibreCAL," 开头，否则抛异常
:FIRMWARE?     → 解析版本号；≥0.2 则同步一次本地时间(:DATE_TIME)
:PORTS?        → 端口数
(连接 communicationFailure → disconnected 信号转发)
(出厂系数问题序列号清单检查，见下文)
```

读系数的两代协议：

```text
慢路径（任意固件）：
  对每个系数：:COEFF:NUM? <set> P1_OPEN   → 点数 N
              循环 N 次：:COEFF:GET? <set> P1_OPEN <i>
                → "1.000000000,0.98,-0.05"（频率 GHz + 实/虚对，CSV）

快路径（固件 ≥ 0.2.1）：
  :COEFF:GET? <set> P1_OPEN   → 一次性回多行文本：
        ! 注释行、# 选项行（忽略）
        <freq> <re> <im> ...（逐行）
        END
```

写回路径（仅写被修改过的系数）：`:COEFF:CREATE <set> <param>` → 逐点 `:COEFF:ADD <freqGHz> <re> <im> ...` → `:COEFF:FIN` 收尾；点数为 0 则改用 `:COEFF:DEL` 删除。

两端口系数还有一个**格式陷阱**：Touchstone 两端口文件的字段顺序是 `S11 S21 S12 S22`（S21 在前），而 GUI 内部 `Touchstone::AddDatapoint` 期望 `S11 S12 S21 S22`，所以读入要交换 `S[1]` 与 `S[2]`，写出前再换回来。

#### 4.2.3 源码精读

**构造握手**：`*IDN?` 应答前缀校验失败即抛异常，构造失败。[caldevice.cpp:82-87](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L82-L87) 固件版本被折算成 `firmware_major_minor` 浮点数用于特性开关，≥0.2 时同步带 UTC 偏移的本地时间。[caldevice.cpp:89-104](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L89-L104) 端口数查询失败则记 0。[caldevice.cpp:105-110](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L105-L110)

**出厂系数召回机制**：源码里硬编码了 150 余个受 2023 年出厂标定相位错误影响的序列号（[caldevice.cpp:18-50](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L18-L50)）。命中后主动抽查 `P12_THROUGH` 在 1/2/3 GHz 三个点的 S12 相位，换算成延时与期望值 498ps 比较，容差 17ps。[caldevice.cpp:116-170](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L116-L170) 这就是"固件系数也能远程修复"的完整闭环：确认有问题则弹出 `factoryUpdateDialog`，按序列号从 `https://librecal.kaeberich.com/calibrationdata/<serial>.zip` 下载新系数（[caldevice.cpp:907](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L907)），解包成 18 个 Touchstone，然后用 `:FACT:ENABLEWRITE I_AM_SURE` 解锁出厂分区、`:FACT:DEL` 格式化（给 5 秒超时），再走正常写回流程。[caldevice.cpp:862-864](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L862-L864) `I_AM_SURE` 这个口令字符串本身就是防误操作的软件互锁。

**端口切换**：`StandardToString`/`StandardFromString` 在枚举与文本（`OPEN`/`SHORT`/`LOAD`/`THROUGH n`/`NONE`）之间转换。[caldevice.cpp:187-207](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L187-L207) `getStandard`/`setStandard` 只是一行查询/命令的封装。[caldevice.cpp:209-220](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L209-L220) `availableStandards` 返回全部可选标准（Through 覆盖 1–4 号目标端口）。[caldevice.cpp:222-225](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L222-L225)

**温度与稳定**：三个查询——温度值、是否稳定（应答字符串等于 `TRUE`）、加热功率。[caldevice.cpp:227-253](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L227-L253) 对话框每秒轮询它们（见 4.3）。

**系数读取分流**：`loadCoefficientSets` 先把端口列表排序（否则 through 系数的命名会错），再按固件版本选择快/慢线程，全程置 `transferActive` 标志。[caldevice.cpp:286-306](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L286-L306) 头文件注释明确警告：传输完成前不要调用其它函数，进度经 `updateCoefficientsPercent`/`updateCoefficientsDone` 信号汇报（[caldevice.h:80-90](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.h#L80-L90)）——因为 `USBDevice` 是单命令在途的同步通道，并发调用会串包。

**慢路径**：先汇总所有系数的点数算百分比基数，再逐系数、逐点查询。每个系数构造一个 lambda `createCoefficient`：查点数 → 建 1 端口或 2 端口 Touchstone → 逐点解析 CSV（频率单位是 GHz，乘 1e9）→ 加点。[caldevice.cpp:375-411](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L375-L411) 两端口数据的 S21/S12 交换就在这里。[caldevice.cpp:396-400](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L396-L400)

**快路径**：不用 `Query()`，而是 `flushReceived` 后直接 `send`，然后在一个循环里裸收行：忽略 `START`/`!`/`#` 开头的行，遇到 `END` 结束，其余按空格分隔解析为数据点。[caldevice.cpp:457-508](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L457-L508) 这本质上是设备把一个 Touchstone 文件当文本流吐出来，一次 USB 往返换一整段数据，避免了慢路径"一点一问"的往返开销。

**写回**：只处理 `modified` 为真的系数；有点数则 CREATE/ADD/FIN 三段式，无点数则 DEL；结束后把空系数集从列表里清除。[caldevice.cpp:593-636](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L593-L636) 写出前同样做一次 S21/S12 交换（方向与读入相反）。[caldevice.cpp:606-610](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L606-L610)

**系数集枚举与索引**：`getCoefficientSetNames` 发 `:COEFF:LIST?`，应答必须以 `FACTORY` 开头（否则视为失败返回空表）。[caldevice.cpp:696-703](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L696-L703) through 系数用 `port1 * ports + port2` 编码成 map 键，要求 `port1 < port2`，非法组合返回 -1。[caldevice.cpp:957-963](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L957-963)

#### 4.2.4 代码实践

**实践目标**：不看文档、只读源码，整理出 LibreCAL 的完整命令表。

**操作步骤**：

1. 在 `caldevice.cpp` 中 grep 所有 `usb->Query(` 与 `usb->Cmd(` 调用。
2. 按功能分组填入下表（答案已给出，先自己填再对照）：

| 命令 | 方向 | 含义 | 源码位置 |
|---|---|---|---|
| `*IDN?` | 查询 | 设备识别，须以 `LibreCAL,` 开头 | [caldevice.cpp:83](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L83) |
| `:FIRMWARE?` | 查询 | 固件版本字符串 | [caldevice.cpp:89](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L89) |
| `:PORTS?` | 查询 | 端口数 | [caldevice.cpp:105](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L105) |
| `:PORT <n> <STD>` | 命令 | 把端口 n 切到标准件 STD | [caldevice.cpp:218](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L218) |
| `:PORT? <n>` | 查询 | 读端口 n 当前标准件 | [caldevice.cpp:211](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L211) |
| `:TEMP?` | 查询 | 温度 | [caldevice.cpp:229](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L229) |
| `:TEMPerature:STABLE?` | 查询 | 恒温是否已稳定（`TRUE`/`FALSE`） | [caldevice.cpp:240](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L240) |
| `:HEATER:POWER?` | 查询 | 加热器功率 | [caldevice.cpp:246](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L246) |
| `:DATE_TIME <t>` / `:DATE_TIME?` | 双向 | 设置/读取设备时钟（≥0.2） | [caldevice.cpp:99](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L99)、[279](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L279) |
| `:COEFF:LIST?` | 查询 | 系数集名列表（含 `FACTORY`） | [caldevice.cpp:698](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L698) |
| `:COEFF:NUM? <set> <param>` | 查询 | 某系数的点数 | [caldevice.cpp:356](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L356) |
| `:COEFF:GET? <set> <param> [i]` | 查询 | 逐点（慢）/整段（快）取系数 | [caldevice.cpp:387](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L387)、[461](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L461) |
| `:COEFF:CREATE/ADD/FIN/DEL` | 命令 | 写系数的三段式与删除 | [caldevice.cpp:601-630](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L601-L630) |
| `:BOOTloader` | 命令 | 进入固件升级引导程序 | [caldevice.cpp:272](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L272) |
| `:FACT:ENABLEWRITE I_AM_SURE` / `:FACT:DEL` | 命令 | 解锁并清除出厂系数分区 | [caldevice.cpp:862-864](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L862-L864) |

**需要观察的现象 / 预期结果**：这张表本身就是结果——LibreCAL 的"协议规范"就藏在 `CalDevice` 的方法里，没有任何独立的协议文档（在本仓库中）。这是读嵌入式配套设备代码的常用技巧：**API 封装层就是协议的事实文档**。

#### 4.2.5 小练习与答案

**练习 1**：为什么快路径要求固件 ≥ 0.2.1，而不是 ≥ 0.2？

**答案**：`:DATE_TIME` 在 0.2 就有了，但"一次 `:COEFF:GET?` 回整段 START…END 文本流"的批量协议是 0.2.1 才加入的行为。版本判断在 [caldevice.cpp:301](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L301)（用 `Util::firmwareEqualOrHigher(firmware, "0.2.1")` 判断，调用方还能用 `fast=false` 强制走慢路径）。对老固件发批量查询只会收到逐点应答，解析必然失败。

**练习 2**：`portsToThroughIndex` 为什么要求 `port1 < port2`，返回 -1 意味着什么？

**答案**：Through 是无向标准件，`P12_THROUGH` 与 `P21_THROUGH` 是同一个东西，设备端只存较小端口在前的那一份。索引函数把有序对 `(port1,port2)` 映射为 `port1 * ports + port2` 做 map 键；非法对（越界或逆序）返回 -1，`getThrough` 对 -1 查不到就返回 `nullptr`，上层据此知道"该组合没有系数"。[caldevice.cpp:947-963](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L947-L963)

**练习 3**：保存系数时如何避免把整套数据重写一遍？

**答案**：每个 `Coefficient` 带 `modified` 标志，`saveCoefficientSetsThread` 里的 lambda 一开始就检查：未修改直接返回 true；完全没有修改时 `saveCoefficientSets` 甚至不开线程、立即发 `updateCoefficientsDone(true)`（[caldevice.cpp:318-327](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L318-L327)）。USB 全速带宽有限，全量重写 18 个 Touchstone 会非常慢。

### 4.3 自动测量流程（LibreCALDialog）

#### 4.3.1 概念说明

`LibreCALDialog` 是"一键校准"的指挥家，协调三方：

- **LibreCAL**（经 `CalDevice`）：负责"换件"——把端口切到 Open/Short/Load/Through；
- **当前活动设备驱动**（`DeviceDriver::getActiveDriver()`）：负责真正取测量；
- **`Calibration` 对象**：负责生成校准件与测量对象，最后求解误差项。

本讲最重要的一个洞察是：**自动校准没有发明任何新的测量机制**。人工校准里，用户在 Calibration Measurements 窗口选中测量项、点 "Measure"，触发 `emit startMeasurements(m)`（[calibration.cpp:622-626](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L622-L626)）；自动校准里，同一个信号由 `LibreCALDialog` 发出（[librecaldialog.cpp:569](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L569)）。下游管线完全一致：`startMeasurements` → `VNA::StartCalibrationMeasurements`（停扫、清旧数据、重配、开启采集）→ VNA 在最后一个平均圈逐点调 `cal->addMeasurements` → 扫完调 `cal->measurementsComplete()` → 发 `measurementsUpdated`。区别只在**谁来推下一步**：人工流程是用户看状态点按钮，自动流程是 `measurementsUpdated` 信号驱动状态机自动推进。

**风险与收益的对比**（对应学习目标 3）：

| 维度 | 机械校准件 | LibreCAL 电子校准 |
|---|---|---|
| 步数 | 双端口 SOLT 需手动换件 7 次以上 | 连一次线，全自动 |
| 出错面 | 接错件/接错口/忘记拧紧 | 端口映射可自动识别；但依赖开关可靠性 |
| 标准件质量 | 高端机械件漂移极小 | 开关、PCB 走线串扰引入额外误差项；需恒温（加热等待） |
| 单点故障 | 件坏了肉眼可见 | 系数错误**不可见**——所以源码里有出厂系数召回与温度门槛两道防线 |
| 连接器损耗 | 反复插拔磨损校准件 | 反复插拔磨损 LibreCAL 端口，标准件本身不磨损 |
| 可追溯 | 人工记录 | 系数集名+序列号自动写入 Calkit 元数据 |

#### 4.3.2 核心流程

整个自动校准的顶层流程：

```text
[构造]  枚举设备 → 选择 → new CalDevice → 列出系数集（默认选第 1 项）
[配置]  每个端口选 Unused / Auto / Port n；1s 定时器轮询温度状态
[Start] determineAutoPorts
   ├─ 无 Auto 端口 ──────────────→ 直接 autoPortComplete
   └─ 有 Auto：acquireControl（暂停当前模式）
        全部 LibreCAL 端口 ← Open（基线）
        循环 i = 1..numPorts：
            单点扫描(最低频率, 最大功率, IFBW=100, 全端口激励)
            收到 1 点 → 端口 i ← Short，其余 ← Open，再扫
        判定：对每个 Auto 的 VNA 端口 p，取 S(p+1)(p+1)
              argmax_i |S_pp^(i) − S_pp^(基线)|，且偏差 > 0.25 才认定
        全部端口 ← None，releaseControl，autoPortComplete
[载入]  loadCoefficients：所需系数未加载 → device->loadCoefficientSets(所需端口)
        updateCoefficientsDone(success) → startCalibration
[校准]  startCalibration：
        1. 用系数 Touchstone 填充 Calkit（setMeasurement = 文件型标准件）
        2. cal->reset()；直接创建 Open/Short/Load/Through 测量对象
        3. 状态机（measurementsUpdated 驱动）：
           第 0 步 全端口←Open   → startMeasurements(所有 Open 测量)
           第 1 步 全端口←Short  → startMeasurements(所有 Short 测量)
           第 2 步 全端口←Load   → startMeasurements(所有 Load 测量)
           第 3+ 步 每对端口：两端←Through → startMeasurements(该 Through)
        4. 全部完成 → cal->compute(SOLT, 用到的端口) → "Calibration activated"
        5. 出错/中止：全端口←None，恢复 UI
```

其中自动端口识别的判定准则是：

\[ \text{LibreCAL 端口} = \arg\max_{1 \le i \le N} \left| S_{pp}^{(i)} - S_{pp}^{(0)} \right|, \quad \text{且} \max_i \left| S_{pp}^{(i)} - S_{pp}^{(0)} \right| > 0.25 \]

\( S_{pp}^{(0)} \) 是全部端口开路时的基线反射，\( S_{pp}^{(i)} \) 是第 i 个 LibreCAL 端口被切到短路时的反射。物理直觉：与被短路的端口相连的那条 VNA 端口反射系数会从接近 +1（开路）跳到接近 −1（短路），模值变化幅度大；未连接的端口几乎不变。0.25 是"确实连上了"的经验门槛。

注意一个测量细节：三类 SOL 测量是**并行**做的（一次扫描中所有端口的标准件同时就位，一次取回所有端口的反射），而 Through 必须**逐对**做（第 i 对直通时其它端口要设为 None 以免寄生路径参与），所以总步数是 \( 3 + \binom{n}{2} \)，双端口即 3 + 1 = 4 步。

#### 4.3.3 源码精读

**构造与设备接入**：构造函数搭建全部 UI 连接；设备下拉框变化时销毁旧 `CalDevice`、按序列号新建，失败则弹错误并把系数下拉框清空禁用。[librecaldialog.cpp:29-77](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L29-L77) 设备列表来自 4.1 的 `GetDevices`。[librecaldialog.cpp:87-90](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L87-L90) Start 按钮接到 `determineAutoPorts`，`autoPortComplete` 信号接到 `loadCoefficients`——这两个连接就是自动流程的骨架。[librecaldialog.cpp:92-93](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L92-L93) 1 秒定时器轮询设备状态。[librecaldialog.cpp:96-98](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L96-L98)

**三重前置校验**：`validatePortSelection` 检查端口映射无重复且至少用一个端口（Auto 记为 -1，暂不算重复）。[librecaldialog.cpp:110-146](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L110-L146) `validateCoefficients` 检查用到的每个端口的 Open/Short/Load 及每对端口的 Through 系数都存在且有点。[librecaldialog.cpp:148-215](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L148-L215) `updateCalibrationStartStatus` 汇总以上并加一道温度闸门：除非偏好设置允许（`Acquisition.allowUseOfUnstableLibreCALTemp`，默认 true，见 [preferences.h:284](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/preferences.h#L284)），温度未稳定就不给开始。[librecaldialog.cpp:239-244](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L239-L244)

**端口分配 UI**：每个 VNA 端口一行下拉框，取值 Unused/Auto/Port 1..N，写入 `portAssignment`（0 未用、-1 自动、>0 为 LibreCAL 端口号）。[librecaldialog.cpp:651-688](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L651-L688) 默认全选 Auto。[librecaldialog.cpp:684](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L684)

**自动端口识别入口**：`determineAutoPorts` 检查是否存在 Auto 端口；有则向活动驱动发 `acquireControl`（AppWindow 收到后会 deactivate 当前模式，见 [appwindow.cpp:355-358](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/appwindow.cpp#L355-L358)，防止模式层与对话框同时指挥设备），把全部 LibreCAL 端口设为 Open，开始第一次扫描。[librecaldialog.cpp:274-302](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L274-L302)

**单点探测扫描**：`startSweep` 配置一个"最低频率（LibreVNA 为 100kHz，下限为 0 时兜底 100kHz）、最大功率、IFBW 100Hz、单点、全端口激励"的 VNA 设置，并在设置的回调里才连接 `VNAmeasurementReceived` → `handleIncomingMeasurement`（DirectConnection，保证顺序、避免重复排队）。[librecaldialog.cpp:400-422](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L400-L422) `stopSweep` 断开连接并 `setIdle`。[librecaldialog.cpp:424-428](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L424-L428)

**逐端口推进与判定**：每收到一个测量点，先停扫、存入 `autoPortMeasurements`，然后切换下一个端口为 Short、把上一个恢复为 Open，再启动下一次扫描；收满 `numPorts+1` 个测量（1 个基线 + 每端口 1 个）后把全部端口复位为 None，对每个 Auto 的 VNA 端口取 `S<p+1><p+1>` 序列做最大偏差判定，超过 0.25 才填入对应下拉框，否则视为未连接。[librecaldialog.cpp:336-398](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L336-L398) 判定核心在 [librecaldialog.cpp:374-390](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L374-L390)，收尾发 `releaseControl` + `autoPortComplete`。[librecaldialog.cpp:392-393](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L392-L393)

**按需加载系数**：`loadCoefficients` 此时已不允许 Auto（再校验一次 `validatePortSelection(false)`），若所需系数尚未就绪，只把**用到的端口**传给 `loadCoefficientSets`——没必要为没接线的端口搬系数。[librecaldialog.cpp:304-334](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L304-L334) 系数到齐后（`updateCoefficientsDone`，QueuedConnection 切回 GUI 线程）取第一套系数并直接进入 `startCalibration`。[librecaldialog.cpp:42-60](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L42-L60)

**从系数到校准件**：`startCalibration` 先清空并重写 Calkit 的元数据——制造商写成 `LibreCAL (<系数集名>)`、序列号取设备序列号、描述注明自动创建。[librecaldialog.cpp:438-443](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L438-L443) 然后为每个用到的端口把 Open/Short/Load 系数 Touchstone 用 `setMeasurement` 塞进 `CalStandard::Open/Short/Load` 对象，为每对端口创建 `CalStandard::Through`。[librecaldialog.cpp:457-494](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L457-L494) 回忆 u9-l1：标准件"系数与测量文件二选一、测量优先"，这里全部走测量文件路线。

**从校准件到测量对象**：`cal->reset()` 后，直接向**私有成员** `cal->measurements` 里 `push_back` 新建的 `CalibrationMeasurement::Open/Short/Load/Through`——这是合法的，因为 `Calibration` 把 `LibreCALDialog` 声明为友元（[calibration.h:16](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.h#L16)，成员在 [calibration.h:171](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.h#L171)）。每个测量对象通过 `setStandard` 绑定到刚才创建的标准件；Through 还会按标准件名字的正反方向设置 `setReverseStandard`。[librecaldialog.cpp:496-548](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L496-L548)

**信号驱动的状态机**：`startNextCalibrationStep` 是一个按 `measurementsTaken` 计数分派的步进函数——前 3 步分别把所有在用端口设为 Open/Short/Load 并发起对应的一组测量；之后每步做一对端口的 Through（此时其它端口设 None）；全部做完则以 SOLT 类型、用到的端口调用 `cal->compute()`，成功即"Calibration activated"。[librecaldialog.cpp:562-617](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L562-L617) 推进机制是把 `measurementsUpdated` 信号连到这个 lambda（QueuedConnection，保证上一轮完全收尾后才进入下一步），中止则连 `measurementsAborted`。[librecaldialog.cpp:619-626](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L619-L626)

**管线的另一半（VNA 侧）**：对话框发出 `cal->startMeasurements(...)` 后，是 VNA 模式接手——停扫、清掉这批测量的旧数据、置 `calWaitFirst`、重启扫描，并在配置完成的回调里才打开 `calMeasuring` 采集开关（避免采到处理中的旧数据）。[vna.cpp:1406-1435](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1406-L1435) 这条连接在 VNA 构造时建立（[vna.cpp:113](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L113)）。数据在**最后一个平均圈**逐点喂给 `cal->addMeasurements`，扫到最后一点时关采集并调 `cal->measurementsComplete()`——正是它发出 `measurementsUpdated`，推状态机走下一步。[vna.cpp:1044-1051](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1044-L1051)、[calibration.cpp:1944-1947](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L1944-L1947)

**两个入口**：除 VNA 菜单 `Calibration → Electronic Calibration`（[vna.cpp:129-133](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L129-L133)）外，Calibration Measurements 窗口里也有一个 "Electronic Calibration" 按钮（[calibration.cpp:692-695](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/calibration.cpp#L692-L695)）。

**硬件存在与否的检测点汇总**（无硬件走读时重点看这几处）：

1. 枚举层：`USBDevice::GetDevices()` 返回空集 → 设备下拉框为空（[librecaldialog.cpp:87-90](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L87-L90)）。
2. 连接层：`new CalDevice(serial)` 内部 `*IDN?` 前缀不符或找不到设备会抛异常 → 对话框捕获并弹 "Failed to connect"（[librecaldialog.cpp:34-39](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L34-L39)）。
3. 状态层：`device == nullptr` 时 Start 永远禁用（[librecaldialog.cpp:220-224](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L220-L224)）。
4. 运行中断连：`USBDevice::communicationFailure` → `CalDevice::disconnected`（[caldevice.cpp:111](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L111)）——注意对话框**没有**连接这个信号，中途拔线的表现是后续命令超时、流程停摆，这是值得改进的边角。

#### 4.3.4 代码实践

**实践目标**：把"使用 LibreCAL 完成一次全双端口校准"整理成一份带类名标注的软件流程步骤清单，并标出硬件存在性检查点（本讲的指定实践任务）。

**操作步骤**：

1. 先自己从 `ui->start` 被点击开始，沿代码走一遍，按下表逐行填写"步骤 / 负责的类·函数 / 关键源码位置"。
2. 对照下面的参考答案修订。
3. 无硬件时：启动 GUI 打开对话框，逐一验证 4.3.3 末尾列出的 4 个检测点的实际表现。

**参考答案（双端口、两端口均设 Auto、系数集 FACTORY）**：

| # | 步骤 | 类·函数 | 源码位置 |
|---|---|---|---|
| 1 | 枚举 LibreCAL | `USBDevice::GetDevices` → `SearchDevices` | [librecaldialog.cpp:87-90](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L87-L90) |
| 2 | 连接并握手（IDN/固件/端口数） | `CalDevice::CalDevice` | [caldevice.cpp:76-114](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/caldevice.cpp#L76-L114) |
| 3 | 列出并默认选择系数集 | `CalDevice::getCoefficientSetNames`（`:COEFF:LIST?`） | [librecaldialog.cpp:62-71](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L62-L71) |
| 4 | 等待温度稳定（1s 轮询） | `LibreCALDialog::updateDeviceStatus` → `CalDevice::stabilized` | [librecaldialog.cpp:253-272](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L253-L272) |
| 5 | 点击 Start，识别 Auto 端口 | `determineAutoPorts`：`acquireControl` → 全端口 Open | [librecaldialog.cpp:274-302](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L274-L302) |
| 6 | 单点探测扫描 ×(端口数+1) | `startSweep` + `handleIncomingMeasurement`（切 Short/Open） | [librecaldialog.cpp:400-422](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L400-L422)、[336-347](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L336-L347) |
| 7 | 最大偏差判定端口映射，复位端口 | 同上（阈值 0.25）→ `releaseControl` | [librecaldialog.cpp:374-393](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L374-L393) |
| 8 | 按需加载系数（仅用到的端口） | `loadCoefficients` → `CalDevice::loadCoefficientSets`（后台线程） | [librecaldialog.cpp:304-334](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L304-L334) |
| 9 | 系数到齐，开始校准 | `updateCoefficientsDone` 回调 → `startCalibration` | [librecaldialog.cpp:42-60](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L42-L60) |
| 10 | 用系数 Touchstone 填充 Calkit | `CalStandard::Open/Short/Load/Through::setMeasurement` | [librecaldialog.cpp:457-494](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L457-L494) |
| 11 | 创建测量对象（友元直接入列） | `CalibrationMeasurement::*` + `cal->measurements.push_back` | [librecaldialog.cpp:496-548](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L496-L548) |
| 12 | 第 0 步：全端口 Open，测反射 | `startNextCalibrationStep` → `emit cal->startMeasurements` | [librecaldialog.cpp:566-570](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L566-L570) |
| 13 | VNA 停扫/清数/重配/开采集 | `VNA::StartCalibrationMeasurements` | [vna.cpp:1406-1435](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1406-L1435) |
| 14 | 末平均圈逐点累积，扫完上报 | `cal->addMeasurements` → `measurementsComplete` → `measurementsUpdated` | [vna.cpp:1044-1051](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/VNA/vna.cpp#L1044-L1051) |
| 15 | 第 1/2 步：Short、Load 各重复 12–14 | 同上 | [librecaldialog.cpp:571-577](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L571-L577) |
| 16 | 第 3 步：P1↔P2 Through（其余 None） | 状态机 default 分支 | [librecaldialog.cpp:579-613](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L579-L613) |
| 17 | 求解并激活 SOLT | `Calibration::compute`（u9-l2 的求解器） | [librecaldialog.cpp:585-601](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L585-L601) |
| 18 | 端口复位 None，恢复 UI | 状态机收尾 | [librecaldialog.cpp:603-607](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L603-L607) |

**需要观察的现象**：无硬件时对话框停在第 3 行所述的"未连接"状态；有硬件时进度条按 3+1=4 步推进。

**预期结果**：双端口校准实际执行 4 组测量（Open/Short/Load/Through），进度百分比按 `measurementsTaken * 100 / (3 + through数)` 递增。没有硬件时以上第 5 步起无法实际触发，结论**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：自动端口探测为什么用"最低频率 + 最大功率 + IFBW 100Hz"的单点设置？

**答案**：目的是让"开路→短路"的反射差异尽可能明显、一次测量尽可能快。低频下传输线和夹具的相位旋转小，开路 (+1) 与短路 (−1) 的差异不会被长电缆的相移冲淡；最大功率提高信噪比；100Hz 中频带宽已经足够窄（单点、全端口激励），又不用等太久。设置代码在 [librecaldialog.cpp:404-418](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L404-L418)。

**练习 2**：为什么 `measurementsUpdated` → `startNextCalibrationStep` 要用 `Qt::QueuedConnection`？

**答案**：`measurementsComplete` 是 VNA 在处理最后一个数据点时同步调用的，此刻调用栈还在数据管线深处。QueuedConnection 把"下一步"排到事件队列，让当前数据点的处理（校准修正、写入 Trace 等）完全结束后再切换 LibreCAL 标准件、重配扫描，避免重入和状态竞争（[librecaldialog.cpp:620](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/Calibration/LibreCAL/librecaldialog.cpp#L620)）。

**练习 3**：如果用户在自动校准中途拔掉 LibreCAL，会发生什么？代码有什么可改进之处？

**答案**：`setStandard` 返回的 `bool` 在对话框里全部被忽略，`USBDevice::communicationFailure` → `CalDevice::disconnected` 信号也没有被对话框连接。所以表现是：命令超时（默认 2 秒）后流程卡住、UI 停在"Taking calibration measurements..."，不会自动清理端口或恢复界面。改进方向：连接 `disconnected` 信号，在槽里中止状态机并提示用户。（这正说明"电子校准件的单点故障不可见"是真实风险，需要在软件层补防御。）

## 5. 综合实践

**任务：编写一份《LibreCAL 双端口自动校准时序与故障分支文档》。**

把 4.3.4 的主流程表扩展成一份完整文档，要求包含：

1. **主时序**：18 步主流程（可合并同类步骤），每步标注触发信号/调用方、负责的类、LibreCAL 端口状态、VNA 设备状态。提示：LibreCAL 端口状态列在自动识别阶段（全 Open → 逐个 Short）与校准阶段（全 Open → 全 Short → 全 Load → P1P2 直通）分别记录。
2. **故障分支**：至少覆盖 5 种失败——找不到设备、`*IDN?` 异常、系数缺失（`validateCoefficients` 拦截）、温度不稳定（偏好禁止时拦截）、测量中途 `measurementsAborted`（用户在校准进度框点了取消）。每种给出：在哪一行被拦截、用户看到什么、状态如何恢复。
3. **对比实验（可选，需硬件）**：同一台 VNA、同一段电缆，分别用机械校准件手动 SOLT 与 LibreCAL 自动 SOLT 各校一次，测量同一个 50Ω 负载的 S11，比较两者的残余误差轨迹，写 300 字分析（重点：电子校准的开关直通路径 vs 机械 Through 的差异）。
4. **评估结论**：结合 4.3.1 的对比表，给出"什么场景该用电子校准、什么场景必须回到机械件"的判断（提示：例行产线测试 vs 计量级验收）。

无硬件时第 3 项跳过，第 1、2、4 项纯代码走读即可完成——所有分支都能在源码中找到明确的拦截行。

## 6. 本讲小结

- LibreCAL 是独立的 USB 电子校准件，GUI 为它开了**不经 DeviceDriver 的第二条 libusb 通路**：`USBDevice` 管字节（发现/连接/按行收发），`CalDevice` 管命令（SCPI 风格文本协议），`LibreCALDialog` 管流程。
- `USBDevice` 是单命令在途的同步阻塞通道：bulk 端点 0x03/0x83、接口 2、CRLF 行协议；拆行在 libusb 事件线程，调用者线程靠条件变量等行——与主设备驱动的异步队列模型形成鲜明对照。
- LibreCAL 的"协议文档"就是 `CalDevice` 的方法集：端口切换 `:PORT`、状态 `:TEMP?/:TEMPerature:STABLE?`、系数读写 `:COEFF:*`；系数读取分慢（逐点查询）与快（≥0.2.1 整段 Touchstone 文本流）两代，写回只传被修改项。
- 自动端口识别是一个巧妙的物理实验：全端口开路做基线，逐端口短路看哪个 VNA 端口的 \( S_{pp} \) 跳变最大（阈值 0.25），一次探测即可建立端口映射。
- 自动校准**零新机制**：它复用 `Calibration::startMeasurements` → `VNA::StartCalibrationMeasurements` → `measurementsUpdated` 的既有回路，只是把"用户点按钮"换成"信号驱动状态机"；Calkit 用系数 Touchstone 以"测量文件"方式填充，最后仍是 u9-l2 的 SOLT 求解器收尾。
- 电子校准的风险（开关可靠性、系数错误不可见）由软件防线兜底：温度稳定闸门、系数完备性校验、出厂系数召回机制；但运行中断连的处理仍是薄弱点。

## 7. 下一步学习建议

- **下一讲 u9-l4（去嵌入框架）**：校准解决的是仪器自身误差，去嵌入解决的是夹具效应——两者在数据管线上一前一后，学完本讲正好顺势进入 `VNA/Deembedding` 的插件式设计。
- **回看 u9-l2 与 u11-l1**：本讲第 17 步调用的 `Calibration::compute` 的求解细节在 u9-l2；`LibreVNA-Test/calibrationtests.cpp` 里有对校准数学的单元测试，可以验证你对"系数→误差项"的理解。
- **延伸阅读**：LibreCAL 的固件与硬件在独立仓库 github.com/jankae/LibreCAL，其 `Documentation/manual.pdf` 有系数集概念的权威说明；对照本讲 4.2 的命令表阅读设备端实现，是一次很好的"两端协议"练习。
- **动手方向**：给 `LibreCALDialog` 连上 `CalDevice::disconnected` 信号并实现优雅中止（见练习 3），这是难度适中、真正有价值的贡献点。
