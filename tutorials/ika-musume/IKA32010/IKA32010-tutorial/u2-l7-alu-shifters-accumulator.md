# ALU、移位器、累加器与标志位

## 1. 本讲目标

本讲聚焦 IKA32010 的「运算心脏」——ALU 子模块及其周边的移位器、累加器与标志位。读完本讲，你应当能够：

- 说清 ALU 两个输入端口（A/B）的来源、`adder_cin` 进位逻辑，以及 AND/OR/XOR/ABS/ADD/SUB/SUBC 七种运算是如何用「一个加法器 + 多个多路选择」统一实现的。
- 区分移位器 A（ALU 输入定标）与移位器 B（累加器输出定标）各自的位置与作用，理解符号扩展抑制 `sha_ssup`。
- 解释 `reg_ovm` 溢出饱和模式如何把越界结果钳位到 32 位有符号极值，以及 SUBC 条件减法如何用两个周期完成「一位」除法。
- 独立追踪一条 SUBC 指令在两个机器周期内 `subc_divided / prev_subc / 累加器` 的变化，并画出时序。

本讲只讲硬件通路与算术原理，不展开每条指令的微码译码（那是 u3-l5 的主题），但会引用若干指令的微码片段来说明控制信号如何驱动 ALU。

## 2. 前置知识

在进入 ALU 之前，请确认你已经掌握下列概念（它们都在前置讲义中建立）：

- **机器周期与相位**（u1-l4）：4 个 `i_EMUCLK` = 1 个 DSP 机器周期，`cyc_ncen`（`cyclecntr==3`）是主工作拍，几乎所有寄存器都在 `cyc_ncen` 边沿更新。
- **内部写总线 `reg_wrbus`**（u2-l1）：16 位共享写总线，是数据 RAM、立即数、栈等数据汇入 ALU 的主干道。本讲的移位器 A 就挂在 `reg_wrbus` 上。
- **数据 RAM 读出**（u2-l5）：RAM 读端口的数据默认经 `reg_wrbus` → 移位器 A 送到 ALU 端口 B，所以一条 ADD 指令「天然」就能把 RAM 里的数加进累加器。

此外需要一点数字电路常识：

- **二进制补码与减法**：减法 `A − B` 用加法器实现为 `A + (~B) + 1`，其中 `~B` 是按位取反，`+1` 是进位输入 `cin`。
- **有符号溢出判定**：把进位进入最高位（`carry_in_31`）与最高位的进位输出（`carry_out_31`）做异或，即为有符号溢出标志 OV。本讲后文会给出对应公式。
- **桶形移位（barrel shifter）**：在一个周期内把数据左移任意位数的纯组合电路，TMS32010 在 ALU 输入/输出各配了一个。

## 3. 本讲源码地图

本讲涉及的全部代码都集中在两个文件：

| 文件 | 作用 | 本讲关注的内容 |
|------|------|----------------|
| `src/IKA32010.sv` | 顶层模块 + 4 个内嵌子模块 | 移位器 A/B、ALU 实例化、`reg_ovm`、`flag_output`、微码默认值、若干指令译码片段 |
| `src/IKA32010_mnemonics.sv` | 助记符常量字典 | `ALU_AND..ALU_SUBC`、`ALU_PBDATA_*`、`ALU_SOURCE_*` 等常量 |

关键代码点速查（永久链接行号均对应当前 HEAD `51bc1f0`）：

- 移位器 A：`IKA32010.sv` 第 424–431 行
- ALU 实例化：`IKA32010.sv` 第 448–456 行
- 移位器 B：`IKA32010.sv` 第 458–471 行
- 状态位拼装 `flag_output`：`IKA32010.sv` 第 479 行
- `reg_ovm` 寄存器：`IKA32010.sv` 第 262–271 行
- 微码默认值（ALU/移位器部分）：`IKA32010.sv` 第 552、584–587 行
- ALU 子模块 `IKA32010_alu`：`IKA32010.sv` 第 1760–1906 行
- SUBC 指令译码：`IKA32010.sv` 第 965–982 行

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先讲 ALU 加法器与七种运算（4.1），再讲前后两个移位器（4.2），然后讲累加器、OVM 饱和与 Z/N/V 标志（4.3），最后用一整节深入 SUBC 条件减法除法器（4.4）——它把前三节的所有部件都串了起来，也是本讲的实践重点。

### 4.1 ALU 端口、加法器与七种运算

#### 4.1.1 概念说明

`IKA32010_alu` 是一个 32 位的算术逻辑单元，对应 TMS32010 手册中的 ALU。它的设计哲学是「**用一个 32 位加法器统一实现所有运算**」：

