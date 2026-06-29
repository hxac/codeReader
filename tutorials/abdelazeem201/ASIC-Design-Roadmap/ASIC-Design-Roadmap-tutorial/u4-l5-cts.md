# 时钟树综合 CTS

## 1. 本讲目标

走过 u4-l4（布局 placement），芯片里所有标准单元都已摆到合法位置、布局时序（WNS/TNS）也基本收敛。但此刻有一根线还是"虚"的——**时钟（clock）**。综合和布局阶段，时钟被当成一根理想的直通线：源端发一个跳变，所有寄存器同时翻转。现实里这不可能：一根时钟源要驱动成千上万个寄存器，物理上必须经过一棵由缓冲器（buffer/反相器）构成的"树"逐级扇出。本讲要做的，就是**把这棵理想时钟线长成一棵真实的时钟树**——这就是 **时钟树综合（Clock Tree Synthesis，CTS）**，ICC2 的主命令是 `clock_opt`。

学完本讲，你应当能够：

1. 读懂 `set_clock_tree_options` 的 `target_skew` / `target_latency` 目标，理解 CTS 要尽量"让所有寄存器在同一瞬间收到时钟"。
2. 理解 CTS **缓冲单元选择**流程：怎么从标准单元库里挑出 `CLKBUF*` 这类专用时钟单元、并用 `set_lib_cell_purpose` 把它们设成 CTS 专用。
3. 读懂 **NDR（Non-Default Routing，非默认布线）规则**与时钟布线的层范围（min/max routing layer），理解为什么时钟网要"加宽、加密间距"走线。
4. **（本讲指定任务）** 解释为什么 CTS 阶段要为 setup 与 hold 设**不同的** `set_clock_uncertainty` 值，并能从源码的 `0.1 -setup` / `0.05 -hold` 中看出量级差异的物理原因。
5. 读懂 `clock_opt` 主命令、`time.remove_clock_reconvergence_pessimism`（CRPR）以及 `clock_opt.flow.enable_ccd` 这几个开关。

> **本讲的关键发现（承接 u4-l4）**：和 placement 一样，CTS 在仓库里也有**两份**实现。`IC Compiler II/PnR.tcl` 是极简模板——CTS 段只有 `create_routing_rule` + `set_clock_routing_rules` + `set_clock_tree_options` + `clock_opt` 四步，约 10 行；`IC Compiler II/Scripts/03_PnR_setup.tcl` 才是带 **CTS 单元选择、驱动单元设置、setup/hold 时钟不确定性、CRPR、前后报告**的完整参考流程。我们对照着读：先看模板"做了什么"，再看参考脚本"还该做什么"。此外，模板里 `set_clock_routing_rules -rules CLK_SPACING` 引用了一个**从未定义**的规则名（应为 `ROUTE_RULES_1`），这是仓库脚本的一处瑕疵，读时要注意。

---

## 2. 前置知识

进入源码前，先把 CTS 阶段的高频术语用大白话过一遍。承接 u4-l1 的 MCMM（slow/fast 角）、u4-l4 的 place_opt / WNS/TNS。

| 术语 | 通俗解释 |
| --- | --- |
| **CTS（Clock Tree Synthesis，时钟树综合）** | 把一根理想时钟源，长成一棵由 buffer/反相器逐级扇出的真实时钟树，让时钟信号能同时（或可控地错时）到达每一个寄存器的时钟脚。 |
| **skew（时钟偏斜）** | 同一个时钟沿，到达不同寄存器的时间差。理想是 0（所有寄存器同时翻转），现实是尽量小。 |
| **latency（时钟延迟）** | 时钟从源端走到某个寄存器时钟脚所花的总时间。CTS 关心的是"源到叶"的延迟与不同寄存器间延迟的一致性。 |
| **CLKBUF / clock buffer（时钟缓冲器）** | 专门为时钟网络设计、输入电容小、驱动能力均衡的缓冲单元。CTS 用它来"接力"放大时钟、平衡各路径延迟。 |
| **NDR（Non-Default Routing，非默认布线）** | 比"默认规则"更宽、间距更大的特殊布线规则，专门用在时钟等关键网络上，降低串扰（crosstalk）、电迁移（EM）与压降，提升信号完整性。 |
| **时钟不确定性（clock uncertainty）** | 对"时钟到底几点到"不确定性的预留量，等于 skew + jitter（抖动）+ margin（余量）。它吃掉时序预算，使检查更保守。 |
| **propagated clock（传播时钟） vs ideal clock（理想时钟）** | CTS 之前时钟是 ideal（理想，工具假设瞬间到达）；CTS 之后时钟变 propagated（传播，工具按真实时钟树算延迟）。这是 CTS 前后的关键状态切换。 |
| **CRPR（Clock Reconvergence Pessimism Removal，时钟重汇聚悲观消除）** | CTS 后，发射与捕获时钟路径会共享一段"公共时钟树干"。工具若对这段同时算"最大延迟"和"最小延迟"就重复计费了；CRPR 把多算的部分补回来，得到更准的 slack。 |
| **CCD（Concurrent Clock Data optimization，时钟-数据并发优化）** | 一种高级模式：建时钟树的同时优化数据路径。关闭它（参考脚本所做）则走"先建树、再优化"的标准顺序流程。 |

**一个直觉**：把时钟想成"学校的上课铃"。综合/布局阶段，假设全校每个教室**同时**听到铃声（ideal clock）。但真实学校只有一只铃，要让全校同时听到，得在走廊里装一串**扩音喇叭（CLKBUF）**，每只喇叭功率一样、到各教室的线长尽量一致——这就是 CTS：用均衡的 buffer 逐级扇出，让铃声**同一时刻**传到每间教室（skew→0），并且**准时**（latency 达标）。NDR 就是"给铃声线加粗、拉开间距"，免得旁边教室的喊声（串扰）干扰铃声。

---

## 3. 本讲源码地图

