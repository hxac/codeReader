# u5-l3 RTOS 资源约束与固件优化技巧

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 NanoVNA 的 RAM 预算表：16KB SRAM 被两个 512 字节栈、640 字节 sweep 线程工作区、4KB 显示缓冲、4.6KB 配置快照等如何瓜分。
2. 解释「main 线程栈 512 字节、实测最大占用 472 字节、仅剩 40 字节余量」这一注释背后的测量方法，并掌握用 `-fstack-usage` 生成的 `.su` 文件静态分析每个函数的栈消耗。
3. 说清一块 `spi_buffer`（2048 像素 × 2 字节 = 4096 字节）如何在不同时间扮演「显示画布 / FFT 临时缓冲 / 读屏接收缓冲」三重角色，以及这套复用为什么安全、在什么前提下会崩坏。
4. 列举固件压体积、省 RAM 的工程手法：`--specs=nano.specs`、链接器垃圾回收、`#pragma pack`、`ENABLE_*` 条件编译、ChibiOS 内核子系统全关。
5. 独立完成一次固件体积与栈占用的量化分析。

## 2. 前置知识

本讲站在两讲肩膀上，请先回顾：

- **u1-l2**：`make` 用 arm-none-eabi 工具链编译、docker 镜像构建、链接脚本把 128K Flash 划成 96K 程序区 + 32K 保存区、RAM 只有 16K。
- **u2-l5**：固件是双线程模型——低优先级 Thread1（sweep 线程）负责测量、UI、绘图；main 线程跑 USB shell；带 `CMD_WAIT_MUTEX` 标志的命令会移交给 sweep 线程执行。

本讲需要补充的几个术语：

| 术语 | 通俗解释 |
|---|---|
| MSP / PSP | Cortex-M 的两个栈指针：MSP（主栈）给中断和异常用；PSP（进程栈）给线程用。ChibiOS 里 main() 也是线程，跑在 PSP 上 |
| 工作区（Working Area） | ChibiOS 静态线程的「全部家当」：线程控制结构 + 私有栈，用 `THD_WORKING_AREA` 宏声明，大小以字节计 |
| 栈水位 | 栈在运行期间被压到的最深位置。一旦越过栈底就是栈溢出，轻则数据错乱、重则 HardFault |
| newlib-nano | GCC 面向嵌入式裁剪的 C 库变体，printf/整数除法等实现大幅缩水，代价是功能与合规性略降 |
| gc-sections | 让链接器把「没人引用的函数/变量」整段丢弃的机制，前提是编译时按函数/数据各自成段 |
| DMA | 直接内存访问，外设自己搬数据不占 CPU。本讲关注的是「DMA 和 CPU 同时碰同一块内存」的竞争问题 |

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [Makefile](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile) | 构建系统 | 两个栈尺寸变量、编译/链接选项、`-fstack-usage` |
| [main.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c) | 固件主体 | Thread1 工作区、栈水位注释、spi_buffer 复用、命令表裁剪、HardFault |
| [chconf.h](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chconf.h) | ChibiOS 内核配置 | 子系统全关的裁剪、栈检查与栈填充调试开关 |
| [STM32F072xB.ld](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/STM32F072xB.ld) | 链接脚本 | 96K/32K Flash 划分、16K RAM 上限 |
| [nanovna.h](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h) | 公共头文件 | `SPI_BUFFER_SIZE`、`properties_t` 布局与 0x1200 注释 |
| ili9341.c | LCD 驱动 | `spi_buffer` 的定义处、DMA 同步等待语义（作为佐证引用） |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**线程与栈大小**、**缓冲区复用**、**编译选项与体积分析**。

### 4.1 线程与栈大小：512 字节里做文章

#### 4.1.1 概念说明

STM32F072 只有 16KB SRAM。栈是 RAM 中「最凶的吞金兽」：每个线程一份、每层函数调用一份局部变量，而且必须按「最坏调用深度」预留——留少了溢出，留多了浪费。

NanoVNA 的线程模型极简，总共只有两个应用线程（外加 ChibiOS 内部的 idle 线程）：

- **main 线程**：跑 USB shell。栈来自 Makefile 变量 `USE_PROCESS_STACKSIZE = 0x200`（512 字节）。
- **sweep 线程（Thread1）**：跑测量、UI、绘图。工作区由源码里 `THD_WORKING_AREA(waThread1, 640)` 静态声明。
- **中断**：全部共用 MSP 主栈，`USE_EXCEPTIONS_STACKSIZE = 0x200`（512 字节）。

