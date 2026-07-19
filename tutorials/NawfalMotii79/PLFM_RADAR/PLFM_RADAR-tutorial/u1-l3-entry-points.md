# 关键入口文件：从哪里开始读源码

## 1. 本讲目标

AERIS-10 雷达的代码分布在 FPGA、STM32、Python GUI 三个差异巨大的技术栈里，面对上百个源文件，初学者最容易「打开一个文件就开始啃，结果读了半天发现不是自己要找的东西」。

本讲的目标是给你一张**入口地图**：

1. 认识三大子系统的「门面文件」——FPGA 的 `radar_system_top.v`、STM32 的 `main.cpp`、GUI 的 `GUI_V7_PyQt.py` 与 `v7/dashboard.py`。
2. 理解每个入口文件在整个端到端流水线里扮演什么角色。
3. 学会**根据自己想研究的问题，反推应该从哪个入口开始读**，避免盲目翻代码。

学完本讲，你应该能在一分钟内决定：「我想看 X 功能，应该先打开哪个文件。」

## 2. 前置知识

本讲假设你已经读过：

- **u1-l1 项目定位**：知道 AERIS-10 是 10.5 GHz 脉冲线性调频（PLFM）相控阵雷达，发射链与接收链镜像对称，接收链包含脉冲压缩、Doppler、MTI、CFAR。
- **u1-l2 仓库目录结构**：知道代码集中在 `9_Firmware/`，下面再分 `9_1_Microcontroller`（MCU）、`9_2_FPGA`、`9_3_GUI` 三层。

如果你还记得 README 里那张「处理流水线（Processing Pipeline）」的 6 步图，本讲就是把那 6 步**映射到具体文件**。

下面几个术语会反复出现，先做最小解释：

- **入口文件（entry point）**：程序启动或模块例化时最先被执行的文件。读懂它，就能知道「系统由哪几块拼起来」。
- **顶层模块（top-level module）**：FPGA 设计里最高一级的 Verilog 模块，它把所有子模块（发射机、接收机、USB……）例化并连线。
- **流水线（pipeline）**：数据像流水一样经过一道道工序，每道工序加工一次。雷达的数据流是：微波 → ADC → FPGA 数字处理 → USB → 上位机显示。

## 3. 本讲源码地图

本讲只看四个「门面」文件，它们分别守在三层子系统的最前面：

| 子系统 | 入口文件 | 行数 | 一句话职责 |
|--------|----------|------|-----------|
| FPGA | `9_Firmware/9_2_FPGA/radar_system_top.v` | 1078 | 把发射机、接收机、CFAR、自测试、USB 接口全部例化并接线的顶层模块 |
| STM32 | `9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp` | 2918 | MCU 上电后的初始化顺序与主循环（电源时序、时钟配置、外设启动） |
| GUI（启动器） | `9_Firmware/9_3_GUI/GUI_V7_PyQt.py` | 40 | 创建 PyQt6 应用、启动主窗口的薄入口 |
| GUI（主窗口） | `9_Firmware/9_3_GUI/v7/dashboard.py` | 2076 | 六标签页主窗口 `RadarDashboard`，协调硬件、处理、显示 |

> 提示：`GUI_V7_PyQt.py` 只有 40 行，它本身几乎不做业务，真正的主窗口逻辑在 `v7/dashboard.py`。这两个文件是一对「启动器 + 实现」的关系，初学者应先看启动器建立全局观，再进 `dashboard.py`。

## 4. 核心概念与源码讲解

### 4.1 FPGA 顶层：radar_system_top.v

#### 4.1.1 概念说明

FPGA 设计通常有几十甚至上百个 `.v` 文件，如果没有一个「总装车间」，你根本不知道它们怎么拼成一整套雷达。`radar_system_top.v` 就是这个总装车间——它是整个 FPGA 设计的**顶层模块**，名字里的 `top` 就是这个意思。

它的职责不是「算」什么，而是「**接线**」：

- 声明所有对外的物理引脚（DAC、ADC、USB、ADAR1000 控制线……）。
- 在内部例化（instantiate）各个功能子模块，把它们用 `wire` 连起来。
- 处理跨时钟域（CDC）和复位同步这类「全局基础设施」。

