# 项目总览：用 FPGA 实现的 DIT 基-2 FFT

## 1. 本讲目标

本讲是整本学习手册的第一讲。读完本讲，你应当能够：

- 用自己的话说清楚「快速傅里叶变换（FFT）」和「按时域抽取（Decimation In Time, DIT）」是在做什么。
- 看懂本项目的整体文件构成，知道每个文件分别负责什么职责。
- 理解 `dit`、`butterfly`、`twiddlefactors` 三个核心模块各做什么、它们之间如何连线。
- 动手画出 `dit` 顶层如何例化 `butterfly` 与 `twiddlefactors` 的结构框图。
- 发现 README 里一个有趣的小「对不上号」：README 提到的 `buffer.v` 在仓库里其实并不叫这个名字。

本讲只要求建立**全局地图**，不要求看懂每一行 Verilog。复杂的控制状态机、流水线时序、地址位运算等细节，会留给后续讲义深入。

## 2. 前置知识

在进入源码之前，先用最通俗的方式把几个概念讲清楚。

### 2.1 什么是傅里叶变换

一段信号（比如一段声音、一组传感器读数）可以看作一组按时间排列的数字 \(x_0, x_1, \dots, x_{N-1}\)。**离散傅里叶变换（DFT）** 把这组「时域」数据转换成一组「频域」数据 \(X_0, X_1, \dots, X_{N-1}\)，告诉我们这组信号里含有哪些频率成分。

直接按定义计算 DFT 的公式是：

\[
X_k = \sum_{n=0}^{N-1} x_n \, e^{-i\,2\pi k n / N}, \qquad k = 0, 1, \dots, N-1
\]

直接算每个 \(X_k\) 都要做 \(N\) 次复数乘法，\(N\) 个 \(X_k\) 加起来是 \(O(N^2)\) 次复数乘法。当 \(N\) 很大时，这个计算量是惊人的。

### 2.2 快速傅里叶变换（FFT）与基-2

**快速傅里叶变换（FFT）** 不是一种新变换，而是计算 DFT 的一种**聪明算法**。它利用了 DFT 公式里大量的重复结构，把计算量从 \(O(N^2)\) 降到 \(O(N \log N)\)。

本项目实现的是其中最经典的一类：**基-2（radix-2）FFT**。它要求 \(N\) 必须是 2 的幂（如 8、16、32）。其核心思想是「分治」：把长度为 \(N\) 的 DFT 拆成两个长度为 \(N/2\) 的 DFT，再递归地拆下去。

### 2.3 按时域抽取（DIT）

把序列按**下标的奇偶**拆成两个子序列，分别求 DFT，再合并——这种拆法就叫**按时域抽取（Decimation In Time, DIT）**。设 \(E_k\) 是偶数下标子序列的 DFT，\(O_k\) 是奇数下标子序列的 DFT，那么：

\[
X_k = E_k + e^{-i\,2\pi k / N} O_k, \qquad k < N/2
\]

\[
X_k = E_{k-N/2} - e^{-i\,2\pi (k-N/2) / N} O_{k-N/2}, \qquad k \ge N/2
\]

公式里反复出现的那个旋转复数 \(e^{-i\,2\pi k / N}\) 有个专门的名字——**旋转因子（twiddle factor）**，常记作 \(W_N^k\)。它是本项目另一个核心模块 `twiddlefactors` 的命名来源。

### 2.4 蝶形运算（Butterfly）

上面合并公式里那种「取两个数、乘一个旋转因子、做一次加法和一次减法、得到两个新数」的结构，画成数据流图长得像一只蝴蝶，所以叫**蝶形运算（butterfly）**。它就是 FFT 的「计算原子」——整个 FFT 就是把成千上万个蝶形串起来。

设蝶形两路输入为 \(XA\)、\(XB\)，旋转因子为 \(W\)，则两路输出为：

\[
YA = XA + W \cdot XB
\]

\[
YB = XA - W \cdot XB
\]

这正是本项目 `butterfly.v` 头部注释里写的那两行公式。

### 2.5 FPGA 与 Verilog

