# 噪声生成与 phase_unwrap

## 1. 本讲目标

本讲讲解 psi_fix 中三类「无输入激励源」的辅助组件：均匀白噪声发生器 `psi_fix_white_noise`、高斯白噪声发生器 `psi_fix_noise_awgn`，以及相位解卷绕器 `psi_fix_phase_unwrap`。学完后你应该能够：

- 说清 LFSR（线性反馈移位寄存器）如何在 FPGA 上产生**确定性、可复现**的伪随机比特流，以及 psi_fix 如何「每位一条 LFSR」拼出定点随机数。
- 解释 `noise_awgn` 如何用「白噪声 + 反累积分布函数查表（gaussify）」把均匀分布映射成高斯分布，这是经典的逆变换采样（inverse transform sampling）思想。
- 画出 `phase_unwrap` 的四级流水（延时→差分→累加→舍入），并解释为何用 `psi_fix_in_range` 检测累加器溢出、溢出时为何「回退到原始输入样本」并拉高 `wrap_o`。
- 读懂这三个组件的协同仿真测试台，理解 `stimuli_set_g` 与刺激数据如何分别覆盖「有/无卷绕」两类场景。

本讲是「DDS、调制解调与噪声」单元的第二讲，承接 u9-l1 的位真双模型与两段式编码风格，把它们应用到不需要外部数据输入的「信号源类」组件上。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个直觉。

### 2.1 伪随机 vs 真随机：为什么 FPGA 喜欢 LFSR

真随机数需要物理噪声源（热噪声、抖动），成本高、不可复现。FPGA 里几乎一律用**伪随机**：一条确定的数学公式，从一个「种子」出发，产出一长串看似杂乱、实则完全确定的序列。它的最大好处是**可复现**——同一个种子永远得到同一条序列，这让仿真和上板结果完全一致，也让 Python 位真模型能逐位复现硬件输出。

LFSR（Linear Feedback Shift Register，线性反馈移位寄存器）是最常用的伪随机发生器：一个移位寄存器每拍左移一位，空出来的最低位用「寄存器中若干位的异或」填上。选对反馈抽头（taps）就能得到周期极长（\(2^{N}-1\)）的「最大长度」序列，统计上接近白噪声。

### 2.2 从均匀分布到高斯分布：逆变换采样

如果 \(U\) 服从 \([0,1]\) 上的均匀分布，\(F\) 是某个目标分布的累积分布函数（CDF），那么 \(F^{-1}(U)\) 就服从该目标分布——这叫**逆变换采样**。直观理解：CDF 把「概率」映射到「取值」，它的反函数自然就把「均匀洒开的概率」变成「按目标密度分布的取值」。

对高斯分布，\(F^{-1}\) 就是正态分布的**分位点函数**（percent-point function，`scipy.stats.norm.ppf`）。psi_fix 的 `gaussify` 组件正是用分段线性查表去逼近这条 \(F^{-1}\) 曲线（见 u8-l1/u8-l2 的线性近似内核）。

### 2.3 相位卷绕与解卷绕

很多算法（如锁相、瞬时测频）输出的是「被折叠进 \([-180°,+180°)\)」的相位。当真实相位连续增长越过 \(+180°\) 时， wraps 成 \(-180°\) 附近，波形上出现一个 \(-360°\) 的「跳变」——这叫**相位卷绕**（wrapping）。

**解卷绕**（unwrapping）的思路：只要每个样本的真实相位增量小于 \(180°\)，那么「当前样本 − 上一样本」在做完 \(360°\) 回绕后，就还原成了真实的小增量；把这些小增量累加起来，就得到连续的真实相位。但如果相位一直朝一个方向涨，累加值迟早会超出输出定点格式能表示的范围——这时只能「放弃累积、回到当前原始样本」并报警，这正是 `wrap_o` 的用途。

### 2.4 承接的前置概念

本讲默认你已经掌握（见前置讲义）：

