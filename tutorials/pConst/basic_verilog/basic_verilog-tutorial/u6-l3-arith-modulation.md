# 算术与调制：加法树、滤波、PWM/PDM

## 1. 本讲目标

本讲把视野从「单个信号的处理」推进到「一批数据的并行计算与模拟量生成」。读完本讲，你应当能够：

- 看懂 `adder_tree` 如何用分层的流水线加法器，在尽量短的时钟周期内把任意多个数加在一起，并自动推算出和的位宽。
- 理解 `moving_average` 如何用一个「延迟线 + 累加器」实现滑动窗口平均，以及为什么「2 的幂次窗口」最省资源。
- 区分 **PWM（脉冲宽度调制）** 与 **PDM（脉冲密度调制）**：一个周期固定、改脉宽；一个脉宽几乎固定、改周期，从而改变电平的「时间平均」。
- 能够把 `moving_average` 和 `pwm_modulator` 串起来，把一个数字序列变成占空比随之变化的方波。

本讲的四个模块在 README 的模块表里都没有打绿圈/红圈难度标记，属于「常用数据处理 IP」一类，前置只需掌握前面讲过的 `clk_divider`（[u2-l1](u6-l3-arith-modulation.md)）和 `delay`（u2-l3）。

## 2. 前置知识

本讲会用到以下你已经学过的概念（若遗忘可回看对应讲义）：

- **自由运行计数器与派生时钟**（u2-l1，`clk_divider`）：一个不断 `+1` 的二进制计数器，其第 N 位天然是主时钟的 \(1/2^{N+1}\) 分频。本讲的 PWM/PDM 调制器会用 `clk_divider` 的某一位当作慢时钟。
- **静态延迟线**（u2-l3，`delay`）：把信号整体向后平移 LENGTH 拍。本讲的 `moving_average` 内部就例化了一个 `delay` 来「记住窗口里最老的那个样本」。
- **位宽计算**（u2-l4，`$clog2`）：把 N 个数加起来，和的位宽要扩 \(\lceil\log_2 N\rceil\) 位；这正是 `adder_tree` 自动计算输出位宽的依据。
- **SystemVerilog 基础写法**（u1-l2）：`always_ff`/`always_comb`、非阻塞 `<=` 与阻塞 `=`、参数化端口 `#(parameter ...)`、`generate` 块。

另外补充两个本讲要用的硬件术语：

- **流水线（pipeline）**：把一个较慢的组合运算拆成多级，每级之间插入寄存器，让整体能跑到更高的时钟频率，代价是结果延迟若干拍才出来。
- **模拟量的「时间平均」**：数字管脚只能输出 0 或 1，但只要 1 出现的比例（占空比/密度）可控，低通滤波后就能得到介于 0 和 Vcc 之间的「平均电压」，这就是 PWM/PDM 驱动电机、LED 调光、音频 DAC 的物理基础。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲定位 |
|------|------|----------|
| [adder_tree.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/adder_tree.sv) | 参数化流水线加法树，把 N 个输入并行求和 | 最小模块①：加法树 |
| [moving_average.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/moving_average.sv) | 滑动窗口平均，内部复用 `delay` | 最小模块②：滑动平均 |
| [pwm_modulator.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pwm_modulator.sv) | 脉冲宽度调制发生器，周期固定、改脉宽 | 最小模块③：PWM 调制 |
| [pdm_modulator.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pdm_modulator.sv) | 脉冲密度调制发生器，周期随设定值变化 | 最小模块④：PDM 调制 |
| [pulse_gen.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pulse_gen.sv) | 通用脉冲发生器，PWM/PDM 都靠它「画」波形 | 依赖件（u6-l4 会专门讲） |
| [clk_divider.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clk_divider.sv) | 自由计数器，提供派生慢时钟 | 依赖件（u2-l1 已讲） |
| [delay.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv) | 静态延迟线，`moving_average` 用它取老样本 | 依赖件（u2-l3 已讲） |

四个 testbench（`adder_tree_tb.sv` / `moving_average_tb.sv` / `pwm_modulator_tb.sv` / `pdm_modulator_tb.sv`）会在各模块的「代码实践」中作为参照。

---

## 4. 核心概念与源码讲解

### 4.1 加法树：adder_tree（并行求和）

#### 4.1.1 概念说明

假设你有 8 个数要加在一起。最朴素的写法是 `sum = a0+a1+a2+a3+a4+a5+a6+a7;`——综合器会把它们串成一条 7 级的加法链，关键路径最长，能跑的频率最低。

**加法树（adder tree）** 的思路是「两两配对、分层相加」，像淘汰赛一样把 N 个数压成 1 个：

```
第0层(输入):  a0 a1 | a2 a3 | a4 a5 | a6 a7
第1层:          b0   |   b1   |   b2   |   b3      (b0=a0+a1 ...)
第2层:                c0      |       c1           (c0=b0+b1)
第3层:                        d0                    (d0=c0+c1 = 总和)
```

