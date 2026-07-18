# 信道均衡 equalizer.v

## 1. 本讲目标

本讲是「频域处理」的第一课。学完后你应该能够：

- 说清楚 802.11 OFDM 的 64 个子载波是如何分工的（数据 / 导频 / DC / 保护），并能读懂 OpenOFDM 里的 `SUBCARRIER_MASK` / `PILOT_MASK` / `LTS_REF`。
- 解释「信道估计 = 用两段 LTS 估出每个子载波的复增益 H，再把数据符号逐子载波除以 H」这套流程，并把它对应到 `equalizer.v` 的状态机。
- 理解「残余频偏 = 用 4 个导频子载波估一个公共相位 θ，再旋转整符号」，并看清 `equalizer` 是如何与共享的 `phase` / `rotate` 协作的。
- 读懂 `divider` / `div_gen_v3_0` 除法器的接口与时延，以及 OpenOFDM 用「乘共轭再除模平方」实现复数除法 `X/H` 的技巧。

本讲承接 [u2-l4 sync_long](u2-l4-sync-long.md)：`sync_long` 负责符号定时与 FFT，把 64 个频域子载波交给 `equalizer`；本讲讲清楚 `equalizer` 拿到这 64 个点之后做了什么。

## 2. 前置知识

### 2.1 什么是「信道」

无线电波从发射天线到接收天线，经过的空气、反射体、天线本身，会对不同频率的成分产生**不同的幅度衰减和相位旋转**。接收端看到的第 `i` 个子载波不是发送的 `S[i]`，而是

\[
X[i] = H[i]\cdot S[i]
\]

其中 `H[i]` 是一个复数（幅度×相位），称为该子载波的**信道增益**（channel gain）。如果不去掉 `H[i]`，星座点会被「拽歪」，无法判决。**均衡（equalization）就是估计 `H[i]` 并把它除掉**。

### 2.2 为什么要两段 LTS

802.11 的长前导（LTS）由**两段完全相同的 64 样本序列**组成，且这是收发双方都已知的「标准答案」。因为两段 LTS 传的都是同一个已知序列，接收端把它们平均一下，就可以压低噪声、估出每个子载波上的 `H[i]`。这是后续一切频域处理的基础。

### 2.3 什么是「残余频偏」

[u2-l3](u2-l3-frequency-offset-correction.md) 里我们用短训练序列做了**粗频偏校正**（CFO）。但粗校正之后通常还残留一点点频偏，它会让星座点**随符号索引整体旋转一个小角度 θ**。这一点点旋转如果不去掉，Viterbi 解码就会失败。OpenOFDM 的做法是：**跳过基于 LTS 的细 CFO，改用 4 个导频子载波逐符号估计这个 θ 并旋转回来**。原因在 `eq.rst` 里有说明：细 CFO 往往小于相位查找表的分辨率，做了也白做。

### 2.4 复数除法的小技巧

硬件里做「复数除法 `X/H`」并不直接算倒数。利用共轭：

\[
\frac{X}{H} = \frac{X\cdot \overline{H}}{H\cdot \overline{H}} = \frac{X\cdot \overline{H}}{|H|^2}
\]

于是「一次复数除法」变成了「两次复数乘法 + 一次实数除法」。分子是 `X × conj(H)`，分母是 `|H|²`（一个实数），最后做一次实数除法即可。这是本讲 4.4 的核心。

> 名词速查：**子载波**（把 20MHz 切成许多窄带）、**FFT**（把时域样本变成频域的 64 个点）、**LTS**（长训练序列，已知参考）、**导频**（永远发已知 BPSK 符号的子载波，用来跟踪相位）、**共轭**（复数虚部取反）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [verilog/equalizer.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v) | 本讲主角。频域入口，做信道估计 + 残余频偏校正 + 子载波均衡。 |
| [verilog/divider.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/divider.v) | 除法器封装：把 Xilinx `div_gen` IP 包成「数据+strobe」握手，并补 36 拍时延。 |
| [verilog/coregen/div_gen_v3_0.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/div_gen_v3_0.v) | Xilinx 除法 IP 的仿真行为模型（综合用 `.ngc` 网表，仿真用这个 `.v`）。 |
| [verilog/common_defs.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_defs.v) | 全局定点缩放定义，`CONS_SCALE_SHIFT` 等决定归一化倍数。 |
| [docs/source/eq.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/eq.rst) | 均衡的权威数学说明文档，本讲所有公式都出自这里。 |
| [verilog/dot11.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) | 顶层。把 `equalizer` 接在 `sync_long` 之后，并把 `phase`/`rot_lut` 在多模块间共享。 |
| [verilog/calc_mean.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/calc_mean.v) | 求两段 LTS 均值的原语（顺带乘上 LTS 符号位）。 |
| [verilog/phase.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v) / [verilog/rotate.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v) | 共享的「求相位」与「按相位旋转」原语，u2-l3 已讲，本讲看它们怎么被复用。 |

