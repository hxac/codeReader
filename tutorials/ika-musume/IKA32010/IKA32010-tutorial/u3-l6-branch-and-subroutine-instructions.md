# 分支与子程序类指令译码

## 1. 本讲目标

本讲剖析 IKA32010 微码中「分支与子程序类」共 13 条指令的译码逻辑。学完后你应当能够：

- 说清为什么分支/调用类指令一律是「两周期（两相位）」结构，并在 `cycle0` / `cycle1` 两个相位里分别做了什么；
- 把 8 条条件分支（BGEZ/BGZ/BLEZ/BLZ/BNZ/BZ/BV/BIOZ）外加 B、BANZ 的「跳转条件」整理成一张表，并对照源码确认每一条依据的是哪个标志位；
- 解释 CALL / CALA / RET 如何借助四级硬件栈完成「压返回地址 → 跳转 → 弹回 PC」的调用与返回。

本讲是专家层的第 6 讲，建立在 [u3-l1（微码架构）](u3-l1-microcode-architecture.md)、[u3-l2（多周期时序）](u3-l2-multicycle-timing-and-state.md) 和 [u2-l6（硬件栈）](u2-l6-hardware-stack.md) 之上。如果你对「默认值 + casez 覆盖」「`ex_inst_cycle` 相位计数」「`stk_push/stk_pop`」这些概念还生疏，建议先回看那三讲。

## 2. 前置知识

在读懂本讲之前，请确认你已经理解下面几个概念（前序讲义已建立）：

- **水平微码与默认值**：IKA32010 用一个巨大的组合 `always @(*)` 块充当「微码存储器」。它先为所有控制信号赋默认值，再用 `casez(if_opcodereg)` 按需覆盖。因此多数指令只需改写少数几个开关。
- **指令周期计数器 `ex_inst_cycle`**：2 位计数器，复位信号 `ex_inst_cycle_rst=YES` 时归零，`=NO` 时自增。单周期指令永远停在相位 0；多周期指令在前几个相位把它改写为 `NO`，最后一个相位依赖默认 `YES` 自动归零。
- **`if_pc_modesel` 的 6 种 PC 模式**：`PC_HOLD`（保持）、`PC_INCREASE`（顺序+1）、`PC_LOAD_IMMEDIATE`（从外部总线 `i_DIN` 装载）、`PC_LOAD_INTERRUPT`（装 `0x002`）、`PC_LOAD_WRBUS`（从内部写总线装载）、`PC_RESET`（清零）。
- **四级硬件栈**：由 `stk_push` / `stk_pop` / `stk_data_sel` 三个信号驱动；`stk_data_sel` 选择压栈数据来自 PC 还是写总线，所有移位发生在 `cyc_ncen`（相位 3）节拍。
- **取指流水偏移**：由于「本拍译码的是上拍取来的指令、本拍 PC 已指向下一个字」，执行地址为 A 的指令时 `if_pc` 已经等于 A+1。反汇编里打印 `{pc-1}` 正是为了补偿这一拍偏移。

一个贯穿全讲的关键事实：**TMS32010 的分支/调用指令是「两字指令」**——第一个字是操作码，第二个字是 12 位目标地址。所以 CPU 必须多花一个机器周期去程序空间把目标地址读进来，这正是「两相位」结构的根源。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/IKA32010.sv](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv) | 主体。本讲重点：PC 更新逻辑、微码默认值区、`BRANCH INSTRUCTIONS` 译码段（B/BANZ/…/BZ/CALA/CALL/RET） |
| [src/IKA32010_mnemonics.sv](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv) | 常量字典。`PC_*` 模式常量、`WRBUS_SOURCE_*` 写总线源常量、`YES/NO` 等 |

本讲对应的最小模块有三个：①分支类指令的 casez 分支与两相位结构；②条件判断逻辑（标志位 → 是否跳转）；③CALL/CALA/RET 与栈的配合。

## 4. 核心概念与源码讲解

### 4.1 分支与子程序类指令的统一两相位结构

#### 4.1.1 概念说明

打开 [src/IKA32010.sv](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv) 的 `BRANCH INSTRUCTIONS` 段，你会看到 B、BANZ、BGEZ……一直到 RET，**每一条都长得几乎一样**：一个 `if(ex_inst_cycle==2'd0) … else if(ex_inst_cycle==2'd1) …` 的两分支结构。

这种高度一致并非巧合，而是由指令本身的格式决定的。分支/调用类指令的目标地址存放在「操作码的下一个程序字」里，CPU 必须先把这个字从外部总线读进来，才能知道往哪儿跳。于是：

- **相位 0（`cycle0`）**：发起一次「表读（`DATA_READ`）」事务，从 `if_pc` 指向的程序字里读出目标地址；同时（对条件分支）判断是否跳转，设置 `if_pc_modesel`。
- **相位 1（`cycle1`）**：在新的 PC 处恢复正常取指（`OPCODE_READ`），把目标处的指令取进指令寄存器。

也就是说，分支类指令借用的是 u3-l2 讲过的「多周期指令 + `ex_inst_cycle`」机制：`cycle0` 里把 `ex_inst_cycle_rst` 改写成 `NO` 推进到 `cycle1`，`cycle1` 用默认的 `YES` 自动归零、回到单周期节奏。

