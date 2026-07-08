# 加减法、进位与算术谓词

## 1. 本讲目标

本讲进入「整数算术」单元，讲解如何用构建块的方式实现加减法器、进位计算器、比较谓词和位宽调整器。学完后你应该能够：

- 说清 `Adder_Subtractor_Binary` 如何**把减法改写成加法**（`A + (~B) + 1`），并自行计算出 `sum`、`carry_out`、`overflow`。
- 解释 `CarryIn_Binary` 为什么只用一行 `A ^ B ^ sum` 就能还原出每一位的进位输入。
- 用 `Arithmetic_Predicates_Binary` 一次拿到有符号 / 无符号的 `eq / lt / lte / gt / gte` 全部比较结果，并说清「有符号比较为何要借用 overflow 位」。
- 用 `Width_Adjuster` 把不同位宽的整数**归一化到同一个宽度**（零扩展 / 符号扩展 / 截断）。

本讲承接 [u5-l1 常量、置零与位/字归约](./u5-l1-constant-annuller-reducer.md) 建立的「**模块即文档、模块即设计意图**」的构建块库思想：那里把一个与门阵列（`Annuller`）、一个常量（`Constant`）都封装成模块，本讲把同样的思想用到「算术」上——连加减法器也要做成模块，并且**复用**它来拼出比较器。

## 2. 前置知识

阅读本讲前，你最好已经掌握（来自更早的讲义）：

- **参数化与位宽**（[u2-l2](./u2-l2-parameterization-and-widths.md)）：`parameter` 默认值为 `0` 的「吵闹失败」护栏、`{WORD_WIDTH{1'b0}}` 复制构造定宽常量、`localparam` 持有内部常量、为什么 `WORD_WIDTH'b0` 非法。
- **赋值风格与三元**（[u3-l1](./u3-l1-assignments-and-ternary.md)）：组合块 `always @(*)` 用阻塞 `=`、链式三元优于嵌套 `if/else`、布尔式写成等式比较 `(x == 1'b1)`。
- **构建块库**（[u5-l1](./u5-l1-constant-annuller-reducer.md)）：用经过测试的小模块自底向上拼装、`generate for` 在精化期展开、`base +: width` 变址位选、`Width_Reducer` 实例化 `Bit_Reducer` 的复用范式。

两个本讲会反复用到的小结论，先放在这里：

1. **FPGA 上的加减法要让 CAD 工具去「推断」**。把 `+` / `-` 运算符直接写在代码里，综合器会把它映射到器件里专用的**快速进位链硬件（ripple-carry / carry chain）**；如果你用一堆布尔门去「结构化」地手搭一个加法器，反而可能映射不上那条快速硬件。本讲的 `Adder_Subtractor_Binary` 就是「用 `+` 让工具推断，但把进位 / 溢出的位宽处理与计算封装成一个模块」。
2. **二进制补码减法 `A - B` 等价于 `A + (~B) + 1`**。这是本讲反复出现的核心技巧——只要把 `B` 按位取反、再补一个最低位的 `+1`，减法和加法就能用同一条加法电路完成，连每一位的进位都正确。

## 3. 本讲源码地图

本讲涉及四个文件，呈现「**工具块 → 小帮手 → 主算术块 → 复用主块的比较器**」的自底向上关系：

| 文件 | 角色 | 一句话作用 |
| --- | --- | --- |
| [Width_Adjuster.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Width_Adjuster.v) | 工具块 | 把输入向量按需**零扩展 / 符号扩展 / 截断**到目标宽度。加减法器内部用它统一位宽。 |
| [CarryIn_Binary.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CarryIn_Binary.v) | 小帮手 | 给定 `A`、`B`、`sum`，反推出每一位的**进位输入**。用一行 XOR 实现。 |
| [Adder_Subtractor_Binary.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Adder_Subtractor_Binary.v) | 主算术块 | 参数化加减法器：用 `+` 让工具推断，并自己算 `carry_out` / `overflow` / 每位 `carries`。内部实例化上面两个块。 |
| [Arithmetic_Predicates_Binary.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Arithmetic_Predicates_Binary.v) | 复用主块 | 一次做一次 `A-B`，从结果的标志位推出有符号 / 无符号的全部比较谓词（Hacker's Delight §2-12）。 |

