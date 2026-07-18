# 指令译码器：从 32 位字到控制信号

## 1. 本讲目标

学完本讲后，你应该能够：

- 理解 PicoRV32 是如何把从内存取回的一个 32 位字（`mem_rdata`）解释成"这条指令要做什么"的。
- 看懂 `instr_addi`、`instr_lui`、`instr_jal` 这样的一根根"一位译码信号"是从 `opcode`/`funct3`/`funct7` 字段如何组合出来的。
- 掌握 RISC-V 五类立即数（I/S/B/U/J）在 `decoded_imm` / `decoded_imm_j` 中是怎样拼装和符号扩展的。
- 弄清楚 `decoder_trigger` / `decoder_pseudo_trigger` 这两个触发信号分别在什么时钟沿把译码结果"锁存"下来，从而理解译码器的两级时序。

本讲是打开 CPU 黑盒后的第一站：我们不关心指令"怎么执行"，只关心 CPU"怎么认出"一条指令。

## 2. 前置知识

在读本讲前，请确认你已经了解（对应前置讲义 u3-l2）：

- **RISC-V 指令的基本字段划分**：一条 32 位指令被切成几个固定字段，最重要的是最低 7 位的 `opcode`（操作码）、`[14:12]` 的 `funct3`、`[31:25]` 的 `funct7`，以及 `rd`（目的寄存器，`[11:7]`）、`rs1`（第一源寄存器，`[19:15]`）、`rs2`（第二源寄存器，`[24:20]`）。
- **原生内存接口**：CPU 通过 `mem_valid`/`mem_ready` 握手从内存读数据，读回的 32 位字出现在输入端口 `mem_rdata` 上。取指时读回的就是指令本身。
- **Verilog 时序基础**：`always @(posedge clk)` 块里的非阻塞赋值 `<=` 在时钟沿生效，"本拍计算、下拍可见"。

如果你还不熟悉 RISC-V 字段布局，先记住这张图，本讲会反复用到：

```
 31       25 24   20 19   15 14  12 11    7 6      0
+-----------+-------+-------+------+-------+--------+
|  funct7   |  rs2  |  rs1  |funct3|   rd  | opcode |
+-----------+-------+-------+------+-------+--------+
```

一个关键直觉：**译码器本质上是一张巨大的"查表电路"**。输入是 32 位的指令字，输出是一堆一位的"是/否"信号（这是不是一条 `addi`？是不是 `lw`？）加上几个寄存器编号和一个立即数。后续的数据通路（ALU、内存接口）就靠这些一位信号来决定做什么运算。

## 3. 本讲源码地图

本讲几乎全部内容都集中在一个文件里：

| 文件 | 作用 |
|------|------|
| [picorv32.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v) | 整个 CPU 核。本讲关注其中标注 `// Instruction Decoder` 的区段（约第 644–1167 行）。 |

译码器涉及的关键代码点：

1. **译码信号声明**：`instr_*`、`decoded_rd/rs1/rs2`、`decoded_imm` 等（第 644–662 行）。
2. **非法指令检测** `instr_trap`（第 679–685 行）。
3. **第一级译码**：取指完成时从 `mem_rdata_latched` 提取 opcode 组、寄存器号、J 型立即数，并展开压缩指令（第 866–934 行）。
4. **第二级译码**：在 `decoder_trigger` 触发时，从 `mem_rdata_q` 生成最终的 `instr_*` 一位信号与 `decoded_imm`（第 1037–1133 行）。
5. **触发信号生成**：`decoder_trigger <= mem_do_rinst && mem_done`（第 1446 行）。
6. **指令字锁存**：`mem_rdata_q` 的更新（第 430–433 行）。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 指令译码一位信号**：从 opcode/funct 到 `instr_addi` 这类一位信号。
- **4.2 立即数生成**：I/S/B/U/J 五类立即数如何拼装进 `decoded_imm`。
- **4.3 译码触发时序**：`decoder_trigger` 与 `decoder_pseudo_trigger` 何时把译码结果锁存下来。

