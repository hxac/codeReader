# ALU 与比较/NZP

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 ALU 在 core 数据通路里的位置：`registers` 把 `rs/rt` 喂给 ALU，ALU 在 `EXECUTE` 拍算出 `alu_out`，再在 `UPDATE` 拍被 `registers`（算术写回）和 `pc`（比较写 NZP）消费。
- 解释 `decoded_alu_arithmetic_mux` 这只 2 位多路开关如何在 `ADD/SUB/MUL/DIV` 四则运算中「四选一」。
- 解释 `decoded_alu_output_mux` 如何在「算术模式」与「比较模式（CMP）」之间切换 ALU 的输出含义。
- **逐位写出** CMP 模式下 `alu_out[2:0]` 的来源（`bit[2]=(rs-rt>0)`、`bit[1]=(rs-rt==0)`、`bit[0]=(rs-rt<0)`），并解释为什么由于 **8 位无符号运算的回绕**，「负数」会被报告成「正」。
- 说明 `enable` 与 `core_state==EXECUTE` 的门控作用，以及 `alu_out_reg` 寄存输出带来的 1 拍延迟（EXECUTE 算、UPDATE 用）。

本讲承接 [u5-l1 指令集与编码](u5-l1-isa-encoding.md)：那里讲 `CMP` 把比较结果写进 NZP 寄存器、`BRnzp` 用 nzp 掩码做逐位与来跳转，但**故意把两个问题留到了本讲**——「`CMP` 到底把哪一位置进 NZP」、以及「为什么无符号运算会让负数回绕」。本讲从 `alu.sv` 的比较表达式逐位兑现这两个承诺。

## 2. 前置知识

- **ALU（Arithmetic Logic Unit，算术逻辑单元）**：CPU/GPU 里专门做运算的部件。tiny-gpu 的 ALU 极简，只做四则运算和一次比较，每个线程独占一份。
- **数据通路 vs 控制信号**：数据通路是数据流动的「管道」（寄存器→ALU→寄存器），控制信号是阀门，决定这一拍让哪个数据进哪根管。本讲的主角 `decoded_alu_arithmetic_mux` / `decoded_alu_output_mux` 就是两只阀门。这套思路在 [u4-l1 Core 解剖](u4-l1-core-anatomy.md) 已建立。
- **无符号 8 位运算与回绕**：`rs`、`rt` 都是 `[7:0]` 无符号数，范围 0–255。`rs - rt` 不会产生负数，而是 **mod 256 回绕**：`5 - 7` 在硬件里等于 `254`。这是本讲最关键的一个知识点，直接决定 CMP 的 NZP 置位。
- **core_state 七阶段**：`IDLE→FETCH→DECODE→REQUEST→WAIT→EXECUTE→UPDATE→DONE`（见 [u4-l2 Scheduler](u4-l2-scheduler-fsm.md)）。ALU 只在 `EXECUTE`（`3'b101`）这一拍干活，结果在下一拍 `UPDATE`（`3'b110`）被消费。
- **NZP 与 BRnzp 掩码匹配**：NZP 是三个条件标志（Negative/Zero/Positive），`CMP` 负责置位，`BRnzp` 用指令里的 nzp 三位做掩码、与 NZP 寄存器逐位与来决定跳转（见 [u5-l1 第 4.4 节](u5-l1-isa-encoding.md)）。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [src/alu.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/alu.sv) | **本讲主角**。一个 `always` 块同时承担四则运算和 CMP 比较，输出 8 位 `alu_out`。 |
| [src/decoder.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv) | 产生本讲的两只控制阀门：`decoded_alu_arithmetic_mux`（选运算）和 `decoded_alu_output_mux`（选算术/比较模式）。 |
| [src/core.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv) | 用 `generate` 为每个线程例化一个 ALU，把 `rs[i]/rt[i]`、`enable`、`core_state` 接进去。 |
| [src/registers.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/registers.sv) | UPDATE 拍消费 `alu_out`：算术指令经 `reg_input_mux=ARITHMETIC` 写回 `rd`。 |
| [src/pc.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/pc.sv) | UPDATE 拍把 `alu_out[2:0]` 抄进 NZP 寄存器；EXECUTE 拍用 NZP 判定 `BRnzp` 是否跳转。 |
| [test/test_matmul.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py) | 含一个真实的 `CMP` + `BRn` 循环，是本讲理论的「现实校验场」。 |

> 一句话定位：`decoder.sv` 拨阀门 → `alu.sv` 在 EXECUTE 拍算出 `alu_out` → `registers.sv` 拿它写回寄存器、`pc.sv` 拿它的低 3 位写 NZP。ALU 是「单出口、双用途」的运算工。