#### 4.1.2 核心流程

以无条件跳转 B 为模板，两相位流程如下：

```text
┌─ cycle0 ──────────────────────────────────────────────────────┐
│  busctrl_req      = DATA_READ     ← 从 if_pc 读「目标地址字」  │
│  if_pc_modesel    = PC_LOAD_IMMEDIATE  ← 条件成立时把 PC ← i_DIN[11:0] │
│  ex_inst_cycle_rst= NO            ← 推进到 cycle1              │
│  if_opcodereg_force_iack = NO     ← 两周期内拒绝中断（原子性） │
│  （cyc_ncen 边沿：PC 装载目标，cycle → 1）                     │
└────────────────────────────────────────────────────────────────┘
┌─ cycle1 ──────────────────────────────────────────────────────┐
│  busctrl_req      = OPCODE_READ   ← 在新 PC 处恢复取指         │
│  （其余用默认值：ex_inst_cycle_rst=YES → cycle 归零）          │
└────────────────────────────────────────────────────────────────┘
```

几个需要弄懂的点：

1. **为什么读目标用 `DATA_READ` 而不是 `OPCODE_READ`？** 两者都从程序空间读（`o_MEN_n` 拉低），但总线控制器在 `DATA_READ` 事务的 `cyclecntr==3` 处会把 `i_DIN` 锁进 `busctrl_inlatch`，供反汇编显示；而 `OPCODE_READ` 会把 `i_DIN` 当作指令锁进 `if_opcodereg`。分支在读目标时显然不想污染指令寄存器，所以用 `DATA_READ`。
2. **PC 怎么拿到目标地址？** 注意 `PC_LOAD_IMMEDIATE` 的实现是直接采样外部总线：`if_pc <= i_DIN[11:0]`。也就是说在 `cycle0` 的 `cyc_ncen` 边沿，「读目标字」和「PC 装载目标」是同一拍、同一条总线数据完成的。
3. **为什么每条都写 `if_opcodereg_force_iack = NO`？** 这正是 u3-l2 讲的「多周期指令原子性」：在 `cycle0` 强制把该信号写成 `NO`，即便此刻有挂起的中断（`int_rq`），指令寄存器也不会被刷成内部 IACK 码，从而保证分支/调用指令的两个相位连续执行完，不被中断打断。注释里的 `//deny interrupt request` 就是这个意思。

#### 4.1.3 源码精读

**PC 的 6 种模式**定义在 [src/IKA32010_mnemonics.sv:1-9](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L1-L9)：

```systemverilog
localparam  PC_HOLD             = 3'd0;
localparam  PC_INCREASE         = 3'd1;
localparam  PC_LOAD_IMMEDIATE   = 3'd2;  // if_pc <= i_DIN[11:0]
localparam  PC_LOAD_INTERRUPT   = 3'd3;  // if_pc <= 0x002
localparam  PC_LOAD_WRBUS       = 3'd4;  // if_pc <= reg_wrbus[11:0]
localparam  PC_RESET            = 3'd5;
```

PC 的实际更新逻辑在 [src/IKA32010.sv:102-115](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L102-L115)，注意 `PC_LOAD_IMMEDIATE` 与 `PC_LOAD_WRBUS` 两个分支——分支跳转用前者，RET/CALA 用后者：

```systemverilog
always @(posedge i_EMUCLK) begin
    if(!i_RS_n) if_pc <= 12'h000;
    else begin if(cyc_ncen) begin
        case(if_pc_modesel)
            PC_HOLD           : if_pc <= if_pc;
            PC_INCREASE       : if_pc <= if_pc_next;          // 顺序+1，到 0xFFF 回绕
            PC_LOAD_IMMEDIATE : if_pc <= i_DIN[11:0];          // ← 分支：目标来自外部总线
            PC_LOAD_INTERRUPT : if_pc <= 12'h002;
            PC_LOAD_WRBUS     : if_pc <= reg_wrbus[11:0];      // ← RET/CALA：目标来自写总线
            PC_RESET          : if_pc <= 12'h000;
            default           : if_pc <= if_pc;
        endcase
    end end
end
```

微码默认值区在 [src/IKA32010.sv:537-549](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L537-L549)，与本讲相关的三条默认值是：`busctrl_req=OPCODE_READ`、`if_pc_modesel=PC_INCREASE`、`ex_inst_cycle_rst=YES`、`if_opcodereg_force_iack=YES`。注意中断预检查 [src/IKA32010.sv:611-616](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L611-L616) 会把 `force_iack` 改写成 `(int_rq)?YES:NO`，所以无中断时它为 `NO`，正常取指才不会被刷成 IACK。

`ex_inst_cycle` 计数器在 [src/IKA32010.sv:511-515](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L511-L515)：

```systemverilog
always @(posedge i_EMUCLK) if(cyc_ncen) begin
    ex_inst_cycle <= (ex_inst_cycle_rst) ? 2'd0 : ex_inst_cycle + 2'd1;
end
```

而模板化的两相位结构，以无条件跳转 **B** 为例，见 [src/IKA32010.sv:1193-1211](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1193-L1211)：

