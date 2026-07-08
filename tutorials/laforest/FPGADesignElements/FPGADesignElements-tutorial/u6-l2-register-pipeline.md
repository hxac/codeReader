# 寄存器流水线

## 1. 本讲目标

上一讲 [u6-l1 Register 家族](./u6-l1-register-family.md) 把「单个寄存器」讲透了。本讲把一串寄存器**首尾相连**，做成「流水线」（pipeline）。学完后你应该能够：

- 说清 `Register_Pipeline` 的内部结构——**每一级就是一个 2:1 多路选择器 + 一个 `Register`**，并解释为什么这样搭。
- 区分三个变体的能力边界：哪个支持**并行加载**？哪个支持 **0 深度**？哪个支持**运行时改变延迟**？
- 实例化一个固定深度的流水线，并**算出它的延迟周期数**。
- 解释 `Register_Pipeline_Variable` 为什么放弃通用 RTL、改用 **Xilinx SRL 原语**，以及它的延迟如何在运行时被 `tap_number` 改变。

> 说明：本讲**不重复** `Register` 模块本身的时序、复位、「最后赋值胜出」等内容（已在 u6-l1、u3-l2 讲透），而是聚焦在「如何把多个 `Register` 串成链、链的深度谁来决定」。

## 2. 前置知识

阅读本讲前，你最好已经掌握（来自更早的讲义）：

- **参数化与位宽**（u2-l2）：`parameter` 默认为 `0` 的「吵闹失败」护栏、`localparam` 派生常量、`{N{1'b0}}` 复制构造定宽常量、`clog2` 计算地址位宽。
- **赋值风格**（u3-l1）：组合块用阻塞 `=`、`generate for` 在精化期展开。
- **多路选择器**（u5-l2）：`Multiplexer_Binary_Behavioural` 用变址部分位选 `words_in[(selector*WORD_WIDTH) +: WORD_WIDTH]` 实现 N 选 1，`selector=0` 选拼接里最右（最低位）的那个字。
- **Register 家族**（u6-l1）：`Register` 的 `clock_enable`/`clear`/`RESET_VALUE` 接口，以及「在基座之上再加一层」的复用范式。

三个本讲会反复用到的小结论，先放在这里：

1. **延迟周期数 = 流水线深度**。数据从 `pipe_in` 进入第 0 级，每拍前进一步，要经过 `PIPE_DEPTH` 拍才从 `pipe_out` 冒出来。
2. **本书约定「移位方向是 LSB → MSB」**：`pipe_in` 喂第 0 级（最低位端），`pipe_out` 读最后一级（最高位端）。
3. **`generate` 在精化期（elaboration）展开**，循环次数必须是编译期常量；所以「深度」若想运行时改变，就不能靠增减 `Register` 实例，而要换一种结构（见 4.3）。

## 3. 本讲源码地图

本讲涉及三个文件，恰好是「全功能 → 极简 → 可变深度」的递进关系：

| 文件 | 角色 | 一句话作用 |
| --- | --- | --- |
| [Register_Pipeline.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Pipeline.v) | 全功能 | 串行 + 并行 I/O、可并行加载、可设每级初值；深度 ≥ 1。 |
| [Register_Pipeline_Simple.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Pipeline_Simple.v) | 极简 | 只有串行 I/O、复位恒为 0；**支持深度 0**（直通）。 |
| [Register_Pipeline_Variable.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Pipeline_Variable.v) | 可变深度 | 延迟深度由 `tap_number` 在**运行时**选择；用 Xilinx SRL 实现。 |

三个文件都建立在 [Register.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v) 之上——前两个直接实例化一串 `Register`，第三个则改走原语路线。阅读时请把这三个文件并排打开，对比它们的**端口列表**与**实例化方式**。

## 4. 核心概念与源码讲解

### 4.1 Register_Pipeline（全功能）

#### 4.1.1 概念说明

`Register_Pipeline` 把 `PIPE_DEPTH` 个 `Register` 串成一条链，让数据字一拍一拍地往后挪。它解决的问题是——**把「延迟一串数据」这件反复出现的事，连同它的并行加载、初值设置，封装成一个有名字的模块**。

源码注释列出了它的两种典型用法：

