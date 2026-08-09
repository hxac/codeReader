# 实验矩阵——频率 × R × N

## 1. 本讲目标

本讲解决一个看似简单、但在数据集类仓库里极其关键的问题：**这套 Vivado 报告到底是在什么实验条件下采集的？覆盖了哪些组合？又有哪些组合是缺失的？**

读完本讲，你应当能够：

- 在任意一份 timing 报告的 Clock Summary 中读出**时钟周期（Period）**，并把它和频率互相换算。
- 画出本仓库的「**方案 × 频率**」覆盖矩阵，说出哪个方案在哪几个频率点有数据。
- 数清楚每个方案在每个频率下覆盖了哪些 **R（抽取率）× N（级数）** 组合。
- 识别出数据缺口（最典型的是 **290 MHz 仅有 CIC Compiler 一种方案**），并发现 `to_delete/` 这种会造成**重复计数**的数据卫生陷阱。

本讲是 u2-l1（读时序报告）和 u2-l4（三种实现方案对比）的自然延伸：前两讲分别教会你「读单个报告」和「区分三种方案」，本讲则把视野拉高到**整套实验的设计层面**——告诉你这些报告是怎么被组织起来的，以及它们之间能否、以及如何被横向比较。

---

## 2. 前置知识

在进入本讲前，请确认你已经掌握以下概念（它们在前置讲义中已建立）：

- **时钟周期与频率互为倒数**：数字电路里，时钟每「滴答」一次的时间间隔叫周期 \(T\)，每秒滴答的次数叫频率 \(f\)，二者满足 \(T = 1/f\)。例如 100 MHz 的时钟，周期是 10 ns（纳秒）。
- **建立时间裕量 WNS**（来自 u1-l4、u2-l1）：WNS 为正代表数据能在一个时钟周期内稳定传递、时序满足；WNS 越接近 0，时序越「紧」，离违例越近。
- **三种实现方案**（来自 u2-l4）：
  - Xilinx **CIC Compiler** IP（厂商 IP，Design 名固定为 `cic_compiler_0`）；
  - **MATLAB HDL Coder**（代码生成，Design 名随参数变化为 `CIC_R{R}_N{N}`）；
  - **Open-source RTL**（手写 RTL，Design 名固定为 `cic_d`）。
- **文件命名规则**（来自 u1-l2）：`timing_impl_R16_N4.txt` 表示「时序报告 / 实现阶段 / 抽取率 R=16 / 级数 N=4」。

如果你对「为什么 CIC 滤波器需要抽取、R 和 N 的物理含义」还不清楚，建议先读 u1-l3。本讲只把 R、N 当作实验里的**自变量（可调参数）**来处理，不再重复推导其 DSP 含义。

> 提示：本仓库是**数据/报告仓库**，不是可运行软件。本讲中提到的「实验」「矩阵」都指的是**论文采集这批报告时所设计的实验方案**，而非仓库里某个可执行程序。仓库内没有 HDL 源码、Tcl 脚本或工程文件，复现相关要素在 u3-l6 单独讨论。

---

## 3. 本讲源码地图

本讲把「源码」理解为**仓库内的真实文件**。涉及的关键文件如下：

| 文件 | 作用 |
|---|---|
| `vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt` | 100 MHz 基准点，用于读出时钟周期 10.000 ns 与 WNS |
| `vivado_reports/reports_at_290Mhz/CIC Compiler/timing_impl_R16_N4.txt` | 290 MHz 高速点，时钟周期 3.448 ns |
| `vivado_reports/reports_at_300Mhz/CIC Compiler/timing_impl_R16_N4.txt` | 300 MHz 更高速点，时钟周期 3.333 ns |
| `vivado_reports/reports_at_300Mhz/MATLAB HDL Coder/timing_impl_R16_N4.txt` | 300 MHz 下 MATLAB 方案的对照样本（与 CIC Compiler 同配置可对比） |

此外，本讲大量依赖**目录结构本身**（频率目录、方案目录、`to_delete/` 子目录），这些目录都是仓库内真实存在的条目，下面会逐一用到。

---

## 4. 核心概念与源码讲解

### 4.1 时钟周期与频率换算

#### 4.1.1 概念说明

在 FPGA 时序分析里，「频率」和「时钟周期」描述的是同一件事的两面：