### 4.1 指令译码一位信号

#### 4.1.1 概念说明

PicoRV32 给 RV32I/M/C 中每一条指令都分配了一根"一位信号"，例如 `instr_addi`、`instr_lw`、`instr_jal`、`instr_mul`（乘法走 PCPI）等。这些信号在任意时刻**至多有一个为 1**（否则就是非法指令）。

为什么不直接用一个 `case` 把指令编成数值（比如 `OPCODE_ADDI=5'd0`）？因为一位信号实现起来非常省硬件：每根信号就是一片与/或逻辑的输出，综合后直接成为后续 ALU、内存控制逻辑的使能线，不需要再"查一次表"。这种风格被称为 **one-hot 译码**（一位热码译码）。

非法指令的判定也很优雅：把所有合法的 `instr_*` 拼成一个大向量做按位或归约，若结果为 0，说明没有任何一条已知指令被选中，即 `instr_trap`。

#### 4.1.2 核心流程

PicoRV32 的译码分成**两级**，分别由不同的触发条件驱动：

1. **第一级（取指完成时）**：当一次取指传输完成（`mem_do_rinst && mem_done`）时，从刚取到的指令字 `mem_rdata_latched` 中：
   - 比对 `opcode`（`[6:0]`）得到"指令组"中间信号，例如 `is_alu_reg_imm`（OP-IMM 组）、`is_lb_lh_lw_lbu_lhu`（LOAD 组）、`is_alu_reg_reg`（OP 组）；
   - 提取 `decoded_rd`、`decoded_rs1`、`decoded_rs2`；
   - 预先算好 J 型跳转立即数 `decoded_imm_j`；
   - 若开启了压缩指令集（`COMPRESSED_ISA`），把 16 位压缩指令在此处"改写"成等价的组信号与寄存器号。
2. **第二级（译码触发时）**：在 `decoder_trigger` 为 1 的那一拍，再结合 `funct3`/`funct7`，把"指令组"细分成具体的 `instr_addi`、`instr_slti` 等一位信号，并生成最终立即数 `decoded_imm`。

之所以分两级，是因为第一级的输入 `mem_rdata_latched` 是"刚刚从内存回来、还没被寄存"的字，而第二级的输入 `mem_rdata_q` 是"已经锁存过一拍"的字；两级各自享用自己时序最合适的输入，避免把过多组合逻辑堆在同一条关键路径上。

#### 4.1.3 源码精读

**一位信号的声明**（[picorv32.v:644-662](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L644-L662)）：这里把每条指令声明为一个 1 位 `reg`，并把立即数、寄存器号、触发信号也一并声明。注意 `decoded_rs2` 固定为 5 位（即使 RV32E 只有 16 个寄存器），而 `decoded_rd`/`decoded_rs1` 用 `regindex_bits` 位宽以适配 RV32E。

**非法指令检测**（[picorv32.v:679-685](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L679-L685)）：把所有合法一位信号用拼接算子 `{...}` 组成向量，再对整体做归约或 `|{...}` 的反相。如果没有任何一位被选中，`instr_trap` 就为 1。`assign` 说明这是一根纯组合线。

第一级译码片段（[picorv32.v:866-884](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L866-L884)）做了两件事：用 `opcode` 比较得到指令组与少数能直接确定的指令，并提取三个寄存器号：

