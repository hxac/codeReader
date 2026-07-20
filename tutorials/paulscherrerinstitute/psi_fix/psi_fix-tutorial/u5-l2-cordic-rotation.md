# CORDIC 旋转模式

## 1. 本讲目标

本讲带读者读懂 `psi_fix_cordic_rot`——psi_fix 库中把**极坐标（幅度 + 相位）变换为直角坐标（同相 I + 正交 Q）**的可综合组件。它最典型的用途是数字下变频（DDC）里的数控振荡器（NCO）输出、复数本振生成等。

读完本讲，你应当能够：

1. 说清 CORDIC **旋转模式 (rotation mode)** 的迭代微旋转算法，以及它为何只需要「移位 + 加减」、不需要乘法器。
2. 理解组件提供的 `SERIAL` 与 `PIPELINED` 两种架构的**吞吐-资源**取舍，并能粗略估算处理一个样本所需的时钟周期。
3. 理解 CORDIC 增益的产生原因、`gain_comp_g` 增益补偿开关，以及 `round_g / sat_g` 在 VHDL 与 Python 位真模型两侧的镜像关系。
4. 注意到一个**重要事实**：`pl_stg_per_iter`（每级迭代的流水粒度）这个旋钮**不在** `cordic_rot` 中，而是存在于它的姊妹组件 `psi_fix_cordic_vect`（矢量模式，下一讲 u5-l3）。`cordic_rot` 的流水实现是把每一级迭代硬展开为恰好一级寄存器。

> 本讲承接 u4-l1（resize_pipe 与 moving average）建立的两段式风格、位增长规则与位真双模型套路；其姊妹讲为 u5-l1（复数运算族）与 u5-l3（CORDIC 矢量模式）。

---

## 2. 前置知识

### 2.1 极坐标与直角坐标

一个二维向量既可以用直角坐标 \((x,y)\) 表示，也可以用极坐标 \((r,\theta)\) 表示。两者关系为：

\[
x = r\cos\theta,\qquad y = r\sin\theta
\]

`cordic_rot` 的输入是 \((r,\theta)\)（`dat_abs_i`、`dat_ang_i`），输出是 \((x,y)\)（`dat_inp_o`、`dat_qua_o`）。注意在本库里 **角度以「圈」为单位**：`dat_ang_i` 取值 \([0,1)\) 表示 \([0, 2\pi)\) 的整圈。

### 2.2 为什么不用查表或乘法器？

直接算 \(\cos\theta\)、\(\sin\theta\) 需要泰勒展开或大查找表，且每个样本要做两次乘法。**CORDIC（COordinate Rotation DIgital Computer）** 给出一种只靠「移位 + 加减」迭代逼近的算法，特别适合 FPGA——移位和加法几乎不耗资源，DSP 乘法器可以省下给真正需要的地方（当然，增益补偿那一步还是会用到一次乘法，见 4.3）。

### 2.3 收敛锥与「半圈折叠」

CORDIC 旋转模式只对**收敛锥内**的角度（约 \(\pm 0.277\) 圈，即略大于 \(\pm\pi/4\)）收敛。要覆盖整圈 \([0,1)\)，组件用了一个巧妙的「半圈折叠」技巧（详见 4.1.2），这正是 `angle_int_fmt_g` 必须是 `(1,-2,x)`（可表示 \(\pm 0.25\) 圈）的原因。

### 2.4 位真双模型（回顾）

每个 VHDL 组件都配一个逐位一致的 Python 模型作为**黄金参考**。`cordic_rot` 也不例外：`hdl/psi_fix_cordic_rot.vhd` 与 `model/psi_fix_cordic_rot.py` 用同一套算式推导中间格式，测试台逐位比对。这一套路已在 u3-l2、u4-l1 反复出现。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [hdl/psi_fix_cordic_rot.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd) | 可综合 VHDL 实体，含 `PIPELINED` 与 `SERIAL` 两套 `generate` 实现 |
| [model/psi_fix_cordic_rot.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cordic_rot.py) | Python 位真模型（黄金参考） |
| [testbench/psi_fix_cordic_rot_tb/psi_fix_cordic_rot_tb.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cordic_rot_tb/psi_fix_cordic_rot_tb.vhd) | 自检测试台（由 TbGen.py 生成模板） |
| [testbench/psi_fix_cordic_rot_tb/Scripts/preScript.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cordic_rot_tb/Scripts/preScript.py) | 协同仿真数据生成脚本 |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl) | 回归配置，声明 cordic_rot_tb 的 4 组参数 |
| [hdl/psi_fix_cordic_vect.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_vect.vhd) | 姊妹组件（矢量模式），用于对照说明 `pl_stg_per_iter` |

