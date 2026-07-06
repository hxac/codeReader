# 顶层 FFT 模块结构

## 1. 本讲目标

本讲是「核心 RTL 模块拆解」单元的第一讲，目标是带读者**通读整个 FFT 处理器的顶层模块 `RTL/FFT.v`**，建立起对数据如何「从输入端口流向输出端口」的整体认知。

学完本讲后，你应该能够：

- 说清 `FFT` 模块每一个输入/输出端口的方向、位宽与含义，并指出复位是高有效还是低有效；
- 解释 12 位有符号输入 `din_r` 如何经过**符号扩展 + 左移 8 位**变成内部 24 位数据通路，以及为什么这一步等价于「放大 256 倍」；
- 画出 5 级流水线（`radix_no1`~`radix_no5`）的级联拓扑，看出每级「蝶形 + 移位 + ROM」的反馈回路，并说出第 5 级为什么没有 ROM；
- 解释末端 `out_r[23:8]` 截位为什么等价于「除以 256」，把内部 24 位通路还原成 16 位输出。

本讲只看「顶层连线」，不深入蝶形运算的算术细节（那是 u3-l2 的事），也不展开移位寄存器与 ROM 的内部实现（分别是 u3-l3、u3-l4）。我们把 `FFT.v` 当成一张「接线图」来读。

## 2. 前置知识

阅读本讲前，请确保你已经了解以下概念（它们在前置讲义中已建立）：

- **radix-2 DIF 五级分解**（u2-l1）：32 点 FFT 被递归拆成 5 级蝶形，每级「先加减、再乘旋转因子」，组数逐级翻倍、组大小逐级减半。
- **旋转因子的定点量化**（u2-l2）：旋转因子 \(W_N^k\) 被统一放大 \(S=2^8=256\) 倍后存成定点整数，因此整个数据通路都需要在「×256」的尺度上对齐。
- **仓库文件地图**（u1-l2）：`RTL/FFT.v` 是顶层，通过 10 条 `include` 把 1 个 `radix2`、5 个 `shift_N`、4 个 `ROM_N` 拉进来；第 5 级旋转因子被常数化，所以 ROM 只有 4 个。

此外需要两个 Verilog 基础概念：

- **有符号定点（signed fixed-point）**：在硬件里，「小数点在哪里」是设计者自己约定的。把一个整数左移 8 位（低位补 0）就相当于把它的数值放大 256 倍；只要所有参与运算的数都放大相同倍数，运算结果在「同一尺度」下就成立，最后再统一缩放回来。
- **二进制补码的符号扩展**：把一个 \(B\) 位有符号数扩成更宽的有符号数时，要把最高位（符号位）复制若干份补到高位，这样数值不变。例如 12 位有符号数扩成 16 位，就是高位补 4 位符号位 `{4{din[11]}}`。

## 3. 本讲源码地图

本讲只涉及一个源文件，但它是整个设计的「总线」：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [RTL/FFT.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v) | FFT 处理器顶层模块 | 端口、内部 wire/reg 网络、5 级模块例化、输入扩位、输出截位 |

顶层 `FFT.v` 内部例化的子模块（本讲只看它们的「端口连线」，不看内部）：

| 子模块 | 数量 | 在顶层中扮演的角色 |
| --- | --- | --- |
| `radix2` | 5 个（`radix_no1`~`radix_no5`） | 蝶形运算核：做加减 + 复数乘旋转因子 |
| `shift_16 / 8 / 4 / 2 / 1` | 各 1 个 | 反馈延时线（FIFO 移位寄存器），延时深度 16/8/4/2/1 |
| `ROM_16 / 8 / 4 / 2` | 各 1 个 | 存旋转因子 + 产生状态机控制信号（第 5 级无 ROM） |

---

## 4. 核心概念与源码讲解

### 4.1 端口与时钟复位

#### 4.1.1 概念说明

任何 Verilog 模块的第一件事都是「把对外接口讲清楚」。顶层 `FFT` 模块对外暴露的就是**数据通路的外部边界**：时钟、复位、握手信号、复数输入、复数输出。看懂端口，就等于看懂了这个 IP 的「引脚定义」。

