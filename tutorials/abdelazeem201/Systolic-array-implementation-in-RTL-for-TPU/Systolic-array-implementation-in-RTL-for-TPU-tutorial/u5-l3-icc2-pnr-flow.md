# ICC2 布局布线 PnR 六阶段流程

## 1. 本讲目标

本讲带读者走出逻辑综合（u5-l1、u5-l2），进入芯片物理实现的后半程——**布局布线（Place and Route，简称 PnR）**。综合把 RTL 翻译成「门级网表」，但那只是一堆逻辑单元的连接关系，还没决定每个门在芯片上的**物理位置**、**连线走哪层金属**、**时钟怎么分发**。PnR 就是把这张「逻辑图」变成「可制造版图（layout）」的过程。

学完本讲，读者应该能够：

- 说清 `pnr/scripts/` 下 `0`~`6` 七个脚本各自属于哪个 PnR 阶段、输入输出是什么、关键命令是什么。
- 看懂 floorplan 的三个核心参数：`core_utilization`、`flip_first_row`、`io2core`。
- 解释 CTS（时钟树综合）阶段为什么要 `clock_opt` 之后才 `derive_pg_connection`，以及为什么 CTS 必须在布线之前完成。
- 理解布线阶段「先布时钟、再布信号」的顺序，以及 verify/output 阶段产出 GDSII / 网表 / SDF / SPEF 这些交付物的含义。

## 2. 前置知识

在进入源码之前，先用通俗语言铺几个概念。

**综合 vs PnR。** 综合（u5-l1）的产物 `syn/output/tpu_top.v` 是门级网表，它回答「用哪些标准单元、怎么连」。PnR 的输入正是这个网表，它回答「这些单元摆在芯片的哪个坐标、用什么金属层把它们物理连起来」。综合用线负载模型（WLM，见 u5-l2）**估计**连线延迟；PnR 在真实的版图上把线布出来，再用**寄生参数提取（RC extraction）**算出真实延迟，因此 PnR 之后的时序才是「签字版（sign-off）」时序。

**Milkyway 库与 mw_cel。** 本项目 PnR 用 Synopsys **IC Compiler（ICC）** 工具（下文 4.1 会解释为什么不是 ICC2）。ICC 的核心数据结构是 **Milkyway 库（`mw_lib`）**，库里面存的是一个个 **cell（`mw_cel`）**——每跑完一个阶段就用 `save_mw_cel -as <名字>` 把当前版图快照另存一份。这样七个脚本就像七张「存档点」，每张存档点既是上一阶段的产出、也是下一阶段的输入。

**core、row、routing layer。** 芯片版图中心可放标准单元的矩形区域叫 **core**；标准单元按一行行排列，每一行叫一个 **row**；单元之间、单元到单元的连线走在多层金属（M1~M9，本项目 Nangate 45nm 工艺）上，层数越高越适合长距离供电和时钟，底层 M1~M3 多用于局部单元内连线。

**setup 与 hold。** 时序检查分两类：**setup（建立）**检查信号在时钟沿到来**之前**是否稳定到达（怕太慢）；**hold（保持）**检查信号在时钟沿之后是否还稳定了一小段（怕太快，被下一拍抢拍）。CTS 之后两类都要报，见 4.3。

**PG 连接。** 每个标准单元除了逻辑引脚，还有电源（VDD）和地（VSS）引脚。`derive_pg_connection` 这个命令就是「把电源/地网络与所有单元的 PG 引脚在逻辑上连起来」——它是后续布线、LVS 的前提。

## 3. 本讲源码地图

本讲涉及的文件全部在 `pnr/scripts/` 下。本讲只精读其中四个，其余三个（`0/2/3`）在 4.1 流程总览里介绍。

| 文件 | 作用 | 本讲处理 |
|------|------|---------|
| [0_design_setup.tcl](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/0_design_setup.tcl) | 建库 + 导入综合网表 + 读 SDC，是整个 PnR 的起点 | 4.1 概述 |
| [1_floorplan.tcl](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/1_floorplan.tcl) | 画核心边界、排 IO、做初步布局 | **4.2 精读** |
| [2_powerplan.tcl](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/2_powerplan.tcl) | 电源环 + 电源条带 + 标准单元供电预布 | 4.1 概述 |
| [3_placement.tcl](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/3_placement.tcl) | 标准单元的合法化、功耗与时序驱动布局 | 4.1 概述 |
| [4_clock_tree.tcl](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/4_clock_tree.tcl) | 时钟树综合（CTS）：构树、配偏斜、修 hold | **4.3 精读** |
| [5_route.tcl](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/5_route.tcl) | 信号与时钟布线、串扰优化 | **4.4 精读** |
| [6_verify_and_output.tcl](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/6_verify_and_output.tcl) | DRC/LVS 校验、填填充单元、导出 GDS/SDF/SPEF | **4.4 精读** |
| eco.tcl | 另一套独立的 `tpu_top` ECO 流程，真正产出 `output/tpu_top.gds` | 4.1 说明 |

