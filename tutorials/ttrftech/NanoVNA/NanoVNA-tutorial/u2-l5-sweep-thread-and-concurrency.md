# 扫频线程：Thread1 主循环与并发协作

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 sweep 线程（Thread1）一次完整迭代的状态流转图，说清"谁在什么时候调用了什么"。
2. 解释 `sweep_mode` 标志（`SWEEP_ENABLE`/`SWEEP_ONCE`）如何控制扫频的启停，以及 `sweep(bool break_on_operation)` 的 `break_on_operation` 参数为什么能让 UI 操作"打断"一次 101 点的扫频。
3. 理解 `shell_function` 函数指针如何在没有互斥量的前提下，把 shell 命令安全地"搬运"到 sweep 线程执行。
4. 理解 `operation_requested`（输入事件）→ `ui_process`（输入处理）→ `plot_into_index` + `redraw_request` → `draw_all`（绘制）这条请求-响应式流水线。
5. 掌握用 `chVTGetSystemTimeX()` 给固件阶段计时的方法，为后续的性能分析打基础。

本讲是第二单元的收官：u2-l1 讲了 sweep() 单个频点"做什么"，u2-l3/u2-l4 讲了数据"怎么来"；本讲回答的是"**什么时候做、由谁做、和谁并发**"——也就是这台仪器作为一台实时系统的调度骨架。

## 2. 前置知识

### 2.1 线程与上下文：三个"并行"的执行者

NanoVNA 固件里同时活跃着三类执行上下文，理解它们的区别是本讲的钥匙：

| 上下文 | 是谁 | 特点 |
|---|---|---|
| **main 线程** | `main()` 里最后的 while 循环，跑 USB shell | 优先级 `NORMALPRIO`（较高） |
| **sweep 线程** | `Thread1`，优先级 `NORMALPRIO-1`（较低） | 负责测量、UI 处理、绘图 |
| **中断上下文** | I2S 回调 `i2s_end_callback`、EXTI 按键回调 `extcb1`、ADC 看门狗等 | 随时可能打断上面两个线程 |

"中断"不是线程：它是硬件事件（如"I2S 收满一块数据"）触发的短函数，执行完立即返回被打断的线程。所以 `i2s_end_callback` 每毫秒"插队"一次，把 DSP 累加往前推一小步。

### 2.2 标志位通信：穷人版消息队列

两个线程要协作，最朴素的办法不是加锁，而是共享几个 `volatile` 变量：

- **volatile** 告诉编译器"这个变量会被你看不见的人修改"，禁止把它缓存进寄存器，每次都重新从内存读。
- 在 Cortex-M0 上，**对齐的 8/16/32 位读写本身是原子的**（一条指令完成），所以单个标志位的"写-读"不需要锁。
- 副作用是"读-改-写"（如 `flags |= BIT`）**不是**原子的——两步之间可能被另一个线程插队。本讲会看到固件如何用"**每个标志只有一个写者**"的纪律绕开这个坑（以及个别没绕开的地方）。

### 2.3 __WFI：让 CPU 睡到下一个中断

`__WFI()`（Wait For Interrupt）是 Cortex-M 的指令：CPU 立即进入低功耗睡眠，**任何中断**（I2S、定时器、USB……）都能把它唤醒。它常被用来实现"睡-醒-查-再睡"的轮询循环，比空转 `while(1);` 省电得多。本讲会在两处遇到它。

### 2.4 ChibiOS 的系统节拍

