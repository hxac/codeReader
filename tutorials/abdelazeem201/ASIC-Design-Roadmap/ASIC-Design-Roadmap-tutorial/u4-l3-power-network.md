# 电源网络设计（Power Network）

> 单元 U4 · ICC2 物理设计主流程 · 第 3 讲
> 依赖：u4-l2 布局规划 Floorplan

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 ICC2 电源网络的**逻辑层**（net）与**物理层**（金属）的区别，以及二者如何通过 `create_net` / `connect_pg_net` / `compile_pg` 串联起来。
- 读懂 `PnR.tcl` 里电源网络的**三段式套路**——顶层 mesh（M6/M7）、宏单元 ring（M6/M7）、标准单元 rail（M1），并理解它们各自承担的供电职责。
- 掌握 `create_pg_*_pattern` → `set_pg_strategy` → `compile_pg` 这条「**模式 → 策略 → 编译**」流水线。
- 读懂 `Vpad.tcl` 的双 `for` 循环如何沿 die 四边均匀布放虚拟电源 pad（virtual pad），并能估算 pad 间距。
- 理解 IR drop 的物理含义，看懂 `analyze_power_plan` 的参数与它回答的问题。

## 2. 前置知识

本讲默认你已经学过 **u4-l2 布局规划**（知道 die、core、core_offset、`initialize_floorplan`）。这里补充几个电源网络专属概念，用通俗语言先建立直觉。

### 2.1 为什么需要专门的「电源网络」

芯片里有成百上千万个标准单元，每一个都要吃电（VDD）和接地（VSS）。综合与布局只关心**逻辑功能**和**时序**，并不保证每个单元的电源引脚都能被金属「喂到」。电源网络设计（Power/Ground network planning，简称 PG）就是专门给全芯片搭一套**低电阻的供电金属骨架**，让每个角落的单元都能拿到接近标称值的电压。

> 直觉：把电源网络想象成一栋大楼的水管系统——主干（mesh）是大口径主管，环线（ring）是给大户型（SRAM 宏单元）的独立支管，rail（轨道）是接进每个房间（标准单元）的小管。

### 2.2 IR drop：电源网络的「体检指标」

电流流过金属会产生压降，遵循欧姆定律：

\[
V_{\text{drop}} = I \times R
\]

离供电入口越远、电流越大的单元，实际拿到的电压就越低。这个压降叫 **IR drop**。压降过大时，单元供电不足，可能导致：

- **时序违例**：电压低 → 单元变慢 → setup 违例。
- **功能错误**：电压低于阈值 → 触发器保持不住数据。

所以电源网络不仅要「连上」，还要「连得足够粗、足够密」，把 IR drop 压在设计允许的范围（通常允许标称电压的百分之几）内。

### 2.3 三层金属、三类结构

本仓库工艺（Nangate 45nm FreePDK45 风格）有多层金属 M1~M9 + MRDL。电源网络按金属层高低分成三类结构，由粗到细：

| 结构 | 典型金属层 | 形状 | 服务对象 |
|------|-----------|------|---------|
| **mesh**（网格） | 顶层 M6/M7 | 横竖正交的网格带 | 全 core 的大面积供电 |
| **ring**（环） | M6/M7 | 绕宏单元一圈的环 | SRAM 等宏单元的局部供电 |
| **rail**（轨道） | 底层 M1 | 沿标准单元行的细条 | 每个标准单元的 VDD/VSS pin |

### 2.4 ICC2 的「pattern / strategy / compile」三段式

这是本讲最重要的设计范式。ICC2 不让你直接画金属，而是分三步：

1. **pattern（模式）**：用 `create_pg_*_pattern` 定义「一段金属长什么样」——多宽、什么层、间距多少、节距（pitch）多少。这是**可复用的模板**。
2. **strategy（策略）**：用 `set_pg_strategy` 把 pattern **绑定到一个范围**（core 还是某些 macros）和**一组 net**（VDD、VSS）。这回答「在哪、给谁、用哪个模板」。
3. **compile（编译）**：用 `compile_pg` 真正执行，把策略**物化成金属走线**。

