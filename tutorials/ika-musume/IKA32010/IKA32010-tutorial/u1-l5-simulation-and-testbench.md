# 仿真与 testbench 入门

## 1. 本讲目标

本讲是入门层（u1）的最后一讲。前面四讲我们已经认识了 IKA32010 的项目定位、目录结构、端口引脚和时钟分频，但这些都还停留在「读源码」层面。要把一个硬件软核真正跑起来、亲眼看到它执行指令，唯一的办法就是**写一个 testbench（测试台）做仿真**。

学完本讲，你应当能够：

- 看懂 `src/IKA32010_tb.v` 里**时钟是如何产生的**，以及复位 / 中断激励的时间顺序。
- 理解 `$readmemh` 是如何把一段程序「烧」进 ROM 模型的。
- 掌握 IKA32010 用 `o_MEN_n` / `o_DEN_n` 两个选通信号、在同一条数据总线上**复用**「指令读」和「数据读」两种事务的机制。
- 自己动手搭一个能喂入 NOP / LACK 指令、并通过反汇编日志观察 PC 推进与累加器变化的最小仿真环境。

---

## 2. 前置知识

在进入源码前，先建立几个 Verilog 仿真的基本概念。如果你已经熟悉，可以跳到第 3 节。

- **testbench**：一段不对应真实硬件、只为仿真而写的 Verilog 代码。它的职责是给被测模块（DUT，Design Under Test）提供激励（时钟、复位、输入信号），并观察输出。本讲的 DUT 就是顶层模块 `IKA32010`。
- **`timescale`**：仿真时间单位。本讲会看到 `` `timescale 10ps/10ps ``，意思是「1 个时间单位 = 10 皮秒」。所有 `#N` 延迟都以它为基准。
- **`$readmemh`**：Verilog 系统任务，把一个**十六进制文本文件**读进一个 memory 数组。这是把编译好的机器码「装载」进仿真模型的常用手段，相当于上电时把固件烧进 ROM。
- **三态 / 高阻（`Z`）**：一根线可以被多个驱动源驱动，只要**任意时刻只有一个源在真正驱动**，其余源输出 `Z`（高阻，相当于「断开」），总线就能正常工作。本讲的「总线复用」正是建立在这个原理上。
- **`$display`**：往仿真控制台打印一行文本。IKA32010 的反汇编功能（`IKA32010_DISASSEMBLY` 宏）就是用它把每条执行的指令打印成可读的助记符。

如果你对 `cyclecntr`（周期计数器）、`o_MEN_n` / `o_DEN_n`（总线选通）这些信号还陌生，建议先复习 u1-l3（端口）和 u1-l4（时钟）。

---

## 3. 本讲源码地图

本讲只围绕两个源码文件：

| 文件 | 作用 |
| --- | --- |
| [src/IKA32010_tb.v](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v) | 唯一的 testbench。负责产生时钟、施加复位 / 中断、实例化 `IKA32010`、并用两组 ROM/RAM 模型驱动外部数据总线。 |
| [src/IKA32010.sv](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv) | 被测模块（DUT）。本讲只关注它与 testbench 直接打交道的部分：`cyclecntr` 取指节拍、`o_MEN_n`/`o_DEN_n`/`o_WE_n` 的选通时序、以及 `i_DIN` 上指令锁存的时机。 |

本讲覆盖两个最小模块：**① testbench 的时钟产生与激励时序**，**② ROM 模型与外部总线复用**。

---

## 4. 核心概念与源码讲解

### 4.1 testbench 的时钟产生与复位 / 中断激励

#### 4.1.1 概念说明

任何一个同步硬件都需要时钟才能动起来。IKA32010 没有内置振荡器，它的主时钟 `i_EMUCLK` 和时钟使能 `i_CLKIN_PCEN` 都必须由外部（在我们的场景里就是 testbench）提供。一个 testbench 的「激励骨架」通常由三部分组成：

1. **自由运行的时钟**——用 `always` 配合 `#延迟` 让一根线不停翻转。
2. **复位 / 中断等控制信号的时序**——用 `initial` 块按时间线拉低、拉高相应引脚。
3. **DUT 实例化**——把上面这些信号连到模块端口上。

`IKA32010_tb.v` 正是按这个骨架组织的，麻雀虽小五脏俱全。

#### 4.1.2 核心流程

testbench 的时钟链路可以这样描述（伪代码）：

