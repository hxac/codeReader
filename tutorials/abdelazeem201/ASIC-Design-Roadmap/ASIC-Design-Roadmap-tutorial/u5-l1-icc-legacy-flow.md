# Synopsys ICC 传统流程

## 1. 本讲目标

本讲把视线从 ICC2（IC Compiler II，U4 主流程所用的新一代工具）拉回到它的**前身 IC Compiler（ICC）**。学完本讲后，读者应该能够：

1. 读懂 ICC 用 Tcl 完成一次「带 IO pad 的芯片级」布图规划与电源规划的完整脚本。
2. 理解 `create_floorplan` 如何用利用率/长宽比定 core 尺寸，以及它与 ICC2 `initialize_floorplan` 的差别。
3. 理解 IO pad 的物理约束（TDF 文件）、corner pad、pad filler 的作用。
4. 理解 ICC 用 `set_fp_rail_constraints` / `synthesize_fp_rail` / `commit_fp_rail` 三件套做电源环 + strap 综合，并用 `power_budget` 反推电流、估算 IR drop。
5. 理解 `preroute_standard_cells` 搭标准单元电源轨、`add_tap_cell_array` 插 well tap 的用意。
6. 把 ICC 与 ICC2 在**库名、设计对象、布图、电源命令**上的演进列成一张对照表，体会 EDA 工具的迭代逻辑。

---

## 2. 前置知识

本讲承接 **u4-l2 布局规划 Floorplan**，默认读者已经掌握：

- ICC2 里 `initialize_floorplan` 画 die/core、利用率（utilization）的概念；
- `create_placement -floorplan` 试摆、`legalize_placement` 合法化；
- die / core 边界、core_offset 留白、`flip_first_row` 这些布图术语（见 u4-l2、u4-l1）；
- 电源网络「逻辑层建网 + 物理层穿金属」的两步范式，以及 IR drop 遵循 \(V=IR\)（见 u4-l3）。

下面先澄清两个**关键背景**，否则会把 ICC 与 ICC2 的脚本读混。

### 2.1 ICC vs ICC2：Milkyway 与 NDM

ICC（也叫 Astro 的后继）使用 **Milkyway（.mw）** 格式的物理库，设计存成 **mw_cel**（Milkyway cell），所以脚本里到处是 `open_mw_lib` / `save_mw_cel`。

ICC2 改用全新的 **NDM** 格式，设计存成 **block**，对应 `open_lib` / `save_block`（U4 里反复出现的 `save_block`）。

这是两代工具最本质的分野：**命令名变了的根本原因，是底层数据模型换了。**

### 2.2 芯片级（带 IO pad）vs 核级（无 IO pad）

U4 的 `PnR.tcl` 是一个**核级（core-level）设计**：里面只有 SRAM 宏单元，没有 IO pad，端口直接落在 core 边界上。

而本讲的 ICC 脚本是一个**芯片级（chip-level）设计**：四边排满了真实的 IO pad（电源 pad、信号 pad、corner pad）。这是为什么 ICC 脚本里多出大量 IO pad 相关命令——这是「设计类型」带来的差异，不全是「工具版本」带来的差异。读源码时要把这两种差异分开看。

> 提示：脚本里电压注释 `0.9V for T40`、`insert well tap for TN40G` 表明这是一个 **40nm（TSMC TN40G）** 工艺的设计；脚本里偶尔出现的 `T90`（90nm）字样是作者保留的旧注释，以 0.9V / TN40G 为准。

---

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
|---|---|---|
| `IC Compiler/create_phy_cell.tcl` | 21 行 | 在网表里**创建物理 IO pad 单元**（电源/地 pad、corner pad、POC pad）的实例 |
| `IC Compiler/io.tdf` | 141 行 | **IO pad 物理约束文件**（TDF），规定每个 pad 落在哪条边、第几号、左右间距 |
| `IC Compiler/01_design_planning.tcl` | 127 行 | **布图规划主脚本**：建 pad → 读 TDF → `create_floorplan` → pad filler → 试摆 → 电源环/strap 综合 |
| `IC Compiler/02_preroute.tcl` | 86 行 | **预布线脚本**：连 IO pad/宏单元环到电源环、搭标准单元 rail、插 well tap、CTS 前零互连时序检查 |

执行顺序是：先跑 `01_design_planning.tcl`（得到 `design_planning` 快照），再跑 `02_preroute.tcl`（得到 `preroute` 快照），之后才进入 CTS。

---

## 4. 核心概念与源码讲解

### 4.1 create_floorplan 与利用率

#### 4.1.1 概念说明

布图规划（floorplan）回答「芯片画多大、core 在哪、留多少边距」。

