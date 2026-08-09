# sgen_cordic — CORDIC 算法与 RTL 实现

## 1. 本讲目标

本讲是「信号生成器」单元（单元 6）的第一篇。前面几单元讲的都是滤波器（FIR/CIC/多相），它们处理的是「已经存在的信号」；从本讲开始，我们转向「凭空产生信号」的硬件——如何在没有浮点单元、没有查表的前提下，用纯移位与加法实时算出正弦、余弦、幅值和角度。

学完本讲，你应当能够：

1. 说清 CORDIC（COordinate Rotation DIgital Computer）为什么能「只用移位和加法」完成旋转与三角计算。
2. 区分**旋转模式**（给角度，算正余弦）与**矢量模式**（给坐标，算幅值与角度）两种工作方式，并能指出各自的判决方向 \(d[i]\) 由谁决定。
3. 看懂 `sgen_cordic.v` 中用 `generate` 切换的两种硬件架构：**展开式**（unrolled，纯组合）与**迭代式**（iterative，资源复用寄存器组），并说出它们在资源、延迟与吞吐上的取舍。

本讲只聚焦算法与架构。增益补偿与 `atan` 查找表的定点量化留到 [u6-l2](u6-l2-cordic-gain-and-lut.md)，比特真验证方法学留到 [u7-l1](u7-l1-bittrue-verification.md)。

## 2. 前置知识

- **补码定点数与符号扩展**：理解最高位（MSB）是符号位、`>>>` 是保持符号的算术右移、`<<<` 是左移。这是 CORDIC「移位即乘以 2 的幂」的地基（见 [u2-l1](u2-l1-fixed-point-bitwidth.md)）。
- **`$clog2` 与位宽自动增长**：乘加会让位宽变宽，本讲会再次用到 `gp_xy_width + $clog2(gp_nr_iter)` 这类派生位宽（见 [u2-l1](u2-l1-fixed-point-bitwidth.md)）。
- **`generate` 编译期结构选择**：用一个参数在编译期二选一地生成两套不同硬件，未被选中的那套不进网表。FIR 模块里的 `gp_tf_df` 已经演示过这个手法（见 [u3-l2](u3-l2-fir-tf-df-topology.md)）。
- **统一时序约定**：异步低有效复位 `i_rst_an`、同步高有效使能 `i_ena`、上升沿触发（见 [u1-l4](u1-l4-coding-style-and-interface.md)）。

一个小提醒：本讲会反复出现一个二值变量 \(d[i]\in\{0,1\}\)。它表示「第 \(i\) 次微旋转的方向」。请先记住一句话：**`d=1` 表示做一次「+旋转」（从 \(x\) 里减去移位后的 \(y\)），`d=0` 表示做一次「−旋转」（往 \(x\) 里加上移位后的 \(y\)）**。后面所有公式都围绕它展开。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [.drl_src_code/sgen_cordic/rtl/sgen_cordic.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v) | **本讲主角**。包含 `sgen_cordic` 主模块（算法 + 两种架构）与 `cordic_gain` 增益补偿子模块。 |
| [.drl_src_code/sgen_cordic/octave/cordic.m](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/cordic.m) | Octave 黄金参考模型（GRM），即「标准答案」。用它对照 RTL 行为。 |
| [.drl_src_code/sgen_cordic/octave/gen_atan_lut.m](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/gen_atan_lut.m) | 生成 `atan_lut.v` 查找表的脚本（`atan_lut.v` 是构建产物，不入库）。 |
| [.drl_src_code/sgen_cordic/octave/stimuli.m](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/stimuli.m) | 9 个测试用例的激励定义（角度、坐标、模式切换）。 |
| [.drl_src_code/sgen_cordic/sim/testbench/sgen_cordic_tb.sv](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/sim/testbench/sgen_cordic_tb.sv) | SystemVerilog 测试台，例化 DUT 并做增益补偿后处理。 |

