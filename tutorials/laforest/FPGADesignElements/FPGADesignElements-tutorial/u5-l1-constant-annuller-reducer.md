# 常量、置零与位/字归约

## 1. 本讲目标

本讲是「组合逻辑基础构件」单元的第一篇，也是进阶层第一篇。学完之后，你应该能够：

- 说清楚本书为什么连「输出一个常量」「按使能把数据清零」「对一串位做与/或归约」「把位重新排列」这种最琐碎的事都要封装成模块，以及这样做换来了什么。
- 掌握 `Constant`、`Annuller`、`Bit_Reducer` / `Word_Reducer`、`Word_Reverser` 这四个最小组合构件的端口、参数与用法。
- 理解贯穿本讲的一句话——**模块即文档、模块即设计意图**：模块名本身就是一句注释，实例名表达「在这个位置上想干什么」。
- 会用 `Annuller` 给一条数据通路做「按使能清零」，会用 `Bit_Reducer` 对一个向量做 AND/OR/XOR 归约。
- 看懂一个真实的拼装范例：`Multiplexer_One_Hot` = 每路一个 `Annuller` + 一个 `Word_Reducer`。

本讲承接 [u2-l2](./u2-l2-parameterization-and-widths.md) 讲过的「参数默认为 0、`{WORD_WIDTH{1'b0}}` 复制构造常量、`localparam`、`clog2`」，也承接 [u4-l1](./u4-l1-modularization-and-building-blocks.md) 提出的「连一个与门阵列都做成模块」的极致模块化主张。本讲就是要把那句话落到四个具体的零件上，让你亲手摸到「构建块库的最底层」。

## 2. 前置知识

先用大白话把几个本讲会用到的术语对齐：

- **组合逻辑（combinational logic）**：输出只取决于当前输入、不记忆历史的电路。本讲的四个模块**全是组合逻辑**，没有时钟、没有寄存器（`Constant`、`Word_Reverser` 连一个门都不消耗，只是连线）。
- **always @(*) 块**：Verilog 里描述组合逻辑的写法，`*` 表示「输入任意一个变化就重算」。按 [u3-l1](./u3-l1-assignments-and-ternary.md) 的铁律，组合块里只用阻塞赋值 `=`。
- **归约（reduction）**：把一串位（或一串字）用一个二元布尔运算（AND/OR/XOR…）「折叠」成一个结果。例如对 8 位向量做 AND 归约，就是判断「是否全为 1」。
- **generate 块**：Verilog-2001 的关键字，用来在精化（elaboration）阶段「批量生成」硬件，例如用 `for` 循环生成 N 个相同的子模块实例或 N 行赋值。本讲的 `Bit_Reducer`、`Word_Reducer`、`Word_Reverser` 都靠它实现「参数化数量的逻辑」。
- **复制构造 `{N{value}}`**：把 `value` 重复 N 次再拼接，得到位宽严格为 N 的常量。这是 [u2-l2](./u2-l2-parameterization-and-widths.md) 讲过的、参数化位宽下构造定宽零值的唯一可靠写法。
- **索引部分位选择 `base +: width`**：Verilog-2001 语法，`v[base +: width]` 表示「从第 `base` 位起、向上取 `width` 位」。本讲的 `Word_Reverser`、`Word_Reducer`、`Multiplexer_One_Hot` 都用它做按字切片。

如果你还不太熟「综合（synthesis）/精化（elaboration）」的差别，可以回看 [u1-l2](./u1-l2-repo-layout-and-conventions.md) 与 [u4-l1](./u4-l1-modularization-and-building-blocks.md)。一句话：**精化**是展开 `generate`/参数、把模块实例摆好的阶段；**综合**再把它们翻成 LUT 和连线。

## 3. 本讲源码地图

本讲围绕四个极小的组合模块展开，最后用一个真实拼装范例收口：

| 文件 | 作用 | 是否消耗逻辑 |
|------|------|------------|
| `Constant.v` | 输出一个常量值。通常只是别处的 `localparam`，但在要对接图形化 IP 系统时做成模块。 | 否（被综合为常数 / 连线） |
| `Annuller.v` | 按 `annul` 信号把一个字清零（门控）。传达「把这路变成 no-op」的设计意图。 | 是（一个 mux 或一排与门） |
| `Bit_Reducer.v` | 把 N 个输入位用 AND/NAND/OR/NOR/XOR/XNOR 折叠成 1 位。修掉了 Verilog 归约运算符的一个语义陷阱。 | 是（一棵归约树） |
| `Word_Reducer.v` | 把 N 个字用某个布尔运算折叠成 1 个字——本质是「对每一位横跨所有字做一次位归约」，内部复用 `Bit_Reducer`。 | 是（每位的归约树） |
| `Word_Reverser.v` | 把一个向量里的「字」顺序反转。靠 `generate`+`for` 在精化时重新排线，不消耗任何逻辑。 | 否（仅连线） |
| `Multiplexer_One_Hot.v` | 综合实践的锚点：用「每路一个 `Annuller` + 一个 `Word_Reducer`」拼出一个独热多路选择器。 | —— |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块。它们共同的潜台词只有一句：**凡是有名字、有意图的小逻辑，都值得做成模块**。下面逐个展开。

