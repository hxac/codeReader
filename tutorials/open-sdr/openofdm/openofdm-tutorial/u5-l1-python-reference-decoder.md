# Python 参考解码器 decode.py

## 1. 本讲目标

本讲进入 OpenOFDM 手册的「验证单元」。前面几单元我们一直在读 Verilog 硬件解码器，本讲换一个视角：**项目自带的浮点参考解码器 `scripts/decode.py`**。

读完本讲，你应当能够：

1. 说清 `decode.py` 在整个项目里扮演的角色——它是硬件 RTL 的「标准答案」，用于逐阶段交叉验证。
2. 跟着 `decode.Decoder.decode_next()` 把一个 802.11 包从 I/Q 样本一路解到字节，记住它返回的 8 元组「期望输出」结构。
3. 理解浮点前端 `ChannelEstimator` 如何用 LTS 做频偏校正与信道均衡，并用导频做残余频偏细校。
4. 厘清 `decode.py` 与第三方库 `commpy` 的真实关系：卷积码用 `commpy`，解调却是自己写的。
5. 知道 `LONG_PREAMBLE_TXT` / `SHORT_PREAMBLE_TXT` / `LTS_REF` 这些「参考数据」的来源与用途。

本讲承接 u1-l2（仿真运行）与 u3-l5（ofdm_decoder 子流水线 / 卷积解码），是下一讲 u5-l2（交叉验证框架 `test.py`）的直接前置。

## 2. 前置知识

在进入源码前，先用通俗语言把几个概念讲清楚。

**什么是「参考实现（reference implementation）」？** 当我们用 Verilog 写一个解码器，里面塞满了定点乘法、查表、流水线寄存器，很容易在某一拍算错。要确认它对不对，最稳的办法是另写一个「逻辑相同、但实现方式完全不同」的程序，把同样的输入喂进去，对比每一步的输出。这个对照程序就是参考实现。OpenOFDM 的参考实现用 Python + numpy 的**浮点双精度**写成，几乎没有定点误差，逻辑也直接对照 802.11 标准，所以可以当「标准答案」。

**为什么用浮点？** Verilog 里一切是整数（例如把幅度归一化到 1024，相位放大 2⁹ 倍），而 Python 用 `complex` 复数双精度浮点，省去了所有定点缩放与对齐。两者逻辑等价，只是数值表示不同。这也是为什么 `decode.py` 里看不到 `CONS_SCALE_SHIFT` 这类定点常数——它直接用真实的复数运算。

**什么是 `commpy`？** `commpy` 是一个开源的「通信算法」Python 库（作者 Veeresh Taranalli，BSD 协议）。OpenOFDM 在 `scripts/commpy/` 下**整包内置（vendored）**了一份修改版，避免外部依赖漂移。其中 `commpy.channelcoding.convcode` 提供了通用的卷积码网格（Trellis）与 Viterbi 译码器，`decode.py` 直接复用它做卷积解码。注意：`commpy` 还自带 `modulation.py`（PSK/QAM 调制解调器），但 `decode.py` **并没有**用它——这点后面会专门讲。

**两个关键术语回顾（来自前置讲义）：**

- **LTS（长训练序列）**：802.11 前导里两段完全相同的 64 样本序列，接收端用它做符号定时、信道估计、细频偏校正（见 u2-l4、u3-l1）。
- **Viterbi / 卷积码**：802.11 用约束长度 7、生成多项式 133/171（八进制）的 1/2 率卷积码，发射端编码、接收端用 Viterbi 算法译码（见 u3-l5）。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [scripts/decode.py](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py) | 浮点参考解码器主体，含 `Decoder`、`ChannelEstimator`、`Demodulator`、`Signal`、`HTSignal` 五个类 |
| [scripts/commpy/channelcoding/convcode.py](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/commpy/channelcoding/convcode.py) | 内置 `commpy` 的卷积码模块，提供 `Trellis` 与 `viterbi_decode`，被 `decode.py` 调用 |
| [scripts/commpy/modulation.py](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/commpy/modulation.py) | 内置 `commpy` 的调制模块（`Modem`/`QAMModem`）；**`decode.py` 未使用它**，本讲用于对比说明 |
| [scripts/test.py](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py) | 交叉验证框架（下一讲 u5-l2 的主角），本讲引用它来说明「期望输出」如何被消费 |

> ⚠️ 运行环境提示：`decode.py` 是 **Python 2** 代码（见文首 `from cStringIO import StringIO` 与大量 `print "..."` 语句），并依赖 `numpy`、`scipy` 与 `wltrace`（见 [requirements.txt](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/requirements.txt)，`wltrace==1.1.1`）。`wltrace` 不在标准库，需要单独安装（`pip install wltrace`）。本讲环境的解释器为 Python 3，**无法直接运行** `decode.py`，因此后续实践中的具体数值输出标注为「待本地验证」。

## 4. 核心概念与源码讲解

本讲按 4 个最小模块拆分：①整体定位与运行入口 `decode_next`；②浮点前端 `ChannelEstimator`；③解调 / 解交织 / 卷积解码（含 `commpy` 关系）；④期望输出结构与参考数据。

### 4.1 参考解码器的整体定位与运行入口

#### 4.1.1 概念说明

`decode.py` 的核心是 `Decoder` 类，它把一段 I/Q 采样「按 802.11 规则」完整解成字节。它的定位非常明确：

- **输入**：和 Verilog 测试台一样，是从 USRP 抓下来的 32 位 I/Q 样本（高 16 位 I、低 16 位 Q），20 MSPS 采样率。
- **输出**：一个 8 元组，包含 SIGNAL 字段、每一步中间结果（星座点、解调比特、解交织比特、卷积解码比特、解扰比特）和最终字节。
- **用途**：这些中间结果就是后续 `test.py` 拿去和 Verilog 仿真输出逐文件比对的「标准答案」。

它和硬件 `dot11.v` 的关系是**逻辑镜像**：两者都走「检测 → 频偏校正 → FFT → 信道估计 → 解调 → 解交织 → 卷积解码 → 解扰」八步流水线（见 u1-l5），只是 `decode.py` 用浮点 numpy，`dot11.v` 用定点 Verilog。换句话说，读 `decode.py` 是理解 802.11 解码算法**最直白**的一条路径——没有流水线寄存器、没有 strobe 握手、没有定点缩放，全是数学公式。