> 提示：`sgen_cordic` 在 README 模块表中标记为 **Stable**（[README.md:27](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/README.md#L27)、[README.md:45](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/README.md#L45)），全称 COordinate Rotation DIgital Computer。

---

## 4. 核心概念与源码讲解

### 4.1 CORDIC 移位相加原理

#### 4.1.1 概念说明

CORDIC 解决的核心问题是：**在硬件里做旋转**。给定平面上的一个向量 \((x,y)\)，把它绕原点旋转一个角度 \(\theta\)。常规做法要用乘法器算 \(\cos\theta\)、\(\sin\theta\)，但 CORDIC 的天才之处在于——它把任意角度 \(\theta\) 拆成一组**预先选好的、越来越小的**基本角度之和，而每个基本角的正切恰好是 2 的整数次幂：

\[
\theta \;\approx\; \sum_{i=0}^{N-1} d_i\,\alpha_i,\qquad \alpha_i=\arctan(2^{-i}),\qquad d_i\in\{+1,-1\}
\]

因为 \(\tan\alpha_i = 2^{-i}\)，所以「乘以 \(\tan\alpha_i\)」退化成「右移 \(i\) 位」——这就是「移位相加」名字的由来：**整个旋转过程不需要任何乘法器**。

推导一下单次微旋转。把向量旋转 \(\alpha_i\)，标准旋转矩阵是：

\[
\begin{aligned}
x' &= x\cos\alpha_i - y\sin\alpha_i = \cos\alpha_i\,(x - y\tan\alpha_i) \\
y' &= x\sin\alpha_i + y\cos\alpha_i = \cos\alpha_i\,(y + x\tan\alpha_i)
\end{aligned}
\]

代入 \(\tan\alpha_i=2^{-i}\)：

\[
x' = \cos\alpha_i\,(x - y\cdot 2^{-i}),\qquad y' = \cos\alpha_i\,(y + x\cdot 2^{-i})
\]

这里有个关键观察：\(\cos\alpha_i\) 这个因子**和旋转方向 \(d_i\)、和目标角度都无关**，只取决于迭代序号 \(i\)。所以可以把每一级的 \(\cos\alpha_i\) 先统统扔掉，等 \(N\) 级全做完之后，再用一个固定的总增益

\[
K = \prod_{i=0}^{N-1}\cos\alpha_i = \prod_{i=0}^{N-1}\frac{1}{\sqrt{1+2^{-2i}}} \approx 0.607252959138945
\]

一次性补回来（由本文件末尾的 `cordic_gain` 模块完成，细节见 [u6-l2](u6-l2-cordic-gain-and-lut.md)）。扔掉 \(\cos\alpha_i\) 之后，每一级就只剩下「移位 + 加减」：

\[
\boxed{\;x_{i+1}=x_i - d_i\cdot(y_i \gg i),\quad y_{i+1}=y_i + d_i\cdot(x_i \gg i),\quad z_{i+1}=z_i - d_i\cdot\alpha_i\;}
\]

其中 \(z\) 是「剩余待旋转角度」，每做一级就减掉一个 \(\alpha_i\)。

#### 4.1.2 核心流程

CORDIC 的数据流可以概括为「三级一组的微旋转，重复 \(N\) 次」：

```text
初始化：x0,y0 输入坐标；z0 输入角度（或初值）
        输入左移 c_ext_bits 位（增加保护位，防止右移丢精度）

for i = 0 .. N-1:
    1. 算方向 d[i]      ← 由当前 z 或 y 的符号决定（见 4.2）
    2. x[i+1] = x[i] ∓ (y[i] >>> i)     ← 移位 + 加/减
    3. y[i+1] = y[i] ± (x[i] >>> i)     ← 移位 + 加/减
    4. z[i+1] = z[i] ∓ atan_lut[i]      ← 查表 + 加/减

输出：x[N-1], y[N-1], z[N-1]（再经 cordic_gain 补偿增益 K）
```

注意第 0 级（\(i=0\))：\(\gg 0\) 就是「不移位」，此时 \(\tan\alpha_0=1\)，\(\alpha_0=\arctan(1)=45°\)——这是最大的一级旋转。之后每一级角度减半（\(45°, 26.6°, 14°, 7.1°, \dots\)），逐渐逼近目标。

