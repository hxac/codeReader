# 多路选择器、解复用与地址译码

## 1. 本讲目标

本讲承接 u5-l1（`Constant`/`Annuller`/`Bit_Reducer`/`Word_Reducer` 这些最基础的组合构件），进入「选择与路由」这一族构件。读完后你应当能够：

- 说出**二进制 mux** 与**独热 mux** 的电路结构差异，并能在具体场景里做出选型；
- 看懂**解复用器（demux）**如何由「二进制→独热转换 + 独热 demux」拼装而成，以及 `BROADCAST` 参数的含义；
- 区分**静态/行为/算术**三种地址译码器与**地址翻译器**的适用范围，并理解 one-hot 与 binary 编码在面积、速度、布线上的取舍；
- 用 `Address_Decoder_Static` 配合 `Multiplexer_One_Hot` 搭出一个最小的「地址 → 片选 → 读数据回送」逻辑。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**多路选择器（multiplexer，简称 mux）** 是一个「N 选 1」的开关：它有若干个数据输入、一个选择信号、一个输出。选择信号的值决定把哪个输入送到输出。我们可以把选择信号理解成「用地址去寻址一个输入」。于是 mux 天然和一个「只读存储」等价：把可能的输出值排成一排当输入，用选择信号去「读」其中一个。

**解复用器（demultiplexer，简称 demux）** 是 mux 的镜像：它有 1 个数据输入、一个选择信号、N 个数据输出。选择信号决定把输入送到哪一路输出（其它路通常清零）。它常用于「把一笔数据按地址分发到对应外设」，并附带给出一个 `valid` 位指明「这次送到了第几路」。

**地址译码器（address decoder）** 回答一个问题：「这个地址落在我负责的范围里吗？」输出只有 1 位 `hit`。地址翻译器（address translator）则更进一步，把一个「没对齐到 2 的幂」的地址范围重排成从 0 开始的连续下标，方便去索引 RAM 或寄存器。这两个概念是内存映射（memory-mapped I/O）的基石。

下面两个术语会反复出现，先解释清楚：

- **binary（二进制）编码**：用 \( \lceil \log_2 N \rceil \) 位表示 N 个选项之一，线少但要先「译码」才能选出具体某一路。
- **one-hot（独热）编码**：用 N 位表示 N 个选项，某一时刻恰好（理想情况下）只有一位为 1。线多，但每一路都有自己的「使能线」，选择逻辑极浅。

本讲还会用到 u2-l2 讲过的**带变址的部分位选** `base +: width`（从 `base` 起取 `width` 位）、u2-l2 的**复制构造** `{N{1'b0}}`、以及 u5-l1 的 `Annuller`（按使能把一个字清零）与 `Word_Reducer`（把多个字归约成一个字）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Multiplexer_Binary_Behavioural.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Multiplexer_Binary_Behavioural.v) | 用二进制选择信号 + 向量部分位选实现的通用 mux，全书推荐的二进制 mux |
| [Multiplexer_One_Hot.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Multiplexer_One_Hot.v) | 用独热选择信号实现的 mux，由 `Annuller` + `Word_Reducer` 拼成，输出函数可参数化 |
| [Demultiplexer_Binary.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Demultiplexer_Binary.v) | 二进制 demux，内部先转独热再调用独热 demux |
| [Demultiplexer_One_Hot.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Demultiplexer_One_Hot.v) | 独热 demux，演示 `BROADCAST` 两种实现 |
| [Binary_to_One_Hot.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Binary_to_One_Hot.v) | 二进制→独热转换器，由 N 个 `Address_Decoder_Behavioural` 组成 |
| [Address_Decoder_Static.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Address_Decoder_Static.v) | 静态地址译码器，范围在参数里固定，逐地址比较后 OR 归约 |
| [Address_Decoder_Behavioural.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Address_Decoder_Behavioural.v) | 行为级地址译码器，范围可运行时改变，两次比较，全书推荐 |
| [Address_Decoder_Arithmetic.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Address_Decoder_Arithmetic.v) | 算术地址译码器，强制用两个减法器做比较 |
| [Address_Translator_Static.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Address_Translator_Static.v) | 静态地址翻译器，把未对齐范围重排为连续下标 |

