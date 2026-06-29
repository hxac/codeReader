# 布局规划 Floorplan

## 1. 本讲目标

上一讲（u4-l1）我们把 ICC2 的 setup 阶段走完了：建库、读网表、连 `link_block`、读 TLU+、设布线方向、配好 MCMM。到这一步，工具手里已经有了一个"知道每个逻辑单元长什么样、但还不知道它们在芯片里摆在哪里"的设计。

本讲就要解决这个问题——**布局规划（Floorplan）**。Floorplan 是整个后端流程里"一次决定、终身受影响"的环节：芯片画多大、核心区域在哪、电源环留多宽、宏单元（SRAM 等）放哪个角、引脚排在哪条边上……这些决定一旦固化，后面 placement / CTS / routing 都要迁就它，时序和拥塞的好坏很大程度在此埋下伏笔。

学完本讲，你应当能够：

1. 读懂 `initialize_floorplan`，理解 die / core / 利用率 / core_offset / flip_first_row 这几个概念。
2. 理解 `place_pins` 放引脚的作用，以及引脚层约束 `set_block_pin_constraints`。
3. 理解虚拟布局 `create_placement -floorplan`、宏单元固定与 `legalize_placement` 合法化的关系。
4. 学会用**拥塞图（congestion map）**和**零互连时序（zero interconnect）**两把尺子评估布图质量，并据此决定是否要加大面积。
5. 理解 `save_block` 如何给布图阶段拍一张"快照"，以便回退。

> 本讲的关键发现：仓库里其实有**两份** floorplan 实现。`IC Compiler II/PnR.tcl` 是一份极简模板（直接从 `create_placement` 跳到电源网络，省略了评估与逐阶段保存）；`IC Compiler II/Scripts/03_PnR_setup.tcl` 才是带评估、带 `save_block` 的完整参考流程。我们会对照着读，这样既看懂模板"在做什么"，也看懂参考脚本"还该做什么"。

---

## 2. 前置知识

在进入源码前，先把几个 floorplan 阶段的高频术语用大白话过一遍。承接 u4-l1 已建立的 site、布线方向、block 等概念。

| 术语 | 通俗解释 |
| --- | --- |
| **die（芯片外框）** | 整颗芯片的外边界矩形，代工厂按这个尺寸切割。 |
| **core（核心区）** | die 向内缩进一圈后的区域，标准单元**只能**摆在 core 内。缩进的宽度由 `core_offset` 决定，通常留给电源环、IO、well tap 等。 |
| **利用率（utilization）** | 标准单元总面积占 core 面积的比例。越高越省面积但越难布线；太低则芯片浪费。 |
| **placement row（放置行）** | core 内一条条平行的"格子行"，标准单元按 site 网格对齐摆进行里。 |
| **合法化（legalization）** | 把布局后可能互相重叠、不对齐的单元，"吸附"到合法的 site 格子上、消除重叠。 |
| **宏单元 / hard IP（macro）** | 像 SRAM 这种自带物理版图的大块头，不是用标准单元拼出来的，必须**手动**摆在固定位置并锁死。 |
| **虚拟布局（virtual / flat placement）** | floorplan 阶段做的一次"试摆"，目的是评估布图够不够好，不是最终布局。 |
| **拥塞（congestion）** | 某片区域要走的线太多、布线通道塞不下。用全局布线（global route）粗算一张"拥塞图"来看。 |
| **零互连时序（zero interconnect）** | 临时假设所有连线延迟为 0，只看逻辑门本身的延迟。用来把"逻辑本身慢"和"布线拖慢"两种问题分开。 |
| **global route（全局布线）** | 不画具体金属走线，只把每条网络分配到大致的布线通道，用于快速估算拥塞和长度。 |

**关于利用率的一个直觉**：利用率就像往一个房间里塞家具。塞 25% 很宽敞（好走线、好散热，但浪费面积），塞 80% 很挤（省面积，但容易拥塞、时序差、IR drop 高）。纯标准单元的设计常用 60%~80%；带很多宏单元或预期拥塞严重时往下压。本讲稍后会看到 `03_PnR_setup.tcl` 用了 `0.25` 这样一个相当保守的值。

---

## 3. 本讲源码地图

| 文件 | 在本讲的作用 |
| --- | --- |
| [IC Compiler II/PnR.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl) | **极简模板**。从 `initialize_floorplan` 到宏单元固定、虚拟布局、合法化、宏上的布线阻挡，行号紧凑，是本讲的主线。 |
| [IC Compiler II/Scripts/03_PnR_setup.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl) | **完整参考流程**。包含基于利用率的初始化、拥塞评估、零互连时序评估、修复手段、`write_floorplan` 与逐阶段 `save_block`，是评估与保存两个模块的主要依据。 |

