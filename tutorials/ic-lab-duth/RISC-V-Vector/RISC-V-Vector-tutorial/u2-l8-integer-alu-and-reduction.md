# 整数 ALU 与跨 lane 归约树

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `v_int_alu.sv` 里**简单 ALU、MUL、DIV、归约树**四段逻辑各自的职责与流水线位置。
- 解释为什么 `VMUL` 需要 3 个执行周期、`VDIV` 需要 4 个执行周期，并能对应到字节切片部分积与逐位组恢复除法的实现。
- 看懂 `vex.sv` 里用 `generate` 连出的「跨 lane 归约树」：lane N 在 EX1/EX2/EX3/EX4 分别与 N+1/N+2/N+4/N+8 号 lane 配对，以及为什么只有偶数 lane（乃至更窄的 lane 子集）会激活归约逻辑。
- 把本讲的「变延迟」和 u2-l7 讲过的转发点、计分板（u2-l5）串起来，理解一条整数指令在执行级的真实开销。

本讲是 u2-l7「vEX 与 vex_pipe 执行流水」的直接下钻：u2-l7 讲的是执行级的「骨架与路由」，本讲讲的是被路由进 `INT_FU` 之后，`v_int_alu` 内部到底算了什么、花了几个周期。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**第一，为什么不同指令会有不同周期数。** 一条 32 位整数加法用一个加法器一拍就能算完；但 32 位乘法本质是「32 个部分积再求和」，若想跑高频就不能用一个超大的组合乘法器（关键路径太长、频率上不去）。工程上常见的折中是把乘法**切成几拍**，每拍算一部分、用寄存器隔开组合路径。除法更慢，通常一位一位（或几位一组）地试商。因此执行级对不同 `microop` 给出**不同的最早就绪拍**，这就是本仓库反复强调的「变延迟」。

**第二，什么叫「恢复除法（restoring division）」。** 这是硬件除法器最经典的算法之一。想象你手算十进制除法：每猜一位商，就用「被除数剩余部分 − 除数×该位商」来更新余数；若减出来是负的，说明这一位商猜大了，要回退。二进制恢复除法每次只试 1 位：

\[
\text{若 } (R - D) \ge 0 \text{，则商位 }=1,\ R \leftarrow R-D;\quad \text{否则商位 }=0,\ R \text{ 不变（恢复）。}
\]

其中 \(R\) 是逐步左移的余数寄存器，\(D\) 是除数。本仓库把 32 位商拆成 4 组、每组 8 位，分摊到 EX1–EX4 四拍，每拍内用 `for` 循环跑 8 次上述迭代。

**第三，什么是「跨 lane 归约树」。** 归约（reduction）指令（如 `vradd` 把一个向量所有元素求和）需要把分散在多个 lane 里的数据汇拢。顺序相加会很慢；树形相加则每「一层」把数据量减半。本仓库的做法是：**把归约树直接铺进流水线**——EX1 配对相邻两个 lane，EX2 把两对合并，EX3 再合并…… 层数 \(=\lceil \log_2(\text{LANES}) \rceil\)。因为每个 lane 都是独立的 `vex_pipe` 实例，归约信号必须通过顶层 `vex.sv` 的 `generate` 连线在 lane 之间穿梭，这就是本讲要讲清的「跨 lane」二字。

> 名词速查（承接前几讲）：`microop` 是 7 位整数操作码（见 u1-l4）；`fu` 是 2 位功能单元编码，`INT_FU=2'b10`（见 u1-l4 的 `vmacros.sv`）；EX1/EX2/EX3/EX4 是 `vex_pipe` 的 4 级数据流水（见 u2-l7）；`mask_i` 是逐元素写回掩码（见 u2-l6）。

## 3. 本讲源码地图

本讲只涉及两个核心文件，外加一个宏定义头：

| 文件 | 角色 |
| --- | --- |
| [rtl/vector/v_int_alu.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv) | 整数 ALU 主体。把简单 ALU / MUL / DIV / 归约树四段逻辑塞进同一个 4 级流水（EX1–EX4），并用一组输出 mux 选出当前拍应该呈现的结果。 |
| [rtl/vector/vex.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv) | 执行级顶层。例化 `VECTOR_LANES` 条 `vex_pipe`，并用 4 段 `generate` 把归约信号在 lane 之间互联。 |
| [rtl/vector/vex_pipe.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv) | 单条 lane 的流水外壳。按 `fu` 路由到 `v_int_alu`/`v_fp_alu`，负责数据寄存器与「变延迟」的 `ready_res_*` 链，以及把归约中间结果累加到 lane 0。 |
| [rtl/vector/vmacros.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv) | 功能单元编码与归约操作码宏 `RDC_ADD/RDC_AND/RDC_OR/RDC_XOR`。 |

一句话定位：`v_int_alu` 是「算什么」，`vex.sv` 的 `generate` 是「怎么连」，`vex_pipe` 是「怎么把不同周期的结果统一收口」。

## 4. 核心概念与源码讲解

