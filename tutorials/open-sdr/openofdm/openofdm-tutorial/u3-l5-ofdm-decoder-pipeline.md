# u3-l5 ofdm_decoder 子流水线与卷积解码

## 1. 本讲目标

本讲精读 `ofdm_decoder.v`。它是 OpenOFDM 解码流水线里把「频域复数」变成「字节」的最后一段，承接 u3-l4 的解交织（deinterleave），向下交给 u3-l6 的解扰（descramble）。

学完本讲，你应当能够：

1. 画出 `ofdm_decoder` 的 5 级子流水线（demodulate → deinterleave → viterbi → descramble → bits_to_bytes），并指出每一级的位宽。
2. 说清楚「2 bit 硬判决 → 3 bit 软判决」的映射 `conv_in0/conv_in1` 为什么写成 `3'b111 / 3'b011`，以及 `erase` 标志的作用。
3. 解释 `viterbi.v` 这个 Xilinx IP 封装如何用 `ce/rdy` 把无 strobe 的黑盒改造成「数据 + strobe」风格。
4. 说清楚 `num_bits_to_decode`、`do_descramble`、`flush` 三个控制信号如何配合，让 Viterbi 在解 SIGNAL/HT-SIG 这类「不扰码、且需要冲刷」的场景下吐出剩余比特。

本讲只讲「比特流如何在 5 个子模块之间流动」，以及「Viterbi 这一级的控制细节」。星座解调的判决门限见 u3-l3，解交织的查表与去穿孔见 u3-l4，解扰的 LFSR 初始化见 u3-l6。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个关键概念。

### 2.1 卷积码与 Viterbi 译码

802.11 发射机在调制之前，会把每个数据比特扩成多个比特再发出去，这就是**卷积编码**（convolutional encoding）。它引入冗余，使接收端能在有噪声时纠正少量错误。802.11 OFDM 的母码是码率 1/2、约束长度 K=7 的卷积码，生成多项式为：

- g0 = (133)₈ = (1011011)₂
- g1 = (171)₈ = (1111001)₂

即每输入 1 个比特，输出 2 个比特（记作 sym0、sym1）。接收端要做的就是把这两路比特「逆推」回原始 1 个比特，这一步用**Viterbi 算法**完成最大似然序列估计。OpenOFDM 不自己写 Viterbi，而是调用 Xilinx 的 `viterbi_v7_0` IP 核（见 [docs/source/decode.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/decode.rst) 的「Viterbi Decoding」一节）。

### 2.2 硬判决 vs 软判决

- **硬判决（hard decision）**：解调器直接吐出 0 或 1，一个比特就是一个比特。
- **软判决（soft decision）**：解调器吐出一个多位数值，不仅表达「像 0 还是像 1」，还表达「有多像」。Viterbi 译码器拿到软判决后纠错能力更强。

OpenOFDM 的 `demodulate`（u3-l3）只输出硬判决（最多 6 bit 的 `bits`），没有携带置信度。因此 `ofdm_decoder` 在喂给 Viterbi 之前，要把 1 bit 硬判决「放大」成 3 bit 软判决，并固定填上「最大置信度」。这正是本讲的核心技巧之一。

### 2.3 Viterbi 的流水延迟与「冲刷」

Viterbi 译码不是「进一个比特、出一个比特」的组合逻辑，它内部维护一条幸存路径，要做**回溯（traceback）**才能输出判决。这意味着：输入最后一个有效比特之后，译码器内部还「积压」了若干个尚未输出的比特，需要继续喂一些「假」输入把它们顶出来。这个动作叫 **flush（冲刷）**。

卷积码的尾比特（tail bits）通常固定为 0（零终止），所以 flush 时喂「0」对应的软判决即可。

### 2.4 控制信号从哪来

`ofdm_decoder` 有三个「指令」输入（见源码 [ofdm_decoder.v:11-13](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L11-L13)）：

- `rate[7:0]`：当前包的速率/ MCS，驱动 demodulate 选星座、驱动 deinterleave 选交织表。
- `do_descramble`：1 表示这段比特是「数据」（需要解扰），0 表示这段比特是 SIGNAL / HT-SIG（控制字段，**不扰码**）。
- `num_bits_to_decode[31:0]`：本次需要解出多少个**编码后**比特，达到后触发 flush。

这三个信号由 `dot11.v` 的状态机根据当前处于 SIGNAL、HT-SIG 还是 DATA 阶段动态写入（见第 4.4 节的表格）。`ofdm_decoder` 本身只读不改。

### 2.5 「数据 + strobe」握手回顾

