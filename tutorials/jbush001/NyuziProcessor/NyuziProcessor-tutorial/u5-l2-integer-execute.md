# 整数执行单元（int_execute_stage）

## 1. 本讲目标

上一讲（u5-l1）我们走完了「操作数 fetch」：指令需要的两个操作数、掩码、子周期都已备齐，并且标量已经被广播成 16 通道的 `vector_t`。本讲顺着数据流继续往下，进入单核流水线三条执行路径中的**整数路径**——`int_execute_stage`。

读完本讲你应该能够：

- 说清 `int_execute_stage` 在一拍内完成了哪些整数/逻辑/移位/比较/向量重排运算，以及它如何把同一套 ALU 实例化 16 份实现 SIMD。
- 解释一条分支指令为什么**在执行阶段才被解析**，解析后如何算出 `rollback_pc`，回滚信号又如何一路传回取指阶段刷新流水线。
- 理解 `eret` 这种特权操作在这里如何被检测，违规时如何变成 `TT_PRIVILEGED_OP` 异常。
- 知道本阶段产出哪几个性能事件脉冲，它们如何汇入 `performance_counters`。

## 2. 前置知识

在进入源码前，先确认几个本讲会反复用到的概念（均在前置讲义中建立过）：

- **三条执行路径**：操作数 fetch 之后，指令按解码阶段填好的 `pipeline_sel` 字段分流到访存（`PIPE_MEM`）、整数（`PIPE_INT_ARITH`）、浮点（`PIPE_FLOAT_ARITH`）三条路径。本讲只看整数路径，它只有**一级**，是最短的那条。参见 [defines.svh:233-237](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L233-L237)（`pipeline_sel_t` 定义）。
- **标量即退化的向量**：执行单元只认 16 通道的 `vector_t`，标量操作数在上一级已被广播复制 16 份。所以本阶段的 ALU 永远按 16 通道算，标量指令只是只用到了 lane 0。
- **`decoded_instruction_t`**：解码级把 32 位指令展开成的统一结构体，本阶段会读取其中的 `alu_op`、`branch`、`branch_type`、`pc`、`immediate_value`、`pipeline_sel` 等字段。参见 [defines.svh:247-287](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L247-L287)。
- **分支条件只看最低位**：Nyuzi 没有传统条件码，`bz/bnz` 直接测试某个标量寄存器的最低位（见 u2-l4）。
- **回滚（rollback）**：当发现取指方向错了（分支 taken、异常、缓存缺失），需要把本线程在更年轻流水级里错误推测的指令作废，并把 PC 改到正确目标。

> 关于本阶段的命名：源码注释里有一句关键提醒——「尽管名字叫 integer execute，这个阶段其实也处理浮点倒数估计（reciprocal）」。这是因为 `reciprocal` 用 ROM 查表单周期完成，和整数运算一样只需一拍，所以也放在这里，而不是放进 5 级浮点流水线。参见 [int_execute_stage.sv:21-29](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L21-L29)。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [hardware/core/int_execute_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv) | **本讲主角**。整数 ALU、分支解析、特权检测、性能事件都在这一个模块里。 |
| [hardware/core/defines.svh](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh) | `alu_op_t`（算术/逻辑/比较操作编码）、`branch_type_t`（分支类型）、`pipeline_sel_t`、`decoded_instruction_t`、`trap_type_t` 等类型定义。 |
| [hardware/core/writeback_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv) | 接收本阶段结果，统一仲裁回滚信号（分支回滚、特权异常都在这里落地），并把回滚广播回取指。 |
| [hardware/core/ifetch_tag_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv) | 回滚信号的终点：用 `wb_rollback_pc` 改写被选中线程的 PC。 |
| [hardware/core/core.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv) | 把本阶段三个性能脉冲与其它模块的事件拼成 `perf_events` 向量。 |
| [hardware/core/performance_counters.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/performance_counters.sv) | 按控制寄存器选定的事件位累加计数。 |
| [hardware/core/reciprocal_rom.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/reciprocal_rom.sv) | 倒数估计查表，被本阶段的 `OP_RECIPROCAL` 调用。 |
| [tests/unit/test_int_execute_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/test_int_execute_stage.sv) | 专门针对本阶段的单元测试，直接驱动模块端口、断言 `rollback_pc`。 |
| [tests/core/isa/branch.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/branch.S) | ISA 级分支功能测试，覆盖 `b/bz/bnz/call` 等所有分支类型。 |

## 4. 核心概念与源码讲解

本讲按规格拆成四个最小模块：**整数 ALU**、**分支解析与回滚**、**特权检测**、**性能事件**。

### 4.1 整数 ALU

#### 4.1.1 概念说明

整数 ALU 是本阶段的核心：所有「一拍就能算完」的整数运算都在这里完成——加减、按位逻辑、移位、前导零/末尾零计数（clz/ctz）、符号扩展、比较、向量通道重排（shuffle/getlane），以及单周期浮点倒数估计。