需要特别注意的是**复位极性**：一个模块到底用「高有效复位」还是「低有效复位」，只能从它内部的 `always` 敏感列表判断，不能只看端口名。本模块端口叫 `reset`（没有 `_n` 后缀），稍后我们会从源码确认它确实是**高有效**。

#### 4.1.2 核心流程

端口可分成四组：

1. **时钟与复位**：`clk`（系统时钟，10 ns 周期 / 100 MHz）、`reset`（异步、高有效）。
2. **输入握手 + 数据**：`in_valid`（输入有效）、`din_r` / `din_i`（实部 / 虚部，12 位有符号）。
3. **输出握手 + 数据**：`out_valid`（输出有效）、`dout_r` / `dout_i`（实部 / 虚部，16 位有符号）。
4. 数据流方向：`din_*`（12 bit）→ 内部 24 bit 通路 → `dout_*`（16 bit）。

#### 4.1.3 源码精读

模块端口声明见 [RTL/FFT.v:L25-L34](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L25-L34)，这段代码定义了全部对外引脚：

```verilog
module FFT(
        input wire clk,
        input wire  reset,                 // 复位：稍后由 posedge reset 确认为高有效
        input wire in_valid,
        input wire signed [11:0] din_r,    // 实部输入，12 位有符号
        input wire signed [11:0] din_i,    // 虚部输入，12 位有符号
        output wire out_valid,
        output reg signed [15:0] dout_r,   // 实部输出，16 位有符号
        output reg signed [15:0] dout_i    // 虚部输出，16 位有符号
    );
```

确认复位极性的证据在主时序块 [RTL/FFT.v:L254-L255](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L254-L255)：

```verilog
always@(posedge clk or posedge reset)begin
    if(reset)begin ... end
```

敏感列表里写的是 `posedge reset`（reset 上升沿触发），且 `if(reset)` 分支里把所有寄存器清零——这说明 `reset=1` 表示「正在复位」，即**高有效异步复位**。

> 小提醒：前置讲义 u1-l3 里提到的 testbench 信号叫 `rst_n`（低有效）。两者不矛盾——testbench 内部会把 `rst_n` 反相后再驱动本模块的 `reset`。看 RTL 时只认本模块自己的 `reset`。

#### 4.1.4 代码实践

1. **实践目标**：把 `FFT` 模块的端口整理成一张「引脚表」，建立对外接口的清晰印象。
2. **操作步骤**：打开 [RTL/FFT.v:L25-L34](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L25-L34)，对每个端口填写「方向 / 位宽 / 有无 signed / 含义」四列。
3. **需要观察的现象**：注意 `din_*` 是 `input wire signed`，而 `dout_*` 是 `output reg signed`——一个是线网、一个是寄存器，差别源于输出需要打一拍。
4. **预期结果**：得到一张 7 行的端口表，并能在表下用一句话写出「复位为高有效异步复位」的依据（`posedge reset` + `if(reset)` 清零）。

#### 4.1.5 小练习与答案

**练习 1**：如果把端口里的 `reset` 改成低有效（即改名为 `rst_n`），顶层 `always` 的敏感列表和 `if` 条件分别要怎么改？

**参考答案**：敏感列表改为 `posedge clk or negedge rst_n`（下降沿触发），复位条件改为 `if(!rst_n)`，其余清零逻辑不变。

**练习 2**：`dout_r` 为什么是 `output reg` 而 `out_valid` 是 `output wire`？

**参考答案**：`dout_r` 在 [L304](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L304) 由 `next_dout_r` 打拍得到，必须寄存器输出；而 `out_valid = assign_out`（[L59](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L59)）只是把一个已寄存的 `reg` 用连续赋值引出，所以是线网。

---

### 4.2 输入符号扩展与定点对齐

#### 4.2.1 概念说明

这是本讲最容易看走眼的一处细节。输入明明是 12 位，内部数据通路却全是 24 位（见后续 `wire [23:0]`）。从 12 位到 24 位不是简单「高位补零」，而是一句同时完成**两件事**的拼接：