> 提醒：这两份脚本里用到的 `ChipTop`、`pit_top`、SRAM 单元名、库名等都是各自模板的占位，并非同一颗设计；我们关注的是**命令与流程**，不纠结具体实例名。

---

## 4. 核心概念与源码讲解

### 4.1 initialize_floorplan 与利用率

#### 4.1.1 概念说明

`initialize_floorplan` 是 floorplan 阶段的"破土动工"命令：它根据你给的尺寸，画出 die 外框、向内缩进得到 core、在 core 里铺好 placement rows 和 site 网格。执行完它，设计才算有了一块"空地"。

确定这块空地多大，有两种截然不同的思路，本仓库两份脚本正好各演示一种：

- **思路 A：直接给 die 边界**（`PnR.tcl` 的做法）。用 `-boundary {{x1 y1} {x2 y2}}` 写死 die 的左下、右上角，core 由 `-core_offset` 向内缩。面积是**你定的**，利用率是**算出来的**。
- **思路 B：直接给利用率**（`03_PnR_setup.tcl` 的做法）。用 `-core_utilization 0.25` 告诉工具目标填充率，工具用网表里所有标准单元的总面积反推需要的 core 面积，再自动生成 die。利用率是**你定的**，面积是**算出来的**。

利用率的定义：

\[
\text{utilization} \;=\; \frac{A_{\text{std\_cells}}}{A_{\text{core}}}
\]

当采用思路 A（已知 die 尺寸 \(W \times H\) 与四周偏移 \(o\)）时，core 面积为：

\[
A_{\text{core}} \;=\; (W - 2o)(H - 2o)
\]

其余两个参数：

- `-core_offset {左 下 右 上}`：die 到 core 的留白，本仓库两份脚本都是四边对称的定值。这块留白主要留给后续的电源环（见 u4-l3）。
- `-flip_first_row true`：控制第一行 placement row 是否翻转方向。相邻行的标准单元朝向通常是交替的（为了保证 well 连续），这个开关决定"第一行朝哪个朝向起头"，会影响 well tap 与单元 orientation。

#### 4.1.2 核心流程

1. 选定目标利用率（或目标 die 尺寸）。
2. 调用 `initialize_floorplan`，生成 die / core / rows / site。
3. （site 与布线方向在 setup 阶段已设好，见 u4-l1，这里不再重复。）

#### 4.1.3 源码精读

**思路 A：写死 die 边界**（极简模板）—— die 是 400×400，core 四边各缩 15：

```tcl
initialize_floorplan -flip_first_row true -boundary {{0 0} {400 400}} -core_offset {15 15 15 15}
```

这段来自 [IC Compiler II/PnR.tcl:L38-L38](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L38-L38)：左下角 (0,0)、右上角 (400,400) 定出 die；`-core_offset {15 15 15 15}` 让 core 每边内缩 15µm。

**思路 B：写死利用率**（参考流程）—— 目标填充率 0.25（25%），四边缩 10：

```tcl
## Create Starting Floorplan
############################
initialize_floorplan -core_utilization 0.25 -flip_first_row true -core_offset {10 10 10 10}
```

来自 [IC Compiler II/Scripts/03_PnR_setup.tcl:L96-L98](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L96-L98)：这里没有 `-boundary`，工具根据网表总面积和 0.25 的利用率自动算出 core 尺寸。

#### 4.1.4 代码实践

**实践目标**：用思路 A 的公式手算 PnR.tcl 这块地的 core 面积，体会"利用率是算出来的"。

1. die 尺寸 \(W = H = 400\)，core_offset \(o = 15\)。
2. 套公式 \(A_{\text{core}} = (400 - 2 \times 15)^2 = 370^2\)。
3. 得 core 面积 = **136 900 µm²**，die 面积 = 160 000 µm²。
4. 假设综合报告告诉你标准单元总面积约为 41 070 µm²，则利用率为 \(41070 / 136900 \approx 0.30\)，即约 30%。

**需要观察的现象 / 预期结果**：core 面积永远小于 die 面积；利用率随你加大 `-boundary` 而下降、随你减小而上升。如果你想从 30% 提到 70%，要么把 die 缩小、要么换用 `-core_utilization 0.7` 让工具自动收紧。

> 待本地验证：上面的标准单元总面积 41 070 µm² 是一个**示例数值**，用于演示计算；真实数字要从你那次综合（DC）的 `report_area` 里读，本仓库未提供该报告。

#### 4.1.5 小练习与答案

