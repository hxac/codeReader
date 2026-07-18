# 发射信号处理子模块（编码/交织/IFFT/调制/前导）

> 本讲承接 [u4-l1 openofdm_tx OFDM 发射机总览](u4-l1-openofdm-tx-overview.md)。u4-l1 讲清了 `openofdm_tx` 的顶层薄壳、`dot11_tx` 三条状态机（FSM1 取字节/编码、FSM2 信号处理、FSM3 转发样点）以及 BRAM/I/Q 的握手时序。本讲不再重复状态机骨架，而是**钻进 FSM1/FSM2 内部**，逐个拆解把「一个待发字节」变成「一对 I/Q 样点」所经过的信号处理子模块：加扰、CRC32、卷积编码、打孔交织、调制映射、IFFT、前导 ROM。

## 1. 本讲目标

读完本讲，你应当能够：

- 说清 802.11 a/g/n 发射基带的**处理顺序**：比特流 → 加扰 → 卷积编码 → 打孔 → 交织 → 调制 → 子载波映射（导频/DC/保护） → IFFT → 加循环前缀 → 拼前导。
- 在源码里定位每个处理阶段对应的**子模块实例**和**关键代码行**，并解释它的输入输出。
- 看懂 `convenc`（生成多项式 g0=133、g1=171）、`punc_interlv_lut`（打孔 + 交织查表合一）、`modulation`（BPSK/QPSK/16-QAM/64-QAM 星座查表）、`ifftmain`（64 点流水线 IFFT）、`crc32_tx`（CRC-32 查表）、四个前导 ROM 的实现思路。
- 用 `dot11_tx_tb.v` 这个自带 testbench 跑一次发射仿真，把待发字节到 I/Q 样点的完整路径「走一遍」。

## 2. 前置知识

本讲默认你已具备 u4-l1 的认知（PL/PS、`openofdm_tx` 在发射链路的位置、`phy_tx_start/started/done` 握手、BRAM 双口读取）。这里再补三个本讲会用到的「通信原理」直觉：

1. **OFDM 一句话直觉。** OFDM 把一串高速比特拆成很多路低速比特，分别调制到很多个**正交的子载波**上同时发送。openwifi 的 20MHz 信道用 **64 点 IFFT**，其中真正承载内容的是 **48 个数据子载波 + 4 个导频子载波**，中间（DC，0Hz）和两边（保护带）置零。每一个 OFDM 符号在时域上占 **64 个样点**，前面再贴一段**循环前缀（CP）**防多径。

2. **为什么要在调制前编码/交织。** 无线信道会突发性出错（一段连续比特都坏掉）。卷积编码加入冗余让接收端能纠错；**交织**把相邻比特打散到不同子载波、不同时间位置，让突发错误变成分散的零星错误，配合纠错码效果最好。**打孔（puncturing）** 则是在卷积码基础上**有规律地丢掉一部分校验位**，用降低一点纠错能力换取更高码率（更高吞吐）。

3. **定点复数 I/Q。** 源码里一个调制点用 32 bit 表示：高 16 bit 是实部 I、低 16 bit 是虚部 Q，都是**有符号定点数**。约定 `16'h4000` 表示 \( +1.0 \)，`16'hC000` 表示 \( -1.0 \)（即 Q3.13 左右的标度）。理解这一点，调制查表里那些 `0x4000 / 0xC000 / 0xD2BF` 才不会显得神秘。

**几个 802.11 术语速查**（源码里会反复出现）：

| 术语 | 含义 |
|------|------|
| `N_BPSC` | 每个子载波承载的**编码后**比特数（1=BPSK，2=QPSK，4=16-QAM，6=64-QAM） |
| `N_DBPS` | 每个 OFDM 符号承载的**数据**比特数（= 打孔后净比特数） |
| `RATE` | 5 bit 速率编码，同时决定了 `N_BPSC`、`N_DBPS` 和打孔图样 |
| PSDU | 物理层要发的真正数据（MAC 递交下来的帧体） |
| PLCP | 物理层汇聚协议头（L-SIG / HT-SIG），告诉接收端「按什么速率解后面的数据」 |
| CP | 循环前缀，OFDM 符号尾部的若干样点复制到头部 |

## 3. 本讲源码地图

本讲所有代码都在 `ip/openofdm_tx/src/` 下。`dot11_tx.v` 是「指挥官」，其余子模块都是被它例化的「执行者」：

| 文件 | 角色 |
|------|------|
| [dot11_tx.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v) | 总指挥：FSM1/FSM2/FSM3，例化并连线所有子模块，决定每拍喂给各子模块什么数据 |
| [convenc.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/convenc.v) | 卷积编码器（R=1/2，g0=133/g1=171） |
| [punc_interlv_lut.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/punc_interlv_lut.v) | 打孔 + 交织**联合查表**（按速率给出写地址和「该比特是否丢弃」） |
| [modulation.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/modulation.v) | 星座映射（BPSK/QPSK/16-QAM/64-QAM 查表） |
| [ifftmain.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/ifftmain.v) | 64 点流水线 IFFT（第三方 Gisselquist FFT） |
| [l_stf_rom.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/l_stf_rom.v) | Legacy 短训练序列 ROM（l_ltf/ht_stf/ht_ltf 同构） |
| [crc32_tx.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/crc32_tx.v) | CRC-32（FCS）查表实现 |
| [ram_simo.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/ram_simo.v) | 单写多读 RAM，用来存「一个交织好的 OFDM 符号的比特」 |
| [dot11_tx_tb.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx_tb.v) | 自带 testbench，用 `.mem` 文件喂 BRAM、把 I/Q 写到 `dot11_tx.txt` |

