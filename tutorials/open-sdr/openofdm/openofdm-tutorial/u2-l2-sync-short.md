# 短训练序列同步 sync_short.v

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `sync_short` 在 OFDM 解码流水线里的位置：它是紧跟在 `power_trigger`（[u2-l1](u2-l1-power-trigger.md)）之后的「第二道关」，负责用延迟自相关把短前导（STS）真正确认下来，并顺带估出一个粗频偏。
- 读懂延迟自相关度量 \( \mathrm{corr}[i] \) 的物理含义，以及 OpenOFDM 如何用「移位代替除法」把它改写成一次与 `0.75` 的比较。
- 掌握三个复数/统计原语：`complex_mult`（复数乘法）、`complex_to_mag_sq`（幅值平方）、`complex_to_mag`（幅值近似），以及滑动平均 `moving_avg` 的工作方式。
- 解释 `plateau_count`、`min_plateau` 与 `has_pos & has_neg` 这套「双重防误检」机制各自挡掉了什么假信号。
- 看清 `phase_offset` 是怎么从 64 点平均相关量里提取相位、再做「除以 16」下采样得到的。

本讲是「前端检测与同步」单元的第二篇，承接 [u2-l1 包检测 power_trigger.v](u2-l1-power-trigger.md)：在 `power_trigger` 用能量门放行之后，`sync_short` 接手做精确的短前导确认。

## 2. 前置知识

**短前导（STS）的周期性。** 802.11 OFDM 包以一段短前导开头：在 20 MSPS 采样率下，它由 10 段完全相同的、每段 16 个 IQ 样本组成，共 160 个样本、持续 8 µs。换句话说，「每隔 16 个样本，波形就重复一次」。这是 `sync_short` 能够检测它的物理基础。

**复数与共轭。** 一个 IQ 样本可以写成复数 \( S = I + jQ \)。它的共轭 \( \overline{S} = I - jQ \)。两个复数相乘 \( S_1\cdot\overline{S_2} \) 的结果还是一个复数，其**幅角（相位）等于两者的相位差**，**幅值等于两者幅值之积**。特别地，\( S\cdot\overline{S} = I^2+Q^2 \) 是一个实数，也就是该样本的功率。`sync_short` 大量用到这两个性质。

**延迟自相关。** 把当前样本 \( S[i] \) 与「16 个样本之前的」\( S[i-16] \) 做共轭乘 \( S[i]\cdot\overline{S[i-16]} \)。若信号每 16 样本重复，则两者几乎相等，乘积的幅值接近样本功率，相位接近 0；把一个窗口内的这种乘积累加再归一化，就得到一个介于 0～1 之间的「自相关度量」。它接近 1，就说明「信号正在每 16 样本重复一次」。

**为什么 `sync_short` 要走在 `power_trigger` 之后。** 上一讲已经讲过：静默段（恒定电平）同样「每 16 样本重复一次」，自相关也会接近 1。所以必须先用 `power_trigger` 的能量门把静默段挡掉，只在「有信号」的区段里再做自相关，`sync_short` 才不会误报。`sync_short` 自己也加了一层防误检（正负样本计数），下文会讲。

**滑动平均。** 自相关度量需要对一个窗口内的样本求和。硬件里用 `moving_avg` 模块实现：维护一个长度为 \( 2^{\text{WINDOW\_SHIFT}} \) 的环形缓冲，每来一个新样本，就把「新样本 − 窗口中最老的样本」累加进一个 `running_sum`，输出 `running_sum >> WINDOW_SHIFT`。这样不必每次都把窗口里所有样本重新相加，资源开销很小。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [verilog/sync_short.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v) | 本讲主角：延迟自相关 + plateau 计数 + 正负样本防误检 + 粗频偏估计。 |
| [verilog/complex_mult.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_mult.v) | 复数乘法原语：封装 Xilinx `complex_multiplier` IP，带 strobe 延时。 |
| [verilog/complex_to_mag_sq.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_to_mag_sq.v) | 幅值平方原语：用 \( S\cdot\overline{S} \) 算 \( I^2+Q^2 \)。 |
| [verilog/complex_to_mag.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_to_mag.v) | 幅值近似原语：\( \alpha{=}1,\beta{=}1/4 \) 的快速幅值估计。 |
| [verilog/moving_avg.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/moving_avg.v) | 滑动平均原语：基于 `ram_2port` 环形缓冲的增量式求和。 |
| [verilog/delay_sample.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/delay_sample.v) | 延时线：把样本延迟 \( 2^{\text{DELAY\_SHIFT}} \) 拍，提供自相关的「16 样本前」。 |
| [verilog/common_params.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v) | 定义 `SR_MIN_PLATEAU` 寄存器地址等常量。 |
| [docs/source/detection.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/detection.rst) | 检测原理文档：自相关公式与 0.75 门限的由来。 |
| [docs/source/freq_offset.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/freq_offset.rst) | 频偏文档：粗频偏 \( \alpha_{ST} \) 公式与「除以 16」的来源。 |
| [verilog/dot11.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) | 顶层：例化 `sync_short`、与 `equalizer` 共享 `phase` 模块、消费 `short_preamble_detected`。 |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：先讲延迟自相关的原理与整条数据通路（4.1），再分别精读三个复数/统计原语（4.2、4.3），然后进入 `sync_short` 的主判定逻辑——plateau 计数与正负样本防误检（4.4），最后讲粗频偏 `phase_offset` 的产生与除以 16（4.5）。

### 4.1 sync_short 总览：延迟自相关如何检出 STS

#### 4.1.1 概念说明

`sync_short` 要回答的问题是：「在 `power_trigger` 已经放行的信号段里，现在是不是真的短前导？」它的判据是短前导的周期性：计算一个延迟自相关度量，当它在一段连续样本里持续接近 1，就宣布「短前导到来」。

