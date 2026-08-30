# 固件入口：main() 初始化流程逐行走读

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出从芯片复位到 `main()` 第一行执行之间发生了什么，以及 SI5351 为什么要在这一切之前就被配置好。
2. 按顺序罗列 `main()` 中二十多个初始化调用各自做什么、为什么排在这个位置。
3. 指出 `config_recall()` 在哪一步恢复用户设置、`uistat` 全局变量的初值从哪里来、校验失败时固件如何回退。
4. 说明 main 线程末尾的 shell 循环如何与 Thread1（统计）、Thread2（显示+UI）以及 I2S 中断回调分工协作。
5. 能够推断"把某个初始化挪到别处会发生什么"，并设计出验证方法。

## 2. 前置知识

- **HAL（硬件抽象层）**：ChibiOS 把"操作每种外设寄存器"的代码封装成驱动对象（如 `I2CD1`、`ADCD1`、`I2SD2`），`xxxStart()` 负责把配置写入寄存器并挂接中断。HAL 之前是裸机（直接写寄存器），之后是"对象+API"。
- **RTOS 内核初始化**：`chSysInit()` 之后调度器开始运转，`main()` 本身被包装成一个线程（此后叫"main 线程"），`chThdSleepMilliseconds()` 这类阻塞调用才可用。注意 `si5351_low.c` 里没有用任何 ChibiOS API——它运行在内核启动之前。
- **静态创建线程**：嵌入式固件不用 `malloc` 建线程，而是用 `THD_WORKING_AREA(waThread1, 128)` 在编译期预留栈空间，再用 `chThdCreateStatic()` 把它变成线程。栈大小是硬约束，128 字节的线程只能干很轻的活。
- **Shell**：ChibiOS 提供的一个组件，把"一行命令 + 参数"分发表（`ShellCommand commands[]`）挂到一个字符流上。这里的字符流是 USB 虚拟串口 `SDU1`。
- **持久化配置的完整性校验**：Flash 里读出来的数据可能是一块全 0xFF（从未写过）或损坏数据。惯用做法是给数据加"魔数（magic，一个约定常量）+ 校验和（checksum）"两道关卡，两关都过才认为数据有效。
- **定点/采样率背景**：只需要知道音频以 48/96/192kHz 采样，I2S 每块缓冲搬运 `AUDIO_BUFFER_LEN`（480）个立体声帧即可，细节留到单元三。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [main.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c) | 固件入口、shell 命令表、两个工作线程、I2S 回调 | `main()` 的 946-1067 行是本讲主线 |
| [flash.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/flash.c) | 配置的 Flash 读写与校验 | `config_recall()`、XOR 校验和 |
| [NANOSDR_STM32_F303/board.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/NANOSDR_STM32_F303/board.c) | ChibiOS 板级支持包（BSP）钩子 | `__early_init()` 里偷跑的 `si5351_setup()` |
| [NANOSDR_STM32_F303/board.h](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/NANOSDR_STM32_F303/board.h) | 引脚宏与 GPIO 静态配置表 | `pal_default_config` 用的 VAL_* 宏 |
| [si5351_low.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351_low.c) | 裸机 I2C 驱动的 SI5351 早期初始化 | `si5351_setup()` |
| [nanosdr.h](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h) | 全局共享头文件 | `uistat_t`、`config_t`、`AUDIO_BUFFER_LEN` |

辅助（只在精读中点到的实现体）：`tlv320aic3204.c`、`dsp.c`、`display.c`、`ili9341.c`、`ui.c`。

## 4. 核心概念与源码讲解

### 4.1 上电第一阶段：`main()` 之前发生的事

#### 4.1.1 概念说明

Cortex-M 复位后，硬件从向量表取出栈指针和复位向量，跳到启动代码（本仓库自定义启动文件为 [crt2.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/crt2.c) 提供的 `__late_init` 等钩子，与 ChibiOS 子模块里的启动汇编配合）。ChibiOS 约定板级包要提供两个钩子：

- `__early_init()`：**栈刚建好、任何其他初始化之前**执行；
- `boardInit()`：在 `halInit()` 内部执行。

CentSDR 的巧妙（或者说"胆大"）之处在于：`__early_init()` 里就调用了 `si5351_setup()`，用**裸机方式**（不经过 ChibiOS 驱动、也不等内核启动）把本振芯片先配好。为什么这么急？因为 SI5351 是正交检波器的本振，越早输出时钟，接收链路越早稳定；而且此时内核还没跑，`chThdSleepMilliseconds()` 之类都不可用，只能用纯寄存器轮询。

#### 4.1.2 核心流程

```text
复位 → 启动汇编建栈
     → __early_init()            ← board.c 提供的钩子
         ├─ si5351_setup()       ← 裸机 I2C 配置本振（si5351_low.c）
         │    ├─ rcc_gpio_init()  复位 AHB/APB、开 I2C1 与 GPIOB 时钟、配 PB8/PB9 复用
         │    ├─ i2c_init(I2C1)   100kHz@8MHz、开模拟滤波、使能外设
         │    └─ si5351_init_bulk() 按 (长度,寄存器,数据...) 哨兵表逐条写入
         └─ stm32_clock_init()   把系统时钟从 8MHz HSE 拉到最终频率
     → __late_init()             ← crt2.c：拷贝 CCM 代码段并上写保护
     → halInit() → ... → boardInit()（本板为空）
     → chSysInit() → main()
```

#### 4.1.3 源码精读

