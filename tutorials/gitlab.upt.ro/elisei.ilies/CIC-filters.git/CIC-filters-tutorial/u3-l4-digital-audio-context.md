# 数字音频应用场景适配

## 1. 本讲目标

本讲把前几讲的「读报告、做对比、看趋势」能力，落到一个具体工程问题上：**为什么数字音频系统要用 CIC 抽取滤波器，以及怎样用本仓库的评估数据指导音频 FPGA 的选型与位宽设计**。

学完后你应当能够：

1. 说清 CIC 在 ΣΔ（Sigma-Delta）ADC 数字前端抽取链中扮演的「第一级大比率抽取」角色。
2. 把音频采样率（44.1/48/96 kHz 等）与 CIC 抽取率 R、调制器过采样率联系起来。
3. 用 CIC 增益与位宽增长公式 \((MR)^N\) 估算所需内部位宽，并据此判断寄存器资源需求。
4. 把本仓库的实验结论（DSP=0、fmax 在百兆赫兹量级、资源随 N 线性增长）映射为音频 CIC 的选型与设计取舍。

> 说明：本仓库只存放 Vivado 报告，**论文正文不在仓库内**，论文使用的具体音频参数（调制器速率、过采样比 OSR、目标 SNR、M 取值等）无法从仓库直接核实，相关结论一律标注「待确认」。本讲中可直接核实的部分，全部来自仓库内的真实报告文件。

## 2. 前置知识

本讲是专家层内容，承接以下两讲，请确认你已掌握：

- **u1-l3 CIC 滤波器原理入门**：积分器（累加器，IIR）+ 梳状器（差分器，FIR）级联；只用加减法和寄存器、不用乘法器；三参数 R（速率比）、N（级数）、M（差分延迟）；直流增益 \((MR)^N\)。
- **u3-l3 R/N 参数扫描趋势分析**：固定方案/频率/器件，资源随 N 近线性增长（拟合 \(\text{Reg}\approx 112N-112\)）、随 R 次线性增长；WNS 与 fmax 随 N 下降。

此外，需要一点信号处理直觉：

- **过采样（Oversampling）**：以远高于奈奎斯特速率的频率采样，再用数字滤波+抽取把速率降下来，换取更高的等效分辨率。
- **抽取（Decimation）= 滤波 + 降采样**：先抗混叠滤波，再每 R 个样本取 1 个，速率降为 \(1/R\)。
- **SNR 与位宽**：每多 1 位约 6 dB 动态范围；16 位音频约 96 dB，24 位约 144 dB。

## 3. 本讲源码地图

本讲涉及的关键文件（均为仓库内真实文件）：

| 文件 | 作用 |
|------|------|
| `README.md` | 唯一的项目说明，确认本仓库服务于 FPGA 上 CIC 滤波器数字音频应用的后实现评估论文 |
| `vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt` | 主分析对象，给出 CIC Compiler 在 R16/N4 下的资源占用（DSP=0、BRAM=0）与器件容量 |
| `vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R64_N4.txt` | 用于佐证「R 增大→位宽增长→寄存器/进位链增长」趋势 |

## 4. 核心概念与源码讲解

### 4.1 ΣΔ 调制与抽取滤波链

#### 4.1.1 概念说明

数字音频 ADC（如手机、声卡里的音频编码器）几乎都用 **ΣΔ（Sigma-Delta）架构**，而不是传统的逐次逼近（SAR）或 Flash。原因在于音频是「窄带、高动态范围」信号——ΣΔ 用**极高的过采样比**换取极高的等效分辨率，且对模拟器件匹配精度要求低，适合 CMOS 工艺。

ΣΔ 调制器在模拟域输出一个**高速、低位宽**（常为 1 位）的码流。例如 48 kHz 的目标音频，调制器可能以几兆赫兹运行。这个高速码流无法直接使用，必须用一个**抽取滤波链**把它降采样到目标速率，同时把噪声整形推到带外的高频噪声滤掉。

这条抽取链通常分三级：

```
ΣΔ调制器高速码流 ──► [CIC抽取] ──► [补偿FIR(CFIR)] ──► [低通FIR/PFIR] ──► 音频基带
   (MHz, 1~多位)        R倍↓          部分↓              再↓           (48 kHz, 多位)
```

