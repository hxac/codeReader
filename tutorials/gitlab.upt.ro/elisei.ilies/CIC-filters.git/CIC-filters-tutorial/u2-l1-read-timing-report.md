# 读懂时序报告（timing）

## 1. 本讲目标

学完本讲后，你应当能够：

- 在一份动辄上千行的 Vivado 时序报告里，**一眼定位**到 `Design Timing Summary`，并据此判断设计是否满足时序约束。
- 准确解释 `WNS / TNS` 与 `WHS / THS` 这一串缩写代表的含义，并理解它们的正负与时序收敛（timing closure）的关系。
- 展开一条路径明细（Path Details），读出 `Requirement`、`Data Path Delay`、`Logic Levels`、`Source Clock Delay` 等关键字段。
- 理解 Vivado 默认开启的 **Slow / Fast 多角（multi-corner）分析**：为什么建立时间在 Slow 角检查、保持时间在 Fast 角检查。

本讲的全部结论都基于仓库内真实存在的两份报告：`timing_impl_R16_N4.txt`（实现阶段）与 `timing_synth_R16_N4.txt`（综合阶段），均为 CIC Compiler 方案、R=16、N=4、时钟 100 MHz。

---

## 2. 前置知识

在进入本讲前，你需要已经掌握（来自 u1-l4）：

- **综合（synthesis）与实现（implementation）的区别**：综合把 HDL 翻译成门级网表但未布线；实现在网表上完成布局布线。本讲会看到两份报告的最大差异正是“有没有真实布线”。
- **建立时间（Setup）与时序约束的直觉**：数据必须在一个时钟周期内稳定到达下一级寄存器，`Slack` 就是衡量“有没有按时到达”的裕量。
- **目标器件与命名规则**：`timing_impl_R16_N4.txt` 文件名中 `impl` 表示实现阶段、`R16` 表示抽取率 R=16、`N4` 表示级数 N=4。

本讲会用到一个核心公式：时钟周期与频率互为倒数。100 MHz 对应

\[ T = \frac{1}{f} = \frac{1}{100\,\text{MHz}} = 10\,\text{ns} \]

这正是报告里 `Requirement = 10.000ns` 的由来。

---

## 3. 本讲源码地图

本讲的“源码”是两份 Vivado 时序报告文本：

| 文件 | 阶段 | 作用 |
| --- | --- | --- |
| `vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt` | 实现（impl） | **主分析对象**。含真实布局布线延迟，是论文“后实现评估”的依据，本讲绝大多数引用都取自这里。 |
| `vivado_reports/reports_at_100Mhz/CIC Compiler/timing_synth_R16_N4.txt` | 综合（synth） | **对照对象**。网表阶段的估计延迟，用来对比“布线前后”的差别。 |

两份报告都由 Vivado 命令 `report_timing_summary` 生成，文件头部就写明了这一点。

---

## 4. 核心概念与源码讲解

### 4.1 Design Timing Summary：一眼看懂时序结论

#### 4.1.1 概念说明

一份完整的时序报告可能有几千行，但判断“设计到底过没过时序”，只需要看其中一张小表——`Design Timing Summary`。它是整份报告的“体检结论页”。

这一页浓缩了三类检查的结果：

- **建立时间检查（Setup）**：数据能不能在时钟沿之前稳定到达。这是最常被关心的。
- **保持时间检查（Hold）**：数据在时钟沿之后能不能稳定保持足够久。
- **脉冲宽度检查（Pulse Width, PW）**：时钟高/低电平的宽度是否满足寄存器要求。

#### 4.1.2 核心流程

阅读 `Design Timing Summary` 的标准动作：

1. 先看表头下方那一行 **`All user specified timing constraints are met.`** ——出现这句话即代表全部约束满足。
2. 再看 `WNS` 判断建立时间裕量，看 `WHS` 判断保持时间裕量。
3. 看 `TNS Failing Endpoints / THS Failing Endpoints` 是否为 0——只要为 0，就没有任何路径违例。

只要第 1 步那句话存在，且 `WNS / WHS` 都为正，时序即收敛（met）。

