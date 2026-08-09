# CIC 位宽推导与黄金参考模型

## 1. 本讲目标

学完本讲后，你应该能够：

- 推导并手算 CIC 抽取滤波器的输出位宽 `gp_oup_width`，理解它来自 Hogenauer 增长公式 \(B_{out}=B_{in}+N\lceil\log_2(RM)\rceil\)。
- 解释 `stimuli.m` 中「增益缩放因子」`SF` 的物理含义，并能判断它在什么条件下等于 1、什么时候大于 1。
- 读懂 Octave 黄金参考模型 `CICFilter.m` 是如何用「全系数多项式 + 抽取相位」产出与 RTL 逐比特一致的响应的，从而理解「比特真」的数学根基。

本讲是 u4-l1（CIC 原理与 `filt_cicd` 抽取滤波器）的延续。u4-l1 讲清了「积分器→下采样→微分器」的数据流与符号扩展；本讲不再重复结构，而是聚焦三件事：**位宽到底怎么算出来、算得有多保守、以及 Octave 用什么公式给出标准答案**。

## 2. 前置知识

阅读本讲前，请先掌握：

- **补码定点与符号扩展**（u2-l1）：CIC 全程在有符号补码下运算，输入要用 MSB 填充到输出位宽。
- **`$clog2` 与 `ceil(log2)` 等价**（u2-l1）：RTL 的 `$clog2(x)` 即 \(\lceil\log_2 x\rceil\)，Octave 的 `ceil(log2(x))` 完全一致——这是 RTL 与 GRM 比特真的基石。
- **CIC 抽取器结构与 `filt_cicd` 模块**（u4-l1）：积分器 \(1/(1-z^{-1})\)、梳状 \(1-z^{-RM}\)、下采样插在中间、环形计数器选相位。
- **`dff` / `shift_register` 原语**（u2-l2、u2-l3）。

补充一个本讲会用到的数学小结论：对任意正整数 \(X\)，记 \(k=\lceil\log_2 X\rceil\)，则

\[
2^{k-1} < X \le 2^{k}
\]

当 \(X\) 恰为 2 的幂时 \(X=2^k\)；否则 \(X<2^k\)。这个区间会在解释 `SF` 时反复用到。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [.drl_src_code/filt_cicd/rtl/filt_cicd.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v) | RTL 实现。本讲聚焦第 12 行的位宽公式、第 22 行的填充宽度、第 60 行的符号扩展。 |
| [.drl_src_code/filt_cicd/octave/stimuli.m](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m) | GRM 的驱动脚本。定义参数、计算位宽 `BG` 与缩放因子 `SF`、调用 `CICFilter.m`、写激励/响应文件。 |
| [.drl_src_code/filt_cicd/octave/CICFilter.m](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/CICFilter.m) | 黄金参考模型本体：用 `filt` 构造单级传递函数，自乘 N 次得到 CIC，再按相位抽取。 |
| [.drl_src_code/filt_cicd/octave/gen_defines.m](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/gen_defines.m) | 把参数写成 `defines_N.sv` 宏，供「哑」测试台注入（参数同步的一环）。 |

---

## 4. 核心概念与源码讲解

### 4.1 Hogenauer 位宽公式

#### 4.1.1 概念说明

CIC 滤波器**没有乘法器**，但它会**放大信号**。问题是：放大多少？需要多少位才不会溢出？

N 级 CIC 抽取器的传递函数（参见 u4-l1）为

\[
H(z)=\left(\frac{1-z^{-RM}}{1-z^{-1}}\right)^{N}
\]

其中 \(R\) 是抽取因子、\(M\) 是差分延迟、\(N\) 是级数。利用恒等式

\[
\frac{1-z^{-RM}}{1-z^{-1}} = 1+z^{-1}+z^{-2}+\cdots+z^{-(RM-1)}
\]

可知单级在直流（\(z=1\)）处的增益是 \(RM\)（\(RM\) 个 1 相加）。N 级级联后直流增益为

\[
|H(1)| = (RM)^{N}
\]

也就是说，一个直流（常数）输入会被放大约 \((RM)^N\) 倍。要无溢出地表示这个放大后的结果，相对于输入需要多出的二进制位数是

