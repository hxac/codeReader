# u3-l1 FIR 原理与 filt_fir 接口结构

## 1. 本讲目标

FIR（有限脉冲响应）滤波器是 DSP 里最基础、最常用的模块，也是 DSP-RTL-Lib（下称 DRL）中结构最直观的一个。学完本讲，你应当能够：

- 说清 FIR「抽头—延迟线—乘加」的卷积结构，并能写出离散卷积公式；
- 看懂 [`filt_fir.v`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v) 的参数表、端口与 `localparam`，并解释输出位宽 `gp_oup_width` 是如何被自动推导出来的；
- 理解 `` `include "filt_coeff.v" `` 系数包含机制，以及 [`gen_coeffs.m`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/octave/gen_coeffs.m) 如何把 Octave 设计出的浮点系数量化、写成 Verilog 系数文件；
- 在对称系数（`gp_symm`）模式下，解释 `c_coeff` 数组长度为何只取一半。

本讲是单元 3（FIR 滤波器）的起点，承接 [u2-l1（定点数与位宽推导）](u2-l1-fixed-point-bitwidth.md) 与 [u2-l2（dff 原语）](u2-l2-dff-primitive.md)。下一讲 [u3-l2](u3-l2-fir-tf-df-topology.md) 会深入对比转置型/直接型两种拓扑；本讲先把「结构、参数、系数生成」三件事讲透。

---

## 2. 前置知识

在进入源码前，先用三段话补齐背景。已经熟悉的读者可以跳到第 3 节。

### 2.1 什么是滤波、什么是卷积

滤波的本质是「按一定规则，把当前样本和它之前的若干个样本加权求和，得到输出」。设输入序列为 \(x[n]\)，滤波器系数（也叫抽头系数、冲激响应）为 \(h[k]\)，共 \(N\) 个抽头，则输出为离散卷积：

\[
y[n] = \sum_{k=0}^{N-1} h[k]\,x[n-k]
\]

直观地说：\(h[0]\) 乘当前样本 \(x[n]\)，\(h[1]\) 乘上一拍样本 \(x[n-1]\)，…… 一直乘到 \(h[N-1]\,x[n-(N-1)]\)，最后全部加起来。因为系数个数有限，所以叫「有限脉冲响应（FIR）」。

### 2.2 硬件上怎么实现卷积

公式里出现的 \(x[n-k]\) 在硬件里就是「把输入延迟 \(k\) 拍」，这正好对应 [u2-l2](u2-l2-dff-primitive.md) 学过的 `dff` 原语（一个 `dff` 等价于 \(z^{-1}\)）。把一串 `dff` 首尾相连就得到一条延迟线（delay line / 抽头延迟线），每个抽头取出一个延迟样本，乘以对应系数，再用加法树求和。这就是 FIR 的硬件骨架：

```
x[n] --+--> [×h0]--+
       dff         +--> y[n]
       |--x[n-1]-->[×h1]--+
       dff                +-->
       |--x[n-2]-->[×h2]--+
       ...                加法树
```

### 2.3 位宽为什么会增长

承接 [u2-l1](u2-l1-fixed-point-bitwidth.md)：两个补码定点数相乘，位宽是两者之和；\(N\) 个乘积再求和，还要再多 \(\lceil\log_2 N\rceil\) 位以防溢出。这是本讲位宽推导的全部依据。注意：DRL 里用 `$clog2(N)`，它在数学上就是 \(\lceil\log_2 N\rceil\)，与 Octave GRM 里的 `ceil(log2(N))` 完全等价——这正是 RTL 与黄金参考模型能做到「比特真」的根基。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [`.drl_src_code/filt_fir/rtl/filt_fir.v`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v) | FIR 主模块：参数表、端口、位宽 `localparam`、`generate` 卷积核、系数包含 |
| [`.drl_src_code/filt_fir/rtl/dff.v`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/dff.v) | 延迟线的基本单元（详见 [u2-l2](u2-l2-dff-primitive.md)），FIR 内部例化它做抽头 |
| [`.drl_src_code/filt_fir/octave/gen_coeffs.m`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/octave/gen_coeffs.m) | 把 Octave 设计的系数写成 `filt_coeff.v`（本讲核心） |
| [`.drl_src_code/filt_fir/octave/stimuli.m`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/octave/stimuli.m) | GRM：设计系数、量化、调用 `gen_coeffs`、生成激励与黄金响应 |
| [`.drl_src_code/filt_fir/rtl/filt_coeff.v`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_coeff.v) | **构建产物**，不在 git 里。由 `gen_coeffs.m` 生成，被 `` `include `` 进主模块 |