**练习 1**：`-boundary {{0 0} {400 400}}` 与 `-core_utilization 0.25` 两种写法，哪种更适合"芯片面积已被封装限制卡死"的项目？

**答案**：前者。封装/PCB 通常对 die 物理尺寸有硬要求，这时应写死 `-boundary`，让利用率作为"结果"被算出来；后者更适合面积灵活、想按利用率定规的设计。

**练习 2**：保持 die 400×400、core_offset 不变，若标准单元总面积翻倍，利用率会变成多少？

**答案**：翻倍。利用率与 \(A_{\text{std\_cells}}\) 成正比，core 面积不变，故分子翻倍即利用率翻倍。

---

### 4.2 place_pins（引脚放置）

#### 4.2.1 概念说明

`place_pins` 决定顶层端口（top-level ports，即 `clk`、`data*`、`Cin*` 等对外信号）在 die 边界上的物理位置与金属层。引脚位置看似不起眼，却直接影响两件事：

- **布线拥塞**：引脚都挤在一条边的一个角落，进出的线会扎堆，造成局部拥塞。
- **时序**：一个引脚离它要驱动的寄存器越远，路径越长、延迟越大；floorplan 阶段就能从引脚位置粗估 I/O 路径好不好走。

floorplan 阶段的 `place_pins` 给的是一个**初步**排布，后面 placement / routing 还会微调；但先有合理初排，评估才有意义。

#### 4.2.2 核心流程

1. （可选）用 `set_block_pin_constraints` 限定引脚允许出现在哪些金属层、哪些边。
2. `place_pins -ports [get_ports *]` 给所有端口排位置。

#### 4.2.3 源码精读

极简模板里只有一行，把**所有**端口交给工具自动排：

```tcl
place_pins -ports [get_ports *]
```

来自 [IC Compiler II/PnR.tcl:L40-L40](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L40-L40)：`[get_ports *]` 取全部顶层端口，`*` 是通配符。

参考流程同样调了 `place_pins`，但**多了一条层约束**：

```tcl
place_pins -ports [get_ports *]
create_placement -floorplan
legalize_placement
set_block_pin_constraints -self -allowed_layers {M3 M4 M5 M6}
```

来自 [IC Compiler II/Scripts/03_PnR_setup.tcl:L111-L115](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L111-L115)：`set_block_pin_constraints -self -allowed_layers {M3 M4 M5 M6}` 把引脚限定在 M3–M6 这几层金属上——底层 M1/M2 留给标准单元内部连线和电源 rail，顶层 M7/M8 留给电源 mesh（见 u4-l3），引脚走中间层互不干扰。

#### 4.2.4 代码实践

**实践目标**：理解层约束的作用。

1. 读 [IC Compiler II/Scripts/03_PnR_setup.tcl:L115-L115](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L115-L115) 这条 `set_block_pin_constraints`。
2. 思考：如果删掉它，让引脚也允许走在 M7（电源 mesh 层），会发生什么？

**预期结果**：引脚可能与电源 mesh 抢同一层金属，造成 DRC 与短路风险；这就是为什么参考脚本把引脚"夹"在中间层 M3–M6。这是**阅读型实践**，无需运行工具即可理解。

#### 4.2.5 小练习与答案

**练习 1**：`place_pins -ports [get_ports *]` 中的 `[get_ports *]` 换成 `[get_pins *]` 会怎样？

**答案**：会出错/无意义。`get_ports` 取的是顶层对外端口，`get_pins` 取的是单元引脚（成千上万个内部引脚），不是 `place_pins` 的合法对象。

**练习 2**：为什么引脚层约束通常避开最顶层的电源 mesh 金属？

**答案**：顶层金属要铺连续的电源/地 mesh，引脚若同层会打断 mesh、引发 DRC；故引脚走中间层、电源独占顶层。

---

### 4.3 虚拟布局、宏单元固定与合法化

#### 4.3.1 概念说明

core 和引脚就位后，下一步是**试摆**所有单元，看这块地够不够好。这一步叫**虚拟布局（virtual placement）**，命令是 `create_placement -floorplan`。注意它和后面 placement 阶段的 `place_opt` 不是一回事：

- `create_placement -floorplan`：floorplan 阶段的快速试摆，**只摆不优化**，目的是给评估（拥塞、时序）提供一张"摆好的图"。
- `place_opt`（u4-l4）：真正的布局优化，会做时序驱动的反复迭代。

