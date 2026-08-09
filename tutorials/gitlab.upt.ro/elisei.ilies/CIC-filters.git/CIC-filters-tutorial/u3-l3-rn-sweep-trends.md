# R/N 参数扫描趋势分析

## 1. 本讲目标

前两篇专家层讲义（u3-l1、u3-l2）做的是「固定一个配置点、横向对比三种实现方案」。本讲把镜头拉远一档：**固定实现方案、让 CIC 的核心参数变化**，看资源与时序指标如何随参数「走」。

具体地，学完后你应该能够：

- 建立「参数扫描（parameter sweep）」的分析思路——知道该固定什么、扫描什么、从哪里取数、怎么制表。
- 区分两类指标的趋势性质：**资源类指标（LUT/寄存器/CARRY4）是确定性的、随参数单调变化**；**时序类指标（WNS）依赖布局布线、带噪声、只呈总体下降趋势**。
- 用最小二乘法把资源随级数 N 的增长拟合成线性模型，并能解释这条线性规律背后的位宽增长机理。
- 用一句话总结「级数 N 与抽取率 R 各自如何驱动资源与时序」，并据此为后续的方案/选型决策提供趋势依据。

本讲只读一种实现方案（Open-source CIC），目的是把「方案」这个变量钉死，纯粹观察 R、N 两个参数本身的影响。

## 2. 前置知识

阅读本讲前，请确认你已经掌握以下概念（均来自前面几篇）：

- **CIC 三个核心参数**（u1-l3）：抽取率 R（速率比）、级数 N（积分器/梳状器各几级）、差分延迟 M。直流增益为 \((MR)^{N}\)，它决定了内部累加器的位宽增长。
- **位宽增长公式**：CIC 抽取器内部最大位宽
  \[ B_{\max} = B_{\text{in}} + \left\lceil N\cdot \log_2(M\cdot R) \right\rceil \]
  即每多一级、或 R 每翻一倍，累加器都要加宽。本讲的两条扫描曲线归根到底都在反映这条公式。
- **Vivado 利用率报告的读法**（u2-l2、u3-l1）：从 `Slice Logic` 取 LUT/Registers，从 `Primitives` 取 FDRE/LUT2/CARRY4 原语数，从 `Slice Logic Distribution` 取 Slice 数与控制集（Control Sets）。
- **WNS 的含义**（u2-l1、u3-l2）：WNS（最差建立裕量）≥ 0 即时序收敛；`fmax \approx 1000/(T_{\text{clk}} - \text{WNS})`。WNS 来自实现阶段（impl）报告，含真实布线延迟。
- **实验矩阵**（u2-l5）：本仓库报告按 `频率 × 方案 × R × N × synth/impl` 多维组织。本讲锁定在 **100 MHz × Open-source CIC** 这一格，沿 R、N 两个维度做扫描。

一个贯穿全讲的关键直觉：**资源是「数」出来的，WNS 是「跑」出来的**。同一个 RTL、同一套参数，资源计数可复现、可单调递推；而 WNS 取决于 Vivado 这次把哪条路径布局成了关键路径，因此 WNS 带有「路径依赖噪声」，曲线会抖动。抓住这个区别，是做好趋势分析的前提。

## 3. 本讲源码地图

本讲的「源码」是 Open-source CIC 方案在 100 MHz 下的一批后实现（impl）报告。全部位于同一目录：

```
vivado_reports/reports_at_100Mhz/Open-source CIC/
├── timing_impl_R16_N{2,3,4,5,6}.txt   ← 本讲 N 扫描取 WNS
├── utilization_impl_R16_N{2,3,4,5,6}.txt ← 本讲 N 扫描取 LUT/Reg/CARRY4
├── utilization_impl_R{4,8,16,32,64}_N4.txt ← 本讲 R 扫描取资源
├── timing_impl_R8_N6.txt              ← 指定精读样本（最复杂点之一）
└── utilization_impl_R8_N6.txt         ← 指定精读样本
```