- AND/OR/XOR 是纯按位逻辑，直接对端口 A 与端口 B 做运算。
- ADD 就是加法本身。
- SUB 借助补码：`A + (~B) + 1`。
- ABS（取累加器绝对值）也走加法器：当累加器为负时，`port_a = ~ACC`、`adder_cin = 1`，于是 `~ACC + 1 = −ACC`，正数时则 `port_a = ACC`、`cin = 0`。
- SUBC（条件减法，用于除法）是 SUB 的「条件版」，再额外用第二个周期做条件左移——这是本讲最精巧的部分，留到 4.4 详解。

端口 A 恒为累加器的回送值（或其取反/置零），端口 B 有两个来源：移位器 A 的输出（数据 RAM 经定标后的操作数）或乘法器的 P 寄存器（用于乘加指令）。两者由 `alu_pbsel` 选择，送入子模块的 `i_ALU_PB`。

#### 4.1.2 核心流程

ALU 在一个机器周期内完成「组合运算」组合逻辑部分，结果在 `cyc_ncen` 边沿写入累加器。流程如下：

1. 顶层微码根据指令给出 `alu_modesel`（3 位运算选择）、`alu_paz`/`alu_pbz`（强制端口 A/B 为 0）、`alu_pbdata`（端口 B 的字/字节截取）、`alu_pbsel`（端口 B 来源）。
2. 子模块用组合逻辑生成 `port_a`、`port_b`、`adder_cin`。
3. 32 位加法器算出 `alu_adder`，并附带溢出位 `alu_ovfl`。
4. 一个组合 `case(i_ALU_MODESEL)` 选择最终 `alu_output`（逻辑运算直接给结果；算术运算套用饱和逻辑）。
5. 在 `cyc_ncen` 边沿，若 `alu_acc_ld` 为高，`alu_output` 锁存进累加器，同时刷新 Z/N/V 标志。

减法的补码化由 `adder_cin` 完成。设 `M = i_ALU_MODESEL`，则：

\[ \text{cin} = \begin{cases} 1 & M \in \{\text{SUB}, \text{SUBC}\} \quad (\text{补码的} +1) \\ \text{ACC}[31] & M = \text{ABS} \quad (\text{仅负数取反}+1) \\ 0 & \text{其他} \end{cases} \]

有符号溢出判定（`carry_in_31 XOR carry_out_31`）：

\[ \text{OV} = C_{\text{in},31} \oplus C_{\text{out},31} \]

源码用 31 位加法器的最高位与 1 位进位段拼接来同时取得这两个量（见 4.1.3）。

#### 4.1.3 源码精读

**ALU 子模块端口**（[src/IKA32010.sv:1760-1778](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1760-L1778)）：注意 `i_CEN` 由顶层接 `cyc_ncen`，`o_ALU_ACC_OUTPUT` 既是输出又反馈回 `i_ALU_PA`，构成「累加器回送」闭环。

**进位输入 `adder_cin` 与端口 A/B 生成**（[src/IKA32010.sv:1800-1836](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1800-L1836)）：这段是 4.1.2 流程的硬件实现。关键三点：

- SUB/SUBC 时 `port_b = ~i_ALU_PB` 配合 `adder_cin=1` 完成补码减法；ABS 时 `port_a = ~i_ALU_PA`（仅负数）配合 `cin=ACC[31]`。
- `alu_paz`/`alu_pbz` 把端口整体置零，于是 `LAC`（置 A 为 0，只剩端口 B → 相当于「装载」）和 `ZAC`（A、B 都置零 → 结果 0）都能复用 ADD 实现。
- `case(i_ALU_PBDATA)` 决定端口 B 取 32 位全字、高 16 位、低 16 位还是低 8 位，对应 ADDH/ZALH（高位）、AND/OR/XOR/ADDS（低字）与 LACK（字节）。

**32 位加法器与溢出位**（[src/IKA32010.sv:1840-1846](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1840-L1846)）：源码把加法拆成「低 31 位」与「最高位 + 进位」两段，正是为了同时取出 `C_in,31`（`alu_adder31[31]`）与 `C_out,31`（`alu_adder1[1]`）来异或得到 `alu_ovfl`。

**运算选择与饱和**（[src/IKA32010.sv:1849-1863](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1849-L1863)）：`ALU_ADD/SUB/SUBC` 三支共享同一套饱和表达式——只有 ADD/SUB/SUBC 受 `i_ALU_OVM`（溢出模式位）影响，逻辑运算（AND/OR/XOR）与 ABS 不做饱和。

**顶层实例化**（[src/IKA32010.sv:448-456](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L448-L456)）：端口 B 由 `alu_pbsel ? reg_p : sha_output` 选择——非乘加指令走移位器 A，乘加指令（APAC/PAC/SPAC）走 P 寄存器（详见 u2-l8）。

#### 4.1.4 代码实践（源码阅读型）

**目标**：验证「逻辑运算只看端口 B 低 16 位，算术运算用全 32 位」。

**步骤**：