| 文件 | 在本讲的作用 |
| --- | --- |
| [IC Compiler II/PnR.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl) | **极简模板**。CTS 段 L154–L166：`remove_routing_blockages` → `create_routing_rule` → `set_clock_routing_rules` → `set_clock_tree_options` → `clock_opt`，约 10 行骨架，缺单元选择/时钟不确定性/CRPR。 |
| [IC Compiler II/Scripts/03_PnR_setup.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl) | **完整参考流程**。CTS 段 L345–L430：含版本链入口、CTS 前后报告、`set_clock_tree_options` 目标、`derive_clock_cell_references`/`set_lib_cell_purpose` 单元选择、NDR 规则、时钟驱动单元、**`set_clock_uncertainty 0.1 -setup` / `0.05 -hold`**、CRPR、`clock_opt`，是本讲"血肉"的主要依据。 |
| [IC Compiler II/NDR_rule.pl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDR_rule.pl) | **自动化生成 NDR 的辅助脚本**（u8-l1 详解）。本讲只借它说明 NDR 的"宽度/间距倍率"从何而来，作为 4.3 的延伸。 |

> 提醒：两份脚本是不同设计的模板（`PnR.tcl` 设计名 `ChipTop`、层名大写 `M1..M9`；`03_PnR_setup.tcl` 设计名 `pit_top`、层名小写 `metal1..metal10`），单元名、库名互不相同；我们关注**命令与流程**，不纠结具体实例名。

---

## 4. 核心概念与源码讲解

### 4.1 时钟树目标：skew 与 latency

#### 4.1.1 概念说明

CTS 要"长树"，但树长成什么样才算好？ICC2 用两个**目标值（target）**来引导 buffer 的插入：

- **target_skew（目标偏斜）**：希望时钟沿到达不同寄存器的时间差**尽量小**。skew 越小，寄存器翻转越同步。
- **target_latency（目标延迟）**：希望时钟从源端到寄存器的总延迟**接近某个目标值**。

关键认知：这两个 `target_*` 是 **CTS 的优化目标（guidance），不是硬约束**。`clock_opt` 会插 buffer、平衡分支，努力让实际 skew/latency 往 target 靠，但不保证一定精确等于。把 target 设小（如 0），等于说"尽力把 skew 压到最小"。

skew 的数学定义：

\[
\text{skew}_{i,j} \;=\; t_{\text{arrival}}(\text{clock@寄存器 } i) \;-\; t_{\text{arrival}}(\text{clock@寄存器 } j)
\]

理想 CTS 让所有寄存器的时钟到达时间一致，则任意 \(i,j\) 的 skew 都趋近 0。为什么 skew 重要？它直接吃掉 setup 预算：

\[
t_{\text{setup\_available}} \;\approx\; T_{\text{period}} \;-\; t_{\text{clk2q}} \;-\; t_{\text{logic}} \;-\; \text{skew}
\]

skew 越大，留给逻辑（\(t_{\text{logic}}\)）的时间越少——所以 CTS 的头号任务就是把 skew 压下去。

#### 4.1.2 核心流程

1. （参考脚本）CTS 前先 `report_clocks -skew` 给当前 skew 拍照。
2. `set_clock_tree_options -target_skew <值> -clock [get_clocks *]`：设 skew 目标。
3. `set_clock_tree_options -target_latency <值> -clock [get_clocks *]`：设 latency 目标。
4. `report_clock_tree_options` 确认目标已生效。
5. `clock_opt` 据此建树、插 buffer。

#### 4.1.3 源码精读

**极简模板——把目标都设为 0（理想）**：

```tcl
set_clock_tree_options -target_latency 0.000 -target_skew 0.000
```

来自 [IC Compiler II/PnR.tcl:L161-L161](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L161-L161)：模板把 skew 与 latency 目标都设成 `0.000`，意思是"要求 CTS 尽量做到零偏斜、零延迟"——这是**理论最优、却脱离现实**的目标（物理上不可能做到绝对 0）。它告诉 `clock_opt`"别设上限，尽量压低 skew"。

**参考脚本——设成现实的目标值**：

```tcl
set_clock_tree_options -target_skew 0.5  -clock [get_clocks *]
set_clock_tree_options -target_latency 0.1  -clock [get_clocks *]
report_clock_tree_options
```

来自 [IC Compiler II/Scripts/03_PnR_setup.tcl:L360-L363](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L360-L363)：参考脚本给 `target_skew` 设 **0.5**（单位 ns）、`target_latency` 设 **0.1**（ns），并且用 `-clock [get_clocks *]` 显式指明**对所有时钟**生效。`0.5ns` 是一个现实可达、又留有余量的偏斜目标；`report_clock_tree_options` 随即把设置打印出来确认。**两份脚本的对比**：模板追求"零偏斜"的理想，参考脚本给出"工程上够用且现实"的目标值。

> 注意：`set_clock_tree_options` 在模板里**没有** `-clock` 参数（默认作用于当前所有时钟）；参考脚本显式带 `-clock [get_clocks *]`，语义等价但更清晰。

#### 4.1.4 代码实践

**实践目标**：对照两份脚本的 `set_clock_tree_options`，体会"理想目标 0"与"现实目标"的区别。

1. 打开 [IC Compiler II/PnR.tcl:L161-L161](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L161-L161)，记下模板的 `target_skew 0.000` / `target_latency 0.000`。
2. 打开 [IC Compiler II/Scripts/03_PnR_setup.tcl:L360-L361](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L360-L361)，记下参考脚本的 `target_skew 0.5` / `target_latency 0.1`。
3. 思考：如果把 target_skew 设成一个**很大**的值（如 5ns），会发生什么？

**预期结果**：target 是引导而非硬上限。设 5ns 等于告诉 `clock_opt`"skew 控制在 5ns 以内就行"，工具就不会尽力压低 skew，建出来的树偏斜可能很大，下游 setup 大量违例。反之设 0 则强制工具尽全力压 skew。**实际取值取决于工艺与频率**——本仓库给的 0.5/0.1 是否适配你的设计**待本地验证**。这是**阅读 + 推理型实践**，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：`target_skew` 设 0 和设 0.5，哪个建出来的时钟树 skew 更小？为什么？

**答案**：设 0（模板）通常 skew 更小——因为它要求工具尽力压到最小；设 0.5（参考）则只要不超过 0.5 即可，工具在面积/功耗与时序间权衡时可能"达标就停"。但"设 0 更小"是有代价的：工具可能插入过多 buffer、增大面积和功耗。所以工程上不会盲目追求 0，而是设一个**现实可达且留余量**的目标。

**练习 2**：为什么说 `target_skew` / `target_latency` 是"目标"而不是"约束"？

