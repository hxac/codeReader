# 综合约束与时序模型

## 1. 本讲目标

上一讲（u6-l1）我们把 `syn.tcl` 跑通了一遍，知道了综合的"五阶段"主流程，并提到约束脚本 `cons.tcl` 是被 `source` 进来的。本讲就专门拆开这个 24 行的约束文件，回答一个问题：

> **"综合工具凭什么知道这颗 FFT 要跑 100 MHz？它又是怎么把现实世界里电压、温度、连线寄生这些不确定因素算进去的？"**

学完本讲你应该能够：

- 看懂 `create_clock`、`set_input_delay`、`set_output_delay` 三条命令，并算出三类路径（输入→寄存器、寄存器→寄存器、寄存器→输出）各自的可用时间窗口；
- 解释 `set_clock_uncertainty` / `set_clock_latency` / `set_false_path -hold` 在做时序例外和裕量预留；
- 理解 `set_operating_conditions` 的多工况（MMMC）思想：用 **ss 慢角查建立时间（setup）**、**ff 快角查保持时间（hold）**，以及 wireload 模型如何在布线前"猜"出连线电容；
- 说出 `group_path` 三个分组（INREG / REGOUT / INOUT）的用途，并能对照 QoR 报告解释为什么 INOUT 分组"消失"了。

本讲是纯"读脚本 + 算预算"的练习，不需要你真的启动 Design Compiler，但我们会用仓库里已经生成好的 `synth_qor.rpt`、`synth_timing.rpt`、`FFT.sdc` 来验证手算结果。

## 2. 前置知识

在进入约束文件之前，先用三段白话建立直觉。

**（1）什么是"时序约束"？**

综合工具（DC）默认并不知道你的设计要跑多快。它只会把 RTL 里的 `+`、`*`、寄存器映射成门电路。如果你不告诉它目标频率，它就随便布一通。**约束（constraints）就是你写给工具的"设计规格书"**：时钟多快、信号从外面进来时已经迟到了多少、信号出去后还要给下游留多少时间、在什么温度电压下工作……工具拿到这些后，才会在"满足时序"和"少用面积"之间做权衡。

**（2）建立时间（setup）与保持时间（hold）——一对孪生检查。**

每个触发器都有两个要求：

- **建立时间 \(T_{setup}\)**：在时钟沿到来**之前**，数据必须提前稳定这么久。否则触发器采到的可能是半个旧值、半个新值。这是"快不快"的问题，**周期越长越容易满足**。
- **保持时间 \(T_{hold}\)**：在时钟沿到来**之后**，数据必须继续稳定这么久。否则刚采到的数据就被下一拍冲掉了。这是" launch 和 capture 错位"的问题，**和周期长短无关**。

综合主要检查建立时间（周期给得起吗），保持时间往往留到布线后再修（因为 hold 依赖最小延迟，布线前算不准）。

**（3）PVT 工艺角（corner）。**

芯片造出来后，工作在什么工艺偏差（P）、什么电压（V）、什么温度（T）下都不确定。组合起来有"最慢角 ss（低电压、高温、慢晶体管）"和"最快角 ff（高电压、低温、快晶体管）"。直觉上：

- ss 角延迟最大 → **最容易违建立时间**，所以 setup 用 ss 查；
- ff 角延迟最小 → launch 飞快、capture 也飞快 → **最容易违保持时间**，所以 hold 用 ff 查。

这就是"多角（multi-corner）"分析的由来。本讲的 `set_operating_conditions` 一行就是在配置这件事。

> 术语速查：`clk`（时钟）、`setup`/`hold`（建立/保持）、`slack`（裕量，正值=满足、负值=违例）、`WCCOM`/`BCCOM`（UMC 130nm 库里的最差/最佳工作条件名）、`wireload`（连线负载模型）、`MMMC`（Multi-Mode Multi-Corner，多模式多角）。

## 3. 本讲源码地图

本讲围绕一个核心文件，并用三个报告文件做交叉验证：

