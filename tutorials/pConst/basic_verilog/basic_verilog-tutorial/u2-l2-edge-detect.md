# 边沿检测：edge_detect

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚「边沿」在数字电路里到底是什么，以及为什么用「当前拍的值」和「上一拍的值」做一次比较就能检测到它。
- 读懂 `edge_detect.sv` 的三件套结构：一级延迟寄存器 `in_d`、组合比较器、`generate` 选择输出级。
- 掌握 `WIDTH` 参数如何把一个 1 位边沿检测器扩展成「一组并行检测器」。
- 理解 `REGISTER_OUTPUTS` 参数在「组合输出（0 拍延迟）」与「寄存输出（1 拍延迟）」之间的取舍。
- 把 `edge_detect` 真正用起来：承接上一讲的 `clk_divider`，把它的某一位派生时钟变成主时钟域里的**单拍使能脉冲**（这正是 `debounce_v2.sv` 的核心手法）。

## 2. 前置知识

本讲默认你已经掌握下面这些概念（都在前几讲出现过），这里只用一两句话温习：

- **D 触发器 / 寄存器**：在时钟上升沿把输入端的值「拍」进输出端，并保持到下一个沿。它是「记住上一拍」的唯一手段。
- **组合逻辑 vs 时序逻辑**：组合逻辑（`always_comb`，`=`）没有记忆，输出随输入即时变化；时序逻辑（`always_ff`，`<=`）有记忆，只在时钟沿更新（详见 u1-l2）。
- **同步复位与异步复位**：本讲的 `edge_detect` 用 `anrst`（async reset，异步、低有效），敏感列表里多一个 `negedge anrst`；而 `clk_divider` 用 `nrst`（同步、低有效）。两者都是「低有效」，只是复位生效的时机不同（详见 u1-l2）。
- **`clk_divider` 的派生时钟关系**：自由计数器的第 `N` 位 `out[N]` 是主时钟的 \(1/2^{N+1}\) 分频，50% 占空比（详见 u2-l1）。
- **位运算真值表**：与 `&`、取反 `~`、异或 `^`。本讲会用 `in & ~in_d`、`in ^ in_d` 这类写法。

一个关键直觉先记在心里：**硬件里没有「瞬间」，只有「时钟拍」**。所以「上升沿」不是某个无穷短的尖峰，而是「这一拍信号是 1、而上一拍它还是 0」——这一拍我们就能用一个宽度恰好为 1 个时钟周期的脉冲把它「标记」出来。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [edge_detect.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect.sv) | 本讲主角。参数化边沿检测器，输出 `rising`/`falling`/`both` 三路脉冲。 |
| [debounce_v2.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/debounce_v2.sv) | **真实用法范例**。把 `clk_divider` 的某一位送进 `edge_detect`，得到单拍采样脉冲 `do_sample`。 |
| [edge_detect_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect_tb.sv) | 作者提供的 testbench。我们会读它的「设计意图」，但也会指出它和当前模块端口对不上的地方（作为「文档/测试滞后于代码」的真实案例）。 |
| [clk_divider.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clk_divider.sv) | 实践任务的输入信号源（承接 u2-l1）。 |

## 4. 核心概念与源码讲解

### 4.1 边沿检测的本质：延迟一拍再比较

#### 4.1.1 概念说明

所谓「边沿」，就是信号电平发生跳变的时刻：

- **上升沿（rising）**：从 0 跳到 1。
- **下降沿（falling）**：从 1 跳到 0。
- **任意边沿（both）**：只要跳变了，不分方向。

问题是：在一个同步时序电路里，我们怎么「看见」一次跳变？答案是**比较「这一拍的值」和「上一拍的值」**：

- 这一拍是 1、上一拍是 0 → 出现了上升沿；
- 这一拍是 0、上一拍是 1 → 出现了下降沿；
- 两拍不同 → 出现了任意边沿。

于是边沿检测器的硬件结构非常朴素：**一个 D 触发器（用来记上一拍的值）+ 一个比较器（把当前值和上一拍的值做位运算）**。这就是 `edge_detect` 的全部核心。

#### 4.1.2 核心流程

