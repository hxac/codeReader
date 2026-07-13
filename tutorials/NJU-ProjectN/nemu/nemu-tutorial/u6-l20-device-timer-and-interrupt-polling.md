# 设备时钟与中断轮询

## 1. 本讲目标

本讲回答一个问题：**在 NEMU 里，时间从哪里来，设备的「周期性事件」（刷新屏幕、产生时钟中断）又是怎样被驱动的？**

真实硬件有一根独立运转的时钟，外设按自己的节拍工作，并在需要时向 CPU 拉起一根中断线。但 NEMU 是一个单线程的软件模拟器——它必须自己「假装」时间在流逝，并主动去问设备「你有事吗」。

学完本讲，你应该能够：

- 说清 `device_update` 为什么、以及如何把「每条指令都调用一次」节流到约 60 Hz；
- 说清 `alarm.c` 如何用 `SIGVTALRM` 信号模拟出硬件时钟中断，以及它与 `device_update` 用的是两个不同的「时钟」；
- 描绘出 `timer_intr → dev_raise_intr → isa_query_intr` 这条中断挂起链路，并指出当前哪些环节是留给你实现的 TODO；
- 解释为什么 `alarm.c` 在 AM 模式下会被编译排除（blacklist）。

本讲承接 u6-l19（典型外设实现）。上一讲我们看到了设备回调（如 RTC、串口），本讲往上走一层，看「谁在调用这些回调、谁在产生时钟中断」。

## 2. 前置知识

### 2.1 轮询 vs 中断

外设与 CPU 通信有两种典型方式：

- **轮询（polling）**：CPU 主动、反复地问设备「你有数据吗」「你有事吗」。优点是简单，缺点是 CPU 要不停去问。
- **中断（interrupt）**：设备有事时主动「拍一下」CPU，CPU 暂停当前工作去处理。优点是高效，缺点是需要一整套中断硬件与协议。

NEMU 两种都用：屏幕刷新和键盘事件用轮询（`device_update`），时钟中断用「信号模拟的硬件中断」（`SIGVTALRM`）。

### 2.2 信号（signal）

信号是 Unix/类 Unix 系统给进程的「软件中断」。一个进程可以注册一个处理函数，当特定信号到达时，操作系统会暂停该进程当前正在做的事，转去执行这个处理函数，返回后再继续原来被中断的地方——这套行为非常像硬件中断。

本讲用到的两个关键点：

- `SIGVTALRM`：一种「虚拟时间到点」信号。
- `setitimer(ITIMER_VIRTUAL, ...)`：设置一个按「进程实际消耗的 CPU 时间」递减的定时器，到期就发 `SIGVTALRM`。

### 2.3 两种时间源

请特别注意，本讲有两个看起来都「每秒 60 次」的机制，但它们走的是不同的时钟：

| 机制 | 时钟源 | 含义 |
|------|--------|------|
| `device_update` 节流 | `get_time()` → `CLOCK_MONOTONIC_COARSE` | **墙钟时间（wall-clock）**：真实流逝的时间 |
| `init_alarm` 定时器 | `setitimer(ITIMER_VIRTUAL)` → `SIGVTALRM` | **虚拟 CPU 时间**：只有 NEMU 进程真正在跑 CPU 时才计时 |

这个区别是本讲最容易踩坑的地方，4.1 和 4.2 会分别讲透。

### 2.4 前序术语回顾

- `nemu_state.state` 的五种取值（`NEMU_RUNNING/STOP/END/ABORT/QUIT`）见 u3-l9；本讲只用到 `NEMU_RUNNING`（CPU 正在跑）这一种。
- `IFDEF(macro, ...)` / `IFNDEF(macro, ...)`：条件编译宏，「宏已定义则保留代码」/「宏未定义则保留代码」，见 u8-l26。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/device/device.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c) | `device_update`（节流轮询 + VGA 刷新 + SDL 事件抽取）、`init_device`（设备装配入口） |
| [src/device/alarm.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/alarm.c) | `init_alarm` 注册 `SIGVTALRM`、`add_alarm_handle` 注册回调、`alarm_sig_handler` 信号处理函数 |
| [include/device/alarm.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/device/alarm.h) | `TIMER_HZ`、`alarm_handler_t` 类型与 `add_alarm_handle` 声明 |
| [src/device/timer.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/timer.c) | RTC 设备回调、`timer_intr`（把时钟信号转成中断挂起）、`init_timer` 注册 alarm 回调 |
| [src/device/intr.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/intr.c) | `dev_raise_intr`——目前是空函数，留给你实现 |
| [src/isa/riscv32/system/intr.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/system/intr.c) | `isa_raise_intr`（TODO）、`isa_query_intr`（目前恒返回 `INTR_EMPTY`） |
| [include/isa.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h) | `isa_raise_intr` / `isa_query_intr` / `INTR_EMPTY` 的接口声明 |
| [src/cpu/cpu-exec.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c) | `execute` 主循环，每条指令后调用 `device_update()` |
| [src/device/filelist.mk](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/filelist.mk) | AM 模式下把 `alarm.c` 加入黑名单 |
| [src/utils/timer.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/timer.c) | `get_time()` 实现（墙钟时间） |

