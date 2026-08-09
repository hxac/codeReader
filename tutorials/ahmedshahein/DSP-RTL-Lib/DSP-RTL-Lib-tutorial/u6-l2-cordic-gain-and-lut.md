# CORDIC 增益补偿与 atan LUT 生成

## 1. 本讲目标

本讲承接 [u6-l1](u6-l1-cordic-algorithm.md)，把目光从「CORDIC 怎么转」收束到两个被有意推迟的细节上：

- CORDIC 每转一级都会把向量「撑长」一点，这个总放大量是多少？硬件里用什么电路把它**补回来**？
- 旋转所需的微旋转角度表 `atan_lut` 是怎么从数学公式变成 Verilog 里一行行 `assign atan_lut[k] = 16'sd...;` 的？为什么小数点要那样摆？

学完后你应当能够：

1. 说清 CORDIC 增益 \(K\approx 1.6468\) 与补偿因子 \(1/K\approx 0.6073\) 的来源，并看懂 `cordic_gain` 模块如何用「乘常数 + 算术右移」在定点下完成补偿。
2. 推导 `c_1_gain = $rtoi(0.607252959138945 * 2**(gp_gain_width-1))`，并手算 `gp_gain_width=12` 时的常数值。
3. 说出 `atan_lut` 的三步量化流水线（`atan` → `uquant` → `floor(·2^(W-2))`），解释「为什么是 `W-2`」。
4. 解释 `gen_atan_lut.m` 与黄金参考模型 `cordic.m` 内嵌的同一段生成代码为何是「比特真」的锁扣。

## 2. 前置知识

- **复数旋转与向量长度**：把向量 \((x,y)\) 绕原点转一个角，长度不变。但 CORDIC 为了省掉乘法，做的是「伪旋转」——只做移位加减、不补 \(\cos\) 因子，所以每转一级长度都会被放大一点点。
- **无穷乘积与极限**：每一级的放大倍数连乘起来，级数趋于无穷时会收敛到一个固定常数，这就是 CORDIC 增益 \(K\)。
- **定点小数（来自 [u2-l1](u2-l1-fixed-point-bitwidth.md)）**：一个 `W` 位有符号数，若把小数点放在「符号位之后」，则它表示的实数范围是 \([-1, +1)\)（即 `Q0.(W-1)`）；若小数点再左移一位，留出 1 位整数位，则范围是 \([-2, +2)\)（即 `Q1.(W-2)`）。本讲会反复用到这两种摆位。
- **`$rtoi` 与 `$clog2`（来自 [u2-l1](u2-l1-fixed-point-bitwidth.md)）**：`$rtoi(real)` 截断小数取整；`$clog2(n)=⌈log₂n⌉`。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [.drl_src_code/sgen_cordic/rtl/sgen_cordic.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v) | 同文件里有两个模块：`sgen_cordic`（算法主体，在 [u6-l1](u6-l1-cordic-algorithm.md) 已精读）与本讲主角 `cordic_gain`（增益补偿）。 |
| [.drl_src_code/sgen_cordic/octave/gen_atan_lut.m](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/gen_atan_lut.m) | 独立脚本：把 \(\arctan(2^{-i})\) 量化后写成 `atan_lut.v`。 |
| [.drl_src_code/sgen_cordic/octave/cordic.m](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/cordic.m) | 黄金参考模型（GRM）。它既给出标准答案，又**内嵌了与 `gen_atan_lut.m` 完全相同**的 `atan_lut.v` 生成代码。 |
| [.drl_src_code/sgen_cordic/octave/uquant.m](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/uquant.m) | 均匀量化器，`gen_atan_lut.m` 与 `cordic.m` 都依赖它（仓库自带，无需另装 LTFAT）。 |
| [.drl_src_code/sgen_cordic/octave/stimuli.m](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/stimuli.m) | GRM 入口：装配 `defines` 结构（含 `gp_gain_width`）、调 `cordic`、把生成的 `atan_lut.v` 搬进 `../rtl/`。 |
| [.drl_src_code/sgen_cordic/sim/testbench/sgen_cordic_tb.sv](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/sim/testbench/sgen_cordic_tb.sv) | 测试台：例化 `sgen_cordic` 后再串一级 `cordic_gain`，演示补偿模块如何接线。 |