1. 打开 [src/IKA32010.sv:1849-1863](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1849-L1863) 的运算选择块。
2. 对比 `ALU_AND`/`ALU_OR`/`ALU_XOR` 三行与 `ALU_ADD` 一行：前者的操作数写成 `{16'h0000, port_b[15:0]}`，后者直接用 `port_a + port_b + ...`。
3. 回到端口 B 截取逻辑 [src/IKA32010.sv:1827-1832](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1827-L1832)，确认 AND/OR/XOR 指令译码里 `alu_pbdata` 被设成 `ALU_PBDATA_LOWWORD`。

**需要观察的现象**：逻辑运算的高 16 位结果只来自 `port_a`（即累加器高字），端口 B 的高 16 位被两层掩码（`{16'h0000,...}` 与 `ALU_PBDATA_LOWWORD`）双重屏蔽。

**预期结果**：你能用一句话解释「为什么 TMS32010 的 AND/OR/XOR 只作用在累加器低 16 位」——因为这是 16 位 DSP，累加器高 16 位是「溢出/保护位」，逻辑运算不应触碰。

**待本地验证**：以上为静态阅读结论；如需眼见为实，可在仿真中执行 `LACK 0xFF` 后 `AND` 一个 `0x000F` 的内存单元，观察累加器高字是否保持 0。

#### 4.1.5 小练习与答案

**练习 1**：ABS 指令译码（[src/IKA32010.sv:776-779](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L776-L779)）把 `alu_pbz = YES`。为什么 ABS 要屏蔽端口 B？

**答案**：ABS 只对累加器自身取绝对值，不需要第二个操作数。屏蔽端口 B 后，加法器实际算的是 `port_a + 0 + cin`，即 `~ACC + 1`（负数时）或 `ACC + 0`（正数时），正是取绝对值。

**练习 2**：`LAC`（装载累加器）译码设 `alu_modesel = ALU_ADD; alu_paz = YES`（[src/IKA32010.sv:862-864](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L862-L864)）。为什么「清零端口 A 的加法」等价于「装载」？

**答案**：端口 A 清零后，`0 + port_b + 0 = port_b`，于是结果就是端口 B（经移位器 A 定标的操作数），相当于把操作数直接装入累加器，丢弃原来的累加器值。

### 4.2 移位器 A 与移位器 B

#### 4.2.1 概念说明

TMS32010 是一颗 16 位定点 DSP，但累加器是 32 位的。为了让 16 位数据在 32 位累加器里对齐，并支持定点小数的定标（scaling），ALU 前后各配了一个桶形移位器：

- **移位器 A（shifter-A，信号 `sha_*`）**：ALU **输入侧**定标。它把来自 `reg_wrbus` 的 16 位数据符号扩展成 32 位，再左移 `sha_amt` 位，送入 ALU 端口 B。指令如 `ADD`/`SUB`/`LAC` 用指令字的 `[11:8]` 位指定 0–15 的移位量。
- **移位器 B（shifter-B，信号 `shb_*`）**：ALU **输出侧**定标。它把 32 位累加器内容左移 0/1/4 位，并选择输出高 16 位或低 16 位，结果 `shb_output` 经 `WRBUS_SOURCE_SHB` 汇入 `reg_wrbus`，供 `SACH`/`SACL` 写回 RAM。

简言之：**移位器 A 决定操作数以怎样的「比例」进入 ALU，移位器 B 决定累加器以怎样的「比例」被存出去**。

#### 4.2.2 核心流程

移位器 A 是纯组合逻辑：

1. 16 位 `reg_wrbus` 经符号扩展成 32 位（`sha_ssup` 为高时改为零扩展）。
2. 再左移 `sha_amt` 位得到 `sha_output`，送入端口 B。

移位器 B 也是纯组合逻辑：

1. 按 `shb_amt`（0/1/4）对 `alu_acc_output` 左移得到 `shb_intermediate`。
2. `shb_mux` 选高 16 位（`shb_mux=HIGH`，用于 SACH）或低 16 位（`shb_mux=LOW`，用于 SACL）输出 `shb_output`。

注意两者的「默认值」都在微码默认块里设好（不移位、不抑制符号、取低字），多数指令沿用默认值即可。

#### 4.2.3 源码精读

**移位器 A**（[src/IKA32010.sv:424-431](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L424-L431)）：注意它是「先符号扩展，再左移」——两步合在一个 `always @(*)` 块里，先赋值再覆盖。`sha_ssup`（sign-extension suppression）为高时用 `{16'h0000, reg_wrbus}` 零扩展，这正是 `ADDS`/`SUBS`/`LACK` 等「无符号」指令的实现方式。

**移位器 B**（[src/IKA32010.sv:458-471](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L458-L471)）：`shb_amt` 只支持 0/1/4 三档（其余回落到不移位），分别对应 SACL（不移位取低字）、以及 SACH 的两档左移。`shb_output` 在第 463 行用 `assign` 声明，与第 124–135 行的写总线选源 MUX 里的 `WRBUS_SOURCE_SHB` 分支对接（见 u2-l1）。

