# 布线 Routing

## 1. 本讲目标

学完本讲，你应该能够：

- 说清「布线（Routing）」在 ICC2 后端流程中的位置——它紧跟 CTS 之后、收尾与输出之前。
- 用一句话讲清布线内部的三个子阶段：global（全局）→ track（轨道分配）→ detail（详细）布线。
- 区分 `route_auto`（初次布线）与 `route_opt`（布线 + 时序/DRC 优化迭代）的分工。
- 看懂并对比 `route.global` / `route.track` / `route.detail` 三类 app 选项（timing_driven / crosstalk_driven）。
- 解释布线前为何要做 `check_design -checks pre_route_stage` 和 `connect_pg_net`。
- 用 `set_ignored_layers` 限定布线层范围，用 `check_routes` 查 DRC。

## 2. 前置知识

承接 **u4-l5（CTS）**：时钟树已建好，时钟由 ideal（理想直通）切换为 propagated（真实传播），`clock_opt` 跑完。此时芯片上的标准单元都已合法摆好、时钟网也接好了，但**绝大多数信号线还是「虚拟」的**——工具只是「记住了 A 要连到 B」，并没有真正用金属把它们画出来。布线就是把这些逻辑连接变成实实在在的金属走线。

承接 **u3-l1（库与物理数据）**：你需要记得金属层栈的概念——相邻层水平/垂直正交交替（本仓库极简 `PnR.tcl` 与完整 `03_PnR_setup.tcl` 两套约定方向相反，详见 u3-l1/u4-l1），以及 `MIN_ROUTING_LAYER`/`MAX_ROUTING_LAYER` 把信号布线限定在 metal1–metal10。

两个直觉概念：

- **global routing（全局布线）**：把芯片切成一个个方格（GCell），只决定每条线网「走哪几个格子、用哪层金属」，不画精确坐标。快，但只是草图。它在 floorplan 阶段还被借用来画拥塞图（u4-l2 的 `route_global -congestion_map_only`）。
- **detail routing（详细布线）**：在轨道上画出每段金属的确切坐标、打孔（via），保证不短路、不违反设计规则（DRC）。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| `IC Compiler II/PnR.tcl` | 极简模板，布线段只有 `remove/set_ignored_layers` + `route_opt` 一行流（L170–L175）。 |
| `IC Compiler II/Scripts/03_PnR_setup.tcl` | 完整参考模板，Section 6 Routing（L432–L477）含 pre-route 检查、PG 连接、`route.global/track/detail` 选项、`route_auto`/`check_routes`/`route_opt`。 |
| `IC Compiler II/Scripts/01_common_setup.tcl` | 定义 `MIN_ROUTING_LAYER`(metal1)、`MAX_ROUTING_LAYER`(metal10)（L25–L26）。 |

> 本仓库有「极简 `PnR.tcl`」与「完整 `03_PnR_setup.tcl`」两套布线写法。极简版省略了 pre-route 检查、PG 重连、路由器调优选项和 `check_routes`，是教学骨架；完整版才是生产级流程。本讲以完整版讲概念，同时标注极简版缺了什么。

## 4. 核心概念与源码讲解

### 4.1 布线的全局视图：主命令与三段子阶段

#### 4.1.1 概念说明

布线（Routing）回答一个问题：**「逻辑上要连的 A 和 B，用哪条金属路径、在哪些层、打几个孔把它们物理连通？」** 这一步从 CTS 接过接力棒，把「逻辑连接表」落实成「版图金属」。

ICC2 把布线拆成三个子阶段，由粗到细：

1. **global routing（全局布线）**：在 GCell 网格上规划每条线网的「大致走向 + 用哪层金属」，求解的是「容量分配」问题，不碰精确坐标。
2. **track routing（轨道分配/布线）**：把全局结果落实到具体的布线轨道（track）上，决定每条线占用哪些轨道、在哪里换层打孔。
3. **detail routing（详细布线）**：逐段画金属、打 via、修小违规，产出符合设计规则的具体几何形状。

三段之后通常还有 **DRC 修复 / 优化迭代**，把短路、间距违规、antenna 等清掉。

ICC2 提供两个总命令来驱动这套流程：

