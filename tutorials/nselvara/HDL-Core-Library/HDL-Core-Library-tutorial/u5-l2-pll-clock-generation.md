# PLL 时钟生成（Xilinx / Intel）

## 1. 本讲目标

本讲讲的是本库里**唯一**一个「没有自研行为级实现」的 IP：时钟生成器 `pll`。学完后你应该能：

- 说清楚 PLL（锁相环）在 FPGA 里干什么，以及为什么它和 RAM、FIFO 这类「纯逻辑」IP 不一样。
- 读懂 `pll.vhd` 的两套厂商实现：Xilinx 的 `PLLE2_BASE` + `BUFG`，Intel 的 `altclklock`，并能用一组倍频/分频 generic 把它们配出指定频率。
- 写出 `PLLE2_BASE` 的频率公式，弄清 `CLK_MULTIPLY` / `CLK_DIVIDE` / `OUT_CLK_x_DIVIDE` 三者怎么乘除叠加。
- 解释为什么这个模块没有 `own_behavioural_*` 架构、为什么 CI（NVC 仿真器）要把它排除掉。

本讲承接 [u2-l1 同一实体多架构模式](u2-l1-multi-architecture-pattern.md)：PLL 同样是「一个 entity、两套 architecture」，但它把「自研行为级」这条腿砍掉了。理解这处「例外」，能让你更清楚这个设计模式的边界在哪里。

## 2. 前置知识

### 什么是锁相环（PLL）

前面的讲义里出现的时钟，都是「直接用输入时钟」或「把时钟开关掉」（如 [u5-l1 clock_enable](u5-l1-clock-enable-gating.md) 的门控）。但很多 FPGA 设计需要**一个和输入不同频率的新时钟**：输入给 100 MHz，可某些高速接口要 200 MHz，某些慢速外设要 25 MHz。这时就用到了 **PLL（Phase-Locked Loop，锁相环）**。

PLL 是 FPGA 芯片内部的一块**硬核模拟电路**（不是用逻辑门拼出来的），它的核心能力是：

- **倍频**：把输入频率乘上去；
- **分频**：把输入频率除下来；
- **移相**：调整输出时钟的相位；
- **去抖动**：把不太干净的输入时钟「洗干净」再输出。

一个关键认知：PLL **不是 RTL 逻辑**，而是芯片里固有的模拟资源（Xilinx 7 系列叫 `PLLE2`，Intel Arria 10 叫 `altclklock`）。我们在 VHDL 里写的 `pll.vhd`，**不是在描述 PLL 本身怎么工作**，而是在「例化并配置」这颗硬核——告诉它倍频几分、分频几分、输出几路。这一点决定了本讲后续几乎所有结论。

### 全局缓冲 BUFG

FPGA 里的时钟信号不能像普通数据线那样随便走布线，它必须走专用的「全局时钟网络」，才能同时、低偏斜（low skew）地到达芯片上每一个触发器。Xilinx 里把信号送上全局网络的元件叫 `BUFG`（全局缓冲）。本讲的 Xilinx 实现里，PLL 出来的每一路时钟都要串一个 `BUFG`。

### 前置术语速查

| 术语 | 含义 |
| --- | --- |
| PLL | 锁相环，硬核模拟资源，做倍频/分频/移相 |
| BUFG | Xilinx 全局缓冲，把时钟送上全局低偏斜网络 |
| `PLLE2_BASE` | Xilinx 7 系列 PLL 的基础原语 |
| `altclklock` | Intel/Altera 的时钟锁相 megafunction |
| VCO | PLL 内部压控振荡器，倍频后的中间频率，有允许的频率区间 |
| `unisim` / `altera_mf` | Xilinx / Intel 的仿真库，提供原语的行为模型 |

## 3. 本讲源码地图

本讲只涉及 `ip/pll/` 目录下三个文件，结构非常紧凑：

| 文件 | 角色 | 作用 |
| --- | --- | --- |
| `ip/pll/pll.vhd` | 设计源码（可综合） | 一个 `pll` entity + 两套 architecture：`xilinx_behavioural`（`PLLE2_BASE`+`BUFG`）和 `intel_behavioural`（`altclklock`） |
| `ip/pll/tb/tb_pll.vhd` | 测试台（仅仿真） | 用 VUnit 测 Xilinx 架构：数时钟边沿、算频率比、验证锁定与稳定性 |
| `ip/pll/tb/tb_pll.do` | 波形脚本 | ModelSim/QuestaSim 的 Tcl 脚本，给 `tb_pll` 信号分组加波形 |

> 注意：与库里大多数 IP 不同，`pll` **没有** `own_behavioural` 架构，也只有一套测试台，且测试台只例化了 `xilinx_behavioural`。原因见 4.4 节。

## 4. 核心概念与源码讲解

### 4.1 pll 实体：统一的时钟生成接口与倍频/分频 generic