**微码默认值**（[src/IKA32010.sv:583-587](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L583-L587)）：`sha_amt=0; sha_ssup=NO; shb_amt=0; shb_mux=LOW` 是默认值，对应「输入不移位、带符号扩展、输出取低 16 位」。

**SACH 指令如何驱动两个移位器之外的部分**（[src/IKA32010.sv:908-925](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L908-L925)）：SACH 只用移位器 B——`shb_amt = if_opcodereg[10:8]`（移位量 0/1/4 取决于编码）、`shb_mux = HIGH`（取高 16 位），然后把 `WRBUS_SOURCE_SHB` 选上 `reg_wrbus` 并置 `ram_wr=YES` 写回 RAM。注意 SACH 不触发 ALU 运算（不设 `alu_acc_ld`），它只读累加器。

#### 4.2.4 代码实践（源码阅读型）

**目标**：对比「带符号扩展」的 ADD 与「符号扩展抑制」的 ADDS，理解 `sha_ssup` 的作用。

**步骤**：

1. 阅读 ADD 译码 [src/IKA32010.sv:786-789](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L786-L789) 与 ADDS 译码 [src/IKA32010.sv:824-827](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L824-L827)。
2. 找出唯一差异：ADDS 多了一行 `sha_ssup = YES`。
3. 回到移位器 A [src/IKA32010.sv:429](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L429)，思考当内存单元 = `0xFFFF` 时两种指令的差异。

**需要观察的现象**：

- ADD：`0xFFFF` 被当作 −1（符号扩展成 `0xFFFFFFFF`），加到累加器等于 −1。
- ADDS：`0xFFFF` 被当作 +65535（零扩展成 `0x0000FFFF`），加到累加器等于 +65535。

**预期结果**：你能用一句话说明「ADDS 用于把 16 位无符号数加进累加器，ADD 则把 16 位有符号数加进累加器」。

**待本地验证**：可在仿真里分别对同一内存值 `0xFFFF` 执行 `ADD` 与 `ADDS`，比对累加器结果。

#### 4.2.5 小练习与答案

**练习 1**：`ZALH`（清零并装载高字）译码里有 `alu_paz=YES`（清端口 A）和 `sha_amt = 5'd16`（[src/IKA32010.sv:1051-1053](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1051-L1053)）。这一步把内存数据放到了累加器的哪一部分？

**答案**：端口 A 清零后，结果 = `port_b` = 内存值左移 16 位，即内存值被放到累加器的 **高 16 位**，低 16 位为 0。这正是「Zero Accumulator and Load High」的语义。

**练习 2**：移位器 B 的 `shb_amt` 只支持 0、1、4 三档（[src/IKA32010.sv:465-470](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L465-L470)）。这对应 TMS32010 的哪条指令的哪几种编码？

**答案**：对应 SACH（Store Accumulator High）指令。SACH 用指令字 `[10:8]` 编码移位量，手册规定该字段只能是 0、1、2、3，但 SACH 的有效左移是 0/1/4 位（编码 0→不移位、1→左移1、2→左移4、3→左移4 的变体）；源码用 `case` 显式列出 0/1/4 三档，其余编码回落到「不移位」。

### 4.3 累加器、OVM 饱和与 Z/N/V 标志

#### 4.3.1 概念说明

累加器（ACC，32 位）是 ALU 的核心寄存器，也是 TMS32010 程序最频繁读写的寄存器。它有两个重要配套：

- **OVM（Overflow Mode，溢出模式位）**：由 `reg_ovm` 寄存器保存，`SOVM` 置位、`ROVM` 复位。当 OVM=1 且运算发生有符号溢出时，ALU 把结果**钳位（饱和）**到 32 位有符号极值——正溢出给 `0x7FFF_FFFF`，负溢出给 `0x8000_0000`。OVM=0 时则原样输出（让溢出「溢出去」，高位被截）。
- **Z/N/V 三个标志**：每次累加器被装载时同步刷新。Z=1 表示结果为零，N=1 表示结果为负（看最高位），V=1 表示发生了有符号溢出。V 还可由 `LST`/特定指令显式置/复位。

`reg_ovm` 有一个反直觉的特性：**复位 `i_RS_n` 不会清除它**（见源码注释），这与 `reg_intm`（复位为 1）等寄存器不同。

#### 4.3.2 核心流程

累加器与标志的更新都发生在 `cyc_ncen` 边沿：

1. **累加器装载**：`alu_acc_ld`（或 SUBC 的 `prev_subc`，见 4.4）为高时，`alu_output` 写入累加器 `o_ALU_ACC_OUTPUT`。
2. **饱和**：在组合的运算选择块里，ADD/SUB/SUBC 三支根据 `i_ALU_OVM` 与 `alu_ovfl` 决定是否钳位。饱和表达式为：

