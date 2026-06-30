# 时钟分频：clk_divider

## 1. 本讲目标

本讲是「基础组合与时序原语」单元的第一讲。我们正式打开第一个绿圈基础模块 `clk_divider.sv`，把它的每一行都读明白。

学完后你应当能够：

- 说出 `clk_divider` 不过是一个**自由运行的二进制计数器**，并解释为什么这样一个计数器可以同时输出很多个慢时钟。
- 推导并记住核心公式：计数器的第 `N` 位 `out[N]` 是原时钟的 \(1/2^{N+1}\) 分频，即 \(f_{\text{out}[N]} = f_{\text{clk}} / 2^{N+1}\)。
- 在 testbench 和真实 FPGA 工程中正确例化 `clk_divider`，并知道它的「用途与局限」——为什么仓库里几乎从不把这些位当作真正的时钟，而是当作慢数据信号或时钟使能脉冲。

> 本讲承接 [u1-l2 模块四段式结构](u1-l2-module-anatomy.md) 与 [u1-l3 仿真器跑起来](u1-l3-simulation-setup.md)。如果你还不熟悉 `always_ff`、`timescale` 或 iverilog 编译流程，建议先读这两篇。

---

## 2. 前置知识

在硬件设计里，「时钟（clock）」是驱动所有时序逻辑跳动的节拍。但在真实工程中，我们常常需要**比主时钟更慢的节拍**，例如：

- 让 LED 每 0.5 秒闪一次（主时钟可能是 50 MHz，远快于人眼能分辨的速度）。
- 产生一个「每 1 毫秒来一个脉冲」的周期事件，用来做心跳或采样。
- 给慢速外设（如某些 UART、低频传感器）提供一个参考频率。

最简单的产生慢节拍的办法，就是**数主时钟的拍数**：每数到 N 拍就翻转一下输出，就得到了主时钟的 \(2N\) 分频。`clk_divider` 用了最极端、最优雅的一种实现——一个不断 `+1` 的二进制计数器，它的**每一位单独看都是一个不同分频比的方波**。

下面三个概念在本讲会反复出现，先统一术语：

| 术语 | 含义 |
|---|---|
| 自由运行计数器（free-running counter） | 上电后每个时钟都自增、不停止、不复位（除非显式复位）的计数器 |
| 分频（clock division） | 用一个高频时钟产生一个低频方波信号，输出频率 = 输入频率 / 分频比 |
| 派生时钟（derived clock） | 由主时钟经分频/计数得到的、频率更低但相位相关的信号 |
| 占空比（duty cycle） | 方波一个周期内高电平所占的比例；50% 表示高低各半 |

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|---|---|
| [clk_divider.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clk_divider.sv) | 模块本体。整个模块只有 10 行有效逻辑，是本讲主角。 |
| [main_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/main_tb.sv) | 仓库顶层 testbench 模板。在其中例化了 `clk_divider`，并把它的 32 位输出送进 `edge_detect` 阵列，演示「派生时钟 → 单拍脉冲」的典型用法。 |
| [example_projects/quartus_test_prj_template_v4/src/main.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/src/main.sv) | Quartus 上板工程模板顶层。例化了两个 `clk_divider`（分别挂在 125 MHz 和 500 MHz 时钟上），并用其中一位驱动 LED 闪烁，是「上板用法」的样板。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **自由二进制计数器**——读懂 `clk_divider` 的实现。
2. **派生同步时钟**——为什么计数器的每一位都是不同分频比的方波，以及它作为「时钟树」的用途与局限。
3. **在工程中使用 clk_divider**——结合 `main_tb.sv` 和 `main.sv` 看真实用法。

---

### 4.1 自由二进制计数器

#### 4.1.1 概念说明

`clk_divider` 名字里有「divider（分频器）」，但读完源码你会发现：它**根本没有专门写任何「分频」逻辑**，它就是一个最朴素的二进制计数器。所有「分频」效果，都是二进制计数的自然副产物。

