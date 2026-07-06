# 控制时序与握手信号

> 本讲是「流水线集成与控制时序」单元的第 3 讲。建议先读完 u4-l1（五级流水线数据流串讲）与 u4-l2（输出排序模块），再进入本讲。

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `FFT.v` 里那几个 `always` 块各自负责什么，并区分「时序块（posedge clk）」与「组合块（always @*）」的分工。
- 看懂第 5 级蝶形的状态 `no5_state` 是如何由 `r4_valid` 与 `s5_count` 共同生成的，以及为什么它「不走」寄存器。
- 理解 `count_y → y_1 → y_1_delay` 这条计数链如何既驱动排序写入、又驱动顺序读出。
- 解释 `over` 完成标志如何触发 `out_valid` 握手，以及 `dout` 是如何被寄存输出的。
- 掌握 `next_xxx` 命名约定背后「组合算下一拍、时钟边沿打进去」的经典时序电路写法。

## 2. 前置知识

本讲只盯着顶层模块 [RTL/FFT.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v) 里**最后那几段 `always` 代码**，但你需要先建立两个直觉：

1. **寄存器与组合逻辑的差别（数字电路基础）**。
   - `reg` 在 Verilog 里只是一个「能被 always 赋值」的类型，**不代表它一定是触发器**。写在 `always @(posedge clk)` 里的是真正的 D 触发器（有时序）；写在 `always @(*)` 里的是组合逻辑（输出随输入立刻变化，没有记忆）。
   - 本项目用一套很规整的写法：要存的状态声明成 `xxx`，它的「下一拍值」声明成 `next_xxx`，组合块算 `next_xxx`，时序块在时钟边沿把 `next_xxx` 塞进 `xxx`。这套写法等价于把「状态寄存器」和「下态逻辑」分开画，是工业上很常见的风格。

2. **第 5 级为什么特殊（承接 u4-l1）**。
   - 前 4 级蝶形的 2 位状态 `state` 都来自对应的 `ROM_N` 模块（ROM_16/8/4/2，见 u3-l4）。
   - 第 5 级没有 ROM，旋转因子被写死成 `24'd256`（即定点 1+j0），它的 `state` 改由顶层用一个叫 `no5_state` 的信号生成。本讲的一大重点，就是讲清楚这个 `no5_state` 是怎么「凭空」造出来的。

> 关键术语回顾（来自 u3-l2）：radix2 蝶形单元靠 2 位 `state` 分时复用——`2'b00` 等待、`2'b01` first half（算和/差）、`2'b10` second half（差乘旋转因子），进入 `01`/`10` 时 `outvalid` 拉高。本讲的 `no5_state` 就是给第 5 级蝶形送这 2 位 `state` 的。

## 3. 本讲源码地图

本讲只涉及一个文件，但会反复跳到它的不同片段：

| 代码位置 | 作用 |
|---|---|
| [RTL/FFT.v:L25-L62](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L25-L62) | 模块端口与本讲相关的 `reg`/`wire` 声明（`count_y`、`no5_state`、`s5_count`、`over`、`assign_out` 等） |
| [RTL/FFT.v:L230-L243](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L230-L243) | 第 5 级 `radix_no5` 的例化：`.state(no5_state)`、旋转因子写死、`outvalid()` 悬空 |
| [RTL/FFT.v:L254-L289](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L254-L289) | **时序块** `always @(posedge clk or posedge reset)`：复位与所有寄存器的打拍更新 |
| [RTL/FFT.v:L290-L311](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L290-L311) | **控制组合块**：算出 `no5_state`、`next_r4_valid`、`next_s5_count`、`next_count_y`、`next_dout_x` |
| [RTL/FFT.v:L313-L321](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L313-L321) | **排序组合块的开头**：生成 `next_over`、`next_out_valid`（大 `case` 的排序细节归 u4-l2） |
| [RTL/FFT.v:L450-L454](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L450-L454) | 排序表最后一格 `5'd31`：在这里把 `next_over` 拉成 1，触发收尾握手 |

> 说明：`FFT.v` 里其实有**三个** `always` 块（一个时序 + 两个组合）。规范里说的「两个 always 块」指的是**时序块**和**控制组合块**这两个本讲的主角；第三个组合块（排序大 `case`）已在 u4-l2 详讲，本讲只取它「生成 `next_over` / `next_out_valid`」那一小段作为 `out_valid` 握手链的一环。

## 4. 核心概念与源码讲解

### 4.1 三个 always 块的总览与时序/组合分工

#### 4.1.1 概念说明

