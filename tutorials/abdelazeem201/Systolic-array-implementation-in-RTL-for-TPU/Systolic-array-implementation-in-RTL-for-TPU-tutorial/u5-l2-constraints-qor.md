# 时序约束 cons.tcl 与综合结果解读

## 1. 本讲目标

上一讲（u5-l1）我们走完了 Design Compiler（DC）综合的**流程**——从读 RTL、展开链接、编译优化到输出网表。但流程里有一个被一句 `source ./cons/cons.tcl` 带过的关键文件：**约束文件**。约束文件回答的是「综合工具要把 RTL 优化成什么样」——跑多快、面积多大、哪些路径不用查、留多少余量。

本讲聚焦 `syn/cons/cons.tcl` 这份约束脚本，以及综合跑完后的产物报告 `syn/report/synth_qor.rpt`、`synth_area.rpt`、`synth_power.rpt`。学完后你应该能够：

1. 逐行看懂 `cons.tcl` 里每一条约束命令（时钟、I/O 延迟、uncertainty、false path、wireload、面积、扇出）在说什么、为什么这么写。
2. 算出本设计的时序预算（为什么时钟是 3.01ns、I/O 延迟是 1.505ns、setup 余量还剩多少）。
3. 独立读懂一份 QoR（Quality of Results）报告：slack、违例路径数、关键路径长度、单元数、面积、DRC 违例分别意味着什么。
4. 判断「时序有没有过」与「设计代价有多大」，并能预判收紧时钟后哪些指标会恶化。

> 前置依赖：本讲承接 u5-l1（综合流程与 `syn.tcl`），需要你知道 `cons.tcl` 是在 `compile_ultra` 之前被 `source` 进来的、以及 Nangate 45nm `ss0p95vn40c`（慢-慢工艺角、0.95V、-40℃）这个保守 PVT 角的含义。

## 2. 前置知识

### 2.1 什么是「时序约束」

RTL 描述的是**功能**（电路该算出什么），而约束描述的是**指标**（电路要跑多快、多小、多省电）。综合工具（DC）在把 RTL 映射成标准单元时，会**在功能正确的前提下，尽量去满足这些指标**。没有约束，工具就不知道该往哪个方向优化。

时序约束里最核心的概念是 **setup（建立）检查**：一个触发器在时钟沿到来之前，其 D 端数据必须**提前稳定**一段时间（`library setup time`），否则采到的值不可靠。综合工具会把每条「上一个触发器 → 下一个触发器」的路径延迟算出来，与可用时间比较，差值就是 **slack**：

- slack ≥ 0：**MET**（满足时序）。
- slack < 0：**VIOLATED**（违例），需要继续优化或改设计。

### 2.2 关键名词速查

| 名词 | 含义 |
|------|------|
| **clock period（时钟周期）** | 时钟一个周期的时间，决定了设计的目标频率。3.01ns ≈ 332 MHz。 |
| **slack（裕量）** | 可用时间 − 实际到达时间。正数=满足，负数=违例。 |
| **critical path（关键路径）** | 全设计里延迟最长、slack 最小（最容易违例）的那条路径。 |
| **TNS（Total Negative Slack）** | 所有违例路径的 slack 之和（只统计负数部分）。=0 说明没有违例。 |
| **uncertainty（时钟不确定性）** | 给时钟留的「保险」，提前把这段时间扣除掉。 |
| **false path（虚假路径）** | 告诉工具「这条路径不会真的发生，别去查它」。 |
| **leaf cell（叶单元）** | 标准单元库里一个具体的门（如一个反相器、一个 D 触发器），不可再分。 |
| **DRC（设计规则）** | max_transition / max_capacitance / max_fanout 等电气规则，和时序是两类不同的检查。 |

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用它做什么 |
|------|------|----------------|
| [syn/cons/cons.tcl](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/cons/cons.tcl) | 约束脚本，36 行 | 逐行精读每条约束 |
| [syn/report/synth_qor.rpt](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_qor.rpt) | 综合质量总报告（一页纸总览） | 读时序、单元数、面积、DRC |
| [syn/report/synth_area.rpt](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_area.rpt) | 面积报告（含层次分解） | 看各子模块面积占比 |
| [syn/report/synth_power.rpt](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_power.rpt) | 功耗报告（含层次分解） | 看各子模块功耗占比 |
| [syn/report/synth_time.rpt](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_time.rpt) | 最差路径详细报告（辅助） | 逐点看关键路径如何走完 |
| [README.md](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/README.md) | 项目说明 | 核对 3ns 周期目标与面积声明 |