#### 4.1.2 核心流程

`Decoder` 对外暴露两个层次：

```text
decode_next()        # 扫描整段样本，找到第一个包，返回 (起始位置, 期望输出...)
   └── decode(samples)   # 解一个已知起始的包，返回 8 元组期望输出
         ├── ChannelEstimator(samples)   # 频偏校正 + 信道估计
         ├── demodulate(carriers)        # 解调 → 比特
         ├── deinterleave(bits)          # 解交织
         ├── viterbi_decode(bits)        # 卷积解码（调 commpy）
         ├── Signal(bits)                # 解析 SIGNAL 字段
         ├── [若 rate==6] 探测 HT-SIG    # 区分 11a / 11n
         ├── 循环解 num_symbol 个数据符号
         ├── descramble(bits)            # 解扰
         └── 拼字节、跳过 SERVICE
```

`decode_next()` 的职责是「在原始样本流里定位包」：它一边读样本，一边用一个粗功率门限（`power_thres`）等信号到来；一旦信号结束（连续一段都低于门限），就用 LTS 互相关精确定位包起点，然后交给 `decode()`。

#### 4.1.3 源码精读

**文件头与依赖**——一眼看清它是 Python 2 且依赖 `commpy`、`wltrace`：

[scripts/decode.py:10-14](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L10-L14) —— `cStringIO`、`print` 语句确认 Python 2；`import commpy.channelcoding.convcode as cc` 说明卷积解码交给 `commpy`；`from wltrace import dot11` 引入帧解析工具（`dot11.Dot11Packet`、`dot11.mcs_to_rate`）。

**`decode_next` 的功率门限触发**：

[scripts/decode.py:436-458](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L436-L458) —— 每次读 `window`（默认 80）个样本为一个 chunk；只要 chunk 内**任意**一个样本的幅度 `abs(c) > power_thres`（默认 200），就置 `trigger=True` 开始收集样本；当某个 chunk **全部**样本都低于门限时，认为包结束，调用 `find_pkt()` 精确定位。这和硬件 `power_trigger.v`（u2-l1）的「门限触发 + 连续低样本解除」思路一致，只是这里用浮点幅度且窗口是整 chunk。

**`find_pkt` 用 LTS 互相关定位包起点**：

[scripts/decode.py:460-471](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L460-L471) —— 取理想 LTS 的后 64 个样本作模板，对收到的样本做滑动互相关（`np.correlate`），找最大的两个峰；若两峰正好相隔 64（LTS 的两段相同序列）、且第一峰离起点足够远，就回推 `min(peaks)-32-160` 得到 STS 的第一个样本索引。这正是硬件 `sync_long.v`（u2-l4）「LTS 双峰定位」的浮点版。

**`decode` 主链路与 8 元组返回**：

[scripts/decode.py:643-656](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L643-L656) —— 先建 `ChannelEstimator`，取第一个 OFDM 符号（SIGNAL）的 48 个子载波，依次 demodulate → deinterleave → viterbi_decode，再 `Signal(bits)` 解析出 rate/length/parity。

[scripts/decode.py:727-730](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L727-L730) —— `decode()` 最终返回的 8 元组：

```python
return signal, cons, demod_out, deinter_out, conv_out, descramble_out, data_bytes, pkt
```

而 `decode_next` 在前面再拼一个包起始位置（`glbl_index`）：

[scripts/decode.py:457-458](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L457-L458) —— `return (glbl_index, ) + self.decode(samples[start:], ...)`，所以 `decode_next()` 实际返回 **9 元组**。下一讲的 `test.py` 正是按 9 个名字解包它的（`begin, expected_signal, cons, expected_demod_out, ...`）。

#### 4.1.4 代码实践

**实践目标**：用最少的代码把 `decode_next()` 跑起来，打印它返回的 9 元组的结构，确认「期望输出」都拿到了。

**操作步骤**：

1. 准备 Python 2 环境：`pip install numpy==1.11.2 scipy wltrace==1.1.1`（参考 [requirements.txt](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/requirements.txt)）。
2. 在仓库根目录写一个最小驱动脚本 `run_ref.py`（**示例代码，非项目原有文件**）：

   ```python
   # 示例代码：调用浮点参考解码器并打印各阶段长度
   import sys
   sys.path.insert(0, "scripts")
   import decode

   sample = "testing_inputs/conducted/dot11a_24mbps_qos_data_e4_90_7e_15_2a_16_e8_de_27_90_6e_42.dat"
   result = decode.Decoder(sample, skip=0).decode_next()
   # result 是 9 元组
   begin, signal, cons, demod, deinter, conv, descramble, data_bytes, pkt = result
   print("packet begins at sample", begin)
   print("signal.rate =", signal.rate, "length =", signal.length, "ht =", signal.ht)
   print("len(cons)        =", len(cons))      # 星座点（复数）个数
   print("len(demod)       =", len(demod))     # 解调比特数
   print("len(deinter)     =", len(deinter))   # 解交织比特数
   print("len(conv)        =", len(conv))      # 卷积解码比特数
   print("len(descramble)  =", len(descramble))# 解扰比特数
   print("len(data_bytes)  =", len(data_bytes))# 输出字节数
   print("first bytes =", [format(b, '02x') for b in data_bytes[:8]])
   ```

3. 运行：`python2 run_ref.py`（必须在仓库根目录，因为样本用相对路径）。

**需要观察的现象**：

- 对 24 Mbps 的 802.11a 包，`signal.rate` 应为 `24`，`signal.ht` 应为 `False`。
- `data_bytes` 的长度应等于 `signal.length`（SIGNAL 字段里的 LENGTH，单位字节）。
- 各阶段长度应当满足：解调比特 ÷ `n_cbps` = OFDM 符号数；卷积解码比特 ÷ `n_dbps` 也是符号数（24 Mbps 下 `n_bpsc, n_cbps, n_dbps = 4, 192, 96`，见下文 4.3 节的 `RATE_PARAMETERS`）。

**预期结果**：脚本应当顺利打印出 9 元组解包后的各项长度。**具体数值待本地验证**（本讲环境为 Python 3，无法运行 Python 2 的 `decode.py`）。

