# 指令解码

## 1. 本讲目标

上一讲我们跟踪了指令从内存被「取」进流水线的过程。指令取回来只是一个 32 位的整数，CPU 还不能直接执行。本讲就聚焦流水线的下一级——**解码级（instruction decode stage）**，它把一个 32 位整数「翻译」成一个带满控制字段的结构体 `decoded_instruction_t`，供后续的发射、操作数读取、执行各级使用。

学完本讲，你应该能够：

1. 说清楚 `decoded_instruction_t` 里每个关键字段（`op1_src`/`op2_src`/`mask_src`/`store_value_vector`/`pipeline_sel` 等）的含义和用途。
2. 对着解码映射表，把任意一种指令格式（R / I / M / C / B）的位段拆解成 op1、op2、mask、store_value 的来源。
3. 解释「中断不是真的中断一条指令，而是在解码级用一条带 trap 标志的空壳指令替换原指令」这一精确中断机制。

## 2. 前置知识

本讲建立在前几讲已讲清的概念之上，这里只做最简提示：

- **指令是 32 位定长**；有 32 个标量寄存器（s0–s31）和 32 个向量寄存器（v0–v31），寄存器号占 5 位（见 u2-l1）。
- **向量 SIMD**：向量寄存器有 16 个通道，标量操作数可广播到所有通道；scatter/gather 类指令需要 16 个 subcycle 逐通道执行（见 u2-l1）。
- **流水线全景**：解码级位于 `ifetch_data_stage` 之后、`thread_select_stage` 之前；解码后指令会按 `pipeline_sel` 分流到三条执行路径——`PIPE_MEM`（访存）、`PIPE_INT_ARITH`（整数）、`PIPE_FLOAT_ARITH`（浮点）（见 u3-l2）。
- **操作编码三处同构**：`alu_op_t`、`memory_op_t` 等编码在硬件 `defines.svh`、模拟器 `instruction-set.h`、LLVM 工具链中数值一致，是协同仿真的基础（见 u2-l2）。

一个需要建立的直觉：**解码级本质上是一张「查表 + 补丁」电路**。它先根据指令最高几位查一张大表，确定这条指令「该读哪些寄存器、立即数从哪几位取、走哪条流水线」，再把若干来自取指级的异常信号「搭车（piggyback）」进来，最后把这些控制信号打包成一个结构体送往下一级。

## 3. 本讲源码地图

本讲只涉及两个文件：

| 文件 | 作用 |
|------|------|
| [hardware/core/instruction_decode_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv) | 解码级本体：把 32 位指令翻译成 `decoded_instruction_t`，并处理中断/异常的「搭车」与精确化。 |
| [hardware/core/defines.svh](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh) | 定义 `decoded_instruction_t` 结构体、`alu_op_t`/`memory_op_t`/`branch_type_t`/`trap_type_t` 等所有编码枚举，是全项目共享的类型表。 |

## 4. 核心概念与源码讲解

按「解码结构 → 格式映射 → 中断替换」三个最小模块展开。

### 4.1 解码结构：decoded_instruction_t

#### 4.1.1 概念说明

取指级送过来的是两个东西：一个 32 位指令字 `ifd_instruction`，和一组伴随的异常标志（如 `ifd_tlb_miss`、`ifd_page_fault` 等，这些是取指阶段发现的访存错误）。

解码级要把这个 32 位整数变成一个**结构体** `decoded_instruction_t`。可以把它理解成一张「工单」：后续每一级流水线都读这张工单上的字段来决定自己该干什么，而不必再去翻那 32 位原始编码。这样做有两个好处：

1. **解耦**：编码格式只在这一级被解析一次，后面所有级都面向统一的结构体字段编程。
2. **携带异常**：取指阶段发现的异常可以塞进这张工单（`has_trap` / `trap_cause`），让它在写回级被统一处理。

#### 4.1.2 核心流程

解码级是一个**纯组合逻辑 + 一组寄存器锁存输出**的阶段，流程是：

```
ifd_instruction[31:0] ──┐
ifd_*_fault / tlb_miss ─┤
cr_interrupt_*          ├──> 组合查表 + 字段拼装 ──> decoded_instr_nxt ──(寄存器)──> id_instruction
io/sync pending         ┘
```

具体步骤：