它的最大特点是**用 `generate` 把同一套单通道 ALU 逻辑复制 16 份**，对应 16 个 SIMD 通道。每个通道独立吃 `operand1[lane]` 和 `operand2[lane]`，独立算出 `lane_result`，最后拼回 `vector_result`。这就是 Nyuzi 向量 SIMD 的硬件落地：一条向量加法在物理上就是 16 个加法器同时干活，1 周期完成。

#### 4.1.2 核心流程

```
每周期：
  for lane in 0..15:                       # generate 静态展开
      a = of_operand1[lane]
      b = of_operand2[lane]
      # 1. 预先算好「减法 + 比较标志」，供 SUB_I 和所有比较指令复用
      {borrow, difference} = a - b
      negative = difference[31]
      overflow = (b[31]==negative) && (a[31]!=b[31])
      zero = (difference == 0)
      signed_gtr = (overflow == negative)
      # 2. 按 alu_op 选 lane_result（查表 / 加法 / 移位 / 比较 0|1 / 倒数…）
      lane_result = select(alu_op, a, b, difference, zero, borrow, signed_gtr, lz, tz, reciprocal)
      vector_result[lane] = lane_result
```

一个值得注意的复用：**减法只算一次**。`difference` 既是 `OP_SUB_I` 的结果，又是所有整数比较的判据来源。比较指令不重新做减法，而是直接组合 `zero / borrow / signed_gtr` 三个标志位，把结果写成 0 或 1。

#### 4.1.3 源码精读

模块端口与上下文（来自 operand_fetch、去往 writeback）：见 [int_execute_stage.sv:31-66](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L31-L66)。注意输入是两个 `vector_t`（16 通道）加掩码和解码后的指令，输出除了结果还有 `ix_rollback_en/ix_rollback_pc/ix_privileged_op_fault` 和三个性能脉冲。

16 通道并行实例化的骨架：[int_execute_stage.sv:76-78](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L76-L78)

```systemverilog
genvar lane;
generate
    for (lane = 0; lane < NUM_VECTOR_LANES; lane++)
    begin : lane_alu_gen
        ...
        assign vector_result[lane] = lane_result;
```

减法与比较标志（一次减法喂给 SUB_I 和所有比较）：[int_execute_stage.sv:98-102](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L98-L102)

```systemverilog
assign {borrow, difference} = {1'b0, lane_operand1} - {1'b0, lane_operand2};
assign negative = difference[31];
assign overflow = lane_operand2[31] == negative && lane_operand1[31] != lane_operand2[31];
assign zero = difference == 0;
assign signed_gtr = overflow == negative;
```

这里 `borrow` 是无符号借位（`a < b` 时为 1），`signed_gtr` 是「有符号大于」的紧凑判据。它们的数学含义见下一节。

ALU 结果选择表（关键片段）：[int_execute_stage.sv:218-249](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L218-L249)

```systemverilog
unique case (of_instruction.alu_op)
    OP_SHL:     lane_result = lane_operand1 << lane_operand2[4:0];
    OP_ADD_I:   lane_result = lane_operand1 + lane_operand2;
    OP_SUB_I:   lane_result = difference;
    OP_CMPEQ_I: lane_result = {{31{1'b0}}, zero};
    OP_CMPGT_I: lane_result = {{31{1'b0}}, signed_gtr && !zero};
    OP_CMPGT_U: lane_result = {{31{1'b0}}, !borrow && !zero};
    OP_CLZ:     lane_result = scalar_t'(lz);
    OP_SEXT8:   lane_result = scalar_t'($signed(lane_operand2[7:0]));
    OP_SHUFFLE,
    OP_GETLANE: lane_result = of_operand1[~lane_operand2];
    OP_RECIPROCAL: lane_result = reciprocal;
    default:    lane_result = 0;
endcase
```

对应的操作码全集见 `alu_op_t` 枚举：[defines.svh:81-124](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L81-L124)。注意浮点操作码（`OP_ADD_F` 等，最高位为 1）不会出现在这张表里——它们走 5 级浮点流水线，只有 `OP_RECIPROCAL` 这种单拍浮点操作留在这里。

几个容易看走眼的细节：

- **操作数来源并不统一**。二元算术/逻辑/移位用 `operand1 ⊕ operand2`；但 `OP_MOVE/OP_CLZ/OP_CTZ/OP_SEXT8/OP_SEXT16/OP_RECIPROCAL` 这些一元操作作用在 **operand2** 上；而 `OP_SHUFFLE/OP_GETLANE` 用 `of_operand1[~lane_operand2]`——把 operand2 当成通道下标，从 operand1 向量里挑一个通道。读源码时要留意每条指令到底吃哪个操作数。
- **移位量只取低 5 位**（`lane_operand2[4:0]`），范围 0–31，超出部分被丢弃。右移的算术/逻辑由 `shift_in_sign` 区分：[int_execute_stage.sv:187-188](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L187-L188)。
- **`clz/ctz` 输入为 0 返回 32**，靠一张 32 项的 `casez` 查表单周期完成：[int_execute_stage.sv:105-143](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L105-L143)（clz）与 [int_execute_stage.sv:146-184](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L146-L184)（ctz）。
- **比较结果是每通道一个 0/1**（`{{31{1'b0}}, flag}`）。写回阶段会把这 16 个 0/1 压成一个 16 位标量掩码写回目标寄存器——这是「向量比较产生掩码」机制，发生在 writeback，不在本阶段（见 [writeback_stage.sv:329-333](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L329-L333) 与 [writeback_stage.sv:401-402](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L401-L402)）。

