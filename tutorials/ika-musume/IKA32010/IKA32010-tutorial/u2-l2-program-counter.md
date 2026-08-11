# 程序计数器 PC 与取指

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 12 位程序计数器 `if_pc` 是什么、它如何决定下一条要执行的指令地址。
- 解释 3 位模式选择器 `if_pc_modesel` 如何用 **6 种模式**（HOLD / INCREASE / LOAD_IMMEDIATE / LOAD_INTERRUPT / LOAD_WRBUS / RESET）控制 PC 在每个机器周期的更新方式。
- 描述指令寄存器 `if_opcodereg` 在 `cyclecntr==3` 时被锁存的取指时序，以及它与 PC 更新共享同一个节拍的「一周期流水」关系。
- 指出 PC 在 `0xFFF` 处的回绕行为，以及中断向量地址 `0x002` 的来源。
- 跟踪一次 `CALL → RET` 过程，把 `if_pc` 与 `if_pc_modesel` 的取值序列写出来，并解释栈与 PC 如何协作完成调用与返回。

## 2. 前置知识

在进入本讲前，请确认你已经掌握以下概念（它们在前序讲义中已建立，这里只做最简回顾）：

- **机器周期与四分频**（u1-l4）：2 位计数器 `cyclecntr` 在 `0→1→2→3` 循环，4 个 `i_EMUCLK` 构成 1 个 DSP 机器周期。`cyc_ncen` 在 `cyclecntr==3` 时拉高一个 `EMUCLK`，是芯片的**主力工作拍**——PC、栈、ALU 写回等几乎所有状态更新都发生在 `cyc_ncen` 上升沿。
- **内部写总线 `reg_wrbus`**（u2-l1）：一条 16 位的全局数据汇流，由组合 MUX `register_wrbus_source_sel` 在 7 个数据源里挑一个送上总线。本讲会用到它的一个读端口：PC 可以从 `reg_wrbus[11:0]` 装入新值（模式 `PC_LOAD_WRBUS`）。
- **「端口名即文档」**：`i_`/`o_` 表方向、`_n` 表低电平有效。本讲反复出现的 `i_DIN` 是 16 位外部数据输入线，取指时它承载从程序 ROM 读回的指令字。
- **水平微码风格**：顶层那个大 `always @(*)` 微码块先为所有控制信号赋「默认值」，再用 `casez(if_opcodereg)` 覆盖。PC 模式选择器 `if_pc_modesel` 也是这样驱动的。

> 一句话定位：本讲只关心「PC 怎么走、指令怎么进来」这两件事；至于 PC 走到的地址上具体发生哪种**总线事务**（指令读 / 表读 / 表写 / IN / OUT），那是下一讲 u2-l3「总线控制器」的内容。

## 3. 本讲源码地图

本讲涉及的关键文件与代码点：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `src/IKA32010.sv` | 顶层模块，含 PC、取指、微码 | `if_pc` 寄存器、`if_pc_modesel`、`if_opcodereg` 取指块、微码默认值与中断检查 |
| `src/IKA32010_mnemonics.sv` | 常量字典 | `PC_HOLD`…`PC_RESET` 六个模式常量 |
| `src/IKA32010_disasm.sv` | 反汇编工具（仿真打印） | `disasm_type0` 等函数如何打印 PC，用于验证取指行为 |

## 4. 核心概念与源码讲解

### 4.1 if_pc：12 位程序计数器与地址回绕

#### 4.1.1 概念说明

程序计数器（Program Counter，简称 PC）是任何处理器里最基础的寄存器之一：它**存放下一条要取指的指令地址**。取指时，PC 的值会被送到地址线 `o_AOUT` 上，外部程序 ROM 据此把对应字的指令内容放到数据线 `i_DIN` 上供核心读入。

IKA32010 复刻的 TMS32010 拥有 **4K 字的程序地址空间**，因此 PC 的宽度是 12 位：