- 8 个输入只需要 3 层加法（\( \lceil\log_2 8\rceil = 3 \)），关键路径从 7 级缩到 3 级。
- 如果每层之间插寄存器（流水线），每一级只有一个加法器的延时，整体频率可以拉得很高，代价是结果要等几拍才出来。

`adder_tree` 就是把上面这张图参数化的产物：输入个数 N **不必是 2 的幂**（不是 2 的幂时用 0 把缺口补齐到最近的 2 的幂），位宽、层数、输出位宽全部由参数自动推算。

#### 4.1.2 核心流程

记输入个数为 `INPUTS_NUM = N`，每个输入 `IDATA_WIDTH = W` 位。模块在编译期算出三个派生参数：

\[
\text{STAGES\_NUM} = \lceil\log_2 N\rceil
\]

\[
\text{INPUTS\_NUM\_INT} = 2^{\text{STAGES\_NUM}} \quad (\text{向上补齐到的 2 的幂})
\]

\[
\text{ODATA\_WIDTH} = W + \lceil\log_2 N\rceil
\]

输出位宽之所以是 \(W + \lceil\log_2 N\rceil\)，是因为 N 个 W 位数相加，最大值不超过 \(N\cdot(2^W-1)\)，需要的位数恰好多出 \(\lceil\log_2 N\rceil\) 位。

执行流程（伪代码）：

```
用一个三维数组 data[层][编号][位宽] 暂存每层每个节点的值
第 0 层：把 N 个输入填进去，不足的补 0          （组合）
第 k 层(k≥1)：相邻两两相加，结果写进下一层      （时序，每级一拍）
最后：data[STAGES_NUM][0] 就是总和，连到 odata
```

因为第 1 层及以后都写在 `always_ff` 里，所以这是一棵**流水线加法树**：输入变化后，要经过 `STAGES_NUM` 拍，`odata` 才稳定。

#### 4.1.3 源码精读

