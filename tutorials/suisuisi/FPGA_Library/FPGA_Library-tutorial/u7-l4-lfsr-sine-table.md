# LFSR 伪随机与正弦查找表

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「线性反馈移位寄存器（LFSR）」是什么，以及它为什么能在 FPGA 上廉价地产生伪随机数。
- 把一段 `TAPS` 位掩码翻译成 GF(2) 上的反馈多项式，判断它是否为本原多项式、对应的最大周期是多少。
- 逐行读懂 projf 库 [`lfsr.sv`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/lfsr.sv) 的伽罗瓦（Galois）实现，包括复位/种子（seed）的默认值处理。
- 理解正弦查找表 [`sine_table.sv`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sine_table.sv) 为什么只存 0°~90° 这四分之一周期，以及它如何用「象限折叠 + 符号修正」拼出 0°~360° 的完整正弦（和余弦）。
- 用 Icarus Verilog 跑通两个现成的 testbench，亲眼看到伪随机序列和正弦波形。

## 2. 前置知识

本讲是 Unit 7（FPGA 数学运算）的一篇，承接 [u7-l1 定点数](u7-l1-number-representation.md) 与 [u5-l3 存储器 ROM/RAM](u5-l3-memory-rom-ram-bram.md)。开始前请确认你大致了解：

- **GF(2) 运算**：在伽罗瓦域 GF(2) 上，加法和减法都是「异或（XOR，`^`）」，乘法是「与（AND，`&`）」。本讲里凡是看到「加」「减」，底层基本都是 XOR。这一点 u2-l3 讲 AES 的 GF(2⁸) 时已经用过。
- **定点数 Q 格式**：一个 N 位整数，约定其中若干位是小数位。本讲里正弦表输出是 Q8.8 定点（共 16 位，低 8 位是小数），数值范围 −1.0 ~ +1.0。回忆 u7-l1：定点数的小数点只是「设计者心中的隐含位置」。
- **ROM 与 `$readmemh`**：u5-l3 讲过，ROM 用一个 `.mem` 文本文件（十六进制一行一个值）在仿真启动时初始化。本讲的正弦样本就来自这样一个文件。
- **SystemVerilog 小子集**：`logic`、`always_comb`/`always_ff`、`$clog2`、参数化模块。这些在 u5-l1 统一介绍过。

两个本讲才出现的新术语先放在这里：

- **伪随机（pseudorandom）**：序列看起来随机，但其实由一个确定性公式和初始状态完全决定。只要知道公式和种子，就能完整复现。
- **查表（lookup table）**：把一个函数（如 sin）的若干采样值提前算好存进 ROM，运行时用输入作地址直接读出来，避免在硬件里做昂贵的三角运算。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lib/maths/lfsr.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/lfsr.sv) | 伽罗瓦 LFSR 模块，参数化长度与抽头，输出伪随机状态字。 |
| [lib/maths/sine_table.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sine_table.sv) | 正弦查找表，只存四分之一周期，靠象限折叠拼整圆。 |
| [lib/maths/res/sine_table_64x8.mem](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/res/sine_table_64x8.mem) | 64 条 8 位十六进制样本，即 0°~90° 的正弦值，由脚本 `sine2fmem.py` 生成。 |
| [lib/maths/xc7/lfsr_tb.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/xc7/lfsr_tb.sv) | LFSR 的 Vivado 仿真激励（纯 SV，也可被 iverilog 跑）。 |
| [lib/maths/xc7/sine_table_tb.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/xc7/sine_table_tb.sv) | 正弦表仿真激励，遍历 0~255 号地址并打印 Q8.8 结果。 |
| [lib/memory/rom_async.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/memory/rom_async.sv) | 异步 ROM，被 `sine_table` 例化用来存样本（u5-l3 精读过）。 |
| [demos/ad-astra/starfield.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/ad-astra/starfield.sv) | LFSR 的真实使用例：星空 demo 用 21 位 LFSR 产生随机星点。 |
| [demos/sinescroll/render_sinescroll.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/sinescroll/render_sinescroll.sv) | 正弦表的真实使用例：正弦滚动字模按正弦曲线排版。 |