\[ 2^{12} = 4096 \text{ 个字} \quad\Rightarrow\quad \text{地址范围 } 0x000 \sim 0xFFF \]

在源码里，PC 这个寄存器叫 `if_pc`（`if` 前缀暗示它属于 **i**nstruction **f**etch「取指」阶段）。

#### 4.1.2 核心流程

PC 的更新遵循三条规则：

1. **同步复位**：`i_RS_n` 为低时，PC 被强制清零到 `0x000`。
2. **节拍门控**：只有在 `cyc_ncen`（`cyclecntr==3`）的上升沿，PC 才可能改变——也就是说 PC **每个机器周期最多更新一次**。
3. **模式驱动**：PC 怎么变，由 3 位选择器 `if_pc_modesel` 决定（详见 4.2）。其中最常见的是 `PC_INCREASE`，把 PC 加 1，指向顺序的下一条指令。

地址回绕用一个组合线网 `if_pc_next` 描述：当 PC 已经是 `0xFFF`（4K 空间的最高地址）时，再 `+1` 不是变成 `0x1000`（超出 12 位），而是**回到 `0x000`**。这就像 12 位计数器自然溢出。

```text
if_pc_next = (if_pc == 0xFFF) ? 0x000 : if_pc + 1
```

#### 4.1.3 源码精读

PC 的声明与回绕线网紧挨在一起，注释直接写明 `//program counter`：

[src/IKA32010.sv:98-100](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L98-L100) —— 声明 12 位 `if_pc`、3 位 `if_pc_modesel`，以及回绕线网 `if_pc_next`。回绕逻辑 `(if_pc == 12'hFFF) ? 12'd000 : if_pc + 12'h001` 就在这一行。

PC 的更新主体是一个 `always @(posedge i_EMUCLK)` 块，外层先判同步复位，内层用 `cyc_ncen` 做节拍门控，最里层是 `case(if_pc_modesel)`：

[src/IKA32010.sv:102-115](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L102-L115) —— PC 的核心更新逻辑。注意三个层次：`if(!i_RS_n)` 复位 → `if(cyc_ncen)` 节拍 → `case(if_pc_modesel)` 模式分派。六种模式对应六种 PC 更新方式；`default` 兜底为保持原值。

PC 的值在取指时被送上地址线。总线控制器里有一个地址 MUX，当处于「PC 地址」模式时，`o_AOUT` 直接等于 `if_pc`：

[src/IKA32010.sv:159-164](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L159-L164) —— `busctrl_addr` 在 `busctrl_mode[3]==0` 时取 `if_pc`（程序计数器），在 `==1` 时取 PA 外设端口地址。这就是 PC 如何「变成」对外地址线的。

#### 4.1.4 代码实践

**实践目标**：在仿真中观察 PC 的顺序自增，以及 0xFFF 回绕现象。

**操作步骤**（源码阅读型 + 仿真观察型）：

1. 打开 `src/IKA32010.sv`，确认 PC 的节拍门控是 `cyc_ncen` 而不是每个 `EMUCLK` 都更新——这解释了为什么 PC「一个机器周期才走一步」。
2. 启用项目自带的反汇编：在编译时定义宏 `IKA32010_DISASSEMBLY`（具体方式见 u3-l8）。反汇编函数会把**每条实际执行的指令**连同它的地址打印出来。
3. 阅读反汇编函数如何处理 PC：[src/IKA32010_disasm.sv:8-21](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L8-L21)。注意第 15 行打印的是 `{pc-1}[11:0]`——它从传入的 `if_pc` 里**减 1** 才得到真实指令地址。这是一个关键线索，4.3 节会解释为什么。

**需要观察的现象**：反汇编输出里 `PC=0x...` 应当随指令顺序递增（`0x000, 0x001, 0x002, …`），每条单周期指令占一行。

**预期结果**：PC 顺序递增；若测试程序足够长、跨越 0xFFF，可观察到回绕到 0x000。**若你不方便运行仿真，本步骤可标注「待本地验证」，仅通过阅读源码确认回绕表达式即可。**