```text
EMUCLK  : 每 1 个时间单位翻转一次  →  周期 = 2 个时间单位
divider : 每个 EMUCLK 上升沿 +1，在 0→1→2→3 之间循环
i_CLKIN_PCEN = (divider == 3)      →  每 4 个 EMUCLK 才拉高 1 次（1/4 占空比窄脉冲）
```

回忆 u1-l4 的结论：IKA32010 内部的 `cyclecntr` 只在 `i_CLKIN_PCEN` 为高的 `i_EMUCLK` 上升沿才前进。而 testbench 把 `i_CLKIN_PCEN` 做成了「每 4 个 EMUCLK 才出现一次」的窄脉冲，于是在仿真里：

- `cyclecntr` 前进一次需要 4 个 `EMUCLK`；
- `cyclecntr` 走完一圈（0→1→2→3，即一个 DSP 机器周期）需要 \(4 \times 4 = 16\) 个 `EMUCLK`。

这就是 u1-l4 里提到的「仿真中一个机器周期被拉长到 16 个 `EMUCLK`」的由来。机器周期 \(T_{mach}\) 与仿真器时钟周期 \(T_{EMUCLK}\) 的关系为：

\[
T_{mach} = 16 \cdot T_{EMUCLK}
\]

复位 / 中断激励则是一条简单的时间线（注意 `initial` 块里的 `#N` 是**相对上一条语句**的延迟）：

```text
t=30   : RS_n ← 0   （第一次复位，拉低）
t=130  : RS_n ← 1   （释放复位）
t=330  : RS_n ← 0   （第二次复位）
t=450  : RS_n ← 1   （再次释放）
t=2870 : INT_n ← 0  （施加一次中断，下降沿）
t=2933 : INT_n ← 1  （撤销中断）
```

两次复位 + 一次中断，覆盖了 DUT 的三条主要控制通路。

#### 4.1.3 源码精读

先看 testbench 的仿真时间标尺和时钟产生：

```verilog
`timescale 10ps/10ps          // 1 时间单位 = 10ps
module IKA32010_tb;

reg             EMUCLK = 1'b1;
...
always #1 EMUCLK = ~EMUCLK;   // 每 1 单位翻转 → EMUCLK 周期 = 2 单位
```

[src/IKA32010_tb.v:1-8](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L1-L8) 定义了仿真时间标尺和自由时钟。`` `timescale 10ps/10ps `` 让 `#1` 等于 10 皮秒，因此 `EMUCLK` 半周期为 10ps、全周期为 20ps——这只是为了让仿真跑得快，并不代表真实频率。

接着是分频器与 `i_CLKIN_PCEN` 的产生：

```verilog
reg     [1:0]   divider = 2'd0;
always @(posedge EMUCLK) divider <= divider + 2'd1;
wire            cen_n = ~(divider == 2'd3);
wire            refclk = ~divider[1];
```

[src/IKA32010_tb.v:10-13](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L10-L13) 用一个 2 位计数器 `divider` 把 `EMUCLK` 四分频。`cen_n` 只在 `divider==3` 时为低，于是它在 4 拍里只拉低 1 拍。注意 `refclk` 虽然被算出来了，但在本 testbench 里**没有接到 DUT**，只是留作参考时钟。

复位 / 中断激励写在 `initial` 块里：

```verilog
initial begin
    #30  RS_n <= 1'b0;
    #100 RS_n <= 1'b1;
    #200 RS_n <= 1'b0;
    #120 RS_n <= 1'b1;

    #2420 INT_n <= 1'b0;
    #63  INT_n <= 1'b1;
end
```

[src/IKA32010_tb.v:15-24](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L15-L24) 给出了两段复位脉冲和一段中断脉冲。`RS_n` 低有效，`INT_n` 下降沿触发中断。

最后看 DUT 怎么被「挂」上去：

```verilog
IKA32010 main (
    .i_EMUCLK       (EMUCLK   ),
    .i_CLKIN_PCEN   (~cen_n   ),   // = (divider==3)，1/4 占空比脉冲
    ...
    .i_RS_n         (RS_n     ),
    .o_MEN_n        (MEN_n    ),
    .o_DEN_n        (DEN_n    ),
    ...
    .i_DIN          (RDBUS    ),
    .o_DOUT         (WRBUS    ),
    .i_BIO_n        (1'b1     ),   // BIO 恒拉高（不触发 BIOZ）
    .i_INT_n        (INT_n    )
);
```