\[
\log_2\big((RM)^{N}\big) = N\log_2(RM) \text{ 位}
\`

由于位宽必须是整数，工程上要向上取整。Hogenauer 给出的「输出寄存器位宽」上界（本库采用的保守形式）是

\[
\boxed{\,B_{out} = B_{in} + N\cdot\lceil\log_2(RM)\rceil\,}
\]

关键设计决策：**每一级（积分器与梳状）都使用完整的 \(B_{out}\) 位宽运算**，中间不截断。这是 Hogenauer 论文的核心结论之一——只要内部全宽运算，就保证各级不溢出。我们在 4.1.3 会看到 RTL 正是这么做：输入先符号扩展到 `gp_oup_width`，之后积分器、抽取、梳状全部在 `gp_oup_width` 上运行。

> 注：更紧的 Hogenauer 公式是 \(B_{in}+\lceil N\log_2(RM)\rceil\)（先乘后取整）。本库用的是 \(B_{in}+N\lceil\log_2(RM)\rceil\)（先取整后乘）。因为 \(\lceil\cdot\rceil\) 后再乘 N 总是不小于先乘再取整，所以**本库公式是保守上界**，永远多分不少分，绝对不会溢出，代价是当 \(RM\) 不是 2 的幂时浪费几位高位。这个「浪费」正是 4.2 节 `SF` 要度量的东西。

#### 4.1.2 核心流程

位宽从参数推导到落地的流程：

1. 设计者设定 \(R\)（抽取因子）、\(N\)（级数）、\(M\)（差分延迟）、\(B_{in}\)（输入位宽）。
2. 公式自动算出 \(B_{out}=B_{in}+N\lceil\log_2(RM)\rceil\)。
3. RTL 把输入 MSB 符号扩展 \(B_{out}-B_{in}\) 位，补齐到全宽。
4. 积分器、抽取寄存器、梳状移位寄存器**全部按 \(B_{out}\) 位宽例化**。
5. 同一个公式在 Octave 的 `stimuli.m` 里用 `ceil(log2(...))` 再算一遍，保证两边位宽一致 → 比特真的前提。

#### 4.1.3 源码精读

**位宽公式**直接写在参数默认值里，是「派生参数」（u1-l3 讲过派生参数因表达式行无裸数字而免遭 sed 误伤）：

[.drl_src_code/filt_cicd/rtl/filt_cicd.v:L6-L13](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L6-L13) —— 这里 `gp_oup_width` 的默认表达式就是 Hogenauer 公式本身：

```verilog
parameter gp_oup_width = gp_inp_width + gp_order*$clog2(gp_decimation_factor*gp_diff_delay)
```

对照数学式 \(B_{out}=B_{in}+N\lceil\log_2(RM)\rceil\)，逐项对应：`gp_inp_width`=\(B_{in}\)、`gp_order`=\(N\)、`$clog2(gp_decimation_factor*gp_diff_delay)`=\(\lceil\log_2(RM)\rceil\)。

**填充宽度**派生自上式，决定要复制多少位符号位：

[.drl_src_code/filt_cicd/rtl/filt_cicd.v:L22-L22](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L22) —— `c_fill_width = gp_oup_width - gp_inp_width`，即纯增益部分 \(N\lceil\log_2(RM)\rceil\)。

**符号扩展**把输入从 \(B_{in}\) 拉到 \(B_{out}\)：

[.drl_src_code/filt_cicd/rtl/filt_cicd.v:L60-L60](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L60) —— `assign w_data = {{c_fill_width{i_data[gp_inp_width-1]}},i_data};`，把输入最高位（符号位）复制 `c_fill_width` 份填到高位，这正是 u2-l1 讲的 `{{fill{sign}}, data}` 模式。

**全宽例化**：积分器里的 `dff` 与梳状里的 `shift_register` 都用 `gp_oup_width` 作为数据宽度，确认「内部不截断」：

[.drl_src_code/filt_cicd/rtl/filt_cicd.v:L65-L95](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L65-L95) —— 积分器段，每个 `dff` 例化 `.gp_data_width(gp_oup_width)`。

**Octave 侧同一公式**：

[.drl_src_code/filt_cicd/octave/stimuli.m:L12-L12](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L12) —— `gp_oup_width = gp_inp_width + gp_order*ceil(log2(gp_decimation_factor*gp_diff_delay));`，把 `$clog2` 换成 `ceil(log2(...))`，数学完全等价。这就是 u2-l1 所说的「RTL `$clog2` == GRM `ceil(log2)`」在 CIC 上的具体兑现。

#### 4.1.4 代码实践

**实践目标**：手算两种配置的 `gp_oup_width`，并用 iverilog 仅做 elaborate（展开）打印模块参数，验证手算与 RTL 一致。

**操作步骤**：

1. 手算。配置 A（RTL 默认）：\(R=4,N=3,M=1,B_{in}=8\)。因 \(RM=4=2^2\)，\(\lceil\log_2 4\rceil=2\)，故 \(B_{out}=8+3\times2=14\)。
   配置 B（`filt_cicd_1.param` 默认）：\(R=4,N=5,M=1,B_{in}=8\)，\(B_{out}=8+5\times2=18\)。
2. 写一个仅用于展开的最小顶层（**示例代码**，不含复位/时钟驱动）：

   ```verilog
   module tb_bw;
     filt_cicd #(.gp_decimation_factor(4), .gp_order(3),
                 .gp_diff_delay(1), .gp_inp_width(8)) u (
       .i_rst_an(1'b1), .i_ena(1'b0), .i_clk(1'b0),
       .i_data(8'sd0), .o_data());
     initial begin
       $display("gp_oup_width = %0d", u.gp_oup_width);
       $display("c_fill_width = %0d", u.c_fill_width);
       $finish;
     end
   endmodule
   ```

3. 用 iverilog 编译并运行：`iverilog -o tb_bw.vvp .drl_src_code/filt_cicd/rtl/filt_cicd.v .drl_src_code/filt_cicd/rtl/dff.v .drl_src_code/filt_cicd/rtl/shift_register.v tb_bw.v && vvp tb_bw.vvp`（顶层文件名以实际为准）。

**需要观察的现象**：终端打印的 `gp_oup_width` 与 `c_fill_width`。

**预期结果**：配置 A 打印 `gp_oup_width = 14`、`c_fill_width = 6`；配置 B 打印 `gp_oup_width = 18`、`c_fill_width = 10`。若引用模块参数的写法在你本地 iverilog 版本上报错，可改为直接 `$display` 同一表达式 `8 + 3*$clog2(4*1)`。

> 本地未实际运行上述命令，确切语法**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：把 `gp_diff_delay` 从 1 改成 2（其余按 RTL 默认 \(R=4,N=3,B_{in}=8\)），`gp_oup_width` 变成多少？

**答案**：\(RM=4\times2=8=2^3\)，\(\lceil\log_2 8\rceil=3\)，\(B_{out}=8+3\times3=17\)。

**练习 2**：为什么本库公式 \(B_{in}+N\lceil\log_2(RM)\rceil\) 一定不小于更紧的形式 \(B_{in}+\lceil N\log_2(RM)\rceil\)？

**答案**：因为 \(\lceil\log_2(RM)\rceil\ge\log_2(RM)\)，两边乘正数 \(N\) 得 \(N\lceil\log_2(RM)\rceil\ge N\log_2(RM)\)。左式已是整数，而右式的「最小不小于整数」就是 \(\lceil N\log_2(RM)\rceil\)，故左式 \(\ge\) 右式。所以本库公式是安全上界。

---

### 4.2 增益缩放因子 SF

#### 4.2.1 概念说明

`stimuli.m` 里有一个量 `SF`，注释写着 `%[1; 2)`。它度量的是：**位宽公式「保守多分配」的增益，相对于真实直流增益，富裕了多少倍**。

回忆 4.1：真实直流增益是 \((RM)^N\)，而位宽公式按 \(N\lceil\log_2(RM)\rceil\) 位增长来分配，对应「可表示的最大摆幅倍数」是 \(2^{N\lceil\log_2(RM)\rceil}\)。两者之比就是 `SF`：

\[
SF = \frac{2^{N\lceil\log_2(RM)\rceil}}{(RM)^{N}} = \left(\frac{2^{\lceil\log_2(RM)\rceil}}{RM}\right)^{N}
\]

记**单级因子** \(f=\dfrac{2^{\lceil\log_2(RM)\rceil}}{RM}\)，则 \(SF=f^{N}\)。

由 4.1 节开头的小结论可知：

- 若 \(RM\) 是 2 的幂，\(RM=2^k\)，则 \(f=2^k/2^k=1\)。
- 否则 \(2^{k-1}<RM<2^k\)，则 \(1<f<2\)。

所以**单级因子 \(f\) 恒在 \([1,2)\) 区间**——这正是源码注释 `%[1; 2)` 的真正含义。

但要注意：变量 `SF` 本身是 \(f^N\)，不是 \(f\)。当 \(RM\) 不是 2 的幂且级数 \(N\) 较大时，\(SF\) 会远超 2。这是本讲最容易被源码注释误导、需要仔细分辨的地方（见 4.2.4 的实践）。

`SF` 的工程意义：它告诉你输出寄存器高位有多少「空挡」。\(SF=1\) 表示信号恰好用满分配的位宽（无富裕）；\(SF>1\) 表示真实输出永远到不了满量程，最高几位基本是符号扩展位。

#### 4.2.2 核心流程

判断 `SF` 的步骤：

1. 求 \(k=\lceil\log_2(RM)\rceil\)。
2. 算单级因子 \(f=2^k/RM\)，它必落在 \([1,2)\)。
3. `SF` \(=f^N\)。
4. 仅当 \(RM\) 是 2 的幂时 `SF=1`；否则 `SF>1`，且随 \(N\) 增大而指数增长。

#### 4.2.3 源码精读

**`SF` 的定义**（注意它对真实增益做了归一，但保留了保守分配的幂）：

[.drl_src_code/filt_cicd/octave/stimuli.m:L14-L16](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L14-L16)：

```octave
BG = gp_order * ceil(log2(gp_decimation_factor*gp_diff_delay)) + gp_inp_width
SF = 2^(gp_order*ceil(log2(gp_decimation_factor*gp_diff_delay)))/((gp_decimation_factor*gp_diff_delay)^gp_order) %[1; 2)
```

- `BG` 就是 `gp_oup_width` 本身（同一表达式，打印出来供人核对）。
- `SF` 分子是 \(2^{N\lceil\log_2(RM)\rceil}\)（保守分配的增益），分母是 \((RM)^N\)（真实直流增益）。
- 注释 `%[1; 2)` 描述的其实是**单级因子 \(f\)**；当 `gp_order=1` 或 \(RM\) 为 2 的幂时 `SF` 才真正落在 \([1,2)\)。

`SF` 并不参与 RTL 综合（它是 GRM 脚本里的诊断量），但它解释了为什么 `stimuli.m` 故意选用 \(R=17\)（非 2 的幂）做激励——为了让位宽公式的「保守性」可见，而不是被「恰好是 2 的幂」掩盖。

#### 4.2.4 代码实践

**实践目标**：手算 `stimuli.m` 当前参数下的 `SF`，验证它**并不**在 \([1,2)\)，并解释源码注释的适用范围。

**操作步骤**：

1. 取 `stimuli.m` 的参数 \(R=17,N=8,M=1\)。
2. 求 \(k=\lceil\log_2 17\rceil\)：因为 \(2^4=16<17<32=2^5\)，故 \(k=5\)。
3. 单级因子 \(f=2^5/17=32/17\approx1.882\)（确实在 \((1,2)\)）。
4. `SF` \(=f^8=(32/17)^8\approx157.6\)。
5. 用等价直算验算：\(SF=2^{40}/17^8=1\,099\,511\,627\,776\,/\,6\,975\,757\,441\approx157.6\)。

**需要观察的现象**：`SF` 远大于 2。

**预期结果**：`SF≈157.6`，**不在** \([1,2)\)。这说明源码注释 `%[1; 2)` 仅对单级因子 \(f\)（或 `gp_order=1`、或 \(RM\) 为 2 的幂）成立；对本配置（\(N=8\) 且 \(RM=17\) 非 2 的幂）不成立。

> 对照默认参数（\(RM=4\) 是 2 的幂）：\(f=2^2/4=1\)，`SF`=\(1^N=1\)，对任意 \(N\) 都在 \([1,2)\)——这就是为什么默认配置下注释看起来「总是对的」，而换到 \(R=17\) 就暴露了它的局限。

#### 4.2.5 小练习与答案

**练习 1**：若 \(RM=8\)（2 的幂）、\(N=4\)，求 `SF`。

**答案**：\(k=\lceil\log_2 8\rceil=3\)，\(f=2^3/8=1\)，`SF`\(=1^4=1\)。凡是 \(RM\) 为 2 的幂，`SF` 恒为 1，与 \(N\) 无关。

**练习 2**：在相同 \(RM=17\) 下，级数 \(N\) 翻倍（从 8 到 16），`SF` 变成原来的几倍？

**答案**：`SF`\(=f^N\)，\(N\) 翻倍则 `SF` 变成原来的 \(f^8\approx157.6\) 倍（即 \(SF_{N=16}=f^{16}=(f^8)^2\approx157.6^2\approx2.48\times10^4\)）。这说明保守分配的「富裕」随级数指数膨胀。

---

### 4.3 CICFilter.m 黄金参考模型

#### 4.3.1 概念说明

黄金参考模型（GRM）的职责是：给定与 RTL **完全相同**的参数和输入，给出「标准答案」。`CICFilter.m` 就是 CIC 的 GRM。它要做到「比特真」——输出与 RTL 逐比特一致——关键在于：**用整数系数做无舍入的整数运算**。

CIC 的传递函数多项式 \(1+z^{-1}+\cdots+z^{-(RM-1)}\) 全是整数系数（每个抽头都是 1），自乘 \(N\) 次后仍是整数多项式。所以用整数输入去 `filter`，结果天然是精确整数，没有任何浮点舍入误差。这与 RTL 内部「全整数补码运算」完全对应——这就是比特真能成立的根本原因。

#### 4.3.2 核心流程

`CICFilter.m` 的算法分四步：

1. **构造单级箱形系数**：`Num = ones(1, R*M)`，即长度为 \(RM\)、全 1 的滑动平均系数，对应 \((1-z^{-RM})/(1-z^{-1})=1+z^{-1}+\cdots+z^{-(RM-1)}\)。
2. **建成 z 域模型并自乘 N 次**：`H = filt(Num, Den, 1/Fs)` 得到单级离散传递函数；`Hcic = (H^N)` 等价于 N 级级联。注意归一化项 `/((R*M)^N)` 被**注释掉了**，所以 `Hcic` 保留了真实直流增益 \((RM)^N\)——这是比特真的必要条件。
3. **取出最终多项式系数**：`Num_CIC = cell2mat(Hcic.num)`、`Den_CIC = cell2mat(Hcic.den)`，得到展开后的整数分子/分母。
4. **滤波 + 按相位抽取**：`FilteredData = downsample(filter(Num_CIC, Den_CIC, Data), R, P)`，先用完整 CIC 滤波，再按抽取因子 \(R\)、相位 \(P\) 降采样。

第 4 步的相位抽取 \(P\) 与 RTL 的 `gp_phase` 对应：RTL 用 `w_sclk = r_count[gp_phase]` 选通下采样时刻，GRM 用 `downsample(..., R, P)` 选同一相位。

#### 4.3.3 源码精读

**函数签名**：

[CICFilter.m:L1-L1](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/CICFilter.m#L1) —— `function [FilteredData Hcic] = CICFilter(M, N, R, P, Fs, Data)`。

> 小提示：第 2 行的注释头写的是 `CICFilter(M, N, R, Fs, Data)`（5 个参数，漏了 `P`），与实际 6 参数签名不符。这是一处**注释漂移**（stale comment）——签名才是权威，调用方 `stimuli.m` 也是按 6 参数传的。阅读源码时要「以代码为准」。

**单级箱形系数**：

[CICFilter.m:L23-L26](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/CICFilter.m#L23-L26)：

```octave
Num     = ones(1,R*M);
Den     = 1;
H       = filt(Num,Den,1/Fs);
Hcic    = (H^N);%/((R*M)^N);
```

`Num = ones(1,R*M)` 是关键：它让单级增益为 \(RM\)，自乘 \(N\) 次后增益 \((RM)^N\)，与 RTL 的 Hogenauer 增长一致。被注释的 `/((R*M)^N)` 若启用会把直流增益归一到 1，那样就**不再比特真**了——所以它必须保持注释。

**滤波与按相位抽取**：

[CICFilter.m:L29-L29](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/CICFilter.m#L29) —— `FilteredData = downsample(filter(Num_CIC,Den_CIC,Data),R, P);`，先滤波后抽取，相位 \(P\) 与 RTL `gp_phase` 对齐。

**调用点（参数映射）**：

[stimuli.m:L126-L126](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L126) —— `[yy Hcic] = CICFilter(gp_diff_delay, gp_order, gp_decimation_factor, gp_phase, 1, octave_data);`，即 \(M=\)`gp_diff_delay`、\(N=\)`gp_order`、\(R=\)`gp_decimation_factor`、\(P=\)`gp_phase`、`Fs=1`。这套参数随后由 `gen_defines.m` 写进 `defines_N.sv`（[gen_defines.m:L5-L10](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/gen_defines.m#L5-L10)），测试台再用宏把同一组参数注入 DUT（详见 u1-l3、u7-l1）。**GRM 与 RTL 用同一参数、同一公式**，是比特真闭环的锁扣。

**响应落盘**：

[stimuli.m:L134-L139](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L134-L139) —— 把 `yy`（GRM 答案）写成 `response_tc_<n>_mat.dat`，把输入 `data` 写成 `stimuli_tc_<n>_mat.dat`，供测试台逐样本比对。

#### 4.3.4 代码实践

**实践目标**：在 Octave 中直接观察 `CICFilter.m` 如何把单级箱形系数「自乘 N 次」得到 CIC 多项式，并验证直流增益等于 \((RM)^N\)。

**操作步骤**：

1. 进入目录 `cd .drl_src_code/filt_cicd/octave`。
2. 在 Octave 交互环境运行（**示例代码**，参数取 `stimuli.m` 的 \(R=17,N=8,M=1\)）：

   ```octave
   M=1; N=8; R=17;
   Num = ones(1, R*M);
   H   = filt(Num, 1, 1);
   Hcic= (H^N);
   Num_CIC = cell2mat(Hcic.num);
   fprintf("length(Num_CIC) = %d\n", length(Num_CIC));
   fprintf("DC gain sum(Num_CIC) = %d\n", sum(Num_CIC));
   fprintf("(R*M)^N = %d\n", (R*M)^N);
   ```

   （`filt` 与 `downsample` 来自 Octave 的 signal 工具箱，若缺失需先 `pkg install -forge signal` 并 `pkg load signal`；具体包名**待本地验证**。）

**需要观察的现象**：`length(Num_CIC)` 与 `sum(Num_CIC)`。

**预期结果**：`length(Num_CIC) = N*(R*M-1)+1 = 8*16+1 = 129`（N 个长度 \(RM\) 的箱形卷积后总长）；`sum(Num_CIC)` 应等于 `(R*M)^N = 17^8 = 6975757441`，即直流增益恰为 \((RM)^N\)。这印证了 4.1 的位宽推导来源。

> 本地未实际运行，确切输出**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `Hcic = (H^N);%/((R*M)^N);` 里的归一化启用（取消注释），GRM 还比特真吗？为什么？

**答案**：不再比特真。启用后直流增益被除以 \((RM)^N\) 归一到 1，输出变成小数；而 RTL 保留全增益、做整数运算，两者数值不再逐比特相等。

**练习 2**：`downsample(filter(...), R, P)` 与「先抽取后滤波」结果一样吗？

**答案**：不一样。Noble 恒等式允许把**抽取**与**抗混叠滤波器**交换位置是有条件的；CIC 的 GRM 严格按 RTL 数据流「先完整滤波、再按相位抽取」实现，所以必须用 `downsample(filter(...))` 这一顺序，才能与 RTL 的积分器（先算全）→ 选相位下采样（后取）逐拍对齐。

---

## 5. 综合实践

**任务**：完成规格要求的端到端手算与验证，把「位宽公式 → 缩放因子 → GRM」三者串起来。

**参数**（即 `stimuli.m` 当前值）：\(R=17,\ N=8,\ M=1,\ B_{in}=2\)。

**步骤**：

1. **手算位宽**。\(RM=17\)，\(k=\lceil\log_2 17\rceil=5\)。
   - `gp_oup_width` \(=B_{in}+Nk=2+8\times5=42\) 位。
   - `c_fill_width` \(=42-2=40\) 位（输入要符号扩展 40 位）。
   - `BG`（`stimuli.m` 第 15 行）同样 \(=Nk+B_{in}=42\)。

2. **手算 SF**。
   - 单级因子 \(f=2^5/17=32/17\approx1.882\)。
   - `SF` \(=f^8=(32/17)^8\approx157.6\)（直算 \(2^{40}/17^8\approx157.6\)）。

3. **运行 GRM 核对**。在装好 Octave 与 signal 工具箱的环境执行 `octave stimuli.m`（或经 `./dsp_rtl_lib.sh -d filt_cicd_1` 触发，参见 u1-l3）。观察终端打印的 `BG` 与 `SF`。

4. **解释 SF 与 \([1,2)\) 的关系**（这是本题的关键，不要被源码注释带偏）：
   - 源码注释 `%[1; 2)` 描述的是**单级因子** \(f=2^{\lceil\log_2(RM)\rceil}/RM\)。由 4.1 的区间结论，\(f\) 恒在 \([1,2)\)：\(RM\) 为 2 的幂时 \(f=1\)，否则 \(f\in(1,2)\)。
   - 但变量 `SF` \(=f^N\)。本配置 \(N=8\)，故 `SF`\(\approx157.6\)，**远不在** \([1,2)\)。
   - 结论：注释只在 `gp_order=1` 或 \(RM\) 恰为 2 的幂时对 `SF` 成立；对默认参数（\(RM=4\)）`SF=1` 看似「永远成立」，换到 \(R=17\) 即暴露局限。

**预期结果**：`gp_oup_width=42`、`BG=42`、`SF≈157.6`；并能在报告中指出源码注释 `%[1; 2)` 的准确适用范围。若本地 Octave 打印与手算不符，优先检查 signal 工具箱是否加载、`filt`/`downsample` 是否可用。

> 综合实践的实际运行结果**待本地验证**。

## 6. 本讲小结

- CIC 输出位宽遵循 Hogenauer 保守上界 \(B_{out}=B_{in}+N\lceil\log_2(RM)\rceil\)，在 RTL 中即 `gp_oup_width` 的默认表达式（[filt_cicd.v:L12](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L12)），输入经 MSB 符号扩展后，积分器与梳状全程全宽运算、不截断。
- 该公式比更紧的 \(B_{in}+\lceil N\log_2(RM)\rceil\) 多分配几位，当 \(RM\) 不是 2 的幂时存在「富裕」。
- 增益缩放因子 `SF`\(=2^{N\lceil\log_2(RM)\rceil}/(RM)^N\) 度量这份富裕；**单级因子** \(f\in[1,2)\)（源码注释 `%[1;2)` 的真意），但 `SF`\(=f^N\) 在大 \(N\)、非 2 的幂 \(RM\) 下可远大于 2（如 \(R=17,N=8\) 时 \(\approx157.6\)）。
- GRM `CICFilter.m` 用整数箱形系数 `ones(1,R*M)` 自乘 N 次构造 CIC，保留真实增益 \((RM)^N\)（归一项被注释），再按相位 `downsample`，故输出为精确整数，与 RTL 整数补码运算逐比特一致——这是比特真的数学根基。
- RTL 的 `$clog2` 与 GRM 的 `ceil(log2)` 用同一公式、同一参数，构成比特真闭环的锁扣；阅读源码时注意 `CICFilter.m` 第 2 行注释头与实际签名不一致的「注释漂移」。

## 7. 下一步学习建议

- **进入 u5（多相滤波器）**：CIC 是无乘法器的多速率滤波器，多相（PPD/PPI）则把带乘法器的长 FIR 拆成并行支路实现高效抽取/插值，位宽增长规律（u2-l1）与多速率时钟思想（u4-l1/u4-l2）会在那里继续复用。
- **深入验证方法学（u7-l1、u7-l2）**：本讲的 GRM 是「比特真」的一环，u7 会把「GRM 生成激励/响应/defines → 测试台 TEXTIO 逐样本比对 → error_count 判定」的完整闭环系统讲解，并精读 `stimuli.m` 的九测试用例激励设计。
- **建议继续阅读的源码**：对照 `filt_cici/octave/` 下的插值 GRM，比较它与 `filt_cicd` 的 GRM 在「上采样 vs 下采样」处理上的镜像差异；并阅读 Hogenauer 原论文中各级位宽的逐级推导，理解本库「全宽不截断」选择的理论依据。
