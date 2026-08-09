# FPGA 后实现评估基础

## 1. 本讲目标

在前几讲里我们已经知道：本仓库不是可运行软件，而是为论文《Post-Implementation Evaluation of CIC Filters for Digital Audio Applications on FPGA》存放的 Vivado 报告数据集；报告按「频率 / 实现方案 / 报告类型」分层；而 CIC 滤波器是一种用积分器-梳状级联、无需乘法器的多速率滤波器。

本讲要回答一个更基础的问题：**这些报告到底在评估什么？** 学完后你应当能够：

- 区分 **综合（synthesis, synth）** 与 **实现（implementation, impl）** 两个阶段，并说出 `timing_impl_*` / `utilization_synth_*` 这类文件名里每个片段的含义。
- 初步建立 **时序（timing）** 与 **利用率（utilization）** 两类评估指标的概念，认识 **WNS、LUT、寄存器、DSP** 等关键词。
- 读懂报告 **头部（header）**，识别目标器件 **xc7a100tcsg324-1**、速度等级 **-1**，以及 **Design State**（设计状态）字段。

> 提示：本讲是「入门」级别，只建立概念。逐节精读时序路径、逐项剖析利用率原语，分别放在后续的 u2-l1、u2-l2。

## 2. 前置知识

- **FPGA 是什么**：一块可以通过代码（HDL）重新「接线」的芯片。你写 Verilog/VHDL 描述电路，工具把它映射到芯片上的真实资源（查找表、寄存器、走线）。
- **时钟（clock）与周期**：同步电路靠时钟节拍推进。时钟周期 \(T\) 与频率 \(f\) 互为倒数：

  \[
  T = \frac{1}{f}
  \]

  例如 100 MHz 的时钟，周期 \(T = 1/(100\times10^{6}) = 10\,\text{ns}\)。本仓库三个频率点 100/290/300 MHz，对应 10 / 3.448 / 3.333 ns。
- **资源（resource）**：FPGA 里固定数量的「建材」，本器件主要有四类——**LUT**（组合逻辑）、**寄存器**（存储状态）、**DSP**（专用乘加单元）、**Block RAM**（片上存储块）。
- **来自前几讲的术语**：CIC 滤波器的积分器（累加）、梳状器（差分）、参数 R（速率比）、N（级数）；文件名 `R16_N4` 表示 R=16、N=4。

## 3. 本讲源码地图

本讲的「源码」就是 Vivado 生成的文本报告本身。重点看两份 100 MHz、CIC Compiler 方案、R16_N4 配置下的实现后报告：

| 文件 | 类型 | 本讲用途 |
| --- | --- | --- |
| `vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt` | 时序（impl） | 认识 timing 报告头部、Design Timing Summary、WNS、时钟 |
| `vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt` | 利用率（impl） | 认识 utilization 报告头部、Slice Logic、DSP、Primitives、Design State |
| `vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_synth_R16_N4.txt` | 利用率（synth） | 与 impl 对比，区分两个阶段 |
| `vivado_reports/reports_at_100Mhz/CIC Compiler/timing_synth_R16_N4.txt` | 时序（synth） | 与 impl 对比，区分两个阶段 |

> 后两个文件不在本讲规格的 `source_files` 里，但它们与 impl 版同名同目录，是理解 synth/impl 区别的最佳对照。

## 4. 核心概念与源码讲解

### 4.1 综合与实现：synth 与 impl 两个阶段

#### 4.1.1 概念说明

把一段 HDL 描述变成可以烧进芯片的电路，Vivado 会依次执行两大阶段：

- **综合（synthesis）**：把 Verilog/VHDL 翻译成由基本逻辑单元（LUT、进位链、触发器等）组成的**门级网表**。此时电路的「逻辑」确定了，但还**没有放到芯片的具体位置**，因此不含真实布线延迟。
- **实现（implementation）**：在综合后的网表上做优化 → 布局（placement，把每个单元摆到芯片具体位置）→ 布线（routing，用真实金属走线连通）→ 生成可下载的网表。

因为 impl 阶段已经完成布线，**impl 报告里的时序数字带真实连线延迟，比 synth 报告更接近芯片真实表现**。本论文标题里的「Post-Implementation Evaluation」（后实现评估）正是指：所有结论都基于 impl（布线之后）的报告。

#### 4.1.2 核心流程