## 4. 核心概念与源码讲解

### 4.1 CORDIC 增益补偿（cordic_gain）

#### 4.1.1 概念说明

[u6-l1](u6-l1-cordic-algorithm.md) 讲过，CORDIC 每一级做的是「移位 + 加减」，刻意**丢掉**了真正的旋转矩阵里那个 \(\cos\alpha_i\) 因子。丢掉它的代价是：向量长度每级都被放大。

第 \(i\) 级的放大倍数是

\[
\frac{1}{\cos\alpha_i}=\sqrt{1+\tan^2\alpha_i}=\sqrt{1+2^{-2i}}
\]

（因为 \(\tan\alpha_i = 2^{-i}\)）。把 \(N\) 级的放大倍数连乘：

\[
K_N=\prod_{i=0}^{N-1}\sqrt{1+2^{-2i}}
\]

当级数足够多（\(N\to\infty\)）时，这个乘积收敛到一个常数

\[
K \approx 1.6467602581
\]

也就是说，CORDIC 跑完之后，\((x,y)\) 的长度被放大了约 \(1.6468\) 倍。要拿到正确结果，就得再乘以补偿因子

\[
\frac{1}{K}\approx 0.6072529350
\]

> 这正是 [u6-l1](u6-l1-cordic-algorithm.md) 里那句「每级的 \(\cos\alpha_i\) 可提公因式为常数 \(K\)，末尾由 `cordic_gain` 一次性补回」的数学含义：补的不是 \(K\)，而是 \(1/K\approx 0.6073\)。

注意两件小事：

- 这个补偿与**旋转方向**无关（无论每级正转还是反转，\(\cos\) 都是正的），所以是一个常数乘法，硬件上只需一个乘法器。
- 在**旋转模式**下 \(x,y\) 分别是 \(\cos,\sin\)，两个都要补偿；在**矢量模式**下 \(y\) 被算法驱赶到 0，只有 \(x\)（幅值）需要补偿。

#### 4.1.2 核心流程

`cordic_gain` 用经典的「定点常数乘法 + 算术右移归一化」三步法：

```
1. 把 1/K ≈ 0.607252959138945 量化成一个 W=gp_gain_width 位有符号常数 c_1_gain：
      c_1_gain = floor( 0.607252959138945 * 2^(W-1) )        // $rtoi 截断
   含义：c_1_gain / 2^(W-1) ≈ 0.607252959138945

2. 做一次全宽度乘法：
      x_tmp = i_cordic_x * c_1_gain                           // 位宽 = gp_xy_width + W

3. 算术右移 (W-1) 位，把多乘进去的 2^(W-1) 缩回来：
      o_cordic_x = x_tmp >>> (W-1)
   净效果：o_cordic_x ≈ i_cordic_x * (1/K)
```

旋转模式（`gp_mode_rot_vec=1`）对 \(y\) 走同样的乘法；矢量模式（`=0`）直接令 \(y\) 输出 0。

#### 4.1.3 源码精读

模块定义与参数：[sgen_cordic.v:180-190](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L180-L190) 声明了 `cordic_gain`，其中 `gp_gain_width` 默认 12（[L182](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L182)），输出位宽 `gp_xy_owidth = gp_xy_width + $clog2(gp_gain_width)`（[L184](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L184)）留出若干保护位。

最关键的一行——把浮点增益烧成定点常数：

[sgen_cordic.v:192](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L192) —— `localparam c_1_gain = $rtoi( 0.607252959138945 * (2.0**(gp_gain_width-1)) );`，编译期把 \(1/K\) 量化为整数。