```verilog
instr_lui     <= mem_rdata_latched[6:0] == 7'b0110111;   // LUI
instr_auipc   <= mem_rdata_latched[6:0] == 7'b0010111;   // AUIPC
instr_jal     <= mem_rdata_latched[6:0] == 7'b1101111;   // JAL
instr_jalr    <= mem_rdata_latched[6:0] == 7'b1100111 &&
                 mem_rdata_latched[14:12] == 3'b000;     // JALR（还要求 funct3=0）
...
is_alu_reg_imm <= mem_rdata_latched[6:0] == 7'b0010011;  // OP-IMM 组（addi/slti/...）
is_alu_reg_reg <= mem_rdata_latched[6:0] == 7'b0110011;  // OP 组（add/sub/...）

decoded_rd  <= mem_rdata_latched[11:7];
decoded_rs1 <= mem_rdata_latched[19:15];
decoded_rs2 <= mem_rdata_latched[24:20];
```

第二级译码片段（[picorv32.v:1037-1062](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1037-L1062)）把"组 + funct3"细分到具体指令。以 OP-IMM 组为例，`funct3` 的 8 种取值里有 6 种直接对应一个 `instr_*`：

```verilog
instr_addi  <= is_alu_reg_imm && mem_rdata_q[14:12] == 3'b000;  // ADDI
instr_slti  <= is_alu_reg_imm && mem_rdata_q[14:12] == 3'b010;  // SLTI
instr_sltiu <= is_alu_reg_imm && mem_rdata_q[14:12] == 3'b011;  // SLTIU
instr_xori  <= is_alu_reg_imm && mem_rdata_q[14:12] == 3'b100;  // XORI
instr_ori   <= is_alu_reg_imm && mem_rdata_q[14:12] == 3'b110;  // ORI
instr_andi  <= is_alu_reg_imm && mem_rdata_q[14:12] == 3'b111;  // ANDI
```

注意 `funct3 == 3'b001` 和 `3'b101` 被单独留给移位指令（`slli`/`srli`/`srai`），因为它们还要再看 `funct7` 来区分算术/逻辑右移（[picorv32.v:1064-1066](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1064-L1066)）：

```verilog
instr_slli  <= is_alu_reg_imm && mem_rdata_q[14:12] == 3'b001 && mem_rdata_q[31:25] == 7'b0000000;
instr_srli  <= is_alu_reg_imm && mem_rdata_q[14:12] == 3'b101 && mem_rdata_q[31:25] == 7'b0000000;
instr_srai  <= is_alu_reg_imm && mem_rdata_q[14:12] == 3'b101 && mem_rdata_q[31:25] == 7'b0100000;
```

OP 组（`add`/`sub`/`sll`...）的译码方式完全对称，只是把 `is_alu_reg_imm` 换成 `is_alu_reg_reg`，并用 `funct3`+`funct7` 区分（见 [picorv32.v:1068-1077](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1068-L1077)）。

#### 4.1.4 代码实践

> **实践目标**：把一条具体指令 `addi x2, x2, 1`（十六进制 `0x00110113`）"喂"给译码器，逐字段追踪它如何点亮 `instr_addi` 这根线。

**操作步骤（纯源码阅读，不需要运行）：**

1. 先把 `0x00110113` 写成 32 位二进制：

   ```
   0x00110113 = 0000_0000 0001_0001 0000_0001 0001_0011
   ```

2. 对照字段表切分：

   | 字段 | 位段 | 二进制值 | 十进制/含义 |
   |------|------|----------|-------------|
   | opcode | `[6:0]` | `0010011` | OP-IMM 组 → `is_alu_reg_imm=1` |
   | rd | `[11:7]` | `00010` | 2（x2） |
   | funct3 | `[14:12]` | `000` | ADDI |
   | rs1 | `[19:15]` | `00010` | 2（x2） |
   | rs2 | `[24:20]` | `10001` | 17（addi 不用） |
   | imm | `[31:20]` | `000000000001` | 1 |

3. 在 [picorv32.v:877](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L877) 确认：`opcode == 0010011` 使第一级的 `is_alu_reg_imm <= 1`，同时 `decoded_rd <= 2`、`decoded_rs1 <= 2`（第 882–883 行）。