关键认知：**shell 不开独立线程，正是为了省一份栈**。u2-l5 讲过的「命令移交 sweep 线程执行」机制，在本讲视角下多了一层含义——它同时也是内存优化手段。

#### 4.1.2 核心流程

RAM 预算总账（数字全部可在源码中找到出处）：

| RAM 项目 | 字节数 | 出处 |
|---|---:|---|
| 中断/异常主栈 MSP | 512 | Makefile `USE_EXCEPTIONS_STACKSIZE = 0x200` |
| main 线程进程栈 PSP | 512 | Makefile `USE_PROCESS_STACKSIZE = 0x200` |
| sweep 线程工作区 waThread1 | 640 | main.c `THD_WORKING_AREA(waThread1, 640)` |
| 显示缓冲 spi_buffer | 4096 | nanovna.h `SPI_BUFFER_SIZE 2048` × 2 字节 |
| 当前配置快照 current_props | 4608 | nanovna.h `sizeof(properties_t) == 0x1200` 注释 |
| 测量数据 measured | 1616 | `float measured[2][101][2]` |
| 音频接收缓冲 rx_buffer | 384 | `int16_t rx_buffer[AUDIO_BUFFER_LEN * 2]` |
| **小计** | **≈13376** | 占 16KB 的 ≈82% |

剩下约 3KB 才轮到 ChibiOS 内核数据、idle 线程工作区、USB/I2C 驱动结构、plot/ui 的零散数组。这就是为什么固件处处透着「抠字节」的气质。

栈水位怎么测？固件用了经典的**填充模式扫描法**：

1. 线程创建时把整个工作区按字节填成固定填充值（`CH_DBG_FILL_THREADS` 打开时生效）；
2. 线程运行时压栈会覆盖掉栈顶方向的填充字节；
3. 想知道「用过多深」，就从栈底向上数还保持填充值的字节数，剩余 = 从未被碰过的部分：
   \[ \text{最大占用} = \text{工作区大小} - \text{剩余填充字节} \]

#### 4.1.3 源码精读

**两个 512 字节栈由 Makefile 决定**。[Makefile:L64-L74](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L64-L74)：`USE_PROCESS_STACKSIZE = 0x200` 是 main() 线程的进程栈，`USE_EXCEPTIONS_STACKSIZE = 0x200` 是处理中断和异常的主栈——两份各 512 字节，谁也不能省。

**sweep 线程工作区 640 字节**。[main.c:L106-L106](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L106-L106)：`static THD_WORKING_AREA(waThread1, 640);` 紧接着就是 u2-l5 精读过的 Thread1 主循环。

**作者留下的实测注释——全讲最有价值的一行**。[main.c:L2366-L2368](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2366-L2368)：

```c
// Main thread stack size defined in makefile USE_PROCESS_STACKSIZE = 0x200
// Profile stack usage (enable threads command by def ENABLE_THREADS_COMMAND) show:
// Stack maximum usage = 472 bytes (need test more and run all commands), free stack = 40 bytes
```

main 线程 512 字节栈，跑遍所有 shell 命令实测最深压到 472 字节，只剩 \(512 - 472 = 40\) 字节余量。注释还坦承「need test more」——这不是理论保证，而是实测水位。这行注释同时告诉我们：**shell 的所有 `cmd_*` 函数都在这 512 字节里跑**，你以后给 shell 新增命令时，命令处理函数的局部变量就直接消耗这 40 字节的冗余。

**「把 shell 挪进独立线程」是被注释掉的诱惑**。[main.c:L32-L37](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L32-L37)：

```c
// If need run shell as thread (use more amount of memory fore stack), after
// enable this need reduce spi_buffer size, by default shell run in main thread
// #define VNA_SHELL_THREAD
```

若打开 `VNA_SHELL_THREAD`，shell 获得独立工作区 [main.c:L2314-L2316](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2314-L2316)：`THD_WORKING_AREA(waThread2, /* cmd_* max stack size + alpha */442);`——注释直接写明这个数字就是「cmd_* 系列最大栈耗 + 裕量」。RAM 此消彼长：多一份 442 字节工作区，就得从别处（比如 spi_buffer）抠回来，所以默认不开。