全项目采用单向无反压握手（u1-l4、u3-l2 已建立）：每个数据都配一个 1 bit 的 `strobe`，strobe 为 1 时数据有效。本讲你会看到 `ofdm_decoder` 如何为「天生没有 strobe」的 Viterbi IP 核补上这层握手。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲如何使用 |
|---|---|---|
| [verilog/ofdm_decoder.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v) | 5 级子流水线顶层，例化 5 个子模块并加控制逻辑 | 主角，逐行精读 |
| [verilog/viterbi.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/viterbi.v) | Xilinx Viterbi IP 的薄封装，示范如何加 strobe | 对照阅读，理解封装意图 |
| [verilog/coregen/viterbi_v7_0.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/viterbi_v7_0.v) | Xilinx coregen 生成的 Viterbi IP 仿真行为模型（黑盒） | 只看端口，不读实现 |
| [verilog/common_defs.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_defs.v) | 全局 define，如 `CONS_SCALE_SHIFT` | 确认软判决与星座刻度的关系 |
| [verilog/dot11.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) | 顶层状态机，写入 `do_descramble` / `num_bits_to_decode` | 看「指令」从哪来 |
| verilog/demodulate.v / deinterleave.v / descramble.v / bits_to_bytes.v | 子流水线各级 | 只看其端口与输出位宽 |

---

## 4. 核心概念与源码讲解

### 4.1 ofdm_decoder 子流水线总览

#### 4.1.1 概念说明

`ofdm_decoder` 不是「一个算法」，而是一个「把 5 个已有模块串成流水线」的**胶水模块**。它的输入是均衡器（equalizer）吐出的频域归一化复数样本，输出是字节。整条链路把数据一步步「压缩」：

\[ \text{复数}(32\text{bit}) \xrightarrow{\text{demod}} \text{比特}(6\text{bit}) \xrightarrow{\text{deinter}} \text{去交织比特}(2\text{bit}) \xrightarrow{\text{viterbi}} \text{纠错比特}(1\text{bit}) \xrightarrow{\text{descramble}} \text{明文比特}(1\text{bit}) \xrightarrow{\text{bits\_to\_bytes}} \text{字节}(8\text{bit}) \]

位宽曲线「32 → 6 → 2 → 1 → 1 → 8」：前半段不断收窄（一个复数点携带的比特越来越少），最后在 `bits_to_bytes` 处把 8 个 1 bit 重新打包成一个字节。

#### 4.1.2 核心流程

数据从左到右单向流动，每一级都用 strobe 握手：

```text
sample_in(32b) + sample_in_strobe
        │  demodulate   rate → 星座 → 6 bit 硬判决
        ▼
demod_out(6b) + demod_out_strobe
        │  deinterleave  rate/ht → 查表重排 → 2 bit + erase
        ▼
deinterleave_out(2b) + erase(2b) + strobe
        │  ★控制：2bit→3bit 软判决 + flush 补 0
        ▼
conv_in0/conv_in1(3b) + conv_erase + conv_in_stb
        │  viterbi_v7_0  Xilinx IP，ce/rdy 握手
        ▼
conv_decoder_out(1b) + conv_decoder_out_stb
        │  descramble   LFSR 解扰（do_descramble=1 才有效）
        ▼
descramble_out(1b) + descramble_out_strobe
        │  bits_to_bytes 跳过 9 bit service 后每 8 bit 成字节
        ▼
byte_out(8b) + byte_out_strobe
```

关键点：`demodulate`、`deinterleave`、`descramble`、`bits_to_bytes` 都自带 strobe 输出，唯独 `viterbi_v7_0` 没有 strobe（它是 Xilinx 黑盒）。所以 `ofdm_decoder` 要手工为它构造 `conv_in_stb`（输入有效）和 `conv_decoder_out_stb`（输出有效）。这是本模块控制逻辑的主要工作。

#### 4.1.3 源码精读

先看端口。`ofdm_decoder` 把每一级的中间结果都拉到顶层端口上，便于仿真落盘与交叉验证（见 [ofdm_decoder.v:1-29](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L1-L29)）：

```verilog
output [5:0] demod_out,            // 第 1 级：解调输出 6 bit
output demod_out_strobe,
output [1:0] deinterleave_out,     // 第 2 级：解交织输出 2 bit
output deinterleave_out_strobe,
output conv_decoder_out,           // 第 3 级：Viterbi 输出 1 bit
output conv_decoder_out_stb,
output descramble_out,             // 第 4 级：解扰输出 1 bit
output descramble_out_strobe,
output [7:0] byte_out,             // 第 5 级：字节输出
output byte_out_strobe
```

5 个子模块按数据流向依次例化（[ofdm_decoder.v:54-116](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L54-L116)）。注意前后级的「输出 strobe」直接接到下一级的「输入 strobe」，形成天然的链式握手：

```verilog
// 1. demodulate：复数 → 6 bit 硬判决
demodulate demod_inst ( ..., .input_strobe(sample_in_strobe),
                        .bits(demod_out), .output_strobe(demod_out_strobe) );

// 2. deinterleave：6 bit → 2 bit + erase
deinterleave deinterleave_inst ( ..., .in_bits(demod_out),
                        .input_strobe(demod_out_strobe),
                        .out_bits(deinterleave_out),
                        .output_strobe(deinterleave_out_strobe), .erase(erase) );

// 3. viterbi_v7_0：2 bit 软判决 → 1 bit（注意是直连 IP，见 4.3）
viterbi_v7_0 viterbi_inst ( ..., .data_in0(conv_in0), .data_in1(conv_in1),
                        .erase(conv_erase), .rdy(vit_rdy),
                        .data_out(conv_decoder_out) );

// 4. descramble：1 bit → 1 bit
descramble decramble_inst ( ..., .in_bit(conv_decoder_out),
                        .input_strobe(conv_decoder_out_stb),
                        .out_bit(descramble_out), ... );

// 5. bits_to_bytes：1 bit → 8 bit
bits_to_bytes byte_inst ( ..., .bit_in(bit_in), .input_strobe(bit_in_stb),
                        .byte_out(byte_out), .output_strobe(byte_out_strobe) );
```

