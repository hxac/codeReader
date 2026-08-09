# 复现实验的方法论

## 1. 本讲目标

前面十三篇讲义都在回答「**报告里写了什么、怎么读、怎么对比**」。本讲是整本手册的收尾，反过来回答一个更上游的问题：

> **「这一仓库里的数百份 Vivado 报告，究竟是怎样被生产出来的？如果我想从零复现其中任意一份，需要走哪些步骤、用到哪些文件？」**

学完本讲，你应当能够：

1. 说清一条 FPGA 设计从「描述」到「后实现报告」的完整流水线：建工程 → 综合 → 实现 → 导出报告。
2. 识别本仓库三种实现方案各自的「生成路径」——IP Catalog、MATLAB HDL Coder、手写 RTL——并知道它们在报告头部留下的可判别痕迹。
3. 准确写出 `report_timing_summary` 与 `report_utilization` 两条导出命令的用途与差异。
4. **诚实地区分**：本仓库里「已经固化、可追溯」的可复现性要素有哪些；而「缺失、需读者自行补齐」的要素又有哪些。

本讲反复强调一个事实：**这是一个「报告数据集」仓库，不是「工程源码」仓库**。所以「复现」二字在本讲的含义不是 `git clone && make`，而是「按报告头部留下的线索，自己把缺失的上游环节补齐，再跑一遍」。

## 2. 前置知识

本讲依赖你已经掌握以下两篇讲义的结论（若有遗忘请先回顾）：

- **u2-l3 报告元信息与文件格式**：每份报告头部都有 Tool Version / Date / Host / Command / Design / Device / Speed File 等元信息，其中 `Command` 字段原样回显了生成该报告的 Tcl 命令；`.txt` 是纯文本可解析，`.rpx` 是 Vivado GUI 交互格式。本讲大量引用头部 `Command` 字段作为「复现命令」的直接证据。
- **u3-l2 时序收敛与最大频率（fmax）分析**：WNS（最差建立裕量）≥0 即代表时序收敛；复现实验的「成功判据」就是重新跑出的 WNS 与本仓库记录的 WNS 一致或接近。

此外需要一点 Vivado 的常识（不熟悉的术语本讲会顺带解释）：

- **综合（synthesis）**：把 HDL 代码翻译成由基本逻辑单元（LUT、触发器、进位链等）组成的网表，但**尚未**在芯片上布局布线。
- **实现（implementation）**：在网表上依次做优化、布局（placement）、布线（routing），得到带真实互连延迟的「已布线设计」（routed design）。本论文标题里的 **Post-Implementation** 正是指这一阶段之后。
- **约束文件（.xdc）**：告诉工具时钟频率、引脚分配等限制的文本文件。时钟频率由 `create_clock -period <周期>` 决定，周期单位为纳秒（ns）。

> 提醒：u1-l4 已经讲过 synth 与 impl 的区别，本讲不再重复概念辨析，而是聚焦「**如何把这条流程跑出来并产出和仓库一致的报告**」。

## 3. 本讲源码地图

本讲引用的「源码」其实就是报告本身——仓库里没有真正的 HDL 源码。我们靠阅读报告头部来反推生产过程。

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/README.md) | 一句话声明：本仓库是 Vivado 生成的 utilization 与 timing 报告，服务于指定论文 |
| `vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt` | CIC Compiler 方案的时序报告；头部给出 `report_timing_summary` 命令、Design 名 `cic_compiler_0`、器件与工具版本 |
| `vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt` | 同方案的利用率报告；头部给出 `report_utilization` 命令、`Design State : Routed` |
| `vivado_reports/reports_at_100Mhz/MATLAB HDL Coder/timing_impl_R16_N4.txt` | HDL Coder 方案时序报告；头部 Design 名为 `CIC_R16_N4`，是三方案中唯一随参数变化的命名 |
| `vivado_reports/reports_at_100Mhz/Open-source CIC/timing_impl_R16_N4.txt` | 手写 RTL 方案时序报告；头部 Design 名为 `cic_d` |

