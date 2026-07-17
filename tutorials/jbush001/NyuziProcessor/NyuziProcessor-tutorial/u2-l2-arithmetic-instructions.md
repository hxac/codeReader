# 算术与比较指令

## 1. 本讲目标

本讲是 ISA 系列的第二讲（接 u2-l1）。在上一讲里，我们已经建立了 Nyuzi 的数据模型（标量 `scalar_t`、16 通道向量 `vector_t`）和指令格式地图。本讲只解决一个问题：**这些寄存器里的数，到底是怎么被「算」出来的**。

读完本讲，你应该能够：

- 读懂 `alu_op_t` 这张贯穿硬件、模拟器、工具链三方的「操作编码表」，知道每一种算术/逻辑/移位/比较/浮点操作对应哪个编码。
- 区分「有符号比较」「无符号比较」「浮点比较」三者语义上的差别，以及它们为什么都只产生 0 或 1。
- 理解一条指令是如何在解码阶段被分流到「整数流水线」或「浮点流水线」的，并知道有一个反直觉的细节：整数乘法其实跑在浮点流水线里。
- 会用模拟器的 `-v` 跟踪输出，把一条指令的「PC → 寄存器写回」对应起来，验证你对指令语义的理解。

> 说明：本任务规格里提到的助记符 `setne_i` / `setgt_u` 在仓库的实际汇编器中并不存在。真实的助记符是 `cmpne_i` / `cmpgt_u`（见 `tests/core/isa/compare_forms.S`）。本讲一律使用真实助记符，不编造指令。

## 2. 前置知识

本讲默认你已经掌握 u2-l1 的内容。这里快速复习三个要点：

1. **标量与向量**：`scalar_t` 是 32 位；`vector_t` 由 16 个 `scalar_t` 拼成（512 位），16 个通道可以同时算。大多数算术指令对 16 个通道并行执行，1 个周期完成。
2. **指令格式**：算术类指令属于 R（寄存器）或 I（立即数）两大格式。解码后，操作数被整理成统一的 `op1`、`op2`、`mask`、`store_value` 四路来源（见 u2-l1 / u4-l2）。
3. **三方共享一张编码表**：硬件 `defines.svh` 里的 `alu_op_t`、模拟器 `instruction-set.h` 里的 `arithmetic_op` 枚举、以及 LLVM 工具链，用的是同一套 6 位操作码。这是 Nyuzi 能做「协同仿真」（硬件与模拟器互验）的前提。

还需要一个直觉：**比较指令的「结果」不是 1/-1，而是 0 或 1**。Nyuzi 没有「条件码寄存器（flags）」，比较指令直接把 0/1 写进目标寄存器。这个设计在后面的代码实践里会反复看到。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲解读的部分 |
| --- | --- | --- |
| `hardware/core/defines.svh` | 全项目共享的类型与常量定义 | `alu_op_t` 操作编码表、`float32_t` 浮点格式、`pipeline_sel_t` 流水线选择、`decoded_instruction_t` 里的 `alu_op`/`compare` 字段 |
| `tools/emulator/instruction-set.h` | C 模拟器侧的 ISA 编码（与硬件同构） | `arithmetic_op` 枚举 |
| `hardware/core/int_execute_stage.sv` | 硬件「整数执行级」，单周期算术/逻辑/移位/比较都在这里 | 16 通道并行 ALU、比较的减法实现、CLZ/CTZ、移位、倒数估计 |
| `hardware/core/instruction_decode_stage.sv`（辅助） | 解码级，决定指令走哪条流水线 | 整数/浮点分流、`compare` 标志 |
| `tools/emulator/processor.c`（辅助） | 模拟器的参考实现，是验证硬件行为的「标尺」 | `scalar_arithmetic_op`、`-v` 跟踪输出格式 |

## 4. 核心概念与源码讲解

本讲的四个最小模块：**整数算术、逻辑与移位、比较操作、浮点算术**。每个模块都对应 `alu_op_t` 编码表里的一组操作码。

### 4.1 整数算术（add_i / sub_i / mull_i / mulh_u / mulh_i）

#### 4.1.1 概念说明

「整数算术」指对 32 位整数做加减乘。Nyuzi 的整数是 32 位宽，加法/减法按二进制补码运算、自然回绕（溢出不触发异常，直接截断到 32 位）。乘法分三类：