**比较判据的数学**。设 `a=lane_operand1`、`b=lane_operand2`，均为 32 位补码。源码先把它们零扩展到 33 位再相减，从而无歧义地拿到无符号借位 `borrow` 与 32 位差 `difference`：

\[
\{borrow,\, difference\} = \{1'b0,a\} - \{1'b0,b\},\qquad borrow = [a <_{u} b]
\]

有符号溢出 `overflow`（此处变量名 `overflow`，语义是「补码减法发生了溢出」）的判据是「两操作数符号不同、且结果符号与减数相同」；负数标志 `negative` 就是差值最高位。由此可推出「有符号严格大于」的紧凑表达：

\[
a >_{s} b \iff overflow == negative
\]

无符号比较则直接用借位：`a <_u b` 当且仅当 `borrow=1`。所以 `OP_CMPGT_U = !borrow && !zero`，`OP_CMPLT_U = borrow && !zero`，与表里完全一致。

**单周期倒数估计**。`OP_RECIPROCAL` 不走浮点流水线，而是查 `reciprocal_rom`（尾数高 6 位 → 6 位估计），再由本阶段组合出指数与符号：[int_execute_stage.sv:190-216](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L190-L216)。ROM 表见 [reciprocal_rom.sv:21-95](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/reciprocal_rom.sv#L21-L95)。它只保证约 6 位精度，且对 0/无穷/NaN 做了特殊处理（除以 0 得无穷），用于软件实现除法（Nyuzi 没有硬件除法指令）。

#### 4.1.4 代码实践

**目标**：用模拟器亲眼看到整数 ALU 的几种典型操作（加法、`clz`、比较）的结果，并对照源码确认结果来源。

**操作步骤**：

1. 阅读现有的整数算术形式测试 [tests/core/isa/int_arithmetic_forms.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/int_arithmetic_forms.S)（该目录下还有 `generate_int_arith.py`，可生成大量随机算术用例）。
2. 参照它写一小段汇编（示例代码，非项目原有文件）：
   ```asm
   move s0, 5
   move s1, 3
   add_i s2, s0, s1     # 期望 s2 = 8
   clz s3, s0           # 5 = 0x00000005，期望 s3 = 29
   setne_i s4, s0, s1   # 5 != 3，期望 s4 = 1
   ```
3. 用 `run_emulator`（或 `bin/nyuzi_emulator -v`）运行，`-v` 会逐条打印 PC 与寄存器写回值。

**需要观察的现象**：`-v` 跟踪里每条指令一行，能看到目标寄存器被写成的值。

**预期结果**：`s2=8`、`s3=29`、`s4=1`。若实际工具链/汇编语法有差异（例如立即数形式写法），以本地编译器为准；如果暂时没有可用的工具链，可改为「源码阅读型实践」：直接对照 [int_execute_stage.sv:218-249](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L218-L249) 的 `case` 表，人工推出 `move s0,5` 后 `clz s3,s0` 的 `lz` 值，确认等于 29。

> 待本地验证：受环境工具链可用性影响，若无法构建请以上述「人工查表」方式完成。

#### 4.1.5 小练习与答案

**练习 1**：`OP_CMPGT_U`（无符号大于）为什么用 `!borrow && !zero`，而不能复用 `signed_gtr`？

**参考答案**：`signed_gtr` 是基于补码溢出的有符号判据，会把最高位当成符号位。无符号比较要求把两个 32 位数都当成非负整数，此时 `a > b` 等价于「减法没有借位且差不为零」，即 `!borrow && !zero`。两者只有在同号时才一致。

**练习 2**：`clz` 输入为 0 时返回 32，而不是 0。从指令语义和硬件实现两个角度解释为什么。

**参考答案**：语义上，一个 32 位数有 32 位都是前导零，所以结果是 32（若返回 0 会和「最高位就是 1、前导零为 0」混淆，丢失信息）。硬件上，这是查表实现里的一条独立分支 `32'b000...0: lz = 32`（见 [int_execute_stage.sv:140](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L140)），单周期给出，无需特殊处理。

---

### 4.2 分支解析与回滚

#### 4.2.1 概念说明

Nyuzi 的分支**不在取指或解码阶段判定**，而是在到达整数执行阶段时才知道两件事：**是否 taken**、**目标地址是什么**。这是因为分支目标可能来自寄存器（间接跳转）、可能依赖运行时的条件值，必须等操作数到齐。

一旦判定 taken，取指方向此前按「顺序+4」推测出来的后续指令就全错了。于是本阶段要发起一次**回滚**：把本线程在取指/解码/线程选择/操作数各级里的年轻指令作废，并把 PC 改到正确目标，下一拍从目标重新取指。

注意：回滚的「决策」在本阶段做出，但「广播与刷新」由写回阶段统一发起——因为同一个周期里可能有多条路径都想回滚，需要一个汇合点保证每周期只处理一个。

#### 4.2.2 核心流程

```
# 组合逻辑：判断 taken
if (valid_instruction && branch && !privileged_op_fault):
    match branch_type:
        BRANCH_ZERO      -> taken = (operand1[0] == 0);  conditional = 1
        BRANCH_NOT_ZERO  -> taken = (operand1[0] != 0);  conditional = 1
        其它(ALWAYS/CALL_OFFSET/CALL_REGISTER/REGISTER/ERET) -> taken = 1

# 组合逻辑：算目标地址 rollback_pc（按分支类型分三路）
match branch_type:
    CALL_REGISTER / REGISTER -> rollback_pc = operand1[0]      # 目标在寄存器
    ERET                     -> rollback_pc = cr_eret_address[thread]
    默认(相对偏移 b/call offset)-> rollback_pc = pc + immediate_value   # immediate 已预乘 4

# 时序逻辑：寄存一拍输出
ix_rollback_en <= branch_taken          # 仅当 taken 才回滚
ix_rollback_pc <= rollback_pc

# → 送入 writeback_stage 仲裁 → wb_rollback_en/pc/thread_idx 广播
# → ifetch_tag_stage: if (wb_rollback_en && 命中线程) next_pc <= wb_rollback_pc
```

分支类型全集见 [defines.svh:154-162](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L154-L162)（`branch_type_t`）。

#### 4.2.3 源码精读

判定是否 taken：[int_execute_stage.sv:263-298](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L263-L298)

```systemverilog
unique case (of_instruction.branch_type)
    BRANCH_ZERO:     begin branch_taken = of_operand1[0] == 0;   conditional_branch = 1; end
    BRANCH_NOT_ZERO: begin branch_taken = of_operand1[0] != 0;   conditional_branch = 1; end
    BRANCH_ALWAYS, BRANCH_CALL_OFFSET, BRANCH_CALL_REGISTER,
    BRANCH_REGISTER, BRANCH_ERET:
                     begin branch_taken = 1; end
endcase
```

注意条件分支只测 `of_operand1[0]`（标量最低位），印证了 u2-l4 讲过的「以最低位代替条件码」。`conditional_branch` 标志同时驱动性能统计（见 4.4）。

计算目标地址 `rollback_pc`（寄存在时钟沿）：[int_execute_stage.sv:301-317](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L301-L317)

```systemverilog
unique case (of_instruction.branch_type)
    BRANCH_CALL_REGISTER,
    BRANCH_REGISTER: ix_rollback_pc <= of_operand1[0];
    BRANCH_ERET:     ix_rollback_pc <= cr_eret_address[of_thread_idx];
    default:         ix_rollback_pc <= of_instruction.pc + of_instruction.immediate_value;
endcase
```

这里 `immediate_value` 在解码阶段已经被预乘 4（字节偏移→字地址，见 u4-l2），所以本阶段直接相加即可，不必再移位。

把 `branch_taken` 寄存成 `ix_rollback_en`：[int_execute_stage.sv:335-345](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L335-L345)

```systemverilog
if (valid_instruction) begin
    ix_instruction_valid <= 1;
    ix_rollback_en       <= branch_taken;
end else begin
    ix_instruction_valid <= 0;
    ix_rollback_en       <= 0;
end
```

**回滚信号如何到达取指**。`ix_rollback_en/ix_rollback_pc` 送到写回阶段后，写回阶段用**组合逻辑**把它转成全局 `wb_rollback_*`（不寄存，因为下一条指令可能是 store，必须在它产生副作用前 squash）：[writeback_stage.sv:216-230](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L216-L230) 处理「来自整数流水线的分支回滚」：

```systemverilog
else if (ix_instruction_valid && ix_rollback_en) begin
    wb_rollback_en        = 1;
    wb_rollback_pc        = ix_rollback_pc;
    wb_rollback_thread_idx= ix_thread_idx;
    wb_rollback_pipeline  = PIPE_INT_ARITH;
    if (ix_instruction.branch_type == BRANCH_ERET) begin
        wb_eret = 1;
        wb_rollback_subcycle = cr_eret_subcycle[ix_thread_idx];
    end
end
```

取指阶段消费这个信号，改写 PC：[ifetch_tag_stage.sv:157-160](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L157-L160)

```systemverilog
else if (wb_rollback_en && wb_rollback_thread_idx == local_thread_idx_t'(thread_idx))
    next_program_counter[thread_idx] <= wb_rollback_pc;
```

取指阶段还有一条值得注意的注释：它**故意不在当前周期就跳过正在被回滚的线程**，因为回滚信号是一条很长的组合链路（执行→写回→取指），是时钟频率的关键路径；为此它把处理推迟到下一拍（见 [ifetch_tag_stage.sv:122-126](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L122-L126)）。这是一个典型的「用一拍延迟换频率」的微架构取舍。

**call 的特殊性**：`call` 既要回滚（跳到目标）又要写回返回地址 `ra`（= `pc+4`）。写回阶段对 `BRANCH_CALL_OFFSET/REGISTER` 做了特判，让它在回滚的同时仍触发一次写回（见 [writeback_stage.sv:383-390](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L383-L390)）。

#### 4.2.4 代码实践（本讲指定实践任务）

**目标**：完整跟踪一条条件分支从「解析」到「刷新取指」的全过程，并定位每一处关键信号。

**操作步骤**：

1. 读 ISA 测试 [tests/core/isa/branch.S:29-53](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/branch.S#L29-L53)，它依次构造了 `bz taken / bz not taken / bnz taken / bnz not taken` 四种情况。
2. 任取一条 `bz s1, 1f`（`s1=0`，应 taken）。对照源码确认：
   - 在 [int_execute_stage.sv:273-277](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L273-L277)：`branch_type==BRANCH_ZERO`，`of_operand1[0]==0` → `branch_taken=1`，`conditional_branch=1`。
   - 在 [int_execute_stage.sv:314-316](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L314-L316)：走 `default` 分支，`ix_rollback_pc <= pc + immediate_value`（目标即 `1f` 的地址）。
   - 在 [int_execute_stage.sv:335-340](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L335-L340)：`ix_rollback_en <= 1`。
3. 跟着信号走出本阶段：[writeback_stage.sv:216-230](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L216-L230) 把它转成 `wb_rollback_en/pc/thread_idx`。
4. 到达终点：[ifetch_tag_stage.sv:157-160](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L157-L160) 把命中线程的 `next_program_counter` 改成 `wb_rollback_pc`，于是下一拍从目标地址重新取指——这就完成了对「顺序+4」错误推测的刷新。
5. （可选，单元测试视角）读 [tests/unit/test_int_execute_stage.sv:55-67](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/test_int_execute_stage.sv#L55-L67)：它的 `branch` task 正是按上面的字段驱动模块，并在后续周期断言 `ix_rollback_pc == last_branch_pc + last_branch_offset`。

**需要观察的现象**：把上述五个代码位置串成一条「`branch_taken` → `rollback_pc` → `ix_rollback_en` → `wb_rollback_*` → `next_program_counter`」的信号链。

**预期结果**：能画出「执行阶段判定 taken → 寄存一拍到写回 → 写回组合广播 → 取指改 PC」的四级路径，并解释为何写回段不寄存（怕 store 副作用漏过）、取指段却接受一拍延迟（换频率）。

> 待本地验证：若想看真实波形，可在 Verilator 下跑 `tests/unit` 的 `test_int_execute_stage`（周期精确，内部信号可见），观察 `ix_rollback_en/ix_rollback_pc` 与输入 `branch_type` 的对应。

#### 4.2.5 小练习与答案

**练习 1**：相对偏移分支（如 `b` / `bz`）的 `rollback_pc` 为什么是 `pc + immediate_value`，而不需要在执行阶段再做 `<< 2`？

**参考答案**：因为偏移量在**解码阶段**就已经被预乘 4（把以「字」为单位的偏移换算成字节地址），写进了 `immediate_value`（见 u4-l2）。执行阶段拿到的 `immediate_value` 已经是字节偏移，直接与 `pc` 相加即可，省掉了执行阶段的一次移位。

**练习 2**：为什么 `eret` 的目标地址要从控制寄存器 `cr_eret_address` 取，而不是用 `pc + immediate`？

**参考答案**：`eret` 是「从陷阱返回」，目标地址是进入陷阱时保存的「返回点」，属于动态的处理器状态，存在控制寄存器里，而不是静态的指令字段。所以执行阶段按线程号从 `cr_eret_address[of_thread_idx]` 读出（见 [int_execute_stage.sv:313](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L313)）。

---

### 4.3 特权检测

#### 4.3.1 概念说明

并非所有指令都能在任意特权级执行。`eret`（异常返回）会改变陷阱级别、恢复 PC 与标志，属于**特权操作**，只能在 supervisor 态执行。如果用户态程序敢执行 `eret`，必须被拦截并转为异常，而不是让它真的执行。

本阶段是这条指令流过的最后一道关卡，因此把检测放在这里：如果指令是 `eret` 但当前线程不在 supervisor 态，就置起 `privileged_op_fault`，让写回阶段把它变成 `TT_PRIVILEGED_OP` 陷阱。

#### 4.3.2 核心流程

```
valid_instruction = of_instruction_valid
                   && 没有被同线程回滚吃掉
                   && pipeline_sel == PIPE_INT_ARITH     # 只看走整数通路的指令

eret               = valid_instruction && branch && branch_type == BRANCH_ERET
privileged_op_fault= eret && !cr_supervisor_en[thread]   # 非特权态执行 eret

# 结果：
#   - 置起 ix_privileged_op_fault（寄存到写回）
#   - 写回把它翻译成 wb_trap + TT_PRIVILEGED_OP，回滚到 trap handler
#   - 分支解析里加了 !privileged_op_fault 守卫，避免错误地「真的返回」
```

#### 4.3.3 源码精读

`valid_instruction` 的三重过滤：[int_execute_stage.sv:255-261](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L255-L261)

```systemverilog
assign valid_instruction = of_instruction_valid
    && (!wb_rollback_en || wb_rollback_thread_idx != of_thread_idx)
    && of_instruction.pipeline_sel == PIPE_INT_ARITH;
assign eret = valid_instruction
    && of_instruction.branch
    && of_instruction.branch_type == BRANCH_ERET;
assign privileged_op_fault = eret && !cr_supervisor_en[of_thread_idx];
```

三个要点：

- 第二行 `!wb_rollback_en || ... != of_thread_idx`：如果本周期这条指令的线程正在被回滚（说明它属于被刷新的年轻指令），就当作无效，不产生任何副作用——这与 u4-l3 讲的「回滚只清比回滚点年轻的指令」一致。
- 第三行 `pipeline_sel == PIPE_INT_ARITH`：只对走整数通路的指令判定，避免对访存/浮点指令误判。
- `cr_supervisor_en` 是一个按线程维护的数组（每线程一个使能位），因为同核多线程里每个线程可能处于不同特权级。

注意分支解析的守卫：[int_execute_stage.sv:268-271](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L268-L271) 里 `&& !privileged_op_fault`，确保发生特权违规时**不会**把 `eret` 当成普通 taken 分支去算 `rollback_pc = cr_eret_address`。

写回阶段把故障翻译成异常：[writeback_stage.sv:177-199](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L177-L199)（关键两句在 [writeback_stage.sv:191-192](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L191-L192)）：

```systemverilog
if (ix_privileged_op_fault)
    wb_trap_cause = {2'b0, TT_PRIVILEGED_OP};
else
    wb_trap_cause = ix_instruction.trap_cause;
```

于是 PC 被回滚到 `cr_trap_handler`，进入陷阱处理流程（详见 u7-l3）。`TT_PRIVILEGED_OP` 的编码见 [defines.svh:200](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L200)。

#### 4.3.4 代码实践

**目标**：理解「用户态执行 eret 会触发 `TT_PRIVILEGED_OP`」这一保护机制如何落地。

**操作步骤**（源码阅读型实践）：

1. 确认 `cr_supervisor_en` 的来源：在 [int_execute_stage.sv:61](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L61) 它由 `control_registers` 模块按线程提供。进入 u7-l2 后会看到它由 `CR_FLAGS` 的某一位驱动。
2. 假设线程 0 处于用户态（`cr_supervisor_en[0]=0`），执行 `eret`：沿 [int_execute_stage.sv:258-261](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L258-L261) → [writeback_stage.sv:177-199](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L177-L199)，确认 `wb_trap=1`、`wb_trap_cause=TT_PRIVILEGED_OP`、`wb_rollback_pc=cr_trap_handler`。
3. 对照内核侧：在 `software/kernel` 的陷阱派发表里（见 u12-l1），`TT_PRIVILEGED_OP` 通常被映射为向用户进程发送非法操作信号。

**需要观察的现象**：用户态 `eret` 既不会真的返回，也不会静默通过，而是精确地转成一个可被内核处理的陷阱。

**预期结果**：能说清「检测点在 int_execute、翻译点在 writeback、处理点在内核 trap handler」三段式。若想实际触发，可在启用虚拟内存/内核的环境下让用户程序执行 `eret` 并观察内核行为（**待本地验证**：需要完整内核环境）。

#### 4.3.5 小练习与答案

**练习 1**：`valid_instruction` 为什么要加上 `pipeline_sel == PIPE_INT_ARITH` 这一过滤？

**参考答案**：本阶段（整数执行）每个周期都会收到操作数 fetch 送来的指令，但其中只有走整数通路的指令才归这里处理；走访存或浮点通路的指令只是「路过」或被选到别的执行单元。如果不加过滤，可能对一条并非在此执行的非整数指令误判 eret/特权，产生虚假异常。

**练习 2**：`cr_supervisor_en` 为什么是一个「每线程一个」的数组，而不是整个核共用一位？

**参考答案**：Nyuzi 单核多线程，同一核的多个硬件线程可以分别运行在不同特权级（例如一个线程跑内核、另一个跑用户进程）。特权级是每线程的状态，所以使能位也必须按线程 `cr_supervisor_en[of_thread_idx]` 索引。

---

### 4.4 性能事件

#### 4.4.1 概念说明

为了让软件能做性能剖析（profiling），硬件在每个关键模块埋了「事件脉冲」：某类事件本周期发生一次，就拉高对应信号一拍。本阶段负责三种与分支相关的事件：**无条件分支**、**条件分支 taken**、**条件分支未 taken**。这些脉冲被 `core.sv` 汇总后交给 `performance_counters` 按用户选定的事件累加。

#### 4.4.2 核心流程

```
# 三个互斥脉冲（仅当 valid_instruction 时统计）
ix_perf_uncond_branch          = !conditional_branch && branch_taken
ix_perf_cond_branch_taken      =  conditional_branch && branch_taken
ix_perf_cond_branch_not_taken  =  conditional_branch && !branch_taken

# core.sv 把它和 dcache/icache miss、指令退休 等 14 个事件拼成一位向量
perf_events = { ix_perf_cond_branch_not_taken,
                ix_perf_cond_branch_taken,
                ix_perf_uncond_branch,
                dd_perf_dtlb_miss, ... }

# performance_counters 按控制寄存器选定的事件位计数
if (perf_events[perf_event_select[i]])
    perf_event_count[i] <= perf_event_count[i] + 1
```

#### 4.4.3 源码精读

三个脉冲的生成：[int_execute_stage.sv:347-349](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L347-L349)

```systemverilog
ix_perf_uncond_branch         <= !conditional_branch && branch_taken;
ix_perf_cond_branch_taken     <=  conditional_branch && branch_taken;
ix_perf_cond_branch_not_taken <=  conditional_branch && !branch_taken;
```

注意它们在 `valid_instruction` 为假的分支里不会被显式清零——但因为 `branch_taken` 和 `conditional_branch` 在每周期开头的 `always_comb` 里都被重置为 0（见 [int_execute_stage.sv:264-266](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L264-L266)），无效周期里三者自然为 0。

`core.sv` 汇总成 14 位 `perf_events`（顺序很重要，位的下标就是事件号）：[core.sv:403-418](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L403-L418)

```systemverilog
assign perf_events = {
    ix_perf_cond_branch_not_taken,
    ix_perf_cond_branch_taken,
    ix_perf_uncond_branch,
    dd_perf_dtlb_miss,
    dd_perf_dcache_hit,
    dd_perf_dcache_miss,
    ifd_perf_itlb_miss,
    ifd_perf_icache_hit,
    ifd_perf_icache_miss,
    ts_perf_instruction_issue,
    wb_perf_instruction_retire,
    l2i_perf_store,
    wb_perf_store_rollback,
    wb_perf_interrupt
};
```

`CORE_PERF_EVENTS` 常量必须与此处信号个数一致（见 [defines.svh:70](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L70) 与 [core.sv:401-402](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L401-L402) 的注释）。

计数器按选定事件累加：[performance_counters.sv:43-47](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/performance_counters.sv#L43-L47)。软件通过控制寄存器 `CR_PERF_EVENT_SELECT0/1`（见 [defines.svh:188-189](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L188-L189)）选定要统计的事件号，再用 `CR_PERF_EVENT_COUNT0_L/H` 读出 64 位计数值。

#### 4.4.4 代码实践

**目标**：用性能计数器统计一段程序的「条件分支 taken / not taken」次数。

**操作步骤**（源码阅读型实践）：

1. 确认事件号：`perf_events` 是拼接向量，MSB 在前。条件分支 not taken 在第 13 位、taken 在第 12 位、无条件分支在第 11 位（从 0 起算的位号，取决于工具如何枚举；以 [core.sv:403-418](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L403-L418) 的实际位序为准）。
2. 参照 `software/libs/libos/bare-metal/performance_counters.c`（u11-l2 会详讲）写一小段代码：用 `setcr` 把事件号写入 `CR_PERF_EVENT_SELECT0`，运行一段带循环/分支的程序，再用 `getcr` 读 `CR_PERF_EVENT_COUNT0_L/H`。
3. 对比「分支 taken」与「分支 not taken」两个计数，估算这段代码的分支预测命中率（粗略：taken 占比）。

**需要观察的现象**：循环体里的 `bnz` 在最后一次不 taken、之前每次都 taken，所以 taken 计数应比 not taken 多 1 左右（取决于循环次数）。

**预期结果**：能用两个计数器的差值印证循环结构。精确数值**待本地验证**（依赖具体被测程序与事件号映射）。

#### 4.4.5 小练习与答案

**练习 1**：为什么把条件分支拆成 taken / not taken 两个事件，而不是只统计「条件分支总数」？

**参考答案**：taken 与 not taken 的比例直接反映分支的走向偏好，是评估分支预测策略、循环占比、热点路径的关键信息。只有总数无法区分「一个 1000 次的循环（999 taken / 1 not taken）」和「1000 个各走一次的分支」，拆开统计才有剖析价值。

**练习 2**：如果某周期没有有效指令，三个脉冲会误报吗？

**参考答案**：不会。无效周期里 `branch_taken` 和 `conditional_branch` 在 `always_comb` 开头被清 0（[int_execute_stage.sv:264-266](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L264-L266)），三个表达式都含 `branch_taken` 或与 `branch_taken` 互斥的 `conditional_branch`，结果恒为 0。

---

## 5. 综合实践

把本讲四个模块串起来，做一次「**一条条件分支指令在 `int_execute_stage` 的完整生命周期**」的端到端跟踪。请按顺序回答/操作：

1. **数据就位**（承接 u5-l1）：一条 `bnz s2, target` 到达本阶段时，`of_operand1`、`of_operand2`、`of_instruction.branch_type`、`of_instruction.pipeline_sel` 各是什么？为什么 `pipeline_sel` 必须是 `PIPE_INT_ARITH`？

2. **ALU 视角**：这条分支本身**不**经过 ALU 的 `case` 表（它没有 `has_dest` 的算术结果），但本周期同级的 ALU 仍在为别的通道算东西。确认 [int_execute_stage.sv:218-249](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L218-L249) 的 `default` 分支会给出 `lane_result = 0`，且不影响分支判定。

3. **分支解析**：设 `s2 = 5`（最低位为 1）。在 [int_execute_stage.sv:263-298](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L263-L298) 推出 `branch_taken` 与 `conditional_branch` 的值（答案：`taken=1, conditional=1`）。

4. **目标地址**：在 [int_execute_stage.sv:301-317](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L301-L317) 写出 `ix_rollback_pc` 的来源（`pc + immediate_value`，且 `immediate_value` 已在解码阶段预乘 4）。

5. **特权守卫**：若该线程此刻 `cr_supervisor_en=0`，会触发 `privileged_op_fault` 吗？（答案：不会，因为这条指令不是 `eret`，`eret` 标志为 0。）

6. **性能脉冲**：本周期哪个 `ix_perf_*` 会被拉高？（答案：`ix_perf_cond_branch_taken`。）

7. **回滚落地**：跟着 [writeback_stage.sv:216-230](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L216-L230) → [ifetch_tag_stage.sv:157-160](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L157-L160)，画出 PC 被改写的完整路径，并解释写回段为何用组合逻辑、取指段为何接受一拍延迟。

**交付物**：一张标注了上述 7 个信号点、从 `of_*` 输入到 `next_program_counter` 输出的信号流图，并附一段话说明「分支解析延迟（执行段 1 拍 + 写回组合 + 取指 1 拍延迟）换来的是时钟频率与单点仲裁的简洁性」。

## 6. 本讲小结

- `int_execute_stage` 是单周期整数执行级：用 `generate` 把同一套 ALU 复制 16 份实现 SIMD，一拍完成整数加减、逻辑、移位、`clz/ctz`、比较、`shuffle/getlane`，以及单周期浮点倒数估计。
- 减法只做一次：`difference` 同时供 `SUB_I` 与所有比较指令复用；有符号比较用 `signed_gtr = (overflow == negative)`，无符号比较用借位 `borrow`。
- 分支在**执行阶段**才解析：`branch_taken` 由 `branch_type` 决定（条件分支只测操作数最低位），目标地址 `rollback_pc` 按类型分三路（寄存器 / `cr_eret_address` / `pc+immediate`）。
- 回滚信号由本阶段寄存一拍送出，写回阶段用**组合逻辑**统一仲裁广播（避免 store 副作用漏过、避免同周期多回滚冲突），取指阶段用它改写 PC，并刻意接受一拍延迟以避开关键路径。
- 特权检测：`eret` 仅 supervisor 可执行，违规时本阶段置 `privileged_op_fault`，写回翻译成 `TT_PRIVILEGED_OP` 陷阱；分支解析带 `!privileged_op_fault` 守卫防止误返回。
- 本阶段产出三个分支性能脉冲，汇入 `core.sv` 的 14 位 `perf_events` 向量，由 `performance_counters` 按控制寄存器选定事件累加。

## 7. 下一步学习建议

- **浮点通路**：本讲多次提到「浮点加减乘走 5 级浮点流水线、只有倒数留在这里」。下一讲 **u5-l3 浮点五级流水线** 会展开 `fp_execute_stage1..5`，并解释为何整数乘法也复用浮点乘法器。可对照 [defines.svh:113-122](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L113-L122) 的浮点操作码。
- **回滚的其它来源**：本讲只讲了「分支回滚」。回滚还有数据缓存缺失、IO 访问、store 队列、异常等来源，全部在 **writeback_stage** 汇合——阅读 [writeback_stage.sv:163-241](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L163-L241) 的四类仲裁，为 u6（缓存）与 u7-l3（异常与回滚）打底。
- **性能剖析**：想看这些脉冲如何变成可读的热点报告，可提前翻阅 `tools/misc/profile.py` 与 `software/libs/libos/bare-metal/performance_counters.c`，对应 **u11-l2 性能计数器与 profiling**。
- **亲手验证**：在 Verilator 下跑 `tests/unit/test_int_execute_stage`，观察 `ix_rollback_en/ix_rollback_pc` 与 `branch_type` 的逐拍对应——这是周期精确、内部信号可见的最佳实验台。