4. 下一拍 `decoder_trigger` 拉高，进入第二级 [picorv32.v:1057](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1057)：`is_alu_reg_imm && funct3==000` → `instr_addi <= 1`。

**预期结果**：你应得到一张"32 位字段 → 译码信号"的映射表（如上），证明 `0x00110113` 唯一点亮 `instr_addi`，并把 `decoded_rd=2`、`decoded_rs1=2`、立即数 `1` 准备好供后续 ALU 使用。若你把 `funct3` 改成 `010`（即 `0x02110193` 一类），点亮的就变成 `instr_slti`。

> 本实践无需运行仿真，属于"源码阅读型实践"。如果想进一步验证，可在 u1-l3 学过的 `testbench_ez.v` 的 `memory[]` 里把某条指令改成 `32'h00110113`，运行 `make test_ez`，对照打印观察译码后的取指/写回行为（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：指令 `0x00208093` 的 opcode 是什么？它会点亮哪个 `instr_*`？（提示：先算 `[6:0]`。）

答案：`0x00208093` 的 `[6:0] = 0010011`（OP-IMM），`funct3 = [14:12] = 001`，`funct7 = [31:25] = 0000000`，故点亮 `instr_slli`（逻辑左移立即数）。

**练习 2**：为什么 `instr_jalr` 在第一级就需要同时检查 `funct3 == 0`，而 `instr_addi` 却要等到第二级才确定？

答案：`JALR` 在 RISC-V 中只有一个合法 `funct3`（000），在第一级用 opcode+funct3 就能唯一确定，于是提前点亮以便跳转逻辑尽早使用；而 OP-IMM 组里的 `addi/slti/...` 共享同一个 opcode，必须等第二级才能用 funct3 区分开。

### 4.2 立即数生成

#### 4.2.1 概念说明

RISC-V 的立即数有个著名的"反直觉"设计：**所有立即数都是带符号的，且符号位永远是指令的第 31 位**。为了让符号位在硬件上始终落在同一个位置（便于符号扩展），不同指令类型的立即数字段在 32 位指令里被打散到了不同的位置。共五种格式：I、S、B、U、J。

- **I 型**（`addi`/`lw`/`jalr` 等）：`imm[11:0] = inst[31:20]`，12 位。
- **S 型**（`sw`/`sh`/`sb`）：`imm[11:5]=inst[31:25]`，`imm[4:0]=inst[11:7]`，12 位。
- **B 型**（分支 `beq` 等）：13 位，最低位恒 0（`imm[12|10:5|4:1|11] = inst[31|30:25|11:8|7]`）。
- **U 型**（`lui`/`auipc`）：`imm[31:12]=inst[31:12]`，低 12 位为 0，20 位。
- **J 型**（`jal`）：21 位，最低位恒 0（`imm[20|10:1|11|19:12] = inst[31|30:21|20|19:12]`）。

PicoRV32 把最终立即数统一放进两个寄存器：

- `decoded_imm[31:0]`：绝大多数指令用的立即数（已符号扩展到 32 位）。
- `decoded_imm_j[31:0]`：J 型立即数，在第一级预先算好，第二级再赋给 `decoded_imm`。

#### 4.2.2 核心流程

立即数的拼装在第二级译码里用一个 `case (1'b1)` 选择，按指令类型选不同的拼法：

```
decoder_trigger 拍：
  if jal           : decoded_imm <= decoded_imm_j           （J 型，第一级已算好）
  if lui/auipc     : decoded_imm <= inst[31:12] << 12       （U 型）
  if jalr/LOAD/OP-IMM : decoded_imm <= $signed(inst[31:20]) （I 型）
  if 分支          : decoded_imm <= $signed({inst[31],inst[7],inst[30:25],inst[11:8],1'b0}) （B 型）
  if STORE         : decoded_imm <= $signed({inst[31:25],inst[11:7]})                       （S 型）
```

