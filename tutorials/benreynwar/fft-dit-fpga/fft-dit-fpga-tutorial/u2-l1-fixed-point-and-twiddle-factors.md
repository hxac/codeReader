# 定点复数表示与旋转因子生成

## 1. 本讲目标

本讲是「核心计算单元」单元的第一讲。在动手分析蝶形运算（`butterfly.v`）之前，我们必须先回答两个基础问题：

1. 一个复数在 FPGA 里到底是怎么存的？`butterfly.v` 的端口写的 `[2*X_WDTH-1:0]` 又是什么含义？
2. FFT 需要的旋转因子 \(W_k = e^{-i2\pi k/N}\) 是连续的浮点数，它是怎样变成 Verilog 里一个查表模块的？

学完本讲，你应当能够：

- 说清楚项目里「复数 = 一个固定位宽整数（实部在高半段、虚部在低半段、幅度限制在 ±1）」这套编码约定。
- 看懂 `c_to_int` 如何把测试用的复数数据编码成整数，以及它的幅度边界检查。
- 看懂 `f_to_istr` 如何把一个 \([0,1]\) 的浮点数量化成定点整数，以及它「正负号单独处理」的设计。
- 跟着 `make_twiddle_factor_file` + jinja2 模板，理解 `twiddlefactors_N.v` 这个 `case` 查表模块是怎么被一行行渲染出来的。
- 手算一个旋转因子，并和生成的 Verilog 文件对照核验。

## 2. 前置知识

在进入源码前，先建立三个直觉。本讲不会假设你已熟悉数字信号处理，但需要你接受下面三件事。

### 2.1 为什么 FFT 离不开旋转因子

离散傅里叶变换（DFT）的定义是：

\[
X_k = \sum_{n=0}^{N-1} x_n \, e^{-i\frac{2\pi}{N}kn}, \qquad k=0,1,\dots,N-1
\]

里面反复出现的 \(e^{-i\frac{2\pi}{N}k}\) 就叫**旋转因子（twiddle factor）**，记作 \(W_N^k\)：

\[
W_N^k = e^{-i\frac{2\pi}{N}k} = \cos\!\left(\tfrac{2\pi k}{N}\right) - i\sin\!\left(\tfrac{2\pi k}{N}\right)
\]

\(W_N^k\) 是单位圆上的复数，模长恒为 1，只有实部和虚部的正负号与大小随 \(k\) 变化。基-2 FFT 把一次 N 点 DFT 拆成很多次「蝶形运算」，每个蝶形都要乘一个旋转因子。所以旋转因子是 FFT 的「乘法常数」，必须事先算好、固化到硬件里。

关键观察：一个 N 点 FFT 只用到 \(N/2\) 个不同的旋转因子 \(W_N^0, W_N^1, \dots, W_N^{N/2-1}\)（因为 \(W_N^{k+N/2} = -W_N^k\)）。这正是后面查表大小为 `N/2` 的原因。

### 2.2 定点数：用整数假装小数

FPGA 里的硬件乘法器天然处理的是**整数**。要用它算小数，常见的做法是**定点数（fixed-point）**：约定一个隐含的小数点位置，把小数当成放大的整数来运算。

例如，约定「小数点在第 6 位之前」，那么整数 `64` 就代表 `1.0`，整数 `45` 就代表 `45/64 ≈ 0.703`。这样做有两个好处：

- 乘法和加减法直接复用整数运算单元。
- 只要约定好「放大的倍数」，所有取值范围都是已知的，便于防溢出。

定点数有几种常见的「位宽分配」（俗称 Q 格式）。本讲会遇到两种，先记住结论，后面源码精读时再对应：

- **旋转因子**用「2 位整数（含符号）+ 剩下位是小数」的格式，即 `1.0` 对应整数 \(2^{\text{width}-2}\)。这个格式能精确表示 `+1.0` 和 `-1.0`。
- **数据（输入/输出）**用「1 位符号 + 剩下位是小数」的格式，即 `±1.0` 近似对应 \(±2^{\text{width}-1}\)。这个格式刚好覆盖 \((-1, +1)\)。