**答案**：约束（constraint）是硬性的，违例即失败；目标是引导，工具努力靠近但不保证精确达到。时钟树的物理延迟取决于布局、buffer 库、拥塞等，不可能精确等于某个值；ICC2 把 skew/latency 作为优化目标去逼近，最终用 `report_clocks -skew` 看实际值、用 `report_qor` 看时序是否收敛来验收。

---

### 4.2 CTS 单元选择：buffer 池的白名单

#### 4.2.1 概念说明

建时钟树要插很多 buffer/反相器，但**用哪些单元来插**很有讲究。芯片库里通常有成百上千种单元，CTS 不能随便挑——它需要的是**输入电容小、驱动能力均衡、上升/下降延迟对称**的专用时钟单元（如 `CLKBUF*`）。如果让 CTS 用普通数据路径的大 buffer，树会不平衡、功耗也会爆。

ICC2 用"**先全排除、再白名单包含**"两步来圈定 CTS 专用单元池：

1. `set_lib_cell_purpose -exclude cts [get_lib_cells]`：先把库里**所有**单元的 `cts` 用途**排除**（即"CTS 一律不许用任何单元"）。
2. `set_lib_cell_purpose -include cts $CTS_CELLS`：再把挑好的 `CLKBUF*` 单元的 `cts` 用途**包含**进来（即"CTS 只许用这些"）。

这就形成一份白名单：CTS 建树时只能从这份 `CTS_CELLS` 池里挑 buffer。配合 `set_dont_touch ... false` 解除这些单元的"勿动"锁，确保它们可被使用。

> `cell purpose`（单元用途）是 ICC2 给每个单元打的"角色标签"（如 cts、hold、opt、block）。不同流程阶段只从"贴了对应标签"的单元里选，避免误用。`derive_clock_cell_references` 还能自动派生一份等价单元集（`cts_leq_set.tcl`，leq = logically equivalent），用于树内平衡。

#### 4.2.2 核心流程

1. `derive_clock_cell_references`：自动派生候选时钟单元集。
2. `get_lib_cells */CLKBUF*`：手工圈定用 `CLKBUF*` 名字的单元作为池。
3. `set_dont_touch $CTS_CELLS false`：解锁，允许使用。
4. `set_lib_cell_purpose -exclude cts [get_lib_cells]`：全排除。
5. `set_lib_cell_purpose -include cts $CTS_CELLS`：白名单包含。
6. `report_lib_cells` 确认哪些单元现在贴了 `cts` 标签。

#### 4.2.3 源码精读

**参考脚本的完整单元选择**：

```tcl
derive_clock_cell_references -output cts_leq_set.tcl > /dev/null

set CTS_CELLS [get_lib_cells */CLKBUF*]
set_dont_touch $CTS_CELLS false
set_lib_cell_purpose -exclude cts [get_lib_cells]
set_lib_cell_purpose -include cts $CTS_CELLS

report_lib_cells -objects [get_lib_cells] -columns {name:20 valid_purposes dont_touch}
```

来自 [IC Compiler II/Scripts/03_PnR_setup.tcl:L368-L375](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L368-L375)：

- `derive_clock_cell_references`（L368）：让工具自动找出一批适合做时钟 buffer/反相器的单元，等价集写进 `cts_leq_set.tcl`；`> /dev/null` 把控制台输出丢弃（只留文件）。
- `get_lib_cells */CLKBUF*`（L370）：用通配符从参考库里挑出所有名字匹配 `CLKBUF*` 的单元（本仓库 Nangate 库的专用时钟缓冲器族），存进变量 `CTS_CELLS`。
- `set_dont_touch $CTS_CELLS false`（L371）：解除这些单元的 dont_touch 锁，使它们可被 CTS 例化使用。
- `set_lib_cell_purpose -exclude cts [get_lib_cells]`（L372）：先把**全部**单元的 `cts` 用途排除。
- `set_lib_cell_purpose -include cts $CTS_CELLS`（L373）：再只把 `CTS_CELLS` 的 `cts` 用途包含——白名单生效。
- `report_lib_cells ... -columns {name valid_purposes dont_touch}`（L375）：打印一张表，逐个单元列出"有效用途"和"是否锁定"，用来肉眼确认 CLKBUF 已贴 `cts` 标签。

> **极简模板的诚实说明**：`PnR.tcl` 的 CTS 段（[L154-L166](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L154-L166)）**完全没有**上述任何单元选择命令。这意味着模板下 `clock_opt` 会用**默认单元池**（库里所有标记为 buffer/inverter 的单元）建树，而非专用 CLKBUF——简单但不够精细。要看正确的单元选择，得读参考脚本。

#### 4.2.4 代码实践

**实践目标**：理解"全排除 + 白名单包含"两步为何要先排除后包含，而非反过来。

1. 读 [IC Compiler II/Scripts/03_PnR_setup.tcl:L372-L373](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L372-L373) 的两行 `set_lib_cell_purpose`。
2. 思考：如果**只写第二步**（`-include cts $CTS_CELLS`）而不先 `-exclude cts [all]`，CTS 可选单元池会变成什么样？
3. 再思考：如果把这两行**互换顺序**（先 include 再 exclude all），结果会怎样？

**预期结果**：只写 include 而不 exclude——库里原本就带 `cts` 标签的单元**仍可选**，白名单失效（池里混进了非 CLKBUF 单元）。互换顺序（先 include 再 exclude all）则最后一步把**包括 CLKBUF 在内的所有单元**全排除，CTS 反而**无单元可用**——建树必然失败。所以"先全排除、再白名单包含"的顺序是关键，逻辑上等价于"清空→只加想要的"。这是**阅读 + 推理型实践**，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 CTS 要用 `CLKBUF*` 这类专用单元，而不是随便挑普通 buffer？

**答案**：专用时钟单元在输入电容、驱动能力、上升/下降延迟对称性上经过专门优化，插出来的树各分支延迟更一致（skew 小）、翻转更干净。普通 buffer 可能延迟不对称、输入电容大，会让时钟树不平衡、功耗升高，甚至引入占空比畸变。

**练习 2**：`derive_clock_cell_references -output cts_leq_set.tcl` 输出的"leq set"（逻辑等价集）在 CTS 里有什么用？

**答案**：时钟树里常常需要"功能相同、驱动能力不同"的单元互相替换来平衡各分支延迟（比如某分支要更快，就换大一号的等价 buffer）。leq set 把这些逻辑等价、尺寸不同的单元归到一组，CTS 在建树/平衡时可在组内灵活换型，而不改变逻辑功能。

