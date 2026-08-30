# 一只旋钮走天下：按键与编码器 UI 状态机

## 1. 本讲目标

CentSDR 整机对用户只暴露**一个旋转编码器**（可旋转、可按压）。频率、音量、增益、调制模式、AGC、频谱显示方式……全部操作都靠这一个小部件完成。学完本讲，你应该能够：

1. 说清「一只旋钮」背后的完整事件管线：EXT 双边沿中断 → `enc_count` 累积 → Thread2 以 100Hz 轮询 `ui_process()` 排空事件。
2. 逐行解释 `btn_check()` 如何用三个时间常数（1ms 消抖、50ms 双击窗、1.6s 长按阈值）和一个事件抑制标志，把有抖动的机械触点变成干净的单击/双击/长按事件。
3. 解释正交编码器的四倍频中断采样、事件编码 s ∈ {0,1,2,3} 与 `trans_tbl` 状态转移表 +「特定状态下 B 边沿计数」的解码方式。
4. 画出 `uistat.mode` 档位状态机的完整转移图：单击循环、按住调档、松开调值，以及 AGC 开启时跳过 RFGAIN 的特例。
5. 发现并分析这套代码的几处边界行为（双击事件无人消费、`mode--` 在 CHANNEL 处不回绕等），并理解「事件抑制」这类机制如何避免手势误触发。

## 2. 前置知识

### 2.1 轮询与中断：两种「感知世界」的方式

- **轮询（polling）**：线程周期性地主动读外设状态。Thread2 每 10ms 调一次 `ui_process()`，就是轮询。优点是逻辑简单、运行在线程上下文里可以随便调用任何函数；缺点是响应延迟受轮询周期限制。
- **中断（interrupt）**：硬件事件发生时 CPU 立刻打断当前代码，跳去执行回调。编码器 A/B 相每来一个边沿就触发一次 EXT 中断。优点是零漏检；缺点是运行在中断上下文，只适合极短的操作（这里只做一次查表和一次加法）。

CentSDR 的选择很典型：**快而窄的信号（编码器边沿）走中断累积计数，慢而宽的逻辑（按键判定、档位切换）走线程轮询**。

### 2.2 机械按键的「抖动」与消抖

机械触点闭合/断开的瞬间会在几微秒到几毫秒内反复通断，电平上表现为一串毛刺。若不处理，一次按下会被误判成几十次「按下-释放」。常见消抖手段：

- 时间滤波：只有电平变化距今超过某阈值（本讲为 1ms）才承认；
- 采样滤波：以远大于抖动持续期的周期采样（本讲 10ms 轮询本身就是第一级滤波）；
- 事件抑制：某些手势（按住旋转）之后的释放不再当作点击。

### 2.3 正交编码器（quadrature encoder）

旋转编码器输出 A、B 两路方波，相位差固定为四分之一周期（90°，「正交」由此得名）。一个机械周期（360°）内 A、B 两线共产生 4 个边沿：

- **方向**由两相边沿的先后次序决定：A 超前 B 为一个方向，B 超前 A 为另一个方向；
- **分辨率**按每周期计数次数分档：只认 B 的一个边沿称 x1 解码；A、B 各一个边沿称 x2；全部 4 个边沿都计数称 x4，即「四倍频」。

本固件让 A、B 的**每个边沿都触发中断**（四倍频采样），但只在其中一部分边沿上真正计数，方向判决交给状态表。

### 2.4 ChibiOS 系统滴答（tick）