```systemverilog
//B - Branch unconditionally
16'b1111_1001_0000_0000: begin
    if(ex_inst_cycle == 2'd0) begin
        busctrl_req = DATA_READ; busctrl_addr_muxsel = BUSCTRL_ADDR_PC;
        if_pc_modesel = PC_LOAD_IMMEDIATE;     // 无条件：直接装载目标
        ex_inst_cycle_rst = NO;                 // → cycle1
        if_opcodereg_force_iack = NO;           // 拒绝中断
        stk_push = NO; stk_data_sel = STACK_DATA_ACC;
    end
    else if(ex_inst_cycle == 2'd1) begin
        busctrl_req = OPCODE_READ; busctrl_addr_muxsel = BUSCTRL_ADDR_PC;  // 恢复取指
    end
    ...
end
```

把这段当作「模板」记住：把 `if_pc_modesel = PC_LOAD_IMMEDIATE` 换成一个三元表达式 `(条件)?PC_LOAD_IMMEDIATE:PC_INCREASE`，再加几条标志位的副作用，就得到了其余各条条件分支。

#### 4.1.4 代码实践

**实践目标**：用眼睛在源码里验证「所有分支/调用类指令都遵循同一个两相位模板」，并量出它们的操作码。

**操作步骤**：

1. 打开 [src/IKA32010.sv](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv)，定位 `//  BRANCH INSTRUCTIONS`（约 1190 行）到 `RET`（约 1461 行）这一整段。
2. 对 13 条指令（B、BANZ、BGEZ、BGZ、BIOZ、BLEZ、BLZ、BNZ、BV、BZ、CALA、CALL、RET）逐条检查，确认：每条都有 `if(ex_inst_cycle==2'd0)` 分支，且该分支里都出现了 `ex_inst_cycle_rst = NO` 与 `if_opcodereg_force_iack = NO`。
3. 把每条的 16 位 casez 标签换算成十六进制操作码，例如 `16'b1111_1001_0000_0000` → `0xF900`（B）。

**需要观察的现象**：你会发现 CALA 与 RET 的 `cycle0` 里 `busctrl_req` 不是 `DATA_READ` 而是 `BUSCTRL_STOP`（它们的目标/返回地址不在程序字里，4.3 节详述）；除此之外模板高度一致。

**预期结果**：除 CALA/RET 外，其余分支/调用在 `cycle0` 都是 `DATA_READ + 条件跳转 + cycle1 恢复取指`。如果你列出的操作码表与本讲 4.2 节的表格一致，说明你已掌握模板。

#### 4.1.5 小练习与答案

**练习 1**：分支指令在 `cycle0` 用 `DATA_READ` 读取目标地址。如果不小心写成 `OPCODE_READ`，会出现什么问题？

**参考答案**：`OPCODE_READ` 事务会在 `cyclecntr==3` 把 `i_DIN` 锁进 `if_opcodereg`（指令寄存器），于是刚取来的「目标地址字」会被当成下一条指令去译码，分支目标丢失；而且 `if_opcodereg` 被改写后 `casez` 就匹配不到原来的分支指令，第二个相位无法执行。`DATA_READ` 则只把数据锁进 `busctrl_inlatch`，不污染指令寄存器。

**练习 2**：为什么 `cycle1` 里只写了一句 `busctrl_req = OPCODE_READ`，却能让 `ex_inst_cycle` 自动归零？

**参考答案**：因为 `ex_inst_cycle_rst` 的默认值是 `YES`（微码默认值区给定），`cycle1` 没有覆盖它，所以在 `cyc_ncen` 边沿计数器被清回 0，指令自然回到单周期节奏。这正是 u3-l2 讲的「最后一相位依赖默认值自终止」。

---

### 4.2 条件判断逻辑：标志位与跳转条件

#### 4.2.1 概念说明

TMS32010 的条件分支并不是「算一遍再比」，而是**直接复用上一条算术/逻辑指令留下的标志位**。IKA32010 的 ALU 子模块对外输出三个标志（[src/IKA32010.sv:444-455](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L444-L455)）：

- `alu_flag_zero`（Z）：累加器 ACC 为 0；
- `alu_flag_neg`（N）：ACC 的符号位为 1（即 ACC 为负）；
- `alu_flag_ovfl`（V）：有符号溢出。

条件分支的译码极其简洁——把 `if_pc_modesel` 写成一个三元表达式即可：

```systemverilog
if_pc_modesel = (<条件>) ? PC_LOAD_IMMEDIATE : PC_INCREASE;
```

条件为真 → 跳转（`PC_LOAD_IMMEDIATE`，装载目标）；条件为假 → 顺序执行（`PC_INCREASE`）。注意「顺序执行」时 `if_pc` 从相位 0 的「目标地址字」处 +1，正好跳过这个操作数字，落到分支指令之后的真正下一条指令——两字指令的 PC 处理因此自洽。

#### 4.2.2 核心流程

为什么 N、Z 两个标志就能表达「大于/小于/等于」？因为 ACC 是 32 位补码有符号数。对任意补码值 \(v\)：

\[
\begin{aligned}
v < 0 &\iff N \\
v > 0 &\iff \neg N \land \neg Z \\
v \ge 0 &\iff \neg N \\
v \le 0 &\iff N \lor Z \\
v = 0 &\iff Z \\
v \ne 0 &\iff \neg Z
\end{aligned}
\]

