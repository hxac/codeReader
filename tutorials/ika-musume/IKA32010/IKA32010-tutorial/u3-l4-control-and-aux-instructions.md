# 控制类与辅助寄存器类指令译码

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 IKA32010 的状态位（V / OVM / INTM / ARP / DP）**不是**一个整体寄存器，而是散落在各处的 1 位触发器，并统一通过「`{set, rst}` 真值表」在 `cyc_ncen` 拍写入。
- 逐条讲出控制类指令（`NOP / DINT / EINT / SOVM / ROVM / LST / SSR / PUSH / POP`）的微码译码：哪些只翻一个状态位、哪些做状态寄存器整体读写、哪些是两周期栈操作。
- 逐条讲出辅助寄存器类指令（`LAR / LARK / SAR / LDP / LDPK / MAR(LARP) / MAR(NOP)`）的微码译码，并能区分「由指令字 bit8 显式指定目标 AR」与「由 ARP 隐式选择当前 AR」两条寻址路径。
- 把 LST 与 SSR 放进 [u3-l1（微码架构）](u3-l1-microcode-architecture.md) 讲过的「默认值 + `casez` 覆盖」框架里，看懂这两条指令如何复用 `reg_wrbus` 与 `flag_output` 完成状态映像的读写。
- 独立核对源码与官方手册对状态位读写的描述是否一致（这是本讲综合实践的核心任务）。

本讲是专家层第四讲，承接 [u3-l1（微码架构总览）](u3-l1-microcode-architecture.md)、[u2-l4（辅助寄存器 ARP/AR 与 DP）](u2-l4-aux-registers-and-dp.md) 与 [u2-l6（硬件堆栈）](u2-l6-hardware-stack.md)，并引用 [u3-l2（多周期指令时序）](u3-l2-multicycle-timing-and-state.md) 中关于 `ex_inst_cycle` 两周期分解的内容。如果你还不熟悉「水平微码默认值」「`cyc_ncen` 主工作拍」「栈的 push/pop 数据选择」，建议先读这三篇。

## 2. 前置知识

### 2.1 状态位：处理器自我描述的「开关组」

DSP 在执行指令时会产生并依赖一组**状态位（status bits / flags）**。TMS32010（也就是 IKA32010 复刻的对象）的状态位有：

| 状态位 | 含义 | 由谁修改 |
|---|---|---|
| `V`（overflow） | 有符号算术溢出标志，1 表示最近一次算术运算发生溢出 | 算术指令自动置位；`LST` 从内存恢复 |
| `OVM`（overflow mode） | 溢出饱和模式开关，1 表示算术溢出时把累加器钳位到极值 | `SOVM / ROVM / LST` |
| `INTM`（interrupt mode） | 中断总开关，1 表示中断被禁止（**注意是 1=禁止**） | `EINT / DINT / 复位` |
| `ARP`（auxiliary register pointer） | 当前使用哪个辅助寄存器（0 或 1） | 间接寻址指令、`LARP / LST` |
| `DP`（data page pointer） | 数据 RAM 的页指针（RAM 地址最高位） | `LDP / LDPK / LST` |

很多资料会把它们笼统称为「状态寄存器」，但**IKA32010 并没有用一个 16 位寄存器把它们物理地装在一起**——它们是分散在源码不同位置的独立触发器（`reg_ovm`、`reg_intm`、`reg_arp`、`reg_dp`，外加 ALU 内部的 `V` 位）。只有在执行 `SSR`/`LST` 这类需要把状态当作一个「16 位字」整体读写的指令时，硬件才用一个组合连线 `flag_output` 把它们临时拼接成一个字。

### 2.2 set / rst：用「两个写请求信号」改一个触发器

要改一个 1 位触发器，最朴素的写法是 `reg <= new_value;`。但在微码风格里，更常见的做法是给每个状态位配**一对控制信号** `{set, rst}`，由微码组合地驱动：

| `{set, rst}` | 下一个值 | 含义 |
|---|---|---|
| `2'b10` | `1` | 置位（set） |
| `2'b01` | `0` | 复位（rst） |
| `2'b00` 或 `2'b11` | 保持原值 | hold |

用布尔式表达（`set` 优先级最高）：

\[
X_{\text{next}} = \text{set} \;\vee\; (\lnot\,\text{rst} \;\wedge\; X)
\]

这种写法的好处是：**「写成什么值」由微码决定，「什么时候写」统一交给 `cyc_ncen` 拍**。微码只需把 `{set, rst}` 置成想要的那一档，剩下的交给时序逻辑。本讲的 `SOVM/ROVM`、`DINT/EINT`、`LDP`、`LARP`、`LST` 全都建立在这套机制上。

### 2.3 复习：两条寻址路径与 bit8 的双重身份

来自 [u2-l4](u2-l4-aux-registers-and-dp.md)：

- **ARP 隐式选择当前 AR**：`ar_addr_output = reg_ar[reg_arp][7:0]`，间接寻址时 RAM 地址取自「ARP 当前指向的」那个 AR。
- **指令字 bit8 显式指定 AR**：辅助寄存器类指令（`LAR/LARK/SAR`）用 `if_opcodereg[8]` 直接点名操作 AR0 还是 AR1，与 ARP 无关。

记住这个区分，本讲 4.3 会反复用到：**寻址用 ARP，装载/存储用 bit8**。

### 2.4 复习：默认值 + casez 覆盖

