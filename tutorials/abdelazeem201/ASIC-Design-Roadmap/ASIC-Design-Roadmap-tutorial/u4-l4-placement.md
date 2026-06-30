# 布局 Placement

## 1. 本讲目标

走过 u4-l2（布图规划）和 u4-l3（电源网络），芯片现在有了 die/core 边界、摆好的宏单元、铺好的电源 mesh/ring/rail。但标准单元还只是"虚拟布局"（`create_placement -floorplan`）摆的一张草图——**只为评估布图好不好，并不优化**。本讲要做的，是真正把标准单元摆到它们该在的位置、并反复优化时序：这就是 **布局（Placement）** 阶段，ICC2 的主命令是 `place_opt`。

学完本讲，你应当能够：

1. 读懂 `place_opt`，理解它内部"粗放布局 → 时序优化 → 合法化"的迭代流程，并能与 floorplan 阶段的 `create_placement` 区分。
2. 区分 `legalize_placement`（**修复**重叠/越位）与 `check_legality`（**只检查不修复**），理解为什么布局后两者都要做。
3. 读懂布局前后那批 `set_app_options`，特别是 `time.disable_recovery_removal_checks`（恢复-移除检查）与 `time.disable_case_analysis`（case 分析）对时序优化的影响。
4. 学会读 `report_qor`（WNS/TNS）和 `report_utilization`（利用率），判断布局质量是否达标。
5. 理解 `03_PnR_setup.tcl` 里"copy_block → current_block → 干活 → save_block"的**block 版本演进**模式，以及极简模板为何丢掉了它。

> **本讲的关键发现（承接 u4-l2）**：和 floorplan 一样，布局在仓库里也有**两份**实现。`IC Compiler II/PnR.tcl` 是极简模板——布局只有 6 行（3 个 set_app_options + place_opt + legalize_placement + report_placement）；`IC Compiler II/Scripts/03_PnR_setup.tcl` 才是带 set_voltage、check_legality、report_qor/report_utilization 落盘、以及 copy_block/save_block 版本管理的完整参考流程。我们对照着读：先看模板"做了什么"，再看参考脚本"还该做什么"。

---

## 2. 前置知识

进入源码前，先把布局阶段的高频术语用大白话过一遍。承接 u4-l2 已建立的 coarse placement、legalization、site、利用率等概念，以及 u4-l1 的 MCMM（slow/fast 角）。

| 术语 | 通俗解释 |
| --- | --- |
| **place_opt（布局优化）** | ICC2 的"一站式"布局命令。内部自动跑：初始布局 → 时序/面积/拥塞优化 → 合法化，反复迭代。是 placement 阶段真正干活的引擎。 |
| **coarse placement（粗放布局）** | place_opt 的第一阶段。先不考虑所有细节约束，快速把单元大致摆好以降低总线长/改善时序，之后再精修。`place.coarse.*` 系列选项控制它。 |
| **legalization（合法化）** | `legalize_placement`：把优化过程中产生的重叠、越位、不对齐 site 的单元**主动修复**——吸附到合法格子、绕开 fixed 宏单元和 placement blockage。是"动手修"。 |
| **check_legality（合法性检查）** | `check_legality`：**只报告**当前布局是否合法（有无重叠、是否对齐 site、有无 DRC），不动手修。是"体检"。 |
| **recovery / removal（恢复 / 移除）** | 异步控制信号（如异步复位 reset_n）相对时钟沿的时序检查。recovery 像 setup（时钟沿前需稳定多久），removal 像 hold（时钟沿后需稳定多久）。 |
| **case analysis（案例分析）** | 给某些引脚设固定逻辑值（如模式选择脚 = 0），并把常量向前传播，关掉那些在该模式下"永远不可能激活"的时序弧，使时序分析更准。 |
| **scandef（scan DEF）** | 扫描链连接信息（DFT 测试用），规定哪些寄存器串在一条扫描链上、先后顺序如何。布局时可据此把同链寄存器摆近，减少扫描布线。 |
| **QoR（Quality of Results）** | `report_qor` 产出的质量总表：含 WNS/TNS、面积、单元数、违例数等。是判断"时序收不收敛"的一页成绩单。 |
| **WNS / TNS** | Worst/Total Negative Slack：最差/累计负裕量。WNS 是最差那条路径离达标还差多少；TNS 把所有违例路径的负裕量加起来。两者越接近 0 越好。 |
| **block 版本（block version）** | 一个设计在 NDM 库里可以存成多个具名快照（如 `placement.design`、`cts.design`）。每阶段从上一阶段拷贝出新块来干活，形成一条版本链，便于回退与审计。 |

**一个直觉**：把布局想成"搬家摆家具"。`create_placement -floorplan`（u4-l2）是搬家公司进门**先大致堆一堆**看好不好放；`place_opt` 是**正式开摆**——边摆边量尺寸、边换家具型号（buffer/尺寸优化）、边保证不重叠不挡道（合法化），目标是"住进去后走动最顺"（时序最好）。

---

## 3. 本讲源码地图

