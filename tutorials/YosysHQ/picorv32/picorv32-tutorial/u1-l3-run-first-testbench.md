# 跑起来：最小测试台 testbench_ez

## 1. 本讲目标

本讲是整套手册里第一次真正「让 CPU 跑起来」。读完本讲后，你应该能够：

- 用 Icarus Verilog（iverilog / vvp）独立运行 `make test_ez`，看到 PicoRV32 的仿真输出。
- 看懂 `testbench_ez.v` 这个不到 90 行的最小测试台：它是怎么产生时钟、怎么复位、怎么用一段 `reg` 数组冒充「内存」的。
- 理解 PicoRV32 原生内存接口的握手三件套：`mem_valid` / `mem_ready` / `mem_wstrb`，以及 `mem_instr` 如何区分「取指」与「访存」。
- 手动推演测试台里预置的 6 条 RV32I 指令，预测它会打印什么，再和真实输出对比。

本讲故意不碰 RISC-V 工具链、不碰 AXI/Wishbone、不碰中断。我们只盯着一个最小闭环：**时钟 → 复位 → 取指 → 访存 → 打印**。

## 2. 前置知识

在读本讲前，建议你已经具备以下概念（不知道也没关系，下面会用一两句话补）：

- **时钟与边沿触发**：同步数字电路靠一个方波「时钟」节拍前进。PicoRV32 在时钟的**上升沿**（`posedge clk`）更新所有内部寄存器，就像所有人听到鼓点同时迈一步。
- **复位（reset）**：上电时寄存器的值是不确定的，需要一个「复位」动作把它们强制归零、并把程序计数器（PC）指向第一条指令。`resetn` 末尾的 `n` 表示**低有效**——为 0 时处于复位，为 1 时才开始跑。
- **内存映射**：CPU 不直接认识「寄存器堆 / RAM / 外设」，它只会对一个地址空间发起读/写。本讲的测试台用一个数组 `memory[0:255]` 充当这块地址空间，既放指令也放数据。
- **握手（handshake）**：CPU 想读写内存时，先把 `mem_valid` 拉高表示「我这次请求有效」；内存准备好后把 `mem_ready` 拉高表示「成交」。两边同时为高的那一拍，一次传输才算完成。

如果你已经读过本单元前两讲（u1-l1 项目总览、u1-l2 仓库与构建），你会知道：仓库真正的 RTL 只有 `picorv32.v` 一个文件，而 `make test_ez` 是**唯一不依赖 RISC-V 工具链**的入口——它把指令直接硬编码在测试台里。这正是它适合作为「第一跑」的原因。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `testbench_ez.v` | 最小仿真测试台，不到 90 行 | 时钟、复位、内存模型、打印、6 条硬编码指令 |
| `picorv32.v` | CPU 主体 RTL | 复位时 PC 与状态的初始化、原生内存接口握手 |
| `Makefile` | 构建与运行入口 | `test_ez` 目标、`testbench_ez.vvp` 的编译规则 |

关键行号一览（永久链接会在第 4 节给出）：

- `testbench_ez.v`：时钟 `always #5 clk = ~clk`（L15）、复位与结束 `initial` 块（L17–26）、打印逻辑（L36–45）、6 条指令预置（L61–70）、内存模型（L72–85）。
- `picorv32.v`：模块参数（L63–88）、端口 `clk`/`resetn`/`trap`/`mem_*`（L90–100）、`mem_xfer` 握手定义（L373）、内存传输状态机（L565–594）、CPU 状态编码（L1172–1179）、复位初始化 `reg_pc <= PROGADDR_RESET`（L1457–1483）。
- `Makefile`：`COMPRESSED_ISA = C`（L19）、`test_ez` 运行规则（L39–43）、`testbench_ez.vvp` 编译规则（L69–71）。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**时钟与复位**、**内建内存模型与字节写使能**、**取指/访存打印**。

### 4.1 时钟与复位

#### 4.1.1 概念说明

PicoRV32 是一款**同步时序**处理器：它内部所有的寄存器（PC、通用寄存器、状态机等）都只在时钟 `clk` 的上升沿发生跃迁。因此要让它跑起来，测试台必须先提供两样东西：

