# 赋值风格、三元运算符与逻辑设计

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 Verilog 里阻塞赋值（`=`）与非阻塞赋值（`<=`）的差别，并知道哪一种必须用在组合块、哪一种必须用在时钟块。
- 把一段难读的「嵌套三元」逻辑，改写成清晰的「链式三元 + 阻塞赋值」多行写法。
- 把布尔表达式写成等式比较（`(A == 1'b1)`），并能根据「独立输入项数」粗略估算一段逻辑要占多少个 6 输入 LUT。

这三件事是本书 Verilog 规范里最常被用到的三块「肌肉记忆」，也是后续读懂 `Pipeline_Skid_Buffer` 等复杂模块的钥匙。

## 2. 前置知识

本讲承接 [u2-l1 受限的 Verilog-2001 与 default_nettype](./u2-l1-verilog2001-and-default-nettype.md)。在那篇里我们已经确认：

- 本书只用 **Verilog-2001 的可综合子集**。
- 每个 `.v` 文件开头写 `` `default_nettype none ``，端口显式声明方向与 `wire`/`reg` 类型。
- 信号只用 `reg` 和 `wire`，逻辑值尽量只用 `0/1`，所有寄存器都要初始化为不含 `X/Z` 的值。

本讲要回答的下一个问题是：**声明完模块和端口之后，模块体内的赋值到底怎么写？** 我们聚焦三件事：

- **赋值**：`always` 块里到底用 `=` 还是 `<=`？
- **选择**：条件逻辑用 `if/else` 还是三元 `?:`？
- **逻辑形态**：布尔式怎么写最清楚？会占多少硬件？

先建立两个直觉，再读源码就不慌：

- **阻塞赋值 `=` 像顺序执行的语句**：这一行算完、立刻写进去，下一行能看到新值。它让我们能把复杂逻辑「拆成几行一步步算」。
- **非阻塞赋值 `<=` 像拍一张并行快照**：所有右侧先采样，等这个时间步结束时才统一写入，写进去的新值要到下一个时钟边沿才被别人看见。它天然描述「寄存器」。

> 名词小贴士：
> - **组合逻辑（combinational）**：输出随时随输入变，没有时钟，没有记忆。本书用 `always @(*)` 描述。
> - **时序逻辑（sequential/clocked）**：输出在时钟边沿更新，有记忆（寄存器）。本书用 `always @(posedge clock)` 描述。
> - **LUT（Look-Up Table，查找表）**：FPGA 实现组合逻辑的基本单元。一个「6 输入 LUT（6-LUT）」能实现任意一个不超过 6 个输入的布尔函数。
> - **race condition（竞争）**：多个事件在同一时刻发生、但求值顺序不确定，导致结果不确定。

## 3. 本讲源码地图

本讲只盯两个文件：

| 文件 | 作用 | 本讲用到的地方 |
| --- | --- | --- |
| `verilog.html` | 全书 Verilog 编码规范正文 | 赋值铁律、向后依赖、三元链式、布尔式、LUT 估算 |
| `Pipeline_Skid_Buffer.v` | 一个把上述规范用到极致的真实模块 | 组合块阻塞赋值、链式三元、等式比较布尔式 |

`Pipeline_Skid_Buffer`（滑动缓冲）是一个「最小双-entry FIFO」式的握手流水线模块。它的控制通路是一台小型状态机，几乎是本讲所有惯用法的活标本——规范正文里讲到状态机时也直接点名「去看 Skid Buffer」。本讲我们**只看它的赋值风格和条件逻辑写法**，状态机本身的细节留给 [u10-l1](./u10-l1-skid-buffer-fsm.md)。

## 4. 核心概念与源码讲解

### 4.1 阻塞与非阻塞赋值

#### 4.1.1 概念说明

Verilog 的过程赋值有两种：

- **阻塞赋值 `=`**：右侧求值后**立即**写入左侧，本块里随后的语句立刻看到新值。名字里的「阻塞」指的是它「挡住」了本块后续语句，直到赋值完成。
- **非阻塞赋值 `<=`**：右侧先求值（采样），但**不立刻写**，而是排队；等到当前时间步结束时，所有排队的非阻塞赋值才**统一**写入。于是块内各行的右侧用的都是「旧值」，写进去的新值要到下一个时钟边沿才被别人看到。

本书把它们和两种 `always` 块**一一对应**，形成一条铁律：

- 组合块 `always @(*)` 里**只用阻塞 `=`**。
- 时钟块 `always @(posedge clock)` 里**只用非阻塞 `<=`**。
- **不要**在同一个 `always` 块里混用两种赋值。

为什么这样配？关键不是「语法不允许」，而是「配错了仿真和综合会对不上」：

- 在时钟块里用阻塞赋值，会引入仿真器求值顺序导致的 **race condition**——同样的代码在不同仿真器、或仿真与综合之间给出不同结果。
- 在组合块里用非阻塞赋值，部分仿真器会拒绝或给出不一致结果（Verilator 的 `COMBDLY` 警告就是冲这个来的）。

反过来，阻塞赋值在组合块里还有一个**正面用途**：把一段复杂逻辑拆成几行中间变量，逐步算下去。这是本书最常用的设计范式。

#### 4.1.2 核心流程

两种赋值的执行语义可以这样对比：

```text
阻塞 =  （顺序执行）
  右侧求值 --> 立刻写入左侧 --> 下一行看到新值
  适合：在组合块里「先算 part_one，再用 part_one 算 part_two」

