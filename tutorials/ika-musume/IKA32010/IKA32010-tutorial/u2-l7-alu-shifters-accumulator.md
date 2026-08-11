# ALU、移位器、累加器与标志位

## 1. 本讲目标

本讲聚焦 IKA32010 的「运算心脏」——ALU 子模块及其周边的移位器、累加器与标志位。读完本讲，你应当能够：

- 说清 ALU 两个输入端口（A/B）的数据从哪里来、`adder_cin` 进位输入如何产生，以及 AND/OR/XOR/ABS/ADD/SUB/SUBC 七种运算是怎样用「一个加法器 + 多个多路选择」统一实现的；
- 区分**移位器 A**（ALU 输入定标）与**移位器 B**（累加器输出定标）各自的位置与作用，理解符号扩展抑制 `sha_ssup`；
- 解释 `reg_ovm` 溢出饱和模式如何把越界结果钳位到 32 位有符号极值，以及 SUBC 条件减法如何用「两个机器周期 + 一个内部锁存」完成「一位」恢复式除法；
- 独立追踪一条 SUBC 指令在两个机器周期内 `subc_divided / prev_subc / 累加器` 的变化，并画出时序。

本讲只讲硬件通路与算术原理，不展开每条指令的完整微码译码（那是 u3-l5 的主题），但会引用若干指令的微码片段来说明控制信号如何驱动 ALU。

## 2. 前置知识

在进入 ALU 之前，请确认你已经掌握下列概念（它们都在前置讲义中建立过）：

- **机器周期与相位**（u1-l4）：4 个 `i_EMUCLK` = 1 个 DSP 机器周期；`cyc_ncen`（`cyclecntr==3`，相位 3）是主工作拍，几乎所有寄存器都在 `cyc_ncen` 的上升沿更新。
- **内部写总线 `reg_wrbus`**（u2-l1）：16 位共享写总线，是 RAM、立即数、栈等数据汇入 ALU 的主干道。本讲的移位器 A 就挂在 `reg_wrbus` 上。
- **数据 RAM 读出**（u2-l5）：RAM 读端口的数据默认经 `reg_wrbus` → 移位器 A 送到 ALU 端口 B，所以一条 ADD 指令「天然」就能把 RAM 里的数加进累加器。
- **水平微码风格**（u3-l1 总览，本讲只需接受「默认值 + `casez` 按需覆盖」这一写法）：微码先给所有控制信号赋默认值，再在对应指令分支里覆盖少数信号。

此外需要一点数字电路常识：

- **二进制补码与减法**：减法 `A − B` 用加法器实现为 `A + (~B) + 1`，其中 `~B` 是按位取反，`+1` 体现为进位输入 `cin = 1`。
- **有符号溢出判定**：把「进入最高位的进位」与「最高位输出的进位」做异或，即为有符号溢出标志 OV。本讲后文会给出对应公式。
- **恢复式除法（restoring division）**：每一步先试减，够减则商位记 1 并保留试减结果，不够减则商位记 0 并恢复原值。SUBC 就是把这个过程硬件化。

## 3. 本讲源码地图

本讲涉及的全部代码都集中在两个文件：

| 文件 | 作用 | 本讲关注的内容 |
|------|------|----------------|
| `src/IKA32010.sv` | 顶层模块 + 4 个内嵌子模块 | 移位器 A/B、ALU 实例化、`reg_ovm`、`flag_output`、微码默认值、若干指令译码片段、末尾的 `IKA32010_alu` 子模块 |
| `src/IKA32010_mnemonics.sv` | 助记符常量字典 | `ALU_AND..ALU_SUBC`、`ALU_PBDATA_*`、`ALU_SOURCE_*` 等常量 |

关键代码点速查（永久链接行号均对应当前 HEAD `51bc1f0`）：

- 移位器 A：`src/IKA32010.sv:424-431`
- ALU 实例化（端口连线）：`src/IKA32010.sv:448-456`
- 移位器 B：`src/IKA32010.sv:458-471`
- 状态寄存器拼装 `flag_output`：`src/IKA32010.sv:479`
- `reg_ovm`（溢出模式位）：`src/IKA32010.sv:262-271`
- `IKA32010_alu` 子模块本体：`src/IKA32010.sv:1760-1906`
- ALU 控制常量：`src/IKA32010_mnemonics.sv:40-57`
- 微码里 ALU 相关默认值：`src/IKA32010.sv:551-587`

## 4. 核心概念与源码讲解

### 4.1 移位器 A 与移位器 B：ALU 的输入与输出定标

#### 4.1.1 概念说明

TMS32010 在 ALU 的「前」和「后」各放了一个**桶形移位器**（barrel shifter）——一种能在单个周期内把数据左移任意位数的纯组合电路。它们的作用是给数据「定标」（scaling），让 DSP 在做运算前后不必单独发一条移位指令。IKA32010 用两段组合逻辑忠实复刻了它们：

- **移位器 A（shifter-A，`sha_*`）**：位于 **ALU 输入侧**。它对来自 `reg_wrbus` 的 16 位数据做「符号扩展 + 左移」，结果送给 ALU 的端口 B。也就是说，进入 ALU 之前先放大/对齐。
- **移位器 B（shifter-B，`shb_*`）**：位于 **累加器输出侧**。它对累加器的 32 位值左移，再选高字或低字，结果经 `reg_wrbus` 写回 RAM（如 SACH/SACL）。

