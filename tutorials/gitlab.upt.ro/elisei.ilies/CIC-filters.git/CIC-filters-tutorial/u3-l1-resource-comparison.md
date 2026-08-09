# 资源利用率横向对比分析

## 1. 本讲目标

本讲是专家层第一篇，把第二单元学到的「读懂单份 utilization 报告」升级为「跨方案横向对比」。学完后你应当能够：

1. 用**控制变量法**在相同条件（同频率、同 R、同 N、同器件）下横向对比三方案的资源占用。
2. 解释三方案 **DSP 全为 0** 的共同根因——CIC 算法只含加减法、无乘法器。
3. 解释 CIC Compiler 为何独家用 **SRL16E 移位寄存器 LUT**，从而成为资源最省的方案。
4. 理解**控制集（Control Sets）**如何决定寄存器打包效率，进而影响最终占用 Slice 数与可综合性。
5. 独立完成一次「固定 R、扫描 N」的资源趋势采集，并据此判断资源最省的方案。

## 2. 前置知识

本讲默认你已掌握下列内容（来自前置讲义）：

- **CIC 滤波器原理**（u1-l3）：积分器 + 梳状器级联，**只做加减法、只用寄存器，不用乘法器**。这是后续解释「DSP=0」的理论根据。
- **利用率报告读法**（u2-l2）：报告按 Slice Logic / Memory / DSP / Primitives 分节，每节是 `Used / Fixed / Prohibited / Available / Util%` 六列表，且 \(\text{Util\%} = \text{Used} \div \text{Available} \times 100\%\)。
- **三种实现方案**（u2-l4）：通过报告头部 `Design` 字段区分——
  - Xilinx CIC Compiler IP：`cic_compiler_0`（Design 名固定）
  - MATLAB HDL Coder：`CIC_R16_N4`（Design 名随参数变化）
  - 手写 RTL Open-source：`cic_d`（Design 名固定）
- **实验矩阵与缺口**（u2-l5）：100 MHz 是**唯一三方案齐全**的频率点，因此所有横向对比都必须以 100 MHz 为基准；CIC Compiler 受 IP 限制**整段缺 R4 与 N2**，矩阵最稀疏。

本讲把对比条件**固定为**：频率 100 MHz、抽取率 R=16、级数 N=4、器件 `xc7a100tcsg324-1`。这是 u2-l4 已建立的标准基准点，保证三者真正「同台竞技」。

## 3. 本讲源码地图

本讲的三份「源码」都是后实现（impl）利用率报告，由 `report_utilization` 命令生成：

| 文件 | Design 名 | 作用 |
|------|-----------|------|
| [vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt) | `cic_compiler_0` | 厂商 IP 方案，本讲的「省资源」标杆 |
| [vivado_reports/reports_at_100Mhz/MATLAB HDL Coder/utilization_impl_R16_N4.txt](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/MATLAB%20HDL%20Coder/utilization_impl_R16_N4.txt) | `CIC_R16_N4` | 代码生成方案，风格统一（FDCE + LUT2） |
| [vivado_reports/reports_at_100Mhz/Open-source CIC/utilization_impl_R16_N4.txt](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/utilization_impl_R16_N4.txt) | `cic_d` | 手写 RTL 方案，资源最重（控制集翻倍） |

> 提醒：三份文件在仓库里**文件名相同**（都叫 `utilization_impl_R16_N4.txt`），只靠所在目录区分方案。批量解析时务必把目录名（方案）一并记入，否则三份会混淆。

## 4. 核心概念与源码讲解

### 4.1 跨方案资源对比

#### 4.1.1 概念说明

「横向对比」回答的问题是：**同一个滤波器，用三种不同方式实现，谁更省资源？** 但要得到有意义的答案，必须严格**控制变量**——只让「实现方案」这一个因素变化，其余条件全部锁死。否则你无法判断资源差异到底来自方案本身，还是来自 R、N 或时钟频率不同。

本讲的控制变量表：

| 控制变量 | 锁定值 | 来源 |
|----------|--------|------|
| 频率 | 100 MHz | 100 MHz 是唯一三方案齐全的频率点（u2-l5） |
| 抽取率 R | 16 | 文件名 `R16` |
| 级数 N | 4 | 文件名 `N4` |
| 器件 | `xc7a100tcsg324-1` | 报告头部 |
| 阶段 | impl（已布线） | 文件名 `impl` + Design State = Routed |

