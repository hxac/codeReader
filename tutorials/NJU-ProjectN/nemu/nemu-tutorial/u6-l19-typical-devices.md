# 典型外设实现

## 1. 本讲目标

上一讲（u6-l18）我们搭好了 NEMU 的**通用设备框架**：`IOMap` 把一段地址区间绑定到一个设备缓冲与一个回调，`map_read/map_write` 负责数据搬运与回调触发，`add_mmio_map/add_pio_map` 负责注册，`init_device` 负责装配。但那套框架是「骨架」——真正让模拟器看起来像一台「有屏幕、有键盘、能打印」的计算机的，是骨架上挂着的具体外设。

本讲就拆解四个最典型的外设：**串口（serial）、定时器（timer/RTC）、键盘（i8042）、VGA 显示**。读完本讲你应当能够：

- 掌握 `io_handler` 回调「读写设备寄存器」的统一编程模式，并能区分读/写两种场景下回调的不同职责。
- 说清串口、RTC、键盘、VGA 各自暴露给客机程序的**寄存器语义**（哪个 offset 干什么）。
- 理解 NEMU 如何用宿主机的 **SDL 窗口、stderr、系统时钟** 把抽象的客机外设「呈现」给真实的人看。
- 动手完成 VGA 的 `vga_update_screen`，并验证串口输出路径。

## 2. 前置知识

本讲建立在你已学完 u6-l18 的基础上，这里只做最关键的回顾，不重复细节。