---

## 4. 核心概念与源码讲解

### 4.1 CORDIC 旋转算法

#### 4.1.1 概念说明

CORDIC 旋转模式的核心思想：**把「旋转一个固定角度 \(\theta\)」分解成若干次「微旋转」**，每次微旋转的角度是 \(\arctan(2^{-i})\)（即第 \(i\) 次只需右移 \(i\) 位）。由于每次微旋转只做「移位 + 加减」，整个旋转不依赖乘法器。

设当前向量为 \((x_i, y_i)\)、剩余未旋转角度为 \(z_i\)。第 \(i\) 次微旋转：

\[
\begin{aligned}
x_{i+1} &= x_i - \sigma_i \cdot y_i \cdot 2^{-i}\\
y_{i+1} &= y_i + \sigma_i \cdot x_i \cdot 2^{-i}\\
z_{i+1} &= z_i - \sigma_i \cdot \arctan(2^{-i})
\end{aligned}
\]

其中方向符号 \(\sigma_i = +1\) 当 \(z_i > 0\)（顺时针补，向目标角靠拢），否则 \(\sigma_i = -1\)。经过 \(N\) 次迭代后 \(z\to 0\)，向量被旋转了 \(\theta\)。

本组件里 \(\sigma_i\) 的实现就是把 `z` 的符号作为选择信号：`signed(zLast) > 0` 时做减/加一组，否则做加/减一组（注意 X 与 Y 的加减方向**相反**，因为它们是旋转矩阵的两行）。

#### 4.1.2 核心流程：初始化 → 迭代 → 象限校正 → 增益校正

```text
输入 (r=dat_abs, θ=dat_ang)，θ ∈ [0,1) 圈
   │
   1. 初始化
   │    x0 = resize(r, internalFmt)        # 幅度
   │    y0 = 0
   │    z0 = resize(θ, angleIntFmt)        # 半圈折叠进 ±0.25 圈
   │    quad = 取 θ 的最高 2 位小数位        # 象限/折叠标记
   │
   2. CORDIC 迭代 i = 0 .. N-1
   │    x_{i+1} = x_i ∓ (y_i >> i)         # CordicStepX
   │    y_{i+1} = y_i ± (x_i >> i)         # CordicStepY
   │    z_{i+1} = z_i ∓ atanTable[i]       # CordicStepZ
   │
   3. 象限校正（撤销半圈折叠）
   │    若 quad ∈ {00, 11}: (x,y) 不变
   │    若 quad ∈ {01, 10}: (x,y) 同时取负
   │
   4. 增益校正（可选）
   │    若 gain_comp_g: (x,y) *= 1/CordicGain(N)
   │
   5. resize 到 out_fmt_g 输出 (I, Q)
```

**半圈折叠的几何直觉**：把角度 \(\theta\) 折叠进 \([-0.25, +0.25)\) 圈（即 ±π/4，落在收敛锥内）等价于对 \(\theta\) 做「模 0.5 圈」运算。而把一个角度减去 0.5 圈（180°）恰好让 \(\cos\)、\(\sin\) 同时变号：

\[
\cos(z+\pi) = -\cos z,\qquad \sin(z+\pi) = -\sin z
\]

于是组件用 `θ` 的最高 2 位小数位 `quad` 来判断**折叠时是否跨过了奇数个 180°**：`quad` 为 `00`/`11` 时跨过偶数个（不变号），`quad` 为 `01`/`10` 时跨过奇数个（同时取负）。这就是 4.1.3 里那段「同时取负」的来历——它不是普通象限旋转，而是 180° 折叠的补偿。

#### 4.1.3 源码精读

**(a) 角度表 `AngleTableReal_c`**——预计算 \(\arctan(2^{-i})/(2\pi)\)（单位：圈），共 32 项，最多支持 32 次迭代。第 0 项 `0.125` 正是 \(\arctan(1)/(2\pi) = (\pi/4)/(2\pi) = 1/8\)。

[hdl/psi_fix_cordic_rot.vhd:56-63](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L56-L63) 预计算 arctan 表（单位为「圈」）。

`AngleTableStdlv` 函数把浮点表在综合期量化为 `angle_int_fmt_g` 定点：

[hdl/psi_fix_cordic_rot.vhd:66-75](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L66-L75) 用 `psi_fix_from_real` 把 arctan 表量化为定点常量数组。

**(b) 三个微旋转函数**——X、Y、Z 各一个，全部用 `psi_fix_shift_right` + `psi_fix_add`/`psi_fix_sub`，注意 X 与 Y 的加减方向相反：