| 文件 | 在本讲的作用 |
| --- | --- |
| [IC Compiler II/PnR.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl) | **极简模板**。布局段只有 L137–L143 共 6 行，是本讲"骨架"的主线；紧随其后的 std filler（L146–L153）作为衔接也略读。 |
| [IC Compiler II/Scripts/03_PnR_setup.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl) | **完整参考流程**。布局段 L319–L340 含 set_voltage、`opt.common.user_instance_name_prefix`、`time.delay_calculation_style`、`check_legality`、`report_qor`/`report_utilization` 落盘、以及 copy_block/save_block 版本链，是本讲"血肉"的主要依据。 |

> 提醒：两份脚本是不同设计的模板（`PnR.tcl` 的设计名 `ChipTop`、`03_PnR_setup.tcl` 的 `pit_top`），单元名、库名互不相同；我们关注**命令与流程**，不纠结具体实例名。

---

## 4. 核心概念与源码讲解

### 4.1 place_opt 流程

#### 4.1.1 概念说明

`place_opt` 是 ICC2 placement 阶段的**总命令**——你只敲这一行，它在内部自动完成一整套布局优化。理解它，关键是抓住它内部的"三段式迭代"：

1. **初始 / 粗放布局（initial / coarse placement）**：先快速把所有标准单元大致摆到 core 里，目标是最小化总线长、缓解拥塞。这一步允许暂时不完美（可能有局部重叠），由 `place.coarse.*` 系列选项控制。
2. **时序 / 面积优化（optimization）**：这是 `place_opt` 区别于 `create_placement` 的核心。它反复做：移动单元、**插入 buffer**、**改变单元尺寸（gate sizing）**、克隆关键寄存器、做逻辑重构……一切都是为了把 WNS/TNS 往 0 拉。这一段是"时序驱动"的。
3. **合法化与精修（legalization & refinement）**：每次优化改动后，把单元吸附回合法 site、消除重叠，再做一轮精修布局。

`place_opt` 默认就是**时序驱动**的——它盯着你 u4-l1 配好的 MCMM 各 scenario 的 setup/hold slack 来优化。

> **与 u4-l2 的对照（重要）**：floorplan 阶段的 `create_placement -floorplan` 是"**只摆不优化**"的试摆，目的是给拥塞/零互连评估提供一张图；本讲的 `place_opt` 是"**边摆边优化**"的真布局，会迭代做 buffer 插入与尺寸优化。前者是草图，后者是正稿。

#### 4.1.2 核心流程

1. 从电源网络完成后的块出发（`copy_block` from `power_plan_1`，见 4.5）。
2. 设好布局前的 app 选项（时序检查开关、scandef 容错、cell 命名前缀等，见 4.3）。
3. 设好延迟计算方式与工作电压（参考脚本）。
4. `place_opt` 跑完整布局优化。
5. `legalize_placement` 清理残余重叠（见 4.2）。
6. 出报告评估（见 4.4）。
7. `save_block` 存版本（见 4.5）。

#### 4.1.3 源码精读

**极简模板的布局段——只有 6 行**：

```tcl
############place_opt#################################
set_app_options -name time.disable_recovery_removal_checks -value false
set_app_options -name time.disable_case_analysis -value false
set_app_options -name place.coarse.continue_on_missing_scandef -value true
place_opt
legalize_placement
report_placement
```

来自 [IC Compiler II/PnR.tcl:L137-L143](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L137-L143)：先开 3 个 app 选项（4.3 详解），然后 `place_opt` 一行完成全部布局优化，`legalize_placement` 收尾清重叠，`report_placement` 出一份布局状态报告。模板把"该有的骨架"都摆出来了，但省了 `check_legality`、QoR/利用率落盘和版本保存。

**参考流程的布局段——血肉齐全**：

```tcl
########### 4. Placement #####################
puts "start_place"
copy_block -from_block power_plan_1.design -to placement.design
current_block placement.design

set_app_options -name time.disable_recovery_removal_checks -value false
set_app_options -name time.disable_case_analysis -value false
set_app_options -name place.coarse.continue_on_missing_scandef -value true
set_app_options -name opt.common.user_instance_name_prefix -value place

set_app_options -list {time.delay_calculation_style auto}
set_voltage 0.95
place_opt
legalize_placement
check_legality -verbos

report_qor > ./report/placement/qor.rpt
report_utilization > ./report/placement/utilization.rpt

save_block
```

来自 [IC Compiler II/Scripts/03_PnR_setup.tcl:L319-L340](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L319-L340)。比模板多出的关键内容：

- `copy_block` + `current_block`（L323–L324）：从电源网络完成块 `power_plan_1.design` **拷贝**出全新的 `placement.design` 来干活，原块不动（4.5 详解版本链）。
- `opt.common.user_instance_name_prefix -value place`（L329）：优化时新插入的 buffer/反相器等单元，名字统一加 `place` 前缀，便于事后追溯"这个单元是布局阶段加的"。
- `time.delay_calculation_style auto`（L331）：从 floorplan 评估时的 `zero_interconnect`（u4-l2）切回"真实估计延迟"——既然要真优化时序，就不能再假设线延迟为 0。
- `set_voltage 0.95`（L332）：设 0.95V 工作电压做延迟计算，对应 MCMM 的 slow 角（u4-l1）；电压低 → 单元慢 → setup 更紧张。
- `check_legality -verbos`（L335）：合法化后再体检一次（4.2 详解）。
- `report_qor` / `report_utilization` 重定向到文件（L337–L338）：把成绩单和利用率写进 `./report/placement/` 留档（4.4 详解）。