**FPGA**（现场可编程门阵列）是一种可以通过写代码来「重新连线」的芯片。我们用硬件描述语言 **Verilog** 描述电路，然后把代码综合成真实的逻辑门和连线。和普通软件不同，Verilog 描述的是**并行发生**的硬件行为——每个 `always` 块都在同时工作。本项目的 Verilog 代码就是最终要在 FPGA 上跑的硬件电路。

> 如果你完全没接触过 Verilog，也不用担心。本讲我们只读模块声明（module 的端口清单）和顶层例化，这些读起来很像函数签名，门槛很低。

## 3. 本讲源码地图

先从整体看这个项目的文件构成。整个仓库非常小，根目录只有几个文件：

| 文件 | 语言 | 作用 |
| --- | --- | --- |
| `dit.v` | Verilog | **主模块**。实现完整的 FFT，包含数据缓冲、控制状态机、地址计算，并例化 `butterfly` 和 `twiddlefactors`。 |
| `butterfly.v` | Verilog | 实现**单步蝶形运算**，是 FFT 的计算原子。 |
| `twiddlefactors_N.v.t` | Jinja2 模板 | 生成 `twiddlefactors` Verilog 模块的**模板文件**。 |
| `generate_twiddlefactors.py` | Python | 读模板、把旋转因子量化成定点整数、生成 `twiddlefactors_N.v`。 |
| `dut_dit.v` | Verilog | 包一层 **wrapper**，让 `dit` 能被 MyHDL 仿真驱动。 |
| `qa_dit.py` | Python | MyHDL 测试台，做协同仿真并与 numpy 对拍。 |
| `pyfft.py` | Python | 输出 FFT 各级中间结果的 Python 参考实现，便于调试。 |
| `myhdl.vpi` | 二进制 | MyHDL 与 iverilog 之间的 VPI 桥接库。 |
| `README.txt` | 文本 | 项目说明，列出每个文件的职责。 |
| `LICENSE.txt` | 文本 | MIT 许可证。 |

> 注意：`dit.v` 在例化时用到了一个叫 `twiddlefactors` 的模块，但仓库里**没有**这个模块的 `.v` 文件——它需要你在编译前用 `generate_twiddlefactors.py` 生成出来（例如 `twiddlefactors_16.v`）。这是本系列第二讲「如何构建与运行测试」要解决的前置步骤。

本讲我们重点读三个文件：`README.txt`、`dit.v`、`butterfly.v`，并顺带认识 `twiddlefactors` 模块长什么样。

## 4. 核心概念与源码讲解

本讲对应的最小模块有三个：`butterfly`（蝶形运算）、`twiddlefactors`（旋转因子查表）、`dit`（FFT 顶层主模块）。在进入它们之前，先用一个概念小节把 FFT 的数学骨架立起来——它正好对应 `dit.v` 顶部那段重要的数学注释。

### 4.1 DIT 的分治骨架：从 DFT 到旋转因子合并

#### 4.1.1 概念说明

第 2 节我们已经写了 DIT 的合并公式。这里要强调的是：DIT 不是「一次性算完」，而是**分若干级（stage）逐级合并**。设 \(N=2^{\text{NLOG2}}\)，那么一共要做 NLOG2 级。每一级里，数据被分成若干个「交织的子序列」（series），每两个相邻子序列通过一批蝶形合并成一个更长的子序列，直到最后一级只剩一个完整序列，也就是最终的 \(X_k\)。

`dit.v` 顶部注释把这套数学写得非常清楚，是理解整个项目的钥匙。

#### 4.1.2 核心流程

DIT 的分治流程可以概括为：

1. 输入 \(N\) 个复数样本。
2. 设当前级有 \(S\) 个交织子序列（第一级 \(S=N/2\)，最后一级 \(S=1\)）。
3. 对每个蝶形：读取两个输入 \(Q_{\text{in0}}\)、\(Q_{\text{in1}}\)，乘上对应的旋转因子，做加减，写出两个输出 \(P_{\text{out0}}\)、\(P_{\text{out1}}\)。
4. 当本级所有蝶形算完，\(S\) 减半，进入下一级。
5. 重复 NLOG2 级后，输出缓冲里就是最终的 \(X_k\)。

