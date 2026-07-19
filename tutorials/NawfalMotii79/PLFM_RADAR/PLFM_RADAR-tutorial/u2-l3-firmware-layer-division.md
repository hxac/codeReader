# 三层固件分工：FPGA / STM32 / GUI

## 1. 本讲目标

AERIS-10 雷达的固件不是一坨代码，而是由 **三个性格迥异的执行体** 协作完成的系统：

- **FPGA**（`radar_system_top.v`）—— 并行、确定、纳秒级实时；
- **STM32 微控制器**（`main.cpp`）—— 串行、有主循环、擅长挂载各种外设；
- **Python GUI**（`v7/dashboard.py`）—— 跑在 PC 上、面向人、刷新慢但算力大。

本讲学完后，你应当能够：

1. 说清楚 FPGA、STM32、GUI **各自的实时性要求与职责边界**；
2. 说出三层之间通过 **USB 命令、GPIO、SPI/I2C** 这三类接口如何协作；
3. 拿到一个新功能需求时，能 **判断它该放在哪一层**，并说出理由。

本讲承接 [u2-l1 整体架构](u2-l1-system-architecture.md) 与 [u2-l2 信号处理流水线](u2-l2-signal-processing-pipeline.md)：前两讲告诉你「系统有哪些子系统」「信号从发射到显示经过哪些阶段」，本讲回答「这些事情分别由谁来干、怎么配合」。

## 2. 前置知识

读本讲前，建议先建立以下几个直觉（不涉及代码细节）：

- **实时性（realtime）≠ 速度快**。实时指的是「必须在确定的截止时间内完成」，否则结果就作废。雷达对回波的处理是实时的——一个 ADC 样本到了，下一个时钟周期就必须接住，不能等。
- **FPGA 没有「主循环」也没有「操作系统」**。它是一块被描述成电路的芯片：你用 Verilog 描述好「在时钟上升沿做什么」，综合后它就**永远**、**并行地**这样做。它擅长流水线式的、逐样本的处理，不擅长「先问一下 GPS 有没有信号、再读一下温度、再决定怎么办」这种充满分支和等待的逻辑。
- **STM32 是一颗通用 CPU**，跑 C/C++，有一个 `while(1)` 主循环，能方便地用 HAL 库操作 I2C/SPI/UART，能延时、能轮询。它反应慢（微秒级），但灵活、能调度、能做安全保护。
- **GUI 跑在 PC 上**，有文件系统、有 numpy/scipy、能联网加载地图，但它是「人眼节奏」的（几十毫秒刷新一次），不可能去处理 400 MHz 的 ADC 数据流。

> 一句话记忆：**FPGA 管波形与数字处理，STM32 管硬件管家与安全，GUI 管人。**

## 3. 本讲源码地图

本讲涉及的关键文件，以及它们在本讲中扮演的角色：

| 文件 | 层 | 本讲用途 |
|------|----|----------|
| [README.md](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/README.md) | — | README 用文字列出了 FPGA / STM32 / GUI 各自的职责清单，是三层分工的「官方说法」 |
| `9_Firmware/9_2_FPGA/radar_system_top.v` | FPGA | 顶层模块，看它的**端口列表**就能知道 FPGA 和谁相连；看它的**命令解码**就知道 GUI 怎么控制它 |
| `9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp` | STM32 | 入口函数，看 `main()` 的初始化顺序与 `while(1)` 主循环，就知道 STM32 在管什么 |
| `9_Firmware/9_3_GUI/v7/dashboard.py` | GUI | 主窗口，六个标签页就是 GUI 职责的目录 |
| `9_Firmware/9_3_GUI/radar_protocol.py` | GUI↔FPGA | `Opcode` 枚举与 `build_command`，是 GUI 控制 FPGA 的「共同语言」 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先分别讲 FPGA、STM32、GUI 的职责（4.1–4.3），再用 4.4 把它们之间的接口串起来。每个模块都配有源码精读和小实践。

### 4.1 FPGA 职责：高速实时信号处理与 USB

#### 4.1.1 概念说明

FPGA（本工程用 Xilinx Artix-7 `XC7A50T`）在系统里扮演 **「确定性的数字信号处理机」**：

