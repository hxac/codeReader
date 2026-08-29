# ui.c：触摸、拨轮、菜单树与数值输入

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `operation_requested` / `OP_LEVER` / `OP_TOUCH` 这套「中断举旗、线程消费」的异步请求模型：为什么触摸坐标测量绝不能放在中断里做。
2. 掌握 `menuitem_t` 表驱动菜单的完整套路：一张 `const` 数组如何同时描述菜单的文字、类型、参数与回调，以及 4 层菜单栈如何进退。
3. 读懂 NORMAL / MENU / NUMERIC / KEYPAD 四种 UI 模式各自的输入处理流程，特别是「长按进旋钮调数、单击进触摸键盘」这条数值输入双通道。
4. 理解 4 线电阻触摸屏的测量与两点校准原理，以及 STM32F0 ADC「模拟看门狗 + TIM3 硬件触发」如何做到零 CPU 轮询的触摸检测；顺带弄懂电池电压是怎么用 VREFINT 校准出来的。
5. 能独立给固件菜单加一个新条目并挂上自己的回调（本讲主实践）。

## 2. 前置知识

本讲是显示与交互子系统的最后一讲，建立在以下已学内容之上，先快速回顾：

- **顶半部 / 底半部中断模型**（u2-l5）：中断服务程序只做「举旗」这类极短操作，耗时处理移交线程。本讲的 `operation_requested` 是这一模式最典型的落地。
- **sweep 线程与 UI 的串行协作**（u2-l5）：所有 UI 处理都发生在低优先级 Thread1 的主循环里（[main.c:127-128](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L127-L128)），测量、UI、绘图绝不并发，所以 UI 代码不需要锁。
- **立即模式渲染与 markmap**（u4-l4）：屏幕没有帧缓冲，菜单打开时通过收窄 `area_width` 腾出右侧区域，关闭时再请求重画被遮挡的格子。
- **LCD 驱动原语**（u4-l1）：`ili9341_fill` / `ili9341_drawstring` / `ili9341_drawfont` 是仅有的几支「画笔」。

再补充几个本讲新用到、但之前没展开的嵌入式概念：

| 术语 | 通俗解释 |
|---|---|
| EXTI | STM32 的「外部中断/事件控制器」，把某个 GPIO 引脚的电平跳变变成中断。NanoVNA 用它监视拨轮的三个触点。 |
| ADC | 模数转换器，把引脚电压变成 0~4095 的数字（12 位）。STM32F072 的 ADC1 有多个输入通道（CH6=PA6、CH7=PA7、CH17=内部基准、CH18=VBAT）。 |
| 模拟看门狗（AWD） | ADC 的一个硬件比较器：每完成一次转换，就检查结果是否落在设定窗口 `[low, high]` 之内，越界则触发中断。NanoVNA 用它「免费」监视触摸线。 |
| TRGO | 定时器的「触发输出」信号。TIM3 每 10ms 发一次 TRGO，硬件直接启动一次 ADC 转换，全程不需要 CPU 参与。 |
| 电阻触摸屏 | 两层透明电阻膜，按压时两层在某点接触。给一层加电压梯度，从另一层读到的分压比就是按压坐标——纯欧姆定律，不需要任何触控芯片。 |
| VREFINT | 芯片内部出厂校准的 1.2V 基准源。用它可反推出真实供电电压，从而校准 VBAT 读数。 |
| 表驱动（data-driven） | 把「菜单长什么样、按下干什么」写成一张 `const` 数据表放在 Flash 里，执行代码只有一份通用的解释器。加菜单项≈加一行数据，不改逻辑。 |
| 定点数 | 用整数按固定比例模拟小数。如 scale 存 `真实值×1000`，避免在无 FPU 的 Cortex-M0 上做浮点显示。 |

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [ui.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c)（约 2300 行） | 全部人机交互逻辑 | 事件解码、四种 UI 模式、菜单表、数值输入、触摸测量与校准 |
| [nanovna.h](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h) | 全项目公共头文件 | `OP_*` 标志、`uistat_t`、`TOUCH_THRESHOLD`、ADC/触摸函数原型 |
| [adc.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/adc.c) | ADC1 裸寄存器驱动 | 单次转换、模拟看门狗、电池电压读取、ADC 中断 |
| [main.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c) | 固件主框架 | Thread1 循环（`ui_process` 调用点）、`vbat` shell 命令 |
| [plot.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c) | 绘图 | `draw_battery_status`（ADC 数据的消费方之一）、`plot_printf` 迷你格式化 |

阅读建议：ui.c 的函数排布是「工具函数（事件解码、触摸测量）→ 菜单回调 → 菜单表 → 模式切换与输入处理 → 中断配置与 `ui_init`」，本讲 4.1~4.4 正好按这个依赖顺序展开。

## 4. 核心概念与源码讲解

### 4.1 ui_process 事件分发：operation_requested 异步请求模型

#### 4.1.1 概念说明

NanoVNA 有两个输入源：右上侧的**拨轮**（可上下拨、可按，相当于单键鼠标+滚轮）和**电阻触摸屏**。它们都接在会触发中断的硬件上，但固件的设计纪律是：

> **中断里只置一个标志位，所有真正的处理都在 sweep 线程里做。**

为什么？看一眼 `touch_check()` 就明白了：它内部有 `chThdSleepMilliseconds(10)` 和两次 `chThdSleepMilliseconds(2)` 的等待（[ui.c:268-288](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L268-L288)）——在中断上下文里睡眠 14ms 是绝对禁止的（会拖死所有低优先级中断和整个 RTOS 调度）。所以固件把「检测到有输入」和「处理这个输入」拆成两半：

- **顶半部（中断上下文）**：只执行 `operation_requested |= OP_xxx`，一条读-改-写指令的量级。
- **底半部（线程上下文）**：`ui_process()` 在 Thread1 每轮循环里检查标志、清零、然后慢慢处理。

这个模型与 u2-l5 讲过的 `shell_function` 函数指针交接、u4-l4 的 `redraw_request` 请求-响应模型一脉相承：**共享一个 volatile 标志，单写者（中断）+ 单读者（线程），无锁且安全。**

#### 4.1.2 核心流程

一次触摸事件从手指到菜单的全链路：

```
手指按下触摸屏（Y 线电压被拉高超过 2000）
 └─ TIM3 TRGO 每 10ms 硬件触发一次 ADC 转换（CH7）
     └─ 结果落在看门狗窗口 [0,2000] 之外 → ADC 模拟看门狗中断
         └─ adc_interrupt() → handle_touch_interrupt()        【中断上下文】
             └─ operation_requested |= OP_TOUCH               （到此为止）
                 └─ Thread1 本轮循环走到 ui_process()          【线程上下文】
                     └─ ui_process_touch():
                         adc_stop() → touch_check() 测坐标（含 14ms 睡眠）
                         按 ui_mode 分发：
                           UI_NORMAL  → 拾取标记 / 选杠杆模式 / 松手后开菜单
                           UI_MENU    → menu_apply_touch() 命中菜单项
                           UI_NUMERIC → numeric_apply_touch() 点选数位
                         touch_start_watchdog() 重新武装看门狗
```

拨轮事件链路与之对称：GPIOA 电平跳变 → EXTI 通道 1/2/3 → `extcb1` → `OP_LEVER` → `ui_process_lever()`。

`ui_process()` 本身极其简短，是整个子系统的唯一入口：

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

标志位定义在 [nanovna.h:431-436](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L431-L436)：`OP_NONE=0x00`、`OP_LEVER=0x01`、`OP_TOUCH=0x02`，另有被注释掉的 `OP_FREQCHANGE` 预留位——这是一个可扩展的位掩码设计。