> 类比：pattern 是蛋糕配方，strategy 是「用这个配方在蛋糕坯（core）上涂一层奶油（VDD/VSS）」，compile 才是真正动手烤。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [IC Compiler II/PnR.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl) | ICC2 物理设计主脚本。本讲聚焦其中的电源网络段（L42–L46 的逻辑建网，以及 L85–L134 的 mesh/ring/rail 物理金属与 IR 分析）。 |
| [IC Compiler II/Vpad.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Vpad.tcl) | 一段**独立可复用的虚拟电源 pad 脚本**。内容与 `PnR.tcl` 的 L114–L134 几乎完全相同，可单独 `source` 进来做 IR 分析。 |
| IC Compiler II/Scripts/01_common_setup.tcl | 定义 `NDM_POWER_NET="VDD"` / `NDM_GROUND_NET="VSS"` 等变量，电源网络名来自这里。 |

> 观察：`Vpad.tcl` 与 `PnR.tcl` 的 IR drop 段是**同一段代码的两份副本**。`Vpad.tcl` 把它抽出来当独立工具用——你可以在 floorplan 之后任意时刻单独 `source Vpad.tcl` 评估电源网络，而不必跑完整 PnR。

---

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：

1. **PG ring/mesh/rail 模式与策略**——逻辑建网 + 三种 pattern + strategy 绑定。
2. **compile_pg 编译**——把策略变成金属的执行顺序。
3. **虚拟 pad 放置**——`Vpad.tcl` 的双 `for` 循环。
4. **IR drop 分析**——`analyze_power_plan` 的用法与物理含义。

---

### 4.1 PG ring/mesh/rail：模式与策略

#### 4.1.1 概念说明

电源网络的搭建分**逻辑**和**物理**两步。

**逻辑层**——在 floorplan 之后、画金属之前，先用 `create_net` 建两根贯穿全芯片的逻辑电源/地网络（VDD/VSS），再用 `connect_pg_net` 把层次化查到的所有单元电源引脚挂到这两根网上。此时**还没有金属**，只是告诉工具「这些引脚逻辑上属于同一个电源网络」。

**物理层**——用三段式套路给这个逻辑网络穿上金属外衣。本仓库搭了**三种结构**：

- **mesh（网格）**：在顶层金属 M6（垂直）/M7（水平）上铺横竖正交的电源带，覆盖整个 core，是主供电骨架。
- **ring（环）**：围绕 4 个 SRAM 宏单元各画一个矩形环，给大电流的宏单元单独供电，避免它们和标准单元抢电。
- **rail（轨道）**：在底层 M1 上沿标准单元行铺细条，直接接到每个标准单元的 VDD/VSS pin。

> 三种结构分工：mesh 管「大面积输送」，ring 管「宏单元特供」，rail 管「最后一米入户」。它们共同构成从顶层供电入口到每个标准单元 pin 的完整通路。

ICC2 提供 `create_pg_mesh_pattern`、`create_pg_ring_pattern`、`create_pg_std_cell_conn_pattern` 三个命令分别定义三种 pattern，再用统一的 `set_pg_strategy` 绑定范围与 net。

#### 4.1.2 核心流程

```
逻辑建网（floorplan 之后立即做）
  create_net -power  VDD          ┐ 逻辑上声明两根电源网络
  create_net -ground VSS          ┘
  connect_pg_net ... */VDD         ┐ 把所有单元的电源引脚挂到网上
  connect_pg_net ... */VSS         ┘

物理金属（布局合法化之后做）
  ┌─ mesh ─ create_pg_mesh_pattern  → set_pg_strategy(core)  → compile_pg
  ├─ ring ─ create_pg_ring_pattern  → set_pg_strategy(macros)→ compile_pg
  └─ rail ─ create_pg_std_cell_conn_pattern → set_pg_strategy(core) → compile_pg
```

#### 4.1.3 源码精读

**逻辑建网**（紧接 `initialize_floorplan` 和 `place_pins` 之后）：