#### 4.1.5 小练习与答案

**练习 1**：如果 PC 当前是 `0xFFF`，模式是 `PC_INCREASE`，下一个机器周期 PC 变成多少？

**参考答案**：`0x000`。因为 `if_pc_next = (if_pc==0xFFF) ? 0x000 : if_pc+1`，0xFFF 是回绕点。

**练习 2**：PC 是 12 位，但内部写总线 `reg_wrbus` 是 16 位。当 PC 从 `reg_wrbus` 装入时（模式 `PC_LOAD_WRBUS`），用到的是哪几位？为什么这样不会出错？

**参考答案**：用到 `reg_wrbus[11:0]`（低 12 位）。因为 PC 只有 12 位宽，截取低 12 位正好对应 4K 地址空间；高 4 位被丢弃。

---

### 4.2 if_pc_modesel 与 PC 模式常量

#### 4.2.1 概念说明

光有一个会 `+1` 的 PC 还不够。处理器要支持跳转（B）、调用子程序（CALL）、返回（RET）、响应中断，PC 就得能在「保持不动 / 自增 / 装入立即数 / 跳到中断向量 / 从总线装入 / 复位」之间切换。IKA32010 用一个 3 位选择器 `if_pc_modesel` 来表达这层「PC 这一拍该怎么动」的意图，它由微码组合地驱动。

3 位共可表达 8 个值，本项目定义并使用了其中 6 个，写成命名常量放在 `IKA32010_mnemonics.sv` 里。给常量起名字（而不是到处写魔法数字 `3'd2`）是这份代码可读性的关键。

#### 4.2.2 核心流程

六种模式及其行为：

| 常量名 | 值 | PC 更新方式 | 典型用途 |
|--------|----|------------|---------|
| `PC_HOLD` | 0 | 保持不变 | 多周期指令的中间相位（如 POP/PUSH/IN/OUT 的 cycle 0），PC 暂停推进 |
| `PC_INCREASE` | 0x1 | `if_pc + 1`（带回绕） | **默认**：顺序执行下一条指令 |
| `PC_LOAD_IMMEDIATE` | 0x2 | `i_DIN[11:0]` | 跳转/调用：从外部总线读入目标地址装入 PC（B / CALL） |
| `PC_LOAD_INTERRUPT` | 0x3 | `0x002` | 响应中断：跳到中断向量地址 |
| `PC_LOAD_WRBUS` | 0x4 | `reg_wrbus[11:0]` | 从内部总线装入：RET 从栈顶取返回地址，CALA 从累加器取目标 |
| `PC_RESET` | 0x5 | `0x000` | 复位态：PC 归零 |

值的 6、7 未定义，在 `case` 里落入 `default`（等价 `PC_HOLD`）。

微码块对 `if_pc_modesel` 的驱动遵循「默认值 + 覆盖」的套路：

1. **默认值**：`if_pc_modesel = PC_INCREASE`（绝大多数指令顺序执行，沿用默认即可）。
2. **复位态覆盖**：当 `ex_state==0`（仍处于复位延迟），强制 `PC_RESET`。
3. **中断检查覆盖**：进入正常态后，先看有无中断请求 `int_rq`——有则 `PC_LOAD_INTERRUPT`，无则维持 `PC_INCREASE`。
4. **指令级覆盖**：`casez(if_opcodereg)` 里，需要改写 PC 行为的指令（B / CALL / RET / CALA / 多周期指令的某些相位）再各自覆盖 `if_pc_modesel`。

这种「先铺默认、再层层覆盖」就是水平微码（horizontal microcode）的核心思想——**一条指令通常只需要覆盖它关心的那几个控制信号，其余的都自动取默认值**。

#### 4.2.3 源码精读

六个常量集中定义在 mnemonics 文件开头，紧跟 `//Program counter control` 注释横幅：

