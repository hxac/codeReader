# rtsim：腔体与 RF 系统实时仿真

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 **rtsim 是什么、为什么用 Verilog 写一个「物理仿真器」**：它是一个可综合的、以真实采样率全速运转的腔体模型，用来给低电平 RF（LLRF）控制器当「被控对象」，让控制器在没有真实加速器的情况下也能被反复锤炼。
- 看懂 rtsim 的**模块层级**：从顶层 `rtsim` / `station`，到电磁侧 `cav_elec`→`cav_mode`，到机械侧 `cav_mech`→`resonator`，再到 `adc_em` 仿真 ADC。
- 解释 `cav_mode` 如何用 **CORDIC 旋转 + 复数乘法 + IIR 低通** 把一个腔体电磁模式等效成一个「旋到基带后的一阶低通」，以及 `resonator` 如何用**时分复用的状态空间传播器**模拟成百上千个机械本征模式。
- 理解 rtsim 为何**大量复用 dsp/cordic 子系统**里的 `cordicg_b22`、`complex_mul`、`dpram`、`reg_delay`。
- 亲手跑通 `make -C rtsim clean all checks rtsim.dat`，并能画出从「以太网/本地总线写寄存器 → 物理参数 → 腔体输出」的数据通路。

本讲属于专家层，承接 [u3-l1 CORDIC 核](u3-l1-cordic.md) 与 [u3-l2 混频器、DDS 与复数乘法](u3-l2-mixer-dds-complex-mul.md)。

## 2. 前置知识

### 2.1 为什么需要一个「腔体仿真器」

超导腔（superconducting RF cavity）是粒子加速器里给带电粒子束流加速的「谐振盒子」。LLRF（Low-Level RF，低电平射频）控制器的任务是：让腔体里的电磁场幅度和相位**精确锁定**在设定值，哪怕束流在不断抽取能量、机械结构在微微振动。

要调试这样一个控制器，最理想的是接一个**真实可复现的腔体**。但真实腔体昂贵、不可重复，于是 LBNL 的 Larry Doolittle 写了一个 `rtsim`：**用 Verilog 把腔体的电磁与机械物理过程建模出来，让它和控制器跑在同一片 FPGA 里、以同样的时钟节拍实时演算**。这样控制器「看见」的 ADC 波形，和真实腔体出来的几乎一样，而且每次仿真完全可复现。

> 关键词：**full-speed**（全速，按真实采样率跑，不是软件慢速模型）、**full-featured**（含电磁模式、机械模式、束流、ADC 噪声）、**plant**（控制理论里的「被控对象」）。

### 2.2 腔体物理的三个耦合部分

理解 rtsim 只需抓住三条物理线：

1. **电磁模式（cavity electrical modes）**：腔体不止在一个频率上共振。主模叫 π 模，附近还有 8π/9、7π/9 等。每个模式都是一个有固有带宽和谐振频率的谐振器。
2. **机械模式（mechanical modes / Lorentz-force detuning）**：腔壁在电磁压力（Lorentz 力 ∝ |E|²）下会微微形变，形变改变腔体几何尺寸，从而**微调电磁谐振频率**。这是一个反馈环：电磁场 → 机械位移 → 频率失谐 → 电磁场。这个效应在强场下会让腔体「失稳」，是 LLRF 设计的头号难题之一。
3. **束流加载（beam loading）**：束流穿过腔体时像「负载」一样抽取能量，表现为一个时变的反向驱动。
4. **传感链**：腔体侧耦合出三路信号——场探针（field）、前向波（forward）、反射波（reflect），各自经 ADC 数字化，带噪声和直流偏置。

### 2.3 几个工程约定（复用前几讲）

- **IQ 交织（IQ interleaving）**：仿真时钟跑在 **2× ADC 速率**（注释里写 188.6 MHz 仿真钟 / 94.3 MHz ADC），用一位 `iq` 信号区分当前拍是 I 还是 Q（高=I，低=Q）。复数乘法、CORDIC 都按这个节拍分时复用，**硬件量减半**。这是 u3-l2 讲过的 `complex_mul` 的接口约定。
- **CORDIC 当三角函数/旋转引擎**：u3-l1 讲过 `cordicg_bN`，这里固定用 `cordicg_b22`（DPW=22），`op=2'b00` 旋转模式，把一个幅度+相位变成 (cos, sin) 复相量。
- **newad.py 自动生成本地总线解码**：u2-l3 讲过 `(*external*)` magic 注释会让 `newad.py` 自动给端口分配 localbus 地址并生成解码器，这里 `phase_step`、`bw`、`prop_const` 等物理参数全是这么暴露给 Host 的。

### 2.4 一点数学：一阶 IIR 与状态空间

腔体电磁模式旋到基带后，就是一个一阶低通，差分方程形如：

\[
v[n] = a\,v[n-1] + (1-a)\,u[n]
\]

其中 \(a\) 靠近单位圆内侧（\( |a|\approx 1\)），决定时间常数；\(\arg(a)\) 决定失谐频率。`resonator.v` 更直接地传播**状态空间**：

\[
z\,v_o = a\,v_o + v_i
\]