1. 一个**时钟方波**，作为节拍。
2. 一个**复位序列**：先保持 `resetn = 0` 一段时间让电路稳定归零，再拉高 `resetn = 1` 让 CPU 开始执行第一条指令。

`testbench_ez.v` 用 Verilog 的 `initial` 块和 `always` 块，在纯仿真环境里造出了这两样东西——不需要任何真实硬件。

#### 4.1.2 核心流程

仿真时间轴（`timescale` 为 `1 ns / 1 ps`）如下：

1. **t = 0**：`clk = 1`，`resetn = 0`（仿真开始即处于复位）。
2. **持续节拍**：每 5 ns 翻转一次 `clk`，于是时钟周期为 \( T = 10\,\text{ns} \)（即 100 MHz）。
3. **复位保持**：`repeat (100) @(posedge clk)`——等满 100 个上升沿，期间 `resetn` 一直是 0。
4. **释放复位**：第 100 个上升沿后 `resetn <= 1`，CPU 从 `PROGADDR_RESET`（默认 0）开始取指。
5. **运行窗口**：再 `repeat (1000) @(posedge clk)`，让 CPU 跑 1000 个上升沿。
6. **结束**：`$finish` 停止仿真。

伪代码：

```text
clk = 1; resetn = 0
每 5ns 翻转 clk                       // 产生 10ns 周期方波
循环 100 个上升沿: 什么都不做          // 保持复位
resetn = 1                            // 释放复位，CPU 起跑
循环 1000 个上升沿: CPU 自由执行
$finish
```

#### 4.1.3 源码精读

测试台里时钟与复位的产生都在开头几行：