如果设计里有**宏单元（macro，如 SRAM）**，必须在 `create_placement` **之前**先把它们摆好并锁死——因为宏单元又大又重，工具的自动布局往往摆不好，需要工程师根据与引脚的对应关系、数据通路走向手工定位。固定之后，`create_placement` 才能在剩下的空地里摆标准单元。

摆完之后通常会有单元重叠、不对齐 site，于是用 `legalize_placement` 把它们吸附到合法格子上。

#### 4.3.2 核心流程

1. **（若有宏单元）** 手工摆宏单元：设 origin（位置）、orientation（朝向）→ `set_fixed_objects` 锁死 → `create_keepout_margin` 加禁布区。
2. `create_placement -floorplan`（可选 `-timing_driven`、`-congestion` 等开关）做虚拟布局。
3. `legalize_placement` 合法化。
4. （可选）在宏单元上方加布线阻挡 `create_routing_blockage`，保护宏单元区域。

#### 4.3.3 源码精读

**宏单元手工摆放（极简模板）**：先定义 SRAM 的几何参数，再逐个设位置和朝向，最后锁死并加 keepout：

```tcl
set sram_width 54.468
set sram_space 40
set sram_start_x 55.4690
set sram_start_y 246.60

set_attribute [get_cells MemYHier_MemXb] orientation R0
set_attribute [get_cells MemYHier_MemXa] origin "$sram_start_x $sram_start_y"
...
set_fixed_objects [get_cell MemXHier_MemXa]
...
create_keepout_margin -type hard -outer {20 20 20 20} [get_cells Mem?Hier_MemX?]
```

这段来自 [IC Compiler II/PnR.tcl:L48-L69](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L48-L69)：四个 SRAM（`Mem?Hier_MemX?`）沿 y=246.60 这条水平线、以 `(width+space)=94.468` 为步长排成一排，朝向统一 `R0`（不旋转）。`set_fixed_objects` 把它们锁死，后续 `create_placement` 不会动它们。`create_keepout_margin -type hard -outer {20 20 20 20}` 在每个宏四周再划 20µm 的硬禁布区，防止标准单元挤到宏单元边上。

**虚拟布局 + 合法化**：

```tcl
create_placement -floorplan -timing_driven
legalize_placement
```

来自 [IC Compiler II/PnR.tcl:L73-L74](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L73-L74)：`-timing_driven` 让试摆过程考虑时序（优先把关键路径上的寄存器摆近）；随后 `legalize_placement` 消除重叠、吸附到 site。

参考流程里则更朴素（不带 `-timing_driven`），但在注释里点出了更全的开关：

```tcl
## Use the following command with any of its options to meet a specific target
#    create_placement -floorplan -timing_driven -congestion -buffering_aware_timing_driven
place_pins -ports [get_ports *]
create_placement -floorplan
legalize_placement
```

来自 [IC Compiler II/Scripts/03_PnR_setup.tcl:L109-L114](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L109-L114)：被注释的那行展示了 floorplan 试摆可选的"满配"开关——`-timing_driven`（时序驱动）、`-congestion`（拥塞感知）、`-buffering_aware_timing_driven`（插入 buffer 友好的时序驱动）。

**宏单元上方的临时布线阻挡**（极简模板）：

```tcl
create_routing_blockage -layers {M1 M2 M3 M4 M5} -boundary [get_attribute [get_cells MemXHier_MemXa] boundary]
...
```

来自 [IC Compiler II/PnR.tcl:L76-L84](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L76-L84)：在四个宏单元的边界范围内、M1–M5 上建布线阻挡，目的是随后编译电源 mesh（`compile_pg`，见 u4-l3）时不让 mesh 走线压过宏单元；编译完会在第 98 行用 `remove_routing_blockages *` 把这些临时阻挡移除。**这部分是过渡到电源网络的内容，细节留给 u4-l3。**

#### 4.3.4 代码实践

**实践目标**：跟踪四个 SRAM 的 x 坐标是怎么算出来的，验证它们确实落在 die（400 宽）之内。

1. 读 [IC Compiler II/PnR.tcl:L49-L64](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L49-L64)。
2. 步长 = `sram_width + sram_space` = 54.468 + 40 = **94.468**。
3. 四个 x 坐标依次为：

\[
x_k \;=\; \text{sram\_start\_x} + k \times 94.468, \quad k = 0,1,2,3
\]

即 55.469、149.937、244.405、338.873。

4. 最后一个宏单元右沿 = 338.873 + 54.468 = **393.341** < 400，落在 die 内。

**预期结果**：四块 SRAM 整齐排在 die 上半部（y=246.60），右沿不超出 die 边界。若你把 `sram_space` 改大到 60，第四块右沿会变成约 453，**超出 die**，工具会报越界错——这就是为什么这些坐标要和 `-boundary` 配套核对。