- `route_auto`：**自动走完 global → track → detail 三段**，做「初次布线」，不做或少做优化。
- `route_opt`：**布线 + 时序/DRC 优化**的迭代器，可在已有布线上继续打磨（也能从零开始跑完整三段 + 优化）。它是 `place_opt`/`clock_opt` 的同款命名风格（u4-l4/u4-l5）。

> 直觉：`route_auto` 像「先把所有线一次性连通，别管好不好看」；`route_opt` 像「在连通的基础上反复修，让时序更好、DRC 更干净」。

#### 4.1.2 核心流程

完整布线段的执行顺序（对应 `03_PnR_setup.tcl` Section 6）：

```
copy_block cts.design → route.design       # 从 CTS 结果开一个布线副本
  ↓
check_design -checks pre_route_stage       # 布线前体检
connect_pg_net (cells + ports)             # 把电源/地逻辑网接上
  ↓
设置 route.global / route.track / route.detail 选项   # 三段路由器调优
  ↓
route_auto                                 # 初次布线：global→track→detail
check_routes                               # 查 DRC/连接性
route_opt                                  # 布线 + 优化迭代
report_qor -summary                        # 看时序结果
  ↓
save_block                                 # 存快照，交给收尾(finishing)
```

而极简 `PnR.tcl` 把中间一大段几乎全砍了，只剩层范围声明 + `route_opt`。

#### 4.1.3 源码精读

**极简版（`PnR.tcl`）**——布线段只有一个 `route_opt`：

[IC Compiler II/PnR.tcl:L170-L175](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L170-L175)：先 `remove_ignored_layers -all` 清空旧的忽略层设置，再用 `set_ignored_layers` 限定信号布线只能用 `$MIN_ROUTING_LAYER`～`$MAX_ROUTING_LAYER` 之间的层，最后一句 `route_opt` 把布线 + 优化全做完。

```tcl
remove_ignored_layers -all
set_ignored_layers \
 -min_routing_layer $MIN_ROUTING_LAYER \
 -max_routing_layer $MAX_ROUTING_LAYER
route_opt
```

注意：极简版**没有** `route_auto`、**没有** `check_routes`、**没有** pre-route 的 `check_design`，全靠 `route_opt` 一条命令内部走完三段 + 优化。这是教学用的「最短布线」。

**完整版（`03_PnR_setup.tcl`）**——Section 6 Routing 的开头：

[IC Compiler II/Scripts/03_PnR_setup.tcl:L432-L441](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L432-L441)：从 `cts.design` 拷出 `route.design` 并 `current_block` 切过去，`puts "start_route"` 打印进度，先 `report_qor -summary` 留一份布线前的时序基线。

```tcl
copy_block -from_block cts.design -to route.design
current_block route.design
puts "start_route"
report_qor -summary
```

这里体现了 u4-l1 讲过的 **block 版本管理** 范式：`copy_block → current_block → 干活 → save_block`，每阶段一个独立副本。布线在 `route.design` 上做，CTS 的 `cts.design` 原封不动保留，出问题可随时回退。

#### 4.1.4 代码实践

**实践目标**：对比极简版与完整版的「布线骨架」差异，找出极简版省略了哪些步骤。

**操作步骤**：

1. 打开 `IC Compiler II/PnR.tcl`，定位 L170–L175 的 `route_opt` 段，数一数这段一共有几条有效命令。
2. 打开 `IC Compiler II/Scripts/03_PnR_setup.tcl`，定位 L432–L477 的 Section 6 Routing，列出完整版在 `route_opt` **之前**调用的所有命令。
3. 做一张「极简 vs 完整」对照表，标出完整版多了哪几类操作。

**需要观察的现象**：极简版的布线段只有 3 条核心命令（`remove_ignored_layers`、`set_ignored_layers`、`route_opt`）；完整版多出 `check_design -checks pre_route_stage`、`connect_pg_net`、6 条 `route.*` 选项、`route_auto`、`check_routes`。

**预期结果**：你会得到一张类似下面的对照表——

| 步骤 | 极简 PnR.tcl | 完整 03_PnR_setup.tcl |
|---|:---:|:---:|
| pre-route check_design | ✗ | ✓ |
| PG 连接 | ✗（依赖前阶段） | ✓ |
| route.global/track/detail 选项 | ✗（用默认） | ✓（6 条） |
| route_auto | ✗ | ✓ |
| check_routes | ✗ | ✓ |
| route_opt | ✓ | ✓ |

