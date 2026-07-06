# 综合报告解读：面积/时序/功耗

## 1. 本讲目标

上一讲（u6-l2）我们读懂了约束脚本 `cons.tcl`，给设计立下了「10 ns 时钟、5 ns 输入/输出延迟、0.1 ns 不确定性、ss/ff 多角」这把尺子。本讲要回答的是：**用这把尺子量完之后，设计到底考了多少分？**

学完本讲你应该能够：

- 在 `synth_qor.rpt` 里找到三个时序路径组（INREG / REGOUT / clk），读懂它们的 Critical Path Length、Slack、违规数，并能用 `cons.tcl` 的约束把每个 Slack 算回出来。
- 在 `synth_qor.rpt` / `synth_area.rpt` 的单元统计区读懂 Leaf Cell、Sequential Cell、Buf/Inv 这些计数，理解「2392 个触发器」「12793 个叶子单元」背后对应了哪些 RTL 结构。
- 在 `synth_area.rpt` 里区分组合面积与非组合面积，说出总面积 202213.12 µm² 是怎么加出来的。
- 在 `synth_power.rpt` 里区分内部功耗、开关功耗、漏电功耗，并**发现报告实测值与 README 规格表的差异**，学会「以报告为准、不盲信汇总表」。
- 独立完成一张「时序 / 面积 / 功耗」结论表，给出「设计是否恰好满足 10 ns」的判断。

> 本讲是「阅卷」讲：u6-l1 把卷子交上去（综合流程），u6-l2 是评分标准（约束），本讲是把成绩单（报告）一行行读给你看。

## 2. 前置知识

在读懂报告前，先用三段话把几个反复出现的术语讲清楚。

**Slack（时序裕量）。** 一条寄存器到寄存器的路径，数据从一个触发器出发，必须在下一个时钟沿到来之前、且留出建立时间（setup time），被下一个触发器稳定采样。于是有：

\[
\text{slack} = \text{required\_time} - \text{arrival\_time}
\]

- `slack ≥ 0`：路径**满足（MET）**时序，数据来得及；
- `slack < 0`：路径**违例（VIOLATED）**，数据来不及，芯片会跑飞。

Slack 越接近 0，说明这条路径越「踩线」，综合器已经把它压榨到极限。本设计 clk 组的 slack 恰好是 0.00，这就是「恰好满足」的字面含义。

**WNS 与 TNS。**

- WNS（Worst Negative Slack）：所有违例路径里最差的那条 slack。全为正时 WNS = 0。
- TNS（Total Negative Slack）：所有违例路径的 slack 之和。没有违例时 TNS = 0。

报告末尾一句 `Design WNS: 0.00 TNS: 0.00 Number of Violating Paths: 0`，意思就是「没有任何一条路径违例」。

**面积的三种口径与功耗的三种口径。**

| 维度 | 面积 | 功耗 |
|---|---|---|
| 组成 1 | 组合面积（Combinational）：与/或/非、加法器、乘法器等运算逻辑 | 内部功耗（Cell Internal）：单元内部电容充放电 |
| 组成 2 | 非组合面积（Noncombinational）：触发器、锁存器等时序单元 | 开关功耗（Net Switching）：连线电容充放电 |
| 组成 3 | Buf/Inv 面积：专门统计缓冲器/反相器 | 漏电功耗（Cell Leakage）：关断状态的静态漏电 |
| 合计 | Total Cell Area | Total Power = 动态（内部+开关）+ 漏电 |

> 一个关键直觉：综合阶段用的是**线负载模型（wireload）**估算连线，没有真实寄生参数，所以开关功耗被严重低估。这就是为什么综合后只有约 3 mW，而版图后（u6-l4，真实寄生）README 报 28 mW——这点会在第 4.4 节展开。

## 3. 本讲源码地图

本讲「源码」其实是四份综合后自动生成的**报告文件**，外加上一讲读过的两个脚本作为对照。

| 文件 | 作用 | 本讲用法 |
|---|---|---|
| `SYN/report/synth_qor.rpt` | Quality of Results，**一份报告看全貌**：三个路径组的时序、单元计数、面积、设计规则 | 读时序组、单元计数、面积汇总 |
| `SYN/report/synth_timing.rpt` | 最差 10 条路径的**详细路径报告**，逐级列出每一级逻辑的延迟增量 | 用 clk 组首条路径把 slack 算回出来 |
| `SYN/report/synth_area.rpt` | 专门的**面积报告**，列出库、端口/网络/单元数、各分项面积 | 核对总面积 202213.12 µm² |
| `SYN/report/synth_power.rpt` | 专门的**功耗报告**（`-analysis_effort low`），分内部/开关/漏电 | 读总功耗并发现与 README 的差异 |
| `SYN/scripts/syn.tcl` | 综合主脚本（u6-l1 已读） | 对照确认**哪些报告是脚本产出的、哪些不是** |
| `SYN/cons/cons.tcl` | 约束脚本（u6-l2 已读） | 把每个 slack 用约束的数值算回去 |