于是 6 条「累加器有符号比较」分支的条件可以一一写出。再加上「溢出分支 BV」「I/O 状态分支 BIOZ」「辅助寄存器分支 BANZ」，就凑齐了全部条件分支。

BIOZ 用到的 `bio_n` 是引脚 `i_BIO_n` 的内部采样值，在 [src/IKA32010.sv:70-73](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L70-L73) 由 `cyc_pcen` 节拍锁存（同极性，`bio_n <= i_BIO_n`）。BIOZ 的语义是「BIO 输入为 0（低电平）则跳转」，对应源码 `bio_n == 1'b0`。

#### 4.2.3 源码精读

下面这张表把 10 条带条件的跳转（含 BANZ、B）逐一对应到源码行与表达式：

| 指令 | 操作码 | 跳转条件（源码表达式） | 依据 | 源码行 |
|------|--------|------------------------|------|--------|
| B | `0xF900` | 无条件（恒 `PC_LOAD_IMMEDIATE`） | — | [1193-1211](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1193-L1211) |
| BANZ | `0xF400` | `reg_ar[reg_arp][8:0] != 9'h000` | AR 低 9 位非 0 | [1213-1235](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1213-L1235) |
| BGEZ | `0xFD00` | `alu_flag_neg != 1'b1` | ACC ≥ 0 | [1238-1256](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1238-L1256) |
| BGZ | `0xFC00` | `(alu_flag_neg != 1) && (alu_flag_zero != 1)` | ACC > 0 | [1258-1276](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1258-L1276) |
| BIOZ | `0xF600` | `bio_n == 1'b0` | BIO 引脚低 | [1279-1297](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1279-L1297) |
| BLEZ | `0xFB00` | `alu_flag_neg == 1 \|\| alu_flag_zero == 1` | ACC ≤ 0 | [1300-1318](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1300-L1318) |
| BLZ | `0xFA00` | `alu_flag_neg == 1'b1` | ACC < 0 | [1320-1338](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1320-L1338) |
| BNZ | `0xFE00` | `alu_flag_zero != 1'b1` | ACC ≠ 0 | [1340-1358](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1340-L1358) |
| BV | `0xF500` | `alu_flag_ovfl == 1'b1` | 溢出（且清 V） | [1360-1379](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1360-L1379) |
| BZ | `0xFF00` | `alu_flag_zero == 1'b1` | ACC = 0 | [1381-1399](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1381-L1399) |

挑三条有代表性的看原文。**BGEZ**（[src/IKA32010.sv:1238-1256](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1238-L1256)）：

```systemverilog
if_pc_modesel = (alu_flag_neg != 1'b1) ? PC_LOAD_IMMEDIATE : PC_INCREASE;
```

**BV**（[src/IKA32010.sv:1360-1379](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1360-L1379)）多了一行——读溢出标志的同时把它清掉，这是 TMS32010 的约定（溢出分支一旦执行，V 位即被复位）：

```systemverilog
if_pc_modesel = (alu_flag_ovfl == 1'b1) ? PC_LOAD_IMMEDIATE : PC_INCREASE;
alu_v_rst = YES;   // 读完即清 V
```

**BANZ**（[src/IKA32010.sv:1213-1235](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1213-L1235)）是唯一「带寄存器副作用」的分支——它在判定的同时把当前辅助寄存器递减，常用于定长循环：

```systemverilog
//BANZ - Branch on auxillary register not zero
//See User Guide p3-16 (pdf p65) for the AR bits that are evaluated
if_pc_modesel = (reg_ar[reg_arp][8:0] != 9'h000) ? PC_LOAD_IMMEDIATE : PC_INCREASE;
ex_inst_cycle_rst = NO;
reg_ar_dec = YES;   // 同时递减当前 AR
```

注意 BANZ 判定的是**递减前**的 AR 当前值（`reg_ar_dec` 是寄存器控制信号，真正的递减发生在 `cyc_ncen` 边沿；而条件表达式此刻读的是尚未更新的旧值）。它对 AR 的低 9 位 `[8:0]` 判零，寻址只取低 8 位，第 8 位专供 BANZ 可见——这一点与 u2-l4 讲的辅助寄存器结构一致。具体的循环次数语义（初始化值与实际迭代次数的对应关系）建议对照 User Guide p3-16 在仿真里确认（待本地验证）。

#### 4.2.4 代码实践

**实践目标**：把 8 条「标志驱动」的条件分支（BGEZ/BGZ/BLEZ/BLZ/BNZ/BZ/BV/BIOZ）整理成一张「标志取值 → 是否跳转」的真值表，并核对源码。

**操作步骤**：

1. 仿照下表，列出每条指令在 N、Z、V、BIO 四个信号各种取值下的跳转结果。以 BGEZ/BGZ/BLEZ/BLZ 为例（仅依赖 N、Z）：

   | N | Z | BGEZ(≥0) | BGZ(>0) | BLZ(<0) | BLEZ(≤0) | BZ(=0) | BNZ(≠0) |
   |---|---|----------|---------|---------|----------|--------|---------|
   | 0 | 0 | 跳 | 跳 | 不跳 | 不跳 | 不跳 | 跳 |
   | 0 | 1 | 跳 | 不跳 | 不跳 | 跳 | 跳 | 不跳 |
   | 1 | 0 | 不跳 | 不跳 | 跳 | 跳 | 不跳 | 跳 |
   | 1 | 1 | 不跳 | 不跳 | 跳 | 跳 | 跳 | 不跳 |

   （N=1,Z=1 在补码里不会同时出现于单次运算结果，列出仅为真值完备。）