读懂顶层，等于拿到了整张 FPGA 的「装配图」。

#### 4.1.2 核心流程

顶层把数据流接成一个环：

```text
        主机命令 (USB)
              │  opcode 解码
              ▼
   host_* 配置寄存器 (CFAR/AGC/MTI/扫描参数...)
              │
   ┌──────────┴──────────┐
   ▼                     ▼
radar_transmitter    radar_receiver_final
(发射: chirp+DAC)    (接收: DDC+匹配滤波+Doppler)
   │                     │
   ▼                     ▼
DAC/混频/ADAR1000    CFAR 检测结果
                          │
                          ▼
                 usb_data_interface(_ft2232h)
                          │
                          ▼
                     USB → 上位机
```

启动顺序可以概括为：

1. 三个外部时钟进入（100M 系统 / 120M DAC / USB 时钟），经 `BUFG` 缓冲。
2. 复位被同步到各个时钟域。
3. 发射机 `tx_inst`、接收机 `rx_inst`、CFAR `cfar_inst`、自测试 `self_test_inst` 依次例化并互连。
4. USB 接口根据 `USB_MODE` 参数二选一例化。
5. 主机通过 USB 下发的命令在 `case` 表里被解码成各个 `host_*` 寄存器，配置整条链路。

#### 4.1.3 源码精读

文件开头有一段很重要的注释，直接告诉你它集成了什么、有哪几个时钟域、`USB_MODE` 参数怎么用：

[radar_system_top.v:L3-L20](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L3-L20) —— 顶层注释，说明它集成发射机、接收机、USB 三大块，并列出 `clk_100m` / `clk_120m_dac` / `ft601_clk` 三个时钟域与 `USB_MODE` 的含义。

`USB_MODE` 是顶层最重要的一个参数化开关，默认值是 `1`（即生产板用的 FT2232H）：

[radar_system_top.v:L145](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L145) —— `parameter USB_MODE = 1;`，决定用 FT601（32 位 USB 3.0，200T 高端板）还是 FT2232H（8 位 USB 2.0，50T 生产板）。

接下来是四块核心例化，每块对应流水线的一个大部件：

[radar_system_top.v:L435](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L435) —— `radar_transmitter tx_inst (...)`，发射机：驱动 DAC 生成 chirp、控制混频器使能与 ADAR1000 的 load 信号。

[radar_system_top.v:L502](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L502) —— `radar_receiver_final rx_inst (...)`，接收机：把 ADC 数据一路加工成距离像、Doppler 谱（U4 整个单元的主角）。

[radar_system_top.v:L620](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L620) —— `cfar_ca cfar_inst (...)`，CFAR 检测器：在 Doppler 谱上找目标。

[radar_system_top.v:L671](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L671) —— `fpga_self_test self_test_inst (...)`，板级自测试（U10 会专门讲）。

最有意思的是 USB 接口的参数化选择，用一个 `generate` 块在编译期二选一：

[radar_system_top.v:L718-L792](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L718-L792) —— `generate if (USB_MODE == 0)` 例化 FT601 版 `usb_data_interface usb_inst`（L721），`else` 例化 FT2232H 版 `usb_data_interface_ft2232h usb_inst`（L794）。两个分支对外引脚不同，因此在 FT601 分支末尾把 FT2232H 专用引脚 `tie-off` 到安全电平（L787-L790）。

> 这就是顶层「总装车间」的价值：换一块 USB 芯片，只要改一个参数 `USB_MODE`，其余子模块完全不用动。

#### 4.1.4 代码实践

**实践目标**：用顶层这张「装配图」画出 FPGA 的模块连接关系，并验证 `USB_MODE` 切换会改变哪个子模块被例化。

**操作步骤**：

1. 打开 `radar_system_top.v`，定位 L435 / L502 / L620 / L671 / L721 / L794 这六处例化。
2. 画一张方块图，把这六个 `*_inst` 当作方块，箭头表示数据从 `tx_inst` → 物理世界 → `rx_inst` → `cfar_inst` → `usb_inst`。
3. 找到 L145 的 `parameter USB_MODE = 1;`，把它想象成开关：现在是 `1`，所以生效的是 L794 的 FT2232H 分支；如果把 `USB_MODE` 改成 `0`，生效的就会变成 L721 的 FT601 分支，同时 L787-L790 的 tie-off 反过来。

