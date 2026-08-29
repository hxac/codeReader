# 固件入口：main() 初始化流程与线程模型

## 1. 本讲目标

学完本讲，你应该能够：

1. 按顺序说出 `main()` 中从 `halInit()` 到主循环之间的全部初始化调用，并解释每一步初始化了哪个外设、为什么必须在这个顺序上。
2. 讲清楚固件运行起来之后的线程模型：sweep 线程（`Thread1`）负责什么、main 线程（shell）负责什么、两者如何交接控制权。
3. 说出 `chconf.h` / `halconf.h` / `mcuconf.h` 三个配置文件分别控制什么，以及它们如何决定了 `halInit()` 和 `chSysInit()` 的实际行为。
4. 拿着本讲的「源码地图」，能独立找到后续每一讲（DSP、校准、绘图、shell）的阅读入口。

---

## 2. 前置知识

### 2.1 嵌入式固件的 main() 和 PC 程序不一样

PC 上的程序：操作系统先启动，你的 `main()` 被调用，跑完就 `return`，进程结束。

嵌入式固件：**没有操作系统为你收尾，也没有「程序结束」这回事**。上电复位后，CPU 从 Flash 固定地址开始执行，经过启动代码（ChibiOS 的 `reset_handler`，把 `.data` 段从 Flash 复制到 RAM、清零 `.bss` 段）后进入 `main()`。`main()` 的最后一行是一个 `while (1)` 死循环——固件要么永远循环，要么死机，永远不返回。

所以读嵌入式 `main()` 的正确姿势是把它分成两段：

- **初始化段**：把硬件从「一堆没配置的硅」变成「一台能用的仪器」；
- **主循环段**：永远运转的业务逻辑。

NanoVNA 的 `main()` 更特殊一点：它的主循环只做一件事（跑 USB shell），而真正的仪器业务（扫频、测量、画图）被放在一个**独立线程** `Thread1` 里。理解这个分工是本讲的核心。

### 2.2 RTOS 三要素：线程、栈、调度

NanoVNA 运行在 ChibiOS/RT 这个实时操作系统（RTOS）上。RTOS 最基本的概念是**线程（thread）**：

- 每个线程有自己的**栈（stack））**——一块用于保存局部变量和函数调用返回地址的内存。栈在 16KB RAM 的 STM32F072 上是非常紧缺的资源，每个线程的栈大小都要精打细算。
- RTOS **调度器（scheduler）** 决定哪个线程在 CPU 上运行。Cortex-M0 上 ChibiOS 采用优先级抢占 + 时间片轮转；优先级相同时线程按时间片交替执行。
- 线程可以让出 CPU 等待事件，例如休眠一段时间（`chThdSleepMilliseconds`）或直接睡指令（`__WFI`，Wait For Interrupt，让 CPU 停止取指以省电，等中断来了再醒）。

本讲会遇到两个线程：

| 线程 | 创建方式 | 栈大小 | 优先级 | 职责 |
|---|---|---|---|---|
| main 线程 | `chSysInit()` 之后自然延续 | 0x200（512 字节，链接脚本设定） | NORMALPRIO | 跑 USB shell 主循环 |
| sweep 线程（Thread1） | `chThdCreateStatic()` | 640 字节 | NORMALPRIO-1 | 扫频、测量、绘图、响应 UI |

注意 `Thread1` 的优先级是 `NORMALPRIO-1`，在 ChibiOS 里数字越小优先级越低——也就是说 **shell 的优先级比 sweep 线程高**，保证了串口命令的响应速度。

### 2.3 ChibiOS 速查表

本讲源码里出现的 ChibiOS API，只需掌握这几个：

| API / 宏 | 作用 |
|---|---|
| `halInit()` | 初始化硬件抽象层（HAL）：按 `board.h` 的 `VAL_GPIOx_*` 配置全部 GPIO，并初始化 `halconf.h` 中启用的各外设驱动框架 |
| `chSysInit()` | 启动 RTOS 内核：初始化调度器、系统定时器，创建 idle 线程，最后把 main 变成一个普通线程 |
| `THD_WORKING_AREA(name, size)` | 声明一个线程的静态栈空间 |
| `THD_FUNCTION(name, arg)` | 定义线程函数（宏展开后是带正确签名的函数） |
| `chThdCreateStatic(wa, size, prio, fn, arg)` | 用静态栈创建线程并立即进入就绪队列 |
| `chThdSleepMilliseconds(ms)` | 让当前线程休眠指定毫秒数，让出 CPU |
| `__WFI()` | Cortex-M0 省电指令：睡到下一个中断 |
| `osalThreadSleepMilliseconds(ms)` | 同 `chThdSleepMilliseconds`（新版 HAL 风格命名） |

### 2.4 本讲涉及的 STM32 外设一览

结合 u1-l1 的硬件全景，`main()` 初始化的外设与固件文件的对应关系：

