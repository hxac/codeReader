# 长训练序列同步与符号定时 sync_long.v

## 1. 本讲目标

`sync_long` 是 OFDM 解码流水线的第三道关，紧接在 `sync_short`（短训练序列同步）之后。本讲要解决一个具体而关键的问题：**精确知道每一个 OFDM 符号从哪一个样本开始**，并把对齐后的样本送进 FFT。

学完本讲你应当能够：

1. 说清楚为什么需要「符号定时」，以及为什么用长训练序列（LTS）的互相关来定位。
2. 读懂 `sync_long.v` 的五状态机：跳尾 → 找第一峰 → 找第二峰 → FFT → 空闲。
3. 理解为什么 64 点互相关被裁剪成只用 LTS 前 16 个样本，以及 `stage_mult` 如何用「4 个并行复乘 × 4 拍流水」拼出这 16 次复乘。
4. 解释 `gi_skip` 为什么在 `short_gi` 时取 9、否则取 17，并算出它与保护间隔（GI）16/8 的关系。
5. 在波形里定位 `long_preamble_detected` 脉冲与对齐后的 `sample_out` 之间的时序关系。

## 2. 前置知识

本讲承接 [u2-l3 中心频偏估计与相位/旋转校正](u2-l3-frequency-offset-correction.md)。在继续之前，请确认你已经理解以下概念。

### 2.1 802.11 OFDM 包结构

802.11a/g 的前导码（preamble）由两段组成，顺序为：

```
短训练序列 STS (10×16=160 样本) | 长训练序列 LTS (160 样本) | SIGNAL 符号 | 数据符号 ...
        |<- sync_short 负责 ->|   |<-- sync_long 负责 -->|
```

- **STS**：由 10 段长度为 16 的重复短序列构成，`sync_short` 用它做延迟自相关，确认「有包」并估出粗频偏。
- **LTS**：由一段 32 样本的循环前缀（CP，相当于两个 GI 叠加）加两段完全相同的 64 样本长训练序列构成，即 `CP(32) + LTS1(64) + LTS2(64) = 160` 样本。两段 LTS 完全一样，这正是 `sync_long` 定位的依据。

每个 OFDM 符号在 20 MSPS 采样率下占 80 个样本（4 µs）：前 16 个是保护间隔（Guard Interval, GI，即循环前缀 CP），后 64 个是真正承载子载波的数据。若启用短保护间隔（Short GI, SGI），则 GI 缩短为 8、符号总长变为 72。

### 2.2 互相关（Cross Correlation）

如果接收端有一份本地已知的 LTS 时域样本 \(H\)，那么把它与接收样本流 \(S\) 滑动相乘求和，在「接收样本恰好对齐 LTS」的位置会出现一个尖峰。这就是互相关，定义为：

\[
Y[i] = \sum_{k=0}^{N-1} S[i+k]\,\overline{H[k]}
\]

其中 \(\overline{H[k]}\) 是 \(H[k]\) 的复共轭。\(Y[i]\) 取幅值后，幅值最大的那个 \(i\) 就是对齐点。`sync_long` 用的就是这个思路。

### 2.3 承接 u2-l3 的频偏线索

`sync_short` 输出的 `phase_offset`（粗频偏 α_ST）会被本模块继续使用：在 FFT 之前，`sync_long` 对每个样本按 \(m\cdot\alpha_{ST}\) 累加相位并交给 `rotate` 旋转，完成「STS 粗 CFO 时域一次校」的下半场。这一点本讲 4.4 节会接上。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [verilog/sync_long.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v) | 本讲主角：LTS 互相关 + 符号定时 + FFT 加载 |
| [verilog/stage_mult.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/stage_mult.v) | 多级流水乘加，并行完成 4 个复数乘法并求和 |
| [verilog/complex_to_mag.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_to_mag.v) | 快速幅值近似，把复相关值变成标量 metric |
| [verilog/delay_sample.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/delay_sample.v) | 基于双口 RAM 的样本延时线（`sync_short` 已用，本模块用到同类思想） |
| [verilog/dot11.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) | 顶层例化 `sync_long`，并与 `equalizer` 共享 `rot_lut` |
| [docs/source/sync_long.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/sync_long.rst) | 官方文档：符号对齐与 FFT 的设计取舍 |