来自 [u3-l1](u3-l1-microcode-architecture.md)：那个巨大的 `always @(*)` 微码块先给所有控制信号赋默认值（描述「读 RAM、做加法但不写回、PC 自增、取下一条」这条最常见通路），再用 `casez(if_opcodereg)` 里的阻塞赋值 `=` 覆盖。本讲涉及的绝大多数控制/辅助寄存器指令都**只覆盖少数几个信号**——这正是「默认值」设计的价值。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/IKA32010.sv](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv) | 唯一的硬件源文件。本讲关注三处：①状态位触发器的 `{set,rst}` 写入逻辑（约 263–343 行）；②`flag_output` 状态映像拼接（479 行）与 `reg_wrbus` 选源 MUX（131–144 行）；③微码 `casez` 中的「CONTROL INSTRUCTIONS」段（620–768 行）与「AUXILLARY REGISTER AND DATA POINTER INSTRUCTIONS」段（1088–1184 行）。 |
| [src/IKA32010_mnemonics.sv](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv) | 常量字典。本讲用到 `WRBUS_SOURCE_*`（写总线选源）、`STACK_DATA_*`（栈数据来源）、`YES/NO`、`ALU_ADD` 等。 |

> 提示：源码注释把存储状态寄存器的指令写作 **SSR**（Store Status Register），而 TI 官方手册中它叫 **SST**；`MAR` 在源码里被拆成 `MAR(LARP)` 与 `MAR(NOP)` 两种编码。这些命名差异在 [u1-l2](u1-l2-repo-layout-and-docs.md) 里已经指出，本讲对照时请留意。

## 4. 核心概念与源码讲解

### 4.1 状态位的统一写入：set / rst 真值表

#### 4.1.1 概念说明

本模块回答一个问题：**微码到底怎样「改」一个状态位？**

IKA32010 没有像 `reg_ar[arp] <= ...` 那样直接对状态位赋值，而是给 `reg_ovm`、`reg_intm`、`reg_arp`、`reg_dp` 各配了一对 `{set, rst}` 信号，由微码组合驱动；`V` 位虽然藏在 ALU 子模块内部，但同样通过 `alu_v_set`、`alu_v_rst` 由微码驱动。这样设计有两个好处：

1. **解耦「写什么」与「何时写」**：微码只管把 `{set,rst}` 钉到目标档位，`cyc_ncen` 边沿统一完成实际写入。
2. **天然支持「保持」**：默认值 `{NO,NO}` 就是 hold，所以「不涉及某个状态位的指令」连写都不用写，沿用默认即可——这与 [u3-l1](u3-l1-microcode-architecture.md) 的默认值哲学一致。

#### 4.1.2 核心流程

每个状态位 `X` 的更新遵循同一个真值表（`set` 优先于 `rst`）：

```
在 cyc_ncen 上升沿：
    case({X_set, X_rst})
        2'b10:  X <= 1'b1;        // set
        2'b01:  X <= 1'b0;        // rst
        default:X <= X;           // hold (含 00 与 11)
    endcase
```

五类状态位的归属：

| 状态位 | 触发器名 | 写入信号 | 主修改者 |
|---|---|---|---|
| OVM | `reg_ovm` | `reg_ovm_set / reg_ovm_rst` | `SOVM / ROVM / LST` |
| INTM | `reg_intm` | `reg_intm_en / reg_intm_dis` | `EINT / DINT`（命名是 en/dis，但语义同 set/rst） |
| ARP | `reg_arp` | `reg_arp_set / reg_arp_rst` | 间接寻址片段、`LARP / LST` |
| DP | `reg_dp` | `reg_dp_set / reg_dp_rst` | `LDP / LDPK / LST` |
| V | ALU 内部 | `alu_v_set / alu_v_rst` | 算术指令、`LST` |

#### 4.1.3 源码精读

**OVM——最干净的 set/rst 范例**。[src/IKA32010.sv:263-271](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L263-L271)：

```verilog
reg reg_ovm = 1'b0; //0 = overflow disabled, 1 = overflow enabled
reg reg_ovm_set, reg_ovm_rst;
always @(posedge i_EMUCLK) if(cyc_ncen) begin
    case({reg_ovm_set, reg_ovm_rst})
        2'b01: reg_ovm <= 1'b0;
        2'b10: reg_ovm <= 1'b1;
        default: reg_ovm <= reg_ovm;
    endcase
end
```

注意注释里的警告：`RESET will not clear this bit!!!`——`reg_ovm` 没有 `if(!i_RS_n)` 分支，复位不影响它；只有 `SOVM/ROVM/LST` 能改。

**INTM——同名异号**。[src/IKA32010.sv:274-286](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L274-L286) 用的是 `reg_intm_en / reg_intm_dis`（enable / disable），真值表里 `2'b10→0`（开中断）、`2'b01→1`（关中断）。**含义与 OVM 相反**：这里 `set` 对应的是数值 `0`。这是因为 INTM 的语义是「1=禁止中断」，所以「enable 中断」等于把它清 0。复位移位 `if(!i_RS_n) reg_intm <= 1'b1;` —— 复位默认**关中断**（详见 [u3-l3](u3-l3-interrupt-mechanism.md)）。

**ARP / DP** 用的是与 OVM 完全相同的 `2'b10/2'b01/default` 模板：[src/IKA32010.sv:310-314](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L310-L314)（ARP）、[src/IKA32010.sv:337-341](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L337-L341)（DP）。DP 块的复位写法是 `if(i_RS_n)` 而非 `if(!i_RS_n)`，极性与 ARP/AR 块相反，这一点在 [u2-l4](u2-l4-aux-registers-and-dp.md) 已经标记为「待本地验证」，此处先按下不表。