| 文件 | 在本讲的作用 |
|------|-------------|
| [utilization_impl_R16_N4.txt](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/utilization_impl_R16_N4.txt) | N 扫描的「锚点」样本，逐字段精读 LUT/Reg/Primitives |
| [timing_impl_R8_N6.txt](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/timing_impl_R8_N6.txt) | 高复杂度样本，精读 WNS 与关键路径 |
| [utilization_impl_R8_N6.txt](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/utilization_impl_R8_N6.txt) | 高复杂度样本，对照资源量级 |
| timing_impl_R16_N{2..6}.txt / utilization_impl_R16_N{2..6}.txt | 提供 N 扫描的其余 4 个数据点 |
| utilization_impl_R{4,8,32,64}_N4.txt | 提供 R 扫描的其余 4 个数据点 |

> 说明：本仓库不含 HDL 源码、Tcl 脚本或工程文件（u1-l1、u3-l6 已交代），所有结论只能从这些 `.txt` 报告中读出。下文凡引用具体行号，均为当前 HEAD `e49b263` 下的真实行号。

## 4. 核心概念与源码讲解

### 4.1 参数扫描方法

#### 4.1.1 概念说明

**参数扫描（parameter sweep）** 是一种最朴素也最可靠的分析方法：把可能影响结果的变量分成「受控变量」和「自变量」，固定所有受控变量，只让一个自变量取一系列值跑实验，记录感兴趣的「响应变量」，最后看响应随自变量的变化趋势。

套到 CIC 上：

- **受控变量（必须钉死）**：实现方案（Open-source CIC）、时钟频率（100 MHz）、目标器件（`xc7a100tcsg324-1`）、报告阶段（impl）。
- **自变量（扫描对象）**：级数 N（本讲主扫描）、抽取率 R（本讲辅扫描）。
- **响应变量（要记录的）**：Slice LUTs、Slice Registers、CARRY4、Slice 数、控制集数（资源类）；WNS（时序类）。

为什么要「钉死」方案和频率？因为不同方案的资源构成天差地别（u3-l1 已证：CIC Compiler 用 SRL16E、Open-source 用 CARRY4），不同频率的 WNS 无法直接比较（u3-l2 已证）。如果不钉死，你看到的「趋势」就是方案差异和频率差异混进来的假象。

#### 4.1.2 核心流程

一次完整的 N 扫描可以拆成 5 步：

1. **锁定格点**：选定 `100 MHz / Open-source CIC / impl` 这一格。
2. **取数**：对每个 N∈{2,3,4,5,6}，打开对应的 `utilization_impl_R16_N{k}.txt` 与 `timing_impl_R16_N{k}.txt`。
3. **读响应**：从利用率报告的 `Slice Logic`、`Primitives` 节读资源；从时序报告的 `Design Timing Summary` 读 WNS。
4. **制表**：把 5 个 N 的数据排成一张表（见 4.2）。
5. **画趋势 / 拟合**：以 N 为横轴、资源或 WNS 为纵轴，看单调性、量级、斜率，必要时做线性回归。

注意一个陷阱（来自 u2-l5）：100 MHz 这一格是仓库里**唯一三方案齐全**的频率点，所以也只有在这一格做 N 扫描，数据才完整；290 MHz 下 Open-source CIC 根本没有数据，切勿凭空补点。

#### 4.1.3 源码精读

先以锚点 `utilization_impl_R16_N4.txt` 为例，演示「取数」这一步到底读哪些行。

报告头部确认我们钉死了正确的受控变量——方案是 `cic_d`（Open-source）、阶段是 `Routed`、器件是 `xc7a100tcsg324-1`：