乘法与模式选择：

- [sgen_cordic.v:193-196](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L193-L196) —— `x_tmp` 恒为 `i_cordic_x * c_1_gain`（`x` 两种模式都要补偿）。
- [sgen_cordic.v:198-205](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L198-L205) —— `y_tmp` 在旋转模式下同样乘 `c_1_gain`，在矢量模式下置 `'d0`。

输出归一化：

[sgen_cordic.v:207-209](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L207-L209) —— `o_cordic_x = x_tmp >>> (gp_gain_width-1);`，算术右移把 \(2^{W-1}\) 缩回，得到净乘 \(1/K\)。`>>>` 对 `signed` 线网做符号扩展，故负样本也被正确缩放。

> **GRM 一致性**：黄金参考模型 [cordic.m:22](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/cordic.m#L22) 写的是 `gain = 0.607252959138945;`，并在 [cordic.m:77-83](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/cordic.m#L77-L83)（旋转）与 [cordic.m:90-95](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/cordic.m#L90-L95)（矢量）用它乘输出。**RTL 与 GRM 用的是同一个魔法常数** `0.607252959138945`，这是比特真的第一处锁扣。

#### 4.1.4 代码实践

**目标**：手算 `gp_gain_width=12` 时的 `c_1_gain`，再用 iverilog 让工具替你打印验证。

**操作步骤**：

1. 手算（倍乘法最不易错）：

\[
0.607252959138945 \times 2^{11} = 0.607252959138945 \times 2048 = 1243.654\ldots
\]

`$rtoi` 截断小数 → `c_1_gain = 1243`。

2. 实际实现的增益 = `1243 / 2^11 = 1243/2048 = 0.60693359375`，与目标 `0.607252959138945` 相差约 `0.00032`，即约 0.65 个 LSB——这就是 12 位量化的代价。

3. （可选，需本地 iverilog）写一个 2 行的顶层，例化 `cordic_gain` 并用 `$display` 打印 `c_1_gain`：

```verilog
// 示例代码：仅用于打印编译期常量，非项目原有文件
module print_gain;
  localparam gp_gain_width = 12;
  localparam c_1_gain = $rtoi( 0.607252959138945 * (2.0**(gp_gain_width-1)) );
  initial $display("c_1_gain = %0d", c_1_gain);
endmodule
```

`iverilog -o gain.print gain_print.v && vvp gain.print` 应输出 `c_1_gain = 1243`。

**需要观察的现象**：把 `gp_gain_width` 改成 24（即 `stimuli.m` 的默认值），重算 `c_1_gain`，体会位宽变宽后量化误差骤降。

**预期结果**：`gp_gain_width=12 → 1243`；`gp_gain_width=24` 时 `c_1_gain` 会大得多、实现增益更贴近 `0.607252959138945`（具体值待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么补偿因子是 \(1/K\approx 0.6073\) 而不是 \(K\approx 1.6468\)？

> **答**：CORDIC 的伪旋转把向量**撑长**了 \(K\) 倍，要还原就得缩回去，所以乘 \(1/K\)。

**练习 2**：`c_1_gain` 是用 `$rtoi`（截断）而非四舍五入生成的。若改成四舍五入，`gp_gain_width=12` 时它会变成多少？

> **答**：`1243.654` 四舍五入为 `1244`。本项目用截断，故为 `1243`。

**练习 3**：矢量模式下为什么 `y_tmp = 'd0`？

> **答**：矢量模式把 \(y\) 驱赶到 0（用于求幅值与角度），输出的 \(y\) 无意义，故直接置零；只有 \(x\)（幅值）需要乘 \(1/K\)。

---

### 4.2 atan_lut 的定点量化

#### 4.2.1 概念说明

CORDIC 每一级要从剩余角度 \(z\) 里扣掉一个基本角 \(\alpha_i = \arctan(2^{-i})\)。这些角度是固定的常数，于是预先算好存成一张表 `atan_lut`，运行时按级号 `i` 取用（[u6-l1](u6-l1-cordic-algorithm.md) 已展示 `z[i] = z[i-1] ± atan_lut[i]`）。

难点不在「算角度」，而在「**怎么把弧度值塞进定宽的整数寄存器**」。两张表必须对齐：

- `atan_lut[i]` 的摆位必须与残角累加器 \(z\) 的摆位**完全一致**，否则 `z - atan_lut[i]` 在定点下就会错位。
- 残角 \(z\) 的取值范围可达 \(\pm\pi/2 \approx \pm 1.571\)，**超过 ±1**，所以 \(z\) 不能用 `Q0.(W-1)`（范围 \([-1,+1)\)），必须用 `Q1.(W-2)`（范围 \([-2,+2)\)）。于是 `atan_lut` 也要按 `Q1.(W-2)` 量化——这就是后面那个 `2^(W-2)` 的来历。

#### 4.2.2 核心流程

三步量化流水线（`gen_atan_lut.m` 与 `cordic.m` 共用）：

```
① 算实数角度：    a[i] = atan(2^-i),                i = 0 .. N-1
② 均匀量化：      q[i] = uquant(a[i], W, "s")        // 有符号, xmax=1, nbits=W
③ 缩放取整：      s[i] = floor( q[i] * 2^(W-2) )     // 摆成 Q1.(W-2)
最终写入：        assign atan_lut[i] = <W>'sd<s[i];
```

`uquant` 的有符号量化粒度（[uquant.m:82-88](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/uquant.m#L82-L88)）为

\[
\text{bucksize}=\frac{x_{\max}}{2^{W}/2-1}=\frac{1}{2^{W-1}-1}
\]

即 `q = round(a / bucksize) * bucksize`。`W=16` 时粒度为 `1/32767`，极细，故 `uquant` 对结果只可能有 ±1 LSB 级的扰动——但确实会出现（见 4.2.4）。

#### 4.2.3 源码精读

表声明与包含：[sgen_cordic.v:33](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L33) 声明 `atan_lut` 为 `gp_angle_depth` 项的数组，[sgen_cordic.v:35](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L35) 用 `\`include "atan_lut.v"` 把构建期生成的赋值语句钩进来——`atan_lut.v` 是**构建产物**，不入 git。

表的取用点（验证它的摆位必须与 `z` 一致）：

- 展开式：[sgen_cordic.v:86](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L86) —— `assign z[i] = (d[i]) ? z[i-1] - atan_lut[i] : z[i-1] + atan_lut[i];`
- 迭代式：[sgen_cordic.v:158-159](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/rtl/sgen_cordic.v#L158-L159) —— `w_z` 取 `atan_lut[r_count_iter]`。

量化三连（`gen_atan_lut.m`）：

- [gen_atan_lut.m:6](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/gen_atan_lut.m#L6) —— `atan_lut = atan(2.^-[0:n_iter-1]);` 生成实数角度向量。
- [gen_atan_lut.m:7](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/gen_atan_lut.m#L7) —— `uquant(..., bitwidth, "s")` 有符号量化。
- [gen_atan_lut.m:8](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/gen_atan_lut.m#L8) —— `floor(atan_lut_q.*2^(bitwidth-2));` 摆成 `Q1.(W-2)`。
- [gen_atan_lut.m:9-12](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/gen_atan_lut.m#L9-L12) —— `fprintf` 按 `assign atan_lut[%d] = %d'sd%d;` 逐行写文件。

#### 4.2.4 代码实践

**目标**：对照实数值，验证前 4 个表项确实是 \(\arctan(2^{-i})\) 的定点量化。

**前置算式**：`atan_lut[0]` 走完整流水线（`W=16`）：

\[
a_0=\arctan(1)=\frac{\pi}{4}=0.78539816\ldots
\]

\[
\text{round}(a_0\cdot 32767)=\text{round}(25735.14)=25735
\]

\[
s_0=\left\lfloor \frac{25735}{32767}\times 2^{14} \right\rfloor=\lfloor 12867.107 \rfloor=\boxed{12867}
\]

即 `atan_lut.v` 第一行应为 `assign atan_lut[0] = 16'sd12867;`（已手算验证）。

**前 4 项的实数角度与「朴素」量化值**（朴素指 `floor(a·2^14)`，未经 `uquant`；实际 `uquant` 可能让末位差 1）：

| i | \(\tan\alpha_i=2^{-i}\) | \(\alpha_i=\arctan(2^{-i})\)（弧度） | 朴素 `floor(a·2^14)` | 实际表值（经 uquant） |
|---|---|---|---|---|
| 0 | 1    | 0.78539816 | 12867 | **12867**（已验证） |
| 1 | 0.5  | 0.46364761 | 7596  | 待本地验证 |
| 2 | 0.25 | 0.24497866 | 4013  | 待本地验证 |
| 3 | 0.125| 0.12435499 | 2037  | 待本地验证 |

**操作步骤**：

1. 在本地装好 Octave，进入 `.drl_src_code/sgen_cordic/octave/`（该目录已自带 `uquant.m`、`ltfatarghelper.m`，无需另装 LTFAT 工具箱）。
2. 执行 `gen_atan_lut(16, 16);`（参数顺序：`n_iter, bitwidth`），同目录会生成 `atan_lut.v`。
3. 打开 `atan_lut.v`，核对前 4 行是否落在上表的量级，重点确认 `atan_lut[0] = 16'sd12867;`。

**需要观察的现象**：

- `atan_lut[1..3]` 可能与「朴素」列差 1。原因：`uquant` 先把角度舍入到 `1/32767` 网格再除以 2，引入了一个略小于 1 LSB 的向下偏置。这正是「待本地验证」的趣味所在——把生成结果与朴素值对比，找出哪几项被 `uquant` 拉低了 1。

**预期结果**：`atan_lut[0] = 16'sd12867;` 确认无误；其余三项与上表「朴素」列相差不超过 1。

#### 4.2.5 小练习与答案

**练习 1**：为什么缩放因子是 `2^(W-2)` 而不是 `2^(W-1)`？

> **答**：残角 \(z\) 要表示到 \(\pm\pi/2\approx\pm1.571\)，超出 `Q0.(W-1)` 的 \([-1,+1)\) 范围，必须用 `Q1.(W-2)`（留 1 位整数位，范围 \([-2,+2)\)）。`atan_lut` 与 \(z\) 同摆位才能保证 `z ± atan_lut[i]` 精确对齐，故同样用 `2^(W-2)`。

**练习 2**：`atan_lut` 最大的一项是哪个？它会溢出 `Q1.(W-2)` 的范围吗？

> **答**：`atan_lut[0] = atan(1) = π/4 ≈ 0.7854 < 1`，是最大项；`0.7854 < 2`，远在 `Q1.14` 的 \([-2,+2)\) 范围内，不溢出。

**练习 3**：把 `bitwidth` 从 16 改成 8，`atan_lut[0]` 大致会变成多少？

> **答**：`2^(8-2)=2^6=64`，`floor(0.7854×64)=floor(50.27)=50`（再经 `uquant` 可能差 1）。位宽越窄，角度分辨率越粗。

---

### 4.3 gen_atan_lut.m 生成流程与 GRM 一致性

#### 4.3.1 概念说明

`gen_atan_lut.m` 是一个**独立的小工具**：给它级数和位宽，它吐出一个 `atan_lut.v`。但在 DRL 的真实构建流程里，`atan_lut.v` 并不是靠手动跑这个脚本产生的，而是**跑黄金参考模型时顺带生成**的。理解这条链路，就理解了 DRL「比特真」的第二处锁扣。

关键事实：`cordic.m`（GRM）内部**逐字复制**了 `gen_atan_lut.m` 的同一段量化代码。这意味着——无论你用哪个入口生成 `atan_lut.v`，RTL 拿到的角度表与 GRM 用来算标准答案的角度表**是同一张表**。表相同，旋转轨迹就相同，逐级 \(x,y,z\) 才能逐比特对齐。

#### 4.3.2 核心流程

从源码到比特真的完整链路：

```
stimuli.m  ──装配 defines（含 gp_angle_width/depth, gp_gain_width）──▶ cordic(defines,x,y,z)
                                                                    │
                                       ┌────────────────────────────┼────────────────────────────┐
                                       ▼                            ▼                            ▼
                              算 CORDIC 标准答案         内嵌 gen_atan_lut 同款代码         gen_defines(defines)
                              （乘 gain=0.607252959…）   写 atan_lut.v                    写 defines.sv（P_* 宏）
                                       │                            │                            │
                                       ▼                            ▼                            ▼
                          response_tc_*_oup_mat.dat     mv atan_lut.v ../rtl/           defines.sv
                                       │                            │                            │
                                       └────────────┬───────────────┴────────────────────────────┘
                                                    ▼
                                    iverilog 编译：sgen_cordic.v + `include atan_lut.v + defines.sv
                                                    │
                                                    ▼
                                    测试台逐样本比对 RTL 与 GRM → error_count==0 → PASSED
```

两处锁扣：

1. **增益锁扣**：RTL 的 `c_1_gain` 与 GRM 的 `gain` 都是 `0.607252959138945`。
2. **角度表锁扣**：RTL `\`include` 的 `atan_lut.v` 由 GRM 用同一段代码生成。

#### 4.3.3 源码精读

GRM 内嵌的 `atan_lut.v` 生成——与 `gen_atan_lut.m` 完全一致：

[cordic.m:116-123](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/cordic.m#L116-L123) —— 注意 `lut_width`、`lut_depth` 直接来自 `defines.gp_angle_width/depth`（[cordic.m:9-10](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/cordic.m#L9-L10)），而角度向量在 [cordic.m:16-21](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/cordic.m#L16-L21) 用 `atan(2^-j)` 构造（`j=i-1`），与 `gen_atan_lut.m:6` 的 `atan(2.^-[0:n_iter-1])` 同源。增益常量则在 [cordic.m:22](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/cordic.m#L22) 写死为 `0.607252959138945`（注释里 `%prod(gain_lut)` 表示本可由 `gain_lut` 连乘得到，但作者选择直接写常数）。

入口装配与搬运：

- [stimuli.m:5-12](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/stimuli.m#L5-L12) —— `defines` 结构，注意默认 `gp_gain_width = 24`（不是 12！），`gp_angle_depth = 24` 而 `gp_nr_iter = 12`（表比级数深，留余量）。
- [stimuli.m:87](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/stimuli.m#L87) —— `gen_defines(defines)` 生成 `defines.sv`（提供 `P_ATAN_GAIN_WIDTH` 等宏）。
- [stimuli.m:89-90](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/stimuli.m#L89-L90) —— 调 `cordic(...)`，`atan_lut.v` 作为副作用被写出。
- [stimuli.m:98](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/stimuli.m#L98) —— `system("mv atan_lut.v ../rtl/")`，把它搬进 RTL 目录，供 `\`include` 找到。

测试台如何串接补偿模块：

- [sgen_cordic_tb.sv:129-148](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/sim/testbench/sgen_cordic_tb.sv#L129-L148) —— 例化 `sgen_cordic`，其 `o_x/o_y` 接到内部线 `x_2_gain/y_2_gain`。
- [sgen_cordic_tb.sv:150-160](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/sim/testbench/sgen_cordic_tb.sv#L150-L160) —— 把 `x_2_gain/y_2_gain` 喂给 `cordic_gain`，演示「DUT 原始输出 → 补偿」的正确接线顺序；`gp_gain_width` 用 `` `P_ATAN_GAIN_WIDTH `` 宏传入。

> **阅读型发现（待你确认）**：在这个测试台里，`cordic_gain` 的输出 `o_cordic_x/o_cordic_y` 被留空（[L158-159](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/sim/testbench/sgen_cordic_tb.sv#L158-L159) 指向 `()`），且 `error_count` 在本文件内未见自增。换言之，`sgen_cordic` 的比特真比对链尚未在本测试台完全接通，`cordic_gain` 此处更像一个「接线示范」。这并不影响本讲对模块本身的理解，但值得你在读源码时留意。

#### 4.3.4 代码实践

**目标**：亲手用独立脚本生成 `atan_lut.v`，并对比它与 GRM 生成的版本是否一致，验证「两处锁扣」。

**操作步骤**：

1. 进入 `.drl_src_code/sgen_cordic/octave/`，运行 `gen_atan_lut(16, 16);`，得到 `atan_lut.v`（记为「版本 A」）。
2. 把生成的 `atan_lut.v` 头 5 行贴出，确认格式为 `assign atan_lut[k] = 16'sd<整数>;`，且 `atan_lut[0] = 16'sd12867;`。
3. （进阶）跑一次 `stimuli`（或直接调 `cordic(defines, 1, 0, 12*pi/180)`），它会再写一份 `atan_lut.v`（记为「版本 B」）。用 `diff` 比对版本 A 与版本 B：只要 `bitwidth/depth` 参数一致，两者应当**完全相同**——这就是「角度表锁扣」的可视化证据。

**需要观察的现象**：

- 版本 A 与版本 B 逐字节相同 → 证明 `gen_atan_lut.m` 与 `cordic.m` 内嵌代码同源。
- 把 `bitwidth` 从 16 改为 8，两份表同步变窄，但依旧彼此相同。

**预期结果**：两份 `atan_lut.v` 在相同参数下完全一致；`atan_lut[0]` 恒为 `16'sd12867`。

> 若本地未装 Octave，本实践可降级为「源码阅读型」：逐行比对 [gen_atan_lut.m:6-12](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/gen_atan_lut.m#L6-L12) 与 [cordic.m:117-123](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_cordic/octave/cordic.m#L117-L123)，确认公式与 `fprintf` 模板逐字对应。

#### 4.3.5 小练习与答案

**练习 1**：`stimuli.m` 默认 `gp_angle_depth=24` 但 `gp_nr_iter=12`，为什么表要做得比级数深？

> **答**：表深度 ≥ 迭代级数即可让 `atan_lut[i]` 不越界；做深一些是给「换更大 `gp_nr_iter` 跑用例」留余量（如 `stimuli.m` 的 case 6/7 把 `gp_nr_iter` 调到 24）。多出的表项不会被使用，但也不额外占运行时——它是编译期 ROM。

**练习 2**：假设有人手写了一个「更精确」的 `atan_lut.v`（用 `2^(W-1)` 摆位而非 `2^(W-2)`），但没改 GRM。比特真还会成立吗？

> **答**：不会。RTL 与 GRM 的 \(z\) 摆位将错开一个 2 倍因子，`z ± atan_lut[i]` 在两侧对不上，逐级轨迹发散。这正说明「同一份表」是比特真的硬要求，而非可有可无的便利。

**练习 3**：为什么 `cordic.m` 要内嵌 `atan_lut.v` 的生成代码，而不是让用户单独跑 `gen_atan_lut.m`？

> **答**：把生成绑进 GRM，能从机制上保证「RTL 用的表」与「GRM 算答案用的表」同源同参数，消除「用户忘跑脚本或参数不一致」的人为失误。`gen_atan_lut.m` 则是抽出来的独立工具，便于单独检视与调试。

## 5. 综合实践

把本讲三块内容串成一个端到端的小调研：**增益位宽与角度表位宽，谁是精度的瓶颈？**

1. 选定一组小参数（如 `gp_nr_iter=8, gp_xy_width=8, gp_z_width=16`）。
2. 让 `gp_gain_width` 在 `{8, 12, 16, 24}` 之间变化，分别手算 `c_1_gain` 与「实现增益」`c_1_gain/2^(W-1)`，绘制实现增益随 `gp_gain_width` 收敛到 `0.607252959138945` 的曲线（可用纸笔或任意工具）。
3. 固定 `gp_gain_width=24`，让角度表 `bitwidth` 在 `{8, 12, 16}` 之间变化，运行 `gen_atan_lut(bitwidth, bitwidth)`，记录 `atan_lut[0]` 与理想值 `π/4` 的相对误差。
4. 回答：当两者都很宽时，CORDIC 整体误差由谁主导？（提示：`gp_nr_iter` 决定算法本身能逼近到多少位，详见 [u6-l1](u6-l1-cordic-algorithm.md) 的收敛性讨论。）

**交付物**：一张三列表（`参数 → 该参数的量化误差 → 结论`），并写一句话总结「在 DRL 里，要保证比特真，最关键的不是把某个常数算到多精，而是 RTL 与 GRM 用同一份常数与同一张表」。

## 6. 本讲小结

- CORDIC 伪旋转把向量累计放大约 \(K\approx1.6468\) 倍，`cordic_gain` 用一次乘 `1/K\approx0.6073` 把它补回。
- 补偿在定点下是「乘常数 `c_1_gain = $rtoi(0.607252959138945·2^(W-1))` + 算术右移 `(W-1)` 位」；`gp_gain_width=12` 时 `c_1_gain = 1243`。
- 旋转模式对 \(x,y\) 都补偿；矢量模式只补偿 \(x\)（幅值），\(y\) 置零。
- `atan_lut` 经 `atan → uquant → floor(·2^(W-2))` 三步量化；`W-2` 是为了与残角 \(z\) 的 `Q1.(W-2)` 摆位对齐（\(z\) 需表示到 \(\pm\pi/2\)）。`atan_lut[0] = 16'sd12867`（`W=16`，已验证）。
- **两处比特真锁扣**：①增益常数 `0.607252959138945` 在 RTL 与 GRM 同值；②`atan_lut.v` 由 GRM 内嵌的同款代码生成（`gen_atan_lut.m` 是其独立版本）。
- `stimuli.m` 跑 GRM 时顺带写出 `atan_lut.v` 并 `mv` 进 `../rtl/`，供 `\`include` 钩入；测试台把 DUT 输出串一级 `cordic_gain` 作接线示范。

## 7. 下一步学习建议

- 本单元最后一篇 [u6-l3](u6-l3-nco-and-rom-reconstruction.md) 转向另一种信号生成器 `sgen_nco`：它用相位累加器 + 四分之一波长 ROM + 象限重构还原正余弦，可与本讲的「查表 + 定点」思路对照——CORDIC 用算法算三角函数，NCO 用查表读三角函数，各擅胜场。
- 想巩固「构建产物由 GRM 生成」这一模式，可复习 [u1-l3](u1-l3-toolchain-and-build-flow.md) 的 `design→octave→iverilog→比对` 流水线，并把本讲的 `atan_lut.v` 代入「GRM 生成 → 测试台消费」的角色。
- 若对「比特真比对闭环」感兴趣，可读 [u7-l1](u7-l1-bittrue-verification.md)；届时可回头审视本讲提到的 `sgen_cordic_tb.sv` 中 `cordic_gain` 输出未接、`error_count` 未自增的现象，思考如何把它补成一个真正自检的测试台。