> 提醒（来自 [u1-l2](u1-l2-repository-structure.md)）：`filt_coeff.v` 是构建时才生成的文件，仓库里看不到它，需要先跑一次 `-d`/`-demo` 流程（见 [u1-l3](u1-l3-toolchain-and-build-flow.md)）。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **FIR 卷积结构**——卷积公式如何变成「延迟线 + 乘法器 + 加法树」，以及 `filt_fir` 用 `generate` 怎么把它铺出来。
2. **filt_fir 参数与自动位宽推导**——参数表、端口、`localparam` 的逐行含义。
3. **filt_coeff.v 系数生成机制**——`` `include `` 钩子、对称取半、`gen_coeffs.m` 的量化写盘。

---

### 4.1 FIR 卷积结构

#### 4.1.1 概念说明

把卷积公式 \(y[n]=\sum_{k=0}^{N-1}h[k]\,x[n-k]\) 落到硬件，有三件事必须明确：

- **延迟线**：产生 \(x[n], x[n-1], \dots, x[n-(N-1)]\) 这一串延迟样本。每个 \(z^{-1}\) 用一个 `dff` 实现。
- **乘法器**：每个抽头一个乘法器，算 \(h[k]\times x[n-k]\)。DRL 里乘法器数量恒等于抽头数 `gp_coeff_length`（注意：`filt_fir` 的对称模式只省系数存储，**不**省乘法器；乘法器减半是 [u3-l4 filt_mac](u3-l4-filt-mac.md) 的事）。
- **加法树**：把 \(N\) 个乘积加起来。

FIR 有两种经典排布：直接型（Direct Form, DF）和转置型（Transposed Form, TF）。`filt_fir` 用一个参数 `gp_tf_df` 在二者之间切换（`1`→TF，`0`→DF）。本讲只让你建立「它们都在算同一个卷积」的直觉，深度对比留给 [u3-l2](u3-l2-fir-tf-df-topology.md)。

#### 4.1.2 核心流程

`filt_fir` 用一个 `generate` 块，按 `gp_tf_df` 选择两条分支，每条分支都用 `for` 循环把 \(N\) 级抽头铺开。伪代码如下：

```
generate
  if (gp_tf_df == 1)  // 转置型
      for i = 0 .. N-1:
          w_mul[i] = x[n] * h[mirror(i)]     // 所有乘法器都吃当前输入 x[n]
          w_add[i] = w_mul[i] + r_dly[i+1]   // 加法链，中间插寄存器
          若 i>0: 用一个 dff 把 w_add[i] 寄存成 r_dly[i]
      o_data = w_add[0]                       // 链头输出
  else               // 直接型
      for i = 0 .. N-1:
          r_dly[i] = x[n-i]                   // 先建延迟线（dff 串）
          w_mul[i] = r_dly[i] * h[mirror(i)]  // 每个抽头乘各自系数
      w_add 用组合加法树把 w_mul[0..N-1] 加起来
      o_data = w_add 的最高段