## 4. 核心概念与源码讲解

### 4.1 符号定时：为什么要用 LTS

#### 4.1.1 概念说明

`sync_short` 只能告诉你「大概在某段样本里有短前导」，它的定位精度不足以对齐 FFT 窗口。原因有二：

- STS 是周期为 16 的重复序列，延迟自相关在一个 16 样本的「平台」上都接近最大，**无法给出单一精确的样本起点**。
- FFT 是一个 64 点的块运算，输入窗口偏 1～2 个样本就会引入相位旋转，导致后续星座解调出错。

因此需要一个**尖锐**的对齐信号。LTS 互相关天然产生一个宽度仅 1 个样本的尖峰（见 `sync_long.rst` 的图），非常适合做精确定时。`sync_long` 的全部工作可以概括成一句话：**滑动计算 LTS 互相关 → 找到两个相隔 64 的尖峰 → 把读指针对齐到 LTS 起点 → 从此逐符号地读 64 样本送 FFT**。

#### 4.1.2 核心流程

`sync_long` 内部是一个五状态机（状态码定义见 [sync_long.v:L118-L122](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L118)）：

```text
S_SKIPPING            跳过短前导尾部 32 个样本（NUM_STS_TAIL）
        │
        ▼
S_WAIT_FOR_FIRST_PEAK 在前 64 个 metric 里记最大值，记地址 addr1
        │
        ▼
S_WAIT_FOR_SECOND_PEAK 在第二个 64 个 metric 里记最大值，记地址 addr2
        │  gap = addr2 - addr1
        │  若 62 < gap < 66：long_preamble_detected=1，对齐 in_raddr=addr1-16
        │  否则：S_IDLE（放弃）
        ▼
S_FFT                 逐符号读 64 样本，经 rotate 校频 → FFT → sample_out
```

注意 `S_FFT` 才是真正出数据的状态，前面三个状态都是「对齐准备」。这一点和 `sync_short`（检测成功就立即出脉冲）不同。

#### 4.1.3 源码精读

**端口与关键参数**（[sync_long.v:L1-L39](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L1)）：输入除了常规的 I/Q 样本外，还有从 `sync_short` 传来的 `phase_offset`（频偏）和一根 `short_gi` 配置线；输出是 `long_preamble_detected` 脉冲、`metric`（供观测）和 `sample_out`（FFT 后的频域样本）。

几个决定模块行为的局部参数：

- [sync_long.v:L29-L31](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L29) 定义 `IN_BUF_LEN_SHIFT = 8`，即输入环形缓冲 `in_buf` 深度为 \(2^8 = 256\)，足够容纳对齐所需的样本窗口；`NUM_STS_TAIL = 32` 是进入 `sync_long` 后要先丢弃的短前导尾部样本数。

**跳尾状态**（[sync_long.v:L221-L229](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L221)）：因为顶层在 `sync_short` 检出短前导后才把控制权交给 `sync_long`，此时样本流还处在 STS 末尾，需要先空转跳过 `NUM_STS_TAIL` 个样本，避免把 STS 残留当成 LTS 去相关。

#### 4.1.4 代码实践

> **实践目标**：确认「跳尾」的真实作用，并量化它对对齐起点的影响。
>
> 1. 在 `verilog/` 目录运行 `make compile && make simulate`（参考 u1-l2），用默认 24Mbps dot11a 样本仿真。
> 2. 用 gtkwave 打开 `sim_out/dot11.vcd`，把 `sync_long_inst.num_sample`、`sync_long_inst.state`、顶层 `long_preamble_detected` 加入信号列表。
> 3. 观察 `state` 从 `S_SKIPPING(0)` 进入 `S_WAIT_FOR_FIRST_PEAK(1)` 的时刻，数一下这期间经历了多少个 `sample_in_strobe` 脉冲。
>
> **预期结果**：`S_SKIPPING` 阶段正好经过 32 个有效样本（`NUM_STS_TAIL`）。若你把 `NUM_STS_TAIL` 改成 16 重新仿真，第一峰地址 `addr1` 会整体前移约 16 个样本位置（待本地验证具体数值）。