### 4.1 Constant：把常量做成模块

#### 4.1.1 概念说明

「输出一个固定值」大概是硬件设计里最简单的行为了——通常我们直接在模块里写一个 `localparam`，需要用的地方摆上它的名字就行。那为什么本书还要专门做一个 `Constant` 模块？

答案在它的注释里：[Constant.v:L4-L7](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Constant.v#L4-L7)。大意是：**当你要和一些「只认模块、不认 localparam」的图形化系统对接时**（例如 Xilinx 的 IP Integrator，IPI），你必须「喂给它一个模块」，这时把常量做成模块就有了意义。

这就点出了「模块即文档、模块即设计意图」的一个侧面：模块化不只是为了让代码好看，还是为了**对接基于模块的工程化系统**。在 IPI 的画布里，一个 `Constant` 方块比一句看不见的 `localparam` 直观得多。

#### 4.1.2 核心流程

`Constant` 的行为极其简单：

```text
输入：无（只有输出端口）
参数：WORD_WIDTH（位宽）、VALUE（常量值）
输出：constant_out，恒等于 VALUE，且位宽为 WORD_WIDTH

实现：用一个 initial 块给 reg 输出端口赋初值 VALUE，
      综合后这根线就是固定的常数，不消耗任何触发器或 LUT。
```

为什么用 `output reg` 加 `initial`，而不是 `output wire` 加 `assign`？因为本书的规矩是：**所有寄存器/输出都必须初始化为不含 X/Z 的值**（见 [u2-l1](./u2-l1-verilog2001-and-default-nettype.md)），而 `reg` 输出端口不能在声明处初始化，必须紧跟一个 `initial` 块赋初值。`Constant` 遵守了这条规矩。

#### 4.1.3 源码精读

整个模块只有端口和一个 `initial`：

```verilog
module Constant
#(
    parameter                   WORD_WIDTH  = 0,
    parameter  [WORD_WIDTH-1:0] VALUE       = 0
)
(
    output reg [WORD_WIDTH-1:0] constant_out
);

    initial begin
        constant_out = VALUE;
    end

endmodule
```

见 [Constant.v:L11-L24](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Constant.v#L11-L24)。三个要点：

1. **参数默认值为 0**（[u1-l2](./u1-l2-repo-layout-and-conventions.md) 讲过的「吵闹失败」栅栏）：`WORD_WIDTH=0` 时端口位宽退化为非法的 `[-1:0]`，模块按定义不可综合，逼你在实例化时显式设位宽。
2. **`VALUE` 的位宽被声明为 `[WORD_WIDTH-1:0]`**：这样无论你传什么进来的常量，都会被裁/补到 `WORD_WIDTH`，避免位宽不匹配的告警。
3. **`initial` 赋值即综合常量**：综合器看到 `constant_out` 始终等于一个编译期已知的值，会直接把它优化成固定的常数连线，不留下任何可看的内容。

#### 4.1.4 代码实践

> **实践类型：源码阅读 + 写一个实例化示例**

1. **实践目标**：体会「连常量都做成模块」的动机，并验证它综合后不剩逻辑。
2. **操作步骤**：
   - 打开 `Constant.v`，对照 [Constant.v:L4-L7](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Constant.v#L4-L7) 的注释，圈出「为何要做成模块」的那条理由（对接 IPI 等图形化 IP 系统）。
   - **示例代码**（非仓库内文件，仅供练习）：写一个顶层模块，用 `Constant` 给一条 8 位数据通路喂一个固定值 `8'hA5`：

     ```verilog
     // 示例代码
     Constant
     #(
         .WORD_WIDTH  (8),
         .VALUE       (8'hA5)
     )
     fixed_header
     (
         .constant_out (header_value)
     );
     ```

   - 若本地有 Vivado/Quartus，把这段综合一下，去看 post-synthesis 原理图里 `header_value` 这根线变成了什么。
3. **需要观察的现象**：`header_value` 应当是一根恒为 `8'hA5` 的常数线，**不占用任何 LUT 或触发器**。
4. **预期结果**：实例名 `fixed_header` 直接表达了「这是数据包的固定包头」这一意图；综合报告里这条线不消耗资源。
5. 综合结果**待本地验证**；源码阅读与实例化写法现在即可完成。

#### 4.1.5 小练习与答案

**练习 1**：既然 `Constant` 综合后不剩任何逻辑，那把它做成模块岂不是「多此一举」？请给出一个它真正有用的场景。

> **参考答案**：当你要和 Xilinx IP Integrator（IPI）这类「只认模块、不认 `localparam`」的图形化系统对接时，必须给它一个模块方块。`Constant` 让你能在原理图/Block Design 里直观地放一个「常量源」，而不是在某个看不见的地方埋一句 `localparam`。

**练习 2**：`Constant` 为什么用 `initial` 给输出赋值，而不是在声明处写 `output reg [WORD_WIDTH-1:0] constant_out = VALUE`？

> **参考答案**：本书要求所有 `reg` 都初始化为不含 X/Z 的值；而 Verilog-2001 里 `reg` 端口**不能在声明处初始化**（那是 SystemVerilog 才允许），所以必须紧跟一个 `initial` 块赋初值。这是 [u2-l1](./u2-l1-verilog2001-and-default-nettype.md) 讲过的 reg 输出初始化惯用法。

### 4.2 Annuller：把「按使能清零」做成模块

#### 4.2.1 概念说明

`Annuller` 做的事一句话能说完：**输入一个字，除非 `annul` 为高，否则原样输出；`annul` 为高时输出全零**。也就是一个「按使能清零」的门控。

它的价值同样不在「能不能算」，而在「**表达意图**」。作者在注释里亲口说了为什么：[Annuller.v:L4-L7](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Annuller.v#L4-L7)——把它做成模块是为了**传达设计意图**（例如「把这个操作码变成 no-op」），并**避免 RTL 原理图被一堆零散的门电路塞满**。这正是 [u4-l1](./u4-l1-modularization-and-building-blocks.md) 「连一个与门阵列都做成模块」的实证。

更有意思的是，作者接着把 `Annuller` 提升成一种**设计思想**：[Annuller.v:L9-L24](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Annuller.v#L9-L24)。大意是——与其用多路选择器（mux）挑出想要的那一路，不如**把不想要的路全部 annul 成零，再把剩下的路 OR-reduce 成一个结果**。这往往对最后的 LUT 更友好，还顺带给了你「如果不止一路留下来该怎么冲突解决」的自由（用 AND/OR/XOR 合并、丢给仲裁器、或并行处理）。本讲的综合实践就会用到这个套路。

#### 4.2.2 核心流程

`Annuller` 的真值表：

| `annul` | `data_in` | `data_out` |
|---------|-----------|------------|
| 0 | X | X（原样透传） |
| 1 | X | 0（清零） |

模块提供两种实现，由 `IMPLEMENTATION` 参数选择：

```text
若 IMPLEMENTATION == "MUX"： data_out = (annul == 0) ? data_in : 0;   // 二选一
若 IMPLEMENTATION == "AND"： data_out = data_in & {WORD_WIDTH{annul==0}}; // 按位与掩码
```

两者逻辑等价，但综合器对它们的「模式匹配」不同：一种可能落到触发器的复位/清零引脚，另一种可能综合成一排与门再被折进前级 LUT。作者因此把两种写法都留给你，按面积/速度取舍——这又是 [u4-l1](./u4-l1-modularization-and-building-blocks.md) 说的「通用构建块 + CAD 优化 = 特化实现」。

#### 4.2.3 源码精读

端口与参数见 [Annuller.v:L49-L58](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Annuller.v#L49-L58)：`annul` 是 1 位控制输入，`data_in`/`data_out` 都是 `WORD_WIDTH` 位。

定宽零值用复制构造，遵守 [u2-l2](./u2-l2-parameterization-and-widths.md) 的规矩：[Annuller.v:L60](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Annuller.v#L60)

```verilog
localparam ZERO = {WORD_WIDTH{1'b0}};
```

两种实现都包在 `generate` 里，靠字符串参数二选一：[Annuller.v:L66-L78](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Annuller.v#L66-L78)

```verilog
generate
    if (IMPLEMENTATION == "MUX") begin : gen_mux
        always @(*) begin
            data_out = (annul == 1'b0) ? data_in : ZERO;
        end
    end
    else if (IMPLEMENTATION == "AND") begin : gen_and
        always @(*) begin
            data_out = data_in & {WORD_WIDTH{annul == 1'b0}};
        end
    end
endgenerate
```

两个细节值得注意：

- 条件都写成 `annul == 1'b0` 这种**等式比较**（见 [u3-l1](./u3-l1-assignments-and-ternary.md) 的「布尔式写成等式比较」主张），而不是按位 `~annul`。
- `AND` 实现里 `{WORD_WIDTH{annul == 1'b0}}` 把 1 位的比较结果复制成 `WORD_WIDTH` 位的掩码，再和 `data_in` 按位与——`annul` 为高时掩码全 0，输出全 0；否则掩码全 1，原样透传。

#### 4.2.4 代码实践

> **实践类型：源码阅读 + 写一个实例化示例**

1. **实践目标**：用 `Annuller` 给一条数据通路实现「按使能清零输出」，并对比两种 `IMPLEMENTATION`。
2. **操作步骤**：
   - **示例代码**：假设你有一条 16 位数据通路 `payload`，想在 `stall` 为高时把输出清零：

     ```verilog
     // 示例代码
     Annuller
     #(
         .WORD_WIDTH     (16),
         .IMPLEMENTATION ("AND")     // 或 "MUX"
     )
     gate_on_stall
     (
         .annul    (stall),          // stall 为高时清零
         .data_in  (payload),
         .data_out (payload_gated)
     );
     ```

   - 在仓库里搜索 `Annuller` 的真实调用处（可用 `grep -n "Annuller" *.v`），看看别人给实例起了什么名字——你会发现名字往往就是一句意图说明（如 `select_input`）。
   - 若本地可综合：分别把 `IMPLEMENTATION` 设成 `"AND"` 和 `"MUX"`，对比 post-synthesis 原理图与资源占用。
3. **需要观察的现象**：实例名 `gate_on_stall` 直接说明「这是在停顿时门控」；`"AND"` 版综合成一排与门或折进前级，`"MUX"` 版可能落到触发器清零端。
4. **预期结果**：两种实现功能一致（`stall=1` 输出 0，`stall=0` 原样输出），综合结构可能不同。
5. 综合对比**待本地验证**；实例化写法与 `grep` 现在即可完成。

#### 4.2.5 小练习与答案

**练习 1**：作者说「与其用 mux 选一路，不如把不想要的路 annul 成零再 OR-reduce」。请用一句话说明这种做法相比直接用 mux 的一个好处。

> **参考答案**：annul+归约把「选择」拆成了「门控 + 合并」两步，既允许你直接控制每一级的流水线（输入很多时很重要），又允许在「不止一路留下来」时自由选择冲突解决方式（AND/OR/XOR 合并、丢给仲裁器、或并行处理），而 mux 只能死板地二选一。

**练习 2**：`Annuller` 的 `AND` 实现里，为什么是 `{WORD_WIDTH{annul == 1'b0}}` 而不能直接写 `data_in & ~annul`？

> **参考答案**：`~annul` 是 1 位的，和 `WORD_WIDTH` 位的 `data_in` 按位与会触发位宽不匹配告警/扩展行为不确定；用 `{WORD_WIDTH{...}}` 复制成定宽掩码，位宽严格匹配，行为确定且告警干净（见 [u2-l2](./u2-l2-parameterization-and-widths.md) 的复制构造）。

### 4.3 Bit_Reducer 与 Word_Reducer：把归约做成模块

#### 4.3.1 概念说明

「归约」是把一串位折叠成一位。`Bit_Reducer` 把常见的 2 输入布尔函数推广到 N 输入：[Bit_Reducer.v:L4-L9](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bit_Reducer.v#L4-L9)。它能轻松算出「这些位里是否*有任意一个*为 1（OR）」「是否*全部*为 1（AND）」「奇偶校验（XOR/XNOR）」，并且可以通过选择性地反转某些输入来译码出任意的中间条件。

这个模块对初学者特别友好：注释里直说，**初学者可以用它实现任意组合逻辑，而只需懂最少的 Verilog**（不用写 always 块、不用纠结阻塞/非阻塞，只接 wire 就行）——见 [Bit_Reducer.v:L11-L13](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bit_Reducer.v#L11-L13)。专家通常不会用它（直接写布尔式更简单），但在三种情况下仍有价值：[Bit_Reducer.v:L15-L22](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bit_Reducer.v#L15-L22)——保持原理图干净（不被一堆零散小门塞满）、给归约起个描述性名字、让综合后哪些逻辑被搬进/搬出该层次一目了然。

**为什么不用 Verilog 自带的归约运算符（`&`、`|`、`^` 等）？** 因为 Verilog 规范对带反转的归约（NAND/NOR/XNOR）有一个语义错误：[Bit_Reducer.v:L23-L42](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bit_Reducer.v#L23-L42)。规范要求 `~^B`（XNOR 归约）执行的是「先做非反转归约（XOR），再反转最终结果」：

\[ \text{Verilog 的 } \texttt{\textasciitilde\textasciicircum}B \;=\; \neg(b_0 \oplus b_1 \oplus b_2 \oplus \cdots) \]

而不是「用 XNOR 算子逐位折叠」：

\[ \text{折叠语义：}\; (\,(b_0 \,\overline{\oplus}\, b_1)\,\overline{\oplus}\, b_2\,)\,\overline{\oplus}\,\cdots \]

这两种结果并不总是相等（输入数 \(\ge 3\) 时就可能不同）。所以 `Bit_Reducer` 用循环按折叠语义实现，以保证布尔行为正确。

`Word_Reducer` 则是 `Bit_Reducer` 的「跨字」版本：[Word_Reducer.v:L4-L7](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Word_Reducer.v#L4-L7)。它把 N 个字折叠成 1 个字，本质是「对每一位横跨所有字做一次位归约」。典型用法正是 4.2 提到的套路：并行算出多个结果，把不想要的 annul 掉，再 OR-归约成一个结果。

#### 4.3.2 核心流程

`Bit_Reducer` 用「部分归约数组」逐位折叠，避免描述出组合环：

```text
输入：bits_in[N-1:0]，OPERATION ∈ {AND,NAND,OR,NOR,XOR,XNOR}
1. 用一个数组 partial_reduction[N-1:0] 存每次中间结果（每位单独存储，否则成组合环）。
2. 起步：partial_reduction[0] = bits_in[0]。
3. 循环 i=1..N-1：partial_reduction[i] = partial_reduction[i-1] op bits_in[i]。
4. 输出：bit_out = partial_reduction[N-1]。
```

数学上就是把 N 个位用算子 op 折叠：

\[ \text{bit\_out} \;=\; b_0 \;\mathrm{op}\; b_1 \;\mathrm{op}\; \cdots \;\mathrm{op}\; b_{N-1} \]

`Word_Reducer` 在此基础上多套一层循环：对字里的每一位 j（0..WORD_WIDTH-1），把所有字的第 j 位 gather 成一个 `bit_word`，再用一个 `Bit_Reducer` 把它折叠成输出字的第 j 位。即：

\[ \text{word\_out}[j] \;=\; \underset{i=0}{\overset{N-1}{\mathrm{op}}}\;\text{words\_in}_i[j] \]

#### 4.3.3 源码精读

**Bit_Reducer 的端口与初始化**：[Bit_Reducer.v:L60-L72](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bit_Reducer.v#L60-L72)。`OPERATION` 默认空串、`INPUT_COUNT` 默认 0（又是「吵闹失败」栅栏）；输出 `bit_out` 用 `initial` 初始化为 0。

**部分归约数组**：[Bit_Reducer.v:L74-L93](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bit_Reducer.v#L74-L93)。注意作者特意解释了为什么每位要单独存：若共用一个寄存器会描述出组合环；并因「跨 always 块读写同一数组」会被 linter 误判为组合环，专门用 `verilator lint_off UNOPTFLAT` 关掉这条警告。

```verilog
// verilator lint_off UNOPTFLAT
reg [INPUT_COUNT-1:0] partial_reduction;
// verilator lint_on  UNOPTFLAT
```

**起步与读出**：[Bit_Reducer.v:L98-L101](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bit_Reducer.v#L98-L101)——把第 0 位喂给 `partial_reduction[0]`，从 `partial_reduction[N-1]` 读出结果。

**按 OPERATION 选算子**：[Bit_Reducer.v:L107-L169](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bit_Reducer.v#L107-L169) 用 `generate` 串起一串 `if/else`，每种运算一个 `for` 循环。以 XOR 为例（节选）：

```verilog
if (OPERATION == "XOR") begin : gen_xor
    always @(*) begin
        for(i=1; i < INPUT_COUNT; i=i+1) begin
            partial_reduction[i] = partial_reduction[i-1] ^ bits_in[i];
        end
    end
end
```

这里有个本书反复出现的**精化期循环套路**：把第 0 次迭代「剥」出来单独处理（起步那行），循环从 `i=1` 开始，避免读 `partial_reduction[-1]` 这种越界下标。`Word_Reducer` 注释里把这个套路叫做 "peeled-out first iteration"。

> 注释 [Bit_Reducer.v:L44-L56](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bit_Reducer.v#L44-L56) 还顺带揭示了 Verilog 字符串的本质：字符串就是一串 8 位字节，所以比较 `"OR"`（16 位）和 `"NAND"`（32 位）会有位宽不匹配，linter 会报警；作者只在参数比较这几行用 `lint_off WIDTH` 临时关掉，以保持其余地方的位宽告警有意义。

**Word_Reducer 复用 Bit_Reducer**：[Word_Reducer.v:L39-L78](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Word_Reducer.v#L39-L78)。两层 `generate for`：外层遍历每一位 j，内层把每个字的第 j 位 gather 进 `bit_word`，然后用一个 `Bit_Reducer` 把它折叠：

```verilog
Bit_Reducer
#(
    .OPERATION      (OPERATION),
    .INPUT_COUNT    (WORD_COUNT)
)
bit_position
(
    .bits_in        (bit_word),
    .bit_out        (word_out[j])
);
```

这段是「构建块库」的精华：`Word_Reducer` **不重写**任何算子分支，而是直接实例化 `Bit_Reducer`——既表达了「字归约 = 位归约的复合」，又免去了把六种算子和配套的 linter 指令再抄一遍（见 [Word_Reducer.v:L53-L64](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Word_Reducer.v#L53-L64)）。要新增一种算子，只需改 `Bit_Reducer`，`Word_Reducer` 一行不用动。

`Word_Reducer.v` 末尾还给了 [另一种实现思路](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Word_Reducer.v#L82-L128)（用 `partial_reduction` 数组直接做字级折叠），用来说明「带剥出首迭代的循环」这种常见代码模式——但它无法复用 `Bit_Reducer`，所以作者没采用，留在注释里当教学。

#### 4.3.4 代码实践

> **实践类型：源码阅读 + 写一个实例化示例**

1. **实践目标**：用 `Bit_Reducer` 对一个向量做 AND 归约（判断「是否全 1」），并理解归约树的展开。
2. **操作步骤**：
   - **示例代码**：判断 8 位状态向量 `flags` 是否「全为 1」：

     ```verilog
     // 示例代码
     Bit_Reducer
     #(
         .OPERATION    ("AND"),
         .INPUT_COUNT  (8)
     )
     all_flags_set
     (
         .bits_in  (flags),
         .bit_out  (all_set)      // 1 当且仅当 flags 全 1
     );
     ```

   - 把 `OPERATION` 改成 `"OR"`（是否有任意一位为 1）、`"XOR"`（奇偶校验），分别说明语义。
   - 打开 `Bit_Reducer.v`，对照 [Bit_Reducer.v:L107-L169](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Bit_Reducer.v#L107-L169)，手算 `INPUT_COUNT=8` 时 `partial_reduction` 数组有几位、循环展开成几行赋值。
3. **需要观察的现象**：`all_set` 只在 `flags == 8'hFF` 时为 1；实例名 `all_flags_set` 直接表达了意图。`partial_reduction` 是 8 位的（含起步位），循环从 `i=1` 跑到 `i=7`，共 7 行赋值。
4. **预期结果**：AND 归约 = 全 1 检测；OR 归约 = 全 0 检测的反；XOR 归约 = 奇偶校验。
5. 行为结果可在任意 Verilog 仿真器里**待本地验证**；手算展开现在即可完成。

#### 4.3.5 小练习与答案

**练习 1**：本书为什么不用 Verilog 自带的 `&`（AND 归约运算符）来实现 `Bit_Reducer` 的 AND 功能，而要写循环？

> **参考答案**：对 AND/OR/XOR 这三种，自带运算符没问题；但对 NAND/NOR/XNOR，Verilog 规范的做法是「先做非反转归约、再反转最终结果」，这与「用反转算子逐位折叠」的语义不完全一致（输入 \(\ge 3\) 时可能不同）。为保证所有六种运算语义正确、行为一致，作者统一用循环按折叠语义实现。

**练习 2**：`Word_Reducer` 为什么要实例化 `Bit_Reducer`，而不是把六种算子再抄一遍？

> **参考答案**：实例化 `Bit_Reducer` 既表达了「字归约本质是位归约的复合」这一设计思想，又复用了已写好（含六种算子和配套 linter 指令）的代码，避免重复枯燥、易错的样板。新增算子时只改 `Bit_Reducer`，`Word_Reducer` 不用动——这是构建块库「自底向上复用」的直接体现。

### 4.4 Word_Reverser：把「重新排线」做成模块

#### 4.4.1 概念说明

`Word_Reverser` 做的事也很朴素：**把输入向量里的「字」顺序反过来**。第一个字在最右（低位），反转后跑到最左（高位）。

它的价值在于两点。第一，**Verilog 不能用「反向下标」直接翻转向量**，必须用 `for` 循环手动搬位——见 [Word_Reverser.v:L4-L7](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Word_Reverser.v#L4-L7)。既然要写循环，不如封成模块，免得每个设计各写一份。第二，**这种「纯连线」的逻辑在精化期就全部算完，不消耗任何逻辑资源**——见 [Word_Reverser.v:L16-L18](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Word_Reverser.v#L16-L18)：综合后它只是一堆改了走向的线，0 个 LUT、0 个触发器。

通过两个参数可以切换反转粒度（见 [Word_Reverser.v:L9-L14](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Word_Reverser.v#L9-L14)）：

- `WORD_WIDTH=1, WORD_COUNT=32`：反转一个 32 位字里的所有位（位反转）。
- `WORD_WIDTH=8, WORD_COUNT=4`：反转 32 位字里的字节顺序（大小端翻转）。

#### 4.4.2 核心流程

反转靠「按字切片 + 反向落位」完成：

```text
总宽 TOTAL_WIDTH = WORD_WIDTH * WORD_COUNT
对每个输入字 i（从 0 到 WORD_COUNT-1，低位在右）：
    源：words_in 里第 i 个字，位于位 [WORD_WIDTH*i +: WORD_WIDTH]
    目：words_out 里第 (WORD_COUNT-1-i) 个字，位于位 [WORD_WIDTH*(WORD_COUNT-1-i) +: WORD_WIDTH]
    => 把源搬到目的位置。
```

即第 `i` 个字（从右数）被搬到第 `WORD_COUNT-1-i` 个字的位置。所有搬运都在精化期由 `generate for` 展开，运行时只是连线。

#### 4.4.3 源码精读

端口与参数见 [Word_Reverser.v:L22-L33](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Word_Reverser.v#L22-L33)。注意一个本书常见手法：`TOTAL_WIDTH` 是由 `WORD_WIDTH * WORD_COUNT` 算出来的「派生参数」，注释明确标注「**不要在实例化时设置**」——它只用来声明端口总宽，设了反而会破坏一致性。

核心搬位逻辑只有一行，包在 `generate for` 里：[Word_Reverser.v:L42-L49](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Word_Reverser.v#L42-L49)

```verilog
generate
    genvar i;
    for (i=0; i < WORD_COUNT; i=i+1) begin : per_word
        always @(*) begin
            words_out[WORD_WIDTH*(WORD_COUNT-i-1) +: WORD_WIDTH] = words_in[WORD_WIDTH*i +: WORD_WIDTH];
        end
    end
endgenerate
```

读懂这行就抓住了全模块：

- `words_in[WORD_WIDTH*i +: WORD_WIDTH]`：取输入第 `i` 个字（从低位起算）。
- `words_out[WORD_WIDTH*(WORD_COUNT-i-1) +: WORD_WIDTH]`：放到输出从高位起算的对应位置——`i=0` 的字落到最高位字，依此类推，实现反转。

`+:` 是 Verilog-2001 的索引部分位选择，`v[base +: width]` 表示「从 `base` 起向上取 `width` 位」，`width` 必须是常量（这里是 `WORD_WIDTH`），`base` 可以是变量表达式。它比 `[base+width-1 : base]` 更安全，因为后者在 `base` 是表达式时容易写错边界。

输出用 `initial` 初始化为定宽零：[Word_Reverser.v:L35-L37](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Word_Reverser.v#L35-L37)（`output reg` 必须初始化，同 [u2-l1](./u2-l1-verilog2001-and-default-nettype.md)）。

#### 4.4.4 代码实践

> **实践类型：源码阅读 + 手算验证**

1. **实践目标**：通过改参数体会「反转粒度」，并手算一次搬位。
2. **操作步骤**：
   - 场景 A（位反转）：`WORD_WIDTH=1, WORD_COUNT=8`，输入 `words_in = 8'b1011_0010`。手算输出。
   - 场景 B（字节翻转）：`WORD_WIDTH=8, WORD_COUNT=2`，输入两个字节 `0xAA, 0x55`（拼成 `16'hAA55`，低字节在右）。手算输出。
   - 对照 [Word_Reverser.v:L46](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Word_Reverser.v#L46) 那行赋值，验证你的手算。
3. **需要观察的现象**：位反转把最低位搬到最高位；字节翻转把低字节搬到高字节位置。
4. **预期结果**：
   - 场景 A：`1011_0010` 反转 → `0100_1101`。
   - 场景 B：`16'hAA55` → `16'h55AA`。
5. 手算现在即可完成；如要在仿真器里确认，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`Word_Reverser` 综合后消耗多少 LUT？为什么？

> **参考答案**：0 个 LUT、0 个触发器。因为它只是把输入的位重新连到输出的不同位置，没有任何逻辑运算；所有「搬位」都在精化期由 `generate for` 展开成纯连线。作者在 [Word_Reverser.v:L16-L18](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Word_Reverser.v#L16-L18) 明说了这一点。

**练习 2**：`TOTAL_WIDTH` 这个参数为什么注释「不要在实例化时设置」？

> **参考答案**：它是 `WORD_WIDTH * WORD_COUNT` 的派生值，只用来声明端口总宽。如果实例化时手动设了它，又没同时改 `WORD_WIDTH`/`WORD_COUNT`，二者就会不一致，端口位宽与切片计算会对不上。这类「派生参数」应保持自动计算、对用户隐藏。

## 5. 综合实践

把本讲四个模块串起来，做一个能直接在仓库里找到原型的拼装练习。

**任务**：本书的 [`Multiplexer_One_Hot.v`](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Multiplexer_One_Hot.v) 正是「Annuller + Word_Reducer = 多路选择器」的实物。请阅读它，把本讲的零件穿起来。

它的构造分两步：

1. **每路一个 `Annuller`**：对每个输入字，如果它对应的 `selectors` 位为 0，就把这一路 annul 成零——[Multiplexer_One_Hot.v:L46-L62](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Multiplexer_One_Hot.v#L46-L62)。注意 `.annul (selectors[i] == 1'b0)`：选中（`selectors[i]==1`）时不清零、原样透传，这正是 4.2 说的「annul 掉不想要的路」。

   ```verilog
   Annuller
   #(
       .WORD_WIDTH     (WORD_WIDTH),
       .IMPLEMENTATION (IMPLEMENTATION)
   )
   select_input
   (
       .annul       (selectors[i] == 1'b0),
       .data_in     (words_in          [WORD_WIDTH*i +: WORD_WIDTH]),
       .data_out    (words_in_selected [WORD_WIDTH*i +: WORD_WIDTH])
   );
   ```

2. **一个 `Word_Reducer` 把剩下的路合并**：默认 `OPERATION="OR"`，于是所有「没被 annul 的字」OR 在一起，等价于「输出被选中的那一路」——[Multiplexer_One_Hot.v:L67-L77](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Multiplexer_One_Hot.v#L67-L77)。

   ```verilog
   Word_Reducer
   #(
       .OPERATION  (OPERATION),
       .WORD_WIDTH (WORD_WIDTH),
       .WORD_COUNT (WORD_COUNT)
   )
   combine_words
   (
       .words_in   (words_in_selected),
       .word_out   (word_out)
   );
   ```

**请你完成**：

1. 用一句话解释：为什么独热（只有一位为 1）的 `selectors` 配合 OR-归约，恰好等价于一个「选出对应输入」的多路选择器？（提示：被 annul 成零的路 OR 进去不改变结果。）
2. 设想 `selectors` 不是独热、而是有两位同时为 1：此时输出是什么？这对应 4.2 里说的哪种「冲突解决」自由度？如果把 `OPERATION` 改成 `"AND"` 又会得到什么（注释 [Multiplexer_One_Hot.v:L4-L11](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Multiplexer_One_Hot.v#L4-L11) 有提示）？
3. 指认这个设计体现了本讲的哪条核心理念。

**参考思路**：

1. 独热时，恰有一路没被 annul（原样透传），其余路全为零；零 OR 进任何值都不变，所以 OR-归约的结果就是那唯一被选中的路。
2. 两位同时为 1 时，有两路同时透传，OR-归约把它们逐位 OR——这就是 4.2 说的「不止一路留下来时，用 AND/OR/XOR 合并」的冲突解决自由度。改用 `"AND"` 则输出「所有被选中的字逐位都为 1 的位」，即「它们一致的位」；`"NOR"` 输出「它们都不为 1 的位」。这正是注释里说的 OR=普通 mux、AND=显示所有字一致的位、NOR=显示所有字都未置位的位。
3. 它体现了「**模块即设计意图**」与「**用构建块拼装**」：顶层 `Multiplexer_One_Hot` 没写任何门级逻辑，完全由 `Annuller`（每路门控）+ `Word_Reducer`（合并）拼成，实例名 `select_input`、`combine_words` 本身就是注释。

> 说明：以上行为分析可在任意 Verilog 仿真器中**待本地验证**；源码阅读与推理现在即可完成。

## 6. 本讲小结

- 本讲的四个模块——`Constant`（常量）、`Annuller`（按使能清零）、`Bit_Reducer`/`Word_Reducer`（位/字归约）、`Word_Reverser`（字序反转）——都是极小的组合逻辑，却都被封装成模块，体现「**连最琐碎的有意图逻辑也做成模块**」。
- 贯穿全讲的核心理念是 **模块即文档、模块即设计意图**：模块名/实例名本身就是一句注释，既传达意图，又避免原理图被零散门电路塞满，还能对接基于模块的图形化系统（如 IPI）。
- `Constant` 用 `initial` 给 `reg` 输出赋常量，综合后只剩连线、不耗逻辑；`Word_Reverser` 用 `generate for` 在精化期重新排线，也是 0 逻辑——两者都证明了「模块化」不等于「多耗资源」。
- `Annuller` 把「按使能清零」做成可配置（`MUX`/`AND`）的门控，并引出「annul 不想要的路 + 归约合并」这一比 mux 更灵活的选择思想。
- `Bit_Reducer` 用循环按折叠语义实现六种布尔归约，修掉了 Verilog 归约运算符对 NAND/NOR/XNOR 的语义错误；`Word_Reducer` 直接复用 `Bit_Reducer`，是构建块库「自底向上复用」的范例。
- 四个模块都严格遵守 [u2-l2](./u2-l2-parameterization-and-widths.md) 的规矩：参数默认 0、`localparam`、`{N{1'b0}}` 复制构造定宽常量；以及 [u3-l1](./u3-l1-assignments-and-ternary.md) 的规矩：组合块只用阻塞赋值、布尔式写成等式比较。

## 7. 下一步学习建议

- **下一讲 [u5-l2](./u5-l2-mux-demux-address.md)** 会从本讲的 `Annuller`+归约思想自然过渡到「正式的」多路选择器、解复用器与地址译码器，讲解二进制/独热 mux 的结构与选型——你会看到本讲综合实践里的 `Multiplexer_One_Hot` 被正式当作构建块来用。
- 若想立刻看更多「构建块拼装」，可读 `Multiplexer_One_Hot.v`（本讲综合实践的锚点）、`Pipeline_Gate.v`（看 `Annuller` 如何出现在弹性流水线里）、`Hamming_Distance.v`（看 `Bit_Reducer`/`Word_Reducer` 如何统计两个向量的差异位）。
- 若想验证本讲模块「不耗逻辑 / 只耗归约树」的说法，建议本地用 Vivado/Quartus 把 `Constant`、`Word_Reverser`、`Bit_Reducer` 各综合一次，对照资源报告与 post-synthesis 原理图。
- 后续 u8（整数算术与计数器）会用到本讲的归约与门控构件（如算术谓词里的比较、计数器里的控制），届时你会看到这些最小构件如何被层层复用。