#### 4.1.3 源码精读

打开实现阶段的报告，定位到 `Design Timing Summary`：

[vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:L163-L173](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L163-L173)

这段的核心内容是：

```
    WNS(ns)  TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints  WHS(ns)  THS(ns)  THS Failing Endpoints  THS Total Endpoints  WPWS(ns) ...
     6.274    0.000                    0                  386    0.044    0.000                     0                  386    4.020  ...

All user specified timing constraints are met.
```

- `WNS = 6.274`：建立时间最差裕量为正，说明数据比要求**早到** 6.274 ns，满足约束。
- `WHS = 0.044`：保持时间最差裕量为正，说明数据在时钟沿后多停留了 0.044 ns，满足约束。
- `TNS Failing Endpoints = 0` 与 `THS Failing Endpoints = 0`：没有任何一条路径违例。
- 末行结论 `All user specified timing constraints are met.`：**全部用户指定的时序约束均满足**，这就是本配置（CIC Compiler、R16、N4、100 MHz）时序收敛的最终判据。

> 小贴士：`WNS` 为正表示“裕量充足”。若 `WNS` 为负，则代表存在建立时间违例，设计在该频率下**不可靠**——这正是后续 u3-l2 估算最大频率 `fmax` 的出发点。

#### 4.1.4 代码实践

1. **实践目标**：在长报告里快速定位结论表。
2. **操作步骤**：打开 `timing_impl_R16_N4.txt`，搜索（Ctrl+F）关键字 `Design Timing Summary`，定位到第二个匹配处（表头下方的数据行）。
3. **需要观察的现象**：表下方应紧跟着 `All user specified timing constraints are met.`。
4. **预期结果**：`WNS = 6.274`、`WHS = 0.044`、`TNS/THS Failing Endpoints` 均为 `0`。
5. 此结论可直接读出，无需运行命令。

#### 4.1.5 小练习与答案

**练习 1**：如果 `WNS = 6.274` 而 `TNS Failing Endpoints = 0`，能否断定设计满足建立时间约束？

**参考答案**：能。`WNS` 为正意味着最差路径都有正裕量，`TNS Failing Endpoints = 0` 进一步说明没有违例路径，二者一致表明建立时间约束全部满足。

**练习 2**：`WNS` 为负、但绝对值很小（例如 −0.01 ns），算不算“过时序”？

**参考答案**：不算。只要 `WNS < 0`，就说明至少有一条路径违例，理论上时序不可靠；绝对值小只代表“差一点点”，常见做法是降低时钟频率或优化该路径，而不是当作满足。

---

### 4.2 WNS/TNS 与 WHS/THS：建立与保持的数字含义

#### 4.2.1 概念说明

`Summary` 表里有四组缩写成对出现，理解它们的关键是分清 **“最差（Worst）”** 与 **“总计（Total）”**、**“建立（Setup）”** 与 **“保持（Hold）”**：

| 缩写 | 全称 | 含义 |
| --- | --- | --- |
| **WNS** | Worst Negative Slack（Setup） | 所有建立时间检查中**最差的一条**裕量 |
| **TNS** | Total Negative Slack（Setup） | 所有违例路径裕量绝对值之和（无违例则为 0） |
| **WHS** | Worst Hold Slack | 所有保持时间检查中**最差的一条**裕量 |
| **THS** | Total Hold Slack | 所有保持违例路径裕量绝对值之和 |

`WNS / WHS` 告诉你“最坏情况坏到什么程度”，`TNS / THS` 配合 `Failing Endpoints` 告诉你“违例范围有多大”。论文做横向对比时，最常引用的就是 `WNS`（见 u3-l2）。

#### 4.2.2 核心流程

建立时间与保持时间的松弛量（Slack）计算方向不同：

- 建立时间裕量 = 需求时间 − 到达时间：

\[ S_{\text{setup}} = T_{\text{required}} - T_{\text{arrival}} \]

  数据到达得**越早越好**，所以 `required − arrival` 为正代表“早到”，满足。

- 保持时间裕量 = 到达时间 − 需求时间：

