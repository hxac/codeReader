# 中心频偏估计与相位/旋转校正

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 **载波频率偏移（CFO）** 与 **采样频率偏移（SFO）** 的区别，以及它们各自在哪个域（时域 / 频域）造成相位旋转。
- 读懂 [`phase.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v) 如何用一个 `atan` 查找表 + 象限还原，把任意复数映射成定点相位。
- 读懂 [`rotate.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v) 如何按相位查 `rot_lut`（存 sin/cos）并做一次复数乘法，完成「旋转一个复数样本」。
- 在 [`dot11.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) 中追踪 `phase_offset` 从 `sync_short` 产生、传给 `sync_long`、再驱动 `rotate` 校正样本的完整数据通路，并解释 `phase` 模块在 `sync_short` 与 `equalizer` 之间的分时复用。

本讲承接 [u2-l2 短训练序列同步](u2-l2-sync-short.md)：在那里 `sync_short` 已经算出了归一化自相关 `prod` 并确认了短前导。本讲要回答的是：**确认前导之后，如何把收发两端本振不一致造成的整体频偏「拧回来」，让后面的 FFT / 信道估计能正常工作。**

## 2. 前置知识

在进入源码前，先用最直白的方式建立两个直觉。

### 2.1 什么是频率偏移（CFO / SFO）

理想的 OFDM 接收机里，收发两端的「时钟」应当完全一致。现实里它们总是有微小偏差，[`docs/source/freq_offset.rst`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/freq_offset.rst) 把这种偏差分成两类：

- **载波频率偏移（Carrier Frequency Offset, CFO）**：收发两端本振（LO）频率不一致造成。它的**症状是时域 I/Q 样本随时间发生相位旋转**——也就是说，每一个进来的复数样本，都被悄悄乘了一个 \(e^{j\omega m}\)（\(m\) 是样本序号）。
- **采样频率偏移（Sampling Frequency Offset, SFO）**：采样时钟频率不一致造成。它的**症状是 FFT 之后频域星座点发生相位旋转**，而且旋转量随子载波序号增大而增大。

一句话区分：**CFO 在时域捣乱，SFO 在频域捣乱。** 文档里给了一张直观的对照（16-QAM 星座图）：不做任何校正时星座点散成一团；只做粗 CFO 校正后稍微聚拢；再做细 CFO + 导频校正后星座点才回到整齐的网格上。

### 2.2 怎么校正：用前导和导频当「已知答案」

802.11 的前导是收发双方都已知的固定序列，因此可以把「接收到的」和「应有的」做对比，反推出频偏：

- **粗 CFO（Coarse）**：用短训练序列 STS。STS 每 16 个样本重复一次，所以相隔 16 个样本的两个样本，在没有频偏时应当几乎同相；它们的**相位差就是 16 个样本上累积的频偏**。公式见 [`freq_offset.rst` 第 54-61 行](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/freq_offset.rst#L54-L61)：

  \[
  \alpha_{ST} = \frac{1}{16}\angle\left(\sum_{i=0}^{N-1}\overline{S[i]}\,S[i+16]\right)
  \]

  这正是上一讲 `sync_short` 里 `prod`（延迟自相关）的来历。除以 16 是因为相位差是 16 个样本累积出来的，要还原成「每个样本」的频偏。

- **细 CFO（Fine）**：用长训练序列 LTS（两个 64 样本的重复）。公式为 \(\alpha_{LT}=\frac{1}{64}\angle(\sum \overline{S[i]}S[i+64])\)。文档第 91-92 行明确说明：**本项目省略了这一步**，原因是查找表的相位分辨率不够。这点很重要，后面讲 `equalizer` 时会看到它是用「每个 OFDM 符号的导频」来补这个缺口的。

- **SFO / 残余频偏**：用每个 OFDM 符号里 4 个已知导频子载波来跟踪，在 `equalizer` 里做。

### 2.3 旋转一个复数 = 乘上 \(e^{j\varphi}\)

复数 \(z = I + jQ\) 旋转角度 \(\varphi\)，就是乘以 \(e^{j\varphi}=\cos\varphi + j\sin\varphi\)：

\[
z' = z\cdot e^{j\varphi} = (I\cos\varphi - Q\sin\varphi) + j(I\sin\varphi + Q\cos\varphi)
\]

所以「旋转」在硬件里就是一次复数乘法。`phase` 模块负责求 \(\angle(\cdot)\)（即 \(\varphi\)），`rotate` 模块负责做这次复数乘法。本讲的核心就是这两个模块，以及它们如何被串起来。

### 2.4 定点表示：相位不是浮点数

硬件里没有浮点，相位用一个**定点整数**表示。约定见 [`common_defs.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_defs.v) 和 [`common_params.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v)：

- `ATAN_LUT_SCALE_SHIFT = 9`：相位放大 \(2^9=512\) 倍存成整数。于是 \(\pi\) 对应整数 \(\pi\times 512\approx 1608\)，正好是 [`common_params.v:6`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L6) 的 `localparam PI = 1608;`。后续的 `PI_2 = PI>>1`、`PI_4 = PI>>2`、`PI_3_4 = PI_2 + PI_4` 全部由它移位派生。
- 文件第 2 行有一句关键注释：**改这个 shift 必须同步改 `common_params.v` 里的 `PI` 定义**。这是定点设计里最常见的「牵一发动全身」。

> 记住这个换算：本讲里看到的所有「相位」都是「真实弧度 × 512」的整数。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [verilog/phase.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v) | 输入一个复数 \((I,Q)\)，输出它的定点相位 \(\angle(I+jQ)\)。用「取绝对值 + 求 min/max + 除法 + atan 查表 + 象限还原」实现。 |
| [verilog/rotate.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v) | 输入一个复数样本和一个相位，输出旋转后的复数样本。用「象限折叠 + rot_lut 取 sin/cos + 复数乘法」实现。 |
| [verilog/coregen/rot_lut.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/rot_lut.v) | Xilinx 双口 Block RAM，512 项、每项 32 位（高 16 位 cos、低 16 位 sin），存 \([0,\pi/4]\) 范围的旋转因子。由 `sync_long` 和 `equalizer` 双口共享。 |
| [verilog/common_defs.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_defs.v) | 定义 `ATAN_LUT_LEN_SHIFT`、`ATAN_LUT_SCALE_SHIFT`、`ROTATE_LUT_*` 等定点缩放常数。 |
| [verilog/common_params.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v) | 定义 `PI=1608` 等定点 π 常数。 |
| [verilog/dot11.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) | 顶层。把 `phase` 在 `sync_short`/`equalizer` 间分时复用，把 `rot_lut` 在 `sync_long`/`equalizer` 间双口共享，并把 `phase_offset` 从 `sync_short` 接到 `sync_long`。 |
| [docs/source/freq_offset.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/freq_offset.rst) | 官方频偏校正原理文档，含粗/细 CFO 与 SFO 的公式与星座图对照。 |

辅助理解（非本讲精读对象，但会引用到）：`sync_short.v`（产生 `phase_offset`）、`sync_long.v`（消费 `phase_offset` 驱动 `rotate`）、`equalizer.v`（第二处使用 `phase` 与 `rotate`）。

## 4. 核心概念与源码讲解

### 4.1 频偏校正的整体策略

#### 4.1.1 概念说明

OpenOFDM 把频偏校正拆成「时域一次粗校 + 频域逐符号细校」两段，分别落在两个模块：

1. **粗 CFO（时域）**：在 `sync_short` 里估出每样本频偏 \(\alpha_{ST}\)，在 `sync_long` 里对**每个进 FFT 的时域样本**乘 \(e^{-jm\alpha_{ST}}\)（\(m\) 是样本序号）。这一步在 FFT **之前**完成。
2. **残余相位 / SFO（频域）**：在 `equalizer` 里，对**每个解出来的 OFDM 符号**，用 4 个导频子载波估出该符号的公共相位误差，再 `rotate` 校正。这一步在 FFT **之后**、按子载波处理时完成。

注意文档明确说明细 CFO（基于 LTS 的 \(\alpha_{LT}\)）被省略，所以「时域粗校」实际上是项目里**唯一**的全局频偏估计，剩下精度问题交给 `equalizer` 的导频逐符号补。

#### 4.1.2 核心流程

```
sync_short:  prod(=conj(S[i-16])·S[i])  ──moving_avg(64)──►  (I,Q)
                                                          │
                           ┌──────────────────────────────┘
                           ▼  （phase 模块，分时复用）
                        ∠(·)  ──取反 /16──►  phase_offset = -α_ST   （每样本频偏）
                           │
                           ▼
sync_long:  对第 m 个时域样本，phase_correction = m · phase_offset
                           │
                           ▼  （rotate 模块 + rot_lut）
            raw样本 ──× e^{-j·phase_correction}──► 校正后样本 ──► FFT
```

关键点：`phase_offset` 是**每样本**的频偏（已经除以 16），而真正施加在第 \(m\) 个样本上的旋转量是 \(m\cdot\text{phase\_offset}\)——这个「累加」发生在 `sync_long` 里。

#### 4.1.3 源码精读：两段校正的落点

粗校在 `sync_long` 内部、FFT 之前。看 [`sync_long.v:158-199`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L158-L199)：先 `rotate` 再 `xfft`，说明旋转发生在时域、FFT 之前——这对应 [`freq_offset.rst:67-69`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/freq_offset.rst#L67-L69) 的校正公式 \(S'[m]=S[m]e^{-jm\alpha_{ST}}\)。

细校在 `equalizer` 内部、FFT 之后。看 [`equalizer.v:209-225`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L209-L225)：这里也有一个 `rotate`，但它的 `phase` 来自导频（`pilot_phase`），是对**频域子载波**做逐符号校正。

#### 4.1.4 代码实践

**实践目标**：在文档与代码之间建立对应关系。

1. 打开 [`docs/source/freq_offset.rst`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/freq_offset.rst)，定位「Coarse CFO Correction」与「Fine CFO Correction」两节。
2. 确认：粗 CFO 公式里的 \(\frac{1}{16}\) 对应代码里哪一处除法？（提示：见 4.4 节 `sync_short` 的 `phase_offset` 移位。）
3. 确认文档第 91-92 行说「细 CFO 被省略」，然后在仓库里搜索是否真的没有基于 LTS 的 64 样本延迟自相关（应当搜不到独立实现，验证文档说法）。

**预期结果**：你会发现项目确实只实现了粗 CFO + 导频细校，没有独立的 \(\alpha_{LT}\) 计算——这与文档一致。

#### 4.1.5 小练习与答案

- **练习**：为什么粗校必须放在 FFT 之前，而导频细校放在 FFT 之后？
  - **答案**：CFO 是时域上每个样本都在累积的相位旋转，必须在进 FFT 前逐样本拧回来，否则子载波间的正交性被破坏（产生 ICI）。而 SFO / 残余相位表现为 FFT 后星座点的旋转，是按子载波、按符号的现象，所以在频域用导频逐符号校正最直接。

---

### 4.2 phase 模块：把复数变成定点相位

#### 4.2.1 概念说明

`phase` 解决的问题：给定复数 \(z=I+jQ\)，求 \(\varphi=\angle z\in[-\pi,\pi)\)，并用定点整数（×512）表示。

直接对 \((I,Q)\) 做 `atan2` 在硬件里很贵。OpenOFDM 的思路是「**折叠到第一象限的 \([0,\pi/4]\) 小区间，查一张小表，再根据象限信息还原**」。这样只需要一张覆盖 \([0,\pi/4]\)、共 \(2^8=256\) 项的 `atan_lut`。

#### 4.2.2 核心流程

`phase` 内部是一条流水线（注释见 [`phase.v:43-61`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L43-L61)）：

1. **取绝对值**：`abs_i = |I|`，`abs_q = |Q|`。
2. **求 max/min 与象限标记**：比较 `abs_i` 和 `abs_q`，大者当 `max`，小者当 `min`。这样 \(\text{min}/\text{max}\in[0,1]\)，对应角度落在 \([0,\pi/4]\)。
   - 同时记录 3 位 `quadrant = {sign_I, sign_Q, swap}`，`swap=1` 表示 Q 比 I 大（做了交换）。
3. **除法**：`quotient = min / (max>>9)`，即 \(512\times\text{min}/\text{max}\)。取低 8 位当 LUT 地址。
4. **查表**：`atan_lut[addr]` 得到 \([0,\pi/4]\) 内的定点角度 `_phase`。
5. **象限还原**：用 `case(quadrant)` 把 `_phase` 映射回完整的 \([-\pi,\pi)\)。

为什么要给象限信息打 36 拍延迟（[`phase.v:77-83`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L77-L83)）？因为中间的 `divider` 要花 36 拍（见 [`divider.v:2`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/divider.v#L2) 的注释 `DELAY: 36 cycles`），象限必须和除法结果**同步到达**最后的 `case`。

#### 4.2.3 源码精读

端口声明里点明输出就是定点相位（[`phase.v:16-18`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L16-L18)）：注释 `[-pi, pi) scaled up by 512`，对应 `ATAN_LUT_SCALE_SHIFT=9`。

**第 1-2 拍：取绝对值 + 求 max/min/象限**（[`phase.v:100-116`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L100-L116)）。注意取负统一用补码 `~x+1`：

```verilog
abs_i <= in_i[DATA_WIDTH-1]? ~in_i+1: in_i;   // |I|
abs_q <= in_q[DATA_WIDTH-1]? ~in_q+1: in_q;   // |Q|
...
if (abs_i >= abs_q) begin
    quadrant <= {in_i_delay[DATA_WIDTH-1], in_q_delay[DATA_WIDTH-1], 1'b0};
    max <= abs_i; min <= abs_q;
end else begin
    quadrant <= {in_i_delay[DATA_WIDTH-1], in_q_delay[DATA_WIDTH-1], 1'b1};
    max <= abs_q; min <= abs_i;
end
```

**除法： divisor = max 右移 9 位**（[`phase.v:64-75`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L64-L75)）。`divisor = max[31:9]` 即 `max>>9`，于是 `quotient = min/(max>>9) ≈ 512·min/max ∈ [0,512]`。取低 8 位当地址（[`phase.v:37`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L37)）：`assign atan_addr = quotient[ATAN_LUT_LEN_SHIFT-1:0];`。

**查表 + 象限还原**（[`phase.v:85-127`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L85-L127)）。`_phase` 是查表得到的 \([0,\pi/4]\) 角度（零扩展成有符号，[`phase.v:38`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L38)）；`case` 用延迟 36 拍后的 `quadrant_delayed` 把它还原到 8 个子区间：

```verilog
case(quadrant_delayed)
    3'b000: phase <= _phase;            // [0, PI/4]
    3'b001: phase <= PI_2 - _phase;     // [PI/4, PI/2]
    3'b010: phase <= -_phase;           // [-PI/4, 0]
    3'b011: phase <= _phase - PI_2;     // [-PI/2, -PI/4]
    3'b100: phase <= PI - _phase;       // [3/4PI, PI]
    3'b101: phase <= PI_2 + _phase;     // [PI/2, 3/4PI]
    3'b110: phase <= _phase - PI;       // [-3/4PI, -PI]
    3'b111: phase <= -PI_2 - _phase;    // [-PI/2, -3/4PI]
endcase
```

每个 case 项旁边的注释就是该象限对应的角度区间。注意 `quadrant` 高 2 位是 \((sign_I, sign_Q)\)，低位是「是否交换了 I/Q」，所以 8 种组合恰好覆盖整个圆周。

#### 4.2.4 代码实践

**实践目标**：用源码逻辑验证 `phase` 在边界输入上的行为。

1. **纯实数正数输入**（\(I>0, Q=0\)）：走一遍流程，`abs_q=0`，`abs_i>=abs_q` 故 `max=abs_i, min=0`，`quadrant={0,0,0}=000`。`dividend=min=0`，所以 `quotient=0`，`atan_addr=0`，最终 `phase=_phase`（`case 000`）。结论：正实轴对应角度 0，符合直觉。
2. **纯虚数正输入**（\(I=0, Q>0\)）：`abs_i=0<abs_q`，走 else 分支，`max=abs_q, min=0`，`quadrant={0,0,1}=001`（swap=1）。同样 `min=0 → quotient=0 → _phase`，最终 `phase = PI_2 - _phase`（`case 001`）。结论：正虚轴对应角度 \(\pi/2\)，也符合直觉。这两例验证了「象限还原」是自洽的。
3. **思考题（留给 [u5-l4](u5-l4-lut-generators.md)）**：当 \(|I|=|Q|\)（45°，\(\pi/4\)）时，`min=max`，`quotient=512`，地址取低 8 位绕回到 0。请去 [`scripts/gen_atan_lut.py`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_atan_lut.py) 看 LUT 的 key/value 是怎么定义的，确认 addr=0 到底对应 0 还是 \(\pi/4\)——这正是 `atan_lut` 构造的细节，本讲先不展开。

**需要观察的现象 / 预期结果**：`phase` 输出落在区间 \([-\pi,\pi)\) 的定点表示（约 \([-1608, 1608)\)）内；正实轴输入应给出接近 0 的输出，正虚轴输入应给出接近 `PI_2=804` 的输出。这一步**待本地验证**：可在 `dot11_tb` 里给 `phase` 喂固定激励观察输出。

#### 4.2.5 小练习与答案

- **练习 1**：`phase` 模块为什么先把 \((I,Q)\) 折叠到 \([0,\pi/4]\) 再查表，而不是直接做 \(\arctan(Q/I)\)？
  - **答案**：直接 \(\arctan\) 要么要处理 \(I=0\) 的除零，要么要覆盖 \([0,2\pi)\) 整圈，LUT 会大很多。折叠到 \([0,\pi/4]\) 后，\(\text{min}/\text{max}\in[0,1]\)，只需一张覆盖 \(\arctan([0,1])=[0,\pi/4]\) 的小表（256 项），象限由 3 位标记还原，面积最省。
- **练习 2**：`quadrant_inst` 为什么是 `DELAY=36`？
  - **答案**：要和 `divider` 的 36 拍延迟对齐，保证走到最后 `case(quadrant_delayed)` 时，象限标记和 `_phase`（来自除法 + 查表）是同一个输入样本产生的。

---

### 4.3 rotate 模块：按相位旋转一个复数样本

#### 4.3.1 概念说明

`rotate` 解决的问题：给定复数样本 \((I,Q)\) 和相位 \(\varphi\)，输出 \((I,Q)\cdot e^{j\varphi}\)。它在 `sync_long` 里用于「把含频偏的时域样本转正」，在 `equalizer` 里用于「把频域子载波转正」。

实现思路和 `phase` 完全对称：先把 \(\varphi\) 折叠到 \([0,\pi/4]\)，查 `rot_lut` 拿到该小角度的 \((\cos,\sin)\)，再用 3 位象限信息把 \((\cos,\sin)\) 还原成完整 \(\varphi\) 的 \((\cos,\sin)\)，最后做一次复数乘法。`rot_lut` 只存 \([0,\pi/4]\) 的旋转因子，省资源。

#### 4.3.2 核心流程

[`rotate.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v) 的流水线分 4 拍控制逻辑 + 一段 `complex_mult`：

1. **第 1 拍**（[`rotate.v:102-104`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v#L102-L104)）：`phase_abs = |phase|`，缓存原 `phase`（保留符号）。
2. **第 2 拍**（[`rotate.v:107-119`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v#L107-L119)）：用 `phase_abs` 与 `PI_4/PI_2/PI_3_4/PI` 比较，把相位折叠到 \([0,\pi/4]\) 得到 `actual_phase`，并生成 3 位 `quadrant = {sign, 2 位折叠标记}`。
3. **查表**：`rot_addr = actual_phase[8:0]`（[`rotate.v:48`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v#L48)），从 `rot_lut` 读出 32 位 `rot_data`，高 16 位是 cos、低 16 位是 sin（[`rotate.v:49-50`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v#L49-L50)）。
4. **第 3-4 拍**（[`rotate.v:123-159`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v#L123-L159)）：把 `quadrant` 延迟两拍对齐 LUT 读出，用 `case` 把 \([0,\pi/4]\) 的 \((\cos,\sin)\) 还原成完整 \(\varphi\) 的 \((\cos,\sin)\)（`rot_i, rot_q`）。
5. **复数乘法 + 重缩放**（[`rotate.v:70-82`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v#L70-L82) 与 [`rotate.v:45-46`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v#L45-L46)）：`complex_mult` 算 \((I,Q)\times(\text{rot\_i},\text{rot\_q})\)，再右移 `ROTATE_LUT_SCALE_SHIFT=11` 位把乘积缩放回 16 位。

输入样本与 strobe 各自延迟 4 拍（[`rotate.v:53-67`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v#L53-L67)），用来对齐第 4 拍才算好的 `rot_i/rot_q`。

#### 4.3.3 源码精读

**象限折叠**（[`rotate.v:107-119`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v#L107-L119)）：把任意 \(\varphi\) 映射成 `actual_phase`\(\in[0,\pi/4]\)，并记录怎么「折回去」：

```verilog
if (phase_abs <= PI_4) begin
    quadrant <= {phase_delayed[31], 2'b00};
    actual_phase <= phase_abs;
end else if (phase_abs <= PI_2) begin
    quadrant <= {phase_delayed[31], 2'b01};
    actual_phase <= PI_2 - phase_abs;
end else if (phase_abs <= PI_3_4) begin
    ...
end else begin
    quadrant <= {phase_delayed[31], 2'b11};
    actual_phase <= PI - phase_abs;
end
```

**象限还原**（[`rotate.v:126-159`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v#L126-L159)）：8 种 `quadrant` 对应 8 种 \((\cos,\sin)\) 的符号/交换组合。例如 `3'b010` 表示「负角、折叠到 \(\pi/2\) 附近」，需要 \((-\sin, \cos)\)：`rot_i <= ~raw_rot_q+1; rot_q <= raw_rot_i;`。

**复数乘法的重缩放**（[`rotate.v:45-46`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v#L45-L46)）：

```verilog
assign out_i = p_i[`ROTATE_LUT_SCALE_SHIFT+15:`ROTATE_LUT_SCALE_SHIFT];
assign out_q = p_q[`ROTATE_LUT_SCALE_SHIFT+15:`ROTATE_LUT_SCALE_SHIFT];
```

即取 32 位乘积的 `[26:11]` 共 16 位，等价于「乘积右移 11 位」。为什么是 11？因为 `rot_lut` 里的 sin/cos 已经放大了 \(2^{11}\)（`ROTATE_LUT_SCALE_SHIFT=11`，见 [`common_defs.v:6`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_defs.v#L6)），乘完必须缩回去，否则每旋转一次幅值就放大 2048 倍。

> 约定：`phase` 模块的相位放大 \(2^9\)（atan 精度），`rotate` 模块的 sin/cos 放大 \(2^{11}\)（旋转因子精度）。两个 shift 独立，但都由 `common_defs.v` 集中定义，改动时要分别核对。

#### 4.3.4 代码实践

**实践目标**：把 `rotate` 当成一个「黑盒复数乘法器」验证。

1. 在 [`sync_long.v:158-174`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L158-L174) 找到 `rotate_inst` 的例化：`in_i/in_q` 接 `raw_i/raw_q`（从输入缓冲读出的原始样本），`phase` 接 `phase_correction`，输出 `fft_in_re/fft_in_im` 直接喂给 `xfft`。确认「旋转 → FFT」的物理顺序。
2. 设想 `phase=0`（无频偏）：此时 `phase_abs=0`，`actual_phase=0`，`rot_lut[0]` 应当返回 \((\cos 0,\sin 0)=(2048,0)\)（放大 \(2^{11}\) 后），于是 \((I,Q)\times(2048,0)\) 再右移 11 位 ≈ \((I,Q)\)——即「不旋转」。这一步**待本地验证**：可在仿真里把 `phase_offset` 强制为 0，对比 `rotate` 输入输出是否近似相等。

**预期结果**：`phase=0` 时 `rotate` 近似恒等变换；`phase=PI_2`（90°）时，`phase_abs=PI_2` 命中折叠的第二个分支（`phase_abs <= PI_2`），落在 `case 3'b001`，输出近似为 \((-Q, I)\)（即乘以 \(j\)）。

#### 4.3.5 小练习与答案

- **练习 1**：`rot_lut` 只存 \([0,\pi/4]\) 的旋转因子，为什么够用？
  - **答案**：因为任意角度 \(\varphi\) 都能通过对称性（\(\cos/\sin\) 的交换与取负）从 \([0,\pi/4]\) 的值还原。`case(quadrant)` 的 8 种情况就是这 8 种对称变换。这样 512 项的表就能覆盖整个 \([-\pi,\pi)\)。
- **练习 2**：`out_i = p_i[26:11]`，为什么不是直接 `p_i >> 11`？
  - **答案**：两者数值等价（取 `[26:11]` 就是右移 11 位后保留 16 位），写切片是为了显式截断到 16 位输出宽度，同时丢掉低位（舍入误差）和可能的高位溢出——这是定点旋转的标准做法。

---

### 4.4 数据通路：phase_offset 的产生、传递与消费

#### 4.4.1 概念说明

前面两节讲了「求相位」和「做旋转」两个零件。本节把它们装回整机：`phase_offset` 是怎么从 `sync_short` 算出来、被 `dot11` 顶层当作一根线接到 `sync_long`、再在 `sync_long` 里逐步累加驱动 `rotate` 的。同时回答两个顶层资源共享问题：

- `phase` 模块只有**一个实例**，却被 `sync_short`（粗 CFO）和 `equalizer`（导频细校）共用——靠状态机分时复用。
- `rot_lut` 只有**一个实例**，却被 `sync_long` 和 `equalizer` 共用——靠双口 RAM 的两个端口。

#### 4.4.2 核心流程

```
┌───────────── dot11.v 顶层 ─────────────┐
│                                         │
│  sync_short ──phase_in_i/q/stb──┐       │
│                (粗 CFO)          ├─MUX──► phase_inst ──phase_out──┐
│  equalizer  ──phase_in_i/q/stb──┘   (state==S_SYNC_SHORT 选源)     │
│                (导频细校)                                          │
│                                                                    │
│  sync_short.phase_offset ──────────────（一根线）─────────► sync_long.phase_offset
│                                                                    │
│  sync_long ──rot_addr/data(A口)──┐                                  │
│  equalizer ──rot_addr/data(B口)──┴─► rot_lut_inst (双口 RAM)        │
└────────────────────────────────────────────────────────────────────┘
```

#### 4.4.3 源码精读

**（A）phase_offset 的产生 —— `sync_short`**

在 [u2-l2](u2-l2-sync-short.md) 里讲过，`sync_short` 把延迟自相关 `prod` 用 window=64 的 `moving_avg` 平滑后送到 `phase` 模块（[`sync_short.v:157-176`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L157-L176)）。`phase_out` 就是 \(\angle(\text{prod})\)。

[`sync_short.v:218`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L218) 先取负：`phase_out_neg <= ~phase_out + 1;`（取负是因为要「拧回」频偏，方向相反）。

然后在确认短前导成功的那一拍锁存 `phase_offset`（[`sync_short.v:234`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L234)）：

```verilog
phase_offset <= {{4{phase_out_neg[31]}}, phase_out_neg[31:4]};
```

这是「符号扩展的算术右移 4 位」= `phase_out_neg / 16` = \(-\angle(\text{prod})/16 = -\alpha_{ST}\)。这里的 `/16` 正好对应公式 \(\alpha_{ST}=\frac{1}{16}\angle(\cdot)\)，而前置的取负对应校正公式里的 \(e^{-jm\alpha}\)。所以一行代码同时实现了「除 16」和「取负」。

**（B）phase 模块的分时复用 —— `dot11`**

[`dot11.v:118-160`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L118-L160) 是顶层里最重要的一段。它用一个三选一 MUX 决定 `phase_inst` 这一刻服务谁（[`dot11.v:133-138`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L133-L138)）：

```verilog
wire[31:0] phase_in_i = state == S_SYNC_SHORT? sync_short_phase_in_i: eq_phase_in_i;
wire[31:0] phase_in_q = state == S_SYNC_SHORT? sync_short_phase_in_q: eq_phase_in_q;
wire phase_in_stb     = state == S_SYNC_SHORT? sync_short_phase_in_stb: eq_phase_in_stb;
```

输出则同时回连给两个调用方（[`dot11.v:143-146`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L143-L146)），由各自的状态机决定当前是否采纳。之所以能这样复用，是因为 `S_SYNC_SHORT`（粗 CFO 估计）和 `equalizer` 工作（导频细校，发生在 `S_DECODE_DATA` 等后续状态）在时间上**绝不重叠**。

**（C）rot_lut 的双口共享 —— `dot11`**

[`dot11.v:96-113`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L96-L113) 例化唯一的 `rot_lut_inst`，A 口接 `sync_long`，B 口接 `equalizer`：

```verilog
rot_lut rot_lut_inst (
    .clka(clock), .addra(sync_long_rot_addr), .douta(sync_long_rot_data),
    .clkb(clock), .addrb(eq_rot_addr),        .doutb(eq_rot_data)
);
```

`rot_lut` 本身是 Xilinx 双口 Block RAM（[`rot_lut.v:40-55`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/rot_lut.v#L40-L55)，`C_MEM_TYPE=4` 即真双口、深度 512、宽度 32），天然支持两个独立地址端口同时读，所以共享零成本。和 `phase` 的「分时」不同，`rot_lut` 是「空间上」的两个端口并行服务两个模块。

**（D）phase_offset 的消费 —— `sync_long`**

`sync_long` 把 `phase_offset` 接进来（[`dot11.v:306`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L306) 的 `.phase_offset(phase_offset)`）。当长前导确认时，把它作为累加起点（[`sync_long.v:275`](https://github.com/open-sdr/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L275)）：`next_phase_correction <= phase_offset;`

然后在 `S_FFT` 状态里，**每读一个时域样本就累加一次**（[`sync_long.v:304-322`](https://github.com/open-sdr/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L304-L322)）：把当前 `next_phase_correction` 作为该样本的 `phase_correction` 喂给 `rotate`，同时 `next_phase_correction += phase_offset`。这样第 \(m\) 个样本的旋转量就是 \(m\cdot\text{phase\_offset}= -m\alpha_{ST}\)，精确实现了 \(S'[m]=S[m]e^{-jm\alpha_{ST}}\)。代码里还用 `phase_offset` 的正负配合 `±DOUBLE_PI` 做了相位回绕（保持在 \([-\pi,\pi)\)），避免累加溢出。

#### 4.4.4 代码实践

**实践目标**：画出「含频偏的时域样本 → 校正后样本」的完整路径，并量化延迟。

1. **梳理分时复用**：在 [`dot11.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) 里确认 `phase_inst` 的输入 MUX 由 `state == S_SYNC_SHORT` 控制；`sync_short` 通过端口 `phase_in_i/q/stb` 与 `phase_out/stb` 与这个共享模块对话（[`dot11.v:284-289`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L284-L289)），`equalizer` 同理（[`dot11.v:330-335`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L330-L335)）。
2. **追踪 phase_offset 数据流**：`sync_short_inst` 的 `.phase_offset(phase_offset)`（[`dot11.v:292`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L292)）→ 顶层输出端口 `phase_offset`（[`dot11.v:41`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L41)）→ `sync_long_inst` 的 `.phase_offset(phase_offset)`（[`dot11.v:306`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L306)）。确认这是一根纯组合连线，中间无寄存器。
3. **写出完整路径**：`sample_in` → `sync_long.in_buf`(RAM 缓存) → `raw_i/raw_q` → `rotate`(乘 \(e^{-jm\alpha_{ST}}\)) → `fft_in_re/im` → `xfft`。频偏在这一路的「rotate」节点被校正。
4. **（可选，待本地验证）仿真观察**：用 [u1-l2](u1-l2-environment-and-simulation.md) 学过的 `make compile && make simulate` 跑一个 dot11a 样本，用 gtkwave 看 `dot11_tb` 暴露的 `phase_offset` 信号——它应当在 `short_preamble_detected` 置位的那一拍附近锁存为一个非零定点值，并在随后的 `S_SYNC_LONG`/`S_FFT` 期间保持不变。

**需要观察的现象 / 预期结果**：`phase_offset` 在整个数据段解码期间是**常数**（粗 CFO 只估一次）；`sync_long` 内部的 `phase_correction` 则随样本序号线性增长（每个样本加一个 `phase_offset`）。

#### 4.4.5 小练习与答案

- **练习 1**：既然 `phase` 模块在 `sync_short` 和 `equalizer` 间分时复用，为什么不会「打架」？
  - **答案**：因为顶层状态机保证两者不同时工作。`sync_short` 只在 `state==S_SYNC_SHORT` 时被 `enable`（[`dot11.v:165`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L165)），此时 MUX 选 `sync_short` 的输入；`equalizer` 的 `phase` 使用发生在 `S_DECODE_SIGNAL` 之后，状态早已离开 `S_SYNC_SHORT`，MUX 切到 `equalizer`。时间上互斥，所以一个实例够用。
- **练习 2**：如果把 `phase_offset` 那行的移位从 `>>4`（除 16）改成 `>>3`（除 8），会发生什么？
  - **答案**：`phase_offset` 会变成 \(-\angle(\text{prod})/8\)，即每样本频偏被高估一倍，`sync_long` 里每个样本的旋转量翻倍，星座点会被「过校」而朝反方向散开。这正好说明 `/16` 是和「STS 每 16 样本重复」这一物理事实绑死的常数，不能随便改。

---

## 5. 综合实践

**任务：把频偏校正从「公式」到「波形」完整走一遍，产出一张数据通路说明图。**

1. **准备**：按 [u1-l2](u1-l2-environment-and-simulation.md) 装好 iverilog/vvp/gtkkwave，在 `verilog/` 下用默认的 dot11a 24Mbps 样本跑通 `make compile && make simulate`。
2. **读码**：对照本讲 4.4 节，在 [`dot11.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) 里用笔标出三条线：
   - `phase` 分时复用的 MUX（`state==S_SYNC_SHORT`）；
   - `rot_lut` 双口共享（A 口 `sync_long`、B 口 `equalizer`）；
   - `phase_offset` 从 `sync_short_inst` 到 `sync_long_inst` 的连线。
3. **画图**：画出从 `sample_in` 到 `sync_long.sample_out` 的模块级框图，必须在 `rotate` 节点上标注「乘 \(e^{-jm\alpha_{ST}}\)，\(\alpha_{ST}\) 来自 sync_short 的 phase_offset」，并在 `xfft` 节点标注「时域→频域」。
4. **验证（待本地验证）**：打开 `dot11.vcd`，定位 `short_preamble_detected` 跳变时刻，测量 `phase_offset` 的锁存值（定点整数），手算它对应的真实频偏角度（除以 512 得弧度）。再观察 `sync_long` 内部 `phase_correction` 是否随样本数线性变化。
5. **提交物**：一张数据通路图 + 一段 150 字以内的「数据从含频偏到校正后的路径说明」，要求点明三个关键节点：①频偏在哪估出（`sync_short`+`phase`）；②相位怎么传递（`phase_offset` 连线）；③样本在哪被旋转（`sync_long`+`rotate`，FFT 之前）。

> 提示：本实践是纯阅读 + 波形观察，不修改任何源码。如果你想做「改参数看效果」的实验，参考 4.4.5 的练习 2，但要记得改完需手动恢复。

## 6. 本讲小结

- 频偏分两类：**CFO**（本振不一致，时域样本相位旋转）和 **SFO**（采样不一致，FFT 后星座点旋转）。OpenOFDM 用「STS 粗估 + 导频细校」应对，省略了基于 LTS 的细 CFO。
- [`phase.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v) 把复数变定点相位：取绝对值 → 求 min/max 折叠到 \([0,\pi/4]\) → 除法 → `atan_lut` 查表 → 3 位象限还原到 \([-\pi,\pi)\)，相位放大 \(2^9\) 倍。
- [`rotate.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v) 把样本旋转一个相位：相位折叠到 \([0,\pi/4]\) → `rot_lut` 取 sin/cos → 象限还原 → 复数乘法 → 右移 11 位重缩放。
- `sync_short` 用一行 `{{4{sign}}, phase_out_neg[31:4]}` 同时完成「取负 + 除 16」，得到每样本频偏 `phase_offset = -α_ST`。
- [`dot11.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) 做了两处资源共享：`phase` 模块靠 `state==S_SYNC_SHORT` 的 MUX 在 `sync_short`/`equalizer` 间**分时**复用；`rot_lut` 靠双口 RAM 在 `sync_long`/`equalizer` 间**并行**共享。
- 校正在 `sync_long` 里逐样本累加 `phase_correction = m·phase_offset` 并驱动 `rotate`，发生在 FFT **之前**，实现 \(S'[m]=S[m]e^{-jm\alpha_{ST}}\)。

## 7. 下一步学习建议

- 下一讲 [u2-l4 长训练序列同步与符号定时 sync_long.v](u2-l4-sync-long.md) 会完整拆开 `sync_long`：本讲里「黑盒」使用的 `phase_correction` 累加、LTS 互相关取峰定位 FFT 起点、GI 跳过等逻辑都会在那里展开。
- 想理解 `phase` 输出在 `equalizer` 里如何被用来做导频细校，等到 [u3-l1 信道均衡 equalizer.v](u3-l1-equalizer.md)，重点看 `S_CALC_FREQ_OFFSET` / `S_ADJUST_FREQ_OFFSET` 两个状态。
- 想搞清楚 `atan_lut` / `rot_lut` 的 256 / 512 项数据到底怎么填出来的，看 [u5-l4 查找表生成脚本](u5-l4-lut-generators.md)（`gen_atan_lut.py` / `gen_rot_lut.py`）。
- 定点缩放常数的全局依赖关系（改一个 shift 要同步改哪些地方）在 [u6-l1 定点数与缩放约定](u6-l1-fixed-point-scaling.md) 系统讲解。