把输入记为 `in`，把延迟一拍后的值记为 `in_d`（d 代表 delayed）。三路输出的布尔关系是：

\[
\text{rising} = \text{in} \cdot \overline{\text{in\_d}}
\]

\[
\text{falling} = \overline{\text{in}} \cdot \text{in\_d}
\]

\[
\text{both} = \text{in} \oplus \text{in\_d} = \text{rising} + \text{falling}
\]

下面这张时序图（每列是一个时钟拍）能让你直观看到 `in_d` 滞后 `in` 一拍，而脉冲只在跳变那一拍出现：

```
拍号      | 0  1  2  3  4  5  6  7  8
in        | 0  0  1  1  1  0  0  1  0
in_d      | 0  0  0  1  1  1  0  0  1     <- 永远比 in 慢一拍
rising    | 0  0  1  0  0  0  0  1  0     <- in&~in_d
falling   | 0  0  0  0  0  1  0  0  1     <- ~in&in_d
both      | 0  0  1  0  0  1  0  1  1     <- in^in_d
```

注意三个现象：

1. `rising` 只在 `in` 由 0 变 1 的那一拍（第 2、7 拍）为 1，宽度恰好 1 拍。
2. `in_d` 是 `in` 整体右移一拍的结果。
3. 如果 `in` **每一拍都翻转**（toggle rate 100%），那么 `rising` 和 `falling` 会每一拍都跟着 `in` 走、`both` 会每一拍都为 1——`edge_detect` 就失效了。这一点作者也写进了模块头部的 INFO 注释里。

#### 4.1.3 源码精读

延迟线 `in_d` 用一个带异步复位的 `always_ff` 实现，作用就是「把 `in` 记一拍」：