> 若暂时装不了 Python 2，可以先用 Python 3 读样本确认数据本身可读（**示例代码**）：`import numpy as np; x=np.fromfile(sample, dtype=np.int16); c=[complex(i,q) for i,q in zip(x[::2],x[1::2])]; print(len(c))`。这只验证「样本是一条复数流」，不解码。

#### 4.1.5 小练习与答案

**练习 1**：`decode_next()` 返回几个元素？为什么比 `decode()` 多一个？
**答案**：9 个。`decode_next` 在 `decode()` 的 8 元组前再拼了一个包起始样本索引 `glbl_index`（见 [decode.py:457-458](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L457-L458)），告诉调用方「这个包在原始样本流里从第几个样本开始」。

**练习 2**：`decode_next` 里既已经有功率门限触发，为什么还要再调一次 `find_pkt` 做 LTS 互相关？
**答案**：功率门限只能粗略判断「信号来了」，给出的起点精度是一个 chunk（最多 80 样本）；而 FFT 要求 OFDM 符号起点对齐到几个样本之内，否则子载波间会泄漏。`find_pkt` 用 LTS 互相关找双峰，把起点精确到样本级（见 [decode.py:460-471](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L460-L471)），这正是硬件 `sync_long.v` 干的活。

---

### 4.2 浮点信号处理前端 ChannelEstimator

#### 4.2.1 概念说明

`ChannelEstimator` 是 `decode.py` 里最「通信味」的一个类，对应硬件流水线里的 sync_short（频偏）+ sync_long（FFT/定时）+ equalizer（信道均衡）三段的合并。它一次性完成：

1. **频偏校正**：估计收发本振不一致造成的载波频率偏移（CFO），并把每个样本反向旋转掉它（对应 u2-l3）。
2. **信道估计**：用两段 LTS 估计每个子载波的复增益 \(H[k]\)（对应 u3-l1）。
3. **逐符号均衡 + 导频细校**：对每个数据 OFDM 符号做 FFT，用 4 个导频子载波估计残余频偏并旋转补偿，再除以 \(H[k]\) 还原发射星座点。

它的存在说明了一件事：**浮点参考实现把硬件里分散在多个模块、用定点和查表实现的算法，浓缩成了一个不到 130 行的 Python 类**。读它等于读「纯算法版」的 OpenOFDM 前端。

#### 4.2.2 核心流程

```text
ChannelEstimator.__init__(samples)
   ├── fix_freq_offset()        # 估计 coarse + fine CFO，整段样本反向旋转
   │     coarse = ∠Σ sts[i]·conj(sts[i+16]) / 16      # STS 延迟 16 自相关
   │     fine   = ∠Σ lts[i]·conj(lts[i+64]) / 64      # LTS 延迟 64 自相关
   ├── FFT(lts1), FFT(lts2)     # 两段 LTS 各做 64 点 FFT
   └── gain[c] = (lts1[c]+lts2[c])/2 · LTS_REF[c]      # 信道估计 H[c]（含 LTS 参考符号）

next_symbol()  # 每次吐一个 OFDM 符号的 48（legacy）/52（HT）个数据子载波
   ├── FFT(去掉 GI 的 64 样本)
   ├── 导频极性校正
   ├── beta = ∠Σ pilot[c]·conj(gain[c])                # 残余 CFO（导频细校）
   ├── symbol[c] *= exp(j·beta)                        # 旋转补偿
   └── symbol[c] /= gain[c]                            # 除以信道 → 均衡
```

**粗频偏公式**：STS 每 16 个样本重复一次，若存在频偏 \(f\)，则相邻 16 样本之间会引入相位 \(16\cdot 2\pi f / f_s\)。对 STS 做延迟 16 的共轭自相关并取相角，再除以 16，就得到单样本相位旋转量 \(\alpha\)：

\[
\alpha_{\text{coarse}} = \frac{1}{16}\,\angle\!\left(\sum_i s[i]\,\overline{s[i+16]}\right)
\]

这与硬件 `sync_short.v` 里 \(\alpha_{ST}=\frac{1}{16}\angle(\cdot)\)（u2-l2）完全一致——只不过硬件用定点 atan 查表，这里用 `cmath.phase`。

**信道估计公式**：设接收 LTS 频域为 \(Y[k]\)，发射 LTS 已知为 \(X[k]\)（即 `LTS_REF`，取 ±1），则

\[
H[k] = Y[k]\cdot X[k] \quad(\text{因为 } X[k]=\pm 1,\ X[k]^{-1}=X[k])
\]

代码里写的是 `gain[c] = (lts1[c]+lts2[c])/2 * LTS_REF[c]`，两段 LTS 取平均是为了降噪；乘 `LTS_REF` 而非除，正是利用了 \(X[k]=\pm 1\) 的性质。

**均衡**：之后每个数据符号 \(Z[k]\) 除以 \(H[k]\) 即得发射星座点估计 \(\hat{X}[k]=Z[k]/H[k]\)。硬件 `equalizer.v`（u3-l1）用「乘共轭 + 实数除法」避免复数除法，这里浮点直接 `/`，最直白。

#### 4.2.3 源码精读

**`fix_freq_offset`：粗 + 细频偏校正**

[scripts/decode.py:267-292](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L267-L292) —— 注意几个关键下标：

- `sts = self.samples[80:160]` —— STS 段（160 个 STS 样本里取后 80）。
- `lts = self.samples[160+32:160+160]` —— LTS 段，跳过 32 个 GI（循环前缀），取 128 个样本（两段 64）。
- 粗校正在 STS 上做（延迟 16），细校正在粗校正后的 LTS 上做（延迟 64）。
- 最后 `self.data_samples = [c*exp(j·n·freq_offset) ...]` 把「数据段」整段按样本序号 \(n\) 累积旋转，抵消频偏——等价于硬件 `sync_long` 里 `rotate(m·α)` 的逐样本旋转。

> 一个有意思的细节：[decode.py:283](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L283) 有 `fine_offset = 0` 把细频偏强制清零。这与硬件 OpenOFDM「跳过 LTS 细 CFO、改用导频逐符号跟踪」的设计取舍一致（见 u2-l3 讲义）。也就是说，参考实现主动放弃了基于 LTS 的细 CFO，只保留粗 CFO + 导频细校，以保证和硬件行为对齐。

**信道估计 `gain`**