[src/IKA32010_mnemonics.sv:1-7](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L1-L7) —— 六个 `PC_*` 常量定义。后面紧跟着 `WRBUS_SOURCE_*`、`BUSCTRL_*` 等其他常量，构成整个项目的「控制信号字典」。

PC 更新 `case` 体里，每个模式分支一行，与上表一一对应：

[src/IKA32010.sv:105-113](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L105-L113) —— `case(if_pc_modesel)` 的六个分支：HOLD 保持、INCREASE 取 `if_pc_next`、LOAD_IMMEDIATE 取 `i_DIN[11:0]`、LOAD_INTERRUPT 取常量 `12'h002`、LOAD_WRBUS 取 `reg_wrbus[11:0]`、RESET 取 `12'h000`。

微码默认值区里，PC 模式的默认就是顺序自增：

[src/IKA32010.sv:543](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L543) —— 微码默认 `if_pc_modesel = PC_INCREASE`。这是「顺序执行」之所以是默认行为的根源。

复位态把默认覆盖成 `PC_RESET`，并停掉总线事务：

[src/IKA32010.sv:600-602](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L600-L602) —— `ex_state==0`（复位延迟期）时 `if_pc_modesel = PC_RESET`、`busctrl_req = BUSCTRL_STOP`。

进入正常态后，先做中断检查，决定是跳中断向量还是继续自增：

[src/IKA32010.sv:613-616](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L613-L616) —— 中断检查：`if_pc_modesel = (int_rq) ? PC_LOAD_INTERRUPT : PC_INCREASE`。同一个三元表达式还顺便设置了 `if_opcodereg_force_iack` 和栈 push（中断响应会压栈保存返回地址）。

#### 4.2.4 代码实践

**实践目标**：把源码中所有「显式改写 `if_pc_modesel`」的位置整理成一张表，建立「哪条指令用了哪种 PC 模式」的全局认识。

**操作步骤**（源码阅读型）：

1. 在 `src/IKA32010.sv` 中检索 `if_pc_modesel =`（赋值，不是 `==` 比较）的所有出现点。
2. 对每一处，记录：所在指令（看上方注释横幅或 `casez` 标签）、所在 `ex_inst_cycle` 相位、赋成哪种模式。
3. 把结果归纳成表。

**需要观察的现象**：你会发现改写集中出现在 BRANCH（B/BANZ/BGEZ…/BV/BZ）、CALL、CALA、RET，以及 POP/PUSH/IN/OUT/DMOV 的多周期相位里；而绝大多数算术逻辑指令（ADD/LAC/SUB…）**根本不碰** `if_pc_modesel`——它们默默沿用默认的 `PC_INCREASE`。

**预期结果**：得到一张类似「`B` cycle0 → `PC_LOAD_IMMEDIATE`」「`RET` cycle0 → `PC_LOAD_WRBUS`」「`IN` cycle0 → `PC_HOLD`」的对照表。这正是水平微码「少数指令才需要覆盖」的直观证据。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `RET` 用 `PC_LOAD_WRBUS`（配合 `WRBUS_SOURCE_STACK`）而不是 `PC_LOAD_IMMEDIATE`？

**参考答案**：返回地址事先压在硬件栈里，不是当前指令字的一部分，也不在本次总线读入的 `i_DIN` 上。所以先把栈顶送上 `reg_wrbus`，再用 `PC_LOAD_WRBUS` 从 `reg_wrbus[11:0]` 装入 PC。`PC_LOAD_IMMEDIATE` 装的是 `i_DIN[11:0]`，用于目标地址来自外部总线的 B/CALL。

**练习 2**：值 6 和 7 没有对应的 `PC_*` 常量。如果由于某种错误 `if_pc_modesel` 变成了 7，会发生什么？

**参考答案**：落入 `case` 的 `default` 分支，`if_pc <= if_pc`，即等价于 `PC_HOLD`，PC 这一拍保持不动。这是一种「安全失效」的设计。

---

### 4.3 if_opcodereg：取指时序与指令寄存器

#### 4.3.1 概念说明