\[ S_{\text{hold}} = T_{\text{arrival}} - T_{\text{required}} \]

  数据需要停留得**足够久**，所以 `arrival − required` 为正代表“停留够久”，满足。

两者**都必须为正**，时序才算真正收敛。报告路径明细里两行写法不同，正是这个原因（见 4.3）。

#### 4.2.3 源码精读

回到实现报告的同一张表，逐列对应：

[vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:L168-L170](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L168-L170)

读法：

```
WNS=6.274  TNS=0.000  TNS Failing=0  TNS Total=386  |  WHS=0.044  THS=0.000  THS Failing=0  THS Total=386  |  WPWS=4.020 ...
```

- `TNS Total Endpoints = 386`：本设计共有 386 个建立时间检查端点；其中 `Failing Endpoints = 0`，全部通过。
- `WHS = 0.044` 比 `WNS = 6.274` 小得多——这是常态：保持时间裕量通常很紧，但只要为正即可。
- `WPWS = 4.020`：脉冲宽度裕量也很充足。

如果你想按时钟域细分，可以再看 `Intra Clock Table`，它把同一套指标按时钟（这里是 `aclk`）拆开：

[vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:L186-L193](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L186-L193)

这里只有一个时钟 `aclk`，所以数值与总表完全一致；多时钟设计里这张表才是按域排查违例的入口。

#### 4.2.4 代码实践

1. **实践目标**：建立“WNS 看最差、TNS Failing 看规模”的判读习惯。
2. **操作步骤**：在 `timing_impl_R16_N4.txt` 中同时记录 `WNS`、`TNS Failing Endpoints`、`WHS`、`THS Failing Endpoints` 四个值。
3. **需要观察的现象**：建立与保持两组的 `Failing Endpoints` 是否都为 0。
4. **预期结果**：四值分别为 `6.274 / 0 / 0.044 / 0`。
5. 此为阅读型实践，结论可直接从报告读取。

#### 4.2.5 小练习与答案

**练习 1**：`WNS = 6.274`、`TNS = 0.000`，二者为什么不冲突？

**参考答案**：`WNS` 是“最差一条路径”的裕量，只要没有违例它就是正数；`TNS` 只累加**违例路径**的负裕量，无违例时恒为 0。所以 `WNS>0` 与 `TNS=0` 是一致的。

**练习 2**：保持时间裕量 `WHS = 0.044` 比建立时间 `WNS = 6.274` 小两个数量级，这正常吗？

**参考答案**：正常。建立时间关注“数据能否在一整个周期内到达”，周期 10 ns 内裕量通常较大；保持时间关注“数据在时钟沿后能否多停留零点几纳秒”，量级本来就很小。只要 `WHS` 为正即可。

---

### 4.3 建立/保持时间与单条路径明细（Max / Min Delay Paths）

#### 4.3.1 概念说明

总表只给结论，要查“到底是哪条路径最慢、慢在哪里”，就要展开 `Timing Details` 里的路径明细。报告会把路径分成两类列出：

- **Max Delay Paths（建立时间，最多 10 条）**：按建立时间松弛量从小到大排列，第一条就是 `WNS` 对应的最差路径。
- **Min Delay Paths（保持时间，最多 10 条）**：按保持时间松弛量从小到大排列，第一条就是 `WHS` 对应的最差路径。

每条路径明细用一张逐跳（hop-by-hop）的延迟表，把“时钟怎么走、数据怎么走、每一段花了多少纳秒”全部列清。

#### 4.3.2 核心流程

读一条路径明细，按这个顺序看最省力：

1. **Slack 行**：先看 `Slack (MET)` 还是 `(VIOLATED)`，括号里还写明了公式方向——建立是 `required time - arrival time`，保持是 `arrival time - required time`。
2. **Source / Destination**：起点和终点寄存器的实例名，告诉你违例发生在设计的哪一部分。
3. **Path Type**：标注这条路径用的是哪个工艺角（见 4.4）。
4. **Requirement**：时序预算，通常等于时钟周期。
5. **Data Path Delay**：数据通路实际消耗的延迟，并拆成 `logic`（逻辑单元）和 `route`（布线）两部分及占比。
6. **Logic Levels**：组合逻辑的级数（经过了几个 LUT / 进位链）。
7. **Clock Path Skew**：时钟偏移，含 `DCD / SCD / CPR` 三个分量。

