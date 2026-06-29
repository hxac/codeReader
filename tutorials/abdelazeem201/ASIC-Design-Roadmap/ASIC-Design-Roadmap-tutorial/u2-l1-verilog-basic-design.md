# 读懂一个简单 Verilog 设计

## 1. 本讲目标

学完本讲后，你应该能够：

- 看懂一段真实 Verilog 代码里 **模块（module）** 和 **端口（port）** 是怎么声明的。
- 区分 **寄存器（reg）** 和 **线网（wire）** 两种数据类型，并知道什么时候必须用哪一个。
- 区分 **时序 always 块**（`always @(posedge clk)`）和 **组合 always 块**（`always @(敏感信号列表)`）的差别。
- 理解什么是 **层次化例化（instantiation）**，以及一个顶层模块如何调用子模块。
- 为 `ARITH` 子模块写一个最小的 testbench 并预测输出。

本讲全部围绕仓库里真实存在的一个文件展开：[MY-Design/MY_DESIGN.v](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v)。它只有 72 行，却同时包含了一个顶层模块 `MY_DESIGN` 和两个子模块 `ARITH`、`COMBO`，是一个非常适合「从零读懂 Verilog」的样本。

## 2. 前置知识

在开始之前，先用一句话建立几个直觉（详细背景见前置讲义 [u1-l2 ASIC 设计流程全景](u1-l2-asic-flow-panorama.md)）：

- **RTL（寄存器传输级）**：用硬件描述语言（如 Verilog）写出的代码，描述电路里数据如何在寄存器之间流动、被运算。本讲的 `MY_DESIGN.v` 就是一段 RTL。
- **Verilog 模块（module）**：Verilog 描述电路的基本单位，类似软件里的「类」或「函数」——它有名字、有输入输出端口、有内部逻辑。一块芯片通常由许多模块层层嵌套组成。
- **综合（synthesis）**：把 RTL 翻译成由真实标准单元（与门、寄存器等）组成的门级网表的过程。要能被综合，RTL 必须遵循「可综合」的写法。本讲会顺带指出哪些写法是可综合的好习惯。
- **位宽（width）**：本文件里大量出现 `[4:0]`，表示一根 5 位宽的总线（最高位是第 4 位，最低位是第 0 位）。

> 提示：如果你完全没写过任何 Verilog，也不用担心。本讲会逐行拆解，术语第一次出现都会解释。

## 3. 本讲源码地图

本讲只涉及一个文件，但里面有 3 个模块，分工如下：

| 模块 | 在文件中的行范围 | 作用 |
| --- | --- | --- |
| `MY_DESIGN` | 第 2–28 行 | 顶层模块，包含两个 always 块（一个时序、一个组合），并例化了 `ARITH` 与 `COMBO`。 |
| `ARITH` | 第 34–51 行 | 算术单元：根据 `sel` 选择做加法或减法。 |
| `COMBO` | 第 56–71 行 | 组合单元：内部再次例化 `ARITH`，并把结果加上 `Cin1`。 |

全文都来自：[MY-Design/MY_DESIGN.v](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v)

## 4. 核心概念与源码讲解

### 4.1 模块与端口声明

#### 4.1.1 概念说明

一个 Verilog **模块**就是一块电路的「封装盒子」：盒子外面露出若干 **端口（port）** 与外界相连，盒子里面是实现逻辑。端口分为三种方向：

- `input`：进盒子的信号（输入）。
- `output`：出盒子的信号（输出）。
- `inout`：双向（本文件没有用到）。

声明端口有两种风格：

- **非 ANSI 风格（Verilog-1995 老风格）**：先在模块名后的括号里列出端口 *名字*，再在模块体内单独声明每个端口的方向和类型。本文件用的就是这种风格。
- **ANSI 风格（较新）**：方向和类型直接写在模块名后的括号里，更简洁。

认识老风格很重要，因为很多遗留脚本、教材和本仓库都用它。

#### 4.1.2 核心流程

