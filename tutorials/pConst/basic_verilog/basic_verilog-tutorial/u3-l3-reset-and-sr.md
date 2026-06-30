# 复位与 SR 触发器：set_reset 家族

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「SR 触发器」要解决什么问题，以及 basic_verilog 里这一组模块为什么强调「无亚稳态（No metastable state）」。
- 区分 `set_reset`（复位优先）和 `reset_set`（置位优先）在「同时按下 set 与 reset」时的行为差别，并能根据应用语义选用。
- 区分同步寄存输出版本（`set_reset`/`reset_set`）与带组合输出的 `_comb` 版本，理解「零延迟响应」的代价与用途。
- 理解 `soft_latch` 作为「组合数据保持电路」如何在不推断硬件锁存器的前提下，复刻出锁存器的行为。

本讲属于「时钟域跨越与复位策略」单元，承接 u1-l2 建立的「参数化端口 / `always_ff` 同步复位 / `always_comb` 组合逻辑」约定，并为后续存储、FIFO 等需要「状态保持」的模块打基础。

## 2. 前置知识

在进入本讲前，请确认你已理解下面这些来自前置讲义的概念：

- **模块、例化、四段式文件结构**：见 u1-l2。本讲的五个模块都遵循「头注释 / INFO / 例化模板 / module 实现」的统一写法。
- **`always_ff` 与同步复位 `nrst`**：时序逻辑写在 `always_ff @(posedge clk)`，低有效同步复位写法是敏感列表只含时钟、块内用 `if( ~nrst )` 先判复位。
- **异步复位 `anrst`**：敏感列表写成 `@(posedge clk or negedge anrst)`，复位一旦撤销即时生效。本讲 `soft_latch` 用到它。
- **组合逻辑 `always_comb`**：用阻塞赋值 `=`，输出随输入即时变化。
- **亚稳态（metastability）**：见 u3-l1。那是「跨时钟域采样异步信号」时触发器输出停在非法电平的物理现象。本讲所说的「无亚稳态」**指的不是这一种**，下文 4.1 会专门区分，避免混淆。

一个直觉性的问题先埋在这里：**当你想做一个「按一下置 1、再按一下清 0」的标志位，而置位和复位有可能同一拍同时到达时，输出该听谁的？** 本讲整篇都在回答这个问题。

## 3. 本讲源码地图

本讲涉及的关键文件（均在仓库根目录）：

| 文件 | 作用 | README 标签 |
|------|------|-------------|
| `set_reset.sv` | 同步 SR 触发器，**复位优先**（reset dominates），寄存输出 | 绿圈基础 |
| `reset_set.sv` | 同步 SR 触发器，**置位优先**（set dominates），寄存输出 | 绿圈基础 |
| `set_reset_comb.sv` | 同上，但 `q` 是组合输出，对 s/r 零延迟响应（复位优先） | 绿圈基础 |
| `reset_set_comb.sv` | 同上，组合输出（置位优先） | 绿圈基础 |
| `soft_latch.sv` | 「软锁存」：用寄存器 + 组合 mux 复刻锁存行为，不推断硬件 latch | 红圈进阶 |
| `soft_latch_tb.sv` | 仓库自带 testbench，证明 `always_latch` / `set_reset_comb` 阵列 / `soft_latch` 三者输出等价 | — |

