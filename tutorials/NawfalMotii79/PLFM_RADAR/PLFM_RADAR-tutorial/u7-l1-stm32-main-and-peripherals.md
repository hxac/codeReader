# STM32 main 与外设初始化

## 1. 本讲目标

AERIS-10 雷达板上电后，最先醒来的不是 FPGA，也不是上位机 GUI，而是一颗 STM32F746 微控制器。它是整块板的「电源管家 + 系统调度员」——负责按严格顺序给各条电源轨上电、把时钟芯片喂到锁定、把 FPGA 从复位里拉起来、然后进入一个永不停歇的健康巡检主循环。

本讲聚焦 STM32 固件的入口文件 [`main.cpp`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp)，读完它你应当能够：

1. 说出 STM32 上电后的**初始化顺序**：从 `HAL_Init` → 时钟树 → 外设句柄 → OCXO 预热 → 时钟分发 → FPGA 上电 → ADAR1000 上电 → 进入主循环。
2. 理解每条电源轨**为什么必须按某个先后顺序**上电，以及为什么这件事只能由 STM32 来做、不能交给 FPGA。
3. 识别 STM32 与 FPGA 之间的**通信方式**——不是高速总线，而是一组 GPIO 握手信号（新 chirp / 新仰角 / 新方位 / 混频器使能 / 复位）。
4. 看懂 no-OS 抽象层（`stm32_spi_ops`、`shims/`）在「把 ADI 公司的芯片驱动跨平台复用」这件事上扮演的角色。

本讲对应的学习阶段是 **intermediate（进阶）**，承接 u2-l3《三层固件分工》。u2-l3 给出了「FPGA 做实时信号处理、STM32 做系统管理、GUI 做可视化」的分工判据；本讲则钻进 STM32 这一层，把「系统管理者」这句话拆成一行行可读的代码。

---

## 2. 前置知识

在进入源码之前，先用通俗语言把几个基础概念讲清楚。如果你已经熟悉 STM32 HAL 库，可以跳过本节。

### 2.1 什么是 HAL

**HAL**（Hardware Abstraction Layer，硬件抽象层）是 ST 官方提供的一套 C 库，把「往某个寄存器写一个值」这种底层操作，包装成 `HAL_GPIO_WritePin(...)`、`HAL_I2C_Init(...)` 这种有语义的函数。这样你的代码就不必关心 STM32F4 和 STM32F7 寄存器地址的差异，只调用统一接口即可。本工程里，所有 `HAL_*` 开头的调用都来自这套库。

### 2.2 外设句柄（Handle）

STM32 HAL 用「句柄」结构体来描述一个外设的完整状态。例如：

- `I2C_HandleTypeDef hi2c1;` —— 描述第 1 路 I2C 总线（实例、时序、地址模式……）。
- `SPI_HandleTypeDef hspi4;` —— 描述第 4 路 SPI 总线。
- `UART_HandleTypeDef huart5;` —— 描述第 5 路 UART。

一个「句柄」就是一根总线。后面所有针对这条总线的操作（发数据、读数据）都把句柄的地址传进去，例如 `HAL_I2C_Master_Transmit(&hi2c1, ...)`。

### 2.3 时钟域与电源域

这是本讲最关键的概念。STM32 板上有**两类「域」**需要严格管理：

- **时钟域**：不同芯片需要不同频率、但**相位必须对齐**的时钟（ADC 要 400 MHz、DAC 要 120 MHz、FPGA 要 100 MHz、本振要 300 MHz……）。这些时钟全部由一颗 AD9523 时钟发生器分发，而 AD9523 又由 STM32 经 SPI 配置。
- **电源域**：不同芯片对上电顺序有严格要求。比如 FPGA 的核心电压 1.0V 必须先于 1.8V、1.8V 先于 3.3V；功放 GaN 器件的栅压 Vg 必须先于漏极 VDD 上电，否则会烧管子。

这两件事都属于「**慢、确定性要求高、出错代价大**」的任务——恰好是 MCU（而非 FPGA）的强项。

### 2.4 看门狗（Watchdog / IWDG）

**IWDG**（Independent Watchdog，独立看门狗）是一个由独立 32 kHz 时钟驱动的倒计时器。固件必须在它数到 0 之前调用 `HAL_IWDG_Refresh()`「喂狗」，否则它会**硬件复位整颗 MCU**。这是一种防止「固件卡死」的最后兜底机制。本工程里看门狗超时设为约 4 秒，并且在「功放电源可能还通着」的危险时刻尤其重要——详见 4.2。

### 2.5 no-OS 抽象层

ADI（Analog Devices）公司为自家的 AD9523、ADF4382 等芯片提供了「驱动代码」。这些驱动被设计成不依赖任何操作系统（no OS = no operating system），而是通过一组**抽象接口**（`no_os_spi_*`、`no_os_delay_*`）访问硬件。要在一颗 STM32 上用这些驱动，你只需为这些抽象接口提供「STM32 版本」的实现（`stm32_spi.c` 等）。这套「适配层」就是 no-OS 抽象层，本讲 4.1 会精读它。

---

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用到什么 |
| --- | --- | --- |
| [`9_1_3_C_Cpp_Code/main.cpp`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp) | STM32 应用入口（约 2900 行）。`main()`、所有 `MX_*_Init`、电源时序、主循环都在这里。 | 外设句柄声明、初始化顺序、电源上下电、FPGA GPIO 通信、看门狗 |
| [`9_1_1_C_Cpp_Libraries/USBHandler.cpp`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/USBHandler.cpp) | USB CDC 接收的状态机。把 USB 收到的字节流解析成「开始标志 → 设置包 → 就绪」。 | 主机通信一侧、状态机示例 |
| [`9_1_1_C_Cpp_Libraries/RadarSettings.cpp`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/RadarSettings.cpp) | 雷达参数数据类。把 `USBHandler` 收到的设置包拆成 9 个 double + 1 个 uint32。 | 设置包解析、范围校验 |
| [`9_1_1_C_Cpp_Libraries/stm32_spi.c`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/stm32_spi.c) | no-OS SPI 接口的 STM32 实现。把 `no_os_spi` 抽象调用翻译成 `HAL_SPI_TransmitReceive`。 | no-OS 抽象层 |
| [`tests/shims/`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/shims) | 测试用的桩文件（`stm32f7xx_hal.h`、`no_os_spi.h` 等），让 MCU 代码能在 PC 上编译。 | 抽象层在测试中的替身 |

> 说明：本讲引用的代码片段均为项目**真实存在**的源码，行号基于当前 HEAD `749bd0f`。代码片段会做删减（用 `// ...` 标注），只保留与本讲主题相关的关键行。

---

## 4. 核心概念与源码讲解

本讲围绕三个最小模块展开：**4.1 HAL 初始化与外设句柄**、**4.2 电源上下电时序**、**4.3 STM32↔FPGA 通信与主机 USB 通道**。三者按 STM32 上电后真实执行的先后顺序排列。

### 4.1 HAL 初始化与外设句柄

#### 4.1.1 概念说明

任何 STM32 程序的第一件事都是**初始化硬件抽象层与外设**。在本工程里，这一步的工作量很大，因为 STM32F746 要同时管：

- **3 路 I2C**（`hi2c1/2/3`）：挂 DAC（设功放栅压）、ADC（读功放电流 Idq 与温度）等慢速外设。
- **2 路 SPI**（`hspi1/4`）：挂 ADAR1000 相移器、AD9523 时钟发生器、ADF4382 本振等需要较高速率的芯片。
- **2 路 UART**（`huart3/5`）：`huart3` 是调试串口，`huart5` 接 UM982 双天线 GPS。
- **2 个定时器**（`htim1/3`）：`htim1` 当微秒计时器（`micros()`），`htim3` 输出 DELADJ PWM 给本振做相位微调。
- **1 个 USB CDC**：与上位机 GUI 通信。
- **1 个 IWDG 看门狗**：防卡死。