## 4. 核心概念与源码讲解

### 4.1 ALU 的角色：寄存器之间的运算工

#### 4.1.1 概念说明

ALU 是一个**组合运算 + 寄存输出**的部件。它不取指、不访存、不做控制流，只做一件事：拿到两个 8 位输入 `rs`、`rt`，按当前指令的吩咐算出一个 8 位结果 `alu_out`。

在 core 的数据通路里，ALU 处在一个闭合环路的中间：

```
   REQUEST 拍                 EXECUTE 拍                 UPDATE 拍
┌────────────┐  rs/rt   ┌─────────┐  alu_out   ┌────────────┐
│ registers  │────────► │   ALU   │──────────► │ registers  │  (算术写回 rd)
└────────────┘          └─────────┘  └────────► │            │
(读出 rs/rt)              (算结果)    alu_out[2:0]│   pc       │  (写 NZP 寄存器)
                                    └──────────►└────────────┘
```

- `rs`、`rt` 在 `REQUEST` 拍由寄存器堆读出并锁存（见 [u5-l1](u5-l1-isa-encoding.md)、[u4-l1](u4-l1-core-anatomy.md)）。
- ALU 在 `EXECUTE` 拍根据控制信号算出 `alu_out`，存进内部寄存器 `alu_out_reg`。
- `UPDATE` 拍，`registers` 把 `alu_out` 写回 `rd`（算术指令），`pc` 把 `alu_out[2:0]` 抄进 NZP 寄存器（CMP 指令）。

注意「单出口、双用途」：ALU 只有一根 8 位输出线 `alu_out`，算术指令和 CMP 指令**共用**这根线，区别只在收件人——算术指令收件人是寄存器堆（用全部 8 位），CMP 指令收件人是 PC（只用低 3 位）。

#### 4.1.2 核心流程

ALU 每个线程一份，但它只在满足两个门控条件时才更新输出：

1. `enable` 为真（当前线程在本 block 的有效线程数内，见 4.4）。
2. `core_state == EXECUTE`（`3'b101`）。

满足后，再看 `decoded_alu_output_mux` 决定走哪条路：

- `decoded_alu_output_mux == 0`（默认，算术模式）：用 `decoded_alu_arithmetic_mux` 在 `ADD/SUB/MUL/DIV` 里四选一，结果 8 位。
- `decoded_alu_output_mux == 1`（CMP 模式）：不算四则运算，而是把 `rs - rt` 的符号信息编码进 `alu_out[2:0]`，高 5 位补 0。

其他 `core_state` 下，ALU 保持上一次的输出不变（寄存器特性）。

#### 4.1.3 源码精读

ALU 的端口非常精简——两个数据输入、两只控制阀门、一个状态输入、一个使能、一个输出：