PC 决定「去哪里取」，而 `if_opcodereg`（指令寄存器）保存「取回来的指令是什么」。它是一个 16 位寄存器，存放当前正在被微码 `casez` 译码的那条 16 位指令字。

这里有一个新手最容易困惑、但也最关键的时序细节：**取指与执行之间隔着一个机器周期**。换句话说：

- 本周期 `casez(if_opcodereg)` 译码并执行的那条指令，是**上一个机器周期末尾**从 ROM 锁存进来的；
- 而本周期 PC 指向的地址，是**下一条**将要执行的指令。

这就解释了 4.1.4 里反汇编函数为何打印 `{pc-1}`：因为执行某指令时，`if_pc` 已经指向「该指令地址 + 1」了，要还原真实指令地址就得减 1。

除了从总线正常取指，`if_opcodereg` 还能被**强制注入一个特殊的「内部操作码」`0xF000`（IACK）**，用来在响应中断时插入一段由微码自己合成的应答动作——这条「指令」并不存在于程序 ROM 里。

#### 4.3.2 核心流程

取指时序可以拆成「总线读」与「寄存器锁存」两半：

```text
一个机器周期内（cyclecntr: 0 → 1 → 2 → 3）：

  [0,1,2] 相位：o_MEN_n = 0（程序 ROM 使能）
               o_AOUT  = if_pc（PC 作为地址）
               → ROM 把 instruction[if_pc] 送上 i_DIN

  [3] 相位边沿（cyc_ncen 上升沿）：
               if busctrl_mode == OPCODE_READ:
                   if_opcodereg <= i_DIN          // 锁存新指令
               if if_opcodereg_force_iack:
                   if_opcodereg <= 0xF000         // 强制注入 IACK
               同时 if_pc 按 if_pc_modesel 更新    // PC 与锁存共享同一拍
```

要点：

1. **锁存条件**：只有当前机器周期是「指令读」事务（`busctrl_mode[2:0]==1`，即 `OPCODE_READ`）时，才把 `i_DIN` 锁进 `if_opcodereg`。若是数据读 / 表写 / IN 等其他事务，`if_opcodereg` 保持不变（多周期指令靠此在多个相位里「停留」在同一条指令）。
2. **IACK 注入优先**：若 `if_opcodereg_force_iack` 为真，无视总线，直接装入 `0xF000`。
3. **复位初值**：复位时 `if_opcodereg <= 0x7F80`——而 `0x7F80` 正是 **NOP** 的操作码。这样复位释放后第一个被「执行」的就是一条无害的 NOP，紧接着才取到程序地址 0 的真实指令。
4. **共享节拍**：`if_opcodereg` 的锁存与 `if_pc` 的更新发生在**同一个 `cyc_ncen` 边沿**。这是「一周期流水」的硬件根源。

#### 4.3.3 源码精读

`if_opcodereg` 与强制 IACK 标志的声明：

[src/IKA32010.sv:89-90](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L89-L90) —— 16 位指令寄存器 `if_opcodereg` 与「强制注入 IACK」标志 `if_opcodereg_force_iack`。

取指的完整 `always` 块，含复位初值、节拍门控与锁存条件：

[src/IKA32010.sv:176-188](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L176-L188) —— 复位时 `if_opcodereg <= 16'h7F80`（NOP）；正常态下，在 `i_CLKIN_PCEN` 且 `cyclecntr==2'd3` 时：若 `if_opcodereg_force_iack` 则装 `16'hF000`（IACK），否则当 `busctrl_mode[2:0]==3'd1`（指令读）时装 `i_DIN`。

注意一个细节：取指锁存用的是 `i_CLKIN_PCEN` 作为外层门控（第 182 行），再在内层判 `cyclecntr==3`。这与 PC 更新用的 `cyc_ncen = (cyclecntr==3) & i_CLKIN_PCEN`（u1-l4）最终落到**同一个边沿**。

「指令读」事务在四个相位上的总线控制电平（这段属于总线控制器，但能帮你看清取指的物理过程）：