\[ \text{result} = \begin{cases} \text{alu\_adder} & \text{OV}=0 \text{ 或 OVM}=0 \\ \{~b_{31},\, 31\times b_{31}\} & \text{OV}=1 \text{ 且 OVM}=1 \end{cases} \]

其中 \(b_{31} = \text{alu\_adder31}[31]\)（结果的符号位）。当 \(b_{31}=0\)（正溢出）钳位成 `0x7FFF_FFFF`，当 \(b_{31}=1\)（负溢出）钳位成 `0x8000_0000`。

3. **标志刷新**：同一 `cyc_ncen` 边沿，若 `alu_acc_ld` 为高，则 Z/N/V 按 `alu_output` 刷新；否则 V 由 `alu_v_set/rst` 控制，Z/N 保持。

4. **OVM 与状态位拼装**：`reg_ovm` 由 SOVM/ROVM/LST 改写；它和 V、INTM、ARP、DP 一起拼成 16 位状态寄存器 `flag_output`，供 SSR 指令存回 RAM。

#### 4.3.3 源码精读

**`reg_ovm` 寄存器**（[src/IKA32010.sv:262-271](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L262-L271)）：注意这段 `always` 块里**没有 `if(!i_RS_n)` 复位分支**，只有 `cyc_ncen` 下的 `set/rst` 真值表，所以复位后 `reg_ovm` 保持初值 `1'b0`（声明时的初始化），这与注释「RESET will not clear this bit!!!」一致——严格说它由 FPGA 的寄存器初值保证，而非同步复位。

**饱和表达式**（[src/IKA32010.sv:1857-1859](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1857-L1859)）：三元嵌套 `i_ALU_OVM ? alu_ovfl ? {~alu_adder31[31], {31{alu_adder31[31]}}} : alu_adder : alu_adder` 正是 4.3.2 公式的逐字翻译。

**累加器装载**（[src/IKA32010.sv:1874-1881](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1874-L1881)）：`alu_acc_ld = prev_subc | i_ALU_ACC_LD`——SUBC 在第二周期靠 `prev_subc` 装载（见 4.4）。复位时累加器清零。

**Z/N/V 标志刷新**（[src/IKA32010.sv:1883-1904](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1883-L1904)）：复位初值 `Z=1, N=0, V=0`。装载时 Z=`(alu_output==0)`、N=`alu_output[31]`、V=溢出位；非装载时 V 由 `{i_ALU_V_SET, i_ALU_V_RST}` 真值表控制（`LST` 装状态寄存器时用）。注意 V 的计算 `alu_adder31[31] ^ alu_adder1[1]` 与第 1846 行的 `alu_ovfl` 完全一致——标志位忠实记录是否溢出，**与 OVM 饱和与否无关**（即使饱和了，V 仍会置位）。

**状态位拼装**（[src/IKA32010.sv:479](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L479)）：`{alu_flag_ovfl, reg_ovm, reg_intm, 4'b1111, reg_arp, 6'b111111, 1'b1, reg_dp}` 对应 TMS32010 状态寄存器：bit15=OV、bit14=OVM、bit13=INTM、bit8=ARP、bit0=DP，其余为保留位（恒 1）。

**SOVM/ROVM 译码**（[src/IKA32010.sv:734-750](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L734-L750)）：两条指令分别只动 `reg_ovm_set`/`reg_ovm_rst`，是最简单的「置位/复位」型控制指令。

#### 4.3.4 代码实践（思考 + 阅读型）

**目标**：理解「V 标志与 OVM 饱和互相独立」。

**步骤**：

1. 假设累加器 = `0x7FFF_FFFF`（最大正数），执行 `ADD #1`（经 LACK/ADD 序列）。
2. 手算：`0x7FFF_FFFF + 1 = 0x8000_0000`，符号位变 1，发生正溢出，`alu_ovfl=1`，`alu_adder31[31]=1`。
3. 套用饱和公式：若 OVM=1，结果 = `{~1, 31×1} = 0x7FFF_FFFF`（钳回最大正数）；若 OVM=0，结果 = `0x8000_0000`（原样，变成最小负数）。
4. 查标志块 [src/IKA32010.sv:1893](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1893)，确认 V=`alu_adder31[31] ^ alu_adder1[1]` 与是否饱和无关。

**需要观察的现象**：无论 OVM 是 0 还是 1，V 标志都会被置 1；但累加器值不同（饱和时回到 `0x7FFF_FFFF`）。

**预期结果**：你能解释「为什么 TMS32010 程序在 SOVM 之后仍能用 BV（溢出跳转）检测到溢出」——因为 V 标志独立于饱和逻辑。

**待本地验证**：上述为手算 + 静态阅读结论；仿真确认时注意先 `SOVM` 再做越界加法。

#### 4.3.5 小练习与答案

**练习 1**：复位后立即读 `reg_ovm`，它的值是多少？为什么？