[IC Compiler II/PnR.tcl:42-46](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L42-L46) —— `create_net -power/-ground` 建逻辑电源/地网络（网络名取自 `NDM_POWER_NET/NDM_GROUND_NET` 变量），`connect_pg_net` 用 `[get_pins -hierarchical "*/VDD"]` 把层次化查到的所有单元电源引脚连到网络。注意引脚名 `VDD`/`VSS` 是**硬编码**，它必须与单元库里真实的电源引脚名一致（本仓库由 `NDM_POWER_PORT` 约定，详见 u3-l1）。

**① mesh pattern + strategy**：

[IC Compiler II/PnR.tcl:86-92](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L86-L92) —— 定义顶层网格模式 `P_top_two`：M7 为水平层、M6 为垂直层（横竖正交），`width: 0.2`（金属带宽 0.2μm），`pitch: 30`（节距 30μm，即每隔 30μm 铺一条带），`spacing: interleaving`（**交错**排布——VDD 和 VSS 带交替排列，利于降低 IR drop 与耦合噪声），`offset` 给每层一个起始偏移，`trim: true` 让网格在边界处自动修剪。

[IC Compiler II/PnR.tcl:93-97](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L93-L97) —— 策略 `S_default_vddvss` 把 `P_top_two` 绑到 `-core`（整个 core）、net 用 `{VSS VDD}`，`-extension {stop:design_boundary_and_generate_pin}` 让网格一直延伸到设计边界并生成边界 pin（供电入口）。最后一行 `compile_pg` 才真正画出金属。

**② ring pattern + strategy**：

[IC Compiler II/PnR.tcl:102-105](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L102-L105) —— 定义宏单元环模式 `P_HM_ring`：水平层 M7、垂直层 M6，`horizontal_width {1}` / `vertical_width {1}`（环线宽 1μm，比 mesh 的 0.2μm 更粗，因为宏单元电流大）。策略 `S_HM_ring_top` 用 `-macros $macro_list` 把环限定在 4 个 SRAM 宏单元周围，`offset {0.1 0.1}` 让环离宏单元边缘 0.1μm。

**③ rail pattern + strategy**：

[IC Compiler II/PnR.tcl:109-111](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L109-L111) —— 定义标准单元轨道模式 `std_rail_conn1`：`-rail_width 0.094 -layers M1`（M1 上 0.094μm 宽的细轨）。策略 `std_rail_1` 绑到 `-core`、net `VDD VSS`，`compile_pg` 生成沿标准单元行的电源轨道。

#### 4.1.4 代码实践

**实践目标**：理解 pattern / strategy / compile 三段式的对应关系。

**操作步骤（源码阅读型）**：

1. 打开 `PnR.tcl` 的 L86–L111，列一张三列对照表：

   | 结构 | pattern 名 | 策略名 | 绑定范围 | net | 金属层 |
   |------|-----------|--------|---------|-----|--------|
   | mesh | `P_top_two` | `S_default_vddvss` | core | VSS VDD | M6/M7 |
   | ring | `P_HM_ring` | `S_HM_ring_top` | `$macro_list` | VSS VDD | M6/M7 |
   | rail | `std_rail_conn1` | `std_rail_1` | core | VDD VSS | M1 |

2. 找出三种结构里金属带宽度的差异：mesh=0.2、ring=1、rail=0.094，并思考「为什么 ring 最粗、rail 最细」。

**需要观察的现象**：三种结构的 `compile_pg` 是**依次单独调用**的，每个 `compile_pg` 只编译一个 strategy。

**预期结果**：你会确认 ICC2 的设计哲学——pattern 是模板（可复用），strategy 决定「在哪用、给谁」，compile 才落地。本讲不实际运行工具，金属是否正确生成 **待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 mesh 的 `spacing: interleaving`（交错排布）比「所有 VDD 带聚一堆、所有 VSS 带聚一堆」更好？

> **参考答案**：交错排布让 VDD 带和 VSS 带相邻交替，二者之间形成紧密的电容耦合（去耦电容），能吸收瞬态电流尖峰，降低动态 IR drop；同时每个区域的单元到最近的 VDD/VSS 带距离都较短，静态压降也更均匀。若同种网络聚堆，远离的单元 IR drop 会显著变大。