**需要观察的现象**：

- 两个 USB 分支（L721 与 L794）例化的模块名不同，但都叫 `usb_inst`，说明下游连线代码可以复用。
- FT601 是 32 位总线（`ft601_data [31:0]`），FT2232H 是 8 位（`ft_data [7:0]`）。

**预期结果**：你得到一张「6 个方块 + USB 二选一」的 FPGA 全景草图。后续读到任何一个子模块（比如 `radar_receiver_final`），都能在这张图上找到它的位置。

> 本实践为源码阅读型，不需要综合实现；如需实际编译，请待本地验证（U10/U14 会讲 Vivado 构建流）。

#### 4.1.5 小练习与答案

**练习 1**：顶层例化了 `tx_inst`、`rx_inst`、`cfar_inst`、`self_test_inst` 四个子模块，但它们为什么不会「各干各的、互相打架」？

**参考答案**：因为顶层用大量 `wire` 把它们的端口连在了一起——例如接收机的检测输出 `rx_detect_flag` 通过 `assign usb_detect_flag = rx_detect_flag;`（L711 附近）送进 USB 接口；主机命令又被解码成 `host_*` 寄存器同时喂给多个子模块。顶层充当「总线/连接器」，子模块只管各自的处理。

**练习 2**：如果把 `USB_MODE` 从 `1` 改成 `0`，FT2232H 那组引脚（`ft_data`、`ft_rd_n` 等）会怎样？

**参考答案**：在 FT601 分支里，这些 FT2232H 引脚会被 tie-off 到安全电平（如 `assign ft_rd_n = 1'b1;`，L787-L790），即处于「不激活」状态，避免物理引脚悬空造成干扰。

---

### 4.2 MCU 入口：main.cpp

#### 4.2.1 概念说明

STM32 微控制器上跑的是「裸机程序」（没有操作系统，或者说只有一个超级循环）。所有 C/C++ 嵌入式程序的入口都叫 `main()`，`main.cpp` 就是 STM32 这一层的大门。

注意一个细节：文件头是 ST 标准的 CubeMX 生成模板（`@file : main.c`），但这里改成了 `.cpp` 后缀，因为项目用 C++ 来组织 ADAR1000 管理器等面向对象的模块。所以你会看到 `extern "C" { ... }` 包裹住那些纯 C 的 no-OS 驱动头文件——这是 C/C++ 混编的典型写法。

`main.cpp` 的核心使命是**系统管理**（而不是实时信号处理，那是 FPGA 的活）：上电时序、时钟/频综配置、外设初始化、GPS/IMU/温度读取。

#### 4.2.2 核心流程

`main()` 的执行顺序严格反映了「硬件要先有电、有时钟，才能工作」的物理约束：

```text
MPU_Config()           保护内存区域
  │
HAL_Init()             初始化 HAL 库 + SysTick
  │
SystemClock_Config()   配置 MCU 主时钟树
  │
MX_*_Init()            初始化 GPIO / TIM / I2C / SPI / UART / USB / 看门狗
  │
等待 OCXO 预热 3 分钟   （恒温晶振稳定，喂狗保活）
  │
AD9523 上电时序 + 配置  （给全系统分发相位对齐时钟）
  │
FPGA 上电时序          （1.0V → 1.8V → 3.3V）
  │
IMU / 频综 / ADAR1000 / GPS 等子系统初始化
  │
主循环（超级循环）
```

这条链条解释了一个反直觉的设计：**电源时序为什么由 STM32 而不是 FPGA 控制**——因为 FPGA 自己也是被供电的对象之一，它必须等 STM32 先把电和参考时钟准备好才能启动。STM32 是整个板子上「最早醒来」的器件。

#### 4.2.3 源码精读

文件头声明这是 ST CubeMX 模板，并交代了版权：

[main.cpp:L1-L18](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1-L18) —— ST 标准 main 模板头，提示这是一个 CubeMX 生成、又被改造成 C++ 的入口。