### 4.1 简单 ALU 操作

#### 4.1.1 概念说明

「简单 ALU」指**一拍之内能用单个组合电路算完**的整数操作：加减、带立即数的加减、移位、按位逻辑、比较、ReLU/step 等。它们全部在 EX1 这一拍出结果，对应的 `ready_res_ex1_o` 被拉高，后续 EX2/EX3/EX4 只是把结果原样往后传（详见 4.2.4 提到的 `ready_res_*` 链）。

这部分逻辑用一个 `case (microop_i)` 把 7 位操作码翻译成一行组合运算。它与 u1-l4 里讲的 `v_int_op_t` 枚举**一一对应**：`7'b0000001` 是 `VADD`，`7'b0000010` 是 `VADDI`，以此类推。

#### 4.1.2 核心流程

简单 ALU 的数据准备 + 运算流程：

1. **操作数预处理**：把输入 `data_a_ex1_i / data_b_ex1_i / imm_ex1_i` 同时转成无符号 `_u`、有符号 `_s`、半字零扩展 `_wu` 三套视图，供不同指令选用。
2. **译码运算**：`case (microop_i)` 命中某条指令 → 用对应的视图做一次组合运算 → 写入 `result_int`，同时拉高 `valid_int_ex1`。
3. **输出选择**：`result_int` 经零扩展到 `EX1_W` 宽度（`result_int_ex1`），在 4.4 节的输出 mux 里参与 EX1 结果竞争。
4. **default 兜底**：未识别的操作码输出 `'x` 且 `valid_int_ex1=0`，保证不会误触发 `ready`。

关键一点：`valid_int_ex1` 既是「这条指令属于简单 ALU」的标志，也是后续变延迟机制的输入——它会让 EX2 起的 `ready_res_*` 一直保持有效，使数据寄存器退化为「直通」。

#### 4.1.3 源码精读

操作数三视图的生成（无符号 / 有符号 / 半字零扩展）：

[rtl/vector/v_int_alu.sv:L99-L109](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L99-L109) —— 注意 `_wu` 把 16 位半字零扩展到 32 位，是 `VADDW/VADDIW` 等「半字运算」指令专用。

简单 ALU 的译码主体（节选前几条与移位、比较、ReLU 几类）：

[rtl/vector/v_int_alu.sv:L115-L251](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L115-L251) —— 这里能看到几个设计细节：

- `VADD/VADDI/VADDW/VADDIW` 分别用 `data_a+data_b`、`data_a+imm`、半字加法；`VSUB/VSUBW` 同理做减法。
- 移位类（`VSLL/VSLLI/VSRA/VSRAI/VSRL/VSRLI`）的移位量一律取 `data_b[4:0]` 或 `imm[4:0]`——只看低 5 位，因为 32 位数移位量上限是 31。
- `VSRA` 用 `>>>`（算术右移），`VSRL` 用 `>>`（逻辑右移）；注意 `VSRA` 的被移数反而接了无符号 `data_a_u_ex1`，算术/逻辑的差别实际由 `>>>`/`>>` 运算符与被移数符号性共同决定。
- 比较类（`VSEQ/VSLT/VSLTU`）结果是 1 位的真值，`VSLT` 直接用原始 `data_a_ex1_i < data_b_ex1_i`（综合工具按操作数的 `signed` 性决定比较方式）。
- 神经网络常用的 `VRELU/VBRELU/VPRELU/VSTEP` 也在这一段：`VRELU` 是标准 ReLU（负数清零），`VBRELU` 按 `data_a[0]`（即模 2）在 ReLU 与「反 ReLU」间二选一，`VPRELU` 把负数屏蔽（`valid_int_ex1 = valid_i & (data_a_s_ex1 >= 0)`）。
- `default` 分支输出 `'x` 并把 `valid_int_ex1` 拉低，避免未定义操作码误产生就绪信号。

最终把 32 位 `result_int` 零扩展到 `EX1_W`（=160 位）宽度：

[rtl/vector/v_int_alu.sv:L252](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L252) —— `EX1_W` 取的是「所有 EX1 中间结果的最大宽度」（这里是 MUL 的 4×40=160 位），所以简单 ALU 的高位全是零填充。

#### 4.1.4 代码实践

**实践目标**：用源码阅读验证「移位量只取低 5 位」与「比较类结果宽度」，避免在写测试程序时误用。

**操作步骤**：

1. 打开 [v_int_alu.sv 的 L147-L221](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L147-L221)。
2. 找到 `VSLL`、`VSRA`、`VSRL` 三条，确认它们的移位量都是 `[4:0]`。
3. 找到 `VSEQ/VSLT/VSLTU`，观察赋值表达式右端是 `(data_a < data_b)` 这种关系运算——结果只有 1 位有效，赋给 32 位 `result_int` 时高位自动补 0。

**需要观察的现象 / 预期结果**：