一个 FFT 顶层模块要同时管三件事：① 把数据一级级算下去（流水线例化，u4-l1 讲过）；② 在合适的时刻给第 5 级蝶形送状态、并统计输出个数（控制时序）；③ 把乱序输出排成自然顺序（排序，u4-l2 讲过）。后两件事就落在那几个 `always` 块上。

把这三件事拆开看，对应关系是：

| always 块 | 敏感表 | 性质 | 负责的事 |
|---|---|---|---|
| 第 1 个 | `posedge clk or posedge reset` | **时序**（触发器） | 复位 + 把所有 `next_xxx` 打进寄存器 |
| 第 2 个 | `@(*)` | **组合** | 控制逻辑：`no5_state`、`next_r4_valid`、`next_s5_count`、`next_count_y`、`next_dout_x` |
| 第 3 个 | `@(*)` | **组合** | 排序逻辑：`result_r_ns`、`next_over`、`next_out_valid`（u4-l2） |

#### 4.1.2 核心流程

时序块与组合块的协同，可以用下面这张「数据流」图概括：

```
        ┌──────────── 组合块 (always @*) ─────────────┐
当前态   │ next_xxx = f(当前态, 输入)                    │
xxx  ──► │                                              │ ──► next_xxx
        └──────────────────────────────────────────────┘
                              │
                              │ （下一个时钟边沿采样）
                              ▼
        ┌──────────── 时序块 (posedge clk) ────────────┐
        │ xxx <= next_xxx;   （打一拍，变成新的当前态）  │
        └──────────────────────────────────────────────┘
                              │
                              └──► 回到组合块，形成反馈环
```

这套「组合算 `next_xxx`、时序打拍」的写法，把**组合逻辑**和**状态寄存器**物理隔离，便于综合、便于阅读，也便于后面做时序约束（u6-l2）。

#### 4.1.3 源码精读

时序块（[RTL/FFT.v:L254-L289](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L254-L289)）的开头是异步、高有效复位：

```verilog
always@(posedge clk or posedge reset)begin
    if(reset)begin
        ...            // 见 4.4.3
    end
    else begin
        din_r_reg <= {{4{din_r[11]}},din_r,8'b0};   // 12→24bit 符号扩展+左移
        in_valid_reg <= in_valid;
        s5_count    <= next_s5_count;
        r4_valid    <= next_r4_valid;
        count_y     <= next_count_y;
        assign_out  <= next_out_valid;
        over        <= next_over;
        y_1_delay   <= y_1;
        dout_r      <= next_dout_r;
        dout_i      <= next_dout_i;
        ...
    end
end
```

注意观察：左边全是「真正要存下来的状态」（`s5_count`、`r4_valid`、`count_y`、`assign_out`、`over`、`y_1_delay`、`dout_x`），右边全是 `next_xxx` 或外部信号。这就是「组合块负责算、时序块负责存」的典型样子。

#### 4.1.4 代码实践

**目标**：在源码里把「寄存器」和「它的下态信号」成对找出来。

**步骤**：
1. 打开 [RTL/FFT.v:L254-L289](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L254-L289)，逐行看 `else` 分支。
2. 对每个形如 `A <= next_A;` 的语句，在 [RTL/FFT.v:L290-L311](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L290-L311) 或排序块里找到 `next_A = ...` 的生成处。

**需要观察的现象**：你会得到一张「寄存器 ↔ 下态信号」对照表。预期会发现 `s5_count↔next_s5_count`、`r4_valid↔next_r4_valid`、`count_y↔next_count_y`、`assign_out↔next_out_valid`、`over↔next_over`、`dout_r↔next_dout_r` 等成对关系。

**预期结果**：除掉 `din_r_reg`、`in_valid_reg`、`y_1_delay` 这几个「直接打拍」的简单流水寄存器外，其余寄存器都遵循 `next_xxx` 约定。

#### 4.1.5 小练习与答案

**练习 1**：`no5_state` 也声明成了 `reg [1:0]`，它出现在时序块的 `xxx <= next_xxx` 列表里吗？

**答案**：不在。`no5_state` 只在第 2 个组合块（[L296-L298](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L296-L298)）里被赋值，时序块的 `else` 分支里没有 `no5_state <= ...`，复位分支里也没有。所以它虽然叫 `reg`，其实是**纯组合**输出，这是它「打破 `next_xxx` 约定」的关键，4.2 节会详细讲。

**练习 2**：为什么本项目要拆成「时序块 + 组合块」两段，而不是把所有逻辑写进一个 `always @(posedge clk)`？

**答案**：分开后，组合逻辑（下态计算）和状态寄存器边界清晰，综合工具更容易做时序分析，读者也更容易区分「这一段是算」还是「这一段是存」。代价是信号数量翻倍（每个寄存器多一个 `next_` 版本），但可读性收益更大。

