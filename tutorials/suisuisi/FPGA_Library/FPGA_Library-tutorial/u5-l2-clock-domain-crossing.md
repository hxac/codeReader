# 时钟生成与跨时钟域同步器

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 FPGA 为什么不能直接拿晶振原始时钟「到处接」，而要用 **PLL/MMCM** 这类硬核原语生成所需频率，并能从 projf 的 `clock_480p.sv` 参数反算出输出频率。
- 理解 **亚稳态（metastability）** 是什么、为什么跨时钟域时会发生、为什么「打两拍（两级触发器同步器）」能极大降低失效率。
- 读懂 projf 的跨时钟域模块 [`xd.sv`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xd.sv#L1-L25)，看清它并不是教科书里最简单的两级触发器同步器，而是一个**脉冲型（toggle）CDC**：把源时钟域的单周期脉冲，安全地翻译成目的时钟域的单周期脉冲。
- 能够亲手画出同步器结构，并解释为什么单级触发器在跨时钟域时不安全。

## 2. 前置知识

在进入源码之前，先用最直白的方式建立三个直觉。

### 2.1 时钟是「节拍」，FPGA 内部可以有好几套节拍

开发板上焊了一颗晶振，比如 100 MHz，它给整块芯片提供一个稳定的方波节拍。但芯片里不同的模块常常需要不同的节拍：显示控制器要 25.2 MHz 的像素时钟，DVI/HDMI 编码要 5 倍于此的 126 MHz，而处理子系统可能要 125 MHz。我们不可能为每个频率都焊一颗晶振，于是用片内的 **PLL（锁相环）** 或 **MMCM（混合模式时钟管理器）** 把输入频率「倍频/分频」成想要的各路频率。projf 把这部分封装在 `lib/clock/` 下。

### 2.2 「时钟域」与「跨时钟域」

工作在同一棵时钟树下的所有触发器，共享同一个节拍，属于同一个 **时钟域（clock domain）**。当「时钟域 A」里的一个信号要被「时钟域 B」里的触发器采样时，A 的跳变沿对 B 来说是**异步**的——它随时可能落在 B 时钟沿的任意时刻。这件事就叫 **跨时钟域（Clock Domain Crossing, CDC）**。CDC 是 FPGA 设计里最隐蔽也最容易翻车的地方：代码看起来「没问题」，综合能过，但偶尔会偶发性出错。

### 2.3 亚稳态：触发器「犹豫了」

一个 D 触发器在时钟沿到来时采样输入。如果输入恰好在时钟沿附近（违反了建立/保持时间 `setup/hold`），触发器的输出既不是干净的 0 也不是干净的 1，而是停留在半电压的「中间态」，并可能停留较长时间才随机塌缩成 0 或 1。这就是 **亚稳态（metastability）**。它的危害是：下一级电路可能把这个「半电压」当作 0，也可能当作 1，甚至让不同扇出看到不同的值，导致整个系统状态错乱。

本讲的两个最小模块正是在处理这两件事：**PLL 原语**负责「生成节拍」，**xd 同步器**负责「安全地把信号从一个节拍搬到另一个节拍」。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `ThreePart/projf-explore/lib/clock/` 下：

| 文件 | 作用 |
| --- | --- |
| [clock/xd.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xd.sv#L1-L25) | 跨时钟域同步器（脉冲型 CDC），本讲主角之一 |
| [clock/xc7/clock_480p.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xc7/clock_480p.sv#L1-L69) | Xilinx 7 系列：用 `MMCME2_BASE` 把 100 MHz 生成 25.2 MHz 像素时钟 + 126 MHz 5x 时钟 |
| [clock/ice40/clock_480p.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/ice40/clock_480p.sv#L1-L45) | Lattice iCE40：用 `SB_PLL40_PAD` 把 12 MHz 生成 25.125 MHz 像素时钟 |
| [clock/xc7/clock_sys.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xc7/clock_sys.sv#L1-L66) | Xilinx 7 系列：生成 125 MHz 通用系统时钟（MMCM 单输出例） |
| [clock/xc7/xd_tb.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xc7/xd_tb.sv#L1-L79) | xd 的 testbench，同时测试「慢→快」与「快→慢」两个方向 |
| [clock/README.md](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/README.md#L1-L52) | clock 分区总览，列出全部模块与「等 lock 再用时钟」的纪律 |

> 提醒：本讲承接 [u5-l1](u5-l1-verilog-library-overview.md)。projf 库的厂商中立思想（纯逻辑模块放分区根目录、必须用硬核原语的功能放进 `xc7/` 与 `ice40/` 双实现、对外端口尽量一致）在这里体现得最明显——`clock_480p` 在两个平台同 名同端口，但内部原语完全不同。

## 4. 核心概念与源码讲解

### 4.1 时钟生成原语：PLL / MMCM

#### 4.1.1 概念说明

PLL（Phase-Locked Loop，锁相环）是一种**模拟+数字混合的硬核电路**，FPGA 厂商已经把它做成片内固定原语（primitive），你不能用 Verilog「写」出一个真正的 PLL，只能**例化**它。它的核心能力是：

- **倍频/分频**：从一个输入时钟生成一个或多个不同频率的输出时钟。
- **相位对齐**：通过反馈让输出时钟与输入时钟保持固定的相位关系。
- **去抖动**：过滤输入时钟的抖动（jitter），输出更干净的时钟。
- **锁定指示**：输出一个 `LOCKED` 信号，稳定后拉高，告诉系统「时钟可用了」。

Xilinx 7 系列（XC7）用的是 `MMCME2_BASE`，文档在 Xilinx UG472；Lattice iCE40 用的是 `SB_PLL40_PAD`，文档在 Lattice TN1251。这两者数学模型相似：都有一级「参考分频」、一级「反馈倍频」、一级「输出分频」。

> 术语小贴士：**MMCM**（Mixed-Mode Clock Manager）比纯 PLL 多了细调相移和动态重配等功能，但最基本的「频率合成」用法与 PLL 一样。projf 里就用 `MMCME2_BASE` 这个最简包装。

#### 4.1.2 核心流程

XC7 的 MMCM 频率合成可以拆成两步公式。设输入频率为 \(f_{\text{in}}\)，则压控振荡器（VCO）频率为：

\[
f_{\text{VCO}} = f_{\text{in}} \times \frac{\text{MULT\_MASTER}}{\text{DIV\_MASTER}}
\]

每路输出再各自分频：

\[
f_{\text{CLKOUT}n} = \frac{f_{\text{VCO}}}{\text{CLKOUT}n\_\text{DIVIDE}}
\]

以 `clock_480p`（XC7）为例，要得到 640×480@60Hz 显示所需的 25.2 MHz 像素时钟，以及给 TMDS 编码用的 5×＝126 MHz：

1. 选 `MULT_MASTER = 31.5`、`DIV_MASTER = 5` → VCO = \(100 \times 31.5 / 5 = 630\) MHz。
2. `CLKOUT1_DIVIDE = 25` → 像素时钟 \(630/25 = 25.2\) MHz。
3. `CLKOUT0_DIVIDE_F = 5` → 5× 时钟 \(630/5 = 126\) MHz。

MMCM 的反馈环路要「闭环」：把 `CLKFBOUT` 直接连回 `CLKFBIN`（片内反馈），PLL 才能锁相。生成的时钟必须经过 **BUFG（全局时钟缓冲器）** 再送到全局时钟网络，以保证整块芯片的低偏斜（low-skew）分发。

iCE40 的 `SB_PLL40_PAD` 思路一样但参数化方式不同，用 `DIVR`（参考分频）、`DIVF`（反馈倍频）、`DIVQ`（输出 2 的幂分频）。从 12 MHz 得 25.125 MHz 的关系（按本设计的参数取值）：

\[
f_{\text{out}} = f_{\text{in}} \times \frac{\text{DIVF}+1}{(\text{DIVR}+1)\times 2^{\text{DIVQ}}}
   = 12\,\text{MHz} \times \frac{66+1}{1 \times 2^{5}} = 25.125\,\text{MHz}
\]

（`DIVF`/`DIVR` 的精确寄存器语义以 Lattice TN1251 为准；上面的算式能复现源码注释声明的 25.125 MHz，可作为校验。）

无论哪种平台，projf 都遵循一条纪律（见 README）：

```verilog
always_ff @(posedge clk_pix) rst_pix <= !clk_pix_locked;  // wait for clock lock
```

也就是 **等 PLL 锁定（lock）后才让下游模块开始工作**，避免在时钟未稳定时输出垃圾信号。

#### 4.1.3 源码精读

XC7 版本的参数与原语例化在 `clock_480p.sv`：

- [clock_480p.sv:19-23](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xc7/clock_480p.sv#L19-L23)：用 `localparam` 集中放频率参数（`MULT_MASTER=31.5`、`DIV_MASTER=5`、`DIV_5X=5.0`、`DIV_1X=25`），方便换分辨率时只改这几行。
- [clock_480p.sv:30-57](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xc7/clock_480p.sv#L30-L57)：例化 `MMCME2_BASE`。注意 `CLKFBOUT(feedback)` 与 `CLKFBIN(feedback)` 自连形成闭环；`CLKOUT0` 输出 5× 时钟、`CLKOUT1` 输出像素时钟；大量未用输出端口（`CLKOUT0B()` 等）留空，并用 `/* verilator lint_off PINCONNECTEMPTY */` 关掉 Verilator 的空连接告警。
- [clock_480p.sv:60-61](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xc7/clock_480p.sv#L60-L61)：两路输出分别经过一个 `BUFG` 再对外暴露，这就是「全局时钟缓冲」。
- [clock_480p.sv:63-68](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xc7/clock_480p.sv#L63-L68)：把 MMCM 的 `LOCKED` 信号**两级同步到像素时钟域**后，才作为 `clk_pix_locked` 对外输出。这本身就是一个「打两拍」同步器——`LOCKED` 来自 MMCM、相对 `clk_pix` 异步，故先同步再用。

iCE40 版本换成 `SB_PLL40_PAD`，结构更短：

- [ice40/clock_480p.sv:18-22](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/ice40/clock_480p.sv#L18-L22)：iCE40 的参数（`FEEDBACK_PATH="SIMPLE"`、`DIVR/DIVF/DIVQ/FILTER_RANGE`）。
- [ice40/clock_480p.sv:25-37](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/ice40/clock_480p.sv#L25-L37)：例化 `SB_PLL40_PAD`，`PACKAGEPIN` 接 12 MHz 输入，`PLLOUTGLOBAL` 直接把 PLL 输出送上全局时钟网络（iCE40 不需要像 XC7 那样显式例化 BUFG），`LOCK` 给出锁定状态。
- [ice40/clock_480p.sv:39-44](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/ice40/clock_480p.sv#L39-L44)：与 XC7 版同样的「lock 两级同步」收尾。

对比 [clock_sys.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xc7/clock_sys.sv#L27-L54)（单输出 125 MHz）与 [clock_480p.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xc7/clock_480p.sv#L30-L57)（双输出 25.2/126 MHz），可以看到 MMCM 「同一 VCO、多路 CLKOUT 各自分频」的典型用法：`clock_sys` 只用 `CLKOUT0`，`clock_480p` 同时用 `CLKOUT0/CLKOUT1`。

#### 4.1.4 代码实践

1. **实践目标**：用频率公式反算输出，验证你理解了 MMCM 参数。
2. **操作步骤**：
   - 打开 [clock_720p.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xc7/clock_720p.sv#L19-L23)，记下 `MULT_MASTER=37.125`、`DIV_MASTER=5`、`DIV_5X=2.0`、`DIV_1X=10`。
   - 用上面的两步公式算 VCO、像素时钟、5× 时钟。
3. **需要观察的现象**：你算出的像素时钟应当是 74.25 MHz（对应 1280×720@60Hz），5× 时钟是 371.25 MHz。
4. **预期结果**：\(f_{\text{VCO}} = 100 \times 37.125 / 5 = 742.5\) MHz；像素 \(742.5/10 = 74.25\) MHz；5× \(742.5/2 = 371.25\) MHz。与文件头注释「74.25 MHz / 371.25 MHz」一致。
5. 若手头有 Vivado，可把 `clock_480p.sv` 设为顶层、跑 [clock_tb.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xc7/clock_tb.sv#L1-L1) 仿真，量 `clk_pix` 周期是否约 39.7 ns（1/25.2 MHz）。无 Vivado 则标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `clock_480p.sv` 里要 `BUFG`，而 iCE40 版没有显式的 `BUFG`？

> **参考答案**：XC7 的 MMCM 输出是普通布线信号，必须显式经过 `BUFG`（全局时钟缓冲）才能进入低偏斜的全局时钟树；iCE40 的 `SB_PLL40_PAD` 有 `PLLOUTGLOBAL` 专用端口，直接把 PLL 输出送上全局网络，省去了显式缓冲器。两者目的相同——保证全芯片时钟偏斜最小。

**练习 2**：把 `clock_sys.sv`（生成 125 MHz）的参数代入公式，验证它的输出。

> **参考答案**：`MULT_MASTER=10.0`、`DIV_MASTER=1.0` → VCO = \(100 \times 10 / 1 = 1000\) MHz；`CLKOUT0_DIVIDE_F=8` → \(1000/8 = 125\) MHz。与注释一致。

---

### 4.2 亚稳态与两级触发器同步器

#### 4.2.1 概念说明

第 2.3 节已经说了亚稳态是「触发器在违规采样时犹豫」。在**同一个时钟域内**，综合工具会检查所有路径是否满足建立/保持时间，因此正常同步设计几乎不会亚稳态。但在 **CDC** 处，源域信号相对目的时钟是异步的，永远有可能撞上时钟沿——这是**统计上必然发生、只是罕见**的事件，无法靠时序约束消除。

衡量「罕见到什么程度」的指标是 **MTBF（平均无故障时间）**。同步器的 MTBF 常用如下形式表达（常数与器件工艺相关）：

\[
\text{MTBF} = \frac{e^{\,t_r / \tau}}{W \cdot f_{\text{clk}} \cdot f_{\text{data}}}
\]

其中 \(t_r\) 是给亚稳态留的「恢复时间」，\(\tau\)、\(W\) 是触发器工艺常数，\(f_{\text{clk}}\) 是目的时钟频率，\(f_{\text{data}}\) 是数据跳变频率。注意 \(t_r\) 在指数上——**多留一个时钟周期的恢复时间，MTBF 会指数级增长**。这正是「打两拍」的数学依据。

#### 4.2.2 核心流程

教科书式的 **两级触发器同步器（two-flop synchronizer，打两拍）** 结构：

```text
   async_in ──► [FF1 @ clk_dst] ──► [FF2 @ clk_dst] ──► synced_out
                     ↑ 可能亚稳态        ↑ 多一拍让它塌缩，输出基本干净
```

伪代码：

```
always_ff @(posedge clk_dst) begin
    sync_ff1 <= async_in;   // 第一拍：可能亚稳态
    sync_ff2 <= sync_ff1;   // 第二拍：给整个 clk_dst 周期让它稳定
end
// 用 sync_ff2，不要用 sync_ff1
```

要点：

1. 第一级触发器 FF1 承担「冒险采样」，可能进入亚稳态。
2. 第二级触发器 FF2 再等一个完整目的时钟周期，让亚稳态有充足时间塌缩成稳定的 0/1。
3. 永远只用 FF2 的输出驱动后续逻辑，绝不直接用 FF1。
4. 这种同步器**只适合「单 bit 电平信号」**；多 bit 总线不能用简单打两拍（各位分别塌缩成不同值会得到非法组合），需要握手或异步 FIFO——本讲的 `xd` 就是握手型思想的一种。

> 诚实提示：projf 的 `xd.sv` 并不是上面这个最简单的两级触发器同步器。它解决的是更难的问题——传递「单周期脉冲」，所以内部用一个 4 级移位寄存器来同时承担「同步」与「脉冲重建」。第 4.3 节会专门讲它。但理解这一节的打两拍，是看懂 `xd` 的前提。

#### 4.2.3 源码精读

严格的两级同步器「片段」在 projf 里其实出现过——就在 PLL 那一节的「lock 同步」代码里：

- [clock_480p.sv:64-68](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xc7/clock_480p.sv#L64-L68)：`locked` 来自 MMCM，相对 `clk_pix` 异步，于是用 `locked_sync_0` 与 `clk_pix_locked` 两级触发器同步后再用。这就是一个标准的、用于「单 bit 电平」的打两拍同步器，只是被用来同步 lock 信号而已。

#### 4.2.4 代码实践（本讲的主实践任务）

1. **实践目标**：亲手画出两级触发器同步器结构，并解释单级为何不安全。
2. **操作步骤**：
   - 在纸上画出：`async_in` → `FF1(clk_dst)` → `FF2(clk_dst)` → `out`，标出 FF1 输出可能亚稳态、FF2 输出干净。
   - 再画一个「单级」版本（只有 FF1，直接用 FF1 输出），假设 FF1 后面扇出到 N 个不同的下游触发器。
3. **需要观察的现象 / 思考**：单级版本里，当 FF1 处于亚稳态的半电压时，下游 N 个触发器在同一时钟沿各自采样这个半电压，由于布线延迟和器件差异，**它们可能读到不同的 0/1**。
4. **预期结果（解释）**：单级触发器不安全，是因为它没有给亚稳态留恢复时间，且半电压会扇出成「同一个信号被解读成不同值」，直接破坏系统一致性。两级触发器多出一个周期，让亚稳态在 FF2 之前塌缩，从而把「偶发错误率」从「可能每秒数次」压到「几千年才一次」量级（MTBF 指数下降）。
5. 本实践为纸笔/思考型，无需上板；如需数值感，可代入 \(f_{\text{clk}}=100\,\text{MHz}\)、\(f_{\text{data}}=10\,\text{MHz}\) 到 MTBF 公式，体会多一级（\(t_r\) 增加 10 ns）对指数项的影响。

#### 4.2.5 小练习与答案

**练习 1**：为什么两级触发器同步器**不能**用来同步一个 8 位数据总线？

> **参考答案**：8 根线各自打两拍后，每根的亚稳态会独立塌缩成 0 或 1，于是目的域可能采到一个「8 位中部分位是新值、部分位是旧值」的非法组合。正确做法是用握手协议（如 `xd` 的请求/应答思想）或异步 FIFO，保证 8 位被当作一个不可分割的整体来传递。

**练习 2**：MTBF 公式里，为什么增加一级触发器对 MTBF 的影响是「指数级」而非「线性」？

> **参考答案**：因为恢复时间 \(t_r\) 出现在指数 \(e^{t_r/\tau}\) 上。多一级触发器相当于多给一个时钟周期（约 \(1/f_{\text{clk}}\)）的 \(t_r\)，使指数项大幅增加，MTBF 随之指数增长；而 \(f_{\text{clk}}\)、\(f_{\text{data}}\) 只在分母线性出现。

---

### 4.3 脉冲型跨时钟域同步器 xd

#### 4.3.1 概念说明

4.2 节的打两拍同步器只能安全传递「电平」信号（一个稳定持续的电平）。但工程里常见的需求是传递一个**单周期脉冲**——比如「源域这一拍表示：一次运算完成了」。问题来了：如果目的时钟比源时钟慢，源域的那个脉冲可能短到目的时钟**根本采不到**（脉冲整个发生在两个目的时钟沿之间）。

`xd.sv`（「xd」取 cross-domain 之意）解决的就是「把源域的单周期脉冲，可靠地变成目的域的单周期脉冲」。它的策略是经典的 **「脉冲转电平 → 跨域 → 电平转脉冲」**：

1. **源域**：每收到一个脉冲，就让一个 toggle（翻转）寄存器翻一次。脉冲是「一瞬」，但 toggle 的翻转是「持久的电平变化」，目的时钟一定采得到。
2. **跨域**：把这个（相对目的时钟异步的）toggle，用一条移位寄存器（内含同步器）安全地搬到目的域。
3. **目的域**：对搬过来的 toggle 做**边沿检测**（相邻两拍异或），每检测到一次跳变，就 regenerated（重建）出一个目的域的单周期脉冲。

#### 4.3.2 核心流程

用伪代码描述 `xd` 的三段逻辑（与源码一一对应）：

```
// ① 源域：脉冲 → 翻转
logic toggle_src = 0;
always_ff @(posedge clk_src)
    toggle_src <= toggle_src ^ flag_src;   // 来一个脉冲就翻一次

// ② 跨域：4 级移位寄存器（兼做同步器）
logic [3:0] shr_dst = 0;
always_ff @(posedge clk_dst)
    shr_dst <= {shr_dst[2:0], toggle_src}; // 左移，新采样进 [0]

// ③ 目的域：边沿检测 → 重建脉冲
always_comb
    flag_dst = shr_dst[3] ^ shr_dst[2];    // 两拍异或=检测到跳变
```

为什么是 4 级移位寄存器？它一身二任：

- **前若干级充当同步器**：`toggle_src` 对 `clk_dst` 异步，`shr_dst[0]` 是冒险的第一采样点（可能亚稳态），后续 `shr_dst[1]/[2]` 给它时间塌缩——这就是 4.2 节打两拍思想的延伸，只是这里多留了余量。
- **后两级做边沿检测**：`shr_dst[2]` 与 `shr_dst[3]` 都是「已经同步干净」的相邻两拍样本，二者异或即可重建目的域脉冲。

整条链是：`toggle_src → shr[0](同步) → shr[1](同步) → shr[2](干净) → shr[3](干净)`，输出 = `shr[3] ^ shr[2]`。

> 重要限制：`xd` 保证「**孤立的**单周期脉冲」能被安全传递，但**不保证**高频连续脉冲。如果源域在一个目的周期（加上同步延迟）内连发多个脉冲，toggle 会来回翻多次，目的域要么合并、要么漏掉。testbench 里有专门的注释展示这点（见 4.3.4）。

#### 4.3.3 源码精读

`xd.sv` 全文只有 25 行，但每一行都值得细看：

- [xd.sv:8-13](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xd.sv#L8-L13)：端口——`clk_src`/`clk_dst` 两套时钟、`flag_src` 源域脉冲输入、`flag_dst` 目的域脉冲输出。注意 `output logic flag_dst`（4.2 节风格，时序输出）。
- [xd.sv:16-17](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xd.sv#L16-L17)：源域翻转寄存器 `toggle_src <= toggle_src ^ flag_src`。`^` 是异或：`flag_src` 为 1 时翻转，为 0 时保持。这正是「脉冲 → 持久电平翻转」的转换。
- [xd.sv:20-21](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xd.sv#L20-L21)：目的域 4 级移位寄存器。`{shr_dst[2:0], toggle_src}` 是「左移一位、最低位塞新值」的标准写法。
- [xd.sv:24](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xd.sv#L24)：`flag_dst = shr_dst[3] ^ shr_dst[2]`，组合逻辑边沿检测，重建目的域脉冲。

配套 testbench [xd_tb.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xc7/xd_tb.sv#L17-L29) **同时例化两个 `xd`**：一个测「慢→快」（`clk_slow=100MHz → clk_fast=250MHz`），一个测「快→慢」（反过来）。激励里有两个极具教学价值的注释：

- [xd_tb.sv:52](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xc7/xd_tb.sv#L52)：慢→快时，源域一个持续两拍的脉冲会让 `toggle_src` 翻两次，目的（快）域看到两次跳变 →「**两个脉冲**」。
- [xd_tb.sv:76](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xc7/xd_tb.sv#L76)：快→慢时，两个挨得太近的源域脉冲（在同一个慢周期内来回翻 toggle）净效果为零，慢域根本看不到跳变 →「**脉冲消失**」。

这两条正是 4.3.2 节「重要限制」的实证：`xd` 只承诺传递孤立脉冲，源域必须控制脉冲发送节奏。

#### 4.3.4 代码实践

1. **实践目标**：把一个脉冲「走」一遍 `xd`，看清 toggle 怎么翻、移位寄存器怎么搬、异或怎么重建脉冲。
2. **操作步骤**：
   - 打开 [xd_tb.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xc7/xd_tb.sv#L34-L56)，关注 `flag_a_src`（慢→快）的第一段激励：`#100 flag_a_src=1; #10 flag_a_src=0;`（一个 10 ns 的单脉冲）。
   - 在纸上推演：源域（100 MHz，周期 10 ns）一个脉冲 → `toggle_src` 由 0 翻 1 并保持；目的域（250 MHz，周期 4 ns）逐拍把 `toggle_src` 移进 `shr_dst`；当 `shr_dst[3]` 与 `shr_dst[2]` 出现一次不同（0→1 的跳变沿到达），`flag_dst` 拉高一个目的周期。
   - 若有仿真器（Icarus Verilog / Verilator / Vivado），编译 `xd.sv` + `xd_tb.sv` 跑仿真，波形里对齐 `flag_a_src`、`toggle_src`、`shr_dst[3:0]`、`flag_a_dst` 四组信号。
3. **需要观察的现象**：
   - `toggle_src` 每来一个 `flag_a_src` 脉冲翻一次，是「电平」而非「脉冲」。
   - `flag_a_dst` 比 `flag_a_src` 晚若干个目的时钟周期（同步延迟），且宽度恒为一个目的周期。
   - 慢→快方向，`flag_a_dst` 与 `flag_a_src` 个数基本一致（孤立脉冲）。
4. **预期结果**：每输入一个孤立脉冲，输出端得到恰好一个目的域脉冲；输入「两拍连发」（[xd_tb.sv:52](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xc7/xd_tb.sv#L52)）会在快域看到两个输出脉冲——这是 toggle 翻两次的必然结果，与 testbench 注释吻合。
5. 若无法本地仿真，标注「待本地验证」，但纸笔推演部分必须完成。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `xd.sv` 的移位寄存器从 4 级改成 2 级（即 `shr_dst[1:0]`，输出 `shr_dst[1]^shr_dst[0]`），会出什么问题？

> **参考答案**：2 级里 `shr_dst[0]` 是第一采样点、可能亚稳态，`shr_dst[1]` 紧跟其后，恢复时间只有一个目的周期（甚至更短，因为边沿检测还要用 `shr_dst[0]` 本身）。MTBF 会显著下降；此外输出异或用到了未充分稳定的 `shr_dst[0]`，更不可靠。4 级是 projf 为留足同步余量所做的工程选择。

**练习 2**：为什么 `xd` 用「toggle（翻转）」而不是「直接把脉冲电平打两拍」？

> **参考答案**：脉冲是「一瞬」，若目的时钟比源时钟慢，脉冲可能整个落在两个目的时钟沿之间，直接打两拍会 100% 漏掉。toggle 把每个脉冲转成一次「持久电平翻转」，目的时钟迟早会采到这次翻转，再用边沿检测把翻转「还原」成一个目的域脉冲。这就是「脉冲↔电平」的可逆转换，保证不丢孤立脉冲。

**练习 3**：`xd` 能不能用来跨域传递一个「连续高频脉冲流」？为什么？

> **参考答案**：不能。如 4.3.4 所示，源域若在一个目的周期内连发多个脉冲，toggle 来回翻多次，净效果可能互相抵消（快→慢时脉冲消失），或被放大成多个目的脉冲（慢→快时）。`xd` 只适合「低于目的域处理能力」的稀疏事件，如「一次运算完成」「一次按键」。高频流应改用异步 FIFO。

---

## 5. 综合实践

把本讲两个最小模块串起来，做一次完整的「时钟生成 + 跨域事件」设计推演：

**任务**：设想一块 XC7 开发板，100 MHz 晶振。你要让「显示时钟域」（25.2 MHz 像素时钟）和「系统时钟域」（125 MHz 系统时钟，由 `clock_sys` 生成）协同工作：系统域每完成一帧渲染，发一个「帧完成」脉冲给显示域，触发一次屏幕刷新。

1. **时钟部分**：参照 [clock_480p.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xc7/clock_480p.sv#L30-L57) 与 [clock_sys.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xc7/clock_sys.sv#L27-L54)，例化两个时钟模块，分别得到 `clk_pix`（25.2 MHz）与 `clk_sys`（125 MHz），并各自等 `locked` 同步后才放行下游复位。
2. **跨域部分**：在系统域产生「帧完成」单周期脉冲 `frame_done_sys`，用 [`xd`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xd.sv#L8-L25) 把它搬到显示域，得到 `frame_done_pix`。
3. **画图**：画出整条链：`frame_done_sys` → `toggle_src`（@`clk_sys`）→ `shr_dst[3:0]`（@`clk_pix`，标注哪些级是同步器、哪两级做边沿检测）→ `frame_done_pix`。
4. **解释**：用本讲学到的语言说明——为什么不能直接把 `frame_done_sys` 接到显示域触发器（答：跨时钟域亚稳态）；为什么用 toggle 而非电平同步（答：脉冲可能被慢域漏采）；系统域「帧完成」最高能多频繁（答：不能快于显示域一次同步延迟所能分辨的间隔，否则脉冲会合并/丢失）。

> 本综合实践为「设计推演型」，无需综合上板；重点是能把 PLL 生成与 CDC 同步器这两件事在一幅图里讲清楚。

## 6. 本讲小结

- FPGA 用片内 **PLL/MMCM 硬核原语**（XC7 的 `MMCME2_BASE`、iCE40 的 `SB_PLL40_PAD`）从一个输入时钟生成多路不同频率，projf 把它们封装成 `clock_480p`/`clock_sys` 等模块，并通过 `BUFG`/`PLLOUTGLOBAL` 走全局时钟网络。
- MMCM 的频率关系是 \(f_{\text{VCO}}=f_{\text{in}}\cdot\text{MULT}/\text{DIV}\)、再各路分频；用 `localparam` 集中参数即可换分辨率。无论哪个平台，都要**等 `LOCKED` 并同步后**再用生成的时钟。
- 跨时钟域的根本风险是 **亚稳态**：异步信号撞上时钟沿会让触发器输出半电压，且可能扇出成「同信号被解读成不同值」。
- 经典对策是 **两级触发器同步器（打两拍）**：用 MTBF 的指数关系，把偶发错误率压到可忽略；但它只适合单 bit 电平。
- projf 的 [`xd.sv`](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xd.sv#L1-L25) 是更强的 **脉冲型 CDC**：脉冲→toggle→4 级移位寄存器（兼同步器）→异或边沿检测→目的域脉冲，能可靠传递孤立脉冲，但不能传高频脉冲流。
- 单级触发器在 CDC 不安全：既没给亚稳态留恢复时间，又会让半电压扇出成不一致的值。

## 7. 下一步学习建议

- 接着学 [u5-l3](u5-l3-memory-rom-ram-bram.md)：跨时钟域传递「数据流」时，异步 FIFO 是比 `xd` 更合适的工具，而 FIFO 的底层正是本讲提到的 Block RAM，下一讲会讲清楚 ROM/RAM/BRAM。
- 若对显示时钟怎么用感兴趣，可跳到 [u6-l1](u6-l1-display-timing.md)：本讲的 `clock_480p` 像素时钟正是喂给显示时序模块的。
- 想深入 CDC 理论，建议阅读 Xilinx UG949（UltraFast 方法论）中「CDC」章节，以及 projf 官方博客 [Simple Clock Domain Crossing](https://projectf.io/posts/lib-clock-xd/)，对照本讲对 `xd.sv` 的逐行解读再读一遍源码。