#### 4.1.4 代码实践

**实践目标**：在两份脚本里数清 `place_opt` 前后到底调了哪些命令，建立"骨架 vs 血肉"的对照表。

1. 打开 [IC Compiler II/PnR.tcl:L137-L143](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L137-L143)，列出模板的 6 行：3 × `set_app_options` → `place_opt` → `legalize_placement` → `report_placement`。
2. 打开 [IC Compiler II/Scripts/03_PnR_setup.tcl:L326-L340](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L326-L340)，圈出参考脚本**多出**的部分：`opt.common.user_instance_name_prefix`、`time.delay_calculation_style auto`、`set_voltage 0.95`、`check_legality`、两条 report 重定向、`save_block`。
3. 把差异填进一张表：左列"命令"，右列"模板有 / 参考有 / 作用"。

**需要观察的现象 / 预期结果**：模板的"必做最小集"是 set 选项 + place_opt + legalize；参考脚本把"可选但推荐"的电压设定、命名前缀、二次体检、报告落盘、版本保存都补上了。这是**源码阅读型实践**，无需运行工具。

#### 4.1.5 小练习与答案

**练习 1**：为什么 floorplan 阶段用 `create_placement -floorplan`，而正式布局要用 `place_opt`，不能反过来？

**答案**：floorplan 阶段还没定局（电源网络、宏单元位置都可能再调），只需要一张快速试摆图来评估布图，`create_placement` 又快又够用；`place_opt` 会做昂贵的 buffer 插入和尺寸优化，此时跑等于白干（后续 floorplan 一变全废）。反过来——若正式布局阶段只用 `create_placement`，则永远不做时序优化，WNS 不会收敛。所以两者各司其职、顺序固定。

**练习 2**：`place_opt` 内部"粗放布局 → 优化 → 合法化"三段，为什么不能只做一次、而要反复迭代？

**答案**：每次插入 buffer 或改尺寸都会改变单元数量和位置，破坏上一次的合法性，需要重新合法化；而合法化移动单元又会改变线长和时序，又触发新一轮优化。时序收敛本质是个"牵一发动全身"的迭代过程，单次达不到最优。

---

### 4.2 合法化检查：legalize_placement 与 check_legality

#### 4.2.1 概念说明

`place_opt` 在反复移动单元、插 buffer、改尺寸的过程中，难免留下"尾巴"——有些单元可能互相重叠、没对齐到 site、或者压到了 fixed 宏单元/keepout 区上。这些都必须在进入 CTS 前清掉。这里有两个**容易混淆**的命令：

| 命令 | 性质 | 作用 |
| --- | --- | --- |
| `legalize_placement` | **修复型** | 主动把越位/重叠的单元**搬**到合法 site，绕开 fixed 对象，消除 DRC。会改变布局。 |
| `check_legality` | **检查型** | 只**报告**当前布局是否合法（重叠数、越位数、DRC），不改变任何东西。是"体检报告"。 |

参考脚本的写法是**先修后查**：`legalize_placement`（修）→ `check_legality`（确认修干净了）。这就像装修完先打扫（legalize），再验收（check）。

> 为什么两个都要？`legalize_placement` 修完后**理论上**应全部合法，但实际可能因拥塞或 fixed 对象太挤而**修不干净**（个别单元无合法位置可放）。`check_legality` 就是为了**抓出这些漏网之鱼**——若报告显示仍有违例，说明 core 太挤或 floorplan 有问题，需要退回去调整，而不是带着违例硬进 CTS。

#### 4.2.2 核心流程

1. `place_opt` 结束，布局基本就位但可能有残余重叠。
2. `legalize_placement` 修复。
3. `check_legality`（参考脚本带 `-verbos`）出详细体检报告。
4. 若仍有违例 → 退回加大 core / 调 floorplan；若干净 → 进 CTS。

#### 4.2.3 源码精读

**极简模板只修不查**：

```tcl
place_opt
legalize_placement
report_placement
```

来自 [IC Compiler II/PnR.tcl:L141-L143](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L141-L143)：模板只调 `legalize_placement` 修复，随后 `report_placement` 出一份布局摘要——注意 `report_placement` 是"布局状态报告"（含合法/非法统计），功能上**近似** check，但 ICC2 里更标准的"体检"命令是 `check_legality`。

**参考脚本先修后查**：

```tcl
place_opt
legalize_placement
check_legality -verbos
```

来自 [IC Compiler II/Scripts/03_PnR_setup.tcl:L333-L335](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L333-L335)：`legalize_placement` 修复后，紧跟 `check_legality -verbos` 验收。

> **诚实说明**：脚本里写的是 `-verbos`（[L335](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L335-L335)），疑为 ICC2 标准开关 `-verbose` 的笔误（少了一个 `e`）。工具是否接受这个拼写、以及它具体打印多详细，**待本地验证**；读脚本时按 `-verbose` 的语义理解即可。

