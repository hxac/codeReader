# 平方根与 DSP 乘法

## 1. 本讲目标

本讲是 Unit 7（FPGA 数学运算）的第三篇，承接 [u7-l1 数的表示与定点数](u7-l1-number-representation.md) 与 [u7-l2 有符号定点除法器](u7-l2-signed-division.md)。学完本讲，你应当能够：

- 说清楚**逐位迭代求根**（digit-recurrence square root）的数学原理：为什么每次处理被开方数的 2 个比特、为什么试减项是 \(4q+1\)。
- 读懂 projf 的整数平方根 `sqrt_int.sv` 与定点平方根 `sqrt.sv`，并解释两者**只在迭代次数公式上差一行**的原因。
- 读懂 `mul.sv` 的 `IDLE/CALC/TRUNC/ROUND` 四状态机，理解有符号定点乘法如何截位、做高斯舍入（round half to even）与溢出检测。
- 解释为什么 Verilog 里一句 `a * b`（有符号）会被综合工具自动映射到 Xilinx 的 **DSP48** 硬核，而不是用 LUT 拼出一个乘法器。
- 为 `sqrt_int.sv` 写一个能自检的 testbench，并用综合报告确认 `mul.sv` 走的是 DSP 通路。

## 2. 前置知识

本讲默认你已经掌握 u7-l1 与 u7-l2 的内容。这里快速回顾几个会反复用到的概念：

- **二进制补码与符号扩展**：同一个比特串在 `unsigned` 和 `signed` 下数值不同；有符号数扩展靠复制符号位。
- **定点数 Q 格式**：用 `WIDTH`（总位宽）与 `FBITS`（小数位）约定，整数位 `IBITS = WIDTH - FBITS`（含符号位）。小数点只是设计者心中的隐含位置，底层全是整数运算。
- **乘法后小数位翻倍**：两个 `FBITS` 小数位的定点数相乘，乘积有 `2*FBITS` 个小数位，必须右移截回原格式；u7-l2 的除法器用「高斯舍入」消除统计偏差，本讲的乘法器沿用同一套舍入思想。
- **多周期状态机**：除法、开方这类「逐位产生结果」的运算不能一拍算完，必须做成多周期状态机（u7-l2 的 `div.sv` 已经见过 `IDLE/INIT/CALC/ROUND/SIGN`）。
- **DSP48**：Xilinx 7 系列 FPGA 内置的硬核乘加单元，含一个 \(18\times25\)（部分模式 \(18\times18\)）有符号乘法器与累加器。用 LUT 拼乘法器又慢又费资源，所以工程上几乎总是让乘法走 DSP。

一个总纲性的认知：**除法、开方是「迭代型」运算（必须多周期），乘法是「组合型」运算（一拍可完成，但通常打一拍寄存以改善时序）**。本讲的主角恰好各占一类。

## 3. 本讲源码地图

本讲全部源码都在 projf 数学库 `ThreePart/projf-explore/lib/maths/` 下：

| 文件 | 作用 | 行数 |
| --- | --- | --- |
| [sqrt_int.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt_int.sv) | 整数平方根（逐位迭代求根） | 59 行 |
| [sqrt.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt.sv) | 定点平方根（与 `sqrt_int` 同构，仅迭代次数不同） | 62 行 |
| [mul.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/mul.sv) | 有符号定点乘法 + 高斯舍入 + 溢出检测 | 105 行 |
| [README.md](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/README.md) | 数学库总览，列出所有模块、测试与配套博客 | 58 行 |

配套的验证资产（实践环节会用到）：

| 文件 | 作用 |
| --- | --- |
| [xc7/sqrt_int_tb.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/xc7/sqrt_int_tb.sv) | 整数平方根的 Vivado testbench，用 `$monitor` 打印 6 组激励 |
| [xc7/sqrt_tb.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/xc7/sqrt_tb.sv) | 定点平方根 testbench（WIDTH=16, FBITS=8） |
| [test/mul.py](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/test/mul.py) | 乘法器的 cocotb 测试，含舍入、溢出等用例 |
| [test/mul.mk](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/test/mul.mk) | 乘法器 cocotb 的 Makefile（设 WIDTH=9, FBITS=4） |