> 这里有一个容易看漏的细节：第 3 级直接例化的是 Xilinx IP `viterbi_v7_0`，**不是** `verilog/viterbi.v` 里的封装。第 4.3 节会解释两者的关系。

#### 4.1.4 代码实践

**实践目标**：建立 5 级子流水线的全局地图，确认每一级的位宽。

**操作步骤**：

1. 打开 [ofdm_decoder.v:54-116](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L54-L116)。
2. 对照 4.1.2 的流程图，为每个 `*_inst` 找到它的「输入 strobe 来源」和「输出 strobe 去向」。
3. 注意 `byte_inst` 的输入不是 `descramble_out`，而是 `bit_in`（一个寄存器）——这是因为 `do_descramble` 为 0 时数据要绕过解扰，详见 4.4。

**需要观察的现象**：第 1→2→3 级之间是「上一级 output_strobe 直连下一级 input_strobe」的纯连线；而第 3→4、第 4→5 之间多了寄存器（`conv_decoder_out_stb`、`bit_in`/`bit_in_stb`），说明这两处有控制逻辑介入。

**预期结果**：你能画出一张「模块 → 输入位宽 → 输出位宽 → strobe 来源」的表，且能解释为什么 Viterbi 这一级不能像前两级那样直接连线（因为它没有自带 strobe）。

#### 4.1.5 小练习与答案

**练习 1**：`ofdm_decoder` 把 `demod_out`、`deinterleave_out`、`conv_decoder_out`、`descramble_out`、`byte_out` 全都暴露成端口，明明内部也能用，为什么要这么做？

> **答案**：这些中间信号要落盘到 `sim_out/*.txt`，供 `scripts/test.py` 与 Python 参考解码器做**逐阶段交叉验证**（见 u5-l2）。一旦某一级出错，可以直接定位是 demod / deinterleave / viterbi / descramble 哪一步出了问题，而不必猜。

**练习 2**：从 `sample_in` 到 `byte_out`，数据位宽经历「32 → 6 → 2 → 1 → 8」。其中 6 → 2 的「收窄」是哪一级完成的？为什么？

> **答案**：由 `deinterleave` 完成。解调每个复数点最多给出 6 bit（64-QAM），但解交织按符号重排后每拍只输出 2 bit（u3-l4 的双口 RAM 每拍读两个地址）。这是为了把「每符号 N_CBPS 个比特」按 1/2 卷积码的节奏（每次喂 2 个编码比特）交给 Viterbi。

---

### 4.2 软判决：把 1 bit 硬判决放大成 3 bit

#### 4.2.1 概念说明