\(v_o\) 是复数状态（实部+虚部），\(a\) 是复数极点。这些会在 4.2、4.3 详讲。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `rtsim/README.md` | 唯一权威说明：模块层级树、设计动机、为何机械谐振器要被多个腔体共享。 |
| `rtsim/rtsim.v` | **顶层**。生成全局 `start` 节拍，实例化 `station`（电磁+ADC）与 `cav_mech`（机械），把电磁反馈 `eig_drive` 与压电、噪声求和后送入机械谐振器，**闭合 Lorentz 失谐环**。 |
| `rtsim/station.v` | **单腔完整模块**。含功放压缩/带宽、`cav_elec`、3 路 `adc_em`、压电耦合、PRNG。`rtsim_tb` 实际例化的是 `rtsim`，`rtsim` 内部又包了 `station`。 |
| `rtsim/cav_elec.v` | **腔体电磁侧**。一个 `generate` 循环例化 N 个 `cav_mode`，每个模式各自读出机械位移对自己的频率扰动、算出本模式场与反射、并产出对机械系统的反馈驱动。 |
| `rtsim/cav_mode.v` | **单个电磁模式**。CORDIC 把驱动旋到腔体坐标系 → `complex_mul` → `lp_pair`（腔体即一阶低通）→ `pair_couple` 旋回 IF 输出 → `mag_square` 算 |E|² 反馈给机械。 |
| `rtsim/resonator.v` | **机械本征模式状态空间传播器**。时分复用：一套乘法器 + 一块 `dpram` 状态向量，逐模式轮转传播，2 拍/模式，最多 512 模式。 |
| `rtsim/cav_mech.v` | 包住 `resonator` + 噪声注入，对外提供 `mech_x`（机械状态）与 `eig_drive`（电磁反馈驱动）。 |
| `rtsim/adc_em.v` | **ADC 仿真器**：把理想场信号加上 AWGN 噪声、直流偏置、可调延迟，输出 16 位，模拟 LTC2175。 |
| `rtsim/Makefile` / `rules.mk` | 构建：依赖 cordic/dsp 目录，`newad.py` 生成本地总线，`param.py` 生成寄存器编程序列，`rtsim_test.py` 做数值校验。 |

## 4. 核心概念与源码讲解

### 4.1 rtsim / station 顶层：把一个完整腔体仿真器组装起来

#### 4.1.1 概念说明

`rtsim` 是「一个腔体」的完整可仿真模型。它做三件事：

1. **生成节拍**：用一个计数器 `mech_cnt` 周期性地拉高 `start`，给机械状态空间传播器（`resonator`）和各 `outer_prod`/`dot_prod` 提供「开始一轮」的同步脉冲。机械侧处理得比电磁侧慢很多，所以这个节拍是降速的。
2. **例化电磁+传感链**（`station`）：功放非线性（`a_compress`）→ 功放带宽（`amp_lp`）→ `cav_elec` → 三路 `adc_em`。
3. **闭合机械反馈环**：把腔体算出的「电磁驱动」`cav_eig_drive`，加上压电驱动 `piezo_eig_drive` 和机械噪声 `noise_eig_drive`，求和、饱和后喂给 `cav_mech`（机械谐振器）；机械谐振器吐出的位移 `mech_x` 再回流到 `cav_elec`，去微调每个电磁模式的频率。

这个「场 → |E|² → 机械 → 频率失谐 → 场」的闭环，就是 Lorentz 力失谐的完整模型，也是 rtsim 区别于「单纯一个 IIR 谐振器」的核心价值。

#### 4.1.2 核心流程

```
                ┌──────────── rtsim (顶层) ────────────┐
 mech_cnt ──► start (每 n_cycles=14 拍拉高一次)
                │                                       │
                ▼                                       ▼
          ┌─ station ──────────────────┐        ┌─ cav_mech ──────────┐
          │ a_compress → amp_lp        │        │  resonator (机械)    │
          │   → cav_elec ──► cav_eig_  │  eig_  │   ↑ drive            │
          │       drive                 │──drive─┤   ↓ position=mech_x │
          │   → adc_em ×3 (场/前向/反射)│        │  noise (PRNG)       │
          │ piezo_couple ─► piezo_eig_  │        └──────────────────────┘
          │       drive                 │               │ mech_x
          └─────────────────────────────┘               │
                  ▲                                     │
                  └───────────── mech_x 回流到 cav_elec ─┘
```

求和节点（在 `rtsim.v` 里）：

```
sum_eig_drive = cav_eig_drive + (piezo_eig_drive + noise_eig_drive)
eig_drive     = SAT(sum_eig_drive)        // 饱和到 18 位
mech_x        ← cav_mech.resonator(eig_drive)
mech_x        → station.cav_elec          // 去微调每个电磁模式的频率
```

#### 4.1.3 源码精读

