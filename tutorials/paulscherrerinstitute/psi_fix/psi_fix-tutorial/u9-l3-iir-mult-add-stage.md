# IIR 一阶低通与 mult_add_stage

## 1. 本讲目标

本讲把「位真双模型」与「两段式流水」方法论落到两个真实组件上：

- `psi_fix_lowpass_iir_order1`：一阶 IIR 低通滤波器，是本库中**唯一的递归（带反馈）滤波器**样例；
- `psi_fix_mult_add_stage`：映射到 FPGA DSP slice 的通用乘加构建块，是并行/半并行 FIR 的积木。

学完后你应能：

1. 写出一阶 IIR 的差分方程，并解释 α、β 两个系数如何由采样率与截止频率算出；
2. 说清 `pipeline_g` 两种取值下「时序（Fmax）」与「延迟（latency）」的取舍；
3. 解释**为什么 IIR 的反馈支路必须做内部量化**，以及 Python 位真模型为何要严格镜像这一量化点；
4. 读懂 `mult_add_stage` 的四级流水如何对应 DSP48 slice 的内部结构，以及它如何级联成 FIR。

本讲承接 u4-l1（差分-累加、位增长规则、Manual Splitting）与 u2-l2（定点运算函数），并与 u7（FIR，无反馈）形成「递归 vs 非递归」的对照。

## 2. 前置知识

- **FIR 与 IIR 的区别**：FIR（有限脉冲响应）只有前向支路，输出只依赖当前和历史输入；IIR（无限脉冲响应）**带反馈**，输出还依赖历史输出。反馈让 IIR 用更少阶数达到更陡的过渡带，但代价是「反馈环路里必须存放有限位宽的中间结果」，从而每拍引入量化误差。
- **一阶低通的直觉**：把 RC 低通电路离散化，得到「当前输入乘一个小权重 β、加上历史输出乘一个接近 1 的权重 α」的递推。截止频率越低（相对采样率），α 越接近 1，滤波越「重」。
- **位增长规则**（来自 u1-l4 / u2-l2）：两个 `[1,a,b]` 有符号数相乘，结果整数位相加后再 +1，即 `[1, a+c+1, b+d]`。本讲会反复用它推导中间格式。
- **DSP slice 与级联链**：Xilinx 等 FPGA 的 DSP48 slice 内含「输入寄存器 → 乘法器 → 加法器/累加器」，且相邻 slice 之间有**专用级联走线**（cascade），可把上一个 slice 的加法结果直接喂给下一个 slice 的加法器，不占用通用逻辑资源。`mult_add_stage` 就是为套用这条链而设计的。
- **位真双模型**（来自 u1-l1 / u3-l1）：每个可综合组件必须有一个逐位一致的 Python 黄金模型；自检测试台读 preScript 生成的整数位模式文本，逐位比对，不一致就打印 `###ERROR###`。

## 3. 本讲源码地图

| 文件 | 作用 |
|:-----|:-----|
| [hdl/psi_fix_lowpass_iir_order1.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lowpass_iir_order1.vhd) | 一阶 IIR 低通的可综合 VHDL，含 `pipeline_g` 两套实现 |
| [model/psi_fix_lowpass_iir_order1.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lowpass_iir_order1.py) | IIR 的 Python 位真模型（黄金参考），与 VHDL 共用同一组系数公式与量化点 |
| [hdl/psi_fix_mult_add_stage.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mult_add_stage.vhd) | 通用乘加构建块，四级流水对应 DSP slice |
| [hdl/psi_fix_fir_par_nch_chtdm_conf.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_par_nch_chtdm_conf.vhd) | 并行 FIR，示范 `mult_add_stage` 如何级联成 MAC 链 |
| [testbench/psi_fix_lowpass_iir_order1_tb/psi_fix_lowpass_iir_order1_tb.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_lowpass_iir_order1_tb/psi_fix_lowpass_iir_order1_tb.vhd) | IIR 自检测试台（stim/check 双进程） |
| [testbench/psi_fix_lowpass_iir_order1_tb/Scripts/preScript.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_lowpass_iir_order1_tb/Scripts/preScript.py) | 协同仿真脚本：用 Python 模型生成 chirp 激励与期望输出 |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl) | 回归配置，注册 IIR 测试台并挂 pre_script |