再看输入处理的二级分发。`ui_process_lever()` 按 `ui_mode` 四态路由：

| ui_mode | 处理函数 | 拨轮行为 |
|---|---|---|
| `UI_NORMAL` | `ui_process_normal()` | 单击开菜单；上/下拨按 `lever_mode` 五种模式移动标记/搜索/调中心/调 span/调电延迟 |
| `UI_MENU` | `ui_process_menu()` | 单击执行当前高亮项；上/下移动选择，移出边界则关闭菜单 |
| `UI_NUMERIC` | `ui_process_numeric()` | 上/下按 `10^digit` 步进改数值；单击确认 |
| `UI_KEYPAD` | `ui_process_keypad()` | 上/下在键盘键位间环形移动；单击按下当前键 |

注意一个细节：`ui_process_touch()`（[ui.c:2170-2203](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L2170-L2203)）的 switch 里**没有 `UI_KEYPAD` 分支**——键盘模式自己在 `ui_process_keypad()` 内部循环里直接轮询 `touch_check()`（[ui.c:2056-2061](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L2056-L2061)），因为键盘输入是一个「进入-循环-退出」的模态会话，不值得拆散到每轮主循环里。

#### 4.1.3 源码精读

**① 共享标志与 UI 状态**——[ui.c:62-82](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L62-L82) 定义了 `volatile uint8_t operation_requested`（跨中断/线程共享，必须 volatile）、`ui_mode`、菜单栈指针 `menu_current_level` 与高亮下标 `selection`。`uistat`（[nanovna.h:448-457](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L448-L457)）则存放「正在编辑的数值、当前数位、当前轨迹、拨轮模式、marker delta/tracking」这类瞬态 UI 状态，初始化为 digit=6、lever_mode=LM_MARKER（[ui.c:28-34](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L28-L34)）。

**② 事件位与时间参数**——[ui.c:36-47](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L36-L47)：

```c
#define EVT_BUTTON_SINGLE_CLICK     0x01
#define EVT_BUTTON_DOUBLE_CLICK     0x02   // 定义了但未使用
#define EVT_BUTTON_DOWN_LONG        0x04
#define EVT_UP                  0x10
#define EVT_DOWN                0x20
#define EVT_REPEAT              0x40

#define BUTTON_DOWN_LONG_TICKS      5000   /* 1sec */
#define BUTTON_DOUBLE_TICKS         2500   /* 500ms */
#define BUTTON_REPEAT_TICKS         625    /* 125ms */
#define BUTTON_DEBOUNCE_TICKS       200
```