**V 位——藏在 ALU 里**。`V` 不在顶层，而是 ALU 子模块的内部寄存器，顶层通过端口 `i_ALU_V_SET / i_ALU_V_RST` 驱动它：[src/IKA32010.sv:454-455](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L454-L455)。算术指令在 ALU 内部自动置位；而 `LST` 则通过拉 `alu_v_set/alu_v_rst` 从内存映像恢复它（见 4.2）。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `{set,rst}` 真值表在仿真里生效，并验证「复位不清 OVM」。

**操作步骤（源码阅读 + 局部加打印）**：

1. 打开 [src/IKA32010.sv:263-271](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L263-L271)，确认 `reg_ovm` 没有 `if(!i_RS_n)` 复位分支。
2. 在 `always @(posedge i_EMUCLK) if(cyc_ncen)` 块的 `case` 之后临时加一行（仅用于本练习，**练习结束后还原**）：

   ```verilog
   $display("t=%0t OVM=%b ovm_set=%b ovm_rst=%b", $time, reg_ovm, reg_ovm_set, reg_ovm_rst);
   ```

3. 用现有 testbench 跑一段包含 `SOVM`（0x7F8B）和 `ROVM`（0x7F8A）的程序。

**需要观察的现象**：

- `SOVM` 执行周期内 `ovm_set=1, ovm_rst=0`，**下一个** `cyc_ncen` 拍 `OVM` 变成 `1`。
- `ROVM` 执行周期内 `ovm_set=0, ovm_rst=1`，下一拍 `OVM` 变成 `0`。
- 两次复位 `i_RS_n` 拉低再松开后，`OVM` 保持拉低前的值（而非被清成 0）。

**预期结果**：`{set,rst}` 与 `OVM` 的关系严格符合 4.1.2 的真值表；复位确不清 OVM。若无法本地运行仿真，明确标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`{set,rst} = 2'b11`（同时 set 与 rst）在 IKA32010 里会得到什么结果？为什么微码里可以忽略这种情况？

**答案**：落入 `default` 分支，保持原值。因为微码每个周期都从默认值 `2'b00` 起步、再按需覆盖，永远不会写出 `2'b11`，所以「11=hold」只是 defensively 的兜底。

**练习 2**：`EINT` 要把 `reg_intm` 写成 `0`（开中断），它应该驱动 `reg_intm_en / reg_intm_dis` 中的哪一个？对应的真值表档位是？

**答案**：驱动 `reg_intm_en=YES`（即 `{en,dis}=2'b10`），对应真值表里 `2'b10 → reg_intm <= 1'b0`。注意 INTM 的命名与数值方向与 OVM 相反。

---

### 4.2 控制类指令 casez 分支

#### 4.2.1 概念说明

控制类指令的共性是：**不搬运数据通路上的数据**（不读写 RAM、不动 AR 数据流），只做三件事之一——翻转某个状态位、把状态映像整体读/写、或在累加器与硬件栈之间搬一次。它们集中住在 opcode 空间的 `0x7F8x / 0x7F9x / 0x7Bxx / 0x7Cxx` 一带，是微码 `casez` 里紧跟内部 `IACK` 之后的第一组：

```
NOP   0x7F80     DINT  0x7F81     EINT  0x7F82
SOVM  0x7F8B     ROVM  0x7F8A
LST   0x7Bxx     SSR   0x7Cxx     (低 8 位是寻址操作数)
PUSH  0x7F9C     POP   0x7F9D
```

可以分成三个小组：

- **(a) 单信号翻转**：`NOP`（什么都不做）、`DINT/EINT`（翻 INTM）、`SOVM/ROVM`（翻 OVM）。
- **(b) 状态寄存器整体读写**：`LST`（从 RAM 读状态映像写回各位）、`SSR`（把各位拼成映像写进 RAM）。
- **(c) 累加器 ↔ 栈**：`PUSH / POP`，两条两周期指令。

一个值得记住的事实：`NOP` 的操作码 `0x7F80` 恰好是指令寄存器 `if_opcodereg` 的复位初值（[src/IKA32010.sv:180](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L180)）。也就是说，复位后第一个被译码的「指令」天然就是 NOP，核心得以安全地空转。

#### 4.2.2 核心流程

**(a) 单信号翻转**：指令体只写一个 `reg_*_set = YES` 或 `reg_*_rst = YES`，其余全部沿用默认值（`PC_INCREASE` + `OPCODE_READ`）。整条指令就是「翻一个位、取下一条」。

**(b1) SSR（存状态映像）**：

```
默认 reg_wrbus_source_sel = WRBUS_SOURCE_RAM
SSR 改写：
    reg_wrbus_source_sel = WRBUS_SOURCE_FLAG   // reg_wrbus = flag_output（16 位状态映像）
    ram_wr = YES                                // 把 reg_wrbus 写进 RAM[ram_addr]
（若 bit7=1 间接寻址）顺带做 AR inc/dec/ARP 副作用
```

**(b2) LST（读状态映像）**：

```
默认 reg_wrbus_source_sel = WRBUS_SOURCE_RAM   // reg_wrbus = RAM[ram_addr]
LST 把 reg_wrbus 的若干位拆回到各状态位：
    reg_wrbus[15] → V      (alu_v_set / alu_v_rst)
    reg_wrbus[14] → OVM    (reg_ovm_set / reg_ovm_rst)
    reg_wrbus[ 0] → DP     (reg_dp_set  / reg_dp_rst)
    ARP 分两种情况：
        bit7=0（直接寻址）：reg_wrbus[8] → ARP
        bit7=1（间接寻址）：由指令字 bit0 → ARP（走间接寻址片段）
```

