# pyfft：分阶段的 DIT 参考实现

## 1. 本讲目标

u4-l1 解决了「Python 和 Verilog 之间数据怎么传」的问题，但留下了另一个更根本的问题：**我们怎么知道 `dit` 算出来的数是对的？** u4-l3 会用 `numpy.fft.fft` 当「最终答案」做逐点比对，但那只能查「最后一步」对不对——如果错了，你并不知道是哪一级（stage）开始算错的。

本讲的 `pyfft.py` 正是用来填补这个空白的「金标准（golden model）」。它用纯 Python 把 DIT（按时域抽取）FFT 的**每一级中间结果**都算出来，让你能像剥洋葱一样，把 `dit` 硬件每一级缓冲里的数和「正确答案」逐级对照，精准定位错误级别。它只有 46 行，却是整个项目调试链路里最关键的诊断工具。

学完本讲，你应当能够：

1. 写出 DIT 分解的核心等式 \(X_k = E_k \pm W_N^k O_k\)，并说明它**同时**写在 `dit.v` 顶部注释和 `pyfft.py` 的 `fftstages` 里——两边是同一个公式的两种表达。
2. 读懂 `fftstages` 的递归结构：它如何用 `cs[::2]`/`cs[1::2]` 把序列拆成偶/奇子序列、如何用 `chain(*zip(...))` 把子结果交错拼回、以及返回的 `stages` 列表里每一级到底是什么。
3. 建立起 **Python 参考模型与 Verilog 各级之间逐点对应**的关系：对 \(N=8\)，能说出 `pyfft` 的 `stage[i]` 对应 `dit` 跑完第 `i` 级蝶形后缓冲里的内容，并理解硬件为防溢出而引入的「除以 \(2^i\)」定标差异。

本讲只聚焦「参考模型的数学与结构」以及「它如何映射到硬件各级」，不展开定点量化的整数编解码（u2-l1 / u4-l3）和地址位运算推导（u3-l3）。

> 阅读提醒：`pyfft.py` 与整个项目的 Python 代码都是 **Python 2** 写的（`qa_dit.py:17` 处 `from pyfft import fftstages` 当前被注释掉，说明它平日是手动按需导入用于调试的）。直接在 Python 3 下运行会因 `/` 整数除法变成浮点而在列表下标处报错，实践环节会给出改法，但能否在你本地跑通**待本地验证**。

---

## 2. 前置知识

进入源码前，先把三个概念讲清楚。本讲假设你已读过 u2-l1（旋转因子与定点）和 u3-l3（地址计算）。

### 2.1 DIT（Decimation In Time，按时域抽取）回顾

DFT 把长度为 \(N\) 的序列 \(x_n\) 变成 \(X_k\)。直接算是 \(O(N^2)\)，FFT 靠「分治」把它降到 \(O(N\log N)\)。**DIT** 是分治的一种切入角度：按下标的**奇偶**把序列拆成两半——偶数子序列 \(e_m=x_{2m}\) 与奇数子序列 \(o_m=x_{2m+1}\)，先分别求它们的 DFT（记为 \(E_k\) 和 \(O_k\)，长度都是 \(N/2\)），再用一条简单公式把它们「合并」成完整的 \(X_k\)。这条合并公式就是本讲的数学主角（见 4.1）。「抽取（decimation）」就是指这个「按下标抽走一半」的动作。

### 2.2 什么是「参考模型 / 金标准」

硬件验证里有个常见套路：先用一门你**信得过**的语言（这里是用浮点复数算的 Python）把同一件事算一遍，得到「标准答案」，再拿硬件的输出和它比。这个标准答案就叫**参考模型（reference model）**或**金标准（golden model）**。它的价值在于：Python 浮点没有定点溢出、没有位宽截断，可以认为是「数学上精确的」，所以一旦硬件对不上参考模型，锅一定在硬件那边。`pyfft.py` 顶部的注释把它定位得很直白：