> 待本地验证：若你在真实 ICC2 环境里分别跑这两个脚本，完整版通常布线 DRC 更干净、时序更好，但耗时更长。

#### 4.1.5 小练习与答案

**练习 1**：`route_auto` 和 `route_opt` 都能布线，为什么完整版要「先 `route_auto` 再 `route_opt`」而不是只跑 `route_opt`？

> **参考答案**：`route_auto` 专注把线「先连通」（global→track→detail 一次过），快且稳定；`check_routes` 在连通后立刻暴露 DRC/连接性问题；`route_opt` 再在这个已连通的基础上做时序 + DRC 优化迭代。分开走相当于「先搭骨架再精修」，比让一个命令同时背负「连通 + 优化」更容易收敛、也更容易定位问题。

**练习 2**：global routing 在 u4-l2（floorplan）和本讲（routing）里都出现过，两次有什么不同？

> **参考答案**：floorplan 阶段用的是 `route_global -congestion_map_only true`，**只画拥塞图、不落地真实布线**，目的是评估布图是否走得通；本讲 routing 阶段的 global routing 是 `route_auto`/`route_opt` 内部的真实一步，要产出最终金属。同样是「全局布线」算法，前者是预演，后者是正戏。

---

### 4.2 布线前的准备：pre-route 检查与 PG 连接

#### 4.2.1 概念说明

布线是整个流程里**计算量最大、最贵**的一步。在按下「布线」按钮前，必须先做两件体检：

1. **pre-route 设计检查**：用 `check_design -checks pre_route_stage` 扫一遍，提前发现会让布线失败或质量恶化的隐患——例如还有单元没合法化、还有线网没连、时钟没 propagated、还有 dont_touch 设置挡路等。提前发现比布到一半崩掉便宜得多。
2. **PG（电源/地）连接**：把逻辑电源网 `VDD`/`VSS` 真正接到每个标准单元和端口的电源管脚上。如果布线前 PG 没接好，信号布线时工具会把电源管脚当成「悬空」处理，导致后续 LVS/连接性一团糟。

> 直觉：pre-route 检查 = 出发前的「车检」；PG 连接 = 确保「每辆车都加了油」。两者都为了不让昂贵的布线白跑。

#### 4.2.2 核心流程

```
set_app_options route.common.verbose_level 1     # 打开详细报告
check_design -checks pre_route_stage             # 体检
set_app_options route.common.verbose_level 0     # 关回静默
report_ignored_layers                            # 看当前哪些层被忽略
update_timing                                    # 重算时序，刷新基线
connect_pg_net -net VDD [get_pins  -hierarchical "*/VDD"]   # 单元电源脚
connect_pg_net -net VSS [get_pins  -hierarchical "*/VSS"]   # 单元地脚
connect_pg_net -net VDD [get_ports -physical_context "*/VDD"]  # 顶层电源端口
connect_pg_net -net VSS [get_ports -physical_context "*/VSS"]  # 顶层地端口
```

#### 4.2.3 源码精读

[IC Compiler II/Scripts/03_PnR_setup.tcl:L446-L456](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L446-L456)：先把 `route.common.verbose_level` 调到 1 让 `check_design` 输出更详细，跑 `check_design -checks pre_route_stage` 做布线前体检，再把冗余度调回 0；接着 `report_ignored_layers` 看当前层范围、`update_timing` 刷新时序；最后两组 `connect_pg_net` 分别把电源/地接到**层次化单元管脚**（`get_pins -hierarchical`）和**顶层物理端口**（`get_ports -physical_context`）。

注意：极简 `PnR.tcl` 在布线段**完全没有**这段体检与 PG 重连——它的 PG 在更早的电源网络阶段（u4-l3 的 `connect_pg_net`）接过一次，布线段直接信任前阶段的结果。这是极简版的另一个「缺口」。

> 小细节：脚本注释（L444–L445）明确写了意图——「check for any issues that might cause problems during routing」「the app option allows for more detailed reporting」，这正是 pre-route 检查的目的。

#### 4.2.4 代码实践