---

## 4. 核心概念与源码讲解

本讲把均衡器拆成四个最小模块：**①频域入口与子载波结构**、**②信道估计（求 H）**、**③残余频偏与逐符号相位跟踪**、**④复数除法与归一化**。四者共同构成 `equalizer` 内部「每个 OFDM 符号循环一次」的处理环。

### 4.1 频域入口：子载波结构与掩码

#### 4.1.1 概念说明

20MHz 信道被切成 64 个子载波，每个宽 0.3125MHz。但并非 64 个全用：只有中间的 **52 个**（legacy 802.11a/g）被点亮，其中 **48 个传数据、4 个是导频**；DC 子载波（第 0 个）空着，两侧各留几个保护子载波。

`eq.rst` 给出的分工如下（见 [docs/source/eq.rst:12-31](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/eq.rst#L12-L31)）：

> 52 out of 64 sub-carriers are utilized, and 4 out of the 52 (-7, -21, 7, 21) sub-carriers are used as pilot sub-carrier and the remaining 48 sub-carriers carries data.

`equalizer.v` 把这套分工用三个 64 位常量编码成「掩码」：某一位为 1 表示该子载波属于这一类。掩码随符号逐位循环移位，实现对「当前输入到底是哪一种子载波」的判断。

#### 4.1.2 核心流程

掩码的位与子载波编号的对应关系由文件顶部注释给出（[verilog/equalizer.v:29-30](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L29-L30)）：

- `mask[0]` → DC 子载波（编号 0）
- `mask[1:26]` → 正子载波 +1 … +26
- `mask[38:63]` → 负子载波 −26 … −1

工作时，掩码每来一个样本就右移一位（把 `mask[0]` 移出），于是「当前这一拍输入的是不是数据/导频」只需要查 `mask[0]`。这是一种典型的「移位寄存器当扫描指针」的硬件写法，省掉一个比较器。

#### 4.1.3 源码精读

四个掩码定义在 [verilog/equalizer.v:31-45](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L31-L45)：

```verilog
localparam SUBCARRIER_MASK =
    64'b1111111111111111111111111100000000000111111111111111111111111110;
localparam HT_SUBCARRIER_MASK =
    64'b1111111111111111111111111111000000011111111111111111111111111110;
// -7, -21, 21, 7
localparam PILOT_MASK =
    64'b0000001000000000000010000000000000000000001000000000000010000000;
localparam DATA_SUBCARRIER_MASK =
    SUBCARRIER_MASK ^ PILOT_MASK;
localparam HT_DATA_SUBCARRIER_MASK = 
    HT_SUBCARRIER_MASK ^ PILOT_MASK;
```

要点：

- `SUBCARRIER_MASK` 点亮全部 52 个 legacy 活跃子载波（含 4 个导频）。
- `PILOT_MASK` 只点亮 4 个导频子载波（注释 `-7, -21, 21, 7`）。
- `DATA_SUBCARRIER_MASK = SUBCARRIER_MASK ^ PILOT_MASK`：异或正好把「既是子载波又是导频」的 4 个位抠掉，留下**纯数据子载波**。

另一个关键常量是 LTS 的**符号位序列** `LTS_REF`（[verilog/equalizer.v:48-53](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L48-L53)）。标准 LTS 每个数据子载波的值是 `±1`，这个 ±1 就是 `LTS_REF` 里的每一位（`eq.rst` 里的 `L[i]`）。它在 4.2 求 H 时用来「乘上参考符号」。

HT 模式比 legacy 多 4 个数据子载波，所以模块复位时把数据子载波个数设为两个不同的值（[verilog/equalizer.v:297](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L297) 与 [verilog/equalizer.v:409](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L409)）：

```verilog
num_data_carrier <= 48;   // legacy 复位（L297）
...
num_data_carrier <= 52;   // 切到 HT 时（L409）
```

> 这两个数正是 `DATA_SUBCARRIER_MASK` 与 `HT_DATA_SUBCARRIER_MASK` 各自的置位个数：legacy 52 − 4 导频 = **48**；HT 56 − 4 导频 = **52**。

#### 4.1.4 代码实践

**目标**：亲手验证「legacy 模式有 48 个数据子载波，导频在 −7/−21/7/21」。

**步骤**：

1. 打开 [verilog/equalizer.v:31-45](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L31-L45)，把 `SUBCARRIER_MASK`、`PILOT_MASK` 抄到一张纸上（或编辑器里）。
2. 按 §4.1.2 的位映射规则，数一下 `SUBCARRIER_MASK` 有多少个 1（应为 52），`PILOT_MASK` 有多少个 1（应为 4）。
3. 做 `SUBCARRIER_MASK ^ PILOT_MASK`（4 个导频位会被异或掉），得到 `DATA_SUBCARRIER_MASK`，再数 1 的个数。
4. 用映射规则把 `PILOT_MASK` 的 4 个置位翻译成子载波编号。

**需要观察的现象 / 预期结果**：

- `DATA_SUBCARRIER_MASK` 置位个数 = **48**，与 [verilog/equalizer.v:297](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L297) 的 `num_data_carrier <= 48` 完全一致。
- 导频子载波 = **{-21, -7, 7, 21}**，与 [verilog/equalizer.v:37](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L37) 注释 `-7, -21, 21, 7` 一致。

这一步是「待本地验证」型的纯阅读任务，无需运行仿真即可得出确定结论。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DATA_SUBCARRIER_MASK` 用异或（`^`）而不是与（`&`）来从 `SUBCARRIER_MASK` 里去掉导频？

**答案**：因为 4 个导频位在 `SUBCARRIER_MASK` 里本来就是 1。异或 `1^1=0` 恰好把它们清零，而其余数据位是 `1^0=1` 保持不变。用「与」会保留这些位，无法去掉导频。

**练习 2**：HT 模式下 `HT_DATA_SUBCARRIER_MASK` 有多少个置位？为什么模块在切到 HT 时要把 `num_data_carrier` 从 48 改成 52？

**答案**：52 个。802.11n@20MHz 比 legacy 多用 4 个子载波（共 56 个活跃，减 4 导频 = 52 数据），所以均衡器每符号要多输出 4 个归一化点，状态机的「收齐 num_data_carrier 个就回 `S_GET_POLARITY`」判定也必须跟着改。

---

### 4.2 信道估计：用两段 LTS 求信道增益 H

#### 4.2.1 概念说明

`eq.rst` 给出的信道增益定义（[docs/source/eq.rst:73-79](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/eq.rst#L73-L79)）是两段 LTS 的平均再乘上参考符号：

\[
H[i] = \tfrac{1}{2}\bigl(LTS_1[i] + LTS_2[i]\bigr)\times L[i],\quad i\in[-26,26]
\]

其中 `L[i]` 是 LTS 参考序列的符号（±1），正是 §4.1 的 `LTS_REF`。求出 `H[i]` 后，数据符号按下式归一化（[docs/source/eq.rst:89-95](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/eq.rst#L89-L95)）：

\[
Y[i] = \frac{X[i]}{H[i]}
\]

#### 4.2.2 核心流程

`equalizer` 用两个状态完成信道估计：

1. **`S_FIRST_LTS`**：把第一段 LTS 的 64 个 FFT 点原样存进一块双口 RAM（`lts_inst`）。
2. **`S_SECOND_LTS`**：第二段 LTS 到来时，逐点把「RAM 里的第一段」和「新到的第二段」送进 `calc_mean` 求平均，并按 `LTS_REF` 的符号位决定是否取反，再把结果**写回同一块 RAM**。写完后，RAM 里存的就是每个子载波的 `H[i]`。

伪代码：

```
S_FIRST_LTS:
  for i in 0..63: H[i] <- LTS1[i]            # 原样存
S_SECOND_LTS:
  for i in 0..63:
      m <- (H[i] + LTS2[i]) / 2              # calc_mean 做平均
      H[i] <- L[i] ? -m : m                  # 乘上参考符号 ±1
  # 此后 H[i] 就是信道增益，供每个数据符号反复使用
```

#### 4.2.3 源码精读

求均值的原语 `calc_mean`（[verilog/calc_mean.v:38-41](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/calc_mean.v#L38-L41)）用算术右移实现「除以 2」，再用补码 `~cc+1` 实现「按符号取反」：

```verilog
aa <= a>>>1;            // 第一段 LTS 的一半
bb <= b>>>1;            // 第二段 LTS 的一半
cc <= aa + bb;          // 平均值
c <= sign_stage[1]? ~cc+1: cc;   // sign=1 则取反，即乘 -1
```

于是 `c = L[i]·(LTS1[i]+LTS2[i])/2 = H[i]`，一步到位把「平均 + 乘参考符号」合在一起。

`equalizer` 里 I、Q 各实例化一个 `calc_mean`（[verilog/equalizer.v:153-178](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L153-L178)），`a` 接 RAM 读出的第一段 LTS、`b` 接当前输入（第二段），`sign` 接 `current_sign`（来自 `LTS_REF` 的当前位）。

状态机的两段（[verilog/equalizer.v:334-373](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L334-L373)）：

```verilog
S_FIRST_LTS: begin
    lts_in_stb <= sample_in_strobe;
    {lts_i_in, lts_q_in} <= sample_in;          // 原样写 RAM
    ...
end

S_SECOND_LTS: begin
    if (sample_in_strobe) begin
        calc_mean_strobe <= sample_in_strobe;
        {input_i, input_q} <= sample_in;        // 第二段送 calc_mean 的 b
        current_sign <= lts_ref[0];             // 取参考符号位
        lts_ref <= {lts_ref[0], lts_ref[63:1]}; // 每读一子载波移一位
        ...
    end
    lts_in_stb <= new_lts_stb;                  // 把均值写回 RAM
    {lts_i_in, lts_q_in} <= {new_lts_i, new_lts_q};
    ...
end
```

注意 `LTS_REF` 也是右移扫描的——每处理一个子载波，就把符号序列移一位，使 `lts_ref[0]` 始终对应当前子载波的参考符号。这与 §4.1 的掩码移位是同一种「扫描指针」技巧。

此后这块 RAM（`lts_inst`，[verilog/equalizer.v:138-151](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L138-L151)）就长期保存 `H[i]`，每个数据符号都会反复读它来做除法。

#### 4.2.4 代码实践

**目标**：验证 `S_SECOND_LTS` 真的算出了 `H[i] = L[i]·(LTS1+LTS2)/2`。

**步骤**：

1. 读 [verilog/calc_mean.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/calc_mean.v)，确认 `cc = a>>>1 + b>>>1` 等价于 `(a+b)/2`，且 `sign` 为 1 时输出 `~cc+1 = -cc`。
2. 读 [verilog/equalizer.v:350-373](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L350-L373)，确认 `calc_mean` 的 `a` = RAM 中第一段 LTS、`b` = 第二段输入、`sign` = `lts_ref[0]`。
3. 用纸笔对一个具体子载波（比如 `i=+7`，假设 `LTS1=+100`，`LTS2=+108`，`L[7]=−1`）算出 `H[7]`。

**预期结果**：`H[7] = −(100+108)/2 = −54`。若把 `L[7]` 改成 `+1`，结果应是 `+54`。这条等式说明：**RAM 里最终存的不是裸 LTS 均值，而是已经乘上参考符号的信道增益**，这正是 4.4 复数除法要用的 `H`。

**待本地验证**：若想看真实数值，可在仿真中用 `$display` 打印 `new_lts_i`，对照 Python 参考解码器 `scripts/decode.py` 输出的 LTS 期望值。

#### 4.2.5 小练习与答案

**练习 1**：为什么用「两段 LTS 平均」而不是只用一段？

**答案**：平均可以把噪声方差减半（两段独立噪声相加再除 2）。LTS 本来就是设计成重复两段，目的之一就是给接收端一个降噪的信道估计样本。

**练习 2**：`calc_mean` 里为什么用 `a>>>1`（算术右移）而不是 `a/2`？

**答案**：`a` 是有符号数，算术右移一位在硬件上就是「除以 2 并保留符号」，比一条除法指令/除法器便宜得多，且对负数也正确（`>>>` 会复制符号位）。

---

### 4.3 残余频偏与逐符号相位跟踪：导频 + 共享 phase/rotate

#### 4.3.1 概念说明

粗 CFO 校正后残留的小频偏，会让**整个符号**（所有子载波）一起旋转一个角度 θ。`eq.rst` 给出用 4 个导频估计这个 θ 的公式（[docs/source/eq.rst:183-196](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/eq.rst#L183-L196)）：

\[
\theta_n = \angle\!\left(\sum_{i\in\{-21,-7,7,21\}} \overline{X^{(n)}[i]}\cdot P^{(n)}[i]\cdot H[i]\right)
\]

其中 `P[n][i]` 是第 n 个符号、第 i 个导频的**已知极性**（±1，由一段伪随机序列决定）。把 4 个导频的贡献加成一个复数，再取它的相角，就是 θ。最终每个数据子载波的输出是「先除 H，再旋转 θ」：

\[
Y^{(n)}[i] = \frac{X^{(n)}[i]}{H[i]}\,e^{j\theta_n}
\]

#### 4.3.2 核心流程

`equalizer` 在每个数据符号上跑一遍这个环（状态 `S_GET_POLARITY → S_CALC_FREQ_OFFSET → S_ADJUST_FREQ_OFFSET`）：

```
S_GET_POLARITY:
    根据 802.11 极性序列算出本符号 4 个导频的 P[i]（±1）
S_CALC_FREQ_OFFSET:
    边收 64 个 FFT 点边把它们存进 in_buf RAM；
    遇到 4 个导频位时，取 conjugate(X_pilot)·P[i]·H[i]，累加成 pilot_sum
    4 个导频凑齐 → 把 pilot_sum 送给共享 phase 模块求 θ → pilot_phase
S_ADJUST_FREQ_OFFSET:
    逐子载波：用 rotate 把样本旋转 pilot_phase（=e^{jθ}），再交给 4.4 除 H
    只输出数据子载波；收齐 num_data_carrier 个 → 回 S_GET_POLARITY 处理下一符号
```

注意：`phase`（求相角）和 `rotate`（旋转）这两个原语都不是 `equalizer` 私有的——它们被 `dot11.v` 在 `sync_short`/`equalizer`、`sync_long`/`equalizer` 之间**共享**，这是 u2-l3 已建立的事实，本讲只看 `equalizer` 这一侧怎么用。

#### 4.3.3 源码精读

**(a) 导频极性生成** —— `S_GET_POLARITY`（[verilog/equalizer.v:375-404](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L375-L404)）。legacy 用一段 127 位伪随机序列 `POLARITY`（[verilog/equalizer.v:56-57](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L56-L57)），每个符号取一位 `polarity[0]`，再按公式 `P={p, p, −p, p}` 展开成 4 个导频的极性：

```verilog
current_polarity <= {
    polarity[0],    // -7
    polarity[0],    // -21
    ~polarity[0],   // 21   ← 注意这一位取反
    polarity[0]     // 7
};
polarity <= {polarity[0], polarity[126:1]};   // 下一个符号推进一位
```

这与 `eq.rst` 的 `P^{(n)}_{-21,-7,7,21}={p_{n%127}, p_{n%127}, p_{n%127}, -p_{n%127}}` 完全对应（`eq.rst` 第 +21 子载波那一个是 `-p`，正好是 `~polarity[0]`）。

**(b) 导频提取与累加** —— `S_CALC_FREQ_OFFSET`（[verilog/equalizer.v:421-456](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L421-L456)）。当 `pilot_mask[0]` 命中（当前是导频子载波），按极性对样本做「共轭 × ±1」，再乘上 `H[i]`：

```verilog
if (current_polarity[0] == 0) begin              // P=+1
    input_i <= sample_in[31:16];
    input_q <= ~sample_in[15:0] + 1;             // 取共轭 conj(X)
end else begin                                    // P=-1
    input_i <= ~sample_in[31:16] + 1;            // = -conj(X) = conj(X)·P
    input_q <= sample_in[15:0];
end
pilot_in_stb <= 1;
```

随后 `pilot_inst`（[verilog/equalizer.v:195-207](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L195-L207)）做复数乘法 `input × H`，乘积累加到 `pilot_sum`：

```verilog
if (pilot_out_stb) begin
    pilot_sum_i <= pilot_sum_i + pilot_i;
    pilot_sum_q <= pilot_sum_q + pilot_q;
    if (pilot_count == 4) phase_in_stb <= 1;     // 4 个导频凑齐，送 phase
end
```

**(c) 求相角** —— `phase_in_i/q = pilot_sum` 直接接出（[verilog/equalizer.v:113-114](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L113-L114)），共享的 `phase` 模块算出 `pilot_phase = θ`（[verilog/equalizer.v:458-462](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L458-L462)）。

**(d) 旋转回来** —— `S_ADJUST_FREQ_OFFSET` 里，整符号的样本从 `in_buf` RAM 重读，送进 `rotate_inst`，按 `pilot_phase` 旋转（[verilog/equalizer.v:209-225](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L209-L225) 与 [verilog/equalizer.v:472-479](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L472-L479)）。这一步乘上的就是 `e^{jθ}`。

**共享机制（顶层）**：`dot11.v` 用一个 MUX 按 `state==S_SYNC_SHORT` 把 `phase` 在 `sync_short` 与 `equalizer` 间分时复用（[verilog/dot11.v:133-138](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L133-L138)），`rot_lut` 用双口 RAM 同时服务 `sync_long` 与 `equalizer`（[verilog/dot11.v:96-113](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L96-L113)）。所以 `equalizer` 这一侧的 `rotate`/`phase` 并不独占硬件，而是「借」全局资源——这也是为什么 `equalizer` 在 `S_CALC_FREQ_OFFSET` 凑齐 4 个导频后才发 `phase_in_stb`，避免和 `sync_short` 抢同一个 `phase` 模块。

#### 4.3.4 代码实践

**目标**：跟踪「导频 → θ → 旋转」这条链，确认它实现的就是 `eq.rst` 的公式。

**步骤**：

1. 读 [verilog/equalizer.v:421-456](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L421-L456)，逐拍确认：`pilot_mask[0]` 为 1 时，`input` = `conj(X_pilot)·P[i]`，`pilot_inst` 输出 = `conj(X)·P·H`，4 次累加成 `pilot_sum`。
2. 读 [verilog/dot11.v:133-146](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L133-L146)，确认 `pilot_sum` 经 MUX 进共享 `phase`，结果回送为 `eq_phase_out`。
3. 读 [verilog/equalizer.v:458-468](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L458-L468)，确认 `phase_out` 被锁存为 `pilot_phase`，并在下一状态驱动 `rotate`。

**需要观察的现象**：若编译时打开 `DEBUG_PRINT` 宏（在 [verilog/Makefile](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/Makefile) 的 iverilog 命令加 `+define+DEBUG_PRINT`），仿真日志会打印 `[PILOT OFFSET] <值>`（[verilog/equalizer.v:459-461](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L459-L461)），这就是每个符号的 θ。

**预期结果**：正常样本下 `[PILOT OFFSET]` 是一个接近 0 的小角度（粗校正已经把大头去掉）；如果故意破坏 CFO 校正，这个值会变大。**待本地验证**（需要 iverilog + 样本）。

#### 4.3.5 小练习与答案

**练习 1**：导频极性公式里 `+21` 子载波为什么是 `~polarity[0]`（取反），而其它三个是 `polarity[0]`？

**答案**：因为 802.11 标准规定 legacy 导频极性为 `P={p, p, p, -p}`（对应 -21,-7,7,+21），`+21` 这一个带负号。`eq.rst` 的 `P^{(n)}={p_{n%127}, p_{n%127}, p_{n%127}, -p_{n%127}}` 明确写了这个负号，硬件里用 `~`（按位取反，对 1 位数即逻辑非）实现。

**练习 2**：为什么 `equalizer` 必须等 `pilot_count==4` 才发 `phase_in_stb`，而不是每来一个导频就发？

**答案**：θ 是 4 个导频**求和之后**那个复数的相角（公式里的求和号）。逐个导频发的话，`phase` 模块算的是单个导频的角度，含大量噪声；4 个加完再取角，相当于对 4 个导频做相位平均，估计更稳。

---

### 4.4 复数除法与归一化：divider / div_gen_v3_0

#### 4.4.1 概念说明

无论信道均衡（除 H）还是后续解调判决门限，都离不开「除法」。但 FPGA 上除法器又大又慢。OpenOFDM 用了两个手段：

1. **算法上**：用 §2.4 的共轭技巧把复数除法 `X/H` 化简为「两次复数乘法 + 一次实数除法」，避免直接算复数倒数。
2. **实现上**：调用 Xilinx 的 `div_gen` IP（一个流水化除法器）做那次实数除法，外面用 `divider.v` 包一层「数据+strobe」握手并补偿 36 拍流水时延。

#### 4.4.2 核心流程

在 `S_ADJUST_FREQ_OFFSET` 里，每个已旋转的子载波 `X'`（= `X·e^{jθ}`）走三步得到归一化输出 `Y`：

```
①  分子  prod = X' × conj(H)        # complex_mult，b 用 -Im(H)
②  分母  mag_sq = H × conj(H) = |H|²  # 另一个 complex_mult
③  相除  norm = (prod << CONS_SCALE_SHIFT) / mag_sq   # divider
```

因为 `prod/mag_sq = X'·conj(H)/|H|² = X'/H = Y`，所以商就是归一化结果，只是整体被左移了 `CONS_SCALE_SHIFT`（=10）位，即放大 1024 倍。这个放大倍数会被下游 `demodulate` 的判决门限吃掉（见 [verilog/common_defs.v:9](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_defs.v#L9)）。

#### 4.4.3 源码精读

**共轭怎么来的**：一行补码取负得到 `-Im(H)`（[verilog/equalizer.v:99](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L99)）：

```verilog
wire signed [15:0] lts_q_out_neg = ~lts_q_out + 1;   // = -Im(H)
```

于是 `(lts_i_out, lts_q_out_neg)` 就是 `conj(H)`。

**分子与分母两个乘法器**（[verilog/equalizer.v:227-251](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L227-L251)）：

```verilog
complex_mult input_lts_prod_inst (
    .a_i(rot_i), .a_q(rot_q),                 // X'（已旋转）
    .b_i(lts_i_out), .b_q(lts_q_out_neg),     // conj(H)
    ...
    .p_i(prod_i), .p_q(prod_q)                // 分子 = X' × conj(H)
);

complex_mult lts_lts_prod_inst (
    .a_i(lts_i_out), .a_q(lts_q_out),         // H
    .b_i(lts_i_out), .b_q(lts_q_out_neg),     // conj(H)
    ...
    .p_i(mag_sq)                              // 分母 = H×conj(H) = |H|²
);
```

**缩放与除法**（[verilog/equalizer.v:125-126](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L125-L126) 与 [verilog/equalizer.v:253-276](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L253-L276)）：

```verilog
wire [31:0] prod_i_scaled = prod_i<<`CONS_SCALE_SHIFT;   // 左移 10 位
wire [31:0] prod_q_scaled = prod_q<<`CONS_SCALE_SHIFT;

divider norm_i_inst (
    .dividend(prod_i_scaled),
    .divisor(mag_sq[23:0]),                    // |H|²，取低 24 位
    .input_strobe(prod_out_strobe),
    .quotient(norm_i),                         // 商 = Y_i × 1024
    ...
);
```

**除法器封装** `divider.v`（[verilog/divider.v:1-31](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/divider.v#L1-L31)）极简：实例化 IP，再用 `delayT` 把输入 strobe 延迟 36 拍作为输出 strobe，让数据与商对齐：

```verilog
div_gen_v3_0 div_inst ( .clk(clock), .dividend, .divisor, .quotient );
delayT #(.DATA_WIDTH(1), .DELAY(36)) out_inst (
    .data_in(input_strobe), .data_out(output_strobe)   // 补 36 拍流水
);
```

注释明确写了 `DELAY: 36 cycles`。被封装的 IP `div_gen_v3_0` 的接口（[verilog/coregen/div_gen_v3_0.v:36-44](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/div_gen_v3_0.v#L36-L44)）是 `dividend[31:0] / divisor[23:0] → quotient[31:0]`（外加一个 `fractional[23:0]`，这里没用）。这个 `.v` 是 Xilinx 生成的**仿真行为模型**（文件头注明「cannot be synthesized, only used with simulation tools」），所以本讲用 iverilog 也能仿真——综合时才换成 `.ngc` 网表。

**输出打包**：归一化结果压成 16 位 I + 16 位 Q（[verilog/equalizer.v:485-498](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L485-L498)），只在数据子载波位输出 strobe：

```verilog
if (norm_out_stb) begin
    data_subcarrier_mask <= {data_subcarrier_mask[0], data_subcarrier_mask[63:1]};
    if (data_subcarrier_mask[0]) begin
        sample_out_strobe <= 1;
        sample_out <= {norm_i[31], norm_i[14:0],   // {符号位, 低15位}
                        norm_q[31], norm_q[14:0]};
    end
end
...
if (num_output == num_data_carrier) state <= S_GET_POLARITY;   // 本符号做完
```

#### 4.4.4 代码实践

**目标**：验证「复数除法 = 乘共轭再除模平方」在硬件里的实现，并理解 36 拍时延如何被消化。

**步骤**：

1. 读 [verilog/equalizer.v:99](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L99) 和 [verilog/equalizer.v:227-251](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L227-L251)，写出 `prod` 与 `mag_sq` 的复数表达式，确认 `prod/mag_sq = X'/H`。
2. 读 [verilog/divider.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/divider.v)，确认除法器本身**没有** `output_strobe` 输出，是靠外面 `delayT #(.DELAY(36))` 把输入 strobe 延 36 拍得到 `output_strobe`。
3. 读 [verilog/equalizer.v:253-276](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L253-L276)，确认 I、Q 各用一个 `divider`（`norm_i_inst`/`norm_q_inst`），但**共用同一个 `prod_out_strobe`** 作为输入 strobe、共用 `mag_sq` 作为除数。

**需要观察的现象**：注意 `S_ADJUST_FREQ_OFFSET` 里 `num_output` 只在 `norm_out_stb` 且命中数据子载波时才自增（[verilog/equalizer.v:485-502](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L485-L502)）。除法器的 36 拍时延会被「先发 64 个旋转请求、再陆续收 64 个商」这种流水自然消化，状态机不必专门等待。

**预期结果**：一个 legacy 符号最终恰好输出 **48 个** `sample_out_strobe`（= `num_data_carrier`），HT 符号输出 **52 个**。这 48/52 个归一化复数就是下一级 `ofdm_decoder`（解调）的输入。**待本地验证**（可在仿真里数 `equalizer_out_strobe` 的脉冲数）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `prod_i_scaled = prod_i << 10` 要在除法**之前**左移，而不是除完再移？

**答案**：整数除法会截断小数。`X/H` 通常小于 1（H 含幅度衰减，X 被衰减过），先除会得到 0。先左移 10 位（放大 1024 倍）再除，相当于保留了 10 位小数精度，商落在合理的整数范围，供 `demodulate` 当定点星座用。这个 `10` 就是 `CONS_SCALE_SHIFT`，和判决门限是配套的。

**练习 2**：`divider.v` 里为什么 I、Q 可以共用同一个除数 `mag_sq`，但不能共用同一个除法器实例？

**答案**：`mag_sq = |H|²` 是实数，对 I 分子和 Q 分子都一样，所以除数相同。但分子 `prod_i` 和 `prod_q` 是两个不同的数，要同时算两个商，所以必须实例化**两个** `divider`（`norm_i_inst`、`norm_q_inst`）并行处理。

---

## 5. 综合实践

把四个最小模块串起来，做一个「全程跟踪一个 legacy 符号」的任务。

**背景**：在 [verilog/dot11.v:321-344](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L321-L344) 里，`equalizer` 的输入是 `sync_long_out`（FFT 后的频域点），输出 `equalizer_out` 喂给 `ofdm_decoder`。

**任务**：

1. **运行仿真**：在 `verilog/` 下按 [u1-l2](u1-l2-environment-and-simulation.md) 的方法用默认 24Mbps dot11a 样本跑一次 `make simulate`，得到 `sim_out/` 下的各阶段 `.txt`。
2. **数脉冲**：打开 `sim_out/` 里 equalizer 对应的输出文件（或直接在 `dot11.vcd` 里数 `equalizer_out_strobe`），确认 **SIGNAL 符号之后每个数据符号产生 48 个归一化样本**。
3. **对照参考**：运行 Python 参考解码器 `scripts/decode.py`（[u5-l1](u5-l1-python-reference-decoder.md) 会详讲），打印均衡后的期望输出，与 `sim_out/` 里的 equalizer 输出逐点比对（这正是 [u5-l2](u5-l2-cross-validation.md) 交叉验证框架做的事）。
4. **画状态环**：用本讲 §4.3.2 / §4.4.3 的内容，画出 `equalizer` 的状态转移图，标出 `S_FIRST_LTS → S_SECOND_LTS → S_GET_POLARITY → S_CALC_FREQ_OFFSET → S_ADJUST_FREQ_OFFSET → (回 S_GET_POLARITY)` 这个「每符号一次」的环，并标注每段用了哪些共享原语（`calc_mean`、`phase`、`rotate`、`divider`）。

**验收标准**：

- 能说清「输入 64 个 FFT 点 → 输出 48 个归一化复数」这个 64→48 的来历（52 活跃 − 4 导频 = 48，导频只用来估 θ 不输出）。
- 能在波形里指出 `phase_in_stb`（导频凑齐那一拍）与 `pilot_phase`（求出的 θ）的对应关系。
- 若 Python 参考与 Verilog 输出一致，说明你对均衡流程的理解与实现吻合。

> 若本地没有 iverilog，任务 1/2 可降级为「源码阅读」：直接在 [verilog/equalizer.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v) 里数 `num_output` 从 0 涨到 `num_data_carrier` 的路径，同样能确认 48 这个数字。

## 6. 本讲小结

- `equalizer` 是 OFDM 解码链的**频域入口**，输入是 `sync_long` FFT 出来的 64 个子载波，输出是逐子载波归一化后的复数（legacy 48 个、HT 52 个）。
- 子载波分工用三个 64 位掩码编码：`SUBCARRIER_MASK`（52 活跃）、`PILOT_MASK`（4 导频 -21/-7/7/21）、`DATA_SUBCARRIER_MASK = 前两者异或`（48 纯数据）；掩码靠逐位右移当「扫描指针」。
- **信道估计**：两段 LTS 经 `calc_mean` 求平均并乘上 `LTS_REF` 符号位，得到每个子载波的复增益 `H`，存进 RAM 长期复用。
- **残余频偏**：4 个导频按极性取共轭、乘 `H`、求和，送共享 `phase` 模块取相角得 θ，再用共享 `rotate` 把整符号旋转 `e^{jθ}`；`phase`/`rot_lut` 都是被 `dot11.v` 在多模块间共享的资源。
- **复数除法**用共轭技巧化为 `X'·conj(H) / |H|²`：两个 `complex_mult` 分别算分子分母，一个 Xilinx `div_gen` 除法器（`divider.v` 封装，36 拍流水）做实数除法，结果左移 `CONS_SCALE_SHIFT`(=10) 位供下游解调。
- 数据流呈 64→48 的收窄：FFT 的 64 点里，DC/保护/导频都不输出，只输出 48 个数据子载波的归一化值给 `ofdm_decoder`。

## 7. 下一步学习建议

- **下一讲 [u3-l2 复数运算与流水线辅助原语](u3-l2-complex-primitives.md)**：本讲反复用到的 `complex_mult`、`complex_to_mag`、`delayT`、`moving_avg`、`calc_mean` 会在那里集中精读，理解它们的握手时延是看懂 `equalizer` 流水对齐的前提。
- **[u3-l3 解调 demodulate](u3-l3-demodulate.md)**：本讲输出的「放大 1024 倍的归一化复数」如何被 `demodulate` 按 `CONS_SCALE_SHIFT` 设定门限、判决成 BPSK/QPSK/16-QAM/64-QAM 比特，是直接下游。
- **回顾 [u2-l3 频偏校正](u2-l3-frequency-offset-correction.md)**：对照粗 CFO（时域、`sync_short`）与本讲的细 CFO（频域、导频），理解 OpenOFDM「粗校正 + 导频跟踪」两段式频偏策略的全貌。
- **延伸阅读**：`eq.rst` 末尾指向 802.11-2012 标准第 20.3.11.10 节，想搞清 HT 多空间流 / 不同带宽下的导频极性，可去那里查表。
