# 一个模块长什么样：统一的文件结构

## 1. 本讲目标

上一讲我们认识了 basic_verilog 这个「可综合 Verilog/SystemVerilog 模块库」的整体面貌。本讲要拉近镜头，看清楚**单独一个模块文件内部长什么样**。

学完本讲，你应该能够：

- 识别仓库里几乎每一个 `.sv` 文件都遵循的**四段式结构**：头注释 / INFO / 例化模板 / module 实现；
- 看懂 SystemVerilog 的**参数化端口**写法 `#(parameter ...)`，理解参数如何决定位宽；
- 理解 `always_ff` 时序逻辑块的标准写法，区分同步复位与异步复位；
- 学会**复制例化模板**，把一个模块快速用进自己的工程。

本讲的两个样板文件是 [clk_divider.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clk_divider.sv) 和 [edge_detect.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect.sv)，它们都是 README 标记为 🟢 绿圈的基础模块，最适合用来建立直觉。

## 2. 前置知识

在进入源码前，先用大白话对齐三个概念：

- **模块（module）**：Verilog 里描述一块硬件的基本单位，有输入输出端口，内部用逻辑描述行为。可以把它类比成一个「芯片的引脚图 + 内部电路」。
- **例化（instantiation）**：把一个已经写好的模块「放进」另一个模块里使用，就像在电路板上焊一颗芯片。例化时要连好端口。
- **时序逻辑 vs 组合逻辑**：组合逻辑的输出只取决于当前输入（像加法器）；时序逻辑的输出依赖时钟沿，能「记住」过去的状态（像计数器、寄存器）。`always_ff` 用来写时序逻辑，`always_comb` 用来写组合逻辑。

如果你对这几个词还陌生，不用急，本讲会结合代码反复说明。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用它说明什么 |
|------|------|------------------|
| [clk_divider.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clk_divider.sv) | 用一个自由计数器分频出多个慢时钟 | 四段式结构的「最简样板」、同步复位 |
| [edge_detect.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect.sv) | 检测信号的上升沿/下降沿，输出单拍脉冲 | 带类型与注释的参数、异步复位、`generate` 多实现 |

这两个文件加起来不到 150 行，却把仓库的写作规范体现得淋漓尽致。

## 4. 核心概念与源码讲解

### 4.1 模块文件四段式约定

#### 4.1.1 概念说明

翻开 basic_verilog 里的任意一个模块文件，你几乎总能看到**同样的四段**，自上而下依次出现。作者把这种统一风格贯穿整个仓库，目的是让任何读者打开任何文件都能「按图索骥」：

1. **头注释（Header）**：一行横线包围的小块，写明文件名、所属仓库、作者邮箱。
2. **INFO 说明**：以 `// INFO ----` 开头，用几行注释讲清楚「这个模块是干什么的」，有时还附带版本变更说明和设计取舍。
3. **例化模板（Instantiation Template）**：包在 `/* --- ... --- */` 块注释里，给出一段**可以直接复制粘贴**的例化代码，告诉使用者端口怎么连。
4. **module 实现**：真正的 `module ... endmodule` 硬件描述。

这四段不是语法要求，而是**仓库约定（convention）**。读懂这个约定，你就掌握了阅读本仓库所有模块的「目录页」。

#### 4.1.2 核心流程

打开一个新模块时，建议按下面顺序读：

```
1. 看头注释      -> 确认文件名和出处（是不是我要找的模块？）
2. 读 INFO       -> 用自然语言理解功能（它解决什么问题？）
3. 抄例化模板    -> 先会用，不必立刻懂内部实现（怎么把它接进我的工程？）
4. 再看 module   -> 需要深入或调试时，才读真正的实现（它是怎么做到的？）
```

这种「先会用、再懂原理」的顺序，正是作者提供例化模板的用意。

#### 4.1.3 源码精读

**第一段：头注释**。`clk_divider.sv` 的开头是一个被横线包围的小块：