#### 4.2.4 代码实践

**实践目标**：体会"修复型"与"检查型"的区别，判断哪个能改变布局。

1. 读 [IC Compiler II/Scripts/03_PnR_setup.tcl:L334-L335](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L334-L335) 的 `legalize_placement` 与 `check_legality -verbos`。
2. 设想：把这两行**互换顺序**（先 `check_legality` 再 `legalize_placement`），报告的违例数会怎样变化？

**预期结果**：先 check 后 legalize——check 报告里会**还有违例**（因为还没修）；先 legalize 后 check——check 报告应**基本无违例**（已修）。这正是"先修后查"的顺序依据。这是**阅读 + 推理型实践**，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：`legalize_placement` 跑完，`check_legality` 仍报 3 处重叠，可能是什么原因？

**答案**：最可能是该区域塞得太满——fixed 宏单元 + keepout + placement blockage 把空间占光，合法化时找不到合法位置放下所有单元。处理方向：退回 floorplan 加大该区 core、缩小/移走 blockage、或重做综合减少单元数。

**练习 2**：为什么进入 CTS（u4-l5）之前必须保证 `check_legality` 干净？

**答案**：CTS 要基于当前布局插时钟 buffer、连时钟树，若此时还有单元重叠/越位，时钟树的长度、skew、latency 都会算歪，后面 routing 还会引发一堆 DRC。布局合法是 CTS 的前提条件。

---

### 4.3 布局前后的 timing app options

#### 4.3.1 概念说明

这是本讲的**核心模块**，也对应指定的实践任务。`place_opt` 本身只是"启动优化引擎"，但**优化什么、按什么口径算时序**，由它前面那批 `set_app_options` 决定。布局阶段这批选项集中在三件事：**要不要做某些时序检查**、**布局容错策略**、**新单元怎么命名**。

先解释两个最关键、也最容易看反的时序检查开关（它们的名字带 `disable`，是"双重否定"）：

**① `time.disable_recovery_removal_checks -value false`**

- 字面：关闭恢复-移除检查 = 否。即 **recovery/removal 检查是"开"的**。
- 什么是 recovery/removal？它们是**异步控制信号**（如异步复位 `reset_n`）相对时钟沿的时序检查：
  - **recovery time（恢复时间）**：异步信号在时钟沿**之前**必须保持稳定的最小时间——类比 setup。
  - **removal time（移除时间）**：异步信号在时钟沿**之后**必须保持稳定的最小时间——类比 hold。

\[
t_{\text{recovery}} \;\leftrightarrow\; t_{\text{setup}}\;(\text{沿前}), \qquad
t_{\text{removal}} \;\leftrightarrow\; t_{\text{hold}}\;(\text{沿后})
\]

- 开启它 = `place_opt` 在优化时**会把异步复位/置位路径也当作关键路径来照顾**。若关掉（设 true），这些路径不检查，异步释放时可能出寄存器误翻转的隐患。

**② `time.disable_case_analysis -value false`**

- 字面：关闭 case 分析 = 否。即 **case 分析是"开"的**。
- 什么是 case 分析？给某些引脚设固定逻辑值（例如把 `test_mode` 脚设为 0），并把这个常量在逻辑里**向前传播**，凡是因该常量而"永远不可能激活"的时序弧（timing arc），就**不计入时序分析**。
- 开启它 = 时序分析更准（剔除了不可能走通的假路径），`place_opt` 不会把优化预算浪费在永远走不到的路径上。

**③ `place.coarse.continue_on_missing_scandef -value true`**

- 粗放布局时，如果**没有扫描链定义（scandef）**，**继续**布局而不是报错停。scandef 描述 DFT 扫描链里寄存器的串联顺序；缺了它，布局就**不遵守扫描链顺序约束**（同链寄存器不会特意摆近），但流程能跑通。

**④ `opt.common.user_instance_name_prefix -value place`**（仅参考脚本）

- `place_opt` 在优化时会**新插入**单元（buffer、反相器、尺寸变体等）。这个选项给所有新单元的名字加 `place` 前缀。结果：流程跑完后，你一眼就能认出"这个单元是布局阶段加的"，CTS/routing 阶段加的则各有各的前缀——便于 ECO 和审计。

**⑤ `time.delay_calculation_style auto`**（仅参考脚本）

- 从 floorplan 评估时的 `zero_interconnect`（u4-l2，假设线延迟为 0）切回 `auto`（用真实估计的寄生算延迟）。要真优化时序，就不能再假设连线没延迟。

**⑥ `set_voltage 0.95`**（仅参考脚本，技术上是另一条命令，但同样在 place_opt 前）

- 设 0.95V 作为延迟计算的工作电压——对应 MCMM 的 **slow 角**（u4-l1 的 ss 工艺角）。电压越低，单元延迟越大、setup 越紧张，所以这是"最坏情况"电压。

#### 4.3.2 核心流程