[src/IKA32010_tb.v:30-51](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L30-L51) 把激励和总线信号连到 DUT。注意三个细节：`i_CLKIN_PCEN` 接的是 `~cen_n`（前述窄脉冲）；`i_BIO_n` 直接常接 `1'b1`，所以本 testbench 不测 `BIOZ` 指令；`o_CLKOUT` 等输出端口悬空（留空），因为本 testbench 不观察它们。

> 对照 DUT 内部：`cyclecntr` 只在 `i_CLKIN_PCEN` 为高时前进，见 [src/IKA32010.sv:49-53](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L49-L53)；由此衍生的 `o_CLKOUT` / `o_CLKOUT_PCEN` / `o_CLKOUT_NCEN` 见 [src/IKA32010.sv:56-58](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L56-L58)。这正好印证了 testbench 的时钟链路。

#### 4.1.4 代码实践

**实践目标**：在纸面上推算 testbench 的时钟节拍，验证你对「16 个 `EMUCLK` = 1 个机器周期」的理解。

**操作步骤**：

1. 打开 [src/IKA32010_tb.v:4-13](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L4-L13)。
2. 列一张表：从 `t=0` 开始，逐个 `EMUCLK` 上升沿记录 `divider`、`cen_n`、`i_CLKIN_PCEN` 的值。
3. 标出 `i_CLKIN_PCEN` 第 1、2、3、4 次为高的时刻，它们对应 DUT 内部 `cyclecntr` 从 0 推进到 3 的四个节拍。
4. 计算这四拍总共消耗多少个 `EMUCLK` 周期。

**需要观察的现象**：`i_CLKIN_PCEN` 每 4 个 `EMUCLK` 出现一次高电平；`cyclecntr` 完成一圈正好需要 16 个 `EMUCLK`。

**预期结果**：一个机器周期 = 16 个 `EMUCLK`（与 u1-l4 结论一致）。仿真时间标尺下，\(T_{mach} = 16 \times 20\text{ps} = 320\text{ps}\)。

> 说明：本环境未安装 Verilog 仿真器，以上为源码静态推算，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `always #1 EMUCLK = ~EMUCLK;` 改成 `always #2 EMUCLK = ~EMUCLK;`，`cyclecntr` 走完一圈需要的「仿真绝对时间」会变吗？DUT 的逻辑行为会变吗？

**答案**：DUT 逻辑行为**不变**，因为 `cyclecntr` 推进只依赖 `i_CLKIN_PCEN` 的相对节拍。但仿真绝对时间会翻倍——`EMUCLK` 周期从 20ps 变 40ps，于是 \(T_{mach}\) 从 320ps 变 640ps。

**练习 2**：testbench 里施加了两次复位脉冲。第二次复位（`t=330` 拉低）释放后，DUT 内部 `if_opcodereg`（指令寄存器）会被复位成什么值？

**答案**：会被复位成 `16'h7F80`，即 `NOP` 的操作码。见 [src/IKA32010.sv:180](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L180)。这意味着复位后核心执行的第一条「指令」是 NOP，直到取到真正的程序为止。

---

### 4.2 程序 ROM 模型与 `$readmemh` 加载

#### 4.2.1 概念说明

IKA32010 没有片内程序 ROM——它的指令必须从外部总线（`i_DIN`）读入（见 u1-l3）。因此 testbench 必须在 DUT 外面**模拟一段程序存储器**：当 DUT 拉低 `o_MEN_n`（指令读选通）并在 `o_AOUT` 上给出地址时，testbench 要在该地址上「摆」好对应的 16 位指令字，让 DUT 在取指节拍把它读走。

`IKA32010_tb.v` 里实际上摆了**两组**存储模型：

- **DSP 程序 ROM**：用 `dsp_hi` / `dsp_lo` 两个 8 位数组拼成 16 位指令字，提供指令读。
- **68000 侧 RAM**（`m68kram`）：模拟街机主板另一颗 CPU 的共享 RAM，提供 IN 指令的数据读。

这是因为这个 testbench 原本是为街机游戏（Twin Cobra 等，见 README）整板仿真写的——DSP 通过 IN/OUT 与 68000 共享内存通信。本讲我们重点关注**程序 ROM**这一组。

#### 4.2.2 核心流程

程序 ROM 的装载与读出流程：