| 外设 | 用途 | 固件文件 | 驱动启用开关（halconf.h） |
|---|---|---|---|
| I2C1 | 挂 si5351 时钟发生器与 tlv320aic3204 编解码器 | `si5351.c`、`tlv320aic3204.c` | `HAL_USE_I2C`（[halconf.h:79](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/halconf.h#L79)） |
| USB（CDC） | 虚拟串口，shell 与上位机的通道 | `usbcfg.c` | `HAL_USE_USB`（[halconf.h:163](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/halconf.h#L163)） |
| SPI1 | 驱动 ili9341 LCD | `ili9341.c` | `HAL_USE_SPI`（[halconf.h:149](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/halconf.h#L149)） |
| I2S2 | 从 codec 采集中频音频样本（DMA） | `main.c` + ChibiOS I2S 驱动 | `HAL_USE_I2S`（[halconf.h:86](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/halconf.h#L86)） |
| DAC2 | 输出一个直流电平（用于偏置/控制） | `main.c` | `HAL_USE_DAC`（[halconf.h:58](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/halconf.h#L58)） |
| EXT（外部中断） | 触摸屏触摸中断 | `ui.c` | `HAL_USE_EXT`（[halconf.h:65](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/halconf.h#L65)） |
| GPT3（通用定时器） | 拨轮/按键轮询节拍 | `ui.c` | `HAL_USE_GPT`（[halconf.h:72](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/halconf.h#L72)） |
| Flash（无 HAL） | 配置与校准数据掉电保存 | `flash.c` | ——（直接操作寄存器） |

术语解释：

- **CDC（Communications Device Class）**：USB 的一个类别，让设备表现为虚拟串口。插上电脑后出现一个串口设备，敲命令就是通过它进出的。
- **DMA（Direct Memory Access）**：外设到内存的直接搬运通道，不需要 CPU 逐字节搬。I2S 采样和 LCD 刷新都靠它提速。
- **EXT（EXTernal interrupt）**：GPIO 引脚电平变化触发的中断，触摸屏的「被按下了」就是靠它通知固件的。

---

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
|---|---|
| `main.c` | 主角。`main()`（2370 行起）、`Thread1`（106 行起）、`sweep()`（857 行起）、shell（2231 行起）全在这个文件 |
| `nanovna.h` | 公共契约。`SWEEP_ENABLE`/`SWEEP_ONCE` 标志、`current_props` 别名宏、`REDRAW_*` 标志都在这里定义 |
| `NANOVNA_STM32_F072/board.h` | 板级定义：每个 GPIO 引脚接了什么、时钟频率、板名 |
| `NANOVNA_STM32_F072/board.c` | 把 `board.h` 的引脚配置组装成 `pal_default_config` 表，供 HAL 初始化 PAL 驱动使用 |
| `chconf.h` | RTOS 内核配置：系统定时器频率、互斥量开关等 |
| `halconf.h` | HAL 配置：启用/关闭哪些外设驱动（上一节表格的开关来源） |
| `mcuconf.h` | MCU 级配置：各驱动的时钟源、中断优先级（例如 I2C1 用哪个时钟源） |
| `ui.c` | `ui_init()`（2279 行）：ADC/EXT/GPT 初始化 |
| `plot.c` | `plot_init()`（1734 行）、`redraw_frame()`（1725 行）：绘图子系统初始化与首帧绘制 |

---

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. `main()` 初始化序列——上电后固件如何把硬件逐个「点亮」；
2. `Thread1` 扫频线程——仪器的发动机如何永动；
3. shell 主循环——命令如何从 USB 串口进入固件。

### 4.1 模块一：main() 初始化序列——从 halInit 到 chThdCreateStatic

#### 4.1.1 概念说明

`main()` 的初始化序列看似是流水账，实际是一条**依赖链**：每一步都依赖前面的步骤已经就绪。理解这条依赖链，就理解了整台仪器的组成：

- **si5351 挂在 I2C 上**，所以 `i2cStart` 必须在 `si5351_init` 之前；
- **LCD 初始化（`ili9341_init`）只依赖 SPI**，所以可以在配置恢复之前先把屏幕点亮；
- **`config_recall()`/`caldata_recall(0)` 从 flash 读回用户设置**，其中 `config.dac_value` 是 DAC 的初值、`frequency0/frequency1` 是扫描范围——所以它们必须发生在 `dacStart`（要填初值）和 `update_frequencies()`（要按恢复的范围生成频点表）之前；
- **`tlv320aic3204_init()` 也走 I2C 配置 codec**，但真正开始采样要等 `i2sStart` 把 I2S2 + DMA 启动起来；
- **`ui_init()` 启用触摸中断和定时器**，而处理这些中断输入的是 `Thread1`——所以它必须在 `chThdCreateStatic` 创建线程之前完成；
- **`plot_init()` + `redraw_frame()` 画出第一帧界面**，让用户在扫频开始前就能看到屏幕框架。

还有一个容易忽略的细节：**shell 在默认配置下并不独立成线程**。看 [main.c:36-37](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L36-L37) 的注释——`VNA_SHELL_THREAD` 被注释掉了，shell 直接跑在 main 线程的主循环里。这是为了省内存：独立线程要多出 442 字节栈（见 [main.c:2315](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2315)），而 STM32F072 只有 16KB RAM。

#### 4.1.2 核心流程

`main()` 全流程（行号对应 [main.c:2370-2455](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2370-L2455)）：

```text
上电复位
  └─ ChibiOS 启动代码（复制 .data、清零 .bss）
       └─ main()
            ├─ [A] RTOS 与总线
            │    1. halInit()            配置 GPIO + HAL 驱动框架
            │    2. chSysInit()          启动调度器，main 成为线程
            │    3. i2cStart(&I2CD1)     启动 I2C1 总线
            │    4. si5351_init()        初始化信号源
            ├─ [B] 通信与显示
            │    5. sduStart(&SDU1)      USB CDC 虚拟串口
            │       (usbDisconnectBus → 等100ms → usbStart → usbConnectBus)
            │    6. ili9341_init()       SPI LCD
            ├─ [C] 状态恢复
            │    7. config_recall()      读回全局配置（DAC值/颜色/触摸校准）
            │    8. caldata_recall(0)    读回 0 号校准槽（频率/轨迹/标记）
            │    9. dacStart(&DACD2)     DAC 输出恢复的电平
            │   10. update_frequencies() 按恢复的范围生成频点表
            ├─ [D] 采集与交互
            │   11. tlv320aic3204_init() 初始化中频采样 codec
            │   12. i2sStart(&I2SD2)     启动 I2S + DMA 采样（并 i2sStartExchange）
            │   13. ui_init()            ADC 电池监测 + 触摸中断 + 定时器
            │   14. plot_init()          绘图子系统（标记全屏待重绘）
            │   15. redraw_frame()       清屏、画频率标注与校准状态
            ├─ [E] 启动发动机
            │   16. chThdCreateStatic(Thread1)  创建 sweep 线程 ← 固件心跳开始
            └─ [F] main 线程进入 while(1)：USB 活跃时跑 shell，否则每秒醒一次
```

其中编号 3~16 就是本讲实践任务要复现的 **14 个初始化调用**（`halInit`/`chSysInit` 属于 RTOS 启动，不计入）。

#### 4.1.3 源码精读

**第一段：RTOS 启动与 I2C 总线、信号源（[main.c:2370-2378](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2370-L2378)）**

```c
int main(void)
{
  halInit();
  chSysInit();

  i2cStart(&I2CD1, &i2ccfg);
  si5351_init();
```

- `halInit()`：按 `board.h` 的引脚表配置所有 GPIO，初始化 HAL 各驱动。引脚表被 [board.c:26-45](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/NANOVNA_STM32_F072/board.c#L26-L45) 组装成 `pal_default_config` 结构，HAL 初始化 PAL 驱动时应用它。
- `chSysInit()`：内核起飞。此后当前执行流「降格」为 main 线程，调度器开始接管。
- `i2cStart(&I2CD1, &i2ccfg)`：启动 I2C1。`i2ccfg` 定义在 [main.c:2331-2357](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2331-L2357)，其中的 `timingr` 时序参数由 `STM32_I2C1SW` 宏决定——当前 [mcuconf.h:62](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/mcuconf.h#L62) 选择 `STM32_I2C1SW_SYSCLK`，因此走 48MHz 系统时钟的 400kHz 分支。这就是「mcuconf.h 的一个宏会改变 main.c 里实际生效的配置」的活例子。
- `si5351_init()`：通过刚启动的 I2C 把时钟发生器配置好（细节在 u2-l2）。

`board.h` 里有什么值得看的？它是整块板子的「接线说明书」：

[board.h:48-63](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/NANOVNA_STM32_F072/board.h#L48-L63) 定义了 GPIOA 上的按键、拨轮、USB、LCD 复位等引脚：

```c
#define GPIOA_BUTTON			0     // 用户按键
#define GPIOA_LEVER1			1     // 拨轮信号 1
#define GPIOA_LEVER2			2     // 拨轮信号 2
#define GPIOA_PUSH				3     // 拨轮按压
...
#define GPIOA_USB_DM            11    // USB 数据-
#define GPIOA_USB_DP            12    // USB 数据+
#define GPIOA_LCD_RESET			15    // LCD 复位
```

[board.h:65-81](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/NANOVNA_STM32_F072/board.h#L65-L81) 则是 GPIOB/GPIOC 上的 SPI、I2C、I2S 和 LED：

```c
#define GPIOB_SPI_SCLK          3     // LCD 的 SPI 时钟
#define GPIOB_SPI_MISO          4
#define GPIOB_SPI_MOSI          5
#define GPIOB_LCD_CS	        6     // LCD 片选
#define GPIOB_LCD_CD    	    7     // LCD 命令/数据选择
#define GPIOB_I2C1_SCL          8     // I2C 时钟线
#define GPIOB_I2C1_SDA          9     // I2C 数据线
#define GPIOB_I2S2_WCLK         12    // I2S 字时钟（接 codec）
#define GPIOB_I2S2_BCLK         13    // I2S 位时钟
#define GPIOB_I2S2_MOSI         15    // I2S 数据
...
#define GPIOC_LED               13    // 板载 LED（扫频时闪烁）
```

另外 [board.h:29-30](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/NANOVNA_STM32_F072/board.h#L29-L30) 声明了外部晶振 8MHz（`STM32_HSECLK 8000000`），ChibiOS 据此推导出 48MHz 系统时钟——这也解释了 `info_about[]` 数组（[main.c:92-104](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L92-L104)）里 `BOARD_NAME`、`VERSION` 等版本信息的来源（`VERSION` 宏由 Makefile 注入，见 u1-l2）。

**第二段：USB CDC 与 LCD（[main.c:2385-2400](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2385-L2400)）**

```c
  sduObjectInit(&SDU1);
  sduStart(&SDU1, &serusbcfg);
  usbDisconnectBus(serusbcfg.usbp);
  chThdSleepMilliseconds(100);
  usbStart(serusbcfg.usbp, &usbcfg);
  usbConnectBus(serusbcfg.usbp);

  ili9341_init();
```

- 先「假装拔线再插线」：`usbDisconnectBus` → 等 100ms → `usbStart` → `usbConnectBus`。这是 USB 规范的常见手法——若复位后 D+ 上拉一直保持，主机会以为设备从没掉线，不重新枚举；先断开再连接强制主机重新识别设备。
- `ili9341_init()` 发送 LCD 的 SPI 初始化命令序列并清屏（细节在 u4-l1）。

**第三段：配置恢复、DAC 与频点表（[main.c:2402-2415](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2402-L2415)）**

```c
/* restore config */
  config_recall();
/* restore frequencies and calibration 0 slot properties from flash memory */
  caldata_recall(0);

  dac1cfg1.init = config.dac_value;
  dacStart(&DACD2, &dac1cfg1);

/* initial frequencies */
  update_frequencies();
```

- `config_recall()` 从 flash 的 `SAVE_CONFIG_ADDR` 区读回 `config_t`（DAC 值、界面颜色、触摸校准、谐波阈值等，结构定义见 [nanovna.h:225-237](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L225-L237)）。
- `caldata_recall(0)` 读回 0 号校准槽 `properties_t`，也就是上次关机时的扫描范围、轨迹设置、标记和校准数据。之后 main.c 里所有 `frequency0`、`sweep_points`、`trace`、`markers` 这些「全局变量」，其实都是 [nanovna.h:395-410](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L395-L410) 定义的 `current_props` 字段别名宏：

```c
#define frequency0 current_props._frequency0
#define sweep_points current_props._sweep_points
#define trace current_props._trace
#define markers current_props._markers
```

- 注意 `dac1cfg1.init = config.dac_value;` 这行——它把恢复的 DAC 初值**填进配置结构体再启动驱动**，所以 `dacStart` 必须排在 `config_recall` 之后。
- `update_frequencies()` 根据恢复的 start/stop 重新生成 101 点频点表 `frequencies[]`，并联动刷新网格与标记索引（细节在 u3-l1）。

**第四段：I2S 采集、UI、绘图与线程创建（[main.c:2417-2430](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2417-L2430)）**

```c
  tlv320aic3204_init();
  i2sInit();
  i2sObjectInit(&I2SD2);
  i2sStart(&I2SD2, &i2sconfig);
  i2sStartExchange(&I2SD2);

  ui_init();
  //Initialize graph plotting
  plot_init();
  redraw_frame();
  chThdCreateStatic(waThread1, sizeof(waThread1), NORMALPRIO-1, Thread1, NULL);
```

- `tlv320aic3204_init()` 用 I2C 配置 codec 的寄存器序列；随后四行启动 I2S2 外设与 DMA，`i2sStartExchange` 一执行，音频样本就开始源源不断 DMA 进 `rx_buffer`（细节在 u2-l3）。
- `ui_init()`（[ui.c:2279-2296](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L2279-L2296)）做三件事：`adc_init()` 初始化电池电压监测、`extStart` 启用触摸屏外部中断、`gptStartContinuous` 启动连续定时器给输入轮询提供节拍。
- `plot_init()`（[plot.c:1734-1737](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1734-L1737)）只有一行 `force_set_markmap()`——把整个绘图区标记为「待重绘」；`redraw_frame()`（[plot.c:1725-1730](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1725-L1730)）清屏并画出底部频率标注和校准状态。
- 最后 `chThdCreateStatic` 创建 sweep 线程：栈用 106 行声明的 `waThread1`（640 字节），优先级 `NORMALPRIO-1`。这行执行完的瞬间，仪器的心脏开始跳动。

**关于线程栈的紧平衡**：[main.c:2366-2369](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2366-L2369) 的注释记录了 main 线程栈的使用实测——`USE_PROCESS_STACKSIZE = 0x200`（512 字节）之下，跑遍所有 shell 命令的峰值占用是 472 字节，只剩 40 字节余量。这就是嵌入式小内存固件的日常。

**三个配置头文件的分工**（本模块的第三块拼图）：

| 文件 | 控制什么 | 本讲相关的关键设置 |
|---|---|---|
| `chconf.h` | RTOS 内核行为 | [chconf.h:44](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chconf.h#L44) `CH_CFG_ST_RESOLUTION 32`（系统时间 32 位）；[chconf.h:51](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chconf.h#L51) `CH_CFG_ST_FREQUENCY 10000`（系统 tick 为 10kHz，即 1 tick = \( 1/10000 \,\text{s} = 100\,\mu\text{s} \)）；[chconf.h:186](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chconf.h#L186) `CH_CFG_USE_MUTEXES FALSE`（**没有启用互斥量**——这解释了 4.3 节 shell 为什么用「延迟执行」而不是加锁来避免并发冲突） |
| `halconf.h` | 启用哪些 HAL 驱动 | 见 2.4 节表格；注意 `HAL_USE_ADC` 为 FALSE（[halconf.h:44](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/halconf.h#L44)），电池 ADC 由 `adc.c` 直接操作寄存器实现 |
| `mcuconf.h` | 驱动的 MCU 级参数 | [mcuconf.h:62](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/mcuconf.h#L62) I2C1 时钟源选 SYSCLK；[mcuconf.h:106](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/mcuconf.h#L106) GPT 用 TIM3；[mcuconf.h:219](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/mcuconf.h#L219) 启用 USB1 |

三者是层叠关系：`mcuconf.h` 被 `halconf.h` 包含，`halconf.h` 又被 `ch.h`/`hal.h` 体系包含。改任何一个都可能波及 `main()` 中调用的行为。

#### 4.1.4 代码实践

**实践目标**：把 `main()` 的 14 个初始化调用整理成一份可执行的「启动时序清单」，强迫自己为每个调用标注它初始化的外设，形成肌肉记忆。

**操作步骤**：

1. 打开 [main.c:2370-2455](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2370-L2455)，通读 `main()`。
2. 在任意有 C 编译器的机器上创建 `init_trace.c`（示例代码，运行在 PC 上，不属于 NanoVNA 固件）：

```c
/* init_trace.c —— 示例代码：打印 NanoVNA main() 的初始化时序
 * 编译运行：gcc -Wall -o init_trace init_trace.c && ./init_trace
 * 顺序依据 main.c:2377-2430 */
#include <stdio.h>

typedef struct {
    const char *call;       /* main() 中的调用 */
    const char *peripheral; /* 初始化的外设/子系统 */
} init_step_t;

/* 顺序与 main.c 完全一致（halInit/chSysInit 属于 RTOS 启动，不计入） */
static const init_step_t boot_seq[] = {
    { "i2cStart(&I2CD1, &i2ccfg)",         "I2C1 总线：si5351 与 codec 都挂在上面" },
    { "si5351_init()",                     "时钟发生器：激励/本振信号源" },
    { "sduStart(&SDU1, &serusbcfg)",       "USB CDC 虚拟串口：shell 与上位机通道" },
    { "ili9341_init()",                    "SPI LCD 显示屏" },
    { "config_recall()",                   "全局配置(DAC值/颜色/触摸校准)从 flash 恢复" },
    { "caldata_recall(0)",                 "0 号校准槽(频率/轨迹/标记)从 flash 恢复" },
    { "dacStart(&DACD2, &dac1cfg1)",       "DAC2 输出恢复的直流电平" },
    { "update_frequencies()",              "按恢复的范围生成频点表 frequencies[]" },
    { "tlv320aic3204_init()",              "音频 codec：中频采样前端" },
    { "i2sStart(&I2SD2, &i2sconfig)",      "I2S2 + DMA 采样通路" },
    { "ui_init()",                         "ADC 电池监测 + EXT 触摸中断 + GPT 定时器" },
    { "plot_init()",                       "绘图子系统：强制全 markmap 重绘" },
    { "redraw_frame()",                    "清屏并画频率标注与校准状态" },
    { "chThdCreateStatic(waThread1, ...)", "创建 sweep 线程 Thread1，固件心跳开始" },
};

int main(void)
{
    unsigned n = sizeof(boot_seq) / sizeof(boot_seq[0]);
    printf("NanoVNA main() 初始化时序（共 %u 步）\n\n", n);
    for (unsigned i = 0; i < n; i++)
        printf("%2u. %-38s -> %s\n", i + 1, boot_seq[i].call, boot_seq[i].peripheral);
    printf("\n初始化完成，main 线程进入 shell 主循环 while(1)\n");
    return 0;
}
```

3. 编译运行：`gcc -Wall -o init_trace init_trace.c && ./init_trace`。

**需要观察的现象**：

- 输出的调用顺序是否与源码逐行对应（拿源码并排比对）；
- 自己填写的 `peripheral` 说明是否能不查资料地讲出「为什么这一步在这个位置」。

**预期结果**：

- 程序输出 14 行编号时序 + 每行的外设说明，顺序与 [main.c:2377-2430](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2377-L2430) 一致；
- 逐行核对后，你应该能回答：为什么 `caldata_recall(0)` 在 `update_frequencies()` 之前？为什么 `ui_init()` 在 `chThdCreateStatic` 之前？（答案见 4.1.1 的依赖链。）

本实践在 PC 上即可完成，无需 NanoVNA 硬件，输出可直接核对源码。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `chThdCreateStatic(...)` 移到 `ui_init()` 之前，会发生什么问题？

**参考答案**：`Thread1` 的循环里会调用 `ui_process()` 处理触摸和拨轮输入，而 `ui_init()` 负责启用触摸外部中断（`extStart`）、定时器（`gptStartContinuous`）和 ADC。线程先于 `ui_init()` 创建的话，第一次 `ui_process()` 可能在中断/定时器尚未配置的状态下运行，读取到未初始化的输入状态；虽然不一定立刻死机，但输入响应不可预期。这体现了初始化顺序的依赖约束。

**练习 2**：`main()` 里 `usbDisconnectBus` 之后为什么有 `chThdSleepMilliseconds(100)`？删掉它有什么风险？

**参考答案**：这是给 USB 主机留出的重新枚举时间——复位后立刻拉高 D+ 上拉，主机会认为设备从未断开而不重新枚举，导致串口不可用。等 100ms 再 `usbConnectBus` 强制主机走一遍完整的设备识别流程。（见 [main.c:2388-2395](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2388-L2395) 的注释。）

**练习 3**：`sweep_points`、`frequency0` 这些「全局变量」实际存放在哪里？

**参考答案**：它们是 [nanovna.h:395-410](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L395-L410) 定义的宏，展开后是全局结构体 `current_props`（类型 `properties_t`）的字段，例如 `#define sweep_points current_props._sweep_points`。这样做让所有可掉电保存的状态集中在一个结构体里，`caldata_save/caldata_recall` 可以整块读写 flash。

---

### 4.2 模块二：Thread1 扫频线程——仪器的心脏

#### 4.2.1 概念说明

`Thread1` 是 NanoVNA 真正的「业务主循环」。它是一个永不退出的线程，一个循环里完成一圈完整的工作：

1. **测量**：如果扫频处于使能状态，调用 `sweep()` 把所有频点测一遍，结果写进 `measured[]` 数组；
2. **执行延迟命令**：如果 shell 主线程排入了命令（`shell_function` 非空），在这个线程里执行它；
3. **处理 UI 输入**：`ui_process()` 读取触摸/拨轮事件；
4. **绘制**：一轮扫频完成后，把数据换算成屏幕坐标（`plot_into_index`）并请求重绘（`draw_all`）。

为什么把测量放在独立线程、shell 放在 main 线程？因为**测量是长任务、交互是短任务**：一次 101 点扫频要几百毫秒到数秒，而串口命令和触摸必须随时被响应。拆成两个线程后，shell 命令里那些会动到测量状态的命令（如 `scan`、`freq`）通过「延迟执行」机制排到 `Thread1` 里跑，避免两个线程同时操作硬件。

关键控制变量是 `sweep_mode`（[nanovna.h:98-100](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L98-L100)）：

```c
#define SWEEP_ENABLE  0x01   /* 连续扫频使能（用户 pause/resume 切换） */
#define SWEEP_ONCE    0x02   /* 只扫一次（shell 的 scan 命令等使用） */
extern int8_t sweep_mode;
```

当两个标志都不置位时，线程执行 `__WFI()` 睡眠省电，等任何中断（USB、触摸、定时器）把它唤醒。

#### 4.2.2 核心流程

`Thread1` 一次迭代（对应 [main.c:112-148](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L112-L148)）：

```text
进入循环 iteration:
  ├─ sweep_mode 含 ENABLE 或 ONCE?
  │    ├─ 是: completed = sweep(true)   ← 真正测量，可被 UI 打断
  │    │       清除 ONCE 位
  │    └─ 否: __WFI() 休眠等中断
  ├─ shell_function 非空?
  │    ├─ 是: 在本线程执行该命令 → 清空 → 睡10ms → continue
  │    └─ 否: 往下
  ├─ ui_process()                       ← 消化触摸/拨轮
  ├─ ENABLE 且 completed?
  │    ├─ 是: (可选) transform_domain 时域变换
  │    │       plot_into_index(measured) 缓存轨迹坐标
  │    │       redraw_request |= REDRAW_CELLS|REDRAW_BATTERY
  │    │       (可选) marker_tracking 时执行 marker_search
  │    └─ 否: 跳过（被打断的半截数据不画）
  └─ draw_all(completed)                ← 按 markmap 刷新屏幕
```

注意两个细节：

- **优先级顺序**：延迟命令 > UI 处理 > 绘制。命令执行后直接 `continue`，意味着那一圈不做 UI 也不画图。
- **`completed` 为 false 时不画**：`sweep()` 中途被 UI 操作打断时返回 false，此时 `measured[]` 里是半新半旧的数据，直接画会留下残影——所以只在完整扫完后才 flush 屏（源码注释也强调 "flush markmap only if scan completed to prevent remaining traces"）。

`sweep()` 内部（对应 [main.c:857-897](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L857-L897)），每个频点做一遍：

```text
for i in 0..sweep_points-1:
  delay = set_frequency(frequencies[i])   # 调 si5351 到该频点
  tlv320aic3204_select(0)                 # 切到 CH0 反射通道
  dsp_start(delay ...)                    # 设定累积次数，复位 DSP
  dsp_wait()                              # 睡到累积完成
  sample_func(measured[0][i])             # 算出复数 gamma 存入数组
  tlv320aic3204_select(1)                 # 切到 CH1 传输通道，重复一遍
  ...
  apply_error_term_at(i)                  # 校准修正（若启用）
  apply_edelay_at(i)                      # 电延迟修正（若非零）
  if 用户请求了操作: return false          # 让线程回上层处理 UI
```

#### 4.2.3 源码精读

**Thread1 全文（[main.c:106-149](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L106-L149)）**：

```c
static THD_WORKING_AREA(waThread1, 640);
static THD_FUNCTION(Thread1, arg)
{
  (void)arg;
  chRegSetThreadName("sweep");          // 给线程命名，调试时可辨认

  while (1) {
    bool completed = false;
    if (sweep_mode&(SWEEP_ENABLE|SWEEP_ONCE)) {
      completed = sweep(true);          // 测量；true = 可被 UI 打断
      sweep_mode&=~SWEEP_ONCE;          // 一次性扫频做完就清标志
    } else {
      __WFI();                          // 无事可做，睡到下一个中断
    }
    // Run Shell command in sweep thread
    if (shell_function) {
      shell_function(shell_nargs - 1, &shell_args[1]);  // 在本线程执行命令
      shell_function = 0;               // 清空，shell 线程据此知道完成
      osalThreadSleepMilliseconds(10);
      continue;                          // 本圈到此为止
    }
    // Process UI inputs
    ui_process();
    if (sweep_mode & SWEEP_ENABLE && completed) {
      if ((domain_mode & DOMAIN_MODE) == DOMAIN_TIME) transform_domain();
      plot_into_index(measured);        // 把复数数组换算为屏幕折线坐标
      redraw_request |= REDRAW_CELLS | REDRAW_BATTERY;
      if (uistat.marker_tracking) { ... } // 标记跟踪搜索
    }
    draw_all(completed);                // 按 markmap 脏区域刷新屏幕
  }
}
```

- 第 106 行 `THD_WORKING_AREA(waThread1, 640)`：静态分配 640 字节线程栈。
- `chRegSetThreadName("sweep")`：线程名会出现在调试器/threads 命令里。
- `redraw_request` 是 [nanovna.h:291-297](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L291-L297) 定义的标志位集合（`REDRAW_CELLS`、`REDRAW_FREQUENCY`、`REDRAW_MARKER` 等），UI 与绘制之间用「请求-响应」的方式解耦。

**sweep() 主体（[main.c:857-897](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L857-L897)）**：

```c
bool sweep(bool break_on_operation)
{
  int i, delay;
  palClearPad(GPIOC, GPIOC_LED);          // 点亮 LED 表示正在扫频
  for (i = 0; i < sweep_points; i++) {
    if (frequencies[i] == 0) break;
    delay = set_frequency(frequencies[i]);     // si5351 切频点
    tlv320aic3204_select(0);                   // CH0: 反射测量
    dsp_start(delay + ((i == 0) ? 1 : 0));
    dsp_wait();
    (*sample_func)(measured[0][i]);            // 计算反射系数

    tlv320aic3204_select(1);                   // CH1: 传输测量
    dsp_start(DELAY_CHANNEL_CHANGE);
    dsp_wait();
    (*sample_func)(measured[1][i]);            // 计算传输系数

    if (cal_status & CALSTAT_APPLY) apply_error_term_at(i);
    if (electrical_delay != 0)      apply_edelay_at(i);

    if (operation_requested && break_on_operation)
      return false;                            // 用户在操作，先回上层
  }
  palSetPad(GPIOC, GPIOC_LED);          // 熄灭 LED
  return true;
}
```

- LED 接在 [board.h:81](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/NANOVNA_STM32_F072/board.h#L81) 定义的 `GPIOC_LED`（PC13）上，扫频时点亮——这就是真机上扫频时 LED 闪烁的来源。
- 注释里的 `// 5300`、`// 700`、`// 1900` 等数字是作者标注的各阶段耗时参考（量级提示），具体单位与测量方式在 u2-l3/u2-l5 讨论，这里只需知道「切频点、切换通道、等待采样」各有时间成本。
- `sample_func` 是函数指针（[main.c:764](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L764)），默认指向 `calculate_gamma`，让采样结果的处理策略可以按模式替换——这是后续 DSP 讲义的入口之一。
- `operation_requested`（[nanovna.h:432-436](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L432-L436)）由触摸/拨轮中断置位，是「测量让位于交互」的开关。

**dsp_start / dsp_wait（[main.c:615-627](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L615-L627)）**：

```c
static inline void
dsp_start(int count)
{
  wait_count = count;
  accumerate_count = bandwidth_accumerate_count[bandwidth];
  reset_dsp_accumerator();
}

static inline void
dsp_wait(void)
{
  while (accumerate_count > 0)
    __WFI();
}
```

- `dsp_start` 设定要等待/累积的缓冲周期数并复位 DSP 累加器；`dsp_wait` 循环睡眠，直到 I2S 回调把 `accumerate_count` 清零。累积次数由当前带宽档位 `bandwidth` 决定——带宽越窄累积越多次，这是 u2-l3/u2-l4 的伏笔。

**暂停/恢复控制（[main.c:151-167](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L151-L167)）**：

```c
static inline void pause_sweep(void)  { sweep_mode &= ~SWEEP_ENABLE; }
static inline void resume_sweep(void) { sweep_mode |= SWEEP_ENABLE; }
void toggle_sweep(void)               { sweep_mode ^= SWEEP_ENABLE; }
```

三个一位操作的函数，就是 UI「暂停/继续」按钮和 shell `pause`/`resume` 命令的全部实现——控制面如此简单，正是因为 `Thread1` 每圈都会重新检查 `sweep_mode`。

#### 4.2.4 代码实践

**实践目标**：用 PC 程序模拟 `Thread1` 的循环调度，直观验证「延迟命令 > UI > 绘制」的优先级和「打断后不画图」的行为。

**操作步骤**：

1. 创建 `thread1_sim.c`（示例代码，逻辑严格对照 [main.c:112-148](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L112-L148)）：

```c
/* thread1_sim.c —— 示例代码：在 PC 上模拟 Thread1 的调度行为
 * 编译运行：gcc -Wall -o thread1_sim thread1_sim.c && ./thread1_sim */
#include <stdio.h>
#include <stdbool.h>

#define SWEEP_ENABLE 0x01
#define SWEEP_ONCE   0x02

static int  sweep_mode = SWEEP_ENABLE;
static int  shell_function_pending = 0;
static int  operation_requested = 0;
static bool completed;

static bool mock_sweep(void) {
    completed = !operation_requested;
    printf("    [sweep] 测量一圈 %s\n", operation_requested ? "（被 UI 打断）" : "完成");
    return completed;
}
static void mock_shell_cmd(void) { printf("    [shell_function] 在 sweep 线程执行延迟命令\n"); }
static void mock_ui_process(void){ printf("    [ui_process] 处理触摸/拨轮\n"); operation_requested = 0; }
static void mock_plot(void)      { printf("    [plot_into_index + draw_all] 缓存轨迹并刷新屏幕\n"); }

int main(void)
{
    for (int round = 0; round < 6; round++) {
        printf("== 第 %d 轮 Thread1 循环 ==\n", round + 1);
        if (round == 2) shell_function_pending = 1;  /* 第3轮注入一条 shell 命令 */
        if (round == 3) operation_requested = 1;     /* 第4轮注入一次用户操作   */

        if (sweep_mode & (SWEEP_ENABLE | SWEEP_ONCE)) {
            completed = mock_sweep();
            sweep_mode &= ~SWEEP_ONCE;
        } else {
            printf("    [__WFI] 扫频未使能，休眠\n");
        }
        if (shell_function_pending) {
            mock_shell_cmd();
            shell_function_pending = 0;
            continue;                                 /* 对照源码：命令后直接 continue */
        }
        mock_ui_process();
        if ((sweep_mode & SWEEP_ENABLE) && completed)
            mock_plot();
    }
    return 0;
}
```

2. 编译运行：`gcc -Wall -o thread1_sim thread1_sim.c && ./thread1_sim`。

**需要观察的现象**：

- 第 3 轮：命令执行后那一轮**没有** `ui_process` 和 `plot` 输出（`continue` 的效果）；
- 第 4 轮：`sweep` 被打断返回未完成，该轮**没有** `plot` 输出，但 `ui_process` 照常运行；
- 第 5 轮起恢复正常：完整扫频 → UI → 绘制。

**预期结果**：输出与上述三条现象一致；把输出与 `Thread1` 源码逐行对照，确认模拟没有偏差。本实践在 PC 上完成，无需硬件。

#### 4.2.5 小练习与答案

**练习 1**：`Thread1` 为什么用 `NORMALPRIO-1` 而不是更高的优先级？

**参考答案**：`NORMALPRIO-1` 低于默认优先级，意味着 shell（main 线程，NORMALPRIO）和内核线程可以抢占它。扫频是长期后台任务，让交互与命令解析优先，才能保证串口命令和 UI 的响应延迟可控。（见 [main.c:2430](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2430)。）

**练习 2**：`sweep_mode = 0`（两个标志都清掉）之后，仪器还会测量吗？触摸还有反应吗？

**参考答案**：不再测量——`Thread1` 走 `__WFI()` 休眠分支；但触摸仍有反应：触摸中断会唤醒 CPU，线程下一圈继续执行 `ui_process()`（只是 `sweep` 分支不进入），可以打开菜单、执行 `resume`。这正是 `pause`/`resume` 命令和 UI 暂停键的工作方式。

**练习 3**：`sweep(true)` 与 `cmd_scan` 里调用的 `sweep(false)`（[main.c:927](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L927)）参数含义有何不同？

**参考答案**：参数是 `break_on_operation`。`Thread1` 传 `true`，允许测量被用户操作打断以便响应 UI；shell 的 `scan` 命令传 `false`，要求一口气扫完整段数据再返回，保证上位机拿到的 101 点数据是完整同一次扫描的结果。

---

### 4.3 模块三：shell 主循环——命令如何进入固件

#### 4.3.1 概念说明

`main()` 初始化完成后的 `while (1)` 就是 shell 主循环。它做的事：

1. 检查 USB 是否被主机枚举成功（`USB_ACTIVE`）；
2. 活跃则打印提示符 `ch> `，读一行命令，解析并执行；
3. 不活跃则休眠 1 秒再查（省电，也避免对不可用流的死循环读）。

这个 shell 是**作者自研的极简实现**，不是 ChibiOS 自带 shell——没有历史记录、没有管道，但有回显、退格处理、引号参数和命令表查找。所有实现只有约 80 行，是「小内存下自己造轮子」的范本。

上一模块埋的伏笔在这里揭晓：因为 [chconf.h:186](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chconf.h#L186) 设定了 `CH_CFG_USE_MUTEXES FALSE`（内核未启用互斥量），固件**不能**用互斥锁来防止「shell 线程执行命令」与「sweep 线程测量」同时操作硬件。解法是把危险命令**搬运到 sweep 线程执行**：shell 只把函数指针登记到 `shell_function`，然后睡眠轮询等 `Thread1` 执行完把它清零。命令表里用 `CMD_WAIT_MUTEX` 标志声明哪些命令走这条路（名字虽叫 MUTEX，实际机制是「延迟到对方线程」）。

#### 4.3.2 核心流程

```text
main 线程 while(1):
  ├─ USB 状态 == USB_ACTIVE?
  │    ├─ 是: 打印 banner "NanoVNA Shell"
  │    │      循环: 打印提示符 "ch> "
  │    │            VNAShell_readLine()  ← 逐字符读+回显，收到 \r 返回
  │    │            VNAShell_executeLine():
  │    │              1. 按空格/tab 切分参数（支持引号包裹）
  │    │              2. 在 commands[] 表中按名字查找
  │    │              3a. 无 CMD_WAIT_MUTEX: 直接调用（main 线程执行）
  │    │              3b. 有 CMD_WAIT_MUTEX: shell_function = 命令
  │    │                  睡眠轮询直到 Thread1 执行并清零
  │    │            未找到命令: 回显 "xxx?"
  │    │      （USB 断开则跳出内层循环）
  │    └─ 否: 跳过
  └─ chThdSleepMilliseconds(1000)  ← 每秒检查一次 USB
```

#### 4.3.3 源码精读

**shell 主循环（[main.c:2432-2454](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2432-L2454)）**：

```c
  while (1) {
    if (SDU1.config->usbp->state == USB_ACTIVE) {
      shell_printf(VNA_SHELL_NEWLINE_STR"NanoVNA Shell"VNA_SHELL_NEWLINE_STR);
      do {
        shell_printf(VNA_SHELL_PROMPT_STR);                 // "ch> "
        if (VNAShell_readLine(shell_line, VNA_SHELL_MAX_LENGTH))
          VNAShell_executeLine(shell_line);
        else
          chThdSleepMilliseconds(200);                      // 流不可用，稍后再试
      } while (SDU1.config->usbp->state == USB_ACTIVE);
    }
    chThdSleepMilliseconds(1000);                           // USB 未激活，每秒查一次
  }
```

`shell_stream` 在文件开头绑定了 USB CDC：`static BaseSequentialStream *shell_stream = (BaseSequentialStream *)&SDU1;`（[main.c:39](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L39)）。命令行缓冲区仅 48 字节、最多 4 个参数（[main.c:46-48](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L46-L48)），够用且省内存。

**VNAShell_readLine（[main.c:2231-2265](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2231-L2265)）**——逐字符读取与回显：

```c
static int VNAShell_readLine(char *line, int max_size)
{
  uint8_t c;
  char *ptr = line;
  while (1) {
    if (streamRead(shell_stream, &c, 1) == 0)
      return 0;                       // 流断开
    if (c == 8 || c == 0x7f) {        // 退格/删除：回显 "\b \b" 擦掉字符
      if (ptr != line) { ... ptr--; }
      continue;
    }
    if (c == '\r') {                  // 回车：行结束
      *ptr = 0;
      return 1;
    }
    if (c < 0x20) continue;           // 其他控制字符丢弃
    if (ptr < line + max_size - 1) {
      streamPut(shell_stream, c);     // 回显
      *ptr++ = (char)c;
    }
  }
}
```

**VNAShell_executeLine（[main.c:2270-2312](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2270-L2312)）**——切分、查表、跨线程执行：

```c
static void VNAShell_executeLine(char *line)
{
  char *lp = line, *ep;
  shell_nargs = 0;
  while (*lp != 0) {
    while (*lp == ' ' || *lp == '\t') lp++;      // 跳过空白
    ep = (*lp == '"') ? strpbrk(++lp, "\"")      // 引号参数：以另一个引号结尾
                      : strpbrk(lp, " \t");      // 普通参数：以空白结尾
    shell_args[shell_nargs++] = lp;
    if ((lp = ep) == NULL) break;
    if (shell_nargs > VNA_SHELL_MAX_ARGUMENTS) { ... return; }
    *lp++ = 0;                                   // 原地切成 C 字符串
  }
  if (shell_nargs == 0) return;
  const VNAShellCommand *scp;
  for (scp = commands; scp->sc_name != NULL; scp++) {
    if (strcmp(scp->sc_name, shell_args[0]) == 0) {
      if (scp->flags & CMD_WAIT_MUTEX) {
        shell_function = scp->sc_function;       // 登记，不执行
        do {
          osalThreadSleepMilliseconds(100);      // 等 sweep 线程执行
        } while (shell_function);
      } else {
        scp->sc_function(shell_nargs - 1, &shell_args[1]);  // 直接执行
      }
      return;
    }
  }
  shell_printf("%s?" VNA_SHELL_NEWLINE_STR, shell_args[0]); // 未知命令
}
```

两个要点：

- 参数是**原地切分**的：把 `line` 缓冲区里的空格直接改成 `\0`，`shell_args[]` 存指针，零拷贝。注意命令收到的 `argc` 是 `shell_nargs - 1`、`argv` 从 `&shell_args[1]` 开始——命令名本身不算参数。
- `CMD_WAIT_MUTEX` 分支把函数指针塞进 `shell_function` 后睡等。对照 4.2.3 的 `Thread1`：它每圈检查 `shell_function`，非空就在 sweep 线程里调用并清零。**同一个命令函数，永远只在一个线程里跑**，硬件资源的天生互斥。

**命令表（[main.c:2143-2157](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2143-L2157)）**：

```c
typedef struct {
  const char        *sc_name;
  vna_shellcmd_t    sc_function;
  uint16_t flags;
} VNAShellCommand;

// Some commands can executed only in sweep thread, not in main cycle
#define CMD_WAIT_MUTEX  1
static const VNAShellCommand commands[] =
{
    {"version"     , cmd_version     , 0},
    {"reset"       , cmd_reset       , 0},
    {"freq"        , cmd_freq        , CMD_WAIT_MUTEX},
    ...
```

注释直白地写明了设计意图：带标志的命令只能在 sweep 线程执行。哪些命令带标志？`freq`、`data`、`scan`、`touchcal`、`touchtest`、`cal`……共同点是**都会动到测量状态或独占外设**。这个表也是后面 u5-l1（shell 命令系统）的入口。

#### 4.3.4 代码实践

**实践目标**：把 `VNAShell_executeLine` 的参数切分逻辑原样搬到 PC 上，验证「argc 不含命令名、引号参数、原地切分」三个行为。

**操作步骤**：

1. 创建 `shell_split.c`（示例代码，切分逻辑逐行照抄 [main.c:2270-2293](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2270-L2293)）：

```c
/* shell_split.c —— 示例代码：在 PC 上复现 VNAShell_executeLine 的参数切分
 * 编译运行：gcc -Wall -o shell_split shell_split.c && ./shell_split */
#include <stdio.h>
#include <string.h>

int main(void)
{
    char line[48] = "scan 1000000 900000000 101 0";
    char *args[8];
    int nargs = 0;
    char *lp = line, *ep;

    while (*lp != 0) {
        while (*lp == ' ' || *lp == '\t') lp++;
        ep = (*lp == '"') ? strpbrk(++lp, "\"") : strpbrk(lp, " \t");
        args[nargs++] = lp;
        if ((lp = ep) == NULL) break;
        *lp++ = 0;                       /* 原地切断 */
    }

    printf("切分出 %d 个 token\n", nargs);
    printf("命令名 shell_args[0] = \"%s\"\n", args[0]);
    printf("命令收到的 argc = %d\n", nargs - 1);
    for (int i = 1; i < nargs; i++)
        printf("argv[%d] = \"%s\"\n", i - 1, args[i]);
    return 0;
}
```

2. 编译运行：`gcc -Wall -o shell_split shell_split.c && ./shell_split`。
3. 把 `line` 改成 `"scan 1000000 900000000 \"a b\""` 再跑一次，观察引号参数如何被当成一个整体。

**需要观察的现象**：

- 第一组输入：切分出 5 个 token，命令收到 `argc = 4`，`argv` 为 `{"1000000","900000000","101","0"}`——正好满足 `cmd_scan` 的参数检查 `argc < 2 || argc > 4`（[main.c:904](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L904)）；
- 第二组输入：`"a b"`（含空格）被切成一个参数。

**预期结果**：两组输出均如上；证明命令收到的参数从 `argv[0]` 开始就是第一个实际参数，命令名已被剥掉。本实践在 PC 上完成，无需硬件。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `version` 命令不需要 `CMD_WAIT_MUTEX`，而 `scan` 需要？

**参考答案**：`version` 只读常量字符串数组 `info_about` 并打印，不触碰测量硬件，在哪个线程跑都安全；`scan` 要改频点表、调用 `sweep()` 独占 si5351/codec/I2S，若与 `Thread1` 的测量并发执行会互相踩硬件状态，所以必须排队到 sweep 线程执行（先 `pause_sweep()` 再扫，见 [main.c:926-927](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L926-L927)）。

**练习 2**：shell 命令行最长多少字符？超长输入会发生什么？

**参考答案**：48 字符（`VNA_SHELL_MAX_LENGTH`，[main.c:48](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L48)）。看 `VNAShell_readLine` 的存储条件 `if (ptr < line + max_size - 1)`：超长部分会被静默丢弃（但仍回显），行尾必然有 `\0`，不会溢出缓冲区。

**练习 3**：如果 USB 线拔掉，`Thread1` 还在扫频吗？屏幕还在更新吗？

**参考答案**：都在。shell 主循环只是退出内层 `do...while` 进入每秒一次的睡眠轮询；`Thread1` 与它互不依赖，会继续扫频、画屏。USB 只是一条控制/数据通道，拔掉不影响仪器本体工作。

---

## 5. 综合实践

**任务：把 `init_trace.c` 升级为「启动时序 + 双线程交接」模拟器 `boot_timeline.c`**，把本讲三个模块串起来。

**要求**：

1. 保留 14 步初始化清单，并为其中至少 6 步追加「依赖说明」列（例如 `caldata_recall(0)` → 「必须在 update_frequencies 之前：频点表要用恢复的 start/stop 生成」）。
2. 初始化打印完成后，用两个函数 `main_thread_loop()` 与 `sweep_thread_loop()` 交替输出模拟双线程：`main_thread_loop` 打印一次 `ch> ` 等待命令（可硬编码一条 `scan`），`sweep_thread_loop` 打印测量与绘图动作，其中 `scan` 命令要体现「登记 shell_function → sweep 线程执行 → 清零 → main 线程继续」的全过程。
3. 运行程序，把输出与真实源码逐行核对：初始化 14 步对照 [main.c:2377-2430](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2370-L2430)，线程交接对照 [main.c:112-148](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L112-L148) 与 [main.c:2296-2308](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2296-L2308)。

**预期结果**：程序输出先按序打印 14 步初始化（含依赖说明），随后输出一段可读的双线程「对话」，能清楚看到 `scan` 命令从 main 线程登记、到 sweep 线程执行、再到 main 线程恢复提示符的完整往返。完成后再回头读一遍真实的 `main()`，应当有「每一行都知道在干嘛」的感觉。

（本实践在 PC 上完成；若你有真机，可用 `minicom`/`screen` 连上 USB 串口敲 `version`、`pause`、`resume`、`scan 1000000 900000000 101 15` 对照观察真机行为——此部分待本地验证。）

---

## 6. 本讲小结

- `main()` 的初始化是一条**依赖链**：I2C 总线 → si5351 信号源 → USB CDC → LCD → flash 配置恢复 → DAC → 频点表 → codec/I2S 采集 → UI 中断 → 绘图首帧 → 创建 `Thread1`，共 14 个关键调用，顺序不可随意调换。
- 固件是**双线程模型**：`Thread1`（640 字节栈，`NORMALPRIO-1`）负责扫频测量、UI 处理与绘图；main 线程（512 字节栈）只跑 USB shell；两者通过 `sweep_mode`、`shell_function`、`operation_requested`、`redraw_request` 这几个简单标志协作。
- `sweep()` 是测量主循环：逐频点「切频率 → 切通道 → 累积采样 → 算复数 → 校准修正」，且可被用户操作打断（`break_on_operation`）。
- shell 是约 80 行的自研实现：`readLine` 逐字符读回显、`executeLine` 原地切分参数查表执行；因为 `CH_CFG_USE_MUTEXES = FALSE`，带 `CMD_WAIT_MUTEX` 标志的命令通过「登记函数指针、延迟到 sweep 线程执行」实现无锁的线程安全。
- `board.h`/`board.c` 是硬件接线说明书（引脚、时钟），`chconf.h`/`halconf.h`/`mcuconf.h` 分别配置 RTOS 内核、HAL 驱动开关和 MCU 级参数——`halInit()`/`chSysInit()` 的行为由这三个文件决定。

## 7. 下一步学习建议

本讲你已经把固件的「骨架」（初始化 + 线程模型）看完了，接下来进入第二单元的测量核心链路：

- **下一讲 u2-l1「VNA 测量原理与 sweep() 主循环」**：深入 `sweep()` 的每一步——5kHz 频偏如何把射频测量搬到音频频段、CH0/CH1 两路测量的物理含义。建议先自己重读 [main.c:857-897](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L857-L897) 带着问题看：`set_frequency` 返回的 `delay` 是干什么的？
- 顺带阅读 `dsp.c`（很短），预习 `sincos_tbl` 与 `dsp_process`——u2-l4 的主角。
- 如果你对「命令如何逐个注册」更感兴趣，也可以先跳去 `main.c` 的 `commands[]` 表（[main.c:2153](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2153)）浏览命令清单，看看每个命令函数的 `VNA_SHELL_FUNCTION` 实现有多短——这会是你 u5-l1 自己添加命令的模板。
