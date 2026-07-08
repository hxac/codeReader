# 脉冲生成、锁存与分频

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `Pulse_Latch` 如何把一个**一闪而过的脉冲**「冻住」成一个持续的电平，并讲明白它其实就是一个 `Register`，为什么 `clear` 的优先级高于 `pulse_in`。
- 用 `Pulse_Generator` 检测一个电平信号的**上升沿/下降沿**，并解释为什么输出脉冲出现在电平变化的**同一周期**、而不是下一周期。
- 用 `Pulse_Divider` 对一串脉冲做**整数分频**，说清「倒计数到 1 就发一个输出脉冲并重装」的工作过程，以及为什么输出脉冲与触发它的输入脉冲落在同一拍。
- 把三个模块的源码串成一条「事件捕获 → 边沿检测 → 事件计数」的脉冲处理工具链，并定位每个关键代码点。

## 2. 前置知识

本讲是「脉冲逻辑与接口转换」单元的第一篇，处理的是同一时钟域内**脉冲与电平之间的相互转换**。你只需要两块基础：

- **u6-l1（Register 家族）**：这是本讲的正式前置。你已经知道 `Register` 是一个同步寄存器，有 `clock`/`clock_enable`/`clear`/`data_in`/`data_out` 五个端口，时钟块里用「**最后赋值胜出**」（last-assignment-wins）的并列 `if` 让 `clear` 自然优先于 `clock_enable`。本讲的 `Pulse_Latch` 和 `Pulse_Generator` 都只是 `Register` 的一种特定接法。
- **u8-l2（二进制计数器与可复用函数）**（学习路径上的前置）：你已经知道 `Counter_Binary` = 1 个 `Adder_Subtractor_Binary` 算下一拍值 + 几个 `Register` 存储，操作优先级是 `clear > load > run`，且 `load` 覆盖 `run`。本讲的 `Pulse_Divider` 就是建立在这样一个下计数器之上。

一个直觉式的复习：在数字电路里，信息有两种基本形态——**电平**（一个持续高或低的信号）和**脉冲**（只高高的一拍、随即回落）。FSM 的状态、握手里的 `valid`/`ready` 是电平；「计数器到顶了」「收到一个字节了」这种事件天然是脉冲。很多设计困难就来自「我手上是个脉冲，但下游要电平」或反过来。本讲三个模块就是在这两种形态之间来回翻译的最小工具。

> 你在 **u14-l1（字同步与脉冲同步）** 里其实已经见过这三种模块的用法：`Pulse_Latch` 充当 4 相握手的「置位/清零」原语，`Pulse_Generator` 把 toggle 的边沿还原成接收脉冲。本讲把它们从 CDC 场景里拆出来，单独讲清楚各自是怎么实现的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `Pulse_Latch.v` | 捕获一个高脉冲并**保持为电平**，直到被 `clear` 清零。本质是一个 `Register`。 |
| `Pulse_Generator.v` | 把 `level_in` 的**电平变化（边沿）**变成一拍宽的脉冲，给出上升沿/下降沿/任意沿三种输出。 |
| `Pulse_Divider.v` | 每收到 `divisor` 个输入脉冲就发一个输出脉冲，即对脉冲流做**整数分频**。内部是一个下计数器。 |
| 辅助原语（已在前序讲义学过） | `Register`（u6-l1）、`Counter_Binary`（u8-l2）。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**脉冲锁存**、**脉冲生成**、**脉冲分频**。三者层层递进——锁存回答「脉冲怎么变电平」，生成回答「电平变化怎么变脉冲」，分频回答「怎么按数到第 N 个脉冲才动作」。

### 4.1 脉冲锁存：把一拍脉冲冻成电平

#### 4.1.1 概念说明

很多场景里，一个事件以单周期脉冲的形式到来，但处理它的 FSM 此刻正忙着别的事，可能错过这一拍。我们希望：**事件来一下，就「记住」，并把输出保持成一个稳态电平，等 FSM 有空再来读，读完再清掉。**

这正是 `Pulse_Latch` 做的事。源码注释一句话点题：

> Captures a high pulse and holds it until cleared. This device simplifies FSM logic by converting a transient event into a steady signal that the FSM can pick up later once it reaches the correct state.

这里要先澄清一个**命名陷阱**：模块叫 `Pulse_Latch`，但它**不是** Verilog 里那种电平敏感的 `latch`（那是综合时要极力避免的东西）。这里的「latch」是动词「锁存/捕获」的意思——它捕获一个事件并冻结成电平。在硬件层面它就是一个**触发器（flip-flop）**，由 `Register` 实现，完全同步、完全可综合。

#### 4.1.2 核心流程