```text
① 上电(initial)：
   $readmemh(".../dsp_hi.txt", dsp_hi)   // 把十六进制文本读进 8 位数组
   $readmemh(".../dsp_lo.txt", dsp_lo)

② 取指时(DUT 把 MEN_n 拉低、地址给到 ADDR)：
   RDBUS = { dsp_hi[ADDR], dsp_lo[ADDR] }   // 拼成 16 位指令字送上 i_DIN
```

关键点：指令字被拆成**高字节 / 低字节**两个文件存放。`dsp_hi[ADDR]` 是高 8 位，`dsp_lo[ADDR]` 是低 8 位，拼起来 `{dsp_hi, dsp_lo}` 才是完整的 16 位指令。这种「拆字节」写法常见于把通用 `.bin`/`.hex` 镜像适配到 16 位总线的场景。

> 注意：testbench 里 `$readmemh` 的路径是硬编码的 Windows 绝对路径（`D:/PROCESSOR/...`），而且仓库里**并不包含** `rom/` 目录——也就是说这份 tb 无法在本地原样跑通。这一点我们在第 5 节的综合实践里会专门解决。

#### 4.2.3 源码精读

DSP 程序 ROM 模型：

```verilog
reg     [7:0]   dsp_hi[0:2047];
reg     [7:0]   dsp_lo[0:2047];
assign  RDBUS = (MEN_n) ? 16'hZZZZ : {dsp_hi[ADDR], dsp_lo[ADDR]};
initial begin
    $readmemh("D:/PROCESSOR/IKA32010/IKA32010/rom/dsp_hi.txt", dsp_hi);
    $readmemh("D:/PROCESSOR/IKA32010/IKA32010/rom/dsp_lo.txt", dsp_lo);
end
```

[src/IKA32010_tb.v:78-84](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L78-L84) 定义了指令 ROM。注意 `assign` 的三态写法：当 `MEN_n` 为高（DUT 不在读指令），`RDBUS` 输出 `16'hZZZZ`（高阻，不驱动总线）；只有 `MEN_n` 为低时才把指令字驱动到 `RDBUS` 上。

`$readmemh` 在 `initial` 里执行一次，把两个文本文件的内容填进数组。被读文件应当是每行一个 2 位十六进制数（对应一个字节）。

对照 DUT 一侧的取指时序：

```verilog
if(cyclecntr == 2'd3) begin
    ...
    if(busctrl_mode[2:0] == 3'd1) if_opcodereg <= i_DIN;   // 指令读：锁存 i_DIN
end
```

[src/IKA32010.sv:182-188](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L182-L188) 表明：DUT 在 `cyclecntr==3` 这一拍，若当前是「指令读」事务（`busctrl_mode[2:0]==1`），就把 `i_DIN`（即 testbench 驱动到 `RDBUS` 上的指令字）锁存进指令寄存器 `if_opcodereg`。这就和上面 testbench 的 `assign` 形成闭环：**tb 在 MEN_n 低电平期间把指令摆到总线上，DUT 在 cyclecntr==3 把它读走**。

> 选通信号本身的时序（`MEN_n` 在 `cyclecntr` 0/1/2 拉低、3 拉高）见 [src/IKA32010.sv:201-208](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L201-L208)，详细的逐相位分析放到 u2-l3（总线控制器）讲。

#### 4.2.4 代码实践

**实践目标**：摆脱对绝对路径 ROM 文件的依赖，改用「直接初始化数组」的方式准备一段最小程序。

**操作步骤**：

1. 复制 [src/IKA32010_tb.v](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v) 到一个新文件（例如 `mini_tb.v`）。
2. 删掉两个 `$readmemh` 调用，改为直接给 `dsp_hi` / `dsp_lo` 赋初值。例如想往地址 0、1、2 分别放 `0x7F80`（NOP）、`0x7E05`（LACK 0x05）、`0x7E0A`（LACK 0x0A）：

```verilog
// 示例代码：直接初始化指令 ROM，替代 $readmemh
initial begin
    // 地址 0 : NOP        = 0x7F80
    dsp_hi[0] = 8'h7F; dsp_lo[0] = 8'h80;
    // 地址 1 : LACK 0x05  = 0x7E05
    dsp_hi[1] = 8'h7E; dsp_lo[1] = 8'h05;
    // 地址 2 : LACK 0x0A  = 0x7E0A
    dsp_hi[2] = 8'h7E; dsp_lo[2] = 8'h0A;
end
```