- **频率 \(f\)**（单位 MHz）：时钟每秒跳变多少百万次，是工程师设定目标时常用的量（例如「我想让这个设计跑到 300 MHz」）。
- **时钟周期 \(T\)**（单位 ns）：每次跳变之间的时间间隔，是 Vivado 时序报告里实际出现的量。

二者互为倒数。当频率以 MHz 为单位、周期以 ns 为单位时，换算关系特别简洁：

\[
T(\text{ns}) = \frac{1000}{f(\text{MHz})}
\]

之所以是 1000 而不是 1，是因为 \(1\,\text{s} = 10^9\,\text{ns}\)，而 \(1\,\text{MHz} = 10^6\,\text{Hz}\)，相除得到 \(10^3\)。

为什么要换算？因为 Vivado 的时序约束（`create_period`、时钟定义）和报告里的 `Requirement`、`Period(ns)` 都用**周期**来表达，而论文和目录名（`reports_at_300Mhz`）用的是**频率**。读懂报告的第一步，就是在这两套语言之间自由切换。

#### 4.1.2 核心流程

把频率换算成周期，并把它和报告里的字段对应起来：

1. 由目录名或论文得到目标频率 \(f\)（如 300 MHz）。
2. 用 \(T = 1000/f\) 算出周期（如 \(1000/300 \approx 3.333\) ns）。
3. 在 timing 报告的 **Clock Summary** 里找 `Period(ns)` 字段核对（应为 3.333 ns）。
4. 注意：时钟波形 `Waveform(ns)` 一般写成 \(\{0,\ T/2\}\)，即第一个数为上升沿起点 0，第二个数为高电平持续时间的一半（半周期）。例如周期 3.333 ns 对应波形 \(\{0.000,\ 1.666\}\)，而 \(3.333/2 \approx 1.666\)。

三个频率点的换算结果如下（本讲已用源码核实）：

| 频率（目录名） | \(T = 1000/f\) 理论值 | 报告里 `Period(ns)` | 报告里实际 `Frequency(MHz)` | `Waveform(ns)` |
|---|---|---|---|---|
| 100 MHz | 10.000 ns | 10.000 | 100.000 | \{0.000, 5.000\} |
| 290 MHz | 3.448 ns | 3.448 | 290.023 | \{0.000, 1.724\} |
| 300 MHz | 3.333 ns | 3.333 | 300.030 | \{0.000, 1.666\} |

一个值得注意的细节：报告里 290 MHz 的实际频率写的是 **290.023**、300 MHz 写的是 **300.030**，并不是整数。这是因为周期被四舍五入到 3 位小数（3.448 ns、3.333 ns），再由 Vivado 反算频率时出现了微小偏差（\(1000/3.448 \approx 290.023\)）。这不影响实验结论，但你在写脚本匹配频率时要留意：**别用 `==290` 这种精确相等去判断**，而要按周期或用容差比较。

#### 4.1.3 源码精读

先看 100 MHz 的 Clock Summary。时钟名为 `aclk`，周期 10.000 ns，频率正好 100.000 MHz，波形 \(\{0, 5\}\) 表示占空比 50%（半周期 5 ns）：

[vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:L177-L183](<https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt#L177-L183>) —— 这是 100 MHz 点的 Clock Summary，给出 `aclk` 的周期 10.000 ns 与频率 100.000 MHz。

再看 290 MHz 的同一段。周期变成 3.448 ns，波形 \(\{0, 1.724\}\)（\(3.448/2 = 1.724\)），频率因周期取整显示为 290.023 MHz：

[vivado_reports/reports_at_290Mhz/CIC Compiler/timing_impl_R16_N4.txt:L177-L183](<https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_290Mhz/CIC Compiler/timing_impl_R16_N4.txt#L177-L183>) —— 290 MHz 点的 Clock Summary，周期 3.448 ns，频率 290.023 MHz。

最后是 300 MHz 点，周期 3.333 ns，波形 \(\{0, 1.666\}\)：

[vivado_reports/reports_at_300Mhz/CIC Compiler/timing_impl_R16_N4.txt:L177-L183](<https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_300Mhz/CIC Compiler/timing_impl_R16_N4.txt#L177-L183>) —— 300 MHz 点的 Clock Summary，周期 3.333 ns，频率 300.030 MHz。

把这三份报告的 `Design Timing Summary` 横向放在一起，还能直观看到「周期变短 → 时序变紧」的趋势（这一趋势的深入分析留给 u3-l2 的 fmax 估算，本讲只作引子）。WNS（最差建立时间裕量）随频率升高而急速下降：

| 频率点 | 周期 (ns) | WNS (ns) | 是否满足时序 |
|---|---|---|---|
| 100 MHz | 10.000 | **6.274** | 满足（裕量充足） |
| 290 MHz | 3.448 | **0.276** | 满足（裕量已经很薄） |
| 300 MHz | 3.333 | **0.121** | 满足（濒临违例） |

[vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:L164-L173](<https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt#L164-L173>) —— 100 MHz 的 Design Timing Summary：WNS=6.274 ns，TNS Failing Endpoints=0，并打印 `All user specified timing constraints are met.`。

[vivado_reports/reports_at_290Mhz/CIC Compiler/timing_impl_R16_N4.txt:L164-L173](<https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_290Mhz/CIC Compiler/timing_impl_R16_N4.txt#L164-L173>) —— 290 MHz 的 Design Timing Summary：WNS 降到 0.276 ns，仍满足。

[vivado_reports/reports_at_300Mhz/CIC Compiler/timing_impl_R16_N4.txt:L164-L173](<https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_300Mhz/CIC Compiler/timing_impl_R16_N4.txt#L164-L173>) —— 300 MHz 的 Design Timing Summary：WNS 仅 0.121 ns，已逼近时序收敛临界。

这也正是**为什么论文要设三个频率点**：单一频率下看不出「离时序违例还有多远」，而在 100 / 290 / 300 MHz 三档之间，WNS 从 6.274 ns 一路压到 0.121 ns，能完整刻画「速度—时序裕量」的折中曲线。

#### 4.1.4 代码实践

**实践目标**：亲手完成一次「频率 → 周期 → 报告字段」的完整对照。

**操作步骤**：

1. 打开 `vivado_reports/reports_at_300Mhz/MATLAB HDL Coder/timing_impl_R16_N4.txt`。
2. 找到 `Clock Summary` 一节，读出 `Period(ns)` 和 `Frequency(MHz)`。
3. 用计算器算 \(1000/300\)，与报告里的周期对比。
4. 再算 \(T/2\)，与 `Waveform(ns)` 的第二个数对比。

**需要观察的现象**：

- 周期应为 3.333 ns，频率应为 300.030 MHz（不是整数 300）。
- 波形第二个数应为 1.666（约等于 \(3.333/2\)）。
- 注意 MATLAB 方案的时钟名是 `clk`（不是 CIC Compiler 的 `aclk`），这在后续跨方案对比时是区分二者的小线索。

**预期结果**（本讲已据源码核实）：周期 3.333 ns，频率 300.030 MHz，波形 \(\{0.000, 1.666\}\)，时钟名 `clk`。

[vivado_reports/reports_at_300Mhz/MATLAB HDL Coder/timing_impl_R16_N4.txt:L172-L178](<https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_300Mhz/MATLAB HDL Coder/timing_impl_R16_N4.txt#L172-L178>) —— MATLAB 方案 300 MHz 的 Clock Summary，时钟名 `clk`，周期 3.333 ns。

#### 4.1.5 小练习与答案

**练习 1**：某设计的时钟周期是 4.000 ns，它的频率是多少 MHz？

> **答案**：\(f = 1000/T = 1000/4.000 = 250\) MHz。

**练习 2**：为什么报告里 300 MHz 的实际频率写成了 300.030 而不是正好 300？

> **答案**：因为时钟周期被四舍五入记为 3.333 ns（精确值应为 \(1000/300 = 3.3333\ldots\) ns）。Vivado 用这个已取整的周期反算频率，得到 \(1000/3.333 \approx 300.030\) MHz，从而出现了 0.030 的偏差。这是周期取整带来的，不是设计问题。

---

### 4.2 实验设计矩阵

#### 4.2.1 概念说明

所谓「实验矩阵」，就是论文在做后实现评估时，**系统地遍历多个自变量**而形成的网格。本实验有三个自变量：

- **频率**：100 / 290 / 300 MHz（共 3 档）；
- **抽取率 R**：\(\{4, 8, 16, 32, 64\}\)（共 5 档）；
- **级数 N**：\(\{2, 3, 4, 5, 6\}\)（共 5 档）。

外加一个「离散维度」：**实现方案**（CIC Compiler / MATLAB HDL Coder / Open-source，共 3 种）。

如果三个方案在所有频率、所有 R×N 组合上都做满，理论上是 \(3 \times 3 \times 5 \times 5 = 225\) 个实验点，每个点又包含 timing×{synth,impl} 与 utilization×{synth,impl} 多份报告。实际仓库并没有做满——这正是下一节要分析的「覆盖范围」与「缺口」。

为什么要把实验设计成矩阵？因为 CIC 滤波器的资源与时序对 R、N 都很敏感（N 决定级联深度、R 决定位宽增长），只有**扫描**这两个参数，才能看出趋势；而固定多个频率，则是为了回答「这个设计到底能跑多快」。把维度铺开成矩阵，是为了让结论具有**可比性和完整性**。

#### 4.2.2 核心流程

读懂数据集的实验矩阵，分两步走：

1. **确认参数全集**：从文件名里收集所有出现过的 R、N 值，确认它们确实是 \(\{4,8,16,32,64\}\) 与 \(\{2..6\}\)。命名规则是 `..._R{R}_N{N}.txt`（详见 u1-l2）。
2. **按维度计数**：对「方案 × 频率」的每一个格子，数清楚里面有几个 R×N 组合，从而知道哪些格子是满的、哪些是稀疏的、哪些是空的。

把第 2 步的结果整理成两张表：

- **覆盖矩阵**（方案 × 频率）：每个格子里标「有无数据」或「组合数」（见 4.3 节）。
- **R×N 细节矩阵**（每个方案在每个频率下，哪些 R、N 组合存在）：见 4.4 节的逐格分析。

#### 4.2.3 源码精读

实验矩阵的「源码」就是**文件清单本身**。以 100 MHz、CIC Compiler 为例，该目录下的 `timing_impl_*.txt` 文件名揭示了它覆盖的 R×N 组合。经统计（本讲核实仓库内容），CIC Compiler 在 100 MHz 下的 timing_impl 报告覆盖以下 11 个组合：

```
R8_N6
R16_N4   R16_N5   R16_N6
R32_N4   R32_N5   R32_N6
R64_N3   R64_N4   R64_N5   R64_N6
```

注意几个特征：

- **完全没有 R=4**：CIC Compiler 这一档一个 R4 的报告都没有。
- **完全没有 N=2**：N 从 3 起步，且 N=3 只出现在 R64 下。
- **R=8 只有 N=6 一个点**，矩阵很稀疏。

这与 u2-l4 的结论一致：CIC Compiler 因受 Xilinx IP 参数限制，矩阵比另两个方案稀疏得多。文件名本身可以从这一目录读取核对：[vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:L1-L10](<https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt#L1-L10>) —— 报告头部可见 `Design : cic_compiler_0`，确认这是 CIC Compiler 方案；同目录下其余 `timing_impl_*` 文件名则给出全部 R×N 组合。

作为对比，**MATLAB HDL Coder 在 100 MHz 下是完整的 5×5 = 25 个组合**（R∈{4,8,16,32,64}，N∈{2,3,4,5,6} 全部齐全）。它还有一个区别于另两方案的标志：**Design 名会随参数变化**，例如 R16_N4 这份报告的头部就写着 `Design : CIC_R16_N4`（而 CIC Compiler 恒为 `cic_compiler_0`、Open-source 恒为 `cic_d`）。这一点可在 300 MHz 的 MATLAB 报告头部直接核实：[vivado_reports/reports_at_300Mhz/MATLAB HDL Coder/timing_impl_R16_N4.txt:L3-L9](<https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_300Mhz/MATLAB HDL Coder/timing_impl_R16_N4.txt#L3-L9>) —— 头部 `Design : CIC_R16_N4`，确认 MATLAB 方案的 Design 名编码了 R、N；100 MHz 下同名文件同理（见 `reports_at_100Mhz/MATLAB HDL Coder/` 目录）。

#### 4.2.4 代码实践

**实践目标**：用一条 shell 命令，从文件名直接还原某个方案在某频率下的 R×N 覆盖。

**操作步骤**：

```bash
# 列出 100MHz、Open-source 方案所有 impl 报告对应的 R×N 组合
ls "vivado_reports/reports_at_100Mhz/Open-source CIC" \
  | grep -oE 'timing_impl_R[0-9]+_N[0-9]+' \
  | sort -u
```

**需要观察的现象**：

- 应当输出一组 `timing_impl_R{R}_N{N}`，覆盖 R∈{4,8,16,32,64}。
- 数一下总数：应该是 **24 个**（而不是 25），因为 **Open-source 方案缺少 `R4_N2` 这一个组合**。

**预期结果**（本讲已据仓库内容核实）：24 个组合，缺 `R4_N2`。这印证了 u2-l4 的结论——Open-source 方案只缺 R4_N2 一个点，而 CIC Compiler 缺得更多（整个 R4 与整个 N2）。

> 若你本地没有 shell 环境，这也是一个有效的「源码阅读型实践」：直接在仓库页面浏览 `reports_at_100Mhz/Open-source CIC/` 目录，人工数 `timing_impl_` 文件名即可，结论相同（24 个，缺 R4_N2）。

#### 4.2.5 小练习与答案

**练习 1**：如果三方案在 3 个频率、5 个 R、5 个 N 上全部做满，理论上有多少个 R×N 实验点？

> **答案**：\(3\text{（方案）} \times 3\text{（频率）} \times 5\text{（R）} \times 5\text{（N）} = 225\) 个。注意每个点还对应多份报告（timing/utilization × synth/impl），实际文件数更多。

**练习 2**：CIC Compiler 方案为什么 R×N 组合数远少于另两个方案？

> **答案**：因为 CIC Compiler 是 Xilinx 提供的成品 IP，其可选参数受 IP 本身限制（部分 R、N 组合不被 IP 支持，或不在这批实验范围内），所以矩阵稀疏；而 MATLAB HDL Coder 与 Open-source 都是「自己生成代码」，可以自由设定任意 R、N，因此覆盖更全。

---

### 4.3 方案/频率覆盖范围

#### 4.3.1 概念说明

光知道每个方案覆盖哪些 R×N 还不够——还要知道**每个方案在哪几个频率点上有数据**。这就是「方案 × 频率」覆盖矩阵。它决定了你**能做哪些横向比较**：

- 想「在 300 MHz 下比较三种方案」？得先确认三种方案在 300 MHz 下都有数据。
- 想「看某方案从 100→300 MHz 的时序变化」？得确认该方案在这几个频率下都有数据。

如果某个格子是空的，那么任何需要它的横向对比就做不了，必须改用别的设计。

#### 4.3.2 核心流程

构建覆盖矩阵的方法很直接：

1. 枚举 3 个频率目录：`reports_at_100Mhz/`、`reports_at_290Mhz/`、`reports_at_300Mhz/`。
2. 在每个频率目录下，看有哪些方案子目录（`CIC Compiler/`、`MATLAB HDL Coder/`、`Open-source CIC/`）。
3. 把「存在/不存在」填进 3×3 的表里。

本讲已按此流程核实仓库，结果如下：

| 方案 ＼ 频率 | 100 MHz | 290 MHz | 300 MHz |
|---|---|---|---|
| **CIC Compiler** | ✅ 有（11 组合） | ✅ 有（11 组合） | ✅ 有（11 组合） |
| **MATLAB HDL Coder** | ✅ 有（25 组合） | ❌ 无 | ✅ 有（25 组合） |
| **Open-source CIC** | ✅ 有（24 组合） | ❌ 无 | ❌ 无 |

读这张表能得到三条关键结论：

1. **100 MHz 是唯一「三方案齐全」的频率点**——所以任何「三种方案同台对比」的结论，可靠的数据来源都是 100 MHz（这也是 u2-l4、u3-l1 选择 100 MHz 做横向对比的原因）。
2. **290 MHz 只有 CIC Compiler 一种方案**——这是最显眼的缺口。
3. **300 MHz 缺了 Open-source 方案**——能比 CIC Compiler vs MATLAB HDL Coder，但比不了 Open-source。

#### 4.3.3 源码精读

覆盖矩阵的「源码」就是**目录是否存在**。本讲用文件系统确认了：

- `reports_at_290Mhz/` 下**只有** `CIC Compiler/` 一个方案子目录（外加它里面的 `to_delete/`），没有 `MATLAB HDL Coder/`、也没有 `Open-source CIC/`。这意味着 290 MHz 的所有跨方案对比都无从谈起，能用的只有 CIC Compiler 单方案数据：[vivado_reports/reports_at_290Mhz/CIC Compiler/timing_impl_R16_N4.txt:L1-L10](<https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_290Mhz/CIC Compiler/timing_impl_R16_N4.txt#L1-L10>) —— 290 MHz 仅存的 CIC Compiler 报告头部，`Design : cic_compiler_0`、`Device : 7a100t-csg324`，与 100/300 MHz 同源同器件。
- `reports_at_300Mhz/` 下有 `CIC Compiler/` 和 `MATLAB HDL Coder/`，但**没有** `Open-source CIC/`。300 MHz 下 MATLAB 方案的报告可作 CIC Compiler 的对照：[vivado_reports/reports_at_300Mhz/MATLAB HDL Coder/timing_impl_R16_N4.txt:L1-L10](<https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_300Mhz/MATLAB HDL Coder/timing_impl_R16_N4.txt#L1-L10>) —— 300 MHz 下 MATLAB 方案头部，`Design : CIC_R16_N4`，与同目录 CIC Compiler 的 `cic_compiler_0` 可在同频同配置下对照。
- 三个频率点、所有方案都使用同一目标器件 `7a100t-csg324`（Artix-7、速度等级 -1），保证了「同器件」这一对照前提成立。

#### 4.3.4 代码实践

**实践目标**：用一条命令自动生成「方案 × 频率」覆盖矩阵，而不是靠肉眼数。

**操作步骤**：

```bash
# 对每个频率目录，打印其下存在的方案子目录
for f in 100Mhz 290Mhz 300Mhz; do
  echo "=== $f ==="
  ls "vivado_reports/reports_at_$f"
done
```

**需要观察的现象**：

- `100Mhz` 下应出现 3 个方案目录：`CIC Compiler`、`MATLAB HDL Coder`、`Open-source CIC`。
- `290Mhz` 下应**只**出现 `CIC Compiler`（以及它内部的 `to_delete`），没有另两个方案。
- `300Mhz` 下应出现 `CIC Compiler`、`MATLAB HDL Coder`，**没有** `Open-source CIC`。

**预期结果**（本讲已据仓库核实）：与 4.3.2 节的覆盖表完全一致。这一步是后续所有「跨方案/跨频率」分析的前提——**先确认覆盖范围，再谈比较**。

> 注意：脚本遍历到 `290Mhz/CIC Compiler/` 时会看到 `to_delete` 这个子目录。它**不是方案目录**，而是待清理的重复文件区，处理方式见 4.4 节。

#### 4.3.5 小练习与答案

**练习 1**：我想做「在 290 MHz 下比较三种 CIC 实现的资源占用」，能做吗？为什么？

> **答案**：不能。290 MHz 下只有 CIC Compiler 一种方案有数据（MATLAB 和 Open-source 都缺失），无法做三方案横向对比。能做的只有 CIC Compiler 自身在 290 MHz 的单点分析。

**练习 2**：哪个频率点最适合做「三方案同台资源对比」？为什么？

> **答案**：100 MHz。因为它是唯一一个三种方案都有数据的频率点，能保证在同频、同 R、同 N 下公平比较。

---

### 4.4 数据缺口识别

#### 4.4.1 概念说明

「数据缺口」分两类，需要分开处理：

1. **结构性缺口（真缺口）**：某个方案在某频率下压根没做实验（如 290 MHz 缺 MATLAB 和 Open-source）。这类缺口意味着**该对比从根本上无法进行**，分析时必须如实说明，不能脑补数据。
2. **卫生型缺口（假缺口 / 陷阱）**：数据其实存在，但被放在了 `to_delete/` 之类的目录里，或者存在重复文件。这类「缺口」不是缺数据，而是**多余、重复的数据**，做批量统计时如果不加区分地全部计入，会导致**重复计数**，得出错误的总数。

识别这两类缺口，是把这批数据用对、用准的前提。尤其是在写脚本批量提取 WNS、资源指标时（u3-l5 会专门讲），一个 `to_delete/` 目录就足以让「三方案齐全」的结论变成「数量翻倍」的错误结论。

#### 4.4.2 核心流程

排查缺口的步骤：

1. **画覆盖矩阵**（已在 4.3 完成），空白格即结构性缺口。
2. **检查每个格子内部**是否有异常子目录（如 `to_delete/`、`old/`、`backup/`）。
3. **对重复文件做一致性核对**：用 `diff` 比较疑似重复的文件，确认它们是否真的内容相同。
4. **在统计脚本里显式排除**这些目录，并在论文/报告中**注明被排除的内容**，避免读者误以为「全量统计」。

本仓库里最典型的卫生型陷阱，就是 290 MHz、CIC Compiler 目录下的 `to_delete/`。

#### 4.4.3 源码精读

290 MHz 的 CIC Compiler 目录里，除了正常的报告，还有一个 `to_delete/` 子目录。本讲用 `diff` 核对后发现：**`to_delete/timing_impl_R16_N4.txt` 与上级目录的同名文件内容完全相同**（`diff` 无输出）。该 `to_delete/` 内共放了 11 个 `timing_impl_*.txt` 文件，正好对应 CIC Compiler 在 290 MHz 的全部 11 个 R×N 组合——也就是说，它是这些 timing 报告的**整批副本**：[vivado_reports/reports_at_290Mhz/CIC Compiler/to_delete/timing_impl_R16_N4.txt:L1-L10](<https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_290Mhz/CIC Compiler/to_delete/timing_impl_R16_N4.txt#L1-L10>) —— `to_delete/` 里的副本，头部 `Design : cic_compiler_0`、`Device : 7a100t-csg324`，与上级目录正本完全一致（diff 验证）。

这是一个真实的「假缺口/重复陷阱」：

- 如果你用 `find vivado_reports -name 'timing_impl_*.txt'` 全仓收集文件，290 MHz 的每个组合会被**计两次**（正本 + `to_delete/` 副本）。
- 更隐蔽的是，`to_delete/` 里**只有 timing、且只有 .txt**，没有对应的 `.rpx`、`utilization`、`synth`——格式不完整，混入统计后还会造成「某组合有 timing 却无 utilization」的错觉。

因此本讲强调一条数据纪律：**做任何全量统计前，先排除 `to_delete/`、`old/` 这类目录，并在结果里注明排除了哪些内容。**

至于结构性缺口，本仓库有两个明确的：

- **290 MHz 缺 MATLAB HDL Coder 与 Open-source**（只测了 CIC Compiler）。
- **300 MHz 缺 Open-source**（只测了 CIC Compiler 与 MATLAB HDL Coder）。

#### 4.4.4 代码实践

**实践目标**：亲手验证 `to_delete/` 是重复副本，并体会不加排除会造成的统计偏差。

**操作步骤**：

```bash
# 1) 比对副本与正本是否一致（无输出 = 内容完全相同）
diff "vivado_reports/reports_at_290Mhz/CIC Compiler/to_delete/timing_impl_R16_N4.txt" \
     "vivado_reports/reports_at_290Mhz/CIC Compiler/timing_impl_R16_N4.txt"

# 2) 不加排除地全仓数 timing_impl 文件数（会偏大）
find vivado_reports -name 'timing_impl_R16_N4.txt' | wc -l

# 3) 排除 to_delete 后再数一次
find vivado_reports -name 'timing_impl_R16_N4.txt' -not -path '*/to_delete/*' | wc -l
```

**需要观察的现象**：

- 第 1 步 `diff` 无任何输出，说明两文件逐字节相同。
- 第 2 步的计数会比第 3 步**多 1**——多出来的正是 `to_delete/` 里的那一份副本。
- 列出第 2 步找到的路径，会看到 290 MHz 下 `timing_impl_R16_N4.txt` 出现了两次。

**预期结果**（本讲已据仓库核实）：第 2 步比第 3 步多 1 个；多出的路径在 `reports_at_290Mhz/.../to_delete/` 下。结论：**批量统计必须排除 `to_delete/`**，否则 290 MHz 的 CIC Compiler 数据会被重复计数。

#### 4.4.5 小练习与答案

**练习 1**：`to_delete/` 里的副本，比上级目录的正本少了哪些文件类型？

> **答案**：`to_delete/` 只有 `timing_impl_*.txt` 这一类（11 个），没有对应的 `.rpx`（交互式报告）、`utilization` 报告，也没有 `synth` 报告。所以它是一份**不完整**的副本。

**练习 2**：为什么说「先排除 `to_delete/`，再统计」是一条必要纪律，而不是可有可无的习惯？

> **答案**：因为如果用通配/递归全量收集文件，`to_delete/` 的副本会被计入，导致 290 MHz 下 CIC Compiler 的组合数从真实的 11 被错算成 22（翻倍），进而让「覆盖率」「平均 WNS」等所有聚合指标失真。排除了它，统计才反映真实实验点数。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面的综合任务（这也是本讲的主实践）。

**任务**：为本仓库绘制一张完整的「方案 × 频率」覆盖矩阵，并完成周期换算与缺口定位。

**要求**：

1. **计算三个频率的时钟周期**：用 \(T(\text{ns}) = 1000/f(\text{MHz})\) 算出 100 / 290 / 300 MHz 的周期，并各打开一份对应报告的 `Clock Summary` 核对 `Period(ns)` 字段。
2. **绘制「方案 × 频率」覆盖矩阵**：行是三种方案，列是三个频率，每格标注「有数据 / 无数据」，有数据的再注明 R×N 组合数（CIC Compiler=11、MATLAB=25、Open-source=24）。
3. **指出 290 MHz 下唯一有数据的方案**，并说明这带来的限制（无法做三方案横向对比）。
4. **标注卫生陷阱**：在矩阵旁注明 290 MHz 的 CIC Compiler 下存在 `to_delete/` 重复副本，统计时需排除。

**参考答案（本讲已据仓库核实）**：

周期换算：100 MHz → 10.000 ns；290 MHz → 3.448 ns（报告频率 290.023 MHz）；300 MHz → 3.333 ns（报告频率 300.030 MHz）。

覆盖矩阵：

| 方案 ＼ 频率 | 100 MHz（10.000 ns） | 290 MHz（3.448 ns） | 300 MHz（3.333 ns） |
|---|---|---|---|
| CIC Compiler | ✅ 11 组合 | ✅ 11 组合（⚠️ 含 `to_delete/` 副本） | ✅ 11 组合 |
| MATLAB HDL Coder | ✅ 25 组合 | ❌ 无 | ✅ 25 组合 |
| Open-source CIC | ✅ 24 组合 | ❌ 无 | ❌ 无 |

**290 MHz 下唯一有数据的方案是 CIC Compiler**。限制：290 MHz 无法做任何跨方案对比，只能分析 CIC Compiler 单方案的频率响应（如 WNS 从 100 MHz 的 6.274 ns 降到 290 MHz 的 0.276 ns）。

**卫生陷阱**：`reports_at_290Mhz/CIC Compiler/to_delete/` 含 11 个 timing 副本，与正本逐字节相同，全量统计时必须用 `-not -path '*/to_delete/*'` 排除，否则该频率组合数会被重复计数。

---

## 6. 本讲小结

- **周期与频率互为倒数**：\(T(\text{ns}) = 1000/f(\text{MHz})\)；本仓库三档为 10.000 / 3.448 / 3.333 ns，报告里频率因周期取整显示为 100.000 / 290.023 / 300.030 MHz。
- **实验是三维矩阵**：频率（3 档）× R（5 档）× N（5 档），再加实现方案（3 种），理论满阵 225 点，实际未做满。
- **覆盖并不均匀**：100 MHz 三方案齐全；290 MHz 只有 CIC Compiler；300 MHz 缺 Open-source。
- **R×N 覆盖因方案而异**：MATLAB 最全（25）、Open-source 次之（24，仅缺 R4_N2）、CIC Compiler 最稀疏（11，整段缺 R4 与 N2）。
- **缺口分两类**：结构性缺口（真没数据，须如实说明）与卫生型陷阱（`to_delete/` 重复副本，统计时必须排除）。
- **同器件前提成立**：所有频率、所有方案都使用 `7a100t-csg324`（Artix-7，速度等级 -1），保证了跨频率、跨方案比较的器件一致性。

---

## 7. 下一步学习建议

本讲建立了「实验矩阵」的全局视图，接下来的学习路径建议如下：

- **横向比资源**：在已确认「100 MHz 三方案齐全」的前提下，进入 **u3-l1（资源利用率横向对比分析）**，做同频、同 R、同 N 的三方案 LUT/寄存器/DSP 对比。
- **纵向比速度**：利用本讲发现的「WNS 随频率升高而下降」趋势，进入 **u3-l2（时序收敛与 fmax 分析）**，学习用 WNS 与时钟周期估算最大可达频率。
- **扫参数看趋势**：固定方案与频率、扫描 R/N，进入 **u3-l3（R/N 参数扫描趋势分析）**，看资源与时序对参数的敏感度。
- **批量提取与汇总**：当你想用脚本把整个矩阵的 WNS/资源汇总成表时，进入 **u3-l5（summary.xlsx 汇总与数据提取）**——届时本讲强调的「排除 `to_delete/`」纪律会直接派上用场。

建议在进入 u3 系列前，先回头确认 u2-l1（读时序报告）和 u2-l2（读利用率报告）已经掌握，因为 u3 全部分析都建立在「能从单份报告里准确读出指标」的基础上。