注意 LST **不动 INTM**——这是官方手册的明确规定，详见 4.2.4 的实践。

**(c) PUSH / POP**：两周期，结构对称，借用 [u3-l2](u3-l2-multicycle-timing-and-state.md) 讲过的 `ex_inst_cycle`：

```
cycle 0：BUSCTRL_STOP + PC_HOLD（不取指、PC 不动），做实际的 push/pop；ex_inst_cycle_rst=NO 推进
cycle 1：恢复 OPCODE_READ，取下一条；ex_inst_cycle_rst 沿用默认 YES，自终止
```

#### 4.2.3 源码精读

**状态映像的拼接 `flag_output`**。[src/IKA32010.sv:479](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L479)：

```verilog
assign flag_output = {alu_flag_ovfl, reg_ovm, reg_intm, 4'b1111,
                      reg_arp, 6'b111111, 1'b1, reg_dp}; //bit1 is don't care
```

位布局（这是 SSR 写出去、LST 读回来的「状态字」格式）：

| bit | 15 | 14 | 13 | 12–9 | 8 | 7–2 | 1 | 0 |
|---|---|---|---|---|---|---|---|---|
| 内容 | V | OVM | INTM | 保留(1) | ARP | 保留(1) | 不关心 | DP |

`flag_output` 经写总线选源 MUX 的 `WRBUS_SOURCE_FLAG` 分支送上 `reg_wrbus`：[src/IKA32010.sv:140](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L140)。

**单信号翻转指令**。`DINT / EINT`（[src/IKA32010.sv:646-661](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L646-L661)）、`SOVM / ROVM`（[src/IKA32010.sv:735-750](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L735-L750)）、`NOP`（[src/IKA32010.sv:639-643](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L639-L643)）的指令体里**除了反汇编 `disasm_*` 调用，只有一行**真正起作用的赋值，例如：

```verilog
//DINT
16'b0111_1111_1000_0001: begin
    reg_intm_dis = YES;            // 关中断：reg_intm <= 1
    ...
//SOVM
16'b0111_1111_1000_1011: begin
    reg_ovm_set = YES;             // 开溢出饱和：reg_ovm <= 1
```

这正是「默认值 + 按需覆盖」的最纯粹体现。

**SSR（存状态映像到 RAM）**。[src/IKA32010.sv:753-768](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L753-L768)：

```verilog
//SSR - Store status register
16'b0111_1100_????_????: begin
    register_wrbus_source_sel = WRBUS_SOURCE_FLAG;  // reg_wrbus = flag_output
    ram_wr = YES;                                   // 写 RAM[ram_addr]
    if(if_opcodereg[7]) begin                       // 间接寻址副作用（可选）
        reg_ar_inc = if_opcodereg[5]; reg_ar_dec = if_opcodereg[4];
        if(!if_opcodereg[3]) begin
            if(if_opcodereg[0]) reg_arp_set = YES;
            else                reg_arp_rst = YES;
        end
    end
```

关键两行：把写总线切到 `WRBUS_SOURCE_FLAG`，再开 `ram_wr`。`ram_addr` 由默认的直接/间接寻址逻辑生成（见 [u2-l5](u2-l5-data-ram-and-addressing.md)）。

**LST（从 RAM 读状态映像）**。[src/IKA32010.sv:664-688](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L664-L688)：

```verilog
//LST
16'b0111_1011_????_????: begin
    if(reg_wrbus[15]) alu_v_set   = YES; else alu_v_rst   = YES; // V
    if(reg_wrbus[14]) reg_ovm_set = YES; else reg_ovm_rst = YES; // OVM
    if(reg_wrbus[0])  reg_dp_set  = YES; else reg_dp_rst  = YES; // DP
    if(if_opcodereg[7]) begin            // 间接寻址：ARP 由指令字控制
        reg_ar_inc = if_opcodereg[5]; reg_ar_dec = if_opcodereg[4];
        if(!if_opcodereg[3]) begin
            if(if_opcodereg[0]) reg_arp_set = YES;
            else                reg_arp_rst = YES;
        end
    end
    else begin                           // 直接寻址：ARP 由映像字 bit8 控制
        if(reg_wrbus[8]) reg_arp_set = YES;
        else             reg_arp_rst = YES;
    end
```

LST 没有改写 `reg_wrbus_source_sel`，所以 `reg_wrbus` 取默认值 `WRBUS_SOURCE_RAM`——也就是 `RAM[ram_addr]`。它把 RAM 字的若干位「拆」回各状态位。**仔细数一遍会发现：没有 INTM。** 这不是 bug，而是与官方手册一致的语义（INTM 只能由 `EINT/DINT/复位` 改变）。

**PUSH / POP 的两周期分解**。以 POP（[src/IKA32010.sv:691-711](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L691-L711)）为例：