- 它直接接到模拟/射频世界的物理引脚上：DAC（发波形）、ADC（收回波）、混频器使能、ADAR1000 相移器加载、USB PHY。
- 它内部有**多个时钟域**（100 MHz 系统域、120 MHz DAC 域、400 MHz ADC 域、USB 时钟域），用专门的「跨时钟域（CDC）」电路保证数据在域之间安全传递。
- 它**逐样本、流水线式**地完成 [u2-l2](u2-l2-signal-processing-pipeline.md) 中的整条接收链：DDC → 匹配滤波 → 距离抽取 → MTI → Doppler → CFAR。
- 它还兼任 **USB 数据接口**，把处理后的 Range-Doppler 数据打包发给上位机。

FPGA **不擅长**：调度一堆慢速外设（GPS、温度、电机）、做电源时序、跑浮点文件系统——这些是 STM32 的活。

#### 4.1.2 核心流程

FPGA 内部的数据流动可以概括为两条对称的链路加一个控制面：

```text
【发射链 TX】 chirp LUT ──▶ DAC ──▶ 混频器/ADAR1000 load（送往射频前端）
【接收链 RX】 ADC ──▶ DDC ──▶ 匹配滤波 ──▶ 距离抽取 ──▶ MTI
             ──▶ Doppler FFT ──▶ DC notch ──▶ CFAR ──▶ 打包 ──▶ USB
【控制面】   USB 收到 4 字节命令 {opcode,addr,value}
             ──▶ case(opcode) ──▶ 写入 host_* 配置寄存器 ──▶ 改变 DSP 行为
```

关键点：**DSP 的行为是可配置的**，但配置的「执行者」是 FPGA 自己；主机只负责「写寄存器」，FPGA 内部电路立刻按新寄存器值运转。

#### 4.1.3 源码精读

先看 FPGA 顶层模块的端口声明，这是判断「FPGA 和谁相连」的最快途径：

[9_Firmware/9_2_FPGA/radar_system_top.v:L22-L135](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L22-L135) —— 这是 FPGA 的全部物理引脚。端口列表里你能看到三类对象：模拟/射频相关（`dac_data`、`adc_d_p/n`、`rx_mixer_en`、`adar_*`），USB 相关（`ft601_*` 与 `ft_*`），以及 **与 STM32 的控制线**（`stm32_new_chirp`、`stm32_new_elevation`、`stm32_new_azimuth`、`stm32_mixers_enable`）。看端口就知道：FPGA 同时挂在射频、USB、STM32 三条总线上。

特别注意 130–134 行的三个 GPIO 输出，它们是 **FPGA 反向通知 STM32** 的通道：

```verilog
output wire gpio_dig5,  // DIG_5: AGC 饱和标志（1=检测到削波）
output wire gpio_dig6,  // DIG_6: AGC 使能标志（镜像 host_agc_enable）
output wire gpio_dig7   // DIG_7: 保留（拉低）
```

[9_Firmware/9_2_FPGA/radar_system_top.v:L1043-L1045](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L1043-L1045) —— 这三行的赋值逻辑说明 `gpio_dig5` 在「本帧出现饱和」时拉高，把 FPGA 内环 AGC 的状态用一根 GPIO 线送给 STM32（这正是 4.4 要讲的「跨层 AGC」外环入口）。

再看控制面——主机命令如何变成 FPGA 内部寄存器：

[9_Firmware/9_2_FPGA/radar_system_top.v:L949-L1002](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L949-L1002) —— 这是一个 `case(usb_cmd_opcode)` 译码表，把每个 opcode 翻译成对一个 `host_*` 寄存器的写入。例如 `8'h23` 写 `host_cfar_alpha`（CFAR 门限系数），`8'h28` 写 `host_agc_enable`。这张表是 GUI 和 FPGA 之间的**契约**，后面 4.3 会看到 Python 端有一份与之逐行对应的枚举。

最后看一个 DSP 子模块的「主机可配置性」如何落地：

[9_Firmware/9_2_FPGA/radar_system_top.v:L620-L651](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L620-L651) —— `cfar_inst` 例化时，它的 `cfg_guard_cells`、`cfg_alpha`、`cfg_cfar_mode`、`cfg_cfar_enable` 等端口直接连到上面的 `host_cfar_*` 寄存器。也就是说：GUI 改一个寄存器值，CFAR 电路下一个帧就按新参数工作——这就是 FPGA 作为「可配置实时处理机」的典型工作方式。

#### 4.1.4 代码实践

**实践目标**：用「读端口」的方式，确认 FPGA 与外界的全部连接对象。

**操作步骤**：