#### 4.3.5 小练习与答案

**练习 1**：为什么宏单元必须**先** `set_fixed_objects` 再 `create_placement`，顺序反过来会怎样？

**答案**：反过来则 `create_placement` 可能把宏单元当作普通大块单元挪走或摆到糟糕的位置；先锁死，自动布局才只在剩余空间摆标准单元，并尊重 keepout 禁布区。

**练习 2**：`create_placement -floorplan` 与后续的 `place_opt`（u4-l4）有何区别？

**答案**：前者是 floorplan 阶段的快速试摆，只摆不优化，用于评估布图；后者是真正的布局优化命令，会迭代做时序/面积/拥塞优化。

---

### 4.4 拥塞与时序评估

#### 4.4.1 概念说明

这是 floorplan 阶段**最重要、却最容易被极简模板省略**的一步。摆好虚拟布局后，必须回答两个问题：

1. **布线走得通吗？** → 看**拥塞（congestion）**。用全局布线（global route）粗算每个布线通道的需求量，超出容量的地方就是拥塞热点。拥塞严重意味着这块地"塞不下这么多线"，要么改 floorplan，要么加面积。
2. **时序大致行不行？** → 看**零互连时序（zero interconnect）**。floorplan 阶段还没有真实寄生，此时若用估出来的线延迟，时序数字噪声很大。一个干净的做法是：临时把连线延迟设为 0，只看逻辑门本身的延迟——

\[
t_{\text{total}} \;=\; \underbrace{t_{\text{logic}}}_{\text{零互连时序}} \;+\; \underbrace{t_{\text{wire}}}_{\text{布线延迟}}
\]

零互连时序告诉你：**就算布线完美，逻辑本身还差多少**。如果零互连下就已经违例（negative slack），说明是逻辑/综合的问题，加大面积没用；如果零互连下 OK、一加上布线延迟就违例，那才是 floorplan / 布线的问题，加大面积或重摆宏单元才可能救。

> **重要对照**：`PnR.tcl` 这份极简模板**没有**任何拥塞或时序评估命令——它从 `create_placement -timing_driven` + `legalize_placement` 直接跳到建电源网络（`create_pg_mesh_pattern`，见 u4-l3）。也就是说，模板"省略了评估"。完整的评估命令都在 `03_PnR_setup.tcl` 的 ASSESSMENT 段。这是本讲实践任务的核心。

#### 4.4.2 核心流程

1. 虚拟布局已就绪。
2. **拥塞评估**：`route_global -congestion_map_only true` 跑一遍只画拥塞图的全局布线；在 GUI 里查看 Global Route Congestion Map；严重时再 `report_congestion -rerun_global_router` 出报告。
3. **时序评估**：先 `report_qor -summary`（带估计线延迟），再把 `time.delay_calculation_style` 设为 `zero_interconnect`，再 `report_qor`——**对比两次 QoR**，差值就是布线延迟带来的影响。
4. **按评估结果修复**：加 `create_bound` 把相关逻辑绑到一片区域、用 `set_congestion_options -max_util` 局部压密度、加 `create_placement_blockage`、或干脆**加大 floorplan 面积**。

#### 4.4.3 源码精读

**拥塞评估**（参考流程 ASSESSMENT 段）：

```tcl
## Analyze Congestion
# report_congestion -rerun_global_router
route_global -congestion_map_only true -effort high
# View Congestion map : In GUI,  > Global Route Congestion Map.
```

来自 [IC Compiler II/Scripts/03_PnR_setup.tcl:L132-L135](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L132-L135)：`route_global -congestion_map_only true -effort high` 只生成拥塞图、不做真实布线，`-effort high` 提高估算精度；`report_congestion -rerun_global_router` 被注释，需要时打开它出文字报告。

**时序评估（零互连对比）**：

```tcl
## Perform timing sanity check
report_qor -summary -include setup
set_app_options -list {time.delay_calculation_style zero_interconnect}
report_qor -summary -include setup
```

来自 [IC Compiler II/Scripts/03_PnR_setup.tcl:L172-L174](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L172-L174)：第一条 `report_qor` 是带估计线延迟的常规 QoR；紧接着把延迟计算风格切到 `zero_interconnect`，再 `report_qor` 一次——两次 WNS/TNS 的差值，就是布线延迟对时序的影响。

随后参考脚本还做了一次更细的时序清理，并报告最差路径：

```tcl
set_app_options -list {time.high_fanout_net_pin_capacitance 0pF
                       time.high_fanout_net_threshold 50}
update_timing -full
report_qor -summary -include setup
view report_timing -max_paths 5
```