**答案**：`1'b0`（溢出模式关闭）。因为 `reg_ovm` 的 `always` 块没有 `if(!i_RS_n)` 同步复位分支，复位不影响它，它保持声明初值 `1'b0`。注释明确标注「RESET will not clear this bit」。

**练习 2**：`ZAC`（清零累加器）译码为 `alu_modesel=ALU_ADD; alu_paz=YES; alu_pbz=YES`（[src/IKA32010.sv:1040-1042](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1040-L1042)）。执行后 Z/N/V 标志分别是什么？

**答案**：结果 = `0 + 0 + 0 = 0`，装入累加器。于是 Z=1（结果为零）、N=0（最高位为 0）、V=0（`0+0` 无溢出，`alu_adder31[31]=0`、`alu_adder1[1]=0`）。

### 4.4 SUBC 条件减法除法器（实践重点）

#### 4.4.1 概念说明

`SUBC` 是 TMS32010 用来做**除法**的指令。DSP 没有硬件除法器，但提供一条「条件减法」指令，让软件循环 16 次完成一个 16 位除法。SUBC 的语义（设除数在数据内存单元 `dma`，被除数在累加器低字）：

> 若 `ACC − (dma << 15) ≥ 0`，则 `ACC ← (ACC − (dma << 15)) << 1 + 1`；否则 `ACC ← ACC << 1`。

每执行一次 SUBC，累加器左移一位，并在最低位「商」出一个比特（够减商 1，不够减商 0）。循环 16 次后，累加器**低 16 位 = 商，高 16 位 = 余数**。

IKA32010 用**两个机器周期**实现一次 SUBC：第一周期（译码周期）做减法并判断符号；第二周期（`prev_subc` 周期）做条件左移与置商位、并装载累加器。这正是源码注释「EX unit takes 1 cycle to process SUBC, but ALU doesn't」（[src/IKA32010.sv:1865](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1865)）的含义——执行单元（EX）视角看 SUBC 是单周期指令，但 ALU 内部要靠 `prev_subc` 跨周期完成。

#### 4.4.2 核心流程

设除数 `D`（已在内存，经移位器 A 左移 15 位后送端口 B），被除数在累加器 `A0`。

**周期 1（SUBC 译码周期，`prev_subc` 仍为 0）**：

- `alu_modesel = ALU_SUBC`，`sha_amt = 15`，故端口 B = `D << 15`。
- 走减法分支：`port_b = ~(D<<15)`、`adder_cin = 1`，所以 `alu_output = A0 − (D<<15)`。
- 组合判断：若 `alu_output[31]==0`（结果非负，「够减」），则 `subc_divided` 在本 `cyc_ncen` 边沿置 1，否则置 0。
- 同时 `prev_subc ← 1`，`prev_adder ← alu_output`（保存减法结果）。
- **注意**：SUBC 译码里**没有** `alu_acc_ld = YES`，所以累加器在本周期**不**装载——装载推迟到下一周期。

**周期 2（`prev_subc` 周期，此时译码的是下一条指令）**：

- 因为 `prev_subc=1`，端口 A/B 生成走特殊分支（[src/IKA32010.sv:1810,1820](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1810)）：
  - 若 `subc_divided=1`（够减）：`port_a = 0`，`port_b = prev_adder << 1`，`adder_cin = 1` → `alu_output = (A0 − D<<15)<<1 + 1`。
  - 若 `subc_divided=0`（不够减）：`port_a = A0 << 1`，`port_b = 0`，`adder_cin = 0` → `alu_output = A0<<1`。
- `alu_acc_ld = prev_subc | ... = 1`，于是累加器装载 `alu_output`。

把两支合并，正好是 4.4.1 的语义。这正是源码注释「!!! next instruction cannot use the ACC !!!」（[src/IKA32010.sv:967](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L967)）的原因：第二周期 ALU 仍在为 SUBC 装载累加器，若下一条指令也想用累加器，就会和 `prev_subc` 分支冲突。

#### 4.4.3 源码精读

**SUBC 指令译码**（[src/IKA32010.sv:965-982](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L965-L982)）：只设 `alu_modesel=ALU_SUBC` 和 `sha_amt=15`，**不设** `alu_acc_ld`（默认 NO）。

**SUBC 控制寄存器**（[src/IKA32010.sv:1793-1795](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1793-L1795)）：`prev_subc`（上一拍是否为 SUBC）、`subc_divided`（够减标志）、`prev_adder`（减法结果缓存）。

**周期 2 的端口生成**（[src/IKA32010.sv:1810,1820,1803](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1810-L1820)）：这三行是 SUBC 的灵魂——`prev_subc=1` 时端口 A/B 与进位都按 `subc_divided` 条件切换。注意 `adder_cin` 在 [src/IKA32010.sv:1803](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1803) 里 `if(subc_divided) adder_cin=1`，这正是「够减时商位 +1」的来源。

