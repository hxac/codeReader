# 移位寄存器与延时单元（shift_16/8/4/2/1）

## 1. 本讲目标

在上一讲（u3-l2）里，我们把 `radix2` 蝶形单元拆成了「first half 算和/差 → second half 把回流过来的差乘旋转因子」两段。但这里有一个被刻意略过的关键问题：**second half 需要的「差」从哪里来、又为什么能恰好和「下一个差」配对？**

答案就是本讲的主角——**移位延时寄存器 `shift_N`**。它是 SDC（单路延迟换向器，Single-path Delay Commutator）架构里把时间「拉长」的器件，负责把 first half 算出的「差」延迟若干拍，再送回蝶形单元参与 second half 运算。

学完本讲，你应当能够：

- 说清为什么 `shift_N` 用一个「超宽寄存器」就能实现 N 个样本的 FIFO 延时；
- 看懂 `(tmp_reg_r<<24) + din_r` 这一行左移拼接写法的精确含义，以及 `dout` 为什么取最高位窗口；
- 解释 `in_valid` / 内部 `valid` 的握手如何让延时线在数据流过后继续把残留样本冲刷干净；
- 归纳出「延时深度 16/8/4/2/1 逐级减半」的规律，并把它和 radix-2 DIF 的分组结构对应起来。

## 2. 前置知识

本讲默认你已经读过 u3-l1（顶层 `FFT.v` 的端口与位宽）和 u3-l2（`radix2` 蝶形的三态机）。需要重点回忆三个事实：

1. **数据通路是 24 位有符号**（`din_r/din_i`、`dout_r/dout_i` 都是 `signed [23:0]`）。
2. **复位是高有效异步复位**（敏感列表写 `posedge clk or posedge reset`），这点和顶层一致。
3. **radix2 的 first half 会输出两路**：和 → `op`（直送下一级），差 → `delay`（进入本讲的 `shift_N`），过 N 拍后回流成 `din_a`。

如果你还不熟悉「FIFO 移位寄存器」「位宽拼接」这类术语，下面这一句先记牢：**把很多个等宽的小格子首尾相连，每个时钟整体往前推一格，最老的那个样本就从队头被挤出去——这就是一个移位延时线。**

## 3. 本讲源码地图

本讲涉及的关键文件都在 `RTL/` 下，五个文件结构几乎一模一样，只是「宽度」不同：

| 文件 | 角色 | 关键看点 |
|------|------|---------|
| [RTL/shift_16.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/shift_16.v) | 第 1 级延时线（延时 16 拍） | 最宽的 384bit 寄存器，是理解全族的样板 |
| [RTL/shift_8.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/shift_8.v) | 第 2 级延时线（延时 8 拍） | 结构同上，宽度减半 |
| [RTL/shift_4.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/shift_4.v) | 第 3 级延时线（延时 4 拍） | 结构同上 |
| [RTL/shift_2.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/shift_2.v) | 第 4 级延时线（延时 2 拍） | 本讲精读对象之一 |
| [RTL/shift_1.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/shift_1.v) | 第 5 级延时线（延时 1 拍） | 退化为单寄存器，是验证规律的极端例子 |

顶层 [RTL/FFT.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v) 把这五个模块分别接在五级蝶形旁边，构成 `radix.delay → shift.din → shift.dout → radix.din_a` 的反馈回路。本讲会反复回到 `FFT.v` 的实例化片段，确认接线关系。

---

## 4. 核心概念与源码讲解

### 4.1 超宽移位寄存器结构

#### 4.1.1 概念说明

要做一个「延迟 N 个样本」的器件，最直观的两种实现是：

- **方案 A：开一个深度为 N 的小数组（每格 24bit），用读写指针循环访问。** 这是真正的 FIFO，需要维护读/写指针。
- **方案 B：开一个 N×24bit 的超宽寄存器，每个时钟把整体左移一格，新样本塞进最低位，最老样本从最高位读出。** 这是「移位寄存器风格的延时线」。