来自 [IC Compiler II/Scripts/03_PnR_setup.tcl:L176-L181](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L176-L181)：`high_fanout_net_*` 两个选项给高扇出网络一个默认电容/阈值，避免高扇出复位/时钟网络把时序算歪；`update_timing -full` 全量重算时序图；`view report_timing -max_paths 5` 看最差的 5 条路径。

**按评估结果修复**（参考脚本给出的可选手段）：

```tcl
## FIXES
#   create_bound -name "temp" -coordinate {55 0 270 270} datamem
#   set_congestion_options -max_util 0.4 -coordinate {x1 y1 x2 y2}
#   create_placement_blockage -name PB -type hard -bbox {x1 y1 x2 y2}
#   create_placement -floorplan -congestion_effort high
## Then you need to re-run create_placement -floorplan
#   create_placement -floorplan  -incremental;
## If there still congestion, change ignored layers, if it is still there, increase floorplan area.
```

来自 [IC Compiler II/Scripts/03_PnR_setup.tcl:L150-L166](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L150-L166)：这是仓库里**关于"如何据评估调面积"最直接的注解**——先试局部手段（绑区域、压密度、加阻挡、高拥塞 effort 重摆），**都不行才加大 floorplan 面积**。`-incremental` 表示在当前布局上微调，而非从零重摆。

#### 4.4.4 代码实践（对应本讲指定任务）

**实践目标**：在 `PnR.tcl` 中找到拥塞与时序评估相关命令，并据此说明你会如何调整布图面积。

**操作步骤（阅读型 + 判断型）**：

1. 打开 [IC Compiler II/PnR.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl)，定位 floorplan 段（约 L38–L84）。
2. 你会发现：**`PnR.tcl` 里没有任何 `route_global` / `report_congestion` / 零互连 `set_app_options` 命令**。它从虚拟布局直接进入电源网络。这就是模板的"评估缺口"。
3. 转而读 [IC Compiler II/Scripts/03_PnR_setup.tcl:L130-L181](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L130-L181)，找到评估命令：拥塞用 `route_global -congestion_map_only true`，时序用两次 `report_qor` 之间的 `zero_interconnect` 切换。

**我会据此这样调面积（判断逻辑）**：

- 先看**拥塞图**。若某区域 overflow > 0（需求超出通道容量），先按 `03_PnR_setup.tcl` 注释里的局部手段处理：`create_bound` 把相关逻辑绑到这片、`set_congestion_options -max_util` 压低局部密度、加 `create_placement_blockage`。
- 再看**零互连时序**。若零互连下 WNS 已为负（逻辑本身慢），加大面积**没用**，应退回去改 RTL 或重新综合；若零互连 OK、加布线延迟后才违例，说明是布线长度/拥塞拖累，这时局部手段若仍救不回来，**就按脚本注释"increase floorplan area"加大 die**——把 `PnR.tcl` 的 `-boundary {{0 0} {400 400}}` 放大到例如 `{{0 0} {420 420}}`，或把 `-core_utilization` 目标调低，让工具有更多布线空间。
- 加大面积后，必须**重新跑** `create_placement -floorplan` 和两把评估尺子，直到 overflow 清零、时序可接受。

**需要观察的现象 / 预期结果**：面积加大后拥塞 overflow 下降、带布线延迟的 WNS 向零靠近；但利用率同时下降、die 成本上升——这是 PPA 的典型取舍。具体数值**待本地验证**（依赖真实网表与库）。

#### 4.4.5 小练习与答案

**练习 1**：零互连时序下 WNS 为 −0.3ns，说明什么？加大 floorplan 面积能解决吗？

**答案**：说明**逻辑本身**就慢 0.3ns（与布线无关）。加大面积只能减布线延迟、减不掉逻辑延迟，所以**不能**解决；应回到综合阶段优化逻辑结构或换更快的工艺角。

**练习 2**：为什么评估拥塞用 `route_global -congestion_map_only true` 而不是直接 `route_opt`？

**答案**：`route_opt` 是昂贵的真实布线 + 优化，floorplan 阶段还没定局，跑它既慢又会因后续 placement 变动而白费；`congestion_map_only` 只用全局布线估一张图，快得多，足以判断布图是否走得通。

---

### 4.5 save_block（布图快照保存）

#### 4.5.1 概念说明

评估通过、布图定局后，要给当前状态拍一张"快照"存进 NDM 库，这就是 `save_block`。它的价值在 u4-l1 已讲过——**每阶段一个快照**，后面任何一步出问题都能 `open_block` 回退到这里，而不必从 setup 重跑。