为什么要分前后两个移位器？因为 DSP 的典型运算是「乘加 + 定点缩放」：输入要按比例放大（移位器 A），结果又要按比例缩小并取高低字存回（移位器 B）。把它们做成 ALU 的固定配件，可以让一条 ADD/SACH 就完成「带移位的加法 / 带移位的存储」。

#### 4.1.2 核心流程

**移位器 A 的流程**（组合，零延迟）：

1. 取 `reg_wrbus`（16 位）。
2. 决定符号扩展方式：`sha_ssup=1` 时**零扩展**（高位补 0），否则**符号扩展**（高位补符号位 `reg_wrbus[15]`）。
3. 把 32 位结果左移 `sha_amt` 位，输出 `sha_output`。

**移位器 B 的流程**（组合，零延迟）：

1. 取累加器 `alu_acc_output`（32 位）。
2. 按 `shb_amt` 左移（源码只实现了 0、1、4 三档，见下方「源码精读」与练习）。
3. 按 `shb_mux` 选择输出高 16 位（`[31:16]`）或低 16 位（`[15:0]`），得 16 位 `shb_output`，随后进入 `reg_wrbus`。

#### 4.1.3 源码精读

移位器 A 的实现只有三行：

[src/IKA32010.sv:424-431](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L424-L431) —— 把 16 位 `reg_wrbus` 符号扩展（或零扩展）为 32 位，再左移 `sha_amt`：

```verilog
reg     [4:0]   sha_amt;     // ALU input shifter control
reg     [31:0]  sha_output;
always @(*) begin
    sha_output = sha_ssup ? {16'h0000, reg_wrbus} : {{16{reg_wrbus[15]}}, reg_wrbus};
    sha_output = sha_output << sha_amt;   //do arithmetic shift
end
```

> 注意：源码注释写的是 "arithmetic shift"，但实际运算符是 `<<`（逻辑左移）。这里的「算术」含义体现在**先做符号扩展、再左移**——左移本身不区分算术/逻辑，符号信息是被前一步保留下来的。对初学者只要记住：先扩展、再左移，低位补 0。

`sha_ssup`（sign-extension suppression，符号扩展抑制）由少数指令置 1，用于「无符号低字」操作。例如 SUBS（无符号减低字）就把 `sha_ssup=YES`，让 RAM 的 16 位数据零扩展后再送 ALU，从而当作无符号数处理——见 [src/IKA32010.sv:1003-1020](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1003-L1020)。

移位器 B 的实现稍特殊，用 `case` 只列了三档移位量：

[src/IKA32010.sv:458-471](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L458-L471) —— 按 `shb_amt` 选择 0/1/4 位移位，再用 `shb_mux` 选高低字：

```verilog
reg             shb_mux;
reg     [2:0]   shb_amt;
assign  shb_output = shb_mux ? shb_intermediate[31:16] : shb_intermediate[15:0];
always @(*) begin
    case(shb_amt)
        3'd0:    shb_intermediate = alu_acc_output;
        3'd1:    shb_intermediate = alu_acc_output << 1;
        3'd4:    shb_intermediate = alu_acc_output << 4;
        default: shb_intermediate = alu_acc_output;
    endcase
end
```

> **待本地验证的细节**：这个 `case` 只处理了 `shb_amt` 为 0、1、4 的情况，2、3（以及 5/6/7）会落入 `default`，相当于**不移位**。而 SACH 指令译码里 `shb_amt = if_opcodereg[10:8]`（3 位，可取 0–7），见 [src/IKA32010.sv:908-925](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L908-L925)。也就是说，编码出的 `SACH shift=2` 或 `shift=3` 在当前实现下会等价于 `shift=0`。这一点请在本讲实践里用仿真确认（手册允许 SACH 移位 0–4，源码与手册在 2/3 档上可能存在差异）。

`shb_output` 是 16 位，最终经 `reg_wrbus` 的选源 MUX 汇入总线（`WRBUS_SOURCE_SHB` 分支）：见 [src/IKA32010.sv:131-144](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L131-L144)。

#### 4.1.4 代码实践

**目标**：验证移位器 A 与移位器 B 的实际移位行为，重点核对待确认的「移位器 B 的 2/3 档」问题。

**操作步骤**（源码阅读型 + 仿真型）：

1. 打开 testbench `src/IKA32010_tb.v`，找到程序 ROM 的 `$readmemh` 加载方式与可写入的程序区（见 u1-l5）。
2. 在程序里安排两条指令：
   - 一条 `ADD dma, shift`，例如 `ADD` 带移位量 `shift=4`（操作码 `[11:8]=4`）。这会驱动移位器 A：`sha_amt = {1'b0, 4} = 5'd4`。先把一个非零数写到某 RAM 单元，再 ADD 它。
   - 一条 `SACH dma, shift`，分别测试 `shift=1` 和 `shift=2`。
3. 在仿真波形里观察 `sha_output`（移位器 A 输出）与 `shb_output`（移位器 B 输出），以及最终 `reg_wrbus`。

**需要观察的现象**：