1. 设 `disable_recovery_removal_checks=false` → 开 recovery/removal 检查。
2. 设 `disable_case_analysis=false` → 开 case 分析。
3. 设 `continue_on_missing_scandef=true` → 缺 scandef 也继续。
4. （参考）设 `user_instance_name_prefix=place` → 新单元加前缀。
5. （参考）设 `delay_calculation_style=auto` + `set_voltage 0.95` → 用真实延迟、最坏电压。
6. `place_opt` 按上述口径优化。

#### 4.3.3 源码精读

**两脚本共享的 3 个选项**（极简模板）：

```tcl
set_app_options -name time.disable_recovery_removal_checks -value false
set_app_options -name time.disable_case_analysis -value false
set_app_options -name place.coarse.continue_on_missing_scandef -value true
```

来自 [IC Compiler II/PnR.tcl:L138-L140](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L138-L140)：开 recovery/removal 检查、开 case 分析、缺 scandef 时粗放布局继续。

**参考脚本额外加的 3 项**（命名前缀 + 延迟方式 + 电压）：

```tcl
set_app_options -name opt.common.user_instance_name_prefix -value place
set_app_options -list {time.delay_calculation_style auto}
set_voltage 0.95
```

来自 [IC Compiler II/Scripts/03_PnR_setup.tcl:L329-L332](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L329-L332)：新单元加 `place` 前缀、延迟计算切回 `auto`、工作电压设 0.95V（slow 角）。

#### 4.3.4 代码实践（对应本讲指定任务）

**实践目标**：列出 placement 阶段前调用的 `set_app_options`，并解释它们对**时序检查 / 恢复-移除检查**的影响。

**操作步骤（阅读 + 归因型）**：

1. 打开 [IC Compiler II/PnR.tcl:L138-L140](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L138-L140) 和 [IC Compiler II/Scripts/03_PnR_setup.tcl:L326-L332](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L326-L332)。
2. 填下面这张"选项 → 取值 → 对时序/恢复-移除的影响"表：

| 选项名 | 取值 | 对时序 / 恢复-移除检查的影响 |
| --- | --- | --- |
| `time.disable_recovery_removal_checks` | `false` | **开启** recovery/removal 检查（双重否定）。`place_opt` 会把异步复位/置位路径也纳入时序优化，避免异步释放时寄存器误翻转。 |
| `time.disable_case_analysis` | `false` | **开启** case 分析。常量引脚传播后，关掉不可能激活的时序弧，时序更准、优化不浪费在假路径上。 |
| `place.coarse.continue_on_missing_scandef` | `true` | 缺 scandef 时粗放布局继续。**不直接改时序**，但影响扫描链寄存器是否被摆近（间接影响扫描路径长度）。 |
| `opt.common.user_instance_name_prefix` | `place` | 仅命名，**不影响时序**。便于追溯新插入单元的来源阶段。 |
| `time.delay_calculation_style` | `auto` | 用真实估计寄生算延迟（区别于 floorplan 的 zero_interconnect），`place_opt` 据此优化。 |
| `set_voltage 0.95` | 0.95V | slow 角电压，单元延迟更大、setup 更紧——在最坏情况下优化时序。 |

3. **重点回答**：如果把 `time.disable_recovery_removal_checks` 改成 `true` 会怎样？

**预期结果**：改成 `true` = 关闭 recovery/removal 检查。`place_opt` 将**不再约束异步复位/置位的释放时序**——异步信号在时钟沿附近抖动时，寄存器可能进入亚稳态或误翻转。这是功能隐患，签核时绝不允许；所以脚本里**特意设 `false` 把它打开**。同理 `disable_case_analysis=false` 也是为了时序分析别漏判、别误判。这是**阅读 + 推理型实践**，结论可从选项名与取值的"双重否定"直接推出，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`time.disable_recovery_removal_checks` 设 `false`，到底是"开"还是"关"恢复-移除检查？为什么这种写法容易看反？

**答案**：是**开**（启用）。选项名是"disable...checks"（关闭检查），取值 `false`（否），"否决了关闭" = 开启。这种"双重否定"是 Synopsys app option 的常见命名习惯，读时要先把名字和取值合起来理解，别被 `disable` 误导。

**练习 2**：为什么 `set_voltage 0.95` 要和 MCMM 的 slow 角搭配？用 fast 角的电压会怎样？

**答案**：0.95V 是 slow 角（ss）的低压工作点，单元最慢、setup 最紧——在最坏情况下优化并签核，才能保证所有工艺角都满足。若改用 fast 角的高压（如 1.25V），单元显得很快、slack 偏乐观，可能"看起来过了"但流片后在 slow 角违例。所以布局/签核盯的是最坏角。

---

### 4.4 QoR 与利用率报告

#### 4.4.1 概念说明

布局跑完，怎么知道"摆得好不好"？靠两份报告：

**① QoR（Quality of Results）—— `report_qor`**

QoR 是一页"成绩单"，最关心的是时序数字。核心两个指标：

\[
\text{slack} \;=\; t_{\text{required}} - t_{\text{arrival}}
\]

某条路径的 slack = 要求到达时间 − 实际到达时间。slack ≥ 0 为满足，slack < 0 为违例。两个聚合指标：