1. 用指令的最高 7 位 `[31:25]` 查一张大表 `dlut_out`，得到一组「控制开关」（如立即数从哪几位取、操作数是标量还是向量、有没有掩码）。
2. 根据 `dlut_out` 里的开关，从 32 位指令中切出 scalar1/scalar2/vector1/vector2 的寄存器号、立即数值、目的寄存器号。
3. 判定 `alu_op`、`branch_type`、`memory_access_type`、`pipeline_sel` 等枚举字段。
4. 把取指级异常、非法指令、syscall、breakpoint、待处理中断合并成一个 `has_trap` 标志和 `trap_cause`；一旦 `has_trap` 为真，所有「读寄存器」类字段都被抑制。
5. 用一组触发器把 `decoded_instr_nxt` 锁存成 `id_instruction`，送往下级。

#### 4.1.3 源码精读

先看解码的产物 `decoded_instruction_t`，它定义在 defines.svh 中：

[hardware/core/defines.svh:L247-L287](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L247-L287) — 定义 `decoded_instruction_t`，这就是解码级产出的「工单」。关键字段含义：

- `has_scalar1`/`scalar_sel1`、`has_vector1`/`vector_sel1`：是否需要读第一个源操作数、它的寄存器号。
- `op1_src`/`op2_src`：操作数 1/2 的来源是标量寄存器、向量寄存器还是立即数。
- `mask_src`：向量掩码来自哪里（scalar1、scalar2 还是全 1）。
- `store_value_vector`：存储类指令要写入内存的数据是否来自向量寄存器。
- `immediate_value`：已经切好并符号扩展好的立即数（分支偏移已在此处预先乘 4）。
- `pipeline_sel`：这条指令该送进哪条执行流水线（访存 / 整数 / 浮点）。
- `has_trap`/`trap_cause`：本条指令携带的异常（含中断）。

操作数来源与掩码来源的取值也很重要，同样是几个小枚举：

[hardware/core/defines.svh:L216-L231](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L216-L231) — `mask_src_t`、`op1_src_t`、`op2_src_t` 三个枚举，是格式映射表里反复出现的「开关值」。

解码级在查表时，先用一个**中间结构体 `dlut_out`** 暂存查表结果，再把它的字段翻译成 `decoded_instruction_t`。这个中间体相当于「每条指令格式自带的解码配置」：

[hardware/core/instruction_decode_stage.sv:L123-L138](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L123-L138) — `dlut_out` 结构体，每个字段都是一个开关：`imm_loc`（立即数位段位置）、`scalar1_loc`/`scalar2_loc`（两个标量寄存器号从哪几位取）、`has_vector1/2`（是否读向量寄存器）、`op1_vector`（op1 是不是向量）、`op2_src`、`mask_src`、`store_value_vector`、`call` 等。

`dlut_out` 里的几个「位置枚举」决定立即数和寄存器号从指令的哪几位切出来：

[hardware/core/instruction_decode_stage.sv:L99-L121](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L99-L121) — `imm_loc_t`（8 种立即数位段）、`scalar1_loc_t`、`scalar2_loc_t`。这些枚举就是「同一份 32 位编码，不同格式把不同位段当寄存器号/立即数」的根源。

最后看流水线选择逻辑，它决定指令分流到哪条执行路径：

[hardware/core/instruction_decode_stage.sv:L382-L398](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L382-L398) — `pipeline_sel` 判定：带 trap 的指令统一走 `PIPE_INT_ARITH`（因为它不需要真正执行，只是把 trap 送到写回级）；R/I 格式里若 `alu_op[5]` 为 1（浮点操作码最高位为 1）或属于整数乘法/`ftoi`，走浮点流水线，否则走整数流水线；分支指令也走整数流水线（分支在整数执行级解析）；其余（访存、缓存控制）走 `PIPE_MEM`。

#### 4.1.4 代码实践

**实践目标**：建立「一条指令 = 一个 decoded_instruction_t 工单」的直觉。

**操作步骤**：

1. 打开 [hardware/core/defines.svh:L247-L287](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L247-L287)，逐个字段标注它「会被流水线哪一级读」。
2. 对照上一讲 u3-l2 的流水线图，确认：操作数字段（`has_scalar1` 等）由 `operand_fetch_stage` 读；`memory_access_type` 由 dcache 两级读；`branch_type` 由 `int_execute_stage` 读；`has_trap`/`trap_cause` 由 `writeback_stage` 读。

