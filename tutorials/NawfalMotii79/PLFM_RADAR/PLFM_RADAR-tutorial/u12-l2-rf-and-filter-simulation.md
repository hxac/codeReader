# RF 链路与滤波器仿真

## 1. 本讲目标

u12-l1 讲了「电磁波怎么从天线辐射出去」——那是链路最末端、面向空间的一环。本讲往回退一步，聚焦**射频（RF）与模拟基带链路**上的仿真资产：中频带通滤波器、DAC 重建滤波器、GaN 功率放大器、RF 开关，以及印制板上的 via fence（金属化过孔墙）隔离结构。

学完后你应该能够：

- 看懂仓库里这套电路仿真文件用的是什么工具、各自仿真什么对象（不是想当然的 LTspice，而是 QucsStudio）。
- 读懂 IF 带通滤波器（单端 + 平衡两版）与 DAC 重建低通滤波器的拓扑、阶数、截止/通带频率，并解释它们在接收/发射链路中的位置。
- 跑通 `Generate_ChirpcsvFile.py`，说清它生成的 chirp CSV 如何作为仿真激励喂给 DAC 重建滤波器，以及它与 FPGA 内 chirp LUT 的对应关系。
- 解释 QPA2962 GaN 功放为什么只服务 AERIS-10E（Extended）版本，以及 via fencing 对 10.5 GHz 多通道 RF 板隔离的意义。

## 2. 前置知识

在进入源码前，先用几段大白话把电路级 RF 仿真的关键词讲清楚。

**S 参数（S-parameters）。** 把一个射频器件当成多端口黑盒，从端口 i 喂入功率、在端口 j 测到多少，记作 S[j,i]。最常用的是 S[1,1]（端口 1 的反射，又称回波损耗 / return loss，衡量「阻抗是否匹配」）和 S[2,1]（端口 1→2 的传输，衡量「插损 / 增益」）。理想滤波器在通带内 |S[2,1]| 接近 0 dB（全通）、|S[1,1]| 很低（几乎不反射）；在阻带内 |S[2,1]| 很低（被挡掉）。本讲的 `.dpl` 显示文件就是把 `dB(S[1,1])`、`dB(S[2,1])` 画成频响曲线，把 `S[1,1]` 画到史密斯圆图上看阻抗。

**巴特沃斯滤波器与「阶数 / 类型」。** 巴特沃斯（Butterworth）是一种通带最平坦的滤波器逼近。「n 阶」决定了过渡带的陡峭程度——阶数越高，从通带到阻带掉得越快，但元件越多、群延迟波动越大。「π 型 / T 型」指电感电容的排布形状：π 型是「电容并联—电感串联—电容并联」，T 型是「电感串联—电容并联—电感串联」。本讲遇到的滤波器标注里会反复出现 `4th order Butterworth, PI-type` 这类写法。

**平衡（差分）与变压器。** 单端信号参考地；平衡信号是一对幅度相等、相位相反的差分线（`Vin_P` / `Vin_N`）。很多混频器、DAC 输出是差分的，所以滤波器也要做成平衡版（用变压器 `Tr` 把单端转差分，或两侧对称排布电感电容）。本讲 `IF_BPF_Balanced` 就是平衡版。

**SPfile（Touchstone S 参数文件）。** 厂商会把实测/标定的器件 S 参数存成 `.s2p`（2 端口）、`.s3p`（3 端口）文件。仿真器用 `SPfile` 元件把它「黑盒」接进原理图，就能在小信号线性仿真里复现真实器件的频响，而不必从晶体管方程建模。本讲的 GaN 功放和 RF 开关都是这么用的。

**Via fence（过孔墙）。** 高频多层板上，相邻走线之间会沿「地平面边缘」打一排密集接地过孔，像一道栅栏把 RF 区域围起来。它的作用不是「连一根线」，而是给返回电流提供一条紧贴信号线的低阻抗通路，同时抑制基板里的平行板模式 / 表面波，从而提高通道间隔离、防止 RF 串扰。

> 本讲承接 u2-l2《雷达信号处理流水线》建立的「中频 = 120 MHz」「发射/接收链镜像对称」的整体认知；其中 DDC、匹配滤波等数字部分属于 FPGA 数字域，本讲只覆盖它们之前的模拟 / 射频段。

## 3. 本讲源码地图