[ChibiOS 的系统定时器](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chconf.h#L51)被配置为 `CH_CFG_ST_FREQUENCY 10000`，即每秒 10000 个 tick——**1 tick = 100 µs**。`chVTGetSystemTimeX()` 返回当前的 tick 数（后缀 `X` 表示"不走内核锁、直接读计数器"的快速版本，可以在任意上下文安全调用）。这就是本讲代码实践里计时的标尺。

### 2.5 与前几讲的衔接

- u2-l1：sweep() 逐频点"设频率 → 选通道 → 先丢再测 → 算 Γ"的四步协议；
- u2-l3：I2S DMA 每毫秒回调一次 `i2s_end_callback`，`wait_count`/`accumerate_count` 两个 volatile 计数器是 ISR 与线程之间的握手；
- u2-l4：`dsp_process` 的正交累加与 `calculate_gamma` 的复数除法。

本讲不再解释这些函数内部，只关注它们的**编排**。

## 3. 本讲源码地图

| 文件 | 本讲关注的内容 |
|---|---|
| [main.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c) | Thread1 主循环、sweep()、dsp_start/dsp_wait、shell 命令表与 VNAShell_executeLine、main() 双线程装配 |
| [nanovna.h](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h) | `SWEEP_*`、`REDRAW_*`、`OP_*` 三组标志位定义，`START_PROFILE` 计时宏 |
| [chconf.h](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chconf.h) | RTOS 配置：tick 频率、互斥量关闭、栈检查开启 |
| [ui.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c) | `operation_requested` 的两个中断来源、`ui_process` 分发 |
| [adc.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/adc.c) | ADC 模拟看门狗中断如何触发触摸事件 |
| [plot.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c) | `plot_into_index` 折线缓存、`draw_all` 按 `redraw_request` 决定刷新范围 |

## 4. 核心概念与源码讲解

### 4.1 Thread1 主循环：仪器的心跳与 sweep_mode 标志

#### 4.1.1 概念说明

一台网络分析仪本质上是一个"永动的采集-显示循环"：不停地扫频、不停地刷新屏幕，同时还要随时响应按键和触摸。NanoVNA 把这三件事全部塞进一个低优先级线程 `Thread1`，用 `while(1)` 顺序执行：

- **要不要测量？** 看 `sweep_mode` 标志；
- **有没有 shell 命令排队？** 看 `shell_function` 指针；
- **有没有按键/触摸？** 看 `operation_requested`（在 `ui_process` 里消化）；
- **要不要重画屏幕？** 看 `redraw_request`（在 `draw_all` 里消化）。

因为测量、UI、绘图在**同一个线程里顺序执行**，它们之间天然互斥——这就是固件能在 `CH_CFG_USE_MUTEXES FALSE`（[chconf.h:186](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chconf.h#L186)）的配置下依然线程安全的核心原因：**把会打架的人关进同一个房间排队**。

`sweep_mode` 的两个标志位定义在 [nanovna.h:98-100](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L98-L100)：

```c
#define SWEEP_ENABLE  0x01   // 连续扫频：扫完一轮立刻再来一轮
#define SWEEP_ONCE    0x02   // 只扫一轮（一次性触发）
extern int8_t sweep_mode;
```

一个诚实的观察：在当前 HEAD 上，**没有任何代码置位 `SWEEP_ONCE`**（用 `grep -rn SWEEP_ONCE *.c` 可以验证，只有 main.c:116 一处清除它的语句）。它是一个保留的历史标志位；固件如今实现"只扫一次"用的是另一条路——`scan` 命令先 `pause_sweep()` 再直接调用 `sweep(false)`（见 4.2.3）。这提醒我们读源码时要区分"定义了的"和"用上了的"。

#### 4.1.2 核心流程

Thread1 一次迭代的完整状态流转（伪代码）：

```text
Thread1（永久循环，优先级 NORMALPRIO-1）
│
├─ sweep_mode 含 ENABLE 或 ONCE?
│   ├─ 是 → completed = sweep(true)     // 测一整轮，可被 UI 打断
│   │        sweep_mode &= ~SWEEP_ONCE  // 一次性标志扫完即清
│   └─ 否 → __WFI()                     // 没事做就睡，等中断叫醒
│
├─ shell_function != 0?                 // 有命令排队等在这执行
│   └─ 是 → 调用它；清零指针；睡 10ms；continue（跳过本轮后续）
│
├─ ui_process()                         // 消化按键/触摸（见 4.4）
│
├─ (SWEEP_ENABLE 有效 且 本轮扫完)?
│   ├─ 是 → 时域模式则 transform_domain()
│   ├─      plot_into_index(measured)   // 复数 → 屏幕折线坐标缓存
│   ├─      redraw_request |= CELLS|BATTERY
│   └─      marker tracking 时搜索并挪 marker
│
└─ draw_all(completed)                  // 按标志位真正画屏（见 4.4）
     └─ 回到循环顶部
```

注意 `completed` 的传播：只有"完整扫完一轮"（`sweep()` 返回 `true`）才走绘图流水线；被打断的一轮（返回 `false`）不更新曲线，避免把半截数据画上屏。

#### 4.1.3 源码精读

**线程的创建与栈**。[main.c:106-110](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L106-L110) 静态分配了 640 字节工作区，线程函数就是普通的 C 函数：

```c
static THD_WORKING_AREA(waThread1, 640);
static THD_FUNCTION(Thread1, arg)
{
  (void)arg;
  chRegSetThreadName("sweep");
```

而在 [main.c:2430](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2430)，main() 在完成全部初始化后启动它，优先级比 shell 低一档：

```c
chThdCreateStatic(waThread1, sizeof(waThread1), NORMALPRIO-1, Thread1, NULL);
```

`NORMALPRIO-1` 意味着：只要 shell 线程想跑 CPU，sweep 线程就得让。实践中这没有造成饥饿，因为 shell 大部分时间阻塞在等 USB 字符，而 sweep 大部分时间睡在 `__WFI()` 里等 I2S 中断。

**主循环本体**。[main.c:112-148](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L112-L148)：

```c
  while (1) {
    bool completed = false;
    if (sweep_mode&(SWEEP_ENABLE|SWEEP_ONCE)) {
      completed = sweep(true);
      sweep_mode&=~SWEEP_ONCE;
    } else {
      __WFI();
    }
    // Run Shell command in sweep thread
    if (shell_function) {
      shell_function(shell_nargs - 1, &shell_args[1]);
      shell_function = 0;
      osalThreadSleepMilliseconds(10);
      continue;
    }
    // Process UI inputs
    ui_process();
    // ...（绘图流水线，见 4.4）
    draw_all(completed);
  }
```

逐行拆解四个要点：

1. `sweep_mode&(SWEEP_ENABLE|SWEEP_ONCE)`：每圈循环**重新读一次标志**，所以外部（shell 的 `pause`/`resume`、菜单的 PAUSE 项）改标志最多一圈内生效——控制面因此可以做得极简单。
2. `__WFI()`：暂停态下线程睡在循环顶部的 WFI 里，靠中断（比如触摸事件最终触发的 EXTI/ADC 中断）唤醒后再次检查标志。
3. `shell_function` 执行完后的 `osalThreadSleepMilliseconds(10)` + `continue`：小睡片刻再回到循环顶部，把测量/UI 的机会让出来；详见 4.3。
4. `draw_all(completed)` 每圈都调用，但内部按 `redraw_request` 决定实际画什么（详见 4.4）。

**控制面的全部实现**只有三个一位操作（[main.c:151-167](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L151-L167)）：

```c
static inline void pause_sweep(void)  { sweep_mode &= ~SWEEP_ENABLE; }
static inline void resume_sweep(void) { sweep_mode |=  SWEEP_ENABLE; }
void toggle_sweep(void)               { sweep_mode ^=  SWEEP_ENABLE; }
```

它们分别被 shell 命令 `pause`/`resume`（[main.c:290-308](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L290-L308)）和 UI 菜单的 PAUSE 项（[ui.c:687-692](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L687-L692)）调用。注意 `cmd_resume` 不只是置位：它还会重建频点表、按需重做校准插值再恢复扫频。

**main 线程这边**（[main.c:2432-2454](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2432-L2454)）：启动 Thread1 后，main() 进入"USB 活着就跑 shell"的循环，USB 断开则每秒醒来检查一次。固件还留了一个可选方案 `VNA_SHELL_THREAD`（[main.c:2314-2329](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2314-L2329)，默认注释掉）把 shell 放进独立线程，代价是多吃 442 字节栈——在 16KB RAM 的机器上作者选择了省。

#### 4.1.4 代码实践

**实践目标**：用 shell 命令实际操纵 `sweep_mode`，验证"控制面只改一个标志、线程下一圈自动服从"的机制，并观察 `scan` 之后仪器处于暂停态这个容易踩坑的行为。

**操作步骤**（需要真机 + USB 串口终端，115200 波特率的 CDC 虚拟串口）：

1. 连接串口终端（如 `screen /dev/ttyACM0` 或 Python `nanovna.py`），敲 `pause`，观察：屏幕曲线不再刷新、扫描指示 LED 停止闪烁。
2. 敲 `resume`，观察曲线恢复滚动。
3. 敲 `scan 50000000 500000000 101`：命令执行时扫完一轮并把数据打回来（若带第 4 参数 outmask），**但之后屏幕不再连续刷新**。
4. 再敲 `resume` 恢复连续扫频。
5. 无真机时的替代做法（源码阅读型）：在纸上填写下表每一步之后 `sweep_mode` 的值（初始 `SWEEP_ENABLE`）：

| 事件 | sweep_mode（十六进制） |
|---|---|
| 上电初始化后 | `0x01` |
| shell 敲 `pause` | ？ |
| 菜单点一次 PAUSE（toggle_sweep） | ？ |
| `scan` 命令执行完 | ？ |

**需要观察的现象**：步骤 3 之后仪器"看起来停了"。

**预期结果**：`cmd_scan` 内部调用了 `pause_sweep()`（见 4.2.3），所以 scan 之后 `sweep_mode = 0x00`；表格答案依次为 `0x01 → 0x00 → 0x01 → 0x00`。USB 抓数据的上位机脚本如果忘掉这一点，会疑惑"为什么 scan 完仪器不刷新了"。（真机行为**待本地验证**。）

#### 4.1.5 小练习与答案

**练习 1**：`sweep_mode = 0x02`（只有 `SWEEP_ONCE`）时，Thread1 会怎样表现？

**答案**：条件 `sweep_mode&(SWEEP_ENABLE|SWEEP_ONCE)` 为真，会执行一轮 `sweep(true)`；随后 `sweep_mode &= ~SWEEP_ONCE` 把仅有的标志清掉，变成 `0x00`；之后每圈都走 `__WFI()` 分支睡觉，UI/绘图仍继续（`ui_process`、`draw_all` 在循环后半段照常执行），只是不再有新测量。这正是"单次触发测量"该有的形态——只是当前代码没有入口去置位它。

**练习 2**：为什么 Thread1 的优先级要比 shell 低一档（`NORMALPRIO-1`）也不会饿死？

**答案**：ChibiOS 是抢占式调度，高优先级的 shell 就绪时确实会抢占 sweep。但 shell 的工作模式是"阻塞等 USB 字符"（`VNAShell_readLine` 里的 `streamRead`），只有用户敲键盘或批量打印数据时才短暂占用 CPU；sweep 线程则把绝大部分时间花在 `dsp_wait()` 的 `__WFI()` 睡眠里（见 4.2.3）。两者几乎不竞争 CPU，所以低优先级足够。

**练习 3**：如果 `sweep_mode` 忘记声明为会被并发访问，只由 shell 线程写、sweep 线程读，还需要加 `volatile` 吗？

**答案**：需要。`sweep_mode` 是 `int8_t`（[main.c:88](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L88)）。若不加 `volatile`，编译器可能把 `while(1)` 里的读取优化成"读一次存寄存器复用"，导致 pause 之后 sweep 线程永远看不到标志变化。（本项目里 `sweep_mode` 实际未加 volatile、`redraw_request` 加了——这种不一致值得留意，`sweep_mode` 靠 `sweep()` 内部大量函数调用迫使编译器重新加载内存，属于"碰巧安全"的写法，新写代码建议显式加 volatile。）

### 4.2 sweep()：可被打断的批处理与 break_on_operation

#### 4.2.1 概念说明

`sweep()` 是对全部 101 个频点的批处理：每个频点做一遍 u2-l1 学过的"设频率→CH0 反射→CH1 传输→误差修正"。一轮在默认带宽下要几十到几百毫秒。问题来了：**用户在扫到第 50 个点时按了菜单键，怎么办？**

选项 A：把这一轮扫完再响应——按键延迟最长达一整轮，手感发木。
选项 B：在任意位置立刻跳出——但中断点之后 `measured[]` 只有半截数据。

NanoVNA 的答案是**粒度折中**：以"一个频点"为最小打断单位。每个频点处理完，检查一次 `operation_requested`；如果有 UI 事件且调用者允许打断（`break_on_operation == true`），立即 `return false` 返回上层——上层（Thread1 主循环）马上调用 `ui_process()` 消化输入，下一圈再**从头**重新扫。打断的代价只是浪费了半轮测量，不会破坏任何数据结构，因为 `measured[]` 的写入永远只发生在 sweep 线程里。

而 `scan` 命令调用的是 `sweep(false)`——不允许打断，保证一次输出完整一致的 101 点数据。

#### 4.2.2 核心流程

单个频点内部的执行序列（含注释中作者标注的耗时参考，单位 µs，来自 [main.c:863-883](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L863-L883) 的行尾注释）：

```text
for 每个频点 i (整轮 ~5300 µs/点):
│
├─ frequencies[i]==0? → break（频点表未填满时提前结束）
├─ delay = set_frequency(frequencies[i])   // ~700：调 si5351，返回需丢弃的缓冲数
├─ tlv320aic3204_select(0)                 // ~60：codec 切到 CH0 反射
├─ dsp_start(delay + (i==0 ? 1 : 0))       // ~1900：丢弃换频暂态，启动累积
│    └─ 两个"等待窗口"里的空档，源码注释特意留了填代码的位置
├─ dsp_wait()                              //   睡在 __WFI 直到累积完成
├─ sample_func(measured[0][i])             // ~60：算 Γ 存入 CH0
├─ tlv320aic3204_select(1) + dsp_start(2)  // ~60+1700：切 CH1 传输，丢 2 块缓冲
├─ dsp_wait()
├─ sample_func(measured[1][i])             // ~60
├─ cal_status 含 APPLY? → apply_error_term_at(i)
├─ electrical_delay != 0? → apply_edelay_at(i)
│                                          // 以上后处理合计 ~170
└─ operation_requested && break_on_operation? → return false   // ★ 打断检查点
```

**等待的艺术**：`dsp_wait()` 不是忙等。看 [main.c:622-627](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L622-L627)：

```c
static inline void
dsp_wait(void)
{
  while (accumerate_count > 0)
    __WFI();
}
```

CPU 睡着等，每 1ms 被 I2S 中断叫醒一次；中断里的 `i2s_end_callback`（[main.c:641-670](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L641-L670)）递减 `wait_count`/`accumerate_count`，条件满足后线程自然退出循环。这是一个"用 WFI 手搓的信号量"：省电，且不需要内核对象。

**时间换精度的旋钮**在 [main.c:604-610](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L604-L610)：五档带宽对应每点累积 1/3/10/33/100 块缓冲，每块 1ms，于是单点测量时间 \( T_{meas} \approx (N_{discard} + N_{accum}) \times 1\,\text{ms} \)，分辨带宽 \( \text{RBW} \approx \frac{1}{N_{accum} \times 1\,\text{ms}} \)——从 1kHz 到 10Hz 正好差两个数量级。

#### 4.2.3 源码精读

**sweep() 本体**，[main.c:856-897](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L856-L897)：

```c
#define DELAY_CHANNEL_CHANGE 2

// main loop for measurement
bool sweep(bool break_on_operation)
{
  int i, delay;
  // blink LED while scanning
  palClearPad(GPIOC, GPIOC_LED);
  // Power stabilization after LED off, also align timings on i == 0
  for (i = 0; i < sweep_points; i++) {         // 5300
    if (frequencies[i] == 0) break;
    delay = set_frequency(frequencies[i]);     // 700
    tlv320aic3204_select(0);                   // 60 CH0:REFLECT, reset and begin measure
    dsp_start(delay + ((i == 0) ? 1 : 0));     // 1900
    //================================================
    // Place some code thats need execute while delay
    //================================================
    dsp_wait();
    // calculate reflection coefficient
    (*sample_func)(measured[0][i]);            // 60

    tlv320aic3204_select(1);                   // 60 CH1:TRANSMISSION, reset and begin measure
    dsp_start(DELAY_CHANNEL_CHANGE);           // 1700
    dsp_wait();
    // calculate transmission coefficient
    (*sample_func)(measured[1][i]);            // 60
                                               // ======== 170 ===========
    if (cal_status & CALSTAT_APPLY)
      apply_error_term_at(i);

    if (electrical_delay != 0)
      apply_edelay_at(i);

    // back to toplevel to handle ui operation
    if (operation_requested && break_on_operation)
      return false;
  }
  // blink LED while scanning
  palSetPad(GPIOC, GPIOC_LED);
  return true;
}
```

五个细节：

1. **`(i == 0) ? 1 : 0`**：第一个点多丢一块缓冲——注释说是"对齐时序"，让一轮的起点有一个稳定的相位基准（LED 刚熄灭也可能引起电源波动，见开头第二行注释）。
2. **`delay` 的来源**：`set_frequency` 返回 si5351 换频后需要丢弃的音频缓冲数（u2-l2 讲过：频段切换要重锁 PLL，得等它稳定）。CH1 只切 codec 通道不动射频，所以固定丢 `DELAY_CHANNEL_CHANGE = 2` 块。
3. **两段 `// Place some code thats need execute while delay` 注释**：作者留下的优化提示——`dsp_wait` 期间的 CPU 是空闲的，理论上可以把误差修正等纯计算挪进来与测量重叠。这是留给二次开发者的并行化空间。
4. **打断检查放在频点末尾**，且必须 `break_on_operation` 为真才生效。
5. **返回值的语义**：`true` = 完整扫完；`false` = 被打断。这个布尔值一路传给 `draw_all(completed)`，决定是否刷新轨迹缓存（4.4.3）。

**`dsp_start` 与配套的 ISR**。[main.c:614-620](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L614-L620) 只是设置两个计数器：

```c
static inline void
dsp_start(int count)
{
  wait_count = count;
  accumerate_count = bandwidth_accumerate_count[bandwidth];
  reset_dsp_accumerator();
}
```

而 [main.c:651-661](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L651-L661) 的中断回调消费它们：

```c
  if (wait_count > 1) {
    --wait_count;                    // 还在丢弃暂态阶段
  } else if (wait_count > 0) {
    if (accumerate_count > 0) {
      dsp_process(p, n);             // 有效累积阶段
      accumerate_count--;
    }
    ...
```

`wait_count` 用"先减到 1 再进入累积"的写法，保证 `dsp_wait()` 的循环条件（`accumerate_count > 0`）在丢弃阶段结束后才开始倒数。两个计数器都是 `volatile uint8_t`（[main.c:601-602](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L601-L602)）：ISR 是唯一写者、线程是读者，单字节访问在 M0 上原子，无需加锁。

**调用方对比**：Thread1 调 `sweep(true)`（[main.c:115](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L115)），`cmd_scan` 调 `sweep(false)`（[main.c:923-927](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L923-L927)）：

```c
  set_frequencies(start, stop, points);
  if (cal_auto_interpolate && (cal_status & CALSTAT_APPLY))
    cal_interpolate(lastsaveid);
  pause_sweep();
  sweep(false);
```

注意 `cmd_scan` 带 `CMD_WAIT_MUTEX` 标志（命令表 [main.c:2177](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2177)），所以它实际运行在 sweep 线程里（4.3 详述）——这意味着 `sweep(false)` 执行期间 UI 不会打断它，但触摸事件会累积在 `operation_requested` 里等它做完。

#### 4.2.4 代码实践

**实践目标**：给 sweep() 的四个阶段（设频率 / CH0 测量 / CH1 测量 / 后处理）加耗时统计，量化 101 点扫频的时间都花在哪，验证"测量等待占大头、控制开销是小头"的判断。

**操作步骤**：

1. 参考 [nanovna.h:492-493](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L492-L493) 已有的计时宏——它用 `chVTGetSystemTimeX()` 取时间差并画到屏幕上：

   ```c
   #define START_PROFILE   systime_t time = chVTGetSystemTimeX();
   #define STOP_PROFILE    {char string_buf[12];plot_printf(string_buf, sizeof string_buf, "T:%06d", chVTGetSystemTimeX() - time);ili9341_drawstringV(string_buf, 1, 60);}
   ```

2. 在 `main.c` 的 `sweep()` 上方加四个累加器（**示例代码**，非项目原有）：

   ```c
   // ===== 示例代码：sweep() 阶段耗时统计 =====
   static uint32_t prof_setfreq, prof_ch0, prof_ch1, prof_post;
   ```

3. 在 `sweep()` 循环体内用 `chVTGetSystemTimeX()` 差值包裹四个阶段（**示例代码**；1 tick = 100 µs，来自 `CH_CFG_ST_FREQUENCY 10000`）：

   ```c
   for (i = 0; i < sweep_points; i++) {
     if (frequencies[i] == 0) break;
     systime_t t0 = chVTGetSystemTimeX();
     delay = set_frequency(frequencies[i]);
     prof_setfreq += chVTGetSystemTimeX() - t0;

     t0 = chVTGetSystemTimeX();
     tlv320aic3204_select(0);
     dsp_start(delay + ((i == 0) ? 1 : 0));
     dsp_wait();
     (*sample_func)(measured[0][i]);
     prof_ch0 += chVTGetSystemTimeX() - t0;

     t0 = chVTGetSystemTimeX();
     tlv320aic3204_select(1);
     dsp_start(DELAY_CHANNEL_CHANGE);
     dsp_wait();
     (*sample_func)(measured[1][i]);
     prof_ch1 += chVTGetSystemTimeX() - t0;

     t0 = chVTGetSystemTimeX();
     if (cal_status & CALSTAT_APPLY)
       apply_error_term_at(i);
     if (electrical_delay != 0)
       apply_edelay_at(i);
     prof_post += chVTGetSystemTimeX() - t0;

     if (operation_requested && break_on_operation)
       return false;
   }
   ```

4. 在 `sweep()` 返回 `true` 之前（`palSetPad` 之后）输出结果（**示例代码**）：

   ```c
   shell_printf("setfreq %u ch0 %u ch1 %u post %u ticks\r\n",
                prof_setfreq, prof_ch0, prof_ch1, prof_post);
   prof_setfreq = prof_ch0 = prof_ch1 = prof_post = 0;
   ```

   打印路径与 `cmd_data` 相同（sweep 线程里 `shell_printf`），是已被现有代码验证安全的做法；统计量的清除放在打印后，避免半途被打断时数据翻倍。

5. 编译烧录（u1-l2 的流程），串口执行：

   ```
   pause
   scan 50000 900000000 101
   bandwidth 0    ← 再用 bandwidth 1/2/3/4 重复 scan
   scan 50000 900000000 101
   ```

**需要观察的现象**：四个数字随带宽档位的变化；`setfreq` 阶段在跨越 300MHz 谐波阈值（会重锁 PLL）的扫描中是否变大。

**预期结果**（**待本地验证**）：`ch0`/`ch1` 随带宽档位近似按 1/3/10/33/100 的比例增长，占绝对大头；`post` 只在开启校准（`cal_status` 含 `CALSTAT_APPLY`）时才明显非零；`setfreq` 占比小但谐波模式下偏大。把实测值除以 101 换算成每点耗时，与源码行尾注释（700+1900+1700+170+…≈5300 µs）对量级。

**无真机替代实践**：把 4.2.2 的频点序列写成一张"上下文标注表"——每行标明该函数运行在哪个上下文（sweep 线程 / I2S 中断 / si5351 经 I2C 阻塞调用），并用作者注释的 µs 数计算四阶段占比（约 13% / 36% / 32% / 3%，余为循环开销），体会"等待即睡眠"的实时系统设计。

#### 4.2.5 小练习与答案

**练习 1**：为什么打断检查放在频点末尾而不是 `dsp_wait()` 循环内？

**答案**：打断的最小单位是"一个完整频点"。若在 `dsp_wait()` 中途跳出，`accumerate_count` 还没减到 0，累积器里留着半截和；下一次 `dsp_start` 虽然会 `reset_dsp_accumerator()`，但"本轮 `measured[]` 里已有半截数据 + 上层误以为扫完"的组合会破坏 `completed` 的语义。放在频点末尾，`measured[0][i]`/`measured[1][i]` 总是成对完整写入。

**练习 2**：`sweep(false)` 期间用户狂点触摸屏，事件会丢吗？

**答案**：不会丢但会"合并"。`operation_requested |= OP_TOUCH`（[ui.c:2272-2276](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L2272-L2276)）是按位或，多次触摸只置同一个位；`sweep(false)` 不检查它，事件等到 `cmd_scan` 返回后由 Thread1 下一圈的 `ui_process()` 统一处理（实际触摸坐标在处理时才测量）。也就是说打断机制牺牲了"中途响应"，换来"批量处理"。

**练习 3**：若把 `accumerate_count` 的 `volatile` 去掉，最可能看到什么现象？

**答案**：`dsp_wait()` 的 `while (accumerate_count > 0)` 可能被编译器优化成只读一次（或干脆用寄存器缓存），条件永远成立，函数永不返回——仪器"冻"在第一次测量上，shell 也随之失去响应（`CMD_WAIT_MUTEX` 命令在等 sweep 线程空出来）。这是嵌入式最经典的 volatile 缺失死循环。

### 4.3 shell_function：没有互斥量的跨线程命令执行

#### 4.3.1 概念说明

shell 在 main 线程解析命令，但很多命令（`scan`、`data`、`cal`、`capture`……）要动 si5351、codec、`measured[]` 这些"测量现场"。如果它们在 main 线程直接执行，就会和 sweep 线程并发抢同一批硬件与数据——而固件根本没有互斥量（`CH_CFG_USE_MUTEXES FALSE`）。

解法出人意料地简单：**把命令函数的指针放进一个共享变量，然后等 sweep 线程自己来执行它**。main 线程是"下单的顾客"，sweep 线程是"唯一的厨师"，`shell_function` 就是挂在窗口上的订单。厨师做完（把指针清零），顾客才离开。任意时刻订单只有一个、厨师也只有一个，天然互斥，一个字节都不用在锁上。

这就是命令表里 `CMD_WAIT_MUTEX` 标志的含义——名字叫"等互斥量"，实际等的是"厨师空出手来"。

#### 4.3.2 核心流程

一次 `scan 50000 30000000 101` 的完整时序：

```text
main 线程                                    sweep 线程（Thread1）
──────────                                    ──────────────────
VNAShell_readLine 读到一行
VNAShell_executeLine 切参数
查表：scan 带 CMD_WAIT_MUTEX
shell_function = cmd_scan      ──写──▶       （正在上一轮循环里）
do {                            循环顶部：检查 sweep_mode
  sleep(100ms)                  shell_function != 0?
} while (shell_function)          ├─ 调 cmd_scan(3, args)     ◀──订单被执行
                                  │    └─ sweep(false) 完整扫完 101 点
（每 100ms 醒来读一次指针）        │    └─ 打印数据
                                  ├─ shell_function = 0       ──写──▶
                                  └─ sleep(10ms); continue
读到的下一条命令 / 打印提示符  ◀── 指针已为 0，while 退出
```

关键握手：**main 线程在指针非零期间绝不解析下一条命令**（阻塞在 do-while 里），所以 sweep 线程"调用→清零"两步之间不可能有新订单写入——不存在丢失或覆盖订单的窗口。同时 `shell_args[]`/`shell_nargs` 也是共享的，同样靠这个握手保护：顾客在厨师读菜单期间绝不会改菜单。

#### 4.3.3 源码精读

**共享的"订单"变量**。[main.c:55-59](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L55-L59)：

```c
// Shell command line buffer, args, nargs, and function ptr
static char shell_line[VNA_SHELL_MAX_LENGTH];
static char *shell_args[VNA_SHELL_MAX_ARGUMENTS + 1];
static uint16_t shell_nargs;
static volatile vna_shellcmd_t  shell_function = 0;
```

`shell_function` 被显式声明为 `volatile` 的函数指针——两个线程都要碰它，这是必须的。（对照 4.1.5 练习 3，`sweep_mode` 反而没有，可见代码并非处处如一。）

**命令表与标志**。[main.c:2143-2152](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2143-L2152)：

```c
typedef struct {
  const char           *sc_name;
  vna_shellcmd_t    sc_function;
  uint16_t flags;
} VNAShellCommand;

// Some commands can executed only in sweep thread, not in main cycle
#define CMD_WAIT_MUTEX  1
```

注释说得直白："有些命令只能在 sweep 线程执行"。[main.c:2153-2208](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2153-L2208) 的表里哪些命令戴了这顶帽子一目了然：`freq`、`data`、`scan`、`touchcal`、`touchtest`、`cal`、`recall`、`capture`——全是会**重配置测量现场或读走测量数据**的；而 `bandwidth`、`marker`、`trace`、`pause` 这类只碰简单变量的命令直接在 main 线程跑（这也埋下 4.4.5 要讨论的小隐患）。

**下单与等待**。[main.c:2296-2312](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2296-L2312)：

```c
  for (scp = commands; scp->sc_name != NULL; scp++) {
    if (strcmp(scp->sc_name, shell_args[0]) == 0) {
      if (scp->flags & CMD_WAIT_MUTEX) {
        shell_function = scp->sc_function;
        // Wait execute command in sweep thread
        do {
          osalThreadSleepMilliseconds(100);
        } while (shell_function);
      } else {
        scp->sc_function(shell_nargs - 1, &shell_args[1]);
      }
      return;
    }
  }
```

100ms 的轮询粒度意味着命令的**启动延迟**最多约等于 sweep 线程当前一圈的时长加 100ms——对人工敲命令完全无感。

**取单与执行**回到 Thread1（[main.c:120-126](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L120-L126)）：

```c
    // Run Shell command in sweep thread
    if (shell_function) {
      shell_function(shell_nargs - 1, &shell_args[1]);
      shell_function = 0;
      osalThreadSleepMilliseconds(10);
      continue;
    }
```

注意它排在 `ui_process()` **之前**：命令优先级高于 UI 输入；`continue` 跳过本圈的测量与绘图，让订单独占这一圈（`scan` 这种长订单内部自己调 `sweep(false)`，不依赖外层）。执行后的 10ms 小睡给 main 线程留出观察"指针已清零"的时间窗，也避免刚执行完重量级命令立刻又开一轮扫频。

**参数怎么传**：`VNAShell_executeLine` 把命令名放在 `shell_args[0]`、参数依次排后（[main.c:2270-2294](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2270-L2294) 的就地切分），所以 sweep 线程调用时传 `shell_nargs - 1` 和 `&shell_args[1]`——跳过命令名，等价于普通 C 的 `argc/argv` 约定。

#### 4.3.4 代码实践

**实践目标**：追踪 `data 0` 命令从敲键到打印的完整路径，亲手标注每条语句的执行上下文，检验对握手协议的理解。

**操作步骤**：

1. 通读 [main.c:2231-2265](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2231-L2265)（`VNAShell_readLine`：读字符、回显、退格处理）和 [main.c:2270-2312](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2270-L2312)（切参、查表、分流）。
2. 在纸上抄下下面这张表，为每行填 main / sweep 两个空格（哪个线程执行了它）：

| 语句 | 上下文 |
|---|---|
| `streamRead(shell_stream, &c, 1)` | ？ |
| `shell_args[shell_nargs++] = lp`（切参数） | ？ |
| `shell_function = scp->sc_function` | ？ |
| `osalThreadSleepMilliseconds(100)`（do-while 里） | ？ |
| `cmd_data` 内的 `for` 循环与 `shell_printf` | ？ |
| `shell_function = 0`（清零） | ？ |

3. 有真机的话用 `data 0` 实测：注意打印 101 行期间屏幕是否还在刷新（应该停住——因为 `cmd_data` 在 sweep 线程独占执行，`draw_all` 得等它）。

**需要观察的现象**：打印期间轨迹不动、打印完恢复。

**预期结果**：表全部填 main / main / main / main / **sweep** / **sweep**；真机上打印期间屏幕静止（**待本地验证**）。这个"shell 长输出会冻结显示"的现象正是单厨师模型的可观察代价。

#### 4.3.5 小练习与答案

**练习 1**：如果 `scan` 不带 `CMD_WAIT_MUTEX`，直接在 main 线程执行，最坏会发生什么？

**答案**：`cmd_scan` 会与 sweep 线程并发操作同一批资源：两者同时经 I2C 配置 si5351/tlv320aic3204（I2C 事务交错导致配置错乱），同时写 `measured[]`（数据互相覆盖），`set_frequencies` 还会重写 `frequencies[]` 而 sweep 正在遍历它。轻则数据错乱，重则 I2C 总线锁死。`CMD_WAIT_MUTEX` 把这些命令序列化进唯一的测量线程，从根上消除竞争。

**练习 2**：main 线程用 100ms 轮询等 `shell_function` 清零，为什么不直接用一个二值信号量唤醒？

**答案**：可以，但代价更高。固件的 RAM 只有 16KB，ChibiOS 信号量对象虽小（几十字节），还需要 `CH_CFG_USE_SEMAPHORES TRUE`（本项目为 FALSE，[chconf.h:167](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chconf.h#L167)）；而命令交互对人来说 100ms 完全无感，轮询期间 main 线程睡在 `osalThreadSleepMilliseconds` 里也不耗 CPU。这是典型的"用可容忍的延迟换 RAM"的嵌入式取舍。

**练习 3**：sweep 线程执行完 `shell_function(...)` 之后那句 `osalThreadSleepMilliseconds(10)` 去掉行不行？

**答案**：功能上大概率仍正确（清零操作本身已保证握手成立），但有两点损失：一是 main 线程可能刚睡下还没醒，sweep 线程立刻开始下一轮扫频会让"下一条命令"的响应排在整轮扫频之后；二是重量级命令（如 `cal`）刚跑完就立刻满负荷扫频，不给系统喘息。这 10ms 是廉价的"礼让"，属于工程打磨而非正确性必需。

### 4.4 ui_process 与 redraw_request：输入处理与绘制流水线

#### 4.4.1 概念说明

输入和显示看似两件事，固件里却共用同一条流水线，因为它们的消费者都是 sweep 线程：

- **输入侧**：中断只负责"举旗"（把 `operation_requested` 置位），真正的解析与响应推迟到 sweep 线程空闲时的 `ui_process()`。这叫**中断顶半部/底半部分离**——中断里做越少越好，复杂的菜单逻辑在线程里慢慢做。
- **显示侧**：需要重画的地方不直接画，而是把要求写进 `redraw_request` 的各个位（**请求**）；每圈循环末尾 `draw_all()` 统一消费这些位，按需重画（**响应**）。请求可以在多个地方发起、一次合并处理，避免重复刷屏。

`operation_requested` 的位定义在 [nanovna.h:432-436](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L432-L436)（`OP_NONE`/`OP_LEVER` 拨轮与按键/`OP_TOUCH` 触摸），`redraw_request` 的位定义在 [nanovna.h:291-297](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L291-L297)：

```c
#define REDRAW_CELLS      (1<<0)   // 曲线区域（按 cell 局部重绘）
#define REDRAW_FREQUENCY  (1<<1)   // 底部频率读数
#define REDRAW_CAL_STATUS (1<<2)   // 校准状态角标
#define REDRAW_MARKER     (1<<3)   // marker 及其读数
#define REDRAW_BATTERY    (1<<4)   // 电池图标
#define REDRAW_AREA       (1<<5)   // 整个绘图区（最重）
```

#### 4.4.2 核心流程

从手指碰上屏幕到像素更新，跨越四个上下文：

```text
【中断】触摸 → 电阻屏电压变化 → ADC 模拟看门狗越限 (adc.c AWD)
【中断】adc_interrupt → handle_touch_interrupt → operation_requested |= OP_TOUCH
【中断】拨轮/按键 → EXTI 上升沿 → extcb1 → operation_requested |= OP_LEVER
                                │
                （sweep() 每个频点末尾看到旗帜，return false 打断扫频）
                                ▼
【sweep 线程】Thread1 循环：ui_process()
                ├─ OP_LEVER? → ui_process_lever → (normal/menu/numeric/keypad 四种模式)
                │              └─ 例如菜单 PAUSE 项 → toggle_sweep()
                ├─ OP_TOUCH? → ui_process_touch → 移动 marker / 进菜单……
                └─ operation_requested = OP_NONE          // 清旗帜
【sweep 线程】(扫完一轮时) plot_into_index(measured)        // 复数→折线坐标缓存
              redraw_request |= REDRAW_CELLS | REDRAW_BATTERY
              marker_tracking? → marker_search() 移动 marker，置 REDRAW_MARKER
【sweep 线程】draw_all(completed)                            // 消费 redraw_request
              └─ 全部处理完后 redraw_request = 0
```

#### 4.4.3 源码精读

**中断侧：两处举旗**。拨轮/按键走 EXTI 回调 [ui.c:2216-2222](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L2216-L2222)；触摸走 ADC 模拟看门狗 [adc.c:154-157](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/adc.c#L154-L157) → [ui.c:2272-2276](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L2272-L2276)：

```c
static void extcb1(EXTDriver *extp, expchannel_t channel)   // EXTI 中断
{
  ...
  operation_requested|=OP_LEVER;
}

void handle_touch_interrupt(void)                            // ADC AWD 中断调用
{
  operation_requested|= OP_TOUCH;
}
```

两个函数都只有一句按位或——中断里"举旗就走"，这正是顶半部该有的样子。

**线程侧：分发**。[ui.c:2205-2213](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L2205-L2213)：

```c
void
ui_process(void)
{
  if (operation_requested&OP_LEVER)
    ui_process_lever();
  if (operation_requested&OP_TOUCH)
    ui_process_touch();
  operation_requested = OP_NONE;
}
```

`ui_process_lever` 内部再按当前 UI 模式（normal/menu/numeric/keypad）分流到不同的处理函数；按键去抖与单击/长按判定在 `btn_check`（[ui.c:127-160](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L127-L160)，用 `chVTGetSystemTime()` 做时间戳比较）。菜单动作则像 [ui.c:687-692](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L687-L692) 的 PAUSE 项那样，最终落到 `toggle_sweep()` 这类一位操作上。

**测量到像素的两级流水**。第一级在 Thread1 主循环（[main.c:131-144](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L131-L144)）：

```c
    if (sweep_mode & SWEEP_ENABLE && completed) {
      if ((domain_mode & DOMAIN_MODE) == DOMAIN_TIME) transform_domain();
      // Prepare draw graphics, cache all lines, mark screen cells for redraw
      plot_into_index(measured);
      redraw_request |= REDRAW_CELLS | REDRAW_BATTERY;

      if (uistat.marker_tracking) {
        int i = marker_search();
        if (i != -1 && active_marker != -1) {
          markers[active_marker].index = i;
          redraw_request |= REDRAW_MARKER;
        }
      }
    }
    // plot trace and other indications as raster
    draw_all(completed);  // flush markmap only if scan completed to prevent
                          // remaining traces
```

`plot_into_index`（[plot.c:1191-1211](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1191-L1211)）把每条启用轨迹的复数换算成屏幕坐标，**只缓存不画屏**：

```c
void
plot_into_index(float measured[2][POINTS_COUNT][2])
{
  int t, i;
  for (t = 0; t < TRACES_MAX; t++) {
    if (!trace[t].enabled)
      continue;
    int ch = trace[t].channel;
    index_t *index = trace_index[t];
    for (i = 0; i < sweep_points; i++)
      index[i] = trace_into_index(t, i, measured[ch]);
  }
  mark_cells_from_index();
  markmap_all_markers();
}
```

第二级 `draw_all`（[plot.c:1409-1425](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1409-L1425)）是 `redraw_request` 的唯一消费者：

```c
void
draw_all(bool flush)
{
  if (redraw_request & REDRAW_AREA)
    force_set_markmap();
  if (redraw_request & REDRAW_MARKER)
    markmap_upperarea();
  if (redraw_request & (REDRAW_CELLS | REDRAW_MARKER | REDRAW_AREA))
    draw_all_cells(flush);
  if (redraw_request & REDRAW_FREQUENCY)
    draw_frequencies();
  if (redraw_request & REDRAW_CAL_STATUS)
    draw_cal_status();
  if (redraw_request & REDRAW_BATTERY)
    draw_battery_status();
  redraw_request = 0;
}
```

从重到轻：整区 → 轨迹 cell → 频率 → 校准角标 → 电池。行尾注释解释了 `completed` 传到这里的用意：只有完整扫完才"冲刷"markmap（脏矩形位图，u4-l4 会专门讲），防止把上一轮的残影留在屏上。

**谁在写 `redraw_request`？** 用 `grep -n redraw_request *.c` 数一数（ui.c/plot.c/main.c 加起来十来处）：绝大多数写入发生在 sweep 线程上下文（`ui_process` 的菜单回调、`plot_into_index` 之后的置位）。但也有例外——`set_electrical_delay`（[main.c:1710-1717](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1710-L1717)）被 `cmd_edelay` 调用，而 `edelay` 命令**没有** `CMD_WAIT_MUTEX`（[main.c:2189](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2189)），于是这行 `redraw_request |= REDRAW_MARKER` 实际运行在 main 线程。`marker`、`trace` 命令同理。这构成一个真实（但被"自愈"掩盖）的竞态窗口，见练习 3。

#### 4.4.4 代码实践

**实践目标**：亲手解码一次 `redraw_request` 的值，把"位标志 → 屏幕上哪块被重画"的映射内化。

**操作步骤**：

1. 对照 [nanovna.h:291-297](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L291-L297) 手工解码下面三个场景下 `draw_all` 入口处 `redraw_request` 的值（写出十六进制和命中的分支）：
   - 场景 A：连续扫频刚完成一轮（无 marker tracking，电池图标随轨迹一起刷新）；
   - 场景 B：场景 A 的基础上开启了 marker tracking 且搜索成功；
   - 场景 C：用户在菜单里改了电延迟（`set_electrical_delay` 置位），恰好与场景 A 的置位发生在同一圈。
2. 为每个场景列出 `draw_all` 里会执行的绘图调用序列（参考 [plot.c:1409-1425](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1409-L1425)）。
3. 有真机可选做：给 `draw_all` 入口临时加一句打印（**示例代码**）`shell_printf("redraw=%02x flush=%d\r\n", redraw_request, flush);`，串口观察连续扫频时值的周期性变化，以及拖动 marker 时新出现的位。

**预期结果**：
- A：`REDRAW_CELLS|REDRAW_BATTERY = 0x11` → 执行 `draw_all_cells(flush=true)` + `draw_battery_status()`；
- B：再或上 `REDRAW_MARKER` → `0x19`，额外执行 `markmap_upperarea()`（且 `draw_all_cells` 因条件包含 MARKER 仍执行）；
- C：`0x19`（假设 `set_electrical_delay` 的 `REDRAW_MARKER` 与 A 的两位合并在同一字节里）。
- 真机打印**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `extcb1`/`handle_touch_interrupt` 里不直接调用菜单处理，而只置一个位？

**答案**：这两个函数运行在中断上下文，最高优先级、会打断一切。菜单处理包含去抖延时、触摸坐标的 ADC 测量、字符串格式化和大量绘图——若在中断里执行，I2S 每 1ms 一次的采样回调都会被长时间推迟，DSP 累加丢块，测量就废了。置位一两条指令即可完成，把长活留给低优先级的 sweep 线程，是标准的顶半部/底半部分离。

**练习 2**：`ui_process()` 末尾为什么是 `operation_requested = OP_NONE;`（整体清零）而不是逐位清？

**答案**：因为 `ui_process` 在同一次调用里已把 LEVER 和 TOUCH 两个位都检查并处理完，剩余的位没有定义含义，整体赋零最省事也最不易漏。副作用是：处理期间中断新置的位会被这次清零"吞掉"——但触摸/按键事件是电平持续或人工重复的，下次触摸再看即可；固件选择了这个简单的语义。

**练习 3**（进阶，源码分析题）：`cmd_edelay`（main 线程执行）里的 `redraw_request |= REDRAW_MARKER` 与 sweep 线程 `draw_all` 里的 `redraw_request = 0` 并发，会产生什么问题？为什么用户几乎察觉不到？

**答案**：`|=` 是读-改-写三步，不是原子操作。时序若为：main 线程读到 `0x11` → sweep 线程 `draw_all` 把它清成 `0x00` → main 线程把"旧值或上 MARKER"的 `0x19` 写回——结果是电池/轨迹的重画请求**丢失**这一圈。用户察觉不到的原因有二：一是连续扫频模式下每个扫描周期都会重新置位 `REDRAW_CELLS|REDRAW_BATTERY`，丢失的请求下一轮自动补上（自愈）；二是窗口本身极窄（几条指令）。但若扫频处于暂停态，丢一次请求的后果就会显现——这解释了为什么严谨的做法是把这类命令也挂上 `CMD_WAIT_MUTEX`，或改用关中断保护的字节操作。这是读这套"无锁"代码时必须保持的清醒：无锁不是因为不可能竞争，而是竞争被"单一写者纪律 + 周期性自愈"消化了。

## 5. 综合实践

**任务：绘制 NanoVNA 的"上下文-时序全景图"，并用它审计一个真实交互的响应路径。**

把本讲四个模块串成一张图（建议用纸或 mermaid）：

1. **三列泳道**：main 线程 / sweep 线程 / 中断上下文（I2S、EXTI、ADC-AWD）。
2. **两条竖直标志轴**夹在泳道之间：左边 `sweep_mode`、`shell_function`、`operation_requested`，右边 `redraw_request`，用箭头标注每个读/写发生在哪个函数的哪一行（本讲引用过的行号足够拼出全图）。
3. 在图上走一遍下面这个场景，写出每一步的编号：**连续扫频中，用户转动拨轮把 marker 从第 30 点挪到第 32 点**——从 EXTI 中断举旗，到 `sweep()` 在某频点末尾 `return false`，到 `ui_process_lever` 修改 `markers[]`，到 `redraw_request` 置位，到 `draw_all` 重画 marker，再到下一轮扫频从头开始。
4. **审计题**：这条路径上有几处"读-改-写"共享变量？哪些在单线程上下文里天然安全，哪些存在理论竞态（对照 4.4.5 练习 3 的分析）？写 200 字的结论。

**验收标准**：图上任意一个箭头你都能说出"这一行代码在哪个文件第几行、为什么运行在那个上下文"。做到这一点，本讲的目标就全部达成了。

## 6. 本讲小结

- **一个心跳**：Thread1 的 `while(1)` 是整台仪器的调度骨架——按序消费 `sweep_mode`（要不要测）、`shell_function`（有没有命令）、`operation_requested`（有没有输入）、`redraw_request`（要不要重画）四个共享标志。
- **无锁的代价与纪律**：测量、UI、绘图全部收进同一个线程顺序执行，天然互斥；跨线程只传"单写者标志"和"函数指针订单"（`CMD_WAIT_MUTEX` 握手），RAM 里一个锁都不用。
- **粒度打断**：`sweep(break_on_operation)` 以频点为最小打断单位响应 UI；`scan` 用 `sweep(false)` 保证一次性输出完整数据，代价是执行期间 UI 冻结、且结束后仪器停在暂停态。
- **等待即睡眠**：`dsp_wait` 的 `__WFI` 循环 + 每 1ms 的 I2S 中断构成"手搓信号量"，`volatile` 计数器是它与线程之间的安全纽带。
- **请求-响应式刷新**：显示更新分两级——`plot_into_index` 缓存折线坐标并置位 `redraw_request`，`draw_all` 每圈统一消费标志、按需局部重画。
- **诚实的边界**：`SWEEP_ONCE` 是无人置位的保留位；`edelay`/`marker` 等无 `CMD_WAIT_MUTEX` 命令对 `redraw_request` 的读-改-写存在理论竞态，靠周期性扫频自愈——读源码时要能看出这两点。

## 7. 下一步学习建议

本讲完成了测量主链路的"调度视角"，接下来有两个方向：

1. **数据与配置层**（第三单元）：u3-l1 讲 `set_sweep_frequency`/`set_frequencies` 如何生成频点表——也就是本讲 sweep() 遍历的 `frequencies[]` 从哪来、marker 索引如何随频率联动；u3-l2/u3-l3 讲 `apply_error_term_at` 背后的 SOL 校准误差模型。
2. **显示与交互层**（第四单元）：u4-l4 专门拆解本讲一笔带过的 markmap 脏矩形机制（`draw_all_cells` 如何只重画脏 cell），u4-l5 深入 `ui_process` 背后的菜单树与四种 UI 模式。

建议先做 u3-l1：它会补全"sweep() 的输入从哪来"这最后一块拼图，让你能从一条 shell 命令出发、完整追到屏幕上的一个像素。