1. 打开 [radar_system_top.v 端口声明](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L22-L135)。
2. 数一下模块顶部注释提到的时钟域有几个（答案在 L12–L19 的注释里）。
3. 把端口按「射频/模拟」「USB」「STM32 控制」「状态输出」四类分组，各列 2–3 个代表引脚。

**需要观察的现象**：`stm32_*` 一组引脚的存在，证明 FPGA 不是孤立运行的，它的扫描节奏由 STM32 的 GPIO 触发。

**预期结果**：时钟域有 3 个（`clk_100m`、`clk_120m_dac`、`ft601_clk`，见 L24–L26）；`stm32_new_chirp`/`stm32_new_elevation`/`stm32_new_azimuth`/`stm32_mixers_enable` 属于 STM32 控制类（L75–L78）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 ADC 数据（400 MHz）不能像 `stm32_mixers_enable` 那样，直接用两级触发器同步到 100 MHz 域？

> **答案**：`stm32_mixers_enable` 是慢速电平信号，两级同步够用；ADC 数据是高速、连续的多比特总线，直接采样会同时面临「采样定理」和「多比特同时跳变（skew）」问题，必须用 **DDC 里的 CIC 抽取** 把速率真正降下来（详见 [u4-l1](u4-l1-ddc-digital-downconversion.md)），而不是简单同步。

**练习 2**：主机命令 `8'h25` 会写哪个寄存器？它对 DSP 行为有什么影响？

> **答案**：写 `host_cfar_enable`（见 L983）。为 1 时启用 CA-CFAR 检测，为 0 时回退为简单幅度门限（向后兼容）。

---

### 4.2 STM32 职责：系统管理者与安全权威

#### 4.2.1 概念说明

STM32（本工程用 `STM32F746xx`）是全板 **最早醒来、最后睡去** 的「管家」。它不做逐样本的信号处理（那是 FPGA 的事），而是承担三类 FPGA 不擅长的职责：

1. **硬件生命周期管理**：电源上下电时序、OCXO 预热、FPGA 复位、看门狗。
2. **慢速外设调度**：时钟发生器（AD9523）、频综（ADF4382）、相移器（ADAR1000）、PA 偏置（DAC5578/ADS7830）、GPS（UM982）、IMU（GY-85）、气压计（BMP180）、温度、步进电机。
3. **安全与监控**：周期性健康检查、过温/过流紧急停机（`Emergency_Stop`）、IWDG 硬件看门狗。

它还充当 FPGA 与 GUI 之间的 **中转**：用 GPIO 触发 FPGA 的扫描节奏，用 USB CDC 给 GUI 推送系统状态和 GPS 位置。

#### 4.2.2 核心流程

STM32 的 `main()` 分两个阶段：**长初始化**（一次性，上电到主循环前）和 **主循环**（`while(1)`，永远跑）。

初始化阶段按物理依赖顺序推进（这是阅读嵌入式代码的关键直觉——**先有电、再有时钟、再配置使用时钟的芯片、最后才打开射频**）：

```text
HAL_Init / SystemClock_Config
  └─ MX_*_Init（GPIO/TIM/I2C/SPI/UART/USB/IWDG 外设句柄）
  └─ OCXO 预热 3 分钟（循环喂看门狗，否则 IWDG ~4s 复位）
  └─ AD9523 上电时序 + configure_ad9523()（给全系统分时钟）
  └─ FPGA 上电时序：1.0V → 1.8V → 3.3V
  └─ IMU / BMP180 / ADF4382 LO 锁定 / ADAR1000 上电+校准
  └─ GPS 初始化 + 步进电机指北
  └─ PA Idq 闭环校准（DAC5578 设 Vg，ADS7830 读 Idq，逼近目标值）
  └─ 复位 FPGA、使能混频器、发送初始状态给 GUI
```

主循环每轮做这几件事：

```text
while (1):
  ① checkSystemHealthStatus()   // 健康/安全检查，失败则进入 SAFE MODE
  ② um982_process()             // 非阻塞解析 GPS NMEA
  ③ 每 5s 查 ADF4382 锁定状态
  ④ 每 5s 读 8 路温度 + 16 路 Idq；过温则开风扇
  ⑤ runRadarPulseSequence()     // 用 GPIO toggle 驱动 FPGA 的扫描节奏
  ⑥ 外环 AGC：读 DIG_6 决定是否启用，读 DIG_5 调整 ADAR1000 增益
  ⑦ HAL_IWDG_Refresh()          // 喂硬件看门狗
```

#### 4.2.3 源码精读