---

### 4.3 NDR 路由规则与层范围

#### 4.3.1 概念说明

时钟网络是全芯片翻转最频繁、扇出最大、最敏感的信号。如果用"默认布线规则"（default rule）走线——默认宽度、默认间距——它会和邻近的数据线发生**串扰（crosstalk）**：相邻线跳变时在时钟线上耦合出噪声，导致时钟沿抖动（jitter）、甚至误触发。为避免这点，CTS 给时钟网套上 **NDR（Non-Default Routing，非默认布线）规则**：把线**加宽**、把到邻居的**间距加大**，让时钟线"独占更宽的隔离带"。

一条 NDR 规则至少要定义两件事：

- **宽度（widths）与间距（spacings）**：每个金属层上的线宽与到同层/邻层导线的间距，通常比 default 大（如 2 倍宽、2 倍间距，记作 `2W2S`）。本仓库参考脚本的规则名 `cts_w2_s2_vlg` 即暗示"2 倍宽、2 倍间距"。
- **层范围（min/max routing layer）**：时钟网允许走在哪几层金属上。

`create_routing_rule` 定义规则模板，`set_clock_routing_rules` 把规则**绑定到所有时钟网**，并限定层范围。

为什么时钟要走**中低层**金属、避开顶层？承接 u3-l1：顶层金属（如 metal6/metal7）留给电源 mesh 用，且越高层越粗、越适合长距离电源主干；时钟网走中低层既能避开电源 mesh，又能控制延迟。这与 u3-l1 的 `MIN/MAX_ROUTING_LAYER` 把信号限定在 metal1–metal10、保护顶层电源金属是同一思想。

#### 4.3.2 核心流程

1. 设 NDR 层范围变量（`CTS_NDR_MIN/MAX_ROUTING_LAYER` 等）。
2. `create_routing_rule` 定义 NDR 模板（宽度、间距、taper 渐变等）。
3. `set_clock_routing_rules -rules <规则> -min_routing_layer ... -max_routing_layer ...`：绑定到时钟网并限定层。
4. `report_routing_rules` / `report_clock_routing_rules` 确认。

#### 4.3.3 源码精读

**极简模板——手写硬编码的 NDR**：

```tcl
create_routing_rule ROUTE_RULES_1 \
 -widths {M3 0.2 M4 0.2 } \
 -spacings {M3 0.42 M4 0.63 }
set_clock_routing_rules -rules CLK_SPACING -min_routing_layer M2 -max_routing_layer M4
```

来自 [IC Compiler II/PnR.tcl:L156-L160](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L156-L160)：

- `create_routing_rule ROUTE_RULES_1`（L156–L158）：定义一条名为 `ROUTE_RULES_1` 的规则，M3、M4 层宽 0.2、间距分别 0.42 / 0.63（比 default 大）。
- `set_clock_routing_rules -rules CLK_SPACING -min_routing_layer M2 -max_routing_layer M4`（L159–L160）：把时钟网绑定到 NDR，限走 M2–M4。

> ⚠️ **仓库瑕疵（重要）**：`create_routing_rule` 建的规则叫 `ROUTE_RULES_1`，但下一行 `set_clock_routing_rules -rules` 引用的却是 `CLK_SPACING`——这个名字在脚本里**从未被定义**。结果时钟网实际上绑定到了一个不存在的规则，CTS 时工具会回退到默认规则或报警。正确写法应是 `-rules ROUTE_RULES_1`。读模板时要留意这处不一致（类似 u4-l4 提到的 `-verbos` 笔误）；要看**正确且完整**的 NDR 设置，读参考脚本。

**参考脚本——变量化、带 taper 的完整 NDR**：

```tcl
set CTS_NDR_MIN_ROUTING_LAYER        "metal4"
set CTS_NDR_MAX_ROUTING_LAYER        "metal5"
set CTS_LEAF_NDR_MIN_ROUTING_LAYER   "metal1"
set CTS_LEAF_NDR_MAX_ROUTING_LAYER   "metal5"
set CTS_NDR_RULE_NAME                "cts_w2_s2_vlg"
set CTS_LEAF_NDR_RULE_NAME           "cts_w1_s2"

create_routing_rule $CTS_NDR_RULE_NAME \
        -default_reference_rule \
        -taper_distance 0.4 \
        -driver_taper_distance 0.4 \
        -widths   {metal3 0.14 metal4 0.28 metal5 0.28} \
        -spacings {metal3 0.14 metal4 0.28 metal5 0.28}

set_clock_routing_rules -rules $CTS_NDR_RULE_NAME \
        -min_routing_layer $CTS_NDR_MIN_ROUTING_LAYER \
        -max_routing_layer $CTS_NDR_MAX_ROUTING_LAYER
```

来自 [IC Compiler II/Scripts/03_PnR_setup.tcl:L379-L395](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L379-L395)：

- 层范围变量（L379–L382）：区分**主干**（trunk，metal4–metal5）与**叶子**（leaf，metal1–metal5）两层范围——时钟树越靠近根用越高层、越靠近叶寄存器用越低层。
- 规则名（L383–L384）：`cts_w2_s2_vlg`（主干 2 倍宽 2 倍间距）、`cts_w1_s2`（叶子）。
- `create_routing_rule`（L386–L391）：`-default_reference_rule` 表示在默认规则基础上叠加；`-taper_distance 0.4` / `-driver_taper_distance 0.4` 允许规则在靠近驱动端**渐变收窄**（taper），避免一上来就用粗线造成大负载；宽度/间距按 metal3/4/5 分别给出。
- `set_clock_routing_rules`（L393–L395）：把 `cts_w2_s2_vlg` 绑到时钟网，限定走 metal4–metal5 主干。

> **与 NDR_rule.pl 的关系**：参考脚本里 NDR 的宽度/间距是**手写**的（如 metal4 0.28）。若不想手算，可用 [IC Compiler II/NDR_rule.pl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDR_rule.pl) 自动读 `.tf` 的 `defaultWidth`/`minSpacing`，按倍率（如 2 倍宽、2 倍间距）缩放，输出 `WIDTH`/`SPACE` 两个 Tcl 变量供 `create_routing_rule` 直接使用（u8-l1 详解）。

