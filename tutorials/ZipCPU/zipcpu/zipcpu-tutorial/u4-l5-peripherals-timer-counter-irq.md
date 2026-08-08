# 外设：中断控制器、定时器、计数器与 Jiffies

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `icontrol` 如何用一个 32 位寄存器把多路外部中断「合并、屏蔽、应答」成一根中断线。
- 区分三种「和时间有关」的外设：`ziptimer`（向下计数到 0 中断）、`zipcounter`（向上计数、溢出中断）、`zipjiffies`（一直向上计数、写一个未来值到点中断）。
- 理解 `wbwatchdog` 总线看门狗与「可写超时值」的普通看门狗之间的区别。
- 知道这些外设在 `zipsystem` 内部总线上的地址，以及它们的中断如何汇入主 PIC。
- 能为「让 CPU 每 \(N\) 个时钟收到一次中断」选对外设、写对寄存器。

## 2. 前置知识

本讲假设你已经读过 [u4-l2 ZipSystem 整合](u4-l2-zipsystem-integration.md)，知道：

- `zipsystem` 把内核和一组外设挂在同一条内部 `sys` Wishbone 总线上。
- CPU 字节地址 \(= 0xFF000000 + \text{sys\_addr}\times 4\)，即外设地址从 `0xFF000000` 起、按字（4 字节）递增。
- 这些外设的中断最终汇成一根 `pic_interrupt` 线送进 CPU；CPU 本身只有一根中断线（见 [u2-l5 中断与双寄存器组](u2-l5-interrupts-dual-regset.md)），多路中断必须先由中断控制器「或」在一起。

需要先建立的几个小概念：

- **电平触发（level-triggered）中断**：中断源只要保持高电平，就认为「一直有中断」。ZipCPU 的 `icontrol` 就是按电平触发的——它把外部中断线「或」进一个状态寄存器，CPU 不应答就不会自动消失。
- **时钟使能 `i_ce`**：很多外设有一个 `i_ce` 输入，只有它为 1 时计数器才走。在 `zipsystem` 里，所有定时器的 `i_ce` 都接 `!cmd_halt`，即「CPU 没被调试器暂停时才计时」。这样在调试器里单步时，定时器会一起停下来，方便对齐。
- **单周期 Wishbone 从设备**：本讲的五个外设都是「0 拍停顿、下一拍 ack」的极简从设备（`o_wb_stall = 0`，`o_wb_ack` 在 `i_wb_stb` 的下一拍拉高）。它们各自只占很少几个地址。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [rtl/peripherals/icontrol.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/icontrol.v) | 可编程中断控制器（PIC）。把最多 15 路中断合并成 1 根线，提供屏蔽/应答。 |
| [rtl/peripherals/ziptimer.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/ziptimer.v) | 31 位向下计数定时器，到 0 拉一次中断，可选自动重装。 |
| [rtl/peripherals/zipcounter.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipcounter.v) | 极简向上计数器，对外部事件计数，溢出（回绕）时中断。 |
| [rtl/peripherals/zipjiffies.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipjiffies.v) | 一直向上计数的「jiffies」计数器，写入一个未来时间点以在该点中断。 |
| [rtl/peripherals/wbwatchdog.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/wbwatchdog.v) | 总线看门狗：超时值由硬件常量给定，到点后锁死中断直到复位。 |
| [rtl/zipsystem.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v) | 把上述外设实例化、分配地址、把中断接进 PIC 的「整合层」。 |

先看一张整体连接图（文字版），建立全局印象：

```
        外部中断线 i_ext_int ─┐
   Timer A/B/C ──┐            │
   Jiffies ──────┤            ├──> 主 PIC (icontrol) ──> pic_interrupt ──> CPU
   DMA ──────────┤            │            ^
   计数器组 ─────┘──> 辅 PIC ─┘            │
                                  (一根线进 CPU)
```

也就是说：定时器/计数器/Jiffies 各自的中断先按位拼成 `main_int_vector`，再送进主 PIC 的 `i_brd_ints`；PIC 根据屏蔽位和总使能位决定是否真正拉响 `pic_interrupt`。

## 4. 核心概念与源码讲解

### 4.1 中断控制器 icontrol：多路合并、屏蔽与应答

#### 4.1.1 概念说明