**需要观察的现象**：你会发现 `decoded_instruction_t` 的字段几乎被流水线**所有**后续级共享——这正是把它设计成一个大结构体、用 `.*` 通配连接（见 u3-l2）的原因。

**预期结果**：你能口述出「`op2_src` 这个字段最终被 `operand_fetch_stage` 用来决定 op2 读寄存器还是用立即数」。

#### 4.1.5 小练习与答案

**练习 1**：`decoded_instruction_t` 里为什么没有一个字段叫「opcode」？

**参考答案**：因为解码级已经把 opcode「展开」成了一组控制开关（`alu_op`、`memory_access_type`、`branch_type`、`pipeline_sel`、各 `*_src` 等）。后续各级只需读这些开关，无需再重复解析 32 位编码，从而解耦。

**练习 2**：`pipeline_sel` 为什么对「带 trap 的指令」一律设为 `PIPE_INT_ARITH`？

**参考答案**：带 trap 的指令不需要真正执行运算，它只是一张「把异常送到写回级」的空壳工单。走哪条流水线对它都无所谓，但必须走一条确实能通到写回级的路径；整数流水线最短、最通用，因此被选作 trap 的运载通道。

---

### 4.2 格式映射：从 7 位特征码到操作数来源

#### 4.2.1 概念说明

Nyuzi 的 32 位指令按最高几位特征码分成五大格式（见 u2-l1 已建立的「R/I/M/C/B」地图）：

| 格式 | 含义 | 特征码 | 典型指令 |
|------|------|--------|----------|
| **R** | 寄存器算术 | `[31:29]==110` | `add_i s0, s1, s2` |
| **I** | 立即数算术 | `[31]==0` | `add_i s0, s1, 5` |
| **M** | 访存 | `[31:30]==10` | `load_32`、`store_block`、`scatter/gather` |
| **C** | 缓存控制 | `[31:28]==1110` | `cache invalidate`、`membar` |
| **B** | 分支 | `[31:28]==1111` | `call`、`bz`、`eret` |

这五个特征码互斥地划分了 32 位空间（`[31]==0` 只属于 I；`[31]==1` 时按 `[30]`、`[29]`、`[28]` 继续细分）。

不同格式的 32 位「布局」不同：同一段位，在 R 格式里可能是第二个寄存器号，在 I 格式里却可能是立即数的一部分。所以解码级必须**先认出格式，再按该格式的规则切位段**。

源码文件顶部有一张非常有用的总览表，列出了每种格式下 op1/op2/mask/store_value 各自来自哪个寄存器或立即数：

[hardware/core/instruction_decode_stage.sv:L38-L52](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L38-L52) — 「Register port to operand mapping」总览表。例如 `R - scalar/scalar` 行表示 op1=s1、op2=s2、无掩码、无 store_value；`M - block` 行表示 op1=s1（基址）、op2=imm（偏移）、mask=s2、store_value=v2。本节的核心实践就是对照这张表拆指令。

#### 4.2.2 核心流程

格式映射的核心是一张用 `casez` 实现的查表：

```
取 ifd_instruction[31:25]（最高 7 位）
        │
        ▼
   casez 大表（≈ 40 行）
        │
        ▼  输出 dlut_out（一组开关）
        │
        ▼  按开关切位段
   scalar_sel1 / scalar_sel2 / vector_sel1 / vector_sel2
   immediate_value（含符号扩展、分支预乘 4）
   alu_op / branch_type / memory_access_type / pipeline_sel
        │
        ▼
   decoded_instr_nxt
```

立即数的切法由 `imm_loc` 决定，分支偏移在解码级就被预先左移两位（即乘 4，因为指令按字编址），这样执行级就不必再做一次移位：

[hardware/core/instruction_decode_stage.sv:L361-L375](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L361-L375) — 立即数切取。注意 `IMM_24_5`/`IMM_24_0`（分支偏移）拼接了 `2'b00` 实现「乘 4」；`IMM_EXT_19` 则拼接 13 位 0，相当于一个左移 13 位的大立即数；其余用 `$signed` 做符号扩展。

#### 4.2.3 源码精读

格式判定只用三条 assign，非常简洁：