**练习 2**：`set_pg_strategy` 的 `-core` 和 `-macros $macro_list` 两个开关分别控制什么？

> **参考答案**：`-core` 表示这套策略作用于整个 core 区域（mesh 覆盖全 core、rail 沿全 core 标准单元行）；`-macros $macro_list` 表示策略只作用于指定的宏单元列表（ring 只绕这 4 个 SRAM）。同一个 pattern 模板可以分别被 `-core` 和 `-macros` 两种策略引用，这正是「模板复用」的价值。

---

### 4.2 compile_pg 编译

#### 4.2.1 概念说明

`compile_pg` 是电源网络搭建的「执行引擎」。前面定义的 pattern 和 strategy 都是**描述性**的——它们只声明意图，并不会在版图上产生金属。只有调用 `compile_pg -strategies {策略名}` 后，ICC2 才会根据策略真正生成 mesh / ring / rail 的金属走线、打孔（via），并完成网络连通。

一个关键点是：**compile_pg 是按 strategy 逐个执行的**。本仓库 mesh、ring、rail 各调一次 `compile_pg`，顺序固定为 mesh → ring → rail。

#### 4.2.2 核心流程

```
compile_pg -strategies {S_default_vddvss}   # ① 先铺顶层 mesh（M6/M7 网格）
remove_routing_blockages *                    # ② 清掉为避免与宏单元冲突而临时设的布线阻挡
compile_pg -strategies {S_HM_ring_top}       # ③ 再给宏单元画 ring
create_routing_blockage -layers * ...        # ④ 在 ring 外围再加一片全层阻挡
compile_pg -strategies std_rail_1             # ⑤ 最后铺底层 rail（M1），接标准单元 pin
```

#### 4.2.3 源码精读

[IC Compiler II/PnR.tcl:97-111](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L97-L111) —— 三次 `compile_pg` 的调用序列：

- L97 `compile_pg -strategies {S_default_vddvss}`：编译顶层 mesh。
- L98 `remove_routing_blockages *`：mesh 画完后清掉此前为避开宏单元而临时设置的布线阻挡（这些阻挡在 L77–L84 创建，目的是让 mesh 不穿过宏单元，改由 ring 专门供电）。
- L105 `compile_pg -strategies {S_HM_ring_top}`：编译宏单元 ring。
- L106 `create_routing_blockage -layers * -boundary {{45 425} {305 617}}`：在 ring 区域外加一片覆盖所有层的布线阻挡，防止后续信号布线穿过宏单元上方。
- L111 `compile_pg -strategies std_rail_1`：编译底层 rail。

> 为什么是这个顺序？mesh 在顶层金属，先铺；ring 也在 M6/M7 但限定在宏单元周围，需要 mesh 骨架就位后再补；rail 在最底层 M1，要等上面结构稳定后再铺，才能正确通过 via 把 rail 连到 mesh/ring。从粗到细、从上到下。

#### 4.2.4 代码实践

**实践目标**：弄清三次 `compile_pg` 各自产出什么、以及中间穿插的两条 routing blockage 命令的作用。

**操作步骤（源码阅读型）**：

1. 在 `PnR.tcl` 中分别定位三次 `compile_pg`（L97、L105、L111）。
2. 对照 L75–L84（创建阻挡）和 L98（清除阻挡）、L106（再次创建阻挡），画出这条「设阻挡 → 画 mesh → 清阻挡 → 画 ring → 再设阻挡 → 画 rail」的时间线。
3. 思考：为什么画 mesh 前要设阻挡、画完后立刻清掉？

**预期结果**：你会理解「routing blockage 是电源网络的临时护栏」——画 mesh 时禁止金属穿过宏单元，画完 mesh 后宏单元改由 ring 供电，于是撤掉护栏让 ring 能在宏单元周围生成。

**待本地验证**：实际阻挡是否生效、metal 是否生成，需在 ICC2 GUI 中观察。

#### 4.2.5 小练习与答案