一个常被忽略的细节：CORDIC 的收敛范围是所有 \(\alpha_i\) 之和 \(\sum\alpha_i\approx 99.88°\)（约 \(\pm1.7433\) 弧度）。超出这个范围的角度需要先做象限预旋转。`stimuli.m` 里的测试用例 8、9 故意用了 \(180°\)、\(200°\)（[stimuli.m:74-83](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/stimuli.m#L74-L83)），正是在探测这个边界。

#### 4.1.3 源码精读

先看模块的参数与端口，建立全局印象：

[sgen_cordic.v:6-25](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L6-L25) — 模块声明。七个参数中最关键的三个是：

- `gp_mode_rot_vec`：选工作模式（1=旋转，0=矢量，详见 4.2）。
- `gp_impl_unrolled_iterative`：选硬件架构（1=展开式，0=迭代式，详见 4.3）。
- `gp_nr_iter`：微旋转级数 \(N\)，决定精度与开销。

输入输出都是 `wire signed`（有符号），端口 `i_x/i_y/i_z/o_x/o_y/o_z` 配合统一时序端口 `i_rst_an/i_ena/i_clk`，完全遵循 [u1-l4](u1-l4-coding-style-and-interface.md) 的接口约定。

接下来看本模块如何把「移位相加」落地为位宽推导：

[sgen_cordic.v:28-35](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L28-L35) — 派生常量与查找表声明。这里体现了 [u2-l1](u2-l1-fixed-point-bitwidth.md) 的位宽增长思想：

```verilog
localparam c_ext_bits          = $clog2(gp_nr_iter);          // 保护位数
localparam c_xy_internal_width = gp_xy_width + c_ext_bits;    // 内部加宽后的 x/y 通路
...
wire signed [gp_angle_width-1:0] atan_lut [0:gp_angle_depth-1];
`include "atan_lut.v"
```

两个要点：

1. **为什么要左移保护位**：每次微旋转都要右移，低比特会不断丢精度；级数越多累积误差越大。模块在输入处把 \(x,y\) 左移 `c_ext_bits` 位（即放大 \(2^{c_ext_bits}\) 倍），给低位留出「精度缓冲」。这正是 `c_xy_internal_width = gp_xy_width + $clog2(gp_nr_iter)` 的来源——和 GRM 里 `2^(xy_width+ceil(log2(n_iter)))` 用的是**同一个公式**（见 [cordic.m:88](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/cordic.m#L88)），这就是 RTL 与 GRM 比特真的锁扣之一。
2. **`atan_lut` 是构建产物**：它由 `gen_atan_lut.m`（或 `cordic.m`）在仿真前生成，经 `stimuli.m` 拷进 `rtl/` 目录（[stimuli.m:98](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/stimuli.m#L98)），仓库里并不存在 `atan_lut.v` 模板。注意要让 `gp_angle_depth ≥ gp_nr_iter`，否则下标越界。

最后看「移位 + 加减」这条核心算式在展开式里的写法：

[sgen_cordic.v:83-87](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L83-L87) — 展开式的核心微旋转算式：

```verilog
assign x[i] = (d[i]) ? x[i-1] - (y[i-1] >>> i) : x[i-1] + (y[i-1] >>> i);
assign y[i] = (d[i]) ? y[i-1] + (x[i-1] >>> i) : y[i-1] - (x[i-1] >>> i);
assign z[i] = (d[i]) ? z[i-1] - atan_lut[i]    : z[i-1] + atan_lut[i];
```

逐行对照公式：`>>> i` 就是「右移 \(i\) 位 = 乘以 \(2^{-i}\)」；`d[i]` 为真时走减法（对应公式里 \(d_i=+1\) 的 \(x_i - y_i2^{-i}\)），为假时走加法。`z` 这一行则用查表值 `atan_lut[i]`（即 \(\alpha_i\)）做加减。整段没有出现一个 `*`——这就是 CORDIC 的「无乘法器」魅力。

#### 4.1.4 代码实践

**实践目标**：亲手验证「移位即乘以 2 的幂」，并理解保护位的作用。

**操作步骤**：

1. 打开 [cordic.m:16-22](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/cordic.m#L16-L22)，确认 `tan_lut(i)=2^-j`、`atan_lut(i)=atan(2^-j)`、`gain_lut(i)=1/sqrt(1+2^(-2j))`。
2. 在草稿纸上手算前 4 级：\(\alpha_0=\arctan(1)=45°\)、\(\alpha_1=\arctan(0.5)\approx26.565°\)、\(\alpha_2=\arctan(0.25)\approx14.036°\)、\(\alpha_3=\arctan(0.125)\approx7.125°\)。
3. 累加这 4 级，得到 \(45+26.565+14.036+7.125\approx92.7°\)——这就是 4 级 CORDIC 能逼近的最大角度。
4. （可选）若本机装了 Octave，运行 `octave --eval "disp(atan(2.^-[0:3])*180/pi)"` 对照。

**需要观察的现象**：每一级角度大约减半，移位量与角度序号一一对应。

**预期结果**：4 级之和约 \(92.7°\)；级数越多，可逼近角度越接近上限 \(99.88°\)、单级角度越精细。

> 若无法运行 Octave，本步为「源码阅读 + 手算」型实践，标注「待本地验证」数值精度。

#### 4.1.5 小练习与答案

**练习 1**：为什么 CORDIC 每一级都可以扔掉 \(\cos\alpha_i\)、最后再补增益 \(K\)？

**参考答案**：因为 \(\cos\alpha_i\) 只依赖序号 \(i\)，与旋转方向 \(d_i\) 和目标角度都无关。各级的 \(\cos\) 因子可写成常数乘积 \(K=\prod\cos\alpha_i\)，提公因式到末尾一次性乘回即可，中间过程因此只保留移位与加减。

**练习 2**：`c_xy_internal_width = gp_xy_width + $clog2(gp_nr_iter)`。当 `gp_xy_width=8`、`gp_nr_iter=16` 时，内部 x/y 通路多宽？为什么需要比输入宽？

**参考答案**：`$clog2(16)=4`，故内部宽 12 位。多出的 4 位是「保护位」，因为每级右移会丢失低位精度，预先左移放大 \(2^4\) 倍可缓冲累积误差。

---

### 4.2 旋转模式与矢量模式

#### 4.2.1 概念说明

CORDIC 同一套「移位相加」硬件，靠**判决方向 \(d[i]\) 的判据不同**，就能完成两种截然不同的任务：

| 模式 | `gp_mode_rot_vec` | 目标 | 判据（决定 \(d_i\)） | 典型用途 |
| --- | --- | --- | --- | --- |
| **旋转模式 Rotation** | `1` | 把向量旋转到目标角度 | 让剩余角度 \(z\to 0\)：\(d_i=\mathrm{sign}(z)\) | 算 \(\cos\theta,\sin\theta\)（旋转 \((K^{-1},0)\)） |
| **矢量模式 Vectoring** | `0` | 把向量旋转到 x 轴 | 让纵坐标 \(y\to 0\)：\(d_i=-\mathrm{sign}(y)\) | 算幅值 \(\sqrt{x^2+y^2}\) 与角度 \(\arctan(y/x)\) |

- **旋转模式**：你给一个角度 \(\theta\)（放进 \(z_0\)），每级选择旋转方向把 \(z\) 往 0 拉。做完后 \(z\approx0\)，而 \((x,y)\) 被旋转了 \(\theta\)。若初值取 \((x_0,y_0)=(K^{-1},0)\)，输出就是 \((\cos\theta,\sin\theta)\)（再补增益）。
- **矢量模式**：你给一个向量 \((x_0,y_0)\)，每级选择旋转方向把 \(y\) 往 0 拉。做完后 \(y\approx0\)，此时 \(z\) 累积的角度就是 \(\arctan(y_0/x_0)\)，而 \(x\) 增长为幅值 \(K\sqrt{x_0^2+y_0}\)。

一句话记忆：**旋转模式盯住 \(z\)，矢量模式盯住 \(y\)**。

#### 4.2.2 核心流程

两种模式的差别只在「\(d[i]\) 怎么取」这一步，其余算式完全相同：

```text
若 旋转模式(gp_mode_rot_vec=1):
    d[i] = (z >= 0) ? +1 : -1      # 想让 z→0
若 矢量模式(gp_mode_rot_vec=0):
    d[i] = (y <  0) ? +1 : -1      # 想让 y→0

随后统一执行 4.1 的三条移位-加减算式。
```

注意 RTL 里 \(d\in\{0,1\}\)（单比特），而 GRM 里 \(d\in\{+1,-1\}\)。映射关系是：**RTL `d=1` ⟺ GRM `d=+1`**（走减法分支 `x - y>>>i`），**RTL `d=0` ⟺ GRM `d=-1`**（走加法分支 `x + y>>>i`）。在阅读两边代码时记住这个一一对应即可。

补码定点的好处在这里尽显：判一个数的正负，只要看它的 MSB（最高位）。MSB=0 表示非负，MSB=1 表示负。于是 `sign(z)` 退化成 `z[MSB]`，完全不需要比较器。

#### 4.2.3 源码精读

看展开式里 \(d[i]\) 的取法（第 0 级初始化与后续级）：

[sgen_cordic.v:58-72](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L58-L72) — 第 0 级（初始化）的方向判决：

```verilog
if (gp_mode_rot_vec)                       // 旋转模式
    assign d[i] = (z_init[c_z_internal_width-1] == 0) ? 1'b1 : 1'b0;  // z>=0 → d=1
else                                       // 矢量模式
    assign d[i] = (y_init[c_xy_internal_width-1] == 1) ? 1'b1 : 1'b0; // y<0  → d=1
```

[sgen_cordic.v:74-87](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L74-L87) — 后续级（\(i>0\)）的方向判决，判据从初值改成上一级结果 \(z[i-1]\) 或 \(y[i-1]\)：

```verilog
if (gp_mode_rot_vec)
    assign d[i] = (z[i-1][c_z_internal_width-1] == 0) ? 1'b1 : 1'b0;  // 盯 z
else
    assign d[i] = (y[i-1][c_xy_internal_width-1] == 1) ? 1'b1 : 1'b0; // 盯 y
```

逐句对照：

- 旋转模式判 `z[MSB]==0`（\(z\ge0\)）→ `d=1` → `z[i]=z[i-1]-atan_lut[i]`（[sgen_cordic.v:86](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L86)），把 \(z\) 减小、推向 0。这与 GRM [cordic.m:33-37](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/cordic.m#L33-L37) 完全一致。
- 矢量模式判 `y[MSB]==1`（\(y<0\)）→ `d=1` → 旋转使 \(y\) 增大、推向 0。对应 GRM [cordic.m:40-44](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/cordic.m#L40-L44) 的 `if (y(i-1)<0), d(i)=1`。

再对照 GRM 的最终输出来理解两种模式的物理含义：

[cordic.m:77-102](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/cordic.m#L77-L102) — 旋转模式输出 `cos_z=x(end)*gain`、`sin_z=y(end)*gain`（[L79-80](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/cordic.m#L79-L80)）；矢量模式输出幅值 `a_z=x(end)*gain`、角度 `r_z=z(end)`（[L92-93](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/cordic.m#L92-L93)）。这正是上表「典型用途」的来源。

#### 4.2.4 代码实践

**实践目标**：追踪一个具体输入在两种模式下的 \(d[i]\) 序列与收敛过程。

**操作步骤**：

1. 读 [stimuli.m:19-22](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/stimuli.m#L19-L22)：测试用例 1 是 `x=1,y=0,z=12°`、旋转模式（默认 `gp_mode_rot_vec=0` 在 [stimuli.m:30](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/stimuli.m#L30) 处会被改写，请以 `defines` 结构为准）。注意 `stimuli.m` 顶部 `defines.gp_mode_rot_vec=0`（[L5](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/stimuli.m#L5)），但各用例会在 switch 内覆盖。
2. 对照 [cordic.m:46-63](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/cordic.m#L46-L63) 的初始化分支：旋转模式第 1 级判 `z_init>=0`。\(z_0=12°>0\)，故 \(d_1=+1\)。
3. 在草稿纸上列出前 4 级的 \(d[i]\) 与剩余角度 \(z[i]\)，观察 \(z\) 单调趋向 0。

**需要观察的现象**：旋转模式下 \(z\) 逐级逼近 0；矢量模式下换成 \(y\) 逼近 0。

**预期结果**：\(z_0=12°\) 经若干级后 \(z\to0\)，最终 \(x\approx\cos12°\)、\(y\approx\sin12°\)（再乘增益 \(K\)）。精确数值「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：旋转模式为何判 `z[MSB]`，矢量模式为何判 `y[MSB]`？

**参考答案**：旋转模式的目标是让剩余角度 \(z\to0\)，故每级按 \(z\) 的符号选方向（正就反向旋、负就正向旋）；矢量模式的目标是让纵坐标 \(y\to0\)，故按 \(y\) 的符号选方向。补码下正负号就是 MSB，所以判 MSB 即可，无需比较器。

**练习 2**：RTL 的 `d=1` 对应走哪条算式分支？它和 GRM 的 `d=+1` 是什么关系？

**参考答案**：`d=1` 走 `x[i-1] - (y[i-1]>>>i)`（减法分支），对应公式 \(x_i - y_i2^{-i}\)，即 GRM 的 `d=+1`。`d=0` 走加法分支，对应 GRM 的 `d=-1`。

---

### 4.3 展开式 vs 迭代式架构

#### 4.3.1 概念说明

同一份 CORDIC 算法，可以铺成两种截然不同的硬件。模块用参数 `gp_impl_unrolled_iterative` 在编译期二选一（手法与 [u3-l2](u3-l2-fir-tf-df-topology.md) 的 `gp_tf_df` 如出一辙）：

- **展开式（unrolled，`=1`）**：把 \(N\) 级微旋转**全部摊开**，每一级都有自己的加减法器，级与级之间用线网直连，**纯组合逻辑**，没有任何寄存器参与迭代。一个时钟都不需要就能算出结果（延迟由组合路径深度决定）。代价是面积随 \(N\) 线性增长。
- **迭代式（iterative，`=0`）**：只保留**一组**加减法器和一组寄存器（`r_x/r_y/r_z`），靠一个迭代计数器 `r_count_iter` 在 \(N\) 个时钟里把它们**重复使用 \(N\) 次**，每拍算一级。面积几乎与 \(N\) 无关，代价是吞吐降为约 \(1/N\)。

这是经典的「面积换速度」谱系：展开式用硅片面积换零延迟、高吞吐；迭代式用时间换面积。和 [u3-l4](u3-l4-filt-mac.md) 里 `filt_mac`「单 MAC 分时复用」的思路同源。

#### 4.3.2 核心流程

**展开式**（一拍出结果，组合链）：

```text
输入 i_x,i_y,i_z
   ↓ 左移 c_ext_bits 得 x_init,y_init
[第0级] d[0]=sign(...) → x[0],y[0],z[0]   ← 各自带一组加减法器
[第1级] d[1]=sign(...) → x[1],y[1],z[1]   ← 各自带一组加减法器
   ...
[第N-1级]                 → x[N-1],y[N-1],z[N-1]
   ↓
o_x,o_y,o_z （组合直出）
```

**迭代式**（\(N\) 拍出一结果，寄存器复用）：

```text
复位：r_count_iter ← 全1（哨兵值，触发首次载入）
每拍 i_clk：
   若 r_count_iter 到达哨兵(≥N)：r_x,r_y,r_z ← 载入新输入（左移保护位）
   否则：r_x,r_y,r_z ← 用「移位 r_count_iter 位 + 查 atan_lut[r_count_iter]」算下一级
        r_count_iter ← r_count_iter + 1
   连续 N 拍完成一次完整旋转，期间 r_count_iter 同时充当：
     (a) 移位量选择器  —— x = r_x + (w_y >>> r_count_iter)
     (b) 查表地址      —— atan_lut[r_count_iter]
     (c) 载入/迭代门控 —— 计满 N 就载入新输入
```

迭代式的精髓在于**资源复用**：同一套加减法器、同一个查找表，靠 `r_count_iter` 在不同拍里「换挡」——这一拍移 3 位、查 `atan_lut[3]`，下一拍移 4 位、查 `atan_lut[4]`。展开式则是把这些「挡位」全部物理铺开，每一挡对应一套硬件。

#### 4.3.3 源码精读

**先看架构选择开关**：整个模块被两个互斥的 `generate if` 包住，编译期只活一个。

[sgen_cordic.v:38-41](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L38-L41) 与 [sgen_cordic.v:97-99](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L97-L99)：

```verilog
generate
  if (gp_impl_unrolled_iterative) begin: g_cordic_unrolled      // 展开式
  ...
generate
  if (!gp_impl_unrolled_iterative) begin: g_cordic_iterative    // 迭代式
```

配套的还有一个「只在迭代式才存在」的计数器位宽：

[sgen_cordic.v:31](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L31) — `c_iter_width = (!gp_impl_unrolled_iterative) ? c_ext_bits : 0;`。展开式时为 0（不需要计数器），迭代式时为 `$clog2(N)` 位。

**展开式**：用 `for` 循环（genvar）把 \(N\) 级摊开，每级一组 `assign`，级间用数组线网 `x[i-1]` 直连：

[sgen_cordic.v:51-93](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L51-L93) — 关键是初始化处的左移保护位 `i_x<<<c_ext_bits`（[L52-53](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L52-L53)），以及末级直出 `o_x=x[gp_nr_iter-1]`（[L91-93](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L91-L93)）。注意所有下标 `i`、`atan_lut[i]` 都是**编译期常量**，综合器会把每级的移位硬连成固定走线，把查找表项硬连成常数——没有任何运行时多路选择。

**迭代式**：核心是三个寄存器 + 一个计数器 + 一组组合「换挡」逻辑。

[sgen_cordic.v:108-126](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L108-L126) — 迭代计数器 `r_count_iter`：

```verilog
reg [c_iter_width-1:0] r_count_iter;
always @(posedge i_clk or negedge i_rst_an) begin
  if (!i_rst_an)                 r_count_iter <= {c_iter_width{1'b1}}; // 复位=全1哨兵
  else if (i_ena)
    if (r_count_iter < gp_nr_iter) r_count_iter <= r_count_iter + 1'b1; // 还没算完，+1
    else                           r_count_iter <= 'd0;                 // 算完，回 0
end
```

[sgen_cordic.v:128-151](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L128-L151) — 寄存器组 `r_x/r_y/r_z` 的「载入 vs 迭代」二选一：

```verilog
if (r_count_iter > (gp_nr_iter-1)) begin   // 计满/哨兵态：载入新输入
    r_x <= i_x<<<c_ext_bits;  r_y <= i_y<<<c_ext_bits;  r_z <= i_z;
end else begin                              // 否则：算下一级
    r_x <= x;  r_y <= y;  r_z <= z;
end
```

[sgen_cordic.v:154-164](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L154-L164) — 这才是迭代式「资源复用」的灵魂：同一套加减法器，靠 `r_count_iter` 选择移位量与查表地址：

```verilog
assign w_z = (gp_mode_rot_vec) ? (...) : (... atan_lut[r_count_iter] ...);  // 运行时查表地址
assign x = r_x + (w_y >>> r_count_iter);   // 运行时移位量
assign y = r_y + (w_x >>> r_count_iter);
assign z = r_z + w_z;
```

对比展开式的 `atan_lut[i]`（编译期常量）与 `>>> i`（编译期常量移位），这里变成了 `atan_lut[r_count_iter]`（**运行时多路选择**）和 `>>> r_count_iter`（**桶形移位器**）——这就是「省面积」的代价：多了一个桶形移位器和一个查找表 MUX。

> **深入观察（待本地验证）**：迭代式的载入门控 `r_count_iter > (gp_nr_iter-1)` 依赖计数器能取到值 `gp_nr_iter`。当 `gp_nr_iter` 恰为 2 的幂（如默认的 16）时，`c_iter_width=$clog2(16)=4`，计数器只能表示 0..15，**无法取到 16**，于是 `>15` 永假、载入分支永不触发。`stimuli.m` 里迭代式用例（tc 6/7）刻意把 `gp_nr_iter` 设为 24（非 2 的幂，[stimuli.m:53,65](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/stimuli.m#L53-L65)），恰好避开了这个边界。这是 CORDIC 模块值得在仿真台上仔细验证的一个细节。

> **另一个诚实提醒**：端口 `o_done`（[sgen_cordic.v:24](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L24)）在本修订中**声明了却从未被赋值**（两个 `generate` 块都只驱动 `o_x/o_y/o_z`）。测试台也留空未接（[sgen_cordic_tb.sv:147](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/sim/testbench/sgen_cordic_tb.sv#L147)）。它目前是为握手预留的接口，使用时需自行驱动。

#### 4.3.4 代码实践

**实践目标**：定量对比 `gp_nr_iter=16` 时两种架构的资源与延迟，并解释 `r_count_iter` 如何实现资源复用。

**操作步骤**：

1. 打开 [.drl_src_code/sgen_cordic/rtl/sgen_cordic.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v)，分别定位展开式（[L39-95](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L39-L95)）与迭代式（[L97-171](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L97-L171)）两段。
2. 数一数每级用到的「加减法单元」个数（x、y、z 三条算式各一个 → 每级 3 个）。
3. 填写下表（分析值；精确综合数据「待本地验证」）：

| 指标 | 展开式 (`=1`) | 迭代式 (`=0`) |
| --- | --- | --- |
| 加/减法单元数 | \(\approx 3\times N=48\)（每级 3 个，共 16 级） | \(\approx 3\)（仅一组，复用） |
| 移位器 | 每级固定移位（综合为走线，近乎免费） | 1 个桶形移位器（运行时变移位量） |
| 查找表访问 | 编译期常量（硬连成常数） | 1 个 MUX（运行时选 `atan_lut[r_count_iter]`） |
| 数据寄存器 | 0（纯组合） | 3 个（`r_x/r_y/r_z`）+ 1 个计数器 |
| 结果延迟 | 0 时钟（组合路径深 \(N\) 级） | \(N=16\) 时钟/结果 |
| 吞吐 | 1 结果/时钟（受限于组合路径） | \(\approx 1/16\) 结果/时钟 |

4. 用一段话解释 `r_count_iter` 的三重角色：① 移位量选择器（`>>> r_count_iter`）；② 查表地址（`atan_lut[r_count_iter]`）；③ 载入/迭代门控（计满 `N` 载入新输入）。

**需要观察的现象**：展开式面积随 \(N\) 线性膨胀但零延迟；迭代式面积几乎与 \(N\) 无关但吞吐降为 \(1/N\)。

**预期结果**：见上表。这是「面积—吞吐」谱系的两端，与 `filt_fir`（并行）/`filt_mac`（串行）的对照同构。

> 精确的资源/频率数据依赖综合工具与工艺库，标注「待本地验证」。若要进一步动手，可用 `iverilog -g2012` 对两种参数各做一次 elaborate（需先用 `gen_atan_lut.m` 生成 `atan_lut.v` 放进 `rtl/`），观察是否成功展开。

#### 4.3.5 小练习与答案

**练习 1**：展开式里 `atan_lut[i]` 的下标 `i` 是 genvar（编译期常量），迭代式里 `atan_lut[r_count_iter]` 的下标是寄存器（运行时变量）。这对综合结果有什么不同影响？

**参考答案**：编译期常量下标会被综合器直接硬连成该查找表项的常数，零成本；运行时变量下标则需要一个多路选择器（MUX），在 \(N\) 个表项里实时选一个，带来面积与时序开销。这正是迭代式「省下 \(N\) 套加减法器、却多出一个桶形移位器与一个 MUX」的来源。

**练习 2**：为什么说展开式与迭代式是「面积换速度」谱系的两端？各自适合什么场景？

**参考答案**：展开式用 \(O(N)\) 套硬件换 0 延迟、单拍吞吐，适合对吞吐/延迟敏感、面积预算充足的场合；迭代式用 \(O(1)\) 硬件加 \(N\) 拍时间，适合面积/功耗受限、可容忍低吞吐的场合。选择取决于系统对「快」与「小」的权衡。

---

## 5. 综合实践

**任务**：用本讲学到的全部知识，把 `sgen_cordic` 当作一个「正余弦发生器」走一遍完整的数据流，并把算法、模式、架构三者串起来。

**步骤**：

1. **选配置**：目标是用旋转模式算 \(\cos/\sin\)。从 [stimuli.m](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/stimuli.m) 选测试用例 1（`x=1,y=0,z=12°`），确认它对应旋转模式。
2. **讲算法**：用 4.1 的三条算式，在草稿纸上手算前 3 级的 \(x[i],y[i],z[i]\)，确认 \(z\) 趋向 0、\((x,y)\) 趋向 \((\cos12°,\sin12°)\) 的方向（增益 \(K\) 暂忽略）。
3. **讲模式**：说明为什么这个任务必须用旋转模式（盯 \(z\)），而算 \(\sqrt{x^2+y^2}\) 才用矢量模式（盯 \(y\)）。引用 [sgen_cordic.v:60-67](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L60-L67) 的判决逻辑。
4. **讲架构**：若把此例配置成迭代式（`gp_impl_unrolled_iterative=0`），画出 `r_count_iter` 从哨兵值出发、载入输入、再逐拍迭代到完成的时序草图，标出每一拍的移位量与查表地址。引用 [sgen_cordic.v:154-164](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L154-L164)。
5. **反思**：用一段话回答——如果系统要求每个时钟输出一对正余弦样本，你会选展开式还是迭代式？为什么？

**预期产出**：一张手算的前 3 级数据表 + 一张迭代式时序草图 + 一段架构选型理由。数值精度「待本地验证」，重点在推理过程的正确性。

## 6. 本讲小结

- **CORDIC = 移位相加的旋转**：把任意角度拆成 \(\alpha_i=\arctan(2^{-i})\) 之和，因 \(\tan\alpha_i=2^{-i}\)，旋转退化成「右移 \(i\) 位 + 加减」，全程无乘法器。
- **增益可剥离**：每级的 \(\cos\alpha_i\) 与方向无关，可提公因式为常数 \(K\approx0.6073\)，末尾由 `cordic_gain` 一次性补回（细节见 u6-l2）。
- **两种模式靠判据切换**：旋转模式（`gp_mode_rot_vec=1`）盯 \(z\)、用于算正余弦；矢量模式（`=0`）盯 \(y\)、用于算幅值与角度。补码下判正负就是看 MSB。
- **RTL 与 GRM 同公式**：`$clog2` 位宽、`atan(2^-i)` 查找表、`\pm sign` 判据在 RTL 与 Octave `cordic.m` 两边一一对应，是比特真的根基。
- **展开式 vs 迭代式**：`gp_impl_unrolled_iterative` 在编译期二选一——展开式纯组合、面积 \(O(N)\)、零延迟；迭代式靠 `r_count_iter` 复用一组寄存器、面积 \(O(1)\)、吞吐 \(1/N\)。
- **`r_count_iter` 三重角色**：移位量选择器 + 查表地址 + 载入/迭代门控；其门控条件在 `gp_nr_iter` 为 2 的幂时存在边界隐患（`stimuli.m` 用 24 规避）。

## 7. 下一步学习建议

- 继续本单元：读 [u6-l2 CORDIC 增益补偿与 atan LUT 生成](u6-l2-cordic-gain-and-lut.md)，搞清 \(K\approx0.6073\) 如何定点量化成 `c_1_gain`、`atan_lut.v` 如何由 `gen_atan_lut.m` 生成。
- 平行阅读：[u6-l3 NCO 相位累加与象限重构](u6-l3-nco-and-rom-reconstruction.md)，对比「查表型」与「计算型」两种信号生成思路。
- 验证方法学：本讲的比特真闭环细节（GRM 产激励/响应、TB 逐样本比对）在 [u7-l1](u7-l1-bittrue-verification.md) 系统讲解，建议随后阅读。
- 源码延伸：精读本文件末尾的 `cordic_gain` 模块（[sgen_cordic.v:180-210](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L180-L210)），它用了 `$rtoi` 把浮点增益转成定点常数，是 RTL 与数学之间的又一桥梁。
