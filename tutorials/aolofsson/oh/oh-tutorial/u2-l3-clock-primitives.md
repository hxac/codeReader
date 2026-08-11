# 时钟控制原语

## 1. 本讲目标

本讲在 u2-l2（触发器家族）的基础上，从「记住一拍数据」升级到「管理时钟本身」。时钟是数字电路的心跳，但在真实芯片里我们经常需要：**让时钟停下来省电、把时钟变慢、在两个时钟之间安全切换**。学完本讲你应当能够：

- 说出门控时钟（clock gating）为什么不能直接用一个 `与门` 实现，以及无毛刺门控的标准做法。
- 读懂 `oh_clockgate` 的 soft/hard 双实现，并理解它与 `asic_clkicgand` 的对应关系。
- 读懂 `oh_clockdiv` 这个「相位可编程分频器」的工作流程：计数器、周期匹配、相位选择、稳定性握手。
- 理解 `oh_clockmux2/4`、`oh_clockmux`、`oh_clockor` 这一组时钟切换/合成原语，以及它们「为什么要求使能信号稳定」。
- 为后续 elink 的时钟域管理（`etx_clocks`/`erx_clocks`）建立概念基础。

## 2. 前置知识

在进入本讲前，你需要先具备以下概念（u2-l2 已建立大部分）：

- **时钟（clock）与边沿触发**：时序逻辑在时钟的上升沿（`posedge`）采样；时钟是一根在 0/1 之间周期翻转的特殊网络，整条数据通路都靠它对齐节拍。
- **低有效复位 `nreset`**：OH! 约定复位信号低有效（`!nreset` 时复位），见 u2-l2。
- **组合逻辑与时序逻辑的区别**：组合逻辑（`assign`）输出随输入立刻变化，没有记忆；时序逻辑靠时钟采样。本讲的「毛刺」问题正是组合逻辑带来的。
- **毛刺（glitch）**：由于信号到达时间略有差异，组合逻辑输出可能在稳定前出现短暂的错误跳变。对普通数据，毛刺危害不大（只要在时钟沿稳定即可）；**但对时钟网络，一个毛刺就是一个假时钟沿**，会让触发器多采一次数据，后果致命。
- **占空比（duty cycle）**：一个时钟周期里高电平所占的比例。理想方波占空比为 50%。
- **soft / hard 双实现**：OH! 同一功能在 stdlib（可综合 RTL）与 asiclib（绑定工艺库的标准单元）两套实现，用 `SYN` 参数切换，见 u1-l4 / u2-l2。

一句话直觉：**数据信号错了，最多算错一个数；时钟信号错了，整个电路会乱跳。** 所以本讲所有原语的核心目标只有一个——**让对时钟的任何操作都「干净、无毛刺」**。

## 3. 本讲源码地图

本讲涉及的关键文件（均位于 `stdlib/rtl/`，硬核对应物在 `asiclib/hdl/`）：

| 文件 | 作用 | 本讲角色 |
|------|------|----------|
| `stdlib/rtl/oh_clockgate.v` | 门控时钟：按 `en` 决定是否输出时钟，用于省功耗 | 4.1 主角 |
| `asiclib/hdl/asic_clkicgand.v` | 硬核 ICG（集成门控时钟）单元，门控的 hard 实现 | 4.1 对应物 |
| `stdlib/rtl/oh_lat0.v` | 低电平透明锁存器，被门控/分频内部用来「稳定使能」 | 4.1/4.2 辅助 |
| `stdlib/rtl/oh_clockdiv.v` | 相位可编程时钟分频器，两路输出 | 4.2 主角 |
| `stdlib/rtl/oh_clockmux2.v` / `oh_clockmux4.v` | 2:1 / 4:1 时钟多路选择器 | 4.3 主角 |
| `stdlib/rtl/oh_clockmux.v` | 参数化 N:1 时钟选择器（one-hot） | 4.3 补充 |
| `stdlib/rtl/oh_clockor.v` | 时钟「或」门，合并多路时钟脉冲 | 4.3 补充 |
| `stdlib/testbench/dut_clockdiv.v` | `oh_clockdiv` 的 DUT 测试包装（**已过时，见 4.2.4**） | 4.2 反面教材 |

