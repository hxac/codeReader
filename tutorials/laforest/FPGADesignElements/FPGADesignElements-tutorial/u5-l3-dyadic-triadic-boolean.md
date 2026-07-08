# Dyadic/Triadic 参数化布尔运算器

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚「用真值表当参数、用一个模块实现一族布尔函数」的设计思想。
- 看懂 `Dyadic_Boolean_Operator` 如何把一个 4:1 多路选择器改造成「两变量布尔函数发生器」。
- 理解 `Dyadic_Boolean_Operations.vh` 运算表的组织方式，并能用它实例化出 AND/OR/XOR 等运算。
- 解释 `Triadic_Boolean_Operator` 为什么不直接用 8:1 多路选择器，而是用 **Shannon 分解** 拆成两个 dyadic 半部。
- 说清「双输出（dual output）」的设计动机：把本会被丢弃的一半计算量重新利用起来。

## 2. 前置知识

本讲是「组合逻辑基础构件」单元的第三篇，承接 [u5-l2](./u5-l2-mux-demux-address.md) 讲过的多路选择器与地址译码。开始之前，请确认你已经了解：

- **真值表（truth table）**：把一个布尔函数所有输入组合对应的输出列成一张表。两变量有 4 种输入组合，所以真值表是 4 位；三变量有 8 种组合，真值表是 8 位。
- **多路选择器（mux）**：N 选 1 的「数据开关」，用 `selector` 当地址去选 `words_in` 中的一项。本讲的 dyadic 运算器复用的就是 [u5-l2](./u5-l2-mux-demux-address.md) 的 `Multiplexer_Binary_Behavioural`。
- **LUT 估算**（见 [u3-l1](./u3-l1-assignments-and-ternary.md)）：N:1 mux 的输入项数 = N + log₂N，4:1 mux 恰好 6 项、装进一个 6 输入 LUT；8:1 mux 要 11 项、装不下一个 6-LUT。这个数字在本讲解释「为什么三变量运算要分解」时会用到。
- **`generate for` 与位拼接**：见 [u2-l2](./u2-l2-parameterization-and-widths.md)。本讲会用 `generate for` 逐位实例化、用 `{word_A[i], word_B[i]}` 把两位拼成地址。

一个关键直觉：两个布尔变量 \(A, B\) 总共能组合出多少种不同的函数？每一位输入组合（共 \(2^2=4\) 种）的输出都可独立取 0 或 1，所以函数总数为

\[
2^{2^2} = 2^4 = 16
\]

同理，三变量共有 \(2^{2^3} = 2^8 = 256\) 种函数。本讲要解决的问题是：**能不能用同一块硬件、靠一个参数把这 16 种（或 256 种）函数全部表达出来，而不是为每种函数写一个模块？**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Dyadic_Boolean_Operations.vh](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Dyadic_Boolean_Operations.vh) | 运算表：用 `define` 把 16 种两变量布尔函数各编码成一个 4 位真值表常量，外加表宽/地址宽宏。 |
| [Dyadic_Boolean_Operator.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Dyadic_Boolean_Operator.v) | Dyadic 运算器：用 `generate for` 逐位放一个 4:1 mux，地址是 `{A[i],B[i]}`，数据是 4 位真值表。 |
| [Triadic_Boolean_Operator.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Triadic_Boolean_Operator.v) | Triadic 运算器：实例化两个 Dyadic 运算器做 \(g(A,B)\)、\(h(A,B)\)，再用按位 2:1 mux 由 \(C\) 在两者间选择，并可选第二输出。 |
| [Multiplexer_Binary_Behavioural.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Multiplexer_Binary_Behavioural.v) | 被 Dyadic 运算器逐位复用的通用二进制 mux（变址部分位选实现）。 |
| [Multiplexer_Bitwise_2to1.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Multiplexer_Bitwise_2to1.v) | 被 Triadic 运算器复用的「按位 2:1 mux」，其注释指出它实现的就是 Shannon 分解。 |