| 文件 | 作用 | 工具 |
| --- | --- | --- |
| [5_Simulations/IF_BPF/IF_BPF.sch](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/IF_BPF/IF_BPF.sch) | 中频带通滤波器，**单端**版：4 阶巴特沃斯、π 型、120–180 MHz、50 Ω | QucsStudio |
| [5_Simulations/IF_BPF_Balanced/IF_BPF_Balanced.sch](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/IF_BPF_Balanced/IF_BPF_Balanced.sch) | 中频带通滤波器，**平衡（差分）**版：5 阶巴特沃斯、T 型、110–170 MHz，含变压器与瞬态仿真 | QucsStudio |
| [5_Simulations/DAC_ReconstructionFilter/Generate_ChirpcsvFile.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/DAC_ReconstructionFilter/Generate_ChirpcsvFile.py) | 生成 LFM chirp 的 CSV 激励（含 DAC 零阶保持阶梯化），喂给下面的重建滤波器做瞬态仿真 | Python (numpy/pandas) |
| [5_Simulations/DAC_ReconstructionFilter/DAC_RLPF.sch](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/DAC_ReconstructionFilter/DAC_RLPF.sch) | DAC 重建低通滤波器：5 阶巴特沃斯、π 型、60 MHz 截止，差分结构，加载 chirp CSV 做瞬态 | QucsStudio |
| [5_Simulations/QPA2962.sch](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/QPA2962.sch) | QPA2962 GaN HEMT 功放，用厂商 `.s2p`（22 V / 1680 mA / 85 ℃）做 2–20 GHz S 参数仿真 | QucsStudio |
| [5_Simulations/RF switch/Impedance.sch](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/RF%20switch/Impedance.sch) | M3SWA2-34DR+ SP3T 射频开关，用厂商 `.s3p` 做 2–12 GHz S 参数与阻抗仿真 | QucsStudio |
| [5_Simulations/Fencing/Via_fencing.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/Fencing/Via_fencing.py) | via fence 几何示意图：对比「最小焊盘」与「稳健焊盘」两种过孔对 10.5 GHz 微带线地平面边缘的影响 | Python (matplotlib) |

补充说明：`5_Simulations/` 下还有同族文件 `Via_fencing2.py`（第二张俯视图）、`Stub_BPF*.sch` / `Sim_BPF_Te_100um/`（其它带通滤波器拓扑，本讲不展开）、以及 `RF switch/Sparameters/` 目录里的 4 个 `.s3p`（覆盖 RF1 ON / RF2 ON × 100 MHz / 40 GHz 两种状态与频段）。本讲聚焦上表 7 个核心文件。

---

## 4. 核心概念与源码讲解

本讲按四个最小模块展开，顺序与信号在链路上的流向一致：

1. **IF 带通滤波**——接收链下变频后、ADC 前的模拟带通，框定 120 MHz 中频信号。
2. **DAC 重建 / chirp**——发射链 DAC 输出的阶梯波如何被低通「平滑」成干净模拟 chirp。
3. **GaN 功放**——Extended 版把 chirp 放大到 10 W 的最后一级。
4. **via 隔离**——不在信号通路上，而是「通路之间」的隔离结构，保证多通道 RF 板不互相串扰。

### 4.1 IF 带通滤波

#### 4.1.1 概念说明

雷达接收链把 10.5 GHz 射频下变频到 120 MHz 中频（IF）（见 u7-l2：TX LO 10.5 GHz − RX LO 10.38 GHz = 120 MHz IF，且该 120 MHz 正好对应 DDC 的 NCO 调谐字）。中频信号进 ADC 之前，必须先用一个**带通滤波器**做两件事：① 保留 120 MHz 附近的信号；② 把带外的镜像、噪声、邻道干扰压下去，否则它们会在后续 ADC 采样时折叠回带内。

带通滤波器的设计参数有：中心频率、带宽（通带上下边沿）、阶数（陡峭程度）、阻抗（本系统恒为 50 Ω）、以及单端还是差分。仓库给了两版：单端版用于快速的小信号 S 参数扫频；平衡版面向差分中频通路，并且额外做了瞬态（时域）仿真，把一段多音信号喂进去看滤波器对真实波形的响应。

#### 4.1.2 核心流程

带通滤波器在 QucsStudio 里的仿真套路是：

1. **搭拓扑**：用电感 `L`、电容 `C` 按 π 型或 T 型排成巴特沃斯带通，两端各加一个 `Pac`（功率源 / 端口，内阻 50 Ω）。
2. **加 `.SP` 仿真**：声明一个对数扫频 `log`，从 12 MHz 扫到 1.8 GHz，取 500 个点。
3. **跑 S 参数**：得到 `S[1,1]`（输入反射）、`S[2,1]`（传输）、`S[2,2]`（输出反射）。
4. **画图判读**（在 `.dpl` 文件里定义）：把 `dB(S[2,1])` 画出来，通带（约 120–180 MHz）应接近 0 dB、阻带应快速跌到 −40 dB 以下；把 `S[1,1]` 画到史密斯圆图，确认通带内落在 50 Ω 中心附近。

对数扫频而非线性扫频，是因为滤波器频响的「兴趣区」横跨两个数量级（12 MHz ~ 1.8 GHz），对数轴能让低频段也有足够的点密度。带通中心频率与带宽由元件值决定：对巴特沃斯带通，每个谐振臂的 \(L,C\) 满足 \(f_0=1/(2\pi\sqrt{LC})\)。

#### 4.1.3 源码精读

`IF_BPF.sch` 的元件清单就是一个 4 阶 π 型巴特沃斯带通，两个端口 P1/P2 各 50 Ω：