参数与端口定义，输出位宽自动加 \(\lceil\log_2 N\rceil\) 位：[adder_tree.sv:28-40](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/adder_tree.sv#L28-L40)。注意 `INPUTS_NUM` 默认 125（不是 2 的幂），`STAGES_NUM = $clog2(INPUTS_NUM)` 与 `INPUTS_NUM_INT = 2 ** STAGES_NUM`（即 128）用来把缺口补齐。

核心数据结构是一个三维数组，下标分别是「层 / 该层节点编号 / 位宽」：[adder_tree.sv:43](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/adder_tree.sv#L43)。层维从 0 到 `STAGES_NUM`，节点维用补齐后的 `INPUTS_NUM_INT`。

整个树用一个 `generate` 循环按层展开：[adder_tree.sv:46-85](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/adder_tree.sv#L46-L85)。其中：

- 第 0 层是「模块输入」，用 `always_comb` 把 N 个输入填进数组，多出来的高位补 0：[adder_tree.sv:53-66](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/adder_tree.sv#L53-L66)。`adder < INPUTS_NUM` 时填真实数据，否则（缺口）填 0。
- 第 1 层及以后是「加法节点」，用 `always_ff @(posedge clk)` 实现，每拍把上一层相邻两节点相加：[adder_tree.sv:67-83](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/adder_tree.sv#L67-L83)。关键一句是：

```verilog
data[stage][adder][ST_WIDTH-1:0] <=
        data[stage-1][adder*2  ][(ST_WIDTH-1)-1:0] +
        data[stage-1][adder*2+1][(ST_WIDTH-1)-1:0];
```

`adder*2` 与 `adder*2+1` 正是「淘汰赛里两两配对」的下标；`ST_WIDTH = IDATA_WIDTH + stage` 让每层位宽自然 +1，吸收进位。每一级都打一拍寄存器，所以这是流水线结构。

> 提示：源码注释 `// is also possible here` 指出，把这里的 `always_ff` 改成 `always_comb`（见 [adder_tree.sv:71](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/adder_tree.sv#L71) 注释行），整棵树就变成**纯组合**加法树——面积更小、延迟更长，是「频率」与「面积」的取舍。

最顶层（`STAGES_NUM` 层的第 0 个节点）就是总和，直接连到输出：[adder_tree.sv:87](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/adder_tree.sv#L87)。

#### 4.1.4 代码实践

**实践目标**：在仿真里验证「7 个输入相加」的求和结果与位宽推算。

仓库自带的 testbench 正好例化了 `INPUTS_NUM=7`、`IDATA_WIDTH=16` 的加法树：[adder_tree_tb.sv:84-98](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/adder_tree_tb.sv#L84-L98)。它把一个随机数 `RandomNumber1` 做不同位窗切片后零扩展成 7 个 16 位输入。

操作步骤（**示例代码**，需要你新建一个最小 testbench 文件，例如 `my_at_tb.sv`）：

1. 例化 `adder_tree #(.INPUTS_NUM(7), .IDATA_WIDTH(16))`，时钟 10ns。
2. 在 `initial` 里给定 7 个已知输入，例如 `{16'd1, 16'd2, 16'd3, 16'd4, 16'd5, 16'd6, 16'd7}`（注意高位对应下标 6，下文「小练习」会讨论拼接顺序）。
3. 等待 `STAGES_NUM = $clog2(7) = 3` 拍后，用 `$display` 打印 `odata`。

需要观察的现象：因为默认是流水线实现，`odata` 不会立刻等于 28，而是延迟 3 个 `clk` 上升沿后才稳定为 28。

预期结果：`odata = 28`（= 1+2+3+4+5+6+7），位宽为 `ODATA_WIDTH = 16 + 3 = 19` 位。若你把 [adder_tree.sv:72](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/adder_tree.sv#L72) 的 `always_ff` 改成 `always_comb`，则 `odata` 当拍即等于 28，无需等待。

> 仓库自带的 `adder_tree_tb.sv` 把 `.odata( )` 留空（[adder_tree_tb.sv:97](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/adder_tree_tb.sv#L97)），它只用于「在波形里肉眼看」，没有自校验——所以本实践让你补上 `$display` 自检。具体波形数值「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`INPUTS_NUM=125`、`IDATA_WIDTH=16` 时，`STAGES_NUM`、`INPUTS_NUM_INT`、`ODATA_WIDTH` 各是多少？

**答案**：`STAGES_NUM = $clog2(125) = 7`（因为 \(2^6=64<125\le128=2^7\)）；`INPUTS_NUM_INT = 2^7 = 128`（补齐到 128，缺口 3 个填 0）；`ODATA_WIDTH = 16+7 = 23` 位。

**练习 2**：为什么 `adder_tree_tb.sv` 里 `idata` 的拼接要写成 `{ 16'd0,RandomNumber1[7:0], ... }` 这种「每个切片前面补 12 个 0」的形式，而不是直接拼 7 个 `RandomNumber1`？

**答案**：`idata` 是 `[INPUTS_NUM-1:0][IDATA_WIDTH-1:0]` 的打包数组，每个元素 16 位；而 `RandomNumber1` 只有 16 位。作者用不同的位窗 `[7:0]`、`[8:1]`……造出 7 个互不相同的 8 位数，再各自零扩展到 16 位，凑成 7 路独立的「伪随机」输入，好让求和结果非平凡、便于在波形里观察。

---

### 4.2 滑动平均：moving_average

#### 4.2.1 概念说明

**滑动平均（moving average, MA）** 是最常用的数字滤波：维护一个长度为 `DEPTH`（窗口）的缓冲区，每来一个新样本，就输出当前窗口内所有样本的平均值，相当于一个低通滤波器，能把含噪声的序列「磨平」。

朴素实现需要每个时钟把窗口里 `DEPTH` 个数全加一遍——窗口一大，加法器就极宽。`moving_average` 用了一个 O(1) 的技巧：**保留上一次的和，每拍只加新样本、减最老样本**。

设窗口长度为 \(D\)，当前窗口和为 \(S_t\)，输入序列为 \(x_t\)，则：

\[
S_t = S_{t-1} + x_t - x_{t-D}
\]

\[
\text{MA}_t = \frac{S_t}{D}
\]

也就是说，「最老的样本」\(x_{t-D}\) 正好是当前输入 \(x_t\) 延迟 \(D\) 拍后的值——所以只要拿一条长度为 `DEPTH` 的延迟线（就是 u2-l3 讲过的 `delay`），就能「免费」取到要被剔除的那个老样本。每拍只做 1 次加法 + 1 次减法 + 1 次除法，与窗口大小无关。

#### 4.2.2 核心流程

```
id  ──┬──────────────────────────────► (新样本 x_t，加进 sum)
      │
      └─► delay(LENGTH=DEPTH) ────────► id_delayed (最老样本 x_{t-D}，从 sum 减掉)

moving_sum:  每拍 <= moving_sum + id - id_delayed   （时序）
od:          moving_sum / DEPTH                       （组合除法）
```

两个工程要点：

1. **位宽扩展**：D 个 W 位数相加，和最多需要 \(W + \lceil\log_2 D\rceil\) 位，所以 `moving_summ` 的位宽比输入宽 `DEPTH_W` 位。
2. **除法**：除以 `DEPTH` 是组合除法。**当 `DEPTH` 是 2 的幂时，除法退化为右移**，几乎不耗资源；不是 2 的幂时，综合器会生成真正的除法器（或用常数除法器），代价大得多。这就是 INFO 里强调「2 的幂次实现最高效」的原因。

#### 4.2.3 源码精读

参数与端口，`DEPTH_W = $clog2(DEPTH)` 用于扩展和的位宽：[moving_average.sv:33-46](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/moving_average.sv#L33-L46)。

内部例化 `delay` 取「最老样本」：[moving_average.sv:49-61](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/moving_average.sv#L49-L61)。注意 `TYPE` 默认 `"CELLS"`（用寄存器实现延迟线），可改成 `"ALTERA_BLOCK_RAM"` 让 Quartus 推断成块 RAM——窗口很大时能显著省寄存器。这正是 u2-l3 讲过的 `delay` 多实现切换。

累加器更新，正是「加新、减老」一步到位：[moving_average.sv:63-73](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/moving_average.sv#L63-L73)。关键三行：

```verilog
moving_summ[...] <= ( moving_summ[...]
                      + id[...]              // adding new item
                      - id_delayed[...] );   // subtracting the last one
```

`moving_summ` 的声明位宽是 `[DATA_W-1+DEPTH_W:0]`（共 `DATA_W+DEPTH_W` 位，注释「considering width expansion」），刚好容下 D 个 W 位数的和。

输出做除法（组合）：[moving_average.sv:75-78](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/moving_average.sv#L75-L78)，注释明确点出「DEPTH 是 2 的幂时除法退化为简单移位」。

> 验证思路：仓库的 `moving_average_tb.sv` 用 `DEPTH=255`（注意 255 **不是** 2 的幂）、`DATA_W=32`，并用一个阶跃输入（前 300 拍 `id='1`，之后 `id='0`）驱动它，用来观察「阶跃响应」的上升与保持：[moving_average_tb.sv:92-124](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/moving_average_tb.sv#L92-L124)。`DEPTH=255` 意味着这里的除法是「真除法」，资源开销最大——是一个反面教材，提醒你尽量用 256 这样的 2 的幂。

#### 4.2.4 代码实践

**实践目标**：用一个阶跃输入观察滑动平均的「爬坡」过程，体会窗口长度的含义。

操作步骤：

1. 复制 [moving_average_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/moving_average_tb.sv) 改成你自己的 testbench，把例化参数改为 `.DEPTH(8)`、`.DATA_W(16)`，并把 `.od( )` 接到一个 `logic [15:0] od;` 上（原 tb 此处留空，需要补上）。
2. 输入激励：前 20 拍 `id = 16'd100`，之后 `id = 16'd0`（即一个高度为 100、宽度为 20 的脉冲）。
3. 用 iverilog 编译运行（参考 u1-l3 的 `iverilog -g2012` 命令），打印每拍的 `od`。

需要观察的现象：`od` 会从 0 逐拍爬升，大约第 8 拍接近 100（窗口填满），第 20 拍之后又逐拍下降，约第 28 拍回到 0——这就是「窗口」在数据流上滑动的过程。

预期结果：稳态最大值约为 100（= 单点值 × 100 / 8 × 8，因为窗口内全是 100），上升/下降过渡各约 8 拍。由于 DEPTH=8 是 2 的幂，这里的 `/8` 会被综合成右移 3 位。

> 「待本地验证」：具体爬坡曲线请在 GTKWave 或 `$display` 打印里确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么作者在 testbench 里偏偏选 `DEPTH=255`，而不是 256？

**答案**：这是一个「演示真除法代价」的取舍——255 不是 2 的幂，`od = moving_summ / 255` 无法退化成移位，综合器必须生成实际的除法电路。若改成 256，则 `/256` 退化为丢掉低 8 位，几乎零开销。INFO 里「2^N implementations are the most efficient」说的就是这件事。

**练习 2**：如果输入恒为常数 C，`moving_average` 稳定后输出是多少？和窗口长度有关吗？

**答案**：稳态时窗口里 D 个样本都是 C，和为 \(D\cdot C\)，再除以 D 等于 C——与窗口长度无关。窗口长度只影响「达到稳态需要的拍数」和「对噪声的平滑程度」，不影响对常数的稳态增益（增益恒为 1）。

---

### 4.3 PWM 调制：pwm_modulator

#### 4.3.1 概念说明

**脉冲宽度调制（Pulse Width Modulation, PWM）**：输出一个**周期固定**的方波，用「高电平占整个周期的比例」（占空比）来表示一个设定值。设定值越大，高电平越宽，低通滤波后的平均电压越高——常用于电机调速、LED 调光。

例如周期 256 拍、设定值 64，则每 256 拍里有 64 拍高电平，占空比约 25%。

`pwm_modulator` 的实现思路极其精简：它**复用** `pulse_gen`（通用脉冲发生器）来「画」波形，自己只负责把「设定值」翻译成 `pulse_gen` 的两个参数 `cntr_max`/`cntr_low`，并提供一个慢时钟。

回顾 `pulse_gen` 的语义（见 [pulse_gen.sv:14-30](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pulse_gen.sv#L14-L30) 的注释）：它内部有个倒计数器 `seq_cntr`，从 `cntr_max` 数到 0 为一个周期；输出 `pulse_out` 在 `seq_cntr >= cntr_low` 时为高。所以：

- `cntr_max` 决定**周期长度**（周期 = `cntr_max + 1`）；
- `cntr_low` 决定**从哪一刻开始拉低**，即决定高电平的宽度。

#### 4.3.2 核心流程

PWM 的关键选择：**周期固定、只改脉宽**。

\[
\text{cntr\_max} = 2^{\text{MOD\_WIDTH}} - 1 \quad (\text{常数，决定固定周期})
\]

\[
\text{cntr\_low} = (2^{\text{MOD\_WIDTH}} - 1) - \text{mod\_setpoint} \quad (\text{随设定值变化})
\]

`pulse_gen` 从 `cntr_max` 倒数到 `cntr_low` 期间输出高电平，所以高电平拍数为：

\[
\text{高电平拍数} = \text{cntr\_max} - \text{cntr\_low} + 1 = \text{mod\_setpoint} + 1
\]

占空比（设 MOD_WIDTH=8，周期 256）：

\[
d = \frac{\text{mod\_setpoint} + 1}{2^{\text{MOD\_WIDTH}}}
\]

设定值 0 → 几乎恒低（1/256 高）；设定值 255 → 几乎恒高。设定值越大，脉宽越宽，平均电压越高。

为了让 PWM 频率落在可听/可驱动范围（默认约 1.5 kHz），模块用 `clk_divider` 产生一个慢时钟 `div_clk`，并取其中一位 `div_clk[(PWM_PERIOD_DIV-1)-MOD_WIDTH]` 当作 `pulse_gen` 的工作时钟。

#### 4.3.3 源码精读

参数：默认 100 MHz 主时钟、`PWM_PERIOD_DIV=16`（故 PWM 频率 \(100\text{M}/2^{16}\approx1.526\,\text{kHz}\)）、`MOD_WIDTH=8`：[pwm_modulator.sv:34-50](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pwm_modulator.sv#L34-L50)。

例化 `clk_divider` 得到 32 位派生时钟总线：[pwm_modulator.sv:54-62](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pwm_modulator.sv#L54-L62)。这正呼应 u2-l1 讲过的「计数器位 = 分频时钟」。

设定值取反：`mod_setpoint_inv = 全1 - mod_setpoint`：[pwm_modulator.sv:66-67](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pwm_modulator.sv#L66-L67)。因为 `pulse_gen` 是「`seq_cntr >= cntr_low` 时为高」，要让「设定值大→高电平宽」，就得把设定值取反再交给 `cntr_low`。

把翻译好的参数交给 `pulse_gen`，并把它的时钟接到派生慢时钟的某一位：[pwm_modulator.sv:71-85](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pwm_modulator.sv#L71-L85)。关键三行：

```verilog
.clk( div_clk[(PWM_PERIOD_DIV-1)-MOD_WIDTH] ),   // 慢时钟
.cntr_max( {1'b0, {MOD_WIDTH{1'b1}} } ),          // = 2^MOD_WIDTH-1，固定周期
.cntr_low( {1'b0, mod_setpoint_inv[...] } ),      // 随设定值变化，决定脉宽
```

> 注意：这里把 `div_clk` 的某一位**直接当作 `pulse_gen` 的时钟**，等于在模块内部凭空生成了一个新的时钟域。这是 u2-l1 提醒过的「派生时钟」用法——能跑，但在真实工程里需要用 `create_generated_clock` 约束它，并留意这条时钟路径的时序。这是 `pwm_modulator` 简洁背后的代价。

#### 4.3.4 代码实践

**实践目标**：在波形里验证「设定值越大，PWM 占空比越大」。

仓库自带 testbench 用一个 32 点正弦表来扫描设定值，相当优雅：[pwm_modulator_tb.sv:100-132](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pwm_modulator_tb.sv#L100-L132)。它取 `MOD_WIDTH=5`（设定值 0~31）、`PWM_PERIOD_DIV=MOD_WIDTH+1=6`（注释说是最小允许值），每个 PWM 周期完成（`start_strobe` 来一次）就把表指针 `sp` 加 1，从而把一个正弦波「调制」到 PWM 占空比上。

操作步骤：

1. 直接编译运行 [pwm_modulator_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pwm_modulator_tb.sv)（需带上 `pwm_modulator.sv`、`pulse_gen.sv`、`clk_divider.sv`、`edge_detect.sv`、`c_rand` 等依赖）。
2. 在波形里同时观察 `sin_table[sp]`（设定值）和 `pwm1.pwm_out`。

需要观察的现象：当设定值处于正弦波峰（约 31）时，`pwm_out` 几乎恒高；处于波谷（约 0）时，`pwm_out` 几乎恒低；中间值则是占空比与之成比例的方波。整体看，`pwm_out` 的「包络」会呈现正弦形状。

预期结果：占空比随 `sin_table[sp]` 线性变化。若把 `pwm_out` 想象成经过低通滤波，得到的就是一个正弦波——这正是 PWM-DAC 的工作原理。「待本地验证」具体波形。

> 一个小坑：[pwm_modulator.sv:19-20](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pwm_modulator.sv#L19-L20) 的例化模板里 `PWM_PERIOD_DIV(16)` 后面漏了一个逗号，直接复制会报语法错；实际例化时记得补上。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `cntr_low` 要用「取反后的设定值」，而不是直接用 `mod_setpoint`？

**答案**：因为 `pulse_gen` 的输出在 `seq_cntr >= cntr_low` 时为高（[pulse_gen.sv:126-127](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pulse_gen.sv#L126-L127)）：`cntr_low` 越小，高电平越宽。若直接把 `mod_setpoint` 接到 `cntr_low`，则「设定值越大 → 高电平越窄」，与直觉相反；取反后 (`cntr_low = 最大值 - 设定值`)，设定值越大 → `cntr_low` 越小 → 高电平越宽，符合「设定值大 = 输出强」的约定。

**练习 2**：默认参数下 PWM 频率约 1.526 kHz，这个数是怎么来的？

**答案**：`pulse_gen` 的时钟是 `div_clk[(PWM_PERIOD_DIV-1)-MOD_WIDTH] = div_clk[7]`，即主时钟的 \(1/2^8 = 1/256\)（约 390.6 kHz）；`pulse_gen` 的周期是 `cntr_max+1 = 256` 拍。所以 PWM 总周期 = \(256 \times 256 = 65536\) 个主时钟周期，频率 = \(100\,\text{MHz}/65536 \approx 1525.88\,\text{Hz}\)，与 `PWM_PERIOD_HZ = CLK_HZ/(2**16)` 一致。

---

### 4.4 PDM 调制：pdm_modulator

#### 4.4.1 概念说明

**脉冲密度调制（Pulse Density Modulation, PDM）**：与 PWM「周期固定、改脉宽」不同，PDM 让**脉冲的疏密**随设定值变化。一种典型做法是：每个周期只发一个固定宽度的窄脉冲，但**周期的长短**由设定值决定——设定值大，周期长，脉冲稀疏（高电平占比高）；设定值小，周期短，脉冲密集（低电平占比高）。从「时间平均」看，密度就代表了模拟量。

`pdm_modulator` 的实现同样复用 `pulse_gen`，但参数翻译方式与 PWM **相反**：

- `cntr_max = mod_setpoint + 2` → **周期随设定值变化**（周期 = `cntr_max + 1`）；
- `cntr_low = 1` → 输出在 `seq_cntr >= 1` 时为高，即整个周期里**只有 `seq_cntr==0` 那一拍为低**。

也就是说，每个周期恰好出现 1 拍低电平「凹槽」，而周期的长度由设定值决定。

#### 4.4.2 核心流程

\[
\text{cntr\_max} = \text{mod\_setpoint} + 2, \qquad \text{cntr\_low} = 1
\]

\[
\text{周期 } T = \text{cntr\_max} + 1 = \text{mod\_setpoint} + 3 \quad (\text{拍})
\]

每个周期里高电平拍数为 \(T - 1 = \text{mod\_setpoint} + 2\)，低电平固定 1 拍，故平均输出（密度）：

\[
\bar{y} = \frac{\text{mod\_setpoint} + 2}{\text{mod\_setpoint} + 3}
\]

设定值越大 → 周期越长 → 那唯一的低电平凹槽越稀疏 → 平均输出越高。与 PWM 对比：

| 维度 | PWM | PDM |
|------|-----|-----|
| 周期 | 固定（`cntr_max` 常数） | 随设定值变化（`cntr_max = 设定值+2`） |
| 脉宽 | 随设定值变化（`cntr_low` 变） | 几乎固定（恒为 1 拍低凹槽） |
| 改变的量 | 高电平「宽度」 | 高电平「密度/频率」 |

#### 4.4.3 源码精读

参数：相比 PWM 多了 `PDM_MIN_PERIOD_HZ` / `PDM_MAX_PERIOD_HZ` 两个「边界频率」常量，因为 PDM 的周期是变的，所以最小/最大周期对应最低/最高密度：[pdm_modulator.sv:33-50](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pdm_modulator.sv#L33-L50)。注意 `PDM_MIN_PERIOD_HZ` 公式里的 `(0+2)`、`PDM_MAX_PERIOD_HZ` 里的 `(256+2)`，正是 `cntr_max = 设定值+2` 的体现。

同样例化 `clk_divider` 产生派生慢时钟：[pdm_modulator.sv:54-62](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pdm_modulator.sv#L54-L62)。

把设定值翻译成「变化的周期 + 固定的凹槽」交给 `pulse_gen`：[pdm_modulator.sv:66-80](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pdm_modulator.sv#L66-L80)。关键三行：

```verilog
.clk( div_clk[(PDM_PERIOD_DIV-1)-MOD_WIDTH] ),
.cntr_max( mod_setpoint[...] + 2 ),   // 周期随设定值变化
.cntr_low( 1 ),                        // 固定：只在 seq_cntr==0 时为低
```

与 PWM 的 `cntr_max`/`cntr_low` 一对照，就能立刻看出「谁固定、谁随设定值变」——这是理解两种调制差别的最直接抓手。

> 与 PWM 一样，`pulse_gen` 的时钟取自 `div_clk` 的某一位（[pdm_modulator.sv:69](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pdm_modulator.sv#L69)），属于派生时钟；同样需要 `create_generated_clock` 约束。例化模板 [pdm_modulator.sv:18-19](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pdm_modulator.sv#L18-L19) 也漏了逗号，使用时注意。

#### 4.4.4 代码实践

**实践目标**：对比同一设定值下 PDM 与 PWM 输出波形的不同。

仓库的 PDM testbench 与 PWM 的几乎同构，也用一个 32 点正弦表扫描设定值，只是推进表指针的节拍来源不同（这里用 `E_DerivedClocks[3]` 这一拍慢时钟上升沿）：[pdm_modulator_tb.sv:100-132](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pdm_modulator_tb.sv#L100-L132)。

操作步骤：

1. 编译运行 [pdm_modulator_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pdm_modulator_tb.sv)。
2. 在波形里对比 `pdm1.pdm_out` 与（如果同时例化了）`pwm1.pwm_out`。

需要观察的现象：
- PDM 输出大部分时间为高，每隔一段出现 1 拍很窄的低凹槽；
- 设定值大时（正弦峰），凹槽非常稀疏，输出平均接近 1；
- 设定值小时（正弦谷），凹槽密集，输出平均明显下降。
- 与 PWM 的「整齐等周期方波」不同，PDM 的低凹槽间距是变化的。

预期结果：PDM 波形不像 PWM 那样「周期整齐」，而是「疏密变化」；但两者的低通滤波结果（平均电平）都随设定值增大而升高。「待本地验证」具体波形。

#### 4.4.5 小练习与答案

**练习 1**：PDM 的 `cntr_low` 为什么固定写 `1` 而不是像 PWM 那样随设定值变化？

**答案**：因为 PDM 调制的是「密度」而非「宽度」。固定 `cntr_low=1` 意味着每个周期只有 1 拍低电平（脉宽固定），真正随设定值变化的是周期长度 `cntr_max = 设定值+2`。周期变了，那 1 拍低电平在时间轴上的「出现频率」就变了，从而改变了高电平的密度。这正是 PDM 与 PWM 的根本区别。

**练习 2**：同样的设定值（例如 MOD_WIDTH=8，设定值=128），PWM 和 PDM 哪个输出更像「稳定的方波」？

**答案**：PWM 更像稳定方波——它的周期固定为 256 拍，每个周期高电平宽度固定为 129 拍，波形严格周期重复。PDM 的周期是 `128+3=131` 拍且随设定值变化，波形虽也周期重复，但「几乎全高 + 1 拍凹槽」的形状与方波相去甚远，更像是带稀疏负脉冲的常高电平。所以驱动需要「干净周期波形」的器件（如伺服电机）多用 PWM，而 1-bit DAC、数字音频多用 PDM/ΔΣ。

---

## 5. 综合实践

把本讲的「滑动平均」与「PWM 调制」串成一条数据通路：**含噪声的计数序列 → 滑动平均（去噪平滑）→ PWM 调制（把平均值转成方波占空比）**。

设计任务（**示例代码**，需你自行新建顶层与 testbench）：

1. **造含噪序列**：在 testbench 里用一个 8 位计数器，每拍叠加一个 ±2 的小随机扰动，作为 `moving_average` 的 `id`。
2. **滑动平均**：例化 `moving_average #(.DEPTH(8), .DATA_W(16))`，把含噪序列接到 `id`，取 `od`。
   - 这里特意选 `DEPTH=8`（2 的幂），让内部的 `/8` 退化成移位，资源最省。
   - 数据通路里 `moving_average` 内部还例化了 `delay`（[moving_average.sv:49-61](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/moving_average.sv#L49-L61)），所以综合时会把 `delay` 一并带上。
3. **PWM 调制**：把平均后的高 8 位接到 `pwm_modulator` 的 `mod_setpoint`（注意位宽对齐：`DATA_W=16` 的 `od` 取高 8 位给 `MOD_WIDTH=8`）。

```verilog
// 示例代码：顶层示意（仅说明连接关系，非仓库原有文件）
moving_average #(.DEPTH(8), .DATA_W(16)) ma (
    .clk(clk), .nrst(nrst), .ena(1'b1),
    .id({8'b0, noisy_counter}),   // 含噪序列
    .od(avg)                      // 16-bit 平滑结果
);

pwm_modulator #(
    .PWM_PERIOD_DIV(16),          // 注意模板里漏了逗号，这里补上
    .MOD_WIDTH(8)
) pwm1 (
    .clk(clk), .nrst(nrst),
    .mod_setpoint(avg[15:8]),     // 取高 8 位当设定值
    .pwm_out(led_pwm),
    .start_strobe(), .busy()
);
```

需要观察的现象：

- 在波形里先看 `noisy_counter`（毛糙的阶梯）与 `avg`（明显被磨平、滞后约 8 拍的阶梯）的对比，体会 `moving_average` 的低通滤波效果。
- 再看 `led_pwm`：当 `avg` 缓慢上升时，PWM 的高电平宽度应随之变宽（占空比升高）；`avg` 下降时占空比降低。整体上，PWM 占空比的「包络」正比于平滑后的计数值。

预期结果：含噪序列经 8 点滑动平均后被明显平滑；平滑值的高 8 位驱动 PWM，使占空比随计数趋势单调变化。若把 `led_pwm` 接到一个 RC 低通滤波器（上板时），就能在电容上看到一个跟随计数器上升/下降的模拟电压。

> 进阶（可选）：把 `pwm_modulator` 换成 `pdm_modulator`，其余不变，对比两者在「同一平均值」下输出波形的差异——这正好呼应 4.3/4.4 两节。完整波形的精确数值「待本地验证」。

---

## 6. 本讲小结

- `adder_tree` 用「两两配对、分层相加」的流水线把 N 个数加起来，关键路径从 \(O(N)\) 降到 \(O(\log N)\)，输入个数不必是 2 的幂（用 0 补齐），输出位宽自动扩 \(\lceil\log_2 N\rceil\) 位；把内部 `always_ff` 改成 `always_comb` 即得纯组合版本。
- `moving_average` 用「累加器 + 延迟线」实现 O(1) 滑动窗口平均：每拍 `sum += 新样本 - 最老样本`，最老样本由 `delay` 免费提供；窗口是 2 的幂时除法退化为移位，最省资源。
- `pwm_modulator` 复用 `pulse_gen`：周期固定（`cntr_max` 常数）、脉宽随设定值变化（`cntr_low = 最大值 - 设定值`），占空比 \(d=(\text{设定值}+1)/2^{\text{MOD\_WIDTH}}\)。
- `pdm_modulator` 同样复用 `pulse_gen`，但参数翻译相反：周期随设定值变化（`cntr_max = 设定值+2`）、脉宽固定（`cntr_low=1`），用脉冲的「疏密」而非「宽窄」表示模拟量。
- 两种调制器都把 `clk_divider` 的某一位当作 `pulse_gen` 的时钟，相当于在模块内部生成派生时钟域——简洁但需要 `create_generated_clock` 约束。
- 滤波与调制可以串联：`moving_average` 去噪后取高位驱动 `pwm_modulator`，就能把数字序列映射成可低通滤波的模拟量。

## 7. 下一步学习建议

- **深挖调制所依赖的 `pulse_gen`**：本讲的 PWM/PDM 只是 `pulse_gen` 的两种「配方」，建议下一站阅读 u6-l4（脉冲与事件发生），精读 [pulse_gen.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pulse_gen.sv) 的倒计数器、`busy` 与 `start_strobe` 逻辑，理解 `cntr_max`/`cntr_low` 的完整语义（含「恒高/恒低/脉冲」三种模式）。
- **回到 CDC 与时序约束**：两种调制器都用了派生时钟，若要把它带上板，需要会写 `create_generated_clock` 与 `set_false_path`——这正是 u7-l2（时序约束与收敛）的内容。
- **扩展阅读**：仓库里还有 `Advanced Synthesis Cookbook/arithmetic/` 下的另一套加法树实现（`adder_tree.v` / `adder_tree_layer.v` / `adder_tree_node.v`，Verilog-2001 风格，分文件分层），可与本讲的 SystemVerilog 一体化版本对照阅读，体会「参数化 generate」与「显式分层例化」两种风格的取舍。
- **综合实战**：当你学完 u6 全部数据处理模块后，可以进入 u7-l4，用「分频→去抖→边沿→FIFO→UART」把多个积木组装成完整系统，本讲的 `moving_average` 可作为其中「传感器数据预处理」一级插入。