紧接着是一长串 `#include`，光看头文件就能列出 STM32 要管哪些器件：

[main.cpp:L20-L71](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L20-L71) —— include 区：`USBHandler`、`ADAR1000_Manager`/`ADAR1000_AGC`、`ad9523`、`adf4382`/`adf4382a_manager`、`DAC5578`、`ADS7830`、`um982_gps`、`GY_85_HAL`、`BMP180` 等，一份「外设清单」。

外设句柄（handle）集中声明，对应物理总线：

[main.cpp:L111-L119](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L111-L119) —— `hi2c1/2/3`（三路 I2C）、`hspi1/4`（两路 SPI）、`htim1/3`（两个定时器，其中 `htim3` 是 B15 修复加入的 DELADJ PWM 定时器）。

入口函数本体在这里：

[main.cpp:L1366](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1366) —— `int main(void)`，STM32 程序入口。

外设初始化的「全家桶」调用，一眼看完所有 `MX_*`：

[main.cpp:L1397-L1408](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1397-L1408) —— `MX_GPIO_Init` / `MX_TIM1_Init` / `MX_TIM3_Init` / `MX_I2C1/2/3_Init` / `MX_SPI1/4_Init` / `MX_UART5_Init` / `MX_USART3_UART_Init` / `MX_USB_DEVICE_Init` / `MX_IWDG_Init`（独立看门狗）。

OCXO 预热循环是嵌入式里很典型的「长延时 + 喂狗」模式：

[main.cpp:L1420-L1429](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1420-L1429) —— 等待 OCXO（恒温晶振）预热 180 秒；因为看门狗超时只有约 4 秒，所以不能用一次 `HAL_Delay(180000)`，而是循环 180 次、每次 1 秒并刷新看门狗。

随后是两段典型的「电源时序」：先给时钟芯片 AD9523 上电并配置，再给 FPGA 上电：

[main.cpp:L1431-L1472](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1431-L1472) —— AD9523 电源时序：先拉低复位，依次使能 1.8V、3.3V 时钟轨，释放复位，再调用 `configure_ad9523()` 配置 12 路输出时钟（注释里列出了 300MHz/400MHz/100MHz/120MHz 等通道分配）。

[main.cpp:L1474-L1485](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1474-L1485) —— FPGA 电源时序：按 `1.0V → 1.8V → 3.3V` 的顺序使能三路电源轨。

#### 4.2.4 代码实践

**实践目标**：从 `main.cpp` 的开头注释与初始化序列，提炼出「STM32 是系统管理者」这一结论。

**操作步骤**：

1. 打开 `main.cpp`，阅读 L1-L18 的文件头注释，再用一句话写出 `main.cpp` 负责什么。
2. 跳到 L1366 的 `int main(void)`，顺着往下读 L1397-L1408 的 `MX_*` 调用，统计 STM32 配置了哪几类外设。
3. 阅读 L1420-L1485，圈出三件必须在主循环之前完成的事：OCXO 预热、AD9523 配置、FPGA 上电。

**需要观察的现象**：

- 初始化顺序严格「由底层到上层」：先时钟、再外设、再 OCXO 预热、再 AD9523、最后才轮到 FPGA 和各功能芯片。
- 每一步都伴随 `DIAG(...)` 日志，说明 STM32 固件有完整的启动诊断输出。

**预期结果**：你能写出类似这样的一句话总结——「`main.cpp` 是 STM32 的启动入口，负责按物理依赖顺序完成 MPU/HAL/时钟/外设初始化、OCXO 预热、AD9523 时钟分发与 FPGA 上电时序，为整块板子建立可工作的硬件底座。」

> 本实践为源码阅读型；若要在真机上观察，可用串口抓取 `DIAG` 日志验证启动顺序（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 OCXO 预热要用 180 次 `HAL_Delay(1000)` 加 `HAL_IWDG_Refresh()` 的循环，而不是直接 `HAL_Delay(180000)`？

**参考答案**：因为独立看门狗（IWDG）的超时大约只有 4 秒（见 L1408 注释）。一次 `HAL_Delay(180000)` 会让 CPU 阻塞 180 秒不去「喂狗」，看门狗会在 4 秒后把 MCU 复位，导致永远启动不完。循环里每秒喂一次狗，既等够了预热时间，又不会触发复位。