endgenerate
```

两种排布算的是同一个 \(\sum h[k]x[n-k]\)，区别在于**寄存器插在哪里**：TF 把寄存器插在加法链中间，DF 把寄存器插在输入延迟线上。这正是下一讲要细讲的「关键路径」差异。

#### 4.1.3 源码精读

整个卷积核包在一个 `generate` 里，先用 `if (gp_tf_df)` 选 TF 分支（[filt_fir.v:L34-L81](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L34-L81)），否则走 DF 分支（[filt_fir.v:L85-L141](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L85-L141)）。

**转置型乘法**（[filt_fir.v:L47](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L47)）：注意所有 TF 乘法器吃的都是**当前输入** `i_data`，而不是延迟后的样本——延迟被搬到了加法链的寄存器里：

```verilog
assign w_mul[(i+1)*c_mul_oup_width-1 -: c_mul_oup_width] = $signed(i_data) * c_coeff[i];
```

**转置型加法 + 中间寄存器**（[filt_fir.v:L59-L79](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L59-L79)）：最后一级只乘不加，其余每级都是「本抽头乘积 + 下一级寄存过来的部分和」，并用一个 `dff` 把本级和寄存起来。这就是「寄存器在加法链中间」的体现：

```verilog
// 加：本抽头 + 上一级寄存下来的部分和
assign w_add[...] = $signed(w_mul[...]) + $signed(r_dly_tf[...]);
// 寄存：用 dff 把 w_add 存成 r_dly_tf，供上一级使用
dff #(.gp_data_width(c_add_oup_width)) FIR_TF_DFF (...);
```

**直接型延迟线**（[filt_fir.v:L122-L140](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L122-L140)）：DF 把 `i_data` 串过一排 `dff`，得到 \(x[n-1], x[n-2], \dots\)。第一级直接接 `i_data`，之后每级都是一个 `dff`：

```verilog
if (i==0) assign r_dly_df[...] = i_data;                 // 第 0 个抽头 = 当前输入
else      dff #(.gp_data_width(gp_inp_width)) FIR_TF_DFF (/* 串成延迟线 */);
```

**直接型乘加**（[filt_fir.v:L88-L120](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L88-L120)）：每个抽头取延迟线对应段、乘以系数，再用组合加法树累加。

**输出选择**（[filt_fir.v:L144-L149](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L144-L149)）：TF 取加法链链头 `w_add[0]`，DF 取加法树最高段，两者都输出 `gp_oup_width` 位。

> 关于打包位宽的小技巧：`w_mul`、`w_add`、`r_dly_*` 都是用「一整条宽线 + 切片」来模拟数组的（例如 `w_mul[(i+1)*W-1 -: W]` 取第 `i` 段）。这是 Verilog-2001 没有「线网数组」时的常见写法，看到 `[...] -: W` 就理解成「数组下标 `i`」即可。

#### 4.1.4 代码实践

**实践目标**：用纸笔追踪一个 3 抽头 FIR 的卷积过程，确认 RTL 算的确实就是离散卷积。

**操作步骤**：

1. 设系数 \(h=[2,\,-1,\,3]\)（即 `gp_coeff_length=3`），输入序列 \(x=[1,\,4,\,0,\,2,\dots]\)。
2. 按 \(y[n]=\sum_{k=0}^{2}h[k]\,x[n-k]\) 逐拍手算：
   - \(y[0]=2\cdot1=2\)
   - \(y[1]=2\cdot4+(-1)\cdot1=7\)
   - \(y[2]=2\cdot0+(-1)\cdot4+3\cdot1=-1\)
   - \(y[3]=2\cdot2+(-1)\cdot0+3\cdot4=16\)
3. 对照 [u2-l2](u2-l2-dff-primitive.md) 的 `dff` 行为，画一条 2 级延迟线（DF 的 `r_dly_df`），标出每个 `dff` 在每个时钟沿之后保存的值，验证它确实给出 \(x[n-1]\)、\(x[n-2]\)。

**需要观察的现象**：延迟线里的值正好是「输入序列整体右移一拍/两拍」；乘加后得到的正是上面手算的 \(y[n]\)。

**预期结果**：\(y=[2,\,7,\,-1,\,16,\dots]\)。如果对不上，多半是延迟方向（谁是谁的 \(z^{-1}\)）或系数下标搞反了。

**待本地验证**：若想看真实波形，可在第 5 节综合实践里用 `./dsp_rtl_lib.sh` 跑完整回归，观察 `.vcd` 里的 `r_dly_df`。

#### 4.1.5 小练习与答案

**练习 1**：把 `filt_fir` 配成 TF（`gp_tf_df=1`），它的乘法器吃的信号是 `i_data` 还是 `r_dly_tf`？为什么？

> **答案**：吃 `i_data`（[filt_fir.v:L47](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L47)）。因为转置型把延迟从「输入端」搬到了「加法链」里——所有乘法器同时算 \(h[k]\cdot x[n]\)，而 \(x[n-k]\) 的延迟由加法链中间的 `dff` 依次提供。这正是 TF 与 DF 的根本区别。

**练习 2**：`filt_fir` 里一共有多少个乘法器？对称模式（`gp_symm=1`）会减少吗？

> **答案**：恒为 `gp_coeff_length` 个，与 `gp_symm` 无关。`gp_symm` 只是把系数数组 `c_coeff` 的长度减半（见 4.3 节），RTL 仍铺满 \(N\) 个乘法器，靠镜像下标复用存储的系数。真正用「预加法器」把乘法器减半的是 [filt_mac](u3-l4-filt-mac.md)。

---

### 4.2 filt_fir 参数与自动位宽推导

#### 4.2.1 概念说明

一个「可参数化」的 RTL 模块，核心就是把设计者会调的东西暴露成 `parameter`，把「能由参数算出来的东西」写成 `localparam`。`filt_fir` 正是这套范式的典型：

- 设计者只需关心：**输入位宽、抽头数、系数位宽、拓扑选择、是否对称**。
- 模块自己推导：**乘法位宽、累加位宽、输出位宽、系数数组长度**。

这意味着你换一组抽头数，所有内部位宽会自动跟着变，不用手改连线——这就是 DRL「可参数化」带来的便利。

#### 4.2.2 核心流程

位宽推导的链路完全遵循 [u2-l1](u2-l1-fixed-point-bitwidth.md) 的两条规律：

1. 乘法位宽 = 被乘数位宽 + 乘数位宽；
2. \(N\) 项求和再多 \(\lceil\log_2 N\rceil\) 位。

落到 `filt_fir`：

\[
\texttt{c\_mul\_oup\_width} = \text{输入位宽} + \text{系数位宽}
\]

\[
\texttt{c\_add\_oup\_width} = \texttt{c\_mul\_oup\_width} + \lceil\log_2(\text{抽头数})\rceil
\]

\[
\texttt{gp\_oup\_width} = \text{输入位宽} + \text{系数位宽} + \lceil\log_2(\text{抽头数})\rceil
\]

注意 `gp_oup_width` 被写成 `parameter` 的默认值表达式（而不是 `localparam`），这样顶层（测试台）可以在例化时把它留空、由默认表达式自动算出，也可以在外部强制覆盖——这是 DRL 处理「派生参数」的惯用法（回顾 [u1-l3](u1-l3-toolchain-and-build-flow.md) 提到的派生参数免遭 `sed` 误伤）。

#### 4.2.3 源码精读

**参数表与端口**（[filt_fir.v:L6-L19](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L6-L19)）：注意端口顺序与 [u1-l4](u1-l4-coding-style-and-interface.md)、[u2-l2](u2-l2-dff-primitive.md) 完全一致（`i_rst_an, i_ena, i_clk` 在前），数据口带 `signed`：

```verilog
module filt_fir #(
  parameter gp_inp_width    = 8,
  parameter gp_coeff_length = 17,
  parameter gp_coeff_width  = 8,
  parameter gp_tf_df        = 1,   // 1-> TF, 0-> DF
  parameter gp_symm         = 1,
  parameter gp_oup_width    = gp_inp_width+gp_coeff_width+$clog2(gp_coeff_length)
) (
  input  wire signed [gp_inp_width-1:0] i_data,
  output wire signed [gp_oup_width-1:0] o_data
  ... // i_rst_an, i_ena, i_clk
);
```

**位宽 `localparam`**（[filt_fir.v:L21-L24](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L21-L24)）：这就是上面三条公式的直接落地：

```verilog
localparam c_mul_oup_width = gp_inp_width   + gp_coeff_width;
localparam c_add_oup_width = c_mul_oup_width + $clog2(gp_coeff_length);
localparam c_filler_length = c_add_oup_width - c_mul_oup_width;
localparam c_coeff_2       = (gp_symm) ? `DIV(gp_coeff_length, 2) : gp_coeff_length;
```