- ADD 移位：`sha_output` 应等于「RAM 数据符号扩展后左移 `shift` 位」。例如 RAM=`0x0003`、`shift=4`，则 `sha_output = 0x0000_0030`。
- SACH 移位 1：`shb_output` 应为累加器高字左移 1 位后的 `[31:16]`。
- SACH 移位 2：根据源码 `case`，应**等价于不移位**（落入 default）。

**预期结果**：移位器 A 行为与手册一致；移位器 B 在 0/1/4 档正常，2/3 档表现为 0 档（待本地验证）。

> 若你暂时无法运行仿真，这仍是有效的「源码阅读型实践」：对照上面的 `case` 语句，自己推导 `shb_amt=2` 时的 `shb_intermediate` 取值（答案：走 `default` 分支 = `alu_acc_output`，不移位）。

#### 4.1.5 小练习与答案

**练习 1**：移位器 A 和移位器 B 都在左移，为什么 ALU 还需要两个移位器，而不是共用一个？

**参考答案**：因为它们的位置和职责不同。移位器 A 在 ALU **之前**，负责把 16 位输入数据定标后送入端口 B；移位器 B 在累加器 **之后**，负责把 32 位结果定标并选高低字写回 RAM。它们的输入宽度（16 vs 32）、输出宽度（32 vs 16）、被驱动的对象都不同，无法复用。

**练习 2**：`sha_ssup`（符号扩展抑制）被置 1 后，输入一个 `0xFFFF` 给移位器 A，扩展后是 `0x0000_FFFF` 还是 `0xFFFF_FFFF`？为什么 SUBS 指令需要它？

**参考答案**：是 `0x0000_FFFF`（零扩展）。SUBS 是「无符号减低字」，它要把数据当无符号数处理，所以必须抑制符号扩展，否则 `0xFFFF` 会被当成大负数。

---

### 4.2 IKA32010_alu 子模块：一个加法器统一七种运算

#### 4.2.1 概念说明

`IKA32010_alu` 是 IKA32010 的算术逻辑单元，也是整个核里最「精打细算」的部件。它支持七种运算：AND / OR / XOR / ABS / ADD / SUB / SUBC（常量见 [src/IKA32010_mnemonics.sv:40-47](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L40-L47)）。

它的设计精髓在于：**没有为每种运算单独做一个电路**，而是用「**一个加法器 + 一组多路选择**」覆盖全部七种运算。具体地：

- **逻辑运算**（AND/OR/XOR）：用各自的按位逻辑门实现，操作数只取端口 B 的低 16 位。
- **算术运算**（ADD/SUB/ABS/SUBC）：全部复用**同一个 32 位加法器**。其中减法靠「取反 + 进位」变成加法，ABS 靠「条件取反 + 进位」实现求绝对值，SUBC 靠带条件的两步加法实现试减。

这样做的好处是省面积、省功耗——在 FPGA 上一个 32 位加法器要占用不少逻辑单元，复用一份就能搞定加减和求绝对值。

ALU 有两个 32 位输入端口：

- **端口 A**（`i_ALU_PA`）：来自累加器自身的反馈（`alu_acc_output`），代表「当前累加器值」。
- **端口 B**（`i_ALU_PB`）：由 `alu_pbsel` 选择——来自移位器 A（`sha_output`，普通算逻指令）或乘法器的 P 寄存器（`reg_p`，乘加类指令如 APAC/PAC/LTA/LTD）。见 [src/IKA32010.sv:441](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L441) 与 [src/IKA32010.sv:451](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L451)。乘法器细节见 u2-l8。

#### 4.2.2 核心流程

ALU 在一个机器周期内的组合数据流（不含 SUBC 的跨周期部分）：

1. **产生进位输入 `adder_cin`**：默认 0；SUB/SUBC 时为 1（凑成补码减法的 +1）；ABS 时为 `PA[31]`（累加器为负时才 +1，把取反后的值补成绝对值）。
2. **生成端口 A**：默认 = 累加器；`alu_paz=1`（强制端口 A 归零）时为 0；ABS 时若累加器为负则取反；SUBC 第二周期有特殊路径。
3. **生成端口 B**：默认 = 端口 B 输入；SUB/SUBC 时取反；`alu_pbz=1`（强制端口 B 归零）时为 0；再用 `alu_pbdata` 选 long/high/low/byte 四种位宽切片。
4. **加法器计算** `port_a + port_b + adder_cin`，同时算出有符号溢出 `alu_ovfl`。
5. **按 `alu_modesel` 选输出**：逻辑运算走按位门，算术运算走加法器结果（叠加 OVM 饱和，见 4.3）。

补码减法的数学依据：

\[ A - B = A + (\sim B) + 1 \]

其中 `cin = 1` 提供 `+1`，`~B` 由「SUB/SUBC 时取反端口 B」实现。

有符号溢出的判定（`alu_ovfl`，对应标志 V）：

\[ \text{OV} = C_{\text{in},31} \oplus C_{\text{out},31} \]

即「进入第 31 位的进位」与「第 31 位输出的进位」相异或。源码用拆分加法来实现：低位 31 位单独加得到 `alu_adder31`，其最高位 `alu_adder31[31]` 正是 `C_{in,31}`；再单独算第 31 位的全加得到 `alu_adder1`，其最高位 `alu_adder1[1]` 正是 `C_{out,31}`。