**SUBC 控制时序**（[src/IKA32010.sv:1866-1872](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1866-L1872)）：每个 `cyc_ncen` 边沿刷新 `subc_divided`、`prev_subc`、`prev_adder`。

**装载门控**（[src/IKA32010.sv:1875](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1875)）：`alu_acc_ld = prev_subc | i_ALU_ACC_LD`——SUBC 在第二周期靠 `prev_subc` 触发装载。

#### 4.4.4 代码实践（跟踪型，本讲主实践）

**目标**：手动跟踪一次 SUBC 在两个周期内 `subc_divided / prev_subc / 累加器` 的变化，验证除法原理；并验证 OVM 饱和时的输出。

**场景设定**：为便于手算，用一个「迷你」例子（位宽缩小但逻辑相同）。设除数 `D = 3`，被除数 `A0 = 7`（放在累加器低字，高字为 0）。为了套用源码的「左移 15」逻辑，我们把它想象成 32 位运算：`D << 15` 是把除数顶到最高位附近。

**操作步骤**（对照源码逐拍填写下表）：

| 时刻 | `prev_subc` | `subc_divided` | `port_a` | `port_b` | `alu_output` | 装载 ACC? | 含义 |
|------|-------------|----------------|----------|----------|--------------|-----------|------|
| 周期1 边沿前 | 0 | 0（初值） | `A0` | `~(D<<15)` | `A0 − (D<<15)` | 否（`alu_acc_ld=0`） | 做减法、判符号 |
| 周期1 边沿后 | 1 | （看 `alu_output[31]`） | — | — | — | — | `prev_subc←1`，`prev_adder←alu_output` |
| 周期2 边沿前 | 1 | 见上行 | 见下 | 见下 | 见下 | 是（`prev_subc=1`） | 条件左移 + 商位 |
| 周期2 边沿后 | 0 | 0 | — | — | — | — | 累加器装入最终结果 |

**周期 2 的两支**（请你自己判断 `subc_divided` 后填入）：

- 若够减（`A0 ≥ D<<15`，`alu_output[31]==0`）：`port_a=0`、`port_b=prev_adder<<1`、`cin=1` → `ACC = (A0 − D<<15)<<1 + 1`。
- 若不够减：`port_a=A0<<1`、`port_b=0`、`cin=0` → `ACC = A0<<1`。

**需要观察的现象**：

1. 累加器在**周期 1 不变**（因为 `alu_acc_ld` 默认 NO），在**周期 2 边沿**才更新——这与普通 ADD/SUB「当周期装载」不同。
2. `subc_divided` 的值完全由周期 1 减法结果的符号位决定。
3. 商位（最低位）在够减时被置 1，正是「够减商 1」。

**预期结果**：你能画出一张两周期的状态表，并解释为什么「SUBC 后必须跟一条不用累加器的指令」（因为周期 2 ALU 仍在装载）。把 16 次 SUBC 串起来，每次累加器左移一位并写入一个商位，最终低 16 位即商。

**OVM 饱和验证（思考题延伸）**：周期 1 的减法 `A0 − (D<<15)` 若发生溢出，由于 `ALU_SUBC` 也套用饱和表达式（[src/IKA32010.sv:1859](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1859)），`prev_adder` 会拿到饱和后的值（`0x7FFF_FFFF` 或 `0x8000_0000`），周期 2 据此继续。本例数值小不会触发，但大数除法时需注意。

**待本地验证**：以上为静态跟踪结论。完整验证需在仿真中执行 16 次 SUBC 循环，建议参考 TMS32010 用户手册（docs/ 下 User's Guide）中 SUBC 除法例程的标准写法，比对最终累加器低字是否等于 `A0 ÷ D` 的商。

#### 4.4.5 小练习与答案

**练习 1**：为什么 SUBC 译码里**不设** `alu_acc_ld = YES`，而普通 ADD 却要设？

**答案**：SUBC 的装载由 `alu_acc_ld = prev_subc | i_ALU_ACC_LD` 控制。若译码周期就设 `alu_acc_ld=YES`，累加器会在周期 1 装载减法结果（而非条件左移后的结果），破坏除法逻辑。SUBC 故意让周期 1 不装载，把装载推迟到周期 2 的 `prev_subc` 拍，那时 `alu_output` 已经是「条件左移 + 商位」的正确结果。

**练习 2**：如果 SUBC 后面紧跟一条 `ADD`（要用累加器），会发生什么？

**答案**：会出错。因为周期 2（`prev_subc=1`）那条 ADD 的累加器装载会被 `prev_subc` 分支覆盖——此时端口 A/B 走的是 SUBC 的特殊分支（`port_a = ACC<<1` 或 `0`），算出的不是 ADD 想要的结果。源码注释「next instruction cannot use the ACC」正是在警告这一点，标准 SUBC 除法循环里 SUBC 后面跟的是 NOP 或另一条 SUBC。