## 4. 核心概念与源码讲解

### 4.1 门控时钟：oh_clockgate

#### 4.1.1 概念说明

在电池设备或大型 SoC 里，不是所有模块每时每刻都在工作。让一个空闲模块的时钟继续翻转，会持续消耗「动态功耗」（即每次翻转充放电寄生电容的能量）。**门控时钟（clock gating）** 的思路很朴素：模块不用的时候，把它的时钟关掉。

最直观的写法似乎是一个 `与门`：

```verilog
assign gated_clk = clk & en;   // ⚠ 危险写法
```

但这是错的。`en` 是普通数据信号，可能在 `clk` 为高的任意时刻发生变化；一旦 `en` 在 `clk` 高电平期间从 1 跳到 0，`gated_clk` 就会被「截断」出半个窄脉冲——这就是毛刺，会被下游触发器当成一个真实的时钟沿。

工业界标准做法叫 **ICG（Integrated Clock Gating，集成门控时钟单元）**：用一个**透明锁存器**把 `en` 抓住，并且只在 **`clk` 为低电平** 的窗口里更新锁存值。这样 `en` 的任何跳变都被「锁」在低电平段，等到下一个上升沿到来时，锁存输出早已稳定，`clk & en_stable` 就能产生干净的门控时钟。

门控单元通常还有一个 `te`（test enable，测试使能）输入：在芯片测试（扫描测试 DFT）时强制 `te=1`，保证时钟始终输出，不受功能 `en` 影响，否则扫描链会因时钟被关而断流。

#### 4.1.2 核心流程

`oh_clockgate` 的 soft 实现流程：

1. `en_sl = en | te` —— 先把功能使能和测试使能合并；`te=1` 时永远开门。
2. 用一个低电平透明锁存器 `oh_lat0` 抓住 `en_sl`：当 `clk==0` 时锁存器透明（输出跟随输入），当 `clk==1` 时保持。输出记为 `en_sh`（stable high）。
3. `eclk = clk & en_sh` —— 只在 `clk` 高电平且 `en_sh` 已稳定为 1 时输出高。

效果：`en` 的跳变只能影响 `en_sh` 的低电平段，等到上升沿时 `en_sh` 早已定值，于是 `eclk` 的上升沿永远是完整的、无毛刺的。

时序示意（文字波形）：

```
clk     _|‾|_|‾|_|‾|_|‾|_
en            _____|（在 clk 高电平期间跳变）
en_sl         _____|          ← 与 en 同样在高端变
en_sh   _____|‾‾‾‾‾‾‾        ← 被锁存，只在 clk 低段更新（实际在下一个低段生效）
eclk    _____|‾|‾|‾|‾        ← 干净的完整脉冲
```

（精确边沿关系建议用仿真确认，见 4.1.4。）

#### 4.1.3 源码精读

