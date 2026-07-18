# 扩展实践：新增调制/MCS/带宽支持

## 1. 本讲目标

OpenOFDM 出厂只支持 802.11a/g 全速率与 802.11n MCS 0–7（20 MHz、单流）。本讲不改代码，而是回答一个工程问题：**「要新增一种调制（例如 256-QAM）或一条 MCS，甚至上 40 MHz 带宽，到底要动哪些文件、哪些查找表、哪些状态机校验？」** 读完本讲，你应当能够：

1. 画出一张「速率敏感点」清单，说明 OFDM 解码链中哪些模块会随调制方式/MCS/带宽而变。
2. 推导把 `demodulate` 从 64-QAM 扩到 256-QAM 时，判决门限该怎么算、位宽该怎么加。
3. 说清解交织 LUT（`deinter_lut`）和 Python 参考解码器（`decode.py`）必须如何同步更新。
4. 解释 40 MHz 带宽为什么「不只是放开 `ht_cbw` 这一道门」，而是牵动 FFT 点数、子载波掩码乃至采样节拍。

本讲是 u6 单元里最「系统思维」的一篇：重点不在算法，而在**变更影响分析（change-impact analysis）**。

## 2. 前置知识

阅读本讲前，请确保已掌握以下概念（对应前置讲义）：

- **rate 字段的编码格式**（u3-3、u4-1）：`rate[7]=0` 表示 legacy 802.11a，`rate[3:0]` 是速率位；`rate[7]=1` 表示 802.11n，`rate[6:0]` 是 MCS。HT 模式下顶层用 `pkt_rate <= {1'b1, ht_mcs}` 拼出 8 位 rate。
- **demodulate 的星座表与定点刻度**（u3-3、u6-1）：判决门限全部从一个 `MAX = 1<<CONS_SCALE_SHIFT`（=1024）派生，背后是「最外层星座点幅度 = MAX」的隐含契约。
- **deinterleave 的两级查表**（u3-4）：用 `{ht, rate[3:0]}` 查 32 项表头，再顺序读子表完成比特重排与去穿孔。
- **S_CHECK_HT_SIG 校验关卡**（u4-3）：HT-SIG 解出后，状态机用 8 道围栏拒绝 MCS>7、40 MHz、STBC、LDPC、SGI、多空间流等不支持特性。
- **「数据 + strobe」握手与软判决**（u3-5）：Viterbi 前把 1 bit 硬判决放大成 3 bit 软判决 `3'b111/3'b011`，这一段与具体调制无关。

几个术语对齐：**MCS**（Modulation and Coding Scheme，调制与编码方案，一个编号同时编码调制方式与卷积码码率）；**N_BPSC**（每子载波比特数，BPSK=1、QPSK=2、16-QAM=4、64-QAM=6、256-QAM=8）；**N_CBPS**（每 OFDM 符号编码比特数 = N_BPSC × 数据子载波数）；**N_DBPS**（每符号数据比特数 = N_CBPS × 码率）；**去穿孔（de-puncture）**——OpenOFDM 的 Viterbi 是固定 1/2 母码，3/4、2/3、5/6 靠接收端在删比特处补 `erase` 还原成 1/2 节奏。

> ⚠️ 关于「MCS 8/9 = 256-QAM」的说明：严格按 802.11n 标准，MCS 8–31 是**多空间流（MIMO）**，而 256-QAM 是 802.11ac（VHT）才引入的调制，11n 单流槽位里并没有 256-QAM。本讲把 256-QAM 当作一个**单流扩展练习**，借用 MCS 8/9 这两个当前被 `ht_mcs > 7` 关卡挡掉的编号来承载它——目的是讲透「扩展一个速率要改哪些地方」，**不追求产出符合标准的实现**。这也是 OpenOFDM 上板只到 MCS 7 的现实原因。

## 3. 本讲源码地图

| 文件 | 在本讲的角色 |
|---|---|
| [verilog/demodulate.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v) | 星座扩展的主战场：rate→调制 `case` 表、判决门限、6 bit 输出位宽 |
| [verilog/common_defs.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_defs.v) | `CONS_SCALE_SHIFT` 定点刻度，决定门限分辨率 |
| [verilog/deinterleave.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v) | 每载波比特位宽（RAM DWIDTH）与 `lut_key` 索引 |
| [verilog/equalizer.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v) | 20 MHz 的 64 位子载波掩码与幅度契约（调制无关的代表） |
| [verilog/dot11.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) | HT-SIG 字段切片与 `S_CHECK_HT_SIG` 校验关卡 |
| [verilog/common_params.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v) | `E_UNSUPPORTED_*` 错误码与状态码定义 |
| [verilog/ofdm_decoder.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v) | 佐证「软判决段与调制无关」 |
| [verilog/sync_long.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v) | FFT 实例化点，40 MHz 扩展「最贵」改动落在这里 |
| [scripts/gen_deinter_lut.py](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py) | 离线生成解交织 LUT，扩展速率必须在此登记 |
| [scripts/decode.py](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py) | Python 浮点参考解码器，必须与 RTL 同步扩展 |