这些报告由 `syn.tcl` 的这几行命令产出：

```tcl
report_area      > ./report/synth_area.rpt
report_cell      > ./report/synth_cells.rpt
report_qor       > ./report/synth_qor.rpt
report_resources > ./report/synth_resources.rpt
report_timing -max_paths 10 > ./report/synth_timing.rpt
```

[SYN/scripts/syn.tcl:25-29](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/scripts/syn.tcl#L25-L29) —— 这五行决定性地把 RTL 综合结果落成五份报告。

> **一个值得留意的细节：** 这五行里**没有 `report_power`**。但 `synth_power.rpt` 仍然存在，且它的生成时间（11:41:59）比其余报告（11:36:53）晚了 5 分钟，文件头还写着 `-analysis_effort low`。这说明功耗报告不是由这份提交进来的 `syn.tcl` 产出的，而是事后在 `dc_shell` 里单独手动跑的（见 [SYN/report/synth_power.rpt:1-7](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_power.rpt#L1-L7)）。读报告时养成「这文件是谁、什么时候、怎么生成的」的习惯，比记住数字更重要。

## 4. 核心概念与源码讲解

### 4.1 QoR 时序路径组与关键路径

#### 4.1.1 概念说明

`report_qor` 把整个设计按**起点类型**分成若干**时序路径组（Timing Path Group）**，每组只报一条最差的路径。本项目在 `cons.tcl` 里用 `group_path` 显式建了 INREG（输入→寄存器）、REGOUT（寄存器→输出）两组，再加上默认的 clk（寄存器→寄存器）组，共三组（u6-l2 讲过还有个空的 INOUT 组不出现）。

分组的意义在于：不同组的「可用时间窗口」不同，所以要分开报、分开优化。输入路径要扣除外部输入延迟，输出路径要扣除外部输出延迟，而寄存器间路径享受完整的一个时钟周期。

#### 4.1.2 核心流程

读 QoR 时序段的流程是「**先看 Slack、再看违规数、最后用约束算回去**」：

1. 在三组里找 Slack 最小的——那组就是设计的瓶颈。
2. 检查每组的 `No. of Violating Paths` 和 `Total Negative Slack`——只要非零就是有路径跑不过。
3. 用 u6-l2 的约束数值，把每组的 Slack 反推出来，验证自己真的看懂了。

三组的可用时间窗口（来自 `cons.tcl`）：

| 路径组 | 起点 → 终点 | 可用时间窗口 |
|---|---|---|
| INREG | 输入端口 → 触发器 | \(T - t_{in} - t_{unc} - t_{setup}\) |
| REGOUT | 触发器 → 输出端口 | \(T - t_{out} - t_{unc}\) |
| clk | 触发器 → 触发器 | \(T - t_{unc} - t_{setup}\)（网络延迟两边相消） |

其中 \(T=10\,\text{ns}\)、\(t_{in}=t_{out}=5\,\text{ns}\)、\(t_{unc}=0.1\,\text{ns}\)、\(t_{setup}\approx 0.22\,\text{ns}\)（来自库，见 4.1.3）。

#### 4.1.3 源码精读

先看 `synth_qor.rpt` 里三组的汇总：

```text
Timing Path Group 'clk'
  Levels of Logic:               58.00
  Critical Path Length:           9.68
  Critical Path Slack:            0.00
  No. of Violating Paths:         0.00
```

[SYN/report/synth_qor.rpt:36-47](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_qor.rpt#L36-L47) —— clk 组：**关键路径 9.68 ns，slack 0.00，零违例**。

三组的完整对比：

| 路径组 | Levels of Logic | Critical Path Length | Slack | 违例数 |
|---|---|---|---|---|
| INREG  ([L10-L21](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_qor.rpt#L10-L21)) | 0 | 0.00 | **4.68** | 0 |
| REGOUT ([L23-L34](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_qor.rpt#L23-L34)) | 1 | 1.63 | **3.27** | 0 |
| clk    ([L36-L47](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_qor.rpt#L36-L47)) | 58 | 9.68 | **0.00** | 0 |

瓶颈一目了然：**clk 组**——58 级逻辑串在一起，吃掉了 9.68 ns，把整个时钟周期几乎用尽；INREG 和 REGOUT 因为只有 0~1 级逻辑，slack 都很宽松。

**关键一步：用约束把 clk 组的 slack = 0.00 算回去。** 打开 `synth_timing.rpt` 的首条路径，它的起点、终点、路径组正是 clk 组那条最差路径：

```text
Startpoint: clk_r_REG2299_S2  (rising edge-triggered flip-flop clocked by clk)
Endpoint:   clk_r_REG213_S14  (rising edge-triggered flip-flop clocked by clk)
Path Group: clk
```

[SYN/report/synth_timing.rpt:18-23](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_timing.rpt#L18-L23) —— 起点终点都是 clk 域触发器，确认这是 clk 组关键路径。

路径末端是整份报告最值得逐行读的地方：

```text
clock clk (rise edge)                   10.00      10.00   ; 捕获沿：一个周期后
clock network delay (ideal)              0.50      10.50   ; 理想时钟网络延迟
clock uncertainty                       -0.10      10.40   ; 扣掉 0.1 ns 不确定性
library setup time                      -0.22      10.18   ; 扣掉 0.22 ns 建立时间
data required time                                10.18
data arrival time                                -10.18
slack (MET)                                        0.00
```

[SYN/report/synth_timing.rpt:111-121](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_timing.rpt#L111-L121) —— **slack (MET) 0.00**，数据到达时间与要求时间精确相等。

把两边对齐成等式：

\[
\text{arrival} = \underbrace{0.50}_{\text{网络延迟}} + \underbrace{9.68}_{\text{关键路径}} = 10.18
\]

\[
\text{required} = \underbrace{10.00}_{\text{周期}} + \underbrace{0.50}_{\text{网络延迟}} - \underbrace{0.10}_{\text{uncertainty}} - \underbrace{0.22}_{\text{setup}} = 10.18
\]

\[
\text{slack} = 10.18 - 10.18 = 0.00 \quad(\text{MET})
\]

这就解释了 QoR 里的 **Critical Path Length = 9.68** 是怎么来的：它是**数据路径本身**的延迟（从起点触发器 CK 到终点触发器 D），**不含**时钟网络延迟；而 timing 报告里的到达时间 10.18 = 网络延迟 0.50 + 数据路径 9.68。两个数字对得上，说明你看懂了。

> **为什么是 58 级逻辑？** 沿着这条路径往上看，它依次穿过 `radix_no1 → radix_no2 → radix_no3 → radix_no4 → radix_no5` 五个蝶形的组合逻辑，最后经顶层 `U4123/U4170` 落入捕获触发器（见 [synth_timing.rpt:45-108](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_timing.rpt#L45-L108)）。这正是 SDC 蝶形单元是纯组合逻辑（u3-l2 讲过 radix2 自身无寄存器）的体现：前级的 `op`（和）输出直连下一级 `din_b`，于是在一个周期内堆叠出一条穿越多级蝶形的深组合链。58 级、9.68 ns，几乎踩线——这也是 u6-l1 为什么非要跑 `compile → optimize_registers → compile_ultra` 三轮优化的原因。

**INREG / REGOUT 的 slack 也能算回去**（用 4.1.2 的窗口公式）：

\[
\text{INREG slack} = 10 - 5\,(t_{in}) - 0.1\,(t_{unc}) - 0.22\,(t_{setup}) - 0\,(\text{path}) = 4.68 \;\checkmark
\]

\[
\text{REGOUT slack} = \bigl(10 - 5\,(t_{out}) - 0.1\,(t_{unc})\bigr) - 1.63\,(\text{path}) = 4.9 - 1.63 = 3.27 \;\checkmark
\]

两个都和报告完全吻合——这证明三组的 slack 全部能用 u6-l2 的约束数值复算出来。

报告末尾给出全局结论：

```text
Design  WNS: 0.00  TNS: 0.00  Number of Violating Paths: 0
Design (Hold)  WNS: 0.00  TNS: 0.00  Number of Violating Paths: 0
```

[SYN/report/synth_qor.rpt:99-104](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_qor.rpt#L99-L104) —— **建立时间（setup）和保持时间（hold）都零违例**，设计在 ss 慢角下完整满足 100 MHz。

#### 4.1.4 代码实践

**实践目标：** 把 clk 组的 slack = 0.00 用自己的计算复现一遍，确认「设计恰好满足 10 ns」这个结论是你自己算出来的、不是抄报告的。

**操作步骤：**

1. 打开 [SYN/report/synth_qor.rpt:36-47](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_qor.rpt#L36-L47)，记下 Critical Path Length = 9.68、Slack = 0.00。
2. 打开 [SYN/report/synth_timing.rpt:111-121](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_timing.rpt#L111-L121)，抄下四个数：周期 10.00、网络延迟 0.50、uncertainty 0.10、setup 0.22。
3. 用上面两个等式手算 arrival 和 required。

**需要观察的现象：** 两个等式的结果都应正好等于 10.18，相减得 0.00。

**预期结果：** arrival = required = 10.18 ns，slack = 0.00 (MET)。结论：**clk 组关键路径恰好踩线满足 10 ns 时钟，没有任何时序裕量**。

> 待本地验证：若你在自己的 DC 环境重跑综合（库版本、dc_shell 版本不同），9.68 和 0.22 这两个数会有微小浮动，slack 可能为 +0.0x 或 −0.0x；但只要 WNS/TNS 仍为 0，结论「满足 100 MHz」不变。

#### 4.1.5 小练习与答案

**练习 1：** 如果把时钟周期从 10 ns 提到 9 ns（约 111 MHz），clk 组会怎样？

**答：** required 变成 \(9 + 0.5 - 0.1 - 0.22 = 9.18\) ns，而 arrival 仍是 10.18 ns，slack = 9.18 − 10.18 = **−1.00 ns**，出现 1 ns 违例，设计跑不到 111 MHz。这也说明 100 MHz 是这个网表的实际频率天花板。

**练习 2：** INREG 组的 Critical Path Length 为什么是 0.00、Levels of Logic 是 0？

**答：** 因为输入端口进来后几乎直接进触发器，中间没有组合逻辑（0 级），数据路径延迟≈0，所以全部可用窗口 4.68 ns 都剩成了 slack。这正是 `cons.tcl` 给输入留 5 ns 借入预算、而实际几乎没用上的体现。

### 4.2 单元与寄存器统计

#### 4.2.1 概念说明

时序看「快不快」，单元统计看「大不大、复杂不复杂」。综合器把 RTL 映射成标准单元库里的具体门，`report_qor` 的 Cell Count 段和 `report_area` 的开头都会给出一份「清单」：用了多少触发器、多少组合门、多少缓冲器。这些数字是把 RTL 结构和门级实现对应起来的桥梁——比如看到 2392 个触发器，就该想到 RTL 里那两个 32×16 的 `result_r/result_i` 排序数组（u4-l2）和各级移位寄存器（u3-l3）。

#### 4.2.2 核心流程

读单元统计的顺序：

1. 先看 **Leaf Cell Count（叶子单元总数）**——这是设计里所有「真实门」的总数，层级单元（hierarchical）不算。
2. 拆成 **Combinational（组合）** vs **Sequential（时序/触发器）** 两类。
3. 单独看 **Buf/Inv（缓冲器/反相器）**——它们是综合器为了修驱动能力、修转换时间硬塞进去的，占多少能反映设计的「健康度」。
4. 看 **Hierarchical Cell Count**——经过 `compile_ultra` + `optimize_registers` 后还剩几个子模块没被展平。

#### 4.2.3 源码精读

`synth_qor.rpt` 的 Cell Count 段：

```text
Hierarchical Cell Count:          6
Hierarchical Port Count:       1330
Leaf Cell Count:              12793
Buf/Inv Cell Count:            1522     ; 缓冲器 76 + 反相器 1446
CT Buf/Inv Cell Count:            0     ; 时钟树缓冲器为 0（综合用理想时钟）
Combinational Cell Count:     10401
Sequential Cell Count:         2392
Macro Count:                      0
```

[SYN/report/synth_qor.rpt:50-62](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_qor.rpt#L50-L62) —— 单元统计的核心几行。

逐行解读：

- **Leaf Cell Count = 12793**：整个设计映射成 12793 个标准单元（叶子节点）。`synth_area.rpt` 给的 `Number of cells: 12799` 略大，是因为它把层级单元也数了进去（见 [synth_area.rpt:13-20](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_area.rpt#L13-L20)）。
- **Combinational 10401 / Sequential 2392**：组合门是主力（约 81%），触发器 2392 个。2392 这个数对应 RTL 里的存储结构：两个 32×16 的 result 排序数组（实部+虚部共 1024 bit）、5 级流水线的输入/中间寄存器、以及把 shift_N 移位寄存器（u3-l3 讲过 16×24=384 bit 的超宽寄存器）综合成触发器链后的总和。
- **Buf/Inv = 1522**：其中反相器 1446、缓冲器仅 76。`CT Buf/Inv = 0` 说明**综合阶段没有插任何时钟树缓冲器**——因为 `cons.tcl` 用的是理想时钟（`set_clock_latency 0.5`，u6-l2），时钟树要等后端 Innovus 的 CTS 阶段才长出来（u6-l4）。
- **Hierarchical Cell Count = 6**：经过 `uniquify` + 三轮编译后，原始的 radix2/shift_N/ROM_N 层级大部分被展平或合并。timing/power 报告里能看到的存活子设计是 5 个 uniquify 出来的蝶形 `radix2_4 / radix2_3 / radix2_2 / radix2_1 / radix2_0`（见 [synth_timing.rpt:28-32](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_timing.rpt#L28-L32)），加上顶层 FFT，正好对应这 6 个层级单元；shift_N 与 ROM_N 模块的逻辑则在优化中被吸收进父层级（其 wireload 组在 power 报告里仅残留 `shift_16` 一个，见 [synth_power.rpt:18-26](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_power.rpt#L18-L26)）。
- **Macro Count = 0**：没有用任何 SRAM/宏单元，全部用标准单元搭——旋转因子 ROM 也是用标准单元实现的查找表（u3-l4）。

设计规则段同样干净：

```text
Total Number of Nets:         14116
Nets With Violations:             0
Max Trans Violations:             0      ; 转换时间无违例
Max Cap Violations:               0      ; 负载电容无违例
```

[SYN/report/synth_qor.rpt:79-85](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_qor.rpt#L79-L85) —— 14116 根网络里没有任何驱动能力或电容违例，说明综合器插入的缓冲器已经把所有网络修合规。

#### 4.2.4 代码实践

**实践目标：** 估算「2392 个触发器」里有多少能对应到 RTL 的排序数组。

**操作步骤：**

1. 回忆 u4-l2：输出排序模块用了 `result_r[0:31]` 和 `result_i[0:31]` 两个二维数组，每个元素 16 位，共 \(32 \times 16 \times 2 = 1024\) 个触发器。
2. 在 `synth_qor.rpt` 的 Sequential Cell Count = 2392 中，先扣除这 1024 个。
3. 思考剩下的 ~1368 个触发器分配在哪里（提示：u3-l3 的 shift_N 移位寄存器、u4-l3 的 count_y/y_1/over 等控制寄存器、各级输入寄存）。

**需要观察的现象：** 排序数组约占触发器总数的 43%（1024/2392），是单一最大的触发器消耗源。

**预期结果：** 得出「排序数组是触发器大户」的结论，并理解为什么 u2-l3/README 强调 bit reverser 能「节省 50% 存储」——这个数组本身就是面积/功耗的大头。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `CT Buf/Inv Cell Count = 0`，但综合后真的没有时钟缓冲器吗？

**答：** 综合阶段确实为 0，因为约束用的是理想时钟（时钟树还没建）。真实的时钟树缓冲器要在 Innovus 的 CTS（Clock Tree Synthesis）阶段才插入，所以这个 0 是综合阶段的预期值，不代表最终芯片没有时钟缓冲器。

**练习 2：** `Number of references: 80`（[synth_area.rpt:20](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_area.rpt#L20)）是什么意思？

**答：** 「reference」指标准单元库里被实际用到的**不同种类**的单元。整个设计虽然实例化了 12793 次，但只用到 80 种不同的门（如某种 D 触发器、某种与非门等）。种类越少，库映射越规整。

### 4.3 面积报告解读

#### 4.3.1 概念说明

面积报告回答「这颗芯片要占多少硅片面积」。本项目总面积 202213.12 µm²（约 0.20 mm²），由两部分组成：

- **组合面积**：所有组合逻辑（加法器、乘法器、与或非、ROM 查找表）占的面积。
- **非组合面积**：所有时序单元（触发器、锁存器）占的面积。

注意面积的单位是 µm²（平方微米），而 README 里版图后写的「1.27 mm」其实是 1.27 mm²（约 1270000 µm²），远大于综合的 202213 µm²——因为版图还要加上电源网络、时钟树、布线占用、IO 余量等（u6-l4 会讲）。

#### 4.3.2 核心流程

读面积报告的顺序：

1. 看 **Library Used**——面积是相对哪个工艺库算的（必须和综合角一致）。
2. 看 **Total cell area**——总面积数字。
3. 拆开看 **Combinational vs Noncombinational**，判断这个设计是「算得多」（组合大）还是「存得多」（非组合大）。
4. 看 **Buf/Inv area** 占比，判断缓冲器开销。

#### 4.3.3 源码精读

`synth_area.rpt` 全文很精炼，关键几行：

```text
Library(s) Used:
    fsc0h_d_generic_core_ss1p08v125c        ; ss 慢角、1.08V、125℃

Number of ports:                         1390
Number of nets:                         15159
Number of cells:                        12799
Number of combinational cells:          10401
Number of sequential cells:              2392
Number of buf/inv:                       1522
Number of references:                      80

Combinational area:             115985.919649
Buf/Inv area:                     6324.479866
Noncombinational area:           86227.200359
Net Interconnect area:      undefined  (Wire load has zero net area)

Total cell area:                202213.120008
```

[SYN/report/synth_area.rpt:9-28](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_area.rpt#L9-L28) —— 面积报告的核心数据。

逐项解读：

- **库 = `ss1p08v125c`**：ss（slow-slow）慢角、1.08 V、125℃。面积用最差角算，保证量出来的面积是保守上界（u6-l1 讲过 target_library 取 ss）。
- **Total cell area = 202213.12 µm²**：这就是 README 规格表里那个数（[README.md:54-59](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/README.md#L54-L59)）。本讲涉及的四个数字里，**只有面积是报告与 README 完全一致的**。
- **组合 115986 µm² / 非组合 86227 µm²**：组合占 57%、非组合（触发器）占 43%。注意这个比例和单元数（组合 10401 个 vs 时序 2392 个）不同——因为单个触发器比单个逻辑门大得多，所以 2392 个触发器虽然只占单元数的 19%，却吃掉了 43% 的面积。这也再次印证「排序数组 + 移位寄存器」是面积大头。
- **Buf/Inv area = 6324 µm²（约 3.1%）**：1522 个缓冲/反相器只占 3% 面积，开销很小，说明综合器没有为了修时序而疯狂插缓冲器（如果这个比例超过 10%，通常说明设计或约束有问题）。
- **Net Interconnect area = undefined**：综合阶段用 wireload 模型，**连线面积被估为 0**。真实连线面积要等版图后的 RC 提取才有（u6-l4）。所以 202213 µm² 是**纯单元面积**，不含连线。

`synth_qor.rpt` 的 Area 段给出完全一致的分项，可交叉核对：

```text
Combinational Area:   115985.919649
Noncombinational Area: 86227.200359
Buf/Inv Area:           6324.479866
Cell Area:            202213.120008
```

[SYN/report/synth_qor.rpt:65-76](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_qor.rpt#L65-L76) —— QoR 的面积段与 area 报告数字逐位相同，两份报告互为校验。

#### 4.3.4 代码实践

**实践目标：** 验证总面积 = 组合 + 非组合，并算出触发器的「单价面积」。

**操作步骤：**

1. 从 [synth_area.rpt:22-28](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_area.rpt#L22-L28) 取三个数：组合 115985.92、非组合 86227.20、总和 202213.12。
2. 手算 \(115985.92 + 86227.20\)，看是否等于 202213.12。
3. 用非组合面积 86227.20 ÷ 时序单元数 2392，估算单个触发器平均面积。

**需要观察的现象：** 两数相加正好等于总面积；单个触发器约 36 µm²。

**预期结果：** \(115985.92 + 86227.20 = 202213.12\,\mu m^2\) ✓；\(86227.20 / 2392 \approx 36.0\,\mu m^2\)/触发器。

#### 4.3.5 小练习与答案

**练习 1：** 为什么综合面积 202213 µm² 远小于 README 版图面积 1.27 mm²？

**答：** 综合只算标准单元面积、且连线面积为 0；版图还要加上：电源环/条带（u6-l4 的 addRing/addStripe）、时钟树缓冲器（CTS）、真实布线占用、IO 与 keep-out 余量、以及利用率（floorplan 通常 0.7）带来的空白。所以版图面积约为单元面积的 5~6 倍是正常的。

**练习 2：** 如果把输入位宽从 12 位提到 16 位，面积报告里哪一项涨得最猛？

**答：** 数据通路变宽 → 组合面积（加法器、乘法器、ROM 查找表）和非组合面积（移位寄存器、排序数组）都会涨，但因为排序数组是 32×位宽，位宽增加会显著推高非组合面积；同时 SDC PE 的乘法器位宽增加主要推高组合面积。两者都会涨，比例取决于具体结构。

### 4.4 功耗报告解读

#### 4.4.1 概念说明

功耗报告回答「这颗芯片跑起来耗多少电」。本节有一个**重要发现**：报告实测总功耗是 2.9519 mW，而 README 规格表写的是 9.9519 mW——两者小数部分「.9519」完全相同，但整数位不同。本节会教你**以报告为准**地读出真实数字，并解释差异的可能来源。

功耗三件套：

- **内部功耗（Cell Internal）**：单元内部电容充放电，与单元种类、翻转率有关。
- **开关功耗（Net Switching）**：连线电容充放电，与连线长度、翻转率有关。
- **漏电功耗（Cell Leakage）**：晶体管关断时的静态漏电流，与温度、电压、工艺有关，几乎不随工作状态变。

动态功耗 = 内部 + 开关；总功耗 = 动态 + 漏电。

#### 4.4.2 核心流程

读功耗报告的顺序：

1. 看 **Operating Conditions 与 Voltage**——确认是哪个工艺角、什么电压下估的。
2. 看 **Total Dynamic / Leakage / Total** 三个汇总数。
3. 看 **Power Group 表**——按 register / combinational 分组，找出谁是耗电大户。
4. 看 **analysis_effort** 与 Net Switching 占比——判断这个功耗数被低估了多少。

#### 4.4.3 源码精读

先看报告头，确认工况与精度：

```text
Report : power
        -analysis_effort low            ; 低精度快速估算
Operating Conditions: WCCOM   Library: ...ss1p08v125c
Global Operating Voltage = 1.08
Dynamic Power Units = 1mW
Leakage Power Units = 1pW
```

[SYN/report/synth_power.rpt:1-15](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_power.rpt#L1-L15) —— 注意 `-analysis_effort low` 和 1.08 V（ss 角），这两个标签决定了数值的可信度。

> **关键提醒：** 头部明确写着 `-analysis_effort low`，意思是综合器用**默认翻转率假设**做了一次快速估算，没有真实的开关活动文件（SAIF/VCD）。这种模式下功耗数只是粗略量级，不能当签核值用。

汇总数字：

```text
Cell Internal Power  =   2.6986 mW   (96%)
Net Switching Power  = 126.3021 uW    (4%)     ; = 0.1263 mW
Total Dynamic Power  =   2.8249 mW  (100%)

Cell Leakage Power   = 127.0537 uW            ; = 0.1271 mW
```

[SYN/report/synth_power.rpt:38-43](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_power.rpt#L38-L43) —— 动态 2.8249 mW + 漏电 0.1271 mW。

底部的 Power Group 表给出分组与总计：

```text
Power Group      Internal    Switching   Leakage     Total      (%)
register         2.6494      1.6785e-02  4.4112e+07  2.7102   (91.81%)
combinational    4.9205e-02  0.1095      8.2942e+07  0.2417   ( 8.19%)
Total            2.6986 mW   0.1263 mW   1.2705e+08 pW   2.9519 mW
```

[SYN/report/synth_power.rpt:46-57](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_power.rpt#L46-L57) —— **register 组占 91.81%，是绝对耗电大户**。

三个关键读数：

- **总功耗 = 2.9519 mW**：\(2.8249\,(\text{动态}) + 0.1271\,(\text{漏电}) = 2.9520 \approx 2.9519\) mW。
- **内部功耗占 96%**：因为综合阶段没有真实连线寄生，开关功耗只有 4%——这严重低估了真实开关功耗。版图后真实寄生进来，开关功耗会显著上升，这是综合 2.95 mW → 版图 28 mW 的主因之一。
- **register 组占 91.81%**：2392 个触发器（排序数组 + 移位寄存器）每拍都在翻转，吃掉了几乎全部功耗；组合逻辑只占 8%。这与 4.2/4.3 节「触发器是面积大头」的结论互相印证——面积大户同时也就是功耗大户。

**现在直面那个差异。** 把报告数和 README 规格表并排放：

| 来源 | 总功耗 | 备注 |
|---|---|---|
| `synth_power.rpt` 实测 | **2.9519 mW** | 动态 2.8249 + 漏电 0.1271，`-analysis_effort low`，1.08 V，ss 角 |
| README 规格表 ([README.md:54-59](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/README.md#L54-L59)) | **9.9519 mW** | 标注「Power 9.9519 mW」 |

两者小数部分「9519」完全相同，整数位一个是 2、一个是 9。本讲**以报告实测的 2.9519 mW 为准**，因为它是可追溯的实际产物（有工况、有精度标签、有分组明细）。README 的 9.9519 mW 可能来自更高精度的分析、不同工艺角/电压（README 提到 1.2 V 版图），或是一次转录出入——**这需要本地用真实 SAIF 重新跑 `report_power -analysis_effort high` 才能定论**。

> 这正是「读报告」相对「读汇总表」的价值：报告给你可复算的明细，而汇总表只给一个孤零零的数。养成对照习惯，能避免被错误数字误导。

#### 4.4.4 代码实践

**实践目标：** 从 `synth_power.rpt` 摘出总功耗并分解，验证「动态 + 漏电 = 总功耗」。

**操作步骤：**

1. 从 [synth_power.rpt:38-43](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_power.rpt#L38-L43) 读 Total Dynamic Power = 2.8249 mW、Cell Leakage Power = 127.0537 uW。
2. 把漏电换算到 mW：\(127.0537\,\mu W = 0.1271\,mW\)。
3. 相加：\(2.8249 + 0.1271 = 2.9520\) mW，与底部 Total 行 2.9519 mW 对比（差 0.0001 为四舍五入）。
4. 算 register 组占比：\(2.7102 / 2.9519 = 91.8\%\)。

**需要观察的现象：** 动态 + 漏电 ≈ 总功耗；register 组独占近 92%。

**预期结果：** 总功耗 2.9519 mW（报告实测），register 组占 91.81%。结论：**本设计的功耗几乎全是触发器翻转贡献的，优化功耗的关键在减少排序数组/移位寄存器的翻转活动**。

#### 4.4.5 小练习与答案

**练习 1：** 为什么开关功耗（Net Switching）只占 4%？这可信吗？

**答：** 因为综合阶段用 wireload 模型，没有真实连线寄生，连线电容被估得很小，所以开关功耗被严重低估。版图后用真实 RC 提取，开关功耗会上升好几倍。所以这个 4% 是综合阶段的「假象」，不是最终芯片的真实比例。

**练习 2：** 如果想让这颗 FFT 更省电，从报告看应该优先改哪里？

**答：** register 组占 91.81%，所以优先减少触发器翻转：比如给排序数组加时钟门控（不排序时不翻转）、用 SRAM 替换大移位寄存器、或降低不活跃级的翻转率。改组合逻辑（只占 8%）收益很小。

## 5. 综合实践

把本讲四个模块串起来，独立产出一张「综合结论表」。这是你向别人汇报「这颗 FFT 综合得怎么样」时最该拿出的一张表。

**任务：** 填完下面这张表，每格都要标注数字来自哪份报告、哪几行。

| 指标 | 数值 | 来源（报告 + 行号） | 是否达标 |
|---|---|---|---|
| 关键路径长度（clk 组） | 9.68 ns | `synth_qor.rpt` L39 / `synth_timing.rpt` L36-L109 | — |
| clk 组 slack | 0.00 ns | `synth_qor.rpt` L40 / `synth_timing.rpt` L121 | **恰好达标（MET）** |
| 全局 setup 违例数 | 0 | `synth_qor.rpt` L101 | 达标 |
| 全局 hold 违例数 | 0 | `synth_qor.rpt` L104 | 达标 |
| 总面积 | 202213.12 µm² | `synth_area.rpt` L28 / `synth_qor.rpt` L75 | — |
| 总功耗（报告实测） | 2.9519 mW | `synth_power.rpt` L57 | — |
| 触发器数量 | 2392 | `synth_qor.rpt` L60 | — |
| 单元总数 | 12793 | `synth_qor.rpt` L54 | — |

**操作步骤：**

1. 逐一打开四份报告，**自己**找到上表每个数字（不要只看本讲已填的答案），在报告里画线确认。
2. 用 4.1.2 的等式把 clk 组的 slack = 0.00 重算一遍，确认 `arrival = required = 10.18`。
3. 用一句话写出整体结论。

**预期结论（示例）：** 「该 32 点 FFT 处理器在 UMC 130nm、ss/1.08V 角下综合后，clk 组关键路径 9.68 ns、slack 0.00，**恰好压线满足 10 ns（100 MHz）时钟**，setup/hold 零违例；总面积 202213 µm²（其中触发器占 43%），功耗报告实测 2.9519 mW（register 组占 92%，因 `-analysis_effort low` 且无真实寄生而偏保守）。设计与 README 规格表在面积上完全一致，在功耗上存在 2.9519 mW vs 9.9519 mW 的差异，需用高精度分析重新核定。」

## 6. 本讲小结

- 时序瓶颈是 **clk 组**：关键路径 9.68 ns、58 级逻辑、slack 恰好 **0.00**，可用 `arrival = 0.50 + 9.68 = required = 10.00 + 0.50 − 0.10 − 0.22` 精确复算，设计**压线满足 100 MHz**。
- INREG（slack 4.68）与 REGOUT（slack 3.27）两组的 slack 都能用 u6-l2 的约束数值（5 ns IO 延迟、0.1 ns uncertainty、0.22 ns setup）反推出来，setup/hold **零违例**。
- 单元统计：12793 个叶子单元、2392 个触发器、1522 个缓冲/反相器（综合期时钟树缓冲器为 0）；触发器对应排序数组与移位寄存器，是面积与功耗的双重大户。
- 面积 **202213.12 µm²**（组合 57% / 非组合 43%），是四项指标里**唯一与 README 规格表完全一致**的；连线面积为 0，真实面积要到版图后才完整。
- 功耗报告**实测总功耗 2.9519 mW**（动态 2.8249 + 漏电 0.1271），register 组独占 91.81%；因 `-analysis_effort low` 与无真实寄生，偏保守。
- 读报告的核心习惯：**先看工况/精度标签，再看明细，最后才信汇总数**——据此发现 README 的 9.9519 mW 与报告 2.9519 mW 存在差异，需重新核定。

## 7. 下一步学习建议

本讲读完的是**综合后**的网表成绩单。接下来：

- **u6-l4 布局布线：Innovus 物理实现流程**——把这份网表变成真实版图。重点对照本讲两个「待补」点：版图后真实 RC 提取会让开关功耗从 4% 跳升（解释 2.95 mW → 28 mW），CTS 会插入本讲为 0 的时钟树缓冲器，floorplan 利用率 0.7 会把 202213 µm² 单元面积撑成 1.27 mm² 版图面积。
- **u7-l2 架构取舍与设计权衡**——把本讲的 202213 µm²、2.95 mW、9.68 ns、2392 个触发器和 README 宣称的「100% 利用率、加法器减半、存储减半」放在一起，做一次完整的架构评估。
- 若想亲手验证功耗差异，可在 dc_shell 里对 `SYN/output/FFT.v` 跑 `report_power -analysis_effort high`，并喂入仿真产生的 SAIF 文件，得到带真实翻转率的签核级功耗。
