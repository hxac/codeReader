# Seesaw 调度与 TMA 流水

## 1. 本讲目标

上一篇（u3-l2）我们读完了 `config.h` 和 `traits.h`，把 kernel 的「静态骨架」搭好了：四个 `TiledMMA`（QK 的 `sQ`/`rQ`，PV 的 `LocalP`/`RemoteP`）、五个 `NamedBarrier`、19 个 TMA barrier，以及 `HEAD_DIM_K=576 = 9×64` 这个整除关系。本篇要把这堆「零件」真正转起来——进入 [splitkv_mla.cuh](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh) 的主循环，理解 SM90 dense decode kernel 的三大动态机制。

学完本篇你应当能：

- 说清 **seesaw（跷跷板）调度** 为什么是「只用一份输出矩阵的 ping-pong 变体」，并证明它的 11 步与 FlashAttention 的 online softmax **数学等价**。
- 看懂 `wg0_subroutine` / `wg1_subroutine` 两个 warpgroup 如何靠 5 个 `NamedBarrier` 在时间上**交错**，让 CUDA Core（softmax/rescale）与 Tensor Core（WGMMA）重叠，从而「喂饱 Tensor Core」。
- 解释**细粒度 TMA 流水**（一个 \(64\times576\) 的 K 块拆成 9 次 \(64\times64\) TMA 拷贝，拷完一块就开算）如何掩盖访存延迟，以及 `EVICT_FIRST` cache hint 为什么反而提升 L2 命中率。

本篇是整个 SM90 dense decode kernel 的「心脏」，也是 u3-l1 compute-bound 结论落地的关键：正是因为解码是 compute-bound，作者才花这么大力气设计 seesaw 去重叠两种 Core。

## 2. 前置知识

### 2.1 寄存器预算：为什么「放不下两份 O」

FlashAttention-3 用「双输出矩阵 ping-pong」来重叠 CUDA Core 与 Tensor Core：warpgroup 0 算 \(O_0\) 时，warpgroup 1 算 \(O_1\)，两者交替。但这要求**同时持有两份 O**。

MLA decode 的输出 \(O\) 是 \(64\times512\)，全 float32：

\[
64 \times 512 = 32768 \quad \text{个 32-bit 寄存器}
\]

博客指出，一个 SM 只有约 65536 个 32-bit 寄存器，**只放得下一份完整的 O**。所以 FA3 的双 O ping-pong 在这里玩不转，必须另想办法——这就是 seesaw 的起点（详见 u3-l2 §2.3，本篇不再重复推导，直接用其结论：O 被竖切成 \(O_L\)、\(O_R\) 各 \(64\times256\)，分给两个 warpgroup）。

### 2.2 Online softmax 的 rescale 公式（复习）

FlashAttention 的核心是「分块 + online softmax」。设当前已累计的输出 \(o\) 是以**旧的全局最大值 \(m_{\text{old}}\)** 为基准归一化的，现在来了一个新块，新的全局最大值变成 \(m_{\text{new}}=\max(m_{\text{old}}, mp)\)，则：

\[
o_{\text{new}} = o_{\text{old}}\cdot \exp(m_{\text{old}} - m_{\text{new}}) + \exp(p - m_{\text{new}})\cdot V
\]

即「旧输出乘以 rescale 因子 \(\exp(m_{\text{old}}-m_{\text{new}})\le 1\)，再加上新块的贡献」。**记住这个 rescale 因子的方向**（\(m_{\text{old}}-m_{\text{new}}\)，是个非正数），后面源码里全是它。代码里用 base-2（`exp2f`），并把 \(\text{scale\_softmax}\) 折算成 \(\log_2\) 域的 `scale_softmax_log2`，原理一致。

### 2.3 stmatrix / ldmatrix 与 NamedBarrier（承接 u3-l2）

两个 warpgroup 要交换 P 矩阵，靠的是这对「寄存器 ⇄ smem 的矩阵搬运工」：

- `stmatrix`（代码里是 `SM90_U32x4_STSM_N`）：把寄存器里的 P 写进 smem。
- `ldmatrix`（代码里是 `SM75_U32x4_LDSM_N`）：把 smem 里的 P 读回寄存器。

