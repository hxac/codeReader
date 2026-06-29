# ASIC 设计流程全景：从 RTL 到 GDSII

> 上一篇（u1-l1）我们认识了仓库本身：它是一份「从 logic 到 layout、从 RTL 到 GDSII」的 ASIC 设计学习路线图，并建立了 **ASIC / FPGA / PPA / RTL / GDSII** 这些基本概念。本篇不再重复这些概念，而是把镜头拉近——**从 RTL 到 GDSII 中间到底发生了什么？** 我们用一条主线把整条设计链路串起来，并指出仓库里哪些脚本分别对应哪个阶段。

## 1. 本讲目标

读完本讲后，你应当能够：

- 用一条主线描述数字 IC 从 **RTL → 综合网表 → 物理设计 → GDSII** 的完整流程，并说出每个阶段的输入和输出。
- 讲清楚后端物理设计的几个经典阶段：**布图规划（floorplan）→ 电源网络（power）→ 布局（placement）→ 时钟树综合（CTS）→ 布线（routing）→ 收尾输出（finishing）**。
- 打开仓库里的 `IC Compiler II/PnR.tcl`，把脚本里的每一段命令对应到上述流程阶段上。
- 解释「签核（sign-off）」与 **GDSII** 交付的含义，理解 RTL 是起点、GDSII 是终点的真正含义。

本讲只建立**全景印象**，不要求你读懂 `PnR.tcl` 的每一条命令——那是后续 U3（库准备）、U4（ICC2 主流程）等单元的任务。本讲的全部依据来自两个文件：`README.md` 与 `IC Compiler II/PnR.tcl`。

## 2. 前置知识

承接 u1-l1，你已经知道 RTL（寄存器传输级代码）是设计的起点、GDSII 是交付代工厂的版图终点。本节只补两个本讲会反复用到的小概念。

### 2.1 什么是「网表（netlist）」

RTL（用 Verilog 写的行为描述）描述的是「电路要做什么」。但芯片最终由具体的门（与门、或门、触发器）组成。**网表**就是把这些门和它们之间的连线列成的一张清单，是「RTL 翻译成具体门级电路」后的产物。你可以把它理解为：

- **RTL** = 菜谱（文字描述步骤）。
- **网表** = 已经切好、摆好盘的食材清单（具体到哪种门、接哪根线）。

后端物理设计工具（本仓库里的 ICC2）**不吃 RTL，只吃网表**——它要给每个门找一个物理位置、再连上线。

### 2.2 前端（frontend）与后端（backend）

业界把设计流程大致分成两段：

- **前端**：把 RTL 翻译成网表，核心动作是**逻辑综合（synthesis）**。
- **后端**：把网表「摆」到芯片版图上，连好线，产出 GDSII，核心动作是**物理设计（physical design / PnR）**。

本仓库的绝大多数脚本都属于**后端**。理解这条「RTL → 网表 → 版图 → GDSII」的主线，是看懂仓库里所有 `.tcl` 脚本的前提。

## 3. 本讲源码地图

本讲只涉及两个文件，但它们一前一后，正好代表「文档」与「真实流程」：

| 文件 | 角色 |
|------|------|
| [README.md](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/README.md) | 仓库的「目录式索引」。其中 `ASIC Design Flow` 一节直接点出了「逻辑综合与时序收敛」与「物理设计」两条主线，是本讲的文字依据。 |
| [IC Compiler II/PnR.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl) | 一份约 210 行的 Synopsys **IC Compiler II** 物理设计主脚本。它几乎按时间顺序走完了整条后端流程，是本讲「源码精读」的主对象。 |

> 名词提示：**PnR** = Place and Route（布局与布线），是物理设计最核心的部分，常被用来代指整个后端流程。**ICC2** = IC Compiler II，是 Synopsys 公司的现代物理设计 EDA 工具。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：前端设计、后端物理设计流程、签核与 GDSII 输出、脚本与流程阶段的对应关系。

---

### 4.1 前端设计：从 RTL 到门级网表

#### 4.1.1 概念说明

前端设计的目标只有一个：**把 RTL 翻译成「工艺库里有现成门」组成的网表**。这件事由**逻辑综合工具**完成（Synopsys 的工具叫 Design Compiler，业内常简称 **DC**）。

综合不是「逐行翻译」，它会在满足你给出的**时序约束（SDC）**的前提下，挑选面积更小、速度更快的实现方式。所以前端阶段的两个关键词是：