**实践目标**：读懂 pre-route 检查与 PG 连接的调用顺序，理解每条命令的检查对象。

**操作步骤**：

1. 在 `03_PnR_setup.tcl` L446–L456 范围内，按出现顺序抄下每条命令。
2. 给每条命令标注它「检查/连接的对象」：例如 `check_design` 检查的是「设计一致性」，`get_pins -hierarchical` 针对「单元管脚」，`get_ports -physical_context` 针对「顶层端口」。
3. 思考：为什么单元管脚用 `get_pins` 而端口用 `get_ports`？

**需要观察的现象**：两组 `connect_pg_net` 分别面向「内部标准单元」和「芯片边界端口」，覆盖范围不重叠。

**预期结果**：你会得到一张「命令 → 对象」表，并得出结论——PG 连接必须同时覆盖**层次化单元管脚**和**顶层物理端口**，缺一不可；否则要么单元悬空、要么边界供电断开。

#### 4.2.5 小练习与答案

**练习 1**：为什么 pre-route 检查要放在布线**之前**，而不是布线之后？

> **参考答案**：布线极昂贵（耗时与算力最大的阶段）。pre-route 检查能提前暴露「单元未合法化 / 线网未连 / 时钟未 propagated」等问题，花几秒修掉；若等布线跑完几小时再发现这些问题，往往要推倒重来，代价极高。先体检再上路，是降低返工风险的标准做法。

**练习 2**：极简 `PnR.tcl` 布线段没有 `connect_pg_net`，它靠什么保证 PG 是接好的？

> **参考答案**：靠**更早阶段**——电源网络阶段（u4-l3 的 `create_net` + `connect_pg_net`，以及 placement 后 filler 阶段的再次 `connect_pg_net`）已经接过一次。极简模板假设那次连接到布线时仍然有效。但生产流程（完整版）会在布线前**再连一次**，因为中间的 placement/CTS 可能引入新的需要连接的对象。

---

### 4.3 三段路由器的调优选项：route.global / route.track / route.detail

#### 4.3.1 概念说明

4.1 讲过布线分 global → track → detail 三段。ICC2 允许你对**每一段**单独开关两个「驱动力」：

- **timing_driven（时序驱动）**：布线时优先照顾关键路径（slack 最差的路径），让它们走更短、更快的路径，必要时给它们让出更好的资源。开 = 更好的时序，但布线更慢。
- **crosstalk_driven（串扰驱动）**：布线时考虑「相邻信号线之间的耦合电容」引发的串扰（一条线翻转会通过耦合电容扰动邻居，造成延迟变化甚至功能错误）。开 = 更少串扰诱导的延迟尖峰，但布线更慢、更费资源。

串扰的物理本质：两条平行走线之间的耦合电容 \(C_c\) 会把一条线（aggressor，攻击者）的翻转耦合到另一条线（victim，受害者）上，产生 Δdelay。串扰驱动布线会主动给敏感 victim 拉开间距或加屏蔽层，降低 \(C_c\)。

#### 4.3.2 核心流程（三类选项对照）

| 选项 | 本仓设置 | 作用阶段 | 含义 |
|---|---|---|---|
| `route.global.timing_driven` | true | 全局布线 | 全局规划时就优先关键路径 |
| `route.global.crosstalk_driven` | **false** | 全局布线 | 全局阶段不做串扰优化（太早、太贵） |
| `route.track.timing_driven` | true | 轨道分配 | 落轨道时照顾关键路径 |
| `route.track.crosstalk_driven` | **true** | 轨道分配 | 轨道阶段开始做串扰优化 |
| `route.detail.timing_driven` | true | 详细布线 | 画金属时继续照顾时序 |
| `route.detail.force_max_number_iterations` | false | 详细布线 | 不强制压满迭代次数（让其自然收敛） |

观察规律：**timing_driven 全程开**（每个阶段都盯时序）；**crosstalk_driven 只在 track 和 detail 开、global 关**——因为 global 阶段线网位置还很粗，做串扰优化既不准又贵，等轨道定下来（track）再开始处理串扰才划算。这是典型的「先粗后细、逐步引入复杂度」的工程取舍。

#### 4.3.3 源码精读