## 4. 核心概念与源码讲解

### 4.1 速率敏感点盘点：哪些模块随调制/MCS/带宽变化

#### 4.1.1 概念说明

OpenOFDM 的解码流水线是固定的 8 步，但其中**只有少数几步是「速率敏感」的**——它们的行为会因调制方式、码率、带宽而不同。扩展速率的第一步，不是写代码，而是把这条链路上所有「会读 rate/MCS/带宽」的点列出来。

一个判断窍门：**问自己「这段逻辑是否需要知道每个子载波承载几比特？」** 如果需要，它就是调制敏感点；如果只看样本的相位/幅度/能量，它就与调制无关。

#### 4.1.2 核心流程

按数据流向，速率敏感点与速率无关点的分布如下（★ 必改，☆ 不用改）：

```
sample_in
  │
  ▼
☆ power_trigger ──── 速率无关（只看能量门限）
☆ sync_short ──────── 速率无关（只看 STS 周期性）
☆ sync_long(FFT) ──── 【带宽敏感】FFT 点数随带宽变（20MHz=64, 40MHz=128）
  │
  ▼
☆ equalizer ───────── 【带宽敏感】子载波掩码、导频位置、LTS_REF 随带宽变
  │                   （调制无关：它只做 X/H 除法）
  ▼
★ demodulate ──────── 【调制敏感】rate→星座 case、判决门限、每载波比特数
★ deinterleave ────── 【调制+码率敏感】N_CBPS、去穿孔模式、每载波比特位宽
☆ viterbi ─────────── 速率无关（固定 1/2 母码 + erase）
☆ descramble ──────── 速率无关（LFSR）
☆ bits_to_bytes ───── 速率无关
  │
  ▼
byte_out → crc32 ──── 速率无关
```

控制平面另有三个速率敏感点：`S_CHECK_SIGNAL`（legacy rate 校验）、`S_CHECK_HT_SIG`（HT 能力围栏）、以及 `num_bits_to_decode` 的长度计算。

**关键结论**：扩展一种调制，核心改动集中在 **demodulate + deinterleave + 一张 LUT + 状态机围栏 + Python 参考**这五处；FFT/viterbi/crc32 都不用动。换带宽则相反——它击穿的是「子载波结构相关」的整条前端（4.4 详述）。

#### 4.1.3 源码精读

**rate 字段如何被消费**——`demodulate` 用 `rate` 的高位与低 4 位联合查星座：