非阻塞 <= （并行快照）
  右侧求值(采样旧值) --> 排入队列 --> 本时间步结束时统一写入
  适合：在时钟块里描述「一排寄存器在同一时钟边沿一起更新」
```

由此衍生出本书推荐的**「组合块算 + 紧跟一个时钟块存」**范式：

```text
always @(*)        组合块：用阻塞 = 把复杂逻辑拆成几行，算出 *_next
always @(posedge clock)  时钟块：用非阻塞 <= 把 *_next 存进寄存器
```

好处是：要不要给某段逻辑加流水线寄存器，只需决定第二个块是不是时钟块，而**不用改动逻辑本身或代码排版**。

> **向后依赖（backward dependency）——组合块里要避免的坑**
>
> 在一个用阻塞赋值的组合块里，如果**前一行**用到了一个值，而该值要到**后面一行**才被赋值，就是「向后依赖」。此时仿真和综合虽然都「正确」，但**可能不一致**：仿真可能看到组合后的新值，而真实硬件实现的是旧值。你会在仿真里看到「功能正确但和硬件对不上」的诡异现象。
>
> 解决办法二选一：**重排赋值顺序**，或**把那一行拆到另一个 `always` 块**。

#### 4.1.3 源码精读

规范正文先把铁律写成两条列表项：

[verilog.html:L436-L441](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L436-L441) —— 组合块用 `=`、时钟块用 `<=` 的两条铁律。

紧接着给出「为什么组合块只用阻塞」的两个理由（综合器可能误报无用寄存器；阻塞赋值在时钟块里引发 race condition）：

[verilog.html:L443-L461](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L443-L461) —— 阻塞赋值只在组合块使用的两条理由。

以及「为什么时钟块只用非阻塞」：

[verilog.html:L463-L469](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L463-L469) —— 非阻塞赋值只在时钟块使用的理由，并点名 Verilator 的 `COMBDLY` 警告。

「向后依赖」的告警框：

[verilog.html:L488-L495](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L488-L495) —— 出现向后依赖时的处理：重排顺序或拆块。

「组合块算 + 时钟块存」范式的代码示例：

[verilog.html:L509-L528](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L509-L528) —— 上半部分是 `always @(*)` 用阻塞赋值拆出 `part_one`/`part_two`，下半部分是 `always @(posedge clock)` 用非阻塞赋值把结果存入寄存器。

现在看真实模块。`Pipeline_Skid_Buffer` 的数据通路选择逻辑就是一个最朴素的组合块 + 阻塞赋值：

[Pipeline_Skid_Buffer.v:L164-L166](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L164-L166) —— 用 `always @(*)` 和阻塞 `=` 选择「用缓冲数据还是输入数据」：

```verilog
always @(*) begin
    selected_data = (use_buffered_data == 1'b1) ? data_buffer_out : input_data;
end
```

算出来的 `selected_data` 随后被送进一个 `Register` 实例（见 [Pipeline_Skid_Buffer.v:L168-L180](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L168-L180)）去寄存——这正是「组合块算 + 寄存器存」范式，只不过「存」这一步被封装进了 `Register` 子模块（其内部时钟块用 `<=`，见 [verilog.html:L889-L898](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L889-L898)），而不是写成一个内联的 `always @(posedge clock)`。

控制通路里计算「插入/取出」两个握手事件的块，同样是组合块 + 阻塞赋值：

[Pipeline_Skid_Buffer.v:L328-L331](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L328-L331) —— 用阻塞 `=` 同时算出 `insert` 和 `remove`，两行互不依赖、无向后依赖。

整篇 `Pipeline_Skid_Buffer` 里**所有** `always @(*)` 块都只用阻塞 `=`；所有「寄存」都交给 `Register` 子模块用非阻塞 `<=` 完成。这并非巧合，而是规范要求的结果。

#### 4.1.4 代码实践

**实践目标**：亲手感受「向后依赖」如何让仿真与硬件对不上，并学会修复。

**操作步骤**：

1. 阅读下面这段**示例代码**（不是项目原有代码），它故意写了一个向后依赖：

   ```verilog
   // 示例代码：含向后依赖的组合块
   always @(*) begin
       sum      = a + partial;   // 前一行用到了 partial
       partial  = b + c;         // 但 partial 在后面一行才被赋值
   end
   ```

2. 用你手头的仿真器（如 Icarus Verilog / Verilator）跑一下，观察 `sum` 在 `b`/`c` 变化的**当拍**是否立刻更新。
3. 按「重排顺序」修复：把 `partial` 的赋值移到 `sum` 之前：

   ```verilog
   // 示例代码：修复后，无向后依赖
   always @(*) begin
       partial = b + c;
       sum     = a + partial;
   end
   ```

**需要观察的现象**：修复前，`sum` 可能要等到下一拍才反映 `b`/`c` 的新值（仿真器先求值后赋值的细节决定）；修复后，`sum` 在当拍立刻正确。

**预期结果**：修复后，仿真行为与综合出的硬件一致。

> **待本地验证**：具体「当拍是否更新」取决于你所用仿真器对阻塞赋值求值顺序的实现，请以本地仿真输出为准。重点是体会：同样的逻辑、不同的行序，仿真可以不同。

#### 4.1.5 小练习与答案

**练习 1**：下面这段在 `always @(posedge clock)` 里用了阻塞赋值，为什么不好？

```verilog
always @(posedge clock) begin
    a = b;
    c = a;
end
```

**参考答案**：时钟块里用阻塞 `=` 会引发 race condition——`c` 是否拿到「新的 `a`」取决于仿真器对本块和别处 `a` 的求值顺序，仿真与综合、或不同仿真器之间可能给出不同结果。时钟块应改用非阻塞 `<=`。

**练习 2**：在 `Pipeline_Skid_Buffer.v` 里找出全部 `always @(*)` 块，确认它们用的都是阻塞 `=`；再找出全部「寄存」发生的地方，确认它们用的都是非阻塞 `<=`（提示：寄存被封装在 `Register` 实例里）。

**参考答案**：`always @(*)` 块共四处（`selected_data` L164、`insert/remove` L328、`load..pass` L350、`state_next` L363、控制信号 L391，按代码实为五处），全部用 `=`；寄存则由 `data_buffer_reg`/`data_out_reg`/`input_ready_reg`/`output_valid_reg`/`state_reg` 五个 `Register` 实例完成，其内部时钟块（[verilog.html:L889-L898](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L889-L898)）用 `<=`。

---

### 4.2 三元运算符与链式写法

#### 4.2.1 概念说明

条件逻辑有两种写法：`if/else` 语句和三元运算符 `cond ? a : b`。本书的规矩是：**除非迫不得已，一律用三元，不用 `if/else`**。规范给出四条理由：

1. **防锁存器**：`if` 写了一个分支却漏写另一个，综合会推断出锁存器（latch）。三元天生两分支都写全，从语法上杜绝这个错误。
2. **条件赋值给常量的唯一手段**：给 `localparam`/`parameter` 做条件赋值，只能用三元，`if/else` 不行。
3. **可链式化**：嵌套的 `case/if/else` 可以改写成「链式三元」的多行阻塞赋值，更紧凑、更好读（注意要配合 4.1 的阻塞赋值，并当心向后依赖）。
4. **正确传播 X**：仿真里 `if/else` 把 `X/Z` 当成假，于是 X 被「吃掉」、不传播，结果出乎意料；三元则会如实地把 X 传播下去。

`if/else` 仍然不可或缺的少数场合：`generate` 块里条件例化逻辑、某些厂商模板为推断特定硬件、以及个别复位代码。

#### 4.2.2 核心流程

核心规矩只有一句：**永远不要嵌套三元**（即让三元的一个分支本身又是三元）。把嵌套拆成「链式」：

```text
// 反例（嵌套三元，难读）
result = (foo == 1'b1) ? ((bar == 1'b1) ? A : B) : C;