---

## 4. 核心概念与源码讲解

### 4.1 总链路：一个比特要走过的流水线

#### 4.1.1 概念说明

把发射基带看成一条**串行的比特流水线**。一拍一拍地，PSDU（真正的数据）的比特依次流过下面几个工位，每个工位都由一个子模块负责：

```
BRAM 字节
   │  (FSM1: 选源 + 加扰 + 算CRC32)
   ▼
bit_scram ──► convenc ──► bits_enc_fifo ──► (FSM2: 打孔+交织写入 bits_ram)
                                                  │
                                                  ▼
                                            modulation ──► 子载波映射(导频/DC/保护)
                                                                  │
                                                                  ▼
                                                             ifftmain(64点IFFT)
                                                                  │
                                                  ┌───────────────┴───────────────┐
                                                  ▼                                 ▼
                                            pkt_fifo                           CP_fifo
                                                  └───────────────┬───────────────┘
                                                                  ▼
                       (FSM3: 在前面拼上 l_stf/l_ltf/ht_stf/ht_ltf 前导)
                                                                  ▼
                                                         result_i / result_q
```

注意这条链路是**两条状态机异步解耦**的：FSM1（取字节+编码）和 FSM2（信号处理）之间用 `bits_enc_fifo` 解耦；FSM2 产出的时域样点经 `pkt_fifo`/`CP_fifo` 再交给 FSM3（转发）。所以 FSM1 可以一边读 BRAM 一边编码，FSM2 可以一边打孔交织一边做 IFFT，三者并行流动。这正是 u4-l1 讲的「前导来自 ROM，所以 `result_iq_valid` 几乎立刻有效」的实现基础——FSM3 不等 FSM1/FSM2，直接从 ROM 吐前导样点。

#### 4.1.2 核心流程

一拍之内，数据是这样流动的：

1. **FSM1 选源**：组合逻辑 [dot11_tx.v:L146-L179](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L146-L179) 决定当前要发的比特 `bit_scram` 来自哪里（L-SIG/HT-SIG 的 PLCP 头、PSDU 数据、PSDU 的 CRC、尾比特或填充比特），并即时异或上扰码。
2. **卷积编码**：`bit_scram` 喂给 `convenc`，每输入 1 bit 产出 2 bit（`bits_enc`）。
3. **入 FIFO**：2 bit 一组压入 `bits_enc_fifo`，等 FSM2 取走。
4. **FSM2 打孔+交织**：FSM2 从 FIFO 取 2 bit，按 `punc_interlv_lut` 给出的地址写入 `bits_ram`（顺便按打孔图样丢掉部分比特）。写满一个 OFDM 符号（`N_DBPS` 比特）后进入下一步。
5. **调制 + 子载波映射**：用 `modulation` 把交织后的比特组映射成 IQ，再把 48 个数据 IQ + 4 个导频 + DC/保护填进 64 点 IFFT 的输入 `ifft_iq`。
6. **IFFT**：`ifftmain` 把 64 个频域子载波变成 64 个时域样点。
7. **CP + 转发**：IFFT 输出按规则写入 `pkt_fifo`（主体）和 `CP_fifo`（循环前缀），由 FSM3 依次读出，前置前导后送给 DAC。

下面按最小模块逐一精读。

---

### 4.2 加扰与 CRC32（最小模块：CRC32）

#### 4.2.1 概念说明

**加扰（scrambling）**：把数据与一个**伪随机序列**异或，让发出去的比特足够「乱」，避免出现长串 0 或长串 1（否则功率谱会有直流尖峰、接收端难同步、自适应增益失灵）。802.11 用一个 7 级 LFSR（反馈多项式 \( x^7 + x^4 + 1 \)），扰码器的初值（`init_data_scram_state`）每个包由上层给定，收发双方用同一初值即可解扰。

**CRC32 / FCS**：在 PSDU 数据后面附上 32 bit 帧校验序列，让接收端判断这一帧有没有传错。注意 802.11 的 CRC 是对 **PSDU（纯数据）** 算的，且**先于加扰**（即对未加扰的数据算 CRC，再把数据和 CRC 一起加扰）。源码里 `crc32_tx` 用的是 4 bit 一拍、查 16 项表的「半字节（nibble）查表法」。

#### 4.2.2 核心流程

- **扰码器**是纯组合 + 一个在 FSM1 里推进的 LFSR 寄存器 `data_scram_state`。每来一拍，输出比特 = 源比特 XOR `data_scram_state[6] ^ data_scram_state[3]`，下一拍 LFSR 左移、最低位填入新的反馈。PLCP 头字段**不加扰**（直接取 BRAM 比特），只有 DATA 字段（service/PSDU/CRC/tail/pad）加扰。
- **CRC32** 在 PSDU 数据阶段，每凑齐 4 bit（一个 nibble）就让 `crc_en` 拉高一拍，把 32 bit 余式推进一步。等到 PSDU 数据发完，寄存器里的值就是 FCS，随后在 `S11_PSDU_CRC` 子状态把它逐位发出。

#### 4.2.3 源码精读