**水位测量依赖两个调试开关**。[chconf.h:L374-L394](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chconf.h#L374-L394)：`CH_DBG_ENABLE_STACK_CHECK TRUE` 让内核在关键点校验栈指针是否越界，`CH_DBG_FILL_THREADS TRUE` 启用填充模式，两者都是发布版固件里仍然开着的（本身就是为 `threads` 命令服务的）。

**`threads` 命令——被裁剪掉的栈观测窗口**。[main.c:L2109-L2138](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2109-L2138)：`cmd_threads` 遍历线程注册表，[第 2126 行](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2125-L2127)就是填充扫描：`while(p[max_stack_use]==CH_DBG_STACK_FILL_VALUE) max_stack_use++;` 数出「从未用过的字节数」。但它默认编译不进去——入口被 [main.c:L63-L63](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L63-L63) 的 `//#define ENABLE_THREADS_COMMAND` 注释掉，且 [main.c:L2110-L2112](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2110-L2112) 用 `#error` 强制你先打开 `CH_CFG_USE_REGISTRY`（默认 FALSE，见 [chconf.h:L145-L150](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chconf.h#L145-L150)）——注册表本身要吃 Flash 和 RAM，观测能力的代价被作者明码标价。

**栈溢出的最后防线是死循环**。[main.c:L2457-L2476](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2457-L2476)：`HardFault_Handler` 把进程栈指针 PSP 取出来传给 `hard_fault_handler_c`，后者目前只做空转：

```c
void HardFault_Handler(void)
{
  uint32_t *sp;
  __asm volatile("mrs %0, psp \n\t" : "=r"(sp));
  hard_fault_handler_c(sp);
}

void hard_fault_handler_c(uint32_t *sp)
{
  (void)sp;
  while (true) {
  }
}
```

它是一个预留的调试桩：真机死机时你可以在 `while` 处下断点，从 `sp` 指向的硬件压栈帧里读 R0-R3、PC、LR，定位撞栈或非法访问的现场。开销为零——几乎没有代码。

#### 4.1.4 代码实践：解读并复算栈水位

1. **实践目标**：把作者的 472/40 注释变成你自己可复算、可扩展的结论。
2. **操作步骤**：
   - 阅读 [main.c:L2366-L2368](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2366-L2368) 注释；
   - 复算：512（0x200）− 472 = 40 字节余量；
   - 做个思想实验：如果你在某个 `cmd_*` 函数里加一个 `char buf[64];` 局部数组用于拼字符串，这 64 字节从哪里扣？结论是直接吞噬 40 字节余量并溢出——所以固件里的命令都直接 `shell_printf` 逐段输出而不是先拼大缓冲（参见 u5-l1）。
3. **需要观察的现象**：纯源码阅读 + 计算，无需硬件。
4. **预期结果**：你能准确说出「main 线程栈的硬上限是 512 字节、实测余量 40 字节」以及新增命令代码的栈预算从哪出。
5. 想在真机上看实测水位：取消 [main.c:L63](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L63-L63) 注释并把 [chconf.h:L150](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chconf.h#L145-L150) 的 `CH_CFG_USE_REGISTRY` 改为 TRUE，重编译烧录后敲 `threads`（**待本地验证**，注意会略微增大固件）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `USE_EXCEPTIONS_STACKSIZE` 和 `USE_PROCESS_STACKSIZE` 是两个独立变量？能否把中断栈降到 256 字节腾 RAM？

答案：前者是 MSP，给所有中断/异常处理程序共用（包括 USB 回调、I2S 的 `i2s_end_callback`，后者还要调 DSP 累加，u2-l3 讲过）；后者是 main 线程的 PSP。两者物理上独立、必须分别预留。理论上可降 MSP，但所有中断处理路径的调用深度以它为上限——`i2s_end_callback` 里已经做了「只做有界计算」的纪律来迁就这个预算，再降就是拿系统稳定性换 RAM，不划算。

**练习 2**：`THD_WORKING_AREA(waThread1, 640)` 里的 640 是什么单位？依据是什么？

答案：字节。佐证是 [main.c:L2315](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2314-L2316) 的 waThread2 注释「cmd_* max stack size + alpha」配的数值 442——与「栈最大占用 472 字节」处于同一量级，显然不是「字」（word，4 字节）计数。

**练习 3**：作者为什么不在发布版里关掉 `CH_DBG_ENABLE_STACK_CHECK` 省点代码？

答案：这个检查开销极小（上下文切换时比较一次栈指针与工作区下界），却能把「静默的栈溢出」变成「可发现的系统挂起」，对一个用户会自由输入命令、调用深度不可枚举的 shell 型固件，是性价比很高的保险。

### 4.2 缓冲区复用：一块 4KB spi_buffer 的三重身份

#### 4.2.1 概念说明

16KB SRAM 放不下 320×240×2 字节 = 150KB 的整屏帧缓冲（u4-l1 结论），所以显示走「立即模式」：每块要画的区域先在 CPU 侧的 `spi_buffer` 里拼好像素，再整块 DMA 推给 LCD。`spi_buffer` 因此是全固件最大的单块 RAM（4096 字节，约占总 RAM 的四分之一）。

固件没有 malloc（ChibiOS 的堆分配子系统整个被关掉，见 4.3），所有缓冲都是静态数组。静态分配的代价是「独占」——一块 4096 字节的缓冲如果只在 2% 的时间里被用到，其余 98% 时间就是死重。NanoVNA 的解法是**按时间片复用**：让几个「肯定不会同时发生」的使用者共享同一块内存。

#### 4.2.2 核心流程

`spi_buffer` 的三重身份，按 sweep 线程一次循环的时间轴排列：

```text
Thread1 循环体（同一栈、同一线程、严格串行）：
  ┌─ sweep()          测量（不碰 spi_buffer）
  ├─ transform_domain()   身份②：把 spi_buffer 当 float 数组用（FFT 临时缓冲）
  ├─ plot_into_index()    计算轨迹坐标（写入 trace_index，不碰 spi_buffer）
  └─ draw_all()       身份①：逐 cell 填 spi_buffer → ili9341_bulk → DMA 上屏
                         （DMA 在 bulk 内部被同步等完才返回）

另一个身份③（偶发）：shell 命令 capture
  → 带 CMD_WAIT_MUTEX 标志 → 移交 sweep 线程执行
  → 身份③：把 spi_buffer 当 LCD 读回缓冲（DMA 从屏写入，再逐字节吐给 USB）
```

安全性建立在两根支柱上：

1. **单写者串行**：所有碰 `spi_buffer` 的代码（绘图、FFT、capture）最终都在 sweep 线程里先后执行，绝无并发；
2. **DMA 同步等待**：显示 DMA 是阻塞式的，发数据前设好源地址，`dmaWaitCompletion` 等到搬完才返回，所以「上一个使用者」归还缓冲的时机是确定的。

#### 4.2.3 源码精读

**缓冲的定义与体量**。[nanovna.h:L307-L308](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L307-L308) 定义 `SPI_BUFFER_SIZE 2048`（单位是像素，每像素 16 位，即 4096 字节），[nanovna.h:L327](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L327-L327) 做 extern 声明，实体在 [ili9341.c:L24](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L24-L24)：`uint16_t spi_buffer[SPI_BUFFER_SIZE];`。

**身份②：FFT 临时缓冲**。[main.c:L194-L199](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L194-L199)：

```c
static void
transform_domain(void)
{
  // use spi_buffer as temporary buffer
  // and calculate ifft for time domain
  float* tmp = (float*)spi_buffer;
```

u3-l5 讲过时域变换：101 点测量数据要零填充到 `FFT_SIZE = 256`（[nanovna.h:L80](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L80-L80)）点再做 IFFT。作为复数 float 数组需要 256 × 2 × 4 = 2048 字节，恰好装进 4096 字节的 spi_buffer。若不复用，就要另开一块 2KB 静态数组——占掉剩余预算的大半。

**身份③：读屏接收缓冲**。[main.c:L727-L745](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L727-L745) 的 `cmd_capture`（u5-l1 讲过的截屏命令）：

```c
#if SPI_BUFFER_SIZE < (3*320 + 1)
#error "Low size of spi_buffer for cmd_capture"
#endif
  for (y = 0; y < 240; y += 2) {
    uint8_t *buf = (uint8_t *)spi_buffer;
    ili9341_read_memory(0, y, 320, 2, 2 * 320, spi_buffer);
```

注意两处防御：编译期 `#error` 保证缓冲够大（LCD 每像素回读 18 位即 3 字节，2 行 320 像素需 640×3+1 = 1921 字节 ≤ 4096，注释「read buffer limit by 2/3 + 1 from spi_buffer size」正是说每次只能读约 2/3 缓冲容量的像素，所以循环步进是 2 行）；运行期靠 `CMD_WAIT_MUTEX` 兜底——见命令表 [main.c:L2190](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2190-L2190) 中 `{"capture", cmd_capture, CMD_WAIT_MUTEX}`，命令被递交 sweep 线程执行，天然与 `draw_all` 串行。

**支柱二的证据：DMA 是同步等完的**。[ili9341.c:L212-L222](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L212-L222)：

```c
static void dmaStreamFlush(uint32_t len)
{
  while (len) {
    uint16_t tx_size = len > 65535 ? 65535 : len;
    dmaStreamSetTransactionSize(dmatx, tx_size);
    dmaStreamEnable(dmatx);
    len -= tx_size;
    dmaWaitCompletion(dmatx);
  }
}
```

`ili9341_bulk`（[ili9341.c:L458-L471](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L458-L471)）把 DMA 源地址设为 `spi_buffer` 后调用它——函数返回那一刻，DMA 一定已经读完毕，缓冲即刻「归还」。假如这里改成异步（发起后立即返回），下一行代码改写 spi_buffer 就会和 DMA 读操作赛跑。

**时间轴总指挥**。[main.c:L131-L147](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L131-L147)：`transform_domain()`（第 132 行）在 `plot_into_index`/`draw_all`（第 134、146 行）**之前**完成对 spi_buffer 的全部读写——同一循环体内先后执行，这是复用成立的时序前提。

#### 4.2.4 代码实践：竞争风险推演

1. **实践目标**：亲手论证「复用为什么现在没事」，以及「哪两种改动会立刻翻车」。
2. **操作步骤**：
   - 假设场景 A：有人把 `dmaStreamFlush` 改成异步（发起 DMA 立即返回）以提高刷新并行度。推演 `draw_all` 刚返回、下一轮循环 `transform_domain` 开始 `memcpy` 写 spi_buffer 时会发生什么；
   - 假设场景 B：有人写了个新的 shell 命令直接调 `ili9341_bulk` 画东西，且忘了加 `CMD_WAIT_MUTEX` 标志。这个命令在 main 线程执行，推演它与 sweep 线程的 `draw_all` 同时填 spi_buffer 的后果；
   - 对照 [main.c:L2104-L2105](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2104-L2105)：`cmd_color` 改颜色后为什么只 `redraw_request |= REDRAW_AREA` 而不直接重画？从缓冲安全角度再解释一遍。
3. **需要观察的现象**：纸面推演（本机无硬件，标注**待本地验证**的部分：若真机可用，可故意构造场景 B 观察花屏）。
4. **预期结果**：A 会造成上一帧 DMA 还在读、CPU 已把 FFT 的 float 位模式写进同一块内存——屏幕上出现杂乱色块；B 是两个线程同时写同一缓冲，画面随机的撕裂/花屏，且依赖时序、难以复现；`cmd_color` 只置标志正是为了让实际绘图收敛到 sweep 线程的 `draw_all`，这也是 u2-l5「请求-响应」模型在内存安全层面的第二个用途。

#### 4.2.5 小练习与答案

**练习 1**：FFT 需要 2048 字节，spi_buffer 有 4096 字节，多出来的 2048 字节是不是浪费？

答案：不是——同一块缓冲还要伺候 `cmd_capture`：读屏每像素占 3 字节，单次需要最多 3×640+1 ≈ 1.9KB，加上直接当像素画布时是 2048 像素×2 字节 = 4096 字节整块用满。缓冲按「所有使用者中最大的那个」定容，复用本身就是让不同峰值需求共享同一块内存。

**练习 2**：`transform_domain` 里 `float* tmp = (float*)spi_buffer;` 这种类型双关（uint16_t 数组当 float 数组用）有没有对齐隐患？

答案：没有。`spi_buffer` 是静态数组，静态存储期对象按其类型对齐（uint16_t 数组至少 2 字节对齐），但作为 4096 字节的全局大数组，实践中的放置满足 4 字节对齐（Cortex-M0 上 float 访问要求 4 字节对齐）。真正值得警惕的是可移植性而非本平台正确性——这是嵌入式代码常见的「依赖目标平台特性」的取舍。

**练习 3**：如果未来新增一个「蓝牙线程」也要画屏，最省 RAM 的正确做法是什么？

答案：不要给蓝牙线程开新的显示缓冲，而是沿用现有约定：蓝牙线程只置 `redraw_request` 标志（或把绘制请求打包成函数指针交给 sweep 线程），所有真正碰 `spi_buffer` 的绘制仍收敛在 sweep 线程内。「单写者」纪律比「多缓冲」便宜得多。

### 4.3 编译选项与体积分析：把 96K Flash 和 16K RAM 花在刀刃上

#### 4.3.1 概念说明

链接脚本（[STM32F072xB.ld:L20-L38](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/STM32F072xB.ld#L20-L38)）划定硬边界：程序区 96K（flash0 @0x08000000）、保存区 32K（flash7 @0x08018000，放校准数据）、RAM 16K。固件功能多（USB 协议栈、RTOS、DSP、FFT、图形、shell），每一项都在挤压这两个数字。

体积优化分四层，从粗到细：

| 层 | 手段 | 效果 |
|---|---|---|
| C 库层 | `--specs=nano.specs` | 换用 newlib-nano，printf 等大幅缩水 |
| 链接层 | `-ffunction-sections` + `--gc-sections`（USE_LINK_GC=yes） | 没被引用的函数整段删除 |
| 编译层 | `-O2`、`-fno-inline-small-functions`、`-fomit-frame-pointer` | 体积与速度平衡；禁止小函数内联膨胀 |
| 源码层 | `ENABLE_*` 条件编译、内核子系统全关、`#pragma pack`、结构体布局 hack | 不编译就用不占空间；字段排布省 padding |

#### 4.3.2 核心流程

一次体积裁剪的决策流：

```text
功能默认是否人人需要？
 ├─ 否 → 用 #ifdef ENABLE_XXX_COMMAND 包住（time/threads/dump 直接不编）
 ├─ RTOS 子系统用不到吗？
 │    └─ 是 → chconf.h 里 CH_CFG_USE_* 全置 FALSE（信号量/互斥量/邮箱/堆…）
 ├─ 结构体能否紧排？
 │    └─ 命令表 50+ 条，每条省 2 字节 padding 就是 ~100 字节 → #pragma pack(2)
 └─ 仍不够 → 用「脏 hack」：依赖结构体字段连续布局，用数组下标代替 switch
```

`-fstack-usage` 则是另一条暗线：它让 GCC 为每个编译单元生成 `.su` 文件，逐函数记录静态栈消耗，供我们做 4.1 的水位核算——一个为「512 字节栈」这种极端约束专门打开的选项。

#### 4.3.3 源码精读

**编译选项一行浓缩四招**。[Makefile:L7-L9](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L7-L9)：

```makefile
USE_OPT = -O2 -fno-inline-small-functions -ggdb -fomit-frame-pointer -falign-functions=16 --specs=nano.specs -fstack-usage
```

逐项拆解：`-O2` 平衡速度与体积；`-fno-inline-small-functions` 反直觉但正确——内联省的是调用开销、涨的是每处展开的代码体积，Flash 紧张时必须抑制；`-ggdb` 保留调试信息（不影响固件体积，调试段不下载进芯片）；`-fomit-frame-pointer` 释放 R7 当通用寄存器，更小更快；`-falign-functions=16` 函数 16 字节对齐；`--specs=nano.specs` 换精简 C 库；`-fstack-usage` 生成 `.su`。

**链接层与指令集**。[Makefile:L21-L39](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L21-L39)：`USE_LINK_GC = yes` 开启按段垃圾回收；`USE_LTO = no`——LTO（链接时优化）能再省一点，但会拖慢构建且与本项目的模块化编译不匹配，作者选择不开；`USE_THUMB = yes` 对应 [Makefile:L190](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L190-L190) 的 `-mthumb`，Cortex-M0 只支持 Thumb，其 16 位编码密度天然省 Flash。

**命令表的条件编译裁剪**。开关集中在 [main.c:L61-L71](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L61-L71)：

```c
//#define ENABLED_DUMP
// Allow get threads debug info
//#define ENABLE_THREADS_COMMAND
// RTC time not used
//#define ENABLE_TIME_COMMAND
// Enable vbat_offset command, allow change battery voltage correction in config
#define ENABLE_VBAT_OFFSET_COMMAND
// Info about NanoVNA, need fore soft
#define ENABLE_INFO_COMMAND
// Enable color command, allow change config color for traces, grid, menu
#define ENABLE_COLOR_COMMAND
```

每个开关对应命令表里的一段 `#ifdef`，如 `time`（[main.c:L2159-L2161](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2159-L2161)）、`dump`（L2166-L2168）、`vbat_offset`（L2192-L2194）、`info`（L2198-L2200）、`color`（L2201-L2203）、`threads`（L2204-L2206）。裁掉一个命令 = 少一个函数体 + 少一行表项。RTC 的 `time` 命令因为硬件上根本没 RTC 而被永久关闭，注释「RTC time not used」说明这是**按硬件配置裁剪**的范例。

**RTOS 内核子系统全关**。[chconf.h:L140-L313](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chconf.h#L140-L313)：`CH_CFG_USE_TM`、`_WAITEXIT`、`_SEMAPHORES`、`_MUTEXES`、`_EVENTS`、`_MESSAGES`、`_MAILBOXES`、`_QUEUES`、`_MEMCORE`、`_HEAP`、`_MEMPOOLS`、`_DYNAMIC`、`_REGISTRY` 全部 FALSE。u2-l5 说过固件靠标志位而不是互斥量做线程协作——现在你看到了另一半动机：**每关一个子系统，内核就少编一批 API 和数据结构**。「架构上不用」与「体积上不编」互为因果。

**命令表按 2 字节打包**。[main.c:L2143-L2149](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2143-L2149)：

```c
#pragma pack(push, 2)
typedef struct {
  const char           *sc_name;
  vna_shellcmd_t    sc_function;
  uint16_t flags;
} VNAShellCommand;
#pragma pack(pop)
```

不打包时指针(4)+指针(4)+uint16(2) 会被补齐到 12 字节；pack(2) 后 10 字节。表里 50 来条命令，省出约 100 字节 Flash——杯水车薪也要省，这是小内存项目的常态。

**结构体布局「脏 hack」**。[main.c:L2093-L2103](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2093-L2103)：`cmd_color` 本可用 switch 分派四个颜色字段，实际却直接 `config.trace_color[i] = color;`，注释自曝「WARNING!!! Dirty hack for size, depend from config struct」——它依赖 `grid_color`/`menu_normal_color`/`menu_active_color`/`trace_color[]` 在 `config_t` 中连续排列的布局事实，用负下标统一寻址，换掉整个 switch。省了代码，代价是 `config_t` 一旦调整字段顺序这里就悄悄错——「依赖布局」类优化的典型风险。

**体积观测工具链**：ChibiOS 的 rules.mk 在链接后自动跑 `arm-none-eabi-size`（[Makefile:L182](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L182-L182) 定义 `SZ = $(TRGT)size`），构建尾部打印 text/data/bss 三行——text 进 Flash，data 双份（Flash 存初值 + RAM 存运行值），bss 只占 RAM。

#### 4.3.4 代码实践：用 .su 文件找出最耗栈的 5 个函数

1. **实践目标**：量化「谁在吃栈」，并与 472/40 的实测注释互相印证。
2. **操作步骤**：
   - 按 u1-l2 的方法完整编译一次（本地工具链或 docker 镜像 `edy555/arm-embedded:8.2`），确认构建尾部 `size` 输出并记录 text/data/bss；
   - `-fstack-usage` 已在 [Makefile:L8](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L7-L9) 默认开启，每个 `.o` 旁边都会有一个同名 `.su` 文件。每行格式为 `文件:行:列:函数名<TAB>字节数<TAB>限定符`（限定符为 static/dynamic/bounded）；
   - 汇总排序（在项目根目录执行）：

     ```bash
     find build -name '*.su' -print0 | xargs -0 cat \
       | awk -F'\t' '{print $2"\t"$1}' | sort -rn | head -5
     ```

   - 只看本项目文件时可再加 `| grep -v ChibiOS` 过滤内核代码；
   - 对照 main.c 的 `.su` 中的 `cmd_*` 条目与 [main.c:L2366-L2368](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2366-L2368) 的 472 字节：注意静态 `.su` 只计单函数自身帧，完整水位还需叠加其调用链上的各层帧。
3. **需要观察的现象**：排序榜上哪些函数名列前茅；`VNAShell_executeLine` 及各 `cmd_*` 函数的静态栈帧是否明显偏大；ChibiOS 内核函数（如线程切换相关）的单帧大小。
4. **预期结果**：**待本地验证**——预期 shell 命令处理链（`VNAShell_executeLine` → `cmd_*`）与 `transform_domain`、FFT、`cal_interpolate` 这类带大局部数组的函数占据榜首，且 main 线程方向各函数帧之和与「实测 472 字节」同量级。若发现某 `cmd_*` 单帧超过 40 字节余量，说明该命令理论上已可致栈溢出，值得写进你的分析报告。
5. 若无法运行 arm 工具链，退化方案：`grep -n "char .*\[[0-9]" main.c ui.c plot.c` 找大局部数组，结合调用链手算各命令的栈帧，同样能得出定性结论。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `USE_LTO = no`？LTO 不是通常都能减小体积吗？

答案：LTO 通过跨模块优化确实常能再省几个百分点，但会让构建显著变慢、可读的 `.su`/调试信息更难对应源码，且收益不确定。这个项目用更直接的手段（不编译没用的东西：ENABLE_* 裁剪、内核子系统全关、gc-sections）已经解决了主要矛盾，作者选择保持构建简单快速——工程上「确定的大头」优先于「不确定的零头」。

**练习 2**：`#pragma pack(push, 2)` 对 `VNAShellCommand` 有什么副作用？

答案：结构体总大小从 12 字节（2 字节 tail padding）降到 10 字节，但 `flags` 字段后整个结构体不再自然对齐——如果将来在表里放一个含 double 或需要 8 字节对齐成员的结构，未打包上下文里的取址可能低效甚至（在要求严格对齐的目标上）出错。对本结构（两个指针 + uint16）纯属红利。

**练习 3**：`config`（config_t）在 RAM 里，`commands[]` 命令表在哪？为什么这样安排？

答案：`commands[]` 是 `static const`，落在 Flash 的 rodata 段——表内容运行期从不修改，放 Flash 既省 RAM 又掉电不丢。这是「能进 Flash 的绝不占 RAM」原则的标准应用；同理 `sincos_tbl`（u2-l4）与字体位图（u4-l1）也都在 Flash。

## 5. 综合实践：栈画像 + 复用取舍报告

把本讲三个模块串成一份 200 字左右的工程取舍分析，产出两样东西：

**第一部分：栈画像（对应 4.1 + 4.3）**

1. 完整编译固件，记录 `arm-none-eabi-size` 的 text/data/bss；
2. 用 4.3.4 的命令找出最耗栈的 5 个函数，抄录函数名与字节数；
3. 沿 `VNAShell_executeLine → cmd_scan → sweep` 画出 main/sweep 两条调用链上各函数的静态栈帧累加，与 512 字节（main 线程）和 640 字节（waThread1）两个上限对比，标注最紧的一条链。

**第二部分：`transform_domain` 复用 `spi_buffer` 的取舍分析（约 200 字）**

围绕以下要点组织你的文字（写成自己的话，不要照抄）：

- 收益：免开 2KB 独立 FFT 缓冲，等于白捡总 RAM 的 1/8；`cmd_capture` 再省约 2KB 读屏缓冲——实际是「三个峰值需求共享一块 4KB」；
- 成立前提：①所有使用者（`transform_domain`、`draw_all`、`cmd_capture`）经 `CMD_WAIT_MUTEX` 与 Thread1 循环收敛到单线程串行；②`dmaStreamFlush` 内 `dmaWaitCompletion` 同步等完，缓冲归还时机确定（[ili9341.c:L212-L222](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ili9341.c#L212-L222)）；
- 风险：约束是隐式的、靠约定维持——新代码若在其他线程/中断直接写屏，或把显示 DMA 改成异步，CPU 改写 `tmp` 时 DMA 仍在读 `spi_buffer`，屏幕出现依赖时序的花屏，极难复现；`cmd_capture` 处还叠加编译期 `#error` 容量检查（[main.c:L733-L735](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L733-L735)）这种脆弱的容量耦合；
- 结论：在 16KB 预算下这笔交换划算，但应在代码注释与团队约定里把「spi_buffer 仅限 sweep 线程」写成显式契约。

**验收标准**：栈画像有真实数字（或明确的「待本地验证」声明）；取舍分析覆盖收益、前提、风险三要素；字数 200 字左右。

## 6. 本讲小结

- 16KB RAM 的账本：两个 512 字节栈（MSP 中断栈 + main 线程 PSP）、640 字节 sweep 线程工作区、4096 字节 spi_buffer、4608 字节 current_props，大头合计已占约 82%。
- main 线程 512 字节栈实测最深 472 字节、余量仅 40 字节——所有 shell 命令都在这条钢丝上跑；栈水位靠 `CH_DBG_FILL_THREADS` 填充扫描法测量，`threads` 命令是观测窗口（默认裁掉，需开 `CH_CFG_USE_REGISTRY`）。
- `spi_buffer` 一块内存三重身份（显示画布 / FFT 临时缓冲 / 读屏缓冲），安全性完全依赖「单线程串行 + DMA 同步等待」两条纪律；违反即花屏，且难复现。
- 体积优化四层组合拳：newlib-nano、gc-sections、`-fno-inline-small-functions` 等编译选项、源码层 `ENABLE_*` 裁剪与内核子系统全关；连命令表都要 `#pragma pack(2)` 抠出 100 字节。
- `-fstack-usage` 生成的 `.su` 文件 + `arm-none-eabi-size` 是这套约束下的标准观测工具，可静态定位最耗栈的函数。
- HardFault 处理是零开销调试桩：读 PSP、空转待断点，为栈溢出等致命错误保留现场。

## 7. 下一步学习建议

下一讲 **u5-l4 二次开发实战：为固件添加新特性** 将把全手册知识收束成一个毕业项目——实现「测量平均」功能。届时本讲的每一项约束都会变成你设计决策的输入：新增的累加缓冲从哪里出（考虑复用而非新开静态数组）、新增 shell 命令的栈帧预算（那 40 字节余量）、新增配置字段如何塞进 `properties_t` 的 `_reserved[49]`（[nanovna.h:L383](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L383-L383) 正是为此预留的对齐填充）。

在进入下一讲前，建议顺手通读 [main.c:L2314-L2345](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2314-L2345)（shell 线程的备选方案与 main 主循环）和 [chconf.h:L140-L313](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/chconf.h#L140-L313)（内核裁剪清单），对照本讲自查：每一项 FALSE 背后对应着固件里哪段「不用它也实现了」的代码。