[edge_detect.sv:L53-L61](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect.sv#L53-L61) —— 延迟线寄存器 `in_d`。`posedge clk or negedge anrst` 表示异步复位；复位时清零，否则每个时钟沿把 `in` 拍进 `in_d`。

比较器是一个纯组合 `always_comb`，把上面的三个布尔公式直接写出来：

[edge_detect.sv:L63-L70](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect.sv#L63-L70) —— 组合比较器，算出 `rising_comb`/`falling_comb`/`both_comb`。

这里有一个细节值得专门讲：每一行都用了 `{WIDTH{anrst}} & (...)` 把结果「与」上复位信号。`{WIDTH{anrst}}` 是把 1 位的 `anrst` 复制成 `WIDTH` 位宽的向量。这样在 `anrst=0`（复位期间）时，三个 `_comb` 信号被强制压成全 0，**避免复位释放的瞬间因为 `in_d` 内容不确定而冒出假边沿**。这是把复位当成「输出闸门」的小技巧。

> 小提醒：`both_comb` 写成 `rising_comb | falling_comb`，和公式里的异或 `in ^ in_d` 在「每次最多跳变一次」的前提下完全等价，但更直观。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认 `in_d` 确实滞后 `in` 一拍，且 `rising` 只在跳变那拍为 1。

**步骤**：

1. 打开 [edge_detect.sv:L53-L70](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect.sv#L53-L70)。
2. 在脑中（或纸上）给 `in` 喂入 `0,0,1,1,1,0,0`，逐拍填出 `in_d`、`rising_comb`、`falling_comb`、`both_comb` 的值，对照 4.1.2 的时序图。
3. 体会 `in_d` 为什么**必须**是 `always_ff`：如果把它改成 `always_comb`，它就无法「记住」上一拍，`in_d` 会等于 `in`，于是 `in & ~in_d` 永远是 0——检测器当场失效。

**预期结果**：你能口算出每一拍的四个信号值，并解释「延迟寄存器不可用组合逻辑替代」。

**待本地验证**：若想看波形，可使用本讲 4.4 节给出的 testbench 模板，把 `in` 换成一个手写方波。

#### 4.1.5 小练习与答案

**Q1**：如果输入 `in` 每个时钟周期都翻转一次（`in` 序列为 `0,1,0,1,0,1,...`），`rising`、`falling`、`both` 分别是什么样？

**参考答案**：`in_d` 序列是 `in` 右移一拍，即 `?,0,1,0,1,0,...`。于是从第二拍起 `in & ~in_d` 每拍都等于 `in`，`~in & in_d` 每拍都等于 `~in`，`both` 每拍都为 1。换言之，检测器分不清「边沿」了，因为它认为每一拍都在跳变——这正是 INFO 注释里那条警告的含义。

**Q2**：为什么 `in_d` 必须单独用一个 `always_ff`，而不能塞进 `always_comb`？

**参考答案**：边沿检测的本质是「比较两个不同时刻的值」，必须有记忆元件保存上一拍。组合逻辑没有记忆，`in_d` 一旦写成 `always_comb` 就会和 `in` 同步变化，二者永远相等，差值恒为 0。

### 4.2 参数化位宽：一个模块就是一组并行检测器

#### 4.2.1 概念说明

很多场景下我们要检测的不是 1 根线，而是一整条总线上的**每一位**是否发生了跳变（比如一组按键、一组状态标志）。`edge_detect` 用一个 `WIDTH` 参数把这件事一次性解决：当 `WIDTH=8` 时，`in`、`rising`、`falling`、`both` 都是 8 位向量，**每一位都独立地做一次完整的边沿检测**。这等价于把 8 个 1 位检测器并联在一起，所以作者在 INFO 里写："`WIDTH` parameter to simplify instantiating arrays of edge detectors"。

#### 4.2.2 核心流程

参数与端口声明在模块头部一次性写好：

[edge_detect.sv:L39-L51](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect.sv#L39-L51) —— 模块声明。两个参数：`WIDTH`（默认 1）、`REGISTER_OUTPUTS`（默认 `1'b0`）；端口 `in`/`rising`/`falling`/`both` 全是 `[WIDTH-1:0]`。

关键在于：4.1 节的比较器用的是**位运算**（`&`、`~`、`|`），不是缩减运算（`&` 写在表达式里是按位与）。所以同一行代码对 1 位和 32 位都成立——位宽由参数决定，电路结构在编译期自动展开成 `WIDTH` 份。这就是「参数化即复用」的威力，也是整个 basic_verilog 仓库的核心风格（见 u1-l2）。

#### 4.2.3 源码精读

注意参数声明里的类型限定 `bit [7:0] WIDTH`：

[edge_detect.sv:L39-L43](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect.sv#L39-L43) —— `WIDTH` 被限定为 `bit [7:0]`，即 8 位无符号，理论最大 255；`REGISTER_OUTPUTS` 被限定为 `bit [0:0]`，即只能取 0 或 1。这种「给参数加位宽类型」的写法既文档化了取值范围，也能让工具在例化时做范围检查。

#### 4.2.4 代码实践（思路型）

**目标**：体会「`WIDTH=N` 等价于 N 个独立检测器」。

**步骤**：设想 `WIDTH=8`、把 `in[7:0]` 接到一个 8 位自由计数器（比如再来一个 `clk_divider`，`WIDTH=8`）的输出上。

**需要观察的现象**：`rising[0]` 会在计数器最低位每次 0→1 时亮一拍（每个 clk 一次）；`rising[1]` 在次低位每次 0→1 时亮一拍（每 2 个 clk 一次）；`rising[7]` 每 256 个 clk 才亮一拍。每一位的脉冲频率互不相同、互不干扰。

**预期结果**：你理解了「位运算 + 参数位宽」天然支持并行检测，不需要写 8 份代码。完整可运行的版本见第 5 节综合实践。

#### 4.2.5 小练习与答案

**Q1**：要检测一条 32 位总线上每一位的上升沿，例化时怎么写？

**参考答案**：`edge_detect #(.WIDTH(32)) ed (.clk(clk), .anrst(anrst), .in(bus[31:0]), .rising(bus_rise[31:0]), ...);`。 thanks to 参数化，一行就把 32 个检测器全部生成出来。

**Q2**：`bit [7:0] WIDTH` 这个类型限定，理论上允许 `WIDTH` 最大是多少？真的能例化到那么大吗？

**参考答案**：类型上最大 255。但实际能否例化到这么宽，取决于目标器件的寄存器资源和时序收敛情况——参数给了上限，器件给了现实上限。

### 4.3 generate 选择实现：组合输出还是寄存输出

#### 4.3.1 概念说明

4.1 节的比较器算出的是 `_comb` 中间信号，还不是模块的最终输出 `rising`/`falling`/`both`。模块用 `REGISTER_OUTPUTS` 参数让你在两种「输出级」里二选一：

- **组合输出（`REGISTER_OUTPUTS=0`，默认）**：直接把 `_comb` 接到输出端口。脉冲在 `in` 跳变的**当拍**就出现，相对 `in` 延迟 0 拍。代价是输出挂在一条组合路径上，如果 `in` 本身来自组合逻辑，输出可能毛刺，且对下游模块的建立时间要求更紧。
- **寄存输出（`REGISTER_OUTPUTS=1`）**：把 `_comb` 再过一级 `always_ff`。脉冲推迟到 `in` 跳变的**下一拍**，相对 `in` 延迟 1 拍。好处是输出是干净的寄存器，时序更稳、能跑更高频率。

这二选一不是 `if` 语句在运行时判断的，而是用 `generate` 在**编译期**就决定到底生成哪一段电路——不用的那段根本不会出现在综合结果里。

#### 4.3.2 核心流程

`generate` 块里一个 `if/else`，两条分支各放一个 `always` 块。编译器根据 `REGISTER_OUTPUTS` 的值只保留其一：

| 模式 | `REGISTER_OUTPUTS` | `rising` 脉冲出现的拍 | 相对 `in` 的延迟 | 输出性质 |
|------|--------------------|------------------------|------------------|----------|
| 组合（默认） | `1'b0` | `in` 跳变的当拍 | 0 拍 | `always_comb`，组合路径 |
| 寄存 | `1'b1` | `in` 跳变的下一拍 | 1 拍 | `always_ff`，干净寄存输出 |

选哪一种？经验法则：**如果输出要驱动下一级时序逻辑、或者要进高扇出网络，优先选寄存输出（1）；如果只是就近驱动组合逻辑、且对延迟敏感，可选组合输出（0）**。

#### 4.3.3 源码精读

整个选择就在这一个 `generate` 块里：

[edge_detect.sv:L72-L98](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect.sv#L72-L98) —— `generate if/else`：`REGISTER_OUTPUTS==1'b0` 时用一个 `always_comb` 把 `_comb` 直通出去（L76-L80）；否则用一个带异步复位的 `always_ff` 把 `_comb` 寄存一拍（L85-L95）。

注意寄存分支里也带了 `posedge clk or negedge anrst` 和清零分支，所以两种实现**复位行为一致**，只是输出延迟差一拍。

#### 4.3.4 代码实践（对照型）

**目标**：在同一份 testbench 里同时例化两个 `edge_detect`，一个 `REGISTER_OUTPUTS=0`、一个 `=1`，喂同一个 `in`，对比 `rising` 错位一拍。

**步骤**：

1. 复制 4.4 节 testbench 里的 `edge_detect` 例化，得到 `ed_comb`（`.REGISTER_OUTPUTS(1'b0)`）和 `ed_reg`（`.REGISTER_OUTPUTS(1'b1)`）两个实例，`in` 都接同一个方波。
2. 仿真后在波形里对齐 `rising_comb`（来自 `ed_comb`）和 `rising_reg`（来自 `ed_reg`）。

**需要观察的现象**：`rising_reg` 比 `rising_comb` 整体晚一个时钟周期；两者脉冲宽度都恰好 1 拍。

**预期结果**：你亲眼看到「同一个参数从 0 改成 1，输出就多了一拍延迟」，并理解这是编译期电路不同导致的，不是运行时判断。

**待本地验证**：实际波形需用 iverilog 或 ModelSim 跑出。

#### 4.3.5 小练习与答案

**Q1**：`REGISTER_OUTPUTS` 的默认值是哪一个？

**参考答案**：`1'b0`，即默认组合输出、0 拍延迟。见 [edge_detect.sv:L41](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect.sv#L41)。

**Q2**：为什么组合比较器里每一项都要 `& {WIDTH{anrst}}`？

**参考答案**：把复位信号当成输出闸门。复位期间 `anrst=0`，三个 `_comb` 信号被强制清零，避免复位释放瞬间 `in_d` 还是未知值（X）而产生假边沿脉冲。寄存分支则通过 `if( ~anrst ) <= '0` 达到同样目的。

### 4.4 真实应用：把分频时钟位变成单拍使能脉冲

#### 4.4.1 概念说明

承接 u2-l1 的关键结论：`clk_divider` 的 `out[N]` 是一棵 50% 占空比的慢时钟树，**直接把它接到别的模块当时钟用，会新建一个时钟域，给时钟域跨越（CDC）带来麻烦**。更优雅的做法是：让所有逻辑都跑在快时钟 `clk` 上，把 `out[N]` 当成一个**普通数据信号**，用 `edge_detect` 取它的上升沿，得到一个「每 \(2^{N+1}\) 拍亮一拍」的单拍脉冲，当作本时钟域里的**使能 tick**。

这样得到的脉冲有两个绝佳性质：

1. 它**宽度恰好 1 个 `clk` 周期**，因为 `rising` 只在 `out[N]` 由 0 变 1 的那一拍为 1；
2. 它**完全同步于 `clk`**，可以直接写进 `always_ff` 的 `if (ena_tick)` 条件里，不引入新时钟域。

`debounce_v2.sv` 就是这条思路的教科书级应用：它用分频时钟的某一位上升沿，周期性地「采样」抖动的按键输入。

#### 4.4.2 核心流程

`debounce_v2` 内部的数据流分三步：

```
clk ──► clk_divider(WIDTH=32) ──► s_clk[31:0]            // 一棵 32 位慢时钟树
                                         │
                                         ▼
                              edge_detect(WIDTH=32)       // 对每一位做上升沿检测
                                         │
                                         ▼
                                s_clk_rise[31:0]           // 每位都是单拍脉冲
                                         │
                          do_sample = s_clk_rise[SAMPLING_FACTOR]
                                         │
                                         ▼
                          if (ena && do_sample) 采样输入   // 周期性采样
```

采样周期由 `SAMPLING_FACTOR` 选哪一位决定：选第 `N` 位，则采样每隔 \(2^{N+1}\) 个 `clk` 发生一次。这是一个**只用一个 `clk` 域、却得到可调慢速率**的经典手法。

#### 4.4.3 源码精读

先看 `debounce_v2` 怎么造那棵慢时钟树：

[debounce_v2.sv:L57-L65](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/debounce_v2.sv#L57-L65) —— 例化 `clk_divider` 得到 `s_clk[SAMPLING_RANGE-1:0]`（`SAMPLING_RANGE=32`）。

紧接着就是本讲主角的实战例化：

[debounce_v2.sv:L67-L75](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/debounce_v2.sv#L67-L75) —— 例化 `edge_detect` 对 `s_clk` 做上升沿检测，得到 `s_clk_rise`。注意这里端口名写的是 `.anrst( nrst )`，**和当前 `edge_detect` 的端口完全对得上**（这是一个「正确且最新」的例化范例，可与下面的 `edge_detect_tb.sv` 对照）。

最后挑出一位当采样脉冲：

[debounce_v2.sv:L77-L78](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/debounce_v2.sv#L77-L78) —— `do_sample = s_clk_rise[SAMPLING_FACTOR]`。这一行就是把「第 N 位分频时钟」翻译成了「每 \(2^{N+1}\) 拍一个的单拍使能」。

#### 4.4.4 代码实践（本讲主实践）

**目标**：复刻 `debounce_v2` 的核心手法——用 `edge_detect` 对 `clk_divider` 的某一位做上升沿检测，把得到的单拍脉冲接到「LED」（仿真里用波形观察），验证脉冲频率与原时钟的关系。

**操作步骤**：

1. 新建一个 testbench（下文给出了**示例代码**，非仓库原有文件），例化 `clk_divider`（`WIDTH=8`）和 `edge_detect`（`WIDTH=8`，默认组合输出）。
2. 把 `edge_detect` 的 `in` 接到 `clk_divider` 的 `out[7:0]`。
3. 把 `rising[7]` 当作「LED 驱动脉冲」观察。
4. 用 iverilog 编译运行（命令参考 u1-l3）：`iverilog -g2012 -o sim.vvp edge_detect.sv clk_divider.sv edge_detect_practice_tb.sv && vvp sim.vvp`，再用 GTKWave 打开 `.vcd`。

示例代码（本讲编写，便于你直接复制改写）：

```systemverilog
// 示例代码：edge_detect_practice_tb.sv（本讲编写，非仓库原有文件）
`timescale 1ns / 1ps

module edge_detect_practice_tb();

  logic clk = 1'b0;
  always #5 clk = ~clk;            // 10ns 周期 => 100 MHz 主时钟

  logic anrst = 1'b0;              // 先保持复位
  initial begin
    #25 anrst = 1'b1;              // 释放异步复位
  end

  // ① 用 clk_divider 造一棵 8 位慢时钟树（承接 u2-l1）
  logic [7:0] div;
  clk_divider #(
    .WIDTH( 8 )
  ) cd (
    .clk( clk ),
    .nrst( anrst ),                // clk_divider 是同步复位 nrst，低有效
    .ena( 1'b1 ),
    .out( div[7:0] )
  );

  // ② 对 div 做上升沿检测（默认组合输出）
  logic [7:0] rise;
  edge_detect #(
    .WIDTH( 8 ),
    .REGISTER_OUTPUTS( 1'b0 )
  ) ed (
    .clk( clk ),
    .anrst( anrst ),               // 注意端口名是 anrst（异步复位）
    .in( div[7:0] ),
    .rising( rise[7:0] ),
    .falling(  ),
    .both(  )
  );

  initial begin
    $dumpfile("edge_detect_practice.vcd");
    $dumpvars(0, edge_detect_practice_tb);
    #5000;                         // 跑足够长，能看到多次 rise[7]
    $finish;
  end

endmodule
```

**需要观察的现象**：

- `div[7]` 每 256 个 `clk` 翻转一次（周期 = 256 个 `clk`，因为 \(2^{7+1}=256\)）。
- `rise[7]` 在 `div[7]` 每次 0→1 的那一拍亮起，宽度恰好 1 拍。
- 数 `rise[7]` 两次拉高之间的 `clk` 周期数，应该是 256。

**预期结果**：`rise[7]` 的脉冲频率 = \(f_{\text{clk}} / 256\)，即原 100 MHz 主时钟的 \(1/256\)。这正是「单拍使能脉冲同步于主时钟、却拥有慢速率」的体现。你完全可以把 `rise[7]` 写进某个 `always_ff` 的 `if (rise[7])` 里做周期性任务，而不必新建时钟域。

> **关于作者自带的 testbench**：仓库里的 [edge_detect_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect_tb.sv) 设计意图很好——用随机数 `c_rand` 喂入、用实例数组 `edge_detect ED1[15:0]` 并联 16 个检测器、注入异步时钟抖动（读 [edge_detect_tb.sv:L13-L29](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect_tb.sv#L13-L29) 和 [L74-L81](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect_tb.sv#L74-L81)）。但它与当前模块对不上：里面例化的 `ClkDivider`（大写）实际模块名是小写的 `clk_divider`；并且给 `edge_detect` 连的是 `.nrst(...)`，而当前端口名是 `.anrst(...)`。这是「测试文件滞后于模块改名」的真实案例（和 u1-l1 提到的文档滞后同类），所以本讲实践我们用上面这份对齐了端口名的干净 testbench。读 `edge_detect_tb.sv` 时，重点学它的**激励写法和实例数组用法**即可。

#### 4.4.5 小练习与答案

**Q1**：`do_sample`（即 `s_clk_rise[SAMPLING_FACTOR]`）的脉冲宽度是几个时钟周期？

**参考答案**：1 个。因为它是 `edge_detect` 的 `rising` 输出，只在对应位 0→1 的那一拍为 1。

**Q2**：若 `SAMPLING_FACTOR=16`，`debounce_v2` 每隔多少个 `clk` 才采样一次输入？

**参考答案**：\(2^{16+1} = 131072\) 个 `clk`。因为 `s_clk[16]` 的周期是 \(2^{17}\) 个 `clk`，每个周期产生一次上升沿脉冲。

**Q3**：为什么 `debounce_v2` 不直接把 `s_clk[SAMPLING_FACTOR]` 接到某个触发器的时钟端，而要绕一圈用 `edge_detect`？

**参考答案**：直接当时钟用会新建一个慢时钟域，引发 CDC 问题、让时序约束变复杂。改用 `edge_detect` 产生单拍使能后，整块电路只跑在一个 `clk` 上，`do_sample` 只是 `always_ff` 里的一个使能条件，时序干净、约束简单。这正是 u2-l1 强调的「把 `out[N]` 当数据/使能，不要当时钟」。

## 5. 综合实践

把本讲的三件套串起来，搭一个「**分频 → 边沿检测 → LED 节拍**」的最小系统，并端到端验证脉冲周期。

**任务描述**：

1. 主时钟 100 MHz（10 ns 周期）。
2. 例化 `clk_divider`（`WIDTH=8`），得到 8 位派生时钟 `div[7:0]`。
3. 例化 `edge_detect`（`WIDTH=8`，`REGISTER_OUTPUTS=1`，即寄存输出），对 `div` 做上升沿检测，得到 `rise[7:0]`。
4. 把 `rise[7]` 想象成「LED 驱动脉冲」。在 testbench 里写一段自校验：用 `always @(posedge clk)` 计数 `rise[7]` 两次拉高之间的 `clk` 周期数 `gap`，断言 `gap == 256`，不符则 `$error`。
5. 同时把 `rise[7]` 接到一个仿真用的「LED 寄存器」`led`（`led <= ~led`），观察 `led` 翻转频率。

**预期结果**：

- `rise[7]` 每 256 个 `clk` 亮一拍；
- `led` 每 256 个 `clk` 翻转一次（周期 512 个 `clk`）；
- 自校验断言通过。

**进阶**：把 `REGISTER_OUTPUTS` 在 0 和 1 之间切换，确认寄存版相比组合版的 `rise[7]` 整体延后 1 拍，但周期仍是 256。这就把 4.1（延迟比较）、4.2（位宽并行）、4.3（generate 选实现）、4.4（分频时钟变使能）四件事一次性串了起来。

> 若本地没有 iverilog/ModelSim，可降级为「源码阅读型综合实践」：在纸上画出 `clk`、`div[7]`、`in_d`、`rise[7]`、`led` 的连续 257 拍波形，标注出 `rise[7]` 的脉冲位置，说服自己间隔确实是 256。

## 6. 本讲小结

- **边沿 = 当前拍与上一拍的差**。用一个 D 触发器 `in_d` 记住上一拍，再用组合比较器算出 `rising = in & ~in_d`、`falling = ~in & in_d`、`both = in ^ in_d`。
- **脉冲宽度恒为 1 拍**，且同步于 `clk`，因此可直接当作本时钟域的使能信号，不会引入新时钟域。
- **`WIDTH` 参数**让位运算式比较器自动展开成「一组并行检测器」，无需复制代码。
- **`generate` + `REGISTER_OUTPUTS`** 在编译期二选一：组合输出（0 拍延迟，默认）或寄存输出（1 拍延迟，时序更稳）。
- **`{WIDTH{anrst}}` 复位门控**强制复位期间输出为 0，避免假边沿。
- **真实用法**：`debounce_v2` 用 `edge_detect` 把 `clk_divider` 的某一位变成单拍采样脉冲 `do_sample`，实现「单时钟域、可调慢速率」——这正是承接 u2-l1 的最佳实践。

## 7. 下一步学习建议

- **向「多级延迟」扩展**：本讲的 `in_d` 只有一级。下一讲 [delay.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv)（u2-l3）把延迟链做成参数化长度，还能在 Xilinx SRL / Altera block RAM 之间切换实现，并引出 `false_path` 约束——它是 `edge_detect` 延迟思想的直接放大。
- **向「跨时钟域」延伸**：当你真的需要在两个不同时钟之间搬移信号时，单级延迟不够。u3 单元的 `cdc_data`（两级同步器）和 `cdc_strobe`（格雷计数器搬移单拍脉冲）正是建立在延迟 + 边沿检测之上。
- **通读 `debounce_v2.sv` 全文**：本讲只看了它的「采样脉冲」前半段，后半段的「窗口内全稳定才翻转输出」逻辑也值得作为时序状态机的阅读练习。