声明一个模块的步骤：

1. 用 `module 模块名 (端口名列表);` 开头。
2. 在模块体内逐行写 `input / output / inout` 声明方向。
3. 再写 `reg / wire` 声明数据类型（下一节解释）。
4. 写内部逻辑（always 块、例化等）。
5. 用 `endmodule` 结尾。

#### 4.1.3 源码精读

顶层模块 `MY_DESIGN` 的声明与端口部分：

[MY-Design/MY_DESIGN.v:2-7](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v#L2-L7) — 这 6 行完成了「模块名 + 端口方向 + 内部信号类型」的全部声明：

- 第 2 行 `module MY_DESIGN ( Cin1, Cin2, Cout, data1, data2, sel, clk, out1, out2, out3);` 拿出端口名字清单。
- 第 3 行 `input [4:0] Cin1, Cin2, data1, data2;` 声明四个 5 位输入。
- 第 4 行 `input sel, clk;` 声明两个 1 位输入（`sel` 是选择信号，`clk` 是时钟）。
- 第 5 行 `output [4:0] Cout, out1, out2, out3;` 声明四个 5 位输出。
- 第 6–7 行声明内部信号类型（见 4.2 节）。

子模块 `ARITH` 的声明更短，同样是非 ANSI 风格：

[MY-Design/MY_DESIGN.v:34-38](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v#L34-L38) — `ARITH` 模块名 + 端口 `a, b, sel, out1`，其中 `a, b` 是 5 位输入，`sel` 是 1 位输入，`out1` 是 5 位输出。

#### 4.1.4 代码实践

**实践目标**：在没有 EDA 工具的情况下，仅靠阅读就能把每个模块的「端口表」画出来。

**操作步骤**：

1. 打开 [MY-Design/MY_DESIGN.v](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v)。
2. 对 `MY_DESIGN`、`ARITH`、`COMBO` 三个模块，分别列出：模块名、所有 input 及其位宽、所有 output 及其位宽。

**需要观察的现象**：你会发现三个模块的端口位宽几乎都是 `[4:0]`（5 位），只有 `sel`、`clk` 是 1 位。

**预期结果**（示例答案，可直接对照）：

| 模块 | 输入 | 输出 |
| --- | --- | --- |
| `MY_DESIGN` | `Cin1[4:0], Cin2[4:0], data1[4:0], data2[4:0], sel, clk` | `Cout[4:0], out1[4:0], out2[4:0], out3[4:0]` |
| `ARITH` | `a[4:0], b[4:0], sel` | `out1[4:0]` |
| `COMBO` | `Cin1[4:0], Cin2[4:0], sel` | `Cout[4:0]` |

#### 4.1.5 小练习与答案

**练习 1**：如果把第 3 行写成 `input Cin1, Cin2, data1, data2;`（去掉 `[4:0]`），每个信号会变成几位？

**答案**：会变成默认的 1 位信号。Verilog 中不写位宽默认是 `[0:0]`，即 1 根线。

**练习 2**：`MY_DESIGN` 模块名后括号里的端口顺序，和后面 `input/output` 声明的顺序必须完全一致吗？

**答案**：不必。括号里只是「端口清单」，方向和位宽由模块体内的 `input/output` 声明决定；顺序可以不同（但保持一致是好习惯，便于阅读）。

---

### 4.2 寄存器与线网（reg 与 wire）

#### 4.2.1 概念说明

Verilog 里信号有两种最常见的数据类型，初学者最容易混淆：

- **`wire`（线网）**：表示一根「物理连线」。它的值由驱动它的东西决定，自己不能被赋值语句主动写。模块例化的输出、`assign` 连续赋值的结果都接到 `wire` 上。
- **`reg`（寄存器型）**：表示一个「能保持状态」的量。它 **并不一定真的综合成一个触发器**——名字叫 reg 容易误导，真正的规则是：**凡是在 `always` 块里被赋值的信号，必须声明为 `reg`**。

记住这一条铁律就够用了：

> 在 `always` 块内被赋值 → 声明为 `reg`；用线连（例化端口、`assign`）→ 声明为 `wire`。

#### 4.2.2 核心流程

判断一个信号该用 `reg` 还是 `wire`：

1. 看它在哪里被赋值。
2. 如果在 `always` 块里被赋值（如 `R1 <= ...`）→ `reg`。
3. 如果是被例化模块的输出驱动，或由 `assign` 驱动 → `wire`。
4. 输入端口 `input` 默认就是 `wire`，不能再声明为 `reg`。

#### 4.2.3 源码精读

顶层模块里同时出现了 `reg` 和 `wire`，对照上面铁律就一目了然：

[MY-Design/MY_DESIGN.v:6-7](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v#L6-L7) — 这两行声明了内部信号类型：

- 第 6 行 `reg [4:0] R1, R2, R3, R4, out1, out2, out3;`：这些都在后面的 `always` 块里被赋值（见 4.3 节），所以是 `reg`。注意 `out1, out2, out3` 既是第 5 行的 `output` 又是这里的 `reg`——一个端口可以同时具备「输出方向」和「reg 类型」。
- 第 7 行 `wire [4:0] arth_o;`：`arth_o` 是第 9 行被例化模块 `ARITH` 的输出端口 `.out1(arth_o)` 驱动的连线，所以是 `wire`。

子模块里也遵循同样规律。例如 `ARITH`：

[MY-Design/MY_DESIGN.v:37-38](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v#L37-L38) — `out1` 是输出，又因为后面在 `always` 块（第 40 行）里被赋值，所以第 38 行声明为 `reg [4:0] out1;`。

> 小结：`MY_DESIGN` 里的 `arth_o` 是 `wire`（连例化输出），`R1..R4 / out1..out3` 是 `reg`（always 内赋值）。`reg` 不等于触发器——只有被时钟沿驱动的 reg 才会综合成触发器，详见下一节。

#### 4.2.4 代码实践

**实践目标**：在不开工具的前提下，根据「铁律」给 `COMBO` 模块的信号补全类型。

**操作步骤**：

1. 打开 [MY-Design/MY_DESIGN.v](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v) 第 56–71 行的 `COMBO` 模块。
2. 对 `Cin1, Cin2, sel, Cout, arth_o` 这五个信号，判断各自应是 `wire` 还是 `reg`。

**需要观察的现象**：注意 `COMBO` 内部第 62 行又声明了一个 `wire [4:0] arth_o;`，它和顶层 `MY_DESIGN` 里的 `arth_o` 是 **两个不同模块里的同名信号，互不相干**（模块作用域隔离）。

**预期结果**：

| 信号 | 类型 | 理由 |
| --- | --- | --- |
| `Cin1, Cin2, sel` | `input`（默认 wire） | 输入端口 |
| `Cout` | `reg`（且是 output） | 在第 66 行 always 块内赋值 |
| `arth_o` | `wire` | 第 64 行被例化模块 `ARITH` 的 `.out1` 驱动 |

#### 4.2.5 小练习与答案

**练习 1**：`reg` 类型的信号一定会在硬件里变成一个 D 触发器吗？

**答案**：不一定。`reg` 只是「在 always 块里被赋值」的语法要求。只有当赋值发生在时钟沿（如 `always @(posedge clk)`）时才综合成触发器；如果是纯组合 always 块（敏感信号是电平），它综合出来的是组合逻辑（如与门、加法器），并不会变成寄存器。

**练习 2**：能否把第 7 行的 `wire [4:0] arth_o;` 改成 `reg [4:0] arth_o;`？

**答案**：不能。`arth_o` 由例化端口 `.out1(arth_o)` 驱动，这种「连线」必须用 `wire`；改成 `reg` 会在编译时报错（reg 不能被例化输出端口驱动）。

---

### 4.3 always 时序块与组合块

#### 4.3.1 概念说明

`always` 块是 Verilog 描述电路行为的核心。它长这样：

```
always @ (敏感事件)
  begin
    ... 赋值语句 ...
  end
```

「敏感事件」决定了这个块 **什么时候被执行**，由此分出两类：

- **时序 always 块**：敏感事件是 **时钟边沿**，如 `always @(posedge clk)`。它在每个时钟上升沿触发一次，综合出来的是 **触发器（寄存器）**——这就是「时序逻辑」。赋值用非阻塞 `<=`。
- **组合 always 块**：敏感事件是 **信号电平的变化**，如 `always @(a, b, sel)`。只要括号里的信号一变，块就重新求值，描述的是 **组合逻辑**（输出随输入立即变化，无记忆）。

还有一个关键区别是 **阻塞 `=` 与非阻塞 `<=`**：

- `<=`（非阻塞）：在块结束时「同时」更新，常用于时序逻辑。
- `=`（阻塞）：立即更新，常用于组合逻辑。

> 本文件的教学示例在组合块里也用了 `<=`，能仿真但不是最佳实践；下文会点出推荐写法。

#### 4.3.2 核心流程

以 `MY_DESIGN` 为例，它同时含两类块：

1. **时序块**（第 13 行）：每个 `clk` 上升沿，把 `arth_o / (data1&data2) / (data1+data2)` 等结果「锁存」进 `R1..R4`。
2. **组合块**（第 21 行）：只要敏感列表 `(out2, R1, R3, R4)` 中任一变化，就重新计算 `out1 / out2 / out3`。

两者配合，构成「寄存器 + 组合运算」的经典结构。

关于非阻塞赋值的「同时更新」语义，可用一个直观式子表达：在同一时钟沿，所有 `<=` 右侧都读到 **旧值**，左侧在沿结束时一起更新。即对一组寄存器 \( R_i \)，下一周期值

\[
R_i[t+1] = f_i\big(\text{输入},\ R_1[t], R_2[t], \dots\big)
\]

所有 \( R_i \) 用 \( t \) 时刻的旧值同时求值、同时更新，互不干扰。

#### 4.3.3 源码精读

**时序块**——第 13–19 行：

[MY-Design/MY_DESIGN.v:13-19](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v#L13-L19) — 这是典型的时序逻辑：

- 触发条件是 `posedge clk`（时钟上升沿）。
- 四条 `<=` 非阻塞赋值：`R1 <= arth_o;`、`R2 <= data1 & data2;`、`R3 <= data1 + data2;`、`R4 <= R2 + R3;`。
- 注意 `R4 <= R2 + R3;` 用到的是 `R2, R3` 的 **旧值**（本周期的），所以综合出的是「一级流水寄存器」关系，而不是组合链。

**组合块**——第 21–26 行：

[MY-Design/MY_DESIGN.v:21-26](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v#L21-L26) — 这是组合逻辑（教学示例）：

- 敏感列表 `always @ (out2, R1, R3, R4)` 是电平敏感，描述组合行为。
- `out1 <= R1 + R3;`、`out2 <= R3 & R4;`、`out3 <= out2 - R3;`。
- 教学提醒：现代可综合写法推荐把敏感列表写成 `always @(*)`（自动包含所有右侧信号），且组合块用阻塞 `=`。本例用 `<=` 与显式列表仍可仿真，但属于旧式教学写法。

**ARITH 的组合块 + case**——第 40–47 行：

[MY-Design/MY_DESIGN.v:40-47](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v#L40-L47) — `ARITH` 用一个 `case` 语句实现「sel 控制 加/减」：

- `case({sel})`：`{sel}` 是拼接运算，把 1 位的 `sel` 包成一个 1 位表达式。
- `1'b0: out1 <= a + b;`（sel=0 做加法），`1'b1: out1 <= a - b;`（sel=1 做减法）。
- `1'b0` 读作「1 位二进制 0」，`1'b1` 读作「1 位二进制 1」。

#### 4.3.4 代码实践

**实践目标**：通过「改写敏感列表」体会组合块的行为，并理解为什么推荐 `@(*)`。

**操作步骤**：

1. 阅读第 21 行 `always @ (out2, R1, R3, R4)`，列出右侧表达式（`R1+R3`、`R3&R4`、`out2-R3`）用到的全部信号：`R1, R3, R4, out2`。
2. 思考：如果将来有人把 `out3 <= out2 - R3;` 改成 `out3 <= R4 - R1;`，但忘了把敏感列表改成 `always @ (out2, R1, R3, R4, R4...)`，会发生什么？
3. 结论：用 `always @(*)` 可以让工具自动把右侧所有信号纳入敏感列表，避免「改了逻辑忘改列表」导致的仿真与综合不一致。

**需要观察的现象**：手写敏感列表容易遗漏；这是 Verilog 教学里最经典的坑之一。

**预期结果**：你应当认同——对组合逻辑，写 `always @(*)` 比手写列表更安全。本文件采用手写列表是老式风格，阅读时理解其意图即可，自己写新代码时建议用 `@(*)`。

> 说明：本实践为「源码阅读型实践」，无需运行工具；结论由 Verilog 语义推导得出。

#### 4.3.5 小练习与答案

**练习 1**：第 15 行 `R1 <= arth_o;` 中的 `<=` 能否换成 `=`？在时序块里换用阻塞赋值通常会有什么风险？

**答案**：语法上可换，但在时序块里混用或使用阻塞 `=` 会导致仿真顺序依赖问题——阻塞赋值立即更新，可能让同一时钟沿里后面的语句读到「刚更新」的值而非旧值，造成仿真与综合结果不一致。时序逻辑统一用非阻塞 `<=` 是公认的安全写法。

**练习 2**：`ARITH` 的 `case({sel})` 中，如果把 `{sel}` 直接写成 `sel`（即 `case(sel)`），行为会变吗？

**答案**：不会变。`{sel}` 只是把单个信号拼成一个 1 位向量，与直接写 `sel` 在这里等价；匹配项 `1'b0 / 1'b1` 都是 1 位，照样能正确分支。这是作者的一种（略显多余但合法的）写法。

---

### 4.4 层次化例化

#### 4.4.1 概念说明

真实芯片不可能把所有逻辑塞进一个模块，而是 **层层嵌套**：顶层模块里「例化（调用）」若干子模块，子模块里又可以例化更下层的模块。这叫 **层次化设计（hierarchical design）**，类似软件里函数调用——但有一个根本区别：

> 硬件例化 **不是执行**，而是「放置一块真实电路」。例化多少次，硬件里就有多少份该电路。

例化的基本语法（**命名端口连接**，推荐）：

```
子模块名  例化名 ( .子模块端口(外部信号), .子模块端口(外部信号), ... );
```

`.子模块端口(外部信号)` 的好处是：连接关系与顺序无关，可读性强。

#### 4.4.2 核心流程

`MY_DESIGN` 的层次结构：

```
MY_DESIGN (顶层)
 ├── ARITH   U1_ARITH   (例化一次，做 data1/data2 的加减)
 └── COMBO   U_COMBO    (例化一次)
        └── ARITH  U2_ARITH   (COMBO 内部又例化一次 ARITH，做 Cin1/Cin2 的加减)
```

所以同一个 `ARITH` 模块在整块电路里被「放置」了 **两次**（U1_ARITH 与 U2_ARITH），它们是两份独立的硬件，互不影响。

#### 4.4.3 源码精读

顶层里两次例化——第 9–10 行：

[MY-Design/MY_DESIGN.v:9-10](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v#L9-L10) — 这是命名端口连接的典型写法：

- 第 9 行 `ARITH U1_ARITH ( .a(data1), .b(data2), .sel(sel), .out1(arth_o) );`：放置一个 `ARITH`，命名为 `U1_ARITH`；把它内部端口 `a/b/sel/out1` 分别接到外部 `data1/data2/sel/arth_o`。结果从 `out1` 流出到 `arth_o` 这根 `wire`。
- 第 10 行 `COMBO U_COMBO ( .Cin1(Cin1), .Cin2(Cin2), .sel(sel), .Cout(Cout) );`：放置一个 `COMBO`，命名为 `U_COMBO`；同名相连（外部 `Cin1` 接内部 `Cin1`，等等），输出 `Cout` 直接连到顶层输出端口。

`COMBO` 内部又例化了一次 `ARITH`——第 64 行：

[MY-Design/MY_DESIGN.v:64](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v#L64) — `COMBO` 在自己体内再放一个 `ARITH U2_ARITH`，把 `Cin1/Cin2/sel` 送给它，结果经 `arth_o`（注意：这是 `COMBO` 自己作用域内的 wire，与顶层的 `arth_o` 同名但不同物）再在第 68 行 `Cout <= arth_o + Cin1;` 里被使用。

> 这就形成了两级层次：顶层 `MY_DESIGN` → 子模块 `COMBO` → 孙子模块 `ARITH`。综合后，工具会按层次展开成扁平网表，这正是后续 [u4 PnR 主流程](u4-l1-icc2-setup-mcmm.md) 要吃进去的网表来源。

#### 4.4.4 代码实践

**实践目标**：用文字把整张层次图画出来，并标注每一处例化的「端口映射」。

**操作步骤**：

1. 在 [MY-Design/MY_DESIGN.v](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v) 里找出所有形如 `模块名 例化名 ( ... );` 的行（共 3 处：第 9、10、64 行）。
2. 对每一处，列出「子模块端口 → 外部信号」的映射表。
3. 用缩进画出层次树（如 4.4.2 所示）。

**需要观察的现象**：注意 `COMBO` 内部第 64 行的 `arth_o` 与顶层第 7 行的 `arth_o` 同名，但因分属不同模块，是两根不同的线——这是「模块作用域」的体现。

**预期结果**：得到 3 行例化映射 + 一棵两级（MY_DESIGN → COMBO → ARITH）层次树。

> 说明：本实践为「源码阅读型实践」，无需运行工具。

#### 4.4.5 小练习与答案

**练习 1**：如果要把第 9 行改成「按位置连接」（顺序连接）的风格，该怎么写？为什么一般不推荐？

**答案**：按位置连接写法是 `ARITH U1_ARITH ( data1, data2, sel, arth_o );`，要求外部信号的顺序必须与 `ARITH` 模块端口声明顺序完全一致。一旦子模块端口顺序调整，连接就会悄悄错位且不报错，因此可读性差、易出错，不推荐。

**练习 2**：`U1_ARITH` 和 `U2_ARITH` 是同一个模块的两次例化，它们在硬件里是同一份电路吗？

**答案**：不是。它们是两份物理上独立的电路（两个加/减法器），只是「图纸（模块定义）」相同。这正是硬件例化与软件函数调用的本质区别。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一个完整的动手任务：**为 `ARITH` 子模块写一个最小 testbench，给定 `a, b, sel` 观察输出**。

**实践目标**：验证你对「模块端口 + 组合 always + case」的理解，亲手例化 `ARITH` 并施加激励。

**操作步骤**：

1. 新建一个文件 `tb_arith.v`（注意：这是 **示例代码**，不是仓库原有文件，请自行创建用于练习，不要改动仓库源码），内容如下：

```verilog
`timescale 1ns/1ps      // 示例代码：仿真时间单位/精度
module tb_arith;         // 示例代码：testbench 顶层，无端口
    reg  [4:0] a, b;     // 激励用 reg
    reg        sel;
    wire [4:0] out1;     // 观察用 wire

    // 例化被测模块 ARITH（命名端口连接，呼应 4.4 节）
    ARITH dut ( .a(a), .b(b), .sel(sel), .out1(out1) );

    initial begin
        // 用例 1：sel=0，做加法 a+b
        a = 5'd1; b = 5'd2; sel = 1'b0;
        #10;
        $display("sel=0  a=%0d b=%0d -> out1=%0d (期望 3)", a, b, out1);

        // 用例 2：sel=1，做减法 a-b（1-2 在 5 位补码下为 -1）
        sel = 1'b1;
        #10;
        $display("sel=1  a=%0d b=%0d -> out1=%0d (期望 31, 即 5'b11111)", a, b, out1);

        // 用例 3：换一组数据做加法
        a = 5'd5; b = 5'd3; sel = 1'b0;
        #10;
        $display("sel=0  a=%0d b=%0d -> out1=%0d (期望 8)", a, b, out1);

        $finish;
    end
endmodule
```

2. 把 `tb_arith.v` 与原文件 `MY_DESIGN.v` 放在同一目录（仿真器需要同时看到 `ARITH` 的定义）。
3. 用任意 Verilog 仿真器编译并运行，例如（命令本身 **待本地验证**，取决于你装的工具）：
   - Icarus Verilog：`iverilog -o tb tb_arith.v MY_DESIGN.v && vvp tb`
   - 或 ModelSim/VCS/Verilator 等价流程。

**需要观察的现象**：

- `sel=0` 时 `out1` 应等于 `a+b`（组合逻辑，输入变后约 10ns 后输出更新）。
- `sel=1` 时 `out1` 应等于 `a-b`；当 `a=1, b=2` 时，结果是 5 位补码 `-1`，即 `5'b11111`，按无符号十进制打印就是 **31**——这是位宽溢出/补码的典型现象，值得留意。

**预期结果**（基于静态逻辑推导；实际波形 **待本地验证**）：

```
sel=0  a=1 b=2 -> out1=3 (期望 3)
sel=1  a=1 b=2 -> out1=31 (期望 31, 即 5'b11111)
sel=0  a=5 b=3 -> out1=8 (期望 8)
```

> 关键收获：写 testbench 时，激励信号（`a/b/sel`）必须是 `reg`，被测输出（`out1`）必须用 `wire` 接——这正好呼应 4.2 节的铁律。如果你打印出的 `out1` 与期望一致，说明你已经真正读懂了 `ARITH` 的组合逻辑与 case 分支。

## 6. 本讲小结

- **模块与端口**：`MY_DESIGN.v` 用非 ANSI 风格声明模块，先列端口名，再分方向（`input/output`），位宽统一为 `[4:0]`。
- **reg 与 wire 铁律**：在 `always` 块内被赋值的信号用 `reg`；由例化端口或 `assign` 驱动的连线用 `wire`。`reg` 不等于触发器。
- **两类 always 块**：`always @(posedge clk)` 是时序逻辑（综合成寄存器，用 `<=`）；`always @(电平敏感列表)` 是组合逻辑（本文件为教学旧式写法，新代码推荐 `always @(*)`）。
- **层次化例化**：顶层 `MY_DESIGN` 例化 `ARITH` 与 `COMBO`，`COMBO` 内部又例化 `ARITH`；同一模块多次例化即多份独立硬件。
- **case 实现选择**：`ARITH` 用 `case({sel})` 在加法与减法间切换，是组合选择器的典型写法。

## 7. 下一步学习建议

- 读懂单模块之后，建议进入 [u2-l2 层次化设计与真实 SoC 示例](u2-l2-hierarchical-soc-cmsdk.md)，看一个真实 SoC（ARM Cortex-M0 CMSDK）的顶层与 AHB 总线接口，体会从 `MY_DESIGN` 到工业级 SoC 的复杂度跃迁。
- 若想了解「这段 RTL 如何被约束住时序」，接着读 [u2-l3 时序约束入门：SDC 与环境约束](u2-l3-sdc-timing-constraints.md)，它会用同目录的 `My_Design.cons` 讲 `create_clock`、`set_input_delay` 等 SDC 概念。
- 自己动手：把第 5 节的 testbench 扩展到也给 `COMBO` 写一个，观察 `Cout` 是否符合「先 `ARITH` 加减、再加 `Cin1`」的预期。