本项目选的是**方案 B**。它的好处是：没有指针、没有地址译码，时序非常干净，综合出来就是一长串触发器级联，对布局布线很友好；代价是寄存器位宽很大（`shift_16` 单是实部就有 384bit）。在 SDC 流水线里，这种「用面积换时序简洁」的取舍是常见且合理的。

> 术语：这种「一个超宽寄存器 + 整体移位」的写法，常被叫做 **shift-register-based delay line** 或 **delay buffer**。它本质上是一个没有外部读写的 FIFO——读写隐含在「移位」这一动作里。

#### 4.1.2 核心流程

以 `shift_16`（延时 16 个样本）为例，它内部维护一个 16 格的「队列」，每格 24bit：

```
位 [383:360]  [359:336]  ...  [47:24]  [23:0]
     格15        格14              格1     格0   ← 格0 是最新样本，格15 是最老样本
     ↑ dout                              ↑ din（新样本塞这里）
```

每个时钟（数据有效时）：

1. 整条队列向高位方向（向左）平移一格；
2. 新样本 `din_r` 进入格0（最低 24 位）；
3. 原来在格15 的最老样本被挤出去——因为它正好就是 `dout` 取的那一段，所以「挤出」=「输出」。

把 `shift_16` 看作 16 级 D 触发器串联，就能立刻得到它的延时：**一个样本从 din 进，要经过 16 拍才能爬到 dout 位置**，于是延时 = 16。

#### 4.1.3 源码精读

先看端口与寄存器声明。`shift_16` 的端口和 `radix2` 完全对齐：24 位有符号实部/虚部双通路。

