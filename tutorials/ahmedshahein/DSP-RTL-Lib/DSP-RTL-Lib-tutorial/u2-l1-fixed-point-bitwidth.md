# Verilog 定点数与位宽推导

## 1. 本讲目标

DSP-RTL-Lib（DRL）的每个模块几乎都在做同一件事：**把若干个定点数相乘、再累加起来**。乘法和累加会让结果的位数不断“长长”，如果位宽算错一比特，整个验证就会失败。因此本讲专门讲清楚一件事——**位宽是如何被自动推导出来的**。

学完本讲你应该能够：

1. 用二进制补码表示有符号定点数，并理解**符号扩展（sign extension）**为什么能保持数值不变。
2. 说出**乘法**与**累加（求和）**各自让位宽增长多少比特，并能手算增长量。
3. 理解 Verilog 的 `$signed`、`$clog2`、`{fill{sign}}` 三个工具在全库位宽推导中各自扮演的角色。
4. 读懂 FIR、CIC、多相滤波器里那一排 `localparam` 是如何把“输入位宽 + 增长量”自动算成输出位宽的。

本讲承接 [u1-l4 统一编码风格与接口约定](u1-l4-coding-style-and-interface.md)：那一讲确立了 `gp_/c_/r_/w_` 命名前缀（`c_` 就是“派生常量”，正是本讲的 `localparam`）和“算术基于补码定点数”这一条约定。本讲把这条约定展开成具体的位宽公式。

## 2. 前置知识

在进入源码前，先用三段白话把基础概念补齐。

**（1）补码（two's complement）。** 一个 N 位的有符号整数，最高位（MSB）是符号位，权重是负的；其余位权重是正的。它的取值范围是：

\[
[-2^{N-1},\; 2^{N-1}-1]
\]

例如 4 位补码能表示 \([-8, 7]\)。负数用“按位取反再加 1”得到，所以 `-1` 在 4 位里是 `4'b1111`。

**（2）定点数（fixed-point）。** DRL 里所有数据都是整数补码，没有硬件浮点。所谓“小数”是靠**人为约定小数点位置**来表达的：同样一串比特，你可以约定它有 3 位小数，也可以约定它有 0 位小数。本讲只关心**整数位宽如何增长**，小数点对齐属于各模块自己的设计约定（比如系数和输入都约定几位小数），不影响“位宽推导”这套机制。

**（3）位宽增长（bit growth）。** 这是本讲的核心直觉：

- 两个 N 位数相加，结果最坏需要 N+1 位（进位）。
- 一个 A 位数乘一个 B 位数，结果最坏需要 A+B 位。
- 把 K 个 W 位数加起来，结果最坏需要 \(W + \lceil \log_2 K \rceil\) 位。

DRL 的所有 `localparam`，本质都是在套用上面这三条规律。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [.drl_src_code/filt_fir/rtl/filt_fir.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v) | FIR 滤波器主模块 | 乘法位宽、累加位宽、输出位宽的三层推导 |
| [.drl_src_code/filt_cicd/rtl/filt_cicd.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v) | CIC 抽取滤波器 | 输入符号扩展 `{fill{sign}}`、积分器位宽 |
| [.drl_src_code/filt_ppd/rtl/mul_add.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/mul_add.v) | 多相滤波器的乘加引擎 | 两级累加的位宽增长 |
| [.drl_src_code/filt_fir/octave/gen_defines.m](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/octave/gen_defines.m) | GRM 侧生成参数宏的脚本 | 证明 Octave 的 `ceil(log2())` 与 Verilog 的 `$clog2()` 完全等价 |
| [.drl_src_code/filt_cicd/rtl/dff.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/dff.v) | 基础寄存器原语 | 说明 `signed` 关键字在端口层 vs 切片层的区别 |