---

## 4. 核心概念与源码讲解

### 4.1 多路选择器（Multiplexer）

#### 4.1.1 概念说明

mux 回答「N 选 1」。本书没有为每种位宽各写一个 mux，而是写**一个参数化的通用 mux**，靠参数 `WORD_WIDTH`（每个输入字的位宽）、`INPUT_COUNT`/`WORD_COUNT`（输入路数）来定规模。这正是 u4-l1「构建块库」思想：用一个通用模块，靠固定输入/调参数让 CAD 工具优化成特化实现。

本书提供了两种风格的 mux，对应两种选择信号编码：

- **二进制 mux**（`Multiplexer_Binary_Behavioural`）：选择信号是紧凑的二进制地址。
- **独热 mux**（`Multiplexer_One_Hot`）：选择信号是独热向量，每一路输入对应一位使能。

#### 4.1.2 核心流程

**二进制 mux** 的核心只有一行：把 `selector` 当成「字地址」，用带变址的部分位选从拼接好的输入向量里切出一个字。

```text
word_out = words_in[ selector*WORD_WIDTH  +:  WORD_WIDTH ]
                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                 从 (selector*WORD_WIDTH) 起，取 WORD_WIDTH 位
```

注意输入约定：**第 0 个字放在拼接向量的最右边**（最低位那一段）。`selector` 越界时输出未定义；`selector` 为 X/Z 时输出为 X/Z（这一点很重要，下面解释）。

**独热 mux** 的核心是「先按位清零，再归约合并」两步，这正是 u5-l1 抛出的「annul 不想要的路 + OR 归约合并」思想的落地：

```text
(1) 对每个输入字，若它的 selector 位为 0，就把该字 annul（清零）。
(2) 把所有（已被清零或保留的）字用 Word_Reducer 归约成一个字。
```

当恰好只有一位 selector 为 1 时，结果就是被选中的那个字；当多位为 1 时，结果是这些字的布尔组合（OR/AND/NOR/…，由 `OPERATION` 参数决定）。

#### 4.1.3 源码精读

**二进制 mux 的核心实现**——一行部分位选：