此外还有**一大堆 GPIO**——电源使能脚、FPGA 握手脚、LED、锁相检测脚等。这些全部要在 `main()` 一开头集中配置好，后面的电源时序和主循环才能调用它们。

一个常被忽视但本工程深度依赖的细节是：**no-OS 抽象层**。ADI 的 AD9523 驱动是用 `no_os_spi_*` 接口写的，它不知道底层是 STM32 还是 Linux。要把这套驱动跑起来，必须提供一个把 `no_os_spi` 翻译成 `HAL_SPI` 的适配器——即 `stm32_spi.c`。

#### 4.1.2 核心流程

`main()` 函数的初始化部分（去掉中间业务逻辑）大致是这样一个流程：

```text
main()
├─ MPU_Config()               # 配置内存保护单元（默认权限）
├─ HAL_Init()                  # 初始化 HAL、SysTick、NVIC 优先级分组
├─ SystemClock_Config()        # 配置 PLL：HSE 8MHz → SYSCLK 144MHz
├─ PeriphCommonClock_Config()  # 定时器外设时钟
├─ MX_GPIO_Init()              # 所有 GPIO 引脚模式与电平
├─ MX_TIM1_Init / MX_TIM3_Init # 微秒计时器 + DELADJ PWM
├─ MX_I2C1/2/3_Init()          # 三路 I2C
├─ MX_SPI1_Init / MX_SPI4_Init # 两路 SPI
├─ MX_UART5_Init / MX_USART3_UART_Init()  # GPS + 调试串口
├─ MX_USB_DEVICE_Init()        # USB CDC
├─ MX_IWDG_Init()              # 启动硬件看门狗（~4s 超时）
├─ HAL_TIM_Base_Start(&htim1)  # 启动微秒计时器
├─ DWT_Init()                  # 启用 DWT 周期计数器（纳秒延时用）
└─ ... 进入业务初始化（OCXO 预热、时钟分发、FPGA 上电 ...）
```

关键点：**外设句柄先全部建好，再开始任何业务逻辑**。这是因为业务逻辑（如配 AD9523）会调用 SPI，而 SPI 必须先初始化好。顺序错了，第一个 `HAL_SPI_Transmit` 就会失败。

#### 4.1.3 源码精读

**(1) 外设句柄的全局声明**

[9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp:L111-L122](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L111-L122) —— 这是 STM32 的全部「总线清单」。一行一个句柄，代表一根总线：

```cpp
I2C_HandleTypeDef hi2c1;
I2C_HandleTypeDef hi2c2;
I2C_HandleTypeDef hi2c3;

SPI_HandleTypeDef hspi1;
SPI_HandleTypeDef hspi4;

TIM_HandleTypeDef htim1;
TIM_HandleTypeDef htim3;  // B15 fix: DELADJ PWM timer (CH2=TX, CH3=RX)

UART_HandleTypeDef huart5;
UART_HandleTypeDef huart3;
```

这张表回答了实践任务里「STM32 配了哪些 I2C/SPI」的问题。每根总线上挂了哪些器件，要结合各器件驱动的初始化调用来看（DAC5578 用 `hi2c1`、ADS7830 用 `hi2c2`、AD9523/ADF4382 用 `hspi4`、ADAR1000 用 `hspi1`），4.1.4 的实践会带读者一一对应。

> 注意命名约定：`hi2c1`/`hspi4`/`htim3`/`huart5` 里的数字就是 STM32F746 硬件上的外设实例号（I2C1、SPI4、TIM3、UART5），HAL 库要求句柄的 `Instance` 字段必须与之对应，否则初始化会失败。

**(2) 在 main() 里集中初始化外设**

[9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp:L1396-L1408](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1396-L1408) —— 一连串 `MX_*_Init()` 把上面那张句柄表全部填好：

```cpp
  /* Initialize all configured peripherals */
  MX_GPIO_Init();
  MX_TIM1_Init();
  MX_TIM3_Init();  // B15 fix: init DELADJ PWM timer before LO manager uses it
  MX_I2C1_Init();
  MX_I2C2_Init();
  MX_I2C3_Init();
  MX_SPI1_Init();
  MX_SPI4_Init();
  MX_UART5_Init();
  MX_USART3_UART_Init();
  MX_USB_DEVICE_Init();
  MX_IWDG_Init();  /* GAP-3 FIX 2: start hardware watchdog (~4 s timeout) */
```

`MX_*` 是 STM32CubeMX 图形工具自动生成的命名约定（MX = CubeMX），每个函数只负责把**一个**外设句柄的各字段填上默认值并调用 `HAL_*_Init`。注释里频繁出现的 `B15 fix`、`GAP-3 FIX` 是项目内部的 bug 修复标记——比如 `MX_TIM3_Init` 必须在 LO manager（本振管理器）使用它**之前**调用，否则会出现悬空 extern（bug #15）。

**(3) 时钟树配置：HSE → 144MHz**

[9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp:L2229-L2257](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L2229-L2257) —— `SystemClock_Config` 把外部 8MHz 晶振（HSE）经 PLL 倍频到 144MHz 作为系统主频：

```cpp
  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSE;
  RCC_OscInitStruct.HSEState = RCC_HSE_ON;
  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_ON;
  RCC_OscInitStruct.PLL.PLLSource = RCC_PLLSOURCE_HSE;
  RCC_OscInitStruct.PLL.PLLM = 25;
  RCC_OscInitStruct.PLL.PLLN = 144;
  RCC_OscInitStruct.PLL.PLLP = RCC_PLLP_DIV2;
  RCC_OscInitStruct.PLL.PLLQ = 3;
```

注意：STM32 自己的 144MHz 与雷达的 10.5GHz 射频毫无关系——它只是 MCU 跑代码的主频。真正决定雷达相位对齐的是后面 AD9523 分发出来的那一堆时钟（见 u7-l2）。STM32 只是用 144MHz 来跑 `delay_us()`、`delay_ns()` 和 DWT 周期计数。

**(4) no-OS 抽象层：把 no_os_spi 翻译成 HAL_SPI**

这是本模块最值得理解的设计。ADI 的 AD9523 驱动通过一个 `no_os_spi_platform_ops` 结构体来调用底层 SPI，本工程在 STM32 上提供了它的实现：