- **WNS（Worst Negative Slack）**：所有端点里**最差**的那个 slack。它是"离达标最远的那条路径"。
- **TNS（Total Negative Slack）**：所有违例路径的负 slack **求和**（只加负的）。它衡量"违例的总量"。

布局后看 WNS/TNS：若 WNS 已接近 0（如 −0.05ns），说明时序基本收敛，可进 CTS；若 WNS 仍很负（如 −0.5ns），说明布局/逻辑还有大问题，要么退回 floorplan 加面积、要么退回综合改逻辑。

**② 利用率报告 —— `report_utilization`**

\[
\text{utilization} \;=\; \frac{A_{\text{std\_cells\_placed}}}{A_{\text{placement\_area}}}
\]

报告当前实际摆下的标准单元面积占可摆放面积的比例，常按区域细分。布局后利用率若远高于 floorplan 预估（如冲到 90%+），说明太挤、拥塞和合法化违例风险大；若很低，说明面积浪费。这是 u4-l2 利用率概念的"实测值"。

> **对照**：极简模板只调了 `report_placement`（[L143](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L143-L143)，布局状态摘要），**没有**把 QoR 和利用率落盘；参考脚本则把 `report_qor` 和 `report_utilization` 重定向成文件存档，便于跨阶段比对。

#### 4.4.2 核心流程

1. `place_opt` + 合法化完成。
2. `report_qor` 看 WNS/TNS（+面积/单元数）。
3. `report_utilization` 看实测利用率。
4. 据 WNS/TNS 与利用率决定：进 CTS，还是退回 floorplan/综合。

#### 4.4.3 源码精读

**参考脚本把报告写进文件**：

```tcl
report_qor > ./report/placement/qor.rpt
report_utilization > ./report/placement/utilization.rpt
```

来自 [IC Compiler II/Scripts/03_PnR_setup.tcl:L337-L338](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L337-L338)：`>` 是 Tcl 的标准输出重定向——把 `report_qor` 的结果写进 `./report/placement/qor.rpt`，利用率写进 `utilization.rpt`。这两个目录/文件需事先存在，否则重定向会失败（**待本地验证**仓库是否提供该目录）。

**极简模板只有一行布局摘要**：

```tcl
report_placement
```

来自 [IC Compiler II/PnR.tcl:L143-L143](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L143-L143)：`report_placement` 打印布局状态（合法/非法统计、单元分布等），功能上接近但不如 `report_qor` 专注时序。模板在流程最末尾（[L186-L189](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L186-L189)）才统一出 `report_design -all` / `report_timing` / `report_power`，布局阶段不留中间报告。

#### 4.4.4 代码实践

**实践目标**：给极简模板补上布局阶段的 QoR/利用率落盘，体会"留中间报告"的价值。

1. 读 [IC Compiler II/Scripts/03_PnR_setup.tcl:L337-L338](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L337-L338) 这两条重定向。
2. 在自己的副本上（**示例代码**，请勿改仓库源文件），给 [IC Compiler II/PnR.tcl:L143](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L143-L143) 的 `report_placement` 后面加：

   ```tcl
   # 示例代码：布局阶段留档 QoR 与利用率
   report_qor > ./report/placement/qor.rpt
   report_utilization > ./report/placement/utilization.rpt
   ```

3. 思考：为什么要在**每个阶段**（floorplan / placement / CTS / route）各存一份 QoR，而不是只存最终一份？

**预期结果**：有了每阶段的 QoR，你能画出 WNS 随阶段变化的曲线——比如布局后 WNS=−0.3，CTS 后变 −0.1，routing 后 +0.05——一眼看出"时序是在哪个阶段收敛的""哪个阶段把时序拖坏了"。只存最终一份则丢失这条诊断线索。是否真能生成报告**待本地验证**（依赖库、网表与报告目录）。

#### 4.4.5 小练习与答案

**练习 1**：布局后 WNS = −0.45ns，TNS = −120ns。这两个数分别说明什么？下一步该退回哪里？

**答案**：WNS=−0.45 说明**最差路径**还差 0.45ns 才达标；TNS=−120 说明违例**总量**很大（很多路径都违例）。布局阶段就差这么多，通常不是布线能救的——先看零互连时序（u4-l2 的尺子）区分是逻辑慢还是布线慢：逻辑慢退回综合，布线/拥塞慢退回 floorplan 加面积或重摆宏单元。

**练习 2**：`report_qor` 与 `report_utilization` 一个看时序、一个看面积密度，为什么布局后两个都要看？

**答案**：时序和面积是一对矛盾——挤一点（利用率高）面积省但时序差、拥塞重；松一点时序好但面积贵。只看时序可能忽略"太挤导致合法化违例/拥塞"；只看利用率可能忽略"WNS 还很差"。两个一起看才能判断布局质量并决定下一步。

---

### 4.5 save_block 与 block 版本演进

#### 4.5.1 概念说明

这是 u4-l2 已埋下伏笔、本讲正式展开的模式。`03_PnR_setup.tcl` 把整个 PnR 流程切成 **setup → floorplan → power → placement → CTS → route → finish** 七段，每段都遵循同一个"四步范式"：