- **延迟流水线（delay pipeline）**：把 `WORD_WIDTH` 设成数据字宽、`PIPE_DEPTH` 设成延迟级数，于是整字整字地往后搬。比如让某路数据比另一路晚 4 拍到达，对齐流水线。
- **移位寄存器（shift register）**：把 `WORD_WIDTH` 设成 1、`PIPE_DEPTH` 设成要移位的数据位宽，于是逐位串入/串出。可以从 `parallel_in` 一次载入整个字，再从 `pipe_out` 逐位移出；或反过来从 `pipe_in` 移入、从 `parallel_out` 一次读出整字。

它比下面两个变体「重」的地方在于：

1. **同时有串行口（`pipe_in`/`pipe_out`）和并行口（`parallel_in`/`parallel_out`）**。
2. 有一个 `parallel_load` 控制信号：为 1 时**整条链同时载入 `parallel_in`**，而不是移位。注释明确：**Load overrides shift（加载压倒移位）**。
3. 有一个 `RESET_VALUES` 参数，给每一级不同的上电初值/清零值。

代价是：**`PIPE_DEPTH` 必须 ≥ 1**。源码注释解释了为什么不做深度 0——那样会让 `parallel_in`/`parallel_out` 端口悬空，引发 CAD 警告，且代码会变得很乱。需要深度 0 时请用 4.2 的 Simple 版本。

#### 4.1.2 核心流程

每一级流水线的结构是「**一个 2:1 选择器 + 一个 `Register`**」：

```text
                 parallel_load=1 ? parallel_in[i] : (上一级输出)
                              │
  pipe_in/上一级 ──► [ 2:1 mux ]──► data_in ──► [ Register ]──► 本级输出
                                                （clock_enable/clear 透传）
```

每拍 `clock_enable` 有效时，每个 `Register` 载入其 `data_in`——也就是「上一级的输出」或「并行加载值」，于是整条链的数据集体后移一位。`parallel_load` 是**全局**的：要么全链移位，要么全链并行载入。

第 0 级与后续级的唯一区别，是 2:1 选择器的「移位输入」来源不同：

```text
第 0 级：选择器输入 = { parallel_in[0], pipe_in }     // 移位来源是外部 pipe_in
第 i 级：选择器输入 = { parallel_in[i], stage_out[i-1] } // 移位来源是上一级
最后：   pipe_out = stage_out[PIPE_DEPTH-1]            // 串行输出来自最后一级
```

延迟周期数：

\[ T_{\text{delay}} = \text{PIPE\_DEPTH} \text{（个时钟周期）} \]

即一个字从 `pipe_in` 到 `pipe_out` 要走 `PIPE_DEPTH` 拍。

关于「为什么是 mux + Register，而不是给 Register 加个 load 端口」——源码用两条综合属性回答了：FPGA 的触发器通常自带**独立的数据端和同步加载端**，那个 2:1 选择器会被综合器**免费吸收进触发器**（不额外占 LUT）。所以这里的 mux 几乎是「零成本」的，却换来了清晰的「移位/加载」分离。

#### 4.1.3 源码精读

模块头部遵循本书约定（`default_nettype none`、参数默认 `0`），并多了一个**派生参数** `TOTAL_WIDTH = WORD_WIDTH * PIPE_DEPTH`，用作并行口的整总线宽度：