[RTL/shift_16.v:14-29](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/shift_16.v#L14-L29) —— 模块端口与内部寄存器声明：注意 `shift_reg_r`/`shift_reg_i` 都是 `384bit`（`[383:0]`），这正是「16 格 × 24bit」的超宽寄存器；`tmp_reg_*` 是它在组合块里的「下一拍影子值」；`counter_16` 是 6 位计数器（后面 4.3 节会专门讨论它）。

```verilog
reg [383:0] shift_reg_r ;   // 16 格实部队列
reg [383:0] shift_reg_i ;   // 16 格虚部队列
reg [383:0] tmp_reg_r  ;    // 组合影子值
reg [383:0] tmp_reg_i  ;
reg [5:0] counter_16, next_counter_16;
reg valid, next_valid;
```

实部/虚部各一条独立的延时线，结构完全相同，所以后续只分析实部 `shift_reg_r`，虚部 `shift_reg_i` 同理。

> 对比阅读：把 [RTL/shift_2.v:24-29](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/shift_2.v#L24-L29) 和 [RTL/shift_1.v:24-29](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/shift_1.v#L24-L29) 拿来并排看，你会发现除「位宽常数」外整段一字不差——这正是「参数化模板被手工实例化成 5 份」的典型写法。

#### 4.1.4 代码实践

**实践目标**：用眼睛确认「寄存器宽度 = N × 24bit」这一关系，建立对超宽寄存器的直觉。

**操作步骤**：

1. 打开五个 `shift_N.v`，分别找到 `reg [?:0] shift_reg_r;` 那一行；
2. 记下每个文件里 `shift_reg_r` 的位宽上限（383 / 191 / 95 / 47 / 23）；
3. 把每个上限 +1，再除以 24。

**需要观察的现象**：每个结果都应当是一个整数——16、8、4、2、1，正好等于模块名后缀。

**预期结果**：宽度 = `(上限+1)/24 = N`，即 `shift_reg` 横向铺开了 N 个 24bit 格子。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `shift_16` 改造成一个延时 32 拍的 `shift_32`，`shift_reg_r` 的位宽应该声明成多少？`dout` 该取哪一段？

**参考答案**：位宽 = 32 × 24 = 768bit，声明为 `reg [767:0] shift_reg_r;`。最老样本在最顶端，`dout` 取最高 24 位 `shift_reg_r[767:744]`。

**练习 2**：为什么本项目用「超宽寄存器移位」而不是「深度为 N 的 RAM + 读写指针」来实现延时？

**参考答案**：移位寄存器写法无需地址译码和指针管理，时序路径就是触发器直连，利于高频（100MHz）下满足 10ns 约束；代价是面积（触发器多）。在字数较少、对时序敏感的 FFT PE 旁，这是一个合理取舍。

---

### 4.2 左移拼接与窗口读取

#### 4.2.1 概念说明

超宽寄存器「怎么动」全靠一行代码：`(tmp_reg_r<<24) + din_r`。这一行同时完成了三件事：

1. **左移 24 位**：把整条队列往高位方向推一格，等价于「丢掉最顶端的最老样本，最低端空出 24 个 0」；
2. **加 `din_r`**：把新样本填进最低端那 24 个 0 里（因为低位是 0，加法等价于拼接）；
3. **整体赋值**：得到的新队列写回 `shift_reg_r`。

而输出 `dout` 则用一个固定的「窗口」从队列最高位切出 24 位——也就是被挤出去的那个最老样本。**「左移进新、窗口取老」合起来就是一次 FIFO 推进。**

> 注意：这里的 `<<24` 之所以恰好是「一格」，是因为一格就是 24bit。24 这个常数来自数据通路位宽（u3-l1 里讲过的 24bit 定点通路）。所以「移位步长」和「数据位宽」是绑定的。

#### 4.2.2 核心流程

设队列当前内容为 `shift_reg_r`（W = N×24 位），新样本为 `din_r`（24 位）。一拍之后：

\[
\text{shift\_reg\_r}^{(t+1)} = \big(\text{shift\_reg\_r}^{(t)} \ll 24\big) + \text{din\_r}
\]

用位拼接的视角看（`\ Circ` 表示拼接），更直观：

\[
\text{shift\_reg\_r}^{(t+1)} = \{\,\text{shift\_reg\_r}^{(t)}[W-25:0],\ \text{din\_r}\,\}
\]

即「砍掉最高 24 位（最老样本），其余整体上移，最低 24 位接 `din_r`」。而读出端固定取最高 24 位：

\[
\text{dout\_r} = \text{shift\_reg\_r}[W-1:W-24]
\]

一个样本从 `din_r` 进入格0，每拍上移一格，经过 N 拍到达格(N−1)（最高位），于是被 `dout` 切出。**所以延时深度 = N 拍。**

#### 4.2.3 源码精读

先看读出窗口。`shift_16` 的 `dout` 是纯组合 `assign`，直接切最高 24 位：

[RTL/shift_16.v:31-32](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/shift_16.v#L31-L32) —— `dout` 取 `shift_reg_r[383:360]`，即 16 格队列里最顶端（最老）的那 24 位。这一行说明「输出 = 被挤出的最老样本」。

```verilog
assign dout_r = shift_reg_r[383:360];
assign dout_i = shift_reg_i[383:360];
```

再看移位推进。注意它出现在时序 `always` 块里两个分支（`if(in_valid)` 与 `else if(valid)`），但移位表达式完全一样：

[RTL/shift_16.v:44-45](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/shift_16.v#L44-L45) —— `(tmp_reg_r<<24) + din_r`：把影子值左移一格（丢老样本），新样本 `din_r` 落到最低 24 位。这一行就是 FIFO 的「入队 + 整体前移」。

```verilog
shift_reg_r <= (tmp_reg_r<<24) + din_r;
shift_reg_i <= (tmp_reg_i<<24) + din_i;
```

> **极端情况验证 `shift_1`**：当 N=1 时，`shift_reg_r` 只有 24 位。此时 `tmp_reg_r<<24` 在 24 位上下文里整体被移出变成 0，于是 `(0)+din_r = din_r`，即 `shift_reg_r <= din_r`。对应 [RTL/shift_1.v:44](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/shift_1.v#L44) —— `shift_1` 退化成「单寄存器打一拍」，`dout` 就是上一拍的 `din`，延时正好 1 拍。这反过来验证了「移位步长 24 = 一格」的通用公式在 N=1 时仍然自洽。

#### 4.2.4 代码实践

**实践目标**：手工模拟 `shift_2` 两拍，亲眼看到「样本入队 → 上移 → 从 dout 出队」的全过程。

**操作步骤**：

1. 假设 `shift_2` 复位后 `shift_reg_r = 0`；
2. 第 1 拍 `din_r = 0xA`（即十进制 10）：按公式 `(0<<24)+0xA = 0xA`，所以 `shift_reg_r` 变成 `48'h0000_0000_000A`；
3. 第 2 拍 `din_r = 0xB`：`tmp_reg_r<<24` 把 `0xA` 从格0 推到格1（高位），低位补 0，再加 `0xB`，得到 `48'h0000_000A_000B`；
4. 此刻读 `dout_r = shift_reg_r[47:24]`。

**需要观察的现象**：第 2 拍之后，`dout_r` 应当等于第 1 拍输入的 `0xA`。

**预期结果**：`dout_r = 0xA`，证明样本入队后正好经过 2 拍出现在 `dout`，延时 = 2。这一步可在仿真器里给 `shift_2` 喂一个简单激励直接验证；若无仿真器，按上面位运算手算即可得到同样结论。

#### 4.2.5 小练习与答案

**练习 1**：把 `(tmp_reg_r<<24) + din_r` 改写成等价的位拼接表达式。

**参考答案**：`shift_reg_r <= {shift_reg_r[W-25:0], din_r};`（W=24N）。加法之所以等价于拼接，是因为左移后最低 24 位是 0，与 24 位的 `din_r` 相加不会产生任何进位。

**练习 2**：`dout` 为什么必须取「最高 24 位」而不是「最低 24 位」？

**参考答案**：因为左移把样本往高位推，最老的样本爬到了最高位。取最高 24 位才是「最先入队的那个」，这才符合 FIFO「先进先出」；若取最低位，读到的将永远是「刚刚进来的新样本」，延时为 0，毫无意义。

---

### 4.3 valid 握手与计数器

#### 4.3.1 概念说明

延时线不是「永远在移」。它要回答两个问题：

1. **什么时候开始移？** —— 上游蝶形开始吐有效数据时（`in_valid` 拉高）。
2. **什么时候可以停？** —— 这是本节最微妙的地方。

本项目的设计选择是：**一旦见到第一个 `in_valid`，就把内部 `valid` 锁存为 1，此后只要 `valid` 为 1 就一直移位。** 换句话说，延时线被「点火」之后就不会主动熄火，而是一直把队列里残留的样本往前冲刷，直到 32 个样本全部流出。这非常契合 SDC 流水线「单路数据块连续流过」的工作方式：数据是一次性灌进来的，冲刷到尾即可。

> 关于 `counter_N`：你会看到每个 `shift_N` 都维护一个 `counter_N / next_counter_N` 计数器，每拍 +1。但**在本模块内部，`counter_N` 既没有出现在任何 `assign` 输出里，也没有用来门控 `dout` 或 `valid`**——延时行为完全由寄存器几何决定，与计数器无关。这个计数器在本模块里看起来是「维护了但未被消费」的。读源码时要有这个判断力：不是所有寄存器都对功能有贡献。它可能是为调试/预留/历史遗留而保留，具体用途待确认，但**不影响延时功能**。

#### 4.3.2 核心流程

时序 `always` 块的三段优先级（高有效异步复位优先级最高）：

```
posedge clk or posedge reset:
  if (reset)              → 清零所有寄存器、valid<=0
  else if (in_valid)      → 移位推进、counter+1、valid <= in_valid（=1，点火）
  else if (valid)         → 继续移位推进、counter+1、valid <= next_valid
  else                    → 保持不动（点火前空闲）
```

组合 `always` 块只算「下一拍的影子值」：

```
next_counter = counter + 1
tmp_reg      = shift_reg        // 影子，供时序块左移用
next_valid   = valid            // 关键：next_valid 恒等于 valid
```

注意 `next_valid = valid` 这一行：它让 `valid` 一旦被 `in_valid` 点亮，就通过 `valid <= next_valid` 自保持在高电平。于是延时线在 `in_valid` 撤销后仍持续移位，完成冲刷。

#### 4.3.3 源码精读

先看时序块的三段结构。这是标准的「异步复位 → 主握手分支 → 自保持分支」写法：

[RTL/shift_16.v:34-54](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/shift_16.v#L34-L54) —— 复位分支清零；`if(in_valid)` 分支点火并移位；`else if(valid)` 分支在 `in_valid` 撤销后依靠内部 `valid` 继续移位，把队列里残留样本冲刷出去。两个非复位分支里的移位表达式一模一样。

```verilog
always@(posedge clk or posedge reset)begin
    if(reset)begin
        shift_reg_r <= 0; ... valid <= 0;
    end
    else if (in_valid)begin
        counter_16  <= next_counter_16;
        shift_reg_r <= (tmp_reg_r<<24) + din_r;
        valid       <= in_valid;     // 点火
    end
    else if (valid)begin
        counter_16  <= next_counter_16;
        shift_reg_r <= (tmp_reg_r<<24) + din_r;
        valid       <= next_valid;   // 自保持
    end
end
```

再看组合块，注意 `next_valid` 的赋值：

[RTL/shift_16.v:57-62](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/shift_16.v#L57-L62) —— `next_valid = valid`，使得 `valid` 一旦为 1 就被锁存；`next_counter_16 = counter_16 + 1` 让计数器自由运行。这一段同时也暴露了 4.3.1 提到的事实：`counter_16` 在本模块内没有驱动任何输出。

```verilog
always@(*)begin
    next_counter_16 = counter_16 + 5'd1;
    tmp_reg_r = shift_reg_r;
    tmp_reg_i = shift_reg_i;
    next_valid = valid;
end
```

> **级间 `in_valid` 的来源**：在顶层里，每级 `shift_N` 的 `in_valid` 接的是**上一级蝶形的 `outvalid`**。例如 [RTL/FFT.v:143-150](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L143-L150) 里 `shift_8` 的 `.in_valid(radix_no1_outvalid)`。这就是 u4-l1 要讲的「valid 菊花链」：第 1 级蝶形一旦吐出有效数据，第 2 级延时线就被点火。本讲你只需记住 `shift_N.in_valid` 来自上游 `radix_(N-1).outvalid`。

#### 4.3.4 代码实践

**实践目标**：理解 `valid` 的「点火—自保持」语义，并验证 `counter_N` 不影响输出。

**操作步骤**：

1. 在仿真器里给 `shift_4` 加激励：先拉高 `in_valid` 喂 4 个样本，然后在第 5 拍把 `in_valid` 拉低，但继续给 `din_r` 喂新值；
2. 观察 `dout_r` 在 `in_valid` 拉低**之后**是否仍在继续变化；
3. 同时把 `counter_4` 加入波形窗口。

**需要观察的现象**：`in_valid` 拉低后，`dout_r` 仍会按拍继续吐出之前入队的样本（冲刷过程），证明内部 `valid` 仍为 1；`counter_4` 在持续 +1，但它的变化与 `dout_r` 的内容没有因果关系。

**预期结果**：`dout_r` 在 `in_valid` 撤销后继续推进若干拍，把残留样本排空；`counter_4` 一直在跑，却对 `dout` 毫无影响。若无法本地仿真，可对照 4.3.3 的源码逻辑推导得出同样结论——这一点不依赖运行环境。

#### 4.3.5 小练习与答案

**练习 1**：如果把组合块里的 `next_valid = valid;` 改成 `next_valid = 1'b0;`，延时线的行为会发生什么变化？

**参考答案**：`valid` 在 `in_valid` 撤销的下一拍就会被清零，于是 `else if(valid)` 分支不再命中，移位停止。结果是队列里还没爬到 `dout` 的样本会被「冻」在原地，永远输出不到——后续蝶形的 second half 会拿到错误（过时）的数据。

**练习 2**：本模块的 `counter_N` 既然不被输出使用，为什么综合后它依然会占面积？

**参考答案**：因为它是 `reg` 且在时序块里被赋值（`counter <= next_counter`），综合工具会为它分配触发器，即便它的输出没人用。除非加 `/* synthesis keep */` 之外的优化屏障，综合器可能在优化阶段识别它是 dead 逻辑而删掉，但 RTL 层面它确实存在。这正是「读源码要分辨有效信号与冗余信号」的意义。

---

### 4.4 延时深度的递减规律

#### 4.4.1 概念说明

把五个模块摆在一起，最显眼的规律是**延时深度 16 → 8 → 4 → 2 → 1，逐级减半**。这不是巧合，而是 radix-2 DIF 算法结构决定的。

回顾 u2-l1 的结论：在 radix-2 DIF 里，每一级把样本分成若干「组」，组的大小每级减半（32 → 16 → 8 → 4 → 2），而蝶形运算配对的是**同一组内相距「组大小/2」的两个样本**。这个「相距距离」就是延时线要提供的延时深度：

- 第 1 级：1 组 × 32 样本，配对距离 = 32/2 = **16** → 用 `shift_16`
- 第 2 级：2 组 × 16 样本，配对距离 = 16/2 = **8** → 用 `shift_8`
- 第 3 级：4 组 × 8 样本，配对距离 = 8/2 = **4** → 用 `shift_4`
- 第 4 级：8 组 × 4 样本，配对距离 = 4/2 = **2** → 用 `shift_2`
- 第 5 级：16 组 × 2 样本，配对距离 = 2/2 = **1** → 用 `shift_1`

所以「延时减半」直接对应「蝶形配对距离减半」，也对应 u3-l2 讲过的反馈回路：`radix.delay → shift_N → radix.din_a`，N 正好让「上半组的差」延时到「下半组的差」到达时与之配对。

#### 4.4.2 核心流程

延时深度 N 与硬件参数的换算关系（本族通用公式）：

\[
\text{寄存器位宽}\ W = 24 \times N
\]

\[
\text{dout 窗口} = [W-1 : W-24] = [24N-1 : 24(N-1)]
\]

\[
\text{延时拍数} = N
\]

每级的 N 又由算法决定：

\[
N_{\text{stage}_k} = \frac{\text{第 } k \text{ 级的组大小}}{2} = 2^{\,5-k},\quad k=1,\dots,5
\]

即 \(N\) 序列 = \(16, 8, 4, 2, 1\)。把这条公式套到 64 点 FFT（u7-l3 会讨论），就是 6 级、延时深度 \(32,16,8,4,2,1\)。

#### 4.4.3 源码精读

在顶层 `FFT.v` 里，每级 `shift_N` 都和同级的 `radix2` 配成一对，构成反馈回路。以第 1 级为例：

[RTL/FFT.v:95-117](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L95-L117) —— `radix_no1` 的 `din_a_r` 取自 `shift_16_dout_r`（注释写 `//fb` 即 feedback），而 `shift_16` 的 `din_r` 取自 `radix_no1_delay_r`。这就闭合了 `delay → shift_16 → din_a` 的反馈环，延时深度 16 对应第 1 级蝶形的配对距离。

```verilog
radix2 radix_no1(
    .din_a_r(shift_16_dout_r), //fb：延时线回流
    .din_b_r(din_r_wire),      //input：新输入
    ...
    .delay_r(radix_no1_delay_r),
    ...
);
shift_16 shift_16(
    .din_r(radix_no1_delay_r), //差分结果进延时线
    .dout_r(shift_16_dout_r),
    ...
);
```

把五级都看一遍，规律一目了然——每级的 `shift_N` 后缀恰好是上一行 `din_a` 注释里隐含的配对距离：

| 级 | 蝶形 | 延时线 | `din_a` 来自 | 配对距离（=N） |
|----|------|--------|--------------|----------------|
| 1 | `radix_no1` | `shift_16` | `shift_16_dout_r` | 16 |
| 2 | `radix_no2` | `shift_8`  | `shift_8_dout_r`  | 8 |
| 3 | `radix_no3` | `shift_4`  | `shift_4_dout_r`  | 4 |
| 4 | `radix_no4` | `shift_2`  | `shift_2_dout_r`  | 2 |
| 5 | `radix_no5` | `shift_1`  | `shift_1_dout_r`  | 1 |

> 验证：[RTL/FFT.v:177-184](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L177-L184)（`shift_4`）、[RTL/FFT.v:211-217](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L211-L217)（`shift_2`）的接法和第 1 级完全同构，只是后缀和位宽逐级减半。

#### 4.4.4 代码实践（本讲主任务）

**实践目标**：把五个 `shift_N` 的几何参数填进一张表，归纳出「延时点数与寄存器宽度的关系式」。

**操作步骤**：

1. 打开 `shift_16.v`、`shift_8.v`、`shift_4.v`、`shift_2.v`、`shift_1.v`；
2. 对每个文件，抄下三处：`reg [?:0] shift_reg_r;` 的位宽、`assign dout_r = shift_reg_r[?:?];` 的窗口、`counter_N` 的位宽；
3. 按下表填写（参考答案在下面，先自己填再对照）。

| 模块 | 寄存器位宽 | dout 高位窗口 | counter 位宽 | 延时点数 N |
|------|-----------|---------------|--------------|-----------|
| shift_16 | 384（[383:0]） | [383:360] | 6（[5:0]） | 16 |
| shift_8  | 192（[191:0]） | [191:168] | 5（[4:0]） | 8 |
| shift_4  | 96（[95:0]）   | [95:72]   | 4（[3:0]） | 4 |
| shift_2  | 48（[47:0]）   | [47:24]   | 3（[2:0]） | 2 |
| shift_1  | 24（[23:0]）   | [23:0]    | 2（[1:0]） | 1 |

**需要观察的现象**：寄存器位宽 = 24 × N；dout 窗口高位 = 24N−1，低位 = 24(N−1)；延时 N = 位宽/24。

**归纳关系式**：

\[
\boxed{\text{延时点数 } N = \frac{\text{寄存器位宽}}{24},\qquad \text{dout 窗口} = [24N-1 : 24(N-1)]}
\]

**预期结果**：五条数据全部满足上式，且 N 序列 = 16/8/4/2/1，与 radix-2 DIF 各级配对距离一致。

#### 4.4.5 小练习与答案

**练习 1**：为什么第 1 级用 `shift_16` 而不是 `shift_32`？明明 FFT 是 32 点。

**参考答案**：因为第 1 级蝶形配对的是「前 16 个样本」与「后 16 个样本」，它们之间的距离是 16 而不是 32。延时线只需把前半组的「差」延迟 16 拍，就能和后半组的「差」对齐。32 点指的是一次 FFT 的总样本数，不是单级延时深度。

**练习 2**：如果把设计扩展成 64 点 FFT，需要新增哪些 `shift_N`？延时序列会变成什么？

**参考答案**：64 点 radix-2 需要 6 级，延时序列变为 32/16/8/4/2/1。需要新增一个 `shift_32`（768bit 寄存器，dout 取 `[767:744]`），原有 5 个继续沿用；顶层要再多例化一级 `radix2 + shift_N + ROM_N`。这是 u7-l3「设计扩展」要展开的内容。

---

## 5. 综合实践

**任务**：用一张图把「延时线为什么能让蝶形的两个半组对齐」讲清楚。

请按下面三步完成：

1. **画延时线时序图**。以 `shift_4` 为例，横轴是时钟拍数 0~8，画出 `din_r`（依次输入样本 \(s_0, s_1, s_2, s_3, \dots\)）和 `dout_r` 的波形。在图上标出 \(s_0\) 从 `din` 进入、到第 4 拍出现在 `dout` 的过程，确认延时 = 4。

2. **画反馈回路框图**。照着 [RTL/FFT.v:95-117](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L95-L117) 画出第 1 级的回路：`radix_no1` 的 `delay_r → shift_16.din_r → shift_16.dout_r → radix_no1.din_a_r`，并在回路旁标注「延时 16 拍」。结合 u3-l2 的 first half 逻辑，说明为什么这个延时恰好让「上半组的差」和「下半组的差」在 second half 相遇。

3. **写一段结论（150 字以内）**：用自己的话回答——「如果把第 1 级的 `shift_16` 换成 `shift_8`，FFT 结果会怎样？为什么？」（提示：配对错位，second half 会拿错误的样本相乘。）

**验收标准**：时序图能清楚显示「延时 N 拍」；框图能正确闭合反馈回路；结论里能点出「延时深度必须等于蝶形配对距离，否则样本错位」这一核心因果。

> 若本地有仿真器，可把 `shift_4` 单独例化成一个迷你 testbench（给 `clk`、`reset`、`in_valid` 和 4 个 `din` 值），用波形直接验证第 1 步的时序图；若没有，按 4.2.2 的位运算公式手算也能得到一致结论，不依赖运行环境。

## 6. 本讲小结

- `shift_N` 用**一个 N×24bit 的超宽寄存器**实现 N 个样本的 FIFO 延时，没有读写指针，时序简洁但面积大。
- 核心动效只有一行：`(tmp_reg_r<<24) + din_r`——左移丢老样本、低位拼新样本；`dout` 用固定窗口切最高 24 位读出最老样本。
- `in_valid` 点火、内部 `valid` 自保持，使延时线在数据流过后继续冲刷残留样本；`counter_N` 在本模块内**未被任何输出消费**，是读源码时要能识别出的冗余信号。
- 延时深度满足 \(N = \text{位宽}/24\)，序列 16/8/4/2/1 逐级减半，恰好等于 radix-2 DIF 每级蝶形的**配对距离 = 组大小/2**。
- 顶层把每级 `shift_N` 与同级 `radix2` 接成 `delay → shift → din_a` 反馈回路，正是 SDC 架构把「时间对齐」交给延时线完成的体现。

## 7. 下一步学习建议

- **承接 u4-l1（五级流水线数据流串讲）**：本讲只看了「单级反馈回路」，下一步应把五级 `radix2 + shift + ROM` 串成完整数据流，看一个样本从 `din` 走到第 5 级 `out` 的全过程，以及 `valid` 在级间的菊花链传递。
- **回顾 u3-l2（radix2 蝶形）**：如果你对「为什么要延时」还不够踏实，回头重读 first half 的「和→op、差→delay」分流，再回来看本讲的反馈回路，因果会非常清楚。
- **预告 u3-l4（ROM 与状态控制）**：`shift_N` 只负责「延时对齐」，而蝶形三态机什么时候做 first half、什么时候做 second half，是由 `ROM_N` 里的计数器产生 `state` 信号驱动的——那是下一讲的主题。
- **进阶 u7-l3（设计扩展）**：本讲练习里已经触到「64 点需要 `shift_32`」的扩展思路，u7-l3 会系统讨论参数化点数/位宽时的改造清单。