注释里给出的关键地址关系是（设 \(M=N\cdot S\) 为该级输出总数）：

\[
P_{kS+j} = Q_{2kS+j} + T_{kS} \cdot Q_{2kS+S+j}
\]

\[
P_{kS+j+M/2} = Q_{2kS+j} - T_{kS} \cdot Q_{2kS+S+j}
\]

其中 \(T_n = e^{-i\,2\pi n / M}\) 就是旋转因子。这套地址映射在本讲只需建立直觉，具体位运算推导留给第三单元第 3 讲。

#### 4.1.3 源码精读

`dit.v` 第 4 到 9 行的头部注释点明了三件事：这是「按时域抽取」FFT；输出整体缩小了 \(N\) 倍以防溢出；旋转因子位宽必须等于数据位宽。

[dit.v:4-9](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L4-L9)：说明这是 DIT FFT，并且「为防止溢出，输出被整体缩小 N 倍」。

> 这条「缩小 N 倍」很重要：它解释了为什么测试时要把硬件输出**除以 N** 再和 numpy 的 `fft.fft` 比对（详见 u4 测试讲义）。每一级蝶形都会做一次右移定标，NLOG2 级累计下来近似除以 \(2^{\text{NLOG2}} = N\)。

DIT 合并公式那段最关键的数学注释在这里：

[dit.v:190-233](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L190-L233)：把 \(X_k\) 拆成偶/奇子序列 DFT（\(E_k\)、\(O_k\)）的合并关系，并推导出各级蝶形的输入/输出地址 `in0_addr`、`in1_addr`、`out0_addr`、`out1_addr` 以及旋转因子地址 `tf_addr`。

这一段虽然长，但它是整个项目算法的灵魂。初读时不必逐行看懂位运算，只要记住：「`dit` 模块就是用一段状态机，按这套地址关系，逐级驱动一个蝶形单元去算」。

#### 4.1.4 代码实践

**实践目标**：用 `pyfft.py` 这个 Python 参考实现，直观感受 DIT 的「逐级合并」。

**操作步骤**：

1. 打开 `pyfft.py`，找到函数 `fftstages`（递归地把序列拆成偶/奇子序列，并返回每一级的中间结果）。
2. 在项目根目录启动 Python，构造一个长度为 8 的简单复数序列，例如 `x = [1, 0, 0, 0, 0, 0, 0, 0]`（这是一个脉冲，它的 FFT 应当是 8 个相等的值）。
3. 调用 `fftstages(x)`，打印返回的每一级结果。

**需要观察的现象**：随着级数推进，序列如何从「两个长度为 4 的子序列」逐步合并成「一个长度为 8 的完整频谱」。

**预期结果**：最后一级的输出应等于 `[1,1,1,1,1,1,1,1]`（脉冲信号的 FFT 为常数）。

> 说明：本步骤的具体调用方式属于 u4 第 2 讲（pyfft 参考模型）的内容；本讲只需建立「分若干级合并」的直觉。如果本地 Python 环境受限，可仅阅读 `pyfft.py` 的递归结构，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么本项目要求 \(N\) 必须是 2 的幂？

**参考答案**：基-2 FFT 每一级都把序列长度折半，只有当 \(N\) 是 2 的幂时，才能一路折半到长度为 1（不需要再分解），递归才恰好走 NLOG2 级。

**练习 2**：`dit.v` 说输出被「缩小 N 倍」。如果输入是 \(N=16\) 点、每级做一次右移定标，那么总共右移了多少位？为什么约等于除以 N？

**参考答案**：NLOG2 = 4 级，每级右移近似 1 位，共约 4 位，即除以 \(2^4 = 16 = N\)。

---

### 4.2 butterfly 模块：单步蝶形运算

#### 4.2.1 概念说明

`butterfly` 是 FFT 的计算原子。它接收两个复数输入 `xa`、`xb` 和一个旋转因子 `w`，输出两个复数 `YA = XA + W·XB` 与 `YB = XA − W·XB`。整个 `dit` 模块其实就是「一遍遍喂数据给一个 `butterfly`」。