> 提醒：`dff.v` 的数据端口声明为 `input wire [gp_data_width-1:0] i_data`（**未带 `signed`**）。也就是说，符号信息不是寄存器自带的，而是由父模块用 `wire signed` 声明 + `$signed()` 强制转换来维护的。这是理解本讲代码的一个关键。

## 4. 核心概念与源码讲解

本讲的三个最小模块层层递进：先学会把一个数“变宽”（符号扩展），再学会算“乘和加各长几比特”（位宽增长），最后学会用 `$clog2` 把这些增长量写成一个自动公式。

### 4.1 补码定点与符号扩展

#### 4.1.1 概念说明

**符号扩展（sign extension）** 解决的问题是：把一个 N 位补码数放进更宽的 M 位（\(M>N\)）数据通路里，且**数值不变**。

做法很简单——把原来的符号位（MSB）复制 \(M-N\) 份，填到高位：

\[
\{\,\underbrace{b_{N-1}\,b_{N-1}\,\cdots\,b_{N-1}}_{M-N \text{ 位}},\; b_{N-1}b_{N-2}\cdots b_0\,\}
\]

为什么这样做能保持数值不变？因为补码里 MSB 的权重是负的 \(-2^{N-1}\)，复制 MSB 等价于在更高位补上若干个“负权重项”和“正权重项”相互抵消的组合，数学上可证扩展前后的整数值相等。

直观例子：4 位的 `-1` 是 `4'b1111`，符号扩展到 8 位就是 `8'b1111_1111`，仍然等于 `-1`。而 `4'b0111`（+7）扩展到 8 位是 `8'b0000_0111`，仍然是 +7。

#### 4.1.2 核心流程

在 DRL 里，符号扩展出现在“**输入数据要进入一条更宽的累加通路**”的时刻。典型流程：

1. 模块算出输出通路宽度 `gp_oup_width`（通常比输入 `gp_inp_width` 宽）。
2. 计算需要补多少位：`c_fill_width = gp_oup_width - gp_inp_width`。
3. 在输入前面拼接 `c_fill_width` 个符号位：`{{c_fill_width{i_data[MSB]}}, i_data}`。
4. 之后所有加法器、寄存器都按 `gp_oup_width` 宽度运行，永远不会因为通路变宽而丢失符号。

与此相关的还有 Verilog 的 **`$signed()`** 函数：它把一个位向量**按补码重新解释为有符号数**。需要特别记住一条规则：

> **对一个 `signed` 向量做“位选择/切片（part-select）”，结果会被当成无符号数。** 想对切片做有符号运算，必须再用 `$signed()` 包一层。

这条规则解释了为什么 DRL 代码里到处都是 `$signed(some_slice)`。

#### 4.1.3 源码精读

最典型的符号扩展出现在 CIC 抽取滤波器里。输入只有 `gp_inp_width` 位，但积分器要不断累加，通路必须预先展宽到 `gp_oup_width`：

[filt_cicd.v:22 — 计算需要补的符号位数](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L22)：

```verilog
localparam c_fill_width = gp_oup_width - gp_inp_width;
```

[filt_cicd.v:60 — 把输入符号扩展到输出宽度](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L60)：把 `i_data` 的最高位 `i_data[gp_inp_width-1]`（即符号位）复制 `c_fill_width` 份，拼到 `i_data` 前面。

```verilog
assign w_data = {{c_fill_width{i_data[gp_inp_width-1]}},i_data};
```

这之后，积分器就在全宽通路上做加法。[filt_cicd.v:70 — 第一级积分器](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L70)：注意 `r_int_dly[...]` 是对一个宽向量做切片，所以必须用 `$signed()` 重新声明符号性，否则两个切片相加会被当成无符号运算：

```verilog
assign w_int_add[...] = $signed(w_data) + $signed(r_int_dly[...]);
```

