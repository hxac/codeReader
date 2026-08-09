# 时序收敛与最大频率（fmax）分析

## 1. 本讲目标

本讲是专家层第二篇，承接 [u2-l1 读懂时序报告] 与 [u2-l5 实验矩阵]。你已经会逐节读一份 Vivado 时序报告、也知道这批数据覆盖 100/290/300 MHz 三个频率点。本讲要再往前走一步：**不再只是「读懂」，而是「下判断」**——判断一个设计是否真正时序收敛、离失败还有多远、最高能跑多快。

学完后你应能：

1. 用 WNS（最差建立裕量）衡量设计离时序失败的距离，识别「临界收敛」的高风险点。
2. 理解 Vivado 的多角（Slow/Fast）分析，说清楚为什么建立时间看 Slow 角、保持时间看 Fast 角。
3. 用一条公式 \( f_{\max} \approx 1/(T_{\text{clk}}-\mathrm{WNS}) \) 从一份报告估算最大可达频率，并理解这个估算的局限性。
4. 读懂 TNS 与 Failing Endpoints，知道它们在「设计失败时」如何量化失败规模。

---

## 2. 前置知识

在进入本讲前，请确认你已掌握（来自前置讲义）：

- **建立/保持时间与 Slack**：建立检查问「数据能不能在一个周期内稳定到达」，Slack = Required − Arrival，为正即满足；保持检查问「数据会不会到得太早、冲掉上一拍」。（[u2-l1]）
- **WNS / TNS / WHS / THS** 的字面含义与报告中的位置（`Design Timing Summary` 一节）。（[u2-l1]）
- **三档频率与周期换算**：\( T_{\text{clk}}(\text{ns}) = 1000 / f(\text{MHz}) \)，100 / 290 / 300 MHz 对应 10.000 / 3.448 / 3.333 ns；报告中频率因取整显示为 290.023 / 300.030 等非整数。（[u2-l5]）
- **实验覆盖不均**：100 MHz 是唯一三方案齐全的频率点；290 MHz 仅有 CIC Compiler。（[u2-l5]）

本讲几乎不涉及资源（LUT/寄存器），那是 [u3-l1] 的主题。本讲只盯住「时间」这一个维度。

> 术语提示：**时序收敛（timing closure）** 指一个设计在目标时钟下全部时序检查通过的状态；**fmax（最大频率）** 指设计仍能收敛时所能承受的最高时钟频率。前者是「现在行不行」，后者是「极限在哪」。

---

## 3. 本讲源码地图

本讲的「源码」是四份 Vivado 时序报告（实现阶段，`timing_impl_*`），外加两份用于综合实践验证的 R64_N6 报告。它们都在 `vivado_reports/` 下，由 `report_timing_summary` 命令生成（命令回显在每份报告头部 `Command` 字段，见 [u2-l3]）。

| 文件 | 角色 | 关键数据 |
|---|---|---|
| `reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt` | 宽松时钟下的基准（裕量大） | WNS = 6.274 ns |
| `reports_at_290Mhz/CIC Compiler/timing_impl_R64_N6.txt` | 临界收敛案例（本讲主角之一） | WNS = 0.163 ns |
| `reports_at_300Mhz/CIC Compiler/timing_impl_R16_N4.txt` | 高频下的临界收敛 | WNS = 0.121 ns |
| `reports_at_300Mhz/MATLAB HDL Coder/timing_impl_R16_N4.txt` | 跨方案对照（逻辑级数更少） | WNS = 0.285 ns |
| `reports_at_100Mhz/CIC Compiler/timing_impl_R64_N6.txt` | 综合实践：R64_N6 宽松基准 | WNS = 5.730 ns |
| `reports_at_300Mhz/CIC Compiler/timing_impl_R64_N6.txt` | 综合实践：R64_N6 高频实测 | WNS = 0.103 ns |

所有报告的目标器件一致（`7a100t-csg324`，Artix-7，速度等级 −1），工具一致（Vivado v.2022.2），保证了跨频率、跨方案比较的「同条件」前提。

---

## 4. 核心概念与源码讲解

### 4.1 时序裕量与收敛：从 WNS 看设计离失败有多远

#### 4.1.1 概念说明

「时序收敛」听起来像一句结论（「收敛了」/「没收敛」），但工程上我们更关心的是**离失败还有多远**。这个「距离」就是**裕量（slack）**。

对建立时间检查：