```verilog
//POP
16'b0111_1111_1001_1101: begin
    if(ex_inst_cycle == 2'd0) begin
        busctrl_req = BUSCTRL_STOP; if_pc_modesel = PC_HOLD;   // 不取指、PC 停
        register_wrbus_source_sel = WRBUS_SOURCE_STACK;        // 栈顶 → reg_wrbus
        alu_modesel = ALU_ADD; alu_paz = YES;                  // A 口清零：ACC ← 0 + 栈值
        alu_pbdata = ALU_PBDATA_LOWWORD; alu_acc_ld = YES;
        ex_inst_cycle_rst = NO;                                // 推进到 cycle 1
        if_opcodereg_force_iack = NO;                          // 原子性：期间不响应中断
        stk_pop = YES;
    end
    else if(ex_inst_cycle == 2'd1) begin
        busctrl_req = OPCODE_READ;                             // 恢复取指
    end
```

PUSH（[src/IKA32010.sv:714-732](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L714-L732)）是对称的：cycle 0 用 `WRBUS_SOURCE_SHB`（累加器经移位器 B）压栈，`stk_push = YES; stk_data_sel = STACK_DATA_ACC;`。注意 [u2-l6](u2-l6-hardware-stack.md) 讲过栈宽只有 12 位，故 `PUSH/POP` 累加器会截断高 4 位。两周期里都写了 `if_opcodereg_force_iack = NO`，含义见 [u3-l2](u3-l2-multicycle-timing-and-state.md)：让指令寄存器在 cycle 0 保持不变，使同一 `casez` 分支在 cycle 1 再次命中——这就是「自终止」的两周期结构。

#### 4.2.4 代码实践

**实践目标**：验证 `SSR` 写进 RAM 的「状态字」与 4.2.3 的位布局一致，并确认 `LST` 不恢复 INTM。

**操作步骤（源码阅读型 + 思维实验）**：

1. 假设执行 `SSR` 之前各状态位为：`V=1, OVM=1, INTM=0, ARP=0, DP=1`。
2. 按 [src/IKA32010.sv:479](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L479) 的拼接式手算 `flag_output`：

   \[
   \text{flag\_output} = \{\,1,\ 1,\ 0,\ 1111,\ 0,\ 111111,\ 1,\ 1\,\}
   \]

   即 `1100_1111_0011_1111` = `0xCF3F`。
3. 写入 RAM 某地址后，再用 `LST` 从同一地址读回，逐位核对：V、OVM、DP、ARP 是否被还原？INTM 呢？

**需要观察的现象**：`SSR` 写入的 RAM 字应为 `0xCF3F`；随后 `LST` 会还原 `V=1, OVM=1, DP=1`、直接寻址下 `ARP=0`，但 **INTM 不变**（无论 RAM 字的 bit13 是什么）。

**预期结果**：手算值 `0xCF3F`；INTM 在 LST 前后保持不变。若手边有仿真器，可在 `SSR` 与 `LST` 之后各打印一次 `flag_output` 比对；否则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `NOP` 的指令体（[src/IKA32010.sv:639-643](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L639-L643)）几乎是空的？

**答案**：因为微码默认值本身就描述了「PC 自增 + 取下一条 + 不改任何状态」的空转行为，NOP 正好等于默认值，所以不需要覆盖任何信号。

**练习 2**：`POP` 用 `alu_paz = YES` 把 ALU 的 A 口清零，又用 `ALU_ADD` 加上栈值。为什么不直接「把栈值搬进累加器」？

**答案**：IKA32010 的累加器写回只能来自 ALU 输出（见 [u2-l7](u2-l7-alu-shifters-accumulator.md)）。要实现 `ACC ← 栈值`，最省事的做法是让 ALU 算 `0 + 栈值`：A 口清零（`alu_paz=YES`）、B 口取栈值（经 `reg_wrbus` → 移位器 A → 端口 B），再做加法。这是「数据通路复用 ALU」的典型写法。

---

### 4.3 辅助寄存器类指令 casez 分支

#### 4.3.1 概念说明

辅助寄存器类指令管理的是**寻址用的寄存器**：两个辅助寄存器 `AR0 / AR1`、指向「当前用哪个 AR」的指针 `ARP`、以及数据 RAM 的页指针 `DP`。它们的能力可以归纳成三类：

- **装载**：`LAR`（从 RAM 装 AR）、`LARK`（立即数装 AR）、`LDP`（从 RAM 装 DP）、`LDPK`（立即数装 DP）。
- **存储**：`SAR`（把 AR 存进 RAM）。
- **修改**：`MAR(LARP)`（间接寻址下修改 AR/ARP）、`MAR(NOP)`（直接寻址下的空操作）。

这一组最关键的两点认知：

1. **`LAR / LARK / SAR` 用指令字 bit8 显式指定目标 AR**（`AR0` 或 `AR1`），与 `ARP` 无关；而间接寻址的 inc/dec/ARP 副作用则总是作用在 `ARP` 当前指向的 AR 上。
2. **「间接寻址操作数译码片段」是被复制粘贴到几十条数据指令里的同一段 6 行代码**，由指令字 bit7/5/4/3/0 驱动。本组的 `LAR / SAR / LDP / MAR(LARP)` 都带这段片段。

#### 4.3.2 核心流程

**目标 AR 的选择**：

```
寻址时（算 RAM 地址）：用 ARP 指向的 AR   ← ar_addr_output = reg_ar[reg_arp][7:0]
LAR/LARK/SAR 的操作 AR：用 bit8 指定的 AR  ← ar_data_output = reg_ar[if_opcodereg[8]]
                                            ← reg_ar[if_opcodereg[8]] <= reg_wrbus  (装载时)
```

**间接寻址片段**（出现在每条带数据操作数的指令里，当 `bit7==1` 时执行）：