[hardware/core/instruction_decode_stage.sv:L229-L231](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L229-L231) — `fmt_r`/`fmt_i`/`fmt_m` 三个格式标志。注意它们虽然都基于 `[31]`/`[31:30]`，但因为 R 要求 `[30:29]==10`、M 要求 `[30]==0`，三者互斥。

下面是全模块的核心——`casez` 查表。每行的 14 个值就是 `dlut_out` 结构体的 14 个字段（顺序对应：`illegal, dest_vector, has_dest, imm_loc, scalar1_loc, scalar2_loc, has_vector1, has_vector2, vector_sel2_9_5, op1_vector, op2_src, mask_src, store_value_vector, call`）。这里摘几行最具代表性的：

[hardware/core/instruction_decode_stage.sv:L164-L176](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L164-L176) — R 格式（`7'b110_???`）与 I 格式（`7'b0_??_???`）的表项。例如第一行 `7'b110_000_?`（R 标量/标量）设 `scalar1_loc=SCLR1_4_0`、`scalar2_loc=SCLR2_19_15`、`op2_src=OP2_SRC_SCALAR2`、`mask_src=MASK_SRC_ALL_ONES`，与总览表第一行一致。

[hardware/core/instruction_decode_stage.sv:L178-L203](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L178-L203) — M 格式（访存）的 store 行（`7'b10_0_????`，`[29]=0`）与 load 行（`7'b10_1_????`，`[29]=1`）。注意 store block（`7'b10_0_0111`）设 `has_vector2=T`、`vector_sel2_9_5=T`、`store_value_vector=T`，即要存的向量数据来自 `instr[9:5]`；scatter/gather（`7'b10_0_1101`）则额外设 `has_vector1=T`、`op1_vector=T`，因为每通道地址来自向量寄存器。

[hardware/core/instruction_decode_stage.sv:L224-L226](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L224-L226) — `default` 分支：任何未被识别的特征码都把 `illegal` 置 1，这会触发 `TT_ILLEGAL_INSTRUCTION` 陷阱。这就是 CPU 拒绝执行非法指令的入口。

切出寄存器号的代码也值得一读。scalar1、scalar2 的位段由各自的 `_loc` 决定：

[hardware/core/instruction_decode_stage.sv:L295-L316](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L295-L316) — scalar1 从 `[14:10]` 或 `[4:0]` 取；scalar2 可从 `[19:15]`、`[14:10]`、`[9:5]` 取。同一段 `[4:0]`，在不同格式里既可能是 scalar1，也可能是 vector1（见下条）。

[hardware/core/instruction_decode_stage.sv:L319-L328](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L319-L328) — vector1 永远来自 `instr[4:0]`，vector2 由 `vector_sel2_9_5` 决定来自 `[9:5]` 还是 `[19:15]`。所以 `instr[4:0]` 这 5 位，在标量格式里被当 scalar1，在向量格式里被当 vector1——由 `has_vector1`/`has_scalar1` 开关区分。

`alu_op` 的解码也体现格式差异：I 格式从 `[28:24]` 取且强制最高位为 0（所以 I 格式编码不出浮点操作），R 格式从 `[25:20]` 取完整 6 位；而 `call` 指令被特殊处理成 `OP_MOVE`（因为 call 等价于「move ra, pc」）：

[hardware/core/instruction_decode_stage.sv:L336-L346](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L336-L346) — `alu_op` 解码，含 call 的特例。

目的寄存器也有一个特例：`call` 的目的固定为 `REG_RA`（即 s31，返回地址寄存器），其余指令的目的来自 `instr[9:5]`：

[hardware/core/instruction_decode_stage.sv:L334](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L334) — `dest_reg = dlut_out.call ? REG_RA : ifd_instruction[9:5]`。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：对照解码映射表，为四种典型格式各写出一条指令，并标出 op1 / op2 / mask / store_value 的来源寄存器。这是把「格式映射」从概念变成肌肉记忆的最直接练习。

**操作步骤**：

先约定记号：用 `sN` 表示标量寄存器，`vN` 表示向量寄存器；`instr[4:0]` 写作「[4:0]」。对照 [总览表 L38-L52](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L38-L52) 与 [casez 表 L164-L203](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L164-L203) 完成下表。