[scripts/decode.py:196-197](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L196-L197) —— `gain = {(c, (lts1[c]+lts2[c])/2*LTS_REF[c]) for c in subcarriers}`，就是上面 \(H[k]\) 公式的直接翻译。

**`next_symbol`：FFT + 导频极性 + 残余频偏 + 均衡**

[scripts/decode.py:201-235](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L201-L235) ——

- [decode.py:202-207](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L202-L207)：取 64 样本做 FFT，注意它先跳过 GI：`short_gi` 时跳 8，否则跳 16（普通 GI）。`self.idx += 72`（short_gi）或 `+= 80`（普通），对应一个 OFDM 符号长度 64+GI。
- [decode.py:218-219](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L218-L219)：导频极性翻转——802.11 规定 4 个导频（\(-21,-7,7,21\)）的极性逐符号按 `polarity` 表变化，接收端要乘回去去掉这层「随机性」。
- [decode.py:222-226](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L222-L226)：残余 CFO 估计——把 4 个导频乘上各自的信道增益 `gain[c]` 求和取相角 `beta`。这对应硬件 `equalizer` 里「导频细校」那段（u3-l1）。
- [decode.py:228-234](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L228-L234)：对每个数据子载波先 `*= exp(j·beta)`（旋转补偿残余 CFO），再 `/= gain[c]`（均衡），收集成 `carriers` 列表返回。

**`switch_ht`：切换到 11n 的 52 子载波**

[scripts/decode.py:237-260](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L237-L260) —— 当确认是 802.11n 包时调用，把 `subcarriers` 从 52 个（\(-26..-1,1..26\)）改成 56 个（\(-28..-1,1..28\)，其中 52 数据 + 4 导频），并用 HT-LTS 重新估信道。注意 [decode.py:251](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L251) 同样有 `ht_offset = 0`，把 HT 频偏清零——和硬件保持一致。

**`do_fft`：64 点 FFT 并按子载波索引取值**

[scripts/decode.py:262-265](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L262-L265) —— `np.fft.fft` 后用 `FFT_MAPPING` 把「负频率子载波」映射到 FFT 输出的高半段（`c if c>0 else 64+c`）。这正是硬件 FFT IP 输出的标准排布。

#### 4.2.4 代码实践

**实践目标**：把 `ChannelEstimator` 单独拎出来，观察它对一个真实包算出的频偏和信道增益，建立「浮点前端到底吐出什么」的直觉。

**操作步骤**：

1. 仍然需要 Python 2 环境。
2. 写一个驱动（**示例代码**）：

   ```python
   # 示例代码：单独观察 ChannelEstimator
   import sys, numpy as np, array
   sys.path.insert(0, "scripts")
   import decode

   path = "testing_inputs/conducted/dot11a_24mbps_qos_data_e4_90_7e_15_2a_16_e8_de_27_90_6e_42.dat"
   # 读原始 int16 I/Q → 复数流
   with open(path, 'rb') as f:
       raw = array.array('h', f.read())
   samples = [complex(i, q) for i, q in zip(raw[::2], raw[1::2])]

   # 用 find_pkt 找包起点（需要先实例化一个 Decoder 或直接复用静态思路）
   dec = decode.Decoder(path, skip=0)
   start = dec.find_pkt(samples)            # LTS 互相关定位
   print("packet start sample =", start)
   eq = decode.ChannelEstimator(samples[start:])   # 会打印 COARSE/FINE/FREQ OFFSET
   print("num subcarriers =", len(eq.subcarriers)) # legacy 应为 52
   print("first symbol has", len(eq.next_symbol()), "data carriers")  # 应为 48
   ```

3. 运行：`python2 run_eq.py`。

**需要观察的现象**：

- 终端会打印 `[COARSE OFFSET] ...`、`[FINE OFFSET] ...`、`[FREQ OFFSET] ...` 三行（来自 [decode.py:273/282/289](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L273-L289) 的 `print`）。
- `len(eq.subcarriers)` 应为 52（legacy），`next_symbol()` 返回长度应为 48（52 − 4 导频）。

**预期结果**：频偏是一个很小的弧度值（典型 \(\sim 10^{-3}\) 量级），`next_symbol()` 返回 48 个复数。**具体数值待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `gain` 的计算是 `* LTS_REF[c]` 而不是 `/ LTS_REF[c]`？
**答案**：因为 LTS 参考符号 `LTS_REF[c]` 只取 \(\pm 1\)（见 [decode.py:125-129](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L125-L129)），其倒数等于自身，乘和除等价；乘法更省事。其物理意义是 \(H[k]=Y[k]\cdot X[k]^{-1}=Y[k]\cdot X[k]\)。

**练习 2**：`next_symbol` 里 `self.idx += 80`（普通 GI）和 `+= 72`（short GI）的差 8 来自哪？
**答案**：一个 OFDM 符号 = 64 数据样本 + GI（循环前缀）。普通 GI 长度 16，所以符号长 80；短 GI 长度 8，符号长 72。FFT 时都取后 64 个数据样本（[decode.py:203-206](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L203-L206)），只是跳过的 GI 头长度不同（16 vs 8）。

---

### 4.3 解调 / 解交织 / 卷积解码（含 commpy 关系）

#### 4.3.1 概念说明

这一段对应硬件 `ofdm_decoder.v` 的子流水线（demodulate → deinterleave → viterbi → descramble，见 u3-l5、u3-l6）。`decode.py` 把它们实现成 `Decoder` 的几个方法，外加一个独立的 `Demodulator` 类。

**关于「commpy 关系」的一个重要事实**：`commpy` 这个内置库同时提供了卷积码（`convcode.py`）和调制解调（`modulation.py`，里面有通用的 `Modem.demodulate` 硬判决/软判决 API）。但 `decode.py` **只用了卷积码部分**：

```python
import commpy.channelcoding.convcode as cc   # 只导入这一项
```

解调完全是 `decode.py` 自己写的 `Demodulator` 类（[decode.py:295-348](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L295-L348)），**没有调用 `commpy.modulation`**。原因有二：一是 `decode.py` 的解调要把星座点按特定格雷码比特序输出，方便和 Verilog 的 `demodulate.v`（u3-l3）逐比特对齐；二是参考实现需要完全掌控比特排列，复用通用 `Modem` 反而要适配。所以「commpy 调制」在本项目里是**备而未用**——它存在于内置库里，但参考解码器走的是自研解调。

#### 4.3.2 核心流程