---

### 4.2 第 5 级 no5_state 的生成（r4_valid + s5_count）

#### 4.2.1 概念说明

前 4 级蝶形的 `state` 都由 ROM 模块「顺带」生成（ROM 一边查旋转因子，一边用计数器分出 `2'b00/01/10`，见 u3-l4）。第 5 级没有 ROM，可它仍然是一个 radix2 蝶形，仍然需要 `state` 来切换 first half / second half。于是顶层必须自己造一个等价的 `state`，这就是 `no5_state`。

造 `no5_state` 需要两路输入：

- **`r4_valid`**：第 4 级蝶形 `radix_no4_outvalid`「打一拍」后的信号，表示「第 5 级现在有活干了」。
- **`s5_count`**：一个 1 位计数器，在 `r4_valid` 有效时每拍翻转，用来在 first half 与 second half 之间来回切。

#### 4.2.2 核心流程

`no5_state` 的生成逻辑（[RTL/FFT.v:L290-L298](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L290-L298)）等价于下面这个状态表：

| `r4_valid` | `s5_count` | `no5_state` | 含义 |
|:---:|:---:|:---:|---|
| 0 | × | `2'b00` | 等待（第 5 级闲着） |
| 1 | 0 | `2'b01` | first half（算和/差） |
| 1 | 1 | `2'b10` | second half（差乘旋转因子） |

配合 `next_s5_count` 的翻转：

```
若 r4_valid == 1： next_s5_count = s5_count + 1   （0↔1 来回翻）
若 r4_valid == 0： next_s5_count = s5_count        （保持）
```

于是稳态下（`r4_valid` 每拍都为 1），`no5_state` 就在 `01` 与 `10` 之间**逐拍交替**，正好对应 radix2 蝶形「先处理一个样本的 first half，下一拍处理它的 second half」的工作节奏。

> 为什么 `r4_valid` 要「打一拍」（延迟 1 拍）？因为第 5 级的反馈数据走的是 `shift_1`（延时 1 拍，见 u3-l3）。第 4 级在某拍算出 `radix_no4_outvalid` 时，反馈样本同时进入 `shift_1`；**下一拍**反馈样本才出现在 `radix_no5.din_a`，而此时 `r4_valid` 也正好升起来。两者同步到达，第 5 级才不会「空打」。这一拍延迟是刻意对齐的，不是多余。

#### 4.2.3 源码精读

第 5 级例化（[RTL/FFT.v:L230-L243](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L230-L243)）把 `no5_state` 接到 `.state()`，旋转因子写死：

```verilog
radix2 radix_no5(
    .state(no5_state),      // ← 顶层自制状态，替代 ROM
    .din_a_r(shift_1_dout_r),  // 反馈（延时 1 拍的差）
    .din_b_r(radix_no4_op_r),  // 第 4 级直通路
    .w_r(24'd256),          // 定点 1+j0：旋转因子常数化
    .w_i(24'd0),
    .op_r(out_r), .op_i(out_i),
    .outvalid()             // ← 悬空：第 5 级是最后一级，outvalid 没人接
);
```

`no5_state` 本体的生成在控制组合块里（[RTL/FFT.v:L292-L298](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L292-L298)）：

```verilog
next_r4_valid = radix_no4_outvalid;                       // 打拍前的源头
if (r4_valid) next_s5_count = s5_count + 1;               // 1 位计数器翻转
    else      next_s5_count = s5_count;

if     (r4_valid && s5_count == 1'b0) no5_state = 2'b01;  // first half
else if(r4_valid && s5_count == 1'b1) no5_state = 2'b10;  // second half
else                                  no5_state = 2'b00;  // 等待
```

这里有个**容易踩坑的细节**：`s5_count` 声明在 [L49](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L49) `reg s5_count,next_s5_count;`，没有指定位宽，Verilog 默认 **1 位**。所以 `s5_count + 1` 就是 `0→1→0→1` 的翻转，而不是无限递增——这正是我们想要的「交替」效果。

#### 4.2.4 代码实践

**目标**：手算从「第 4 级首次输出」到「第 5 级状态机启动」的控制波形。

**步骤**：
1. 设 `radix_no4_outvalid`（记作 `r4ov`）在第 `T` 拍首次升为 1，此后每拍保持 1。
2. 逐拍填写下表（信号取**该拍开始时**的寄存器值）。

| 拍 | r4ov | r4_valid | s5_count | no5_state |
|---|---|---|---|---|
| T-1 | 0 | 0 | 0 | `2'b00` |
| T | 1 | 0 | 0 | `2'b00` |
| T+1 | 1 | ① | 0 | ② |
| T+2 | 1 | 1 | 1 | ③ |
| T+3 | 1 | 1 | 0 | ④ |