ICC 用 **`create_floorplan`** 一次性给出三件事：

- **core 长宽比** `core_aspect_ratio`：core 的宽/高比，1 表示正方形 core；
- **利用率** `core_utilization`：标准单元与宏单元面积之和 ÷ core 面积，决定 core 要留多少余量；
- **core 到 pad 的边距**：core 边界到 IO pad 环的距离。

利用率越高，core 越小、芯片越便宜，但越拥挤、越难布线。0.6 是一个偏保守、好收敛的值。

#### 4.1.2 核心流程

`create_floorplan` 在 ICC 里的定尺寸逻辑是「**写死利用率，工具算面积**」：

\[
\text{core\_area} \approx \frac{\text{cell\_area}}{\text{utilization}}
\]

读入综合后的网表时，工具已知所有标准单元与宏单元的总面积；给定 `core_utilization 0.6`，就能反推 core 面积，再配合 `core_aspect_ratio 1` 得到正方形 core 的边长。`-flip_first_row` 让第一行标准单元翻转方向（行翻转，与 ICC2 `initialize_floorplan -flip_first_row true` 同义，见 u4-l1）。

`left/right/bottom/top_io2core` 给的是 **core 到 IO pad 环的四向距离**（这里统一 90μm），这正是芯片级设计才需要的参数——核级设计只有 `core_offset`（core 到 die 边），没有 pad 距离。

#### 4.1.3 源码精读

