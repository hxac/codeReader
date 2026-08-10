# 算法基础：DFT、Cooley-Tukey 与旋转因子

## 1. 本讲目标

本讲是整个 fpga-fft 学习手册的「算法地基」。后续每一讲都会反复出现「蝶形运算」「旋转因子」「逐级分治」「倒序」这些词，如果你对这些概念只是听过名字却没真正推过一遍公式，读硬件源码时就会处处卡壳。

学完本讲，你应该能够：

1. 写出 **DFT（离散傅里叶变换）** 的定义式，并说清楚旋转因子 \(W_N\) 是什么。
2. 用旋转因子的**共轭对称性**和**周期性**解释「为什么 FFT 频谱是镜像的」「为什么大量乘法可以省掉」。
3. 手推 **Cooley-Tukey 分治**：把一个 \(N\) 点 DFT 拆成两个 \(N/2\) 点 DFT，再递归到单点，并理解复杂度为什么从 \(O(N^2)\) 降到 \(O(N\log_2 N)\)。
4. 区分 **DIF（频率抽取）** 与 **DIT（时间抽取）** 两种实现，说清楚 **bit-reverse 倒序** 在流程里到底发生在哪一步——这直接对应本硬件流水线「输出是倒序的、需要重排」这一现状。

本讲只讲算法，不讲 Verilog。所有推导都对照仓库里的真实文档与 MATLAB 脚本，每一处都给出永久链接，你可以边读边核对。

---

## 2. 前置知识

阅读本讲前，你只需要具备：

- **复数基础**：知道 \(j=\sqrt{-1}\)，会算 \((a+jb)\) 的加减乘，知道欧拉公式 \(e^{j\theta}=\cos\theta+j\sin\theta\)。
- **离散序列的概念**：把一段连续信号按固定间隔采样，得到一串数 \(x(0), x(1), \dots, x(N-1)\)，这就是「离散时间信号」。
- **求和符号 \(\Sigma\)**：能看懂 \(\sum_{n=0}^{N-1}\) 这种有限求和。
- **对数**：知道 \(\log_2 N\) 大致是什么意思（比如 \(\log_2 8=3\)）。

如果你对「为什么要做傅里叶变换」还比较陌生，一句话理解：**傅里叶变换把一段信号从「随时间变化」的视图，转换成「由哪些频率成分组成」的视图**。比如一段含 50Hz 和 120Hz 两个正弦波叠加的信号，做 FFT 后会在频谱图上看到这两个频率各有一个尖峰。本项目的 MATLAB 脚本里生成的测试信号正好就是这种叠加正弦波。

几个本讲会反复用到的术语，先统一口径：

| 术语 | 含义 |
| --- | --- |
| DFT | 离散傅里叶变换，把 \(N\) 个时域采样变成 \(N\) 个频域采样，是 FFT 的「原始定义版」 |
| FFT | 快速傅里叶变换，DFT 的一种高效算法，结果和 DFT 完全一样，只是算得快 |
| 旋转因子（twiddle factor） | DFT 公式里反复出现的复数常数 \(W_N=e^{-j2\pi/N}\) |
| 蝶形运算（butterfly） | FFT 里最基本的「一对输入 → 一对输出」的小运算单元 |
| bit-reverse 倒序 | 把二进制下标位序反转，DIF 放在最后、DIT 放在最前 |
| DIF / DIT | 频率抽取 / 时间抽取，两种 FFT 推导路线，最终结果等价 |

---

## 3. 本讲源码地图

本讲涉及的文件全部是「算法与文档」层，不涉及 Verilog：

| 文件 | 作用 |
| --- | --- |
| [scheme/FFT.md](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/scheme/FFT.md) | 项目的设计文档，用 LaTeX 公式完整推导了 DFT 定义、旋转因子性质、Cooley-Tukey 分治、bit-reverse，是本讲的主线 |
| [matlab/DFT_original.m](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/matlab/DFT_original.m) | 最朴素的 DFT 实现，直接套定义式，\(O(N^2)\)，用来当「正确答案」对照 |
| [matlab/FFT_iterative_DIF.m](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/matlab/FFT_iterative_DIF.m) | DIF 路线的迭代 FFT，逐级保存中间结果，**本讲两个最小模块之一** |
| [matlab/FFT_iterative_DIT.m](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/matlab/FFT_iterative_DIT.m) | DIT 路线的迭代 FFT，先倒序再蝶形，最后与 MATLAB 内置 `fft` 做误差校验 |

阅读建议：先读 `scheme/FFT.md` 建立公式直觉，再读 `DFT_original.m` 看「笨办法」，最后对照 `FFT_iterative_DIF.m` 看分治如何把笨办法变快。