- 定点格式三元组 \([s,i,f]\) 与位增长规则（u1-l4、u2-l1）。
- 两段式编码：组合进程 `p_comb` 写 `r_next`、时序进程 `p_seq` 仅打拍，用 `record` 封装流水线（u3-l3）。
- 位真双模型与协同仿真：Python 模型即黄金参考，`preScript.py` 用 `psi_fix_get_bits_as_int` 把定点值写成「位模式有符号整数」文本，测试台用 `###ERROR###` 逐位比对（u3-l2）。
- Manual Splitting：把舍入级与饱和级拆成两拍流水（u2-l2、u4-l1）。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [hdl/psi_fix_white_noise.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_white_noise.vhd) | 均匀白噪声发生器：N 条独立 LFSR 拼出 N 位定点随机数 |
| [hdl/psi_fix_noise_awgn.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_noise_awgn.vhd) | 高斯白噪声（AWGN）发生器：白噪声 → gaussify 查表 → 舍入/饱和 |
| [hdl/psi_fix_phase_unwrap.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_phase_unwrap.vhd) | 相位解卷绕器：差分-累加四级流水，溢出回退 + `wrap_o` 指示 |
| [model/psi_fix_noise_awgn.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_noise_awgn.py) | AWGN 的 Python 位真模型（黄金参考） |
| [model/psi_fix_white_noise.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_white_noise.py) | 白噪声的 Python 位真模型，逐位复现 LFSR |
| [model/psi_fix_phase_unwrap.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_phase_unwrap.py) | 相位解卷绕的 Python 位真模型 |
| [model/psi_fix_lin_approx.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py) | 线性近似内核与 gaussify 表的代码生成（u8-l2） |
| [testbench/psi_fix_white_noise_tb/Scripts/preScript.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_white_noise_tb/Scripts/preScript.py) | 白噪声协同仿真：生成 `output_S.txt`/`output_U.txt` |
| [testbench/psi_fix_phase_unwrap_tb/Scripts/preScript.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_phase_unwrap_tb/Scripts/preScript.py) | 解卷绕协同仿真：构造有/无卷绕刺激 |

---

## 4. 核心概念与源码讲解

### 4.1 白噪声生成（psi_fix_white_noise）

#### 4.1.1 概念说明

`psi_fix_white_noise` 产出格式为 `out_fmt_g` 的均匀白噪声。它的核心思想是「**输出定点数的每一位，各自由一条独立的 LFSR 驱动**」：若输出共 \(N\) 位，就用 \(N\) 条 32 位 LFSR，第 \(i\) 条的最低位 `Lfsr(i)(0)` 就是输出第 \(i\) 位的随机比特。因为各 LFSR 互相独立、各自的序列又近似白噪，拼出来的定点数在统计上接近均匀分布、频谱平坦（白色）。

三个关键设计取舍：

- **确定性 / 可复现**：种子 `seed_g` 是 generic，硬件上电与 Python 模型用同一颗种子，得到同一条序列——这是协同仿真能逐位比对的前提。
- **每位一条 LFSR 而非一条 LFSR 取多拍**：这样每个有效样本都能在一拍内并行产出全部 \(N\) 位，吞吐为 1 样本/时钟，代价是 \(N\) 个寄存器。
- **限制输出 ≤32 位**：因为有符号格式的最高位（符号位）也来自某条 LFSR，整个输出宽度受限于 LFSR 宽度，组件用断言强制 `out_fmt` 总位宽 ≤ 32。

文件头部的注释还点明：psi_common 库里也有一个更通用的 PRBS 发生器（可选反馈抽头权重），但**不带定点格式输出**；本组件专门补上「直接出定点」这一档。

#### 4.1.2 核心流程

单拍内组合进程做两件事（顺序很重要）：

1. **先用旧状态产生输出**：`OutData(i) := r.Lfsr(i)(0)`——输出取自更新前的 LFSR 最低位。
2. **再更新 LFSR**（仅当 `vld_i='1'`）：每条 LFSR 左移一位，最低位填入反馈比特
   \[
   b_{0}^{new} = b_{31} \oplus b_{20} \oplus b_{26} \oplus b_{25}
   \]
   抽头 \(\{31,20,26,25\}\) 是一条已知的 32 位最大长度多项式。

复位时，第 \(i\) 条 LFSR 被初始化为 `seed_g + 2^i`，于是 \(N\) 条 LFSR 起始状态各差一个移位量，保证各比特位互不相关。当 `out_fmt_g.S=1`（有符号）时，最高位天然充当符号位，输出落在 \([-1, +1)\) 类的有符号区间；`S=0`（无符号）时则是纯无符号随机数。

