# 并发与实时：线程模型、中断上下文与负载测量

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 CentSDR 固件的全部执行流——两个工作线程、一个 shell 线程、两路中断（I2S DMA 回调与编码器 EXT 中断）——并说出每条执行流的周期、职责与共享数据。
2. 解释 `stat.busy_cycles` / `stat.interval_cycles` 的测量原理（进入/离开回调时读周期计数器），并手算负载百分比、推算各采样率档位下 `interval_cycles` 的理论值。
3. 分析 `spdispinfo.update_flag` 这一个 8 位变量如何在没有互斥量、没有信号量的条件下，完成「中断 ↔ 线程」的生产者-消费者同步——包括它为什么**不是**严格无竞态的、以及为什么竞态在这里是无害的。
4. 读懂 chconf.h / halconf.h / mcuconf.h 三份裁剪文件里与本讲相关的关键项，特别是中断优先级数值与「DSP 回调抢占一切」这条调度铁律。

本讲是前四个单元的「并发复盘」：u1-l3 走过的初始化、u2-l3 的 I2S 回调、u4-l3 的脏标志刷新，在这里被放回同一个调度模型里审视。

## 2. 前置知识

### 2.1 线程、优先级与抢占

RTOS 里每个线程是一个独立的执行流，内核按优先级调度：**高优先级线程一旦就绪，立刻抢占（preempt）低优先级线程**。ChibiOS 中 `NORMALPRIO` 是普通优先级基准，`NORMALPRIO + 1` 比 `NORMALPRIO` 高一级。同优先级线程之间默认不互相抢占（除非开启时间片轮转），靠主动睡眠/让出切换。

### 2.2 中断上下文：比所有线程都「大」

中断服务程序（ISR）运行在**中断上下文**：它不属于任何线程，优先级由硬件 NVIC 决定，**高于全部线程**。中断上下文里不能调用任何会睡眠的 API（睡眠意味着「让出 CPU 等别人唤醒」，而中断没有自己的线程身份可挂起）——调用即内核断言失败直接停机。这条约束是理解本讲所有代码分工的钥匙。

### 2.3 Cortex-M 的 NVIC 优先级数值方向

STM32 的中断优先级数字是**反的**：0 最高、15 最低（mcuconf.h 开头注释明示 `15...0 Lowest...Highest`）。看到 `STM32_I2S_SPI2_IRQ_PRIORITY 2` 和 `STM32_USB_USB1_LP_IRQ_PRIORITY 14`，要立刻反应过来：I2S 中断比 USB 中断「硬」得多。

### 2.4 DWT 周期计数器

Cortex-M3/M4 内核自带 DWT（Data Watchpoint and Trace）单元，其中 `CYCCNT` 是一个每位 CPU 时钟加 1 的 32 位自由计数器。72MHz 下约 59.6 秒回绕一次。用它做「起止差」测量时，32 位无符号/有符号减法在模运算下天然容忍回绕，只要被测间隔远小于回绕周期。

### 2.5 生产者-消费者与脏标志

经典生产者-消费者问题：一方产生数据、另一方消费，速率不同步。教科书解法是队列+互斥量+条件变量。资源紧张的固件里常用轻量替代——**脏标志（dirty flag）**：生产者只置一个位，消费者看到位就重画、然后清掉。本讲的 `update_flag` 是它的四位数版。

### 2.6 「读-改-写」非原子性

`flag |= BIT` 与 `flag &= ~BIT` 在 CPU 里是三条指令：读、改、写。如果中断恰好插在读和写之间并修改了同一个变量，中断的修改会被线程随后的写覆盖——这就是**丢失更新**。判断它是否致命，要看丢一次的代价。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [main.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c) | 固件入口、线程创建、shell 命令 | Thread1/Thread2 主体、`i2s_end_callback`、`cmd_stat` |
| [chconf.h](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/chconf.h) | ChibiOS 内核裁剪 | tick 频率、tickless、时间片、调试开关 |
| [halconf.h](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/halconf.h) | HAL 外设子系统开关 | 哪些驱动被编入固件 |
| [mcuconf.h](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/mcuconf.h) | STM32F3 驱动参数 | 各中断的优先级数值、时钟树 |
| [nanosdr.h](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h) | 全局共享头 | `stat_t` 结构（L26-L45） |
| [display.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c) | 屏幕绘制 | `spdispinfo` 定义、`disp_fetch_samples`、`disp_process` |
| [ui.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c) | 按键/编码器 | `ext_callback` 中断、`enc_count` 排空 |
| [dsp.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c) | 解调算法 | `disp_fetch_samples` 的调用点（生产者证据） |
| [python/centsdr.py](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py) | 主机控制工具 | 综合实践用的串口收发套路 |
| NANOSDR_STM32_F303/board.h | 板级定义 | `STM32_HSECLK 8000000`（推算 CPU 主频用） |

> 说明：ChibiOS 以 git 子模块引入，本工作副本中 `ChibiOS/` 目录内容未检出，因此涉及内核实现的细节（如 `port_rt_get_counter_value` 的定义处）无法给出仓库内永久链接，文中相应处标注「待确认」。

## 4. 核心概念与源码讲解

### 4.1 执行流全景：两线程、一 shell 与两路中断

#### 4.1.1 概念说明

前面四单元我们分别看过初始化、I2S、显示、UI，但从未把它们**同时**放在桌上。RTOS 视角的第一件事就是数清楚：这块 72MHz 的芯片上，到底有几条并行执行流？它们谁抢谁？共享哪些全局数据？

答案是五条：Thread1、Thread2、shell 线程、I2S DMA 传输完成中断、编码器 EXT 中断（外加 ChibiOS 自己的系统 tick 与 idle 线程这两个「体制内」角色）。

#### 4.1.2 核心流程

固件稳定运行后的执行流全景：