**扰码比特选择**——这段组合逻辑是「当前要发的源比特 + 是否加扰」的总开关：

[ip/openofdm_tx/src/dot11_tx.v:L146-L179](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L146-L179)

要点：
- `S1_L_SIG`/`S1_HT_SIG` 阶段 `bit_scram = bram_din[plcp_bit_cnt]`——**PLCP 头不加扰**，直接从 BRAM 取。
- PSDU 数据阶段 `bit_scram = data_scram_state[6] ^ data_scram_state[3] ^ bram_din[...]`——这就是扰码：源比特 XOR 伪随机比特。
- PSDU 的 CRC 阶段异或的是 `pkt_fcs[pkt_fcs_idx]`——把算好的 FCS 当作「也要被加扰的数据」继续送出去。

**LFSR 推进**——`data_scram_state` 在 FSM1 的每个 `S1_DATA` 拍左移并计算新反馈位：

[ip/openofdm_tx/src/dot11_tx.v:L312-L312](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L312)

这正好实现反馈多项式 \( x^7+x^4+1 \)：新比特 = `state[6] ^ state[3]`，塞进移位寄存器最低位。

**CRC32 模块本体**——半字节查表法，`idx = crc_out[3:0] ^ data_in` 用当前余式低 4 位与输入 nibble 异或得到表索引，查 16 项表后与「右移 4 位后的余式」异或，得到新余式：