**练习 2**：从 L20-L71 的 `#include` 列表，推断 STM32 至少要管理哪几类外部器件？

**参考答案**：时钟与频综（`ad9523`、`adf4382`、`adf4382a_manager`）、波束赋形（`ADAR1000_Manager`/`ADAR1000_AGC`）、PA 偏置与电流检测（`DAC5578`、`ADS7830`）、传感器（`um982_gps` GPS、`GY_85_HAL` IMU、`BMP180` 气压）、USB 通信（`USBHandler`）。这正好印证「STM32 = 系统管理者」。

---

### 4.3 GUI 入口：GUI_V7_PyQt.py 与 v7/dashboard.py

#### 4.3.1 概念说明

上位机 GUI 是用 Python + PyQt6 写的。和 FPGA/MCU 不同，Python 程序的「入口」通常是一个可以直接 `python xxx.py` 运行的脚本。这里有两个文件需要区分：

- `GUI_V7_PyQt.py`：**启动器**。它只负责创建一个 PyQt6 应用、把主窗口显示出来，本身不含业务逻辑（只有 40 行）。
- `v7/dashboard.py`：**主窗口实现**。定义了 `RadarDashboard` 这个 `QMainWindow`，包含全部六个标签页和所有交互逻辑。

为什么拆成两个文件？因为 GUI 代码量很大（`dashboard.py` 有 2076 行），把「启动」和「实现」分开，能让入口保持干净，也方便单元测试单独 import `v7` 包。

GUI 在端到端流水线里位于最末端：它从 USB 读 FPGA 发来的数据包，组装成 Range-Doppler 图，再做聚类与跟踪，最终把目标点画到屏幕和地图上。

#### 4.3.2 核心流程

GUI 启动到看到目标点的过程：

```text
python GUI_V7_PyQt.py
  │
  ▼
QApplication + RadarDashboard.show()      （启动器）
  │
  ▼
dashboard.py 构造六个标签页                 （主窗口初始化）
  ├── Main View      Range-Doppler 热力图 + 目标表
  ├── Map View       Leaflet 地图
  ├── FPGA Control   27 个 opcode 寄存器面板
  ├── AGC Monitor    增益/峰值/饱和实时曲线
  ├── Diagnostics    连接状态、包统计、自测试、日志
  └── Settings       DSP 参数 + About
  │
  ▼
后台线程 (workers.py) 从 USB 读字节流
  │
  ▼
processing.py 聚类 + Kalman 跟踪
  │
  ▼
RangeDopplerCanvas / 地图刷新  → 屏幕上的目标点
```

关键点：GUI 内部又是「**主线程负责显示、后台线程负责采集**」的结构，二者靠队列解耦。这条线索在 U8 会展开，本讲只需知道入口在哪里。

#### 4.3.3 源码精读

启动器极其简短，一眼看完：