[src/IKA32010.sv:200-208](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L200-L208) —— `cyclecntr` 为 0/1/2 时 `o_MEN_n=0`（ROM 使能、驱动 `i_DIN`），为 3 时 `o_MEN_n=1`（释放）；指令字恰好在 `cyclecntr==3` 边沿被 `if_opcodereg` 锁存。

被注入的内部 IACK 操作码 `0xF000` 在微码里有专门的 `casez` 分支处理（完成中断应答）：

[src/IKA32010.sv:625-636](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L625-L636) —— `16'b1111_0000_0000_0000`（即 `0xF000`）分支：恢复顺序取指 `if_pc_modesel = PC_INCREASE`，并在 `int_rq` 时给出 `int_ack`。中断的完整流程见 u3-l3。

#### 4.3.4 代码实践

**实践目标**：用一个最小探针，在仿真中直接观察 `if_opcodereg` 在 `cyclecntr==3` 边沿的锁存行为，验证「一周期流水」。

**操作步骤**（需要修改 testbench / 顶层，仿真观察型）：

1. 在 testbench 或顶层（临时，仅用于调试）加入一段**示例代码**探针，在每个 `cyc_ncen` 边沿打印 PC、当前指令字和模式选择器：

   ```verilog
   // 示例代码（非项目原有），建议放在顶层模块内、微码块之外
   always @(posedge i_EMUCLK) if (cyc_ncen)
       $display("t=%0t  if_pc=0x%03h  if_opcodereg=0x%04h  if_pc_modesel=%0d",
                $time, if_pc, if_opcodereg, if_pc_modesel);
   ```

2. 运行仿真，观察连续两拍：注意第 N 拍 `if_pc` 指向的地址 A，与第 N+1 拍 `if_opcodereg` 里出现的指令内容是否对应程序 ROM 中地址 A 处的字。

**需要观察的现象**：你会看到 `if_opcodereg` 的变化比 `if_pc` 指向新地址**晚一个机器周期**——这正是「本周期执行的指令是上周期取的」。

**预期结果**：若程序 ROM 地址 `A` 处存放指令字 `W`，那么在 `if_pc == A+1` 的那一拍，`if_opcodereg == W`。**若无法运行仿真，标注「待本地验证」**；仅通过对比 [src/IKA32010.sv:176-188](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L176-L188)（锁存）与 [src/IKA32010.sv:102-115](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L102-L115)（PC 更新）共享 `cyc_ncen` 边沿，即可在源码层面确认这一关系。

> 提醒：本步骤改动了源文件。本讲义严禁改源码用于正式交付——调试探针仅在你本地临时的副本上使用，验证完毕后务必还原。

#### 4.3.5 小练习与答案

**练习 1**：为什么复位时要把 `if_opcodereg` 初始化成 `0x7F80`（NOP），而不是 `0x0000`？

**参考答案**：`0x7F80` 是 NOP 的操作码。复位释放后的第一个机器周期，微码会 `casez` 译码 `if_opcodereg`——若初值是某个随机/未定义码，可能触发非预期行为；用 NOP 作初值，相当于在「真正取到地址 0 的指令」之前先执行一条空指令，保证启动过程无害且确定。

**练习 2**：多周期指令（如 IN）在它的 cycle 0 相位里通常把 `if_pc_modesel` 设成 `PC_HOLD`，并且当周期的总线事务不是「指令读」。这两个事实合在一起，对 `if_opcodereg` 有什么影响？

**参考答案**：因为当周期不是「指令读」（`busctrl_mode[2:0]!=1`），`if_opcodereg` 不会被 `i_DIN` 覆盖，保持为当前指令；于是下一个机器周期微码仍在译码同一条 IN 指令，配合 `ex_inst_cycle` 自增进入 cycle 1。`PC_HOLD` 则保证 PC 在这些中间相位不乱走。两者共同实现了「一条指令占用多个机器周期」。

---