其中 `$signed(...)` 是 Verilog 的符号扩展：把括号内最高位当作符号位向左填充到 32 位。这就是为什么所有立即数的符号位都设计成落在 `inst[31]`——这样无论哪种格式，符号扩展都"自然正确"。

#### 4.2.3 源码精读

J 型立即数在第一级就预先算好（[picorv32.v:880](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L880)），是全讲义里最"绕"的一行：

```verilog
{ decoded_imm_j[31:20], decoded_imm_j[10:1], decoded_imm_j[11],
  decoded_imm_j[19:12], decoded_imm_j[0] } <= $signed({mem_rdata_latched[31:12], 1'b0});
```

读法：右边先把 `inst[31:12]` 拼上一个 `0` 作为最低位（强制 `imm[0]=0`），再用 `$signed` 符号扩展到 32 位。左边把这个 32 位结果按 J 型的"乱序"位段打散写入 `decoded_imm_j`。展开后等价于：

\[
\text{imm}_J = \text{signext}\big(\text{inst}_{31}\,\text{inst}_{19:12}\,\text{inst}_{20}\,\text{inst}_{30:21}\,0\big)
\]

第二级再把它交给 `decoded_imm`（[picorv32.v:1119-1133](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1119-L1133)）：

```verilog
(* parallel_case *)
case (1'b1)
    instr_jal:
        decoded_imm <= decoded_imm_j;                              // J 型
    |{instr_lui, instr_auipc}:
        decoded_imm <= mem_rdata_q[31:12] << 12;                   // U 型
    |{instr_jalr, is_lb_lh_lw_lbu_lhu, is_alu_reg_imm}:
        decoded_imm <= $signed(mem_rdata_q[31:20]);                // I 型
    is_beq_bne_blt_bge_bltu_bgeu:
        decoded_imm <= $signed({mem_rdata_q[31], mem_rdata_q[7],
                                mem_rdata_q[30:25], mem_rdata_q[11:8], 1'b0});  // B 型
    is_sb_sh_sw:
        decoded_imm <= $signed({mem_rdata_q[31:25], mem_rdata_q[11:7]});        // S 型
    default:
        decoded_imm <= 1'bx;                                       // 该指令无立即数
endcase
```

要点：

- `(* parallel_case *)` 是综合提示，告诉工具这些分支互斥，便于优化。
- 对 **I 型**，直接 `$signed(inst[31:20])`，符号位正是 `inst[31]`。
- 对 **B 型**，最低位补 `1'b0`（分支目标永远偶对齐），再把符号位 `inst[31]` 放最高位。
- 对 **U 型**，`inst[31:12] << 12` 等价于"把高 20 位放到结果的高 20 位、低 12 位清零"，注意这里**不做**符号扩展（`lui` 语义本就是把 20 位立即数装载到高位）。

#### 4.2.4 代码实践

> **实践目标**：手工算出三条指令的 `decoded_imm`，验证你对五种格式的理解。

**操作步骤**：

1. `addi x2, x2, 1`（`0x00110113`，I 型）：`inst[31:20] = 000000000001`，`decoded_imm = $signed(...) = 1`。
2. `lui x1, 0x12345`（U 型，编码 `0x123450b7`）：`decoded_imm = 0x12345 << 12 = 0x12345000`。
3. `beq x1, x2, offset`（B 型，假设编码为 `0x00208463`）：取 `inst[31]=0, inst[7]=1, inst[30:25]=000000, inst[11:8]=0100`，拼上最低位 0，得 `imm = 0b0_0000_0000_0100_0 = 8`（即向前跳 8 字节，注意符号位为 0 表示正偏移）。

**需要观察的现象**：三种格式虽然立即数在指令里的"位置"完全不同，但经过译码后都变成同一个 32 位 `decoded_imm`，符号位都来自 `inst[31]`。

**预期结果**：你能不查表写出任意一条 RV32I 指令的 `decoded_imm` 计算式。结果若与你用 RISC-V 反汇编器（`riscv32-objdump`）算出的立即数一致，就说明你理解正确。