> [pyfft.py:4-6](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/pyfft.py#L4-L6) — 「Gives expected results of FFT DIT stages to compare with verilog code.」（给出 FFT DIT 各级的期望结果，用于和 verilog 代码对照。）

README 里也强调它的用途是调试：

> [README.txt:17](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/README.txt#L17) — 「pyfft.py - Generates output of intermediate FFT stages. Useful for debugging.」（生成 FFT 中间各级的输出，对调试很有用。）

### 2.3 递归与「级（stage）」

`fftstages` 是一个**递归**函数：求长度 \(N\) 的 FFT 时，它先递归地求两个长度 \(N/2\) 的子 FFT，再合并。每一次「合并」就对应硬件里的一**级**蝶形运算。对 \(N=2^{\text{NLOG2}}\)，一共要合并 NLOG2 次；`fftstages` 会把这 NLOG2 次合并（外加最初输入）逐级记录下来，返回 NLOG2+1 个数组。理解「递归 = 分级」是读懂本讲的关键。

> 一句话总结：`pyfft.py` 是用 Python 浮点复数实现的、与 `dit.v` 同一套 DIT 数学的参考模型；它把每一次合并单独存成一「级」，让你能把硬件每一级缓冲里的数和精确答案逐级比对。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [pyfft.py](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/pyfft.py) | 本讲第一主角。只有一个函数 `fftstages`，递归实现 DIT，返回每一级中间结果。仅 46 行。 |
| [dit.v](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v) | 本讲第二主角。重点读它**顶部的数学注释**（第 190-233 行）——那是 `fftstages` 公式的硬件侧「同款表述」。 |
| [qa_dit.py](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py) | 只看第 17 行那行被注释的 `from pyfft import fftstages`，理解参考模型在测试里「按需手动导入」的角色。 |

> 注意：本讲**不修改任何源码**，只阅读它们。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块推进：先讲 DIT 合并的数学等式（它把 `dit.v` 注释和 `pyfft` 的 `fs` 块统一起来），再逐行精读 `fftstages` 的递归与逐级输出，最后建立 Python 各级与 Verilog 各级的逐点对应关系。

### 4.1 DIT 的核心等式：从 dit.v 注释到 pyfft 的 fs

#### 4.1.1 概念说明

DIT 之所以能把 \(O(N^2)\) 降到 \(O(N\log N)\)，靠的就是下面这条「合并等式」。设 \(E_k=\text{DFT}(e_m)\)、\(O_k=\text{DFT}(o_m)\) 分别是偶、奇子序列（长度 \(N/2\)）的 DFT，\(W_N^k = e^{-2\pi i k/N}\) 是旋转因子（u2-l1 已讲过它的定点量化），那么完整序列的 DFT \(X_k\) 可以写成：

\[
X_k = E_k + W_N^k\, O_k, \qquad k = 0,1,\ldots,N/2-1
\]

\[
X_{k+N/2} = E_k - W_N^k\, O_k, \qquad k = 0,1,\ldots,N/2-1
\]

直观地说：\(X\) 的前半段是「\(E\) 加上旋转过的 \(O\)」，后半段是「\(E\) 减去旋转过的 \(O\)」。后半段的「减号」其实来自旋转因子的周期性——\(W_N^{k+N/2} = e^{-2\pi i (k+N/2)/N} = e^{-\pi i} W_N^k = -W_N^k\)，所以同一个 \(E_k,O_k\) 配对，到后半段时旋转因子自带一个负号。

这条等式是整个项目的「数学宪法」：`dit.v` 顶部注释把它写成数学符号，`pyfft.py` 的 `fs` 块把它写成 Python 代码，`butterfly.v` 则把它做成硬件（u2-l2 的 \(Y_A=X_A+W\cdot X_B\)、\(Y_B=X_A-W\cdot X_B\) 就是同一条等式的一次合并）。三处说的是同一件事。

#### 4.1.2 核心流程

把上面的等式摊成一次合并的步骤：

1. **抽取**：把输入 \(x_n\) 按下标拆成偶序列 \(e_m=x_{2m}\) 和奇序列 \(o_m=x_{2m+1}\)（对应 `pyfft` 的 `cs[::2]`、`cs[1::2]`）。
2. **递归求子 DFT**：分别算 \(E=\text{DFT}(e)\)、\(O=\text{DFT}(o)\)，长度各 \(N/2\)。
3. **配旋转因子**：对每个 \(k\)，取 \(W_N^k=e^{-2\pi i k/N}\)（对应 `pyfft` 的 `tf = cmath.exp(-2*cmath.pi*1j*k/N)`）。
4. **合并**：前半段 \(X_k = E_k + W_N^k O_k\)，后半段 \(X_{k+N/2} = E_k - W_N^k O_k\)（对应 `pyfft` 的 `fs` 循环里 `if k < N/2` 的两个分支）。

因为 \(E_k,O_k\) 本身又是更短序列的 DFT，所以步骤 1-4 可以**递归**套用，直到序列长度为 1（DFT 退化成它自己）。这就是 DIT 的全部精髓。

#### 4.1.3 源码精读

先看 `dit.v` 顶部注释怎么写这条等式——注意它和我们上面的公式**逐字符对应**：

> [dit.v:198-199](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L198-L199) — 写明 `for k<N/2 : X_k = E_k + exp(-2*pi*i*k/N)*O_k` 与 `for k>=N/2 : X_k = E_{k-N/2} - exp(-2*pi*{k-N/2}/N)*O_{k-N/2}`，正是前半段「加」、后半段「减」两条公式。

注释紧接着点明了「分级」的整体框架：

> [dit.v:200-202](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L200-L202) — 「We use this relationship to calculate the DFT ... in a series of stages. After the final stage the output is \(X_k\). After the second to last stage the output is an interleaving of \(E_k\) and \(O_k\).」即：最后一级产出 \(X_k\)，倒数第二级产出 \(E_k/O_k\) 的**交错排列**。这条结论是 4.3 节建立「逐级对应」的钥匙。

再看 `pyfft.py` 怎么用代码实现同一条等式。核心是 `fs` 循环：

```python
# pyfft.py:37-44 （fs 合并 = dit.v 注释的 X_k 公式）
fs = []
for k in range(0, len(cs)):
    tf = cmath.exp(-2*cmath.pi*1j*k/N)     # tf = W_N^k
    if k < len(cs)/2:                       # 前半段：X_k = E_k + W·O_k
        f = ess[-1][k] + tf*oss[-1][k]
    else:                                   # 后半段：复用同一条公式
        f = ess[-1][k-N/2] + tf*oss[-1][k-N/2]
    fs.append(f)
```

这里 `ess[-1]` 就是上一步算出的 \(E_k\)（偶子序列的完整 DFT），`oss[-1]` 就是 \(O_k\)。注意 `pyfft` 对**后半段**没有显式写减号，而是用了下标 `k-N/2` 配上 `tf=exp(-2πi k/N)`——这看起来和 `dit.v:199` 的「显式减号」不同，但代数上**完全等价**。验证如下：令 \(m=k-N/2\)（即 \(k=m+N/2\)），则

\[
\text{tf} = e^{-2\pi i (m+N/2)/N} = e^{-2\pi i m/N}\cdot e^{-\pi i} = -\,W_N^m
\]

于是后半段 `f = E_m + (-W_N^m)·O_m = E_m - W_N^m·O_m`，正好就是 `dit.v:199` 的后半段公式。**所以 `pyfft` 的单分支写法和 `dit.v` 注释的双公式写法是同一个数学。** 看懂这一点，就抓住了本讲最核心的对应关系。

#### 4.1.4 代码实践

**实践目标**：用一段独立的小程序验证 DIT 合并等式确实能还原出完整 DFT。

**操作步骤**：

1. 写一个短脚本（Python 2/3 皆可），对长度为 4 的复数序列 `xs` 手工执行一次合并：取 `E=numpy.fft.fft(xs[::2])`、`O=numpy.fft.fft(xs[1::2])`，再按下式拼出 `X`：

   ```python
   # 示例代码（非项目原文件）
   import numpy as np
   xs = np.array([1+0j, 2-1j, 0+1j, -1+0j])
   E = np.fft.fft(xs[::2]); O = np.fft.fft(xs[1::2])
   N = len(xs)
   X = np.zeros(N, dtype=complex)
   for k in range(N//2):
       W = np.exp(-2j*np.pi*k/N)
       X[k]      = E[k] + W*O[k]     # 前半段：加
       X[k+N//2] = E[k] - W*O[k]     # 后半段：减
   print(X)
   print(np.fft.fft(xs))
   ```

2. 把脚本算出的 `X` 与 `numpy.fft.fft(xs)` 并排打印。

**需要观察的现象**：两行输出逐点几乎相等（浮点误差在 \(10^{-15}\) 量级）。

**预期结果**：手工合并的 `X` 与 `numpy.fft.fft(xs)` 完全一致，证明 `dit.v:198-199` 那条等式是对的。这其实就是在重演 `pyfft.fs` 做的事。

#### 4.1.5 小练习与答案

**练习 1**：当 \(k=0\) 时，\(X_0\) 等于什么？为什么？

**参考答案**：\(W_N^0=1\)，故 \(X_0=E_0+O_0\)。而 \(E_0\) 是偶序列所有元素之和、\(O_0\) 是奇序列所有元素之和，加起来就是**全部 \(x_n\) 之和**——即 DFT 的直流（DC）分量。这也是 4.2 节脉冲例子里 `stage[-1][0]` 特别好算的原因。

**练习 2**：为什么后半段配对用的是同一个 \(E_k,O_k\)（下标 \(k\) 而不是 \(k+N/2\)）？

**参考答案**：因为 \(E,O\) 的长度只有 \(N/2\)，下标本就只到 \(N/2-1\)。后半段 \(X_{k+N/2}\) 复用前半段同一对 \(E_k,O_k\)，靠的是旋转因子的符号翻转 \(W_N^{k+N/2}=-W_N^k\) 来区分加/减，而不是换一对子序列。这正是「一个蝶形产出两个输出（\(X_k\) 与 \(X_{k+N/2}\)）」的由来。

---

### 4.2 fftstages 源码精读：递归、交错与逐级输出

#### 4.2.1 概念说明

4.1 节讲的是「一次合并」的公式。`fftstages` 把这次合并**递归**起来，并且做了一件额外的事：它不只返回最终结果，而是把**每一次合并的输出都单独存下来**，按顺序排成一个列表返回。这样调用者就能看到序列从「原始输入」一步步演化到「完整 FFT」的全过程，每一格都能和 `dit` 硬件对应的一级缓冲做比对。

要理解它的返回结构，关键是想清楚「级」是怎么累加出来的：顶层合并产生 1 个数组（最终 FFT）；它递归调用的两个子函数各自又返回了一串子级数组。顶层要做的是：把两边同位置的子级**交错（interleave）**拼成更长的数组，再把自己算出的最终数组接在最后。

#### 4.2.2 核心流程

`fftstages(cs)` 的执行流程（\(N=\)`len(cs)`）：

1. **校验**：若 \(N\) 不是 2 的幂，报错。
2. **递归基**：若 \(N=1\)，返回 `[cs]`——一个只含「自身」的级。
3. **抽取 + 递归**：`ess = fftstages(cs[::2])`（偶子序列各级），`oss = fftstages(cs[1::2])`（奇子序列各级）。
4. **交错拼接**：对 `ess`、`oss` 里每一对同位置的子级 `es`、`os`，用 `chain(*zip(es, os))` 把它们交错合并成一个长度翻倍的数组，作为本级列表的一项。
5. **最终合并**：按 4.1 的等式算出本层的 `fs`（即 \(X_k\)），接在列表末尾。
6. 返回这个列表。

关键结论：返回的列表长度是 \(\log_2 N + 1 = \text{NLOG2}+1\)。其中：

- `stages[0]` = 原始输入（自然序）。证明靠归纳：递归基是单元素；每一层把偶/奇两半的 `stages[0]` 交错，正好还原成上一层输入的自然序。
- `stages[-1]` = 完整 FFT \(X_k\)（与 `numpy.fft.fft` 一致）。
- `stages[-2]` = \(E_k\) 与 \(O_k\) 的**交错排列** \([E_0,O_0,E_1,O_1,\ldots]\)——正是 [dit.v:200-202](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L200-L202) 说的「倒数第二级是 \(E_k/O_k\) 的交错」。
- 一般地，`stages[i]` 是「做完 \(i\) 次合并」后的数组。

#### 4.2.3 源码精读

先看函数签名与文档串，它把「返回各级」的意图说得很清楚：

> [pyfft.py:13-25](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/pyfft.py#L13-L25) — `fftstages(cs)` 的文档串：「Returns a list of the output from FFT DIT stages ... Each list corresponds to the output from a FFT DIT stage. The final list ... should be the correct FFT of the input 'cs'.」（返回 FFT DIT 各级输出的列表；每个子列表对应一级，最后一个子列表应是输入的正确 FFT。）

依赖项很简单（注意 `nfft` 其实**未被使用**——参考模型有意只用 `cmath.exp` 手算，而不调用 numpy 的 FFT，从而成为一个独立可信的对照）：

> [pyfft.py:8-11](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/pyfft.py#L8-L11) — 导入 `cmath`、`math`、`from numpy import fft as nfft`（未用）、`from itertools import chain`。

校验与递归基：

```python
# pyfft.py:26-33
N = len(cs)
if math.log(N)/math.log(2) != int(math.log(N)/math.log(2)):
    raise ValueError("Length must be a power of 2")
if N == 1:
    return [cs]
ess = fftstages(cs[::2])    # 偶子序列各级（DIT 抽取）
oss = fftstages(cs[1::2])   # 奇子序列各级
```

`cs[::2]`/`cs[1::2]` 就是 2.1 节说的「按下标抽偶/奇」——DIT 的「抽取」动作在这两行里完成。

接着是「交错拼接」，这是本函数最巧妙、也最容易被忽略的一段：

```python
# pyfft.py:34-36
stages = []
for es, os in zip(ess, oss):
    stages.append(list(chain(*zip(es, os))))
```

`zip(es, os)` 把两个等长子级配成一对对 `(es[0],os[0]),(es[1],os[1]),...`；`chain(*...)` 把这些对子**摊平**成 `es[0],os[0],es[1],os[1],...`。效果就是把偶、奇两半的结果**交错**拼回一个长度翻倍的数组。这一步正是实现 `dit.v` 那种「\(k\cdot S+j\)」交错数据布局的 Python 版本（详见 4.3）。

最后是 4.1 已剖析过的 `fs` 合并（[pyfft.py:37-44](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/pyfft.py#L37-L44)），算完接在列表末尾并返回：

```python
# pyfft.py:45-46
stages.append(fs)
return stages
```

#### 4.2.4 代码实践

**实践目标**：跑通 `fftstages`，直观看到 NLOG2+1 级数组的演化。

**操作步骤**：

1. 在项目根目录建一个临时脚本（**示例代码**，非项目原文件），导入并调用：

   ```python
   # 示例代码；在 Python 3 下需先把 pyfft.py 里的 `/` 改成 `//`（见下方说明）
   from pyfft import fftstages
   import numpy as np
   x = [1, 0, 0, 0, 0, 0, 0, 0]          # 单位脉冲
   st = fftstages(x)
   print('共', len(st), '级 (期望 log2(N)+1 = 4)')
   for i, s in enumerate(st):
       print('stage', i, ':', [round(v.real, 4)+round(v.imag, 4)*1j for v in s])
   ```

2. 对照检查三个性质：`st[0] == x`、`st[-1] == numpy.fft.fft(x)`、`st[-2]` 等于 \(E,O\) 的交错。

**需要观察的现象**：打印出 4 个长度为 8 的数组，数值只有 \(0\) 和 \(1\)。

**预期结果**（这是「脉冲输入」的精确答案，可逐点核对）：

```
stage 0 : [1+0j, 0j, 0j, 0j, 0j, 0j, 0j, 0j]     ← 原始输入
stage 1 : [1+0j, 0j, 0j, 0j, 1+0j, 0j, 0j, 0j]    ← 做完第 1 级合并
stage 2 : [1+0j, 0j, 1+0j, 0j, 1+0j, 0j, 1+0j, 0j] ← E_k/O_k 交错
stage 3 : [1+0j, 1+0j, 1+0j, 1+0j, 1+0j, 1+0j, 1+0j, 1+0j] ← 完整 FFT
```

> **Python 3 兼容提示**：`pyfft.py` 用了 `len(cs)/2` 与 `k-N/2` 作为列表下标（[pyfft.py:40,43](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/pyfft.py#L40-L43)）。在 Python 2 里 `/` 对整数是整除，没问题；但在 Python 3 里 `/` 是真除法（得浮点），会导致 `ess[-1][k-N/2]` 因浮点下标而 `TypeError`。本地若是 Python 3，请把这两处的 `/` 临时改成 `//`（**在副本上改，不要动原文件**）。能否在你本地直接跑通**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么返回的级数是 NLOG2+1，而不是 NLOG2？

**参考答案**：因为列表里**包含了原始输入**作为 `stages[0]`（递归基 `N==1` 返回 `[cs]`，之后每一层只是把它交错拼长），之后每一次合并追加一级。\(N=2^{\text{NLOG2}}\) 共合并 NLOG2 次，所以总级数 = 1（输入）+ NLOG2（合并）= NLOG2+1。

**练习 2**：把 `chain(*zip(es, os))` 换成 `es + os`（直接拼接而不是交错），`stages[-1]` 还会等于正确的 FFT 吗？

**参考答案**：不会。`es+os` 是「先全部偶、再全部奇」，破坏了 `dit.v` 注释要求的「\(E_k\) 与 \(O_k\) 交错排列」布局。虽然最终合并 `fs` 仍按公式算，但**中间各级**的数据位置会错乱，导致「逐级对应硬件缓冲」失效；而且递归上传的 `stages[-1]`（来自更深层）其元素位置也会错。`zip+chain` 的交错正是为了和硬件的 `k*S+j` 地址布局对齐（见 4.3）。

---

### 4.3 从参考模型到硬件：pyfft 各级与 dit 各级的逐点对应

#### 4.3.1 概念说明

4.2 讲清了 `fftstages` 返回什么。本节回答最实用的问题：**这些级，和 `dit.v` 硬件跑到哪一步时缓冲里的内容，是怎么对应的？** 答案令人愉快：**逐点（position-by-position）对应**——`pyfft.stages[i]` 的第 \(n\) 个元素，恰好等于 `dit` 做完第 \(i\) 级蝶形后，工作缓冲第 \(n\) 号槽位的值（仅差一个防溢出定标）。这意味着 `pyfft` 不只能验最终结果，而是真正的「逐级金标准」。

之所以能逐点对齐，是因为两边用了**同一种数据布局**：`dit.v` 把第 \(i\) 级的结果按 \(k\cdot S+j\) 的地址交错存放（u3-l3 详述），而 `pyfft` 的 `chain(*zip(es,os))` 交错出来的正是同一种排列。所以 `pyfft` 才敢在文件头宣称自己是「to compare with verilog code」。

#### 4.3.2 核心流程

先建立级数对应。`dit.v` 的状态机从 `S=N/2` 开始，每级把 `S` 右移一位，直到 `S=1`（最后一级），共 NLOG2 级蝶形：

> [dit.v:336](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L336) — `INIT` 态把 `S` 置为 `N/2`（第一级的 series 数）。
> [dit.v:389](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L389) — 每级结束时 `S <= S >> 1`（series 数减半）。
> [dit.v:277](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L277) — `last_stage = (S == 1)`，最后一级写 `bufferout`。

于是对 \(N=8\)（NLOG2=3）有下面的对应表：

| pyfft 的级 | dit.v 状态 | 缓冲内容（\(N=8\)，自然序下标） |
| --- | --- | --- |
| `stages[0]` | 输入刚写满 `bufferin` | 原始 \(x_0\ldots x_7\) |
| `stages[1]` | 第 1 级（\(S=4\)）蝶形算完 | 2 点 DFT 的交错排列 |
| `stages[2]` | 第 2 级（\(S=2\)）蝶形算完 | \(E_k/O_k\) 交错（4 点子 DFT） |
| `stages[3]` | 第 3 级（\(S=1\)）蝶形算完，写入 `bufferout` | 完整 \(X_0\ldots X_7\) |

即：**`pyfft.stages[i]` ↔ `dit` 做完第 \(i\) 级后的缓冲**（`stages[0]` 对应「0 级 done」即输入）。

再说定标差异。硬件为防溢出，**每级蝶形输出都右移一位**（`butterfly.v` 里的 `>>>1`，见 u2-l3），所以做完 \(i\) 级后，值被整体除以 \(2^i\)；做完全部 NLOG2 级，除以 \(2^{\text{NLOG2}}=N\)。而 `pyfft` 是无定标的精确浮点。`dit.v` 顶部注释明确写了这个总定标：

> [dit.v:5](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L5) — 「The produced FFT is scaled down by a factor of N to prevent overflow.」（产出的 FFT 被缩小 \(N\) 倍以防溢出。）

于是比对规则是：

\[
\text{dit 做完第 } i \text{ 级的缓冲} \;\times\; 2^i \;=\; \texttt{pyfft.stages[i]}
\]

\[
\text{dit 最终输出} \;\times\; N \;=\; \texttt{pyfft.stages[-1]} \;=\; \text{numpy.fft.fft}(x)
\]

最后一条正是 u4-l3 的 `test_basic` 把硬件输出「除以 N」再和 `numpy.fft.fft` 比对的依据（u4-l3 会详述）。

最后看通用级的旋转因子。`dit.v` 注释把任意一级的合并写成（\(T_n=e^{-2\pi i n/M}\)，\(M=N\cdot S\)）：

> [dit.v:215-219](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L215-L219) — `P_{k*S+j} = Q_{2*k*S+j} + T_{k*S} * Q_{k*2*S+S+j}` 与减号版本。把 \(T_{kS}=e^{-2\pi i\,kS/(NS)}=e^{-2\pi i\,k/N}=W_N^k\) 代入，正是 4.1 那条等式——说明**每一级用的旋转因子都是 \(W_N^k\)**，这与 u3-l3 推出的 `tf_addr=kS` 查表、u2-l1 只需生成 \(N/2\) 个旋转因子完全自洽。

#### 4.3.3 源码精读

为什么「逐点对应」成立？关键是 `dit` 最后一级把结果写进 `bufferout` 的顺序。在最后一级（\(S=1\)），`series_bits` 已被右移成全 0，u3-l3 推出此时 `in0_addr=2k`、`in1_addr=2k+1`、`tf_addr=k`、`out0_addr=k`、`out1_addr=k+N/2`：读相邻的 `E_k,O_k` 对、配 \(W_N^k\)、把 \(X_k\) 写到 `bufferout[k]`、把 \(X_{k+N/2}` 写到 `bufferout[k+N/2]`。再加上输出进程按下标 \(0,1,\ldots,N-1\) 顺序读 `bufferout`：

```verilog
// dit.v:164-169 （输出进程顺序读 bufferout）
if (bufferout_full) begin
    out_x <= bufferout[bufferout_addr];
    out_nd <= 1'b1;
    bufferout_addr <= bufferout_addr + 1;
    ...
end
```

所以 `dit` 最终吐出的序列就是 \(X_0,X_1,\ldots,X_{N-1}\) 的**自然序**，与 `pyfft.stages[-1]`、与 `numpy.fft.fft` 在位置上**完全一致**（仅差 \(N\) 倍定标）。这就是为什么最终结果的比对可以是「逐点」的、不需要重排。

#### 4.3.4 代码实践

**实践目标**：用 4.2.4 的脉冲结果，亲手验证「`pyfft.stages[i]` ↔ `dit` 第 \(i\) 级缓冲」的对应，并解释偶/奇子序列如何合并为下一级。

**操作步骤**：

1. 拿 4.2.4 的四级输出，对照下表逐点填入 `dit` 各级缓冲「应该是什么」（脉冲输入 \(x=[1,0,0,0,0,0,0,0]\)，偶序列 \(e=[1,0,0,0]\)、奇序列 \(o=[0,0,0,0]\)）：

   | 级 | dit.v 缓冲（应与 pyfft 一致） | 由谁、怎么合并来 |
   | --- | --- | --- |
   | 0 | `[1,0,0,0,0,0,0,0]` | 原始输入 |
   | 1 | `[1,0,0,0,1,0,0,0]` | 相距 4 的样本做 2 点蝶形（\(W_8^0=1\)）：位置 \(j\) ← \(x_j+x_{j+4}\)，位置 \(j+4\) ← \(x_j-x_{j+4}\) |
   | 2 | `[1,0,1,0,1,0,1,0]` | 把第 1 级结果当 \(Q\)，按 `dit.v:218-219` 配 \(W_8^k\) 合并；本例奇路径为 0，故只剩偶路径 \(E_k\) 交错 |
   | 3 | `[1,1,1,1,1,1,1,1]` | \(X_k=E_k+W_8^k O_k\)；本例 \(O_k\equiv0\)，故 \(X_k=E_k\equiv1\) |

2. 针对**第 1 级**亲手核验两个蝶形：取 \(j=0\)，\(x_0=1,x_4=0\)，按 [dit.v:218-219](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L218-L219)（\(T_{kS}=W_8^0=1\)）得 \(P_0=x_0+x_4=1\)、\(P_{0+4}=x_0-x_4=1\)，与 `stages[1]` 的第 0、4 位一致。

3. 写一句话解释「偶/奇子序列如何合并为下一级」：参考 [dit.v:198-199](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L198-L199) 与 [pyfft.py:39-43](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/pyfft.py#L39-L43)。

**需要观察的现象**：表里每一格都和 4.2.4 打印的 `stage i` 逐位相同；第 1 级手算的两个值落在正确位置。

**预期结果**：`pyfft.stages[i]` 与「`dit` 第 \(i\) 级缓冲 ×\(2^i\)」逐点相等。脉冲例子里数值都是 0/1，定标不改变它们，所以连 \(2^i\) 都不必乘即可直接比对——这也是选脉冲做入门例子的好处。换成任意输入时，记得给硬件侧乘上 \(2^i\)（最终级乘 \(N\)）再比对。本步骤的具体数值能否在你本地复现**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：若 `dit` 第 2 级缓冲里读出某槽位为 \(v\)（已解码回浮点复数），对应的 `pyfft` 期望值是多少？

**参考答案**：第 2 级对应 `pyfft.stages[2]`，而定标因子是 \(2^i=2^2=4\)，故期望值 \(=v\times 4\)。一般地，第 \(i\) 级乘 \(2^i\)，最终级（\(i=\text{NLOG2}\)）乘 \(2^{\text{NLOG2}}=N\)。

**练习 2**：为什么脉冲输入的 `stages[2]` 是 `[1,0,1,0,1,0,1,0]` 而不是「8 个 1」？

**参考答案**：`stages[2]` 是 \(E_k/O_k\) 的**交错**——\([E_0,O_0,E_1,O_1,E_2,O_2,E_3,O_3]\)。脉冲的奇序列全 0，故 \(O\equiv[0,0,0,0]\)；偶序列 \([1,0,0,0]\) 的 4 点 DFT \(E\equiv[1,1,1,1]\)。交错后偶数位是 \(E_k=1\)、奇数位是 \(O_k=0\)，正是 `[1,0,1,0,1,0,1,0]`。要到 `stages[3]` 把 \(E,O\) 合并（\(O=0\) 所以 \(X=E\)）才得到 8 个 1。

---

## 5. 综合实践

把本讲的三块知识串起来，完成下面这个贯穿任务（即本讲指定的代码实践任务）。

**任务**：用 `pyfft.py` 的 `fftstages` 处理一个长度为 8 的复数序列，打印每一级输出，并对照 `dit.v` 顶部注释解释「偶/奇子序列如何合并为下一级」。

**步骤**：

1. **准备输入**：选一个**非平凡**（偶、奇两半都非零）的长度 8 复数序列，例如（**示例代码**）：

   ```python
   # 示例代码；Python 3 下先把 pyfft.py 的两处 / 改成 //（在副本上改）
   from pyfft import fftstages
   import numpy as np
   x = [0, 1, 2, 3, 4, 5, 6, 7]          # 简单实序列，偶/奇两半都非零
   evens, odds = x[::2], x[1::2]          # 偶 [0,2,4,6]、奇 [1,3,5,7]
   ```

2. **打印各级**：调用 `fftstages(x)`，逐级打印（用 `round` 抑制浮点毛刺）。

3. **解释合并**：挑最后一级 `stages[-1]`，对照 [dit.v:198-199](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L198-L199) 写出它的来历——`stages[-2]` 是 \(E_k/O_k\) 的交错，最后一级对每个 \(k\) 做 \(X_k=E_k+W_8^k O_k\)（前半段）与 \(X_{k+4}=E_k-W_8^k O_k\)（后半段），旋转因子 \(W_8^k=e^{-2\pi i k/8}\)。

4. **三向核对**：验证 `stages[-1]` 与 `numpy.fft.fft(x)` 逐点相等；验证 `stages[-2]` 等于 `interleave(numpy.fft.fft(evens), numpy.fft.fft(odds))`；验证 `stages[0] == x`。

5. **映射到硬件**：在打印结果旁标注每一级对应 `dit.v` 的哪一级蝶形（\(S=4,2,1\)），并写出「若要比对硬件缓冲，需给硬件侧乘 \(2^i\)」。

**需要观察的现象**：`stages[-1]` 与 `numpy.fft.fft` 完全吻合；`stages[-2]` 恰是偶/奇子 DFT 的交错；最后一级确实由 `stages[-2]` 经旋转因子加减合并而来。

**预期结果**：你应能写出类似这样的一段解释——「`stages[-2]` 把偶子序列 \(e=[0,2,4,6]\) 的 4 点 DFT \(E\) 和奇子序列 \(o=[1,3,5,7]\) 的 4 点 DFT \(O\) 交错成 \([E_0,O_0,\ldots,E_3,O_3]\)；最后一级按 `dit.v:198-199`，前半段 \(X_k=E_k+W_8^k O_k\)、后半段 \(X_{k+4}=E_k-W_8^k O_k\)，把这对 \(E,O\) 合并成完整 8 点 FFT。」具体打印数值能否在你本地复现**待本地验证**。

> 进阶：若本地有 iverilog+MyHDL 环境，可仿照 u4-l1/u4-l3 的方式把一组同样的 \(x\) 喂给 `dit`，抓出 `bufferX`/`bufferY` 在每级结束时的内容（用 `DEBUGMODE` 或波形），解码回复数后乘以 \(2^i\)，与 `pyfft.stages[i]` 逐点比对——这就是 `pyfft` 作为「逐级金标准」的完整用法。环境是否就绪**待本地验证**。

---

## 6. 本讲小结

- DIT 的核心等式 \(X_k=E_k+W_N^k O_k\)、\(X_{k+N/2}=E_k-W_N^k O_k\) 同时写在 [dit.v:198-199](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L198-L199) 的注释和 [pyfft.py:37-44](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/pyfft.py#L37-L44) 的 `fs` 块里；`pyfft` 后半段用「下标 \(k-N/2\) 配 \(\text{tf}=W_N^k\)」代替显式减号，代数上完全等价。
- `fftstages` 是递归 DIT：用 `cs[::2]`/`cs[1::2]` 抽取偶/奇子序列（[pyfft.py:32-33](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/pyfft.py#L32-L33)），用 `chain(*zip(es,os))` 把子级交错拼回（[pyfft.py:34-36](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/pyfft.py#L34-L36)），返回 NLOG2+1 个数组。
- 返回结构：`stages[0]`=原始输入、`stages[-1]`=完整 FFT（=`numpy.fft.fft`）、`stages[-2]`=\(E_k/O_k\) 交错——正是 [dit.v:200-202](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L200-L202) 说的「倒数第二级是 \(E/O\) 交错」。
- 与硬件逐点对应：`pyfft.stages[i]` 恰好等于 `dit` 做完第 \(i\) 级蝶形后缓冲里的内容（\(N=8\) 时各级对应 \(S=4,2,1\)），因为两边用了同一种 \(k\cdot S+j\) 交错布局——`pyfft` 因此是真正的「逐级金标准」。
- 定标差异：硬件每级右移一位（[dit.v:5](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L5) 缩小 \(N\) 倍），`pyfft` 无定标，故比对规则是「硬件第 \(i\) 级 ×\(2^i\) = `pyfft.stages[i]`」，最终级乘 \(N\)。
- 每一级用的旋转因子都是 \(W_N^k\)（由 [dit.v:215-219](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L215-L219) 的 \(T_{kS}=e^{-2\pi i k/N}\) 给出），这与 u3-l3 的 `tf_addr=kS`、u2-l1 只生成 \(N/2\) 个旋转因子自洽。

---

## 7. 下一步学习建议

至此你已掌握 `pyfft` 这套「逐级金标准」的数学与结构。接下来建议：

- **看懂测试判定**：u4-l3 会进到 [qa_dit.py](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/qa_dit.py) 的 `test_basic`，看它如何把 `dit` 的输出**除以 N** 后与 `numpy.fft.fft` 逐点 `assertAlmostEqual`——这正是本讲「最终级乘 \(N\) 才能比对」的工程落地，并分析定点量化误差容限。
- **把误差讲透**：本讲的 `pyfft` 是无定标浮点，硬件是定点 + 每级 `>>>1`。u4-l3 会解释 `c_to_int`/`int_to_c` 的编解码、位宽对精度的影响，以及为什么用 `assertAlmostEqual` 而非 `assertEqual`。
- **建议先精读的源码**：在进入 u4-l3 之前，回头对照 [dit.v:190-233](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/dit.v#L190-L233) 的整段数学注释与 [pyfft.py](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/pyfft.py) 全文，确认你能把注释里的 \(P_{kS+j}\)、\(Q\)、\(T_n\) 一一对应到 `fftstages` 的 `fs`、`ess[-1]`、`tf`——这是把「参考模型」和「实际测试比对」连起来的最后一块拼图。