> 一个重要现实约束：`butterfly` 内部用**流水线 + 乘法器复用**来省硬件，代价是「输入不能连续两拍到达」。这个时序细节是 u2 第 3 讲的主题，本讲先记住结论即可。

#### 4.2.2 核心流程

蝶形的数学流程是先把复数乘法 \(W \cdot XB\) 拆成实部/虚部：

\[
W \cdot XB = (w_{re} + i\,w_{im})(xb_{re} + i\,xb_{im})
\]

实部：\(w_{re}\,xb_{re} - w_{im}\,xb_{im}\)；虚部：\(w_{re}\,xb_{im} + w_{im}\,xb_{re}\)。

然后：

1. 把输入复数按「实部在高 X_WDTH 位、虚部在低 X_WDTH 位」拆开。
2. 计算复数乘积 \(W \cdot XB\)（分实部、虚部）。
3. \(YA = XA + W\cdot XB\)，\(YB = XA - W\cdot XB\)。

#### 4.2.3 源码精读

`butterfly.v` 头部注释把功能说得非常直白：

[butterfly.v:4-14](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L4-L14)：说明模块输入 \(W, XA, XB\)，输出 \(YA = XA + W\cdot XB\)、\(YB = XA - W\cdot XB\)，并且「输入最多每两拍来一次，以便少用乘法器」。

模块声明与端口（这是本讲最该看懂的部分）：

[butterfly.v:16-46](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L16-L46)：参数 `M_WDTH`（旁路数据宽度）、`X_WDTH`（数据/旋转因子位宽）；端口包括时钟 `clk`、复位 `rst_n`、旁路数据 `m_in/m_out`、旋转因子 `w`、两路输入 `xa/xb`、握手 `x_nd/y_nd`、复数输出 `y`。

几个端口值得专门记住：

- `m_in` / `m_out`：这是一组**旁路（pass-through）数据**，蝶形本身不计算它，只是把它像 `x_nd` 一样延迟 3 拍后从 `m_out` 吐出来。`dit` 利用它把「这批结果该写回哪个缓冲、是不是最后一级」等控制信息夹带在数据流里一起走完流水线。
- `x_nd` / `y_nd`：握手信号。`x_nd=1` 表示「本拍有新输入」；`y_nd=1` 表示「本拍输出的是 `YA`，下一拍输出 `YB`」。
- `w` / `xa` / `xb` / `y`：都是 `2*X_WDTH` 位宽，高位放实部、低位放虚部。

把复数拆成实部/虚部的 `assign` 在这里：

[butterfly.v:48-63](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L48-L63)：把 `w`、`xa`、`xb` 的高半当实部、低半当虚部，并把输出 `{y_re, y_im}` 拼回一个复数。

真正的计算在四级流水线的 `always` 块里（STAGE 1～4）。本讲只需知道它存在，细节留待 u2：

[butterfly.v:99-168](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L99-L168)：四级流水线，逐级计算 \(W\cdot XB\) 的实部/虚部，再分别输出 `YA`（STAGE 3）和 `YB`（STAGE 4）。

#### 4.2.4 代码实践

**实践目标**：对照源码确认蝶形端口的数据宽度与含义。

**操作步骤**：

1. 打开 [butterfly.v:16-46](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L16-L46)。
2. 在纸上列一张表，记录每个端口的方向（input/output）、位宽、含义。
3. 回答：`xa`、`xb`、`w`、`y` 的位宽都是多少？为什么是这个数？

**需要观察的现象**：所有复数端口位宽都是 `2*X_WDTH`，因为一个复数 = 实部（X_WDTH 位）+ 虚部（X_WDTH 位）。

**预期结果**：`xa/xb/w/y` 都是 `2*X_WDTH` 位；当 `X_WDTH=8` 时，复数占 16 位，实部在 `[15:8]`、虚部在 `[7:0]`。

#### 4.2.5 小练习与答案

**练习 1**：`butterfly` 的 `m_in`/`m_out` 是用来做数学计算的吗？

**参考答案**：不是。它只是「旁路数据」，蝶形对它不做运算，只按和 `x_nd→y_nd` 相同的延迟拍数把它从输入透传到输出，让上层模块把控制信息随数据流一起走完流水线。

**练习 2**：为什么源码注释说「输入不能连续两拍到达」？