```tcl
copy_block  -from_block <上一阶段>.design -to <本阶段>.design   ; # 1. 从上一阶段拷贝出新块
current_block <本阶段>.design                                   ; # 2. 切到新块干活
... 本阶段命令 ...                                               ; # 3. 干活
save_block                                                      ; # 4. 存档
```

这样就形成一条**版本链**：

\[
\text{init\_design} \;\to\; \text{floorplan\_design} \;\to\; \text{power\_plan\_1} \;\to\; \boxed{\text{placement}} \;\to\; \text{cts} \;\to\; \text{route} \;\to\; \text{finish}
\]

本讲的 placement 段，就是从 `power_plan_1.design`（u4-l3 电源网络的产物）拷贝出 `placement.design` 来干活。

**为什么这么做？** 三个好处：

- **原块只读**：每阶段都在**拷贝**上干活，上一阶段的成品（如 `power_plan_1`）不被破坏。布局跑坏了，`power_plan_1` 完好无损，重拷一份再试即可。
- **可任意回退**：任何阶段出问题，`open_block` / `copy_block` 回到链上任一节点重跑，不必从 setup 起步。
- **可审计**：每个具名块就是一次"阶段快照"，配合每阶段的 QoR 报告（4.4），整条流程可追溯。

> **诚实对照（与 u4-l2 一致）**：极简模板 `PnR.tcl` **完全没有**这套 copy/current/save 的版本链——它从头到尾在一个块里跑，只在最末尾 `save_block -as "${TOP_DESIGN}_Final"` 存一次（[L190](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L190-L190)）。也就是说，模板把"每阶段快照"这个工程好习惯省略了；要看正确的版本管理，得读 `03_PnR_setup.tcl`。

#### 4.5.2 核心流程

1. `copy_block -from_block power_plan_1.design -to placement.design`：从电源网络完成块拷贝。
2. `current_block placement.design`：切到新块。
3. 跑布局（set 选项 → place_opt → legalize → check → report）。
4. `save_block`：存 `placement.design`，供下一阶段 CTS 拷贝。

#### 4.5.3 源码精读

**placement 段的版本链入口**：

```tcl
puts "start_place"
copy_block -from_block power_plan_1.design -to placement.design
current_block placement.design
... 布局命令 ...
save_block
```

来自 [IC Compiler II/Scripts/03_PnR_setup.tcl:L322-L324](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L322-L324) 与 [L340-L340](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L340-L340)：`copy_block` 把 u4-l3 完成的 `power_plan_1.design` 整块复制成 `placement.design`；`current_block` 把后续命令的目标切到新块；布局完 `save_block` 存档。

**与上下游阶段的衔接**——往前看电源阶段怎么交班：

```tcl
copy_block -from_block pit_top.dlib:pit_top/floorplan_design.design -to_block power_plan_1
current_block power_plan_1.design
...
save_block
```

来自 [IC Compiler II/Scripts/03_PnR_setup.tcl:L219-L220](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L219-L220)（电源段入口）与 [L318-L318](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L318-L318)（电源段存档）：电源段从 `floorplan_design` 拷出 `power_plan_1`，干完 `save_block`，正好交给本讲 placement 段做 `copy_block` 的源。往后看，CTS 段又从 `placement.design` 拷出 `cts.design`（[L345](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L345-L345)）——一环扣一环。

**极简模板的唯一存档**（对比）：

```tcl
save_block -as "${TOP_DESIGN}_Final"
```

来自 [IC Compiler II/PnR.tcl:L190-L190](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L190-L190)：模板只在流程最末存一个 `*_Final`，布局阶段不留中间快照——便于通读，但不利于工程回退。

#### 4.5.4 代码实践

**实践目标**：把"四步范式"套到 placement 段，画出整条版本链。

1. 读 [IC Compiler II/Scripts/03_PnR_setup.tcl:L322-L340](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L322-L340)，确认 placement 段是"copy_block → current_block → 干活 → save_block"四步。
2. 在脚本里搜索每个阶段的 `copy_block -from_block ...`，补全版本链：

   ```
   init_design  (L69-71 存)
        ↓ copy
   floorplan_design (L80 / L210 存)
        ↓ copy
   power_plan_1 (L219 / L318 存)
        ↓ copy
   placement (L323 / L340 存)   ← 本讲
        ↓ copy
   cts (L345 / L430 存)
        ↓ copy
   route (L435 / L477 存)
        ↓ copy
   finish (L481 / L494 存)
   ```

3. 思考：若 layout 阶段（placement）跑出一个糟糕的结果，你想回到电源网络完成点重试，该用哪条命令？

**预期结果**：用 `copy_block -from_block power_plan_1.design -to placement_try2.design` 重新拷贝一份再跑——`power_plan_1` 原块不受影响。这就是版本链的核心价值：**任何阶段失败都不污染上游成品**。具体命令名/块名是否与你的环境一致**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`copy_block` + `current_block` 两步，能不能合并成一步？为什么参考脚本要分开写？

**答案**：`copy_block` 只负责"复制并产生新块"，并不自动把后续命令的目标切过去；`current_block` 才是把"当前工作块"切到新块。分开写语义清晰、不易出错——少了 `current_block` 的话，后面的 `place_opt` 可能仍跑在旧块上，白干还污染上游。某些工具/写法允许带切换的拷贝，但参考脚本选择显式两步，更安全。