```
bit5 → reg_ar_inc   // 当前 AR[ARP] 低 9 位 +1
bit4 → reg_ar_dec   // 当前 AR[ARP] 低 9 位 -1
if(!bit3)           // bit3=0 时才允许改 ARP（bit3=1 表示「本指令不改 ARP」）
    bit0 → reg_arp_set / reg_arp_rst
```

**逐条流程**：

| 指令 | 操作码模式 | 动作 |
|---|---|---|
| `LAR` | `0011_100?_????_????` | `reg_ar_ld=YES`，目标 `AR[bit8]`，源来自 RAM（默认 `WRBUS_SOURCE_RAM`）；带间接片段 |
| `LARK` | `0111_000?_????_????` | `WRBUS_SOURCE_IMM`（reg_wrbus = `{8'h00, imm[7:0]}`）+ `reg_ar_ld=YES`，目标 `AR[bit8]` |
| `SAR` | `0011_000?_????_????` | `WRBUS_SOURCE_AR`（reg_wrbus = `AR[bit8]`）+ `ram_wr=YES`；带间接片段 |
| `LDP` | `0110_1111_????_????` | 由 `reg_wrbus[0]`（RAM 字最低位）驱动 `DP` 的 set/rst；带间接片段 |
| `LDPK` | `0110_1110_0000_000?` | 由 `bit0`（立即数）驱动 `DP` 的 set/rst |
| `MAR(LARP)` | `0110_1000_1???_????` | 只跑间接寻址片段（修改当前 AR 与 ARP） |
| `MAR(NOP)` | `0110_1000_0???_????` | 直接寻址，什么都不做（指令体只剩反汇编调用） |

> 旁注：`MAR(LARP)` 的操作码 `0x68xx`，源码用 **bit7** 区分两种编码：bit7=1（间接）落到 `MAR(LARP)`，bit7=0（直接）落到 `MAR(NOP)`。这和全 ISA「bit7 选直接/间接寻址」的约定一致。

#### 4.3.3 源码精读

**目标 AR 由 bit8 指定**。[src/IKA32010.sv:298](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L298)（存储/输出时读哪个 AR）与 [src/IKA32010.sv:317-319](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L317-L319)（装载时写哪个 AR）：

```verilog
assign ar_data_output = reg_ar[if_opcodereg[8]]; //used to save AR data
...
if(reg_ar_ld) begin
    reg_ar[if_opcodereg[8]] <= reg_wrbus;
end
```

注意这两处用的是 `if_opcodereg[8]`，而间接寻址地址用的是 `reg_arp`——正是 4.3.1 强调的两条路径。

**LAR（从 RAM 装载 AR）**。[src/IKA32010.sv:1092-1107](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1092-L1107)：

```verilog
//LAR - Load Auxillary Register
16'b0011_100?_????_????: begin
    reg_ar_ld = YES;                       // reg_ar[bit8] <= reg_wrbus (= RAM 字)
    if(if_opcodereg[7]) begin              // 间接寻址副作用片段
        reg_ar_inc = if_opcodereg[5]; reg_ar_dec = if_opcodereg[4];
        if(!if_opcodereg[3]) begin
            if(if_opcodereg[0]) reg_arp_set = YES;
            else                reg_arp_rst = YES;
        end
    end
```

源是默认的 `WRBUS_SOURCE_RAM`，所以装载的是 `RAM[ram_addr]`。注意 `LAR` 模式里 `bit8` 是 `?`（通配），所以 `0011_1000_...` 与 `0011_1001_...` 分别对应装 `AR0` 与装 `AR1`。

**LARK（立即数装载 AR）**。[src/IKA32010.sv:1109-1117](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1109-L1117)：

```verilog
//LARK - Load Auxillary Register Immediate
16'b0111_000?_????_????: begin
    register_wrbus_source_sel = WRBUS_SOURCE_IMM;  // reg_wrbus = {8'h00, imm[7:0]}
    reg_ar_ld = YES;                               // reg_ar[bit8] <= reg_wrbus
```

`WRBUS_SOURCE_IMM` 在选源 MUX 里把指令字低 8 位零扩展成 16 位（[src/IKA32010.sv:139](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L139)），所以 `LARK` 装入的是 8 位无符号立即数（高 8 位补 0）。`LARK` 不带间接寻址片段——因为它没有数据地址操作数。

**SAR（存储 AR 到 RAM）**。[src/IKA32010.sv:1168-1184](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1168-L1184)：

```verilog
//SAR - Store auxiliary register
16'b0011_000?_????_????: begin
    register_wrbus_source_sel = WRBUS_SOURCE_AR;   // reg_wrbus = AR[bit8]
    ram_wr = YES;                                  // 写 RAM[ram_addr]
    if(if_opcodereg[7]) begin                      // 间接寻址副作用片段
        ...
```

`WRBUS_SOURCE_AR` 把 `ar_data_output`（即 `AR[bit8]`）送上总线（[src/IKA32010.sv:137](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L137)）。

**LDP（从 RAM 装 DP）与 LDPK（立即数装 DP）**。[src/IKA32010.sv:1132-1158](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1132-L1158)：

```verilog
//LDP - Load Data Memory Page Pointer
16'b0110_1111_????_????: begin
    if(reg_wrbus[0]) reg_dp_set = YES;             // 由 RAM 字 bit0 驱动
    else             reg_dp_rst = YES;
    if(if_opcodereg[7]) begin ... end              // 间接寻址副作用
//LDPK - Load Data Memory Page Pointer Immediate
16'b0110_1110_0000_000?: begin
    if(if_opcodereg[0]) reg_dp_set = YES;          // 由指令字 bit0（立即数）驱动
    else                reg_dp_rst = YES;
```