[hdl/psi_fix_cordic_rot.vhd:93-110](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L93-L110) `CordicStepX`：`z>0` 时 \(x - (y\!>\!i)\)，否则 \(x + (y\!>\!i)\)。

[hdl/psi_fix_cordic_rot.vhd:113-129](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L113-L129) `CordicStepY`：方向与 X 相反，`z>0` 时 \(y + (x\!>\!i)\)。

[hdl/psi_fix_cordic_rot.vhd:132-145](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L132-L145) `CordicStepZ`：从剩余角度中减/加 `AngleTable_c(iteration)`。

这里出现的 `psi_fix_shift_right(..., true)`（动态移位）正是 u2-l2 讲过的、为 Vivado 可综合性而特殊实现的移位函数，最后一参 `iterations_g - 1` 即 `maxShift`。

**(c) 半圈折叠与象限校正**——`QuadFmt_c = (0,0,2)` 取角度最高 2 位小数位；初始化与校正逻辑如下：

[hdl/psi_fix_cordic_rot.vhd:86-89](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L86-L89) 定义 `QuadFmt_c`（2 位无符号小数）等内部常量。

[hdl/psi_fix_cordic_rot.vhd:203-210](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L203-L210) 流水实现的象限校正：`Quad` 为 `00`/`11` 时透传，否则用 `psi_fix_neg` 同时对 X、Y 取负。

**(d) Python 镜像**——`Process` 方法与 VHDL 完全同构，是黄金参考：