[Multiplexer_Binary_Behavioural.v:88-94](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Multiplexer_Binary_Behavioural.v#L88-L94) —— 用 `initial` 给 `reg` 输出端口赋初值（u2-l1 讲过的规矩），再用 `always @(*)` 做组合选择。`(selector * WORD_WIDTH) +: WORD_WIDTH` 是 u2-l2 的带变址部分位选。

端口声明体现了 u2-l2 的参数化与「派生参数」用法：

[Multiplexer_Binary_Behavioural.v:73-86](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Multiplexer_Binary_Behavioural.v#L73-L86) —— `TOTAL_WIDTH = WORD_WIDTH * INPUT_COUNT` 是「不要在实例化时设」的派生参数，用来声明拼接输入的总位宽。

> **为什么用部分位选，而不用 `if` 或 `case`？**
> 源码注释用一个「二选一」的反例说明了 Verilog 的著名陷阱：`if (selector == 1'b0)` 在 `selector` 为 X 时会把 X 当成「假」而走 `else`，导致**仿真返回 `option_B`、综合却返回 X**——仿真与综合不一致。改用部分位选后，`selector` 为 X 时索引为 X，输出自然是 X，仿真和综合一致。这与 u3-l1「三元优于 `if`、并要正确传播 X」是一脉相承的。

**独热 mux 的两步实现**——`Annuller` 数组 + `Word_Reducer`：

[Multiplexer_One_Hot.v:42-62](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Multiplexer_One_Hot.v#L42-L62) —— 用 `generate for` 为每个输入字实例化一个 `Annuller`，当 `selectors[i] == 1'b0` 时把该字清零。这里把 u5-l1 的 `Annuller` 当构建块直接复用。

[Multiplexer_One_Hot.v:67-77](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Multiplexer_One_Hot.v#L67-L77) —— 再用 u5-l1 的 `Word_Reducer` 把清零后的若干字归约成一个字；`OPERATION` 默认 `"OR"`，正好实现「多路选一」。

`Multiplexer_One_Hot` 的端口与参数：

[Multiplexer_One_Hot.v:22-36](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Multiplexer_One_Hot.v#L22-L36) —— 注意 `selectors` 的位宽正好等于 `WORD_COUNT`（每路一位），`words_in` 的总位宽是 `WORD_COUNT * WORD_WIDTH`。

**选型小结**（详见 4.3.5 的练习与第 5 节）：

| 维度 | 二进制 mux | 独热 mux |
| --- | --- | --- |
| 选择信号位宽 | \( \lceil \log_2 N \rceil \) | \( N \) |
| 选择逻辑深度 | 较深（译码树） | 极浅（每路一个独立门） |
| 速度 | 受译码树限制 | 通常更快 |
| 布线 | 选择信号线少 | 选择信号线多（每路一线） |
| 输出函数 | 隐含 OR，不可改 | `OPERATION` 可参数化（OR/AND/NOR/…） |
| 适用 | 选择信号本来就是二进制（如寄存器号、地址） | 选择信号本来就是独热（如片选、仲裁授权） |

源码注释还提到：若你的 HDL 没有 Verilog 的「部分位选」对应物，二进制 mux 无法移植，可改用结构化版本 `Multiplexer_Binary_Structural`，它还能把输出函数从隐含的 OR 扩展成其它布尔运算。

#### 4.1.4 代码实践

**实践目标**：用一个 mux 当「小型查找表」，体会「mux = 只读存储」。

**操作步骤**（源码阅读型 + 示例实例化）：

1. 打开 [Multiplexer_Binary_Behavioural.v:92-94](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Multiplexer_Binary_Behavioural.v#L92-L94)，确认它就是 `word_out = words_in[(selector*WORD_WIDTH) +: WORD_WIDTH]`。
2. 想象一个 4 选 1、字宽 4 位的 mux，把 `words_in` 设成 4 个常量 `4'd2, 4'd3, 4'd5, 4'd7`（第 0 个在最右）。这相当于实现了「输入 0/1/2/3 → 输出 2/3/5/7」的小函数。
3. 下面是**示例代码**（非项目原有），展示如何实例化它：

```verilog
// 示例代码：用二进制 mux 实现一个 4 选 1 的小函数
wire [1:0] selector;
wire [15:0] words_in  = {4'd7, 4'd5, 4'd3, 4'd2}; // 第0个(=2)在最右
wire [3:0]  word_out;

Multiplexer_Binary_Behavioural
#(
    .WORD_WIDTH  (4),
    .INPUT_COUNT (4),
    .ADDR_WIDTH  (2)        // clog2(4) = 2
)
func_lut
(
    .selector (selector),
    .words_in (words_in),
    .word_out (word_out)
);
```

**需要观察的现象**：`selector=2'b00` 时 `word_out` 应为 `4'd2`；`selector=2'b11` 时为 `4'd7`。若故意把 `selector` 设成 X，输出应为 X（验证上面讲的「仿真/综合一致」）。

**预期结果**：输出严格等于被选中那一段的值。综合后该 mux 应折叠成少量 LUT（按 u3-l1 的估算法，4:1 mux 输入项数 \( N+\log_2 N = 4+2=6 \)，恰为一个 6 输入 LUT）。

> 完整的仿真/综合现象**待本地验证**（取决于你的 CAD 工具如何优化）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Multiplexer_Binary_Behavioural` 不用 `case` 语句实现？

**参考答案**：`case` 在路数多时既冗长又易错，而且每改一次输入路数都要改实现；部分位选是一行通用代码，路数由参数决定，CAD 自然优化。此外 `case`（默认 `if` 式匹配）在 X 处理上也有和 `if` 类似的仿真/综合不一致风险。

**练习 2**：独热 mux 里，如果 `selectors` 有两位同时为 1，输出是什么？

**参考答案**：取决于 `OPERATION`。默认 `"OR"` 时，输出是两个被选中字的按位 OR；设成 `"AND"` 时是按位 AND。所以独热 mux 同时具备「多输入合并」的能力，不局限于严格独热。

---

### 4.2 解复用器（Demultiplexer）

#### 4.2.1 概念说明

demux 是 mux 的反向：1 个输入、N 个输出、1 个选择信号。选择信号决定把输入送到哪一路输出，并给出一个 `valids_out` 向量指明「这次命中了第几路」。

本书的 demux 同样分二进制与独热两种，而且**二进制 demux 直接复用独热 demux**：先用一个「二进制→独热」转换器把选择信号变成独热，再交给独热 demux。这是「构建块库」自底向上复用的又一个范例（和 u5-l1 里 `Word_Reducer` 复用 `Bit_Reducer` 一个套路）。

#### 4.2.2 核心流程

**二进制 demux** 内部两步：

```text
(1) Binary_to_One_Hot: 把 ADDR_WIDTH 位二进制 selector 转成 OUTPUT_COUNT 位独热向量。
(2) Demultiplexer_One_Hot: 用该独热向量把 word_in 送到对应输出，其它输出清零。
```

**独热 demux** 的关键是一个 `BROADCAST`（广播）参数：

- `BROADCAST = 0`（默认）：输入只送到被选中的那一输出，其它输出被 `Annuller` 清零。更安全、更易调试，未选中的下游「看不见」数据。
- `BROADCAST = 1`：把输入原样复制到所有输出（纯连线、零逻辑），靠 `valids_out` 告诉下游「这次该接收的是第几路」，其它路可以「偷听」。

无论哪种，`valids_out` 都直接等于 `selectors`（独热向量本身）。

#### 4.2.3 源码精读

**二进制 demux = 转换器 + 独热 demux**：

[Demultiplexer_Binary.v:60-71](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Demultiplexer_Binary.v#L60-L71) —— 先实例化 `Binary_to_One_Hot`，把二进制 `selector` 转成 `selector_one_hot`。

[Demultiplexer_Binary.v:75-88](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Demultiplexer_Binary.v#L75-L88) —— 再把独热向量交给 `Demultiplexer_One_Hot` 完成实际分发。

**二进制→独热转换器**本身又是 N 个地址译码器：

[Binary_to_One_Hot.v:36-51](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Binary_to_One_Hot.v#L36-L51) —— 对每个可能的输出值 `i`，实例化一个 `Address_Decoder_Behavioural`，其 `base_addr == bound_addr == i`。当输入 `binary_in == i` 时第 `i` 位被置 1。若输入超出输出范围，输出全零（表示「无值」）。这就把「4.3 地址译码」和「4.2 demux」串了起来。

**独热 demux 的两种实现**（`generate if`）：

[Demultiplexer_One_Hot.v:66-68](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Demultiplexer_One_Hot.v#L66-L68) —— `valids_out` 直接等于 `selectors`，告诉下游哪一路被选中。

[Demultiplexer_One_Hot.v:75-104](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Demultiplexer_One_Hot.v#L75-L104) —— 用 `generate if (BROADCAST == 0)` 为每个输出挂一个 `Annuller`（选中时放行、否则清零）；`generate else if (BROADCAST == 1)` 则用 `{OUTPUT_COUNT{word_in}}` 把输入复制到所有输出（零逻辑）。注释提醒：把 `BROADCAST` 设成非 0/1 的值会把输入和输出断开，触发 CAD 严重警告。

> 注释里还有一个工程经验：非广播模式看似多花了 `Annuller` 逻辑，但这些 `Annuller`「几乎一定会消失进下游 LUT 里」，独立综合 demux 看到的面积是高估的。

#### 4.2.4 代码实践

**实践目标**：跟踪「一个二进制地址如何变成一路有效输出」的调用链。

**操作步骤**（源码阅读型）：

1. 从 [Demultiplexer_Binary.v:62-71](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Demultiplexer_Binary.v#L62-L71) 进入 `Binary_to_One_Hot`。
2. 在 [Binary_to_One_Hot.v:36-51](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Binary_to_One_Hot.v#L36-L51) 看到：输入 `binary_in = 3` 时，第 3 个 `Address_Decoder_Behavioural`（`base=bound=3`）的 `hit` 为 1，于是 `one_hot_out[3] = 1`，其余为 0。
3. 回到 [Demultiplexer_One_Hot.v:75-97](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Demultiplexer_One_Hot.v#L75-L97)：只有第 3 路的 `Annuller` 放行，`word_in` 出现在 `words_out` 的第 3 段，其余段为 0；`valids_out = 4'b1000`。

**需要观察的现象**：`selector` 越界（如 4 路 demux 给 `selector=5`）时，没有任何 `Address_Decoder_Behavioural` 命中，`valids_out` 全零、`words_out` 全零——这是安全的「无命中」行为。

**预期结果**：调用链 `Demultiplexer_Binary → Binary_to_One_Hot → Address_Decoder_Behavioural（×N）→ Demultiplexer_One_Hot → Annuller（×N）`。把这条链画出来，你就理解了「为什么 demux 不是一块新逻辑，而是已有构件的组合」。

> 综合后各 `Annuller` 是否真的被吸收进下游，**待本地验证**（依赖下游逻辑形态）。

#### 4.2.5 小练习与答案

**练习 1**：`BROADCAST=1` 时 demux 几乎不花逻辑，为什么本书仍把默认值设成 `BROADCAST=0`？

**参考答案**：非广播更安全、更易调试：未选中的下游物理上拿不到数据，无法「偷听」或误收；仿真里也更容易追踪「数据到底去了哪一路」。多出来的 `Annuller` 通常会被下游吸收，代价没有看上去那么大。

**练习 2**：`Demultiplexer_Binary` 的 `selector` 位宽是 `ADDR_WIDTH`，输出路数是 `OUTPUT_COUNT`。这两者必须满足什么关系？

**参考答案**：通常 `OUTPUT_COUNT <= 2^ADDR_WIDTH`。`Binary_to_One_Hot` 允许输出宽度小于全部可能值，此时落在范围外的 `selector` 值会产生全零输出（「无值」）。

---

### 4.3 地址译码/翻译（Address Decoder / Translator）

#### 4.3.1 概念说明

地址译码器只回答一个二元问题：「地址 `addr` 是否落在 `[base, bound]` 范围内？」输出 1 位 `hit`。它和 demux 的关系是：**把 N 个译码器并起来，它们的 `hit` 输出就构成一个独热向量**——这正是 `Binary_to_One_Hot` 的构造方式（4.2.3 已见）。

本书提供三种译码器，差别在于「范围是否固定」「比较用什么电路」：

- **静态（Static）**：`base/bound` 是参数，范围在精化期固定。
- **行为（Behavioural）**：`base/bound` 是输入端口，可运行时改变；用两次整数比较表达。
- **算术（Arithmetic）**：与行为级功能相同，但强制用两个减法器（`Arithmetic_Predicates_Binary`）实现比较。

地址翻译器（Translator）解决另一个问题：当你把一段**未对齐到 2 的幂**的地址范围映射到硬件时，地址低位（LSB）不会从 0 开始顺序计数，导致「映射顺序」和「物理顺序」错乱。翻译器用一张小 ROM 把这些杂乱的 LSB 重排成连续的 0,1,2,…。

#### 4.3.2 核心流程

**静态译码器**的流程（朴素但有效）：

```text
ADDR_COUNT = bound - base + 1                 // 范围内地址个数
for i in [base, bound]:
    per_addr_match[i-base] = (addr == i)      // 逐地址比较
hit = |per_addr_match                          // OR 归约所有比较结果
```

**行为译码器**用两次比较代替循环：

```text
hit = (addr >= base_addr) && (addr <= bound_addr)
```

\[ \text{hit} \;=\; \mathbb{1}\{\,\text{base} \le \text{addr} \le \text{bound}\,\} \]

**翻译器**的流程：在 `initial` 里按「起始偏移」预填一张查找表，运行时 `output_addr = translation_table[input_addr]`。

#### 4.3.3 源码精读

**静态译码器**——逐地址比较 + OR 归约：

[Address_Decoder_Static.v:42-43](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Address_Decoder_Static.v#L42-L43) —— 用 `localparam` 算出范围内地址个数 `ADDR_COUNT` 和定宽零常量 `COUNT_ZERO`（u2-l2 的复制构造）。

[Address_Decoder_Static.v:58-66](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Address_Decoder_Static.v#L58-L66) —— `for` 循环逐地址比较，存入 `per_addr_match` 向量；第二个 `always` 做 OR 归约得到 `hit`。注意 `i[ADDR_WIDTH-1:0]` 这个切片：为了避免整数 `i` 与输入地址比较时位宽不匹配告警，只取需要的低位（代价是 `ADDR_WIDTH > 32` 时不可靠）。

> 源码注释明确写「**我不推荐这个实现**」：它会让 CAD 工具为最多 \( 2^{\text{ADDR\_WIDTH}} \) 个地址各建一点逻辑、并存进一个最多 \( 2^{\text{ADDR\_WIDTH}} \) 位的向量，精化与优化极慢；超过约 20 位地址基本不可用。作者保留它是因为曾在 CPU 里用它译码寄存器操作数（极小固定范围）。

**行为译码器**——两次比较（全书推荐）：

[Address_Decoder_Behavioural.v:49-56](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Address_Decoder_Behavioural.v#L49-L56) —— 两个中间变量 `base_or_higher`、`bound_or_lower` 分别做 `>=` 和 `<=`，再相与。注释指出不同版本 CAD 会把它综合成减法器或 \( \log_2(\text{ADDR\_WIDTH}) \) 层 LUT 树，哪个更快不一定；但它在 20 位以上地址范围不会撞到向量宽度上限，也不需要漫长的优化。

**算术译码器**——强制两个减法器：

[Address_Decoder_Arithmetic.v:49-99](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Address_Decoder_Arithmetic.v#L49-L99) —— 实例化两个 `Arithmetic_Predicates_Binary`，分别取出 `addr >= base_addr` 和 `addr <= bound_addr` 两个谓词（其余输出端口悬空），最后相与。注释说明：小范围时它比行为级大（两个减法器 vs 几个 LUT），大范围时算术速度可能成为瓶颈。**只有当你必须用算术电路时才选它**。

**地址翻译器**——一张 `ramstyle="logic"` 的小 ROM：

[Address_Translator_Static.v:84-88](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Address_Translator_Static.v#L84-L88) —— 用 `(* ramstyle="logic" *)`（Quartus）和 `(* ram_style="distributed" *)`（Vivado）属性把翻译表强制实现为 LUT 逻辑，否则它可能随机变成块 RAM/异步 LUT RAM，无法被优化进其它逻辑且通常太慢。这两个属性写在源码里，正是 u4-l2 讲的「绑定实现的约束要写进源码」。

[Address_Translator_Static.v:110-122](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Address_Translator_Static.v#L110-L122) —— `initial` 里按起始偏移 `INPUT_ADDR_BASE` 预填表（下标 `j` 从基址的 LSB 起步、每次 `+1` 并对深度取模回绕），把杂乱的 LSB 映射成连续的 0,1,2,…；运行时 `output_addr = translation_table[input_addr]`。

> 注释里给了一个具体例子：地址 7~10 映射到 0~3，原始两位 LSB 序列是 3,0,1,2，需映射成 0,1,2,3。

**三种译码器选型小结**：

| 译码器 | 范围 | 比较电路 | 推荐场景 |
| --- | --- | --- | --- |
| Static | 固定（参数） | 逐地址比较 + OR 归约 | 极小固定范围（如 CPU 寄存器译码）；范围 >20 位不可用 |
| Behavioural | 可变（输入） | 两次整数比较 | **通用首选** |
| Arithmetic | 可变（输入） | 两个减法器 | 必须用算术电路时 |

#### 4.3.4 代码实践（本讲综合实践也以此为基础）

**实践目标**：用 `Address_Decoder_Static` 产生一个独热片选向量，再喂给 `Multiplexer_One_Hot` 回送读数据——把本讲三个最小模块串起来。

**操作步骤**（示例实例化 + 源码阅读）：

1. 设想 4 个外设，各占一个固定地址范围。我们为每个外设实例化一个 `Address_Decoder_Static`，把它们的 `hit` 输出收集成一个 4 位独热向量 `cs_one_hot`。
2. 4 个外设各自给出一个 8 位「读数据」，拼接成 `read_data_in`。
3. 用 `Multiplexer_One_Hot` 以 `cs_one_hot` 为选择信号，把命中外设的读数据回送到 `read_data_out`。

下面是**示例代码**（非项目原有）：

```verilog
// 示例代码：地址 -> 片选 -> 读数据回送
// 4 个外设分别映射到固定地址范围（地址宽度 ADDR_WIDTH=8）
`default_nettype none

module Read_Mux_Example
#(
    parameter ADDR_WIDTH = 8,
    parameter WORD_WIDTH = 8
)
(
    input  wire [ADDR_WIDTH-1:0] addr,
    input  wire [4*WORD_WIDTH-1:0] read_data_in, // 4 个外设的读数据拼接(第0个在最右)
    output wire [WORD_WIDTH-1:0]  read_data_out,
    output wire [3:0]             cs_one_hot
);

    // (1) 每个外设一个静态译码器，得到独热片选
    //     例：外设0=0x10..0x1F, 外设1=0x20..0x2F, ...
    Address_Decoder_Static #(.ADDR_WIDTH(ADDR_WIDTH), .ADDR_BASE(8'h10), .ADDR_BOUND(8'h1F)) dec0 (.addr(addr), .hit(cs_one_hot[0]));
    Address_Decoder_Static #(.ADDR_WIDTH(ADDR_WIDTH), .ADDR_BASE(8'h20), .ADDR_BOUND(8'h2F)) dec1 (.addr(addr), .hit(cs_one_hot[1]));
    Address_Decoder_Static #(.ADDR_WIDTH(ADDR_WIDTH), .ADDR_BASE(8'h30), .ADDR_BOUND(8'h3F)) dec2 (.addr(addr), .hit(cs_one_hot[2]));
    Address_Decoder_Static #(.ADDR_WIDTH(ADDR_WIDTH), .ADDR_BASE(8'h40), .ADDR_BOUND(8'h4F)) dec3 (.addr(addr), .hit(cs_one_hot[3]));

    // (2) 用独热片选作为 mux 的选择信号，回送命中外设的读数据
    Multiplexer_One_Hot
    #(
        .WORD_WIDTH (WORD_WIDTH),
        .WORD_COUNT (4),
        .OPERATION  ("OR")           // 严格独热时即"选一"
    )
    read_mux
    (
        .selectors (cs_one_hot),
        .words_in  (read_data_in),
        .word_out  (read_data_out)
    );

endmodule

`default_nettype wire
```

**需要观察的现象**：当 `addr=8'h25` 时，只有 `dec1` 命中，`cs_one_hot=4'b0010`，`read_data_out` 等于 `read_data_in` 的第 1 段；当 `addr` 不落在任何范围时，`cs_one_hot=4'b0000`，`Multiplexer_One_Hot` 的 OR 归约结果为全零。

**预期结果**：读数据严格跟随命中外设。综合后，4 个静态译码器各自只覆盖 16 个地址（很小），是 `Address_Decoder_Static` 的合理用武之地。

> 上述综合面积与时序**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么这个「片选 + 回读」结构里，mux 选独热而不是二进制？

**参考答案**：因为片选信号**天然就是独热**——一次访问恰好命中一个外设，N 个译码器的 `hit` 输出本身就是 N 位独热向量。把它直接当 `Multiplexer_One_Hot` 的 `selectors`，无需任何转换。若改用二进制 mux，反而要先把独热「压缩」回二进制（一次编码），再让 mux 内部「译码」回独热，多此一举。这正是「选择信号本来就是什么编码，就用什么 mux」的选型原则。

**练习 2**：如果外设数量增加到 32 个，且每个范围是运行时可改的，你会换用哪个译码器？

**参考答案**：换成 `Address_Decoder_Behavioural`（范围可变、可扩展到 20 位以上），而不是 `Address_Decoder_Static`（32 个范围会让静态译码器的逐地址比较逻辑爆炸）。

**练习 3**：`Address_Decoder_Static` 里为什么用 `i[ADDR_WIDTH-1:0]` 而不是直接用 `i` 去和 `addr` 比较？

**参考答案**：`i` 是 32 位整数，`addr` 是 `ADDR_WIDTH` 位，直接比较会触发位宽不匹配告警；只取 `i` 的低 `ADDR_WIDTH` 位就位宽匹配了。代价是当 `ADDR_WIDTH > 32` 时取位不完整，所以源码注释说该译码器对超过 32 位的范围不可靠。

---

## 5. 综合实践

把本讲三个最小模块串成一个最小「内存映射读通道」：

1. **地址译码**：用 4 个 `Address_Decoder_Static`（或运行时可改时用 `Address_Decoder_Behavioural`）把 8 位 `addr` 译成 4 位独热 `cs_one_hot`（见 4.3.4 示例）。
2. **数据回送**：把 4 个外设的读数据拼进 `Multiplexer_One_Hot` 的 `words_in`，以 `cs_one_hot` 为 `selectors`，得到 `read_data_out`。
3. **分发写数据（选做）**：再实例化一个 `Demultiplexer_Binary`，把写数据按地址分发到对应外设，观察它的 `valids_out` 与 `cs_one_hot` 是否一致（注意 `Demultiplexer_Binary` 内部也是先转独热，所以两者会自然吻合）。

完成后请回答：

- 整条读通路上，地址信号经过了哪些构件？分别用了 binary 还是 one-hot 编码？
- 如果把 `Multiplexer_One_Hot` 换成 `Multiplexer_Binary_Behavioural`，你需要在它前面插一个什么转换？这会带来什么代价？
- 如果某个外设的地址范围不是 2 的幂对齐（例如 0x17~0x1A），直接用 `cs_one_hot` 作为 RAM 下标会有什么问题？这时该请出哪个构件？（提示：`Address_Translator_Static`。）

> 写通路与未对齐范围下 RAM 下标的完整综合结果**待本地验证**。

## 6. 本讲小结

- **mux** 有两种实现：二进制 mux 用一行部分位选 `(selector*WORD_WIDTH) +: WORD_WIDTH`，独热 mux 用「`Annuller` 数组 + `Word_Reducer`」，后者还能把输出函数参数化为 OR/AND/NOR/…。
- **mux = 只读存储**：把可能输出值当输入、用选择信号寻址，即可表达任意小型布尔/查找函数。
- 二进制 mux 选择信号线少但译码深；独热 mux 线多但每路独立、极浅，且当选择信号天然独热时零转换成本。
- **demux** 是 mux 的反向；二进制 demux = `Binary_to_One_Hot` + `Demultiplexer_One_Hot`，`BROADCAST` 控制是「仅命中路」还是「全广播」。
- **地址译码器**输出 1 位 `hit`；把 N 个译码器并起来就是独热向量（这正是 `Binary_to_One_Hot` 的构造）。Static 固定范围、Behavioural 可变（首选）、Arithmetic 强制算术。
- **地址翻译器**用一张 `ramstyle="logic"` 的 ROM 把未对齐范围的 LSB 重排成连续下标，配合译码器用于内存映射。
- 选型总原则：**选择信号本来就是什么编码，就用什么 mux/demux**；译码器按「范围是否固定、是否要算术、范围多大」三问来选。

## 7. 下一步学习建议

本讲把「选择/路由」族构件讲完了，下一讲 u5-l3（Dyadic/Triadic 参数化布尔运算器）会用「运算表参数化」的思路实现多种布尔函数——你会看到它与独热 mux 的「把输出值当输入」思想遥相呼应。之后进入 u6（寄存器与流水线寄存器），把本讲的组合 mux/demux 与时序元件结合，构成真正的数据通路。

建议继续阅读的源码：

- [Multiplexer_Binary_Structural.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Multiplexer_Binary_Structural.v)（结构化、可移植、输出函数可扩展的版本）；
- [Address_Translator_Arithmetic.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Address_Translator_Arithmetic.v)（可运行时改变范围的翻译器）；
- [Annuller.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Annuller.v) 与 [Word_Reducer.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Word_Reducer.v)（本讲被反复复用的两个 u5-l1 构件，值得再读一遍）。