先看 STM32 持有哪些外设总线——这决定了它能挂多少器件：

[9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp:L111-L122](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L111-L122) —— 三个 I2C（`hi2c1/2/3`）、两个 SPI（`hspi1/4`）、两个定时器（`htim1/3`）、两个 UART（`huart5` 给 GPS、`huart3` 给调试串口）。I2C1 挂 DAC5578（设 PA 栅压 Vg），I2C2 挂 ADS7830（读 Idq 与温度），SPI4 配 AD9523/ADF4382，SPI1 配 ADAR1000。

再看初始化的调用顺序，体会「按物理依赖推进」：

[9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp:L1396-L1408](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1396-L1408) —— 一连串 `MX_*_Init()` 把所有外设句柄配好，最后 `MX_IWDG_Init()` 启动硬件看门狗。

[9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp:L1421-L1429](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1421-L1429) —— OCXO 预热 180 秒。注意它**不能用** `HAL_Delay(180000)`，因为那会让 IWDG 超时复位；改为「每秒喂一次狗」的循环。这是嵌入式里很经典的坑。

[9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp:L1474-L1485](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1474-L1485) —— FPGA 上电时序：先 1.0V（核心），再 1.8V，再 3.3V（IO）。顺序错了会损坏 FPGA，所以这种事**必须由最先醒来的 STM32 用 GPIO 严格控制**，不能交给 FPGA 自己。

最后看主循环里 STM32 与 FPGA 的「GPIO 对话」：

[9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp:L2180-L2209](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L2180-L2209) —— 每轮调用 `runRadarPulseSequence()` 用 GPIO toggle 告诉 FPGA「新 chirp / 新仰角 / 新方位」（对应 FPGA 端的 `stm32_new_chirp` 等输入）；紧接着是 **外环 AGC**：读 `FPGA_DIG6`（AGC 使能）、`FPGA_DIG5_SAT`（饱和标志），据此调整 ADAR1000 增益。最后 `HAL_IWDG_Refresh()` 喂狗。

> 这段代码恰好把「STM32 三个职责」串在一帧里：扫描调度（GPIO）+ 外设调度（ADAR1000 增益）+ 安全（喂狗）。

#### 4.2.4 代码实践

**实践目标**：确认 STM32 配置了哪些总线、各自挂了什么器件。

**操作步骤**：

1. 打开 [main.cpp 外设句柄](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L111-L122)，列出 `hi2c1/2/3`、`hspi1/4`、`huart5/uart3` 各对应哪条总线。
2. 全文搜索 `DAC5578_Init`、`ADS7830_Init`、`ADF4382A_Manager_Init`、`AD9523`，看每个器件挂在哪条总线上（提示：看传给 `*_Init` 的第几个参数是 `hi2c*` 还是 `hspi*`）。

**需要观察的现象**：同一类器件（如两片 ADS7830）会挂在同一条 I2C 上，靠**不同 I2C 地址**区分。

**预期结果**：DAC5578 用 I2C1（地址 0x48/0x49），ADS7830 用 I2C2（地址 0x48/0x4A），温度 ADS7830 用 I2C2（地址 0x49），AD9523/ADF4382 用 SPI4，ADAR1000 用 SPI1，UM982 GPS 用 UART5。

#### 4.2.5 小练习与答案

**练习 1**：为什么电源上下电时序由 STM32 而不是 FPGA 控制？

> **答案**：FPGA 本身需要按 1.0→1.8→3.3V 的顺序上电才能正常工作，在它「自己还没上电」时不可能让它去控制上电。STM32 上电即可用 GPIO 拉高/拉低电源使能脚，所以它是天然的唯一选择。这也是 STM32 必须最先醒来的根本原因。

**练习 2**：`runRadarPulseSequence()` 里为什么对 `GPIOD` 的 pin 8/9/10 用 `HAL_GPIO_TogglePin`（翻转）而不是 `WritePin`（写固定电平）？

> **答案**：因为 FPGA 端的 `stm32_new_chirp` 等输入是用**边沿**触发新事件的（每翻转一次 = 一个新事件）。用 Toggle 天然产生边沿，且 STM32 不用记当前电平，简化了状态管理。

---

### 4.3 GUI 职责：可视化、目标跟踪与寄存器控制

#### 4.3.1 概念说明

Python GUI（V7，基于 PyQt6）跑在 PC 上，是 **面向人的那一层**。它的实时性要求最低（人眼节奏，约 10 Hz 刷新），但算力和存储最充裕。它承担：