阅读时请把这四个文件并排打开，重点观察 `Adder_Subtractor_Binary` 如何**实例化** `Width_Adjuster` 和 `CarryIn_Binary`，以及 `Arithmetic_Predicates_Binary` 如何**实例化** `Adder_Subtractor_Binary`。

## 4. 核心概念与源码讲解

### 4.1 位宽调整器 Width_Adjuster

#### 4.1.1 概念说明

做算术或布尔运算之前，常常需要先把两个**位宽不同**的整数**归一化到同一个宽度**。例如一个 1 位的进位 `carry_in` 要参与 `WORD_WIDTH` 位的加法，就必须先扩到 `WORD_WIDTH` 位，否则会触发位宽不匹配告警，甚至悄悄扩展成错的值。

`Width_Adjuster` 就是干这件事的通用工具块：给它一个输入宽度、一个目标宽度、以及一个「是否有符号」的标志，它就输出扩展或截断后的向量。源码注释明确提醒：**截断不会保护你丢失有效位**——它只管机械地取低 `WORD_WIDTH_OUT` 位。

#### 4.1.2 核心流程

设位宽差 `PAD_WIDTH = WORD_WIDTH_OUT - WORD_WIDTH_IN`，按三种情况处理：

```
若 PAD_WIDTH == 0：输出直接接输入（等宽直通）
若 PAD_WIDTH >  0：需要扩展
    若 SIGNED 且输入最高位为 1（负数）：高位补全 1（符号扩展）
    否则                              ：高位补全 0（零扩展）
若 PAD_WIDTH <  0：截断，取输入的低 WORD_WIDTH_OUT 位
```

符号扩展和零扩展是两个关键术语：

- **零扩展（zero-extend）**：高位补 0。用于无符号数扩宽，数值不变。
- **符号扩展（sign-extend）**：高位补上符号位（最高位）。用于有符号数扩宽，数值与符号都不变。例如 4 位 `1011`（-5）符号扩展到 8 位是 `11111011`（仍是 -5）。

#### 4.1.3 源码精读