**需要观察的现象**：`r4_valid` 比 `r4ov` 晚 1 拍升起；`no5_state` 在 `r4_valid` 升起的那一拍才从 `00` 切到 `01`。

**预期结果**：① = 1（因为 `next_r4_valid=r4ov` 在 T 拍已是 1，T+1 打进 `r4_valid`）；② = `2'b01`；③ = `2'b10`；④ = `2'b01`。可以看到 `no5_state` 自 T+1 起稳定地 `01→10→01→10` 交替。

#### 4.2.5 小练习与答案

**练习 1**：如果 `radix_no5` 的 `.outvalid()` 不悬空，而是接到某个下一级，会怎样？

**答案**：第 5 级是流水线最后一级，没有「下一级」可接，所以 `outvalid` 没有消费者，悬空是合理的。第 5 级的有效输出靠 `count_y`（统计 `radix_no4_outvalid` 个数）来表征，而不是靠 `radix_no5.outvalid`，见 4.3。

**练习 2**：把 `no5_state` 改成「先寄存一拍再送出去」会出什么问题？

**答案**：会让 `no5_state` 整体晚 1 拍，与 `shift_1` 送来的反馈数据错位，第 5 级会在错误的时刻做 first/second half，输出全错。所以 `no5_state` 故意保持纯组合、不打拍——这是它与其它 `next_xxx` 信号最大的不同。

---

### 4.3 count_y 与 y_1 计数链

#### 4.3.1 概念说明

`count_y` 是一个 6 位计数器，**它每收到一个第 4 级输出脉冲 `radix_no4_outvalid` 就加 1**，本质上是在数「到现在为止，第 5 级已经吐出了多少个频域样本」。32 点 FFT 一共要吐 32 个样本，所以 `count_y` 会从 0 走到 32（甚至更高）。

`y_1` 是 `count_y` 派生出来的 5 位索引，定义为：

\[
y\_1 = \begin{cases} \text{count\_y} - 1, & \text{count\_y} > 0 \\ 0, & \text{count\_y} = 0 \end{cases}
\]

这个 `y_1` 一身二用（见 u4-l2）：

- 在**写入相**，`y_1` 是排序大 `case` 的选择信号，决定当前样本被写进 `result_r_ns` 的哪一个槽位（按位反转映射）。
- 在**读出相**，因为 `count_y` 继续涨、`y_1` 借 5 位截断会再次从 0 扫到 31，于是 `y_1_delay`（`y_1` 打一拍）又被用来顺序读出 `dout`。

#### 4.3.2 核心流程

`count_y` 的计数与 `y_1` 的派生（[RTL/FFT.v:L300-L301](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L300-L301) 与 [L60](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L60)）：

```
写入相（count_y: 1 → 32）    y_1 = count_y - 1  扫 0 → 31   ← 给 case(y_1) 选槽
─────────────────────────────────────────────
读出相（count_y: 33 → 64）   y_1 因 5 位截断又扫 0 → 31      ← 给 dout 顺序读
```

关键点：`count_y` 是 **6 位**，`y_1` 是 **5 位**。当 `count_y = 32`（写完第 32 个）之后，`count_y - 1 = 32`，截到低 5 位就回卷成 0，于是读出相自动重新从索引 0 开始扫——这是「同一根计数链同时服务写入和读出」的小巧思。

#### 4.3.3 源码精读

`count_y` 与 `next_count_y`（控制组合块，[RTL/FFT.v:L300-L301](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L300-L301)）：

```verilog
if(radix_no4_outvalid) next_count_y = count_y + 5'd1;   // 每个第4级输出 +1
else                   next_count_y = count_y;
```

`y_1` 是连续赋值（[RTL/FFT.v:L60](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L60)）：

```verilog
assign y_1 = (count_y>5'd0)? (count_y - 5'd1) : count_y;
```

注意 `count_y - 5'd1` 是 6 位运算，赋给 5 位的 `y_1` 时**截掉最高位**，这正是读出相回卷的来源。声明见 [L43-L44](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L43-L44)（`count_y`/`next_count_y` 为 `[5:0]`）与 [L56](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L56)（`y_1` 为 `[4:0]`）。

> 顺带提一句：`next_count_y` 用的是 `radix_no4_outvalid`（第 4 级直通），而 4.2 的 `no5_state` 用的是 `r4_valid`（第 4 级打一拍）。前者负责「数样本给排序用」，后者负责「驱动第 5 级状态机」，两者刻意差一拍，互不干扰。

#### 4.3.4 代码实践

**目标**：用纸笔验证 `y_1` 在写入相与读出相的取值序列。