3. 也可以用一个 16 位数组再拆字节，写起来更直观：

```verilog
// 示例代码：用 16 位数组描述程序，再拆成 hi/lo
reg [15:0] prog [0:2047];
integer k;
initial begin
    prog[0] = 16'h7F80; prog[1] = 16'h7E05; prog[2] = 16'h7E0A;
    for(k=3; k<2048; k=k+1) prog[k] = 16'h7F80; // 其余填 NOP
    for(k=0; k<2048; k=k+1) begin
        dsp_hi[k] = prog[k][15:8];
        dsp_lo[k] = prog[k][7:0];
    end
end
```

**需要观察的现象**：编译时不再因找不到 `rom/*.txt` 而报错；仿真开始后 DUT 能从地址 0 开始取到 NOP、LACK 指令。

**预期结果**：因环境无仿真器，**待本地验证**——但只要字节顺序（高字节进 `dsp_hi`、低字节进 `dsp_lo`）正确，取指应当正常。

#### 4.2.5 小练习与答案

**练习 1**：为什么程序被拆成 `dsp_hi` / `dsp_lo` 两个文件，而不是直接一个 16 位的 `.hex`？

**答案**：这是一种把「面向字节的通用 ROM 镜像」适配到 16 位数据总线的手法。很多汇编器/提取工具按字节输出，用两个字节文件分别存高、低字节，可以在不改动提取流程的前提下拼出 16 位字；同时也方便单独替换某一侧。

**练习 2**：`$readmemh` 读入的文本文件里，每一行应当写几位十六进制？

**答案**：因为 `dsp_hi` / `dsp_lo` 都是 `reg [7:0]`（8 位），所以每行写 **2 位**十六进制（一个字节），例如 `7F`、`80`。如果写 4 位会与数组位宽不匹配。

---

### 4.3 外部总线复用：用 `o_MEN_n` / `o_DEN_n` 选择读数据源

#### 4.3.1 概念说明

IKA32010 对外只有**一条**数据输入总线 `i_DIN`（testbench 里叫 `RDBUS`），但指令读（取指）、表读（TBLR）、IN 指令读都要从这条总线拿数据，而这些数据来自**不同的存储模型**。怎么让同一条总线在不同时刻接通不同的数据源？

答案是**总线复用（multiplexing）**：让所有数据源都挂到 `RDBUS` 上，但用两个互斥的选通信号 `o_MEN_n`（指令读）和 `o_DEN_n`（数据/IN 读）来决定「现在轮到谁驱动」。未被选中的源输出高阻 `Z`（让出总线），被选中的源输出真实数据。这本质上是把一组三态驱动器当成一个多路选择器（MUX）来用。

#### 4.3.2 核心流程

testbench 里 `RDBUS` 被**两条** `assign` 同时驱动，但它们靠「互斥 + 高阻」避免冲突：

```text
assign RDBUS = (DEN_n) ? 16'hZZZZ : m68kram[addrlatch[12:0]];   // 数据源 A：68000 侧 RAM
assign RDBUS = (MEN_n) ? 16'hZZZZ : {dsp_hi[ADDR], dsp_lo[ADDR]}; // 数据源 B：DSP 指令 ROM
```

| 状态 | MEN_n | DEN_n | RDBUS 上是谁在驱动 |
| --- | --- | --- | --- |
| 指令读 | 0（低） | 1（高） | 数据源 B（指令 ROM），A 输出 Z |
| 数据读（IN/TBLR） | 1（高） | 0（低） | 数据源 A（68000 RAM），B 输出 Z |
| 空闲 / 输出 | 1（高） | 1（高） | 两者都输出 Z，总线悬空 |

DUT 的总线控制器保证 `MEN_n` 与 `DEN_n` **永远不会同时为低**（在 [src/IKA32010.sv:191-253](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L191-L253) 的每个 `busctrl_mode` 分支里，至多只有一个为低），所以绝不会出现「两个源抢着驱动」的冲突。这就是三态总线复用能可靠工作的前提。

#### 4.3.3 源码精读

数据源 A（68000 侧 RAM）：

```verilog
assign  RDBUS = (DEN_n) ? 16'hZZZZ : m68kram[addrlatch[12:0]];
```

[src/IKA32010_tb.v:72](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L72) 在 `DEN_n` 为低时把 `m68kram` 的内容驱动到总线。地址来自 `addrlatch`，它由 DSP 的 OUT 指令更新（见下文）。