1. **符号扩展**：把 12 位有符号数扩成 16 位有符号数（数值不变）；
2. **左移 8 位（×256）**：在最低 8 位补 0，把数值放大 256 倍。

为什么要放大 256 倍？因为旋转因子在 u2-l2 里已经被量化成「真值 ×256」的定点整数。为了让「数据 × 旋转因子」在同一个尺度上相乘，输入数据也必须先抬到 ×256 的尺度。这就是**定点对齐**。

#### 4.2.2 核心流程

设输入 12 位有符号整数的真值为 \(V_{in}\)。经过符号扩展后数值仍为 \(V_{in}\)，再左移 8 位：

\[
V_{reg} \;=\; V_{in}\times 2^{8} \;=\; V_{in}\times 256
\]

拼接的位宽核算（必须正好等于 24）：

\[
\underbrace{\{4 \times \text{din}[11]\}}_{4\text{ 位符号扩展}} \;+\; \underbrace{\text{din}[11:0]}_{12\text{ 位原值}} \;+\; \underbrace{8'\text{b}0}_{8\text{ 位低位补零}} \;=\; 24\text{ 位}
\]

- 4 + 12 + 8 = 24，正好匹配 `reg signed [23:0] din_r_reg`。
- 高 4 位是符号位的复制（保持有符号数值不变）；
- 低 8 位是 0（等价于 ×256）。

#### 4.2.3 源码精读

寄存器声明见 [RTL/FFT.v:L46](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L46)，确认 `din_r_reg` 是 24 位有符号：

```verilog
reg signed [23:0] din_r_reg,din_i_reg;
```

真正的扩位发生在主时序块的 else 分支 [RTL/FFT.v:L273-L274](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L273-L274)：

```verilog
din_r_reg <= {{4{din_r[11]}},din_r,8'b0};
din_i_reg <= {{4{din_i[11]}},din_i,8'b0};
```

随后通过连续赋值打成线网 [RTL/FFT.v:L61-L62](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L61-L62)，再喂给第一级蝶形：

```verilog
assign din_r_wire = din_r_reg;
assign din_i_wire = din_i_reg;
```

于是在第一级蝶形例化处 [RTL/FFT.v:L99-L100](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L99-L100)，`din_b` 口收到的就是 24 位的、×256 尺度的数据：

```verilog
.din_b_r(din_r_wire),//input
.din_b_i(din_i_wire),//input
```

完整的位宽变换链路（实践任务会用到）是：

\[
\texttt{din\_r[11:0]} \;\xrightarrow{\text{L273 扩位}}\; \texttt{din\_r\_reg[23:0]} \;\xrightarrow{\text{L61 赋值}}\; \texttt{din\_r\_wire[23:0]} \;\xrightarrow{\text{L99 例化}}\; \texttt{radix\_no1.din\_b\_r[23:0]}
\]

#### 4.2.4 代码实践

1. **实践目标**：亲手验证 `{{4{din_r[11]}},din_r,8'b0}` 的位宽与数值。
2. **操作步骤**：
   - 取一个测试值，例如 `din_r = 12'sd100`（十进制 100）。它落在 12 位有符号范围内。
   - 手算：符号位 `din_r[11]=0`，所以高 4 位是 `0000`，中间 12 位是 100 的二进制，低 8 位是 0。
   - 算出 `din_r_reg` 的十进制值（应该是 100 × 256 = 25600）。
   - 再取一个负值 `din_r = -3`，重复上述手算（注意 12 位补码下 −3 = `0xFFD`，符号位为 1，高 4 位符号扩展为 `1111`）。
3. **需要观察的现象**：无论正负，扩位后数值都恰好是原值的 256 倍，符号保持正确。
4. **预期结果**：填一张表，列「输入真值 / 12 位补码 / 扩位后 24 位 / 扩位后十进制」，确认最后一列恒等于第一列 ×256。

> 说明：本实践是「纸笔 + 脑补」型，不需要仿真器。若你已在跑仿真，也可在 testbench 里用 `$display` 打印 `din_r_reg` 验证，但需先把 `din_r_reg` 通过层次路径引用出来（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：如果把拼接里的 `8'b0` 改成 `4'b0`（只左移 4 位），整个 FFT 的结果会怎样变化？

**参考答案**：输入只放大 \(2^4=16\) 倍，而旋转因子仍是 ×256，两者尺度不再一致。每次「数据 × 旋转因子」会少贡献一个 16 倍因子，最终输出幅度会比正确值小 16 倍（且不同级累积会进一步偏移），SNR 判定大概率失败。

**练习 2**：为什么符号扩展是 4 位，而不是 12 位或别的位数？

**参考答案**：因为总宽 24 位 = 4（符号扩展）+ 12（原值）+ 8（移位）。4 位符号扩展只是为了「填满高位、保持数值」，具体位数由「24 − 12 − 8 = 4」决定，不是任意选的。

---

### 4.3 五级蝶形 + 移位 + ROM 例化

#### 4.3.1 概念说明

这是顶层 `FFT.v` 的「主角」——把 5 个 `radix2`、5 个 `shift`、4 个 `ROM` 像搭积木一样连成 5 级流水线。这一节只看「积木怎么连」，不看积木内部。

理解连线的钥匙是一个反复出现的**反馈回路拓扑**（这正是「单路延迟换向器 SDC」架构的特征）。每一级（以第 1 级为例）都长这样：

```
          ┌─────────── feedback (延时回环) ──────────┐
          ↓                                          │
      ┌───┴────────┐   delay   ┌──────────┐   dout   │
      │  radix_no1 │ ─────────▶│ shift_16 │ ─────────┘
      │            │            └──────────┘
      │            │   op                          state/w
      │            │ ────┐               ┌──────────────┐
      └────────────┘     │               │   ROM_16     │
            ↑ din_b       └──────────────▶│              │
            │                             └──────────────┘
       din_r_wire                                ↑
       (前级或输入)                          in_valid
```

每一级有三条关键连线：

1. **反馈回环**：`radix.delay` → `shift.din` → `shift.dout` → `radix.din_a`。蝶形把「需要延时的那一半数据」从 `delay` 口吐出，进 shift 延时线，延时后再从 `din_a` 回流到蝶形，与下一批新数据配对。
2. **前向数据**：`radix.op` → 下一级的 `din_b`。蝶形算出的「另一半结果」直接送给下一级。
3. **控制/旋转因子**：`ROM` 同时给蝶形提供旋转因子 `w_r/w_i` 和状态机控制 `state`（除第 5 级外）。

第 5 级是个**特例**：它的旋转因子被常数化为 \(W=256+j\cdot0\)（即真值 \(1+j0\)，因为最后一级旋转因子恒为 1，见 u2-l1），所以**不需要 ROM**；它的 `state` 由顶层自己用组合逻辑生成。

#### 4.3.2 核心流程

把 5 级串起来，数据流的「主轴」是这样走的（每级之间是前向 `op` → 下一级 `din_b`）：

```
din_r/din_i (12b)
   │  ×256 扩位 (L273)
   ▼
[radix_no1] ──op──▶ [radix_no2] ──op──▶ [radix_no3] ──op──▶ [radix_no4] ──op──▶ [radix_no5] ──▶ out_r/out_i (24b)
   ▲  └shift_16┘     ▲  └shift_8┘       ▲  └shift_4┘       ▲  └shift_2┘       ▲  └shift_1┘
  ROM_16             ROM_8              ROM_4              ROM_2            (无 ROM,
                                                                          w=256+j0)
```

各级反馈延时线的深度（即 shift 模块名里的数字）逐级减半：

| 级 | 蝶形 | 延时线 | ROM | 反馈延时深度 | 与算法的关系 |
| --- | --- | --- | --- | --- | --- |
| 1 | `radix_no1` | `shift_16` | `ROM_16` | 16 | \(N/2 = 32/2\) |
| 2 | `radix_no2` | `shift_8` | `ROM_8` | 8 | \(N/4\) |
| 3 | `radix_no3` | `shift_4` | `ROM_4` | 4 | \(N/8\) |
| 4 | `radix_no4` | `shift_2` | `ROM_2` | 2 | \(N/16\) |
| 5 | `radix_no5` | `shift_1` | 无（常数） | 1 | \(N/32\) |

延时深度 16→8→4→2→1 的减半规律，正对应 radix-2 DIF 把 32 点逐级对半分解的结构（见 u2-l1）。延时线深度 \(N/2^{k+1}\) 与「该级需要在反馈路径上暂存多少样本」直接相关——这部分物理含义会在 u3-l3（移位寄存器）展开。

#### 4.3.3 源码精读

**第 1 级**（标准模板）见 [RTL/FFT.v:L95-L126](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L95-L126)，包含蝶形、移位、ROM 三件套：

```verilog
radix2 radix_no1(
        .state(rom16_state),          // 来自 ROM_16 的状态控制
        .din_a_r(shift_16_dout_r),    // feedback：延时线回流
        .din_a_i(shift_16_dout_i),
        .din_b_r(din_r_wire),         // input：外部输入（已 ×256）
        .din_b_i(din_i_wire),
        .w_r(rom16_w_r),              // twiddle：旋转因子实部
        .w_i(rom16_w_i),
        .op_r(radix_no1_op_r),        // 前向输出 → 下一级 din_b
        .op_i(radix_no1_op_i),
        .delay_r(radix_no1_delay_r),  // 需延时的数据 → shift_16
        .delay_i(radix_no1_delay_i),
        .outvalid(radix_no1_outvalid) // 有效信号 → 下一级 shift/ROM
    );

shift_16 shift_16( .din_r(radix_no1_delay_r), .dout_r(shift_16_dout_r) ... );
ROM_16  rom16 ( .w_r(rom16_w_r), .state(rom16_state) ... );
```

> 注意三个 `in_valid` 的来源不同，这是「握手菊花链」：`shift_16` 与 `ROM_16` 的 `in_valid` 都来自被寄存过的 `in_valid_reg`（[L112](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L112)、[L121](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L121)）。

**第 2~4 级**结构完全一致，只是改了名字与延时深度：`radix_no2`+`shift_8`+`ROM_8`（[L128-L159](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L128-L159)）、`radix_no3`+`shift_4`+`ROM_4`（[L162-L193](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L162-L193)）、`radix_no4`+`shift_2`+`ROM_2`（[L196-L227](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L196-L227)）。它们的 `in_valid` 分别来自前一级的 `outvalid`（例如 [L145](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L145)、[L154](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L154)），形成逐级传递的有效信号链。

**第 5 级（特例）** 见 [RTL/FFT.v:L230-L252](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L230-L252)：

```verilog
radix2 radix_no5(
        .state(no5_state),            // 注意：不是来自 ROM，而是顶层自生成
        .din_a_r(shift_1_dout_r),
        .din_b_r(radix_no4_op_r),     // 前向：第 4 级输出
        .w_r(24'd256),                // ★ 旋转因子常数化：实部 = 256（即真值 1）
        .w_i(24'd0),                  // ★ 虚部 = 0
        .op_r(out_r),                 // 最终 24 位输出
        .op_i(out_i),
        .delay_r(radix_no5_delay_r),
        .outvalid()                   // ★ 悬空：第 5 级不需要再传 outvalid
    );
```

三处「★」就是第 5 级与前 4 级的本质区别：

1. **旋转因子直接写常数** `24'd256` / `24'd0`（[L236-L237](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L236-L237)）。256 在 ×256 尺度下就是真值 1，对应 \(W_N^0=1\)——这正是 radix-2 DIF 最后一级旋转因子恒为 1 的体现（u2-l1）。
2. **没有 ROM 例化**：因为旋转因子不需要查表。
3. **`outvalid` 端口悬空**（`.outvalid()`，[L242](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L242)）：第 5 级的「数据已被消费」信号改由顶层自己用 `r4_valid` + `s5_count` 组合生成 `no5_state`（见 [L292-L298](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L292-L298)），这部分控制时序留待 u4-l3 详解。

#### 4.3.4 代码实践

1. **实践目标**：把 5 级流水线的「连线关系」整理成一张表，并标出每级延时深度。
2. **操作步骤**：
   - 打开 [RTL/FFT.v:L95-L252](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L95-L252)。
   - 对每一级，抄下三组连线：① `din_a`（反馈）来自哪个 shift；② `din_b`（前向）来自哪里；③ `w`（旋转因子）来自哪个 ROM 还是常数。
   - 统计每级 shift 模块名里的数字，填出延时深度列。
3. **需要观察的现象**：第 1~4 级是「蝶形 + shift + ROM」三件套，第 5 级只有「蝶形 + shift」两件套，且旋转因子为常数 256。
4. **预期结果**：得到本节 4.3.2 那张「级 / 蝶形 / 延时线 / ROM / 延时深度」表（16/8/4/2/1），并能口头解释「为什么第 5 级没有 ROM」。

#### 4.3.5 小练习与答案

**练习 1**：第 1 级蝶形的 `din_a`（反馈）和 `din_b`（前向输入）分别接到哪里？为什么需要这两路输入？

**参考答案**：`din_a` 接 `shift_16_dout`（本级延时线的输出，是「上一批被延时的数据」）；`din_b` 接 `din_r_wire`（外部新输入）。蝶形运算需要把「延时回来的旧数据」和「当前新数据」配对做加减，所以才需要两路输入——这就是 SDC 反馈回环的意义。

**练习 2**：如果把第 5 级的 `.w_r(24'd256)` 误写成 `.w_r(24'd0)`，FFT 输出会变成什么样？

**参考答案**：第 5 级旋转因子本应是 1，写成 0 后，second half 的复数乘法会把所有「需要乘旋转因子」的分支乘成 0，输出会大面积出错（实部/虚部大量为 0 或异常），SNR 判定必然失败。

**练习 3**：观察 5 个 shift 模块的延时深度 16/8/4/2/1，写出第 \(k\) 级（\(k=1..5\)）延时深度的通项公式。

**参考答案**：第 \(k\) 级延时深度 \(= 32 / 2^{k} = 2^{5-k}\)，即 \(16, 8, 4, 2, 1\)。它正好等于该级蝶形需要暂存的样本数 \(N/2^{k}\)。

---

### 4.4 输出截位：24 位通路还原成 16 位输出

#### 4.4.1 概念说明

输入端我们把数据「放大 256 倍」抬到 ×256 尺度（4.2 节）；经过 5 级蝶形运算后，输出 `out_r/out_i` 仍是 24 位的 ×256 尺度数据。但对外端口 `dout_r/dout_i` 只有 16 位。怎么把 24 位「缩」回 16 位？

答案就是**截位**：取 24 位的高 16 位 `out_r[23:8]`。这在数值上等价于**右移 8 位（除以 256）**，正好抵消输入端的 ×256，把结果还原成「整数尺度」。

> 为什么输出是 16 位而输入是 12 位？因为 FFT 运算过程中（尤其多级蝶形累加）数值会增长，需要更多位宽来容纳动态范围，所以输出比输入宽 4 位。这正是定点 FFT 设计中「字长增长」的体现。

#### 4.4.2 核心流程

末端截位的数值关系：

\[
V_{out\_16} \;=\; \left\lfloor \frac{V_{path\_24}}{2^{8}} \right\rfloor
\]

即取 `out_r[23:8]` 这 16 位，丢弃最低 8 位 `[7:0]`。这是**截断（truncation）而非四舍五入**——直接扔掉低位，会引入一个小于 1 个 LSB 的量化误差，但在 16 位精度下通常可以接受（SNR ≥ 40 dB 的判定就是用来兜底这个误差的，见 u5-l1）。

尺度上的对称美：

\[
\underbrace{V_{in}}_{12\text{ 位}} \;\xrightarrow{\times 256}\; \underbrace{V_{path}}_{24\text{ 位通路}} \;\xrightarrow{/ 256}\; \underbrace{V_{out}}_{16\text{ 位}}
\]

输入放大、输出缩小，旋转因子也统一在 ×256 尺度——整个数据通路在同一套定点约定下自洽。

#### 4.4.3 源码精读

末端截位发生在那个大型 `case(y_1)` 排序块里。每一条 case 分支都在做同一件事——把 24 位 `out_r` 截成 16 位写入排序缓冲。以第一条分支为例 [RTL/FFT.v:L324-L329](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L324-L329)：

```verilog
case((y_1))
5'd0 : begin
        result_r_ns[31] = out_r[23:8];   // ★ 24 位截高 16 位 = /256
        result_i_ns[31] = out_i[23:8];
    end
```

`out_r[23:8]` 表示取第 23 位 down to 第 8 位，共 \(23-8+1=16\) 位。其余 31 条分支（如 [L330-L333](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L330-L333)）结构完全相同，只是写入的 `result_r_ns[索引]` 不同——这个「索引」由 `y_1` 决定，实现的是**位反转排序**（u2-l3、u4-l2 专题讲解，本讲只需知道它把乱序输出整理成正常顺序）。

排序缓冲 `result_r[0:31]` 是 16 位深的二维数组，声明见 [RTL/FFT.v:L37](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L37)。最终 16 位结果在 [L304-L305](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L304-L305) 读出打拍成 `dout_r`：

```verilog
next_dout_r = result_r[y_1_delay];
next_dout_i = result_i[y_1_delay];
```

所以从 24 位 `out_r` 到 16 位 `dout_r` 的完整链路是：

\[
\texttt{out\_r[23:0]} \;\xrightarrow{\text{L326 截位 [23:8]}}\; \texttt{result\_r\_ns[索引] (16b)} \;\xrightarrow{\text{L285 寄存}}\; \texttt{result\_r[索引] (16b)} \;\xrightarrow{\text{L304 读出}}\; \texttt{dout\_r[15:0]}
\]

#### 4.4.4 代码实践

1. **实践目标**：确认 `out_r[23:8]` 是 16 位，并理解截位的数值含义。
2. **操作步骤**：
   - 数一数 `[23:8]` 的位宽：\(23 - 8 + 1 = 16\)，正好对上 `result_r_ns` 元素的 16 位宽。
   - 假设某拍 `out_r = 24'd25600`（即真值 100，因为 100×256=25600）。手算 `out_r[23:8]` 的十进制值，应该是 100。
   - 再假设 `out_r = 24'd25650`（真值约 100.195）。截位后 `out_r[23:8]` 应为 100（小数部分被丢弃），体会「截断误差」。
3. **需要观察的现象**：截位等价于整除 256，会丢掉不足 256 的尾数。
4. **预期结果**：写出两三个 `out_r` 取值 → `out_r[23:8]` 的对照，确认「/256 取整」关系，并指出最大截断误差不超过 1 个 16 位 LSB。

> 说明：本实践为纸笔验证。若要在仿真中观察，可在 testbench 里把 `dout_r` 与软件参考 `FFT.py` 的输出对比，统计误差——这正是 u5-l1 SNR 验证做的事。

#### 4.4.5 小练习与答案

**练习 1**：截位 `out_r[23:8]` 等价于除以多少？为什么这个除数能抵消输入端的放大？

**参考答案**：等价于除以 256（右移 8 位）。因为输入端在 [L273](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L273) 恰好左移 8 位（×256），输出端右移 8 位正好抵消，把数据从 ×256 尺度还原回整数尺度。

**练习 2**：如果把截位改成四舍五入（在截位前加上 `out_r[7]` 进位），对 SNR 会有什么影响？

**参考答案**：四舍五入的量化误差比直接截断更小（截断误差范围 \([0,1)\) LSB 且有偏，舍入误差范围 \((-0.5,0.5]\) LSB 且无偏），长期看 SNR 会略有提升。本项目为省硬件用了直接截断，靠 16 位字长保证 SNR ≥ 40 dB。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成下面这个**贯通任务**——「画一张 FFT 顶层的数据流与位宽变换总图」。

**任务**：在一张纸上（或文档里）画出从 `din_r/din_i` 到 `dout_r/dout_i` 的完整数据通路，要求标注：

1. **输入扩位点**：在 `din_r[11:0]` 旁边标注「L273：`{{4{din_r[11]}},din_r,8'b0}` → ×256，12b→24b」，并画出经过 `din_r_reg` → `din_r_wire` → `radix_no1.din_b` 的三段路径。
2. **五级流水线框**：画出 5 个蝶形（`radix_no1`~`radix_no5`）的前向 `op` 箭头链；在每个蝶形下方画出它的反馈延时线（`shift_16`/`shift_8`/`shift_4`/`shift_2`/`shift_1`），并标注延时深度 16/8/4/2/1；在前 4 级上方画出对应的 ROM（`ROM_16`/`ROM_8`/`ROM_4`/`ROM_2`），第 5 级标注「w=256+j0，无 ROM」。
3. **输出截位点**：在 `radix_no5` 的 `op_r/out_r` 之后标注「L326：`out_r[23:8]` → /256，24b→16b」，并画出经过 `result_r_ns` → `result_r` → `dout_r` 的路径。
4. **尺度标注**：在图的最左、中、右三处分别写「整数尺度（×1）」「×256 尺度」「整数尺度（×1）」，体现「放大—运算—缩小」的对称结构。

**自检问题**（做完图后回答）：

- 输入 12 位是怎么变成 24 位的？（答：4 位符号扩展 + 12 位原值 + 8 位左移 = 24 位，数值 ×256）
- 第 5 级为什么没有 ROM？（答：最后一级旋转因子恒为 1，常数化为 256+j0，无需查表）
- 输出 16 位是怎么从 24 位来的？（答：取 `out_r[23:8]` 高 16 位，等价 /256，抵消输入端的 ×256）
- 5 级延时深度满足什么规律？（答：16/8/4/2/1，逐级减半，第 k 级为 \(N/2^k\)）

> 说明：本实践为「源码阅读 + 画图」型，不依赖仿真器。如果你已在跑仿真（u1-l3），可额外在波形里量一下从 `in_valid` 拉高到 `out_valid` 拉高的拍数，验证你画的流水线深度与实际延迟一致（待本地验证）。

## 6. 本讲小结

- **端口**：`FFT` 模块对外是 12 位有符号输入、16 位有符号输出，`reset` 为**高有效异步复位**（依据 `posedge reset` + `if(reset)` 清零）。
- **输入定点对齐**：`din_r` 经 `{{4{din_r[11]}},din_r,8'b0}` 一步完成「4 位符号扩展 + 左移 8 位」，12 位变 24 位，数值 ×256，与旋转因子的 ×256 尺度对齐。
- **五级流水线**：前 4 级是「`radix2` + `shift_N` + `ROM_N`」三件套反馈回路，延时深度 16/8/4/2 逐级减半；第 5 级无 ROM，旋转因子常数化为 `24'd256`/`24'd0`，`outvalid` 悬空、`state` 由顶层自生成。
- **连线拓扑**：每级有反馈回环（`radix.delay`→`shift`→`radix.din_a`）和前向链（`radix.op`→下一级 `din_b`）；握手信号 `in_valid`/`outvalid` 逐级菊花链传递。
- **输出截位**：末端 `out_r[23:8]` 取高 16 位，等价 /256，把 ×256 尺度还原成整数尺度，写入 16 位排序缓冲后输出 `dout_r`。
- **尺度自洽**：输入 ×256、内部 24 位通路、输出 /256，整套定点约定前后呼应、数值闭环。

## 7. 下一步学习建议

本讲只看了「顶层连线和位宽变换」，把每个子模块当成黑盒。接下来应该逐个「打开黑盒」：

- **u3-l2 radix2 蝶形单元与 SDC 处理元**：打开 `radix2.v`，看它的三态机（waiting/first half/second half）如何消费本讲提到的 `state` 信号，以及 second half 的复数乘法如何从「4 乘 2 加」优化为「3 乘 5 加」。
- **u3-l3 移位寄存器与延时单元**：打开 `shift_16.v` 等，看一个 384 位超宽寄存器如何用 `(tmp_reg<<24)+din` 实现本讲提到的「延时深度 16」的 FIFO 反馈。
- **u3-l4 ROM 与状态控制模块**：打开 `ROM_16.v` 等，看它如何用一个计数器同时产生旋转因子查表和驱动蝶形状态机的 `state` 信号。

读完这三讲，再回头看本讲的「五级流水线图」，你会发现自己已经能从门级理解每一条连线背后的电路，届时可继续进入 u4（流水线集成与控制时序）。