## 4. 核心概念与源码讲解

本讲按「中断挂起链路」自上而下拆成四个最小模块：

1. **device_update 节流轮询**：谁在周期性刷新屏幕、抽键盘事件，频率如何被压到 60 Hz；
2. **alarm 信号机制**：`SIGVTALRM` 如何模拟硬件时钟中断、回调如何注册；
3. **timer_intr → dev_raise_intr**：时钟信号如何变成「中断挂起标志」；
4. **isa_query_intr**：CPU 侧如何查询并响应这个挂起的中断。

### 4.1 device_update 节流轮询与 SDL 事件抽取

#### 4.1.1 概念说明

回忆 u3-l9：`execute()` 主循环每执行完一条客机指令，就会调用一次 `device_update()`：

```c
// src/cpu/cpu-exec.c
static void execute(uint64_t n) {
  Decode s;
  for (;n > 0; n --) {
    exec_once(&s, cpu.pc);
    g_nr_guest_inst ++;
    trace_and_difftest(&s, cpu.pc);
    if (nemu_state.state != NEMU_RUNNING) break;
    IFDEF(CONFIG_DEVICE, device_update());   // 每条指令都调用
  }
}
```

[src/cpu/cpu-exec.c:L74-L83](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L74-L83) 中第 81 行 `device_update()` 是每条指令的固定插入点。

一个程序一秒钟可能执行上千万条指令，如果每条指令都真的去刷新屏幕、轮询 SDL 事件，模拟器会被这些「与指令执行无关」的工作彻底拖垮。所以 `device_update` 的第一要务是**节流（throttle）**：绝大多数调用直接 `return`，只有「差不多到了 1/60 秒」才真正干活。这正是「轮询」二字的核心——频繁地询问，但只在合适时机才动手。

#### 4.1.2 核心流程

`device_update` 的工作分两段：

```
device_update():
  now = get_time()                  # 读墙钟时间（微秒）
  if now - last < 1000000 / TIMER_HZ:   # 不到一个节拍（约 16666 us）
      return                        # 直接返回，啥也不干
  last = now                        # 记下本次「真干活」的时刻

  # —— 以下每秒只跑约 60 次 ——
  vga_update_screen()               # 刷新 VGA 画面（若开启 VGA）
  while SDL_PollEvent(&event):      # 抽干 SDL 事件队列
      SDL_QUIT      -> nemu_state.state = NEMU_QUIT
      SDL_KEYDOWN/UP -> send_key(scancode, is_keydown)
```