| 指令（汇编示意） | 格式 / 表项 | op1 来源 | op2 来源 | mask 来源 | store_value 来源 |
|------------------|-------------|----------|----------|-----------|------------------|
| `add_i s0, s1, s2`（标量+标量） | R，`7'b110_000_?` | scalar1 = s1（[4:0]） | scalar2 = s2（[19:15]） | 全 1（无掩码） | 无 |
| `add_i s0, s1, 5`（标量+立即数） | I，`7'b0_00_????` | scalar1 = s1（[4:0]） | immediate = 5（[23:10]，符号扩展） | 不适用（标量无掩码） | 无 |
| `store_block v2, s1, off`（向量块存储） | M，`7'b10_0_0111` | scalar1 = s1（[4:0]，基址） | immediate = off（[24:10]，偏移） | 全 1（无掩码版） | vector2 = v2（[9:5]） |
| `store_scatter v2, v1, off`（scatter 存储） | M，`7'b10_0_1101` | vector1 = v1（[4:0]，每通道地址） | immediate = off（[24:15]，偏移） | scalar2 = s2（[14:10]） | vector2 = v2（[9:5]） |

逐行核对要点：

1. **R 标量/标量**：查 `7'b110_000_?` 行，`op2_src=OP2_SRC_SCALAR2`、`scalar2_loc=SCLR2_19_15`，故 op2 来自 `[19:15]`；`mask_src=MASK_SRC_ALL_ONES`，故无掩码。
2. **I 立即数**：查 `7'b0_00_????` 行，`op2_src=OP2_SRC_IMMEDIATE`、`imm_loc=IMM_23_10`，故 op2 是 `[23:10]` 符号扩展后的值；标量格式不读掩码。
3. **M 块存储**：查 `7'b10_0_0111` 行，`has_vector2=T`、`vector_sel2_9_5=T`、`store_value_vector=T`，故待存数据是 `[9:5]` 指定的向量；基址 scalar1 来自 `[4:0]`，偏移是 `[24:10]`。
4. **M scatter/gather**：查 `7'b10_0_1101` 行，`has_vector1=T`、`op1_vector=T`，故 op1 是 `[4:0]` 指定的向量（每通道一个地址）；`mask_src=MASK_SRC_SCALAR2`、`scalar2_loc=SCLR2_14_10`，故掩码来自 `[14:10]` 指定的标量；待存数据 vector2 来自 `[9:5]`。

**需要观察的现象**：同一段 `[4:0]`，在 R/I/块存储里被当 scalar1（基址或第一操作数），在 scatter/gather 里却被当 vector1（地址向量）——区别只在于 `dlut_out.has_vector1`/`op1_vector` 这两个开关。

**预期结果**：你能不看源码，只凭指令特征码（最高几位）判断出它属于哪种格式、四个操作数各来自哪个寄存器号位段。

> 说明：本表为「示例代码」，汇编写法是为讲解自创的助记符示意；项目的真实汇编语法以 LLVM 工具链（NyuziToolchain）的 `clang` 为准。若要在模拟器里实际跑，可写等价的 C 代码由编译器生成这些指令，再用 `nyuzi_emulator -v` 反查。

#### 4.2.5 小练习与答案

**练习 1**：为什么 I 格式的 `alu_op` 解码要写成 `alu_op_t'({1'b0, ifd_instruction[28:24]})`（强制最高位为 0）？

**参考答案**：浮点操作码的 `[5]` 位都是 1（见 u2-l2 的 `alu_op_t`，`OP_ADD_F` 起 `[5]=1`）。I 格式是「标量 + 立即数」，浮点指令需要两个寄存器操作数、没有立即数形式，所以 I 格式不应能编码出浮点操作；强制最高位为 0 正好把 `[28:24]` 映射到整数操作码空间，避免歧义。

**练习 2**：分支指令的偏移量为什么在解码级就要「乘 4」？

**参考答案**：Nyuzi 指令 32 位定长，按字（4 字节）编址，分支目标地址 = PC + 偏移 × 4。在解码级一次性把 `[24:5]`/`[24:0]` 左移两位拼好（`{..., 2'b00}`），后续整数执行级只需做一次加法即可得到目标 PC，不必再移位，缩短关键路径。

---

### 4.3 中断替换：精确中断的实现

#### 4.3.1 概念说明

「精确中断（precise interrupt）」是指：从软件视角看，中断恰好发生在两条指令的边界上——中断点之前的所有指令都已执行完，中断点之后的所有指令都还没执行。这对操作系统正确保存/恢复现场至关重要。