- **第一级：CIC 抽取滤波器**。承担最大的降采样比（R 可达 32~256），工作在最高速率，且**不能用乘法器**（速率太高）。这正是 CIC 的用武之地——它只用加减法。
- **第二级：补偿 FIR（CFIR）**。CIC 的频响是 sinc 函数，通带内有明显「下垂（droop）」，CFIR 把这个下垂补偿回来。
- **第三级：低通/整形 FIR（PFIR）**。做最终的陡峭抗混叠与速率微调，这里才大量使用乘加（MAC），才会动用 FPGA 的 DSP 单元。

一句话：**CIC 永远是抽取链的第一级，因为只有它能在最高速率下做大比率抽取而不需要乘法器。**

#### 4.1.2 核心流程

ΣΔ 数字前端的信号流可以概括为：

1. 模拟 ΣΔ 调制器以 \(f_\text{mod}\) 输出 1 位（或低位宽）码流。
2. CIC 抽取器以 \(f_\text{mod}\) 接收，在积分器组（高速）完成累加，经 ↓R 降采样后由梳状器组（低速 \(f_\text{mod}/R\)）做差分。
3. 输出送入 CFIR/PFIR，最终得到目标采样率的音频数据。

> 为什么 CIC 把梳状器放在降采样之后？这是 u1-l3 讲过的 **Noble 恒等式**：把梳状器的延迟搬到低速率侧，延迟长度从 \(MR\) 拍缩短为 \(M\) 拍，省寄存器。这也解释了为什么本仓库里 CIC 的梳状延迟可以用 `SRL16E` 这种小移位寄存器实现（见 u2-l2）。

#### 4.1.3 源码精读

本仓库不含 HDL 源码，但 README 明确指出评估对象就是用于数字音频的 CIC 滤波器：