文档 [detection.rst:L59-L69](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/detection.rst#L59-L69) 给出的标准度量是：

\[
\mathrm{corr}[i] = \frac{\left\lvert\sum_{k=0}^{N} S[i+k]\cdot\overline{S[i+k+16]}\right\rvert}{\sum_{k=0}^{N} S[i+k]\cdot\overline{S[i+k]}}
\]

分子是「当前样本与 16 样本之后样本的共轭乘」在一个窗口内的累加幅值，分母是同一窗口内的功率累加。当信号每 16 样本重复时，分子≈分母，比值≈1。

#### 4.1.2 核心流程

直接按公式实现需要一次除法，而 FPGA 上除法很贵。OpenOFDM 的做法是：**固定门限 0.75**，把「\( \mathrm{corr} > 0.75 \)」改写成「分子 \( > 0.75 \times \) 分母」，两边都用移位算 0.75，从而完全避免除法（见 [detection.rst:L100-L106](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/detection.rst#L100-L106)）。

整条数据通路可以画成这样（对应 `sync_short.v` 里的实例化）：

```text
                 ┌──────────────┐
sample_in ──────▶│ delay_sample │── sample_delayed ──┐
                 │   延迟 16    │                    │ (取共轭)
                 └──────────────┘                    ▼
                          ┌──────────────────────────────────┐
sample_in ───────────────▶│ complex_mult (delay_prod_inst)   │── prod (S·conj(S_delayed))
                          └──────────────────────────────────┘
                                     │
            ┌────────────────────────┼────────────────────────┐
            ▼ (I/Q 各做 window=16 平均)                        ▼ (window=64 平均)
   moving_avg ×2  → prod_avg                          moving_avg ×2 → phase_in_i/q
            │                                                │
   complex_to_mag → delay_prod_avg_mag                      ▼ (送共享 phase 模块求 atan)
            │                                          phase_out
            ▼                                                │
sample_in → complex_to_mag_sq → mag_sq_avg ──┐              ▼
                                  prod_thres │ 0.75×      取反 + 除以16
                                  (分母)     │              │
            delay_prod_avg_mag > prod_thres ◀┘              ▼
                          │                          phase_offset (粗频偏)
                          ▼
            plateau_count++ / 正负样本计数
                          │  持续够久且 has_pos & has_neg
                          ▼
                short_preamble_detected
```

读图要点：

- **分子**（自相关幅值）= `delay_prod_avg_mag`，由 `prod`（样本与 16 样本前样本的共轭乘）经 window=16 平均、再取幅值得到。
- **分母**（功率）= `mag_sq_avg`，由每个样本的 \( I^2+Q^2 \) 经 window=16 平均得到。
- **门限** `prod_thres = 0.75 × mag_sq_avg`，于是「分子 > prod_thres」等价于「\( \mathrm{corr} > 0.75 \)」。
- 同一个 `prod` 还旁路一条 window=64 的平均支路，专门用于估计粗频偏 `phase_offset`。

#### 4.1.3 源码精读：端口与参数

模块端口见 [sync_short.v:L3-L25](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L3-L25)：输入是熟悉的 `sample_in[31:0]` + `sample_in_strobe`，输出有两个——一是单比特 `short_preamble_detected`，二是 32 位有符号 `phase_offset`（粗频偏，送给下游 `sync_long` 的旋转校正）。另外还有一组 `phase_in_i/phase_in_q/phase_in_stb` 与 `phase_out/phase_out_stb`，是与顶层共享的 `phase` 模块的对接端口（4.5 详述）。

两个关键 localparam 在 [sync_short.v:L28-L29](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L28-L29)：

```verilog
localparam WINDOW_SHIFT = 4;   // 检测用平均窗口 = 2^4 = 16
localparam DELAY_SHIFT  = 4;   // 延时线长度 = 2^4 = 16
```

`DELAY_SHIFT=4` 正好对应「16 样本重复周期」；`WINDOW_SHIFT=4` 给检测度量一个 16 点窗口。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：把「公式 → 硬件数据通路」的对齐关系建立起来。
2. **步骤**：对照上面的框图，在 [sync_short.v:L83-L188](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L83-L188) 里逐个找到 `mag_sq_inst`、`sample_delayed_inst`、`delay_prod_inst`、`delay_prod_avg_mag_inst`、`freq_offset_i/q_inst` 这些实例，确认它们各自对应框图的哪一支。
3. **观察**：注意 `delay_prod_inst`（[sync_short.v:L118-L132](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L118-L132)）的 `b_q` 接的是 `sample_delayed_conj[15:0]`——也就是延迟样本的 Q 路**取反**，这正是「共轭」的硬件实现。
4. **预期结果**：你能指着框图说出每个箭头对应源码里哪几行。

#### 4.1.5 小练习与答案

**练习 1**：为什么延时线长度（`DELAY_SHIFT=4` → 16）和检测平均窗口（`WINDOW_SHIFT=4` → 16）恰好都取 16？

**参考答案**：16 是短前导 STS 的重复周期。延时取 16，才能让 \( S[i] \) 与「上一个周期的同一相位」\( S[i-16] \) 对齐相乘，从而在周期信号上得到大幅值；窗口取 16（实际还会配合 plateau 计数）是为了在一个完整周期里把瞬时抖动平均掉，得到稳定的相关度量。

---

### 4.2 复数运算原语：complex_mult / complex_to_mag_sq / complex_to_mag

#### 4.2.1 概念说明

`sync_short` 的自相关离不开三件复数运算：复数乘法（算 \( S\cdot\overline{S_{\text{delayed}}} \)）、幅值平方（算功率 \( I^2+Q^2 \)）、幅值（算自相关矢量的模长）。OpenOFDM 把它们封装成三个可复用的小模块，全都遵循项目的「数据 + strobe」握手风格。

需要特别记住一个小技巧：**对一个补码数取相反数（用来做共轭的 Q 路取反、或相位取负）就是「按位取反再加一」`~x + 1`**。这个写法在本讲里会出现三次。

#### 4.2.2 核心流程

三个原语之间的关系：

```text
complex_mult        : (a_i,a_q) × (b_i,b_q) → (p_i, p_q)   通用复数乘
complex_to_mag_sq   : 内部调用 complex_mult，令 b = conj(a) → p_i = I²+Q²
complex_to_mag      : 用 |I|、|Q| 近似模长 → max + min/4
```

也就是说，`complex_to_mag_sq` 并不是独立实现，而是**复用 `complex_mult`**，把第二个操作数设成第一个的共轭，于是乘积的实部就是幅值平方。

#### 4.2.3 源码精读

**(1) complex_mult：复数乘法。** 它把真正的乘法交给 Xilinx 的 `complex_multiplier` IP，自己只做寄存与 strobe 对齐，见 [complex_mult.v:L29-L45](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_mult.v#L29-L45)：

```verilog
complex_multiplier mult_inst (
    .clk(clock), .ar(ar), .ai(ai), .br(br), .bi(bi),
    .pr(prod_i), .pi(prod_q)
);

delayT #(.DATA_WIDTH(1), .DELAY(5)) stb_delay_inst ( /* strobe 延时 5 拍对齐数据 */
    .data_in(input_strobe), .data_out(output_strobe)
);
```

always 块（[complex_mult.v:L47-L65](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_mult.v#L47-L65)）先把四个输入寄存一拍喂给 IP，再把 IP 输出寄存一拍得到 `p_i/p_q`；同时用一条 `delayT` 把 `input_strobe` 延时固定拍数（这里是 5），保证「数据有效的那一拍」`output_strobe` 才拉高。这种「数据走寄存器链、strobe 走等长延时线」是项目里所有流水线原语的标准做法。

**(2) complex_to_mag_sq：幅值平方 = 自共轭乘。** 见 [complex_to_mag_sq.v:L19-L32](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_to_mag_sq.v#L19-L32)，它实例化 `complex_mult`，令 `a = (i,q)`、`b = (i, q_neg)`：

```verilog
complex_mult mult_inst (
    .a_i(input_i), .a_q(input_q),
    .b_i(input_i), .b_q(input_q_neg),   // b 的 Q 路取反 → b = conj(a)
    ...
    .p_i(mag_sq)                        // (I+jQ)(I-jQ) = I²+Q²，实部即功率
);
```

其中 `input_q_neg <= ~q + 1`（[complex_to_mag_sq.v:L44](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_to_mag_sq.v#L44)）就是用补码取反实现共轭。因为 \( S\cdot\overline{S} \) 恒为实数，所以只取 `p_i` 作为 `mag_sq`，丢弃 `p_q`（理论上为 0）。

> 在 `sync_short` 里，`mag_sq_inst` 就是用它算每个输入样本的瞬时功率（[sync_short.v:L83-L94](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L83-L94)），作为分母 `mag_sq_avg` 的来源。

**(3) complex_to_mag：幅值近似。** 模长 \( \sqrt{I^2+Q^2} \) 需要开方，太贵。这里用了一个经典的快速估计（注释里给了出处 [complex_to_mag.v:L33-L35](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_to_mag.v#L33-L35)）：取 \( \alpha=1,\beta=1/4 \)，即

\[
|S| \approx \max(|I|,|Q|) + \tfrac{1}{4}\min(|I|,|Q|)
\]

对应代码 [complex_to_mag.v:L44-L50](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_to_mag.v#L44-L50)：

```verilog
abs_i <= i[DATA_WIDTH-1]? (~i+1): i;     // |I|，又是 ~x+1 取绝对值
abs_q <= q[DATA_WIDTH-1]? (~q+1): q;     // |Q|
max   <= abs_i > abs_q? abs_i: abs_q;
min   <= abs_i > abs_q? abs_q: abs_i;
mag   <= max + (min>>2);                 // α=1, β=1/4，min>>2 = min/4
```

文档说该近似平均误差约 0.6%，对「门限比较」这种粗判完全够用。在 `sync_short` 里，`delay_prod_avg_mag_inst` 用它算平均自相关矢量的模长（分子），见 [sync_short.v:L178-L188](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L178-L188)。

#### 4.2.4 代码实践

1. **目标**：验证「幅值平方 = 自共轭乘」与「幅值近似」的数值正确性。
2. **步骤**：在纸上取一个样本，比如 \( I=3, Q=4 \)。
   - 用 `complex_to_mag_sq` 的逻辑算：\( I^2+Q^2 = 9+16 = 25 \)。
   - 用 `complex_to_mag` 的逻辑算：\( \max(3,4) + \min(3,4)/4 = 4 + 0.75 = 4.75 \)，而真实模长 \( \sqrt{25}=5 \)，误差 5%。
3. **观察**：幅值近似在高斯整数（如 3-4-5）上误差略大，但平均误差很小；关键是它只需要比较、移位、加法，没有乘方与开方。
4. **预期结果**：你能解释为什么 `complex_to_mag_sq` 用 `p_i` 而忽略 `p_q`（因为自共轭乘积恒为实数），以及为什么幅值近似对门限比较足够。

#### 4.2.5 小练习与答案

**练习 1**：`complex_to_mag_sq` 为什么可以只输出 `p_i`（实部）而丢弃 `p_q`（虚部）？

**参考答案**：因为它计算的是 \( S\cdot\overline{S} = (I+jQ)(I-jQ) = I^2+Q^2 \)，结果恒为实数，虚部 \( IQ-QI=0 \)。所以 `p_q` 理论上恒为 0，只需取实部 `p_i` 作为功率。

**练习 2**：`complex_mult` 里为什么要把 `input_strobe` 用 `delayT` 延时 5 拍再作为 `output_strobe`？

**参考答案**：因为数据本身要走「输入寄存 → complex_multiplier IP → 输出寄存」这条多级流水线，从输入到有效输出有固定的若干拍延迟。`output_strobe` 必须与「数据真正到达输出端」的那一拍对齐，否则下游会读到无效数据。用一条等长的 `delayT(5)` 把 strobe 同步延后，是保证握手正确的最简单办法。

---

### 4.3 滑动平均 moving_avg 与延时线 delay_sample

#### 4.3.1 概念说明

自相关度量的分子分母都需要「在一个窗口内求和」。`moving_avg` 用增量式更新实现滑动窗口求和：维护一个长度为 \( W = 2^{\text{WINDOW\_SHIFT}} \) 的环形缓冲（基于双口 RAM），每来一个新样本 \( x_{\text{new}} \)，就把窗口里最老的样本 \( x_{\text{old}} \) 弹出、新样本压入，并把累加和更新为

\[
\text{running\_sum} \leftarrow \text{running\_sum} + x_{\text{new}} - x_{\text{old}}
\]

输出取 `running_sum >> WINDOW_SHIFT`（即除以 \( W \)，得到窗口平均）。这种「加新减旧」的做法每个样本只需一次加、一次减，远比「每次把整个窗口重加一遍」省资源。

`delay_sample` 是同源的更简单模块：它也用双口 RAM 做环形缓冲，但只输出「\( 2^{\text{DELAY\_SHIFT}} \) 拍之前的样本」，不做求和——正好给自相关提供「16 样本前」的那个样本。

#### 4.3.2 核心流程

`moving_avg` 的工作节奏：

```text
每个 input_strobe 拍：
    读出 RAM[addr] 的老样本 old_data            (这一拍同时读)
    running_sum += new_data − old_data          (窗口满后；未满则只 += new_data)
    把 new_data 写回 RAM[addr]                   (覆盖最老样本)
    addr = (addr+1) mod W
    窗口首次填满后置 full=1，此后 output_strobe 跟随 input_strobe
    data_out <= running_sum >> WINDOW_SHIFT
```

关键细节：窗口**未填满**之前不产生有效输出（`output_strobe <= full`，[moving_avg.v:L71](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/moving_avg.v#L71)）；填满后才每来一个样本输出一次平均值。

#### 4.3.3 源码精读

参数与窗口规模在 [moving_avg.v:L19-L20](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/moving_avg.v#L19-L20)：

```verilog
localparam WINDOW_SIZE = 1<<WINDOW_SHIFT;          // 窗口长度
localparam SUM_WIDTH   = DATA_WIDTH + WINDOW_SHIFT; // 累加和加宽，防溢出
```

环形缓冲用双口 RAM 实现，A 口写新样本、B 口读老样本（[moving_avg.v:L34-L47](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/moving_avg.v#L34-L47)）。增量更新与 full 标志见 [moving_avg.v:L56-L71](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/moving_avg.v#L56-L71)：

```verilog
if (input_strobe) begin
    addr <= addr + 1;
    data_out <= running_sum[SUM_WIDTH-1:WINDOW_SHIFT];   // 即 running_sum >> WINDOW_SHIFT
    if (addr == WINDOW_SIZE-1) full <= 1;
    if (full)
        running_sum <= running_sum + ext_new_data - ext_old_data;  // 加新减旧
    else
        running_sum <= running_sum + ext_new_data;                 // 预热期只加不减
    output_strobe <= full;
end
```

> 注意 `ext_old_data` / `ext_new_data` 是把 DATA_WIDTH 位数据符号扩展到 SUM_WIDTH（[moving_avg.v:L27-L28](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/moving_avg.v#L27-L28)），保证带符号相减正确。

`delay_sample` 的环形缓冲逻辑几乎一样（[delay_sample.v:L27-L52](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/delay_sample.v#L27-L52)），区别是没有 `running_sum`，`data_out` 直接是 `RAM[addr]`（即恰好一个窗口前的样本）。`sync_short` 用 `DELAY_SHIFT=4` 把它配成 16 拍延时（[sync_short.v:L107-L116](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L107-L116)）。

#### 4.3.4 代码实践

1. **目标**：理解「预热期」与「满窗后」两种行为对 `sync_short` 的影响。
2. **步骤**：`sync_short` 里挂了 5 个 `moving_avg`：两个给 `mag_sq_avg`/`prod_avg`（`WINDOW_SHIFT=4`，窗口 16），两个给 `phase_in_i/q`（`WINDOW_SHIFT=6`，窗口 64）。请你在 [sync_short.v:L96-L176](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L96-L176) 里把这 5 个实例和它们的窗口大小一一列出来。
3. **观察**：窗口 64 的那两个 `moving_avg`（`freq_offset_i/q_inst`，[sync_short.v:L156-L176](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L156-L176)）在收到前 64 个有效样本之前 `phase_in_stb` 不会拉高，因此 `phase_offset` 在短前导刚开始的一小段里还是 0。
4. **预期结果**：你能说出「为什么粗频偏估计要比检测度量用更大的窗口」——检测只要快速响应（窗口 16），而频偏要估得准（窗口 64，对应文档里 \( N=64 \)）。

#### 4.3.5 小练习与答案

**练习 1**：`moving_avg` 为什么把累加和位宽从 `DATA_WIDTH` 加宽到 `DATA_WIDTH + WINDOW_SHIFT`？

**参考答案**：窗口里最多有 \( W = 2^{\text{WINDOW\_SHIFT}} \) 个样本相加，最坏情况下累加和的位宽会比单个样本多 `WINDOW_SHIFT` 位。不加宽就会溢出，导致平均值错误。

**练习 2**：`delay_sample` 与 `moving_avg` 都用 `ram_2port` 做环形缓冲，结构几乎相同。它们的本质区别是什么？

**参考答案**：`delay_sample` 只输出「窗口最老的那个样本」（纯延时），不维护累加和；`moving_avg` 在延时的基础上额外维护 `running_sum` 并输出 `running_sum >> WINDOW_SHIFT`（窗口平均）。可以把 `delay_sample` 理解成 `moving_avg` 去掉求和部分的「裸延时线」。

---

### 4.4 主判定逻辑：0.75 门限、plateau 计数与正负样本防误检

#### 4.4.1 概念说明

有了分子 `delay_prod_avg_mag` 和分母 `mag_sq_avg`，主判定逻辑要决定「什么时候拉高 `short_preamble_detected`」。它由三重条件叠加：

1. **自相关足够高**：`delay_prod_avg_mag > prod_thres`，其中 `prod_thres = 0.75 × mag_sq_avg`，等价于 \( \mathrm{corr} > 0.75 \)。
2. **持续足够久（plateau）**：上述条件必须连续满足超过 `min_plateau` 个样本，用 `plateau_count` 计数，避免单个尖峰误触发。
3. **既有正样本又有负样本**：在 plateau 期间，I 路必须同时出现过足够多的正值和负值（各 > 25%），用 `has_pos & has_neg` 卡掉「恒定非零电平」这种假信号。

第三条是 `sync_short` 自己的第二道防线，文档 [detection.rst:L116-L122](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/detection.rst#L116-L122) 明确说明：即使过了 `power_trigger` 的能量门，仍可能有「恒定非零」信号（自相关也高），所以再用正负样本计数把它们挡掉。25% 这个比例同样是为了「只用移位」（`min_plateau>>2`）。

#### 4.4.2 核心流程

主逻辑（每个 `delay_prod_avg_mag_stb` 拍推进一次）：

```text
on each delay_prod_avg_mag_stb:
    prod_thres = mag_sq_avg/2 + mag_sq_avg/4           # = 0.75 × 分母
    if delay_prod_avg_mag > prod_thres:                # corr > 0.75
        if sample_in[31]==1:  neg_count++              # I 为负
        else:                  pos_count++              # I 为正
        has_pos = pos_count > min_plateau/4             # 正样本占比 > 25%
        has_neg = neg_count > min_plateau/4             # 负样本占比 > 25%
        if plateau_count > min_plateau:                 # 已持续够久
            short_preamble_detected <= has_pos & has_neg
            phase_offset             <= phase_out_neg / 16   # 顺带锁存粗频偏
            复位 plateau_count / pos_count / neg_count
        else:
            plateau_count++
    else:                                              # corr 跌破 0.75
        复位 plateau_count / pos_count / neg_count
        short_preamble_detected <= 0
```

#### 4.4.3 源码精读

**门限 prod_thres = mag_sq_avg 的 1/2 + 1/4。** 见 [sync_short.v:L220](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L220)：

```verilog
prod_thres <= {1'b0, mag_sq_avg[31:1]} + {2'b0, mag_sq_avg[31:2]};
```

- `{1'b0, mag_sq_avg[31:1]}` = `mag_sq_avg >> 1` = 分母的 **1/2**；
- `{2'b0, mag_sq_avg[31:2]}` = `mag_sq_avg >> 2` = 分母的 **1/4**；
- 两者相加 = 分母的 \( \tfrac{3}{4} \) = `0.75 × mag_sq_avg`。

所以「`delay_prod_avg_mag > prod_thres`」（[sync_short.v:L223](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L223)）就是「分子 > 0.75×分母」，两边同除以分母即「\( \mathrm{corr} > 0.75 \)」。**这正是用两次移位替代一次除法**。

> 一个对照细节：文档 [detection.rst:L100-L106](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/detection.rst#L100-L106) 把同一件事说成「`numerator>>1 + numerator>>2` 与分母比较」，即把 0.75 乘在分子上；代码则把 0.75 乘在分母上得到 `prod_thres` 再与分子比较。两种写法代数等价（都是 \( \text{分子} > 0.75\times\text{分母} \)），代码选择把门限预先算好存在寄存器里，每个样本只比较一次。

**正负样本计数与 has_pos & has_neg。** 计数器声明见 [sync_short.v:L64-L72](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L64-L72)，注释直白地说明了目的：确保短前导同时含正、负 I 路，以免恒定功率段误报。判定逻辑见 [sync_short.v:L213-L216](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L213-L216)：

```verilog
min_pos <= min_plateau>>2;            // 25% 门限（移位实现）
min_neg <= min_plateau>>2;
has_pos <= pos_count > min_pos;       // 正样本是否够多
has_neg <= neg_count > min_neg;       // 负样本是否够多
```

I 路正负的判定用的是符号位 `sample_in[31]`，见 [sync_short.v:L224-L228](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L224-L228)：

```verilog
if (sample_in[31]) begin
    neg_count <= neg_count + 1;        // 符号位为 1 → I 为负
end else begin
    pos_count <= pos_count + 1;        // 符号位为 0 → I 为正
end
```

**plateau 计数与最终置位。** 见 [sync_short.v:L229-L244](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L229-L244)：

```verilog
if (plateau_count > min_plateau) begin
    plateau_count <= 0;
    pos_count <= 0;  neg_count <= 0;
    short_preamble_detected <= has_pos & has_neg;     // 三重条件的最后一关
    phase_offset <= {{4{phase_out_neg[31]}}, phase_out_neg[31:4]};  // 顺带锁粗频偏
end else begin
    plateau_count <= plateau_count + 1;
    short_preamble_detected <= 0;
end
```

注意几个细节：

- `min_plateau` 是唯一一个设置寄存器，默认值 100，挂载方式见 [sync_short.v:L78-L80](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L78-L80)，地址 `SR_MIN_PLATEAU=6`（[common_params.v:L22](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L22)）。
- 判定用严格大于 `plateau_count > min_plateau`，从 0 起算，所以实际需要略多于 `min_plateau` 个连续高相关样本才会触发（与上一讲 `power_trigger` 的解除窗口一样是 off-by-one 风格）。
- 一旦相关跌破门限（`else` 分支，[sync_short.v:L239-L243](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L239-L243)），所有计数器立刻清零——plateau 必须「连续」，断一次就得重来。
- `short_preamble_detected` 只在「成功置位那一拍」为 1，下一拍若无新高相关样本会被清 0（[sync_short.v:L245-L247](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L245-L247)），所以它是一个脉冲，顶层靠这个脉冲边沿推进状态机。

**顶层如何消费。** `dot11.v` 在 `S_SYNC_SHORT` 态轮询 `short_preamble_detected`，一旦为 1 就复位并使能 `sync_long`、转入 `S_SYNC_LONG`，见 [dot11.v:L488-L496](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L488-L496)。若在检测成功前 `power_trigger` 提前跌落，则退回 `S_WAIT_POWER_TRIGGER`（[dot11.v:L483-L486](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L483-L486)）。

#### 4.4.4 代码实践（本讲主实践：源码追踪型）

1. **目标**：把「`sample_in` → `short_preamble_detected`」的整条组合/时序逻辑链路走通，并亲笔写两段说明。
2. **步骤**：
   - 从 [sync_short.v:L83](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L83) 的 `mag_sq_inst` 和 [sync_short.v:L118](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L118) 的 `delay_prod_inst` 出发，分别追到 `mag_sq_avg`（分母）和 `delay_prod_avg_mag`（分子）。
   - 找到 [sync_short.v:L220](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L220) 的 `prod_thres` 赋值，以及 [sync_short.v:L223](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L223) 的比较、[sync_short.v:L233](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L233) 的置位。
3. **要写的两段说明（交付物）**：
   - **说明 A（prod_thres 为何取 1/2 + 1/4）**：因为短前导判据是归一化自相关 \( \mathrm{corr} > 0.75 \)，而 \( 0.75 = \tfrac12+\tfrac14 \)。为避免除法，把不等式 \( \text{分子}/\text{分母} > 0.75 \) 改写成 \( \text{分子} > 0.75\times\text{分母} \)，其中 \( 0.75\times\text{分母} \) 用两次右移（`>>1` 和 `>>2`）相加即可得到，这就是 `prod_thres`。代码把 0.75 乘在分母上、预先存成寄存器，每个样本只做一次比较。
   - **说明 B（has_pos & has_neg 的作用）**：这是用来挡「恒定非零电平」的第二道防线。恒定信号虽然自相关也接近 1，但它的 I 路要么恒正、要么恒负，`pos_count` 与 `neg_count` 必有一个为 0，于是 `has_pos & has_neg` 为假，拒绝置位。真实短前导围绕零振荡，正负样本都不少，两条不等式同时成立才放行。25% 的门限（`min_plateau>>2`）是为「只用移位」而选的。
4. **预期结果**：你能不看源码，对着框图复述出「分子、分母、0.75 门限、plateau、正负计数」这五者如何串成一次成功的 `short_preamble_detected` 脉冲。
5. **待本地验证**（可选）：把 `SR_MIN_PLATEAU` 的默认值 `.at_reset(100)` 临时改成 `.at_reset(10)`，`make simulate` 后在 `sim_out/short_preamble_detected.txt`（由测试台落盘）观察置位时刻是否明显提前——注意改完要还原。

#### 4.4.5 小练习与答案

**练习 1**：假设输入是一个恒定非零直流（I 恒为 +200，Q 恒为 0）。`sync_short` 会不会误报短前导？为什么？

**参考答案**：不会。这种信号每 16 样本完全重复，自相关确实接近 1，`delay_prod_avg_mag > prod_thres` 会成立，`plateau_count` 也会涨上去；但它的 I 路恒正，`neg_count` 始终为 0，`has_neg` 为假，于是 `short_preamble_detected <= has_pos & has_neg` 为 0。正负样本计数正是为这种情形设计的。

**练习 2**：为什么 `short_preamble_detected` 被设计成「只亮一拍的脉冲」，而不是「持续高电平」？

**参考答案**：因为顶层 `dot11.v` 用它做状态转移触发器——一旦检测到，就立刻转去 `S_SYNC_LONG` 并复位 `sync_short`（见 [dot11.v:L488-L496](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L488-L496)）。一个脉冲边沿足以触发这次转移；若做成持续高电平，反而需要在顶层额外做边沿检测，且容易在 `sync_short` 已经被复位后仍看到残留高电平，造成歧义。

---

### 4.5 phase_offset：粗频偏估计与除以 16 下采样

#### 4.5.1 概念说明

`sync_short` 在确认短前导的同时，还顺带估出一个**粗载波频偏（Coarse CFO）** `phase_offset`，供下游 `sync_long` 旋转校正样本。原理见 [freq_offset.rst:L52-L61](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/freq_offset.rst#L52-L61)：

\[
\alpha_{ST} = \frac{1}{16}\angle\!\left(\sum_{i=0}^{N-1}\overline{S[i]}\,S[i+16]\right)
\]

直觉是：相邻两个周期（相隔 16 样本）的相位差，等于 16 个样本上累积的 CFO；除以 16 就得到「每个样本」的相位增量 \( \alpha_{ST} \)。后续每个样本按 \( S'[m]=S[m]\,e^{-jm\alpha_{ST}} \) 旋转即可粗校正。

注意公式里那个 **1/16**——它在硬件里体现为一次「除以 16」的下采样。

#### 4.5.2 核心流程

`phase_offset` 的产生分四步：

```text
prod (S·conj(S_delayed))
   │
   ▼ moving_avg window=64 (freq_offset_i/q_inst)   —— 对应公式里 N=64 的求和
phase_in_i / phase_in_q                            (复数形式的平均相关量)
   │
   ▼ 共享 phase 模块 (顶层 dot11.v 里与 equalizer 分时复用)
phase_out = atan(phase_in_i, phase_in_q)           (求相位，定点查表)
   │
   ▼ 取反：phase_out_neg = ~phase_out + 1           (对齐旋转方向)
   ▼ 算术右移 4 位：phase_out_neg / 16              (公式里的 1/16)
phase_offset                                       (送给 sync_long)
```

其中「求相位 `atan`」由顶层共享的 `phase` 模块完成，`sync_short` 只负责把平均相关量送进去、把相位结果取回来再做 1/16 缩放。

#### 4.5.3 源码精读

**window=64 的平均支路。** `prod` 的 I/Q 分别送进窗口为 64（`WINDOW_SHIFT=6`）的两个 `moving_avg`，见 [sync_short.v:L156-L176](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L156-L176)：

```verilog
moving_avg #(.DATA_WIDTH(32), .WINDOW_SHIFT(6)) freq_offset_i_inst (
    .data_in(prod[63:32]), ..., .data_out(phase_in_i), .output_strobe(phase_in_stb)
);
moving_avg #(.DATA_WIDTH(32), .WINDOW_SHIFT(6)) freq_offset_q_inst (
    .data_in(prod[31:0]),  ..., .data_out(phase_in_q)
);
```

这条支路对应文档 [freq_offset.rst:L71-L73](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/freq_offset.rst#L71-L73) 所说的「`prod_avg` 再送一个 window=64 的 `moving_avg`」。注意它与检测用的 `prod_avg`（window=16）是**两条独立的平均支路**，共享同一个 `prod` 源。

**与顶层共享 phase 模块。** `sync_short` 不自己算 atan，而是通过 `phase_in_i/phase_in_q` 把平均相关量送给顶层，再把 `phase_out` 接回来。顶层 `dot11.v` 用状态做二选一：当 `state==S_SYNC_SHORT` 时把 `sync_short` 的 `phase_in_*` 接到共享 `phase` 模块，否则接 `equalizer` 的，见 [dot11.v:L133-L146](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L133-L146)：

```verilog
wire[31:0] phase_in_i = state == S_SYNC_SHORT? sync_short_phase_in_i: eq_phase_in_i;
...
assign sync_short_phase_out = phase_out;   // 共享 phase 的输出回送 sync_short
```

这种「一个 phase 模块、两个调用方分时复用」是为了省 FPGA 资源（详见后续 [u6-l2](u6-l2-resource-reuse.md)）。

**取反与除以 16。** 见 [sync_short.v:L218](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L218) 与 [sync_short.v:L234](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L234)：

```verilog
phase_out_neg <= ~phase_out + 1;                                          // 取反（补码）
...
phase_offset <= {{4{phase_out_neg[31]}}, phase_out_neg[31:4]};            // 算术右移 4 位 = /16
```

- `~phase_out + 1` 又一次用到补码取反，目的是把相位符号翻转，对齐下游 `rotate` 模块的旋转方向（校正公式里是 \( e^{-jm\alpha_{ST}} \)，带负号）。
- `{{4{phase_out_neg[31]}}, phase_out_neg[31:4]}` 是带符号的算术右移 4 位：高 4 位用符号位 `phase_out_neg[31]` 复制填充，低 28 位取原值的 `[31:4]`。结果就是 `phase_out_neg / 16`，且符号正确——这正是公式里的 \( \tfrac{1}{16} \)。
- `phase_offset` 只在 `short_preamble_detected` 置位那一拍被锁存（它与置位写在同一个 `if` 分支里），所以输出给下游的是一个稳定的、对应短前导末尾的粗频偏估计。

#### 4.5.4 代码实践

1. **目标**：把「prod → 平均 → atan → 取反 → /16 → phase_offset」这条支路与数学公式逐项对上。
2. **步骤**：对照上面的四步流程，分别在源码里找到 window=64 平均（[sync_short.v:L156-L176](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L156-L176)）、共享 phase 接线（[dot11.v:L133-L146](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L133-L146)）、取反（[sync_short.v:L218](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L218)）和除以 16（[sync_short.v:L234](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L234)）。
3. **观察**：注意 `phase_offset` 与 `short_preamble_detected` 在同一个 `if (plateau_count > min_plateau)` 分支里赋值——也就是说，**粗频偏是在确认短前导的同一拍、用此前 64 个样本平均出来的相位给出的**。
4. **预期结果**：你能解释公式里的三个量分别对应哪段硬件——\( N=64 \) 对应 `WINDOW_SHIFT=6`；\( \angle(\cdot) \) 对应共享 `phase` 模块；\( \tfrac{1}{16} \) 对应算术右移 4 位。
5. **待本地验证**：`phase` 模块本身的 atan 查表细节（`phase.v` / `atan_lut`）留到 [u2-l3 中心频偏估计与相位/旋转校正](u2-l3-frequency-offset-correction.md) 精读，本讲只需理解 `sync_short` 这一侧的「平均 + 取反 + /16」。

#### 4.5.5 小练习与答案

**练习 1**：公式里为什么要除以 16？这个 16 和延时线的 16 是同一个 16 吗？

**参考答案**：因为 \( \angle(\sum\overline{S[i]}S[i+16]) \) 表示的是「相隔 16 个样本」累积出来的相位差，即 16 个样本上累积的 CFO。要得到「每个样本」的相位增量 \( \alpha_{ST} \)（后续逐样本旋转用），就必须再除以 16。这个 16 与延时线的 16（`DELAY_SHIFT=4`）是同一个物理量——短前导的 16 样本重复周期。

**练习 2**：`phase_offset` 为什么用 `{{4{phase_out_neg[31]}}, phase_out_neg[31:4]}` 而不是直接 `phase_out_neg >> 4`？

**参考答案**：因为 `phase_out_neg` 是有符号数（补码），它的「除以 16」必须做**算术右移**——高位补符号位而非补 0。`{4{phase_out_neg[31]}}` 正是把符号位复制 4 份填到最高位，保证负数除以 16 后仍是正确的负数。直接写 `>> 4` 在 Verilog 里对有符号 `reg` 是否为算术移位取决于声明与工具，显式拼接符号位是最稳妥、最可移植的写法。

---

## 5. 综合实践

把本讲五个最小模块串起来，完成下面这个贯穿性任务：**在波形里把一次成功的短前导检测「五马分尸」成五个阶段，并用源码解释每个阶段对应哪段电路。**

1. **准备**：按 [u1-l2](u1-l2-environment-and-simulation.md) 跑通默认的 24Mbps dot11a 样本仿真，用 gtkwave 打开 `sim_out/dot11.vcd`，把 `sync_short_inst` 下的关键信号加入视图：`sample_in[31:16]`（I 路）、`mag_sq_avg`、`prod_avg[63:32]`/`prod_avg[31:0]`、`delay_prod_avg_mag`、`prod_thres`、`plateau_count`、`pos_count`、`neg_count`、`has_pos`、`has_neg`、`short_preamble_detected`、`phase_offset`。
2. **分段观察**（用游标对齐到 `short_preamble_detected` 首次拉高的那一拍，往回看）：
   - **阶段① 分母建立**：观察 `mag_sq_avg` 在短前导段稳定在一个正值（功率水平）。
   - **阶段② 分子建立**：观察 `delay_prod_avg_mag` 在短前导段爬升到接近 `mag_sq_avg`（因为 corr≈1）。
   - **阶段③ 门限比较**：确认 `prod_thres ≈ 0.75 × mag_sq_avg`，且 `delay_prod_avg_mag` 在短前导段持续高于 `prod_thres`。
   - **阶段④ plateau 累计**：观察 `plateau_count` 从 0 一路涨到超过 `min_plateau`(100)，期间 `pos_count` 与 `neg_count` 都在涨（短前导正负振荡）。
   - **阶段⑤ 置位与频偏锁存**：在 `plateau_count > min_plateau` 那一拍，`short_preamble_detected` 出现一个单拍脉冲，同时 `phase_offset` 锁存为一个非零值。
3. **验证 has_pos & has_neg**：在脉冲那一拍回看 `has_pos`、`has_neg` 是否都为 1；若把视图拉到包结束后的静默/恒定段，应能看到相关量虽可能偏高，但正负计数之一为 0，从而不会误置位。
4. **交付**：写一份带时间戳的「五阶段报告」，每阶段标注：观察到的信号、对应的源码行号、与公式/框图的对应关系。并附上你对本讲主实践里「说明 A（prod_thres = 1/2+1/4）」和「说明 B（has_pos & has_neg）」的两段文字解释。

> 说明：本综合实践需要本地有 iverilog + gtkwave 环境，并依赖 `sync_short_inst` 内部信号在 `$dumpvars` 后可见。不同 iverilog 版本的层级名可能略有差异，若找不到某信号，可在 gtkwave 里按信号名搜索（如 `plateau_count`）。

## 6. 本讲小结

- `sync_short` 是流水线第二道关：在 `power_trigger` 放行后，用延迟自相关确认短前导，并顺带估出粗频偏 `phase_offset`。
- 核心度量是归一化自相关 \( \mathrm{corr} \)，但为避免除法，OpenOFDM 固定 0.75 门限，改写成「分子 > \( \tfrac12+\tfrac14 \)×分母」的移位比较——这就是 `prod_thres` 取 `mag_sq_avg` 的 1/2+1/4 的原因。
- 三个复数原语：`complex_mult`（封装 Xilinx IP 的通用复数乘）、`complex_to_mag_sq`（用 \( S\cdot\overline{S} \) 算功率）、`complex_to_mag`（\( \alpha{=}1,\beta{=}1/4 \) 的快速幅值近似）。共轭/取负统一用补码 `~x+1`。
- `moving_avg` 用 `ram_2port` 环形缓冲做增量式「加新减旧」滑动平均；同源的 `delay_sample` 只做纯延时（16 拍），给自相关提供「16 样本前」。
- 主判定是三重条件叠加：`corr > 0.75`（门限）**且** 连续超过 `min_plateau` 个样本（plateau）**且** `has_pos & has_neg`（正负样本各 >25%，挡恒定电平）。成功置位是一个单拍脉冲，顶层靠它转入 `S_SYNC_LONG`。
- `phase_offset` 由同一 `prod` 经 window=64 平均、送共享 `phase` 模块求 atan、再取反并算术右移 4 位（除以 16）得到，对应公式 \( \alpha_{ST}=\tfrac{1}{16}\angle(\cdot) \)，在检测成功那一拍锁存。

## 7. 下一步学习建议

下一讲 [u2-l3 中心频偏估计与相位/旋转校正](u2-l3-frequency-offset-correction.md) 会接着本讲末尾的 `phase_offset` 往下走：精读本讲里「只用了、没展开」的 `phase` 模块（atan 查表）和 `rotate` 模块（按相位旋转复数样本），看清 `phase_offset` 是如何驱动样本旋转完成粗频偏校正的。建议在进入下一讲前：

- 务必完成本讲 4.4.4 的主实践，把「`sample_in` → `short_preamble_detected`」的链路和两段说明写下来。
- 重读 [detection.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/detection.rst) 的 `Short Preamble Detection` 节与 [freq_offset.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/freq_offset.rst) 的 `Coarse CFO Correction` 节，把 0.75 门限与 \( \alpha_{ST} \) 公式的来龙去脉吃透。
- 顺带浏览 [verilog/phase.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v) 与 [verilog/rotate.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v)，预习 `phase_offset` 的消费方。