- **RTL**：你写的行为级 Verilog。
- **时序约束 SDC**：你告诉工具「时钟有多快、输入输出要留多少时间」。
- **工艺库（Liberty/.db）**：代工厂提供的「门菜单」，综合工具从中挑门。

#### 4.1.2 核心流程

前端阶段可以简化为：

```text
RTL(.v)  ──┐
SDC(.sdc) ──┼──▶ 逻辑综合(Design Compiler) ──▶ 门级网表(.v)
库(.db)  ──┘
```

综合输出的网表是一个 `.v` 文件，但它和 RTL 的 `.v` 已经不是一回事——它里面全是具体的标准单元（如 `AND2X1`、`DFFARX1`），不再是行为描述。

#### 4.1.3 源码精读

`PnR.tcl` 本身是后端脚本，但它用一个变量清晰地暴露了「网表来自前端综合工具」这个事实：

[IC Compiler II/PnR.tcl:14-18](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L14-L18) — 把综合输出的门级网表路径赋值给变量 `gate_verilog`，随后 `read_verilog` 把它读进 ICC2：

```tcl
set TOP_DESIGN ChipTop
set gate_verilog "../../dc/output/compile.v"
read_verilog -top $TOP_DESIGN $gate_verilog
```

注意路径里的 `dc/output/compile.v`：**`dc` 就是 Design Compiler 的缩写**，`output/compile.v` 是它综合后吐出的门级网表。这一行就是「前端交给后端的交接点」——后端工具 ICC2 不关心你的 RTL 长什么样，它只接收这份网表。

仓库的 README 也在「ASIC Design Flow」一节把前端列为第一条主线：