这两种格式的「放大倍数」差一倍，并非疏忽——`butterfly.v` 里专门有一次 `>>> (X_WDTH-2)` 的右移把它们对齐（详见 u2-l2、u2-l3）。本讲只需先把这两套编码各自弄懂。

### 2.3 复数怎么塞进一个整数

一个复数有实部和虚部。本项目约定：把实部和虚部各占 `width` 位，**实部拼在高 `width` 位、虚部拼在低 `width` 位**，合成一个 `2*width` 位的整数。后面你会看到，无论是测试代码 `c_to_int`、`int_to_c`，还是 `butterfly.v` 的端口拆分 `w_re = w[2*X_WDTH-1:X_WDTH]` / `w_im = w[X_WDTH-1:0]`，都遵守这套「高实低虚」的拼装规则。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `generate_twiddlefactors.py` | 用 Python 生成旋转因子 Verilog 文件 | `f_to_istr`（量化）、`make_twiddle_factor_file`（生成流程） |
| `twiddlefactors_N.v.t` | jinja2 模板 | 渲染出的 `twiddlefactors` 模块的 `case` 查表结构 |
| `qa_dit.py` | MyHDL 测试台 | `c_to_int`（测试数据的定点复数编码）、`TF_WDTH==X_WDTH` 约束 |
| `butterfly.v`（参考） | 蝶形运算 | 仅用于确认旋转因子端口 `w` 如何被拆成 `w_re/w_im` |

> 提醒：`generate_twiddlefactors.py` 与 `qa_dit.py` 都是 2012 年的 **Python 2** 代码。在 Python 3 下，诸如 `range(0, N/2)`（`N/2` 在 Py3 中是浮点）、`raise StandardError`（Py3 已移除）等都会出问题。本讲的代码实践以「阅读 + 手算」为主；若要在本地真正运行，请先准备好 Python 2 环境，相关结论标注为「待本地验证」。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：先讲编码总原则，再分别讲数据编码 `c_to_int`、旋转因子量化 `f_to_istr`，最后讲生成流程 `make_twiddle_factor_file` 与 `twiddlefactors` 模板。

### 4.1 定点复数编码的总原则

#### 4.1.1 概念说明

无论数据还是旋转因子，本项目都遵循同一条约定：

- 一个复数被编码成一个位宽为 `2*W` 的整数，其中 `W` 是「每个分量（实部或虚部）的位宽」。
- **高 `W` 位是实部，低 `W` 位是虚部**。
- 每个分量的取值范围被限制在 \([-1, 1]\) 之内（旋转因子因为模长恒为 1，天然满足；测试数据则由代码显式校验）。

这条约定是 `butterfly.v` 端口 `[2*X_WDTH-1:0]` 的由来，也是后面 `dit.v` 把数据搬来搬去时的基本单位。

#### 4.1.2 核心流程

把一个复数 \(c = a + bi\) 编码成整数 `code` 的统一流程：

```text
1. 检查 |a| ≤ 1 且 |b| ≤ 1，否则报错。
2. 把 a 量化成 W 位的定点整数 a_int（带符号/或带正负号）。
3. 把 b 量化成 W 位的定点整数 b_int。
4. code = (a_int << W) | b_int   // 实部在高 W 位，虚部在低 W 位
```

解码（整数 → 复数）则是反过来：`a_int = code >> W`，`b_int = code & ((1<<W)-1)`，再各自除回放大倍数。

#### 4.1.3 源码精读

这条「高实低虚」的拼装，最直接的证据在 `butterfly.v` 把旋转因子端口拆开的那几行——硬件侧就是把一个 `2*X_WDTH` 位的 `w` 一刀切成实部和虚部：