#### 4.2.3 源码精读

ALU 子模块的端口与常量：

[src/IKA32010.sv:1760-1791](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1760-L1791) —— 注意它把 `i_CEN` 接成了 `cyc_ncen`（相位 3 主拍），并重复声明了一份 `ALU_*` 常量（子模块自用，不依赖 `include`）。

端口 A/B 与进位的生成（最关键的一段）：

[src/IKA32010.sv:1797-1837](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1797-L1837) —— 决定加法器的两个操作数与 `cin`：

```verilog
adder_cin = 1'b0;
if(subc_divided) adder_cin = 1'b1;                       //SUBC 试减成功后补的 +1
else begin
    if(i_ALU_MODESEL == ALU_SUB || i_ALU_MODESEL == ALU_SUBC) adder_cin = 1'b1; //补码减法的 +1
    else if(i_ALU_MODESEL == ALU_ABS) adder_cin = i_ALU_PA[31]; //负数求绝对值时 +1
end
...
if(i_ALU_MODESEL == ALU_ABS) port_a = i_ALU_PA[31] ? ~i_ALU_PA : i_ALU_PA; //ABS: 负则取反
...
if(i_ALU_MODESEL == ALU_SUB || i_ALU_MODESEL == ALU_SUBC) port_b = ~i_ALU_PB; //减法: 取反 B
else port_b = i_ALU_PB;
case(i_ALU_PBDATA)   //端口 B 的位宽切片
    ALU_PBDATA_LONGWORD: port_b = port_b;
    ALU_PBDATA_HIGHWORD: port_b = {port_b[31:16], 16'h0000};
    ALU_PBDATA_LOWWORD : port_b = {16'h0000, port_b[15:0]};
    ALU_PBDATA_BYTE    : port_b = {24'h0000_00, port_b[7:0]};
endcase
```

`alu_paz`（端口 A 归零，用于「不读累加器」的加载型指令如 LAC/ZAC/LACK）和 `alu_pbz`（端口 B 归零，用于 ABS/ZAC）也在这一段里处理。

加法器与溢出的实现：

[src/IKA32010.sv:1839-1846](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1839-L1846) —— 拆成「低 31 位加」与「第 31 位加」两段，正是为了同时拿到 `C_in,31` 和 `C_out,31`：

```verilog
wire [31:0] alu_adder31 = port_a[30:0] + port_b[30:0] + adder_cin; //含 C_in,31 = alu_adder31[31]
wire [1:0]  alu_adder1  = port_a[31] + port_b[31] + alu_adder31[31]; //含 C_out,31 = alu_adder1[1]
wire [31:0] alu_adder   = {alu_adder1[0], alu_adder31[30:0]};
wire        alu_ovfl    = alu_adder31[31] ^ alu_adder1[1];
```

七种运算的最终选择（叠加 OVM 饱和）：

[src/IKA32010.sv:1848-1863](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1848-L1863)：

```verilog
case(i_ALU_MODESEL)
    ALU_AND : alu_output = port_a & {16'h0000, port_b[15:0]};
    ALU_OR  : alu_output = port_a | {16'h0000, port_b[15:0]};
    ALU_XOR : alu_output = port_a ^ {16'h0000, port_b[15:0]};
    ALU_ABS : alu_output = alu_adder;
    ALU_ADD : alu_output = i_ALU_OVM ? (alu_ovfl ? sat : alu_adder) : alu_adder;
    ALU_SUB : alu_output = ...;   //同 ADD 的饱和逻辑
    ALU_SUBC: alu_output = ...;   //同 ADD 的饱和逻辑
endcase
```

> 注意逻辑运算只对端口 B 的**低 16 位**做按位运算（`{16'h0000, port_b[15:0]}`）。这意味着：AND 会把累加器高 16 位清零（与 0），而 OR/XOR 会保留累加器高 16 位（与 0 做 OR/XOR 不变）。这是忠实于 TMS32010 的行为——逻辑指令只影响低半部分，对高半部分 AND 清零、OR/XOR 保持。

减法/求绝对值都**没有独立的减法器**：SUB 复用加法器（取反 B + cin=1），ABS 也复用加法器（条件取反 A + cin=PA[31]）。这是「一个加法器统一加减」的核心证据。

**SUBC 的两周期机制**（本模块最巧妙的部分）：

[src/IKA32010.sv:1865-1872](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1865-L1872) —— SUBC 在 ALU 内部要花**两个机器周期**，靠两个内部寄存器 `prev_subc`、`subc_divided`、`prev_adder` 串联：

```verilog
always @(posedge i_EMUCLK) if(i_CEN) begin
    if(i_ALU_MODESEL == ALU_SUBC && alu_output[31] == 1'b0) subc_divided <= 1'b1; //试减结果非负→「够减」
    else subc_divided <= 1'b0;
    prev_subc <= i_ALU_MODESEL == ALU_SUBC;   //标记「下一拍要完成 SUBC 第二步」
    prev_adder <= alu_output;                  //保存第一拍的试减结果
end
```

关键点（务必理解）：