**步骤**：假设稳态下 `radix_no4_outvalid` 每拍为 1，从 `count_y = 0` 开始，逐拍填表（`y_1` 用 5 位十进制写）。

| count_y | y_1 | 相位 |
|---|---|---|
| 0 | 0 | 写入相（尚未开始） |
| 1 | 0 | 写入相 |
| 2 | 1 | 写入相 |
| ... | ... | ... |
| 32 | 31 | 写入相最后一格 |
| 33 | 0 | 读出相开始 |
| 34 | 1 | 读出相 |
| ... | ... | ... |
| 64 | 31 | 读出相最后一格 |

**需要观察的现象**：`count_y = 32` 时 `y_1 = 31`；`count_y = 33` 时 `y_1` 不是 32 而是 0（5 位截断）。

**预期结果**：写入相 `y_1` 单调扫 0→31；读出相 `y_1` 再次扫 0→31。两条相位共用一根计数链。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `count_y` 要做成 6 位、而 `y_1` 只要 5 位？

**答案**：32 个样本需要 `count_y` 至少能数到 32，5 位最多到 31 不够，所以用 6 位（能到 64）。而 `y_1` 作为 32 槽的索引，5 位（0~31）刚好；让 `y_1` 比 `count_y` 少一位，正是利用截断在读出相自动回卷，省掉一个单独的读出计数器。

**练习 2**：`y_1` 的减 1（`count_y - 1`）这个「偏移」是干什么的？

**答案**：它对应排序大 `case` 里 `y_1 = 0` 时写入的是 `result_r_ns[31]`（见 [L325-L327](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L325-L327)），即硬件表是「位反转再左移一格」。u4-l2 已证明这等价于 `bitrev₅(y_1) − 1`，这个 −1 偏移不可省，否则样本整体错位、SNR 不通过。

---

### 4.4 out_valid 与 dout 的寄存输出

#### 4.4.1 概念说明

`out_valid` 是顶层对外的「输出有效」握手信号，告诉 testbench（或下游模块）「`dout_r/dout_i` 上现在是一份有效的 FFT 频域样本」。它和 `dout` 一起，构成「先攒够 32 个、再一把顺序吐出」的两阶段输出节奏：

- **攒数据**：排序块把 32 个乱序样本写进 `result_r/result_i` 数组（写入相）。
- **吐数据**：32 个写完后，`out_valid` 拉高并保持，`dout` 按 `y_1_delay = 0→31` 的顺序把数组内容读出来（读出相）。

负责「切换这两个阶段」的核心标志叫 `over`（意为「写完了」）。

#### 4.4.2 核心流程

握手链可以画成一条信号的接力：

```
写入相: y_1 扫 0→31
            │
            └─► 当 y_1==31, next_over = 1            （组合，本拍置位）
                              │
                              ▼  (posedge clk)
                         over <= 1                    （自锁，此后一直为 1）
                              │
next_out_valid = (next_over==1) ? 1'b1 : assign_out   （组合）
                              │
                              ▼  (posedge clk)
                        assign_out <= 1  →  out_valid = 1   （对外握手拉高）

读出相: next_dout_r = result_r[y_1_delay]              （out_valid 高时读数组）
                              │
                              ▼  (posedge clk)
                          dout_r <= next_dout_r         （寄存输出，与 out_valid 对齐）
```

要点：
- `over` 在 `y_1==31` 那拍被置位（[L453](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L453)），随后自锁保持，所以 `out_valid` 一旦拉高就**不再回落**，配合 testbench 连续采 32 拍。
- `out_valid` 拉高与「读出相 `y_1` 回卷到 0」是同一时机，保证第一个 `dout` 对应 `result_r[0]`。

#### 4.4.3 源码精读

`over` 与 `next_out_valid` 在排序组合块开头（[RTL/FFT.v:L313-L321](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L313-L321)）：

```verilog
always @(*) begin
    next_over = over;                          // 默认自锁：写完后保持 1
    for (...) begin result_r_ns[i]=result_r[i]; ... end  // 默认保持原值
    if(next_over==1'b1) next_out_valid = 1'b1; // 一旦写完，输出永远有效
    else                next_out_valid = assign_out;
    ...
end
```

`next_over` 的真正置位点在排序表的最末一格（[RTL/FFT.v:L450-L454](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L450-L454)）：

```verilog
5'd31 : begin
    result_r_ns[30] = out_r[23:8];   // 写最后一个槽
    result_i_ns[30] = out_i[23:8];
    next_over = 1'b1;                // ← 32 个写完，触发收尾握手
end
```

`out_valid` 对外连线与 `dout` 读出（[RTL/FFT.v:L59](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L59) 与 [L303-L309](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L303-L309)）：