- 若你在仿真里喂一条 `vsll v0, v1, v2`，其中 `v2` 的某元素 = 33（`0b100001`），实际移位量是 `33 & 0x1F = 1`，而不是 33。
- `vslt` 的结果元素只会是 `0` 或 `1`，不会出现其他数值。

> 说明：本实践为源码阅读型，结论可直接从 `case` 表达式推出，无需运行仿真即「预期结果」如上；若要眼见为实，可在 dot_product 之外自备一条 `vsll` 的 CSV 跑 u1-l5 的流程验证（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`VADDIW` 和 `VADDI` 有什么区别？分别用了哪套操作数视图？
**答案**：`VADDI` 是 32 位加立即数，用 `data_a_u_ex1 + imm_u_ex1`（全字无符号）；`VADDIW` 是「半字」加立即数，用 `data_a_wu_ex1 + imm_wu_ex1`，即先把操作数低 16 位零扩展到 32 位再相加，等价于只在低 16 位上做加法。

**练习 2**：为什么 `VSEQ/VSLT` 用原始 `data_a_ex1_i/data_b_ex1_i`，而 `VSLTU` 用 `_u` 视图？
**答案**：`VSEQ` 只判相等，符号性无关；`VSLT` 是有符号小于，依赖操作数本身的 `signed` 性（综合器按声明判定）；`VSLTU` 是无序（无符号）小于，必须显式用 `$unsigned` 后的 `_u` 视图，确保做无符号比较。

**练习 3**：若 `microop_i` 落到 `default`，`valid_int_ex1` 是什么？它对下游有什么影响？
**答案**：`valid_int_ex1=0`、`result_int='x`。下游的 `ready_res_ex1_o` 不会被这条指令拉高，变延迟链也不会误判就绪，相当于这条 lane 在 EX1「没有整数结果产出」。

---

### 4.2 乘除法实现（MUL 与 DIV）

MUL 与 DIV 是本讲的「变延迟」主角：它们无法一拍算完，必须把运算摊到多拍。注意，**所有指令走的都是同一条 EX1→EX2→EX3→EX4 物理流水**（见 u2-l7），所谓「3 周期 / 4 周期」指的是「结果最早在哪一拍可被转发/写回」，而不是「指令提前离开流水线」。

#### 4.2.1 概念说明（MUL：字节切片部分积）

两个 32 位数相乘，积最宽 64 位。直接写 `a*b` 综合出的乘法器关键路径很长，不利于高频。本仓库的做法是把乘数 `b` **按字节切成 4 段**，每段 8 位，分别与被乘数 `a` 相乘，得到 4 个「部分积」，再在下一拍按字节位置对齐求和：

\[
a \times b = a\cdot b_0 + (a\cdot b_1)\cdot 2^{8} + (a\cdot b_2)\cdot 2^{16} + (a\cdot b_3)\cdot 2^{24}
\]

其中 \(b_i\) 是 `b` 的第 \(i\) 字节。每个部分积 \(a\cdot b_i\) 是「32 位 × 8 位」，宽度 40 位（这正是 `PARTIAL_SUM_W = DATA_WIDTH + 8 = 40`）。

为处理**有符号乘法**，EX1 先把两个操作数取绝对值（若为负则做 `~x+1`），EX3 再根据真实符号决定是否把乘积取反。`VMULH/VMULHSU/VMULHU` 取乘积的高 32 位，`VMUL/VMULWDN` 取低 32 位（由 `upper_part` 标志控制）。

#### 4.2.2 概念说明（DIV：逐位组恢复除法）

除法用前文讲过的**恢复除法**。32 位商太多，一拍算不完，本仓库把它拆成 **4 拍、每拍算 8 位**（`DIV_CALC_CYCLES=4`、`DIV_BIT_GROUPS=32/4=8`）。每拍内用一个 `for` 循环跑 8 次「左移—试减—定商位—可能恢复」的迭代，把这部分商位填出来，再把「余数 + 部分商 + 除数」三件套寄存到下一拍继续。到 EX4 做最终符号修正，并按 `is_rem` 选择输出商（`VDIV/VDIVU`）还是余数（`VREM/VREMU`）。

#### 4.2.3 核心流程

**MUL 流程（3 拍，结果在 EX3 就绪）**：

1. **EX1 译码 + 部分积**：`case` 识别 `VMUL/VMULH/VMULHSU/VMULHU/VMULWDN`，设 `sign_mul_ex1`（是否带符号）、`diff_type`（VMULHSU 的混合符号）、`upper_part`（取高位还是低位）。对操作数取绝对值，算 4 个 40 位部分积，拼成 160 位 `result_mul_ex1`；并把符号信息寄存到 EX2。
2. **EX2 对齐求和**：4 个部分积按 \(0/8/16/24\) 字节偏移零扩展到 64 位后相加，得到 64 位 `extended_sum`，作为 `result_mul_ex2`。
3. **EX3 符号修正 + 选半字**：若结果应为负，做 `~result + 1`；再按 `upper_part_ex3` 选高 32 位或低 32 位，得到 `result_mul_ex3`。此时 `valid_mul_ex3=1` → `ready_res_ex3_o` 拉高。