## 5. 综合实践：跟踪一次 CALL → RET

本任务把本讲三个模块（`if_pc`、`if_pc_modesel`、`if_opcodereg`）与栈（u2-l6）串起来，验证你对 PC 控制与取指时序的理解。

### 任务背景

`CALL`（直接调用）与 `RET`（子程序返回）是 2 相位（`ex_inst_cycle` = 0, 1）指令。它们的核心是「PC 跳到目标地址 / 从栈恢复返回地址」，恰好用到了本讲最关键的两个非常规 PC 模式：`PC_LOAD_IMMEDIATE` 与 `PC_LOAD_WRBUS`。

> 说明：`CALL` 的目标地址是通过一次「数据读」从外部总线取得的（细节归 u2-l3 总线控制器）。本练习只关心 PC 与栈的配合，因此把目标地址记为抽象的 `T`，不纠结它具体来自哪片存储。

### 设定

假设程序（程序空间，经 `o_MEN_n` 读取）布局如下：

| 地址 | 内容 | 含义 |
|------|------|------|
| `0x000` | `CALL` (0xF800) | 调用子程序 |
| `0x001` | （下一条指令） | 返回点 |
| `0x010` | `RET` (0x7F8D) | 子程序：直接返回 |

`CALL` 的目标地址 `T = 0x010` 由其数据读相位从总线取得。

### 参考译码

先对照源码确认两个指令各相位做了什么：

[src/IKA32010.sv:1422-1440](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1422-L1440) —— `CALL` 译码。cycle 0：数据读取目标、`if_pc_modesel = PC_LOAD_IMMEDIATE`、`stk_push=YES` 且 `stk_data_sel=STACK_DATA_PC`（压入返回地址）、`ex_inst_cycle_rst=NO`（推进到 cycle 1）。cycle 1：恢复成 `OPCODE_READ`。

[src/IKA32010.sv:1442-1461](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1442-L1461) —— `RET` 译码。cycle 0：`if_pc_modesel = PC_LOAD_WRBUS`、`register_wrbus_source_sel = WRBUS_SOURCE_STACK`（栈顶送上总线）、`stk_pop=YES`。cycle 1：恢复成 `OPCODE_READ`。

栈实例化处可见压栈数据来源就是 `if_pc`（当 `stk_data_sel==STACK_DATA_PC`）：

[src/IKA32010.sv:412-416](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L412-L416) —— 栈的 `i_DIN = stk_data_sel ? if_pc : reg_wrbus[11:0]`。`CALL` 压的是 `if_pc`，`RET` 出栈经 `reg_wrbus` 回灌给 PC。

### 时序追踪表

下表用「本拍 `if_pc` / `if_pc_modesel` → 边沿后发生什么」的形式给出完整序列。设取指 `CALL` 的那一拍 `if_pc=0x000`（边沿后 `if_opcodereg` 变成 CALL、`if_pc` 变 `0x001`）。

| 拍 | `if_opcodereg` | `ex_inst_cycle` | `if_pc`（本拍值） | `if_pc_modesel` | `cyc_ncen` 边沿后的变化 |
|----|----------------|-----------------|------------------|-----------------|------------------------|
| ① 取指 CALL | （上条） | 0 | `0x000` | `PC_INCREASE`（默认） | `if_opcodereg←CALL`；`if_pc←0x001` |
| ② CALL cycle0 | CALL | 0 | `0x001` | `PC_LOAD_IMMEDIATE` | 数据读得目标 `T=0x010`；`if_pc←0x010`；**栈压入 `0x001`**；`ex_inst_cycle←1` |
| ③ CALL cycle1 | CALL | 1 | `0x010` | `PC_INCREASE`（默认） | `if_opcodereg←RET`（取到 `0x010` 处）；`if_pc←0x011`；`ex_inst_cycle←0` |
| ④ RET cycle0 | RET | 0 | `0x011` | `PC_LOAD_WRBUS` | `reg_wrbus←栈顶=0x001`；`if_pc←0x001`；**栈弹出**；`ex_inst_cycle←1` |
| ⑤ RET cycle1 | RET | 1 | `0x001` | `PC_INCREASE`（默认） | `if_opcodereg←`（`0x001` 处的指令，即返回点）；`if_pc←0x002`；`ex_inst_cycle←0` |

