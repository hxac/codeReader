# 对称系数优化

## 1. 本讲目标

学完本讲，你应当能够：

- 说清**线性相位 FIR 对称系数**的数学含义，以及「预加（pre-add）为什么能把乘法数量减半」的原理。
- 精读 `filt_fir.v` 的 `gp_symm` 分支，看懂 `c_coeff_2` 如何把**系数存储**减半，并理解镜像下标 `gp_coeff_length-1-i` 是如何只靠一半系数重建整条抽头链的。
- 厘清一个关键事实：`filt_fir` 只省了**系数 ROM**，并没有省乘法器；真正用预加法器把乘法器减半的是 `filt_mac`，从而建立两份代码之间的联系。
- 掌握**中间抽头（middle tap）**在滤波器长度为奇数 / 偶数时的不同处理方式。

## 2. 前置知识

本讲承接 [u3-l2 转置型与直接型 FIR 结构](u3-l2-fir-tf-df-topology.md)，默认你已经掌握：

- **FIR 卷积**：\(y[n]=\sum_{k=0}^{N-1} h[k]\,x[n-k]\)，其中 \(h[k]\) 是系数（抽头），\(x[n-k]\) 是延迟线上的样本（参见 [u3-l1](u3-l1-fir-structure.md)）。
- **补码定点与符号扩展**：见 [u2-l1](u2-l1-fixed-point-bitwidth.md)。
- **generate 结构选择**：`filt_fir` 用 `gp_tf_df` 在转置型（TF）与直接型（DF）之间二选一，见 [u3-l2](u3-l2-fir-tf-df-topology.md)。
- **命名前缀**：`gp_` 可覆盖参数、`c_` 派生常量、`r_` 寄存器、`w_` 组合连线，见 [u1-l4](u1-l4-coding-style-and-interface.md)。

一个需要先建立的直觉：很多实用 FIR（尤其是低通滤波器）的系数是**对称**的，即 \(h[k]=h[N-1-k]\)。这种对称既是「线性相位」的来源，也是一块「白送的硬件优化空间」——本讲就是讲如何把这块空间用起来，以及 `filt_fir` 与 `filt_mac` 在「用到什么程度」上的分野。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [.drl_src_code/filt_fir/rtl/filt_fir.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v) | 并行（每抽头一乘法器）FIR。本讲聚焦它的 `gp_symm` 分支：用镜像下标把**系数数组**减半。 |
| [.drl_src_code/filt_mac/rtl/filt_mac.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/rtl/filt_mac.v) | 单乘法器分时复用（MAC）FIR。本讲聚焦它的**预加法器**，这是真正把乘法数量减半的地方。 |
| [.drl_src_code/filt_fir/octave/gen_coeffs.m](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/octave/gen_coeffs.m) | 黄金参考模型（GRM）侧的系数生成器。对称时只写出前一半系数，是 RTL 端「半数组」的对应物。 |

## 4. 核心概念与源码讲解

本讲三个最小模块按「原理 → `filt_fir` 的半数组 → `filt_mac` 的预加法器」递进。

### 4.1 对称系数预加（原理）

#### 4.1.1 概念说明

设滤波器长度为 \(N\)，系数满足**偶对称** \(h[k]=h[N-1-k]\)。这是「第一类（Type I，\(N\) 奇）/ 第二类（Type II，\(N\) 偶）线性相位 FIR」的共同特征。代入卷积公式，把「相加项」\(k\) 与「镜像项」\(N-1-k\) 配对：

- 当 \(N=2M+1\) 为**奇数**（有唯一中间抽头 \(h[M]\)）：

\[
y[n]=\sum_{k=0}^{M-1} h[k]\bigl(x[n-k]+x[n-(N-1-k)]\bigr)\;+\;h[M]\,x[n-M]
\]

- 当 \(N=2M\) 为**偶数**（没有单独的中间抽头）：

\[
y[n]=\sum_{k=0}^{M-1} h[k]\bigl(x[n-k]+x[n-(N-1-k)]\bigr)
\]