- **可视化**：Range-Doppler 热力图、地图、AGC 实时曲线、诊断指示灯。
- **目标跟踪**：在已经 CFAR 后的数据上做聚类（DBSCAN）、关联与 Kalman 滤波——这类重浮点、可变分支的计算放在 FPGA 太奢侈，放在 PC 正合适。
- **寄存器控制**：把 FPGA 的所有 `host_*` 配置寄存器封装成图形控件，背后翻译成 4 字节 USB 命令发给 FPGA。
- **回放与仿真**：用 `SoftwareFPGA` + `ReplayEngine` 在没有真硬件时也能开发。

**关键认知**：GUI **不接触原始 ADC 数据**，它消费的是 FPGA 已经做完 CFAR、打包好的 Range-Doppler 帧（64 距离 × 32 多普勒）。

#### 4.3.2 核心流程

GUI 启动后，主窗口 `RadarDashboard` 构建六个标签页，并启动一条后台数据线程：

```text
连接（FT2232H / FT601 / Mock / Replay）
  └─ RadarDataWorker 后台线程：从 USB 读字节流
        └─ 解析 11 字节数据包 ──▶ 拼成 64×32 RadarFrame ──▶ 入队
  └─ 主线程 QTimer（100 ms）：
        └─ 取帧 ──▶ RadarProcessor（聚类/跟踪） ──▶ 刷新热力图 + 目标表
  └─ FPGA Control 标签页：用户改控件
        └─ build_command(opcode, value) ──▶ 4 字节 USB 写
  └─ AGC Monitor：解析状态包的 agc_* 字段 ──▶ 画曲线
```

注意三个层次的时间尺度：USB 读是连续的后台线程，界面刷新是 100 ms 的定时器，用户操作是「点一下按钮」级别的偶尔事件。

#### 4.3.3 源码精读

GUI 的职责最直观地体现在它的模块文档字符串里：

[9_Firmware/9_3_GUI/v7/dashboard.py:L1-L24](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L1-L24) —— 文档明确列出六个标签页：Main View（Range-Doppler 图）、Map View（地图）、**FPGA Control（全部 27 个 opcode 的寄存器面板）**、**AGC Monitor（实时 AGC 曲线）**、Diagnostics（连接/包统计/自测试/日志）、Settings。这六个页就是 GUI 职责的「目录」。注意末尾一句：「The old STM32 magic-packet start flow has been removed」——旧版 GUI 要先给 STM32 发一个魔数启动包，V7 已改为直接控制 FPGA 寄存器。

六个标签页的添加位置一目了然：

- [dashboard.py:L504](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L504) — Main View
- [dashboard.py:L610](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L610) — Map View
- [dashboard.py:L848](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L848) — FPGA Control
- [dashboard.py:L993](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L993) — AGC Monitor
- [dashboard.py:L1091](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L1091) — Diagnostics
- [dashboard.py:L1197](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L1197) — Settings

GUI 怎么把「用户改控件」翻译成 FPGA 命令？关键是 `Opcode` 枚举与 `build_command`：

[9_Firmware/9_3_GUI/radar_protocol.py:L53-L103](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L53-L103) —— `Opcode(IntEnum)` 把每个 opcode 起了名字。**这份枚举必须和 [radar_system_top.v 的 case 表](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L949-L1002) 逐行对应**——这是 GUI 与 FPGA 之间的硬契约（项目还专门有「跨层契约测试」来保证两者不漂移，见 [u11-l3](u11-l3-cross-layer-contract-tests.md)）。

[9_Firmware/9_3_GUI/radar_protocol.py:L168-L175](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L168-L175) —— `build_command` 把 `{opcode, addr, value}` 拼成一个 32 位字、按大端打包成 4 字节。这就是 GUI 发给 FPGA 的命令物理形态。例如设置 CFAR alpha=0x30：`build_command(Opcode.CFAR_ALPHA, 0x30)` → 字节 `23 00 00 30`。

最后看 GUI 主动发命令的入口：

[9_Firmware/9_3_GUI/v7/dashboard.py:L1276-L1283](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L1276-L1283) —— `_send_custom_command` 从界面读 opcode（十六进制）和 value（十进制），调用 `_send_fpga_cmd`。FPGA Control 标签页里所有预设控件最终都汇聚到这条路径。

#### 4.3.4 代码实践

**实践目标**：亲手构造一条 GUI→FPGA 命令，验证 opcode 映射。

**操作步骤**：