2. 对 BV 单独列表：V=1 → 跳（且执行后 V 被清 0）；V=0 → 不跳。
3. 对 BIOZ 单独列表：`bio_n==0`（即 `i_BIO_n` 为低）→ 跳；否则不跳。
4. 逐行回到 4.2.3 的源码表，把表格里的结论与源码三元表达式对照，确认完全一致。

**需要观察的现象**：BGEZ 与 BLZ 互为反跳（一个看 `!N`，一个看 `N`）；BZ 与 BNZ 互为反跳（`Z` vs `!Z`）；BGZ 与 BLEZ 把 Z 也纳入考量，因此不能只看 N。

**预期结果**：你的真值表与源码表达式逐位吻合。如果某格对不上，多半是把「>0」「≥0」的 Z 处理搞反了——回看本节开头的补码判别公式。

> 说明：本实践是「源码阅读型」，不需要运行仿真即可完成；若想动态验证，可在 testbench 里用 LACK/LAC 构造出特定的 ACC 值再执行条件分支，借助反汇编观察 PC 是否跳到目标（运行结果待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：用一条 ALU 指令 + 一条条件分支，实现「如果 ACC 等于 5 则跳转到 SUB」，该用哪条分支？需要先做什么？

**参考答案**：TMS32010 没有直接的「比较」指令，标准做法是先 `SUB` 一个立即数（这里需要把 5 放进某 RAM 单元或用其它方式减 5），让 ACC 变为 0 并更新 Z 标志，再用 `BZ SUB` 跳转。即「相减置 Z → 判零跳转」。注意 SUB 会改变 ACC，若还需保留原值要另作安排。

**练习 2**：BV 为什么要在译码里额外写一句 `alu_v_rst = YES`？

**参考答案**：V（溢出）标志是「粘性」的——一旦置位会一直保持，直到显式清除。如果 BV 读了 V 却不清，那么此后每次 BV 都会跳转，逻辑就错了。所以 TMS32010 约定：BV 一旦执行（无论是否跳转）就把 V 复位，源码用 `alu_v_rst = YES` 实现这一约定。

---

### 4.3 栈配合：CALL / CALA / RET 的调用与返回

#### 4.3.1 概念说明

子程序调用本质上 = 「跳转 + 把返回地址记下来」；返回 = 「按记下的地址跳回来」。IKA32010 用 u2-l6 讲过的四级硬件栈来「记地址」，并由三个微码信号驱动：`stk_push`（压栈）、`stk_pop`（弹栈）、`stk_data_sel`（压栈数据来源：PC 或写总线）。

栈子模块的实例化在 [src/IKA32010.sv:412-415](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L412-L415)，压栈数据由 `stk_data_sel` 在 `if_pc` 与 `reg_wrbus[11:0]` 间二选一：

```systemverilog
IKA32010_stack u_stack (
    .i_EMUCLK(i_EMUCLK), .i_RST_n(i_RS_n), .i_CEN(cyc_ncen),
    .i_PUSH(stk_push), .i_POP(stk_pop),
    .i_DIN(stk_data_sel ? if_pc : reg_wrbus[11:0]), .o_DOUT(stk_output)
);
```

三条指令的差异只在「目标地址从哪来」和「压/弹哪个方向」：

| 指令 | 操作码 | 目标地址来源 | 栈操作 | cycle0 总线事务 |
|------|--------|--------------|--------|-----------------|
| CALL（直接调用） | `0xF800` | 程序字（外部总线） | 压 PC | `DATA_READ`（读目标字） |
| CALA（间接调用） | `0x7F8C` | 累加器 ACC | 压 PC | `BUSCTRL_STOP`（不读总线） |
| RET（返回） | `0x7F8D` | 栈顶 | 弹栈 → PC | `BUSCTRL_STOP` |

CALL 和 CALA 都用 `stk_push=YES; stk_data_sel=STACK_DATA_PC` 把返回地址压栈；RET 用 `stk_pop=YES` 弹栈，并通过 `WRBUS_SOURCE_STACK` 把栈顶送上写总线、再用 `PC_LOAD_WRBUS` 装进 PC。栈宽 12 位，正好等于 PC 宽度，所以返回地址无损保存（这也是 PUSH/POP 累加器会被截断高 4 位、而 CALL/RET 不会的原因——见 u2-l6）。

#### 4.3.2 核心流程

**CALL（直接调用）** 与条件分支几乎相同，只多了「压栈」：

```text
cycle0:  DATA_READ@if_pc  → 读出目标地址字
         PC_LOAD_IMMEDIATE → PC ← 目标
         stk_push=YES, stk_data_sel=PC → 压入返回地址（当前 if_pc）
cycle1:  OPCODE_READ@新PC → 取子程序首条指令
```

**CALA（间接调用）** 的目标不在程序字里，而在累加器中，所以 `cycle0` 不读总线（`BUSCTRL_STOP`），而是把 ACC 经「移位器 B」送上写总线、再装载进 PC：