- **第一周期**（EX 单元给出 `ALU_SUBC`）：ALU 计算「累加器 − (操作数 << 15)」的试减结果。若结果非负（`alu_output[31]==0`），`subc_divided` 置 1（够减），否则置 0。同时把试减结果存进 `prev_adder`，把 `prev_subc` 置 1。**注意：这一拍累加器并不会被打入**——因为 SUBC 译码里并没有把 `alu_acc_ld` 设为 YES（见 [src/IKA32010.sv:965-982](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L965-L982)）。
- **第二周期**（`prev_subc==1`）：此时 EX 单元已经推进到**下一条指令**（SUBC 在 EX 侧只占 1 个机器周期），但 ALU 看到 `prev_subc==1`，会**忽略当前指令的 `alu_modesel`**，走专门的 `prev_subc` 分支完成第二步并把结果打入累加器。这就是 SUBC 译码里那句警告 `!!! next instruction cannot use the ACC !!!` 的含义：下一拍 ALU 正在为 SUBC 收尾，会改写累加器，所以紧随 SUBC 的指令绝不能依赖累加器。

第二周期的数据通路（`prev_subc` 分支）在 [src/IKA32010.sv:1810](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1810)、[src/IKA32010.sv:1820](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1820)、[src/IKA32010.sv:1850](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1850)：

- `port_a = subc_divided ? 0 : (累加器 << 1)`
- `port_b = subc_divided ? (prev_adder << 1) : 0`
- `adder_cin = subc_divided ? 1 : 0`
- `alu_output = port_a + port_b + adder_cin`
- `alu_acc_ld = prev_subc | i_ALU_ACC_LD`（见 [src/IKA32010.sv:1875](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1875)），所以第二拍累加器必定被打入。

把它展开成 SUBC 的语义就是**一位恢复式除法**：

- 够减（`subc_divided=1`）：`ACC ← ((ACC − op<<15) << 1) + 1`，末尾的 `+1` 就是商位 = 1。
- 不够减（`subc_divided=0`）：`ACC ← ACC << 1`，商位 = 0。

#### 4.2.4 代码实践

**目标**：完整追踪一条 SUBC 指令在两个机器周期内 `subc_divided / prev_subc / 累加器` 的变化，理解其除法原理；并讨论 OVM 饱和对 SUBC 的影响。

**初始条件**：累加器 `ACC = 0x0000_8010`，RAM 中某单元（经 AR 间接寻址）`RAM = 0x0001`，`OVM = 0`，`alu_pbsel = SHFT`（默认）。

**第一周期**（`prev_subc=0`，`ALU_SUBC`，`sha_amt=15`）：

| 信号 | 取值 | 说明 |
|------|------|------|
| `sha_output` | `0x0000_8000` | `0x0001` 符号扩展为 `0x0000_0001`，左移 15 位 |
| `port_a` | `0x0000_8010` | 累加器反馈 |
| `port_b` | `0xFFFF_7FFF` | SUBC 取反端口 B：`~0x0000_8000` |
| `adder_cin` | `1` | SUBC 凑补码 +1 |
| `alu_output` | `0x0000_0010` | `0x0000_8010 + 0xFFFF_7FFF + 1 = 0x1_0000_0010`，取低 32 位 |
| `alu_output[31]` | `0` | 非负 → 够减 |
| `subc_divided`（周期末锁存） | `1` | 够减 |
| `prev_adder`（周期末锁存） | `0x0000_0010` | 保存试减结果 |
| `prev_subc`（周期末锁存） | `1` | 标记第二周期 |
| 累加器 | **未变**（仍 `0x0000_8010`） | 第一周期 `alu_acc_ld=0` |

**第二周期**（`prev_subc=1`，`subc_divided=1`，EX 单元已在执行下一条指令）：

| 信号 | 取值 | 说明 |
|------|------|------|
| `port_a` | `0x0000_0000` | `subc_divided ? 0 : ACC<<1` → 0 |
| `port_b` | `0x0000_0020` | `subc_divided ? prev_adder<<1 : 0` → `0x0010<<1` |
| `adder_cin` | `1` | `subc_divided ? 1 : 0` → 1 |
| `alu_output` | `0x0000_0021` | `0 + 0x0020 + 1` |
| 累加器（周期末锁存） | `0x0000_0021` | `alu_acc_ld = prev_subc = 1` |

**结果解释**：累加器从 `0x0000_8010` 变成 `0x0000_0021`。最低位的 `1` 正是「够减」时写入的**商位**。这就是一次「一位」除法。连续执行 16 次 SUBC（每次操作数不变），就能完成 16 位除以 16 位的除法：商出现在累加器低 16 位，余数出现在高 16 位。

**关于 OVM 饱和（待本地验证）**：源码在 SUBC 的输出选择里**也套了 OVM 饱和逻辑**（[src/IKA32010.sv:1859](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1859)）。但 SUBC 除法**依赖二进制补码的自然回绕来判断符号**，一旦 `OVM=1`，试减结果会被钳位（见 4.3 的饱和值），`alu_output[31]` 不再反映真实符号，`subc_divided` 会判错，除法就被破坏。因此**正确使用 SUBC 必须 `OVM=0`**（这也是 TMS32010 手册的要求）。建议你在仿真里做对照实验：