#### 4.2.5 小练习与答案

**练习 1**：为什么 B 型和 J 型立即数的最低位都恒为 0？

答案：RISC-V 指令在 32 位/压缩混合下仍按 2 字节边界对齐，跳转目标地址永远是偶数，所以立即数最低位没必要编码，省下一位给更高位用（B 型因此能表达 13 位范围，J 型 21 位）。

**练习 2**：`auipc` 与 `lui` 的立即数拼法相同（都是 U 型），但二者语义不同。这个区别是在译码器里体现的，还是在后续执行阶段体现的？

答案：在执行阶段体现。译码器对两者都产生相同的 `decoded_imm`（U 型）并分别点亮 `instr_lui`/`instr_auipc`；执行时（见后续 u4-l2/u5-1）`lui` 把立即数直接写入 rd，而 `auipc` 写入的是 `reg_pc + decoded_imm`。译码器只负责"认出"和"取数"，不负责运算。

### 4.3 译码触发时序

#### 4.3.1 概念说明

译码器不是"组合地、随时地"工作——它的输出 `instr_*` 和 `decoded_imm` 是 `reg`，只在特定时钟沿被刷新。控制这个刷新的信号就是 `decoder_trigger`。理解它的时序，是理解"取回的指令字何时变成可用的控制信号"的关键。

这里还出现一个看似多余的双胞胎：`decoder_pseudo_trigger`（伪触发）。它的作用是"让缓存与追踪逻辑以为发生了一次译码，但实际上不重新译码"。这一节我们先抓住主触发 `decoder_trigger`，伪触发作为进阶了解。

#### 4.3.2 核心流程

把译码器放进取指流水里看：

```
cpu_state_fetch:
  mem_do_rinst <= 1        发起一次取指读
  ...                      等待内存握手 ...
  mem_done = 1             取指传输完成的那一拍

  // 下一拍沿：
  decoder_trigger <= mem_do_rinst && mem_done   （第 1446 行）

  // 同一拍沿，第一级译码用 mem_rdata_latched 算组信号（第 866 行起）
  // 同时 mem_rdata_q <= mem_rdata  把指令字锁存一拍（第 432 行）

  // 再下一拍沿，decoder_trigger 已为 1：
  //   第二级译码用 mem_rdata_q 生成 instr_* 与 decoded_imm（第 1037 行起）
  //   cpu_state 从 fetch 推进到 ld_rs1（第 1574 行）
```

时序上可以总结为：

1. **取指完成拍**：`mem_done=1`，第一级译码锁存组信号与寄存器号，`mem_rdata_q` 锁存指令字，并预约下一拍把 `decoder_trigger` 拉高。
2. **译码拍**：`decoder_trigger=1`，第二级译码产出最终 `instr_*`/`decoded_imm`；同一拍主状态机（u4-l2 详讲）读这些信号决定下一步。
3. **执行拍起**：`instr_*` 保持稳定，供 ALU/内存接口使用，直到下一条指令的译码拍再次刷新它们。

`decoder_trigger_q` 是 `decoder_trigger` 再延迟一拍的版本（[picorv32.v:1447](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1447)），用于把"刚刚译出的"指令的助记符、立即数等缓存进 `cached_*`，供追踪/调试输出使用（[picorv32.v:792-802](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L792-L802)）。

#### 4.3.3 源码精读

触发信号的产生（[picorv32.v:1446-1449](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1446-L1449)）：

```verilog
decoder_trigger <= mem_do_rinst && mem_done;
decoder_trigger_q <= decoder_trigger;
decoder_pseudo_trigger <= 0;
decoder_pseudo_trigger_q <= decoder_pseudo_trigger;
```