**DIV 流程（4 拍，结果在 EX4 就绪）**：

1. **EX1 译码 + 首 8 位**：`case` 识别 `VDIV/VDIVU/VREM/VREMU`，设 `sign_div_ex1`、`is_rem_ex1`。被除数、除数取绝对值，跑 8 次恢复除法迭代得到首 8 位商与中间余数；把 `{商, 余数, 除数}` 寄存到 EX2，同时算好符号信息。
2. **EX2/EX3 各 8 位**：每拍从上一拍的「三件套」里取出余数与部分商，继续跑 8 次迭代，再写回三件套。
3. **EX4 末 8 位 + 符号修正**：跑最后 8 次迭代得到完整 32 位商与最终余数；按符号对商和余数分别取反，按 `is_rem` 选一个作为 `result_div_ex4`，`ready_res_ex4_o` 拉高。

#### 4.2.4 源码精读

关键宽度参数（`PARTIAL_SUM_W` 决定 MUL 部分积位宽，`DIV_CALC_CYCLES/DIV_BIT_GROUPS` 决定 DIV 分几拍、每拍几位）：

[rtl/vector/v_int_alu.sv:L60-L62](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L60-L62)

**MUL EX1——取绝对值 + 4 个字节切片部分积**：

[rtl/vector/v_int_alu.sv:L322-L335](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L322-L335) —— `data_a/data_b` 在 `sign_mul_ex1` 为真且 MSB 为 1 时做 `~x+1` 取绝对值；`part_1..part_4` 分别是 `data_a * data_b[字节k]`，4 个 40 位部分积拼成 `result_mul_ex1`；同时把「最终符号」`sign_ex2` 寄存。

**MUL EX2——按字节偏移对齐求和**：

[rtl/vector/v_int_alu.sv:L350-L360](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L350-L360) —— `extended_1..4` 分别左移 0/8/16/24 字节，相加得到 64 位 `extended_sum`。

**MUL EX3——符号修正 + 高/低半字选择**：

[rtl/vector/v_int_alu.sv:L381-L384](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L381-L384) —— `result_mul_wide` 按符号取反；`upper_part_ex3` 选 `[63:32]`（VMULH 类）还是 `[31:0]`（VMUL 类）。这就是 VMUL 为什么需要 3 拍：EX1 部分积 → EX2 求和 → EX3 符号与选半字，缺一不可。

**DIV EX1——恢复除法迭代主体**（EX2/EX3/EX4 是同构的复制粘贴）：

[rtl/vector/v_int_alu.sv:L442-L465](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L442-L465) —— 看这段就能理解「逐位组恢复除法」：

- `remainder = {remainder[W-2:0], quotient[W-1]}` 与 `quotient = quotient << 1` 合起来等价于「把 {余数, 商} 整体左移 1 位」。
- `diff = remainder - divider`；若 `diff[W]`（借位位）为 1，说明不够减 → 商位写 0、余数恢复（不变）；否则商位写 1、余数更新为 `diff`。
- 这个过程先手写 1 次，再用 `for (i=0; i<DIV_BIT_GROUPS-1; i++)` 循环 7 次，合计 **每拍 8 位**。
- `result_div_ex1 = {..., quotient, remainder, divider_init}` 把「商 + 余数 + 除数」三件套打包传给下一拍。

**DIV EX4——最终符号修正与商/余数选择**：

[rtl/vector/v_int_alu.sv:L616-L619](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L616-L619) —— `remainder_final` 按 `sign_div_ex4`（余数跟随被除数符号）取反，`result_final` 按 `sign_res_div_ex4`（商跟随被除数⊕除数符号）取反，`is_rem_ex4` 选余数（VREM）还是商（VDIV）。EX1→EX2→EX3→EX4 共 4 拍，每拍 8 位，合起来正好 32 位商，这就是 VDIV 需要 4 拍的原因。

**输出 mux——为什么不同指令在不同拍就绪**：

[rtl/vector/v_int_alu.sv:L846-L869](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L846-L869) —— 这段是理解「变延迟」的关键：

- `ready_res_ex1_o = valid_int_ex1 | valid_rdc_ex1` → 简单 ALU（与短归约）EX1 就绪。
- `ready_res_ex2_o = valid_rdc_ex2` → EX2 只有归约会就绪（MUL/DIV 在 EX2 还没算完，不拉高）。
- `ready_res_ex3_o = valid_mul_ex3 | valid_rdc_ex3` → **MUL 在 EX3 就绪**。
- `ready_res_ex4_o = valid_div_ex4 | valid_rdc_ex4` → **DIV 在 EX4 就绪**。

这个 `ready_res_*` 信号回到 `vex_pipe`，就是 u2-l7 讲的「让数据寄存器退化为直通」的开关：一旦某拍就绪，后续拍 `data_exN <= data_exN-1` 只是把已算好的结果往后搬运，不再重算。

#### 4.2.5 代码实践