#### 4.3.3 源码精读

**建立时间最差路径**（实现阶段）位于 `Max Delay Paths` 之下：

[vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:L250-L271](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L250-L271)

关键字段读法（精简版）：

```
Slack (MET) :        6.274ns  (required time - arrival time)
  Path Type:         Setup (Max at Slow Process Corner)
  Requirement:       10.000ns  (aclk rise@10.000ns - aclk rise@0.000ns)
  Data Path Delay:   3.726ns  (logic 1.930ns (51.804%)  route 1.796ns (48.196%))
  Logic Levels:      7  (CARRY4=5 LUT4=2)
  Clock Path Skew:  -0.027ns (DCD - SCD + CPR)
    Destination Clock Delay (DCD): 4.416ns
    Source Clock Delay      (SCD): 4.782ns
    Clock Pessimism Removal (CPR): 0.339ns
```

逐项解释：

- `Slack (MET) : 6.274ns (required time - arrival time)`：建立时间裕量为正、已满足；方向是“需求 − 到达”。
- `Requirement: 10.000ns`：等于一个 `aclk` 周期（100 MHz → 10 ns），这就是数据到达的预算。
- `Data Path Delay: 3.726ns`：数据通路只花了 3.726 ns，其中逻辑 1.930 ns、布线 1.796 ns——**逻辑与布线几乎各占一半**。
- `Logic Levels: 7 (CARRY4=5 LUT4=2)`：最差路径经过 7 级组合逻辑（5 级进位链 + 2 个查找表）。级数越多，组合延迟越大；这条路径正是 CIC 加法器（`gen_adder`）的进位链。
- `Source / Destination` 实例名都含 `...gen_no_dsp48.gen_adder...`，说明最差路径在 CIC Compiler 的“无 DSP48、纯逻辑加法器”部分——这与 u1-l4 看到的 `DSP=0` 互相印证。
- `Source Clock Delay (SCD) = 4.782ns`：时钟从管脚走到源寄存器花了 4.782 ns（含 `IBUF` + `BUFG` + 全局时钟网）。

**保持时间最差路径**位于 `Min Delay Paths` 之下，写法恰好相反：

[vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:L1046-L1058](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L1046-L1058)

```
Slack (MET) :        0.044ns  (arrival time - required time)
  Path Type:         Hold (Min at Fast Process Corner)
  Requirement:       0.000ns  (aclk rise@0.000ns - aclk rise@0.000ns)
  Data Path Delay:   0.268ns  (logic 0.141ns (52.702%)  route 0.127ns (47.298%))
  Logic Levels:      0
```

注意三处与建立路径的不同：① Slack 方向是 `arrival time - required time`；② `Requirement = 0.000ns`（保持检查比较的是**同一个**时钟沿，预算为 0）；③ `Logic Levels = 0`（最差保持路径是一段很短的直接连线）。

#### 4.3.4 代码实践

本讲义的主实践任务即落在这一节。

1. **实践目标**：从最差建立路径中提取四个关键字段，并据此判断是否满足约束。
2. **操作步骤**：打开 `timing_impl_R16_N4.txt`，搜索 `Max Delay Paths`，读取紧随其后的第一条路径。
3. **需要观察的现象**：`Slack (MET)` 的数值与符号、`Requirement`、`Data Path Delay`、`Logic Levels`、`Source Clock Delay`。
4. **预期结果**（已从报告确认）：

   | 字段 | 取值 |
   | --- | --- |
   | Slack（建立） | 6.274 ns，**MET（满足）** |
   | Requirement | 10.000 ns |
   | Data Path Delay | 3.726 ns（logic 1.930 / route 1.796） |
   | Logic Levels | 7（CARRY4=5 LUT4=2） |
   | Source Clock Delay (SCD) | 4.782 ns |