[ip/openofdm_tx/src/crc32_tx.v:L19-L46](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/crc32_tx.v#L19-L46)

\[ c_{\text{new}} = (\,c_{\text{old}} \gg 4\,) \oplus \mathrm{table}\big[\,(c_{\text{old}} \,\&\, \mathtt{0xF}) \oplus d_{\text{in}}\,\big] \]

每来一个 nibble 执行一次上式，PSDU 全部 nibble 处理完后 `crc_out` 即为 FCS。

**CRC 的输入 nibble 提取**——`crc_data[3:0]` 从 BRAM 的 64 bit 字里按 `psdu_bit_cnt` 取出当前的 4 bit：

[ip/openofdm_tx/src/dot11_tx.v:L128-L131](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L128-L131)

（当 64 bit 字的最后一个 nibble 取完，会改用 `bram_din_last_nibble` 缓存的值，避免读到下一字。）

**CRC 使能**——每 4 个数据比特（`psdu_bit_cnt[1:0]==2'b11`）才推进一次：

[ip/openofdm_tx/src/dot11_tx.v:L139-L139](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L139)

#### 4.2.4 代码实践

**目标**：观察「PLCP 头不加扰、PSDU 数据加扰、CRC 也加扰」这条规则在源码里的体现。

**步骤**：
1. 打开 `dot11_tx.v`，定位 4.2.3 引用的 `bit_scram` 组合块（L146-L179）。
2. 列一张表：`state1` / `state11` 各取值时，`bit_scram` 的来源是「BRAM 直取」「扰码 XOR BRAM」「扰码 XOR FCS」「0」中的哪一个。
3. 再看 L312 的 LFSR 推进只在 `S1_DATA` 子状态发生——确认 PLCP 头阶段 `data_scram_state` 不变（因为头不加扰，扰码器不需要转）。

**预期结果**：你会得到「L-SIG/HT-SIG 阶段扰码器冻结、DATA 阶段才每个数据比特滚动一次」的结论，这正是 802.11 的规定。

**待本地验证**：扰码器初值 `init_data_scram_state` 由 `openofdm_tx` 顶层传入，可结合 `dot11_tx_tb.v`（固定为 `7'b1111111`）观察波形里 `data_scram_state` 的滚动是否符合 \( x^7+x^4+1 \)。

#### 4.2.5 小练习与答案

**练习 1**：为什么 L-SIG/HT-SIG 头不加扰，而 PSDU 必须加扰？
> 参考答案：PLCP 头要被接收端在「还不知道扰码初值」时先解出来（它携带速率等信息），所以必须明文发送；PSDU 已有约定的扰码初值（service 字段里隐含），收端解出头后即可用同一初值解扰，因此可以也应当加扰以平滑功率谱。

**练习 2**：`crc_en` 为什么用 `psdu_bit_cnt[1:0]==2'b11` 作条件？
> 参考答案：CRC32 这里按 4 bit（nibble）一拍推进。`psdu_bit_cnt` 最低 2 位每 4 个比特循环到 `11` 一次，正好对齐一个 nibble 的边界，保证每凑齐 4 bit 才查一次表。

---

### 4.3 卷积编码与打孔交织（最小模块：卷积编码与打孔交织）

#### 4.3.1 概念说明

**卷积编码（convenc）**：把每一个输入比特，结合当前编码器状态，输出多个校验比特。openwifi 用经典的 802.11 编码：码率 \( R=1/2 \)，两个生成多项式（八进制）\( g_0=133 \)、\( g_1=171 \)，约束长度 7（6 级移位寄存器）。每输入 1 bit 输出 2 bit，所以基带码率 1/2。

**打孔（puncturing）**：1/2 码对高速率（9/18/36/54/48 Mbps）「太冗余」。打孔按一个固定图样**周期性地丢掉一部分编码输出位**，让等效码率升到 3/4 或 2/3。接收端 Viterbi 译码时把这些位置当作「擦除（erasure）」处理。

**交织（interleaving）**：把一个 OFDM 符号内的 `N_CBPS` 个编码后比特按一个置换规则重排，使相邻比特分散到相距较远的子载波上。

openwifi 的关键设计是：**把「打孔」和「交织」合并成一张查表**。`punc_interlv_lut` 给出两样东西——

- `idx_o[17:0]`：高 9 位和低 9 位是两个候选的**交织写入地址**；
- `punc_o[1:0]`：两个**打孔使能**位，指示对应的编码比特是否被丢弃。

FSM2 据此决定「这一对编码比特里，留哪个、写到哪个交织地址」。

#### 4.3.2 核心流程

1. `convenc` 把 `bit_scram` 编成 2 bit `bits_enc`，压入 `bits_enc_fifo`（2 bit 宽，解耦 FSM1/FSM2）。
2. FSM2 在 `S2_PUNC_INTERLV` 状态，向 `punc_interlv_lut` 输入当前速率 `rate` 和符号内序号 `dbps_cnt_FSM2`，得到 `interlv_addrs`（交织地址）和 `punc_info`（打孔信息）。
3. 根据 `enc_pos`（编码位 0/1）和 `punc_info`，选择把 `bits_enc_fifo_odata[0]` 或 `[1]` 写入 `bits_ram` 的某个地址；被标记为打孔的比特直接不写（丢弃）。
4. 当一个 OFDM 符号的 `N_DBPS` 个比特写满，进入调制阶段。

**码率与打孔的对应关系**（从速率表与 LUT 推得）：

| 速率 | 调制 | `N_BPSC` | `N_DBPS` | 码率 | 打孔 |
|------|------|----------|----------|------|------|
| 6 / 12 / 24 Mbps | BPSK/QPSK/16-QAM | 1/2/4 | 24/48/96 | 1/2 | 无（`punc_o=00`） |
| 9 / 18 / 36 / 54 Mbps | BPSK/QPSK/16-QAM/64-QAM | 1/2/4/6 | 36/72/144/216 | 3/4 | 有（周期性丢 1/3） |
| 48 Mbps | 64-QAM | 6 | 192 | 2/3 | 有 |

吞吐验证（20MHz，OFDM 符号 4µs）：\( \text{速率} = N_{\text{DBPS}} \times 250\,\text{kbit/s} \)，例如 `N_DBPS=48` → 12 Mbps ✓。

#### 4.3.3 源码精读

**卷积编码器本体**——6 级移位寄存器 + 两个模 2 加（异或）实现 g0=133、g1=171：

[ip/openofdm_tx/src/convenc.v:L17-L25](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/convenc.v#L17-L25)

```verilog
state <= {state[4:0], bit_in};                                  // 移位寄存器
assign bits_out[0] = bit_in ^ state[0]^state[1]^state[2]^state[5]; // g0=133
assign bits_out[1] = bit_in ^ state[1]^state[2]^state[4]^state[5]; // g1=171
```

`bits_out[0]` 对应抽头 133（二进制 `1011011`，含 bit0/1/2/5），`bits_out[1]` 对应 171（`1111001`，含 bit1/2/4/5）。每拍 1 bit 进、2 bit 出。

**convenc 在 dot11_tx 里的例化与复位**：

[ip/openofdm_tx/src/dot11_tx.v:L187-L195](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L187-L195)

注意 `enc_reset` 在每个 PLCP 头结束（`plcp_bit_cnt==23/47`）时把编码器清零——因为 L-SIG、HT-SIG 各自独立编码，且尾部 6 个 0 用于把卷积码「冲回零状态」方便接收端译码。

**解耦 FIFO**——存编码后的 2 bit，`SIZE(10)` 即 1024 深：

[ip/openofdm_tx/src/dot11_tx.v:L385-L393](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L385-L393)

**打孔+交织查表的例化**——按速率在「符号 0 用 6Mbps 的 BPSK 头速率、其余符号用真实速率」间切换：

[ip/openofdm_tx/src/dot11_tx.v:L403-L408](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L403-L408)

`rate` 端口的那个三元表达式说明：L-SIG/HT-SIG 符号（`ofdm_cnt_FSM2<=0` 或 `<=2`）强制用 6Mbps 的 BPSK 规则，之后才用真实 `RATE`。

**LUT 输出含义**——以 6Mbps（`punc_o` 全 0，无打孔）为例：

[ip/openofdm_tx/src/punc_interlv_lut.v:L19-L29](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/punc_interlv_lut.v#L19-L29)

6Mbps 时每个 `idx_i` 给出 `{addr_A, addr_B}`（两个交织地址，高 9 位/低 9 位），`punc_o=00` 表示两位编码比特都保留。再看 9Mbps（`rate==5'b01111`）：

[ip/openofdm_tx/src/punc_interlv_lut.v:L49-L56](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/punc_interlv_lut.v#L49-L56)

这里出现 `idx_o={9'd511,9'd48}; punc_o={1'b1,1'b0}`——`511` 是无效地址（`bits_ram` 深度 52×8=416，511 超界即「这一位丢弃」），`punc_o[1]=1` 表示该编码比特被打孔。这就是 3/4 码率打孔图样的查表实现。

**用查表结果写 bits_ram**——`bits_ram_waddr` 与 `punc_bit` 由 `enc_pos` 和 `punc_info` 共同决定：

[ip/openofdm_tx/src/dot11_tx.v:L426-L428](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L426-L428)

规则是：`enc_pos` 在未被丢弃的位上翻转（`(|punc_info)==0` 时 `enc_pos<=~enc_pos`），从而把 1/2 码的两个输出交替分配到地址 A/B；被标记打孔的位则跳过。`bits_ram`（`ram_simo`，`DEPTH(52)`）就是「一个交织好的 OFDM 符号的比特仓库」，调制阶段一次读出 6 bit。

#### 4.3.4 代码实践

**目标**：对照「6Mbps 无打孔、9Mbps 有打孔」两张 LUT，亲手验证打孔地址 `511` 的含义。

**步骤**：
1. 打开 `punc_interlv_lut.v`，分别看 `rate==5'b01011`（6Mbps）和 `rate==5'b01111`（9Mbps）两段 `case`。
2. 数一数 6Mbps 一个符号（24 个 `idx_i`）里 `idx_o` 出现了多少个有效地址；9Mbps（36 个 `idx_i`）里有多少行含 `9'd511`。
3. 在 `dot11_tx.v:L426` 附近确认：当 `interlv_addrs` 的高/低 9 位是 `511` 时，`bits_ram_waddr` 会被选成另一个有效地址（`punc_info[0]` 控制），从而**不把打孔位写进 RAM**。

**预期结果**：6Mbps 24 拍全部写入（无 511）；9Mbps 出现 511 的拍对应的编码比特被丢弃，等效码率从 1/2 升到 3/4。

**待本地验证**：在 XSim 里跑 `dot11_tx_tb.v`，给 BRAM 喂 `ht_tx_intf_mem_mcs7_gi1_aggr0_byte100.mem`（MCS7=64-QAM、3/4 码率），抓 `bits_ram` 的写地址与写使能，统计实际写入比特数是否等于 `N_DBPS`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 9/18/36/54 Mbps 的 LUT 里 `punc_o` 经常出现 `{1'b1,1'b0}`，而 6/12/24 Mbps 里 `punc_o` 全是 `00`？
> 参考答案：前者是 3/4 码率，需要打孔（周期性丢 1/3 编码位），所以 `punc_o` 非零；后者是 1/2 码率，不打孔，两位编码比特都保留，故 `punc_o=00`。

**练习 2**：`enc_pos` 这个 1 bit 寄存器的作用是什么？
> 参考答案：卷积编码每拍输出 2 bit（`bits_out[0]` 和 `[1]`），但 FIFO 是 2 bit 一起出的。`enc_pos` 用来交替标记「当前处理的是这对编码比特的第 0 位还是第 1 位」，配合交织地址 A/B 把两位分别写到不同位置。

**练习 3**：`bits_ram` 用 `ram_simo`（单写多读），「多读」体现在哪里？
> 参考答案：调制阶段一次需要 `N_BPSC`（1/2/4/6）个比特，`ram_simo` 的 `data_o[5:0]` 一次性把同一地址周围连续的 6 个 bit 全部读出（[ram_simo.v:L30-L35](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/ram_simo.v#L30-L35)），供 `modulation` 直接取所需位宽，省去多次读 RAM。

---

### 4.4 调制映射与 IFFT（最小模块：调制与 IFFT）

#### 4.4.1 概念说明

**调制映射（modulation）**：把交织后的 `N_BPSC` 个比特映射成 1 个复数星座点（I, Q）。openwifi 支持 BPSK（1 bit/子载波）、QPSK（2）、16-QAM（4）、64-QAM（6）。实现方式是最直接的**查表**：用 `N_BPSC` 选定星座，用输入比特作索引，查出对应的 `{I[15:0], Q[15:0]}`。星座点幅度按归一化系数缩放，使不同调制下平均功率一致。

**子载波映射**：64 点 IFFT 的输入有 64 个复数槽位，但只有 48 个放数据、4 个放导频、DC 与保护带置零。需要把调制输出的数据点按规则填进对应槽位，并在 4 个固定位置（子载波 -21/-7/7/21，对应 `iq_cnt` 7/21/43/57）插入导频。

**IFFT**：把「频域 64 个子载波」变成「时域 64 个样点」。openwifi 用的是第三方 [Gisselquist pipelined FFT](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/ifftmain.v)（LGPL），64 点、全流水线、每拍吃一个复数、每拍吐一个复数，内部 6 级 radix-2 + bit-reverse。

#### 4.4.2 核心流程

1. FSM2 在 `S2_MOD_IFFT_INPUT` 状态，按 `iq_cnt`（0~63）逐拍生成 64 个 IFFT 输入：先填 DC/保护带（0），再在导频位置填 `pilot_iq`，其余位置用 `mod_addr` 从 `bits_ram` 读出比特、经 `modulation` 得到 `mod_IQ`。
2. `ifft_ce` 在这 64 拍里拉高，让 `ifftmain` 流水线吃满；之后关闭，等下一个符号。
3. IFFT 输出 `ifft_o_result`（时域样点）按规则分别写进 `pkt_fifo`（符号主体 64/56 样点）和 `CP_fifo`（循环前缀 16/8 样点）。
4. 导频极性由扰码器（`pilot_gain`）和 HT 极性序列 `ht_polarity` 共同决定，逐符号翻转。

#### 4.4.3 源码精读

**modulation 模块本体**——纯组合查表，以 BPSK 为例：

[ip/openofdm_tx/src/modulation.v:L17-L25](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/modulation.v#L17-L25)

```verilog
if(N_BPSC == 1) begin            // BPSK
  case(bits_in[0])
    1'b0: IQ = {16'hC000, 16'h0000};  // I=-1, Q=0
    1'b1: IQ = {16'h4000, 16'h0000};  // I=+1, Q=0
  endcase
```

QPSK（`N_BPSC==2`，[modulation.v:L28-L35](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/modulation.v#L28-L35)）用 `0x2D41`(≈+0.35)/`0xD2BF`(≈-0.35)，即 ±1/√2 的定点值。16-QAM/64-QAM 同理只是表更大（[modulation.v:L38-L59](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/modulation.v#L38-L59) 与 L62-L135）。

**modulation 的例化**——同样在「符号 0 强制 BPSK」的速率切换：

[ip/openofdm_tx/src/dot11_tx.v:L435-L439](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L435-L439)

**子载波映射（DC/保护/导频/数据）**——这是把调制点装进 64 点 IFFT 的核心组合逻辑：

[ip/openofdm_tx/src/dot11_tx.v:L455-L475](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L455-L475)

逐条解读：
- `iq_cnt==0`（DC，0Hz）以及保护带（legacy/前几个 HT 符号 `iq_cnt 27~37`；HT 数据符号 `29~35/36`）→ 填 `DC_SB_IQ = 0`。
- `iq_cnt==7/21/43/57` → 填 4 个导频 `pilot_iq[2/3/0/1]`。
- 其余 `iq_cnt` → 填数据 `mod_IQ`（HT 的训练符号 ofdm_cnt 1/2 要把 I/Q 互换，对应 [L469-L470](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L469-L470)）。

**导频极性**——由扰码器输出 `pilot_gain` 与（HT 时）`ht_polarity` 异或决定是 `+0x4000` 还是 `-0x4000`（`0xC000`）：

[ip/openofdm_tx/src/dot11_tx.v:L526-L562](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L526-L562)

**IFFT 例化**——64 点流水线 IFFT：

[ip/openofdm_tx/src/dot11_tx.v:L480-L486](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L480-L486)

`i_ce` 即 `ifft_ce`（每符号 64 拍高），`o_sync` 在每帧第一个有效输出时拉高（用来对齐 FIFO 写入），`o_result` 即时域样点。

**IFFT 内部结构**——6 级 `fftstage`/`qtrstage`/`laststage` + `bitreverse`：

[ip/openofdm_tx/src/ifftmain.v:L99-L156](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/ifftmain.v#L99-L156)

文件头注释（[ifftmain.v:L34-L44](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/ifftmain.v#L34-L44)）说明它是用 `./fftgen -f 64 ...` 生成的固定 64 点核，4 个 stage 用 DSP 乘法器。

#### 4.4.4 代码实践

**目标**：在源码里「数」出一个 OFDM 符号的 64 个 IFFT 输入槽位分配。

**步骤**：
1. 打开 [dot11_tx.v:L455-L475](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L455-L475)。
2. 用一张 8×8 表（`iq_cnt` 0~63）标注每个槽位：DC、保护带（0）、导频（P）、数据（D）。
3. 数「数据」槽位是否恰好 48 个，「导频」是否 4 个（`iq_cnt` 7/21/43/57）。

**预期结果**：48 数据 + 4 导频 + 12 个 0（DC+保护）= 64，与 802.11 20MHz 信道一致。

**待本地验证**：在 testbench 波形里抓 `iq_cnt` 与 `ifft_iq[31:16]`（I 路），确认 `iq_cnt` 命中保护带时 I=0、命中导频时 I=±0x4000。

#### 4.4.5 小练习与答案

**练习 1**：QPSK 的星座点为何用 `0x2D41` 和 `0xD2BF` 而不是 `0x4000`/`0xC000`？
> 参考答案：QPSK 四个点在单位圆上 ±45°，坐标是 \( \pm 1/\sqrt{2} \)。\( 1/\sqrt{2} \approx 0.707 \)，定点 \( 0.707 \times 2^{13} \approx 0x2D41 \)。归一化后各调制平均功率一致。

**练习 2**：为什么 IFFT 输入要按「子载波序号 → `iq_cnt`」做一次地址映射（[L569-L597](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L569-L597) 的 `mod_addr` 计算），而不是直接 `mod_addr=iq_cnt`？
> 参考答案：IFFT 槽位 `iq_cnt` 是按频率从低到高排列的物理位置，而 802.11 的数据子载波编号是 -26~-1、+1~+26，需要映射到 IFFT 的 0~63（负频率放在高频段）。`mod_addr` 的那些分段计算就是做这个「频率到 IFFT 槽位」的重排。

**练习 3**：`ifft_ce` 在一个符号内高电平多少拍？为什么是这个数？
> 参考答案：64 拍。一个 OFDM 符号 64 个子载波，IFFT 每拍吃一个复数输入，所以喂满一帧要 64 拍（见 [L569-L605](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L569-L605) `iq_cnt<63` 的计数）。

---

### 4.5 前导与训练序列 ROM（最小模块：前导 ROM）

#### 4.5.1 概念说明

Wi-Fi 帧不是一上来就发数据，而是先发一段接收端「认识」的**前导（preamble）/训练序列**，让接收端完成 AGC、包检测、频偏估计、信道估计、符号定时。802.11 a/g 用 Legacy 前导（L-STF + L-LTF + L-SIG）；802.11 n 在前面再加 HT 前导（HT-SIG + HT-STF + HT-LTF）。

这些前导波形是**标准规定好的固定样点**，发射端不需要现算，直接存 ROM、按地址读出即可。openwifi 用 4 个 ROM：`l_stf_rom`、`l_ltf_rom`（Legacy）、`ht_stf_rom`、`ht_ltf_rom`（HT）。

#### 4.5.2 核心流程

- FSM3（转发状态机）在 `phy_tx_start` 后先进入 `S3_L_STF`，从 `l_stf_rom` 读 160 个样点（10 个短训练 × 16 样点），并在第一个样点拉高 `phy_tx_started`（通知上层「真开始发了」，u4-l1 已讲）。
- 依次输出 L-LTF（160 样点）、L-SIG（80 样点）、可选 HT-SIG/HT-STF/HT-LTF。
- 然后进入 `S3_DATA`，从 `pkt_fifo`/`CP_fifo` 读 IFFT 产出的数据样点。
- 前导阶段的 `result_i/result_q` 直接来自 ROM（不走 FIFO），数据阶段才来自 FIFO。

#### 4.5.3 源码精读

**四个前导 ROM 的例化**：

[ip/openofdm_tx/src/dot11_tx.v:L84-L110](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L84-L110)

注意地址宽度不同：`l_stf`/`ht_stf` 是 4 bit（16 项，短训练重复），`l_ltf` 是 8 bit，`ht_ltf` 是 7 bit——对应各自训练序列长度。

**L-STF ROM 内容**——16 项，每项 32 bit（高 16 bit=I，低 16 bit=Q），是标准规定的短训练序列样点：

[ip/openofdm_tx/src/l_stf_rom.v:L15-L35](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/l_stf_rom.v#L15-L35)

L-STF 共 160 个样点 = 10 次重复的 16 样点序列，所以 ROM 只存 16 项、地址用低 4 位循环。

**FSM3 用 preamble_addr 推进前导**——以 L-STF 为例：

[ip/openofdm_tx/src/dot11_tx.v:L753-L767](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L753-L767)

`preamble_addr` 从 0 数到 159（L-STF 长度），`phy_tx_started` 在 `preamble_addr==0` 时脉冲一拍。

**最终 I/Q 多路选择**——前导阶段选 ROM，数据阶段选 FIFO：

[ip/openofdm_tx/src/dot11_tx.v:L826-L828](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L826-L828)

这行长长的三元表达式就是：`state3` 在 L-STF/L-LTF/HT-STF/HT-LTF 时分别取对应 ROM 的高/低 16 位；否则按 `fifo_turn` 取 `pkt_fifo` 或 `CP_fifo`。

#### 4.5.4 代码实践

**目标**：确认前导样点数与标准一致，并理解「前导不经 FIFO、直读 ROM」的设计。

**步骤**：
1. 读 [dot11_tx.v:L745-L823](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L745-L823) 的 FSM3，记录每个 `S3_*` 状态发出的样点数（看 `preamble_addr` 的边界与各 `pkt_iq_sent/CP_iq_sent` 比较）。
2. 对照标准：L-STF 160、L-LTF 160、L-SIG 80（含 CP）、HT-SIG 160、HT-STF 80、HT-LTF 80。
3. 解释为什么前导用 ROM 而不进 `pkt_fifo`。

**预期结果**：FSM3 各状态样点数与标准吻合；前导用 ROM 是因为这些样点固定且需在 FSM1/FSM2 还没产出数据时就开始发，走 FIFO 会有空窗。

**待本地验证**：跑 testbench，在 `dot11_tx.txt` 输出文件开头，前 160 行 I/Q 应是 L-STF 的重复模式（每 16 行一组）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `l_stf_rom` 只存 16 项，却能发出 160 个样点？
> 参考答案：L-STF 是 16 样点短训练序列重复 10 次。地址用 `preamble_addr[3:0]`（4 bit，模 16）即可循环读出，所以 ROM 只需存一个周期 16 项。

**练习 2**：`phy_tx_started` 为什么在 L-STF 第一个样点（`preamble_addr==0`）才拉高，而不是 `phy_tx_start` 一来就高？
> 参考答案：`phy_tx_start` 是上层「请求发送」的脉冲，而 `phy_tx_started` 表示「第一个空口样点真正发出」。上层 MAC（xpu）用它做 CSMA/CA 的时序基准（比如 SIFS 后发 ACK 的计时起点），必须对齐到实际波形起始。

---

## 5. 综合实践：跑一次 `dot11_tx_tb`，画出完整数据路径

**目标**：把本讲五个最小模块（加扰/CRC、卷积编码/打孔交织、调制/IFFT、前导 ROM）串起来，用自带的 `dot11_tx_tb.v` 跑一次发射，验证「字节 → I/Q 样点」的完整链路。

**步骤**：

1. **建仿真工程**。在 `ip/openofdm_tx` 下用 `create_vivado_proj.sh`（参见 u7-l3 单 IP 仿真讲义）创建一个以 `dot11_tx_tb.v` 为顶层的行为级仿真工程，注意把 `src/` 下所有 `.v`（尤其是 `ifftmain`、`fftstage`、`bitreverse`、`ram_simo`、`axi_fifo_bram`、各 ROM、`convenc`、`punc_interlv_lut`、`modulation`、`crc32_tx`）都加进去。

2. **准备测试向量**。testbench 里默认读取：

   [ip/openofdm_tx/src/dot11_tx_tb.v:L28-L28](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx_tb.v#L28)

   即 `unit_test/test_vec/ht_tx_intf_mem_mcs7_gi1_aggr0_byte8176.mem`（HT、MCS7=64-QAM、3/4 码率、短 GI、8176 字节聚合帧）。也可换 L26/L27 注释行里另两个向量（`tx_intf.mem` legacy、`..._byte100.mem` 短包）。

3. **跑仿真**。testbench 的 200MHz 时钟、复位、`phy_tx_start` 脉冲都已写好：

   [ip/openofdm_tx/src/dot11_tx_tb.v:L32-L43](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx_tb.v#L32-L43)

   它在 `phy_tx_done` 拉高时 `$finish`，并把每个有效样点写成 `%d %d`（I, Q）到 `dot11_tx.txt`：

   [ip/openofdm_tx/src/dot11_tx_tb.v:L49-L56](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx_tb.v#L49-L56)

4. **追踪一条完整路径**（本讲的核心练习）。在波形里按顺序抓以下信号，填一张「处理阶段 → 信号 → 观察点」表：

   | 阶段 | 信号 | 观察点 |
   |------|------|--------|
   | BRAM 取字节 | `bram_addr`, `bram_din` | 地址递增、读出首字参数表（速率/长度） |
   | 加扰+编码 | `bit_scram`, `data_scram_state`, `bits_enc` | L-SIG 期间扰码器冻结、DATA 期间滚动 |
   | FIFO/打孔/交织 | `bits_enc_fifo_*`, `punc_info`, `bits_ram_waddr` | MCS7 出现 `511` 打孔地址 |
   | 调制 | `mod_addr`, `mod_IQ` | 64-QAM 六比特映射 |
   | 子载波映射 | `iq_cnt`, `ifft_iq` | DC/保护=0、导频位置 ±0x4000 |
   | IFFT | `ifft_ce`, `ifft_o_result`, `ifft_o_sync` | 每符号 64 拍、`o_sync` 对齐 |
   | 前导转发 | `state3`, `preamble_addr`, `result_i/q` | 先 L-STF/L-LTF（ROM），后 DATA（FIFO） |

5. **核对样点总数**。综合 L635/L641 的注释：`nof_iq2send` 最终约 \( 480 + 20169\times80 = 1614000 \) 个 IQ 样点（最大包），用 `dot11_tx.txt` 的行数验证。

**预期结果**：你应当能看到一条从 BRAM 字节，经加扰/编码/打孔/交织/调制/IFFT，到最终 I/Q 样点的、与第 4.1 节链路图完全一致的波形证据链。

**待本地验证**：本实践需要 Vivado 2022.2 + XSim。若仅做源码阅读，也可不跑仿真，只按上表在源码里逐段定位信号并填写「观察点」列，完成静态走查。

---

## 6. 本讲小结

- 发射基带是一条**串行比特流水线**：加扰 → 卷积编码 → 打孔 → 交织 → 调制 → 子载波映射（导频/DC/保护） → IFFT → 加 CP → 拼前导。`dot11_tx.v` 是指挥官，其余子模块都是被例化的执行者。
- **加扰**用 7 级 LFSR（\( x^7+x^4+1 \)），PLCP 头不加扰、PSDU 及其 CRC 加扰；**CRC32** 用半字节查表法，每 4 bit 推进一步。
- **卷积编码** R=1/2（g0=133/g1=171）；**打孔 + 交织被合并成一张 LUT**（`punc_interlv_lut`），靠 `punc_o` 标记丢弃位、`idx_o` 给交织地址，用地址 `511` 表示「打孔不写入」。
- **调制**是纯查表（BPSK/QPSK/16-QAM/64-QAM），星座点用 `0x4000=+1` 标度的定点值；**子载波映射**把 48 数据 + 4 导频（`iq_cnt` 7/21/43/57）+ DC/保护填进 64 点 IFFT。
- **IFFT** 是第三方 64 点流水线核（`ifftmain`，6 级 + bit-reverse），每符号喂 64 拍。
- **前导**（L-STF/L-LTF/HT-STF/HT-LTF）是固定样点 ROM，FSM3 直接读出，**不经过数据 FIFO**，从而在前几个符号 FSM1/FSM2 还在产出时就能先发前导。

## 7. 下一步学习建议

- 本讲只到 `dot11_tx` 输出 I/Q。这些 I/Q 如何被 `tx_intf` 经 `tx_iq_intf`/`dac_intf` 送到 AD9361 DAC，请看 [u4-l3 tx_intf 发射接口与 BRAM 缓存](u4-l3-tx-intf.md)。
- 卷积编码对应的**接收端 Viterbi 译码**在 `openofdm_rx` 子模块里（[u3-l3](u3-l3-openofdm-rx.md)），可对照理解编/解码的对称性。
- 想亲手改一个调制点或打孔图样做实验，先读 [u7-l3 IP 仿真与 testbench 实践](u7-l3-ip-simulation-testbench.md) 学会建单 IP 仿真工程。
- 速率/调制/码率的「真相源」是 `dot11_tx.v` 的 RATE 表（[L240-L308](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L240-L308)），建议把它和 802.11 标准的 Table 17-3/18-15 对照阅读，加深对 `N_BPSC`/`N_DBPS` 的理解。