#### 4.1.1 概念说明

`pll` 模块对外承诺一个简单的契约：给我一个输入时钟 `in_clk`，我还你两路输出时钟 `out_clk_0`、`out_clk_1`，外加一个 `locked` 信号告诉你「PLL 是否已稳定锁定」。

至于「怎么生成这两路时钟」，那是 architecture 的事——这正是 [u2-l1](u2-l1-multi-architecture-pattern.md) 讲过的「接口与实现分离」。entity 只负责定端口和 generic（可调旋钮），把 Xilinx / Intel 两套实现藏在 architecture 里。

generic 是这里的核心：它们是**配置 PLL 的旋钮**。理解这几个旋钮的数学关系，是本讲的重点。

#### 4.1.2 核心流程：generic 如何决定输出频率

`pll` entity 提供了 5 个 generic（见下方源码）。其中前 4 个参与频率计算，第 5 个 `OUT_CLK_1_DIVIDE` 只在 Xilinx 实现里生效：

| generic | 类型/默认值 | 含义 |
| --- | --- | --- |
| `IN_CLK_PERIOD_PS` | `real := 8.0` | 输入时钟周期（皮秒）。默认 8.0 = 8 ns = **125 MHz** |
| `CLK_MULTIPLY` | `positive := 8` | 倍频系数（Xilinx 的 `CLKFBOUT_MULT` / Intel 的 `clock0_boost`） |
| `CLK_DIVIDE` | `positive := 1` | 输入预分频（Xilinx 的 `DIVCLK_DIVIDE` / Intel 的 `clock0_divide`） |
| `OUT_CLK_0_DIVIDE` | `positive := 8` | 第一路输出分频（仅 Xilinx 用 `CLKOUT0_DIVIDE`） |
| `OUT_CLK_1_DIVIDE` | `positive := 40` | 第二路输出分频（仅 Xilinx 用 `CLKOUT1_DIVIDE`） |

Xilinx `PLLE2_BASE` 的输出频率公式（本库两套 generic 是统一命名的，所以下面同时给出 Xilinx 原语名）：

\[
f_{\text{out}_x} = f_{\text{in}} \times \frac{\text{CLK\_MULTIPLY}}{\text{CLK\_DIVIDE} \times \text{OUT\_CLK\_}x\text{\_DIVIDE}}
\]

换算成周期更直观（这也是测试台 `EXPECTED_OUT0_PERIOD` 用的式子）：

\[
T_{\text{out}_x} = T_{\text{in}} \times \frac{\text{OUT\_CLK\_}x\text{\_DIVIDE} \times \text{CLK\_DIVIDE}}{\text{CLK\_MULTIPLY}}
\]

直觉上可以这样记：**先倍频后分频**。输入先进 PLL 被 `CLK_MULTIPLY/CLK_DIVIDE` 抬到一个内部高频（VCO），再分别被 `OUT_CLK_x_DIVIDE` 除下来，得到两路输出。`CLK_MULTIPLY` 在分子（越大输出越快），三个 `_DIVIDE` 在分母（越大输出越慢）。

用 entity 的默认值代入（输入 125 MHz）：

- `out_clk_0` = 125 × 8 / (1 × 8) = **125 MHz**（和输入同频，相当于「直通整形」）
- `out_clk_1` = 125 × 8 / (1 × 40) = **25 MHz**

#### 4.1.3 源码精读

先看 entity 本身——它只声明端口和 generic，完全不提任何厂商：