- `mull_i`：取乘积的**低 32 位**（无论有符号无符号，低 32 位都一样）。
- `mulh_u`：无符号乘，取乘积的**高 32 位**。
- `mulh_i`：有符号乘，取乘积的**高 32 位**。

为什么要把「低 32 位」和「高 32 位」拆成两条指令？因为 32×32 的乘积是 64 位，一条 32 位指令写不回去。编译器要算 64 位乘法时，会同时发出 `mull_i`（取低位）和 `mulh_u`/`mulh_i`（取高位）两条指令。

#### 4.1.2 核心流程

一条 `add_i s2, s0, s1` 的生命周期：

1. **解码**：解码级把操作码识别为 `OP_ADD_I`（编码 `6'b000101`），填进 `decoded_instruction_t.alu_op`。
2. **分流**：因为 `OP_ADD_I` 的最高位（bit 5）是 0，且不是乘法，解码级把它分给**整数流水线** `PIPE_INT_ARITH`。
3. **取操作数**：`operand_fetch_stage` 从寄存器堆读出 `s0`、`s1`，放进 `of_operand1`、`of_operand2`（向量形式，标量广播到 16 通道）。
4. **执行**：`int_execute_stage` 的 16 通道 ALU 各自算 `operand1 + operand2`。
5. **写回**：结果经写回级写进 `s2`。

对向量形式 `add_i v2, v0, v1`，16 个通道同时各算各的加法，1 周期完成——这就是 SIMD 的威力。

#### 4.1.3 源码精读

**操作编码表**——所有算术/逻辑/移位/比较/浮点操作的 6 位编码都在这一张表里（硬件侧）：

[defines.svh:80-124](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L80-L124) 定义了 `alu_op_t`。其中整数算术是这几行：

```systemverilog
OP_ADD_I  = 6'b000101,    // Add integer
OP_SUB_I  = 6'b000110,    // Subtract integer
OP_MULL_I = 6'b000111,    // Multiply integer low
OP_MULH_U = 6'b001000,    // Unsigned multiply, return high bits
OP_MULH_I = 6'b011111,    // Signed multiply high
```

**模拟器侧的同构枚举**（数值与硬件完全一致，便于协同仿真比对）：