而两个 warpgroup 之间的「点名汇合」靠 5 个 `NamedBarrier`（[traits.h:101-107](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/traits.h#L101-L107)）：`sScale0Ready`、`sScale1Ready`、`sP0Ready`、`rO1sP0sV0RIssued`、`sMInitialized`。`NamedBarrier::arrive_and_wait(num_threads, id)` 让指定数量的线程在某个编号上汇合。本篇会反复用到它们。

### 2.4 WGMMA 是异步的

一条 `wgmma.mma_async`（CUTLASS 里通过 `TiledMMA` + `cute::gemm` 发射）发射后**立即返回**，CUDA Core 可以去干别的活（比如 softmax），等结果要用时再 `warpgroup_wait<N>()`。这是「重叠两种 Core」的硬件前提：发完 GEMM 不必傻等。代码里大量 `warpgroup_arrive()` / `warpgroup_commit_batch()` / `warpgroup_wait<N>()` 都是在驾驭这个异步性。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [splitkv_mla.cuh](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh) | 本篇主角。从 TMA 拷贝、QK/PV 的 GEMM、softmax/rescale，到 `wg0_subroutine`/`wg1_subroutine` 主循环、kernel 入口、启动配置全在这里。 |
| [traits.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/traits.h) | 提供 `TiledMMA_*`、`NamedBarriers`、`SharedMemoryPlan`。本篇只引用，不重讲（见 u3-l2）。 |
| [docs/20250422-new-kernel-deep-dive.md](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250422-new-kernel-deep-dive.md) | 官方深度博客，seesaw 11 步公式与设计动机的原始出处。 |

一句话：本篇把博客里的 11 步「数学公式」逐行对到 `splitkv_mla.cuh` 的 C++/CUDA 代码上。

## 4. 核心概念与源码讲解

### 4.1 seesaw 数学变换

#### 4.1.1 概念说明

seesaw 要解决的问题是：**只有一份 O 的寄存器预算下，如何仍能重叠两个 warpgroup 的 CUDA Core 与 Tensor Core？**

作者的回答是——在 FlashAttention 的 online softmax 之上，**再加一层数学变换**。每一步同时取**两个 KV 块**（\(K_0,V_0\) 和 \(K_1,V_1\)），把输出竖切成 \(O_L\)、\(O_R\)（各 \(64\times256\)），V 也对应切成 \(V_{0L},V_{0R},V_{1L},V_{1R}\)。然后按一套精心排序的 11 步来算，使得：

- warpgroup 0 拥有 \(O_L\)、算 \(p_0=\text{softmax}(qK_0^\top)\)；
- warpgroup 1 拥有 \(O_R\)、算 \(p_1=\text{softmax}(qK_1^\top)\)；
- 两个 warpgroup 通过 smem 交换 \(p_0,p_1\) 和 rescale 因子，最终各自把自己的半边 \(O\) 累加正确。

博客把它叫「**只用一个输出矩阵的 ping-pong 变体**」——像跷跷板一样，两个 warpgroup 此起彼伏地用同一份「输出寄存器额度」交替推进，故称 seesaw。

#### 4.1.2 核心流程：11 步与等价性证明

设 \(m\) 为两个 warpgroup **共享**的 running max（初始 \(-\infty\)），\(o_L,o_R\) 初始为 0。下表方括号 `[0]/[1]` 是执行该步的 warpgroup 编号（取自博客 [docs/20250422-new-kernel-deep-dive.md:27-40](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250422-new-kernel-deep-dive.md#L27-L40)）：

| 步 | warpgroup | 运算 |
|----|-----------|------|
| 1 | [0] | \(p_0 = qK_0^\top / \text{scale}\) |
| 2 | [1] | \(p_1 = qK_1^\top / \text{scale}\) |
| 3 | [0] | \(mp_0=\max(p_0),\ m_{\text{new},0}=\max(m,mp_0),\ s_0=\exp(m_{\text{old}}-m_{\text{new},0})\)，更新 \(m\gets m_{\text{new},0}\) |
| 4 | [0] | \(p_0 \gets \exp(p_0 - m_{\text{new},0})\)（softmax） |
| 5 | [0] | \(o_L \gets o_L\cdot s_0 + p_0 V_{0L}\) |
| 6 | [1] | \(mp_1=\max(p_1),\ m_{\text{new},1}=\max(m,mp_1),\ s_1=\exp(m_{\text{new},0}-m_{\text{new},1})\)，更新 \(m\gets m_{\text{new},1}\) |
| 7 | [1] | \(p_1 \gets \exp(p_1 - m_{\text{new},1})\)（softmax） |
| 8 | [1] | \(o_R \gets o_R\cdot(s_0\cdot s_1) + p_1 V_{1R}\) |
| 9 | [0] | \(p_0 \gets p_0\cdot s_1\) |
| 10 | [1] | \(o_R \gets o_R + p_0 V_{0R}\) |
| 11 | [0] | \(o_L \gets o_L\cdot s_1 + p_1 V_{1L}\) |

> 注：博客原文把 \(s_0\) 写成 \(\exp(m_{\text{new},0}-m)\)；但代码里实际用的是 §2.2 的标准方向 \(\exp(m_{\text{old}}-m_{\text{new}})\)（见 4.1.3）。两者只是记号习惯，下面按代码方向证明。

**等价性证明**（为什么这 11 步等于标准 FA online softmax）：

标准 FA 处理两个块后，输出应以**全局最大值** \(m_{\text{new},1}=\max(m_{\text{old}},mp_0,mp_1)\) 为基准：

\[
o_L = o_L^{(\text{旧})}\cdot\exp(m_{\text{old}}-m_{\text{new},1}) + \exp(p_0-m_{\text{new},1})V_{0L} + \exp(p_1-m_{\text{new},1})V_{1L}
\]

看 seesaw 对 \(o_L\) 做了什么（步骤 5、9、11）：

\[
\begin{aligned}
\text{步骤5:}\quad o_L &\gets o_L^{(\text{旧})}\cdot s_0 + \exp(p_0-m_{\text{new},0})V_{0L} \\
\text{步骤11:}\quad o_L &\gets \bigl(o_L^{(\text{旧})}\cdot s_0 + \exp(p_0-m_{\text{new},0})V_{0L}\bigr)\cdot s_1 + \exp(p_1-m_{\text{new},1})V_{1L}
\end{aligned}
\]

把 \(s_0=\exp(m_{\text{old}}-m_{\text{new},0})\)、\(s_1=\exp(m_{\text{new},0}-m_{\text{new},1})\) 代入，注意 \(s_0\cdot s_1=\exp(m_{\text{old}}-m_{\text{new},1})\)，且 \(\exp(p_0-m_{\text{new},0})\cdot s_1=\exp(p_0-m_{\text{new},1})\)，化简后正好等于上面的标准式。✓

\(o_R\) 同理，但它**一次性**乘 \(s_0\cdot s_1\)（步骤 8），因为 warpgroup 1 做 softmax 时 \(s_0,s_1\) 都已知，可以合并成一次乘法；而 \(o_L\) 分两次乘（步骤 5 乘 \(s_0\)、步骤 11 乘 \(s_1\)），因为 warpgroup 0 先做 softmax、当时 \(s_1\) 还没算出来。**这种「\(o_L\) 分两次 rescale、\(o_R\) 合并一次 rescale」的不对称，正是为了让两个 warpgroup 的 softmax 在时间上错开、从而重叠两种 Core 的关键。** 步骤 9 把 \(p_0\) 提前乘 \(s_1\)，是为了让步骤 10 里 warpgroup 1 用「借来的 \(p_0\)」时它已经是 \(m_{\text{new},1}\) 基准，无需再 rescale。

#### 4.1.3 源码精读

代码把 11 步拆进几个 device 函数。先看**两个 warpgroup 各自的 softmax + rescale 主体**。

**wg0 的步骤 3、4、5 的 rescale 部分** —— [splitkv_mla.cuh:331-391](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L331-L391) `wg0_bunch_0`：

- [splitkv_mla.cuh:349-358](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L349-L358)：对 \(p_0\) 做 causal mask，并求行向 max \(mp_0\)（步骤 3 的前半）。
- [splitkv_mla.cuh:363-370](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L363-L370)：`new_max = max(sM, cur_max)` 即 \(m_{\text{new},0}\)；`scale_for_old = exp2f(sM - new_max)` 即 \(s_0\)；写入 `sScale0` 并更新 `sM`（步骤 3 的后半，\(s_0\) 落进 smem 供 wg1 步骤 8 取用）。
- [splitkv_mla.cuh:373-377](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L373-L377)：`rO0 *= scale_for_old`，即步骤 5 的「\(o_L\cdot s_0\)」那半。
- [splitkv_mla.cuh:382-388](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L382-L388)：`rP0 = exp2f(rP0*scale - new_max)` 即步骤 4 的 softmax；同时 `rPb = (InputT)rP0` 把 float 的 \(p_0**降精度*成 bf16/half 存进 `rPb`（给后续 GEMM 当 A 操作数）。行向 expsum 累加进 `rL`（分母）。

注意 [splitkv_mla.cuh:17-18](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L17-L18) 的两个初始值常量：`MAX_INIT_VAL_SM=-1e30f`（`sM` 的初值，比 `-inf` 稍大）和 `MAX_INIT_VAL=-1e33f`（mask 用）。注释解释了为什么 `sM` 要用一个「不那么负」的值：因为后面要算 `new_max = max(sM, cur_max*scale)`，必须保证 `MAX_INIT_VAL * scale_softmax_log2 < MAX_INIT_VAL_SM`，否则 mask 出来的极小值乘以 scale 后会反过来盖过初始 `sM`。

**wg1 的步骤 6、7、8 的 rescale 部分** —— [splitkv_mla.cuh:406-475](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L406-L475) `wg1_bunch_0`：

- [splitkv_mla.cuh:442-449](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L442-L449)：`old_max = sM`（此时已是 wg0 更新过的 \(m_{\text{new},0}\)）；`new_max = max(old_max, cur_max)` 即 \(m_{\text{new},1}\)；`scale_for_old = exp2f(old_max - new_max)` 即 \(s_1\)；写入 `sScale1` 并更新 `sM`（步骤 6）。
- [splitkv_mla.cuh:453-461](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L453-L461)：步骤 7 的 softmax（\(p_1\gets\exp(p_1-m_{\text{new},1})\)），同样降精度到 `rP1b`。
- [splitkv_mla.cuh:465-470](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L465-L470)：`cur_scale_for_o1 = scale_for_old * sScale0(row_idx)`。这就是步骤 8 里的 \(s_0\cdot s_1\)！注意它把 wg0 写进 smem 的 `sScale0`（\(s_0\)）和自己的 `scale_for_old`（\(s_1\)）**当场乘起来**，让 \(o_R\) 一次 rescale 到位——4.1.2 证明里的那个 \(s_0 s_1=\exp(m_{\text{old}}-m_{\text{new},1})\) 在这里落地。然后 `rO1 *= cur_scale_for_o1`。

**步骤 9（\(p_0\gets p_0\cdot s_1\)）** —— [splitkv_mla.cuh:529-545](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L529-L545) `wg0_scale_rP0`：读 smem 里的 `sScale1`（\(s_1\)），把寄存器里**还没降精度的 float \(p_0\)**（`rP0`）乘以 \(s_1\)，结果写进 `rPb`。这一步之后 `rPb` 就是 \(\exp(p_0-m_{\text{new},1})\)（\(m_{\text{new},1}\) 基准），可供步骤 10 warpgroup 1 直接用。

**步骤 11 的 \(o_L\cdot s_1\) 部分** —— [splitkv_mla.cuh:553-570](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L553-L570) `wg0_rescale_rO0`：`rO0 *= sScale1`（\(o_L\) 再乘 \(s_1\)），同时 `rL` 也乘 \(s_1\)（分母要和分子保持同基准）。

> 小结：softmax/exp/rescale 这些**标量与逐元素运算全在 CUDA Core 上跑**（`exp2f`、`max`、`__shfl_xor_sync` 行内归约）；而 \(pV\) 的矩阵乘才上 Tensor Core（WGMMA）。seesaw 的全部「数学」都浓缩在上面四个函数里。

#### 4.1.4 代码实践

**目标**：把 4.1.2 表格里的步骤 3/4/5/6/7/8/9/11 逐一对到代码行，亲手验证 rescale 因子的方向与 §2.2 一致。

**操作步骤**：

1. 打开 `wg0_bunch_0`（[L331-391](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L331-L391)），找到 `scale_for_old = exp2f(sM(row_idx) - new_max)`。确认它是 \(\exp(m_{\text{old}}-m_{\text{new},0})\)（非正），不是反方向。
2. 打开 `wg1_bunch_0`（[L406-475](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L406-L475)），找到 `cur_scale_for_o1 = scale_for_old * sScale0(row_idx)`（[L465](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L465)）。手算验证它等于 \(\exp(m_{\text{old}}-m_{\text{new},1})\)。
3. 打开 `wg0_scale_rP0`（[L529-545](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L529-L545)）和 `wg0_rescale_rO0`（[L553-570](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L553-L570)），确认它们都乘的是 `sScale1`（即 \(s_1\)）。

**需要观察的现象**：

- `sScale0`（\(s_0\)）由 wg0 写、wg1 读；`sScale1`（\(s_1\)）由 wg1 写、wg0 读——两个 rescale 因子方向相反地穿越 smem。
- \(o_L\) 在两处被乘（`wg0_bunch_0` 的 Scale-O 乘 \(s_0\)、`wg0_rescale_rO0` 乘 \(s_1\)）；\(o_R\) 只在一处被乘（`wg1_bunch_0` 乘 \(s_0 s_1\)）。这就是 4.1.2 末尾说的「不对称」。

**预期结果**：你能在代码里指出「\(o_L\) 被两次 rescale、\(o_R\) 被一次合并 rescale」的具体行号，并能解释这种不对称是为了让两个 warpgroup 的 softmax 错开。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `wg0_bunch_0` 里 `rO0 *= scale_for_old`（[L374-377](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L374-L377)）删掉，改在 `wg0_rescale_rO0` 里一次性乘 \(s_0 s_1\)，行不行？

> **答案**：数学上行（最终 \(o_L\) 都乘了 \(s_0 s_1\)），但**破坏调度**。`wg0_bunch_0` 是 wg0 在步骤 5 之前唯一能拿到 \(s_0\) 的时机（\(s_0\) 此时刚算出来，还没被后续指令挤掉寄存器）；而且步骤 5 的 \(p_0 V_{0L}\) 要加到「已 rescale 的 \(o_L\)」上，二者必须同基准。若推迟到步骤 11 才 rescale，\(o_L\) 在步骤 5 就和新贡献基准不一致了。更重要的是，把 rescale 集中到一处会错失「CUDA Core 做 rescale 的同时 Tensor Core 做 GEMM」的重叠机会。所以这里 rescale 拆两半是**调度需要**，不只是数学需要。

**练习 2**：步骤 9（\(p_0\gets p_0\cdot s_1\)）为什么由 **wg0** 来做，而不是由要用 \(p_0\) 的 wg1 来做？

> **答案**：因为此时 \(p_0\)（`rP0`）还在 **wg0 的寄存器**里（是 float，未降精度）。由 wg0 就地乘 \(s_1\) 后再 `stmatrix` 写进 smem，传递给 wg1 的就是「已 rescale 到 \(m_{\text{new},1}\) 基准」的 \(p_0\)。若让 wg1 来乘，得先把未 rescale 的 float \(p_0\) 传过 smem，精度和带宽都更浪费。这体现了 seesaw 的一个原则：**谁拥有数据、谁就地完成它负责的变换，再以最终形态交换。**

---

### 4.2 双 warpgroup 交错

#### 4.2.1 概念说明

光有 4.1 的数学等价性还不够——11 步如果**串行**执行，Tensor Core 大量时间在闲着等 CUDA Core 做 softmax。seesaw 的真正威力在于：把 11 步**铺到两个 warpgroup 上并发执行**，用 `NamedBarrier` 只在「真的有数据依赖」的地方才汇合，其余时间让两个 warpgroup 各自的 CUDA Core 与 Tensor Core 交错跑满。

直觉：warpgroup 0 的 Tensor Core 在做 \(p_0 V_{0L}\)（步骤 5，WGMMA 异步）时，warpgroup 0 自己的 CUDA Core 可以去做下一块的 softmax，同时 warpgroup 1 的 CUDA Core 在做步骤 6/7 的 softmax。只要依赖关系允许，就绝不空等。这就是博客说的「overlap CUDA Core and Tensor Core operations by interleaving the two warpgroups」。

#### 4.2.2 核心流程：wg0_subroutine / wg1_subroutine 的时间线

主循环里，wg0 反复调 `wg0_subroutine`，wg1 反复调 `wg1_subroutine`。每调一次处理**两个 KV 块**（block_idx 和 block_idx+1，对应博客的 \(K_0,V_0\) 与 \(K_1,V_1\)）。两个 subroutine 的指令流被刻意**错位编排**，使得一方的 CUDA Core 段与另一方的 Tensor Core 段在时间上重叠。

下面把两个 subroutine 的关键节点按代码顺序列出，并标出每次 `NamedBarrier` 汇合的「握手」：

```
wg0_subroutine (拥有 o_L=rO0, 算 p0)            wg1_subroutine (拥有 o_R=rO1, 算 p1)
────────────────────────────────────────       ────────────────────────────────────────
[步骤3,4] wg0_bunch_0: softmax(p0),写 sScale0
          arrive(sScale0Ready) ──────────────► wait(sScale0Ready)   ◄── 握手1: s0 就绪
                                                 [步骤6,7,8-rescale] wg1_bunch_0:
                                                   softmax(p1), 写 sScale1,
                                                   rO1 *= s0*s1
                                                 arrive(sScale1Ready)
[步骤5] rO0 += rPb @ sV0L  (LocalP, WGMMA)     [步骤8] rO1 += rP1b @ sV1R (LocalP, WGMMA)
                                                 save rP1b→sP1 (stmatrix)
          wait(sScale1Ready) ◄──────────────── arrive(sScale1Ready) ──► 握手2: s1 就绪
[步骤9] wg0_scale_rP0: p0 *= s1 → rPb
        save rPb→sP0 (stmatrix)
          arrive(sP0Ready) ───────────────────► wait(sP0Ready)        ◄── 握手3: sP0 就绪
                                                 [步骤10] rO1 += sP0 @ sV0R (RemoteP, WGMMA)
                                                 arrive(rO1sP0sV0RIssued)
          wait(rO1sP0sV0RIssued) ◄──────────── arrive(rO1sP0sV0RIssued) ─► 握手4: sV0 可复用
[步骤11] wg0_rescale_rO0: rO0 *= s1
         rO0 += sP1 @ sV1L (RemoteP, WGMMA)
（之后为下一轮 prefetch QK^T、launch TMA）
```

四条 NamedBarrier 握手对应 seesaw 里四个**不可回避的数据依赖**（\(s_0\)、\(s_1\)、\(p_0\)、sV0 复用权），其余时间两个 warpgroup 各自跑满。注意 `rO1sP0sV0RIssued` 这个名字有点拗口，它的含义是：wg1 已经发射完 `rO1 += sP0 @ sV0R`（RemoteP 用了 sV0），从此 sV0 这块 smem wg0 可以拿去干别的（比如覆盖写下一块的 K）——这是**smem 复用权**的交接，不是数据本身。

#### 4.2.3 源码精读

**wg0_subroutine**（[splitkv_mla.cuh:742-832](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L742-L832)）按代码顺序读：

- [L778](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L778)：`wg0_bunch_0`（步骤 3、4、5 的 rescale 段）。
- [L779](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L779)：`NamedBarrier::arrive(..., sScale0Ready)` ——通知 wg1：\(s_0\) 好了（握手 1 的 arrive 端，注意只 arrive 不 wait）。
- [L786](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L786)：`warpgroup_cooperative_pv_gemm_localP(rPb, sV0L, rO0, ...)` ——步骤 5 的累加 \(o_L \mathrel{+}= p_0 V_{0L}\)，这是**第一套 WGMMA（O_L 的 LocalP）**。
- [L792](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L792)：`NamedBarrier::arrive_and_wait(..., sScale1Ready)` ——等 wg1 算出 \(s_1\)（握手 2）。
- [L797](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L797)：`wg0_scale_rP0`（步骤 9）。
- [L798](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L798)：`save_rPb_to_sP(rPb, sP0, ...)`（stmatrix，把 rescale 后的 \(p_0\) 写进 sP0）。
- [L800](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L800)：`NamedBarrier::arrive(..., sP0Ready)`（握手 3 arrive 端）。
- [L808](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L808)：`NamedBarrier::arrive_and_wait(..., rO1sP0sV0RIssued)`（握手 4，等 sV0 复用权）。
- [L809](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L809)：`wg0_rescale_rO0`（步骤 11 的 \(o_L\cdot s_1\) 段）。
- [L810](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L810)：`warpgroup_cooperative_pv_gemm_remoteP(sP1, sV1L, rO0, ...)` ——步骤 11 的累加 \(o_L \mathrel{+}= p_1 V_{1L}\)，这是**第二套 WGMMA（O_L 的 RemoteP，读 wg1 写的 sP1）**。
- [L816/L827](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L816-L827)：为下一轮 prefetch `P0 = Q @ K0^T`（步骤 1），夹在 PV 之后是为了把 QK^T 也流水进来（见 4.3）。

**wg1_subroutine**（[splitkv_mla.cuh:854-945](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L854-L945)）对称地读：

- [L890](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L890)：`arrive_and_wait(sScale0Ready)`（握手 1 wait 端）。
- [L891](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L891)：`wg1_bunch_0`（步骤 6、7、8 的 rescale 段）。
- [L892](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L892)：`arrive(sScale1Ready)`（握手 2 arrive 端）。
- [L904](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L904)：`warpgroup_cooperative_pv_gemm_localP(rP1b, sV1R, rO1, ...)` ——步骤 8 的累加 \(o_R \mathrel{+}= p_1 V_{1R}\)，**O_R 的第一套 WGMMA（LocalP）**。
- [L913](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L913)：`arrive_and_wait(sP0Ready)`（握手 3 wait 端）。
- [L918](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L918)：`warpgroup_cooperative_pv_gemm_remoteP(sP0, sV0R, rO1, ...)` ——步骤 10 的累加 \(o_R \mathrel{+}= p_0 V_{0R}\)，**O_R 的第二套 WGMMA（RemoteP，读 wg0 写的 sP0）**。
- [L920](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L920)：`arrive(rO1sP0sV0RIssued)`（握手 4 arrive 端）。

**「两套 WGMMA」就此对齐**：每个 warpgroup 对自己的半边 O 各做两次 WGMMA——一次 LocalP（用自己的寄存器 P），一次 RemoteP（用对方 stmatrix 写进 smem 的 P）。这正是 u3-l2 §4.2 留给本篇解释的「LocalP/RemoteP 落到 seesaw 哪几步」的答案：步骤 5/8 是 LocalP，步骤 10/11 是 RemoteP。

**warpgroup 的派发**在 kernel 入口 [splitkv_mla.cuh:975](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L975) 算出 `warpgroup_idx = threadIdx.x / 128`（0 或 1，因为 `NUM_THREADS=256`），随后在 [L1105](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L1105) `if (warpgroup_idx == 0) { ...wg0 路径... } else { ...wg1 路径... }`。两个 warpgroup 跑**同一份 kernel 代码的不同分支**，靠 5 个 NamedBarrier 在 `wg0_subroutine`/`wg1_subroutine` 内部按 4.2.2 的时间线汇合。注意主循环步长是 2（`block_idx += 2`，如 [L1139](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L1139)），因为每轮处理两个 KV 块；循环尾巴用 `IS_BLK0_LAST/IS_BLK1_LAST/IS_BLK2_LAST` 模板参数处理「剩一块/剩两块」的边界（如 [L1143-1147](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L1143-L1147)），控制 causal mask 和 OOB 填零（`fill_oob_V`，[L579-597](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L579-L597)）。

#### 4.2.4 代码实践（本讲指定任务）

**目标**：对照博客 11 步 seesaw 公式，在 `splitkv_mla.cuh` 里定位**两套 WGMMA（\(O_L\)/\(O_R\)）**和**softmax/scale 更新代码**，给关键行加注释（源码阅读型实践，无需 GPU）。

**操作步骤**：

1. 先确认「两套 WGMMA」的物理含义：每个 warpgroup 持有 `rO`（[L1088](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L1088)，形状 `((2,2,32),1,1)` = \(64\times256\)，正是 O 的一半）。wg0 的 `rO` 即 \(O_L\)、wg1 的 `rO` 即 \(O_R\)。
2. 在 `wg0_subroutine` 里给这两行加中文注释：
   - [L786](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L786)：`// seesaw 步骤5: o_L += p0 @ V0L（LocalP, rs, 用本 warpgroup 寄存器里的 p0）`
   - [L810](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L810)：`// seesaw 步骤11: o_L += p1 @ V1L（RemoteP, ss, 读 wg1 写入 smem 的 sP1）`
3. 在 `wg1_subroutine` 里对称地给这两行加注释：
   - [L904](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L904)：`// seesaw 步骤8: o_R += p1 @ V1R（LocalP）`
   - [L918](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L918)：`// seesaw 步骤10: o_R += p0 @ V0R（RemoteP, 读 wg0 写入 smem 的 sP0）`
4. 给 softmax/scale 更新点加注释，标注它们属于哪一步、乘的是 \(s_0\) 还是 \(s_1\)：
   - `wg0_bunch_0` 的 Scale-O（[L374-377](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L374-L377)）：`// 步骤5 的 o_L *= s0`
   - `wg1_bunch_0` 的 Scale O（[L466-470](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L466-L470)）：`// 步骤8 的 o_R *= (s0*s1)`
   - `wg0_scale_rP0`（[L541-542](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L541-L542)）：`// 步骤9: p0 *= s1`
   - `wg0_rescale_rO0`（[L565-566](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L565-L566)）：`// 步骤11 的 o_L *= s1`

**需要观察的现象**：

- 四次 WGMMA（`localP` ×2 + `remoteP` ×2）正好覆盖 seesaw 的步骤 5、8、10、11；其余步骤（3、4、6、7、9）全是 CUDA Core 标量/逐元素运算。
- 每次 `remoteP` 调用之前，必定先有一次对端的 `save_rPb_to_sP`（stmatrix）和一次 `NamedBarrier::arrive(sP0Ready/sP1Ready 等价物)`。

**预期结果**：加完注释后，你能一眼看出「哪几行是 Tensor Core（WGMMA）、哪几行是 CUDA Core（softmax/rescale）、它们靠哪几个 NamedBarrier 串起来」。这就是 seesaw 调度的完整阅读地图。

> 说明：本实践只读不运行（注释改动不改变语义，仅用于学习）。若想真正验证正确性，应跑 `tests/test_flash_mla_dense_decoding.py`（见 u8-l2），但那需要 SM90 GPU。

#### 4.2.5 小练习与答案

**练习 1**：为什么四个 NamedBarrier 握手都是「arrive」配「arrive_and_wait」，而不是两边都 `arrive_and_wait`？

> **答案**：`NamedBarrier::arrive(num, id)` 只「敲门」不等待，`arrive_and_wait(num, id)` 敲门后还阻塞等对端也敲。设计上让**先完成数据准备的一方**只 arrive（不空等，继续干别的活，比如发射下一条 WGMMA），**需要消费该数据的一方**才 arrive_and_wait。这样把「数据生产者早早通知、消费者按需等待」做到极致，最大化重叠。例如 wg0 写完 `sScale0` 后只 `arrive(sScale0Ready)` 就去发步骤 5 的 GEMM，不等 wg1。

**练习 2**：主循环为什么步长是 2（`block_idx += 2`），而不是 1？

> **答案**：因为 seesaw 的 11 步一次消费**两个 KV 块**（\(K_0,V_0\) 与 \(K_1,V_1\)）：wg0 负责 block_idx，wg1 负责 block_idx+1。两个块的双缓冲刚好对应 smem 里的 `sK0`/`sK1`。步长 2 才能和这套「成对处理 + 双缓冲」的调度自洽。

---

### 4.3 细粒度 TMA 流水与 cache hint

#### 4.3.1 概念说明

即便 kernel 是 compute-bound（带宽不是瓶颈，见 u3-l1），也**不能无视访存延迟**：如果数据没就绪就开算，Tensor Core 只能干等。作者用两招掩盖 KV 的加载延迟：

1. **细粒度 TMA copy–GEMM 流水**：一个 \(64\times576\) 的 K 块**不一次性**搬，而是拆成 9 个 \(64\times64\) 子块逐个 TMA 搬。第 0 个子块一到，就先算它对应的 QK^T，边搬边算，给后面的子块留出到达时间。
2. **`EVICT_FIRST` cache hint**：告诉 L2「这块数据搬完就用、用完不再复用，请最先淘汰它」，反而提升了 L2 命中率（因为不让一次性流过的 KV 挤掉真正热的驻留数据）。

#### 4.3.2 核心流程

**细粒度 TMA 拷贝**由递归模板 `launch_kv_tiles_copy_tma<START, END>` 完成（[splitkv_mla.cuh:29-52](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L29-L52)）：从第 `START` 个 \(64\times64\) 子块拷到第 `END-1` 个，每个子块拷贝时绑定一个独立的 `barriers_K[START_HEAD_DIM_TILE_IDX]`，并在 `with(...)` 里挂上 `EVICT_FIRST`。调用 `launch_kv_tiles_copy_tma<0, 9>(...)` 就是「拷整个 K 块的 9 个子块」，`<0, 4>` 就是「只拷前 4 个」（用来填流水）。

为什么是 9？因为 `HEAD_DIM_K=576=9×64`（u3-l2 §4.1）。`576÷64=9` 这个整除关系让 TMA 分块数、barrier 数（`barriers_K0[9]`）、QK^T 的子 GEMM 数三处天然对齐。

**拷贝–计算流水**的逻辑在 `warpgroup_cooperative_qkt_gemm`（[splitkv_mla.cuh:196-254](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L196-L254)）：它把 Q 和 K 都切成 9 个 \((\text{BLOCK\_SIZE\_M},64)\)/\((\text{PAGE\_BLOCK\_SIZE},64)\) 子块，然后「等第 0 块 → 算第 0 块 → 等第 1 块 → 算第 1 块 → …」。靠 `PHASE_IDX` 模板参数把这个流水铺到 wg0/wg1 和相邻循环迭代上（见 4.3.3）。核心思想（注释见 [L181-187](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L181-L187)）：**先算最早到的块，给后到的块更多时间到达**，从而把访存和计算重叠。

#### 4.3.3 源码精读

**`launch_kv_tiles_copy_tma`** —— [splitkv_mla.cuh:36-52](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L36-L52)：`idx_in_warpgroup==0`（即每个 warpgroup 的 0 号线程）发起 TMA。关键行 [L47](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L47)：

```cpp
cute::copy(tma_K.with(reinterpret_cast<...&>(barriers_K[START_HEAD_DIM_TILE_IDX]), 0,
                       cute::TMA::CacheHintSm90::EVICT_FIRST), cur_gKV, cur_sKV);
```

这里 `tma_K.with(barrier, 0, EVICT_FIRST)` 把「拷贝完成时 arrive 这个 barrier」和「带上 EVICT_FIRST hint」两件事一次绑定。然后 `if constexpr (START+1 < END) launch_kv_tiles_copy_tma<START+1, END>(...)` 递归拷下一个子块。

**Q 的拷贝也用 `EVICT_FIRST`**：[splitkv_mla.cuh:691-710](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L691-L710) `launch_q_copy`，[L704](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L704) 同样挂 `EVICT_FIRST`。

**「拷一块算一块」的最小单元** —— `qkt_gemm_one_tile_sQ`（[splitkv_mla.cuh:122-145](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L122-L145)）：

- [L131-132](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L131-L132)：0 号线程 `barrier->arrive_and_expect_tx(64*64*2)`（告诉 mbarrier「预期收到 8192 字节」）。
- [L134](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L134)：所有线程 `barrier->wait(...)` 等这一块 K 到齐。
- [L138-142](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L138-L142)：对这一个子块发 4 条 WGMMA（`(_0.._3)` 是 K 维的 4 个 k_block），累加进 `rP`。

`qkt_gemm_one_tile_rQ`（[L153-178](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L153-L178)) 是同一套，只是 A 操作数换成寄存器里的 `rQ8`（Q 的第 8 块，u3-l2 §4.2 讲过为何要挪进寄存器）。两者由宏 `QKT_GEMM_ONE_TILE`（[L213-218](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L213-L218)）按 tile 编号分支。

**`warpgroup_cooperative_qkt_gemm` 的三相流水** —— [splitkv_mla.cuh:196-254](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L196-L254) 用 `PHASE_IDX ∈ {0,1,2}` 把「9 块的 QK^T」铺到两轮主循环迭代上：

| PHASE | 执行者 | 算哪些 tile | 作用 |
|-------|--------|-----------|------|
| 0 | wg0 | tile 0,1,2,3 | 本轮 K0 的前半段 QK^T（步骤 1 的前半） |
| 1 | wg1 | tile 4,5,6,7,8,0,1,2,3 | K1 全 9 块 + 翻转 phase（双缓冲 K0/K1） |
| 2 | wg0 | tile 4,5,6,7,8 | 本轮 K0 的后半段 QK^T |

注意 PHASE 1 里 wg1 把 tile 4~8 算完后又回头算 0~3，配合 [L241](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L241) 的 `cur_phase ^= 1` 翻转 barrier 相位——这是 smem 双缓冲 `sK0`/`sK1` 的经典相位翻转手法：同一组 `barriers_K0[9]` 在两轮里被复用，靠相位区分「这次到达」还是「上次到达」。

**一个反直觉的注释**：[splitkv_mla.cuh:940-943](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L940-L943) 解释了为什么 `cute::warpgroup_wait<0>()` 必须放在 `if` 外面：若放进去，NVCC 无法正确分析循环，会误以为在 WGMMA 流水里使用了累加寄存器，从而插入 `WARPGROUP.ARRIVE`/`WARPGROUP.DEPBAR.LE` 把 WGMMA 串行化。这种「为迁就编译器分析而调整代码位置」的细节，正是手工 WGMMA kernel 调优的日常。另外 [L56-75](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L56-L75) 还有一个 `prefetch_kv_tiles`，注释明说「currently not used because it leads to performance degradation」—— prefetch 在这里反而拖慢，是实验出来的结论。

> 关于 `EVICT_FIRST` 为什么提升 L2 命中率：KV 在 decode 里是「流式读一次」的数据，不会复用。若不标 `EVICT_FIRST`，它会按 LRU 正常驻留，把别的请求还在用的热数据挤出去；标了之后，它一用完就被优先淘汰，给热数据腾位。博客 [docs/20250422-new-kernel-deep-dive.md:55](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250422-new-kernel-deep-dive.md#L55) 明确说这是「shown by experiments」验证过的优化。

#### 4.3.4 代码实践

**目标**：验证「9 个 TMA 子块 + 逐块 GEMM」的流水结构，并定位 `EVICT_FIRST` 的所有出现处。

**操作步骤**：

1. 数 `launch_kv_tiles_copy_tma<0, 9>` 在 kernel 里的调用（如 [L1082](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L1082)、[L795](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L795)、[L822](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L822)、[L926](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L926)、[L932](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L932)），确认 `<0, 4>`（填流水前 4 块）与 `<0, 9>`/`<4, 9>`（搬剩余块）的搭配。
2. 在 `qkt_gemm_one_tile_sQ` 里确认「等一块 → 算一块」的 wait/gemm 配对（[L132](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L132) wait、[L138-142](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L138-L142) gemm）。
3. 全文搜 `EVICT_FIRST`，数它出现几次（答案：2 处，TMA K 拷贝与 TMA Q 拷贝）。

**需要观察的现象**：

- 每个 `barriers_K0[i]`（i=0..8）只对应一个 \(64\times64\) 子块，子块一到 barrier 就 release，GEMM 立刻能算这一块——这就是「拷完一块就开算」。
- `<0, 4>` 这种「先搬一半」的调用出现在主循环开头（填流水线），`<4, 9>` 出现在循环里（补搬后半），二者配合实现稳定的逐块重叠。

**预期结果**：你理解了「为什么是 9 个 TMA barrier、为什么 GEMM 能在 TMA 还没搬完时就启动」。这正是博客「fine-grained TMA copy - GEMM pipelining」在源码里的落地。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `launch_kv_tiles_copy_tma` 改成「一次性 TMA 搬整个 \(64\times576\) 块、只用 1 个 barrier」，性能会怎样？

> **答案**：会变慢。因为 GEMM 必须等整个 576 列全到齐才能开始，访存延迟无法被计算掩盖。细粒度拆成 9 份后，第 0 份（64 列）一到就能先算，后续 8 份在算第 0 份的同时陆续到达，访存和计算重叠。这就是「fine-grained」的含义。

**练习 2**：`EVICT_FIRST` 用在 Q 和 K 上，但**没有**用在输出 O 的 TMA store 上（见 `store_o` 里的 `tma_O` 拷贝，[L652-656](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L652-L656)）。为什么？

> **答案**：`EVICT_FIRST` 是**读**缓存策略（控制搬进 smem 时 L2 如何保留该 cacheline）。输出 store 是写操作，走的策略不同；而且 O 写完就读走，无需特别 hint。cache hint 主要服务于「反复读 global→smem」的 KV/Q 流式加载。

---

## 5. 综合实践

把三大模块串起来，画一张「seesaw + 双 warpgroup + TMA 流水」的**联合时间线图**（纸笔练习，无需 GPU）：

1. 横轴是时间，纵轴分两行：wg0、wg1，再单独留一行标「TMA 引擎」。
2. 在 wg0 行画出：QK^T（PHASE 0/2 的 WGMMA）→ `wg0_bunch_0`（CUDA Core softmax）→ localP GEMMA（步骤 5）→ `wg0_scale_rP0`+`save_rPb_to_sP` → remoteP GEMMA（步骤 11），中间用箭头标出 4 个 NamedBarrier 握手。
3. 在 wg1 行对称画出步骤 6/7/8（含 `wg1_bunch_0`）、remoteP 步骤 10。
4. 在 TMA 行画出 `launch_kv_tiles_copy_tma<0,4>` 与 `<4,9>` 的 9 个子块拷贝，用虚线连到 wg0/wg1 里对应的 `qkt_gemm_one_tile_*`，体现「拷一块算一块」。
5. 在每个 TMA 拷贝上标 `EVICT_FIRST`。

**交付物**：一张能同时解释三件事的图——(a) 两个 warpgroup 如何靠 4 次 NamedBarrier 握手完成 \(p_0,p_1,s_0,s_1\) 的交换；(b) wg0 的 WGMMA 与 wg1 的 softmax 在时间上如何错开重叠；(c) 9 个 TMA 子块如何逐个喂给 QK^T。

**自检**：画完后，你应当能指着图回答——「为什么 \(o_L\) 要分两次 rescale、\(o_R\) 只 rescale 一次？」「为什么有 9 个 barrier 而不是 1 个？」「`EVICT_FIRST` 加在哪两类拷贝上？」如果三问都能答上，本讲就通了。

## 6. 本讲小结

- **seesaw 是「只用一份输出矩阵的 ping-pong 变体」**：因寄存器只放得下一份 \(64\times512\) 的 O，作者把 O 竖切成 \(O_L/O_R\) 分给两个 warpgroup，再设计 11 步数学变换，证明它与标准 FA online softmax **逐项等价**（4.1.2 给了完整证明）。
- **关键不对称**：\(o_L\) 分两次 rescale（乘 \(s_0\)、再乘 \(s_1\)），\(o_R\) 合并一次 rescale（乘 \(s_0 s_1\)）；步骤 9 把 \(p_0\) 提前乘 \(s_1\)。这些不对称都是为了错开两个 warpgroup 的 softmax、最大化两种 Core 的重叠。
- **两个 warpgroup 的交错**由 `wg0_subroutine`/`wg1_subroutine` 承担，靠 4 个 NamedBarrier 握手（`sScale0Ready`/`sScale1Ready`/`sP0Ready`/`rO1sP0sV0RIssued`）交换 \(s_0,s_1,p_0\) 与 smem 复用权；每个 warpgroup 各做两次 WGMMA（localP 用自己的 P、remoteP 用对方的 P）。
- **细粒度 TMA 流水**把 \(64\times576\) 的 K 块拆成 9 个 \(64\times64\) 子块逐个搬，`qkt_gemm_one_tile_*` 拷一块算一块，掩盖访存延迟；9 这个数来自 `HEAD_DIM_K/64`。
- **`EVICT_FIRST` cache hint** 加在 TMA 读 Q/K 上，让流式数据用完即淘汰，反而提升 L2 命中率（实验验证）。
- 代码里多处「为迁就 NVCC 分析/避免 register spill」的细节（如 [L940-943](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L940-L943) 的 `warpgroup_wait` 位置、[L1121](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L1121) 防溢出的 `if`），是手工 WGMMA kernel 调优的典型痕迹。

## 7. 下一步学习建议

本篇讲完了 **kernel 内部**的 seesaw 调度与 TMA 流水，但还有两块「外延」没碰：

1. **接口层如何把请求喂进这个 kernel**：`block_idx`/`end_block_idx`/`num_sm_parts` 从哪来？`o_accum`/`lse_accum` 这些 split-KV 缓冲怎么挂？这就是下一篇 **u3-l4「Dense decode 接口与 split-KV 编排」** 的内容——它讲 `csrc/api/dense_decode.h` 如何做张量校验、head 维重排、分配 split-KV 缓冲、生成调度元数据并调用 combine。建议接着读 u3-l4，把「kernel 内」和「kernel 外」拼成完整闭环。
2. **split-KV 与 combine**：本篇 kernel 里 `is_no_split` 分支（[L1230](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L1230)）决定写 `o_ptr` 还是 `oaccum_ptr`，但「为什么要把长 KV 切成多 split、最后怎么归并」要等到 **u4（Split-KV、Combine 与 Tile Scheduler）** 才展开。本篇看到的 `num_splits_ptr`、`softmax_lseaccum_ptr` 就是 u4 的伏笔。

如果想再深入 seesaw 的数学，可重读博客 [docs/20250422-new-kernel-deep-dive.md:25-42](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250422-new-kernel-deep-dive.md#L25-L42) 并对照本篇 4.1.2 的证明；想理解 WGMMA fragment 在线程里的排布（`get_AorC_row_idx`、`local_row_idx` 那套），可参考 PTX 文档 [wgmma-64n16-a](https://docs.nvidia.com/cuda/parallel-thread-execution/#wgmma-64n16-a)（代码注释里给了链接）。