- [IF_BPF.sch:17-30](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/IF_BPF/IF_BPF.sch#L17-L30)：声明端口 `P1`/`P2`（均为 50 Ω）、4 个接地电感臂（`L1=28.88nH`、`L3=11.96nH` 等）、对应的并联调谐电容（`C1=40.6pF`、`C3=98.03pF` 等）以及两段串联的 LC（`L2/C2`、`L4/C4`）——这就是 π 型带通的物理实现。
- [IF_BPF.sch:31](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/IF_BPF/IF_BPF.sch#L31)：`.SP` 行声明对数扫频 `"log" 12MHz 1.8GHz 500`，即整个带通仿真的扫频范围与点数。
- [IF_BPF.sch:49](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/IF_BPF/IF_BPF.sch#L49)：原理图上的文字标注写明设计意图——`band-pass filter, 120MHz...180MHz, 4th order Butterworth, PI-type, impedance 50Ω`。

显示文件 [IF_BPF.dpl](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/IF_BPF/IF_BPF.dpl) 里把 `dB(S[1,1])`、`dB(S[2,1])` 画成矩形图、把 `S[1,1]` 画成史密斯圆图，是判读这份仿真的标准「仪表盘」。

平衡版 `IF_BPF_Balanced.sch` 把同一思想搬到差分链路上：

- [IF_BPF_Balanced.sch:17-18](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/IF_BPF_Balanced/IF_BPF_Balanced.sch#L17-L18)：用两个变压器 `Tr1`/`Tr2`（`line_filter_inductor` 模型）实现单端↔平衡转换，于是后续 L/C 完全对称分布在 `Vin_P`/`Vin_N` 两条线上。
- [IF_BPF_Balanced.sch:84-85](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/IF_BPF_Balanced/IF_BPF_Balanced.sch#L84-L85)：两个 `Vfile` 电压源加载 `multiband_signal.csv` 作为差分瞬态激励——这是「把真实波形喂进滤波器看时域响应」的关键，单端版只有扫频、没有这一步。
- [IF_BPF_Balanced.sch:173-174](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/IF_BPF_Balanced/IF_BPF_Balanced.sch#L173-L174)：两段文字标注——一段写 `5th order Butterworth, T-type`，另一段给出 `110MHz...170MHz` / `120MHz...170MHz`，说明这是阶数更高（5 阶）、拓扑不同（T 型）的版本，且两段滤波器略带宽不同（差分链路常分段调优）。

> 两版对比说明一个工程习惯：**单端版（4 阶 π、120–180 MHz）**用来快速做 S 参数扫频、确认阻抗与插损；**平衡版（5 阶 T、110–170 MHz）**更贴近实际差分 PCB 链路，并能用 CSV 激励跑瞬态看波形保真度。

#### 4.1.4 代码实践

**实践目标**：用纸笔 + 源码读出这份滤波器的中心频率与带宽是否自洽。

1. 打开 `IF_BPF.sch`，记下第一个并联谐振臂的 `L1=28.88nH`、`C1=40.6pF`。
2. 用 \(f_0=1/(2\pi\sqrt{LC})\) 估算该臂的谐振频率。
3. 把算出的 \(f_0\) 与原理图标注的 120–180 MHz 通带对比。

**预期结果**：\(LC=28.88\text{ nH}\times40.6\text{ pF}\approx1.172\times10^{-18}\)，\(\sqrt{LC}\approx1.083\times10^{-9}\)，\(f_0\approx1/(2\pi\times1.083\text{e-}9)\approx147\text{ MHz}\)——正好落在 120–180 MHz 通带的中间。这就是元件值与设计意图自洽的快速校验。若你的结果偏差很大，说明读错了元件编号（注意 `L1/C1` 是成对并联接地的一臂，不是任意一对）。

#### 4.1.5 小练习与答案

**练习 1**：单端 IF_BPF 用的是 4 阶 π 型，平衡版用的是 5 阶 T 型。阶数从 4 提到 5，对滤波器频响最直接的影响是什么？
**答**：过渡带更陡峭（通带到阻带的下降更快），代价是元件更多、通带边缘的群延迟波动更大。

**练习 2**：为什么 IF_BPF 的 `.SP` 扫频用 `log`（对数）而 RF 开关的 `Impedance.sch` 用 `lin`（线性）？
**答**：带通滤波器的兴趣区横跨 12 MHz~1.8 GHz 两个数量级，对数轴能让低频段也有足够采样密度看清谐振；RF 开关只关心 2–12 GHz 这一个倍频程内的插损平坦度，线性轴更直观。

---

### 4.2 DAC 重建 / chirp 生成

#### 4.2.1 概念说明

发射链上，FPGA 把数字 chirp 样本送给 DAC，DAC 输出的是**阶梯波**（零阶保持，ZOH）——每个采样周期内电压保持恒定，而不是平滑的模拟曲线。阶梯波的频谱里除了想要的基带 chirp，还带有大量以采样率整数倍为中心的镜像（image）。**DAC 重建滤波器**就是紧接 DAC 之后的一个模拟低通滤波器，把这些高频镜像滤掉，留下干净的基带 chirp，再送去上变频。

这一节有两个文件配合：`Generate_ChirpcsvFile.py` 用 Python 合成一段 LFM chirp 并「阶梯化」成 CSV；`DAC_RLPF.sch` 是 5 阶巴特沃斯低通（60 MHz 截止），在 QucsStudio 里加载这个 CSV 做瞬态仿真，验证重建滤波器确实把阶梯抹平成干净 chirp。

> 概念澄清：这个 Python chirp 与 FPGA 内的 chirp LUT（见 u5-l1 的 `long_chirp_lut.mem`）**是两份不同的产物**。FPGA LUT 是真正发射出去、存进 BRAM 的硬件波形（3600 样本 @ 120 MHz = 30 µs）；Python CSV 是给电路仿真用的激励，复用同样的 LFM 数学公式，目的是「拿一段真实 chirp 去敲重建滤波器」。两者共享「瞬时频率线性扫描」的原理，但用途不同，不可混为一谈。

#### 4.2.2 核心流程

LFM（线性调频）chirp 的相位随时间二次方增长，瞬时频率线性变化。设采样间隔 \(T_s=1/F_s\)，第 \(N\) 个样本的时间 \(t=N T_s\)，扫频周期 \(T_b\)，则：

\[
\theta_n = 2\pi\left(\frac{N^2 T_s^2 (f_{max}-f_{min})}{2 T_b} + f_{min} N T_s\right)
\]

\[
y = 1 + \sin(\theta_n)
\]

对其求瞬时频率 \(f(t)=\frac{1}{2\pi}\frac{d\theta}{dt}\) 得：

\[
f(t) = f_{min} + \frac{f_{max}-f_{min}}{T_b}\,t
\]

即频率从 \(f_{min}\) 线性扫到 \(f_{max}\)。脚本里 `Tau` 是脉冲重复周期（两个 chirp 之间的间隔），`Duration` 是总仿真时长，于是波形是「一段 chirp + 一段静默」循环重复。`hold_per_sample>1` 时把每个样本重复若干次，让 CSV 本身就呈阶梯状，模拟 DAC 的零阶保持输出。

#### 4.2.3 源码精读

`Generate_ChirpcsvFile.py` 的核心是 [Generate_ChirpcsvFile.py:5-8](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/DAC_ReconstructionFilter/Generate_ChirpcsvFile.py#L5-L8) 的默认参数：`Fs=125e6, Tb=1e-6, Tau=2e-6, fmax=30e6, fmin=10e6, Duration=6e-6`——即 125 MHz 采样、1 µs 扫频、10→30 MHz（带宽 20 MHz，与 u5-l1 发射链的 20 MHz 带宽一致）、每 2 µs 一个脉冲、总长 6 µs（含 3 个 chirp）。

相位生成在 [Generate_ChirpcsvFile.py:40-43](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/DAC_ReconstructionFilter/Generate_ChirpcsvFile.py#L40-L43)，正是上面那条二次相位公式；`ramp = 1.0 + np.sin(theta_n)`（加 1 使波形落在 0~2，便于 DAC 单极性输出）。

重复拼装在 [Generate_ChirpcsvFile.py:49-54](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/DAC_ReconstructionFilter/Generate_ChirpcsvFile.py#L49-L54)：`while idx + n <= total_samples` 每隔 `prf_samples` 个样本插入一段 `ramp`，其余补零，形成「chirp—静默—chirp」的脉冲串。

阶梯化（ZOH）在 [Generate_ChirpcsvFile.py:59-66](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/DAC_ReconstructionFilter/Generate_ChirpcsvFile.py#L59-L66)：`np.repeat(y, hold_per_sample)` 把每个样本复制若干次，等效于把采样率提到 \(F_s \cdot\)`hold_per_sample`，让 CSV 本身就是阶梯波。

入口 `__main__` 在 [Generate_ChirpcsvFile.py:117-129](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/DAC_ReconstructionFilter/Generate_ChirpcsvFile.py#L117-L129) 调用 `generate_multi_ramp_csv(...)`，默认输出 `multi_ramp_stairs.csv`（仓库里已有此文件，前几行形如 `0.0,1.0` / `8e-09,1.485...`，时间步 8 ns = 1/125 MHz）。

消费侧 `DAC_RLPF.sch`：

- [DAC_RLPF.sch:29-30](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/DAC_ReconstructionFilter/DAC_RLPF.sch#L29-L30)：两个 `Vfile` 源加载 `multi_ramp_stairs.csv`，模式 `"hold"`——即对样本做零阶保持，正是上一步 Python 生成的阶梯波，分别驱动差分的 `Vin_P`/`Vin_N`。
- [DAC_RLPF.sch:18-22](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/DAC_ReconstructionFilter/DAC_RLPF.sch#L18-L22)：差分 π 型低通的元件——串联电感 `L3=L4=107.3nH`、并联电容 `C4=32.79pF`、`C5=106.1pF`、`C6=32.79pF`，源端各串 25 Ω（`R2/R3`）配 50 Ω 差分阻抗。
- [DAC_RLPF.sch:17](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/DAC_ReconstructionFilter/DAC_RLPF.sch#L17)：`.TR` 瞬态仿真，线性扫描 0~10 µs、5020 个点。
- [DAC_RLPF.sch:65](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/DAC_ReconstructionFilter/DAC_RLPF.sch#L65)：标注 `low-pass filter, 60MHz cutoff, 5th order Butterworth, PI-type, impedance 50Ω`。60 MHz 截止远高于 chirp 的 30 MHz 上沿，保证 chirp 在通带内几乎无衰减，同时压掉 125 MHz 采样镜像。

#### 4.2.4 代码实践

**实践目标**：亲手生成一段 chirp，并把它接到重建滤波器仿真里。

1. 进 `5_Simulations/DAC_ReconstructionFilter/`，运行 `python Generate_ChirpcsvFile.py`（需 `numpy`、`pandas`、`matplotlib`）。它会弹出阶梯波图，并写出 `multi_ramp_stairs.csv`。
2. 用编辑器打开生成的 CSV，确认时间步是 8 ns（=1/125 MHz），电压在 0~2 之间。
3. 改 `fmax=50e6`（把上沿抬到 50 MHz，越过 60 MHz 截止的接近区）重新生成，对比波形。
4. 若装有 QucsStudio：打开 `DAC_RLPF.sch`，跑 `.TR` 瞬态仿真，在 `Vout_P`/`Vout_N` 节点观察输出是否比输入更平滑（高频阶梯被滤掉）。**无 QucsStudio 则只做步骤 1–3，跳过仿真，标注「待本地验证」**。

**需要观察的现象**：原始 CSV 是阶梯状（DAC ZOH）；`fmax=30MHz` 时重建滤波器输出应接近光滑正弦扫频；`fmax=50MHz` 时因接近 60 MHz 截止，输出幅度会出现可见衰减与相位畸变——这正是「为什么重建滤波器截止频率要留余量」的直观证据。

#### 4.2.5 小练习与答案

**练习 1**：为什么 DAC 之后必须加重建低通，不能直接把 DAC 阶梯波送进混频器？
**答**：阶梯波含大量采样率整数倍处的高频镜像，混频器会把它们一起搬到射频频谱上形成杂散与虚假信号，违反发射频谱模板。重建低通先把这些镜像压掉。

**练习 2**：`Generate_ChirpcsvFile.py` 默认 `hold_per_sample=1`（不阶梯化），但 `DAC_RLPF.sch` 的 `Vfile` 用 `"hold"` 模式。这两处的「阶梯化」是一回事吗？
**答**：效果等价但分工不同。`hold_per_sample>1` 是在 Python 侧把阶梯直接写进 CSV 增加点数；`Vfile` 的 `"hold"` 是仿真器在读 CSV 时自动对样本做零阶保持。两者都模拟 DAC 的 ZOH，二选一即可。当前 `.sch` 依赖仿真器侧的 `hold`，所以 Python 即便输出 `hold_per_sample=1` 的稀疏样本也能正确驱动。

---

### 4.3 GaN 功放（QPA2962）

#### 4.3.1 概念说明

AERIS-10N（Nexus，3 km）每通道约 1 W 发射功率；AERIS-10E（Extended，20 km）要把探测距离提到 6 倍以上，按雷达方程 \(R_{max}\propto P_t^{1/4}\)，需要把功率大幅提高，于是每路末级改用 **QPA2962 GaN HEMT 功放**做到 10 W（README 明确：「16x Power Amplifier Boards - Used only for AERIS-10E version, featuring 10Watt QPA2962 GaN amplifier for extended range」）。GaN（氮化镓）相比 GaAs / Si 能在更高电压、更高温度下输出大功率，且击穿电压高、寄生电容小，是 10.5 GHz 大功率首选工艺。

这一级不是用晶体管方程自己建模，而是直接用厂商标定的 **`.s2p` S 参数文件**做小信号线性仿真，确认它在工作频段（2–20 GHz，覆盖 10.5 GHz）的增益与匹配是否符合数据手册。

#### 4.3.2 核心流程

QPA2962 仿真的流程极简：

1. 把厂商 `.s2p`（`QPA2962_SN63_22v1680ma_85C.s2p`，即 22 V 漏压、1680 mA 静态电流、85 ℃）挂到 `SPfile` 元件上。
2. 两端各加 50 Ω 端口 P1/P2。
3. `.SP` 线性扫频 2–20 GHz、100 点。
4. 读 `dB(S[2,1])` 看小信号增益、`dB(S[1,1])`/`dB(S[2,2])` 看输入/输出匹配。

注意 **`.s2p` 是小信号 S 参数**，描述的是该静态偏置点（22 V / 1680 mA）附近的线性增益，**不能**预测大信号下的饱和输出功率（Psat）、1 dB 压缩点（P1dB）或效率——那些需要负载牵引（load-pull）或 X 参数模型。本仿真解决的是「这块芯片在 10.5 GHz 是否有足够的小信号增益且端口匹配良好」。

#### 4.3.3 源码精读

- [QPA2962.sch:17](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/QPA2962.sch#L17)：`SPfile X1` 加载 `QPA2962_SN63_22v1680ma_85C.s2p`，声明 2 端口——注意文件名编码了偏置条件（22 V / 1680 mA / 85 ℃），换偏置点要换文件。
- [QPA2962.sch:19-20](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/QPA2962.sch#L19-L20)：输入/输出端口 `P1`/`P2`，均 50 Ω、0 dBm（小信号激励，符合 S 参数线性前提）。
- [QPA2962.sch:23](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/QPA2962.sch#L23)：`.SP` 线性扫频 `lin 2GHz 20GHz 100`，覆盖整个 X / Ku 段，10.5 GHz 落在区间中段。

> 仿真结果如何指导 Extended 版功放设计：在 10.5 GHz 处读 `dB(S[2,1])` 得到小信号增益，确认它大于系统链路预算要求的值；读 `S[1,1]`/`S[2,2]` 在史密斯圆图上的位置，决定输入/输出匹配网络（微带匹配枝节）的拓扑与元件值；多个偏置点的 `.s2p` 对比还能在线性度与功耗之间选静态工作点。最终的 10 W 饱和功率仍需在实物板 + 大信号测试台上验证，仿真的作用是「在投板前把匹配网络调到合理起点」。

#### 4.3.4 代码实践

**实践目标**：理解 `.s2p` 偏置编码与频率范围的关系。

1. 打开 `QPA2962.sch`，从第 17 行读出 `.s2p` 文件名，拆解 `22v1680ma85C` 各字段含义。
2. 确认 `.SP` 扫频范围（2–20 GHz）是否覆盖 10.5 GHz。
3. （可选）用文本编辑器打开该 `.s2p` 文件本身（若仓库含此 s2p），查看其头注释里给出的偏置、校准条件与频率列范围。

**预期结果**：文件名 `QPA2962_SN63_22v1680ma_85C.s2p` 表示 SN63 批次、22 V 漏源电压、1680 mA 静态漏电流、85 ℃ 壳温下的标定；扫频 2–20 GHz 包含 10.5 GHz。**`s2p` 是否在仓库中随附需本地确认（标注「待本地验证」）**——若缺失，则此 `.sch` 只是仿真框架，跑起来需要自行向厂商索取该 `.s2p`。

#### 4.3.5 小练习与答案

**练习**：为什么用 S 参数（`.s2p`）仿真 GaN 功放，无法直接得到「10 W 输出功率」这个结论？
**答**：S 参数是小信号线性模型，只描述固定偏置点附近的增益与匹配；输出功率饱和、P1dB、效率这些大信号非线性指标需要负载牵引或 X 参数。10 W 是数据手册在大信号台架上测出的，本仿真只能确认小信号增益与匹配是否到位。

---

### 4.4 via 隔离（Via Fencing）与 RF 开关

#### 4.4.1 概念说明

最后这一节把「不在主信号通路上、但决定整块板能不能用」的两类仿真放在一起：via fence 几何与 RF 开关 S 参数。

**Via fence**：AERIS-10E 有 16 路大功率 GaN 通道挤在一块板上，10.5 GHz 时通道间距与波长同量级（λ₀≈2.86 cm），若不隔离，相邻通道会经基板表面波、平行板模式严重串扰，破坏波束赋形。沿地平面边缘打一排密集接地过孔（via fence），给返回电流一条紧贴信号的低阻抗通路，把每路 RF 区域「围」起来。`Via_fencing.py` 是一张几何示意图，对比两种过孔焊盘尺寸对地平面边缘距离的影响。

**RF 开关**：发射/接收分时复用同一条射频通路（见 u2-l2「TR switch」），靠 SP3T（单刀三掷）开关切换。仓库用 Mini-Circuits **M3SWA2-34DR+**，其 S 参数以 `.s3p` 给出。`Impedance.sch` 仿真确认它在 10.5 GHz 的插损（导通一路）与隔离度（关断一路）是否达标。

#### 4.4.2 核心流程

via fence 示意图脚本流程：

1. 设定微带线宽 `line_width=0.204` mm、基板厚度 `substrate_height=0.102` mm、过孔钻径 `via_drill=0.20` mm。
2. 画一条横贯左右的橙色 RF 线。
3. 在 RF 线上下两侧、距线边 `polygon_offset=0.30` mm 处画虚线表示地平面（铜皮）边缘。
4. 沿地平面边缘摆放两排过孔：绿色「Case A」（焊盘 `via_pad_A=0.20` mm，最小焊盘）与红色「Case B」（焊盘 `via_pad_B=0.45` mm，稳健焊盘）对比。

RF 开关仿真流程：`SPfile` 加载 `.s3p`（3 端口），三个端口各 50 Ω，`.SP` 线性扫频 2–12 GHz，读 `S[2,1]`（导通插损）与其余 S 参数（隔离度）。

#### 4.4.3 源码精读

via fence 几何参数在 [Via_fencing.py:3-8](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/Fencing/Via_fencing.py#L3-L8)：`line_width=0.204`、`substrate_height=0.102`、`via_drill=0.20`、`via_pad_A=0.20`（最小焊盘）、`via_pad_B=0.45`（稳健焊盘）、`spacing_via_center_to_edge=0.50`。

RF 线绘制在 [Via_fencing.py:15-16](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/Fencing/Via_fencing.py#L15-L16)，地平面边缘距线边 `polygon_offset=0.30` mm（[Via_fencing.py:19](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/Fencing/Via_fencing.py#L19)）。两排过孔（Case A 绿 / Case B 红）在 [Via_fencing.py:27-43](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/Fencing/Via_fencing.py#L27-L43) 沿 `polygon_y1`/`polygon_y2` 两条边绘制。标题在 [Via_fencing.py:57](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/Fencing/Via_fencing.py#L57) 写明 `Via Fence Setup for 10.5 GHz Microstrip Line`。

> 注意第 59 行的 `plt.savefig("/mnt/data/via_fence_setup.png")` 是一个**硬编码的绝对路径**（指向某个沙箱目录），本地跑多半写不进去；仓库里实际入库的 PNG 是 `via_fence_setup_pitch.png` 与 `via_fence_setup_pitch_offset.png`（由同族脚本 `Via_fencing2.py` 生成）。这是「读真实代码、不信注释/旧路径」的典型例子——要么改成本地路径再运行，要么直接读 `Via_fencing2.py`。

RF 开关仿真在 `Impedance.sch`：

- [Impedance.sch:17](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/RF%20switch/Impedance.sch#L17)：`SPfile X1` 加载 `Sparameters/M3SWA2-34DR+_3.5V_RF1 ON_40GHz_Plus25DegC_UNIT1.s3p`——文件名编码了控制电平 3.5 V、RF1 导通状态、标定到 40 GHz、25 ℃。这是 3 端口（SP3T）器件，故用 `.s3p`。
- [Impedance.sch:18-20](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/RF%20switch/Impedance.sch#L18-L20)：三个端口 `P1`（公共端 RFC）、`P2`、`P3`（两掷 RF1/RF2），均 50 Ω。
- [Impedance.sch:25](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/5_Simulations/RF%20switch/Impedance.sch#L25)：`.SP` 线性扫频 `lin 2GHz 12GHz 100`，覆盖 10.5 GHz。
- 同目录 `Sparameters/` 下另有 3 个 `.s3p`，分别对应 RF1/RF2 导通 × 100 MHz/40 GHz 频段，用于交叉验证不同掷、不同频段下的插损与隔离度。

#### 4.4.4 代码实践

**实践目标**：把 via fence 示意图跑出来，并读懂开关 `.s3p` 的状态编码。

1. 进 `5_Simulations/Fencing/`，把 `Via_fencing.py` 第 59 行的 `"/mnt/data/via_fence_setup.png"` 改成相对路径如 `"via_fence_setup.png"`，运行 `python Via_fencing.py`（需 `matplotlib`）。
2. 观察输出图：橙色 RF 线居中，上下蓝色虚线是地平面边缘（距线边 0.30 mm），绿色小圈是 Case A 最小焊盘过孔、红色大圈是 Case B 稳健焊盘过孔。
3. 打开 `RF switch/Impedance.sch` 第 17 行，拆解 `.s3p` 文件名：`M3SWA2-34DR+` / `3.5V` / `RF1 ON` / `40GHz` / `Plus25DegC` / `UNIT1` 各字段含义。
4. 列出 `RF switch/Sparameters/` 目录下全部 `.s3p` 文件名，说明它们覆盖了哪些「掷 × 频段」组合。

**需要观察的现象**：Case A 焊盘（Ø0.20）小于线宽（0.204），Case B 焊盘（Ø0.45）明显更大。焊盘越大，与地平面的耦合电容越大、对 50 Ω阻抗的扰动越大，但机械可靠性更高、制造容差更宽——这是 RF 设计里「电气性能 vs 可制造性」的典型权衡。**第 1 步若因 matplotlib 后端无显示而失败，改用 `savefig` 输出 PNG 即可（标注「待本地验证」）**。

#### 4.4.5 小练习与答案

**练习 1**：via fence 为什么要尽量靠近信号线（`polygon_offset=0.30` mm 而不是 3 mm）？
**答**：返回电流希望走紧贴信号线的路径以最小化回路电感。地平面边缘越近、过孔墙越靠内，返回电流通路越短、回路电感越小，对高频信号的隔离与完整性越好；离远了就退化成普通地平面，隔离效果大打折扣。

**练习 2**：RF 开关为什么用 `.s3p` 而不是 `.s2p`？
**答**：M3SWA2-34DR+ 是 SP3T（3 端口）器件——一个公共端 RFC 加两个掷 RF1/RF2。要同时描述「导通一路的插损」和「关断另一路的隔离度」，必须用 3 端口 S 参数矩阵 `.s3p`；`.s2p` 只能描述单端对，无法表达第三个端口的耦合。

---

## 5. 综合实践

把本讲四个模块串成一条「发射链信号完整性」检查任务。

**背景**：假设你要为 AERIS-10E（Extended 版）复核发射模拟链路：FPGA → DAC → 重建滤波器 → 上变频 → RF 开关 → GaN 功放 → 天线，并保证 16 路之间不串扰。

**任务**：

1. 用 `Generate_ChirpcsvFile.py` 生成一段带宽 20 MHz（`fmin=10e6, fmax=30e6`）的 chirp CSV，确认它落在 DAC 重建滤波器（`DAC_RLPF.sch`，60 MHz 截止）的通带内。
2. 打开 `DAC_RLPF.sch`，确认它加载的 CSV 文件名与你生成的文件一致（`multi_ramp_stairs.csv`），并说明 `Vfile` 的 `"hold"` 模式如何把离散样本变成 DAC 阶梯波。
3. 沿链路向后，打开 `QPA2962.sch`，确认 GaN 功放仿真的频率范围（2–20 GHz）覆盖 10.5 GHz，并指出这份 `.s2p` 能 / 不能告诉你功放的哪些指标。
4. 打开 `RF switch/Impedance.sch`，说明 SP3T 开关在发射/接收分时复用中扮演的角色，以及为什么需要 `.s3p`。
5. 最后打开 `Via_fencing.py`，说明在 16 路并排的 Extended 版功放板上，via fence 如何防止邻道串扰；若第 59 行的硬编码保存路径导致脚本跑不起来，给出你的修正方案。

**交付物**：一张标注了上述五个仿真文件在发射链上位置的信号流框图，以及一段话总结「哪些指标可在仿真中确认、哪些必须等实物板」。

**预期结论**：重建滤波器与 IF/带通可由 QucsStudio 仿真确认频响；GaN 功放的小信号增益与匹配可仿真，但 10 W 饱和功率、隔离度实物表现、via fence 的真实串扰必须等投板后在频谱仪 / 网分上实测——仿真的价值是「把投板前的设计调到合理起点」。

## 6. 本讲小结

- 本讲这套电路仿真文件用的是 **QucsStudio**（`<QucsStudio Schematic 5.8>`），不是 LTspice；先认对工具，再读元件。
- **IF 带通**有两版：单端 4 阶 π 型（120–180 MHz）做 S 参数扫频，平衡 5 阶 T 型（110–170 MHz）做差分 + 瞬态仿真；二者都框定 120 MHz 中频。
- `Generate_ChirpcsvFile.py` 用 LFM 二次相位公式合成 10→30 MHz（带宽 20 MHz）chirp 并可阶梯化，作为 `DAC_RLPF.sch`（60 MHz 截止低通）的瞬态激励，验证 DAC 重建滤波器抹平阶梯镜像。
- 该 Python chirp 与 FPGA 内 `long_chirp_lut.mem`（u5-l1）是不同产物：前者是仿真激励，后者是硬件 BRAM 波形，二者共享 LFM 原理。
- **QPA2962 GaN 功放**只服务 AERIS-10E（10 W × 16），用厂商 `.s2p` 做小信号 S 参数仿真，能确认增益与匹配，但大信号 Psat/P1dB 需实物验证。
- **Via fence** 沿地平面边缘打接地过孔墙，给返回电流低阻抗通路、抑制基板模式，是 16 通道 10.5 GHz 板隔离的关键；RF 开关 M3SWA2-34DR+ 用 `.s3p` 描述 SP3T 的插损与隔离。

## 7. 下一步学习建议

- 这些仿真产出的元件值与拓扑，最终要落到实物 PCB 上。建议接着读 u13-l1《硬件设计文件导览》，看 RF 滤波器、GaN 功放、via fence 如何在 `4_Schematics and Boards Layout/` 的 `.sch`/`.brd` 与叠层文件里实现。
- 若想理解这些模拟信号在数字侧如何被处理，回到 u4-l1（DDC）与 u5-l1（chirp 发射机），把「DAC 重建滤波器输出 → 上变频 → 发射」与「接收 → IF BPF → ADC → DDC」两条链在 u2-l2 的全景图上对齐。
- 对 GaN 功放的偏置校准（把每路 Idq 闭环到目标值）感兴趣，读 u7-l3《ADAR1000 波束赋形与 Idq 校准》，那里讲 STM32 如何用 DAC5578 设栅压、ADS7830 读电流做开机闭环。
- 延伸仿真：仓库 `5_Simulations/` 下还有 `Stub_BPF*.sch`（枝节带通）、`Sim_BPF_Te_100um/`（基于 Gerber 的 3D 滤波器电磁仿真）等，可作为「从电路仿真跨入电磁全波仿真」的自习材料，呼应 u12-l1 的 openEMS 方法论。