\[ \text{Slack}_{\text{setup}} = \text{Required} - \text{Arrival} \]

- `Required` = 时钟周期 \( T_{\text{clk}} \)（单时钟、单周期路径下，就是目标频率的倒数）。
- `Arrival` = 数据实际从源触发器到达目的触发器的时间（含逻辑延迟 + 布线延迟 + 时钟偏移等）。
- Slack > 0：数据提前于要求时间到达，**满足**；Slack < 0：数据迟到，**违例**。

一个设计有成百上千条路径，每条都有自己的 Slack。我们用一个数概括全局：**WNS（Worst Negative Slack，最差建立裕量）**——所有路径里最小的那个 Slack。WNS 为正，意味着连最差的那条路径也满足，于是全局满足；WNS 为负，则至少有一条路径违例。

> 名字里的 Negative 是历史遗留：WNS 可以是正值。判断收敛只看符号——**WNS ≥ 0 即建立收敛**。

「**临界收敛**」是指 WNS 很小但仍是正数的情况。这时设计名义上满足，但裕量所剩无几，工艺波动、温度变化或一次重新布线都可能把它推翻。本讲要教你的，正是识别这种「擦边过线」的高风险点。

#### 4.1.2 核心流程

判断一个设计的时序状态，按这个顺序看报告：

1. 打开 `Design Timing Summary`，读 WNS、WHS（最差保持裕量）。
2. 看 WNS / WHS 是否 ≥ 0。
3. 看是否出现官方结论行 `All user specified timing constraints are met.`。
4. 看 `TNS Failing Endpoints` 是否为 0（详见 4.4）。
5. 若 WNS 很小（比如 < 周期的 5%），标记为「临界收敛」，结合 fmax 分析评估风险。

裕量随频率的变化规律（直觉）：

\[ \text{WNS} = T_{\text{clk}} - \text{Arrival}_{\text{critical}} \]

频率升高 → \( T_{\text{clk}} \) 变小 → WNS 下降。把同一设计在 100 / 290 / 300 MHz 下分别跑，WNS 会一路收窄，这就是本讲观察的主线。

#### 4.1.3 源码精读

先看 CIC Compiler R16_N4 在宽松的 100 MHz 下的全局结论。报告头部确认这是实现阶段、CIC Compiler IP、目标器件 7a100t-csg324：

[reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:3-9](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L3-L9) —— 工具版本、生成命令、Design 名 `cic_compiler_0`、器件与速度等级。

`Design Timing Summary` 给出全局数字（关键看 WNS 那一列）：

[reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:168-173](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L168-L173) —— WNS=6.274 ns，TNS=0，Failing=0；下方紧跟 `All user specified timing constraints are met.`。

WNS = 6.274 ns，而周期才 10.000 ns——裕量占了周期的 **62.7%**，设计极其宽裕。对比 300 MHz 下同一配置：

[reports_at_300Mhz/CIC Compiler/timing_impl_R16_N4.txt:168-173](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_300Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L168-L173) —— WNS 收窄到 0.121 ns，仍然满足，但裕量只剩周期的 **3.6%**。

把频率从 100 MHz 提到 300 MHz，WNS 从 6.274 ns 塌缩到 0.121 ns——这正是「裕量随频率收窄」的直观写照。0.121 ns 的裕量意味着：任何让关键路径再慢 0.121 ns 的因素，都会让设计翻车。这就是临界收敛。

再看本讲的「临界样本」：CIC Compiler R64_N6 @ 290 MHz。