两条都只动 `DP` 一个状态位（走 4.1 的 set/rst 机制），区别仅在数据来源：`LDP` 读 RAM 字的最低位，`LDPK` 用指令字内嵌的立即位。

**MAR(LARP) 与 MAR(NOP)**。[src/IKA32010.sv:1119-1130](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1119-L1130) 与 [src/IKA32010.sv:1160-1166](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1160-L1166)。`MAR(LARP)` 的指令体**就是一段间接寻址片段**——它存在的全部意义就是修改当前 AR（inc/dec）与 ARP；`MAR(NOP)`（bit7=0 的直接寻址）则什么都不做，连片段都没有，只剩反汇编打印。这解释了为什么官方把「只改 ARP」的 `LARP` 视作 `MAR` 的一个编码：在本实现里它就是「带间接寻址操作数的 MAR」。

**共享的间接寻址片段**——以 `ADD` 为参照。[src/IKA32010.sv:791-797](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L791-L797)：

```verilog
if(if_opcodereg[7]) begin
    reg_ar_inc = if_opcodereg[5]; reg_ar_dec = if_opcodereg[4];
    if(!if_opcodereg[3]) begin //AR register
        if(if_opcodereg[0]) reg_arp_set = YES;
        else                reg_arp_rst = YES;
    end
end
```

把这段和 `LAR`、`SAR`、`LDP`、`LST`、`SSR` 里的对应片段并排看，会发现它们**逐字相同**。这就是 [u2-l4](u2-l4-aux-registers-and-dp.md) 提到的「间接寻址操作数译码片段」——微码用复制粘贴的方式，让任何带数据操作数的指令都能附带 AR/ARP 副作用。

#### 4.3.4 代码实践

**实践目标**：亲手用 `LARK`+`MAR(LARP)`+`SAR` 操作 AR，验证「bit8 指定操作 AR」与「间接寻址副作用走 ARP」两条路径互不干扰。

**操作步骤（思维实验 + 可选仿真）**：假设初值 `ARP=0, AR0=0x1234, AR1=0x5678`，依次执行：

1. `LARK AR1, 0xAB` —— 操作码 `0111_0001_1010_1011`（bit8=1 选 AR1，立即数 0xAB）。
2. `MAR *+, 1` —— 操作码属于 `MAR(LARP)`（bit7=1），令当前 AR（ARP 指向的 AR0）+1，并把 ARP 改成 1。
3. `SAR AR0, ...` —— 把 AR0 存进某 RAM 单元。

**需要观察的现象**：

- 步骤 1 后：`AR1 = 0x00AB`（高 8 位补 0），`AR0` 不变。
- 步骤 2 后：`AR0 = 0x1235`（低 9 位 +1），`ARP = 1`。
- 步骤 3 后：写入 RAM 的是 `AR0 = 0x1235`（由 bit8=0 选定），与步骤 1 改过的 AR1 无关。

**预期结果**：装载/存储始终按 bit8 选 AR；inc/dec/ARP 副作用始终按 ARP 当时的指向。两条路径独立。若没有仿真器，标注「待本地验证」，但应能用本讲的位定义手算出每个中间值。

#### 4.3.5 小练习与答案

**练习 1**：`LARK` 能装入的最大值是多少？为什么？

**答案**：255（0xFF）。因为 `WRBUS_SOURCE_IMM` 只取指令字低 8 位并零扩展成 16 位（[src/IKA32010.sv:139](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L139)），AR 高 8 位恒为 0。

**练习 2**：指令字 bit3 在间接寻址片段里起什么作用？给出一个「bit3=1」时 ARP 不会改变的例子。

**答案**：bit3=1 时片段跳过 `reg_arp_set/rst` 的赋值，相当于「本指令不改 ARP」。例如 `ADD *, 1, 1`（bit3=1）会做 `AR` 的 inc/dec（若 bit5/4 非零）但不改 ARP；只有 bit3=0 时 bit0 才决定 ARP 的新值。

**练习 3**：为什么 `MAR(NOP)`（[src/IKA32010.sv:1160-1166](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1160-L1166)）的指令体几乎是空的？

**答案**：因为它用直接寻址（bit7=0），既没有数据地址操作数、也不进入间接寻址片段，而默认微码值本身就是「取下一条、不改任何状态」，于是 `MAR(NOP)` 与 `NOP` 一样无需覆盖任何信号。

---

## 5. 综合实践

综合实践的任务是**把本讲的三块知识串起来，核对 `LST` 与 `SSR` 两条指令读写的状态位，并与 TI 官方手册比对**。这正是学习目标里最后一条要求。

### 5.1 任务

为 `LST` 与 `SSR` 各列一张「读写状态位表」，列出发：状态位（V / OVM / INTM / ARP / DP）、源码里驱动的信号、数据来源、与官方手册是否一致。然后回答三个问题。

### 5.2 参考作答框架

**SSR（Store Status Register，官方 SST）** —— 读各状态位、写 RAM：

| 状态位 | 是否被读取 | 体现在映像字的哪一位 |
|---|---|---|
| V | 是 | bit15（`alu_flag_ovfl`） |
| OVM | 是 | bit14（`reg_ovm`） |
| INTM | 是 | bit13（`reg_intm`） |
| ARP | 是 | bit8（`reg_arp`） |
| DP | 是 | bit0（`reg_dp`） |