| 文件 | 作用 | 本讲怎么用 |
|------|------|-----------|
| `SYN/cons/cons.tcl` | **约束脚本（本讲主角）**，24 行，定义时钟、IO 延迟、时序例外、工艺角、面积目标 | 逐行精读 |
| `SYN/scripts/syn.tcl` | 综合主脚本 | 只看第 18–20 行：`cons.tcl` 如何被 `source` 进来 |
| `SYN/report/synth_qor.rpt` | 综合质量报告 | 读取三个路径组的 slack，验证手算预算 |
| `SYN/report/synth_timing.rpt` | 关键路径详情 | 验证 ideal clock latency=0.5、工艺角=WCCOM |
| `SYN/output/FFT.sdc` | 综合后吐出的 SDC | 看 `all_inputs`/`all_outputs` 集合被展开成哪些端口 |

约束文件本身的加载位置在综合主脚本里：

[SYN/scripts/syn.tcl:18-20](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/scripts/syn.tcl#L18-L20) —— 先 `check_design` 体检，再 `source ./cons/cons.tcl` 载入约束，最后 `link` 把设计跟工艺库连起来。约束必须在 `link` 之前生效，这样 `compile_ultra` 才能"看着约束去优化"。

> 踩坑提示：`cons.tcl` 通篇用 `[all_inputs]`、`[all_outputs]` 这种**端口集合**写法，而不是点名 `din_r[0]`、`reset` 之类。这样做很健壮——哪怕端口改名也能照常工作。值得注意的是：RTL 顶层（`RTL/FFT.v`）的复位端口叫 `reset`，而综合后吐出的网表/SDC 里却变成了 `rst_n`（见 [SYN/output/FFT.sdc:114](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/output/FFT.sdc#L114)），复位极性也对不上（RTL 高有效、SDC 名字暗示低有效）。这正是 u5-l1 已经记录在案的一处项目不一致；本讲因为用的是集合写法，所以不受影响。

## 4. 核心概念与源码讲解

本讲把 24 行 `cons.tcl` 拆成四个最小模块。

### 4.1 时钟与 I/O 延迟约束

#### 4.1.1 概念说明

时钟是整个芯片的心跳。`create_clock` 一行就定义了这个心跳：多快（周期）、高低电平各占多久（占空比）。

但仅有时钟还不够。FFT 不是一个孤岛——它的输入是上游芯片送来的，输出要喂给下游芯片。上游送来的数据相对于时钟沿可能已经迟到了一段时间（`set_input_delay`），下游要可靠采样，又要求我们的输出信号比下一个时钟沿提前稳定一段时间（`set_output_delay`）。这两条命令本质上是**把芯片外部的时序预算"借"进/"借"出**，让工具知道端口以内的逻辑还剩多少时间可用。

#### 4.1.2 核心流程

时钟周期 \(T_{clk}=10\,\text{ns}\)（对应 100 MHz），波形 `{0 5}` 表示 0 ns 上升、5 ns 下降，占空比 50%。三类路径的**可用时间窗口**各不相同：

\[ T_{\text{可用}}^{INREG} = T_{clk} - T_{\text{input\_delay}} - T_{\text{uncertainty}} - T_{setup} \quad (\text{输入}\to\text{第一级寄存器}) \]

\[ T_{\text{可用}}^{clk} = T_{clk} - T_{\text{uncertainty}} - T_{setup} \quad (\text{寄存器}\to\text{寄存器}) \]

\[ T_{\text{可用}}^{REGOUT} = T_{clk} - T_{\text{output\_delay}} \quad (\text{末级寄存器}\to\text{输出端口}) \]

把 10 ns、5 ns、0.1 ns（见 4.2）代入，得到三档预算：

| 路径类型 | 公式代入 | 可用窗口（忽略 \(T_{setup}\)） |
|---------|---------|-----------------------------|
| INREG（输入→寄存器） | \(10-5-0.1\) | ≈ 4.9 ns |
| clk（寄存器→寄存器） | \(10-0.1\) | ≈ 9.9 ns |
| REGOUT（寄存器→输出） | \(10-5\) | = 5.0 ns |

注意：**不能把 5 ns 输入延迟和 5 ns 输出延迟同时从同一个 10 ns 周期里扣掉**——它们分别属于不同的路径。唯一会同时沾上两者的，是"输入端口直接组合逻辑到输出端口"的纯透传路径（INOUT 组），而本设计是全流水线寄存化的，**根本不存在**这种透传路径（4.4 会用 QoR 证实）。

#### 4.1.3 源码精读

[SYN/cons/cons.tcl:1](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/cons/cons.tcl#L1) —— 定义时钟：周期 10 ns、波形 `{0 5}`、绑到端口 `clk` 上。这就是 100 MHz 的来源。

```tcl
create_clock -name clk -period 10 -waveform {0 5} [get_ports clk]
```

[SYN/cons/cons.tcl:2-3](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/cons/cons.tcl#L2-L3) —— IO 延迟。`-max 5` 表示最坏情况下外部数据迟到 5 ns / 输出要提前 5 ns；`[remove_from_collection [all_inputs] [get_ports clk]]` 把时钟端口从输入集合里抠掉（时钟自己就是参考，不能再给自己加延迟）。

```tcl
set_input_delay  -max 5 -clock [get_clocks clk] [remove_from_collection [all_inputs] [get_ports clk]]
set_output_delay -max 5 -clock [get_clocks clk] [all_outputs]
```

[SYN/cons/cons.tcl:10-11](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/cons/cons.tcl#L10-L11) —— 驱动强度与负载。`set_drive 1` 假设每个输入端口由一个阻值为 1 的理想驱动源推动（用来算输入翻转时间）；`set_load 1` 假设每个输出端口挂 1 pF 外部负载（用来算输出翻转时间和延迟）。这两条让端口以内的延迟估算更真实。

#### 4.1.4 代码实践

**目标：用 QoR 报告反推 INREG 组的内部延迟。**

1. 打开 [SYN/report/synth_qor.rpt:10-21](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_qor.rpt#L10-L21)，读出 INREG 组的 `Critical Path Slack = 4.68`、`Levels of Logic = 0.00`。
2. 用上面 INREG 的公式：\(T_{\text{可用}} \approx 10-5-0.1 = 4.9\,\text{ns}\)。
3. 那么"端口→第一级寄存器"的实际数据延迟（含 setup）= \(4.9 - 4.68 = 0.22\,\text{ns}\)。
4. 对照 `Levels of Logic = 0`：0 级逻辑意味着输入端口几乎直连进触发器，所以 0.22 ns 基本就是连线 + 触发器 setup 时间，完全合理。

**预期结果**：你算出的 0.22 ns 与"0 级逻辑"互相印证，说明约束和报告是自洽的。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `set_input_delay` 从 5 ns 改成 8 ns，INREG 组的可用窗口会变成多少？设计还能满足时序吗？

**答案**：可用窗口 = \(10-8-0.1 = 1.9\,\text{ns}\)（再扣 setup）。原本内部延迟就 ~0.22 ns，依然满足；但如果某条输入路径带了组合逻辑，1.9 ns 就会非常紧张。

**练习 2**：为什么 `set_input_delay` 的对象要 `remove` 掉 `clk` 端口？

**答案**：因为 `clk` 端口本身就是时钟源，是建立时间检查的**参考基准**；如果给参考基准再叠加一个 input delay，就等于"把尺子自己也量进去"，会双重计算。

### 4.2 时序例外与不确定性

#### 4.2.1 概念说明

时钟定义只是"理想心跳"。现实中，时钟信号到达每个触发器的时间会有偏差（时钟偏斜 skew）、会抖动（jitter），综合阶段还根本没有真实的时钟树（CTS 在后端 Innovus 才做）。约束文件用一组命令来"提前打补丁"：

- `set_clock_uncertainty`：给时钟留一点裕量，模拟未来的 skew + jitter；
- `set_clock_latency`：在没有真实时钟树时，用一个估计的插入延迟；
- `set_false_path -hold` + `set_fix_hold`：把 IO 路径上的保持时间检查**先关掉**，留到布线后再修；
- `set_dont_touch_network`：禁止综合工具缓冲/克隆时钟网络（时钟树交给后端）。

#### 4.2.2 核心流程

带不确定性的建立时间裕量公式（寄存器到寄存器）：

\[ \text{slack}_{setup} = T_{clk} - T_{\text{uncertainty}} - T_{\text{data}} - T_{setup} \]

\(T_{\text{uncertainty}}=0.1\,\text{ns}\) 把可用窗口从 10 ns 压到 9.9 ns。`set_clock_latency 0.5` 因为是 ideal 时钟，对 launch 和 capture 对称施加，**在 reg-to-reg 的 slack 计算里相互抵消**，只在路径报告里以"clock network delay (ideal) 0.50"的形式出现（见 4.2.3 验证）。

为什么敢把 IO 的 hold 检查关掉？因为 hold 违例取决于**最小延迟路径**，而布线前的最小延迟纯粹是 wireload 猜的，不准；与其被假违例干扰，不如 `set_false_path -hold` 暂时屏蔽，等后端有了真实连线，再由 `set_fix_hold` 在芯片内部插延迟缓冲来修。

#### 4.2.3 源码精读

[SYN/cons/cons.tcl:4-5](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/cons/cons.tcl#L4-L5) —— 不确定性与理想延迟：

```tcl
set_clock_uncertainty 0.1 [get_clocks clk]   ;# 预留 0.1 ns 给 skew+jitter
set_clock_latency     0.5 [get_clocks clk]   ;# 理想时钟树插入延迟估计
```

[SYN/cons/cons.tcl:6-9](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/cons/cons.tcl#L6-L9) —— 时序例外与时钟保护：

```tcl
set_false_path -hold -from [remove_from_collection [all_inputs] [get_ports clk]]
set_false_path -hold -to   [all_outputs]
set_fix_hold        [get_clocks clk]          ;# 授权工具后续在内部插缓冲修 hold
set_dont_touch_network [get_ports clk]        ;# 别在综合阶段动时钟网络
```

验证 `set_clock_latency 0.5` 确实生效且为 ideal：见 [SYN/report/synth_timing.rpt:36-37](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_timing.rpt#L36-L37)，关键路径报告里赫然写着 `clock network delay (ideal) 0.50`。

#### 4.2.4 代码实践

**目标：在时序报告里找到 0.1 ns 不确定性对应的"扣减项"。**

1. 打开 `SYN/report/synth_timing.rpt`，沿关键路径一路读到末尾的 `data arrival time` 和 `data required time` 两段。
2. 在 `data required time` 段里，你会看到一行 `clock uncertainty -0.10`——这就是 `set_clock_uncertainty 0.1` 在 required time 上扣掉的 0.1 ns。
3. 再找 `clock network delay (ideal) 0.50`，确认它同时出现在 launch 和 capture 两侧，所以对 slack 净影响为 0。

**预期结果**：required time 比"纯周期"少了约 0.1 ns + setup，正好对应 9.68 ns 关键路径下 slack = 0.00 的压线状态。**待本地验证**（需要能浏览完整 timing 报告尾部）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `set_clock_latency` 对寄存器到寄存器的 slack 没有影响，却仍然要设？

**答案**：因为它对称施加在 launch 和 capture 触发器上，相减抵消。但它会影响"输入→寄存器"和"寄存器→输出"这两类路径的延迟估算（这两类只有一端有 latency），所以仍需设一个合理估值。

**练习 2**：`set_false_path -hold` 关掉的是建立还是保持检查？为什么偏偏关这一种？

**答案**：关的是**保持（hold）**检查。因为 hold 依赖最小延迟，布线前的 wireload 模型估不准最小延迟，提前检查会产生大量假违例；建立时间依赖最大延迟，估得相对准，所以保留。

### 4.3 多工况操作条件（MMMC）与 wireload 模型

#### 4.3.1 概念说明

芯片在工作时，电压和温度会浮动，晶体管的快慢也会因工艺偏差而不同。设计必须保证**在最恶劣的条件下依然能用**。`set_operating_conditions` 这一行就是告诉工具：请同时在"最慢角（max）"和"最快角（min）"两个库里分析。

- **max 库 = ss 角**（`fsc0h_d_generic_core_ss1p08vm40c`，1.08 V / 125 ℃，条件名 `WCCOM`）→ 延迟最大 → 查**建立时间**；
- **min 库 = ff 角**（`fsc0h_d_generic_core_ff1p32vm40c`，1.32 V / -40 ℃，条件名 `BCCOM`）→ 延迟最小 → 查**保持时间**。

至于 `set_wire_load_model`：综合阶段还没有真实的金属连线，工具怎么知道一根线带多少电容？答案是查一张**经验表**——wireload 模型。它根据一个网络驱动的扇出个数，估算这根线的电容和电阻。`G5K` 就是 UMC 130nm 库提供的一种 wireload 模型名。

#### 4.3.2 核心流程

多角分析的分工：

\[ \text{setup 检查} \xrightarrow{\text{用}} \text{ss / WCCOM（max，慢，延迟大）} \]

\[ \text{hold 检查} \xrightarrow{\text{用}} \text{ff / BCCOM（min，快，延迟小）} \]

wireload 估算模式（`Wire Load Model Mode: enclosed`，见时序报告第 15 行）：当一个子模块被更大的模块包住时，用**最外层包围模块**的 wireload 模型来估算其内部连线，避免边界处的估算突变。

#### 4.3.3 源码精读

[SYN/cons/cons.tcl:19-20](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/cons/cons.tcl#L19-L20) —— 多工况与 wireload：

```tcl
set_operating_conditions -min_library fsc0h_d_generic_core_ff1p32vm40c -min BCCOM \
                         -max_library fsc0h_d_generic_core_ss1p08v125c -max WCCOM
set_wire_load_model -name G5K -library fsc0h_d_generic_core_ss1p08v125c
```

验证多角与 wireload 确实被综合采用了：[SYN/report/synth_timing.rpt:14-15](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_timing.rpt#L14-L15) 显示 `Operating Conditions: WCCOM`（用慢角查 setup）、`Wire Load Model Mode: enclosed`；报告第 27–32 行还列出各子模块用了 `G5K`/`enG10K`/`enG5K` 等不同 wireload（按模块规模自动选档）。这条命令也被原样写进了综合后 SDC：[SYN/output/FFT.sdc:9-13](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/output/FFT.sdc#L9-L13)。

#### 4.3.4 代码实践

**目标：把库名拆成"电压+温度"四要素。**

1. 读 `fsc0h_d_generic_core_ss1p08vm40c` 这个库名：`ss` = 慢工艺、`1p08v` = 1.08 V、`m40` = -40 ℃……等等，注意 ss 角却是 `125c`（高温），而 `m40`（-40 ℃）出现在 ff 角 `ff1p32vm40c` 里。请对照 4.3.1 的结论核对这些参数。
2. 在 [SYN/scripts/syn.tcl:5-6](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/scripts/syn.tcl#L5-L6) 里确认：`target_library`（映射目标，决定面积/延迟）只选了 ss 角，而 `link_library`（可引用库）同时挂了 ss 和 ff 两角——这与"setup 用 ss、hold 用 ff"的多角思想一致。

**预期结果**：库名的 `ss/ff`、`1p08v/1p32v`、`125c/m40c` 三组对比，正好对应 PVT 的三个维度。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `target_library` 只用 ss 角，而 `link_library` 却要同时挂 ss 和 ff？

**答案**：`target_library` 是把逻辑**映射成具体单元**的库，用最慢角映射能保证映射后的电路在最恶劣条件下仍达标（保守、留余量）；`link_library` 提供"已存在单元"的时序模型供多角分析，所以要把 ff 角也挂上以便查 hold。

**练习 2**：布线之后，`set_wire_load_model` 估算的连线电容会被什么取代？

**答案**：会被 Innovus 提取的真实寄生（`extractRC`，见 u6-l4）取代。所以 wireload 只是布线前的"占位估计"，签核时序以布线后的真实 RC 为准。

### 4.4 面积优化与路径分组

#### 4.4.1 概念说明

`group_path` 把成千上万条时序路径按"起点/终点类型"分进几个篮子。这样做有两个好处：一是**报告更清晰**（QoR 会按组分别报 slack，一眼看出哪类路径最紧）；二是**优化更聚焦**（可以针对某个组单独下优化力度）。

本设计定义了三个组：

- **INREG**：从所有输入端口出发的路径（输入→内部寄存器）；
- **REGOUT**：到所有输出端口结束的路径（内部寄存器→输出）；
- **INOUT**：从输入端口**直达**输出端口的纯组合路径。

此外，DC 会自动建一个 **clk 组**，装所有"寄存器→寄存器"路径。所以最终理论上有 4 个组。

剩下的两条：`set_max_area 0` 是"在满足时序的前提下，面积越小越好"的硬指标；`set_boundary_optimization` 允许工具跨模块边界（如 `radix_no1`、`shift_16` 之间）做优化，打破层次墙以换取更好的结果。

#### 4.4.2 核心流程

路径分组逻辑（伪代码）：

```
对每条时序路径 P:
    if P.start ∈ all_inputs and P.end ∈ all_outputs:  归入 INOUT
    elif P.start ∈ all_inputs:                         归入 INREG
    elif P.end ∈ all_outputs:                          归入 REGOUT
    else:                                              归入 clk（寄存器→寄存器）
```

面积目标的含义：`set_max_area 0` 不是"面积做成 0"，而是"以 0 为目标去逼近"——即在不违时的前提下无限趋近最小面积。

#### 4.4.3 源码精读

[SYN/cons/cons.tcl:13-15](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/cons/cons.tcl#L13-L15) —— 三个路径分组：

```tcl
group_path -name INREG  -from [all_inputs]
group_path -name REGOUT -to   [all_outputs]
group_path -name INOUT  -from [all_inputs] -to [all_outputs]
```

[SYN/cons/cons.tcl:17](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/cons/cons.tcl#L17) —— 端口网络分离，防止一个网络同时驱动多个端口导致网表导出问题：

```tcl
set_fix_multiple_port_nets -all -buffer_constants
```

[SYN/cons/cons.tcl:22-23](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/cons/cons.tcl#L22-L23) —— 面积目标与跨边界优化：

```tcl
set_max_area 0
set_boundary_optimization {"*"}
```

**关键验证——INOUT 组为什么"消失"了？** 打开 [SYN/report/synth_qor.rpt:10-47](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_qor.rpt#L10-L47)，QoR 只报告了 `INREG`、`REGOUT`、`clk` 三个组，**没有 INOUT**。原因正是：这颗 FFT 是全流水线寄存化的（5 级 radix2，级间都是寄存器/延时线），**没有任何一条从输入端口直通到输出端口的纯组合路径**，所以 INOUT 组是空的，DC 就不报它。这是"约束写了一个组、但设计里没有对应路径"的活教材。

#### 4.4.4 代码实践

**目标：用 QoR 数值填出"四组三档"对照表。**

1. 从 [SYN/report/synth_qor.rpt](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_qor.rpt) 读出三个组的 `Critical Path Slack` 和 `Critical Path Length`。
2. 填写：

| 组 | 关键路径长度 | Slack | 含义 |
|----|-----------|-------|------|
| INREG | 0.00 | 4.68 | 输入直进寄存器，几乎无逻辑 |
| REGOUT | 1.63 | 3.27 | 寄存器经 1 级逻辑到输出 |
| clk | **9.68** | **0.00** | 5 级蝶形串联，最紧、压线 |
| INOUT | —（无） | —（无） | 设计无透传路径，组为空 |

**预期结果**：clk 组 slack 恰好为 0.00，说明综合后 100 MHz 压线满足，没有任何违例（`No. of Violating Paths = 0`）。这与 u6-l1 的结论一致。

#### 4.4.5 小练习与答案

**练习 1**：如果未来给设计加了一条"旁路"，让 `din_r` 直接组合连到 `dout_r`（不经过寄存器），QoR 报告会发生什么变化？

**答案**：INOUT 组将不再为空，会出现在 QoR 里；且因为这条透传路径要同时承受 input_delay 和 output_delay，可用窗口只有 \(10-5-5-0.1=-0.1\,\text{ns}\)，几乎必然违例——这正是为什么高速设计都尽量避免组合透传。

**练习 2**：`set_max_area 0` 和"不设面积约束"有什么区别？

**答案**：不设时，工具只追求满足时序，可能用大单元堆出余量；设 0 则在满足时序后**继续压面积**到极限。代价是编译时间更长，且可能为了省面积而引入关键路径风险。

## 5. 综合实践

**综合任务：手算三类路径的建立时间预算，并用 QoR 报告交叉验证。**

本任务把本讲四个模块串起来，是"读脚本 + 算预算 + 对报告"的完整闭环。

### 步骤 1：算预算（不用启动 DC，纯手算）

依据 `cons.tcl` 的 \(T_{clk}=10\,\text{ns}\)、\(T_{\text{input\_delay}}=T_{\text{output\_delay}}=5\,\text{ns}\)、\(T_{\text{uncertainty}}=0.1\,\text{ns}\)，填写下表（先忽略 \(T_{setup}\)，约 0.1–0.3 ns）：

| 路径组 | 可用窗口公式 | 数值 | QoR 实测 slack | 实际数据延迟 ≈ 窗口 − slack |
|-------|------------|------|--------------|--------------------------|
| INREG | \(10-5-0.1\) | 4.9 ns | 4.68 | ≈ 0.22 ns |
| clk | \(10-0.1\) | 9.9 ns | 0.00 | ≈ 9.9 ns（含 setup，关键路径报告 9.68 ns） |
| REGOUT | \(10-5\) | 5.0 ns | 3.27 | ≈ 1.73 ns（关键路径报告 1.63 ns） |

### 步骤 2：回应"−5 −5 −0.1"的陷阱

题目原本给的式子 \(10-5-5-0.1=-0.1\,\text{ns}\) 看起来是负数、不可能满足。请解释：

- 这个式子把**输入延迟**和**输出延迟**同时从同一个周期扣掉了，这只有在"输入端口组合直达输出端口"的 INOUT 路径上才成立；
- 而 clk 组（寄存器到寄存器）**既不沾 input_delay 也不沾 output_delay**，它的真实窗口是 \(10-0.1=9.9\,\text{ns}\)，不是 −0.1 ns；
- QoR 证明 INOUT 组为空，所以那个负数根本不会出现在任何真实路径上——这正是本项目能用 100 MHz 的根本原因。

### 步骤 3：解释三个 `group_path` 的用途

- **INREG**：单独盯住"外部到第一级寄存器"的入口路径，便于评估上游接口时序；
- **REGOUT**：单独盯住"末级寄存器到下游"的出口路径，便于评估下游接口时序；
- **INOUT**：兜底捕获任何组合透传路径，防止漏检；本设计为空，恰好证明全寄存化的流水线没有透传隐患。

### 步骤 4（可选，待本地验证）

如果你本地有 DC 环境，可以试着把 `set_clock_uncertainty` 从 0.1 改成 0.5，重跑 `compile_ultra`，观察 `synth_qor.rpt` 里 clk 组的 slack 是否从 0.00 跌成负值（预期会违例约 0.4 ns），从而体会 uncertainty 对裕量的直接挤压。**本环境无法运行综合，故标注待本地验证。**

## 6. 本讲小结

- `create_clock -period 10 -waveform {0 5}` 定义了 100 MHz、50% 占空比的心跳，是所有时序的参考基准。
- 三类路径的可用窗口各不相同：INREG ≈ 4.9 ns、clk ≈ 9.9 ns、REGOUT = 5.0 ns；**不能把 5 ns 输入延迟和 5 ns 输出延迟叠在同一条路径上扣**，那是 INOUT 透传路径才有的极端情形。
- `set_clock_uncertainty 0.1` 预留 skew+jitter 裕量；`set_clock_latency 0.5` 给 ideal 时钟一个插入延迟估计，对 reg-to-reg slack 净影响为 0。
- `set_false_path -hold` 暂关 IO 的保持检查（布线前算不准最小延迟），`set_fix_hold` 授权后端修 hold，`set_dont_touch_network` 把时钟树留给后端。
- 多工况（MMMC）：ss/WCCOM 查 setup、ff/BCCOM 查 hold；wireload 模型 G5K 在布线前估算连线寄生，布线后被真实 RC 取代。
- `group_path` 分出 INREG/REGOUT/INOUT 三组，INOUT 在本设计为空（全寄存化、无透传）；QoR 显示 clk 组 9.68 ns 关键路径、slack 0.00，恰好压线满足 100 MHz。

## 7. 下一步学习建议

本讲只解读了"约束写了什么"，还没有解读"综合出来的结果好不好"。下一讲 **u6-l3 综合报告解读：面积/时序/功耗** 将打开 `synth_qor.rpt`、`synth_area.rpt`、`synth_power.rpt`、`synth_timing.rpt`，逐项解读关键路径长度、单元/寄存器数量、总面积（约 202213 µm²）与功耗（约 9.95 mW），把本讲算出的 9.9 ns 预算和 0.00 slack 落到具体的标准单元上。

如果你对"这些约束怎么被 Innovus 复用"感兴趣，可以跳读 u6-l4，看后端如何 `read_sdc` 拿到本讲产生的 `FFT.sdc`，并在布线后用真实 RC 重算时序。