1. 打开 [Opcode 枚举](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L53-L103)，查到 `CFAR_ALPHA = 0x23`。
2. 在 Python 里执行（**示例代码，非项目原有**）：

   ```python
   from radar_protocol import RadarProtocol, Opcode
   RadarProtocol.build_command(Opcode.CFAR_ALPHA, 0x30)
   # 预期: b'\x23\x00\x00\x30'
   ```
3. 对照 [FPGA case 表 L981](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L981)，确认 `8'h23` 确实写 `host_cfar_alpha`。

**需要观察的现象**：4 字节里第一字节是 opcode、最后两字节是 value，中间两字节是 addr（通常 0）。

**预期结果**：`b'\x23\x00\x00\x30'`，与 FPGA 译码一致。如果将来运行失败，请标注「待本地验证」（本环境未安装 GUI 运行依赖）。

#### 4.3.5 小练习与答案

**练习 1**：为什么把 DBSCAN 聚类和 Kalman 跟踪放在 GUI，而不是 FPGA？

> **答案**：这两者都是**数据依赖的、分支密集的浮点算法**（聚类个数随场景变化、卡尔曼增益是浮点矩阵），用 FPGA 实现面积大、收益低；而 PC 上有现成的 numpy/sklearn/filterpy，且对延迟不敏感（10 Hz 足够）。这正是「按实时性与算力特性分层」的典型决策。

**练习 2**：GUI 读到的 `agc_saturation_count` 来自哪里？经过了哪几层？

> **答案**：FPGA 内 `rx_agc_saturation_count`（接收 AGC 统计）→ 打包进状态包 word 4 → USB 上传 → GUI `parse_status_packet` 填入 `StatusResponse.agc_saturation_count`（见 [radar_protocol.py:L145-L149](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L145-L149)）→ AGC Monitor 画曲线。即 FPGA→GUI 的单向只读通路。

---

### 4.4 三层如何协作：USB 命令 / GPIO / SPI-I2C 三类接口

#### 4.4.1 概念说明

把 4.1–4.3 串起来，三层之间其实只有 **三类物理接口**，理解了它们就理解了整个系统的协作方式：

| 接口 | 谁和谁 | 方向 | 用途 | 时间尺度 |
|------|--------|------|------|----------|
| **USB 命令/数据** | GUI ↔ FPGA | 双向 | GUI 写寄存器、FPGA 回状态包与 Range-Doppler 数据 | 帧级（ms） |
| **GPIO 控制线** | STM32 ↔ FPGA | 双向 | STM32 触发扫描（chirp/仰角/方位/混频器）；FPGA 回报 AGC 饱和（DIG_5/6） | 事件级（µs） |
| **I2C/SPI/UART** | STM32 ↔ 外设芯片 | 双向 | 配置 AD9523/ADF4382/ADAR1000/DAC5578/ADS7830/GPS/IMU | 慢速外设 |

注意一个容易混淆的点：**GUI 和 STM32 之间也有一条 USB**（USB CDC），但那条只传系统状态字符串和 GPS 二进制包，**不传雷达数据**。雷达数据走的是 GUI↔FPGA 的 USB（FT2232H/FT601）。两条 USB 物理上是分开的。

#### 4.4.2 核心流程

用「自动增益控制（AGC）」这个功能把三类接口全部走一遍，就能看到三层如何环环相扣：

```text
① GUI 在 FPGA Control 页勾选「AGC Enable」
     └─ build_command(0x28, 1) ──USB──▶ FPGA 写 host_agc_enable=1
② FPGA 内环 rx_gain_control 每帧根据饱和/峰值调增益
③ FPGA 把饱和统计放进状态包（USB 回 GUI，供 AGC Monitor 显示）
   同时把饱和标志拉到 gpio_dig5，把 agc_enable 镜像到 gpio_dig6
④ STM32 主循环读 DIG_6（是否启用）→ 读 DIG_5（是否饱和）
     └─ 若饱和，通过 SPI1 调 ADAR1000 的 VGA 公共增益 ──SPI──▶ ADAR1000
⑤ ADAR1000 改变模拟增益，影响下一帧的 ADC 输入 ──▶ 回到 ②
```

这就是 README 里说的 **「Hybrid AGC — cross-layer FPGA/STM32/GUI loop」**：FPGA 做快内环（每帧），STM32 做慢外环（每帧读 GPIO、偶尔调 ADAR1000），GUI 只观测与使能。三层各按自己的时间尺度参与同一个控制目标。