```text
cycle0:  BUSCTRL_STOP（不访问外部总线）
         register_wrbus_source_sel = WRBUS_SOURCE_SHB, shb_mux=LOW → 写总线 ← ACC 低字
         PC_LOAD_WRBUS → PC ← reg_wrbus[11:0] = ACC[11:0]
         stk_push=YES, stk_data_sel=PC → 压入返回地址
cycle1:  OPCODE_READ@新PC → 取子程序首条指令
```

其中 `shb_mux=LOW` 让移位器 B 输出取低字（`shb_output = alu_acc_output[15:0]`，见 [src/IKA32010.sv:463](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L463)），加上默认 `shb_amt=0`（不移位），最终 `PC ← ACC[11:0]`。

**RET（返回）** 把栈顶弹回 PC：

```text
cycle0:  BUSCTRL_STOP
         register_wrbus_source_sel = WRBUS_SOURCE_STACK → 写总线 ← {4'h0, 栈顶}
         PC_LOAD_WRBUS → PC ← 栈顶[11:0]
         stk_pop=YES → 栈整体上移
cycle1:  OPCODE_READ@新PC → 取返回处的指令
```

注意 RET 与 CALL/CALA 严格对称：CALL/CALA 压 `if_pc`、跳目标；RET 把栈顶（即当初压入的值）弹回 PC。三者都是两周期指令，`cycle1` 统一恢复取指。

> 关于压栈值：由于取指流水，执行某指令时 `if_pc` 已指向其后一个字，CALL/CALA 压入的正是此刻的 `if_pc`。它与 CALL 指令字之间的精确偏移，建议在仿真里直接观察 `stack[0]` 的值来确认（待本地验证）——本讲聚焦于「压 PC、弹 PC」的机制，这一机制在任何情况下都成立。

#### 4.3.3 源码精读

**CALL（直接调用）** 见 [src/IKA32010.sv:1422-1440](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1422-L1440)，与 B 的差别仅在多了 `stk_push=YES; stk_data_sel=STACK_DATA_PC`：

```systemverilog
//CALL - Call Subroutine Direct
16'b1111_1000_0000_0000: begin
    if(ex_inst_cycle == 2'd0) begin
        busctrl_req = DATA_READ; busctrl_addr_muxsel = BUSCTRL_ADDR_PC;
        if_pc_modesel = PC_LOAD_IMMEDIATE;
        ex_inst_cycle_rst = NO;
        if_opcodereg_force_iack = NO;
        stk_push = YES; stk_data_sel = STACK_DATA_PC;   // ← 压返回地址
    end
    else if(ex_inst_cycle == 2'd1) begin
        busctrl_req = OPCODE_READ; busctrl_addr_muxsel = BUSCTRL_ADDR_PC;
    end
    ...
end
```

**CALA（间接调用）** 见 [src/IKA32010.sv:1401-1420](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1401-L1420)。注意它用 `BUSCTRL_STOP` + `WRBUS_SOURCE_SHB` + `shb_mux=LOW` 把 ACC 送上写总线，再 `PC_LOAD_WRBUS` 装载：

```systemverilog
//CALA - Call Subroutine Indirect(from Accumulator)
16'b0111_1111_1000_1100: begin
    if(ex_inst_cycle == 2'd0) begin
        busctrl_req = BUSCTRL_STOP; busctrl_addr_muxsel = BUSCTRL_ADDR_PC;
        if_pc_modesel = PC_LOAD_WRBUS;                  // PC ← 写总线
        register_wrbus_source_sel = WRBUS_SOURCE_SHB; shb_mux = LOW;  // 写总线 ← ACC 低字
        ex_inst_cycle_rst = NO;
        if_opcodereg_force_iack = NO;
        stk_push = YES; stk_data_sel = STACK_DATA_PC;   // ← 压返回地址
    end
    else if(ex_inst_cycle == 2'd1) begin
        busctrl_req = OPCODE_READ; busctrl_addr_muxsel = BUSCTRL_ADDR_PC;
    end
    ...
end
```

写总线选源 MUX 在 [src/IKA32010.sv:133-144](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L133-L144)，注意 `WRBUS_SOURCE_STACK` 与 `WRBUS_SOURCE_SHB` 两条分支正是 RET/CALA 用到的：

```systemverilog
WRBUS_SOURCE_SHB     : reg_wrbus = shb_output;
WRBUS_SOURCE_RAM     : reg_wrbus = ram_output;
WRBUS_SOURCE_STACK   : reg_wrbus = {4'h0, stk_output};   // ← RET：栈顶零扩展到 16 位
```

**RET（返回）** 见 [src/IKA32010.sv:1442-1461](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1442-L1461)。它与 CALA 的 `cycle0` 几乎镜像，只是把「压栈 + 写总线取 ACC」换成「弹栈 + 写总线取栈顶」：