（以上路径均相对 `ThreePart/projf-explore/`。）

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：先讲清**反馈多项式**这条数学主线（4.1），再用它解释 **LFSR 的 Verilog 实现**（4.2），最后转到完全不同的**正弦查找表**（4.3）。

### 4.1 反馈多项式与 m 序列

#### 4.1.1 概念说明

LFSR 的全部行为由一个 GF(2) 上的多项式决定，称为**反馈多项式（feedback polynomial）**。一个长度为 LEN 的 LFSR 对应一个 LEN 次多项式：

\[
P(x) = c_{LEN} x^{LEN} + c_{LEN-1} x^{LEN-1} + \cdots + c_1 x + c_0
\]

其中每个系数 \(c_i \in \{0,1\}\)。系数为 1 的项就是「抽头（tap）」。projf 用一个位掩码 `TAPS` 来装这些系数，规则是：**`TAPS` 的第 i 位为 1 ⟺ 多项式含 \(x^{i+1}\) 项；常数项 \(x^0=1\) 永远隐含存在**。最高次项 \(x^{LEN}\)（即 `TAPS` 的第 `LEN-1` 位）必须为 1。

举两个本仓库里真实出现的例子（下一节的源码会用到）：

| 出处 | LEN | TAPS（二进制） | 置位 bit | 对应多项式 | 周期 |
| --- | --- | --- | --- | --- | --- |
| `lfsr.sv` 默认值 | 8 | `8'b10111000` | {7,5,4,3} | \(x^8+x^6+x^5+x^4+1\) | 255 |
| `starfield.sv` 星空 | 21 | `21'b101000...0` | {20,18} | \(x^{21}+x^{19}+1\) | 2 097 151 |

如果这个反馈多项式是 GF(2) 上的**本原多项式（primitive polynomial）**，那么 LFSR 从任意非零状态出发，都会遍历除全零以外的所有 \(2^{LEN}-1\) 个状态后才回到起点。这样的序列叫**最大长度序列（maximal-length sequence），简称 m 序列**，其周期为：

\[
T = 2^{LEN} - 1
\`

上面两个多项式恰好都是本原多项式，对应 Xilinx 经典应用笔记 **XAPP052** 给出的最大长度抽头表（n=8 取 8,6,5,4；n=21 取 21,19），所以查表选抽头是工程上的标准做法，不必自己试。

m 序列之所以「看起来随机」，是因为它有三个良好统计性质：

- **平衡性**：一个完整周期里 1 的个数比 0 恰好多 1。
- **游程分布**：连续的同类比特（游程）长度越短出现越频繁，近似理想随机。
- **双值自相关**：平移后与自身重合时相关值很高，错位时几乎为 0，类似噪声。

但必须记住它是**伪**随机：完全确定、可复现，且**不含密码学强度**——看到连续 LEN 个比特就能反推整个序列。所以 LFSR 适合做星空、抖动渲染、测试激励，**绝不能直接当密钥流**（AES 才是密码学用途，见 Unit 2）。

最后一条关键约束：**全零状态是「锁死态」**。因为反馈是 XOR，一旦寄存器全零，反馈永远是 0，序列就卡死在零。所以种子绝不能为 0——这点直接决定了 4.2 节复位逻辑的写法。

#### 4.1.2 核心流程

把一个候选 `TAPS` 变成「能用的随机源」，流程是：

1. 查 XAPP052 表（或用下节实践里的脚本枚举），为给定 LEN 选一组本原抽头。
2. 把抽头按本节的位规则写成 `TAPS` 位掩码（最高位必为 1）。
3. 确认周期为 \(2^{LEN}-1\)。
4. 选一个**非零**种子作为初值。

判断「本原」最简单的工程办法不是手算，而是**仿真跑一圈看状态数**：从某个非零种子出发，数经过多少拍回到种子；若等于 \(2^{LEN}-1\) 就是本原，否则 `TAPS` 选错了。

#### 4.1.3 源码精读

反馈多项式在源码里就体现为模块参数 `TAPS`。先看 [`lfsr.sv:10-19`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/lfsr.sv#L10-L19) 的参数声明：

```verilog
module lfsr #(
    parameter LEN=8,                   // shift register length
    parameter TAPS=8'b10111000         // XOR taps
    ) (
    ...
    output      logic [LEN-1:0] sreg   // lfsr output
);
```

`LEN=8`、`TAPS=8'b10111000` 就是上表第一行，对应 \(x^8+x^6+x^5+x^4+1\)。注意 `TAPS` 的位宽随 `LEN` 变化，所以换长度时必须同时换 `TAPS`——这正是星空 demo 里要做的事，见 [`starfield.sv:41-44`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/ad-astra/starfield.sv#L41-L44)：