#### 4.4.3 源码精读

**接口一：GPIO 控制线（STM32→FPGA 扫描触发）**

- FPGA 端输入声明：[radar_system_top.v:L75-L78](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L75-L78) `stm32_new_chirp` 等。
- STM32 端翻转：[main.cpp:L487](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L487) `HAL_GPIO_TogglePin(GPIOD, GPIO_PIN_8)` —— 同一根物理线，两端各看一半。

**接口一（反向）：GPIO（FPGA→STM32 AGC 回报）**

- FPGA 端输出：[radar_system_top.v:L130-L134](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L130-L134) 声明 `gpio_dig5/6/7`，并在 [L1043-L1045](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L1043-L1045) 赋值。
- STM32 端读取：[main.cpp:L2192-L2204](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L2192-L2204) 读 `FPGA_DIG6_Pin` / `FPGA_DIG5_SAT_Pin`，再 `outerAgc.applyGain(adarManager)`。

**接口二：USB 命令（GUI→FPGA）**

- GUI 构造：[radar_protocol.py:L168-L175](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L168-L175) `build_command`。
- FPGA 译码：[radar_system_top.v:L949-L1002](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L949-L1002) `case(usb_cmd_opcode)`。

**接口三：I2C/SPI（STM32→外设）**

- 例如配置时钟树：[main.cpp:L1069-L1253](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1069-L1253) `configure_ad9523()` 通过 SPI4 把通道分频比写进 AD9523，决定全系统的时钟。

#### 4.4.4 代码实践（决策练习）

**实践目标**：练习「判断一个新功能该放哪一层」。

**操作步骤**：对下面三个候选功能，分别判断主导层、配合层、所用接口：

1. 「CFAR 门限系数 alpha 可调」
2. 「把雷达平台自身的 GPS 经纬度显示在地图中央」
3. 「当 PA 温度超 75℃ 时立即切断射频」

**预期结果**（详见下一节答案）：三类功能分别落在 FPGA、GUI、STM32，恰好对应三种接口（USB 寄存器 / USB CDC 状态 / GPIO+I2C）。

#### 4.4.5 小练习与答案

**练习**：请给出 4.4.4 三个功能的分层归属。

> **答案**：
> 1. **CFAR alpha**：主导 **FPGA**（`host_cfar_alpha` 寄存器驱动 CFAR 电路），配合 **GUI**（控件发 `0x23` 命令），接口 **USB 命令**。STM32 不参与。
> 2. **GPS 地图居中**：主导 **GUI**（地图组件），数据由 **STM32** 采集（UM982 经 UART5→解析→USB CDC 二进制包→GUI 的 `GPSDataWorker`），接口 **USB CDC**（STM32↔GUI）。FPGA 不参与。
> 3. **过温切射频**：主导 **STM32**（`checkSystemHealth` + `Emergency_Stop`，因为这是安全权威且能直接拉低电源使能），配合 **ADAR1000/PA**（I2C/SPI 切偏置、GPIO 切电源轨），接口 **GPIO+I2C**。GUI 仅事后看到状态。三者时间尺度差异巨大：FPGA 帧级、GUI 100 ms、STM32 安全响应秒级但优先级最高。

---

## 5. 综合实践

本次综合实践把本讲的核心能力——**「拿到需求，判断分层 + 指出接口」**——用在三个真实功能上。请针对 **「自动增益控制（AGC）」「GPS 定位」「CFAR 阈值设置」** 三个功能，分别完成下表（建议先自己填，再对照 4.4.5 与下面的参考）：

| 功能 | 主导层 | 配合层 | 通信接口 | 关键源码位置 |
|------|--------|--------|----------|--------------|
| 自动增益控制（AGC） | ? | ? | ? | ? |
| GPS 定位 | ? | ? | ? | ? |
| CFAR 阈值设置 | ? | ? | ? | ? |

**参考答案与追踪过程**：

1. **自动增益控制（AGC）** —— **三层联动**，这是教科书级的跨层示例。
   - 主导：**FPGA 内环**（`rx_gain_control`，每帧调增益）+ **STM32 外环**（每帧读 DIG_5/6 调 ADAR1000）。GUI 只使能/观测。
   - 接口：FPGA↔STM32 走 **GPIO（DIG_5/DIG_6）**；GUI↔FPGA 走 **USB 命令（0x28-0x2C）** 与 **状态包回读**。
   - 源码：FPGA [radar_system_top.v:L1043-L1045](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L1043-L1045)；STM32 [main.cpp:L2192-L2204](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L2192-L2204)；GUI [radar_protocol.py:L93-L98](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L93-L98)。