```verilog
assign out_valid = assign_out;                       // 对外握手
...
if(next_out_valid) begin
    next_dout_r = result_r[y_1_delay];               // 读出相：按顺序读数组
    next_dout_i = result_i[y_1_delay];
end else begin
    next_dout_r = dout_r;  next_dout_i = dout_i;     // 写入相：保持
end
```

复位把所有相关寄存器清零（[RTL/FFT.v:L261-L266](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L261-L266)），保证上电后 `out_valid=0`、`over=0`、`count_y=0`、`dout=0`：

```verilog
if(reset)begin
    ...
    count_y <= 0; assign_out <= 0; over <= 0;
    dout_r <= 0; dout_i <= 0; y_1_delay <= 0;
    ...
end
```

#### 4.4.4 代码实践

**目标**：定位 `out_valid` 从 0 跳到 1 的精确时刻，并解释它为什么「不回落」。

**步骤**：
1. 在 [L453](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L453) 找到 `next_over = 1'b1` 的触发条件。
2. 在 [L315](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L315) 找到 `next_over = over`（自锁）。
3. 在 [L320-L321](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L320-L321) 找到 `next_out_valid` 依赖 `next_over`。

**需要观察的现象**：`over` 一旦在某拍变成 1，下一拍 `next_over = over = 1`，再下一拍 `over` 仍是 1……形成自锁。

**预期结果**：`out_valid` 在 `y_1` 命中 31 之后的第 2 拍升为 1（一拍给 `over` 自锁、一拍给 `assign_out` 寄存），此后永久为 1。待本地用仿真器验证精确延迟拍数。

#### 4.4.5 小练习与答案

**练习 1**：`assign out_valid = assign_out;` 里 `assign_out` 是 `reg`，为什么用 `assign` 连线？

**答案**：`assign_out` 虽是 `reg` 类型（因为它在时序块里被赋值），但在这里我们只是把它的值「连线」到输出端口 `out_valid`，没有再触发任何条件，所以用 `assign` 做等价连接。端口 `out_valid` 声明为 `wire`（[L31](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L31)），必须用 `assign` 驱动。

**练习 2**：如果 `over` 不做自锁（把 `next_over = over` 去掉，只在 `y_1==31` 置 1），`out_valid` 会怎样？

**答案**：`over` 只会在 `y_1==31` 那**一拍**为 1，下一拍 `y_1` 进入读出相（变成 0），`next_over` 失去置位条件又回到默认……实际上会变成 0，`next_out_valid` 跟着回落，`out_valid` 会抖动，`dout` 无法稳定输出 32 个样本。自锁是必须的。

---

### 4.5 时序块与组合块的协同（next_xxx 约定与复位）

#### 4.5.1 概念说明

把前面三节拼起来，可以看出本设计的控制部分遵循一套**统一的写作范式**：

1. 每个需要记忆的状态 `xxx`，都配一个 `next_xxx`。
2. 组合块（`always @(*)`）里，**只写 `next_xxx = ...`**，描述「下一拍该是什么」。
3. 时序块（`always @(posedge clk or posedge reset)`）里，**只写 `xxx <= next_xxx`**，描述「时钟边沿把它存下来」。
4. 复位分支把所有 `xxx` 清零。

这套范式的好处是：组合逻辑里不会意外综合出锁存器（因为每个 `next_xxx` 都在组合块里被完整赋值），时序边界一目了然。唯一的「破例」是 `no5_state`——它没有 `next_` 版本，是纯组合直出（4.2 已解释原因）。

#### 4.5.2 核心流程

一次完整的「控制时序」可以串成下面这条主线（从第 4 级开始有输出算起）：

```
radix_no4_outvalid ──┬────────────────────► next_count_y (数样本)
                     │
                     └─► [打一拍] r4_valid ─► no5_state (第5级状态)
                                          ─► out_r/out_i (第5级算结果)
                                          ─► case(y_1) 写 result_r_ns[]
                                                │
                          y_1==31 ─► next_over ─► over ─► next_out_valid
                                                                      │
                                              assign_out ◄─ [打一拍] ┘
                                                  │
                                                  ▼
                                            out_valid (对外)
                                                  │
                          next_dout_r = result_r[y_1_delay] ─► dout_r (对外)
```

#### 4.5.3 源码精读

时序块的非复位分支统一用 `xxx <= next_xxx`（[RTL/FFT.v:L272-L288](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L272-L288)）；复位分支统一清零，包括用 `for` 循环清整个排序数组（[RTL/FFT.v:L267-L270](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L267-L270)）：