[9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/stm32_spi.c:L55-L98](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/stm32_spi.c#L55-L98) —— `stm32_spi_write_and_read` 把抽象的「读写 N 字节」翻译成 HAL 的 `HAL_SPI_TransmitReceive`，并可选地用 GPIO 软件控制片选（CS）：

```c
    /* Assert CS (active low) */
    if (cs_port)
        HAL_GPIO_WritePin(cs_port, cs_pin, GPIO_PIN_RESET);

    HAL_StatusTypeDef hal_ret;
    hal_ret = HAL_SPI_TransmitReceive(hspi, data, data, bytes_number, 200);

    /* Deassert CS */
    if (cs_port)
        HAL_GPIO_WritePin(cs_port, cs_pin, GPIO_PIN_SET);
```

注意一个有趣的细节：发送和接收**用同一个 `data` 缓冲**（`HAL_SPI_TransmitReceive(hspi, data, data, ...)`）。SPI 是全双工的，主机每发一个字节的同时也会收到一个字节，所以「写寄存器」和「读寄存器」的区别只在于你是否关心返回的字节——ADI 驱动用同一套调用搞定两者。

[9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/stm32_spi.c:L119-L124](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/stm32_spi.c#L119-L124) —— 这个 `stm32_spi_ops` 结构体就是 ADI 驱动和 STM32 HAL 之间的「插头」。AD9523 驱动初始化时把 `platform_ops = &stm32_spi_ops` 填进去，之后它调用 `spi->platform_ops->write_and_read(...)` 就会落到上面的 STM32 实现里：

```c
/* platform ops struct */
const struct no_os_spi_platform_ops stm32_spi_ops = {
    .init = &stm32_spi_init,
    .write_and_read = &stm32_spi_write_and_read,
    .remove = &stm32_spi_remove,
};
```

这套抽象的价值在于**可移植**与**可测试**：同一份 AD9523 驱动代码，在 STM32 上接 `stm32_spi_ops`，在 PC 单元测试时接一个 mock（见 [`tests/shims/`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/shims) 下的桩文件），驱动本身一行都不用改。u11-l2 会专门讲这套 shim/mock 测试体系。

**(5) 看门狗初始化**

[9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp:L2842-L2853](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L2842-L2853) —— IWDG 用片内 32kHz LSI 时钟，分频 256 后每 tick 约 8ms，重载值 500 → 超时约 4 秒：

```cpp
static void MX_IWDG_Init(void)
{
    hiwdg.Instance       = IWDG;
    hiwdg.Init.Prescaler = IWDG_PRESCALER_256;
    hiwdg.Init.Reload    = 500;
    hiwdg.Init.Window    = IWDG_WINDOW_DISABLE;
    if (HAL_IWDG_Init(&hiwdg) != HAL_OK) { /* ... */ }
}
```

这个 4 秒窗口会反复出现在 4.2 和 4.3——任何长于 4 秒的阻塞操作都必须中途「喂狗」，否则 MCU 复位。

#### 4.1.4 代码实践

**实践目标**：把 STM32 的 5 条总线（`hi2c1/2/3`、`hspi1/4`）与它们各自挂载的器件一一对应，建立「总线 → 器件」的心智地图。

**操作步骤**：

1. 打开 [main.cpp:L111-L122](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L111-L122)，确认 5 个句柄的名字。
2. 用文本搜索（在你的编辑器里）在 `main.cpp` 全文中搜以下调用，找到每个器件用的是哪个句柄：
   - `DAC5578_Init(&hdac1, &hi2c1, 0x48, ...)` 和 `DAC5578_Init(&hdac2, &hi2c1, 0x49, ...)` —— 两片 DAC 都挂在 **I2C1**，地址 0x48/0x49。
   - `ADS7830_Init(&hadc1, &hi2c2, 0x48, ...)`、`ADS7830_Init(&hadc2, &hi2c2, 0x4A, ...)`、`ADS7830_Init(&hadc3, &hi2c2, 0x49, ...)` —— 三片 ADC（2 片读 Idq + 1 片读温度）挂在 **I2C2**。
   - `init_param.spi_init.extra = &hspi4;`（在 `configure_ad9523` 内）—— **AD9523 走 SPI4**。
   - `ADF4382A_Manager_Init` 内部也用 `hspi4`（本振，与 AD9523 共线）。
   - ADAR1000 用 **SPI1**（其片选 `ADAR_1/2/3/4_CS_3V3` 是 GPIO 软件控制）。
3. 把结果填进下表（参考答案见 4.1.5）。

**需要观察的现象**：注意 I2C 是「多器件共线 + 7 位地址区分」，而 SPI 是「多器件共线 + GPIO 片选区分」。这是两类总线最大的组织差异——同一个 `hi2c2` 上挂 3 片 ADS7830 仅靠地址不同来区分；而同一个 `hspi4` 上挂 AD9523 和 ADF4382 则靠各自独立的 CS 引脚区分。

**预期结果**：你能画出一张「总线—器件」对照表，并解释为什么 DAC 用 I2C（慢速、多片共址很方便）而 ADAR1000 相移器用 SPI（需要更高速率更新相位）。

> 待本地验证：以上器件到总线的映射来自源码调用，但若你手头有原理图（`4_Schematics and Boards Layout/4_6_Schematics/MainBoard/`），建议交叉核对一次，因为代码里的 `hi2c2` 最终对应到物理哪条 I2C 走线，仍以原理图为准。

#### 4.1.5 小练习与答案

**练习 1**：`hi2c1`、`hi2c2` 上分别挂了哪些器件？为什么 PA 栅压 DAC（DAC5578）和 Idq 电流 ADC（ADS7830）被放在了**不同**的 I2C 总线上，而不是都挂在 `hi2c1`？

> **参考答案**：`hi2c1` 挂 2 片 DAC5578（地址 0x48/0x49，设 16 路 PA 栅压 Vg）；`hi2c2` 挂 3 片 ADS7830（地址 0x48/0x4A/0x49，读 16 路 Idq + 8 路温度）。分开放在两条总线上的原因至少有二：一是**电气隔离**——DAC 是给功放栅极灌电压的模拟器件、ADC 是从电流采样电阻读微弱电压，两者在嘈杂的射频环境里若共线容易互相串扰；二是**总线负载与速率**——分成两条 I2C 后，校准时对 DAC 写、对 ADC 读可以分别满速进行，不会互相阻塞。这体现了「按功能/噪声域分总线」的工程习惯。

**练习 2**：AD9523 驱动是 ADI 用 `no_os_spi` 接口写的，它怎么「知道」自己跑在 STM32 上？如果以后要把雷达板换成一颗 Linux SoC 来配 AD9523，需要改哪些代码？

> **参考答案**：AD9523 驱动**不知道**自己跑在 STM32 上。它在初始化时接收一个 `platform_ops` 指针（本工程里填 `&stm32_spi_ops`，见 [stm32_spi.c:L119-L124](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/stm32_spi.c#L119-L124)），之后所有 SPI 访问都经这个指针间接调用。换到 Linux SoC 时，只需要新写一个 `linux_spi.c` 提供 `no_os_spi_platform_ops` 的 Linux 实现（底层换成 `read()/write()` 到 `/dev/spidev`），并把初始化里的 `platform_ops` 指过去；ADI 驱动本身和 `main.cpp` 里的配置逻辑（寄存器值）一行都不用改。这就是 no-OS 抽象层的全部价值。

---

### 4.2 电源上下电时序

#### 4.2.1 概念说明

如果说 4.1 是「把工具摆好」，那 4.2 就是「按正确顺序打开每一把工具的电源」。这是 STM32 作为「系统安全权威」最核心的职责。

电源时序之所以重要，是因为**很多芯片对上电顺序有硬性要求**，违反这些要求轻则工作异常、重则永久损坏：

- **FPGA**（Xilinx Artix-7）：核心电压 1.0V 必须先于 1.8V（辅助/IO），1.8V 先于 3.3V。顺序反了会导致 FPGA 上电时序错乱、配置失败。
- **时钟发生器 AD9523**：要先上 1.8V 再上 3.3V，并在复位释放后才能写寄存器。
- **ADAR1000 相移器**：要先上 3.3V 再上 5V，并先关掉混频器（不让射频乱发）再校准。
- **GaN 功放（QPA2962）**：栅压 Vg 必须先于漏极 VDD 上电。若先加 VDD，管子会因无栅极偏置而进入不可控大电流状态——「烧管子」。
- **OCXO**（恒温晶振）：上电后需要几分钟预热到热稳定，否则输出的 10MHz 基准频率漂移会直接污染整条时钟链。

这些任务交给 FPGA 不合适，因为 FPGA 本身**就是被上电的对象之一**——它得先被人把电通好、从复位里拉起来，才能干活。所以「上电时序」只能由一个**独立、先于一切醒来、不依赖被管对象**的角色来执行，这个角色就是 STM32。

#### 4.2.2 核心流程

STM32 的上电时序在 `main()` 里是一段长长的线性流程，大致分 6 个阶段：

```text
阶段 0: HAL/时钟/外设初始化（见 4.1）
阶段 1: OCXO 预热        — 等 180 秒（喂狗循环）
阶段 2: AD9523 上电+配置  — 1.8V → 3.3V → 释放复位 → SPI 写寄存器
阶段 3: FPGA 上电        — 1.0V → 1.8V → 3.3V
阶段 4: ADAR1000 上电    — 先关混频器 → 3.3V → 5V → 校准
阶段 5: 功放 PA 上电     — DAC 设 Vg → ADC 校准 Idq → 加 RFPA VDD
阶段 6: 复位 FPGA、开混频器、进主循环
```

对应还有一个「镜像」的**下电时序**（`systemPowerDownSequence`）和一套**紧急停机**（`Emergency_Stop`），用于故障时安全关断功放。下电顺序与上电相反：先撤偏置、再撤电源。

下电与紧急停机的关键设计是：**「快到慢」地切断射频通路**——先停混频器（射频立即停）、再撤 PA 元件级电源、最后撤 bulk 电源。

#### 4.2.3 源码精读

**(1) OCXO 预热：必须边等边喂狗**

[9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp:L1420-L1429](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1420-L1429) —— 这是整个时序里最反直觉的一段。OCXO 要预热 3 分钟，但你**不能**直接写 `HAL_Delay(180000)`：

```cpp
  //Wait for OCXO 3mn
  DIAG("CLK", "OCXO warmup starting -- waiting 180 s (3 min)");
  uint32_t ocxo_start = HAL_GetTick();
  /* [GAP-3 FIX 2] Cannot use HAL_Delay(180000) — IWDG would reset MCU.
   * Instead loop in 1-second steps, kicking the watchdog each iteration. */
  for (int ocxo_sec = 0; ocxo_sec < 180; ocxo_sec++) {
      HAL_IWDG_Refresh(&hiwdg);
      HAL_Delay(1000);
  }
```

原因正是 4.1 的看门狗：IWDG 超时只有约 4 秒。若写 `HAL_Delay(180000)`，MCU 会在这 180 秒里死等，根本没机会喂狗，于是第 4 秒就被看门狗复位——然后从头开始，永远卡在「等 180 秒 → 复位 → 等 180 秒」的死循环里。解决办法是把长等待**拆成 1 秒的小段**，每段之间喂一次狗。这条注释 `[GAP-3 FIX 2]` 标记的就是这个 bug 的修复。这是「时序约束」与「安全约束」相互冲突时的典型折中。

**(2) AD9523 上电时序：1.8V → 3.3V → 释放复位**

[9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp:L1431-L1445](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1431-L1445) —— 先拉低复位（保持 AD9523 在复位态）、依次使能 1.8V 与 3.3V 时钟电源轨、再释放复位，之后才能写寄存器：

```cpp
  DIAG("PWR", "Asserting AD9523 reset (pin LOW)");
  HAL_GPIO_WritePin(AD9523_RESET_GPIO_Port,AD9523_RESET_Pin,GPIO_PIN_RESET);

  //Power sequencing AD9523
  DIAG("PWR", "Enabling 1.8V clock rail");
  HAL_GPIO_WritePin(EN_P_1V8_CLOCK_GPIO_Port,EN_P_1V8_CLOCK_Pin,GPIO_PIN_SET);
  HAL_Delay(100);
  DIAG("PWR", "Enabling 3.3V clock rail");
  HAL_GPIO_WritePin(EN_P_3V3_CLOCK_GPIO_Port,EN_P_3V3_CLOCK_Pin,GPIO_PIN_SET);
  HAL_Delay(100);
  DIAG("PWR", "Releasing AD9523 reset (pin HIGH)");
  HAL_GPIO_WritePin(AD9523_RESET_GPIO_Port,AD9523_RESET_Pin,GPIO_PIN_SET);
  HAL_Delay(100);
```

注意每个动作之间有 `HAL_Delay(100)`（100ms）间隔——电源轨稳定需要时间，立刻写寄存器会失败。这就是 4.1.2 流程图里阶段 2 的「先上电、释放复位、再 `configure_ad9523()`」。

> 一个值得注意的历史 bug：[main.cpp:L1210-L1212](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1210-L1212) 的注释 `[Bug #2 FIXED]` 记录：曾有一段代码在 AD9523 **还在复位态时**就调用 `ad9523_setup()` 写寄存器，结果所有写入都被芯片忽略。修复方式就是删掉那次过早的 setup 调用，只保留「释放复位之后」的那一次。这正是「时序错了，代码看似正常但完全不工作」的典型。

**(3) FPGA 上电时序：1.0V → 1.8V → 3.3V**

[9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp:L1474-L1485](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1474-L1485) —— Xilinx 7 系 FPGA 要求核心电压（1.0V，VCCINT）先于辅助电压（1.8V，VCCAUX）先于 IO 电压（3.3V，VCCO）：

```cpp
  //Power sequencing FPGA
  DIAG("PWR", "Enabling 1.0V FPGA rail");
  HAL_GPIO_WritePin(EN_P_1V0_FPGA_GPIO_Port,EN_P_1V0_FPGA_Pin,GPIO_PIN_SET);
  HAL_Delay(100);
  DIAG("PWR", "Enabling 1.8V FPGA rail");
  HAL_GPIO_WritePin(EN_P_1V8_FPGA_GPIO_Port,EN_P_1V8_FPGA_Pin,GPIO_PIN_SET);
  HAL_Delay(100);
  DIAG("PWR", "Enabling 3.3V FPGA rail");
  HAL_GPIO_WritePin(EN_P_3V3_FPGA_GPIO_Port,EN_P_3V3_FPGA_Pin,GPIO_PIN_SET);
  HAL_Delay(100);
```

这段代码直接回答了实践任务的后半问「电源时序为何由 STM32 而非 FPGA 控制」：**FPGA 自己的电源就是被这段代码控制的对象**，它不可能在「自己还没上电」时去执行上电时序。这是一个「鸡生蛋」问题，唯一的解法是引入一个**比 FPGA 更早醒来、且不受 FPGA 上电影响**的管理者——STM32（它由板上的常驻 3.3V LDO 供电，独立于被控的各条轨）。

**(4) ADAR1000 上电：先关射频，再上电校准**

[9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp:L1744-L1757](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1744-L1757) —— 注意第一行不是「上电」，而是**先关掉混频器**，确保上电过程中没有射频乱发：

```cpp
  //Tell FPGA to turn off TX RF signal by disabling Mixers
  DIAG("BF", "Disabling TX mixers (GPIOD pin 11 LOW)");
  HAL_GPIO_WritePin(GPIOD, GPIO_PIN_11, GPIO_PIN_RESET);

  DIAG("PWR", "Enabling 3.3V ADAR12 + ADAR34 rails");
  HAL_GPIO_WritePin(EN_P_3V3_ADAR12_GPIO_Port,EN_P_3V3_ADAR12_Pin,GPIO_PIN_SET);
  HAL_GPIO_WritePin(EN_P_3V3_ADAR34_GPIO_Port,EN_P_3V3_ADAR34_Pin,GPIO_PIN_SET);
  HAL_Delay(500);
  DIAG("PWR", "Enabling 5.0V ADAR rail");
  HAL_GPIO_WritePin(EN_P_5V0_ADAR_GPIO_Port,EN_P_5V0_ADAR_Pin,GPIO_PIN_SET);
```

这里 3.3V 与 5V 之间留了 **500ms**（比 AD9523 的 100ms 更长），因为 ADAR1000 上电后需要时间完成内部自举。之后才调用 `systemPowerUpSequence()` 做 ADTR1107 前端 + ADAR1000 校准（见 [main.cpp:L354-L397](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L354-L397)）。

**(5) 功放 PA 上电 + Idq 闭环校准**

这是时序里最精细的一步。GaN 功放的栅压 Vg 决定静态工作电流 Idq；Vg 设错，Idq 会过大（烧管）或过小（无放大）。所以上电时必须**边设 Vg、边读 Idq、闭环逼近目标值**。DAC5578 设 Vg、ADS7830 读 Idq：

[9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp:L1932-L1947](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1932-L1947) —— 对每个通道，逐步降低 DAC 值（降低 Vg → 改变 Idq），直到 Idq 接近 1.680A：

```cpp
  DIAG("PA", "Starting Idq calibration loop for DAC1 channels 0-7 (target=1.680A)");
  for (uint8_t channel = 0; channel < 8; channel++){
      uint8_t safety_counter = 0;
      DAC_val = 126; // Reset for each channel
      do {
          if (safety_counter++ > 50) { // Prevent infinite loop
              // ...
              break;
          }
          DAC_val = DAC_val - 4;
          DAC5578_WriteAndUpdateChannelValue(&hdac1, channel, DAC_val);
          adc1_readings[channel] = ADS7830_Measure_SingleEnded(&hadc1, channel);
          Idq_reading[channel] = (3.3/255) * adc1_readings[channel] / (50 * 0.005);
      } while (DAC_val > 38 && abs(Idq_reading[channel] - 1.680) > 0.2);
```

Idq 的换算公式 `Idq = Vadc / (G × Rshunt)`，其中 `G=50`（INA241A3 电流放大器增益）、`Rshunt=5mΩ`。`safety_counter` 防止校准环卡死——这也是为什么这步必须放在「加 RFPA VDD 之前」：先让 Idq 稳了，再加漏极高电压。

**(6) 紧急停机：从射频到电源逐层切断**

[9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp:L820-L858](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L820-L858) —— 当发生过流、过温等危险时，`Emergency_Stop()` 按「快到慢」切断，并在最后**死循环里持续喂狗**：

```cpp
    /* Immediately clear all DAC outputs to zero using hardware CLR */
    DAC5578_ActivateClearPin(&hdac1);
    DAC5578_ActivateClearPin(&hdac2);
    // 1. TX mixers (stop RF immediately)
    HAL_GPIO_WritePin(GPIOD, GPIO_PIN_11, GPIO_PIN_RESET);
    // 2. PA 5V per-element supplies
    // 3. PA 5.5V bulk supply
    // 4. RFPA VDD enable
    // ...
    /* Keep outputs cleared until reset.
     * MUST refresh IWDG here — otherwise the watchdog would reset the MCU,
     * re-running startup code which re-energizes PA rails. */
    while (1) {
        HAL_IWDG_Refresh(&hiwdg);
        HAL_Delay(100);
    }
```

末尾这段注释是点睛之笔：紧急停机后**绝不能让看门狗复位 MCU**。因为复位会重跑 `main()`，而 `main()` 的上电时序会**重新给功放上电**——这就把一次「安全停机」变成「反复上电烧管」。所以死循环里要不断喂狗，保住「停机」状态，直到人工干预。这是「安全设计」与「看门狗」冲突的又一个例子，和 OCXO 那段正好相反。

#### 4.2.4 代码实践

**实践目标**：用一张时序图把 6 个上电阶段串起来，体会「先上谁、后上谁、为什么」。

**操作步骤**：

1. 在 `main.cpp` 里依次定位以下代码块，记下它们的行号与关键 GPIO 引脚：
   - OCXO 预热循环（[L1420-L1429](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1420-L1429)）。
   - AD9523 上电（[L1431-L1445](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1431-L1445)）。
   - FPGA 上电（[L1474-L1485](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1474-L1485)）。
   - ADAR1000 上电（[L1744-L1757](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1744-L1757)）。
   - PA 上电 + Idq 校准（[L1840-L1972](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1840-L1972)）。
   - 复位 FPGA + 开混频器（[L1974-L1985](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1974-L1985)）。
2. 在纸上（或文本里）画一条从左到右的时间轴，把每个「`EN_P_*_Pin` 拉高」事件按出现顺序标上去，写出该事件的电压与目的。
3. 思考题：如果把 ADAR1000 上电（阶段 4）和 FPGA 上电（阶段 3）**对调**，会发生什么？把 OCXO 预热（阶段 1）移到最后又会怎样？

**需要观察的现象**：你会注意到每两步之间都插着 `HAL_Delay(100)` 或更长。这些延时不是「偷懒」，而是给电源轨留出稳定时间——这是「时序」与「延时」两个概念的区别：时序是先后顺序，延时是每步之间的留白。

**预期结果**：你能画出一张 6 阶段时序图，并在每个阶段旁标注「为什么必须在此时」（例如：FPGA 必须在 AD9523 之后上电，因为 FPGA 的系统时钟 100MHz 来自 AD9523）。

#### 4.2.5 小练习与答案

**练习 1**：OCXO 预热为什么要 180 秒？这 180 秒里 STM32 是「完全闲着」的吗？

> **参考答案**：OCXO（恒温晶振）内部有一个加热元件，把晶体烘到设定温度并稳定下来需要数分钟；只有温度稳定，它输出的 10MHz 基准频率才不会漂移。而 AD9523 又把这个 10MHz 倍频分发给全板，所以 OCXO 没热稳就配 AD9523 等于「在流沙上盖房子」。这 180 秒里 STM32 **并非闲着**——它每秒都要调用一次 `HAL_IWDG_Refresh(&hiwdg)` 喂狗（见 [L1425-L1428](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1425-L1428)），否则看门狗第 4 秒就复位 MCU，预热永远完不成。

**练习 2**：`Emergency_Stop()` 的死循环里为什么要持续 `HAL_IWDG_Refresh`？如果不喂狗，会导致什么具体后果？

> **参考答案**：紧急停机后系统必须**停留在「电源全断」的安全状态**等待人工处理。死循环里若不喂狗，IWDG 会在约 4 秒后硬件复位 MCU，使 `main()` 从头跑一遍——而 `main()` 的上电时序（4.2.3 的阶段 2~5）会**重新把功放 PA 的电源轨一条条拉起来**。这等于在「已经判定危险、刚切断电源」之后立刻又给功放上电，可能反复触发过流/过温、最终烧毁 GaN 功放。所以喂狗在这里不是「防止卡死」，反而是「故意保持卡死」——把一次安全停机锁死，避免被看门狗意外复活。详见 [main.cpp:L851-L857](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L851-L857) 的注释。

**练习 3**：阶段 6（[L1974-L1985](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1974-L1985)）先把 FPGA 复位（PD12 拉低再拉高），再把混频器使能（PD11 拉高）。为什么这个顺序不能反？

> **参考答案**：混频器使能（PD11 高）会让射频信号真正从发射链灌出去。如果先开混频器、再复位 FPGA，那么「射频已经在发」但「FPGA 还在复位、chirp 控制器还没跑起来」的这几毫秒里，DAC 输出、TR 开关、混频器使能都处于未定义状态，可能发出乱码射频。先复位 FPGA、等它的 chirp 控制器与发射状态机就绪，再开混频器，才能保证「射频一通就是受控的波形」。

---

### 4.3 STM32↔FPGA 通信与主机 USB 通道

#### 4.3.1 概念说明

STM32 配好电源、拉起 FPGA 之后，它的工作并没有结束——它要在主循环里**持续和 FPGA 对话**，并在故障时**通知 FPGA 停发射频**。这里有一个反直觉的事实：STM32 和 FPGA 之间**没有用高速总线（SPI/并口）通信**，而是用了一组**慢速 GPIO 握手信号**。

为什么不走 SPI？因为 STM32 与 FPGA 之间要交换的不是「数据」，而是「事件」——「新的一个 chirp 开始了」「仰角变了」「方位角变了」「该发/不该发射频」。事件是单比特的、对时序敏感但对带宽不敏感，一根 GPIO 翻转一行就够了，比 SPI 更轻、更实时。这与 u3-l2 讲过的「FPGA 内部跨时钟域用 toggle-CDC 传脉冲」是同一种思想：**用最简单的电平翻转表达事件**。

本模块覆盖两条通信路径：

1. **STM32 → FPGA（GPIO 握手）**：chirp 节拍、仰角/方位步进、混频器使能、复位。
2. **主机 PC → STM32（USB CDC）**：`USBHandler` 解析上位机发来的设置包，`RadarSettings` 把它拆成参数。注意：雷达的高速数据回传走的是 **FPGA↔PC 的直连 USB**（FT601/FT2232H，见 u6-l1），**不经过 STM32**；STM32 这条 USB CDC 只承载慢速的控制/状态文本。

#### 4.3.2 核心流程

STM32 与 FPGA 的 GPIO 握手约定（全部在 `GPIOD` 上）：

| GPIO | 方向 | 含义 |
| --- | --- | --- |
| `PD8` | STM32→FPGA | 翻转一次 = 一个新 chirp 开始 |
| `PD9` | STM32→FPGA | 翻转一次 = 仰角（elevation）变了 |
| `PD10` | STM32→FPGA | 翻转一次 = 方位角（azimuth）变了 |
| `PD11` | STM32→FPGA | 混频器使能（高=发射频，低=关射频） |
| `PD12` | STM32→FPGA | FPGA 复位（低脉冲） |
| `PD13`（DIG5） | FPGA→STM32 | AGC 饱和标志（外环 AGC 输入） |
| `PD14`（DIG6） | FPGA→STM32 | AGC 使能（外环 AGC 同步） |

主机 PC↔STM32 的 USB CDC 通道则是一个三态状态机：

```text
WAITING_FOR_START  ──收到 [23,46,158,237]──▶  RECEIVING_SETTINGS
RECEIVING_SETTINGS ──收齐 "SET...END" 包──▶  READY_FOR_DATA
READY_FOR_DATA     ──（忽略后续字节）──▶  READY_FOR_DATA
```

#### 4.3.3 源码精读

**(1) GPIO 握手：一次 chirp 用一次翻转**

[9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp:L486-L504](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L486-L504) —— `executeChirpSequence` 里每发一个 chirp，就把 `PD8` 翻转一次，作为「新 chirp」事件送给 FPGA：

```cpp
    // First chirp sequence (microsecond timing)
    for(int i = 0; i < num_chirps; i++) {
        HAL_GPIO_TogglePin(GPIOD, GPIO_PIN_8); // New chirp signal to FPGA
        adarManager.pulseTXMode();
        delay_us((uint32_t)T1);
        adarManager.pulseRXMode();
        delay_us((uint32_t)(PRI1 - T1));
    }
```

注意它用的是 `TogglePin`（翻转），不是 `WritePin(SET)`。翻转意味着 FPGA 端只要做**边沿检测**（rising 或 falling 都算）就能还原出「事件次数」——这比「高=有事件、低=无事件」的电平约定更稳健，因为电平约定在两个事件之间必须回到低，事件挨得太近时会撞在一起。这正是 u5-l1 讲过的 toggle-CDC 思想在 MCU 侧的对应写法。

仰角与方位角也是同样的翻转约定：

[9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp:L525-L527](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L525-L527) 与 [L557-L558](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L557-L558)：

```cpp
    for(int beam_pos = 0; beam_pos < 15; beam_pos++) {
    	HAL_GPIO_TogglePin(GPIOD, GPIO_PIN_9);// Notify FPGA of elevation change
        // ...
    }
    // ...
    HAL_GPIO_TogglePin(GPIOD,GPIO_PIN_10);//Tell FPGA that there is a new azimuth
```

**(2) 混频器使能：电平信号（非翻转）**

与 chirp 不同，混频器使能用的是**电平**而非翻转——高=发、低=停，因为它表达的是「状态」而非「事件」：

[9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp:L1983-L1985](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1983-L1985) —— 上电完毕后打开混频器：

```cpp
  //Tell FPGA to apply TX RF by enabling Mixers
  DIAG("FPGA", "Enabling TX mixers (GPIOD pin 11 HIGH)");
  HAL_GPIO_WritePin(GPIOD, GPIO_PIN_11, GPIO_PIN_SET);
```

故障时第一件事就是把它拉低（前面 4.2.3 的 `Emergency_Stop` 与主循环里的安全模式都是如此），因为「关混频器」是停止射频最快的一刀，比关电源还快。

**(3) FPGA→STM32 的反向 GPIO：跨层 AGC 外环**

GPIO 不是单向的。FPGA 也会通过 `PD13/PD14` 把内环 AGC 的状态回报给 STM32，让 STM32 做「外环」增益调整：

[9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp:L2191-L2205](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L2191-L2205) —— 读 `DIG6`（使能）与 `DIG5`（饱和），做 2 帧去抖后调整 ADAR1000 的增益：

```cpp
      {
          bool dig6_now = (HAL_GPIO_ReadPin(FPGA_DIG6_GPIO_Port,
                                            FPGA_DIG6_Pin) == GPIO_PIN_SET);
          static bool dig6_prev = false;  // matches boot default (AGC off)
          if (dig6_now == dig6_prev) {
              outerAgc.enabled = dig6_now;
          }
          dig6_prev = dig6_now;
      }
      if (outerAgc.enabled) {
          bool sat = HAL_GPIO_ReadPin(FPGA_DIG5_SAT_GPIO_Port,
                                      FPGA_DIG5_SAT_Pin) == GPIO_PIN_SET;
          outerAgc.update(sat);
          outerAgc.applyGain(adarManager);
      }
```

这就是 u2-l3 提到的「Hybrid AGC 跨层闭环」在 MCU 侧的入口：FPGA 内环检测饱和 → 经一根 GPIO 告诉 STM32 → STM32 外环调 ADAR1000 的 VGA 增益。这条路径完整展开见 u9-l1。

**(4) USB CDC：接收回调挂到 USBHandler**

主机 PC 经 USB CDC 发来的字节，由 HAL 的回调函数 `CDC_Receive_FS` 接收，再转交给全局的 `usbHandler`：

[9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp:L340-L352](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L340-L352) —— 这是个 `extern "C"` 回调，因为 HAL 是 C 库：

```cpp
extern "C" {
    // USB CDC receive callback (called by STM32 HAL)
    void CDC_Receive_FS(uint8_t* Buf, uint32_t *Len) {
        DIAG("USB", "CDC_Receive_FS callback: %lu bytes received", *Len);
        // Process received USB data
        usbHandler.processUSBData(Buf, *Len);

        // Prepare for next reception
        USBD_CDC_SetRxBuffer(&hUsbDeviceFS, &usb_rx_buffer[0]);
        USBD_CDC_ReceivePacket(&hUsbDeviceFS);
    }
}
```

注意末尾两行：处理完本次数据后，立刻「重装」接收缓冲并启动下一次接收——这是 USB CDC 非阻塞接收的标准套路，否则只能收一次。

**(5) USBHandler 状态机：三态解析**

[9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/USBHandler.cpp:L18-L40](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/USBHandler.cpp#L18-L40) —— `processUSBData` 根据 `current_state` 分派到三个处理函数：

```cpp
void USBHandler::processUSBData(const uint8_t* data, uint32_t length) {
    if (data == nullptr || length == 0) { /* ... */ return; }
    switch (current_state) {
        case USBState::WAITING_FOR_START:
            processStartFlag(data, length);
            break;
        case USBState::RECEIVING_SETTINGS:
            processSettingsData(data, length);
            break;
        case USBState::READY_FOR_DATA:
            // Ready to receive radar data commands
            break;
    }
}
```

状态机的价值在于「**跨多次 USB 读取拼出一个完整包**」。USB CDC 每次回调收到的字节数不确定（可能 4 字节、可能 64 字节），一个完整的设置包可能被切成好几段才到齐。状态机让 `USBHandler` 记住「我现在等什么」，从而把碎片重新拼起来。

**(6) 起始标志：先防下溢，再扫描**

[9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/USBHandler.cpp:L42-L68](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/USBHandler.cpp#L42-L68) —— 找 4 字节起始标志 `[23,46,158,237]`，注意第一行的下溢保护：

```cpp
void USBHandler::processStartFlag(const uint8_t* data, uint32_t length) {
    // Start flag: bytes [23, 46, 158, 237]
    const uint8_t START_FLAG[] = {23, 46, 158, 237};
    // Guard: need at least 4 bytes to contain a start flag.
    // Without this, length - 4 wraps to ~4 billion (uint32_t unsigned underflow)
    // and the loop reads far past the buffer boundary.
    if (length < 4) return;
    for (uint32_t i = 0; i <= length - 4; i++) {
        if (memcmp(data + i, START_FLAG, 4) == 0) {
            start_flag_received = true;
            current_state = USBState::RECEIVING_SETTINGS;
            // ...
        }
    }
}
```

`length` 是 `uint32_t`（无符号）。若没有 `if (length < 4) return;`，当 `length=2` 时 `length - 4` 不会变成 -2，而是下溢成约 42 亿，`for` 循环就会从缓冲区一直读到内存尽头——典型缓冲区越界。这行 guard 就是防御性编程的缩影。

> 一个项目演进细节：[main.cpp:L1831-L1835](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1831-L1835) 的 `[STM32-006 FIXED]` 注释记录：生产环境的 V7 PyQt GUI **并不会**发送这套 4 字节起始标志，所以早期一段「等起始标志才进主循环」的阻塞代码会让 MCU 在上电后无限挂死。修复方式是删掉那段阻塞循环，让 MCU 直接进主循环，握手改为非阻塞处理。这说明 `USBHandler` 的「起始标志→设置包」流程目前主要是历史/兼容路径，真正的雷达数据并不依赖它。

**(7) RadarSettings：把字节包拆成参数**

[9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/RadarSettings.cpp:L23-L78](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/RadarSettings.cpp#L23-L78) —— 收齐 `"SET...END"` 包后，按固定偏移逐字段抽取 9 个 double + 1 个 uint32：

```cpp
bool RadarSettings::parseFromUSB(const uint8_t* data, uint32_t length) {
    // Minimum packet size: "SET" + 9 doubles + 1 uint32_t + "END" = 82 bytes
    if (data == nullptr || length < 82) { settings_valid = false; return false; }
    if (memcmp(data, "SET", 3) != 0) { settings_valid = false; return false; }
    if (memcmp(data + length - 3, "END", 3) != 0) { settings_valid = false; return false; }

    uint32_t offset = 3;  // Skip "SET"
    system_frequency = extractDouble(data + offset);   offset += 8;
    chirp_duration_1 = extractDouble(data + offset);   offset += 8;
    // ...
    chirps_per_position = extractUint32(data + offset); offset += 4;
    // ...
    settings_valid = validateSettings();
    return settings_valid;
}
```

`extractDouble`（[L96-L105](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/RadarSettings.cpp#L96-L105)）按大端序把 8 字节拼成 `uint64_t`，再 `memcpy` 成 `double`。这是一种「**位置型**」协议——字段顺序固定、无类型标签，好处是紧凑（82 字节装下 10 个参数），坏处是两端必须严格同步字段顺序，任何一边改了字段顺序另一边就会解析错乱。这与 u6-l2 讲的 FPGA 状态包「位域必须两侧一致」是同一类契约问题。

抽完之后还做范围校验（[L80-L94](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/RadarSettings.cpp#L80-L94)），例如频率必须在 1~100GHz、`chirps_per_position` 在 1~256 之间——防止上位机发来垃圾值导致后端越界或死循环。

#### 4.3.4 代码实践

**实践目标**：跟踪一次「新 chirp」事件从 STM32 的 `TogglePin` 到 FPGA 接收的完整路径，体会 GPIO 握手如何跨时钟域工作。

**操作步骤**：

1. 在 `main.cpp` 中找到所有 `HAL_GPIO_TogglePin(GPIOD, GPIO_PIN_8)` 的调用点（chirp）、`GPIO_PIN_9`（仰角）、`GPIO_PIN_10`（方位）。确认它们都发生在 `executeChirpSequence` 与 `runRadarPulseSequence` 里。
2. 打开 u3-l2（或直接看 [`radar_system_top.v`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v)），搜索 STM32 来的 `stm32_new_chirp` 端口。确认 FPGA 端用了「2 级电平同步 + 边沿检测」把这次翻转还原成一个 100MHz 域的脉冲。
3. 画一张时序图：STM32 的 144MHz 域里 PD8 翻转一次 → 经同步器进入 FPGA 的 120MHz chirp 域 → 触发 `plfm_chirp_controller` 开新 chirp。
4. 思考：为什么 STM32 用 `TogglePin`（翻转）而不是 `WritePin(SET)` 后立刻 `WritePin(RESET)`？后者会产生一个多窄的脉冲，为什么有风险？

**需要观察的现象**：你会看到 MCU 侧的「翻转」和 FPGA 侧的「边沿检测」是天生配对的设计——两边都假设「事件 = 电平变化」，而不是「事件 = 高电平」。这种约定让事件可以无间隔地连续发生（两次翻转之间不需要回零）。

**预期结果**：你能解释清楚「为什么 MCU 与 FPGA 之间用 GPIO 翻转而不是 SPI」——SPI 适合传数据流，GPIO 翻转适合传事件；雷达扫描的 chirp/仰角/方位都是事件，且要求 MCU 与 FPGA 双方对每次事件的计数严格一致，翻转+边沿检测是最轻量可靠的方案。

> 待本地验证：上述 FPGA 端的同步器实现细节，建议结合 u3-l2 与 `radar_system_top.v` 中 `stm32_new_chirp` 的实际接法核对，因为不同 HEAD 下端口命名可能微调。

#### 4.3.5 小练习与答案

**练习 1**：`executeChirpSequence` 里用的是 `HAL_GPIO_TogglePin`（翻转），而 `Emergency_Stop` 里用的是 `HAL_GPIO_WritePin(..., GPIO_PIN_RESET)`（写电平）。这两种用法的本质区别是什么？各适合表达什么？

> **参考答案**：`TogglePin` 表达「**事件**」——每调用一次代表一次发生，接收方靠边沿计数还原次数，事件与事件之间不需要回零，可以无间隔连续发生（chirp、仰角、方位都是此类）。`WritePin(SET/RESET)` 表达「**状态**」——高=一种状态、低=另一种状态，接收方靠当前电平判断，适合「开/关」「使能/禁止」这类持续状态（混频器使能、各电源轨、LED 都是此类）。混用错会出问题：如果用 `WritePin(SET)` 表达「新 chirp」，连续两个 chirp 之间必须夹一个 `WritePin(RESET)`，否则第二个 chirp 的「上升沿」根本不存在、FPGA 检测不到。

**练习 2**：`RadarSettings::parseFromUSB` 为什么要做 `length < 82` 的长度检查和 `validateSettings()` 的范围检查？如果都去掉，最坏会发生什么？

> **参考答案**：这是「**永远不要相信外部输入**」的防御性编程。长度检查（[L25-L28](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/RadarSettings.cpp#L25-L28)）防止短于 82 字节时后续 `extractDouble(data + offset)` 越界读到包外内存；范围检查（[L80-L94](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/RadarSettings.cpp#L80-L94)）防止上位机发来「合法但荒谬」的值（如 `chirps_per_position = 0` 会导致后面循环除零或死循环、`max_distance = 1e30` 会撑爆显示缓冲）。两者都去掉，最坏情况是 USB 线上任何噪声或恶意数据都能让 MCU 越界读写或卡死——而 MCU 卡死又会被看门狗复位、重跑上电时序、给功放重新上电，放大成硬件级事故。

**练习 3**：雷达的高速采样数据（每帧 2048 个距离-多普勒单元）回传到 PC，走的是 STM32 这条 USB CDC 吗？为什么？

> **参考答案**：**不走** STM32。高速数据走的是 **FPGA↔PC 的直连 USB**（FT601 32 位 USB3.0 或 FT2232H 8 位 USB2.0，见 u6-l1），因为 STM32 的 USB CDC 带宽与实时性都远远不够承载雷达数据流。STM32 这条 USB CDC 只承载**慢速**信息：开机时发一次系统状态字符串（`getSystemStatusForGUI`，见 [main.cpp:L2010-L2016](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L2010-L2016)）、可选地接收 GUI 的设置包。这是 u2-l3「三层分工」的又一体现：数据高速通道归 FPGA，控制/状态慢速通道归 STM32。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「**给新同事讲解 STM32 上电后头 5 分钟发生了什么**」的源码阅读型实践。

**任务**：假设你正在带一位刚加入项目的新同事 review STM32 固件。请基于本讲内容，产出一份「上电后头 5 分钟」的逐步讲解文档（可以是 Markdown 笔记），要求：

1. **时间轴**：从上电瞬间到进入主循环，按真实时间顺序列出至少 8 个关键事件，每个事件标注：
   - 大致发生时刻（例如「上电后 ~0s」「上电后 ~180s」「上电后 ~185s」）。
   - 对应的源码位置（文件 + 行号永久链接）。
   - 一句话说明「这一步在干什么、为什么必须在此时」。
2. **总线图**：画一张 STM32 的 5 条总线（`hi2c1/2/3`、`hspi1/4`）与所挂器件的对照表。
3. **三个为什么**：用本讲学到的理由回答——
   - 为什么电源时序由 STM32 而非 FPGA 控制？
   - 为什么 OCXO 预热的 180 秒里要每秒喂一次狗？
   - 为什么 MCU↔FPGA 用 GPIO 翻转而不是 SPI？
4. **一个风险点**：指出整个上电流程中你觉得最容易出错的一步（提示：可从「顺序依赖」「喂狗窗口」「范围校验」里选），并说明如果这步错了会引发什么连锁后果。

**评估标准**：你的文档应当让新同事在不读源码的情况下，也能大致复述出「STM32 上电 → 等晶振 → 配时钟 → 给 FPGA 上电 → 校准功放 → 进主循环巡检」这条主线，并理解每个环节背后的工程理由。

> 这个练习不需要你改任何代码——它训练的是「**把源码读成故事**」的能力，而这正是后续阅读 ADAR1000 波束赋形（u7-l3）、跨层 AGC（u9-l1）等更复杂模块的基础。

---

## 6. 本讲小结

- **STM32 是全板最早醒来的系统管理者**：它独立于被控的各条电源轨，由常驻 3.3V LDO 供电，因此能够执行「给 FPGA 上电」这种 FPGA 自己做不到的鸡生蛋任务。
- **初始化顺序是「先建句柄、再跑业务」**：`main()` 先用一连串 `MX_*_Init()` 把 I2C/SPI/UART/Timer/USB/IWDG 全部配好，再进入 OCXO 预热与电源时序——因为业务逻辑（如配 AD9523）要调用 SPI，而 SPI 必须先就绪。
- **电源时序由 6 个阶段构成**：OCXO 预热 → AD9523 上电+配置 → FPGA 上电（1.0→1.8→3.3V）→ ADAR1000 上电+校准 → 功放 PA 上电+Idq 闭环 → 复位 FPGA/开混频器；每步之间留 `HAL_Delay` 让电源轨稳定，下电与紧急停机严格按「快到慢」切断射频。
- **看门狗（IWDG，~4s）是贯穿全局的安全约束**：长等待（OCXO 180s）必须拆段喂狗；紧急停机的死循环反而要**故意**喂狗以锁死安全状态，避免复位导致功放被重新上电。
- **STM32↔FPGA 用 GPIO 握手而非高速总线**：chirp/仰角/方位用 `TogglePin`（事件、边沿检测），混频器使能用 `WritePin`（状态、电平）；FPGA 反向用 DIG5/DIG6 把 AGC 状态回报给 STM32，构成跨层外环。
- **no-OS 抽象层让 ADI 芯片驱动可移植、可测试**：`stm32_spi_ops` 把 `no_os_spi` 翻译成 `HAL_SPI`，使同一份 AD9523/ADF4382 驱动在 STM32 与 PC 单元测试（shims/mock）下都能跑。

---

## 7. 下一步学习建议

本讲把 STM32 的「上电与主循环骨架」讲完了，但故意没有展开两块「重头戏」：

1. **时钟树与频率合成（u7-l2）**：本讲只讲了「AD9523 上电后由 STM32 配置」，但没讲 STM32 **配了什么**——AD9523 怎么把 100MHz 倍频到 3.6GHz 再分发出 300/400/120/100/60MHz？ADF4382 本振怎么锁相？下一讲专门拆这条时钟链。
2. **ADAR1000 波束赋形与 Idq 校准（u7-l3）**：本讲只提到「ADAR1000 上电后做校准、PA 用 DAC 设 Vg、ADC 读 Idq」，但没讲 4 片相移器如何拼出 16 通道波束、Idq 闭环如何收敛、AGC 外环如何调增益。下一讲深入相控阵前端。

此外，如果你对「**这套 STM32 代码怎么在 PC 上跑测试**」感兴趣，可以直接跳到 **u11-l2《STM32 单元测试与 bug 回归》**——那里会讲 `tests/shims/` 下的桩文件如何让 `main.cpp` 的逻辑（尤其是被 `[Bug #N]`、`[GAP-3 FIX]` 标记修复的部分）在没有真硬件的情况下被验证。

阅读顺序建议：**u7-l2 → u7-l3 → u9-l1**（跨层 AGC）→ u11-l2（测试）。完成这条线后，你将对 STM32 这一层「从上电到波束到安全」有完整的把握。