> 关键事实（已用文件检索确认）：本仓库**不含**任何 `.v` / `.vhd` / `.tcl` / `.xdc` / `.xpr` / `.xci` 文件。也就是说，HDL 源码、Tcl 构建脚本、约束文件、Vivado 工程与 IP 定制文件**全部缺失**。后文凡涉及这些文件的具体内容，均标注「待确认 / 需自行准备」。

## 4. 核心概念与源码讲解

### 4.1 Vivado 综合与实现流程（synth → impl）

#### 4.1.1 概念说明

要复现一份「后实现报告」，先要理解一份设计在 Vivado 里走过的生命周期。本模块不讲 synth/impl 的定义（u1-l4 已讲），而讲**流水线的阶段顺序**以及**每个阶段的产物如何对应到仓库里的文件名**。

仓库里每个报告文件名都带 `_synth_` 或 `_impl_`（见 u1-l2 的命名规则）。这不仅是分类标签，而是真实反映了「这条命令是在哪个阶段的_design_上运行的」。我们要复现，就得把设计推进到对应阶段，再调用报告命令。

#### 4.1.2 核心流程

复现一份 impl 报告的流水线如下（伪流程）：

```
① 准备设计      产生 HDL 源码 / IP（三种路径之一，见 4.2~4.4）
        │
② 建工程        create_project；add_files（源码）；add_files（约束 .xdc）
        │         ── .xdc 里用 create_clock -period <T> 锁定目标频率
        │
③ 综合 synth    launch_runs synth_1          → 产出综合后网表
        │         （对应文件名里的 _synth_）
        │
④ 实现 impl     launch_runs impl_1
        │           ├ opt_design     逻辑优化
        │           ├ place_design   布局
        │           └ route_design   布线  → 产出 routed design
        │         （对应文件名里的 _impl_）
        │
⑤ 导出报告      report_timing_summary  → timing_impl_*.txt (+ .rpx)
                 report_utilization    → utilization_impl_*.txt
```

目标频率到时钟周期的换算（u2-l5 已建立）：

\[
T_{\text{clk}}(\text{ns}) = \frac{1000}{f(\text{MHz})}
\]

三档频率对应的标称周期：

| 频率 | 标称周期 |
| --- | --- |
| 100 MHz | 10.000 ns |
| 290 MHz | 3.448 ns |
| 300 MHz | 3.333 ns |

> 注意（来自 u2-l5）：报告内显示的真实频率是 290.023 / 300.030 等非整数，说明实际写入 `.xdc` 的 `create_clock -period` 并非干净的 3.448/3.333 ns。**该约束文件不在仓库内，精确周期值待确认**；复现时建议先按标称值跑，再按报告内的实际频率微调。

#### 4.1.3 源码精读

判断一份报告属于哪个阶段，最可靠的证据有两条：**文件名里的 `synth`/`impl`**，以及 **utilization 报告头部的 `Design State` 字段**。

利用率报告头部明确写着 `Design State : Routed`，这正是「实现阶段已完成、设计已布线」的权威信号：

[vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt:6-10](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L6-L10) —— 头部 `Command` 给出 `report_utilization`，`Design` 为 `cic_compiler_0`，`Device` 为 `xc7a100tcsg324-1`，末行 `Design State : Routed` 证明该报告跑在已布线设计上（即 impl 阶段产物）。

而时序报告头部**没有** `Design State` 字段（u1-l4、u2-l3 已指出），其阶段只能靠文件名 `_impl_` 判定，但可以靠 WNS 的取值佐证：impl 报告含真实布线延迟，WNS 通常比 synth 报告小。看 CIC Compiler impl 时序报告的 Design Timing Summary：

[vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:168-173](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L168-L173) —— `WNS=6.274 ns`、`TNS=0.000`、`Failing Endpoints=0`，并以 `All user specified timing constraints are met.` 收尾，说明该 impl 设计在 100 MHz 下时序收敛。

复现成功的判据就是：你重新跑出的 impl 报告，其头部 `Device/Design State` 与此处一致，且 WNS 接近 6.274 ns（布局布线有随机性，不必完全相等）。