[reports_at_290Mhz/CIC Compiler/timing_impl_R64_N6.txt:168-173](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_290Mhz/CIC%20Compiler/timing_impl_R64_N6.txt#L168-L173) —— WNS=0.163 ns，周期 3.448 ns，裕量仅占 **4.7%**；结论行仍为「met」，但已擦边。

`Clock Summary` 一节记录了周期与频率的对应，是所有换算的原始依据：

[reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:181-183](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L181-L183) —— `aclk` 波形 `{0.000 5.000}`、周期 10.000 ns、频率 100.000 MHz。

#### 4.1.4 代码实践

**实践目标**：亲手确认「频率升高 → WNS 下降」这条规律，并学会用「裕量占周期百分比」判断风险。

**操作步骤**：

1. 分别打开三份报告的 `Design Timing Summary`，记录 WNS：
   - `reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt`（行 170）
   - `reports_at_300Mhz/CIC Compiler/timing_impl_R16_N4.txt`（行 170）
   - `reports_at_290Mhz/CIC Compiler/timing_impl_R64_N6.txt`（行 170）
2. 从各自 `Clock Summary` 读出周期 \( T_{\text{clk}} \)。
3. 计算裕量占比：\( \mathrm{WNS} / T_{\text{clk}} \times 100\% \)。

**需要观察的现象**：WNS 随频率单调下降；占比从 60% 量级跌到个位数百分点。

**预期结果**（可直接核对）：

| 配置 | 周期 \(T_{\text{clk}}\) | WNS | 裕量占比 | 判断 |
|---|---|---|---|---|
| R16_N4 @100MHz | 10.000 ns | 6.274 ns | 62.7% | 极宽裕 |
| R64_N6 @290MHz | 3.448 ns | 0.163 ns | 4.7% | 临界 |
| R16_N4 @300MHz | 3.333 ns | 0.121 ns | 3.6% | 临界 |

#### 4.1.5 小练习与答案

**练习 1**：某设计在 200 MHz（周期 5 ns）下 WNS = −0.200 ns。它时序收敛了吗？最差路径的数据到达时间大约是多少？

**参考答案**：没有收敛（WNS < 0）。由 \( \text{Arrival} = T_{\text{clk}} - \text{WNS} = 5.000 - (-0.200) = 5.200 \) ns，最差路径比一个周期还慢 0.2 ns。

**练习 2**：为什么不能只看「是否出现 met 结论」，还要看 WNS 的具体数值？

**参考答案**：met 只说明 WNS ≥ 0，但不说明裕量大小。WNS = 0.001 ns 和 WNS = 5 ns 都是「met」，但前者是临界收敛，几乎不可靠（一次重布线就可能违例），后者才真正稳健。

---

### 4.2 多角分析：为何建立看 Slow、保持看 Fast

#### 4.2.1 概念说明

FPGA 在真实世界里要面对工艺（process）、电压（voltage）、温度（temperature）的波动，统称 **PVT**。同一份电路，在「慢」条件下晶体管开关慢、延迟大；在「快」条件下开关快、延迟小。Vivado 的静态时序分析器不会只算一次，而是**在多个工艺角（corner）下分别算每条路径**，再把最差的结果报给你——这叫**多角分析（multi-corner analysis）**。

为什么建立和保持看不同的角？直觉如下：

- **建立时间（setup）怕慢**：数据要在一个周期内「赶到」。延迟越大越赶不上。所以最严苛的建立检查出现在延迟最大的角——**Slow 角**。
- **保持时间（hold）怕快**：保持检查要求新数据不能「到得太早」、把上一拍还没采样的数据冲掉。延迟越小，数据冲得越快，越容易违例。所以最严苛的保持检查出现在延迟最小的角——**Fast 角**。

于是报告里的规则是：**建立取 Slow 角最差，保持取 Fast 角最差**。所谓「多角取最差」，就是同一类检查，跨所有工艺角取 Slack 最小的那个作为该检查的最终 WNS / WHS。这保证报告里的数是最悲观的，留给现实波动以余量。

#### 4.2.2 核心流程

1. 报告头部 `Timer Settings` 一节确认多角分析已开启，并列出分析的角。
2. 在路径明细里看 `Path Type` 字段：建立路径标注 `Setup (Max at Slow Process Corner)`，保持路径标注 `Hold (Min at Fast Process Corner)`。
3. 全局 WNS 来自 Slow 角的建立检查；全局 WHS 来自 Fast 角的保持检查。
4. `Pessimism Removal`（CPR）会把时钟公共路径在 Slow/Fast 两角下的重复悲观扣除，避免「自己跟自己比」造成的虚假违例。

#### 4.2.3 源码精读

`Timer Settings` 一节显示多角分析开启，且 Slow / Fast 两个角都被分析：

[reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:19-34](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L19-L34) —— `Enable Multi Corner Analysis: Yes`；下面的表里 Slow、Fast 两角的 Max Paths / Min Paths 均为 `Yes`。

最差**建立**路径的明细，`Path Type` 直接写明取自哪个角：

[reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:258-261](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L258-L261) —— `Path Type: Setup (Max at Slow Process Corner)`；Requirement 10.000 ns；Data Path Delay 3.726 ns（逻辑 1.930 / 布线 1.796）；Logic Levels 7。

最差**保持**路径（在报告下方的 `Min Delay Paths` 段），角随之切换：

[reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:1048-1055](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L1048-L1055) —— `Slack (MET): 0.044ns`，`Path Type: Hold (Min at Fast Process Corner)`，Requirement 0.000 ns（同沿检查）。

注意 `Requirement` 的差别：建立检查的 Requirement = 一个周期（10.000 ns），保持检查的 Requirement = 0.000 ns（因为保持是同一时钟沿的「数据 vs 时钟」竞速，起点相同）。这也是为什么 WHS 的绝对值天然很小（0.044 ns），不能用建立裕量的尺度去衡量它。

在 290 MHz 的临界样本里，CPR 把时钟公共路径的悲观补回来 0.339 ns，这是 Slow/Fast 两角分析的配套修正：

[reports_at_290Mhz/CIC Compiler/timing_impl_R64_N6.txt:262-266](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_290Mhz/CIC%20Compiler/timing_impl_R64_N6.txt#L262-L266) —— Clock Path Skew −0.031 ns（= DCD − SCD + CPR），其中 CPR = 0.339 ns。

#### 4.2.4 代码实践

**实践目标**：在一份报告里同时找到建立与保持各自的工艺角标注，验证「建立 Slow、保持 Fast」。

**操作步骤**：

1. 打开 `reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt`。
2. 在 `Max Delay Paths` 段（约行 250）找到第一条建立路径，读 `Path Type` 与 `Slack (MET)`。
3. 翻到 `Min Delay Paths` 段（约行 1046），找到第一条保持路径，读 `Path Type` 与 `Slack (MET)`。

**需要观察的现象**：两段的 `Path Type` 角不同；建立 Requirement = 10.000 ns，保持 Requirement = 0.000 ns。

**预期结果**：建立路径 `Path Type: Setup (Max at Slow Process Corner)`，Slack = 6.274 ns；保持路径 `Path Type: Hold (Min at Fast Process Corner)`，Slack = 0.044 ns。

#### 4.2.5 小练习与答案

**练习 1**：为什么建立检查取 Slow 角、保持检查取 Fast 角，而不是都用同一个角？

**参考答案**：建立怕数据迟到，迟到在「最慢」角最严重，故取 Slow；保持怕数据早到，早到在「最快」角最严重，故取 Fast。各取最悲观角，确保任何 PVT 条件下都不违例。

**练习 2**：某报告 WNS = 0.5 ns（Slow 角）。如果芯片实际运行在 Fast 角，建立裕量会更大还是更小？

**参考答案**：更大。Fast 角延迟更小，Arrival 更短，建立 Slack 更大。报告报 Slow 角的 0.5 ns 是最差情况，实际 Fast 角下裕量只会更好——这正是多角分析「留余量」的意义。

---

### 4.3 fmax 估算：用 WNS 推算最高频率

#### 4.3.1 概念说明

**fmax（最大频率）** 是一个设计在仍能时序收敛的前提下，所能承受的最高时钟频率。它回答「这个设计最快能跑多快」。

Vivado 报告不会直接给你 fmax，但你能从 WNS 估出来。推导很简单：当前周期 \( T_{\text{clk}} \)、当前最差建立裕量 WNS。如果把周期再缩短 WNS 这么多，最差路径就恰好「擦边」（Slack = 0）。于是最短可承受周期约为 \( T_{\text{clk}} - \mathrm{WNS} \)，对应最高频率：

\[ f_{\max} \approx \frac{1}{T_{\text{clk}} - \mathrm{WNS}} \]

（\( T_{\text{clk}} \) 单位取 ns 时，结果单位为 GHz；×1000 得 MHz。）

等价地，因为 \( \text{WNS} = T_{\text{clk}} - \text{Arrival}_{\text{critical}} \)，所以 \( T_{\text{clk}} - \mathrm{WNS} \approx \text{Arrival}_{\text{critical}} \)，即关键路径的实际到达时间。这与你能在路径明细里读到的 `Data Path Delay`（再叠加时钟偏移等）一致。

> **重要 caveat（本讲核心洞见）**：fmax 估算是对**某一次具体综合+实现跑（run）**而言的。当你把时钟约束得宽松（如 100 MHz），工具有大量裕量，布局布线「不上紧」，关键路径偏慢，算出的 fmax 偏**悲观**；当你把约束收紧到接近极限（如 290/300 MHz），工具会拼命优化布局布线，关键路径变快，算出的 fmax 更接近**真实极限**。所以：**可信的 fmax 估计来自最贴近失败点（WNS 小但为正）的那次 run**，而不是宽松那次。

#### 4.3.2 核心流程

1. 从报告 `Design Timing Summary` 取 WNS，从 `Clock Summary` 取 \( T_{\text{clk}} \)。
2. 代入 \( f_{\max} \approx 1/(T_{\text{clk}} - \mathrm{WNS}) \)。
3. 若有多档频率的同配置报告，分别算，比较哪次估计最可信（最紧的那次）。
4. 用最紧一次的 fmax 预测「再高一档频率是否可行」，并用更高频率的实测报告验证。

#### 4.3.3 源码精读

以 CIC Compiler R16_N4 为例，跨两档频率估算：

**@100 MHz**：WNS = 6.274 ns，\( T_{\text{clk}} \) = 10.000 ns。
\[ f_{\max} \approx \frac{1}{10.000 - 6.274} = \frac{1}{3.726\,\text{ns}} \approx 268.4\,\text{MHz} \]

注意 \( 10.000 - 6.274 = 3.726 \) ns，**正好等于**该路径的 `Data Path Delay`（3.726 ns），印证了「最短周期 ≈ 关键路径到达时间」：

[reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:259-260](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L259-L260) —— Requirement 10.000 ns；Data Path Delay 3.726 ns（逻辑 1.930 / 布线 1.796）。

**@300 MHz**：WNS = 0.121 ns，\( T_{\text{clk}} \) = 3.333 ns。
\[ f_{\max} \approx \frac{1}{3.333 - 0.121} = \frac{1}{3.212\,\text{ns}} \approx 311.3\,\text{MHz} \]

[reports_at_300Mhz/CIC Compiler/timing_impl_R16_N4.txt:259-261](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_300Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L259-L261) —— Requirement 3.333 ns；Data Path Delay 3.210 ns（逻辑 1.764 / 布线 1.446）；Logic Levels 5。

**对照读数**：100 MHz 那次估出 fmax ≈ 268 MHz，可真实情况是设计在 300 MHz 仍收敛（WNS = +0.121）。268 < 300，说明宽松 run 的估算**偏悲观**——工具在 100 MHz 下「没使劲」，关键路径做到 3.726 ns；而在 300 MHz 约束下工具把关键路径压到了 3.210 ns。这正是上面 caveat 的活教材：**要信 300 MHz 那次（≈311 MHz），不要信 100 MHz 那次（≈268 MHz）**。

再看跨方案的对比，体会「逻辑级数少 → 关键路径短 → fmax 高」：300 MHz 下，MATLAB HDL Coder 的 R16_N4 逻辑级数只有 4（CIC Compiler 是 5），且 fmax 估算更高：

[reports_at_300Mhz/MATLAB HDL Coder/timing_impl_R16_N4.txt:255-257](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_300Mhz/MATLAB%20HDL%20Coder/timing_impl_R16_N4.txt#L255-L257) —— Requirement 3.333 ns；Data Path Delay 3.097 ns（逻辑 1.400 / 布线 1.697）；Logic Levels 4（CARRY4=3, LUT2=1）。

MATLAB 版 WNS = 0.285 ns（见行 165），\( f_{\max} \approx 1/(3.333-0.285) = 1/3.048 \approx 328.1 \) MHz，确实高于 CIC Compiler 的 311 MHz。注意它的 Design 名是 `CIC_R16_N4`、时钟叫 `clk`（CIC Compiler 叫 `aclk`），布线延迟占比更大（54.8% vs 45.1%）——这些是 [u2-l4] 讲过的方案签名，这里它们直接影响了 fmax。

#### 4.3.4 代码实践

**实践目标**：用一条公式从 WNS 估算 fmax，并体会「宽松 run 偏悲观」。

**操作步骤**：

1. 取 CIC Compiler R16_N4 @100MHz 的 WNS（6.274）与周期（10.000），算 fmax。
2. 取 @300MHz 的 WNS（0.121）与周期（3.333），算 fmax。
3. 比较两个估计值，回答「哪个更可信、为什么」。

**需要观察的现象**：两次估算给出明显不同的 fmax，宽松那次更低。

**预期结果**：@100MHz ≈ 268.4 MHz（悲观，不可信）；@300MHz ≈ 311.3 MHz（贴近极限，可信）。原因：约束越紧，工具布局布线越激进，关键路径越短。

> 待本地验证：若你在本地 Vivado 重跑，把同一 R16_N4 分别约束在 270 / 280 / 290 / 300 MHz，会看到 WNS 从正单调下降到接近 0，fmax 估计随之收敛到真实极限——这是验证 caveat 的最直接方式。本仓库只存了 100/290/300 三档成品报告，无法在此复现中间档，标注待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：某设计 @200 MHz（5 ns）WNS = 1.0 ns。估算 fmax。若把时钟直接设到这个 fmax，WNS 会是多少？

**参考答案**：\( f_{\max} \approx 1/(5.000 - 1.000) = 1/4.000\,\text{ns} = 250 \) MHz。若设到 250 MHz（周期 4.000 ns），理想情况下 WNS ≈ 0（擦边）。实际因重新布线，可能略正或略负，这正是临界点的风险。

**练习 2**：为什么用 100 MHz 报告估出的 fmax，常常低于设计实际能达到的频率？

**参考答案**：100 MHz 约束宽松，WNS 很大，工具没有动力把关键路径压到最短，布局布线「松散」，于是 \( T_{\text{clk}}-\mathrm{WNS} \) 偏大、fmax 偏低。紧约束 run 才逼出真实最快路径。

---

### 4.4 TNS Failing Endpoints：失败规模的量化指标

#### 4.4.1 概念说明

WNS 告诉你「有没有违例」（一个布尔判断），但当 WNS < 0、设计真的失败时，你还需要知道「**失败有多严重**」——是只有一两条路径擦边，还是几千条路径集体崩盘？这两个问题由 **TNS** 和 **Failing Endpoints** 回答。

- **TNS（Total Negative Slack）**：所有**负** Slack 的总和（取绝对值累加）。它衡量「总共违例了多少纳秒」。若所有路径都满足（无负 Slack），TNS = 0。
- **TNS Failing Endpoints**：Slack 为负的路径终点（endpoint）数量，告诉你「有多少条路径违例」。
- **TNS Total Endpoints**：参与检查的路径终点总数，是分母。

直觉：WNS 是「最差一条」，TNS 是「所有差的总和」，Failing Endpoints 是「差的条数」。一个设计若 WNS = −0.01 ns 但 Failing Endpoints = 1，那几乎算修好了（一条路径微调即可）；若 WNS = −2 ns 且 Failing Endpoints = 500，那就是系统性问题，要大改。

> 本数据集的全部设计**都满足时序**（WNS ≥ 0），所以下面看到的 TNS 与 Failing Endpoints 全是 0。本模块要教的是「这些字段为非零时的含义」，并学会用它们的零值来**确认收敛**。

#### 4.4.2 核心流程

1. 在 `Design Timing Summary` 或 `Intra Clock Table` 读 `TNS(ns)`、`TNS Failing Endpoints`、`TNS Total Endpoints`。
2. 若 WNS ≥ 0：必有 TNS = 0、Failing = 0，收敛确认。
3. 若 WNS < 0：读 TNS 看违例总量，读 Failing Endpoints 看违例广度，据此判断修复难度。
4. 结合官方结论行 `All user specified timing constraints are met.`（满足时出现）做最终判定。

#### 4.4.3 源码精读

CIC Compiler R16_N4 @100MHz 的全局表，TNS 与 Failing 全为 0：

[reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:168-170](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L168-L170) —— `WNS 6.274 / TNS 0.000 / TNS Failing 0 / TNS Total 386`；保持侧 `WHS 0.044 / THS 0.000 / THS Failing 0 / THS Total 386`。

`Timing Details` 段把同一信息再说一遍，并区分 Setup / Hold / PW（pulse width）三类检查：

[reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:244-246](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L244-L246) —— `Setup: 0 Failing Endpoints, Worst Slack 6.274ns, Total Violation 0.000ns`；Hold 与 PW 同为 0 Failing。

到了 290 MHz 的临界样本，WNS 只剩 0.163 ns，但 Failing Endpoints **仍然为 0**——这就是「临界但收敛」的完整证据：最差路径擦边过线，没有任何一条违例：

[reports_at_290Mhz/CIC Compiler/timing_impl_R64_N6.txt:244-246](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_290Mhz/CIC%20Compiler/timing_impl_R64_N6.txt#L244-L246) —— `Setup: 0 Failing Endpoints, Worst Slack 0.163ns, Total Violation 0.000ns`。

> 反事实推演（非仓库数据，仅说明含义）：假设这份 290 MHz 报告的 WNS 变成 −0.050 ns，那么 `TNS Failing Endpoints` 可能显示 `3`、`Total Violation 0.120ns`——意思是 719 个终点里有 3 条违例、累计违例 0.120 ns。这种「少量、小幅」违例通常靠局部插入寄存器或调整布局即可修复；而若显示 `Failing 400 / Total Violation 80ns`，则是架构性问题。本仓库无失败样本，故为推演，标注为说明性示例。

#### 4.4.4 代码实践

**实践目标**：用 TNS / Failing Endpoints 的零值确认收敛，并理解非零含义。

**操作步骤**：

1. 打开 `reports_at_290Mhz/CIC Compiler/timing_impl_R64_N6.txt`。
2. 读行 170 的 `TNS Failing Endpoints` 与 `TNS Total Endpoints`。
3. 读行 173 的官方结论行。
4. 回答：WNS 这么小（0.163），有没有任何一条路径违例？

**需要观察的现象**：尽管 WNS 极小，Failing Endpoints 仍为 0，结论行仍为 met。

**预期结果**：`TNS Failing Endpoints = 0`，`TNS Total Endpoints = 719`；`All user specified timing constraints are met.`——确认「临界但完全收敛，零违例」。

#### 4.4.5 小练习与答案

**练习 1**：两个设计都 WNS < 0。A：TNS = 0.05 ns，Failing = 1；B：TNS = 30 ns，Failing = 600。哪个更难修？

**参考答案**：B 难得多。B 有 600 条路径违例、累计 30 ns，是系统性问题（如组合路径过长、时钟域问题），要动结构；A 只有 1 条路径擦边 0.05 ns，通常一点布局调整或加一级流水就能解决。

**练习 2**：为什么说「TNS = 0」与「WNS ≥ 0」是等价说法？

**参考答案**：TNS 是所有负 Slack 的和。只要有一条路径 WNS < 0，TNS 就一定 > 0；反之若 TNS = 0，说明没有任何负 Slack，即所有路径 Slack ≥ 0，亦即 WNS ≥ 0。所以二者互为充要条件（对同一类检查而言）。

---

## 5. 综合实践：CIC Compiler R64_N6 的 fmax 追踪与 300 MHz 验证

本任务把本讲四个模块串起来：对一个固定配置（CIC Compiler、R64_N6），跨频率追踪 WNS、估算 fmax、用最紧一次的估计预测 300 MHz 是否可行，最后**用仓库里真实存在的 300 MHz 报告验证你的预测**。

**实践目标**：亲手完成一次「fmax 推算 → 预测 → 实测验证」的完整闭环。

**操作步骤**：

1. 打开 `reports_at_100Mhz/CIC Compiler/timing_impl_R64_N6.txt`，记录 WNS（行 170 / 行 244）与周期（行 183）。
2. 打开 `reports_at_290Mhz/CIC Compiler/timing_impl_R64_N6.txt`，同样记录 WNS（行 170 / 行 244）与周期（行 183）。
3. 对这两档分别用 \( f_{\max} \approx 1/(T_{\text{clk}}-\mathrm{WNS}) \) 估算 fmax。
4. 回答：哪一档的 fmax 估计更可信？基于可信估计，R64_N6 在 300 MHz（周期 3.333 ns）是否仍可能满足时序？
5. 打开 `reports_at_300Mhz/CIC Compiler/timing_impl_R64_N6.txt`，读实测 WNS（行 170 / 行 244），验证你的预测。

**需要观察的现象**：宽松档（100 MHz）估出的 fmax 偏低、会误判 300 MHz 不可行；紧档（290 MHz）估出的 fmax 略高于 300 MHz、预测可行；实测 300 MHz WNS 仍为正（擦边收敛）。

**参考结果**（可直接核对，你算出来的应与此一致）：

| 频率档 | 周期 \(T_{\text{clk}}\) | WNS | \(T_{\text{clk}}-\mathrm{WNS}\) | fmax 估算 | 可信度 |
|---|---|---|---|---|---|
| 100 MHz | 10.000 ns | 5.730 ns | 4.270 ns | ≈ 234.2 MHz | 低（宽松 run，偏悲观） |
| 290 MHz | 3.448 ns | 0.163 ns | 3.285 ns | ≈ 304.4 MHz | 高（贴近极限） |
| 300 MHz（实测） | 3.333 ns | 0.103 ns | 3.230 ns | ≈ 309.6 MHz | 实测收敛 |

**结论**：

- 用 100 MHz 的估计（234 MHz）会**错误地**认为 300 MHz 不可行——这是宽松 run 的悲观陷阱。
- 用 290 MHz 的估计（304 MHz）预测：300 MHz 略低于估算的 fmax，**应该擦边可行**。
- 实测 300 MHz 报告：WNS = **+0.103 ns**，`0 Failing Endpoints`，`All user specified timing constraints are met.`——**确实满足**，但裕量只剩 3.1%，属典型临界收敛。

参考最差路径数据（用于自检你的读数）：
- @100MHz：Requirement 10.000 ns，Data Path Delay 4.266 ns，Logic Levels 9。[报告行 259-261](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R64_N6.txt#L259-L261)
- @290MHz：Requirement 3.448 ns，Data Path Delay 3.281 ns，Logic Levels 9。[报告行 259-261](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_290Mhz/CIC%20Compiler/timing_impl_R64_N6.txt#L259-L261)
- @300MHz：Requirement 3.333 ns，Data Path Delay 3.274 ns，Logic Levels 7。[报告行 259-261](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_300Mhz/CIC%20Compiler/timing_impl_R64_N6.txt#L259-L261)

> 注意一个细节：从 290 MHz 的 9 级逻辑到 300 MHz 的 7 级逻辑，工具为了满足更紧的约束重排了关键路径（逻辑级数下降、关键路径换了一条）。这恰好印证了 4.3 的 caveat——不同频率是不同的 P&R run，关键路径并非固定不变，所以 fmax 估计也随 run 而变。

---

## 6. 本讲小结

- **WNS 是时序收敛的总开关**：WNS ≥ 0 即建立收敛，但还要看裕量大小；裕量占周期比例 < 5% 即属临界收敛（如 R64_N6 @290MHz 仅 4.7%、R16_N4 @300MHz 仅 3.6%）。
- **多角取最差**：建立检查在 Slow 角最严（怕慢），保持检查在 Fast 角最严（怕快）；报告 WNS 来自 Slow 角建立、WHS 来自 Fast 角保持，`Path Type` 字段会标明。
- **fmax 估算公式**：\( f_{\max} \approx 1/(T_{\text{clk}}-\mathrm{WNS}) \approx 1/\text{关键路径到达时间} \)。
- **fmax 估算有条件**：宽松 run（大 WNS）的估算是悲观的，可信估计来自最贴近失败点的紧 run；实测 R64_N6 在 100/290/300 MHz 的 fmax 估计分别为 234 / 304 / 310 MHz，越紧越接近真实极限。
- **TNS / Failing Endpoints 量化失败规模**：本数据集全部满足（TNS=0、Failing=0）；非零时，Failing Endpoints 给违例条数、TNS 给违例总量，用以判断修复难度。
- **跨方案差异影响 fmax**：同配置下 MATLAB HDL Coder（逻辑级数 4）比 CIC Compiler（逻辑级数 5）关键路径更短、fmax 更高（@300MHz 约 328 MHz vs 311 MHz）。

---

## 7. 下一步学习建议

- 本讲只分析了「频率」这一个维度对 WNS/fmax 的影响。下一步读 [u3-l3 R/N 参数扫描趋势分析]，看在固定频率下，**改变抽取率 R 和级数 N** 时资源与时序如何变化——那会把 WNS 放进一个二维参数空间。
- 若你对「资源构成为何影响关键路径」感兴趣，可回看 [u3-l1 资源利用率横向对比分析] 中 SRL16E、CARRY4、控制集的讨论，它们正是本讲 Logic Levels 与布线延迟差异的根源。
- 想把本讲方法变成自动化脚本，批量从几百份 `timing_impl_*.txt` 抽取 WNS/周期算 fmax，请读 [u3-l5 summary.xlsx 汇总与数据提取]；注意排除 290 MHz 下的 `to_delete/` 重复副本，以免 fmax 统计被重复计数污染（见 [u2-l5]）。
- 若要自行复现本讲的频率扫描（补齐 270 / 280 MHz 等中间档以画出 WNS-频率曲线），参考 [u3-l6 复现实验的方法论]，但需自行准备 HDL/Tcl/约束（仓库不含，待确认）。