#### 4.1.3 源码精读

实体定义：默认输出格式 `(0,0,31)`（无符号 31 位），默认种子 `X"A38E3C1D"`，复位高有效。注意它**只有 `vld_i`、没有 `rdy`**——组件无条件产出，下游必须能接住（见 [hdl/psi_fix_white_noise.vhd:23-33](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_white_noise.vhd#L23-L33)）。

宽度断言，限制输出 ≤32 位（否则 LFSR 宽度不够驱动所有位）：[hdl/psi_fix_white_noise.vhd:55-62](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_white_noise.vhd#L55-L62)。

record 里关键的是 `Lfsr : t_aslv32(0 to OutBits_c - 1)`——一个「32 位 std_logic_vector 的数组」，长度等于输出位数：[hdl/psi_fix_white_noise.vhd:44-48](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_white_noise.vhd#L44-L48)。

组合进程——「先输出、后更新」的两段：[hdl/psi_fix_white_noise.vhd:73-85](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_white_noise.vhd#L73-L85)。这段同时体现了两段式风格里 `v := r` 的好处：未在 `vld_i='0'` 分支里显式赋值的 `OutData`，靠 `v := r` 自动保持上一拍值。

时序进程与复位——注意第 \(i\) 条 LFSR 的初值是 `seed_g + 2^i`（`shift_left(to_unsigned(1,32), i)` 即 \(2^i\)）：[hdl/psi_fix_white_noise.vhd:98-109](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_white_noise.vhd#L98-L109)。

Python 位真模型把同一套规则镜像过来——逐位生成、每位的 LFSR 用 `seed + (1<<bitNr)` 作种子，反馈抽头 `[31,20,26,25]` 与 VHDL 完全一致：[model/psi_fix_white_noise.py:33-61](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_white_noise.py#L33-L61)。有符号转换 `np.where(outVec >= 2**(outBits-1), outVec - 2**outBits, outVec)` 对应 VHDL 里「最高位 LFSR 当符号位」的语义。

#### 4.1.4 代码实践

**实践目标**：验证「同一颗种子 → 同一条序列」，并理解输出是如何按位拼装的。

**操作步骤**（源码阅读 + 可选运行）：

1. 打开 [model/psi_fix_white_noise.py:33-46](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_white_noise.py#L33-L46)，跟踪 `Generate(samples)`：外层 `for bitNr in range(self.outBits)` 对每一位调用 `_GenerateBit`，再把每位结果左移到对应权位累加。
2. 对照 [hdl/psi_fix_white_noise.vhd:73-85](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_white_noise.vhd#L73-L85)，确认两侧「第 i 位由第 i 条 LFSR 的 bit0 驱动」完全同构。
3.（可选运行）在仓库根目录，把 `model/` 加入 `sys.path` 后运行下面这段**示例代码**（非项目原有代码）：

   ```python
   from psi_fix_pkg import *
   from psi_fix_white_noise import psi_fix_white_noise
   import numpy as np
   fmt = psi_fix_fmt_t(1, 0, 7)          # 有符号 8 位
   gen = psi_fix_white_noise(fmt, seed=0xA38E3C1D)
   seq = gen.Generate(5)
   print(np.asarray(seq))                # 同一 seed 永远同一组值
   ```

**需要观察的现象**：两次运行（同一 seed）输出完全相同；改变 `seed` 后序列立刻不同。把 `PLOT_ON=True`（见 [testbench/.../preScript.py:41-51](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_white_noise_tb/Scripts/preScript.py#L41-L51)）还能看到时域杂乱、频谱近似平坦（「白」的由来）。

**预期结果**：确定性序列可复现；协同仿真测试台里 [preScript.py:34-36](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_white_noise_tb/Scripts/preScript.py#L34-L36) 生成 10000 个样本，VHDL 输出与 Python 黄金模型逐位一致，无 `###ERROR###`。若你无法本地运行 Python，相关数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为何第 \(i\) 条 LFSR 的初值要写成 `seed_g + 2^i`，而不是所有 LFSR 都用同一个 `seed_g`？
**答案**：若所有 LFSR 初值相同，它们会演化出完全相同的序列，于是输出定点数的每一位同步翻转，统计上不再是均匀白噪。加 \(2^i\) 让各 LFSR 起始状态错开，各比特位互不相关。

**练习 2**：组件为什么把输出宽度断言限制在 ≤32 位？
**答案**：每条 LFSR 宽 32 位，输出第 \(i\) 位取自第 \(i\) 条 LFSR 的 bit0。要驱动超过 32 位的输出就需要更宽/更多的 LFSR，当前实现未提供，故用断言拦截非法配置。

---

### 4.2 高斯噪声生成（psi_fix_noise_awgn）

#### 4.2.1 概念说明

`psi_fix_noise_awgn` 产出**高斯（正态）分布**的白噪声（AWGN = Additive White Gaussian Noise）。它不是另写一套随机数发生器，而是**复用 4.1 的白噪声做均匀源，再经 gaussify 查表把均匀分布映射成高斯分布**——这正是 §2.2 的逆变换采样。

数据通路三级：

1. `psi_fix_white_noise` 产出格式固定为 `IntFmt_c=(1,0,19)` 的均匀随机数（有符号，范围约 \([-1,+1)\)）。
2. `psi_fix_lin_approx_gaussify20b`（u8-l1/u8-l2 的线性近似内核）用一张分段线性表逼近正态分布的逆 CDF，把均匀输入变换成高斯输出（限幅在 \([-1,+1]\)）。
3. Manual Splitting 两级 resize：先舍入（round/wrap，多 1 位整数位做头空），再饱和（trunc/sat）到用户指定的 `out_fmt_g`。

因此 `noise_awgn` 的位真模型只是把这三个已有的 Python 模型串起来——**它没有自创任何算术**，黄金参考天然可信。

#### 4.2.2 核心流程

整体是「例化两个子组件 + 一个两拍 resize 流水」：

- 子组件 `i_white_noise`：均匀源，输出 `IntFmt_c=(1,0,19)`。
- 子组件 `i_gaussify`：逆 CDF 查表，输入输出同为 `(1,0,19)`。
- 组合进程两拍：
  - **Round 级**：`psi_fix_resize(Norm, IntFmt_c → RndFmt_c, round, wrap)`，其中 `RndFmt_c=(1,1,out_fmt_g.F)` 多一个整数位——注释写明「Cannot saturate by design」，因为舍入可能把 \(\pm 1.0\) 推到略超 \(\pm 1\)，必须留头空、不能在此饱和。
  - **Sat 级**：`psi_fix_resize(Rnd, RndFmt_c → out_fmt_g, trunc, sat)`，注释「Only saturation, rounding already done」——这是 Manual Splitting 的标准拆法：舍入与饱和分两拍，既提 Fmax 又保证数值正确。

逆 CDF 表本身在设计期生成（见 [model/psi_fix_lin_approx.py:65-67](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L65-L67)）：

\[
\texttt{GAUSSIFY\_TABLE} = \mathrm{clip}_{[-1,1]}\!\left(\frac{\texttt{norm.ppf}(\text{linspace}(0.001,0.999,1025))}{3}\right)
\]

即把标准正态的分位点函数在 \([0.001,0.999]\) 上等分采样、除以 3 缩放、限幅到 \([-1,1]\)。`_Gaussify` 运行时对它做线性插值（[model/psi_fix_lin_approx.py:69-74](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L69-L74)）：把输入 \([-1,1]\) 映射到表索引 \([0,1024]\)，取整数段索引与余数，做 `offset + gradient×余数`。

#### 4.2.3 源码精读

实体约束输出格式必须为 `[1,0,x]` 且 \(F \le 19\)——因为内部固定走 `(1,0,19)` 的 gaussify：[hdl/psi_fix_noise_awgn.vhd:62-70](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_noise_awgn.vhd#L62-L70)。

两个内部格式常量——`IntFmt_c` 由 gaussify 决定、`RndFmt_c` 多一个整数位做舍入头空：[hdl/psi_fix_noise_awgn.vhd:40-41](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_noise_awgn.vhd#L40-L41)。

组合进程的 Round 级与 Sat 级（注意 round/sat 与 trunc/sat 的刻意分配）：[hdl/psi_fix_noise_awgn.vhd:81-87](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_noise_awgn.vhd#L81-L87)。

例化白噪声源（`out_fmt_g => IntFmt_c`，把均匀源格式钉死成 `(1,0,19)`）：[hdl/psi_fix_noise_awgn.vhd:114-126](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_noise_awgn.vhd#L114-L126)。

例化 gaussify（u8 生成的组件，输入输出同格式）：[hdl/psi_fix_noise_awgn.vhd:128-137](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_noise_awgn.vhd#L128-L137)。

Python 位真模型——三步串联，与 RTL 一一对应，`Generate` 末尾的 resize 用 `round/sat`（注意 Python 侧是一次性 resize，RTL 拆成两拍，但数值等价）：[model/psi_fix_noise_awgn.py:37-45](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_noise_awgn.py#L37-L45)。

#### 4.2.4 代码实践

**实践目标**：验证「白噪声 + gaussify」确实把均匀分布整形为高斯分布，并理解可复现性如何由底层 `white_noise` 的种子保证。

**操作步骤**：

1. 读 [model/psi_fix_noise_awgn.py:37-45](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_noise_awgn.py#L37-L45)，确认 `Generate` 就是「均匀 → gaussify 近似 → resize」三步。
2. 读 [testbench/psi_fix_noise_awgn_tb/Scripts/preScript.py:29-52](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_noise_awgn_tb/Scripts/preScript.py#L29-L52)，确认协同仿真用 `(1,0,15)` 输出格式生成 10000 样本，写盘成位模式整数。
3.（可选运行）把 `PLOT_ON=True`，preScript 会同时画「时域波形 / 频谱 / **直方图**」三联图（[preScript.py:35-45](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_noise_awgn_tb/Scripts/preScript.py#L35-L45)）。

**需要观察的现象**：直方图应呈钟形（高斯），而白噪声（4.1）的直方图是平的（均匀）；频谱两者都近似平坦（都是「白」）。同一种子重复运行，AWGN 输出逐位不变。

**预期结果**：VHDL `noise_awgn` 输出与 Python 黄金模型逐位一致，无 `###ERROR###`。由于无真随机，**硬件上板与仿真结果完全可复现**。运行结果若未本地执行，标注**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Round 级用 `wrap` 而不是 `sat`？
**答案**：舍入可能把刚好等于满量程 \(\pm 1.0\) 的值推到略超 \(\pm 1\)。若此刻就饱和，会把「舍入造成的微小越界」错误地削顶，破坏高斯分布的尾部统计。故先用多一位整数位的 `RndFmt_c` 做 round+wrap 容纳越界，把饱和推迟到下一拍再处理。

**练习 2**：`noise_awgn` 的可复现性来自哪里？它自己维护随机状态吗？
**答案**：来自底层 `psi_fix_white_noise` 的 LFSR 与固定种子 `seed_g`。`noise_awgn` 自身不维护任何随机状态，它只做确定性的「查表 + resize」，所以整条链路完全确定、可复现。

---

### 4.3 相位解卷绕（psi_fix_phase_unwrap）

#### 4.3.1 概念说明

`psi_fix_phase_unwrap` 把「折叠进 \([-180°,+180°)\)」的相位序列还原成连续相位。约定：**输入以「半圈」为单位**，即 `1.0` 表示 \(180°\)，输入格式 `in_fmt_g` 典型为 `(1,0,15)`（范围 \([-1,+1)\)）。输出格式 `out_fmt_g` 必须有符号且至少 1 个整数位（如 `(1,3,15)`，可表示到 \(\pm 8\) 圈）。

算法四级流水，核心是「**差分去卷绕 + 累加还原**」：

- **差分级**：`diff = 当前 − 上一`，在 `DiffFmt_c=(1,0,in_fmt.F)` 内做 **wrap**。该格式覆盖 \([-1,+1)=\pm 180°\)，于是任何小于 \(180°\) 的真实增量都被正确还原（跨越 \(\pm 180°\) 边界造成的 \(-360°\) 跳变被回绕抵消）。
- **累加级**：`sum = sum + diff`，在 `SumFmt_c=(1, max(out_fmt.I+1,1), in_fmt.F)` 内累加，得到连续相位。
- **溢出回退**：累加相位可能一直增长，没有「理论上够用」的输出格式。一旦累加值在舍入到 `out_fmt_g` 后**会越界**（用 `psi_fix_in_range` 判定），就把累加器重置为「当前原始输入样本」、并拉高 `wrap_o`——即此处发生了一次溢出，输出回退为输入相位。
- **输出级**：把累加值 resize/round 到 `out_fmt_g`。

文件头注释一句话概括：「unwrap phase signal for instance +/- 180 -> 360° and so on with indicator」（见 [hdl/psi_fix_phase_unwrap.vhd:10](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_phase_unwrap.vhd#L10)）。

#### 4.3.2 核心流程

四级流水（valid 用 `Vld(0..3)` 数组逐级平移，沿用 u3-3 的 record 流水封装风格）：

```
Stage0  寄存输入 dat_i，并把上一拍样本存入 InLast_0
Stage1  Diff_1 = psi_fix_sub(InData_0, InLast_0, DiffFmt_c, trunc, wrap)   -- 去卷绕差分
Stage2  Sum_v = psi_fix_add(Sum_2, Diff_1, SumFmt_c)                       -- 累加
        若 not psi_fix_in_range(Sum_v, SumFmt_c, out_fmt_g, round_g):
            Sum_v = psi_fix_resize(InData_1, in_fmt_g, SumFmt_c)           -- 溢出回退到原始输入
            Wrap_v = '1'
Stage3  OutData_3 = psi_fix_resize(Sum_2, SumFmt_c, out_fmt_g, round_g)    -- 末端舍入
        OutWrap_3 = Wrap_2
```

数学上，差分级依赖回绕恒等式（\(N\) 为半圈单位的整数，对应 \(360°\) 的整数倍）：

\[
(\phi_{k} - \phi_{k-1}) \bmod [-1, +1) \;=\; \Delta\phi_{k}\quad\text{（当 }|\Delta\phi_{k}|<1\text{，即真实增量}<180°\text{）}
\]

累加 \(\widehat{\phi}_{k}=\sum_{j}\Delta\phi_{j}\) 即得连续相位。Python 模型的注释说得直白：累加相位没有「理论上足够」的输出格式，溢出时回退到原始样本、从那里继续解卷绕，并用 `wrap_o` 示警（[model/psi_fix_phase_unwrap.py:17-20](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_phase_unwrap.py#L17-L20)）。

#### 4.3.3 源码精读

实体端口——注意 `wrap_o` 是溢出指示输出：[hdl/psi_fix_phase_unwrap.vhd:24-40](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_phase_unwrap.vhd#L24-L40)。

两个关键中间格式：`SumFmt_c` 给累加值（整数位比输出多 1，留累加头空），`DiffFmt_c=(1,0,in_fmt.F)` 覆盖 \(\pm 180°\)：[hdl/psi_fix_phase_unwrap.vhd:45-46](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_phase_unwrap.vhd#L45-L46)。

差分级（Stage1）——`psi_fix_sub` 用 **wrap**，这是去卷绕的关键：[hdl/psi_fix_phase_unwrap.vhd:91-95](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_phase_unwrap.vhd#L91-L95)。

累加 + 溢出回退（Stage2）——`psi_fix_in_range` 判定累加值能否安全舍入进 `out_fmt_g`，越界则回退到原始输入 `InData_1` 并置 `Wrap_v`：[hdl/psi_fix_phase_unwrap.vhd:97-107](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_phase_unwrap.vhd#L97-L107)。

输出级（Stage3）末端舍入：[hdl/psi_fix_phase_unwrap.vhd:109-113](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_phase_unwrap.vhd#L109-L113)。

Python 位真模型 `Process`——先用 `np.roll` 求上一拍、做 wrap 差分，再循环累加，逻辑与 RTL 的四级流水数值等价：[model/psi_fix_phase_unwrap.py:48-72](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_phase_unwrap.py#L48-L72)。

#### 4.3.4 代码实践

**实践目标**：理解 `phase_unwrap` 为何需要 `stimuli_set_g` 与精心设计的刺激数据，说清它们如何分别覆盖「有卷绕 / 无卷绕」两类场景。

**操作步骤**：

1. 打开 [testbench/psi_fix_phase_unwrap_tb/Scripts/preScript.py:32-41](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_phase_unwrap_tb/Scripts/preScript.py#L32-L41)，读刺激构造代码：`rampForward`/`rampBackward` 是缓变斜坡（**无卷绕**），`bigSteps` 是大跳变（边界情况），`overflowPos`/`overflowNeg` 是长达 100 样本的单向斜坡（**强制累加器溢出 → 触发 `wrap_o`**）。
2. 对照 [testbench/psi_fix_phase_unwrap_tb/psi_fix_phase_unwrap_tb.vhd:42-44](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_phase_unwrap_tb/psi_fix_phase_unwrap_tb.vhd#L42-L44)，看 `stimuli_set_g="S"` 对应输入 `(1,0,15)`（有符号半圈），`"U"` 对应 `(0,1,15)`（无符号，覆盖 \([0,2)\) 即 \([0°,360°)\) 的另一种相位约定）。
3. 看 [sim/config.tcl:388-394](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L388-L394) 跑了三组：`S/duty5`、`U/duty5`、`S/duty1`。

**需要观察的现象与解释**：

- `stimuli_set_g` 的 S/U **本身**区分的是两种相位**输入约定**（有符号 \([-1,+1)\) vs 无符号 \([0,2)\)），解卷绕器对两种约定都必须正确工作，故各跑一组。
- 真正的「**有/无卷绕**」覆盖来自 preScript 里的刺激数据本身：`rampForward`/`rampBackward`/`bigSteps` 走的是无溢出路径（`wrap_o` 恒 0，验证连续相位被正确还原）；`overflowPos`（`cumsum(+0.2×100)=+20`，远超 `out_fmt=(1,3,15)` 的 \(\pm 8\) 范围）和 `overflowNeg` 则**反复触发溢出回退**，验证 `wrap_o` 被正确拉高、且回退后能从原始样本继续解卷绕。两组场景缺一不可：只测斜坡发现不了溢出回退 bug，只测溢出又验不了正常累加。

**预期结果**：三组参数下，VHDL 的 `(dat_o, wrap_o)` 与 Python 黄金模型逐位一致（测试台把两者拼成两列文本比对，见 [psi_fix_phase_unwrap_tb.vhd:148-149](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_phase_unwrap_tb/psi_fix_phase_unwrap_tb.vhd#L148-L149) 与 [preScript.py:85-92](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_phase_unwrap_tb/Scripts/preScript.py#L85-L92)），无 `###ERROR###`。具体波形**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：差分级为什么必须用 `psi_fix_wrap`（回绕）而不是 `psi_fix_sat`（饱和）？
**答案**：去卷绕靠的就是「把跨越 \(\pm 180°\) 边界造成的 \(-360°\) 跳变回绕抵消掉」。若用饱和，\(-1.8\) 会被钳到 \(-1.0\)，丢失真实增量信息，累加出来的就不再是连续相位。回绕让 \(-1.8 \to +0.2\)，恰好还原小于 \(180°\) 的真实正增量。

**练习 2**：`SumFmt_c` 的整数位为何取 `max(out_fmt_g.I+1, 1)`？
**答案**：累加值需要比最终输出多至少 1 个整数位的头空，这样「累加瞬间越界但舍入后其实仍落在 out_fmt 范围内」的情况不会被误判为溢出；`psi_fix_in_range` 才能准确区分「真溢出」与「舍入后安全」。`max(...,1)` 保证即使 `out_fmt_g.I=0` 也至少有 1 个整数位。

**练习 3**：当 `wrap_o='1'` 的那一拍，`dat_o` 输出的是什么？
**答案**：输出的是「当前原始输入样本」被 resize 到 `out_fmt_g` 的值（累加器被重置为原始输入），而非累加值。这是「无理论上足够输出格式」时的优雅降级：放弃累积、回到输入相位、从那里继续解卷绕，并用 `wrap_o` 告知下游发生过一次溢出。

---

## 5. 综合实践

把本讲三个组件串成一条「**带噪相位通道 + 解卷绕**」的源码阅读型综合任务，串联起白噪声、高斯整形与解卷绕三段知识。

**任务**：阅读源码，画出下面这条假想链路的框图与各级定点格式，并回答问题。

```
psi_fix_white_noise (1,0,19)  →  psi_fix_lin_approx_gaussify20b  →  [假想: 相位调制]  →  psi_fix_phase_unwrap
```

**步骤**：

1. 从 [hdl/psi_fix_noise_awgn.vhd:114-137](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_noise_awgn.vhd#L114-L137) 抄出「白噪声 → gaussify」的例化，标注两端的格式都是 `(1,0,19)`。
2. 解释：为什么 `noise_awgn` 能直接复用 `white_noise`，而无需自己实现随机数？（答：高斯整形是确定性的查表变换，随机性完全来自底层白噪声的 LFSR + 种子。）
3. 假设把 `noise_awgn` 的输出当作「相位抖动」叠加到一个线性增长的相位上，再送入 `phase_unwrap`（`in_fmt=(1,0,15)`、`out_fmt=(1,3,15)`）。根据 [hdl/psi_fix_phase_unwrap.vhd:45-46](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_phase_unwrap.vhd#L45-L46) 与 §4.3.2，判断：抖动会让 `wrap_o` 偶发拉高吗？为什么？（提示：抖动幅度小、不会让单拍增量超过 \(180°\)，故差分级正确去卷绕；但若线性斜率累积超过 \(\pm 8\) 圈，累加器仍会溢出回退。）
4. 用一句话总结：这三个组件共同体现了 psi_fix 的哪条方法论？（答：**确定性可复现**——全部用固定种子/确定性查表，黄金参考可逐位复现，`###ERROR###` 为唯一失败判据。）

**预期结果**：能画出三级格式流图，并说清「随机性集中在 white_noise、其余都是确定性变换」这一架构事实。

## 6. 本讲小结

- `psi_fix_white_noise` 用「**每位一条 32 位 LFSR**」并行拼出定点均匀白噪；抽头 \(\{31,20,26,25\}\)、种子 `seed_g + 2^i`，序列**完全确定可复现**，是整条噪声链的随机性源头。
- `psi_fix_noise_awgn` = 白噪声（均匀源）+ `gaussify20b`（逆 CDF 分段线性查表，逆变换采样）+ Manual Splitting 两级 resize（先 round/wrap、再 trunc/sat），把均匀分布整形为高斯；自身不维护随机状态。
- gaussify 表在设计期由 `scipy.stats.norm.ppf` 生成并限幅到 \([-1,1]\)，是 u8 线性近似内核的「一个内核 + 一张表」实例。
- `psi_fix_phase_unwrap` 用「**wrap 差分去卷绕 + 累加还原**」四级流水；累加相位没有理论上足够的输出格式，故用 `psi_fix_in_range` 检测溢出、越界则回退到原始输入样本并拉高 `wrap_o`。
- 协同仿真沿用全库套路：`preScript.py` 跑 Python 黄金模型，用 `psi_fix_get_bits_as_int` 写位模式整数文本，测试台 `###ERROR###` 逐位比对；`phase_unwrap` 还把 `wrap_o` 作为第二列一起比对。
- 测试覆盖上，`stimuli_set_g` 的 S/U 区分**有符号/无符号两种相位输入约定**，而真正的**有/无卷绕**覆盖由 preScript 刺激数据（缓变斜坡 vs 长单向斜坡强制溢出）分别承担，两组缺一不可。

## 7. 下一步学习建议

- **横向对比「随机性」实现**：本讲的 LFSR 白噪声是「每位独立 LFSR」；可对比 psi_common 库里更通用的 PRBS 发生器（`white_noise.vhd` 头部注释提到可选反馈抽头权重），理解资源-统计质量的取舍。
- **深入 gaussify 的表生成**：回到 u8-l2，精读 [model/psi_fix_lin_approx.py:65-74](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L65-L74) 与 `Design()`/`GenerateEntity()`，看 `Gaussify20Bit` 配置如何被同一套模板渲染成 `psi_fix_lin_approx_gaussify20b.vhd`，体会「生成式组件族」的维护方式。
- **下一讲 u9-l3**：进入 `lowpass_iir_order1` 一阶 IIR 与 `mult_add_stage` 乘加构建块，看反馈型滤波器如何处理内部量化与流水——它与本讲 `phase_unwrap` 的累加反馈、`noise_awgn` 的 Manual Splitting 一脉相承。
- 若对相位处理感兴趣，可结合 u9-l1 的 DDS（相位累加器产生连续相位）反向理解：DDS 是「连续相位 → 折叠查表」，而 `phase_unwrap` 恰是其逆问题（「折叠相位 → 还原连续」），两者对照阅读收益最大。