#### 4.1.4 代码实践

**实践目标**：用「文件名 + 头部字段」两条线索，独立判定任一报告所属阶段。

**操作步骤**：

1. 打开 `vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_synth_R16_N4.txt`（注意是 `_synth_`），读其头部 `Design State` 字段。
2. 对比同方案 `utilization_impl_R16_N4.txt` 头部的 `Design State`。
3. 打开 `timing_synth_R16_N4.txt` 与 `timing_impl_R16_N4.txt`，分别找 `Design Timing Summary` 里的 WNS。

**需要观察的现象**：

- synth 利用率报告的 `Design State` 应为 `Synthesized`，impl 的为 `Routed`。
- synth 时序报告的 WNS 通常**大于** impl 的（因为 synth 尚无布线延迟，更乐观）。

**预期结果**：两份 utilization 报告的 `Design State` 一字之差（Synthesized vs Routed），印证流水线阶段。如果你看到的不是这样，说明读错了文件。

> 若无法在本机运行 Vivado，本实践属于纯源码阅读型，结论可由仓库内报告直接读出，无需运行命令。

#### 4.1.5 小练习与答案

**练习 1**：为什么时序报告头部没有 `Design State` 字段，我们仍能判定它是 impl 报告？

**参考答案**：靠文件名 `_impl_` 标签；再用 WNS 数值佐证——impl 报告含真实布线延迟，WNS 比 synth 报告小、更接近真实收敛情况。

**练习 2**：复现 300 MHz 这个实验点时，`.xdc` 里 `create_clock -period` 应大约填多少？

**参考答案**：标称周期 \(1000/300 \approx 3.333\) ns。但因报告内真实频率为 300.030 MHz，精确值待确认，建议先填 3.333 ns 试跑。

---

### 4.2 CIC Compiler IP（IP Catalog 路径）

#### 4.2.1 概念说明

三方案中，最「省事」也最「黑盒」的是 Xilinx 官方的 **CIC Compiler IP**。用户不需要写一行 HDL，而是在 Vivado 的 **IP Catalog** 里搜索 "CIC Compiler"，打开图形化定制界面，勾选抽取率 R、差分延迟 M、级数 N、数据位宽等参数，由工具自动生成一个名为 `cic_compiler_0` 的可综合模块。

这条路径的好处是参数有 GUI 校验、质量有厂商保证；代价是**受 IP 自身参数范围限制**。u2-l4 已发现：CIC Compiler 方案**完全没有 R4 和 N2 的数据**，矩阵最稀疏（仅 11 个 R×N 组合）。这正是 IP 不允许某些取值所致——这是「生成路径决定了实验覆盖范围」的典型例证。

#### 4.2.2 核心流程

```
① IP Catalog 检索 "CIC Compiler"
② Customize IP：填 Filter Type=Decimation、Decimation Rate=R、
                 Differential Delay=M、Number of Stages=N、
                 Input/Output Data Width …
③ Generate IP  → 产出 cic_compiler_0.xci（IP 定制产物，仓库内缺失）
④ 写一层 wrapper HDL 例化 cic_compiler_0
⑤ 走 4.1 的「建工程 → synth → impl → 报告」流水线
```

> 步骤③会生成 `.xci` 文件（IP 定制信息），步骤④需要 wrapper HDL。这两类文件**均不在仓库内**，复现时需自行创建（待确认）。

#### 4.2.3 源码精读

报告头部的 `Design` 字段就是这条路径的「指纹」。CIC Compiler 方案所有报告的 Design 名都固定为 `cic_compiler_0`——这正是 Vivado 默认给首个 CIC Compiler IP 实例的命名：

[vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:6-9](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L6-L9) —— `Command` 行末尾的输出路径 `C:/Users/Elisei/Desktop/report/timing_impl_R16_N4.txt` 暴露了实验机的 Windows 环境与工作目录；`Design : cic_compiler_0` 表明顶层设计是 Vivado 自动命名的 CIC Compiler IP 实例，参数 R/N 不体现在 Design 名里（区别于 HDL Coder 方案）。