ZipCPU 的 CPU 核只有一根中断输入线（见 u2-l5），但一个真实系统通常有十几路中断源（定时器、DMA、外部引脚……）。`icontrol` 就是解决「多进一出」的可编程中断控制器（PIC）。它的设计哲学是：

- **只占一个 Wishbone 地址**，所有控制塞进一个 32 位字。
- **0 拍延迟**：读它不增加任何等待周期。
- **一次写就能完成「使能 + 应答」**，常用操作单周期搞定。

这个 32 位字被切成四段：

| 段 | 位 | 含义 |
|----|----|------|
| 总使能 | bit 31 | 全局中断使能 `r_mie`，关掉则一律不响 |
| 逐路使能 | bits 16…30 | 每一位对应一路中断的「允许响铃」开关 |
| 任意挂起 | bit 15 | 只要有任何挂起中断（不论是否使能），这一位为 1 |
| 挂起状态 | bits 0…14 | 每一位对应一路中断「是否正在挂起」 |

#### 4.1.2 核心流程

`icontrol` 内部就三个寄存器：挂起状态 `r_int_state`、逐路使能 `r_int_enable`、总使能 `r_mie`。每一拍：

1. **采样外部中断**：把外部输入 `i_brd_ints`「或」进 `r_int_state`（电平触发，只要线为高就一直记着）。
2. **CPU 写入时做三件事**，巧妙地用 bit 15 当「写方向」开关：
   - bit 15 = 1 → 这是「使能写」：bit 31 决定 `r_mie` 置 1，bits 16+ 把对应使能位置 1；同时 bits 0..14 里写 1 的位会**清除**挂起（应答中断）。
   - bit 15 = 0 → 这是「禁能写」：bit 31 决定 `r_mie` 清 0，bits 16+ 把对应使能位清 0。
3. **判决**：只要 `r_mie` 为 1 且「挂起状态 按位与 使能」非零，就让 `w_any` 为真，下一拍 `o_interrupt` 拉高。

注意 bit 15 的双重身份：**读**时它是「任意挂起」指示位；**写**时它被复用为「这次写是要使能还是禁能」的方向标志。这是一个很省位的把戏。

#### 4.1.3 源码精读