[model/psi_fix_cordic_rot.py:91-128](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cordic_rot.py#L91-L128) 初始化 → 迭代循环 → 象限校正 → 增益校正，与 VHDL 一一对应。

[model/psi_fix_cordic_rot.py:115-118](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cordic_rot.py#L115-L118) Python 侧象限校正用 `np.select` 在 `quad==0/0.25/0.5/0.75` 上选择是否取负，与 VHDL 的 `"00"/"11"` 判断同义。

**(e) 约束断言**——VHDL 与 Python 都对格式做防呆，且条件完全一致：

[hdl/psi_fix_cordic_rot.vhd:155-161](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L155-L161) 断言：角度输入无符号、内部角度格式必须 `(1,-2,x)`、幅度输入无符号、内部格式必须有更多整数位等。

[model/psi_fix_cordic_rot.py:55-60](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cordic_rot.py#L55-L60) Python 侧抛出完全对应的 `ValueError`。

#### 4.1.4 代码实践：验证「半圈折叠」的几何含义

1. **实践目标**：亲手验证「角度折叠进 ±π/4 后，跨奇数个 180° 时 \((\cos,\sin)\) 同时变号」这一论断。
2. **操作步骤**：
   - 取三个角度：\(a_1=0.1\) 圈、\(a_2=0.3\) 圈、\(a_3=0.6\) 圈。
   - 对每个 \(a\)，分别写出其 `quad`（最高 2 位小数位，即 `round(a*4)` 的 2 位二进制）、折叠后残差 \(r = ((a + 0.25) \bmod 0.5) - 0.25\)。
   - 计算 \((\cos(2\pi a), \sin(2\pi a))\) 与 \((\cos(2\pi r), \sin(2\pi r))\)，比较二者是否仅差一个整体变号。
3. **需要观察的现象**：
   - \(a_1=0.1\)：`quad=00`（0），\(r=0.1\)，不变号。
   - \(a_2=0.3\)：`quad=01`（0.25），\(r=-0.2\)，\((\cos,\sin)\) 整体变号。
   - \(a_3=0.6\)：`quad=10`（0.5），\(r=0.1\)，\((\cos,\sin)\) 整体变号。
4. **预期结果**：`quad` 落在 `00`/`11` 时无需变号，落在 `01`/`10` 时需对 X、Y 同时取负——与源码第 [203-210](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L203-L210) 行的逻辑一致。
5. 若不便手算，可在 Python 里用 `np.cos/np.sin` 直接验证（属「源码阅读型实践」，未实际运行命令）。

#### 4.1.5 小练习与答案

**练习 1**：为何 `angle_int_fmt_g` 必须是 `(1,-2,x)`？如果把整数位改成 `-1`（即 `(1,-1,x)`）会怎样？

**答案**：`(1,-2,x)` 的可表示范围是 \([-0.25, +0.25)\) 圈，恰好把残差角装进 CORDIC 的收敛锥（约 ±0.277 圈）。若改成 `(1,-1,x)`，范围变为 ±0.5 圈，超出收敛锥，CORDIC 无法把 \(z\) 收敛到 0，结果出错；这也是源码断言 `angle_int_fmt_g.I = -2` 的原因（第 [157](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L157) 行）。

**练习 2**：第 0 级微旋转的 `shift=0`，此时 `psi_fix_shift_right(y, fmt, 0, ...)` 结果是什么？这一级是否退化成了纯加减？

**答案**：右移 0 位等于不变，所以第 0 级就是 \(x \mp y\)、\(y \pm x\) 的纯加减（旋转 45°，对应 arctan 表第 0 项 0.125 圈）。这正解释了为何 CORDIC 收敛范围以 ±45° 为基础。

---

### 4.2 串行/流水架构

#### 4.2.1 概念说明

同一个 CORDIC 算法可以用两种硬件架构实现，由 generic `mode_g` 选择：

- **`PIPELINED`（流水）**：把 \(N\) 次迭代**空间展开**成 \(N\) 级流水线，每级一组加法器/减法器/移位器。每个时钟周期可以**吞入一个样本**（吞吐 1 样本/周期），但资源随迭代次数线性增长。
- **`SERIAL`（串行）**：只保留**一组** X/Y/Z 寄存器和**一组**运算逻辑，靠一个 `IterCnt` 计数器在 \(N\) 个周期内**分时复用**。资源近似常数（与 \(N\) 无关），但吞吐降到约 1 样本/\(N\) 周期。

两者的**延迟（latency）相近**（都是 \(N\) 加几个寄存器级），区别主要在**吞吐与资源**——这是经典的「面积换吞吐」取舍。

> **关于 `pl_stg_per_iter`（重要、易混淆）**：本讲的姊妹组件 `psi_fix_cordic_vect`（矢量模式）有一个 generic `pl_stg_per_iter_g : integer range 1 to 2`，允许在每个迭代级里插入 1 或 2 级寄存器以改善时序（Fmax）。但 **`psi_fix_cordic_rot` 没有这个 generic**——它的流水实现被硬展开为「每级迭代恰好一级寄存器」（见 4.2.3 的 `for i in 0 to iterations_g-1 loop`）。因此本组件的吞吐-资源旋钮只有 `mode_g`（全流水 vs 全串行）这「两档」，没有 vect 那样的中间粒度。需要细粒度时序权衡时，应选用 `psi_fix_cordic_vect`（见 u5-l3）或在外层加寄存器。

#### 4.2.2 核心流程

**PIPELINED** 的数据流：

```text
vld_i ──► [init] ──► [iter 0] ──► [iter 1] ──► … ──► [iter N-1] ──► [QC] ──► [out] ──► vld_o
            │          │            │                     │              │        │
           X(0),Y(0)  X(1),Y(1)   X(2),Y(2)   …        X(N),Y(N)       xQc,yQc  dat_inp/qua_o
rdy_i 恒为 '1'（无反压），每拍可吞一个样本
```

**SERIAL** 的数据流（单个样本的生命周期）：

```text
样本到达 → 锁存进 Xin/Yin/Zin（1 拍）
        → IterCnt=0：第 0 次迭代（Xin→X）            ┐
        → IterCnt=1：第 1 次迭代（X→X）              │ 共 N 拍
        → …                                          │ （CORDIC 引擎分时复用）
        → IterCnt=N-1：最后一次迭代，置 CordVld       ┘
        → 象限校正（1 拍）→ 输出（1 拍）→ vld_o
rdy_i = not XinVld：仅当输入暂存寄存器空时才接收新样本
```

#### 4.2.3 源码精读

**(a) PIPELINED 分支**——`rdy_i` 恒为 `'1'`，迭代在 `for` 循环里空间展开：

[hdl/psi_fix_cordic_rot.vhd:166-175](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L166-L175) 流水分支声明 `X/Y/Z/Vld/Quad` 数组（深度 `0 to iterations_g`）并把 `rdy_i <= '1'`。

[hdl/psi_fix_cordic_rot.vhd:187-191](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L187-L191) 初始化第 0 级：把幅度/角度 resize 进内部格式、`Y(0)=0`、`Quad(0)` 取角度高位。

[hdl/psi_fix_cordic_rot.vhd:196-200](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L196-L200) 关键的 `for i in 0 to iterations_g-1 loop`：**每级迭代恰好一组寄存器**（这就是 `cordic_rot` 没有 `pl_stg_per_iter` 的直接证据）。`Vld` 与 `Quad` 用切片赋值随数据逐级平移（与 u3-l3 的 valid 流水同构）。

注意：`vld_o`、`dat_inp_o`、`dat_qua_o` 在 [213-220](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L213-L220) 行再做一级输出寄存器。

**(b) SERIAL 分支**——只有一组 `X/Y/Z`，靠 `IterCnt` 复用：

[hdl/psi_fix_cordic_rot.vhd:229-242](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L229-L242) 串行分支声明单个 `X/Y/Z` + 输入暂存 `Xin/Yin/Zin`，`rdy_i <= not XinVld`。

[hdl/psi_fix_cordic_rot.vhd:254-260](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L254-L260) 输入锁存：`XinVld='0' and vld_i='1'` 时把样本存进 `Xin/Yin/Zin`。

[hdl/psi_fix_cordic_rot.vhd:262-286](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L262-L286) CORDIC 循环：`IterCnt=0` 时从 `Xin` 起算第 0 级；之后每拍算一级，到 `IterCnt=iterations_g-1` 时清零计数器并置 `CordVld`。注意「下一个样本可在迭代期间预存进 `Xin/Yin/Zin`」，但 CORDIC 引擎本身是瓶颈——每个样本要占它 \(N\) 拍。

**(c) 两模式共用的输出段**——象限校正与（可选）增益补偿在两个 `generate` 里各写一份，逻辑完全相同（这是少数没有抽出公共代码的地方，读者可留意）：

[hdl/psi_fix_cordic_rot.vhd:298-306](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L298-L306) 串行实现的输出段（与 [213-220](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L213-L220) 流水版同构）。

#### 4.2.4 代码实践：估算两种模式的周期数

1. **实践目标**：根据 `iterations_g` 估算 SERIAL 与 PIPELINED 处理一个样本的周期数，并解释 `pl_stg_per_iter` 为何**不适用**于本组件。
2. **操作步骤**：
   - 打开 [hdl/psi_fix_cordic_rot.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd)，定位 `g_pipelined`（第 [166](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L166) 行）与 `g_serial`（第 [229](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L229) 行）两个 `generate`。
   - 取测试台使用的 `iterations_g = 21`（[testbench/.../psi_fix_cordic_rot_tb.vhd:49](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cordic_rot_tb/psi_fix_cordic_rot_tb.vhd#L49)）。
   - 数一数流水路径上的寄存器级数：`init(1) + 迭代(21) + QC(1) + out(1) = 24` 拍延迟；吞吐 = 1 样本/周期。
   - 数串行路径：输入锁存(1) + 引擎迭代(21) + QC(1) + out(1) ≈ 24 拍延迟；吞吐 ≈ 1 样本/21 周期（引擎是瓶颈）。
3. **需要观察的现象**：两模式**延迟相近**，但**吞吐差约 21 倍**；资源上流水版需要 21 套加减/移位，串行版只需 1 套。
4. **预期结果**：得到一张「延迟 / 吞吐 / 资源」三列对比表，说明 `mode_g` 是本组件唯一的面积-吞吐旋钮。
5. 关于 `pl_stg_per_iter`：在 `psi_fix_cordic_rot.vhd` 的 generic 列表（第 [27-38](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L27-L38) 行）中**找不到**它；对照 [hdl/psi_fix_cordic_vect.vhd:38](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_vect.vhd#L38) 可见该旋钮只属于矢量模式。精确周期数「待本地验证」（取决于综合与时序约束）。

#### 4.2.5 小练习与答案

**练习 1**：为什么说 PIPELINED 与 SERIAL 的「延迟」反而差不多，区别在「吞吐」？

**答案**：延迟由数据从输入走到输出经过的寄存器级数决定。流水版有 \(N\) 级迭代寄存器，串行版虽然只有一组寄存器，但数据要在其上「停留」\(N\) 拍才走完——两者墙钟延迟都在 \(N+\)常数拍量级。区别在于：流水版每拍能进新样本（吞吐 1/周期），串行版要等当前样本算完才能进下一个（吞吐 ≈ 1/\(N\) 周期）。

**练习 2**：`rdy_i <= not XinVld`（第 [242](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L242) 行）意味着什么？为什么串行版需要这个握手，而流水版把 `rdy_i` 直接接到 `'1'`？

**答案**：`not XinVld` 表示「输入暂存寄存器空闲时才接收」。串行版处理一个样本要 \(N\) 拍，若上游连续送数会溢出，故必须用 `rdy_i` 反压。流水版每拍都能吞一个样本、各级都有独立寄存器缓冲，所以恒为 `'1'`、无需反压。

**练习 3**（命名观察）：`cordic_rot` 的 ready 端口叫 `rdy_i`，但它的方向是 `out`（第 [45](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L45) 行）。这与命名约定（`_i` 表示输入）矛盾。姊妹组件 `cordic_vect` 的对应端口叫什么？

**答案**：`cordic_vect` 的对应端口已改名为 `rdy_o`（[hdl/psi_fix_cordic_vect.vhd:46](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_vect.vhd#L46)），这正是 Changelog 记录的「renamed rdy_i to rdy_o」修正（详见 u5-l3）。`cordic_rot` 目前仍保留旧的 `rdy_i` 命名（功能无误，只是名字有误导性），这是读者读源码时应留意的一处历史遗留。

---

### 4.3 增益补偿

#### 4.3.1 概念说明

每次 CORDIC 微旋转不仅转角度，还会把向量的长度放大一点点——第 \(i\) 级的长度放大因子是 \(\sqrt{1+2^{-2i}}\)（因为移位加减等价于一个略带放大的旋转）。经过 \(N\) 级，总放大倍数为：

\[
G(N) = \prod_{i=0}^{N-1} \sqrt{1+2^{-2i}} \;\approx\; 1.6468 \quad (N\to\infty)
\]

这个 **CORDIC 增益 \(G(N)\)** 意味着：若不补偿，输出向量的幅度会比真实 \(r\) 大约偏 \(G\) 倍。`gain_comp_g` 开关决定是否在输出前乘以 \(1/G(N)\) 把幅度校正回真实值。

- `gain_comp_g = False`（默认）：不补偿。输出含 \(G\) 倍增益，适合下游已经知道并吸收这个增益的应用（省一次乘法）。
- `gain_comp_g = True`：在 X、Y 输出上各乘一次 `GcCoef = 1/G(N)`，得到真实的 \((r\cos\theta, r\sin\theta)\)。需要 2 个乘法器。

#### 4.3.2 核心流程

```text
计算 CordicGain(N) = ∏ sqrt(1 + 2^-2i),  i=0..N-1
   │
GcCoef = psi_fix_from_real(1/CordicGain(N), fmt=(0,0,17))   # 无符号 17 位小数
   │
若 gain_comp_g:
    out_I = psi_fix_mult(xQc, internalFmt, GcCoef, (0,0,17), outFmt, round, sat)
    out_Q = psi_fix_mult(yQc, internalFmt, GcCoef, (0,0,17), outFmt, round, sat)
否则:
    out_I = psi_fix_resize(xQc, internalFmt, outFmt, round, sat)
    out_Q = psi_fix_resize(yQc, internalFmt, outFmt, round, sat)
```

`GcCoef` 的格式选 `(0,0,17)`（无符号、17 位小数）：因为 \(1/G(N)\approx 0.607\in[0,1)\)，无符号即可，17 位小数给足精度。内部格式 `internal_fmt_g` 的整数位必须比幅度输入多（断言第 [161](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L161) 行），正是为了容纳这个 \(G\approx 1.65\) 倍的幅度膨胀不溢出。

#### 4.3.3 源码精读

**(a) 增益函数与系数常量**：

[hdl/psi_fix_cordic_rot.vhd:77-84](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L77-L84) `CordicGain(iterations)` 函数：在综合期循环计算 \(\prod\sqrt{1+2^{-2i}}\)。

[hdl/psi_fix_cordic_rot.vhd:86-89](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L86-L89) `GcFmt_c=(0,0,17)` 与 `GcCoef_c = psi_fix_from_real(1/CordicGain(N), GcFmt_c)`：把补偿系数量化为定点常量。

**(b) 输出段的条件乘法**（流水版；串行版第 [300-306](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L300-L306) 行同理）：

[hdl/psi_fix_cordic_rot.vhd:214-220](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L214-L220) `if gain_comp_g then psi_fix_mult(...) else psi_fix_resize(...)`：补偿走乘法、不补偿走纯 resize。

**(c) Python 镜像**：

[model/psi_fix_cordic_rot.py:80-89](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cordic_rot.py#L80-L89) `CordicGain` 属性：与 VHDL 同式。

[model/psi_fix_cordic_rot.py:71-74](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cordic_rot.py#L71-L74) 构造时预计算 `gainCompCoef` 与 `angleTable`，格式与 VHDL 常量一致（`GAIN_COMP_FMT=(0,0,17)`，见第 [22](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cordic_rot.py#L22) 行）。

[model/psi_fix_cordic_rot.py:120-126](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_cordic_rot.py#L120-L126) `Process` 末段的增益校正分支，与 VHDL 第 [214-220](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L214-L220) 行一一对应。

**(d) 测试台如何同时覆盖两种配置**——`gain_comp_g` 同时控制 DUT 与「选用哪份期望输出文件」：

[testbench/.../psi_fix_cordic_rot_tb.vhd:73](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cordic_rot_tb/psi_fix_cordic_rot_tb.vhd#L73) `RespFileName_c` 按 `gain_comp_g` 选择 `outputWithGc.txt` 或 `outputWithNoGc.txt`。

[testbench/.../Scripts/preScript.py:49-53](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cordic_rot_tb/Scripts/preScript.py#L49-L53) preScript 实例化两个模型（`gainComp=True` 用 round/sat，`gainComp=False` 用 trunc/wrap），各跑一遍 `Process()`。

[testbench/.../Scripts/preScript.py:80-87](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cordic_rot_tb/Scripts/preScript.py#L80-L87) 把两份结果都写成位模式整数文本，供测试台按需比对（preScript 只跑一次、多份输出复用，正是 u3-l2 的套路）。

**(e) 回归参数矩阵**——`config.tcl` 把 `{PIPELINED, SERIAL} × {gain_comp on, off}` 四种组合各跑一轮：

[sim/config.tcl:326-333](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L326-L333) cordic_rot_tb 的 4 组 `tb_run_add_arguments`：流水+Gc、流水+无Gc、串行+Gc、串行+无Gc，且 Gc 用 round/sat、无 Gc 用 trunc/wrap（与 preScript 的两种模型严格对应）。

#### 4.3.4 代码实践：观察增益补偿对幅度的影响

1. **实践目标**：量化「开/关 `gain_comp_g`」对输出幅度的影响，验证补偿后输出 ≈ 真实 \(r\)、不补偿时偏大约 \(G(N)\) 倍。
2. **操作步骤**：
   - 阅读 [testbench/.../Scripts/preScript.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cordic_rot_tb/Scripts/preScript.py)，注意它用 `iterations=21`（第 [35](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cordic_rot_tb/Scripts/preScript.py#L35) 行）。
   - 用公式手算 \(G(21)=\prod_{i=0}^{20}\sqrt{1+2^{-2i}}\)（或用 Python 侧 `CordicGain` 属性），应得到约 1.6468。
   - 在 [sim/config.tcl:329-332](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L329-L332) 的四组参数中，对比「`gain_comp_g=true`」与「`gain_comp_g=false`」两组的期望输出文件 `outputWithGc.txt` 与 `outputWithNoGc.txt`（由 preScript 生成）。
3. **需要观察的现象**：对同一输入样本，`outputWithNoGc` 的 I/Q 数值约为 `outputWithGc` 的 \(G(21)\approx 1.65\) 倍（在定点量化误差范围内）。
4. **预期结果**：理解 `gain_comp_g` 本质是「是否在硬件里多做一次乘法把 \(G\) 消掉」；同时确认 preScript 用 round/sat 配 Gc、用 trunc/wrap 配无 Gc，是为了让两份期望输出与 `config.tcl` 的 generic 严格对齐。
5. 若手算 \(G(21)\) 不便，可标注「待本地用 Python 模型的 `CordicGain` 属性验证」。

#### 4.3.5 小练习与答案

**练习 1**：`GcCoef` 为何用无符号格式 `(0,0,17)` 而不是有符号格式？

**答案**：\(1/G(N)\approx 0.607\) 恒为正且小于 1，无符号格式 `(0,0,17)` 的范围 \([0,1)\) 足以表示，省掉一位符号位、把精度全留给小数位（17 位）。这与 u4-l1 里 `GcCoefFmt=(0,1,16)` 的思路一致——按系数的真实范围精打细算地选格式。

**练习 2**：`config.tcl` 里「有 Gc」配 `round/sat`、「无 Gc」配 `trunc/wrap`，为什么不是随便搭配？

**答案**：因为 preScript 用两个独立的 Python 模型实例分别生成两份期望输出：`gainComp=True` 那个实例构造时传入 `round/sat`，`gainComp=False` 那个传入 `trunc/wrap`（[preScript.py:49-53](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_cordic_rot_tb/Scripts/preScript.py#L49-L53)）。`config.tcl` 的 generic 必须与生成期望输出时的参数严格一致，测试台的逐位比对才有意义。这也再次体现了「位真双模型」对参数一致性的苛刻要求。

---

## 5. 综合实践

**任务**：为 `psi_fix_cordic_rot` 做一次「参数-架构」选型分析，把本讲三个最小模块串起来。

假设你要在 100 MHz 时钟域上生成一个复数本振，要求：
- 角度分辨率覆盖整圈，相位精度不低于 16 位；
- 输入样本率 100 MSPS（每拍一个样本）；
- 资源紧张，DSP 乘法器预算只够 2 个。

请完成：

1. **算法层**（对应 4.1）：写出 `dat_ang_i` 从整圈 \([0,1)\) 折叠进 \([-0.25,+0.25)\) 的过程，并说明为何 `angle_int_fmt_g` 取 `(1,-2,23)` 能让残差落在收敛锥内。
2. **架构层**（对应 4.2）：在 100 MSPS、每拍一个样本的要求下，`mode_g` 应选 `PIPELINED` 还是 `SERIAL`？为什么？若改选 `SERIAL`，最大可支持的样本率是多少（按 `iterations_g=21` 估算）？
3. **增益层**（对应 4.3）：在 DSP 预算只有 2 个乘法器的约束下，`gain_comp_g` 设 `True` 还是 `False`？说明你的取舍（提示：补偿需要 2 个乘法器）。
4. **验证层**：参照 [config.tcl:329-332](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L329-L332)，为你的选型写出一行 `tb_run_add_arguments`，并指出应比对 preScript 生成的哪份 `output*.txt`。

**参考结论**：
1. 见 4.1.2/4.1.5，`(1,-2,23)` 表示 ±0.25 圈，残差 ∈ ±0.25 < 收敛锥 ±0.277。
2. 每拍一个样本 → 必须选 `PIPELINED`；`SERIAL` 在 `iterations_g=21` 下吞吐约 100M/21 ≈ 4.76 MSPS，远达不到 100 MSPS。
3. DSP 预算仅 2 个 → 设 `gain_comp_g=False`，把 2 个乘法器留给别处，由下游吸收 CORDIC 增益（或改用 `psi_fix_pol2cart_approx` 等无乘法器近似，见 u8-l3）。
4. 例如 `-gmode_g=PIPELINED -ggain_comp_g=false -ground_g=psi_fix_trunc -gsat_g=psi_fix_wrap`，比对 `outputWithNoGc.txt`。

---

## 6. 本讲小结

- `psi_fix_cordic_rot` 用 **CORDIC 旋转模式**把极坐标 \((r,\theta)\) 变换为直角坐标 \((I,Q)\)，核心是「移位 + 加减」的迭代微旋转，不依赖乘法器算三角函数。
- 角度表 `AngleTableReal_c` 是 \(\arctan(2^{-i})/(2\pi)\)（单位：圈）；整圈角度通过**半圈折叠**装进收敛锥，用 `quad`（角度最高 2 位）判断是否对 \((x,y)\) 同时取负。
- `mode_g` 提供 `PIPELINED`（每拍一个样本、资源 ∝ 迭代数）与 `SERIAL`（资源近似常数、吞吐 ≈ 1/\(N\) 周期）两档；**延迟相近，区别在吞吐与资源**。
- **`pl_stg_per_iter` 不属于本组件**——它是姊妹组件 `psi_fix_cordic_vect` 的 generic；`cordic_rot` 流水实现硬展开为每级迭代一级寄存器。
- CORDIC 会引入 \(G(N)=\prod\sqrt{1+2^{-2i}}\approx 1.65\) 的幅度增益，`gain_comp_g` 控制是否乘 `1/G` 补偿（耗 2 个乘法器）；`GcCoef` 用 `(0,0,17)` 无符号格式。
- VHDL 与 Python 模型用同一套算式与同一组格式断言，`config.tcl` 用 4 组参数（2 模式 × 2 增益配置）覆盖，preScript 一次性生成两份期望输出文件供各轮复用。
- 读者应留意一处历史遗留：`cordic_rot` 的 ready 输出仍叫 `rdy_i`（方向实为 `out`），而 `cordic_vect` 已修正为 `rdy_o`。

---

## 7. 下一步学习建议

- **下一讲 u5-l3（CORDIC 矢量模式）**：阅读 `psi_fix_cordic_vect`，它与本组件是镜像对称的一对（直角→极坐标、求模与相位），并拥有本讲提到的 `pl_stg_per_iter_g` 旋钮与已修正的 `rdy_o` 命名。对照阅读能加深对「CORDIC 双向应用」与「流水粒度」的理解。
- **u9-l1（DDS 与调制解调）**：看 `psi_fix_dds_18b` 如何把 CORDIC/NCO 用在数控振荡器与非整数比调制解调里，是本组件的典型应用场景。
- **u8-l3（sqrt/inv/pol2cart_approx）**：对比 `psi_fix_pol2cart_approx`——另一种「极坐标→直角坐标」的无乘法器近似实现，体会 CORDIC 与查表近似的资源/精度取舍。
- **源码延伸**：想加深定点手感，可回看 u2-l2 的 `psi_fix_shift_right`（动态移位、`maxShift`）与 u4-l1 的 `GcCoefFmt` 格式选取思路，它们在本讲的微旋转与增益补偿里都被用到。