1. `OVM=0`：按上表完成一次 SUBC，累加器得到 `0x0000_0021`。
2. `OVM=1`：构造一个会让第一周期 `alu_ovfl=1` 的场景，观察 `alu_output` 是否被钳位成 `0x7FFF_FFFF` 或 `0x8000_0000`，并确认此时除法结果出错。

> 说明：本实践给出了基于源码推导的预期值；具体的波形数值请以本地仿真为准（「待本地验证」）。如果你暂时无法仿真，也可以纯靠阅读上述源码与公式完成推导——上面的两张表就是「源码阅读型实践」的产物。

#### 4.2.5 小练习与答案

**练习 1**：SUB 在源码里并没有独立的减法电路，它是怎么用加法器实现 `ACC − X` 的？

**参考答案**：减法被改写为补码加法 `ACC − X = ACC + (~X) + 1`。源码在 SUB（和 SUBC）时把端口 B 取反（`port_b = ~i_ALU_PB`），并把进位输入 `adder_cin` 设为 1 提供 `+1`，于是加法器算出的就是差值。

**练习 2**：AND、OR、XOR 三条指令对累加器**高 16 位**的影响分别是什么？为什么？

**参考答案**：源码里三者都是 `port_a {&,|,^} {16'h0000, port_b[15:0]}`。端口 B 的高 16 位是 0，所以：AND 把高 16 位清零（与 0），OR/XOR 保持高 16 位不变（与 0 做 OR/XOR）。这符合 TMS32010：逻辑指令只在低半部分生效。

**练习 3**：为什么 SUBC 译码里要写 `!!! next instruction cannot use the ACC !!!`？

**参考答案**：因为 SUBC 在 EX 单元只占 1 个机器周期，第二周期 ALU 会利用 `prev_subc` 自主完成第二步并把结果写入累加器；此时 EX 单元已经在执行下一条指令。如果下一条指令也要读/写累加器，就会被 SUBC 的收尾写覆盖（或读到尚未更新的值），所以下一条指令不能使用累加器。

---

### 4.3 累加器、OVM 饱和与 Z/N/V 标志

#### 4.3.1 概念说明

运算做完后，要把结果存起来、并记录运算的「状态」。IKA32010 用三样东西承接：

- **累加器（ACC，32 位）**：ALU 的「工作台」，几乎所有算逻运算的结果都写回这里。它**就住在 ALU 子模块内部**（端口 `o_ALU_ACC_OUTPUT`，见 [src/IKA32010.sv:1774](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1774)），顶层并没有一个独立的累加器寄存器——它的输出 `alu_acc_output` 同时回喂给端口 A，形成「累加」闭环。
- **OVM（溢出模式位）**：一个 1 位系统寄存器 `reg_ovm`。`OVM=1` 时，算术运算一旦发生有符号溢出，结果会被**钳位**到 32 位有符号的极大值 `0x7FFF_FFFF`（正溢出）或极小值 `0x8000_0000`（负溢出），而非回绕。这对音频/DSP 很有用——饱和比回绕听上去更可接受。
- **三个标志位 Z/N/V**：`Z`（Zero，结果为零）、`N`（Negative，结果为负）、`V`（oVerflow，有符号溢出），由 ALU 子模块输出（`o_Z/o_N/o_V`，见 [src/IKA32010.sv:1777](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1777)），供条件分支（BZ/BNZ/BLZ/BGEZ/BV 等）判断。

#### 4.3.2 核心流程

**累加器写回**：在每个 `cyc_ncen` 边沿，若 `alu_acc_ld=1`（普通算逻指令置位，或 SUBC 第二周期由 `prev_subc` 强制），累加器装入 `alu_output`；复位时装入 0。

**OVM 饱和**：当 `reg_ovm=1` 且 `alu_ovfl=1` 时，算术结果按 `alu_adder31[31]`（即 `C_in,31`）选择钳位值：

\[ \text{sat} = \begin{cases} \text{0x7FFF\_FFFF} & \text{若 } C_{\text{in},31}=1 \text{（正溢出）}\\ \text{0x8000\_0000} & \text{若 } C_{\text{in},31}=0 \text{（负溢出）} \end{cases} \]

源码用一行位拼接实现两档选择：`{~alu_adder31[31], {31{alu_adder31[31]}}}`。当 `C_in,31=1`，得 `{0, 31个1} = 0x7FFF_FFFF`；当 `C_in,31=0`，得 `{1, 31个0} = 0x8000_0000`。

**标志更新**：每个 `cyc_ncen` 边沿，若 `alu_acc_ld=1`，则 `Z = (alu_output==0)`、`N = alu_output[31]`、`V = alu_ovfl`；否则 `Z/N` 保持，`V` 可由专门的 `alu_v_set/alu_v_rst` 单独改写（供 LST 指令装入状态位用）。

**OVM 与 INTM 的写入**：`reg_ovm` 用 `{set, rst}` 真值表更新：SOVM 置位、ROVM 复位、LST 从状态位装入（见 u3-l4 的状态位读写映射）。

#### 4.3.3 源码精读

累加器写回控制（复位清零，`cyc_ncen` + `alu_acc_ld` 双门控）：