但 Nyuzi 流水线有两个让精确中断变难的特性（见模块顶部注释）：

1. **指令可以乱序退休（retire out of order）**：因为整数、浮点、访存三条路径长度不等（1/5/2+ 级），先进入的指令可能后完成。
2. **某线程可能已有指令在流水线里，且这些指令随后会触发回滚**（比如分支预测错误）。

如果在执行级中途才响应当前指令的中断，就很难保证「之前的都做完、之后的都没做」。Nyuzi 的解法很巧妙：**在解码级，如果发现当前线程有挂起且使能的中断，就把正在解码的这条指令「替换」成一条带 trap 标志的空壳指令**。这条空壳指令不读任何寄存器、不做任何运算，只是把 `trap_cause = TT_INTERRUPT` 送到写回级去触发陷阱入口。

因为替换发生在解码级，被替换的指令「尚未生效」，而它之后的指令还没进入流水线，所以中断点天然精确。

#### 4.3.2 核心流程

```
cr_interrupt_pending  ┐
cr_interrupt_en       ├──> 按位与  ──────────────────────┐
                                                         │
ior_pending           ┐                                  │
dd_load_sync_pending  ├─> 取反后清掉「两段式指令进行中」的线程 ─┤
sq_store_sync_pending ┘                                  │
                                                         ▼
                                            masked_interrupt_flags
                                                         │
                              取本线程位 & !ocd_halt ─────▶ raise_interrupt
                                                         │
                        与 illegal/syscall/breakpoint/各 fault 合并
                                                         ▼
                                                     has_trap = 1
                                                         │
                          trap_cause 优先级仲裁（中断优先级最高）
                                                         ▼
                          抑制所有 has_scalar*/has_vector*/has_dest
                         （空壳指令不读寄存器、不写目的）
```

注意流程里有一个重要细节：**两段式指令进行中时不允许打断**。像 I/O 访问、同步访存（load_sync/store_sync）这类指令需要发射两次——第一次排队、第二次取结果。如果在两次之间插入中断，内部状态就乱了。所以解码级会把这些正在等待第二拍的线程从中断位图里清掉。

#### 4.3.3 源码精读

模块顶部注释把「为什么要用替换」讲得很清楚，是本节最重要的背景：

[hardware/core/instruction_decode_stage.sv:L27-L37](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L27-L37) — 注释说明：中断通过「替换指令」实现，与流水线早期阶段产生的 trap 处理方式一致；之所以这样做，是因为指令会乱序退休，且某线程可能已有待回滚的指令在流水线里。

中断触发条件的核心三行：

[hardware/core/instruction_decode_stage.sv:L279-L281](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L279-L281) — `masked_interrupt_flags` 把「挂起」与「使能」按位与，再用三个取反信号屏蔽掉正处于两段式指令中间的线程；`raise_interrupt` 取出当前被解码线程的那一比特（且片上调试器未 halt）。本行上方 L272-L278 的注释解释了为什么要屏蔽两段式指令。

`raise_interrupt` 一旦为真，就被并入 `has_trap`，并优先赋值为 `TT_INTERRUPT`：

[hardware/core/instruction_decode_stage.sv:L237-L241](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L237-L241) — `has_trap` 是所有异常源的汇总：非法指令、syscall、breakpoint、待处理中断，以及取指级带来的 tlb_miss/page_fault/supervisor_fault/alignment_fault/executable_fault。任一为真，这条指令就变成 trap 空壳。

[hardware/core/instruction_decode_stage.sv:L246-L268](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L246-L268) — `trap_cause` 优先级仲裁。注意 **`raise_interrupt` 被判在第一位**，中断优先级最高；其后依次是 TLB miss（必须在 page fault 之前，因为 TLB miss 时权限位无效）、page fault、supervisor、对齐、不可执行、非法指令、syscall、breakpoint。这个顺序与 dcache_data_stage 保持一致（见 L243-L245 注释）。

「替换」的关键机制：所有「需要读寄存器/写目的」的字段都带 `&& !has_trap`，所以一旦 trap 置位，这条空壳指令既不读源、也不写目的，只把 trap 送走：

