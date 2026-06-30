# 跨时钟域单周期脉冲：cdc_strobe

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚为什么一个**单周期脉冲**（strobe）不能直接套用上一讲的 `cdc_data` 两级同步器去跨时钟域——它会“漏采”。
- 读懂 [cdc_strobe.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe.sv) 里的 **2 位格雷计数器**：为什么“每次只变 1 bit”是它能安全跨域的根本原因，并能手算出 `00→01→11→10→00` 的循环。
- 解释 clk2 域用 **两级移位 + 比较** 如何把“计数器值变了”重新还原成 clk2 域里的一个单拍脉冲。
- 说出 cdc_strobe 的两条**速率限制**：clk1 侧“最多隔一拍一次”、以及 clk2 远慢于 clk1 时会“重叠/丢失”，并理解“约 2 个 clk2 周期传播延迟”的来源。
- 写出对应的 `_FP_ATTR` false_path 约束，并解释它为什么用 `-from`（而上一讲 `_SYNC_ATTR` 用 `-to`）。

## 2. 前置知识

本讲紧接 **u3-l1（cdc_data：两级数据同步器）**，请确保你已经掌握：

- **亚稳态**与**两级同步器**：跨域直接采样会亚稳态，靠“打两拍”把衰减时间撑满一个周期来兜底。
- `cdc_data` 处理的是**逐位稳定的多 bit 数据**；它**不能**用来同步单拍脉冲——这是本讲的出发点。
- `_SYNC_ATTR` 命名约定：给同步器实例名加后缀，一条 `set_false_path` 用 `-to` 指向第一级寄存器即可管住全部同步器。

此外你需要两个上一讲已建立、本讲会反复用到的直觉：

1. 触发器只在时钟上升沿采样。如果被采信号的高电平只维持**一个源时钟周期**，而目的时钟又比源时钟慢，那么这个高电平很可能整个落在目的时钟的两次采样之间——**从来没被采到**，事件就丢了。
2. 把“瞬时事件”变成“**持久的状态变化**”，目的时钟迟早会采到它。cdc_strobe 的全部技巧就是这一句话的电路化。

> 小提示：README 把 [cdc_strobe.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe.sv) 标为标准难度的“clock crossing synchronizer for one-cycle strobes”。它和上一讲的 `cdc_data` 是一对：一个搬**数据**，一个搬**事件**。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲怎么看 |
|------|------|-----------|
| [cdc_strobe.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe.sv) | 跨时钟域单拍脉冲同步器 | 本讲主角。重点看 4 段：边沿检测、格雷计数器 `gc_FP_ATTR`、clk2 两级缓冲 `gc_b`、变化比较 `strb2` |
| [cdc_strobe_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe_tb.sv) | 配套 testbench | 它正好构造了“clk1 快、clk2 慢且带抖动”的场景，并用两个计数器 `strb1_cntr`/`strb2_cntr` 量化“丢没丢事件”——综合实践的直接依据 |
| [cdc_data.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_data.sv) | 上一讲的数据同步器 | 做对照：同样跨域，机制与命名后缀都不同 |

---

## 4. 核心概念与源码讲解

本讲对应四个最小模块：**格雷计数器**、**跨域变化检测**、**速率限制**、**`_FP_ATTR` 约束约定**。它们是一条因果链：脉冲进来 → 格雷计数器把它变成“翻转 1 bit”（4.1）→ clk2 域两级缓冲后比较出“值变了”，重新生成一个脉冲（4.2）→ 这套机制对速率有要求（4.3）→ 跨域那条采样路径要写 false_path 豁免（4.4）。

