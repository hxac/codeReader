# CORDIC 矢量模式（psi_fix_cordic_vect）

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 CORDIC **矢量模式（vectoring）** 的算法原理：如何只用「移位 + 加减」从直角坐标 \((I,Q)\) 算出幅度 \(r=\sqrt{I^2+Q^2}\) 与相位 \(\theta=\mathrm{atan2}(Q,I)\)。
- 把矢量模式与上一讲的旋转模式（`psi_fix_cordic_rot`，见 u5-l2）放在一起对照，理解两者是**同一迭代内核、不同驱动目标**的对称体。
- 读懂 `psi_fix_cordic_vect` 的 VHDL 实现：象限折叠、CORDIC 迭代、增益补偿、PIPELINED 与 SERIAL 两种架构，以及它与 Python 位真模型如何逐位对齐。
- 理解 AXI4-Stream 握手中 `rdy` 的方向语义，解释 4.0.2 版本为何把 `rdy_i` 重命名为 `rdy_o`。

## 2. 前置知识

本讲默认你已经学完：

- **u1-l4**：定点三元组 \([s,i,f]\)、位增长规则、`vld/rdy` 握手与 `_i/_o/_g` 命名后缀。
- **u2-l1 / u2-l2**：`psi_fix_pkg` 的类型定义与 `resize/add/sub/mult/abs/shift_right` 等运算函数。
- **u3-l2**：自检测试台 + `preScript.py` 协同仿真套路（Python 模型生成 `Data/*.txt`，VHDL 用 `###ERROR###` 逐位比对）。
- **u5-l2**：旋转模式 CORDIC（`psi_fix_cordic_rot`）的迭代算法、PIPELINED/SERIAL 架构与增益补偿。

两个会反复用到的小概念：

- **CORDIC**（COordinate Rotation DIgital Computer）：一种只含「移位 + 加减 + 查表」、不含乘法器的迭代算法，适合硬件算三角函数、求模、求相位。
- **atan2 / 象限**：把一个二维向量 \((I,Q)\) 的角度按它落在第几象限分别处理。本讲里角度用「圈（turns）」为单位，即 \(1.0\) 圈 \(=2\pi=360^\circ\)，所以 \(0.5\) 圈 \(=\pi=180^\circ\)。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|:--|:--|
| [hdl/psi_fix_cordic_vect.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_vect.vhd) | 矢量模式 CORDIC 的可综合 VHDL 实现（PIPELINED 与 SERIAL 两种架构） |
| [model/psi_fix_cordic_vect.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cordic_vect.py) | 配套的 Python 位真模型（黄金参考） |
| [hdl/psi_fix_cordic_rot.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd) | 旋转模式 CORDIC，用于对照二者的对称性 |
| [testbench/psi_fix_cordic_vect_tb/Scripts/preScript.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cordic_vect_tb/Scripts/preScript.py) | 协同仿真脚本：跑 Python 模型，落盘输入/期望输出文本 |
| [testbench/psi_fix_cordic_vect_tb/psi_fix_cordic_vect_tb.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cordic_vect_tb/psi_fix_cordic_vect_tb.vhd) | 自检测试台：重放输入、逐位比对输出 |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl) | 回归配置：声明 cordic_vect 的 5 组参数矩阵 |
| [Changelog.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/Changelog.md) | 记录 4.0.2 的 `rdy_i→rdy_o` 修正 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：

1. **CORDIC 矢量算法**——迭代如何把 \(y\) 驱赶到 0，从而得到模与相位。
2. **模/相位输出与象限处理**——为什么先取绝对值映射到第一象限、输出再按象限补偿。
3. **握手接口与 `rdy_i→rdy_o` 命名修正**——端口方向语义与 4.0.2 的修正动因。

### 4.1 CORDIC 矢量算法

#### 4.1.1 概念说明

给定直角坐标 \((I,Q)\)，我们想求它的极坐标：

\[
r=\sqrt{I^2+Q^2},\qquad \theta=\mathrm{atan2}(Q,I)
\]

直接做平方、开方、反正切在硬件里又贵又慢。CORDIC 给了一个聪明办法：**通过一系列固定角度的微旋转，把向量一步步转到一个已知姿态，边转边把转过的角度记下来。**

矢量模式的目标是「把 \(y\) 转到 0」——也就是把向量转到与 \(x\) 轴重合。每一步根据当前 \(y\) 的符号决定旋转方向，让 \(|y|\) 不断减小。转完之后：