[board.c:72-75](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/NANOSDR_STM32_F303/board.c#L72-L75) 是整个"抢跑"的入口——先配本振，再配系统时钟：

```c
void __early_init(void) {
  si5351_setup();
  stm32_clock_init();
}
```

[si5351_low.c:101-107](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351_low.c#L101-L107) 三步完成裸机初始化；注意它操作的 I2C1 外设稍后会在 `main()` 里被 `i2cStart()` 重新接管。

[si5351_low.c:6-25](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351_low.c#L6-L25) 先复位所有 AHB/APB 外设、启用 I2C1 与 GPIOB 时钟，并把 PB8/PB9 配成复用开漏——这就是"没有 PAL 驱动时手工配 GPIO"的样子。

[si5351_low.c:74-88](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351_low.c#L74-L88) 的配置表用「长度 + 数据字节」为一个单元、`0` 作哨兵结尾（与 tlv320aic3204.c 的表同款编码）。注释写明策略：26MHz 晶振 ×32 = 832MHz PLL，多合成器 832/8MHz = 104 分频，即**开机默认 CLK2 输出 8MHz**。

[board.c:80-81](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/NANOSDR_STM32_F303/board.c#L80-L81) 的 `boardInit()` 是空的——板级事情已经在 `__early_init` 做完了。

[board.c:25-62](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/NANOSDR_STM32_F303/board.c#L25-L62) 的 `pal_default_config` 由 `halInit()` 应用，它把 board.h 里那堆 `VAL_GPIOx_MODER/AFR...` 宏一次性写进 GPIO——LED（PC13）、按键、SPI 等引脚的静态模式都定义在那里。

#### 4.1.4 代码实践

1. **实践目标**：搞清楚 `__early_init` 是"谁"调用的，理解 BSP 钩子的调用合同。
2. **操作步骤**：
   - 在仓库根目录执行 `grep -rn "__early_init" --include="*.c" --include="*.h" .`（ChibiOS 子模块未检出时只会命中 board.c/board.h，这本身就是线索）；
   - 阅读 board.c 中 `__early_init` 上方的原注释："This initialization must be performed just after stack setup and before any other initialization"；
   - 检出 ChibiOS 子模块后（`git submodule update --init`，上一讲 u1-l2 讲过），在 `ChibiOS/os/` 下搜索调用点。具体调用它的汇编/启动文件位于子模块内，本环境中未检出，**行号待确认**。
3. **需要观察的现象**：`__early_init` 的唯一在仓库内的定义与调用合同注释。
4. **预期结果**：能回答"为什么 `si5351_setup()` 里不能调用 `chThdSleepMilliseconds()`"（答：内核尚未启动，`chSysInit()` 还没执行，调度器不存在）。

#### 4.1.5 小练习与答案

**练习 1**：`si5351_low.c` 的 `i2c_init()` 里 `TIMINGR = 0x10420F13` 注释写"100kHz @ 8MHz"。为什么强调 8MHz——之后系统时钟拉高了，这个值还有效吗？

**答案**：该时序按 I2C1 时钟源为 8MHz 计算。看 [si5351_low.c:19](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351_low.c#L19) 的 `RCC->CFGR3 |= RCC_CFGR3_I2C1SW_HSI`——I2C1 被固定挂到 16MHz 内部 HSI 振荡器（注释里的 8MHz 指早期版本/晶振配置，属历史遗留），不随系统时钟变化，因此后续 `stm32_clock_init()` 拉高主频不影响它。这也解释了为什么这段裸机代码在时钟切换前运行是安全的。

**练习 2**：`main()` 里 `i2cStart(&I2CD1, &i2ccfg)`（[main.c:986](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L986)）会不会破坏 `si5351_setup()` 已完成的配置？

**答案**：不会破坏 SI5351 芯片本身（那是一颗独立芯片，配置已写进它的寄存器），但 STM32 这边的 I2C1 外设会被 ChibiOS 驱动按 `i2ccfg`（[main.c:65-70](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L65-L70)，注释自嘲"voodoo magic"）**重新初始化**。此后 TLV320AIC3204 的所有访问都走 `i2cAcquireBus(&I2CD1)` / `i2cMasterTransmitTimeout()`（[tlv320aic3204.c:14-16](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L14-L16)），与 SI5351 共享同一条总线。

### 4.2 内核启动与配置恢复：`halInit` / `chSysInit` / `config_recall`

#### 4.2.1 概念说明

`halInit()` 初始化 HAL 并应用 board.h 的 GPIO 静态配置；`chSysInit()` 启动调度器，从此 `main()` 只是众多线程之一。接下来固件做的第一件"业务"事是 `config_recall()`：把上次关机前保存在 Flash 最后一页的 `config_t` 整块读回 RAM 全局变量 `config`。若 Flash 里没有有效数据（新机器、擦除过、校验失败），`config_recall()` 返回 -1，而 RAM 里的 `config` 保持 `main.c` 里写死的出厂默认值——这就是"掉电不丢 + 出厂回退"的双保险。

#### 4.2.2 核心流程

```text
halInit()  → HAL 就绪，board.h 的 GPIO 配置生效
chSysInit() → 调度器启动，main() 变成线程
config_recall()
   ├─ Flash 0x0801f800 处 magic != 'CONF'(0x434f4e45)? → return -1（保持默认值）
   ├─ 整块 XOR 校验 != 0?                              → return -1（保持默认值）
   └─ memcpy(&config, flash, sizeof)                    → return 0
config.button_polarity != 0 ? → 给 PA0、PB0..PB5 配上拉（板卡版本差异）
uistat = config.uistat        → 把保存的 UI 状态注入全局 uistat
```

校验和的数学很优雅。设结构体除 `checksum` 字段外的所有 32 位字为 \( W_1,\dots,W_n \)，`checksum()` 从初值 \( L \)（`len`）开始连异或。保存时先把 `checksum` 清零再计算：

\[ S = L \oplus W_1 \oplus \cdots \oplus W_n \]

把它写进 `checksum` 字段。读回时对**整个结构体（含 checksum 字段本身）**再做同样的异或：

\[ L \oplus W_1 \oplus \cdots \oplus W_n \oplus S = L \oplus W_1 \oplus \cdots \oplus W_n \oplus L \oplus W_1 \oplus \cdots \oplus W_n = 0 \]

数据完好则必为 0，任何一位翻转都会破坏这个抵消关系。

#### 4.2.3 源码精读

[main.c:955-968](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L955-L968) 是本模块主线：内核起来后第一件事就是恢复配置，然后按板卡版本配上拉、再把 `config.uistat` 拷给全局 `uistat`：

```c
halInit();
chSysInit();

/* restore config */
config_recall();                 // ← 返回值被忽略：失败即静默用默认值

if (config.button_polarity != 0) {
  palSetGroupMode(GPIOA, 1, 0, PAL_MODE_INPUT_PULLUP);
  palSetGroupMode(GPIOB, 6, 0, PAL_MODE_INPUT_PULLUP);
}

uistat = config.uistat;          // 原注释笔误为 "copy uistat from uistat"
```

注意两处顺序依赖：按键上拉依赖 `config.button_polarity`（存在 Flash 里），所以必须在 `config_recall()` 之后；后续所有读 `uistat` 的初始化（如 `update_iqbal()` 读 `uistat.iqbal`）依赖第 968 行的拷贝。

[flash.c:111-125](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/flash.c#L111-L125) 是双关卡校验：先比 magic 再验校验和，都过了才 `memcpy`：

[flash.c:68-77](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/flash.c#L68-L77) 的 `checksum()` 从 `len` 起异或每一个 32 位字——这就是上面公式里 \( L \) 的来源。

[flash.c:82-84](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/flash.c#L82-L84) 把配置区钉在 `0x0801f800`——128KB Flash 的最后一页（页大小 0x800），远离代码段，擦写不会伤到固件。

[main.c:120-163](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L120-L163) 是出厂默认 `config`：默认 567kHz 中波、AM 调制、AGC 中速、18 个预置信道（7.1MHz LSB、14.1/21.1MHz USB、26.8/27.5/28.4MHz FM 立体声……）。`config_recall()` 失败时机器就以这套默认值开机。

[nanosdr.h:289-299](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L289-L299) 的 `config_t` 定义了整块布局：magic、DAC 初值、AGC 参数、100 个信道（[nanosdr.h:282](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L282) `CHANNEL_MAX`）、一份 `uistat_t`、三个板卡特性字节，最后是 checksum。[nanosdr.h:303](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L303) 定义魔数 `'CONF'`。

[nanosdr.h:256-274](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L256-L274) 的 `uistat_t` 记录"用户此刻的状态"：模式档位、频率、调制、音量、增益、AGC 档、CW 音调、IQ 平衡等。**`uistat` 的唯一初值来源就是 `config.uistat`**——理解了这一点，就理解了为什么关机前按一下旋钮保存、下次开机一切如旧。

#### 4.2.4 代码实践

1. **实践目标**：验证 magic + XOR 校验和的回退机制，不依赖硬件。
2. **操作步骤**：
   - 在 PC 上用 C 或 Python 复现 `checksum()`（32 位字连异或、初值为 `len`）和 `config_recall()` 的逻辑，`config_t` 用等长的字节数组代替即可；
   - 构造三种输入：① 全 0xFF（从未写过的擦除态 Flash）；② magic 正确、随机翻转一个数据位；③ 完整正确的保存块；
   - 记录三种子下 `config_recall` 模拟器的返回值。
3. **需要观察的现象**：① 在 magic 关即被拒；② 过了 magic 关但校验和非 0 被拒；③ 返回 0 且数据被拷贝。
4. **预期结果**：与固件行为一致——前两种情况下 RAM 里的默认 `config`（567kHz/AM）原封不动。有硬件时可进一步用 shell 的 `clearconfig 1234` 擦除配置区后重启验证（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：[main.c:959](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L959) `config_recall();` 的返回值被丢弃了。这样写安全吗？如果要更严谨该怎么改？

**答案**：当前语义下安全——失败时 `config` 保持出厂默认值，机器仍能开机。但"配置丢失"这一事件被完全吞掉，用户不知道自己上次的状态没了。更严谨的写法是 `if (config_recall() < 0) { /* 置一个标志，稍后在 LCD 或 shell 里提示 "config restored to defaults" */ }`。

**练习 2**：为什么 `config_save()`（[flash.c:93-95](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/flash.c#L93-L95)）计算校验和前必须先把 `config.checksum` 清零？

**答案**：若不清零，校验和会把"上一次算出的旧 checksum"也异或进去，结果依赖历史内容；清零后按 4.2.2 的公式，读回时整块异或恰好抵消为 0，校验才成立。

**练习 3**：`uistat = config.uistat;`（[main.c:968](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L968)）为什么必须发生在 `update_iqbal()` / `update_agc()` 之前？

**答案**：`update_iqbal()` 读 `uistat.iqbal`（[main.c:234-239](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L234-L239)），`update_agc()` 读 `uistat.agcmode`（[main.c:241-245](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L241-L245)）。拷贝在前，编解码器才能拿到上次保存的 IQ 平衡与 AGC 档位，而不是未初始化的全 0。

### 4.3 模拟外设点火：DAC、ADC 与 USB 串口

#### 4.3.1 概念说明

这一段把三件与"音频主链路"无关的外设先点起来：DAC 输出一路由配置恢复的直流电压；ADC1 为温度/电池/基准电压监测做准备；USB CDC 枚举成虚拟串口，是人操控机器的主要通道。I2C 也在此时从裸机切换到 ChibiOS 驱动，为接下来配置编解码器铺路。

#### 4.3.2 核心流程

```text
dac1cfg1.init = config.dac_value      ← 初值来自恢复的配置
dacStart(&DACD1, &dac1cfg1)           ← DAC1 输出上电即到位
adcStart(&ADCD1, NULL)
adcSTM32EnableTS/VBAT/VREF(&ADCD1)    ← 打开内部温度/电池/基准通道
i2cStart(&I2CD1, &i2ccfg)             ← I2C1 交给 ChibiOS 驱动
sduObjectInit(&SDU1); sduStart(&SDU1, &serusbcfg)
usbDisconnectBus → usbStart → usbConnectBus   ← 先断开再枚举，避免复位后主机不重新识别
```

#### 4.3.3 源码精读

[main.c:970-983](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L970-L983)：DAC 初值取自 `config.dac_value`（默认 1080，12 位量程中点 2047 之下），12 位右对齐模式；随后 ADC 使能三个**内部通道**——温度传感器、VBAT 分压、VREFINT。这三个值只被 `measure_adc()` 轮询读取，用于 `stat` 命令显示。

[main.c:986-991](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L986-L991)：`i2cStart` 接管 I2C1；`sduStart` 挂起 Serial-over-USB CDC 驱动 `SDU1`，配置来自 [usbcfg.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/usbcfg.c)（下一讲 u1-l4 精读）。

[main.c:993-1001](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L993-L1001)：USB 的"断开→启动→连接"三连是经典手法——复位后若 D+ 上拉一直保持，主机不会重新枚举；先断开相当于"假装拔了线"。

[main.c:395-410](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L395-L410) 的 `adc_single_read()` 值得一看：它**绕过** ChibiOS 的 ADC 组转换机制，每次都手写 SMPR/CFGR/SQR1 寄存器、发起一次转换并忙等完成。这就是"初始化归驱动、单次读数归裸机"的混合风格——`adcStart` 只负责把外设上电使能。

#### 4.3.4 代码实践

1. **实践目标**：读懂 `stat` 命令输出的最后三行从哪来。
2. **操作步骤**：接上 USB，打开串口（115200 或 CDC 默认，见下一讲），输入 `stat`；对照 [main.c:444-446](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L444-L446) 三行 `chprintf`，再沿 `adc_single_read(ADC1, ADC1_CHANNEL_TEMP/BAT/VREF)` 回到 [main.c:412-421](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L412-L421)。
3. **需要观察的现象**：`temp` 随手温变化的原始码值、`bat` 电池电压码值、`vref` 接近常数的 VREFINT 码值。
4. **预期结果**：三个 12 位原始码（0~4095）。`vref` 基本不变（内部带隙基准），`temp` 捏住芯片会缓慢上升。无硬件时此实验**待本地验证**，可改为纯阅读：解释为什么 `adc->ISR = adc->ISR;`（写 1 清零中断标志）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 DAC 的初值要通过 `dac1cfg1.init = config.dac_value;`（[main.c:974](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L974)）动态填入，而不是用 [main.c:927-931](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L927-L931) 结构体里写死的 1080？

**答案**：`dac` shell 命令（[main.c:567-578](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L567-L578)）会改 `config.dac_value`，配合 `save` 后该值随配置持久化。启动时用恢复值覆盖默认值，机器才能"记住"上次调好的电压；结构体里的 1080 只是配置丢失时的兜底。

**练习 2**：`usbDisconnectBus()` 与 `usbConnectBus()` 之间没有延时会怎样？代码里被注释掉的 `chThdSleepMilliseconds(200)`（[main.c:999](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L999)）说明什么？

**答案**：某些主机/集线器需要 D+ 上拉先消失一段时间才认定设备离线、随后重新枚举；注释掉延时说明实测当前硬件组合不需要它（USB 状态机自身的复位流程已提供了足够间隔），这是"实测后裁剪"的痕迹。若在某台主机上出现复位后串口消失，恢复这行延时是第一嫌疑。

### 4.4 音频流水线上电：编解码器、I2S 与 DSP

#### 4.4.1 概念说明

这是整机的心脏起搏器安装过程：先把 TLV320AIC3204 编解码器按四张配置表初始化（PLL、时钟、路由、解除静音），再启动 STM32 的 I2S DMA 双缓冲，从此**每搬运完一块样本就触发一次 `i2s_end_callback`，在中断上下文里执行当前解调函数**——机器的"心跳"开始了。最后 `dsp_init()` 初始化立体声导频 PLL 的状态。注意：LCD、shell、工作线程此时都还没起来，也就是说 DSP 先于一切用户界面运转。

#### 4.4.2 核心流程

```text
tlv320aic3204_init()
   ├─ conf_data_pll     PLL 起振（CODEC_CLKIN 来自内部 PLL）
   ├─ conf_data_clk     NDAC/MDAC/NADC/MADC 分频，确定采样率
   ├─ conf_data_routing 输入→ADC→DSP→DAC→耳机 路由与增益
   ├─ wait_ms(40)       等模拟部分稳定
   └─ conf_data_unmute  解除静音
i2sInit()  i2sObjectInit(&I2SD2)
i2sStart(&I2SD2, &i2sconfig)      挂上 rx_buffer/tx_buffer 双缓冲与回调
i2sStartExchange(&I2SD2)          DMA 开跑 → 心跳开始
dsp_init()                        立体声分离状态复位
```

块长为 480 个交织 IQ 帧（[nanosdr.h:93](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L93) `AUDIO_BUFFER_LEN`），回调周期

\[ T = \frac{480}{f_s} \]

48kHz 时 10ms、96kHz 时 5ms、192kHz 时 2.5ms——采样率越高，留给同一段 DSP 代码的时间越短，这正是 `stat` 里 load 百分比随模式变化的原因。

#### 4.4.3 源码精读

[tlv320aic3204.c:183-190](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L183-L190)：四张表 + 40ms 等待，全部经 `I2CD1` 发送。

[main.c:1003-1015](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L1003-L1015)：I2S 五连之后才调 `dsp_init()`。这里藏着本讲最有讨论价值的一个顺序问题——**回调已开跑，DSP 全局状态才初始化**。当前默认解调是静态初值 `am_demod`（[main.c:113](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L113)），而 `dsp_init()` 只做 `stereo_separate_init()`（[dsp.c:895-898](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L895-L898)），AM 路径不碰立体声状态，所以侥幸无害；但若有人把默认 `signal_process` 改成 `fm_demod_stereo`，就会存在"回调先读、初始化后写"的竞争。把 `dsp_init()` 挪到 `i2sStartExchange()` 之前更稳妥。

[main.c:278-286](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L278-L286) 的 `i2sconfig` 把 `tx_buffer`/`rx_buffer`、块长 `AUDIO_BUFFER_LEN * 2`（半字数）和回调 `i2s_end_callback` 绑给驱动。

[main.c:258-276](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L258-L276) 是"心跳"本体：取 DMA 当前块的两个指针 `p`（收）、`q`（发），**调用函数指针 `signal_process` 完成解调**，前后用 DWT 周期计数器记录耗时写入 `stat.busy_cycles/interval_cycles`。注意开头结尾对 `GPIOC_LED` 的置位/清零——PC13 上的 LED 实际是 **DSP 负载指示灯**，每个回调亮一次，而不是传统意义的"心跳闪烁"。模式切换（`set_modulation`，[main.c:179-194](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L179-L194)）只是原子地替换这个函数指针，热切换无需重启 I2S。

[main.c:99-107](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L99-L107)：`rx_buffer`/`tx_buffer` 各 960 个 int16（480 对 I/Q 交织），加上 `buffer`/`buffer2` 四个中间缓冲，由 `buffers_table` 统一登记类型与长度，供 `data` 命令和显示抓取使用。

#### 4.4.4 代码实践

1. **实践目标**：用实测数据验证回调周期公式，并体会采样率对实时预算的影响。
2. **操作步骤**：有硬件时——通过 shell 分别在 `fs 48` 与 `fs 192` 后执行 `stat`，记录 `load: N% (busy/interval)` 中的两个 cycle 数；用 interval_cycles 换算毫秒数（Cortex-M4 DWT 计数频率 = 系统主频，主频由 `board.c` 的 `stm32_clock_init()` 设定，其实现位于 ChibiOS 子模块中，具体数值**待确认**）。无硬件时——纯计算：48kHz 下每秒回调 100 次，若 load 显示 60%，求 DSP 每块可用时间和实际耗时。
3. **需要观察的现象**：192kHz 时 interval_cycles 约为 48kHz 时的 1/4，busy_cycles 基本不变（同一算法）。
4. **预期结果**：回调用时不变的前提下周期缩短 4 倍，load 百分比大约升到原来的 4 倍（AM 模式下算法相同）。数值**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：从代码看，开机后 `set_tune()` 与 `set_modulation()` 一次也没被调用。那么刚上电时机器收的是什么频率、用什么解调？

**答案**：本振停在 `si5351_setup()` 写入的默认 CLK2 = 8MHz（[si5351_low.c:83-84](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351_low.c#L83-L84) 注释），解调停在静态初值 `signal_process = am_demod`、`mode_freq_offset = AM_FREQ_OFFSET`（[main.c:113-114](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L113-L114)）——**与 `uistat.freq`/`uistat.modulation` 显示值无关**。直到用户转动旋钮（触发 `update_frequency`/`recall_channel`）或发 shell 命令，屏幕上"恢复的频率"才真正落到硬件上。这也解释了一个用户体验细节：开机后需要碰一下旋钮，机器才真正开始按显示频率接收。

**练习 2**：`set_fs()`（[main.c:205-226](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L205-L226)）切换采样率时的停启顺序是什么？为什么中间要 `chThdSleepMilliseconds(40)`？

**答案**：顺序为 `tlv320aic3204_stop()`（停 WCLK/BCLK，即编解码器侧分频器下电）→ `i2sStopExchange()`（停 I2S DMA）→ 睡 40ms → `i2sStartExchange()` → `tlv320aic3204_set_fs(fs)`。先停对端时钟再停本端 DMA，避免 DMA 采样到半生不熟的时钟；40ms 是等待编解码器内部状态稳定——源码注释自嘲"wait a second (not enough in 20ms)"，说明这个值是实测调出来的。

### 4.5 人机界面上线：LCD、显示子系统与 UI 输入

#### 4.5.1 概念说明

音频链路跑起来之后才轮到"脸面"：`ili9341_init()` 复位屏幕并灌注初始化寄存器序列；`disp_init()` 初始化 1024 点 CFFT 实例、瀑布图参数、清屏并挂起一次全量 UI 重绘；`ili9341_set_direction()` 按保存的 `lcd_rotation` 决定是否倒置 180°；`ui_init()` 启动编码器外部中断。最后 `update_iqbal()`/`update_agc()` 把恢复的用户参数真正写进编解码器。注意：此刻 Thread2 还没创建，`disp_init()` 只是把 `FLAG_UI` 挂起，真正的绘制要等显示线程开始跑 `disp_process()`。

#### 4.5.2 核心流程

```text
ili9341_init()   spi_init() → 硬复位(PB 复位脚) → 软复位(0x01) → 关显示(0x28) → 初始化序列 → 开显示
disp_init()      arm_cfft_radix4_init_q31(1024) → waterfall_init() → clear_background() → FLAG_UI
ili9341_set_direction(config.lcd_rotation)
shellInit()
创建 Thread1("blink")
ui_init()        extStart(&EXTD1)  编码器 A/B 相中断
update_iqbal(); update_agc()       把 uistat 里的参数落到 TLV320AIC3204
创建 Thread2("button")
```

#### 4.5.3 源码精读

[ili9341.c:187-198](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ili9341.c#L187-L198)：先 `spi_init()` 再硬复位（拉低复位脚 10ms）→ 软复位 → 关显示，之后才走 `ili9341_init_seq` 哨兵表。屏幕这种慢速外设的初始化充满等待，顺序错一点就是白屏。

[display.c:1469-1474](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1469-L1474)：`disp_init()` 四件事——CFFT 实例、瀑布初始化、黑底清屏、置 `FLAG_UI` 请求全量重绘。

[main.c:1017-1034](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L1017-L1034)：LCD 初始化、显示子系统初始化、旋转方向、shell 就绪的顺序。`ili9341_set_direction(config.lcd_rotation)` 在 `disp_init()` **之后**再调一次方向是为了覆盖清屏时的默认方向，旋转后紧接着的 `disp_init()` 全量重绘（见 `cmd_lcd`，[main.c:862-872](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L862-L872) 的用法印证了这个约定）。

[ui.c:221-227](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L221-L227)：`ui_init()` 仅一行有效代码 `extStart(&EXTD1, &extconf)`——把编码器 A/B 相接到 EXTI 中断，之后旋钮每转一格都进中断累加 `enc_count`。

[main.c:1041-1046](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L1041-L1046)：`update_iqbal()` 把 `uistat.iqbal` 换算成系数写入编解码器的 mini-DSP 一阶 IIR（[main.c:234-239](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L234-L239)），`update_agc()` 应用 AGC 档位（[main.c:241-245](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L241-L245)）。两行注释掉的 `tlv320aic3204_config_adc_filter*` 是 DC 抑制实验的遗迹，说明这条链路经过反复调试。

#### 4.5.4 代码实践（本讲核心思考题）

1. **实践目标**：推断"把 `disp_init()` 移到 `tlv320aic3204_init()` 之前会发生什么"，并设计验证方法。
2. **操作步骤（推断）**：
   - 列出 `disp_init()` 的依赖：`arm_cfft_radix4_init_q31`（纯内存，无外设依赖）、`waterfall_init()`（内存参数）、`clear_background()`（**经 SPI 向 LCD 写 320×240 像素**）、`spdispinfo.update_flag = FLAG_UI`（内存）；
   - 再列出 `clear_background()` 的依赖链：→ `ili9341_fill` → `ili9341_bulk` → SPI 发送 → 依赖 `ili9341_init()` 里的 `spi_init()` 和屏幕复位序列；
   - 注意 `main()` 中 `ili9341_init()`（1020 行）本身就在 `tlv320aic3204_init()`（1007 行）**之后**——所以"移到 tlv320aic3204_init 之前"必然也落在 `ili9341_init()` 之前。
3. **推断结果**：`disp_init()` 会在 SPI 尚未配置、屏幕尚未复位/初始化时发送大量像素数据。SPI 外设寄存器是复位默认值（禁用、低速率），数据要么发不出去、要么以错误极性/格式发出；屏幕多半保持白屏/花屏，且 `clear_background()` 的 24 次 `ili9341_fill` 可能因驱动层等待发送完成而**长时间阻塞甚至卡死**（取决于 `spiSend` 的实现是否轮询标志位）。唯一不受影响的是 CFFT 实例和 `update_flag` 这两个纯内存操作——如果只把 CFFT 部分前移倒无妨。
4. **验证方法**：
   - 静态验证（无硬件）：把改动提交编译器检查——`disp_init()` 与 `ili9341_init()` 无编译期依赖，能编过，**说明这类顺序错误编译器抓不住**，只能靠走读和实测；
   - 动态验证（有硬件）：实际交换两行烧录，观察开机屏幕现象与串口 `stat` 的 fps 是否归零（Thread2 的 `disp_process` 若被卡住，fps 停在 0）；再单独试验"只把 `arm_cfft_radix4_init_q31` 前移"应无任何异常，以区分两类依赖。**待本地验证**。
5. **附带结论**：正确的依赖表述是——`disp_init()` 依赖 `ili9341_init()`（SPI+屏幕），与 `tlv320aic3204_init()` 无直接依赖；因此"LCD 早于音频初始化"在原理上可行，前提是把 `ili9341_init()` 一并前移。这个练习的价值在于学会**用依赖链而非行号顺序来推理初始化次序**。

#### 4.5.5 小练习与答案

**练习 1**：`shellInit()`（[main.c:1034](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L1034)）放在两个工作线程创建之前，它做错了会导致什么？

**答案**：`shellInit()` 初始化 shell 子系统的全局状态，必须在任何 shell 线程创建之前调用一次。顺序正确时无感；若放在 `chThdCreateFromHeap` 之后，则第一条 shell 会话可能使用未初始化的链表/互斥量。它不阻塞、不依赖外设，属于"便宜但要早"的调用。

**练习 2**：为什么 `update_iqbal()`/`update_agc()` 不放在 `tlv320aic3204_init()` 刚结束的地方（紧贴 1007 行之后），而要等到 1045 行？

**答案**：它们读取的是全局 `uistat`，而 `uistat = config.uistat` 虽然在 968 行已完成，理论上早调用也能拿到正确值——但放在所有初始化接近收尾处，可以保证写入编解码器的参数不会被后续任何初始化（如 `tlv320aic3204_stop/set_fs` 的路径）意外覆盖。同时它必须在 Thread2 创建之前完成，否则 UI 线程可能以未应用的参数开始刷新显示。这是一种防御性排序，而非硬性数据依赖。

### 4.6 线程就位与 shell 循环：main 的收官

#### 4.6.1 概念说明

初始化完成后，固件的常驻执行者共四类：**main 线程**（只负责孵化 shell）、**Thread1 "blink"**（统计与功率测量）、**Thread2 "button"**（显示刷新 + UI 轮询）、**I2S 回调**（中断上下文的实时 DSP，不属于任何线程）。四者优先级都不高（NORMALPRIO 一档，shell +1），真正的实时性由中断保证——这是小型 RTOS 固件的典型分工：线程做"慢而杂"的事，中断做"快而硬"的事。

#### 4.6.2 核心流程

```text
创建 Thread1("blink",   栈128字, NORMALPRIO)
创建 Thread2("button",  栈512字, NORMALPRIO)
main 线程循环:
    若 USB 已枚举(USB_ACTIVE):
        从堆上创建 shell 线程(NORMALPRIO+1, 栈2048字) 并 chThdWait 等它退出
    否则睡 1000ms
```

#### 4.6.3 源码精读

[main.c:22-47](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L22-L47) Thread1 每 100ms 做一轮：`calc_stat()`（扫 rx_buffer 算 RMS/min/max，[main.c:351-376](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L351-L376)）→ `measure_power_dbm()`（8.8 定点功率）→ `disp_update_power()`；每 10 轮（约 1 秒）再刷新 fps/溢出计数、读三路 ADC、`disp_update()` 请求全量重绘。注意变量 `count` 未初始化即 `++`（[main.c:26,36](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L26-L36)）——栈上随机值起步，属无害但值得注意的瑕疵。

[main.c:906-924](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L906-L924) Thread2 每 10ms 一轮：`disp_process()`（按 FLAG 增量重绘）→ `ui_process()`（按键/编码器状态机）→ `fps_count++` → 查询编解码器粘滞标志统计 ADC 溢出。10ms 周期决定了 UI 响应与波形刷新的上限（fps 上限 100）。

[main.c:1055-1066](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L1055-L1066) shell 循环：USB 枚举成功才从**堆**上创建 shell 线程（`chThdCreateFromHeap`，与两个静态线程形成对比——shell 生命周期不确定，用堆更合适），`chThdWait` 阻塞等它退出（USB 断开时 shell 结束），随后回到外层循环；没插 USB 时每秒醒一次检查状态。main 线程从此再无别的工作。

[main.c:934-940](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L934-L940)：shell 挂在 `SDU1` 上，命令表 `commands`（[main.c:874-904](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L874-L904)，28 条）是下一讲的主角。

线程间同步没有一个互斥量，靠的是**单写单读 + 标志位**：I2S 回调写 `stat`、抓频谱样本；Thread1 读 `rx_buffer` 算统计；Thread2 通过 `spdispinfo.update_flag` 的各个 FLAG 位知道哪块 UI 该重绘（[display.c:1413-1414](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1413-L1414) 起）。这种"低技术但够用"的并发设计在单元五会系统复盘。

#### 4.6.4 代码实践

1. **实践目标**：观察三个执行体的节奏差异。
2. **操作步骤**：有硬件时——插上 USB 打开串口终端，输入 `stat`；对照输出逐项回答来源：`load`（I2S 回调写）、`fps`（Thread2 计数、Thread1 每秒归零）、`callback count`（回调自增）；连续执行几次 `stat` 观察计数增长比例。无硬件时——纯走读：给 [main.c:423-459](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L423-L459) 的每个 `chprintf` 标注"哪个执行体生产的这份数据"。
3. **需要观察的现象**：`callback count` 每秒约增 100（48kHz）或 400（192kHz）；`fps` 稳定在 100 附近（10ms 周期上限）。
4. **预期结果**：数值与 4.4 节的周期公式自洽。**待本地验证**。

#### 4.6.5 小练习与答案

**练习 1**：为什么 shell 线程用 `chThdCreateFromHeap` 而两个工作线程用 `chThdCreateStatic`？

**答案**：Thread1/Thread2 与固件同生共死，栈大小固定且已知（128/512 字），静态预留最省心也最省碎片；shell 线程随 USB 插拔生灭、数量不确定（一次一个但反复创建销毁），从堆分配、`chThdWait` 回收更合适。`SHELL_WA_SIZE`（[main.c:934](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L934)）给到 2048 字，因为 shell 要解析命令行、调 `chprintf`。

**练习 2**：main 线程的 while 循环里 `chThdWait(shelltp)` 阻塞等待期间，Thread1/Thread2 和 I2S 回调会受影响吗？

**答案**：不会。`chThdWait` 只阻塞 main 线程自身，调度器照常运行其他线程；I2S 回调在中断上下文，优先级高于一切线程。四类执行体的调度由 RTOS 抢占式内核和 NVIC 管理，互不依赖 main 线程"推动"。

**练习 3**：`stat.fps` 为什么由 Thread2 递增、Thread1 归零（[main.c:16-45](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L16-L45)）？这是否有竞争？

**答案**：fps 定义为"显示线程每秒循环次数"，生产者自然是 Thread2；Thread1 每约 1 秒读取并清零，作为低速消费者。`fps_count++` 与 `fps_count = 0` 分属两个线程，严格说存在读改写竞争，但计数只是统计观测、偶尔丢 1 无伤大雅，作者据此选择了无锁写法——嵌入式里"可容忍的统计毛刺"常见此类取舍。

## 5. 综合实践

**任务：给你的 main() 写一份"带依赖注释的初始化时序图"。**

1. **注释**：打开 [main.c:946-1067](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L946-L1067)，为每一个初始化调用补一行注释，格式建议 `// 依赖: xxx | 作用: xxx | 被谁依赖: xxx`。例如 `config_recall()` 一行应写出：依赖=Flash 里 0x0801f800 的数据与 `flash.c` 的校验函数；作用=恢复 config；被依赖=按键上拉、DAC 初值、`uistat` 拷贝、`lcd_rotation`、`update_iqbal/agc`。
2. **画图**：把 4.1~4.6 的流程整合成一张完整的启动时序图（工具不限，Mermaid、纸笔照片均可），纵轴为时间，用三种颜色区分"main 之前 / main 线程内 / 初始化完成后交棒给谁"，并标注两个关键心跳点：`i2sStartExchange`（DSP 心跳开始）与 `chThdCreateStatic(waThread2...)`（UI 心跳开始）。
3. **思考题**（4.5.4 的延伸）：基于你整理的依赖图回答——如果把 `uistat = config.uistat;` 挪到 `dacStart` 之后、`i2cStart` 之前，机器还能正常开机吗？哪些功能会以什么方式出错？（提示：检查 968 行之后、该新位置之前的所有调用里谁读 `uistat`；再检查 `config.button_polarity` 那段是否受影响。）写出你的推断和验证方法（改一行、编译、上电观察哪几项异常）。
4. **验收标准**：注释不出现"初始化某外设"这类空话，每条都能指出数据流向；思考题的结论与 4.5.4 的依赖链分析法一致。

## 6. 本讲小结

- `main()` 之前，[board.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/NANOSDR_STM32_F303/board.c) 的 `__early_init()` 已经用裸机 I2C 把 SI5351 配到默认 8MHz，并完成系统时钟切换——接收链路先于操作系统就绪。
- `config_recall()` 是启动流程里第一个业务调用：magic + XOR 校验和双关卡（校验和初值取 `len`、整块异或归零的设计），失败即静默回退到 `main.c` 里的出厂默认 `config`；`uistat` 的初值唯一来源是 `config.uistat`。
- 外设点火顺序有明确的依赖逻辑：DAC/ADC/USB/I2C → 编解码器四表初始化 → I2S DMA 启动（DSP 心跳开始）→ `dsp_init` → LCD → 显示子系统 → UI 中断 → 参数回写（iqbal/agc）→ 两个工作线程 → shell 循环。
- 初始化顺序错误编译器抓不住：`dsp_init()` 晚于 `i2sStartExchange()` 是现存的一个侥幸竞争；`disp_init()` 前移会踩空 SPI 与屏幕初始化——分析这类问题要靠依赖链推理，不是行号顺序。
- 机器开机即"半configured"状态：本振停在 8MHz、解调停在 `am_demod`，恢复的 `uistat.freq/modulation` 要等第一次用户交互才落到硬件——读启动代码能直接解释"开机要拨一下旋钮"的现象。
- 常驻执行体四类：main 线程（孵化 shell）、Thread1（100ms 统计/功率）、Thread2（10ms 显示+UI）、I2S 回调（中断上下文 DSP，PC13 LED 是负载灯），并发靠单写单读 + 标志位，几乎无锁。

## 7. 下一步学习建议

- **下一讲 u1-l4《与接收机对话：USB CDC Shell 与 Python 控制工具》**：本讲结尾的 shell 循环和 `commands[]` 表将在那里展开——你会学会新增一条自己的命令，并用 `python/centsdr.py` 远程读取 `uistat` 验证。
- 若你更关心"心跳"本身，可以提前跳到单元二 [u2-l3](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L258-L276) 涉及的 I2S 数据流，但建议按大纲顺序先修完单元一。
- 延伸阅读源码：[flash.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/flash.c) 全文（不到 140 行，含页擦除与半字编程的寄存器细节，u4-l5 会精讲）；[board.h](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/NANOSDR_STM32_F303/board.h) 的 `VAL_GPIO*` 宏（对照板子原理图认引脚）。
- 动手题：把第 5 节综合实践画出的时序图保留好，学完 u5-l1（并发与实时）后再回头标注每个执行体的优先级与共享数据，你会得到一张完整的固件运行时视图。