> 说明：`mult_add_stage` 在本库中是**纯 VHDL 结构性组件**，没有配套的 Python 位真模型，也没有自己的测试台目录（文档 `doc/files/psi_fix_mult_add_stage.md` 里提到的 `psi_fix_mult_add_stage_tb.vhd` 并不存在于仓库中）。它的正确性通过引用它的 FIR 组件（`fir_par_*` / `fir_dec_semi_*`）的回归间接验证，这与 u4-l2 的 `param_ram`、u4-l3 的 `comparator` 同属「归约到可信原语/上层覆盖」的策略。

## 4. 核心概念与源码讲解

### 4.1 一阶 IIR 低通滤波器实现

#### 4.1.1 概念说明

一阶 IIR 低通是最简单的递归滤波器，其差分方程为：

\[
y[n] = \beta \cdot x[n] + \alpha \cdot y[n-1]
\]

其中 α、β 由连续时间 RC 低通的「冲激不变」离散化得到。设截止频率 \(f_c\)、采样率 \(f_s\)，时间常数 \( \tau = 1/(2\pi f_c) \)，则：

\[
\alpha = e^{-\frac{1}{f_s \tau}}, \qquad \beta = 1 - \alpha
\]

它的直流增益为 1（因为 \( H(e^{j0}) = \beta/(1-\alpha) = 1 \)），所以通带不衰减。α 越接近 1（即 \(f_c \ll f_s\)），平滑越强、延迟越大。VHDL 头部的示意框图正画出这条「前向乘 β → 加法器 → 寄存器 → 输出，输出再乘 α 反馈回加法器」的结构。

文档明确提醒：这种直通结构**只适合截止频率比采样率低一两个数量级**的场景；若截止频率接近直流，对系数量化精度的要求会急剧上升，应改用专门针对低频的结构。这也是 `coef_fmt_g` 被暴露成 generic、并要求用户自己评估量化误差的原因。

#### 4.1.2 核心流程

整个组件可拆成「综合期常数计算」与「运行时递推」两段：

1. **综合期**（常数计算）：
   - 用 `coef_alpha_func` 由 \(f_s, f_c\) 算出实数 α；
   - 把 α 和 \(1-\alpha\) 各自量化到 `coef_fmt_g`，得到 `alpha_c`、`beta_c` 两个常量位向量（烧进硬件，不占乘法器之外的运行时资源）。

2. **运行时**（逐拍递推，以下以 `pipeline_g = false` 为例）：
   - **stage 0**：`mulIn = dat_i × beta_c`（前向支路乘法）；
   - **stage 1**：`add = mulIn + fb`（加法器，fb 是反馈寄存器）；同拍若有效则 `fb = add × alpha_c`（反馈支路乘法，更新反馈寄存器）；
   - **stage 2**：`res = resize(add)`（量化到输出格式）。

   一组配套的 `strb` 选通向量随数据逐级平移，既驱动 `vld_o`，也**门控反馈更新**——只有当有效样本推进到加法级时才允许更新 `fb`，保证无效节拍不破坏递推关系。

3. **位真镜像**：Python 模型 `Filter()` 用同一个递推循环、同一组量化点跑出期望输出，preScript 把输入与期望输出写成整数文本，测试台逐位比对。

#### 4.1.3 源码精读

系数计算函数把数学公式直接翻译成 VHDL（注意 `tau` 用 \(2\pi f_c\) 表达）：