> 说明：本讲把 `cons.tcl` 与三份报告对照着讲——**左边写约束，右边看结果**，这是后端工程师日常工作的核心动作。

---

## 4. 核心概念与源码讲解

本讲按「写约束 → 看结果」的顺序，拆成 5 个模块：时钟与 I/O 约束、时序裕量管理、wireload/面积/扇出约束、QoR 时序与单元面积解读、层次面积与功耗分布。

### 4.1 时钟与 I/O 约束

#### 4.1.1 概念说明

时钟是整个时序分析的**基准节拍**。综合工具必须先知道「时钟加在哪个端口、周期多长」，才能算出每条路径有多少可用时间。本设计的时钟直接加在 `tpu_top` 的 `clk` 端口上。

有了时钟之后，还要处理两件事：

- **I/O 延迟**：芯片内部这条路径的两端，可能并不全在芯片内部——起点可能来自外部输入端口，终点可能是外部输出端口。约束要告诉工具「外部输入数据相对于时钟沿有多晚才到」「外部输出要在时钟沿之前多久就准备好」，这样工具才能正确评估穿过 I/O 的路径。
- **理想时钟网络**：在综合阶段，真实的时钟树（CTS）还没建（那是布局布线 PnR 的事），所以工具假设时钟是理想的、瞬间到达每个触发器，避免在综合阶段误判时序。

#### 4.1.2 核心流程

```
1. create_clock  → 在 clk 端口定义 3.01ns 周期时钟
2. set_ideal_network / set_dont_touch_network → 把时钟网络标为理想、不许动
3. set_input_delay  → 声明输入相对时钟晚到 1.505ns
4. set_output_delay → 声明输出要提前 1.505ns 准备好
```

这里有一个**贯穿全脚本的设计意图**：I/O 延迟取「半周期」。

\[
\text{input\_delay} = \text{output\_delay} = \frac{T_{clk}}{2} = \frac{3.01}{2} = 1.505\,\text{ns}
\]

也就是说，设计者把一个时钟周期**对半分**：前半周期留给外部输入/输出，后半周期留给芯片内部逻辑。这也解释了为什么时钟周期是 **3.01** 而不是整数 3——为了让它的一半正好是 1.505，便于 I/O 预算分配（同时也比目标 3ns 留了 10ps 的小尾巴）。

#### 4.1.3 源码精读

定义时钟，周期 3.01ns，挂在 `clk` 端口（这是整个时序分析的基准）：