- 累加起来的旋转角度 \(z\) 就是原始向量与 \(x\) 轴的夹角 \(\theta\)；
- 而 \(x\) 被放大了一个固定的 CORDIC 增益 \(G_N\)，所以 \(x_{\text{终}}=G_N\cdot r\)。

关键吸引力：**每一步只用一次移位（乘 \(2^{-i}\)）和一次加减**，完全不需要乘法器来算三角函数。

#### 4.1.2 核心流程

第 \(i\) 次迭代（\(i=0,1,\dots,N-1\)），令旋转方向 \(d_i=-\mathrm{sign}(y_i)\)（让 \(y\) 趋向 0）：

\[
\begin{aligned}
x_{i+1} &= x_i - d_i\,y_i\,2^{-i}\\
y_{i+1} &= y_i + d_i\,x_i\,2^{-i}\\
z_{i+1} &= z_i - d_i\,\alpha_i,\qquad \alpha_i=\frac{\arctan(2^{-i})}{2\pi}\text{（单位：圈）}
\end{aligned}
\]

初值 \(z_0=0\)。迭代 \(N\) 次后，当 \(y_0\ge0,\,x_0\ge0\)（第一象限）时：

\[
x_N = G_N\cdot\sqrt{x_0^2+y_0^2},\qquad z_N=\mathrm{atan2}(y_0,x_0),\qquad G_N=\prod_{i=0}^{N-1}\sqrt{1+2^{-2i}}\approx 1.6468
\]

要点：

- **角度表** \(\alpha_i=\arctan(2^{-i})/2\pi\) 是常数，预先量化成定点存进 ROM（`AngleTable_c`）。
- **CORDIC 增益** \(G_N\) 也是常数。若想输出真实的 \(r\)，就乘 \(1/G_N\) 补偿；若不在乎绝对幅度（例如只做相位检测），可不补偿省下一个乘法器，输出即为 \(G_N\cdot r\)。
- **收敛范围有限**：CORDIC 只能旋转一定锥角内的向量。下一节会看到，工程上用「先取绝对值折到第一象限」绕开这个限制。

> 与旋转模式（u5-l2）的对称性：旋转模式把 **\(z\) 驱赶到 0**（输入目标角 \(\theta\)，输出旋转后的 \((x,y)\)）；矢量模式把 **\(y\) 驱赶到 0**（输入 \((x,y)\)，输出角度 \(z\)）。两者共享同一组「移位 + 加减 + 查角度表」的迭代内核，**唯一区别是决定旋转方向所看的那个符号**——旋转看 \(\mathrm{sign}(z)\)，矢量看 \(\mathrm{sign}(y)\)。

#### 4.1.3 源码精读

**(1) 角度表与增益——VHDL 侧**

角度表用 32 个 `arctan(2^-i)/(2π)` 实数预存，再按 `iterations_g` 截取前 \(N\) 个、量化成 `angle_int_fmt_g`：