[chconf.h:51](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/chconf.h#L51) 配置 `CH_CFG_ST_FREQUENCY` 为 10000 Hz，即 1 tick = 100µs：

\[ T_{tick} = \frac{1}{10000}\,\text{s} = 100\,\mu\text{s} \]

本讲所有时间常数（消抖、双击窗、长按阈值）都以 tick 为单位，换算全靠这个频率。

### 2.5 前置讲义回顾

- u1-l3：Thread1/Thread2 的创建与分工；
- u4-l3：`disp_process()` 的 FLAG 脏标志机制——本讲的 `disp_update()`/`disp_clear_aux()` 正是往那里投递刷新请求；
- u2-l2：`tlv320aic3204_set_gain`、`set_volume` 等增益接口（本讲档位调整最终都落到它们）。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [ui.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c) | UI 状态机本体 | 事件宏、`btn_check`、`ext_callback`/`trans_tbl`、`ui_process`、`ui_init` |
| [main.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c) | 线程与命令 | Thread2 轮询循环、上拉配置、`save_config_current_channel`、`uitest` 命令 |
| [nanosdr.h](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h) | 全局共享头 | `uistat_t` 中的 mode/agcmode/digit/spdispmode/wfdispmode 枚举 |
| display.c（辅助） | 屏幕渲染 | mode 值如何映射为高亮/辅助信息带（u4-l3 已详述，本讲只引用） |
| chconf.h（辅助） | 内核配置 | tick 频率 10000 Hz |

## 4. 核心概念与源码讲解

### 4.1 交互模型总览：一只旋钮、两类事件、一个 100Hz 轮询循环

#### 4.1.1 概念说明

整机交互只有一个物理部件：带按压开关的旋转编码器，共占用三个 GPIO：

- PA0：按压开关（button push）；
- PB1、PB2：编码器 A、B 两相（对应 EXTI 通道 1、2）。

固件把它拆成**两类逻辑事件**：

1. **按键事件**——以 10ms 周期采样 PA0 电平，由 `btn_check()` 判定出单击 / 双击 / 长按；
2. **旋转事件**——A/B 每个边沿进 EXT 中断，由 `ext_callback()` 累积到全局 `enc_count`，轮询时用 `fetch_encoder_tick()` 一次性取走（排空）并清零。

两者汇合在 `ui_process()`：先处理按键事件切换「档位」（mode），再按**旋转时是否按住**决定是「调档」还是「调值」。这就是「一只旋钮走天下」的全部秘密——**一个输入维度（旋转量）× 一个修饰键（是否按住）× 一个档位状态机**。

#### 4.1.2 核心流程

```text
硬件层    PA0 ──轮询采样──┐
          PB1/PB2 ──EXT双边沿中断──> ext_callback: enc_count ± 1, enc_status 查表
                                   │
线程层    Thread2 (100Hz)          │
          ├─ disp_process()        │  上一周期的刷新请求先画掉
          ├─ ui_process() <────────┘
          │   ├─ btn_check()          -> 单击/长按事件
          │   ├─ fetch_encoder_tick() -> 本周期累积的旋转格数
          │   ├─ 按住旋转 => 切换 uistat.mode（调档）
          │   └─ 松开旋转 => 修改当前 mode 对应的数值（调值）
          ├─ chThdSleepMilliseconds(10)
          └─ stat.fps_count++（顺便统计帧率/ADC 溢出）
```

注意顺序：`disp_process()` 在 `ui_process()` **之前**执行，所以本周期内 UI 状态机请求的刷新要到下一个周期（约 10ms 后）才真正画到屏幕上。

#### 4.1.3 源码精读

**轮询循环**：[main.c:906-924](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L906-L924) 定义了 Thread2（注册名 "button"），其中 [main.c:913-916](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L913-L916) 是心脏：先 `disp_process()` 消化脏标志，再 `ui_process()` 采集输入并可能设置新的脏标志，睡 10ms，`fps_count++` 计一帧。循环末尾还顺路读编解码器的 sticky 寄存器统计 ADC 溢出。

**事件编码**：[ui.c:32-48](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L32-L48) 定义了事件位图（`EVT_BUTTON_SINGLE_CLICK` 0x01、`EVT_BUTTON_DOUBLE_CLICK` 0x02、`EVT_BUTTON_DOWN_LONG` 0x04、旋转方向 `EVT_UP`/`EVT_DOWN` 等）和三个时间常数（[ui.c:41-43](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L41-L43)）：

| 宏 | 值 | 换算（1 tick = 100µs） | 含义 |
|---|---|---|---|
| `BUTTON_DOWN_LONG_TICKS` | 16000 | 1.6 s | 按住超过此时长判为长按 |
| `BUTTON_DOUBLE_TICKS` | 500 | 50 ms | 距上次按下 50ms 内再按判为双击 |
| `BUTTON_DEBOUNCE_TICKS` | 10 | 1 ms | 电平变化的最小承认间隔 |

**按键读取与板级极性**：[ui.c:56-60](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L56-L60) 的 `read_buttons()` 读 GPIOA 端口 bit0，再与 `config.button_polarity` 异或——rev1 板 PA0 接上拉、按下接地（读到 0），极性 1 把它归一化成「按下 = 1」；rev0 板极性为 0。极性由 `revision` 命令写入 config（[main.c:797-817](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L797-L817)），上拉本身在 [main.c:961-965](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L961-L965) 启动时按极性配置（GPIOB 的 6 个引脚一起配，覆盖了编码器的 PB1/PB2）。

**计数排空**：[ui.c:136-146](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L136-L146)——中断上下文只对 `enc_count` 做 ±1；线程侧 `fetch_encoder_tick()` 读出并清零。32 位对齐的 int 在 Cortex-M4 上读写本身是原子的，最坏情况只在「读」与「清零」两语句之间到达的边沿被丢掉一格——对旋钮完全可接受（更严格的并发分析见 u5-l1）。

#### 4.1.4 代码实践

**实践目标**：在真实硬件上直观看到「中断累积计数」与「轮询事件」的分工。

**操作步骤**（有硬件时）：

1. 用 USB 连上接收机，打开虚拟串口（`python/centsdr.py` 或任意终端）；
2. 执行 shell 命令 `uitest`（[main.c:846-860](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L846-L860)）：固件会每 100ms 打印一次全局 `enc_count`，共 100 次；
3. 在打印期间向一个方向慢慢旋转编码器一格，再快速旋转多格；
4. 按下旋钮不放，观察屏幕高亮档位是否不变（按键本身不切档，切档靠单击）。

**需要观察的现象**：慢转一格 `enc_count` 增减的绝对值；快转多格时数值跳变的幅度；打印间隔约 100ms（`uitest` 自己的节拍，与 Thread2 的 10ms 不同）。

**预期结果**：`enc_count` 随旋转方向增减；一格对应的计数增量取决于编码器每周期触点数与解码方式（这正是 4.3 节实践要定量测量的）。注意 `enc_count` 是「累积器」，旋钮闲置时数值应保持不变。

无硬件时做**源码阅读型实践**：画出从「PB1 引脚电平变化」到「屏幕上频率数字高亮位改变」的完整调用链，标注每一步运行在哪个上下文（ISR / Thread2 / 显示线程）。

#### 4.1.5 小练习与答案

**练习 1**：为什么编码器用中断而按键用轮询？反过来行不行？

**答案**：编码器边沿最短只有微秒级且连续到来，10ms 轮询会漏掉大量边沿导致计数偏少甚至方向判错，所以必须每个边沿进中断；按键的电平变化持续几十毫秒以上，10ms 轮询足够采样，而按键判定要调用 `save_config_current_channel()` 这类耗时操作（内部有 Flash 擦写），必须留在线程上下文。反过来（编码器轮询、按键中断）前者会漏计，后者虽然可行但会白白多一路 EXTI 配置，且长按/双击判定照样需要时间基准，代码不会更简单。

**练习 2**：Thread2 里 `disp_process()` 为什么排在 `ui_process()` 前面？交换顺序有什么影响？

**答案**：这样安排使得 `ui_process()` 新设置的脏标志（如 `disp_update()` 置位的 FLAG_UI）在**下一个** 10ms 周期才被绘制，每次用户输入的视觉反馈固定延迟一个轮询周期。交换后 `ui_process()` 产生的刷新请求能在同一周期内立即画出，响应快 10ms——两种写法都正确，差别只在固定的反馈延迟，代价是 `ui_process()` 后线程要多等一个完整周期才睡眠，略微拉长单次循环耗时。

### 4.2 btn_check()：消抖、双击窗口、长按与事件抑制

#### 4.2.1 概念说明

`btn_check()` 是一个**每 10ms 被调用一次**的纯函数式状态机（带两个静态状态：上次电平 `last_button`、上次按下时刻 `last_button_down_ticks`，外加一个抑制标志）。它把原始电平翻译成事件位图。设计上有三个关键常数（1ms/50ms/1.6s）和一个「事件抑制」机制：

- **消抖**：电平变化必须距「上次被承认的按下」至少 1ms；
- **双击窗**：按下时如果距上次被承认的按下不足 50ms，判为双击（且**不刷新**按下时刻基准）；
- **长按**：持续按住超过 1.6s 触发一次长按事件，随即置抑制标志，之后直到释放都不会再产生任何事件；
- **抑制**：`inhibit_button_event()` 供外部（`ui_process` 的按住旋转分支）调用——「按住旋转调档」这个手势结束时松开的手指不应再被当成一次单击。

#### 4.2.2 核心流程

调用一次 `btn_check()` 的判定流程：

```text
读当前电平 cur（归一化：1=按下）
changed = last_button XOR cur
├─ 按键位有变化：
│   ├─ 距上次承认的按下 < 1ms  → 忽略（消抖）
│   ├─ 变为释放：
│   │   ├─ 抑制标志=1 → 只清抑制标志，不发事件
│   │   └─ 否则        → 发 EVT_BUTTON_SINGLE_CLICK
│   └─ 变为按下：
│       ├─ 距上次按下 < 50ms → 发 EVT_BUTTON_DOUBLE_CLICK（不更新基准时刻）
│       └─ 否则               → 更新 last_button_down_ticks（长按计时起点）
└─ 按键位无变化：
    └─ 按住中 && 未抑制 && 距按下 ≥ 1.6s
        → 发 EVT_BUTTON_DOWN_LONG，并置抑制标志（防重复、吞掉随后的释放点击）
last_button = cur
返回事件位图
```

三个典型手势的时序展开（时间单位 tick，1 tick = 100µs）：

| 手势 | 事件序列 |
|---|---|
| 干净单击（按 100ms 松） | 释放时 1× SINGLE |
| 双击（按 30ms 松、再按 30ms 松，间隔 < 50ms） | 第 1 次释放 1× SINGLE → 第 2 次按下 1× DOUBLE → 第 2 次释放 1× SINGLE |
| 长按（按住 2s） | 1.6s 时刻 1× DOWN_LONG，释放时**无事件** |
| 按住 + 旋转（调档后松开） | 外部已调 `inhibit_button_event()`，释放时**无事件** |

注意双击那一行：第二次按下产生 DOUBLE，但**两次释放各自都会产生 SINGLE**——这是理解 4.2.5 练习 2 的关键。

#### 4.2.3 源码精读

[ui.c:68-105](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L68-L105) 是完整函数。逐段看：

- [ui.c:70-73](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L70-L73)：读电平、算异或得到「变化的位」、取系统时间。`chVTGetSystemTime()` 返回 tick 数。
- [ui.c:74-75](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L74-L75)：**消抖门**——只有距 `last_button_down_ticks` 超过 10 tick 的变化才被承认。注意基准是「上次被承认的按下」而非「上次任意变化」。
- [ui.c:76-82](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L76-L82)：**释放路径**——如果被抑制则只清标志；否则发单击事件。这就是「按住旋转调档后松开不会误切档」的实现。
- [ui.c:83-90](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L83-L90)：**按下路径**——距上次按下小于 500 tick 判双击（且不更新基准时刻，保证第三次快速按下仍以第一次为基准）；否则记录新的按下时刻，作为长按计时的起点。
- [ui.c:92-100](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L92-L100)：**电平不变路径**——持续按住且未抑制且超过 16000 tick，发一次长按事件并立刻置抑制。置抑制有两重效果：长按期间不会每 10ms 重复发一次；随后松开也不再发单击。
- [ui.c:62-66](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L62-L66)：`inhibit_button_event()` 就是置位那个标志，供 `ui_process` 在「按住旋转」分支末尾调用（[ui.c:306](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L306)）。

一个值得注意的组合行为：**先按住旋转、再超时**——旋转分支先置了抑制，之后即使按满 1.6s，[ui.c:95](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L95) 的 `!button_event_inhibited` 条件不成立，长按保存**不会**触发。也就是说「调档」手势会主动屏蔽「保存」手势，两个长按类操作互不干扰。

#### 4.2.4 代码实践

**实践目标**：把 `btn_check()` 提取到 PC 上，用一个虚拟时钟和脚本化电平序列驱动它，验证三件事：干净单击只发一次 SINGLE、长按只发一次 LONG 且不发 SINGLE、抑制标志在释放后必然复位。

**操作步骤**：

1. 新建 `btn_sim.c`（示例代码，逻辑照抄 [ui.c:68-105](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L68-L105)，仅把 `chVTGetSystemTime()` 换成虚拟时钟、`read_buttons()` 换成全局电平变量）：

```c
/* 示例代码：btn_check 的宿主端模拟器核心 */
#include <stdio.h>
#include <stdlib.h>

#define EVT_SINGLE   0x01
#define EVT_DOUBLE   0x02
#define EVT_LONG     0x04
#define LONG_TICKS   16000
#define DOUBLE_TICKS   500
#define DEBOUNCE_TICKS  10
#define BIT_PUSH        0

static unsigned last_button, inhibited, level;
static unsigned long last_down, vticks;

static int btn_check(void)            /* 与 ui.c:68-105 等价 */
{
    int cur = level ? 1 : 0;
    int changed = last_button ^ cur;
    int status = 0;
    unsigned long ticks = vticks;
    if (changed & (1 << BIT_PUSH)) {
        if (ticks >= last_down + DEBOUNCE_TICKS) {
            if (!cur) {
                if (inhibited) inhibited = 0;
                else status |= EVT_SINGLE;
            } else {
                if (ticks < last_down + DOUBLE_TICKS)
                    status |= EVT_DOUBLE;
                else
                    last_down = ticks;
            }
        }
    } else {
        if (cur && !inhibited && ticks >= last_down + LONG_TICKS) {
            status |= EVT_LONG;
            inhibited = 1;
        }
    }
    last_button = cur;
    return status;
}

static long n_single, n_double, n_long;
static void settle(unsigned long dt)  /* 以 100tick(=10ms) 步进推进虚拟时钟 */
{
    for (; dt >= 100; dt -= 100) {
        vticks += 100;
        int ev = btn_check();
        n_single += !!(ev & EVT_SINGLE);
        n_double += !!(ev & EVT_DOUBLE);
        n_long    += !!(ev & EVT_LONG);
    }
    vticks += dt;
}
```

2. 再写三个脚本化场景（都从 `vticks` 足够大、`level=0` 开始）：

```c
/* 场景 A：干净单击 —— 按下 100ms 后释放 */
level = 1; settle(100); level = 0; settle(1000);
/* 断言：n_single 增量 == 1，n_long 增量 == 0 */

/* 场景 B：长按 —— 按住 2s 后释放 */
level = 1; settle(2000 * 10); level = 0; settle(100);
/* 断言：n_long 增量 == 1，n_single 增量 == 0，inhibited == 0 */

/* 场景 C：按住旋转 —— 按下后立刻抑制，再释放 */
level = 1; settle(100); inhibited = 1; level = 0; settle(100);
/* 断言：n_single 增量 == 0，inhibited == 0 */
```

3. 编译运行：`gcc -O2 btn_sim.c -o btn_sim && ./btn_sim`，用 `printf` 打印各场景的事件计数和 `inhibited` 终值，与断言比对。

**需要观察的现象**：三个场景的事件计数是否符合断言；场景 B 中若把按住时长改成恰好 16000 tick 附近的边界值（例如 15900、16000、16100），事件是否出现/缺失。

**预期结果**：A→1 次 SINGLE；B→恰 1 次 LONG、0 次 SINGLE、抑制标志复位为 0；C→0 次事件且标志复位。所有场景函数都正常返回（每次调用的开销是常数，结构上不存在死循环）。边界实验中，由于 `settle` 以 100 tick 为步进采样，恰好压在 16000 上的行为取决于最后一次采样时刻，这正好演示了轮询采样的量化效应。此实践为纯 PC 端模拟，结果可直接复现，无需硬件。

#### 4.2.5 小练习与答案

**练习 1**：消抖基准是「上次被承认的按下」，不是「上次任意电平变化」。由此推断：释放之后 2ms 内的触点回弹（快速断-通-断）会发生什么？

**答案**：回弹的「再接通」距上次被承认的按下（比如 200ms 前）远超 1ms 消抖门，会被**承认**为一次新的按下；而如果它又落在 50ms 双击窗内（即整个按压历时不足 50ms），还会触发 DOUBLE 事件，随后的「再释放」又发一次 SINGLE。也就是说这套消抖主要防的是**按下瞬间**的毛刺（被 10ms 轮询周期天然滤掉）与紧随按下的小间隔变化，对**释放沿**的慢速回弹防护较弱——实际能正常工作，依赖的是所用编码器开关释放沿抖动远小于 50ms 双击窗且多数抖动快于 10ms 采样周期。这也解释了为什么双击窗不能设得太大：越大，误判双击的风险越高。

**练习 2**：`EVT_BUTTON_DOUBLE_CLICK` 在整个固件里没有任何消费者（`ui_process` 只处理 SINGLE 和 LONG，见下文 4.4.3）。为什么作者实现了双击判定却不用它？

**答案**：从 4.2.2 的时序表可见，双击手势的第一次释放**先于**第二次按下发生，即 SINGLE 事件先于 DOUBLE 出现。若想让「双击」有独立语义，必须把单击的确认**延迟**到双击窗（50ms）超时之后——要么引入延迟队列，要么接受「双击 = 两次单击 + 一次双击」的复合事件。前者增加延迟和复杂度，后者语义混乱。于是作者选择：只用单击和长按，双击判定留在代码里但不接线。这是嵌入式 UI 里非常典型的取舍（宁可手势少，不要手势歧义）。

**练习 3**：把 `BUTTON_DOWN_LONG_TICKS` 从 16000 改成 8000（0.8s），对现有交互有什么副作用？

**答案**：长按更快触发，保存信道更顺手；但「按住旋转调档」的手势更容易超过 0.8s——虽然旋转分支已置抑制、不会触发保存（见 4.2.3 末尾的分析），可是一旦用户按住不动超过 0.8s 想先看清档位再转，就会先触发一次保存（蜂鸣 + Flash 擦写）。Flash 擦写期间线程会被阻塞（见 u4-l5），可能造成旋转计数丢失或界面卡顿。所以长按阈值实际是「保存误触发概率」与「操作费力程度」的折中。

### 4.3 编码器解码：EXT 双边沿中断、事件编码与 trans_tbl 状态转移表

#### 4.3.1 概念说明

正交编码器的解码要回答两个问题：**转了几格**（计数）和**往哪边转**（方向）。方向无法从单个边沿判断——必须知道「这条边沿来临之前两线处于什么相位」。因此需要一个 4 状态的记忆（A/B 电平组合在旋转周期里的四个稳定相位），每来一个边沿查表转移，并在特定转移上计数。

CentSDR 的做法：

1. EXTI 通道 1、2（PB1、PB2）配置为**双边沿触发**——每个机械周期 4 个边沿全部产生中断（四倍频采样）；
2. 中断里把「哪根线 + 上升还是下降」编码成事件号 s；
3. 以 s 为行、旧状态为列查 `trans_tbl` 得到新状态；
4. 只在两个特定组合上计数：状态 0 遇 B 上升 → `enc_count--`；状态 3 遇 B 下降 → `enc_count++`。

#### 4.3.2 核心流程

事件编码（[ui.c:156-158](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L156-L158)）：

\[ s = 2\,(\text{channel} - 1) + \text{level} \]

| s | 含义 |
|---|---|
| 0 | A 线下降沿 |
| 1 | A 线上升沿 |
| 2 | B 线下降沿 |
| 3 | B 线上升沿 |

中断处理伪代码：

```text
ext_callback(channel):                      # channel ∈ {1, 2}
    cur  = 读 GPIOB 整个端口
    s    = (channel-1)*2                    # A 相基值 0，B 相基值 2
    s   |= (cur >> channel) & 1             # 该引脚当前电平：1=上升沿
    若 enc_status==0 且 s==3 (B上升):  enc_count -= 1
    若 enc_status==3 且 s==2 (B下降):  enc_count += 1
    enc_status = trans_tbl[s][enc_status]   # 查表转移到新状态
```

注意计数判断用的是**转移前**的状态：B 的边沿只有「从正确的相位赶来」才计数，A 的边沿永远只推进状态、不计数。这就是用状态表做方向判决、再选择性计数的方式。

#### 4.3.3 源码精读

**EXT 配置**：[ui.c:171-206](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L171-L206) 的 `extconf` 是 ChibiOS EXT 驱动的通道表，**数组下标即 EXTI 通道号（= 引脚号）**。[ui.c:174-175](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L174-L175) 启用了下标 1、2 两项：端口 GPIOB、`EXT_CH_MODE_BOTH_EDGES`（双边沿）、`AUTOSTART`。所以编码器 A 接 PB1、B 接 PB2。通道 0 的注释残迹（[ui.c:173](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L173)）显示作者试过把按键也挂到 EXTI，后来改成轮询。驱动在 `ui_init()` 里启动：[ui.c:221-227](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L221-L227)。

**中断回调**：[ui.c:148-169](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L148-L169)。

- [ui.c:151](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L151)：一次 `palReadPort(GPIOB)` 读整个端口，再用 `(1 << channel)` 取出**本引脚**当前电平——中断响应时边沿已经完成，读到的是新电平（1 = 刚上升）。
- [ui.c:152-155](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L152-L155)：4×4 转移表。**代码按 `[s][enc_status]`（事件为行、旧状态为列）索引**；而源码里那行注释「falling A / rising A / falling B / rising B」排布在初始化数据上方，读起来更像在标注列——行/列语义与注释的对应关系本身就是一道好的阅读题（见练习 1）。表里只有一部分单元格对应理想正交序列的合法转移，其余是非法转移时的兜底值，这正是查表法的好处：非法输入不会越界、不会卡死，只是落到某个状态继续运行。
- [ui.c:159-162](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L159-L162)：两个计数守卫（转移前状态 + B 边沿）。
- [ui.c:164-168](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L164-L168)：`#if 0` 包起来的一段调试残迹（通道 0 事件时清零计数），被注释掉的 `dragged` 变量（[ui.c:53](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L53)）同样说明这套交互迭代过多轮。

**诚实地说**：仅凭源码无法完全还原这张表在真实编码器上的确切行为——它依赖具体型号的触点相位与接线方向，作者以「在硬件上工作正常」为准绳标定。但这不影响读懂机制，反而正适合做成模拟实践（下节）。

#### 4.3.4 代码实践

**实践目标**：在 PC 上用理想正交信号驱动 `ext_callback` 的逻辑，统计不同方向、不同相位假设下「每个机械周期产生多少个 ±1 计数」，体会查表解码对非法转移的容错。

**操作步骤**：

1. 把 [ui.c:148-169](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L148-L169) 提取成宿主函数（示例代码），`palReadPort` 换成由你维护的 `A`、`B` 两个电平变量拼出的「端口值」：

```c
/* 示例代码：ext_callback 逻辑的宿主提取版 */
static int enc_status, enc_count;
static int A, B;                        /* 模拟两线电平 */

static const int trans_tbl[4][4] = {    /* 照抄 ui.c:152-155 */
  { 0, 0, 3, 3 }, { 1, 1, 2, 2 }, { 0, 1, 1, 0 }, { 3, 2, 2, 3 }
};

static void edge(int channel)           /* channel: 1=A 线, 2=B 线 */
{
    int cur = (channel == 1 ? A : B) << channel;
    int s = (channel - 1) * 2;
    if (cur & (1 << channel)) s |= 1;
    if (enc_status == 0 && s == 3) enc_count--;
    if (enc_status == 3 && s == 2) enc_count++;
    enc_status = trans_tbl[s][enc_status];
}
```

2. 生成一个方向的理想边沿序列（A 超前 B 90°）：`edge(A↑), edge(B↑), edge(A↓), edge(B↓)` 为一个周期，循环 1000 次；再生成反方向序列（B 超前 A）循环 1000 次；
3. 打印每个方向的 `enc_count` 总量，换算「每周期平均计数」；
4. 再故意插入乱序边沿（模拟触点抖动/换向），观察计数是否发散或状态是否进入无法计数的轨道。

**需要观察的现象**：两个方向的每周期平均计数及其符号；插入非法边沿后状态轨迹的恢复情况。

**预期结果**：待本地验证——按理想 90° 相位手工推演这张表会得到与直觉不同的每周期计数（不是教科书式的 x1/x2/x4 整数），因为表中多数单元格是为非法转移准备的兜底值，状态轨迹未必与理想四相位一一对应。真实设备上的每格计数可以用 `uitest` 命令实测（见 4.1.4）。这个实践的价值正在于：**查表式解码器的行为必须靠仿真/实测确认，而不能靠「应该是四倍频」的直觉假设**。

#### 4.3.5 小练习与答案

**练习 1**：`trans_tbl` 上方的注释写着一排「falling A / rising A / falling B / rising B」，而代码用 `trans_tbl[s][enc_status]` 索引。请说明两种读法（注释标注行 vs 标注列）分别对应什么语义。

**答案**：若注释标注的是**列**，则表的语义是「旧状态 × 事件 → 新状态」，代码应写成 `trans_tbl[enc_status][s]`；若标注的是**行**，则与代码的 `[s][enc_status]` 一致，语义是「事件 × 旧状态 → 新状态」。实际代码按后者工作。C 语言二维数组按行展开，注释与索引方式错位是固件代码里常见的可读性陷阱——结论：**以代码为准**，注释只反映作者写作时的心智模型。修改这张表前，先写 4.3.4 的模拟器验证，比读注释可靠。

**练习 2**：为什么 `ext_callback` 里不直接做 `enc_count += 方向`，而要先查表维护 `enc_status`？

**答案**：单个边沿不携带方向信息。若只看「B 上升就加」，就无法区分 B 上升是顺时针行程的一部分还是逆时针行程的一部分；如果编码器在两个相位之间来回抖动（手停在格点上），无状态的计数会疯狂累积噪声。4 状态的相位记忆使得只有「从正确相位出发的 B 边沿」才计数，抖动只在相邻状态间来回、不会满足计数守卫，天然抗抖。这也是为什么状态变量 `enc_status` 必须是跨中断存活的静态全局（[ui.c:136](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L136)）。

**练习 3**：如何把旋钮方向反过来（顺时针变减）？给出至少两种方案。

**答案**：① 硬件：把 PB1/PB2 两根线对调（A、B 互换后所有边沿的 channel 编号对调，方向判决随之反转）；② 软件：把两个计数守卫的 `++`/`--` 对调（[ui.c:159-162](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L159-L162)），或在 `fetch_encoder_tick()` 返回前取负；③ 软件：交换 `extconf` 中通道 1、2 的配置对应的回调参数解释（把 `s` 的基值对调）。最省事的是 ②，一行改动、不动接线。

### 4.4 ui_process() 档位状态机：单击循环、按住调档、松开调值

#### 4.4.1 概念说明

`uistat.mode` 是一个枚举档位（[nanosdr.h:257-258](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L257-L258)），它**不表示接收机的工作状态**（那是 `uistat.modulation`、`uistat.agcmode` 等），而是表示「旋钮此刻正在操纵哪一个参数」：

| mode 值 | 枚举 | 松开旋转时调节的量 | 范围与落地动作 |
|---|---|---|---|
| 0 | CHANNEL | 信道号 | 0~99，调完 `recall_channel()` 召回频率+调制 |
| 1 | FREQ | 频率 | 按 `digit` 位的 10^digit Hz 步进，调完 `set_tune()` |
| 2 | VOLUME | 音量 | −7~29，`tlv320aic3204_set_volume()` |
| 3 | MOD | 调制模式 | MOD_CW…MOD_FM_STEREO，`set_modulation()` + 重调本振 |
| 4 | AGC | AGC 档 | MANUAL/SLOW/MID/FAST，`set_agc_mode()` |
| 5 | RFGAIN | 前端增益 | −24~135（PGA 0~95 + 数字增益），`set_gain()` |
| 6 | AGC_MAXGAIN | AGC 最大增益 | 0~127，写入 `config.agc.maximum_gain` |
| 7 | CWTONE | CW 侧音 | −2000~1999 Hz，`update_cwtone()` 重算相位步进 |
| 8 | IQBAL | IQ 平衡 | −4000~3999（步进 ×10），`update_iqbal()` 写 codec mini-DSP |
| 9 | SPDISP | 频谱采样点 | CAP/CAP2/IF/AUD 四选一（见 u4-l1） |
| 10 | WFDISP | 下方显示方式 | WATERFALL/WAVEFORM/MAG/MAG2（见 u4-l2） |

交互模型一句话：**单击切档（沿「常用档循环」）、按住旋转也切档（可进入全部档位）、松开旋转调值、长按保存**。档位还会被 `disp_process()` 用来决定屏幕上哪一行高亮（[display.c:1427-1438](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1427-L1438)：AGC_MAXGAIN/CWTONE/IQBAL 三档改画 `draw_aux_info()` 辅助带，其余画状态栏），这就是「编辑焦点」的视觉呈现（u4-l3 已详述）。

#### 4.4.2 核心流程

`ui_process()`（[ui.c:246-355](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L246-L355)）每次被轮询到时执行：

```text
status = btn_check()                 # 按键事件
tick   = fetch_encoder_tick()        # 本周期旋转格数（带符号）

若 status 有 SINGLE_CLICK:
    mode+1；[AGC 开启则跳过 RFGAIN]；[落到 AGC_MAXGAIN 则直接跳到 SPDISP]
    （即单击只在「常用档」间循环，辅助档 AGC_MAXGAIN/CWTONE/IQBAL 不在循环里）
    mode %= 11；disp_update()
否则若 status 有 DOWN_LONG:
    蜂鸣一声；save_config_current_channel()   # 保存信道 + 整个 uistat 到 Flash

若 tick != 0（本周期转过旋钮）:
    若按住旋转（read_buttons() != 0）:         # —— 调档 ——
        若 mode==FREQ: 调 digit（0=1Hz 位 … 7=10MHz 位），到边界则顺势离开 FREQ 档
        否则: mode±1（方向由 tick 符号决定）
              [AGC 开启则跳过 RFGAIN]
              [进入/离开辅助带档位时 disp_clear_aux() 请求清屏]
        mode %= 11
        disp_update()；inhibit_button_event()  # 松开时不再误发单击
    否则（松开旋转）:                            # —— 调值 ——
        按 mode 分发到上表对应的调节动作（minmax 钳位 → 调底层 → 更新 uistat）
    disp_update()
```

单击循环的两条轨道（由 `agcmode` 是否为 MANUAL 决定）：

```text
AGC 开启（agcmode != MANUAL）:
  CHANNEL → FREQ → VOLUME → MOD → AGC ──(跳过 RFGAIN)──> SPDISP → WFDISP ─┐
  ↑────────────────────────────────────────────────────────────────────────┘
AGC 手动（agcmode == MANUAL）:
  CHANNEL → FREQ → VOLUME → MOD → AGC → RFGAIN ──(跳过 AGC_MAXGAIN 及两个辅助档)──> SPDISP → WFDISP
```

按住旋转则可到达**全部** 11 档（含 AGC_MAXGAIN/CWTONE/IQBAL），且同样遵守 AGC 跳过规则。

**AGC 跳过 RFGAIN 的特例**：AGC 工作时 MICPGA 增益由芯片自动控制，手动 RFGAIN 档失去意义，所以 [ui.c:254-255](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L254-L255)（单击）、[ui.c:289-291](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L289-L291)（按住旋转，向下）、[ui.c:296-298](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L296-L298)（按住旋转，向上）三处都在落地到 RFGAIN 时再 ±1 跳过去。功率显示也会相应切换数据来源：AGC 开启时功率补偿用读回的 AGC 增益（[main.c:384-386](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L384-L386)）。

#### 4.4.3 源码精读

- [ui.c:249-250](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L249-L250)：一次取齐两类输入。事件优先级：SINGLE 分支用 `if`、LONG 分支用 `else if`——一次调用里两者不可能同时置位（一次采样只走一条路径），写法只是防御。
- [ui.c:252-260](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L252-L260)：单击切档。注意 `uistat.mode++` 作用于 int 型枚举，最后的 `%= MODE_MAX` 只做**上回绕**。
- [ui.c:261-264](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L261-L264)：长按保存。`tlv320aic3204_beep()` 先给一声听觉反馈（Flash 擦写会卡顿，先响铃告知用户「收到了」），再调 [main.c:247-256](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L247-L256) 的 `save_config_current_channel()`：把当前频率/调制写进 `config.channels[uistat.channel]`，把整个 `uistat` 快照进 `config.uistat`，最后 `config_save()` 落 Flash（u4-l5 的主题）。
- [ui.c:265-281](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L265-L281)：**FREQ 档按住旋转**调的是「编辑位」`digit`：`tick < 0` 把编辑位向高位移（步进变粗，digit=0 是 1Hz、digit=7 是 10MHz，屏幕上当前位反色高亮，见 [display.c:1153](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1153)）；移到边界（0 或 7）再继续转就顺势 `mode±1` 离开 FREQ 档——档位切换和位选择共用同一个旋转维度，无键切换。
- [ui.c:282-304](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L282-L304)：**其余档按住旋转**切档。`disp_clear_aux()`（定义在 [display.c:1463-1466](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1463-L1466)，置 FLAG_AUX_INFO 让显示线程清掉辅助带）在四个特定落点上触发：向下进入 IQBAL/RFGAIN、向上进入 AGC_MAXGAIN/SPDISP——正好是「进入或离开辅助信息带」的四个方向边界，避免新旧两套文本叠加（u4-l3 的像素所有权问题）。
- [ui.c:305-306](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L305-L306)：调档后立即请求刷屏，并调 `inhibit_button_event()` 吞掉松开时的单击。
- [ui.c:307-351](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L307-L351)：**松开旋转调值**，一档一个 `else if`。所有数值都用 `minmax()`（[ui.c:237-244](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L237-L244)）钳位——注意它的语义是「`x >= max` 返回 `max-1`」，所以调用处传的 `max` 都是**开区间上界**（例如音量传 `VOLUME_MAX+1`，[ui.c:312](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L312)）。两个细节：RFGAIN 档的 `set_gain()`（[ui.c:111-126](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L111-L126)）把超过 95 的部分折算成数字增益、负值也走数字增益（呼应 u2-l2 的增益链）；MOD 档切换后要补一次 `update_frequency()`（[ui.c:336](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L336)），因为新模式的 `mode_freq_offset` 变了（AM/CW 有 10kHz 低中频偏移），本振必须重算（呼应 u2-l1）。
- [ui.c:353](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L353)：无论调了什么，最后统一 `disp_update()` 置 FLAG_UI。

**两个由代码可直接推出的边界行为**（值得记住）：

1. `mode--` 在 CHANNEL(0) 处**不回绕**：得到 −1，而 C99 里 `-1 % 11 == -1`，`uistat.mode` 变成 −1。此时松开旋转哪个分支都不匹配（什么都不调），按住旋转再向下还会继续 −2、−3……只有反向旋转或单击才会回到合法档位。也就是说「向下」方向的档位链在 CHANNEL 处断头，不构成环。
2. 从辅助档（如 IQBAL）**单击**离开时没有调 `disp_clear_aux()`（[ui.c:252-260](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L252-L260) 无此调用）——是否在屏幕上留下残影取决于 `draw_info()` 是否完整覆盖辅助带，待本地验证（见 4.4.5 练习 3）。

#### 4.4.4 代码实践

**实践目标**：为「频谱显示档 ↔ 瀑布/波形档」设计一个快捷手势并写出可落地的状态跳转伪代码，同时不破坏「长按 = 保存」的既有语义。

**操作步骤**：

1. 分析约束：SPDISP(9) 与 WFDISP(10) 本就相邻，单击循环里也是挨着的，所以新手势的价值在于**少转几格 + 不动档位序列**。可用的空闲事件只有 `EVT_BUTTON_DOUBLE_CLICK`（4.2.5 练习 2 已分析它的 SINGLE 先行问题），最稳妥的是改造长按分支做「按档位分发」。
2. 写出伪代码（示例设计：长按在 SPDISP/WFDISP 两档间互切，其余档保持保存语义）：

```text
# 替换 ui.c:261-264 的长按分支
若 status 有 EVT_BUTTON_DOWN_LONG:
    若 uistat.mode == SPDISP:
        uistat.mode = WFDISP          # 就地跳档
        disp_update()
    否则若 uistat.mode == WFDISP:
        uistat.mode = SPDISP
        disp_update()
    否则:
        tlv320aic3204_beep()
        save_config_current_channel() # 原语义保留
```

3. 若你想改用双击实现，先回答：双击的两次释放各会产生一次 SINGLE_CLICK，单击分支会把 mode 推两格，如何补偿？（参考方案：在双击触发时把 mode 回退两格，或给 btn_check 增加双击后抑制后续 SINGLE 的逻辑——这正是它至今未被使用的原因。）
4. （有硬件时）把伪代码落成 C，重新编译烧录（u1-l2 的流程），在 SPDISP 档长按验证直接跳到 WFDISP、再长按跳回；在 FREQ 档长按验证仍然保存（蜂鸣后用 shell 的 `channel list` 确认信道内容已更新）。

**需要观察的现象**：长按 1.6s 后档位切换是否只发生一次（抑制标志保证不重复触发）；保存档位长按时蜂鸣与 `channel list` 结果；切换后下方显示区域的渲染模式是否随之改变。

**预期结果**：待本地验证（涉及硬件烧录）。纯设计部分（伪代码与手势冲突分析）可在纸面完成。

#### 4.4.5 小练习与答案

**练习 1**：AGC 开启（`agcmode != AGC_MANUAL`）时，用户正在 RFGAIN 档调节，此时通过 shell 命令 `agc slow` 打开 AGC——之后旋转旋钮会发生什么？

**答案**：`set_agc_mode()`（[main.c:630-655](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L630-L655)）只改 `uistat.agcmode` 并刷新显示，**不检查也不搬移当前的 `uistat.mode`**。于是用户仍停留在 RFGAIN 档，但下一次按住旋转时：向上 `mode++` 落到 RFGAIN 会再 +1 跳到 AGC_MAXGAIN，向下 `mode--` 落到 RFGAIN（从 AGC_MAXGAIN 出发时）会再 −1 跳到 AGC——两条路都会立刻离开 RFGAIN。而若停留在 RFGAIN 档不动、只松开旋转，仍会继续手动调增益（调节分支本身没有 AGC 检查），与 AGC 自动控制相互打架，直到 AGC 的下一次衰减把它压回去。这说明「跳过规则」只写在**导航**代码里，没写在**调节**代码里——一个典型的状态不一致窗口。

**练习 2**：`uistat.mode` 会被长按保存进 Flash（`config.uistat = uistat`）。结合 4.4.3 的边界行为 1，这可能造成什么后果？

**答案**：若用户在 CHANNEL 档向下旋转使 mode 变成 −1，随后长按保存，`uistat.mode = -1` 被写入 Flash；下次开机 `config_recall()` 恢复后整机就停在这个「死档」——所有松开旋转的调节分支都不匹配，屏幕上也没有高亮行，用户必须单击（−1+1=0 回到 CHANNEL）或反向旋转才能恢复。修复思路：在 `mode--` 后加下回绕（`if (mode < 0) mode = MODE_MAX - 1;`），或在保存前对 mode 做合法性检查。

**练习 3**：从 IQBAL 档直接单击离开（不经过按住旋转），辅助信息带的像素是否会被正确清掉？给出验证方法。

**答案**：代码路径上单击分支（[ui.c:252-260](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L252-L260)）不调 `disp_clear_aux()`，而按住旋转的对应路径调了——所以**不保证**被清掉，取决于 `draw_info()` 是否完整重绘同一像素带。验证方法：有硬件时进入 IQBAL 档（按住旋转向下走到 8），松开后单击一次切到 SPDISP，肉眼检查状态栏区域是否残留 AGCMAX/CWTONE/IQBAL 文本；或对比两条路径的 `draw_info()`/`clear_aux_info()` 覆盖坐标（[display.c:1386-1389](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1386-L1389) 清的是 (0,48) 起 184×24 的矩形）。若确有残影，修复即在单击分支的相应落点补一次 `disp_clear_aux()`。待本地验证。

### 4.5 长按保存与信道召回：一个完整的交互闭环

#### 4.5.1 概念说明

档位状态机之外，UI 还承担「记忆」：100 个信道（`config.channels[]`，每项存频率 + 调制模式，[nanosdr.h:282-287](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L282-L287)）加一份完整 `uistat` 快照，全部住在 Flash 末页（u4-l5 详述存储格式）。UI 侧的闭环是：

- **召回**：CHANNEL 档松开旋转 → `recall_channel()` 把信道的频率/调制灌回 `uistat` 并作用于硬件；
- **保存**：任意档长按 → `save_config_current_channel()` 把当前频率/调制写入当前信道号，并快照整个 `uistat`，落 Flash。

#### 4.5.2 核心流程

```text
CHANNEL 档松开旋转:
    channel = minmax(channel + tick, 0, 100)        # 有效 0~99
    recall_channel(channel):
        uistat.freq         <- config.channels[ch].freq
        uistat.modulation   <- config.channels[ch].modulation
        set_modulation(...)                          # 换解调函数指针 + 采样率 + 本振偏移
        update_frequency()                           # set_tune -> si5351

任意档长按 (>=1.6s):
    beep()                                          # 听觉确认
    save_config_current_channel():
        config.channels[uistat.channel].freq        <- uistat.freq
        config.channels[uistat.channel].modulation  <- uistat.modulation
        config.uistat                                <- uistat    # 整机状态快照（含 mode）
        config_save()                                # 擦页 + 编程 + 校验和（u4-l5）
```

#### 4.5.3 源码精读

- [ui.c:208-219](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L208-L219)：`recall_channel()`。注意被注释掉的 `rfgain` 召回（[ui.c:213](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L213)、[ui.c:216](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L216)）——信道只记频率和调制，不记增益，作者取舍过。`set_modulation()` 内部（[main.c:179-194](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L179-L194)）会按 `mod_table` 一并切采样率与解调函数指针，所以召回一个 FM 立体声信道会自动切到 192kHz。
- [main.c:247-256](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L247-L256)：`save_config_current_channel()`，长按与 shell 命令 `channel save`（[main.c:763-775](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L763-L775)）共用这套写入逻辑（命令版不快照 uistat、不落 Flash——要再敲 `save` 才写 Flash，两者语义略有差别）。
- [main.c:120-163](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L120-L163)：出厂默认 config，注意 `.mode = CHANNEL`（[main.c:128](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L128)）和预置的 18 个短波/调频信道——首次上电（或校验失败回退时）就有可听的台。
- [ui.c:221-235](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L221-L235)：`ui_init()` 启动 EXT 驱动后，把 `uistat`（开机时已从 Flash 恢复）逐项作用到硬件：音量、增益、AGC、调制、频率。这是 u1-l3「首次用户交互前把状态落到硬件」的最后一棒。

#### 4.5.4 代码实践

**实践目标**：用 shell 命令复现并检验长按保存的语义，理解 UI 手势与 shell 命令是同一套底层函数的两个入口。

**操作步骤**（有硬件时）：

1. 连接 shell，`show all` 记录当前 frequency/mode/channel/agc；
2. 用 `tune 7100000` 设频率、`mode lsb` 切模式（注意 [main.c:83-96](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L83-L96) 的 `tune` 会顺手把 `uistat.mode` 设为 FREQ 并刷屏——shell 也在操纵 UI 状态）；
3. 物理长按旋钮 2 秒（听到蜂鸣）；
4. `channel list` 查看当前信道号的内容是否变成了 7100000/lsb；
5. 旋转到别的信道再转回来（CHANNEL 档松开旋转），确认频率模式被召回。

无硬件时做**源码阅读型实践**：列出「长按保存」和 `channel save`+`save` 两条路径各自最终修改了 `config_t` 的哪些字段、哪些字段只有其中一条路径会碰，写成一张对照表。

**需要观察的现象**：步骤 4 中信道内容更新；步骤 5 召回后采样率/解调是否随之切换（屏幕模式图标与音调变化）。

**预期结果**：待本地验证（需硬件）。源码对照表的预期结论：长按路径额外快照 `config.uistat`（含 mode、digit、spdispmode 等全部 UI 状态），`channel save` 只写信道两项——这就是「长按保存」能记住你离开时的操作档位的原因。

#### 4.5.5 小练习与答案

**练习 1**：为什么保存前要 `beep()`，而不是保存后？

**答案**：`config_save()` 涉及 Flash 页擦除与编程，期间该线程被阻塞（且 ChibiOS 里 Flash 操作通常在临界区内，见 u4-l5 的 `chSysLock`），UI 会卡顿几十毫秒。先蜂鸣能给用户即时的操作确认（「手势已被识别」），再去承担卡顿；若保存后才响，用户在按压的 1.6s+ 里得不到反馈，容易误以为没按到而反复长按，造成多次擦写。这是「先反馈、后慢操作」的通用交互原则。

**练习 2**：`recall_channel()` 不召回音量、AGC 档、显示方式——这些设置什么时候被恢复？

**答案**：开机时。`config_recall()` 从 Flash 恢复整个 `config`，`uistat = config.uistat`（[main.c:967-968](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L967-L968)），再由 `ui_init()` 统一作用到硬件。也就是说设计上把状态分成两层：**每信道**的（频率、调制）随信道召回，**每用户**的（音量、AGC、显示偏好、当前档位）随电源周期恢复。信道切换时你想保留什么、想跟随什么，这个分层就是答案。

## 5. 综合实践

把 4.2.4 的按键模拟器扩展成一个**带档位状态机的整机交互仿真**，验证你为 SPDISP/WFDISP 设计的新手势不会破坏既有语义：

1. **合并两个模型**：在 `btn_sim.c` 基础上加入简化版 `ui_process()`——只保留 `mode`（int，0~10）与 `agcmode`（0~3）两个状态变量，实现单击循环（含 AGC 跳过与 AGC_MAXGAIN→SPDISP 跳转）与你的新长按分支；`tick` 用随机 ±1 模拟旋转格数，`read_buttons()` 直接用模拟器的 `level`。
2. **随机压力测试**：跑 1000 个随机手势——每个手势随机选择（单击 / 长按 / 按住 + 随机 tick / 松开 + 随机 tick / 双击），随机持续 1~30000 tick。每次循环断言三条不变量：
   - `btn_check()` 都正常返回（循环有界，无死锁）；
   - 手势间隙（释放 + 空转 2000 tick）之后 `inhibited == 0`（抑制标志必然复位，不会永久吞掉按键）；
   - `mode` 要么在 0~10，要么是文档记录过的 −1 死档（向下越界），绝不会出现其他负数或 ≥11 的值——如果出现，说明你的跳转伪代码有漏 `% MODE_MAX` 的路径。
3. **定向场景回放**：脚本化回放「SPDISP 档长按」50 次，统计 `mode` 落点是否 100% 是 WFDISP 且保存动作（打印一条 `SAVE` 日志模拟）零触发；再回放「FREQ 档长按」50 次，断言保存动作 100% 触发、档位不变。
4. **（有硬件，选做）**：把新手势编译烧录进固件，重复步骤 4 的两个定向场景，对照模拟器的统计。

**预期结果**：步骤 2 的三条不变量全部成立；步骤 3 的统计为 100%/0% 与 0%/100%。硬件部分待本地验证。

## 6. 本讲小结

- **事件管线**：编码器 A/B 双边沿走 EXT 中断（四倍频采样），`enc_count` 在 ISR 里累积、Thread2 每 10ms 用 `fetch_encoder_tick()` 排空；按键电平由同一线程轮询 `btn_check()` 判定——快信号中断、慢逻辑轮询。
- **按键状态机**：1ms 消抖门、50ms 双击窗、1.6s 长按阈值三个常数（tick = 100µs）加上事件抑制标志，构成完整的手势识别；「按住旋转」会置抑制，使松开时不误发单击，也会屏蔽随后的长按保存。
- **编码器解码**：事件编码 s = 2×(channel−1) + level，`trans_tbl[s][enc_status]` 查表转移维护四相位状态，只有「状态 0 遇 B 上升」和「状态 3 遇 B 下降」两个组合真正计数——方向判决靠状态记忆，抗触点抖动。
- **档位状态机**：`uistat.mode` 是「旋钮正在操纵谁」的编辑焦点，单击走常用档循环、按住旋转可到达全部 11 档、松开旋转调值（`minmax` 钳位）；AGC 开启时导航代码会在三个方向上跳过 RFGAIN 档，但调节代码不做检查。
- **交互闭环**：CHANNEL 档旋转召回信道（频率+调制），任意档长按保存（蜂鸣 → 写信道 → 快照整个 uistat → 落 Flash）；shell 命令与 UI 手势共享同一套底层函数，是同一状态机的两个入口。
- **代码也留了作业**：双击事件无人消费（SINGLE 先行的时序困境）、`mode--` 在 CHANNEL 处不回绕出 −1 死档、从辅助档单击离开不清辅助带——三处都是练手的好题材。

## 7. 下一步学习建议

- **u4-l5（掉电不丢：配置的 Flash 持久化）**：本讲长按保存的终点 `config_save()`/`config_recall()` 在那一讲展开——页擦除、半字编程、magic + XOR 校验和，以及 `config_t` 里 100 个信道与 `uistat` 的完整布局。
- **u5-l1（并发与实时）**：`enc_count` 的 ISR/线程共享、`ext_callback` 与 I2S 音频回调的优先级关系、用 `stat` 命令实测 UI 轮询与 DSP 负载的相互影响。
- **动手方向**：把综合实践的模拟器留好，任何对 `ui.c` 的改动（新增档位、改手势、修 −1 死档）都可以先在模拟器上跑 1000 个随机事件再上硬件——这套「事件注入 + 不变量断言」的方法对任何嵌入式 UI 都通用。