除了 `save_block`，参考脚本还用 `write_floorplan` 把布图（含电源网络拓扑、固定宏单元）导出成独立文件，方便**跨工具或跨流程复用**（例如交给 DCG、或别的流程重启）。

> **诚实对照**：`PnR.tcl` 这份极简模板**没有**逐阶段 `save_block`——整份脚本只在最末尾 `save_block -as "${TOP_DESIGN}_Final"` 存一次（见 [IC Compiler II/PnR.tcl:L190-L190](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L190-L190)）。也就是说，模板把"每阶段快照"这个好习惯**省略**了；逐阶段保存的正确写法要看 `03_PnR_setup.tcl`。

#### 4.5.2 核心流程

1. floorplan 完成、PG 引脚已连、`check_mv_design` 通过。
2. `write_floorplan` 导出布图文件（可选）。
3. `save_block -as <库>:<block>/<快照名>` 存快照。

#### 4.5.3 源码精读

参考流程在 floorplan 末尾的完整收尾（PG 连接 → 检查 → 导出 → 保存）：

```tcl
## PG Pin connections
create_net -power $NDM_POWER_NET
create_net -ground $NDM_GROUND_NET
connect_pg_net -net $NDM_POWER_NET [get_pins -hierarchical "*/VDD"]
connect_pg_net -net $NDM_GROUND_NET [get_pins -hierarchical "*/VSS"]
check_mv_design

### Write floorplan for later re-use in ICC2
write_floorplan -net_types {power ground} \
   -include_physical_status {fixed locked} \
   -read_def_options {-add_def_only_objects all -no_incremental} \
   -force -output ./output/{DESIGN_NAME}.fp/

## Save the block
save_block -as pit_top.dlib:pit_top/floorplan.design
save_block
```

来自 [IC Compiler II/Scripts/03_PnR_setup.tcl:L184-L211](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L184-L211)（PG 连接 L184–L192、write_floorplan L194–L204、save_block L206–L211）：

- `create_net` + `connect_pg_net`：建立逻辑电源/地网络并把所有单元的 VDD/VSS 引脚连上（**注意**：这只是逻辑连接，物理上的电源 mesh/ring/rail 要到 u4-l3 才铺）。
- `check_mv_design`：多电压设计一致性检查，确保 PG 连接、电平转换器等没有遗漏后再存快照。
- `write_floorplan`：把电源/地网络和 fixed/locked 的物理对象（宏单元等）导出成 `.fp` 文件。
- `save_block -as pit_top.dlib:pit_top/floorplan.design`：把当前 block 以 `floorplan.design` 这个快照名存入 NDM 库 `pit_top.dlib`。后续阶段出问题，`open_block pit_top.dlib:pit_top/floorplan.design` 即可回到这一步。

对比极简模板里**唯一的**一次保存（在整个流程最末）：

```tcl
save_block -as "${TOP_DESIGN}_Final"
```

来自 [IC Compiler II/PnR.tcl:L190-L190](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L190-L190)：模板只在最后存一个 `*_Final` 快照，floorplan 阶段不留中间快照——便于阅读，但不利于工程回退。

#### 4.5.4 代码实践

**实践目标**：给极简模板补上 floorplan 阶段的快照保存。

1. 在 [IC Compiler II/PnR.tcl:L84-L84](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L84-L84)（宏单元布线阻挡之后、电源 mesh 之前）插入一行（**示例代码**，仅作演示，请勿修改仓库源文件，可在自己的副本上试验）：

   ```tcl
   # 示例代码：给 floorplan 拍快照
   save_block -as ${TOP_DESIGN}_floorplan
   ```

2. 读 [IC Compiler II/Scripts/03_PnR_setup.tcl:L210-L210](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L210-L210)，对比参考脚本用 `pit_top.dlib:pit_top/floorplan.design` 这种"库名:block 名/快照名"的完整写法。

**预期结果**：执行后该 block 会在 NDM 库里多出一个名为 `*_floorplan` 的快照；之后电源网络若出错，`open_block ${TOP_DESIGN}_floorplan` 即可回到 floorplan 定局点重试，无需重跑 setup。是否真能 `open_block` 成功**待本地验证**（依赖完整库与网表）。

#### 4.5.5 小练习与答案

**练习 1**：`save_block` 与 `write_floorplan` 有何区别？

**答案**：`save_block` 把整个 block 当前状态（含布局、时序、所有对象）存进 NDM 库，是 ICC2 内部的版本快照；`write_floorplan` 只把布图信息（电源网络、固定宏单元的物理状态）导出成文本/DEF 文件，便于跨流程复用。前者是"存档"，后者是"导出布图"。