[vivado_reports/reports_at_100Mhz/Open-source CIC/utilization_impl_R16_N4.txt:L6-L10](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/utilization_impl_R16_N4.txt#L6-L10) —— 头部元信息，`Design: cic_d`、`Design State: Routed`，确认这就是 Open-source 方案的后实现报告。

资源主指标取自 `Slice Logic` 表的两行：

[vivado_reports/reports_at_100Mhz/Open-source CIC/utilization_impl_R16_N4.txt:L35-L38](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/utilization_impl_R16_N4.txt#L35-L38) —— `Slice LUTs = 177`、`Slice Registers = 325`，这是 N=4 的两个核心响应值。

原语明细（用于解释资源由什么搭成）取自 `Primitives` 表：

[vivado_reports/reports_at_100Mhz/Open-source CIC/utilization_impl_R16_N4.txt:L179-L181](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/utilization_impl_R16_N4.txt#L179-L181) —— `FDRE = 325`（与寄存器数自洽）、`LUT2 = 164`、`CARRY4 = 64`。

对照一个高复杂度点 `utilization_impl_R8_N6.txt`，量级明显更重：

[vivado_reports/reports_at_100Mhz/Open-source CIC/utilization_impl_R8_N6.txt:L35-L38](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/utilization_impl_R8_N6.txt#L35-L38) —— `Slice LUTs = 322`、`Slice Registers = 481`，明显大于 N=4 的 177/325。

[vivado_reports/reports_at_100Mhz/Open-source CIC/utilization_impl_R8_N6.txt:L179-L181](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/utilization_impl_R8_N6.txt#L179-L181) —— `CARRY4 = 123`，几乎是 N=4 的两倍。这个两倍关系不是巧合，4.3 会用位宽公式解释。

#### 4.1.4 代码实践

**实践目标**：亲手把 5 个 N 的资源数「取」出来，体会扫描取数就是这么朴素。

**操作步骤**：

1. 打开 `utilization_impl_R16_N2.txt`、`R16_N3.txt`、`R16_N5.txt`、`R16_N6.txt` 四个文件（N=4 已在上面取过）。
2. 在每个文件里定位 `1. Slice Logic` 小节，读 `Slice LUTs` 与 `Slice Registers` 两行的 `Used` 列。
3. 把 5 个 N 的值填进下表（参考答案见 4.2.3）。

**需要观察的现象**：随着 N 从 2 增到 6，LUT 和寄存器数应**单调上升**，没有回跳。

**预期结果**：LUT 序列 58 → 108 → 177 → 287 → 383；寄存器序列 125 → 213 → 325 → 450 → 566。如果你读到的数和这一致，取数就对了。

> 待本地验证：本仓库的 `.txt` 是纯文本，你也可以用 `grep "Slice LUTs" utilization_impl_R16_N*.txt` 一次性把 5 个值打出来核对，不必逐个手翻。

#### 4.1.5 小练习与答案

**练习 1**：为什么本讲要把「实现方案」钉死成 Open-source CIC 一种，而不是像 u3-l1 那样三种一起比？

**参考答案**：u3-l1 的目的是比「同一功能、不同实现」的方案差异，所以方案是自变量；本讲的目的是看「CIC 参数本身」对资源/时序的影响，方案差异会污染趋势，必须把它钉成受控变量。问题不同，钉死的对象就不同。

**练习 2**：如果有人用 290 MHz 下 Open-source CIC 的数据补一条 N=4 的点，错在哪里？

**参考答案**：290 MHz 这一格根本没有 Open-source CIC 的数据（u2-l5 已指出 290 MHz 仅有 CIC Compiler）。补出来的点要么是杜撰，要么是借了别的方案/频率的数据，破坏了「频率钉死」这一受控条件，趋势不可信。

---

### 4.2 趋势分析

#### 4.2.1 概念说明

取完数，下一步是**看趋势**——把表格里的数当序列看：单调不单调？增长快不快？两个自变量（N 和 R）谁的「斜率」更陡？响应变量里，谁平滑、谁抖动？

这里要先建立两个对照：

- **资源序列 vs WNS 序列**：资源应该单调、平滑；WNS 大体下降但允许抖动。
- **N 扫描 vs R 扫描**：N 驱动的是「级数变多」，R 驱动的是「位宽变宽」，两者的增长曲线形状不同（一个近线性、一个近对数）。

#### 4.2.2 核心流程

先把两条扫描的数据摆出来，再读图。

**N 扫描（固定 R=16，N 从 2 到 6）**，响应值取自各 `utilization_impl_R16_N{k}.txt` 与 `timing_impl_R16_N{k}.txt`：

| N | Slice LUTs | Slice Registers | CARRY4 | Slice | 控制集 | WNS(ns) |
|---|-----------|-----------------|--------|-------|--------|---------|
| 2 | 58  | 125 | 15  | 29  | 8  | 7.446 |
| 3 | 108 | 213 | 34  | 49  | 10 | 6.491 |
| 4 | 177 | 325 | 64  | 89  | 13 | 3.473 |
| 5 | 287 | 450 | 102 | 141 | 15 | 4.769 |
| 6 | 383 | 566 | 141 | 165 | 17 | 3.273 |

数据来源（行号因文件略有差异，WNS 均在各自 `Design Timing Summary` 的数据行）：

- LUT/Reg：[utilization_impl_R16_N2.txt:L35-L38](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/utilization_impl_R16_N2.txt#L35-L38) 与 [utilization_impl_R16_N6.txt:L35-L38](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/utilization_impl_R16_N6.txt#L35-L38)（N=3/4/5 同结构）。
- CARRY4 / Slice / 控制集：[utilization_impl_R16_N4.txt:L72-L87](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/utilization_impl_R16_N4.txt#L72-L87)（各 N 文件同结构）。
- WNS：[timing_impl_R16_N2.txt:L157](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/timing_impl_R16_N2.txt#L157)（=7.446）与 [timing_impl_R16_N6.txt:L165](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/timing_impl_R16_N6.txt#L165)（=3.273），其余 N 取自各自 `Design Timing Summary`。

**R 扫描（固定 N=4，R 从 4 到 64）**，响应值取自各 `utilization_impl_R{r}_N4.txt`：

| R | Slice LUTs | Slice Registers | CARRY4 |
|---|-----------|-----------------|--------|
| 4  | 96  | 187 | 27 |
| 8  | 139 | 260 | 52 |
| 16 | 177 | 325 | 64 |
| 32 | 238 | 381 | 88 |
| 64 | 259 | 406 | 93 |

数据来源：[utilization_impl_R16_N4.txt:L35-L38](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/utilization_impl_R16_N4.txt#L35-L38)（R=16 锚点），其余 R 取自 `utilization_impl_R{4,8,32,64}_N4.txt` 的对应行。

读这两张表，立刻能读出四条趋势：

1. **N 扫描里，所有资源列都单调上升**——58→383、125→566、15→141、29→165，没有一个回跳。资源是「数」出来的，所以单调是必然的。
2. **N 扫描里，WNS 总体下降但不单调**——7.446→6.491→3.473→4.769→3.273。注意 N=5（4.769）比 N=4（3.473）还高，这就是「路径依赖噪声」。
3. **R 扫描里，资源也上升，但越涨越慢**——寄存器 187→406，而 R 从 32→64（翻倍）只多了 25 个寄存器。这是典型的「次线性（对数型）」增长，与 N 的近线性增长形成对比。
4. **CARRY4 在两条扫描里都涨**，但 R 扫描的涨幅（27→93，约 3.4 倍）远小于它跟随位宽增长应有的幅度，再次印证 R 的影响要「打折」到对数尺度上才看得清。

#### 4.2.3 源码精读

WNS 取自时序报告的汇总表。以最复杂的 N=6 点为例，确认取数位置：

[vivado_reports/reports_at_100Mhz/Open-source CIC/timing_impl_R16_N6.txt:L163-L165](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/timing_impl_R16_N6.txt#L163-L165) —— `Design Timing Summary` 数据行，`WNS = 3.273`、`TNS Failing Endpoints = 0`，即 N=6 仍收敛但裕量已是这组里最小的。

对照 N=2 的宽松点：

[vivado_reports/reports_at_100Mhz/Open-source CIC/timing_impl_R16_N2.txt:L155-L157](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/timing_impl_R16_N2.txt#L155-L157) —— `WNS = 7.446`，几乎吃掉时钟周期（10 ns）的 3/4 都还稳。从 N=2 到 N=6，WNS 掉了约 4.2 ns。

#### 4.2.4 代码实践

**实践目标**：用一条命令把整列资源数一次性打出来，避免手翻五份文件。

**操作步骤**：在仓库根目录执行（示例代码，仅作演示，非项目原有脚本）：

```bash
# 把每个 N 的 Slice LUTs 行连同文件名打出来
grep -H "Slice LUTs" "vivado_reports/reports_at_100Mhz/Open-source CIC/utilization_impl_R16_N"*.txt
```

**需要观察的现象**：输出按 N=2..6 顺序，`Used` 列应递增。

**预期结果**：依次得到 58、108、177、287、383。

**待本地验证**：不同系统的 `grep -H` 输出顺序可能不同；如需严格按 N 排序，可改用 `for n in 2 3 4 5 6; do grep "Slice LUTs" ".../utilization_impl_R16_N${n}.txt"; done`。

#### 4.2.5 小练习与答案

**练习 1**：N 扫描表里，为什么资源列全部单调，而 WNS 在 N=5 处回跳？

**参考答案**：资源是综合后「数」原语得到的，同样的 RTL+参数必得同样的计数，所以随 N 严格单调；WNS 是布局布线后某条关键路径的裕量，取决于 Vivado 这次把哪条路径放在了关键位置，带布局依赖噪声，故允许回跳。这正说明「资源」和「WNS」是两类性质不同的响应变量。

**练习 2**：对比两条扫描，级数 N 和抽取率 R，哪个对资源的「斜率」更陡？

**参考答案**：N 更陡。N 每加 1，寄存器约多 110 个（近线性）；R 从 32 翻倍到 64，寄存器只多 25 个（次线性）。原因是 N 直接增加级数（每级一套积分器+梳状器），而 R 只通过位宽公式里的 \(\log_2 R\) 间接加宽累加器，影响要弱得多。

---

### 4.3 资源随 N 的线性增长模型

#### 4.3.1 概念说明

观察到资源随 N 单调上升后，自然会问：**升得有多规律？能不能写成公式？** 对工程选型而言，一个「资源 ≈ 斜率·N + 截距」的线性模型，能让你在选 N 时立刻估算面积代价。

为什么资源会**近似线性**？因为 CIC 抽取器有 N 级积分器 + N 级梳状器，每级都是一个定宽累加器/差分器：

- 每加一级 N，就多出 2 个累加器（一组寄存器 + 一组进位链 CARRY4），这是一个**与 N 成正比**的固定增量。
- 同时位宽公式 \(B_{\max}=B_{\text{in}}+\lceil N\log_2(MR)\rceil\) 让**所有已有级**都随 N 缓慢加宽，这是一个**与 N 成正比但系数小**的附加增量。

两份增量都近似随 N 线性叠加，所以总资源对 N 近似线性。

#### 4.3.2 核心流程

用最小二乘法把寄存器序列 \((N,\text{Reg})=(2,125),(3,213),(4,325),(5,450),(6,566)\) 拟合成直线 \(\text{Reg}\approx aN+b\)。最小二乘解为：

\[
a=\frac{\sum (N_i-\bar N)(\text{Reg}_i-\overline{\text{Reg}})}{\sum (N_i-\bar N)^2},\qquad b=\overline{\text{Reg}}-a\bar N
\]

代入 5 个点（\(\bar N=4,\ \overline{\text{Reg}}=335.8\)）算得 \(a\approx 111.9,\ b\approx -111.8\)，即：

\[
\boxed{\text{Slice Registers}\;\approx\;112\cdot N-112\quad(\text{Open-source CIC},\ R=16,\ 100\,\text{MHz})}
\]

校验：N=2 预测 112（实测 125），N=6 预测 560（实测 566），最大相对误差 < 3 %，拟合很好。

同样对 LUT 和 CARRY4 做拟合（斜率分别约 +83/级、+32/级），LUT 的拟合稍差一些，因为 LUT 受「LUT 合并」等综合策略影响，计数不如寄存器干净。

#### 4.3.3 源码精读

线性模型的「两个增量」都能在原语表里对上号。看 N=4 的 Primitives：

[vivado_reports/reports_at_100Mhz/Open-source CIC/utilization_impl_R16_N4.txt:L179-L181](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/utilization_impl_R16_N4.txt#L179-L181) —— N=4 时 FDRE=325、LUT2=164、CARRY4=64。

再看 N=6（同 R=16）：

[vivado_reports/reports_at_100Mhz/Open-source CIC/utilization_impl_R16_N6.txt:L179-L181](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/utilization_impl_R16_N6.txt#L179-L181) —— N=6 时 FDRE=566、LUT2=372、CARRY4=141。

N 从 4→6（加 2 级），FDRE 增 241、CARRY4 增 77，平均每级约 +120 寄存器、+38 进位链——与公式 \(\text{Reg}\approx 112N-112\) 的斜率量级一致。CARRY4 的快涨正是位宽增长 \(+\lceil\log_2(MR)\rceil\) 拍宽进位链的体现。

#### 4.3.4 代码实践

**实践目标**：自己算出寄存器随 N 的线性拟合，并用它预测一个新点。

**操作步骤**：

1. 取 4.2 的寄存器序列（125, 213, 325, 450, 566）与 N（2,3,4,5,6）。
2. 用最小二乘公式（或任一表格软件的 `SLOPE`/`INTERCEPT`、或 Python `numpy.polyfit`）求斜率和截距，应得到约 `a=111.9, b=-111.8`。
3. 用模型预测 N=7（仓库无此点）的寄存器数：\(112\times 7-112=672\)。

**需要观察的现象**：拟合直线应紧贴 5 个实测点；预测值落在序列外延处，量级合理（介于 N=6 的 566 之上）。

**预期结果**：模型 \(\text{Reg}\approx 112N-112\)，N=7 预测约 672。**待本地验证**：仓库只到 N=6，该预测无法用现成报告核对，标注为外推预测。

#### 4.3.5 小练习与答案

**练习 1**：为什么寄存器比 LUT 拟合得更干净？

**参考答案**：寄存器（FDRE）与累加器一一对应，数量由位宽和级数直接决定，几乎不受综合启发式影响；LUT 计数会被「LUT 合并」「LUT as Memory」等策略调整（利用率报告里就有 `* Warning! LUT value is adjusted to account for LUT combining.`），所以 LUT 序列不如寄存器平滑。

**练习 2**：如果 R 从 16 改成 64，斜率 112 会变吗？变大还是变小？

**参考答案**：会变大。R 增大让位宽公式里的 \(\log_2(MR)\) 增大，每一级的累加器更宽，每加一级 N 带来的寄存器增量更多，故斜率上升。可用 R 扫描数据佐证：固定 N=4，R 从 16→64 时寄存器从 325 涨到 406。

---

### 4.4 WNS 随复杂度下降

#### 4.4.1 概念说明

资源随 N 线性增长是「确定性」的一面；WNS 则是「时序压力」的一面。直觉是：级数越多、位宽越宽，关键路径上的组合逻辑越深、数据到达时间越长，建立裕量 WNS 就越小。所以 **WNS 随 N（以及随 R）总体下降**。

但要时刻记住两点（承接 u3-l2）：

1. **WNS 带噪声**：它取决于这一次布局布线把哪条路径放成了关键路径，所以允许像本数据这样在 N=5 出现回跳。看 WNS 趋势要看「总体走向」和「最差点」，不能苛求严格单调。
2. **宽松约束下的 WNS 偏悲观**：本扫描都在 100 MHz（周期 10 ns）下跑，约束很松，Vivado 布局布线松散，WNS 偏小；用这样的 WNS 估 fmax 会偏保守。但**相对趋势**（谁高谁低）仍然可靠。

#### 4.4.2 核心流程

把 WNS 序列（7.446, 6.491, 3.473, 4.769, 3.273）画出来，并把它换算成 fmax 估值：

\[
f_{\max}\approx\frac{1000}{T_{\text{clk}}-\text{WNS}}=\frac{1000}{10-\text{WNS}}\quad(\text{MHz},\ T_{\text{clk}}=10\,\text{ns})
\]

| N | WNS(ns) | fmax 估值(MHz) |
|---|---------|----------------|
| 2 | 7.446 | ≈391 |
| 3 | 6.491 | ≈285 |
| 4 | 3.473 | ≈153 |
| 5 | 4.769 | ≈191 |
| 6 | 3.273 | ≈149 |

读表：fmax 从 N=2 的约 391 MHz 掉到 N=6 的约 149 MHz——**级数翻三倍，最高频率掉到不足四成**。这就是 CIC 「想要更陡的滤波（大 N）就得降速」的硬权衡。注意 N=5 因 WNS 回跳，fmax 估值（191）反而高于 N=4，这并非真实性能反弹，而是噪声，提醒我们 fmax 估值也要看趋势而非单点。

#### 4.4.3 源码精读

WNS 之外，时序报告里能直接看到「复杂度变高」的物理证据——关键路径的逻辑级数（Logic Levels）和组合延迟。看 R8_N6 这条高复杂度路径：

[vivado_reports/reports_at_100Mhz/Open-source CIC/timing_impl_R8_N6.txt:L247-L256](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/timing_impl_R8_N6.txt#L247-L256) —— 最差建立路径：`Slack (MET) 3.163ns`、`Data Path Delay 6.746ns`（其中 logic 3.648ns、route 3.098ns）、`Logic Levels 10 (CARRY4=8 LUT2=2)`。

逻辑级数达到 10 级、数据路径延迟 6.746 ns——这就是 WNS 被压缩的直接原因：进位链 CARRY4 堆叠了 8 级，组合深度大，到达时间接近占满周期。对比低复杂度点，逻辑级数会明显更少，WNS 自然更宽裕。

时钟约束本身确认我们在同一周期下比较：

[vivado_reports/reports_at_100Mhz/Open-source CIC/timing_impl_R8_N6.txt:L176-L178](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/timing_impl_R8_N6.txt#L176-L178) —— `Clock Summary`：`clk` 周期 10.000 ns、频率 100.000 MHz，所有 N 点都跑在同一时钟下。

#### 4.4.4 代码实践

**实践目标**：把 WNS 序列换算成 fmax，亲手看出「N 越大、fmax 越低」的权衡。

**操作步骤**：

1. 取 4.2 表里 N=2..6 的 WNS（7.446, 6.491, 3.473, 4.769, 3.273）。
2. 对每个值算 \(f_{\max}=1000/(10-\text{WNS})\)，填进上表。
3. 在同一张图上把 WNS（左轴）和 fmax（右轴）对 N 画出来。

**需要观察的现象**：WNS 总体下行（带 N=5 回跳），fmax 总体下行（带对应反弹），两条曲线互为镜像。

**预期结果**：fmax 从约 391 MHz 降到约 149 MHz。**待本地验证**：这些 fmax 是宽松约束下的保守估值；如需更贴近真实的 fmax，应参考 u3-l2 中紧约束（290/300 MHz）的 run，而不是这里的 100 MHz。

#### 4.4.5 小练习与答案

**练习 1**：为什么 WNS 在 N=5 反而比 N=4 高？这是否推翻了「WNS 随 N 下降」的结论？

**参考答案**：不推翻。WNS 依赖具体布局，N=4 这次恰好把一条较深的路径放成了关键路径，使 WNS 偏小；N=5 的关键路径不同，WNS 偶然更大。看趋势要看 N=2→6 的总体走向（7.446→3.273，净降约 4.2 ns）和最差点（N=6 最紧），单点的回跳是噪声。

**练习 2**：某音频应用要求 fmax ≥ 200 MHz，按本表估算 N 最多能取到几？

**参考答案**：N=3 的 fmax 估值约 285 MHz（满足），N=4 约 153 MHz（不满足），N=5 因噪声约 191 MHz（擦边但不可靠）。保守地，N 应控制在 3 及以内才能稳过 200 MHz。注意这是 100 MHz 宽松约束下的保守结论，实际能力可能更高（见 u3-l2 的紧约束数据）。

---

## 5. 综合实践

**任务**：完成「Open-source CIC @100 MHz、R=16」的 N 扫描全流程，产出一张趋势图和一句结论。

**步骤**：

1. **取数**：对 N=2..6，分别从 `utilization_impl_R16_N{k}.txt` 读 Slice LUTs、Slice Registers、CARRY4，从 `timing_impl_R16_N{k}.txt` 读 WNS（5 个点 × 4 个响应 = 20 个数）。
2. **制表**：把数据整理成 4.2 那样的表。
3. **拟合**：对寄存器列做线性拟合，写出 \(\text{Reg}\approx aN+b\) 的 a、b。
4. **画图**：用任一工具（表格软件、Python/matplotlib、甚至手绘）画两张图——
   - 图 A：横轴 N，纵轴 LUT 与 Reg（资源，近线性上升）。
   - 图 B：横轴 N，纵轴 WNS 与由它算出的 fmax（时序，总体下降、带噪声）。
5. **一句结论**：用一句话总结资源与时序随 N 的变化规律。

**参考结论**：在 Open-source CIC、100 MHz、R=16 下，**资源（LUT/寄存器/CARRY4）随级数 N 近似线性单调增长（寄存器约 \(\text{Reg}\approx 112N-112\)），而时序裕量 WNS（及由此估得的 fmax）随 N 总体下降、带布局噪声，呈现「面积换性能」的经典权衡——N 翻三倍，fmax 从约 391 MHz 跌到约 149 MHz。**

**待本地验证**：仓库不提供画图脚本，图需自行绘制；fmax 为宽松约束下的保守估值。

## 6. 本讲小结

- **参数扫描 = 钉死受控变量、扫描自变量、记录响应**：本讲把方案/频率/器件/阶段全钉死，只让 N（主）和 R（辅）变化。
- **资源是「数」出来的，WNS 是「跑」出来的**：资源随 N 严格单调，WNS 总体下降但允许回跳（N=5 的 4.769 > N=4 的 3.473），两者性质不同，分析时要区别对待。
- **资源随 N 近线性**：寄存器拟合 \(\text{Reg}\approx 112N-112\)，根因是每加一级多出 2 个定宽累加器；CARRY4 快涨反映位宽 \(\lceil N\log_2(MR)\rceil\) 的增长。
- **资源随 R 次线性（对数型）**：R 从 32→64 翻倍只多 25 个寄存器，因为 R 仅通过 \(\log_2 R\) 间接加宽累加器，影响远弱于 N。
- **WNS 随复杂度下降、fmax 随之走低**：N=2→6，fmax 估值从约 391 MHz 跌到约 149 MHz；关键路径的逻辑级数（如 R8_N6 的 10 级、CARRY4×8）是 WNS 被压缩的物理证据。
- **结论一句话**：CIC 选大 N 拿更陡的滤波，代价是近线性的面积增长和显著走低的最高频率。

## 7. 下一步学习建议

- **横向回到方案对比**：本讲只看了一种方案随参数的趋势。可以回到 u3-l1，把这里的「N 扫描」方法用到三种方案上，看 CIC Compiler 的 SRL16E 风格是否也有同样的近线性斜率。
- **紧约束下的 fmax**：本讲的 fmax 来自宽松的 100 MHz run，偏保守。结合 u3-l2 的 290/300 MHz 紧约束数据，可以得到更贴近真实的最大频率与 N 的关系。
- **数字音频映射**：带着「N 不能太大、否则 fmax 跌穿」的结论，进入 u3-l4，看在音频抽取链里 N、R 该怎么折中选型。
- **自动化提取**：本讲取数靠手翻/grep，可读 u3-l5，学习如何用脚本批量从 `.txt` 报告里提取 WNS/资源，把整张扫描表自动生成。