---

## 4. 核心概念与源码讲解

本讲按四个最小模块推进：先定义 DFT，再讲旋转因子的两条性质，再推 Cooley-Tukey 分治，最后落到 DIF/DIT 的 MATLAB 迭代实现与 bit-reverse。这四步正是硬件流水线「逐级蝶形」的算法蓝本。

### 4.1 DFT 的定义与旋转因子

#### 4.1.1 概念说明

DFT 回答的问题是：给定 \(N\) 个时域采样 \(x(0), x(1), \dots, x(N-1)\)，怎样求出它的 \(N\) 个频域采样 \(X(0), X(1), \dots, X(N-1)\)？

最直接的定义就是一个求和：每一个频域点 \(X(k)\) 都是所有时域点 \(x(n)\) 乘上一个复数权重后求和。这个复数权重反复出现，记作**旋转因子** \(W_N\)，它是一个模长为 1、辐角为 \(-2\pi/N\) 的复数。直观上，\(W_N^{nk}\) 表示「把 \(x(n)\) 旋转一个角度后再累加」，不同的 \(k\) 对应不同的旋转组合，从而分离出不同的频率成分。

#### 4.1.2 核心流程

DFT 的定义式（正变换）和反变换如下：

\[
X(k)=\sum_{n=0}^{N-1}x(n)W_{N}^{nk}, \quad 0\le k \le N-1
\]

\[
x(n)=\frac{1}{N}\sum_{k=0}^{N-1}X(k)W_{N}^{-nk}, \quad 0\le n \le N-1
\]

旋转因子定义为：

\[
W_{N}=e^{-j \frac{2\pi}{N}}=\cos\frac{2\pi}{N}-j\sin\frac{2\pi}{N}
\]

「直接按定义算 DFT」的计算量分析：

- **复数层面**：每个 \(X(k)\) 要做 \(N\) 次复数乘法，\(N\) 个 \(X(k)\) 共 \(N^2\) 次复数乘法、\(N(N-1)\) 次复数加法。
- 因为一次复数乘法 = 4 次实数乘法 + 2 次实数加法，所以实数乘法约 \(4N^2\) 次。

当 \(N\) 很大（本项目最大 \(N=16384\)）时，\(N^2\) 是约 2.7 亿次复数乘法——这正是「为什么要用 FFT」的动机。

#### 4.1.3 源码精读

`scheme/FFT.md` 一开头就把正反变换和旋转因子定义列了出来：