**参考答案**：因为模块用一套乘法器分时复用来先后算 \(W\cdot XB\) 的实部和虚部，需要两拍才能完成一次复数乘法的全部乘法；若连续两拍送入新数据，前后两次的乘法会争用同一批乘法器资源，所以协议要求输入至少间隔一拍。（详见 u2 第 3 讲。）

---

### 4.3 twiddlefactors 模块：旋转因子查表

#### 4.3.1 概念说明

旋转因子 \(W_N^k = e^{-i\,2\pi k/N}\) 是一些固定的复数常数。在硬件里，与其每个时钟都用 CORDIC 之类的方法去「算」它，不如**提前算好、存成一张表**，运行时按地址查表即可。本项目就采用查表法：用一个 Python 脚本把 \(N/2\) 个旋转因子预先量化成定点整数，生成一个 Verilog 的 `case` 查表模块，名字就叫 `twiddlefactors`。

#### 4.3.2 核心流程

生成与使用旋转因子的流程是：

1. **量化（Python 端）**：对每个 \(k \in [0, N/2)\)，计算 \(e^{-i\,2\pi k/N}\) 的实部和虚部，把每个落在 \([-1, 1]\) 的浮点数按 `maxno = 2^(width-2)` 量化成定点整数。
2. **生成（模板渲染）**：用 Jinja2 模板 `twiddlefactors_N.v.t` 把这 \(N/2\) 个量化值填进一个 `case (addr)` 语句，得到 `twiddlefactors_N.v`。
3. **查表（硬件运行时）**：`dit` 模块把蝶形需要的旋转因子地址送上 `addr` 并拉高 `addr_nd`，`twiddlefactors` 下一拍在 `tf_out` 上给出对应的定点复数。

#### 4.3.3 源码精读

量化的核心函数 `f_to_istr`：