```
┌─ 中断层（高于一切线程，按 NVIC 优先级再分高下）──────────────┐
│                                                              │
│  I2S DMA 半满/全满中断 [优先级 2]                             │
│    └─ i2s_end_callback() → signal_process() 解调             │
│         每 240/fs 秒一次：48k→5ms，192k→1.25ms               │
│                                                              │
│  EXTI 编码器双边沿中断 [优先级 6]                              │
│    └─ ext_callback() 累加 enc_count                          │
│                                                              │
│  系统tick [8]  I2C [10]  SPI/LCD [10]  串口 [12]  USB [13/14] │
└──────────────────────────────────────────────────────────────┘
┌─ 线程层（NORMALPRIO 起步）───────────────────────────────────┐
│  Thread1 "blink"  [NORMALPRIO]   100ms 周期                   │
│    统计 rx_buffer、算功率、置 FLAG_POWER；每秒快照 fps/溢出     │
│  Thread2 "button" [NORMALPRIO]   10ms 周期                    │
│    disp_process() 消费脏标志 + ui_process() 轮询按键/编码器    │
│  shell 线程       [NORMALPRIO+1] 按需（USB CDC 有连接时）       │
│    执行 27 条命令中的任意一条                                   │
│  main 线程       孵化 shell 后每 1s 检查 USB 状态              │
└──────────────────────────────────────────────────────────────┘
```

共享数据清单（**全固件没有一个互斥量**，全靠「单写者 + 标志位 + 原子宽度」）：

| 共享数据 | 写者 | 读者 | 同步手段 |
|---|---|---|---|
| `stat.busy_cycles/interval_cycles` | I2S 中断 | shell（`cmd_stat`） | int32 单写单读，STR 原子 |
| `stat.fps_count/overflow_count` | Thread2 | Thread1（快照） | 同上 |
| `rx_buffer` | DMA | 中断回调、Thread1、shell | 无（读到跨界数据无害） |
| `uistat` | shell、Thread2(UI) | 中断回调（`spdispmode`）、绘制 | 无（多为字节/字段的宽松一致） |
| `signal_process` 指针 | shell（`set_modulation`） | 中断回调 | 32 位指针写原子 |
| `enc_count` | EXT 中断 | Thread2（`fetch_encoder_tick`） | 读后清零，可能丢 1 tick |
| `spdispinfo.update_flag` | 中断回调 + 三个线程 | Thread2（清位） | 4.4 节专讲 |

#### 4.1.3 源码精读

**线程创建与优先级**——[main.c:1039](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L1039) 与 [main.c:1053](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L1053) 以 `NORMALPRIO` 静态创建两个工作线程（栈分别为 128 与 512 字节，见 [main.c:22](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L22)、[main.c:906](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L906)）；[main.c:1058-1066](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L1058-L1066) 里 main 线程循环检测 USB 激活后，从**堆**上分配 2048 字节栈创建 shell 线程，优先级 `NORMALPRIO + 1`：

```c
thread_t *shelltp = chThdCreateFromHeap(NULL, SHELL_WA_SIZE,
                                        "shell", NORMALPRIO + 1,
                                        shellThread, (void *)&shell_cfg1);
chThdWait(shelltp);
```

注意 shell 比两个工作线程**优先级高**：用户敲命令的响应永远比后台统计急。而同级的 Thread1/Thread2 之间因为 `CH_CFG_TIME_QUANTUM 0`（见 4.5）不互相抢占，靠各自的 `chThdSleepMilliseconds` 让出。

**Thread1「统计/功率」**——[main.c:22-47](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L22-L47)：每 100ms 扫一遍 `rx_buffer` 算 RMS/极值（`calc_stat`）、换算功率（`measure_power_dbm`）、置 `FLAG_POWER` 请求刷功率计；每第 10 次循环（约 1 秒）把 `fps_count`、`overflow_count` 快照到 `stat.fps`/`stat.overflow`，顺带读一次温度/电池/基准电压并置 `FLAG_UI` 整屏刷新。

```c
while (1) {
  chThdSleepMilliseconds(100);
  calc_stat();
  measure_power_dbm();
  disp_update_power();          // update_flag |= FLAG_POWER
  if (++count == 10) {
    stat.fps = stat.fps_count;  // 每秒快照
    ...
  }
}
```

**Thread2「显示+UI」**——[main.c:906-924](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L906-L924)：10ms 一拍，先 `disp_process()` 消费脏标志画屏，再 `ui_process()` 处理按键与编码器，然后 `fps_count++`（这就是 fps 的分子来源），最后读一次编解码器的粘滞标志寄存器累加 ADC 溢出计数：

```c
while (1)
{
  disp_process();
  ui_process();
  chThdSleepMilliseconds(10);
  stat.fps_count++;
  {
    int flag = tlv320aic3204_get_sticky_flag_register();
    if (flag & AIC3204_STICKY_ADC_OVERFLOW)
      stat.overflow_count++;
  }
}
```

注意 `tlv320aic3204_get_sticky_flag_register()` 走 I2C——**只有线程上下文才敢这么干**（I2C 驱动内部可能等待/上互斥），这行若搬进 I2S 回调就是死机。

**编码器中断**——[ui.c:148-169](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L148-L169) 的 `ext_callback` 在 EXTI 双边沿中断里查状态转移表累计 `enc_count`；Thread2 侧由 [ui.c:140-146](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L140-L146) 的 `fetch_encoder_tick()` 用「读走 + 清零」排空。EXT 驱动的启动在 [ui.c:222-228](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L222-L228)（`ui_init` → `extStart`），通道配置见 [ui.c:171-180](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L171-L180)。

**I2S 回调的注册**——[main.c:278-286](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L278-L286) 的 `I2SConfig` 把 `i2s_end_callback` 挂为 DMA 传输完成回调，`rx_buffer`/`tx_buffer` 各 `AUDIO_BUFFER_LEN * 2` 个半字（[nanosdr.h:93](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L93) 定义 `AUDIO_BUFFER_LEN` 为 480）。

#### 4.1.4 代码实践