> 提示：projf 的 `README.md` 明确说开方原理详见博客 [Square Root](https://projectf.io/posts/square-root-in-verilog/)，乘法/除法的配套博客是 [Verilog Library](https://projectf.io/verilog-lib/)。本讲只基于仓库内真实源码讲解，博客作为延伸阅读。

---

## 4. 核心概念与源码讲解

### 4.1 逐位迭代求根：整数平方根 sqrt_int

#### 4.1.1 概念说明

开方在硬件里为什么必须做成多周期？因为平方根**没有友好的组合电路**——不像加法一拍就能算完。开方本质上和除法一样，是一个「逐位确定结果」的过程：结果的每一位都要靠一次试减（trial subtraction）来判断是 0 还是 1，所以它必然是一个迭代型状态机。

回忆十进制下的「笔算开方」（schoolbook square root）：把被开方数从右往左**两位一组**分组，然后像长除法一样一组一组地处理。二进制下完全一样，只是「两位一组」变成「**两比特一组**」——这是因为给一个数补两个低位 0 等于乘以 4，而 \((2q)^2 = 4q^2\)，根翻倍则平方翻 4 倍，正好对上。这就是「每次处理 2 个比特」的数学根源。

核心代数恒等式（理解整个算法的关键）：

\[
(2q+1)^2 = 4q^2 + 4q + 1
\]

设当前已经确定的部分根是 \(q\)（已经处理的比特），剩余的「部分余数」为 \(r\)。当我们再吃进 2 个被开方数比特后，新的待试余数变成 \(4r\)（左移 2 位）。要判断下一位根能否取 1（即新根变成 \(2q+1\)），就看：

\[
4r - (4q + 1) \;\ge\; 0 \;?
\]

- 若 \(\ge 0\)：下一位根取 **1**，新部分根为 \(2q+1\)，新余数更新为 \(4r-(4q+1)\)。
- 若 \(< 0\)：下一位根取 **0**，新部分根为 \(2q\)，余数不变（不试减成功就不动）。

其中那个 \((4q+1)\) 在硬件里就是一个位拼接：`{q, 2'b01}`（把 \(q\) 左移 2 位、末尾补 01，数值上就是 \(4q+1\)）。这就是 `sqrt_int.sv` 里 `ac - {q, 2'b01}` 这一句的全部含义。

由于 WIDTH 位的被开方数最多能开出一个 WIDTH/2 位的根（因为 \((2^{WIDTH/2})^2 = 2^{WIDTH}\)），所以迭代次数恰为：

\[
\text{ITER} = \text{WIDTH} \div 2
\]

#### 4.1.2 核心流程

`sqrt_int` 是一个不带显式 `enum` 状态机的「隐式两态」设计：靠 `start`/`busy`/`valid` 三个握手信号驱动。流程如下：

1. **启动（`start` 拉高一拍）**：把被开方数 `rad` 装进一个宽移位寄存器 `{ac, x}`，清零部分根 `q` 与计数器 `i`，置 `busy=1`。
2. **迭代（每拍一次，共 ITER 次）**：
   - 组合逻辑计算试减结果 `test_res = ac - {q, 2'b01}`；
   - 看 `test_res` 的最高位（符号位）判断够不够减；
   - 够减：更新余数、根末位上 1；
   - 不够减：余数不变、根末位上 0；
   - 同时 `{ac, x}` 整体左移 2 位，把 `x` 里的下两个被开方数比特「拉下来」喂给 `ac`。
3. **结束（`i == ITER-1`）**：`busy=0`、`valid=1`，输出最终 `root` 与 `rem`（余数，需撤销最后一次左移）。

一次完整计算的拍数 = 启动 1 拍 + ITER 拍迭代（末拍同时出结果）。对于默认 WIDTH=8，ITER=4，约 5 拍出结果。

#### 4.1.3 源码精读

先看端口与参数。模块只有一个参数 `WIDTH`（被开方数位宽），输入 `clk`/`start`/`rad`，输出 `busy`/`valid`/`root`/`rem`——典型的「启动-忙-有效」三握手多周期接口：

[sqrt_int.sv:8-16](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt_int.sv#L8-L16) —— `sqrt_int` 的端口与参数声明。注意没有 `rst` 复位端口，复位靠 `start` 重新装初值完成。

接着是工作寄存器。这里有三个关键寄存器，理解它们的位宽与拼法是读懂算法的核心：

[sqrt_int.sv:18-24](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt_int.sv#L18-L24) —— 工作寄存器声明。要点：

- `x`：被开方数的副本（WIDTH 位），每拍左移 2 位，负责把比特源源不断「喂」给累加器。
- `q`：正在逐位生长的部分根（WIDTH 位，实际只用低 ITER 位）。
- `ac`：累加器/部分余数，**比 WIDTH 宽 2 位**（`WIDTH+2`），用来容纳试减时的中间结果与符号位判断。
- `test_res`：试减的纯组合结果（`WIDTH+2` 位），看它的最高位 `test_res[WIDTH+1]` 判断正负。
- `ITER = WIDTH >> 1`：迭代次数恰好是位宽的一半。

核心的试减与根生长逻辑在 `always_comb` 里，只有短短 9 行，但浓缩了 4.1.1 的全部数学：

[sqrt_int.sv:26-35](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt_int.sv#L26-L35) —— 试减与根生长的组合逻辑。逐句对照：

- `test_res = ac - {q, 2'b01};` —— 计算 \(4r-(4q+1)\)。`{q, 2'b01}` 就是位拼接的 \(4q+1\)。
- `if (test_res[WIDTH+1] == 0)` —— 检查最高位（符号位）是否为 0，即结果是否 \(\ge 0\)。
- 够减分支：`{ac_next, x_next} = {test_res[WIDTH-1:0], x, 2'b0};` —— 新余数取试减结果，`{ac, x}` 整体左移 2 位（把 `x` 的高 2 位挤进 `ac`，`x` 低位补 0）；`q_next = {q[WIDTH-2:0], 1'b1};` —— 根末位上 1（等价于 \(q \leftarrow 2q+1\)）。
- 不够减分支：余数取旧的 `ac`（不试减），根 `q_next = q << 1;` —— 末位上 0（\(q \leftarrow 2q\)）。

时序部分用 `always_ff` 维护状态与寄存器更新：

[sqrt_int.sv:37-43](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt_int.sv#L37-L43) —— `start` 装初值。关键是其中的 `{ac, x} <= {{WIDTH{1'b0}}, rad, 2'b0};`：把 `rad` 放进移位寄存器的中段，`ac` 高位补 0、`x` 低位补 0，并预先把 `rad` 的最高 2 比特送入 `ac` 的低位，为第一次试减做好准备。

[sqrt_int.sv:44-55](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt_int.sv#L44-L55) —— 迭代主循环。当 `i == ITER-1`（最后一拍）时：`busy<=0; valid<=1;`，把组合逻辑算出的 `q_next` 锁存为最终 `root`，并把 `ac_next[WIDTH+1:2]` 作为 `rem`——这里 `[WIDTH+1:2]` 右舍 2 位正是注释所说的「undo final shift」（撤销最后一次左移 2 位，还原真实余数）。

**一个具体数字（来自自带 testbench）**：取 `rad = 8'b01011010 = 90`。手算可知 \(9^2=81 \le 90 < 100=10^2\)，所以 `root=9`、`rem=90-81=9`。第一次试减时 `ac` 初值为 1（`rad` 最高 2 比特 `01`），`{q,01}=1`，`test_res=0 \ge 0`，故根的最高位上 1——与「根=9=1001₂，最高位为 1」一致。其余 3 次迭代同理逐位确定剩下的 `001`，最终得到 `root=9, rem=9`。这个结果可直接用 [xc7/sqrt_int_tb.sv:48-50](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/xc7/sqrt_int_tb.sv#L48-L50) 仿真验证。

#### 4.1.4 代码实践

> **实践目标**：为 `sqrt_int.sv` 写一个**自检 testbench**，对若干输入求整数平方根并自动断言结果正确。

仓库自带的 [xc7/sqrt_int_tb.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/xc7/sqrt_int_tb.sv) 只用 `$monitor` 打印结果，**不自动判对错**（要靠人眼看波形/日志）。我们改进它，加入 `$error` 断言。

**操作步骤**：

1. 复制 `xc7/sqrt_int_tb.sv` 为一份新文件（例如 `sqrt_int_selfcheck_tb.sv`，放在你自己的工作目录，**不要改动 projf 源码**）。
2. 把每个激励改成「施加 `start` → 等待 `valid` → 检查 `root` 与 `rem`」的子任务调用，并用黄金期望值断言。
3. 期望值提前用任何语言算好（也可心算）：\(\text{root}=\lfloor\sqrt{\text{rad}}\rfloor\)，\(\text{rem}=\text{rad}-\text{root}^2\)。

下面是**示例代码**（不是 projf 原有文件，仅作参考骨架，参数 WIDTH=8）：

```verilog
// 示例代码：sqrt_int 的自检 testbench 骨架
task automatic do_sqrt(input [7:0] rad_in, input [7:0] exp_root, input [7:0] exp_rem);
    rad = rad_in; start = 1;
    @(posedge clk); start = 0;        // start 仅拉高一拍
    wait (valid == 1);                 // 等待计算完成
    @(posedge clk);                    // 让 valid 稳定采样
    if (root !== exp_root || rem !== exp_rem)
        $error("sqrt(%0d): got root=%0d rem=%0d, expected root=%0d rem=%0d",
               rad_in, root, rem, exp_root, exp_rem);
    else
        $display("sqrt(%0d) = %0d rem %0d  [OK]", rad_in, root, rem);
endtask

// 在 initial 块里依次调用（期望值已手算）：
// do_sqrt(8'd0,   8'd0,  8'd0);   // sqrt(0)=0
// do_sqrt(8'd1,   8'd1,  8'd0);   // sqrt(1)=1
// do_sqrt(8'd90,  8'd9,  8'd9);   // 9^2=81, rem=9
// do_sqrt(8'd81,  8'd9,  8'd0);   // 完全平方
// do_sqrt(8'd121, 8'd11, 8'd0);   // 11^2=121
// do_sqrt(8'd255, 8'd15, 8'd30);  // 15^2=225, rem=30
```

**需要观察的现象**：每完成一次开方，`busy` 在计算期间为 1、`valid` 在末拍拉高；日志应打印 6 行 `[OK]`。

**预期结果**：对 WIDTH=8，每次计算约 5 个时钟周期（1 拍启动 + 4 拍迭代）；上表 6 组全部通过。

**如果你无法本地运行仿真**：明确标注「待本地验证」。你也可以退而用 Verilator 或 Icarus Verilog 命令行跑：`iverilog -g2012 -o sim sqrt_int.sv sqrt_int_selfcheck_tb.sv && vvp sim`（`-g2012` 开启 SystemVerilog 子集，因为源码用了 `logic`/`always_comb`/`always_ff`）。

#### 4.1.5 小练习与答案

**练习 1**：把 `WIDTH` 从 8 改成 16，迭代次数 `ITER` 变成多少？一次计算大约要多少拍？

> **答案**：`ITER = 16 >> 1 = 8`，约 1+8=9 拍出结果。根最多 8 位（\(\sqrt{65535}\approx255.99\)，最大根 255）。

**练习 2**：试减项为什么是 `{q, 2'b01}`（即 \(4q+1\)）而不是 `{q, 2'b11}`（即 \(4q+3\)）？用 4.1.1 的恒等式说明。

> **答案**：由 \((2q+1)^2 = 4q^2 + 4q + 1\)，当部分根从 \(q\) 变成 \(2q+1\) 时，平方值增加了 \(4q+1\)。已处理部分贡献了 \(q^2\)，故新增的待减量正是 \(4q+1\)，对应位拼接 `{q, 2'b01}`。`4q+3` 不对应任何一步的代数增量。

**练习 3**：`rem <= ac_next[WIDTH+1:2];` 为什么要从第 2 位开始截取（而不是从第 0 位）？

> **答案**：最后一拍 `always_comb` 仍对 `{ac, x}` 做了一次左移 2 位，使 `ac_next` 里的余数被放大了 4 倍。截取 `[WIDTH+1:2]`（丢弃最低 2 位）等于除以 4，即注释「undo final shift」，还原真实余数。

---

### 4.2 定点平方根 sqrt：同构不同参

#### 4.2.1 概念说明

`sqrt.sv` 处理的是**定点数**开方。被开方数是 WIDTH 位、含 FBITS 个小数位的定点数，真实数值为 \(\text{rad}/2^{\text{FBITS}}\)。问题是：定点开方后，小数位会「减半」——\(\sqrt{x}\) 把数值的尺度开方，小数位本应变成 FBITS/2。

projf 的做法是：保持输出仍是 WIDTH 位、FBITS 个小数位的定点格式，那么输出 `root` 表示的数值应满足：

\[
\left(\frac{\text{root}}{2^{\text{FBITS}}}\right)^2 = \frac{\text{rad}}{2^{\text{FBITS}}}
\quad\Longrightarrow\quad
\text{root}^2 = \text{rad} \cdot 2^{\text{FBITS}}
\]

也就是说，硬件实际要算的是整数 \(\text{rad}\cdot 2^{\text{FBITS}}\) 的平方根。这个「放大 \(2^{\text{FBITS}}\)」在代码里**不靠额外乘法实现**，而是靠「多跑 FBITS/2 次迭代」：因为移位寄存器 `x` 只有 WIDTH 位，当真实的 `rad` 比特被消耗完后，后续迭代会继续从 `x` 拉下 0——这等价于在被开方数末尾补 FBITS 个 0，即乘以 \(2^{\text{FBITS}}\)。于是迭代次数变成：

\[
\text{ITER} = (\text{WIDTH} + \text{FBITS}) \div 2
\]

#### 4.2.2 核心流程

与 `sqrt_int` **完全相同的试减/根生长/移位流程**，唯一区别是 `ITER` 公式不同。这正呼应了 u6-l1 讲显示时序时见过的「**同构不同参**」设计模式：一份算法骨架、靠参数差异化适配多个场景。

#### 4.2.3 源码精读

端口多了一个 `FBITS` 参数：

[sqrt.sv:8-19](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt.sv#L8-L19) —— `sqrt` 的端口声明，新增 `parameter FBITS=0`。

**全文唯一与 `sqrt_int` 不同的一行**——迭代次数公式：

[sqrt.sv:26](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt.sv#L26) —— `localparam ITER = (WIDTH+FBITS) >> 1;`。对比 [sqrt_int.sv:23](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt_int.sv#L23) 的 `ITER = WIDTH >> 1;`，这正是「定点版多跑 FBITS/2 次迭代」的落点。

除此之外，[sqrt.sv:29-38](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt.sv#L29-L38) 的 `always_comb` 与 [sqrt.sv:40-60](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt.sv#L40-L60) 的 `always_ff` 与 `sqrt_int` 逐字相同——读者可自行 diff 对照。

**一个具体数字（来自自带 testbench）**：取 WIDTH=16、FBITS=8（Q8.8 格式），`rad = 16'b0000_0010_0000_0000 = 512`，表示定点值 \(512/256 = 2.0\)。期望 \(\sqrt{2.0}\approx 1.4142\)。按 4.2.1，硬件算的是 \(\sqrt{512\cdot 256}=\sqrt{131072}\approx 362.0\)（\(362^2=131044 \le 131072 < 363^2=131769\)）。输出 `root=362`，还原成 Q8.8 为 \(362/256\approx 1.414\)，与期望一致。可用 [xc7/sqrt_tb.sv:43-45](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/xc7/sqrt_tb.sv#L43-L45) 仿真验证（该 testbench 用 `SF = 2.0**-8.0` 把定点值换算回浮点打印）。

#### 4.2.4 代码实践

> **实践目标**：对比 `sqrt_int` 与 `sqrt` 在**同一被开方数比特串**下的输出差异，体会 FBITS 的作用。

**操作步骤**：

1. 同时打开 [sqrt_int_tb.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/xc7/sqrt_int_tb.sv)（WIDTH=8）与 [sqrt_tb.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/xc7/sqrt_tb.sv)（WIDTH=16,FBITS=8）。
2. 跟踪 `rad = 16'b0000_0010_0000_0000`（定点 2.0）在 `sqrt_tb` 里的输出，确认 `root` 还原后约为 1.414。
3. 回答：如果把同一个比特串 `0000_0010_0000_0000`（=512）直接喂给 `sqrt_int`（WIDTH=16），`root` 会是多少？为什么与定点版不同？

**需要观察的现象**：定点版 `sqrt` 输出 362（Q8.8 ≈ 1.414）；整数版 `sqrt_int` 对 512 开方得 22（\(22^2=484\le512<529=23^2\)）。

**预期结果**：两者数值不同，因为定点版隐式算了 \(\sqrt{512\cdot 256}\)，整数版算的是 \(\sqrt{512}\)。这正是 FBITS 通过「多迭代 = 末尾补零 = 乘 \(2^{\text{FBITS}}\)」实现的尺度变换。

#### 4.2.5 小练习与答案

**练习 1**：`sqrt` 默认 `FBITS=0`，此时它与 `sqrt_int` 行为是否一致？

> **答案**：一致。`FBITS=0` 时 `ITER=(WIDTH+0)>>1=WIDTH>>1`，与 `sqrt_int` 公式相同，且其余逻辑逐字相同，故两者等价。

**练习 2**：为什么定点开方后，结果的「小数位」本应是 FBITS/2，而 projf 却能保持 FBITS 位？

> **答案**：因为硬件实际算的是 \(\sqrt{\text{rad}\cdot 2^{\text{FBITS}}}\)（通过多跑 FBITS/2 次迭代隐式实现）。结果整数 \(=\lfloor\sqrt{\text{rad}\cdot 2^{\text{FBITS}}}\rfloor\)，当它被放回 WIDTH 位、FBITS 小数位的格式时，恰好恢复了 FBITS 个小数位的精度。

---

### 4.3 有符号定点乘法 mul 与高斯舍入

#### 4.3.1 概念说明

`mul.sv` 是 projf 数学库里**唯一一个「组合型」核心运算**——乘法本身一拍就能算完（`a * b`），但模块仍做成了多周期状态机。原因不是乘法慢，而是要**在乘法周围包装截位、舍入、溢出检测**，并把这条长组合链拆成几拍以改善时序，同时把结果寄存输出。

回顾 u7-l1：两个 WIDTH 位、FBITS 小数位的有符号定点数相乘：

- 乘积位宽翻倍：\(2\times\text{WIDTH}\) 位；
- 小数位翻倍：\(2\times\text{FBITS}\) 位；
- 必须截回 WIDTH 位、FBITS 小数位——这既要「截掉多余的小数位」（取中间的一段窗口），又要决定如何处理被截掉的低位（舍入）。

projf 用的是**高斯舍入（round half to even，向偶数舍入）**：当被截部分恰好是 0.5 时，舍入到最近的**偶数**，从而消除「总是向上舍入」带来的统计偏差（u7-l2 的除法器也用同一策略）。例如 2.5 舍入到 2、3.5 舍入到 4。

#### 4.3.2 核心流程

四状态机 `IDLE → CALC → TRUNC → ROUND`：

1. **IDLE**：等 `start`。来了之后寄存输入 `a1<=a; b1<=b;`、记录两输入符号差 `sig_diff`、置 `busy=1`，进入 CALC。
2. **CALC**：算全宽乘积 `prod <= a1 * b1;`（\(2\times\text{WIDTH}\) 位有符号），进入 TRUNC。这一拍是把乘法单独隔离，让综合工具能干净地把它映射成一个 DSP。
3. **TRUNC**：从 `prod` 里截出 WIDTH 位的 `prod_t`（截位窗口见 4.3.3），同时抽出舍入判定所需的比特：舍入位 `round`、偶数判定 `even`、被截低位 `rbits`，进入 ROUND。
4. **ROUND**：做高斯舍入得到最终 `val`，做溢出检测决定 `valid`/`ovf`，置 `done=1`（仅高一拍）、`busy=0`，回 IDLE。

一次乘法共 3 拍计算（CALC/TRUNC/ROUND）+ 启动过渡。

#### 4.3.3 源码精读

端口是标准的「启动-忙-完成-有效-溢出」握手：

[mul.sv:8-22](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/mul.sv#L8-L22) —— `mul` 端口声明。注意 `a`/`b`/`val` 都标了 `signed`（承接 u7-l1：projf 把端口统一标 signed 以保证符号语义不被混用破坏），且有 `rst` 同步复位与 `ovf` 溢出标志。

截位窗口的三个常量是理解 TRUNC 的钥匙：

[mul.sv:24-30](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/mul.sv#L24-L30) —— 截位与舍入常量。其中 `IBITS = WIDTH - FBITS`（含符号位的整数位，定义见 u7-l1）。由乘积小数位翻倍、需丢掉 FBITS 位可知：

- `LSB = WIDTH - IBITS = FBITS`：截位窗口的下界，正好丢掉多余的 FBITS 个小数位；
- `MSB = 2*WIDTH - IBITS - 1`：上界；
- 窗口宽度 \(= \text{MSB}-\text{LSB}+1 = \text{WIDTH}\)：截出的 `prod_t` 恰为 WIDTH 位，格式回到 Q(IBITS.FBITS)。
- `HALF = {1'b1, {FBITS-1{1'b0}}}`：FBITS 位、值为 \(2^{\text{FBITS}-1}\)，即定点小数里的「恰好 0.5」。

CALC 阶段，**全文最关键的一行**：

[mul.sv:45-48](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/mul.sv#L45-L48) —— CALC 状态。`prod <= a1 * b1;` 是一句有符号乘法，综合后会被映射到 DSP48（详见 4.4）。它被单独放在一个状态、单独打一拍寄存，是良好的「让乘法成为一颗 DSP」的写法。

TRUNC 阶段把乘积切片：

[mul.sv:49-56](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/mul.sv#L49-L56) —— TRUNC 状态。要点：

- `prod_t <= prod[MSB:LSB];` —— WIDTH 位截位结果；
- `rbits <= prod[FBITS-1:0];` —— 被丢掉的 FBITS 位（用来判断「是否恰好半值」）；
- `round <= prod[FBITS-+:1];` —— 即 `prod[FBITS-1]`，紧贴截位窗口下界的那一位，是「0.5 判定线」；
- `even <= ~prod[FBITS+:1];` —— 即 `~prod[FBITS]`，截位结果的最低位取反；当 `prod[FBITS]==0` 时 `even=1`，表示截位结果是偶数。

ROUND 阶段做高斯舍入：

[mul.sv:57-64](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/mul.sv#L57-L64) —— ROUND 状态的舍入。`val <= (round && !(even && rbits == HALF)) ? prod_t + 1 : prod_t;` 读作：

- 若「舍入位为 0」→ 不进位，直接取 `prod_t`；
- 若「舍入位为 1」但**不是**「恰好半值且截位结果为偶」→ 进位 `prod_t+1`（四舍五入的常规情形）；
- 若「恰好半值（`rbits==HALF`）且截位结果为偶（`even`）」→ **不进位**（向偶数靠，这就是 round half to even）。

同一拍还做溢出检测：

[mul.sv:66-74](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/mul.sv#L66-L74) —— 溢出检测。两个条件同时满足才算「未溢出、结果有效」：

1. `sig_diff == prod_t[WIDTH-1+:1]` —— 输入符号差（异或得到的期望积符号）等于实际结果符号位；若不符，说明乘积连符号都翻了，必然溢出；
2. `prod[2*WIDTH-1:MSB+1] == '0 || prod[2*WIDTH-1:MSB+1] == '1` —— 截位窗口**之上**的高位必须「全 0 或全 1」（即正确的符号扩展）；若既有 0 又有 1，说明有效位被挤出了 WIDTH 位窗口。

满足则 `valid=1, ovf=0`；否则 `valid=0, ovf=1`（如自带 cocotb 测试里 `8*8`、`5*4`、`-7*3` 这三组期望溢出，见 [test/mul.py:211-300](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/test/mul.py#L211-L300)）。

IDLE 启动与符号差寄存：

[mul.sv:76-85](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/mul.sv#L76-L85) —— IDLE 状态。`sig_diff <= (a[WIDTH-1+:1] ^ b[WIDTH-1+:1]);` 记录两输入符号异或。注意 `sig_diff` **只用于溢出检测的符号校验**，并不参与乘法运算——因为 `a1 * b1` 两边都是 `signed`，乘积的符号天然正确（见 4.4.3）。

末尾的 `ifdef COCOTB_SIM` 块（[mul.sv:98-103](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/mul.sv#L98-L103)）是 projf 数学库统一的「在 cocotb 下导出 VCD 波形」开关，承接 [README.md:29-39](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/README.md#L29-L39) 的说明。

#### 4.3.4 代码实践

> **实践目标**：用仓库自带的 cocotb 测试套件跑 `mul`，观察高斯舍入与溢出行为。

**操作步骤**：

1. 确认本机有 Python、cocotb、Icarus Verilog 与 `FixedPoint`（`pip install cocotb spfpm`，`apt install iverilog`）。
2. 进入 `ThreePart/projf-explore/lib/maths/test/`，执行 `make -f mul.mk`。Makefile 见 [test/mul.mk](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/test/mul.mk#L14)，它把 WIDTH 设为 9、FBITS 设为 4（`COMPILE_ARGS += -Pmul.WIDTH=9 -Pmul.FBITS=4`）。
3. 阅读测试用例 [test/mul.py](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/test/mul.py)，重点关注 `round_1`~`round_4`（2.5/3.5/4.5/5.5 × 2.0625，验证向偶舍入）与 `ovf_1`~`ovf_3`（验证溢出）。
4. 对照 `test_dut_multiply`（[test/mul.py:24-64](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/test/mul.py#L24-L64)）：它用 Python `FXnum` 做黄金参考模型，断言 `val == model_c`，并检查 `busy/done/valid/ovf` 四个握手信号。

**需要观察的现象**：`round_1`（2.5×2.0625）与 `round_2`（3.5×2.0625）的乘积真值都落在半值附近，DUT 会按「向偶」选择不同方向；`nonbin_4`（3.6×0.6）等用例标了 `expect_fail=True`，因为非二进制有理数无法精确表示，DUT 与模型可能选到真值两侧（这是已知的设计权衡，不是 bug）；`ovf_1`（8×8）等会触发 `valid=0, ovf=1`。

**预期结果**：`make -f mul.mk` 跑完，除明确标注 `expect_fail=True` 的用例外全部通过；溢出用例的 `ovf` 被断言为 1。若无法本地运行，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：在 WIDTH=9、FBITS=4 下，`a=2.5`、`b=2.0625`，乘积真值为 5.15625。截位后 `prod_t` 对应的定点值落在哪两个可表示值之间？舍入位是否为 1？最终进位方向由什么决定？

> **答案**：\(2.5\times2.0625=5.15625\)。FBITS=4 时定点分辨率 \(1/16=0.0625\)。5.15625 落在 \(5.125=81/16\) 与 \(5.1875=83/16\) 之间，且恰为两者中点（半值）。此时舍入位 `round=1`、被截低位 `rbits==HALF`（恰好半值），故是否进位由「向偶」规则决定（截位结果 81 为奇，则向偶数方向进位到 83 这一偶数值）。具体方向以仿真为准——这正是 `round_1` 用例要验证的半值行为。

**练习 2**：为什么 `sig_diff` 只在溢出检测里用，而不参与 `a1*b1` 的计算？

> **答案**：`a1`、`b1` 都声明为 `signed`，Verilog 的 `*` 对两个有符号数会直接产生正确符号的乘积，无需额外处理符号。`sig_diff` 只是「输入符号的异或」，作为溢出检测里的一道独立符号校验（期望符号 vs 实际结果符号），用于捕捉「乘积太大导致连符号位都被挤翻」的极端溢出。

**练习 3**：截位窗口的高位「全 0 或全 1」为什么能作为「未溢出」的判据？

> **答案**：截位窗口之上的高位理应是 `prod_t` 符号位的符号扩展（正数全 0、负数全 1）。一旦这些高位「既有 0 又有 1」，说明有效数值已经越过 WIDTH 位窗口的上界、侵入了符号扩展区，即发生溢出。

---

### 4.4 DSP 单元映射与符号正确性

#### 4.4.1 概念说明

为什么 FPGA 工程师如此在意「乘法走 DSP」？因为用 LUT（查找表）拼一个乘法器极其昂贵——一个 16×16 乘法器要消耗数百个 LUT 与进位链，且时序差；而 7 系列 FPGA 每个 DSP48E1 slice 内置一个 \(18\times25\) 位有符号乘法器 + 累加器，**一颗 DSP 就能算一个乘法**，速度快、功耗低、资源省。因此，让综合工具把乘法识别并映射到 DSP，是数值密集型设计（滤波器、矩阵、Mandelbrot 等）能否落地的关键。

projf 的 `mul.sv` 就是「**用最朴素的 `*` 让工具自己推断 DSP**」的典范：它不例化任何 `DSP48E1` 原语，只写了一句有符号 `a1 * b1`，剩下的交给综合器。这种「行为级描述、结构级映射」的分工，是高密度综合（HLS）与手写 RTL 共同推崇的做法。

#### 4.4.2 核心流程

综合器推断 DSP 的条件大致是：操作数是有符号（或无符号）的固定位宽乘法、位宽不超过 DSP 的 \(18\times25\) 上限。映射过程：

1. 识别 `a1 * b1` 是一个 WIDTH×WIDTH 的乘法；
2. 检查位宽是否落在 DSP48 输入口径内（7 系列单颗 DSP 支持 \(18\times25\)）；
3. 若是，把 `a1`、`b1` 分别接到 DSP 的 A、B 端口，乘积从 P 端口输出；
4. `mul.sv` 把乘积单独打一拍寄存（`prod <= a1 * b1;` 在 CALC 状态），这一拍正好可对应 DSP 内部的输出寄存器（pipelined register），进一步改善时序。

对于符号：DSP48 的乘法器原生支持有符号运算（由输入端口的符号属性决定）。只要 RTL 里两个操作数都是 `signed`，综合器就会配置 DSP 为有符号模式，乘积的最高位就是正确的符号位——这正是 `mul.sv` 无需手工做符号修正的根本原因。

#### 4.4.3 源码精读

回顾那一句被单独成拍的乘法：

[mul.sv:47](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/mul.sv#L47) —— `prod <= a1 * b1;`。`a1`、`b1` 在 [mul.sv:33](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/mul.sv#L33) 声明为 `logic signed [WIDTH-1:0]`，`prod` 在 [mul.sv:35](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/mul.sv#L35) 声明为 `logic signed [2*WIDTH-1:0]`。两侧都 `signed`，所以：

- 乘积位宽自动为 \(2\times\text{WIDTH}\)；
- 符号由硬件乘法器原生保证，无需 `sig_diff` 介入；
- 综合后，这一句被映射为一颗 DSP48（默认 WIDTH=8 时是 \(8\times8\)，cocotb 用例里是 \(9\times9\)，都远在 \(18\times25\) 口径内）。

对比 `sqrt_int`/`sqrt`：开方**没有**对应的「单颗硬核」，所以它必须用 LUT/FF 搭出迭代数据通路——这也是开方比乘法「贵」的根本原因（开方贵在迭代逻辑与多周期控制，乘法贵在乘法本身但被 DSP 免单）。

#### 4.4.4 代码实践

> **实践目标**：用综合报告确认 `mul.sv` 的乘法被映射到 DSP48，而非用 LUT 实现。

**操作步骤**：

1. 在 Vivado 里创建一个最小工程，目标器件任选 7 系列（如 `xc7a35tcpg236-1`，Arty 板的器件）。
2. 加入 [mul.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/mul.sv) 作为设计源（顶层或例化均可，可设默认 WIDTH=8、FBITS=4）。
3. 跑综合（Run Synthesis）。
4. 打开综合后的 **Utilization（资源利用）报告**，找到 `DSPs`（或 `DSP48`）一栏。
5. （可选对照）把 `mul.sv` 复制一份，临时在综合设置里加 `(* use_dsp48 = "no" *)` 属性到 `prod` 的乘法上（或用相关综合选项关闭 DSP 推断），重新综合，对比 LUT 占用变化。

**需要观察的现象**：标准综合下，`DSP48` 利用数应为 **1**（一颗 DSP 承担整个 \(8\times8\) 或 \(9\times9\) 有符号乘法）；LUT/FF 占用很少（主要是状态机与截位/舍入的少量组合逻辑）。对照实验里关闭 DSP 推断后，`DSP48=0` 而 LUT 数显著上升。

**预期结果**：综合报告证实 `mul.sv` 的乘法走 DSP 通路。若你没有 Vivado，可用 Yosys 的 `show` 或读其综合日志中的 `DSP48` 推断信息；无法本地综合时标注「待本地验证」。

> 提示：projf 数学库 [README.md:5-17](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/README.md#L5-L17) 把除法、乘法、开方并列列出，但只有乘法天然走 DSP；除法（u7-l2）与开方（4.1/4.2）都是 LUT 实现的迭代数据通路——这是选型时的关键区别。

#### 4.4.5 小练习与答案

**练习 1**：如果把 WIDTH 设为 32（仍是 \(32\times32\) 有符号乘法），一颗 7 系列 DSP48（\(18\times25\)）还够吗？综合器会怎么做？

> **答案**：不够。\(32\times32\) 超过单颗 DSP 的 \(18\times25\) 口径，综合器会把它分解成多颗 DSP（通常 4 颗 \(18\times18\) 拼出 \(32\times32\)，外加加法组合），资源利用从 1 变成多颗。这解释了为什么高位宽乘法要谨慎评估 DSP 预算。

**练习 2**：`sqrt_int` 为什么不像 `mul` 那样「免费」吃到一颗硬核？

> **答案**：FPGA 没有内置的「开方硬核」，开方是逐位试减的迭代算法，必须用 LUT/FF 搭出移位寄存器、试减比较器与多周期控制逻辑。只有乘法（以及乘加）有 DSP 这种专用硬核可映射。

**练习 3**：`mul.sv` 为什么把 `a1 * b1` 单独放在 CALC 状态打一拍，而不是和截位逻辑合在一拍？

> **答案**：把乘法单独寄存一拍（对应 DSP 内部输出寄存器）有两大好处——(1) 让综合器干净地把乘法识别成一颗 DSP 并使用其流水寄存器，改善时序；(2) 把「乘法」与「截位+舍入+溢出」这条长组合链切成两段，降低关键路径延迟。这是用状态机给组合逻辑「加缓冲」的常见手法。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「**定点向量长度计算器**」的小任务。

**背景**：给定点 \((x,y)\)，求其到原点的距离 \(r=\sqrt{x^2+y^2}\)（即向量的 2-范数）。这正好把「乘法（算 \(x^2\)、\(y^2\)）」与「开方」串成一条流水线，是图形学、信号处理里极常见的运算。

**任务**：

1. 用两个 `mul` 实例分别算 \(x^2\) 与 \(y^2\)（取 WIDTH=16、FBITS=8 的定点格式）。
2. 用一个普通加法器算 \(s = x^2 + y^2\)（注意加法后整数位会进位，需预留位宽或讨论溢出）。
3. 用一个 `sqrt`（WIDTH=16、FBITS=8）对 \(s\) 开方，得到 \(r\)。
4. 写一个顶层 testbench：输入几组已知点（如 \((3,4)\to5\)、\((5,12)\to13\)、\((1,1)\to1.414\)），驱动 `start`，等待 `valid`，断言 \(r\) 与期望值的定点误差在 1 个 LSB 以内。

**需要思考与记录的问题**：

- `mul` 输出是 WIDTH 位，两个平方相加可能需要 WIDTH+1 位才能不溢出——你如何把加宽后的 \(s\) 喂给只有 WIDTH 位输入的 `sqrt`？（提示：可右移截位，或改 `sqrt` 的 WIDTH 参数，并讨论精度损失。）
- 整条流水线的总延迟是多少拍？（两个 `mul` 并行各 3 拍 + 加法 1 拍 + `sqrt` 的 \(1+(\text{WIDTH}+\text{FBITS})/2\) 拍。）
- 这条数据通路里，哪一段走 DSP？哪一段走 LUT？为什么？

**预期结果**：完成顶层连接与 testbench，对 \((3,4)\) 得到约 5.0、对 \((1,1)\) 得到约 1.414（允许 ±1 LSB 误差）。若无法上板/仿真，至少画出实例化框图与每段延迟，并标注「待本地验证」。

> 进阶：projf 的 Mandelbrot demo（u7-l5）正是反复做「定点复数乘法 + 模长比较」的典型场景，可以对照阅读，体会 `mul` 在真实图形算法里的用法。

---

## 6. 本讲小结

- **开方是迭代型运算**：`sqrt_int` 用逐位试减（digit-recurrence）求根，每次处理被开方数的 2 个比特，试减项 \(\{q,2'b01\}=4q+1\) 来自恒等式 \((2q+1)^2=4q^2+4q+1\)；迭代次数 \(\text{ITER}=\text{WIDTH}/2\)。
- **定点版 `sqrt` 与整数版同构不同参**：唯一差别是 `ITER=(WIDTH+FBITS)/2`，多跑的 FBITS/2 次迭代等价于在被开方数末尾补 FBITS 个 0（即乘 \(2^{\text{FBITS}}\)），从而保持输出仍有 FBITS 个小数位。
- **乘法是组合型运算**：`mul.sv` 用 `IDLE/CALC/TRUNC/ROUND` 四状态机包裹一句有符号 `a1*b1`，额外完成截位、高斯舍入（round half to even）与双重溢出检测（符号校验 + 高位符号扩展校验）。
- **乘法走 DSP、开方走 LUT**：`signed` 操作数上的 `*` 会被综合器自动映射到 DSP48 硬核，符号由乘法器原生保证；开方没有对应硬核，必须用 LUT/FF 搭迭代通路——这是两者资源代价的根本差异。
- **握手接口统一**：`sqrt_int`/`sqrt`/`mul` 都遵循「`start` 启动 → `busy` 计算 → `valid`/`done` 完成」的多周期握手范式，便于串联成更大流水线（如综合实践的向量长度计算器）。

## 7. 下一步学习建议

- **继续 Unit 7**：阅读 [u7-l4 LFSR 伪随机与正弦查找表](u7-l4-lfsr-sine-table.md)，看 `lfsr.sv`（反馈多项式）与 `sine_table.sv`（用 u5-l3 的 ROM 存四分之一周期正弦样本），与本讲的 `mul`/`sqrt` 一起，构成 projf 数学库的完整图景。
- **综合应用**：阅读 [u7-l5 Mandelbrot 集](u7-l5-mandelbrot-maths-demo.md)，看定点复数乘法 \(z^2+c\) 如何在真实图形算法里反复调用本讲的乘法，并体会「定点数 + 多周期运算单元」如何拼成一个完整的硬件算法。
- **延伸阅读（项目外）**：projf 博客 [Square Root in Verilog](https://projectf.io/posts/square-root-in-verilog/) 与 [Verilog Library](https://projectf.io/verilog-lib/) 给出了开方与乘法的算法推导；Xilinx UG479（7 Series DSP48E1 Slice User Guide）是理解 DSP48 内部结构与推断规则的一手资料。
- **动手方向**：尝试给本讲的「向量长度计算器」加上流水线化（每拍吞吐一组输入），对比单倍吞吐与流水吞吐下的 DSP/LUT 占用与时延，体会「迭代型运算如何融入流水线设计」的工程权衡。