**练习 1**：如果把三次 `compile_pg` 合并成一条 `compile_pg -strategies {S_default_vddvss S_HM_ring_top std_rail_1}`，会有什么潜在问题？

> **参考答案**：合并后无法在 mesh 与 ring 之间穿插 `remove_routing_blockages`（L98）。脚本作者刻意把 mesh 编译和 ring 编译**拆开**，中间插一条清阻挡命令，是因为画 ring 前需要先撤掉为 mesh 设的临时护栏。合并编译会丢失这个时序控制点，可能导致 ring 与宏单元区域布线冲突或 mesh 残留不期望的阻挡。

**练习 2**：为什么 rail 放在最后编译？

> **参考答案**：rail 在最底层 M1，需要通过 via 向上连接到 mesh/ring 才能真正通电。若先画 rail，上层 mesh/ring 还没就位，via 无法正确生成。从顶层往底层、从粗到细的编译顺序保证了每一层结构都能正确连到已存在的上层供电骨架。

---

### 4.3 虚拟 pad 放置：Vpad.tcl 的双 for 循环

#### 4.3.1 概念说明

IR drop 分析需要知道**电流从哪里注入**电源网络。真实芯片的电源是通过封装的电源 pad / bump 灌进来的，但在做 PnR 阶段的早期评估时，pad 还没确定（甚至没有）。`set_virtual_pad` 就用来**人为指定一批「虚拟电源 pad」**——它们不代表真实的物理 pad，只是告诉 IR 分析工具「假设电流从这些坐标点注入」。

`Vpad.tcl` 是一段独立脚本，用两个 `for` 循环沿 die 的四条边均匀撒一批虚拟 pad，作为 `analyze_power_plan` 的电流源。它和 `PnR.tcl` 的 L114–L134 是同一段代码。

#### 4.3.2 核心流程

```
1. 取 core 的外接框 bbox → die_llx/lly/urx/ury
2. for 循环沿「下边 + 上边」（x 方向递增）
     每 80 步放一组：底边 VSS@i, VDD@i+40；顶边 VSS@i, VDD@i+40
3. for 循环沿「左边 + 右边」（y 方向递增）
     每 80 步放一组：左边 VSS@i, VDD@i+40；右边 VSS@i, VDD@i+40
4. analyze_power_plan ... -use_terminals_as_pads  # 用这些虚拟 pad 做电流注入
```

#### 4.3.3 源码精读