**回调签名**：每个设备注册时可以挂一个回调，类型为（见 [include/device/map.h:L21-L21](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/device/map.h#L21-L21)）

```c
typedef void(*io_callback_t)(uint32_t offset, int len, bool is_write);
```

三个参数分别是：相对于设备基址的偏移、访问宽度、是否为写操作。设备逻辑就写在这个回调里。

**读写的非对称时序**（u6-l18 的核心结论，本讲反复用到）：

- `map_read`：**先回调，后读缓冲**（见 [src/device/io/map.c:L55-L62](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/map.c#L55-L62)）。回调负责「准备数据」（比如把当前时间写进缓冲），随后框架才把缓冲读出来交给客机。
- `map_write`：**先写缓冲，后回调**（见 [src/device/io/map.c:L64-L70](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/map.c#L64-L70)）。框架先把客机要写的值落进缓冲，回调再据此「做出反应」（比如把字符打印出来）。

记住这条时序，后面四个设备的回调读起来就顺了：**读设备的回调 = 准备数据；写设备的回调 = 产生副作用**。

**两条映射通道**：x86 有独立端口 I/O，用 `add_pio_map`；其余 ISA 走内存映射，用 `add_mmio_map`。同一份设备代码用 `CONFIG_HAS_PORT_IO` 宏在两者间切换（见各设备的 `init_xxx`）。地址等配置项都在 [src/device/Kconfig](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/Kconfig) 里。

**相关术语**：

- **MMIO**（Memory-Mapped I/O）：用访存指令访问设备寄存器。
- **PIO**（Port I/O）：x86 专用，用 `in/out` 指令访问独立端口地址空间。
- **scancode**：按键的编码值。
- **framebuffer / vmem**：帧缓冲，存放屏幕每个像素颜色的内存。
- **SDL**：Simple DirectMedia Layer，一个跨平台的多媒体库，NEMU 用它创建窗口、绘制像素、捕获键盘事件。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/device/serial.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/serial.c) | 串口：客机写字符 → 宿主 stderr 输出 |
| [src/device/timer.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/timer.c) | RTC：客机读时间 → 返回宿主开机微秒数；并触发时钟中断 |
| [src/device/keyboard.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/keyboard.c) | 键盘 i8042：SDL 按键入队 → 客机读数据口出队 |
| [src/device/vga.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/vga.c) | VGA：vmem 像素缓冲 + vgactl 控制/同步寄存器 |
| [src/device/device.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c) | `device_update`：节流刷新 VGA + 抽取 SDL 事件；`init_device` 装配所有外设 |
| [src/device/io/map.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/map.c) | `map_read/map_write`：搬运数据 + 触发回调（u6-l18 已讲） |
| [src/device/Kconfig](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/Kconfig) | 各设备的端口/MMIO 地址与开关 |

---

## 4. 核心概念与源码讲解

### 4.1 串口 serial——把客机字符流导向宿主 stderr

#### 4.1.1 概念说明

串口是嵌入式系统里最朴素的外设：CPU 往一个寄存器里写一个字节，硬件就把这个字节通过串行线一位一位地发出去，对端收到后显示成字符。在真实硬件里这是 16550 UART（一种通用异步收发器），代码注释也写明兼容 16550（见 [src/device/serial.c:L19-L20](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/serial.c#L19-L20)）。

在 NEMU 里我们没有真实的串行线，所以把「发出去的字符」直接送到宿主机的 **stderr**（标准错误流）。这样客机程序里一句 `putchar('H')`，最终就会出现在你运行 NEMU 的终端上——这就是 PA 里 `printf` 能「看见」输出的底层支撑。

NEMU 的串口被刻意做得**极简**：只支持「写一个字符」这一个动作，连读都不支持。

#### 4.1.2 核心流程

客机执行一条「向串口地址写字节」的指令时：

1. `paddr_write`（或 `pio_write`）按地址路由到串口的 `IOMap`。
2. `map_write` 先把这个字节写进 `serial_base[0]` 缓冲。
3. `map_write` 随后调用回调 `serial_io_handler(offset=0, len=1, is_write=true)`。
4. 回调发现是写、offset 是 0，调用 `serial_putc(serial_base[0])`。
5. `serial_putc` 用 `putc(ch, stderr)` 把字符吐到宿主 stderr。

读串口则直接 `panic`，因为 NEMU 串口不支持输入。

#### 4.1.3 源码精读

设备缓冲与字符输出函数（[src/device/serial.c:L22-L29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/serial.c#L22-L29)）：

```c
#define CH_OFFSET 0
static uint8_t *serial_base = NULL;

static void serial_putc(char ch) {
  MUXDEF(CONFIG_TARGET_AM, putch(ch), putc(ch, stderr));
}
```

`CH_OFFSET` 给出「数据寄存器」在设备内的偏移量 0。`serial_putc` 用 `MUXDEF` 在两种宿主环境下二选一：AM 模式调 AM 提供的 `putch`，native 模式调标准库 `putc` 输出到 **stderr**。选 stderr 而非 stdout，是因为 stdout 往往带缓冲且可能与客机自身的输出混在一起，stderr 不缓冲、即时可见，更适合做调试输出通道。

回调本体（[src/device/serial.c:L31-L41](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/serial.c#L31-L41)）：

```c
static void serial_io_handler(uint32_t offset, int len, bool is_write) {
  assert(len == 1);
  switch (offset) {
    case CH_OFFSET:
      if (is_write) serial_putc(serial_base[0]);
      else panic("do not support read");
      break;
    default: panic("do not support offset = %d", offset);
  }
}
```

注意三个细节：第一，`assert(len == 1)` 强制串口只能一字节一字节地写，符合「逐字符」语义；第二，只有 `is_write` 时才输出，读直接 panic；第三，只有 offset 0 合法，其它偏移（真实 16550 还有一堆线路控制寄存器）一律 panic——这是教学取舍，够用即可。

注册函数（[src/device/serial.c:L43-L51](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/serial.c#L43-L51)）：

```c
void init_serial() {
  serial_base = new_space(8);
#ifdef CONFIG_HAS_PORT_IO
  add_pio_map ("serial", CONFIG_SERIAL_PORT, serial_base, 8, serial_io_handler);
#else
  add_mmio_map("serial", CONFIG_SERIAL_MMIO, serial_base, 8, serial_io_handler);
#endif
}
```

`new_space(8)` 从 `io_space` 池切 8 字节（虽然只用了 offset 0，留出余量是给「假装自己是 16550」的余地）。x86 走 PIO，基址 `CONFIG_SERIAL_PORT` 默认 `0x3f8`（这正是 PC 上第一串口 COM1 的经典端口，见 [src/device/Kconfig:L20-L24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/Kconfig#L20-L24)）；其余 ISA 走 MMIO，基址 `CONFIG_SERIAL_MMIO` 默认 `0xa00003f8`（见 [src/device/Kconfig:L25-L27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/Kconfig#L25-L27)）。

#### 4.1.4 代码实践

**实践目标**：验证串口「写一字节 → stderr 一字符」的全链路。

**操作步骤（源码阅读型 + 可选运行）**：

1. 在 [src/device/serial.c:L36](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/serial.c#L36-L36) 的 `serial_putc` 调用处，确认它读的是 `serial_base[0]`，这正是 `map_write` 第一步落进缓冲的那个字节。
2. 回溯 `map_write`（[src/device/io/map.c:L64-L70](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/map.c#L64-L70)），口述一遍「先 host_write 落缓冲、再 invoke_callback」的顺序，解释为什么回调里能直接从 `serial_base[0]` 取到刚写的值。
3. 若本地已能编译运行 NEMU：写一段最简客机程序（或用 PA 提供的 AM 程序），让它向串口地址循环写 `"Hello\n"` 的每个字节，运行后观察终端 stderr 是否打印出 `Hello`。若暂无条件运行，标注「待本地验证」。

**需要观察的现象**：每写一个字节，终端就出现一个字符；字符出现在 stderr 流上（可用 `./nemu ... 2>err.log` 把 stderr 重定向到文件单独查看）。

**预期结果**：客机写入的字节序列原样出现在 stderr。

#### 4.1.5 小练习与答案

**练习 1**：为什么串口的回调里对「读」操作直接 `panic`，而不是返回某个默认值？

**参考答案**：因为 NEMU 的串口被设计成纯输出设备（16550 的发送保持寄存器），没有实现输入路径；客机一旦尝试读，说明程序行为与 NEMU 的设备模型不符，属于「不该发生」的访问，用 `panic` 立即暴露问题比默默返回错误值更有利于调试。

**练习 2**：若把 `serial_putc` 里的 `stderr` 改成 `stdout`，运行一个会大量 `printf` 的客机程序，可能出现什么不好的现象？

**参考答案**：stdout 默认是行缓冲或全缓冲，客机输出的字符可能积在缓冲里不及时显示，且会与 NEMU 自身打印到 stdout 的信息交织错乱；用 stderr 不缓冲，字符即时可见，调试更可靠。

---

### 4.2 定时器 timer / RTC——按需读取宿主时间并触发时钟中断

#### 4.2.1 概念说明

RTC（Real-Time Clock，实时时钟）给客机程序提供一个「当前时间」。NEMU 没有真实时钟硬件，于是把宿主机的墙钟时间换算成「自 NEMU 启动以来的微秒数」交给客机。这个时间值是 64 位的，而客机一次最多读 32 位（4 字节），所以需要拆成高低两个 32 位半字，放在两个寄存器里。

同时，定时器还承担另一个关键职责：**周期性地触发时钟中断**，驱动多道程序运行。这部分通过 `add_alarm_handle` 注册一个回调，由 u6-l20 要讲的 SIGVTALRM 信号驱动。本讲只看「读时间」这条数据路径。

#### 4.2.2 核心流程

读时间的精妙之处在于「**读高半字时刷新、读低半字时不刷新**」：

1. 客机读 offset 4（高半字）。`map_read` 先调 `rtc_io_handler(offset=4, is_write=false)`。
2. 回调调用 `get_time()` 取当前微秒数 `us`，把低 32 位写进 `rtc_port_base[0]`、高 32 位写进 `rtc_port_base[1]`。
3. `map_read` 再把 `rtc_port_base[1]`（即 `space+4`）读出来返回给客机。
4. 客机紧接着读 offset 0（低半字）。此时回调不再刷新（offset≠4），`map_read` 直接返回 `rtc_port_base[0]`。

这样**先读高半字、再读低半字**，得到的两个半字就来自同一次 `get_time()` 采样，拼接后才是一个自洽的 64 位时间值，避免了「读高半字与读低半字之间时间已经变化」的撕裂（tearing）问题。

时钟中断路径（u6-l20 展开）：

1. 宿主 SIGVTALRM 信号每 \(1000000/\text{TIMER\_HZ}\) 微秒触发一次（`TIMER_HZ=60`，即约 60 Hz）。
2. 信号处理函数遍历 `handler[]`，调用 `timer_intr`。
3. `timer_intr` 若发现 NEMU 正在运行，就调用 `dev_raise_intr()`（当前为空 stub，留待学生实现）。

#### 4.2.3 源码精读

设备缓冲与回调（[src/device/timer.c:L20-L29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/timer.c#L20-L29)）：

```c
static uint32_t *rtc_port_base = NULL;

static void rtc_io_handler(uint32_t offset, int len, bool is_write) {
  assert(offset == 0 || offset == 4);
  if (!is_write && offset == 4) {
    uint64_t us = get_time();
    rtc_port_base[0] = (uint32_t)us;
    rtc_port_base[1] = us >> 32;
  }
}
```

`assert(offset == 0 || offset == 4)` 把合法偏移钉死在两个 4 字节寄存器上。关键是那个 `if (!is_write && offset == 4)`——**只有「读高半字」时才刷新时间**。`us >> 32` 取高 32 位，`(uint32_t)us` 截断取低 32 位。

这正是 u6-l18 「读：回调先准备数据」的典型用例：`map_read` 会先调这个回调把最新时间写进缓冲，再 `host_read` 把缓冲读出来（见 [src/device/io/map.c:L55-L62](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/map.c#L55-L62)）。所以「读 offset 4」其实返回的是「刚被回调刷新过的高半字」。

时钟中断回调（[src/device/timer.c:L31-L38](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/timer.c#L31-L38)）：

```c
#ifndef CONFIG_TARGET_AM
static void timer_intr() {
  if (nemu_state.state == NEMU_RUNNING) {
    extern void dev_raise_intr();
    dev_raise_intr();
  }
}
#endif
```

注意 `#ifndef CONFIG_TARGET_AM`：AM 模式下 NEMU 不自己模拟时钟中断（AM 有自己的 `io_read(AM_TIMER_UPTIME)`），所以这段被排除。`dev_raise_intr()` 的实现在 [src/device/intr.c:L18-L19](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/intr.c#L18-L19) 目前是空函数——这是 PA 留给你实现中断挂起的地方，本讲先记住这个钩子存在。

注册与挂中断（[src/device/timer.c:L40-L48](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/timer.c#L40-L48)）：

```c
void init_timer() {
  rtc_port_base = (uint32_t *)new_space(8);
#ifdef CONFIG_HAS_PORT_IO
  add_pio_map ("rtc", CONFIG_RTC_PORT, rtc_port_base, 8, rtc_io_handler);
#else
  add_mmio_map("rtc", CONFIG_RTC_MMIO, rtc_port_base, 8, rtc_io_handler);
#endif
  IFNDEF(CONFIG_TARGET_AM, add_alarm_handle(timer_intr));
}
```

8 字节正好放两个 `uint32_t`。最后一行把 `timer_intr` 注册进 alarm 的处理函数表（见 [src/device/alarm.c:L26-L29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/alarm.c#L26-L29) 的 `add_alarm_handle`），native 模式专用。

`get_time()` 的实现在 [src/utils/timer.c:L41-L45](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/timer.c#L41-L45)，它用 `boot_time` 记录首次调用时刻，之后每次返回「当前时刻 − 启动时刻」的微秒差，所以 RTC 报告的是「NEMU 启动以来流逝的微秒数」，而非绝对墙钟时间。

#### 4.2.4 代码实践

**实践目标**：理解「先读高半字刷新、后读低半字」如何保证时间一致性。

**操作步骤（源码阅读型）**：

1. 假设客机先读 offset 4、再读 offset 0，分别跟踪两次 `map_read` 的调用，说明哪一次触发了 `get_time()`、哪一次没有。
2. 思考：如果回调写成「读 offset 0 也刷新」，会发生什么问题？（提示：撕裂）
3. 进阶：阅读 [src/utils/timer.c:L26-L45](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/timer.c#L26-L45)，说明为何首次调用 `get_time` 时 `boot_time` 才被赋值，这是「惰性初始化」。

**需要观察的现象**：两次读返回的值拼成的 64 位数单调递增，且不会出现「低半字远大于高半字对应范围」的异常跳变。

**预期结果**：按「先 4 后 0」的顺序读，能得到稳定的、自洽的时间读数；若颠倒顺序或两次都读 0，时间值会不自洽。**待本地验证**：可写一段客机程序循环读 RTC 并打印，观察数值。

#### 4.2.5 小练习与答案

**练习 1**：`rtc_io_handler` 里为什么用 `if (!is_write && offset == 4)` 而不是 `if (offset == 4)`？

**参考答案**：因为客机「写」offset 4（比如清寄存器）时不应当触发时间刷新——刷新只在「读」时才有意义（要给客机返回新数据）。加上 `!is_write` 把写操作排除，避免写寄存器意外触发 `get_time()`。

**练习 2**：`timer_intr` 为什么要先判断 `nemu_state.state == NEMU_RUNNING` 才调 `dev_raise_intr()`？

**参考答案**：因为 SIGVTALRM 信号是异步的，可能在 NEMU 已经停下（如命中断点、HIT TRAP、QUIT）之后仍被投递。若不判断状态就抛中断，会在 CPU 不该运行时错误地挂起中断，破坏状态机；先判断保证「只在真正运行时才记一笔中断」。

---

### 4.3 键盘 i8042——SDL 按键入队、客机轮询出队

#### 4.3.1 概念说明

键盘是典型的「**异步生产、同步消费**」设备：按键事件随时发生（人随时可能敲键），但 CPU 只在它主动去读键盘寄存器时才能拿到数据。真实硬件用 i8042 键盘控制器芯片做缓冲，NEMU 用一个**环形队列**模拟这个缓冲。

这里有一个关键的「解耦」设计：

- **生产端**：宿主机的 SDL 在窗口里捕获按键，转成 NEMU 内部 scancode，塞进队列——这件事发生在 `device_update` 里，与 CPU 执行指令异步。
- **消费端**：客机程序读 i8042 数据口时，回调从队列里取一个 scancode 返回——这件事由客机的读指令同步触发。

两端通过队列解耦，互不阻塞。NEMU 还做了一层**编码映射**：SDL 的 scancode（与平台相关）先映射成统一的「AM scancode」（与平台无关的抽象键码），再交给客机，保证客机代码可移植。

#### 4.3.2 核心流程

按键从敲下到被客机读到的完整链路：

1. `device_update` 在 SDL 事件循环里收到 `SDL_KEYDOWN/KEYUP`（见 [src/device/device.c:L53-L61](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c#L53-L61)），调用 `send_key(scancode, is_keydown)`。
2. `send_key` 用 `keymap[scancode]` 把 SDL scancode 翻译成 AM scancode，用 `KEYDOWN_MASK` 标记按下/抬起，`key_enqueue` 入队。
3. 客机执行「读 i8042 数据口」指令，`map_read` 先调 `i8042_data_io_handler`。
4. 回调调 `key_dequeue()`，把队首 scancode 写进 `i8042_data_port_base[0]`（队列空则写 `NEMU_KEY_NONE=0`）。
5. `map_read` 把这个值读出来返回给客机。

#### 4.3.3 源码精读

**AM scancode 枚举**用 X-macro 生成（[src/device/keyboard.c:L25-L39](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/keyboard.c#L25-L39)）：

```c
#define NEMU_KEYS(f) \
  f(ESCAPE) f(F1) f(F2) ... f(Z) f(X) ... f(UP) f(DOWN) f(LEFT) f(RIGHT) ...

#define NEMU_KEY_NAME(k) NEMU_KEY_ ## k,

enum {
  NEMU_KEY_NONE = 0,
  MAP(NEMU_KEYS, NEMU_KEY_NAME)
};
```

`NEMU_KEYS` 是一个「键名列表」宏，对每个键名调用传入的 `f`。把 `f` 换成 `NEMU_KEY_NAME`，就展开成 `NEMU_KEY_ESCAPE, NEMU_KEY_F1, ...`，于是 `enum` 自动给每个键分配 1、2、3……这些就是平台无关的 AM scancode（关于 `MAP` 宏本身见 u8-l26）。同一份 `NEMU_KEYS` 后面还会复用，避免键名表写两遍。

**SDL→AM 映射表**（[src/device/keyboard.c:L41-L46](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/keyboard.c#L41-L46)）：

```c
#define SDL_KEYMAP(k) keymap[SDL_SCANCODE_ ## k] = NEMU_KEY_ ## k;
static uint32_t keymap[256] = {};

static void init_keymap() {
  MAP(NEMU_KEYS, SDL_KEYMAP)
}
```

这里 `f` 换成 `SDL_KEYMAP`，对每个键展开成 `keymap[SDL_SCANCODE_ESCAPE] = NEMU_KEY_ESCAPE;`，一次性把整张映射表填好。`keymap` 以 SDL scancode 为下标、AM scancode 为值。

**环形队列**（[src/device/keyboard.c:L48-L65](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/keyboard.c#L48-L65)）：

```c
#define KEY_QUEUE_LEN 1024
static int key_queue[KEY_QUEUE_LEN] = {};
static int key_f = 0, key_r = 0;          // front / rear

static void key_enqueue(uint32_t am_scancode) {
  key_queue[key_r] = am_scancode;
  key_r = (key_r + 1) % KEY_QUEUE_LEN;
  Assert(key_r != key_f, "key queue overflow!");
}

static uint32_t key_dequeue() {
  uint32_t key = NEMU_KEY_NONE;
  if (key_f != key_r) {                   // 非空才取
    key = key_queue[key_f];
    key_f = (key_f + 1) % KEY_QUEUE_LEN;
  }
  return key;
}
```

经典的循环数组队列：`key_f` 指向队首、`key_r` 指向下一个写入位，`% KEY_QUEUE_LEN` 实现回绕。`key_f == key_r` 表示空。`key_enqueue` 写 rear 后推进，并断言没有追上 front（溢出）；`key_dequeue` 空时返回 `NEMU_KEY_NONE`，这是「无按键」的哨兵值。

**生产端**（[src/device/keyboard.c:L67-L72](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/keyboard.c#L67-L72)）：

```c
void send_key(uint8_t scancode, bool is_keydown) {
  if (nemu_state.state == NEMU_RUNNING && keymap[scancode] != NEMU_KEY_NONE) {
    uint32_t am_scancode = keymap[scancode] | (is_keydown ? KEYDOWN_MASK : 0);
    key_enqueue(am_scancode);
  }
}
```

两个守卫：NEMU 必须在运行、且该 SDL scancode 必须在映射表里有对应项（否则丢弃）。`KEYDOWN_MASK = 0x8000`（见 [src/device/keyboard.c:L19-L19](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/keyboard.c#L19-L19)）占用 AM scancode 的最高位：1 表示按下、0 表示抬起，低 15 位是键码。这样一次「按键事件」既包含「哪个键」也包含「按下还是抬起」。

**消费端（数据口回调）**（[src/device/keyboard.c:L85-L89](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/keyboard.c#L85-L89)）：

```c
static void i8042_data_io_handler(uint32_t offset, int len, bool is_write) {
  assert(!is_write);
  assert(offset == 0);
  i8042_data_port_base[0] = key_dequeue();
}
```

只允许读 offset 0：每次读都出队一个按键。注意这里没有「状态寄存器」告诉你「现在有没有按键」——客机只能不停地读，读到 0（`NEMU_KEY_NONE`）就知道没按键。这是 NEMU 对真实 i8042 的简化（真实 i8042 有状态口指示缓冲是否为空）。

**AM 模式的另一条路**（[src/device/keyboard.c:L73-L81](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/keyboard.c#L73-L81)）：当 `CONFIG_TARGET_AM` 定义时，键盘不自己接 SDL，而是直接调 AM 的 `io_read(AM_INPUT_KEYBRD)` 拿事件——因为 AM 模式下 NEMU 跑在另一个 native 平台上，键盘由那个平台负责。

注册（[src/device/keyboard.c:L91-L100](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/keyboard.c#L91-L100)）：`new_space(4)`、初始值置 `NEMU_KEY_NONE`、按 ISA 选 PIO/MMIO（端口 `0x60` / MMIO `0xa0000060`）、native 下 `init_keymap()`。

#### 4.3.4 代码实践

**实践目标**：用源码阅读追踪「一次按键 → 一次读口」的完整数据流。

**操作步骤**：

1. 打开 [src/device/device.c:L36-L67](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c#L36-L67)，找到 `SDL_KEYDOWN` 分支如何调用 `send_key`。
2. 跟踪 `send_key`（[src/device/keyboard.c:L67-L72](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/keyboard.c#L67-L72)）→ `key_enqueue`，画出一个按键 scancode 经过「SDL scancode → keymap → AM scancode | KEYDOWN_MASK → key_queue」的变换链。
3. 跟踪客机读数据口：`paddr_read` → `map_read` → `i8042_data_io_handler` → `key_dequeue`，说明每次读会从队列拿走一个元素。
4. 思考：如果客机读口的速度慢于按键速度，会发生什么？（队列积压直至溢出 Assert）

**需要观察的现象**：每次读数据口恰好返回一个按键事件；队列空时返回 0。

**预期结果**：能口述完整链路并解释 `KEYDOWN_MASK` 的作用。**待本地验证**：开启 SDL 窗口运行一个会读键盘的客机程序，敲键观察其反应。

#### 4.3.5 小练习与答案

**练习 1**：为什么需要 `keymap` 这层映射，而不是直接把 SDL scancode 放进队列给客机？

**参考答案**：SDL scancode 依赖宿主平台与 SDL 版本，若直接暴露给客机，客机程序就必须知道 SDL 的编码细节，无法跨平台移植。映射成 NEMU 自定义的 AM scancode 后，客机面对的是一套稳定、与宿主无关的键码，可移植性更好。

**练习 2**：`key_dequeue` 在队列为空时返回 `NEMU_KEY_NONE`（0）。这种「以 0 表示无数据」的设计有什么隐患？真实 i8042 是怎么解决的？

**参考答案**：隐患是客机无法区分「真的没按键」和「读到了值为 0 的按键」——不过此处 0 恰好就是 `NEMU_KEY_NONE`，所以语义自洽；但代价是客机必须轮询（忙等）读口，浪费 CPU。真实 i8042 提供独立的状态寄存器（位 0 表示输出缓冲满，即「有数据可读」），客机先查状态再读数据，避免盲目轮询。NEMU 为简化省去了状态口。

---

### 4.4 VGA 显示——vmem 帧缓冲 + vgactl 控制寄存器

#### 4.4.1 概念说明

VGA 让客机程序能在屏幕上画图。它的模型很直白：开辟一大块内存叫**帧缓冲 vmem**，每个像素占 4 字节（ARGB 各 8 位），程序把每个像素的颜色写进 vmem，显示设备就据此刷新屏幕。屏幕分辨率由配置决定（默认 400×300，可选 800×600）。

NEMU 的 VGA 暴露给客机**两段地址**：

- **vmem**（帧缓冲）：一大块只写 MMIO（默认基址 `0xa1000000`），客机往里写像素。
- **vgactl**（控制寄存器）：8 字节，offset 0 是「只读」的分辨率信息，offset 4 是「只写」的同步（sync）命令。

这里有个设计上的不对称：**vmem 没有 callback**（写像素只是把数据落进缓冲，不需要即时副作用），而**像素真正显示到屏幕**这件事，由 `device_update` 周期性地调用 `vga_update_screen` 完成。客机写完一帧像素后，往 sync 寄存器写一个非零值表示「这一帧画完了，请刷新」，`vga_update_screen` 检测到 sync 非零就把 vmem 推上屏幕、并清零 sync。这个 sync 寄存器相当于一个「flush / 刷新」命令。

#### 4.4.2 核心流程

画一帧并显示出来的流程：

1. 客机计算每个像素颜色，逐个写入 vmem（MMIO 写，无 callback，直接落缓冲）。
2. 客机往 vgactl offset 4 写一个非零值（sync 命令）。
3. CPU 主循环每执行一条指令都会调 `device_update`（见 u3-l9），`device_update` 节流到约 60Hz 后调 `vga_update_screen`。
4. `vga_update_screen`（**待实现**）发现 sync≠0，调 `update_screen()` 把 vmem 渲染到 SDL 窗口，再把 sync 清零。

分辨率查询：客机读 vgactl offset 0，返回 `(width << 16) | height`（高 16 位宽、低 16 位高）。

#### 4.4.3 源码精读

**屏幕尺寸**（[src/device/vga.c:L19-L32](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/vga.c#L19-L32)）：

```c
#define SCREEN_W (MUXDEF(CONFIG_VGA_SIZE_800x600, 800, 400))
#define SCREEN_H (MUXDEF(CONFIG_VGA_SIZE_800x600, 600, 300))

static uint32_t screen_size() {
  return screen_width() * screen_height() * sizeof(uint32_t);   // 每像素 4 字节
}
```

`MUXDEF` 据 `CONFIG_VGA_SIZE_*` 选尺寸（u8-l26 讲宏体系）。`screen_size()` 给出 vmem 的字节数：宽 × 高 × 4。

**两个全局缓冲**（[src/device/vga.c:L34-L35](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/vga.c#L34-L35)）：`vmem`（像素缓冲）与 `vgactl_port_base`（8 字节控制/同步寄存器）。

**待实现的刷新函数**（[src/device/vga.c:L74-L77](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/vga.c#L74-L77)）：

```c
void vga_update_screen() {
  // TODO: call `update_screen()` when the sync register is non-zero,
  // then zero out the sync register
}
```

这正是本讲综合实践要填的空。注意 `update_screen()` 只在 `CONFIG_VGA_SHOW_SCREEN` 下才有定义（见下节），所以填充时要带条件编译保护。

**注册流程**（[src/device/vga.c:L79-L92](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/vga.c#L79-L92)）：

```c
void init_vga() {
  vgactl_port_base = (uint32_t *)new_space(8);
  vgactl_port_base[0] = (screen_width() << 16) | screen_height();
#ifdef CONFIG_HAS_PORT_IO
  add_pio_map ("vgactl", CONFIG_VGA_CTL_PORT, vgactl_port_base, 8, NULL);
#else
  add_mmio_map("vgactl", CONFIG_VGA_CTL_MMIO, vgactl_port_base, 8, NULL);
#endif

  vmem = new_space(screen_size());
  add_mmio_map("vmem", CONFIG_FB_ADDR, vmem, screen_size(), NULL);
  IFDEF(CONFIG_VGA_SHOW_SCREEN, init_screen());
  IFDEF(CONFIG_VGA_SHOW_SCREEN, memset(vmem, 0, screen_size()));
}
```

要点：

- `vgactl_port_base[0]` 初始化为打包后的分辨率，供客机只读；`[1]`（sync）初值为 0。
- vgactl 的 callback 是 **`NULL`**——读分辨率只是读缓冲、写 sync 只是写缓冲，都不需要副作用，所以不挂回调。
- **vmem 只用 MMIO**（`add_mmio_map`，无 PIO 分支），基址 `CONFIG_FB_ADDR` 默认 `0xa1000000`，callback 也是 `NULL`：写像素就是单纯写内存。
- `init_screen()` 创建 SDL 窗口，仅 `CONFIG_VGA_SHOW_SCREEN` 时执行。

**`device_update` 的节流调度**（[src/device/device.c:L36-L45](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c#L36-L45)）：

```c
void device_update() {
  static uint64_t last = 0;
  uint64_t now = get_time();
  if (now - last < 1000000 / TIMER_HZ) {
    return;                          // 距上次刷新不足一帧，跳过
  }
  last = now;

  IFDEF(CONFIG_HAS_VGA, vga_update_screen());
  ...
}
```

`1000000 / TIMER_HZ` 即 \(10^6/60 \approx 16667\) 微秒，约 60Hz。`device_update` 被 CPU 主循环**每条指令**调用一次（见 u3-l9 的 `execute` 循环），但靠这个静态 `last` 节流到每秒约 60 次真正刷新，避免无谓地把 vmem 推上屏幕。

#### 4.4.4 代码实践

**实践目标**：理解 vmem（数据面）与 sync（控制面）的分工，为下一节实现 `vga_update_screen` 做准备。

**操作步骤**：

1. 阅读 [src/device/vga.c:L88-L89](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/vga.c#L88-L89)，确认 vmem 是 callback 为 `NULL` 的纯 MMIO 区——即写像素不触发任何回调，只是改缓冲。
2. 阅读 [src/device/device.c:L36-L45](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c#L36-L45)，解释节流公式 `now - last < 1000000 / TIMER_HZ` 为何能把「每条指令调一次」降频到「每秒约 60 次」。
3. 思考：vgactl 用 `NULL` 回调，那客机写 sync 寄存器的值去了哪里？谁负责读它？（答：进了 `vgactl_port_base[1]`，由 `vga_update_screen` 读。）

**需要观察的现象**：写 sync 后，sync 值静静躺在缓冲里，直到下次 `vga_update_screen` 被调（当前是空函数，所以什么都不发生——这正是要实现的）。

**预期结果**：能说清「vmem 是数据通道、sync 是命令通道、device_update 是轮询驱动」三者的关系。

#### 4.4.5 小练习与答案

**练习 1**：为什么 vmem 的 callback 是 `NULL`，而串口的 callback 不能是 `NULL`？

**参考答案**：写像素只是把颜色数据存进缓冲，不需要任何即时动作（真正的刷新由 `vga_update_screen` 异步完成），所以无需 callback。串口则不同：每个写字节的动作都必须立刻产生「输出到 stderr」的副作用，否则字符就丢了，所以必须有 callback 在 `map_write` 末尾被触发。

**练习 2**：vgactl 的 offset 0 打包成 `(width << 16) | height`。若分辨率是 400×300，这个 32 位寄存器的值是多少？

**参考答案**：\(400 \times 2^{16} + 300 = 26214400 + 300 = 26214700\)，十六进制为 `0x0190012C`（高 16 位 `0x0190`=400，低 16 位 `0x012C`=300）。客机读出后用移位与掩码即可拆回宽高。

---

### 4.5 SDL 渲染——宿主机如何呈现客机外设行为

#### 4.5.1 概念说明

本节回答一个贯穿前四个设备的问题：**NEMU 是怎样让抽象的客机外设「被真人看见」的？**

答案是一层「**宿主呈现翻译层**」：NEMU 的设备代码把客机对抽象寄存器的读写，翻译成对宿主操作系统 API 的调用——

| 客机抽象动作 | 翻译成的宿主调用 |
|--------------|------------------|
| 向串口写字节 | `putc(ch, stderr)`（C 标准库） |
| 读 RTC 时间 | `clock_gettime` / `gettimeofday`（经 `get_time`） |
| 读键盘 | `SDL_PollEvent` 抽取按键事件 |
| 画 VGA 像素 | SDL 纹理更新 + 窗口渲染 |

SDL（Simple DirectMedia Layer）是这层里负责「窗口与图形」的库。串口与 RTC 用 C 标准库就够了，而键盘与 VGA 需要一个真正的图形窗口，于是引入 SDL。换句话说，**SDL 是宿主机替客机「扮演」键盘和显示器的那只手**。

#### 4.5.2 核心流程

SDL 在两个方向上工作：

- **输出方向（VGA 显示）**：客机像素写进 vmem → `vga_update_screen` → `update_screen` → SDL 把 vmem 作为纹理贴到窗口上 → 屏幕显示出画面。
- **输入方向（键盘捕获）**：SDL 从窗口事件队列取按键 → `device_update` 的 `SDL_PollEvent` 循环 → `send_key` → 键盘队列 → 客机读数据口。

两条方向都汇聚到 `device_update` 这一个调度点：它既刷 VGA（输出），又抽 SDL 事件（输入）。

#### 4.5.3 源码精读

**SDL 初始化与纹理**（[src/device/vga.c:L41-L57](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/vga.c#L41-L57)）：

```c
static SDL_Renderer *renderer = NULL;
static SDL_Texture  *texture  = NULL;

static void init_screen() {
  SDL_Window *window = NULL;
  char title[128];
  sprintf(title, "%s-NEMU", str(__GUEST_ISA__));
  SDL_Init(SDL_INIT_VIDEO);
  SDL_CreateWindowAndRenderer(
      SCREEN_W * (MUXDEF(CONFIG_VGA_SIZE_400x300, 2, 1)),
      SCREEN_H * (MUXDEF(CONFIG_VGA_SIZE_400x300, 2, 1)),
      0, &window, &renderer);
  SDL_SetWindowTitle(window, title);
  texture = SDL_CreateTexture(renderer, SDL_PIXELFORMAT_ARGB8888,
      SDL_TEXTUREACCESS_STATIC, SCREEN_W, SCREEN_H);
  SDL_RenderPresent(renderer);
}
```

`init_screen` 创建窗口标题（如 `riscv32-NEMU`）、窗口+渲染器、以及一张与屏幕等大的纹理。注意 400×300 模式下窗口尺寸 ×2（`MUXDEF(CONFIG_VGA_SIZE_400x300, 2, 1)`），把小屏幕放大显示，看得更清楚。像素格式 `SDL_PIXELFORMAT_ARGB8888` 正对应 vmem 里每像素 4 字节 ARGB。

**纹理刷新**（[src/device/vga.c:L59-L64](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/vga.c#L59-L64)）：

```c
static inline void update_screen() {
  SDL_UpdateTexture(texture, NULL, vmem, SCREEN_W * sizeof(uint32_t)); // 把 vmem 喂给纹理
  SDL_RenderClear(renderer);                                            // 清屏
  SDL_RenderCopy(renderer, texture, NULL, NULL);                        // 把纹理贴上
  SDL_RenderPresent(renderer);                                          // 提交显示
}
```

这就是 `vga_update_screen` 待实现时要调用的目标函数：它把 vmem 当前内容一次性推到窗口上。`SCREEN_W * sizeof(uint32_t)` 是纹理每行的字节数（pitch），与 vmem 布局一致。

**`device_update` 的 SDL 事件抽取**（[src/device/device.c:L46-L66](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c#L46-L66)）：

```c
#ifndef CONFIG_TARGET_AM
  SDL_Event event;
  while (SDL_PollEvent(&event)) {
    switch (event.type) {
      case SDL_QUIT:
        nemu_state.state = NEMU_QUIT;
        break;
#ifdef CONFIG_HAS_KEYBOARD
      case SDL_KEYDOWN:
      case SDL_KEYUP: {
        uint8_t k = event.key.keysym.scancode;
        bool is_keydown = (event.key.type == SDL_KEYDOWN);
        send_key(k, is_keydown);
        break;
      }
#endif
      default: break;
    }
  }
#endif
```

`SDL_PollEvent` 非阻塞地抽干事件队列：点关闭窗（`SDL_QUIT`）就让 NEMU 退出；按键事件（`SDL_KEYDOWN/UP`）交给 `send_key` 进键盘队列。AM 模式不接 SDL（`#ifndef CONFIG_TARGET_AM`），因为 AM 平台自己管输入。

**装配顺序**（[src/device/device.c:L76-L89](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c#L76-L89)）：`init_device` 先 `init_map`（分配 io_space 池），再依次 `init_serial/init_timer/init_vga/init_i8042/...`，最后 `init_alarm`。顺序由依赖决定：必须先有 io_space 池才能 `new_space`，必须先建好设备才能由 alarm 周期触发。

#### 4.5.4 代码实践

**实践目标**：把「客机寄存器 → 宿主 API」的翻译关系在脑中连成一张图。

**操作步骤（源码阅读型）**：

1. 列表对照四个设备，分别写出它们「产生宿主副作用」的代码位置：串口 `serial_putc`→`putc(stderr)`、RTC `get_time`→`clock_gettime`、键盘 `SDL_PollEvent`→`send_key`、VGA `update_screen`→`SDL_UpdateTexture/RenderPresent`。
2. 思考：为什么键盘和 VGA 用 SDL，而串口和 RTC 不用？（提示：前者需要图形窗口，后者只需文本流与系统时间。）
3. 找到 [src/device/filelist.mk:L28-L32](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/filelist.mk#L28-L32)，看 `LIBS += $(shell sdl2-config --libs)` 如何把 SDL 库链接进来，且仅 native 模式链接。

**需要观察的现象**：所有「能被真人感知」的外设行为，背后都对应一次宿主 API 调用。

**预期结果**：能画出「客机抽象寄存器 ↔ NEMU 设备代码 ↔ 宿主 API（SDL/stdio/time）」三层映射图。

#### 4.5.5 小练习与答案

**练习 1**：若把 `device_update` 里的节流判断去掉，让 `vga_update_screen` 每条客机指令都调用一次，会有什么后果？

**参考答案**：CPU 主循环每条指令都会调一次 `device_update`，若不节流，则每条指令都触发一次 `update_screen`，意味着每秒可能成千上万次 SDL 纹理更新与窗口提交，既严重拖慢模拟速度，又因刷新过于频繁而无意义（人眼只能感知约 60Hz）。节流到 60Hz 在「流畅」与「低开销」间取得平衡。

**练习 2**：AM 模式（`CONFIG_TARGET_AM`）下，VGA 的 `update_screen` 实现完全不同（见 [src/device/vga.c:L66-L71](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/vga.c#L66-L71)），为什么？

**参考答案**：AM 模式下 NEMU 自身是跑在另一个 native 平台（如 native 或另一个模拟器）上的程序，不能直接开 SDL 窗口（那会与宿主平台冲突）。于是它转而调用 AM 提供的抽象显示接口 `io_write(AM_GPU_FBDRAW, ...`，把画图工作委托给上层 AM——这正是「ISA/平台无关」抽象的体现。

---

## 5. 综合实践

本任务把 VGA 与串口两条路径串起来，作为本讲的收尾。

**任务**：

1. **实现 `vga_update_screen`**（[src/device/vga.c:L74-L77](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/vga.c#L74-L77)）：当 sync 寄存器（`vgactl_port_base[1]`）非零时，调用 `update_screen()` 刷新屏幕，然后把 sync 清零。注意 `update_screen()` 仅在 `CONFIG_VGA_SHOW_SCREEN` 下有定义，需用 `IFDEF` 保护，参考答案如下：

   ```c
   // 示例代码（非项目原有，需你填入 vga.c）
   void vga_update_screen() {
     if (vgactl_port_base[1] != 0) {
   #ifdef CONFIG_VGA_SHOW_SCREEN
       update_screen();
   #endif
       vgactl_port_base[1] = 0;
     }
   }
   ```

2. **验证串口路径**：在 menuconfig 里确认 `Device support → Enable serial` 与 `Enable VGA`（含 `Enable SDL SCREEN`）都打开，重新 `make` 编译。运行一个会向串口地址写一段文字（如 `Hello, NEMU!\n`）的客机程序，把 stderr 重定向观察：`./build/xxx-nemu -b ... 2>serial.log`，检查 `serial.log` 是否出现你写的文字。

3. **联动观察**（**待本地验证**）：再让同一个程序往 vmem 写一些像素、然后往 sync 寄存器写 1，观察 SDL 窗口是否在约 1/60 秒内显示出你写的像素图案、且 sync 被清零后不会反复刷新。

**验收标准**：

- 串口写的字符完整出现在 stderr。
- VGA 窗口能显示客机写入 vmem 的像素，且 sync 被正确清零（不会因 sync 恒为 1 而每帧重画同一幅静止画面——虽然结果一样，但清零是协议约定的「确认」）。
- 能用一句话说清「客机写串口 → stderr」「客机写 vmem+sync → SDL 窗口」两条链路各自的中间环节。

> 提示：本实践会修改 `vga.c`，属教学约定的 PA 实现内容；若你只想阅读不改源码，可仅完成第 2、3 步的源码追踪与设计说明，把 `vga_update_screen` 的实现写在讲义旁作为设计稿。

## 6. 本讲小结

- 四个外设都遵循 u6-l18 的同一套框架：`new_space` 切缓冲 → `add_mmio_map/add_pio_map` 注册 → `map_read/map_write` 搬数据并触发 `io_callback_t` 回调。
- **串口**最简：写 offset 0 的一个字节 → 回调 `serial_putc` → 宿主 stderr，只写不读。
- **RTC** 利用「读高半字才刷新」的回调时序，让两次 32 位读拼接出自洽的 64 位时间；同时通过 `add_alarm_handle(timer_intr)` 挂上时钟中断钩子（接 u6-l20）。
- **键盘 i8042** 是「生产—消费」解耦的样板：SDL 事件经 `keymap` 映射成 AM scancode 入环形队列，客机读数据口时回调出队，`KEYDOWN_MASK` 用最高位编码按下/抬起。
- **VGA** 把数据面（vmem，callback 为 NULL 的纯 MMIO）与控制面（vgactl：分辨率只读、sync 只写）分开，刷新由 `device_update` 节流到约 60Hz 后调 `vga_update_screen`（待实现）完成。
- **SDL** 是宿主呈现层：NEMU 把客机的抽象寄存器读写翻译成 SDL/stdio/time 等宿主 API，才让模拟的外设「看得见、摸得着」。

## 7. 下一步学习建议

本讲把「数据路径」讲完了——四个设备怎么读写寄存器。但还有两条线没收尾：

- **中断线**：`timer_intr → dev_raise_intr()` 目前是空 stub，键盘也可能需要中断。下一讲 **u6-l20（设备时钟与中断轮询）** 会讲 `device_update` 的节流原理、`alarm.c` 用 SIGVTALRM 模拟硬件时钟、以及 `dev_raise_intr / isa_query_intr` 的待实现职责，把设备与 CPU 中断机制连起来。
- **状态机线**：当设备能让 CPU「停下」响应中断时，就需要理解 NEMU 的执行状态机。学完 u6-l20 后可进入 **U7（中断、异常与系统模式）**，其中 u7-l21 会把本讲的 `isa_raise_intr/isa_query_intr` 与 `cpu_exec` 主循环接上。

建议接下来：先读 [src/device/alarm.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/alarm.c) 与 [src/device/intr.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/intr.c)，带着「`timer_intr` 那个钩子最终要触发什么」的问题进入 u6-l20。