[README.md:79-82](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/README.md#L79-L82) — 明确把「Logic Synthesis & Timing Closure（逻辑综合与时序收敛）」列为设计流程的开端。

> 本讲只点到为止。RTL 怎么写、SDC 怎么写，见 U2 单元；逻辑综合的细节与开源综合器 yosys，见 u10-l1。

#### 4.1.4 代码实践

1. **实践目标**：亲手确认「后端吃的是网表、不是 RTL」。
2. **操作步骤**：
   - 打开 `IC Compiler II/PnR.tcl`，定位到第 15 行 `set gate_verilog "../../dc/output/compile.v"`。
   - 用仓库根目录下的真实 RTL 样例 `MY-Design/MY_DESIGN.v` 与之对比（前者是行为级 RTL，后者是综合后的门级网表路径）。
3. **需要观察的现象**：`PnR.tcl` 全文搜索不到任何 `read_verilog ... MY_DESIGN.v` 之类的「读 RTL」行为；它读的是 `dc/output/compile.v`。
4. **预期结果**：你会确认 ICC2 的输入是综合产物，而不是源 RTL。
5. 运行结果：**待本地验证**（需安装 ICC2 与 DC 才能真正跑通，本讲只做阅读理解）。

#### 4.1.5 小练习与答案

**练习 1**：为什么后端工具不直接读 RTL，而要先经过综合？

> **参考答案**：RTL 是行为描述，没有具体的门和连线，无法决定每个门的物理位置和连线长度；综合把它翻译成由工艺库具体门组成的网表，后端才能「给每个门摆位置、拉连线」。

**练习 2**：`../../dc/output/compile.v` 里的 `dc` 指的是什么？

> **参考答案**：指 Synopsys Design Compiler（逻辑综合工具），`output/compile.v` 是它综合后输出的门级网表。

---

### 4.2 后端物理设计主流程

#### 4.2.1 概念说明

后端（PnR）的任务是：**在芯片这块「硅地皮」上，给网表里的每个门安排位置，再把它们连起来，并保证能跑得快、不违反工艺规则**。它分成几个有先后顺序的经典阶段。本仓库的 `PnR.tcl` 几乎是这些阶段的「全流程样例」，所以本模块用它做主线。

#### 4.2.2 核心流程

后端的经典阶段顺序如下（每个阶段都依赖上一个阶段的结果）：

```text
① setup(初始化：建库、读网表、设层方向、MCMM)
        │
② floorplan(布图规划：画 die/core 边界、放引脚、固定宏单元)
        │
③ power(电源网络：环/rail/mesh，给每个单元供电)
        │
④ placement(布局：给标准单元找合法位置)
        │
⑤ CTS(时钟树综合：把时钟均衡地分发到每个触发器)
        │
⑥ routing(布线：把所有逻辑连线变成金属线)
        │
⑦ finishing(收尾：插填充、修 DRC、输出交付物)
```

README 对后端的概括也正好印证了这套阶段划分：

[README.md:83-85](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/README.md#L83-L85) — 「Physical Design」覆盖 Floorplanning、Placement、CTS、Routing、DRC/LVS。

下面挑几个阶段，用 `PnR.tcl` 的真实命令说明。

#### 4.2.3 源码精读

**(a) setup 阶段：建立设计库、读网表、设置金属层方向**

[IC Compiler II/PnR.tcl:1-9](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L1-L9) — 设置多核、source 通用设置、设库、`create_lib` 建立设计库：

```tcl
set_host_options -max_cores 16
source ./input/common_setup.tcl
...
create_lib -ref_libs $NDM_REFERENCE_LIB_DIRS_MVT -technology $TECH_FILE ../work/chiptop
```

[IC Compiler II/PnR.tcl:20-29](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L20-L29) — 逐层声明金属布线方向（垂直/水平交替），这是后续布线能「横竖不打架」的前提。

**(b) floorplan 阶段：画出 die 与 core**

[IC Compiler II/PnR.tcl:38-40](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L38-L40) — 初始化布图、放引脚：

```tcl
initialize_floorplan -flip_first_row true -boundary {{0 0} {400 400}} -core_offset {15 15 15 15}
place_pins -ports [get_ports *]
```

`-boundary {{0 0} {400 400}}` 画出了一个 400×400 微米见方的 die（芯片外框），`-core_offset {15 15 15 15}` 表示四边各内缩 15 微米得到 core（可放标准单元的可用区）。

一个衡量布图好坏的关键指标是**利用率（utilization）**：

\[
U = \frac{A_{\text{cell}}}{A_{\text{core}}}
\]

其中 \(A_{\text{cell}}\) 是所有标准单元面积之和，\(A_{\text{core}}\) 是 core 可用面积。\(U\) 太高（如 >0.85）会布不通、拥塞；太低又浪费面积。

**(c) power 阶段：电源 mesh 与 rail**

[IC Compiler II/PnR.tcl:86-97](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L86-L97) — 定义电源 mesh 模式并 `compile_pg` 编译供电网络：

```tcl
create_pg_mesh_pattern P_top_two -layers { ... }
set_pg_strategy S_default_vddvss -core -pattern { {name: P_top_two} {nets:{VSS VDD}} } ...
compile_pg -strategies {S_default_vddvss}
```

**(d) placement 阶段：`place_opt`**

[IC Compiler II/PnR.tcl:141-143](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L141-L143) — 布局优化并合法化、报告布局质量：

```tcl
place_opt
legalize_placement
report_placement
```

注意它前面一行就有 `############place_opt#################################` 这样的「`#` 墙」分隔注释——这正是后面实践任务要找的阶段标记。

**(e) CTS 阶段：`clock_opt`**

[IC Compiler II/PnR.tcl:161-166](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L161-L166) — 设置时钟树目标（偏斜/延迟），再 `clock_opt` 综合时钟树：

```tcl
set_clock_tree_options -target_latency 0.000 -target_skew 0.000
clock_opt
```

**(f) routing 阶段：`route_opt`**

[IC Compiler II/PnR.tcl:171-175](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L171-L175) — 声明允许的布线层范围，再 `route_opt` 完成布线：

```tcl
remove_ignored_layers -all
set_ignored_layers -min_routing_layer $MIN_ROUTING_LAYER -max_routing_layer $MAX_ROUTING_LAYER
route_opt
```

> 小结：`place_opt` → `clock_opt` → `route_opt` 这三行是后端流程的「三大主命令」，恰好对应 placement / CTS / routing 三个阶段。记住这三个名字，你就抓住了 `PnR.tcl` 的骨架。

#### 4.2.4 代码实践

1. **实践目标**：在 `PnR.tcl` 中肉眼追踪后端流程顺序。
2. **操作步骤**：从第 1 行往下读，按出现先后记录这些命令的行号：`create_lib`、`read_verilog`、`initialize_floorplan`、`compile_pg`、`place_opt`、`clock_opt`、`route_opt`。
3. **需要观察的现象**：它们的出现顺序应该严格遵循 setup → floorplan → power → placement → CTS → routing。
4. **预期结果**：你会得到一张「命令 → 阶段」的对应表，确认脚本确实是按设计流程的时间顺序书写的。
5. 运行结果：**待本地验证**（仅阅读，无需运行 EDA 工具）。

#### 4.2.5 小练习与答案

**练习 1**：为什么电源网络（power）要放在布局（placement）之前做？

> **参考答案**：电源 mesh/rail 决定了每一行标准单元的供电轨道（VDD/VSS）。只有先铺好这些轨道，布局时标准单元才能「坐」在轨道上、自动接上电源；反过来先布局再铺电源，容易造成单元供电缺失或大量重做。

**练习 2**：`place_opt`、`clock_opt`、`route_opt` 分别对应哪三个后端阶段？

> **参考答案**：分别对应 **placement（布局）**、**CTS（时钟树综合）**、**routing（布线）**。

**练习 3**：若某设计的布图利用率 \(U\) 高达 0.95，你预期后端会出什么问题？

> **参考答案**：几乎没有余量放标准单元和走线，极可能出现**拥塞（congestion）**和**布不通（routing DRC 违例）**，需要加大 core 面积或减少逻辑。

---

### 4.3 签核与 GDSII 输出

#### 4.3.1 概念说明

后端流程跑完，并不意味着「可以交付了」。在真正把版图交给代工厂之前，还要做两件事：

- **签核（sign-off）**：用比 PnR 工具更权威、更严格的方法**独立验证**时序等关键指标。本仓库对应的签核工具是 Synopsys **PrimeTime**（做静态时序分析 STA）。
- **输出 GDSII**：把版图导出成代工厂能识别的二进制格式 **GDSII**（`.gds`），这是真正交付给晶圆厂的「图纸」。

所以 RTL → GDSII 这条线的「终点」不是 `route_opt`，而是 **写出 GDSII 文件那一刻**。

#### 4.3.2 核心流程

签核与输出可以简化为：

```text
完成 routing 的版图
      │
      ├─▶ 导出网表/SDC/SPEF(寄生) ──▶ PrimeTime 签核 STA
      │
      └─▶ write_gds ──▶ GDSII 文件 ──▶ 交付代工厂
```

其中 **SPEF** 是描述版图实际连线寄生（电阻/电容）的文件，签核工具靠它算出真实的时序延迟。

#### 4.3.3 源码精读

`PnR.tcl` 的最后一段就是「收尾 + 签核准备 + 输出」：

[IC Compiler II/PnR.tcl:187-207](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L187-L207) — 先出报告与存档，再分别导出 SPEF、网表、GDSII：

```tcl
report_design -all
report_timing
report_power
save_block -as "${TOP_DESIGN}_Final"
...
write_parasitics -output {../output/${TOP_DESIGN}.spef}        ;# 给 PrimeTime 用的寄生
write_verilog -include {pg_netlist unconnected_ports} ../output/${TOP_DESIGN}_pg.v   ;# 带电源的网表
write_verilog -exclude {pg_netlist} ../output/${TOP_DESIGN}.v                       ;# 普通网表
write_gds ... ../output/${TOP_DESIGN}.gds                      ;# 交付代工厂的 GDSII
```

注意几个关键点：

- `write_parasitics ... .spef`：导出寄生文件，**专门喂给 PrimeTime 做签核**。
- `write_gds ... .gds`：产出 **GDSII**，这才是 RTL→GDSII 这条线的物理终点。
- `report_timing`：在 PnR 内部先看一眼时序。签核阶段的「时序好不好」用静态时序分析的语言描述为 **slack（余量）**：

\[
\text{slack} = T_{\text{required}} - T_{\text{arrival}}
\]

\( \text{slack} \ge 0 \) 表示满足时序，\( \text{slack} < 0 \) 表示违例（需要回头修）。PnR 工具的 slack 只是「内部估计」，最终是否过关以 PrimeTime 签核为准。

README 同样把「RTL to GDSII」作为整条流程的标语：

[README.md:87-88](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/README.md#L87-L88) — 列出「RTL to GDSII」作为物理设计学习的推荐主线资料。

> 本讲只建立「签核 + GDSII 是终点」的概念。PrimeTime 的实际脚本与 STA 细节，见 U6 单元。

#### 4.3.4 代码实践

1. **实践目标**：找到「GDSII 终点」对应的代码行。
2. **操作步骤**：打开 `PnR.tcl`，定位到 `write_gds` 开头的那几行（约第 200 行），阅读它的 `-output` 路径。
3. **需要观察的现象**：输出文件名以 `.gds` 结尾，且会 `merge_files`（合并标准单元和 SRAM 的 GDS 视图）。
4. **预期结果**：你能指认出「这一行就是 RTL→GDSII 流程的最后一公里」。
5. 运行结果：**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`.spef` 文件是给谁用的？为什么需要它？

> **参考答案**：给签核工具 PrimeTime 用。它记录了版图连线的真实寄生（RC），PrimeTime 据此算出真实时序延迟，从而判断是否满足时序。没有寄生，时序分析就不准。

**练习 2**：为什么说 `route_opt` 跑完还不算「到了 GDSII」？

> **参考答案**：因为 `route_opt` 之后还要做收尾（插填充、修 DRC）、出报告、导出 SPEF/网表、最后 `write_gds` 才得到 GDSII。`route_opt` 只是布线完成，离交付还差输出这一步。

---

### 4.4 仓库脚本与流程阶段的对应关系

#### 4.4.1 概念说明

学完全景后，最重要的不是记住某条命令，而是建立「**仓库里的每个脚本对应流程的哪一段**」这张地图。有了这张地图，你以后看到任何一个 `.tcl` 文件，就能立刻知道它在你脑中流程图的哪个位置。

#### 4.4.2 核心流程

把仓库的主要脚本/目录按设计阶段对齐：

| 设计阶段 | 对应的仓库脚本/目录 | 说明 |
|----------|---------------------|------|
| 前端 RTL | `MY-Design/MY_DESIGN.v`、`cmsdk/` | 可综合的 Verilog 设计样例 |
| 前端约束 | `MY-Design/My_Design.cons` | SDC 时序约束样例 |
| 库准备 | `IC Compiler II/NDM_Creation.tcl`、`LEF2FRAM/` | 把工艺库/LEF 转成 ICC2 的 NDM 参考库 |
| 后端主流程 | `IC Compiler II/PnR.tcl`（本讲主角） | 走完 setup→floorplan→…→finishing |
| NDR 自动化 | `IC Compiler II/NDR_rule.pl` | 自动生成时钟布线规则 |
| 低功耗意图 | `low_power.upf` | 多电压域电源意图（UPF） |
| 签核 STA | `PrimeTime/`（`pt.tcl` 等） | 静态时序分析签核 |
| 其它工具流程 | `IC Compiler/`、`mentor_scripts/` | 对比 ICC 传统流程与 Mentor Nitro |
| 版图定制 | `Logo.pl` | SKILL 脚本，把图像画到版图上 |

> 这张表覆盖了后续 U2–U10 几乎全部讲义的入口。本讲你只需记住：**`PnR.tcl` 是后端主流程的「骨架脚本」**，其余脚本围绕它做库准备、自动化、签核和对比。

#### 4.4.3 源码精读

`PnR.tcl` 用一排排 `#` 字符作为「阶段分隔注释」。你可以在脚本里搜到这些标记，它们把 210 行的脚本天然切成了若干段：

[IC Compiler II/PnR.tcl:137](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L137) — `############place_opt#################################`（布局阶段开始）

[IC Compiler II/PnR.tcl:154](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L154) — `#########################Setting CTS Options###############################`（CTS 阶段开始）

[IC Compiler II/PnR.tcl:164](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L164) — `############clock_opt#################################`（CTS 主命令）

[IC Compiler II/PnR.tcl:170](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L170) — `############route_opt#################################`（布线阶段开始）

[IC Compiler II/PnR.tcl:185](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L185) — `###########reports##########################`（收尾报告与输出）

> 注意：脚本里没有字面写「setup」「floorplan」「routing」这些英文阶段名，但 `place_opt`、`clock_opt`、`route_opt` 这几行注释就是事实上的阶段分隔。把它们和本讲 4.2 的流程图一一对应，是理解整个仓库的钥匙。

#### 4.4.4 代码实践（本讲主实践任务）

1. **实践目标**：亲手把 `PnR.tcl` 的注释分段画成一张流程图。
2. **操作步骤**：
   - 通读 `IC Compiler II/PnR.tcl`，找出所有「`#` 墙」分隔注释（如 `place_opt`、`Setting CTS Options`、`clock_opt`、`route_opt`、`reports` 等）。
   - 在每段注释下方找到该段的「主命令」：`create_lib`（setup）、`initialize_floorplan`（floorplan）、`compile_pg`（power）、`place_opt`（placement）、`clock_opt`（CTS）、`route_opt`（routing）、`write_gds`（finishing）。
   - 用纸或画图工具，从上到下画出 7 个方框，每个方框写「阶段名 + 主命令 + 行号」。
3. **需要观察的现象**：7 个阶段方框之间是单向箭头，没有任何「回头」——这印证了后端流程的强先后顺序。
4. **预期结果**：得到一张类似下面的流程图：
   ```text
   setup(create_lib,L9) → floorplan(initialize_floorplan,L38) → power(compile_pg,L97)
        → placement(place_opt,L141) → CTS(clock_opt,L166)
        → routing(route_opt,L175) → finishing(write_gds,L200)
   ```
5. 运行结果：**待本地验证**（本实践是源码阅读型，无需运行 EDA 工具）。

#### 4.4.5 小练习与答案

**练习 1**：仓库里 `PrimeTime/` 目录对应流程的哪一段？`LEF2FRAM/` 呢？

> **参考答案**：`PrimeTime/` 对应**签核（sign-off / STA）**阶段；`LEF2FRAM/` 对应**库准备**阶段（在 setup 之前，把物理库数据转换成工具可用的参考库）。

**练习 2**：如果只看一个文件就想了解「整条后端流程」，你会选哪个？为什么？

> **参考答案**：选 `IC Compiler II/PnR.tcl`。因为它按时间顺序串起了 setup→floorplan→power→placement→CTS→routing→finishing 几乎全部后端阶段，是一份「全流程样例」。

---

## 5. 综合实践

把本讲的四个模块串起来，完成下面这个贯穿性小任务：

**任务：画出「RTL → GDSII」全流程脚本地图**

1. 准备一张白纸，横向分成三大列：**前端**、**后端**、**签核与交付**。
2. 在「前端」列里写上：输入是 `MY-Design/MY_DESIGN.v`（RTL）+ `My_Design.cons`（SDC），动作是综合（DC），输出是 `dc/output/compile.v`（网表）。
3. 在「后端」列里，把 `PnR.tcl` 的 7 个阶段按顺序画成竖向链，每个阶段标注主命令和行号（参考 4.4.4 的流程图）。
4. 在「签核与交付」列里，从后端末尾分出两条线：一条 `write_parasitics → .spef → PrimeTime`（签核），一条 `write_gds → .gds`（交付代工厂）。
5. 最后在整张图最左端写 **RTL**、最右端写 **GDSII**，确认你画出的就是本讲标题「从 RTL 到 GDSII」。

完成后，你应该能把仓库里任何一个脚本（`NDM_Creation.tcl`、`pt.tcl`、`low_power.upf`、`NDR_rule.pl` …）放回这张图的某个位置。这张图也是你后续阅读 U3–U10 所有讲义的「导航底图」。

## 6. 本讲小结

- ASIC 设计的主线是 **RTL → 综合网表 → 物理设计（PnR）→ 签核 → GDSII**；前端用 RTL+SDC 做综合，后端吃的是网表而不是 RTL。
- 后端物理设计有清晰的阶段顺序：**setup → floorplan → power → placement → CTS → routing → finishing**，缺一不可、先后固定。
- `PnR.tcl` 的三大主命令 `place_opt` / `clock_opt` / `route_opt` 分别对应 placement / CTS / routing，抓住这三个词就抓住了脚本骨架。
- 真正的流程终点不是 `route_opt`，而是 **`write_gds` 产出 GDSII**；签核（PrimeTime + SPEF）负责在交付前独立验证时序。
- 仓库里的每个脚本都能放进这条主线：库准备（`NDM_Creation.tcl`、`LEF2FRAM/`）在前、PnR 主流程（`PnR.tcl`）居中、签核（`PrimeTime/`）在后，低功耗（`low_power.upf`）与自动化（`NDR_rule.pl`）穿插其中。

## 7. 下一步学习建议

- 如果你想先看懂 RTL 与约束：进入 **U2**，从 `MY-Design/MY_DESIGN.v` 学 Verilog 基础，再到 `My_Design.cons` 学 SDC 时序约束。
- 如果你想直接深入后端主流程：进入 **U4**，那里会把 `PnR.tcl` 的 setup/floorplan/placement/CTS/routing/finishing 每个阶段单独拆成一篇精读讲义。
- 在进入 U4 之前，建议先读 **U3**（库数据与物理数据准备），因为 `PnR.tcl` 第 9 行 `create_lib -ref_libs ...` 所引用的 NDM 参考库，正是 U3 讲的库准备产物。
- 签核侧的 PrimeTime 静态时序分析，集中在 **U6** 单元，建议在学完 U4 的 finishing/输出后再读。