README 对这一组模块的定位见 [README.md:85-95](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/README.md#L85-L95)，注意 `soft_latch.sv` 被标为红圈（进阶），其余四个为绿圈。

## 4. 核心概念与源码讲解

### 4.1 SR 触发器与「无亚稳态」的由来

#### 4.1.1 概念说明

SR 触发器（Set-Reset trigger）是最基本的状态保持元件：它有一个「置位」输入 `s`、一个「复位」输入 `r`，以及输出 `q`（通常还有反相输出 `nq`）。语义是：

- 只给 `s`：`q` 变 1 并保持（置位）。
- 只给 `r`：`q` 变 0 并保持（复位）。
- 两个都不给：`q` 保持原值。
- **两个同时给**：经典 SR 的「麻烦状态」，本节的核心。

教科书里的经典 SR 锁存器是用两个交叉耦合的与非/或非门搭出来的**纯组合反馈**电路。它的麻烦在于：当 `s` 与 `r` 同时有效、随后又**同一时刻撤销**时，电路没有任何信息告诉它该停在 0 还是 1，只能靠两个门之间皮秒级的器件偏差决定——输出可能振荡，也可能最终随机落定。这种「无确定解」的非法状态，工程上常被泛称为「metastable / indeterminate」。

basic_verilog 这一族模块的 INFO 都写着 `No metastable state`，含义是：**它们不是用交叉耦合门搭的异步锁存器，而是用时钟沿驱动的触发器（flip-flop）实现的同步电路**。因为一切状态更新都发生在确定的时钟沿上，并且对「同时置位与复位」显式约定了一个赢家（优先级），输出永远是确定值，没有非法中间态、没有振荡。

> ⚠️ **务必区分两种「亚稳态」**：这里的「无亚稳态」指**消除了经典 SR 锁存器的非法状态/竞争**，**不等于**「对任意异步输入免疫」。如果你的 `s`/`r` 来自另一个时钟域、违反了本触发器的建立/保持时间，触发器照样会进入 u3-l1 讲的那种物理亚稳态。要让跨域信号安全，仍需 `cdc_data` / `cdc_strobe` 那一套同步器。

#### 4.1.2 核心流程

一个「同步 SR 触发器」每个时钟上升沿做三件事，伪代码如下：

```
在 posedge clk：
  if (复位有效)   q ← 0          // 同步复位，优先级最高
  else if (s)     q ← 1          // 试探置位
         if (r)   q ← 0          // 再试探复位（注意是两个独立 if，不是 else-if）
  // 两个 if 的书写顺序 = 优先级链
```

关键是**两个独立的 `if`，而不是 `else if`**：当 `s` 与 `r` 同时为 1，谁写在后面、谁就是最终赢家——这正是 4.2 节区分「置位优先 / 复位优先」的全部秘密。

衡量 SR 行为的标准工具是「次态真值表」，其中 \(q\) 表示当前态、\(q^+\) 表示下一个时钟沿后的次态：

| s | r | 经典异步 SR 锁存器 | 同步 SR（复位优先） | 同步 SR（置位优先） |
|---|---|---|---|---|
| 0 | 0 | 保持 \(q\) | 保持 \(q\) | 保持 \(q\) |
| 0 | 1 | 复位 → 0 | 复位 → 0 | 复位 → 0 |
| 1 | 0 | 置位 → 1 | 置位 → 1 | 置位 → 1 |
| 1 | 1 | **非法/振荡** | 0（复位赢） | 1（置位赢） |

右边两列就是本讲四个模块要实现的两种「赢家约定」。

#### 4.1.3 源码精读

先看最朴素的 `set_reset.sv`。模块端口与实现见 [set_reset.sv:25-44](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/set_reset.sv#L25-L44)，核心时序逻辑如下：

```systemverilog
always_ff @(posedge clk) begin
  if( ~nrst ) begin
    q = 0;            // 同步复位，最高优先
  end else begin
    if( s ) q = 1'b1; // 先试探置位
    if( r ) q = 1'b0; // 再试探复位 → 复位写在后面，复位优先
  end
end

assign nq = ~q;
```

这段代码在 [set_reset.sv:34-43](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/set_reset.sv#L34-L43)。两个细节值得注意：

1. **复位优先级最高**：`if( ~nrst )` 是外层判断，复位一旦有效直接把 `q` 清 0，`s`/`r` 都没机会插手。
2. **作者用了阻塞赋值 `=` 而非 `<=`**：这偏离了 u1-l2 里「`always_ff` 默认用非阻塞 `<=`」的约定。这里是有意为之——用阻塞赋值后，`if(s) q=1; if(r) q=0;` 在同一拍内**顺序求值**，「先 set 再 reset」就成了一条字面意义上的优先级链，读起来一目了然。在这个块里 `q` 是唯一的左值、且无跨块依赖，所以阻塞赋值是安全的；它和不阻塞写法在这个具体电路里功能等价（同时为 1 时都是后写的 `r` 生效），但可读性更好。

INFO 注释 [set_reset.sv:6-8](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/set_reset.sv#L6-L8) 把结论写得很直白：「Synchronous SR trigger variant / No metastable state / RESET signal dominates here」。

#### 4.1.4 代码实践

**目标**：用「源码阅读 + 手工推演」确认同步 SR 的次态表。

1. 打开 `set_reset.sv`，对照 4.1.2 的真值表，逐行手算当 `(s,r)` 取 `(0,0)/(0,1)/(1,0)/(1,1)` 时 `q` 的次态。
2. 在脑中（或纸上）追踪一次「同时 set 与 reset」：进入 `else` 块后，`if(s)` 先把 `q` 置 1，紧接着 `if(r)` 又把它拉回 0，最终 `q=0`。
3. 想一想：如果把第二个 `if(r)` 删掉、只留 `if(s)`，这个模块退化成什么？（答：一个「只能置位、靠复位清零」的简单标志位。）

**需要观察的现象**：优先级完全由两个 `if` 的书写顺序决定，与综合器无关。
**预期结果**：`set_reset` 在 `s=r=1` 时 `q=0`。
**待本地验证**：如果你想直观看到，可以把 4.2 节的 testbench 直接跑起来。

#### 4.1.5 小练习与答案

**练习 1**：为什么 INFO 敢说「No metastable state」？它和 u3-l1 讲的跨域亚稳态是一回事吗？

> **答**：因为该模块是时钟沿驱动的同步触发器，对 `s=r=1` 这一非法输入显式约定了优先级（谁写在后面谁赢），输出永远确定，消除了经典交叉耦合 SR 锁存器的振荡/非法态。它和跨域亚稳态**不是**一回事——后者是异步信号违反建立/保持时间导致的触发器物理亚稳态，本模块并不解决跨域问题。

**练习 2**：把 `set_reset.sv` 里的两个 `if` 顺序对调（先 `r` 后 `s`），模块行为会变成什么？

> **答**：会变成「置位优先」（即 `reset_set` 的行为）：同时按下时 `s` 写在后面，`q=1`。这正是下一节 `reset_set.sv` 的写法。

### 4.2 置位优先 vs 复位优先：set_reset 与 reset_set

#### 4.2.1 概念说明

`set_reset.sv` 和 `reset_set.sv` 是一对「镜像」模块：端口、复位策略、输出反相完全相同，**唯一差别**就是两个 `if` 的先后顺序，因而决定了 `s` 与 `r` 同时有效时谁说了算。

- `set_reset`：**复位优先（reset dominates）**。同时按下时 `q=0`。适合「安全侧」语义——例如异常标志，宁可漏报一次 set，也要保证复位能把系统拉回安全态。
- `reset_set`：**置位优先（set dominates）**。同时按下时 `q=1`。适合「报警侧」语义——例如报警触发，宁可多报一次，也不能让复位把刚发生的告警盖掉。

选用哪一个，本质是一个**系统安全策略**问题，不是电路性能问题。

#### 4.2.2 核心流程

两者的次态表只在最后一行不同：

| s | r | `set_reset` 的 \(q^+\)（复位优先） | `reset_set` 的 \(q^+\)（置位优先） |
|---|---|---|---|
| 0 | 0 | \(q\)（保持） | \(q\)（保持） |
| 0 | 1 | 0 | 0 |
| 1 | 0 | 1 | 1 |
| 1 | 1 | **0** | **1** |

实现上就是把 4.1.3 里的两行 `if` 交换位置：

```
set_reset（复位优先）        reset_set（置位优先）
  if( s ) q = 1;               if( r ) q = 0;
  if( r ) q = 0;   ← r 后写    if( s ) q = 1;   ← s 后写
```

#### 4.2.3 源码精读

`reset_set.sv` 的实现见 [reset_set.sv:34-43](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/reset_set.sv#L34-L43)，与 `set_reset.sv` 几乎逐字相同，只是两个 `if` 换了顺序：

```systemverilog
end else begin
  if( r ) q = 1'b0; // 先试探复位
  if( s ) q = 1'b1; // 再试探置位 → 置位写在后面，置位优先
end
```

INFO 注释 [reset_set.sv:6-8](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/reset_set.sv#L6-L8) 同样写明「SET signal dominates here」。把两份源码左右并排看，你会深刻体会到：**这两个模块的全部差异，就是两行代码的顺序**。

#### 4.2.4 代码实践（本讲主实践）

**目标**：把 `set_reset` 与 `reset_set` 并排例化，用完全相同的 `s`/`r` 驱动，重点观察 `s=r=1` 时两者输出的差别。

**操作步骤**：新建下面这个 testbench（**示例代码**，非仓库原有文件），与 `set_reset.sv`、`reset_set.sv` 一起编译。

```systemverilog
`timescale 1ns / 1ps
module sr_compare_tb;

  logic clk;
  initial begin clk = 1'b0; forever #5 clk = ~clk; end   // 100 MHz

  logic s, r;

  // 复位优先
  logic q_rst_dom, nq_rst_dom;
  set_reset SR1 ( .clk(clk), .nrst(1'b1), .s(s), .r(r),
                  .q(q_rst_dom), .nq(nq_rst_dom) );

  // 置位优先
  logic q_set_dom, nq_set_dom;
  reset_set RS1 ( .clk(clk), .nrst(1'b1), .s(s), .r(r),
                  .q(q_set_dom), .nq(nq_set_dom) );

  initial begin
    $dumpfile("sr_compare.vcd"); $dumpvars(0, sr_compare_tb);
    s=0; r=0;
          #20;                 // 一拍保持
    s=1; r=0; #10; s=0; #10;   // 只置位
    r=1;      #10; r=0; #10;   // 只复位
    s=1; r=1; #10;             // ★同时置位与复位★
    $display("s=r=1 => set_reset.q=%b (复位优先) | reset_set.q=%b (置位优先)",
             q_rst_dom, q_set_dom);
    s=0; r=0; #20;
    $finish;
  end
endmodule
```

**需要观察的现象**：在波形里，`q_rst_dom` 与 `q_set_dom` 在「只 set」「只 reset」段完全一致；唯独进入 `s=r=1` 那一拍后出现分叉。

**预期结果**（由源码语义推得）：
- `s=1,r=0` 段：两者 `q` 都为 1。
- `s=0,r=1` 段：两者 `q` 都为 0。
- `s=1,r=1` 段：`q_rst_dom=0`、`q_set_dom=1`，$display 打印可见分叉。

**待本地验证**：请用 iverilog（`iverilog -g2012 -o sim sr_compare_tb.sv set_reset.sv reset_set.sv && vvp sim`）或 ModelSim 编译运行，对照 $display 输出与波形确认上述预测。

#### 4.2.5 小练习与答案

**练习 1**：设计一个「按键紧急停止」标志位：按下急停按钮置 1，按维护按钮清 0，两者可能同时按下。应该选 `set_reset` 还是 `reset_set`？

> **答**：选 `set_reset`（复位优先）并不对——这里要让「急停」优先，急停是 `s`，所以应选**置位优先**的 `reset_set`，保证急停按下时即使维护也同时按下，急停信号依然置 1。选型的关键是想清楚「同时发生时，哪一侧的语义更安全/更重要」。

**练习 2**：如果把 `nrst` 接成常 `1'b1`（如本 testbench），模块的复位还有用吗？

> **答**：复位永远不会触发，模块只受 `s`/`r` 控制。这在 testbench 里常用，目的是单独观察 SR 行为、排除复位干扰。真实上板时 `nrst` 应接系统复位。

### 4.3 组合输出版本：_comb 家族

#### 4.3.1 概念说明

4.1/4.2 的 `set_reset`/`reset_set` 有一个共同特点：`q` 是**寄存输出**，只在时钟上升沿更新。这意味着如果 `s` 在两个时钟沿之间冒出一个**比一个周期还窄**的脉冲，寄存器可能根本采不到，`q` 不会有反应。

`set_reset_comb.sv` / `reset_set_comb.sv` 解决的就是「我想让输出对 s/r **当拍即响应**」的需求：它们保留一个寄存器 `q_reg` 来「记住」历史状态，但把对外的 `q` 做成**组合输出**——`s` 或 `r` 一变化，`q` 在组合逻辑延迟后就跟着变，**不必等到下一个时钟沿**。源码头部的 ASCII 波形图（见 [set_reset_comb.sv:12-28](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/set_reset_comb.sv#L12-L28)）画的就是这一点：同一组输入下，`_comb` 版的 `q` 比 `set_reset.sv` 的 `q` 早半个周期跳变。

#### 4.3.2 核心流程

以 `set_reset_comb`（复位优先）为例，电路拆成两半：

1. **寄存部分 `q_reg`**：逻辑与 4.1 的 `set_reset` 完全一致，负责「长记忆」——把发生过的事件锁存到下一个时钟沿之后。
2. **组合输出 `q`**：`assign q = (s || q_reg) && ~r;`，把「当前 s/r」与「记住的 q_reg」混合后立即输出。

`_comb` 版的组合输出真值表（\(q_{reg}\) 为当前记住的值）：

| s | r | `set_reset_comb` 的 \(q=(s\lor q_{reg})\land \lnot r\) | `reset_set_comb` 的 \(q=s\lor(q_{reg}\land\lnot r)\) |
|---|---|---|---|
| 0 | 0 | \(q_{reg}\)（保持） | \(q_{reg}\)（保持） |
| 0 | 1 | 0 | 0 |
| 1 | 0 | 1 | 1 |
| 1 | 1 | **0**（复位优先） | **1**（置位优先） |

可见 `_comb` 版**保留了与非 `_comb` 版一致的优先级约定**，只是输出从「沿驱动」变成了「组合驱动」。

#### 4.3.3 源码精读

`set_reset_comb` 的实现见 [set_reset_comb.sv:45-66](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/set_reset_comb.sv#L45-L66)，关键两段：

```systemverilog
logic q_reg = 0;
always_ff @(posedge clk) begin        // 寄存部分：与 set_reset 一致
  if( ~nrst )      q_reg = 0;
  else begin
    if( s ) q_reg = 1'b1;
    if( r ) q_reg = 1'b0;             // 复位优先
  end
end

assign q = (s || q_reg) && ~r;        // 组合输出：当拍即响应，复位优先
assign nq = ~q;
```

`always_ff` 块在 [set_reset_comb.sv:55-62](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/set_reset_comb.sv#L55-L62)，组合输出在 [set_reset_comb.sv:64](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/set_reset_comb.sv#L64)。

对应的置位优先版本 `reset_set_comb` 见 [reset_set_comb.sv:64](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/reset_set_comb.sv#L64)，只把组合表达式换成 `assign q = s || (q_reg && ~r);`（`s` 在或运算的最外层，所以置位优先）。其寄存部分的两个 `if` 顺序也与 `reset_set` 一致（先 `r` 后 `s`），见 [reset_set_comb.sv:55-62](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/reset_set_comb.sv#L55-L62)。

#### 4.3.4 代码实践

**目标**：直观看到「组合输出比寄存输出响应更快」。

**操作步骤**：在 4.2.4 的 testbench 里再加一个 `set_reset_comb` 例化，把它的 `q_comb` 与 `set_reset` 的 `q_rst_dom` 同时拉到波形里；然后把激励里的某个 `s` 脉冲改窄（例如在两个时钟沿之间用 `#1 s=1; #1 s=0;` 制造一个亚周期窄脉冲）。

**需要观察的现象**：
- 正常宽脉冲下，`q_comb` 比 `q_rst_dom` **早大约半个周期**跳变（一个走组合路径、一个等时钟沿）。
- 窄脉冲下，`q_rst_dom`（寄存）可能完全无反应，而 `q_comb`（组合）会冒出一个相应的窄尖峰。

**预期结果**：组合输出对输入「零延迟」响应；代价是 `q` 上可能出现组合毛刺，且 `q` 不再是干净的寄存器输出，时序收敛更费力。
**待本地验证**：窄脉冲是否被寄存版漏采，取决于脉冲与时钟沿的相对位置，请在你的仿真器里实际跑一遍观察。

#### 4.3.5 小练习与答案

**练习 1**：`set_reset_comb` 里既然 `q` 是组合输出，为什么还要保留 `q_reg`？直接 `assign q = (s || 1'b0) && ~r;` 不行吗？

> **答**：不行。`q_reg` 提供「记忆」：当 `s` 撤销后，是 `q_reg`（在 `s` 有效期间被时钟沿锁存为 1）让 `q` 继续保持 1。没有 `q_reg`，`s` 一撤销 `q` 立刻掉回 0，就失去了 SR 触发器「保持」的核心能力，退化成一个纯组合逻辑。

**练习 2**：`_comb` 版的优先级是由组合表达式决定，还是由 `always_ff` 里 `if` 顺序决定？

> **答**：两者一致、相互印证。`always_ff` 里 `if` 顺序决定 `q_reg` 的优先级；组合表达式 `q=(s||q_reg)&&~r` 中 `&&~r` 在最外层同样体现复位优先。两处必须保持一致，否则寄存态与组合输出会出现矛盾。

### 4.4 软锁存 soft_latch：组合数据保持电路

#### 4.4.1 概念说明

`soft_latch.sv`（README 标红圈进阶）解决另一个问题：**想「锁住」一个多 bit 数据，但又不想让综合器推断出硬件锁存器（latch）**。

在 FPGA/ASIC 流程里，纯组合的 `always_latch` 或不完整的 `if` 会推断出电平敏感锁存器，这类器件时序分析困难、容易出问题。`soft_latch` 的思路是**用一个寄存器 + 一个组合多路选择器，复刻出锁存器的行为**，但实际综合出的全是触发器和组合逻辑，**不产生任何硬件 latch**——这就是它叫「软（software）锁存」的原因。

它的接口：`latch` 是「透明/保持」控制（高电平透明、低电平保持），`in`/`out` 是 `WIDTH` 位数据（默认 1 位，参数化），`anrst` 是异步低有效复位。

#### 4.4.2 核心流程

电路分两部分协作：

1. **寄存缓冲 `in_buf`**（`always_ff`，异步复位）：每个时钟沿，若 `latch` 有效，就把当前 `in` 存进 `in_buf`。它记录「上一次 `latch` 有效时」的输入。
2. **组合输出 `out`**（`always_comb`）：按下表实时选择输出源。

| `anrst` | `latch` | `out` | 含义 |
|---|---|---|---|
| 0 | × | `0` | 异步复位，组合清零 |
| 1 | 1 | `in` | 透明：直接透传当前输入（零延迟） |
| 1 | 0 | `in_buf` | 保持：输出上次锁存值 |

效果就是一个电平敏感锁存器：`latch` 高时输出跟随输入，`latch` 低时冻结最近一次的值——但实现上是「触发器存历史 + 组合 mux 选当下」，没有任何门级锁存器。

#### 4.4.3 源码精读

`soft_latch` 的端口与参数见 [soft_latch.sv:53-62](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/soft_latch.sv#L53-L62)。寄存与组合两段：

```systemverilog
logic [WIDTH-1:0] in_buf = '0;

// buffering input data —— 寄存缓冲（异步复位）
always_ff @(posedge clk or negedge anrst) begin
  if( ~anrst )            in_buf[WIDTH-1:0] <= '0;
  else if( latch )        in_buf[WIDTH-1:0] <= in[WIDTH-1:0];
end

// mixing combinational and buffered data to the output —— 组合输出
always_comb begin
  if( ~anrst )            out[WIDTH-1:0] <= '0;
  else if( latch )        out[WIDTH-1:0] <= in[WIDTH-1:0];
  else                    out[WIDTH-1:0] <= in_buf[WIDTH-1:0];
end
```

寄存部分在 [soft_latch.sv:67-73](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/soft_latch.sv#L67-L73)，组合输出在 [soft_latch.sv:76-84](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/soft_latch.sv#L76-L84)。

> 📝 **风格提示**：这里 `always_comb` 块内用了非阻塞 `<=`，这偏离了 u1-l2 里「组合逻辑用阻塞 `=`」的常规约定（多数 lint 工具会告警）。在该模块里由于 `out` 是块内唯一左值、且无跨块组合依赖，仿真与综合行为一致；读源码时把它理解为「组合选择输出源」即可，不必纠结赋值符号。

**为什么说它与本讲的 SR 家族同源？** 仓库自带的 `soft_latch_tb.sv` 给出了直接证据：它用三种方式实现同一个「数据保持」行为并断言三者完全相等，见 [soft_latch_tb.sv:99-129](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/soft_latch_tb.sv#L99-L129)：

- `data1`：Verilog 原生 `always_latch`（真硬件锁存器）；
- `data2`：用一整组 `set_reset_comb` 按位拼出的保持电路（`s = set & bit`、`r = (set & ~bit) | ret`）；
- `data3`：真正的 `soft_latch` 例化。

三者输出在每个时钟沿被比较，只要出现不等就把 `success` 拉低，见 [soft_latch_tb.sv:133-146](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/soft_latch_tb.sv#L133-L146)。这说明：**`soft_latch` 的行为可以用按位 `set_reset_comb` 阵列等价搭出**——SR 家族与软锁存在本是同一个「组合数据保持」思想的两种封装。

#### 4.4.4 代码实践

**目标**：用现成的 `soft_latch_tb.sv` 体会「软锁存 = 无 latch 的数据保持」。

**操作步骤**：

1. 阅读 [soft_latch_tb.sv:99-129](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/soft_latch_tb.sv#L99-L129)，理解 `data1/data2/data3` 三路的激励为何等价（`set`/`ret` 由一个随机数的高位推导，`ret` 同时充当 `data2` 的复位与 `data3` 的 `~anrst`）。
2. 用 iverilog 或 ModelSim 编译运行该 testbench，观察 `data1`、`data2`、`data3` 三条波形是否始终重合，以及 `success` 是否恒为 1。

**需要观察的现象**：`data2`（SR 阵列）与 `data3`（`soft_latch`）逐 bit 跟随 `data1`（真锁存器）；当 `set` 有效时三路都透传随机数，`ret` 有效时三路同时清零，其余时间三路都保持上次值。
**预期结果**：`success` 全程为 1，证明三种实现功能等价。
**待本地验证**：该 testbench 还例化了 `clk_divider`、`edge_detect`、`c_rand` 等模块，编译时需把这些源文件一并加入文件列表。

#### 4.4.5 小练习与答案

**练习 1**：既然 `soft_latch` 行为上像个锁存器，为什么综合后不会出现硬件 latch？

> **答**：因为它的「保持」靠 `in_buf` 触发器实现——`latch` 低时 `out` 取的是 `in_buf`（一个寄存器输出），而非靠组合反馈环路。综合器看到的是「触发器 + 组合 mux」，没有任何不完整 `if` 或组合反馈，因此不推断 latch。

**练习 2**：把 `soft_latch` 的 `latch` 接一个常 `1'b1`，模块退化成什么？

> **答**：`out` 恒等于 `in`（始终透明），退化成一根「带异步复位清零」的直通导线；`in_buf` 仍在每拍跟随 `in`，但对输出不再有意义。

## 5. 综合实践

**任务**：搭一个「故障锁存与状态快照」小电路，把本讲四个最小模块串起来。

要求：

1. 用 `set_reset` 做一个**故障标志位**：单拍 `fault` 脉冲作为 `s`（故障置位后要保持），维护按钮 `clear` 作为 `r`。先想清楚：若 `fault` 与 `clear` 可能同时按下，从「安全」角度你希望谁优先？据此决定用 `set_reset` 还是 `reset_set`，并说明理由。
2. 再用 `reset_set` 做一个对照版本 `flag_b`，把 `flag_a` 与 `flag_b` 同时拉进波形，验证两者在 `fault` 与 `clear` 同拍到达时输出不同。
3. 用 `soft_latch` 在 `fault` 生效期间捕获一个 8 位 `status` 状态码（`latch` 接 `flag_a`、`in` 接 `status`），使得维护人员事后能读出故障发生时刻的状态。

**验证方式**：写一个 testbench，模拟「故障发生 → 同时按下 clear → 故障恢复后读 status」的序列，观察：
- `flag_a` 与 `flag_b` 在「同拍 set+reset」时的分叉（呼应 4.2）；
- `soft_latch` 的 `out` 是否在 `flag_a` 高电平期间跟随 `status`、在 `flag_a` 被清零后保持住最后那个值。

**预期结论**：选 `set_reset`（复位优先）会让 clear 盖过 fault，故障标志可能被提前清除——若希望「故障绝不丢」，应改用 `reset_set`（置位优先）。这个练习的核心不是电路多复杂，而是体会**「同时 set+reset 时谁优先」是一个系统安全决策**，而本讲这一族模块正是把这个决策封装成了两个一行之差的模块。

> 该综合实践的波形结果需**待本地验证**：你应当实际编译运行自己写的 testbench，对照上述预期结论。

## 6. 本讲小结

- basic_verilog 的 SR 家族是**时钟沿驱动的同步触发器**，不是交叉耦合门的异步锁存器；通过显式约定「同时 set+reset 的赢家」，消除了经典 SR 的非法/振荡态——这就是 INFO 所说的「No metastable state」。
- **务必区分**：此处的「无亚稳态」≠ u3-l1 的跨时钟域亚稳态；跨域信号仍需 `cdc_data`/`cdc_strobe` 同步。
- `set_reset`（复位优先）与 `reset_set`（置位优先）的**唯一差别是两个 `if` 的书写顺序**，优先级完全是系统安全语义的选择。
- `_comb` 版本在寄存器 `q_reg` 之外加一条组合输出，使 `q` 对 s/r **当拍零延迟响应**，代价是可能引入组合毛刺、时序更难收敛。
- `soft_latch` 用「触发器存历史 + 组合 mux 选当下」复刻锁存行为，**不推断硬件 latch**，README 因此将其标为红圈进阶。
- 仓库 `soft_latch_tb.sv` 证明：`always_latch`、`set_reset_comb` 阵列、`soft_latch` 三者输出等价——SR 家族与软锁存同属一个「组合数据保持」思想。

## 7. 下一步学习建议

- 本讲的 SR 触发器是最简单的「状态机砖块」。下一步建议进入 **u4 单元（存储器与 FIFO）**，看 `fifo_single_clock_ram` 如何用 RAM + 指针计数实现更深的数据缓冲——其中满/空标志的保持与 4.3 的组合输出思想一脉相承。
- 如果你关心「标志位/状态如何安全地跨时钟域」，回顾 u3-l1（`cdc_data`）与 u3-l2（`cdc_strobe`），并把本讲的 `set_reset` 与它们对比：本讲解决「同域内谁优先」，CDC 模块解决「跨域如何不丢、不亚稳」。
- 想强化对「寄存输出 vs 组合输出」时序差异的直觉，可结合 u2-l3（`delay`）的移位链与 u7-l2（时序约束）的 `set_false_path`，理解为什么 `_comb` 版的组合路径会给时序收敛带来额外压力。