[testbench_ez.v:11-26](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_ez.v#L11-L26) —— 声明 `clk`/`resetn`/`trap`，用 `always #5 clk = ~clk` 生成时钟，用 `initial` 块控制「复位 100 拍 → 运行 1000 拍 → 结束」。其中 `repeat (100) @(posedge clk); resetn <= 1;` 就是释放复位的时刻。

CPU 一侧，`clk`/`resetn` 是最朴素的两个输入：

[picorv32.v:90-91](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L90-L91) —— `input clk, resetn` 与 `output reg trap`。`trap` 会在 CPU 进入非法/异常状态时拉高（本讲的程序不会触发）。

复位那一拍，CPU 内部做的初始化是本模块最关键的一行：

[picorv32.v:1457-1483](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1457-L1483) —— 当 `!resetn` 时，`reg_pc <= PROGADDR_RESET`（把 PC 指向复位地址，默认 0）、`reg_next_pc <= PROGADDR_RESET`、中断屏蔽 `irq_mask <= ~0`（先屏蔽所有中断）、`cpu_state <= cpu_state_fetch`（状态机进入「取指」态）。这就是为什么复位释放后，CPU 一定会从地址 0 取第一条指令。

> 提示：`PROGADDR_RESET` 的默认值定义在模块参数表里。本讲程序放在 `memory[0]`，对应地址 0，所以默认值正好匹配。

#### 4.1.4 代码实践

**目标**：亲眼看到 `resetn` 从 0 跳到 1 的时刻，建立「复位窗口」的直觉。

**步骤**：

1. 运行带波形的版本：`make test_ez_vcd`（它会给 vvp 传 `+vcd`，生成 `testbench.vcd`）。
2. 用 GTKWave（或任意波形查看器）打开 `testbench.vcd`。
3. 把信号 `testbench.clk` 和 `testbench.resetn` 拖进视图。

**应观察的现象**：`clk` 是整齐的 10ns 方波；`resetn` 在前 100 个上升沿内保持 0，之后稳定为 1。

**预期结果**：`resetn` 的上升沿出现在第 100 个 `clk` 上升沿附近。这个时刻之后，CPU 才开始真正取指。

> 如果本地没有安装 GTKWave，仅运行 `make test_ez` 观察文本输出也可——`resetn` 的行为不影响文本打印，只是让你看不到波形细节。本步骤的波形结论若无法验证，可标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `repeat (100) @(posedge clk)` 改成 `repeat (0)`，会发生什么？

**参考答案**：复位窗口被取消，`resetn` 几乎立刻拉高。理论上 CPU 仍能从地址 0 起跑，但仿真中各 `reg` 的初值依赖 `!resetn` 分支被真正执行过——复位拍数太少可能导致部分寄存器未被初始化（出现 `x`）。这就是为什么测试台故意留 100 拍复位余量。

**练习 2**：`resetn` 为什么是「低有效」而不是「高有效」？

**参考答案**：低有效复位是硬件设计惯例——上电瞬间电源尚未稳定时，信号默认会被弱下拉到 0，恰好对应「处于复位」状态，电路更安全。名字里的 `n`（not）就是为了提醒使用者它是低有效。

### 4.2 内建内存模型与字节写使能

#### 4.2.1 概念说明

CPU 自己并不关心「指令从哪来、数据存到哪」，它只对一组地址发起读/写。`testbench_ez.v` 用一个 Verilog 数组 `reg [31:0] memory [0:255]` 扮演这块地址空间：

- 它既**放指令**（CPU 取指时读它），也**放数据**（程序读写变量时访问它）。这就是「指令与数据共用一条总线」的冯·诺依曼风格。
- 它只有 256 个字（1024 字节），地址范围 `0x000`–`0x3ff`。本讲的程序和数据都落在这个窗口内。

「字节写使能」`mem_wstrb` 是 4 位信号，每一位对应 32 位字里的一个字节：

| `mem_wstrb` | 含义 |
| --- | --- |
| `4'b0000` | 本次是读，不写任何字节 |
| `4'b1111` | 写整字（4 个字节都写）——对应 `sw` |
| `4'b0011` | 只写低 2 字节——对应 `sh` 到低半字 |
| `4'b0001` | 只写最低字节——对应 `sb` |

这种「按字节选择性写入」的设计，让一条 32 位总线也能支持字节/半字存储指令，而不必为每种宽度单独配一组数据线。

#### 4.2.2 核心流程

一次内存访问的握手流程（不考虑提前一拍的 look-ahead 接口，那是后话）：

```text
CPU:  拉高 mem_valid，给出 mem_addr（读：mem_wstrb=0；写：mem_wstrb≠0、mem_wdata=数据）
内存: 下一拍拉高 mem_ready；若是读，同时把 mem_rdata 准备好
双方: mem_valid && mem_ready 同时为高的那一拍 → 一次传输完成
CPU:  撤销 mem_valid（或发起下一次访问）
```

CPU 内部用一个 2 位状态机 `mem_state` 驱动这个过程；测试台那侧则用一个 `always` 块被动响应。

#### 4.2.3 源码精读

先看测试台怎么「造内存」和「预置程序」：

[testbench_ez.v:61-70](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_ez.v#L61-L70) —— 声明 `memory [0:255]`，并在 `initial` 块里把 6 条 RV32I 指令写进 `memory[0..5]`。注释已给出汇编（`li`/`sw`/`lw`/`addi`/`j`），机器码与汇编一一对应。

再看测试台如何响应 CPU 的访问——这是本模块最核心的一段：

[testbench_ez.v:72-85](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_ez.v#L72-L85) —— 每个上升沿先把 `mem_ready <= 0`（默认不就绪）；若看到 `mem_valid && !mem_ready` 且地址 `< 1024`，则下一拍 `mem_ready <= 1` 并：读时回填 `mem_rdata <= memory[mem_addr >> 2]`；写时按 `mem_wstrb` 的 4 位分别选择性地更新对应字节。注释 `/* add memory-mapped IO here */` 提示：地址 ≥ 1024 的区域留给内存映射外设（这正是 u2-l2 里 `0x10000000` UART 的伏笔）。

注意 `mem_addr >> 2`：因为每条字长 4 字节，地址按 4 对齐，右移 2 位得到字索引。地址 1020（`0x3fc`）右移 2 位 = 255，所以本讲程序访问的是 `memory[255]`。

CPU 这侧，握手完成的判定写在一行 `assign` 里：

[picorv32.v:373](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L373) —— `assign mem_xfer = (mem_valid && mem_ready) || ...`。`mem_xfer` 为真就代表「这一拍内存传输完成」，CPU 据此推进状态机。

驱动这套握手的是 CPU 内部的 `mem_state` 状态机：

[picorv32.v:565-594](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L565-L594) —— 复位时 `mem_state <= 0`、`mem_valid <= 0`；之后状态 0 等到 `mem_do_prefetch/mem_do_rinst/mem_do_rdata`（取指/取指令/取数据）或 `mem_do_wdata`（写数据）请求，就拉高 `mem_valid` 并迁到状态 1（读路径）或状态 2（写路径），直到 `mem_xfer` 完成才撤销 `mem_valid`。

`mem_wstrb` 在 CPU 端口里的方向：

[picorv32.v:97-100](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L97-L100) —— `mem_addr`/`mem_wdata`/`mem_wstrb` 都是 CPU **输出**，`mem_rdata` 是 CPU **输入**。也就是说：地址、数据、写使能由 CPU 给出，读回数据由内存给出——和上面测试台的接线完全对得上。

#### 4.2.4 代码实践

**目标**：体会 `mem_wstrb` 的字节选择性。

**步骤**：

1. 打开 `testbench_ez.v`，找到 `memory[1] = 32'h 0000a023; // sw x0,0(x1)`。这是一条整字写（`sw`），运行时 `mem_wstrb` 应为 `4'b1111`。
2. 阅读打印逻辑（下一模块会讲）确认 `write` 行会带 `(wstrb=1111)`。
3. 思考：如果把这条改成字节写 `sb`（机器码不同），按 [testbench_ez.v:78-81](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_ez.v#L78-L81) 的逐字节更新逻辑，`memory[255]` 里只有 1 个字节会被改写，其余 3 字节保持原值。

**应观察的现象**：`sw` 时 `wstrb=1111`，4 字节一起更新；`sb` 时 `wstrb` 只有 1 位为 1。

**预期结果**：本讲程序全部用 `sw`，所以你看到的所有 `write` 行都是 `wstrb=1111`。

> 这是源码阅读型实践：不要求改源码（本讲禁止改源码），只要你能解释「为什么 `sw` 对应 4 位全 1、`sb` 只对应 1 位」即可。

#### 4.2.5 小练习与答案

**练习 1**：程序里 `x1 = 1020 = 0x3fc`，为什么访问的是 `memory[255]` 而不是 `memory[1020]`？

**参考答案**：因为 `memory` 是 32 位字数组，每个元素占 4 字节。字节地址 `0x3fc = 1020` 对应字索引 \( 1020 / 4 = 255 \)，测试台用 `mem_addr >> 2` 完成这个换算。若直接写 `memory[1020]` 会越界（数组只有 256 项）。

**练习 2**：如果某条指令向地址 2000（`0x7d0`）发起写，测试台会发生什么？

**参考答案**：`mem_addr < 1024` 判定为假，内存模型既不拉高 `mem_ready` 也不更新任何字节——这次写会「卡住」等待应答（CPU 一直 `mem_valid` 但永远等不到 `mem_ready`）。注释 `/* add memory-mapped IO here */` 正是预留：真实设计会在这里接外设。

### 4.3 取指/访存打印

#### 4.3.1 概念说明

光让 CPU 跑起来还不够，我们还要「看见」它在干什么。`testbench_ez.v` 用 `$display` 在每次内存传输完成时打印一行，把 CPU 的行为变成肉眼可读的日志。

关键在于 **`mem_instr`** 这一位信号：CPU 取指令时把它拉高（`1`），访问数据时拉低（`0`）。于是同一条总线、同一组 `mem_*` 信号，靠这一位就能区分「这次是取指」还是「这次是读写变量」。测试台据此把输出分成三类：

- `ifetch`：取指令（`mem_instr == 1`）。
- `write`：写数据（`mem_instr == 0` 且 `mem_wstrb != 0`）。
- `read`：读数据（`mem_instr == 0` 且 `mem_wstrb == 0`）。

这种「指令与数据共用总线、靠一位区分」的安排，是理解 PicoRV32 原生内存接口的钥匙，也是后续 AXI/Wishbone 适配（u7-l1）的基础。

#### 4.3.2 核心流程

打印逻辑的判定（每个上升沿评估一次）：

```text
if (mem_valid && mem_ready):           // 这一拍正好有一次传输完成
    if (mem_instr):        打印 "ifetch  <addr>: <rdata>"
    else if (mem_wstrb):   打印 "write   <addr>: <wdata> (wstrb=<wstrb>)"
    else:                  打印 "read    <addr>: <rdata>"
```

因为内存模型在 `mem_valid && !mem_ready` 的下一拍才把 `mem_ready` 拉高，所以「`mem_valid && mem_ready` 同时为高」恰好只持续一拍——每笔传输**恰好打印一次**，不会重复也不会遗漏。

#### 4.3.3 源码精读

打印逻辑就一处：

[testbench_ez.v:36-45](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_ez.v#L36-L45) —— 在 `always @(posedge clk)` 里，条件 `mem_valid && mem_ready` 命中时，按 `mem_instr`/`mem_wstrb` 三分天下，分别 `$display` 出 `ifetch`/`write`/`read`。注意 `write` 行打印的是 `mem_wdata`（写入的数据）和 `mem_wstrb`，`read`/`ifetch` 行打印的是 `mem_rdata`（读回的数据，取指时即指令机器码）。

`mem_instr` 在 CPU 端口里的声明：

[picorv32.v:93-95](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L93-L95) —— `output reg mem_valid, mem_instr` 与 `input mem_ready`。CPU 自己决定 `mem_instr` 的取值：取指/取指令时为 1，数据访存时为 0。在 `mem_state` 状态机里，[picorv32.v:585](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L585) 的 `mem_instr <= mem_do_prefetch || mem_do_rinst;` 正是把它设成「这是一次取指」的地方。

#### 4.3.4 代码实践

**目标**：通过打印日志，辨认一条 `lw`（加载）指令的完整生命周期。

**步骤**：

1. 运行 `make test_ez`，在输出里找到对应 `memory[2]`（`lw x2, 0(x1)`，地址 `0x8`）的行。
2. 你应该能看到一行 `ifetch 0x00000008: 0x0000a103`（取到 `lw` 这条指令）。
3. 紧接着会出现一行 `read 0x000003fc: <某值>`——这就是 `lw` 真正去内存读数据的那次访问（`mem_instr=0, mem_wstrb=0`）。
4. 之后才是下一条指令 `addi`（地址 `0xc`）的 `ifetch`。

**应观察的现象**：一条 `lw` 对应「1 次 `ifetch` + 1 次 `read`」两行；而一条 `sw` 对应「1 次 `ifetch` + 1 次 `write`」两行。纯计算指令（如 `addi`、`li`）只有 `ifetch`、没有数据访问。

**预期结果**：你能清晰看到「取指」与「访存」被 `mem_instr` 区分开。

> 若你的本地输出里 `read`/`ifetch` 的先后顺序与上面略有不同，那是 PicoRV32 预取（prefetch）机制造成的细微交错，不影响数值结论；精确到行的顺序标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ifetch` 行打印的是 `mem_rdata`，而 `write` 行打印的是 `mem_wdata`？

**参考答案**：取指是一次「读」——CPU 从内存读回指令机器码，所以用读数据 `mem_rdata`（它就是指令编码本身，如 `0x0000a103`）。写是一次「写」——CPU 把数据送给内存，所以用写数据 `mem_wdata`。

**练习 2**：如果一个程序里没有任何 `lw`/`sw`，输出里还会出现 `read`/`write` 行吗？

**参考答案**：不会。没有数据访存指令，`mem_instr` 在非取指时不会被触发，也就没有 `read`/`write`。输出将只剩下连续的 `ifetch` 行。本讲的程序刻意用了 `lw`/`sw`，正是为了让你看到这三类输出齐全。

## 5. 综合实践

把三个模块串起来，完成本讲的核心任务：**运行 `make test_ez`，手动推演前几次循环写入的值，预测打印结果并与实际输出对比**。

### 5.1 先读懂这 6 条指令

[testbench_ez.v:63-69](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_ez.v#L63-L69) 预置的程序：

| 字索引 | 地址 | 机器码 | 汇编 | 作用 |
| --- | --- | --- | --- | --- |
| 0 | `0x00` | `0x3fc00093` | `li x1, 1020` | `x1 = 1020 (0x3fc)` |
| 1 | `0x04` | `0x0000a023` | `sw x0, 0(x1)` | 把 `0` 写到地址 `0x3fc`（即 `memory[255]`） |
| 2 | `0x08` | `0x0000a103` | `lw x2, 0(x1)` | `x2 = memory[255]` ← **loop 起点** |
| 3 | `0x0c` | `0x00110113` | `addi x2, x2, 1` | `x2 = x2 + 1` |
| 4 | `0x10` | `0x0020a023` | `sw x2, 0(x1)` | 把 `x2` 写回 `0x3fc` |
| 5 | `0x14` | `0xff5ff06f` | `j <loop>` | 跳回 `0x08` |

这是一个**自增计数器死循环**：先把 `memory[255]` 清零，然后不断「读出 → 加 1 → 写回」。

### 5.2 运行步骤

1. 确认已安装 Icarus Verilog（`iverilog` / `vvp`）。
2. 在仓库根目录执行：

   ```bash
   make test_ez
   ```

   底层等价于 [Makefile:39-43](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L39-L43) 的 `test_ez: testbench_ez.vvp` → `vvp -N testbench_ez.vvp`，而 `testbench_ez.vvp` 由 [Makefile:69-71](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L69-L71) 用 `iverilog` 编译 `testbench_ez.v` 与 `picorv32.v` 得到。

3. 观察终端打印。

> 小知识：[Makefile:19](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L19) 里 `COMPRESSED_ISA = C`，于是连 `test_ez` 也会带 `-DCOMPRESSED_ISA` 编译。但本程序 6 条指令全是 32 位，压缩扩展并不影响行为——这恰好印证 u1-l2 的结论：`COMPRESSED_ISA` 是编译期开关，是否真正用到取决于指令编码。

### 5.3 手动推演（前两次循环）

逐轮追踪 `memory[255]` 的值（地址 `0x3fc`）：

| 阶段 | 动作 | `x2` | `memory[255]` |
| --- | --- | --- | --- |
| 初始 | `sw x0` | — | `0x00000000` |
| 循环 0 读 | `lw x2` → `x2 = 0` | `0` | `0x00000000` |
| 循环 0 加 | `addi` → `x2 = 1` | `1` | `0x00000000` |
| 循环 0 写 | `sw x2` | `1` | `0x00000001` |
| 循环 1 读 | `lw x2` → `x2 = 1` | `1` | `0x00000001` |
| 循环 1 加 | `addi` → `x2 = 2` | `2` | `0x00000001` |
| 循环 1 写 | `sw x2` | `2` | `0x00000002` |
| 循环 2 写 | … | `3` | `0x00000003` |

规律：**`write 0x3fc` 的值依次是 `0, 1, 2, 3, …`；`read 0x3fc`（来自 `lw`）的值依次是 `0, 1, 2, …`（比同一轮的 `write` 落后一拍）**。

### 5.4 预测的打印输出（前两次循环）

依据第 4.3 节的判定规则，前若干行应当形如（地址与数据均为十六进制）：

```text
ifetch 0x00000000: 0x3fc00093          # li x1,1020
ifetch 0x00000004: 0x0000a023          # sw x0,0(x1)
write  0x000003fc: 0x00000000 (wstrb=1111)   # 初始清零
ifetch 0x00000008: 0x0000a103          # lw（循环0）
read   0x000003fc: 0x00000000          # lw 读回 0
ifetch 0x0000000c: 0x00110113          # addi → x2=1
ifetch 0x00000010: 0x0020a023          # sw
write  0x000003fc: 0x00000001 (wstrb=1111)   # 写回 1
ifetch 0x00000014: 0xff5ff06f          # j loop
ifetch 0x00000008: 0x0000a103          # lw（循环1）
read   0x000003fc: 0x00000001          # lw 读回 1
ifetch 0x0000000c: 0x00110113          # addi → x2=2
ifetch 0x00000010: 0x0020a023          # sw
write  0x000003fc: 0x00000002 (wstrb=1111)   # 写回 2
ifetch 0x00000014: 0xff5ff06f          # j loop
...
```

### 5.5 对比与结论

- **数值规律（确定性）**：`write 0x3fc` 单调递增 `0,1,2,3,…`，`read 0x3fc` 同样递增但滞后一轮。这一结论完全由 Verilog 决定，不依赖运行环境，应与你的实际输出一致。
- **行序细节**：受 PicoRV32 预取机制影响，个别 `ifetch`/`read` 的先后可能微调，但每条指令「先取指、后访存」的总顺序不变。若你的输出行序与上文有出入，属正常现象（精确行序待本地验证）。
- **最终值**：测试台只让 CPU 跑 1000 个上升沿，这个死循环最终停在某个计数值（大约几十次迭代）。**精确的最终计数值待本地验证**——取决于 PicoRV32 在这套慢速内存下的实际 CPI。

> 如果你愿意深入：可以把 `Makefile` 里 `test_ez` 的运行拍数（`testbench_ez.v` 中的 `repeat (1000)`）临时调大，观察计数是否继续递增，从而验证「这是个无限循环、只受仿真时长约束」。

## 6. 本讲小结

- `make test_ez` 是**唯一不依赖 RISC-V 工具链**的入口：指令直接硬编码在 `testbench_ez.v` 的 `memory[]` 里，只需 Icarus Verilog 即可运行。
- 测试台用 `always #5 clk = ~clk` 造 10 ns 周期（100 MHz）时钟，用 `initial` 块完成「复位 100 拍 → 运行 1000 拍 → `$finish`」。
- 复位那一拍 CPU 把 `reg_pc <= PROGADDR_RESET`（默认 0）、`cpu_state <= cpu_state_fetch`，所以一定从地址 0 取第一条指令。
- 原生内存接口的握手核心是 `mem_valid && mem_ready`（[picorv32.v:373](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L373)）；`mem_wstrb` 4 位字节写使能让一条 32 位总线支持字节/半字/字写入。
- `mem_instr` 这一位区分「取指」与「访存」，使指令和数据共用同一组 `mem_*` 信号——这是冯·诺依曼式总线的关键，也是后续 AXI/Wishbone 适配的基础。
- 预置的 6 条指令构成一个自增计数器死循环：`write 0x3fc` 的值依次为 `0,1,2,3,…`，规律确定、可手动预测。

## 7. 下一步学习建议

你已经让 PicoRV32 在最小闭环里跑了起来，并理解了时钟、复位、内存模型与握手打印。接下来可以沿两条路推进：

1. **工具链与真实固件（u2-l1、u2-l2）**：本讲的程序是手写机器码。下一单元会教你安装 RV32 工具链，把 C/汇编编译成 `.elf`/`.bin`/`.hex`，再让 `make test`（基于 AXI 的完整测试台）跑起一个真正的 Hello World，通过 `0x10000000` 内存映射 UART 输出。
2. **CPU 外观（u3-l1、u3-l2）**：本讲只接了 `clk`/`resetn`/`trap`/`mem_*` 这几根线。后续讲义会系统讲解 `picorv32` 的全部 `parameter`（`ENABLE_*`、`TWO_*`、`COMPRESSED_ISA`、`PROGADDR_*` 等）和完整的端口分组（look-ahead、PCPI、IRQ、trace），帮你从「能跑」走向「能配置、能集成」。

建议先做 u2-l1，亲手把一段 C 变成 `.hex`，体会「本讲的 `memory[]` 其实就是 `.hex` 的归宿」。