[src/alu.sv:9-22](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/alu.sv#L9-L22) —— 端口定义。注意 `rs/rt` 是 `input reg [7:0]`（无符号 8 位），`alu_out` 是 `output wire [7:0]`；两个 `decoded_*_mux` 是 decoder 送来的控制信号。

文件顶部注释点明了「每线程一份 ALU」的 SIMD 设计：

[src/alu.sv:4-8](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/alu.sv#L4-L8) —— 注释说明 ALU 负责执行 ADD/SUB/MUL/DIV，且每个 core 的每个线程都有自己独立的 ALU。

core 用 `generate for` 把这份 ALU 复制 `THREADS_PER_BLOCK` 份，并把 `rs[i]/rt[i]`、`enable`、`core_state` 接好：

[src/core.sv:136-146](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L136-L146) —— ALU 实例化。`.rs(rs[i])`、`.rt(rt[i])` 把该线程的寄存器读出值接进来，`.alu_out(alu_out[i])` 把结果接回，供 registers/pc 消费。

#### 4.1.4 代码实践

**目标**：在真实内核里确认「算术指令的 rs/rt/rd 流向」。

**步骤**：
1. 打开 [test/test_matadd.py:18](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L18)，找到 `ADD R4, R1, R0`（编码 `0b0011010000010000`）。
2. 切字段：opcode=`0011`(ADD)、rd=`0100`(R4)、rs=`0001`(R1)、rt=`0000`(R0)。
3. 走一遍数据通路：REQUEST 拍 `registers` 读出 `R1→rs`、`R0→rt`；EXECUTE 拍 ALU 算 `rs+rt`；UPDATE 拍写回 `R4`。

**预期结果**：这条指令语义是 `R4 ← R1 + R0`（即 `addr(A[i]) = baseA + i`）。你应当能说清「rs、rt 从哪来、alu_out 到哪去」这三段路。（源码阅读型实践，无需运行仿真。）

#### 4.1.5 小练习与答案

**练习 1**：ALU 只有一根 8 位输出 `alu_out`，为什么算术指令和 CMP 指令不会「抢」这根线？
**答案**：因为它们由不同的 opcode 触发，发生在不同指令的执行中，不会同时发生。而且即便 ALU 在非算术指令时也总会算点东西（见 4.4），收件人靠 decoder 的控制信号决定要不要采纳——算术指令 `reg_input_mux=ARITHMETIC` 收 `alu_out`，CMP 指令靠 `nzp_write_enable` 只收 `alu_out[2:0]`，互不干扰。

**练习 2**：为什么 ALU 要放在 `registers` 与 `pc` 之间，而不是直接挂在内存总线上？
**答案**：tiny-gpu 是 load-store 架构（见 [u5-l1](u5-l1-isa-encoding.md)）：运算只能在寄存器之间进行，不能直接「寄存器 op 内存」。所以 ALU 的输入必须是 `rs/rt`，输出必须先回寄存器堆，要操作内存得另走 LSU。

---

### 4.2 四则运算：decoded_alu_arithmetic_mux 的四选一

#### 4.2.1 概念说明

算术指令 `ADD/SUB/MUL/DIV` 长得几乎一样：都是 `rd ← rs (op) rt`，唯一区别是那个运算符 `op`。硬件自然不会为每种运算各造一个 ALU，而是**用一只多路开关（mux）**让同一个 ALU 在四种运算里选一种。

这只开关就是 `decoded_alu_arithmetic_mux`，2 位宽，正好编码四种运算：

| `decoded_alu_arithmetic_mux` | 运算 | 对应指令 |
|------------------------------|------|----------|
| `2'b00` | `rs + rt` | ADD |
| `2'b01` | `rs - rt` | SUB |
| `2'b10` | `rs * rt` | MUL |
| `2'b11` | `rs / rt` | DIV |

注意 mux 的值就是一张「运算选择表」，由 decoder 在 DECODE 拍根据 opcode 设好，一路保持到 EXECUTE 拍供 ALU 使用。

#### 4.2.2 核心流程

ALU 的算术分支就是一张 `case` 表，伪代码：

```
if core_state == EXECUTE and output_mux == 0:   # 算术模式
    case arithmetic_mux:
        00:  alu_out_reg <= rs + rt
        01:  alu_out_reg <= rs - rt
        10:  alu_out_reg <= rs * rt
        11:  alu_out_reg <= rs / rt
```

四个分支都用 Verilog 内建的 `+ - * /` 运算符，结果按 8 位截断（无符号）。`/` 是无符号整数除法（截断小数）。

decoder 那边则是一一对应地设置这只 mux：

| 指令 | decoder 设置的 `alu_arithmetic_mux` |
|------|--------------------------------------|
| `ADD` | `2'b00` |
| `SUB` | `2'b01` |
| `MUL` | `2'b10` |
| `DIV` | `2'b11` |

#### 4.2.3 源码精读

ALU 顶部的 `localparam` 给四种运算起了名字，使 `case` 表可读：

[src/alu.sv:23-26](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/alu.sv#L23-L26) —— `ADD=00, SUB=01, MUL=10, DIV=11`，与 mux 值一一对应。

算术 `case` 表本体：

[src/alu.sv:42-55](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/alu.sv#L42-L55) —— 四个分支分别做 `rs+rt`、`rs-rt`、`rs*rt`、`rs/rt`，结果写入 `alu_out_reg`。这是算术模式的全部逻辑。

decoder 在 DECODE 拍为每条算术指令设好 mux 值（注意它们同时设 `reg_write_enable=1`、`reg_input_mux=ARITHMETIC`，确保结果会被写回）：

[src/decoder.sv:95-114](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L95-L114) —— ADD/SUB/MUL/DIV 四个分支，分别把 `decoded_alu_arithmetic_mux` 设为 `00/01/10/11`，与 ALU 的 `case` 表严密对齐。

#### 4.2.4 代码实践

**目标**：对 `rs=5`、`rt=7`，手算四种算术运算的 8 位结果，体会无符号截断/回绕。

**步骤**：
1. 假设某线程 `R1=5`、`R2=7`，执行 `ADD/SUB/MUL/DIV R0, R1, R2`。
2. 按 [src/alu.sv:42-55](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/alu.sv#L42-L55) 的公式逐个算。
3. 把每个结果写成 8 位二进制与十六进制。

**预期结果**（关键：注意 SUB 和 DIV）：

| 运算 | 数学值 | 8 位硬件结果 | 二进制 | 十六进制 |
|------|--------|--------------|--------|----------|
| ADD | 5 + 7 = 12 | 12 | `00001100` | `0x0C` |
| SUB | 5 − 7 = −2 | **254**（回绕） | `11111110` | `0xFE` |
| MUL | 5 × 7 = 35 | 35 | `00100011` | `0x23` |
| DIV | 5 ÷ 7 = 0.71… | **0**（整数截断） | `00000000` | `0x00` |

最值得留意的是 **SUB**：数学上 `5 − 7 = −2`，但 8 位无符号没有负数，结果是 `256 − 2 = 254 = 0xFE`。这个「回绕」现象正是下一节 CMP 行为的根因。**待本地验证**：可在 `make test_matadd` 的日志里，观察一次涉及 SUB 的算术（matmul 有 `SUB R7, R0, R7`，见 [test/test_matmul.py:22](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py#L22)），确认寄存器值符合无符号语义。

#### 4.2.5 小练习与答案

**练习 1**：`MUL` 两个 8 位数相乘，最大结果可达 `255 × 255 = 65025`，远超 8 位。tiny-gpu 会怎样处理？
**答案**：`alu_out_reg` 是 8 位寄存器，`rs * rt` 的结果被**截断成低 8 位**（高位丢弃）。所以 `200 × 2 = 400` 会变成 `400 mod 256 = 144`。这是教学型 GPU 的简化，真实 GPU 会用更宽的累加器或高低位寄存器。

**练习 2**：`DIV R0, R1, R2` 当 `R2=0` 时会发生什么？
**答案**：Verilog 中 `x / 0` 的行为是**未定义/返回未知值 `x`**（综合后通常为 0 或垃圾值）。tiny-gpu 没有除零保护，因此内核编写者必须自己保证除数非零。这是另一个「教学简化」点。

---

### 4.3 比较模式与 NZP 编码：无符号回绕的关键辨析

> 本节是本讲的核心，也是 [u5-l1](u5-l1-isa-encoding.md) 留给本讲的「悬念解答」：CMP 到底把哪一位置进 NZP，以及为什么负数会回绕。

#### 4.3.1 概念说明

`CMP rs, rt` 不写任何通用寄存器（注意 decoder 里 `CMP` 分支**不**拉高 `reg_write_enable`），它的唯一作用是：比较 `rs` 与 `rt`，把比较结果编码进 `alu_out` 的**低 3 位**，再由 PC 单元抄进 NZP 寄存器，供后续 `BRnzp` 判跳。

模式切换由 `decoded_alu_output_mux` 完成：

- `output_mux == 0`：算术模式（默认），ALU 走 4.2 的四则运算 `case`。
- `output_mux == 1`：比较模式，ALU 跳过四则运算，改算 `alu_out[2:0]` 的 N/Z/P 编码。

`CMP` 是唯一把 `output_mux` 设为 1 的指令。

#### 4.3.2 核心流程

比较模式的那一行代码，是本讲信息密度最高的一句：

```verilog
alu_out_reg <= {5'b0, (rs - rt > 0), (rs - rt == 0), (rs - rt < 0)};
```

把这句「拼接」展开，`alu_out` 的 8 位是：

| 位 | 内容 | 含义（命名） |
|----|------|--------------|
| `[7:3]` | `5'b0` | 恒为 0，占位 |
| `[2]` | `(rs - rt > 0)` | P（Positive，大于零） |
| `[1]` | `(rs - rt == 0)` | Z（Zero，等于零） |
| `[0]` | `(rs - rt < 0)` | N（Negative，小于零） |

所以从低位往高位读 `bit[0]/bit[1]/bit[2]` 正好是 N/Z/P，与助记符顺序一致。

**关键辨析：无符号回绕让 N 位「永远为 0」**。`rs`、`rt` 是无符号 8 位，`rs - rt` 在 Verilog 里也是无符号运算：它**永远不会小于 0**，不够减时回绕成一个很大的正数（≈ `256 − |rs−rt|`）。因此三个比较的实际真值是：

| 数学关系 | `rs - rt`（8 位无符号） | `(>0)` bit[2] | `(==0)` bit[1] | `(<0)` bit[0] | `alu_out[2:0]` |
|----------|------------------------|---------------|-----------------|----------------|----------------|
| `rs > rt` | 正小值 | 1 | 0 | 0 | `100` |
| `rs == rt` | 0 | 0 | 1 | 0 | `010` |
| `rs < rt` | 回绕大值（如 254） | **1** | 0 | **0** | `100` |

看出来了吗？`rs > rt` 和 `rs < rt` **算出来的 `alu_out[2:0]` 完全一样**，都是 `100`！因为回绕后的「负」变成了「大正」，`(rs-rt > 0)` 照样成立，而 `(rs-rt < 0)` 永远不成立。结论：

- **N 位（bit[0]）结构性地恒为 0**，硬件永远无法表达「小于」。
- 只有两种可区分的结果：`rs == rt`（`010`，只有 Z）与 `rs != rt`（`100`，只有 P）。
- 也就是说，这套 CMP 在无符号语义下**只能分辨「相等 / 不等」**，无法分辨大小方向。

那 matmul 的「`loop while k < N`」为什么还能正确工作？因为那个循环真正需要的只是「`k` 是否等于 `N`」——见 4.3.4 与第 5 节的综合实践。

#### 4.3.3 源码精读

比较模式那一行就藏在 `output_mux` 分支里：

[src/alu.sv:37-39](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/alu.sv#L37-L39) —— 当 `decoded_alu_output_mux == 1`（CMP），把 `{5'b0, (rs-rt>0), (rs-rt==0), (rs-rt<0)}` 写入 `alu_out_reg`。注释直说「这些值用来和 NZP 寄存器比较」。

decoder 里 `CMP` 是把 ALU 切到比较模式的唯一指令，同时打开 NZP 写使能：

[src/decoder.sv:91-94](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L91-L94) —— `CMP` 分支只做两件事：`decoded_alu_output_mux <= 1`（切比较模式）、`decoded_nzp_write_enable <= 1`（允许 PC 写 NZP）。不拉 `reg_write_enable`，所以不污染通用寄存器。

PC 单元在 UPDATE 拍把这 3 位「原样」抄进 NZP 寄存器（位序一一对应）：

[src/pc.sv:57-64](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/pc.sv#L57-L64) —— `nzp[2] <= alu_out[2]`、`nzp[1] <= alu_out[1]`、`nzp[0] <= alu_out[0]`，即 NZP 寄存器 = `{P, Z, N}`。注意这里把 `alu_out` 当成 3 位标志来用，与算术指令把 `alu_out` 当 8 位数据来用形成对照——同一根线，两种解读。

随后 `BRnzp` 在 EXECUTE 拍用指令里的 nzp 掩码与 NZP 寄存器逐位与：

[src/pc.sv:42-50](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/pc.sv#L42-L50) —— `(nzp & decoded_nzp) != 0` 即跳转到 `decoded_immediate`，否则 `current_pc + 1`。掩码 `decoded_nzp` 来自指令的 `[11:9]`（见 [u5-l1 4.4](u5-l1-isa-encoding.md)）。

#### 4.3.4 代码实践

**目标**：对 `rs=5`、`rt=7` 给出 CMP 模式下 `alu_out[2:0]` 的值，并解释「数学上是负、硬件却报正」的原因。

**步骤**：
1. 按 [src/alu.sv:37-39](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/alu.sv#L37-L39) 的表达式，先算 `rs - rt` 的 8 位无符号值。
2. 再分别求 `(>0)`、`(==0)`、`(<0)` 三个真值。
3. 拼成 `alu_out[2:0]`，指出 N/Z/P 哪一位为 1。

**预期结果**：

- `rs - rt = 5 - 7`，8 位无符号回绕为 **254**（`0xFE`）。
- `(254 > 0) = 1` → `bit[2]`（P）= 1
- `(254 == 0) = 0` → `bit[1]`（Z）= 0
- `(254 < 0) = 0` → `bit[0]`（N）= 0
- 故 `alu_out[2:0] = 3'b100`，**只有 P 位（bit[2]）为 1**。

解读：数学上 `5 < 7` 是「负」，但硬件把它报告为「P（正）」，因为无符号回绕让 `5 − 7` 变成了大正数 254。这正是 [u5-l1](u5-l1-isa-encoding.md) 提示的「负数回绕」现象。**待本地验证**：在 matmul 仿真日志里找到一次 `CMP R9, R2` 且 `k<N` 的周期，确认该线程随后 `alu_out[2:0]` 呈 `100`。

**进阶校验**（现实中的循环）：[test/test_matmul.py:37-38](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py#L37-L38) 是 `CMP R9, R2` 紧跟 `BRn LOOP`（掩码 `100`）。把 `k` 从 0 递增到 `N=2`：`k=0,1` 时 `k != N` → `alu_out[2:0]=100` → 与掩码 `100` 逐位与得 `100` ≠ 0 → **跳回 LOOP**（继续循环）；`k=2` 时 `k == N` → `alu_out[2:0]=010` → `010 & 100 = 0` → **不跳，退出循环**。所以循环恰好执行内维度 N=2 次——这正是矩阵乘法需要的。可见这个「只能分辨相等/不等」的 CMP 对计数循环已足够。

#### 4.3.5 小练习与答案

**练习 1**：若 `rs=7`、`rt=5`（`rs > rt`），CMP 后 `alu_out[2:0]` 是多少？和 `rs=5`、`rt=7`（`rs < rt`）的结果有何关系？
**答案**：`7 - 5 = 2 > 0` → `alu_out[2:0] = 100`。**与 `5 < 7` 的结果完全相同**（都是 `100`）。这正说明无符号 CMP 无法区分「大于」与「小于」——两者都落在「不等」这一类。

**练习 2**：既然 N 位恒为 0，那 `BRn`（注释里的「小于则跳」）这个名字是不是用错了？
**答案**：从「字面数学语义」看确实名不副实——它并不能真正检测「负」。但从「这个循环实际靠相等/不等来工作」的角度看，`BRn` 配合计数循环（`k != N` 时继续）功能上是正确的，所以测试能通过。这属于 tiny-gpu 的教学简化之一，更严肃的有符号比较留给了未来的扩展（可对照 [u7 调度取舍](u7-l1-scheduling-tradeoffs.md)）。

---

### 4.4 时序与门控：EXECUTE 计算、UPDATE 消费

#### 4.4.1 概念说明

ALU 不是一直都在算的。它受两道门控保护，确保只在「该算的线程、该算的节拍」才更新输出：

1. **`enable` 门控**：core 用 `generate` 建了 `THREADS_PER_BLOCK` 个 ALU，但本次 block 可能只有 `thread_count` 个线程（最后一块常是尾数）。多余的 ALU 用 `enable = (i < thread_count)` 关掉，既省翻转也避免乱写。
2. **`core_state == EXECUTE` 门控**：ALU 只在七阶段状态机的 `EXECUTE`（`3'b101`）这一拍写 `alu_out_reg`，其他节拍保持原值。

此外，`alu_out` 是**寄存输出**（经 `alu_out_reg`），用非阻塞赋值 `<=`，因此有 1 拍延迟：EXECUTE 拍算、UPDATE 拍才被消费。这个延迟与 scheduler 的节拍严密咬合。

#### 4.4.2 核心流程

一条算术指令（如 `ADD`）在 ALU 视角下的时序：

```
节拍:        REQUEST          EXECUTE          UPDATE
registers:  读 rs/rt ────────►                  写回 rd ◄──────── alu_out
ALU:                          算 rs+rt ────────► (alu_out_reg 在此拍末更新)
                                            ↑
                                  非阻塞 <= 带来 1 拍延迟：
                                  EXECUTE 末写入，UPDATE 拍读出
```

- `REQUEST` 拍：`registers` 把 `rs/rt` 锁存（[registers.sv:75-78](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/registers.sv#L75-L78)）。
- `EXECUTE` 拍：ALU 命中 `core_state==3'b101`，算出结果用 `<=` 写进 `alu_out_reg`（本拍结束的上升沿生效）。
- `UPDATE` 拍：`alu_out_reg` 已是新值；`registers` 此时读 `alu_out` 写回 `rd`（[registers.sv:81-88](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/registers.sv#L81-L88)），`pc` 此时读 `alu_out[2:0]` 写 NZP（[pc.sv:58-64](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/pc.sv#L58-L64)）。

> 顺带一个细节：即便当前是非算术、非 CMP 指令（如 `LDR`、`NOP`），ALU 在 EXECUTE 拍仍会算（默认 `output_mux=0`、`arithmetic_mux=0`，即算 `rs+rt`）并写入 `alu_out_reg`。但此时 `reg_write_enable=0`、`nzp_write_enable=0`，结果没人采纳，属于「算了但被忽略」——这正是控制信号驱动数据通路的体现。

#### 4.4.3 源码精读

ALU 的整个时序逻辑就一个 `always @(posedge clk)`，三道门控 `reset → enable → core_state` 层层嵌套：

[src/alu.sv:31-39](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/alu.sv#L31-L39) —— 复位优先（清 0），其次 `enable`，最内层 `core_state==3'b101`（EXECUTE）。注释点明「只在 EXECUTE 计算 alu_out」。

输出经寄存器中转：

[src/alu.sv:28-29](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/alu.sv#L28-L29) —— `alu_out_reg` 是真正的状态寄存器，`assign alu_out = alu_out_reg` 把它连到端口。所以 `alu_out` 是「上个 EXECUTE 拍的结果」。

`enable` 门控在 core 例化时生成：

[src/core.sv:139](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L139) —— `.enable(i < thread_count)`，只有前 `thread_count` 个线程的 ALU 才被点亮。

消费端在 UPDATE 拍对齐：`registers` 用 `core_state==3'b110` 守卫写回：

[src/registers.sv:81-88](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/registers.sv#L81-L88) —— UPDATE 拍、`reg_input_mux==ARITHMETIC(2'b00)` 时，`registers[rd] <= alu_out`。此时读到的 `alu_out` 正是 EXECUTE 拍算出的值，时序咬合。

#### 4.4.4 代码实践

**目标**：跟踪一条 `ADD` 指令从 REQUEST 到 UPDATE 的三拍数据流动。

**步骤**：
1. 选 [test/test_matadd.py:22](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L22) 的 `ADD R6, R4, R5`（`C[i] = A[i] + B[i]`）。
2. 在纸上画三个节拍方框：REQUEST / EXECUTE / UPDATE。
3. 标出每个节拍里 `rs/rt`、`alu_out_reg`、`registers[R6]` 的变化时机。
4. 重点标注：`alu_out_reg` 在 EXECUTE 拍末用 `<=` 更新，`registers[R6]` 在 UPDATE 拍读到新值。

**预期结果**：你能说清「为什么 `registers` 必须在 UPDATE 而非 EXECUTE 读 `alu_out`」——因为非阻塞赋值的 1 拍延迟。这解释了 scheduler 为何要把 EXECUTE 和 UPDATE 拆成两拍（见 [u4-l2](u4-l2-scheduler-fsm.md)）。（源码阅读型实践。）

#### 4.4.5 小练习与答案

**练习 1**：为什么多余的 ALU（`i >= thread_count`）要用 `enable` 关掉，而不是让它们空转？
**答案**：一是省功耗（教学项目不太在意），更重要的是**避免它们把无效的 `rs+rt` 写进各自的 `alu_out_reg`、进而被误读**。虽然写回端另有 `reg_write_enable` 把关，但关掉 `enable` 是更干净的第一道闸，也让这些线程的 `lsu`/`registers`/`pc` 一并冻结，保证「硬件建满、运行时按需点亮」（见 [u4-l1](u4-l1-core-anatomy.md)）。

**练习 2**：若把 [src/alu.sv:36](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/alu.sv#L36) 的 `core_state == 3'b101` 误写成 `3'b110`（UPDATE），会发生什么？
**答案**：ALU 会在 UPDATE 拍才算结果，但 `registers` 也在 UPDATE 拍就要读 `alu_out` 写回——此时读到的还是上上条指令的旧值，写回会错乱。这正说明 EXECUTE/UPDATE 的拆分与 ALU 的 1 拍寄存延迟是配套设计，不能随意挪动。

---

## 5. 综合实践

把本讲知识串起来：**拆解 matmul 的 `CMP + BRn` 循环，手算 NZP 置位、预测循环次数，并解释「无符号回绕」为何反而让循环正确**。

### 背景

matmul 内核用下面三条指令构成内层循环（计算点积的累加）：

```asm
   ADD R9, R9, R1     ; k += 1                      [test_matmul.py:36]
   CMP R9, R2         ; 比较 k 与 N(=R2=2)            [test_matmul.py:37]
   BRn LOOP           ; 掩码 100，命中则跳回 LOOP      [test_matmul.py:38]
```

其中 `R2 = N = 2`（矩阵内维度），`R9 = k` 从 0 开始每次加 1。

### 步骤

1. 对 `k = 0, 1, 2`（即 `rs = k`、`rt = N = 2`），分别按 4.3 的方法算出 `CMP` 后的 `alu_out[2:0]`。
2. 写出 `BRn` 的掩码（`instruction[11:9]`，由编码 `0b0001100000001100` 读出）。
3. 对每个 `k`，判断 `(nzp & mask) != 0` 是否成立，决定「跳回 LOOP」还是「退出循环」。
4. 汇总：循环体执行了几次？是否等于内维度 N=2？

### 参考答案（请先自己做再对照）

| k | rs−rt（数学） | rs−rt（8 位无符号） | `alu_out[2:0]` | NZP 寄存器 | 掩码（BRn） | `nzp & mask` | 动作 |
|---|---------------|---------------------|-----------------|------------|-------------|--------------|------|
| 0 | 0 − 2 = −2 | 254 | `100` | `100` | `100` | `100` ≠ 0 | 跳回 LOOP |
| 1 | 1 − 2 = −1 | 255 | `100` | `100` | `100` | `100` ≠ 0 | 跳回 LOOP |
| 2 | 2 − 2 = 0 | 0 | `010` | `010` | `100` | `000` = 0 | 退出循环 |

结论：循环体在 `k=0` 与 `k=1` 时各执行一次，`k=2` 时退出——**恰好 2 次，等于内维度 N=2**，矩阵乘法的点积求和正确。

### 关键洞察

注意 `k=0,1` 时数学上是 `k < N`（负），但由于无符号回绕，`alu_out[2:0]` 落在 `100`（P 位），与 `BRn` 的掩码 `100` 命中而跳转。换句话说：

- 这个循环**真正依赖的是「`k` 是否等于 `N`」**（相等→`010`→不跳；不等→`100`→跳），而「相等/不等」正是无符号 CMP **唯一能分辨**的两种情形。
- 「`BRn` / 小于则跳」的命名是**字面误称**（N 位恒为 0，硬件并无「小于」概念），但因为它实质上是个「`while k != N`」循环，功能完全正确——这就是为什么 matmul 测试能通过。

### 进阶（可选）

把 `BRn` 的掩码从 `100` 改成 `010`（即「相等则跳」，对应 `BRz`），预测循环行为会如何变化？（答案：`k=2` 时 `nzp=010` 与掩码 `010` 命中而跳回，循环变成「`k` 到达 N 后反而继续」，陷入计数超过 N 的死循环——因为 `k>N` 时 `alu_out[2:0]` 仍是 `100`，与 `010` 不命中才退出，行为完全不同。）**待本地验证**：可仿照 [u6-l3 编写内核](u6-l3-writing-kernels.md) 的方法改 `test_matmul.py` 的这条指令编码，跑 `make test_matmul` 观察是否如预测般行为异常。

## 6. 本讲小结

- ALU 是 core 数据通路里的运算工：输入 `rs/rt`，输出 `alu_out`，处在 `registers ↔ ALU ↔ registers/pc` 的闭环中（[alu.sv:9-22](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/alu.sv#L9-L22)）。
- 算术四则由 `decoded_alu_arithmetic_mux` 这只 2 位 mux 四选一：`00/01/10/11` = `ADD/SUB/MUL/DIV`（[alu.sv:42-55](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/alu.sv#L42-L55)），值由 decoder 在 DECODE 拍设好（[decoder.sv:95-114](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L95-L114)）。
- 算术/比较模式由 `decoded_alu_output_mux` 切换：`0` 走四则运算，`1`（仅 CMP）算 `alu_out[2:0]` 的 N/Z/P 编码（[alu.sv:37-39](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/alu.sv#L37-L39)）。
- CMP 的位映射为 `bit[2]=(rs-rt>0)=P`、`bit[1]=(rs-rt==0)=Z`、`bit[0]=(rs-rt<0)=N`；由于 8 位无符号回绕，**N 位恒为 0**，CMP 实际只能分辨「相等/不等」（`010` vs `100`）。
- ALU 受 `enable` 与 `core_state==EXECUTE` 双重门控，输出经 `alu_out_reg` 寄存（1 拍延迟），故「EXECUTE 算、UPDATE 用」与 scheduler 的节拍严密咬合（[alu.sv:31-39](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/alu.sv#L31-L39)）。
- matmul 的 `CMP+BRn` 循环之所以正确，是因为它本质是「`while k != N`」，恰好落在无符号 CMP 能分辨的范围内——「负数回绕」非但不是 bug，反而是循环得以工作的机制。

## 7. 下一步学习建议

- ALU 的结果除算术写回外，另一条去路是 `alu_out[2:0] → NZP → BRnzp`。想看 `BRnzp` 如何用这套 NZP 做条件跳转、以及 PC 收敛假设带来的分支分歧问题，请读 [u5-l4 寄存器堆与程序计数器](u5-l4-registers-pc.md) 和 [u7-l1 调度取舍](u7-l1-scheduling-tradeoffs.md)。
- 想了解与 ALU 并列的另一类执行单元——LSU 如何异步访存、为何需要 scheduler 的 WAIT 阶段配合——请读 [u5-l3 LSU 异步访存](u5-l3-lsu-async-memory.md)。
- 想亲手把本讲的 CMP/BRnzp 用进一个新内核并接入仿真，跳到 [u6-l3 编写与仿真内核](u6-l3-writing-kernels.md)，那里以 matadd/matmul 为范本教你写程序。
- 推荐本地跑一次 `make test_matmul`，打开 `test/logs` 日志，找到 `CMP R9, R2` 与 `BRn LOOP` 前后的周期，对照本讲的 NZP 置位表，把「纸面分析」和「真实波形」对上号——本讲才算真正落地。