其中 `` `DIV `` 是文件开头定义的向上取整宏（[filt_fir.v:L4](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L4)），即 `ceil(N/D)`：

```verilog
`define DIV(N, D) (N%D==0) ? (N/D) : (N/D+1)
```

**内部打包线与系数数组声明**（[filt_fir.v:L26-L28](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L26-L28)）：`w_mul`/`w_add` 是「宽线 + 切片」式数组，`c_coeff` 是真正的线网数组，长度由 `c_coeff_2` 决定：

```verilog
wire signed [gp_coeff_length*c_mul_oup_width-1:0] w_mul;
wire signed [gp_coeff_length*c_add_oup_width-1:0] w_add;
wire signed [gp_coeff_width-1:0]                  c_coeff [0:c_coeff_2-1];
```

**测试台如何接收派生位宽**：在 [`filt_fir_tb.sv`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/sim/testbench/filt_fir_tb.sv#L104-L117) 里，DUT 例化时 `gp_oup_width` 留空（[L110](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/sim/testbench/filt_fir_tb.sv#L110) `.gp_oup_width()`），让它用默认表达式自动算；而对应的宏 `P_OUP_DATA_W` 由 [`gen_defines.m`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/octave/gen_defines.m#L10) 用同一个公式 `p_data_width+p_coeff_width+ceil(log2(p_coeff_length))` 算出来注入——RTL 与 GRM 用同一个公式，是比特真的前提。

#### 4.2.4 代码实践

**实践目标**：手算一组参数下的所有位宽，并理解派生参数如何被「自动算出」。

**操作步骤**：

1. 取 `gp_inp_width=8`、`gp_coeff_width=12`、`gp_coeff_length=17`。
2. 手算：
   - `c_mul_oup_width = 8 + 12 = 20`
   - `$clog2(17)`：因为 \(2^4=16 < 17 \le 32=2^5\)，所以 `= 5`
   - `c_add_oup_width = 20 + 5 = 25`
   - `gp_oup_width = 8 + 12 + 5 = 25`
   - `c_coeff_2`（对称）`= DIV(17,2) = 9`
3. 打开 [`gen_defines.m:L10`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/octave/gen_defines.m#L10)，确认它写的 `ceil(log2(p_coeff_length))` 与 `$clog2` 在 `p_coeff_length=17` 时都得 5。

**需要观察的现象**：累加位宽比乘法位宽正好多了 \(\lceil\log_2 N\rceil\) 位；这多出来的几位就是为了容纳 \(N\) 个乘积相加可能产生的进位/增长。

**预期结果**：`c_mul_oup_width=20`、`c_add_oup_width=25`、`gp_oup_width=25`、`c_coeff_2=9`。

**待本地验证**：若装了 Icarus Verilog，可写一个只 `elaborate` 不仿真的小顶层，用 `$display` 打印这些 `localparam` 对照（Icarus 支持 `iverilog -g2012` 后用 `initial $display(...)` 在零时刻打印）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `gp_oup_width` 写成 `parameter` 而不是 `localparam`？

> **答案**：因为它是「派生参数」，默认值由表达式给出，但又允许顶层在需要时覆盖（例如测试台 [L110](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/sim/testbench/filt_fir_tb.sv#L110) 就把它留空、走默认表达式）。`localparam` 不允许外部覆盖，会失去这个灵活性。

**练习 2**：把抽头数从 17 改成 16，`c_add_oup_width` 会变吗？

> **答案**：不变。`$clog2(16)=4`、`$clog2(17)=5`……等等——其实**会变**：`$clog2(16)=4`，所以 16 抽头时 `c_add_oup_width = 20+4 = 24`，比 17 抽头的 25 少 1 位。这是一个常被忽略的细节：\(N\) 是 2 的幂时，\(\lceil\log_2 N\rceil=\log_2 N\) 刚好不「多 borrow」一位。

---

### 4.3 filt_coeff.v 系数生成机制

#### 4.3.1 概念说明

FIR 的频率响应完全由系数 \(h[k]\) 决定。设计系数是 DSP 算法的事（在 Octave/MATLAB 里做），RTL 只负责「按给定系数做卷积」。于是 DRL 把两者解耦：

- **Octave 侧**：用滤波器设计函数（如 `remez`/`fir1`）算出浮点系数 → 量化成定点整数 → 写成 `filt_coeff.v`。
- **RTL 侧**：主模块用 `` `include "filt_coeff.v" `` 把系数「钩」进来，完全不关心系数是怎么设计出来的。

这样换一组频率指标时，只重新生成 `filt_coeff.v` 即可，RTL 一行都不用动。同时，因为系数可能为负、且要写成固定位宽的补码，`gen_coeffs.m` 还要处理「负号 + 指定位宽的 `sd` 字面量」格式。

#### 4.3.2 核心流程

`gen_coeffs(b, q, symm)` 的三步：

1. **决定写多少个系数**：若 `symm==1`，只写 \(\lceil N/2\rceil\) 个（对称，后一半可由镜像下标复用）；否则写满 \(N\) 个。
2. **逐个写 `assign` 语句**：对每个系数 `b(i)`，按正负分别格式化成
   - 正：`assign c_coeff[k] =  q'sd值;`
   - 负：`assign c_coeff[k] = -q'sd|值|;`
3. **落盘**到 `filt_coeff.v`，构建脚本随后把它移到 `rtl/` 目录供 `` `include ``。

与之配套，`stimuli.m` 在调用 `gen_coeffs` 之前，先做「设计 + 量化 + 缩放」：

```
b = remez(...)                              // 设计浮点系数
q_b = quantize(b, p_coeff_width, ...)       // 量化到 [-1,1) 定点
b  = round((2^(p_coeff_width-1)-1) * q_b)   // 缩放成 q 位有符号整数
... gen_coeffs(b, p_coeff_width, p_symm)    // 写成 filt_coeff.v
```

#### 4.3.3 源码精读

**`include` 钩子**（[filt_fir.v:L32](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L32)）：主模块在声明完 `c_coeff` 数组之后、`generate` 之前，把系数文件包含进来：

```verilog
`include "filt_coeff.v"
```

被包含的 `filt_coeff.v` 内容形如（示意，非仓库现有文件）：

```verilog
assign c_coeff[0] =  8'sd12;
assign c_coeff[1] = -8'sd37;
...
```

**`gen_coeffs.m` 主体**（[gen_coeffs.m:L1-L19](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/octave/gen_coeffs.m#L1-L19)）：

```octave
function gen_coeffs(b, q, symm)
  FID = fopen("filt_coeff.v", "w");
  if (symm==1)
    filt_len = ceil(length(b)/2);   % 只写前一半
  else
    filt_len = length(b);
  end
  for i = 1 : filt_len,
    if b(i) < 0,
      fprintf(FID, "assign c_coeff[%d] = -%d'sd%d;\n", i-1, q, abs(b(i)));
    else
      fprintf(FID, "assign c_coeff[%d] =  %d'sd%d;\n", i-1, q, b(i));
    end
  end
  fclose(FID);
```

要点：

- `q` 就是 `gp_coeff_width`，写成 `q'sd`（指定位宽的有符号十进制）字面量，保证补码位宽与 RTL 声明一致。
- 负数特意拆成 `-q'sd|值|`，这样无论综合工具还是仿真器都能正确识别成有符号负数（直接写 `-37` 进 `sd` 在某些工具上行为不直观）。
- 下标写成 `i-1`，因为 Octave 数组从 1 开始，而 Verilog `c_coeff` 从 0 开始。

**镜像下标如何复用前一半系数**（[filt_fir.v:L43-L53](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L43-L53)，TF 分支；DF 在 [L88-L100](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L88-L100)）：

```verilog
if (i < c_coeff_2)                       // 前一半：直接用 c_coeff[i]
  ... = $signed(i_data) * c_coeff[i];
else                                     // 后一半：用镜像 c_coeff[N-1-i]
  ... = $signed(i_data) * c_coeff[gp_coeff_length-1-i];
```

因为线性相位 FIR 满足 \(h[i]=h[N-1-i]\)，所以抽头 \(i\) 和抽头 \(N-1-i\) 共用同一个存储的系数——这就是「对称取半」的由来。

**`stimuli.m` 里的设计—量化—调用链**（[stimuli.m:L7-L17](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/octave/stimuli.m#L7-L17) 与 [L122](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/octave/stimuli.m#L122)）：注意默认 `p_coeff_length=64, p_coeff_width=13, p_symm=1`，所以 `filt_coeff.v` 会写 \(\lceil 64/2\rceil=32\) 行 `assign`。最后还用 `yy = filter(b,1,data)`（[L125](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/octave/stimuli.m#L125)）算出黄金响应，供测试台逐样本比对。

#### 4.3.4 代码实践

**实践目标**：亲手生成一个 `filt_coeff.v`，验证「对称取半」与负系数格式。

**操作步骤**：

1. 安装 Octave 及 `signal` 包（`pkg install -forge signal` 后 `pkg load signal`）。`fir1`/`remez` 都在该包里。
2. 在 `.drl_src_code/filt_fir/octave/` 目录下，编写下面这段**示例代码**（保存为 `my_design.m`）：

   ```octave
   % 示例代码：设计 17 抽头低通 FIR 并生成 filt_coeff.v
   pkg load signal
   N   = 17;          % 抽头数 = gp_coeff_length
   q   = 8;           % 系数位宽 = gp_coeff_width
   b   = fir1(N-1, 0.4);              % 归一化截止 0.4 的低通
   b   = round((2^(q-1)-1) * b / max(abs(b)));  % 缩放到 q 位有符号整数
   gen_coeffs(b, q, 1);               % symm=1 → 只写 ceil(17/2)=9 行
   ```
3. 运行 `octave my_design.m`，打开生成的 `filt_coeff.v`。

**需要观察的现象**：

- 文件恰好 **9 行** `assign`（= \(\lceil17/2\rceil\)），而不是 17 行——这就是对称取半。
- 负系数被写成 `-8'sd...`，正系数写成 `8'sd...`。
- 第 9 个系数（下标 8）是中间抽头，它自成一个镜像（\(N-1-8=8\)），不会被任何「后一半」抽头额外引用。

**预期结果**：`filt_coeff.v` 含 9 行，下标 `c_coeff[0]` 到 `c_coeff[8]`；`c_coeff[8]` 是中间抽头，通常绝对值最大。

**待本地验证**：`fir1` 输出的具体数值取决于 Octave/signal 版本，故不给出确定数值；重点是**行数 = 9** 与**负号格式**这两点。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `gp_symm=1` 时 `c_coeff` 数组长度是 \(\lceil N/2\rceil\) 而不是 \(N/2\)？以 \(N=17\) 为例说明。

> **答案**：因为奇数长度 FIR 有一个「正中间」抽头，它是自己的镜像（\(h[8]=h[17-1-8]=h[8]\)），必须单独存一份。\(N=17\) 时 \(\lceil17/2\rceil=9\)，即下标 `0..8`；其中下标 8 就是那个自镜像的中间抽头。若用 \(N/2=8\)（向下取整）就会丢掉中间抽头。`gen_coeffs.m` 的 `ceil(length(b)/2)`（[L6](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/octave/gen_coeffs.m#L6)）与 RTL 的 `` `DIV(N,2) ``（[L24](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L24)）都正确地向上取整。

**练习 2**：如果不改 `filt_fir.v`，能否让它跑一个非对称（`gp_symm=0`）滤波器？需要做什么？

> **答案**：能。把 `gp_symm` 设为 0（在 `.param` 里或测试台例化时），`c_coeff_2` 就等于 `gp_coeff_length`，`gen_coeffs` 也必须用 `symm=0` 调用以写满 \(N\) 个系数。RTL 与 GRM 两侧的 `symm` 必须一致，否则系数数组长度对不上会报错。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「从系数设计到比特真验证」的迷你流程。

**任务**：让 `filt_fir` 跑一个你自己设计的 17 抽头对称低通滤波器。

**步骤**：

1. **设计系数**：按 4.3.4 的示例代码，用 `fir1` 设计 17 抽头低通，`gen_coeffs(b, 8, 1)` 生成 `filt_coeff.v`，确认是 9 行。
2. **核对位宽**：用 4.2.4 的方法手算 `gp_inp_width=7, gp_coeff_width=8, gp_coeff_length=17` 时的 `c_mul_oup_width`、`c_add_oup_width`、`gp_oup_width`（注意 7+8=15，`$clog2(17)=5`，故输出 20 位）。
3. **配置参数**：参考 [u1-l3](u1-l3-toolchain-and-build-flow.md)，编辑 `filt_fir` 的 `.param`（或在 `stimuli.m` 顶部把 `p_coeff_length=17, p_coeff_width=8, p_data_width=7, p_symm=1`），让 RTL 与 GRM 用同一组参数。
4. **跑回归**：执行 `./dsp_rtl_lib.sh -d -design filt_fir`（或对照 `-demo` 的用法），观察 9 个测试用例的 `PASSED/FAILED`。
5. **追踪一拍**：在脉冲测试用例（`stimuli.m` 的 case 1）下，对照 4.1.4 的手算方法，验证输出 \(y[n]\) 正好是系数序列本身（因为脉冲响应 = 系数）。

**验收标准**：9 个用例全部 `PASSED`（`error_count==0`）；脉冲用例的输出前 17 个样本等于你设计的 \(h[k]\)。若脉冲用例对不上，多半是 `symm` 两侧不一致或系数缩放比例错了。

> **待本地验证**：本实践依赖 iverilog + octave 环境，具体 `PASSED` 数量与波形需在你的机器上确认。命令与参数名以仓库当前的 `dsp_rtl_lib.sh` 与 `stimuli.m` 为准。

---

## 6. 本讲小结

- FIR 的本质是离散卷积 \(y[n]=\sum h[k]\,x[n-k]\)，硬件上由「延迟线（`dff` 串）+ 乘法器 + 加法树」实现。
- `filt_fir` 用一个 `generate` 块按 `gp_tf_df` 在转置型/直接型之间切换；两者算同一个卷积，区别在于寄存器插在加法链还是输入延迟线（细节留给 [u3-l2](u3-l2-fir-tf-df-topology.md)）。
- 位宽由参数自动推导：`c_mul_oup_width = 输入位宽+系数位宽`，`c_add_oup_width` 再加 `$clog2(抽头数)`，`gp_oup_width` 写成 `parameter` 默认表达式，允许顶层覆盖或留空自动算。
- `gp_symm` 只把系数数组 `c_coeff` 减半（省存储），RTL 通过镜像下标 `gp_coeff_length-1-i` 复用前一半系数；乘法器数量不变。
- 系数与 RTL 解耦：Octave 的 `gen_coeffs.m` 把量化后的整数系数写成 `filt_coeff.v`，主模块用 `` `include `` 钩进来；负数特意写成 `-q'sd|值|`。
- RTL 的 `$clog2` 与 GRM 的 `ceil(log2)` 用同一个公式，是比特真验证的数学根基。

---

## 7. 下一步学习建议

- **下一讲 [u3-l2](u3-l2-fir-tf-df-topology.md)**：深入对比转置型与直接型——寄存器位置如何决定关键路径与吞吐，为什么 TF 能每拍吞一个样本、DF 却受限于组合加法树深度。
- **[u3-l3](u3-l3-fir-symmetric-coefficients.md)**：对称系数的「预加法器」优化（\(x[n]+x[N-1-n]\) 再乘），以及它如何与 [filt_mac](u3-l4-filt-mac.md) 联系起来真正减少乘法器。
- **延伸阅读**：对照 [`.drl_src_code/filt_mac/octave/gen_coeffs.m`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/octave/gen_coeffs.m)，看资源共享型 FIR 是否复用同一套系数生成脚本；以及 [u7-l1](u7-l1-bittrue-verification.md) 里测试台如何用 `$fscanf` + `s_clk` 完成逐样本比对。