[scheme/FFT.md:L1-L10](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/scheme/FFT.md#L1-L10) —— DFT 正反变换定义式与旋转因子 \(W_N=e^{-j2\pi/N}\)，并指出「改善 DFT 计算效率的大多数方法用到了旋转因子的对称性和周期性」（这是下一模块的引子）。

而 `matlab/DFT_original.m` 则是「笨办法」的完整实现，几乎就是定义式的直译：

```matlab
function y = DFT_original(source)
    N = length(source);
    Wn = exp(-1j* 2 * pi / N);     % 旋转因子 W_N
    n = 0 : N-1;
    k = n';
    power_coefficient = k * n;     % 得到 N×N 的指数矩阵 nk
    W = Wn^(power_coefficient);    % 得到 N×N 的旋转因子矩阵 W_N^{nk}
    y = W * source;                % 矩阵乘法一次性算出所有 X(k)
end
```

[matlab/DFT_original.m:L1-L10](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/matlab/DFT_original.m#L1-L10) —— 它构造了一个 \(N\times N\) 的矩阵 \(W\)，元素 \((k,n)\) 就是 \(W_N^{nk}\)，再用一次矩阵乘法得到全部 \(X(k)\)。这是理解 DFT 「本质是一个矩阵乘法」的最清晰视角，但它的时间复杂度是实打实的 \(O(N^2)\)。

#### 4.1.4 代码实践

**目标**：亲手感受「定义式 DFT = 矩阵乘法」，并确认它与 MATLAB 内置 `fft` 结果一致。

**步骤**：

1. 在 MATLAB 命令行输入：
   ```matlab
   x = [1 2 -3 -1]';           % 列向量，转置一下
   X_slow = DFT_original(x);   % 笨办法
   X_fast = fft(x);            % MATLAB 内置
   disp([X_slow, X_fast]);     % 并排显示，应完全相等
   ```
2. 观察 `X_slow` 与 `X_fast` 两列数值是否完全相同（除了浮点误差）。

**预期结果**：两列数值一致，印证 `fft` 只是 DFT 的一种快速算法，结果不变。具体数值我们会在 4.4 节手算验证，应为 \([-1,\ 4-3j,\ -3,\ 4+3j]\)。

#### 4.1.5 小练习与答案

**练习 1**：当 \(N=4\) 时，写出旋转因子 \(W_4\) 的具体复数值。

**答案**：\(W_4=e^{-j2\pi/4}=e^{-j\pi/2}=\cos(\pi/2)-j\sin(\pi/2)=-j\)。进而 \(W_4^0=1\)，\(W_4^1=-j\)，\(W_4^2=(-j)^2=-1\)，\(W_4^3=(-j)^3=j\)。

**练习 2**：用 \(W_4\) 的值，按定义式算 \(X(0)\)（取 \(x=[1\ 2\ -3\ -1]\)）。

**答案**：\(X(0)=x(0)W_4^0+x(1)W_4^0+x(2)W_4^0+x(3)W_4^0=1+2-3-1=-1\)。

---

### 4.2 旋转因子的共轭对称性与周期性

#### 4.2.1 概念说明

为什么 DFT 算得这么慢？因为定义式里有大量「重复劳动」——很多 \(W_N^{nk}\) 其实是同一个值，很多 \(X(k)\) 其实彼此关联。FFT 之所以快，正是利用了旋转因子的两条核心性质：

1. **周期性**：\(W_N^N = e^{-j2\pi}=1\)，所以旋转因子每 \(N\) 次方循环一次。这意味着 \(W_N^{nk}\) 的指数可以模 \(N\)，大量项其实相等。
2. **共轭对称性**：\(W_N^{N-k} = W_N^{-k} = (W_N^{k})^*\)。这导致实信号的频谱 \(X(N-k)=X(k)^*\)，也就是「频谱镜像对称」——你在频率 \(f\) 看到一个尖峰，在 \(-f\)（即 \(N-k\)）也看到一个对称的共轭尖峰。

这两条性质不只是「省乘法」，它们还解释了 FFT 输出图上常见的现象：正负频率成对出现、只需看前半段频谱就够。

#### 4.2.2 核心流程

`scheme/FFT.md` 对这两条性质给出了严谨推导。

**共轭对称性**（解释频谱镜像）：

\[
\begin{aligned}
X(N-k) &= \sum_{n=0}^{N-1}x(n)W_{N}^{n(N-k)} \\
       &= \sum_{n=0}^{N-1}x(n)W_{N}^{n(-k)}W_{N}^{Nn} \\
       &= \sum_{n=0}^{N-1}x(n)W_{N}^{-nk} \quad (\text{因为 } W_N^{Nn}=1)\\
       &= \left(\sum_{n=0}^{N-1}x(n)W_{N}^{nk}\right)^{*} = X(k)^{*}
\end{aligned}
\]

关键一步用了周期性 \(W_N^{Nn}=(W_N^N)^n=1^n=1\)，把 \(W_N^{n(N-k)}\) 拆成了 \(W_N^{-nk}\)。当输入 \(x(n)\) 是实数时，\(X(k)^{*}\) 就是简单的共轭，于是 \(X(N-k)\) 与 \(X(k)\) 关于中点镜像对称。

**周期性**（本身）：

\[
X(N+k) = \sum_{n=0}^{N-1}x(n)W_{N}^{n(N+k)} = \sum_{n=0}^{N-1}x(n)W_{N}^{nk} = X(k)
\]

即频谱以 \(N\) 为周期。这是把旋转因子指数「取模」的依据，也是后续 Cooley-Tukey 推导里 \(W_{N}^{2rk}=W_{N/2}^{rk}\) 这一步的来源。

#### 4.2.3 源码精读

[scheme/FFT.md:L13-L21](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/scheme/FFT.md#L13-L21) —— 共轭对称性的完整推导，文档特别标注「解释了为什么 FFT 的频谱是镜像对称的」。

[scheme/FFT.md:L22-L30](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/scheme/FFT.md#L22-L30) —— 周期性推导 \(X(N+k)=X(k)\)。

[scheme/FFT.md:L33-L39](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/scheme/FFT.md#L33-L39) —— 直接 DFT 的计算量分析：复数乘法 \(N^2\) 次、复数加法 \(N(N-1)\) 次。这就是后续要用 FFT 去打的「靶子」。

#### 4.2.4 代码实践

**目标**：用 MATLAB 直观看到「频谱镜像对称」和「周期性」。

**步骤**：

1. 运行：
   ```matlab
   N = 64;
   x = [1 2 -3 1 zeros(1, N-4)];   % 实信号
   X = fft(x);
   plot(abs(X), 'o-');             % 画幅度谱
   ```
2. 观察幅度谱 \(|X(k)|\) 在 \(k\) 和 \(N-k\) 处是否关于 \(k=N/2\) 对称。

**预期结果**：幅度谱左右对称（因为实信号的 \(|X(N-k)|=|X(k)^*|=|X(k)|\)），中间 \(k=N/2\) 是对称轴。这就是共轭对称性的直观体现。

#### 4.2.5 小练习与答案

**练习 1**：证明 \(W_N^{N/2}=-1\)。

**答案**：\(W_N^{N/2}=e^{-j2\pi/N \cdot N/2}=e^{-j\pi}=\cos\pi-j\sin\pi=-1\)。这个结论在 Cooley-Tukey 推导里把 \(W_N^{k+N/2}\) 化成 \(-W_N^k\) 时直接用到。

**练习 2**：为什么对实信号做 FFT 后，看频谱只需要看前半段（\(k=0\sim N/2\)）？

**答案**：因为共轭对称性 \(X(N-k)=X(k)^*\)，幅度 \(|X(N-k)|=|X(k)|\)，后半段是前半段的镜像，不携带新的幅度信息。

---

### 4.3 Cooley-Tukey 分治：从 \(O(N^2)\) 到 \(O(N\log_2 N)\)

#### 4.3.1 概念说明

Cooley-Tukey 是 FFT 的核心思想：**分而治之**。与其一次性算 \(N\) 点 DFT，不如把输入按「偶数下标」和「奇数下标」分成两半，先各算一个 \(N/2\) 点 DFT，再用旋转因子把它们合并。而 \(N/2\) 点 DFT 又可以继续一分为二……直到分到单点（1 点 DFT 就是它本身，不需要算）。

这条递归路线，对应到硬件上就是**流水线的「逐级」结构**——本项目里每一级 `fft_*` 模块做的正是「一级蝶形 + 旋转因子相乘」。所以理解这一节，等于理解了流水线为什么要分成 \(\log_2 N\) 级。

#### 4.3.2 核心流程

把 DFT 定义式里的 \(n\) 拆成偶数项 \(n=2r\) 和奇数项 \(n=2r+1\)：

\[
\begin{aligned}
X(k) &= \sum_{n=0}^{N-1}x(n)W_{N}^{nk} \\
     &= \sum_{r=0}^{N/2-1}x(2r)W_{N}^{2rk}+\sum_{r=0}^{N/2-1}x(2r+1)W_{N}^{(2r+1)k} \\
     &= \sum_{r=0}^{N/2-1}x(2r)W_{N/2}^{rk}+W_{N}^{k}\sum_{r=0}^{N/2-1}x(2r+1)W_{N/2}^{rk}
\end{aligned}
\]

这里用到了周期性推出的 \(W_N^{2rk}=W_{N/2}^{rk}\)（因为 \(W_N^2=W_{N/2}\)）。令

\[
A(k)=\sum_{r=0}^{N/2-1}x(2r)W_{N/2}^{rk}, \qquad B(k)=\sum_{r=0}^{N/2-1}x(2r+1)W_{N/2}^{rk}
\]

即 \(A(k)\) 是偶数子序列的 \(N/2\) 点 DFT，\(B(k)\) 是奇数子序列的 \(N/2\) 点 DFT。于是：

\[
X(k)=A(k)+W_{N}^{k}B(k), \quad 0\le k \le \frac N2-1
\]

那 \(k+N/2\) 处的 \(X\) 呢？利用 \(A(k)\)、\(B(k)\) 以 \(N/2\) 为周期，以及 4.2 练习里的 \(W_N^{k+N/2}=-W_N^k\)：

\[
X\!\left(k+\frac N2\right)=A(k)-W_{N}^{k}B(k), \quad 0\le k \le \frac N2-1
\]

把这两行并排看：

\[
\boxed{\;\;X(k)=A(k)+W_N^k B(k),\qquad X(k+N/2)=A(k)-W_N^k B(k)\;\;}
\]

这就是**蝶形运算**（butterfly）的数学原型——一对输入 \((A,B)\)，乘上旋转因子后做一次「加」和一次「减」，得到一对输出。计算量随之改变：

- 每次把 \(N\) 点拆成两个 \(N/2\) 点，递归 \(\log_2 N\) 层就到单点。
- 每层做 \(N/2\) 个蝶形，每个蝶形 1 次复数乘法、2 次复数加减。
- 总复杂度 \(\boxed{O(N\log_2 N)}\)。

以 \(N=16384\) 为例：直接 DFT 约 \(2.7\times10^8\) 次复数乘法，而 FFT 只需 \(16384\times14\approx2.3\times10^5\) 次——快了一千多倍。

#### 4.3.3 源码精读

[scheme/FFT.md:L56-L91](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/scheme/FFT.md#L56-L91) —— Cooley-Tukey 的完整推导，得出 \(X(k)=A(k)+W_N^k B(k)\) 与 \(X(k+N/2)=A(k)-W_N^k B(k)\)，并总结「N 点分为 N/2 点，持续分割直至单点」的递归思想。

[scheme/FFT.md:L174-L178](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/scheme/FFT.md#L174-L178) —— 计算量分析：Cooley-Tukey 拆成 \(\log_2 N\) 级，每级 \(N\) 次复数乘法，总复杂度 \(N\log_2 N\)。

> ⚠️ **一处文档笔误提醒**：`scheme/FFT.md` 第 178 行小结写的是「总的计算复杂度为 \(O(\log_2 N)\)」，这里漏了因子 \(N\)，正确应为 \(O(N\log_2 N)\)（前两行 176–177 的推导本身是对的）。读文档时以 \(N\log_2 N\) 为准。

#### 4.3.4 代码实践

**目标**：用「分治手算」验证一个小例子，体会 \(A(k)\pm W_N^k B(k)\) 的合并。

**步骤**：取 \(x=[1\ 2\ -3\ -1]\)，\(N=4\)。

1. 偶数子序列：\(x_e=[x(0),x(2)]=[1,-3]\)；奇数子序列：\(x_o=[x(1),x(3)]=[2,-1]\)。
2. 算两个 2 点 DFT（2 点 DFT 就是 \(A(0)=x_e(0)+x_e(1)\)、\(A(1)=x_e(0)-x_e(1)\)，因为 \(W_2^0=1,W_2^1=-1\)）：
   - \(A(0)=1+(-3)=-2\)，\(A(1)=1-(-3)=4\)
   - \(B(0)=2+(-1)=1\)，\(B(1)=2-(-1)=3\)
3. 合并（\(W_4^0=1\)，\(W_4^1=-j\)）：
   - \(X(0)=A(0)+W_4^0 B(0)=-2+1=-1\)
   - \(X(2)=A(0)-W_4^0 B(0)=-2-1=-3\)
   - \(X(1)=A(1)+W_4^1 B(1)=4+(-j)\cdot3=4-3j\)
   - \(X(3)=A(1)-W_4^1 B(1)=4-(-j)\cdot3=4+3j\)

**预期结果**：\(X=[-1,\ 4-3j,\ -3,\ 4+3j]\)。注意合并时输出下标是 \(k\) 与 \(k+N/2\) 交替出现的（\(X(0),X(2)\) 一对，\(X(1),X(3)\) 一对），这正是后面 bit-reverse 倒序问题的来源。这个结果与 `scheme/FFT.md` 第 154 行给的算例完全一致。

#### 4.3.5 小练习与答案

**练习 1**：从 \(W_N^{2rk}\) 推出 \(W_{N/2}^{rk}\) 用到了旋转因子的哪条性质？

**答案**：用到了定义 \(W_N=e^{-j2\pi/N}\)，所以 \(W_N^2=e^{-j2\pi/(N/2)}=W_{N/2}\)，于是 \(W_N^{2rk}=(W_N^2)^{rk}=W_{N/2}^{rk}\)。本质是周期性。

**练习 2**：为什么 \(A(k)\) 以 \(N/2\) 为周期？

**答案**：\(A(k)\) 是长度 \(N/2\) 的序列的 DFT，由周期性，\(A(k+N/2)=A(k)\)。所以合并下半边 \(X(k+N/2)\) 时，\(A(k+N/2)\) 直接用 \(A(k)\) 代入。

---

### 4.4 DIF / DIT 迭代实现与 bit-reverse 倒序

#### 4.4.1 概念说明

递归的 Cooley-Tukey 在工程上通常改写成**迭代**（for 循环）形式，因为迭代更省栈空间、更适合硬件流水线。迭代有两种等价路线：

- **DIF（Decimation In Frequency，频率抽取）**：先做蝶形运算，把输出按「频率」拆成上下半边；**最后**对输出做 bit-reverse 倒序。
- **DIT（Decimation In Time，时间抽取）**：**先**对输入做 bit-reverse 倒序，再做蝶形运算。

两者结果完全相同，区别只是「倒序在前还是在后」。本硬件项目采用 **DIF** 路线——这也解释了为什么流水线最后输出的频谱是 bit-reversed 顺序，需要在输出端再做一次重排（参见后续 u5-l4 讲义对「倒序缺失」的讨论）。

**bit-reverse 倒序**是什么？经过逐级分治后，元素的下标（用二进制表示）会被打散。比如 8 点的下标 \(0\sim7\) 写成 3 位二进制 \(000\sim111\)，把每个下标的二进制位**反过来读**，就得到它在分治树里的实际位置：

| 原下标 | 二进制 | 倒序二进制 | 倒序下标 |
| --- | --- | --- | --- |
| 0 | 000 | 000 | 0 |
| 1 | 001 | 100 | 4 |
| 2 | 010 | 010 | 2 |
| 3 | 011 | 110 | 6 |
| 4 | 100 | 001 | 1 |
| 5 | 101 | 101 | 5 |
| 6 | 110 | 011 | 3 |
| 7 | 111 | 111 | 7 |

所以 8 点的倒序顺序是 `[0 4 2 6 1 5 3 7]`。MATLAB 用 `bitrevorder` 函数一步完成这个映射。

#### 4.4.2 核心流程

**DIF 迭代流程**（对应 `FFT_iterative_DIF.m`）：

```
输入 x (自然顺序)
for level = 0 .. log2(N)-1:
    len = 2^(log2(N) - level)   # 当前每段的长度，从 N 逐级减半到 2
    mid = len / 2
    Wn  = exp(-j*2π/len)
    对每一段、每个 k in [0, mid-1]:
        A = x(k)     + x(k+mid)         # 上半边
        B = (x(k)    - x(k+mid)) * Wn^k # 下半边（先差后乘旋转因子）
        x(k)     = A
        x(k+mid) = B
    保存本级的中间结果 X_FFT_middle_result
对输出做 bit-reverse 得到自然顺序 X
```

注意 DIF 蝶形的特征是 **「先加减、后乘旋转因子」**（差值 \(x(k)-x(k+mid)\) 再乘 \(W_n^k\)）。这正好对应硬件蝶形单元 `butterfly.v` 的「先做加减、再把下支送去和旋转因子相乘」的结构。

**DIT 迭代流程**（对应 `FFT_iterative_DIT.m`）：

```
输入 x
先对 x 做 bit-reverse 得到 x_bit_reversed
for level = 1 .. log2(N):
    len = 2^level             # 当前每段长度，从 2 逐级翻倍到 N
    mid = len / 2
    Wn  = exp(-j*2π/len)
    对每一段、每个 k in [0, mid-1]:
        A = x_bit_reversed(k)
        B = x_bit_reversed(k+mid) * Wn^k   # 先乘旋转因子
        x_bit_reversed(k)     = A + B      # 先乘后加减
        x_bit_reversed(k+mid) = A - B
输出 X = x_bit_reversed (已是自然顺序)
```

DIT 蝶形的特征是 **「先乘旋转因子、后加减」**，且 `len` 从 2 向上增长（与 DIF 的从 N 向下相反）。两条路线一前一后做倒序，互为镜像。

#### 4.4.3 源码精读

DIF 脚本的核心蝶形循环（**本讲两个最小模块之一**）：

[matlab/FFT_iterative_DIF.m:L28-L45](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/matlab/FFT_iterative_DIF.m#L28-L45) —— `levels=log2(N)` 定总级数；外层 `for level` 控制当前段长 `len`（从 \(N\) 逐级减半）；内层算 `A=x(pos+k)+x(pos+k+mid)` 与 `B=(x(pos+k)-x(pos+k+mid))*Wn^k`，正是 DIF 蝶形「先加减、后乘旋转因子」。每级结束后把 `x` 存入 `X_FFT_middle_result`，留作与硬件各级输出逐拍比对的黄金参考。

[matlab/FFT_iterative_DIF.m:L47-L52](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/matlab/FFT_iterative_DIF.m#L47-L52) —— 蝶形算完后，用 `bitrevorder` 把下标倒序，再 `X_FFT(reversed_index+1)=x` 重排成自然顺序。注意 MATLAB 数组下标从 1 开始，所以 `+1`。

DIT 脚本与之镜像对照：

[matlab/FFT_iterative_DIT.m:L31-L34](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/matlab/FFT_iterative_DIT.m#L31-L34) —— DIT **先**对输入做 `bitrevorder`，得到 `x_bit_reversed`，注释提醒「MATLAB 数组下标从 1 开始」。

[matlab/FFT_iterative_DIT.m:L41-L55](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/matlab/FFT_iterative_DIT.m#L41-L55) —— DIT 蝶形 `A=x(k)`、`B=x(k+mid)*Wn^k`，然后 `x(k)=A+B`、`x(k+mid)=A-B`，即「先乘旋转因子、后加减」；`len=2^level` 从 2 向上增长。

[matlab/FFT_iterative_DIT.m:L65-L71](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/matlab/FFT_iterative_DIT.m#L65-L71) —— 把迭代结果与 MATLAB 内置 `fft(x)` 做误差校验，方差大于 1 就报错。这是「我的 FFT 实现是否正确」的回归测试。

bit-reverse 的原理与一种 C 语言位运算实现，文档里也有记录：

[scheme/FFT.md:L104-L142](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/scheme/FFT.md#L104-L142) —— bit-reverse 原理（末位 0 表偶数、1 表奇数，逐级分解形成倒序）与一段按位交换的 C 实现，可用作硬件输出倒序模块的算法参考。

#### 4.4.4 代码实践

**目标**：跑通 DIF 迭代，并用 4 点小例子的中间结果对照手算。

**步骤**：

1. 打开 `matlab/FFT_iterative_DIF.m`，它默认 \(N=1024\) 并生成含噪正弦信号。为了对照 4.3 节的手算，把开头改成最小例子（属于示例代码，原脚本里第 6 行就有被注释掉的 `x = [1 2 -3 -1]`，可参考）：
   ```matlab
   N = 4;
   x = [1 2 -3 -1];
   levels = log2(N);                 % = 2
   X_FFT_middle_result = zeros(levels+1, N);
   X_FFT_middle_result(1,:) = x;
   for level = 0:levels-1
       len = 2^(levels-level);       % level 0 -> 4, level 1 -> 2
       mid = len/2;
       Wn = exp(-1j*2*pi/len);
       for pos = 1:len:N
           for k = 0:mid-1
               A = x(pos+k) + x(pos+k+mid);
               B = (x(pos+k) - x(pos+k+mid))*Wn^(k);
               x(pos+k)     = A;
               x(pos+k+mid) = B;
           end
       end
       X_FFT_middle_result(level+2,:) = x;
   end
   index = 0:N-1;
   reversed_index = bitrevorder(index);
   X_FFT(reversed_index+1) = x;
   disp(X_FFT);                      % 应为 [-1, 4-3j, -3, 4+3j]
   disp(X_FFT_middle_result);        % 看每一级中间结果
   ```
2. 把 `X_FFT_middle_result` 打印出来，逐级核对：
   - 初值：`[1 2 -3 -1]`
   - level 0（len=4）：算完后应为 `[-2 1 4 -3j]`
   - level 1（len=2）：算完后应为 `[-1 -3 4-3j 4+3j]`
   - bit-reverse 后：`[-1 4-3j -3 4+3j]`

**需要观察的现象**：蝶形阶段输出的 `[-1 -3 4-3j 4+3j]` 与最终结果 `[-1 4-3j -3 4+3j]` 相比，第 2、3 个元素被互换了——这正是 bit-reverse 倒序在起作用。

**预期结果**：`X_FFT` 与 4.3 节手算、与 `fft([1 2 -3 -1])` 完全一致，均为 \([-1,\ 4-3j,\ -3,\ 4+3j]\)。

> 若本地无 MATLAB，可用 Octave 替代；若都没有，上述中间结果已逐级列出，可作为「源码阅读型实践」直接对照推导。

#### 4.4.5 小练习与答案

**练习 1**：DIF 和 DIT 各在哪一步做 bit-reverse？为什么位置相反？

**答案**：DIF 在**最后**对输出倒序，DIT 在**最前**对输入倒序。位置相反是因为两者的分治方向相反——DIF 从大段往小段切（输出端被打散成倒序），DIT 从小段往大段合（输入端需先排成倒序）。两者最终都得到自然顺序的频谱。

**练习 2**：本硬件流水线输出的是倒序的频谱，结合本节内容，这说明它走的是 DIF 还是 DIT？

**答案**：是 **DIF**。因为 DIF 的蝶形在前、倒序在后，而本硬件只在输出端需要倒序（即蝶形算完直接输出，倒序留作后处理），这正是 DIF 的特征。

**练习 3**：`bitrevorder([0 1 2 3 4 5 6 7])` 的结果是？

**答案**：`[0 4 2 6 1 5 3 7]`（见 4.4.1 节的 8 点倒序表）。

---

## 5. 综合实践

本实践把本讲四个模块串起来：手算、跑脚本、画图，三管齐下。

**任务 A：手算 4 点 DFT 并与脚本互相验证（承接 4.3、4.4）**

1. 用 4.3 节的 Cooley-Tukey 分治，手算 \(x=[1\ 2\ -3\ -1]\) 的 4 点 DFT，得到 \(X=[-1,\ 4-3j,\ -3,\ 4+3j]\)。
2. 按 4.4.4 节修改并运行 `FFT_iterative_DIF.m`，确认脚本输出的 `X_FFT` 与手算一致。
3. 再调 `DFT_original([1 2 -3 -1]')` 与 `fft([1 2 -3 -1])`，确认三种方法（定义式笨办法、迭代 DIF、内置 fft）结果完全相同。
4. 把 4.4.4 节列出的每一级 `X_FFT_middle_result` 逐行核对，确保你能解释「为什么 level 1 结束后第 2、3 个元素需要被倒序互换」。

**任务 B：画一张 8 点 Cooley-Tukey 逐级拆分图（承接 4.3）**

用纸笔或画图工具，画出 8 点 DIF 的三级蝶形流图，要求：

1. 输入端写自然顺序下标 `0 1 2 3 4 5 6 7`。
2. 第 1 级（len=8, mid=4）：把每对相距 4 的元素 \((0,4),(1,5),(2,6),(3,7)\) 连成一个蝶形，上支是「加」、下支是「差再乘 \(W_8^k\)」（\(k=0,1,2,3\)）。
3. 第 2 级（len=4, mid=2）：对相邻的 4 元素组各做蝶形，旋转因子是 \(W_4^k\)。
4. 第 3 级（len=2, mid=1）：相邻两两蝶形，旋转因子是 \(W_2^0=1\)。
5. 输出端标注得到的下标顺序（应为倒序 `[0 4 2 6 1 5 3 7]`），并在图旁注明「最后一步 bit-reverse 还原成自然顺序」。

画完后，对照 `scheme/FFT.md` 第 168 行的总结「以 8 点 FFT 为例，最终求得 FFT 结果为 \([X_e+WX_o,\ X_e-WX_o]\)」，确认你的流图里每个蝶形都符合 \(A\pm W B\) 的形式。

> 若无法确定运行结果，明确写「待本地验证」；流图本身属于纯算法推导，可独立完成。

---

## 6. 本讲小结

- DFT 的定义是 \(X(k)=\sum_{n=0}^{N-1}x(n)W_N^{nk}\)，旋转因子 \(W_N=e^{-j2\pi/N}\)；直接按定义算是 \(O(N^2)\) 复数乘法。
- 旋转因子有两条关键性质：**周期性** \(W_N^N=1\) 和**共轭对称性** \(X(N-k)=X(k)^*\)，后者解释了实信号频谱的镜像对称。
- Cooley-Tukey 把 \(N\) 点 DFT 拆成偶/奇两个 \(N/2\) 点 DFT，用蝶形 \(X(k)=A(k)\pm W_N^k B(k)\) 合并，递归 \(\log_2 N\) 级，复杂度降到 \(O(N\log_2 N)\)。
- 蝶形运算就是「一对输入加减、配旋转因子、得到一对输出」，是 FFT 也是本硬件流水线最基本的一级运算。
- 迭代 FFT 有 DIF（先蝶形后倒序）和 DIT（先倒序后蝶形）两条等价路线；本项目走 **DIF**，所以输出是 bit-reversed 顺序。
- bit-reverse 倒序把下标二进制位反转，DIF 放最后、DIT 放最前；这是后续理解「硬件输出需要重排」的算法根源。

---

## 7. 下一步学习建议

算法地基打好后，建议按以下顺序继续：

1. **u1-l4 数据流总览**：把本讲的「逐级蝶形」对应到硬件——看 `fft_top.v` 如何把 14 级 `fft_*` 模块首尾串成一条流水线，每级就是一个 Cooley-Tukey 层。
2. **u2-l1 蝶形运算单元**：精读 `butterfly.v`，看 DIF 蝶形「上支求和 \(A+C\)、下支求差 \(C-A\)」如何用 Verilog 的加减法器和打拍寄存器实现——你会发现它和本讲 4.4 节的 `A=x(k)+x(k+mid)`、`B=x(k)-x(k+mid)` 一一对应。
3. **u2-l3 定点与旋转因子量化**：本讲的旋转因子 \(W_N\) 是模长 1 的复数，硬件里用定点数（放大 \(2^{16}\)）来表示，那一讲会讲清楚 `46341` 怎么来的。
4. **u5-l1 MATLAB 黄金参考**：深入学习 `X_FFT_middle_result` 如何逐级保存中间结果，用于和硬件各级输出逐拍比对——本讲 4.4.4 节已经为你预演了这个比对思路。