// 正解（链式：两行阻塞赋值，第二行用第一行的结果）
partial = (bar == 1'b1) ? A       : B;
result  = (foo == 1'b1) ? partial : C;
```

链式的好处会随着条件增多而放大：

- `partial` 成了仿真里**可观测的中间信号**，调试时能看到。
- 条件可以**任意扩展**到 3 个、7 个、更多，每行一个，按顺序往下传。
- 它天然成为**有限状态机（FSM）**的编程范式：从「当前状态」出发，逐条测试转移条件，命中就改成下一状态，全部不命中就保持原状态。

注意：链式依赖前一行结果，所以它必须用**阻塞赋值**，并且**不能出现向后依赖**（4.1 已述）。

#### 4.2.3 源码精读

规范先列「三元优于 if/else」的四条理由：

[verilog.html:L582-L629](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L582-L629) —— 三元优于 `if/else` 的四条理由，含「`if` 漏分支生锁存器」「`if` 在仿真里吃掉 X」等说明。

随后是「绝不嵌套、改链式」的规矩和对照例子：

[verilog.html:L631-L654](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L631-L654) —— 把嵌套三元拆成两行链式阻塞赋值的正反对照。

最精彩的活样本在 `Pipeline_Skid_Buffer` 的状态转移逻辑里。它把「7 条转移规则」写成 7 行链式三元，每一行都把上一行的 `state_next` 当输入往下传，全部不命中就保持 `state` 不变：

[Pipeline_Skid_Buffer.v:L363-L371](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L363-L371) —— 用 7 行链式三元 + 阻塞赋值计算下一状态：

```verilog
always @(*) begin
    state_next = (load   == 1'b1) ? BUSY  : state;
    state_next = (flow   == 1'b1) ? BUSY  : state_next;
    state_next = (fill   == 1'b1) ? FULL  : state_next;
    state_next = (flush  == 1'b1) ? BUSY  : state_next;
    state_next = (unload == 1'b1) ? EMPTY : state_next;
    state_next = (dump   == 1'b1) ? FULL  : state_next;
    state_next = (pass   == 1'b1) ? FULL  : state_next;
end
```

逐行读：第一行先令 `state_next` 默认等于当前 `state`（保持）；其后每行用一个转移条件去「覆盖」它。这就是规范正文里说的「从一个寄存器出发，顺序测试、把更新后的值往下传；全部不命中则保持不变」的 FSM 范式，也是「最后赋值胜出（last assignment wins）」的体现。算出的 `state_next` 再交给一个 `Register` 实例寄存（[Pipeline_Skid_Buffer.v:L373-L385](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L373-L385)）。

而最简单的「单行三元」例子是数据选择：

[Pipeline_Skid_Buffer.v:L164-L166](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L164-L166) —— `(use_buffered_data == 1'b1) ? data_buffer_out : input_data`，一个条件两分支，干净利落。

#### 4.2.4 代码实践

**实践目标**：把一段嵌套三元改写成两行链式阻塞赋值（本讲指定实践任务的第一部分）。

**操作步骤**：

1. 阅读下面这段**示例代码**（嵌套三元，难读）：

   ```verilog
   // 示例代码：嵌套三元
   always @(*) begin
       result = (mode == 2'b00) ? ((en == 1'b1) ? din : 8'h00) : dout;
   end
   ```

2. 按链式规矩，拆成两行：先算内层三元到一个中间变量，外层再用它。

   ```verilog
   // 示例代码：链式改写
   always @(*) begin
       inner = (en    == 1'b1) ? din  : 8'h00;
       result = (mode == 2'b00) ? inner : dout;
   end
   ```

3. 对照 `Pipeline_Skid_Buffer.v` 的 `state_next` 块（L363-L371），体会「链式可以扩展到任意多行」。

**需要观察的现象**：改写后逻辑等价，但 `inner` 成为可在波形里观察的中间信号；条件变多时只需新增一行，不必再往里嵌套。

**预期结果**：两段代码综合结果一致，链式版本更易读、易调试。

> **待本地验证**：可用仿真器对两种写法施加相同激励，比对 `result` 波形是否完全一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么本书禁止「嵌套三元」？给出两条理由。

**参考答案**：（1）嵌套三元可读性极差，下一个读者难以还原逻辑意图；（2）拆成链式后，中间变量成为仿真可观测信号，且可任意扩展条件数量，是 FSM 等复杂逻辑的推荐范式。

**练习 2**：在 `Pipeline_Skid_Buffer.v` 的 `state_next` 块里，如果 7 个转移条件一个都不命中，`state_next` 的值是什么？为什么？

**参考答案**：等于 `state`（当前状态）。因为第一行把 `state_next` 初始化为 `state`，后续每行只在命中时改写它；全不命中则一路传递保持为 `state`，即「状态不变」。

---

### 4.3 布尔表达式与 LUT 估算

#### 4.3.1 概念说明

**布尔表达式怎么写**：本书要求把布尔值写成**与期望值的等式/不等式比较**，而不是直接用位值做按位运算。对照：

```verilog
// 不推荐
C = A & ~B;

// 推荐
C = (A == 1'b1) && (B == 1'b0);
```

好处有三：意图清晰（一眼看出「A 为真且 B 为假」）；不必死记每个信号的高/低有效极性；位宽显式，能避免一些 bug 和告警。需要取反一个比较时，用**逻辑非 `!`**（恒返回 1 位），不要用按位取反 `~`。

**LUT 怎么估算**：设计逻辑时，要心里有数——生成某一位输出需要多少个**独立输入项**，再对照目标 FPGA 的 LUT 容量。以 6 输入 LUT（6-LUT）为例：

- 任何不超过 6 个输入项的布尔函数，通常都能装进**一个 6-LUT**（每位输出一个）。
- 把逻辑组织成「≤6 项的表达式 + 寄存器」交替，能最小化逻辑与互连延迟，给综合器更大的布局布线自由度。
- 一个被寄存的 4:1 多路选择器可以「免费」加一级寄存器——因为它正好一个 LUT 装得下，寄存器是顺带的。

#### 4.3.2 核心流程

估算一段组合逻辑的 LUT 用量，用「数独立输入项」法：

```text
1. 列出决定该位输出的所有独立输入信号（含数据位和选择/控制位）。
2. 一个 6-LUT 最多吃 6 个输入项。
3. LUT 数 ≈ ceil(输入项数 / 6)（每位输出），再乘以输出位宽。
```

对多路选择器，输入项数有一个简洁公式。一个 \(N{:}1\) 的 mux（\(N\) 为 2 的幂）的输入项数为：

\[
\text{输入项} = N + \log_2 N
\]

即 \(N\) 个数据位加上 \(\log_2 N\) 个选择位。于是：

| mux | 数据位 | 选择位 | 输入项 | 6-LUT 数（每输出位） |
| --- | --- | --- | --- | --- |
| 4:1 | 4 | 2 | 6 | 1 |
| 8:1 | 8 | 3 | 11 | 2（11 项装不下一个 6-LUT） |

规范还提醒：**警惕宽于 8:1 的 mux**，别把逻辑设计成「从一大堆选项里做一次大选择」，更好的做法是**把一串小选择流水线化**。

#### 4.3.3 源码精读

规范对布尔式的写法约定：

[verilog.html:L563-L580](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L563-L580) —— 用等式比较 `(A == 1'b1) && (B == 1'b0)` 代替按位 `A & ~B`，并提醒取反用 `!` 而非 `~`。

LUT 估算与 4:1 mux 例子：

[verilog.html:L656-L674](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L656-L674) —— 6-LUT 容量、4:1 mux 恰好 6 个输入项、警惕宽于 8:1 的 mux。

真实模块里的等式比较写法，最典型的是握手事件计算：

[Pipeline_Skid_Buffer.v:L328-L331](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L328-L331) —— `(input_valid == 1'b1) && (input_ready == 1'b1)`，用 `==` 显式比较、用 `&&` 逻辑与，正是规范推荐的布尔式写法：

```verilog
always @(*) begin
    insert = (input_valid  == 1'b1) && (input_ready  == 1'b1);
    remove = (output_valid == 1'b1) && (output_ready == 1'b1);
end
```

同样风格还出现在转移条件计算里（[Pipeline_Skid_Buffer.v:L350-L358](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L350-L358)），每条转移都是几个 `(x == 1'b1)` 用 `&&` 串起来。

模块末尾的注释给出了一个真实设计的资源账本，可作为 LUT 估算的对照：

[Pipeline_Skid_Buffer.v:L399-L402](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L399-L402) —— 64 位连接下，skid buffer 用 128 个寄存器做缓冲，外加 4–9 个寄存器（及配套 LUT）做 FSM 与接口输出。

#### 4.3.4 代码实践

**实践目标**：估算一个 4:1 mux 占多少个 6-LUT，并推广到 8:1（本讲指定实践任务的第二部分）。

**操作步骤**：

1. 对一个 4:1 mux，数独立输入项：4 个数据输入 + 2 个选择位 = 6 项。
2. 套用公式：6 项 ÷ 6-LUT 容量 6 = **1 个 6-LUT**（每输出位）。
3. 若输出是 8 位宽，则需 \(1 \times 8 = 8\) 个 6-LUT。
4. 推广到 8:1 mux：8 数据 + 3 选择 = 11 项；\(\lceil 11/6 \rceil = 2\) 个 6-LUT（每输出位）。

**需要观察的现象**：4:1 mux 恰好填满一个 6-LUT，因此「免费」可被寄存；8:1 mux 已经需要两级 LUT，关键路径会变长。

**预期结果**：4:1 → 1 个 6-LUT/位；8:1 → 2 个 6-LUT/位。这与规范正文「警惕宽于 8:1 的 mux」的告诫一致。

> **待本地验证**：实际 LUT 数会因可断裂 LUT（fracturable LUT）、逻辑打包（packing）和综合器优化而略有出入，可用综合报告里的 LUT 占用数对照。

#### 4.3.5 小练习与答案

**练习 1**：把 `C = A & ~B` 改写成规范推荐的等式比较形式。

**参考答案**：`C = (A == 1'b1) && (B == 1'b0);`

**练习 2**：一个 16:1 mux 在 6-LUT 的 FPGA 上，每位输出大约需要几个 LUT？为什么规范劝你「别设计成一次大选择」？

**参考答案**：16 数据 + 4 选择 = 20 项，\(\lceil 20/6 \rceil = 4\) 个 6-LUT/位，关键路径明显变长。规范建议把一次大选择拆成一串小选择并流水线化，让每级都落在 ≤6 项的一个 LUT 内，从而提速。

---

## 5. 综合实践

把本讲三块内容串起来做一个小任务。

**任务**：给定下面这段「集齐所有毛病」的**示例代码**——`if/else`、嵌套三元、按位布尔式混用——请按本书规范重写它，并估算重写前后某一位输出的 LUT 用量。

```verilog
// 示例代码：待重构
always @(posedge clock) begin
    if (sel == 1'b1) begin
        y <= (a == 1'b1) ? ((b == 1'b1) ? d1 : d2) : d3;
    end else begin
        y <= d4;
    end
end

always @(*) begin
    z = x & ~w;
end
```

**建议改写要点**：

1. 把 `if/else` 选择拆成「组合块算 `y_next` + 时钟块用 `<=` 存 `y`」，选择逻辑用**链式三元**：
   - 先 `inner = (b == 1'b1) ? d1 : d2;`
   - 再 `mid   = (a == 1'b1) ? inner : d3;`
   - 再 `y_next = (sel == 1'b1) ? mid : d4;`
2. 把 `z = x & ~w;` 改成等式比较：`z = (x == 1'b1) && (w == 1'b0);`
3. 估算 `y` 这一路某一位的输入项：涉及 `sel, a, b, d1, d2, d3, d4` 共 7 个输入项 → \(\lceil 7/6 \rceil = 2\) 个 6-LUT/位；若能复用 `d*` 或简化条件，可压回 1 个。

**完成后自检**：

- 组合块是否全部用阻塞 `=`？时钟块是否用非阻塞 `<=`？两者是否分离？
- 三元是否都「链式」、无嵌套？
- 布尔式是否都写成 `==` 比较？
- LUT 估算是否与「数输入项」法一致？

> **待本地验证**：用仿真器对重构前后施加相同激励，比对 `y`/`z` 波形是否一致；再用综合器看 LUT 占用是否与估算相符。

## 6. 本讲小结

- **赋值铁律**：组合块 `always @(*)` 只用阻塞 `=`；时钟块 `always @(posedge clock)` 只用非阻塞 `<=`；不在同一块内混用。配错的代价是仿真与综合对不上。
- **阻塞赋值的正面用途**：在组合块里把复杂逻辑拆成几行中间变量逐步算；当心「向后依赖」，出现就重排顺序或拆块。
- **「组合块算 + 时钟块存」范式**：复杂逻辑先在 `always @(*)` 算出 `*_next`，再在紧跟的时钟块（或 `Register` 子模块）里寄存，是否流水线化只改第二块的触发方式。
- **三元优于 if/else**：防锁存器、可条件赋值常量、可链式、正确传播 X；但**绝不嵌套三元**，改写成「链式三元 + 阻塞赋值」。
- **布尔式写等式比较**：用 `(A == 1'b1) && (B == 1'b0)` 代替 `A & ~B`，取反用 `!`。
- **LUT 估算**：数独立输入项，6 项以内一个 6-LUT；\(N{:}1\) mux 输入项 \(= N + \log_2 N\)，4:1 恰好 1 个 6-LUT、8:1 需 2 个，宽于 8:1 应改流水线小选择。

## 7. 下一步学习建议

- 本讲提到的「最后赋值胜出」和复位封装，是下一讲 [u3-l2 复位哲学与 Register 模块](./u3-l2-resets-and-register-module.md) 的主题，建议紧接着读。
- 想看本讲的链式三元与「组合块算 + 时钟块存」范式在一台真实状态机里如何施展，可提前翻 [u10-l1 Skid Buffer 与 COTTC FSM 方法](./u10-l1-skid-buffer-fsm.md)，并把 `Pipeline_Skid_Buffer.v` 完整读一遍。
- 若想了解 `Register` 子模块内部如何用非阻塞 `<=` 和异步复位封装寄存器，可读 `Register.v` 与 `verilog.html` 的 Resets 一节（本讲只引用了其中的时钟块片段）。