这里有一个值得留意的「注释陷阱」：ChibiOS 系统节拍频率配置为 10kHz（[chconf.h:51](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chconf.h#L51) `CH_CFG_ST_FREQUENCY 10000`，即 1 tick = 100µs），所以真实时长是：长按 5000 tick = **500ms**（注释写 1sec）、重复 625 tick = **62.5ms**（注释写 125ms）、去抖 200 tick = **20ms**。三处注释恰好都是真实值的 2 倍——大概率是按 5kHz 节拍写的旧注释。读源码时拿配置对账，不要盲信注释。

**③ 拨轮电平读取**——[ui.c:49-59](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L49-L59)：拨轮三个触点接 GPIOA 的 bit1(下拨)/bit2(按下)/bit3(上拨)，`READ_PORT()` 一次读整个端口再掩码。

**④ `btn_check()`：非阻塞状态查询**——[ui.c:127-160](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L127-L160)。用 `last_button ^ cur_button` 找出发生跳变的位，若该位当前为 1 且距上一次检查超过 20ms（软去抖），则报告对应事件。它被每轮 `ui_process` 调一次，是「顺手看一眼」的语义。

**⑤ `btn_wait_release()`：阻塞等待一次交互结束**——[ui.c:162-199](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L162-L199)。与 ④ 配对：进入某个连续操作（如拖动标记、连发调数）后，用它在循环里持续解码——按住超过 500ms 产生 `EVT_BUTTON_DOWN_LONG`；此后每 62.5ms 产生一次 `EVT_UP|EVT_REPEAT` 或 `EVT_DOWN|EVT_REPEAT`（这就是拨轮长按能连续滚动的机制）；`inhibit_until_release` 保证长按事件发出后、手指松开前不再产生单击事件。函数名里的 release 指「等待这一轮拨轮交互收尾」。

**⑥ 两个中断顶半部**——EXTI 回调 [ui.c:2216-2222](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L2216-L2222) 与触摸中断 [ui.c:2272-2276](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L2272-L2276)：

```c
static void extcb1(EXTDriver *extp, expchannel_t channel) {
  (void)extp; (void)channel;
  operation_requested|=OP_LEVER;          // 拨轮：只举旗
}
...
void handle_touch_interrupt(void) {
  operation_requested|= OP_TOUCH;         // 触摸：只举旗
}
```

EXTI 配置 [ui.c:2224-2250](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L2224-L2250) 只使能 GPIOA 的通道 1/2/3、上升沿触发。`handle_touch_interrupt` 的调用方在 adc.c（见 4.4）。

**⑦ NORMAL 模式下的拨轮五模式**——[ui.c:1768-1794](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1768-L1794) 的 `ui_process_normal()` 按 `uistat.lever_mode` 分派到 `lever_move_marker`（逐点移动标记）/ `lever_search_marker`（跳到左右极值）/ `lever_move`（调中心或起点）/ `lever_zoom_span`（1-2-5 步进缩放 span）/ `lever_edelay`（20% 比例调电延迟）。杠杆模式由屏幕上下边缘触摸切换（见 `touch_lever_mode_select`，[ui.c:2150-2168](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L2150-L2168)）：点屏幕底行左侧选 LM_CENTER、右侧选 LM_SPAN，顶行则按有无电延迟选 LM_EDELAY 或 LM_MARKER——这就是屏幕顶部那两行提示条对应的交互。

#### 4.1.4 代码实践

**实践目标**：不写代码，用「跟踪调用链」的方式验证本模块的事件流，把中断到菜单的路径亲手走一遍。

**操作步骤**：

1. 在仓库根目录执行 `grep -n "operation_requested" ui.c nanovna.h`，确认全部 7 处引用（1 处定义、2 处置位、2 处消费、1 处清零、1 处 extern 声明）与 4.1.2 的链路图一致。
2. 打开 [ui.c:2170-2213](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L2170-L2213)，逐行标注 `ui_process_touch()` 在 `UI_NORMAL` 模式下的三个分支（拾取标记 / 选杠杆模式 / 等待释放后进菜单）。
3. 思考并回答：为什么 `menu_select_touch()`（[ui.c:1447-1455](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1447-L1455)）里要先 `touch_wait_release()` 再 `menu_invoke(i)`，而不是立即执行菜单项？（提示：如果不等待，`ui_process_touch` 返回后……触摸屏还处于按下状态，看门狗会不会立刻再次触发？）

**需要观察的现象 / 预期结果**：`ui_process_touch` 末尾调用了 `touch_start_watchdog()` 重新武装 ADC 看门狗，而此时手指可能还没松开——看门狗会立刻再次中断。但 `touch_check()` 的状态机（[ui.c:283-287](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L283-L287)）用 `last_touch_status` 对比，只有**状态变化**才返回 `EVT_TOUCH_PRESSED/RELEASED`，持续按住只返回 `EVT_TOUCH_DOWN`，而 `UI_MENU` 分支对 `PRESSED` 和 `DOWN` 都响应但命中判定基于坐标——所以连续触发是幂等的，最多重复执行同一项。`menu_select_touch` 先等释放再调用回调，正是为了避免「菜单项刚执行完、手指还压在屏幕上导致二次触发」。无硬件时此推理**待本地验证**；有真机可长按一个菜单项观察是否只执行一次。

#### 4.1.5 小练习与答案

**练习 1**：如果把手按在触摸屏上不动，`touch_check()` 返回什么？ Firmware 会反复执行菜单项吗？
**答**：返回 `EVT_TOUCH_DOWN`（持续按住态）。不会反复弹菜单——在 `UI_NORMAL` 下开菜单前有 `touch_wait_release()` 兜底；在 `UI_MENU` 下重复命中同一项在功能上幂等（重画同一菜单）。

**练习 2**：`operation_requested` 为什么必须声明为 `volatile uint8_t` 而不是普通 `uint8_t`？
**答**：它在中断（写）与 sweep 线程（读/清）之间共享。不加 volatile，编译器可能把循环中的读取优化成寄存器缓存，线程永远看不到中断改的值。`uint8_t` 则保证读写是单条指令、天然原子（位或操作的非原子性由「单写者+末尾整体清零」的调用时序掩盖：清零发生在两类消费都完成之后）。

**练习 3**：`EVT_BUTTON_DOUBLE_CLICK`（0x02）和 `OP_FREQCHANGE`（被注释的 0x04）说明了什么工程现象？
**答**：这是典型的「预留但未实现」的痕迹——事件模型按位掩码设计，扩展只需加一个位，但双击检测与频率变更通知最终没有做。读源码时看到这类死定义，应对照使用点判断它是能力预留还是废弃残留。

### 4.2 menuitem_t 菜单表：表驱动菜单树与状态高亮

#### 4.2.1 概念说明

NanoVNA 的全部菜单——6 个顶层入口、几十个条目——没有一行 `if (选择==3 && 在二级菜单)` 式的硬编码，而是**一张张 `const menuitem_t` 数组**：

```c
typedef struct {
  uint8_t type;        // 条目类型 MT_*
  uint8_t data;        // 传给回调的第二参数（含义由各回调自定）
  char *label;         // 显示文字（支持 "\2" 双行编码）
  const void *reference; // 回调函数 或 子菜单表指针（按 type 解释）
} menuitem_t;
```

这个结构体用了 `#pragma pack(push, 2)` 压缩对齐（[ui.c:84-92](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L84-L92)），4 个字段压成 6 字节（不打包则是 8 字节），几十张表加起来省下的 Flash 在 128K 的芯片上是实打实的收益。

`type` 决定 `reference` 字段的含义，构成一个微型「解释器」：

| type | reference 含义 | 点击行为 |
|---|---|---|
| `MT_NONE` | — | **哨兵**，表结束标志 |
| `MT_BLANK` | — | 占位空行（跳过不画） |
| `MT_SUBMENU` | `const menuitem_t *` 子菜单表 | 压栈进入子菜单 |
| `MT_CALLBACK` | `menuaction_cb_t` 回调函数 | 执行回调 |
| `MT_CANCEL` | —（BACK 项） | 弹栈返回上级 |
| `MT_CLOSE` | — | 直接回 NORMAL 模式 |

好处：**加一个菜单功能 = 在表里加一行 + 写一个回调**，解释器（`menu_invoke`）一行不改。这正是本讲实践的立足点。

#### 4.2.2 核心流程

菜单树的静态结构（由各表互相引用构成，全部定义在 [ui.c:843-1058](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L843-L1058)）：

```
menu_top（顶层，无 BACK）
├─ DISPLAY  → menu_display
│   ├─ TRACE    → menu_trace        （TRACE 0~3 开关）
│   ├─ FORMAT   → menu_format       （LOGMAG/PHASE/DELAY/SMITH/SWR + MORE→menu_format2）
│   ├─ SCALE    → menu_scale        （SCALE/DIV、REFERENCE POSITION、ELECTRICAL DELAY）
│   ├─ CHANNEL  → menu_channel      （CH0 反射 / CH1 透射）
│   ├─ TRANSFORM→ menu_transform    （ON、低通冲击/阶跃、带通、WINDOW→子菜单、VELOCITY FACTOR）
│   ├─ BANDWIDTH→ menu_bandwidth    （1k/300/100/30/10 Hz 五档）
│   └─ ← BACK
├─ MARKER   → menu_marker（SELECT MARKER / SEARCH / OPERATIONS / SMITH VALUE 四个子菜单）
├─ STIMULUS → menu_stimulus（START/STOP/CENTER/SPAN/CW/PAUSE）
├─ CAL      → menu_cal（CALIBRATE→menu_calop、SAVE→menu_save、RESET、CORRECTION）
├─ RECALL   → menu_recall（0~4 槽）
└─ CONFIG   → menu_config（TOUCH CAL/TOUCH TEST/SAVE/VERSION、DFU→menu_dfu）
```

运行时用一个**菜单栈**（`menu_stack[4]` + `menu_current_level`）记录当前位置：

```
点击 MT_SUBMENU 项:
  menu_push_submenu(sub)  → level+1（上限 3），menu_stack[level]=sub，
                            erase_menu_buttons() 擦掉旧菜单 → draw_menu() 画新菜单
点击 MT_CANCEL 项 / 回调里调 menu_move_back():
  menu_move_back()        → level-1，重画上级
```

最深路径示例：`menu_top(0) → menu_cal(1) → menu_calop(2)`，DONE 回调再 push `menu_save(3)`——恰好用满 `MENU_STACK_DEPTH_MAX=4` 层（[ui.c:1060-1063](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1060-L1063)）。

绘制与命中：菜单占据屏幕右侧 60px 宽、每项 30px 高、最多 8 行（[ui.c:1142-1145](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1142-L1145)），即 x∈[260,320)。触摸命中测试就是一遍简单的矩形判定（[ui.c:1457-1479](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1457-L1479)）；点到菜单区之外则等释放后直接回 NORMAL 模式（=「点空白处关菜单」）。

#### 4.2.3 源码精读

**① 顶层表与菜单栈**——[ui.c:1050-1063](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1050-L1063)：`menu_top[]` 六个 `MT_SUBMENU` 项加哨兵；`menu_stack` 初始只放 `menu_top`。

**② 解释器 `menu_invoke()`**——[ui.c:1111-1140](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1111-L1140)：

```c
static void
menu_invoke(int item)
{
  const menuitem_t *menu = menu_stack[menu_current_level];
  menu = &menu[item];
  switch (menu->type) {
  case MT_NONE:
  case MT_BLANK:
  case MT_CLOSE:
    ui_mode_normal();  break;
  case MT_CANCEL:
    menu_move_back();  break;
  case MT_CALLBACK: {
    menuaction_cb_t cb = (menuaction_cb_t)menu->reference;
    if (cb == NULL) return;
    (*cb)(item, menu->data);        // 把「下标」和「data」都交给回调
    break;
  }
  case MT_SUBMENU:
    menu_push_submenu((const menuitem_t*)menu->reference);
    break;
  }
}
```

回调统一签名 `void (*menuaction_cb_t)(int item, uint8_t data)`（[ui.c:436](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L436)）。**回调有两种取参风格**：

- 用 `item`（表内下标）：如 `menu_stimulus_cb`（[ui.c:669-694](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L669-L694)）`switch(item)` 分辨 START/STOP/CENTER/SPAN/CW/PAUSE——条目含义与位置强绑定，**中间插项会错位**。
- 用 `data` 字段：如 `menu_calop_cb`（[ui.c:438-445](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L438-L445)）用 `data` 传 `CAL_OPEN/CAL_SHORT/...`，`menu_trace_cb`（[ui.c:552-571](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L552-L571)）用 `data` 传轨迹号——位置无关，**推荐写法**（本讲实践即用 data 风格，虽然无参数也保持签名一致）。

顺带一个源码彩蛋：`menu_bandwidth_cb` 实际只声明了一个参数 `int item`（[ui.c:634-639](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L634-L639)），却通过 `menuaction_cb_t` 两参指针类型调用——在 ARM EABI 调用约定下多余的 r1 实参被自然忽略，能跑但属于不值得模仿的灰色手法（C 标准视角是未定义行为）。

**③ 菜单按钮绘制**——[ui.c:1415-1445](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1415-L1445) `draw_menu_buttons()`：循环 `i < MENU_BUTTON_MAX(8)`，遇 `MT_NONE` 停、遇 `MT_BLANK` 跳过；每项先 `ili9341_fill` 画 60×28 背景块，`UI_MENU` 模式下当前 `selection` 项用 `config.menu_active_color` 高亮；然后调 `menu_item_modify_attribute()` 做**状态反色**，最后写字。

**④ 状态高亮 `menu_item_modify_attribute()`**——[ui.c:1340-1413](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1340-L1413)。这是「菜单反映仪器当前状态」的关键：逐表逐项判断，例如

- `menu_trace` 中已启用的轨迹项背景换成该轨迹颜色（`config.trace_color[item]`）；
- `menu_marker_sel`/`menu_marker_search` 中已启用的 marker、tracking 项反白；
- `menu_calop` 中已完成采集的标准件（查 `cal_status` 的 `CALSTAT_OPEN` 等位，承接 u3-l2）反白；
- `menu_bandwidth` 中当前带宽档反白成纯黑底白字（[ui.c:1390-1394](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1390-L1394)）。

这也解释了为什么很多回调末尾都要再调一次 `draw_menu()`——状态变了，反色位置要跟着变。

**⑤ 双行文字编码**——`menu_is_multiline()`（[ui.c:1329-1338](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1329-L1338)）：label 以 `'\2'` 开头表示双行，其后是两个以 NUL 分隔的字符串，如 `"\2REFERENCE\0POSITION"`。60px 宽放不下长单词时的省 Flash 技巧（不用 snprintf 拼接）。

**⑥ 菜单模式与绘图区的联动**——`ui_mode_menu()`（[ui.c:1600-1612](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1600-L1612)）把 `area_width` 从 `AREA_WIDTH_NORMAL` 收窄 60px——新轨迹画到 x=260 为止，不会钻进菜单底下；`leave_ui_mode()`（[ui.c:1499-1510](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1499-L1510)）退出时 `erase_menu_buttons()` 擦掉菜单再请求重画被遮挡的格子，正好接上 u4-l4 的 markmap 机制。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：给 DISPLAY 菜单新增一个 `INFO` 条目，点击后把当前扫描点数 `sweep_points` 和带宽档 `bandwidth` 短暂显示在屏幕上，体验 `menuitem_t` 的 `type/data/label/reference` 四字段与回调挂接的全流程。

**操作步骤**：

1. **写回调**。在 ui.c 中 `menu_bandwidth_cb`（约 634 行）之前插入（必须在 `menu_display[]` 表定义之前，否则编译器不认识这个符号）：

   ```c
   /* 示例代码：本讲新增的练习回调 */
   static void
   menu_display_info_cb(int item, uint8_t data)
   {
     (void)item; (void)data;
     char buf[24];

     adc_stop();                 /* 要用 ADC 测触摸，先停看门狗（借用） */
     ili9341_set_foreground(DEFAULT_FG_COLOR);
     ili9341_set_background(DEFAULT_BG_COLOR);
     plot_printf(buf, sizeof buf, "POINTS: %d", sweep_points);
     ili9341_drawstring(buf, OFFSETX, 100);
     plot_printf(buf, sizeof buf, "BANDWIDTH: %d", bandwidth);
     ili9341_drawstring(buf, OFFSETX, 115);

     /* 等待一次触摸或单击后返回，交互模式参考 show_version() */
     while (touch_check() != EVT_TOUCH_PRESSED &&
            !(btn_check() & EVT_BUTTON_SINGLE_CLICK))
       ;
     touch_start_watchdog();     /* 归还 ADC，重新武装触摸看门狗 */

     redraw_frame();             /* 恢复被文字覆盖的绘图区（同 menu_config_cb） */
     request_to_redraw_grid();
     draw_menu();
   }
   ```

   要点：`sweep_points`/`bandwidth` 是 `current_props._sweep_points`/`_bandwidth` 的别名宏（[nanovna.h:397](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L397)、[nanovna.h:409](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L409)）；「停看门狗→用触摸→重启看门狗」的借用-归还模式照抄 `show_version()`（[ui.c:374-397](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L374-L397)）。

2. **挂菜单项**。修改 [ui.c:951-960](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L951-L960) 的 `menu_display[]`，在 BACK 之前加一行：

   ```c
   const menuitem_t menu_display[] = {
     { MT_SUBMENU, 0, "TRACE", menu_trace },
     { MT_SUBMENU, 0, "FORMAT", menu_format },
     { MT_SUBMENU, 0, "SCALE", menu_scale },
     { MT_SUBMENU, 0, "CHANNEL", menu_channel },
     { MT_SUBMENU, 0, "TRANSFORM", menu_transform },
     { MT_SUBMENU, 0, "BANDWIDTH", menu_bandwidth },
     { MT_CALLBACK, 0, "INFO", menu_display_info_cb },  /* ← 新增 */
     { MT_CANCEL, 0, S_LARROW" BACK", NULL },
     { MT_NONE, 0, NULL, NULL } // sentinel
   };
   ```

   注意容量约束：加完共 7 项 + BACK = 8 项，恰好顶满 `MENU_BUTTON_MAX=8`，固件菜单不支持滚动，再多就显示不下了。另外 `menu_display` 的条目全是子菜单、`menu_item_modify_attribute()` 中也没有按 `menu_display` 下标取状态的分支，所以在它中间/末尾插项是安全的——**不要**往 `menu_stimulus` 里插，它的回调按下标 switch，会整体错位。

3. **编译烧录**（方法承接 u1-l2）：有工具链直接 `make && make flash`；无本地工具链用 docker：

   ```bash
   docker run --rm -v "$PWD":/work -w /work edy555/arm-embedded:8.2 make
   ```

**需要观察的现象 / 预期结果**：真机上单击拨轮开菜单 → DISPLAY → INFO，屏幕中央偏左出现 `POINTS: 101`（默认 101 点）与 `BANDWIDTH: 0`；任意触摸或单击后恢复菜单。注意显示的 `BANDWIDTH` 是**档位编号**（0~4，对应 1kHz/300/100/30/10Hz，见 [ui.c:941-949](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L941-L949) 与 u2-l3 的带宽表），不是 Hz 数值——若想显示成频率需自行查 `bandwidth_accumerate_count`（[main.c:604](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L604)）旁边的字符串表。等待期间扫频暂停（回调在 sweep 线程里阻塞，承接 u2-l5 的单线程串行模型）。无真机时，编译通过即验证了语法与符号引用，运行效果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：想在 CAL 菜单的 SAVE 子菜单里再加一个「SAVE 5」，只改表就够吗？
**答**：不够。`menu_save[]` 表加一行 `{ MT_CALLBACK, 5, "SAVE 5", menu_save_cb }` 只是 UI 部分；回调里 `caldata_save(data)` 的 data=5 需要 flash 层有第 5 个校准槽（u3-l4：槽位地址按 `SAVE_SLOT_OFFSET` 步进布局，共 5 槽，槽号被 `caldata_save` 的参数校验约束）。超出布局会写错地址或被拒绝——表驱动只解耦了 UI，不改底层容量。

**练习 2**：为什么菜单表全部声明为 `const`？存放在哪个存储区？
**答**：`const` 全局数组进 Flash 的 `.rodata` 段，不占 16KB SRAM。这是 16KB RAM 设备上表驱动 UI 的前提——几十张菜单表若在 RAM，光菜单就能吃掉一大半内存。

**练习 3**：`ensure_selection()`（[ui.c:1065-1074](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1065-L1074)）在什么时候防住什么 bug？
**答**：进入/返回菜单时，若上次离开时的高亮下标 `selection` 超出新菜单的条目数（各菜单长度不同），会越界读表。它扫到 `MT_NONE` 哨兵数出长度，把 `selection` 钳到最后一个有效项。

### 4.3 数值输入：NUMERIC 旋钮调数与 KEYPAD 触摸键盘

#### 4.3.1 概念说明

频率、span、scale、电延迟这些参数需要输入任意数值。NanoVNA 为同一个目标准备了两套交互，由 `KM_*` 枚举（[ui.c:70-72](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L70-L72)）统一描述「正在编辑什么」：

- **UI_NUMERIC（旋钮调数）**：屏幕底部出现 30px 数值条，大字体显示当前值，某一数位高亮。拨轮上下 = 该位 ±1；长按切换到「移位模式」左右挪焦点数位；单击确认写入。
- **UI_KEYPAD（触摸键盘）**：右侧菜单区变成一块 4×4 触摸数字键盘（数字、小数点、退格、以及 ×1/K/M/G 单位键），点单位键表示「输入结束」并携带数量级。

入口在 `menu_stimulus_cb` / `menu_scale_cb` / `menu_velocity_cb`，用**同一个动作的不同时长**分流（[ui.c:679-685](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L679-L685)）：

```c
if (btn_wait_release() & EVT_BUTTON_DOWN_LONG) {   // 长按 500ms
  ui_mode_numeric(item);      // 旋钮逐位调
  ui_process_numeric();
} else {
  ui_mode_keypad(item);       // 单击 → 触摸键盘
  ui_process_keypad();
}
```

一个小细节体现了两模式的分量：数值显示区一屏最多 10 位，`draw_numeric_input()` 用一个 16 位掩码 `xsim = 0b0010010000000000` 逐位左移，在第 3、6 位后多空 8px——硬编码模拟出「千分位分隔」的视觉效果（[ui.c:1289-1327](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1289-L1327)）。

#### 4.3.2 核心流程

**NUMERIC 模式**（`ui_process_numeric`，[ui.c:1967-2025](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1967-L2025)）：

```
ui_mode_numeric(KM_x):
  leave_ui_mode()               收起菜单
  area_height = 240-30          绘图区上移，底部让给数值条
  draw_numeric_area_frame()     画底条 + 目标名（"START" 等）
  fetch_numeric_target()        读当前值进 uistat.value，数位数进 uistat.digit
  draw_numeric_area()           格式化 "%9d" 显示

循环内（拨轮）:
  非移位模式:  上/下 → uistat.value ± 10^digit   （digit=0 即个位 ±1）
  移位模式:    上/下 → digit ± 1（0~8），越界 → goto exit（放弃编辑）
  单击 → set_numeric_value() 写回目标 + ui_mode_normal()
  长按 → 切换 digit_mode（改调数位 or 移焦点）
触摸（numeric_apply_touch）:
  点数值条下半 → +10^digit；上半 → -10^digit
  直接点某个数字 → 选中该数位
  x<64（左侧） → 取消退出；x>260（右侧）→ 切换到 KEYPAD 模式
```

**KEYPAD 模式**（`keypad_click` 见 [ui.c:1827-1898](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1827-L1898)，主循环 `ui_process_keypad` 见 [ui.c:2027-2069](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L2027-L2069)）：

```
ui_mode_keypad(KM_x):
  keypads = keypads_mode_tbl[KM_x]     选键盘布局（freq/scale/time 三种）
  draw_menu() + draw_keypad() + 数值条

keypad_click(key):
  数字/./-  → 追加进 kp_buf 字符串（最多 NUMINPUT_LEN=10 字符）
  KP_BS     → 退格；buf 已空 → 返回 KP_CANCEL（整个编辑放弃）
  单位键 ×1/K/M/G/N/P → my_atof(kp_buf) × 单位倍率
       按 KM_x 写回（set_sweep_frequency / set_trace_scale / ...）
       → 返回 KP_DONE（编辑完成）

退出前统一: redraw_frame() + request_to_redraw_grid() + ui_mode_normal()
```

注意键盘输入是**字符串→浮点→目标**的路线：人打的是 `"100"` + `M`，固件拼成 `"100"` 后 `my_atof` 得 100.0，再乘 10^6 写入频率。这比逐位整数编辑更接近计算器的直觉，代价是一次浮点转换（只在输入结束时发生一次，无 FPU 也能接受）。

#### 4.3.3 源码精读

**① 键盘布局表**——键位坐标与键码打包进 2 字节的 `keypads_t`（x:4bit, y:4bit, c:8bit，[ui.c:1178-1182](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1178-L1182)），三种布局 `keypads_freq/keypads_scale/keypads_time`（[ui.c:1186-1253](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1186-L1253)）：频率键盘带 G/M/K/×1，时间键盘（电延迟）带 N/P/负号，scale 键盘最精简。每张表以 `c=-1` 结尾。绘制用 20×22 大数字字体 `ili9341_drawfont`（[ui.c:1259-1277](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1259-L1277)），键位命中测试 [ui.c:1900-1924](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1900-L1924) 同样是矩形判定 + 先高亮等释放再触发（`touch_wait_release` 后重画两次制造按键反馈）。

**② 目标读写**——`fetch_numeric_target()`（[ui.c:1512-1556](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1512-L1556)）按 `keypad_mode` 把当前值搬进 `uistat.value`，注意三处**定点化**：scale/refpos 乘 1000 存（0.5dB/div → 500）、velocity factor 乘 100（0.83 → 83）、延迟 scale 乘 1e12（皮秒）；末尾数一遍位数作为初始焦点 `uistat.digit`。`set_numeric_value()`（[ui.c:1558-1590](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1558-L1590)）反向往回写。定点技巧让整个逐位编辑流程都在 32 位整数域内完成，只在最终写入时做一次除法。

**③ 单位键终结输入**——`keypad_click()`（[ui.c:1827-1898](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1827-L1898)）开头：

```c
if ((c >= KP_X1 && c <= KP_G) || c == KP_N || c == KP_P) {
  int32_t scale = 1;
  if (c >= KP_X1 && c <= KP_G) {
    int n = c - KP_X1;
    while (n-- > 0)
      scale *= 1000;          // ×1→K→M→G 每级 1000
  } else if (c == KP_N) {
    scale *= 1000;            // 纳秒→皮秒
  }
  double value = my_atof(kp_buf) * scale;
  switch (keypad_mode) { ... set_sweep_frequency(ST_START, value); ... }
  return KP_DONE;
}
```

KP_x1~KP_G 编号为 12~15（[ui.c:1166-1169](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1166-L1169)），`c - KP_X1` 得 0~3，连乘对应数量级——用**键码的连续编号编码倍率**，省一张查表。

**④ 旋钮调数的步进**——[ui.c:2004-2013](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L2004-L2013)：

```c
int32_t step = 1;
for (n = uistat.digit; n > 0; n--)
  step *= 10;
if (status & EVT_DOWN) uistat.value += step;
if (status & EVT_UP)   uistat.value -= step;
```

`10^digit` 的循环实现（M0 上移位也行，但编译器对变量循环乘法同样友好，且 `digit` 上限 9 时 `10^9` 恰好仍在 int32 范围内）。

#### 4.3.4 代码实践

**实践目标**：在 PC 上用 Python 复现 `keypad_click` 的「字符串+单位→数值」换算和 `fetch_numeric_target` 的定点化规则，验证对两套数值输入机制的理解（无硬件即可完成）。

**操作步骤**：

1. 编写 `kpsim.py`（示例代码，非项目源码）：

   ```python
   def my_atof(s):
       # 固件 my_atof 支持 [+-]?digits[.digits]，此处等价实现
       return float(s)

   def keypad_click(buf, unit):        # unit: 'X1','K','M','G','N','P'
       if unit == 'P':                 # 皮秒：单位即 1
           return my_atof(buf)
       if unit == 'N':                 # 纳秒 → 皮秒
           return my_atof(buf) * 1000
       scale = 1000 ** ('X1', 'K', 'M', 'G').index(unit)
       return my_atof(buf) * scale

   def fixed_point(km, real):          # fetch_numeric_target 的定点规则
       if km in ('KM_SCALE', 'KM_REFPOS'):
           return round(real * 1000)
       if km == 'KM_VELOCITY_FACTOR':
           return round(real * 100)
       if km == 'KM_SCALEDELAY':
           return round(real * 1e12)
       return round(real)              # 频率类：原值

   for case in [('100', 'M'), ('900', 'M'), ('10', 'G'), ('4.5', 'G'), ('50', 'X1')]:
       print(case, '->', keypad_click(*case))
   print('scale 0.5dB/div ->', fixed_point('KM_SCALE', 0.5))
   print('velocity 0.83 ->', fixed_point('KM_VELOCITY_FACTOR', 0.83))
   ```

2. 运行 `python3 kpsim.py`。

**需要观察的现象 / 预期结果**：输出应为 `100M → 100000000.0`、`10G → 10000000000.0`、`4.5G → 4500000000.0`、`50X1 → 50.0`、`scale 0.5 → 500`、`velocity 0.83 → 83`。对照 [ui.c:1831-1841](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1831-L1841) 的连乘逻辑与 [ui.c:1531-1545](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1531-L1545) 的定点规则逐项核对。注意频率上限：`set_sweep_frequency` 会把值钳制到 `STOP_MAX`（u3-l1），所以键盘输入 `99G` 也不会炸，只会被钳回——这正是「输入层宽松、频率正门严格」分层的体现。

#### 4.3.5 小练习与答案

**练习 1**：为什么 KEYPAD 输入要走「字符串 → my_atof → 浮点」而 NUMERIC 用纯整数？
**答**：KEYPAD 允许小数（"4.5"G）和负号，天然是文本输入，一次性转换成本可接受；NUMERIC 是逐位 ±1 的连续微调，每秒可能触发几十次，必须留在整数域保证响应速度并避免浮点误差累积。

**练习 2**：在 NUMERIC 模式把焦点数位一路下拨越过 0 会发生什么（[ui.c:1998-2003](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1998-L2003)）？
**答**：`goto exit` → 直接 `ui_mode_normal()`，**不调用** `set_numeric_value()`——即「越界 = 放弃编辑」，这是固件里隐式的取消手势（触摸 x<64 同理）。

**练习 3**：`kp_buf` 长 `NUMINPUT_LEN+1 = 11` 字节，输入时如何防溢出？
**答**：追加数字/小数点前都检查 `kp_index < NUMINPUT_LEN`（[ui.c:1876-1885](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1876-L1885)），末尾统一 `kp_buf[kp_index] = '\0'` 保 NUL 结尾；小数点还查重（已有 `.` 不再追加）。10 字符上限与 `draw_numeric_input` 固定 10 位显示区严格配套。

### 4.4 触摸与 ADC 驱动：电阻屏、校准、模拟看门狗与电池

#### 4.4.1 概念说明

**电阻屏测量**：NanoVNA 的触摸屏没有触控 IC，四根线（X 层两根、Y 层两根）直接接在 GPIO 上：PB0/PA6 属于一层（代码称 X 线），PB1/PA7 属于另一层（Y 线），其中 PA6=ADC_IN6、PA7=ADC_IN7。测量某一方向坐标时：

1. 给**一层**两端分别加 VDD 和 GND——该层形成线性电压梯度 \( V(d) = V_{DD}\cdot d/L \)；
2. 把**另一层**设为高阻输入，按压点的接触使两层导通，浮层拾取到的电压等于按压位置在梯度层上的分压；
3. ADC 读这个电压，12 位读数 ≈ 坐标 × 4096 / L。

**怎么知道「被按下了」？** 最朴素的办法是 CPU 不停地测——浪费。NanoVNA 的做法是把 ADC 的**模拟看门狗**当成免费的硬件监测器：让 TIM3 每 10ms 自动触发一次 ADC 转换 PA7（Y 线，平时被下拉电阻拉到 0V），看门狗窗口设为 [0, 2000]。没人碰时读数始终在窗口内，没有任何中断；一旦按压，X 层的高电平经接触电阻耦合到 Y 线，读数越出窗口上限，硬件立刻产生 AWD 中断。**待机成本 = 每 10ms 一次硬件转换，CPU 占用为零**，检测延迟最坏 10ms——对人类手指足够快。

**ADC 的「借用-归还」纪律**：同一个 ADC 外设同时承担「看门狗监测」「触摸坐标测量」「电池电压测量」三个角色，而 STM32F0 规定转换进行中（ADSTART 置位）不能改配置。于是固件形成固定礼仪：`adc_stop()` 停下连续看门狗模式 → `adc_single_read()` 逐次单发测量 → 完事后 `touch_start_watchdog()` 恢复监测。ui.c 里每个用触摸的函数（`ui_process_touch`、`ui_process_keypad`、`show_version`、`touch_cal_exec`……）都严格遵守，自己写的回调也必须遵守（4.2 实践已示范）。

**电池电压**：ADC 通道 18 接 VBAT 经内部 2:1 分压，通道 17 是出厂校准的 VREFINT。读数换算成真实毫伏数需用 VREFINT 消除供电漂移：

\[ V_{bat}(mV) = \frac{3300 \times 2 \times N_{vbat}}{4096} \times \frac{VREFINT_{CAL}}{N_{vrefint}} \]

#### 4.4.2 核心流程

触摸检测与测量的完整时序：

```
【待机】 TIM3 TRGO --10ms--> ADC 转换 PA7 --> 结果在 [0,2000] 内 → 无事发生
【按下】 某次转换结果 >2000 → AWD 中断 → handle_touch_interrupt() → OP_TOUCH
【处理】 sweep 线程 ui_process_touch():
           adc_stop()                         停看门狗
           touch_check():
             touch_status() 检测按压(>阈值)
               └ 按 → 睡10ms等稳定 → touch_measure_x() → touch_measure_y()
                        (各含2ms梯度建立等待) → 复核仍按着才接受坐标
                 └ 状态变化 → EVT_TOUCH_PRESSED / RELEASED，否则 DOWN / NONE
           按 ui_mode 分发处理
           touch_start_watchdog()             恢复监测
```

两点校准的数学：让用户先后点屏幕左上角与右下角，得到原始读数 (x1,y1)、(x2,y2)，则

\[ x_{pix} = \frac{(x_{raw} - x_1)\times 16}{k_x},\quad k_x = \frac{(x_2-x_1)\times 16}{320} \]

（y 同理，320/240 为屏幕宽高）。\(k_x\) 是「每像素原始读数 ×16」的定点表示，乘 16 是为了让小斜率也能用整数存得够准——与 u3-l1 频率表、4.3 节定点化一脉相承的手法。

#### 4.4.3 源码精读

**① 触摸三种 GPIO 姿态**——`touch_prepare_sense()`（[ui.c:241-252](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L241-L252)）是待机姿态：X 线双端推挽输出高，Y 线双端下拉输入——按压时 Y 线（PA7）被拉向高，这正是看门狗监视的电压。`touch_measure_x()`（[ui.c:221-239](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L221-239)）转为测量姿态：X 线放开为输入，Y 线一端拉高一端拉低形成梯度，睡 2ms 等梯度建立后 `adc_single_read(ADC_CHSELR_CHSEL6)` 读 PA6；`touch_measure_y()`（[ui.c:201-219](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L201-L219)）交换角色读 CH7。

**② 触摸状态机**——`touch_check()`（[ui.c:268-288](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L268-L288)）：检测到按压后先 `chThdSleepMilliseconds(10)` 等手指稳定再测坐标，测完**复核**仍处于按压态才把坐标写进 `last_touch_x/y`（防止松手瞬间的拖影读数），最后回到待机姿态并按「状态是否变化」输出 PRESSED/RELEASED/DOWN/NONE 四种事件。

**③ 两点校准**——`touch_cal_exec()`（[ui.c:304-337](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L304-337)）：画角标提示 → `touch_wait_release()` 阻塞等一次完整按压-释放 → 取 `last_touch_x/y` 为角点原始值；两次采点后按上面的公式写 `config.touch_cal[4]`（掉电保存，u3-l4）。换算在 `touch_position()`（[ui.c:367-372](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L367-L372)），全整数运算。若屏幕点得不准，CONFIG → TOUCH CAL 走的就是这条路径。

**④ ADC 驱动层**——[adc.c:33-78](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/adc.c#L33-L78)：`adc_init()` 做上电校准（ADCAL）后使能；`adc_single_read(chsel)` 是阻塞单次转换——选通道、239.5 周期采样（高输入阻抗的电阻屏分压需要长采样时间）、启动并忙等完成、返回 `DR`。看门狗启动 `adc_start_analog_watchdogd()`（[adc.c:106-127](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/adc.c#L106-L127)）的关键配置：

```c
cfgr1 = ADC_CFGR1_RES_12BIT | ADC_CFGR1_AWDEN      // 使能模拟看门狗
  | ADC_CFGR1_EXTEN_0                              // 外部触发：上升沿
  | ADC_CFGR1_EXTSEL_0 | ADC_CFGR1_EXTSEL_1;       // 触发源 = TRG3 (TIM3_TRGO)
...
VNA_ADC->TR   = ADC_TR(0, TOUCH_THRESHOLD);        // 窗口 [0,2000]
VNA_ADC->IER  = ADC_IER_AWDIE;                     // 越界中断使能
```

`TOUCH_THRESHOLD` 为 2000（[nanovna.h:468](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L468)）——约 1.6V，低于按压耦合电平、高于悬噪。TIM3 由 `ui_init()`（[ui.c:2278-2296](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L2278-L2296)）以 1kHz 计数、周期 10 tick 启动连续模式，即 TRGO 每 **10ms** 一发。ADC 中断服务 [adc.c:144-167](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/adc.c#L144-L167)：清标志后仅当 `AWD` 置位时调 `handle_touch_interrupt()`——回到 4.1 的举旗点，闭环。

**⑤ 电池测量**——`adc_vbat_read()`（[adc.c:80-104](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/adc.c#L80-L104)）：

```c
#define ADC_FULL_SCALE 3300
#define VREFINT_CAL (*((uint16_t*)0x1FFFF7BA))   // 出厂校准值(系统存储区)
...
uint32_t vrefint = adc_single_read(ADC_CHSELR_CHSEL17);
uint32_t vbat    = adc_single_read(ADC_CHSELR_CHSEL18);
ADC->CCR &= ~(ADC_CCR_VREFEN | ADC_CCR_VBATEN);
touch_start_watchdog();
// 除以 4096 而非 4095：省一次除法，误差可忽略（源码注释言明）
uint16_t vbat_raw = ((ADC_FULL_SCALE * 2 * vbat)>>12) * VREFINT_CAL / vrefint;
if (vbat_raw < 100)
  return -1;                       // D2 未焊接的机器：无电池分压
return vbat_raw + config.vbat_offset;
```

VBATEN 打开时内部分压电阻持续耗电，所以用完立即关——又一处「借用-归还」。读数 `<100mV` 视为无电池（部分整机不焊 D2），返回 -1 由调用方跳过绘制。两个消费方：`draw_battery_status()`（[plot.c:1688-1715](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1688-L1715)，每趟扫完由 `REDRAW_BATTERY` 触发（[plot.c:1423](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/plot.c#L1423)、[main.c:135](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L135)），按 4100~3100mV 区间逐格画电池条，低于 3300mV 变警告色）；以及 shell 命令 `vbat`（[main.c:2031-2036](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2031-L2036)，注册于 [main.c:2191](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2191)），USB 串口敲 `vbat` 直接打印毫伏数。

#### 4.4.4 代码实践

**实践目标**：用 Python 复现触摸校准换算与电池公式，把两个硬件公式变成可在 PC 上检验的纯数学（无硬件可完成）；有真机的读者加做 TOUCH CAL 与 `vbat` 命令观察。

**操作步骤**：

1. 编写 `adcsim.py`（示例代码，非项目源码）：

   ```python
   def touch_position(raw_x, raw_y, cal):        # cal = config.touch_cal[4]
       x = (raw_x - cal[0]) * 16 // cal[2]       # 定点斜率，整数除法截断
       y = (raw_y - cal[1]) * 16 // cal[3]
       return x, y

   def cal_from_corners(x1, y1, x2, y2):         # touch_cal_exec 的采样公式
       return [x1, y1, (x2 - x1) * 16 // 320, (y2 - y1) * 16 // 240]

   def vbat_mv(n_vbat, n_vref, vrefint_cal=1630):  # adc_vbat_read 公式
       return ((3300 * 2 * n_vbat) >> 12) * vrefint_cal // n_vref

   # 自校验：采样两点 → 反算应回到角点附近
   cal = cal_from_corners(620, 600, 2600, 2300)   # 取自 ui.c:105 注释例值
   print('corner(620,600)  ->', touch_position(620, 600, cal))
   print('corner(2600,2300)->', touch_position(2600, 2300, cal))
   print('center ->', touch_position(1610, 1450, cal))
   print('vbat:', vbat_mv(2000, 1650), 'mV')      # n_vref 假设 1650
   ```

2. 运行 `python3 adcsim.py`。

**需要观察的现象 / 预期结果**：两个角点反算应得约 `(0, 0)` 与 `(320, 240)`（整数截断允许 ±1），中心点约 `(161, 120)`；`vbat_mv(2000, 1650)` ≈ `3180` mV（(6600×2000)>>12 = 3222，再乘 1630/1650 ≈ 3183，取整后约 3180~3183，随整数除法次序略有出入——这也正是源码把 `>>12` 提前做的用意：先缩再乘防溢出）。有真机者可进一步：① CONFIG → TOUCH CAL 重采校准、CONFIG → TOUCH TEST 画线验证（`touch_draw_test()`，[ui.c:339-364](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L339-L364)）；② USB 串口执行 `vbat` 对比屏幕电池格数。Python 侧结果可直接本地验证，真机部分**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `adc_single_read` 选 239.5 周期采样，而看门狗模式只用 1.5 周期（[adc.c:67](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/adc.c#L67)、[adc.c:119](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/adc.c#L119)）？
**答**：测坐标时信号源是电阻屏分压——内阻高达几十 kΩ，采样电容充电慢，必须长采样时间才准；看门狗只需判断「是否超过 2000」的粗阈值，且由 1kHz 定时器持续触发，短采样降低每次转换占用，让 TIM3 触发的连续转换不与前次重叠。

**练习 2**：把 `TOUCH_THRESHOLD` 从 2000 改成 3000，会有什么可感知的变化？
**答**：轻触（耦合电压较低，如按在屏幕边缘、按得轻）可能低于 3000 而不再触发 AWD 中断——表现为「轻点无响应」；抗噪能力则略有提升。这是一个灵敏度/可靠性的折中旋钮。

**练习 3**：`adc_vbat_read` 里为什么先 `adc_stop()`、末尾 `touch_start_watchdog()`，中间还要动 `ADC->CCR`？
**答**：同一 ADC 三用。电池测量用的是内部通道（CH17/18），需在 CCR 打开 VREFEN/VBATEN 使能内部基准与 VBAT 分压；这些单发测量要求先停掉看门狗的连续转换（ADSTART 未清不能改配置），测完关掉省电的外设并恢复触摸监测——完整的「借用-归还」闭环。

## 5. 综合实践

**任务：给顶层菜单加一个 STATUS 子菜单，把「扫描点数 / 带宽档 / 电池电压」做成一页状态信息。** 这个任务串起本讲全部三个模块：菜单表定义（4.2）、回调里的 ADC 借用-归还（4.4）、以及背后的事件模型（4.1）。

1. **定义子菜单表**（放在 `menu_top[]` 之前）：

   ```c
   /* 示例代码：综合实践新增 */
   static void menu_status_info_cb(int item, uint8_t data);

   const menuitem_t menu_status[] = {
     { MT_CALLBACK, 0, "INFO", menu_status_info_cb },
     { MT_CANCEL, 0, S_LARROW" BACK", NULL },
     { MT_NONE, 0, NULL, NULL } // sentinel
   };
   ```

2. **写回调**：参考 4.2.4 的 `menu_display_info_cb`，在等待循环之前多加一行电池读数（注意顺序——`adc_vbat_read` 内部自带 stop/start，需放在 `adc_stop()` 之后、触摸等待循环之前）：

   ```c
   int16_t vbat = adc_vbat_read();
   if (vbat > 0) {
     plot_printf(buf, sizeof buf, "VBAT: %d mV", vbat);
     ili9341_drawstring(buf, OFFSETX, 130);
   }
   ```

3. **挂到顶层**：在 `menu_top[]`（[ui.c:1050-1058](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L1050-L1058)）的 CONFIG 项之后加 `{ MT_SUBMENU, 0, "STATUS", menu_status },`。顶层表当前 6 项 + 哨兵，加 1 项共 7 项，未超 `MENU_BUTTON_MAX`。
4. **编译验证**：`make`（或 docker 方式）通过后烧录；真机操作 MENU → STATUS → INFO，应看到三行状态，触摸退出后界面完整恢复。
5. **检查单**（自测）：回调是否遵守了 adc_stop → 用 ADC → touch_start_watchdog 的顺序？退出路径是否 `redraw_frame()+request_to_redraw_grid()+draw_menu()` 三件套（对照 `menu_config_cb`，[ui.c:490-507](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L490-L507)）？等待期间扫频是否暂停（对照 u2-l5 的单线程模型解释原因）？

无硬件读者的替代验证：只做第 1~3 步 + `make` 编译通过，并把 STATUS 菜单的预期调用链写成时序（`menu_invoke → menuaction_cb → adc_vbat_read → draw_battery... 之类`，实际是本回调直接画字），运行效果**待本地验证**。

## 6. 本讲小结

- **异步请求模型**：EXTI（拨轮）与 ADC 模拟看门狗（触摸）两个中断源都只做 `operation_requested |= OP_xxx`，真正的处理在 sweep 线程的 `ui_process()` 里完成——因为触摸测量含 14ms 级睡眠，绝不能进中断；这与 `shell_function`、`redraw_request` 共同构成固件统一的无锁并发风格。
- **表驱动菜单**：`menuitem_t{type,data,label,reference}` 四字段 + `MT_*` 六类型 + 4 层菜单栈，把几十个菜单项压缩成 Flash 里的 `const` 数据表；加功能 = 加一行表 + 写一个回调。回调取参有 `item` 下标（位置敏感）与 `data` 字段（位置无关）两种风格，后者更安全。
- **四模式输入**：NORMAL（拨轮五模式杠杆）/ MENU（选择+执行）/ NUMERIC（旋钮按 `10^digit` 逐位调、定点整数域）/ KEYPAD（字符串→my_atof→单位倍率→写回）；长按与单击是 NUMERIC/KEYPAD 的分流开关。
- **电阻触摸**：一层加电压梯度、另一层浮空读分压即得坐标；两点校准用 ×16 定点斜率把原始读数映射到 320×240 像素；TIM3 每 10ms 硬件触发 ADC + [0,2000] 看门狗窗口，实现零 CPU 轮询、最坏 10ms 延迟的按压检测。
- **ADC 三用的借用-归还纪律**：看门狗监测、坐标测量、电池单发测量共享 ADC1，`adc_stop() → 使用 → touch_start_watchdog()` 的顺序在每个消费者里都严格成立，自写代码也必须遵守。
- **电池链路**：VBAT(2:1 分压) 与 VREFINT 双通道读数 + 出厂校准值换算毫伏，低于 100mV 判无电池；消费方是扫完触发 `REDRAW_BATTERY` 的电池图标与 `vbat` shell 命令。

## 7. 下一步学习建议

本讲完，显示与交互单元（u4）全部结束，你已具备给这台仪器「改脸」的能力。接下来进入最后一单元（u5）：

1. **u5-l1（USB CDC Shell）**：回到 main.c，看另一条输入通道——自研 shell 的命令表（`commands[]`）与 `CMD_WAIT_MUTEX` 跨线程执行。你会发现它和本讲的 `menuitem_t` 是同一种表驱动思想的孪生实现，对比两者是很好的复习。
2. **u5-l2（Python 上位机）**：用 `capture` 命令抓屏、`scan`/`data` 抓数据，把本讲的屏幕交互换成脚本自动化。
3. **u5-l4（二次开发实战）**：毕业项目会要求同时动 shell 命令、properties_t 持久化与 UI——本讲的菜单实践正是其中 UI 侧的直接预备。
4. 若想继续深挖交互，可自行阅读 [ui.c:374-397](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L374-L397) 的 `show_version`（多行信息页的画法）与 [ui.c:399-415](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L399-L415) 的 `enter_dfu`（菜单回调触发系统复位的例子，与 u1-l2 的 DFU 烧录流程首尾呼应）。