`Pulse_Latch` 的实现极其简洁——把 `Register` 三个端口按特定方式接死：

```text
                data_in = 1'b1（恒为 1）
                       │
pulse_in ──▶ clock_enable  ──┐
                             ▼
                       ┌──────────┐
              clock ──▶│ Register │──▶ level_out
              clear ──▶│  (1 bit) │
                       └──────────┘
```

由此推出行为：

1. 某拍 `pulse_in` 为高 → 当拍时钟沿把 `data_in`（=1）写入 → 下一拍起 `level_out = 1`。
2. `pulse_in` 回落后，`level_out` **仍是 1**（`Register` 不变就不动）——脉冲被「冻」成了电平。
3. `clear` 拉高一拍 → 当拍时钟沿把 `level_out` 写回 `RESET_VALUE`（默认 0）——事件被「消费」完毕。

关键点：`clear` 与 `pulse_in` 同拍为高时，**`clear` 胜出**。这来自 `Register` 的「最后赋值胜出」——我们会在 4.1.3 用源码确认。

#### 4.1.3 源码精读

整个模块就是一个 `Register` 实例，端口定义如下：

[Pulse_Latch.v:10-19](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Latch.v#L10-L19)——只有 `clock`/`clear`/`pulse_in`/`level_out` 四个端口，`RESET_VALUE` 参数决定清零后的电平。

核心实现就这一处：

```verilog
Register
#(
    .WORD_WIDTH     (1),
    .RESET_VALUE    (RESET_VALUE)
)
latch
(
    .clock          (clock),
    .clock_enable   (pulse_in),   // 脉冲当使能
    .clear          (clear),
    .data_in        (1'b1),        // 恒为 1
    .data_out       (level_out)
);
```

[Pulse_Latch.v:21-33](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Latch.v#L21-L33)——把 `pulse_in` 当成 `Register` 的 `clock_enable`，`data_in` 接死 `1'b1`。于是「来一个脉冲」=「使能一拍」=「把 1 写进触发器」。

那 `clear` 凭什么优先？看 `Register` 本体：

```verilog
always @(posedge clock) begin
    if (clock_enable == 1'b1) begin
        data_out <= data_in;          // 先写数据（=1）
    end
    if (clear == 1'b1) begin
        data_out <= RESET_VALUE;      // 再写清零值——后写胜出
    end
end
```

[Register.v:65-73](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v#L65-L73)——这是 u3-l2 讲过的「最后赋值胜出」惯用法：两个并列 `if`，非阻塞赋值 `<=` 在时间步末统一生效，**后一条** `if` 命中时盖掉前一条。所以 `clear` 与 `pulse_in` 同拍有效时，`clear` 必赢，`level_out` 被清成 `RESET_VALUE`。这正是 4 相握手（set/clear）需要的语义：`pulse_in` 置位、`clear` 清零，清零权威最高。

> 注意：`pulse_in` 必须已经同步于 `clock`。`Pulse_Latch` 是个触发器，不是 CDC 同步器；跨时钟域的事件要先过 `CDC_*` 同步器，再喂给它。

#### 4.1.4 代码实践

**实践目标**：用一个最小例子验证「一拍脉冲被冻成持续电平，直到 clear 才回落」，并确认 clear 的优先级。

**操作步骤（含示例代码）**：

1. 实例化一个 `Pulse_Latch`，给它喂一个只高 1 拍的 `pulse_in`，观察 `level_out`：

```verilog
// 示例代码：仅供阅读理解，非项目原有文件
reg pulse_in = 1'b0;
reg clear    = 1'b0;
wire level_out;

Pulse_Latch event_capture(.clock(clock), .clear(clear),
                          .pulse_in(pulse_in), .level_out(level_out));

// 测试序列（每行一个时钟周期）：
// N  : pulse_in=1, clear=0   -> 下一拍 level_out=1
// N+1: pulse_in=0, clear=0   -> level_out 仍为 1（冻住了！）
// N+2: pulse_in=0, clear=0   -> level_out 仍为 1
// M  : pulse_in=0, clear=1   -> 下一拍 level_out=0（消费完毕）
```

2. 把 `Pulse_Latch.v:21-33` 的接法对照上面序列，逐拍推演 `Register` 内部那两个 `if` 的命中情况。
3. 再构造一拍 `pulse_in=1` 且 `clear=1` 同时有效，预测 `level_out`。

**需要观察的现象 / 预期结果**：

- `pulse_in` 只高了 1 拍，`level_out` 却从下一拍起**一直为 1**，直到你主动 `clear`——这正是「瞬时事件 → 稳态电平」。
- `pulse_in` 与 `clear` 同拍有效时，`level_out` 下一拍为 `RESET_VALUE`（0），证明 `clear` 优先。

> 待本地验证：若在仿真里跑上述序列，应观察到 `level_out` 在 `pulse_in` 拉高后的那一拍置 1，并在后续空闲周期保持 1，直到 `clear` 拉高那一拍之后的下一拍才归 0。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `Pulse_Latch` 里 `Register` 的 `data_in` 从 `1'b1` 改成接一个外部信号 `set_value`，模块的含义会变成什么？

**参考答案**：它会变成一个「**带同步清零的使能寄存器**」——`pulse_in` 当使能、采样 `set_value`、`clear` 同步清零。此时它捕获的不再是「事件有无」，而是「事件发生时的某个值」。换言之，原版 `Pulse_Latch` 是它的 1 bit 单值特例（`set_value` 恒为 1）。

**练习 2**：为什么 `Pulse_Latch` 用 `Register` 的 `clock_enable` 来响应 `pulse_in`，而不是写一个 `always @(posedge clock) if (pulse_in) level_out <= 1;`？

**参考答案**：复用 `Register` 把「数据/控制分离」做到了最底层（见 u4-l1、u6-l1）：复位（`clear`）的优先级、`last-assignment-wins` 的正确性、综合时触发器的推断，都由 `Register` 统一保证，不必在每个用到的地方重写时序逻辑、也就不必处处重新论证正确性。这正是本书「构建块库」的核心收益。

---

### 4.2 脉冲生成：把电平变化变回一拍脉冲

#### 4.2.1 概念说明

`Pulse_Latch` 解决了「脉冲 → 电平」。反过来，`Pulse_Generator` 解决「**电平变化 → 脉冲**」——也就是**边沿检测**：当 `level_in` 发生跳变（上升/下降）时，输出一个一拍宽的脉冲。

源码注释说清了它的两个关键性质：

> Converts a change in `level_in` (an edge) into a pulse lasting one clock cycle. **The input edge must be synchronous to the clock.** The pulse outputs are combinational: a given pulse is generated in the same cycle as the relevant change in signal level.

两条要点先记住：① 输入必须已在本时钟域（它不是 CDC 同步器，内部只是一个 1 拍延迟寄存器）；② 输出是**组合的**，脉冲落在电平变化的**同一拍**。第二点是它与「先打一拍再异或」的朴素写法的关键区别，下面用源码讲透。

#### 4.2.2 核心流程

思路很直白：**把输入延迟一拍，再拿当前值和延迟值比较。**

```text
level_in ──▶ [Register（延迟 1 拍）] ──▶ level_in_delayed
   │                                        │
   │     ┌──────────────────────────────────┘
   ▼     ▼
  比较（组合）：
    pulse_posedge_out = (level_in==1) && (level_in_delayed==0)   // 上升沿
    pulse_negedge_out = (level_in==0) && (level_in_delayed==1)   // 下降沿
    pulse_anyedge_out = pulse_posedge_out || pulse_negedge_out    // 任意沿
```

时序推演（设 `level_in` 在第 N 拍由 0 变 1）：

| 周期 | level_in | level_in_delayed（=上一拍的 level_in） | pulse_posedge_out |
| --- | --- | --- | --- |
| N-1 | 0 | 0 | 0 |
| N | **1** | 0（还是上一拍的 0） | **1（脉冲！）** |
| N+1 | 1 | 1 | 0 |

注意第 N 拍：`level_in` 已经是 1，而 `level_in_delayed` 因为是寄存器输出、要等到第 N→N+1 的时钟沿才更新成 1，所以第 N 拍它仍是 0。两者一比，上升沿脉冲**就在第 N 拍这一拍**拉高——这正是「脉冲落在变化的同一周期」。

#### 4.2.3 源码精读

端口给出三种边沿输出：

[Pulse_Generator.v:15-22](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Generator.v#L15-L22)——`pulse_posedge_out`/`pulse_negedge_out`/`pulse_anyedge_out` 三个 `output reg`，对应上升沿、下降沿、任意沿。

**第一步：把输入延迟一拍。** 用一个常使能、不清零的 `Register` 当 D 触发器：

```verilog
Register
#(.WORD_WIDTH(1), .RESET_VALUE(1'b0))
delay
(
    .clock          (clock),
    .clock_enable   (1'b1),         // 每拍都更新
    .clear          (1'b0),
    .data_in        (level_in),
    .data_out       (level_in_delayed)
);
```

[Pulse_Generator.v:34-46](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Generator.v#L34-L46)——`clock_enable` 接死 `1'b1`、`clear` 接死 `1'b0`，于是它就是一个纯粹的 1 拍延迟触发器，`level_in_delayed` 永远比 `level_in` 晚一拍。

**第二步：组合比较产生脉冲。**

```verilog
always @(*) begin
    pulse_posedge_out = (level_in          == 1'b1) && (level_in_delayed  == 1'b0);
    pulse_negedge_out = (level_in          == 1'b0) && (level_in_delayed  == 1'b1);
    pulse_anyedge_out = (pulse_posedge_out == 1'b1) || (pulse_negedge_out == 1'b1);
end
```

[Pulse_Generator.v:52-56](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Generator.v#L52-L56)——这是 u3-l1 讲过的「组合块用阻塞赋值 + 三元/等式比较」范式。注意它把布尔式写成等式比较 `(level_in == 1'b1)` 而非按位 `level_in`，这样能正确传播 X 值，也更好读。

[Pulse_Generator.v:4-7](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Generator.v#L4-L7)——源码注释明确两条性质：输入边沿必须同步于时钟；脉冲输出是组合的、与电平变化同周期。

一个常被忽视的用处（注释 [Pulse_Generator.v:9-11](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Generator.v#L9-L11)）：它能把一个「持续时间未知」的条件转成一次性事件，从而**消灭一些简单 FSM**。例如「某信号一变化就只更新一次寄存器」，不必写状态机记「是否已更新过」，用一个 `Pulse_Generator` 取上升沿当使能即可。

#### 4.2.4 代码实践

**实践目标**：用 `Pulse_Generator` 检测一个电平信号的上升沿，并确认脉冲落在变化那一拍。

**操作步骤（含示例代码）**：

1. 实例化 `Pulse_Generator`，给 `level_in` 喂一段先低后高再低的电平：

```verilog
// 示例代码：仅供阅读理解
reg  level_in = 1'b0;
wire posedge_pulse, negedge_pulse, anyedge_pulse;

Pulse_Generator edge_detect
(
    .clock               (clock),
    .level_in            (level_in),
    .pulse_posedge_out   (posedge_pulse),
    .pulse_negedge_out   (negedge_pulse),
    .pulse_anyedge_out   (anyedge_pulse)
);

// 测试序列（每行一个时钟周期）：
// N-1: level_in=0  -> 各脉冲均 0
// N  : level_in=1  -> posedge_pulse=1（与变化同拍！），anyedge=1
// N+1: level_in=1  -> 各脉冲均 0
// M  : level_in=0  -> negedge_pulse=1，anyedge=1
```

2. 对照 4.2.2 的时序表，逐拍填出 `level_in_delayed` 与三个输出的值。
3. 想一想：若把 `level_in` 直接连到一个跨时钟域、未经同步的信号，会出什么问题？

**需要观察的现象 / 预期结果**：

- `posedge_pulse` 恰好在 `level_in` 从 0 跳到 1 的**那一拍**为 1，且只高 1 拍；`negedge_pulse` 同理对应下降沿；`anyedge_pulse` 在两种跳变时都为 1。
- 输入若未同步，`level_in` 可能在时钟沿附近抖动，导致延迟寄存器与组合比较在同一拍看到不一致的值，产生毛刺或漏检——所以注释强调「输入边沿必须同步于时钟」。

> 待本地验证：在仿真里给 `level_in` 一个完整的高低跳变，应看到 `pulse_posedge_out` 与跳变同拍拉高 1 拍，`pulse_negedge_out` 在回落同拍拉高 1 拍。

#### 4.2.5 小练习与答案

**练习 1**：为什么输出脉冲出现在电平变化的**同一拍**，而不是「下一拍」？朴素写法 `assign pulse = level_in ^ level_in_delayed;` 会一样吗？

**参考答案**：因为 `pulse_*_out` 是**组合**地比较「当前 `level_in`」与「上一拍的 `level_in_delayed`」。在第 N 拍，`level_in` 已是变化后的新值，而 `level_in_delayed` 还是旧值（寄存器要等下一个时钟沿才更新），两者不同 ⇒ 组合逻辑立刻在本拍输出脉冲。朴素写法 `level_in ^ level_in_delayed` 对 `anyedge` 是等价的（异或检测不同），但拿不到「上升还是下降」的方向，且不区分 X 传播——所以模块用三个显式的等式比较分别给出 posedge/negedge/anyedge。

**练习 2**：`Pulse_Generator` 能不能用来给一个异步信号「打两拍做同步」？为什么？

**参考答案**：不能。它内部的 `Register` 只是延迟 1 拍，且组合输出直接由未同步的 `level_in` 参与——这既不能消除亚稳态（没有 u13-l1 那种紧挨摆放、带 `ASYNC_REG` 的同步链），也不能保证可靠采样。跨域同步必须用 `CDC_Bit_Synchronizer` 之类的专用同步器；`Pulse_Generator` 的前提是「输入已同步」。

---

### 4.3 脉冲分频：数到第 N 个脉冲才动作

#### 4.3.1 概念说明

很多设计需要「**每发生 N 个事件，就触发一次动作**」：每收 4 个字节就置一个「满」标志、每 1000 个时钟周期产生一个慢速使能、把一串脉冲除以一个整数得到商…… `Pulse_Divider` 就是干这个的——它每收到 `divisor` 个输入脉冲，就发一个单周期输出脉冲，从而把脉冲的数量「除以」`divisor`。

源码注释给了一组例子（`divisor=3`）：

> 9 input pulses will produce 3 output pulses; 5 input pulses will produce 1 output pulse; 7 input pulses will produce 2 output pulses.

写成数学式，输入 \(N_{\text{in}}\) 个脉冲、除数为 \(d\) 时：

\[ N_{\text{out}} = \left\lfloor \frac{N_{\text{in}}}{d} \right\rfloor, \qquad r = N_{\text{in}} \bmod d \]

（余数 \(r\) 就是「再补几个输入脉冲就能再多一个输出脉冲」的数目，见注释 [Pulse_Divider.v:33-42](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Divider.v#L33-L42) 列出的「整数除法」用法。）

它还有一个对时序至关重要的性质（注释 [Pulse_Divider.v:16-21](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Divider.v#L16-L21)）：**输出脉冲与触发它的那个输入脉冲落在同一拍**，即从 `pulses_in` 到 `pulse_out` 存在一条组合路径。这是有意为之，为了让「第 N 个事件到达」与「信号发出」之间没有一拍延迟（例如「当前这一笔正好填满缓冲」必须当拍就报告）。

#### 4.3.2 核心流程

核心是一个**下计数器**（`Counter_Binary`），其当前值 `count` 的含义是「**距离下一个输出脉冲，还差几个输入脉冲**」。设 `divisor=3`：

```text
pulses_in ──┐
divisor ────┤        ┌─────────────────────────┐
restart ────┼─控制──▶│ Counter_Binary（下计数）│──▶ count
            │  逻辑  │   INCREMENT=1, up_down=1 │
            │        └─────────────────────────┘
            │                    │
            │   run       = pulses_in && (count != 0)
            │   division_done = pulses_in && (count == 1)
            │   load      = division_done || restart || div_by_zero
            │   pulse_out = division_done && !restart
            │   div_by_zero = (count == 0)
            └──────────────────────────────────────
```

以 `divisor=3` 为例的计数轨迹（每个输入脉冲让 count 减 1）：

| 输入脉冲序号 | count（动作前） | 动作 | pulse_out |
| --- | --- | --- | --- |
| 第 1 个 | 3 | run：3→2 | 0 |
| 第 2 个 | 2 | run：2→1 | 0 |
| 第 3 个 | 1 | division_done + load：1→3（重装） | **1（同拍）** |
| 第 4 个 | 3 | run：3→2 | 0 |

三条关键纪律（全部来自源码与注释）：

1. **计数永远不会数到 0**：当 `count` 到 1、再来一个脉冲就 `division_done`，同时 `load` 重装 `divisor`，于是 `count` 从 1 直接跳回 `divisor`，绕过了 0。注释 [Pulse_Divider.v:73-79](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Divider.v#L73-L79) 明确：「0 永远不会通过计数到达」。
2. **0 只能靠「装入 0」出现**：若把 `divisor` 设成 0，`count` 变 0，`div_by_zero` 拉高，输出脉冲被禁用，并且每拍都 `load`（重装）直到 `divisor` 变非零——注释 [Pulse_Divider.v:29-31](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Divider.v#L29-L31)。
3. **`restart` 重装并暂停**：拉高一拍就强制重装 `divisor`、重新开始；一直拉着不放手则「停机」（`pulse_out` 被 `&& !restart` 封死）——注释 [Pulse_Divider.v:22-27](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Divider.v#L22-L27)。

#### 4.3.3 源码精读

参数与端口（注意 `WORD_WIDTH` 默认 0，必须实例化时设定，否则位宽非法——这是 u2-l2 讲过的「参数默认 0」安全栅栏）：

[Pulse_Divider.v:46-58](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Divider.v#L46-L58)——`INITIAL_DIVISOR` 是上电初值（第一轮计数用它），之后每轮重装用的是 `divisor` 输入端口的当前值。

两个定宽常量（u2-l2 讲过的复制构造法）：

```verilog
localparam WORD_ZERO = {WORD_WIDTH{1'b0}};
localparam WORD_ONE  = {{WORD_WIDTH-1{1'b0}}, 1'b1};
```

[Pulse_Divider.v:70-71](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Divider.v#L70-L71)——`WORD_ONE` 是「数值 1」的定宽常量，喂给计数器当 `INCREMENT`，所以计数器每次走 1。

**核心：下计数器。**

```verilog
Counter_Binary
#(
    .WORD_WIDTH     (WORD_WIDTH),
    .INCREMENT      (WORD_ONE),       // 每次减 1
    .INITIAL_COUNT  (INITIAL_DIVISOR) // 上电初值
)
pulse_counter
(
    .clock          (clock),
    .clear          (1'b0),
    .up_down        (1'b1),   // 1 = 下计数
    .run            (run),
    .load           (load),
    .load_count     (divisor),
    ...
    .count          (count)
);
```

[Pulse_Divider.v:85-106](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Divider.v#L85-L106)——`up_down=1` 表示下计数（见 `Counter_Binary` 端口约定 [Counter_Binary.v:33](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Counter_Binary.v#L33)）。注意 `load_count` 接的是 `divisor` 输入端口，所以每次 `load` 都把当前的 `divisor` 装进去——这意味着分频比可以**动态改变**（在每次重装时生效）。

为什么 `count` 不会在 `run` 与 `load` 同拍时被错误地「减 1 再装值」？看 `Counter_Binary` 的控制逻辑：

```verilog
always @(*) begin
    next_count   = (load == 1'b1) ? load_count : incremented_count;  // load 覆盖
    load_counter = (run == 1'b1) || (load == 1'b1);
    ...
end
```

[Counter_Binary.v:84-90](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Counter_Binary.v#L84-L90)——`next_count` 用三元在「装入值」与「加 1 后的值」之间二选一，**`load` 覆盖 `run`**。所以第 3 个脉冲那拍 `run=1` 且 `load=1` 同时发生时，装的是 `divisor`（=3），不是 1−1=0。这正是「绕过 0」的实现机制。

**控制逻辑（四行组合决定一切）。** 这是整个模块的「大脑」：

```verilog
reg division_done = 1'b0;

always @(*) begin
    run             = (pulses_in     == 1'b1) && (count     != WORD_ZERO);
    division_done   = (pulses_in     == 1'b1) && (count     == WORD_ONE);
    load            = (division_done == 1'b1) || (restart   == 1'b1) || (div_by_zero == 1'b1);
    pulse_out       = (division_done == 1'b1) && (restart   == 1'b0);
end
```

[Pulse_Divider.v:120-125](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Divider.v#L120-L125)——逐行解读：

- `run`：来脉冲且尚未到 0，就让计数器减 1。
- `division_done`：来脉冲且 `count` 正好是 1——这一拍就是「第 `divisor` 个」。
- `load`：完成一次、或 `restart`、或除零——任何一种都触发重装。
- `pulse_out`：完成一次且未被 `restart` 封掉——这就是那一拍同周期发出的输出脉冲，组合地依赖 `pulses_in`。

注意 `pulse_out` 与 `division_done` 都直接含 `pulses_in`，所以确有一条 `pulses_in → pulse_out` 的组合路径（呼应 4.3.1 提到的「同拍报告」性质）。

**除零检测被刻意拆到另一个组合块。** 这是一个容易被忽略的工程细节：

```verilog
always @(*) begin
    div_by_zero = (count == WORD_ZERO);
end
```

[Pulse_Divider.v:114-116](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Divider.v#L114-L116)——为什么 `div_by_zero` 不和上面四行写在同一个 `always` 里？注释 [Pulse_Divider.v:108-112](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Divider.v#L108-L112) 解释：linter（如 verilinter）看不到模块层级内部，如果把 `div_by_zero`（影响 `load`）和 `pulse_out` 写在一起，可能误报一条 `div_by_zero` 与 `pulse_out` 之间的**假组合环**。拆成并列的 procedural block 就避开了这个误报。

#### 4.3.4 代码实践

**实践目标**：用 `Pulse_Divider` 对一串脉冲做分频，验证「每 `divisor` 个输入脉冲出 1 个输出脉冲」，并确认输出与第 N 个输入同拍。

**操作步骤（含示例代码）**：

1. 实例化一个 `divisor=3` 的 `Pulse_Divider`，喂一串脉冲：

```verilog
// 示例代码：仅供阅读理解
localparam W = 4;                 // 计数位宽，需实例化时指定
reg  [W-1:0] divisor = 4'd3;
reg         pulses_in = 1'b0;
reg         restart   = 1'b0;
wire        pulse_out, div_by_zero;

Pulse_Divider
#(.WORD_WIDTH(W), .INITIAL_DIVISOR(4'd3))
div3
(
    .clock       (clock),
    .restart     (restart),
    .divisor     (divisor),
    .pulses_in   (pulses_in),
    .pulse_out   (pulse_out),
    .div_by_zero (div_by_zero)
);

// 把 pulses_in 每 4 拍拉高一次（或直接常拉高，观察「数到 3 出 1」）
```

2. 按 4.3.2 的轨迹表，逐个输入脉冲推演 `count`、`division_done`、`pulse_out`。
3. 再把 `divisor` 改成 0，观察 `div_by_zero` 与 `pulse_out`。
4. 把 `pulses_in` 常接 `1'b1`，观察输出是否变成「每 3 个时钟周期一个使能脉冲」——这正是注释里「从主时钟派生周期性使能」的用法。

**需要观察的现象 / 预期结果**：

- 输入 9 个脉冲 → 输出 3 个；输入 5 个 → 输出 1 个；输入 7 个 → 输出 2 个（与注释例子一致）。
- 每个输出脉冲都**与第 3、6、9… 个输入脉冲同拍**出现（组合路径），而非滞后一拍。
- `divisor=0` 时 `div_by_zero=1`、`pulse_out` 恒为 0，直到 `divisor` 改回非零。
- `pulses_in` 常高时，`pulse_out` 是周期为 `divisor` 个时钟的使能脉冲（且 `pulses_in` 不必是「彼此分离」的单拍脉冲——常高就等于每拍一个脉冲，注释 [Pulse_Divider.v:12-14](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Divider.v#L12-L14) 明确了这一点）。

> 待本地验证：在仿真里把 `pulses_in` 接一个固定节拍源、`divisor=3`，应看到 `pulse_out` 每 3 个输入脉冲拉高 1 拍，并与触发它的那个输入脉冲落在同一时钟周期。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `count` 永远数不到 0？如果让它数到 0 会怎样？

**参考答案**：当 `count` 减到 1、再来一个输入脉冲时，`division_done` 命中，`load` 同时命中，于是 `Counter_Binary` 依据「`load` 覆盖 `run`」直接装入 `divisor`，`count` 从 1 跳回 `divisor`，绕过了 0。`count==0` 被 `div_by_zero` 单独保留为「除零错误」的标志——若允许正常计数到 0，就无法把 0 与「正常的下一个输出」区分开，除零检测也就失去意义。

**练习 2**：模块里有一条 `pulses_in → pulse_out` 的组合路径，注释说这是「必要的」。请解释：如果改成「下一拍才报告」，会带来什么坏处？

**参考答案**：很多用途依赖「第 N 个事件到达的**当拍**就发出信号」——例如「当前这一笔正好填满缓冲的最后一个空位」必须当拍报告给下游，下游才能在下一拍及时反应。若引入一拍延迟，下游就永远慢一拍，可能在已经溢出后才收到「满」信号。所以这条组合路径是有意的时序取舍（代价是该路径要满足时序约束）。

**练习 3**：把 `pulses_in` 常接 `1'b1` 后，`Pulse_Divider` 相当于一个什么样的电路？为什么说它比「用逻辑生成一个分频时钟」更好？

**参考答案**：它相当于一个**产生周期性使能脉冲**的电路——每 `divisor` 个主时钟周期拉高 1 拍。这比「分频出新时钟」更好，因为它没有创造新的时钟域（新时钟会带来时钟偏斜、需要单独约束、影响 CDC 分析），所有逻辑仍跑在唯一的主时钟上，只是用一个使能脉冲去门控——这正是 FPGA 设计里「用时钟使能替代分频时钟」的标准建议。

---

## 5. 综合实践

把本讲三个模块串成一个完整的「**事件统计与节流**」设计。

**场景**：一个传感器每个完成测量的周期会拉高 `done` 一拍（脉冲形式）。下游有两路需求：(A) 一个 FSM 要在「至少有一次测量完成」后才开始工作，但它当前正忙，可能错过那一拍；(B) 一个慢速记录器只想每 4 次测量记录一次。

**任务**：

1. **选型与连线**：用本讲的三个模块搭出两条处理路径。
   - 路径 A：把 `done`（脉冲）转成「有未处理测量」的持续电平 `measurement_pending`，供 FSM 稍后读取；FSM 处理完后给你一个 `ack` 一拍脉冲来清掉它。指出该用哪个模块、`pulse_in`/`clear` 分别接什么。
   - 路径 B：对 `done` 脉冲流做 4 分频，得到 `record_now`。指出该用哪个模块、`divisor` 设多少。
2. **时序核验**：用 4.1、4.2、4.3 的源码，回答：路径 A 里 `measurement_pending` 相对 `done` 延迟几拍？为什么 `ack` 能可靠地清掉它（引用 `Register` 的 last-assignment-wins）？
3. **边沿用法**：如果 `done` 来自一个可能持续高好几拍的信号（而非严格一拍），路径 B 还能正确计数吗？结合注释 [Pulse_Divider.v:12-14](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Divider.v#L12-L14) 说明。若你想「只在它真正上升那一拍计一次」，应该额外插一个哪个模块？
4. **除零防护**：若系统初始化阶段 `divisor` 还没准备好、暂时为 0，路径 B 会怎样？引用源码说明 `div_by_zero` 的行为，以及 `divisor` 变非零后能否自动恢复。

**参考要点**：

1. 路径 A 用 `Pulse_Latch`：`pulse_in` 接 `done`，`clear` 接 `ack`，`level_out` 即 `measurement_pending`。路径 B 用 `Pulse_Divider`：`divisor=4`（`INITIAL_DIVISOR=4`），`pulses_in` 接 `done`，`pulse_out` 即 `record_now`。
2. `measurement_pending` 相对 `done` 延迟 **1 拍**（`Pulse_Latch` 内部是 `Register`，时钟沿采样后才更新）。`ack` 能可靠清零，是因为 [Register.v:65-73](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v#L65-L73) 里 `clear` 的 `if` 在 `clock_enable`（=`pulse_in`）的 `if` 之后，非阻塞赋值「后写胜出」，所以即使 `done` 与 `ack` 同拍到来，`clear` 也必赢。
3. 能正确计数：注释 [Pulse_Divider.v:12-14](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Divider.v#L12-L14) 明确——`pulses_in` 高几拍就等于几个独立脉冲（常高 = 每拍一个）。若只想在上升沿计一次，应在 `done` 进 `Pulse_Divider` 之前插一个 `Pulse_Generator`，取 `pulse_posedge_out` 喂给 `pulses_in`。
4. `divisor=0` 时 `count=0`、`div_by_zero=1`、`pulse_out` 恒 0，且每拍 `load` 重装（[Pulse_Divider.v:114-116](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Divider.v#L114-L116) 与 [Pulse_Divider.v:120-125](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pulse_Divider.v#L120-L125)）；一旦 `divisor` 变非零，下一拍 `load` 把它装进 `count`，`div_by_zero` 清零，自动恢复正常分频。

## 6. 本讲小结

- **脉冲锁存 `Pulse_Latch`** 把一拍脉冲「冻」成持续电平，直到被 `clear` 清零；它本质是一个 `Register`（`clock_enable=pulse_in`、`data_in=1'b1`），靠 last-assignment-wins 让 `clear` 优先，是 4 相握手 set/clear 的原语。
- **脉冲生成 `Pulse_Generator`** 做边沿检测，把电平变化变回一拍脉冲；机制是把输入延迟 1 拍再与当前值组合比较，给出上升沿/下降沿/任意沿三种输出。关键性质：输出是组合的、与电平变化**同拍**出现，且输入必须已同步于时钟。
- **脉冲分频 `Pulse_Divider`** 每收到 `divisor` 个输入脉冲就发一个输出脉冲；核心是一个下计数器（`count` = 距下次输出还差几个脉冲），数到 1 即 `division_done` 并重装，永远绕过 0。
- `Pulse_Divider` 有意保留一条 `pulses_in → pulse_out` 的组合路径，使「第 N 个事件到达」与「信号发出」落在**同一拍**；`div_by_zero` 被拆到单独组合块以避开 linter 的假组合环误报。
- 三者共享本书一贯哲学：脉冲/电平的转换全由 `Register`/`Counter_Binary` 这些已测试的构建块拼出，模块本身只做连线与组合控制；输入都必须先同步到本时钟域，跨域要用 `CDC_*` 同步器。
- 三者合在一起，构成同一时钟域内「**事件捕获（Latch）→ 边沿还原（Generator）→ 事件计数（Divider）**」的完整脉冲处理工具链，是 FSM 简化、周期性使能生成、整数除法的常用零件。

## 7. 下一步学习建议

- 本讲的脉冲都还局限在**单一时钟域**。下一讲 **u15-l2（脉冲与流水线互转）** 讲 `Pulse_to_Pipeline` 与 `Pipeline_to_Pulse`，把脉冲接口与 ready/valid 弹性流水线互转——本讲三个模块正是那两个转换器内部会反复用到的零件。
- 想看本讲模块在 CDC 场景里的真实用法，回看 **u14-l1（字同步与脉冲同步）**：`Pulse_Latch` 是 4 相握手原语、`Pulse_Generator` 把 toggle 边沿还原成接收脉冲。
- 想再深入 `Pulse_Divider` 背后的计数器实现，回看 **u8-l2（二进制计数器与可复用函数）**，理解 `Counter_Binary` 的 `clear > load > run` 优先级与 `load` 覆盖 `run` 的机制——这正是本讲「绕过 0」能成立的基础。