先看一眼 `cdc_strobe` 的端口，建立整体印象。[cdc_strobe.sv:L53-L63](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe.sv#L53-L63)：

```systemverilog
module cdc_strobe (
  input arst,         // async reset
  input clk1,         // clock domain 1 clock
  input nrst1,        // clock domain 1 reset (inversed)
  input strb1,        // clock domain 1 strobe
  input clk2,         // clock domain 2 clock
  input nrst2,        // clock domain 2 reset (inversed)
  output strb2        // clock domain 2 strobe
);
```

一个异步复位 `arst`，两套“时钟 + 低有效复位 + 脉冲”三元组：`clk1/nrst1/strb1` 是源域，`clk2/nrst2/strb2` 是目的域。输入 `strb1` 是 clk1 域的单拍（或更宽）脉冲，输出 `strb2` 是 clk2 域还原出的单拍脉冲。整体数据流是：

```text
strb1 ─▶ [边沿检测 strb1_ed] ─▶ [2位格雷计数器 gc_FP_ATTR]   ← clk1 域
                                        │ (跨域，异步)
                                        ▼
            [两级缓冲 gc_b[1],gc_b[0]] ─▶ [比较: gc_b[1]!=gc_b[0]] ─▶ strb2   ← clk2 域
```

下面逐段拆开。

---

### 4.1 格雷计数器

#### 4.1.1 概念说明

跨时钟域搬运一个**多 bit 的值**时，最怕的是“多位同时翻转”。比如普通二进制从 `011` 加到 `100`，三位同时变；如果 clk2 正好在它们翻转的瞬间采样，由于各 bit 的翻转/落定时间不同，可能采到 `000`、`010`、`111` 等**中间垃圾值**——既不是旧的 `011` 也不是新的 `100`。

**格雷码（Gray code）**就是为了消除这个隐患：它编排编码顺序，使**相邻两个值之间只有 1 bit 不同**。于是无论 clk2 在翻转瞬间何时采样，某个 bit 要么还是旧值、要么已是新值，绝不可能采到“第三个垃圾态”——最坏只是晚一拍看到新值。这就是 cdc_strobe 敢于直接把计数器值送到另一个时钟域的底气。

cdc_strobe 用的是一个 **2 位格雷计数器**：它只有 4 个状态，每收到一个脉冲就走一格。2 位足够，因为我们不关心“计数到几”，只关心“**值有没有变**”。

#### 4.1.2 核心流程

2 位反射格雷码的循环是 `00 → 01 → 11 → 10 → 00`。本模块用一句极简的位运算实现“走一格”：

\[
gc_{\text{next}} \;=\; \{\, gc[0],\ \overline{gc[1]} \,\}
\]

即“新高位 = 旧低位，新低位 = 旧高位取反”。逐拍代入验证：

| 步数 | gc[1] gc[0] | 与上一步不同的位 |
|------|-------------|------------------|
| 0 | `00` | — |
| 1 | `01` | bit0 翻转 |
| 2 | `11` | bit1 翻转 |
| 3 | `10` | bit0 翻转 |
| 4（=0） | `00` | bit1 翻转 |

每一步都**恰好翻转 1 bit**，正是格雷码的关键性质。每来一个脉冲事件，计数器就走一格，于是“发生了几个事件”被编码成“走了几格”——而“走了一格”在跨域传输中等价于“有且仅有 1 bit 翻转”，对亚稳态天然鲁棒。

#### 4.1.3 源码精读

在让计数器走格之前，模块先对 `strb1` 做了**边沿检测**，保证“哪怕 `strb1` 高电平维持了好几拍，也只算一次事件”。[cdc_strobe.sv:L65-L80](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe.sv#L65-L80)：

```systemverilog
// buffering strb1
logic strb1_b = 1'b0;
always @(posedge clk1 or posedge arst) begin
  if( arst )            strb1_b <= '0;
  else if( ~nrst1 )     strb1_b <= '0;   // Quartus demands to split these if conditions
  else                  strb1_b <= strb1;
end

// strb1 edge detector
// prevents secondary strobe generation in case strb1 is not one-cycle-high
logic strb1_ed;
assign strb1_ed = ( ~strb1_b && strb1 );
```

`strb1_b` 是 `strb1` 打一拍；`strb1_ed = ~strb1_b && strb1` 只在 `strb1` 的**上升沿那一拍**为真。注释点明了它的作用：即便 `strb1` 宽到跨好几个 clk1 周期，也只在第一个上升沿产生一个单拍事件，杜绝“一个长脉冲被当成多次事件”。

随后就是这个最小模块的主角——格雷计数器。[cdc_strobe.sv:L82-L92](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe.sv#L82-L92)：

```systemverilog
// 2 bit gray counter, it must NEVER be reset
logic [1:0] gc_FP_ATTR = '0;
always @(posedge clk1 or posedge arst) begin
  if( arst ) begin
    // nop
  end else begin
    if( strb1_ed ) begin
      gc_FP_ATTR[1:0] <= {gc_FP_ATTR[0],~gc_FP_ATTR[1]}; // incrementing counter
    end
  end
end
```

两点必须看懂：

- **`gc_FP_ATTR` 这个名字不是随便取的**：后缀 `_FP_ATTR`（false path attribute）是 4.4 节 false_path 约束的“钩子”，和上一讲 `_SYNC_ATTR` 是同一套命名约定思想。
- **注释强调“it must NEVER be reset”**，代码里 `arst` 分支真的是 `// nop`（什么都不做）。原因是：如果在 clk1 域把它清零，而 clk2 域的缓冲里还停着旧值，clk2 之后采到 0 就会以为“值变了”，凭空产生一个假脉冲。所以这个计数器的值**只随真实脉冲单调循环，永不归零**。复位安全由下一节的 `gc_b` 自己负责。

#### 4.1.4 代码实践

**目标**：在纸上把格雷计数器“走格”的过程跑一遍，确认它确实是格雷码。

1. 假设 `strb1_ed` 连续 5 拍为高（模拟 5 个事件）。
2. 从 `gc_FP_ATTR = 00` 开始，逐拍套用 `gc_next = {gc[0], ~gc[1]}` 写出每拍的新值。
3. **预期结果**：`00 → 01 → 11 → 10 → 00 → 01`（第 5 拍回到 `00`，第 6 拍又变 `01`），且每步只变 1 bit。
4. **进阶（待本地验证）**：在 [cdc_strobe_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe_tb.sv) 里临时加一句 `$display("gc=%b", M.gc_FP_ATTR);`（在支持层次访问的仿真器中），观察这个内部寄存器是否按上表循环。

#### 4.1.5 小练习与答案

**Q1**：为什么不用普通二进制计数器，而要用格雷码？

> **答**：普通二进制进位时多位同时翻转（如 `011→100`），跨域采样会采到中间垃圾值。格雷码相邻状态只差 1 bit，跨域采样最坏只是晚一拍看到新值，不会出现非法中间态——这正是它能裸跨时钟域的根本原因。

**Q2**：把计数器从 2 位扩成 4 位（16 个状态）会更有优势吗？

> **答**：对本模块的需求（只检测“变没变”）没有意义，2 位已足够。多位格雷计数器的价值在于“跨域传递**事件个数**”（如异步 FIFO 用格雷码传读写指针），那里需要区分很多状态；cdc_strobe 只关心“有没有变化”，2 位刚好。

---

### 4.2 跨域变化检测

#### 4.2.1 概念说明

上一节把脉冲变成了“格雷计数器走了 1 格”，但计数器在 clk1 域。现在要在 clk2 域**重新生成一个脉冲**。

这里要先回答本讲标题里那个最关键的问题：**为什么单周期脉冲不能直接用 `cdc_data` 那种两级同步器搬？**

> 因为两级同步器是“逐 clk2 周期采样电平”的。一个只维持 1 个 clk1 周期的窄脉冲，很可能整个落在 clk2 的两次上升沿之间，**电平从来没被采到**，事件永久丢失。

cdc_strobe 的解法是上一节埋下的伏笔：脉冲已被转换成计数器值的**持久翻转**——这个新值会一直保持到下一个脉冲到来。于是 clk2 “迟早”会采到它，绝不会漏。然后在 clk2 域做一件简单的事：**比较相邻两拍的计数器值，不等就说明“刚刚发生了一次事件”，输出一个脉冲。**

这就是经典的“**电平敏感 + 边沿还原**”跨域手法：把瞬时事件硬化成持久的电平变化（对亚稳态鲁棒），再在目的域用边沿检测把变化还原回瞬时脉冲。

#### 4.2.2 核心流程

clk2 域用两级寄存器 `gc_b[0]`、`gc_b[1]` 把 clk1 的 `gc_FP_ATTR` 接过来：

```text
gc_FP_ATTR (clk1域) ──▶ [gc_b[0]] ──▶ [gc_b[1]]
                         第1级          第2级
                      (跨域采样点,      (落定后再用)
                       可能亚稳态)

strb2 = ( gc_b[1] != gc_b[0] )    ← 比较前后两拍，不等即"刚变了"
```

逐 clk2 拍追踪一次“计数器从 A 变到 B”：

- 变化前：`gc_b[0]=gc_b[1]=A`，`strb2 = (A!=A) = 0`。
- 下一个 clk2 沿：`gc_b[0]` 采到新值 B，`gc_b[1]` 采到上一拍的 `gc_b[0]`=A。于是 `gc_b[0]=B, gc_b[1]=A`，`strb2 = (A!=B) = 1` —— **脉冲出现，宽 1 个 clk2 周期**。
- 再下一个 clk2 沿：`gc_b[0]` 仍为 B，`gc_b[1]` 跟上变成 B。`gc_b[0]=B, gc_b[1]=B`，`strb2 = (B!=B) = 0` —— 脉冲收回。

所以每次计数器“走一格”，clk2 域就稳定地吐出**一个单拍脉冲**。这同时也回答了头注释里“约 2 个 clk2 周期传播延迟”的来源——`gc_b` 这条两级移位链正是上一讲见过的“两级同步器深度”，它决定了变化要在 clk2 域走两级才能被比较出来。

> 关于“格雷计数器不需要同步器”那行注释：它的意思不是“没有同步触发器”，而是“**不需要 `cdc_data` 那种多 bit 同步逻辑**”。`gc_b[0]`/`gc_b[1]` 这两级触发器**物理上就是**同步器——只不过因为有格雷码“每次只变 1 bit”打底，对每个 bit 独立做两级同步就是安全的，无需额外握手。

#### 4.2.3 源码精读

clk2 域的两级缓冲。[cdc_strobe.sv:L94-L105](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe.sv#L94-L105)：

```systemverilog
// buffering counter value on clk2
// gray counter doesnt need a synchronizer
logic [1:0][1:0] gc_b = '0;
always @(posedge clk2 or posedge arst) begin
  if( arst ) begin
    gc_b[1:0] <= {2{gc_FP_ATTR[1:0]}};         // 把当前计数器值复制 2 份
  end else if( ~nrst2 ) begin
    gc_b[1:0] <= {2{gc_FP_ATTR[1:0]}};
  end else begin
    gc_b[1:0] <= {gc_b[0],gc_FP_ATTR[1:0]};    // shifting left
  end
end
```

逐句拆解：

- `logic [1:0][1:0] gc_b` 是一个 2 元素的 packed 数组，每个元素 2 bit，即 `gc_b[1]` 和 `gc_b[0]`，各装一份 2 位格雷值。
- 正常工作时 `gc_b <= {gc_b[0], gc_FP_ATTR}` 是“左移”：新 `gc_b[1]` = 旧 `gc_b[0]`，新 `gc_b[0]` = 当前的 `gc_FP_ATTR`。所以 **`gc_b[0]` 是第一级**（直接采跨域的 `gc_FP_ATTR`，是亚稳态发生地），**`gc_b[1]` 是第二级**（落定后再用）。这和上一讲 `data[1]/data[2]` 的角色完全对应。
- 复位分支故意把两级都置成 `{2{gc_FP_ATTR}}`（即把当前的计数器值复制两份给 `gc_b[0]` 和 `gc_b[1]`），让两级**相等**，于是 `strb2 = (相等) = 0`，避免复位瞬间产生假脉冲。这正弥补了 `gc_FP_ATTR` 自身“永不复位”留下的空档——配合得天衣无缝。

最后是比较器，一行就还原出脉冲。[cdc_strobe.sv:L107-L108](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe.sv#L107-L108)：

```systemverilog
// gray_bit_b edge detector
assign strb2 = ( gc_b[1][1:0] != gc_b[0][1:0] );
```

`strb2` 是 `gc_b` 两级的组合比较输出。由于 `gc_b[0]`/`gc_b[1]` 都是寄存器，稳态下比较结果是一个干净的、宽 1 个 clk2 周期的脉冲。**注意**：使用方应当像 testbench 那样在 clk2 域再采一拍（把它当时钟使能 / 写请求），不要直接拿组合输出驱动异步逻辑。

#### 4.2.4 代码实践

**目标**：在现有 testbench 里直接看清“一次 strb1 → 一个 strb2 单拍脉冲”。

1. 打开 [cdc_strobe_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe_tb.sv)，确认 DUT 例化：[cdc_strobe_tb.sv:L116-L127](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe_tb.sv#L116-L127) 把 `clk1=clk200`、`clk2=clk33`、`strb1` 接到模块 `M`，输出 `strb2`。
2. 用 iverilog 或 ModelSim 编译运行（编译方法见 u1-l3），在波形里同时显示 `strb1`（clk200 域）和 `strb2`（clk33 域）。
3. **观察现象**：每出现一个 `strb1` 脉冲，clk33 域里延迟约 2 个 clk33 周期后出现一个**宽度恰为 1 个 clk33 周期**的 `strb2` 脉冲。
4. **预期结果**：`strb2` 与 `strb1` 一一对应（在速率安全的前提下），且 `strb2` 宽度恒为 1 拍。**待本地验证**（取决于仿真器与波形缩放）。

#### 4.2.5 小练习与答案

**Q1**：如果把 `strb2` 的比较改写成 `gc_b[1] == gc_b[0]`（取相等），行为会怎样？

> **答**：含义反了：平时（相等）输出持续高，只有“刚变化”那一拍为低。这不再是脉冲而是一个反相的“静默标志”，下游无法直接当事件用。原始写法 `!=` 让“平时为 0、事件时为 1 拍”才是标准的脉冲语义。

**Q2**：为什么 `gc_b` 要做成两级，一级（直接采 `gc_FP_ATTR` 然后比较）不行吗？

> **答**：一级会把 `gc_FP_ATTR` 跨域采样时的**亚稳态**直接暴露给比较器，可能产生毛刺甚至错误脉冲。两级里，第一级 `gc_b[0]` 承受亚稳态、给足时间落定，第二级 `gc_b[1]` 再用已落定的值参与比较，输出才干净。这正是上一讲“两级同步器”的同一道理。

---

### 4.3 速率限制

#### 4.3.1 概念说明

cdc_strobe 不是万能的——它对脉冲速率有两道“天花板”，头注释里写得明明白白。[cdc_strobe.sv:L6-L22](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe.sv#L6-L22)：

- **clk1 侧（源域）硬限制：最大输入脉冲速率是“每隔一个 clk1 周期一次”**（every second clk1 clock cycle）。
- **clk2 侧（目的域）相对限制：当 clk2 明显慢于 clk1 时，输出脉冲可能“拉宽好几拍”、甚至“重叠/丢失”**，此时必须主动降低输入事件速率。

理解这两条，才能知道什么时候 cdc_strobe 会被“喂撑”。

#### 4.3.2 核心流程

**第一道天花板来自 4.1 的边沿检测。** 要产生一个 `strb1_ed`，`strb1` 必须先为低、再翻高。所以两个连续事件之间，`strb1` 至少要经历“高 1 拍 → 低 ≥1 拍 → 再高”，最少 2 个 clk1 周期。因此：

\[
f_{\text{strb1,max}} \;=\; \frac{1}{2\,T_{\text{clk1}}} \;=\; \frac{f_{\text{clk1}}}{2}
\]

即“最多隔一拍一次”。这是模块的**固有上限**，与 clk2 无关。

**第二道天花板来自 clk2 能不能跟得上。** 4.2 里每发生一次事件，计数器走 1 格，clk2 要采到这次“变化”才能吐一个 `strb2`。若两次事件挤在 clk2 的两次采样之间发生，由于 2 位格雷码每格只变 1 bit，两次走格可能让 clk2 看到的“净变化”仍只是 1 bit（甚至绕回原值）——于是两次事件只被还原成一次，甚至一次都还原不出，这就是注释说的“overlap or miss”。极端地，2 位格雷码周期为 4，若 4 个事件挤在两次 clk2 采样之间，计数器绕回原值，clk2 完全察觉不到，**全部丢失**。

所以经验法则是：**事件速率必须明显低于 clk2 的采样速率**，尤其当 clk2 ≪ clk1 时要主动限速。传播延迟方面，头注释给出“约 2 个 clk2 周期”——这就是 4.2 里 `gc_b` 两级同步链的深度。

#### 4.3.3 源码精读

第一道天花板的来源就在边沿检测那两行。[cdc_strobe.sv:L77-L80](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe.sv#L77-L80)：

```systemverilog
logic strb1_ed;
assign strb1_ed = ( ~strb1_b && strb1 );
```

`strb1_b` 是 `strb1` 的上一拍。要让 `strb1_ed` 再次为真，`strb1_b` 必须先回到 0，也就是 `strb1` 必须先出现过一个低拍——这就是“最多隔一拍一次”的硬件根源。

第二道天花板与“clk2 慢”的警示，连同传播延迟，都写在 INFO 里。[cdc_strobe.sv:L14-L22](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe.sv#L14-L22)：

```systemverilog
// -  When clk2 is essentially less than clk1 it is possible that strb2 will
//    remain HIGH for several consecutive clk2 cycles. On the output every
//    HIGH cycle should be considered as a separate strobe event
//
// -  When clk2 is essentially less than clk1 - output strobes could even
//    "overlap" or miss. In this case, please restrict input strobe event rate
//
// -  cdc_strobe module features a 2 clock cycles propagation delay
```

注意第一条里一个反直觉的现象：当 clk2 远慢于 clk1 时，`strb2` 可能**连续高好几拍**。原因是 clk1 侧事件密集、计数器在 clk2 一个周期内连走了好几格，但 clk2 两级缓冲追上后，`gc_b[1]!=gc_b[0]` 会在多个连续 clk2 周期里都为真（每拍都在“追赶”一个不同的历史值）。注释特意提醒：此时**每一拍 HIGH 都要当成一个独立事件**来用，下游必须按拍计数，而不是只看“有没有跳变沿”。

testbench 恰好把这套场景搭了出来：clk1 是快的 `clk200`，clk2 是慢且带抖动的 `clk33`。[cdc_strobe_tb.sv:L14-L33](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe_tb.sv#L14-L33)：

```systemverilog
logic clk200;                  // 快域：#2.5 半周期 ⇒ 200 MHz
initial begin #0 clk200 = 1'b0;  forever #2.5 clk200 = ~clk200; end

logic clk33a;                  // 外部“异步”时钟：#7 半周期 ⇒ 周期 14ns
initial begin #0 clk33a = 1'b0;  forever #7 clk33a = ~clk33a; end

logic clk33;
always @(*) begin              // 再叠 0~2ns 随机抖动，模拟真实异步源
  clk33 = #($urandom_range(0, 2000)*1ps) clk33a;
end
```

而“事件源”是用随机数生成的电平再过一次 `edge_detect` 得到的单拍 `strb1`，[cdc_strobe_tb.sv:L98-L114](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe_tb.sv#L98-L114)。最关键的是它用**两个计数器**直接量化“丢没丢”：[cdc_strobe_tb.sv:L129-L141](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe_tb.sv#L129-L141)：

```systemverilog
logic [7:0] strb1_cntr = '0;
always_ff @(posedge clk200) begin            // 在快域数 strb1 发了几次
  if( strb1 ) strb1_cntr[7:0] <= strb1_cntr[7:0] + 1'b1;
end

logic [7:0] strb2_cntr = '0;
always_ff @(posedge clk33) begin             // 在慢域数 strb2 收到几次
  if( strb2 ) strb2_cntr[7:0] <= strb2_cntr[7:0] + 1'b1;
end
```

`strb1_cntr` 是“发出的事件数”，`strb2_cntr` 是“收到的事件数”。两者相等 ⇒ 没丢；`strb2_cntr < strb1_cntr` ⇒ 丢了。这条对照线就是综合实践的判定依据。

#### 4.3.4 代码实践（本讲核心实践）

**目标**：在“clk1 快、clk2 慢”的现有 testbench 里，验证“输入脉冲过密时输出会丢失”，并确定一个安全的最大速率。

1. **先跑随机版**：直接编译运行 [cdc_strobe_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe_tb.sv)，跑足够长时间后在波形里比较 `strb1_cntr` 与 `strb2_cntr`（或在 `initial` 里加 `$display` 周期性打印二者）。
   - **观察现象**：由于随机事件偶尔密集 + clk2 较慢且带抖动，大概率会出现 `strb2_cntr < strb1_cntr` 的时刻——这就是“丢失”。
2. **改成确定性扫频**：把随机事件源 [cdc_strobe_tb.sv:L98-L105](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe_tb.sv#L98-L105) 替换成一个“每 `K` 个 clk200 周期发一个单拍脉冲”的发生器（示例代码，需自行接入）：
   ```systemverilog
   // 示例代码：每 K 拍产生一个单拍 strb1
   localparam int K = 2;                 // 先试最快（隔一拍一次）
   logic [15:0] cnt = '0;
   always_ff @(posedge clk200) begin
     if (cnt == K-1) cnt <= '0; else cnt <= cnt + 1'b1;
   end
   assign strb1 = (cnt == K-1);
   ```
   从 `K=2` 开始逐步增大到 `3, 4, 6, 8 …`，每个 `K` 跑一段固定时长，记录稳态下 `strb1_cntr` 与 `strb2_cntr` 的差值。
3. **需要观察的现象**：
   - `K=2`（clk1 侧理论最快）时，因 clk2 明显慢于 clk1，多半仍会丢——`strb2_cntr` 增长慢于 `strb1_cntr`。
   - 随着 `K` 增大（事件越来越稀疏），`strb2_cntr` 会逐渐追上 `strb1_cntr`。
4. **预期结果 / 安全速率判定**：把“开始稳定相等”的最小 `K` 记为安全阈值。分析上它应满足“事件周期 \(K\cdot T_{\text{clk1}}\) 明显大于 clk2 周期 \(T_{\text{clk2}}\)”。本例 `clk200`=5ns、`clk33a`=14ns，可据此估算并对照实测。**待本地验证**：具体安全 `K` 取决于你本地仿真时长与抖动种子，但“`K` 越大越不丢”的趋势一定成立。

> 说明：这是“源码阅读 + 自建最小仿真”型实践。仓库未提供扫频脚本，上面发生器是示例代码，需要你手动替换原 tb 的 `strb1s/strb1` 驱动段并重新编译。

#### 4.3.5 小练习与答案

**Q1**：clk1 侧“最多隔一拍一次”的上限，是 clk2 的频率决定的吗？

> **答**：不是。它完全由 4.1 的边沿检测 `strb1_ed = ~strb1_b && strb1` 决定——要再次检测到上升沿，`strb1` 必须先经历一个低拍，所以最快也得 2 个 clk1 周期一次。这是源域固有上限，与 clk2 无关。clk2 的快慢只决定第二道天花板。

**Q2**：clk2 ≪ clk1 时，注释说 `strb2` 可能“连续高好几拍”，为什么下游不能只看上升沿？

> **答**：因为此时 clk1 侧在一次 clk2 周期内连走了多格，clk2 两级缓冲追赶时，`gc_b[1]!=gc_b[0]` 会在连续多拍都为真，**每拍都代表一个独立的历史事件**。若只看上升沿，就只能数到 1 个，丢了其余。正确做法是按拍把每拍 HIGH 都计为一次事件（即 testbench 里 `if(strb2) cntr<=cntr+1` 的写法）。

---

### 4.4 `_FP_ATTR` 约束约定

#### 4.4.1 概念说明

和上一讲 `cdc_data` 一样，跨域那条路径（从 clk1 的 `gc_FP_ATTR` 到 clk2 的 `gc_b[0]`）是**异步**的，静态时序分析（STA）无法、也不应要求它满足建立/保持时间。不豁免它，工具就会报出一条“永远修不好”的违例噪声。

豁免的方式还是 `set_false_path`。但这次仓库选了与 `_SYNC_ATTR` **不同**的一头：上一讲是 `-to` 指向目的域第一级，这里却是 `-from` 指向源域的格雷计数器。和上一讲一样，靠一个命名后缀 `_FP_ATTR`，**一条**通配约束就能管住工程里所有同类跨域。

#### 4.4.2 核心流程

“命名即契约”在这一头的工作流：

```text
写 RTL：  把脉冲跨域的那个源寄存器命名为 xxx_FP_ATTR（cdc_strobe 内部固定叫 gc_FP_ATTR）
   │
   ▼
写约束：  一条 set_false_path -from <匹配 *_FP_ATTR*>
   │
   ▼
匹配：    通配符命中所有 _FP_ATTR 寄存器 → 它们“出发”的跨域路径全部豁免
```

最关键的对比（务必和上一讲分清）：

| 后缀 | 用在 | 豁免的路径方向 | 为什么是这个方向 |
|------|------|----------------|------------------|
| `_SYNC_ATTR`（u3-l1） | `delay`/`cdc_data` 数据同步器 | `-to` 第一级 `data[1]` | 异步段终点是目的域第一级，按“终点”豁免 |
| `_FP_ATTR`（本讲） | `cdc_strobe` 脉冲跨域 | `-from` 格雷计数器 `gc_FP_ATTR` | 异步段起点是源域计数器，按“起点”豁免 |

两者其实豁免的是**同一类东西**（一条无公共时钟基准的异步路径），只是作者选择从哪一端打通配符。方向取决于“哪一段路径是异步的、且最方便用名字锚定”。

#### 4.4.3 源码精读

模板直接写在头注释里。[cdc_strobe.sv:L26-L33](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe.sv#L26-L33)：

```systemverilog
// False_path constraint is required from all nodes with "_FP_ATTR" suffix
//
// For Quartus:
// set_false_path -from [get_registers {*_FP_ATTR*}]
//
// For Vivado:
// set_false_path -from [get_cells -hier -filter {NAME =~ *_FP_ATTR*}]
```

逐字解读：

- **Quartus**：`get_registers {*_FP_ATTR*}` 选中所有名字含 `_FP_ATTR` 的寄存器，`-from` 表示“从它们出发的路径”全部不分析。对 cdc_strobe 而言，命中的就是 `gc_FP_ATTR` 这一个寄存器，它出发的路径正是去往 clk2 域 `gc_b[0]` 的那条异步采样线。
- **Vivado**：`get_cells -hier -filter {NAME =~ *_FP_ATTR*}` 用 `-hier` 递归全层次、按名字过滤出对应 cell，同样用 `-from`。

而真正带上后缀、会被这条约束捕获的寄存器，就是 4.1 见过的那个。[cdc_strobe.sv:L83](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe.sv#L83)：

```systemverilog
logic [1:0] gc_FP_ATTR = '0;
```

名字里的 `_FP_ATTR` 让它与上一条通配约束精准对上。这就是为什么作者宁可把变量名取得这么长——它同时承载了“绝不能复位的脉冲事件计数器”和“请豁免我出发的跨域路径”两层意图。

> 与上一讲呼应：`_SYNC_ATTR` 把意图编码进**实例名**（`delay` 的 instance），用 `-to` 瞄准第一级；`_FP_ATTR` 把意图编码进**寄存器名**（`gc_FP_ATTR`），用 `-from` 瞄准源端。两套约定、两条模板，各管一类跨域电路。

#### 4.4.4 代码实践

**目标**：亲手写出 cdc_strobe 需要的 false_path，并说清它为什么用 `-from`。

1. 阅读上面的 [cdc_strobe.sv:L26-L33](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe.sv#L26-L33) 模板。
2. 在一份 Vivado `.xdc` 里写下：
   ```tcl
   set_false_path -from [get_cells -hier -filter {NAME =~ *_FP_ATTR*}]
   ```
   在一份 Quartus `.sdc` 里写下：
   ```tcl
   set_false_path -from [get_registers {*_FP_ATTR*}]
   ```
3. **观察现象**（待本地验证，需有含 `cdc_strobe` 的工程综合）：综合后，原本以 `gc_FP_ATTR` 为起点的跨域时序违例会从报告中消失——路径仍在网表里，只是被排除分析。
4. **解释为什么用 `-from`**：cdc_strobe 的异步段是“clk1 的 `gc_FP_ATTR` 被 clk2 的 `gc_b[0]` 采样”，这条线**从 `gc_FP_ATTR` 出发**、跨到另一个时钟域。用 `-from` 锚定这个源寄存器，正好精确豁免这一条跨域采样线，而不会误伤 clk2 域内部 `gc_b[0]→gc_b[1]` 那段**同域、本应收敛**的正常路径。

#### 4.4.5 小练习与答案

**Q1**：如果误把方向写成 `-to [get_cells {*_FP_ATTR*}]`，会发生什么？

> **答**：会豁免**错方向**。`-to *_FP_ATTR*` 指向的是“**进入** `gc_FP_ATTR` 的路径”，而那正是 clk1 域内部 `strb1_ed` 驱动计数器的正常同域路径——本该被时序检查。真正需要豁免的“从 `gc_FP_ATTR` 出发去往 clk2”的异步段反而没被排除，违例依旧。所以方向必须对：这里是 `-from`。

**Q2**：工程里还有一个 `gc_FP_ATTR` 之外、名字也含 `_FP_ATTR` 的寄存器（另一个 cdc_strobe 实例的计数器），这条约束会一并覆盖它吗？

> **答**：会，而且这正是想要的。通配符 `*_FP_ATTR*` 命中工程内**所有**带该后缀的寄存器，所以多个 `cdc_strobe` 实例的跨域路径被一条约束全部豁免——这就是命名约定“新增零维护”的价值。

---

## 5. 综合实践

把四个最小模块串起来，完成规格要求的核心任务：**在两个不同频率时钟下驱动 cdc_strobe，验证输入脉冲过密时输出会丢失，并确定安全最大速率；同时写出对应的 `_FP_ATTR` 约束。**

**操作步骤**：

1. **复用现成场景**：[cdc_strobe_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe_tb.sv) 已经搭好“clk1=clk200 快、clk2=clk33 慢且带抖动”的环境，并且用 `strb1_cntr` / `strb2_cntr`（[L129-L141](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe_tb.sv#L129-L141)）把“发出数”和“接收数”量化好了——直接用。
2. **观察随机场景下的丢失**：先原样编译运行（方法见 u1-l3），在波形里比较两个计数器。在抖动 + 随机密集事件下，应能看到 `strb2_cntr` 偶尔落后于 `strb1_cntr`，直观印证 4.3 的第二道天花板。
3. **扫频定安全速率**：按 4.3.4 的示例代码，把随机事件源换成“每 `K` 个 clk200 周期一次”的确定性脉冲，`K` 从 2 扫到 8。记录每个 `K` 下 `strb1_cntr − strb2_cntr` 的稳态差值，找到“开始稳定不丢”的最小 `K`，即为该 clk1/clk2 配置下的安全最大速率。
4. **写约束**：在你的工程约束文件里加上 4.4 的 `_FP_ATTR` false_path（Vivado 或 Quartus 版）。
5. **端到端理解检查**：对照波形回答——`strb2` 的宽度是不是恒为 1 个 clk33 周期？当 `K=2`（clk1 侧理论最快）时为什么仍可能丢？（提示：clk2 侧追不上）当事件极密时 `strb2` 会不会出现连续多拍 HIGH？（对应 4.3 的“每拍都算一个事件”）

**需要观察与解释**：

- `strb2_cntr == strb1_cntr` ⇒ 安全；`strb2_cntr < strb1_cntr` ⇒ 丢失。安全最大速率本质上要求“事件周期 \(K\cdot T_{\text{clk1}}\) 明显大于 clk2 周期 \(T_{\text{clk2}}\)”，即事件要比 clk2 的采样**更稀疏**。
- 即便丢失，`gc_FP_ATTR` 也“永不复位”（4.1）、跨域值因格雷码而永不出现垃圾态（4.1/4.2）——丢的是“事件分辨率”，不是“数据正确性”。理解这一区别，是用好 cdc_strobe 的关键。
- **待本地验证**：具体安全 `K` 与丢失比例取决于本地仿真时长、抖动种子与工具，但“事件越稀疏越不丢、`strb2` 恒为单拍”这两个定性结论一定成立。

## 6. 本讲小结

- 单拍脉冲**不能**直接用 `cdc_data` 两级同步器跨域——窄脉冲可能整个落在目的时钟两次采样之间而被永久漏采。
- cdc_strobe 的核心是 **2 位格雷计数器**：每来一个脉冲走一格，每格只翻转 1 bit，因此跨域采样最坏只是晚一拍，绝不会采到非法中间态。
- clk1 侧先做**边沿检测**（`strb1_ed`），保证宽脉冲只算一次事件；这也决定了源域硬上限——**最多隔一拍一次**。
- clk2 侧用**两级缓冲 `gc_b` + 比较** `gc_b[1]!=gc_b[0]` 把“计数器值变了”还原成一个单拍 `strb2`；`gc_b[0]` 是第一级（亚稳态发生地），`gc_b[1]` 是第二级，传播延迟约 2 个 clk2 周期。
- 速率有两道天花板：clk1 侧“最多隔一拍一次”、clk2 侧“事件须比 clk2 采样更稀疏”；clk2 ≪ clk1 时 `strb2` 可能连续多拍 HIGH，**每拍都要当独立事件**。
- 跨域路径用 `_FP_ATTR` 命名约定 + **一条** `set_false_path -from *_FP_ATTR*` 豁免；方向是 `-from`（源端计数器），与 `_SYNC_ATTR` 的 `-to`（目的端第一级）相反，二者各管一类跨域电路。

## 7. 下一步学习建议

- **u3-l3（复位与 SR 触发器）**：本单元的下一讲，离开 CDC 回到时序基础，讲 `set_reset`/`reset_set` 家族与 `soft_latch`，帮你补齐“控制信号如何被可靠置位/复位”这块拼图。
- **u4（存储器与 FIFO）**：本讲的格雷计数器是**异步 FIFO** 跨域指针的标准手法。学完 FIFO 后你会再次见到“格雷码跨域”，届时它传递的是“指针值”而非“事件有无”，正好和本讲形成升华对照。
- **u7-l2（时序约束与收敛）**：本讲只聚焦 `_FP_ATTR` 这一条 false_path；`create_clock`、Fmax 提取脚本，以及 `_SYNC_ATTR`/`_FP_ATTR` 两套约定的系统化对比，在那里完整展开。
- **阅读建议**：把 [cdc_strobe.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe.sv) 与上一讲的 [cdc_data.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_data.sv) 对照重读，体会仓库如何用“两个小模块 + 两套命名约定”覆盖跨时钟域的两大类需求（数据 vs 事件），这是本库最具迁移价值的设计思想之一。