**解调（`Demodulator.demodulate`）**：用最小欧氏距离做硬判决。对每个星座点 \(z\)，缩放后找最近的理想星座点，再把该点的索引展开成 `bits_per_sym` 比特：

\[
\text{idx} = \arg\min_k |z\cdot\text{scale} - C_k|,\quad \text{bits} = \text{binary}(\text{idx})
\]

其中 `scale` 把外层幅度对齐到星座最外层（BPSK/QPSK=1，16-QAM=3，64-QAM=7）。这和硬件 `demodulate.v`「归一化到 MAX=1024 后比门限」本质相同（见 u3-l3），只是浮点版用最近邻、硬件用门限。

**解交织（`deinterleave`）**：标准 802.11 两步置换（first_perm + second_perm），按 `n_cbps`（每符号编码比特数）分块重排。legacy 与 HT 的列数（`n_col`）不同：legacy=16，HT=13。

**卷积解码（`viterbi_decode`）**：

1. **去穿孔（de-puncture）**：非 1/2 率（3/4、2/3、5/6）要在被发射端删掉的位置补回「空比特」，代码里用整数 `2` 作「未知/erase」标记。
2. **调 commpy**：构造 Trellis，调 `cc.viterbi_decode(..., tb_depth=35)`。
3. **裁尾**：去掉最后 7 个尾比特。

**解扰（`descramble`）**：用前 7 个接收比特做「直装法」初始化 7 级 LFSR，再逐比特异或还原（与 u3-l6 完全对应）。

#### 4.3.3 源码精读

**速率参数表（Table 78）**

[scripts/decode.py:94-104](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L94-L104) —— `RATE_PARAMETERS`：`rate -> (n_bpsc, n_cbps, n_dbps)`。例如 24 Mbps → `(4, 192, 96)`：每子载波 4 比特（16-QAM），每符号 192 编码比特（48 数据子载波 × 4），每符号 96 数据比特（1/2 率卷积码）。HT 版本在 [decode.py:106-115](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L106-L115)，子载波数换成 52。

**自研解调器 `Demodulator`**

[scripts/decode.py:314-348](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L314-L348) —— `__init__` 按 rate/mcs 选 BPSK/QPSK/16-QAM/64-QAM，构造理想星座点数组 `cons_points` 并记 `scale`；`demodulate` 用 `np.argmin(abs(sym*scale - cons_points))` 做最近邻硬判决，再把索引格式化成比特串。注意 [decode.py:296-312](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L296-L312) 的 `QAM16_MAPPING` / `QAM64_MAPPING` 显式写了格雷码到幅度等级的映射，这就是它不复用 `commpy.modulation` 的关键——比特排列必须和 Verilog 严格一致。