[IC Compiler II/Vpad.tcl:1-4](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Vpad.tcl#L1-L4) —— 取 core 外接矩形（bbox）的四个坐标，存进命名为 `die_llx/lly/urx/ury` 的变量。

> ⚠️ 命名陷阱：变量名叫 `die_*`，但取的是 `get_core_area` 的 bbox，而非 `get_die_area`。在本仓库 floorplan 里 die=`{{0 0} {400 400}}`、core_offset=`{15 15 15 15}`，所以 core 框约为 `{{15 15} {385 385}}`——也就是说这批「die」坐标实际是 core 的边界。读代码时不要被名字误导。

[IC Compiler II/Vpad.tcl:6-12](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Vpad.tcl#L6-L12) —— 第一个 `for` 循环：横坐标 `i` 从 `die_llx+20` 开始，步长 80，直到 `< die_urx-40` 停止。每次迭代在**下边**（y=die_lly）和**上边**（y=die_ury）各放一对 pad：VSS 在 `i`、VDD 在 `i+40`。

[IC Compiler II/Vpad.tcl:13-19](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Vpad.tcl#L13-L19) —— 第二个 `for` 循环：纵坐标 `i` 从 `die_lly+20` 开始，步长 80，直到 `< die_ury-40` 停止。每次迭代在**左边**（x=die_llx）和**右边**（x=die_urx）各放一对 pad：VSS 在 `i`、VDD 在 `i+40`。

> pad 间距的数学：在一次迭代里，VSS 落在 `i`，VDD 落在 `i+40`，所以**相邻 pad（不分网络）间距为 40**，VSS 与下一个 VSS（下个迭代的 `i+80`）间距为 **80**。四条边形成 VSS、VDD 交替的密集电源「围栏」。

#### 4.3.4 代码实践（本讲的核心实践任务）

**实践目标**：手算验证 `Vpad.tcl` 沿 die 四边放置了多少个虚拟 pad、间距是多少。

**操作步骤**：

1. 假设 core 框为 `{{15 15} {385 385}}`（即 `die_llx=15, die_lly=15, die_urx=385, die_ury=385`）。
2. 套入第一个 `for` 循环：起点 `die_llx+20 = 35`，步长 80，条件 `i < die_urx-40 = 345`。
   - 列出 `i` 的取值序列：35 → 115 → 195 → 275（下一个 355 已 ≥ 345，停止）。
   - 即 4 次迭代，每次迭代在上下两边各放 4 个 pad（2 VSS + 2 VDD）。
3. 套入第二个 `for` 循环：参数完全对称（lly=15, ury=385），同样是 4 次迭代，左右两边各放 4 个 pad。
4. 统计总数与间距。

**需要观察的现象 / 预期结果**：

- 每条边上 pad 的 x（或 y）坐标：下边为 `35(VSS), 75(VDD), 115(VSS), 155(VDD), 195(VSS), 235(VDD), 275(VSS), 315(VDD)`——共 8 个 pad。
- **相邻 pad 间距（VSS↔VDD 交替）= 40μm；同网络 pad 间距（VSS↔VSS）= 80μm。**
- 四条边共约 **32 个虚拟 pad**（每条边 8 个 × 4 边）。注意四角附近会与垂直边的 pad 临近，实际去重后总数可能略少，**精确数字待本地验证**。
- 坐标用 `[format {%.1f %.1f} ...]` 格式化成一位小数，是为了满足 ICC2 坐标精度（数据库单位）要求。

> 结论：这是一个**双密度交错**的虚拟 pad 布局——同网络每隔 80μm、相邻每隔 40μm，相当于让 VDD 和 VSS 都有足够密的电流注入点，便于 IR 分析得到贴近真实的压降分布。

**待本地验证**：以上坐标与计数基于 floorplan 边界 `{{0 0} {400 400}}` 推算；若实际 core bbox 不同，pad 数量会随之变化。可用 ICC2 的 `report_virtual_pads`（或 GUI）核对。

#### 4.3.5 小练习与答案

**练习 1**：如果把循环步长从 80 改成 160（其它不变），虚拟 pad 会变密还是变疏？对 IR drop 分析结果有何影响？

> **参考答案**：步长 160 意味着 `i` 跳得更远，pad 数量约减半，变**疏**。电流注入点变少后，每个 pad 要承担更大电流、覆盖更远区域，分析得到的 IR drop 会**变大**（更悲观）。反之步长越小、pad 越密，IR drop 越小（更乐观）。所以 pad 密度是 IR 分析的一个调节旋钮，应贴近真实封装的电源 pad 分布。

**练习 2**：为什么变量名是 `die_*` 却取了 `get_core_area`？这会造成什么实际影响？

> **参考答案**：这是脚本的命名不严谨——名字暗示取 die 边界，实际取的是 core 边界。由于本仓库 die=`{{0 0}{400 400}}`、core_offset 15，core 比 die 内缩 15μm，于是虚拟 pad 实际落在 core 框上而非 die 外框上。影响是：pad 离供电区域更近，IR 分析会比「真放在 die 外框」略乐观。真实项目应明确区分 `get_die_area` 与 `get_core_area`，按封装 pad 的实际位置选对。

---

### 4.4 IR drop 分析：analyze_power_plan

#### 4.4.1 概念说明

虚拟 pad 撒好后，`analyze_power_plan` 把电源网络当作一个**电阻网络**，在每个 pad 注入电流（总电流由功耗预算和电压换算），用类似 SPICE 的方法解出网格上每个节点的电压，从而得到**电压分布图**和**最大 IR drop**。这是 placement 阶段早期就能做的「电源网络体检」，比跑完整动态仿真快得多。

总注入电流由功耗预算与电压决定：

\[
I_{\text{total}} = \frac{P_{\text{budget}}}{V_{\text{dd}}}
\]

本仓库 `power_budget 250`（单位通常为 mW）、`voltage 1.2`（V），则：

\[
I_{\text{total}} = \frac{250\,\text{mW}}{1.2\,\text{V}} \approx 208.3\,\text{mA}
\]

这约 208mA 的总电流被分摊到所有标准单元（按各自的功耗权重），再从虚拟 pad 注入、沿 mesh/rail 流动，沿途产生压降。

#### 4.4.2 核心流程

```
撒虚拟 pad（4.3 节）
   ↓
analyze_power_plan
   -power_budget 250          # 总功耗预算（电流的来源）
   -voltage 1.2                # 标称供电电压
   -nets {VDD VSS}             # 对哪些电源网络做分析
   -use_terminals_as_pads      # 把上面撒的虚拟 pad 当作电流注入点
   ↓
输出：电压分布图 + 最大/平均 IR drop 报告
```

#### 4.4.3 源码精读

[IC Compiler II/Vpad.tcl:21](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Vpad.tcl#L21) —— `analyze_power_plan -power_budget 250 -voltage 1.2 -nets {VDD VSS} -use_terminals_as_pads`：以 250mW 预算、1.2V 电压，对 VDD/VSS 网络做电源计划分析，并用前面 `set_virtual_pad` 撒的 pad 作为电流注入点。

[IC Compiler II/PnR.tcl:134](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L134) —— 同一条 `analyze_power_plan`，是 `Vpad.tcl` 在主脚本里的对应副本，位于 mesh/ring/rail 三个 `compile_pg` 全部完成之后（L97/105/111）。

> 关键时序：IR 分析必须在**电源网络物理金属铺好后**才有意义——否则没有电阻网络可解。所以 `analyze_power_plan` 出现在三次 `compile_pg` 之后。这也是为什么本仓库把「撒 pad + 分析」整段放在电源金属编译完成的 L114–L134。

各参数含义：

| 参数 | 值 | 含义 |
|------|-----|------|
| `-power_budget` | 250 | 总功耗预算（mW），决定总注入电流 |
| `-voltage` | 1.2 | 标称电源电压（V） |
| `-nets` | {VDD VSS} | 分析的电源/地网络 |
| `-use_terminals_as_pads` | — | 把 `set_virtual_pad` 的点当作电流注入终端 |

#### 4.4.4 代码实践

**实践目标**：理解 IR drop 分析需要哪些输入、回答什么问题。

**操作步骤（源码阅读型）**：

1. 在 `PnR.tcl` 中确认 `analyze_power_plan`（L134）出现在三次 `compile_pg`（L97/105/111）之后，思考「为什么不能更早」。
2. 假设 `power_budget` 提高到 500mW，按公式 \(I = P/V\) 重算总电流，并预测 IR drop 会变大还是变小。

**预期结果**：

- `power_budget` 翻倍 → 总电流翻倍（约 416mA）→ 在相同电阻网络下 \(V_{\text{drop}} = IR\) 也翻倍 → **IR drop 翻倍**。
- 反之，若想降低 IR drop，可以：加粗/加密 mesh（降 R）、增加虚拟 pad 密度（缩短电流路径）。

**待本地验证**：实际最大/平均 IR drop 数值需在 ICC2 中运行 `analyze_power_plan` 并查看报告（一般会输出一个电压分布彩图与 worst drop 百分比）。

#### 4.4.5 小练习与答案

**练习 1**：`analyze_power_plan` 为什么放在 `compile_pg` 之后，而不是 mesh pattern 定义之后？

> **参考答案**：pattern 和 strategy 只是描述性声明，没有产生金属。`compile_pg` 之后才有真实的金属走线和 via，才能构成可求解的电阻网络。在 compile 前分析，工具没有物理电阻数据可解，无法得到 IR drop。

**练习 2**：如果 `analyze_power_plan` 报告 worst IR drop 超标，你会优先调哪两个旋钮？

> **参考答案**：（1）降低电阻——加粗 mesh 的 `width`、减小 `pitch`（铺更密），或增加金属层并行供电；（2）优化电流注入——若 pad 分布不合理（如某条边 pad 过稀），调整 `Vpad.tcl` 的步长/起始点让 pad 更均匀。通常先看电压分布图定位「热点」，再对症加密该区域 mesh。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，画出本仓库电源网络的「从逻辑到金属到体检」完整流程图，并解释每一处 Tcl 命令的归属。

**步骤**：

1. 在一张纸上从上到下面出五个阶段方框：
   - ① 逻辑建网（`create_net` + `connect_pg_net`）
   - ② mesh 物理金属（`create_pg_mesh_pattern` + `set_pg_strategy -core` + `compile_pg`）
   - ③ ring 物理金属（`create_pg_ring_pattern` + `set_pg_strategy -macros` + `compile_pg`）
   - ④ rail 物理金属（`create_pg_std_cell_conn_pattern` + `set_pg_strategy -core` + `compile_pg`）
   - ⑤ IR 体检（`set_virtual_pad` × 双循环 + `analyze_power_plan`）
2. 在每个方框旁标注它在 `PnR.tcl` 的行号区间（① L42–46；② L86–98；③ L102–106；④ L109–111；⑤ L114–134）。
3. 标注穿插其中的两条 routing blockage 命令（L98 清除、L106 再设）属于哪个阶段之间。
4. 写一句话总结：为什么这个顺序是「逻辑→粗（mesh）→宏（ring）→细（rail）→体检」。

**预期结果**：你应当能用一张图说清 ICC2 电源网络的完整搭建逻辑，并指出每个最小模块对应的真实命令与行号。实际数值（metal 是否生成、IR drop 多少）**待本地在 ICC2 中验证**。

---

## 6. 本讲小结

- ICC2 电源网络分**逻辑层**（`create_net`/`connect_pg_net` 建网挂引脚）与**物理层**（`compile_pg` 生成金属）两步，逻辑在前、物理在后。
- 物理金属走**三段式套路**：`create_pg_*_pattern`（定义模板）→ `set_pg_strategy`（绑定范围+net）→ `compile_pg`（落地成金属）。
- 本仓库搭了 mesh（M6/M7 全 core 网格）、ring（M6/M7 绕 4 个 SRAM 宏单元）、rail（M1 标准单元行细轨）三种结构，编译顺序固定为 mesh → ring → rail，从粗到细、从上到下。
- mesh 用 `spacing: interleaving` 让 VDD/VSS 交错排布，ring 用更粗的 1μm 线宽应对宏单元大电流，rail 用 0.094μm 细轨入户。
- `Vpad.tcl` 用两个 `for` 循环（步长 80、相邻 pad 间距 40、同网络间距 80）沿 die 四边均匀撒虚拟电源 pad，作为 IR 分析的电流注入点；其内容与 `PnR.tcl` L114–L134 是同一份代码。
- IR drop 遵循 \(V_{\text{drop}}=IR\)，`analyze_power_plan` 以 `power_budget`/`voltage` 换算总电流（约 208mA），必须在 `compile_pg` 之后运行；IR 超标时优先加粗/加密 mesh 或调整 pad 密度。

## 7. 下一步学习建议

- **下一讲 u4-l4 布局 Placement**：电源网络铺好后进入 `place_opt`，正式做标准单元布局优化与时序收敛。届时你会看到 `connect_pg_net` 在 filler 插入后**再次调用**（PnR.tcl L151–152、L183–184）——因为新加入的 filler/filler cell 也要接电，这正是本讲「逻辑建网」模式的延伸。
- **横向阅读建议**：对照 `mentor_scripts/` 的 Nitro 流程，看 Mentor 工具如何用不同命令完成等价的电源网络搭建（详见 u5-l2），体会「电源网络三段式」是 ICC2 特有还是 EDA 通用思想。
- **源码延伸**：可继续读 `IC Compiler/02_preroute.tcl`（旧版 ICC 的电源 rail 综合命令），对比 ICC2 的 `compile_pg`，理解工具演进（u5-l1）。