被比较的自变量只有一个：实现方案（CIC Compiler / HDL Coder / Open-source）。

#### 4.1.2 核心流程

一次合格的横向对比分四步：

1. **同源确认**：先看三份报告头部，确认 `Tool Version`（同为 Vivado 2022.2）、`Device`（同为 xc7a100tcsg324-1）、`Design State`（同为 Routed），排除「工具/器件/阶段不一致」的干扰。
2. **统一口径**：对每份报告都从同一张表（Section 1 Slice Logic）的同一行取 `Slice LUTs`、`Slice Registers`，从 Section 4 取 `DSPs`，从 Section 3 取 `Block RAM Tile`，保证口径一致。
3. **并表对比**：把三方案的数字排进一张表，差额一眼可见。
4. **归因**：对每一处显著差异，回到 Primitives 原语表与 Slice Logic Distribution 找「用什么零件搭出来」，解释差异的成因（见 4.2~4.4）。

关键资源只有四类：**Slice LUTs（组合逻辑）、Slice Registers（寄存器/触发器）、DSP（乘加单元）、Block RAM（块存储）**。前两者决定通用逻辑面积，后两者决定专用资源是否被调用。

#### 4.1.3 源码精读

先确认三份报告同源。三份头部都标注同一器件与同一已布线状态，例如 CIC Compiler 头部：

[报告头部（工具/器件/Design State 同源）](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L3-L10) —— `Design : cic_compiler_0`、`Device : xc7a100tcsg324-1`、`Design State : Routed`，另两份仅 `Design` 名不同（`CIC_R16_N4` / `cic_d`），器件与状态完全一致，对比有效。

三方案的核心资源并表（R=16, N=4, 100 MHz）：

| 指标 | CIC Compiler | MATLAB HDL Coder | Open-source |
|------|:---:|:---:|:---:|
| Slice LUTs | **155** | 169 | 177 |
| └ LUT as Logic | 130 | 169 | 177 |
| └ LUT as Memory（Shift Register） | **25** | 0 | 0 |
| Slice Registers | **261** | 266 | 325 |
| Slice（物理切片） | **61** | 67 | 89 |
| Unique Control Sets | 7 | 7 | **13** |
| DSP | 0 | 0 | 0 |
| Block RAM Tile | 0 | 0 | 0 |
| 主导触发器 | FDRE×260 + FDSE×1 | FDCE×266 | FDRE×325 |
| CARRY4（进位链） | 25 | 40 | **64** |
| SRL16E（移位寄存器 LUT） | **41** | 0 | 0 |

数字取自三份报告的 Slice Logic 节。例如 CIC Compiler 的 LUT/寄存器行：

[Slice Logic：LUT=155 / Registers=261（CIC Compiler）](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L32-L45)

