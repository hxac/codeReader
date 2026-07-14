# 浮点五级流水线

## 1. 本讲目标

本讲深入 Nyuzi 单核流水线里最长的一条执行路径——浮点执行流水线 `fp_execute_stage1` 到 `fp_execute_stage5`。学完后你应当能够：

- 说清一条 IEEE 754 binary32 浮点数在五级流水线里如何被「拆开、运算、再合上」；
- 区分浮点加法路径（对阶→相加→规整→舍入）与浮点乘法路径（尾数相乘→规格化→舍入），并知道它们为何能共用同一组流水级寄存器；
- 解释 guard / round / sticky 三位的作用，以及 Nyuzi 采用的「舍入到最近偶数」策略；
- 理解倒数估计 ROM `reciprocal_rom` 的用途、精度，以及它为什么在**整数执行单元**里而不是浮点流水线里；
- 指出该实现「非完全 IEEE 754 兼容」的几个具体取舍点。

## 2. 前置知识

在进入本讲前，请确认你已经掌握以下概念（它们来自前置讲义）：

- **binary32 数据模型**：一个 32 位浮点数被拆成 1 位符号、8 位指数、23 位尾数。Nyuzi 用结构体 `float32_t` 描述它：[defines.svh:28-36](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L28-L36)。指数采用偏置 127 的表示法，值为 `(-1)^sign × 1.significand × 2^(exponent-127)`。
- **隐藏位（hidden bit）**：规格化数（exponent ≠ 0）的尾数前有一个隐含的 `1.`；非规格化数（subnormal，exponent = 0）则没有。浮点硬件的第一件事就是把隐藏位「补」出来，变成 24 位整数再做整数运算。
- **流水线分流**：在 [u3-l2](u3-l2-core-pipeline.md) 中我们提到，操作数 fetch 之后指令会按 `pipeline_sel` 分成三条路径——`PIPE_MEM`、`PIPE_INT_ARITH`、`PIPE_FLOAT_ARITH`。本讲讲的就是 `PIPE_FLOAT_ARITH` 这一条。
- **alu_op 编码**：在 [u2-l2](u2-l2-arithmetic-instructions.md) 中我们说过，浮点操作码最高位为 1。具体地，`OP_ADD_F = 6'b100000`、`OP_MUL_F = 6'b100010`、`OP_ITOF = 6'b101010`、`OP_FTOI = 6'b011011`，见 [defines.svh:107-116](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L107-L116)。
- **整数乘法也走这条路**：[u2-l2](u2-l2-arithmetic-instructions.md) 已经点明，`mull_i`/`mulh_u`/`mulh_i` 这三条**整数**乘法指令并不在单周期的整数 ALU 里完成，而是「借用」浮点流水线里的 64 位乘法器。这是本讲一个反直觉但重要的设计点。

> 一点直觉：为什么浮点要专门做成 5 级？因为浮点加减乘的每一步（补隐藏位、对阶、相加、前导零计数、规格化移位、舍入）都是比较「重」的组合逻辑，塞进单周期会拖慢时钟。把它们拆成 5 拍，每一拍只做一小段逻辑，时钟频率才能拉起来。代价是延迟变长——但多线程机制（[u4-l3](u4-l3-thread-select.md)）让其他线程填满这些空拍，整体吞吐不受影响。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [hardware/core/fp_execute_stage1.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage1.sv) | 第 1 级：拆开操作数、判定大小、计算对阶移位与乘法指数、把乘数被乘数透传给下一级 |
| [hardware/core/fp_execute_stage2.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage2.sv) | 第 2 级：执行对阶右移（产生 guard/round/sticky）、执行 64 位尾数乘法 |
| [hardware/core/fp_execute_stage3.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage3.sv) | 第 3 级：尾数加减（含 round-to-even 预测）、整型↔浮点符号转换、乘法透传 |
| [hardware/core/fp_execute_stage4.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage4.sv) | 第 4 级：加法结果前导零计数（决定规格化移位量） |
| [hardware/core/fp_execute_stage5.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage5.sv) | 第 5 级：规格化移位、最终舍入、组装 inf/nan/比较结果、选出最终写回值 |
| [hardware/core/reciprocal_rom.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/reciprocal_rom.sv) | 64 项的倒数查找表，被**整数执行单元**的单周期 `OP_RECIPROCAL` 调用 |
| [hardware/core/defines.svh](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh) | `float32_t` 类型与浮点操作码定义 |
| [hardware/core/int_execute_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv) | 单周期整数 ALU，其中实例化 `reciprocal_rom` 并实现 `OP_RECIPROCAL` |
| [tests/float/README.md](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/float/README.md) | 浮点验证测试说明，明确指出硬件在 Verilator 上有舍入误差 |
| [tests/whole-program/fdiv.cpp](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/whole-program/fdiv.cpp) | 用 `1.0f / x` 验证软件除法（依赖倒数估计）的整机测试 |

## 4. 核心概念与源码讲解

### 4.1 浮点流水线全景与数据模型

#### 4.1.1 概念说明

`fp_execute_stage1` 到 `fp_execute_stage5` 这 5 个模块在 [core.sv:372-376](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L372-L376) 里顺序实例化，用 `.*` 通配连接。它们处理的不只是「浮点加减乘」，还包括：