#### 4.3.4 代码实践

**实践目标**：理解 NDR 的"倍率"语义，并发现模板那处规则名不一致。

1. 读 [IC Compiler II/PnR.tcl:L156-L160](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L156-L160)，回答：模板里时钟网的 NDR 规则名到底是 `ROUTE_RULES_1` 还是 `CLK_SPACING`？为什么这是个问题？
2. 读 [IC Compiler II/Scripts/03_PnR_setup.tcl:L386-L391](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L386-L391)，对比 metal4 的宽度 `0.28` 与 metal3 的 `0.14`：哪一层更宽？这暗示了什么？
3. 看 [NDR_rule.pl:L93-L94](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDR_rule.pl#L93-L94) 的 `scaled_width = width * WM`，解释 `WM=2` 时的效果。

**预期结果**：模板里 `set_clock_routing_rules -rules CLK_SPACING` 引用了**未定义**的 `CLK_SPACING`，应为 `ROUTE_RULES_1`——这是仓库瑕疵。参考脚本里 metal4/metal5 比 metal3 更宽（0.28 vs 0.14），暗示越靠近顶层时钟主干走得越粗，降低长线延迟与压降。`NDR_rule.pl` 的 `WM=2` 表示把默认宽度乘 2 得到 NDR 宽度。具体数值是否贴合你的工艺**待本地验证**。这是**阅读 + 推理型实践**，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么时钟网要"加宽、加大间距"（NDR），而普通数据线用默认规则就行？

**答案**：时钟网扇出极大、翻转最频繁，对串扰和电迁移（EM）最敏感。加宽降电阻、减压降与 EM 风险；加大间距降低与邻居的耦合电容，减小串扰引起的 jitter。普通数据线扇出小、翻转稀疏，串扰影响有限，用 default 规则足够，NDR 会白白增加面积和拥塞。

**练习 2**：参考脚本里为何把时钟主干限在 metal4–metal5，而不是顶层 metal6/metal7？

**答案**：顶层金属（metal6/metal7）被电源 mesh 占用（见 u4-l3、u3-l1），时钟网走上去会和电源网冲突；且顶层虽粗、适合长距离电源主干，但对时钟而言延迟和负载未必最优。时钟走中低层（metal4–metal5）既能避开电源 mesh，又便于精细控制延迟与平衡——这是"信号层与电源层分工"的体现。

---

### 4.4 clock uncertainty：setup 与 hold 为何不同

> **本模块对应本讲指定的代码实践任务**："解释 CTS 阶段为何要为 setup 与 hold 设置不同的 clock uncertainty 值。"

#### 4.4.1 概念说明

**时钟不确定性（clock uncertainty）** 是对"时钟到底几点到达"不确定性的预留量。它的物理来源是三部分之和：

\[
\text{uncertainty} \;=\; \text{skew} \;+\; \text{jitter} \;+\; \text{margin}
\]

- **skew（偏斜）**：时钟到不同寄存器的时间差。
- **jitter（抖动）**：时钟周期本身的随机波动（电源噪声、PLL 抖动等引起）。
- **margin（余量）**：工程上额外留的安全垫。

uncertainty 怎么影响检查？它**吃掉时序预算**，使检查更保守：

- **对 setup**：uncertainty 从可用周期里**减去**。等效周期变小，setup 更难满足：

\[
T_{\text{eff,setup}} \;=\; T_{\text{period}} \;-\; \text{uncertainty}_{\text{setup}}
\]

- **对 hold**：uncertainty **加到** hold 要求上。等效要求寄存器数据保持更久，hold 更难满足：

\[
t_{\text{hold,required}} \;=\; t_{\text{hold}} \;+\; \text{uncertainty}_{\text{hold}}
\]

两个关键认知：

**① uncertainty 是 CTS 前后的"占位符"。** CTS **之前**，时钟还是 ideal（虚拟），真实时钟树不存在，工具无法知道实际 skew——于是用一个大 uncertainty 把"将来树建好后的 skew+jitter"都提前预留掉。CTS **之后**，时钟变 propagated（真实树已建），skew 由工具**实际算出**了，uncertainty 就该**缩小**，只保留 jitter+margin（skew 部分已被真实计算取代）。所以 uncertainty 随 CTS 推进**递减**。

**② setup 与 hold 的 uncertainty 量级不同，且 setup 通常更大。** 原因在于 jitter 对两者的作用不对称：

- **setup 检查**（发射沿 N → 捕获沿 N+1，相隔一个周期）：发射与捕获时钟沿**相隔整整一个周期**。周期 jitter 直接改变这段时间——若周期变短，捕获沿提前，setup 预算被吃。**峰值周期 jitter 完全计入 setup**，故 setup uncertainty 偏大。

\[
\text{uncertainty}_{\text{setup}} \;\approx\; \text{skew} + \text{jitter}_{\text{peak}} + \text{margin}
\]

- **hold 检查**（发射与捕获在**同一个**沿 N，零周期）：发射与捕获时钟来自同一源、发生在**同一瞬间**。两者的 jitter 是**共模**（同时漂移、同向同量），在"求两个时钟到达时间差"时**相互抵消**。hold 只受 skew（时钟树到两个寄存器的路径差）和少量非相关 jitter 影响。故 hold uncertainty 偏小。

\[
\text{uncertainty}_{\text{hold}} \;\approx\; \text{skew} + \text{jitter}_{\text{residual}} + \text{margin}, \quad \text{jitter}_{\text{residual}} \ll \text{jitter}_{\text{peak}}
\]

这正是本仓库参考脚本设 `0.1 -setup` / `0.05 -hold` 的物理依据：setup（0.1）约为 hold（0.05）的两倍，因为 setup 多扛了一份峰值周期 jitter。

#### 4.4.2 核心流程

1. （CTS 之前，来自 SDC）设较大的 uncertainty 占位（覆盖 pre-CTS skew+jitter）。
2. CTS 阶段（参考脚本 L412–L416）遍历所有 scenario，设**缩小后**的 setup/hold uncertainty：
   - `set_clock_uncertainty 0.1 -setup [all_clocks]`
   - `set_clock_uncertainty 0.05 -hold [all_clocks]`
3. `clock_opt` 建树；建树后 skew 由传播时钟实际计算。
4. 签核（u6 PrimeTime）时用最终（最小）的 uncertainty，只留 jitter+margin。

#### 4.4.3 源码精读

**参考脚本——CTS 阶段为每个 scenario 设 setup/hold 不同的 uncertainty**：

```tcl
#      Change the uncertainty for all clocks in all scenarios
foreach_in_collection scen [all_scenarios] {
  current_scenario $scen
  set_clock_uncertainty 0.1 -setup [all_clocks]
  set_clock_uncertainty 0.05 -hold [all_clocks]
}
```

来自 [IC Compiler II/Scripts/03_PnR_setup.tcl:L412-L416](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L412-L416)：

- `foreach_in_collection scen [all_scenarios]`（L412）：遍历所有 scenario（u4-l1 的 `func_fast` / `func_slow`），逐个 `current_scenario` 切过去（L413）。
- `set_clock_uncertainty 0.1 -setup [all_clocks]`（L414）：对该 scenario 的所有时钟，设 **setup 不确定性 = 0.1ns**。
- `set_clock_uncertainty 0.05 -hold [all_clocks]`（L415）：设 **hold 不确定性 = 0.05ns**。
- 注释 "Change the uncertainty for all clocks in all scenarios"（L411）说明：这一步是在 CTS 处把先前（综合 SDC 里）较大的 pre-CTS uncertainty **改写成** CTS 后的小值。

**两值的对比**：

| 检查类型 | uncertainty | 物理含义 | 为何这个量级 |
| --- | --- | --- | --- |
| **setup** | `0.1` ns | 覆盖 skew + **峰值周期 jitter** + margin | 发射与捕获相隔一整周期，jitter 完全计入 |
| **hold** | `0.05` ns | 覆盖 skew + **残余 jitter** + margin | 同沿检查，jitter 共模抵消，残余小 |

> **极简模板的诚实说明**：`PnR.tcl` 的 CTS 段（[L154-L166](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L154-L166)）**完全没有** `set_clock_uncertainty`。模板把这件事推给了上游 SDC（综合阶段由 `My_Design.cons` 那类约束文件设，见 u2-l3 的 0.15ns）。也就是说，模板不在 CTS 处调整 uncertainty；要看 setup/hold 分别设值、且随 CTS 缩小的正确做法，必须读参考脚本这一段。

#### 4.4.4 代码实践（对应本讲指定任务）

**实践目标**：解释为什么 CTS 阶段要为 setup 与 hold 设**不同**的 `set_clock_uncertainty` 值（`0.1 -setup` vs `0.05 -hold`）。

**操作步骤（阅读 + 物理推理型）**：

1. 打开 [IC Compiler II/Scripts/03_PnR_setup.tcl:L414-L415](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L414-L415)，确认 setup 用 `0.1`、hold 用 `0.05`。
2. 用"jitter 共模抵消"论证：设想两枚相邻寄存器 A、B 由同一时钟源驱动。
   - **setup**：A 在第 N 沿发射数据，B 在第 **N+1** 沿捕获。两沿相隔一个周期，周期 jitter 让 B 的捕获沿**相对** A 的发射沿漂移——这个漂移**没有抵消**，全额吃掉 setup 预算。
   - **hold**：A、B 在**同一**第 N 沿翻转。源端抖动让 A、B 的时钟**同时**漂移同量（共模），求"两时钟到达时间差"时**相减抵消**，只剩时钟树到 A、B 的路径差（skew）。
3. 填下面这张归因表：

| 项 | setup 检查 | hold 检查 |
| --- | --- | --- |
| 发射/捕获沿关系 | 相隔一整周期（N → N+1） | 同一沿（N → N） |
| 周期 jitter 是否计入 | **全额计入**（峰值） | **共模抵消**（只留残余） |
| uncertainty 主要成分 | skew + 峰值 jitter + margin | skew + 残余 jitter + margin |
| 脚本取值 | `0.1` ns（较大） | `0.05` ns（较小） |

**需要观察的现象 / 预期结果**：因为 setup 要多扛一份峰值周期 jitter、hold 的 jitter 大部分在同沿相消，所以 setup 的 uncertainty（0.1）**约为** hold 的（0.05）两倍。这正是源码用两个不同值的物理依据。若强行把两者设成相同（如都设 0.1），则要么 setup 偏乐观（少算了 jitter、hold 设大了反而过于保守，挤压优化空间），要么 hold 偏保守——总之不符物理，签核时该松的松不了、该紧的紧不对。这是**阅读 + 推理型实践**，结论可从检查的时序关系直接推出，无需运行工具。

> 顺带回答一个常见追问："为何 CTS 阶段要重新设（缩小）uncertainty？"——因为综合/布局阶段时钟还是 ideal，uncertainty 必须很大以预留整棵未来的时钟树 skew；而到了 CTS（参考脚本这一步），时钟树即将变真实、skew 即将被实际计算，所以把 uncertainty 从"大占位"缩小到"只留 jitter+margin"的真实值。这步**缩小**本身也是 CTS 的标志动作之一。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `set_clock_uncertainty 0.1 -setup` 误写成 `0.1 -hold`（setup 没设），会发生什么？

**答案**：setup 路径的 uncertainty 缺失（或回退到上一级的值），setup 检查会比真实情况**乐观**——可能"看起来过了"但流片后因真实 jitter + skew 而违例。而 hold 的 0.1 又过大，把 hold 逼得过紧，工具白白插一堆 delay buffer 去 fix hold，浪费面积。两类检查的 uncertainty 必须分别正确设置。

**练习 2**：综合阶段（pre-CTS）的 clock uncertainty 通常比 CTS 这一步（0.1/0.05）**更大**，为什么？

**答案**：综合阶段时钟还是 ideal，真实时钟树尚未建立，工具无法知道实际 skew，必须用一个**大** uncertainty 把"将来整棵树的 skew + jitter + margin"全部提前预留。等到了 CTS（本步），时钟树即将变真实、skew 即将被实际计算，于是把那部分预留的 skew 卸掉，只保留 jitter + margin，故 uncertainty 显著缩小。uncertainty 随流程推进**单调递减**。

---

### 4.5 clock_opt 与 CRPR

#### 4.5.1 概念说明

前面 4.1–4.4 都是"为建树做准备"——设目标、选单元、定 NDR、设不确定性。真正**建树并优化**的是这一行命令：

```tcl
clock_opt
```

`clock_opt` 是 ICC2 CTS 的总命令（与 placement 的 `place_opt`、routing 的 `route_opt` 并列），它内部自动完成：

1. **时钟树综合**：根据目标（skew/latency）、单元池（CLKBUF）、NDR 规则，插入 buffer/反相器，长出时钟树。
2. **时钟延迟传播**：把时钟从 ideal 切换为 propagated，工具开始按真实树算延迟。
3. **数据路径优化**：建树后顺带做一轮数据路径的时序优化（插 buffer、改尺寸）。

**CRPR（Clock Reconvergence Pessimism Removal）**

`clock_opt` 建好真实时钟树后，会引入一个叫 **CRPR** 的话题。看这张时钟路径示意：

\[
\text{源端} \;\to\; \underbrace{\text{公共时钟树干}}_{\text{launch 与 capture 共享}} \;\to\; \begin{cases} \text{launch 寄存器} \\ \text{capture 寄存器} \end{cases}
\]

发射（launch）和捕获（capture）的时钟路径，在分叉前共享一段**公共树干**。做 setup 检查时，工具为了悲观（保守），会对 launch 路径取**最大延迟**（让数据尽量晚到，最坏情况）、对 capture 路径取**最小延迟**（让时钟尽量早到，最坏情况）。可是公共树干是**同一段物理连线**，它不可能同时既是最大又是最小——同时取 max 和 min 就**重复计费**了，造成人为的悲观（slack 被算得比真实更差）。**CRPR 把公共树干上多算的那部分延迟补回来**，得到更准（不那么悲观）的 slack。

`set_app_options -name time.remove_clock_reconvergence_pessimism -value true` 就是**开启**这个补偿。CRPR **只在 CTS 之后才有意义**——CTS 之前时钟是 ideal，没有真实公共路径可"重复计费"，也就没什么可补。

> 这与 u4-l4 结尾埋的伏笔呼应：placement 阶段虽然也提过 `time.remove_clock_reconvergence_pessimism`，但 CRPR 真正发挥作用是在 CTS 把真实时钟树建出来之后。

#### 4.5.2 核心流程

1. （已完成）设目标、选单元、定 NDR、设不确定性。
2. `set_app_options -name time.remove_clock_reconvergence_pessimism -value true`：开 CRPR。
3. `set_app_options -name clock_opt.flow.enable_ccd -value false`：关闭时钟-数据并发优化（走标准顺序流程）。
4. `clock_opt`：建树 + 切传播时钟 + 数据优化。
5. `report_qor -summary` / `report_timing`：验收 CTS 后时序。

#### 4.5.3 源码精读

**参考脚本——CRPR + CCD 开关 + clock_opt**：

```tcl
set_app_options -name time.remove_clock_reconvergence_pessimism -value true
report_clock_settings
set_app_options -name clock_opt.flow.enable_ccd -value false
clock_opt
```

来自 [IC Compiler II/Scripts/03_PnR_setup.tcl:L418-L423](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L418-L423)：

- `time.remove_clock_reconvergence_pessimism -value true`（L418）：**开启 CRPR**——CTS 后把公共时钟树干上 max/min 重复计费的部分补回，消除人为悲观。
- `report_clock_settings`（L420）：把当前所有时钟相关设置打印出来，便于核对（目标、uncertainty、CRPR 等是否都到位）。
- `clock_opt.flow.enable_ccd -value false`（L422）：**关闭**并发时钟-数据优化（CCD）。关掉后走"先建时钟树、再优化数据"的标准顺序；开启则两者同时做、更激进但也更耗时/难收敛。
- `clock_opt`（L423）：真正建树、切传播时钟、顺带数据优化。

**CTS 后的验收报告**：

```tcl
report_qor -summary
report_timing
```

来自 [IC Compiler II/Scripts/03_PnR_setup.tcl:L425-L427](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L425-L427)：CTS 完成后 `report_qor -summary` 看 WNS/TNS（此刻 setup 应明显改善，因为时钟从 ideal 变成真实传播、uncertainty 也缩小了），`report_timing` 看关键路径细节。

**极简模板——只有一行 clock_opt**：

```tcl
clock_opt
```

来自 [IC Compiler II/PnR.tcl:L166-L166](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L166-L166)：模板直接 `clock_opt` 一行——不开 CRPR 开关（用默认值）、不关 CCD、CTS 前后也不出报告。CRPR 在 ICC2 默认通常是开启的，所以模板不显式设也可能"碰巧正确"，但参考脚本的显式 `true` 才是可读、可控的工程写法。

> 提醒：CTS 段在参考脚本里同样遵循"copy_block → current_block → 干活 → save_block"的版本链范式——入口 [L345-L346](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L345-L346) 从 `placement.design` 拷出 `cts.design`，出口 [L430](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L430-L430) `save_block` 存档交给 routing（与 u4-l4 4.5 节一脉相承）。

#### 4.5.4 代码实践

**实践目标**：理解 CRPR"补回公共树干重复计费"的含义，并判断它为何只对 CTS 后有效。

1. 读 [IC Compiler II/Scripts/03_PnR_setup.tcl:L418-L418](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L418-L418) 的 `time.remove_clock_reconvergence_pessimism -value true`。
2. 画一张时钟路径图：源端 → 公共树干 →（分叉）→ launch 寄存器 / capture 寄存器。标出"公共树干"段。
3. 思考：若公共树干延迟是 \(d\)（max）和 \(d\)（min），CRPR 开启前 setup 把它算成 \(\max - \min\) 的重复，开启后补回这部分。如果**关掉** CRPR（`-value false`），slack 会偏大还是偏小？

**预期结果**：关掉 CRPR，公共树干的 max/min 之差被**重复扣减**，slack 会**偏小（更悲观）**——看起来时序更差，可能导致工具过度优化、插多余 buffer。开启 CRPR 把这部分补回，得到更接近真实的（不那么悲观的）slack。所以工程上通常开启。这是**阅读 + 推理型实践**，无需运行。CRPR 在本仓库的具体补偿量**待本地验证**（取决于真实时钟树形状）。

#### 4.5.5 小练习与答案

**练习 1**：CRPR 为什么在 CTS **之前**（如布局阶段）没有意义？

**答案**：CTS 之前时钟是 ideal（虚拟直通），根本没有真实的时钟树，也就没有"launch 与 capture 共享的公共树干"这一物理结构，自然没有重复计费可言。CRPR 补偿的是真实时钟树共享段的 max/min 悲观差，所以它只在 CTS 建出真实树之后才生效。

**练习 2**：`clock_opt.flow.enable_ccd -value false` 关掉了什么？什么场景下会想开成 true？

**答案**：关闭了"时钟-数据并发优化"（CCD），改走"先建时钟树、再优化数据路径"的标准顺序流程。流程简单、易收敛、可预测。当设计对时序要求极高、标准顺序建树后 setup 仍差一口气时，可尝试开成 true 让工具在建树的同时优化数据路径、榨取更多时序，但代价是运行时间更长、收敛更难、结果更难预测。

---

## 5. 综合实践

**任务**：以 CTS 阶段为靶子，做一次"读模板 → 借参考补全 → 解释 setup/hold uncertainty → 接 CRPR/clock_opt"的小演练，把本讲五个模块串起来。

1. **读极简模板**：通读 [IC Compiler II/PnR.tcl:L154-L166](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L154-L166)，确认它的 CTS 只有"`remove_routing_blockages` → `create_routing_rule ROUTE_RULES_1` → `set_clock_routing_rules -rules CLK_SPACING`（⚠️ 名字不一致）→ `set_clock_tree_options`（目标全 0）→ `clock_opt`"。标注它**缺**了什么：没有 CTS 单元选择、没有 `set_clock_uncertainty`、没有 CRPR/CCD 开关、没有 CTS 前后报告、没有 `copy_block`/`save_block` 版本链。

2. **借参考补全**：从 [IC Compiler II/Scripts/03_PnR_setup.tcl:L345-L430](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L345-L430) 把缺的部分"翻译"到模板对应位置（写在你的副本上，**不要改仓库源文件**）：
   - 在 `set_clock_tree_options` 前补 CTS 单元选择（`get_lib_cells */CLKBUF*` → `set_lib_cell_purpose -exclude cts [all]` → `-include cts $CTS_CELLS`）；
   - 修正模板里 `set_clock_routing_rules -rules CLK_SPACING` 为真实存在的规则名；
   - 在 `clock_opt` 前补 `set_clock_uncertainty 0.1 -setup [all_clocks]` 与 `0.05 -hold [all_clocks]`（遍历 scenario）；
   - 补 `time.remove_clock_reconvergence_pessimism -value true` 与 `clock_opt.flow.enable_ccd -value false`；
   - 用 `copy_block`/`current_block`/`save_block` 把 CTS 段包起来，从 `placement.design` 接力到 `cts.design`。

3. **解释 setup/hold uncertainty（对应指定任务）**：用自己的话写一段，说明为何 `0.1 -setup` > `0.05 -hold`——重点讲 setup 跨整周期、峰值 jitter 全额计入；hold 同沿、jitter 共模抵消只留残余。

4. **画 CTS 数据流**：在补全后的脚本旁画一条流程：`placement.design →[copy]→ cts.design →[设目标/选单元/NDR/uncertainty/CRPR]→ clock_opt（ideal 时钟变 propagated）→[report_qor]→ routing`。

> 这是**源码阅读 + 流程设计型**实践，不要求真跑通 ICC2（缺库与网表）；重点是说清"模板缺什么、参考脚本补什么、setup 与 hold 的 uncertainty 为什么不一样、CRPR/clock_opt 在做什么"。

---

## 6. 本讲小结

- **CTS（时钟树综合）** 把理想时钟线长成真实的 buffer 树，主命令是 **`clock_opt`**；与 placement 的 `place_opt`、routing 的 `route_opt` 并列。
- **时钟树目标**由 `set_clock_tree_options` 的 `target_skew` / `target_latency` 设定：模板设 0（理想）、参考脚本设 0.5/0.1（现实），它们是优化引导而非硬约束。
- **CTS 单元选择**用"先 `set_lib_cell_purpose -exclude cts [all]` 全排除、再 `-include cts $CTS_CELLS` 白名单"两步圈出专用 `CLKBUF*` 池；模板没有这一步，用默认池。
- **NDR 路由规则**给时钟网"加宽、加大间距"以降串扰/EM，`create_routing_rule` 定义、`set_clock_routing_rules` 绑定并限定层范围（如 metal4–metal5 主干、metal1–metal5 叶子）；模板里 `set_clock_routing_rules -rules CLK_SPACING` 引用了未定义名，是仓库瑕疵。
- **clock uncertainty** 是 skew + jitter + margin 的预留量，setup 与 hold 设**不同**值：`0.1 -setup`（跨整周期、峰值 jitter 全额计入）约两倍于 `0.05 -hold`（同沿、jitter 共模抵消只留残余）；CTS 阶段把 pre-CTS 的大占位缩小到只留 jitter+margin。
- **CRPR**（`time.remove_clock_reconvergence_pessimism true`）补回真实时钟树公共树干上 max/min 的重复计费，消除人为悲观，只在 CTS 建出真实树后才有意义；`clock_opt.flow.enable_ccd false` 关闭并发优化走标准顺序流程。

---

## 7. 下一步学习建议

- CTS 完成、时钟由 ideal 变成 propagated、`report_qor` 显示 setup 改善后，下一步是给芯片"布线"——**u4-l6 布线 Routing**（`route_opt`、`route.global/track/detail`、pre-route 检查、DRC）。届时你会看到 CTS 建好的时钟树如何被布线落实，以及 NDR 规则如何在实际布线中生效。
- 想深入 NDR 的自动生成（如何从 `.tf` 的 `defaultWidth`/`minSpacing` 按倍率算出宽度/间距），可读 **u8-l1 NDR 路由规则自动化**，它逐行解析 [NDR_rule.pl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDR_rule.pl) 的正则解析与倍率缩放。
- 想理解 setup/hold、uncertainty、slack 的更底层定义，可回看 **u2-l3（SDC 时序约束）**——本讲的 clock uncertainty 就是 SDC `set_clock_uncertainty` 命令在 ICC2 里的延续，签核时会在 **u6（PrimeTime STA）** 再次出现（用最终的、最小的 uncertainty）。
- 想横向对比其他工具的 CTS，可看 **u5-l2（Mentor Nitro 参考流程）** 的 `2_clock.tcl` 阶段，它同样讲 CTS 与 CRPR，可与 ICC2 的 `clock_opt` 对照体会"流程本质相通、命令各异"。