**练习 2**：为什么 `save_block` 前要 `check_mv_design`？

**答案**：确保电源域、PG 连接、电平转换器等多电压（multi-voltage）相关设置没有遗留错误；存进快照的就是"已验证干净"的状态，避免把错误固化下来、被后续阶段继承。

---

## 5. 综合实践

**任务**：以 `PnR.tcl` 的 floorplan 段为靶子，做一次"找缺口 + 补流程 + 判面积"的小演练，把本讲五个模块串起来。

1. **读极简模板**：通读 [IC Compiler II/PnR.tcl:L38-L84](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L38-L84)，依次标注出：initialize_floorplan（L38）、place_pins（L40）、宏单元固定（L48–L69）、虚拟布局+合法化（L73–L74）、宏上布线阻挡（L76–L84）。确认它**缺**了什么：没有拥塞评估、没有零互连时序评估、没有 floorplan 阶段的 `save_block`。
2. **借参考流程补全**：从 [IC Compiler II/Scripts/03_PnR_setup.tcl:L130-L181](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L130-L181) 把三段评估命令"翻译"到模板对应位置——在 `legalize_placement`（L74）之后插入：`route_global -congestion_map_only true -effort high`、`report_qor -summary`、`set_app_options ... zero_interconnect` + 再 `report_qor`、最后 `save_block -as ${TOP_DESIGN}_floorplan`。（这是**示例代码**，写在你的副本上，不要改仓库源文件。）
3. **判面积**：基于评估输出做决策——
   - 拥塞 overflow 不为零 → 先用 `set_congestion_options -max_util` / `create_placement_blockage` 局部压；
   - 零互连 WNS 已为负 → 判定为逻辑问题，**不**加面积，退回综合；
   - 零互连 OK、加线延迟后违例且局部手段无效 → 把 `-boundary {{0 0} {400 400}}` 适度放大（如改 420 或 440），重跑虚拟布局与评估，直到 overflow 清零、时序可接受。
4. **记录取舍**：写下你最终的 die 尺寸、对应的利用率和 WNS，体会"面积 vs 时序 vs 成本"的 PPA 权衡。

> 这是**源码阅读 + 流程设计型**实践，不要求真的跑通 ICC2（缺库与网表）；重点是能把模板的缺口说清楚、把参考流程的评估手段对号入座、并给出调面积的判据。

---

## 6. 本讲小结

- Floorplan 用 `initialize_floorplan` 画出 die / core / rows，有两种定尺寸思路：`PnR.tcl` 写死 `-boundary`（利用率算出来），`03_PnR_setup.tcl` 写死 `-core_utilization`（面积算出来）；利用率 \(= A_{\text{std\_cells}} / A_{\text{core}}\)。
- `place_pins -ports [get_ports *]` 给顶层端口排初位，`set_block_pin_constraints` 把引脚限定在中间金属层，避开电源 mesh 顶层。
- 宏单元（SRAM）必须**先**手工摆位 + `set_fixed_objects` 锁死 + `create_keepout_margin` 加禁布区，再做虚拟布局 `create_placement -floorplan`，最后 `legalize_placement` 合法化。
- 评估布图质量的两把尺子：**拥塞图**（`route_global -congestion_map_only true`）看走得通不通；**零互连时序**（`set_app_options time.delay_calculation_style zero_interconnect` 前后对比 `report_qor`）把"逻辑慢"与"布线慢"分开。
- 极简模板 `PnR.tcl` **省略了评估与逐阶段保存**；完整流程（评估命令、修复手段、`write_floorplan`、`save_block`）都在 `03_PnR_setup.tcl`。
- 调面积是最后一招：先用局部手段（绑区域、压密度、加阻挡），都不行才加大 die；零互连就违例时加大面积无效，应退回综合。

---

## 7. 下一步学习建议

- 本讲把布图"空地"和宏单元摆好了，下一步就是在这块地上铺电——**u4-l3 电源网络设计**（PG ring / mesh / rail、`compile_pg`、IR drop 分析）。届时你会明白 floorplan 里 `-core_offset` 留出的那圈空白、以及宏单元上的临时 `create_routing_blockage` 是为谁准备的。
- 想进一步理解为什么 floorplan 决定一切，可回头对比 [IC Compiler II/Scripts/03_PnR_setup.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl) 里 floorplan 与 placement 两段的 QoR 差异。
- 等学完 u4-l3 电源网络，再进入 u4-l4 布局（`place_opt`），那里会看到 floorplan 阶段虚拟布局与正式布局优化的衔接。