[hardware/core/instruction_decode_stage.sv:L293-L294](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L293-L294) — `has_scalar1` 仅在 `!has_trap` 时为真；[L303-L304](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L303-L304)、[L319](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L319)、[L321](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L321)、[L330](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L330) 的 `has_scalar2`/`has_vector1`/`has_vector2`/`has_dest` 同理。这就是「替换」的字面含义——原指令的所有副作用都被抹掉。

最后，即使原取指无效，trap 也要被送出去——valid 信号用 `|| has_trap` 把 trap「搭车」放行：

[hardware/core/instruction_decode_stage.sv:L444-L461](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L444-L461) — 输出锁存。`id_instruction_valid <= (ifd_instruction_valid || has_trap) && ...`，注释 L456-L457 点明这是为了「把 ifetch 的 fault 和 TLB miss 搭在指令里送出去」；同时如果写回级正好对本线程回滚，则抑制本拍输出，避免把已废弃的指令推进去。

#### 4.3.4 代码实践

**实践目标**：理解「中断替换 = 抑制原指令所有副作用 + 强制 trap_cause=TT_INTERRUPT」。

**操作步骤**（源码阅读型实践）：

1. 在 [L279-L281](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L279-L281) 假设 `raise_interrupt=1`，沿信号追：
   - 它进入 [L237-L241](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L237-L241) 使 `has_trap=1`；
   - 进入 [L248-L249](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L248-L249) 使 `trap_cause=TT_INTERRUPT`；
   - 使 [L293](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L293)、[L303](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L303)、[L319](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L319)、[L321](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L321)、[L330](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L330) 的各 `has_*` 全部为 0；
   - 进入 [L384-L385](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L384-L385) 使 `pipeline_sel=PIPE_INT_ARITH`。
2. 思考：这条空壳指令到了 `operand_fetch_stage` 会怎样？因为 `has_scalar1/2`、`has_vector1/2` 全为 0，它不读任何寄存器；到了整数执行级也无运算；到写回级，`has_trap=1` 触发 trap 处理（详见 u7-l3）。

**需要观察的现象**：原指令的 32 位编码此时完全「失忆」——除了它携带的 PC（`decoded_instr_nxt.pc = ifd_pc`，[L380](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L380)），其余字段都被 trap 抹平。这个 PC 就是中断返回时要恢复的「下一条待执行指令」地址，保证了精确性。

**预期结果**：你能用自己的话讲清「中断不是插队执行，而是把解码级的当前指令改写成一条只携带 PC 和 trap 标志的空指令」。

**待本地验证**：若要观察运行时行为，可在模拟器里用一个会触发中断的程序（例如设置 `CR_INTERRUPT_TRIGGER`，见 u7-l2），用 `-v` 跟踪，确认中断发生在某条指令边界上、且 `CR_TRAP_PC` 指向被替换指令的 PC。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `masked_interrupt_flags` 要把 `ior_pending`、`dd_load_sync_pending`、`sq_store_sync_pending` 对应的线程位清掉？

**参考答案**：I/O 访问和同步访存（load_sync/store_sync）都是「两段式」指令——第一次发射排队、第二次取结果，且第一次已经更新了内部状态（如 store queue、sync 监视位）。如果在这两拍之间插入中断，中断处理返回后第二次发射的语义就错了。所以在这些指令的第二拍到来前，禁止打断对应线程。

**练习 2**：`trap_cause` 仲裁里为什么 `raise_interrupt` 排在所有 fault 之前，而且 `TT_TLB_MISS` 必须排在 `TT_PAGE_FAULT` 之前？

**参考答案**：中断是「外部异步事件」，应在当前指令边界被优先响应，所以排第一。TLB miss 必须先于 page fault，是因为发生 TLB miss 时页表项不存在、权限位（present/writable/executable）都无效；若先判 page fault 会用无效的权限位误报。这与 dcache_data_stage 里的判定顺序保持一致（见 L243-L245 注释），确保两处对同一访问报出相同的异常类型。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「手工解码器」小任务。

**任务**：给定下面这条（示例）32 位指令字，手工完成解码，并把结果填成一个 `decoded_instruction_t`。

```
instr = 32'b110_000_000101_00010_00000_00001
            │└─┘└────────┘└────┘└────┘└────┘
            │ alu  [25:20] [19:15][14:10] [4:0]
           [31:29]
```