**实践目标**：跟踪一条 `vmul` 在 `v_int_alu` 内部的「数据宽度变化曲线」，直观看到 3 拍分别算什么。

**操作步骤**：

1. 读 [v_int_alu.sv 的 MUL 三段](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L316-L384)，记录每拍的输出宽度：
   - EX1：`result_mul_ex1` = `{part_4,part_3,part_2,part_1}` = 4×40 = **160 位**（4 个未对齐的部分积）。
   - EX2：`result_mul_ex2` = `extended_sum` = **64 位**（已对齐求和的完整乘积）。
   - EX3：`result_mul_ex3` = **32 位**（符号修正后选出的高/低半字）。
2. 同样对 DIV 跟踪 [三件套](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L465) `{quotient, remainder, divider}`：EX1/EX2/EX3 都是 96 位（3×32），EX4 收敛到 32 位。

**需要观察的现象 / 预期结果**：

| 指令 | EX1 宽度 | EX2 宽度 | EX3 宽度 | EX4 宽度 | 最早就绪拍 |
| --- | --- | --- | --- | --- | --- |
| 简单 ALU（VADD…） | 32（填 0 到 160） | — | — | — | EX1 |
| VMUL | 160 | 64 | 32 | — | EX3 |
| VDIV | 96 | 96 | 96 | 32 | EX4 |

这就是 EX1_W/EX2_W/EX3_W/EX4_W 在 `vex_pipe` 里被设成 `4*(W+8)/3W/3W/W` 的原因——它们必须容纳「所有可能的中间结果」中的最大者。

> 这条「宽度曲线」是纯源码阅读结论；若要在波形上确认，可在仿真里单独抓一条 `vmul` 的 `res_int_ex1/ex2/ex3` 信号（待本地验证）。

#### 4.2.6 小练习与答案

**练习 1**：为什么 MUL 要在 EX1 先把操作数取绝对值，到 EX3 再统一取反，而不是直接做有符号乘法？
**答案**：把符号与绝对值分离后，EX1 的部分积、EX2 的求和都按无符号做，电路简单、关键路径短；只有 EX3 一处需要做符号修正（一次 `~x+1`）。若全程带符号，每个部分积的符号处理都会增加组合深度，拖低频率。

**练习 2**：`VMULH`（有符号、取高位）与 `VMULHU`（无符号、取高位）在 EX1 的 `sign_mul_ex1` 和 `upper_part` 各是什么？
**答案**：二者 `upper_part` 都是 `1'b1`（取高 32 位）；`VMULH` 的 `sign_mul_ex1=1`（带符号，EX1 取绝对值、EX3 取反），`VMULHU` 的 `sign_mul_ex1=0`（纯无符号，不取绝对值也不取反）。`VMULHSU` 更特殊，`diff_type=1`，符号只由被乘数决定。

**练习 3**：DIV 的 `sign_res_div_ex2 <= sign_div_ex1 & dividend[31] ^ divider[31]`（见 [L470](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L470)）为什么是「被除数符号 ⊕ 除数符号」？
**答案**：商的符号由「两数是否同号」决定：同号（⊕=0）商为正，异号（⊕=1）商为负，这正是异或的含义。注意运算符优先级，`&` 先于 `^` 结合，所以实际是 `(sign & dividend[31]) ^ divider[31]`；这是源码里值得留意的一处书写（待本地验证其与设计意图是否完全一致）。

---

### 4.3 跨 lane 归约树

#### 4.3.1 概念说明

归约指令（`VRADD/VRAND/VROR/VRXOR`，microop 高两位 `2'b10`）要把一个向量所有元素累加/归并成**一个标量**，写回到 0 号元素。若让 lane 0 顺序累加 N 个 lane，需要 N 拍；树形归约只需 \(\lceil\log_2 N\rceil\) 拍。

本仓库的精巧之处在于：**把归约树铺进已有的 EX1–EX4 流水**，不额外加拍。每条 lane 的 `vex_pipe` 在 EX1 算「本 lane 与相邻 lane 的归并」，结果随流水线前进；顶层 `vex.sv` 再用 `generate` 把「lane k 的归约输入」接到「lane k+步长 的归约输出」。步长每级翻倍：EX1 步长 1、EX2 步长 2、EX3 步长 4、EX4 步长 8。

因为每一级只保留「子树的根」，所以**激活的 lane 越来越少**：EX1 只有偶数 lane（0/2/4/6…）激活，EX2 只有 4 的倍数 lane（0/4…），EX3 只有 8 的倍数（0…），EX4 只有 16 的倍数（0）。最终的归约结果落在 lane 0，再由 `vex_pipe` 里的「临时归约寄存器」跨多个 uop 累加（处理 VL>LANES 的多寄存器展开，承接 u2-l6 的 head_uop/end_uop）。

#### 4.3.2 核心流程

以 8 lane 为例，一棵 3 级归约树的数据流（每条 lane 持有元素 \(a_k\)）：