[instruction-set.h:29-73](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/instruction-set.h#L29-L73) 里的 `arithmetic_op`，例如 `OP_ADD_I = 5`、`OP_MULL_I = 7`、`OP_MULH_U = 8`、`OP_MULH_I = 31`。

**硬件执行**——`int_execute_stage` 是一个 `generate for` 循环，把同一套 ALU 逻辑实例化 16 份，每份处理一个向量通道：

[int_execute_stage.sv:76-78](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L76-L78) 开始 16 通道生成。加法与减法在主 `case` 里只有两行：

[int_execute_stage.sv:230-231](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L230-L231) — `OP_ADD_I: lane_result = lane_operand1 + lane_operand2;` 和 `OP_SUB_I: lane_result = difference;`（`difference` 在第 98 行就算好了）。

注意：`OP_MULL_I` / `OP_MULH_U` / `OP_MULH_I` **不在** `int_execute` 的 `case` 里（命中 `default: lane_result = 0`，见 [int_execute_stage.sv:247](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L247)）。它们被解码级显式地送去**浮点流水线**，因为乘法器硬件就放在那里：

[instruction_decode_stage.sv:386-392](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L386-L392) — 判定条件 `alu_op[5] || alu_op == OP_MULL_I || alu_op == OP_MULH_U || alu_op == OP_MULH_I || alu_op == OP_FTOI` 成立时走 `PIPE_FLOAT_ARITH`，否则走 `PIPE_INT_ARITH`。

> 这是一个值得记住的反直觉点：**整数乘法（`mull_i`/`mulh_*`）跑在浮点流水线里**。原因是硬件只设了一个乘法器，放在浮点单元；整数乘法复用它。

**模拟器参考实现**（不分流水线，直接算）：

[processor.c:897-905](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L897-L905) — `OP_ADD_I`/`OP_SUB_I`/`OP_MULL_I` 直接用 C 的 `+ - *`；`OP_MULH_U` 用 `(uint64_t)` 强转后右移 32 位取高位。模拟器是「功能模型」，不关心走哪条流水线，只保证结果与硬件一致。

#### 4.1.4 代码实践

**实践目标**：验证 `mulh_u` 与 `mull_i` 配合能拼出一个 64 位乘积。

**操作步骤**（源码阅读型 + 本地可选运行）：

1. 打开 [processor.c:901-905](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L901-L905)，确认 `mull_i` 取低 32 位、`mulh_u` 取高 32 位。
2. 取两个数，比如 `a = 0xFFFFFFFF`、`b = 0xFFFFFFFF`。手算：64 位无符号乘积是 `0xFFFFFFFE00000001`。
   - `mull_i` 应得 `0x00000001`（低 32 位）。
   - `mulh_u` 应得 `0xFFFFFFFE`（高 32 位）。
3. 若本地已按 u1-l2 构建出 `bin/nyuzi_emulator`，可写一段 C 调用并 `printf` 这两个值，用 `run_emulator` 运行核对。

**需要观察的现象 / 预期结果**：两个「半截」结果拼起来正好等于完整的 64 位乘积 `0xFFFFFFFE00000001`。若本地无法运行，标记「待本地验证」，但手算结论是确定的。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mull_i` 不区分有符号/无符号？
**答**：因为乘积的低 32 位在有符号和无符号解释下完全相同（这是补码运算的性质），所以低 32 位用一条指令即可；只有高 32 位才需要 `mulh_i`（有符号）和 `mulh_u`（无符号）区分。

**练习 2**：`add_i` 溢出时会发生什么？
**答**：什么陷阱都不会触发。结果按 32 位二进制自然回绕（截断），第 33 位的进位被丢弃。需要检测溢出的代码必须自己用比较指令判断。

---

### 4.2 逻辑与移位（or / and / xor / shl / shr / ashr / clz / ctz / move / sext）

#### 4.2.1 概念说明

这一组是「位级」操作：

- **按位逻辑**：`or`、`and`、`xor`，逐位运算。
- **移位**：`shl`（左移）、`shr`（逻辑右移，高位补 0）、`ashr`（算术右移，高位补符号位）。移位量取 `operand2` 的低 5 位（0–31）。
- **位计数**：`clz`（前导零个数，count leading zeros）、`ctz`（末尾零个数，count trailing zeros）。输入为 0 时结果为 32。
- **搬运/扩展**：`move`（把 `operand2` 原样写到结果）、`sext8`/`sext16`（把 8/16 位有符号数符号扩展到 32 位）。

一个容易踩的坑：**这一组里的「一元操作」（clz/ctz/move/sext/ftoi/reciprocal）操作的是 `operand2`，不是 `operand1`**。二元操作（and/or/xor/移位）则是 `operand1 <op> operand2`，其中移位的「被移数」是 `operand1`、「移位量」是 `operand2`。

#### 4.2.2 核心流程

以 `ashr s2, s0, s1` 为例（把 `s0` 算术右移 `s1` 位）：

1. 解码 → `OP_ASHR`（`6'b001001`），bit5=0，走整数流水线。
2. 取操作数：`operand1 = s0`（被移数），`operand2 = s1`（移位量）。
3. 执行：根据是否为 `ashr` 决定右移时高位补 0 还是补符号位，再右移 `operand2[4:0]` 位。
4. 写回 `s2`。

`clz`/`ctz` 用一个 `casez` 查找表在一个周期内得出结果（组合逻辑），不需要循环计数。

#### 4.2.3 源码精读

**编码**（节选）：

```systemverilog
OP_ASHR = 6'b001001,   // Arithmetic shift right (sign extend)
OP_SHR  = 6'b001010,   // Logical shift right (no sign extend)
OP_SHL  = 6'b001011,   // Logical shift left
OP_CLZ  = 6'b001100,   // Count leading zeroes
OP_CTZ  = 6'b001110,   // Count trailing zeroes
```
见 [defines.svh:90-95](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L90-L95)。

**移位的硬件实现**——关键在「补什么」：

[int_execute_stage.sv:187-188](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L187-L188) — `shift_in_sign` 在 `OP_ASHR` 时取符号位、否则取 0；然后把 32 个符号/0 位拼到高位，再右移 `lane_operand2[4:0]` 位。这就同时实现了逻辑右移和算术右移。

**CLZ 查找表**——用 33 行 `casez` 把「第一个 1 在第几位」直接映射成前导零个数：

[int_execute_stage.sv:105-143](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L105-L143) — 例如 `32'b1????...` 表示最高位就是 1，`lz = 0`；`32'b01???...` 表示最高位是 0、次位是 1，`lz = 1`；以此类推；全 0 时 `lz = 32`。CTZ 的表结构对称，见 [int_execute_stage.sv:146-184](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L146-L184)。

注意 CLZ/CTZ 的查找表查的是 `lane_operand2`（即 operand2），见 `unique casez (lane_operand2)`。这印证了「一元操作用 operand2」。

**主 `case` 里的位级操作**：

[int_execute_stage.sv:220-249](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L220-L249) — 例如：
```systemverilog
OP_SHL: lane_result = lane_operand1 << lane_operand2[4:0];
OP_MOVE: lane_result = lane_operand2;
OP_OR: lane_result = lane_operand1 | lane_operand2;
OP_CLZ: lane_result = scalar_t'(lz);
OP_SEXT8: lane_result = scalar_t'($signed(lane_operand2[7:0]));
```

**模拟器对照**：

[processor.c:905-916](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L905-L916) — 模拟器用 C 的 `>>` / `<<` 和 `__builtin_clz` / `__builtin_ctz`，并在输入为 0 时显式返回 32（因为 `__builtin_clz(0)` 是未定义行为）。

#### 4.2.4 代码实践

**实践目标**：确认 `clz(0) == 32` 这个边界行为。

**操作步骤**：

1. 读 [processor.c:911-914](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L911-L914)，注意 `value2 == 0 ? 32u : __builtin_clz(value2)` 这一行的存在意义。
2. 对照硬件 [int_execute_stage.sv:140](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L140) 的 `32'b000...0: lz = 32;`。

**预期结果**：硬件查表和模拟器特判都给出 `clz(0) = 32`、`ctz(0) = 32`。两边一致，这正是协同仿真能通过的前提。若本地有模拟器，可写一句 `printf` 打印 `__builtin_clz` 与 Nyuzi `clz` 指令的结果对比。

#### 4.2.5 小练习与答案

**练习 1**：`ashr` 和 `shr` 对同一个负数 `0x80000000` 右移 4 位，结果分别是什么？
**答**：`ashr` 补符号位 → `0xF8000000`；`shr` 补 0 → `0x08000000`。

**练习 2**：移位量是 `operand2` 的低 5 位。如果 `operand2 = 33`，实际移几位？
**答**：33 的低 5 位是 `100001` 取低 5 位 = `00001`，即移 1 位（33 mod 32 = 1）。

---

### 4.3 比较操作（cmpxx_i 有符号 / cmpxx_u 无符号 / cmpxx_f 浮点）

#### 4.3.1 概念说明

Nyuzi 的比较指令是「set 型」：它把比较结果（真=1、假=0）直接写进目标寄存器，而不是设置条件码。这使得比较结果可以像普通数据一样参与后续运算、做掩码、进向量通道。

比较分三大类，每类有 eq/ne/gt/ge/lt/le 六种关系：

| 类别 | 助记符后缀 | 语义 | 示例 |
| --- | --- | --- | --- |
| 有符号整数 | `_i` | 把操作数当补码有符号数比 | `cmpgt_i`：有符号大于 |
| 无符号整数 | `_u` | 把操作数当无符号数比 | `cmpgt_u`：无符号大于 |
| 浮点 | `_f` | 按 IEEE754 浮点序比 | `cmpgt_f`：浮点大于 |

为什么 `_i` 和 `_u` 要分开？因为同样的 32 位比特模式，解释成有符号还是无符号，大小关系会颠倒。最经典的例子：`0xFFFFFFFF` 作为无符号数是最大值（4294967295），作为有符号数是 -1（最小值之一）。所以 `cmpgt_u` 和 `cmpgt_i` 对同一对输入可能给出相反的结果。

C 编译器据此选择指令：源码里写 `int` 比较会生成 `_i`，写 `unsigned` 比较会生成 `_u`，写 `float` 比较会生成 `_f`。

#### 4.3.2 核心流程

所有比较都以「先做减法」为基础。设 `a = operand1`、`b = operand2`，计算 `diff = a - b`，从中提取两个关键标志：

- **borrow（借位）**：无符号意义下 `a < b` 时借位为 1。
- **negative（差值符号位）**与 **overflow（有符号溢出）**：二者组合出有符号意义下的大小关系。

然后六种关系各用一个布尔表达式把这些标志拼成 0/1：

- 相等：`diff == 0`
- 无符号小于：`borrow && diff != 0`
- 有符号大于：`!overflow ? (diff > 0 有符号) : 翻转` —— 硬件里浓缩成一句 `signed_gtr && !zero`，其中 `signed_gtr = (overflow == negative)`。

有符号比较为什么需要 `overflow`？因为有符号减法在溢出时，差值的符号位会「说谎」。用 \[ overflow == negative \] 这一经典判据修正后，`signed_gtr` 才正确反映「a 有符号大于 b」。

浮点比较（`_f`）不在这套减法标志里，它走浮点流水线，由浮点单元按 IEEE754 序比较。

#### 4.3.3 源码精读

**编码**（节选）：

```systemverilog
OP_CMPEQ_I = 6'b010000,   OP_CMPNE_I = 6'b010001,
OP_CMPGT_I = 6'b010010,   OP_CMPGE_I = 6'b010011,
OP_CMPLT_I = 6'b010100,   OP_CMPLE_I = 6'b010101,
OP_CMPGT_U = 6'b010110,   OP_CMPGE_U = 6'b010111,
OP_CMPLT_U = 6'b011000,   OP_CMPLE_U = 6'b011001,
```
见 [defines.svh:97-106](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L97-L106)。浮点比较见 [defines.svh:117-122](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L117-L122)。

**硬件：减法与标志**（每个通道都算一遍，比较指令和 sub_i 共用这套减法）：

[int_execute_stage.sv:98-102](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L98-L102) —
```systemverilog
assign {borrow, difference} = {1'b0, lane_operand1} - {1'b0, lane_operand2};
assign negative = difference[31];
assign overflow = lane_operand2[31] == negative && lane_operand1[31] != lane_operand2[31];
assign zero = difference == 0;
assign signed_gtr = overflow == negative;
```

**硬件：六种整数比较 → 0/1**：

[int_execute_stage.sv:232-241](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L232-L241) — 例如：
```systemverilog
OP_CMPEQ_I: lane_result = {{31{1'b0}}, zero};
OP_CMPGT_I: lane_result = {{31{1'b0}}, signed_gtr && !zero};
OP_CMPLT_U: lane_result = {{31{1'b0}}, borrow && !zero};
```
结果恒为 `0` 或 `1`（高 31 位补 0）。注意整数比较 `_i` / `_u` 都在**整数流水线**里完成（bit5=0）。

**解码级设置 `compare` 标志**：比较指令会被打上 `compare=1` 标记，向量比较时用来做「按通道归约」（16 个通道各比一次，再 OR 成一个标量掩码）。完整清单见 [instruction_decode_stage.sv:425-442](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L425-L442)。

**模拟器对照**（语义最清楚的参考实现）：

[processor.c:917-936](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L917-L936) — 例如 `OP_CMPGT_I: return (uint32_t)((int32_t)value1 > (int32_t)value2);`、`OP_CMPGT_U: return (uint32_t)(value1 > value2);`。有符号比较用 `(int32_t)` 强转，无符号比较直接用 `uint32_t`，差别一目了然。

**真实汇编用法**：[tests/core/isa/compare_forms.S:44-53](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/compare_forms.S#L44-L53) 用 `cmpeq_i`/`cmpgt_u` 等测试向量形式；`arithmetic_macros.h` 里则用 `cmpne_i`/`cmpeq_i` 的结果配合 `bz`/`bnz` 来判定自检是否通过（见 [arithmetic_macros.h:25-27](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/arithmetic_macros.h#L25-L27)）。这正是「比较结果直接当分支条件」的典型用法。

#### 4.3.4 代码实践

**实践目标**：体会 `_i` 与 `_u` 在同一对输入下的差异。

**操作步骤**：

1. 取 `a = 0xFFFFFFFF`、`b = 0x00000001`。
2. 预测：
   - `cmpgt_u a, b`：无符号下 `0xFFFFFFFF (大) > 1` → 真 → 结果 `1`。
   - `cmpgt_i a, b`：有符号下 `-1 > 1` → 假 → 结果 `0`。
3. 用 [processor.c:921-930](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L921-L930) 的逻辑核对你的预测。

**预期结果**：两条指令结果不同（`1` vs `0`），直观说明有符号/无符号比较必须分开。这一步纯源码阅读即可确认，无需运行；若要运行验证，标记「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：比较指令的目标寄存器里写进去的是什么类型的值？
**答**：32 位整数，且只可能是 `0` 或 `1`（高 31 位补 0）。它不是布尔类型，而是一个普通的 32 位寄存器值。

**练习 2**：`cmpge_i`（有符号大于等于）如何用 `signed_gtr` 和 `zero` 表达？
**答**：`signed_gtr || zero`（见 [int_execute_stage.sv:235](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L235)）。「大于」或「等于」任一成立即为「大于等于」。

---

### 4.4 浮点算术（add_f / sub_f / mul_f / itof / ftoi / reciprocal）

#### 4.4.1 概念说明

浮点算术处理 IEEE754 binary32（即 C 的 `float`）。在 `defines.svh` 里它的位结构是：

\[ \text{float32} = \underbrace{1}_{\text{sign}} \oplus \underbrace{8}_{\text{exponent}} \oplus \underbrace{23}_{\text{significand}} \text{（共 32 位）} \]

见 [defines.svh:28-36](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L28-L36)。

浮点算术包括：

- `add_f` / `sub_f` / `mul_f`：浮点加减乘。**注意：没有除法指令**。
- `itof`：整数转浮点；`ftoi`：浮点转整数（向零截断）。
- `reciprocal`：倒数估计（近似值，只有约 6 位精度），用来软件实现除法：`a / b ≈ a * reciprocal(b)`。
- 浮点比较 `cmpxx_f`（见 4.3）。

#### 4.4.2 核心流程

浮点算术的关键特征是：**它走一条独立的、更长的流水线**。

- 解码级发现 `alu_op[5] == 1`（所有 `_f` 操作码最高位都是 1，例如 `OP_ADD_F = 6'b100000`），于是分给 `PIPE_FLOAT_ARITH`。
- 浮点流水线是 **5 级**（`fp_execute_stage1`–`stage5`），因为浮点加法需要对阶、规整、舍入，远比整数加法复杂（这条流水线的内部细节是 u5-l3 的主题，本讲不展开）。
- 整数执行级 `int_execute_stage` 只负责浮点操作里的**一个例外**：`reciprocal`（倒数估计），因为它只是查一张 ROM 表，单周期就能完成。

所以从「指令分流」看：

```
解码级
  ├── alu_op[5]==1 或 整数乘法  → 浮点流水线（5 级）：add_f/sub_f/mul_f/itof/ftoi/mull_i/mulh_*
  └── 其余整数运算            → 整数流水线（1 级）：add_i/sub_i/and/or/xor/移位/比较/clz/ctz/reciprocal
```

一个细节：`ftoi` 的编码 `OP_FTOI = 6'b011011`（bit5=0），但解码级在 [instruction_decode_stage.sv:388-389](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L388-L389) 把它显式列入浮点流水线清单——因为它要把浮点数「对阶截断」成整数，复用了浮点单元的硬件。

#### 4.4.3 源码精读

**编码**（节选）：

```systemverilog
OP_ADD_F  = 6'b100000,   OP_SUB_F = 6'b100001,   OP_MUL_F = 6'b100010,
OP_ITOF   = 6'b101010,   OP_FTOI  = 6'b011011,   OP_RECIPROCAL = 6'b011100,
```
见 [defines.svh:108-112](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L108-L112)。注意 `add_f`/`sub_f`/`mul_f` 的 bit5 都是 1，`itof`/`ftoi`/`reciprocal` 的 bit5 是 0。

**整数执行级里的「浮点例外」——倒数估计**：

[int_execute_stage.sv:190-216](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L190-L216) — `reciprocal` 用 `reciprocal_rom`（[int_execute_stage.sv:192-194](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L192-L194)）查表，并对「除以 0 → ∞」「除以 ±∞ → ±0」「除以 NaN → NaN」做了特判。主 `case` 里：

[int_execute_stage.sv:246](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L246) — `OP_RECIPROCAL: lane_result = reciprocal;`。

`add_f`/`sub_f`/`mul_f` 在 `int_execute` 的 `case` 里**不存在**（命中 `default: 0`），因为它们去了浮点流水线。

**模拟器参考实现**（功能模型，直接用 C 的 `float` 运算）：

[processor.c:956-963](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L956-L963) —
```c
case OP_ADD_F: return value_as_int(value_as_float(value1) + value_as_float(value2));
case OP_MUL_F: return value_as_int(value_as_float(value1) * value_as_float(value2));
case OP_ITOF:  return value_as_int((float)(int32_t)value2);
```
`value_as_float` / `value_as_int` 是同一块 32 位内存在 `uint32_t` 与 `float` 之间的类型双关。`reciprocal` 的模拟器实现见 [processor.c:939-948](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L939-L948)，注释明确写了「Reciprocal only has 6 bits of accuracy」（只有 6 位精度）。

> 重要提醒：硬件的浮点实现**不是完全 IEEE754 兼容**的（例如舍入、特殊值的处理有简化，`reciprocal` 是近似值）。这是 Nyuzi 作为「实验性处理器」的有意取舍。模拟器用宿主机的 `float`（通常完全 IEEE754）当参考，因此协同仿真里浮点结果有「已知差异」，相关限制在 u8-l3 讲。

#### 4.4.4 代码实践

**实践目标**：用 `reciprocal` + `mul_f` 软件实现一次浮点除法，并理解为何是近似。

**操作步骤**：

1. 读 [processor.c:939-961](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L939-L961)，确认 `reciprocal(b)` 只有约 6 位尾数精度。
2. 手算一个例子：`10.0f / 4.0f`。
   - 精确结果 = `2.5f`。
   - 用 `reciprocal(4.0f)` 得到 ≈ `0.249...`（6 位精度），再 `mul_f(10.0f, …)` ≈ `2.49…`，与 `2.5` 有微小误差。
3. 若本地有模拟器，写 C：`float approx = 10.0f * (1.0f / 4.0f);`（编译器对 `float` 除法通常就是这样 lowering 成 reciprocal+mul），用 `printf("%.9g", approx)` 观察。

**需要观察的现象 / 预期结果**：软件除法结果在「6 位有效数字」附近开始偏离精确值。这正是 Nyuzi 不提供硬件除法、改用近似 reciprocal 的代价。本地运行结果标记「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `add_f` 不在 `int_execute_stage` 里实现？
**答**：浮点加法需要对阶（让两个操作数指数对齐）、尾数相加、再规整和舍入，无法在单周期内完成。硬件为它单独建了一条 5 级流水线（`fp_execute_stage1`–`5`）。`int_execute_stage` 只负责单周期能完成的简单操作。

**练习 2**：`ftoi` 的操作码 bit5 是 0，为什么还是走浮点流水线？
**答**：因为「浮点转整数」需要把浮点尾数按指数移位并截断，复用了浮点单元里的对阶/移位硬件。解码级在 [instruction_decode_stage.sv:388-389](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L388-L389) 把 `OP_FTOI` 显式列入浮点流水线清单。

---

## 5. 综合实践

把四个模块串起来：**写一个用 `cmpne_i`、`cmpgt_u` 和 `add_f` 的条件计算，并用模拟器 `-v` 跟踪每条指令的寄存器写回。**

> 真实助记符说明：本任务规格里写的 `setne_i` / `setgt_u` 在仓库汇编器中不存在，对应真实指令是 `cmpne_i`（有符号不等）和 `cmpgt_u`（无符号大于）。下面用真实助记符。

### 步骤 1：写一段 C 代码

```c
// 示例代码（非项目原有代码，仅为本实践编写）
#include <stdio.h>

int main(void) {
    unsigned int a = 5, b = 3;
    int changed = (a != b);        // 期望生成 cmpne_i，结果写 1
    int greater  = (a > b);        // unsigned 比较，期望生成 cmpgt_u，结果写 1
    int flags = changed + greater; // 期望生成 add_i，结果 2

    float x = 1.5f, y = 2.5f;
    float sum = x + y;             // 期望生成 add_f，结果 4.0

    printf("flags=%d sum=%.1f\n", flags, sum);
    return 0;
}
```

### 步骤 2：编译并反汇编，建立 PC → 指令映射

用 NyuziToolchain 把它编译成 ELF，再用工具链自带的反汇编工具（具体命令名待本地确认，例如 `llvm-objdump -d` 或工具链提供的 objdump）查看。在反汇编里找到：

- `cmpne_i` 指令及其地址（PC）；
- `cmpgt_u` 指令及其地址；
- `add_f` 指令及其地址。

记下这三条指令的 PC。C 里的 `int` 比较应生成 `_i` 系列、`unsigned` 比较应生成 `_u` 系列、`float` 加法应生成 `add_f`——这正是 4.3 里「编译器按类型选指令」的体现。

### 步骤 3：转 hex 并用 `-v` 跟踪

按 u1-l4 的方式把 ELF 转成 hex 镜像，然后：

```bash
./run_emulator -v program.hex
```

模拟器的 `-v` 跟踪输出格式（来自 [processor.c:637-638](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L637-L638) 与 [processor.c:654-657](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L654-L657)）是：

```
<PC> [th <线程号>] s<寄存器号> <= <写入值>        # 标量写回
<PC> [th <线程号>] v<寄存器号>{<掩码>} <= <16个通道值>  # 向量写回
```

**注意：跟踪行里没有指令助记符**，只有 PC 和写回值。你要用步骤 2 记下的 PC 去「对账」。

### 步骤 4：核对预期

按记下的 PC 过滤跟踪输出，应能观察到：

- `cmpne_i` 那一行的 PC 处，目标标量寄存器被写入 `0x00000001`（因为 `5 != 3`）。
- `cmpgt_u` 那一行的 PC 处，目标标量寄存器被写入 `0x00000001`（无符号 `5 > 3`）。
- `add_f` 那一行的 PC 处，目标寄存器被写入 `4.0f` 的 IEEE754 编码 `0x40800000`。

**预期结果 / 待本地验证**：三条比较/算术指令的写回值与上述一致。`cmpne_i`、`cmpgt_u` 的结果恒为 `0` 或 `1`；`add_f` 写回的是 `0x40800000`（即 4.0f）。若本地尚未构建工具链，可仅完成步骤 1–2 的源码/反汇编阅读，并把运行核对标记为「待本地验证」。

> 进阶：把 `a, b` 改成 `int`（有符号）再反汇编，观察 `cmpgt_u` 是否变成 `cmpgt_i`——亲手验证 4.3 的结论。

## 6. 本讲小结

- `alu_op_t` 是一张贯穿硬件 `defines.svh`、模拟器 `instruction-set.h`、LLVM 工具链三方的 6 位操作编码表；整数算术/逻辑/移位/比较/浮点全部统一在这一张表里。
- 简单的整数运算（加减、逻辑、移位、CLZ/CTZ、整数比较、move、sext、reciprocal）在**单周期整数执行级** `int_execute_stage` 里完成，且对 16 个向量通道**并行实例化**。
- 比较指令是「set 型」：结果直接写进目标寄存器，只可能是 `0` 或 `1`；有符号 `_i`、无符号 `_u`、浮点 `_f` 三套语义分离，编译器按 C 类型自动选择。
- 浮点算术（`add_f`/`sub_f`/`mul_f`）和**整数乘法**（`mull_i`/`mulh_*`）、`ftoi` 都走**独立的 5 级浮点流水线**；唯一的例外是 `reciprocal`，它用 ROM 查表在整数执行级单周期完成（约 6 位精度）。
- 模拟器 `processor.c` 的 `scalar_arithmetic_op` 是这些指令的「功能参考实现」，用 C 的原生运算写成；它是协同仿真里比对硬件行为的标尺。
- `-v` 跟踪只输出「PC + 寄存器写回值」，没有助记符，需要结合反汇编建立 PC→指令映射来解读。

## 7. 下一步学习建议

本讲只讲了「指令语义」和「整数执行级」。建议接下来：

1. **u2-l3（内存访问）**：算完之后怎么把结果存回内存？`load_32`/`store_32` 与向量块访存、scatter/gather 是下一块拼图。
2. **u2-l4（分支与控制寄存器）**：比较结果如何驱动 `bz`/`bnz` 分支，以及控制寄存器组。
3. **u5-l2（整数执行单元深入）**：本讲的 `int_execute_stage` 还承担**分支解析与流水线回滚**、特权指令检测、性能事件统计，这些留到流水线篇细讲。
4. **u5-l3（浮点五级流水线）**：本讲里被反复「推迟」的浮点加法内部（对阶、规整、舍入、guard/round/sticky 位）将在那里展开。
5. 想立刻动手验证，可先读 [tests/core/isa/int_arithmetic_forms.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/int_arithmetic_forms.S) 和 [compare_forms.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/compare_forms.S)，它们是本讲所有指令的现成自检测试。