[verilog/demodulate.v:61-83](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v#L61-L83) 用 `{rate[7], rate[3:0]}` 这 5 位把 rate 映射到 `BPSK/QPSK/QAM_16/QAM_64` 四个 localparam。其中 `rate[7]=1` 的 8 个分支（`5'b10000`~`5'b10111`）正是 MCS 0–7。**MCS 8/9 在这张表里完全没有分支**，会落到 `default: mod <= BPSK`——这就是第一个要补的洞。

**rate 在 deinterleave 里如何被消费**——

[verilog/deinterleave.v:111](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L111) 在写满一个符号后，用 `lut_key <= {6'b0, ht, rate[3:0]}` 作为 LUT 地址去查子表起始偏移。MCS 8 对应 `rate[3:0]=4'b1000`，MCS 9 对应 `4'b1001`，地址分别是 24、25（见 4.3.3）。

**rate 在状态机里如何被消费**——

[verilog/dot11.v:766](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L766) HT 数据用 `pkt_rate <= {1'b1, ht_mcs}` 组装 rate 字段（`ht_mcs` 来自 `ht_sig1[6:0]`，见 [verilog/dot11.v:213](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L213)）。注意 MCS 字段是 **7 位**（[verilog/dot11.v:67](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L67)），理论上能表达 0–127，但 [verilog/dot11.v:712](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L712) 的 `if (ht_mcs > 7)` 把它卡死在 0–7。

**调制无关的佐证**——

[verilog/ofdm_decoder.v:145-146](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L145-L146) 把 deinterleave 的 1 bit 硬判决映射成 `3'b111/3'b011` 软判决。无论上游是 64-QAM 还是 256-QAM，到这里都已是 1 bit，所以软判决段与调制无关。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认 rate 字段在整条链路上的所有消费点，建立「速率敏感点」全图。

**步骤**：

1. 在 `verilog/` 目录执行（只读检索）：
   ```bash
   grep -rn "rate\[" verilog/*.v
   grep -rn "ht_mcs\|ht_cbw\|num_data_carrier" verilog/*.v
   ```
2. 逐条分类：哪些是「读 rate 选调制/选子表」，哪些只是「透传 rate 给下游」。
3. 把命中点归入「调制敏感」「带宽敏感」「子载波敏感」三栏。

**需要观察的现象**：真正「用 rate 做决策」的 RTL 只有 `demodulate.v`（case 表）、`deinterleave.v`（lut_key）、`dot11.v`（S_CHECK_HT_SIG）三处，其余全是透传。

**预期结果**：得到一张三栏表（文件 : 行号 : 用途），覆盖 demodulate 的 case、deinterleave 的 lut_key、dot11 的 S_CHECK_HT_SIG，与 4.1.2 的渗透图一致。

#### 4.1.5 小练习与答案

**Q1**：`viterbi.v` / `crc32.v` 需要为 256-QAM 做修改吗？为什么？

**答**：不需要。Viterbi 吃的是去交织后的 1 bit 硬判决（再被 ofdm_decoder 放大成 3 bit 软判决），crc32 吃的是装配好的字节——两者都工作在「比特/字节」层，与每子载波承载几比特无关。

**Q2**：为什么 `equalizer` 是「带宽敏感」却「调制无关」？

**答**：均衡器做的是 `X/H`（接收符号 ÷ 信道复增益），这是线性运算，对任何调制都一样；但子载波总数、导频位置、LTS 参考序列都随带宽而变，所以它带宽敏感。

---

### 4.2 demodulate：从 64-QAM 扩展到 256-QAM

#### 4.2.1 概念说明

`demodulate` 是「频域复数 → 比特」的转折点。256-QAM 每个子载波承载 **8 比特**（I 路 4 bit + Q 路 4 bit），是 64-QAM（6 bit）的下一档。扩展它要解决三件事：

1. **加一个星座分支**：在 case 表里为 MCS 8/9 增加 `QAM_256`。
2. **算判决门限**：256-QAM 每轴 8 个幅度等级，需要 7 道判决边界。
3. **加宽输出位宽**：当前 `bits` 只有 6 位，装不下 8 bit。

#### 4.2.2 核心流程

**幅度等级与门限推导**。沿用 OpenOFDM「最外层星座点幅度 = MAX」的契约（见 u3-3、u6-1），其中 `MAX = 1<<CONS_SCALE_SHIFT = 1024`：

[verilog/demodulate.v:17](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v#L17) 与 [verilog/common_defs.v:9](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_defs.v#L9)。

OpenOFDM 的 QAM 约定：每轴幅度等级是 ±1, ±3, ±5, … 的奇数序列，最外层被归一化到 `MAX`；判决门限取相邻等级的中点（偶数 2,4,6,…），写成 `MAX` 的分数。

| 调制 | 每轴幅度等级 | 相邻中点 | 门限占最外层比例 | 代码常量 |
|---|---|---|---|---|
| 16-QAM | ±1, ±3 | 2 | 2/3 | `QAM_16_DIV = MAX*2/3` |
| 64-QAM | ±1, ±3, ±5, ±7 | 2, 4, 6 | 2/7, 4/7, 6/7 | `QAM_64_DIV_{0,1,2}` |
| **256-QAM** | ±1, ±3, …, ±15 | 2, 4, 6, 8, 10, 12, 14 | 2/15, 4/15, …, 14/15 | **新增 `QAM_256_DIV_{0..6}`** |

256-QAM 的 7 道门限为：

\[
\texttt{QAM\_256\_DIV}_k = \texttt{MAX}\cdot\frac{2(k+1)}{15},\quad k=0,1,\dots,6
\]

代入 MAX=1024，得约 \(136, 273, 410, 546, 683, 819, 956\)。

**位宽**：每轴 1 位符号 + 3 位幅值 = 4 位，两轴合计 8 位，因此 `bits` 必须从 6 位加宽到 8 位。

> ⚠️ **契约风险**：「最外层 = MAX」假设发送端星座归一化让最外层落在固定幅度。真实 802.11 各调制有不同的 \(K_\text{MOD}\) 归一化因子，最外层幅度并不一致。落地时门限应**根据实际抓包样本（或 Python 参考均衡后的星座点）校准**，而非照搬公式。本讲公式给出的是「保持现有契约」下的设计默认值。

#### 4.2.3 源码精读

**6 位输出是硬约束**——

[verilog/demodulate.v:13](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v#L13) 声明 `output reg [5:0] bits`。这 6 位正好够 64-QAM（I/Q 各 3 bit）。256-QAM 需要 8 位，所以这一行要改成 `[7:0]`，并且会**连锁影响 deinterleave 的输入位宽与 RAM**（见 4.3）。

**门限派生**——

[verilog/demodulate.v:17-23](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v#L17-L23) 定义 `MAX` 与 `QAM_16_DIV`、`QAM_64_DIV_{0,1,2}`，全部从 `MAX` 派生。256-QAM 在此补 `localparam QAM_256 = 5;` 与 7 个 `QAM_256_DIV_*`。

**64-QAM 判决写法是 256-QAM 的模板**——

[verilog/demodulate.v:102-111](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v#L102-L111) 用「符号位 + 两道幅值比较」得到每轴 3 bit。256-QAM 只需把「两道比较」扩成「三道比较」（每轴再增 1 个幅值位），输出每轴 4 bit。注意比特排列必须与 802.11 的格雷码映射一致，并和 Python `Demodulator` 对齐（见 4.4）。

**case 表的缺口**——

[verilog/demodulate.v:72-80](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v#L72-L80) 列出了 MCS 0–7（`5'b10000`~`5'b10111`），MCS 8/9（`5'b11000`/`5'b11001`）没有分支，会落到 [verilog/demodulate.v:82](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v#L82) 的 `default`。

#### 4.2.4 代码实践（设计型，不落地）

**目标**：算出 256-QAM 的门限常数，并写出 case 与判决分支的「草稿」（标注为示例代码，不写入源码）。

**步骤**：

1. 用 `python3 -c "M=1024; print([M*2*(k+1)//15 for k in range(7)])"` 算出 7 道门限的整数值。
2. 仿照 [verilog/demodulate.v:19-23](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v#L19-L23) 的写法，写出（示例代码，非项目原有）：
   ```verilog
   // 示例代码：256-QAM 门限（保持「最外层=MAX」契约）
   localparam QAM_256 = 5;
   localparam QAM_256_DIV_0 = MAX*2/15;
   localparam QAM_256_DIV_1 = MAX*4/15;
   // ... 直到 QAM_256_DIV_6 = MAX*14/15;
   ```
3. 在 case 表里补 `5'b11000`/`5'b11001` → `mod <= QAM_256`。

**需要观察的现象**：当 `CONS_SCALE_SHIFT` 仍是 10 时，最小门限约 136，最大约 956，都落在 0–1024 内且彼此可区分；说明现有刻度对 256-QAM **勉强够用**。

**预期结果**：得到一份 256-QAM 门限表与一段 case/判决草稿。若担心分辨率不足（相邻门限最近间距 ~136，噪声裕度变小），可参考 u6-l1 把 `CONS_SCALE_SHIFT` 提到 11——但那会牵动 equalizer 的 `<<CONS_SCALE_SHIFT` 与 demodulate 全部门限，属于「牵一发动全身」的改动。

> 待本地验证：门限是否需要按真实抓包样本校准（见上方契约风险）；Verilog 整数除法为截断，实际数值与四舍五入略有差异。

#### 4.2.5 小练习与答案

**Q1**：为什么 256-QAM 每轴只需 3 道幅值比较（加符号位）就能得到 4 bit？

**答**：每轴 8 个等级，需要 \(\lceil\log_2 8\rceil=3\) 个幅值位；加 1 个符号位共 4 bit。3 道比较能把正半轴的 4 个等级一一区分（类似 64-QAM 用 2 道比较分正半轴 2 个幅值位）。

**Q2**：如果把 `CONS_SCALE_SHIFT` 从 10 改成 11，`QAM_64_DIV_0 = MAX*2/7` 会自动跟着变吗？

**答**：会。因为 `MAX = 1<<CONS_SCALE_SHIFT`，门限全部以 `MAX*...` 表达，shift 一改全部门限自动缩放——这正是 u6-l1 强调的「门限从 MAX 派生」的好处。

---

### 4.3 deinterleave 位宽与 LUT 重新生成，equalizer 子载波约定

#### 4.3.1 概念说明

解交织是「符号内全局位置重排」：必须先把一个 OFDM 符号的全部 \(N_\text{CBPS}\) 个比特缓存进双口 RAM，再按原始顺序读出。256-QAM 让 \(N_\text{CBPS}\) 从 64-QAM 的 \(6\times52=312\) 涨到 \(8\times52=416\)，所以：

- **每载波比特位宽**要从 6 加到 8（RAM 的 DWIDTH）。
- **解交织映射表**要按新的 \(N_\text{CBPS}\) 重新生成（`deinter_lut`）。
- **equalizer 的子载波掩码**对 256-QAM @ 20MHz **不用改**（子载波结构没变，还是 52 个活跃）；只有换带宽才动它。

#### 4.3.2 核心流程

解交织 RAM 的容量与位宽：

| 项 | 64-QAM (MCS 7) | 256-QAM (练习) | 是否要改 |
|---|---|---|---|
| 数据子载波数 (HT 20MHz) | 52 | 52 | 否 |
| 每载波比特 \(N_\text{BPSC}\) | 6 | 8 | 是（位宽） |
| \(N_\text{CBPS}\) | 312 | 416 | 是（LUT 内容） |
| RAM 深度（AWIDTH=6，64 深） | 够 | 够（52<64） | 否 |
| RAM 位宽（DWIDTH） | 6 | 8 | **是** |

LUT 重生成流程：在 `gen_deinter_lut.py` 的 `RATES` 列表登记 MCS 8/9 → 脚本调用 `decode.deinterleave()` 算出 \(N_\text{CBPS}=416\) 的重排映射 → 按 MCS 选定的码率生成去穿孔指令 → 写进 `deinter_lut.mif/.coe` 的第 24/25 号子表。

#### 4.3.3 源码精读

**RAM 位宽是硬约束**——

[verilog/deinterleave.v:54](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L54) 例化 `ram_2port #(.DWIDTH(6), .AWIDTH(6))`。256-QAM 必须把 `DWIDTH(6)` 改成 `DWIDTH(8)`，同时把上游 `in_bits [5:0]`（[verilog/deinterleave.v:11](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L11)）与下游 `bit_outa/bit_outb [5:0]` 都加宽到 8 位。

**位选信号位宽恰好够用**——

[verilog/deinterleave.v:37-38](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L37-L38) 的 `lut_bita/lut_bitb` 是 3 位，能索引 0–7，正好覆盖 256-QAM 的 8 比特——**这块不用加宽**，是个小幸运。

**子载波数对 HT 已经是 52**——

[verilog/deinterleave.v:19-22](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L19-L22) 用 `ht` 选 52（HT）或 48（legacy）。256-QAM 走 HT 分支，子载波数不变——这是「换调制不改子载波」的直接证据。

**LUT 的两级查表与空槽位**——

[verilog/deinterleave.v:27](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L27) 与 [verilog/deinterleave.v:111](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L111)：`lut_key` 是 11 位，查表头时填 `{6'b0, ht, rate[3:0]}`，即 `{ht, rate[3:0]}` 这 5 位索引（范围 0–31）。HT MCS 的索引 = `16 + MCS`（见 [scripts/gen_deinter_lut.py:196-200](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L196-L200) 的 `idx = (1<<4) + mcs`），于是 MCS 8→索引 24、MCS 9→索引 25，**完全落在 32 项表头之内**，连 `lut_key` 位宽都不用动。

**ROM 端有富余容量**——

[verilog/coregen/deinter_lut.v:47-48](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/deinter_lut.v#L47-L48)（`addra` 11 位、`douta` 22 位）与 [verilog/coregen/deinter_lut.v:82-84](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/deinter_lut.v#L82-L84)（读深度 2048）确认：MCS 8/9 各增加一段子表（几百行），远未触及 2048 行上限，故 ROM 接口不用重建，只需重新生成 `.mif` 内容。

**equalizer 的 20MHz 子载波结构（不用改）**——

[verilog/equalizer.v:31-45](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L31-L45) 定义了 64 位掩码（52 活跃 + 4 导频）。256-QAM @ 20MHz 复用这套掩码。[verilog/equalizer.v:490-491](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L490-L491) 的输出取 `norm_i[14:0]`（15 位幅值 + 符号），这就是 demodulate 门限赖以成立的「MAX=1024」幅度契约的来源——改 demodulate 门限时，这一步是校准基准。

#### 4.3.4 代码实践（工具链型）

**目标**：让 LUT 生成脚本「认识」MCS 8/9，并确认子表能落进现有 ROM 容量。

**步骤**：

1. 读 [scripts/gen_deinter_lut.py:65-84](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L65-L84) 与 [scripts/decode.py:480-489](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L480-L489)，确认 Python 的交织公式完全由 `n_cbps`（来自 `HT_MCS_PARAMETERS`）参数化。
2. 在 `HT_MCS_PARAMETERS`（[scripts/decode.py:106-115](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L106-L115)）中新增两条（示例数据，需校准）：
   ```python
   # 示例代码：256-QAM，N_BPSC=8，N_CBPS=416
   8: (8, 416, 208),   # R=1/2 -> N_DBPS=208
   9: (8, 416, 312),   # R=3/4 -> N_DBPS=312
   ```
   > 选 1/2 与 3/4 是因为 \(416 \times 1/2=208\)、\(416\times3/4=312\) 都是整数；\(416\times2/3\)、\(416\times5/6\) 不是整数（\(416=2^5\times13\)），故避开 2/3、5/6 码率。这也是为什么把 256-QAM 塞进 HT 52 子载波框架是个教学简化——真实 VHT 用不同 \(N_\text{SD}\) 才能支持 5/6。
3. 在 `gen_deinter_lut.py` 的 `RATES` 末尾追加 `(0, 8, True)`、`(0, 9, True)`，并仿照 [scripts/gen_deinter_lut.py:93-102](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L93-L102) 给 MCS 8 选 `erase='1/2'`、MCS 9 选 `erase='3/4'`。
4. 运行脚本（Python 2 环境），观察它打印的 `Total row`。

**需要观察的现象**：脚本会打印新增子表的偏移与总行数，并向上取整到 2 的幂。

**预期结果**：总行数远小于 `deinter_lut` ROM 的 2048 深度，说明**现有 ROM 容量足够**，无需换更大的 BRAM。

> 待本地验证：需在 Python 2 + scipy 环境运行；生成后用 `test.py` 对一个 256-QAM 样本做交叉验证（前提是已有抓包样本）。

#### 4.3.5 小练习与答案

**Q1**：`lut_bita/lut_bitb` 是 3 位（0–7），为什么 256-QAM 不用加宽它，却必须加宽 RAM 的 DWIDTH？

**答**：3 位位选信号能索引 8 个比特，正好够 256-QAM 每载波 8 bit，所以位选不用加。但 RAM 每个地址存的是**整个载波的全部比特**，64-QAM 存 6 bit、256-QAM 要存 8 bit，所以 DWIDTH 必须从 6 加到 8。

**Q2**：为什么 256-QAM @ 20MHz 不用改 equalizer 的子载波掩码？

**答**：子载波结构由带宽决定，不由调制决定。20MHz 永远是 52 个活跃子载波（48 数据 + 4 导频），无论 BPSK 还是 256-QAM。换调制只改「每子载波几比特」，不改变载波数量与位置。

---

### 4.4 状态机校验放开、Python 参考同步与 40 MHz 带宽

#### 4.4.1 概念说明

即便改好了 demodulate 与 deinterleave，包仍然解不出来——因为 `S_CHECK_HT_SIG` 这道关卡会把 MCS 8/9 直接判死。同时，Python 参考解码器必须同步扩展，否则 `test.py` 的交叉验证会因「Python 不认识 MCS 8」而失败。本模块还顺带讲清 40 MHz 带宽扩展的真实代价——它远不止放开 `ht_cbw` 那一道门。

#### 4.4.2 核心流程

**MCS 8/9 解锁的三个动作**：

1. **放开状态机围栏**：把 `ht_mcs > 7` 改成允许 8、9（例如 `ht_mcs > 9`），其余 STBC/LDPC/SGI/多空间流围栏保持拒绝。
2. **Python 同步**：`HT_MCS_PARAMETERS` 加 MCS 8/9 条目，`Demodulator` 加 256-QAM 分支。
3. **码率/长度自洽**：确认 `num_bits_to_decode` 与 `pkt_len` 仍按字节长度驱动（见 4.4.3）。

**40 MHz 扩展的影响链**（远比换调制大）：

```
ht_cbw 围栏放开
   │
   ├── FFT 点数 64 → 128（xfft_v7_1 要换配置或新例化）
   ├── 子载波 52 → 108（掩码、导频、LTS_REF、HT_LTS_REF 全变）
   ├── equalizer 缓冲 AWIDTH 6 → 7（64 深 → 128 深）
   ├── deinterleave num_data_carrier 52 → 108、N_CBPS 重算、LUT 重生成
   ├── 采样率 20 → 40 MSPS  ★ 致命阻断点
   │     └── 100MHz/40MSPS = 2.5（非整数）→ 5:1 喂样节拍根本不成立
   └── 需要新抓包样本集（testing_inputs 没有 40MHz）
```

**关键结论**：换调制是「局部手术」，换带宽是「前端重构」。OpenOFDM 把 40MHz 挡在门外，不只是省事——100 MHz 工作时钟 ÷ 40 MSPS = 2.5 这个非整数，让 u1-l4 建立的「每 5 拍一个样本」节拍直接失效，这是比 FFT 点数更根本的阻断点。

#### 4.4.3 源码精读

**S_CHECK_HT_SIG 的 8 道围栏**——

[verilog/dot11.v:709-740](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L709-L740) 依次检查 MCS、CBW、rsvd、STBC、FEC、SGI、num_ext、tail。其中：

- [verilog/dot11.v:712](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L712) `if (ht_mcs > 7)` → `E_UNSUPPORTED_MCS`：这是挡住 MCS 8/9 的直接原因，要支持 256-QAM 必须放宽它。
- [verilog/dot11.v:715](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L715) `else if (ht_cbw)` → `E_UNSUPPORTED_CBW`：40MHz 的门。
- [verilog/dot11.v:721](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L721) `ht_stbc != 0`、[verilog/dot11.v:724](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L724) `ht_fec_coding`（LDPC）、[verilog/dot11.v:727](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L727) `short_gi`、[verilog/dot11.v:730](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L730) `ht_num_ext != 0`（多空间流）：这几道门对「单流 256-QAM」练习**必须继续拒绝**，否则会撞上更深的硬限制（单天线、无 STBC 解码器）。

错误码定义见 [verilog/common_params.v:57-65](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L57-L65)（`E_UNSUPPORTED_MCS=1` … `E_WRONG_CRC=9`）。

**长度与码率如何驱动解码（基本不用改）**——

[verilog/dot11.v:765-766](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L765-L766)：HT 数据段 `num_bits_to_decode <= (ht_len+3)<<4`，`pkt_rate <= {1'b1, ht_mcs}`。注意 `num_bits_to_decode` 只对非 DATA（SIGNAL/HT-SIG）驱动 flush，DATA 的硬停止靠 `byte_count >= pkt_len`（`pkt_len=ht_len`，按字节计）。由于它是**字节长度驱动**而非调制驱动，256-QAM 不必改这里——这是个好消息。

**HT-SIG 的 CRC-8 不用改**——

[verilog/dot11.v:680-707](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L680-L707) 的 `S_CHECK_HT_SIG_CRC` 固定用 `crc_count` 走到 35（`<24` 喂 ht_sig1、`<34` 喂 ht_sig2、`==34` 断流、`==35` 比对）。CRC-8 覆盖 HT-SIG 固定的 34 比特，MCS 只改变这 34 比特里的**数值**，不改变**长度**，所以多项式、初值、喂入拍数都不变——**校验逻辑与业务参数解耦**。

**软判决段与调制无关（佐证）**——

[verilog/ofdm_decoder.v:145-146](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L145-L146) 把 1 bit 硬判决映射成 `3'b111/3'b011` 软判决。无论上游是 64-QAM 还是 256-QAM，到这里都已是 1 bit，所以**ofdm_decoder 不用为 256-QAM 改一行**。

**Python 参考的两个扩展点**——

- [scripts/decode.py:106-115](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L106-L115) `HT_MCS_PARAMETERS` 只有 MCS 0–7，必须加 8/9。
- [scripts/decode.py:295-348](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L295-L348) `Demodulator` 的 `__init__` 用 if/elif 选星座，没有 256-QAM 分支——需加一段（`scale=15`、`bits_per_sym=8`、构造 256 个星座点），且其比特排列必须与 RTL `demodulate` 完全一致，否则 `test.py` 的 DEMOD 阶段比对会失败。

**40MHz 的 FFT 与节拍硬限制**——

[verilog/sync_long.v:185](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L185) 例化 `xfft_v7_1 dft_inst`，是 64 点 FFT（20MHz）；[verilog/sync_long.v:36](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L36) 的 `gi_skip = short_gi?9:17` 与 64 样本符号（[verilog/sync_long.v:239](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L239) `num_sample >= 64`）都写死了 64。40MHz 要换成 128 点 FFT、128 样本符号、新的 GI。更致命的是采样率：40MHz 信道需 40 MSPS，而 100MHz÷40MSPS=2.5 不是整数，u1-l4 建立的「每 5 拍一个样本」节拍根本不成立——要么把工作时钟提到 200MHz（÷40MSPS=5），要么改喂样逻辑。

[verilog/equalizer.v:31-53](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L31-L53) 的 `SUBCARRIER_MASK`/`HT_SUBCARRIER_MASK`/`PILOT_MASK`/`LTS_REF`/`HT_LTS_REF` 全是 64 位常量，每一位对应 64 点 FFT 的一个子载波；40MHz 要全部重定义为 128 位。[verilog/equalizer.v:138](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L138) 与 [verilog/equalizer.v:180](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L180) 的两个 `ram_2port #(.AWIDTH(6))`（64 深）也要扩到 `AWIDTH(7)`（128 深）。对照之下，`demodulate` 在 40MHz 下**一个字都不用改**——这就是两类扩展成本差异的根。

#### 4.4.4 代码实践（设计型）

**目标**：写一份「MCS 8/9 解锁」与「40MHz 评估」的影响清单。

**步骤**：

1. 打开 [verilog/dot11.v:709-740](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L709-L740)，标注哪几道围栏要放宽（MCS）、哪几道必须保留（CBW/STBC/FEC/SGI/num_ext）。
2. 列出 Python 同步项：`HT_MCS_PARAMETERS` 加 2 条、`Demodulator` 加 256-QAM 分支、比特序对齐。
3. 单独评估 40MHz：列出 FFT、掩码、缓冲、节拍、样本集五项改动，并标注「采样节拍 2.5 非整数」为阻断点。

**需要观察的现象**：你会发现「解锁 MCS 8/9」是一个**可控的局部改动**（4 个文件 + 1 张 LUT），而「40MHz」是一个**前端重构**（动 FFT IP、动时钟、动全部子载波常量）。

**预期结果**：两张清单——一张「256-QAM 改动表」（小），一张「40MHz 改动表」（大，且标注阻断点）。

> 待本地验证：40MHz 是否真能在 Spartan 3A-DSP 上跑下 128 点 FFT + 更高时钟，需综合后看资源/时序报告。

#### 4.4.5 小练习与答案

**Q1**：放开 `ht_mcs > 7` 后，为什么还要保留 `ht_stbc != 0`、`ht_num_ext != 0` 这两道门？

**答**：STBC 与多空间流（num_ext）都意味着**多天线**。OpenOFDM 只有单条接收链，物理上无法解 MIMO；即便放开围栏，后面也没有第二条 equalizer/demodulate 通路来处理第二个空间流。所以这两道门对单流扩展必须继续关闭。

**Q2**：`num_bits_to_decode = (ht_len+3)<<4` 为什么对 256-QAM 不用改？

**答**：它由 `ht_len`（HT-SIG 里的字节长度字段）驱动，与调制无关；且它只对不扰码的 SIGNAL/HT-SIG 驱动 flush。真正的 DATA 停止靠 `byte_count >= pkt_len`（字节计数），同样与调制无关。所以调制升级不影响长度计算。

---

## 5. 综合实践

**任务**：设计一个「支持 802.11n MCS 8/9（256-QAM，单流）」的改造方案。**只出设计文档与影响清单，不写实现代码。**

**输入假设**：沿用 HT-mixed 前导（L-SIG + HT-SIG + HT-LTS），20 MHz 带宽，MCS 8 = 256-QAM R=1/2，MCS 9 = 256-QAM R=3/4（理由见 4.3.4：\(N_\text{CBPS}=416\) 只有 1/2、3/4 能整除）。

**要求产出**：

1. **RTL 改动表**（文件 : 行号 : 改法）：
   - `verilog/demodulate.v`：`bits` 位宽 6→8（[L13](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v#L13)）；case 表补 MCS 8/9（[L72-80](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v#L72-L80)）；新增 `QAM_256` localparam 与 7 道门限（[L17-23](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v#L17-L23)）与新判决分支。
   - `verilog/deinterleave.v`：`ram_2port` DWIDTH 6→8（[L54](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L54)）；`in_bits`/`bit_outa`/`bit_outb` 加宽；`lut_key` 已自然支持（[L111](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L111)）。
   - `verilog/dot11.v`：放宽 [L712](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L712) 的 `ht_mcs > 7` 为 `ht_mcs > 9`；其余围栏保持。
   - `ofdm_decoder.v` / `equalizer.v` / `viterbi.v` / `crc32.v` / `ht_sig_crc.v`：**无需改动**，逐一说明理由（软判决段调制无关、均衡器调制无关、Viterbi/crc32 工作在比特/字节层、HT-SIG CRC 覆盖长度与 MCS 无关）。
2. **LUT 改动**：在 `gen_deinter_lut.py` 的 `RATES` 追加 MCS 8/9，选 1/2 与 3/4 穿孔模式，重生成 `deinter_lut.mif/.coe`，确认写入表头[24]/[25]、总深度 < 2048。
3. **状态机放开说明**：列出 `S_CHECK_HT_SIG`（[L709-740](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L709-L740)）中哪些围栏放开（MCS）、哪些保留（CBW/STBC/FEC/SGI/num_ext/tail）及保留原因。
4. **Python 同步**：`HT_MCS_PARAMETERS`（[decode.py:106-115](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L106-L115)）加 MCS 8/9；`Demodulator`（[decode.py:295-348](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L295-L348)）加 256-QAM 分支；强调比特排列必须与 RTL 对齐，否则 `test.py` 的 DEMOD 比对失败。
5. **风险与待验证项**：门限需用真实抓包样本校准（契约风险）；当前 `testing_inputs` 无 256-QAM 样本；若分辨率不足可考虑提 `CONS_SCALE_SHIFT`（牵动 equalizer/demodulate 全部门限）；并说明本方案与真实 802.11n/ac 标准的差异（256-QAM 实为 802.11ac 调制）。

**验收标准**：清单能自洽地解释「为什么 ofdm_decoder/viterbi/crc32/ht_sig_crc 不用改」，并能指出至少一个阻断点（无样本 / 门限待校准）。

## 6. 本讲小结

- 扩展速率的第一步是**画速率敏感点全图**：真正随调制变动的只有 demodulate、deinterleave、一张 LUT、状态机围栏、Python 参考五处；FFT/viterbi/crc32 都与调制无关。
- 256-QAM 让 `demodulate` 的输出位宽从 6 bit 涨到 8 bit，并需要 7 道判决门限 \( \text{MAX}\times 2(k+1)/15 \)；门限全部从 `MAX=1<<CONS_SCALE_SHIFT` 派生，刻度升级时自动缩放。
- 解交织的 RAM 位宽要跟着加宽到 8，但深度（64）与位选信号（3 位）都够用；`deinter_lut` 用 `{ht,rate[3:0]}` 索引，MCS 8/9 落在表头[24]/[25] 的空槽，ROM 深度 2048 足以容纳。
- `S_CHECK_HT_SIG` 是硬关卡：`ht_mcs>7` 必须放宽，但 STBC/LDPC/SGI/多空间流必须继续拒绝（单流硬件限制）；HT-SIG 的 CRC-8 覆盖固定 34 比特，与 MCS 无关，不用改。
- **换调制是局部手术，换带宽是前端重构**：40MHz 牵动 FFT 点数（64→128）、全部子载波掩码、缓冲深度，且采样率 100MHz÷40MSPS=2.5 非整数，直接打破 5:1 喂样节拍——这才是 40MHz 真正的阻断点。
- Python 参考必须与 RTL **同步**扩展（参数表 + Demodulator 分支 + 比特序对齐），否则 `test.py` 交叉验证会在 DEMOD 阶段失败。

## 7. 下一步学习建议

- 若想把本讲的 256-QAM 设计**落地验证**，先复习 u5-l2（`test.py` 交叉验证框架）与 u5-l4（LUT 生成脚本），把「重生成 LUT → 跑 Python 期望 → 跑仿真 → 逐阶段比对」的闭环走通。
- 若对 40MHz 仍感兴趣，下一步应读 u6-l3（Xilinx IP / coregen）与 `sync_long.v` 的 `xfft_v7_1` 例化，评估换 128 点 FFT 的资源代价，并重新审视 u1-l4 的采样节拍假设。
- 若想理解「为什么单流扩展不能碰 STBC/多空间流」，建议结合 u6-l2（模块复用与资源优化）思考：要支持 MIMO，需要把 equalizer/demodulate/viterbi 整条链复制 N 份，资源开销是质变。
- 继续沿着 u6 单元推进，下一篇 u6-l6 讲 `rand_gen.v` 随机激励自检——它能帮你在没有真实抓包样本时，为新增的 256-QAM 通路构造伪随机测试激励。