**练习 3**：`prev_adder` 这个寄存器保存的是什么？为什么周期 2 需要它？

**答案**：`prev_adder` 保存周期 1 减法的结果 `A0 − (D<<15)`（[src/IKA32010.sv:1871](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1871)）。周期 2 在「够减」分支里要用到这个减法结果再左移 1 位再加 1，但此时组合的 `alu_output` 已经不再是减法（因为 `prev_subc=1` 切换了分支），所以必须把减法结果缓存到 `prev_adder` 跨周期带过来。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「阅读 + 手算」综合任务：

**任务**：阅读 [src/IKA32010.sv:776-783](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L776-L783) 的 ABS 指令，然后画出 ABS 执行时数据从累加器流经 ALU 再回到累加器的完整路径，标出每一段用到的本讲部件。

**要求**：

1. 指出 ABS 用到的移位器（A 还是 B？还是都不用？）、ALU 运算模式、端口 A/B 生成分支、进位输入、是否饱和、是否装载累加器、Z/N/V 如何刷新。
2. 分「累加器为正」和「累加器为负」两种情况，分别写出 `port_a`、`adder_cin`、`alu_output` 的值。
3. 解释为什么 ABS 之后 V 标志**不会**被置位（提示：取绝对值本身不会产生新的有符号溢出——除非累加器是 `0x8000_0000`，这是经典的「最小负数无对应正数」陷阱，待本地验证源码在该特例下的行为）。

**参考答案要点**：

- ABS 不用移位器 A（`sha_amt` 默认 0，且端口 B 被 `alu_pbz` 屏蔽），也不用移位器 B（不写 RAM）。它只走 ALU 加法器。
- 运算模式 `ALU_ABS`：`port_a = ACC[31] ? ~ACC : ACC`，`adder_cin = ACC[31]`（[src/IKA32010.sv:1806,1814](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1806-L1814)），端口 B=0。
  - ACC 为正：`alu_output = ACC + 0 + 0 = ACC`。
  - ACC 为负：`alu_output = ~ACC + 0 + 1 = −ACC`（即绝对值）。
- ABS 套用的是 `ALU_ABS` 分支（[src/IKA32010.sv:1856](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1856)），**不**经过饱和表达式，所以 OVM 对 ABS 无影响。
- `alu_acc_ld=YES`，装载后 Z/N 按结果刷新，V 按加法器溢出位刷新；正常情况下 `~ACC+1` 不溢出，故 V=0。

## 6. 本讲小结

- ALU 用**一个 32 位加法器**统一实现 AND/OR/XOR/ABS/ADD/SUB/SUBC：减法靠 `~B + 1`（`adder_cin`），ABS 靠「负数取反加一」。
- **移位器 A** 在 ALU 输入侧，把 16 位 `reg_wrbus` 符号扩展（或零扩展）再左移，对应 ADD/SUB/LAC 等的定标；**移位器 B** 在输出侧，把累加器左移 0/1/4 位并取高/低字，对应 SACH/SACL。
- 端口 B 的来源由 `alu_pbsel` 在「移位器 A」与「乘法器 P 寄存器」间选择，是算术指令与乘加指令的交汇点。
- **OVM 饱和**（`reg_ovm`）仅在 ADD/SUB/SUBC 生效，把溢出结果钳位到 `0x7FFF_FFFF` 或 `0x8000_0000`；复位不清除 `reg_ovm`。
- **Z/N/V 标志**在累加器装载时刷新，V 独立于饱和逻辑——即使饱和了 V 仍置位，供 BV 指令检测。
- **SUBC** 用两个周期完成「一位」除法：周期 1 做条件减法并判符号，周期 2（`prev_subc`）做条件左移并写入商位，循环 16 次完成 16 位除法。

## 7. 下一步学习建议

本讲讲清了 ALU 的硬件通路与算术原理，下一步建议：

- **u2-l8（乘法器与 T/P 寄存器）**：弄清 `reg_p`（P 寄存器）如何作为 ALU 端口 B 的另一来源（`alu_pbsel = ALU_SOURCE_MUL`），把乘法与累加连成乘加（MAC）通路。
- **u3-l2（多周期指令时序与状态机）**：SUBC 的「EX 单周期 / ALU 双周期」差异正是多周期时序的典型案例，学完 u3-l2 你会更清楚 `ex_inst_cycle` 与 ALU 内部 `prev_subc` 的关系。
- **u3-l5（累加器算术逻辑类指令译码）**：本讲只点到 ADD/SUB/SUBC/ABS 等指令如何驱动 ALU 控制信号，u3-l5 会逐条剖析全部累加器类指令的微码译码。
- **docs/ 下的 TMS32010 User's Guide**：对照手册中 ALU、移位器、SUBC 除法例程的章节，验证本讲的静态分析，尤其是 SUBC 除法循环的标准写法与边界情况。