[generate_twiddlefactors.py:8-18](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/generate_twiddlefactors.py#L8-L18) 这是 `f_to_istr` 的全貌，下文 4.3 再细讲，这里先知道它负责「把一个 \([0,1]\) 的数量化成整数」。

而 `butterfly.v` 对端口 `w` 的拆分，证明了「高实低虚」的约定在硬件里真实生效：

[butterfly.v:30-30](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L30-L30) 旋转因子端口声明为 `input wire signed [2*X_WDTH-1:0] w`，即一个 `2*X_WDTH` 位的有符号整数，实部和虚部打包在里面。

[butterfly.v:49-52](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L49-L52) 用 `assign` 把 `w` 的高 `X_WDTH` 位取为 `w_re`（实部）、低 `X_WDTH` 位取为 `w_im`（虚部）。`xa`、`xb` 也是同样的拆法。

这套「一个整数装一个复数」的约定，让蝶形运算的端口数量保持简洁：每个复数只需一根总线。

#### 4.1.4 代码实践

**实践目标**：在源码里确认「高实低虚」的拼装出现在哪些地方。

**操作步骤**：

1. 打开 `butterfly.v`，找到第 49–52 行，确认 `w_re` 取的是高位、`w_im` 取的是低位。
2. 同文件里找到 `xa_re/xa_im`、`xb_re/xb_im`（约第 53–60 行），确认它们用了完全相同的拆分方式。
3. 打开 `qa_dit.py`，找到 `int_to_c`（第 62–75 行），看它如何用 `k >> x_width` 取实部、`k % pow(2,x_width)` 取虚部。

**需要观察的现象**：硬件（Verilog）用位拼接/位截取，Python 用移位和取模，两边拆法在数学上等价——都验证了「实部在高半段、虚部在低半段」。

**预期结果**：你能用一句话说出 `w[X_WDTH-1:0]` 装的是虚部、`w[2*X_WDTH-1:X_WDTH]` 装的是实部。

#### 4.1.5 小练习与答案

**练习 1**：如果 `X_WDTH = 16`，那么 `butterfly.v` 里 `w`、`xa`、`xb` 这几个端口各是多少位？一个复数占多少位？

**答案**：每个分量 16 位，复数占 `2*16 = 32` 位，所以 `w`、`xa`、`xb` 都是 `[31:0]` 共 32 位。

**练习 2**：为什么可以把实部和虚部「拼」进同一个整数，而不互相干扰？

**答案**：因为实部固定占据高 `W` 位、虚部固定占据低 `W` 位，各管一段，互不重叠；需要时用移位/截取就能还原，等价于一个 `2W` 位的容器里装了两个独立的 `W` 位字段。

---

### 4.2 c_to_int：把测试数据编码成定点整数

#### 4.2.1 概念说明

`c_to_int` 是测试台 `qa_dit.py` 里的工具函数，作用是把一个**测试用复数**编码成 `2*x_width` 位的整数，再通过 MyHDL 喂给 Verilog 设计。它体现了数据通道（区别于旋转因子）那套定点约定，并且显式地做了幅度范围检查——一旦实部或虚部超出 \([-1,1]\) 就直接抛错。

#### 4.2.2 核心流程

数据通道每个分量的编码思路是「把 \([-1,1]\) 线性映射到一个 `x_width` 位整数的全部取值范围」，高位为 1 视为负数，效果近似二进制补码的小数。伪代码：

```text
输入: 复数 c, 位宽 x_width
1. 若 c.real 或 c.imag 不在 [-1, 1]，抛 ValueError。
2. 把负的分量「折」到上半段（加上 2），使每个分量都落在 [0, 2)。
3. maxint = 2^x_width - 1
4. i = round(c.real / 2 * maxint)   # 实部量化
   q = round(c.imag / 2 * maxint)   # 虚部量化
5. 返回 (i << x_width) | q          # 实部高、虚部低
```

对应的解码函数 `int_to_c`（第 62–75 行）做相反的事：取高 `x_width` 位、低 `x_width` 位，各乘 `2/maxint` 还原，再对超过 1 的值减 2 折回负数。

> 关于尺度：数据通道里 \(1.0\) 近似对应整数 \(2^{x\_width-1}\)（即「1 位符号 + \(x\_width-1\) 位小数」的格式）。注意它用的是 `maxint = 2^x_width - 1` 而非 `2^x_width`，加上 `round`，所以这是一种**近似**映射，存在 1 个 LSB 量级的量化误差——测试里用 `assertAlmostEqual(..., 3)` 来容忍这点误差（详见 u4-l3）。

#### 4.2.3 源码精读

[qa_dit.py:19-36](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L19-L36) 这是 `c_to_int` 的全貌。逐段看：

- 第 27–28 行：幅度边界检查，保证 \(|实部|, |虚部| \le 1\)，这正是「幅度限制在 ±1」的来源。
- 第 29–30 行：实部为负时，`c = c.real + 2 + c.imag*1j`，把实部从 \([-1,0)\) 折到 \([1,2)\)，相当于用无符号高位段表示负数。
- 第 33–35 行：用 `c.real/2 * maxint` 做线性量化并四舍五入，实部、虚部各算一次。
- 第 36 行：`i * 2^x_width + q`，即实部左移 `x_width` 位再拼上虚部。

[qa_dit.py:91-92](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py#L91-L92) `TestBench.__init__` 里强制 `tf_width == x_width`，否则抛错。这说明虽然「旋转因子」和「数据」用了不同的定点尺度，但**两者的位宽必须相同**，这是整个设计的一致性前提。

> ⚠️ **疑似笔误（待本地验证）**：第 31–32 行处理虚部为负的分支写的是 `c.imag = c.real + (c.imag+2)*1j`。一方面，Python 的 `complex` 是不可变对象，对 `.imag` 赋值会抛 `AttributeError`；另一方面，对照第 29–30 行处理实部的写法，这里按对称性似乎应为 `c = c.real + (c.imag+2)*1j`。也就是说，当输入复数的虚部为负时，这段代码按字面运行大概率会报错。本讲只说明其**意图**（把负虚部折到上半段，与实部对称），具体能否运行请以本地 Python 2 环境为准。

#### 4.2.4 代码实践

**实践目标**：手动追踪 `c_to_int`，体会「幅度限制 + 线性量化」两步。

**操作步骤**：

1. 假设 `x_width = 16`，取 `c = 0.5 + 0j`。手算：实部 0.5 不为负，`i = round(0.5/2 * 65535) = round(16383.75) = 16384`；虚部 0，`q = 0`；结果 `code = 16384 * 65536 + 0`。
2. 再取 `c = -0.5 + 0j`。实部为负，折成 `1.5`，`i = round(1.5/2*65535) = round(49151.25) = 49151`。
3. 对照 `int_to_c`（第 62–75 行）把上面两个 `code` 解码，看是否还原回约 `±0.5`。

**需要观察的现象**：正数 0.5 量化成 `16384 ≈ 2^14`（落在低半段），负数 -0.5 量化成 `49151`（落在高半段，高位为 1）。

**预期结果**：正负数分别落在 16 位字段的上/下两半，对应二进制补码「高位为 1 表示负」的直观效果。

#### 4.2.5 小练习与答案

**练习 1**：`c_to_int` 在什么输入下会抛 `ValueError`？为什么要做这个检查？

**答案**：当 `c.real` 或 `c.imag` 不在 `[-1, 1]` 时抛错。因为定点格式只有 1 位符号位（数据通道近似为 Q1.(width-1)），超出 ±1 的数无法在固定位宽内正确表示，必须在源头拦住。

**练习 2**：把 `c = 1.0 + 0j`、`x_width = 16` 代入，`i` 等于多少？再用 `int_to_c` 解码回来是多少？

**答案**：`i = round(1.0/2 * 65535) = round(32767.5) = 32768`。解码：`i = 32768 * 2 / 65535 ≈ 1.000015`，因不大于 1 不再减 2，约为 `1.0`（有约 1 LSB 的量化误差）。

---

### 4.3 f_to_istr：旋转因子的幅度量化与符号

#### 4.3.1 概念说明

旋转因子 \(W_N^k\) 的模长恒为 1，实部、虚部都在 \([-1,1]\)。`f_to_istr` 只负责其中「一半」的工作：把一个**幅度**（非负、不超过 1 的浮点数）量化成一个定点整数。至于正负号，它不直接处理，而是交给调用方 `make_twiddle_factor_file` 单独记录（`re_sign`/`im_sign`），最终在 Verilog 里写成 `-16'sd...`。

这种「幅度与符号分离」的设计很巧妙：`f_to_istr` 永远只需要处理 \([0,1]\)，逻辑极简；正负号则是 Verilog 二进制补码字面量天然支持的前缀。

#### 4.3.2 核心流程

`f_to_istr(width, f)` 的核心只有一行数学：

\[
\text{maxno} = 2^{\text{width}-2}, \qquad \text{result} = \mathrm{round}(f \times \text{maxno})
\]

为什么是 \(2^{\text{width}-2}\)？因为旋转因子采用「2 位整数（含符号）+ 其余位为小数」的定点格式：在这个格式下，整数 `0b0100...0`（共 `width` 位）正好代表 `+1.0`，而它的整数值正是 \(2^{\text{width}-2}\)。换言之：

\[
1.0 \;\longleftrightarrow\; 2^{\text{width}-2}
\]

于是 \(f \in [0,1]\) 乘以 \(2^{\text{width}-2}\) 再四舍五入，就得到它在定点格式下的整数表示。`width=8` 时，`maxno = 2^6 = 64`，所以 `0.7071 → round(45.25) = 45`。

> 注意与数据通道的区别：数据用 \(2^{\text{width}-1}\)/单位（多一位小数、没有独立整数位），旋转因子用 \(2^{\text{width}-2}\)/单位（留出一位整数位，从而能精确表示 ±1.0）。两者位宽相同但尺度差一倍，由 `butterfly.v` 里 `>>> (X_WDTH-2)` 的右移对齐（见 u2-l3）。

#### 4.3.3 源码精读

[generate_twiddlefactors.py:8-18](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/generate_twiddlefactors.py#L8-L18) `f_to_istr` 全貌：

- 第 15–16 行：校验 `f ∈ [0,1]`，越界抛错——幅度不可能超过 1（旋转因子模长为 1），这是防御性检查。
- 第 17 行：`maxno = pow(2, width-2)`，即 \(2^{\text{width}-2}\)。
- 第 18 行：`int(round(f * maxno))`，量化并转成整数字符串返回。

注释里举的例子「f 为 1 时得到二进制 `010000000`」就是在说：`1.0` 在该定点格式下的位形态是 `0 1 0...0`（符号位 0、整数位 1、其余小数位 0），其整数值正是 `maxno`。

`butterfly.v` 消费旋转因子时，把乘积右移 `(X_WDTH-2)` 位，正好抵消这 \(2^{\text{width}-2}\) 的放大倍数，可作旁证：

[butterfly.v:84-84](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L84-L84) `assign zbw_im = (zbw_m1 >>> (X_WDTH-2)) + (zbw_m2 >>> (X_WDTH-2));` 右移 `X_WDTH-2` 位，与 `f_to_istr` 的 \(2^{\text{width}-2}\) 放大倍数互为逆运算（乘积同时含数据和旋转因子的放大，这里只抵消旋转因子那一份，详见 u2-l3）。

#### 4.3.4 代码实践

**实践目标**：用计算器/手算验证 `f_to_istr` 的量化结果。

**操作步骤**：取 `width = 8`，分别对 `f = 1.0`、`0.7071`、`0` 计算 `round(f * 64)`。

**需要观察的现象**：

- `1.0 → 64`（即 `maxno`，对应二进制 `01000000`，代表 +1.0）。
- `0.7071 → round(45.25) → 45`。
- `0 → 0`。

**预期结果**：你得到 `64`、`45`、`0` 三个整数，分别对应旋转因子 `+1.0`、`±0.7071` 的幅度、`0`。

#### 4.3.5 小练习与答案

**练习 1**：`f_to_istr(8, 0.7071)` 等于多少？为什么 `maxno` 是 64 而不是 128？

**答案**：等于 `45`。`maxno = 2^(8-2) = 64`，因为旋转因子格式保留了 2 位给「符号 + 整数位」（这样才能精确表示 ±1.0），剩下 6 位才是小数，所以 1.0 对应 \(2^6 = 64\)，而不是数据通道里的 \(2^7 = 128\)。

**练习 2**：为什么 `f_to_istr` 只接受 `f ∈ [0,1]`，而不接受负数？

**答案**：因为它只量化「幅度」，正负号由 `make_twiddle_factor_file` 通过 `re_sign/im_sign` 单独记录，并在 Verilog 里用 `-` 前缀表达。把幅度和符号解耦，函数逻辑更简单、更不容易出错。

---

### 4.4 make_twiddle_factor_file 与 twiddlefactors 模板

#### 4.4.1 概念说明

`f_to_istr` 只管量化一个数，而 `make_twiddle_factor_file` 负责把整张旋转因子表（\(N/2\) 个）算出来，配合 jinja2 模板 `twiddlefactors_N.v.t`，渲染成一个可被 `iverilog` 编译的 Verilog 模块 `twiddlefactors_N.v`。这个模块本质上是一个**时钟驱动的查表 ROM**：给一个地址 `addr`，下一拍吐出对应的旋转因子 `tf_out`。这是「用 Python 离线生成硬件常量」的典型套路。

#### 4.4.2 核心流程

`make_twiddle_factor_file(N, tf_width)` 的流程：

```text
1. Nlog2 = log2(N)
2. 对 i = 0 .. N/2 - 1：
     a. v = e^{-i*2π*i/N}              # 第 i 个旋转因子（复数）
     b. 记录 v.real 的符号到 re_sign（'-' 或 ''），并取其幅度
     c. 记录 v.imag 的符号到 im_sign（'-' 或 ''），并取其幅度
     d. re = f_to_istr(tf_width, |v.real|)
        im = f_to_istr(tf_width, |v.imag|)
     e. 把 {i, re_sign, re, im_sign, im} 存进列表 tfs
3. 用 jinja2 渲染 twiddlefactors_N.v.t，得到 twiddlefactors_N.v
```

模板侧 `twiddlefactors_N.v.t` 用 `{% for tf in tfs %}` 把每个旋转因子渲染成 `case` 语句的一条分支：

```text
addr 的位宽 = Nlog2 - 1 位   # 正好够编址 N/2 个旋转因子
case (addr)
  (Nlog2-1)'d0: tf_out <= { <re_sign>tf_width'sd<re>, <im_sign>tf_width'sd<im> };
  (Nlog2-1)'d1: tf_out <= { ... };
  ...
  default: tf_out <= 0;
endcase
```

注意 `{<re>, <im>}` 这个位拼接：高 `tf_width` 位是有符号的实部，低 `tf_width` 位是有符号的虚部，正好对上 4.1 的「高实低虚」约定。

#### 4.4.3 源码精读

[generate_twiddlefactors.py:20-49](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/generate_twiddlefactors.py#L20-L49) `make_twiddle_factor_file` 全貌：

- 第 29 行：`Nlog2 = int(math.log(N, 2))`，得到 FFT 级数/地址位宽参数。
- 第 31 行：`for i in range(0, N/2)`——只生成 \(N/2\) 个旋转因子（`N/2` 在 Python 2 是整除，Python 3 会变浮点导致 `range` 报错，这是 Py2/3 坑之一）。
- 第 34 行：`v = cmath.exp(-i*2j*cmath.pi/N)`，即 \(W_N^i = e^{-i2\pi i/N}\)。注意这里的循环变量名 `i` 与虚数单位 `2j` 容易混淆，`2j` 才是虚数单位。
- 第 35–44 行：分别处理实部、虚部的符号——为正则 `re_sign=''`，为负则 `re_sign='-'` 并把该分量取正（存幅度）。这样后续 `f_to_istr` 收到的永远是 \([0,1]\) 的幅度。
- 第 45–46 行：调用 `f_to_istr` 把幅度量化成整数字符串。
- 第 48 行：`template.render(...)` 把 `tf_width`、`tfs`、`Nlog2` 喂给模板，写出 `twiddlefactors_N.v`。

[twiddlefactors_N.v.t:4-9](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/twiddlefactors_N.v.t#L4-L9) 模块的端口：`addr` 宽度为 `[Nlog2-2:0]`，即 `Nlog2-1` 位，正好编址 `0 .. 2^{Nlog2-1}-1 = 0..N/2-1` 个旋转因子；`tf_out` 为有符号 `2*tf_width` 位。

[twiddlefactors_N.v.t:15-18](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/twiddlefactors_N.v.t#L15-L18) `case` 查表的核心：每条分支形如 `{{Nlog2-1}}'d{{tf.i}}: tf_out <= { {{tf.re_sign}}{{tf_width}}'sd{{tf.re}}, {{tf.im_sign}}{{tf_width}}'sd{{tf.im}} };`。`{{tf.re_sign}}` 渲染为空串或 `-`，`{{tf_width}}'sd{{tf.re}}` 渲染为如 `8'sd45`，于是负的实部会变成 `-8'sd45`——一个合法的 Verilog 有符号补码字面量。

[twiddlefactors_N.v.t:11-25](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/twiddlefactors_N.v.t#L11-L25) 整个 `always @(posedge clk)` 块：当 `addr_nd` 有效时按 `addr` 查表更新 `tf_out`，否则保持；`default` 分支输出 0。这是一个标准的「寄存器输出、一拍延迟」的查表 ROM。

> 这也解释了 u1-l2 强调的前置步骤：编译前必须先 `make_twiddle_factor_file(N, tf_width)` 生成 `twiddlefactors_N.v`，否则 `iverilog` 在 `TestBench.prepare()` 里找不到这个源文件，编译必然失败。

#### 4.4.4 代码实践

**实践目标**：在脑子里「手动渲染」模板的一条分支，理解 jinja2 占位符如何变成 Verilog。

**操作步骤**：取 `N=8, tf_width=8, i=1`，按 4.4.2 的流程算出 `v = e^{-iπ/4} ≈ 0.7071 - 0.7071i`，于是 `re_sign=''`, `re=45`, `im_sign='-'`, `im=45`，`Nlog2=3`。把它套进模板第 17 行那条分支。

**需要观察的现象**：渲染结果应当是

```verilog
2'd1: tf_out <= { 8'sd45,  -8'sd45 };
```

即地址 1 对应旋转因子 `+0.7071 - 0.7071i`：实部 `8'sd45`（正）、虚部 `-8'sd45`（负）。

**预期结果**：你能从「公式 → 符号+幅度 → 量化整数 → Verilog 字面量」走完整条链路，不依赖真正运行脚本。

#### 4.4.5 小练习与答案

**练习 1**：`N=8` 时，生成的 `twiddlefactors_8.v` 里 `case` 有几条有效分支？`addr` 是几位？

**答案**：`N/2 = 4` 条有效分支（`i = 0,1,2,3`），外加一条 `default`。`addr` 宽度为 `Nlog2-1 = 2` 位（`[1:0]`），正好编址 0–3。

**练习 2**：把 `i=0`（即 \(W_8^0\)）的分支渲染出来。

**答案**：\(W_8^0 = 1 + 0i\)，`re_sign=''`, `re=f_to_istr(8,1.0)=64`, `im_sign='-'`（实部为 0 走 else 分支，但 `-8'sd0` 仍为 0）, `im=0`。分支为 `2'd0: tf_out <= { 8'sd64,  -8'sd0 };`，等价于 `{+1.0, 0}`。

---

## 5. 综合实践

把本讲的 4 个模块串起来，完成下面这个「手动生成 + 核验」任务（对应本讲的 `practice_task`）。

**任务**：模拟调用 `make_twiddle_factor_file(8, 8)`，手写出生成的 `twiddlefactors_8.v` 关键内容，并用 `k=1` 那条分支验证你的量化是否正确。

**步骤**：

1. **列旋转因子**。对 `i = 0,1,2,3`，计算 \(v_i = e^{-i2\pi i/8}\)：
   - `i=0`: \(1 + 0i\)
   - `i=1`: \(\cos(\pi/4) - i\sin(\pi/4) \approx 0.7071 - 0.7071i\)
   - `i=2`: \(e^{-i\pi/2} = -i\)，即 \(0 - 1i\)
   - `i=3`: \(\cos(3\pi/4) - i\sin(3\pi/4) \approx -0.7071 - 0.7071i\)

2. **量化幅度**（`tf_width=8`，`maxno=64`）：

   | i | re_sign | re=round(\|re\|·64) | im_sign | im=round(\|im\|·64) |
   | - | ------- | ------------------ | ------- | ------------------ |
   | 0 | `''`    | 64                 | `'-'`   | 0                  |
   | 1 | `''`    | 45                 | `'-'`   | 45                 |
   | 2 | `''`    | 0                  | `'-'`   | 64                 |
   | 3 | `'-'`   | 45                 | `'-'`   | 45                 |

3. **拼出 `case` 分支**（`Nlog2=3`，地址字面量宽度 `Nlog2-1=2`）：

   ```verilog
   // 示例代码：手动按模板渲染出的内容（非仓库已有文件，N=8 时才会生成）
   case (addr)
     2'd0: tf_out <= { 8'sd64,  -8'sd0  };
     2'd1: tf_out <= { 8'sd45,  -8'sd45 };
     2'd2: tf_out <= { 8'sd0,   -8'sd64 };
     2'd3: tf_out <= { -8'sd45, -8'sd45 };
     default: tf_out <= 16'd0;
   endcase
   ```

4. **核验 `k=1`**：`{8'sd45, -8'sd45}` 解码为实部 `+45/64 ≈ +0.703`、虚部 `-45/64 ≈ -0.703`，与理论值 `0.7071 - 0.7071i` 在 8 位定点精度下一致（误差来自四舍五入，约 0.004）。

**进阶（待本地验证）**：若本地有 Python 2 + jinja2，真正运行 `make_twiddle_factor_file(8, 8)`，对比生成的 `twiddlefactors_8.v` 与你手写的内容是否逐行一致；并解释为什么 `maxno` 用 64 而非 128。

## 6. 本讲小结

- 复数在本项目里统一编码为「实部在高半段、虚部在低半段」的固定位宽整数，`butterfly.v` 的 `[2*X_WDTH-1:0]` 端口就是据此拆分。
- 数据通道用 `c_to_int`（近似 Q1.(width-1)，\(1.0 \approx 2^{width-1}\)），并强制 \(|实部|,|虚部| \le 1\)；旋转因子用 `f_to_istr`（Q2.(width-2)，\(1.0 = 2^{width-2}\)），两套尺度差一倍，但位宽必须相等（`tf_width == x_width`）。
- `f_to_istr` 只量化幅度 \([0,1]\)，正负号由 `make_twiddle_factor_file` 单独记录，最终在 Verilog 里写成 `-N'sd...` 的有符号补码字面量。
- `make_twiddle_factor_file` 用 jinja2 模板把 \(N/2\) 个旋转因子渲染成一个时钟驱动的 `case` 查表 ROM（`twiddlefactors_N.v`），地址位宽为 `Nlog2-1`。
- 旋转因子文件必须在 `iverilog` 编译前生成，否则 `TestBench.prepare()` 找不到源文件、编译失败。
- 这些脚本为 Python 2 代码（如 `range(0, N/2)`、`c.imag` 赋值等），在 Python 3 下需修改，实际可运行性请以本地为准。

## 7. 下一步学习建议

本讲解决了「复数怎么存、旋转因子怎么来」。下一步进入 u2-l2《蝶形运算：从复数方程到端口》，你将看到 `butterfly.v` 如何用本讲定义的 `w`（旋转因子）、`xa`、`xb`（数据）这些端口，完成 \(Y_A = X_A + W\cdot X_B\)、\(Y_B = X_A - W\cdot X_B\) 的复数蝶形，以及为什么实部、虚部的乘减能拆成几次整数乘法。建议在阅读 u2-l2 前，先回头确认本讲 4.1 的「高实低虚」约定和 4.3 的旋转因子尺度，因为 u2-l2 会直接用到位截取 `w[2*X_WDTH-1:X_WDTH]` 和右移 `>>> (X_WDTH-2)` 这两个细节。