关键观察：**配对之后，每一对只需要一次乘法**（先做一次加法 \(x[n-k]+x[n-(N-1-k)]\)，再乘 \(h[k]\)）。于是乘法次数从 \(N\) 降到 \(\lceil N/2\rceil\)。这就是「预加（pre-add）省乘法器」的全部原理——用便宜的加法器换昂贵的乘法器。

> 术语：\(\lceil\cdot\rceil\) 是向上取整。\(\lceil 17/2\rceil=9\)，\(\lceil 16/2\rceil=8\)。

#### 4.1.2 核心流程

把卷积改写成「预加 + 半数乘法」的伪流程：

```
输入：延迟线样本 x[0..N-1]（x[0] 最新，x[N-1] 最旧）
输出：y[n]

若 N 为奇数 (N=2M+1)：
    for k = 0 .. M-1:
        s = x[k] + x[N-1-k]      # 预加，1 个加法器
        y += h[k] * s             # 只乘一次
    y += h[M] * x[M]              # 中间抽头单独处理（无配对）

若 N 为偶数 (N=2M)：
    for k = 0 .. M-1:
        s = x[k] + x[N-1-k]      # 预加
        y += h[k] * s             # 只乘一次
```

无论奇偶，乘法次数都是 \(\lceil N/2\rceil\)；区别仅在于**奇数长度多出一个没有配对的中间抽头**，需要单独乘一次。这个小差别会贯穿后面两份源码。

#### 4.1.3 源码精读

原理本身是数学，但「只存一半系数」这件事在 GRM 侧就已经发生了。看 `gen_coeffs.m`：