```verilog
if(reset)begin
    din_r_reg<=0; din_i_reg<=0; in_valid_reg<=0;
    s5_count<=0; r4_valid<=0; count_y<=0;
    assign_out<=0; over<=0; dout_r<=0; dout_i<=0; y_1_delay<=0;
    for (i=0;i<=31;i=i+1) begin
        result_r[i] <= 0;        // 整个排序缓冲清零
        result_i[i] <= 0;
    end
end
```

> 注意复位风格：`posedge clk or posedge reset` 表示**异步、高有效**复位（与 u3-l1 一致）。`reset` 一升起来，寄存器立刻归零，不必等时钟边沿。但 `no5_state` 不在这份清零名单里——因为它是组合信号，复位时它由 `r4_valid==0` 自动得到 `2'b00`（等待），天然就是「复位态」。

#### 4.5.4 代码实践

**目标**：把本设计的「`next_xxx` 范式」整理成一张对照表，识别唯一破例。

**步骤**：通读 [L254-L311](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L254-L311)，按下表填写。

| 寄存器 `xxx` | 是否在时序块更新？ | `next_xxx` 在哪生成？ | 是否遵循范式？ |
|---|---|---|---|
| `s5_count` | 是（L276） | L293-L294 | ✅ |
| `r4_valid` | 是（L277） | L292 | ✅ |
| `count_y` | 是（L278） | L300-L301 | ✅ |
| `assign_out` | 是（L279） | L320-L321 | ✅ |
| `over` | 是（L280） | L315 + L453 | ✅ |
| `dout_r` | 是（L282） | L303-L309 | ✅ |
| `y_1_delay` | 是（L281） | `y_1`（L60 assign） | ✅（直接打拍） |
| `no5_state` | **否** | L296-L298 | ❌ 纯组合破例 |

**需要观察的现象**：除了 `no5_state`，所有控制状态都严格遵循「组合算 `next_xxx`、时序打拍」。

**预期结果**：得到上表；`no5_state` 是唯一的破例，原因是它要即时驱动第 5 级蝶形、不能多一拍延迟。

#### 4.5.5 小练习与答案

**练习 1**：复位时 `no5_state` 会是什么值？需要专门给它写复位代码吗？

**答案**：复位时 `r4_valid` 被清成 0，组合逻辑里 `else no5_state = 2'b00`（[L298](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L298)）自动生效，所以 `no5_state` 自然为「等待」。它是组合信号，不能也不需要写进时序复位分支。

**练习 2**：时序块里 `for (i=0;i<=31;i=i+1) result_r[i] <= result_r_ns[i];` 这种写法，综合后会变成什么？

**答案**：这是「整数 `i` 作循环变量」的可综合写法，综合工具会在编译期把循环展开成 32 条独立的 `result_r[i] <= result_r_ns[i];` 非阻塞赋值，等价于 32 个独立的 16 位寄存器，不是动态循环。复位分支里同样的 `for` 也一样展开。

---

## 5. 综合实践：画出第 5 级控制时序波形

本讲的综合实践，是把上面四个最小模块串成**一张完整的控制时序波形图**，覆盖「从第 4 级首次输出 → 第 5 级状态机启动 → 排序写入 32 个 → out_valid 拉高 → dout 顺序读出」的整条链路。

### 实践目标

在纸上（或绘图工具里）画出从 `radix_no4_outvalid` 首次升高开始、连续约 70 拍的关键控制信号波形，并标注每个 `next_xxx` 信号的「生成位置」与它驱动的「时序寄存器」。

### 操作步骤

1. **列信号清单**。从 [RTL/FFT.v:L254-L311](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L254-L311) 抄出本讲关心的信号：`radix_no4_outvalid`、`r4_valid`、`s5_count`、`no5_state`、`count_y`、`y_1`、`over`、`assign_out`/`out_valid`、`y_1_delay`、`dout_r`。
2. **标注 next_xxx 来源**。在每个信号旁边写一行小字，例如：
   - `r4_valid` ←（打一拍）← `next_r4_valid = radix_no4_outvalid`（[L292](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L292)）
   - `no5_state` ←（组合直出）← `r4_valid`+`s5_count`（[L296-L298](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L296-L298)）
   - `over` ←（打一拍）← `next_over`（[L315](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L315)/[L453](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L453)）
   - `out_valid` ← `assign_out` ←（打一拍）← `next_out_valid`（[L320-L321](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L320-L321)）