[GUI_V7_PyQt.py:L1-L9](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/GUI_V7_PyQt.py#L1-L9) —— 文件 docstring，明确写着「Entry point. Launches the RadarDashboard main window.」，并给出用法 `python GUI_V7_PyQt.py`。

[GUI_V7_PyQt.py:L17](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/GUI_V7_PyQt.py#L17) —— `from v7 import RadarDashboard`，从 `v7` 包导入主窗口类。

[GUI_V7_PyQt.py:L26-L36](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/GUI_V7_PyQt.py#L26-L36) —— `main()`：创建 `QApplication`、设置应用名与字体、实例化 `RadarDashboard()`、`window.show()`，最后 `app.exec()` 进入 Qt 事件循环。

进入主窗口实现，开头 docstring 就是六标签页的「说明书」：

[v7/dashboard.py:L1-L24](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L1-L24) —— docstring 逐条列出六个标签页（Main / Map / FPGA Control / AGC Monitor / Diagnostics / Settings）的职责，并说明它用 `radar_protocol.py` 走 FT2232H 或 FT601 与 FPGA 通信。

主窗口类的定义起点：

[v7/dashboard.py:L131](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L131) —— `class RadarDashboard(QMainWindow):`，GUI 的核心类。

标签页的创建代码集中在一处，是 GUI 结构最直观的索引：

[v7/dashboard.py:L341-L349](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L341-L349) —— `self._tabs = QTabWidget()` 后依次调用 `_create_main_tab()` / `_create_map_tab()` / `_create_fpga_control_tab()` / `_create_agc_monitor_tab()` / `_create_diagnostics_tab()` / `_create_settings_tab()`，对应六个标签页。

每个 `_create_*_tab()` 方法末尾都会 `self._tabs.addTab(...)` 把构建好的面板挂上去，例如：

[v7/dashboard.py:L504](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/v7/dashboard.py#L504) —— `self._tabs.addTab(tab, "Main View")`，Main View 标签页就是屏幕上看到 Range-Doppler 图和目标表的那一页。

#### 4.3.4 代码实践

**实践目标**：验证「启动器 → 主窗口 → 六标签页」这条阅读路径，并锁定「雷达数据 → 屏幕目标点」的入口。

**操作步骤**：

1. 打开 `GUI_V7_PyQt.py` 全文（只有 40 行），阅读 L1-L9 的 docstring 和 L26-L36 的 `main()`，用一句话写出它的职责。
2. 打开 `v7/dashboard.py` 的 L1-L24 docstring，把六标签页的名称抄下来；再跳到 L341-L349，确认这六个 `_create_*_tab()` 与 docstring 一一对应。
3. 在 L504 的 `addTab(tab, "Main View")` 处停下——这就是屏幕上显示 Range-Doppler 图和目标表的那一页。

**需要观察的现象**：

- 启动器几乎没有业务代码，全部重活都委托给 `RadarDashboard`。
- 六个标签页的创建方法是平行的，任何一个标签页都可以单独读、单独改。

**预期结果**：

- `GUI_V7_PyQt.py` 的一句话总结：「它是 GUI 的薄启动器，创建 QApplication 并显示 `RadarDashboard` 主窗口。」
- **「雷达数据如何变成屏幕上的目标点」应该从 GUI 入口开始追踪**：先 `GUI_V7_PyQt.py` → `v7/dashboard.py` 的 `RadarDashboard` → Main View 标签页（L504）里的 `RangeDopplerCanvas` 和目标表；因为「屏幕上的目标点」就是在这一页被渲染出来的。看到画布后，再反向追问「数据从哪来」，就会自然进入后台采集线程（U8 的 `workers.py`）和处理链（`processing.py`）。

> 为什么不从 FPGA 开始？因为「屏幕上的目标点」是 GUI 的产物。从 FPGA 开始会先读一大堆数字处理代码，离「屏幕」太远；从 GUI 入口开始，能最快看到目标点的样子，再顺藤摸瓜往上游走。当然，如果你研究的是「目标点背后的信号处理算法」，那就该回到 FPGA 入口（4.1）。

#### 4.3.5 小练习与答案

**练习 1**：既然 `GUI_V7_PyQt.py` 只有 40 行、几乎不做业务，为什么不直接把它的内容写进 `dashboard.py`？

**参考答案**：分离启动器与实现有两个好处——一是让入口文件保持极简，新人一眼就能看懂「程序怎么跑起来」；二是 `v7` 作为一个 Python 包可以被其他脚本（比如测试 `test_v7.py`、回放脚本）直接 `import`，而不必非得启动一个 Qt 应用。这是 Python 项目里常见的「薄入口 + 包实现」模式。

**练习 2**：`dashboard.py` 的 L48-L67 从 `models` / `hardware` / `processing` / `workers` / `map_widget` 导入了一堆东西。从这些导入名，猜猜 GUI 内部分了哪几个职责层？

**参考答案**：大致分为：数据模型层（`models`：`RadarTarget`、`RadarSettings` 等数据类）、硬件通信层（`hardware`：`FT2232HConnection`、`FT601Connection`、`RadarProtocol`）、信号处理层（`processing`：`RadarProcessor`）、后台线程层（`workers`：采集/回放工作线程）、地图展示层（`map_widget`）。GUI 自己只做「编排」，把活分派给这些模块。

---

## 5. 综合实践

把三个入口串起来，做一次**「问题 → 入口」反查训练**。

针对下面五个研究问题，分别写出你应该**首先打开哪个入口文件、定位到哪一行附近**，并简述理由：

| 研究问题 | 首先打开的入口 | 定位（文件:行） | 理由 |
|----------|----------------|-----------------|------|
| 雷达数据怎么变成屏幕上的目标点？ | （自填） | （自填） | （自填） |
| 板子上电后，电源是按什么顺序起来的？ | （自填） | （自填） | （自填） |
| FPGA 里发射机和接收机是怎么连在一起的？ | （自填） | （自填） | （自填） |
| 我想新加一个 USB 命令控制某个参数，从哪改起？ | （自填） | （自填） | （自填） |
| GUI 有几个标签页，分别干什么？ | （自填） | （自填） | （自填） |

**参考答案**（先自己填，再对照）：

1. 屏幕目标点 → `v7/dashboard.py`，定位 `RadarDashboard`（L131）与 Main View 标签页（L504）。因为目标点是 GUI 渲染的，从这里往上游追最快。
2. 电源上电顺序 → `main.cpp` 的 `int main`（L1366），重点看 L1420-L1485 的 OCXO 预热、AD9523 上电、FPGA 上电三段。
3. FPGA 收发机连接 → `radar_system_top.v` 的 `tx_inst`（L435）与 `rx_inst`（L502）例化。
4. 新增 USB 命令 → 先看 `radar_system_top.v` 顶层 `USB_MODE` generate（L718-L792）与命令解码 `host_*` 寄存器区（L223-L278 附近），再在 Python 端 `radar_protocol.py` 同步 opcode（U6/U14 会展开）。
5. GUI 标签页 → `v7/dashboard.py` 的 docstring（L1-L24）与 `_create_*_tab()`（L344-L349）。

这张表是后续整本手册的「导航仪」——任何时候不知道从哪读起，就回到这张表。

## 6. 本讲小结

- 三大子系统各有一个「门面」入口：FPGA 的 `radar_system_top.v`（顶层接线）、STM32 的 `main.cpp`（启动与系统管理）、GUI 的 `GUI_V7_PyQt.py` + `v7/dashboard.py`（启动器 + 主窗口）。
- `radar_system_top.v` 用 `USB_MODE` 参数在 FT601 与 FT2232H 两个 USB 模块间二选一，其余子模块（发射机/接收机/CFAR/自测试）以 `*_inst` 形式被例化并互连。
- `main.cpp` 的初始化序列反映硬件依赖：先 HAL/时钟/外设，再 OCXO 预热，再 AD9523 配置，最后 FPGA 上电——STM32 是全板「最早醒来」的器件。
- `GUI_V7_PyQt.py` 是 40 行的薄启动器，真正的主窗口是 `v7/dashboard.py` 里的 `RadarDashboard`，它构建六个标签页。
- 读源码要「按问题选入口」：研究显示与目标点从 GUI 入口；研究电源/时钟从 MCU 入口；研究数字信号处理与硬件接线从 FPGA 入口。
- 把本讲的「问题 → 入口」对照表留在手边，它是后续每一讲的导航工具。

## 7. 下一步学习建议

入口地图建立之后，建议按数据流方向继续深入：

1. **想先看整条流水线怎么串起来**：进入 U2，特别是 u2-l2「雷达信号处理流水线」，它会从端到端视角把 DAC → 混频 → 波束 → ADC → DDC → 匹配滤波 → Doppler → CFAR → USB → GUI 的每一步对应到具体文件。
2. **想钻进 FPGA 数字处理**：从本讲的 `radar_receiver_final rx_inst`（L502）出发，进入 U4「FPGA 接收信号处理链」。
3. **想了解 STM32 怎么管理外设**：从本讲的 `MX_*_Init` 与 AD9523 配置出发，进入 U7「STM32 微控制器固件」。
4. **想看 GUI 内部数据流**：从本讲的 `RadarDashboard` 与 Main View 出发，进入 U8「Python GUI V7 架构与数据流」。

无论选哪条线，记得随时回到本讲的「问题 → 入口」对照表定位自己。