端口定义：[stdlib/rtl/oh_clockgate.v:8-17](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_clockgate.v#L8-L17) —— `clk`/`te`/`en` 输入，`eclk` 门控时钟输出。注意注释明确写了 `en` 必须来自 **正边沿触发器**（`from positive edge FF`），即 `en` 本身是寄存器输出、与时钟同步，这是无毛刺的前提。

soft 分支核心三行：[stdlib/rtl/oh_clockgate.v:26-33](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_clockgate.v#L26-L33) —— 即上面流程的 1-3 步。其中 `oh_lat0` 是关键：

[stdlib/rtl/oh_lat0.v:21-26](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_lat0.v#L21-L26) —— `always @(clk or in) if(!clk) out_reg <= in;`。这是一个电平敏感的透明锁存器：`clk` 为 0 时透明（`out` 跟随 `in`），`clk` 为 1 时保持。把它接在 `en` 上，就实现了「只在低电平段采样使能」的 ICG 行为。

hard 分支：[stdlib/rtl/oh_clockgate.v:36-44](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_clockgate.v#L36-L44) —— 当 `SYN!="TRUE"` 时例化 `asic_clockgate`。

> ⚠ **诚实的发现（待确认）**：本讲的硬核对应物在 asiclib 里实际存在的是 `asic_clkicgand`（见下），而 `oh_clockgate` 的 hard 分支引用的 `asic_clockgate` 在当前仓库里**找不到定义**（用 `Glob`/`Grep` 在 `asiclib/` 全目录未命中）。这与本手册反复强调的「文档/引用可能滞后、代码即事实」原则一致。学习门控的 hard 行为时，请以下面的 `asic_clkicgand` 为准。

`asic_clkicgand` 的实现与 soft 分支几乎逐行对应：[asiclib/hdl/asic_clkicgand.v:14-20](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_clkicgand.v#L14-L20) —— `always @(clk or en or te) if(~clk) en_stable <= en | te;` 再 `assign eclk = clk & en_stable;`。命名里的 `icg` = Integrated Clock Gating，`and` = 用与门合并。这就是 `oh_clockgate` 在 ASIC 流程中的「真身」。

#### 4.1.4 代码实践

**实践目标**：直观看到「`en=0` 时 `eclk` 停摆、`en=1` 时 `eclk` 与 `clk` 同相」，并确认 `en` 在高电平期间跳变不会产生窄脉冲。

**操作步骤**（源码阅读型 + 最小仿真，需先按 u1-l3 配好 iverilog 与 `OH_HOME`）：

1. 阅读上面的 [oh_clockgate.v soft 分支](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_clockgate.v#L19-L35)，确认 `en_sh` 只能在 `clk` 低电平段改变。
2. 自行写一个最小 testbench（**示例代码，非项目原有文件**）：

```verilog
// 示例代码：tb_clockgate.v （需用 -g2005、配合 stdlib 库编译）
`timescale 1ns/1ps
module tb_clockgate;
  reg clk = 0;
  reg en = 0;
  reg te = 0;
  wire eclk;

  oh_clockgate uut (.clk(clk), .te(te), .en(en), .eclk(eclk));

  always #5 clk = ~clk;           // 100MHz

  initial begin
    $dumpfile("wave.vcd"); $dumpvars(0, tb_clockgate);
    te = 0;
    en = 0; #40;                  // 关门段：观察 eclk 是否恒 0
    en = 1; #40;                  // 开门段：观察 eclk 是否跟随 clk
    en = 0; #20;                  // 再关门
    $finish;
  end
endmodule
```

3. 用 iverilog 编译并运行（路径与库请参照 u1-l3 的 `libs.cmd` 机制，把 `stdlib/rtl` 纳入 `-y` 搜索路径）。

**需要观察的现象**：

- `en=0` 期间：`eclk` 始终为 0（时钟被关断，无翻转）。
- `en=1` 期间：`eclk` 与 `clk` 完全同相，且 `en` 跳变瞬间 `eclk` 没有出现窄刺。

**预期结果 / 待本地验证**：`en` 的下降沿落在 `clk` 高电平段时，`eclk` 的当前高电平会被完整放行到本周期结束、下一个周期才真正停摆——这正是 ICG 的正确行为。精确的纳秒级边沿请用 gtkwave 打开 `wave.vcd` 确认（标注「待本地验证」）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `oh_clockgate` 里的 `oh_lat0` 去掉，直接写 `assign eclk = clk & en;`，在 `en` 从 1→0 且正好 `clk` 为高时会发生什么？

**参考答案**：`eclk` 会立刻从 1 跳到 0，把当前的高电平半周期「削」成一个窄脉冲。下游上升沿触发的触发器可能把这个下降沿之后的残留或下一个不完整高电平误判为时钟事件，导致多采一次数据。这就是必须用锁存器的原因。

**练习 2**：`te`（test enable）为什么在扫描测试时必须能强制为 1？

**参考答案**：扫描测试要把芯片里所有触发器串成一条移位寄存器逐位移入/移出数据，这要求每个触发器的时钟始终有效。若此时某个模块因功能 `en=0` 被门控关掉，扫描链就在这里断流。`te=1` 经 `en | te` 强制打开门控，保证测试期间时钟不缺。

---

### 4.2 时钟分频：oh_clockdiv

#### 4.2.1 概念说明

很多场景需要一个比主时钟更慢的时钟：给慢速外设、产生波特率、做时钟域之间的速率匹配。**分频器（clock divider）** 就是把输入时钟 `clk` 变成 `clk/N`。

最简单的分频是 `T` 触发器链（每来一个沿翻转一次，两级串起来就是 4 分频）。但 OH! 的 `oh_clockdiv` 做得更通用：它是一个 **相位可编程的分频器**，不只能 2 分频，还能任意整数分频，并且可以独立设定「输出在计数周期的哪一格拉高、哪一格拉低」，从而灵活控制占空比与相位。它还输出两路时钟（`clkout0`/`clkout1`），并把第二路做成可移相，便于产生 0°/90° 正交时钟。

为了让下游安全使用，分频器在每次参数变化后需要一个「稳定握手」：改变分频比期间输出可能不稳定，于是用一个计数器数够 8 个周期后才拉高 `clkstable`，告诉外部「现在的输出可以放心用了」。

#### 4.2.2 核心流程

`oh_clockdiv` 的核心是「计数器 + 周期匹配 + 相位选择 + 输出寄存器 + 时钟 mux」。伪代码：

```
每个 clk 上升沿（clken=1 时）:
    if counter == clkdiv:   counter <= 0      // 周期匹配，归零
    else:                   counter <= counter + 1

    // 相位选择：在指定计数值上置位/清零输出
    if counter == rise0:    clkout0_reg <= 1
    else if counter == fall0: clkout0_reg <= 0

clkout0 = (clkdiv==0) ? clk          // 0 == 旁路，直接输出原时钟
                     : clkout0_reg   // 否则输出分频后的方波
```

关键参数语义（见源码注释 `0==bypass, 1=div/2, 2=div/3, ...`）：当 `clkdiv >= 1` 时，计数器在 `0..clkdiv` 之间循环（共 `clkdiv+1` 个值），所以分频比为

\[
N = \text{clkdiv} + 1
\]

即 `clkdiv=1` → 2 分频，`clkdiv=3` → 4 分频。`clkdiv=0` 是旁路（bypass），直接把 `clk` 送出。

`clkphase0` 把 16 位拆成两段：低 8 位 `[7:0]` 指定「在哪个计数值置位（rise）」，高 8 位 `[15:8]` 指定「在哪个计数值清零（fall）」。于是占空比由 rise/fall 两个格点的距离决定。

稳定性握手：`clkchange` 拉高后会清零一个 3 位 `period` 计数器，每过一个 `period_match`（一个完整分频周期）加 1，数到 `3'b111`（8 个周期）才令 `clkstable=1`。

#### 4.2.3 源码精读

端口与参数：[stdlib/rtl/oh_clockdiv.v:9-32](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_clockdiv.v#L9-L32) —— 注意 `clkdiv[7:0]`、`clkphase0[15:0]`、`clkphase1[15:0]` 这几个配置口，以及 `clkchange`/`clken`/`clkstable` 这组握手信号。

稳定性握手（change detect）：[stdlib/rtl/oh_clockdiv.v:50-58](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_clockdiv.v#L50-L58) —— 数 8 个周期后 `clkstable = (period==3'b111)`。

周期计数器：[stdlib/rtl/oh_clockdiv.v:64-73](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_clockdiv.v#L64-L73) —— `counter` 在 `period_match` 时归零，否则加 1；`period_match = (counter == clkdiv)`。

相位选择：[stdlib/rtl/oh_clockdiv.v:79-82](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_clockdiv.v#L79-L82) —— 把 `counter` 与 `clkphase0/1` 的各字节比较，产生 `clkrise0/clkfall0/clkrise1/clkfall1` 四个匹配脉冲。

`clkout0` 的生成与旁路 mux：[stdlib/rtl/oh_clockdiv.v:88-116](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_clockdiv.v#L88-L116) —— `clkout0_reg` 在 `clkrise0` 置位、`clkfall0` 清零；随后用 `oh_clockmux2` 在「分频时钟 `clkout0_reg`」与「旁路原时钟 `clk`」之间选择，选择信号 `clk0_sel` 同样经过 `oh_lat0` 稳定化处理（避免在时钟选择时引入毛刺）。这里**分频器内部就用到了 4.3 的无毛刺时钟 mux 与低电平锁存**，三个原语是环环相扣的。

`clkout1` 多了一级移相：[stdlib/rtl/oh_clockdiv.v:122-165](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_clockdiv.v#L122-L165) —— 用 `negedge clk` 打一拍得到 `clkout1_shift`，再用 4:1 mux 在「常规分频/移相分频/旁路」之间选，用于产生与 `clkout0` 正交的第二路时钟。

> ⚠ **诚实的发现（待确认）**：仓库里有一个 [stdlib/testbench/dut_clockdiv.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dut_clockdiv.v#L40-L46) 测试包装，但它例化 `oh_clockdiv` 时用的端口（`.clkout / .clkout90 / .en / .divcfg`）与当前源码的实际端口（`clkout0 / clkout1 / clken / clkdiv` 等）**完全对不上**，直接编译会报端口找不到。这是历史遗留，说明该测试包装已与当前 `oh_clockdiv` 接口脱节。做下面实践时请以源码实际端口为准，不要照搬这个过时文件。

#### 4.2.4 代码实践

**实践目标**：让 `oh_clockdiv` 产生一个 4 分频（`clkout0` 频率 = `clk` 的 1/4）、50% 占空比的时钟，并在波形上数出周期。

**操作步骤**（源码阅读 + 最小仿真）：

1. 配置参数选择：要 4 分频，由 \(N=\text{clkdiv}+1\) 得 `clkdiv = 8'd3`。
2. 要 50% 占空比：让输出在计数 0 时置位、计数 2 时清零（高 2 拍、低 2 拍）。即 `clkphase0[7:0]=8'h00`（rise@0），`clkphase0[15:8]=8'h02`（fall@2），合写为 `clkphase0 = 16'h0200`。
3. 最小 testbench（**示例代码**）：

```verilog
// 示例代码：tb_clockdiv4.v （需 -g2005 与 stdlib 库）
`timescale 1ns/1ps
module tb_clockdiv4;
  reg clk = 0;
  reg nreset = 0;
  wire clkout0, clkout1, clkstable;

  oh_clockdiv #(.N(2)) uut (
    .clk(clk), .nreset(nreset),
    .clkchange(1'b0), .clken(1'b1),
    .clkdiv(8'd3),                  // 4 分频
    .clkphase0(16'h0200),           // rise@0, fall@2
    .clkphase1(16'h0200),
    .clkout0(clkout0), .clkrise0(), .clkfall0(),
    .clkout1(clkout1), .clkrise1(), .clkfall1(),
    .clkstable(clkstable)
  );

  always #5 clk = ~clk;

  initial begin
    $dumpfile("wave.vcd"); $dumpvars(0, tb_clockdiv4);
    #12 nreset = 1;                // 释放复位
    #200 $finish;
  end
endmodule
```

**需要观察的现象**：

- `clkout0` 的周期是 `clk` 的 4 倍（`clk` 每 4 个上升沿，`clkout0` 完成一个完整周期）。
- 高电平持续 2 个 `clk` 周期，低电平持续 2 个 `clk` 周期（50% 占空比）。
- 复位释放后，`clkstable` 经过约 8 个分频周期后拉高。

**预期结果 / 待本地验证**：波形应呈现稳定 4 分频。若占空比不对，多半是 `clkphase0` 的字节顺序填反（`[7:0]` 是 rise、`[15:8]` 是 fall）。精确周期用 gtkwave 测量确认（标注「待本地验证」）。

#### 4.2.5 小练习与答案

**练习 1**：想要 3 分频（`clkdiv=2`）且占空比 1/3（高 1 拍、低 2 拍），`clkphase0` 应填什么？

**参考答案**：计数器循环 0,1,2 三值。高 1 拍即在计数 0 置位、计数 1 清零：rise 字节 `8'h00`，fall 字节 `8'h01`，故 `clkphase0 = 16'h0100`。

**练习 2**：`clkchange` 拉高后，`clkstable` 为什么要等 8 个周期才恢复？

**参考答案**：分频器在切换分频比时，计数器与输出寄存器处于过渡态，可能产生不完整的周期或相位偏移。等待 8 个完整分频周期足以让计数器回到稳态循环、让锁存的选择信号稳定，之后输出才是「干净且周期确定」的，可以安全交给下游使用。

---

### 4.3 时钟切换与合成：oh_clockmux2/4、oh_clockmux、oh_clockor

#### 4.3.1 概念说明

很多系统需要在运行时在两个或多个时钟源之间切换（例如在「低速晶振」与「高速 PLL」之间切换，或在正常时钟与调试时钟之间切换）。直觉上用一个普通 `mux` 选一下就行——但这又是一个毛刺陷阱：

如果选择信号 `sel` 在两个时钟都为高电平的时刻发生切换，输出端可能短暂地**同时**或**都不**连接到任一时钟的高电平，产生窄刺。安全切换的标准做法是 **one-hot 使能 + 或门合成**：为每路时钟配一个「使能」位，要求任意时刻**最多只有一个使能为 1**（one-hot），并且这个使能向量已经过稳定化（无毛刺）；输出 = 各路 `（使能 & 时钟）` 的按位或。这样切换发生时，被关掉的那路先因为 `en→0` 而停止贡献，被打开的那路才从自己的下一个完整上升沿开始贡献——毛刺被规避。

`oh_clockor` 则是「时钟或门」：把多路时钟用或门合并成一路，常用于「任一时钟有脉冲，输出就有脉冲」的事件合并场景（例如把多个异步到来的单周期脉冲合并成一条中断线）。

#### 4.3.2 核心流程

- **oh_clockmux2**（2:1）：`clkout = (en0 & clkin0) | (en1 & clkin1)`，要求 `en0/en1` 稳定且互斥。
- **oh_clockmux4**（4:1）：把上式扩展到 4 路。
- **oh_clockmux**（N:1，参数化）：`clkout = |(clkin & en)`，`en` 是 one-hot 位掩码。
- **oh_clockor**（N 输入或）：`clkout = |clkin`，无条件合并。

切换两路时钟的安全流程（与 4.1 的门控同源）：

```
1. 把「选择哪一路」编码成 one-hot 使能向量 en[N-1:0]。
2. 用低电平透明锁存器（oh_lat0）在目标时钟的低电平段把 en 抓稳。
3. clkout = | (en & clkin)  —— 各路分别 AND 再 OR。
```

这与门控时钟本质同构：门控是「1 路时钟 × 1 个使能」，时钟 mux 是「N 路时钟 × N 个使能」。所以 4.1 学的 ICG 原理在这里直接推广。

#### 4.3.3 源码精读

`oh_clockmux2` soft 分支：[stdlib/rtl/oh_clockmux2.v:22-25](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_clockmux2.v#L22-L25) —— 注释特别强调 `en0/en1` 必须是 `stable high`（稳定高）。端口注释见 [oh_clockmux2.v:13-19](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_clockmux2.v#L13-L19)。

`oh_clockmux4` 同构的 4 路版本：[stdlib/rtl/oh_clockmux4.v:26-31](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_clockmux4.v#L26-L31) —— 四个 `(en & clkin)` 项相或。

参数化 `oh_clockmux`：[stdlib/rtl/oh_clockmux.v:20-22](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_clockmux.v#L20-L22) —— `assign clkout = |(clkin & en);`，注释再次强调 `one hot` 与 `only one is active`。

`oh_clockor`：[stdlib/rtl/oh_clockor.v:19-21](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_clockor.v#L19-L21) —— 直接 `assign clkout = |(clkin);`，没有使能，纯合并。

> 旁注（待确认）：和 4.1 一样，这些时钟 mux 的 hard 分支会例化 `asic_clockmux2/4` 等单元；当前 `asiclib/` 里实际存在的是 `asic_clkmux2.v` 等以 `asic_clk*` 命名的硬核（注意命名差异：源码引用 `asic_clockmux2`，库文件名是 `asic_clkmux2`）。学习 soft 行为不受影响；若走 ASIC 流程需核对实际可用的硬核名。

#### 4.3.4 代码实践

**实践目标**：用 `oh_clockmux2` 在两个不同频率的时钟之间切换，观察「使能稳定且互斥」时输出无毛刺地完成切换。

**操作步骤**（源码阅读型）：

1. 阅读 [oh_clockmux2 soft 分支](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_clockmux2.v#L22-L25)，确认它是 `en0&clkin0 | en1&clkin1`。
2. 设想：`clkin0` = 慢时钟（如 4 分频），`clkin1` = 快时钟（原速）；初始 `en0=1, en1=0`；某时刻令 `en0=0, en1=1` 完成切换。
3. 在脑中或纸上画出：切换前 `clkout` 是慢时钟，切换后是快时钟，且切换瞬间没有半高窄刺——因为两路 en 必须先稳定。

**需要观察的现象**：切换发生在某个时钟的低电平段之后，输出从一个时钟干净地交接到另一个时钟。

**预期结果 / 待本地验证**：若违反「one-hot / 稳定」约定（例如 `en0` 与 `en1` 同时为 1，或 en 带毛刺），`clkout` 会出现窄刺或频率混叠。可仿照 4.1.4 写一个最小 testbench 用 gtkwave 验证（标注「待本地验证」）。

#### 4.3.5 小练习与答案

**练习 1**：`oh_clockmux2` 的两个使能 `en0`、`en1` 能否同时为 1？

**参考答案**：不建议。同时为 1 时 `clkout = clkin0 | clkin1`，两路时钟会叠加，输出频率与形状都不可预期，失去「切换」语义。设计上必须保证使能 one-hot（至多一个为 1），并把使能信号经过锁存稳定化。

**练习 2**：`oh_clockor` 与 `oh_clockmux` 都是「或」运算，区别在哪？

**参考答案**：`oh_clockor` 是无条件或（直接 `|clkin`），用于合并多路时钟脉冲；`oh_clockmux` 是带 one-hot 使能的条件或（`|(clkin & en)`），用于在多路时钟中**选一路**输出。前者没有「选择」概念，后者有。

---

## 5. 综合实践

把本讲三个最小模块串起来：**用 `oh_clockdiv` 产生一个 4 分频时钟，再交给 `oh_clockgate` 门控，最后用 `oh_clockmux2` 在「分频时钟」与「旁路原时钟」之间切换**。

**任务**：

1. 按 4.2.4 的参数（`clkdiv=3`、`clkphase0=16'h0200`）让 `oh_clockdiv` 产出 4 分频 `clkout0`。
2. 把 `clkout0` 接到 `oh_clockgate.clk`，用一个激励寄存器驱动 `en`：先 `en=1` 跑若干周期，再 `en=0`。
3. （可选进阶）再用 `oh_clockmux2`，`clkin0=clkout0`（分频）、`clkin1=clk`（原速），用一个稳定的 one-hot 使能在两者间切换。
4. 用 gtkwave 观察 `clk`、`clkout0`、`eclk`（门控输出）、`clkout`（mux 输出）四条波形。

**需要观察与记录**：

- 4 分频关系是否成立。
- `en=0` 时门控输出是否干净停摆（无残留窄刺）。
- mux 切换时是否无毛刺。

**最小连线示意**（示例代码，仅描述连接关系）：

```verilog
// 示例代码：综合实践连线骨架（端口以源码为准，省略复位/相位细节）
oh_clockdiv  div  (/* clkdiv=3, clkphase0=16'h0200 */, .clkout0(div_clk));
oh_clockgate gate (.clk(div_clk), .en(en), .te(1'b0), .eclk(gated_clk));
oh_clockmux2 sw   (.en0(en_div), .en1(en_raw), .clkin0(gated_clk), .clkin1(clk), .clkout(clkout));
```

预期：`en=0` 时 `gated_clk` 停摆，`clkout` 经 mux 选择后相应停摆或切到原速；切换与门控瞬间均无窄刺。精确波形**待本地验证**。

## 6. 本讲小结

- **门控时钟不能直接 `clk & en`**：`en` 在高电平段的跳变会削出毛刺。正确做法是用低电平透明锁存器在 `clk` 低段把 `en` 抓稳，再与 `clk` 相与——这就是 ICG。`oh_clockgate` 的 soft 分支与硬核 `asic_clkicgand` 实现一致。
- **`oh_clockdiv` 是相位可编程分频器**：用计数器 + `period_match` 设定周期，用 `clkphase0/1` 的两段字节分别指定置位/清零格点，从而任意整数分频且占空比可配；分频比 \(N=\text{clkdiv}+1\)。
- **安全切换时钟的本质与门控同构**：one-hot 使能 + 各路 `(en & clk)` 相或；`oh_clockmux2/4` 与参数化 `oh_clockmux` 都要求使能「稳定且互斥」。
- **`oh_clockor`** 是无条件或门，用于合并多路时钟脉冲，没有选择语义。
- **稳定化是共同主题**：门控、分频、切换都用 `oh_lat0`（低电平透明锁存）把控制信号锁在低段，确保上升沿前已稳定。
- **诚实提醒**：仓库中 `oh_clockgate`/时钟 mux 的 hard 分支引用的若干 `asic_clock*` 单元在当前 `asiclib/` 里名称对不上或缺失，`stdlib/testbench/dut_clockdiv.v` 也与现行端口脱节——学习时一律以 RTL 源码实际端口为准。

## 7. 下一步学习建议

- 本讲只解决了「单时钟域内的关断、分频、切换」。一旦信号要**从一个时钟域跨到另一个时钟域**，就要面对亚稳态问题——这正是下一讲 **u2-l4 跨时钟域同步原语**（`oh_dsync`/`oh_rsync`/`oh_pulse2pulse`/`oh_edge2pulse`）的主题，建议紧接着学。
- 这些时钟原语建立的概念（门控、分频、无毛刺切换、稳定握手）在 **第 7 单元 elink 高速链路** 的 `etx_clocks.v` / `erx_clocks.v` 中会以更复杂的形式重现（发送时钟对齐、时钟数据恢复）。注意：elink 的时钟模块并不直接例化本讲的 stdlib 原语，而是使用 `asic_clk*` 与 `xilibs` 的 PLL 模型，但其底层思路与本章一致。
- 想立刻动手的读者，可回到 **u1-l3** 复习 `build.sh`/`sim.sh`/`view.sh` 三步流程，把本讲的示例 testbench 真正跑出 VCD 并用 gtkwave 看波形。