数据源 B（DSP 指令 ROM）：

```verilog
assign  RDBUS = (MEN_n) ? 16'hZZZZ : {dsp_hi[ADDR], dsp_lo[ADDR]};
```

[src/IKA32010_tb.v:80](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L80) 在 `MEN_n` 为低时把指令字驱动到总线。地址直接用 DUT 给出的 `ADDR`（即 `o_AOUT`）。

这两条 `assign` 共同驱动同一个 `wire RDBUS`，靠三态 `Z` 实现分时复用——这正是「总线复用」的精妙之处。

再看 `addrlatch` 这个细节，它体现了 DSP 与 68000 RAM 的交互方式：

```verilog
reg     [15:0]  addrlatch;
always @(posedge EMUCLK) if(!WE_n && ADDR[2:0] == 3'd0) addrlatch <= WRBUS;
```

[src/IKA32010_tb.v:55-56](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L55-L56) 在 `WE_n` 为低（DUT 在写，即 OUT 指令）且 `ADDR[2:0]==0`（外设口 PA0）时，把 DUT 输出的数据 `WRBUS`（`o_DOUT`）锁进 `addrlatch`。换言之，DSP 用一条 `OUT PA0, ...` 把「接下来要读的地址」告诉 68000 侧 RAM，后续的 IN 读就用这个地址取数据。这是整板仿真里的握手约定，初学者只需理解：**OUT 写出去的数据可以反过来用作下次 IN 读的地址**。

最后回到 DUT 一侧，确认选通信号互斥：

```verilog
// 指令读 (busctrl_mode[2:0]==1)：MEN_n 低，DEN_n 始终高
2'd0: begin o_MEN_n <= 1'b0; o_DEN_n <= 1'b1; ... end
...
// IN 指令 (busctrl_mode[2:0]==4)：DEN_n 低，MEN_n 始终高
2'd0: begin o_MEN_n <= 1'b1; o_DEN_n <= 1'b0; ... end
```

[src/IKA32010.sv:201-241](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L201-L241) 的「指令读」分支里 `DEN_n` 恒为 1，「IN」分支里 `MEN_n` 恒为 1——这从源头保证了两个数据源不会同时驱动总线。

#### 4.3.4 代码实践

**实践目标**：用源码静态分析，验证「同一条 `RDBUS` 不会出现驱动冲突」。

**操作步骤**：

1. 打开 [src/IKA32010.sv:191-253](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L191-L253)，对每个 `busctrl_mode[2:0]` 取值（0=空闲、1=指令读、2=表读、3=表写、4=IN、5=OUT）逐相位（cyclecntr 0/1/2/3）记录 `MEN_n`、`DEN_n` 的电平。
2. 逐一确认：是否存在任意一个相位里 `MEN_n==0` **且** `DEN_n==0`？
3. 打开 [src/IKA32010_tb.v:72](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L72) 和 [src/IKA32010_tb.v:80](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L80)，对照确认：两个 `assign` 的条件 `(DEN_n)` 和 `(MEN_n)` 是否恰好对应 DUT 的两种事务。

**需要观察的现象**：在所有相位里，`MEN_n` 与 `DEN_n` 不同时为 0；两条 `assign` 的有效区间互不重叠。

**预期结果**：没有任何相位产生总线冲突。这是三态总线复用正确性的根本保证。**待本地验证**（可用波形查看器在仿真里核对）。

#### 4.3.5 小练习与答案

**练习 1**：如果某个相位里 `MEN_n` 和 `DEN_n` 同时为 0，`RDBUS` 上会出现什么？为什么 IKA32010 要刻意避免它？

**答案**：两个 `assign` 会同时驱动真实数据，结果取决于两个驱动值的逐位冲突——相同位相同得该值，不同位得 `X`（不确定）。IKA32010 在总线控制器里让两者互斥，正是为了杜绝这种不确定，保证取指 / 取数据都不会读到垃圾值。

**练习 2**：表写（TBLW，`busctrl_mode==3`）和 OUT（`busctrl_mode==5`）会让 `RDBUS` 上出现数据吗？

**答案**：不会。这两种事务是 DUT 向外**写**数据（`o_DOUT`/`o_WE_n`），此时 `MEN_n` 与 `DEN_n` 都保持高（见 [src/IKA32010.sv:222-252](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L222-L252)），所以两条 `assign` 都输出 `Z`，`RDBUS` 悬空——写事务不读总线，逻辑自洽。