> 提示：每个脚本第一行几乎都是 `source setup.tcl`，但仓库里 **`setup.tcl` 并不存在**（待确认）。它的作用可由 `eco.tcl` 顶部的变量声明推断：定义公共变量 `design`（设计名）和 `sc_dir`（标准单元库根目录）。下文凡用到 `${design}` 的地方，在这套 `0`~`6` 脚本里取值为占位名 `CHIP`（一个通用模板），而真正跑 `tpu_top` 的是 `eco.tcl`。

## 4. 核心概念与源码讲解

### 4.1 ICC PnR 七阶段流程总览（含全流程表）

#### 4.1.1 概念说明

首先纠正一个用词：本讲标题与项目大纲都写「ICC2」，但**实际脚本是经典 Synopsys IC Compiler（ICC）**，不是 IC Compiler II（ICC2）。判断依据有三，都来自源码：

- 命令前缀全是 ICC 体系：`create_mw_lib`、`save_mw_cel`、`route_zrt_auto`、`clock_opt`、`set_tlu_plus_files`（TLU+ 寄生模型）；
- 数据结构是 **Milkyway 库（`mw_lib`/`mw_cel`）**，ICC2 用的是 **NDM 库（`open_block`/`create_block`）**；
- 仓库里 [pnr/command.log](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/command.log) 头部明确写着 `Running icc_shell Version G-2012.06-ICC-SP2`。

ICC 与 ICC2 命令名不同、库格式不同，但**阶段思想一致**。读者记住「本项目是 ICC 流程」即可，不必纠结命名。

七阶段流水线的本质是：**每一步都让版图离「可流片」更近一点，并且每一步都把「连线延迟估计」变得更真一点**。综合阶段用的是 WLM 粗估；到 placement 有了真实坐标但仍无连线，就用 `set_zero_interconnect_delay_mode` 假设零线延迟；到 route 把线布出来后，`extract_rc` 才给出真实寄生。时序因此是逐步收敛的。

#### 4.1.2 核心流程

七阶段递进，每段一个脚本、一个 `mw_cel` 存档点：

```text
0_design_setup ──► 1_floorplan ──► 2_powerplan ──► 3_placement
   (导入网表)        (画边界/IO)      (电源网络)      (摆标准单元)
                                                                      │
   ◄── 6_verify_and_output ◄── 5_route ◄── 4_clock_tree (CTS) ◄──────┘
      (DRC/LFS + 导出)        (布线)        (时钟树综合)
```

阶段编号 `0`~`6` 即脚本文件名前缀，顺序固定。CTS（4）排在 placement（3）之后、route（5）之前，这个顺序是本讲重点（4.3）。

#### 4.1.3 源码精读