- **浮点加/减**（`OP_ADD_F` / `OP_SUB_F`）
- **浮点乘**（`OP_MUL_F`）
- **整数乘**（`OP_MULL_I` / `OP_MULH_U` / `OP_MULH_I`）——借用同一颗乘法器
- **浮点↔整数转换**（`OP_FTOI` / `OP_ITOF`）
- **浮点比较**（`OP_CMPGT_F` 等 6 种）

更准确地说，这是一条「乘加转换」流水线。每条指令从操作数 fetch 级进入第 1 级时，会带上 `pipeline_sel == PIPE_FLOAT_ARITH` 的标记，[fp_execute_stage1.sv:299](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage1.sv#L299) 据此决定 `fx1_instruction_valid`：

```systemverilog
fx1_instruction_valid <= of_instruction_valid
    && (!wb_rollback_en || wb_rollback_thread_idx != of_thread_idx)
    && of_instruction.pipeline_sel == PIPE_FLOAT_ARITH;
```

> 这段代码做两件事：一是回滚时把被冲刷线程的本级指令作废；二是只放行浮点路径指令。注意第 1 级里这一句用到了 `wb_rollback_en`，而后面几级在第 2 级额外用 `wb_rollback_pipeline != PIPE_MEM` 来区分访存路径的回滚，这是因为浮点路径较长，回滚窗口要和访存路径错开（详见 [u5-l2](u5-l2-integer-execute.md) 关于写回结构冒险的讨论）。

#### 4.1.2 核心流程：两条并行子通路

整条流水线内部其实「并行」跑着**两条独立的子通路**，它们的中间结果分别用各自的前缀信号传递，到第 5 级再根据指令类型二选一：

```
                     ┌────────── 加法子通路（add path）──────────┐
op1, op2  →  stage1  │  补隐藏位、比大小、对阶移位量、加法指数  │
            stage2  │  对阶右移（guard/round/sticky）           │
            stage3  │  尾数加减（含 round-to-even 预测）        │
            stage4  │  前导零计数 → 规格化移位量                │
            stage5  │  规格化移位 + 最终舍入 + 组装 inf/nan     │
                     └──────────────────────────────────────────┘
                     ┌────────── 乘法子通路（mul path）──────────┐
            stage1  │  乘法指数相加、乘数被乘数透传             │
            stage2  │  ★ 64 位尾数乘积 ★                        │
            stage3  │  透传                                     │
            stage4  │  透传                                     │
            stage5  │  规格化（最多移 1 位）+ 舍入 + 组装        │
                     └──────────────────────────────────────────┘
```

注意：两条子通路在每一级都**同时**计算，只是乘法通路在第 2 级之后基本只是「搬运」乘积，而加法通路在第 3、4 级还要做加减和前导零检测。第 5 级用 `alu_op` 选择把哪条通路的结果送出。

#### 4.1.3 数据模型：`float32_t`

每条通路在每个 lane（共 16 个 SIMD 通道）上独立运行，所以你会看到所有信号都是 `[NUM_VECTOR_LANES-1:0]` 维度的数组。每个 lane 的原始操作数被 cast 成 `float32_t` 以便按字段访问：

```systemverilog
assign fop1 = of_operand1[lane_idx];
assign fop2 = of_operand2[lane_idx];
assign op1_hidden_bit = fop1.exponent != 0;    // Check for subnormal numbers
assign full_significand1 = {op1_hidden_bit, fop1.significand};
```

这段在 [fp_execute_stage1.sv:125-130](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage1.sv#L125-L130)。`full_significand` 是 24 位：把隐藏位拼到 23 位尾数前面。后续所有整数运算都作用在这 24 位（乘法时扩展到 64 位）上。

### 4.2 尾数乘积

#### 4.2.1 概念说明

浮点乘法的核心是「尾数相乘、指数相加、符号异或」三件事。其中指数相加与符号异或在第 1 级就能算完，而 24 位 × 24 位 = 48 位的尾数乘积是「重活」，被放到第 2 级。整数乘法（`mull_i`/`mulh_u`/`mulh_i`）也复用这同一颗乘法器，只是把 32 位操作数原样送进去。

#### 4.2.2 核心流程

```
stage1:  乘法指数 = exp1 + exp2 - 127   （去掉一次重复偏置）
         乘法符号 = sign1 ^ sign2
         被乘数   = full_significand1（浮点）或 operand1（整数）
         乘数     = full_significand2（浮点）或 operand2（整数）
stage2:  乘积     = sext(被乘数) × sext(乘数)   ← 64 位
stage3-4: 透传乘积
stage5:   规格化（最多右移 1 位）+ 舍入 + 组装结果
```

#### 4.2.3 源码精读

**第 1 级：算乘法指数，选乘数被乘数。**

指数相加要去掉一次偏置 127（两个偏置指数相加会比真实指数多出 127）。代码用扩展位保留进位与下溢标志：

```systemverilog
assign {mul_exponent_underflow, mul_exponent_carry, mul_exponent}
    =  {2'd0, fop1.exponent} + {2'd0, fop2.exponent} - 10'd127;
```

见 [fp_execute_stage1.sv:190-191](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage1.sv#L190-L191)。注意旁边的注释 `// XXX handle underflow`——下溢处理并不完整，这是后面「非完全 IEEE 兼容」的原因之一。

接着按是否整数乘法选择送进乘法器的数：

```systemverilog
if (imul)
begin
    // Unsigned multiply
    fx1_multiplicand[lane_idx] <= of_operand1[lane_idx];
    fx1_multiplier[lane_idx]   <= of_operand2[lane_idx];
end
else
begin
    fx1_multiplicand[lane_idx] <= scalar_t'(full_significand1);
    fx1_multiplier[lane_idx]   <= scalar_t'(full_significand2);
end
```

见 [fp_execute_stage1.sv:265-275](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage1.sv#L265-L275)。整数乘送原始 32 位操作数；浮点乘送补好隐藏位的 24 位尾数。

**第 2 级：真正算乘积。**

这是整条流水线里最「重」的一拍。对有符号高位乘（`OP_MULH_I`）做符号扩展，其余情况按无符号拼 0，然后用一个 `*` 直接做 64 位乘法：

```systemverilog
assign sext_multiplicand = {{32{fx1_multiplicand[lane_idx][31] && imulhs}},
    fx1_multiplicand[lane_idx]};
assign sext_multiplier = {{32{fx1_multiplier[lane_idx][31] && imulhs}},
    fx1_multiplier[lane_idx]};
...
// XXX Simple version. Should have a wallace tree here to collect partial products.
fx2_significand_product[lane_idx] <= sext_multiplicand * sext_multiplier;
```

见 [fp_execute_stage2.sv:115-139](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage2.sv#L115-L139)。注释明确写道：这里本该用 Wallace 树压缩部分积，但实现上偷懒用了综合工具自带的 `*`。这让逻辑更简单，但时序/面积由综合工具决定，也属于设计取舍。

> **整数乘法如何取结果？** `mull_i` 取低 32 位（`product[31:0]`），`mulh_u`/`mulh_i` 取高 32 位（`product[63:32]`）。这个选择发生在第 5 级，见 [fp_execute_stage5.sv:196-199](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage5.sv#L196-L199)。这就是为什么整数乘法必须走 5 级浮点流水线——它要等乘积算完才能取高低位。

**第 5 级：乘积规格化。**

两个 `[1, 2)` 的规格化尾数相乘，结果在 `[1, 4)`，所以最多需要右移 1 位来规格化（把多余的整数位挪回尾数）。代码用 `product[47]` 这一位判断是否需要移位：

```systemverilog
assign mul_normalize_shift = !fx4_significand_product[lane_idx][47];
assign {mul_normalized_significand, mul_guard, mul_round, mul_sticky_bits} = mul_normalize_shift
    ? {fx4_significand_product[lane_idx][45:0], 1'b0}
    : fx4_significand_product[lane_idx][46:0];
```

见 [fp_execute_stage5.sv:154-157](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage5.sv#L154-L157)。`product[47]` 为 1 表示乘积 ≥ 2，需要右移一位并把指数加 1；为 0 则已经规格化。`// XXX does not handle subnormal product` 这条注释再次提醒：**非规格化乘积不被处理**。

#### 4.2.4 代码实践：跟踪一次浮点乘法

**实践目标**：用一个具体数值，沿五级流水线验证乘积的形成。

**操作步骤（源码阅读型，已为你在下方算好）**：

计算 `1.5f × 2.5f`。两数的 IEEE 754 编码：

| 值 | 十六进制 | 符号 | 指数 | 尾数 | full_significand |
| --- | --- | --- | --- | --- | --- |
| 1.5 = 1.1₂ | 0x3FC00000 | 0 | 127 (0x7F) | 0x400000 | 0xC00000 |
| 2.5 = 1.01₂ | 0x40200000 | 0 | 128 (0x80) | 0x200000 | 0xA00000 |

沿各级传递的关键量（理论值）：

| 级 | 关键中间量 | 值 |
| --- | --- | --- |
| stage1 | `mul_exponent` = 127 + 128 − 127 | **128 (0x80)** |
| stage1 | `mul_sign` = 0 ⊕ 0 | **0** |
| stage1 | `multiplicand` / `multiplier` | **0xC00000 / 0xA00000** |
| stage2 | `significand_product` = 0xC00000 × 0xA00000 | **0x780000000000** |
| stage2 | `guard/round/sticky` | 乘法路径不在此处用，stage5 另算 |
| stage3、stage4 | 乘积透传 | 0x780000000000 |
| stage5 | `product[47]` = 0 → `mul_normalize_shift` = 1 | 右移 1 位 |
| stage5 | `mul_normalized_significand` | **0x700000** |
| stage5 | 无舍入（低位全 0）→ `mul_rounded_significand` | 0x700000 |
| stage5 | `mul_hidden_bit` = `product[46]` | 1（规格化） |
| stage5 | `mul_exponent` 不变 | 0x80 |
| stage5 | `fmul_result` = {0, 0x80, 0x700000} | **0x40700000 = 3.75** ✓ |

**需要观察的现象**：`product[47]` 是否为 0 决定是否右移；`mul_normalized_significand` 恰为 0x700000（对应 1.875 = 1.111₂ 的尾数）。

**预期结果**：最终 `fmul_result = 0x40700000`，即十进制 3.75，与 `1.5 × 2.5` 一致。

> 若你想在仿真器里亲眼看到这个结果，可参照本讲末尾「综合实践」用 `run_emulator` 跑一段 `printf("%f", 1.5f * 2.5f)` 的程序，输出应为 3.750000（**待本地验证**——具体字符串格式以你本地构建为准）。

#### 4.2.5 小练习与答案

**练习 1**：为什么整数乘法 `mull_i` 必须走 5 级浮点流水线，而不能像 `add_i` 那样在单周期整数 ALU 里完成？

**参考答案**：因为整数乘法复用了浮点流水线第 2 级里的 64 位乘法器（`sext_multiplicand * sext_multiplier`），而乘法是一个高延迟组合运算，塞不进单周期。`mulh_u`/`mulh_i` 还要取乘积的高 32 位，必须等到第 2 级乘积算出、并在第 5 级用 `product[63:32]` 选出，所以整条 5 级都得走完。

**练习 2**：`1.0f × 1.0f` 的 `product[47]` 是 0 还是 1？这会导致第 5 级做什么？

**参考答案**：`1.0` 的 full_significand = 0x800000，乘积 = 0x800000 × 0x800000 = 0x400000000000，bit 46 = 1，bit 47 = 0，所以 `mul_normalize_shift = 1`，需要右移 1 位规格化（因为 1.0 × 1.0 = 1.0，落在 [1,2) 区间，不需要进位指数）。结果应为 0x3F800000 = 1.0。

### 4.3 对阶与规整

#### 4.3.1 概念说明

浮点加减不能像整数那样直接逐位加减——两个小数点位置（指数）不同的数必须先「把小数点对齐」才能运算。对齐的方法是**把指数小的那个尾数右移**，每右移 1 位指数加 1，直到两边指数相等。对齐之后才能做尾数加减，最后再把结果「规格化」（让小数点回到标准位置）。

这个过程分三步，跨第 1、2、4、5 级：

1. **比大小 + 算移位量**（stage1）：判定哪个操作数指数大，把它放进 `_le`（larger exponent）通道，另一个放 `_se`（smaller exponent），并算出 `_se` 需要右移多少位。
2. **执行对阶右移**（stage2）：把 `_se` 右移，移出去的位形成 guard/round/sticky。
3. **前导零计数 + 规格化移位**（stage4、stage5）：相加后结果可能需要左移（抵消时高位变 0）或右移 1 位（进位），由前导零数量决定。

#### 4.3.2 核心流程

```
stage1:  if op1 指数大或相等且尾数大 → op1 入 _le, op2 入 _se
         否则                         → op2 入 _le, op1 入 _se（并翻转结果符号）
         se_align_shift = min(exp_diff, 27)
stage2:  aligned_se = significand_se >> se_align_shift
         顺带抽出 guard / round / sticky 三位
stage3:  unnormalized_sum = significand_le ± aligned_se   （含舍入预测）
stage4:  leading_zeroes = countl_zero(unnormalized_sum)   （决定左移多少）
stage5:  shifted = unnormalized_sum << norm_shift
         再做一次「右移 1 位 + 舍入」处理可能的进位溢出
```

为什么 stage1 里把移位量截到 27？因为尾数只有 24 位，右移超过 27 位时高位已经全 0，只需用 sticky 位记住「有非零位被丢弃」即可。代码：

```systemverilog
fx1_se_align_shift[lane_idx] <= exp_difference < 8'd27 ? 6'(exp_difference) : 6'd27;
```

见 [fp_execute_stage1.sv:257](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage1.sv#L257)。

#### 4.3.3 源码精读

**第 1 级：判定大小与符号。** 代码先比指数，指数相同再比尾数，并刻意在相等时让 operand1 留在 `_le`（正确处理 ±0 的符号）：

```systemverilog
assign op1_larger = fop1.exponent > fop2.exponent
        || (fop1.exponent == fop2.exponent && full_significand1 >= full_significand2);
assign exp_difference = op1_larger ? fop1.exponent - fop2.exponent
    : fop2.exponent - fop1.exponent;
```

见 [fp_execute_stage1.sv:195-198](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage1.sv#L195-L198)。随后按 `op1_larger` 把两数分到 `_le`/`_se`，并决定结果符号：大指数那个数的符号「赢」，见 [fp_execute_stage1.sv:213-242](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage1.sv#L213-L242)。一个细节：`ftoi`/`itof` 转换会「劫持」这条通路——它们把 `_le` 强制设为 0，把待转换的整数/浮点值放进 `_se`，复用后面的移位与加减逻辑完成格式转换。

**第 2 级：对阶右移 + 抽 guard/round/sticky。** 这是浮点舍入的关键一拍。代码把 27 个 0 拼在 `_se` 后面再右移，移出的最低 3 类位分别记为 guard（保留位）、round（舍入位）、sticky（粘位，只要移出位中有任何 1 就置 1）：

```systemverilog
assign {aligned_significand, guard, round, sticky_bits} = {fx1_significand_se[lane_idx], 27'd0} >>
    fx1_se_align_shift[lane_idx];
assign sticky = |sticky_bits;
```

见 [fp_execute_stage2.sv:110-112](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage2.sv#L110-L112)。guard/round/sticky 是 IEEE 754 舍入的三位标准设施：guard 和 round 是被截掉的最高两位，sticky 汇总所有更低位的「是否非零」信息，三者一起足以正确实现「舍入到最近偶数」。

**第 4 级：前导零计数。** 加减之后结果可能「不够规格化」（例如 1.0 − 0.5 = 0.5，需要左移）。代码用一个 32 项的 `casez` 表查出前导零个数，等价于一个单周期的优先编码器：

```systemverilog
unique casez (fx3_add_significand[lane_idx])
    32'b1???????????????????????????????: leading_zeroes = 0;
    32'b01??????????????????????????????: leading_zeroes = 1;
    ...
    32'b00000000000000000000000000000000: leading_zeroes = 32;
endcase
```

见 [fp_execute_stage4.sv:91-134](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage4.sv#L91-L134)。这个移位量在第 5 级被用来左移规格化。注意 `ftoi` 复用了这个字段（`fx4_norm_shift <= ftoi ? fx3_ftoi_lshift : leading_zeroes`），见 [fp_execute_stage4.sv:139](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage4.sv#L139)。

**第 5 级：规格化移位 + 指数调整。** 拿到移位量后左移尾数，并相应调整指数（减去移位量，再加回 8 是因为这里把结果摆在「24 位整数 + 8 位小数」的固定位置）：

```systemverilog
assign adjusted_add_exponent = fx4_add_exponent[lane_idx]
    - FLOAT32_EXP_WIDTH'(fx4_norm_shift[lane_idx]) + FLOAT32_EXP_WIDTH'(8);
assign shifted_significand = fx4_add_significand[lane_idx] << fx4_norm_shift[lane_idx];
```

见 [fp_execute_stage5.sv:103-106](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage5.sv#L103-L106)。

#### 4.3.4 代码实践：观察对阶与 sticky

**实践目标**：理解为什么对阶右移要保留 guard/round/sticky。

**操作步骤**：

1. 阅读 [fp_execute_stage2.sv:110-112](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage2.sv#L110-L112)，确认 `{significand_se, 27'd0} >> se_align_shift` 这一拼接把 27 个 0 拼在尾数后面。
2. 构造一个对齐移位为 27 的极端情形：两个指数相差极大的浮点数相加（例如 `1.0f + 很小的数`），此时 `_se` 几乎被完全右移出去。
3. 思考：如果 stage2 **不**保留 sticky 位，直接截断，结果会怎样？

**需要观察的现象**：当 `se_align_shift` 很大时，`aligned_significand` 几乎为 0，但 `sticky` 仍可能为 1，记录着「被丢弃的位里有非零值」这一事实。

**预期结果**：sticky 位保证后续舍入正确——例如做 `1.0 + 极小正数`，即使极小数的尾数被完全右移出 24 位有效区，只要它非零，sticky = 1 就会让结果「向上舍入」而不是简单截断为 1.0。如果你删掉 sticky 逻辑（思想实验，**不要改源码**），这类加法会出现系统性偏低误差。具体某个输入的精确位模式**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：对阶时为什么总是右移小指数那个数，而不是左移大指数那个数？

**参考答案**：左移大指数的数会丢失最高有效位（溢出），不可接受；而右移小指数的数只会丢失最低位（精度损失），用 guard/round/sticky 可以把损失纳入舍入决策。所以浮点对阶总是「小的向大的看齐」。

**练习 2**：第 4 级的 `leading_zeroes` 可能取到 32，对应什么情况？

**参考答案**：`unnormalized_sum == 0`，即两数相加恰好抵消（例如 `1.0 + (−1.0)`）。此时结果是真正的 0，第 5 级会用 `add_subnormal && add_result_significand == 0` 的分支把结果强制设为 `+0.0`（IEEE 754 规定相反符号相加为零时结果取正零）。

### 4.4 舍入与特殊值

#### 4.4.1 概念说明

舍入（rounding）和特殊值（inf / nan）处理是浮点硬件最 tricky 的两件事。Nyuzi 采用 IEEE 754 的默认舍入模式——**舍入到最近偶数（round-to-nearest, ties-to-even）**：当待舍入的值正好处于两个可表示数正中间时，选择尾数为偶数（最低位为 0）的那个。这样做能让误差长期统计均值为零。

特殊值有三类：

- **inf**（无穷）：指数 = 0xFF、尾数 = 0，分 +inf / −inf。
- **nan**（非数）：指数 = 0xFF、尾数 ≠ 0。Nyuzi 把所有 nan 规范化成 `0x7fffffff`，**不保留** payload。
- **subnormal**（非规格化）：指数 = 0。

这些特殊值的判定在第 1 级就完成，结果（`result_inf`/`result_nan`）一路传递到第 5 级，在那里优先于普通运算结果输出。

#### 4.4.2 核心流程

```
stage1:  判定 inf/nan/equal（基于 exponent == 0xFF 等），结果随流水线下传
stage3:  加法路径的「舍入预测」：
           round_tie = guard && !round && !sticky      （正中间）
           round_up  = guard && (round || sticky)      （超过中点）
           do_round  = round_up || (sum_odd && round_tie)   ← ties-to-even
stage5:  组装最终结果，优先级：
           result_nan → 0x7fffffff
           result_inf / overflow → {sign, 0xFF, 0}
           相加为零 → +0.0
           正常 → {sign, exponent, significand}
```

#### 4.4.3 源码精读

**第 1 级：识别特殊值。** 用指数与尾数的组合判定 inf/nan：

```systemverilog
assign fop1_inf = fop1.exponent == 8'hff && fop1.significand == 0;
assign fop1_nan = fop1.exponent == 8'hff && fop1.significand != 0;
```

见 [fp_execute_stage1.sv:132-135](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage1.sv#L132-L135)。随后按操作类型汇总 `result_nan`：乘法时 `inf × 0` 是 nan，比较时只要任一操作数是 nan 结果就为「无序」（false），见 [fp_execute_stage1.sv:168-181](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage1.sv#L168-L181)。

**第 3 级：加法的 round-to-even 预测。** 这是整条流水线最精巧的一段。关键是**在相加之前**就预测舍入方向，从而把舍入折叠进加法的进位输入：

```systemverilog
assign sum_odd = fx2_significand_le[lane_idx][0] ^ fx2_significand_se[lane_idx][0];
assign round_tie = (fx2_guard[lane_idx] && !(fx2_round[lane_idx] || fx2_sticky[lane_idx]));
assign round_up = (fx2_guard[lane_idx] && (fx2_round[lane_idx] || fx2_sticky[lane_idx]));
assign do_round = (round_up || (sum_odd && round_tie));
assign carry_in = fx2_logical_subtract[lane_idx] ^ (do_round && !ftoi);
assign {unnormalized_sum, _unused} = {fx2_significand_le[lane_idx], 1'b1}
    + {(fx2_significand_se[lane_idx] ^ {32{fx2_logical_subtract[lane_idx]}}), carry_in};
```

见 [fp_execute_stage3.sv:105-121](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage3.sv#L105-L121)。读法：

- `sum_odd` 用两尾数最低位异或预测**和**的最低位是否为奇数（决定 ties 时往哪边靠）。
- `round_tie` 表示「恰好在正中间」，`round_up` 表示「超过中点」。
- `do_round` 综合：超过中点直接进，正好中间则只在和为奇数时进（让它变成偶数）——这正是 ties-to-even。
- 减法用「按位取反 + 进位」实现（`^ {32{logical_subtract}}` 把减数取反，配合 `carry_in` 补 1），把加减与舍入合并成一次加法。

**第 5 级：乘法路径的舍入。** 乘法也有自己的 guard/round/sticky（从规格化后的乘积低位抽出来），逻辑与加法同构：

```systemverilog
assign mul_sticky = |mul_sticky_bits;
assign mul_round_tie = mul_guard && !(mul_round || mul_sticky);
assign mul_round_up = mul_guard && (mul_round || mul_sticky);
assign mul_do_round = mul_round_up || (mul_round_tie && mul_normalized_significand[0]);
assign mul_rounded_significand = mul_normalized_significand + FLOAT32_SIG_WIDTH'(mul_do_round);
```

见 [fp_execute_stage5.sv:158-162](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage5.sv#L158-L162)。

**第 5 级：组装最终结果。** 用一段 `always_comb` 按优先级处理特殊值，普通结果排最后：

```systemverilog
if (fx4_result_inf[lane_idx] || add_overflow)
    add_result = {fx4_add_result_sign[lane_idx], 8'hff, 23'd0};      // inf
else if (fx4_result_nan[lane_idx])
    add_result = {32'h7fffffff};                                      // nan (规范化)
else if (add_result_significand == 0 && add_subnormal)
    add_result = 0;                                                   // +0.0
else
    add_result = {fx4_add_result_sign[lane_idx], add_result_exponent, add_result_significand};
```

见 [fp_execute_stage5.sv:117-133](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage5.sv#L117-L133)。

最后，所有操作的结果在第 5 级用一个寄存器写回阶段二选一/多选一送出——`ftoi` 输出移位后的整数、比较输出 0/1、整数乘取高低位、浮点乘输出 `fmul_result`、其余输出 `add_result`：

```systemverilog
if (ftoi)
    fx5_result[lane_idx] <= ... shifted_significand;        // 或 nan 时 0x80000000
else if (fx4_instruction.compare)
    fx5_result[lane_idx] <= scalar_t'(compare_result);
else if (imull)
    fx5_result[lane_idx] <= fx4_significand_product[lane_idx][31:0];
else if (imulh)
    fx5_result[lane_idx] <= fx4_significand_product[lane_idx][63:32];
else if (fmul)
    fx5_result[lane_idx] <= fx4_mul_underflow[lane_idx] ? 32'h00000000 : fmul_result;
else
    fx5_result[lane_idx] <= add_result;
```

见 [fp_execute_stage5.sv:185-204](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage5.sv#L185-L204)。注意浮点比较结果在哪一级最终落地——它复用了加法通路算出的 `add_result_sign`（符号位代表「谁大」）与第 1 级就算好的 `equal`，见 [fp_execute_stage5.sv:138-149](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage5.sv#L138-L149)。

#### 4.4.4 为什么这个实现「非完全 IEEE 754 兼容」

把上面散见的 `XXX` 注释和测试 README 串起来，可以得到一份明确的「不兼容清单」：

1. **下溢处理不完整**：[fp_execute_stage1.sv:189](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage1.sv#L189) 注释 `// XXX handle underflow`。
2. **非规格化乘积不处理**：[fp_execute_stage5.sv:153](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage5.sv#L153) 注释 `// XXX does not handle subnormal product`。
3. **−0.0 + 0.0 的符号**：[fp_execute_stage5.sv:128](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage5.sv#L128) 注释承认会「误伤」一些情形。
4. **乘法器非 Wallace 树**：[fp_execute_stage2.sv:138](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage2.sv#L138) 注释，依赖综合工具的 `*`。
5. **nan 不保留 payload**：统一规范化为 `0x7fffffff`。
6. **官方测试说明**：[tests/float/README.md:6-7](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/float/README.md#L6-L7) 明确写道——「There are currently many failures when executing against Verilator, mostly caused by **rounding errors** in hardware. All of the emulator tests should pass.」这是最直接的证据：功能参考（模拟器）能过，但 RTL 在舍入边界上与参考不一致。

> 这并非「bug」，而是 Nyuzi 作为**实验性**处理器有意识的取舍：用更简单的逻辑换更短的代码与更高的频率，代价是放弃 IEEE 754 的少数边界精度。这也是为什么浮点指令的副作用在协同仿真里被刻意排除在严格比对范围之外（见 [u8-l3](u8-l3-cosimulation.md)）。

### 4.5 倒数估计 ROM

#### 4.5.1 概念说明

Nyuzi **没有硬件除法指令**。要算 `a / b`，软件需要：先用 `OP_RECIPROCAL` 指令拿到 `1/b` 的近似值，再用乘法和若干次牛顿-拉夫森迭代精化。`OP_RECIPROCAL` 是**单周期**指令，由一个 64 项的查找表 `reciprocal_rom` 实现——这就是它不在 5 级浮点流水线里、而在单周期整数执行单元 `int_execute_stage` 里的原因。

#### 4.5.2 核心流程

```
输入：一个 binary32 浮点数 x
  ↓ 取尾数高 6 位作为 ROM 地址
ROM：  输出 6 位倒数估计（≈ 1/x 的尾数高 6 位）
  ↓ 整数执行单元拼装：
       倒数指数 = 253 − x.exponent (+ 微调)
       倒数符号 = x.sign
  ↓ 输出：binary32 近似 1/x（约 6~7 位有效精度）
软件：  用乘法 + 牛顿迭代精化到全精度
```

#### 4.5.3 源码精读

**ROM 本体**：一个 6 位输入、6 位输出的 `case` 表，共 64 项，由 `tools/misc/make_reciprocal_rom.py` 自动生成：

```systemverilog
module reciprocal_rom(
    input [5:0] significand,
    output logic[5:0] reciprocal_estimate);

    always_comb
    begin
        case (significand)
            6'h0: reciprocal_estimate = 6'h0;
            6'h1: reciprocal_estimate = 6'h3e;
            ...
            6'h3f: reciprocal_estimate = 6'h0;
            default: reciprocal_estimate = 6'h0;
        endcase
    end
endmodule
```

见 [reciprocal_rom.sv:21-95](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/reciprocal_rom.sv#L21-L95)。地址 `0x1` 对应估计 `0x3e`（= 62），地址 `0x3f` 对应 `0x0`——值随地址单调递减，符合「输入越大倒数越小」。

**调用处**：在 `int_execute_stage` 里实例化 ROM，地址取尾数最高 6 位：

```systemverilog
assign fp_operand = lane_operand2;
reciprocal_rom rom(
    .significand(fp_operand.significand[22:17]),
    .reciprocal_estimate);
```

见 [int_execute_stage.sv:190-194](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L190-L194)。然后用一段组合逻辑处理特殊输入并拼装最终 binary32：

```systemverilog
if (fp_operand.exponent == 0)
    reciprocal = {fp_operand.sign, 8'hff, 23'd0};    // 次正规或 0 → inf（也处理除零）
else if (fp_operand.exponent == 8'hff)
    reciprocal = (fp_operand.significand != 0)
        ? {1'b0, 8'hff, 23'h7fffff}                   // 1/nan = nan
        : {fp_operand.sign, 8'h00, 23'h000000};       // 1/±inf = ±0
else
    reciprocal = {fp_operand.sign, 8'd253 - fp_operand.exponent
        + 8'((fp_operand.significand[22:17] == 0)),
        reciprocal_estimate, {17{1'b0}}};
```

见 [int_execute_stage.sv:196-216](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/int_execute_stage.sv#L196-L216)。倒数指数 = `253 − exp`（因为 `1/2^e` 的偏置指数是 `127 − (exp − 127) = 254 − exp`，再因尾数估计已归一化到 `[0.5, 1)` 微调一位得 253）；尾数估计放在最高 6 位、低 17 位补 0。

#### 4.5.4 代码实践：软件除法的端到端验证

**实践目标**：确认 `OP_RECIPROCAL` 只是「种子」，真正的除法精度由软件迭代补足。

**操作步骤**：

1. 阅读 [tests/whole-program/fdiv.cpp](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/whole-program/fdiv.cpp)，它用 `1.0f / a`、`1235.0f / b`、`c / 0.4f` 三条除法，每条都用 `// CHECK:` 注释给出期望的 binary32 十六进制结果。
2. 注意三处期望值：
   - `1.0f / 123.0f` → `0x3c053408`
   - `1235.0f / 11.1f` → `0x42de85c5`
   - `1.0f / 0.4f` → `0x40200000`
3. 构建并运行（**待本地验证**具体命令，参考 [u1-l4](u1-l4-first-program.md) 的 `run_emulator` 流程）：
   ```
   cd tests/whole-program
   ./run_emulator fdiv
   ```
4. 观察输出的三行十六进制是否与 `CHECK` 一致。

**需要观察的现象**：虽然 `OP_RECIPROCAL` 只有约 6 位精度，但最终除法结果达到了 binary32 的全精度（与宿主机算出的结果一致），说明软件库在 `OP_RECIPROCAL` 之后做了精化迭代。

**预期结果**：三行输出依次为 `0x3c053408`、`0x42de85c5`、`0x40200000`，全部通过 `CHECK` 校验。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `OP_RECIPROCAL` 放在单周期的整数执行单元，而不是 5 级浮点流水线？

**参考答案**：因为它只是一个 64 项查找表 + 简单指数拼装，组合逻辑很轻，单周期就能完成，没必要付 5 拍延迟的代价。把它放在整数 ALU 里还能让除法的「种子」尽快产出，缩短软件迭代的等待。

**练习 2**：ROM 地址用尾数的高 6 位（`significand[22:17]`）而不是全部 23 位，这会带来什么后果？

**参考答案**：这意味着很多不同的输入会查到同一个估计值，倒数估计只有约 6~7 位有效精度。精确的全精度倒数必须由软件用牛顿-拉夫森等迭代算法补足——这就是 Nyuzi「用软件换硬件」除法策略的核心。

## 5. 综合实践

把本讲全部内容串起来，做一个「沿五级流水线追踪浮点乘加」的完整源码阅读任务。

**任务**：计算 `z = (1.5f × 2.5f) + 0.25f`，即先做浮点乘、再做浮点加，跟踪两步运算各自经过的流水级与中间量。

**步骤**：

1. **乘法阶段**：参考 4.2.4 节已算好的 `1.5f × 2.5f = 3.75 (0x40700000)`。确认这个结果作为后续加法的第一个操作数。
2. **加法阶段**：把 `3.75 (0x40700000)` 与 `0.25` 相加。`0.25 = 0x3E800000`（指数 0x7D = 125，full_significand = 0x800000）。
   - 在 stage1（[fp_execute_stage1.sv:195-258](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage1.sv#L195-L258)）回答：哪个数进 `_le`？`_se_align_shift` 等于多少？（指数差 = 128 − 125 = 3。）
   - 在 stage2（[fp_execute_stage2.sv:110-112](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage2.sv#L110-L112)）回答：对阶后 `0.25` 的尾数右移 3 位，guard/round/sticky 各是多少？
   - 在 stage3（[fp_execute_stage3.sv:105-121](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage3.sv#L105-L121)）回答：`do_round` 是否成立？为什么？
   - 在 stage4（[fp_execute_stage4.sv:91-134](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage4.sv#L91-L134)）回答：前导零个数是多少？需要规格化移位吗？
   - 在 stage5（[fp_execute_stage5.sv:103-133](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/fp_execute_stage5.sv#L103-L133)）回答：最终 `add_result` 是多少？应为 `0x40800000`（= 4.0）。
3. **反思非 IEEE 兼容**：对照 4.4.4 节的清单，指出在这个加法里哪几处取舍「没踩到」（本例不涉及下溢、subnormal、nan），从而体会这些边界情况为何是 IEEE 兼容性的难点。
4.（可选）用 `run_emulator` 跑一段 `printf` 打印 `(1.5f * 2.5f) + 0.25f` 的程序，验证结果为 4.000000（**待本地验证**）。

**预期结果**：手工追踪得到的加法结果 `0x40800000 = 4.0`，与 `(1.5 × 2.5) + 0.25 = 4.0` 一致；你能说清每级传递的关键中间量与 guard/round/sticky 的取值。

## 6. 本讲小结

- Nyuzi 的浮点执行是一条 **5 级流水线**（`fp_execute_stage1..5`），同时承载浮点加减乘、整数乘法、浮点↔整数转换和浮点比较，靠 `pipeline_sel == PIPE_FLOAT_ARITH` 选路。
- 流水线内部有**加法**与**乘法**两条并行子通路，各自维护中间结果，到第 5 级按 `alu_op` 选出最终写回值。
- **尾数乘积**在第 2 级用一个 64 位 `*` 完成（非 Wallace 树）；整数乘法复用同一颗乘法器，第 5 级再取高低 32 位。
- **对阶规整**跨第 1（比大小/算移位）、2（右移+guard/round/sticky）、4（前导零计数）、5（规格化移位）级完成。
- 舍入采用 **round-to-nearest ties-to-even**，第 3 级在相加前预测舍入方向并折叠进进位输入。
- 特殊值 inf/nan 在第 1 级识别、第 5 级优先输出；nan 统一规范化为 `0x7fffffff`。
- 由于下溢、subnormal、舍入边界等不完整处理，该实现**非完全 IEEE 754 兼容**，官方测试 README 明确 Verilator 上存在舍入误差。
- **倒数估计** `reciprocal_rom` 是单周期 64 项查找表，位于整数执行单元，仅提供约 6 位精度，全精度除法由软件牛顿迭代完成——Nyuzi 没有硬件除法。

## 7. 下一步学习建议

- 想看浮点结果如何与整数、访存路径在写回级汇合、如何参与精确异常与回滚，请继续阅读 [u7-l3 Trap 处理与回滚](u7-l3-trap-rollback.md)。
- 想理解这条 5 级长路径如何与其他长度的执行路径在写回级不撞车，回到 [u4-l3 线程选择与记分牌](u4-l3-thread-select.md) 复习「写回结构冒险」一节。
- 想从功能参考实现的角度对照硬件浮点行为，阅读 [u8-l1 模拟器架构与指令执行](u8-l1-emulator-architecture.md) 中 `processor.c` 的浮点指令处理，并了解 [u8-l3 协同仿真](u8-l3-cosimulation.md) 为何把浮点副作用排除在严格比对之外。
- 若对图形渲染如何压榨这条 16 通道浮点流水线感兴趣，可预习 [u9-l3 librender 渲染库基础](u9-l3-librender-basics.md) 与 [u13 光栅化与着色](u13-l2-rasterization.md)。