[Slice Logic：LUT=169 / Registers=266（MATLAB HDL Coder）](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/MATLAB%20HDL%20Coder/utilization_impl_R16_N4.txt#L32-L43)

[Slice Logic：LUT=177 / Registers=325（Open-source）](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/utilization_impl_R16_N4.txt#L32-L43)

**一眼结论**：

- **CIC Compiler 资源最省**：LUT 最少（155）、寄存器最少（261）、占用 Slice 最少（61）。
- **Open-source 资源最重**：寄存器最多（325）、Slice 最多（89）、进位链 CARRY4 最多（64）、控制集最多（13）。
- **HDL Coder 居中**：寄存器与 CIC Compiler 接近（266 vs 261），但 LUT 更偏向纯组合逻辑（169 全部是 LUT as Logic）。
- **三方案 DSP 与 Block RAM 全部为 0**——这是最醒目的共同点，下一节专门解释。

> 注意：三方案的绝对占用率都很低（Util% 普遍 < 0.6%），因为 xc7a100tcsg324-1 是中规模器件（63400 LUT）。差异虽小，但**相对比例**与**构成方式**的规律非常清晰，这正是论文做对比的价值所在。

#### 4.1.4 代码实践（源码阅读型）

**目标**：亲手从三份报告提取四类资源并复算 Util%，验证上表数字。

**步骤**：

1. 用下面三条命令分别提取三方案的 `Slice LUTs` 行（注意目录名带空格要加引号）：

```bash
grep -m1 "Slice LUTs " "vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt"
grep -m1 "Slice LUTs " "vivado_reports/reports_at_100Mhz/MATLAB HDL Coder/utilization_impl_R16_N4.txt"
grep -m1 "Slice LUTs " "vivado_reports/reports_at_100Mhz/Open-source CIC/utilization_impl_R16_N4.txt"
```

2. 对每行的 Used 与 Available 复算 Util%，例如 CIC Compiler：\(155 \div 63400 \times 100\% \approx 0.244\%\)，与报告的 `0.24` 吻合。

**需要观察的现象**：三行的 Used 列依次约为 155 / 169 / 177，递增；Available 列都是 63400。

**预期结果**：得到与 4.1.3 表格一致的三个 LUT 数值，确认 CIC Compiler 最省。本实践不依赖任何工具链，纯文本检索即可完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么必须固定到 100 MHz 才能做三方案横向对比？

> **答案**：因为只有 100 MHz 是三方案都齐全的频率点（u2-l5）。290 MHz 只有 CIC Compiler，300 MHz 缺 Open-source。若不固定频率，资源差异会混入「频率/时序压力不同」的干扰，无法归因到方案本身。

**练习 2**：三方案 LUT 差距很小（155 vs 177，差 14），能否就此断定「方案选择无所谓」？

> **答案**：不能。本设计绝对占用率极低，差距在器件容量里几乎可忽略；但相对而言 CIC Compiler 省约 12% 的 LUT。更关键的是**构成方式**不同（CIC Compiler 用 SRL16E、Open-source 控制集翻倍），这些差异在更密集的设计或更小器件上会被放大。结论要看「构成」而非只看绝对差。

---

### 4.2 DSP 使用差异

#### 4.2.1 概念说明

DSP 在 7 系列 FPGA 里指 **DSP48E1** 切片，内部含一个 \(18\times25\) 乘法器加一个累加器，是做乘加运算的专用硬核。一般 FIR 滤波器靠乘法器实现抽头乘系数，会大量占用 DSP。而 CIC 的核心运算只有积分器（累加，加法）和梳状器（差分，减法），**数学上不含任何乘法**——这是 u1-l3 讲过的 CIC 根本特性。因此我们预期：无论用哪种实现方案，DSP 都应当是 0。

#### 4.2.2 核心流程

验证「CIC 无乘法器」的推理链：

1. CIC 抽取器 = N 级积分器（高速） → ↓R 降采样 → N 级梳状器（低速）。
2. 积分器：\(y[n] = y[n-1] + x[n]\)（一次加法）。
3. 梳状器：\(y[n] = x[n] - x[n-MR]\)（一次减法 + 一段延迟寄存器）。
4. 全流程只用到 **加、减、寄存器**，无乘法 → 无需 DSP48 的乘法器 → DSP=0。
5. 由于三种生成路径（IP / 代码生成 / 手写 RTL）实现的是同一个算法，三份报告应同时显示 DSP=0，形成**交叉互证**。

#### 4.2.3 源码精读

三份报告的 Section 4（DSP）都只有一行，且 Used=0：

[DSPs = 0（CIC Compiler）](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L110-L117)

同位置（Section 4）在另外两份报告中同样是 `| DSPs | 0 | ... | 240 | 0.00 |`（HDL Coder 与 Open-source 的 DSP 行）。器件共有 240 个 DSP 可用，三方案一个都没用。

同理，Section 3 的 `Block RAM Tile` 也全为 0（CIC Compiler [Memory 节](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L97-L106)），说明 CIC 的延迟用触发器/移位寄存器实现，不必动用块存储。

**为什么说这是「交叉互证」**：三种来源、风格迥异的实现（厂商优化 IP、MATLAB 自动生成的 HDL、人工手写 RTL）**独立**给出了相同的 DSP=0 结论。这比单一实现的结论可靠得多——它排除了「只是某个实现恰好没用 DSP」的偶然，确认**算法本身**不需要乘法器。

#### 4.2.4 代码实践（源码阅读型）

**目标**：确认三方案 DSP 全为 0，并定量感受「省下了多少专用资源」。

**步骤**：

```bash
grep -H "^| DSPs" "vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt" \
                  "vivado_reports/reports_at_100Mhz/MATLAB HDL Coder/utilization_impl_R16_N4.txt" \
                  "vivado_reports/reports_at_100Mhz/Open-source CIC/utilization_impl_R16_N4.txt"
```

**需要观察的现象**：三行 Used 列全是 0，Available 列全是 240。

**预期结果**：三方案 DSP 占用率均为 0.00%。若换成一个等抽头数的普通 FIR，这里通常会看到几十个 DSP 被占用——可据此体会 CIC 在专用资源上的节省。

#### 4.2.5 小练习与答案

**练习 1**：若某天你看到一份 CIC 报告里 DSP > 0，可能是什么原因？

> **答案**：最可能是**输入/输出接口或后级补偿滤波**混进了乘法（例如增益校正、位宽截断处的舍入乘常数），或实现者错误地在梳状/积分器里引入了乘法。纯 CIC 核心本身不应有乘法，出现 DSP>0 应当核查设计是否被改动。

**练习 2**：为什么 Block RAM 也是 0？

> **答案**：CIC 的延迟（梳状器差分延迟）只需要几十到几百拍，用触发器或 LUT 移位寄存器（SRL16E）就够，远未到需要动用 36 Kb 块存储的规模，故 BRAM=0。

---

### 4.3 SRL16E / 移位寄存器

#### 4.3.1 概念说明

**SRL16E** 是 7 系列的一种原语：把一片 SLICEM 类型的 LUT 配置成**最多 16 拍的移位寄存器**。它用一个 LUT 位实现本该用一串触发器（每个寄存器一拍）才能做到的延迟，于是「**用 1 个 LUT 换掉若干个寄存器**」。这正是 CIC Compiler 区别于另两方案的核心技巧。

梳状器需要一段差分延迟（u1-l3 中的 M，按 noble identity 在低速侧为 M 拍）。这段延迟可用两种零件实现：

- 一串 D 触发器（每个触发器存 1 拍）→ 占用 Slice Registers。
- SRL16E（1 个 LUT 存最多 16 拍）→ 占用 LUT as Memory，**省下触发器**。

CIC Compiler 选了后者，HDL Coder 与 Open-source 选了前者。

#### 4.3.2 核心流程

判断一个方案是否用了 SRL16E，看两个地方是否同时非零：

1. **Primitives 原语表**里 `SRL16E` 行的 Used 是否 > 0。
2. **Slice Logic Distribution** 里 `LUT as Memory → LUT as Shift Register` 是否 > 0。

两者互相对应：原语表数「逻辑实例数」，Slice Logic 数「物理 LUT 占用」。若两者都为 0，说明该方案把延迟全做成了触发器链。

> 关于两个数字的对账：CIC Compiler 报告里 SRL16E 原语实例为 41，而 `LUT as Shift Register` 物理占用为 25。两者不等是因为 Slice Logic 顶部有 `Warning! LUT value is adjusted to account for LUT combining`（[第 46 行警告](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L46)）——Vivado 会把可合并的 LUT 用法折算，所以原语实例数与物理 LUT 数并非简单 1:1。本讲不臆测精确折算公式，只依据「两个数字均非零」得出「CIC Compiler 确实用了 LUT 移位寄存器」这一确定结论。

#### 4.3.3 源码精读

CIC Compiler 的 Primitives 原语表里，`SRL16E` 以 41 个排第四：

[Primitives：SRL16E ×41（仅 CIC Compiler 有）](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L181-L196)

对应的 Slice Logic Distribution 显示 `LUT as Shift Register = 25`（全部 LUT as Memory 都是 Shift Register，没有分布式 RAM）：

[LUT as Memory = 25（全为 Shift Register）](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L71-L93)

而另两份报告在同一位置完全是零：

- HDL Coder：`LUT as Memory = 0`、`LUT as Shift Register = 0`，Primitives 表里**没有 SRL16E 这一行**（[HDL Coder Primitives](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/MATLAB%20HDL%20Coder/utilization_impl_R16_N4.txt#L176-L190)）。
- Open-source：同样 `LUT as Memory = 0`、无 SRL16E（[Open-source Primitives](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/utilization_impl_R16_N4.txt#L176-L190)）。

这正好解释了 4.1.3 表中的反差：CIC Compiler 把 25 个 LUT 当移位寄存器用，**换掉了相当数量的触发器**，所以它的 Slice Registers 最少（261，比 Open-source 的 325 少 64 个）。这是厂商 IP 经过专门面积优化的「特征签名」。

#### 4.3.4 代码实践（源码阅读型）

**目标**：在三份报告中定位 SRL16E，确认它为 CIC Compiler 独有。

**步骤**：

```bash
grep -Hc "SRL16E" "vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt" \
                  "vivado_reports/reports_at_100Mhz/MATLAB HDL Coder/utilization_impl_R16_N4.txt" \
                  "vivado_reports/reports_at_100Mhz/Open-source CIC/utilization_impl_R16_N4.txt"
```

`-c` 统计匹配行数。

**需要观察的现象**：第一行（CIC Compiler）匹配行数 > 0（SRL16E 出现在 Primitives 表），后两行为 0。

**预期结果**：CIC Compiler 该字段 ≥1（实际原语表里 1 行），HDL Coder 与 Open-source 均为 0。若想看具体数量，去掉 `-c` 改用 `grep -H "SRL16E"`，会看到 CIC Compiler 的 `| SRL16E | 41 | Distributed Memory |`。

#### 4.3.5 小练习与答案

**练习 1**：SRL16E 最多存 16 拍。若梳状器需要 32 拍延迟，CIC Compiler 会怎么做？

> **答案**：需要把两片 SRL16E 级联（16+16=32）。延迟越长，占用的 SRL16E（即 LUT as Memory）越多。这也解释了为何 N、M 越大，CIC Compiler 的 LUT as Memory 会上升。

**练习 2**：为什么 HDL Coder 与 Open-source 没有自动用上 SRL16E？

> **答案**：SRL16E 需要综合器识别「一段连续移位」并映射到 SLICEM LUT。HDL Coder 生成的代码与手写 RTL 的写法（如显式逐级寄存器、或综合器未推断）没触发该优化，于是延迟落回了普通触发器链，寄存器数因而偏高。

---

### 4.4 控制集（Control Sets）

#### 4.4.1 概念说明

**控制集（Control Set）** 是 7 系列 FPGA 里一个容易被忽略、却直接决定面积的概念。一个控制集 = 一种唯一的 `{时钟, 时钟使能, 复位/置位}` 组合。规则是：**只有控制集完全相同的寄存器，才能被打包进同一个 Slice**。

推论很直接：

- 控制集**越多** → 寄存器被切分到越多组、越难塞进同一个 Slice → Slice 利用率下降 → **占用 Slice 数上升**。
- 控制集**越少** → 寄存器集中、打包紧密 → Slice 更省。

Vivado 官方建议把全设计的控制集数量压到很低（理想个位数），就是为了提高打包密度。本讲三方案的控制集数量差异，正是 Open-source「面积最重」的深层原因。

#### 4.4.2 核心流程

用控制集解释面积差异的三步：

1. 从 Slice Logic Distribution 取 `Unique Control Sets`。
2. 从同一节取 `Slice`（物理切片占用数）。
3. 把三方案的「控制集 → Slice」成对比较，看控制集多的方案是否 Slice 也明显多。

可用一个粗略的打包密度指标：

\[
\text{每 Slice 寄存器数} \;=\; \frac{\text{Slice Registers}}{\text{Slice}}
\]

该值越高，说明寄存器打包越紧密。

#### 4.4.3 源码精读

三方案的 `Unique Control Sets` 与 `Slice` 数（均在 Slice Logic Distribution 末尾）：

[控制集 = 7、Slice = 61（CIC Compiler）](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L71-L93)

HDL Coder 同节显示 `Unique Control Sets = 7`、`Slice = 67`（[HDL Coder](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/MATLAB%20HDL%20Coder/utilization_impl_R16_N4.txt#L69-L88)）。

[控制集 = 13、Slice = 89（Open-source）](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/Open-source%20CIC/utilization_impl_R16_N4.txt#L69-L88)

并表与打包密度：

| 方案 | Unique Control Sets | Slice Registers | Slice | 每 Slice 寄存器数 |
|------|:---:|:---:|:---:|:---:|
| CIC Compiler | 7 | 261 | 61 | 4.28 |
| MATLAB HDL Coder | 7 | 266 | 67 | 3.97 |
| Open-source | **13** | 325 | **89** | 3.65 |

**关键观察**：Open-source 的控制集几乎翻倍（13 vs 7），而它的寄存器只比 CIC Compiler 多约 25%（325 vs 261），但 Slice 却多了 46%（89 vs 61）。寄存器增量远小于 Slice 增量——多出来的 Slice 不是因为「寄存器多到装不下」，而是因为控制集多导致**打包密度下降**（每 Slice 寄存器数从 4.28 掉到 3.65）。这也呼应了 4.1.3 中 Open-source 的 CARRY4 进位链多达 64、Primitives 全是 FDRE 触发器的「重触发器、重进位」风格：手写 RTL 为各级用了更多独立的使能/复位分组，制造了额外控制集。

> 注意：`Available Control Sets` 在报告里按 `Slice × 1` 估算（见各表下方注释），只是上限提示，不是硬性器件资源，分析时只看 `Unique Control Sets` 的相对差异即可。

#### 4.4.4 代码实践（源码阅读型）

**目标**：提取三方案的控制集与 Slice 数，计算打包密度，验证「控制集多 → Slice 多」。

**步骤**：

```bash
for f in "CIC Compiler" "MATLAB HDL Coder" "Open-source CIC"; do
  echo "== $f =="
  grep -m1 "Slice "    "vivado_reports/reports_at_100Mhz/$f/utilization_impl_R16_N4.txt" | grep -E "^\| Slice "
  grep -m1 "Unique Control Sets" "vivado_reports/reports_at_100Mhz/$f/utilization_impl_R16_N4.txt"
done
```

> 提示：若 shell 因目录名含空格/连字符报错，可逐条手敲 `grep` 命令；上面循环仅为示意。

**需要观察的现象**：Open-source 的 `Unique Control Sets` 数字最大（13），其 `Slice`（取 Slice Logic Distribution 中 `Slice` 行的 Used 列）也最大（89）。

**预期结果**：得到 7/7/13 的控制集与 61/67/89 的 Slice 数；手算每 Slice 寄存器数后，应看到 Open-source 打包密度最低。

#### 4.4.5 小练习与答案

**练习 1**：若要把 Open-source 的 Slice 数降下来，最有效的代码层面手段是什么？

> **答案**：合并控制信号——让所有级共用同一个时钟、同一个时钟使能、同一个（同步）复位，尽量减少独立的使能/复位分组，从而把控制集从 13 压回个位数。控制集一下降，寄存器就能更紧密地打包，Slice 占用随之减少。

**练习 2**：控制集多为什么会降低可综合性/布局质量？

> **答案**：控制集多意味着寄存器按控制信号被分成很多互不可打包的组，每个组都要求各自的 Slice 资源，导致 Slice 利用率下降、布局碎片化，进而可能加剧布线拥塞、压低 fmax。这也是为什么 Vivado 把「控制集最小化」列为推荐编码风格。

---

## 5. 综合实践：固定 R=16 扫描 N，画出「N–资源」曲线

**实践目标**：固定 R=16、频率 100 MHz，收集三方案在 N=2..6 的 utilization_impl 数据，提取 LUT/Reg/DSP，绘制「N–资源」趋势，并指出资源最省的方案。

**操作步骤**：

1. 对三方案、各 N 值，从对应报告提取 `Slice LUTs`、`Slice Registers`、`DSPs`。下面以 Open-source CIC 为例给出检索命令（其余两方案把目录名换掉即可）：

```bash
for n in 2 3 4 5 6; do
  f="vivado_reports/reports_at_100Mhz/Open-source CIC/utilization_impl_R16_N${n}.txt"
  echo "N=$n"
  grep -m1 "Slice LUTs "    "$f"
  grep -m1 "Slice Registers" "$f"
  grep -m1 "^| DSPs"         "$f"
done
```

2. **注意数据缺口**：CIC Compiler 在 100 MHz 下**只有 N=4/5/6**（缺 N2、N3，受 IP 限制，见 u2-l5），所以它的曲线只能从 N=4 起画；HDL Coder 与 Open-source 是 N=2..6 完整五点。
3. 把数据填入下表（本讲已替你从仓库实际报告中提取完毕，可直接核对）：

| N | CIC Compiler LUT/Reg | MATLAB HDL Coder LUT/Reg | Open-source LUT/Reg | (三方案 DSP 均为 0) |
|:--:|:--:|:--:|:--:|:--:|
| 2 | —（无数据） | 56 / 92 | 58 / 125 | 0 |
| 3 | —（无数据） | 104 / 167 | 108 / 213 | 0 |
| 4 | 155 / 261 | 169 / 266 | 177 / 325 | 0 |
| 5 | 208 / 351 | 244 / 377 | 287 / 450 | 0 |
| 6 | 265 / 453 | 319 / 482 | 383 / 566 | 0 |

   （以上每个数字均可在对应文件 `vivado_reports/reports_at_100Mhz/<方案>/utilization_impl_R16_N<n>.txt` 中核对。）

4. 以 N 为横轴、LUT 与寄存器分别为纵轴，把三方案画成两条/三条折线（纸笔或电子表格均可）。

**需要观察的现象**：

- 随 N 增大，三方案的 LUT 与寄存器都**近似线性增长**。以寄存器为例，每增加一级 N，HDL Coder 约增加 90~110 个寄存器，Open-source 约增加 90~125 个——对应每多一级积分器 + 一级梳状器所需的位宽寄存器。
- 在 N=4/5/6 三个共同点上，**CIC Compiler 的 LUT 与寄存器始终最低**；Open-source 始终最高。
- DSP 在全部 13 个数据点上恒为 0，再次印证「CIC 无乘法器」。

**预期结果（结论）**：

- **资源最省的方案 = CIC Compiler**（有数据的 N=4/5/6 上 LUT、寄存器、Slice 三项均最低），这归功于 4.3 的 SRL16E 优化与 4.4 的低控制集（7）。
- **资源最重的方案 = Open-source**（控制集 13、触发器最多、进位链最多）。
- **HDL Coder** 在 N=2/3（CIC Compiler 无数据处）是两方案中更省的一方，整体居中。
- 若只看「能完整覆盖 N=2..6 的方案」，HDL Coder 与 Open-source 都齐全，其中 **HDL Coder 资源更省**。

> 待本地验证：曲线斜率与「最省方案」判断不依赖任何运行环境，纯文本检索即可复现；若你想用脚本批量出图，可参考 u3-l5 的批量解析方法（注意排除 290 MHz 下的 `to_delete/` 重复副本，本 100 MHz 目录已确认无该副本）。

## 6. 本讲小结

- **控制变量法**是横向对比的前提：必须锁定频率（100 MHz）、R、N、器件、阶段，只让方案这一个因素变化，否则差异无法归因。
- 在 R16_N4 @100MHz 下，**CIC Compiler 资源最省**（LUT 155 / Reg 261 / Slice 61），**Open-source 最重**（LUT 177 / Reg 325 / Slice 89），HDL Coder 居中。
- 三方案 **DSP 与 Block RAM 全为 0**，且由三条独立生成路径交叉互证：CIC 算法只含加减法、无乘法器、无需块存储。
- CIC Compiler 独家使用 **SRL16E 移位寄存器 LUT（41 个原语）**，用 LUT as Memory 换掉触发器，是其寄存器最少的直接原因。
- **控制集**决定打包密度：Open-source 控制集翻倍（13 vs 7），导致每 Slice 寄存器数下降、Slice 占用比寄存器增长得更快，这是其面积最重的深层原因。
- 扫描 N 后，资源随 N 近似线性增长；在所有共同数据点上 CIC Compiler 始终最省。

## 7. 下一步学习建议

- **u3-l2 时序收敛与 fmax 分析**：本讲只看了「面积」，下一讲转入「速度」——用 WNS 与时钟周期估算最大频率 fmax，看资源最省的 CIC Compiler 是否也时序最优。
- **u3-l3 R/N 参数扫描趋势分析**：本讲的综合实践已初探 N 扫描，下一讲会系统化地扫描 R 与 N，建立资源/时序对参数的敏感度模型。
- 继续阅读建议：在仓库内对比同方案、不同频率（如 CIC Compiler 的 100/290/300 MHz 三份 timing 报告），体会「同面积、不同时序压力」的差异，为 u3-l2 做准备。