节拍周期是 `1000000 / TIMER_HZ` 微秒。`TIMER_HZ` 定义在 [include/device/alarm.h:L19](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/device/alarm.h#L19) 为 `60`，故：

\[
T_{\text{节拍}} = \frac{1{,}000{,}000}{60} \approx 16666\ \text{us} \approx 16.6\ \text{ms}
\]

即屏幕与事件轮询被限制在约 60 Hz。这与人眼舒适刷新率（≥ 24 Hz、通常 60 Hz）和键盘中断响应延迟匹配。

#### 4.1.3 源码精读

`device_update` 全貌：

```c
void device_update() {
  static uint64_t last = 0;                       // 静态：跨调用保持
  uint64_t now = get_time();
  if (now - last < 1000000 / TIMER_HZ) {          // 节流闸门
    return;
  }
  last = now;

  IFDEF(CONFIG_HAS_VGA, vga_update_screen());     // 刷新屏幕

#ifndef CONFIG_TARGET_AM                          // AM 模式下无 SDL
  SDL_Event event;
  while (SDL_PollEvent(&event)) {                 // 抽干事件队列
    switch (event.type) {
      case SDL_QUIT: nemu_state.state = NEMU_QUIT; break;
#ifdef CONFIG_HAS_KEYBOARD
      case SDL_KEYDOWN:
      case SDL_KEYUP: {
        uint8_t k = event.key.keysym.scancode;
        bool is_keydown = (event.key.type == SDL_KEYDOWN);
        send_key(k, is_keydown);                  // 见 u6-l19 键盘设备
        break;
      }
#endif
      default: break;
    }
  }
#endif
}
```

[src/device/device.c:L36-L67](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c#L36-L67) 即上述函数，重点解读三处：

1. **`static uint64_t last = 0;`**（[L37](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c#L37)）：`static` 局部变量只在首次调用初始化为 0，之后在调用间保持值，用来记住「上次真干活」的时刻。
2. **节流闸门 `now - last < 1000000 / TIMER_HZ`**（[L39-L41](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c#L39-L41)）：这是函数的灵魂。`get_time()` 返回的是从 NEMU 启动至今的**墙钟微秒数**。两次「真干活」之间至少间隔约 16666 us。
3. **`while (SDL_PollEvent(&event))`**（[L48](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c#L48)）：`SDL_PollEvent` 是**非阻塞**的——有事件就取出并返回 1，没有就返回 0。`while` 用来「抽干」积压的事件队列。窗口关闭（`SDL_QUIT`）会把 `nemu_state.state` 置为 `NEMU_QUIT`，下一轮 `execute` 循环检测到状态非 `NEMU_RUNNING` 就会 `break` 退出（见 u3-l9）。

`get_time()` 用的是墙钟。看一眼它的实现，理解 4.2 的对照：

```c
static uint64_t boot_time = 0;

static uint64_t get_time_internal() {
  ...
  struct timespec now;
  clock_gettime(CLOCK_MONOTONIC_COARSE, &now);          // 墙钟、单调时钟
  uint64_t us = now.tv_sec * 1000000 + now.tv_nsec / 1000;
  return us;
}

uint64_t get_time() {
  if (boot_time == 0) boot_time = get_time_internal();  // 首次记录启动时刻
  uint64_t now = get_time_internal();
  return now - boot_time;                               // 返回「启动至今」的微秒
}
```

[src/utils/timer.c:L24-L45](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/timer.c#L24-L45)：`get_time` 基于 `CLOCK_MONOTONIC_COARSE`，返回相对启动时刻的墙钟微秒。**注意它衡量的是真实流逝时间，与 NEMU 是否在模拟、模拟多快都无关。**

#### 4.1.4 代码实践

**实践目标**：直观感受节流的存在。

**操作步骤**（源码阅读型实践，无需运行）：

1. 阅读 [src/device/device.c:L37-L42](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c#L37-L42)，确认 `last` 是 `static`。
2. 想象一个每秒执行 1000 万条指令的程序：`device_update` 一秒被调用 1000 万次，但 `vga_update_screen()` 一秒只被调用约 60 次。
3. （可选修改型，待本地验证）把 `TIMER_HZ` 临时改大（如 600）重新编译运行带 VGA 的程序，观察屏幕刷新是否明显变快、CPU 占用是否上升；改回 60。

**需要观察的现象**：修改 `TIMER_HZ` 后，刷新频率随之变化，验证「闸门周期 = 1000000 / TIMER_HZ」。

**预期结果**：`TIMER_HZ` 翻倍 → 每秒刷新次数大约翻倍。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `last` 必须是 `static`？如果是普通局部变量会怎样？

**参考答案**：普通局部变量每次调用都重新初始化为 0，`now - last` 永远等于 `now`，闸门形同虚设，`vga_update_screen` 会被每条指令调用一次，模拟器被拖垮。`static` 让 `last` 在调用间保持，才能记住「上次干活时刻」。

**练习 2**：节流闸门用的是 `get_time()`（墙钟），而不是「指令计数」。这样的好处是什么？

**参考答案**：用墙钟能保证屏幕刷新、事件响应与真实时间同步——无论客机程序跑得快还是慢（被 SDB 单步暂停时甚至完全不动），屏幕都按真实约 60 Hz 刷新，键盘事件也能及时被抽走。若改成按指令计数，则模拟越快刷新越频繁、暂停时画面冻结，体验不可控。

---

### 4.2 alarm 信号机制——用 SIGVTALRM 模拟时钟中断

#### 4.2.1 概念说明

`device_update` 解决了「屏幕/键盘的轮询」，但还差一样东西：**时钟中断**。

真实机器里有一个独立运转的定时器芯片（如 PC 上的 8253/HPET），它按固定频率向 CPU 发中断，操作系统靠这个「心跳」做时间片调度、维护系统时间。NEMU 是软件，没有这根硬件心跳，于是它用宿主机的 **`SIGVTALRM` 信号**来扮演这个角色：让操作系统在固定间隔给 NEMU 进程发一个软件中断，NEMU 在信号处理函数里「拉起中断线」。

这套机制全部封装在 `alarm.c`。它的核心是一个「观察者模式」：允许别的模块（如 timer）把自己的处理函数注册进来，等信号一到就一并调用。

#### 4.2.2 核心流程

```
启动期：
  init_device() -> init_alarm()
  init_timer()  -> add_alarm_handle(timer_intr)   # timer 把自己挂上去

init_alarm():
  注册 SIGVTALRM 的处理函数 = alarm_sig_handler
  设置 ITIMER_VIRTUAL 定时器，周期 = 1000000 / TIMER_HZ us

运行期（由操作系统异步触发）：
  每过「NEMU 消耗 16666 us CPU 时间」
    -> 操作系统发 SIGVTALRM
    -> alarm_sig_handler() 遍历 handler[]，依次调用
        -> timer_intr()（见 4.3）
```

注意两个层次：

- **谁注册信号 + 定时器**：`init_alarm`。
- **谁被信号调用**：`alarm_sig_handler`，它再分发到所有已注册的 `handler`。

#### 4.2.3 源码精读

先看公开接口与常量：

```c
#define TIMER_HZ 60

typedef void (*alarm_handler_t) ();            // 回调类型：无参无返回
void add_alarm_handle(alarm_handler_t h);
```

[include/device/alarm.h:L19-L22](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/device/alarm.h#L19-L22)：`alarm_handler_t` 是一个「无参、返回 void」的函数指针类型；`TIMER_HZ` 既是 device_update 的节流频率，也是这里的定时器频率——同一个常量驱动两个机制。

`alarm.c` 全文很短，逐段看：

```c
#define MAX_HANDLER 8

static alarm_handler_t handler[MAX_HANDLER] = {};   // 回调表，最多 8 个
static int idx = 0;                                  // 当前已注册个数

void add_alarm_handle(alarm_handler_t h) {
  assert(idx < MAX_HANDLER);
  handler[idx ++] = h;                               # 追加到表尾
}

static void alarm_sig_handler(int signum) {          # 信号处理函数
  int i;
  for (i = 0; i < idx; i ++) {
    handler[i]();                                    # 依次调用所有回调
  }
}
```

[src/device/alarm.c:L21-L36](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/alarm.c#L21-L36)：这是一个最朴素的观察者模式——`handler[]` 是回调表，`add_alarm_handle` 注册（往尾部追加），`alarm_sig_handler` 通知（遍历调用）。信号到达时，已注册的所有回调（目前只有 `timer_intr`）都会被调用。

再看注册信号与定时器的 `init_alarm`：

```c
void init_alarm() {
  struct sigaction s;
  memset(&s, 0, sizeof(s));
  s.sa_handler = alarm_sig_handler;                  # 信号到达时调它
  int ret = sigaction(SIGVTALRM, &s, NULL);          # 关联到 SIGVTALRM
  Assert(ret == 0, "Can not set signal handler");

  struct itimerval it = {};
  it.it_value.tv_sec = 0;
  it.it_value.tv_usec = 1000000 / TIMER_HZ;          # 首次到期：约 16666 us
  it.it_interval = it.it_value;                       # 之后每次间隔相同
  ret = setitimer(ITIMER_VIRTUAL, &it, NULL);         # 用「虚拟 CPU 时间」
  Assert(ret == 0, "Can not set timer");
}
```

[src/device/alarm.c:L38-L51](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/alarm.c#L38-L51)。两步：

1. **`sigaction(SIGVTALRM, ...)`**（[L42](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/alarm.c#L42)）：把 `SIGVTALRM` 信号与 `alarm_sig_handler` 绑定。`sigaction` 是比老的 `signal()` 更可控的注册接口。
2. **`setitimer(ITIMER_VIRTUAL, ...)`**（[L49](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/alarm.c#L49)）：设置一个周期性定时器。`it_value` 是首次到点时间，`it_interval` 是之后每次的间隔，二者都设为约 16666 us。到期时操作系统会发 `SIGVTALRM`。

**关键点：`ITIMER_VIRTUAL` 与 4.1 的墙钟完全不同。** `ITIMER_VIRTUAL` 只在进程**真正占用 CPU 运行**时才递减；进程被挂起（比如停在 SDB 提示符等输入）、或宿主机忙别的事时，它不计时。也就是说：

- 当 NEMU 全速模拟时，约每 16.6 ms 的「CPU 时间」发一次 `SIGVTALRM`；
- 当你停在 SDB 单步调试时，进程不消耗 CPU，定时器不走，不会积压一堆中断。

这是一个精心选择的近似——它让时钟中断的频率大致跟宿主能提供的算力挂钩，而不是跟墙钟挂钩。否则，单步调试 1 秒墙钟就会积压 60 个待处理中断，一旦恢复运行会「burst」式地一次性投递，行为反直觉。

> 对照：4.1 的 `device_update` 用墙钟（`get_time`），4.2 的 `init_alarm` 用虚拟 CPU 时间（`ITIMER_VIRTUAL`）。两者名义上都是 60 Hz，但「60 Hz 的什么」不同。墙钟保证人能感知的屏幕/键盘实时性；虚拟时间保证中断与实际模拟量挂钩。

#### 4.2.4 代码实践

**实践目标**：看清「信号 → 回调」的分发结构。

**操作步骤**（源码阅读型）：

1. 阅读 [src/device/alarm.c:L26-L36](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/alarm.c#L26-L36)，确认 `add_alarm_handle` 与 `alarm_sig_handler` 通过共享 `handler[]` 与 `idx` 协作。
2. 在仓库里搜索 `add_alarm_handle` 的调用点，确认目前只有 `init_timer` 注册了 `timer_intr`（见 4.3）。
3. 思考：如果再有一个设备也需要「每 1/60 秒被叫醒」，该怎么做？

**预期结果**：调用 `add_alarm_handle(你的函数)` 即可，无需改 `alarm.c`——这正是观察者模式的扩展性。

#### 4.2.5 小练习与答案

**练习 1**：把 `ITIMER_VIRTUAL` 换成 `ITIMER_REAL`（对应 `SIGALRM`，按墙钟计时），会发生什么不希望的现象？

**参考答案**：`ITIMER_REAL` 按真实墙钟递减。当你在 SDB 提示符前停顿、或客机程序运行很慢时，定时器仍按墙钟不停到期、积压信号；一旦 NEMU 恢复接收信号，积压的中断会一次性涌进来，导致「停了一会儿再跑」时出现大量突发时钟中断。`ITIMER_VIRTUAL` 只在 NEMU 占用 CPU 时计时，避免了这种积压。

**练习 2**：`MAX_HANDLER` 当前为 8，若注册第 9 个回调会怎样？

**参考答案**：`add_alarm_handle` 里的 `assert(idx < MAX_HANDLER)` 会触发断言失败，程序终止。这是用断言保护的「设计上限」，提示调用者当前容量。

---

### 4.3 从 timer_intr 到 dev_raise_intr——中断挂起链路

#### 4.3.1 概念说明

4.2 解决了「信号到达时调用谁」，被调用的就是本节的主角 `timer_intr`。它的职责很单一：**把「时钟信号」翻译成「向 CPU 请求一个中断」**。

但这里有一个重要的设计：硬件中断不是「想发就立刻被 CPU 处理」的。CPU 一条指令没执行完，不会去响应中断；而且通常要等当前指令结束、检查到中断请求后才响应。所以真实硬件里有一个「中断挂起（pending）位」——设备拉高它表示「我有中断」，CPU 在指令边界上去看这个位。

NEMU 复刻这个模型：

- `timer_intr` 调 `dev_raise_intr`，后者应当**设置一个挂起标志**（表示「时钟中断待处理」）；
- CPU 在每条指令结束后，通过 `isa_query_intr` 去**查询**这个标志，若有则真正进入中断处理。

当前 `dev_raise_intr` 是个**空函数**——这正是留给你实现的部分。

#### 4.3.2 核心流程

```
SIGVTALRM 到达
  -> alarm_sig_handler()
      -> timer_intr()
          if (nemu_state.state == NEMU_RUNNING):
              dev_raise_intr()        # TODO：应当设置「时钟中断挂起」标志

（后续由 CPU 侧查询，见 4.4）
```

注意 `timer_intr` 有一个**状态守卫**：只有当 CPU 正在运行（`NEMU_RUNNING`）时才请求中断。停在 SDB 时虽然进程仍在消耗 CPU（所以信号会来），但不该向一个没在跑的 CPU 注入中断。

#### 4.3.3 源码精读

`timer_intr` 定义在 timer.c 里，并被 timer.c 自己注册到 alarm：

```c
#ifndef CONFIG_TARGET_AM                        # AM 模式下不需要这套
static void timer_intr() {
  if (nemu_state.state == NEMU_RUNNING) {
    extern void dev_raise_intr();              # 声明在 intr.c
    dev_raise_intr();                          # 请求时钟中断（待实现）
  }
}
#endif

void init_timer() {
  rtc_port_base = (uint32_t *)new_space(8);
  ...                                          # 注册 RTC 设备（见 u6-l19）
  IFNDEF(CONFIG_TARGET_AM, add_alarm_handle(timer_intr));   # 把 timer_intr 挂到 alarm
}
```

[src/device/timer.c:L31-L48](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/timer.c#L31-L48)。两点：

1. **`if (nemu_state.state == NEMU_RUNNING)`**（[L33](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/timer.c#L33)）：状态守卫。`nemu_state.state` 在 `cpu_exec` 进入时被置为 `NEMU_RUNNING`（见 [src/cpu/cpu-exec.c:L106](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L106)），退出时回到 `NEMU_STOP/END/...`。停在 SDB 时不是 `NEMU_RUNNING`，不会注入中断。
2. **`add_alarm_handle(timer_intr)`**（[L47](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/timer.c#L47)）：timer 把自己的 `timer_intr` 注册进 4.2 的回调表，于是每次 `SIGVTALRM` 都会调到它。这一行把「设备模块（timer）」与「时钟源（alarm）」解耦——timer 不需要知道信号机制，alarm 也不需要知道有谁需要时钟。

再看被调用的 `dev_raise_intr`，目前是空的：

```c
#include <isa.h>

void dev_raise_intr() {
}
```

[src/device/intr.c:L16-L19](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/intr.c#L16-L19)：函数体什么都没做。这意味着：**当前即使时钟信号不断到达，也不会有任何中断被挂起**——因为挂起动作还没实现。这条链路目前是「断」的，等你接上。

> 为什么把 `dev_raise_intr` 放在独立的 `intr.c`，而不是写在 timer.c 里？因为中断挂起位通常属于「CPU/ISA 侧」的状态（不同 ISA 的中断控制器不同），而 `timer.c` 是「设备侧」。用独立文件 + 空实现，让设备侧只需调用 `dev_raise_intr()`，而不必关心具体 ISA 如何记录挂起——这是一个跨层接缝。

#### 4.3.4 代码实践

**实践目标**：理解挂起链路当前在何处「断开」。

**操作步骤**（源码阅读型）：

1. 从 [src/device/alarm.c:L31-L36](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/alarm.c#L31-L36) 的 `alarm_sig_handler` 出发，顺着调用链画出来：`alarm_sig_handler → handler[i]() → timer_intr → dev_raise_intr`。
2. 到 [src/device/intr.c:L18-L19](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/intr.c#L18-L19) 看到空函数体，标记这里就是「断点」。
3. 思考：一个合理的 `dev_raise_intr` 应该改什么变量？（提示：一个表示「时钟中断待处理」的布尔/标志，通常放在 CPU 侧全局或 `isa` 相关结构里。）

**预期结果**：能画出完整的「信号→挂起」调用链，并指出 `dev_raise_intr` 是唯一未实现环节。

#### 4.3.5 小练习与答案

**练习 1**：`timer_intr` 为什么要判 `nemu_state.state == NEMU_RUNNING`？删掉这个判断会怎样？

**参考答案**：信号 `SIGVTALRM` 在 SDB 交互（等待输入时其实不会，因为进程不占 CPU；但单步间也可能触发）期间也会到达。如果不判状态，就会在 CPU 没在跑时也设置挂起标志，可能导致恢复运行后立刻被一个不该有的中断打断，或与单步逻辑冲突。判状态保证「只有真正在跑才请求中断」。

**练习 2**：`dev_raise_intr` 被 `extern` 声明在 timer.c 内部、定义在 intr.c。为什么不放到一个正式的头文件里？

**参考答案**：这是一个临时的、跨「设备↔ISA」边界的接缝，目前只有 timer 一个调用者，用 `extern` 局部声明足够，避免污染公共头文件。等接口稳定（例如 PA 后续阶段）再整理进头文件更合适。

---

### 4.4 isa_query_intr——CPU 侧的中断查询与响应

#### 4.4.1 概念说明

到这里，「时钟信号 → 中断挂起」的前半段讲完了。后半段是 CPU 侧：**每条指令执行完，CPU 主动问一句「现在有待处理的中断吗？」**，如果有，就跳到中断处理入口。

这就是 `isa_query_intr` 的职责：查询当前是否有挂起的中断，若有则返回中断号，否则返回一个特殊值 `INTR_EMPTY` 表示「没有」。

这里有一个 NEMU 初学者容易忽略的事实：**当前仓库里 `isa_query_intr` 从来没有被调用过。** 它和 `isa_raise_intr` 都被声明、定义了，但没有任何执行路径会去调用 `isa_query_intr`。这是因为「在每条指令后查询中断并响应」这一步也是留给你实现的 TODO（通常在 `isa_exec_once` 末尾，执行完指令后查询；若有中断则调 `isa_raise_intr`）。所以整条时钟中断链路的「最后一公里」也需要你来接通。

#### 4.4.2 核心流程

完整的中断响应闭环（虚线 = 留给你实现）：

```
SIGVTALRM
  -> alarm_sig_handler -> timer_intr
      -> dev_raise_intr()            # TODO：置「时钟中断挂起」标志

execute 主循环（每条指令后）:
  exec_once(...)
  device_update()                     # 已有：屏幕/键盘轮询
  --- 以下为待接入的中断查询点 ---
  # NO = isa_query_intr()              # TODO：查询挂起中断
  # if (NO != INTR_EMPTY):
  #     cpu.pc = isa_raise_intr(NO, cpu.pc)   # TODO：进入中断处理
```

`isa_query_intr` 与 `isa_raise_intr` 的分工：

- `isa_query_intr()`：**有没有**中断？返回中断号或 `INTR_EMPTY`。
- `isa_raise_intr(NO, epc)`：**响应**中断号 `NO`——保存返回地址 `epc`、跳转到中断向量，并返回新的 `pc`。

#### 4.4.3 源码精读

接口声明在 isa.h：

```c
// interrupt/exception
vaddr_t isa_raise_intr(word_t NO, vaddr_t epc);
#define INTR_EMPTY ((word_t)-1)
word_t isa_query_intr();
```

[include/isa.h:L49-L52](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L49-L52)。`INTR_EMPTY` 定义为 `(word_t)-1`，即全 1（如 riscv32 下是 `0xFFFFFFFF`）。之所以选全 1，是因为合法的中断号都是较小的非负整数，全 1 不会与任何真实中断号冲突，是个安全的「空」哨兵。

riscv32 的实现（全部是 TODO/桩）：

```c
word_t isa_raise_intr(word_t NO, vaddr_t epc) {
  /* TODO: Trigger an interrupt/exception with ``NO''.
   * Then return the address of the interrupt/exception vector.
   */
  return 0;
}

word_t isa_query_intr() {
  return INTR_EMPTY;
}
```

[src/isa/riscv32/system/intr.c:L18-L28](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/system/intr.c#L18-L28)。当前 `isa_query_intr` 恒返回 `INTR_EMPTY`，意味着「永远没有中断」——配合 4.3 的空 `dev_raise_intr`，整条时钟中断链路当前完全不工作，这正是本讲综合实践要接通的部分。

**关于 `isa_query_intr` 的接入点**：标准做法是在 [src/isa/riscv32/inst.c:L75-L78](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L75-L78) 的 `isa_exec_once` 末尾、`decode_exec` 之后插入查询逻辑。理由是：中断必须在「一条指令执行完毕」的边界上被响应，而 `isa_exec_once` 正是「执行一条 ISA 指令」的统一入口。大致形如（**示例代码**，非项目原有）：

```c
// 示例代码：isa_exec_once 末尾的中断查询（待你在 PA 中实现）
int isa_exec_once(Decode *s) {
  s->isa.inst = inst_fetch(&s->snpc, 4);
  decode_exec(s);

  word_t intr_no = isa_query_intr();          // 有挂起中断吗？
  if (intr_no != INTR_EMPTY) {
    s->dnpc = isa_raise_intr(intr_no, s->snpc); // 响应：跳到中断向量
  }
  return 0;
}
```

注意把返回地址设成 `s->snpc`（顺序下一条指令地址），因为中断返回后应执行被打断指令的下一条；把跳转目标写进 `s->dnpc`，这样 `exec_once` 末尾 `cpu.pc = s->dnpc` 会自然提交（PC 流转见 u3-l10）。完整的 `isa_raise_intr` 实现（保存 epc、关中断、跳向量）留待 u7-l21 详讲。

#### 4.4.4 代码实践

**实践目标**：把 `isa_query_intr` 与「挂起标志」接通（本节只做查询返回，完整响应见综合实践）。

**操作步骤**：

1. 在某处（建议 `src/isa/riscv32/system/intr.c` 顶部）加一个全局标志，**示例代码**：
   ```c
   #include <isa.h>
   static bool timer_intr_pending = false;

   void dev_raise_intr() {                       // 在 intr.c 里实现，覆盖原空函数
     timer_intr_pending = true;
   }

   word_t isa_query_intr() {
     if (timer_intr_pending) {
       timer_intr_pending = false;               // 查询即「取走」
       return IRQ_TIMER;                         // 你的时钟中断号，如 RISC-V 的机器时钟中断
     }
     return INTR_EMPTY;
   }
   ```
   注意：`dev_raise_intr` 当前定义在 `src/device/intr.c`。若你把它的实现挪到 ISA 侧的 `src/isa/riscv32/system/intr.c`，需要把 `src/device/intr.c` 里那份删掉或改成空，避免链接时「重复定义」。或者让设备侧的 `dev_raise_intr` 调一个 ISA 侧函数。具体组织方式请按你的 PA 要求来。
2. 此时 `isa_query_intr` 已能返回中断号，但还需在 `isa_exec_once` 末尾调用它、并在 `isa_raise_intr` 里真正处理（见综合实践与本讲后续 u7-l21）。

**需要观察的现象**：开启 ITRACE（见 u8-l25）运行一个会长时间运行的程序，在中断被响应后应能看到 PC 跳到了中断向量地址。

**预期结果**：每约 16.6 ms 的 CPU 时间产生一次时钟中断查询命中。（完整可运行需要 u7-l21 的 `isa_raise_intr`。）

#### 4.4.5 小练习与答案

**练习 1**：`INTR_EMPTY` 为什么定义成 `(word_t)-1` 而不是 `0`？

**参考答案**：因为 `0` 在某些 ISA 里是合法的中断号（如 x86 的除零异常、RISC-V 的某些异常码），用它当「空」会和真实中断号冲突。`(word_t)-1` 是全 1（如 32 位下 `0xFFFFFFFF`），通常不会是任何合法中断号，是安全的哨兵值。

**练习 2**：为什么「查询中断」放在每条指令结束后，而不是放在 `exec_once` 开头、或 `cpu_exec` 进入时只查一次？

**参考答案**：硬件响应中断的时机就是「当前指令执行完、下一条还没取」的边界。放在每条指令结束后最贴合这个语义，保证任何指令完成后都能被及时打断。若只在 `cpu_exec` 进入时查一次，则一次 `c` 命令跑百万条指令期间都不会响应中断；若放在 `exec_once` 开头，则可能与「先完成当前指令」的语义冲突。

---

## 5. 综合实践

**任务**：接通完整的时钟中断链路 `timer_intr → dev_raise_intr → isa_query_intr → isa_raise_intr`，并解释 `alarm.c` 为何在 AM 模式下被 blacklist。

### 第一部分：实现中断挂起与查询

1. **实现 `dev_raise_intr`**（当前在 [src/device/intr.c:L18-L19](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/intr.c#L18-L19) 是空的）：让它设置一个「时钟中断挂起」标志。建议把标志和查询逻辑都放到 ISA 侧的 `src/isa/riscv32/system/intr.c`（因为中断号、挂起语义与 ISA 强相关），并处理好与设备侧 `src/device/intr.c` 的重复定义问题。
2. **实现 `isa_query_intr`**（[src/isa/riscv32/system/intr.c:L26-L28](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/system/intr.c#L26-L28)）：有挂起标志则返回时钟中断号并清标志，否则返回 `INTR_EMPTY`。
3. **实现 `isa_raise_intr`**（[src/isa/riscv32/system/intr.c:L18-L24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/system/intr.c#L18-L24)）：保存返回地址（RISC-V 的 `mepc`）、记录中断号（`mcause`）、跳到中断向量（`mtvec`），返回新 `pc`。本步细节见 u7-l21。
4. **接入查询点**：在 [src/isa/riscv32/inst.c:L75-L78](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L75-L78) 的 `isa_exec_once` 末尾，`decode_exec(s)` 之后查询 `isa_query_intr()`，非空则 `s->dnpc = isa_raise_intr(NO, s->snpc)`。
5. **验证**：写一个开中断、使能时钟中断、在中断处理里打印或自增的小程序（或在 AM 上跑需要时钟的程序），观察是否每约 1/60 秒（按 CPU 时间计）进入一次中断处理。若无法运行完整程序，至少用「添加日志」验证 `dev_raise_intr` 被调用、`isa_query_intr` 曾返回非 `INTR_EMPTY`。

### 第二部分：分析 alarm 在 AM 模式下的 blacklist

阅读 [src/device/filelist.mk:L26](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/filelist.mk#L26)：

```makefile
SRCS-BLACKLIST-$(CONFIG_TARGET_AM) += src/device/alarm.c
```

以及 device.c 里同样用 `#ifndef CONFIG_TARGET_AM` 包裹的 SDL 代码（[L46-L66](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c#L46-L66)）、timer.c 里 `#ifndef CONFIG_TARGET_AM` 包裹的 `timer_intr`（[L31-L38](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/timer.c#L31-L38)）和 `init_device` 里 `IFNDEF(CONFIG_TARGET_AM, init_alarm())`（[L88](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c#L88) → 实际为 [src/device/device.c:L88](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c#L88)）。

请回答：为什么 AM 模式下要把 `alarm.c` 编译排除？

**参考分析**：在 Native 模式下，NEMU 是一个独立进程，需要自己用 `SIGVTALRM` 信号「凭空」模拟出硬件时钟中断、并亲自用 SDL 呈现外设。而在 AM 模式（`CONFIG_TARGET_AM`）下，NEMU 被编译成一个库，链接进一个跑在「真实主机或另一层模拟器」之上的 AM 程序；此时「时间」和「设备」由 AM 抽象层本身提供——`get_time_internal` 在 AM 下走 `io_read(AM_TIMER_UPTIME)`（见 [src/utils/timer.c:L27-L28](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/timer.c#L27-L28)），`init_device` 在 AM 下走 `ioe_init()`（[src/device/device.c:L77](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c#L77)）。此时再注册一个 `SIGVTALRM` 信号处理器既无必要（AM 自己管设备/时钟）又有风险（在一个被嵌入到别的程序里的库里装信号处理器，会干扰宿主程序自身的信号处理）。所以 `alarm.c` 整体被 blacklist 排除，`device_update` 的 SDL 分支、`timer_intr`、`init_alarm()` 调用也都用 `CONFIG_TARGET_AM` 条件编译关掉。一句话：**Native 模式自己造时钟与外设，AM 模式把这些交给 AM 抽象层，所以信号模拟的那套只属于 Native。**

## 6. 本讲小结

- NEMU 用**两种独立的「时钟」**驱动设备：`device_update` 用墙钟（`get_time` / `CLOCK_MONOTONIC_COARSE`）把屏幕刷新与 SDL 事件抽取节流到约 60 Hz；`init_alarm` 用虚拟 CPU 时间（`ITIMER_VIRTUAL` / `SIGVTALRM`）周期性触发时钟中断。两者名义上都是 `TIMER_HZ=60`，但计的是不同的时间。
- `device_update` 是「每条指令调用一次、但用 `now - last` 闸门节流」的轮询：绝大多数调用直接返回，只有到节拍才真正刷新 VGA、抽 SDL 事件。
- `alarm.c` 是观察者模式：`add_alarm_handle` 往 `handler[]` 表注册回调，`SIGVTALRM` 一到就由 `alarm_sig_handler` 遍历调用。`timer` 通过 `add_alarm_handle(timer_intr)` 把自己挂上去，与信号机制解耦。
- 时钟中断走 `timer_intr → dev_raise_intr` 设置挂起，CPU 侧在每条指令后用 `isa_query_intr` 查询、`isa_raise_intr` 响应。当前 `dev_raise_intr` 与 `isa_query_intr`（及调用它的人）都是留给你实现的 TODO，整条链路目前是「断」的。
- `timer_intr` 有 `nemu_state.state == NEMU_RUNNING` 守卫，保证只在 CPU 真正运行时才注入中断；`INTR_EMPTY = (word_t)-1` 是安全的「无中断」哨兵。
- AM 模式下 `alarm.c` 被 `filelist.mk` blacklist 排除，SDL/`timer_intr`/`init_alarm` 也被 `CONFIG_TARGET_AM` 条件编译关闭——因为 AM 自己通过 `ioe_init` / `io_read` 提供设备与时钟，Native 的信号模拟那一套不适用。

## 7. 下一步学习建议

- **u7-l21 中断与异常机制**：本讲只讲到「挂起 + 查询」，`isa_raise_intr` 的完整实现（保存 `mepc`、写 `mcause`、跳 `mtvec`、关中断）在下一单元详讲，是本讲综合实践的直接后续。
- **u7-l22 分页与 MMU 地址翻译**：同样是 `isa.h` 抽象接口的「实现后补」风格，可与本讲的 `isa_query_intr` 对照阅读，体会 NEMU「接口先行」的设计哲学。
- **建议继续阅读的源码**：在实现综合实践前，先读一遍 [src/isa/riscv32/system/intr.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/system/intr.c) 与 RISC-V 的 `isa-def.h`，确认 `mepc`/`mcause`/`mtvec`/`mstatus` 等 CSR 是否已在 `CPU_state` 中定义，作为 u7-l21 的预习。