[IC Compiler II/Scripts/03_PnR_setup.tcl:L459-L464](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L459-L464)：依次设置 6 条 app 选项，把 global/track/detail 三段的 timing 与 crosstalk 驱动逐一打开或关闭——注意 `route.global.crosstalk_driven` 是 `false`，其余 crosstalk/timing 都是 `true`。

```tcl
set_app_options -name route.global.timing_driven    -value true
set_app_options -name route.global.crosstalk_driven -value false
set_app_options -name route.track.timing_driven     -value true
set_app_options -name route.track.crosstalk_driven  -value true
set_app_options -name route.detail.timing_driven    -value true
set_app_options -name route.detail.force_max_number_iterations -value false
```

> 这正是本讲实践任务要对比的「三类 timing/crosstalk 选项」。极简 `PnR.tcl` 没有这 6 条，等于全用 ICC2 默认值。

#### 4.3.4 代码实践（★ 本讲主实践）

**实践目标**：对比 `route.global` / `route.track` / `route.detail` 三类选项中 timing_driven 与 crosstalk_driven 的设置差异，解释为什么 crosstalk 只在后两段开。

**操作步骤**：

1. 打开 `IC Compiler II/Scripts/03_PnR_setup.tcl`，定位 L459–L464 这 6 条 `set_app_options`。
2. 画一张 3×2 的表格：行 = global/track/detail，列 = timing_driven / crosstalk_driven，填入每条的 true/false。
3. 圈出唯一一个 `false`（`route.global.crosstalk_driven`）。
4. 用一句话解释：为什么 global 阶段不开 crosstalk，而 track/detail 都开？

**需要观察的现象**：timing_driven 这一列**全是 true**；crosstalk_driven 这一列是 **false / true / true**——global 关、后两段开。

**预期结果**：你应得出结论——

- timing 贯穿三段都开，因为时序是「始终要盯」的目标。
- crosstalk 在 global 关掉，因为 global 阶段线网只有「格子级」位置、耦合关系还没定准，此时做串扰优化既不精确又拖慢全局规划；等 track 阶段线网落到具体轨道、相邻关系确定后，再开 crosstalk 才有意义且高效。

> 待本地验证：在真实设计上，若把 `route.global.crosstalk_driven` 也改成 true，布线时间通常明显增加，但最终串扰改善有限——印证了「太早做不划算」。

#### 4.3.5 小练习与答案

**练习 1**：如果设计对串扰极不敏感（比如全同步、翻转很稀疏），你会怎么调这些选项省运行时间？

> **参考答案**：可以把 `route.track.crosstalk_driven` 和 `route.detail.crosstalk_driven` 都设为 false。串扰驱动布线很贵，若设计本身串扰风险低，关掉能显著加快布线、几乎不损失质量。timing_driven 一般仍建议保留。

**练习 2**：`route.detail.force_max_number_iterations = false` 是什么意思？设成 true 会怎样？

> **参考答案**：详细布线器内部会迭代修 DRC，`force_max_number_iterations` 控制是否「强制跑满最大迭代次数」。设 false 表示让其按收敛情况**自然停止**（修干净了就停，省时间）；设 true 会**压满迭代**，可能多修掉一些边缘违规，但耗时更长。模板选 false 是「够好就停」的实用策略。

---

### 4.4 层范围控制与 DRC 检查：set_ignored_layers、check_routes

#### 4.4.1 概念说明

**忽略层（ignored layers）**：芯片有十几层金属，但不是每层都该让信号线随便用。比如顶层金属（metal9/metal10）常留给电源 mesh（u4-l3），MRDL 留给顶层 RDL 重布线。用 `set_ignored_layers -min_routing_layer`/`-max_routing_layer` 给信号布线画一个「可用区间」：低于 min 或高于 max 的层，信号线不准用。

`remove_ignored_layers -all` 先清空一切旧设置（避免前阶段残留的忽略层干扰），再用 `set_ignored_layers` 重新声明干净的区间——这是「先清后设」的标准套路。

布线完成后，要用 **DRC（Design Rule Check，设计规则检查）** 验证：金属间距、宽度、短路、开路、via 数量等是否都满足代工厂规则。`check_routes` 是 ICC2 专门查「布线结果」的命令——报告还有多少 DRC 违规、多少线网未连、多少 open/short。理想结果是 `check_routes` 干净（0 违规），否则要回到 `route_opt` 修。