### 你应该得出的结论

- **调用方向**：`CALL` 在 cycle0 用 `PC_LOAD_IMMEDIATE` 把 PC 装成目标 `T`，同时把**返回地址 = 当时 `if_pc`（`0x001`，即 CALL 的下一条程序地址）**压栈。
- **返回方向**：`RET` 在 cycle0 用 `PC_LOAD_WRBUS`，配合 `WRBUS_SOURCE_STACK` 把栈顶（返回地址 `0x001`）经 `reg_wrbus` 装回 PC，同时弹栈。
- **PC 与栈的协作**：PC 负责「记着去哪儿」，栈负责「记着回哪儿」。`CALL` 写栈 + 改 PC；`RET` 读栈 + 改 PC，两者对称。
- **两相位结构**：两条指令都在 cycle1 把总线事务切回 `OPCODE_READ`，让取指流水线重新对齐——这就是为什么它们各自占 2 个机器周期。

### 验证建议

启用 `IKA32010_DISASSEMBLY` 宏跑仿真，对照反汇编输出（每行带 `PC=0x...`），检查：`CALL` 出现在 `PC=0x000`，下一条出现的是子程序里的指令（`PC=0x010`），`RET` 之后回到 `PC=0x001`。**若本地暂不具备仿真环境，标注「待本地验证」**，仅以上述源码追踪作为依据即可完成理解。

## 6. 本讲小结

- `if_pc` 是 12 位程序计数器，对应 4K 字程序空间，地址范围 `0x000~0xFFF`；到达 `0xFFF` 后自增会**回绕到 `0x000`**。
- PC 只在 `cyc_ncen`（`cyclecntr==3`）边沿更新，每个机器周期最多变一次。
- `if_pc_modesel` 是 3 位 PC 模式选择器，定义了 **6 种模式**（HOLD/INCREASE/LOAD_IMMEDIATE/LOAD_INTERRUPT/LOAD_WRBUS/RESET），由水平微码「默认 `PC_INCREASE` + 按需覆盖」驱动。
- 中断会让 PC 跳到固定向量地址 **`0x002`**（`PC_LOAD_INTERRUPT`）。
- `if_opcodereg` 是 16 位指令寄存器；当机器周期为「指令读」时，在 `cyclecntr==3` 边沿把 `i_DIN` 锁存进来；复位初值是 NOP（`0x7F80`）；中断时可被强制注入内部 IACK 码 `0xF000`。
- 取指锁存与 PC 更新**共享同一个 `cyc_ncen` 边沿**，形成「一周期流水」：本拍译码的指令是上拍取的，本拍 PC 指向下一条。

## 7. 下一步学习建议

- **u2-l3 外部总线控制器**：本讲反复提到「指令读 / 数据读」决定了 `if_opcodereg` 是否锁存、以及 `o_AOUT` 在 PC 与 PA 之间如何切换，这些都由 `busctrl_mode` / `busctrl_req` 在四个相位上的电平决定，下一讲会逐相位讲透。
- **u2-l6 硬件堆栈**：本讲的综合实践已用到栈的 push/pop，但栈的 4 级移位式实现、`stk_data_sel` 在 PC 与 ACC 间的选择尚未展开，留给堆栈专讲。
- **u3-l3 中断机制**：本讲只点了中断向量 `0x002` 和 IACK 注入；`i_INT_n` 的多级同步、`int_latched`、`int_rq` 与 `reg_intm` 的关系将在中断专讲里完整串联。
- **延伸阅读**：对照 `docs/` 下 TMS32010 用户手册中关于「Program Counter」「Stack」「Interrupts」的章节，把源码行为与官方时序图一一对应。