5. 结论：`Slack` 为正且标注 `(MET)`，因此该路径（也是全设计最差路径）满足时序约束。

#### 4.3.5 小练习与答案

**练习 1**：`Data Path Delay` 拆成 `logic` 与 `route` 两部分有什么用？

**参考答案**：它直接提示优化方向。若 `route` 占比高，说明布线延迟大，可尝试改善布局或加流水线；若 `logic` 占比高，说明组合逻辑太深，可减少 `Logic Levels`。本例两者各约一半，属正常。

**练习 2**：建立检查的 `Requirement = 10.000ns`，保持检查的 `Requirement = 0.000ns`，为什么差这么多？

**参考答案**：建立检查比较的是**相邻两个**时钟沿（间隔一个周期 10 ns），数据必须在这段时间内到达；保持检查比较的是**同一个**时钟沿（间隔 0 ns），只关心数据在该沿之后是否停留够久，所以预算为 0。

---

### 4.4 多角（Slow / Fast）分析

#### 4.4.1 概念说明

芯片制造存在工艺偏差，同一批晶圆上晶体管的开关速度有快有慢。Vivado 默认开启 **多角分析（Multi-Corner Analysis）**，在两个极端工艺角下都做时序检查，以保证芯片在任何情况下都能正常工作：

- **Slow 角（慢速工艺角）**：晶体管最慢，组合逻辑延迟最大。
- **Fast 角（快速工艺角）**：晶体管最快，组合逻辑延迟最小。

直觉上，“慢”对建立时间最不利（数据跑得慢，最容易迟到），而“快”对保持时间最不利（数据跑得太快，到达太早反而可能破坏保持）。因此：

- **建立时间在 Slow 角取最差值**。
- **保持时间在 Fast 角取最差值**。

#### 4.4.2 核心流程

在路径明细里，多角分析的痕迹体现在 `Path Type` 一行：

- 建立路径写 `Setup (Max at Slow Process Corner)` → 这条最差建立裕量是在 **Slow** 角算出来的。
- 保持路径写 `Hold (Min at Fast Process Corner)` → 这条最差保持裕量是在 **Fast** 角算出来的。

总表里的 `WNS / WHS`，正是分别取自这两个最不利角的值。

#### 4.4.3 源码精读

先看报告开头的 `Timer Settings`，确认多角分析已开启：

[vivado_reports/reports_at_100Mhz/CIC Compiler/timing_impl_R16_N4.txt:L30-L34](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L30-L34)

```
  Corner  Analyze    Analyze    
  Name    Max Paths  Min Paths  
  ------  ---------  ---------  
  Slow    Yes        Yes        
  Fast    Yes        Yes        
```

这说明 **Slow 与 Fast 两个角都同时分析了 Max（建立）和 Min（保持）路径**。其中最终被采纳的“最差值”是：建立用 Slow 角、保持用 Fast 角。

再到路径明细里印证：