#### 4.1.5 小练习与答案

**练习**：为什么 `sync_long` 不能像 `sync_short` 那样，一旦检测成功就立刻把样本往后传，而必须先完成「双峰定位」再进 `S_FFT`？

**参考答案**：`sync_short` 输出的只是「检测到包」的脉冲，定位精度是一个 16 样本的平台，不足以确定 64 点 FFT 窗口的起点。`sync_long` 必须先用 LTS 互相关找到一个样本级精度的对齐点（`in_raddr = addr1 - 16`），才能保证后续每个符号送进 FFT 的 64 个样本正好落在数据段、不混入 GI。

---

### 4.2 LTS 互相关与 stage_mult 多级乘加

#### 4.2.1 概念说明

理想情况下，互相关应当用完整的 64 点 LTS：

\[
Y[i] = \sum_{k=0}^{63} S[i+k]\,\overline{H[k]}
\]

这要求对每个样本 \(i\) 做 64 次复数乘法。在 FPGA 上，64 个并行复乘器是巨大的资源开销。`sync_long.rst` 用一组对比图（`match_size.png`）说明了一个关键观察：**只用 LTS 的前 16 个样本做互相关，仍然能产生两个清晰、狭窄的尖峰**。于是 OpenOFDM 做了一次工程妥协，把相关长度从 64 裁到 16：

\[
Y[i] = \sum_{k=0}^{15} S[i+k]\,\overline{H[k]}
\]

16 次复乘仍然不算少，但可以拆成「4 个并行复乘器 × 4 个流水级」来实现，这就是 `stage_mult` 的来历。

#### 4.2.2 核心流程

`sync_long` 内部用一个深度 16 的移位寄存器 `cross_corr_buf` 维护「最近 16 个样本」，再通过一个 4 拍的 `mult_stage` 状态机，每拍把其中 4 个样本和对应的 4 个 LTS 共轭值送进 `stage_mult`：

```text
sample_in_strobe 到来：
  1. 新样本推入 cross_corr_buf[15]，整体下移
  2. mult_stage=1：送 buf[1..4]  × LTS共轭[1..4]   → stage_mult（4 个复乘并行）
  3. mult_stage=2：送 buf[4..7]  × LTS共轭[5..8]
  4. mult_stage=3：送 buf[8..11] × LTS共轭[9..12]
  5. mult_stage=4：送 buf[12..15]× LTS共轭[13..16]
stage_mult 把 4 个复乘结果求和，4 拍后得到 16 项相关的部分和
do_mult 用 sum_stage 把 4 个部分和累加 → 完整的 sum_i/sum_q
complex_to_mag 把 (sum_i, sum_q) 变成标量 metric
```

注意 `stage_mult` 输出 `sum` 是 `{sum_i(32), sum_q(32)}`，即一次给出 4 个复乘之和的实部和虚部。

#### 4.2.3 源码精读

**移位窗与分批发数**（[sync_long.v:L361-L390](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L361)）是 `do_mult` 任务的前半段。这里以第一拍为例：

```verilog
cross_corr_buf[15] <= sample_in;          // 推入最新样本
for (...) cross_corr_buf[i] <= cross_corr_buf[i+1];  // 整体下移

stage_X0 <= cross_corr_buf[1];   // 4 个接收样本
stage_X1 <= cross_corr_buf[2];
...
stage_Y0[31:16] <= 156;          // LTS 共轭（实部）
stage_Y0[15:0]  <= 0;            // LTS 共轭（虚部）
...
mult_strobe <= 1;                // 启动 stage_mult
```

`stage_Y0/1/2/3` 装的是 **LTS 前 16 个样本的复共轭**（因为互相关用 \(\overline{H[k]}\)）。这些常数 156、−5、40、97… 是 802.11-2012 标准 Table L-6 给出的 LTS 时域样值的定点量化值，由参考脚本 `scripts/decode.py` 中的 `LONG_PREAMBLE` 可交叉对照（[decode.py:L80-L83](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L80)）。后续 `mult_stage==1/2/3` 分别送出第 5–8、9–12、13–16 个 LTS 共轭（[sync_long.v:L392-L439](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L392)）。