1. **实践目标**：不看讲义，独立列出五条执行流并标注周期与优先级。
2. **操作步骤**：
   - 打开 [main.c:946-1067](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L946-L1067)（`main` 函数），把每个 `chThdCreate*` 调用记一行：名字、栈大小、优先级、入口函数。
   - 打开 [ui.c:171-180](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L171-L180) 与 [main.c:278-286](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L278-L286)，补上两路中断的触发源与回调名。
3. **需要观察的现象**：纸上表应出现 3 个线程（含 shell）+ 2 路应用中断；shell 优先级数值最大（同级里最高）但栈也最大（2048B）。
4. **预期结果**：能回答「如果 Thread2 的 `disp_process()` 画屏耗时超过 10ms 会发生什么」——下一拍被推迟、fps 下降，但音频不中断（画屏在线程层，永远压不过中断层的 DSP）。

#### 4.1.5 小练习与答案

**练习 1**：shell 线程跑 `data` 命令转储 `rx_buffer` 时（[main.c:315-349](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L315-L349)），DSP 中断仍在改写同一块缓冲。为什么作者不加锁？

**答案**：加锁意味着中断回调要等线程释放锁——中断不能睡眠等待，只能自旋或失败；而 `data` 的用途是人工观察波形，读到「半块新半块旧」的混合数据在调试场景可接受。这是典型的「按用途放宽一致性」的固件取舍。

**练习 2**：`set_modulation`（[main.c:179-194](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L179-L194)）在 shell 线程里换 `signal_process` 函数指针，而中断每 1.25~5ms 就要调用它。这个热切换为什么不会出事？

**答案**：两点保障。其一，32 位函数指针在 Cortex-M 上是单条 STR 存储，中断要么看到旧函数、要么看到新函数，不存在「半个指针」。其二，换指针之前 `set_fs`（[main.c:205-226](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L205-L226)）已按需停掉 I2S 交换并睡 40ms——采样率变化时回调根本不在运行，新旧算法切换发生在「无人调用」的窗口里。

### 4.2 硬实时心脏：I2S 回调与周期计数负载测量

#### 4.2.1 概念说明

「硬实时」的定义不是「快」，而是**有截止时间（deadline）**：音频流每 \( 240/f_s \) 秒送来 240 帧，回调必须在这一窗口内算完，否则下一块 DMA 数据直接覆盖未处理完的缓冲——没有任何重试机会。所以 DSP 被放在优先级最高的应用中断里，让一切线程（包括 shell 和显示）都给它让路。

但「放得高」不等于「跑得完」。工程上必须能**量化**：每个回调花掉多少 CPU 周期？两次回调之间预算是多少周期？前者除以后者就是负载。CentSDR 的答案优雅得只有六行：进入回调读一次周期计数器，调完解调再读一次。

#### 4.2.2 核心流程

测量原理（`cnt_s` 为进入时刻计数、`cnt_e` 为解调结束时刻计数、`last` 为上次进入时刻）：

\[
\text{busy\_cycles} = cnt_e - cnt_s \qquad \text{interval\_cycles} = cnt_s - \text{last}
\]

\[
\text{load} = \frac{\text{busy\_cycles}}{\text{interval\_cycles}} \times 100\%
\]

回调周期由采样率决定：

\[
T_{cb} = \frac{240}{f_s} =
\begin{cases}
5\,\text{ms} & f_s = 48\,\text{kHz} \\
2.5\,\text{ms} & f_s = 96\,\text{kHz} \\
1.25\,\text{ms} & f_s = 192\,\text{kHz}
\end{cases}
\]