注意 Device 字段写法是 `7a100t-csg324`（timing 报告）与 `xc7a100tcsg324-1`（utilization 报告）——同一器件在两类报告里写法不同（u2-l3 已提示），脚本匹配器件名时要兼容两种写法。

#### 4.2.4 代码实践

**实践目标**：验证「CIC Compiler 的 Design 名不随 R/N 变化」。

**操作步骤**：

1. 打开 CIC Compiler 目录下任意两份不同参数的报告，例如 `timing_impl_R8_N6.txt` 与 `timing_impl_R64_N3.txt`。
2. 各读头部第 7 行 `Design` 字段。

**需要观察的现象**：两份报告参数完全不同（R8/N6 vs R64/N3），但 `Design` 都写作 `cic_compiler_0`。

**预期结果**：Design 名恒为 `cic_compiler_0`，与 R/N 无关——这正是 IP 自动命名的特征，也是它与 HDL Coder 方案（Design 名随参数变）的根本区别。

> 待本地验证：若你手头有 Vivado，可自行生成一个 CIC Compiler IP，确认其顶层确实叫 `cic_compiler_0`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 CIC Compiler 方案的实验矩阵里整段缺 R4 与 N2？

**参考答案**：因为 Xilinx CIC Compiler IP 在其定制 GUI 中对这些取值有限制或不支持，参数无法被选中，因而无数据——这是「生成路径限制实验覆盖」的直接证据。

**练习 2**：复现 `cic_compiler_0` 时，仓库里缺哪类文件？

**参考答案**：缺 IP 定制产物 `.xci` 与例化它的 wrapper HDL（`.v`/`.vhd`），以及工程文件 `.xpr`，均需自行准备（待确认）。

---

### 4.3 MATLAB HDL Coder 工作流

#### 4.3.1 概念说明

第二种路径是 **MATLAB HDL Coder**：在 MATLAB 里用 `dsp.CICDecimator` System 对象（或 MATLAB 函数）描述 CIC 抽取器，再用 HDL Coder 把这段算法描述翻译成可综合的 VHDL/Verilog。这条路径介于「黑盒 IP」与「纯手写」之间——算法层用 MATLAB 表达（带浮点仿真保障正确性），RTL 层由工具生成。

HDL Coder 的特征签名是 **Design 名随参数变化**：报告里写作 `CIC_R{R}_N{N}`，例如 `CIC_R16_N4`。这是因为 HDL Coder 会按用户指定的顶层名生成模块，作者顺势把 R、N 编进名字里以便区分。u2-l4 指出，HDL Coder 方案**完整覆盖 5×5 网格**（25 个 R×N 组合），没有 IP 那种参数限制——这是 MATLAB 描述自由度的体现。

#### 4.3.2 核心流程

```
① MATLAB：h = dsp.CICDecimator;
           h.DecimationFactor = R; h.DifferentialDelay = M;
           h.NumSections      = N; h.FixedPointDataType ...
② 生成 RTL：通过 HDL Workflow Advisor，或脚本
           hdlset_param / makehdl(h, 'TargetLanguage','Verilog')
   → 产出顶层模块 CIC_R{R}_N{N}.v（仓库内缺失）
③ 在 Vivado 中新建工程，add_files 加入生成的 RTL
④ 走 4.1 的「synth → impl → 报告」流水线
```

> 步骤②的 MATLAB 源码（`.m`）与生成的 RTL（`.v`/`.vhd`）**均不在仓库内**，具体脚本与参数取值待确认。

#### 4.3.3 源码精读

对比同一份 `timing_impl_R16_N4.txt` 在两方案下的头部，就能一眼看出「生成路径」的差别。CIC Compiler 的 Design 是 `cic_compiler_0`，而 HDL Coder 的 Design 是 `CIC_R16_N4`：