[clk_divider.sv:L1-L5](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clk_divider.sv#L1-L5) —— 标注文件名、仓库地址、作者邮箱，相当于文件的「名片」。

**第二段：INFO 说明**。紧跟着是 `// INFO ----` 引导的功能描述：

[clk_divider.sv:L7-L9](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clk_divider.sv#L7-L9) —— 一句话说明「把主时钟分频，得到多个更慢的同步时钟」。

`edge_detect.sv` 的 INFO 更详细，还附带了版本说明和一条重要提示：

[edge_detect.sv:L7-L19](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect.sv#L7-L19) —— 说明了版本、参数化改动、复位方式，以及「当输入每拍都翻转时，`both` 输出会一直有效」这条边界行为。读 INFO 往往能帮你提前避开坑。

**第三段：例化模板**。INFO 之后是一段被块注释包起来的「现成代码」：

[clk_divider.sv:L11-L22](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clk_divider.sv#L11-L22) —— 这是 `clk_divider` 的例化模板。它被注释包住，所以不会被编译；但它展示了**完整的用法**：参数怎么传（`.WIDTH( 32 )`）、例化名（`CD1`）、每个端口接什么信号。

`edge_detect.sv` 的例化模板结构完全一致，只是参数和端口更多：

[edge_detect.sv:L22-L36](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect.sv#L22-L36) —— 注意它连「不用的端口」也写出来了（如 `.falling( )`、`.both( )` 留空），这是一种好习惯：所有端口都显式列出，避免遗漏。

**第四段：module 实现**。最后才是真正的硬件描述：

[clk_divider.sv:L25-L43](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clk_divider.sv#L25-L43) —— 从 `module clk_divider ...` 到 `endmodule`。第四段我们会在 4.2、4.3 节细看端口和时序逻辑。

> 小结：四段式 = 名片 + 说明书 + 用法示例 + 真正实现。后续单元遇到任何模块，都可以套这个框架去读。

#### 4.1.4 代码实践

**实践目标**：用肉眼确认「四段式约定」在整个仓库的普遍性。

**操作步骤**：

1. 打开本仓库根目录，任选两个还没看过的模块，例如 `delay.sv` 和 `pwm_modulator.sv`。
2. 对每个文件，分别定位它的：① 头注释横线块、② `// INFO` 行、③ `/* --- INSTANTIATION TEMPLATE BEGIN ---` 行、④ `module` 关键字所在行。
3. 用文本编辑器的「跳转到行」逐一定位。

**需要观察的现象**：这四个标记是否在两个文件里都能找到，且出现顺序一致（头注释 → INFO → 模板 → module）。

**预期结果**：绝大多数根目录下的 `.sv` 文件都严格遵循同一顺序。若发现个别文件缺某一段（例如某些纯工具函数文件可能没有例化模板），把它记下来——那通常是「不便于直接例化」的特殊模块。

> 待本地验证：具体哪些文件缺段，需要你实际打开确认；本讲不假设已运行任何命令。

#### 4.1.5 小练习与答案

**练习 1**：例化模板为什么用块注释 `/* ... */` 包起来，而不是直接写成可编译代码？

**参考答案**：因为同一个文件里已经有一个 `module clk_divider` 的定义了。如果模板再以普通代码出现，就会出现「重复定义同名模块」的语法错误。包在注释里，既能让读者看到用法，又不会参与编译。

**练习 2**：INFO 段是给谁看的？综合工具（编译器）会读它吗？

**参考答案**：INFO 是给人看的自然语言文档，描述功能和注意事项。综合工具会忽略所有注释，所以 INFO 不影响电路；它的价值在于让其他工程师（包括未来的你）快速理解模块意图。

---

### 4.2 parameter 化端口

#### 4.2.1 概念说明

如果说四段式是「文件的骨架」，那么**参数化端口**就是 basic_verilog 模块「高度复用」的秘密武器。

一个计数器，今天可能需要 8 位，明天可能需要 32 位。如果每次都重写代码，维护成本很高。SystemVerilog 的 `parameter`（参数）允许你把「位宽」这类数值**抽出来当成可配置项**：模块内部用参数名占位，使用者在例化时再传入具体数值。这样**一份代码就能适配多种位宽**。

basic_verilog 几乎所有模块都是参数化的，这正是它号称「跨项目、跨厂商高度复用」的根基。

#### 4.2.2 核心流程

参数化模块的写法可以概括为三步：

```
1. 在 module 名后用 #( parameter ... ) 声明参数，并给默认值
2. 在端口列表里用参数名参与计算位宽，例如 [(WIDTH-1):0]
3. 例化时用 .参数名( 数值 ) 覆盖默认值（不写则用默认值）
```

#### 4.2.3 源码精读

先看 `clk_divider.sv` 的端口声明，这是最朴素的参数化写法：

[clk_divider.sv:L25-L32](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clk_divider.sv#L25-L32) —— 几个要点：

- `#( parameter WIDTH = 32 )`：声明参数 `WIDTH`，默认值 32。它没有写类型，因此是「无类型参数」（隐式 `logic`/整数）。
- `output logic [(WIDTH-1):0] out = '0`：输出 `out` 的位宽是 `WIDTH` 位。当 `WIDTH=32` 时，它就是 `[31:0]`。
- `= '0`：这是 SystemVerilog 的写法，表示「把整根线初始化为 0」。`'0` 会自动展开成对应位宽的全 0，比写 `32'b0` 更通用。

例化模板里 `.WIDTH( 32 )` 重新把参数设成 32（和默认值相同，仅作演示）：

[clk_divider.sv:L13-L14](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clk_divider.sv#L13-L14) —— 把它改成 `.WIDTH( 8 )`，输出就变成 8 位，模块行为随之改变，但**源码一行都不用动**。

再看 `edge_detect.sv`，它展示了**带类型、带注释的参数**写法：

[edge_detect.sv:L39-L51](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect.sv#L39-L51) —— 这里有两点比 `clk_divider` 更进一步：

- `bit [7:0] WIDTH = 1`：参数显式声明为 8 位无符号（`bit [7:0]`），最大可表示 255。行尾注释 `// signal width` 说明含义。带类型能让工具更早发现「传了一个超大值」之类的错误。
- `bit [0:0] REGISTER_OUTPUTS = 1'b0`：这是一个「开关型」参数，只取 0 或 1，用来在「组合实现」和「寄存实现」之间二选一（见 4.3 节的 `generate`）。

端口部分 `[WIDTH-1:0] in`、`[WIDTH-1:0] rising` 等同样用 `WIDTH` 决定总线宽度，于是**一组参数化的边沿检测器可以并行处理多位信号**。

> 两种端口位宽写法对照：`[(WIDTH-1):0]`（`clk_divider`）与 `[WIDTH-1:0]`（`edge_detect`）完全等价，括号只是让运算顺序更显式。本仓库两种风格都有，读到时不必困惑。

#### 4.2.4 代码实践

**实践目标**：亲手体会「改参数即改电路」，不必读实现细节。

**操作步骤**：

1. 复制 `clk_divider.sv` 的例化模板，把 `.WIDTH( 32 )` 改成 `.WIDTH( 4 )`。
2. 想象 `WIDTH=4` 时，`out` 是 4 位（`[3:0]`）。
3. 用上一讲你了解到的 testbench 思路，设想在波形里观察 `out[0]`、`out[1]`、`out[2]`、`out[3]`。

**需要观察的现象**：`out` 总线从 32 位缩小到 4 位，但模块源码本身没有改动。

**预期结果**：综合后资源占用更少；`out[3]` 成为最高有效位（每 16 个时钟翻转一次）。

> 待本地验证：确切的资源占用和波形需要你在 iverilog/Quartus/Vivado 中实际编译查看。

#### 4.2.5 小练习与答案

**练习 1**：`edge_detect.sv` 里为什么把 `WIDTH` 声明成 `bit [7:0]` 而不是不限类型？如果使用者传入 `WIDTH = 300` 会怎样？

**参考答案**：`bit [7:0]` 限定参数取值范围是 0~255，是一种防御性写法，也便于工具做范围检查。若传入 300，会超出 8 位能表示的范围，行为取决于工具，通常会被截断或报警——这正是作者加类型约束想避免的情况。

**练习 2**：例化时如果不写 `.WIDTH( ... )`，模块还能用吗？位宽是多少？

**参考答案**：能用。参数有默认值（`clk_divider` 是 32，`edge_detect` 是 1），不显式传参就采用默认值。所以 `clk_divider CD1 (.clk(clk), ...);` 是合法的，`out` 会是 32 位。

---

### 4.3 always_ff 时序逻辑

#### 4.3.1 概念说明

参数化决定了「电路有多宽」，而 `always_ff` 决定了「电路在时钟驱动下怎么动」。

`always_ff` 是 SystemVerilog 专门用来写**时序逻辑**（带时钟、带状态）的关键字。它会综合成一排触发器（flip-flop）。紧跟其后的 `@(posedge clk)` 表示「每次时钟上升沿，执行一次块内的语句」。

本仓库的时序模块几乎都用 `always_ff`，并且遵循一个清晰的模式：**先写复位分支，再写正常工作分支**。复位又分两种：

- **同步复位（synchronous）**：复位信号只在时钟沿生效。`clk_divider` 用的是这种，复位信号叫 `nrst`。
- **异步复位（asynchronous）**：复位信号一来立刻生效，不等时钟。`edge_detect` 用的是这种，复位信号叫 `anrst`（前缀 `a` 提示 async）。

#### 4.3.2 核心流程

标准 `always_ff` 模板的伪代码：

```
always_ff @(posedge clk [or negedge nrst]) begin   // [] 内为异步复位时才加
  if ( ~nrst ) begin
      // 复位分支：把所有寄存器清零
  end else begin
      // 正常工作分支：用非阻塞赋值 <= 更新状态
  end
end
```

关键约定：

- 敏感列表里只有 `posedge clk` → 同步复位；
- 敏感列表里是 `posedge clk or negedge nrst` → 异步复位（复位下降沿也触发）。
- 块内一律用**非阻塞赋值 `<=`**，这是时序逻辑的标准写法，能避免仿真与综合不一致。

#### 4.3.3 源码精读

`clk_divider.sv` 是**同步复位 + 自由计数器**的最简例子：

[clk_divider.sv:L35-L41](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clk_divider.sv#L35-L41) —— 逐行解读：

- `always_ff @(posedge clk)`：敏感列表只有时钟上升沿，所以这是**同步复位**。
- `if ( ~nrst )`：`nrst` 是高有效（active-high）的「使能式」复位命名——`~nrst` 为真即「复位有效」。复位时把 `out` 清零。
- `else if (ena)`：`ena` 是使能信号，为真时才计数。
- `out[...] <= out[...] + 1'b1`：非阻塞赋值，每个时钟沿把计数器加 1。由于 `out` 是 `WIDTH` 位，它会自然从全 0 数到全 1 再回绕到 0，于是每一位就是一个**二分频、四分频、八分频……**的方波。这就是「分频器」的本质：一个自由运行的二进制计数器。

`edge_detect.sv` 展示了**异步复位 + 数据延迟线**：

[edge_detect.sv:L55-L61](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect.sv#L55-L61) —— 注意敏感列表多了 `or negedge anrst`：`anrst` 一旦拉低，立刻触发清零，不必等时钟沿，这就是**异步复位**。`in_d` 是输入 `in` 延迟一拍的副本——比较 `in` 和 `in_d` 就能知道信号是不是「刚刚跳变」，从而得到边沿脉冲。

边沿的布尔运算是用 `always_comb`（组合逻辑）写的：

[edge_detect.sv:L66-L70](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect.sv#L66-L70) —— `rising = anrst 有效 且 (当前为1 且 上一拍为0)`。`{WIDTH{anrst}}` 是把 1 位 `anrst` 复制成 `WIDTH` 位宽，用来在复位时把输出强制清零（一种门控手法）。`always_comb` 用的是阻塞赋值 `=`，与 `always_ff` 的 `<=` 形成对照。

最后，`generate` 块根据参数 `REGISTER_OUTPUTS` 在两种实现间二选一：

[edge_detect.sv:L72-L98](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect.sv#L72-L98) —— `REGISTER_OUTPUTS==0` 走组合输出（零延迟），否则走 `always_ff` 寄存输出（一拍延迟）。这是「同一个模块、两种时序特性」的参数化典型——读者现阶段只要知道 `generate if` 能在编译期选择实现即可，细节会在后续进阶讲义展开。

> 关键对照：`always_ff`（时钟驱动、`<=`、有状态） vs `always_comb`（组合、`=`、无状态）。本仓库里凡是「要记住状态」的逻辑都用前者。

#### 4.3.4 代码实践

**实践目标**：在 `clk_divider` 的 `always_ff` 块里做一行改动，观察复位行为。

**操作步骤**：

1. 在你自己的工作副本里（**不要改动仓库原文件**），把 `clk_divider` 另存为 `clk_divider_play.sv`。
2. 把复位分支改成异步：把 `always_ff @(posedge clk)` 改为 `always_ff @(posedge clk or negedge nrst)`。
3. 在 testbench 里，故意让 `nrst` 在**两个时钟沿之间**拉低再拉高。

**需要观察的现象**：改造前（同步复位），`out` 要等到下一个时钟沿才清零；改造后（异步复位），`nrst` 一拉低 `out` 立刻清零。

**预期结果**：波形上能明显看到「复位是否与时钟对齐」的差别。这正是 `clk_divider`（同步）与 `edge_detect`（异步）在设计选择上的不同。

> 待本地验证：请在仿真器中实际跑出波形确认上述时序差。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `always_ff` 块内统一用非阻塞赋值 `<=`，而 `always_comb` 用阻塞赋值 `=`？

**参考答案**：时序逻辑里多个寄存器应「同时」更新，非阻塞赋值 `<=` 在块结束时统一生效，符合触发器行为，能保证仿真和综合一致。组合逻辑描述的是「当前值立刻决定输出」，用阻塞赋值 `=` 更自然。混用容易引入仿真与综合不一致的竞争，所以本仓库严格区分。

**练习 2**：从命名上判断，`nrst` 和 `anrst` 哪个是异步复位？依据是什么？

**参考答案**：`anrst` 是异步复位。依据有两点：① 前缀 `a` 是 `async` 的缩写；② 它的敏感列表写作 `posedge clk or negedge anrst`，把复位下降沿也列入了敏感事件。而 `nrst` 只出现在 `if (~nrst)` 里，敏感列表只有时钟，是同步复位。

---

## 5. 综合实践

把本讲三块知识（四段式结构、参数化端口、`always_ff` 时序逻辑）串起来，自己造一个标准模块。

**任务**：参照 `clk_divider.sv` 的写法，新建一个 `my_toggle.sv`，实现一个 1 位输出、每 `CLK_HZ/2` 个时钟翻转一次的模块，并**补全 INFO 与例化模板注释**。

**为什么是 `CLK_HZ/2`**：输出每 `CLK_HZ/2` 拍翻转一次，那么一个完整周期（高电平 + 低电平）需要 \(2 \times \text{CLK\_HZ}/2 = \text{CLK\_HZ}\) 拍。当时钟频率为 \(f_{\text{clk}} = \text{CLK\_HZ}\) 时，输出频率为

\[
f_{\text{out}} = \frac{f_{\text{clk}}}{\text{CLK\_HZ}} = \frac{\text{CLK\_HZ}}{\text{CLK\_HZ}} = 1 \text{ Hz}
\]

也就是说，当 `CLK_HZ = 50_000_000`（50 MHz）时，输出正好每秒翻转一次——典型的「LED 心跳灯」。

**操作步骤**：

1. 新建文件 `my_toggle.sv`，严格按四段式填写：头注释 → INFO → 例化模板 → module。
2. 参数 `CLK_HZ` 默认值设为 `50_000_000`。
3. 内部用一个计数器数到 `CLK_HZ/2 - 1`，到顶后清零并翻转输出。
4. 端口参考 `clk_divider`：`clk`、`nrst`、`ena`、`toggle_out`。

**参考实现（示例代码，非仓库原有文件）**：

```systemverilog
//------------------------------------------------------------------------------
// my_toggle.sv
// mimics the style of https://github.com/pConst/basic_verilog
//------------------------------------------------------------------------------

// INFO ------------------------------------------------------------------------
//  Toggles a 1-bit output every CLK_HZ/2 clock cycles,
//  producing a 1 Hz square wave when the clock really runs at CLK_HZ Hz.
//  Typical use: a "heartbeat" LED.
//

/* --- INSTANTIATION TEMPLATE BEGIN ---

my_toggle #(
  .CLK_HZ( 50_000_000 )
) TG1 (
  .clk( clk ),
  .nrst( 1'b1 ),
  .ena( 1'b1 ),
  .toggle_out(  )
);

--- INSTANTIATION TEMPLATE END ---*/


module my_toggle #( parameter
  CLK_HZ = 50_000_000            // 输入时钟频率 (Hz)
)(
  input  logic clk,
  input  logic nrst,
  input  logic ena,
  output logic toggle_out = 1'b0
);

  localparam logic [$clog2(CLK_HZ/2)-1:0] HALF = (CLK_HZ/2) - 1;  // 计数上限
  logic [$clog2(CLK_HZ/2)-1:0] cnt = '0;                          // 计数器

  always_ff @(posedge clk) begin
    if ( ~nrst ) begin
      cnt        <= '0;
      toggle_out <= 1'b0;
    end else if (ena) begin
      if ( cnt == HALF ) begin
        cnt        <= '0;
        toggle_out <= ~toggle_out;     // 到顶则翻转
      end else begin
        cnt <= cnt + 1'b1;
      end
    end
  end

endmodule
```

**需要观察的现象**：

1. 文件四段是否齐全、顺序是否正确；
2. 参数 `CLK_HZ` 是否同时出现在例化模板和 module 声明里；
3. `always_ff` 是否先写复位分支、再用 `<=` 赋值。

**预期结果**：把 `.CLK_HZ( 50_000_000 )` 仿真 1 秒（5 千万拍），`toggle_out` 应翻转 2 次（即完成 1 个周期）。把 `.CLK_HZ( 10 )` 设小，便于在波形里肉眼数出「每 5 拍翻转一次」。

> 待本地验证：上述计数宽度 `$clog2(CLK_HZ/2)` 与翻转周期需在仿真器中实测确认；不同工具对 `localparam` 参与位宽计算的支持略有差异，遇到问题可先把位宽写死成常量排查。

## 6. 本讲小结

- basic_verilog 的 `.sv` 文件几乎都遵循**四段式**：头注释（名片）→ INFO（功能说明）→ 例化模板（可复制用法）→ module 实现。
- **例化模板**用块注释包住，既是文档又不会引发重复定义；列出所有端口（哪怕留空）是好习惯。
- **参数化端口** `#(parameter ...)` 让一份代码适配多种位宽，是本仓库「高度复用」的根基；参数可以带类型（如 `bit [7:0]`）和注释。
- `always_ff @(posedge clk)` 写时序逻辑，统一用非阻塞赋值 `<=`；**同步复位**只有时钟在敏感列表（`nrst`），**异步复位**额外含 `negedge anrst`。
- `always_comb` 写组合逻辑，用阻塞赋值 `=`，与 `always_ff` 形成清晰分工。
- 读模块的推荐顺序：头注释 → INFO → 例化模板 → module 实现，先会用、再懂原理。

## 7. 下一步学习建议

- **横向巩固**：用本讲的四段式框架去读 [delay.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv) 和 [cdc_data.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_data.sv)，确认你能迅速定位它们的四段、参数和 `always_ff` 块。
- **进入下一讲**：u1-l3「用仿真器跑起来」会教你如何用 `main_tb.sv` 和 `sim_clk_gen.sv` 给这些模块搭测试平台，并把本讲的模块真正在波形里跑起来。
- **后续主线**：当你能熟练阅读四段式结构后，u2 单元将逐个深入 `clk_divider`、`edge_detect` 等绿圈模块的算法细节——届时 `always_ff` 与参数化会成为你最趁手的阅读工具。