```
RTL (Verilog/VHDL)
      │  综合 synth
      ▼
  门级网表  ───────────► 生成 *_synth_* 报告 (Design State: Synthesized)
      │  实现 impl
      │   ├─ opt   逻辑优化
      │   ├─ place 布局
      │   └─ route 布线
      ▼
  布线后网表 ─────────► 生成 *_impl_* 报告 (Design State: Routed)
      │
      ▼
  bitstream (本仓库不含)
```

判断一份报告属于哪个阶段有两个抓手：**文件名里的 `synth` / `impl`**，以及**头部的 `Design State` 字段**（下一节会看到，它只出现在 utilization 报告里）。

#### 4.1.3 源码精读

先看同一份设计（CIC Compiler、R16_N4、100 MHz）综合阶段与实现阶段的 utilization 报告头部：

**综合阶段**——头部第 10 行写明 `Design State : Synthesized`：

[vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_synth_R16_N4.txt:L1-L11](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_synth_R16_N4.txt#L1-L11) —— 综合后利用率报告头部，`Design State` 为 `Synthesized`。

**实现阶段**——同一字段变成了 `Routed`：

[vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt:L1-L11](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L1-L11) —— 实现后利用率报告头部，`Design State` 为 `Routed`，表示已完成布线。

再看时间戳（头部 `Date` 行）：synth 报告生成于 `10:14:44`，impl 报告生成于 `10:14:51`，impl 晚了约 7 秒。这与「先综合、后实现」的顺序一致——**时间戳本身就是 synth→impl 流程顺序的证据**。

#### 4.1.4 代码实践

1. **目标**：亲手验证 synth 与 impl 是两个独立阶段。
2. **步骤**：在 `vivado_reports/reports_at_100Mhz/CIC Compiler/` 下，找到 `utilization_synth_R16_N4.txt` 与 `utilization_impl_R16_N4.txt`，分别打开各自头部第 6 行（`Command`）和第 10 行（`Design State`）。
3. **观察**：两份的 `Command` 都以 `report_utilization` 开头，但 `Design State` 一为 `Synthesized`、一为 `Routed`；`Date` 行 impl 更晚。
4. **预期结果**：你能在不读正文的情况下，仅凭头部就判定哪份是综合后、哪份是实现后。
5. 运行结果：待本地验证（仓库为纯文本数据集，无需编译，用任意文本编辑器即可查看）。

#### 4.1.5 小练习与答案

**练习 1**：`timing_impl_R16_N4.txt` 中的 `impl` 说明它是哪个阶段的报告？
**答案**：实现（implementation，布线）之后。所以它的时序数字含真实布线延迟，是论文做评估的依据。

**练习 2**：为什么论文不只用 synth 报告，而强调「Post-Implementation」？
**答案**：synth 阶段尚未布线，时序偏乐观；只有 impl（Routed）后才反映芯片真实可达的性能，结论才可信。

---

### 4.2 时序约束与 timing 报告：WNS 等指标入门

#### 4.2.1 概念说明

**时序（timing）** 关心一件事：在一个时钟周期内，数据能不能从源头寄存器稳定地传到目标寄存器。衡量它的核心量是 **Slack（裕量）**：

\[
\text{Slack} = \text{Required time} - \text{Arrival time}
\]

- Slack > 0：**MET**（满足），数据比要求早到，还有富余；
- Slack < 0：**VIOLATED**（违例），数据来不及，电路可能工作出错。

**WNS（Worst Negative Slack）** 就是全部路径里**最差的那条建立时间（setup）裕量**。本器件 100 MHz（周期 10 ns）下，若 WNS 为正，说明还能再提速；若接近 0，说明逼近极限。注意：WNS 的名字里虽带「Negative」，但当所有路径都满足时它仍是一个**正数**——「最差」不等于「为负」。

#### 4.2.2 核心流程

每个时钟周期的时序检查可简化为：

1. 时钟上升沿，源寄存器放出数据（Arrival 与时钟有关）。
2. 数据经过组合逻辑和连线（Data Path Delay）抵达目标寄存器。
3. 必须在**下一个时钟沿之前**、并满足目标寄存器建立时间，才算成功。
4. Slack = 下一个沿允许的最晚到达时间（Required） − 数据实际到达时间（Arrival）。

报告会同时在 **Slow（慢工艺角）** 和 **Fast（快工艺角）** 两个工艺条件下分析，取最保守（最差）结果。Slow 角代表芯片在高温低电压等不利情况下的表现，因此 setup 违例几乎总在 Slow 角出现。

#### 4.2.3 源码精读

**Design Timing Summary**——全设计的时序总览，第 168–170 行给出关键指标：

[vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:L164-L173](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L164-L173) —— 时序总览：`WNS(ns)` 为 `6.274`、`TNS Failing Endpoints` 为 `0`（共 386 个终点无一违例），并给出结论 `All user specified timing constraints are met.`（所有时序约束均已满足）。

**Clock Summary**——说明约束的时钟及其周期：

[vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:L177-L183](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L177-L183) —— 时钟 `aclk` 波形 `{0 5}`、周期 `10.000 ns`、频率 `100.000 MHz`，印证本目录是 100 MHz 频率点。

**最差路径明细**——逐条列出最慢路径：

[vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:L252-L265](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L252-L265) —— 第 1 条最差路径：`Slack (MET) : 6.274ns`；`Requirement: 10.000ns`（时钟周期）；`Data Path Delay: 3.726ns`（其中逻辑 51.8%、布线 48.2%）；`Logic Levels: 7`（数据穿过 7 级逻辑）。

> **承接 u1-l3（CIC 原理）**：第 253 行的路径源点名是 `.../decimator.decimation_filter/comb/.../int_comb_stage/...`，这里的 `decimator`（抽取器）、`comb`（梳状）、`int_comb_stage`（积分-梳状级）正是上一讲讲的 CIC 结构在网表里的真实命名——**报告正文把 CIC 的原理「写」在了层级路径里**。

#### 4.2.4 代码实践

1. **目标**：学会快速判断一份 impl 时序报告是否满足约束。
2. **步骤**：打开 `timing_impl_R16_N4.txt`，跳到 `Design Timing Summary`（约第 164 行）。
3. **观察**：读取 `WNS(ns)` 的数值与正负号，以及第 173 行那句话。
4. **预期结果**：WNS = `6.274`（正），且看到 `All user specified timing constraints are met.`，说明 100 MHz 下满足时序、还有约 6.3 ns 富余。
5. 运行结果：待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：若把某份报告的 WNS 从 `+6.274` 变成 `-0.300`，意味着什么？
**答案**：最差路径违例 0.3 ns，时序不满足，电路在该时钟下可能出错，需要降频或优化设计。

**练习 2**：`Requirement: 10.000ns` 这个数字从哪里来？
**答案**：来自 `Clock Summary` 里 `aclk` 的周期 10 ns（即 100 MHz）。时钟周期就是数据传输的「时间预算」。

---

### 4.3 利用率与 utilization 报告：LUT/寄存器/DSP 入门

#### 4.3.1 概念说明

**利用率（utilization）** 关心另一件事：设计**占了多少芯片资源**。报告把资源按类别统计，并给出利用率百分比：

\[
\text{Util\%} = \frac{\text{Used}}{\text{Available}} \times 100\%
\]

本器件涉及的关键资源类别：

- **Slice LUTs（查找表）**：实现组合逻辑（与、或、加法等）的基本单元。
- **Slice Registers（寄存器/触发器）**：存储时钟状态，对应流水线与累加器等。
- **DSP（数字信号处理单元，Artix-7 中为 DSP48E1）**：专用乘加运算，速度快、面积省。
- **Block RAM**：片上存储块，做大容量数据缓存。

> **承接 u1-l3**：CIC 滤波器只用加减法和寄存器、**不用乘法器**。下面的报告会印证这一点——DSP 占用为 0。

#### 4.3.2 核心流程

利用率报告结构清晰，分章节统计不同资源类别：

```
1. Slice Logic        → LUT、寄存器
2. Slice Logic Dist.  → LUT/寄存器的分布细节
3. Memory             → Block RAM
4. DSP                → DSP 单元
6. Clocking           → 时钟资源（如 BUFG）
8. Primitives         → 原语清单（FDRE/LUT3/CARRY4/SRL16E …）
```

每一节都是一张 `Used / Fixed / Prohibited / Available / Util%` 五列表，读「Used」和「Util%」两列即可。

#### 4.3.3 源码精读

**Slice Logic**——核心资源占用：

[vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt:L29-L45](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L29-L45) —— `Slice LUTs` 用了 `155`（可用 63400，Util% 0.24）；`Slice Registers` 用了 `261`（可用 126800，Util% 0.21）；另有 25 个 LUT 被用作移位寄存器（Shift Register）。

**Memory 与 DSP**——验证 CIC 不用乘法器、不用大块存储：

[vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt:L97-L117](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L97-L117) —— `Block RAM Tile` 用 `0`；`DSPs` 用 `0`（可用 240）。这正符合 CIC「无乘法器」的特性。

**Primitives**——把资源细到具体原语：

[vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt:L181-L196](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L181-L196) —— 用量靠前的原语：`FDRE`（同步复位触发器）260 个、`LUT3` 96 个、`LUT2` 74 个、`SRL16E`（移位寄存器）41 个、`CARRY4`（进位链）25 个。

> 小词表：**FDRE** = 带同步复位的 D 触发器；**SRL16E** = 用一个 LUT 实现 16 拍移位寄存器（这正是 CIC 梳状器差分延迟的常见实现）；**CARRY4** = 4 位快速进位链，做加法器用。

#### 4.3.4 代码实践

1. **目标**：从一份 utilization 报告提取四类核心资源。
2. **步骤**：打开 `utilization_impl_R16_N4.txt`，分别在 `1. Slice Logic`、`3. Memory`、`4. DSP` 三节读 `Used` 列。
3. **观察**：记录 Slice LUTs、Slice Registers、Block RAM Tile、DSPs 四个数值及其 `Available`。
4. **预期结果**：LUT=155、Reg=261、BRAM=0、DSP=0；占用率均远低于 1%。
5. 运行结果：待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：报告里 `LUT as Shift Register = 25`，而 `Slice LUTs` 总用量也是 155，二者什么关系？
**答案**：25 个 LUT 被配置成移位寄存器（SRL）使用，是 155 个 LUT 总量中的一个子类（含在 155 之内）。

**练习 2**：为什么 CIC Compiler 这个设计 DSP=0？
**答案**：CIC 只做加、减、延迟，不含乘法，因此不需要专用乘加单元 DSP；加减法由 LUT + CARRY4 完成。

---

### 4.4 目标器件 xc7a100tcsg324-1 与 Design State

#### 4.4.1 概念说明

所有资源「Available」列的数字，都由**目标器件**决定。本实验的器件名是 **xc7a100tcsg324-1**，可拆解为四段：

| 片段 | 含义 |
| --- | --- |
| `xc7a` | Xilinx 7 系列、Artix（Artix-7）家族 |
| `100t` | 约 10 万逻辑单元的密度等级 |
| `csg324` | 封装类型（CSG），324 个引脚/焊球 |
| `-1` | **速度等级（speed grade）** |

**速度等级**对 7 系列（Artix/Kintex）而言有 -1、-2、-3 等档（数字越大越快、也越贵）。本器件 `-1` 是**最慢**的一档，意味着芯片本身的延迟最大、对高频时序最不利——论文若在 -1 上仍能跑到某频率，换 -2/-3 会更宽裕。

#### 4.4.2 核心流程

- 器件型号 → 决定 `Available` 资源池（本器件：LUT 63400、Reg 126800、DSP 240、Block RAM Tile 135，均可从报告核对）。
- 速度等级 + 速度文件（Speed File）→ 决定时序计算用的延迟模型。
- **Design State（设计状态）** → 标明报告是在 Vivado 流程的哪一步生成的：
  - `Synthesized`：刚做完综合，未布线；
  - `Routed`：已布线，即「实现后」。

#### 4.4.3 源码精读

utilization 报告头部信息最全，**含 9 个字段**：

[vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt:L1-L11](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L1-L11) —— utilization 报告头部：`Design : cic_compiler_0`、`Device : xc7a100tcsg324-1`、`Speed File : -1`、`Design State : Routed`。

timing 报告头部则只有 **8 个字段，没有 `Design State` 行**，而且器件写法略不同：

[vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:L1-L10](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L1-L10) —— timing 报告头部：`Device : 7a100t-csg324`、`Speed File : -1  PRODUCTION 1.23 2018-06-13`，**没有 `Design State` 字段**。

这是两份报告的真实差异，值得记住：

| 字段 | utilization 报告 | timing 报告 |
| --- | --- | --- |
| 器件写法 | `xc7a100tcsg324-1`（完整型号） | `7a100t-csg324`（同一器件的另一种写法） |
| Speed File | `-1` | `-1  PRODUCTION 1.23 2018-06-13`（含速度文件版本与日期） |
| Design State | **有**（`Routed` 或 `Synthesized`） | **无此字段** |

> **小提示**：`7a100t-csg324` 与 `xc7a100tcsg324-1` 指的是同一颗芯片，只是命名风格不同；速度等级 `-1` 在两边都出现。

#### 4.4.4 代码实践（对应本讲主实践任务）

1. **目标**：从一份 timing 与一份 utilization 报告头部，提取元信息并做成对比表。
2. **步骤**：分别打开 `timing_impl_R16_N4.txt` 和 `utilization_impl_R16_N4.txt` 的前 11 行，按下表填空。
3. **观察**：注意哪些字段两边一致、哪些 timing 报告缺失或写法不同。
4. **预期结果**（参考答案）：

   | 字段 | timing_impl_R16_N4.txt | utilization_impl_R16_N4.txt |
   | --- | --- | --- |
   | Tool Version | Vivado v.2022.2 (win64) | Vivado v.2022.2 (win64) |
   | Date | Thu Jul 17 10:14:51 2025 | Thu Jul 17 10:14:51 2025 |
   | Design | cic_compiler_0 | cic_compiler_0 |
   | Device | 7a100t-csg324 | xc7a100tcsg324-1 |
   | Speed File | -1 PRODUCTION 1.23 2018-06-13 | -1 |
   | Design State | （无此字段） | Routed |

5. 运行结果：待本地验证（直接对照文本即可）。

#### 4.4.5 小练习与答案

**练习 1**：如果只给你 `timing_impl_R16_N4.txt`，你能否确定它是「实现后」而非「综合后」的报告？
**答案**：能。靠**文件名中的 `impl`**（timing 报告头部无 Design State 字段可借力，所以文件名是主要判据）。

**练习 2**：器件名里的 `-1` 表示什么？换成 `-3` 时序会更容易满足吗？
**答案**：`-1` 是速度等级（最慢档）。`-3` 更快、延迟更小，相同频率下时序裕量更大、更容易满足。

## 5. 综合实践

把本讲四个模块串起来，完成一份「报告指纹卡」。任选 100 MHz、CIC Compiler 方案下的 `R16_N4`，对 **synth 与 impl 各一份 utilization 报告**完成下表，并用一句话给该设计下结论：

| 项目 | utilization_synth_R16_N4 | utilization_impl_R16_N4 |
| --- | --- | --- |
| 生成时间（Date） | 10:14:44 | 10:14:51 |
| Design State | Synthesized | Routed |
| Slice LUTs（Used） | （自填） | 155 |
| Slice Registers（Used） | （自填） | 261 |
| DSP（Used） | （自填） | 0 |
| Block RAM Tile（Used） | （自填） | 0 |

**进阶思考**：

1. synth 与 impl 的资源用量通常很接近还是差很多？为什么？（提示：布局布线主要影响时序和资源分布，逻辑总量基本不变。）
2. 结合 4.2 的 `WNS = 6.274ns` 与器件速度等级 `-1`，用一句话总结：这个 CIC 设计在 100 MHz、Artix-7 -1 上表现如何？
3. 打开同目录任意一份 `timing_synth_*` 报告的 Design Timing Summary，比较它的 WNS 与 `timing_impl_*` 的 WNS，验证「synth 时序更乐观」这一说法。

> 参考：本实践只需文本编辑器即可完成，无需安装 Vivado。若要复现整套报告生成流程，见 u3-l6。

## 6. 本讲小结

- Vivado 流程分 **综合（synth）** 与 **实现（impl，含布局布线）** 两阶段；impl 报告带真实布线延迟，是论文「后实现评估」的依据。
- 时序报告用 **Slack / WNS** 衡量「能否赶上时钟」：WNS 为正即满足。本设计 100 MHz 下 WNS = +6.274 ns，`All user specified timing constraints are met.`
- 利用率报告用 **LUT / 寄存器 / DSP / Block RAM** 衡量「占了多少资源」，并以 `Util% = Used/Available` 表示。
- 目标器件 **xc7a100tcsg324-1** = Artix-7、约 10 万逻辑单元、CSG324 封装、速度等级 **-1**（最慢档）。
- **Design State**：utilization 报告里有此字段（`Synthesized` 或 `Routed`），timing 报告头部没有；文件名里的 `synth`/`impl` 是判别阶段的可靠依据。
- 报告正文里 `int_comb_stage`、`decimator` 等路径名，把上一讲的 CIC 原理「刻」进了真实网表。

## 7. 下一步学习建议

- 想逐节读懂时序报告的每一条路径、WHS/TNS、Slow/Fast 双角？进入 **u2-l1 读懂时序报告（timing）**。
- 想逐项剖析 utilization 的 Slice Logic Distribution、Primitives 原语？进入 **u2-l2 读懂利用率报告（utilization）**。
- 想理解 `.txt` 与 `.rpx` 两种报告格式、报告头部每条元信息的来由？进入 **u2-l3 报告元信息与文件格式**。
- 在那之前，建议先用本讲的「报告指纹卡」方法，把 290 MHz、300 MHz 任一报告也扫一遍头部，养成「先读头部、再读正文」的习惯。