- 最差建立路径 `Path Type: Setup (Max at Slow Process Corner)`——见 [L258](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L258)。
- 最差保持路径 `Path Type: Hold (Min at Fast Process Corner)`——见 [L1054](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_impl_R16_N4.txt#L1054)。

两相对照，正好印证了“建立查 Slow、保持查 Fast”的规则。

#### 4.4.4 代码实践

1. **实践目标**：亲手确认每条建立/保持路径分别用了哪个工艺角。
2. **操作步骤**：在 `timing_impl_R16_N4.txt` 中搜索 `Path Type:`，分别查看 `Max Delay Paths` 区（L250 起）和 `Min Delay Paths` 区（L1046 起）的首条路径。
3. **需要观察的现象**：建立路径全是 `... at Slow Process Corner`，保持路径全是 `... at Fast Process Corner`。
4. **预期结果**：建立 10 条路径均为 `Setup (Max at Slow Process Corner)`；保持 10 条路径均为 `Hold (Min at Fast Process Corner)`。
5. 此为阅读型实践，结论可直接从报告核对。

#### 4.4.5 小练习与答案

**练习 1**：为什么建立时间要在 Slow 角而不是 Fast 角取最差值？

**参考答案**：建立时间关心数据到达是否足够早。Slow 角下晶体管最慢、组合延迟最大，数据到达最晚，是对建立时间最不利的情形，因此用 Slow 角代表最坏情况。

**练习 2**：如果某设计的保持时间在 Fast 角通过、但在 Slow 角有违例，总表里 `WHS` 会显示通过还是违例？

**参考答案**：保持检查的“最差值”取自 **Fast** 角，因此只要 Fast 角通过，报告采纳的 `WHS` 即为正、显示满足；Slow 角的保持违例不会改变 `WHS` 的取值（但可在对应路径明细里查到）。这也解释了为什么读报告时要留意 `Path Type` 注明的工艺角。

---

## 5. 综合实践

把本讲四个模块串起来，做一个“综合 vs 实现”对比小任务。

**任务**：同时打开 `timing_synth_R16_N4.txt` 与 `timing_impl_R16_N4.txt`，回答以下问题：

1. 两份报告的 `WNS` 分别是多少？哪一份更小（更悲观）？
2. 展开各自的 `Max Delay Paths` 首条路径，比较 `Data Path Delay` 及其 `route`（布线）占比。布线后哪一份的 `route` 更大？
3. 观察路径明细逐跳表里的 `Location` 列：实现报告里有没有具体的物理位置（如 `SLICE_X3Y57`、`N15`）？综合报告里写的是什么？

**参考答案**（均可从报告确认）：

| 项目 | 综合（synth） | 实现（impl） |
| --- | --- | --- |
| `WNS` | 6.531 ns | 6.274 ns（更小、更悲观） |
| `Data Path Delay` | 3.365 ns（route 1.247 ns，约 37%） | 3.726 ns（route 1.796 ns，约 48%） |
| 逐跳表 `Location` | 多为空、网络标 `unplaced` | 有真实位置（如 `SLICE_X3Y57`）、网络标 `routed` |

综合报告可在 [L168-L170](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_synth_R16_N4.txt#L168-L170) 与 [L250-L265](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/timing_synth_R16_N4.txt#L250-L265) 查看；实现报告见本讲 4.3 的引用。

**结论**：实现阶段引入了真实布局布线，所以布线延迟变大、`WNS` 变小、且每跳都有了物理坐标。这正是 u1-l4 所说“impl 报告含真实布线延迟，是论文后实现评估依据”的直接证据，也为下一讲 u2-l3（报告元信息与 `.txt`/`.rpx` 格式）理解两份报告的关系做铺垫。

---

## 6. 本讲小结

- 判断时序只需看 `Design Timing Summary`：`WNS / WHS` 为正、`TNS/THS Failing Endpoints` 为 0、且出现 `All user specified timing constraints are met.`，即代表收敛。
- `WNS` 看最差建立裕量、`TNS` 看违例规模；`WHS / THS` 是保持时间的对应版本，二者都必须为正。
- 展开路径明细可定位最慢路径：本设计最差建立路径 `Requirement = 10.000 ns`、`Data Path Delay = 3.726 ns`、`Logic Levels = 7`、`SCD = 4.782 ns`，落在 CIC 的无 DSP48 加法器进位链上。
- 建立松弛量是 `required − arrival`，保持松弛量是 `arrival − required`，方向相反，报告 `Slack` 行括号里写得很清楚。
- 多角分析：建立取 **Slow** 角最差值、保持取 **Fast** 角最差值，读路径时务必留意 `Path Type` 标注的工艺角。

---

## 7. 下一步学习建议

- 继续阅读 **u2-l2 读懂利用率报告（utilization）**，把“时序是否过”与“用了多少资源”两套指标配齐。
- 学完 u2-l2 后，进入 **u2-l5 实验矩阵**，理解 100/290/300 MHz 三个频率点的时钟周期换算——这正是后续 u3-l2 用 `WNS` 估算 `fmax` 的基础。
- 进阶可看 **u3-l2 时序收敛与最大频率（fmax）分析**：把本讲的 `WNS = 6.274 ns` 与时钟周期 `10 ns` 结合，估算本设计最大可跑频率。