\[
\begin{aligned}
\text{EX1}:&\quad s_0=a_0+a_1,\ s_2=a_2+a_3,\ s_4=a_4+a_5,\ s_6=a_6+a_7 \\
\text{EX2}:&\quad t_0=s_0+s_2,\ t_4=s_4+s_6 \\
\text{EX3}:&\quad u_0=t_0+t_4 = \sum_{k=0}^{7} a_k
\end{aligned}
\]

16 lane 时再叠一级 EX4：\(v_0 = u_0 + u_8 = \sum_{k=0}^{15} a_k\)。层数 = \(\lceil\log_2(\text{LANES})\rceil\)，正好对应 EX1..EX\(\lceil\log_2 N\rceil\)。这也解释了 README 里「>16 lane 需要加背压」的硬限制：EX4 之后没有更多流水级来容纳第 5 级归约。

每级 `v_int_alu` 内部用一个 `case(rdc_op)` 在 `+ / & / | / ^` 四种归并间选择；归并操作码 `rdc_op` 在 EX1 由 7 位 microop 译出（`RDC_ADD/AND/OR/XOR`，2 位），随后随流水线传递。

#### 4.3.3 源码精读

**lane 间归约信号声明**（`vex.sv` 顶层为每条 lane 准备了 4 对归约输入/输出）：

[rtl/vector/vex.sv:L53-L60](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L53-L60) —— `rdc_data_exN_i/exN_o` 是「跨 lane」的桥梁，每条 lane 一份。

**EX1 连线——步长 1，相邻 lane 配对**：

