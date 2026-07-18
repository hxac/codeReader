# ofdm_decoder 子流水线与卷积解码

## 1. 本讲目标

上一讲（u3-l4）我们看清了 `deinterleave` 如何把一个 OFDM 符号里被打乱的比特按原始顺序重排出来，并在非 1/2 码率的位置插入「哑比特」并打上 `erase` 标记。这些每拍 2 比特的输出，本讲要被送进**卷积译码器（Viterbi）**还原成原始数据比特，再经解扰、拼字节，最终变成 802.11 帧的字节流。

本讲聚焦在把这些独立模块「串成一条流水线」的 [`ofdm_decoder.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v) 上。读完本讲你应该能够：

1. 画出 `ofdm_decoder` 内部「解调 → 解交织 → Viterbi → 解扰 → 拼字节」的 5 级数据流，并说出每一级的位宽与 strobe 信号。
2. 解释为什么 `conv_in0/conv_in1` 用 `3'b111 / 3'b011` 来表示一个「软判决比特」，以及 `erase` 如何实现去穿孔（de-puncture）。
3. 掌握 `num_bits_to_decode`、`do_descramble`、`flush` 三个控制信号的作用，特别是 SIGNAL/HT-SIG 字段为何需要 `flush`「补 0」把 Viterbi 内部残留的比特冲出来。
4. 了解 `viterbi.v` 如何用一个 `ce` 门控给 Xilinx 黑盒 IP「加上 strobe」，并注意到主链路其实是直接例化 IP、并未使用这个封装。

---

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**卷积码与 Viterbi 译码。** 802.11 发射端用 1/2 码率的卷积码给数据加冗余：每输入 1 个比特，输出 2 个编码比特。接收端用 Viterbi 算法在所有可能的输入序列里找一条「与接收序列最相似」的最大似然路径，从而**纠正传输中翻转的比特**。Viterbi 是 OFDM 解码流水线里唯一真正「纠错」的环节，前面所有步骤（同步、FFT、均衡、解调）都只是把信号还原成「带噪比特」。

**硬判决 vs 软判决。** 解调器如果只输出 0/1，叫「硬判决」，丢掉了「这个比特我有多确定」的信息；如果输出一个带置信度的数值，叫「软判决」。Viterbi 用软判决能多纠错约 2 dB。Xilinx 的 `viterbi_v7_0` IP 接受 **3 位软判决**，采用「符号-幅值（sign-magnitude）」编码：最高位是硬判决值（0 或 1，即符号），低 2 位是置信度幅值（0～3，越大越确定）。本讲的一个关键观察是：OpenOFDM 的 `demodulate` 只产出硬判决比特，所以 `ofdm_decoder` 要在喂给 Viterbi 之前，把这些硬比特「升级」成最大置信度的软判决。

**穿孔（puncturing）与去穿孔。** 为了提高码率（3/4、2/3、5/6），发射端在 1/2 卷积码输出后有规律地**删掉**一部分编码比特。接收端要按同样的规律把「哑比特」**补回去**，并告诉 Viterbi「这一位我不知道」，这就是上一讲 `deinterleave` 输出的 `erase` 标记的用途。补回去之后，Viterbi 看到的永远是整齐的 1/2 节奏的比特对。

如果这些概念还比较陌生，可以先把本讲当作「一条数据如何在 5 个模块间流动、谁在什么时候触发谁」的接线图来读，细节后几讲会逐步展开。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [verilog/ofdm_decoder.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v) | **本讲主角**。把 5 个子模块例化串联，并用一个 `always` 块完成「2 bit → 3 bit 软判决」映射与 `flush`/`do_descramble` 控制逻辑。 |
| [verilog/viterbi.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/viterbi.v) | 一份「给 Xilinx Viterbi IP 加 strobe」的**演示性封装**。注意：主解码链路并未使用它，`ofdm_decoder` 直接例化 IP。 |
| [verilog/coregen/viterbi_v7_0.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/viterbi_v7_0.v) | Xilinx Viterbi IP 的仿真行为模型（文件很大，本讲只关心它的端口 `ce/sclr/data_in0/data_in1/erase/rdy/data_out`）。 |
| [verilog/common_defs.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_defs.v) | 全局定点宏，本讲主要关联 `CONS_SCALE_SHIFT`（解调刻度，间接决定软判决满量程）。 |

辅助理解（非本讲主角，但会引用）：

- [verilog/dot11.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v)：顶层状态机，负责在 SIGNAL / HT-SIG / DATA 三种场景下给 `ofdm_decoder` 设定 `do_descramble` 与 `num_bits_to_decode`。
- [verilog/descramble.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/descramble.v)、[verilog/bits_to_bytes.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/bits_to_bytes.v)：流水线的最后两级，下一讲（u3-l6）会专门讲。
- [docs/source/decode.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/decode.rst)：官方对解码四步（解调/解交织/Viterbi/解扰）的文字说明。

---

## 4. 核心概念与源码讲解

### 4.1 ofdm_decoder 子流水线总览

#### 4.1.1 概念说明

`ofdm_decoder` 本身**几乎不写算法**，它的核心职责是「接线 + 控制」：把 5 个已经独立实现好的模块，按数据流向串成一条子流水线，并在它们之间做一些位宽/控制上的适配。这与顶层 `dot11.v`「只连线、不写算法」的风格一脉相承。理解了这一层，你就能把整条「复数样本 → 字节」的路径在脑子里跑一遍。

#### 4.1.2 核心流程

数据从左到右流经 5 级，每一级都用「数据 + strobe」握手，前一级的 `output_strobe` 就是后一级的 `input_strobe`：

```
sample_in(32)  ─▶ demodulate ─▶ deinterleave ─▶ [软判决适配] ─▶ viterbi_v7_0 ─▶ descramble ─▶ bits_to_bytes ─▶ byte_out(8)
   I/Q 复数        6 bit/子载波    2 bit/拍        3,3 bit/拍      1 bit            1 bit          8 bit/字节
                  (硬判决)        (含 erase)       (软判决+erase)   (纠错后)         (解扰)         (拼字节)
```

位宽与节奏的演变（注意「位宽」和「每拍产出」是两回事）：

| 阶段 | 信号 | 位宽 | 含义 |
|------|------|------|------|
| 输入 | `sample_in` | 32 | 高 16 位 I、低 16 位 Q 的均衡后复数 |
| ① 解调 | `demod_out` | 6 | 一个子载波判决出的最多 6 比特（BPSK 用 1 位，64-QAM 用 6 位） |
| ② 解交织 | `deinterleave_out` | 2 | 每拍输出 2 个已重排的编码比特，配 `erase[1:0]` |
| ③ 软判决适配 | `conv_in0/conv_in1` | 3,3 | 把 2 个硬比特各自扩成 3 位软判决，配 `conv_erase` |
| ④ Viterbi | `conv_decoder_out` | 1 | 纠错后的 1 个数据比特 |
| ⑤ 解扰 | `descramble_out` | 1 | 解扰后的 1 个比特（DATA 才走这一级） |
| ⑥ 拼字节 | `byte_out` | 8 | 每 8 个比特拼成 1 字节 |

可以看到位宽呈现「32 → 6 → 2 → 3 → 1 → 1 → 8」先收窄、再在 Viterbi 处收敛成单比特、最后由 `bits_to_bytes` 重新汇合成字节的形状。这正是「冗余被逐步剥离」的物理体现：解调把复数压成比特，解交织把符号内的比特重排，软判决 + Viterbi 把 2 个编码比特「合并」回 1 个数据比特（1/2 码率），最后再 8 倍汇合。

#### 4.1.3 源码精读

5 级实例化集中在 [verilog/ofdm_decoder.v:54-116](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L54-L116)。关键片段（已删去端口对齐）：

```verilog
// ① 解调
demodulate demod_inst (
    .rate(rate), .cons_i(input_i), .cons_q(input_q),
    .input_strobe(sample_in_strobe),
    .bits(demod_out), .output_strobe(demod_out_strobe));

// ② 解交织（输出 2 bit + erase）
deinterleave deinterleave_inst (
    .rate(rate), .in_bits(demod_out),
    .input_strobe(demod_out_strobe),
    .out_bits(deinterleave_out), .output_strobe(deinterleave_out_strobe),
    .erase(erase));

// ④ Viterbi（输入是 ③ 适配出来的 conv_in0/conv_in1，见 4.3）
viterbi_v7_0 viterbi_inst (
    .clk(clock), .ce(vit_ce), .sclr(vit_clr),
    .data_in0(conv_in0), .data_in1(conv_in1), .erase(conv_erase),
    .rdy(vit_rdy), .data_out(conv_decoder_out));

// ⑤ 解扰
descramble decramble_inst (
    .in_bit(conv_decoder_out), .input_strobe(conv_decoder_out_stb),
    .out_bit(descramble_out), .output_strobe(descramble_out_strobe));

// ⑥ 拼字节
bits_to_bytes byte_inst (
    .bit_in(bit_in), .input_strobe(bit_in_stb),
    .byte_out(byte_out), .output_strobe(byte_out_strobe));
```

几个要点：

- `demod_inst` 的输入直接是顶层送进来的 `sample_in`（拆成 `input_i/input_q`，见 [ofdm_decoder.v:36-37](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L36-L37)），`rate` 决定它选哪种星座。
- 注意 ③「软判决适配」**不是一个独立模块**，而是下面那个 `always` 块里几行组合逻辑（[ofdm_decoder.v:143-153](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L143-L153)），它的输出 `conv_in0/conv_in1/conv_erase` 直接喂给 `viterbi_inst`。这与 u1-l5 提到的「概念步骤 ≠ 模块一一对应」完全一致。
- 顶层 `dot11.v` 例化 `ofdm_decoder` 时，把均衡后的 I/Q 接到 `sample_in`，把状态机算好的 `do_descramble`、`num_bits_to_decode`、`pkt_rate` 接进来，把 `byte_out` 收走，见 [dot11.v:356-382](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L356-L382)。

#### 4.1.4 代码实践

**实践目标**：建立整条子流水线的「导航图」，后续每个模块都能挂到这张图上。

**操作步骤**：

1. 打开 [verilog/ofdm_decoder.v:54-116](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L54-L116)。
2. 在纸上（或文本里）画出 6 个方框：`sample_in → demodulate → deinterleave → (软判决) → viterbi → descramble → bits_to_bytes → byte_out`。
3. 在每条连线上标注「位宽 + strobe 信号名」，例如 `demod_out(6) / demod_out_strobe`。
4. 用不同颜色标出两类信号：**数据平面**（实线，被 strobe 驱动）与**控制平面**（虚线，来自顶层 `dot11.v` 的 `rate / do_descramble / num_bits_to_decode`）。

**需要观察的现象**：你会发现 ③ 软判决适配没有方框（它是 `always` 块里的逻辑），而 ④ 与 ⑤ 之间多了一条「`do_descramble` 选择开关」——这正是 4.4 要讲的 DATA/SIGNAL 分流。

**预期结果**：得到一张与上文「核心流程」图一致的接线图，作为后续精读每级的索引。

#### 4.1.5 小练习与答案

**练习 1**：`demod_out` 是 6 位，但 `deinterleave_out` 只有 2 位，数据是不是「变少」了？
**答案**：没有变少，只是**节奏变了**。`demodulate` 每个**子载波**输出一次（最多 6 比特），`deinterleave` 先用双口 RAM 缓存**整个符号**的全部比特，再每拍输出 2 个比特；总比特数守恒，只是从「按子载波」改成了「按拍匀速输出」。

**练习 2**：为什么 Viterbi 的输入是**两个** 3 位符号 `conv_in0/conv_in1`，而不是一个？
**答案**：因为卷积码是 1/2 码率，每 1 个数据比特对应 2 个编码比特。Viterbi 每拍吃进一对编码比特（`sym0, sym1`），译出 1 个数据比特，所以输入端天然是「成对」的。

---

### 4.2 viterbi IP 封装：给黑盒加 strobe

#### 4.2.1 概念说明

Xilinx 的 `viterbi_v7_0` 是一个**纯同步、带 `ce`（clock enable）和 `sclr`（同步复位）的 IP 黑盒**：它在每个 `ce` 有效的时钟沿吃进一对符号，经过内部一段「回溯（traceback）」延迟后，在某个时钟沿吐出一个译码比特并拉高 `rdy`。它本身**没有 strobe 概念**，也不懂「数据 + strobe」握手。

但 OpenOFDM 全项目都采用「数据 + strobe」的单向握手（见 u3-l2）。于是需要一个**适配层**：把上游的 strobe「翻译」成 IP 能理解的 `ce`，把 IP 的 `rdy`「翻译」回下游能理解的 strobe。[verilog/viterbi.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/viterbi.v) 就是这份适配层的一个干净写法。

#### 4.2.2 核心流程

适配的思路只有一句话：**用 strobe 当作 clock enable**。

- 输入侧：`ce = reset | (enable & input_strobe)`。只有当 `input_strobe` 有效（且模块 `enable`）时，IP 才在这一拍真正「吃」数据；其余拍 `ce=0`，IP 内部状态原地不动，就像时钟暂停了一样。复位时 `ce=1` 让 `sclr` 生效。
- 输出侧：`output_strobe = rdy`。IP 算出一个比特时自然拉高 `rdy`，直接拿来当输出 strobe，数据 `data_out` 与它在同一拍对齐。

```
input_strobe ─┐
              ├─▶ ce = reset | (enable & input_strobe) ─▶ [IP] ─▶ rdy ─▶ output_strobe
enable      ──┘                                          └─▶ data_out
reset       ─▶ sclr
```

这种「strobe 门控 ce」是给一切同步 IP（不只是 Viterbi）加握手的通用手法，理解了它，你看 `equalizer` 里的除法器、`sync_long` 里的 FFT 接口都会是同一套套路。

#### 4.2.3 源码精读

封装版 [verilog/viterbi.v:20-29](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/viterbi.v#L20-L29)：

```verilog
viterbi_v7_0 viterbi_inst (
    .clk(clock),
    .ce(reset | (enable & input_strobe)),   // ★ 用 strobe 门控 ce
    .sclr(reset),
    .data_in0(sym0), .data_in1(sym1),
    .erase(erite),
    .rdy(output_strobe),                     // ★ rdy 直接当输出 strobe
    .data_out(out_bit));
```

**重要事实**：主解码链路**并没有使用**这个 `viterbi` 封装模块。`ofdm_decoder` 里直接例化了 IP 本身，并自己算 `ce`，见 [verilog/ofdm_decoder.v:39-45](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L39-L45) 与 [ofdm_decoder.v:81-90](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L81-L90)：

```verilog
wire vit_ce  = reset | (enable & conv_in_stb);   // 与封装版完全相同的套路
wire vit_clr = reset;
wire vit_rdy;
...
viterbi_v7_0 viterbi_inst (
    .clk(clock), .ce(vit_ce), .sclr(vit_clr),
    .data_in0(conv_in0), .data_in1(conv_in1), .erase(conv_erase),
    .rdy(vit_rdy), .data_out(conv_decoder_out));

assign conv_decoder_out_stb = vit_ce & vit_rdy;  // ce & rdy 才是真的「有效输出」
```

也就是说 `ofdm_decoder` 把 `viterbi.v` 那几行「内联」了。这里还多了一个细节：输出 strobe 是 `vit_ce & vit_rdy`，而不只是 `vit_rdy`——因为只有在 `ce` 有效（即我们这一拍真的喂了输入）时，`rdy` 的含义才成立；`ce=0` 的拍上即使 `rdy` 还有残留也不应被下游当作新数据。

#### 4.2.4 代码实践

**实践目标**：确认「封装版」与「内联版」是同一套 ce 门控逻辑，并理解为什么主链路选择内联。

**操作步骤**：

1. 并排打开 [verilog/viterbi.v:20-29](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/viterbi.v#L20-L29) 与 [verilog/ofdm_decoder.v:81-90](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L81-L90)。
2. 把两者的端口逐项对应：`sym0↔conv_in0`、`sym1↔conv_in1`、`erase↔conv_erase`、`input_strobe↔conv_in_stb`、`out_bit↔conv_decoder_out`、`output_strobe↔conv_decoder_out_stb`。
3. 验证两份代码的 `ce` 表达式是否一致（除了变量名）。

**需要观察的现象**：两者的 `ce` 都是 `reset | (enable & <strobe>)`，仅变量名不同；`viterbi.v` 把 `rdy` 直接当 strobe，而 `ofdm_decoder.v` 用 `vit_ce & vit_rdy` 更严谨。

**预期结果**：写出一句结论——「`viterbi.v` 是可复用的演示封装，`ofdm_decoder` 出于对 `conv_in_stb`/`flush` 等本地信号的灵活控制，选择直接内联同样的 ce 门控，因此封装模块在主链路中未被实例化。」（可在仓库内全局搜索 `viterbi viterbi_inst` 验证封装模块确实无人调用。）

#### 4.2.5 小练习与答案

**练习 1**：如果把 `ce` 直接恒接 `1'b1`（不用 strobe 门控），会发生什么？
**答案**：IP 会在**每个时钟沿**都把 `data_in0/data_in1` 当成新符号吃进去。由于上游数据只在 strobe 拍上才有效，其它拍上是旧值/垃圾值，IP 会把这些重复或垃圾符号也译码，输出一堆错误比特。门控 `ce` 的本质是「让 IP 的内部时间跟着数据的节奏走」。

**练习 2**：为什么 `conv_decoder_out_stb = vit_ce & vit_rdy`，而不只是 `vit_rdy`？
**答案**：`vit_rdy` 是 IP 内部的「这一拍有合法输出」标志，但只有当 `vit_ce=1`（我们确实在推进 IP）时它才对应一次真实的译码。在 `ce=0` 的拍上 `rdy` 可能仍保持上一拍的高电平，若直接当 strobe 会造成下游多收一个重复比特。加 `& vit_ce` 是为了与输入节奏严格对齐。

---

### 4.3 软判决与 erase：2 bit 硬判决升级为 3 bit 软判决

#### 4.3.1 概念说明

这是本讲最关键的一小段代码，也是本讲的第一个核心实践任务所在。

`deinterleave` 每拍吐出 2 个**硬判决**比特 `deinterleave_out[1:0]`（每个非 0 即 1）和一个 2 位的 `erase` 标记。而 Viterbi IP 要的是 **3 位软判决**符号。于是 `ofdm_decoder` 必须做一次「1 位 → 3 位」的扩展。问题是：这 3 位该填什么？

答案是 OpenOFDM 的一个**设计取舍**：由于 `demodulate` 已经做掉了硬判决（没有把置信度保留下来传给后面），到 Viterbi 这里时我们已经**没有真正的软信息可用**。于是采取最简单的策略——**把每个硬比特都当成「最大置信度」**喂给 Viterbi：

- 硬比特 = 1 → 软判决 `3'b111`（「我非常确信是 1」）
- 硬比特 = 0 → 软判决 `3'b011`（「我非常确信是 0」）

`erase` 则用来处理穿孔位置：那些被发射端删掉、接收端补回来的「哑比特」，我们**真的不知道**它的值，于是让 IP 把它当「未知」处理，不参与路径度量的计算。

#### 4.3.2 核心流程

3 位「符号-幅值」软判决的编码约定：

\[ v = (b_2,\, b_1,\, b_0),\qquad \text{硬判决值} = b_2,\qquad \text{置信度幅值} = \{b_1,b_0\}\in\{0,1,2,3\} \]

- `3'b111`：\(b_2=1\)（判为 1），幅值 \(=3\)（最确信）。
- `3'b011`：\(b_2=0\)（判为 0），幅值 \(=3\)（最确信）。
- `3'b100` / `3'b000`：幅值为 0，表示「完全不确定」（软判决里最弱的 1 / 0）。

因此一个硬比特 \(c\in\{0,1\}\) 到软判决的映射为：

\[ \text{soft}(c) = \{\,c,\;2'b11\,\} \quad\Longrightarrow\quad c=1\mapsto 3'b111,\;\; c=0\mapsto 3'b011 \]

整条 ③ 适配逻辑的伪代码：

```
if 有 deinterleave 输出 strobe（且未 flush）:
    conv_in0   = out[0] ? 3'b111 : 3'b011   # 硬比特 → 满幅软判决
    conv_in1   = out[1] ? 3'b111 : 3'b011
    conv_erase = erase                      # 穿孔位原样透传给 IP
    conv_in_stb = 1                          # 告诉 IP 这一拍有合法输入
```

#### 4.3.3 源码精读

软判决映射就两行，在 [verilog/ofdm_decoder.v:143-153](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L143-L153)：

```verilog
if (!flush) begin
    conv_in_stb <= deinterleave_out_strobe;
    conv_in0    <= deinterleave_out[0] ? 3'b111 : 3'b011;   // ★ 硬比特→满幅软判决
    conv_in1    <= deinterleave_out[1] ? 3'b111 : 3'b011;
    conv_erase  <= erase;                                   // ★ 穿孔标记透传
end else begin
    // flush 分支见 4.4
end
```

`erase` 来自 `deinterleave` 模块的 LUT 输出（[verilog/deinterleave.v:34-35](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L34-L35)）：当某一位是「为补穿孔而插入的哑比特」时，对应的 LUT 表项里 `null_a/null_b` 位置 1，于是 `erase[i]=1`，`conv_erase` 原样把它送给 Viterbi IP，IP 便知道这一位是「未知」，不把它纳入累加度量。这正是上一讲提到的去穿孔（de-puncture）在数据平面上的最终落点。

#### 4.3.4 代码实践（本讲指定实践 · 第一部分）

**实践目标**：解释 `conv_in0/conv_in1` 为什么用 `3'b111 / 3'b011` 来代表一个软判决比特。

**操作步骤**：

1. 打开 [verilog/ofdm_decoder.v:145-147](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L145-L147)。
2. 写出 3 位符号-幅值编码表（共 8 个值），标出每个值的「硬判决值」与「置信度」。
3. 对照 `demodulate` 的输出（[verilog/demodulate.v:13](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v#L13) 的 `bits` 是确定的 0/1 比特），说明为什么这里只能填「满幅」置信度，而无法填出真正的软信息。

**需要观察的现象**：你会确认 `3'b111` 与 `3'b011` 的差别**仅在最高位**（符号位），低 2 位都是 `11`（满幅）——也就是说「0 和 1 被赋予相同的最强置信度，区别只在方向」。

**预期结果**：得到一段 100～150 字的解释，核心是：「`demodulate` 已做硬判决、丢弃了置信度，故 `ofdm_decoder` 只能把每个硬比特映射成『符号 = 硬比特值、幅值 = 最大』的软判决 `3'b111/3'b011`；真正的软信息已在解调时丢失，这是 OpenOFDM 用硬判决喂 Viterbi 的既定取舍。」

#### 4.3.5 小练习与答案

**练习 1**：如果要做成「真正的软判决」，应该改哪里？
**答案**：需要让 `demodulate` 不再做硬判决，而是输出每个比特的置信度（例如星座点到判决边界的距离），再把 `ofdm_decoder` 的映射改成 `{硬判决位, 2位置信度}`。这会牺牲解调器的简洁性，但能给 Viterbi 提供真正的软信息、提升纠错能力。

**练习 2**：当一个编码比特的 `erase=1` 时，`conv_in0` 的值（`3'b111` 或 `3'b011`）还重要吗？
**答案**：不重要。`erase=1` 告诉 IP「这一位未知」，IP 会忽略 `data_in0` 的具体数值、不把它计入路径度量。所以穿孔位的哑比特填什么都可以，代码里它仍然按硬比特规则填，但这个值实际不会被使用。

---

### 4.4 控制平面：num_bits_to_decode、do_descramble 与 flush

#### 4.4.1 概念说明

`ofdm_decoder` 除了「接线」，还承担一份**控制职责**：根据当前解码的是 SIGNAL / HT-SIG / DATA，决定要不要解扰、要不要在结尾「冲一下」Viterbi。这是本讲第二个核心实践任务（`flush`）所在。

关键背景：**Viterbi 是一个有内部延迟的流式译码器**。你喂进去最后一个符号后，它内部还要回溯若干拍才能吐出最后几个译码比特。对于 SIGNAL / HT-SIG 这种「只解码固定几个字节、译完立刻复位」的字段，如果不在结尾继续推几下 Viterbi，最后那几个比特就会**卡在 IP 内部出不来**，导致字节拼不全。`flush` 就是干这件事的——在结尾持续喂「已知为 0」的哑符号，把残留比特冲出来。

而 DATA 字段不需要 `flush`，因为数据是连续流，长度由顶层 `pkt_len` 控制，结尾自然由卷积码的 6 个 0 尾比特终止，且整个包结束后会被复位。

#### 4.4.2 核心流程

三个控制信号由顶层 `dot11.v` 状态机设定（见 4.4.3），它们的含义：

| 信号 | 含义 | 典型取值 |
|------|------|----------|
| `do_descramble` | 1 = 走解扰（DATA）；0 = 不解扰（SIGNAL/HT-SIG） | DATA=1，其余=0 |
| `num_bits_to_decode` | 当前字段要从 `deinterleave` 收多少个比特后才 flush | SIGNAL=48，HT-SIG=96 |
| `flush`（内部） | 1 = 持续向 Viterbi 喂「强 0」哑符号，冲出残留比特 | 仅 `~do_descramble` 时触发 |

控制逻辑伪代码：

```
每拍:
  if deinterleave 有输出:
      deinter_out_count += 2          # 数已经收到多少比特
  else (符号间的空隙):
      if (不解扰) 且 (已收够 num_bits_to_decode):
          flush = 1                    # 触发冲刷

  if 未 flush:
      正常喂软判决 (4.3)
  else:                                # flush 分支
      conv_in_stb = 1                  # 每拍都推 IP
      conv_in0 = conv_in1 = 3'b011     # 喂「强 0」(802.11 尾比特就是 0)
      conv_erase = 0

  # 输出路由（也是 do_descramble 控制）
  if deinter_out_count > 0:            # 等真正有数据了再往下传
      if 不解扰:
          bit_in = conv_decoder_out    # SIGNAL/HT-SIG：直接拼字节
      else:
          bit_in = descramble_out      # DATA：先解扰再拼字节
          并跳过前 9 个 service 比特
```

两个要点：

1. **`flush` 喂的是 `3'b011`（强 0），不是 `erase`**。因为 802.11 卷积码的尾比特本身就是 6 个 0，用「确信为 0」的软判决去冲，正好与编码端的 0 尾吻合，能把残留路径正确导向终态。
2. **`flush` 一旦置 1 就保持**，直到顶层用 `ofdm_reset` 复位 `ofdm_decoder` 才清零。对 SIGNAL 字段，`dot11.v` 在收满 3 字节后（[dot11.v:550-559](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L550-L559)）就会拉高 `ofdm_reset` 切到 `S_CHECK_SIGNAL`，自然结束这次 flush。

至于 `skip_bit = 9`：DATA 字段开头是 16 位的 SERVICE 字段，其中前 7 位被 `descramble` 模块用来初始化 LFSR（u3-l6 详述），剩下 9 位是保留位、需要丢弃，所以这里 `skip_bit` 初值为 9，先丢掉这 9 个解扰输出再开始拼真正的数据字节（见 [ofdm_decoder.v:126-127](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L126-L127) 与 [ofdm_decoder.v:155-172](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L155-L172)）。

#### 4.4.3 源码精读

控制信号在顶层 `dot11.v` 的设定点：

- **SIGNAL 字段**（BPSK 1/2，48 个编码比特）：`do_descramble=0`、`num_bits_to_decode=48`，见 [dot11.v:518-519](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L518-L519)。
- **legacy DATA 字段**：`do_descramble=1`、`num_bits_to_decode=(legacy_len+3)<<4`，见 [dot11.v:588-589](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L588-L589)。
- **HT-SIG 字段**（两个符号，96 个编码比特）：`do_descramble=0`、`num_bits_to_decode=96`，见 [dot11.v:622-623](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L622-L623)。
- **HT DATA 字段**：`do_descramble=1`、`num_bits_to_decode=(ht_len+3)<<4`，见 [dot11.v:765-767](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L765-L767)。

`ofdm_decoder` 内部的 flush 与路由逻辑在 [verilog/ofdm_decoder.v:118-174](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L118-L174)，关键片段：

```verilog
// flush 触发条件：不解扰 ＋ 已收够比特 ＋ 当前是符号间空隙
if (deinterleave_out_strobe)
    deinter_out_count <= deinter_out_count + 2;
else if (~do_descramble && deinter_out_count >= num_bits_to_decode)
    flush <= 1;

// flush 时持续喂「强 0」
if (!flush) begin
    conv_in_stb <= deinterleave_out_strobe;
    conv_in0 <= deinterleave_out[0] ? 3'b111 : 3'b011;
    conv_in1 <= deinterleave_out[1] ? 3'b111 : 3'b011;
    conv_erase <= erase;
end else begin
    conv_in_stb <= 1;            // 每拍都推
    conv_in0 <= 3'b011;          // 强 0
    conv_in1 <= 3'b011;
    conv_erase <= 0;
end
```

注意触发条件里的 `else`——`flush` 只能在 `deinterleave_out_strobe` 为 0 的「符号间空隙」拍上置位。这保证不会在一个符号还没输出完时提前冲刷。对于 SIGNAL（恰好 1 个符号、48 比特），`deinter_out_count` 数到 48 之后、下一拍没有 strobe，`flush` 才被拉起来。

#### 4.4.4 代码实践（本讲指定实践 · 第二部分）

**实践目标**：解释 `do_descramble=0`（SIGNAL / HT-SIG 场景）时 `flush` 如何「补 0」让 Viterbi 输出剩余比特。

**操作步骤**：

1. 打开 [verilog/ofdm_decoder.v:132-153](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L132-L153)。
2. 追踪一次 SIGNAL 解码的完整时间线：
   - 顶层在 [dot11.v:516-528](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L516-L528) 检出长前导后，设 `num_bits_to_decode=48`、`do_descramble=0`，进入 `S_DECODE_SIGNAL`。
   - `deinterleave` 把 SIGNAL 符号的 48 个编码比特匀速吐出，`deinter_out_count` 数到 48。
   - 之后没有更多 strobe，`flush` 被拉高，Viterbi 开始每拍吃进一个「强 0」对。
   - 残留比特被冲出来 → `conv_decoder_out` → `bit_in`（因为 `~do_descramble`，直接走 [ofdm_decoder.v:156-158](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L156-L158)）→ `bits_to_bytes` → `byte_out`。
   - 顶层收满 3 字节后 [dot11.v:550-559](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L550-L559) 拉高 `ofdm_reset`，`flush` 随之清零。
3. 思考：为什么 DATA 场景（`do_descramble=1`）不需要 flush？

**需要观察的现象**：若用仿真（参考 u1-l2 的 `make simulate`，用 `testing_inputs/conducted` 下的 dot11a 样本），在波形里盯住 `dot11_state==S_DECODE_SIGNAL` 区间，会看到 `deinterleave_out_strobe` 停止后 `flush` 跳变为 1、`conv_in_stb` 持续为 1、`conv_in0/conv_in1` 维持 `3'b011`，随后第 3 个 `byte_out_strobe` 出现。

**预期结果**：写出一段解释——「SIGNAL/HT-SIG 译码后立即要复位，但 Viterbi 内部还有未吐出的残留比特；`flush` 通过持续喂 `3'b011`（强 0，与卷积码 0 尾一致）把残留比特冲出来；DATA 不解扰结束也不立即复位，由 `pkt_len` 与卷积尾比特自然收尾，故不需要 flush。」

> 若暂时无法跑仿真，可标注「待本地验证」并仅完成源码追踪部分。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `flush` 分支里的 `3'b011` 改成 `3'b111`（强 1），SIGNAL 译码会出什么问题？
**答案**：`flush` 的作用是补齐卷积码的 6 个 0 尾比特。若改成「强 1」，Viterbi 会以为尾比特是 1，回溯路径会被导向错误的状态，残留比特译错，SIGNAL 的 rate/length 可能解错，进而整个包都按错的速率解码。

**练习 2**：为什么 `flush` 的触发条件里要有 `~do_descramble`？去掉它（让 DATA 也 flush）会怎样？
**答案**：DATA 是连续数据流，长度由 `pkt_len` 控制，包结束后整体复位，残留比特不影响（且 DATA 末尾本就有卷积尾比特）。若强行让 DATA 也 flush，会在每个符号间的空隙误触发，破坏正常数据流。`~do_descramble` 把 flush 严格限制在「译完即复位」的 SIGNAL/HT-SIG 场景。

---

## 5. 综合实践

**任务**：以一个 802.11a 24 Mbps 的 SIGNAL 符号为对象，把本讲四节内容串成一条完整的时间线，并用一张「时序表」呈现。

**建议步骤**：

1. 选用 `testing_inputs/conducted` 下的 dot11a 样本，按 u1-l2 跑 `make compile && make simulate`，得到 `sim_out/` 与 `dot11.vcd`（若环境不具备，则纯做源码追踪，并标注「待本地验证」）。
2. 在波形里定位 `dot11_state` 进入 `S_DECODE_SIGNAL` 的区间，依次记录下列信号的关键跳变（拍数近似即可）：
   - `sample_in_strobe` → `demod_out_strobe`（解调）
   - `demod_out_strobe` → `deinterleave_out_strobe`（解交织，注意符号缓存延迟）
   - `deinterleave_out_strobe` 与 `erase`（去穿孔标记）
   - `conv_in0/conv_in1`（软判决值）与 `conv_in_stb`
   - `flush` 跳变为 1 的时刻
   - `conv_decoder_out_stb`（Viterbi 出比特）
   - `byte_out_strobe`（共 3 次）
3. 对照本讲四节，在时序表里标注：
   - 哪一段对应 4.1 的「5 级流水」；
   - `conv_in0` 取 `3'b111`/`3'b011` 的依据对应 4.3；
   - `flush` 区间对应 4.4；
   - Viterbi 的 `ce` 门控对应 4.2。
4. 验证一个量化结论：SIGNAL 的 `num_bits_to_decode=48`，而 `deinter_out_count` 每拍 `+2`，所以应在第 24 个 `deinterleave_out_strobe` 之后、紧接着的无 strobe 拍上看到 `flush` 置 1。

**预期产出**：一张「SIGNAL 解码时序表」+ 一段说明，解释数据如何从 32 位复数样本一步步收窄成 3 个字节，以及 `flush` 在结尾的补 0 作用。

---

## 6. 本讲小结

- `ofdm_decoder` 是一条 5 级子流水线：`demodulate → deinterleave → (软判决适配) → viterbi_v7_0 → descramble → bits_to_bytes`，位宽走「32 → 6 → 2 → 3 → 1 → 1 → 8」，本质是冗余被逐步剥离再汇合成字节。
- ③「软判决适配」不是独立模块，而是 `always` 块里把 2 个硬比特扩成两个 3 位软判决 `3'b111/3'b011` 的几行逻辑；`erase` 透传穿孔标记给 Viterbi。
- 给 Xilinx Viterbi IP「加 strobe」的通用手法是用输入 strobe 门控 `ce`、用 `rdy` 当输出 strobe；`ofdm_decoder` 直接内联了这套逻辑，`viterbi.v` 封装模块在主链路中并未被使用。
- `do_descramble` 决定输出走 `conv_decoder_out`（SIGNAL/HT-SIG）还是 `descramble_out`（DATA，并跳过 9 个 service 比特）。
- `flush` 仅在 `~do_descramble` 且收够 `num_bits_to_decode` 个比特后触发，持续喂 `3'b011`（强 0，吻合卷积码 0 尾）把 Viterbi 内部残留比特冲出来；DATA 不需要 flush。
- 三个控制信号 `do_descramble / num_bits_to_decode` 由顶层 `dot11.v` 状态机按 SIGNAL / HT-SIG / DATA 三种场景分别设定。

---

## 7. 下一步学习建议

- **u3-l6 解扰与串并转换**：深入 `descramble.v` 的 7 级 LFSR 初始化（本讲提到的「前 7 比特初始化」与「跳过 9 个 service 比特」都会在那里讲透）和 `bits_to_bytes.v` 的拼字节逻辑。
- **u4 控制平面**：本讲的 `do_descramble / num_bits_to_decode / ofdm_reset` 都来自顶层状态机；学完 u4-l1（dot11 状态机）与 u4-l2（SIGNAL 解析）后，你会看清这些信号是在哪个状态、依据什么字段算出来的。
- **u5 Python 参考解码器与交叉验证**：想确认本讲的软判决/flush 行为是否正确，可以用 `scripts/decode.py` 跑出 Viterbi 阶段的期望输出，再用 `scripts/test.py` 与仿真落盘的 `conv_*.txt` 逐位比对。
- **延伸阅读**：`docs/source/decode.rst` 的 Viterbi 与 Descrambling 两节是本讲对应的官方文字说明，可作为对照。