---

## 5. 综合实践

把本讲三个模块串起来，亲手搭一个「能跑 NOP / LACK、并打印反汇编日志」的最小 testbench。原版 `IKA32010_tb.v` 依赖 `rom/*.txt` 和绝对路径，本地跑不通；我们绕开它，用直接初始化的方式准备程序。

### 5.1 实践目标

实例化 `IKA32010`，喂入 `NOP → LACK 0x05 → LACK 0x0A → NOP` 四条指令，借助 DUT 自带的反汇编功能（`IKA32010_DISASSEMBLY` 宏）在仿真控制台观察：

- PC 的推进顺序（`0 → 1 → 2 → 3`）；
- 每条指令对应的助记符打印；
- （若开启波形）累加器 ACC 在两条 LACK 后分别变成 `0x0005`、`0x000A`。

### 5.2 操作步骤

1. **准备文件**：在工作目录放好 `IKA32010.sv`、`IKA32010_disasm.sv`、`IKA32010_mnemonics.sv`（DUT 通过 `` `include `` 引用后两者，需保证它们在同一编译目录或在 include 路径里）。
2. **新建 `mini_tb.v`**（示例代码，非项目原有文件）：

```verilog
// 示例代码：最小 testbench，喂入 NOP / LACK 并观察反汇编
`timescale 1ns/1ps
module mini_tb;

    reg             EMUCLK = 1'b1;
    reg             RS_n   = 1'b1;

    always #5 EMUCLK = ~EMUCLK;          // EMUCLK 周期 10ns

    reg     [1:0]   divider = 2'd0;
    always @(posedge EMUCLK) divider <= divider + 2'd1;
    wire            cen_n = ~(divider == 2'd3);

    initial begin
        #20  RS_n <= 1'b0;               // 复位
        #80  RS_n <= 1'b1;               // 释放
    end

    wire            MEN_n, DEN_n, WE_n;
    wire    [11:0]  ADDR;
    wire    [15:0]  RDBUS, WRBUS;

    IKA32010 main (
        .i_EMUCLK       (EMUCLK   ),
        .i_CLKIN_PCEN   (~cen_n   ),
        .o_CLKOUT       (         ),
        .o_CLKOUT_PCEN  (         ),
        .o_CLKOUT_NCEN  (         ),
        .i_RS_n         (RS_n     ),
        .o_MEN_n        (MEN_n    ),
        .o_DEN_n        (DEN_n    ),
        .o_WE_n         (WE_n     ),
        .o_AOUT         (ADDR     ),
        .i_DIN          (RDBUS    ),
        .o_DOUT         (WRBUS    ),
        .o_DOUT_OE      (         ),
        .i_BIO_n        (1'b1     ),
        .i_INT_n        (1'b1     )
    );

    // —— 直接初始化的指令 ROM（替代 $readmemh）——
    reg     [7:0]   dsp_hi[0:2047];
    reg     [7:0]   dsp_lo[0:2047];
    integer i;
    initial begin
        for(i=0; i<2048; i=i+1) begin      // 全部先填 NOP
            dsp_hi[i] = 8'h7F;
            dsp_lo[i] = 8'h80;
        end
        // 地址1 : LACK 0x05 = 0x7E05
        dsp_hi[1] = 8'h7E; dsp_lo[1] = 8'h05;
        // 地址2 : LACK 0x0A = 0x7E0A
        dsp_hi[2] = 8'h7E; dsp_lo[2] = 8'h0A;
        // 地址0,3 保持 NOP(0x7F80)
    end

    // —— 数据读源：本练习不读数据，DEN_n 低时给 0 ——
    assign RDBUS = (MEN_n) ? ((DEN_n) ? 16'hZZZZ : 16'h0000)
                           : {dsp_hi[ADDR], dsp_lo[ADDR]};

    initial begin
        #5000 $finish;                     // 跑一段时间后结束
    end
endmodule
```

3. **编译运行**（以 iverilog 为例，命令供参考）：

```bash
iverilog -g2012 -I. -o sim.vvp mini_tb.v IKA32010.sv
vvp sim.vvp
```