```systemverilog
//RET - Return from Subroutine
16'b0111_1111_1000_1101: begin
    if(ex_inst_cycle == 2'd0) begin
        busctrl_req = BUSCTRL_STOP; busctrl_addr_muxsel = BUSCTRL_ADDR_PC;
        if_pc_modesel = PC_LOAD_WRBUS;                   // PC ← 写总线（=栈顶）
        register_wrbus_source_sel = WRBUS_SOURCE_STACK;  // 写总线 ← 栈顶
        ex_inst_cycle_rst = NO;
        if_opcodereg_force_iack = NO;
        stk_push = NO; stk_pop = YES; stk_data_sel = STACK_DATA_ACC;  // ← 弹栈
    end
    else if(ex_inst_cycle == 2'd1) begin
        busctrl_req = OPCODE_READ; busctrl_addr_muxsel = BUSCTRL_ADDR_PC;
    end
    ...
end
```

可以看到 RET 与 CALL/CALA 完美对称：调用方 `stk_push=PC` + 跳目标，返回方 `stk_pop` + 把同一个值装回 PC。栈深只有 4 级（u2-l6），所以子程序嵌套不能超过 4 层，否则栈底的返回地址会被新压入的数据挤丢。

#### 4.3.4 代码实践

**实践目标**：用源码追踪法把一次「CALL → 子程序 → RET」的执行分解成逐相位表，看清栈与 PC 如何协作。

**操作步骤**：

1. 假设程序布局：`@K` 是 CALL 操作码，`@K+1` 是目标地址字（目标 = `@SUB`），子程序首条指令在 `@SUB`，子程序末尾是 RET（`@R`，单字指令）。
2. 按下表把每个相位的 4 个关键信号填出来（答案见「预期结果」）：

   | 时刻 | 指令/相位 | `busctrl_req` | `if_pc_modesel` | `stk_push/pop` | `ex_inst_cycle_rst` |
   |------|-----------|---------------|-----------------|----------------|---------------------|
   | CALL cycle0 | CALL @K | ? | ? | ? | ? |
   | CALL cycle1 | CALL @K | ? | （默认） | （默认） | （默认） |
   | … 子程序执行 … | | OPCODE_READ | PC_INCREASE | — | YES |
   | RET cycle0 | RET @R | ? | ? | ? | ? |
   | RET cycle1 | RET @R | ? | （默认） | （默认） | （默认） |

3. 同时记录每个相位结束时 `if_pc` 与栈顶 `stack[0]` 的变化。

**需要观察的现象**：CALL 的 `cycle0` 同时发生「读目标字」「PC 装载目标」「栈压入返回地址」三件事；RET 的 `cycle0` 同时发生「栈顶送上写总线」「PC 装载栈顶」「栈弹出」三件事。两者都在 `cycle1` 用 `OPCODE_READ` 恢复取指。

**预期结果**（关键信号）：

| 时刻 | `busctrl_req` | `if_pc_modesel` | 栈操作 | `ex_inst_cycle_rst` |
|------|---------------|-----------------|--------|---------------------|
| CALL cycle0 | `DATA_READ` | `PC_LOAD_IMMEDIATE` | `push`（PC） | `NO` |
| CALL cycle1 | `OPCODE_READ` | 默认 `PC_INCREASE` | 默认无 | 默认 `YES` |
| RET cycle0 | `BUSCTRL_STOP` | `PC_LOAD_WRBUS` | `pop` | `NO` |
| RET cycle1 | `OPCODE_READ` | 默认 `PC_INCREASE` | 默认无 | 默认 `YES` |

> 说明：本实践为源码阅读型，无需运行即可完成。若要在仿真中动态确认 `stack[0]` 在每一步的实际值（进而确认返回地址的精确偏移），可借助反汇编或波形观察，运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：CALA 与 CALL 都压返回地址，为什么 CALA 的 `cycle0` 用 `BUSCTRL_STOP` 而 CALL 用 `DATA_READ`？

**参考答案**：CALL 是「直接调用」，目标地址存放在程序字里（CALL 操作码的下一个字），必须发起 `DATA_READ` 从外部总线把它读进来。CALA 是「间接调用」，目标地址已经在累加器 ACC 里，不需要访问外部总线，所以用 `BUSCTRL_STOP` 停掉总线事务，直接经移位器 B 把 ACC 送上写总线、装载进 PC。

**练习 2**：如果一个程序嵌套调用了 5 层子程序（第 5 层 CALL 时栈里已有 4 个返回地址），会发生什么？

**参考答案**：栈只有 4 级，且是「移位式」结构——第 5 次压栈时，原本栈底（`stack[3]`）的返回地址会被新数据挤出丢失。等最外层子程序 RET 时，栈里已没有正确的返回地址，程序会跳到错误位置。因此 TMS32010 程序必须自行保证子程序嵌套不超过 4 层（详见 u2-l6）。

**练习 3**：RET 的 `stk_data_sel` 写成 `STACK_DATA_ACC`（而不是 `STACK_DATA_PC`），有影响吗？

**参考答案**：没有影响。`stk_data_sel` 只在 `stk_push=YES` 时决定压栈数据来源；RET 是 `stk_pop=YES`、`stk_push=NO`，根本不压栈，`stk_data_sel` 的取值不会被使用。这里写成 `STACK_DATA_ACC` 只是沿用默认值的写法（默认 `stk_data_sel=STACK_DATA_PC` 之外，作者统一把弹栈场景标记为 ACC 以示「不压」），对行为无影响。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「读码 + 造表」的综合任务：