**stage_mult 的并行乘加**（[stage_mult.v:L41-L110](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/stage_mult.v#L41)）：内部例化了 4 个 Xilinx `complex_multiplier` IP（`mult_inst1..4`），把 4 组 (X, Y) 复乘结果在时钟域里两两相加：

```verilog
sum_i1 <= prod_0_i + prod_1_i;   // (复乘0+复乘1) 的实部
sum_i2 <= prod_2_i + prod_3_i;   // (复乘2+复乘3) 的实部
sum[63:32] <= sum_i1 + sum_i2;   // 4 个复乘的实部之和
sum[31:0]  <= sum_q1 + sum_q2;   // 4 个复乘的虚部之和
```

输出有效标志 `output_strobe` 由一根 `delayT #(.DELAY(5))` 延时线产生（[stage_mult.v:L86-L92](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/stage_mult.v#L86)），正好抵消乘法器与累加寄存器的流水级数——这是典型的「数据走流水、strobe 走等长延时线」的握手对齐手法。

**部分和累加**（[sync_long.v:L447-L458](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L447)）：`stage_mult` 每出一拍部分和，`sum_stage` 就加 1 并累加到 `sum_i/sum_q`；当 `sum_stage==3`（4 个部分和到齐）时拉高 `sum_stb`，表示一个完整的 16 项互相关值已就绪。

**幅值近似得 metric**（[sync_long.v:L58-L69](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L58)）：`sum_i/sum_q` 送给 `complex_to_mag`（例化为 `sum_mag_inst`）。幅值近似公式为（[complex_to_mag.v:L50](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_to_mag.v#L50)）：

\[
\text{mag} \approx \max(|I|,|Q|) + \tfrac{1}{4}\min(|I|,|Q|)
\]

即 α=1、β=1/4 的快速幅值估计（平均误差约 0.006），避免开方。输出的 `metric` 就是我们用来找尖峰的标量。

#### 4.2.4 代码实践

> **实践目标**：把「16 复乘 = 4 拍 × 4 并行」的拆分在源码里走一遍。
>
> 1. 打开 `stage_mult.v`，确认它只有 4 个 `complex_multiplier` 实例（不是 16 个），理解这就是「资源妥协」的具体体现。
> 2. 打开 `sync_long.v` 的 `do_mult`，列出 `mult_stage` 从 1 到 4 时 `stage_Y0..3` 的取值，把它们两两拼成复数（高 16 位实部、低 16 位虚部），你会得到 16 个 LTS 共轭定点值。
> 3. 对比 `scripts/decode.py` 里 `LONG_PREAMBLE` 的前 16 项（注意取共轭和定点放大），看是否对得上。
>
> **预期结果**：源码里的 16 个常数与标准 LTS 时域样值（量化后）一致；若不一致，说明你漏看了「共轭」或定点缩放因子。是否完全逐一对应「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `stage_mult` 要用一根 `delayT #(.DELAY(5))` 来生成 `output_strobe`，而不是直接把 `input_strobe` 接出来？

**参考答案**：因为 4 个复乘器和后面的两级加法寄存器构成了一段流水线，数据从输入到 `sum` 有效需要若干拍。`output_strobe` 必须与数据同步到达下游，所以让 stb 走一条等长（5 拍）的延时线，实现「strobe 跟着数据一起流水」。

**练习 2**：如果把相关长度从 16 改回 64，`stage_mult` 和 `do_mult` 各需要怎样改？

**参考答案**：`stage_mult` 内的复乘器要从 4 个扩到更多（或维持 4 个但分 16 拍而非 4 拍），`do_mult` 的 `mult_stage` 要扩成 16 个状态、`cross_corr_buf` 要扩到 64 深、`sum_stage` 的终止条件也要相应放大。代价是相关耗时和数据延时显著增加。

---

### 4.3 双峰检测与对齐

#### 4.3.1 概念说明

LTS 由两段完全相同的 64 样本序列组成，因此互相关结果会出现**两个相隔恰好 64 个样本的尖峰**。这个「相隔 64」是一个极强的几何约束，可以用来剔除假峰：单看一个尖峰可能被噪声或数据段偶然匹配出来，但同时出现两个、且间距正好是 64±1 的尖峰，几乎只能是真正的 LTS。

`sync_long` 的策略因此是：在前 64 个样本里记最大峰（`addr1`），在接下来的 64 个样本里再记最大峰（`addr2`），然后校验 `gap = addr2 - addr1` 是否落在 \((62, 66)\) 区间内。

#### 4.3.2 核心流程

```text
S_WAIT_FOR_FIRST_PEAK （数 64 个 metric）：
    if metric_stb && metric > metric_max1:
        metric_max1 ← metric
        addr1       ← in_raddr - 1     // 记录当前峰位置

S_WAIT_FOR_SECOND_PEAK （再数 64 个 metric）：
    if metric_stb && metric > metric_max2:
        metric_max2 ← metric
        addr2       ← in_raddr - 1
    gap ← addr2 - addr1
    if gap > 62 && gap < 66:
        long_preamble_detected ← 1
        in_raddr       ← addr1 - 16     // 对齐到 LTS 真正起点
        num_input_consumed ← addr1 - 16
        next_phase_correction ← phase_offset
        state ← S_FFT
    else:
        state ← S_IDLE                  // 放弃，等下一包
```

#### 4.3.3 源码精读

**第一峰**（[sync_long.v:L231-L247](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L231)）：

```verilog
do_mult();
if (metric_stb && (metric > metric_max1)) begin
    metric_max1 <= metric;
    addr1       <= in_raddr - 1;   // 减 1 抵消 do_mult 里的地址自增
end
```

注意 `addr1 <= in_raddr - 1` 的「−1」：因为 `do_mult` 在 `mult_stage==4` 时已经把 `in_raddr` 自增了一次（[sync_long.v:L443-L444](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L443)），所以当前 metric 对应的窗口起点要回退一拍。

**第二峰与 gap 校验**（[sync_long.v:L249-L283](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L249)）：

```verilog
if (num_sample >= 64) begin
    if (gap > 62 && gap < 66) begin
        long_preamble_detected <= 1;
        in_raddr <= addr1 - 16;           // 对齐到 LTS 起点
        num_input_consumed <= addr1 - 16;
        next_phase_correction <= phase_offset;   // 频偏累加种子
        state <= S_FFT;
    end else begin
        state <= S_IDLE;                  // 间距不对，判为假峰
    end
end
```

「−16」的注释写得很清楚（[sync_long.v:L268-L270](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L268)）：offset by the length of cross correlation buffer size。因为互相关用的是 16 点窗口，峰值出现时 `in_raddr` 已经指到窗口末尾，真正的 LTS 起点要往前扣 16。文档 `sync_long.rst` 也指出：若第一峰在样本 \(N\)，则 160 样本的长前导起始于 \(N-32\)（其中 32 = CP 长度），这里 `addr1-16` 是把读指针对齐到 LTS1 的 FFT 窗口起点。

#### 4.3.4 代码实践

> **实践目标**：在仿真波形里验证「双峰间距 = 64」。
>
> 1. 仿真结束后，打开 `sim_out/sync_long_metric.txt`（测试台在 [dot11_tb.v:L185-L186](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L185) 把每个有效 `metric` 落盘）。
> 2. 找出 metric 的两个局部最大值，记录它们对应的样本序号，相减得到 gap。
> 3. 同时打开 `sim_out/sync_long_frame_detected.txt`，确认 `long_preamble_detected` 拉高的时刻正好发生在第二个峰之后。
>
> **预期结果**：两个尖峰的间距约为 64（落在 62～66 区间），`long_preamble_detected` 是一个单拍脉冲，紧随第二峰出现。

#### 4.3.5 小练习与答案

**练习**：如果把 gap 判定区间从 `(62,66)` 放宽到 `(60,68)`，会对系统行为有什么影响？

**参考答案**：放宽区间会提高「检出率」——在低信噪比、频偏较大导致尖峰位置抖动时仍能判为成功；但代价是「误检率」上升，可能把非 LTS 的两段相似数据误判为前导，导致后续 FFT 窗口错位、解出一包垃圾。工程上 64±1 是经验权衡。

---

### 4.4 FFT 窗口、GI 跳过与频偏旋转

#### 4.4.1 概念说明

对齐之后，`sync_long` 进入 `S_FFT`，这是它真正「出活」的状态。它要完成三件事：

1. **逐符号读取 64 样本送 FFT**：每个 OFDM 符号取末尾 64 个样本（即去掉头部 GI 后的数据段）做 64 点 FFT。FFT 由 Xilinx IP `xfft_v7_1`（实例名 `dft_inst`）承担。
2. **跳过保护间隔 GI**：符号与符号之间隔着一个 GI（normal=16，SGI=8），读指针要跨过它才能对准下一个符号的数据段。
3. **FFT 前做频偏旋转**：把 `sync_short` 给的 `phase_offset` 按 \(m\cdot\alpha_{ST}\) 逐样本累加，用 `rotate` 模块旋转样本，完成 CFO 校正。

#### 4.4.2 核心流程

`gi_skip` 的取值是本模块的一个关键设计点（[sync_long.v:L36](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L36)）：

```verilog
wire [7:0] gi_skip = short_gi ? 9 : 17;
```

为什么是 9 和 17，而不是 GI 的 8 和 16？因为 `gi_skip` 表示的是「读完当前符号第 64 个样本后，读指针还要再前进多少步才能落到下一个符号的数据段起点」。设当前符号最后一个 FFT 样本为 \(s_{79}\)，下一个符号的数据段第一个样本为 \(s_{\text{next}}\)：

- normal GI：下个符号结构是 `[GI(16) | data(64)]`，\(s_{\text{next}} = s_{79} + 1 + 16 = s_{96}\)，即跨 \(1 + 16 = 17\)。
- SGI：下个符号结构是 `[GI(8) | data(64)]`，\(s_{\text{next}} = s_{79} + 1 + 8 = s_{88}\)，即跨 \(1 + 8 = 9\)。

所以 `gi_skip = GI + 1`，多出来的 1 用来跨过当前符号的最后一个样本。公式化为：

\[
\text{gi\_skip} = (\text{short\_gi}\;?\;8 : 16) + 1
\]

> 注意：LTS 的两个 64 样本之间没有 GI（它们紧邻），所以读完第一个 LTS（`num_ofdm_symbol==0`）时只 `+1`，不走 `gi_skip`，从第二个 LTS（即数据符号）开始才跳 GI。

#### 4.4.3 源码精读

**输入缓冲**（[sync_long.v:L143-L156](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L143)）：用 `ram_2port` 做一个 256 深的双口 RAM `in_buf`，A 口随 `sample_in_strobe` 写入新样本，B 口在 FFT 加载时按 `in_raddr` 读出 `raw_i/raw_q`。这正是 [delay_sample.v:L27-L40](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/delay_sample.v#L27) 所用的同一类「RAM 当延时线」手法。

**频偏旋转**（[sync_long.v:L158-L174](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L158)）：从 `in_buf` 读出的 `raw_i/raw_q` 先经 `rotate`（实例 `rotate_inst`）按 `phase_correction` 旋转，再送 FFT。相位累加逻辑在 [sync_long.v:L304-L322](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L304)，核心是：

```verilog
phase_correction       <= next_phase_correction;
next_phase_correction  <= next_phase_correction + phase_offset;  // 逐样本累加
```

即每个送 FFT 的样本，相位都在前一个的基础上再加一个 `phase_offset`，实现 \( \phi_m = m\cdot\alpha_{ST} \)。当累加值越过 ±π 时，减去/加上 `DOUBLE_PI` 折叠回主值区间（`PI`/`DOUBLE_PI` 定义见 [common_params.v:L6-L7](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L6)）。这就是 u2-l3 讲过的「\(S'[m]=S[m]e^{-jm\alpha_{ST}}\)」在硬件里的逐样本落地。

**FFT 启动握手**（[sync_long.v:L293-L301](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L293)）：只有当缓冲里可用样本 `num_input_avail > 64` 时才拉高 `fft_start`，避免读超前于写；`fft_start` 经一根 `delayT #(.DELAY(9))` 延时成 `fft_start_delayed`（[sync_long.v:L176-L182](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L176)）再去触发 `xfft_v7_1` 的 `start`，对齐 IP 的建立时序。

**GI 跳过**（[sync_long.v:L324-L342](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L324)）是本讲实践的重点：

```verilog
if (in_offset == 63) begin              // 一个符号的 64 样本读完
    fft_loading <= 0;
    num_ofdm_symbol <= num_ofdm_symbol + 1;
    if (num_ofdm_symbol > 0) begin
        // skip the Guard Interval for data symbols
        in_raddr <= in_raddr + gi_skip;          // 数据符号：跳 GI（17 或 9）
        num_input_consumed <= num_input_consumed + gi_skip;
    end else begin
        in_raddr <= in_raddr + 1;                // LTS1→LTS2：无 GI，仅 +1
        num_input_consumed <= num_input_consumed + 1;
    end
end else begin
    in_raddr <= in_raddr + 1;                    // 符号内顺序推进
    num_input_consumed <= num_input_consumed + 1;
end
```

`in_offset` 在每个符号内从 0 数到 63，数满 64 即一个 FFT 块结束；此时根据是不是第一个符号（LTS1）决定下一步地址跳多少。

**FFT 输出**（[sync_long.v:L137](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L137) 与 [L344-L345](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L344)）：`fft_out = {fft_out_re[22:7], fft_out_im[22:7]}`，从 IP 的 23 位输出里取高 16 位（实部、虚部各 16 位），随 `fft_valid` 通过 `sample_out`/`sample_out_strobe` 送给下游 `equalizer`。

#### 4.4.4 代码实践

> **实践目标**：在波形里把 `gi_skip` 的 9/17 切换、对齐 `sample_out` 的时序关系看清楚。
>
> 1. 用一个 **dot11a**（normal GI）样本仿真，在 gtkwave 里把 `sync_long_inst.short_gi`、`sync_long_inst.in_raddr`、`sync_long_inst.in_offset`、`sync_long_inst.num_ofdm_symbol`、`sync_long_inst.sample_out_strobe` 加入。
> 2. 在 `in_offset` 从 63 翻回 0 的边沿上，观察 `in_raddr` 的增量：当 `num_ofdm_symbol==0`（LTS1→LTS2）时应为 +1；当 `num_ofdm_symbol>0`（数据符号）时应为 +17。
> 3. 再用一个 **dot11n SGI** 样本（若 `testing_inputs` 中有）重复，确认数据符号的增量变为 +9。
> 4. 在 `sample_out_strobe` 第一个脉冲处，回溯它相对 `long_preamble_detected` 延迟了多少个时钟——这段延迟就是 FFT IP 的流水深度。
>
> **预期结果**：normal GI 下数据符号间 `in_raddr` 增量为 17，SGI 下为 9；`sample_out` 的有效输出在 `long_preamble_detected` 之后约数十拍（FFT 流水延迟）出现。SGI 样本的可用性「待本地确认」。

#### 4.4.5 小练习与答案

**练习 1**：`num_ofdm_symbol==0` 时为什么用 `in_raddr + 1` 而不是 `gi_skip`？

**参考答案**：`num_ofdm_symbol==0` 对应刚读完 LTS1。LTS1 和 LTS2 在长前导里是紧邻的两段 64 样本，中间没有保护间隔（GI 只在 LTS 最前面的 CP 一次性给出 32 样本），所以从 LTS1 末尾到 LTS2 起点只需跨过最后一个样本（+1）。从 LTS2 开始才进入正常的「GI+data」符号结构，才需要跳 `gi_skip`。

**练习 2**：`fft_start` 为什么要先经 `delayT` 延时 9 拍才去触发 `xfft_v7_1.start`？

**参考答案**：从拉高 `fft_start` 到第一批 `xn_re/xn_im` 数据有效，中间隔着 `rotate` 旋转和读 RAM 的流水级数。直接把 `fft_start` 接到 FFT IP 的 `start`，会导致 IP 在数据尚未就绪时就开始采样。延时 9 拍是为了让 `start` 信号与第一个有效输入样本同步到达 IP。

---

## 5. 综合实践

**任务**：把本讲四个最小模块串起来，画一张 `sync_long` 的「数据 + 控制」时序图，并用仿真数据验证一个关键数字。

具体步骤：

1. **画控制时序**：依据 4.1 的五状态机，画一条时间轴，标出 `S_SKIPPING`（32 样本）→ `S_WAIT_FOR_FIRST_PEAK`（64 metric）→ `S_WAIT_FOR_SECOND_PEAK`（64 metric）→ `S_FFT` 的状态切换点，并在 `S_FFT` 段内标出每个符号的 64 样本窗口和 GI 跳空。
2. **画数据通路**：从 `sample_in` 出发，画 `in_buf(ram_2port)` → `raw_i/q` → `rotate`（受 `phase_correction` 控制）→ `xfft_v7_1` → `fft_out` → `sample_out` 的链路；另画一条对齐通路：`cross_corr_buf` → `stage_mult` → `sum_i/q` → `complex_to_mag` → `metric` → 取峰逻辑 → `in_raddr`。
3. **验证一个数字**：仿真后从 `sim_out/sync_long_metric.txt` 读出两个尖峰的样本序号之差（应≈64），从 `sim_out/sync_long_out.txt` 数一个完整 OFDM 符号输出的样本数（应为 64）。把这两个数字标注在你的时序图上。
4. **回答**：若 `short_gi=1`，你时序图里「数据符号间的跳空」应该从多少改成多少？为什么是 9 而不是 8？

> 提示：这是「源码阅读 + 仿真验证」型实践，不要求改源码。若某些测试样本缺失，相应结论标注「待本地验证」即可，不要假装跑过。

## 6. 本讲小结

- `sync_long` 的职责是**符号定时**：用 LTS 互相关找到样本级精度的对齐点，再把样本逐符号送进 FFT。
- 为了省 FPGA 资源，64 点互相关被裁成**只用 LTS 前 16 个样本**，并由 `stage_mult` 用「4 并行复乘 × 4 拍流水」拼出，最终用 `complex_to_mag` 的 α=1/β=1/4 快速幅值近似得到标量 `metric`。
- 定位靠**双峰 + 间距校验**：在前 64 和后 64 各取最大峰，要求 `gap = addr2 - addr1` 落在 \((62,66)\)，通过后把 `in_raddr` 对齐到 `addr1 - 16`，并发出 `long_preamble_detected` 单拍脉冲。
- `gi_skip = short_gi ? 9 : 17`，即 `GI + 1`（normal 16、SGI 8），用于读完一个符号的 64 样本后跨到下一个符号的数据段；LTS1→LTS2 因无 GI 而单独走 `+1`。
- FFT 前由 `rotate` 按 \(m\cdot\alpha_{ST}\) 逐样本旋转，承接 `sync_short` 的粗频偏 `phase_offset`，完成 CFO 校正的下半场；FFT 由 Xilinx IP `xfft_v7_1` 实现，输出取高 16 位送下游 `equalizer`。
- 全模块延续项目的「数据 + strobe」握手风格，多处用 `delayT` 让 strobe 与流水数据同步到达。

## 7. 下一步学习建议

- 下一篇 [u3-l1 信道均衡 equalizer.v](u3-l1-equalizer.md) 将接收本模块 `sample_out` 输出的频域样本，用 LTS 做信道估计与逐子载波均衡——你会看到 `sync_long` 对齐出来的 LTS1/LTS2 在那里被用来除以参考 LTS。
- 建议顺带阅读 [docs/source/sync_long.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/sync_long.rst) 的「FFT」一节，对照官方对「裁剪到 16 样本」这一取舍的图示说明。
- 想深入理解 FFT IP 的行为，可回到 [verilog/coregen/xfft_v7_1.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/xfft_v7_1.v) 阅读其仿真行为模型，这部分将在 u6 单元「Xilinx IP 与 coregen 依赖」中系统讲解。