模块端口很简单：输入宽度、是否有符号、输出宽度三个参数，加一对输入输出（[Width_Adjuster.v:L16-L28](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Width_Adjuster.v#L16-L28)）。注意输出是 `reg`，因为它来自本模块的 `always` 块。

核心是用 `generate` 在**精化期**就根据位宽差选出三套互斥实现之一，所以综合后只剩其中一种（[Width_Adjuster.v:L36-L58](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Width_Adjuster.v#L36-L58)）：

- 截断分支用变址位选取低位（[Width_Adjuster.v:L53-L57](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Width_Adjuster.v#L53-L57)）：`adjusted_output = original_input[WORD_WIDTH_OUT-1:0];`
- 扩展分支用三元一次性决定补 0 还是补 1（[Width_Adjuster.v:L45-L51](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Width_Adjuster.v#L45-L51)），补位用复制构造 `{PAD_WIDTH{1'b1}}` / `{PAD_WIDTH{1'b0}}` 拼出严格定宽的 pad（这正是 u2-l2 讲过的「复制构造定宽常量」手法）。

这套 `generate if` 的写法和 [u5-l1](./u5-l1-constant-annuller-reducer.md) 里 `Word_Reverser` 的 `generate for` 是同一类「精化期展开、零运行逻辑」的惯用法。

#### 4.1.4 代码实践（零扩展）

**实践目标**：亲手用 `Width_Adjuster` 把一个 4 位窄向量零扩展到 8 位，观察高位被补 0。

**操作步骤**（以下为**示例代码**，非项目原有文件，需自行放入仿真工程并设好参数）：

```verilog
`default_nettype none
module tb_width_adjuster_zero;
    reg  [3:0] in_val;
    wire [7:0] out_val;

    // 把 4 位输入零扩展（SIGNED=0）到 8 位
    Width_Adjuster #(.WORD_WIDTH_IN(4), .SIGNED(0), .WORD_WIDTH_OUT(8))
    dut (.original_input(in_val), .adjusted_output(out_val));

    initial begin
        in_val = 4'b1011;   // 4 位无符号 = 11
        #10 $display("zero-extend 4'b1011 -> %b (=%0d)", out_val, out_val);
        in_val = 4'b0101;   // 5
        #10 $display("zero-extend 4'b0101 -> %b (=%0d)", out_val, out_val);
        $finish;
    end
endmodule
`default_nettype wire
```

**需要观察的现象**：输出总是 8 位，且高 4 位恒为 0。

**预期结果**：`4'b1011` → `8'b00001011`（值仍为 11）；`4'b0101` → `8'b00000101`（值仍为 5）。

**待本地验证**：上述输出需用 Icarus Verilog（`iverilog -o sim tb.v Width_Adjuster.v && vvp sim`）或 Verilator 实跑确认。

> 思考题：若把 `.SIGNED(0)` 改成 `.SIGNED(1)`，`4'b1011` 会扩展成什么？答案见 4.1.5。

#### 4.1.5 小练习与答案

1. **练习**：为什么截断分支不需要 `SIGNED` 参数？
   **答案**：截断只取低 `WORD_WIDTH_OUT` 位，对有符号和无符号都是同一组比特，截断逻辑与符号无关；是否截掉了符号位是调用方的责任，模块不负责保护。
2. **练习**：4.1.4 里把 `SIGNED` 设为 1，`4'b1011` 扩到 8 位等于多少？
   **答案**：`4'b1011` 最高位是 1，符号扩展，补全 1，得 `8'b11111011`。作为有符号数是 -5（原 4 位 `1011` 也是 -5），数值与符号都不变。

---

### 4.2 加减法器 Adder_Subtractor_Binary

#### 4.2.1 概念说明

`Adder_Subtractor_Binary` 是一个**有符号**的参数化加减法器。一个信号 `add_sub` 决定做加法还是减法：`0` 做 `A + B + carry_in`，`1` 做 `A - B - carry_in`。这个 `0/1` 的取值刚好和「符号位」的习惯一致。

它输出四样东西：

- `sum`：结果。
- `carry_out`：最高位的进位输出（无符号运算时用它判断溢出）。
- `overflow`：有符号溢出标志（仅在处理有符号数时有意义）。
- `carries[WORD_WIDTH-1:0]`：进入每一位的进位输入（供外部做进一步的位级运算）。

源码注释点明了两条设计取向（[Adder_Subtractor_Binary.v:L14-L25](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Adder_Subtractor_Binary.v#L14-L25)）：

1. **让 CAD 工具推断加减法**，而非用布尔门结构化手搭——前者才能映射到专用快速进位链。把这个封装成模块，是为了「藏起为了让进位逻辑无告警综合而做的位宽调整」。
2. **全程用无符号加法实现**，自己处理进位。这样做的好处是——**不依赖 Verilog 那条容易踩坑的「表达式里所有项都必须声明 signed，否则整个表达式被悄悄当成无符号」规则**。

#### 4.2.2 核心流程

把减法改写成加法是核心。设 `add_sub` 为选择信号：

```
减法 (add_sub=1)：A - B - carry_in
    = A + (~B) + 1 - carry_in
    = A + (~B) + (1 + carry_in_signed)
其中 carry_in_signed 把 1 位的 carry_in 符号扩展成 0 或 -1：
    carry_in=0 -> 0 ;  carry_in=1 -> -1
于是 +1 + (-1) = 0，正好抵消成 A - B - 1，即带借位的减法。

加法 (add_sub=0)：A + B + carry_in
    = A + B + carry_in_unsigned
其中 carry_in_unsigned 把 carry_in 零扩展成 0 或 +1。
```

为此模块准备三组「可能取反的 B、补码偏移、选定的进位」，再做一次统一的加法。整个过程：

1. 用两个 `Width_Adjuster` 把 1 位 `carry_in` 同时扩成**无符号（0/+1）**和**有符号（0/-1）**两个 `WORD_WIDTH` 位宽的字。
2. 组合块按 `add_sub` 选出 `B_selected`（`B` 或 `~B`）、`negation_offset`（`0` 或 `+1`）、`carry_in_selected`（无符号版或有符号版）。
3. 把它们都前补一个 0 位、扩成 `WORD_WIDTH+1` 位，做一次**无符号加法**，最高位天然就是 `carry_out`。
4. 用 `CarryIn_Binary` 反推出每一位的进位输入 `carries`。
5. 用「MSB 的进位输入与进位输出是否一致」算出有符号 `overflow`。

有符号溢出的判定标准是：**进入最高位的进位**与**离开最高位的进位**不一致时发生溢出。这正是第 5 步的依据。

#### 4.2.3 源码精读

端口与参数（[Adder_Subtractor_Binary.v:L29-L42](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Adder_Subtractor_Binary.v#L29-L42)）：注意 `sum`、`carry_out`、`overflow` 是 `reg`（来自本模块 always 块），而 `carries` 是 `wire`（来自下面的子实例）。

定宽常量 `ZERO` 和 `ONE`（[Adder_Subtractor_Binary.v:L44-L45](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Adder_Subtractor_Binary.v#L44-L45)）：`ONE = {{WORD_WIDTH-1{1'b0}},1'b1}` 用拼接在最低位放一个 1，得到严格定宽的常数 `+1`（这是减法补码 `+1` 的来源）。

第 1 步，两个 `Width_Adjuster` 把 `carry_in` 扩成无符号和有符号两版（[Adder_Subtractor_Binary.v:L58-L83](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Adder_Subtractor_Binary.v#L58-L83)）。这是上一节 `Width_Adjuster` 的直接应用，也呼应了注释里「不靠符号扩展那堆坑」的说法。

第 2 步，组合块选出三个中间量（[Adder_Subtractor_Binary.v:L95-L99](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Adder_Subtractor_Binary.v#L95-L99)）：三行链式三元，干净地表达了「加法用原值、减法用取反 + 偏移」。

第 3 步，统一加法（[Adder_Subtractor_Binary.v:L116-L118](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Adder_Subtractor_Binary.v#L116-L118)）：

```verilog
{carry_out, sum} = {1'b0, A} + {1'b0, B_selected}
                 + {1'b0, negation_offset} + {1'b0, carry_in_selected};
```

左边是 `WORD_WIDTH+1` 位（多 1 位存 `carry_out`），右边每项都**显式前补一个 0**到同宽。注释说得很明白：本可以让 Verilog 隐式扩展（见 LRM 1364-2001 §4.4），但作者刻意避免隐式扩展以减少告警、防 bug，于是手动补 0，逼出一次「干净的无符号加法」。

第 4 步，实例化 `CarryIn_Binary` 反推每位进位（[Adder_Subtractor_Binary.v:L125-L135](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Adder_Subtractor_Binary.v#L125-L135)）。注意它把**可能已取反的** `B_selected` 喂给 `CarryIn_Binary`，因为减法时取反的 B 对外不可见，必须在这里算。

第 5 步，有符号溢出（[Adder_Subtractor_Binary.v:L140-L142](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Adder_Subtractor_Binary.v#L140-L142)）：`overflow = (carries[WORD_WIDTH-1] != carry_out);`——进 MSB 的进位与出 MSB 的进位不一致即溢出。

#### 4.2.4 代码实践（手算减法并对照源码）

**实践目标**：用一个 4 位减法验证「`A - B = A + (~B) + 1`」，并对照源码确认 `carry_out` 与 `overflow`。

**操作步骤**：取 `WORD_WIDTH=4`，`add_sub=1`（减法），`carry_in=0`，`A=4'b0011`（3），`B=4'b0101`（5）。手工跟踪：

1. `B_selected = ~B = 4'b1010`
2. `negation_offset = ONE = 4'b0001`
3. `carry_in_selected = 0`（减法用有符号扩展，carry_in=0 → 全 0）
4. `{carry_out, sum} = {1'b0,0011} + {1'b0,1010} + {1'b0,0001} = 5'b01110` → `carry_out=0, sum=4'b1110`
5. `CarryIn_Binary` 反推：`carries = A ^ B_selected ^ sum = 0011 ^ 1010 ^ 1110`。

**需要观察的现象**：`sum = 4'b1110` 作为有符号数是 -2，正好是 `3 - 5`；`carry_out=0` 表示无符号意义下发生了借位（3 < 5）；MSB 进位输入 `carries[3]` 与 `carry_out` 是否一致决定 `overflow`。

**预期结果**：`sum = 4'b1110`（-2），`carry_out = 0`。因为 `3 - 5` 没有超出 4 位有符号表示范围（-8..7），故 `overflow` 应为 0。

**待本地验证**：用仿真器实例化一个 `WORD_WIDTH=4` 的 `Adder_Subtractor_Binary` 跑这组输入，打印 `sum`、`carry_out`、`overflow`、`carries` 与手算对照。

#### 4.2.5 小练习与答案

1. **练习**：为什么作者要在加法那一行给每个右值都前补 `{1'b0, ...}`？
   **答案**：为了显式地把所有项扩到 `WORD_WIDTH+1` 位，与左值 `{carry_out, sum}` 同宽，避免依赖 Verilog 的隐式位宽扩展规则，从而消除综合告警、减少隐式扩展带来的潜在 bug。
2. **练习**：若做 `A=4'b1000`（-8）`- B=4'b0001`（1），`overflow` 会是多少？
   **答案**：`-8 - 1 = -9`，超出 4 位有符号范围（-8..7），发生溢出，`overflow = 1`。可自行用 `A + (~B) + 1` 验证 MSB 进位入与进位出不一致。

---

### 4.3 进位计算器 CarryIn_Binary

#### 4.3.1 概念说明

`CarryIn_Binary` 是一个很小的「小帮手」模块。给定参与加法的两个数 `A`、`B` 以及它们的和 `sum`，它能反推出**每一位的进位输入**是多少。

这件事的价值在于：拿到「进入最高位的进位」之后，把它和「离开最高位的进位」对比，就能判断**有符号溢出**，也能进一步算出各种**算术比较谓词**（下一节）。它还可以用于子字并行计算，比如判断一个向量里某字节的加法有没有溢出到相邻字节。

#### 4.3.2 核心流程

一位全加器的真值表中，和位与输入、进位的关系是：

\[
\text{sum}_i = A_i \oplus B_i \oplus \text{carry\_in}_i
\]

也就是说，和位是「两个输入位」与「进位输入」三者异或。把上式两边都异或上 \(A_i \oplus B_i\)，就能把进位输入孤立出来：

\[
\text{carry\_in}_i = A_i \oplus B_i \oplus \text{sum}_i
\]

于是只要把整个字做按位异或 `A ^ B ^ sum`，就一次性得到了每一位的进位输入——零个比较器、零个 if，纯组合逻辑，一行搞定。

#### 4.3.3 源码精读

端口只有三个输入（`A`、`B`、`sum`）和一个输出 `carryin`（[CarryIn_Binary.v:L19-L28](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CarryIn_Binary.v#L19-L28)）。`carryin` 是 `reg` 并在 `initial` 里初始化为全 0（[CarryIn_Binary.v:L30-L32](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CarryIn_Binary.v#L30-L32)），遵循本书「所有寄存器都初始化」的规矩（这里虽是组合 `reg`，仍给初值以消除 X）。

核心一行（[CarryIn_Binary.v:L39-L41](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CarryIn_Binary.v#L39-L41)）：

```verilog
carryin = A ^ B ^ sum;
```

注释解释得很清楚：先「无进位地重加一次」（即 XOR），再与输入的 `sum` 比较（仍是 XOR），若不同则说明该位当时有进位输入。

#### 4.3.4 代码实践（验证 XOR 还原进位）

**实践目标**：用 4.2.4 的减法实例，验证 `A ^ B_selected ^ sum` 确实还原出每位进位。

**操作步骤**：沿用 `A=4'b0011`、`B_selected=4'b1010`、`sum=4'b1110`，计算 `carries = 0011 ^ 1010 ^ 1110`。

**需要观察的现象**：`carries[3]`（进 MSB 的进位）的值，应与「MSB 这一列是否真有进位输入」一致。

**预期结果**：`0011 ^ 1010 = 1001`，`1001 ^ 1110 = 0111`，即 `carries = 4'b0111`。最高位 `carries[3]=0`，与 `carry_out=0` 相等，故 `overflow=0`，与 4.2.4 结论一致。

**待本地验证**：在 4.2.4 的仿真里把 `carries` 也打印出来对照。

#### 4.3.5 小练习与答案

1. **练习**：为什么不能用 `A + B == sum` 来检测进位？
   **答案**：`+` 运算本身会丢掉进位信息（结果被截断到 `WORD_WIDTH` 位），无法逐位还原进位；而 XOR 是逐位运算，且和位正是 `A^B^carry_in`，所以 `A^B^sum` 能精确还原每一位的进位输入。
2. **练习**：`CarryIn_Binary` 里的 `carryin` 是 `reg` 还是寄存器？
   **答案**：是 `reg` 但**不是**寄存器——它由 `always @(*)`（组合块）驱动，综合后是纯组合逻辑。`reg` 在 Verilog 里只是「过程赋值的目标」，与是否有时钟无关（见 u3-l1）。

---

### 4.4 算术谓词 Arithmetic_Predicates_Binary

#### 4.4.1 概念说明

做一次 `A - B`，结果的几个标志位就足以推出**所有的整数比较**：相等、小于、小于等于、大于、大于等于，而且有符号、无符号各一套。`Arithmetic_Predicates_Binary` 就是把这件事封装好的模块，一次实例化就同时输出 9 个谓词（1 个相等 + 4 个无符号 + 4 个有符号）。

它的算法依据是 Henry S. Warren, Jr.《Hacker's Delight》§2-12「计算机如何设置比较标志」（[Arithmetic_Predicates_Binary.v:L1-L15](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Arithmetic_Predicates_Binary.v#L1-L15)）。这也是本讲「**复用主块拼出更上层功能**」的范例：它内部直接实例化了一个 `Adder_Subtractor_Binary` 做减法，再把标志位组合成谓词——和 [u5-l1](./u5-l1-constant-annuller-reducer.md) 里 `Word_Reducer` 实例化 `Bit_Reducer` 是同一种「构建块库」复用。

#### 4.4.2 核心流程

先做一次 `A - B`，拿到三个量：

- `difference`：`A - B` 的结果。
- `negative`：结果的符号位（`difference[MSB]`）。
- `carry_out`：减法的进位输出（注意二进制补码减法里它的**含义是反的**：`carry_out=1` 表示**没有**借位，即无符号意义下 `A >= B`）。
- `overflow_signed`：有符号溢出。

然后分两套推导：

**无符号比较**（只看 `carry_out` 与 `A_eq_B`）：

| 谓词 | 表达式 | 直觉 |
| --- | --- | --- |
| `A_eq_B` | `difference == 0` | 减法为 0 即相等 |
| `A_lt_B_unsigned` | `carry_out == 0` | 减法有借位 → A < B |
| `A_gte_B_unsigned` | `carry_out == 1` | 无借位 → A >= B |
| `A_lte_B_unsigned` | `lt \|\| eq` | |
| `A_gt_B_unsigned` | `gte && !eq` | |

**有符号比较**（关键在于修正溢出对符号位的影响）：

| 谓词 | 表达式 |
| --- | --- |
| `A_lt_B_signed` | `negative != overflow_signed` |
| `A_gte_B_signed` | `negative == overflow_signed` |
| `A_lte_B_signed` | `lt \|\| eq` |
| `A_gt_B_signed` | `gte && !eq` |

为什么有符号比较要 `negative XOR overflow`？直觉是：

- 若**没有溢出**：结果的符号位如实反映 `A - B` 的真实符号，`negative=1` 就意味着 `A < B`，所以 `A_lt_B = negative`。
- 若**发生溢出**：结果的符号位被「翻反」了，`negative=1` 反而意味着 `A >= B`，所以 `A_lt_B = !negative`。
- 两种情况合并，恰好就是 `A_lt_B = negative XOR overflow`。

这正是主流 CPU（如 x86）做有符号比较时「`SF != OF` 即 less-than」的同款逻辑。

#### 4.4.3 源码精读

端口一次给出 9 个谓词输出（[Arithmetic_Predicates_Binary.v:L16-L35](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Arithmetic_Predicates_Binary.v#L16-L35)）。

第 1 步，实例化 `Adder_Subtractor_Binary` 做减法（`add_sub=1`、`carry_in=0`），拿 `difference`、`carry_out`、`overflow_signed`（[Arithmetic_Predicates_Binary.v:L57-L73](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Arithmetic_Predicates_Binary.v#L57-L73)）。注意它把不用的 `carries` 端口空连 `()`，并用 `verilator lint_off PINCONNECTEMPTY` 抑制告警——这是本书处理「悬空输出」的标准写法。

第 2 步，组合块用阻塞赋值依次算出全部谓词（[Arithmetic_Predicates_Binary.v:L84-L97](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Arithmetic_Predicates_Binary.v#L84-L97)）。注释特意指出它**利用了阻塞赋值的顺序求值**来复用中间结果（如先算 `A_lt_B_unsigned`，再用它拼 `A_lte_B_unsigned`），让综合器更容易优化出共享逻辑。这呼应了 u3-l1 讲过的「组合块用阻塞 `=`、可拆成几行中间量逐步算」。

布尔式都写成等式比较（如 `(carry_out == 1'b0)`、`(negative != overflow_signed)`），而不是按位运算——正是 u3-l1 强调的「布尔式写成等式比较」规范。

#### 4.4.4 代码实践（有符号比较）

**实践目标**：用 `Arithmetic_Predicates_Binary` 做一组跨「正负」的有符号比较，验证 `A_lt_B_signed` 在溢出情形下仍正确。

**操作步骤**（以下为**示例代码**，非项目原有文件）：

```verilog
`default_nettype none
module tb_predicates_signed;
    reg  [3:0] A, B;
    wire       eq, lt_u, lte_u, gt_u, gte_u, lt_s, lte_s, gt_s, gte_s;

    Arithmetic_Predicates_Binary #(.WORD_WIDTH(4)) dut (
        .A(A), .B(B),
        .A_eq_B(eq),
        .A_lt_B_unsigned(lt_u), .A_lte_B_unsigned(lte_u),
        .A_gt_B_unsigned(gt_u), .A_gte_B_unsigned(gte_u),
        .A_lt_B_signed(lt_s),   .A_lte_B_signed(lte_s),
        .A_gt_B_signed(gt_s),   .A_gte_B_signed(gte_s)
    );

    integer ai, bi;
    initial begin
        // 扫描几组有代表性的 4 位有符号对（-8..7）
        for (ai = -8; ai <= 7; ai = ai + 1)
            for (bi = -8; bi <= 7; bi = bi + 1) begin
                A = ai[3:0]; B = bi[3:0]; #1;
                // 自检：硬件谓词应与软件语义一致
                if ((ai <  bi) !== lt_s) $display("SIGNED LT MISMATCH: %0d<%0d got %b", ai, bi, lt_s);
                if ((ai >= bi) !== gte_s) $display("SIGNED GE MISMATCH: %0d>=%0d got %b", ai, bi, gte_s);
            end
        $display("signed predicate sweep done");
        $finish;
    end
endmodule
`default_nettype wire
```

**需要观察的现象**：全范围扫描 256 组（含 `-8` 与正数比较这种**会触发溢出**的边界）后，自检不应打印任何 `MISMATCH`。

**预期结果**：`A_lt_B_signed` 在所有 256 组上都等于软件语义的 `ai < bi`，包括 `ai=-8, bi=7` 这种「最小减最大」、符号位会被溢出翻反的极端情形——这正验证了 `negative XOR overflow` 的修正作用。

**待本地验证**：用 Icarus Verilog 实跑确认无 `MISMATCH` 输出。

#### 4.4.5 小练习与答案

1. **练习**：为什么无符号比较完全不需要 `overflow`？
   **答案**：无符号比较只关心「减法有没有借位」，而借位直接体现在 `carry_out` 上（`carry_out=0` 即有借位 → `A < B`）。溢出是「有符号结果超出表示范围」的概念，对无符号比较无意义。
2. **练习**：把 `Arithmetic_Predicates_Binary` 想成「一个 `Adder_Subtractor_Binary` 加一点组合逻辑」，这体现了什么设计思想？
   **答案**：体现了构建块库的自底向上复用——不重新实现减法和标志位计算，而是直接实例化已测试好的 `Adder_Subtractor_Binary`，在其输出上拼接谓词逻辑。这与 `Counter_Binary = Adder_Subtractor_Binary + Register`（下一讲 u8-l2）是同一种思路。

---

## 5. 综合实践

把本讲四个模块串起来，搭一个**带比较结果输出的 8 位加减运算单元**。

**任务**：实例化一个 `WORD_WIDTH=8` 的 `Adder_Subtractor_Binary`，把它的 `sum`、`carry_out`、`overflow` 引出；再实例化一个 `Arithmetic_Predicates_Binary` 拿到 `A_lt_B_signed`、`A_gt_B_signed`、`A_eq_B`。然后做下列验证：

1. 选 `A = 8'sd50`、`B = 8'sd30`、`add_sub=0`（加法），确认 `sum = 80`、无 `overflow`。
2. 选 `A = 8'sd100`、`B = 8'sd100`、`add_sub=1`（减法），确认 `A_eq_B=1`、`difference=0`。
3. 选 `A = -8'sd100`（`8'b10011100`）、`B = 8'sd100`、`add_sub=1`，确认 `overflow` 是否为 1（`-200` 超出 8 位有符号范围 -128..127），并确认此时 `A_lt_B_signed` 仍正确为 1。
4. 额外思考：如果你还想把一个 1 位的 `carry_in` 接进这个单元，应该在哪一步用 `Width_Adjuster` 把它扩到 8 位？（提示：`Adder_Subtractor_Binary` 内部已经替你做了这件事，见 4.2.3。）

**预期结果**：第 3 步会触发有符号溢出，`A_lt_B_signed` 靠 `negative XOR overflow` 修正后仍为 1，正好说明溢出修正的必要。**待本地验证**：完整仿真并打印各标志位。

## 6. 本讲小结

- `Adder_Subtractor_Binary` 用 `+` 让 CAD 工具**推断**加减法（映射到快速进位链），并把减法改写成 `A + (~B) + 1`，全程无符号加法，绕开 Verilog 的 signed 表达式陷阱。
- 有符号溢出判定为「**进 MSB 的进位 ≠ 出 MSB 的进位**」，即 `overflow = (carries[MSB] != carry_out)`。
- `CarryIn_Binary` 用一行 `A ^ B ^ sum` 还原每一位的进位输入，依据是全加器和位公式 `sum_i = A_i ^ B_i ^ carry_in_i`。
- `Arithmetic_Predicates_Binary` 复用一个 `Adder_Subtractor_Binary` 做一次 `A-B`，从标志位一次推出有符号 / 无符号的全部比较谓词；**有符号比较的核心是 `negative XOR overflow`**（即 x86 的 `SF != OF`）。
- `Width_Adjuster` 用 `generate if` 在精化期按位宽差三选一：等宽直通、（符号/零）扩展、截断，是统一位宽的通用工具块。
- 四个模块呈现清晰的「工具块 → 小帮手 → 主块 → 复用主块」自底向上复用链，是构建块库思想在算术上的落地。

## 7. 下一步学习建议

- 下一讲 [u8-l2 二进制计数器与可复用函数](./u8-l2-counter-and-functions.md) 会把本讲的 `Adder_Subtractor_Binary` 与 `Register` 拼成 `Counter_Binary`，并讲解 `clog2_function.vh` 这类可复用函数库的组织——正是本讲「自底向上复用」的延续。
- 想深入「进位链 / 溢出 / 比较标志」背后的位运算技巧，推荐阅读源码注释引用的 Henry S. Warren, Jr.《Hacker's Delight》§2-12，以及 `Adder_Subtractor_Binary.v` 注释里提到的 LRM 1364-2001 §4.4（表达式位长）。
- 建议继续浏览本书的 `verilog.html` 中关于算术推断的章节，理解「为什么用 `+` 而不是结构化门级描述」的更多细节。