## 4. 核心概念与源码讲解

### 4.1 运算表：用 4 位真值表描述 16 种二元布尔函数

#### 4.1.1 概念说明

「运算表」是本讲的基石。我们不写 16 个分别叫 AND、OR、XOR…… 的模块，而是约定一种**编码**：把任意一个两变量布尔函数压缩成一个 4 位常量，这个常量就是该函数的真值表。

约定是这样的：把两个输入位拼成一个 2 位数，**A 是最高位（MSB）**，即索引为 `{A, B}`：

| {A,B} | A | B | 索引值 | 选中真值表的哪一位 |
| --- | --- | --- | --- | --- |
| 00 | 0 | 0 | 0 | `truth_table[0]` |
| 01 | 0 | 1 | 1 | `truth_table[1]` |
| 10 | 1 | 0 | 2 | `truth_table[2]` |
| 11 | 1 | 1 | 3 | `truth_table[3]` |

以 **AND** 为例：只有 A=1 且 B=1 时输出才为 1，也就是只有索引 3 那一位是 1，其余为 0。写成 4 位常量（第 3 位为 MSB）就是 `4'b1000`。再以 **XOR** 为例：A≠B 时输出 1，即索引 1、2 为 1，写成 `4'b0110`。

于是「描述一个函数」=「填一张 4 位真值表」。这就是运算表的全部思想。

#### 4.1.2 核心流程

运算表文件本身不含任何逻辑，只是用编译器宏把 16 张真值表命名好：

1. 用 `` `ifndef ... `define ... `endif `` 包一层，做成**幂等包含**，防止同一宏在多文件工程里被重复定义。
2. 先定义两个**永不改变的尺寸常量**：真值表宽 4 位、选择地址宽 2 位。
3. 再把 16 种函数各定义成一个 4 位 `define`，命名直观（如 `DYADIC_A_AND_B`、`DYADIC_A_XOR_B`），供使用方直接拿去当 `truth_table` 参数。
4. 使用方在模块开头 `` `include `` 这个文件即可。

#### 4.1.3 源码精读

先看幂等包含与两个尺寸宏（真值表宽、地址宽，这两个值是由「两变量」这件事本身决定的，永远不变）：