CPU 主频 = HSE 8MHz（[board.h:34](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/NANOSDR_STM32_F303/board.h#L34) `STM32_HSECLK 8000000`）经 PLL 9 倍频（[mcuconf.h:49-51](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/mcuconf.h#L49-L51) `PREDIV_VALUE 1`、`PLLMUL_VALUE 9`、`STM32_SW_PLL`）= **72MHz**。于是 `interval_cycles` 的理论值：

| 采样率 | 回调周期 | interval_cycles 理论值 |
|---|---|---|
| 48kHz | 5ms | 360,000 |
| 96kHz | 2.5ms | 180,000 |
| 192kHz | 1.25ms | 90,000 |

（待本地验证：以 `stat` 命令实测值对照。）

#### 4.2.3 源码精读

**测量本体**——[main.c:258-276](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L258-L276)，全固件最精炼的六行仪表：

```c
void i2s_end_callback(I2SDriver *i2sp, size_t offset, size_t n)
{
  int32_t cnt_s = port_rt_get_counter_value();
  int32_t cnt_e;
  int16_t *p = &rx_buffer[offset];
  int16_t *q = &tx_buffer[offset];
  palSetPad(GPIOC, GPIOC_LED);

  (*signal_process)(p, q, n);        // 解调 DSP，唯一的重活

  cnt_e = port_rt_get_counter_value();
  stat.interval_cycles = cnt_s - stat.last_counter_value;
  stat.busy_cycles = cnt_e - cnt_s;
  stat.last_counter_value = cnt_s;

  stat.callback_count++;
  palClearPad(GPIOC, GPIOC_LED);
}
```

逐行解读：

- `port_rt_get_counter_value()` 是 ChibiOS 内核提供的实时计数器读取接口（在 ARMv7-M 移植中即读 DWT 的 `CYCCNT`；因 ChibiOS 子模块在本副本中未检出，其实现文件路径待确认）。
- `interval_cycles` 是「本次进入 − 上次进入」，即**含上轮忙碌在内的完整周期**，所以 load 恒有意义；一旦 `busy > interval`，打印值会超过 100%，意味着已经丢样。
- 三个 `int32_t` 差值都能正确承受 32 位回绕（模运算性质），只要间隔远小于 59.6s。
- `palSetPad`/`palClearPad` 把 LED 引脚变成**示波器探头点**：引脚高电平宽度 = 本轮解调耗时，占空比 = 负载。
- `stat.callback_count` 是运行秒数的另一种表达（192kHz 下每秒 800 次）。

**显示端**——[main.c:433](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L433) 在 `cmd_stat` 里做除法：

```c
chprintf(chp, "load: %d%% (%d/%d)\r\n",
         stat.busy_cycles * 100 / stat.interval_cycles,
         stat.busy_cycles, stat.interval_cycles);
```

shell 读的是中断刚刚写的两个 int32——各自原子，但**两个之间不成对**，可能读到「busy 是本次、interval 是上次」的错配；对观测而言误差一次回调周期，无害。

#### 4.2.4 代码实践

1. **实践目标**：验证 `interval_cycles` 与采样率的反比关系，建立「周期预算」的数量级直觉。
2. **操作步骤**（有硬件时）：
   - 用 `mode am`（48kHz）→ `stat`，记录 `load` 行的 interval 值；
   - `mode fms`（192kHz）→ `stat`，再记录；
   - `fs 96` 手动切档 → `stat`，第三次记录。
3. **需要观察的现象**：三个 interval 值应约为 360000 / 180000 / 90000，比值 4:2:1。
4. **预期结果**：若实测显著偏离，先怀疑主频配置（HSE 晶振是否 8MHz）再怀疑测量。无硬件时可在 PC 上推演：写 10 行脚本打印 `240/fs*72e6` 三个值作为对照基准（**示例代码**）：

```python
# 示例代码：推算各采样率下的 interval_cycles
for fs in (48000, 96000, 192000):
    print(fs, int(240 / fs * 72e6))
```

#### 4.2.5 小练习与答案

**练习 1**：为什么把 `measure_adc()`（内部忙等轮询 ADC，[main.c:395-410](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L395-L410)）放在 Thread1 而不是 I2S 回调里？

**答案**：`adc_single_read` 用 `while (adc->CR & ADC_CR_ADSTART)` 自旋等待转换完成。在 Thread1 里这最多浪费几十微秒线程时间；放进硬实时回调则是无界阻塞——ADC 时钟分频（`STM32_ADC12PRES DIV64`，[mcuconf.h:56](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/mcuconf.h#L56)）下转换时间不短，直接吃穿 1.25ms 预算。判据就一条：**轮询可以发生在线程，永远不要发生在有截止时间的中断里**。

**练习 2**：`stat.last_counter_value` 若是 16 位会怎样？

**答案**：72MHz 下 16 位计数器约 0.91ms 回绕一次，比 192kHz 的回调周期还短，差值法失效（间隔会与真实值相差任意个 65536）。32 位是本方案能工作的最低宽度。

### 4.3 系统健康仪表：stat 结构、fps 与 ADC 溢出监测

#### 4.3.1 概念说明

负载回答「DSP 忙不忙」，但一台接收机的健康还有两个维度：**交互流畅吗**（fps）与**前端饱和了吗**（ADC 溢出）。三者全部攒在 `stat_t` 里，由不同上下文分工填写——`stat_t` 因此是本讲「多写者共享」的最佳标本。

ADC 溢出尤其值得解释：TLV320AIC3204 的 ADC 若输入幅度超出满量程，会在内部置一个**粘滞标志位**（不自动清零，读到才清）。固件利用这个特性做累积计数——Thread2 每拍查询一次，查到就 `overflow_count++`。计数持续增长 = 前端增益该调小了。

#### 4.3.2 核心流程

```
I2S 中断 ──每拍──> busy_cycles / interval_cycles / callback_count / last_counter_value
Thread2 ──每拍──> fps_count++ ；查粘滞标志 → overflow_count++
Thread1 ──每100ms──> rms/min/max（扫描 rx_buffer）
        ──每秒──> fps ← fps_count 清零；overflow ← overflow_count 清零
                  temperature/battery/vref（轮询 ADC）
shell ──cmd_stat──> 全部读出打印
```

fps 的语义是 **Thread2 主循环频率**，理论上限 100（10ms 睡眠）。它同时充当「显示+UI 子系统吞吐」的代理指标：`disp_process` 画瀑布越慢，循环越拖长，fps 越低。

#### 4.3.3 源码精读

**`stat_t` 结构**——[nanosdr.h:26-45](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L26-L45)，注意字段注释里藏着各自的写者：

```c
typedef struct {
  int32_t rms[2];            // Thread1 每100ms
  int16_t ave[2];
  int16_t min[2];
  int16_t max[2];

  uint32_t callback_count;   // I2S 中断
  int32_t last_counter_value;
  int32_t interval_cycles;
  int32_t busy_cycles;

  uint16_t fps_count;        // Thread2 累加，Thread1 快照
  uint16_t fps;
  uint16_t overflow_count;   // Thread2 累加，Thread1 快照
  uint16_t overflow;

  uint16_t vref;             // Thread1 每秒（轮询 ADC）
  uint16_t temperature;
  uint16_t battery;
} stat_t;
```

**快照逻辑**——[main.c:36-45](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L36-L45)：`fps = fps_count; fps_count = 0;` 这两步之间存在竞态（Thread2 若恰好在此间隙 +1，该拍被吞），误差 ±1，无碍。

**一个值得注意的隐患**——[main.c:26](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L26) 声明 `int count;` **未初始化**，随后 [main.c:36](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L36) 判 `++count == 10`。按 C 标准，自动变量初值不确定：实际取值取决于它落在栈/寄存器的残留（本固件开启 `CH_DBG_FILL_THREADS`，栈会被 0x55 填充，见 4.5）。若 `count` 初值不是 0 附近的数，「每秒快照」会推迟极久才成立——fps 与 overflow 的读数是否正常刷新，**待本地验证**；无论如何，正确写法是 `int count = 0;`。这是真实代码里「编译能过、逻辑碰运气」的典型样本。

**溢出查询**——[main.c:918-922](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L918-L922)（Thread2 内，见 4.1.3 引文）经 I2C 读粘滞标志寄存器；`cmd_stat` 的输出行在 [main.c:435](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L435)。

#### 4.3.4 代码实践

1. **实践目标**：用 `stat` 输出做一次「体检」，区分三类指标。
2. **操作步骤**（有硬件时）：`gain 90` 把前端增益拉高 → 对着强信号/直接断开天线 → 连发几次 `stat`。
3. **需要观察的现象**：`overflow` 每秒递增（ADC 饱和）；`rms/max` 逼近 32767；`load` 不变（溢出是模拟域事件，DSP 开销与信号无关）。
4. **预期结果**：`gain 40` 以下 overflow 回到 0 并不再增长。无硬件时：**源码阅读型实践**——从 [main.c:423-446](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L423-L446) 的 `cmd_stat` 输出清单里，给每一行标注「写者上下文 / 更新周期」两列，做成表格（答案即 4.3.2 的流程图加 4.3.3 的字段注释）。

#### 4.3.5 小练习与答案

**练习 1**：fps 为什么天然封顶在 100 附近？什么情况下会显著低于 100？

**答案**：Thread2 每循环睡 10ms（[main.c:915](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L915)），加上循环体耗时，每秒最多约 100 拍。当 `disp_process` 需要整屏重画（FLAG_UI 触发 320×240 像素级操作）或瀑布/波形渲染变重时，单拍超 10ms，fps 应声下跌——它是显示子系统负载的代理指标。

**练习 2**：`stat.fps_count++`（Thread2）与 `stat.fps = stat.fps_count`（Thread1）无锁并发，为什么可以接受？

**答案**：两者都是 16 位单写操作，uint16_t 在 32 位总线上读写原子；最坏情况是快照瞬间丢/多计一拍，统计意义下无影响。若这是 64 位计数器或「读-改-写」复合操作，就必须上互斥或关中断。

### 4.4 无锁生产者-消费者：spdispinfo.update_flag

#### 4.4.1 概念说明

u4-l3 讲过 `disp_process()` 的脏标志机制「是什么」，本讲补上并发视角的「为什么能行」：一个 `uint8_t`，四个位，五六个写者分属**中断与线程两种上下文**，没有一把锁，却能稳定工作多年。拆开会发现它并非严格无竞态——而是**竞态有界、后果自愈**。识别这种「工程上安全、形式上违规」的边界，是读实时固件的进阶能力。

#### 4.4.2 核心流程

四个位的身份（[display.c:589-593](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L589-L593)）：

| 位 | 含义 | 置位者（上下文） | 清位者 |
|---|---|---|---|
| FLAG_SPDISP | 频谱/瀑布/波形样本就绪 | `disp_fetch_samples`（**I2S 中断**，经 dsp.c 调用） | Thread2 |
| FLAG_POWER | 功率计数据更新 | Thread1 `disp_update_power()` | Thread2 |
| FLAG_UI | 界面元素需重画 | shell 命令 / Thread2(ui) `disp_update()` | Thread2 |
| FLAG_AUX_INFO | 辅助信息行需清除 | `disp_clear_aux()` | Thread2 |

单向数据流（对 FLAG_SPDISP 而言）：

```
I2S 中断                          Thread2
────────                          ───────
disp_fetch_samples()
  ├─ 消费者还忙(FLAG_SPDISP=1)? → 丢弃本帧，立刻返回
  ├─ 空闲 → 把加窗样本攒入 SPDISP_BUFFER
  └─ 攒满 2048 点 → update_flag |= FLAG_SPDISP
                                        disp_process()
                                          ├─ 读到 FLAG_SPDISP=1
                                          ├─ 波形/频谱/瀑布 绘制（读缓冲）
                                          └─ update_flag &= ~FLAG_SPDISP
```

要点：**生产者从不等待**。消费者没消化完，新帧直接丢——用「丢显示」换「不拖 DSP」，方向永远正确（音频不能丢，画面可以）。

#### 4.4.3 源码精读

**结构定义**——[display.c:582-596](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L582-L596)：

```c
// when event sent with SEV from M4 core, filled following data
typedef struct {
	q31_t *buffer;
	uint32_t buffer_rest;
	uint8_t update_flag;
} spectrumdisplay_t;

// update_flag
#define FLAG_SPDISP 	(1<<0)
#define FLAG_POWER 		(1<<1)
#define FLAG_UI 		(1<<2)
#define FLAG_AUX_INFO	(1<<3)

spectrumdisplay_t spdispinfo;
```

注释里的 "SEV from M4 core" 是这套代码从**双核（M0/M4）祖先项目**移植的化石——[display.c:733](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L733) 还留着 `// currently proccessing in M0APP` 的注释。在本固件里，生产者是 I2S 中断、消费者是 Thread2，机制不变。

**生产者**——[display.c:724-773](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L724-L773) 的 `disp_fetch_samples`，被 dsp.c 的解调函数在四个钩子处调用（例如 [dsp.c:355](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L355)、[dsp.c:365](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L365)、[dsp.c:371](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L371)、[dsp.c:384](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L384) 的 `demod_weaver` 内）。关键三段：

```c
if (mode != uistat.spdispmode)      // 用户没选我，零成本退出
    return;

if (spdisp_fetch_rest == 0) {
    if (spdispinfo.update_flag & FLAG_SPDISP) {
        return;                      // 消费者还在画 → 丢帧保实时
    }
    spdisp_fetch_current = SPDISP_BUFFER;   // 开新帧
    spdisp_fetch_rest = SPDISP_BUFFER_LENGTH;
    ...
}
...
if (spdisp_fetch_rest == 0) {        // 攒满 2048 点
    spdispinfo.buffer = SPDISP_BUFFER;
    spdispinfo.update_flag |= FLAG_SPDISP;  // 通知消费者
}
```

**消费者**——[display.c:1411-1448](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1411-L1448) 的 `disp_process()`（Thread2 调用）逐位检查、绘制、清位；三个置位接口在 [display.c:1450-1466](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1450-L1466)（`disp_update` / `disp_update_power` / `disp_clear_aux`）。

**竞态剖析（本讲核心论证）**。`|=` 与 `&= ~` 都是「读-改-写」，在 Cortex-M 上是三条指令，**不是原子操作**。两类真实存在的竞态窗口：

1. **Thread2 清位被中断插入**：`disp_process` 执行 `update_flag &= ~FLAG_SPDISP`，若 I2S 中断恰在「读」（得 0）与「写」（清位）之间把 FLAG_SPDISP 置 1，线程随后的写会把刚置的位抹掉——**丢一帧显示**。但下一次回调（最多 5ms / 1.25ms 后）会重新置位，画面最多迟一拍，肉眼不可见。
2. **线程互抢**：shell（NORMALPRIO+1）可在 Thread1/Thread2 的 `|=` 中途抢占并自己也 `|=`，同理可能丢失对方的一次置位——少刷一次功率行/界面行，100ms 后自愈。

所以准确的说法是：**无锁 ≠ 无竞态，而是竞态的后果被设计成「丢一次可再生的请求」**。另外两个诚实的观察：

- `spdispinfo` 与 `stat` 都**没有 `volatile` 修饰**。严格按 C 标准，跨中断共享的可写对象应当 `volatile`；这里能工作的实际原因是所有访问都发生在跨函数调用的边界上（编译器无法证明无副作用，只得重新加载）。能跑，但不严谨——若将来内联加激进优化，可能出微妙 bug。
- 唯一真正不可丢的数据（音频样本本身）根本不经过标志位，由 DMA 硬件直写——**关键数据走硬件，提示性数据走标志位**，是这个设计的分层智慧。

#### 4.4.4 代码实践

1. **实践目标**：把「丢帧自愈」变成肉眼可见的实验。
2. **操作步骤**（有硬件时）：`mode fms`（192kHz，显示帧生产最快）→ `spdisp` 调成瀑布（`wfdisp` 若适用）→ `stat` 连续观察 `fps` 与 `load`；再用 `mode cw`（48kHz，帧生产慢 4 倍）对照。
3. **需要观察的现象**：192kHz 下若 Thread2 绘制跟不上，`disp_fetch_samples` 频繁走「丢帧」分支——fps 下降但音频无爆音、load 不劣化。
4. **预期结果**：fps 在两档间有明显差异而 load 都稳定 <100%，即验证「显示让路、DSP 不受影响」。无硬件时的**源码阅读型实践**：在 [display.c:731-735](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L731-L735) 的丢帧分支处** mentally** 插入一行计数 `stat_drop++`（不必真改源码，写在笔记里），说明：该计数器属中断上下文写、shell 读，同样只需 uint32_t 单写单读，无需加锁。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `spdispinfo.update_flag` 的四个位拆成四个 `bool` 变量，机制还成立吗？

**答案**：功能上大体成立，但失去「一次读、判多位」的批量检查能力，且将来若想做「一次原子清多位」或位域扩展（uint8_t 还有 4 个空位）都不方便。位集合是嵌入式最便宜的「多播信箱」。

**练习 2**：生产者为什么在 `spdisp_fetch_rest == 0`（开新帧）时才检查 FLAG_SPDISP，而不是每次调用都检查？

**答案**：一帧跨越多次回调（2048 点 / 每回调最多 240 点，约需 5~9 拍攒满）。攒帧中途消费者本来就不该碰缓冲，无需检查；只有「准备覆盖缓冲开头」那一刻才必须确认消费者已放手。把检查收敛到边界点，中间各拍只做纯拷贝。

**练习 3**：设想把 `disp_process()` 从 Thread2 搬进一个 NEW 线程并提为 `NORMALPRIO+2`，会改善什么、破坏什么？

**答案**：改善：绘制不再与 `ui_process` 互相排队，fps 或有提升。破坏：`ui_process` 与 `disp_process` 目前隐式依赖「同线程串行」——UI 改 `uistat`（如 `spdispmode`、`mag_shift`）与绘制读 `uistat` 之间将出现真数据竞争，需要另行加锁或改用消息传递。教训：调优先级前先画共享数据表（4.1.2 那张）。

### 4.5 内核与中断优先级裁剪：chconf.h / halconf.h / mcuconf.h

#### 4.5.1 概念说明

ChibiOS 的哲学是「按需编译」：内核 IPC 原语、HAL 外设驱动、每个中断的优先级，全部在三个头文件里显式声明。这三份文件是理解系统**调度铁律**的最终依据——尤其 mcuconf.h 里那张优先级表，直接决定了 4.1-4.4 的一切分析是否成立。

#### 4.5.2 核心流程

本固件实际生效的中断优先级排序（数字越小越硬）：

```
 2  I2S(SPI2) DMA/IRQ   ← DSP 回调：硬实时之王
 5  ADC
 6  EXTI(编码器)
 8  系统tick(ST, TIM2)
10  I2C / SPI(LCD)
12  串口 USART1
13/14 USB 高/低优先级    ← shell 的物理通道：最软
```

推论链：**DSP 回调可以打断一切**（包括内核 tick 与编码器中断）；反过来，USB 中断（shell 的字节的来源）永远无法延迟音频——这就是「拧命令再频繁也不会卡声音」的制度保证。而编码器中断（6）排在 tick（8）之前：拧旋钮的响应优先于线程调度节拍。

线程侧：shell(NORMALPRIO+1) > Thread1 = Thread2(NORMALPRIO)，同级无时间片（`CH_CFG_TIME_QUANTUM 0`），靠睡眠让出。

#### 4.5.3 源码精读

**chconf.h（内核裁剪）**——[chconf.h:44-84](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/chconf.h#L44-L84)：

- [L51](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/chconf.h#L51) `CH_CFG_ST_FREQUENCY 10000`：系统 tick 10kHz，即 100µs 分辨率——`chThdSleepMilliseconds(10)` 的精度来源。
- [L61](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/chconf.h#L61) `CH_CFG_ST_TIMEDELTA 2`：**tickless 模式**。内核不是每 100µs 醒一次空转，而是在「下一个最近到期点」才编程定时器——省电且减少无谓唤醒。
- [L84](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/chconf.h#L84) `CH_CFG_TIME_QUANTUM 0`：同级线程不轮转（tickless 下必须为 0），同级协作、异级抢占——与 4.1 的行为分析互为印证。
- [L142](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/chconf.h#L142) `CH_CFG_USE_TM FALSE`：内核自带的耗时测量 API 被裁掉——固件直接用 DWT（4.2），不重复付费。
- [L150](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/chconf.h#L150) `CH_CFG_USE_REGISTRY TRUE`：`chRegSetThreadName("blink")` 等注册 API 可用，调试器能看到线程名。
- [L150-L313](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/chconf.h#L150-L313) 一串 IPC 原语（信号量/互斥/邮箱/堆……）保持 TRUE——虽然应用代码一个都没用，但 HAL 驱动内部要用（如 I2C 互斥）。

**chconf.h 调试开关**——[chconf.h:338-405](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/chconf.h#L338-L405)：`CH_DBG_SYSTEM_STATE_CHECK`、`CH_DBG_ENABLE_CHECKS`、`CH_DBG_ENABLE_ASSERTS`、`CH_DBG_ENABLE_STACK_CHECK` 全开（[L338](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/chconf.h#L338)、[L347](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/chconf.h#L347)、[L357](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/chconf.h#L357)、[L384](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/chconf.h#L384)），[L394](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/chconf.h#L394) `CH_DBG_FILL_THREADS TRUE` 尤其实用：线程创建时栈被 0x55 填充，调试器里看「还剩多少 0x55」即知栈高水位——Thread1 只有 128 字节栈，全靠这个确认没爆。

**halconf.h（外设开关）**——生效为 TRUE 的子系统（[L37](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/halconf.h#L37) PAL、[L42](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/halconf.h#L42) ADC、[L57](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/halconf.h#L57) DAC、[L62](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/halconf.h#L62) EXT、[L77](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/halconf.h#L77) I2C、[L86](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/halconf.h#L86) I2S、[L135](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/halconf.h#L135) SERIAL、[L141](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/halconf.h#L141) SERIAL_USB、[L148](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/halconf.h#L148) SPI、[L161](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/halconf.h#L161) USB）恰好对应 README 框图里的每条硬件链路；CAN/GPT/PWM/RTC/SDC/UART 全 FALSE——没有的硬件不留代码。

**mcuconf.h（中断优先级表）**——上面排序表的出处：I2S 在 [mcuconf.h:167-175](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/mcuconf.h#L167-L175)（`STM32_I2S_SPI2_IRQ_PRIORITY 2`，同时可见 SPI2 配为**从机+全双工**模式，与 u2-l3「编解码器是 I2S 主机」互证）；EXTI 在 [mcuconf.h:110-124](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/mcuconf.h#L110-L124)（全 6）；系统 tick 在 [mcuconf.h:237-238](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/mcuconf.h#L237-L238)（`STM32_ST_IRQ_PRIORITY 8`、用 TIM2）；USB 在 [mcuconf.h:259-260](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/mcuconf.h#L259-L260)（13/14）。

#### 4.5.4 代码实践

1. **实践目标**：验证「优先级 2 的 I2S 中断抢占优先级 6 的编码器中断」这一断言的可推演性。
2. **操作步骤**：**源码阅读型实践**（无需硬件）——从 [mcuconf.h](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/mcuconf.h) 抄出全部 `*_IRQ_PRIORITY` 行，按数值升序排成表；再对照 4.1.2 共享数据表，对每行问一句「如果这个中断被推迟，谁受害最重」。
3. **需要观察的现象**：整理出的表应与 4.5.2 的排序一致；你会发现固件没有使用任何优先级分组配置代码（ChibiOS HAL 接管），四 位抢占优先级全部裸用。
4. **预期结果**：能口头回答「为什么 USB 故意放最软」——shell 是人类节奏（毫秒级容忍），音频是机器节奏（1.25ms 硬期限）。

#### 4.5.5 小练习与答案

**练习 1**：`CH_CFG_ST_FREQUENCY` 从 10000 提到 100000，`chThdSleepMilliseconds(10)` 会更准吗？代价是什么？

**答案**：分辨率从 100µs 提到 10µs，短睡眠更准；但 tickless 模式下收益有限（本来就按需唤醒），而内核时间相关结构的运算开销与 TIM2 中断频率上升。本固件 10ms 周期的线程根本用不到 10µs 分辨率——「够用的精度」是裁剪的品味。

**练习 2**：调试开关（CHECKS/ASSERTS/STACK_CHECK）全开会拖慢系统，为什么这个项目敢开？

**答案**：这是手作品质固件而非量产固件：断言能在开发期立刻暴露「中断里调用睡眠 API」这类致命错误（直接停机带 panic 信息，而不是偶发死机难复现）。发布版若要榨性能，把四个 `CH_DBG_*` 关掉即可省代码与周期。

## 5. 综合实践

**任务**：用主机脚本量化两种解调模式的实时开销——FM 立体声（192kHz）对比 CW（48kHz），把本讲的负载模型跑出真数据。

**背景**：`mode fms` 与 `mode cw` 除算法不同外，`mod_table`（[main.c:170-177](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L170-L177)）会自动带上 192 与 48 的采样率档位，`set_modulation` → `set_fs` 一路换过去，无需手动 `fs`。算法差异（u3-l2/u3-l5 已析）：立体声链 = 频响校正 FIR + 反正切鉴频 + 19kHz 导频 PLL + 和差矩阵，且**每秒样本数是 CW 模式的 4 倍**；CW 链 = 两次 NCO 混频 + 6 阶椭圆低通 ×2 路。预期 fms 的 `busy_cycles` 显著更大，而 `interval_cycles` 反而只有 CW 档的 1/4——两头夹击，load 差距是「乘法级」的。

**步骤**（需硬件 + Python 3 + pyserial；`python3 -m pip install pyserial matplotlib`）：

1. 保存以下脚本为 `loadlog.py`（**示例代码**，未在硬件上运行过；`centsdr.py` 本身是 Python 2 代码，其 `fetch_data` 在 Python 3 下有 bytes/str 兼容问题，故这里直接用 pyserial 复刻同一套路——写命令、读到 `ch>` 提示符为止）：

```python
#!/usr/bin/env python3
# 示例代码：周期发送 stat，记录 load/busy/interval/fps，输出 CSV
import re, time, serial, sys

DEV = sys.argv[1] if len(sys.argv) > 1 else '/dev/ttyACM0'
N, INTERVAL = 50, 0.2

def read_stat(ser):
    ser.write(b'stat\r')
    out = b''
    while not out.endswith(b'ch>'):
        c = ser.read(1)
        if not c:
            raise TimeoutError('no prompt, check cable')
        out += c
    t = out.decode('ascii', 'replace')
    load = re.search(r'load: (\d+)% \((\d+)/(\d+)\)', t)
    fps  = re.search(r'fps: (\d+)', t)
    return tuple(int(g) for g in load.groups()) + (int(fps.group(1)),)

with serial.Serial(DEV, timeout=2) as ser:
    time.sleep(1); ser.reset_input_buffer()
    rows = []
    for mode in ('fms', 'cw'):
        ser.write(('mode %s\r' % mode).encode())
        time.sleep(2)                      # 等 set_fs 的 40ms 握手 + 稳定
        ser.reset_input_buffer()
        for i in range(N):
            pct, busy, interval, fps = read_stat(ser)
            rows.append((mode, i, pct, busy, interval, fps))
            time.sleep(INTERVAL)
    for r in rows:
        print('%s,%d,%d,%d,%d,%d' % r)
```

2. 运行 `python3 loadlog.py /dev/ttyACM0 > loadlog.csv`。
3. 绘制时间序列（**示例代码**）：`python3 -c "import matplotlib.pyplot as p,csv; r=list(csv.reader(open('loadlog.csv')))[1:]; [p.plot([int(x[2]) for x in r if x[0]==m],label=m+' load%') for m in ('fms','cw')]; p.legend(); p.show()"`（或用你熟悉的任何工具画第 3、6 列）。

**需要观察的现象与预期结果**（待本地验证）：

- `interval` 列：fms ≈ 90000，cw ≈ 360000（比值 1:4，与 4.2.2 推算一致；每组**前几行**可能因 `set_fs` 的 40ms 睡眠混入大间隔离点，剔除即可）。
- `load` 列：fms 明显高于 cw；fps 则相反方向变化（fms 下 Thread2 每秒渲染的数据帧多 4 倍）。
- 若 fms 的 load 逼近或超过 100%：音频应出现破裂，同时 `callback_count` 增速不再匹配 800 次/秒——这就是 4.2 说的「无重试的实时失败」现场。

**结果解释框架**（写进你的实验报告）：

\[
\text{load} = \frac{\text{busy}}{\text{interval}} = \frac{C_{\text{alg}} \cdot f_s / 48000}{240 \cdot 72\,\text{MHz} / f_s} \propto C_{\text{alg}} \cdot f_s^2
\]

即负载近似与「每样本成本 × 采样率²」成正比：fms 相对 cw 是 \( (C_{fms}/C_{cw}) \times 16 \)——一次 4 倍来自采样率本身，再一次 4 倍来自预算窗口缩短。这正是「采样率翻倍、负载翻四倍」的嵌入式 DSP 经验法则。

## 6. 本讲小结

- **五条执行流**：Thread1（统计/功率，100ms）、Thread2（显示+UI，10ms）、shell（NORMALPRIO+1，按需）、I2S DMA 中断（DSP，每 240/fs 秒）与编码器 EXT 中断；共享 `stat`/`uistat`/`rx_buffer`/`signal_process`/`enc_count`/`update_flag`，全固件零互斥量。
- **硬实时的位置**：DSP 跑在优先级 2 的 I2S 中断里，压过编码器（6）、内核 tick（8）与 USB（13/14）——音频期限 1.25~5ms 不可协商，shell 与显示永远让路。
- **负载测量**：`port_rt_get_counter_value()` 前后两次读数相减得 `busy_cycles`，进-进相减得 `interval_cycles`，`load% = busy×100/interval`；72MHz 下三档采样率的 interval 理论值为 360000/180000/90000；负载近似 ∝ 每样本成本 × f_s²。
- **无锁 ≠ 无竞态**：`update_flag` 的 `|=`/`&= ~` 存在丢失更新窗口，但被设计成「丢一次可再生的显示请求」+ 单向数据流（生产者忙则丢帧、绝不阻塞），且未加 `volatile` 依赖调用边界的重载——工程安全、形式违规，二者都要看见。
- **裁剪即架构**：tickless（`ST_TIMEDELTA 2`）+ 无时间片（`TIME_QUANTUM 0`）+ 裁掉内核测时 API（`USE_TM FALSE`，因为直接用 DWT）+ 调试断言全开——三份 conf 文件是调度铁律的成文法。
- **遗留隐患**：Thread1 的 `int count;`（[main.c:26](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L26)）未初始化，fps/overflow 的每秒快照逻辑依赖未定义初值——正确写法应为 `int count = 0;`。

## 7. 下一步学习建议

- **u5-l2（SIMD 与定点优化）**：本讲的 load 是「开销」，下一讲拆开 `busy_cycles` 的内部——`__SMLAD`/`__SMLSDX` 等指令如何把每样本成本压到单周期级，直接回答「怎么把 load 降下来」。
- **u5-l3（链接脚本与内存布局）**：Thread1 的 128 字节栈、CCM RAM 的利用、`stat` 这类全局落在哪个段——内存版图是并发之外另一条架构主线。
- **延伸阅读（源码）**：对照 [flash.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/flash.c) 中 `chSysLock` 的用法（u4-l5 已讲），体会「关中断保护」与本章「标志位免保护」两种手段的适用边界；若本地检出 ChibiOS 子模块，可顺藤摸瓜找 `port_rt_get_counter_value` 的定义（本讲标注待确认之处），验证 DWT 读数路径。