2. **GPS 定位** —— **STM32 采集 + GUI 展示**，FPGA 完全不参与。
   - 主导：**STM32**（UM982 经 UART5 → `um982_process` 解析 → 经纬度写入全局变量 → 打成二进制包经 USB CDC 发给 GUI）；**GUI**（`GPSDataWorker` 收包 → 地图居中 + 给每个检测打位置标签）。
   - 接口：GPS↔STM32 走 **UART5**；STM32↔GUI 走 **USB CDC**（与雷达数据 USB 是两条独立总线）。
   - 源码：STM32 [main.cpp:L1778-L1800](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1778-L1800) 与 [L2064-L2070](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L2064-L2070)；GUI [dashboard.py:L161](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L161)、[L1426](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L1426)。

3. **CFAR 阈值设置** —— **GUI 发、FPGA 执行**，STM32 不参与。
   - 主导：**FPGA**（`cfar_ca` 子模块按 `host_cfar_alpha` / `host_detect_threshold` 工作）；**GUI**（FPGA Control 页控件发命令）。
   - 接口：**USB 命令**（`0x23` CFAR_ALPHA、`0x03` DETECT_THRESHOLD）。
   - 源码：FPGA [radar_system_top.v:L620-L651](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L620-L651)（例化）+ [L981](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L981)（译码）；GUI [radar_protocol.py:L85-L89](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L85-L89)。

**思考题（选做）**：为什么 AGC 必须做成跨层，而 CFAR 阈值不需要？提示——CFAR 阈值只影响数字域的判定，FPGA 自己就能闭环；而 AGC 要改变的是 **模拟前端（ADAR1000）的增益**，而模拟前端归 STM32 管，所以必须跨层。

## 6. 本讲小结

- AERIS-10 的固件由 **FPGA / STM32 / GUI** 三个执行体分工：FPGA 负责确定性的高速信号处理与 USB 数据，STM32 负责电源/时钟/波束/传感/安全等「管家」事务，GUI 负责可视化、跟踪与寄存器控制。
- 三层之间只有 **三类物理接口**：GUI↔FPGA 走 **USB 命令+数据**；STM32↔FPGA 走 **GPIO 控制线**（扫描触发与 AGC 回报）；STM32↔各芯片走 **I2C/SPI/UART**。
- GUI 与 FPGA 通过 **`Opcode` 枚举 ↔ Verilog `case` 表** 这份硬契约通信；改一边必须同步改另一边（项目有跨层契约测试盯防）。
- STM32 是 **最早醒来、最后睡去** 的安全权威：电源时序、看门狗、过温过流紧急停机只能放在它这里。
- **分层的判据是「实时性 + 算力特性」**：逐样本实时 → FPGA；外设调度与安全 → STM32；重浮点/人交互 → GUI。Hybrid AGC 是三层联动的最佳范例。
- 时间尺度差异巨大：FPGA 帧级（ms）、STM32 外环帧级但安全响应优先、GUI 约 10 Hz；理解每层的「节奏」是判断功能归属的关键。

## 7. 下一步学习建议

本讲建立了「三层分工」的全局视图，后续讲义会带你分别深入每一层的内部：

- 想深入 **FPGA 内部**：先读 [u3-l1 FPGA 顶层模块 radar_system_top 全景](u3-l1-fpga-top-module.md)，再按 [u4 系列](u4-l1-ddc-digital-downconversion.md) 走接收信号处理链。
- 想深入 **STM32 内部**：从 [u7-l1 STM32 main 与外设初始化](u7-l1-stm32-main-and-peripherals.md) 开始，再看时钟树（u7-l2）与 ADAR1000 波束赋形（u7-l3）。
- 想深入 **GUI 内部**：读 [u8-l1 GUI V7 架构与启动](u8-l1-gui-v7-architecture.md)，然后看数据采集线程（u8-l2）与目标跟踪（u8-l3）。
- 想看 **跨层如何被验证不漂移**：直接跳到 [u11-l3 跨层契约测试](u11-l3-cross-layer-contract-tests.md)，它会用本讲提到的 Opcode 契约做三层一致性检查。
- 想看 **跨层 AGC 的完整闭环**：本讲只讲了骨架，细节在 [u9-l1 Hybrid AGC](u9-l1-hybrid-agc-loop.md)。