[IC Compiler/01_design_planning.tcl:25-26](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/01_design_planning.tcl#L25-L26) —— 画 die 与 core、给利用率与 core-pad 边距：

```tcl
set CORE_PAD_SPACE 90 ;
create_floorplan -core_aspect_ratio 1 -core_utilization 0.6 -flip_first_row \
  -left_io2core $CORE_PAD_SPACE -right_io2core $CORE_PAD_SPACE \
  -bottom_io2core $CORE_PAD_SPACE -top_io2core $CORE_PAD_SPACE
```

紧接着是「第一次编译」——布图阶段的试摆，等价于 ICC2 的 `create_placement -floorplan -timing_driven`：

[IC Compiler/01_design_planning.tcl:49-50](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/01_design_planning.tcl#L49-L50) —— 多核、时序驱动的布图试摆：

```tcl
set_host_options -max_cores 4
create_fp_placement -timing_driven -effort high -num_cpus 4
```

`create_fp_placement`（fp = floorplan）就是 ICC 的「布图阶段试摆」命令，注意名字里的 `_fp_` 前缀——ICC 把布图阶段的命令都冠以 `fp`，而 ICC2 改成把 `-floorplan` 作为开关（`create_placement -floorplan`）。

#### 4.1.4 代码实践

**实践目标**：体会利用率对 core 面积的反向决定作用。

**操作步骤**：

1. 打开 `01_design_planning.tcl` 第 26 行，找到 `-core_utilization 0.6`。
2. 假设网表里所有标准单元 + 宏单元总面积为 `S`，写出 core 面积公式。
3. 分别把利用率改成 `0.5`、`0.7`、`0.85`，在纸上估算 core 面积会如何变化。

**需要观察的现象 / 预期结果**：

\[
\text{core\_area}(0.6) = \frac{S}{0.6} \approx 1.67\,S,\quad
\text{core\_area}(0.85) = \frac{S}{0.85} \approx 1.18\,S
\]

利用率从 0.6 提到 0.85，core 面积缩到约 71%，芯片更便宜，但留给电源 mesh 与布线的空间大幅缩水，拥塞与时序会更紧张。**这是一个待本地验证的纸面估算**：真实 core 面积还受 `core_aspect_ratio`、pad 环形状约束，需在 ICC 里跑 `report_area` / `report_utilization` 才能得到准确值。

#### 4.1.5 小练习与答案

**练习 1**：为什么核级设计（ICC2 `PnR.tcl`）的 `initialize_floorplan` 没有 `*_io2core` 参数？
**答案**：核级设计没有 IO pad 环，core 直接贴 die 边，只需要 `core_offset`（core 到 die 边的留白）；`io2core` 是 core 到 pad 环的距离，只有芯片级设计才有 pad 环才用得上。

**练习 2**：把 `core_utilization` 调到 0.95 一定能让芯片成本最低吗？
**答案**：不一定。利用率过高 → core 拥塞 → 走不通需要加大面积或退回综合，反而更慢更贵；成本最低点是「在能收敛时序/布线的前提下尽量高」，0.6 这种保守值是为收敛留余地。

---

### 4.2 IO pad 约束与 pad filler

#### 4.2.1 概念说明

芯片级设计要把每一个对外端口做成一个**物理 IO pad 单元**（较大的物理结构，含 ESD 保护、电平转换）。这些 pad 不是随便摆的，要遵守两类约束：

- **实例先得存在**：网表里得先 `create_cell` 把 pad 单元实例化出来（电源 pad、corner pad、POC pad 等不一定在 RTL 里，需要脚本补建）；
- **物理位置约束**：每个 pad 落在芯片第几条边（side 1~4）、第几号（order）、左右最小间距（iospace）。

此外，四角必须放 **corner pad**（PCORNER）把 pad 环拐成封闭矩形；pad 之间留的缝要用 **pad filler** 填满，保证电源轨在 pad 环上连续。

#### 4.2.2 核心流程

ICC 的 IO pad 流程是「**先建实例，再读 TDF 约束，再补 filler**」三步：

1. `source create_phy_cell.tcl` → `create_cell` 把电源/地/corner/POC pad 实例化进网表；
2. `read_pin_pad_physical_constraints $TDF_FILE` → 读 TDF，把 pad 钉到指定边和顺序上；
3. `insert_pad_filler` → 用一串不同宽度的 filler pad 把 pad 之间的缝填满，最小的 filler 当 overlap 收尾。

TDF（pad physical constraint）文件里每一行就是「**某个 pad 名 → 第几边 → 第几号 → 左右 iospace**」的逐条登记。

#### 4.2.3 源码精读

[IC Compiler/create_phy_cell.tcl:2-13](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/create_phy_cell.tcl#L2-L13) —— 列出所有要补建的 pad 实例名，分 core 电源、IO 电源、corner、POC 四类：

```tcl
set CORE_POWER_LIST  [list core_vdd1 core_vdd2 core_vdd3 core_vdd4]
set CORE_GROUND_LIST [list core_vss1 core_vss2 core_vss3 core_vss4]
set PAD_POWER_LIST   [list io_vdd01 ... io_vdd19]
set PAD_GROUND_LIST  [list io_vss01 ... io_vss20]
set CORNER_LIST      [list cornerUL cornerUR cornerLR cornerLL]   ;# 四角
set POC_LIST         [list io_vdd20]                               ;# 上电控制
```

[IC Compiler/create_phy_cell.tcl:15-20](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/create_phy_cell.tcl#L15-L20) —— 按主单元（master）批量 `create_cell`，注意 PVDD1/PVDD2 区分 core 电源域与 IO 电源域：

```tcl
create_cell $CORE_POWER_LIST  PVDD1DGZ    ;# core 电源 pad
create_cell $CORE_GROUND_LIST PVSS1DGZ    ;# core 地 pad
create_cell $PAD_POWER_LIST   PVDD2DGZ    ;# IO 电源 pad
create_cell $PAD_GROUND_LIST  PVSS2DGZ    ;# IO 地 pad
create_cell $CORNER_LIST      PCORNER     ;# 四角单元
create_cell $POC_LIST         PVDD2POC    ;# Power-on Control
```

回到布图脚本，先 source 它再读 TDF：

[IC Compiler/01_design_planning.tcl:16-21](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/01_design_planning.tcl#L16-L21) —— 先建 pad 实例，再设 iospace 并读 TDF：

```tcl
source $CREATE_CELL_FILE                              ;# 必须先 create_cell
set IO_SPACE 8.5 ; # 5~10
read_pin_pad_physical_constraints $TDF_FILE
```

[IC Compiler/io.tdf:2-5](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/io.tdf#L2-L5) —— 四个 corner pad 分别钉在四条边，corner 决定 pad 环的四个拐角：

```tcl
set_pad_physical_constraints -side 1 -pad_name cornerUL
set_pad_physical_constraints -side 2 -pad_name cornerUR
set_pad_physical_constraints -side 3 -pad_name cornerLR
set_pad_physical_constraints -side 4 -pad_name cornerLL
```

[IC Compiler/io.tdf:10-13](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/io.tdf#L10-L13) —— side 1 的前几个 pad：`-order` 定顺序、`-min_left/right_iospace $IO_SPACE` 定左右间距（`io_vdd20` 是 POC pad 排第一）：

```tcl
set_pad_physical_constraints -side 1 -pad_name io_vdd20         -order 1  -min_left_iospace $IO_SPACE -min_right_iospace $IO_SPACE
set_pad_physical_constraints -side 1 -pad_name ipad_F_opt_Im_12 -order 2  -min_left_iospace $IO_SPACE -min_right_iospace $IO_SPACE
...
```

最后用 pad filler 填缝：

[IC Compiler/01_design_planning.tcl:30](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/01_design_planning.tcl#L30) —— 按从宽到窄的顺序填缝，`PFILLER0005` 当 overlap_cell 兜底：

```tcl
insert_pad_filler -cell {PFILLER20 PFILLER10 PFILLER5 PFILLER1 PFILLER05 PFILLER0005} \
                  -overlap_cell {PFILLER0005}
```

#### 4.2.4 代码实践

**实践目标**：理解 TDF 如何把一堆 pad 排成四条封闭的边。

**操作步骤**：

1. 打开 `io.tdf`，统计每条边（side 1~4）实际登记了多少个 pad（含被注释掉的）。
2. 找出四个 corner pad（cornerUL/UR/LR/LL）分别在哪条边。
3. 观察 side 1 里 `io_vdd20`（POC）的 order 是几，并解释它为什么必须紧挨 corner。

**需要观察的现象 / 预期结果**：每条边约 30 个 pad，corner pad 各占一边端点把 pad 环封口；POC pad `io_vdd20` 排在 side 1 的 order 1，紧贴 cornerUL，因为上电控制需要尽早接到 IO 电源域。**这是源码阅读型实践**，无需运行工具即可完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `read_pin_pad_physical_constraints` 必须在 `source create_phy_cell.tcl` 之后？
**答案**：TDF 约束的对象是 pad **实例**；只有先用 `create_cell` 把实例建出来，约束命令才能按 `pad_name` 找到目标并钉位置，否则会报找不到实例的错误。

**练习 2**：`overlap_cell {PFILLER0005}` 是什么意思？
**答案**：填缝时多个 filler 之间或与 pad 之间可能出现无法用大 filler 填满的窄缝，`overlap_cell` 指定用最小的 filler（PFILLER0005）以允许重叠的方式把残余缝隙填实，保证 pad 环电源轨连续。

---

### 4.3 电源环 strap 综合与 IR

#### 4.3.1 概念说明

ICC 的电源网络物理实现走的是「**约束 → 综合 → 提交**」三件套，与 ICC2「pattern → strategy → compile」三段式（u4-l3）是同构的，只是命令名不同：

| 步骤 | ICC 命令 | ICC2 对应（u4-l3） |
|---|---|---|
| 设层/strap/环约束 | `set_fp_rail_constraints` | `create_pg_*_pattern` + `set_pg_strategy` |
| 综合落地 | `synthesize_fp_rail` | `compile_pg` |
| 固化为正式金属 | `commit_fp_rail` | （`compile_pg` 直接落地） |

电源网络要解决两层需求：**core 周围的 ring（环）**把电流从电源 pad 引到芯片内部，**贯穿 core 的 strap（电源条带）**把电流均匀分发到 core 各处。strap 在水平层（M6）和垂直层（M5）各布一簇，形成网格。

IR drop（电压降）遵循欧姆定律：

\[
V_{\text{drop}} = I \cdot R_{\text{mesh}}
\]

其中总电流由功耗预算与供电电压决定：

\[
I = \frac{P}{V}
\]

#### 4.3.2 核心流程

`synthesize_fp_rail` 在给定电压、功耗预算的条件下，估算总电流，自动选择 strap 数量与宽度，画出电源 mesh/ring，并可求解 IR drop。算完后用 `commit_fp_rail` 把「暂存」的电源结构固化为正式金属。

本仓库脚本里：
- 电压 `voltage_supply 0.9`（0.9V，40nm）；
- 功耗预算 `power_budget 30`（30 mW）。

由此估算总电流：

\[
I = \frac{30\,\text{mW}}{0.9\,\text{V}} \approx 33.3\,\text{mA}
\]

> 对比：u4-l3 的 ICC2 `Vpad.tcl` 例子用 `power_budget` 反推得到约 208mA；这里只有 33mA，说明本 ICC 设计规模更小。两者的 IR drop 都是「总电流 × 等效电阻网络」，原理一致。

#### 4.3.3 源码精读

先看 strap 与 ring 的约束定义。H_LAYER=M6（水平）、V_LAYER=M5（垂直）：

[IC Compiler/01_design_planning.tcl:63-71](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/01_design_planning.tcl#L63-L71) —— 给水平层 M6 加 strap 约束：10~15 条、宽 4μm、最小间距：

```tcl
set H_LAYER M6
set V_LAYER M5
set STRAP_WIDTH 4
set_fp_rail_constraints -add_layer -layer $H_LAYER \
    -direction horizontal \
    -max_strap 15 -min_strap 10 \
    -max_width $STRAP_WIDTH -min_width $STRAP_WIDTH \
    -spacing minimum
```

[IC Compiler/01_design_planning.tcl:79-87](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/01_design_planning.tcl#L79-L87) —— 设电源环：VDD/VSS 交替 4 对，环宽/间距/偏移各 4μm，`-extend_strap core_ring` 让 strap 接到 core ring：

```tcl
set_fp_rail_constraints -set_ring -nets \
    [format "%s %s %s %s %s %s %s %s" $POWER $GROUND $POWER $GROUND $POWER $GROUND $POWER $GROUND] \
    -horizontal_ring_layer $H_LAYER -vertical_ring_layer $V_LAYER \
    -ring_width 4 -ring_spacing 4 -ring_offset 4 \
    -extend_strap core_ring
```

[IC Compiler/01_design_planning.tcl:89-92](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/01_design_planning.tcl#L89-L92) —— 全局约束：不在硬宏上布电源、不自动加宽、环留在 core 外：

```tcl
set_fp_rail_constraints -set_global \
    -no_routing_over_hard_macros \
    -no_same_width_sizing \
    -keep_ring_outside_core
```

然后是综合与提交：

[IC Compiler/01_design_planning.tcl:110-113](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/01_design_planning.tcl#L110-L113) —— 电源综合：电压 0.9V、功耗预算 30mW，注释提醒「**之后要查 IR drop 图**」：

```tcl
synthesize_fp_rail -nets [list $POWER $GROUND] \
    -voltage_supply 0.9 \
    -synthesize_power_plan \
    -power_budget 30
```

[IC Compiler/01_design_planning.tcl:121](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/01_design_planning.tcl#L121) —— 把综合出的电源结构固化为正式金属：

```tcl
commit_fp_rail
```

#### 4.3.4 代码实践

**实践目标**：用功耗预算反推电流，体会 IR drop 估算的输入。

**操作步骤**：

1. 读第 110-113 行，记下 `voltage_supply` 与 `power_budget`。
2. 用 \(I=P/V\) 算出总电流（单位 mA）。
3. 假设该电源 mesh 在最远点的等效电阻为 \(R=2\,\Omega\)，估算最坏 IR drop（单位 mV）。

**预期结果**：

\[
I \approx \frac{30}{0.9} \approx 33.3\,\text{mA},\qquad
V_{\text{drop}} = 33.3\,\text{mA}\times 2\,\Omega \approx 66.6\,\text{mV}
\]

相对 0.9V 电源，66.6mV 约占 7.4%。**这是纸面估算，待本地验证**：真实等效电阻与最远点位置由 `synthesize_fp_rail` 求解电阻网络给出，需在 ICC 里看 IR drop map（脚本注释也提醒了）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 strap 要在水平层（M6）和垂直层（M5）各布一簇？
**答案**：电源要在二维平面上均匀覆盖 core。水平层走横线、垂直层走竖线，两层交叉形成网格，任何标准单元都能就近接到电源；只用一个方向会有覆盖不到的条带。

**练习 2**：`-no_routing_over_hard_macros` 在解决什么问题？
**答案**：硬宏（如 SRAM）内部有自己的金属，顶层的电源 strap 如果压在宏单元上方可能引发 DRC 或与宏内部金属冲突，所以禁止电源 strap 跨越硬宏。

---

### 4.4 preroute rail 与 well tap

#### 4.4.1 概念说明

`01_design_planning.tcl` 综合出了 core ring + strap mesh，但还差两件落地工作（由 `02_preroute.tcl` 完成）：

1. **把电源 pad、宏单元环接到电源 ring/mesh 上**——pad 提供电流入口，宏单元需要独立供电，这些「短线连接」用 `preroute_instances` 完成；
2. **搭标准单元电源轨（rail）**——标准单元的 VDD/VSS 脚贴在 M1 的横向电源轨上，这条轨要沿每行标准单元贯通，用 `preroute_standard_cells` 画出；
3. **插 well tap（阱接触）**——CMOS 工艺的阱（well）必须有规则间隔的接触连到电源，否则会出现 latch-up（闩锁效应）或衬底浮空，`add_tap_cell_array` 按固定间距插入 well tap 单元。

`preroute_*` 系列命令里的「pre-route」指的是「**在正式 signal routing（布线）之前**，先把电源/地这些特殊网络连好」。ICC2 里这些都被 `compile_pg` 的 rail/strap strategy 吸收了（u4-l3），不再有独立的 `preroute_standard_cells` 命令。

#### 4.4.2 核心流程

`02_preroute.tcl` 的电源落地顺序：

1. `preroute_instances`（忽略宏单元）→ 连 IO pad 到电源环；
2. `preroute_instances`（忽略 pad）→ 连宏单元 block ring 到电源环；
3. `preroute_standard_cells` → 沿标准单元行画 M1 电源轨，并贯通空行；
4. `set_pnet_options -complete` → 告诉工具「电源 mesh 所在层下面不准摆标准单元」，再 `create_fp_placement -incremental` 重摆一次（让布局主动避开电源条带）；
5. `derive_pg_connection` → 在逻辑层把电源网挂到所有单元引脚；
6. `add_tap_cell_array` → 每 20μm 插一个 well tap（FILLTIE4_A9TR），且不在 M1/M2 下插。

#### 4.4.3 源码精读

[IC Compiler/02_preroute.tcl:12-16](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/02_preroute.tcl#L12-L16) —— 连 IO pad 到电源环（忽略宏单元和封面单元），多层连接时延伸 16μm：

```tcl
preroute_instances  -ignore_macros \
                    -ignore_cover_cells \
                -primary_routing_layer pin \
                -extend_for_multiple_connections \
                -extension_gap 16
```

[IC Compiler/02_preroute.tcl:42-51](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/02_preroute.tcl#L42-L51) —— 搭标准单元横向电源轨：贯通空行、不在宏上走、删悬浮片段：

```tcl
preroute_standard_cells -extend_for_multiple_connections \
                        -extension_gap 16 -connect horizontal \
                        -remove_floating_pieces \
                        -do_not_route_over_macros \
                        -fill_empty_rows \
                        ...
                        -route_type {P/G Std. Cell Pin Conn}
```

[IC Compiler/02_preroute.tcl:61-65](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/02_preroute.tcl#L61-L65) —— 把电源 mesh 设为「禁摆标准单元」层后增量重摆，再补 well tap：

```tcl
set_pnet_options -complete $PWR_NET_LYR       ;# M6/M5 电源层下不准放标准单元
create_fp_placement -timing_driven -congestion_driven -incremental all
derive_pg_connection -power_net $POWER -ground_net $GROUND -power_pin $POWER -ground_pin $GROUND
add_tap_cell_array -master_cell_name {FILLTIE4_A9TR} -distance 20 \
                   -no_tap_cell_under_layers {M1 M2} ; # insert well tap for TN40G
```

> 注意 `set_pnet_options -complete` 与 `-partial` 的区别（脚本注释第 57-59 行）：`-complete` 表示电源 mesh **完全**压住该层、该层不许摆任何标准单元；`-partial` 允许部分标准单元压在电源条带下。生产流程多选 `-complete` 保证 IR 与可布线性。

之后脚本还做了 **CTS 前的零互连时序检查**（这是 u4-l2 讲过的「评估布图质量」手段在 ICC 里的等价做法）：

[IC Compiler/02_preroute.tcl:76-80](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/02_preroute.tcl#L76-L80) —— 把所有互连延迟置零，纯看逻辑时序，判布图是否合理：

```tcl
set_zero_interconnect_delay_mode true
report_timing > [format "%s%s%s" $RPT_PATH $TOP "_plan_timing.rpt" ]
report_constraint -all > [format "%s%s%s" $RPT_PATH $TOP "_plan_clk_con.rpt" ]
set_zero_interconnect_delay_mode false
```

这与 u4-l2 介绍的「零互连时序评估」是完全一样的思想：零互连下就违例，说明逻辑本身慢，加大 core 面积没用，要退回综合。

#### 4.4.4 代码实践

**实践目标**：弄清 well tap 的插入间距与禁插层。

**操作步骤**：

1. 读第 65 行，找出 well tap 的主单元名、间距、禁插层。
2. 解释为什么禁插层里列了 M1、M2。
3. 假设 core 宽 1000μm，按 `distance 20` 估算一行大约要插多少个 well tap。

**预期结果**：主单元 `FILLTIE4_A9TR`，间距 20μm，禁在 M1/M2 下插（避免与底层金属/标准单元电源轨冲突）；core 宽 1000μm 时，单行约 1000/20 = 50 个 well tap。**待本地验证**：实际数量还要看 core 尺寸与已占用区域。

#### 4.4.5 小练习与答案

**练习 1**：为什么要在 `preroute_standard_cells` 之后、用 `set_pnet_options -complete` 再增量重摆一次？
**答案**：电源 strap 画好后，其正下方的区域不能再放标准单元（否则压在电源条带上引发 DRC 与 IR 问题）。设 `-complete` 后增量重摆，让布局器主动把标准单元挪出电源条带覆盖区，保证电源网络与布局互不冲突。

**练习 2**：ICC2 里为什么看不到 `preroute_standard_cells` 这个命令？
**答案**：ICC2 把标准单元电源轨也纳入统一的 PG pattern/strategy 体系，由 `compile_pg` 一次性生成 rail/strap/ring（见 u4-l3），不再单设一条 preroute 命令；这是两代工具电源流程整合的体现。

---

### 4.5 ICC 与 ICC2 命令差异

#### 4.5.1 概念说明

把前面四节出现的命令汇总成一张对照表，就能看出 ICC → ICC2 的演进主线：**底层数据模型从 Milkyway 换成 NDM，导致库/设计对象/打开命令全部改名；电源与布图命令则从「分散多命令」收敛成「pattern + strategy + compile」的统一范式。**

读老脚本时，只要认得 ICC 命令对应的 ICC2 命令，就能把知识迁移过来。

#### 4.5.2 核心流程

迁移思路：先认**对象名**（mw_cel → block），再认**布图命令**（create_floorplan → initialize_floorplan），最后认**电源三件套**（set_fp_rail/ synthesize_fp_rail / commit_fp_rail → pattern/strategy/compile_pg）。少数命令（如 `add_tap_cell_array`、`derive_pg_connection`）ICC2 沿用，名字没变。

#### 4.5.3 源码精读（对照表）

下表左列均来自本讲 ICC 脚本的真实命令，右列对应 ICC2 `PnR.tcl`（U4）里的命令：

| 用途 | ICC（本讲脚本） | ICC2（U4 PnR.tcl） |
|---|---|---|
| 物理库格式 | Milkyway（.mw） | NDM（.ndm） |
| 设计对象 / 保存 | mw_cel → [save_mw_cel](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/01_design_planning.tcl#L125-L126) | block → `save_block`（u4-l1） |
| 打开设计 | `open_mw_lib` / `open_mw_cel`（脚本第 11-12 行注释掉） | `open_lib` / `open_block` |
| 布图初始化 | [create_floorplan](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/01_design_planning.tcl#L25-L26) | `initialize_floorplan`（u4-l1，PnR.tcl 第 38 行） |
| 布图阶段试摆 | [create_fp_placement](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/01_design_planning.tcl#L49-L50) | `create_placement -floorplan`（u4-l2，PnR.tcl 第 73 行） |
| 正式布局优化 | （ICC 里亦用 place_opt） | `place_opt`（u4-l4） |
| 电源约束 | [set_fp_rail_constraints](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/01_design_planning.tcl#L67-L92) | `create_pg_*_pattern` + `set_pg_strategy`（u4-l3） |
| 电源综合 | [synthesize_fp_rail](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/01_design_planning.tcl#L110-L113) | `compile_pg`（u4-l3） |
| 电源提交 | [commit_fp_rail](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/01_design_planning.tcl#L121) | （`compile_pg` 直接落地，无单独提交步骤） |
| PG 逻辑连接 | [derive_pg_connection](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/01_design_planning.tcl#L55) | `connect_pg_net`（u4-l3，PnR.tcl 第 45-46 行） |
| 标准单元电源轨 | [preroute_standard_cells](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/02_preroute.tcl#L42-L51) | `compile_pg`（rail pattern，u4-l3） |
| pad filler | [insert_pad_filler](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/01_design_planning.tcl#L30) | （核级设计无 pad；标准单元 filler 为 `create_stdcell_fillers`，见 u4-l7） |
| well tap | [add_tap_cell_array](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler/02_preroute.tcl#L65) | `add_tap_cell_array`（命令名沿用） |

#### 4.5.4 代码实践（本讲指定实践任务）

**实践目标**：列出 `01_design_planning.tcl` 与 ICC2 `PnR.tcl` 中至少三个命令名差异，体会工具演进。

**操作步骤**：

1. 并排打开本讲的 `01_design_planning.tcl` 与 U4 的 `IC Compiler II/PnR.tcl`。
2. 从上表挑出至少三对命令（建议选布图、电源、保存三类各一对）。
3. 对每对命令写一句话：**命令名变了，但语义对应什么**。

**预期结果（示例三对）**：

| ICC（01_design_planning.tcl） | ICC2（PnR.tcl） | 语义关系 |
|---|---|---|
| `create_floorplan -core_utilization 0.6` | `initialize_floorplan -boundary {{0 0} {400 400}}` | 都是画 die/core；ICC 靠利用率算面积，ICC2 直接给边界 |
| `set_fp_rail_constraints` + `synthesize_fp_rail` + `commit_fp_rail` | `create_pg_mesh_pattern` + `set_pg_strategy` + `compile_pg` | 都是「约束→综合→落地」三步电源规划，ICC 分三命令、ICC2 整合 |
| `save_mw_cel -as "design_planning"` | `save_block -as "${TOP_DESIGN}_..."` | 都是存设计快照；对象从 mw_cel 换成 block |

这是源码阅读型实践，无需运行工具。

#### 4.5.5 小练习与答案

**练习 1**：为什么 ICC 的电源规划要 `commit_fp_rail` 这一步，而 ICC2 没有？
**答案**：ICC 的 `synthesize_fp_rail` 先把电源结构画成「暂存/预览」态（方便你看 IR drop 再决定改不改），确认满意后用 `commit_fp_rail` 固化为正式金属；ICC2 把这两步合并进 `compile_pg`，落地即正式，简化了流程。

**练习 2**：哪些命令 ICC 与 ICC2 名字几乎没变？
**答案**：`add_tap_cell_array`、`derive_pg_connection`、`report_timing`、`read_sdc` 这些通用性强的命令在两代工具里基本沿用；变化最大的是与「Milkyway/NDM 数据模型」和「电源规划范式」强相关的命令。

---

## 5. 综合实践

**任务**：给本讲的 ICC 芯片级设计画一张「**从建 pad 到 CTS 前**」的执行流程图，并标注每一步用的命令、对应的 ICC2 命令、以及它解决什么问题。

**要求**：

1. 按 `create_phy_cell.tcl` → `01_design_planning.tcl` → `02_preroute.tcl` 的真实顺序，列出至少 8 个关键步骤。
2. 每个步骤标注：①ICC 命令；②对应的 ICC2 命令（查 4.5 对照表）；③一句话作用。
3. 标出哪几步是**芯片级设计独有**（即核级 ICC2 `PnR.tcl` 里没有的，如建 IO pad、读 TDF、pad filler、`preroute_instances` 连 pad 环）。
4. 在「电源综合」一步旁标出电压 0.9V、功耗预算 30mW 与估算电流 ≈33mA。

**参考骨架**（请自行补全命令与作用）：

```
建 pad 实例(create_cell) → 读 TDF(read_pin_pad_physical_constraints)
  → create_floorplan(利用率0.6) → insert_pad_filler
  → create_fp_placement(试摆) → derive_pg_connection
  → set_fp_rail_constraints(ring+strap) → synthesize_fp_rail(0.9V/30mW) → commit_fp_rail
  → preroute_instances(pad→环) → preroute_standard_cells(rail) → set_pnet+增量重摆
  → add_tap_cell_array(well tap, 20μm) → 零互连时序检查 → 进入 CTS
```

**预期结果**：得到一张能同时反映「ICC 老命令」与「ICC2 新命令」双视图的流程图，并能指出 IO pad 相关步骤是芯片级设计带来的、与工具版本无关的额外内容。

---

## 6. 本讲小结

- ICC 是 ICC2 的前身，用 **Milkyway** 库与 **mw_cel** 设计对象；ICC2 改用 **NDM** 与 **block**——这是两代工具命令名变化的根因。
- `create_floorplan -core_utilization` 用利用率反推 core 面积，并给 core 到 pad 环的边距；对应 ICC2 的 `initialize_floorplan`。
- 芯片级设计独有：用 `create_cell` 建 IO/corner/POC pad，用 TDF（`set_pad_physical_constraints`）钉 pad 顺序与 iospace，用 `insert_pad_filler` 填 pad 间缝隙。
- ICC 电源三件套 `set_fp_rail_constraints`（约束）→ `synthesize_fp_rail`（综合，含 IR 估算）→ `commit_fp_rail`（固化），对应 ICC2 的 pattern/strategy/`compile_pg`。
- `preroute_instances` 连 pad/宏单元环到电源环，`preroute_standard_cells` 搭标准单元 M1 电源轨，`add_tap_cell_array` 每 20μm 插 well tap 防 latch-up。
- IR drop 遵循 \(V=IR\)，本设计由 0.9V/30mW 反推总电流约 33mA；零互连时序检查（`set_zero_interconnect_delay_mode`）用于判布图是否需要返工。

---

## 7. 下一步学习建议

- **横向对比另一家工具**：下一讲 **u5-l2 Mentor Nitro 参考流程**，去看 Siemens/Mentor 的 Nitro 如何用 `import → place → clock → route → export` 五阶段与 db 依赖组织流程，与本讲的 ICC 两脚本流程三方对照，体会不同 EDA 厂商对同一物理设计问题的不同切分。
- **回到签核侧**：学完工具横向对比后，**U6 PrimeTime STA** 进入静态时序签核，把本讲末尾的「零互连时序检查」升级成基于真实寄生（SPEF）的签核级时序分析。
- **继续读老脚本**：若工作中遇到 Milkyway 老库，可回头结合 **u3-l3 LEF 到 Milkyway/FRAM 的层映射**，理解 ICC 时代如何用 LEF2FRAM 准备物理库数据（ICC2 已改用 NDM，见 u3-l2）。