```verilog
lfsr #(
    .LEN(21),
    .TAPS(21'b101000000000000000000)
    ) lsfr_sf ( ... );
```

这里 `LEN` 提到 21（为了得到更大的随机状态空间画满天星），`TAPS` 同步换成 21 位本原抽头 {20,18}，周期立即涨到 \(2^{21}-1 \approx 209\) 万。这两段代码印证了「反馈多项式 = `TAPS` 位掩码」这一对应关系。

#### 4.1.4 代码实践

**目标**：亲手验证「`TAPS` ↔ 多项式 ↔ 周期」三者的一致性。

**操作步骤**（源码阅读型 + 小脚本）：

1. 打开 [`lfsr.sv:12`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/lfsr.sv#L12)，把 `8'b10111000` 的置位 bit 数出来：{7,5,4,3}。
2. 按「bit i ⟺ \(x^{i+1}\)」翻译成多项式，应得到 \(x^8+x^6+x^5+x^4+1\)。
3. 写一段几行的 Python，模拟 8 位伽罗瓦 LFSR（下一节给出更新公式），从种子 1 出发，记录每个新状态，数到第几次回到 1：
   ```python
   LEN, TAPS = 8, 0b10111000
   s = 1; seen = 0
   while True:
       s = ((s >> 1) ^ (TAPS if (s & 1) else 0)) & ((1<<LEN)-1)
       seen += 1
       if s == 1: break
   print(seen)   # 预期 255
   ```

**需要观察的现象**：脚本输出 255，正好等于 \(2^8-1\)。

**预期结果**：周期 255。若你故意把 `TAPS` 改成非本原值（如 `8'b10100000`），输出会明显小于 255，说明该抽头不产生 m 序列。

> 若无 Python 环境，本步骤也可标为「待本地验证」，直接进入 4.2 用仿真器观察。

#### 4.1.5 小练习与答案

**练习 1**：`TAPS=8'b10111000` 的置位 bit 是哪些？写出对应多项式与周期。

**答案**：置位 bit {7,5,4,3} ⟺ 多项式 \(x^8+x^6+x^5+x^4+1\)；它是本原多项式，周期 \(2^8-1=255\)。

**练习 2**：为什么 LFSR 的种子不能取全零？

**答案**：反馈是 XOR，全零状态下每一拍反馈位都是 0，状态永远停留在全零，序列锁死。所以种子必须非零；这也解释了为什么模块在 seed=0 时改用全 1 作默认种子（见 4.2.3）。

---

### 4.2 Galois LFSR 的 Verilog 实现

#### 4.2.1 概念说明

反馈多项式决定了「算什么」，而 LFSR 的**结构**有两种经典实现：

- **斐波那契（Fibonacci）LFSR**：把若干个抽头位 XOR 起来产生一个新比特，从一端移入，寄存器整体移位。反馈是一条「多输入 XOR」链。
- **伽罗瓦（Galois）LFSR**：每拍先把寄存器整体移一位，再根据「移出去的那一位」是否为 1，决定是否对整个寄存器异或一个抽头掩码。

projf 的 [`lfsr.sv`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/lfsr.sv) 文件头明确写着 *Galois Linear-Feedback Shift Register*，采用的是第二种。伽罗瓦形式的好处是：每个抽头只是一个并行 XOR，路径里只有一级 XOR 延迟，容易跑高时钟频率；而且「移位 + 整体掩码 XOR」写起来非常简洁。两种结构在数学上等价（互为 reciprocal polynomial），产生的 m 序列周期相同。

#### 4.2.2 核心流程

每来一个时钟上升沿，且 `en` 有效时，伽罗瓦 LFSR 做两件事，合成一句赋值：

1. **右移一位**：`{1'b0, sreg[LEN-1:1]}`——最高位补 0，原最低位 `sreg[0]` 被「挤出去」。
2. **条件异或掩码**：如果刚挤出去的 `sreg[0]` 为 1，则把整个寄存器异或 `TAPS`；否则异或 0（不变）。

伪代码：

```
if en:
    out_bit = sreg[0]                       # 移出的比特
    shifted  = (0 concat sreg[LEN-1:1])     # 右移，MSB 补 0
    sreg_next = shifted ^ (out_bit ? TAPS : 0)
if rst:
    sreg_next = (seed != 0) ? seed : 全 1    # 避免锁死态
```

复位时若种子为 0 就用全 1 顶上，正是 4.1.5 练习 2 的结论落地。

#### 4.2.3 源码精读

整个模块只有一个 `always_ff`，核心是 [`lfsr.sv:21-24`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/lfsr.sv#L21-L24)：

```verilog
always_ff @(posedge clk) begin
    if (en)  sreg <= {1'b0, sreg[LEN-1:1]} ^ (sreg[0] ? TAPS : {LEN{1'b0}});
    if (rst) sreg <= (seed != 0) ? seed : {LEN{1'b1}};
endmodule
```

逐句拆：

- `{1'b0, sreg[LEN-1:1]}` 是右移一位、最高位补 0。
- `sreg[0] ? TAPS : {LEN{1'b0}}` 用移出的最低位作条件：为 1 才异或 `TAPS`，为 0 则异或全零（保持移位结果不变）。`{LEN{1'b0}}` 是 LEN 位 0 的复制写法，保证两个分支位宽一致。
- 整句合起来：`sreg <= 右移结果 ^ 条件掩码`，与 4.2.2 的伪代码一一对应。
- 复位句 `sreg <= (seed != 0) ? seed : {LEN{1'b1}}`：seed 非零直接用，seed 为零则默认全 1，巧妙避开锁死态。注意文件头第 8 行注释 *NB. Ensure reset is asserted for one or more cycles before enable*——因为种子是在 `rst` 期间灌入的，必须先复位至少一拍再拉 `en`。

输出就是寄存器本身 `sreg`（端口在 [`lfsr.sv:18`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/lfsr.sv#L18)），整字当伪随机数用；星空 demo 还从中挑几位做亮度（[`starfield.sv:36-39`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/ad-astra/starfield.sv#L36-L39) `sf_star = sf_reg[7:0]`）。

#### 4.2.4 代码实践

**目标**：用 Icarus Verilog 跑现成 testbench，观察 8 位 LFSR 的状态序列并确认周期。

**操作步骤**：

1. 准备源文件清单：被测模块 `lib/maths/lfsr.sv` 与激励 `lib/maths/xc7/lfsr_tb.sv`。
2. 用 iverilog 编译（`-g2012` 打开 SV 子集，projf 全靠它）：
   ```bash
   cd ThreePart/projf-explore/lib/maths
   iverilog -g2012 -o /tmp/lfsr_tb.vvp xc7/lfsr_tb.sv lfsr.sv
   vvp /tmp/lfsr_tb.vvp
   ```
3. 想看波形时，在 testbench 里临时加 `$dumpfile("lfsr.vcd"); $dumpvars;`，再用 GTKWave 打开 `lfsr.vcd`。
4. 改试验：把 [`lfsr_tb.sv:35`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/xc7/lfsr_tb.sv#L35) 的 `seed = '1;` 改成 `seed = 8'b0;`，重新跑。

**需要观察的现象**：

- 正常种子下，`sreg` 每拍变一个值，看起来杂乱无章；耐心数（或脚本统计）会发现 255 拍后回到起点。
- `seed=0` 时，复位按默认规则灌入全 1，序列仍正常起步——验证了「seed=0 ⟹ 全 1」的兜底逻辑。

**预期结果**：8 位 LFSR 周期为 255，与 4.1.4 的脚本一致；seed=0 不会锁死。

> 命令未在本环境实跑，若 iverilog 未安装或路径不同，标为「待本地验证」。也可改用 Vivado 直接打开 `xc7/vivado/lfsr_tb_behav.wcfg` 波形配置。

#### 4.2.5 小练习与答案

**练习 1**：把 `lfsr.sv` 第 22 行的 `{1'b0, sreg[LEN-1:1]}` 改成左移 `{sreg[LEN-2:0], 1'b0}` 并把条件位换成 `sreg[LEN-1]`，还能是同一个 LFSR 吗？

**答案**：它是同一个反馈多项式对应的「左移 + 取最高位」版本，数学上等价、周期不变，只是状态比特排列方向相反。projf 统一选右移写法以保持代码风格一致。

**练习 2**：星空 demo 用 `LEN=21`，为什么不用 `LEN=8`？

**答案**：8 位 LFSR 只有 255 个状态，星点很快重复，画面缺乏随机感；21 位有约 209 万个状态，配合 `MASK` 抽样（[`starfield.sv:13`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/ad-astra/starfield.sv#L13)）能生成足够稀疏、不重复的星空。

---

### 4.3 正弦查找表 sine_table

#### 4.3.1 概念说明

在 FPGA 上求 sin(x)，最省事的办法不是搭一个三角运算电路，而是**查表**：把 sin 在一个周期内的采样值提前算好存进 ROM，运行时拿角度作地址直接读。但「存整圆」很费存储：要 256 条样本就得 256 个 ROM 表项。

正弦函数有个天赐的对称性，可以砍掉 3/4 的存储：

- \(\sin(180^\circ-\theta)=\sin(\theta)\)：第二象限是第一象限的镜像。
- \(\sin(360^\circ-\theta)=-\sin(\theta)\)：第四象限是第一象限取负。
- \(\sin(180^\circ+\theta)=-\sin(\theta)\)：第三象限是第一象限取负。

于是只需存 **0°~90°（第一象限）** 的样本，其余三个象限靠「折叠地址 + 修正符号」现场拼出来。projf 的 `sine_table` 默认存 64 条 8 位样本（文件 [`sine_table_64x8.mem`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/res/sine_table_64x8.mem)），却能用 8 位地址（0~255）表达整圆 0°~360°——存储省了 4 倍。

输出是 **Q8.8 有符号定点**（共 16 位，低 8 位小数），范围 −1.0 ~ +1.0，与 u7-l1 的定点数约定完全一致。顺带一提：**余弦就是正弦相位加 90°**，`cos(θ)=sin(θ+90°)`，所以同一张表也能查余弦——只需给输入地址加一个 `ROM_DEPTH` 的偏移（见 4.3.4 的 sinescroll 用法）。这也是 [README](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/README.md) 第 17 行写「sine and cosine from lookup table」的原因。

#### 4.3.2 核心流程

整圆共 `4*ROM_DEPTH` 个地址（默认 256），用 `ADDRW=$clog2(4*ROM_DEPTH)` 位（默认 8 位）编址。地址 `id` 的**最高两位**决定象限，**低 6 位**经折叠后去查四分之一周期 ROM：

```
quad = id 的最高两位
case quad:
  00 (Q1,   0°~ 90°): tab_id = id                       # 正查
  01 (Q2,  90°~180°): tab_id = 2*ROM_DEPTH - id低6位     # 镜像
  10 (Q3, 180°~270°): tab_id = id低6位 - 2*ROM_DEPTH     # 正查，再取负
  11 (Q4, 270°~360°): tab_id = 4*ROM_DEPTH - id低6位     # 镜像，再取负

tab_data = ROM[tab_id 折叠进 0~63]                        # 0°~90° 样本

# 符号修正与峰值特判
if id == ROM_DEPTH:      data = +1.0     # 恰好 90°
elif id == 3*ROM_DEPTH:  data = -1.0     # 恰好 270°
elif quad 为 Q1/Q2:      data = +tab_data
else (Q3/Q4):            data = -tab_data
```

两个细节值得留意（4.3.3 用源码佐证）：

- `tab_id` 是 6 位向量（`$clog2(ROM_DEPTH)` 位），折叠公式算出的中间值会被**截断/回绕**到 6 位，这正是源码里 `verilator lint_off WIDTH` 注释存在的原因。
- 90° 与 270° 两个峰值处，折叠 + 截断恰好算不对，所以用 `if (id == ROM_DEPTH)` / `if (id == 3*ROM_DEPTH)` 两个特判直接钉死成 ±1.0。

#### 4.3.3 源码精读

**第一段：参数与四分之一周期 ROM**（[`sine_table.sv:8-28`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sine_table.sv#L8-L28)）：

```verilog
module sine_table #(
    parameter ROM_DEPTH=64,  // 0° to 90° 的样本数
    parameter ROM_WIDTH=8,
    parameter ROM_FILE="",
    parameter ADDRW=$clog2(4*ROM_DEPTH)  // 整圆 0°~360°
) (
    input  wire logic [ADDRW-1:0] id,
    output      logic signed [2*ROM_WIDTH-1:0] data  // Q8.8 定点
);
    logic [$clog2(ROM_DEPTH)-1:0] tab_id;     // 6 位，索引四分之一表
    logic [ROM_WIDTH-1:0] tab_data;
    rom_async #(.WIDTH(ROM_WIDTH), .DEPTH(ROM_DEPTH), .INIT_F(ROM_FILE))
        sine_rom (.addr(tab_id), .data(tab_data));
```

`data` 是 `signed [15:0]`——Q8.8 定点输出。ROM 用的是 u5-l3 精读过的 `rom_async`（异步组合读，`always_comb data = memory[addr]`），内容从 `ROM_FILE`（即 `.mem` 文件）用 `$readmemh` 装载。

**第二段：象限折叠**（[`sine_table.sv:30-41`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sine_table.sv#L30-L41)）：

```verilog
logic [1:0] quad;
always_comb begin
    quad = id[ADDRW-1:ADDRW-2];               // 最高两位定象限
    case (quad)
        2'b00: tab_id = id[ADDRW-3:0];                //  I
        2'b01: tab_id = 2*ROM_DEPTH - id[ADDRW-3:0];  // II
        2'b10: tab_id = id[ADDRW-3:0] - 2*ROM_DEPTH;  // III
        2'b11: tab_id = 4*ROM_DEPTH - id[ADDRW-3:0];  // IV
    endcase
end
```

`id[ADDRW-3:0]` 即低 6 位。`/* verilator lint_off WIDTH */` 包住 case，是因为 `2*ROM_DEPTH - ...` 这类表达式的中间位宽大于 6 位目标 `tab_id`，靠截断完成回绕折叠。

**第三段：符号修正与峰值特判**（[`sine_table.sv:43-55`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sine_table.sv#L43-L55)）：

```verilog
always_comb begin
    if (id == ROM_DEPTH) begin        // sin(90°)=+1.0  → 0x0100
        data = {{ROM_WIDTH-1{1'b0}}, 1'b1, {ROM_WIDTH{1'b0}}};
    end else if (id == 3*ROM_DEPTH) begin  // sin(270°)=-1.0 → 0xFF00
        data = {{ROM_WIDTH{1'b1}}, {ROM_WIDTH{1'b0}}};
    end else begin
        if (quad[1] == 0)             // Q1/Q2 为正
            data = {{ROM_WIDTH{1'b0}}, tab_data};
        else                          // Q3/Q4 为负（补码取负）
            data = {2*ROM_WIDTH{1'b0}} - {{ROM_WIDTH{1'b0}}, tab_data};
    end
end
```

要点：

- 正值把 8 位 `tab_data` 零扩展到 16 位 `{8'h00, tab_data}`。
- 负值用 `0 - tab_data` 做补码取负（GF 里减法 = 加法 = XOR，但这里是普通二进制补码减法，得到 Q8.8 的负小数）。
- 90° 的 `0x0100`：整数部分 1、小数部分 0，即 Q8.8 的 +1.0；270° 的 `0xFF00` 是 −256 的 16 位补码，即 −1.0。

想确认这些值，对照 [`sine_table_64x8.mem`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/res/sine_table_64x8.mem)：第 0 行 `00`（sin0°=0）、第 32 行 `B5`=181（181/256≈0.707，约 sin45°）、第 63 行 `FF`=255（sin90°≈1.0）。文件头注明由 `sine2fmem.py` 生成，可自行改精度重生成。

#### 4.3.4 代码实践

**目标**：跑 `sine_table_tb` 看整圆波形，并用「地址加偏移」查余弦。

**操作步骤**：

1. 编译三个文件：激励、被测模块、以及它依赖的 `rom_async`：
   ```bash
   cd ThreePart/projf-explore/lib/maths
   iverilog -g2012 -o /tmp/sine_tb.vvp \
       xc7/sine_table_tb.sv sine_table.sv ../memory/rom_async.sv
   ```
2. testbench 里 `ROM_FILE="sine_table_64x8.mem"`（见 [`sine_table_tb.sv:12`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/xc7/sine_table_tb.sv#L12)），`$readmemh` 按相对路径找该文件，所以**要在 `lib/maths/res/` 目录下运行 vvp**，或把 `.mem` 复制到运行目录：
   ```bash
   ( cd res && vvp /tmp/sine_tb.vvp )
   ```
3. testbench 会遍历 id = 0,1,21,32,43,63,64,65,100,127,128,129,149,191,192,193,224,255，逐个 `$display` 打印二进制与 Q8.8 浮点值（打印格式见 [`sine_table_tb.sv:28`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/xc7/sine_table_tb.sv#L28)）。
4. 想生成连续正弦波：把激励改成 `for (id=0; id<256; id=id+1)` 循环打印，再把数值画出来。
5. 查余弦的示范已在 demo 里：[sinescroll 用 `sin_id + sin_offs` 作地址](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/sinescroll/render_sinescroll.sv#L35-L42)，`sin_offs` 每帧自增（[第 129 行](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/sinescroll/render_sinescroll.sv#L129)），观察它是如何让字模沿正弦曲线波动的。

**需要观察的现象**：

- id=0 打印 0.0；id=32 打印 ≈0.707；id=64 打印 1.0；id=128 打印 0.0；id=192 打印 −1.0；id=255 打印接近 0 的负小值。
- 把 256 个点连起来，是一条完整的、上下对称的正弦曲线。

**预期结果**：打印值与 `.mem` 对照（除 90°/270° 特判外）逐项吻合；整圆波形光滑、周期为 256。

> iverilog 命令未在本环境实跑，路径或版本差异可能导致 `$readmemh` 找不到 `.mem`；若遇到，按步骤 2 调整工作目录，或标为「待本地验证」。也可直接用 Vivado 打开 `xc7/vivado/sine_table_tb_behav.wcfg`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `sine_table` 只存 0°~90° 而不直接存 0°~360°？

**答案**：利用正弦的三个对称性（第二象限镜像、第三/四象限取负），存四分之一周期就能折叠出整圆，存储量省 4 倍；代价只是几个组合逻辑表达式和两个峰值特判。

**练习 2**：把 `ROM_DEPTH` 从 64 改成 128（样本翻倍），`ADDRW` 与地址分辨率各变成多少？对波形有什么影响？

**答案**：`ADDRW=$clog2(4*128)=$clog2(512)=9` 位，整圆地址分辨率从 256 点提升到 512 点；波形更细腻、量化误差更小，但 ROM 容量翻倍。注意还需重新生成对应的 `.mem` 文件。

**练习 3**：如何用同一张 `sine_table` 查 `cos(θ)`？

**答案**：`cos(θ)=sin(θ+90°)`，把输入地址加 `ROM_DEPTH`（默认 64，即 90°）再查表即可，无需第二张表。sinescroll demo 正是用地址偏移实现相位移动的。

---

## 5. 综合实践

把本讲两块内容串成一个「**LFSR 噪声 + 正弦调制**」的小实验，作为收尾任务：

**任务描述**：设计一个顶层模块，每拍用 `lfsr` 产生一个 8 位伪随机数 `noise`，同时用一个 8 位计数器 `phase` 自增作为正弦表地址；把 `noise` 和 `sine_table` 的低 8 位输出做平均（或叠加），得到「被随机噪声扰动」的正弦序列。

**建议步骤**：

1. 例化一个 `lfsr #(.LEN(8), .TAPS(8'b10111000))`，时钟与复位沿用本仓 100 MHz 约定。
2. 例化一个 `sine_table #(.ROM_DEPTH(64), .ROM_WIDTH(8), .ROM_FILE("sine_table_64x8.mem"))`，地址 `id` 接一个 8 位自增计数器 `phase`。
3. 顶层输出 `out = (sreg[7:0] + data[7:0]) >> 1`（示例代码，简单平均）。
4. 写 testbench 跑 256 拍，`$display` 打印 `phase, sreg, data, out`，把 `out` 序列画出来。
5. 观察并记录：纯正弦波形（关闭 LFSR）与叠加噪声后的波形差异；改变 LFSR 种子，观察噪声细节变化但正弦包络不变。

**预期结果**：得到一条以正弦曲线为包络、叠加伪随机抖动的波形；换种子只改变抖动细节，不改变正弦周期——这正是 LFSR（随机源）与 sine_table（确定性查表）协同的典型效果。本任务为开放性练习，连线与截位方式不唯一；若无仿真环境，可只完成「画出预期数据流框图 + 列出关键信号清单」。

## 6. 本讲小结

- **LFSR 的本质是一个 GF(2) 反馈多项式**：`TAPS` 位掩码按「bit i ⟺ \(x^{i+1}\)」翻译成多项式，常数项隐含；本原多项式给出最大长度 m 序列，周期 \(2^{LEN}-1\)。
- **projf 用伽罗瓦结构**实现：每拍右移一位，若移出的最低位为 1 则整体异或 `TAPS`；一句 `sreg <= {1'b0, sreg[LEN-1:1]} ^ (sreg[0] ? TAPS : 0)` 搞定。
- **全零是锁死态**，所以复位时 seed=0 自动改用全 1 兜底，且必须先复位至少一拍再 `en`。
- **LFSR 是伪随机、非密码学**：适合星空/抖动/激励，不可当密钥流（密钥流见 Unit 2 的 AES）。
- **sine_table 只存四分之一周期**（默认 64 条 8 位样本），靠象限折叠 + 符号修正 + 90°/270° 特判拼出整圆，存储省 4 倍；输出 Q8.8 有符号定点。
- **余弦 = 正弦相位加 90°**：同一张表加地址偏移 `ROM_DEPTH` 即可查余弦，sinescroll demo 即此用法。

## 7. 下一步学习建议

- **横向应用 LFSR**：阅读 [`demos/ad-astra`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/ad-astra/starfield.sv) 全工程，看 21 位 LFSR 如何配合 `MASK` 抽样渲染星空，并在 [framebuffers/top_david_fizzle.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/framebuffers/xc7/top_david_fizzle.sv) 里看 LFSR 如何给帧缓冲加随机闪烁。
- **横向应用 sine_table**：阅读 [demos/sinescroll](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/sinescroll/render_sinescroll.sv) 与 [demos/rasterbars](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/rasterbars/render_rasterbars.sv)，体会正弦表如何驱动字模与彩条动画。
- **向纵深学数学库**：本讲是 u7（FPGA 数学运算）的一环。建议接着读 [u7-l2 除法器](u7-l2-signed-division.md)（同款状态机风格）、[u7-l3 平方根/DSP 乘法](u7-l3-sqrt-multiply.md)，把 maths 分区整套收齐；并可回头对照 [u5-l3 存储器](u5-l3-memory-rom-ram-bram.md) 看 `rom_async`/`rom_sync` 的差异，理解为何正弦表选了异步读。
- **想生成自定义正弦表**：按 README 第 52 行指引，用 [sine2fmem](https://github.com/projf/fpgatools/tree/master/sine2fmem) 脚本改精度重生成 `.mem`，配合练习 2 把分辨率调到 512 点。