[vivado_reports/reports_at_100Mhz/MATLAB HDL Coder/timing_impl_R16_N4.txt:3-9](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/MATLAB%20HDL%20Coder/timing_impl_R16_N4.txt#L3-L9) —— 工具版本同为 Vivado v.2022.2、器件同为 `7a100t-csg324`，唯独 `Design : CIC_R16_N4` 把抽取率与级数直接写进名字，这是 HDL Coder 生成路径的判别特征。注意 `Date` 为 `Thu Jul 17 23:16:56 2025`，与 CIC Compiler 同方案的 `10:14:51` 不同，说明两方案是在同一天不同时刻分批跑的。

三方案 Design 名对照（补上 Open-source 手写路径，详见 4.4）：

| 方案 | Design 名 | 是否随 R/N 变化 | 生成路径 |
| --- | --- | --- | --- |
| CIC Compiler | `cic_compiler_0` | 否 | IP Catalog |
| MATLAB HDL Coder | `CIC_R{R}_N{N}` | 是 | MATLAB → HDL Coder |
| Open-source CIC | `cic_d` | 否 | 手写 RTL |

#### 4.3.4 代码实践

**实践目标**：用 Design 名的「是否含参数」快速判别一份报告属于哪条生成路径。

**操作步骤**：

1. 从 `MATLAB HDL Coder` 目录挑两份不同参数报告的头部 `Design` 字段，例如 `CIC_R16_N4` 与 `CIC_R64_N6`。
2. 从 `Open-source CIC` 目录挑一份，看其 Design。

**需要观察的现象**：HDL Coder 的 Design 名里能直接读出 R 和 N；Open-source 的 Design 名是固定的 `cic_d`，看不出参数。

**预期结果**：仅凭 `Design` 字段，就能把「HDL Coder 生成」与「手写/IP 生成」区分开——这是脚本批量分类报告时的关键判据。

#### 4.3.5 小练习与答案

**练习 1**：为什么 HDL Coder 方案能覆盖完整 5×5 矩阵，而 CIC Compiler 不能？

**参考答案**：HDL Coder 的参数在 MATLAB 层设置，自由度高、无 IP GUI 的取值限制，因此 R∈{4,8,16,32,64}、N∈{2..6} 全部可生成。

**练习 2**：复现 HDL Coder 方案时，仓库里缺哪类文件？

**参考答案**：缺 MATLAB 算法源码（`.m`，含 `dsp.CICDecimator` 参数设置）与 HDL Coder 生成的 RTL（`.v`/`.vhd`），均需自行准备（待确认）。

---

### 4.4 report 命令与可复现性要素

#### 4.4.1 概念说明

本模块是全讲的收束：把「如何复现」拆成两半——**已经固化的要素**与**缺失需补齐的要素**。

报告头部回显的 `Command` 字段，是整个仓库里**最接近「构建脚本」的东西**。虽然仓库没有 `.tcl` 文件，但每份报告都把它被生成时执行的那条 Tcl 命令原样记了下来。这意味着「导出报告」这一步是完全可复现的——你照着 `Command` 抄一遍即可。但「生成设计、综合、实现」这几步的上游文件，仓库一概没有。

#### 4.4.2 核心流程

复现 = 「补齐缺失上游」+「按头部线索跑下游」。可复现性要素清单如下：

| 类别 | 要素 | 是否在仓库内 | 来源 / 说明 |
| --- | --- | --- | --- |
| 工具链 | Vivado v.2022.2 (win64) Build 3671981 | ✅ 头部可读 | timing/utilization 报告 L3 |
| 目标器件 | xc7a100tcsg324-1（Artix-7，速度等级 -1） | ✅ 头部可读 | utilization L8；timing 写作 7a100t-csg324 |
| 速度文件 | Speed File `-1` PRODUCTION 1.23 | ✅ 头部可读 | timing L9 |
| 报告命令 | `report_timing_summary` / `report_utilization` | ✅ 头部回显 | 见下文源码精读 |
| 实验机 | Host `DESKTOP-OA8NOG1`（Windows） | ✅ 头部可读 | timing L5 |
| HDL 源码 | `.v` / `.vhd` | ❌ 缺失 | 三方案 RTL，需自行准备（待确认） |
| 约束文件 | `.xdc`（含 `create_clock -period`） | ❌ 缺失 | 频率点 100/290/300MHz 的精确周期待确认 |
| Tcl 脚本 | `.tcl`（建工程/跑 run） | ❌ 缺失 | 需自行编写（待确认） |
| 工程文件 | `.xpr` | ❌ 缺失 | 需自行新建 |
| IP 定制 | `.xci`（仅 CIC Compiler） | ❌ 缺失 | 需在 IP Catalog 重建（待确认） |
| MATLAB 源 | `.m`（仅 HDL Coder） | ❌ 缺失 | 需自行编写（待确认） |

文件检索已确认：仓库内只有 `.txt` / `.rpx` / `.xlsx`，没有任何源码或构建文件。所以上表中所有 ❌ 行都是「读者必须自己补」的。

#### 4.4.3 源码精读

两条导出命令是复现的「下游指令」，直接照抄即可。先看时序报告的 `Command`：

[vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:3-9](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L3-L9) —— 头部固化了工具链与器件信息：`Tool Version : Vivado v.2022.2 (win64)`、`Device : 7a100t-csg324`、`Speed File : -1 PRODUCTION 1.23`。这些就是「已经固化、可追溯」的可复现性要素——任何人想严格复现，必须用同版本 Vivado、同型号同速度等级器件。

`Command` 字段里那条长命令的要点拆解（这是「示例代码」，从报告 L6 原样转写）：

```tcl
# 示例代码：导出时序报告（改写自报告头部 Command 字段）
report_timing_summary \
    -delay_type min_max \
    -report_unconstrained \
    -check_timing_verbose \
    -max_paths 10 \
    -input_pins \
    -routable_nets \
    -name  timing_1 \
    -file  ./timing_impl_R16_N4.txt \
    -rpx   ./timing_impl_R16_N4.rpx
```

- `-delay_type min_max`：同时做建立（max）与保持（min）检查。
- `-max_paths 10`：每条路径组最多保留 10 条路径明细。
- `-file`：产出纯文本 `.txt`（可 grep、可脚本解析，见 u3-l5）。
- `-rpx`：额外产出 Vivado GUI 交互格式 `.rpx`（u2-l3 已说明，时序命令带 `-rpx`，故 `.txt` 与 `.rpx` 成对出现）。

再看利用率报告的 `Command`，它短得多：

[vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt:6-10](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L6-L10) —— `Command` 仅有 `report_utilization -file ... -name utilization_1`，**没有 `-rpx`**。这就解释了 u2-l3 的观察：利用率报告只有 `.txt`、没有配套 `.rpx`，是因为命令里压根没写 `-rpx`。改写为示例：

```tcl
# 示例代码：导出利用率报告（改写自报告头部 Command 字段）
report_utilization \
    -name  utilization_1 \
    -file  ./utilization_impl_R16_N4.txt
```

把这两条命令放在 4.1 流水线的第⑤步，你就完整复现了「导出报告」这一环。剩下的，是补齐第①~④步缺失的源码与约束。

#### 4.4.4 代码实践

**实践目标**：亲手从报告头部「挖」出一条可运行的 Tcl 命令，验证仓库具备「报告导出」级的可复现性。

**操作步骤**：

1. 打开本讲引用的 4 份报告头部，逐行记录 `Tool Version`、`Device`、`Speed File`、`Host`、`Command` 五个字段。
2. 把 `Command` 字段抄成两条 Tcl 命令（参考上文示例代码）。
3. 在仓库根目录执行文件检索（例如通配 `**/*.v`、`**/*.tcl`、`**/*.xdc`、`**/*.xpr`），确认这些扩展名**零命中**。

**需要观察的现象**：

- 4 份报告的 `Tool Version` 完全一致（Vivado 2022.2），证明整批数据同源、同工具链。
- `Command` 可直接转写成可读 Tcl。
- 源码/约束/工程文件检索全部为空。

**预期结果**：得到一份「已固化要素清单」（工具链、器件、命令）和一份「缺失要素清单」（HDL、.xdc、.tcl、.xpr、.xci、.m）。前者照抄即可复现下游，后者必须读者自行补齐。

> 待本地验证：第 3 步的检索结果取决于你 clone 的仓库内容；本讲撰写时已确认仓库仅含报告文件。

#### 4.4.5 小练习与答案

**练习 1**：为什么说本仓库具备「报告导出级」可复现性，却不具备「设计级」可复现性？

**参考答案**：因为每份报告头部都原样回显了 `report_timing_summary` / `report_utilization` 命令，照抄即可复现导出；但生成设计所需的 HDL、约束、工程、IP、MATLAB 源码全部缺失，无法从仓库直接重建设计。

**练习 2**：若要让复现结果与本仓库「逐字对齐」，哪几项必须严格一致？

**参考答案**：Vivado 版本（2022.2）、目标器件（xc7a100tcsg324-1）、速度等级（-1，Speed File 1.23）、约束文件里的时钟周期（决定 100/290/300MHz 频率点）。前三项可从头部直接读出，第四项（精确周期）待确认。

---

## 5. 综合实践

把本讲全部内容串起来，完成下面这个「**复现 CIC Compiler R16_N4 @100MHz**」的端到端任务。这是本讲的交付物，请产出一分「复现清单」文档。

### 任务

针对「CIC Compiler 方案、抽取率 R=16、级数 N=4、目标频率 100 MHz」这一实验点，产出：

1. **关键步骤清单**：从「无」到「得到 `timing_impl_R16_N4.txt` 与 `utilization_impl_R16_N4.txt`」的完整步骤（参考 4.1.2 的流水线）。
2. **Vivado 命令清单**：至少包含建工程、加约束（`create_clock -period`）、跑 synth、跑 impl、导出 timing、导出 utilization 六条命令；后两条直接从报告头部 `Command` 抄写。
3. **缺失要素清单**：明确标注仓库中缺失、需读者自行补齐的文件类型，并对每一类给出「从哪里来 / 怎么造」的建议。

### 操作步骤

1. **读头部，锁定条件**：从 `timing_impl_R16_N4.txt` 头部记录工具链、器件、Design 名；从 `utilization_impl_R16_N4.txt` 头部确认 `Design State : Routed`。
2. **补上游**（仓库缺失，标注「需自行准备」）：
   - 在 IP Catalog 生成 CIC Compiler IP，参数取 R=16、N=4（M 与位宽按论文设定，本仓库内待确认）。
   - 写 wrapper 例化 `cic_compiler_0`。
   - 写 `.xdc`：`create_clock -period 10.000 -name clk [get_ports clk]`（对应 100MHz）。
3. **跑流水线**（示例命令，需在装有 Vivado 2022.2 的机器上执行）：

   ```tcl
   # 示例代码：复现流水线（上游文件需自行准备）
   create_project cic_repro ./cic_repro -part xc7a100tcsg324-1
   add_files -norecurse {./cic_compiler_0_wrapper.v ./cic_compiler_0.xci}
   add_files ./constraints.xdc
   set_property top cic_compiler_0_wrapper [current_fileset]
   launch_runs synth_1 -jobs 4
   wait_on_run synth_1
   launch_runs impl_1 -jobs 4
   wait_on_run impl_1
   # 打开 impl_1 的 routed design 后执行：
   report_timing_summary -file ./timing_impl_R16_N4.txt -rpx ./timing_impl_R16_N4.rpx \
       -delay_type min_max -max_paths 10 -input_pins -routable_nets
   report_utilization -file ./utilization_impl_R16_N4.txt
   ```

4. **核对结果**：把你跑出的 `timing_impl_R16_N4.txt` 与仓库版本对比——头部 `Device` 应一致；`Design Timing Summary` 的 WNS 应接近仓库记录的 6.274 ns（不必完全相等，因为布局布线有随机性）。

### 需要观察的现象

- 头部三件套（Tool Version / Device / Design State）应与仓库一致或等价。
- WNS 与仓库值（6.274 ns）量级一致；若数量级不同，多半是约束周期或器件选错。
- 导出的 utilization 报告应看到 DSP=0、Block RAM=0（印证 CIC 只用加减法，见 u2-l2/u3-l1）。

### 预期结果

产出一分「复现清单」，其中：

- **步骤** 7 步左右（生成 IP → wrapper → xdc → 建工程 → synth → impl → 报告）。
- **命令** 至少 6 条；其中两条报告命令照抄头部 `Command`。
- **缺失要素** 至少 5 类：`.v/.vhd`（wrapper）、`.xci`（IP 定制）、`.xdc`（约束）、`.tcl`（构建脚本）、`.xpr`（工程）；HDL Coder 方案另缺 `.m`。每一类都标注「待确认 / 需自行准备」。

> 若你暂时没有 Vivado 环境，本任务也可降级为「纯纸面复现」：仅交付步骤清单、命令清单与缺失要素清单，并在每条命令旁注明其出处（报告头部 `Command` 或通用 Vivado 用法）。

## 6. 本讲小结

- **复现 = 补齐上游 + 照抄下游**：仓库把「导出报告」这一步的 Tcl 命令完整留在头部 `Command` 字段，可直接复现；但生成设计所需的上游文件全部缺失。
- **流水线五步**：准备设计 → 建工程（加源码 + 加 `.xdc`）→ synth → impl（opt/place/route，产出 `Design State : Routed`）→ 导出报告。
- **三种生成路径各有指纹**：CIC Compiler → Design 名固定 `cic_compiler_0`（IP Catalog，受参数限制、矩阵稀疏）；MATLAB HDL Coder → Design 名 `CIC_R{R}_N{N}`（随参数变、覆盖最全）；Open-source → Design 名固定 `cic_d`（手写 RTL）。
- **两条报告命令的差异**：`report_timing_summary` 带 `-rpx`（故时序报告 `.txt`+`.rpx` 成对），`report_utilization` 不带（故利用率报告只有 `.txt`）。
- **可复现性要素分两类**：已固化——Vivado 2022.2、器件 xc7a100tcsg324-1、速度等级 -1、报告命令、实验机；缺失——HDL 源码、`.xdc` 约束、`.tcl` 脚本、`.xpr` 工程、`.xci` IP、`.m` MATLAB 源（均待确认 / 需自行准备）。
- **成功判据**：重跑后头部 `Device`/`Design State` 一致，且 WNS 接近仓库记录值（如 CIC Compiler R16_N4 @100MHz 的 6.274 ns）。

## 7. 下一步学习建议

本讲是学习手册的最后一篇，至此你已经走完「认识项目 → 读懂报告 → 评估分析 → 复现方法」的完整闭环。后续可沿以下方向继续深入：

1. **真正动手复现一个点**：挑一个最小实验点（如 Open-source CIC、R16_N4、100MHz，Design 名 `cic_d`，手写 RTL 最易起步），从写 CIC 抽取器的 Verilog 开始，跑通整条 synth→impl→报告 流水线，亲手得到第一份 WNS。
2. **补齐可复现性**：为仓库配套一份 `.tcl` 构建脚本与示例 `.xdc`，把本讲的「下游命令」封装成可一键运行的流程，填补仓库当前的「设计级」可复现性空白。
3. **跨频率 fmax 实测**：在同一个设计上扫描 `create_clock -period`（100→290→300MHz），实测 WNS 随频率收紧的过程，验证 u3-l2 的 fmax 估算公式 \(\,f_{\max}\approx 1/(T_{\text{clk}}-\text{WNS})\)。
4. **回归论文**：本仓库服务于论文《Post-Implementation Evaluation of CIC Filters for Digital Audio Applications on FPGA》；建议结合论文正文（不在仓库内，待确认）核对实验参数（M 值、位宽、具体音频采样率），把「报告数据」与「论文结论」一一对应起来。

> 推荐继续阅读的「源码」：本仓库内最值得反复研读的就是各报告头部 `Command` / `Design` / `Device` 三字段——它们是连接「数据集」与「真实 FPGA 流程」的唯一桥梁。