[创建 3.01ns 时钟](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/cons/cons.tcl#L4-L4)

```tcl
create_clock -name clk -period 3.01 [get_ports clk]
```

把时钟网络标为理想、且不许工具改动（综合阶段时钟树还没建，先按理想处理）：

[时钟网络设为理想且不可触碰](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/cons/cons.tcl#L8-L9)

```tcl
set_ideal_network       [get_ports clk]
set_dont_touch_network  [all_clocks]
```

I/O 延迟。注意 `[remove_from_collection [all_inputs] [get_ports clk]]`——把所有输入端口的延迟设为 1.505ns，但要**先把 clk 本身从输入集合里剔除**（时钟端不算数据输入）：

[输入/输出延迟各设为半周期 1.505ns](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/cons/cons.tcl#L13-L14)

```tcl
set_input_delay  -max 1.505 -clock [get_clocks clk] [remove_from_collection [all_inputs] [get_ports clk]]
set_output_delay -max 1.505 -clock [get_clocks clk] [all_outputs]
```

#### 4.1.4 代码实践

**目标**：验证「I/O 延迟 = 半周期」这一关系，并理解 `remove_from_collection` 的必要性。

**步骤**：
1. 打开 `cons.tcl`，确认 `create_clock -period` 的值。
2. 用计算器算 `period / 2`，与 `set_input_delay`、`set_output_delay` 的 `-max` 值比对。
3. 想一想：如果删掉 `[remove_from_collection ... [get_ports clk]]`，直接对 `[all_inputs]` 设 input delay，会发生什么？

**预期**：3.01 / 2 = 1.505，与脚本完全吻合。若不剔除 clk，时钟端口会被当成「数据输入」额外加上 1.505ns 的延迟，导致时钟本身的分析被污染——这是初学者常犯的错误，脚本用 `remove_from_collection` 规避了它。

#### 4.1.5 小练习与答案

**练习 1**：如果把时钟周期从 3.01ns 改成 4.0ns，按「半周期」约定，`set_input_delay` 应改成多少？
**答案**：4.0 / 2 = 2.0ns。

**练习 2**：为什么对 `[all_outputs]` 设 output delay 时不需要像输入那样剔除 clk？
**答案**：因为 clk 是输入端口，本就不在 `[all_outputs]` 集合里，无需剔除。

---

### 4.2 时序裕量管理：clock uncertainty、false path 与 hold 修复

#### 4.2.1 概念说明

光有时钟和 I/O 延迟还不够，真实的芯片里时钟不会「绝对准时」——工艺偏差、电压波动、串扰都会让时钟沿抖动。为了不让综合结果「卡在刀尖上」，设计者会主动扣除一段 **clock uncertainty（时钟不确定性）** 作为保险：工具算可用时间时，先减掉这段。

另外有两类路径**在功能上根本不会发生**，却会被工具当成需要检查的路径，白白浪费优化力气，甚至报出假违例：

- **hold（保持）违例**：检查的是「数据不能到得太快」。对纯输入端口→内部、内部→纯输出端口这类路径，hold 检查在综合阶段意义不大（真实 hold 修复要等时钟树建好之后），所以用 **false path -hold** 把它们从 hold 检查里排除。

最后，`set_fix_hold` 是**打开**工具的 hold 自动修复能力（注意它只是「授权」，是否真插延迟要去看报告）。

#### 4.2.2 核心流程

setup 检查下，一条路径的「可用时间」预算是这样扣出来的：

\[
T_{\text{required}} = T_{clk} - T_{\text{uncertainty}} - t_{\text{setup}}
\]

本设计里：

\[
T_{\text{required}} = 3.01 - 0.20 - 0.0309 = 2.7791\,\text{ns}
\]

其中 0.0309ns 是 `synth_time.rpt` 记录的库 setup 时间。这段 0.20ns 的 uncertainty 就是留给时钟抖动与后续 PnR 的保险。

#### 4.2.3 源码精读

设 0.20ns 时钟不确定性（setup 检查会先把这段时间扣掉）：

[时钟不确定性 0.20ns](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/cons/cons.tcl#L16-L16)

```tcl
set_clock_uncertainty 0.20 [get_clocks]
```

把「输入端口的 hold 检查」和「输出端口的 hold 检查」标记为 false path（综合阶段不查 hold，留给 PnR 建好时钟树后再做）：

[对 I/O 路径关闭 hold 检查](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/cons/cons.tcl#L17-L18)

```tcl
set_false_path -hold -from [remove_from_collection [all_inputs] [get_ports clk]]
set_false_path -hold -to   [all_outputs]
```

授权工具做 hold 修复（仅授权，实际效果看 `synth_hold.rpt`）：

[打开 hold 修复](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/cons/cons.tcl#L35-L35)

```tcl
set_fix_hold [get_clocks clk]
```

#### 4.2.4 代码实践

**目标**：体会 uncertainty 对 slack 的影响。

**步骤**：
1. 假设当前最差路径的 data arrival time = 2.7466ns（来自 `synth_time.rpt`）。
2. 分别按 uncertainty = 0.20ns 和 uncertainty = 0ns 计算 `T_required` 与 slack。
3. 对比两次 slack，体会「保险」要付出的代价。

**预期**：
- uncertainty = 0.20：T_required = 2.7791，slack = 2.7791 − 2.7466 = **+0.0325ns（MET）**。
- uncertainty = 0：T_required = 3.01 − 0 − 0.0309 = 2.9791，slack = **+0.2325ns**。

可见 uncertainty 把 setup 余量从 0.23ns 压到了 0.03ns——留了保险，但设计也因此「几乎贴着时序边缘」，这正是下一模块 QoR 里 slack 只有 0.03 的原因。

#### 4.2.5 小练习与答案

**练习**：如果设计者把 `set_clock_uncertainty` 从 0.20 调大到 0.30，QoR 里的 slack 会变成多少？还满足时序吗？
**答案**：T_required = 3.01 − 0.30 − 0.0309 = 2.6791，slack = 2.6791 − 2.7466 = **−0.0675ns（VIOLATED）**。保险开太大反而会把本来满足的时序变成违例——uncertainty 不是越大越好。

---

### 4.3 线负载模型、面积与扇出约束

#### 4.3.1 概念说明

综合阶段**还没有真实的版图连线**，那么工具怎么知道一根导线有多长、带来多大延迟和电容？答案是**线负载模型（Wire Load Model, WLM）**：一套根据「设计面积/规模」估算平均连线长度的统计模型。本设计用 `enclosed`（包围）模式——以把当前层次完全包围住的最小子设计的 WLM 来估算。

除此之外，脚本还设了两类约束：

- **面积约束** `set_max_area 0`：这是 DC 的「无下限最小化」写法——告诉工具「面积越小越好，不要设下限」。工具会在时序满足的前提下，尽量把面积往小压。
- **扇出/设计规则约束** `set_max_fanout 1.64`：限制一根线能驱动的负载。脚本注释里给出了推导：最小 `fanout_load` 0.041 × WLM 最大扇出 20 = 0.82，再 ×2 留余量 = **1.64**。这是一个**保守的电气规则**约束，和时序是两套独立的检查。

#### 4.3.2 核心流程

```
1. 选线负载模型与模式 → 综合阶段估计连线延迟/电容
2. set_max_area 0    → 在满足时序前提下尽量压面积
3. set_max_fanout    → 限制单线负载（DRC 规则，与 timing 独立）
```

一个**关键认知**：`set_max_area 0` 与 `set_max_fanout` 都是「目标/规则」，不是「保证」。最终面积是多少、有没有扇出违例，必须去看报告（`synth_qor.rpt` 的 Design Rules 段、`synth_area.rpt`），不能假设设了就一定满足。

#### 4.3.3 源码精读

线负载模型三件套——自动选择 + enclosed 模式 + 指定选择组：

[线负载模型设置](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/cons/cons.tcl#L22-L24)

```tcl
set auto_wire_load_selection area_reselect
set_wire_load_mode enclosed
set_wire_load_selection_group predcaps
```

最大扇出（DRC 约束，注释解释了 0.041×20×2=1.64 的来源）：

[最大扇出 1.64（保守 DRC 约束）](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/cons/cons.tcl#L26-L30)

```tcl
# Defensive setting: smallest fanout_load 0.041 and WLM max fanout # 20 => 0.041*20 = 0.82
set_max_fanout 1.64 $design
```

面积无下限最小化：

[面积越小越好](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/cons/cons.tcl#L32-L33)

```tcl
# Area Constraint
set_max_area   0
```

#### 4.3.4 代码实践

**目标**：理解「设了 `set_max_area 0`」与「面积真被压到 0」是两回事，并预告 DRC 违例。

**步骤**：
1. 打开 `synth_qor.rpt` 的 Area 段，读出 `Design Area`。
2. 对照 `set_max_area 0`，思考：面积为什么不是 0？
3. 打开 `synth_qor.rpt` 的 Design Rules 段，读出 `Max Fanout Violations` 的数量。

**预期**：Design Area ≈ 66191.44（绝非 0）——`set_max_area 0` 只是「尽量小」，在时序与功能约束下，面积有它的下限。同时你会发现 **Max Fanout Violations = 27175**——大量线超过了 1.64 的扇出限制。这说明：**时序满足了，但 DRC（扇出）违例一大堆**，这些要靠后续 PnR 阶段的缓冲器插入/单元放大来修（见 u5-l3）。

#### 4.3.5 小练习与答案

**练习 1**：`set_max_area 0` 里这个 `0` 是什么意思？是要求面积等于 0 吗？
**答案**：不是。`0` 是 DC 的惯用写法，表示「不设面积下限、尽量最小化」。实际面积由时序/功能约束共同决定，远大于 0。

**练习 2**：为什么 QoR 里 `Max Fanout Violations` 有 27175 个，但 `No. of Violating Paths`（时序违例）却是 0？
**答案**：因为这是**两类独立检查**。前者是 DRC 电气规则（线驱动负载过大），后者是 setup 时序检查。本设计时序过了，但扇出 DRC 没过，留待 PnR 修复。

---

### 4.4 综合结果解读一：QoR 时序、单元与面积

> 从本模块起，我们从「写约束」转到「看结果」。`synth_qor.rpt` 是综合后**最重要的一页纸总览**。

#### 4.4.1 概念说明

QoR（Quality of Results）报告把一次综合的**时序、单元数、面积、设计规则、编译耗时**压缩在一份报告里，是判断「这次综合成不成功」的第一手依据。读 QoR 的顺序通常是：

1. **先看时序段**：slack 正不正？有没有违例路径？TNS 是不是 0？
2. **再看单元/面积段**：规模多大？组合/时序单元比例如何？缓冲器占比高不高？
3. **最后看设计规则段**：有没有 DRC 违例（max_transition / max_capacitance / max_fanout）？

#### 4.4.2 核心流程：本设计的时序预算全景

把约束（`cons.tcl`）与最差路径（`synth_time.rpt`）对齐，本设计的完整时序预算如下：

```
时钟周期 T_clk                              = 3.01  ns
- clock uncertainty                         = 0.20  ns  （保险，cons.tcl 设）
- library setup time                        = 0.0309 ns  （库固有）
= data required time                        = 2.7791 ns
  data arrival time（最差路径，synth_time）  = 2.7466 ns
    └ input external delay                  = 1.505  ns  （I/O 半周期）
    └ 内部组合逻辑（QoR "Critical Path Length"）= 1.2416 ns ≈ 1.24 ns
= slack (MET)                               = +0.0325 ns
```

这里有一个值得注意的细节：QoR 报告里的 `Critical Path Length: 1.24` **不等于** `synth_time.rpt` 里的 data arrival time 2.7466。两者差出的正是 1.505ns 的输入外部延迟：

\[
2.7466 - 1.505 = 1.2416 \approx 1.24
\]

即 QoR 的「关键路径长度」统计的是**内部组合逻辑段**的延迟，而 `synth_time.rpt` 的到达时间则**含 I/O 外部延迟**。两者口径不同，但 slack 一致（都 ≈ +0.03ns）。

#### 4.4.3 源码精读

QoR 时序段——本设计的「成绩单」：

[QoR 时序段：slack 0.03、0 违例、0 TNS](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_qor.rpt#L10-L21)

```
Levels of Logic:              41.00      ← 关键路径穿过 41 级逻辑（偏长）
Critical Path Length:          1.24       ← 内部组合段延迟（不含 I/O 外部延迟）
Critical Path Slack:           0.03       ← setup 余量，正数=满足
Critical Path Clk Period:      3.01       ← 对应 cons.tcl 的时钟周期
Total Negative Slack:          0.00       ← 0 = 没有任何违例路径
No. of Violating Paths:        0.00       ← 0 条 setup 违例
Worst Hold Violation:          0.00       ← hold 也无违例
No. of Hold Violations:        0.00
```

单元计数段——规模与结构：

[QoR 单元计数：54885 个叶单元、2828 个触发器、0 个宏](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_qor.rpt#L24-L34)

```
Leaf Cell Count:              54885       ← 标准单元总数
Buf/Inv Cell Count:           33656       ← 其中缓冲器/反相器占 61%！
Combinational Cell Count:     52057       ← 组合单元
Sequential Cell Count:         2828       ← 时序单元（触发器）
Macro Count:                      0       ← 无 SRAM 宏（SRAM 在顶层外部，见 u1-l3）
```

面积段：

[QoR 面积段：Design Area 66191.44](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_qor.rpt#L37-L45)

```
Combinational Area:    53403.22
Noncombinational Area: 12788.22
Buf/Inv Area:          19856.10         ← 缓冲器占近 30% 面积
Cell Area:             66191.439634
Design Area:           66191.439634
```

设计规则段——时序过了，但扇出 DRC 没过：

[QoR 设计规则段：27175 条 max_fanout 违例](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_qor.rpt#L48-L55)

```
Total Number of Nets:         61482
Nets With Violations:         27175      ← 44% 的线有违例
Max Trans Violations:             0      ← 翻转时间 OK
Max Cap Violations:               0      ← 电容 OK
Max Fanout Violations:        27175      ← 全是扇出违例（cons.tcl 的 1.64 太严）
```

#### 4.4.4 代码实践（对应总实践任务的核心）

**目标**：判断本设计是否满足 3.01ns 时序，并解释 `Critical Path Clk Period` 与 README 中 3ns 目标的关系。

**步骤**：
1. 读 `synth_qor.rpt` 的时序段，记下 `Critical Path Slack`、`No. of Violating Paths`、`Total Negative Slack`。
2. 读 `synth_time.rpt`，找到最差路径的起止点和 slack 行。
3. 读 README 的 *Synthesize* 表，比对 `Cycle time` 与 QoR 的 `Critical Path Clk Period`。

**需要观察的现象与预期**：
- 时序：`slack (MET) 0.0325`、`No. of Violating Paths: 0`、`TNS: 0`——**满足 3.01ns 时序，但余量极小（仅 0.03ns）**。
- 最差路径（[synth_time.rpt:L17-L19](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_time.rpt#L17-L19)）：起点是**输入端口 `srstn`（复位）**，终点是 `systolic/matrix_mul_2D_reg[1][7][10]`（MAC 累加寄存器，见 u2-l2）。这说明最差路径来自**复位信号的高度扇出与门控**，而不是数据通路本身。
- 周期关系：QoR `Critical Path Clk Period: 3.01` ↔ README `Cycle time (3) ns`。**3.01ns 是约束脚本里写的真实周期，README 的「3」是它的整数化表述与设计目标**（332 MHz）。3.01 比 3 多出的 0.01ns 一方面让半周期 = 1.505 成整数位小数，另一方面也给报告留了一丁点尾巴。

> ⚠️ 一个需要诚实指出的出入：README *Synthesize* 表声称 `Total area 116493.18`，但本次提交的 `synth_qor.rpt` 记录 `Design Area = 66191.44`。两者不一致。按照「文档与源码矛盾时以源码（产物报告）为准」的原则（见 u1-l1），**本讲所有面积以报告的 66191.44 为准**；README 的 116493.18 可能来自另一次综合或不同统计口径（待确认）。

**若把周期收紧到 2.5ns，预判哪些指标会恶化**（请自行推算后对照）：

\[
T_{\text{required}}(2.5) = 2.5 - 0.20 - 0.0309 = 2.2691\,\text{ns}
\]

- 即使把 I/O 延迟也按半周期减半为 1.25ns，到达时间 = 1.25 + 1.24 = 2.49ns > 2.2691 → **slack ≈ −0.22ns（VIOLATED）**；若 I/O 延迟维持 1.505，违例更严重（≈ −0.48ns）。
- 恶化指标：`Critical Path Slack` 变负、`No. of Violating Paths` 从 0 暴增、`TNS` 显著为负；为追时序，DC 会拼命插缓冲器/换大单元 → **`Buf/Inv Area`（现 19856）、`Design Area`（现 66191）双双上升**；`Compile CPU Statistics`（现约 2035s）也会变长；功耗随之上升。
- 而 `Levels of Logic: 41` 偏高，意味着这条路径很难靠单纯插缓冲器修好，可能需要**插流水线寄存器**重构——那就是改 RTL 层面的事了。

#### 4.4.5 小练习与答案

**练习 1**：QoR 里 `Critical Path Length: 1.24` 和 `synth_time.rpt` 里 `data arrival time 2.7466` 差出的 1.505ns 是什么？
**答案**：是 `set_input_delay` 设的输入外部延迟。QoR 的 Critical Path Length 只统计内部组合逻辑段，不含 I/O 外部延迟；二者口径不同。

**练习 2**：`Buf/Inv Cell Count: 33656` 占了叶单元的 61%，这说明什么？
**答案**：说明综合工具为了满足时序（驱动大扇出、修复延迟）插入了大量缓冲器/反相器。这也是为什么后续模块里 `Buf/Inv Area` 占了近 30%——缓冲器是面积的「隐形成本」。

**练习 3**：最差路径的起点是 `srstn` 而非某个数据输入，这暗示了什么？
**答案**：复位信号 `srstn` 需要扇出到所有 2828 个触发器，是高扇出网络，经多级缓冲/门控后延迟很长。它成为关键路径说明复位树的优化空间最大。

---

### 4.5 综合结果解读二：层次面积与功耗分布

#### 4.5.1 概念说明

QoR 给的是**设计总体**数字，但「这 6 万多面积花在哪了」「谁最耗电」需要看**带层次分解**的 `synth_area.rpt`（`report_area -hier`）和 `synth_power.rpt`（`report_power -hier`）。这两份报告把面积/功耗按子模块层层拆开，是定位「优化重点」的依据。

功耗分三部分：
- **Switching Power（开关功耗）**：信号翻转时充放电电容消耗的动态功耗。
- **Internal Power（内部功耗）**：单元内部（如晶体管开关瞬间）消耗的动态功耗。
- **Leakage Power（漏电功耗）**：不翻转也存在的静态漏电。

> 注意：功耗报告头部有两条警告 `unannotated primary inputs` / `unannotated sequential cell outputs`（[synth_power.rpt:L3-L4](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_power.rpt#L3-L4)），且 `analysis_effort low`，说明这是**没有真实翻转活动**的粗略估计，数字仅供量级参考，不能当作精确功耗。

#### 4.5.2 核心流程

```
读 synth_area.rpt 层次表 → 找面积大头 → 锁定优化重点（本设计：systolic）
读 synth_power.rpt 层次表 → 找功耗大头 → 同样指向 systolic
```

本设计的结论一句话：**无论看面积还是功耗，`systolic`（8×8 脉动阵列本体）都是绝对大头**，这与 u5-l1 里它占 89.6% 面积的判断完全一致。

#### 4.5.3 源码精读

面积层次分解——`systolic` 一家独大：

[synth_area.rpt 面积总量](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_area.rpt#L24-L30)

```
Combinational area:       53403.22
Noncombinational area:    12788.22
Total cell area:          66191.439634
```

[synth_area.rpt 各子模块面积占比](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_area.rpt#L41-L43)

```
addr_sel            183.5400    0.3%
quantize            413.6300    0.6%
systolic          59306.0297   89.6%   ← 阵列本体
```

[synth_area.rpt write_out 与控制器面积](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_area.rpt#L236-L239)

```
systolic_controll   331.1700    0.5%   ← 主控状态机（u3-l1）
write_out          5944.3020    9.0%   ← 写回（u3-l3），第二大头
```

功耗层次分解——同样指向 `systolic`：

[synth_power.rpt 顶层与子模块功耗](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_power.rpt#L42-L46)

```
tpu_top            Switch 1.13e+03  Int 5.50e+03  Leak 2.95e+05  Total 6.92e+03  100%
  write_out         67.13    731.73    3.07e+04     829.55    12.0%
  systolic_controll  7.33     49.88    1.56e+03      58.77     0.8%
  systolic         1.05e+03  4.66e+03  2.59e+05    5.97e+03   86.2%   ← 阵列本体
```

把功耗单位理清（动态功耗 µW、漏电 nW）：顶层总功耗约 **6.92 mW**，其中动态 ≈ 6.63 mW、漏电 ≈ 0.295 mW。`systolic` 占 86.2%，`write_out` 占 12.0%，控制器和其它几乎可忽略。

#### 4.5.4 代码实践

**目标**：用层次报告定位优化重点，并理解「面积大头 = 功耗大头」。

**步骤**：
1. 在 `synth_area.rpt` 里找出占比最大的子模块，记下它的绝对面积与百分比。
2. 在 `synth_power.rpt` 里找出同一个子模块的功耗占比。
3. 思考：如果要降低本设计的功耗/面积，应该优先优化哪个模块？为什么 `write_out` 虽小（9%）却不容忽视？

**预期**：`systolic` 同时是面积（89.6%）和功耗（86.2%）的绝对大头——要降本，必须从脉动阵列本身（cell 数量、位宽、复用）入手。`write_out` 面积占 9%（5944），是第二大头，主要来自三端口写回的反对角线重排逻辑（u3-l3），是次要优化点。控制器 `systolic_controll` 仅 0.5%/0.8%，优化它收益极小。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `systolic` 的面积占比（89.6%）比它的「逻辑直觉」看起来还大？
**答案**：因为它内部有 64 个 cell，每个 cell 含 8×8 乘法器 + 21bit 累加器 + 移位队列，还要插大量缓冲器去满足时序（Buf/Inv Area 占近 30%）。乘法器阵列是面积杀手。

**练习 2**：功耗报告里的 Leakage（漏电）只有约 0.295 mW，远小于动态功耗，这正常吗？
**答案**：在 45nm 这一工艺节点、且采用慢角低电压（0.95V）的综合估计下，动态功耗主导是常见的。但要注意这是 low-effort、无真实翻转活动的粗估，真实漏电占比可能不同。

---

## 5. 综合实践

**任务**：作为「后端工程师」，给本次综合写一份一页纸的**时序收敛小结**（纯阅读型，不改源码）。

请综合本讲所有报告，回答以下问题，形成一份小结文档：

1. **时序结论**：本设计是否满足目标频率？引用 QoR 的 slack、违例路径数、TNS 三个数字作为证据。
2. **时序预算**：用本讲的预算表，说明 3.01ns 周期是如何被 `uncertainty(0.20)` + `setup(0.0309)` + `I/O(1.505)` + `内部逻辑(1.24)` 切分的，最终剩多少余量。
3. **规模与代价**：引用 QoR 给出叶单元数、触发器数、Design Area，并用 `synth_area.rpt` 指出面积大头；用 `synth_power.rpt` 指出功耗大头。
4. **遗留问题**：指出 QoR Design Rules 段的 27175 条 max_fanout 违例，说明它们与时序违例的区别，以及交给哪个后续阶段（u5-l3 的 PnR）处理。
5. **风险预警**：基于本讲对「收紧到 2.5ns」的推算，指出本设计时序余量极小（仅 0.03ns）这一风险，并给出一条不改 RTL 之外的应对建议（例如：放宽 `set_max_fanout`、调小 uncertainty、换更快工艺角等，任选一条并说明权衡）。

**预期产出**：一份 200~400 字的小结，所有结论都带报告里的具体数字佐证，不写空话。这份小结本身就是真实芯片项目里综合工程师交付给后端/架构师的标准产物。

## 6. 本讲小结

- `cons.tcl` 用 `create_clock -period 3.01` 建立时序基准，I/O 延迟取**半周期** 1.505ns（= 3.01/2），把周期对半分给外部与内部。
- `set_clock_uncertainty 0.20` 主动扣除保险，`set_false_path -hold` 关闭综合阶段无意义的 I/O hold 检查，`set_fix_hold` 授权 hold 修复。
- `set_max_area 0` 是「尽量小」而非「等于 0」；`set_max_fanout 1.64` 是保守 DRC 约束——两者都与时序检查相互独立。
- QoR 显示**时序满足**：slack +0.03、0 违例、0 TNS；但 `Critical Path Length 1.24` 只含内部逻辑，完整到达时间 2.7466 还要加上 I/O 外部延迟 1.505。
- **DRC 有 27175 条 max_fanout 违例**（时序过、扇出没过），留待 PnR 修复；最差路径起点是复位 `srstn`，提示复位树是优化重点。
- 面积/功耗**大头都是 `systolic`**（面积 89.6%、功耗 86.2%）；报告面积 66191.44 与 README 声称的 116493.18 不一致，以报告为准。

## 7. 下一步学习建议

- **进入 u5-l3（ICC2 布局布线）**：本讲遗留的 27175 条 max_fanout 违例，正是 PnR 阶段通过缓冲器插入与单元放大要解决的对象；同时 PnR 会用真实连线替换本讲的线负载模型（WLM）估计，时序数字会重新收敛。
- **回看 u5-l1**：把本讲的「约束 → 报告」对照 u5-l1 的「流程」，完整理解 `syn.tcl` 里 `source cons.tcl → compile_ultra → report_*` 这条主线的因果关系。
- **延伸阅读**：`syn/report/synth_setup.rpt`（4 条最差 setup 路径）、`synth_hold.rpt`（hold 路径）、`report_violation.rpt`（全部违例）可帮助你进一步练习读报告；`command.log` 记录了综合全程的每一条命令，是复盘综合的最佳材料。