[src/IKA32010.sv:1874-1881](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1874-L1881)：

```verilog
wire alu_acc_ld = prev_subc | i_ALU_ACC_LD;   //SUBC 第二周期强制打入
always @(posedge i_EMUCLK) begin
    if(!i_RST_n) o_ALU_ACC_OUTPUT <= 32'h0000_0000;
    else begin
        if(i_CEN) if(alu_acc_ld) o_ALU_ACC_OUTPUT <= alu_output;
    end
end
```

OVM 饱和的一行实现（在 ADD/SUB/SUBC 的输出选择里）：

[src/IKA32010.sv:1857](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1857)：

```verilog
ALU_ADD : alu_output = i_ALU_OVM ? (alu_ovfl ? {~alu_adder31[31], {31{alu_adder31[31]}}} : alu_adder) : alu_adder;
```

标志位的更新（含复位初值 `Z=1, N=0, V=0`，以及 `V` 的独立 set/rst）：

[src/IKA32010.sv:1883-1904](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1883-L1904)：

```verilog
if(alu_acc_ld) begin
    o_Z <= alu_output == 32'h0000_0000;
    o_N <= alu_output[31];
    o_V <= alu_adder31[31] ^ alu_adder1[1];        //有符号溢出
end
else begin
    case({i_ALU_V_SET, i_ALU_V_RST})               //LST 等指令单独改 V
        2'b10: o_V <= 1'b1;
        2'b01: o_V <= 1'b0;
        default: o_V <= o_V;
    endcase
end
```

`reg_ovm` 本体（注意注释：**复位不会清除 OVM**，初值为 0）：

[src/IKA32010.sv:262-271](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L262-L271) —— 由 SOVM/ROVM/LST 驱动（SOVM 见 [src/IKA32010.sv:743-750](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L743-L750)、ROVM 见 [src/IKA32010.sv:734-741](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L734-L741)、LST 的 OVM 位见 [src/IKA32010.sv:667-668](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L667-L668)）。

最后，标志位会被拼成 16 位「状态寄存器」整体输出，供 SSR 存回 RAM 或 LST 装入：

[src/IKA32010.sv:479](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L479)：

```verilog
assign flag_output = {alu_flag_ovfl, reg_ovm, reg_intm, 4'b1111, reg_arp, 6'b111111, 1'b1, reg_dp};
```

对照后得到状态寄存器位域（高位在左）：

| 位 | 15 | 14 | 13 | 12–9 | 8 | 7–2 | 1 | 0 |
|----|----|----|----|------|---|-----|---|---|
| 内容 | OV | OVM | INTM | 1(保留) | ARP | 1(保留) | 1(无关) | DP |

> **待对照手册验证的细节**：源码里只要发生累加器写回（`alu_acc_ld`），Z/N/V 都会重算——包括 AND/OR/XOR 这些逻辑指令（它们也会把 `alu_acc_ld` 置 1）。而 TMS32010 手册通常表述逻辑指令不影响 OV。源码此处对 V 的计算是基于加法器的，逻辑指令下可能给出「无意义」的 V 值。建议结合仿真确认逻辑指令后 V 标志的实际行为。

#### 4.3.4 代码实践

**目标**：用一个会溢出的 ADD，直接观察 OVM 饱和把结果钳位到极值的过程，并核对 Z/N/V 标志。

**操作步骤**：

1. 先执行 `LACK 0x7F`（把累加器低字节设为 `0x7F`，再配合其它方式构造一个接近正上限的值；或用 `LAC`/`ZALH` 直接装入高字）。最简单的构造：用 `ZALH` 把 `0x7FFF` 装入累加器高字（累加器 = `0x7FFF_0000`），再用 `ADDS` 加一个 `0xFFFF`（无符号低字）。
2. 分别在 `OVM=0`（`ROVM`）和 `OVM=1`（`SOVM`）两种模式下重复上述加法。
3. 在仿真波形里观察累加器 `o_ALU_ACC_OUTPUT` 与标志 `o_V`。

**需要观察的现象**：

- `OVM=0`：正溢出时累加器发生**回绕**（wrap），`V=1`。
- `OVM=1`：同样的加法，累加器被钳位为 `0x7FFF_FFFF`（正溢出）或 `0x8000_0000`（负溢出），`V=1`。
- `Z`、`N`：反映**钳位后**的值（`0x7FFF_FFFF` → `N=0, Z=0`；`0x8000_0000` → `N=1, Z=0`）。

**预期结果**：OVM 饱和按上式钳位，标志位反映钳位后的累加器值。若仿真结果与本推导不一致，请以仿真为准并记录差异（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：复位后，Z/N/V 三个标志的初值分别是什么？为什么 Z 的初值是 1？

**参考答案**：`Z=1, N=0, V=0`（见 [src/IKA32010.sv:1885-1886](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1885-L1886)）。Z=1 是因为复位后累加器被清零，而「累加器为 0」这件事对应零标志为真。

**练习 2**：复位会不会把 `reg_ovm` 清零？这有什么影响？

**参考答案**：不会（注释明确写 `RESET will not clear this bit!!!`，且复位分支里没有对 `reg_ovm` 赋值）。影响是：上电后 OVM 的值取决于 FPGA 触发器的初值（源码声明 `reg_ovm = 1'b0` 给了初值 0，但严格来说复位脉冲不会改变它）。若你依赖一个确定的 OVM 初值，最好在程序开头显式执行一次 ROVM 或 SOVM。