[.drl_src_code/filt_fir/octave/gen_coeffs.m:5-9](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/octave/gen_coeffs.m#L5-L9) —— 对称时只写出前一半系数：

```matlab
if (symm==1)
  filt_len = ceil(length(b)/2);   % 只写 ⌈N/2⌉ 个
else
  filt_len = length(b);           % 非对称才写全部
endif
```

也就是说，`symm=1` 时生成的 `filt_coeff.v` **只有 `⌈N/2⌉` 行 `assign c_coeff[k]=...`**。这正好对应 RTL 端「半大小的系数数组」。GRM 与 RTL 在「存几条系数」上完全对齐，是比特真验证的前提。

#### 4.1.4 代码实践

**实践目标**：用纸笔验证「预加减半」对一组真实对称系数成立。

**操作步骤**：

1. 取一个 5 抽头对称滤波器 \(h=[1,2,3,2,1]\)（\(N=5\)，奇数，\(M=2\)）。
2. 直接卷积：\(y=h[0]x[n]+h[1]x[n-1]+h[2]x[n-2]+h[3]x[n-3]+h[4]x[n-4]\)。
3. 按本讲公式改写为预加形式，验证二者相等。

**预期结果**：

直接卷积展开后，利用 \(h[0]=h[4]=1\)、\(h[1]=h[3]=2\)：

\[
y = 1\cdot(x[n]+x[n-4]) + 2\cdot(x[n-1]+x[n-3]) + 3\cdot x[n-2]
\]

乘法从 5 次降到 \(3=\lceil5/2\rceil\) 次（两对 + 一个中间抽头 \(h[2]\)），加法多出 2 次。数值完全相等。这正是后面 `filt_mac` 在硬件里做的事。

#### 4.1.5 小练习与答案

**练习 1**：若 \(N=16\)（偶数，对称），预加后需要几次乘法？有没有「单独处理」的中间抽头？

**答案**：\(\lceil16/2\rceil=8\) 次乘法。偶数长度没有无配对的中间抽头，每一项都能两两配对，不需要单独处理。

**练习 2**：两个 \(B\) 位有符号数相加，结果最少要几 bit 才不溢出？这对预加法器的位宽意味着什么？

**答案**：需要 \(B+1\) bit（最高位可能进位）。所以预加法器的输出必须比延迟线样本宽 1 bit——这一点在 `filt_mac` 的 `w_mul_inp_a` 声明里会直接看到。

---

### 4.2 镜像抽头索引（filt_fir 的系数存储减半）

#### 4.2.1 概念说明

`filt_fir` 是**并行**架构：每个抽头配一个乘法器，吞吐为 1 样本/时钟（见 u3-l2）。它的对称优化做了一件**有限但很巧妙**的事——把**系数数组 `c_coeff` 的大小减半**，而**不是**把乘法器减半。

为什么要强调这个区别？因为这是本讲最容易产生误解的地方：

- `filt_fir` 仍然例化 `gp_coeff_length` 个乘法器（17 抽头就是 17 个乘）。
- 但系数数组只存 `⌈N/2⌉` 个唯一值，第二个半段的乘法器通过**镜像下标**复用前半段的系数。

这省的是**系数 ROM 的存储面积**，不是乘法器面积。真正把乘法器减半的是 4.3 节的 `filt_mac`。

#### 4.2.2 核心流程

`filt_fir` 对称分支的工作流程：

```
c_coeff_2 = ⌈N/2⌉                      # 唯一系数个数
c_coeff[0 .. c_coeff_2-1]              # 只存前一半（由 gen_coeffs.m 喂入）

for i = 0 .. N-1:                       # 仍是 N 个乘法器
    if i < c_coeff_2:
        idx = i                          # 前半段：直接用
    else:
        idx = (N-1) - i                  # 后半段：镜像复用
    w_mul[i] = <data> * c_coeff[idx]
```

注意镜像下标 `idx = (N-1)-i`：当 `i` 走到后半段时，它把下标「折回」前半段，于是后半段的乘法器读到的系数与前半段对称位置完全相同。

#### 4.2.3 源码精读

**第一步：派生唯一系数个数 `c_coeff_2`。**

[.drl_src_code/filt_fir/rtl/filt_fir.v:4](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L4) 定义了一个向上取整的宏：

```verilog
`define DIV(N, D) (N%D==0) ? (N/D) : (N/D+1)
```

[.drl_src_code/filt_fir/rtl/filt_fir.v:24](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L24) 用它算出半数组大小：

```verilog
localparam c_coeff_2 = (gp_symm) ? `DIV(gp_coeff_length, 2) : gp_coeff_length;
```

- `gp_symm=1`：`c_coeff_2 = ⌈N/2⌉`（如 \(N=17\Rightarrow 9\)）。
- `gp_symm=0`：`c_coeff_2 = N`（退化为普通非对称，存全部）。

[.drl_src_code/filt_fir/rtl/filt_fir.v:28](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L28) 把数组声明成 `c_coeff_2` 大小——这就是「系数 ROM 减半」的落点：

```verilog
wire signed [gp_coeff_width-1:0] c_coeff [0:c_coeff_2-1];
```

**第二步：转置型（TF）里的镜像下标。**

[.drl_src_code/filt_fir/rtl/filt_fir.v:43-53](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L43-L53) —— TF 形式下所有乘法器都读同一个广播输入 `i_data`，靠下标区分系数：

```verilog
if (gp_symm) begin:g_fir_symm
  if (i<c_coeff_2)
    assign w_mul[...] = $signed(i_data) * c_coeff[i];                  // 前半段
  else
    assign w_mul[...] = $signed(i_data) * c_coeff[gp_coeff_length-1-i];// 后半段：镜像
end
```

注意转置型里乘法器个数仍是 `gp_coeff_length`（循环 `for(i=0;i<gp_coeff_length;i=i+1)` 在第 41 行），对称只改变了**读哪个系数**。

**第三步：直接型（DF）里同样的镜像写法。**

[.drl_src_code/filt_fir/rtl/filt_fir.v:88-101](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L88-L101) —— DF 形式下乘法器读延迟线 `r_dly_df[i]`，但镜像逻辑完全一样：

```verilog
if (i<c_coeff_2)
  assign w_mul[...] = $signed(r_dly_df[...]) * c_coeff[i];
else
  assign w_mul[...] = $signed(r_dly_df[...]) * c_coeff[gp_coeff_length-1-i]; // 镜像
```

> 小结：无论 TF 还是 DF，`filt_fir` 的 `gp_symm` 只动「系数下标」，不动乘法器数量。这是「并行、保吞吐、省系数 ROM」的选择；想真正省乘法器，要换架构（见 4.3）。

#### 4.2.4 代码实践

**实践目标**：手动追踪 `gp_symm=1`、`gp_coeff_length=17` 时，`c_coeff_2` 的取值与每个乘法器 `i` 实际选用的系数下标，画出对称配对表，并说明中间抽头的处理。

**操作步骤**：

1. 套用公式：`c_coeff_2 = ⌈17/2⌉ = 9`，即唯一系数 `c_coeff[0..8]`。
2. 对每个 `i = 0..16`，按「`i<9` 用 `i`；否则用 `16-i`」算出实际下标 `idx`。
3. 列表，观察序列是否回文（palindromic），并定位中间抽头。

**预期结果（对称配对表）**：

| 乘法器 `i` | 实际系数下标 `idx` | 唯一系数 | 说明 |
| :-: | :-: | :-: | :-: |
| 0 | 0 | `c_coeff[0]` | 前半段起点 |
| 1 | 1 | `c_coeff[1]` | |
| 2 | 2 | `c_coeff[2]` | |
| 3 | 3 | `c_coeff[3]` | |
| 4 | 4 | `c_coeff[4]` | |
| 5 | 5 | `c_coeff[5]` | |
| 6 | 6 | `c_coeff[6]` | |
| 7 | 7 | `c_coeff[7]` | |
| **8** | **8** | **`c_coeff[8]`** | **中间抽头（仅出现一次）** |
| 9 | 16−9 = 7 | `c_coeff[7]` | 镜像复用 |
| 10 | 6 | `c_coeff[6]` | 镜像复用 |
| 11 | 5 | `c_coeff[5]` | 镜像复用 |
| 12 | 4 | `c_coeff[4]` | 镜像复用 |
| 13 | 3 | `c_coeff[3]` | 镜像复用 |
| 14 | 2 | `c_coeff[2]` | 镜像复用 |
| 15 | 1 | `c_coeff[1]` | 镜像复用 |
| 16 | 0 | `c_coeff[0]` | 镜像复用（与前半段起点配对） |

整条系数序列为 `[0,1,2,3,4,5,6,7,8,7,6,5,4,3,2,1,0]`——一个回文，仅由 9 个唯一值重建。

**中间抽头在奇偶长度下的处理**：

- **奇数长度（如 17）**：`c_coeff_2=9`，中间抽头 `i=8` 落在「前半段」分支（`8<9`），直接用 `c_coeff[8]`；后半段镜像下标最大只到 7，所以 `c_coeff[8]` 永远不会被任何镜像项重复读取——**恰好使用一次**，与 4.1 节「中间抽头单独乘一次」的数学完全吻合。`filt_fir` 这里无需任何 `if` 特判，靠 `⌈N/2⌉` 的取整天然把中间项归到前半段。
- **偶数长度（如 16）**：`c_coeff_2=⌈16/2⌉=8`，没有「中间」这一项，`i=0..7` 用 `i`、`i=8..15` 用 `15-i`，两两完美配对，全部系数都被使用恰好两次。

> 留意：`filt_fir` 对中间抽头「不特判」，是因为它根本没有做预加——每个乘法器独立乘，中间抽头自然只乘一次。下一节的 `filt_mac` 因为要预加，就必须显式区分中间抽头。

#### 4.2.5 小练习与答案

**练习 1**：把 `gp_coeff_length` 从 17 改成 18（仍 `gp_symm=1`），`c_coeff_2` 变成多少？乘法器有几个？

**答案**：`c_coeff_2 = ⌈18/2⌉ = 9`，唯一系数 9 个；但乘法器仍是 18 个（`filt_fir` 不省乘法器）。配对为 9 对，无中间抽头。

**练习 2**：为什么说 `filt_fir` 的 `gp_symm` 优化「省面积但有限」？

**答案**：它只把系数 ROM 从 \(N\) 条减到 \(\lceil N/2\rceil\) 条，乘法器仍是 \(N\) 个、吞吐仍是 1 样本/时钟。要进一步省乘法器，必须改用预加架构（`filt_mac`），代价是吞吐下降。

**练习 3**：`gp_symm=1` 时，如果 `gen_coeffs.m` 误写了全部 \(N\) 条系数，RTL 会怎样？

**答案**：`c_coeff` 数组只有 `c_coeff_2` 个槽位（`[0:c_coeff_2-1]`），多写的 `c_coeff[c_coeff_2..N-1]` 越界，elaborate 阶段就会报错。GRM 与 RTL 必须在「存几条」上严格一致。

---

### 4.3 乘法器减半（filt_mac 的预加法器）

#### 4.3.1 概念说明

`filt_mac` 走的是与 `filt_fir` 完全不同的路线：**用单个乘法器 + 累加器分时复用**，把所有抽头的乘加「摊」到多个时钟周期完成（详见 u3-l4）。在这种「顺序处理」的架构里，对称性的预加优化可以被**完整地**用上：

- 乘法输入 `A` 不再是单个延迟线样本，而是**预加和** `x[k]+x[N-1-k]`。
- 于是每个输出样本只需 \(\lceil N/2\rceil\) 次 MAC，而不是 \(N\) 次。

这正是 4.1 节原理的硬件落地。代价是吞吐下降（一个样本要花更多周期），换的是乘法器从「\(N\) 个并行」降到「1 个」。这和 `filt_fir` 形成清晰的「面积—吞吐」谱系两端。

#### 4.3.2 核心流程

`filt_mac` 对称分支的工作流程（每个输出样本重复一次）：

```
c_coeff_size = ⌈N/2⌉
c_even_odd_symm = (N 为偶数) ? 1 : 0     # 标记「长度是否为偶数」

r_count_coeff 从 0 计数到 c_coeff_size-1，每个时钟做一次 MAC：

if 对称:
    if (长度为奇数 且 计数到中间抽头):
        A = delay_line[中间]             # 中间抽头：无配对，单独用
    else:
        A = delay_line[r] + delay_line[N-1-r]   # 预加！
else:
    A = delay_line[r]                    # 非对称：直接用

B = c_coeff[r]
累加器 += A * B                           # 单乘法器
```

注意两个派生量：`c_coeff_size` 是「MAC 次数 = 唯一系数个数 = ⌈N/2⌉」；`c_even_odd_symm` 专门用来标记**长度是否为偶数**，从而决定要不要对中间抽头做特判。

#### 4.3.3 源码精读

**第一步：派生 MAC 次数与奇偶标志。**

[.drl_src_code/filt_mac/rtl/filt_mac.v:22](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/rtl/filt_mac.v#L22) —— 用另一种写法算 \(\lceil N/2\rceil\)：

```verilog
localparam c_coeff_size = (gp_symm) ? ((gp_coeff_length/2)+(gp_coeff_length%2)) : gp_coeff_length;
```

`floor(N/2)+(N%2)` 与 `filt_fir` 的 `DIV(N,2)` 数值相同（都是 \(\lceil N/2\rceil\)），只是写法不同。

[.drl_src_code/filt_mac/rtl/filt_mac.v:23](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/rtl/filt_mac.v#L23) —— 奇偶标志（注意：它为 1 表示**偶数**长度）：

```verilog
localparam c_even_odd_symm = (gp_symm && ((gp_coeff_length%2)==0)) ? 1'b1 : 1'b0;
```

[.drl_src_code/filt_mac/rtl/filt_mac.v:33](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/rtl/filt_mac.v#L33) —— 系数数组同样减半：

```verilog
wire signed [gp_coeff_width-1:0] c_coeff [0:c_coeff_size-1];
```

**第二步：预加法器（本讲的核心）。**

[.drl_src_code/filt_mac/rtl/filt_mac.v:34](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/rtl/filt_mac.v#L34) —— 预加和的位宽比样本多 1 bit，正是 4.1.5 练习 2 的结论：

```verilog
wire signed [gp_inp_width    :0] w_mul_inp_a; // for symmetric filters to avoid overflow of DL addition
```

（`[gp_inp_width:0]` 共 `gp_inp_width+1` 位。）

[.drl_src_code/filt_mac/rtl/filt_mac.v:77-89](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/rtl/filt_mac.v#L77-L89) —— 预加法器本体，中间抽头特判就藏在这里：

```verilog
generate
  if (gp_symm) begin:g_symm_inp_a
    assign w_mul_inp_a = ( !c_even_odd_symm && (r_count_coeff==c_coeff_size-1) ) ? // MIDDLE TAP
                           r_delay_line[r_count_coeff]
                         :                                                                         // MIRRORED TAPS
                           ($signed(r_delay_line[r_count_coeff]) + $signed(r_delay_line[gp_coeff_length-1-r_count_coeff]));
  end
  else begin:g_asymm_inp_a
    assign w_mul_inp_a = (r_count_coeff<c_coeff_size) ? $signed(r_delay_line[r_count_coeff]) : 'sd0;
  end
endgenerate
```

读这段三元运算的关键：

- 条件 `!c_even_odd_symm && (r_count_coeff==c_coeff_size-1)` 同时要求「长度为奇数」且「计数到最后一次」。只有奇数长度才有无配对的中间抽头，而它恰好在计数器的最后一拍（`c_coeff_size-1`）。
  - `N=17`：`c_coeff_size=9`，最后一拍 `r_count_coeff=8`，正是中间抽头 → 用 `r_delay_line[8]` 单独乘一次。其余 8 拍做预加。
  - `N=16`：`c_even_odd_symm=1`，条件恒假 → 每一拍都做预加，没有特判，8 对完美配对。
- 预加分支：`r_delay_line[r] + r_delay_line[N-1-r]`，镜像下标 `N-1-r` 与 `filt_fir` 的镜像写法同源，但这里是**对数据**预加，而非对系数镜像。

**第三步：单乘法器 + 累加器。**

[.drl_src_code/filt_mac/rtl/filt_mac.v:92-98](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/rtl/filt_mac.v#L92-L98) —— 只有一个乘法器，结果送累加：

```verilog
assign w_mul_inp_b = (r_count_coeff<c_coeff_size) ? $signed(c_coeff[r_count_coeff]) : 'sd0;
assign w_mul_oup   = $signed(w_mul_inp_a) * $signed(w_mul_inp_b);   // 单乘法器
assign w_add_oup   = $signed(w_mul_oup)   + $signed(r_add_oup);     // 累加
```

对比 `filt_fir` 的 `gp_coeff_length` 个并行乘法器，这里只有 1 个——对称预加让它在 \(\lceil N/2\rceil\) 拍内做完原本需要 \(N\) 拍（或 \(N\) 个乘法器一拍）的工作。

> 节拍控制：[.drl_src_code/filt_mac/rtl/filt_mac.v:127](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/rtl/filt_mac.v#L127) 的 `w_done` 在计数到 `c_coeff_size-1` 时拉高，标志一个样本的 MAC 序列完成；延迟线移位、累加器清零、输出锁存都由它节拍（详见 u3-l4）。

#### 4.3.4 代码实践

**实践目标**：对照源码，手算 `filt_mac` 在 `gp_coeff_length=17`、`gp_symm=1` 时一个输出样本的 MAC 序列，验证「9 次 MAC + 中间抽头特判」。

**操作步骤**：

1. 计算 `c_coeff_size = 8+1 = 9`，`c_even_odd_symm = 0`（17 为奇数）。
2. 对 `r_count_coeff = 0..8`，逐拍写出 `w_mul_inp_a` 与 `w_mul_inp_b` 的取值（用 `r_delay_line[k]` 与 `c_coeff[k]` 的符号表示）。
3. 标出哪一拍命中中间抽头特判。

**预期结果（MAC 序列表）**：

| 拍 `r_count_coeff` | `w_mul_inp_a`（预加/特判） | `w_mul_inp_b` |
| :-: | :-- | :-- |
| 0 | `r_delay_line[0] + r_delay_line[16]` | `c_coeff[0]` |
| 1 | `r_delay_line[1] + r_delay_line[15]` | `c_coeff[1]` |
| 2 | `r_delay_line[2] + r_delay_line[14]` | `c_coeff[2]` |
| 3 | `r_delay_line[3] + r_delay_line[13]` | `c_coeff[3]` |
| 4 | `r_delay_line[4] + r_delay_line[12]` | `c_coeff[4]` |
| 5 | `r_delay_line[5] + r_delay_line[11]` | `c_coeff[5]` |
| 6 | `r_delay_line[6] + r_delay_line[10]` | `c_coeff[6]` |
| 7 | `r_delay_line[7] + r_delay_line[9]`  | `c_coeff[7]` |
| **8** | **`r_delay_line[8]`**（中间抽头特判，无配对） | **`c_coeff[8]`** |

9 拍完成 9 次 MAC（8 次预加 + 1 次中间抽头），覆盖全部 17 个延迟线样本，恰好等于 4.1 节的 \(\lceil17/2\rceil=9\)。把 9 次 `A*B` 累加起来，就得到一个完整的输出样本。

**需要观察的现象**：第 8 拍的 `w_mul_inp_a` 不再是两项之和，而是单个样本——这就是 `!c_even_odd_symm && (r_count_coeff==c_coeff_size-1)` 命中后的行为。如果把长度改成 16，则 `c_even_odd_symm=1`，第 8 拍（`c_coeff_size-1=7`）也会是预加 `r_delay_line[7]+r_delay_line[8]`，没有任何特判拍。

> 待本地验证：上述节拍可用 iverilog 仿真 `filt_mac`，在 `w_mul_inp_a`/`w_mul_inp_b` 上打断点或加 `$display` 逐拍打印确认。

#### 4.3.5 小练习与答案

**练习 1**：`filt_mac` 在 `gp_coeff_length=16`、`gp_symm=1` 时，一个样本需要几拍 MAC？有没有特判拍？

**答案**：`c_coeff_size=8`，需要 8 拍；`c_even_odd_symm=1`，条件恒假，没有特判拍，每一拍都是预加。

**练习 2**：把 `filt_mac` 的 `w_mul_inp_a` 改回 `gp_inp_width-1:0`（去掉多出的 1 bit），对称模式下会发生什么？

**答案**：两个 `gp_inp_width` 位补码数相加可能溢出，最高位进位会被截断，预加和出错，进而乘积累加结果偏离黄金响应，回归仿真会 FAILED。这正是源码注释里 `// to avoid overflow of DL addition` 的含义。

**练习 3**：用一句话对比 `filt_fir` 与 `filt_mac` 对对称性的利用程度。

**答案**：`filt_fir` 用镜像下标把**系数 ROM**减半但保留全部并行乘法器（保吞吐）；`filt_mac` 用预加法器把**MAC 次数**减半、降到单乘法器（省面积、降吞吐）。

---

## 5. 综合实践

**任务**：用同一组 9 抽头对称系数 \(h=[-1,2,-3,4,-5,4,-3,2,-1]\)（\(N=9\)，奇数，中间抽头 \(h[4]=-5\)），分别在 `filt_fir` 与 `filt_mac` 的模型上「走一遍」，体会两者对对称性的不同利用。

**步骤**：

1. **数学层**：写出该滤波器的预加卷积式，确认乘法次数 = \(\lceil9/2\rceil=5\)（4 对 + 1 中间）。
2. **`filt_fir` 层**：
   - 算 `c_coeff_2 = ⌈9/2⌉ = 5`，唯一系数为 `[-1,2,-3,4,-5]`。
   - 列出 9 个乘法器的系数下标序列，验证它是回文 `[-1,2,-3,4,-5,4,-3,2,-1]`，且中间抽头 `-5` 只出现一次。
   - 指出：乘法器仍是 9 个，优化只发生在系数 ROM（5 条 vs 9 条）。
3. **`filt_mac` 层**：
   - 算 `c_coeff_size = 5`，`c_even_odd_symm = 0`。
   - 列出 5 拍 MAC 序列，指出第 4 拍（`r_count_coeff=4`）命中中间抽头特判，`w_mul_inp_a = r_delay_line[4]` 单独使用。
   - 指出：只有 1 个乘法器，5 拍完成一个样本。
4. **比对**：用一段话总结「同一份对称系数，在并行架构里省 ROM、在顺序架构里省乘法器」这一设计取舍，并联系 u3-l2 的「面积换速度」主题。

**预期结果**：你能清楚说出——对称性是一块「白送」的优化空间，`filt_fir` 取了「保吞吐、省系数存储」这一端，`filt_mac` 取了「省乘法器、降吞吐」另一端；两者共享同一份由 `gen_coeffs.m` 生成的「半数组」系数文件，并通过各自的镜像/预加机制重建完整卷积，因而对同一黄金参考模型都能做到比特真。

## 6. 本讲小结

- 线性相位 FIR 的对称性 \(h[k]=h[N-1-k]\) 允许把配对项预加 \(x[n-k]+x[n-(N-1-k)]\)，使乘法次数从 \(N\) 降到 \(\lceil N/2\rceil\)。
- `filt_fir` 的 `gp_symm` 用 `c_coeff_2=⌈N/2⌉` 把**系数数组减半**，并通过镜像下标 `gp_coeff_length-1-i` 重建回文系数序列；但乘法器仍是 \(N\) 个——它省的是系数 ROM，不是乘法器。
- `filt_mac` 才是真正把乘法器减半的地方：用预加法器 `w_mul_inp_a = x[k]+x[N-1-k]` 在**单乘法器**上只做 \(\lceil N/2\rceil\) 次 MAC，代价是吞吐下降。
- 中间抽头只在**奇数长度**出现且无配对：`filt_fir` 靠 `⌈N/2⌉` 取整天然把它归到前半段（恰好用一次）；`filt_mac` 用 `c_even_odd_symm` 显式特判，在最后一拍单独使用中间样本。
- GRM 侧的 `gen_coeffs.m` 在 `symm=1` 时只写 `⌈N/2⌉` 条系数，与 RTL 的半数组严格对齐，是比特真验证的前提。
- 预加和需要比样本多 1 bit 才不溢出（`filt_mac` 的 `w_mul_inp_a` 声明为 `[gp_inp_width:0]`）。

## 7. 下一步学习建议

- 继续 [u3-l4 filt_mac — 资源共享型 MAC FIR](u3-l4-filt-mac.md)：本讲只看了 `filt_mac` 的对称预加，下一讲会完整讲清它的延迟线移位节拍、抽头计数器、`w_done/o_done` 控制与「一个样本要花几个时钟」的吞吐模型。
- 若想看对称性在更复杂拓扑里的运用，可预习单元 5 的多相滤波器（[u5-l3 mul_add 多相乘加引擎](u5-l3-mul-add-engine.md)），那里的系数矩阵化与列划分会和本讲的「系数编排」思路呼应。
- 建议回到源码动手验证：用 `./dsp_rtl_lib.sh -d` 生成 `filt_fir` 与 `filt_mac`，分别切换 `gp_symm=0/1`、`gp_coeff_length` 奇偶，对照回归的 PASSED/FAILED 与生成的 `filt_coeff.v` 行数，加深「半数组」与「预加」的体感。