（位段从高到低依次为：`[31:29]=110`、`[25:20]=000101`、`[19:15]=00010`、`[14:10]=00000`、`[9:5]=00000`、`[4:0]=00001`。）

**要求**：

1. 判断格式（R/I/M/C/B）与 `dlut_out` 表项特征码。
2. 查 [casez 表 L164-L176](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L164-L176)，写出 `dlut_out` 各开关值。
3. 据此给出：`alu_op`（查 [defines.svh:L81-L124](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L81-L124)）、`op1_src`/`op2_src`/`mask_src`、`scalar_sel1`/`scalar_sel2`/`dest_reg`、`pipeline_sel`、`has_trap`。
4. 用一句话描述这条指令「在做什么」。

**参考答案**：

1. `[31:29]=110` → R 格式；特征码 `[31:25]=110_000`（最高 7 位），命中 `7'b110_000_?`（R 标量/标量）。
2. `dlut_out = {illegal=F, dest_vector=F, has_dest=T, imm_loc=IMM_ZERO, scalar1_loc=SCLR1_4_0, scalar2_loc=SCLR2_19_15, has_vector1=F, has_vector2=F, vector_sel2_9_5=F, op1_vector=F, op2_src=OP2_SRC_SCALAR2, mask_src=MASK_SRC_ALL_ONES, store_value_vector=F, call=F}`。
3. `alu_op = alu_op_t'(instr[25:20]) = 000101 = OP_ADD_I`；`op1_src=OP1_SRC_SCALAR1`（scalar1=`[4:0]`=00001=s1）；`op2_src=OP2_SRC_SCALAR2`（scalar2=`[19:15]`=00010=s2）；`mask_src=MASK_SRC_ALL_ONES`；`dest_reg=[9:5]=00000=s0`；`pipeline_sel=PIPE_INT_ARITH`（整数加法）；`has_trap=0`。
4. 这条指令是 `add_i s0, s1, s2`：把标量寄存器 s1 与 s2 相加，结果写入 s0，无掩码，走整数流水线。

---

## 6. 本讲小结

- 解码级把 32 位指令翻译成统一的结构体 `decoded_instruction_t`，后续所有级只面向这个结构体编程，实现编码格式与流水线逻辑的解耦。
- 格式识别只看最高几位：R（`[31:29]=110`）、I（`[31]=0`）、M（`[31:30]=10`）、C（`[31:28]=1110`）、B（`[31:28]=1111`）；一张 `casez` 大表把特征码映射成 `dlut_out` 控制开关。
- 同一段位（如 `[4:0]`）在不同格式下含义不同（scalar1 或 vector1），区别由 `dlut_out` 的开关决定；立即数位段、符号扩展、分支偏移「乘 4」都在解码级一次性完成。
- 操作数四要素 op1/op2/mask/store_value 的来源完全由格式决定，总览表（L38-L52）是查阅的快捷入口。
- 精确中断通过「解码级指令替换」实现：挂起且使能的中断会把当前指令改写成只携带 PC 与 `TT_INTERRUPT` 的空壳指令，并抑制其所有读/写寄存器副作用；两段式指令（IO/sync）进行中不打断。
- 所有异常（中断、各 fault、非法指令、syscall、breakpoint）汇成 `has_trap`，并按固定优先级仲裁出 `trap_cause`，统一送到写回级处理。

## 7. 下一步学习建议

解码级产出的 `decoded_instruction_t` 接下来会进入**线程选择级（thread_select_stage）**与**记分牌（scoreboard）**——这是下一讲 u4-l3 的主题。建议：

1. **先读 u4-l3**：看 `thread_select_stage` 如何用记分牌跟踪 `decoded_instruction_t` 里的 `has_scalar1/2`、`has_vector1/2`、`has_dest` 来规避数据冒险，理解为什么解码级要把「是否读某寄存器」拆成独立的 `has_*` 标志。
2. **再读 u5-l1（operand_fetch_stage）**：看它如何消费 `op1_src`/`op2_src`/`mask_src`/`store_value_vector` 这些字段，把本讲「查表得到的开关」真正变成读寄存器的动作。
3. **延展阅读**：对照 [tools/emulator/instruction-set.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/instruction-set.h) 中模拟器的解码，确认两侧对同一种格式的位段切分完全一致——这是协同仿真（u8-l3）能逐指令比对的前提。