**练习 3**：状态寄存器里的「保留位」为什么是 1 而不是 0？

**参考答案**：源码 `flag_output` 里多处写死 `4'b1111`、`6'b111111`、`1'b1`，这是为了忠实复刻 TMS32010 状态寄存器里那些「读出恒为 1」的保留位。用 SSR 把状态存回 RAM 时，这些位也会是 1，与原芯片行为一致。

## 5. 综合实践

把本讲的「移位器 A → ALU → 累加器 → 移位器 B → 标志」串成一条完整的定点运算链：

**任务**：用一组指令计算 `y = |(x << 4) − k|`，并把结果的高字存回 RAM。其中 `x`、`k` 是 RAM 里的两个 16 位定点数。

**建议步骤**：

1. 把 `x` 装入累加器：用 `LAC x, 0`（`LAC` 设 `alu_paz=YES`、`sha_amt=0`，把 RAM 数据直接经移位器 A 送端口 B、清零端口 A 后相加，等价于「装入」）。
2. 左移 4 位并减 `k`：用 `SUB k, 4`（`sha_amt=4` 让移位器 A 把 `k` 左移 4 位，ALU 做补码减法）。
3. 求绝对值：用 `ABS`（设 `ALU_ABS`，条件取反 + `cin=PA[31]`）。
4. 存高字：用 `SACH y, 0`（移位器 B 不移位、选高字，经 `reg_wrbus` 写回 RAM）。

**追踪要点**（在仿真或源码阅读中确认）：

- 每一步 `reg_wrbus` 的数据来自哪里（RAM / 移位器 B）；
- 移位器 A 的 `sha_amt` 与 `sha_ssup` 在 LAC、SUB 时的取值；
- 每一步执行后 Z/N/V 的变化（尤其 SUB 后的 V、ABS 后的 N）；
- 若 `x << 4 − k` 溢出，分别测试 `OVM=0` 与 `OVM=1` 下最终 `y` 的差异。

> 这个任务同时用到了移位器 A（输入定标）、ALU（减法、绝对值）、累加器（中间结果）、移位器 B（输出定标）和标志位（判断溢出/符号），是本讲内容的一次综合演练。具体数值请以本地仿真为准。

## 6. 本讲小结

- ALU 用「**一个加法器 + 多路选择**」统一了 AND/OR/XOR/ABS/ADD/SUB/SUBC 七种运算；减法靠取反端口 B + `cin=1` 变加法，ABS 靠条件取反端口 A + `cin=PA[31]`。
- 两个输入端口：端口 A 是累加器反馈（可被 `alu_paz` 清零），端口 B 由 `alu_pbsel` 在移位器 A 与乘法器 P 寄存器间选择，并经 `alu_pbdata` 做 long/high/low/byte 切片。
- 有符号溢出 `alu_ovfl = C_in,31 ⊕ C_out,31`，源码用「低 31 位加 + 第 31 位加」的拆分加法器同时拿到这两个进位。
- **移位器 A** 在 ALU 输入侧做符号扩展 + 左移（`sha_ssup` 抑制符号扩展）；**移位器 B** 在累加器输出侧左移并选高低字，源码只实现了 0/1/4 三档移位（2/3 档待本地验证）。
- **累加器住在 ALU 子模块内部**，写回由 `cyc_ncen` + `alu_acc_ld` 门控；`reg_ovm=1` 时算术溢出会被钳位到 `0x7FFF_FFFF` / `0x8000_0000`（复位不清除 OVM）。
- **SUBC 是两周期指令**：第一周期试减并锁存 `subc_divided/prev_subc/prev_adder`，第二周期由 `prev_subc` 自主完成「够减则 `(diff<<1)+1`、不够减则 `ACC<<1`」，等价于一位恢复式除法；使用 SUBC 必须 `OVM=0`，且其后一条指令不能使用累加器。

## 7. 下一步学习建议

- **乘法器与 T/P 寄存器（u2-l8）**：本讲多次提到端口 B 的另一个来源是乘法器的 P 寄存器（`alu_pbsel=ALU_SOURCE_MUL`）。下一讲会讲清 16×16 有符号乘法、T 寄存器锁存与 P 寄存器输出，以及 APAC/PAC/LTA/LTD 如何把乘积经 ALU 加到累加器。
- **微码架构总览（u3-l1）**：本讲引用了大量「默认值 + `casez` 覆盖」的片段，u3-l1 会系统讲解这套水平微码框架。
- **累加器类指令完整译码（u3-l5）**：本讲只点到了 ADD/SUB/SUBC/ABS/SACH/SACL/ZAC 等指令如何驱动 ALU 控制信号，u3-l5 会逐条剖析全部累加器类指令。
- **建议同步阅读**：`docs/` 下 TMS32010 用户手册中关于「ALU、累加器、移位器、状态寄存器、SUBC 除法算法」的章节，把源码实现与官方描述对照，尤其关注本讲标注的「待本地验证」点（移位器 B 的 2/3 档、逻辑指令对 V 的影响）。