DRC 与拥塞的关系可以用一个比例感受：某区域若

\[
r = \frac{\text{该区域需要布的线网轨道数}}{\text{该区域可用的轨道数}} > 1
\]

就会 overflow，detail 布线器塞不下，产生 DRC 间距/短路违规。这正是 floorplan 阶段用 `route_global -congestion_map_only` 提前盯拥塞的原因（u4-l2）。

#### 4.4.2 核心流程

```
remove_ignored_layers -all                                   # 先清空
set_ignored_layers -min_routing_layer $MIN_ROUTING_LAYER \   # metal1
                    -max_routing_layer $MAX_ROUTING_LAYER     # metal10
... route_auto / route_opt ...                               # 布线
check_routes                                                 # 查 DRC/连接性
# 若有违规 → route_opt 再修 → check_routes 再查，直到干净
```

#### 4.4.3 源码精读

**极简版层范围声明**：[IC Compiler II/PnR.tcl:L171-L174](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L171-L174)：`remove_ignored_layers -all` 清空，`set_ignored_layers` 用 `$MIN_ROUTING_LAYER`/`$MAX_ROUTING_LAYER` 限定区间。这两个变量定义在 `01_common_setup.tcl`：

[IC Compiler II/Scripts/01_common_setup.tcl:L25-L26](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl#L25-L26)：`MIN_ROUTING_LAYER = metal1`、`MAX_ROUTING_LAYER = metal10`——信号布线允许用 metal1 到 metal10，顶层与底层之外不让用。

> 注意：极简 `PnR.tcl` 第 2 行 `source ./input/common_setup.tcl` 引用的是仓库里**不存在**的 `./input/common_setup.tcl`（本仓库只有 `Scripts/01_common_setup.tcl` 与 `PrimeTime/common_setup.tcl`），所以 `$MIN_ROUTING_LAYER` 等变量的真实定义要回到 `Scripts/01_common_setup.tcl` 找——这是模板「依赖文件缺失」的又一处体现（u1-l3 已提醒）。

**完整版布线主流程与 DRC 检查**：[IC Compiler II/Scripts/03_PnR_setup.tcl:L466-L473](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L466-L473)：`route_auto` 初次布线 → `check_routes` 查 DRC → `route_opt` 优化 → `report_qor -summary` 看时序。

```tcl
route_auto
check_routes
route_opt
report_qor -summary
```

`check_routes` 夹在 `route_auto` 和 `route_opt` 之间，作用是「先布完、查一遍、暴露问题、再让 route_opt 有针对性地修」。极简 `PnR.tcl` 既没有 `route_auto` 也没有 `check_routes`，DRC 全靠 `route_opt` 内部隐式处理。

#### 4.4.4 代码实践

**实践目标**：理清「先清后设」的层范围声明套路，以及 `check_routes` 在流程里的位置。

**操作步骤**：

1. 在 `PnR.tcl` L171–L174 确认 `remove_ignored_layers -all` 出现在 `set_ignored_layers` **之前**，体会「先清后设」。
2. 在 `03_PnR_setup.tcl` L466–L473 确认 `check_routes` 出现在 `route_auto` 之后、`route_opt` 之前。
3. 假设 `check_routes` 报告还有 12 个 DRC 违规，写出你会追加的修复命令（提示：再跑一次 `route_opt`，再 `check_routes`）。

**需要观察的现象**：`check_routes` 不是布线的终点，而是「布完初版 → 查 → 修」循环的中间检查点。

**预期结果**：你会写出类似「`route_opt` → `check_routes`（循环直到干净）→ `report_qor -summary`」的收尾套路，并理解极简版把这个循环全压进了 `route_opt` 内部。

#### 4.4.5 小练习与答案

**练习 1**：为什么不直接让信号布线用所有金属层（不设 min/max）？

> **参考答案**：① 顶层金属（metal9/10）要留给电源 mesh，若被信号线占用会和电源网络争资源、抬高 IR drop；② 底层（如 metal1）常用于标准单元内部 rail，不适合长距离信号；③ 某些特殊层（MRDL 重布线层）有专门用途。设 min/max 是把「信号层」和「电源/特殊层」划清边界，保护关键金属资源。

**练习 2**：`check_routes` 报告有违规，但 `route_opt` 跑完仍清不掉，下一步该怀疑什么？

> **参考答案**：先怀疑**布图本身太挤**（拥塞 overflow），即某区域 \(r>1\)、轨道根本不够。这通常不是布线器能修的，要退回 floorplan：加大面积、降利用率、加 placement/routing blockage、或调整 ignored layers 放开更多层。布线修不掉的 DRC，根因常在布图。

---

## 5. 综合实践

**任务**：给极简 `PnR.tcl` 的布线段「补全」，让它接近完整版的水平。

背景：`PnR.tcl` L170–L175 的布线段只有 3 条命令，缺了 pre-route 体检、PG 重连、路由器调优选项、`route_auto` 和 `check_routes`。请参照 `03_PnR_setup.tcl` Section 6（L432–L477），为 `PnR.tcl` 设计一段更完整的布线流程。

**操作步骤**：

1. 读 `PnR.tcl` L170–L175，确认现状：`remove_ignored_layers` → `set_ignored_layers` → `route_opt`。
2. 读 `03_PnR_setup.tcl` L446–L473，按顺序列出完整版多了哪些步骤。
3. 把这些步骤**按正确先后**插入 `PnR.tcl` 的 `route_opt` 之前，写出你版本的布线段（写在自己的笔记里，**不要改源码**）。关键顺序应为：
   - `check_design -checks pre_route_stage`（体检）
   - `connect_pg_net`（cells + ports，PG 重连）
   - `route.global/track/detail` 6 条选项（调优）
   - `route_auto`（初次布线）
   - `check_routes`（查 DRC）
   - `route_opt`（优化）
   - `report_qor -summary`（看结果）
4. 解释：为什么 `check_routes` 必须在 `route_auto` 之后、`route_opt` 之前？

**预期结果**：你能写出一段 15 行左右、顺序正确的布线 Tcl，并能用一句话回答第 4 步——`check_routes` 是「先布完初版暴露问题」的检查点，给随后的 `route_opt` 提供修复依据；放早了没东西可查，放晚了（`route_opt` 之后）就成了只报告不修复。

> 待本地验证：在真实 ICC2 中，补全后的布线 DRC 违规数通常显著低于极简版，但布线耗时增加。

## 6. 本讲小结

- 布线（Routing）在 CTS 之后、收尾之前，把「逻辑连接」落实成「金属走线」，是流程里最贵的一步。
- 布线内部分三段：**global（网格规划）→ track（轨道分配）→ detail（逐段画金属打孔）**，由粗到细。
- 两个总命令：`route_auto` 做初次布线（连通优先），`route_opt` 做布线 + 时序/DRC 优化迭代；二者常搭配「先连通再精修」。
- 布线前要做 **pre-route 体检**（`check_design -checks pre_route_stage`）和 **PG 重连**（`connect_pg_net` 覆盖单元管脚 + 顶层端口）。
- `route.global/track/detail` 三类选项中，**timing_driven 全程开**、**crosstalk_driven 只在 track/detail 开**（global 太早不准不划算）。
- **层范围**用「先清后设」：`remove_ignored_layers -all` → `set_ignored_layers -min/-max_routing_layer`（本仓 metal1–metal10）；**DRC** 用 `check_routes` 查，违规则回 `route_opt` 修，修不掉多半是布图拥塞。
- 极简 `PnR.tcl` 把上述一大半省略，全压进 `route_opt`；完整 `03_PnR_setup.tcl` 才是生产级流程。

## 7. 下一步学习建议

- **u4-l7 收尾与输出**：布线后还要插 filler cell、再次 `connect_pg_net`、`check_lvs`，最后 `write_verilog`/`write_sdc`/`write_parasitics`(SPEF)/`write_def`/`write_gds` 交付。本讲的 `route_opt` 结果正是收尾的输入。
- **u6 PrimeTime STA**：收尾阶段 `write_parasitics` 产出的 SPEF + 网表将喂给 PrimeTime 做签核 STA，布线后的真实寄生决定最终 slack。
- **延伸阅读**：想横向对比，看 u5-l1（ICC 传统流程）和 u5-l2（Mentor Nitro）的布线阶段如何用不同命令实现同样的 global/detail 思路。