注意每个时钟沿 `decoder_pseudo_trigger` 默认被清 0，只在特定执行路径（如 load/store 完成、某些分支命中）才被显式置 1（见 [picorv32.v:1874-1875](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1874-L1875) 与 [picorv32.v:1907-1908](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1907-L1908)）。

第二级译码的门控条件是 `decoder_trigger && !decoder_pseudo_trigger`（[picorv32.v:1037](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1037)）：

```verilog
if (decoder_trigger && !decoder_pseudo_trigger) begin
    ... // 生成 instr_*、decoded_imm
end
```

这意味着：当 load/store 指令在执行末尾把 `decoder_trigger` 与 `decoder_pseudo_trigger` 同时置 1 时，`instr_*`/`decoded_imm` **不会**被重新计算——它们保持当前指令的内容，但 `decoder_trigger_q` 仍会刷新 `cached_*`。这样追踪逻辑能拿到一致的"当前指令"信息，而译码结果不会被意外覆盖。

主状态机在 `cpu_state_fetch` 里消费译码结果（[picorv32.v:1557-1575](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1557-L1575)）：

```verilog
if (decoder_trigger) begin
    reg_next_pc <= current_pc + (compressed_instr ? 2 : 4);
    ...
    if (instr_jal) begin
        reg_next_pc <= current_pc + decoded_imm_j;   // 用第一级算好的 J 立即数
        latched_branch <= 1;
    end else begin
        mem_do_rinst <= 0;
        mem_do_prefetch <= !instr_jalr && !instr_retirq;
        cpu_state <= cpu_state_ld_rs1;               // 推进到下一状态
    end
end
```

由此可见，译码器输出的 `instr_jal`/`decoded_imm_j`/`compressed_instr` 直接驱动了 PC 的更新与状态机的跳转——译码器是后续所有执行的"发令枪"。

#### 4.3.4 代码实践

> **实践目标**：用纸笔或文本编辑器画出 `addi x2, x2, 1` 从取指完成到进入执行的三拍时序图，标出关键信号的变化。

**操作步骤**：

1. 画三列：`T0（取指完成拍）`、`T1（译码拍）`、`T2（进入 ld_rs1）`。
2. 在 `T0` 标注：`mem_done=1`、`mem_rdata_q ← 0x00110113`、`is_alu_reg_imm ← 1`、`decoded_rd ← 2`、`decoded_rs1 ← 2`，并标注"`decoder_trigger` 将在 T1 拉高"。
3. 在 `T1` 标注：`decoder_trigger=1`、`instr_addi ← 1`、`decoded_imm ← 1`，主状态机读到 `decoder_trigger` 后设 `reg_next_pc ← current_pc + 4`、`cpu_state ← ld_rs1`。
4. 在 `T2` 标注：`decoder_trigger_q=1`（缓存当前指令的助记符/立即数），状态机开始读 rs1=x2 的值，准备送入 ALU。

**需要观察的现象**：`mem_rdata_q` 比 `decoder_trigger` 早一拍准备好，所以第二级译码用的是"已经稳定一拍"的指令字；`instr_addi` 与 `decoded_imm` 在 `decoder_trigger` 那一拍同时可用，正好赶上主状态机做决策。

**预期结果**：得到一张清晰的三拍时序表，体现"取指 → 锁存 → 译码 → 推进"的节奏。这就是 PicoRV32 译码器与主状态机之间的握手协议。

#### 4.3.5 小练习与答案

**练习 1**：如果把第二级译码的门控改成只用 `decoder_trigger`（去掉 `!decoder_pseudo_trigger`），load/store 完成时会发生什么？

答案：load/store 在执行末尾会同时把 `decoder_trigger` 和 `decoder_pseudo_trigger` 置 1。若不加 `!decoder_pseudo_trigger` 保护，第二级译码会再次运行，用（可能已经变化的）`mem_rdata_q` 重新生成 `instr_*`/`decoded_imm`，可能覆盖掉当前正在提交的指令信息，导致错误。伪触发就是为了"借触发之名行缓存之实，但不重新译码"。