[generate_twiddlefactors.py:8-18](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/generate_twiddlefactors.py#L8-L18)：把 \([0,1]\) 的浮点数 `f` 量化成整数 `round(f * 2^(width-2))`。注意 `maxno = pow(2, width-2)`——只用了 `width-2` 位，给符号位和防溢出留了余量。

生成函数 `make_twiddle_factor_file`：

[generate_twiddlefactors.py:20-49](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/generate_twiddlefactors.py#L20-L49)：循环 \(k=0..N/2-1\)，用 `cmath.exp(-i*2π*k/N)` 算旋转因子，分离正负号后调用 `f_to_istr` 量化，最后用模板渲染出 `twiddlefactors_{N}.v`。

模板里 `twiddlefactors` 模块的样子：

[twiddlefactors_N.v.t:4-9](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/twiddlefactors_N.v.t#L4-L9)：模块声明。端口有时钟 `clk`、地址 `addr`（宽度 `Nlog2-1` 位，因为只需 \(N/2\) 个表项）、地址有效 `addr_nd`、输出 `tf_out`（宽度 `2*tf_width`，实虚部拼接）。

查表的 `case` 语句在模板的 `always` 块里：

[twiddlefactors_N.v.t:11-25](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/twiddlefactors_N.v.t#L11-L25)：当 `addr_nd` 有效时，按 `addr` 在 `case` 里选出一项，把 `{实部, 虚部}` 两个 `tf_width` 位有符号数拼成 `tf_out`。

> 关键提醒：仓库里**没有** `twiddlefactors_16.v` 这类生成文件，只有模板 `.v.t`。`dit.v` 例化的 `twiddlefactors` 模块必须由你先运行 `generate_twiddlefactors.py` 生成出来，否则编译会报「找不到模块 `twiddlefactors`」。

#### 4.3.4 代码实践

**实践目标**：手算一个旋转因子的量化值，理解定点编码。

**操作步骤**：

1. 取 \(N=8\)、`tf_width=8`，则 `maxno = 2^(8-2) = 64`。
2. 对 \(k=0\)：\(e^{0} = 1\)，实部 1 → `round(1*64)=64`，虚部 0 → `0`。
3. 对 \(k=1\)：\(e^{-i\,2\pi/8} = \cos(45^\circ) - i\sin(45^\circ) \approx 0.7071 - 0.7071\,i\)，实部 → `round(0.7071*64)=45`，虚部 → `45`。

**需要观察的现象**：所有旋转因子的实部/虚部绝对值都不超过 64（= `maxno`），因为它们的幅度都不超过 1。

**预期结果**：`k=0` 对应 `{64, 0}`，`k=1` 对应 `{45, 45}`（实部正、虚部经符号处理后也为正并量化为 45）。

> 说明：符号位的处理细节（`re_sign`/`im_sign`、对负值取绝对值再量化）会在 u2 第 1 讲详解；本讲只需建立「浮点幅度 → 整数」的直觉。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `f_to_istr` 用 `2^(width-2)` 而不是 `2^(width-1)` 作为最大值？

**参考答案**：留出 2 位的余量——1 位给有符号数的符号位，另 1 位给乘法后可能增长的字长，避免在蝶形做加减法时溢出。这与「输出缩小 N 倍防溢出」是一脉相承的设计。

**练习 2**：`twiddlefactors` 模块运行时是在「计算」旋转因子，还是在「查找」它？

**参考答案**：在查找。它只是一个 `case` 查表，地址 `addr` 选中预先量化好的常量，下一拍输出。计算工作在编译前由 Python 提前完成了。

---

### 4.4 dit 模块：FFT 的顶层主模块

#### 4.4.1 概念说明

`dit` 是整个项目的**顶层主模块**，也是本系列后续讲义的主角。它把 `butterfly`（计算）和 `twiddlefactors`（查表）这两个子模块连起来，再加上自己的数据缓冲、控制状态机和地址计算，凑成一个完整的 FFT。本讲我们只看它的「外部接口」「内部缓冲概览」和「如何例化两个子模块」这三件事，建立结构框图。

#### 4.4.2 核心流程

从外部看，`dit` 像一个数据流水器件：

1. 外部按节拍往 `in_x` 喂 \(N\) 个复数样本，每来一个拉高一拍 `in_nd`。
2. 样本先进**输入双缓冲**（`bufferin0`/`bufferin1`），收满一组后通知控制状态机。
3. 控制状态机驱动 `butterfly` 逐级计算，期间数据在**工作缓冲**（`bufferX`/`bufferY`）之间来回搬运。
4. 算完最后一级，结果写入**输出缓冲**（`bufferout`）。
5. `dit` 按节拍从 `out_x` 吐出 \(N\) 个结果，每吐一个拉高一拍 `out_nd`。
6. 若输入来得太快、来不及算，`overflow` 会被拉高报警。

#### 4.4.3 源码精读

`dit` 的模块参数与端口声明：

[dit.v:11-41](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L11-L41)：参数 `N=16`（FFT 长度）、`NLOG2=4`（log2 N）、`X_WDTH=8`（数据位宽）、`TF_WDTH=8`（旋转因子位宽，必须等于 `X_WDTH`）、`DEBUGMODE`。端口有时钟 `clk`、复位 `rst_n`、输入 `in_x`/`in_nd`、输出 `out_x`/`out_nd`、溢出标志 `overflow`。

> 注意端口注释里的编码约定：「复数中实部在低位、虚部在高位」。这一点和 `butterfly.v` 的拆位 `assign` 是一致的。

`dit` 内部定义了一组**全局数据缓冲**，这是它和「单纯一个蝶形」最大的区别：

[dit.v:50-84](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L50-L84)：声明输入双缓冲 `bufferin0`/`bufferin1`、工作缓冲 `bufferX`/`bufferY`、输出缓冲 `bufferout`，以及一组「缓冲是否满」「数据是否已更新」的标志位。

这些缓冲的协作（双缓冲 A/B 翻转、`updatedX`/`updatedY` 防止「读还没写」）是 u3 第 1 讲的主题。本讲只要知道：**数据要经过三级缓冲：输入缓冲 → 工作缓冲 → 输出缓冲**。

驱动这一切的是一个四状态控制状态机：

[dit.v:183-188](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L183-L188)：定义四个状态 `FSM_ST_INIT`、`FSM_ST_IDLE`、`FSM_ST_CALC`、`FSM_ST_SEND`。状态机的转移条件和它如何驱动 `x_nd`/`tf_addr_nd`、何时推进到下一级，是 u3 第 2 讲的主题。

最后，看 `dit` 是如何**例化**两个子模块的——这是本讲最关键的结构点：

[dit.v:545-552](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L545-L552)：例化 `twiddlefactors`（实例名 `twiddlefactors_0`），把旋转因子地址 `tf_addr`、地址有效 `tf_addr_nd` 接到输入，把查表结果 `tf` 接到输出。

[dit.v:554-570](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L554-L570)：例化 `butterfly`（实例名 `butterfly_0`），参数 `M_WDTH = 3 + 2*NLOG2`、`X_WDTH` 同顶层；把两路输入 `in0`/`in1`、旋转因子 `tf`、握手 `x_nd` 接进去，把结果 `z`、`z_nd` 和旁路控制信息 `m_out` 接出来。

仔细看 `butterfly` 的例化会发现一个非常巧妙的设计：`dit` 通过 `m_in` 把一组控制信息 `{readbuf_switch_old, out0_addr, out1_addr, finished, last_stage}` 塞进蝶形，蝶形原样延迟 3 拍后从 `m_out` 吐回 `{readbuf_switch_z, out0_addr_z, out1_addr_z, finished_z, last_stage_z}`。这样 `dit` 就能知道「现在收到的这批结果当初是从哪个缓冲、哪个地址读出来的，应该写回哪里」。

#### 4.4.4 代码实践

**实践目标**：理清 `dit` 与两个子模块的连线关系。

**操作步骤**：

1. 打开 [dit.v:545-570](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L545-L570)。
2. 在纸上画三个方框：中间是 `dit`，左边是 `twiddlefactors`，右边是 `butterfly`。
3. 用箭头标出 `dit` 给 `twiddlefactors` 的 `addr`/`addr_nd`、`twiddlefactors` 回给 `dit` 的 `tf_out`（再由 `dit` 转发给 `butterfly` 的 `w`）。
4. 标出 `dit` 给 `butterfly` 的 `xa`/`xb`/`x_nd`，以及 `butterfly` 回给 `dit` 的 `y`/`y_nd`。

**需要观察的现象**：`twiddlefactors` 的输出 `tf` 并不直接进 `butterfly`，而是先连到 `dit` 内部的 `tf` 线，再由 `dit` 把它接到 `butterfly.w`。也就是说两个子模块之间没有直接连线，所有数据都要经过 `dit` 内部的 wire 中转。

**预期结果**：得到一张「`dit` 居中、`twiddlefactors` 与 `butterfly` 在两侧、通过 `dit` 内部连线串联」的结构框图。

#### 4.4.5 小练习与答案

**练习 1**：`dit` 例化 `butterfly` 时，为什么要把 `M_WDTH` 设成 `3 + 2*NLOG2`？

**参考答案**：因为 `m_in` 这组旁路数据由 3 个 1 位标志（`readbuf_switch_old`、`finished`、`last_stage`）加上两个各 `NLOG2` 位的地址（`out0_addr`、`out1_addr`）拼成，总位宽恰好是 `3 + 2*NLOG2`。

**练习 2**：`dit` 模块声明里 `TF_WDTH` 和 `X_WDTH` 有什么约束？

**参考答案**：注释明确要求 `TF_WDTH` 必须等于 `X_WDTH`（因为 `butterfly` 假定旋转因子和数据位宽相同）。

---

## 5. 综合实践

把本讲的三块知识串起来，完成下面这个**贯穿性小任务**（对应本讲规格里的实践任务）。

### 任务

阅读 `README.txt` 与三个 Verilog 模块声明，画出 `dit` 顶层如何例化 `butterfly` 与 `twiddlefactors` 的结构框图，并标注出 README 中提到的 `buffer.v` 对应实际哪个文件。

### 操作步骤

1. **读 README**：打开 [README.txt:1-18](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/README.txt#L1-L18)，注意它列出了 `dit.v`、`buffer.v`、`generate_twiddlefactors.py`、`twiddlefactors_N.v.t`、`dut_dit.v`、`qa_dit.py`、`pyfft.py`。
2. **找 buffer.v**：在仓库根目录列文件，会发现**根本没有 `buffer.v`**。README 第 7 行写的是「`buffer.v` - Contains a module for a single butterfly step」（一个单步蝶形）。对照实际文件，承担「单步蝶形」职责的是 [butterfly.v:4-14](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L4-L14)。所以 README 说的 `buffer.v` 实际对应 `butterfly.v`——这是项目历史命名遗留的一个小不一致。
3. **画框图**：
   - 中央画 `dit` 大方框，内部标出输入缓冲 → 工作缓冲 → 输出缓冲三级。
   - 在 `dit` 内部画 `butterfly_0` 和 `twiddlefactors_0` 两个小方框。
   - 标注 `dit` → `twiddlefactors` 的 `addr/addr_nd`，`twiddlefactors` → `dit` 的 `tf_out`（→ `butterfly.w`）。
   - 标注 `dit` → `butterfly` 的 `xa(in0)/xb(in1)/x_nd` 和 `m_in`，`butterfly` → `dit` 的 `y(z)/y_nd(z_nd)/m_out`。
4. **标注 README 命名**：在框图旁注一句「README 中的 `buffer.v` = 实际的 `butterfly.v`」。

### 需要观察的现象

- README 描述的文件清单和实际仓库文件**几乎一一对应，唯独 `buffer.v` 对不上**。
- `dit` 是唯一一个会「例化」别的模块的顶层，`butterfly` 和 `twiddlefactors` 都是被它驱动的从模块。

### 预期结果

得到一张清晰的结构框图，并能在图中正确标注 `buffer.v` 实为 `butterfly.v`。同时能用自己的话讲清楚：「外部数据 → dit 的输入缓冲 → dit 用地址计算喂数据给 butterfly（旋转因子由 twiddlefactors 查表提供）→ 结果经工作缓冲逐级合并 → 输出缓冲 → 对外输出」。

> 本实践为「源码阅读型实践」，不需要运行仿真工具，纸笔即可完成。

## 6. 本讲小结

- 本项目用 Verilog 实现了一个**按时域抽取（DIT）的基-2 FFT**，README 自述目标是「简单且文档齐全」，而非追求高效。
- 整个工程的核心是三个模块：**`dit`（顶层主控）**、**`butterfly`（单步蝶形，FFT 的计算原子）**、**`twiddlefactors`（旋转因子查表）**。
- 蝶形运算的数学本质是 \(YA = XA + W\cdot XB\)、\(YB = XA - W\cdot XB\)，复数按「实部高位、虚部低位」定点编码。
- 旋转因子不在硬件里现算，而是由 Python 脚本 `generate_twiddlefactors.py` 配合模板 `twiddlefactors_N.v.t` 预量化成定点查表模块，**编译前必须先生成**。
- `dit` 通过例化把 `butterfly` 和 `twiddlefactors` 串起来，并用三级缓冲（输入/工作/输出）和一个四状态机控制数据流。
- README 中的 `buffer.v` 在实际仓库中对应的是 `butterfly.v`，这是阅读时需要注意的一处命名遗留不一致。

## 7. 下一步学习建议

本讲建立了全局地图，接下来的学习建议是：

1. **先学 u1-l2《如何构建与运行测试》**：动手把 `generate_twiddlefactors.py` 跑起来生成 `twiddlefactors_N.v`，并尝试用 iverilog + MyHDL 跑通 `qa_dit.py`，让项目真正「转起来」。这是后续一切深入的前提。
2. **再进入 u2 单元**：深入 `butterfly.v` 的四级流水线和乘法器复用（u2-l2、u2-l3），并彻底搞懂定点量化（u2-l1）。
3. **然后攻克 u3 单元**：`dit.v` 的双缓冲（u3-l1）、控制状态机（u3-l2）、地址位运算（u3-l3）是本项目最硬核的部分。
4. **最后是 u4 单元**：协同仿真机制、Python 参考模型、量化误差验证与参数化扩展，让你具备二次开发能力。

建议你随手保留本讲画的那张结构框图，后面读任何一段 `dit.v` 代码时，都可以回头对照「我现在在框图的哪一条连线上」。