Xilinx `viterbi_v7_0` 的输入 `data_in0/data_in1` 各是 3 bit（[viterbi_v7_0.v:45-46](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/viterbi_v7_0.v#L45-L46)）。这 3 bit 是**软判决**：既要表达「这个编码比特是 0 还是 1」，又要表达「我有多确信」。

OpenOFDM 的 `demodulate` 只给硬判决（1 bit），没有置信度信息。于是 `ofdm_decoder` 的策略是：**固定填最大置信度**，让 Viterbi 把这个硬判决当成「非常确定」的软值来用。这样虽不能享受完整的软判决增益，但接口对得上、实现最简单。

#### 4.2.2 核心流程

3 bit 软判决采用**符号-幅值（sign-magnitude）**约定：

- `bit[2]`（最高位）= 硬判决的数据位（0 或 1）；
- `bit[1:0]`（低两位）= 置信度幅值，`2'b11` 最大、`2'b00` 最小（相当于「未知」）。

于是映射规则是：

| 硬判决比特（deinterleave_out） | conv_in 取值 | 二进制 | bit[2] 数据位 | bit[1:0] 置信度 |
|---|---|---|---|---|
| 1 | 7 | `3'b111` | 1 | `11`（最大） |
| 0 | 3 | `3'b011` | 0 | `11`（最大） |

可以看到：`3'b111` 与 `3'b011` **只差最高位**，低两位恒为 `2'b11`。换言之，「填最大置信度」就是把低两位永远钉死在 `11`，让真正的比特信息只走最高位。

> 这与星座刻度 `CONS_SCALE_SHIFT = 10`（[common_defs.v:9](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_defs.v#L9)）是两套独立的刻度：后者管 demodulate 的判决门限（u3-l3），前者管 Viterbi 的软判决编码。本讲只关注后者。

#### 4.2.3 源码精读

软判决映射在一个 always 块里用三元运算符实现（[ofdm_decoder.v:143-153](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L143-L153)）：

```verilog
if (!flush) begin
    conv_in_stb <= deinterleave_out_strobe;          // 正常：跟解交织的节奏
    conv_in0 <= deinterleave_out[0] ? 3'b111 : 3'b011; // bit0 → 软判决
    conv_in1 <= deinterleave_out[1] ? 3'b111 : 3'b011; // bit1 → 软判决
    conv_erase <= erase;                              // 透传去穿孔标志
end else begin
    conv_in_stb <= 1;        // 冲刷：持续给有效
    conv_in0 <= 3'b011;      // 喂「确信的 0」
    conv_in1 <= 3'b011;
    conv_erase <= 0;
end
```

这段代码做了三件事，对应三个最小知识点：

1. **正常通路**（`!flush`）：`conv_in0` 取 `deinterleave_out[0]` 的软判决，`conv_in1` 取 `deinterleave_out[1]` 的软判决。`conv_in_stb` 直接复用 `deinterleave_out_strobe`，保证「解交织吐一对、Viterbi 吃一对」。
2. **冲刷通路**（`flush`，见 4.4）：持续喂 `3'b011`（确信的 0）并拉高 `conv_in_stb`，把 Viterbi 内部积压的比特顶出来。
3. **erase 透传**：`conv_erase` 直接接 deinterleave 的 `erase` 信号。`erase` 来自交织查找表项的 `null_a/null_b` 位（[deinterleave.v:14-16](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L14-L16)、[deinterleave.v:34-35](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L34-L35)），标记非 1/2 码率下被「去穿孔」补出来的空位（u3-l4）。被标 erase 的软值会被 Viterbi 当「未知」处理，而不是当成「确信的 0」——这一点很关键：**erase 用专门的标志位表达「未知」，而不是靠把置信度写成 `2'b00`**。

> 注意：`3'b011` 在 flush 通路里既是「确信的 0」，又在 erase 标志配合下可能表示「未知」。区别全在于 `conv_erase` 这一位，而不是软值本身的数值。这是初学者最容易混淆的地方。

#### 4.2.4 代码实践

**实践目标**：验证 `3'b111 / 3'b011` 的「最高位 = 数据、低两位 = 最大置信度」解读。

**操作步骤**：

1. 读 [ofdm_decoder.v:145-146](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L145-L146)，确认两个三元表达式只有「条件为真」时给 `3'b111`、为假时给 `3'b011`。
2. 把两个值写成二进制对比：`3'b111` 与 `3'b011`，确认仅 `bit[2]` 不同，`bit[1:0]` 同为 `2'b11`。
3. 思考：如果想让 Viterbi 得到「中等置信度」，应该把低两位改成什么？（答：例如 `2'b01`，即 `3'b101`/`3'b001`。）

**需要观察的现象**：无论 `deinterleave_out` 是 0 还是 1，低两位永远是 `11`。

**预期结果**：你能用一句话向同伴解释——「OpenOFDM 因为只有硬判决，所以把软判决的置信度永远钉死在最大，真正的比特信息只走最高位。」

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接把 `deinterleave_out[0]`（1 bit）零扩展成 `3'b001 / 3'b000` 喂给 Viterbi？

> **答案**：那样低两位会是 `2'b00` 或 `2'b01`，置信度很低甚至被当成「未知」。Viterbi 会因为「不够确信」而更倾向于依赖其他比特和卷积码约束关系来纠错，反而可能把本来正确的硬判决心信息丢掉。钉死在 `2'b11` 是在「没有真实置信度」时最合理的选择：告诉 Viterbi「这个比特我很确定，别改」。

**练习 2**：`conv_erase` 与软判决数值都能表达「不确定」，二者职责怎么分？

> **答案**：`conv_erase` 专门用于「去穿孔」补出来的空位（u3-l4 的 `null_a/null_b`），表示「这个位置根本没收到比特、是插进去的占位」；而软判决数值表达「收到了比特、但置信度有高低」。被 erase 的输入即使数值是 `3'b011`，Viterbi 也不会当成「确信的 0」，而是按未知处理。两条通道分离，语义清晰。

---

### 4.3 Viterbi IP 封装与 strobe 对齐

#### 4.3.1 概念说明

Xilinx `viterbi_v7_0` 是 coregen 生成的黑盒（[viterbi_v7_0.v:36-46](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/viterbi_v7_0.v#L36-L46)）。它的接口是「AXI 风格的 ce/rdy」而不是「数据 + strobe」：

- `clk`：时钟；
- `ce`（clock enable）：拉高时本拍吃一个输入；
- `sclr`（synchronous clear）：同步复位；
- `data_in0/data_in1/erase`：输入；
- `rdy`：输出有效脉冲；
- `data_out`：输出比特。

OpenOFDM 全项目统一用「数据 + strobe」，所以需要一个**适配层**把 `ce/rdy` 翻译成 strobe。`verilog/viterbi.v` 就是这个适配层的「教科书式」范本。

#### 4.3.2 核心流程

适配思路非常简单：

- **输入侧**：`ce = reset | (enable & input_strobe)`。即「复位时一直使能（清状态），否则只有在收到输入 strobe 的那一拍才吃数据」。这等价于把 input_strobe 当成 ce。
- **输出侧**：IP 的 `rdy` 直接当 `output_strobe`。

这样 `viterbi` 模块对外就变成了「`input_strobe` 进、`output_strobe` 出」的标准握手模块。

#### 4.3.3 源码精读

先看 `viterbi.v` 封装（[viterbi.v:5-31](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/viterbi.v#L5-L31)）：

```verilog
module viterbi ( ..., input input_strobe, output output_strobe );

viterbi_v7_0 viterbi_inst (
    .clk(clock),
    .ce(reset | (enable & input_strobe)),   // ★把 strobe 翻译成 ce
    .sclr(reset),
    .data_in0(sym0), .data_in1(sym1), .erase(erase),
    .rdy(output_strobe),                    // ★把 rdy 当 output_strobe
    .data_out(out_bit)
);
endmodule
```

文件开头的注释也点明了意图（[viterbi.v:1-4](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/viterbi.v#L1-L4)）：

```text
* A wrapper of Xilinx Viterbi IP core
* Added strobe signal.
```

**但是**：`ofdm_decoder.v` 并没有例化这个封装，而是直接例化了 IP 本身（[ofdm_decoder.v:81-90](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L81-L90)），并自己实现同样的 ce/rdy 翻译：

```verilog
wire vit_ce  = reset | (enable & conv_in_stb);   // 与 viterbi.v 的 ce 公式完全一致
wire vit_clr = reset;
wire vit_rdy;

viterbi_v7_0 viterbi_inst (
    .clk(clock), .ce(vit_ce), .sclr(vit_clr),
    .data_in0(conv_in0), .data_in1(conv_in1), .erase(conv_erase),
    .rdy(vit_rdy), .data_out(conv_decoder_out)
);

assign conv_decoder_out_stb = vit_ce & vit_rdy;   // ★输出 strobe = ce 与 rdy 同时有效
```

对比可见，`ofdm_decoder` 的 `vit_ce` 公式与 `viterbi.v` 的 `ce` 完全相同（[ofdm_decoder.v:39](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L39)）。输出 strobe 多了一个 `& vit_ce`（[ofdm_decoder.v:45](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L45)），进一步保证「只有在喂输入的拍上才认输出」。

> 为什么 `ofdm_decoder` 不直接复用 `viterbi.v`？因为它的输入 `conv_in0/conv_in1/conv_in_stb` 是由上面的 flush 控制逻辑（4.4）驱动的寄存器，需要把「正常通路」与「冲刷通路」先在一个 always 块里综合好，再喂给 IP。直接内联比「先封一层再接控制逻辑」更直观。`viterbi.v` 更像是作者留下的「如何给这个 IP 加 strobe」的独立示范，便于别处或日后复用。

#### 4.3.4 代码实践

**实践目标**：确认 `ofdm_decoder` 内联的 ce/rdy 逻辑与 `viterbi.v` 封装等价。

**操作步骤**：

1. 并排打开 [viterbi.v:20-29](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/viterbi.v#L20-L29) 与 [ofdm_decoder.v:39-45](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L39-L45)。
2. 比较两处 `ce` 的表达式：都是 `reset | (enable & <strobe>)`。
3. 比较输出 strobe：`viterbi.v` 直接用 `rdy`，`ofdm_decoder` 用 `vit_ce & vit_rdy`。思考多出的 `vit_ce` 在什么情况下会让二者不同。

**需要观察的现象**：当 `rdy` 在某个没有 `ce` 的拍上意外拉高时，`ofdm_decoder` 会因为 `& vit_ce` 把它过滤掉，而 `viterbi.v` 不会。这说明 `ofdm_decoder` 对「假输出」更保守。

**预期结果**：你能复述「`ce = reset | (enable & strobe)`、`output_strobe = ce & rdy`」这两条适配公式，并指出 `ofdm_decoder` 比封装多了一层 `& ce` 的过滤。

#### 4.3.5 小练习与答案

**练习 1**：`ce` 公式里为什么要有 `reset |`？去掉会怎样？

> **答案**：复位时需要让 IP 的 `sclr` 与 `ce` 同时有效，确保内部状态被清干净。如果复位时 `ce=0`，`sclr` 可能因为 IP 内部没有时钟使能而不生效，导致残留旧状态。`reset |` 保证复位期间 IP 一直被「使能 + 清零」。

**练习 2**：`viterbi_v7_0.v` 是一个 2.2 MB 的文件（[viterbi_v7_0.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/viterbi_v7_0.v)），为什么要随仓库提交这么大的文件？

> **答案**：它是 coregen 用 netgen 导出的**仿真行为模型**（文件头明确写「verification model」，见 [viterbi_v7_0.v:22-28](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/viterbi_v7_0.v#L22-L28)），由大量 `sig00000xxx` 内部线网拼出，功能正确但不可综合。提交它是为了在**没有 Xilinx 综合工具链**时也能用 iverilog 跑仿真（u1-l2、u6-l3）。综合时则改用配套的 `.ngc` 网表和授权 IP。

---

### 4.4 flush、num_bits_to_decode 与 do_descramble

#### 4.4.1 概念说明

Viterbi 有回溯延迟（traceback depth）。输入最后一个有效比特后，译码器内部还积压着若干判决没输出。要把它们顶出来，就得继续喂输入——这就是 **flush**。

在 OpenOFDM 里，需要 flush 的恰恰是 **SIGNAL 与 HT-SIG** 这两类控制字段：

- 它们是卷积编码的（要走 Viterbi），但**不扰码**（`do_descramble = 0`）。
- 它们的比特数是固定的、已知的（SIGNAL 净荷 24 bit → 编码 48 bit；HT-SIG 净荷 48 bit → 编码 96 bit）。
- 解完就结束，没有「后续数据」自然把它们顶出来，所以必须主动 flush。

而 DATA 字段（`do_descramble = 1`）后面通常还有更多符号，数据流会持续把 Viterbi 的输出顶出来，且顶层用 `byte_count`/`pkt_len` 控制收尾，所以不走这里的 flush 分支。

#### 4.4.2 核心流程

`dot11.v` 在不同阶段写入不同的指令（见下表，来源 [dot11.v:518-519](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L518-L519)、[dot11.v:588-589](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L588-L589)、[dot11.v:622-623](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L622-L623)、[dot11.v:628-629](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L628-L629)）：

| 阶段 | `do_descramble` | `num_bits_to_decode` | 是否 flush |
|---|---|---|---|
| legacy SIGNAL | 0 | 48 | 是（解完 48 bit 后补 0） |
| HT-SIG | 0 | 96 | 是（解完 96 bit 后补 0） |
| legacy DATA | 1 | `(legacy_len+3)<<4` | 否（靠后续符号/顶层 byte_count 收尾） |
| HT DATA | 1 | `(ht_len+3)<<4` | 否 |

> `(legacy_len+3)<<4` 的含义：`legacy_len` 是字节数，`+3` 对应 service(2 字节) + tail/补齐(1 字节)，`<<4` = `×16` = `×8（比特/字节）×2（1/2 码率）`，即「需要解码的编码后比特数」。

`ofdm_decoder` 内部用 `deinter_out_count` 数已经收到的解交织比特，达到 `num_bits_to_decode` 且当前没有新输入时，把 `flush` 拉高，进入「持续喂确信的 0」状态。

#### 4.4.3 源码精读

flush 的触发与执行都在主 always 块里（[ofdm_decoder.v:132-153](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L132-L153)）：

```verilog
if (deinterleave_out_strobe) begin
    deinter_out_count <= deinter_out_count + 2;     // 每拍进 2 bit
end else begin
    // 只有「不扰码」且「已收够」才 flush
    if (~do_descramble && deinter_out_count >= num_bits_to_decode) begin
        flush <= 1;
    end
end
if (!flush) begin
    conv_in_stb <= deinterleave_out_strobe;          // 正常通路（见 4.2）
    conv_in0   <= deinterleave_out[0] ? 3'b111 : 3'b011;
    conv_in1   <= deinterleave_out[1] ? 3'b111 : 3'b011;
    conv_erase <= erase;
end else begin
    conv_in_stb <= 1;        // 持续有效
    conv_in0   <= 3'b011;    // 确信的 0（卷积码尾比特为零终止）
    conv_in1   <= 3'b011;
    conv_erase <= 0;
end
```

逻辑读法：

1. `deinter_out_count` 每收到一对解交织输出（`deinterleave_out_strobe` 为 1）就 `+2`（因为每拍 2 bit）。
2. 当某一拍**没有**新输出（`else` 分支），说明解交织这一符号已经吐完；此时若 `do_descramble==0`（SIGNAL/HT-SIG）且收够 `num_bits_to_decode` 个比特，就把 `flush` 置 1。
3. `flush` 一旦为 1，后续每拍都给 Viterbi 喂「`conv_in_stb=1`、`conv_in0=conv_in1=3'b011`、`conv_erase=0`」，即「确信的 0」。这正好对应卷积码零终止的尾比特，能把 Viterbi 内部积压的判决一个个顶出来，直到顶层状态机切走（`ofdm_reset` 复位）。

> 注意 flush 只在 `~do_descramble` 时触发。这是因为只有 SIGNAL/HT-SIG 才满足「固定长度 + 不扰码 + 解完即停」三个条件。DATA 字段虽然也卷积编码，但它的收尾由顶层 `byte_count >= pkt_len` 控制（u4-l5 的 FCS 比对），不需要这里 flush。

再看输出到字节那一段（[ofdm_decoder.v:155-172](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L155-L172)），它解释了 `do_descramble` 的另一个作用——「要不要绕过解扰」：

```verilog
if (deinter_out_count > 0) begin
    if (~do_descramble) begin
        bit_in     <= conv_decoder_out;             // SIGNAL/HT-SIG：直接成字节
        bit_in_stb <= conv_decoder_out_stb;
    end else begin
        bit_in <= descramble_out;                   // DATA：先解扰再成字节
        if (descramble_out_strobe) begin
            if (skip_bit > 0) begin                 // 跳过 9 bit service
                skip_bit <= skip_bit - 1;
                bit_in_stb <= 0;
            end else begin
                bit_in_stb <= 1;
            end
        end
    end
end
```

- `do_descramble = 0`：Viterbi 输出直接送 `bits_to_bytes`（SIGNAL/HT-SIG 没扰码，无需解扰）。
- `do_descramble = 1`：Viterbi 输出先经 `descramble`，解扰后的比特再送 `bits_to_bytes`，并跳过前 9 bit（service 字段，详见 u3-l6）。

`skip_bit` 在复位时初始化为 9（[ofdm_decoder.v:126-127](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L126-L127)）：

```verilog
// skip the first 9 bits of descramble out (service bits)
skip_bit <= 9;
```

> 这里的 9 而不是 7，是因为 `descramble` 模块自身会先用前 7 个输入比特初始化 LFSR（u3-l6），这 7 个比特不会出现在 `descramble_out` 上；`descramble_out` 上最早出现的 9 个有效比特对应 SERVICE 字段的 9 个保留位（bits 7–15），跳过它们之后才是真正的 MPDU 数据。详细推导见 u3-l6。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：把本讲的两个核心问题——「`conv_in0/conv_in1` 为什么是 `3'b111 / 3'b011`」与「`do_descramble=0` 时 flush 如何补 0」——在源码与波形上验证一遍。

**操作步骤**：

1. **解读软判决**。打开 [ofdm_decoder.v:145-146](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L145-L146)，写下：当 `deinterleave_out[0]=1` 时 `conv_in0=3'b111`，`=0` 时 `conv_in0=3'b011`。对照 4.2.2 的表格，确认「最高位 = 数据、低两位 = 最大置信度」。
2. **追踪 flush 触发**。从 [dot11.v:518-519](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L518-L519) 看 SIGNAL 阶段 `do_descramble=0`、`num_bits_to_decode=48`；再到 [ofdm_decoder.v:139-141](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L139-L141) 确认：解交织吐完、`deinter_out_count >= 48` 时 `flush` 被拉高。
3. **追踪 flush 执行**。在 [ofdm_decoder.v:148-153](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L148-L153) 确认 `flush=1` 后持续喂 `3'b011` + `conv_in_stb=1`，把 Viterbi 的剩余判决顶出来。
4. **仿真验证（可选，需本地环境）**。在 `verilog/` 目录跑默认的 24Mbps dot11a 样本（`make simulate`），用 gtkwave 打开 `dot11.vcd`，在 SIGNAL 阶段（`dot11_state == S_DECODE_SIGNAL` 附近）观察 `conv_in_stb`、`conv_in0`、`conv_in1`、`flush` 这几个信号。

**需要观察的现象**：

- 在 SIGNAL 解码的前 48 个编码比特期间，`conv_in0/conv_in1` 随 `deinterleave_out` 在 `3'b111`/`3'b011` 之间跳变，`conv_in_stb` 与 `deinterleave_out_strobe` 同步。
- 当 `deinter_out_count` 达到 48 后，`flush` 拉高，随后若干拍 `conv_in0`、`conv_in1` 恒为 `3'b011`、`conv_in_stb` 恒为 1，直到顶层 `ofdm_reset` 把 `ofdm_decoder` 复位（`flush` 随之清 0）。

**预期结果**：你能用一段话回答——「SIGNAL/HT-SIG 因为不扰码且长度固定，解完 `num_bits_to_decode` 个比特后，`ofdm_decoder` 拉高 `flush`，持续给 Viterbi 喂确信的 0（`3'b011`）作为卷积码的尾比特，把回溯延迟里积压的剩余判决顶出来，直到顶层切走并复位。」

> 若无法本地运行仿真，上述对源码逻辑的解读即为「源码阅读型实践」的结论，波形部分标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `ofdm_decoder.v:139` 的 `~do_descramble` 条件去掉，让 DATA 解码也触发 flush，会发生什么？

> **答案**：DATA 字段会过早进入 flush，持续喂 0 给 Viterbi，把真正的后续数据比特污染成 0，导致解码错误。flush 是专门为「固定长度、不扰码、解完即停」的 SIGNAL/HT-SIG 设计的，DATA 靠顶层 `byte_count` 收尾，不能走这条路。

**练习 2**：flush 时喂的是 `3'b011`（确信的 0），为什么不喂 `3'b111`（确信的 1）或带 erase 的「未知」？

> **答案**：802.11 卷积码采用**零终止**——编码器在每个帧/字段末尾追加 K-1=6 个 0 把编码器归零。所以尾比特就是 0，喂「确信的 0」最符合真实情况，Viterbi 会据此正确收尾。喂 1 会与真实尾比特矛盾、可能引入错误；喂「未知」（带 erase）则放弃了已知的尾比特信息，收敛更慢。

**练习 3**：`deinter_out_count` 为什么每拍 `+2` 而不是 `+1`？

> **答案**：`deinterleave` 每拍输出 2 个比特（`out_bits[1:0]`，见 [deinterleave.v:14](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L14)），对应 1/2 母码「每次喂一对编码比特」的节奏。所以计数按对累加，`+2`，这样 `deinter_out_count` 直接等于「已收到的编码比特数」，与 `num_bits_to_decode`（也是编码比特数）口径一致。

---

## 5. 综合实践

把本讲所有知识点串成一个端到端的「源码追踪」任务。

**任务**：以一个 24Mbps dot11a 样本为对象，从 `ofdm_decoder_inst` 的输入追到输出，画出一张「时间轴 + 信号」表，证明你理解了 5 级子流水线与三个控制信号。

**操作步骤**：

1. **入口**：在 [dot11.v:356-381](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L356-L381) 找到 `ofdm_decoder_inst` 的例化，确认 `sample_in` 来自 `equalizer_out`、`sample_in_strobe` 来自 `ofdm_in_stb`（后者绑定 `equalizer_out_strobe`）。
2. **指令**：在 SIGNAL 阶段（[dot11.v:518-519](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L518-L519)）记录 `do_descramble=0`、`num_bits_to_decode=48`；在 DATA 阶段（[dot11.v:588-589](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L588-L589)）记录 `do_descramble=1`、`num_bits_to_decode=(legacy_len+3)<<4`。
3. **五级追踪**：沿着 [ofdm_decoder.v:54-116](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L54-L116) 的 5 个实例，写出每级的「输入 strobe 来源 → 输出位宽 → 输出 strobe 去向」。
4. **软判决与 flush**：在 SIGNAL 阶段定位 `conv_in0/conv_in1` 由 `3'b111/3'b011` 切到全 `3'b011`（flush）的时刻；在 DATA 阶段定位 `bit_in` 走 `descramble_out` 分支、`skip_bit` 从 9 递减到 0 的过程。
5. **交叉验证（可选）**：运行 `scripts/test.py`（u5-l2）对该样本做逐阶段比对，确认 `conv_decoder_out`、`descramble_out`、`byte_out` 与 Python 参考一致。

**产出**：一张表格，包含「阶段（SIGNAL / DATA）× 信号（rate、do_descramble、num_bits_to_decode、conv_in 取值、flush、skip_bit）× 观察到的行为」。这张表既是对本讲的总结，也是后续学习 u3-l6（解扰）、u4（状态机）、u5（交叉验证）的导航图。

> 第 5 步若无法本地运行，标注「待本地验证」，前 4 步的源码追踪结论仍然成立。

## 6. 本讲小结

- `ofdm_decoder` 是一条 5 级子流水线（demodulate → deinterleave → viterbi → descramble → bits_to_bytes），位宽曲线「32 → 6 → 2 → 1 → 1 → 8」：先收窄、最后重新打包成字节。
- 软判决采用「最高位 = 数据、低两位 = 置信度」的符号-幅值约定；`conv_in0/conv_in1` 写成 `3'b111 / 3'b011` 表示「比特 1/0 + 最大置信度」，因为 demodulate 只给硬判决。
- `erase` 是独立的「去穿孔空位」标志，与软判决数值分离——被 erase 的输入即使值是 `3'b011` 也按「未知」处理。
- Xilinx `viterbi_v7_0` 没有 strobe，靠 `ce = reset | (enable & strobe)`、`output_strobe = ce & rdy` 翻译成「数据 + strobe」风格；`viterbi.v` 是这个翻译的独立封装，但 `ofdm_decoder` 选择内联。
- `flush` 专为 SIGNAL / HT-SIG（`do_descramble=0`、固定长度）服务：解够 `num_bits_to_decode` 个比特后持续喂「确信的 0」（卷积码零终止尾比特），把 Viterbi 回溯延迟里的剩余判决顶出来。
- `do_descramble` 同时决定「要不要解扰」与「要不要走 flush 分支」：SIGNAL/HT-SIG 直通成字节，DATA 先解扰并跳过 9 bit service。

## 7. 下一步学习建议

- **u3-l6 解扰与串并转换**：本讲反复提到的 `skip_bit=9` 与 service 字段、LFSR 初始化，都在 `descramble.v` 与 `bits_to_bytes.v` 里。建议接着读 u3-l6，把 802.11 扰码生成多项式 \(S(x)=x^7+x^4+1\) 与「前 7 bit 初始化」串起来。
- **u4-l1 / u4-l2 顶层状态机与 SIGNAL 解析**：本讲的 `do_descramble`、`num_bits_to_decode` 都来自 `dot11.v` 状态机。读 u4-l1/l2 可以看清这些指令在 `S_DECODE_SIGNAL`、`S_CHECK_SIGNAL`、`S_DECODE_DATA` 之间是如何被写入与复位的。
- **u5-l2 交叉验证框架**：本讲把 `demod_out`、`deinterleave_out`、`conv_decoder_out`、`descramble_out`、`byte_out` 都暴露成端口，正是为了 `test.py` 逐阶段比对。读完 u5-l2 你就能用 Python 参考解码器反过来验证本讲的软判决与 flush 逻辑是否正确。
- **源码延伸**：若想理解 Viterbi IP 的内部参数（约束长度、回溯深度、软判决宽度如何配置），可翻阅 `verilog/coregen/` 下 viterbi 的 `.xco` 配置（若存在）与 u6-l3 的 coregen 依赖说明。