**练习 2**：为什么需要 `mem_rdata_q` 这个"延迟一拍"的版本，而不让第二级译码直接用 `mem_rdata_latched`？

答案：第一级译码在取指完成拍已经用了 `mem_rdata_latched`（组合出来的锁存字）算组信号；如果第二级也立刻用同一来源，两级译码的逻辑会挤在同一拍内串联，形成又长又慢的组合路径。引入 `mem_rdata_q` 把第二级挪到下一拍，用一拍寄存器把关键路径切断，利于提高 fmax。

## 5. 综合实践

把本讲三个模块串起来，完成一个"**人工反汇编 + 译码追踪**"任务：

1. 任选 4 条不同的 RV32I 指令，要求分别覆盖 I、S、B、U/J 五种立即数格式中的至少 4 种（例如 `addi`、`sw`、`beq`、`lui`、`jal`）。先用 `riscv32-unknown-elf-as`/`objdump` 把它们汇编成机器码，或直接手写编码。
2. 对每条指令，按本讲 4.1.4 的方法填写"32 位字段 → 译码信号"映射表：标出 opcode、funct3、funct7、rd、rs1、rs2，并指出它点亮哪个 `instr_*`、落入哪个立即数格式、`decoded_imm` 的值是多少。
3. 对其中一条（建议选 `jal`），再按 4.3.4 画出三拍时序图，标注 `mem_rdata_q`、`decoder_trigger`、`decoded_imm_j`、`decoded_imm` 与 `instr_jal` 的变化时刻。
4. 最后到源码里给每条指令找到它被点亮的精确行号（提示：4.1.3 列出的代码段），核对你的判断。

完成本任务后，你应当能"看一眼机器码就说出 PicoRV32 译码器会把它变成哪些控制信号和什么立即数"——这正是阅读后续执行通路（ALU、内存接口）的前提。

## 6. 本讲小结

- PicoRV32 用 **one-hot（一位热码）** 风格译码：每条指令对应一根 `instr_*` 一位信号，非法指令由 `instr_trap`（所有合法信号的归约或取反）判定。
- 译码分**两级**：第一级在取指完成拍用 `mem_rdata_latched` 提取 opcode 组、寄存器号与 J 型立即数；第二级在 `decoder_trigger` 拍用 `mem_rdata_q` 生成最终 `instr_*` 与 `decoded_imm`。
- 立即数统一进 `decoded_imm`，五种格式（I/S/B/U/J）各有专门拼法，且符号位永远落在 `inst[31]`，靠 `$signed` 自然符号扩展；J 型立即数在第一级预先算好放 `decoded_imm_j`。
- 译码刷新由 `decoder_trigger <= mem_do_rinst && mem_done` 驱动；`decoder_pseudo_trigger` 是 load/store 完成时的"只缓存不重译"机制，避免覆盖正在提交的指令信息。
- 译码器是后续所有执行的"发令枪"：`instr_*`、`decoded_imm`、`decoded_rd/rs1/rs2` 直接被主状态机与数据通路消费。

## 7. 下一步学习建议

译码器只回答了"这是什么指令、立即数多少"，还没回答"CPU 按什么顺序、在哪些状态里执行它"。下一讲 **u4-l2 主状态机 cpu_state** 会接续本讲，讲解 `cpu_state_fetch/ld_rs1/ld_rs2/exec/shift/stmem/ldmem/trap` 八个状态如何消费本讲产出的 `instr_*` 与 `decoded_imm`，完成从取指到写回的完整流程。

建议同步阅读：

- [picorv32.v 第 1170 行起的 `// Main State Machine` 区段](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1170)，对照本讲看 `decoder_trigger` 如何驱动状态转移。
- 想加深对立即数格式直觉的读者，可参考 RISC-V 手册中"Base Integer Instructions"一节的立即数编码图。