这就是本模块的精髓——**用一个自由运行的计数器，一次性得到一整套慢时钟**，而不是为每一个想要的慢时钟单独写一个分频器。

模块只有 3 个控制端口：

- `clk`：主时钟，计数器靠它的上升沿一步一步往前走。
- `nrst`：**低有效同步复位**（`n` 表示 active-low，low active）。为 0 时把整个计数器清零。
- `ena`：计数使能。为 1 才计数，为 0 则冻结当前值。
- `out[(WIDTH-1):0]`：计数输出，宽度由参数 `WIDTH` 决定（默认 32 位）。

#### 4.1.2 核心流程

每个 `clk` 上升沿，模块做一件事：

```text
if (~nrst)        // 复位（低有效）
    out <= 0
else if (ena)     // 使能时
    out <= out + 1
// 否则保持不变
```

把前 8 拍的 `out`（以 WIDTH=8 为例）列出来，规律就一目了然：

| 拍数 | out[7] | out[6] | out[5] | out[4] | out[3] | out[2] | out[1] | out[0] |
|---|---|---|---|---|---|---|---|---|
| 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 |
| 2 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 |
| 3 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 |
| 4 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| ... | | | | | | | | |
| 255→0 | 回到全 0，循环 | | | | | | | |

注意每一列单独看：`out[0]` 每拍都翻转；`out[1]` 每 2 拍翻转一次；`out[2]` 每 4 拍翻转一次……这正是下一节要讲的「派生时钟」。这里只要先记住：**它就是一个自由运行的二进制计数器**。

#### 4.1.3 源码精读

先看模块的「名片」与例化模板，这是仓库统一的四段式写法（详见 [u1-l2](u1-l2-module-anatomy.md)）：