[ip/pll/pll.vhd:11-25](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/pll.vhd#L11-L25) —— `pll` 的 entity：4 个端口（1 入 2 出时钟 + 1 个 `locked`）、5 个 generic。注意端口类型用 `std_ulogic`，符合本库一贯风格。

注意第 13 行 `IN_CLK_PERIOD_PS: real := 8.0`：默认值 8.0 ps 写法其实是 **8000 ps = 8 ns = 125 MHz**——这正是本讲综合实践要用到的输入频率。

> 一个值得留意的细节：entity 的 generic 默认值是 Xilinx 语义（带 `OUT_CLK_0_DIVIDE` / `OUT_CLK_1_DIVIDE`），而 Intel 的 `altclklock` 并没有「第二路独立分频」这个概念（见 4.3 节）。也就是说，这个 entity 的接口其实是以 Xilinx 能力为基准设计的，Intel 实现只能力所能及地映射其中一部分。

#### 4.1.4 代码实践

这是一个**源码阅读 + 手算**型的实践（PLL 必须有厂商库才能跑仿真，我们在 4.4 节再讨论运行环境）。

1. **目标**：用 entity 默认 generic，手算两路输出频率，验证你理解了公式。
2. **步骤**：打开 [ip/pll/pll.vhd:12-18](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/pll.vhd#L12-L18)，取默认值（`IN_CLK_PERIOD_PS=8.0`→125 MHz，`CLK_MULTIPLY=8`，`CLK_DIVIDE=1`，`OUT_CLK_0_DIVIDE=8`，`OUT_CLK_1_DIVIDE=40`），套用上面的周期公式。
3. **观察**：`out_clk_0` 应与输入同频（125 MHz），`out_clk_1` 应为 25 MHz。
4. **预期结果**：`T_out0 = 8ns × 8×1/8 = 8 ns`（125 MHz）；`T_out1 = 8ns × 40×1/8 = 40 ns`（25 MHz）。
5. **待本地验证**：若你能在 Vivado/ModelSim（带 `unisim`）里跑通 `tb_pll`，可在波形上量这两路周期做对照。

#### 4.1.5 小练习与答案

**练习 1**：若想把 `out_clk_0` 配成「输入的 2 倍频」，固定 `CLK_DIVIDE=1`，应该怎么设 `CLK_MULTIPLY` 和 `OUT_CLK_0_DIVIDE`？

> **答案**：套公式 \( f_{out0} = f_{in} \times M/(D \cdot O_0) \)，要 2 倍频即 \( M/(D \cdot O_0) = 2 \)。取 `CLK_MULTIPLY=8`、`OUT_CLK_0_DIVIDE=4` 即可（8/(1×4)=2）。

**练习 2**：为什么 entity 用「周期（`IN_CLK_PERIOD_PS`）」而不是「频率」作为输入 generic？

> **答案**：因为 Xilinx `PLLE2_BASE` 的 `CLKIN1_PERIOD` 原语参数要求的就是**周期（纳秒）**（见 4.2.3 节）。用周期作通用量，可以最直接地喂给原语，避免一次「频率→周期」的换算。

---

### 4.2 Xilinx 实现：PLLE2_BASE + BUFG

#### 4.2.1 概念说明

`xilinx_behavioural` 架构例化了一颗 Xilinx 7 系列的 PLL 硬核 `PLLE2_BASE`，并用两颗 `BUFG` 把它的两路输出分别送上全局时钟网络。这是本库「同一 entity 多架构」模式在 Xilinx 这一侧的标准做法：用 `xpm` / `unisim` 原语去封装厂商硬核。

#### 4.2.2 核心流程

`PLLE2_BASE` 是「基础版」PLL（相对 `PLLE2_ADV` 而言，端口更少、配置更简单），它的内部信号通路是：

```
in_clk ──► [DIVCLK_DIVIDE 预分频] ──► [鉴相器] ──► [VCO 倍频 CLKFBOUT_MULT] ──┬──► CLKOUT0_DIVIDE ──► clkout0 ──► BUFG ──► out_clk_0
                                                                                │
                                                                                └──► CLKOUT1_DIVIDE ──► clkout1 ──► BUFG ──► out_clk_1

              ┌──────────────────────────────┘
              ▼
        clkfbout ──► (内部反馈) ──► clkfbin   ← 这个反馈环是 PLL「锁相」的关键
```

要点：

1. **反馈环**：`clkfbout`（反馈时钟输出）直接连回 `clkfbin`（反馈输入），构成 PLL 锁相所需的闭环。这是 `PLLE2_BASE` 的标准接法（内部反馈，不需要外部从 PCB 引回）。
2. **VCO 中间频率**：\( f_{\text{VCO}} = f_{\text{in}} \times \text{CLK\_MULTIPLY} / \text{CLK\_DIVIDE} \)。这个值必须落在芯片允许的 VCO 区间内（7 系列通常约 600–1600 MHz，依速度等级而异），综合工具会检查。
3. **两路输出**：`clkout0` 和 `clkout1` 由各自的 `CLKOUTx_DIVIDE` 从 VCO 分频得到，频率可不同。
4. **全局缓冲**：每路输出各串一个 `BUFG`，把信号送上全局低偏斜网络，确保能当时钟用。
5. **锁定指示**：`locked` 信号在 PLL 稳定后拉高，告诉系统「现在输出的时钟可信了」。

#### 4.2.3 源码精读

首先看 architecture 前的厂商库声明——这是 [u2-l2](u2-l2-vendor-simulation-libraries.md) 讲过的「依赖局部化」风格：声明紧贴在 `xilinx_behavioural` 之前，而不是堆在文件顶部。

[ip/pll/pll.vhd:27-30](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/pll.vhd#L27-L30) —— `library unisim; use unisim.vcomponents.all;` 紧接着 `architecture xilinx_behavioural`，提供 `PLLE2_BASE` 和 `BUFG` 的可见性。

architecture 内部先算了一个常量，把皮秒换算成 `PLLE2_BASE` 要的纳秒：

[ip/pll/pll.vhd:31-34](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/pll.vhd#L31-L34) —— `IN_CLK_PERIOD_NS := IN_CLK_PERIOD_PS / 1000.0`，并声明 3 个内部信号：`out_clk_f_b`（反馈）、`pll_clks_0/1`（PLL 裸输出，进 BUFG 前）。

接着是核心的 `PLLE2_BASE` 例化：

[ip/pll/pll.vhd:36-53](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/pll.vhd#L36-L53) —— `PLLE2_BASE` 的 generic map 把本库 generic 一一映射到原语参数（`CLKIN1_PERIOD`/`CLKFBOUT_MULT`/`CLKOUT0_DIVIDE`/`CLKOUT1_DIVIDE`/`DIVCLK_DIVIDE`）；port map 里 `clkin1=>in_clk`、反馈环 `clkfbout<=>clkfbin=>out_clk_f_b`、两路输出与 `locked`。`pwrdwn=>'0'`（不省电）、`rst=>'0'`（不复位）固定拉死。

注意 generic 的对应关系，这正是 4.1.2 公式的来源：

| 本库 generic | PLLE2_BASE 参数 | 公式角色 |
| --- | --- | --- |
| `CLK_MULTIPLY` | `CLKFBOUT_MULT` | 分子（倍频） |
| `CLK_DIVIDE` | `DIVCLK_DIVIDE` | 分母（预分频） |
| `OUT_CLK_0_DIVIDE` | `CLKOUT0_DIVIDE` | 分母（输出 0 分频） |
| `OUT_CLK_1_DIVIDE` | `CLKOUT1_DIVIDE` | 分母（输出 1 分频） |

最后两颗 `BUFG`，把 PLL 裸输出送上全局网络：

[ip/pll/pll.vhd:55-65](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/pll.vhd#L55-L65) —— `clk_0_inst`/`clk_1_inst` 两颗 `BUFG`：`I=>pll_clks_x`，`O=>out_clk_x`。这正是 `pll_clks_*`（PLL 裸输出）与 `out_clk_*`（对外端口）之间隔着 `BUFG` 的原因。

#### 4.2.4 代码实践

**目标**：根据测试台的实测方法，确认 `PLLE2_BASE` 的输出频率。

1. 打开 [ip/pll/tb/tb_pll.vhd:43-46](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/tb/tb_pll.vhd#L43-L46)，记下测试台用的 generic：输入 100 MHz，`CLK_MULTIPLY=8`，`CLK_DIVIDE=1`，`OUT_CLK_0_DIVIDE=4`，`OUT_CLK_1_DIVIDE=10`。
2. 套公式手算：`out_clk_0` 应为 100×8/(1×4) = **200 MHz**，`out_clk_1` 应为 100×8/(1×10) = **80 MHz**。这正好对应源码第 45–46 行注释的期望值。
3. 阅读测试台「数边沿算频率」的逻辑：[ip/pll/tb/tb_pll.vhd:98-118](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/tb/tb_pll.vhd#L98-L118) 是两个对 `out_clk_0/1` 上升沿计数的进程；[ip/pll/tb/tb_pll.vhd:152-166](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/tb/tb_pll.vhd#L152-L166) 在固定 1000 个输入周期内，用「总时长 / 边沿数」算出实测周期。
4. **观察**：`test_clock_generation` 用「频率比」而非绝对周期做校验（仿真时序精度有限），见 [ip/pll/tb/tb_pll.vhd:169-185](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/tb/tb_pll.vhd#L169-L185)。
5. **预期结果**：手算的 200 MHz / 80 MHz 与测试台注释、与边沿数比值一致。
6. **待本地验证**：在带 `unisim` 的 ModelSim/Vivado 仿真器里跑 `tb_pll`，看 `test_clock_generation`、`test_clock_stability` 两个用例通过。

> **进阶观察**：注意 [tb_pll.vhd:171-173](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/tb/tb_pll.vhd#L171-L173) 的频率比校验表达式是 `CLK_MULTIPLY / OUT_CLK_0_DIVIDE * CLK_DIVIDE`，即 \(M\cdot D / O_0\)；而真实的频率比应为 \(M/(D\cdot O_0)\)。由于本测试台固定 `CLK_DIVIDE=1`，两者相等，校验通过；若你把 `CLK_DIVIDE` 改成非 1，这个比值表达式就需要修正。理解这一点，说明你已经吃透了三个 generic 的叠加关系。

#### 4.2.5 小练习与答案

**练习 1**：Xilinx 实现里，为什么 `clkfbout` 和 `clkfbin` 要接在一起？

> **答案**：PLL 必须有一个闭合的反馈环才能「锁相」——它比较反馈时钟和输入时钟的相位差，调整 VCO 直到两者对齐。把 `clkfbout`（反馈输出）直连 `clkfbin`（反馈输入）是 `PLLE2_BASE` 的内部反馈接法，意味着 PLL 用自己倍频后的时钟做反馈，不依赖从 PCB 引回的外部信号。

**练习 2**：`pll_clks_0` 和 `out_clk_0` 有什么区别？能省掉中间的 `BUFG` 吗？

> **答案**：`pll_clks_0` 是 `PLLE2_BASE` 的裸输出，`out_clk_0` 是它经过 `BUFG` 后的版本。`BUFG` 把信号送上全局低偏斜时钟网络，使其能可靠地驱动全芯片触发器。原则上如果不当时钟用、只驱动少量逻辑，可以不挂 `BUFG`；但本模块的用途就是「输出可用时钟」，所以必须挂。

---

### 4.3 Intel 实现：altclklock

#### 4.3.1 概念说明

`intel_behavioural` 架构例化 Intel 的 `altclklock` megafunction。它和 Xilinx 的 `PLLE2_BASE` 是对等物——都是配置 PLL 硬核——但 Intel 用的是「`altera_mf`（megafunction）」抽象层，参数命名和端口语义都不同。这里同样遵循「依赖局部化」：`library altera_mf` 紧贴 architecture 前。

#### 4.3.2 核心流程

`altclklock` 的频率公式更简单（只有一路独立配置）：

\[
f_{\text{out}} = f_{\text{in}} \times \frac{\text{clock0\_boost}}{\text{clock0\_divide}} = f_{\text{in}} \times \frac{\text{CLK\_MULTIPLY}}{\text{CLK\_DIVIDE}}
\]

注意它**只用了 entity 的前两个频率 generic**（`CLK_MULTIPLY`→`clock0_boost`，`CLK_DIVIDE`→`clock0_divide`），`OUT_CLK_0_DIVIDE` / `OUT_CLK_1_DIVIDE` 在 Intel 这套架构里**没有被映射**。这意味着：

- Intel 实现的 `out_clk_0` 频率 = \( f_{\text{in}} \times M/D \)；
- `out_clk_1` 在 generic map 里**没有独立的分频/倍频参数**，它的行为取决于 `altclklock` 对未配置 `clock1` 的默认处理（这是 Xilinx 与 Intel 两套实现在「第二路输出」能力上的真实差异）。

#### 4.3.3 源码精读

先看架构声明：

[ip/pll/pll.vhd:68-71](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/pll.vhd#L68-L71) —— `library altera_mf; use altera_mf.altera_mf_components.all;` 紧贴 `architecture intel_behavioural`。注意：`intel_behavioural` 前没有 `library unisim`，`xilinx_behavioural` 前没有 `library altera_mf`——两套实现的厂商依赖互不污染。

接着看 `altclklock` 例化本身：

[ip/pll/pll.vhd:73-95](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/pll.vhd#L73-L95) —— `altclklock` 的 generic map 只配了 `clock0_boost => CLK_MULTIPLY` 和 `clock0_divide => CLK_DIVIDE`（外加一些固定设置如 `intended_device_family => "Arria 10"`、`operation_mode => "NORMAL"`、`valid_lock_cycles => 2` 等）。

把它和 Xilinx 版逐点对照，能看到两套实现的**结构性差异**：

| 维度 | Xilinx (`PLLE2_BASE`) | Intel (`altclklock`) |
| --- | --- | --- |
| 倍频 generic | `CLKFBOUT_MULT <= CLK_MULTIPLY` | `clock0_boost <= CLK_MULTIPLY` |
| 预分频 generic | `DIVCLK_DIVIDE <= CLK_DIVIDE` | `clock0_divide <= CLK_DIVIDE` |
| 第二路输出 | `CLKOUT1_DIVIDE <= OUT_CLK_1_DIVIDE` | **无对应参数**（`clock1` 未配 boost/divide） |
| 反馈来源 | PLL 自身的 `clkfbout`（内部反馈） | `fbin => out_clk_0`（从输出 0 引回） |
| 全局缓冲 | 显式挂两颗 `BUFG` | **无显式缓冲**（Quartus 会自动推断全局网络） |
| 目标器件 | 由 Vivado 选定 | `intended_device_family => "Arria 10"` 写死 |

[ip/pll/pll.vhd:88-95](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/pll.vhd#L88-L95) —— port map：`inclock=>in_clk`、`clock0=>out_clk_0`、`clock1=>out_clk_1`、`fbin=>out_clk_0`（反馈从 `out_clk_0` 引回）、`locked=>locked`。

> **关于 `clock1` 的诚实说明**：由于 Intel 的 generic map 没有为 `clock1` 提供独立的 boost/divide，本库里 Xilinx 与 Intel 两套实现对 `out_clk_1` 的产出**并不严格等价**。Xilinx 版能给出与 `out_clk_0` 不同频率的第二路时钟；Intel 版的 `clock1` 行为取决于 `altclklock` 的默认配置。这一点在真实 Intel 工具链中的具体表现**待确认**（需要查 `altera_mf` 文档或在 Quartus 里实测），但读者应意识到：同一个 entity、两套 architecture，并不意味着两者在所有端口上行为完全一致。

#### 4.3.4 代码实践

**目标**：理解 Intel 架构的简化频率公式，并对比两套实现。

1. 阅读 [ip/pll/pll.vhd:73-95](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/pll.vhd#L73-L95)，确认 Intel 版只映射了 `clock0_boost` / `clock0_divide` 两个频率参数。
2. 假设输入 125 MHz、`CLK_MULTIPLY=8`、`CLK_DIVIDE=1`，用 Intel 公式算 `out_clk_0`：125 × 8/1 = **1000 MHz**（同一个 generic 组合在 Xilinx 版会被 `OUT_CLK_0_DIVIDE` 再除一次，而 Intel 版没有这一除——这是两套实现最直接的差异）。
3. **观察**：对照 4.2.3 的 Xilinx 公式，Xilinx 版同样的 `M=8, D=1` 会先到 VCO 的 1000 MHz 再除以 `OUT_CLK_0_DIVIDE`；Intel 版 `clock0` 就直接是 1000 MHz。这说明**两套架构对同一组 generic 的解释不同**，移植时不能照搬 generic。
4. **预期结果**：你应当能用自己的话解释「为什么从 Xilinx 切到 Intel 时，generic 往往要重新算」——因为 Intel 缺少「输出再分频」这一级。
5. **待本地验证**：Intel 版需要 `altera_mf` 库（Quartus 自带），本仓库的 `tb_pll` 只测了 Xilinx 版，Intel 版的精确行为需在 Quartus/ModelSim-Intel 版中验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Intel 版没有像 Xilinx 版那样显式例化 `BUFG`？

> **答案**：Xilinx 要求显式用 `BUFG`/`BUFGCE` 把信号送上全局网络；Intel/Quartus 的工具会**自动推断**时钟信号并分配全局布线资源，所以 RTL 里通常不写显式全局缓冲原语。两家的「时钟工程化」哲学不同。

**练习 2**：`altclklock` 的 `fbin => out_clk_0` 和 Xilinx 版的 `clkfbin => out_clk_f_b` 有何不同？

> **答案**：Xilinx 版反馈来自 PLL 内部生成的 `clkfbout`（内部反馈，没经过 `BUFG`）；Intel 版反馈直接取自 `out_clk_0`（即输出端）。Intel 这种接法把输出缓冲也纳入了反馈环，能补偿输出路径上的延迟；两者都是合法的 PLL 反馈拓扑，但起点不同。

---

### 4.4 为什么 PLL 没有自研行为级实现——与 CI 排除

#### 4.4.1 概念说明

这是本讲最重要的一个「为什么」。回顾 [u2-l1](u2-l1-multi-architecture-pattern.md)：本库大多数 IP 都有三套 architecture（`xilinx_behavioural_*` / `intel_behavioural_*` / `own_behavioural_*`），其中 `own_behaviourral_*` 是「厂商无关、纯 VHDL-2008、开箱即仿真」的实现，是整个学习手册反复强调的「钥匙」。

但 `pll` 只有前两套，**没有** `own_behavioural`。原因在 4.2.1 已经埋下：PLL 是**硬核模拟资源**，它的核心（压控振荡器、鉴相器）根本不是数字逻辑，**无法用 VHDL 的进程和信号精确建模**。你可以用 VHDL 写一个「频率变换」的近似模型，但它无法反映真实 PLL 的锁定动态、抖动、压控振荡器行为——也就失去了作为可信仿真参照的价值。所以本库诚实地不为它造一个假的 `own_behaviourral`，而是承认：这个 IP 就是必须依赖厂商库。

这一点在 README 的技术支持表里写得很直白：

[README.md:322](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L322) —— 「Clock Generator (PLL)」一行：Xilinx = Yes，Intel = Yes，**Own/Behavioral = No**。它是全表唯一一个 Own/Behavioral 列为 No 的 IP。

[README.md:338](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L338) —— 注释明确：「PLL modules are vendor-specific and do not have a pure behavioral implementation.」

#### 4.4.2 核心流程：CI 如何因此排除 PLL

既然 PLL 必须依赖厂商库，而本仓库的 CI 用的是开源仿真器 **NVC**（详见 [u1-l4](u1-l4-ci-and-toolchain.md)），问题就来了：NVC 虽然完整支持 VHDL-2008，但它**无法编译/绑定 Verilog 原语**，而 `PLLE2_BASE` 的 Xilinx 仿真模型是 Verilog 写的（`unisims_ver`），CI 又改用纯 VHDL 行为模型替代（grlib/gplgpu 提供），但那套开源模型里**恰恰缺少 `PLLE2_BASE` 的 VHDL 绑定**。于是 CI 干脆把整个 PLL 文件排除掉：

[ip/test_runner_ci_cd.py:45-48](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner_ci_cd.py#L45-L48) —— `excluded_list` 把 `tb_pll.vhd` 和 `pll.vhd` 都排除，注释写明原因：「missing VHDL binding for PLLE2_BASE」。

[ip/test_runner_ci_cd.py:37-38](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner_ci_cd.py#L37-L38) —— CI 顶部也打印了策略：「VHDL behavioral models for Xilinx primitives (like PLLE2_BASE)」+「NVC cannot directly use Verilog primitives」。

换句话说，PLL 是全库**唯一**被 CI 排除的模块，根因链是：

```
PLL 是硬核模拟资源（无法纯 VHDL 建模）
        │
        ▼
必须依赖厂商仿真库（PLLE2_BASE 的 Verilog 模型 / altera_mf）
        │
        ▼
CI 用 NVC，NVC 不能编译 Verilog 原语，开源 VHDL 替代模型又缺 PLLE2_BASE 绑定
        │
        ▼
CI 把 tb_pll.vhd / pll.vhd 放进 excluded_list
        │
        ▼
PLL 只能在本地带厂商库的环境（Vivado/Quartus/ModelSim）里验证
```

#### 4.4.3 源码精读

再次确认 `pll.vhd` 里确实只有两套 architecture、没有第三套：

[ip/pll/pll.vhd:30](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/pll.vhd#L30) —— `architecture xilinx_behavioural`。
[ip/pll/pll.vhd:71](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/pll.vhd#L71) —— `architecture intel_behavioural`。文件在第 96 行 `end architecture;` 结束，**之后没有任何 `own_behavioural`**。这与本库其他 IP（如 `fifo_sync` 有三套）形成鲜明对比。

测试台也只例化了 Xilinx 版，印证了「本地能跑哪套取决于有没有厂商库」：

[ip/pll/tb/tb_pll.vhd:265](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/tb/tb_pll.vhd#L265) —— `DUT: entity work.pll(xilinx_behavioural)`。测试台依赖 `unisim`（由本地 `test_runner.py` 的 `use_xilinx_libs=True` 加载），所以只测 Xilinx 架构；Intel 架构没有配套测试台。

#### 4.4.4 代码实践

**目标**：亲手确认「PLL 是全库唯一无自研实现 + 唯一被 CI 排除」这件事。

1. 在本仓库根目录执行 `git ls-files ip/pll/`，确认 `ip/pll/` 下只有 `pll.vhd`、`tb/tb_pll.vhd`、`tb/tb_pll.do` 三个文件，没有 `tb/tb_pll_altclklock.vhd` 之类的 Intel 测试台。
2. 打开 [ip/pll/pll.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/pll.vhd) 通读全文，数 architecture 的数量：应当只有 `xilinx_behavioural` 和 `intel_behavioural` 两套，没有第三套。
3. 打开 [ip/test_runner_ci_cd.py:45-48](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner_ci_cd.py#L45-L48)，确认 `excluded_list` 里只有 PLL 相关的两项。
4. **观察**：把这三步串起来，PLL 同时满足「无 own_behavioural」「无 Intel 测试台」「CI 排除」三个条件，是全库唯一。
5. **预期结果**：你能在不跑仿真、纯靠读源码和 README 的情况下，向别人解释清楚「为什么 PLL 是这个库里的特例」。

#### 4.4.5 小练习与答案

**练习 1**：为什么本库不为 PLL 写一个「近似」的 `own_behaviourral` 架构来让 CI 能跑？

> **答案**：因为 PLL 的核心（VCO、鉴相器）是模拟电路，任何 VHDL 近似模型都无法反映真实锁定动态、抖动和压控行为。一个不可信的近似模型混进回归测试，反而会给出「假绿灯」——让人误以为 PLL 行为已被验证。本库选择「诚实缺位」而非「虚假覆盖」，这是更负责任的工程态度。

**练习 2**：如果有一天 NVC 学会了绑定 Verilog 原语，`excluded_list` 里还需要保留 PLL 吗？

> **答案**：大概率不需要。CI 排除 PLL 的直接原因是「NVC 缺 `PLLE2_BASE` 的 VHDL 绑定」；若 NVC 能直接用 Xilinx 的 Verilog 仿真模型（`unisims_ver`），`tb_pll`（它例化的是 `xilinx_behavioural`）就能在 CI 里跑起来，届时可以从 `excluded_list` 移除这两项。

---

## 5. 综合实践

把本讲的三件事——频率公式、两套实现差异、为什么没有自研实现——串成一个完整任务：

**任务：给定 125 MHz 输入（`IN_CLK_PERIOD_PS=8000.0`），算出让 Xilinx 的 `out_clk_0` 输出 100 MHz 的一组 generic，并解释你的选择。**

步骤：

1. **列方程**。套用 Xilinx 公式 \( f_{out0} = f_{in} \times M/(D \cdot O_0) \)，代入 \( f_{in}=125 \)、\( f_{out0}=100 \)，得：

   \[
   \frac{100}{125} = 0.8 = \frac{M}{D \cdot O_0}
   \]

   即需要 \( M : (D \cdot O_0) = 4 : 5 \)。

2. **挑一组整数解**。最简比 \( 4/5 \)：`CLK_MULTIPLY=4`，`CLK_DIVIDE=1`，`OUT_CLK_0_DIVIDE=5`。验算：125 × 4/(1×5) = 100 MHz ✓，周期 = 8000 ps × 5×1/4 = 10000 ps = 10 ns ✓。

3. **考虑 VCO 约束**。`CLK_MULTIPLY=4` 时 VCO = 125 × 4/1 = 500 MHz，对 7 系列大多偏低。把它整体放大 2 倍：`CLK_MULTIPLY=8`，`OUT_CLK_0_DIVIDE=10`（`CLK_DIVIDE` 仍为 1），比值仍是 8/(1×10) = 0.8，但 VCO = 125 × 8 = 1000 MHz，落在常见 VCO 区间内。验算：125 × 8/10 = 100 MHz ✓。

4. **改测试台验证**（本地带 `unisim` 的环境）。把 [tb_pll.vhd:43-46](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/tb/tb_pll.vhd#L43-L46) 的常量改成 `IN_CLK_FREQUENCY = 125 MHz`、`OUT_CLK_0_DIVIDE = 10`（`CLK_MULTIPLY=8`、`CLK_DIVIDE=1` 保持），同步把 [tb_pll.vhd:48](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/pll/tb/tb_pll.vhd#L48) 的 `EXPECTED_OUT0_PERIOD` 会自动重算（公式依赖这些常量）。

5. **观察 `locked` 与输出频率**。跑 `test_clock_generation`，确认 `locked` 在约 1 us 后（测试台 `wait for 1 us` 等锁定）稳定为高，且 `out_clk_0` 的实测周期 ≈ 10 ns。

6. **待本地验证**：本仓库 CI 不跑 `tb_pll`（见 4.4），所以这一步必须在装有 Vivado/ModelSim + `unisim` 的机器上做。若没有这种环境，请回到第 1–3 步，用手算 + 4.2.4 的源码阅读完成对公式与实现的理解。

7. **迁移思考（选做）**：如果改用 Intel 架构实现同样的「125 MHz → 100 MHz」，generic 该怎么设？提示：Intel 公式少了输出再分频这一级，\( f_{out}=f_{in}\times M/D = 100 \) ⇒ \( M/D = 0.8 = 4/5 \)，可取 `CLK_MULTIPLY=4`、`CLK_DIVIDE=5`。注意此时 `OUT_CLK_0_DIVIDE` 在 Intel 架构里**不起作用**。

## 6. 本讲小结

- `pll` 是一个时钟生成器，对外给两路输出时钟 + 一个 `locked` 信号；它通过 5 个倍频/分频 generic 配置输出频率。
- Xilinx 实现用 `PLLE2_BASE`（PLL 硬核）+ 两颗 `BUFG`（全局缓冲），公式为 \( f_{out_x} = f_{in} \times M/(D \cdot O_x) \)；反馈取自 PLL 自身 `clkfbout`。
- Intel 实现用 `altclklock`，公式简化为 \( f_{out} = f_{in} \times M/D \)；它没有「输出再分频」一级，`OUT_CLK_1_DIVIDE` 不被映射，且不显式例化全局缓冲（Quartus 自动推断）。
- 两套实现**对同一组 generic 的解释不同**，从一家迁到另一家时 generic 必须重算；`out_clk_1` 在两套实现里也不严格等价。
- PLL 是**硬核模拟资源**，无法用纯 VHDL 精确建模，因此它是全库**唯一没有 `own_behaviourral` 实现**的 IP。
- 正因为它必须依赖厂商库，而 CI 的 NVC 仿真器缺 `PLLE2_BASE` 的 VHDL 绑定，所以 `tb_pll.vhd` / `pll.vhd` 是全库**唯一被 CI 排除**的文件。

## 7. 下一步学习建议

- 若你想看「**能用纯 VHDL 实现、CI 能跑**」的时序基础设施，回到 [u5-l1 clock_enable](u5-l1-clock-enable-gating.md) 对照：那里用 `if generate` + `BUFGCE` 解决「开关时钟」，是数字逻辑能搞定的范畴，与 PLL 的「硬核」形成互补。
- 接下来进入 [u6 RAM 内存模块](u6-l1-single-port-ram.md) 开始存储原语之旅；RAM 的「推断 BRAM」和 PLL 的「例化硬核」是两种截然不同的 FPGA 资源利用思路，值得对照体会。
- 若你对 PLL 的锁定动态、VCO 约束、移相等更深层话题感兴趣，建议阅读 Xilinx UG472（7 系列时钟资源用户指南）与 Intel 的 `altclklock` megafunction 用户指南——本讲只覆盖了源码里出现的那几个参数。