> 对比 `commpy.modulation`：[scripts/commpy/modulation.py:49-94](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/commpy/modulation.py#L49-L94) 的 `Modem.demodulate` 也是 `argmin(|sym - constellation|)` 最近邻硬判决，思路一致；但它的星座由 `symbol_mapping=arange(m)` 决定（自然序），而 `decode.py` 要的是特定格雷序，所以另写一份。

**解交织 `deinterleave`**

[scripts/decode.py:480-523](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L480-L523) —— 两次置换：

```python
first_perm[j]  = (s*(j/s)) + ((j + n_col*j/n_cbps) % s)
second_perm[i] = n_col*i - (n_cbps-1)*(i/n_row)
out_bits[base+second_perm[first_perm[j]]] = in_bits[base+j]
```

`n_col`、`n_row`、`s` 都来自 802.11 标准（legacy：`n_col=16, n_row=3*n_bpsc`；HT：`n_col=13, n_row=4*n_bpsc`）。这是硬件 `deinterleave.v` 查表（`deinter_lut`）背后的那张表的「生成公式」——见 u3-l4 与 `scripts/gen_deinter_lut.py`（u5-l4）。

**卷积解码 `viterbi_decode`：去穿孔 + commpy**

[scripts/decode.py:525-573](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L525-L573) ——

- [decode.py:532-569](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L532-L569)：按码率分四种去穿孔模式（3/4、2/3、5/6、无）。被补回的位置填 `2`（erase）。
- [decode.py:571-573](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L571-L573)：

  ```python
  extended_bits = np.array([0]*2 + bits + [0]*12)               # 前补 2、后补 12 个 0
  trellis = cc.Trellis(np.array([7]), np.array([[0133, 0171]])) # 1/2 率、生成多项式 133/171（八进制）
  return list(cc.viterbi_decode(extended_bits, trellis, tb_depth=35))[:-7]  # 丢尾 7 比特
  ```

  `0133`/`0171` 是 Python 2 的八进制字面量（= 91 / 121），即 802.11 标准的卷积码生成多项式。`tb_depth=35` 是 Viterbi 回溯深度。前补 `[0]*2`、后补 `[0]*12` 是为了在数据前后加「确知 0」，帮助回溯对齐——这与硬件 `ofdm_decoder` 的 flush（喂确信 0 顶出回溯延迟，见 u3-l5）是同一个道理。

**commpy 的 `Trellis` 与 `viterbi_decode`**

[scripts/commpy/channelcoding/convcode.py:103-172](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/commpy/channelcoding/convcode.py#L103-L172) —— `Trellis.__init__` 遍历所有状态与输入，根据 `g_matrix` 算出 `next_state_table` 和 `output_table` 两张网格转移表（`number_states = 2^total_memory`）。这是 Viterbi 译码的「字典」。

[scripts/commpy/channelcoding/convcode.py:474-573](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/commpy/channelcoding/convcode.py#L474-L573) —— `viterbi_decode` 是标准 ACS（Add–Compare–Select）+ 回溯实现，默认 `decoding_type='hard'`，用汉明距离作分支度量（[convcode.py:428-437](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/commpy/channelcoding/convcode.py#L428-L437)）。注意去穿孔填的 `2` 在硬判决里既不等于 0 也不等于 1，对所有分支贡献相同代价，等效于「未知」——和硬件把 erase 当「未知」处理（u3-l5）一致。

**解扰 `descramble`**

[scripts/decode.py:575-591](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L575-L591) —— 直装法初始化：用前 7 个接收比特反推 LFSR 状态 `X[6:0]`（因为发射端 SERVICE 前 7 比特固定为 0，扰码位即等于 LFSR 输出位），之后 `feedback = X[6]^X[3]`（多项式 \(x^7+x^4+1\)），逐比特 `feedback ^ b` 还原。与硬件 `descramble.v`（u3-l6）一致。

#### 4.3.4 代码实践

**实践目标**：单独验证「卷积解码」这一步，看清去穿孔如何把 24 Mbps（1/2 率，无穿孔）和 36 Mbps（3/4 率）区别对待，并确认 commpy Viterbi 能正确译出已知比特。

**操作步骤**：

1. 写一个不依赖样本、不依赖 wltrace 的小测试（**示例代码**）：

   ```python
   # 示例代码：单独测 commpy 卷积编码→Viterbi 译码回环
   import numpy as np
   import sys
   sys.path.insert(0, "scripts")
   import commpy.channelcoding.convcode as cc

   msg = np.array([1,0,1,1,0,0,1,0,1,1,0,1,0,0,1,1], dtype=int)  # 任意 16 比特
   trellis = cc.Trellis(np.array([7]), np.array([[0o133, 0o171]]))
   coded = cc.conv_encode(msg, trellis)          # 1/2 率编码
   decoded = cc.viterbi_decode(coded, trellis, tb_depth=35)
   print("msg     =", list(msg))
   print("decoded =", list(decoded[:len(msg)]))
   print("match   =", list(msg) == list(decoded[:len(msg)]))
   ```

2. 运行：`python2 run_viterbi.py`（这段只用到 numpy + commpy，不需要 wltrace）。

**需要观察的现象**：

- `coded` 长度约为 `msg` 的 2 倍（1/2 率）加上尾比特。
- `decoded[:len(msg)]` 应当与 `msg` 逐比特相等（无噪回环，必对）。

**预期结果**：`match = True`。这个回环验证了 `Trellis([7],[[133,171]])` + `viterbi_decode(tb_depth=35)` 这条调用链本身是好的——它正是 `decode.py` 第 [571-573](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L571-L573) 行用的同一条链。**具体输出待本地验证**。

> 进阶：把 `msg` 比特数加大到 48（一个 24 Mbps 符号的 `n_dbps`=96 的一半），观察尾比特补 0 后能否完整译出，体会「前补 2、后补 12 个 0」的作用。

#### 4.3.5 小练习与答案

**练习 1**：`decode.py` 既导入了 `commpy.channelcoding.convcode`，为什么解调却另写 `Demodulator` 类，不用 `commpy.modulation`？
**答案**：因为参考解码器必须和 Verilog `demodulate.v` 的**比特排列严格一致**（格雷码序，见 `QAM16_MAPPING`/`QAM64_MAPPING`，[decode.py:296-312](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L296-L312)），而 `commpy.modulation.Modem` 用自然序 `symbol_mapping=arange(m)`（[modulation.py:114/136](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/commpy/modulation.py#L114)），比特序对不上；卷积码则没有这种「排列」差异，可直接复用。

**练习 2**：去穿孔时填的 `2` 在 commpy 硬判决 Viterbi 里起什么作用？
**答案**：`2` 既不等于 0 也不等于 1，在汉明距离度量下对所有候选分支贡献相同代价，等效于「这一位未知，不偏向任何路径」——即 erase/擦除语义，和硬件 `ofdm_decoder` 把 erase 标志位当「未知」喂给 viterbi（u3-l5）完全对应。

**练习 3**：`viterbi_decode` 末尾为什么 `[ :-7]` 丢掉最后 7 个比特？
**答案**：那 7 个比特是卷积码的「尾比特（tail）」区域——发射端在帧末补 0 让网格回到零状态，它们不是有效数据，所以译完丢掉（[decode.py:573](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L573)）。这与硬件状态机里 tail 校验（u4-l2）是同一组比特。

---

### 4.4 期望输出结构与 LONG_PREAMBLE_TXT 等参考数据

#### 4.4.1 概念说明

最后这个模块讲两件事，它们是「交叉验证」能跑起来的基础：

1. **期望输出结构**：`decode()` 返回的 8 元组里，每一项的语义、长度、类型分别是什么——下一讲 `test.py` 怎么消费它们。
2. **参考数据**：`LONG_PREAMBLE_TXT`、`SHORT_PREAMBLE_TXT`、`LTS_REF`、`HT_LTS_REF`、`polarity` 这些写死在源码里的常量从哪来、用在哪。

#### 4.4.2 核心流程

**8 元组语义表**（`decode()` 返回，`decode_next()` 在最前面多一个 `glbl_index`）：

| 序号 | 名称 | 类型 | 含义 | Verilog 对应落盘文件 |
| --- | --- | --- | --- | --- |
| 0 | `glbl_index` | int | 包在原始样本流里的起始样本号（仅 `decode_next`） | — |
| 1 | `signal` | `Signal`/`HTSignal` | 解出的 SIGNAL/HT-SIG 字段（rate/length/parity…） | `signal_out.txt` |
| 2 | `cons` | list[complex] | 所有数据符号的均衡后星座点 | （隐性，比对 demod 间接覆盖） |
| 3 | `demod_out` | list[int] | 解调比特流（0/1） | `demod_out.txt` |
| 4 | `deinter_out` | list[int] | 解交织比特流 | `deinterleave_out.txt` |
| 5 | `conv_out` | list[int] | 卷积解码比特流 | `conv_out.txt` |
| 6 | `descramble_out` | list[int] | 解扰比特流 | `descramble_out.txt` |
| 7 | `data_bytes` | list[int] | 最终字节（0–255） | `byte_out.txt` |
| 8 | `pkt` | `dot11.Dot11Packet` | 用 wltrace 解析出的 MAC 帧 | （不直接比对） |

这张表把「Python 期望」与「Verilog 落盘」一一对应起来，下一讲 `test.py` 的工作就是逐行读右边的 `.txt`，与左边比。

**参考数据的用途**：

- `LONG_PREAMBLE_TXT` / `SHORT_PREAMBLE_TXT`：**理想**的 LTS / STS 时域样本（浮点）。用于 `find_pkt()` 的互相关定位（`np.correlate(samples, lts[-64:])`）。
- `LTS_REF` / `HT_LTS_REF`：LTS 在**频域**每个子载波的 BPSK 参考符号（±1）。用于信道估计（\(H[k]=Y[k]\cdot X[k]\)）。
- `polarity`：导频子载波的逐符号极性序列（802.11 规定）。用于 `next_symbol()` 里去掉导频的「伪随机」极性翻转。
- `RATE_BITS` / `RATE_PARAMETERS` / `HT_MCS_PARAMETERS`：802.11 标准 Table 78 的速率参数表。

#### 4.4.3 源码精读

**期望输出的拼装——`decode` 末段**

[scripts/decode.py:706-730](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L706-L730) —— 注意这里对**所有**数据符号的星座点 `cons` 统一收集后，再一次性 demod/deinter/conv/descramble（而不是逐符号）。这是浮点参考实现的便利之处——它不需要像硬件那样逐拍流水，可以「攒齐再算」：

```python
demod_out      = self.demodulate(cons, signal)
deinter_out    = self.deinterleave(demod_out, signal.rate, signal.mcs, signal.ht)
conv_out       = self.viterbi_decode(deinter_out, signal)
descramble_out = self.descramble(conv_out)
data_bits      = descramble_out[16:]          # 跳过 16 比特 SERVICE
num_bytes      = min(len(data_bits)/8, signal.length)
data_bytes     = [self.array_to_int(data_bits[i*8:(i+1)*8]) for i in range(num_bytes)]
return signal, cons, demod_out, deinter_out, conv_out, descramble_out, data_bytes, pkt
```

[decode.py:711-715](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L711-L715) —— `descramble_out[:16]` 应当全 0（SERVICE 字段），否则告警；然后 `descramble_out[16:]` 才是真正的 MPDU 比特。这个「跳过 SERVICE 16 比特」与硬件 `ofdm_decoder` 的 `skip_bit=9`（7 个 LFSR 初始化位已被 descramble 吞掉 + 9 = 16，见 u3-l6）是同一件事的两种说法。

**`array_to_int`：8 比特拼字节（LSB 先到）**

[scripts/decode.py:732-734](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L732-L734) —— `int(''.join(arr[::-1]), 2)`，先到的比特落 LSB。对应硬件 `bits_to_bytes.v`（u3-l6）。

**理想 LTS/STS 参考样本**

[scripts/decode.py:16-58](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L16-L58) —— `LONG_PREAMBLE_TXT` 是一段三元组文本（`索引 I Q`），覆盖索引 0–159 共 160 个理想 LTS 时域样本。

[scripts/decode.py:80-90](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L80-L90) —— 把文本解析成 `LONG_PREAMBLE` 列表（按索引排序）与 `SHORT_PREAMBLE`（只取索引 16–31 的一个 STS 周期，16 个样本）。`find_pkt` 用 `LONG_PREAMBLE[-64:]` 作互相关模板。

**LTS 频域参考符号**

[scripts/decode.py:125-129](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L125-L129) —— `LTS_REF`：52 个 ±1 值，对应 legacy 52 个子载波的 LTS BPSK 参考符号。HT 版本 `HT_LTS_REF` 在 [decode.py:154-159](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L154-L159)（56 个值）。

**导频极性序列**

[scripts/decode.py:130-136](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L130-L136) —— `polarity`：一个长长的 ±1 序列，第 \(n\) 个数据符号的导频极性取 `polarity[n]`，由 `itertools.cycle` 循环供给（[decode.py:199](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L199)）。

**`Signal` / `HTSignal` 字段解析**

[scripts/decode.py:351-367](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L351-L367) —— legacy SIGNAL：24 比特切 RATE(4)/RSVD(1)/LENGTH(12)/PARITY(1)/TAIL(6)，`parity_ok = sum(bits[:18])%2==0`（偶校验，覆盖前 18 比特）。对应硬件 u4-l2。

[scripts/decode.py:370-414](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L370-L414) —— HT-SIG：48 比特切 MCS/CBW/Length/STBC/FEC/SGI/CRC/Tail，并在 [decode.py:399-414](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L399-L414) 用 `calc_crc` 重算 CRC-8 比对。对应硬件 u4-l3。

#### 4.4.4 代码实践

**实践目标**：不跑完整解码，只验证「参考数据」本身正确——确认 `LONG_PREAMBLE` 真的有 160 个样本、`LTS_REF` 有 52 个值，并理解它们如何被 `find_pkt` / 信道估计使用。这是纯 Python 3 也能做的「源码阅读型实践」。

**操作步骤**：

1. 用 Python 3 直接 `import` 不行（`decode.py` 是 Py2），但我们可以**复制解析逻辑**验证参考数据规模（**示例代码**）：

   ```python
   # 示例代码：验证参考数据规模（Python 3 可跑，逻辑复制自 decode.py:80-90）
   LONG_PREAMBLE_TXT = open("scripts/decode.py").read()
   # 这里仅为示意：实际请从 decode.py 拷贝 LONG_PREAMBLE_TXT 字面量
   # 解析后应有 160 个 (idx,i,q)，SHORT_PREAMBLE 取 idx 16..31 共 16 个
   ```

2. 更实际的做法：用 Python 3 读 `scripts/decode.py`，把 `LONG_PREAMBLE_TXT`、`SHORT_PREAMBLE_TXT` 两个字符串抠出来，按 [decode.py:80-90](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L80-L90) 的 `zip(*(iter(s.split()),)*3)` 解析，打印 `len(LONG_PREAMBLE)` 与 `len(SHORT_PREAMBLE)`。

3. 再统计 `LTS_REF`（[decode.py:127-129](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L127-L129)）列表的长度与 ±1 计数。

**需要观察的现象**：

- `len(LONG_PREAMBLE)` == 160（理想 LTS 时域样本，索引 0–159）。
- `len(SHORT_PREAMBLE)` == 16（一个 STS 周期）。
- `len(LTS_REF)` == 52（legacy 子载波数）。
- `LTS_REF` 中每个元素都是 +1 或 −1。

**预期结果**：上述四个断言全部成立。这一步**可在 Python 3 下本地验证**（只需拷贝字符串常量，不导入 `decode.py` 主体），用来确认你对参考数据规模的理解。

#### 4.4.5 小练习与答案

**练习 1**：`LONG_PREAMBLE`（时域）和 `LTS_REF`（频域）分别在 `decode.py` 哪里被用？为什么一个有时域一个有频域两套？
**答案**：`LONG_PREAMBLE[-64:]` 用在 `find_pkt()` 做**时域互相关**定位包起点（[decode.py:465-466](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L465-L466)）；`LTS_REF` 用在信道估计 `gain = (lts1+lts2)/2 * LTS_REF`（[decode.py:196-197](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L196-L197)）。定位要在时域做（找峰），信道估计要在频域做（除以子载波增益），所以两套都需要。

**练习 2**：为什么 `decode()` 在循环解完所有数据符号后，要**重新**对 `cons` 做一次 `demodulate`/`deinterleave`/...？前面解 SIGNAL 时不已经做过一遍了吗？
**答案**：前面那一遍（[decode.py:646-651](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L646-L651) 处）是为了先解出 SIGNAL 字段、拿到 rate/length 来决定后续解多少个符号、用什么调制；拿到这些信息后，循环 `next_symbol()` 收集所有数据符号的星座点 `cons`（[decode.py:698-704](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L698-L704)），再按**真正的 rate** 统一 demod/deinter/conv（[decode.py:706-709](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L706-L709)）。SIGNAL 那遍用默认 6 Mbps 解调只是「先读头部」，不能用来解 DATA。

---

## 5. 综合实践

**综合任务**：把本讲四个模块串起来，用 `decode.py` 对 `testing_inputs/conducted/dot11a_24mbps_qos_data_e4_90_7e_15_2a_16_e8_de_27_90_6e_42.dat` 做一次完整解码，并产出一份「期望输出基准报告」，为下一讲 u5-l2 的交叉验证做准备。

**要求**：

1. 在 Python 2 环境下（`pip install numpy==1.11.2 scipy wltrace==1.1.1`），写一个驱动脚本调用 `decode.Decoder(sample).decode_next()`。
2. 打印以下信息，并记录成一张表：
   - 包起始样本号 `begin`；
   - `signal.rate`、`signal.length`、`signal.ht`、`signal.parity_ok`；
   - 六个中间结果的长度：`cons`、`demod_out`、`deinter_out`、`conv_out`、`descramble_out`、`data_bytes`；
   - `data_bytes` 的前 16 字节（十六进制）。
3. **验证一致性**（笔算）：
   - 24 Mbps 对应 `RATE_PARAMETERS[24] = (4, 192, 96)`，即 `n_cbps=192, n_dbps=96`。
   - 检查 `len(demod_out) / 192` 是否等于 `len(conv_out) / 96`（都应等于数据 OFDM 符号数）。
   - 检查 `len(data_bytes)` 是否等于 `signal.length`。
4. **对照硬件**（可选，衔接 u5-l2）：若已按 u1-l2 跑过 Verilog 仿真，打开 `verilog/sim_out/byte_out.txt`，逐字节与本脚本打印的 `data_bytes` 比对，应当完全一致。

**交付物**：一张包含上述数值的 Markdown 表格，外加一段 100 字以内的结论（例如「Python 期望输出 N 字节，与 sim_out/byte_out.txt 逐字节一致/不一致」）。

> 提示：若暂时没有 Python 2 环境，可先完成「源码阅读型」部分——对照本讲的 8 元组语义表，在 [decode.py:706-730](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L706-L730) 逐行标注每个返回项的来源，再在能跑的环境补齐数值（标注「待本地验证」）。

## 6. 本讲小结

- `scripts/decode.py` 是 OpenOFDM 的**浮点参考解码器**，扮演硬件 RTL 的「标准答案」，其 `Decoder.decode_next()` 返回一个 9 元组（含包起点 + `decode()` 的 8 元组期望输出），供 `test.py` 逐阶段交叉验证。
- 浮点前端 `ChannelEstimator` 把硬件的 sync_short + sync_long + equalizer 浓缩成一类：粗频偏（STS 延迟 16 自相关）+ 信道估计（`gain=(lts1+lts2)/2·LTS_REF`）+ 导频细校（`beta=∠Σ pilot·conj(gain)`）+ 除法均衡。
- 解调走的是**自研** `Demodulator`（最近邻硬判决 + 自定义格雷码序），**未用** `commpy.modulation`；卷积解码则复用 `commpy.channelcoding.convcode`（`Trellis([7],[[133,171]])` + `viterbi_decode(tb_depth=35)`），并通过填 `2` 表示去穿孔 erase。
- `LONG_PREAMBLE_TXT`/`SHORT_PREAMBLE_TXT` 是理想时域 LTS/STS，用于 `find_pkt` 互相关定位；`LTS_REF`/`HT_LTS_REF` 是频域 BPSK 参考符号，用于信道估计；`polarity` 是导频逐符号极性序列。
- 期望输出的 8 元组与 Verilog 落盘文件（`signal_out.txt`、`demod_out.txt`、`deinterleave_out.txt`、`conv_out.txt`、`descramble_out.txt`、`byte_out.txt`）一一对应，这是下一讲交叉验证的接线表。
- `decode.py` 是 Python 2 代码、依赖 `wltrace`/`scipy`，在 Python 3 环境无法直接运行——实践中需准备 Python 2 环境，或用「源码阅读型实践」替代。

## 7. 下一步学习建议

- **下一讲 u5-l2（交叉验证框架 `test.py`）**：本讲建立的「8 元组期望输出」会在 `test.py` 里被逐项消费——它调 `decode.Decoder().decode_next()` 生成期望、调 `iverilog`/`vvp` 跑硬件仿真、再逐文件比对 `sim_out/*.txt`。建议接着读 [scripts/test.py](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py)，重点看它如何处理「正负子载波交换」（[test.py:114-120](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L114-L120)）和 descramble 前 7 比特补偿（[test.py:183](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L183)）这两个易错点。
- **u5-l3（仿真测试台 `dot11_tb.v`）**：本讲多次提到 `sim_out/*.txt`，想看清这些文件是怎么从 Verilog 里写出来的，就读测试台。
- **回看 u3-l5 / u3-l6**：如果对卷积解码的软判决/flush、解扰的直装法还有疑问，本讲的 `viterbi_decode` 和 `descramble` 是它们的「无流水线简化版」，对照阅读会豁然开朗。
- **拓展阅读**：`commpy` 是一个完整的通信库，`scripts/commpy/` 下还有 `channels.py`、`filters.py`、`ldpc.py`、`turbo.py` 等，可作为理解 802.11 以外通信算法的参考资料。