[hdl/psi_fix_cordic_vect.vhd:57-76](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_vect.vhd#L57-L76) —— `AngleTableReal_c` 给出 \(\alpha_0=0.125\) 圈（即 \(45^\circ\)）等常数，`AngleTableStdlv` 函数用 `psi_fix_from_real` 把它们量化进定点。

增益用 `CordicGain` 函数连乘 \(\sqrt{1+2^{-2i}}\) 算出，再取倒数量化成系数：

[hdl/psi_fix_cordic_vect.vhd:78-90](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_vect.vhd#L78-L90) —— `GcFmt_c=(0,0,17)` 是增益补偿系数的定点格式，`GcCoef_c=1/CordicGain(N)`。

> 注意：增益补偿系数格式 `(0,0,17)`（17 位小数、无符号）是一个刻意选择——它使补偿乘法 `x · GcCoef` 的两个操作数合起来能落入单个 DSP48 slice，与文档建议一致（见 `doc/files/psi_fix_cordic_vect.md`）。

**(2) 三步迭代函数——VHDL 侧**

三个函数 `CordicStepX/Y/Z` 正好对应上面的三条迭代式，注意它们**共用同一个判据 `signed(yLast) < 0`**（即 \(d_i\) 由 \(y\) 符号决定），这正是矢量模式的签名：

[hdl/psi_fix_cordic_vect.vhd:96-147](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_vect.vhd#L96-L147) —— `CordicStepX` 先用 `psi_fix_shift_right` 把 \(y\) 右移 \(i\) 位（乘 \(2^{-i}\)），再按 \(y\) 符号选择加/减；`CordicStepZ` 则按 \(y\) 符号选择从 \(z\) 里加/减一个 \(\alpha_i\)。

对照旋转模式的同名函数，可以看到判据的差别直接体现在**函数签名**上：

[hdl/psi_fix_cordic_rot.vhd:131-145](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L131-L145) —— 旋转模式的 `CordicStepZ` 签名只有 `(zLast, iteration)`，判据是 `signed(zLast) > 0`（把 \(z\) 驱向 0）；而矢量的 `CordicStepZ(zLast, yLast, iteration)` 多带了一个 `yLast`，判据是 `signed(yLast) < 0`（把 \(y\) 驱向 0）。一个看 \(z\)、一个看 \(y\)——这就是两种模式的全部算法差异。

**(3) 三步迭代——Python 位真侧**

Python 模型用 `np.where(yLast < 0, ...)` 镜像同一判据，保证两侧逐位一致：

[model/psi_fix_cordic_vect.py:120-136](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cordic_vect.py#L120-L136) —— `_CordicStepX/Y/Z` 与 VHDL 的三个函数一一对应，内部都用 `psi_fix_rnd_t.trunc / psi_fix_sat_t.wrap`，与 VHDL 端的 `psi_fix_trunc / psi_fix_wrap` 对齐。

#### 4.1.4 代码实践

**实践目标**：亲手验证「迭代内核只在判据符号上区分两种模式」这一对称论断。

**操作步骤**（源码阅读型）：

1. 打开 [hdl/psi_fix_cordic_vect.vhd:133-147](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_vect.vhd#L133-L147)（矢量的 `CordicStepZ`）与 [hdl/psi_fix_cordic_rot.vhd:131-145](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L131-L145)（旋转的 `CordicStepZ`）。
2. 逐行比对两者：函数参数列表差了哪一个？判据分别是 `signed(?) < 0` 还是 `> 0`？加减支路是否完全同构？
3. 用同样的方法比对各自的 `CordicStepX` / `CordicStepY`，确认它们的移位 + 加减结构几乎逐字相同。

**需要观察的现象**：除了「决定方向的符号来源」和「\(z\) 是累加还是逼近 0」之外，两条迭代算式在两个文件里是同构的。

**预期结果**：你会得到一张「换一个符号判据 = 换一种坐标变换」的对照表（见 4.2.3 的表）。这正是 CORDIC 设计优美之处，也是 psi_fix 把 rot 与 vect 放成姊妹件的原因。

> 本实践为源码阅读型，无需运行仿真即可完成。

#### 4.1.5 小练习与答案

**练习 1**：矢量模式里，如果某次迭代 \(y_i>0\)，那么 \(d_i\) 取什么值？\(z\) 这一步是加 \(\alpha_i\) 还是减 \(\alpha_i\)？

**答案**：\(d_i=-\mathrm{sign}(y_i)=-1\)。代入 \(z_{i+1}=z_i-d_i\alpha_i\) 得 \(z_{i+1}=z_i+\alpha_i\)，即 **加** \(\alpha_i\)。对照代码 `CordicStepZ` 的 `else`（`yLast>=0`）分支返回 `psi_fix_add(z, Atan)`，一致。

**练习 2**：13 次迭代的 CORDIC 增益 \(G_{13}\) 约为多少？如果不做增益补偿，输入单位圆上一点 \((1,0)\)，输出幅度大约是多少？

**答案**：\(G_{13}=\prod_{i=0}^{12}\sqrt{1+2^{-2i}}\approx 1.6468\)。不补偿时输出 \(x_{13}=G_{13}\cdot r\)，对 \((1,0)\) 有 \(r=1\)，故输出幅度约为 \(1.6468\)。这就是 `gain_comp_g=False` 时幅度被放大的原因。

**练习 3**：为什么 CORDIC 迭代里算 \(\arctan\) 不需要乘法器？

**答案**：因为 \(\arctan(2^{-i})\) 对每个 \(i\) 都是常数，可以预先算好量化进定点存进 `AngleTable_c`（ROM）。迭代中只做「按符号加/减表里查出的常数」，加法器即可，无需运行时算反正切。

### 4.2 模/相位输出与象限处理

#### 4.2.1 概念说明

CORDIC 的收敛锥只有约 \(\pm 99.9^\circ\)，无法直接处理任意象限的向量。`psi_fix_cordic_vect` 的对策是：

1. **入口取绝对值**：把 \((I,Q)\) 的符号位记下来（2 bit 记象限），对 \(I,Q\) 各取绝对值，向量被「折」进第一象限（\(x_0\ge0,\,y_0\ge0\)），落入收敛锥。
2. **在第一象限跑 CORDIC**：得到 \(z=\mathrm{atan2}(|Q|,|I|)\in[0,\,0.25]\) 圈，以及 \(x=G_N\cdot r\)。
3. **出口按象限补偿角度**：用记下的 2 bit 象限，把第一象限的 \(z\) 还原成真实相位。

幅度 \(r\) 与象限无关（取绝对值不改变模长），所以幅度支路不需要象限补偿；只有相位支路需要。

#### 4.2.2 核心流程

象限编号 `Quad = sign(I) & sign(Q)`（高位是 \(I\) 符号，低位是 \(Q\) 符号）。相位还原规则（单位：圈，\(1.0=2\pi\)）：

| Quad | 象限（原始 \(I,Q\)） | 还原相位 |
|:--|:--|:--|
| `00` | \(I\ge0,\,Q\ge0\)（第一象限） | \(\theta=z\) |
| `10` | \(I<0,\,Q\ge0\)（第二象限） | \(\theta=0.5-z\) |
| `11` | \(I<0,\,Q<0\)（第三象限） | \(\theta=0.5+z\) |
| `01` | \(I\ge0,\,Q<0\)（第四象限） | \(\theta=1.0-z\) |

幅度支路：若 `gain_comp_g=True`，\(r=x\cdot(1/G_N)\)；否则直接输出 \(x\)（即 \(G_N\cdot r\)）。

> 设计上有一个细节：为了能表示 \(0.5\) 和 \(1.0\) 这两个补偿常数，内部角度格式被扩展了一位整数位——`AngleIntExtFmt=(angle_int_fmt_g.S, max(angle_int_fmt_g.I,1), angle_int_fmt_g.F)`。例如默认 `angle_int_fmt_g=(1,0,18)` 时，`AngleIntExtFmt=(1,1,18)`，这样有符号格式 \((1,1,18)\)（范围约 \([-2,+2)\)）才能装下 \(1.0\)。

#### 4.2.3 源码精读

**(1) 入口取绝对值 + 记象限——VHDL 流水版**

[hdl/psi_fix_cordic_vect.vhd:195-204](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_vect.vhd#L195-L204) —— `psi_fix_abs` 对 \(I,Q\) 取绝对值得到 `XAbs/YAbs`（注释里用 `attribute use_dsp48 ... "no"` 明确禁止综合进 DSP，因为取绝对值的进位链在 DSP 里反而慢）；`QuadAbs` 拼接两个符号位；随后 resize 进 `internal_fmt_g` 送入迭代。

**(2) 出口象限补偿——VHDL 流水版**

[hdl/psi_fix_cordic_vect.vhd:222-234](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_vect.vhd#L222-L234) —— 幅度按 `gain_comp_g` 选择「乘 `GcCoef`」或「直接 resize」；相位用 `case Quad` 四选一，分别对应上表的 \(z,\,0.5-z,\,0.5+z,\,1.0-z\)，常数 `AngInt_0_5_c`/`AngInt_1_0_c` 用 `AngleIntExtFmt` 表示。串行版的同一逻辑在 [hdl/psi_fix_cordic_vect.vhd:300-314](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_vect.vhd#L300-L314)。

**(3) Python 位真镜像——象限补偿**

[model/psi_fix_cordic_vect.py:103-115](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cordic_vect.py#L103-L115) —— `zQ1..zQ4` 用 `psi_fix_sub/add` 算出四个候选相位，`np.select` 按原始输入 \((I,Q)\) 的符号四选一；幅度同样按 `gainComp` 选择乘补偿或直接 resize。结构与 VHDL 的 `case Quad` 完全同构。

**(4) 协同仿真如何覆盖两个分支**

测试台根据 `gain_comp_g` 选不同的期望文件，配合 `config.tcl` 的参数矩阵跑两轮：

[testbench/psi_fix_cordic_vect_tb/psi_fix_cordic_vect_tb.vhd:73](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cordic_vect_tb/psi_fix_cordic_vect_tb.vhd#L73) —— `RespFileName_c` 在 `outputWithGc.txt` 与 `outputWithNoGc.txt` 之间二选一。

[testbench/psi_fix_cordic_vect_tb/Scripts/preScript.py:51-94](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cordic_vect_tb/Scripts/preScript.py#L51-L94) —— preScript 实例化两个模型（一个带 GC、一个不带），各跑一遍 `Process()`，再经 `psi_fix_get_bits_as_int` 把幅度与相位都写成位模式有符号整数，落盘成 `outputWithGc.txt` / `outputWithNoGc.txt`，供两轮回归复用。

**旋转 vs 矢量对称对照表**（把 4.1 的结论收口）：

| 维度 | 旋转模式 `cordic_rot`（u5-l2） | 矢量模式 `cordic_vect`（本讲） |
|:--|:--|:--|
| 坐标变换 | 极坐标 → 直角坐标 \((r,\theta)\to(I,Q)\) | 直角坐标 → 极坐标 \((I,Q)\to(r,\theta)\) |
| 驱动目标 | 把 \(z\) 驱赶到 0 | 把 \(y\) 驱赶到 0 |
| 方向判据 | \(d_i=-\mathrm{sign}(z_i)\) | \(d_i=-\mathrm{sign}(y_i)\) |
| \(z\) 初值 | 目标角度 \(\theta\) | 0 |
| \(z\) 终值 | \(\approx 0\) | \(\approx\mathrm{atan2}(Q,I)\) |
| 主输出 | 旋转后的 \((x,y)=\) \((I,Q)\) | 幅度 \(x=G_N r\)、相位 \(z=\theta\) |
| 增益补偿 | 可选乘 \(1/G_N\) | 可选乘 \(1/G_N\) |
| 内部角度格式约束 | `angle_int_fmt_g=(1,-2,x)` | `angle_int_fmt_g=(1,0,x)`，并扩展一位整数位 |

#### 4.2.4 代码实践

**实践目标**：理解 preScript 的刺激设计如何同时覆盖「角度扫描」与「随机点」两类场景。

**操作步骤**（源码阅读型）：

1. 阅读 [testbench/psi_fix_cordic_vect_tb/Scripts/preScript.py:38-48](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cordic_vect_tb/Scripts/preScript.py#L38-L48)。
2. 观察它构造了两段输入：`sigLogicI/Q` 用 `np.linspace` 在 \([0,2\pi)\) 扫角度、幅度从 0.01 到 0.99 扫幅度（均匀扫过所有象限与所有幅度）；`sigRandI/Q` 用 `np.random.seed(0)` 固定种子的随机点覆盖单位圆内部。
3. 思考：为什么既需要「角度/幅度均匀扫描」又需要「固定种子随机」？

**需要观察的现象**：逻辑扫描保证每个象限、每个角度区间、从近 0 到接近 1 的幅度都被命中（用来逼出象限补偿分支与边界行为）；固定随机点则补充统计性的逐位精度验证，且种子固定使结果可复现。

**预期结果**：你能解释「逻辑扫描管分支覆盖、随机点管精度统计」这一刺激设计分工——这正是 u3-l1「最坏情况刺激」思想在 CORDIC 上的落地。

> 若本地已配好 Python 环境（NumPy/SciPy），可直接 `cd testbench/psi_fix_cordic_vect_tb/Scripts && python3 preScript.py` 观察它在 `Data/` 下生成三个 `.txt`；若未配环境，本实践作为源码阅读型完成即可（运行结果待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：原始输入 \((-0.6,\,0.8)\)（\(I<0,Q\ge0\)），取绝对值后 CORDIC 算得 \(z=0.165\) 圈。最终输出相位是多少圈？约多少度？

**答案**：Quad=`10`（第二象限），相位 \(\theta=0.5-z=0.5-0.165=0.335\) 圈 \(\approx 120.6^\circ\)。校验：\(\mathrm{atan2}(0.8,-0.6)\approx 126.9^\circ\)，差异来自 \(z\) 的迭代近似与定点量化。

**练习 2**：为什么幅度支路不需要按象限补偿，而相位支路需要？

**答案**：取绝对值只改变向量方向、不改变模长，\(\sqrt{I^2+Q^2}=\sqrt{|I|^2+|Q|^2}\)，所以无论原始在第几象限，幅度都由第一象限的 CORDIC 直接给出。而相位与方向强相关，取绝对值丢了方向信息，必须用记下的象限位把第一象限角度还原成真实相位。

**练习 3**：默认 `angle_int_fmt_g=(1,0,18)`。`AngleIntExtFmt` 是什么？为什么需要它？

**答案**：`AngleIntExtFmt=(1,1,18)`——把整数位从 0 扩到 `max(0,1)=1`。因为象限补偿要表示常数 \(0.5\) 和 \(1.0\)，而有符号 \((1,0,18)\) 的范围是 \([-1,\,+1)\)，装不下 \(+1.0\)；扩成 \((1,1,18)\)（范围约 \([-2,+2)\)）后才能装下。

### 4.3 握手接口与 `rdy_i→rdy_o` 命名修正

#### 4.3.1 概念说明

回顾 u1-l4 的 AXI4-Stream 握手：数据在 `vld`（TVALID）与 `rdy`（TREADY）同拍为高时完成传递。这里要厘清一个**方向**问题：

- 对**接收数据的一方**（本组件的输入流）来说，`rdy` 是「我（接收方）是否准备好接收」的信号——它由**本组件自己产生**，告诉上游「现在能不能给我数据」。
- 因此从**本实体的端口方向**看，输入流的 `rdy` 是一个**输出端口**（`out`）：它是本组件对外声明的能力，不是从外部读进来的。

PSI 命名约定（u1-l4）：端口后缀 `_i` 表示输入端口（`in`）、`_o` 表示输出端口（`out`）。所以一个由本组件驱动、表示「我准备好接收输入」的 ready 信号，**正确的名字应是 `rdy_o`**——它是输出端口、却被错误地叫成 `rdy_i`（看起来像输入端口），就是 4.0.2 修正的 bug。

#### 4.3.2 核心流程

两种架构下，ready 的产生方式不同：

- **PIPELINED**：每个时钟都能吃一个样本，所以 ready 恒为 1，`rdy_o <= '1'`。
- **SERIAL**：一个样本要占多个时钟迭代，组件用 `XinVld` 标志「输入寄存器里是否已排队了一个待算样本」，于是 `rdy_o <= not XinVld`——还没排队时才 ready。

无论哪种，`rdy` 都是组件**自己产生并对外输出**的信号，端口方向是 `out`。

#### 4.3.3 源码精读

**(1) 修正后的端口声明**

[hdl/psi_fix_cordic_vect.vhd:40-50](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_vect.vhd#L40-L50) —— 注意 `rdy_o : out std_logic`：方向是 `out`，名字后缀是 `_o`，二者一致了。它紧挨着输入端口 `vld_i/dat_inp_i/dat_qua_i`（这些都是 `in`，后缀 `_i`）。

**(2) ready 的产生**

- 流水版恒高：[hdl/psi_fix_cordic_vect.vhd:182](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_vect.vhd#L182) —— `rdy_o <= '1';`
- 串行版带反压：[hdl/psi_fix_cordic_vect.vhd:254](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_vect.vhd#L254) —— `rdy_o <= not XinVld;`，配合 [hdl/psi_fix_cordic_vect.vhd:266-271](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_vect.vhd#L266-L271) 的输入锁存逻辑。

**(3) Changelog 记录的修正**

[Changelog.md:1-3](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/Changelog.md#L1-L3) —— 4.0.2 的 Bugfixes 明确写「renamed rdy_i to rdy_o in psi_fix_cordic_vect」。`psi_fix_cordic_vect` 最早在 [Changelog.md:209](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/Changelog.md#L209)（1.5.0）引入。

**(4) 修正带来的连带改动**

测试台把 DUT 的 `rdy_o` 接到本地的 `InRdy` 信号，并把它回送给 `ApplyTextfileContent` 作为输入流的反压：

[testbench/psi_fix_cordic_vect_tb/psi_fix_cordic_vect_tb.vhd:99](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cordic_vect_tb/psi_fix_cordic_vect_tb.vhd#L99) —— `rdy_o => InRdy`（端口名随源码一起改了）。
[testbench/psi_fix_cordic_vect_tb/psi_fix_cordic_vect_tb.vhd:156-161](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cordic_vect_tb/psi_fix_cordic_vect_tb.vhd#L156-L161) —— `ApplyTextfileContent(..., Rdy => InRdy, Vld => InVld, ...)`，可见 `InRdy` 是 DUT 输出、用作重放时的反压握手。

> 一个值得注意的「文档滞后」：由 `hdl2md` 自动生成的 [doc/files/psi_fix_cordic_vect.md:46](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/psi_fix_cordic_vect.md#L46) 接口表里**仍然写着 `rdy_i`**（虽然 In/Out 列标的是 `o`）。源码已改、文档未同步刷新——这反过来印证了：旧名 `rdy_i` 曾长期存在，且自动产物的更新可能落后于手改的源码。读文档遇到与源码不一致时，**以源码为准**。

> 顺带对比姊妹件 `cordic_rot`：它的 ready 仍叫 `rdy_i` 且方向是 `out`（见 [hdl/psi_fix_cordic_rot.vhd:45](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L45)）——同一个命名 bug 在 vect 上已修、在 rot 上尚未修。这是初学者读源码时常遇到的「同构组件却命名不一致」的真实案例。

#### 4.3.4 代码实践

**实践目标**（即本讲指定的实践任务）：对照 Changelog 中「renamed rdy_i to rdy_o」的修正，在 `cordic_vect.vhd` 中定位该端口，并解释为何它是输出。

**操作步骤**：

1. 在 [hdl/psi_fix_cordic_vect.vhd:46](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_vect.vhd#L46) 定位端口 `rdy_o : out std_logic`。
2. 确认它的方向是 `out`，且在架构体里被赋值（`rdy_o <= '1'` 或 `rdy_o <= not XinVld`），即由本组件**驱动**。
3. 对照未修正的 [hdl/psi_fix_cordic_rot.vhd:45](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L45)（`rdy_i : out std_logic`），体会旧名「方向是 out 却叫 `_i`」的违和。
4. 回答：为什么这个 ready 是输出？

**需要观察的现象**：`rdy_o` 是输入数据流的握手 ready，由接收方（本 CORDIC 组件）产生，表达「我现在能否再吃一个样本」。

**预期结果**：你能用自己的话解释——在 AXI4-Stream 里，输入流的 `rdy`（TREADY）方向永远朝向上游发送方，所以对接收数据的本实体而言它是**输出端口**；PSI 约定用 `_o` 标输出端口，故 `rdy_o` 名副其实，旧名 `rdy_i` 是误导性的命名 bug。把 `Rdy => InRdy` 接进测试台的 `ApplyTextfileContent` 也正说明：它是 DUT 给出的、用来反压输入重放的信号。

> 本实践为源码阅读型，无需运行仿真。

#### 4.3.5 小练习与答案

**练习 1**：假如某个组件的输入流 ready 真的是一个 `in` 端口（叫 `rdy_i`），那它会代表什么语义？与本讲的 `rdy_o` 有何不同？

**答案**：若 `rdy` 是 `in`，意味着本组件的输入能否被接收由**外部**决定（例如本组件不是数据流的终点，而是把 ready 透传出去）。本讲的 `rdy_o` 是本组件**自己**根据内部状态（流水恒 ready / 串行看 `XinVld`）产生的反压信号，方向是 `out`。两者方向相反、语义不同，这正是命名必须用 `_i/_o` 区分的原因。

**练习 2**：串行模式下，`rdy_o <= not XinVld`。如果一个样本刚被锁进 `Xin`（`XinVld` 变 1），`rdy_o` 会怎样？这对上游意味着什么？

**答案**：`XinVld=1` 时 `rdy_o=0`，组件告诉上游「我现在不能收」。上游必须把 `vld` 保持住直到 `rdy_o` 重新拉高（即排队样本被搬进工作寄存器、`XinVld` 清 0）。这是标准的 AXI-S 反压握手，保证串行 CORDIC 慢速迭代时不丢样本。

**练习 3**：`config.tcl` 里 cordic_vect 跑了几组参数？分别覆盖了什么？

**答案**：见 [sim/config.tcl:316-324](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L316-L324)，共 5 组：2 组 PIPELINED（带 GC、`pl_stg_per_iter_g=1/2`）、1 组 PIPELINED（不带 GC）、1 组 SERIAL（带 GC）、1 组 SERIAL（不带 GC）。覆盖了两种架构 × 两种增益补偿，外加流水版每级迭代 1 或 2 个流水段的时序选项。

## 5. 综合实践

把本讲三个模块串起来，完成一次「端到端读懂一个 CORDIC 矢量样本如何流过组件」的源码追踪：

1. **入口（4.2）**：从 [hdl/psi_fix_cordic_vect.vhd:195-204](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_vect.vhd#L195-L204) 出发，跟踪一个输入样本 \((I,Q)\)：它如何被取绝对值、符号位如何被拼成 `Quad`、如何 resize 进 `internal_fmt_g`。
2. **握手（4.3）**：说明这个样本在什么条件下会被接收（PIPELINED 恒收；SERIAL 看 `rdy_o=not XinVld`），并指出 `rdy_o` 为何是输出端口。
3. **迭代（4.1）**：跟踪样本流过 \(N\) 级 CORDIC 迭代（流水版是展开的 `for i in 0 to iterations_g-1` 循环 [hdl/psi_fix_cordic_vect.vhd:209-219](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_vect.vhd#L209-L219)；串行版是 `IterCnt` 计数循环 [hdl/psi_fix_cordic_vect.vhd:274-297](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_vect.vhd#L274-L297)），每一步 \(x/y/z\) 如何被 `CordicStepX/Y/Z` 更新。
4. **出口（4.2）**：跟踪 [hdl/psi_fix_cordic_vect.vhd:222-234](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_vect.vhd#L222-L234) 如何用记下的 `Quad` 把 \(z\) 还原成真实相位、用 `gain_comp_g` 决定幅度是否乘 \(1/G_N\)。
5. **位真对账**：对照 [model/psi_fix_cordic_vect.py:85-115](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cordic_vect.py#L85-L115) 的 `Process()`，确认 Python 模型以完全相同的「取绝对值 → 迭代 → 象限补偿 → 增益补偿」顺序算出期望值，再由 preScript 经 `psi_fix_get_bits_as_int` 落盘，最终在测试台里与 VHDL 输出逐位比对。

**交付物**：画一张「输入 \((I,Q)\) → 绝对值/象限 → \(N\) 级迭代 → 象限补偿相位 + 增益补偿幅度 → 输出 \((r,\theta)\)」的数据流图，并在图上标注每一步对应的源码行号与所用的 `psi_fix_*` 函数。这张图同时适用于流水版与串行版（差别只在「展开成空间上的多级」还是「复用一组寄存器在时间上多拍」）。

## 6. 本讲小结

- CORDIC **矢量模式**用「移位 + 加减 + 查角度表」把向量转到与 \(x\) 轴重合（把 \(y\) 驱到 0），边转边累加角度，从而由直角坐标 \((I,Q)\) 算出幅度与相位，全程不需要三角函数乘法器。
- 矢量模式与旋转模式共享同一迭代内核，**唯一差别是方向判据**：矢量看 \(\mathrm{sign}(y)\)、旋转看 \(\mathrm{sign}(z)\)，这直接体现在 `CordicStepZ` 的函数签名与判据上。
- 为了绕开有限收敛锥，入口先取绝对值把向量折进第一象限、记下 2 bit 象限；出口再按象限把第一象限相位还原成 \([0,1)\) 圈的真实相位。幅度与象限无关，不需补偿。
- CORDIC 引入固定增益 \(G_N\approx1.6468\)，`gain_comp_g` 控制是否乘 \(1/G_N\) 补偿；补偿系数格式刻意选成 `(0,0,17)` 以落入单个 DSP。
- 组件提供 PIPELINED（1 样本/时钟、资源大，多一个 `pl_stg_per_iter_g` 时序旋钮）与 SERIAL（约 \(N\) 时钟/样本、资源小，带 `rdy_o` 反压）两种架构，且都有 Python 位真模型 + preScript 协同仿真逐位验证。
- 4.0.2 把输入流的 ready 从误导性的 `rdy_i` 改成 `rdy_o`——因为对接收数据的实体而言，TREADY 是由本组件产生、对外输出的反压信号，端口方向是 `out`；姊妹件 `cordic_rot` 尚未做此修正，自动生成的文档表也还停留在旧名，读源码时以源码为准。

## 7. 下一步学习建议

- **横向对照**：回到 [hdl/psi_fix_cordic_rot.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd)，把本讲的「对称对照表」逐项在源码里落实，彻底吃透 rot/vect 这对姊妹件。
- **另一种求模/极坐标变换**：本系列的 u5-l1 讲过 `psi_fix_complex_abs`（用平方+求和+线性近似开方求模）与 u8 系列会讲 `psi_fix_pol2cart_approx`（基于正弦近似的极坐标→直角坐标）。学完本讲后，可对比「CORDIC 法」与「近似/LUT 法」在资源、精度、延迟上的取舍。
- **应用层**：CORDIC 矢量模式常用于幅度/相位检测与解调。学完 u9 的 `psi_fix_demod_real2cplx`、`psi_fix_phase_unwrap` 后，你会看到本组件产出的相位如何被后续的相位解卷绕消费。
- **贡献新算术组件**：本组件是「自创算术 + Python 位真模型 + preScript 协同仿真」的典型样板（与 u4-l3 的二进制除法同类）。若你要贡献新组件，可参考 u10-l1 的五件套流程，以 `psi_fix_cordic_vect` 为模板。