顶层端口——注意它把腔体内部信号（18 位有符号场）和最终 ADC 输出（16 位）分开，并暴露 localbus 用于配置物理参数：[rtsim/rtsim.v:9-24](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/rtsim.v#L9-L24) 说明：`iq` 是 IQ 交织标志；`drive`/`piezo` 是仿真激励；`a_field/a_forward/a_reflect` 是三路仿 ADC 输出；`lb_*` 是本地总线配置口。

关键参数（编译期固定，README 解释了为何不让 Host 改 `n_mech_modes`：它与 `interp_span` 有耦合）：[rtsim/rtsim.v:36-45](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/rtsim.v#L36-L45)。`n_mech_modes=7`、`n_cycles=14`、`mode_count=3`（三个电磁模式）。

全局节拍发生器——`mech_cnt` 从 `n_cycles-1` 倒数到 0 循环，`start` 在归零拍拉高一拍；再用 `reg_delay` 派生出 `start_outer`（0 拍延迟，喂 `outer_prod`）和 `start_eig`（1 拍延迟，喂 `resonator`）：[rtsim/rtsim.v:55-64](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/rtsim.v#L55-L64)。

> `reg_delay` 是 dsp 子系统里复用的「带使能的移位寄存器延时线」，参数 `dw`（位宽）和 `len`（延时拍数），见 [dsp/reg_delay.v:5-13](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/reg_delay.v#L5-L13)。rtsim 全程靠它做流水线对齐。

例化 `station`（电磁+ADC 一整套）：[rtsim/rtsim.v:71-80](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/rtsim.v#L71-L80)。注意 `(* lb_automatic *)` 注释——这是 newad.py 的标记，让它递归下钻把这个实例的可配置端口接到本地总线。

例化 `cav_mech`（机械侧）：[rtsim/rtsim.v:85-92](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/rtsim.v#L85-L92)。`eig_drive` 进、`mech_x` 出、`noise_eig_drive` 是机械噪声。

闭合反馈环——三项驱动求和、饱和、打两拍寄存，并输出裁剪标志位：[rtsim/rtsim.v:94-109](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/rtsim.v#L94-L109)。宏 `SAT(x,old,new)` 是 rtsim 自己定义的饱和宏（高位符号位全同则不饱和，否则截到饱和值）；`UNIFORM` 判断高位是否全 0 或全 1，用于检测溢出裁剪。`clips` 把两个裁剪位（驱动求和溢出、谐振器溢出）汇总给 Host。

`station` 内部——功放链 + `cav_elec` + 三路 ADC：[rtsim/station.v:64-86](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/station.v#L64-L86) 是功放压缩与带宽低通；[rtsim/station.v:104-111](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/station.v#L104-L111) 是 `cav_elec` 例化；[rtsim/station.v:122-130](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/station.v#L122-L130) 是三路 `adc_em`，分别取 PRNG 的不同比特段当噪声源。

#### 4.1.4 代码实践

**目标**：跑通 rtsim 的单元测试，并验证你理解的「节拍→求和→反馈」数据通路。

**步骤**：

1. 进入仓库根目录，运行 `make -C rtsim checks`（等价于分别跑 `a_comp_check resonator_check cav_mode_check afilter_siso_check`）。每条都应打印 `PASS`。
2. 运行 `make -C rtsim rtsim_tb rtsim_in.dat` 生成顶层仿真可执行文件与寄存器编程序列。
3. 运行 `make -C rtsim rtsim.dat`。这个 target（见 [rtsim/Makefile:49-51](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/Makefile#L49-L51)）会先 `vvp rtsim_tb +pfile=rtsim.dat` 跑仿真、把三路 ADC 经 CIC 下变频成 IQ 写进 `rtsim.dat`，再跑 `python rtsim_test.py` 做数值校验。
4. 打开 `rtsim.v`，对照 4.1.2 的方框图，用笔把 `start`/`start_outer`/`start_eig`、`cav_eig_drive`、`piezo_eig_drive`、`noise_eig_drive`、`eig_drive`、`mech_x` 这几个信号在 `rtsim` 与 `station` 与 `cav_mech` 之间的连线画出来。

**需要观察的现象**：`rtsim_test.py` 校验仿真末态三路 IQ 模值（见 [rtsim/rtsim_test.py:38-43](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/rtsim_test.py#L38-L43)，期望 `|cav|=8739 ± 10`、`|fwd|=6714`、`|rfl|=6022`）。若 PASS，说明整个电磁-机械闭环在你的机器上数值自洽。

**预期结果**：终端打印 `PASS`。**待本地验证**（数值依赖本机工具链版本）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `n_mech_modes` 从 7 改大，`n_cycles` 会怎样？为什么 README 不建议把 `n_mech_modes` 做成 Host 可配？
**答案**：`n_cycles = n_mech_modes*2`，它直接决定 `start` 节拍周期与 `resonator` 时分复用的轮转长度，还和 `interp_span = ceil(log2(n_cycles))` 耦合，牵动多处位宽与流水线对齐，所以编译期固定、不暴露给 Host。

**练习 2**：`clips` 输出里两位分别监控什么溢出？
**答案**：见 [rtsim/rtsim.v:107-109](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/rtsim.v#L107-L109)，高位 `edrive_clip` 监控三项驱动求和 `sum_eig_drive` 的饱和裁剪，低位 `res_clip` 来自 `cav_mech.resonator` 的状态传播饱和。

---

### 4.2 cav_elec 与 cav_mode：腔体的电磁侧

#### 4.2.1 概念说明

`cav_elec` 代表「腔体的全部电磁行为」。一个真实腔体有好几个电磁模式（π 模及其邻居），所以 `cav_elec` 用一个 `generate for` 循环例化 `mode_count`（默认 3）个 `cav_mode`，每个 `cav_mode` 是**一个**电磁模式。

`cav_mode` 的精髓是一句话：**把腔体旋到它自己的固有坐标系（基带），它就退化成一个一阶 IIR 低通**。具体分四步：

1. 用 CORDIC 把外部驱动（HPA 输出 / 束流）从「实验室 IF 坐标」旋转到「腔体固有旋转坐标」。旋转角由两部分组成：本振相位 `lo_phase`（固定 IF）和机械失谐相位 `mech_phase`（随腔体形变缓慢漂移）。
2. 在固有坐标里，腔体就是个低通 `lp_pair`：带宽由参数 `bw` 决定。
3. 把低通输出（腔内场 `res`）用 `pair_couple` 旋回 IF 坐标，分成「场探针」和「反射波」两路耦合输出。
4. 用 `mag_square` 算 `|res|²`，这正是推动机械形变的 Lorentz 力，反馈给机械侧。

`cav_elec` 还在模式循环里干了件关键事：每个模式各自做一个 `dot_prod`，把**共享的**机械位移向量 `mech_x` 投影到自己那一行「灵敏度矩阵」上，得到**本模式**的频率扰动；并做一个 `outer_prod`，把本模式的 `|E|²` 投影成对机械本征模式的反馈驱动。所有模式的探针波、反射波、机械反馈各自累加。

#### 4.2.2 核心流程

`cav_mode` 单模式内部（IQ 交织，每两拍处理一个复数对）：

```
mech_freq ──► mech_phase 累加器（每 IQ 对加一次）──► mech_phase
drive_coupling/beam_mag ──► CORDIC(mech_phase) ──► (cos,sin) 复相量 mul_coef
drive × mul_coef  (complex_mul) ──► mul_result          # 旋到腔体坐标
mul_result + beam_drv ──► lp_pair(bw) ──► res           # 腔体=一阶低通
res ──► pair_couple(lo_phase - mech_phase) ──► probe_refl   # 旋回 IF，分两路耦合
res ──► mag_square ──► v_squared = |E|²                  # 反馈给机械
```

`cav_elec` 的 `generate` 循环体（每个模式）：

```
dot_prod(mech_x)        ──► d_result    # 机械位移 → 本模式频率扰动
cic_interp(d_result)    ──► m_fine_freq # 平滑
cav_freq(fine+coarse)   ──► m_freq      # 本模式最终失谐频率
cav_mode(... m_freq)    ──► m_probe_refl, v_squared
outer_prod(v_squared)   ──► m_eig_drive # 本模式 → 机械反馈
Σ m_probe_refl, Σ m_eig_drive           # 跨模式累加
```

数学上，`lp_pair` 实现的一阶低通（IQ 交织下 z⁻² ≈ 一个真实采样周期的 z⁻¹）：

\[
\text{state}[n] = \text{state}[n-1] + (\text{drive}[n] - \text{state}[n])\cdot bw + \text{beam}
\]

带宽上界 \(f_{clk}/(2\cdot 2^{\text{shift}}\cdot 2\pi)\)，默认 `shift=18` 时极窄（注释举例 188.6 MHz 下约 57.2 Hz 最大带宽，对应高 Q 超导腔）。

#### 4.2.3 源码精读

`cav_mode` 端口与「本模式内存映射」注释：[rtsim/cav_mode.v:41-64](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/cav_mode.v#L41-L64)。注意 `drive_coupling`、`beam_coupling`、`bw` 都标了 `(* external *)`，由 newad.py 自动接本地总线；`mech_freq` 是 28 位有符号（步长 0.022 Hz，范围 ±2.94 MHz，足以表示 8π/9、7π/9 模式）。

机械相位累加——每对 IQ 拍（`~iq` 时）累加一次 `mech_freq`，取高 19 位当相位：[rtsim/cav_mode.v:78-80](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/cav_mode.v#L78-L80)。

CORDIC 旋转——`iq` 拍喂 `drive_coupling`、`~iq` 拍喂 `beam_mag`，复用同一个 CORDIC（硬件减半的体现）；这是复用 u3-l1 讲的 cordicg：[rtsim/cav_mode.v:90-92](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/cav_mode.v#L90-L92)。

复数乘法把驱动旋进腔体坐标——复用 dsp 的 `complex_mul`（[dsp/complex_mul.v:27-42](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/complex_mul.v#L27-L42)），`gate_in=1'b1`、`iq` 控制 IQ 复用：[rtsim/cav_mode.v:106-107](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/cav_mode.v#L106-L107)。

腔体即一阶低通——`lp_pair`：[rtsim/cav_mode.v:118-120](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/cav_mode.v#L118-L120)。`lp_pair` 内部状态机见 [rtsim/lp_pair.v:33-39](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/lp_pair.v#L33-L39)，参数 `shift=18` 见 [rtsim/lp_pair.v:18-19](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/lp_pair.v#L18-L19)。

旋回 IF 并分两路耦合输出（场探针/反射）——`pair_couple`，相位 = `lo_phase - mech_phase`：[rtsim/cav_mode.v:124-133](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/cav_mode.v#L124-L133)。`pair_couple` 内部又一个 CORDIC + 复乘，见 [rtsim/pair_couple.v:38-65](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/pair_couple.v#L38-L65)。

算 |E|² 反馈——`mag_square` 做「平方 + (1+z⁻¹) 两拍平均」：[rtsim/cav_mode.v:135-139](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/cav_mode.v#L135-L139)，内部见 [rtsim/mag_square.v:20-26](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/mag_square.v#L20-L26)。

`cav_elec` 的 `generate` 模式循环——这是「多电磁模式」的核心，每个模式独立 dot/interp/freq/mode/outer，再跨模式累加：[rtsim/cav_elec.v:133-175](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/cav_elec.v#L133-L175)。注释里的 `(* lb_automatic, gvar="mode_n", gcnt=3 *)` 是 newad.py 的「generate 展开」标记，让 3 个模式的寄存器各占一段地址（station.v 头部注释列出了地址布局，mode0 在 16–23、mode1 在 24–31、mode2 在 32–39）。

累加器寄存与输出——`probe_refl_acc`/`eig_drive_acc` 用 `mode_ln=ceil(log2(mode_count))` 额外位宽防溢出，最后寄存一拍：[rtsim/cav_elec.v:178-184](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/cav_elec.v#L178-L184)。

本振 DDS——`ph_gacc` 是带门控、带可编程模数的相位累加器（u3-l2 `ph_acc` 的变体），LO 相位步长 7/33（每对 IQ 拍）：[rtsim/cav_elec.v:73-75](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/cav_elec.v#L73-L75)，内部见 [rtsim/ph_gacc.v:15-22](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/ph_gacc.v#L15-L22)。

`dot_prod`（机械位移→频率扰动）逐元素乘累加，9 拍后给出结果与 `strobe`：[rtsim/dot_prod.v:36-44](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/dot_prod.v#L36-L44)。`outer_prod`（标量→机械反馈向量）在 `start` 后 2 拍快照 `x`，保证整个输出向量来自同一个自洽的 `x` 值：[rtsim/outer_prod.v:32-48](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/outer_prod.v#L32-L48)。`cav_freq` 把细调频率与粗调频率合并：[rtsim/cav_freq.v:18-20](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/cav_freq.v#L18-L20)。

> 复用清单：本模块用到的 `cordicg_b22`（u3-l1）、`complex_mul`（u3-l2）、`dpram`（u3-l4）全部来自 dsp/cordic 目录，靠 [rtsim/rules.mk:6-8](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/rules.mk#L6-L8) 里 `VFLAGS += -y$(DSP_DIR) -y$(CORDIC_DIR)` 让 iverilog 按模块名搜到；`cav_mode_auto` 依赖 `cordicg_b22.v` 由 cordic 子目录的生成器现场生成。

#### 4.2.4 代码实践

**目标**：用现成的单模式测试台 `cav_mode_tb` 直观看到一个电磁模式的场建立过程，并理解 `bw`（带宽）参数的影响。

**步骤**：

1. 运行 `make -C rtsim cav_mode_check`。它会编译 `cav_mode_tb`，跑仿真生成 `cav_mode.dat`，再用 `cav_mode_check.py` 画波形图（需 matplotlib；若无则会跳过画图但数值校验仍跑）。
2. 阅读测试台激励：[rtsim/cav_mode_tb.v:35-40](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/cav_mode_tb.v#L35-L40)。注意 `drive` 在 `iq` 拍为 0、`~iq` 拍为 30000（即给 Q 分量加常值激励），到 `cc>1400` 撤掉；`mech_freq=2000000`（约 44 kHz 失谐）；`bw=100000`；为了加速仿真，例化时用 `shift=7` 而非默认 18（时间常数快 2048 倍）。
3. 把 `cav_mode_tb.v` 第 55 行的 `bw=100000` 改成 `bw=10000`（带宽缩小 10 倍），重新 `make -C rtsim cav_mode_check`。
4. 观察 `cav_mode_check.py` 输出的腔体发射波形（或 `cav_mode.dat` 数值列）：场建立/释放的时间常数应明显变长。

**需要观察的现象**：带宽越小，腔体场 `probe_refl` 的指数建立时间越长（高 Q 腔响应慢）。

**预期结果**：`cav_mode_check` 仍打印 `PASS`（它只校验波形形状一致性，不锁死时间常数值）。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`cav_mode` 里 CORDIC 的输入 `cordic_x` 在 `iq` 拍和 `~iq` 拍分别喂什么？为什么能共用一个 CORDIC？
**答案**：见 [rtsim/cav_mode.v:84-87](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/cav_mode.v#L84-L87)，`iq` 拍喂 `beam_mag`、`~iq` 拍喂 `drive_coupling`。因为 IQ 交织下，两拍才组成一个复数运算周期，束流耦合与驱动耦合是两个独立的实数幅度，正好分占两拍，共用一个 CORDIC 流水线。

**练习 2**：`cav_elec` 的累加器为什么比单个模式的输出多 `mode_ln=2` 位？
**答案**：3 个模式的有符号输出相加最多增长 ⌈log₂3⌉=2 位，多出的位宽防止跨模式累加溢出，见 [rtsim/cav_elec.v:118-124](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/cav_elec.v#L118-L124) 的注释。

---

### 4.3 resonator：机械本征模式的状态空间传播器

#### 4.3.1 概念说明

机械侧的物理是：腔壁有一组本征振动模式（mechanical eigenmodes），每个模式是一个二阶振子（有惯性、有弹性、有阻尼）。把它们写成状态空间，每个模式是一个**复数一阶递推**：

\[
z\,v_o = a\,v_o + v_i
\]

\(v_o\) 是复数状态（实部 + 虚部 = 36 位），\(a\) 是复数极点。极点到单位圆的距离决定阻尼，极点的幅角决定该机械模式的振动频率。`resonator.v` 文件头注释把更精确的形式写为：

\[
z\,v_o = v_o + (w_d\,v_o + v_i)\cdot 4^{(scale-9)}, \quad w_d = (a-1)\cdot 4^{(7-scale)}
\]

这里 `scale`（0–7）是个**二进制定标**旋钮，用移位而不是浮点来扩大动态范围，让同一套定点硬件能表示从快到慢的各种机械模式。

`resonator` 的真正巧思是**时分复用（time-multiplexing）**：它不为每个机械模式 instantiate 一个谐振器，而是**用一套乘法器 + 一块 `dpram` 状态向量，轮流处理所有模式，每个模式占 2 拍**。参数 `pcw=10` 意味着状态向量有 2¹⁰ 个位置，每个模式占 2 个位置，所以最多 2⁹=512 个模式；两次 `start` 脉冲之间最多 2^pcw 拍。这正是 README 里强调的「机械谐振器要能被多个腔体共享」的技术基础——一台机械「计算机」服务全局。

#### 4.3.2 核心流程

```
pc (程序计数器) ── 每 start 归零，否则 +1，轮转 0..2^pcw-1
                  iq = pc[0]            # 偶数拍处理实部、奇数拍处理虚部

读: dpram[pc]  ──► ab_out (旧状态 v_o)          # 36 位复数状态
读: prop_const[pc] ──► wd_out, scale            # Host 配置的极点与定标
复乘: complex_mul(ab_out, wd_out) ──► mul_result # 矩阵 [-d k; -k -d] · v_o
加:  mul_result + drive ──► foo_result          # 加上电磁/压电/噪声驱动 v_i
移位: foo_result <<< (2*scale) ──► shf_result   # 二进制定标
加:  ab_out(延迟) + shf_result ──► sum_result   # v_o + (a-1)v_o + v_i
饱和: SAT(sum_result) ──► sat_result            # 写回 dpram[pc_d]
输出: position = sat_result[35:18]              # 取实部高 18 位
```

注意读写地址错开：写地址是 `pc_d`（`pc` 延迟 11 拍，对齐到流水线末端），读地址是 `pc`，保证「读旧值—算—写新值」的循环正确。`drive` 信号必须在 `start` 后第 12、13 拍准时到达（注释约定），输出在驱动后 4 拍出现，且这套时序是**循环的**，可以跨越下一个 `start`。

#### 4.3.3 源码精读

模块端口与设计约束（`pcw=10`、最少 5 个模式、`start` 间隔不超过 2^pcw）：[rtsim/resonator.v:36-53](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/resonator.v#L36-L53)。状态空间数学定义见文件头注释 [rtsim/resonator.v:7-17](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/resonator.v#L7-L17)。

程序计数器与 IQ 分相：[rtsim/resonator.v:55-57](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/resonator.v#L55-L57)。读写地址错开用 `reg_delay` 延迟 `pc`：[rtsim/resonator.v:60-62](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/resonator.v#L60-L62)。

状态向量双口 RAM——`dpram` 复用自 dsp（[dsp/dpram.v:4-13](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/dpram.v#L4-L13)），A 口写（`pc_d`）、B 口读（`pc`），36 位宽、`pcw` 位地址：[rtsim/resonator.v:64-70](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/resonator.v#L64-L70)。

复乘传播状态 + 加驱动：[rtsim/resonator.v:88-96](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/resonator.v#L88-L96)。注释「matrix [-d k; -k -d]」说明 `complex_mul` 在这里被当成 2×2 实矩阵乘法用（复乘 (a+jb)(c+jd) 的实部/虚部正好是这个矩阵形式）。

二进制定标 case——`scale_d` 控制 `<<<` 移位量（0 到 14 位），扩大动态范围：[rtsim/resonator.v:103-113](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/resonator.v#L103-L113)。

旧状态 + 增量求和、饱和、写回、输出位置与裁剪标志：[rtsim/resonator.v:116-134](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/resonator.v#L116-L134)。

`cav_mech` 把 `resonator` 包起来，再加机械噪声（用 PRNG 做白噪声）：[rtsim/cav_mech.v:36-42](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/cav_mech.v#L36-L42) 是谐振器例化，[rtsim/cav_mech.v:56-64](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/cav_mech.v#L56-L64) 是噪声累加器。

#### 4.3.4 代码实践

**目标**：用 `resonator_tb` + `resonator_check.py` 验证状态空间传播器在数值上等价于 scipy 的黄金参考滤波器。

**步骤**：

1. 运行 `make -C rtsim resonator_check`。它先跑 `resonator_tb` 生成 `resonator.dat`（一个机械模式对阶跃驱动的响应），再跑 `resonator_check.py`。
2. 阅读测试台：[rtsim/resonator_tb.v:46-50](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/resonator_tb.v#L46-L50) 是阶跃驱动（在 `cc%14==13` 拍给 1000）；[rtsim/resonator_tb.v:67-70](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/resonator_tb.v#L67-L70) 直接往 `dpram` 与系数存储器里写初值，绕过本地总线。
3. 阅读 `resonator_check.py`：它把寄存器设定还原成抽象极点 \(a = 1 + \text{a\_reg}\cdot 2^{-17}\cdot 2^{-18}\cdot 4^{scale}\)，然后用 `scipy.signal.lfilter([1],[1,-a], drive)` 做黄金参考，比较两者轨迹：[rtsim/resonator_check.py:39-57](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/resonator_check.py#L39-L57)。

**需要观察的现象**：脚本会打印 `a`（极点）、`r`（从仿真轨迹实测的比值，应≈a）、`err`（仿真与 scipy 的标准差）；只要 `err<0.6` 就 `PASS`。

**预期结果**：`err` 远小于 0.6，打印 `PASS`，证明定点 Verilog 传播器与浮点 scipy 在该模式下数值一致。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `resonator` 用读写地址错开（`pc` 读、`pc_d` 写）而不是同一个地址？
**答案**：状态传播是「读旧状态 → 算新状态 → 写回」的循环，中间隔着十几拍流水线（复乘、定标、求和、饱和）。写地址必须延迟到流水线末端（`pc_d` 延迟 11 拍）才与正在写回的数据对齐，否则会写错位置或读到自己刚写的值。

**练习 2**：若想让机械侧支持最多 1024 个模式，该改哪个参数？会牵连什么？
**答案**：把 `pcw` 从 10 改到 11（状态向量翻倍），但要同步加宽 `prop_const_addr`/`k_out_addr` 等地址端口宽度、`start` 节拍周期 `n_cycles` 也要相应增大，且 Host 系数存储器变大——牵一发动全身，所以 README 选择编译期固定。

---

### 4.4 adc_em：ADC 仿真器（噪声 / 偏置 / 延迟）

#### 4.4.1 概念说明

真实 ADC 不是理想的。`adc_em` 给理想场信号加上三样东西，让仿真出的 ADC 码流更逼真：

1. **加性高斯白噪声（AWGN）**：模拟 ADC 的量化/热噪声。注释目标是仿 LTC2175——14 位、73 dB SNR。
2. **直流偏置（offset）**：模拟通道直流误差。
3. **可调延迟（del）**：模拟 ADC 输出相对采样时刻的流水线延迟。

巧妙之处在于 AWGN 的产生方式：不调用浮点正态分布，而是利用中心极限定理——**数 13 个随机比特里 1 的个数**，相邻两拍做差，得到近似零均值高斯。文件头注释详细推导了需要约 20+ 个随机比特才能凑出 73 dB SNR 对应的噪声方差。

#### 4.4.2 核心流程

```
rnd (13 bit/拍，来自 PRNG) ──► bit_cnt = popcount(rnd)     # 当前拍 1 的个数
bit_cnt_d (上一拍)            ──► awgn = bit_cnt - bit_cnt_d  # 差分≈高斯
sum = in + (awgn << 3) + offset                              # 加噪声+偏置
sum_trunc = sum[18:4]                                        # 截位到 15 位
sat = SAT(sum_trunc)                                         # 饱和到 14 位
dval = reg_delay(sat, len=del, gate=strobe)                  # 可调延迟
adc = {dval, 2'b0}                                           # 左移2位补到16位接口
```

`strobe` 即 `iq`：ADC 每对 IQ 拍采样一次（每真实样本两拍），所以 `adc_em` 设计成「双时钟」工作，每拍吃 13 个随机比特。

#### 4.4.3 源码精读

模块端口与 SNR 推导注释：[rtsim/adc_em.v:2-32](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/adc_em.v#L2-L32)。`offset` 标 `(* external *)` 由 Host 配置；`rnd` 来自 PRNG；`del` 是延迟参数。

AWGN 产生——`bit_cnt` 数 13 位里 1 的个数（实现 popcount），`awgn = bit_cnt - bit_cnt_d`：[rtsim/adc_em.v:38-45](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/adc_em.v#L38-L45)。注释点出：当每个随机比特都公平时，`awgn` 均值严格为零；若 PRNG 停摆，AWGN 立即归零（不会注入直流）。

求和、截位、饱和：[rtsim/adc_em.v:48-53](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/adc_em.v#L48-L53)。`awgn <<< 3` 把噪声放大到目标方差。

可调延迟与 16 位接口填充：[rtsim/adc_em.v:57-61](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/adc_em.v#L57-L61)。注释说当前仿 14 位 ADC，但对外留 16 位接口「向前兼容」，所以末尾补两个 0。

三路 `adc_em` 在 `station` 里的实例化，分别喂场、前向、反射，并取 PRNG 不同比特段当噪声（避免三路噪声完全相关）：[rtsim/station.v:122-130](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/station.v#L122-L130)。

> PRNG 由 [rtsim/prng.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/prng.v) 提供，是两个 TT800 伪随机数发生器的外壳，`iva/ivb` 种子同样由 newad.py 暴露成 `plus-we`（写使能选通）型 Host 寄存器，所以仿真噪声序列可复现。

#### 4.4.4 代码实践

**目标**：理解 AWGN 的「数比特」法，并估算它产生的噪声有效值。

**步骤**：

1. 阅读 [rtsim/adc_em.v:38-45](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/adc_em.v#L38-L45) 与文件头 14-26 行的 SNR 推导。
2. 手算：13 个公平随机比特里 1 的个数 ~ 二项分布 B(13, 0.5)，方差 13/4=3.25；`awgn` 是相邻两拍之差，方差翻倍约 6.5，标准差约 2.55；再 `<<<3`（×8）后有效值约 20.4 个 ADC 量化单位。
3. 对照注释里的目标（73 dB SNR 对应 14 位满量程正弦的噪声有效值约 1.3 量化单位），思考这里的定标是否一致（注释提到「除以二」之类微调，实际工程值以仿真为准）。

**需要观察的现象**：纯靠组合 13 个随机比特的 popcount 差分，就能得到近似高斯分布，无需浮点。

**预期结果**：你应当能解释「为什么不直接用一位随机抖动」（一位抖动峰/rms 太差，注释里算出仅 2.45），以及「为什么 PRNG 停摆时噪声自动归零」。**待本地验证**（可用 cocotb 或在 tb 里把 `rnd` 固定，观察 `awgn` 归零）。

#### 4.4.5 小练习与答案

**练习 1**：`adc_em` 末尾 `assign adc = {dval, 2'b0};` 为什么左移两位？
**答案**：当前仿真的是 14 位 ADC，但 Bedrock 对外约定 16 位 ADC 接口（向前兼容更高分辨率器件），所以在 14 位有效码后补两个 0 凑成 16 位，见 [rtsim/adc_em.v:30-32](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/adc_em.v#L30-L32) 与输出行注释。

**练习 2**：三路 ADC 为什么分别用 `rnda[12:0]`、`rnda[25:13]`、`rndb[12:0]` 三段不同的随机比特？
**答案**：让三路 ADC 的噪声彼此不相关（否则场、前向、反射三路噪声会同步漂移，不真实），见 [rtsim/station.v:122-130](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/station.v#L122-L130)。

## 5. 综合实践

**任务**：跑通完整的 `rtsim` 顶层仿真，画出 `cav_elec → cav_mode` 的实例树，并解释「机械谐振器为何被设计成可被多个 `cav_elec` 共享」。

**步骤**：

1. 在仓库根目录执行：
   ```
   make -C rtsim clean all checks rtsim.dat
   ```
   `all` 编译所有测试台（[rtsim/Makefile:19](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/Makefile#L19) 列出了 `TEST_BENCH`），`checks` 跑数值校验（[rtsim/Makefile:26](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/Makefile#L26)），`rtsim.dat` 跑顶层 4 路仿真并校验末态（[rtsim/Makefile:44-51](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/Makefile#L44-L51)）。
2. 对照 README 的模块层级树（[rtsim/README.md:13-28](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/README.md#L13-L28)），画出 `cav_elec` 内部的实例树：
   ```
   cav_elec
   ├── ph_gacc            (LO DDS)
   ├── pair_couple drive_couple   (prompt 前/反射耦合)
   └── generate (mode_n = 0..2):  cav_mode[3]
         ├── dot_prod       (mech_x → 频率扰动)
         ├── cic_interp     (平滑)
         ├── cav_freq       (细+粗频率)
         ├── cav_mode mode  (CORDIC + complex_mul + lp_pair + pair_couple + mag_square)
         └── outer_prod     (|E|² → 机械反馈)
   ```
   注意 `cav_mode[3]` 是 `generate` 产生的三个独立实例（在 rtsim_tb 里通过 `v.station.cav_elec.cav_mode[0..2]` 路径访问，见 [rtsim/rtsim_tb.v:121-124](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/rtsim_tb.v#L121-L124)）。
3. 解释共享问题：阅读 [rtsim/README.md:44-49](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/rtsim/README.md#L44-L49)。要点应包括：
   - 机械谐振器（`resonator`）不属于电磁元件，所以不放在 `cav_elec` 内部，而是上提到 `cav_mech`/`rtsim` 层；
   - 它用**时分复用 + 共享状态向量**实现，天然适合「一台计算机服务多个腔体」；
   - 这能建模「单一机械模式耦合多个腔体」的场景（如 Jefferson Lab 观察到的 cavity fratricide「腔体相残」现象），对单源多腔架构的稳定性分析至关重要；
   - 每个 `cav_elec` 各自做 `dot_prod` 从共享的 `mech_x` 读出**对自己电磁模式**有效的频率扰动。

**验收**：所有 `checks` 与 `rtsim_test` 打印 `PASS`；你画出的实例树与源码 `generate` 循环一致；能用 3 句话说清「机械谐振器为何共享」。

## 6. 本讲小结

- **rtsim 是可综合的、全速运转的腔体物理仿真器**，作为 LLRF 控制器的可复现被控对象，把电磁模式、机械模式、束流、ADC 串成一个实时模型。
- **顶层 `rtsim` 闭合 Lorentz 失谐反馈环**：电磁场 → |E|² → 机械位移 → 频率失谐 → 电磁场；`station` 负责「功放+电磁+ADC」一整套，`cav_mech` 负责机械。
- **`cav_mode` 把腔体旋到固有坐标后退化成一阶 IIR 低通**（`lp_pair`），靠 CORDIC 旋转 + `complex_mul` 在 IF 坐标与腔体坐标间往返，`mag_square` 产出机械反馈。
- **`cav_elec` 用 `generate` 循环堆叠多个电磁模式**，每个模式用 `dot_prod` 从共享机械位移读出本模式频率扰动、用 `outer_prod` 把 |E|² 投回机械侧。
- **`resonator` 是时分复用的状态空间传播器**：一套乘法器 + 一块 `dpram` 状态向量轮流处理上百个机械模式（2 拍/模式），这是「机械侧可被多腔共享」的技术基础。
- **`adc_em` 用「13 比特 popcount 差分」近似高斯噪声**，加偏置与可调延迟，全程定点、可复现。
- **rtsim 大量复用 dsp/cordic 子系统**（`cordicg_b22`、`complex_mul`、`dpram`、`reg_delay`），并用 newad.py 自动生成本地总线寄存器映射、用 `param.py` 生成寄存器编程序列、用 `*_check.py` 做数值校验——这是 Bedrock「Python 辅助 + 全 Verilog 仿真」方法学的集中体现。

## 7. 下一步学习建议

- **下一讲 [u6-l3 cmoc：低电平 RF 控制器](u6-l3-cmoc-llrf.md)**：把 rtsim 当被控对象，看真正的 LLRF 控制器（`rf_controller`、`cryomodule`）如何挂上去闭环；重点看 `cryomodule.v` 如何用 `data_xdomain` 把本地总线跨到 `clk1x` 域（呼应 u4-l1）。
- 若想更扎实掌握本讲复用的原语，回看 [u3-l1 CORDIC](u3-l1-cordic.md) 与 [u3-l2 复数乘法](u3-l2-mixer-dds-complex-mul.md)，并阅读 [dsp/lp_pair 的近亲 `iirFilter`/`biquad`](../dsp/biquad.v) 对比 IIR 实现。
- 想深入物理建模可阅读 rtsim 目录下的 `doc/physics.tex`（`make -C rtsim physics.pdf`）与 `doc/block.eps`、`block_mode.eps` 框图。
- 对「多腔共享机械模式」感兴趣，可研究如何把 `rtsim` 扩展为多 `cav_elec` 共享一个 `cav_mech`（README 末段点明的方向）。