[README.md:L1](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/README.md#L1) —— 这一行说明报告服务于 "Post-Implementation Evaluation of CIC Filters for **Digital Audio Applications** on FPGA" 论文，确认了 CIC 的应用场景是数字音频。

更关键的是，报告里的资源构成直接印证了「CIC 是只做加减、不含乘法器的第一级抽取器」这一角色定位。看 CIC Compiler 方案（R16/N4）的 DSP 与 BRAM：

[utilization_impl_R16_N4.txt:L110-L117](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L110-L117) —— DSP 节：`DSPs | 0`，240 个可用全不用。

[utilization_impl_R16_N4.txt:L97-L106](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L97-L106) —— Memory 节：`Block RAM Tile | 0`，135 块全不用。

**DSP=0、BRAM=0** 是 CIC 的「特征签名」：它不需要乘法器（DSP），也不需要块存储（BRAM）做系数表/延迟线。这恰好是 ΣΔ 链第一级所要求的——能以最高速率跑、零乘法开销。真正消耗 DSP 的是后级的 FIR，不在本评估范围内。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：验证「CIC 只用加减、无乘法」这一论断在三种实现方案中是否一致。
2. **步骤**：分别打开下列三份 utilization 报告的 DSP 节：
   - `vivado_reports/reports_at_100Mhz/CIC Compiler/utilization_impl_R16_N4.txt`
   - `vivado_reports/reports_at_100Mhz/MATLAB HDL Coder/utilization_impl_R16_N4.txt`
   - `vivado_reports/reports_at_100Mhz/Open-source CIC/utilization_impl_R16_N4.txt`
3. **观察**：三份报告的 `DSPs` 行是否都是 `0`。
4. **预期**：三者均为 0，三种独立实现路径交叉互证 CIC 无乘法器。
5. 这是 u3-l1 已建立的结论，此处用于巩固「CIC 适合做 ΣΔ 第一级抽取」的物理依据。

#### 4.1.5 小练习与答案

**练习 1**：为什么不让 FIR 直接做 ΣΔ 的大比率抽取，而要先过 CIC？

> **答案**：FIR 需要乘法器，在调制器几兆赫兹的速率下逐样本做乘加，功耗和资源代价很高；CIC 只用加减法，能在最高速率下完成 32~256 倍的大比率抽取，把速率降到 FIR 可承受的范围，再由 FIR 做精细滤波。

**练习 2**：CIC 之后为什么必须接一个补偿 FIR（CFIR）？

> **答案**：CIC 的频率响应是 sinc 函数，通带内（尤其接近 \(f_s/2\) 处）有显著幅度下垂，会 distortion 音频信号；CFIR 的频响设计成 CIC 的逆，把通带拉平。

---

### 4.2 音频采样率与抽取比 R

#### 4.2.1 概念说明

音频世界有一套固定的采样率档位：

| 场景 | 采样率 |
|------|--------|
| CD 音质 | 44.1 kHz |
| 专业/影视/USB 音频 | 48 kHz |
| 高解析 | 96 kHz、192 kHz |

ΣΔ ADC 的调制器以**过采样率 OSR**（Oversampling Ratio）倍于基带速率运行。常见 OSR 为 64、128、256、512。调制器时钟为：

\[
f_\text{mod} = \text{OSR}\times f_\text{audio}
\]

例如目标 48 kHz、OSR=64，则调制器码流为 \(64\times 48\,\text{kHz}=3.072\,\text{MHz}\)。整个抽取链的总抽取比等于 OSR（这里是 64）。

关键设计选择：**总抽取比 OSR 怎么在 CIC 与后续 FIR 之间分配？** CIC 通常拿走大头（如 R=64 直接一步到位，或 R=32 留 2 倍给 FIR），因为 CIC 在高速率下效率最高。

#### 4.2.2 核心流程

给定目标音频速率与 OSR，反推 CIC 配置：

1. 确定 \(f_\text{audio}\)（如 48 kHz）与 OSR（如 64）。
2. 算 \(f_\text{mod}=\text{OSR}\cdot f_\text{audio}\)（如 3.072 MHz）。
3. 选 CIC 的 R：若希望 CIC 一步到基带，则 \(R=\text{OSR}\)；若留余量给 FIR，则 R 取 OSR 的约数（如 R=32，FIR 再 2 倍）。
4. 检查 R 是否落在工程可用范围。本仓库评估的 R 取值为 \(\{4,8,16,32,64\}\)，正好覆盖典型音频 OSR 的约数。

> 注意：本仓库文件名只编码 R 与 N，**不编码 M**（M 为差分延迟，取值待确认，CIC 习惯取 M=1 或 M=2）。论文具体使用的 OSR 与采样率，因论文不在仓库内，标注「待确认」。

#### 4.2.3 源码精读

本仓库的 R 取值范围是从文件名归纳出来的（u1-l2、u2-l4 已建立）：

CIC Compiler 方案在 100 MHz 下的报告覆盖 R∈{8,16,32,64}（CIC Compiler IP 受厂商限制，无 R4、无 N2，矩阵稀疏；见 u2-l4）。看 R=64 的资源报告头部确认配置：

[utilization_impl_R64_N4.txt:L7-L10](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R64_N4.txt#L7-L10) —— `Design: cic_compiler_0`、`Device: xc7a100tcsg324-1`、`Design State: Routed`，确认这是 CIC Compiler IP 在 Artix-7 上的后实现报告。

值得强调：**R=64 这一档直接对应「48 kHz × OSR=64 = 3.072 MHz 调制器，CIC 一步抽取到 48 kHz」的典型音频场景**，所以本仓库的数据对音频选型是有现实代表性的（尽管论文是否正是此配置待确认）。

#### 4.2.4 代码实践（计算型）

1. **目标**：把音频采样率换算成调制器时钟，并匹配到本仓库的 R 档位。
2. **步骤**：对下表每个音频场景，假设 OSR=64，计算 \(f_\text{mod}\)，并指出哪个 R 档位可让 CIC 一步降到基带。

   | \(f_\text{audio}\) | OSR | \(f_\text{mod}\) | 一步到基带的 R |
3. **预期**：48 kHz → 3.072 MHz → R=64；若改为 OSR=128，则需 R=128（超出本仓库范围，说明此时 CIC 不能一步到位，需拆分）。
4. **观察**：本仓库最大 R=64，恰好覆盖 OSR=64 的一步抽取；OSR 更大时必须用「CIC + FIR」多级拆分。

#### 4.2.5 小练习与答案

**练习 1**：目标 44.1 kHz（CD）、OSR=128，调制器时钟是多少？若 CIC 取 R=64，后级还需几倍抽取？

> **答案**：\(f_\text{mod}=128\times 44.1\,\text{kHz}\approx 5.645\,\text{MHz}\)。CIC 取 R=64 后剩下 \(128/64=2\) 倍，由后级 FIR 完成。

**练习 2**：为什么 OSR 越大，越倾向于用 CIC+多级 FIR 拆分，而不是单级 CIC？

> **答案**：单级大 R 的 CIC 通带下垂与混叠抑制会变差，且按 4.3 节的位宽公式，R 越大内部位宽增长越猛；拆成 CIC（拿大头）+ FIR（精细补偿）能在资源、位宽与频响之间取得平衡。

---

### 4.3 CIC 增益与位宽增长

#### 4.3.1 概念说明

这是本讲最硬核的模块，直接决定音频 CIC 的寄存器资源需求。

CIC 抽取器的**直流增益**为：

\[
G = (MR)^N
\]

也就是说，一个 R=64、N=4、M=1 的 CIC，直流增益高达 \(64^4 = 16{,}777{,}216\)，约 \(2^{24}\)。这个巨大增益意味着**积分器内部的数据通路会随级数飞速变宽**，必须用足够位宽的累加器，否则溢出。

Hogenauer 给出的**无溢出最大内部位宽**（full growth width）为：

\[
B_\text{max} = B_\text{in} + \lceil N\log_2(MR) \rceil
\]

其中 \(B_\text{in}\) 是输入字长（如 1 位 ΣΔ 码流则 \(B_\text{in}=1\)）。这个 \(B_\text{max}\) 是积分器累加器必须保证的位宽，**寄存器数量近似正比于 \(B_\text{max}\)**——这就把「位宽增长」与 u3-l3 观察到的「资源随 N、R 增长」联系起来了。

> 工程上，最终输出不需要保留全部 \(B_\text{max}\) 位。Hogenauer 还给出了逐级可丢弃的低位数公式，使截断噪声低于量化噪声底；但积分器段（降采样之前）必须保留全宽 \(B_\text{max}\) 以防溢出。

#### 4.3.2 核心流程

估算音频 CIC 寄存器用量的步骤：

1. 确定音频配置 \(R, N, M, B_\text{in}\)。
2. 算 \(B_\text{max} = B_\text{in} + \lceil N\log_2(MR)\rceil\)。
3. 数寄存器：N 个积分器累加器 + N×M 个梳状延迟，每个宽 \(B_\text{max}\)，故寄存器位数约 \(N(1+M)\,B_\text{max}\)；折算触发器个数再考虑打包。
4. 与目标器件容量比较。

举例：R=64、N=4、M=1、\(B_\text{in}=1\)：

\[
B_\text{max}=1+\lceil 4\log_2(64)\rceil = 1+24 = 25\text{ 位}
\]

寄存器位数约 \(4\times(1+1)\times 25 = 200\) 位（一阶估算）。

#### 4.3.3 源码精读

用真实报告验证「R 增大 → 位宽增长 → 寄存器与进位链增长」这一趋势。固定方案（CIC Compiler）、频率（100 MHz）、N=4，只把 R 从 16 加到 64：

[utilization_impl_R16_N4.txt:L32-L45](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L32-L45) —— R16/N4：Slice Registers = **261**，Slice LUTs = 155。

[utilization_impl_R64_N4.txt:L32-L45](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R64_N4.txt#L32-L45) —— R64/N4：Slice Registers = **339**，Slice LUTs = 210。

R 从 16→64（\(\log_2\) 增加 2 位），寄存器 261→339（+78）。再看 Primitives 表里受位宽直接驱动的进位链与移位寄存器：

[utilization_impl_R16_N4.txt:L181-L196](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L181-L196) —— R16/N4：FDRE=260、**CARRY4=25**、SRL16E=41。

[utilization_impl_R64_N4.txt:L181-L196](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R64_N4.txt#L181-L196) —— R64/N4：FDRE=338、**CARRY4=34**、SRL16E=51。

CARRY4（进位链，承担多位加减法的逐位进位）从 25→34（+9），SRL16E（梳状器移位延迟）从 41→51（+10）。这正是位宽增长的物理证据：**累加器变宽，进位链更长、移位寄存器更宽**。

> 说明：上面的一阶估算（~200 位/触发器）与 IP 实测（261/339 个寄存器）在同一量级，差异来自 IP 内部的流水线、输出格式化与控制寄存器开销，以及 Xilinx CIC Compiler IP 内部数据通路的固定划分（具体如何划分待确认）。我们要把握的是**趋势方向**，而非逐位精确吻合。

#### 4.3.4 代码实践（计算 + 阅读型）

1. **目标**：用位宽公式解释为什么 N 对资源的影响比 R 更剧烈（承接 u3-l3）。
2. **步骤**：
   - 对 R=16、M=1、\(B_\text{in}=1\)，算 N=2、4、6 各自的 \(B_\text{max}\)。
   - 再对 N=4，算 R=16、32、64 各自的 \(B_\text{max}\)。
3. **观察**：\(B_\text{max}\) 随 N 的变化斜率 vs 随 R 的变化。
4. **预期**：固定 R 时，\(B_\text{max}\) 随 N **线性**增长（每级加 \(\log_2(MR)\) 位）；固定 N 时，随 R **对数型**增长（\(B_\text{max}\propto\log_2 R\)）。这与 u3-l3「资源随 N 近线性、随 R 次线性」的实测结论在根因上吻合。
5. 本步骤纯计算，无需运行命令；如需核对，可与 `vivado_reports/reports_at_100Mhz/CIC Compiler/` 下不同 N、R 的 utilization 报告的寄存器数对比。

#### 4.3.5 小练习与答案

**练习 1**：R=64、N=6、M=1、\(B_\text{in}=1\)，求 \(B_\text{max}\)。

> **答案**：\(B_\text{max}=1+\lceil 6\times\log_2 64\rceil=1+36=37\) 位。注意级数 N=6 比示例的 N=4 又多长了 12 位，这就是 u3-l3 里「资源随 N 近线性增长」的位宽根源。

**练习 2**：为什么说「积分器段必须保留全宽 \(B_\text{max}\)」，而梳状器/输出可以截断？

> **答案**：积分器是 IIR（带反馈的累加器），任何溢出都会永久污染后续所有样本，所以必须无溢出，用满 \(B_\text{max}\)；梳状器与最终输出是 FIR 式前馈结构，可在保证截断噪声低于量化底的前提下安全丢弃低位。

---

### 4.4 选型建议：把实验结论映射到音频设计

#### 4.4.1 概念说明

现在把前几个模块和前几讲的数据，综合成对音频 FPGA 选型的可操作结论。核心判断有三条：

**结论一：音频 CIC 的速度约束几乎不存在，位宽才是瓶颈。**
u3-l3 给出 CIC 的 fmax 在约 149~391 MHz 量级（随 N 下降）。而音频 ΣΔ 调制器时钟通常只有几兆赫兹（如 3.072 MHz）。**调制器速率 ≪ fmax**，意味着音频 CIC 永远不会逼近时序墙——本仓库 100/290/300 MHz 的评估刻画的是「速度天花板」，不是音频工作点。所以对音频 CIC，**位宽（决定 SNR）和混叠抑制才是绑定约束，时序不是**。

**结论二：音频 CIC 对 FPGA 面积的需求极小，选型应由后级 FIR 决定。**
看 CIC Compiler R16/N4 在 xc7a100t（Artix-7，63400 LUT / 126800 寄存器 / 240 DSP / 135 BRAM）上的占用：

[utilization_impl_R16_N4.txt:L32-L45](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L32-L45) —— LUT=155（Util% 0.24）、寄存器=261（0.21）、DSP=0、BRAM=0，全部不到 0.5%。

CIC 本身只占器件的零头；真正吃资源的是后级的补偿/整形 FIR（它们才用 DSP 和 BRAM）。**所以为一个音频 ΣΔ 前端选 FPGA 时，按 FIR 的 MAC 需求和音频通道数来选，CIC 几乎不构成约束。** xc7a100t 这种规模对单通道音频 CIC 是「大材小用」。

**结论三：三种实现方案对音频的取舍。**
- **CIC Compiler IP**：资源最省（用 SRL16E 把梳状延迟塞进 LUT），开箱即用，但 R/N 受 IP 限制（无 R4、无 N2），且 IP 是黑盒、可移植性差。
- **MATLAB HDL Coder**：参数覆盖最全（5×5 满），便于从算法仿真到位真代码的连续验证，适合研究型音频原型。
- **Open-source RTL**：完全可控、可移植到任意 FPGA 厂商，但面积最重（控制集翻倍，见 u3-l1），适合需要源码级定制的量产设计。

> 以上方案取舍来自 u2-l4、u3-l1 的实测，是仓库可核实内容。论文最终采用了哪种方案、以及具体的音频指标（通道数、SNR 目标、是否多级抽取），因论文不在仓库内，标注「待确认」。

#### 4.4.2 核心流程

音频 CIC 选型的决策树：

1. 定音频基带速率与 OSR → 算 \(f_\text{mod}\)、总抽取比。
2. 拆分抽取比：CIC 拿大头（R 取 OSR 或其大约数），FIR 拿剩余。
3. 用 \(B_\text{max}=B_\text{in}+\lceil N\log_2(MR)\rceil\) 估算内部位宽，确认满足目标 SNR（每 1 位≈6 dB）。
4. 选 N：N 越大混叠抑制越强、通带越陡，但位宽与寄存器近线性增长、fmax 下降。在 SNR 达标前提下取**最小的 N**。
5. 选实现方案：原型/研究 → HDL Coder；量产单厂 → CIC Compiler IP；跨厂可移植 → Open-source。
6. 选 FPGA：按后级 FIR 的 DSP/BRAM 需求 + 通道数选，CIC 资源忽略不计。

#### 4.4.3 源码精读

器件容量信息（用于结论二）来自 utilization 报告的 Available 列：

[utilization_impl_R16_N4.txt:L32-L45](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L32-L45) —— Available 列：Slice LUTs 63400、Slice Registers 126800，Used 仅 155/261，Util% 0.24/0.21。

[utilization_impl_R16_N4.txt:L110-L117](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R16_N4.txt#L110-L117) —— DSP 节：240 个 DSP 全部可用、CIC 用 0。

这些数字直接支撑「CIC 对面积无要求、选型看 FIR」的判断。

#### 4.4.4 代码实践（阅读 + 推理型）

1. **目标**：判断 xc7a100t 能否容纳「多通道音频 ΣΔ 前端」。
2. **步骤**：
   - 假设 8 通道音频，每通道一个 R16/N4 CIC Compiler。
   - 用上面的 Used 数乘以 8，再与 Available 对比。
3. **预期**：8×155=1240 LUT、8×261=2088 寄存器，相对 63400/126800 仍不到 2%，DSP 仍为 0。说明即便多通道，CIC 也远不构成约束。
4. **结论**：把节省下来的 DSP/BRAM 预算留给后级 FIR，这正是音频 FPGA 选型的真实着眼点。

#### 4.4.5 小练习与答案

**练习 1**：对一个要求 24 位（约 144 dB）输出的音频链，CIC 的 \(B_\text{max}\) 是否一定 ≥24？

> **答案**：是的。要保留 24 位有效输出，内部全宽 \(B_\text{max}\) 必须显著大于 24（因为高位还要容纳增益 \((MR)^N\) 带来的增长，低位可能在输出级按 Hogenauer 公式截断）。这正是高解析音频（96/192 kHz、24 位）会把 CIC 位宽与寄存器推高的原因。

**练习 2**：若某音频应用把 CIC 工作时钟从 3 MHz 提到 300 MHz，时序还安全吗？依据是什么？

> **答案**：要看具体配置的 fmax。u3-l3 显示大 N（如 N=6）下 fmax 可降到约 149 MHz，此时 300 MHz 会**时序违例**。本仓库 290/300 MHz 档正是用来探明这一天花板的（见 u2-l5、u3-l2）。回到常规音频几兆赫兹时钟，则安全裕量极大。

---

## 5. 综合实践

**任务**：假设一个音频 ΣΔ 前端需要把高速码流抽取到 48 kHz，给出一个合理的 CIC 配置（R、N、M）与所需内部位宽估算，并说明依据。

**参考解答（设计型，非唯一答案）**：

1. **设定**：目标 \(f_\text{audio}=48\,\text{kHz}\)，取 OSR=64，则 \(f_\text{mod}=64\times 48\,\text{kHz}=3.072\,\text{MHz}\)。ΣΔ 调制器输出 1 位码流，故 \(B_\text{in}=1\)。
2. **选 R**：让 CIC 一步到基带，\(R=64\)（落在本仓库评估范围 \(\{4,8,16,32,64\}\) 内）。剩余 1 倍无需再降。
3. **选 M**：取工程习惯值 \(M=1\)（仓库未编码 M，待确认）。
4. **选 N**：在混叠抑制与资源间折中。取 \(N=4\)（u3-l3 表明 N=4 时 fmax 仍高、资源适中）。
5. **算位宽**：

   \[
   B_\text{max}=B_\text{in}+\lceil N\log_2(MR)\rceil=1+\lceil 4\times\log_2 64\rceil=1+24=25\text{ 位}
   \]

6. **核对资源**：一阶估算寄存器位数 \(\approx N(1+M)B_\text{max}=4\times 2\times 25=200\) 位。与本仓库实测 [utilization_impl_R64_N4.txt:L40](https://github.com/gitlab.upt.ro/elisei.ilies/CIC-filters.git/blob/e49b263d702c6fb0d4ea1d5b8390d307e6ba43d1/vivado_reports/reports_at_100Mhz/CIC%20Compiler/utilization_impl_R64_N4.txt#L40) 的 **339 个寄存器**同量级（IP 含流水线/格式化开销，故实测更高）。
7. **核对接收带宽**：25 位内部宽远超 16 位音频（~96 dB）所需，留足 SNR 余量与增益头部。
8. **核对速度**：\(f_\text{mod}=3.072\,\text{MHz}\)，远低于 u3-l3 给出的 fmax（百兆赫兹量级），时序裕量无穷。

**结论**：R=64、N=4、M=1、\(B_\text{max}\approx 25\) 位 是一个合理且资源极轻的音频 CIC 配置；本仓库的 R64/N4 报告可作为其面积与时序的现实参照。

> 若把目标改为 24 位高解析音频（96 kHz、OSR=128），则 \(f_\text{mod}\) 翻倍、CIC 不能一步到位（R=128 超出仓库范围），需拆成 CIC R=64 + FIR 2 倍，且 \(B_\text{max}\) 需再加长以满足 24 位有效输出——这正是多级抽取链存在的理由。

## 6. 本讲小结

- CIC 在 ΣΔ 音频 ADC 数字前端中担任**第一级大比率抽取**，因其只用加减法、无乘法器（仓库三方案 DSP=0 互证），能在调制器几兆赫兹的高速码流上工作。
- 音频采样率（44.1/48/96 kHz）经 OSR 推出调制器时钟；总抽取比 OSR 在 CIC（拿大头）与 FIR（精细补偿）间分配。本仓库 R∈{4,8,16,32,64} 正好覆盖典型音频抽取比。
- CIC 直流增益 \(G=(MR)^N\) 驱动内部位宽增长 \(B_\text{max}=B_\text{in}+\lceil N\log_2(MR)\rceil\)，这是寄存器随 N（线性）、随 R（对数）增长的根因；真实报告 R16→R64 的寄存器 261→339、CARRY4 25→34 即其物理证据。
- 对音频而言，调制器时钟（MHz 级）远低于 CIC 的 fmax（百兆赫兹级），**时序不是绑定约束，位宽（SNR）与混叠抑制才是**；N 应在满足 SNR 前提下取最小。
- CIC 占器件面积不到 0.5%、DSP/BRAM 均为 0，**音频 FPGA 选型应由后级 FIR 的 MAC 需求与通道数决定**，CIC 不构成约束。
- 论文使用的具体音频参数（OSR、采样率、SNR 目标、M 值、最终方案）不在仓库内，一律标注「待确认」。

## 7. 下一步学习建议

- **继续 u3-l5**：学习如何用脚本从大量 timing/utilization 报告批量提取 WNS 与资源指标汇总成表，把本讲的「R64 配置」扩展为跨 R、N 的系统化参数扫描。
- **继续 u3-l6**：了解如何复现整套基准（Vivado 综合与实现、CIC Compiler IP / HDL Coder / 手写 RTL 三条设计生成路径），把你在本讲设计的 R64/N4 音频配置真正跑出报告。
- **延伸阅读（仓库外）**：Hogenauer 的 CIC 经典论文（关于逐级截断位数的严格推导）、以及 Xilinx PG140（CIC Compiler IP 产品指南），用于核实本讲中标注「待确认」的 IP 内部位宽划分与 M 取值。