**任务**：下面是一段假想的 TMS32010 程序片段（仅用于读码练习，非项目自带代码）：

```text
@000:  LACK   0          ; ACC <- 0   （Z=1, N=0）
@001:  BGEZ   @010       ; 条件分支
@003:  LACK   9          ; 不会被命中
...
@010:  CALL   @020       ; 调用子程序
@012:  LACK   7          ; 返回点
...
@020:  LACK   3          ; 子程序入口
@021:  RET              ; 返回
```

请回答：

1. 执行到 `@001` 的 BGEZ 时，N=0、Z=1。根据 4.2 节的真值表，它会跳吗？跳到哪？
2. 程序最终执行 `@012` 的 LACK 7，这中间 CALL 与 RET 各经历了哪两个相位？栈顶在 CALL 的 `cycle0` 之后保存了什么值？RET 又如何把这个值送回 PC？
3. 如果把 `@001` 改成 `BLZ`（ACC<0 才跳），同样 N=0、Z=1 的条件下会跳吗？

**参考答案**：

1. BGEZ 的条件是 `alu_flag_neg != 1`，即 `!N`。N=0 → 条件为真 → 跳转到 `@010`。`@003` 不会被执行。
2. CALL @010 是两周期指令：`cycle0` 用 `DATA_READ` 读出目标地址字（`@020`），`PC_LOAD_IMMEDIATE` 把 PC 装载为 `@020`，同时 `stk_push=PC` 把返回地址压入栈顶；`cycle1` 在 `@020` 处 `OPCODE_READ` 取子程序首条指令。子程序末尾 RET @021 也是两周期：`cycle0` 用 `WRBUS_SOURCE_STACK` 把栈顶送上写总线，`PC_LOAD_WRBUS` 把它装回 PC，同时 `stk_pop` 弹栈；`cycle1` 在返回点 `OPCODE_READ` 恢复取指，于是执行到 `@012`。（栈顶在 CALL 后保存的是当时压入的 `if_pc`；其相对 CALL 指令字的精确偏移可在仿真中确认——待本地验证。）
3. BLZ 的条件是 `alu_flag_neg == 1`，即 `N`。N=0 → 条件为假 → 不跳，顺序执行 `@003`。

这个练习同时用到了「条件判断（4.2）」「两相位结构（4.1）」和「CALL/RET 栈配合（4.3）」三部分知识。

## 6. 本讲小结

- 分支与子程序类指令（B/BANZ/BGEZ/BGZ/BIOZ/BLEZ/BLZ/BNZ/BV/BZ/CALA/CALL/RET）**全部是两周期指令**，共用 `if(ex_inst_cycle==2'd0) … else if(==2'd1) …` 模板：`cycle0` 读目标/做判定/压栈，`cycle1` 用 `OPCODE_READ` 恢复取指。
- 两周期源自「分支/调用是两字指令」：第一个字是操作码，第二个字是 12 位目标地址，必须多花一拍从程序空间读进来。
- 条件分支的判定**只是一行三元表达式** `(条件)?PC_LOAD_IMMEDIATE:PC_INCREASE`；条件直接复用 ALU 的 N/Z/V 标志、`bio_n` 或辅助寄存器值，不需要额外运算。
- 8 条标志分支可由补码判别公式一一推出：`<0`↔N、`>0`↔¬N∧¬Z、`≥0`↔¬N、`≤0`↔N∨Z、`=0`↔Z、`≠0`↔¬Z；BV 额外在读 V 后清 V。
- BANZ 是唯一带寄存器副作用的分支：判定 AR 低 9 位非 0 的同时把当前 AR 递减，专用于定长循环。
- CALL/CALA 都用 `stk_push=PC` 压返回地址再跳转，RET 用 `stk_pop` + `WRBUS_SOURCE_STACK` + `PC_LOAD_WRBUS` 把栈顶弹回 PC；三者在 `cycle0` 把 `if_opcodereg_force_iack=NO` 以保证两相位原子执行、不被中断打断。
- 栈只有 4 级，子程序嵌套不得超过 4 层；栈宽 12 位等于 PC 宽度，所以调用/返回地址无损保存。

## 7. 下一步学习建议

- 下一讲 [u3-l7（乘法器与 I/O/数据存储类指令译码）](u3-l7-multiplier-and-io-instructions.md) 会剖析另一组多周期指令（IN/OUT/TBLR/TBLW），它们同样使用 `ex_inst_cycle` 分相位，但相位数更多（如 TBLR 三周期），可与本讲的「两相位」对照阅读，加深对多周期状态机的理解。
- 想进一步确认本讲里标注「待本地验证」的细节（BANZ 循环次数语义、CALL 压栈值的精确偏移），建议结合 [u1-l5（仿真与 testbench）](u1-l5-simulation-and-testbench.md) 自行编写最小 testbench，喂入 B/CALL/RET 序列，用反汇编（`disasm_type4`）或波形观察 `if_pc`、`stack[0]`、`ex_inst_cycle` 的变化。
- 若想从「单条指令」上升到「整段程序的时序」，可回看 [u3-l2（多周期指令时序与状态机）](u3-l2-multicycle-timing-and-state.md)，把本讲的相位分解放回 `ex_state` 状态机与复位延迟链的整体框架里。