[psi_fix_lowpass_iir_order1.vhd:51-57](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lowpass_iir_order1.vhd#L51-L57) —— 综合期由 \(f_s, f_c\) 算出实数 α。

随后 α 与 \(1-\alpha\) 在综合期各自量化为常量位向量 `alpha_c` / `beta_c`：

[psi_fix_lowpass_iir_order1.vhd:60-62](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lowpass_iir_order1.vhd#L60-L62) —— 两个系数被 `psi_fix_from_real` 量化到 `coef_fmt_g`，烧成硬件常量。

`pipeline_g = false` 的运行时递推（最精简的三级流水）：

[psi_fix_lowpass_iir_order1.vhd:111-122](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lowpass_iir_order1.vhd#L111-L122) —— 注意第 115 行加法用 `psi_fix_trunc`，第 119 行反馈乘法用 `round_g/sat_g`；`strb(1)` 门控反馈更新。

Python 黄金模型用 `for` 循环实现同样的递推（递归滤波器的循环不可避免）：

[psi_fix_lowpass_iir_order1.py:63-71](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lowpass_iir_order1.py#L63-L71) —— `add = mulIn_i + fb`，`fb = add × alpha`，`out[i] = resize(add)`，逐样本循环。

两侧系数公式完全同构（`CoefAlphaCalc` 与 VHDL 的 `coef_alpha_func` 是同一份式子）：

[psi_fix_lowpass_iir_order1.py:77-80](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lowpass_iir_order1.py#L77-L80) —— Python 侧的 α 计算，与 VHDL 一致。

> 关键对照点：VHDL 第 115 行加法用 `trunc`、Python 第 69 行只传 `sat` 而不传 round。两者位等价，因为加法 `int_fmt + int_fmt → int_fmt` 的分数位不变（24→24），没有低位被丢弃，round 与 trunc 结果完全相同——Python 注释「Rounding not required since fractional bits are not changed」正是此意。

#### 4.1.4 代码实践

**实践目标**：手算一组真实系数，验证「直流增益为 1」与「α 随截止频率降低而趋近 1」。

**操作步骤**：

1. 取测试台参数 \(f_s = 100\,\text{MHz}\)、\(f_c = 1\,\text{MHz}\)（见 [psi_fix_lowpass_iir_order1_tb.vhd:39-40](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_lowpass_iir_order1_tb.vhd#L39-L40)）。
2. 用公式算 α：\( \tau = 1/(2\pi \cdot 10^6) \approx 1.59\times10^{-7}\,\text{s} \)，\( \alpha = e^{-10^{-8}/\tau} = e^{-2\pi\cdot 10^{-2}} \approx 0.939 \)，\( \beta \approx 0.061 \)。
3. 检验直流增益：\( \beta/(1-\alpha) = 0.061/(1-0.939) = 0.061/0.061 \approx 1.0 \)。
4. 再取 \(f_c = 10\,\text{kHz}\) 重算，应得到 α ≈ 0.9994，验证「截止频率越低 α 越接近 1」。

**需要观察的现象 / 预期结果**：\(f_c\) 每降低一个数量级，α 更靠近 1，β 更靠近 0；直流增益始终为 1。可把 α、β 量化到 `coef_fmt_g = (1,0,17)` 后再算一次 \( \alpha_c + \beta_c \)，会发现由于各自独立量化，二者之和**不一定恰好等于 1.0**（存在约 1 LSB 的直流增益误差）——这正是文档强调「系数格式要自己评估量化误差」的原因。

#### 4.1.5 小练习与答案

**练习 1**：若把 `f_cutoff_hz_g` 调到与 `f_sample_hz_g` 相等，α 会接近多少？滤波器还起低通作用吗？

**参考答案**：\( \alpha = e^{-2\pi} \approx 0.00187 \)，β ≈ 0.998。此时几乎不反馈历史输出，输出≈输入，低通作用几乎消失——印证「该结构只适合 \(f_c \ll f_s\)」。

**练习 2**：为什么反馈乘法用 `round_g/sat_g`，而加法却固定用 `psi_fix_trunc`？

**参考答案**：反馈乘法 `add × alpha` 的全精度积分数位比 `int_fmt` 多，必须丢弃大量低位 → 量化发生，round/trunc 结果不同，故用用户配置的 `round_g`。加法 `int + int → int` 分数位不变，无量化，trunc 与 round 等价，写哪个都行。

---

### 4.2 pipeline_g 流水选项：时序与延迟的权衡

#### 4.2.1 概念说明

`pipeline_g` 是一个布尔 generic，注释写得很直白：

> `True = Optimize for clock speed, False = Optimize for latency`

它对应同一个算法的**两份 RTL**（用 `generate` 二选一）。差别只在于是否在「前向乘法 → 加法器」这条组合路径中间多插一拍寄存器：

- **False（低延迟）**：乘法结果 `mulIn` 直接进加法器，`mulIn` 与 `add` 在相邻两拍，组合路径含「乘法 + 加法」，关键路径长、Fmax 低，但总延迟少一拍。
- **True（高时钟频率）**：在乘法器与加法器之间多插一级 `mulInFF` 寄存器，把「乘法」和「加法」分到两个时钟周期，关键路径短、Fmax 高，但总延迟多一拍。

这是 u3-l3 / u4-l1 讲过的「Manual Splitting（手动拆分流水）」在递归结构上的应用：把一条长组合路径拆成两段，用一拍延迟换更高的时钟频率。注意：因为这是递归滤波器，额外插的寄存器必须配合 `strb` 选通，确保「每来一个有效样本，反馈恰好更新一次」，多出来的流水寄存器不改变样本间的递推关系，只增加端到端延迟。

#### 4.2.2 核心流程

用选通索引 `strb` 的最终位置即可读出两种模式的延迟：

| 模式 | 前向数据通路寄存器 | `vld_o` 来源 | 端到端延迟 |
|:-----|:-------------------|:-------------|:-----------|
| `pipeline_g = false` | mulIn → add → res（3 级） | `strb(2)` | 3 拍 |
| `pipeline_g = true`  | mulIn → mulInFF → add → res（4 级） | `strb(3)` | 4 拍 |

两种模式的反馈更新门控位置也相应错开：false 模式在 `strb(1)='1'` 时更新 `fb`，true 模式在 `strb(2)='1'` 时更新 `fb`（因为加法结果要多一拍才出现）。两条 strobe 链都满足「一个有效样本对应恰好一次反馈更新」，故算法位等价。

#### 4.2.3 源码精读

`generate` 二选一的外壳：

[psi_fix_lowpass_iir_order1.vhd:71-72](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lowpass_iir_order1.vhd#L71-L72) —— `pipe_gene` 与 `nopipe_gene` 两个 generate 分支。

`pipeline_g = true` 多出来的 `mulInFF` 寄存器（这就是「拆分乘法+加法」的那一拍）：

[psi_fix_lowpass_iir_order1.vhd:82-90](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lowpass_iir_order1.vhd#L82-L90) —— 注意第 83 行的 `mulInFF <= mulIn`，以及第 88 行反馈门控改为 `strb(2)`。

两模式下 `vld_o` 的来源不同，直接对应延迟差异：

[psi_fix_lowpass_iir_order1.vhd:99-100](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lowpass_iir_order1.vhd#L99-L100)（true：`strb(3)`）与 [psi_fix_lowpass_iir_order1.vhd:127-128](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lowpass_iir_order1.vhd#L127-L128)（false：`strb(2)`）。

测试台用 generic `pipeline_g` 把这个旋钮暴露出来，方便两套实现各跑一轮回归：

[psi_fix_lowpass_iir_order1_tb.vhd:28-31](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_lowpass_iir_order1_tb.vhd#L28-L31) —— 测试台 generic `pipeline_g`，映射进 DUT。

#### 4.2.4 代码实践

**实践目标**：通过比较两份 RTL 的 strobe 索引，确认延迟差为 1 拍，并理解为何位真模型不需要为两种模式分别写期望输出。

**操作步骤**：

1. 在 [hdl/psi_fix_lowpass_iir_order1.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lowpass_iir_order1.vhd) 中分别数 `pipe_gene` 与 `nopipe_gene` 两条分支里前向数据通路上的寄存器个数（mulIn、mulInFF、add、res）。
2. 对照 `vld_o` 取自 `strb(3)` 还是 `strb(2)`，确认两种模式延迟分别为 4 拍与 3 拍。
3. 打开 [preScript.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_lowpass_iir_order1_tb/Scripts/preScript.py)，确认它只生成一份 `output.txt`。

**需要观察的现象 / 预期结果**：两份 RTL 用同一份期望输出 `output.txt` 都能通过位真比对——因为测试台按 `vld_o`（而非固定拍数）对齐输出，且两种模式样本间的递推关系完全相同，只是端到端延迟不同；`###ERROR###` 不会因模式切换而出现。实际跑回归验证为「待本地验证」（需 PsiSim + Modelsim/GHDL 环境）。

#### 4.2.5 小练习与答案

**练习 1**：如果目标 FPGA 时序很松（时钟频率很低），应该选哪个模式？为什么？

**参考答案**：选 `pipeline_g = false`。时序不紧张时无需拆分组合路径，少一拍延迟更划算（文档称 optimize for latency）。

**练习 2**：为什么 `pipeline_g = true` 模式里反馈门控是 `strb(2)` 而不是 `strb(1)`？

**参考答案**：true 模式下加法结果 `add` 比 false 模式晚一拍出现（多了 `mulInFF`），反馈乘法 `fb = add × alpha` 必须等到 `add` 有效那一拍才更新，对应的选通位正是 `strb(2)`；提前更新会拿到尚未就绪的加法结果，破坏递推。

---

### 4.3 IIR 反馈支路的内部量化与位真考量

#### 4.3.1 概念说明

这是本讲的核心，也是 IIR 与 FIR（u7）最本质的区别。先看反馈支路这一行：

```vhdl
fb <= psi_fix_mult(add, int_fmt_g, alpha_c, coef_fmt_g, int_fmt_g, round_g, sat_g);
```

`add` 是 `int_fmt_g = (1,0,24)`、`alpha_c` 是 `coef_fmt_g = (1,0,17)`。按位增长规则，两个有符号数相乘的全精度积为：

\[
(1,0,24) \times (1,0,17) \;\Rightarrow\; (1,\;0+0+1,\;24+17) = (1,1,41)
\]

即 43 位。但 `fb` 必须存回 `int_fmt_g = (1,0,24)`（25 位）才能在下一拍喂给加法器。于是 **43 位的乘积被强制量化成 25 位**：丢掉 1 个整数位（饱和）和 17 个分数位（舍入）。这一量化**每来一个样本就发生一次**，且量化后的值会在反馈环里无限循环。

这就是「IIR 内部量化不可避免」的根本原因：

- 反馈寄存器是**有限位宽**的物理存储，不可能在递归环里保留无限精度；
- 每拍量化误差 \( e[n] \) 注入环中，下一拍又乘以 α 留下、再叠加新的误差。对稳定滤波器（\(|\alpha|<1\)），误差有界但持续存在，严重时表现为**极限环（limit cycle）**；
- 因此 `int_fmt_g` 必须留足分数位（这里 24 位），把环内量化误差压到可接受水平。

对照 FIR（u7）：FIR 无反馈，累加器可以全精度累加，**只在末端 resize 一次**，量化误差不循环、不积累。这正是同一定点库在两种结构上的分水岭。

#### 4.3.2 核心流程

「位真考量」在这里体现为一个硬约束：**Python 黄金模型必须镜像硬件的每一个量化点，尤其是反馈环里的那一个**。流程是：

1. 标出硬件 RTL 中所有定点运算，逐一确认其结果格式与 round/sat 配置；
2. 在 Python 模型里**用相同的 `intFmt`、相同的 round/sat** 重放这些运算；
3. 对反馈支路，Python 必须同样把 `add × alpha` 量化回 `intFmt`（而不是保留全精度 float），否则模型与硬件会逐渐发散；
4. preScript 把模型输出经 `psi_fix_get_bits_as_int` 写成整数位模式，测试台逐位比对。

IIR 这一组件里共有 **三个量化点**：

| 量化点 | 运算 | 全精度结果 | 量化到 | 配置 |
|:-------|:-----|:-----------|:-------|:-----|
| 前向乘法 | `dat × beta` | `(1,1,32)` | `int_fmt=(1,0,24)` | round/sat |
| **反馈乘法（环内）** | `add × alpha` | `(1,1,41)` | `int_fmt=(1,0,24)` | round/sat |
| 输出 resize | `add → out` | `(1,0,24)` | `out_fmt=(1,0,14)` | round/sat |

加法 `int+int→int` 不量化（分数位不变），故不在表中。

#### 4.3.3 源码精读

VHDL 反馈支路（量化发生处）：

[psi_fix_lowpass_iir_order1.vhd:89](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lowpass_iir_order1.vhd#L89) —— `fb = add × alpha_c`，结果量化到 `int_fmt_g`，环内每拍一次。

Python 模型在循环里做完全相同的量化（注意 `fb` 同样回到 `intFmt`）：

[psi_fix_lowpass_iir_order1.py:68-71](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lowpass_iir_order1.py#L68-L71) —— 第 70 行 `fb = psi_fix_mult(add, intFmt, alpha, coefFmt, intFmt, rnd, sat)`，与 VHDL 第 89 行一一对应。

preScript 把模型输出转成位模式整数，供测试台逐位比对：

[preScript.py:63-64](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_lowpass_iir_order1_tb/Scripts/preScript.py#L63-L64) —— `psi_fix_get_bits_as_int` 把定点值写成有符号整数（整数即位模式），`fmt="%i"` 落盘。

测试台 check 进程把 DUT 输出转回有符号整数逐行比对：

[psi_fix_lowpass_iir_order1_tb.vhd:154-166](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_lowpass_iir_order1_tb.vhd#L154-L166) —— 不符即打印 `###ERROR###`（CI 唯一失败判据）。

#### 4.3.4 代码实践（本讲主任务）

**实践目标**：对照 Python 模型，论证「反馈支路为何需要内部量化」，并亲手观察取消该量化点的后果。

**操作步骤**：

1. 打开 [model/psi_fix_lowpass_iir_order1.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lowpass_iir_order1.py) 的 `Filter()`，定位第 70 行 `fb = psi_fix_mult(..., intFmt, ...)`，确认它把乘积量化回了 `intFmt`。
2. **思想实验（推荐本地用 NumPy 复现）**：复制该模型，把反馈支路改成「不量化」——让 `fb` 用 Python float 全精度传递（例如 `fb = add.astype(float) * float(alpha)`），其余不变，跑同样的 chirp 输入（见 [preScript.py:40-46](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_lowpass_iir_order1_tb/Scripts/preScript.py#L40-L46)）。
3. 比较两份输出的最低几位是否出现差异。
4. 把 `intFmt` 的分数位从 24 降到（例如）8，重跑量化版模型，观察输出与高精度版的偏差。

**需要观察的现象 / 预期结果**：

- 步骤 2 中，「不量化反馈」的输出会比量化版**更精确**，但它已**不再位等价于 VHDL**——若用它当黄金参考，测试台必然报 `###ERROR###`。这反向证明：位真模型必须主动「降精度」去贴硬件，而不是追求自身最准。
- 步骤 4 中，`intFmt` 分数位越少，环内量化误差越大，输出与全精度的偏差越明显，极端时会观察到直流偏置或低频小幅度振荡（极限环雏形）。

**说明**：步骤 2–4 属「待本地验证」，需本地 Python + NumPy/SciPy 环境；步骤 1 是纯源码阅读，可立即完成。

#### 4.3.5 小练习与答案

**练习 1**：把反馈乘法的全精度积 `(1,1,41)` 量化到 `int_fmt=(1,0,24)` 时，丢了多少个分数位？为什么这不会立即导致溢出？

**参考答案**：丢了 17 个分数位（24+17−24）和 1 个整数位（饱和）。不立即溢出是因为信号本身有界：`add` 是低通输出，跟踪 `dat ∈ [−1,+1)`，乘以 α∈(0,1) 后 `fb` 仍落在 `[−1,+1)` 内，`int_fmt` 的 1 个符号位刚好够用。

**练习 2**：如果 Python 模型把 `fb` 留成全精度 float 不量化，长期运行后模型与 VHDL 的输出关系会怎样？

**参考答案**：模型会比 VHDL 更精确，二者逐位发散；用该模型做黄金参考会让测试台持续报 `###ERROR###`。位真的要义是模型贴硬件的量化点，而非模型自身最优。

---

### 4.4 mult_add_stage：映射到 DSP slice 的乘加构建块

#### 4.4.1 概念说明

`psi_fix_mult_add_stage` 是一个**结构性构建块**：它本身不实现某个信号处理算法，而是把「一次乘法 + 一次加法」打包成四级流水，刻意排布成能被综合工具**直接推断成单个 DSP slice**（如 Xilinx DSP48）的形态。多个 stage 通过专用级联链首尾相连，就构成并行/半并行 FIR 的 MAC 链（详见 u7-l2）。

它的两个关键设计：

- **四级流水对应 DSP48 内部结构**：输入寄存器（2 级，对应 DSP48 的 A/B 输入寄存器 + 流水寄存器）→ 乘法器 → 带级联输入的加法器（对应 DSP48 的加法器/累加器 + CASCADEIN）。乘法用**全精度** `MultFmt_c`、加法保持 `add_fmt_g`，全程不 round/sat——这是为了不引入额外逻辑、把全部计算塞进 DSP。
- **双模式 B 口**（`in_b_is_coef_g`）：B 既可以当数据（每次有效触发一次乘加并传播 valid），也可以当**异步更新的系数**（只在 B 有效时把系数写进 DSP 的输入寄存器，不产生输出样本）——后者正是 FIR 在运行时换系数的用法。

#### 4.4.2 核心流程

四级流水的数据流（每级一个寄存器，对应 DSP48 的流水节点）：

```
Stage 0:  InAReg0/InBReg0 <= dat_a_i/dat_b_i   (输入寄存 1)
Stage 1:  InAReg1/InBReg1 <= InAReg0/InBReg0   (输入寄存 2)
Stage 2:  MultReg <= InAReg1 * InBReg1          (乘法，全精度 MultFmt_c)
Stage 3:  AddReg  <= chain_add_i + MultReg      (加法，带级联输入)
```

关键端口：

| 端口 | 方向 | 作用 |
|:-----|:-----|:-----|
| `chain_add_i` | in | 上一级 stage 的加法输出，进本级加法器（级联链入口） |
| `chain_add_o` | out | 本级加法输出，接下一级 `chain_add_i`（级联链出口） |
| `del2_a_o` / `del2_b_o` | out | A/B 各延迟 2 拍的副本，用于 FIR 的抽头间数据移位 |
| `vld_b_i` + `in_b_is_coef_g` | — | 决定 B 是数据（触发输出）还是系数（仅写寄存器） |

全精度乘积格式由位增长规则严格推出：

\[
\text{MultFmt\_c} = (\max(S_a,S_b),\; I_a+I_b+1,\; F_a+F_b)
\]

那个 `+1` 整数位正是「两个有符号数相乘整数位相加后再 +1」的规则。

#### 4.4.3 源码精读

全精度乘积格式常量（位增长规则的直接落地）：

[psi_fix_mult_add_stage.vhd:54](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mult_add_stage.vhd#L54) —— `MultFmt_c` 取 `Ia+Ib+1`、`Fa+Fb`，全精度不丢位。

四级流水主体（Stage 0 输入寄存 + 双模式 B 口选择）：

[psi_fix_mult_add_stage.vhd:71-84](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mult_add_stage.vhd#L71-L84) —— `in_b_is_coef_g=false` 时 `Vld0 <= vld_a_i or vld_b_i`（B 作数据），`true` 时 `Vld0 <= vld_a_i`（B 仅写系数，不产生输出）。

Stage 2 乘法与 Stage 3 带级联输入的加法（核心 DSP 操作，无 round/sat）：

[psi_fix_mult_add_stage.vhd:92-99](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mult_add_stage.vhd#L92-L99) —— 第 93 行 `psi_fix_mult(... MultFmt_c)` 无量化参数（走默认 trunc/wrap，但因结果格式恰为全精度，等价无量化）；第 98 行 `psi_fix_add(chain_add_i, ..., MultReg, ...)` 把上级级联和与本级乘积相加。

在并行 FIR 里的级联用法——首级 `chain_add_i` 接 0，输出接 `DspAccuChain(0)` 喂给下一级：

[psi_fix_fir_par_nch_chtdm_conf.vhd:110-128](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_par_nch_chtdm_conf.vhd#L110-L128) —— 首个 slice 的 `del2_a_o` 接入数据链、`chain_add_o` 接入累加链。

后续抽头把上一级的 `chain_add_o` 喂给本级的 `chain_add_i`，把上一级的 `del2_a_o` 喂给本级的 `dat_a_i`，形成级联 MAC：

[psi_fix_fir_par_nch_chtdm_conf.vhd:154-175](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_par_nch_chtdm_conf.vhd#L154-L175) —— `for i in 1 to taps_g-1 generate`，逐抽头级联。

#### 4.4.4 代码实践

**实践目标**：理解「一个 stage = 一个 DSP slice」、「多个 stage 级联 = 一条 FIR MAC 链」。

**操作步骤**：

1. 在 [hdl/psi_fix_mult_add_stage.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mult_add_stage.vhd) 中，把四个 Stage 注释（71、85、91、96 行）与 DSP48 的「A/B 寄存器 → M（乘法）→ P（加法/累加）」四段对应起来。
2. 打开 [hdl/psi_fix_fir_par_nch_chtdm_conf.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_par_nch_chtdm_conf.vhd)，跟踪 `DspAccuChain` 与 `DspDataChain` 两条链：前者把 `chain_add_o(i-1)` → `chain_add_i(i)`（累加），后者把 `del2_a_o(i-1)` → `dat_a_i(i)`（数据逐抽头移位）。
3. 数一数：一个 N 抽头并行 FIR 需要例化多少个 `mult_add_stage`？

**需要观察的现象 / 预期结果**：

- 步骤 2：第一条链实现「乘积之和」\( \sum h[k]x[n-k] \)（FIR 的 MAC），第二条链实现转置直接型的数据移位。
- 步骤 3：N 抽头 = N 个 stage = N 个 DSP slice（首级 `chain_add_i` 接 0，末级 `chain_add_o` 给出最终和）。这与 u7-l1 「par 每抽头 1 个乘法器、1 周期/样本」的结论一致。

**说明**：本实践为纯源码阅读型，无需运行仿真即可完成。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `psi_fix_mult` 在 Stage 2 不传 round/sat 参数？传了会有什么后果？

**参考答案**：因为 `MultFmt_c` 正好等于全精度积，没有位要丢弃，量化不发生，参数无意义。若强行传一个比全精度更窄的 `r_fmt`，就会在乘法后插入了量化逻辑，脱离 DSP 单 slice 的映射，资源与 Fmax 双输。

**练习 2**：`in_b_is_coef_g = true` 时，为什么 `Vld0 <= vld_a_i` 而不是 `vld_a_i or vld_b_i`？

**参考答案**：B 作系数时，更新系数只是改写 DSP 输入寄存器，不应产生新的输出样本；输出节奏完全由 A（数据）的有效性决定。若改成 `or`，每次写系数都会冒出一个虚假的有效输出，破坏 FIR 的样本对齐。

---

## 5. 综合实践

把本讲三个知识点（IIR 递推、`pipeline_g` 权衡、反馈量化）串起来，完成一次「源码追踪 + 位真论证」：

**任务**：跟踪一个有效样本在 `pipeline_g = true` 的 IIR 中从 `dat_i` 到 `dat_o` 的完整旅程，并指出该样本在反馈环里引入的量化点。

**操作步骤**：

1. 在 [hdl/psi_fix_lowpass_iir_order1.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lowpass_iir_order1.vhd) 的 `pipe_gene` 分支里，按时钟拍数列出该样本经过的每一级寄存器与对应的 `strb` 位（提示：mulIn→mulInFF→add→res，对应 strb(0)→(1)→(2)→(3)）。
2. 指出该样本在「stage 0 前向乘法」与「stage 3 反馈乘法」各发生了一次量化，分别把 `(1,1,32)` 与 `(1,1,41)` 量化到 `int_fmt=(1,0,24)`。
3. 打开 [model/psi_fix_lowpass_iir_order1.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lowpass_iir_order1.py)，确认 `Filter()` 循环里第 63、70、71 行正是这三个量化点的镜像。
4. 得出结论：**两套 RTL（pipeline true/false）共用同一份位真模型，因为它们样本间的递推与量化点完全相同，差异仅在端到端延迟**。

**预期产出**：一张「拍数 × 信号/strb」表 + 一句结论——位真模型贴的是「算法 + 量化点」，而不是「某一种流水实现」。实际仿真回归（`pipeline_g` 两种取值都不出现 `###ERROR###`）为「待本地验证」。

## 6. 本讲小结

- 一阶 IIR 低通实现差分方程 \( y[n]=\beta x[n]+\alpha y[n-1] \)，α、β 在综合期由 \(f_s, f_c\) 算出并量化为硬件常量；该结构只适合 \(f_c \ll f_s\)。
- `pipeline_g` 是「时序 vs 延迟」的开关：true 多插一级 `mulInFF` 拆分「乘+加」关键路径、Fmax 高、延迟 4 拍；false 延迟 3 拍、Fmax 低。两模式共用同一份位真模型。
- **IIR 反馈支路必须内部量化**：`add × alpha` 的 43 位全精度积被强制存回 25 位 `fb` 寄存器，每拍一次，误差在环内循环；这是 IIR 与无反馈 FIR 的本质区别，也是 `int_fmt` 必须留足分数位的原因。
- 位真模型必须**镜像硬件的每个量化点**（尤其是反馈环那个），宁可主动降精度去贴硬件，也不追求模型自身最准——否则逐位发散、`###ERROR###` 必现。
- `mult_add_stage` 是映射 DSP slice 的四级流水乘加块（输入寄存×2 → 全精度乘 → 带级联输入的加法），通过 `chain_add` 首尾级联即构成并行/半并行 FIR 的 MAC 链；它是纯结构性组件，无独立 Python 模型与测试台，靠上层 FIR 回归覆盖。

## 7. 下一步学习建议

- **回头看 FIR**：重读 u7-l2，体会「无反馈 → 仅末端量化 → 可全精度累加」与本讲 IIR 的对照，理解为何 FIR 的 `accuFmt` 可以放心做大、而 IIR 的 `intFmt` 要谨慎选。
- **看 CIC**：u6 的 CIC 也是递归结构（积分器带反馈），但其用「整数 + 取模」仿真补码回绕，量化点处理与本讲不同，可对比两种递归滤波器的位真策略。
- **动手贡献**：参考 u10-l1，尝试给一个「二阶 IIR（biquad）」写 Python 位真模型——关键是正确标出两个反馈支路的量化点，并用本讲的「拍数 × 信号」表法验证与 RTL 对齐。
- **延伸阅读**：`doc/files/psi_fix_lowpass_iir_order1.md` 关于「低截止频率应换结构」的建议，以及 `doc/files/psi_fix_mult_add_stage.md` 的 3 抽头并行 FIR 示意图，配合本讲源码精读食用。