**练习 2**：极简模板 `PnR.tcl` 全程在一个块里跑、最后才 `save_block -as *_Final`，这样做的代价是什么？

**答案**：丢掉了**中间回退点**。一旦 CTS 或 routing 出问题，无法回到"布局完成"或"电源完成"的状态重试，只能从 setup 重跑整条流程，耗时巨增；也无法做"每阶段 QoR 曲线"这种跨阶段诊断。模板是为"好读"牺牲了"工程健壮性"。

---

## 5. 综合实践

**任务**：以 placement 阶段为靶子，做一次"读模板 → 借参考补全 → 判时序 → 接版本链"的小演练，把本讲五个模块串起来。

1. **读极简模板**：通读 [IC Compiler II/PnR.tcl:L137-L143](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L137-L143)，确认它的布局只有"3 个 set_app_options + place_opt + legalize_placement + report_placement"。标注它**缺**了什么：没有 `check_legality`、没有 QoR/利用率落盘、没有 `copy_block`/`save_block` 版本链、没有 `set_voltage` 与命名前缀。
2. **借参考补全**：从 [IC Compiler II/Scripts/03_PnR_setup.tcl:L319-L340](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L319-L340) 把缺的部分"翻译"到模板对应位置（写在你的副本上，**不要改仓库源文件**）：
   - 在 `place_opt` 前补 `opt.common.user_instance_name_prefix -value place`、`time.delay_calculation_style auto`、`set_voltage 0.95`；
   - 在 `legalize_placement` 后补 `check_legality`（验收）；
   - 补 `report_qor > ./report/placement/qor.rpt` 与 `report_utilization > ./report/placement/utilization.rpt`；
   - 用 `copy_block`/`current_block`/`save_block` 把布局段包起来，从电源完成块接力。
3. **判时序**（对应指定任务的核心）：列出布局前后所有 `set_app_options`，重点解释 `time.disable_recovery_removal_checks=false` 开启了恢复-移除检查（异步复位/置位的 setup/hold 类约束）、`time.disable_case_analysis=false` 开启了 case 分析（剔除假路径），并说明若把它们改成 `true` 会带来的签核风险。
4. **接版本链**：在补全后的脚本里画一条从 `power_plan_1 → placement → cts` 的版本链箭头，标出每一步的 `copy_block` 与 `save_block` 行号。

> 这是**源码阅读 + 流程设计型**实践，不要求真跑通 ICC2（缺库与网表）；重点是说清"模板缺什么、参考脚本补什么、每个选项对时序/恢复-移除检查意味着什么"。

---

## 6. 本讲小结

- **place_opt** 是 ICC2 placement 的总命令，内部跑"粗放布局 → 时序优化（buffer 插入/尺寸优化）→ 合法化"的迭代；它与 floorplan 阶段"只摆不优化"的 `create_placement` 相对，是真正的时序驱动布局。
- **legalize_placement 是修复型**（动手消除重叠/越位），**check_legality 是检查型**（只出体检报告）；参考脚本"先修后查"，模板只修不查。
- 布局前的 `set_app_options` 决定优化口径：`time.disable_recovery_removal_checks=false` **开启**异步信号的恢复-移除检查，`time.disable_case_analysis=false` **开启** case 分析剔除假路径，`place.coarse.continue_on_missing_scandef=true` 缺 scandef 时继续；参考脚本还加了 `user_instance_name_prefix=place`（新单元命名）、`delay_calculation_style=auto`（真实延迟）、`set_voltage 0.95`（slow 角电压）。
- **report_qor** 看 WNS/TNS 判时序收敛，**report_utilization** 看实测利用率判拥挤程度；参考脚本把两者落盘到 `./report/placement/`，模板只有一行 `report_placement`。
- **block 版本演进**：`03_PnR_setup.tcl` 每段都"copy_block → current_block → 干活 → save_block"，形成 init→floorplan→power→placement→cts→route→finish 的版本链；placement 段从 `power_plan_1.design` 拷贝、存为 `placement.design` 交给 CTS。极简模板省略了这条链，只在最末存一个 `*_Final`。

---

## 7. 下一步学习建议

- 布局完成、`check_legality` 干净后，下一步是给芯片"搭时钟树"——**u4-l5 时钟树综合 CTS**（`clock_opt`、`set_clock_tree_options`、NDR 路由规则、clock uncertainty）。届时你会看到 placement 阶段设的 `time.remove_clock_reconvergence_pessimism` 等选项如何承接进 CTS。
- 想理解为什么布局后还要单独看 QoR，可对比 [IC Compiler II/Scripts/03_PnR_setup.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl) 里 placement（L337）与 CTS（L352）、route（L473）三个阶段的 `report_qor`，体会 WNS 随阶段的收敛曲线。
- 关于 recovery/removal 与 case analysis 的更底层原理，可回看 u2-l3（SDC 时序约束）里 setup/hold 与 launching/capturing register 的定义——它们是同一套时序检查体系在不同信号类型上的体现。