**阶段 0：建库与导入。** [0_design_setup.tcl:3-6](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/0_design_setup.tcl#L3-L6) 用 Nangate 45nm 工艺的 techfile 创建 Milkyway 库；[0_design_setup.tcl:12-14](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/0_design_setup.tcl#L12-L14) 配 TLU+（连线寄生查找表）——这是 ICC 算真实 RC 的依据；[0_design_setup.tcl:17-20](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/0_design_setup.tcl#L17-L20) 把综合网表 `../syn/output/${design}.v` 导进库（承接 u5-l1 的综合产物）；[0_design_setup.tcl:23](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/0_design_setup.tcl#L23) 读入综合阶段写出的 SDC 时序约束（u5-l2 的 cons.tcl）。最后 `save_mw_cel -as 0_design_setup`。

**阶段 2：电源网络。** [2_powerplan.tcl:4-6](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/2_powerplan.tcl#L4-L6) 在 M4（水平）、M5（竖直）上设电源条带（strap）与电源环（ring）的几何约束；[2_powerplan.tcl:8-9](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/2_powerplan.tcl#L8-L9) 用 `synthesize_fp_rail` 自动综合电源方案并提交；[2_powerplan.tcl:14](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/2_powerplan.tcl#L14) 把标准单元的 PG 引脚预连到电源轨。电源网络要在 placement 之前搭好骨架，否则布完单元再插电源条带会撞线。

**阶段 3：标准单元布局。** [3_placement.tcl:1](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/3_placement.tcl#L1) 先报功耗做基线；[3_placement.tcl:5-7](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/3_placement.tcl#L5-L7) 在 CTS 之前做一次功耗与时序驱动的 `place_opt`。注意此时**还没有时钟树**（时钟在阶段 4 才建），所以这里只摆「组合逻辑和被时钟驱动的触发器位置」。

> 关于 `eco.tcl`：仓库里还有一套独立流程 [eco.tcl](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/eco.tcl)，它在顶部 `set design tpu_top` 并 `open_mw_lib tpu_top`，是针对 `tpu_top` 的 ECO（工程变更单）流程，最终 `write_stream ... ./output/tpu_top.gds` 产出了 `pnr/output/` 目录里真实的 `tpu_top.gds / tpu_top_icc.v / tpu_top.sdc`。而 `0`~`6` 脚本用的是占位设计名 `CHIP`、输出到 `../post_layout/CHIP_layout.*`，更像一份**通用模板**。两套并存是本仓库的真实状态，本讲按大纲聚焦 `0`~`6` 模板。

#### 4.1.4 代码实践

**实践目标：** 把 `0`~`6` 七个脚本整理成一张「输入—关键命令—产出」流程表，建立全局认知。

**操作步骤：**

1. 逐个打开 `pnr/scripts/0_*.tcl` ~ `6_*.tcl`，只看每个脚本最后两行的 `save_mw_cel -as <名字>`，记下每阶段的存档点名。
2. 抄出每个脚本里最具代表性的 1~2 条命令。
3. 填入下表（答案已给，作为参考）。

| # | 阶段 | 关键命令 | 存档点（`save_mw_cel -as`） |
|---|------|----------|---------------------------|
| 0 | design_setup | `create_mw_lib` / `import_designs` / `read_sdc` | `0_design_setup` |
| 1 | floorplan | `create_floorplan -core_utilization 0.5 ...` | `1_floorplan` |
| 2 | powerplan | `set_fp_rail_constraints` / `synthesize_fp_rail` / `commit_fp_rail` | `2_powerplan` |
| 3 | placement | `place_opt -power` / `derive_pg_connection` | `3_placement` |
| 4 | clock_tree (CTS) | `set_clock_tree_options` / `clock_opt -fix_hold_all_clocks -no_clock_route` | `4_cts` |
| 5 | route | `route_zrt_group -all_clock_nets` / `route_zrt_auto` / `route_opt -stage detail` | `5_route` |
| 6 | verify_and_output | `verify_drc` / `verify_lvs` / `write_stream -format gds` | `6_corefiller` |

**需要观察的现象：** 注意存档点名（`0_design_setup`、`1_floorplan`、`2_powerplan`、`3_placement`、`4_cts`、`5_route`、`6_corefiller`）与脚本编号并不完全一致——`4` 存成 `4_cts`、`6` 存成 `6_corefiller`，这是历史命名，跑流程时要据此找对输入 cell。

**预期结果：** 能复述「网表进来 → 画边界 → 搭电源 → 摆单元 → 建时钟树 → 布线 → 校验导出」这条主线，并指出每阶段的 `mw_cel` 存档名。

#### 4.1.5 小练习与答案

**练习 1：** 阶段 0 的 `import_designs` 读的是哪个目录的网表？它和 u5-l1 的关系是什么？
**答案：** 读 `../syn/output/${design}.v`（[0_design_setup.tcl:17-20](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/0_design_setup.tcl#L17-L20)），即 u5-l1 用 Design Compiler 综合产出的门级网表。PnR 的入口正是综合的出口。

**练习 2：** 为什么电源网络（阶段 2）要在标准单元布局（阶段 3）之前搭好？
**答案：** 电源环和电源条带占用大量金属层和版图空间。若先布完单元再插电源，条带会撞上已布的信号线和单元；先搭电源骨架，placement 才能在已知供电结构下合法地摆单元。

**练习 3：** `command.log` 里的版本字符串说明实际工具是 ICC 还是 ICC2？依据是什么？
**答案：** 是 **ICC**（`icc_shell Version G-2012.06-ICC-SP2`）。再结合命令体系（`mw_lib`/`route_zrt`/`clock_opt`/TLU+）佐证。脚本中没有任何 ICC2 的 `ndm`/`open_block`/`create_block` 命令。

---

### 4.2 floorplan 阶段（1_floorplan.tcl）

#### 4.2.1 概念说明

**floorplan（布局规划）** 决定芯片的「大蓝图」：核心矩形多大、IO 引脚排在四周哪里、宏单元放哪、标准单元排成多少行。它不关心每个具体单元的精确坐标，只定边界与排布规则。floorplan 一旦定下，后续的电源、布局、布线都在这个「画框」里进行。

三个关键参数：

- **`core_utilization`（核心利用率）**：核心面积被标准单元占满的比例。本项目取 `0.5`，即只填满 50%。利用率低，剩余空间留给布线通道、电源条带和 CTS 缓冲器，时序与可布线性更好，代价是芯片更大。
- **`flip_first_row`（翻转首行）**：把第一行标准单元镜像翻转。相邻两行一正一反，电源轨（VDD/VSS）就能在行边界对接共享，保证阱区（well）连续。
- **`io2core`（IO 到核心距离）**：IO 引脚到核心边界的留白。本项目四边都留 `15`（微米单位），空间留给电源环和 IO 走线。

#### 4.2.2 核心流程

`1_floorplan.tcl` 的执行顺序：

```text
1. read_pin_pad_physical_constraints (io_pin.tdf)   ← 读 IO 引脚排布约束
2. create_floorplan  utilization=0.5, flip_first_row, io2core=15
3. identify_clock_gating / report_clock_gating       ← 识别时钟门控单元
4. create_fp_placement -timing_driven                ← 一次时序驱动的初布局
5. set_zero_interconnect_delay_mode true → report_timing → false   ← 零线延迟时序体检
6. create_fp_placement -congestion_driven (×2)       ← 拥塞驱动精修
7. save_mw_cel -as 1_floorplan
```

第 5 步是个巧妙的「体检」：临时令所有连线延迟为 0，再 `report_timing`。这样报出的时序**只反映纯逻辑延迟**，不掺入还没布的连线，方便工程师判断「时序问题到底是逻辑太深，还是连线太长」。

#### 4.2.3 源码精读

**画核心与排 IO。** [1_floorplan.tcl:3-4](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/1_floorplan.tcl#L3-L4)：

```tcl
read_pin_pad_physical_constraints ../pre_layout/design_data/io_pin.tdf
create_floorplan -core_utilization 0.5 -flip_first_row -left_io2core 15 -bottom_io2core 15 -right_io2core 15 -top_io2core 15
```

> 注：`io_pin.tdf`（IO 排布文件）在仓库里并不存在（待确认），它是 floorplan 的外部输入。命令本身说明：核心利用率 0.5、首行翻转、四边 IO 到核心留白 15。

**时钟门控识别。** [1_floorplan.tcl:6-7](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/1_floorplan.tcl#L6-L7) 的 `identify_clock_gating` 让工具知道哪些单元的时钟是被门控的——这关系到阶段 4 的 CTS 要不要处理门控时钟。回顾 u5-l1：综合时 `-gate_clock` 开了却零门控单元，所以这里只是预防性识别。

**零线延迟时序体检。** [1_floorplan.tcl:9-12](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/1_floorplan.tcl#L9-L12)：

```tcl
create_fp_placement -timing_driven
set_zero_interconnect_delay_mode true
report_timing
set_zero_interconnect_delay_mode false
```

`set_zero_interconnect_delay_mode true` 与后面的 `false` 必须成对——它是一个全局开关，开期间所有时序报告都假设连线零延迟。

**拥塞驱动精修与存档。** [1_floorplan.tcl:15-19](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/1_floorplan.tcl#L15-L19) 连做两次 `create_fp_placement -congestion_driven`（第二次带 `-incremental all` 增量优化），降低布线拥塞，最后存档 `1_floorplan`。

#### 4.2.4 代码实践

**实践目标：** 理解 `core_utilization` 对芯片面积与时序可布线性的权衡。

**操作步骤（源码阅读型）：**

1. 读 [1_floorplan.tcl:4](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/1_floorplan.tcl#L4)，确认 `core_utilization 0.5`。
2. 假设标准单元总面积为 \(A_{cells} \)，则核心面积 \(A_{core}\) 满足 \(A_{cells} / A_{core} = 0.5\)，即 \(A_{core} = 2 A_{cells}\)——核心比单元大一倍。
3. 推演：若把 `0.5` 改成 `0.8`，核心面积变小、芯片更小更便宜，但剩余布线空间从 50% 压到 20%，时序收敛和拥塞都会变难。

**需要观察的现象：** 利用率与「可用布线资源」是此消彼长。

**预期结果：** 能口述「`core_utilization` 越低 → 芯片越大 → 布线越宽松 → 时序越容易收敛；越高则相反」。

> 待本地验证：若有 ICC 环境，可分别用 `0.5` 与 `0.8` 跑 floorplan，对比 `report_congestion` 的溢出（overflow）数量与 `report_timing` 的 slack。

#### 4.2.5 小练习与答案

**练习 1：** `flip_first_row` 解决什么物理问题？
**答案：** 让相邻两行标准单元的电源轨（VDD/VSS）在边界对接、共享，并保证 N 阱/P 阱连续。不翻则两行的同名电源轨错位，无法直连。

**练习 2：** `set_zero_interconnect_delay_mode` 在 [1_floorplan.tcl:10](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/1_floorplan.tcl#L10) 为何要紧跟一个 `false`？
**答案：** 它是全局开关，若不关掉，后续所有阶段（powerplan、placement、CTS）的时序都会被误当成零线延迟，导致 CTS 和优化决策错误。必须成对开关。

**练习 3：** floorplan 阶段的 `create_fp_placement` 与阶段 3 的 `place_opt` 有何不同？
**答案：** `create_fp_placement` 是 floorplan 内的**粗排**（timing/congestion 驱动的初步摆放，为电源规划和拥塞评估服务）；`place_opt` 是真正的**标准单元合法化与优化**（功耗+时序驱动，输出可送 CTS 的精确布局）。

---

### 4.3 CTS 阶段（4_clock_tree.tcl）

#### 4.3.1 概念说明

**CTS（Clock Tree Synthesis，时钟树综合）** 是 PnR 中最讲究的环节。综合后的网表里，时钟信号从单一的 `clk` 源扇出，要驱动成百上千个触发器。若直接一根线连所有触发器，扇出巨大、延迟/偏斜失控。CTS 的工作是**插入一串时钟缓冲器（buffer）/反相器（inverter），搭成一棵树**，让时钟尽量同时到达每个触发器，把「时钟偏斜（skew）」压到最小。

CTS 必须在布线**之前**做，原因有二：

1. **CTS 会改变网表与布局**：它插入大量新单元（时钟缓冲器），这些单元要占位置。布线假设网表稳定，若先布线再插缓冲器，就得拆掉重布。
2. **时钟网优先级最高**：布线阶段（5）会先单独布时钟网（`route_zrt_group -all_clock_nets`），再布信号网，给时钟网优先权。所以时钟树结构必须先于信号布线确定。

而本阶段内部，「**先 `clock_opt` 再 `derive_pg_connection`**」的顺序，正是本讲代码实践要回答的核心问题（见 4.3.4）。

#### 4.3.2 核心流程

`4_clock_tree.tcl` 的执行顺序：

```text
1. source add_tie.tcl                  ← 插入 tie-high/tie-low 单元，钉住悬空输入
2. identify_clock_gating               ← 识别时钟门控
3. set_clock_tree_options ...          ← 设定 CTS 质量目标（skew/transition/cap）
4. set_fix_hold [all_clocks]           ← 授权修复 hold 违例
5. clock_opt -fix_hold_all_clocks -no_clock_route   ← 建树+修hold，但不布时钟线
6. derive_pg_connection (VDD/VSS)      ← 给所有单元（含新插入的时钟缓冲器）连电源
7. report_timing (setup) + report_timing -delay_type min (hold)   ← 报两类时序
8. save_mw_cel -as 4_cts
```

`clock_opt` 的 `-no_clock_route` 很关键：它**建好时钟树结构、插好缓冲器、配好偏斜、修好 hold，但不把时钟线物理布出来**。时钟线的物理布线留给阶段 5 的 `route_zrt_group -all_clock_nets`。这样 CTS 专管「逻辑结构」，布线阶段专管「物理走线」，分工清晰。

#### 4.3.3 源码精读

**插 tie 单元与门控识别。** [4_clock_tree.tcl:3-4](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/4_clock_tree.tcl#L3-L4)：

```tcl
source -echo ../pre_layout/design_data/add_tie.tcl
identify_clock_gating
```

> `add_tie.tcl` 在仓库里不存在（待确认）。tie 单元（接固定高/低电平）用来钉住触发器或组合单元未用的输入引脚，防止悬空导致功耗与噪声。

**CTS 质量目标。** [4_clock_tree.tcl:5](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/4_clock_tree.tcl#L5)：

```tcl
set_clock_tree_options -max_transition 0.500 -max_capacitance 600.000 -max_fanout 2000 ... -target_skew 0.000 -buffer_relocation TRUE -buffer_sizing TRUE -gate_relocation TRUE ... -operating_condition max
```

读几个关键值：`-max_transition 0.500`（时钟网翻转时间≤0.5ns，即压 slew）、`-target_skew 0.000`（目标零偏斜）、`-buffer_sizing TRUE`/`-buffer_relocation TRUE`（允许 CTS 调缓冲器尺寸与位置）、`-operating_condition max`（用最坏工艺角，与 u5-l2 的 ss 角一致）。

**授权修 hold 与建树。** [4_clock_tree.tcl:6-7](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/4_clock_tree.tcl#L6-L7)：

```tcl
set_fix_hold [all_clocks]
clock_opt -fix_hold_all_clocks -no_clock_route
```

`set_fix_hold` 先开「允许修 hold」的开关，`clock_opt` 才会在建树时插入延迟单元把过短的 hold 路径拉长。

**PG 连接（顺序关键）。** [4_clock_tree.tcl:8](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/4_clock_tree.tcl#L8)：

```tcl
derive_pg_connection -power_net {VDD} -ground_net {VSS} -power_pin {VDD} -ground_pin {VSS}
```

这一行排在 `clock_opt` **之后**。

**报 setup 与 hold。** [4_clock_tree.tcl:10](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/4_clock_tree.tcl#L10)（默认 setup）与 [4_clock_tree.tcl:12](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/4_clock_tree.tcl#L12)（`-delay_type min` 即 hold）分别报两类时序。

#### 4.3.4 代码实践

**实践目标：** 解释本阶段「先 `clock_opt` 再 `derive_pg_connection`」的顺序原因，以及 CTS 为何必须排在布线之前。

**操作步骤（源码阅读型，配合推演）：**

1. 打开 [4_clock_tree.tcl:7-8](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/4_clock_tree.tcl#L7-L8)，确认 `clock_opt` 在前、`derive_pg_connection` 在后。
2. 回答下面两个问题（参考答案见后）：
   - **Q1：为什么 `derive_pg_connection` 必须在 `clock_opt` 之后？**
   - **Q2：为什么整个 CTS 阶段（4）必须排在布线阶段（5）之前？**

**参考答案：**

- **Q1（clock_opt 先于 derive_pg_connection）：** `clock_opt` 会**插入大量新的时钟缓冲器/反相器单元**进设计。这些新单元和原有单元一样有 VDD/VSS 引脚，但刚插入时尚未连到电源网络。`derive_pg_connection` 紧随其后，正是为了让**包括新插缓冲器在内的所有单元**的 PG 引脚都与 VDD/VSS 网络连通。若反过来先连 PG 再 clock_opt，后插入的缓冲器 PG 引脚会悬空，导致后续 LVS（版图对原理图）报错、布线阶段电源不完整。
- **Q2（CTS 先于布线）：** 其一，CTS 插入新单元会改变网表和布局，而布线要求网表稳定，先布后插必返工；其二，布线阶段会优先单独布时钟网（[5_route.tcl:5](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/5_route.tcl#L5) 的 `route_zrt_group -all_clock_nets`），时钟树结构必须先于信号布线定下来，时钟网才有优先权可言。

**需要观察的现象：** 顺序不是风格问题，而是依赖关系——PG 连接依赖「单元已全部就位」，布线依赖「时钟树已定」。

**预期结果：** 能画出 `clock_opt（插缓冲器）→ derive_pg_connection（连电源）→ 阶段5 route_zrt_group（布时钟线）`这条因果链。

> 待本地验证：在 ICC 里若把 [4_clock_tree.tcl:8](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/4_clock_tree.tcl#L8) 的 `derive_pg_connection` 删掉，跑到阶段 6 的 `verify_lvs` 应会报出时钟缓冲器 PG 引脚悬空（floating port）。

#### 4.3.5 小练习与答案

**练习 1：** `clock_opt` 的 `-no_clock_route` 是什么意思？时钟线最终在哪布？
**答案：** 意为「建好时钟树结构、插好缓冲器、修好 hold，但**不**做时钟线的物理布线」。时钟线的物理布线在阶段 5 由 [5_route.tcl:5](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/5_route.tcl#L5) 的 `route_zrt_group -all_clock_nets` 完成。

**练习 2：** `set_clock_tree_options` 的 `-target_skew 0.000` 表达什么设计意图？
**答案：** 把时钟偏斜（同一时钟沿到达不同触发器的时间差）目标设为 0，即希望时钟尽量同时到达所有触发器，最大化有效时钟周期。实际难以严格为 0，但工具会尽量逼近。

**练习 3：** 为什么 [4_clock_tree.tcl:12](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/4_clock_tree.tcl#L12) 要单独 `report_timing -delay_type min`？
**答案：** 默认 `report_timing` 报 setup（怕信号太慢）；`-delay_type min` 报 hold（怕信号太快被下一拍抢拍）。CTS 之后两类都要看，因为 CTS 既影响时钟到达时间（setup）又因插延迟修 hold。

---

### 4.4 route 与 verify 阶段（5_route.tcl + 6_verify_and_output.tcl）

#### 4.4.1 概念说明

**route（布线）** 把所有逻辑连接关系变成真实的金属连线。本项目用 Synopsys 的 **Zroute**（命令前缀 `route_zrt_*`）路由器。布线顺序遵循「时钟优先」：先用 `route_zrt_group -all_clock_nets` 专门布时钟网，再用 `route_zrt_auto` 布所有信号网。这是因为时钟网对延迟与串扰最敏感，要先抢占最优走线层与最短路径。

布线阶段还要开 **信号完整性（SI，Signal Integrity）** 检查：相邻走线会通过寄生电容耦合产生**串扰（crosstalk）**，造成延迟抖动和噪声。`set_si_options` 打开串扰分析与预防。

**verify（校验）与 output（导出）** 是 PnR 的收尾：先做 **DRC（设计规则检查）** 和 **LVS（版图对原理图）** 确认版图可制造且与网表一致；插入**填充单元（core filler）** 保证阱区连续；最后导出一批交付物：

- **GDSII（`write_stream -format gds`）**：版图的工业标准二进制格式，送晶圆厂流片用。
- **门级网表（`write_verilog`）**：带物理信息的后仿真网表。
- **SDF（`write_sdf`）**：标准延迟格式，供后仿真反标真实延迟。
- **SDC（`write_sdc`）**：后版图时序约束。
- **SPEF（`write_parasitics`，先 `extract_rc`）**：寄生参数文件，含真实 RC，是签字版时序的依据。

#### 4.4.2 核心流程

`5_route.tcl` 的执行顺序：

```text
1. derive_pg_connection                        ← 刷新 PG
2. check_zrt_routability                       ← 布线前可布线性预检
3. set_ignored_layers -max_routing_layer M9    ← 顶层 M9 以上不布线
4. set_si_options (delta_delay/static_noise/xtalk_prevention)  ← 开串扰分析
5. route_zrt_group -all_clock_nets             ← 先布时钟网
6. route_zrt_auto                              ← 再布所有信号网
7. route_opt -stage detail -xtalk_reduction    ← 精修布线 + 降串扰
8. derive_pg_connection -create_ports top      ← 建顶层 PG 端口
9. save_mw_cel -as 5_route
```

`6_verify_and_output.tcl` 的执行顺序：

```text
1. verify_drc -ignore_density                  ← 设计规则检查
2. verify_lvs -ignore_floating_port            ← 版图对原理图
3. source addCoreFiller.tcl                    ← 插填充单元（阱连续）
4. save_mw_cel -as 6_corefiller
5. set_write_stream_options + derive_pg_connection + verify_lvs
6. write_stream -format gds (→ CHIP.gds)       ← 导 GDSII
7. write_verilog (→ CHIP_layout.v)             ← 后版图网表
8. write_sdf / write_sdc                       ← 延迟与约束
9. extract_rc + write_parasitics SPEF          ← 寄生提取
```

#### 4.4.3 源码精读

**布线前预检与层限制。** [5_route.tcl:2-3](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/5_route.tcl#L2-L3)：

```tcl
check_zrt_routability  -error_view CHIP.err
set_ignored_layers  -max_routing_layer M9
```

`-max_routing_layer M9` 把最高可用布线层限制在 M9，更高的金属层（如顶层供电）保留不用。

**串扰选项。** [5_route.tcl:4](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/5_route.tcl#L4) 打开 `-delta_delay true -static_noise true -route_xtalk_prevention true`，让路由器在布线时就规避串扰。

**时钟优先 + 信号 + 精修。** [5_route.tcl:5-7](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/5_route.tcl#L5-L7)：

```tcl
route_zrt_group -all_clock_nets
route_zrt_auto
route_opt -stage detail -xtalk_reduction
```

注意 [5_route.tcl:5](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/5_route.tcl#L5) 的 `route_zrt_group -all_clock_nets`，正是承接 CTS 阶段 `-no_clock_route` 留下的活——CTS 建好了树结构，这里才把树 physically 布出来。

**校验与填充。** [6_verify_and_output.tcl:1-3](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/6_verify_and_output.tcl#L1-L3)：

```tcl
verify_drc -ignore_density
verify_lvs -ignore_floating_port
source -echo ../pre_layout/design_data/addCoreFiller.tcl
```

> `addCoreFiller.tcl` 在仓库里不存在（待确认）。core filler 是无逻辑功能的填充单元，塞在标准单元行间隙，保证阱区与电源轨连续。

**导出 GDS 与网表。** [6_verify_and_output.tcl:13](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/6_verify_and_output.tcl#L13) 的 `write_stream -format gds` 写出 GDSII；[6_verify_and_output.tcl:15](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/6_verify_and_output.tcl#L15) 的 `write_verilog` 写后版图网表；[6_verify_and_output.tcl:17](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/6_verify_and_output.tcl#L17) 与 [6_verify_and_output.tcl:19](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/6_verify_and_output.tcl#L19) 写 SDF 与 SDC。

**寄生提取。** [6_verify_and_output.tcl:21-22](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/6_verify_and_output.tcl#L21-L22)：

```tcl
extract_rc
write_parasitics -output ../post_layout/CHIP_layout -format SPEF -compress
```

`extract_rc` 基于真实版图几何算出每条线的电阻电容，`write_parasitics` 存成 SPEF。这取代了 u5-l2 的 WLM 粗估，是签字版时序的输入。

> 注意：[6_verify_and_output.tcl:7-17](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/6_verify_and_output.tcl#L7-L17) 里若干 `write_stream`/`write_verilog` 的路径含大量连续斜杠（如 `////t/CHIP.gds`），是模板未填干净的占位符（待确认）。真正产出 `output/tpu_top.gds` 等文件的是 [eco.tcl](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/eco.tcl)，其 `write_stream -lib $design -cells $design ./output/${design}.gds` 路径完整。

#### 4.4.4 代码实践

**实践目标：** 把布线阶段「时钟优先、信号随后、精修收尾」的三步串起来，并理解 SPEF 为何是签字版时序的依据。

**操作步骤（源码阅读型）：**

1. 读 [5_route.tcl:5-7](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/5_route.tcl#L5-L7)，把三条命令对应到「时钟网布线 → 信号网布线 → 精修降串扰」三步。
2. 回顾 u5-l2：综合阶段用线负载模型（WLM）**估计**连线延迟；现在读 [6_verify_and_output.tcl:21-22](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/6_verify_and_output.tcl#L21-L22)，`extract_rc` 基于真实版图算 RC。
3. 对照 README 的 PnR 声明：「I have succeeded to meet my time constraints, and all the test-bench data passed」——说明布线后用 SPEF/SDF 做的后仿真（既验证时序又验证功能）通过了。

**需要观察的现象：** 综合时序（WLM）是估计，后版图时序（SPEF）是真实；后者才是流片签字依据。

**预期结果：** 能说清「`route_zrt_group -all_clock_nets`（时钟）→ `route_zrt_auto`（信号）→ `route_opt -stage detail -xtalk_reduction`（精修）」三步，以及 `extract_rc` + SPEF 取代 WLM 的意义。

> 待本地验证：用 `report_timing` 对比布线前（阶段 4，基于估算）与布线后（阶段 6，基于 SPEF）的 slack，应能观察到时序收敛（slack 非负、违例数为 0），与 README 的「meet time constraints」一致。

#### 4.4.5 小练习与答案

**练习 1：** 为什么 [5_route.tcl:5](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/5_route.tcl#L5) 要在 `route_zrt_auto` 之前先 `route_zrt_group -all_clock_nets`？
**答案：** 时钟网对延迟和偏斜最敏感，要先布才能抢占最优走线层与最短路径，保证时钟质量。若和信号网一起布，时钟网可能被挤到次优路径，skew 与延迟变差。

**练习 2：** `verify_drc` 与 `verify_lvs` 各查什么？
**答案：** `verify_drc` 查设计规则（线宽、间距、孔等制造规则）是否违反；`verify_lvs` 查版图与原理图（网表）是否一致——即版图里连出来的电路和综合给的网表是不是同一个电路。

**练习 3：** `extract_rc` 产出的 SPEF 与 u5-l2 的 WLM 有何本质区别？
**答案：** WLM 是综合阶段的**统计估算**（按线负载模型猜连线 RC）；SPEF 是 PnR 之后基于**真实版图几何**算出的精确寄生 RC。前者用于综合期粗判，后者用于流片签字（sign-off）时序。

---

## 5. 综合实践

**任务：** 用一张完整的「七阶段依赖图」把本讲所有知识串起来，并预测「跳过 CTS 阶段」会引发哪些连锁故障。

**操作步骤：**

1. 画一条横向流水线，标出 `0_design_setup → 1_floorplan → 2_powerplan → 3_placement → 4_clock_tree → 5_route → 6_verify_and_output`。
2. 在每个阶段下标注：① 该阶段读哪个 `mw_cel` 存档点、② 该阶段 `save_mw_cel -as` 的存档点名、③ 一条代表性命令。
3. **故障推演**：假设跳过阶段 4（CTS），直接从 `3_placement` 进 `5_route`。请按顺序回答：
   - 时钟网会怎样？（无树结构，单点巨扇出，skew 失控）
   - [5_route.tcl:5](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/5_route.tcl#L5) 的 `route_zrt_group -all_clock_nets` 还有没有「树」可布？
   - [4_clock_tree.tcl:8](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/4_clock_tree.tcl#L8) 的 `derive_pg_connection` 若被跳过，[6_verify_and_output.tcl:2](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/6_verify_and_output.tcl#L2) 的 `verify_lvs` 会报什么？
4. 把故障推演的结论写成一两条 bullet。

**预期结论（参考）：**

- 跳过 CTS → 时钟无缓冲器树，`route_zrt_group -all_clock_nets` 没有「树」可布（CTS 的 `-no_clock_route` 把布线权交给阶段 5，但树结构得在阶段 4 建好），时钟 skew/setup 必然违例，README 的「meet time constraints」不可能成立。
- 跳过 CTS 里的 `derive_pg_connection` → 时钟缓冲器（本应被插入）不存在，但已有的标准单元若也未被本阶段重连，`verify_lvs` 会报 floating port / PG 不完整。这反向印证了「CTS 阶段 derive_pg_connection 紧跟 clock_opt」的必要性。

## 6. 本讲小结

- 本项目 PnR 实为 **Synopsys IC Compiler（ICC，G-2012.06）** 流程，依据是 `command.log` 版本字符串与 `mw_lib`/`route_zrt`/`clock_opt`/TLU+ 命令体系；并非 ICC2，但阶段思想一致。
- 七个脚本 `0`~`6` 对应 design_setup → floorplan → powerplan → placement → CTS → route → verify_and_output，每阶段用 `save_mw_cel -as` 存一个快照（注意 `4_cts`、`6_corefiller` 命名与编号不齐）。
- floorplan 三参数：`core_utilization 0.5`（留一半布线余量）、`flip_first_row`（行间电源轨共享/阱连续）、`io2core 15`（IO 到核心留白）。
- CTS 用 `clock_opt -fix_hold_all_clocks -no_clock_route` 建树修 hold 但不布时钟线；`derive_pg_connection` 必须排在 `clock_opt` 之后，因为后者新插入的时钟缓冲器需要连电源。
- 布线遵循「时钟优先」：先 `route_zrt_group -all_clock_nets`，再 `route_zrt_auto`，最后 `route_opt -stage detail -xtalk_reduction` 精修降串扰。
- 收尾用 `verify_drc`/`verify_lvs` 校验、插 core filler，再导出 GDSII/网表/SDF/SDC/SPEF；其中 `extract_rc` + SPEF 取代综合期 WLM，是签字版时序依据，对应 README「时序与 testbench 全通过」的结论。
- 仓库里 `0`~`6` 是用占位名 `CHIP` 的通用模板（部分输出路径含 `////` 占位符，待确认），真正产出 `output/tpu_top.gds` 的是 [eco.tcl](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/eco.tcl) 的 `tpu_top` ECO 流程。

## 7. 下一步学习建议

- **衔接 u5-l4（FPGA 综合与 SoC 集成）**：本讲讲的是 ASIC PnR（出 GDSII 流片），下一讲转向 FPGA——两者工具链与目标完全不同（FPGA 用 Vivado 综合到 LUT/BRAM，ASIC 用 ICC 出标准单元版图），可对比体会「同一份 RTL、两种实现路径」。
- **深入阅读建议**：若想看真实的 `tpu_top` 版图产出，读 [eco.tcl](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/pnr/scripts/eco.tcl)，对照 `pnr/output/` 目录里的 `tpu_top.gds`、`tpu_top_icc.v`、`tpu_top.sdc`，理解 ECO 流程如何收尾。
- **回溯验证链**：可回看 u4（端到端仿真）与 u5-l2（综合时序），把「RTL 仿真 → 综合时序 → 后版图 SPEF 时序」这条验证链补全，理解为何 README 说「all test-bench data passed」既指功能仿真也指后版图时序。