对比看 [dff.v:13](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/dff.v#L13)：寄存器自己的端口是 `input wire [gp_data_width-1:0] i_data`，不带 `signed`。也就是说“这批比特到底是不是有符号数”由调用者决定，寄存器只负责原样搬运比特——这也正是 u1-l4 讲过的“`reg/wire` 关键字表如何被驱动，`r_/w_` 表有无记忆”的体现。

#### 4.1.4 代码实践

**实践目标**：确认符号扩展不改变数值，并看懂 CIC 的展宽量。

**操作步骤**：

1. 取 `gp_inp_width=8`、`gp_decimation_factor=4`、`gp_order=3`、`gp_diff_delay=1`（filt_cicd 的默认参数）。
2. 按 [filt_cicd.v:12](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L12) 的公式手算 `gp_oup_width = 8 + 3*$clog2(4*1)`。
3. 再算 `c_fill_width`，回答：输入 `8'sb10000000`（十进制 -128）经过符号扩展后的 14 位值是多少？

**预期结果**：`gp_oup_width = 8 + 3×2 = 14`；`c_fill_width = 14 - 8 = 6`；`-128` 扩展为 `14'b11_1111_10000000`，十进制仍是 `-128`。

> 待本地验证：以上手算结果可用任意 Verilog 仿真器打印 `{{6{1'b1}}, 8'sb10000000}` 的 `$signed()` 值核对。本环境未安装仿真器，故标注待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`8'sb00000011`（+3）符号扩展到 12 位，结果是什么？
**答案**：`12'b000000000011`，仍为 +3（高位补 6 个 0）。

**练习 2**：如果把 [filt_cicd.v:60](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L60) 的 `i_data[gp_inp_width-1]` 改成 `1'b0`（即零扩展而不是符号扩展），负数输入会发生什么？
**答案**：负数会被当成很大的正数（例如 `-128` 变成 `128`），后续累加结果全部错误。所以有符号通路必须用符号位填充。

### 4.2 乘加位宽增长

#### 4.2.1 概念说明

符号扩展解决的是“一个数变宽”，而本模块解决的是“运算结果变宽”。两条核心规律：

**乘法规律**：一个 A 位补码数乘一个 B 位补码数，乘积最坏需要 \(A+B\) 位。最极端的例子是两个最负数相乘：\((-2^{A-1})\times(-2^{B-1}) = 2^{A+B-2}\)，这个正数能放进 \(A+B\) 位补码（其正数上限是 \(2^{A+B-1}-1\)，足够容纳）。

**求和规律**：把 K 个 W 位数加起来，结果最坏需要

\[
W + \lceil \log_2 K \rceil
\]

位。直觉是：每把数值数量翻倍，最坏就多 1 比特可能的进位。

这两条规律合起来，就是 FIR 这类“乘了再加”的结构里位宽增长的全部来源。

#### 4.2.2 核心流程

以 FIR 为例，一次完整的“乘加”位宽推导流程：

1. 输入数据宽 `gp_inp_width`，系数宽 `gp_coeff_width`。
2. **每个抽头的乘积**宽度 = `gp_inp_width + gp_coeff_width`（乘法规律）。
3. **把 N 个乘积加起来**，累加器宽度 = 乘积宽 + \(\lceil \log_2 N \rceil\)（求和规律），其中 N = 抽头数 `gp_coeff_length`。
4. 输出宽度就等于这个累加器宽度。

如果是多相滤波器那种“先按列加、再把各列结果加起来”的两级结构，求和规律要套两次：先加 `gp_decimation_factor` 个（多 \(\lceil\log_2(\text{dec})\rceil\) 位），再加 `c_col` 个（再多 \(\lceil\log_2(\text{c\_col})\rceil\) 位）。

#### 4.2.3 源码精读

[filt_fir.v:21-22 — 乘积宽与累加宽](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L21-L22)：第 21 行是乘法规律，第 22 行是求和规律，注释里直接对应上一节的公式。

```verilog
localparam c_mul_oup_width = gp_inp_width   + gp_coeff_width;            // 乘法
localparam c_add_oup_width = c_mul_oup_width + $clog2(gp_coeff_length);  // 求和
```

[filt_fir.v:47 — 一个抽头的乘法](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L47)：`i_data` 虽然端口声明为 `signed`，但这里仍用 `$signed()` 强调有符号乘法，乘积写入宽 `c_mul_oup_width` 的切片。

```verilog
assign w_mul[(i+1)*c_mul_oup_width-1 -: c_mul_oup_width] = $signed(i_data) * c_coeff[i];
```

[filt_fir.v:65 — 转置型里的链式加法](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L65)：把当前乘积与上一级寄存器的值相加。两个切片都用 `$signed()` 包住，因为切片本身是无符号的：

```verilog
assign w_add[...] = $signed(w_mul[...]) + $signed(r_dly_tf[...]);
```

多相引擎 `mul_add` 展示了**两级求和**。先看乘积宽和第一级列内求和宽：

[mul_add.v:27-29 — 两级累加的位宽](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/mul_add.v#L27-L29)：`c_mul_out_width` 是乘积宽；`c_add_out_width` 在它基础上加了 \($clog2(\text{dec})\)（一列里 dec 个乘积相加）；`c_sum_out_width` 再加 \($clog2(\text{c\_col})\)（c_col 个列结果相加）。

```verilog
localparam c_mul_out_width = gp_idata_width + gp_coeff_width;
localparam c_add_out_width = $clog2(gp_decimation_factor) + c_mul_out_width;
localparam c_sum_out_width = c_add_out_width      + $clog2(c_col);
```

[mul_add.v:123 — 加法树里的一次相加](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/mul_add.v#L123)：把两个乘积切片相加，结果宽是 `c_add_out_width`，同样靠 `$signed()` 维持符号。

#### 4.2.4 代码实践

**实践目标**：手算一次“乘 + 单级求和”的位宽。

**操作步骤**：取 `gp_inp_width=8`、`gp_coeff_width=12`，分别算：两个数相乘的乘积宽；把 8 个这样的乘积相加的累加宽（提示：\(\lceil\log_2 8\rceil = 3\)）。

**预期结果**：乘积宽 = 8+12 = 20；8 个求和的累加宽 = 20 + 3 = 23。

> 待本地验证：可写一行 `$display("%0d", 20 + $clog2(8))` 在仿真器里打印核对。

#### 4.2.5 小练习与答案

**练习 1**：为什么两个 N 位补码数相乘，结果只需 2N 位而不是 2N+1 位？
**答案**：两个最负数相乘得到 \(2^{2N-2}\)，是正数且不超过 \(2N\) 位补码的正上限 \(2^{2N-1}-1\)，故 2N 位足够，不需要额外一位。

**练习 2**：把 17 个 20 位数相加，累加器至少要多宽？比单独的 20 位多了几位？
**答案**：宽 \(20 + \lceil\log_2 17\rceil = 20 + 5 = 25\) 位；比 20 位多 5 位。

### 4.3 `$clog2` 自动位宽推导

#### 4.3.1 概念说明

前两节的公式里反复出现 \(\lceil\log_2(\cdot)\rceil\)，在 Verilog 里它对应系统函数 **`$clog2(x)`**，返回大于等于 \(x\) 的最小 2 的幂的指数，也就是 \(\lceil\log_2 x\rceil\)。它的物理意义是“表示/累加 x 个东西所需的额外比特数”。

几个常用值（务必记住）：

| \(x\) | 1 | 2 | 3 | 4 | 5–8 | 9–16 | 17–32 | 33–64 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `$clog2(x)` | 0 | 1 | 2 | 2 | 3 | 4 | 5 | 6 |

DRL 把 `$clog2` 用在两个地方：一是**算求和的位宽增长**（前节已见），二是把它写进**派生参数的默认值**里，让输出位宽随输入参数自动算出来——这就是“自动位宽推导”。

#### 4.3.2 核心流程

全库统一的“位宽自动推导”套路是：

1. 在模块参数表里，给 `gp_oup_width` 一个**由表达式构成的默认值**（而不是一个写死的数字）。
2. 这个表达式 = `输入位宽 + 增长项`，增长项由 `$clog2` 算出。
3. 因为它是默认值，所以用户不传 `gp_oup_width` 时自动算；用户也可以显式覆盖。
4. 内部再用 `localparam` 把“乘积宽”“累加宽”等中间量逐一派生出来，供切片宽度引用。

与此同时，Octave 侧的黄金参考模型（GRM）用 `ceil(log2(...))` 写出**完全相同**的表达式。两边公式一致，是保证 RTL 与 GRM “逐比特一致（bit-true）”的前提。

#### 4.3.3 源码精读

三个模块的输出位宽公式，规律一目了然：

[filt_fir.v:12 — FIR 输出宽](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L12)：输入宽 + 系数宽 + 抽头数带来的求和增长。

```verilog
parameter gp_oup_width = gp_inp_width+gp_coeff_width+$clog2(gp_coeff_length)
```

[filt_cicd.v:12 — CIC 抽取输出宽（Hogenauer 最大位增长）](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L12)：每一级积分器都会因为抽取比 \(R=\text{dec}\times\text{diff\_delay}\) 带来 \(\lceil\log_2 R\rceil\) 位增益，共 `gp_order` 级，所以增长项乘以 `gp_order`。

```verilog
parameter gp_oup_width = gp_inp_width + gp_order*$clog2(gp_decimation_factor*gp_diff_delay)
```

[mul_add.v:13 — 多相乘加输出宽](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/mul_add.v#L13)：两级求和，所以有两个 `$clog2` 项（第二项里的 `DIV` 宏是向上取整的除法，对应“列数” `c_col`）。

```verilog
parameter gp_odata_width = gp_idata_width+gp_coeff_width+$clog2(gp_decimation_factor)+$clog2(`DIV(gp_coeff_length,gp_decimation_factor))
```

[filt_fir.v:23 — “要补几位符号位”也是派生量](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L23) 与 [mul_add.v:31](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/mul_add.v#L31)：把“累加宽 − 乘积宽”算成填充位数，本质和 4.1 的 `c_fill_width` 同源。

```verilog
localparam c_filler_length = c_add_oup_width - c_mul_oup_width;   // filt_fir
localparam c_msb_filler_width = c_sum_out_width - c_add_out_width; // mul_add
```

**最关键的一处对照**在 GRM 侧。[gen_defines.m:10](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/octave/gen_defines.m#L10) 用 Octave 的 `ceil(log2(...))` 生成输出位宽宏，和 [filt_fir.v:12](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L12) 的 `$clog2(...)` 一模一样：

```matlab
fprintf(fid, "`define P_OUP_DATA_W \t%d\n",defines.p_data_width+defines.p_coeff_width+ceil(log2(defines.p_coeff_length)));
```

这就是“RTL 用 `$clog2`、GRM 用 `ceil(log2)`，两者天然等价”的实证——位宽推导不是 RTL 一边的事，而是 RTL 与 GRM 共同遵守的契约。

#### 4.3.4 代码实践

**实践目标**：填出 `$clog2` 速查表，并确认 Octave/Verilog 两边公式等价。

**操作步骤**：

1. 不查表，凭“找最小 \(n\) 使 \(2^n \ge x\)”推算 `$clog2(17)` 和 `$clog2(31)`。
2. 写一行 `$display("$clog2(17)=%0d", $clog2(17));` 在任意 Verilog 仿真器里打印核对。
3. 打开 [gen_defines.m:10](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/octave/gen_defines.m#L10)，确认 `ceil(log2(p_coeff_length))` 与 [filt_fir.v:12](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L12) 的 `$clog2(gp_coeff_length)` 是同一项。

**预期结果**：`$clog2(17)=5`（因为 \(2^4=16<17\le32=2^5\)）；`$clog2(31)=5`。

> 待本地验证：`$display` 的输出依赖本地仿真器，本环境未安装，故标注待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`$clog2(1)` 等于多少？为什么 CIC 公式里 `gp_diff_delay=1` 不会让 `$clog2(dec*1)` 变小出错？
**答案**：`$clog2(1)=0`；但 `dec*diff_delay` 至少等于抽取比 `dec`（≥2），所以 `dec*1` 仍 ≥2，`$clog2` 结果 ≥1，不会出错。

**练习 2**：`mul_add` 默认参数 `gp_decimation_factor=31, gp_coeff_length=53`，算 `c_col = DIV(53,31)` 与 `$clog2(c_col)`。
**答案**：`c_col = ceil(53/31) = 2`；`$clog2(2) = 1`。

**练习 3**：为什么 filt_fir 的 `gp_oup_width` 和 `c_add_oup_width` 在数值上总是相等？
**答案**：二者表达式相同——`gp_oup_width = inp+coeff+$clog2(length)`，而 `c_add_oup_width = (inp+coeff)+$clog2(length)`。一个是端口派生参数，一个是内部 localparam，殊途同归。

## 5. 综合实践

把 4.1～4.3 串起来，完成本讲指定的实践任务：**给定 `gp_inp_width=8`、`gp_coeff_width=12`、`gp_coeff_length=17`，手算 filt_fir 的 `c_mul_oup_width`、`c_add_oup_width`、`gp_oup_width`，再用 iverilog 打印这些 localparam 验证。**

### 步骤 1：手算

依据 [filt_fir.v:12](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L12) 与 [filt_fir.v:21-22](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L21-L22)：

- `c_mul_oup_width = 8 + 12 = 20`（乘法规律）
- `$clog2(17) = 5`（因为 \(2^4=16<17\le32=2^5\)）
- `c_add_oup_width = 20 + 5 = 25`（求和规律）
- `gp_oup_width = 8 + 12 + 5 = 25`（与 `c_add_oup_width` 同值，见练习 4.3-3）

### 步骤 2：用 iverilog 验证

`filt_fir` 内部的 `c_mul_oup_width`、`c_add_oup_width` 是 localparam，最稳妥、零依赖的核对方式是写一个**复刻其位宽公式**的最小模块并打印。新建 `tb_bitwidth.sv`（**示例代码**，仅供位宽核对，非项目原有文件）：

```verilog
// tb_bitwidth.sv —— 复刻 filt_fir.v 第 12/21/22 行的位宽推导，仅供位宽核对
module tb_bitwidth;
  localparam gp_inp_width    = 8;
  localparam gp_coeff_width  = 12;
  localparam gp_coeff_length = 17;

  // 复刻 filt_fir.v:21-22
  localparam c_mul_oup_width = gp_inp_width + gp_coeff_width;
  localparam c_add_oup_width = c_mul_oup_width + $clog2(gp_coeff_length);
  // 复刻 filt_fir.v:12
  localparam gp_oup_width    = gp_inp_width + gp_coeff_width + $clog2(gp_coeff_length);

  initial begin
    $display("$clog2(17)      = %0d", $clog2(17));        // 预期 5
    $display("c_mul_oup_width = %0d", c_mul_oup_width);   // 预期 20
    $display("c_add_oup_width = %0d", c_add_oup_width);   // 预期 25
    $display("gp_oup_width    = %0d", gp_oup_width);      // 预期 25
    $finish;
  end
endmodule
```

编译运行（需本地安装 iverilog）：

```bash
iverilog -g2012 -o tb_bitwidth.vvp tb_bitwidth.sv
vvp tb_bitwidth.vvp
```

**预期输出**：

```
$clog2(17)      = 5
c_mul_oup_width = 20
c_add_oup_width = 25
gp_oup_width    = 25
```

> 待本地验证：本环境未安装 iverilog/octave，上述输出为依据公式的预期值，请在本机运行后核对。

### 步骤 3（进阶，可选）：elaborate 真实的 `filt_fir`

如果想直接对 `filt_fir` 实例验证 `gp_oup_width`，需要注意两个依赖（这些在 [u1-l3](u1-l3-toolchain-and-build-flow.md) 与 [u3-l1](u3-l1-fir-structure.md) 详述）：

1. `filt_fir.v` 里有 `` `include "filt_coeff.v" ``，但**该文件不在仓库里**，它是 [gen_coeffs.m](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/octave/gen_coeffs.m) 生成的产物。位宽核对不关心系数具体数值，可临时放一个只含若干 `assign c_coeff[k] = ...;` 的**桩文件**应付 elaborate。
2. 需要把同目录的 `dff.v` 一起编译。

编译命令大致为（用 `+incdir+` 指向含 `filt_coeff.v` 桩文件的目录）：

```bash
iverilog -g2012 -s tb_top +incdir+. tb_top.sv .drl_src_code/filt_fir/rtl/filt_fir.v .drl_src_code/filt_fir/rtl/dff.v
```

在 `tb_top` 里例化 `filt_fir` 并用 `$display("%0d", $bits(o_data))` 即可读到端口侧的 `gp_oup_width`（预期 25）。内部 localparam 若要用层次名 `dut.c_mul_oup_width` 访问，取决于所用 iverilog 版本对 localparam 层次引用的支持，故推荐以步骤 2 的复刻法为主。

**观察要点**：把 `gp_coeff_length` 从 17 改成 16，重跑步骤 2，应看到 `$clog2` 从 5 变为 4、`c_add_oup_width`/`gp_oup_width` 从 25 变为 24——这正是“抽头数降到 2 的幂时，求和增长少 1 位”的直观体现。

## 6. 本讲小结

- **符号扩展**用 `{{fill{sign}}, data}` 把补码数变宽而不改数值；DRL 在输入进入宽累加通路前统一做这件事（如 [filt_cicd.v:60](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L60)）。
- **切片会丢失符号性**，所以代码里到处是 `$signed(slice)`，这是读懂加法/乘法语句的前提。
- **乘法**让位宽增长 \(A+B\)，**求和 K 项**让位宽再增长 \(\lceil\log_2 K\rceil\)；这两条是全库位宽推导的全部基础。
- `$clog2(x)` 就是 \(\lceil\log_2 x\rceil\)，被写进 `gp_oup_width` 的默认表达式，实现**位宽自动推导**。
- **RTL 的 `$clog2` 与 GRM 的 `ceil(log2)` 完全等价**（[gen_defines.m:10](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/octave/gen_defines.m#L10)），这是 RTL/GRM 比特真验证的数学根基。
- 本讲指定参数下：`c_mul_oup_width=20`、`c_add_oup_width=25`、`gp_oup_width=25`。

## 7. 下一步学习建议

- 本讲只讲了“位宽如何算”，还没讲 FIR 的**卷积结构**与系数如何生成。下一讲 [u3-l1 FIR 原理与 filt_fir 接口结构](u3-l1-fir-structure.md) 会把 [filt_fir.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v) 的延迟线、乘加链和 `filt_coeff.v` 的来源讲透，建议紧接着读。
- 想立刻动手跑通一次完整仿真，可先读 [u1-l3 工具链与构建运行流程](u1-l3-toolchain-and-build-flow.md)，用 `./dsp_rtl_lib.sh -demo` 看 CIC 抽取滤波器的比特真回归。
- 对 CIC 的 Hogenauer 位宽公式（`gp_order*$clog2(dec*diff_delay)`）想深入，可在学完 u3 后跳到 [u4-l3 CIC 位宽推导与黄金参考模型](u4-l3-cic-bitwidth-and-grm.md)。