> 说明：`IKA32010.sv` 使用了 SystemVerilog 风格（`logic`/`always @(*)` 等）和 `` `include ``，需用支持 SV 的仿真器并打开相应选项（iverilog 用 `-g2012`）。

### 5.3 需要观察的现象

控制台应出现形如下面的反汇编日志（因 `IKA32010_DISASSEMBLY_SHOWID` 默认开启，前缀是设备名）：

```text
IKA32010_ikakawa: RESET
IKA32010_ikakawa:  PC=0x000 | NOP
IKA32010_ikakawa:  PC=0x001 | LACK 0x05
IKA32010_ikakawa:  PC=0x002 | LACK 0x0A
IKA32010_ikakawa:  PC=0x003 | NOP
...
```

- `PC=0x000 | NOP` 对应复位后执行的第一条指令（地址 0 的 NOP）。
- 之后 PC 依次推进到 1、2、3，助记符随之变成 `LACK 0x05`、`LACK 0x0A`、`NOP`。
- 若用波形查看器观察 DUT 内部 `main.alu_acc_output`（或累加器寄存器），应在执行完 `LACK 0x05` 后看到 ACC=`0x0005`，执行完 `LACK 0x0A` 后看到 ACC=`0x000A`。

### 5.4 预期结果

PC 按 `0→1→2→3` 推进，反汇编日志依次打印 `NOP / LACK 0x05 / LACK 0x0A / NOP`，累加器被立即数 `0x05`、`0x0A` 覆盖。

### 5.5 待本地验证

本环境未安装 Verilog 仿真器，上述日志与波形为**依据源码与指令编码静态推算的预期结果，待本地验证**。请在装有 iverilog / Verilator / ModelSim 等工具的本地环境中运行确认。

> 编码依据（均来自源码）：NOP = `16'b0111_1111_1000_0000`（`0x7F80`），见 [src/IKA32010.sv:639](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L639)；LACK 立即数取指令低 8 位、编码 `16'b0111_1110_????_????`（`0x7E00 | K`），见 [src/IKA32010.sv:879-884](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L879-L884)。

---

## 6. 本讲小结

- testbench 由「自由时钟 + initial 激励 + DUT 实例化」三件套组成；`IKA32010_tb.v` 用 `divider` 四分频 `EMUCLK` 得到 1/4 占空比的 `i_CLKIN_PCEN`，使仿真里一个机器周期 = 16 个 `EMUCLK`。
- 复位 `i_RS_n` 低有效（两次复位脉冲），中断 `i_INT_n` 下降沿触发（一次脉冲），`i_BIO_n` 在本 testbench 里常接高、不测 BIOZ。
- IKA32010 无片内 ROM，指令靠 `i_DIN` 读入；testbench 用 `dsp_hi`/`dsp_lo` 两个 8 位数组拼出 16 位指令字，通过 `$readmemh` 装载（原版用了仓库里不存在的绝对路径，需改造）。
- DUT 在 `cyclecntr==3` 且为「指令读」事务时把 `i_DIN` 锁存进 `if_opcodereg`，与 testbench 在 `MEN_n` 低电平驱动指令字形成闭环。
- 外部总线复用的核心：两条 `assign` 靠 `o_MEN_n` / `o_DEN_n` 互斥选通 + 高阻 `Z`，让指令 ROM 和数据 RAM 分时共享同一条 `RDBUS`；DUT 保证两选通从不同时为低，杜绝驱动冲突。

---

## 7. 下一步学习建议

到这里，你已经能从外部把 IKA32010「点亮」并看到它取指执行。下一步建议进入进阶层 u2，从**数据通路**入手理解核内机制：

- **u2-l1（内部写总线 `reg_wrbus`）**：本讲你看到了 `i_DIN`/`RDBUS` 这条外部总线，接下来认识核内那条贯穿所有子模块的「内部写总线」与它的 7 个数据源。
- **u2-l2（程序计数器 PC）**：本讲你观察到 PC 推进，下一讲拆解 `if_pc` 的多种工作模式（HOLD / INCREASE / LOAD_IMMEDIATE …）。
- **u2-l3（总线控制器）**：本讲我们只点到了 `MEN_n`/`DEN_n`/`WE_n` 的互斥关系，下一讲逐相位分析这五种事务（指令读 / 表读 / 表写 / IN / OUT）的完整时序。

同时建议读一读 `docs/` 下的 TMS32010 用户手册中「Timing / Memory Interface」相关章节，把本讲的仿真波形和官方时序图对照看，印象会更深。