[Dyadic_Boolean_Operations.vh:L10-L16](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Dyadic_Boolean_Operations.vh#L10-L16) — 用 `ifndef` 包成幂等包含，并定义 4 位真值表宽与 2 位地址宽。

接着是 16 张真值表本身。注释明确「A 是真值表索引的最高位」，与上面 AND=`4'b1000`、XOR=`4'b0110` 的推导一致：

[Dyadic_Boolean_Operations.vh:L21-L36](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Dyadic_Boolean_Operations.vh#L21-L36) — 16 种两变量布尔函数各自的 4 位真值表编码，从 `DYADIC_ZERO`(0000) 到 `DYADIC_ONE`(1111)。

值得专门点出几个有教学意义的项：

- `DYADIC_A_AND_B 4'b1000` 与 `DYADIC_A_NAND_B 4'b0111`：互为按位取反，体现「NAND = NOT AND」。
- `DYADIC_A 4'b1100`、`DYADIC_B 4'b1010`、`DYADIC_NOT_A 4'b0011`、`DYADIC_NOT_B 4'b0101`：函数其实只依赖一个输入，真值表呈规律镜像。
- `DYADIC_ZERO 4'b0000` 与 `DYADIC_ONE 4'b1111`：输出与输入无关，是两个「常量函数」。

#### 4.1.4 代码实践

**实践目标**：亲手把两个常见函数翻译成 4 位真值表，验证你对编码规则的理解。

**操作步骤**：

1. 打开运算表文件，找到 `DYADIC_A_OR_B`，确认它等于 `4'b1110`。
2. 自己推导 **XNOR**（A=B 时输出 1）的真值表，再对照文件里的 `DYADIC_A_XNOR_B`。
3. 推导 **A AND NOT B** 的真值表，对照 `DYADIC_A_AND_NOT_B`。

**需要观察的现象**：A=1,B=0（索引 2）这种「只有一种输入命中」的函数，其真值表里只有 1 位是 1。

**预期结果**：

- XNOR：索引 0（00）和 3（11）为 1 → `4'b1001`，与 `DYADIC_A_XNOR_B` 一致。
- A AND NOT B：仅索引 2（A=1,B=0）为 1 → `4'b0100`，与 `DYADIC_A_AND_NOT_B` 一致。

> 待本地验证：如果你手边有 iverilog，可在下面 4.2.4 的测试台里把这些常量打印出来核对。

#### 4.1.5 小练习与答案

**练习 1**：`DYADIC_A_OR_B` 为什么是 `4'b1110` 而不是 `4'b0111`？

> **答案**：OR 仅在 A=0,B=0（索引 0）时输出 0，其余三种组合都输出 1，所以真值表只有最低位（索引 0）是 0，即 `4'b1110`。

**练习 2**：三变量共有多少种布尔函数？四变量呢？

> **答案**：函数总数为 \(2^{2^n}\)。三变量 \(2^{2^3}=2^8=256\) 种；四变量 \(2^{2^4}=2^{16}=65\,536\) 种。这正是源码注释里「256 possible triadic」「65,536 possible tetradic functions」的来源，也是真值表位数随变量数**指数增长**、函数总数**双指数增长**的根因。

**练习 3**：为什么运算表文件要用 `` `ifndef `` 包起来？

> **答案**：为了让多个 `.v` 文件都 `` `include `` 它时不重复定义同一个宏（重复定义会触发 CAD 工具的 redefinition 警告，见文件第 7-9 行注释）。这与 [u2-l2](./u2-l2-parameterization-and-widths.md) 里 `clog2_function.vh` 的幂等包含是同一个套路。

---

### 4.2 Dyadic 运算器：把多路选择器当成布尔函数发生器

#### 4.2.1 概念说明

有了运算表，下一步是造一台「吃了真值表就变成对应函数」的硬件。Dyadic 运算器的巧妙之处在于：**它本质上就是一个 4:1 多路选择器，只是用法反过来**。

通常我们用 mux「在多个数据里选一个」；这里我们把 **真值表当成数据、把 {A,B} 当成地址**：每一位输入组合 `{A[i],B[i]}` 恰好是 0~3 中的一个地址，去真值表里选中对应那一位作为输出。由于真值表的第 k 位描述的就是「输入组合为 k 时的输出」，于是 mux 输出恰好就是该布尔函数在该输入下的取值——整个 4:1 mux 等价于一个「可由真值表任意编程」的两变量逻辑门。

而且这个「编程」是**运行时可变**的：把 `truth_table` 接成常量，它就是一个固定的 AND/XOR；把 `truth_table` 接成 ALU 的控制信号，它就能在运行中切换函数。源码注释指出它适合做 ALU 数据通路与 CPU 分支/工业控制的条件判断逻辑。

#### 4.2.2 核心流程

1. 模块开头 `` `include `` 运算表文件，拿到 `DYADIC_TRUTH_TABLE_WIDTH`、`DYADIC_SELECTOR_WIDTH` 等宏与 16 个函数常量。
2. 端口：`truth_table`(4 位)、`word_A`、`word_B`、`result`，三者字宽统一为参数 `WORD_WIDTH`。
3. 用 `generate for` 对字的每一位 i 各实例化一个 `Multiplexer_Binary_Behavioural`：
   - `selector = {word_A[i], word_B[i]}`（2 位地址，A 在高位）。
   - `words_in = truth_table`（4 个 1 位「输入字」就是真值表的 4 位）。
   - `word_out = result[i]`。
4. 每一位独立计算，于是整个字同步完成「同一种、但逐位的」布尔运算。

> 注意一个细节：mux 的 `selector*WORD_WIDTH +: WORD_WIDTH` 在 `WORD_WIDTH=1` 时退化为 `truth_table[selector]`，所以 `{A[i],B[i]}` 这个 2 位数正好索引到真值表的第 `{A,B}` 位——与 4.1 的编码约定严丝合缝。

#### 4.2.3 源码精读

先看文件头：包含运算表、关闭隐式线网，都是 [u2-l1](./u2-l1-verilog2001-and-default-nettype.md) 立的规矩：

[Dyadic_Boolean_Operator.v:L23-L36](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Dyadic_Boolean_Operator.v#L23-L36) — `include` 运算表、`default_nettype none`、端口声明；注意 `truth_table` 宽度直接用运算表里的 `DYADIC_TRUTH_TABLE_WIDTH` 宏，而字宽用模块自己的 `WORD_WIDTH` 参数。

核心是这段逐位 mux 的 `generate`：

[Dyadic_Boolean_Operator.v:L38-L54](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Dyadic_Boolean_Operator.v#L38-L54) — 对每一位实例化一个 4:1 mux：`selector` 是 `{word_A[i],word_B[i]}`，`words_in` 是 4 位真值表，`word_out` 是 `result[i]`。一位一个 mux，就是把布尔函数发生器复制了 `WORD_WIDTH` 份。

被复用的 mux 本体只有一行实质逻辑——变址部分位选（这行也解释了 X 值为何能正确传播，详见 [u5-l2](./u5-l2-mux-demux-address.md)）：

[Multiplexer_Binary_Behavioural.v:L92-L94](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Multiplexer_Binary_Behavioural.v#L92-L94) — `word_out = words_in[(selector * WORD_WIDTH) +: WORD_WIDTH]`，用 `selector` 当地址切出对应字；在 Dyadic 里 `WORD_WIDTH=1`，故就是 `truth_table[selector]`。

LUT 用量估算（承接 [u3-l1](./u3-l1-assignments-and-ternary.md)）：每个 4:1 mux 的输入项 = 4（数据）+ 2（地址）= 6，恰好填满一个 6 输入 LUT。所以一个 N 位 dyadic 运算器约耗 N 个 LUT——这正是它「贴近底层硬件」的体现，也是下一节 triadic 不照搬此法的原因。

#### 4.2.4 代码实践

**实践目标**：实例化一个 Dyadic 运算器实现按位 XOR，并用一个最小测试台自检。

**操作步骤**：

1. 新建一个仿真顶层（示例代码，非项目原有文件），把 `truth_table` 绑成 `DYADIC_A_XOR_B`，输入几组 A/B，观察 `result` 是否等于 A^B。
2. 用 iverilog 编译运行（命令仅供参考，**待本地验证**）。

下面是**示例代码**：

```verilog
`include "Dyadic_Boolean_Operations.vh"   // 拿到 DYADIC_A_XOR_B 等常量

`default_nettype none

module xor_demo;   // 示例代码，非项目原有文件
    localparam WORD_WIDTH = 4;

    reg  [WORD_WIDTH-1:0] a, b;
    wire [WORD_WIDTH-1:0] y;

    Dyadic_Boolean_Operator
    #(
        .WORD_WIDTH(WORD_WIDTH)
    )
    uut
    (
        .truth_table(`DYADIC_A_XOR_B),   // 一个常量把整块硬件“编程”成 XOR
        .word_A      (a),
        .word_B      (b),
        .result      (y)
    );

    initial begin
        a = 4'b1010; b = 4'b0110;  #1;  // 期望 y = 4'b1100
        a = 4'b1111; b = 4'b0000;  #1;  // 期望 y = 4'b1111
        a = 4'b1010; b = 4'b1010;  #1;  // 期望 y = 4'b0000
        $finish;
    end
endmodule
```

```bash
# 参考命令，待本地验证（需 iverilog）
iverilog -g2001 -o sim xor_demo.v Dyadic_Boolean_Operator.v Multiplexer_Binary_Behavioural.v
vvp sim
```

**需要观察的现象**：仅靠改 `truth_table` 这一个绑定，同一块硬件就能变成 XOR；把它换成 `` `DYADIC_A_AND_B ``，输出立刻变成按位 AND，**无需改动任何别的连线**。

**预期结果**：三组输入的 `y` 分别为 `1100`、`1111`、`0000`，与 A^B 完全一致。

#### 4.2.5 小练习与答案

**练习 1**：把上面例子的 `truth_table` 改成 `` `DYADIC_A_AND_B ``，输入 `a=4'b1010; b=4'b0110;` 时 `y` 应该是多少？

> **答案**：按位 AND → `1010 & 0110 = 0010`，即 `y = 4'b0010`。

**练习 2**：为什么 Dyadic 运算器**逐位**放 mux，而不是用一个宽 mux 一次处理整个字？

> **答案**：因为布尔运算是**按位独立**的——每一位的输出只取决于同位的 A、B 与同一张真值表，位与位之间没有进位/依赖。逐位 mux 让每位恰好填进一个 6-LUT，结构规整、易于流水线化；若用一个超宽 mux 反而无法对齐到 FPGA 的 LUT 结构。

**练习 3**：若实例化时忘了设 `WORD_WIDTH`，会发生什么？（提示：回顾 [u1-l2](./u1-l2-repo-layout-and-conventions.md)）

> **答案**：`WORD_WIDTH` 默认为 0，端口宽度退化为非法的 `[-1:0]`，在 elaboration 阶段吵闹地失败。这是本书「参数默认值为 0」安全栅栏的体现，防止静默用错位宽。

---

### 4.3 Triadic 运算器：Shannon 分解与双输出

#### 4.3.1 概念说明

三变量布尔函数有 256 种，照搬 dyadic 的思路似乎只要把 4:1 mux 换成 8:1 mux、把 4 位真值表换成 8 位即可。但源码注释明确拒绝了这条路，原因正是 [u3-l1](./u3-l1-assignments-and-ternary.md) 的 LUT 估算：8:1 mux 的输入项 = 8（数据）+ 3（地址）= 11，**装不进一个 6 输入 LUT**，于是设计会脱离 FPGA 底层结构——难以按需流水线化、更受 CAD 工具重定时与逻辑打包的摆布。

解决办法是一条经典数学恒等式——**Shannon 分解（也叫 Boole 展开定理）**：任取一个变量（这里取 C）当「分解轴」，把三变量函数 \(f(A,B,C)\) 拆成两个两变量子函数

\[
g(A,B) = f(A,B,0), \qquad h(A,B) = f(A,B,1)
\]

再用 C 把它们重新拼起来：

\[
f(A,B,C) = \bigl(g(A,B)\ \&\ \sim C\bigr)\ \big|\ \bigl(h(A,B)\ \&\ C\bigr)
\]

也就是「C=0 时取 g，C=1 时取 h」。把它降了一阶：三变量变成「两个两变量函数 + 一个 2:1 选择」，而每个两变量函数都能像 dyadic 那样恰好填进一个 LUT。

注意一个极易混淆的点：这里的 C 是**按位**选择，不是整字选择。每一位的 `C[i]` 决定该位取 `g[i]` 还是 `h[i]`。源码用 `Multiplexer_Bitwise_2to1` 来做这件事——而这个模块的注释一语道破：它实现的就是 Shannon 分解（见下方源码引用）。

#### 4.3.2 核心流程

1. 实例化**第一个** Dyadic 运算器，真值表接 `dyadic_truth_table_1`，算出 \(g(A,B)\)。
2. 实例化**第二个** Dyadic 运算器，真值表接 `dyadic_truth_table_2`，算出 \(h(A,B)\)。
3. 用 `Multiplexer_Bitwise_2to1`（`select_1`）按 `word_C` 在 g、h 间按位选择，得 `result_1`。
4. 计算 `word_D = dual ? ~word_C : word_C`。
5. 用第二个 `Multiplexer_Bitwise_2to1`（`select_2`）按 `word_D` 再选一次，得 `result_2`。

第 4、5 步就是「双输出」的来源，下一小节专门讲它的动机。

#### 4.3.3 源码精读

两个 dyadic 半部，分别对应 \(g(A,B)=f(A,B,0)\) 与 \(h(A,B)=f(A,B,1)\)：

[Triadic_Boolean_Operator.v:L97-L127](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Triadic_Boolean_Operator.v#L97-L127) — 实例化两个 `Dyadic_Boolean_Operator`，分别由 `dyadic_truth_table_1`、`dyadic_truth_table_2` 编程，输出命名 `g` 与 `h`。这是「用构建块拼装更大构件」的典型范例（参见 [u4-l1](./u4-l1-modularization-and-building-blocks.md)）。

第一个按位选择，把 C 当 selector 在 g、h 间挑：

[Triadic_Boolean_Operator.v:L132-L142](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Triadic_Boolean_Operator.v#L132-L142) — `select_1` 用 `word_C` 当 bitmask，0 选 g、1 选 h，得 `result_1`。

`Multiplexer_Bitwise_2to1` 本身的注释把这件事的性质点透了：

[Multiplexer_Bitwise_2to1.v:L8-L11](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Multiplexer_Bitwise_2to1.v#L8-L11) — 指出这个看似平凡的按位 2:1 mux 实现的正是 Shannon 分解 / Boole 展开定理，可把 N 变量函数组合成 N+1 变量函数。

接着是双输出的关键——条件取反 C 得到 D：

[Triadic_Boolean_Operator.v:L144-L150](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Triadic_Boolean_Operator.v#L144-L150) — `word_D = (dual == 1'b1) ? ~word_C : word_C;`。`dual` 为 1 时 D 是 C 的逐位取反，为 0 时 D 等于 C。注意这里用三元、组合块阻塞赋值，符合 [u3-l1](./u3-l1-assignments-and-ternary.md) 的规范。

第二个选择器用 D 当 selector：

[Triadic_Boolean_Operator.v:L156-L166](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Triadic_Boolean_Operator.v#L156-L166) — `select_2` 用 `word_D` 当 bitmask再做一次 g/h 选择，得 `result_2`。

#### 4.3.4 为何要双输出（本节重点，对应实践题第二问）

源码注释给了关键洞察：按 \(f=(g\&\sim C)|(h\&C)\) 计算 `result_1` 时，**永远有一半的工作量被丢掉**——当某位 C=0 时，h 那一位的计算被弃用；C=1 时，g 那一位被弃用。「被丢弃的计算」是一个信号：说明我们漏掉了硬件能提供的算力。

双输出正是为了把这些被丢掉的结果也接出来。当 `dual=1` 时，`word_D=~word_C`，于是 `result_2` 在每一位上选的恰好是 `result_1` **没选**的那个函数——两位输出合起来，把 g 和 h 的每一位都用上了，没有浪费。

这带来两类实用用法（均出自源码注释）：

1. **同时算两个独立的 dyadic 函数**：令两张真值表是两个不同的两变量函数，C/D 各挑一个，双输出就同时给出两个结果。
2. **Banyan 交换开关**：令 \(g(A,B)=A\)（真值表 `DYADIC_A`）、\(h(A,B)=B\)（`DYADIC_B`），则 `{result_1, result_2}` 在 C 全 0 时为 `{A,B}`、C 全 1 时为 `{B,A}`——C 控制着 A、B 在两路输出间「直通 or 交叉」，这正是交换网络的基本积木。

若 `dual=0`，则 D=C，`result_2` 恒等于 `result_1`，退化为普通单输出 triadic 运算器。

#### 4.3.5 代码实践

**实践目标**：用两张 dyadic 真值表实现三变量 **Majority**（多数表决）函数 \(AB+AC+BC\)，验证 Shannon 分解。

**操作步骤**：

1. 把 Majority 按 C 展开：
   - \(g(A,B) = f(A,B,0) = AB + A\cdot0 + B\cdot0 = AB\)，对应 `DYADIC_A_AND_B`（`4'b1000`）。
   - \(h(A,B) = f(A,B,1) = AB + A + B\)。因为 \(A+B\) 吸收 \(AB\)（\(AB+ A + B = A+B\)），所以 \(h=A+B\)，对应 `DYADIC_A_OR_B`（`4'b1110`）。
2. 于是 Majority = `C ? (A OR B) : (A AND B)`。
3. 把它接进 Triadic 运算器（示例代码），跑几组输入核对。

```verilog
// 示例代码，非项目原有文件
Triadic_Boolean_Operator
#(.WORD_WIDTH(1)) maj
(
    .dyadic_truth_table_1(`DYADIC_A_AND_B),  // g = AB   (C=0 时的 Majority)
    .dyadic_truth_table_2(`DYADIC_A_OR_B),   // h = A+B  (C=1 时的 Majority)
    .word_A(a), .word_B(b), .word_C(c),
    .dual(1'b0),            // 单输出即可
    .result_1(maj_out),
    .result_2(/* unused */)
);
```

**需要观察的现象**：`(a,b,c)` 取 `(1,1,0)`→`maj_out=1`（两个 1）；`(1,0,0)`→`maj_out=0`（只有一个 1）；`(1,0,1)`→`maj_out=1`。

**预期结果**：`maj_out` 仅在 A、B、C 中至少有两个为 1 时为 1，正是 Majority。这与源码注释里「Majority 用于三模冗余（TMR）」的说法呼应（详见 [Bit_Voting](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bit_Voting.v)，留待 [u16-l1](./u16-l1-popcount-ntz-voting.md) 展开）。

> 待本地验证：若没有仿真器，也可纯做「纸面验证」——按 4.3.1 的公式手算 8 种输入组合，与 Majority 定义逐一对齐。

#### 4.3.6 小练习与答案

**练习 1**：为什么不直接用 8:1 mux 实现三变量运算？

> **答案**：8:1 mux 输入项数 = 8+3 = 11，超过一个 6 输入 LUT 的容量，会脱离 FPGA 底层结构、难以流水线化、更受 CAD 工具摆布。Shannon 分解把它降成「两个能各自填进 LUT 的两变量函数 + 一个按位 2:1 选择」，更贴近硬件。

**练习 2**：`dual=1` 且两位输出都接出时，相比 `dual=0` 多换来了什么？

> **答案**：`dual=0` 时 `result_2=result_1`，第二个 mux 重复劳动；`dual=1` 时 D=~C，`result_2` 输出的是 `result_1` 没选中的那一半，把原本被丢弃的 g/h 计算量利用起来，可用于同时算两个独立 dyadic 函数或做 Banyan 交换。

**练习 3**：把 `dual` 设成 1、`word_C` 全 1、两张真值表分别是 `DYADIC_A` 与 `DYADIC_B`，`result_1` 和 `result_2` 分别是什么？

> **答案**：g=A、h=B。C=1 → `result_1=h=B`。D=~C=0 → `result_2=g=A`。所以 `{result_1,result_2}={B,A}`——A、B 被交叉送出，这就是 Banyan 开关的「交叉」态。

## 5. 综合实践

把本讲三块知识串起来，完成下面这个**纸面 + 可选仿真**的小任务：

**任务**：用本讲的运算器实现一个「位掩码合并」操作——按掩码 C 选择「保留 A 的位」还是「保留 B 的位」，即 `result = C ? B : A`（按位）。

1. **选型**：判断这是 dyadic 还是 triadic 问题。（提示：结果依赖三个输入 A、B、C。）
2. **分解**：写出 g(A,B)=f(A,B,0) 与 h(A,B)=f(A,B,1)，再到运算表里查出它们对应的 `DYADIC_*` 常量。
3. **接线**：给出 `Triadic_Boolean_Operator` 的实例化代码（哪些端口接 A/B/C，`dual` 设几，用哪个 result）。
4. **（可选）验证**：用 4.2.4 的测试台风格写几组输入自检。

**参考答案**：

- 这是 triadic 问题（依赖 A、B、C 三个输入）。
- C=0 时 result=A → g(A,B)=A → `DYADIC_A`（`4'b1100`）。
- C=1 时 result=B → h(A,B)=B → `DYADIC_B`（`4'b1010`）。
- 实例化时 `dyadic_truth_table_1 = DYADIC_A`、`dyadic_truth_table_2 = DYADIC_B`、`word_C = mask`、`dual = 1'b0`、取 `result_1`。
- 进阶观察：这其实等价于直接用一个 `Multiplexer_Bitwise_2to1`（bitmask=C、in_0=A、in_1=B）。可见**同一功能常有多种拼法**，选哪种取决于你把它看成「布尔函数」还是「数据选择」——这正是本书「构建块库」思想的回报。

## 6. 本讲小结

- 两个布尔变量共有 16 种函数，每种都能压缩成一个 **4 位真值表**；三变量有 256 种、对应 8 位真值表。函数总数随变量数 \(n\) 双指数增长：\(2^{2^n}\)。
- **运算表** `Dyadic_Boolean_Operations.vh` 用 `define` 把这 16 张表命名好，并用 `ifndef` 包成幂等包含，约定「A 是真值表索引的最高位」。
- **Dyadic 运算器**把 4:1 mux 反过来用：真值表当数据、`{A[i],B[i]}` 当地址，逐位复制 N 份，每个 mux 恰好填满一个 6-LUT；改 `truth_table` 这一个绑定即可在运行时切换函数。
- **Triadic 运算器**不照搬成 8:1 mux（装不进 LUT），而用 **Shannon 分解** 把 \(f(A,B,C)\) 拆成 \(g(A,B)=f(A,B,0)\) 与 \(h(A,B)=f(A,B,1)\)，再用按位 2:1 mux 由 C 选择——`Multiplexer_Bitwise_2to1` 的注释直言它就是 Shannon 分解。
- **双输出**把按 \(f=(g\&\sim C)|(h\&C)\) 计算时被丢弃的一半结果接出来（`dual=1` 时 D=~C），可同时算两个 dyadic 函数或充当 Banyan 交换开关。
- 这是「一个模块 + 一张运算表」实现一整族函数的范式，也是后续把构建块拼成更大引擎（如算术、位操作）的模板。

## 7. 下一步学习建议

- 接着学 [u6-l1 寄存器家族](./u6-l1-register-family.md)：把本讲的组合运算器配上寄存器，就能做出流水线化的 ALU 位运算级。
- 想看三变量布尔函数的「应用侧」，可先跳读 [Bit_Voting.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bit_Voting.v)（Majority/Minority，TMR 用），正式讲解在 [u16-l1](./u16-l1-popcount-ntz-voting.md)。
- 想深入「Shannon 分解」作为组合工具的威力，留意后续讲义里 **CarryIn_Binary** 的恢复进位用法 `A^B^(A+B)`（源码注释在本讲 4.3 节已提及）。
- 回顾 [u5-l2](./u5-l2-mux-demux-address.md) 的 mux/demux，体会「布尔函数发生器」与「数据选择器」其实是同一硬件的两种视角。