[rtl/vector/vex.sv:L125-L129](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L125-L129) —— `for (k=0; k<VECTOR_LANES; k+=2) rdc_data_ex1_i[k] = rdc_data_ex1_o[k+1]`。即偶数 lane k 接收奇数 lane k+1 的 `rdc_data_ex1_o`。结合 `vex_pipe` 里 `rdc_data_ex1_o = data_a_i`（[vex_pipe.sv:L569](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv#L569)），lane 0 在 EX1 得到的是 \(a_0 + a_1\)。

**EX2 / EX3 / EX4 连线——步长 2 / 4 / 8**：

[rtl/vector/vex.sv:L133-L153](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L133-L153) ——

- EX2：`rdc_data_ex2_i[k] = rdc_data_ex2_o[k+2]`（k=0,4,…），`rdc_data_ex2_o[k]=data_ex1[k]`（[vex_pipe.sv:L570](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv#L570)）。lane 0 在 EX2 得到 \(s_0+s_2\)。
- EX3：`rdc_data_ex3_i[k] = rdc_data_ex3_o[k+4]`，lane 0 得到 \(t_0+t_4\)，即 8 元素之和。
- EX4：`rdc_data_ex4_i[k] = rdc_data_ex4_o[k+8]`，仅在 `VECTOR_LANES>8`（即 16 lane）时生成，lane 0 得到 16 元素之和。

> 主题里说的「lane N 到 lane N−2/N−4/N−8 的连线」就是 EX2/EX3/EX4 这三段：从 lane k 的视角，它在第 N 级把自己的 `rdc_data_exN_o` 喂给「低步长」方向的 lane k−2 / k−4 / k−8（等价地，lane k−2 在 EX2 接收 lane k 的输出）。步长随级数翻倍，正是二叉归约树的硬件投影。

**哪些 lane 真正激活归约逻辑**（`v_int_alu` 用 `generate` 按 lane 号裁剪）：

[rtl/vector/v_int_alu.sv:L636-L688](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L636-L688) —— EX1 归约段的条件是 `if (!VECTOR_LANE_NUM[0])`，即 **lane 号为偶数**才生成真正的归约逻辑，奇数 lane 走 `g_rdc_ex1_stubs`（恒 0）。这就是「归约树只在偶数 lane 激活」的字面来源：每对 (2k, 2k+1) 只需要在偶数 lane 2k 保留一个归并结果。

逐级收窄的激活条件：

- EX2：`VECTOR_LANES>2 & VECTOR_LANE_NUM[1:0]==2'b00`（[L692](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L692)）→ lane 0,4,…（4 的倍数）。
- EX3：`VECTOR_LANES>4 & VECTOR_LANE_NUM[2:0]==3'b000`（[L745](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L745)）→ lane 0,8,…（8 的倍数）。8 lane 时只有 lane 0。
- EX4：`VECTOR_LANES>8 & VECTOR_LANE_NUM[3:0]==4'b0000`（[L798](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L798)）→ lane 0,16,…（16 的倍数）。16 lane 时只有 lane 0。

**每级归约的运算与就绪判定**（以 EX1 为例）：

[rtl/vector/v_int_alu.sv:L640-L678](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L640-L678) —— `case(microop_i)` 命中 `VRADD/VRAND/VROR/VRXOR` 时，`tree_result_ex1 = data_a_ex1_i <op> rdc_data_ex1_i`（op 由 `rdc_op_ex1` 指示），并设 `valid_rdc_ex1`。注意两个细节：

- `odd_rdc_override = ((vl_i-1) == VECTOR_LANE_NUM)`：当活跃元素数恰好让「对端 lane 越界」（如 vl=3，lane 2 的对端 lane 3 不在活跃范围）时，本 lane 直接取 `data_a_ex1_i`（不做归并），避免引入无效数据。
- `valid_rdc_ex1` 带 `vl_i<=2`（或 `VECTOR_LANES==2`）的条件：归约结果「最早在哪级就绪」取决于 vl。vl≤2 在 EX1 就绪、vl≤4 在 EX2、vl≤8 在 EX3、vl≤16 在 EX4——这是归约版的「变延迟」，与输出 mux 里的 `ready_res_exN_o` 联动。

**lane 0 跨 uop 累加的临时寄存器**（处理 VL>LANES 的多寄存器展开，承接 u2-l6）：

[rtl/vector/vex_pipe.sv:L395-L446](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv#L395-L446) —— 只在 `VECTOR_LANE_NUM==0`（lane 0）生成。它把每一组 uop 的归约结果按 `rdc_op_ex4` 累加进 `temp_rdc_result_ex4`；`head_uop` 时初始化（装入第一个有效结果），`end_uop` 时把累加值通过 `use_temp_rdc_result` 选到写回路径（[vex_pipe.sv:L565](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex_pipe.sv#L565)），写回 0 号元素。

**归约操作码宏**（注意一处值得复核的定义）：

[rtl/vector/vmacros.sv:L29-L33](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv#L29-L33) —— `RDC_ADD=2'b00`、`RDC_AND=2'b10`、`RDC_OR=2'b11`、`RDC_XOR=2'b11`。注意 `RDC_OR` 与 `RDC_XOR` 被定义成了**相同的 2'b11**，这意味着下游按 `rdc_op` 做 `case` 选择时（[L707-L728](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv#L707-L728) 等）二者会出现重复 case 标签，`VROR` 与 `VRXOR` 在 EX2/EX3/EX4 的行为可能无法区分（EX1 因按 7 位 microop 译码，仍能正确区分）。这看起来是一处笔误，但具体仿真/综合行为待本地验证。

#### 4.3.4 代码实践

**实践目标**：亲手把「8 lane 的归约树连线」画出来，验证 lane 间步长与激活 lane 集合。

**操作步骤**：

1. 假设 `VECTOR_LANES=8`，列 out EX1/EX2/EX3 三级的 `rdc_data_exN_i[k] = rdc_data_exN_o[k+步长]` 连线（参考 [vex.sv:L125-L153](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L125-L153)）。
2. 标出每级实际生成归约逻辑的 lane：EX1={0,2,4,6}、EX2={0,4}、EX3={0}。
3. 追踪元素 \(a_0,\ldots,a_7\) 如何在 3 拍内汇拢到 lane 0 的 EX3 结果。

**需要观察的现象 / 预期结果**：

```
EX1:  L0=a0+a1   L2=a2+a3   L4=a4+a5   L6=a6+a7      (步长1, 偶数lane)
EX2:  L0=(a0+a1)+(a2+a3)        L4=(a4+a5)+(a6+a7)   (步长2)
EX3:  L0=((a0+a1)+(a2+a3))+((a4+a5)+(a6+a7))          (步长4)
                = a0+a1+a2+a3+a4+a5+a6+a7
```

4. 再把 `VECTOR_LANES` 想象成 16，补出 EX4（步长 8，lane 0 = EX3_L0 + EX3_L8），并回答：要支持 32 lane 需要新增哪一级、为什么当前 RTL 做不到。

**预期结果**：支持 32 lane 需要第 5 级归约（步长 16），但流水线只有 EX1–EX4 四级数据寄存器，且没有背压机制让 lane 0 等待更多级；这正是 README「>16 lane 需加背压」限制的根因。

#### 4.3.5 小练习与答案

**练习 1**：为什么 EX1 归约只在偶数 lane 激活，而不是所有 lane 都算？
**答案**：相邻两个 lane (2k, 2k+1) 归并后只需保留一份结果，把它放在偶数 lane 2k 即可；若奇数 lane 也算，会重复计算且其结果无人消费。奇数 lane 走 stub（输出 0），既省面积也避免污染下游。

**练习 2**：`valid_rdc_ex1` 里的 `vl_i <= 2` 是什么意思？为什么 EX2 的条件变成 `vl <= 4`？
**答案**：归约结果在哪一级「算完」取决于要归并多少个元素。vl≤2 时 EX1 一级（一对 lane）就够，结果在 EX1 就绪；vl≤4 需要 EX1+EX2 两级，结果在 EX2 就绪；以此类推每级翻倍。`valid_rdc_exN` 就是「结果在本级已经完整」的标志，驱动 `ready_res_exN_o`。

**练习 3**：若 `VECTOR_LANES=2`，归约在哪一级完成？`vex.sv` 里 EX2/EX3/EX4 的 `generate` 会生成什么？
**答案**：2 lane 时归约在 EX1 完成（一对 lane 即全部）。`vex.sv` 里 EX2 的 `if (VECTOR_LANES>2)`、EX3 的 `>4`、EX4 的 `>8` 全部不成立，这三段 `generate` 不生成任何连线；`v_int_alu` 里 EX2/EX3/EX4 归约段同样走 stub。这正是参数化设计让归约树「按需生长」的体现。

---

## 5. 综合实践

本讲贯穿性任务是：**用 `dot_product` 示例把 MUL 与归约串起来看一遍**。该示例（[vector_simulator/examples/dot_product/instrs.csv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/examples/dot_product/instrs.csv)）恰好同时用到本讲的两类操作：

```csv
vandi, v0, v0, #0      # 累加器清零
vld,   v1, #0          # 载入数组 A
vld,   v2, #2048       # 载入数组 B
vmul,  v3, v1, v2      # 逐元素相乘 → 3 周期 (EX3 就绪)
vradd, v4, v3          # 把乘积归约求和 → 归约树 (EX1..EX3)
vadd,  v4, v0          # 加到累加器（这里 v0=0）
```

**任务步骤**：

1. **源码侧**：对照本讲 4.2 和 4.3，回答——`vmul` 的结果最早在 EX3 就绪，此时计分板（u2-l5）才会解除对 `v3` 的 RAW 等待，因此 `vradd v4, v3` 至少要等 `vmul` 进入流水线 3 拍后才能取到 `v3` 的元素；`vradd` 本身又根据 vl 决定在哪一级归约就绪。请用一张时序图把「vmul 的 3 拍 + vradd 的归约拍数」画出来。
2. **运行侧（可选，待本地验证）**：按 u1-l5 的流程，把 `dot_product/instrs.csv` 复制到 `vector_simulator/` 目录，运行 `python sim_generator.py instrs.csv 5000 8`（AVL=5000、8 lane），再用 `compile_vector_simulator.do` 跑仿真，查看 `results.log` 的 `total_cycles` 与 stall 统计。
3. **观察**：把 `VECTOR_LANES` 从 8 改成 4（需同步改 `vstructs.sv` 的 `DUMMY_VECTOR_LANES`，见 u1-l3 的耦合提醒），重跑 `dot_product`，比较 `total_cycles` 变化，并解释「lane 减半 → 归约树少一级 → 但每拍处理元素也减半」对总周期的综合影响。

> 说明：步骤 2/3 涉及实际跑仿真与改参数，本讲无法替你执行，请本地验证。源码侧的时序分析（步骤 1）是本综合实践的核心交付物。

## 6. 本讲小结

- `v_int_alu` 把**简单 ALU / MUL / DIV / 归约树**四段逻辑共用同一条 EX1–EX4 流水，靠输出 mux 与 `ready_res_*` 选择当前拍的有效结果。
- **简单 ALU**（加减/移位/逻辑/比较/ReLU）在 EX1 一拍出结果，`valid_int_ex1` 拉高 `ready_res_ex1_o`。
- **VMUL = 3 拍**：EX1 字节切片部分积（4×40=160 位）→ EX2 按字节对齐求和（64 位）→ EX3 符号修正与高/低半字选择（32 位），`ready_res_ex3_o` 在 EX3 拉高。
- **VDIV = 4 拍**：32 位商用恢复除法，每拍算 8 位（`DIV_BIT_GROUPS=8`），EX4 做符号修正并选商或余数，`ready_res_ex4_o` 在 EX4 拉高。
- **跨 lane 归约树**铺在流水线里：EX1 步长 1（偶数 lane）、EX2 步长 2、EX3 步长 4、EX4 步长 8，激活 lane 逐级收窄到 lane 0；层数 \(=\lceil\log_2\text{LANES}\rceil\)，这就是「最多 16 lane」的根因。
- 「变延迟」不等于「提前离队」：所有指令都走满 4 拍物理流水，差异只在**最早就绪拍**，由 `ready_res_*` 链让后续数据寄存器退化为直通——这与 u2-l7 的转发点、u2-l5 的计分板是一致的。

## 7. 下一步学习建议

- **横向**：本讲只讲了 `INT_FU` 路由进来的 `v_int_alu`。同一路由下的 `v_fp_alu` 目前是占位实现（见 u2-l7），可对照阅读 [rtl/vector/v_fp_alu.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_fp_alu.sv)，理解「预留接口、未填实现」的工程取舍。
- **纵向（推荐下一步）**：进入单元三，学 [u3-l1 VMU 存储单元与三路仲裁](u3-l1-vmu-memory-unit-and-arbitration.md)，看访存岔路如何与计算路在 vIS 汇聚——本讲的「ticket/lock」与归约写回都将在那里被「解耦执行」串联起来。
- **验证向**：学完单元三后可跳到 [u4-l6 SVA 断言与验证](u4-l6-sva-assertions-and-verification.md)，看 `vex_sva.sv` 如何为本讲的 microop 合法性与 X 传播做断言检查——届时你会更理解为什么 `default` 分支返回 `'x` 是有意为之。