[Register_Pipeline.v:47-66](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Pipeline.v#L47-L66) —— 模块声明与端口：注意 `TOTAL_WIDTH` 标注「Don't set at instantiation」，它是算出来的；`parallel_out` 与 `pipe_out` 都是 `output reg`（来自本模块的 `always` 块）。

[Register_Pipeline.v:78-79](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Pipeline.v#L78-L79) —— 为每一级声明一对内部线网数组 `pipe_stage_in`/`pipe_stage_out`，把级与级之间的连线显式化。

[Register_Pipeline.v:82-88](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Pipeline.v#L82-L88) —— 关键注释 + 两条属性 `(* multstyle = "logic" *)`（Quartus）/`(* use_dsp = "no" *)`（Vivado）：**禁止把选择器塞进 DSP 块**。注释说这里用 DSP 很糟糕，因为触发器的同步加载端已经白送了一个 2:1 mux。

[Register_Pipeline.v:94-119](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Pipeline.v#L94-L119) —— **第 0 级**被从循环里「剥」出来单独写，避免循环里出现 `i-1 = -1` 的越界下标。选择器把 `{parallel_in[0 +: WORD_WIDTH], pipe_in}` 接到 `words_in`、`parallel_load` 接到 `selector`：因为 u5-l2 里 mux 的 `selector=0` 选拼接最右元素，所以 `parallel_load=0` 选 `pipe_in`（移位）、`=1` 选 `parallel_in`（加载）——这正是「Load overrides shift」。

[Register_Pipeline.v:129-172](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Pipeline.v#L129-L172) —— `generate for` 从 `i=1` 开始铺出剩余各级，每级同样是「mux + `Register`」，移位输入接上一级 `pipe_stage_out[i-1]`，并行初值取 `RESET_VALUES[WORD_WIDTH*i +: WORD_WIDTH]`。

[Register_Pipeline.v:174-178](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Pipeline.v#L174-L178) —— 最后把最后一级输出接到串行输出 `pipe_out`。

关于 `RESET_VALUES` 的字节序：源码注释（38-42 行）说明它是「各级初值的拼接，**最右端是第 0 级（LSB）**」。所以 `RESET_VALUES[0 +: WORD_WIDTH]` 给第 0 级，依此类推——与 `parallel_out` 的布局完全对齐。

#### 4.1.4 代码实践

**实践目标**：实例化一个 4 级 `Register_Pipeline`，把一个标记字送进 `pipe_in`，验证它**恰好经过 4 个时钟周期**后从 `pipe_out` 出现——亲手确认「延迟 = PIPE_DEPTH」。

**操作步骤**：

1. 写一个最小顶层和测试桩（**示例代码**，非项目原文件）：

   ```verilog
   `default_nettype none
   module tb_pipe;
       reg clock = 1'b0;
       always #5 clock = ~clock;          // 10ns 周期，仅仿真

       wire [7:0] pipe_out;
       reg  [7:0] pipe_in  = 8'h00;
       reg         load    = 1'b0;
       reg  [31:0] parallel_in = 32'h0;

       Register_Pipeline #(
           .WORD_WIDTH(8),
           .PIPE_DEPTH(4)                    // 4 级 → 4 拍延迟
       ) dut (
           .clock         (clock),
           .clock_enable  (1'b1),            // 每拍都移位
           .clear         (1'b0),
           .parallel_load (load),
           .parallel_in   (parallel_in),
           .parallel_out  (),
           .pipe_in       (pipe_in),
           .pipe_out      (pipe_out)
       );

       initial begin
           // 第 0 拍送入标记字 8'hAA
           @(posedge clock); pipe_in = 8'hAA;
           @(posedge clock); pipe_in = 8'h00;  // 之后输入清零，便于观察那一个字
       end
   endmodule
   ```

2. 数拍：从送入 `8'hAA` 的那一拍起，第 1/2/3 拍 `pipe_out` 应仍是 0，**第 4 拍**才出现 `8'hAA`。

**需要观察的现象**：

- `pipe_out` 在第 4 个 `posedge clock` 之后变为 `8'hAA`，再过一拍变回 0（因为后续输入是 0）。
- 若把 `.PIPE_DEPTH(4)` 改成 `2`，`8'hAA` 会提前到第 2 拍出现。

**预期结果**：延迟周期数 = `PIPE_DEPTH`，与 4.1.2 的公式一致。

> 待本地验证：本项目 `tests/` 目前只有 `Counter_Gray` 的 cocotb 用例，没有流水线现成测试台。可用 Icarus Verilog（`iverilog -g2001`）跑上面的最小 tb，或在掌握 u18-l2 后改写成 cocotb 自检。

#### 4.1.5 小练习与答案

**练习 1**：源码为什么把第 0 级从 `generate for` 循环里「剥」出来单独写？

**答案**：因为循环体里每一级的「移位输入」是上一级的输出 `pipe_stage_out[i-1]`。如果循环从 `i=0` 开始，就会出现 `pipe_stage_out[-1]` 这个越界下标。把第 0 级单独写、让它直接接外部的 `pipe_in`，就避开了 `-1`。这是处理「链头特例」的标准套路——4.2 的 Simple 版本用了同样的技巧。

**练习 2**：如果我把 `parallel_load` 恒接 `1'b1`、并把 `pipe_in` 悬空，这个模块退化成什么？

**答案**：退化成「**一排独立的寄存器**」（a bank of registers）。因为每级选择器永远选 `parallel_in[i]`，链式移位被切断，每个 `Register` 各自载入自己的并行字。源码注释（35-36 行）正是这么说：把 `parallel_load` 恒接 1、`pipe_in` 悬空，就得到一个「打包好的寄存器堆」。反之把 `parallel_load` 恒接 0，选择器会被优化掉，得到纯移位寄存器（但此时更推荐用 Simple 版本）。

---

### 4.2 Register_Pipeline_Simple（极简，支持深度 0）

#### 4.2.1 概念说明

`Register_Pipeline_Simple` 是 `Register_Pipeline` 的「瘦身版」：**砍掉并行加载、砍掉非零初值**，换来一个更干净、且**支持 `PIPE_DEPTH = 0`** 的流水线。

它解决一个前者解决不了的问题——**「我可能一拍都不想延迟」**。当 `PIPE_DEPTH = 0` 时，它把 `pipe_out` 直接接线到 `pipe_in`，**不推断任何逻辑**。而 4.1 的全功能版本做不到这一点（并行口会悬空、报警告）。

源码注释给的典型用途是「**流水线对齐**」和「**给输入加几级寄存器，让综合器能把它们重定时（retime）到后面的组合逻辑里，从而跑更高频率**」——后者正是 u6-l1 反复强调「`Register` 刻意不带异步复位以保留重定时能力」的延续。

一个值得注意的反常细节：它的 `PIPE_DEPTH` 默认值是 **`-1`**，而不是本书处处可见的 `0`。

#### 4.2.2 核心流程

因为没有了并行加载，每一级就不再需要 2:1 选择器，结构退化成最朴素的「`Register` 链」：

```text
pipe_in ──► [Reg 0] ──► [Reg 1] ──► ... ──► [Reg PIPE_DEPTH-1] ──► pipe_out
```

`generate` 里用 `if/else if` 处理三种深度：

```text
if (PIPE_DEPTH == 0)     pipe_out = pipe_in            // 组合直通，0 逻辑
else if (PIPE_DEPTH > 0) 串起 PIPE_DEPTH 个 Register    // 正常流水线
// （PIPE_DEPTH 为 -1 等非正值时，两个分支都不命中 → 模块空转，吵闹地暴露未设参数）
```

延迟周期数仍是 `PIPE_DEPTH`（深度 0 时为 0，即不延迟）。

**关于默认值 `-1` 的妙处**：本书的护栏惯例是「参数默认 `0` → 忘设就崩」。但在这里，`0` 是一个**合法且有意义**的取值（直通）！如果默认仍是 `0`，忘设参数的人会静默得到一个「不延迟」的设计，极难发现。于是作者把默认值改成 `-1`——一个既不等于 0、也不大于 0 的值，让两个 `generate` 分支都不命中、模块空转，从而**吵闹地**提醒你「忘设 `PIPE_DEPTH` 了」。这是「吵闹失败」护栏**根据上下文自适应**的精彩样本：当 `0` 不再能当哨兵时，就换一个能当哨兵的值。

#### 4.2.3 源码精读

[Register_Pipeline_Simple.v:20-34](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Pipeline_Simple.v#L20-L34) —— 模块声明：注意第 23 行 `parameter PIPE_DEPTH = -1`，以及端口只有 `pipe_in`/`pipe_out`，没有并行口。`clock`/`clock_enable`/`clear` 在深度 0 时会变成无用输入，所以用 `verilator lint_off UNUSED` 把告警关掉（26-31 行）。

[Register_Pipeline_Simple.v:42-48](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Pipeline_Simple.v#L42-L48) —— 深度 0 分支：一个纯组合 `always @(*)` 把 `pipe_in` 接到 `pipe_out`，综合后只剩一根线。

[Register_Pipeline_Simple.v:49-95](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Pipeline_Simple.v#L49-L95) —— 深度 > 0 分支：与 4.1 完全同构——第 0 级（`input_stage`，57-69 行）单独剥出接 `pipe_in`，`for(i=1...)` 铺出剩余各级（75-89 行），最后接 `pipe_out`（93-95 行）。区别仅在于：没有选择器、`RESET_VALUE` 恒为 `WORD_ZERO`。

#### 4.2.4 代码实践

**实践目标**：通过对比源码结构，理解「Simple 与全功能版本的差别只在于少了一层选择器」，并验证深度 0 的直通行为。

**操作步骤（源码阅读型 + 实例化对比）**：

1. 并排打开 [Register_Pipeline.v:94-119](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Pipeline.v#L94-L119)（全功能第 0 级）与 [Register_Pipeline_Simple.v:57-69](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Pipeline_Simple.v#L57-L69)（Simple 第 0 级）。注意后者直接把 `pipe_in` 接到 `Register` 的 `.data_in`，中间没有 `Multiplexer_Binary_Behavioural`。
2. 实例化一个深度 0 的 Simple 流水线（**示例代码**）：

   ```verilog
   Register_Pipeline_Simple #(
       .WORD_WIDTH(8),
       .PIPE_DEPTH(0)        // 直通：不延迟
   ) pass_through (
       .clock        (clock),
       .clock_enable (1'b1),
       .clear        (1'b0),
       .pipe_in      (data_in),
       .pipe_out     (data_out)   // 期望：data_out 组合地等于 data_in
   );
   ```

**需要观察的现象**：

- 深度 0 时，`data_out` 在**同一拍**（组合地）跟随 `data_in` 变化，不等待时钟沿。
- 把 `.PIPE_DEPTH(0)` 改成 `3`，`data_out` 就变成 3 拍延迟。
- 把 `.PIPE_DEPTH` 这一行整个删掉（用默认 `-1`），综合/精化应当报错或告警「输出未驱动」——这正是哨兵值在提醒你。

**预期结果**：能说清「Simple = 全功能版本去掉并行加载与初值，换来支持深度 0；当不需要并行口时，用 Simple 更省心」。

> 待本地验证：深度 0 的「组合直通」可用 `iverilog` 仿真确认 `data_out` 与 `data_in` 同相；删参数时的告警形态取决于你用的综合器。

#### 4.2.5 小练习与答案

**练习 1**：既然深度 0 就是「把 `pipe_out` 接到 `pipe_in`」，我直接在顶层写一行 `assign pipe_out = pipe_in;` 不就行了，为什么要用这个模块？

**答案**：如果你的设计**确定永远**是 0 拍延迟，直接连线当然可以。但 `Register_Pipeline_Simple` 的价值在于「**深度是个参数**」：今天设 0，明天改成 3，只需改一处参数，连线和实例都不动。直接 `assign` 做不到这种参数化。此外，把延迟做成模块，意图在原理图里一目了然（「这里故意延迟 N 拍」），而不是埋在一根普通连线里。

**练习 2**：为什么 Simple 版本不设 `RESET_VALUES` 参数（像全功能版本那样给每级不同初值）？

**答案**：因为它定位是「极简」。所有级都用同一个 `WORD_ZERO`（[Register_Pipeline_Simple.v:60](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Pipeline_Simple.v#L60) 与 79 行）。如果你需要每级不同的初值或并行加载，那就回到了 4.1 的全功能版本——两个变体各管一段，互不抢戏。这是「极致模块化、按需选块」思想的体现（u4-l1）。

---

### 4.3 Register_Pipeline_Variable（运行时可变延迟）

#### 4.3.1 概念说明

前两个变体的深度都是**编译期参数**——一旦综合完，延迟就固定了。`Register_Pipeline_Variable` 解决一个更难的问题：**让延迟深度在运行时（runtime）可变**。

它通过一个 `tap_number` 输入选择「从哪一级把数据抽出来」。比如一条 32 级的链，你可以在运行中决定「这次我想要 5 拍延迟」或「这次想要 27 拍延迟」。源码注释点出它的代价与边界：

- **`PIPE_DEPTH` 只能是 16 或 32**——因为底层用的是 AMD/Xilinx 的 **SRL（Shift Register LUT）原语** `SRL16E`/`SRLC32E`，这两种原语的最大深度正好是 16/32。
- **`tap_number` 从 0 开始**：`0` 选第 1 级的输出，`PIPE_DEPTH-1` 选最后一级的输出。**无法直接选输入**，所以最小延迟是 1 拍。
- 改 `tap_number` 会**立刻**改抽头，开始输出新位置上的数据——**可能跳过某些数据，也可能重读旧数据**。

为什么不像前两个变体那样用一串 `Register` 加一个 `Multiplexer` 来实现？源码注释说得很直白：那样做**面积代价极大**。SRL 的妙处在于：**一个 LUT 就能当 16 或 32 级的移位寄存器用**，比同等深度的触发器链省得多。这是全书少数几处「为面积放弃通用 RTL、改用厂商原语」的地方——注释也注明「移植到其他 FPGA 家族应当很容易」。

#### 4.3.2 核心流程

实现分两步：

```text
① 用一个 Register 把 tap_number 存起来（tap_number_load 脉冲时载入新值）
② 对数据的每一位，各实例化一个 SRL 原语：
      - D   = input_data[i]        （串入）
      - Q   = output_data[i]       （抽出）
      - A   = tap_number_current   （所有位共用同一个抽头地址）
      - CE  = shift_data           （移位使能）
      - CLK = clock
```

因为 SRL 是**按位**的原语，所以对 `WORD_WIDTH` 位的字，要并行铺 `WORD_WIDTH` 个 SRL，它们共用同一个 `tap_number_current` 当地址——于是整字同步延迟同样的深度。

延迟周期数与 `tap_number` 的关系：

\[ T_{\text{delay}} = \text{tap\_number} + 1 \]

`tap_number = 0` → 读第 1 级 → 延迟 1 拍；`tap_number = PIPE_DEPTH-1` → 读最后一级 → 延迟 `PIPE_DEPTH` 拍（即注释所说「最大 PIPE_DEPTH 拍」）。改变 `tap_number` 的方法：**给 `tap_number_load` 一个脉冲**，把新的 `tap_number` 载入内部寄存器；此后的数据就按新延迟输出。

> 注意「立刻换抽头」的副作用：如果你在数据流到第 5 级时把抽头从 2 改到 10，输出会马上跳去读第 11 级——那里的旧数据会冒出来，而原来第 3 级里正在路上的数据被跳过了。所以变深度适合「不要求每拍都连续」的场景（如可变延迟线、对齐搜索），不适合要求严格无损吞吐的流式数据通路。

#### 4.3.3 源码精读

[Register_Pipeline_Variable.v:27-45](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Pipeline_Variable.v#L27-L45) —— 模块声明：`PIPE_DEPTH` 注释为「16 or 32 only」；派生参数 `ADDR_WIDTH = clog2(PIPE_DEPTH)`（第 33 行）——16/32 分别对应 4/5 位地址，正好喂给 SRL 的地址引脚。

[Register_Pipeline_Variable.v:47](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Pipeline_Variable.v#L47) —— 在模块体内 `` `include "clog2_function.vh" `` 才能用 `clog2`（u2-l2 的惯用法：函数文件用 `` `ifndef `` 包成幂等，可在体内包含）。

[Register_Pipeline_Variable.v:55-67](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Pipeline_Variable.v#L55-L67) —— 第 ① 步：复用一个 `Register`（又是基座复用！）把 `tap_number` 存起来，`clock_enable` 接 `tap_number_load`——只有载入脉冲那一拍才改地址。`clear` 把它清零。

[Register_Pipeline_Variable.v:75-119](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register_Pipeline_Variable.v#L75-L119) —— 第 ② 步：`generate for(i=0; i < WORD_WIDTH; i=i+1)` 对每一位实例化一个 SRL。`PIPE_DEPTH==32` 时用 `SRLC32E`（5 位地址 `A`，80-96 行）；`PIPE_DEPTH==16` 时用 `SRL16E`（4 位地址 `A0..A3`，97-115 行）。所有 SRL 的地址端都接同一个 `tap_number_current`，`CE` 都接 `shift_data`，从而整字同步。

#### 4.3.4 代码实践

**实践目标**：实例化一个 `Register_Pipeline_Variable`，先设 `tap_number=0` 观察最小延迟，再脉冲 `tap_number_load` 把抽头改大，观察「延迟在运行时改变」。

**操作步骤**：

1. 写最小顶层（**示例代码**，需在 Xilinx 工具链下综合/仿真，因为用到 SRL 原语）：

   ```verilog
   `default_nettype none
   module tb_var_pipe;
       reg clock = 1'b0;
       always #5 clock = ~clock;

       reg         shift_data      = 1'b1;
       reg  [7:0]  input_data      = 8'h00;
       wire [7:0]  output_data;
       reg         tap_number_load = 1'b0;
       reg  [4:0]  tap_number      = 5'd0;     // 先选第 1 级（延迟 1 拍）

       Register_Pipeline_Variable #(
           .WORD_WIDTH(8),
           .PIPE_DEPTH(32)
       ) dut (
           .clock           (clock),
           .clear           (1'b0),
           .tap_number_load (tap_number_load),
           .tap_number      (tap_number),
           .shift_data      (shift_data),
           .input_data      (input_data),
           .output_data     (output_data)
       );

       initial begin
           @(posedge clock); input_data = 8'hAA;
           @(posedge clock); input_data = 8'h00;
           // 稍后改延迟到 10 拍
           repeat(5) @(posedge clock);
           tap_number = 5'd9;                  // 选第 10 级 → 延迟 10 拍
           @(posedge clock); tap_number_load = 1'b1;
           @(posedge clock); tap_number_load = 1'b0;
       end
   endmodule
   ```

2. 观察两段：起初 `output_data` 比 `input_data` 晚 1 拍；载入新 `tap_number=9` 后，后续数据晚 10 拍出现。

**需要观察的现象**：

- 初始（`tap_number=0`）：`8'hAA` 在输入后**下一拍**出现在 `output_data`。
- 载入 `tap_number=9` 后：新送入的数据要等**10 拍**才出现。
- 改抽头的那一瞬，`output_data` 可能冒出之前残留在新抽头位置的旧值——这就是注释说的「可能跳过/重读数据」。

**预期结果**：延迟随 `tap_number` 改变，关系满足 \( T_{\text{delay}} = \text{tap\_number}+1 \)。

> 待本地验证：SRL 原语是 Xilinx 专有，需在 Vivado/XSim 或带 Xilinx 仿真库的环境下跑；纯 Icarus 可能不识别 `SRLC32E`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Register_Pipeline_Variable` 要「按位」实例化 SRL（循环 `i < WORD_WIDTH`），而不是实例化一个「整字 SRL」？

**答案**：因为 `SRL16E`/`SRLC32E` 是**1 位**的原语——一个 LUT 只搬 1 条移位线。要搬 `WORD_WIDTH` 位的整字，就得并行铺 `WORD_WIDTH` 个 SRL，每个搬一位，共用同一个 `tap_number_current` 地址。这正是源码 `generate for` 按位展开的原因。代价是位宽越宽，SRL 数量线性增长——但仍远比 `WORD_WIDTH × PIPE_DEPTH` 个触发器省。

**练习 2**：源码注释说「无法直接选输入」（最小延迟 1 拍）。结合 SRL 的结构，为什么做不到 0 拍延迟？

**答案**：SRL 的地址 `A` 选择的是「移位寄存器内部第 N 级」的输出，数据必须先被时钟搬进 SRL 才能被地址读到——即最少经过 1 个时钟沿。`tap_number=0` 读的是「第 1 级」而非「输入端」，所以最小延迟是 1 拍，做不到 4.2 那种「深度 0 直通」。这是用 SRL 换面积的固有代价。

---

## 5. 综合实践

把三个变体串起来，做一次「**选型 + 拼装**」的小设计。

**任务背景**：你有一个数据处理通路，输入是 `data_in`（8 位），需要满足三个互相独立的延迟需求：

1. 主数据通路要把 `data_in` **延迟恰好 3 拍**，与另一路并行的计算结果对齐。
2. 有一条配置信号 `cfg_pipe`，软件有时要求它**延迟 0 拍**（直通）、有时要求延迟 **2 拍**，且这个选择在编译期就能定。
3. 有一条调试用的观测信号，要求**在运行时**由寄存器配置，在 **1 到 16 拍**之间动态调整延迟。

**要求**：

1. 三条延迟分别选哪个变体？说出理由（提示：是否需要并行加载？深度是否要在运行时变？是否可能为 0？）。
2. 写出三条实例化的骨架（**示例代码**），标出各自的 `PIPE_DEPTH` 来源。
3. 指出第 3 条「运行时可变」的实现为何受限于 16 或 32、且最小延迟为何是 1 而不是 0。

**参考思路**：

- 第 1 条：用 `Register_Pipeline_Simple`（不需要并行加载、固定深度）。`PIPE_DEPTH=3`。
- 第 2 条：也用 `Register_Pipeline_Simple`，`PIPE_DEPTH` 设成参数（0 或 2），因为它支持深度 0 的直通；用全功能版本会浪费并行口。
- 第 3 条：用 `Register_Pipeline_Variable`，`PIPE_DEPTH=16`，`tap_number` 接一个配置寄存器（可用 `Register` 存），`tap_number_load` 在配置写入时脉冲。
- 受限原因：`Register_Pipeline_Variable` 底层是 Xilinx SRL16E/SRLC32E 原语，最大深度就是 16/32；SRL 的地址读的是「内部第 N 级」，数据至少要被搬进一级才能被读，故最小延迟 1 拍。

**验收**：能说清「固定深度且无并行需求 → Simple；需要并行加载/每级初值 → 全功能；运行时可变 → Variable（受 SRL 限制）」——这就把三个最小模块的选型逻辑真正串起来了。

> 待本地验证：完整综合需在 Vivado 下进行（第 3 条用到 SRL）；前两条可在任意 Verilog-2001 仿真器下用最小 tb 验证延迟拍数。

## 6. 本讲小结

- `Register_Pipeline` 是全功能流水线：**每级 = 2:1 选择器 + `Register`**，同时支持串行 `pipe_in`/`pipe_out` 与并行 `parallel_in`/`parallel_out`；`parallel_load` 为 1 时整链并行加载（**Load overrides shift**）；`PIPE_DEPTH ≥ 1`。
- 那个 2:1 选择器会被综合器**免费吸收进触发器的同步加载端**，所以源码用 `multstyle="logic"`/`use_dsp="no"` 属性**禁止塞进 DSP**。
- `Register_Pipeline_Simple` 砍掉并行加载与初值，换来**支持 `PIPE_DEPTH = 0`**（组合直通、0 逻辑）；其 `PIPE_DEPTH` 默认 **`-1`** 而非 `0`——因为 `0` 在这里是合法取值，护栏只好换 `-1` 当哨兵。
- 三个变体的延迟都是 `PIPE_DEPTH` 拍（Simple 在深度 0 时为 0）。
- `Register_Pipeline_Variable` 让深度**运行时可变**：用 `tap_number` 选抽头，延迟 = `tap_number + 1`，改值需脉冲 `tap_number_load`；底层用 Xilinx **SRL 原语**（每 LUT 当 16/32 级移位寄存器），故 `PIPE_DEPTH` 只能是 16 或 32。
- 选型口诀：要并行加载/每级初值 → 全功能 `Register_Pipeline`；只要固定延迟（含可能的 0 拍）→ `Register_Pipeline_Simple`；要运行时改延迟 → `Register_Pipeline_Variable`（受 SRL 深度限制，最小延迟 1 拍）。

## 7. 下一步学习建议

本讲把「无脑移位的寄存器链」讲完。自然的下一步是进入「**会握手、会停顿**」的弹性流水线，那里延迟不再是固定的几个寄存器，而是由 ready/valid 控制的动态缓冲：

- **下一单元 [u9 Ready/Valid 握手原理](./u9-l1-handshake-interfaces.md)**：先建立 `valid`/`ready`/`data` 三信号与「握手完成」的判定，理解为什么固定深度的 `Register_Pipeline` 还不够——下游一旦不 ready，流水线得能「停」。
- **[u10-l1 Skid Buffer 与 COTTC FSM 方法](./u10-l1-skid-buffer-fsm.md)**：看 `Pipeline_Skid_Buffer` 如何用 FSM 把寄存器流水线改造成可停顿的弹性缓冲——它是本讲「寄存器链」加上「控制通路」后的第一个真实范本。
- **延伸阅读源码**：打开 [Counter_Binary.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Counter_Binary.v)，看它如何把 `Adder_Subtractor_Binary` 与多个 `Register` 拼成计数器——与本讲的「链式 `Register`」对照，体会构建块库的复用方式。