依据：[src/IKA32010.sv:479](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L479)（`flag_output` 拼接）+ [src/IKA32010.sv:753-768](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L753-L768)（`SSR` 指令体）。

**LST（Load Status register）** —— 读 RAM、写各状态位：

| 状态位 | 是否被写回 | 驱动信号 | 数据来源 |
|---|---|---|---|
| V | ✅ 是 | `alu_v_set / alu_v_rst` | `reg_wrbus[15]` |
| OVM | ✅ 是 | `reg_ovm_set / reg_ovm_rst` | `reg_wrbus[14]` |
| INTM | ❌ **否** | —— | 不恢复 |
| ARP | ✅ 是（分两种） | `reg_arp_set / reg_arp_rst` | 直接寻址：`reg_wrbus[8]`；间接寻址：指令字 bit0 |
| DP | ✅ 是 | `reg_dp_set / reg_dp_rst` | `reg_wrbus[0]` |

依据：[src/IKA32010.sv:664-688](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L664-L688)。

### 5.3 三个核对问题

1. **对称性**：`SSR` 会把 INTM 写进映像字（bit13），但 `LST` 不读它。这是实现 bug 吗？
   - **答案**：不是。这与官方 TMS32010 手册一致——`LST` 明确规定不装载 INTM 位（中断使能只能由 `EINT/DINT/复位` 改变）。`SSR` 仍写出 INTM 只是为了让映像字格式完整、可读。

2. **ARP 的双源**：为什么 `LST` 在直接寻址与间接寻址下，ARP 的来源不同？
   - **答案**：直接寻址（bit7=0）时没有间接操作数，所以 ARP 只能来自映像字本身（`reg_wrbus[8]`）；间接寻址（bit7=1）时，间接操作数本就要表达「顺便改 ARP」，所以 ARP 改由指令字 bit0 控制（走间接寻址片段），与映像字无关。两种情况下 V/OVM/DP 都来自映像字。

3. **位布局核对**：用 4.2.4 的例子（`V=1, OVM=1, INTM=0, ARP=0, DP=1` → 映像 `0xCF3F`），若紧接着执行 `LST`（直接寻址），各状态位会变成什么？INTM 呢？
   - **答案**：`V=1, OVM=1, DP=1, ARP=0`（均被映像字还原），`INTM` 保持 `LST` 执行前的值不变。

> 如果手边有仿真器，建议把 [src/IKA32010_tb.v](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v) 里的程序 ROM 换成一小段自编程序：`SOVM → EINT → LARK AR0,0 → SSR *（把状态存到 RAM0）→ DINT → ROVM → LST *（从 RAM0 读回）`，在每次 `SSR`/`LST` 后打印 `flag_output`，逐一比对上表。否则上述结论标注「待本地验证」。

## 6. 本讲小结

- IKA32010 的状态位（V/OVM/INTM/ARP/DP）是**散落的 1 位触发器**，统一用 `{set, rst}` 真值表在 `cyc_ncen` 拍写入；`set` 优先于 `rst`，`00/11` 为 hold。
- 控制类指令里的单信号翻转（`DINT/EINT/SOVM/ROVM`）只覆盖一个 `set/rst` 信号；`NOP` 因等于默认值而几乎为空；`NOP` 的操作码 `0x7F80` 正是指令寄存器的复位初值。
- `SSR`（官方 SST）通过 `WRBUS_SOURCE_FLAG` 把 `flag_output` 拼成的 16 位状态字写进 RAM；`LST` 反向把 RAM 字的 bit15/14/0/8 拆回 V/OVM/DP/ARP，但**不恢复 INTM**——与官方手册一致。
- `PUSH/POP` 是两周期指令，用 `ex_inst_cycle` 分相位：cycle 0 做实际栈操作（`PC_HOLD`+`BUSCTRL_STOP`+`if_opcodereg_force_iack=NO`），cycle 1 恢复取指。
- 辅助寄存器类指令中，`LAR/LARK/SAR` 用**指令字 bit8 显式选目标 AR**，而间接寻址的 inc/dec/ARP 副作用永远作用在 **ARP 当前指向的 AR** 上——两条路径独立。
- 一段逐字相同的「间接寻址操作数译码片段」（由 bit7/5/4/3/0 驱动）被复制进几乎所有带数据操作数的指令，是 IKA32010 微码复用的典型模式。

## 7. 下一步学习建议

- 接着读 [u3-l5（累加器算术逻辑类指令译码）](u3-l5-accumulator-instructions.md)，看 `ADD/LAC/SACH/SUB/SUBC` 等指令如何驱动 ALU、移位器与 RAM——它们同样带着本讲的「间接寻址片段」，并与 4.1 的 `V` 标志自动置位机制直接相关。
- 再读 [u3-l6（分支与子程序类指令）](u3-l6-branch-and-subroutine-instructions.md)，理解 `CALL/RET` 如何与本讲的栈机制（[u2-l6](u2-l6-hardware-stack.md)）配合完成调用返回。
- 想深入 LST/SSR 的语义对照，可翻 `docs/` 下的 TMS32010 用户手册中关于「Status Register Operations」一节，与 [src/IKA32010.sv:479](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L479) 的位布局互相印证。
- 若想验证本讲结论，建议用 [src/IKA32010_tb.v](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v) 作为模板，自编一段「翻转状态位 → SSR 存档 → 改状态 → LST 还原」的小程序，结合反汇编输出观察每个状态位的变化。