[clk_divider.sv:7-8](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clk_divider.sv#L7-L8) —— INFO 一句话点明用途：「把主时钟分频，得到多个派生的、更慢的、同步的时钟」。关键词是 **derivative（派生）** 和 **synchronous（同步）**，这预告了下一节的两个核心结论。

[clk_divider.sv:11-22](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clk_divider.sv#L11-L22) —— 例化模板。复制它、改 `.WIDTH(...)` 和端口连接即可使用，无需改源码。

接着是模块主体。参数化端口是仓库高度复用的根基：

[clk_divider.sv:25-32](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clk_divider.sv#L25-L32) —— `WIDTH` 默认 32，决定 `out` 有多少位、也就决定了你一次能拿到多少个不同分频比的慢时钟。`out` 用 `output logic ... = '0` 在声明时直接给了初值 0（上电值）。

整个模块的全部逻辑只有这一个 `always_ff` 块：

[clk_divider.sv:35-41](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clk_divider.sv#L35-L41) —— 这就是「自由二进制计数器」的全部。第 36 行 `if ( ~nrst )` 是低有效同步复位；第 38 行 `else if (ena)` 是使能条件；第 39 行 `out <= out + 1'b1` 是核心：每拍自增 1。注意全程使用非阻塞赋值 `<=`，这正是 [u1-l2](u1-l2-module-anatomy.md) 讲过的时序逻辑约定。

> 小提醒：第 39 行写的是 `+ 1'b1` 而不是 `+ 1`。这是仓库的统一风格——显式标注位宽，避免综合器把常数推断成 32 位整数，在跨工具（Quartus/Vivado/Gowin）时更安全。

#### 4.1.4 代码实践

**实践目标**：用 `$display` 直接「看」计数器在数数，确认它真的只是 `+1`。

**操作步骤**（示例代码，需自行建为 `clk_divider_trace_tb.sv`，编译方式参考 [u1-l3](u1-l3-simulation-setup.md)，iverilog 请加 `-g2012`）：

```systemverilog
`timescale 1ns / 1ps
module clk_divider_trace_tb();
  logic clk = 1'b0;
  always #5 clk = ~clk;            // 10ns 周期 = 100 MHz

  logic nrst = 1'b0;
  initial begin
    #12 nrst = 1'b1;               // 释放复位，开始计数
  end

  logic [7:0] out;
  clk_divider #( .WIDTH( 8 ) ) dut (
    .clk( clk ), .nrst( nrst ), .ena( 1'b1 ), .out( out )
  );

  initial begin
    $dumpfile("trace.vcd"); $dumpvars;
    repeat(20) @(posedge clk) $display("t=%0t  out=%0d (8'b%08b)", $time, out, out);
    $finish;
  end
endmodule
```

**需要观察的现象**：每拍打印里 `out` 恰好 `+1`；复位期间 `out` 保持 0。

**预期结果**：打印形如 `out=0,1,2,3,...,19`，二进制列每拍只有最低几位在变。如果你看到这个，就证明你读懂了「自由计数器」这一层。

> 若本地无仿真器无法运行，标记「待本地验证」，但手算前 20 拍的 `out` 值应当与打印一致。

#### 4.1.5 小练习与答案

**练习 1**：把 `.ena( 1'b1 )` 改成 `.ena( 1'b0 )`，`out` 会怎样？

**答案**：`ena=0` 时第 38 行条件不成立，`out` 既不复位也不自增，**永远停在复位释放那一刻的值（0）**。这说明 `ena` 可以当作「暂停计数」开关。

**练习 2**：模块用的是同步复位还是异步复位？依据是什么？

**答案**：**同步复位**。依据是 [clk_divider.sv:35](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clk_divider.sv#L35) 的敏感列表只有 `posedge clk`，没有 `negedge nrst`；复位动作发生在时钟沿内部（第 36 行），而不是时钟一复位就立刻生效。这符合 [u1-l2](u1-l2-module-anatomy.md) 讲的 `nrst` 命名约定。

---

### 4.2 派生同步时钟

#### 4.2.1 概念说明

现在回到 4.1.2 那张表，单独盯住某一列：

- `out[0]`：`0,1,0,1,...` —— 每拍翻转，是一个周期为 2 拍的方波 → 频率 = \(f_{\text{clk}}/2\)
- `out[1]`：`0,0,1,1,0,0,1,1,...` —— 每 2 拍翻转，周期 4 拍 → 频率 = \(f_{\text{clk}}/4\)
- `out[k]`：每 \(2^k\) 拍翻转，周期 \(2^{k+1}\) 拍 → 频率 = \(f_{\text{clk}}/2^{k+1}\)

于是得到本讲最重要的公式：

\[
f_{\text{out}[N]} \;=\; \frac{f_{\text{clk}}}{2^{N+1}}, \qquad N = 0,1,2,\dots,\text{WIDTH}-1
\]

等价地，周期关系：

\[
T_{\text{out}[N]} \;=\; 2^{N+1} \cdot T_{\text{clk}}
\]

并且由于二进制计数的对称性，每一位**高电平和低电平各占一半时间**，所以每一位都是 **50% 占空比**的方波。

为什么说这些派生时钟是「同步」的？因为它们全部来自同一个自由计数器、由**同一个 `clk` 上升沿**更新。所有位都在同一个时钟沿跳变，相位一致、没有毛刺（每个位都是一个独立的寄存器输出，不是组合逻辑拼出来的）。这就是 INFO 里 **synchronous** 的含义，也是它被称为「时钟树」的底气——一棵树上挂满了互相协调的慢时钟。

#### 4.2.2 核心流程

把 8 位计数器 `out[7:0]` 每一位对应的分频比列成表：

| 位 | 翻转周期（主时钟拍数） | 分频比 | 频率（设 \(f_{\text{clk}}=100\text{ MHz}\)） |
|---|---|---|---|
| out[0] | 2 | /2 | 50.00 MHz |
| out[1] | 4 | /4 | 25.00 MHz |
| out[2] | 8 | /8 | 12.50 MHz |
| out[3] | 16 | /16 | 6.25 MHz |
| out[4] | 32 | /32 | 3.125 MHz |
| out[5] | 64 | /64 | 1.5625 MHz |
| out[6] | 128 | /128 | 781.25 kHz |
| out[7] | 256 | /256 | 390.625 kHz |

**用途**：

- **慢数据信号**：把高位（如 `out[25]`）直接接到 LED，得到肉眼可见的闪烁。
- **周期脉冲源**：把某一位送进 `edge_detect`，把它的上升沿变成主时钟域里的**单拍使能脉冲**，用来做定时采样、心跳、刷新。
- **时间参考**：在需要「每隔固定拍数做一次某事」的场合，直接用某一位当使能。

**局限**（这是面试和工程实战的考点）：

1. **只能是 2 的幂次分频**。无法直接得到 /3、/5、/6、/10 等任意分频比——那需要带比较器的计数分频器。
2. **不要把 `out[N]` 当成真正的时钟去驱动另一个 `always_ff`**。虽然波形上它是方波，但它走的是**数据布线资源**，不在器件的专用时钟树上，抖动和延迟都不受控；而且每多用一位就会凭空多出一个「时钟域」，让时序约束（`create_generated_clock`）和 CDC 分析变得复杂。仓库的推荐做法见 4.3：**把它当数据用，或用 `edge_detect` 转成主域里的使能脉冲**。

#### 4.2.3 源码精读

派生时钟关系**完全隐含在 [clk_divider.sv:39](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clk_divider.sv#L39) 这一行** `out <= out + 1'b1` 里。模块里没有任何「分频器」代码——这正是本模块最巧的地方：二进制自增本身就免费产生了 \( \text{WIDTH} \) 个不同分频比的方波。

`WIDTH` 参数同时决定了两件事（[clk_divider.sv:25-26](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clk_divider.sv#L25-L26)）：

- 你能拿到几个派生时钟（位数），
- 最慢的那个时钟有多慢（最高位 `out[WIDTH-1]` 的分频比是 /\(2^{\text{WIDTH}}\)）。

例如 `WIDTH=32` 时，最慢的 `out[31]` 是 /4294967296（约 43 亿）分频——在 100 MHz 主时钟下周期约 43 秒。这就是为什么仓库例化模板默认 `WIDTH(32)`：一次拿到从极快到极慢的一整条慢时钟谱。

#### 4.2.4 代码实践

**这是本讲的主实践任务**：例化 `WIDTH=8` 的 `clk_divider`，分别观察 `out[0]~out[7]`，写出每个位的分频比并在波形里验证。

**操作步骤**（示例代码，建为 `clk_divider_div8_tb.sv`）：

```systemverilog
`timescale 1ns / 1ps
module clk_divider_div8_tb();
  logic clk = 1'b0;
  always #5 clk = ~clk;             // 100 MHz

  logic nrst = 1'b0;
  initial begin #12 nrst = 1'b1; end

  logic [7:0] out;
  clk_divider #( .WIDTH( 8 ) ) dut (
    .clk( clk ), .nrst( nrst ), .ena( 1'b1 ), .out( out )
  );

  initial begin
    $dumpfile("div8.vcd"); $dumpvars;
    #3000 $finish;                 // 3000ns > out[7] 的一个完整周期(2560ns)
  end
endmodule
```

用 iverilog 编译运行（参考 [u1-l3](u1-l3-simulation-setup.md)）：`iverilog -g2012 -o sim clk_divider.sv clk_divider_div8_tb.sv && vvp sim`，然后用 GTKWave 打开 `div8.vcd`。

**需要观察的现象**：把 `out[7:0]` 展开成 8 条独立信号线，逐条测量其翻转周期。

**预期结果**：与 4.2.2 的分频比表完全一致——

| 位 | 实测周期（ns） | 推得分频比 |
|---|---|---|
| out[0] | 20 | /2 |
| out[1] | 40 | /4 |
| out[2] | 80 | /8 |
| out[3] | 160 | /16 |
| out[4] | 320 | /32 |
| out[5] | 640 | /64 |
| out[6] | 1280 | /128 |
| out[7] | 2560 | /256 |

每往下一位，周期恰好 ×2、频率恰好 ÷2。每条线占空比都是 50%。若本地无仿真器，标记「待本地验证」，但上表即为理论预期。

#### 4.2.5 小练习与答案

**练习 1**：主时钟 50 MHz，想让一个 LED 以大约 1 Hz 闪烁，应当选 `WIDTH` 和哪一位？

**答案**：\(1\text{ Hz} = 50\times10^6 / 2^{N+1}\)，解得 \(2^{N+1} \approx 5\times10^7\)，\(N+1 \approx 25.6\)，取 `out[25]`（/67108864 ≈ 0.745 Hz）或 `out[24]`（约 1.49 Hz）。`WIDTH` 至少要 26。本讲 4.3 会看到 Quartus 模板正是用 `div_clk125[25]`（125 MHz 下约 1.86 Hz）点灯。

**练习 2**：为什么说这些派生时钟「没有毛刺」？

**答案**：因为每一位都是 `always_ff` 里的寄存器输出（[clk_divider.sv:35-41](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clk_divider.sv#L35-L41)），由同一个 `clk` 沿更新，不存在组合逻辑的竞争冒险；并且是**同步计数器**（不是行波/异步计数器），各位同时跳变，不会有逐级延迟造成的中间态。

---

### 4.3 在工程中使用 clk_divider

#### 4.3.1 概念说明

知道原理之后，更重要的是学会「正确地」用它。本模块用两个真实例子讲两种最典型的、也是仓库推荐的用法：

1. **testbench 里的「派生时钟 → 单拍脉冲」**：在 `main_tb.sv` 里，把 `clk_divider` 的 32 位输出整体送进 `edge_detect` 阵列，得到 32 路主时钟域里的周期脉冲。
2. **上板工程里的「慢数据点灯 / 作数据源」**：在 Quartus 模板 `main.sv` 里，把计数器高位接到 LED 闪烁，把另一组计数位当成参与运算的数据。

注意两种用法有一个共同点——**都没有把 `out[N]` 接到任何 `always_ff` 的时钟端口**。这正是 4.2.2 说的「局限」的正面教材。

#### 4.3.2 核心流程

**testbench 用法**（`main_tb.sv`）：

```text
clk200(200MHz) ──> clk_divider(WIDTH=32) ──> DerivedClocks[31:0]
                                                    │
                                       edge_detect 阵列（在 clk200 域）
                                                    ▼
                                          E_DerivedClocks[31:0]  （每位一个单拍脉冲）
```

派生时钟的每一位经过 `edge_detect` 后，其上升沿被压缩成 `clk200` 域里**仅持续一拍**的脉冲。于是你得到 32 个频率各不相同、却全部同步在 `clk200` 上的周期事件源——可用来做随机种子、定时激励等。

**上板用法**（Quartus `main.sv`）：

```text
FPGA_CLK1_50 ──> sys_pll ──> clk125 ──> clk_divider ──> div_clk125[31:0] ──> LED[7]=div_clk125[25]（闪烁）
                            └─> clk500 ──> clk_divider ──> div_clk500[31:0] ──> 与输入数据异或（当数据用）
```

#### 4.3.3 源码精读

先看 testbench。`main_tb.sv` 用 `initial`/`forever` 产生 200 MHz 主时钟 `clk200`（[main_tb.sv:17-22](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/main_tb.sv#L17-L22)，半周期 #2.5ns），再产生一次性的 `nrst_once`（[main_tb.sv:53-61](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/main_tb.sv#L53-L61)），然后把 `clk_divider` 挂上去：

[main_tb.sv:63-71](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/main_tb.sv#L63-L71) —— 例化 `clk_divider #( .WIDTH(32) )`，输出 32 位 `DerivedClocks`。这是本模块在 testbench 里的标准用法。

紧接着就是「派生时钟 → 单拍脉冲」的关键一环：

[main_tb.sv:73-81](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/main_tb.sv#L73-L81) —— 用 `edge_detect ed1[31:0]` 这一**模块数组**，把 32 位 `DerivedClocks` 整体送进去，每个 `ed1[i]` 监测 `DerivedClocks[i]` 的上升沿，输出到 `E_DerivedClocks[i]`。结果是：第 `i` 路脉冲的频率 = \(f_{\text{clk200}}/2^{i+1}\)，且全部是 `clk200` 域里的单拍脉冲，后面还能拿来喂随机数发生器 `c_rand`（[main_tb.sv:84-98](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/main_tb.sv#L84-L98)）。这就是「不创建新时钟域、却得到多档周期事件」的范式。

再看上板工程。Quartus 模板先用 PLL（厂商 IP 黑盒 `sys_pll`）把板载 50 MHz 晶振倍频成 125 MHz 与 500 MHz（[example_projects/quartus_test_prj_template_v4/src/main.sv:77-83](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/src/main.sv#L77-L83)），然后挂两个 `clk_divider`：

[example_projects/quartus_test_prj_template_v4/src/main.sv:85-103](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/src/main.sv#L85-L103) —— `cd_125` 与 `cd_500` 两个 `clk_divider`，分别挂在 125 MHz 和 500 MHz 时钟上，输出 32 位 `div_clk125` 和 `div_clk500`。一次例化就把两套慢时钟谱都备好。

最直接的「慢数据点灯」用法只有一行：

[example_projects/quartus_test_prj_template_v4/src/main.sv:105](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/src/main.sv#L105) —— `assign LED[7] = div_clk125[25];`。125 MHz 下 `out[25]` ≈ 1.86 Hz，LED 每秒约闪两次，肉眼可见。注意这里用的是 `assign`（组合连接），把计数位当**数据**驱动 GPIO，而不是当时钟。

而把计数位「当数据源」参与运算的例子在测试逻辑里：

[example_projects/quartus_test_prj_template_v4/src/main.sv:158-161](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/src/main.sv#L158-L161) —— `out_data_comb = in_data_reg ^ div_clk500[31:0];`，把 `clk500` 的 32 位计数器与输入数据异或，产生一组不断变化的输出。`div_clk500` 在这里完全是一个「不断自增的数据源」，而不是时钟。

> 全仓库可以搜到：`clk_divider` 的 `out[N]` 从不被接到任何 `always_ff @(posedge ...)` 的时钟端口——这就是 4.2 节「局限」在真实工程里的体现。

#### 4.3.4 代码实践

**实践目标**：阅读型实践——在仓库里找到并解释「LED 闪烁频率」与「派生时钟脉冲」两条用法链。

**操作步骤**：

1. 打开 [main.sv:105](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/src/main.sv#L105)，确认 `LED[7]` 由 `div_clk125[25]` 驱动。
2. 计算：`clk125` = 125 MHz，`out[25]` 分频比 = \(2^{26}\) = 67108864，所以 LED 闪烁频率 = \(125\times10^6 / 67108864 \approx 1.86\) Hz。
3. 打开 [main_tb.sv:73-81](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/main_tb.sv#L73-L81)，确认 `E_DerivedClocks[i]` 是 `DerivedClocks[i]` 的上升沿脉冲，频率 = \(200\text{ MHz}/2^{i+1}\)。

**需要观察的现象**：两条链都把 `out[N]` 当作**数据或使能**，没有任何一处当作新时钟域。

**预期结果**：你能用自己的话讲清——为什么作者要点 `div_clk125[25]` 而不是 `div_clk125[5]`？（答：`[5]` 是 ~1.95 MHz，太快肉眼看不出闪烁；`[25]` 才落在 1~2 Hz 的人眼舒适区。）

#### 4.3.5 小练习与答案

**练习 1**：如果把 [main.sv:105](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/src/main.sv#L105) 改成 `LED[7] = div_clk125[5];`，LED 看起来会怎样？

**答案**：`out[5]` ≈ 1.95 MHz，远超人眼融合频率（约 60 Hz），LED 看起来会是**常亮**（亮度约减半，因为 50% 占空比），观察不到闪烁。要可见闪烁应选高位（如 `[23]~[26]`）。

**练习 2**：为什么 `main_tb.sv` 用 `edge_detect` 把派生时钟转成脉冲，而不是直接把 `DerivedClocks[i]` 当 `clk` 用？

**答案**：因为这样所有逻辑都留在**同一个 `clk200` 时钟域**里，`E_DerivedClocks[i]` 只是 `clk200` 域里每 \(2^{i+1}\) 拍拉高一次的使能脉冲，避免了创建 32 个新的派生时钟域，CDC 分析和时序约束都简单得多。这是「时钟使能（clock enable）」优于「派生时钟」的经典范例。

---

## 5. 综合实践

把本讲的三块知识串起来，做一个端到端的小验证：**用 `clk_divider` + `edge_detect` 复刻仓库 `main_tb.sv` 里的「多档周期脉冲」思路，并测量其中一路的频率。**

任务：

1. 写一个 testbench，主时钟 100 MHz，例化一个 `WIDTH=8` 的 `clk_divider` 得到 `out[7:0]`。
2. 用一个 `edge_detect`（参考 [edge_detect.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect.sv)，`WIDTH=1`）监测 `out[5]` 的上升沿，得到单拍脉冲 `pulse5`。
3. 在波形里：
   - 确认 `out[5]` 的周期是 640 ns（/64）；
   - 确认 `pulse5` 每隔 640 ns 出现一次、每次只持续一个 100 MHz 时钟周期（10 ns）；
   - 算出 `pulse5` 的频率应为 \(100\text{ MHz}/64 \approx 1.5625\) MHz。
4. 用一句话说明：为什么 `pulse5` 虽然频率只有 1.5625 MHz，却仍然属于 100 MHz 时钟域、不需要做 CDC？

> 评判标准：波形里 `out[5]` 与 `pulse5` 的周期、脉宽与上述预期一致；第 4 问能答出「因为脉冲是在 100 MHz 的 `always_ff` 里产生的使能信号，不是新的时钟」。若本地暂无仿真器，至少手绘 `out[5]` 与 `pulse5` 的时序图，并标注周期与脉宽，标注「待本地验证」。

---

## 6. 本讲小结

- `clk_divider` 的本质是一个**自由运行的二进制计数器**：每个时钟沿 `out <= out + 1`，复位低有效、同步、可选使能——仅此而已，没有任何专门的「分频」逻辑。
- 核心公式：第 `N` 位是主时钟的 \(1/2^{N+1}\) 分频，即 \(f_{\text{out}[N]} = f_{\text{clk}}/2^{N+1}\)，且 50% 占空比。
- 所有派生位共享同一个 `clk` 沿，因此**同步、相位一致、无毛刺**，相当于一棵一次成型的「时钟树」。
- **用途**：高位点 LED 闪烁、配合 `edge_detect` 生成主域里的单拍周期脉冲、当自增数据源参与运算。
- **局限**：只能 2 的幂次分频；且**不要把 `out[N]` 当成真正的时钟**去驱动 `always_ff`——仓库的推荐范式是当数据或时钟使能用，避免凭空增加时钟域。
- 例化时用 `#( .WIDTH(...) )` 决定能拿到几档慢时钟、最慢有多慢；仓库默认 `WIDTH=32`。

---

## 7. 下一步学习建议

- **下一讲 [u2-l2 边沿检测 edge_detect](u2-l2-edge-detect.md)**：本讲反复出现的 `edge_detect` 是 `clk_divider` 的最佳搭档。下一讲会讲清它如何用一级延迟寄存器比较得到 rising/falling/both 脉冲，以及 `generate` 在组合/寄存输出间的切换。
- 之后可读 [u2-l3 delay 同步链](u2-l3-delay-synchronizer.md)，看「移位寄存器延迟」与 SRL 推断，为 [u3 时钟域跨越](u3-l1-cdc-data.md) 的两级同步器打基础。
- 想看更多 `clk_divider` 的工程实例，可直接浏览 [example_projects/ 下各模板的 main.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/)，对比同一模块在 Quartus / Vivado / Gowin 工程里的写法。