模块端口与参数 `IUSED`（实际使用的中断路数，1~15）见 [rtl/peripherals/icontrol.v:L80-L95](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/icontrol.v#L80-L95)。设计意图的注释（含位布局）在 [rtl/peripherals/icontrol.v:L19-L48](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/icontrol.v#L19-L48)。

写方向的判定，bit 15 当开关：

```verilog
assign wb_write     = (i_wb_stb)&&(i_wb_we);
assign enable_ints  = (wb_write)&&( i_wb_data[15]);
assign disable_ints = (wb_write)&&(!i_wb_data[15]);
```

见 [rtl/peripherals/icontrol.v:L106-L108](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/icontrol.v#L106-L108)。

挂起状态寄存器——外部中断「或」进来，写 1 清除（应答）：

```verilog
always @(posedge i_clk)
if (i_reset)
    r_int_state  <= 0;
else if (wb_write)
    r_int_state <= i_brd_ints | (r_int_state & (~i_wb_data[(IUSED-1):0]));
else
    r_int_state <= (r_int_state | i_brd_ints);
```

见 [rtl/peripherals/icontrol.v:L116-L123](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/icontrol.v#L116-L123)。注意写操作里 `r_int_state & (~i_wb_data)` 表示「把写 1 的那些挂起位清掉」，所以「写 1 = 应答该路中断」。

逐路使能与总使能见 [rtl/peripherals/icontrol.v:L131-L152](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/icontrol.v#L131-L152)；最终中断输出：

```verilog
assign w_any = ((r_int_state & r_int_enable) != 0);
...
o_interrupt <= (r_mie)&&(w_any);
```

见 [rtl/peripherals/icontrol.v:L156-L167](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/icontrol.v#L156-L167)。读回的数据把这四段拼成一个字，见 [rtl/peripherals/icontrol.v:L173-L183](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/icontrol.v#L173-L183)。0 拍停顿、当拍 ack 见 [rtl/peripherals/icontrol.v:L185-L186](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/icontrol.v#L185-L186)。

#### 4.1.4 代码实践

**实践目标**：用 spec 给出的宏理解一次「使能 Timer A 中断」该往 PIC 写什么值。

**操作步骤**：

1. 打开 [doc/src/spec.tex:L2031-L2035](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2031-L2035)，记下三个宏：
   - `EINT(A) = 0x80008000 | (A<<16)`（使能中断源 A：bit31 总使能 + bit15 方向位 + 使能位）
   - `SYSINT_TMA = 0x10`（Timer A 是第 4 号中断源，bit4）
2. 把 `EINT(SYSINT_TMA)` 展开成二进制，对照本节的位布局表，指出每一位置 1 的含义。

**预期结果**：`EINT(SYSINT_TMA) = 0x80008000 | (0x10<<16) = 0x80108000`。其中 bit31=1（开总使能）、bit20=1（使能第 4 号，即 Timer A）、bit15=1（声明这是使能写）。这一行就解释了 spec 示例里 `zip->z_pic = EINT(SYSINT_TMA);` 在做什么。**待本地验证**：你可以在仿真里读回 PIC 寄存器，确认 bit20 与 bit31 都已置位。

#### 4.1.5 小练习与答案

**练习 1**：想让 CPU 收到「Timer A 挂起」的中断，PIC 的总使能、Timer A 使能、Timer A 挂起三者分别必须是什么状态？
**答案**：三者必须同时为 1。任何一环为 0 都不会拉响 `o_interrupt`（见 `w_any = r_int_state & r_int_enable`，再乘 `r_mie`）。

**练习 2**：进入中断处理程序后，CPU 应该往 PIC 写什么来「应答（清掉）Timer A 的挂起」而不影响别的中断？
**答案**：写一个 bit15=0、且 bits[0..14] 中只有第 4 位为 1 的值，即 `DINT(0x10) | 0x10`（禁能写形式 + 在挂起位上写 1 清除）。注意如果还想保持 Timer A 使能，需要随后再做一次使能写。

---

### 4.2 ziptimer：向下计数到 0 中断

#### 4.2.1 概念说明

`ziptimer` 是 ZipCPU 的「标准定时器」。它是一个 31 位（`VW = BW-1`）的**向下计数器**：写一个值进去就开始倒数，数到 0 拉一次中断。最高位（bit 31）是「自动重装」开关：开了它，每次到 0 都会自动重新装入上次的值，于是变成「周期性闹钟」。

源码头注释把行为讲得很清楚：写入 5，就会数 `5, 4, 3, 2, 1, 中断`，即 5 个时钟后响一次（见 [rtl/peripherals/ziptimer.v:L9-L41](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/ziptimer.v#L9-L41)）。

#### 4.2.2 核心流程

1. CPU 写入一个非零值 `V`：`r_value` ← `V`，`r_running` ← 1（开始运行）；写 0 则停止。
2. 每个 `i_ce` 拍：若未到 0，`r_value` 减 1；若刚好从 1 变到 0：
   - 拉高一拍 `o_int`（中断只响一个时钟）。
   - 若 `auto_reload`，则下一拍把 `r_value` 重装为 `interval_count`（上次写入的值），继续周期运行；否则 `r_running` 清 0、停表。
3. 读返回 `{auto_reload, r_value}`。

中断周期（自动重装模式）：

\[
T_{\text{int}} = V \cdot T_{\text{clk}} \quad (\text{每 } V \text{ 个 } i\_ce \text{ 时钟响一次})
\]

#### 4.2.3 源码精读

模块端口见 [rtl/peripherals/ziptimer.v:L73-L92](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/ziptimer.v#L73-L92)，注意 `i_ce` 是计数使能。运行标志与「写 0 即停」：

```verilog
always @(posedge i_clk)
if (i_reset)               r_running <= 1'b0;
else if (wb_write)         r_running <= (|i_wb_data[(VW-1):0]);
else if ((r_zero)&&(!auto_reload)) r_running <= 1'b0;
```

见 [rtl/peripherals/ziptimer.v:L110-L118](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/ziptimer.v#L110-L118)。

计数值的递减与重装：

```verilog
else if ((i_ce)&&(r_running)) begin
    if (!r_zero)                        r_value <= r_value - 1'b1;
    else if (auto_reload)               r_value <= interval_count;
end
```

见 [rtl/peripherals/ziptimer.v:L165-L178](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/ziptimer.v#L165-L178)。到 0 拉一拍中断：

```verilog
else // if (i_ce)
    o_int <= (r_value == { {(VW-1){1'b0}}, 1'b1 });
```

见 [rtl/peripherals/ziptimer.v:L200-L206](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/ziptimer.v#L200-L206)。自动重装逻辑由综合期参数 `RELOADABLE`（默认 1）裁剪，关掉则不生成重装硬件，见 [rtl/peripherals/ziptimer.v:L122-L160](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/ziptimer.v#L122-L160)。在 `zipsystem` 里它被实例化三次（Timer A/B/C），`i_ce` 都接 `!cmd_halt`，地址用 `sys_addr[1:0]` 区分，见 [rtl/zipsystem.v:L1355-L1395](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1355-L1395)。

#### 4.2.4 代码实践

**实践目标**：理解「周期中断」该怎么写。

**操作步骤**：阅读 spec 的 `timer_delay` 示例 [doc/src/spec.tex:L2037-L2047](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2037-L2047)。它是「单次延时」用法（不开自动重装）。请你把示例改成「每 `nclocks` 个时钟永久响一次」。

**预期结果**：只需在写定时器值时把最高位（自动重装位）置 1，即 `zip->z_tma = 0x80000000 | nclocks;`，其余流程不变。**待本地验证**：在中断处理里不要停表，观察是否真的周期性进入中断。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ziptimer` 的中断位宽是 31（`VW = BW-1`）而不是 32？
**答案**：第 32 位（bit 31）被挪去当「自动重装」控制位了，所以真正能用的计数值只有 31 位，最大重装值 \(2^{31}-1\)。

**练习 2**：`o_int` 会持续高电平吗？CPU 没及时应答会怎样？
**答案**：不会，`o_int` 只在「数到 0 那一拍」高一个时钟。但 PIC（icontrol）会把这个脉冲「或」进挂起寄存器并保持，所以 CPU 哪怕慢几拍也能看到。在自动重装模式下，若 CPU 处理太慢，下一次到 0 会再来一个脉冲，但挂起位仍是那一位（不会叠加计数）。

---

### 4.3 zipcounter：向上计数、溢出中断（与 ziptimer 的本质区别）

#### 4.3.1 概念说明

`zipcounter` 是 ZipCPU 里最简单的外设：一个**向上**计数器，对「外部事件」`i_event` 计数，溢出（从全 1 回绕到 0）时拉一次中断。关键差异在于——它数的不是「时钟」，而是任意事件；它**不能停**（没有停止位），只能写一个值重新开始。

源码注释说明了它的设计意图：用于「进程记账」——在每个任务开始时把计数器清零，结束时读回，就知道这个任务花了多少时钟、停顿了多少次、执行了多少条指令（见 [rtl/peripherals/zipcounter.v:L7-L22](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipcounter.v#L7-L22)）。

#### 4.3.2 核心流程

整个外设的核心逻辑浓缩在**一个 always 块**里：

```verilog
if (i_reset)
    { o_int, o_wb_data } <= 0;
else if ((i_wb_stb)&&(i_wb_we))
    { o_int, o_wb_data } <= { 1'b0, i_wb_data };      // 写：置新值、清中断
else if (i_event)
    { o_int, o_wb_data } <= o_wb_data + {{(BW-1){1'b0}},1'b1};  // 事件：+1
else
    o_int <= 1'b0;                                     // 否则：清中断
```

这里有一个非常优雅的把戏：`{ o_int, o_wb_data }` 拼成 33 位，做 `o_wb_data + 1`。当 `o_wb_data` 为全 1（`0xFFFFFFFF`）时，加 1 的进位自然溢出到最高位 `o_int`，于是 `o_int` 在回绕那一拍恰好为 1——**中断位就是加法的进位输出**。其它情况进位为 0。

#### 4.3.3 源码精读

端口与计数逻辑见 [rtl/peripherals/zipcounter.v:L55-L90](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipcounter.v#L55-L90)。`o_wb_ack` 仍是「下一拍 ack」，`o_wb_stall = 0`，见 [rtl/peripherals/zipcounter.v:L94-L101](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipcounter.v#L94-L101)。

在 `zipsystem` 里它被实例化 8 次，构成「性能计数器组」，每个实例的 `i_event` 接不同的 CPU 内部信号，从而统计不同的事件。例如「主任务时钟计数器」对 `!cmd_halt` 计数（CPU 在运行的时钟数）：

```verilog
zipcounter mtask_ctr(
    i_clk, 1'b0, (!cmd_halt), sys_cyc,
    (sys_stb)&&(sel_counter)&&(sys_addr[2:0] == 3'b000), sys_we, sys_data,
    mtc_stall, mtc_ack, mtc_data, mtc_int
);
```

见 [rtl/zipsystem.v:L1049-L1057](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1049-L1057)。8 个计数器分别数：主/用户的「任务时钟、操作数停顿、预取停顿、指令数」，靠 `sys_addr[2:0]` 区分（地址 `0x20`~`0x3c`），见 [rtl/zipsystem.v:L1099-L1140](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1099-L1140)。用户组计数器的 `i_event` 还多了一个 `&& cpu_gie` 的「与」门，只在用户态才走表，这正是 spec 说的「用户计数器只在 GIE 置位时才递增」。

#### 4.3.4 代码实践

**实践目标**：在源码里看清「中断位 = 加法进位」这一设计。

**操作步骤**：打开 [rtl/peripherals/zipcounter.v:L79-L90](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipcounter.v#L79-L90)，追踪 `i_event` 为真、且 `o_wb_data == 0xFFFFFFFF` 时 `{o_int, o_wb_data}` 的取值。

**预期结果**：`0xFFFFFFFF + 1 = 0x1_00000000`，截到 33 位即 `{o_int=1, o_wb_data=0}`。所以那一拍 `o_int=1`、计数值归零，正好就是「溢出中断」。接着下一拍没有事件时 `o_int <= 0`，中断只持续一拍。**这条结论可直接从代码推得，无需运行。**

#### 4.3.5 小练习与答案

**练习 1（本讲核心问题之一）**：`zipcounter` 与 `ziptimer` 的本质区别是什么？
**答案**：四点——
- **方向**：counter 向上数，timer 向下数。
- **触发源**：counter 数「外部事件」`i_event`（在 ZipSystem 里是时钟使能、停顿、指令数等内部信号，也可以是任意脉冲）；timer 数自己的 `i_ce` 时钟使能。
- **中断条件**：counter 在 32 位回绕（溢出）时中断，周期固定为 \(2^{32}\) 个事件（除非中途重写）；timer 在数到 0 时中断，周期由写入值决定。
- **可控性**：counter 不能停、只能重写；timer 写 0 可停、还有自动重装模式。

因此「让 CPU 每 \(N\) 个时钟收到一次中断」应当用 `ziptimer`（写 \(N\)、开自动重装），而不是 counter。

**练习 2**：为什么形式化属性里断言「`o_int` 不可能连续两拍为 1」？
**答案**：因为 `o_int` 只在回绕那一拍（`i_event` 且全 1）为 1；回绕后值变 0，下一拍就算还有事件也只会得到进位 0。spec 对应的断言见 [rtl/peripherals/zipcounter.v:L236-L239](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipcounter.v#L236-L239)。

---

### 4.4 zipjiffies：一直向上计数、写一个未来值到点中断

#### 4.4.1 概念说明

`zipjiffies` 的灵感来自 Linux 的 `jiffies`：进程可以请求「睡到第 N 个 jiffie」。它的工作方式与 timer/counter 都不同：

- 内部有一个**永不停止、只能复位清零**的向上计数器 `r_counter`，每个 `i_ce` 加 1。
- 读它返回当前 jiffie 值。
- **写它**不是设初值，而是「预约一个中断时间点」：CPU 读出当前值、加上睡眠长度、再写回去，外设就会在计数器走到那个值时拉一次中断。
- 多次写入时，外设会自动选择**最近的那个未来时间点**；写到「过去」的值则立刻中断。

源码注释把这个模型讲得很细，见 [rtl/peripherals/zipjiffies.v:L7-L41](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipjiffies.v#L7-L41)。

#### 4.4.2 核心流程

关键在于用**有符号减法**判断「未来 vs 过去」：

\[
\text{till\_wb} = \text{i\_wb\_data} - r\_\text{counter} - (\text{i\_ce}\,?\,1:0)
\]

- 若 `till_wb > 0`：写入值在未来，登记为下一次中断时间 `int_when`，置 `int_set`。
- 若 `till_wb <= 0`：写入值已到（或已过），立即拉一拍 `o_int`。
- 当计数器走到 `int_when`（即 `r_counter == int_when`）时，拉一拍 `o_int` 并自我清除 `int_set`（中断是自清的，不需要 CPU 应答）。

若有多个未来值被写入，外设比较 `till_when = int_when - i_wb_data` 来决定保留更近的那个（见 [rtl/peripherals/zipjiffies.v:L185-L188](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipjiffies.v#L185-L188)）。注意最大可预约时长约为 \(2^{31}\) 个时钟（有符号正数范围），超过就要分段设闹钟。

#### 4.4.3 源码精读

永不停止的计数器：

```verilog
always @(posedge i_clk)
if (i_reset)        r_counter <= 0;
else if (i_ce)      r_counter <= r_counter+1;
```

见 [rtl/peripherals/zipjiffies.v:L100-L115](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipjiffies.v#L100-L115)。

写入的「未来/过去」判定与延迟一拍的处理：

```verilog
new_set <= (i_wb_stb && i_wb_we);
...
till_wb   <= (i_wb_data - r_counter - (i_ce ? 1:0));
till_when <= (int_when - i_wb_data);
```

见 [rtl/peripherals/zipjiffies.v:L137-L153](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipjiffies.v#L137-L153)。中断的产生与自清：

```verilog
if ((i_ce)&&(int_set)&&(r_counter == int_when))
    o_int <= 1'b1;                 // 到点：响一拍
else if ((new_set)&&(till_wb <= 0))
    o_int <= 1'b1;                 // 写过去值：立刻响
...
if ((new_set)&&(till_wb > 0))  int_set <= 1'b1;
else if (int_now)              int_set <= 1'b0;   // 响过后自清
```

见 [rtl/peripherals/zipjiffies.v:L157-L181](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipjiffies.v#L157-L181)。在 `zipsystem` 里它实例化在 `sys_addr[1:0]==2'b11`（地址 `0xff00001c`），见 [rtl/zipsystem.v:L1400-L1410](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1400-L1410)。

#### 4.4.4 代码实践

**实践目标**：理解 jiffies 的「读—加—写」用法。

**操作步骤**：阅读 spec 的 ZipJiffies 章节 [doc/src/spec.tex:L3088-L3156](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L3088-L3156)。写出「睡 `N` 个时钟」的三步伪代码。

**预期结果**：
```
now  = zip->jiff;        // 读当前 jiffie
zip->jiff = now + N;     // 预约 N 拍后的中断
// 等待中断（OS 通常把 (now+N, 任务) 登记进一个排序表）
```
**待本地验证**：注意 spec 指出 jiffies 没法取消已设的中断，OS 必须自己维护一张「未来的闹钟表」，每次中断后重新写最近的一个。

#### 4.4.5 小练习与答案

**练习 1**：为什么用「有符号减法」就能判断未来/过去，而不是直接比大小？
**答案**：因为计数器会回绕。比如当前值是 `0xFFFFFF F0`、写入 `0x10`，直接无符号比较会觉得 `0x10 < 当前值` 而判为「过去」，但实际上跨过回绕点后它还在未来。有符号减法把「半圈以内」视为正（未来），「超过半圈」视为负（过去），这正是 `2^31` 上限的由来。

**练习 2**：jiffies 中断需要 CPU 应答吗？
**答案**：不需要。`o_int` 只响一拍，且 `int_set` 会在响过那一拍自动清零。但要再次中断，CPU 必须再写一个未来值——这是它和 timer（自动重装）的最大不同。

---

### 4.5 wbwatchdog：总线看门狗与看门狗定时器

#### 4.5.1 概念说明

ZipSystem 里有**两种**「看门狗」，很容易混淆，务必分清：

1. **可写的看门狗定时器（WDT，地址 `0xff000004`）**：其实是一个 `ziptimer` 实例（`RELOADABLE=0`，即一次性、不重装）。它的 `o_int` 被接到 CPU 的复位线上，所以一旦数到 0 就复位 CPU。软件写一个值启动、写 0 停止。
2. **总线看门狗 `wbwatchdog`（地址 `0xff000008`）**：这才是 `wbwatchdog.v` 这个文件。它的超时值**不是软件写的，而是综合时常量输入** `i_timeout`。它专门盯 Wishbone 总线：只要总线有活动（`cyc`/`stb`/`ack` 之一）就复位它；若一笔总线交易卡住超过 `i_timeout` 个时钟，它就拉响，转成一个总线错误（不是 CPU 复位）。

本节聚焦 `wbwatchdog.v` 本身。

#### 4.5.2 核心流程

`wbwatchdog` 极简：

1. 复位时 `r_value` 装入 `i_timeout`，`o_int` 清 0。
2. 每拍：若 `o_int` 还没拉响，`r_value` 减 1（用「加全 1」实现减 1）。
3. 当 `r_value` 减到 1（下一拍到 0）时，`o_int` 置 1。
4. 一旦 `o_int` 置 1，就**锁死**：`r_value` 不再变化，`o_int` 一直为 1，直到下一次复位。

也就是说它是一个「一次性、不可在运行中重启、到点锁死等复位」的看门狗。

#### 4.5.3 源码精读

模块端口：注意没有 Wishbone 接口，只有时钟、复位、常量超时输入和一根中断输出，见 [rtl/peripherals/wbwatchdog.v:L51-L61](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/wbwatchdog.v#L51-L61)。

倒计数与「到点后停止」：

```verilog
always @(posedge i_clk)
if (i_reset)
    r_value <= i_timeout[(BW-1):0];
else if (!o_int)
    r_value <= r_value + {(BW){1'b1}}; // r_value - 1
```

见 [rtl/peripherals/wbwatchdog.v:L66-L73](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/wbwatchdog.v#L66-L73)。`+{(BW){1'b1}}` 等价于 `-1`，是常见的「加全 1 = 减 1」写法。中断锁存：

```verilog
if (i_reset)             o_int <= 1'b0;
else if (!o_int)         o_int <= (r_value == { {(BW-1){1'b0}}, 1'b1 });
```

见 [rtl/peripherals/wbwatchdog.v:L77-L83](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/wbwatchdog.v#L77-L83)。

在 `zipsystem` 里，总线看门狗实例化为 14 位、超时 `0x2000`（8192 个时钟）：

```verilog
wbwatchdog #(14) u_watchbus(
    i_clk,(cpu_reset)||(reset_wdbus_timer),
        14'h2000, wdbus_int
);
```

见 [rtl/zipsystem.v:L984-L990](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L984-L990)。其中 `reset_wdbus_timer = (!o_wb_cyc)||(o_wb_stb)||(i_wb_ack)`（见 [rtl/zipsystem.v:L982](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L982)）——任何总线活动都给它复位，所以只有「总线真的卡死」才会让它数到 0。

对照：软件可写的 WDT 用的是 `ziptimer`，见 [rtl/zipsystem.v:L960-L974](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L960-L974)，其 `o_int` 即 `wdt_reset`，接进 CPU 复位逻辑。

#### 4.5.4 代码实践

**实践目标**：从连接方式看清两种看门狗的不同后果。

**操作步骤**：对比 [rtl/zipsystem.v:L960-L974](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L960-L974)（WDT，输出名 `wdt_reset`）与 [rtl/zipsystem.v:L984-L990](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L984-L990)（总线看门狗，输出名 `wdbus_int`）。在源码里分别追踪这两个信号最终接到哪里。

**预期结果**：`wdt_reset` 汇入 `cpu_reset`，所以「软件看门狗到点 = CPU 复位」；`wdbus_int` 用来终止卡死的总线交易并产生总线错误（CPU 可从 uCC 的总线错位读到，地址被锁存进 `r_wdbus_data`）。**待本地验证**：可在仿真里故意制造一次无应答的总线读，观察是否在约 8192 个时钟后报总线错。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `wbwatchdog` 用「加全 1」而不是直接写 `r_value - 1`？
**答案**：等价。`r_value + {(BW){1'b1}}` 就是 `r_value + 0xFF...F = r_value - 1`。这种写法在 Verilog 里常用于避免显式减法或统一加法器资源，是代码风格选择，不影响功能。

**练习 2**：软件能动态调整总线看门狗的超时吗？
**答案**：不能。`i_timeout` 是综合时常量（`14'h2000`），运行时不可写。要改超时必须改 RTL 重新综合。这正是它和 WDT（软件可写）的关键区别。

---

## 5. 综合实践

**任务**：为 ZipSystem 设计一个「每 1000 个时钟响一次」的周期中断，并说清需要动哪些寄存器、走哪些信号通路。

请按下列步骤完成一份说明文档（可以只写在纸上，不必改源码）：

1. **选外设**：在 timer / counter / jiffies 三者里选一个，并说明为什么是 `ziptimer`（提示：要「每固定时钟数」且「周期」，counter 的周期是 \(2^{32}\) 不可调、jiffies 需要每次重设）。
2. **写定时器**：选用 Timer A（地址 `0xff000010`）。要让它周期响，应写入的 32 位值是多少？写出计算（提示：`0x80000000 | 1000`）。
3. **使能中断**：往主 PIC（地址 `0xff000000`）写什么值才能让 Timer A 的中断真正送进 CPU？参考 4.1.4 的 `EINT(SYSINT_TMA)` 展开（提示：`0x80108000`）。
4. **追踪通路**：对照 [rtl/zipsystem.v:L550-L551](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L550-L551)，指出 `tma_int` 落在 `main_int_vector` 的哪一位，再说明它如何穿过 PIC 成为 `pic_interrupt`。
5. **应答**：在中断处理里，应答 Timer A 应往 PIC 写什么（参考 4.1.5 练习 2）？

**预期结果**：你应能得到一组确定的寄存器写入值，并能在 [rtl/zipsystem.v:L658-L665](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L658-L665) 的 `sel_*` 译码里确认 Timer A 确实被 `sel_timer && sys_addr[1:0]==2'b00` 选中。这就把「外设内部行为」和「系统整合」串了起来。**待本地验证**：在 `sim/verilator` 里把上述写入做成一段汇编，用 `stest` 跑，看是否周期性进入中断处理。

## 6. 本讲小结

- `icontrol` 用**一个 32 位字**完成「合并 + 屏蔽 + 应答」：bit31 总使能、bits16+ 逐路使能、bit15 任意挂起/写方向、bits0..14 挂起状态（写 1 清除）。它把多路电平中断「或」成一根线送 CPU。
- `ziptimer` 是 31 位**向下**计数器，到 0 响一拍中断，bit31 控制自动重装，写 0 停表——这是「每 \(N\) 个时钟周期中断」的标准答案。
- `zipcounter` 是**向上**事件计数器，把「加 1 的进位」直接当溢出中断，数的是任意事件而非时钟，不能停——它与 timer 的本质区别在于方向、触发源、中断条件、可控性。
- `zipjiffies` 是**永不停止的向上时钟计数器**，写一个未来值即可在该时刻响一次（用有符号减法判未来/过去，上限约 \(2^{31}\)），中断自清、无需应答。
- `wbwatchdog` 是**总线看门狗**：超时值综合时常量、到点锁死、由总线活动复位、产生总线错误；它和「软件可写、复位 CPU」的 WDT（实为 `ziptimer`）是两回事。
- 这五个外设在 `zipsystem` 里地址固定（PIC `0xff000000`、TimerA `0xff000010`、Jiffies `0xff00001c`、计数器组 `0xff000020` 起），中断分别汇入 `main_int_vector` 的固定位，再经主 PIC 送 CPU。

## 7. 下一步学习建议

- 想看这些外设如何被「批量」用形式化方法证明正确，继续读 [u5-l2 形式化验证体系](u5-l2-formal-verification.md)：每个外设文件末尾的 `` `ifdef FORMAL `` 段都是现成的 SymbiYosys 证明，`icontrol`/`zipcounter` 的证明规则尤其值得对照本讲读一遍。
- 想了解另一类「主动搬数据」的外设，读 [u4-l6 DMA 控制器](u4-l6-dma-controllers.md)：`wbdmac` 同样挂在这条 `sys` 总线上，它的中断也进 `main_int_vector` 的 bit0。
- 想亲手跑一个真实中断程序，回到 [u1-l4 第一个程序](u1-l4-first-simulation.md) 的基础上，结合本讲的综合实践，在 `sim/zipsw` 里写一个最小定时器中断示例并用 `make stest` 验证。