3. **手画波形**。设 `radix_no4_outvalid` 在第 `T` 拍首次为 1，此后稳态每拍为 1。按下表逐拍推导（节选关键拍）：

   | 拍 | radix_no4_outvalid | r4_valid | s5_count | no5_state | count_y | y_1 | over | out_valid |
   |---|---|---|---|---|---|---|---|---|
   | T-1 | 0 | 0 | 0 | 00 | 0 | 0 | 0 | 0 |
   | T | 1 | 0 | 0 | 00 | 1 | 0 | 0 | 0 |
   | T+1 | 1 | 1 | 0 | 01 | 2 | 1 | 0 | 0 |
   | T+2 | 1 | 1 | 1 | 10 | 3 | 2 | 0 | 0 |
   | ... | 1 | 1 | 0/1 | 01/10 | … | … | 0 | 0 |
   | T+31 | 1 | 1 | … | … | 32 | 31 | 0→1 | 0 |
   | T+32 | 1 | 1 | … | … | 33 | 0(回卷) | 1 | 0→1 |
   | T+33 | 1 | 1 | … | … | 34 | 1 | 1 | 1 |

4. **画出关键边沿标注**：
   - `r4_valid` 比 `radix_no4_outvalid` 晚 1 拍；
   - `no5_state` 自 `T+1` 起 `01↔10` 交替；
   - `over` 在 `y_1==31`（即 `count_y==32`）那拍置位并自锁；
   - `out_valid` 紧随 `over` 拉高并保持；
   - `dout_r` 在 `out_valid` 高时按 `y_1_delay` 顺序输出 `result_r[0..31]`。

### 需要观察的现象

- 第 5 级状态机的启动比第 4 级输出晚 **1 拍**（`r4_valid` 的打拍延迟），这一拍恰好等于 `shift_1` 的反馈延时。
- `out_valid` 的上升沿与「`y_1` 回卷到 0」基本同步，保证读出相从 `result_r[0]` 开始。
- `no5_state` 是唯一不经过寄存器、组合直出的控制信号。

### 预期结果

得到一张覆盖「启动 → 稳态写入 → 收尾握手 → 稳态读出」四个阶段的控制波形图，并能口头复述每个跳变的成因。表中的精确拍数（尤其 `over`/`out_valid` 相对 `y_1==31` 的延迟）建议在本地仿真器里用 `SIM/FFT_tb.v` 跑一次，对照波形确认——若与上表有出入，以仿真波形为准（**待本地验证**）。

### 进阶（可选）

把 `SIM/FFT_tb.v` 里的看门狗延迟上限（68 拍）与本图对照，思考：为什么从 `in_valid` 拉高到第一个 `out_valid`，总共需要的拍数与「`shift` 延时线深度之和 + 上述控制握手延迟」吻合？这正好回扣 u4-l1 提到的「填充时延主源于延时线深度之和 31」。

## 6. 本讲小结

- `FFT.v` 的控制部分由「1 个时序块 + 2 个组合块」组成：时序块管寄存与复位，控制组合块算 `no5_state` 与各 `next_xxx`，排序组合块算 `result_r_ns`/`next_over`/`next_out_valid`（u4-l2）。
- 第 5 级没有 ROM，其蝶形状态 `no5_state` 由顶层用 `r4_valid`（第 4 级 `outvalid` 打一拍）与 1 位翻转计数器 `s5_count` 共同生成，稳态下 `01↔10` 逐拍交替。
- `count_y` 是 6 位输出样本计数器，派生出 5 位索引 `y_1 = count_y − 1`；写入相 `y_1` 扫 0→31 驱动排序写入，读出相借 5 位截断再扫 0→31 驱动顺序读出。
- `over` 在 `y_1==31` 时置位并自锁，触发 `out_valid`（经 `assign_out` 寄存）拉高并保持；`dout` 在 `out_valid` 高时按 `y_1_delay` 顺序读出 `result_r/result_i`。
- 整个控制部分遵循「组合算 `next_xxx`、时序块 `xxx <= next_xxx`」的统一范式，异步高有效复位清零全部状态；`no5_state` 是唯一破例（纯组合直出，不寄存、不复位），原因是它要即时驱动第 5 级蝶形。

## 7. 下一步学习建议

- **横向验证**：回到 u4-l1，把本讲的 `valid` 菊花链与前 4 级 ROM 的 `state` 生成对照，体会「前 4 级用 ROM 生成 state、第 5 级用顶层自制 no5_state」的对称设计。
- **纵向深入**：进入 u5-l1（Testbench 与 SNR 验证方法），看 testbench 是如何**消费**本讲的 `out_valid` 与 `dout` 的——你会更清楚为什么 `out_valid` 要做成「拉高后保持」的电平，而不是脉冲。
- **物理实现**：学完 u5 后进入 u6-l2（综合约束），理解这里的「时钟周期 10ns、异步复位」如何被翻译成 Design Compiler 的 `create_clock` 与 `set_clock_uncertainty` 约束。
