# 解交织 deinterleave.v

## 1. 本讲目标

上一讲（u3-l3）我们把均衡后的复数星座点判决成了比特：`demodulate.v` 每个 OFDM 符号送出一串最多 6 位的 `in_bits`。但这些比特的顺序并不是卷积码编码器原始吐出的顺序——发射机在调制前做过**比特交织**，本讲要做的就是把这次交织**还原**回去。

学完本讲，你应当能够：

- 说清「交织/解交织」到底在打乱什么、为什么要打乱、为什么必须在 RAM 里缓存一整个符号才能还原；
- 读懂 [`deinterleave.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v) 的三态状态机 `S_INPUT → S_GET_BASE → S_OUTPUT`，并能解释 `lut_key` 如何由 `{ht, rate[3:0]}` 构成；
- 理解 [`ram_2port.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/ram_2port.v) 双口 RAM「A 口写、B 口读」的重排缓冲用法；
- 看懂 [`deinter_lut`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/deinter_lut.v) 这张 22 位宽查找表的两级查表结构，以及 `erase` 信号如何在 3/4、2/3、5/6 码率下做**去穿孔（de-puncture）**。

本讲是八步解码流水线（u1-l5）中「频域复数 → 比特」之后的第二步，承接 u3-l3 解调、连接 u3-l5 的 Viterbi 卷积解码。

## 2. 前置知识

阅读本讲前，你需要大致了解以下概念（不熟悉的可先看 u3-l1 信道均衡与 u3-l3 解调）：

- **OFDM 符号与子载波**：20MHz 信道被切成 64 个子载波，其中 legacy（802.11a/g）有 48 个数据子载波、HT（802.11n 20MHz 单流）有 52 个数据子载波。每个数据子载波在一个 OFDM 符号里承载 \(N_{BPSC}\) 个比特（BPSK=1、QPSK=2、16-QAM=4、64-QAM=6）。
- **\(N_{CBPS}\)（coded bits per symbol）**：一个 OFDM 符号里的**编码后**比特总数，等于「数据子载波数 × \(N_{BPSC}\)」。例如 802.11a 24Mbps 是 16-QAM，\(N_{CBPS}=48\times4=192\)。
- **卷积码与码率**：发射端用 1/2 卷积码编码（每 1 个数据比特变 2 个编码比特），再用**穿孔（puncturing）**删掉一部分编码比特，从而得到 2/3、3/4 等更高码率。接收端要在送入 Viterbi 之前把被删掉的比特「补回来」（标成未知/erase）。
- **「数据 + strobe」握手**：全项目的统一风格——数据线旁配一根 strobe，strobe 为高的那一拍数据才有效，模块之间不反压。本讲的 `input_strobe`/`output_strobe` 都遵循此约定（见 u1-l4、u3-l2）。
- **双口 RAM**：一块存储器有两个独立端口，可同时（不同地址）读写。本讲用它实现「一边写入、另一边按重排顺序读出」。

> 一个关键直觉：解交织是一个**符号内的位置重排（permutation）**。要还原原始顺序，你必须先把这个符号的全部 \(N_{CBPS}\) 个比特都收齐、存好，然后才能按原始顺序把它们读出来。这就是为什么本模块中心是一块 RAM。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [verilog/deinterleave.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v) | 解交织主模块：三态状态机，调度「写满一个符号 → 查表读出」 |
| [verilog/usrp2/ram_2port.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/ram_2port.v) | 平台胶水模块：参数化双口 RAM，作重排缓冲 |
| [verilog/coregen/deinter_lut.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/deinter_lut.v) | Xilinx Block Memory IP 封装的只读查找表，存放所有码率/MCS 的重排索引与穿孔标记 |
| [scripts/gen_deinter_lut.py](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py) | 离线生成 `deinter_lut.mif/.coe` 的 Python 脚本，把复杂数学预计算成查表 |
| [verilog/delayT.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/delayT.v) | 固定延时链，用于把 LUT 输出与 RAM 读出延时对齐（见 u3-l2） |
| [verilog/ofdm_decoder.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v) | 上层：例化 `deinterleave`，把它的 `out_bits`/`erase` 喂给 Viterbi |
| [docs/source/decode.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/decode.rst) | 官方文档：交织/解交织的数学定义与查表思路 |

---

## 4. 核心概念与源码讲解

### 4.1 deinterleave 模块：符号级重排与三态状态机

#### 4.1.1 概念说明

**交织（interleaving）是什么、为什么要有它？**

在发射机里，卷积码编码器输出的比特流，在映射到子载波星座点之前，会被先「打乱」一次。这次打乱有两个目的（见 [decode.rst:101-122](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/decode.rst#L101-L122)）：

1. **频率分集**：让原本相邻的编码比特分散到不相邻的子载波上。这样当某个子载波深衰落时，丢掉的是「不相邻」的若干编码比特，Viterbi 仍有可能纠正，而不是连丢一串。
2. **可靠性均衡**：让相邻编码比特轮流落在星座点的「高位/低位」（I 轴与 Q 轴、最高有效位与最低有效位），避免连续比特都落在容易判错的位上。

**解交织就是这次打乱的逆运算**：把收到的、按子载波顺序排列的 \(N_{CBPS}\) 个比特，还原成卷积码编码器当年的原始顺序，好让下游 Viterbi 解码。

**为什么必须用 RAM 缓存一整个符号？** 因为这是一个符号内的**全局重排**——输出第 0 个比特可能来自输入的第 137 个比特，输出第 1 个比特可能来自输入的第 5 个比特……在读出任何一个比特之前，你无法知道它来自哪里，除非整个符号的比特都已经在手上了。所以策略必然是：**先把一个符号的全部比特写进缓冲，再按查表给出的原始顺序逐个读出**。这就解释了模块中心那块双口 RAM 的存在。

#### 4.1.2 核心流程

`deinterleave` 对**每一个 OFDM 符号**循环执行三步（对应三个状态）：

```
            ┌─────────────────────────────────────────────────┐
            │  S_INPUT：把 demod 送来的 Ncbps 个 6-bit 字      │
            │          逐拍写入 RAM（A 口），写满一个符号       │
            └──────────────────────┬──────────────────────────┘
                                   │ addra 走完一个符号（命中 half-1）
                                   ▼
            ┌─────────────────────────────────────────────────┐
            │  S_GET_BASE：用 {ht, rate[3:0]} 查 LUT 表头，     │
            │              得到「当前码率子表」的起始地址        │
            └──────────────────────┬──────────────────────────┘
                                   │ lut_key <= 起始地址
                                   ▼
            ┌─────────────────────────────────────────────────┐
            │  S_OUTPUT：从起始地址开始顺序读 LUT，每条表项告诉 │
            │            「下一个该读 RAM 哪个地址的哪个比特」；│
            │            每拍输出 2 个解交织后比特 + 2 个 erase │
            │            读到 done 标志 → 回到 S_INPUT 等下一符号│
            └─────────────────────────────────────────────────┘
```

注意三个关键设计点：

- **每拍输出 2 个比特**：因为上游 Viterbi 每个 `ce` 周期吃 2 个软判决比特（`data_in0/data_in1`，见 u3-l5）。所以 LUT 每条表项一次给出 **两个** 比特的来源（`addra/bita` 与 `addrb/bitb`）。
- **重排顺序不在硬件里算，而是查表**：802.11 的重排公式涉及除法与取模（见 4.1.2 的公式），在 FPGA 里逐拍算代价高。OpenOFDM 把所有码率/MCS 的重排映射**离线**算好，烤进一张 ROM（`deinter_lut`），硬件只做「给地址、取数据」。
- **lut_key 是两级查表**：先用 `{ht, rate[3:0]}` 在表头查到「子表起始地址」，再从起始地址顺序往下读。下面 4.3 会展开。

#### 4.1.3 源码精读

**端口**：输入是上一级 `demodulate` 的 `rate(8)`、`in_bits(6)`、`input_strobe`；输出是给 Viterbi 的 `out_bits(2)`、`erase(2)`、`output_strobe`。

[deinterleave.v:4-17](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L4-L17) 定义了这组端口：

```verilog
module deinterleave(
    input clock, input reset, input enable,
    input [7:0] rate,
    input [5:0] in_bits,
    input input_strobe,
    output [1:0] out_bits,
    output [1:0] erase,
    output output_strobe
);
```

**legacy 还是 HT？** 用 `rate` 字段的最高位区分，并据此决定数据子载波数：

[deinterleave.v:19-22](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L19-L22)

```verilog
wire ht = rate[7];                       // rate[7]=1 表示 HT(802.11n)
wire [5:0] num_data_carrier = ht? 52: 48;   // HT 52、legacy 48
wire [5:0] half_data_carrier = ht? 26: 24;  // = num_data_carrier/2
```

这正是本讲标题里「区分 legacy（48）与 HT（52）的 \(N_{CBPS}\) 影响」的来源：缓冲深度、触发阈值都随 `ht` 切换。

**三态状态机的核心**：

[deinterleave.v:84-86](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L84-L86) 定义状态码；

[deinterleave.v:108-123](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L108-L123) 是 `S_INPUT`：

```verilog
S_INPUT: begin
    if (input_strobe) begin
        if (addra == half_data_carrier-1) begin
            lut_key <= {6'b0, ht, rate[3:0]};   // ← 用 {ht,rate[3:0]} 当 key 查表头
            ram_delay <= 0;
            lut_valid <= 0;
            state <= S_GET_BASE;                 // 写满一个符号，转去查表
        end else begin
            if (addra == num_data_carrier-1) addra <= 0;  // 地址回绕
            else addra <= addra + 1;                       // 写指针 +1
        end
    end
end
```

这里 A 口的写使能 `.wea(input_strobe)`（见 4.3.3 的 RAM 例化）意味着：**每个 `input_strobe` 都把当前 `in_bits` 写进 `ram[addra]`，同时 `addra` 递增**。当写指针走完一个符号（命中 `half_data_carrier-1`），就带着 `{ht, rate[3:0]}` 这个 key 进入 `S_GET_BASE`。

[deinterleave.v:125-133](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L125-L133) 是 `S_GET_BASE`：它用上一拍查到的 `lut_out`（表头里存的就是「子表起始地址」）作为新的 `lut_key`，并经过一拍 `ram_delay` 握手：

```verilog
S_GET_BASE: begin
    if (ram_delay) begin
        lut_key <= lut_out;        // 表头值 = 子表起始地址，作为新 key
        ram_delay <= 0;
        state <= S_OUTPUT;
    end else begin
        ram_delay <= 1;            // 等一拍，让 LUT 读出生效
    end
end
```

[deinterleave.v:135-152](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L135-L152) 是 `S_OUTPUT`：每拍把 LUT 给出的 `lut_addra/lut_addrb` 送给 RAM 地址线、`lut_key+1` 读下一条表项；读到 `lut_done` 就回 `S_INPUT` 等下一个符号：

```verilog
S_OUTPUT: begin
    if (ram_delay) begin
        addra <= lut_addra;        // RAM A 口读地址
        addrb <= lut_addrb;        // RAM B 口读地址
        if (lut_done) begin        // 当前码率子表读完
            lut_key <= 0; lut_valid <= 0; state <= S_INPUT;
        end else begin
            lut_valid <= 1; lut_key <= lut_key + 1;  // 顺序读下一条表项
        end
    end else begin
        ram_delay <= 1; lut_valid <= 1; lut_key <= lut_key + 1;
    end
end
```

**输出与 erase 的组合逻辑**：

[deinterleave.v:34-49](https://github.com/open-sdr/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L34-L49)

```verilog
assign erase[0] = lut_out_delayed[21];   // null_a：当前 bit_a 是否为穿孔补的空
assign erase[1] = lut_out_delayed[20];   // null_b：当前 bit_b 是否为穿孔补的空
wire [2:0] lut_bita = lut_out_delayed[7:5];   // 从 6-bit 字里选哪一位
wire [2:0] lut_bitb = lut_out_delayed[4:2];
wire [5:0] lut_addra = lut_out_delayed[19:14]; // RAM 地址 a
wire [5:0] lut_addrb = lut_out_delayed[13:8];  // RAM 地址 b
wire lut_done  = lut_out_delayed[0];           // 子表结束标志
assign out_bits[0] = lut_valid_delayed? bit_outa[lut_bita]: 0; // RAM[a] 的第 bita 位
assign out_bits[1] = lut_valid_delayed? bit_outb[lut_bitb]: 0; // RAM[b] 的第 bitb 位
assign output_strobe = enable & lut_valid_delayed & lut_out_delayed[1]; // out_stb
```

可以看到 LUT 一条 22 位表项的位域含义（与 4.3 节脚本里的格式完全对应）。

#### 4.1.4 代码实践

> 本实践对应大纲里的练习任务：**追踪 `S_INPUT→S_GET_BASE→S_OUTPUT`，说明 `lut_key` 如何由 `{ht, rate[3:0]}` 构成、`erase` 何时置位、为何需要 RAM 缓存一个完整符号。**

**实践目标**：用源码阅读 + 仿真观察，亲手把上面三个问题坐实。

**操作步骤**：

1. 打开 [deinterleave.v:107-155](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L107-L155)，在 `case(state)` 三个分支各加一行 `$display` 调试注释（仅用于阅读理解，**不要改动逻辑**），在心里标注每个状态负责什么。
2. 回答 `lut_key` 的构成：在 `S_INPUT` 末尾 [deinterleave.v:111](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L111)，`lut_key <= {6'b0, ht, rate[3:0]}`。这是一个 11 位 key：最高有效位是 `ht`（= `rate[7]`），低 4 位是 `rate[3:0]`。它去查 LUT 的**表头**（前 32 项），表头里存的是「当前 rate/MCS 对应子表」的起始地址。把 `rate` 字段的取值与表头索引对应起来：
   - legacy：`rate[3:0]` 就是 SIGNAL 字段里的 4 位速率码（如 6Mbps=`1011`=11、24Mbps=`1001`=9）；
   - HT：`{1, mcs[3:0]}` = `16 + MCS`（MCS0..7 → 索引 16..23）。
   你可以对照 [gen_deinter_lut.py:53-84](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L53-L84) 的 `RATE_BITS` 与 `RATES` 表核对。
3. 回答 `erase` 何时置位：[deinterleave.v:34-35](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L34-L35) 里 `erase[0]/erase[1]` 直接取自表项的 `null_a/null_b` 位。这两个位**只在非 1/2 码率**时被脚本置 1——因为 3/4、2/3、5/6 码率在发射端删过比特，解交织时要把这些「被删位置」补成空比特并标 erase，好让 Viterbi 当成「未知」处理。详见 4.3.4。
4. 回答「为何需要 RAM 缓存一个完整符号」：因为解交织是符号内全局重排，输出比特来自符号内任意位置，必须等整个符号的 \(N_{CBPS}\) 个比特都进缓冲后才能按原始顺序读出。这正是 `S_INPUT`（写满）→ `S_OUTPUT`（读出）两段式的根本原因。
5. （可选仿真）在 `verilog/` 下 `make compile && make simulate`，用 gtkwave 打开 `dot11.vcd`，把 `dot11_inst.decode_inst.deinterleave_inst.state`、`addra`、`lut_key`、`erase`、`output_strobe` 拉到同一窗口，观察一个 24Mbps 符号：`state` 从 0→1→2 走一遍、`addra` 在 `S_INPUT` 期间递增、`S_OUTPUT` 期间 `output_strobe` 周期性拉高。

**需要观察的现象**：

- `state` 在每个 OFDM 符号上完成一次 `0→1→2→0` 循环；
- `S_INPUT` 期间 `input_strobe` 的个数 = \(N_{CBPS}/N_{BPSC}\)（即数据子载波数，24Mbps 为 48）；
- `S_OUTPUT` 期间 `output_strobe` 的个数 × 2 ≈ \(N_{CBPS}\)（3/4 码率会因补空比特而略多）。

**预期结果**：你能用一句话复述三态机职责，并能解释 `erase` 与 RAM 缓存的必要性。

> 待本地验证：上面「`output_strobe` 个数 × 2 ≈ \(N_{CBPS}\)」的精确计数，建议在你本地样本上实测确认（不同码率下穿孔补空的数量不同）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `ht` 接反（把 legacy 包当成 HT 处理），`num_data_carrier` 会变成多少？对 `S_INPUT` 的写满判定有什么影响？

**参考答案**：`num_data_carrier` 会从 48 变成 52、`half_data_carrier` 从 24 变成 26。于是 `S_INPUT` 会多等 4 个 `input_strobe` 才命中 `half_data_carrier-1`（25）而转态；同时 LUT 的 key 也会指向 HT 子表，地址与码率全部错位，导致整符号解交织错乱、Viterbi 输出乱码、最终 `fcs_ok` 为 0。

**练习 2**：为什么 `output_strobe` 的表达式里要乘 `lut_out_delayed[1]`（即表项里的 `out_stb` 位），而不是只要 `lut_valid_delayed` 就发 strobe？

**参考答案**：因为 `lut_valid_delayed` 只表示「正在输出某个符号的有效阶段」，但 LUT 表项中存在 `out_stb=0` 的行（如子表末尾的复位/结束行，其 `done=1`、`out_stb=0`）。这些行不应产生输出 strobe。用表项自带的 `out_stb` 位精确控制「这一拍到底要不要输出」，避免把结束行误当成数据发给 Viterbi。

---

### 4.2 ram_2port：双口 RAM 重排缓冲

#### 4.2.1 概念说明

`ram_2port` 是 USRP 平台带来的一个**参数化双口 RAM 胶水模块**（见 u1-l3 的平台代码分类）。它本身不含任何 OFDM 知识，纯粹是一块「两个独立端口的存储器」。`deinterleave` 借助它的「A 口写、B 口读」能力，把「写入顺序」和「读出顺序」解耦：

- **A 口**负责在 `S_INPUT` 阶段，按样本到达顺序把每个 6-bit 字写进去（`wea=input_strobe`）；
- **B 口**负责在 `S_OUTPUT` 阶段，按 LUT 给出的重排地址读出来；
- A 口的 `doa` 也被复用为「读 a」输出，因为 `S_OUTPUT` 一次要读两个地址（`addra` 和 `addrb`），正好一个端口读一个。

之所以用**双口**而不是单口：解交织读出时**每拍要同时取两个不同地址**的比特（`bit_a` 和 `bit_b`），双口 RAM 的两套地址线正好满足，无需分时。

#### 4.2.2 核心流程

`ram_2port` 的两套端口完全对称、各自独立：

```
A 口（clka, addra, dia, wea, doa）：S_INPUT 时 wea=input_strobe 写；S_OUTPUT 时读出 doa=bit_outa
B 口（clkb, addrb, dib, web, dob）：web 恒为 0，只读；S_OUTPUT 时读出 dob=bit_outb
```

两个端口的读操作都带**一级寄存器输出**（`doa <= ram[addra]`），所以「给地址」到「拿到数据」之间有 **1 拍** 延时。这正是 `deinterleave` 里那根 `delayT #(.DELAY(2))` 延时线存在的理由——把 LUT 给出的位选择信号（`bita/bitb`、`erase`）往后延，使其与 RAM 读出的数据对齐（见 4.2.3 与 4.1.3 的 `delay_inst`）。

#### 4.2.3 源码精读

[ram_2port.v:20-39](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/ram_2port.v#L20-L39) 是参数化的端口声明，`DWIDTH`（数据位宽）和 `AWIDTH`（地址位宽）都可调：

```verilog
module ram_2port #(
    parameter DWIDTH=32, parameter AWIDTH=9
)(
    input clka, input ena, input wea,
    input  [AWIDTH-1:0] addra,
    input  [DWIDTH-1:0] dia,
    output reg [DWIDTH-1:0] doa,
    input clkb, input enb, input web,
    input  [AWIDTH-1:0] addrb,
    input  [DWIDTH-1:0] dib,
    output reg [DWIDTH-1:0] dob
);
```

[ram_2port.v:41-48](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/ram_2port.v#L41-L48) 声明存储体并上电清零：

```verilog
reg [DWIDTH-1:0] ram [(1<<AWIDTH)-1:0];   // 深度 = 2^AWIDTH
integer i;
initial begin
    for(i=0;i<(1<<AWIDTH);i=i+1) ram[i] <= {DWIDTH{1'b0}};
    doa <= 0; dob <= 0;
end
```

[ram_2port.v:50-65](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/ram_2port.v#L50-L65) 是两个端口的读写逻辑（完全对称）：

```verilog
always @(posedge clka) begin
    if (ena) begin
        if (wea) ram[addra] <= dia;   // 写优先：同址同拍先写
        doa <= ram[addra];            // 读出（带一级寄存）
    end
end
always @(posedge clkb) begin
    if (enb) begin
        if (web) ram[addrb] <= dib;
        dob <= ram[addrb];
    end
end
```

在 `deinterleave` 里它被例化成 6 位宽、6 位地址（深度 64，够装 52 个 HT 子载波）：

[deinterleave.v:54-67](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L54-L67)

```verilog
ram_2port #(.DWIDTH(6), .AWIDTH(6)) ram_inst (
    .clka(clock), .ena(1), .wea(input_strobe),   // A 口：随 input_strobe 写
    .addra(addra), .dia(in_bits), .doa(bit_outa),
    .clkb(clock), .enb(1), .web(0),              // B 口：只读
    .addrb(addrb), .dib(32'hFFFF), .dob(bit_outb)
);
```

#### 4.2.4 代码实践

**实践目标**：通过精读确认「A 口写、B 口读、两级寄存器延时」这三件事，并理解它为何天然适合做重排缓冲。

**操作步骤**：

1. 在 [ram_2port.v:50-65](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/ram_2port.v#L50-L65) 确认：A 口在 `S_INPUT`（`wea=input_strobe`）时把 `in_bits` 写进 `ram[addra]`；B 口 `web=0` 永远不写，只读 `ram[addrb]`。
2. 在 [deinterleave.v:54-67](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L54-L67) 确认：A 口地址 `addra` 同时被「写阶段」和「读阶段」复用（`S_OUTPUT` 里 `addra <= lut_addra`），B 口地址 `addrb` 只在 `S_OUTPUT` 用。因为写满之后才读，两者在时间上错开，复用 A 口地址不会冲突。
3. 在 [deinterleave.v:75-81](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L75-L81) 找到 `delayT #(.DATA_WIDTH(23), .DELAY(2))`，它把 `{lut_valid, lut_out}`（共 23 位）整体延 2 拍。结合 [ram_2port.v:55-56](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/ram_2port.v#L55-L56) 的「读出带 1 级寄存」，思考：地址建立 1 拍 + RAM 读出 1 拍 = 数据要 2 拍后才到，所以选择信号（`bita/bitb`）也必须延 2 拍才能正确选位。

**需要观察的现象**：`bit_outa`/`bit_outb` 比 `lut_addra`/`lut_addrb` 滞后出现，正是 `delayT` 把表项的位选信号对齐到这一刻。

**预期结果**：你能解释「为什么是双口、为什么 A 口地址可被读写两阶段复用、为什么要 delayT(2)」。

> 待本地验证：建议在波形里把 `ram_inst.addra`、`ram_inst.doa`、`deinterleave_inst.lut_out_delayed` 三者对齐观察 2 拍关系。

#### 4.2.5 小练习与答案

**练习 1**：`ram_2port` 的 A 口在「同址同拍先写后读」（`if(wea) ram[addra]<=dia; doa<=ram[addra];`）。在 `deinterleave` 的实际使用中，A 口会在读阶段同时发生写吗？为什么这是安全的？

**参考答案**：不会。读阶段（`S_OUTPUT`）里 `input_strobe` 为 0（上游 demod 已经把这个符号的比特送完，正在等下一个符号），所以 `wea=0`，A 口只读不写。写只在 `S_INPUT` 发生、读只在 `S_OUTPUT` 发生，时间上互斥，因此 A 口地址被两阶段复用是安全的。

**练习 2**：如果改用**单口** RAM（同一时刻只能读或写一个地址），`S_OUTPUT` 每拍要输出 2 个比特还能做到吗？代价是什么？

**参考答案**：不能在 1 拍内同时取两个地址。要么把 `S_OUTPUT` 的吞吐减半（每拍只读 1 个地址、输出 1 比特），要么把单口 RAM 跑到 2 倍时钟频率分时读两次。前者会拖慢解码、与 Viterbi 每 `ce` 吃 2 比特的节奏不匹配；后者增加时序压力。所以双口 RAM 是「每拍 2 比特」吞吐的最自然选择。

---

### 4.3 deinter_lut：两级查表与穿孔（puncture）补偿

#### 4.3.1 概念说明

`deinter_lut` 是一张 **2048 深 × 22 位宽** 的 ROM（Xilinx Block Memory IP，见 4.3.3），由 [gen_deinter_lut.py](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py) 离线生成。它解决两个问题：

1. **把重排数学预计算成查表**。802.11 的交织/解交织是两次排列，公式里有除法和取模（见 4.3.2）。在 FPGA 里逐拍算这些又慢又费资源。OpenOFDM 的做法是：在 Python 里按 [decode.rst:101-137](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/decode.rst#L101-L137) 的公式把「每个码率/MCS 下，输出第 i 个比特该取输入的第几个比特」全部算好，烤成表，硬件只查表。

2. **顺带做去穿孔（de-puncture）**。对 3/4、2/3、5/6 这些非 1/2 码率，发射端删过比特；解交织在还原顺序的同时，要在被删的位置**插入空比特**并标记 `erase`，让下游 Viterbi 当「未知」处理。这个「在哪里插空、插的空标 erase」的逻辑，也被一并编码进 LUT 表项的 `null_a/null_b` 位（见 4.3.4）。

> 关键直觉：LUT 表项不只是「地址」，它是「**下一步动作的完整指令**」——读哪个地址、选哪一位、要不要标 erase、要不要发 strobe、子表结不结束。硬件是个纯粹的「取指—执行」机，所有智能都在表里。

#### 4.3.2 核心流程

**重排数学**（来自 [decode.rst:101-137](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/decode.rst#L101-L137) 与 [decode.py:480-523](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L480-L523)）。设 \(k\) 为原始（卷积码输出）比特序号，\(i\) 为第一次排列后的序号，\(j\) 为第二次排列后（即发射、映射到子载波）的序号：

- 参数：legacy \(N_{COL}=16,\ N_{ROW}=3N_{BPSC}\)；HT 20MHz \(N_{COL}=13,\ N_{ROW}=4N_{BPSC}\)；\(s=\max(N_{BPSC}/2,\,1)\)。
- 发射端两次排列（\(k\to i\to j\)）：

\[
i = N_{ROW}\times(k \bmod N_{COL}) + \left\lfloor \frac{k}{N_{COL}}\right\rfloor
\]

\[
j = s\left\lfloor\frac{i}{s}\right\rfloor + \left(i+N_{CBPS}-\left\lfloor N_{COL}\frac{i}{N_{CBPS}}\right\rfloor\right)\bmod s
\]

- 接收端两次逆排列（\(j\to i\to k\)）：

\[
i = s\left\lfloor\frac{j}{s}\right\rfloor + \left(j+\left\lfloor N_{COL}\frac{j}{N_{CBPS}}\right\rfloor\right)\bmod s
\]

\[
k = N_{COL}\cdot i-(N_{CBPS}-1)\left\lfloor \frac{i}{N_{ROW}} \right\rfloor
\]

这些公式在 Python 里对每个 \(j\in[0,N_{CBPS})\) 算出对应的 \(k\)，得到「输出位置 \(j\) ← 取源比特 \(k\)」的映射，即 [gen_deinter_lut.py:87-89](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L87-L89) 里的 `seq`。再把 \(k\) 拆成「子载波号 \(k/N_{BPSC}\)」+「位号 \(k\bmod N_{BPSC}\)」，写进表项的 `addra/bita`（必要时配 `addrb/bitb` 凑成一对）。

**两级查表**：LUT 被组织成「**32 项表头** + **若干子表**」。表头第 `idx` 项存的是「该 rate/MCS 子表的起始偏移」。

- legacy：`idx = rate[3:0]`（4 位速率码，0..15）；
- HT：`idx = 16 + MCS`（16..23）。

硬件先用 `{ht, rate[3:0]}` 当地址读表头拿到起始偏移（`S_GET_BASE`），再从起始偏移顺序往下读子表（`S_OUTPUT` 里 `lut_key+1`），直到读到 `done=1` 的结束行。

**22 位表项格式**（来自 [gen_deinter_lut.py:15-51](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L15-L51)，与 4.1.3 的 RTL 位域完全一致）：

| 位 | 字段 | 含义 |
|----|------|------|
| 21 | `null_a` | 第一个输出比特是否为穿孔补的「空」（→ `erase[0]`） |
| 20 | `null_b` | 第二个输出比特是否为穿孔补的「空」（→ `erase[1]`） |
| 19:14 | `addra` | 第一个比特所在的 RAM 地址（子载波号） |
| 13:8 | `addrb` | 第二个比特所在的 RAM 地址 |
| 7:5 | `bita` | 第一个比特在 6-bit 字里的位号 |
| 4:2 | `bitb` | 第二个比特在 6-bit 字里的位号 |
| 1 | `out_stb` | 本拍是否产生 `output_strobe` |
| 0 | `done` | 当前码率子表是否结束 |

#### 4.3.3 源码精读

**RTL 侧——ROM 封装**：[deinter_lut.v:40-48](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/deinter_lut.v#L40-L48) 声明了一个 11 位地址、22 位数据的只读存储器：

```verilog
module deinter_lut(clka, addra, douta);
input clka;
input [10 : 0] addra;     // 11 位地址 → 最多 2048 项
output [21 : 0] douta;     // 22 位表项
```

它内部例化 Xilinx `BLK_MEM_GEN_V4_2`（[deinter_lut.v:52-105](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/deinter_lut.v#L52-L105)），关键参数：`C_READ_DEPTH_A(2048)`、`C_READ_WIDTH_A(22)`、`C_LOAD_INIT_FILE(1)`、`C_INIT_FILE_NAME("deinter_lut.mif")`、`C_MEM_TYPE(3)`（真双口 ROM）。也就是说，这张表的内容来自 `deinter_lut.mif` 文件——即 Python 脚本的产物。在 `deinterleave` 里它被当成纯组合/单周期 ROM 用（[deinterleave.v:69-73](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L69-L73)）。

**脚本侧——表的组织与生成**：[gen_deinter_lut.py:192-210](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L192-L210) 构造「32 项表头 + 各 rate/MCS 子表」：

```python
header = [0]*32          # 表头：第 idx 项 = 该 rate/MCS 子表的起始偏移
lut = []
offset = 32              # 子表从第 32 项开始
for rate, mcs, ht in RATES:
    if ht:   idx = (1<<4) + mcs          # HT: 16+MCS
    else:    idx = int(RATE_BITS[rate], 2)  # legacy: 速率码
    header[idx] = offset                 # 表头记录起始偏移
    data = do_rate(rate=rate, mcs=mcs, ht=ht)
    offset += len(data)
    lut.extend(data)
```

这正是「`{ht, rate[3:0]}` 当 key 查表头拿起始地址」的脚本侧对应：legacy 的 `idx` 是 `rate[3:0]`，HT 的 `idx` 是 `16+MCS = {1, mcs[3:0]}`，与 [deinterleave.v:111](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L111) 的 `{6'b0, ht, rate[3:0]}` 完全吻合。

**子表末尾的复位行**：[gen_deinter_lut.py:169-177](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L169-L177) 在每个子表最后追加一行 `done=1` 的结束行（legacy `mask=(24<<14)+1`、HT `mask=(26<<14)+1`），即 `addra=half_data_carrier`、`out_stb=0`、`done=1`：

```python
if ht:  mask = (26<<14) + 1
else:   mask = (24<<14) + 1
data.append(mask)
data.extend([0]*4)     # 4 个 0 哨兵
```

这一行被 `S_OUTPUT` 读到时，`lut_done=1`，于是 [deinterleave.v:139-142](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L139-L142) 把 `addra` 复位到 `half_data_carrier`（24/26）并回到 `S_INPUT`，为下一个符号的写入做好准备——这与模块 `reset` 时 `addra <= num_data_carrier>>1`（[deinterleave.v:92](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L92)）一致。

#### 4.3.4 代码实践

**实践目标**：把一条 22 位 LUT 表项「手工解码」，并理解 3/4 码率下 `erase`（去穿孔）是怎么被编码进表的。

**操作步骤**：

1. **读表项格式**。打开 [gen_deinter_lut.py:124](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L124)，看一条「正常」表项如何拼装：

   ```python
   base = (addra<<14) + (addrb<<8) + (bita<<5) + (bitb<<2) + (1<<1)
   #        19:14        13:8        7:5       4:2       1(out_stb)
   ```
   即一条没有穿孔的表项：`null_a=null_b=0`、`out_stb=1`、`done=0`。对照 4.3.2 的位表，确认每一位落点。

2. **看 3/4 码率的穿孔补空**。读 [gen_deinter_lut.py:126-139](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L126-L139)：

   ```python
   elif erase == '3/4':
       if puncture == 0:
           mask = base; data.append(mask); puncture = 1      # 两个真比特
       else:
           mask = (1<<20) + base; data.append(mask)          # null_b=1（bit_b 空）
           mask = (1<<21) + base; data.append(mask)          # null_a=1（bit_a 空）
           puncture = 0
   ```
   含义：3/4 码率下，每 3 条输出表项里，有 1 条是「两个真比特」，另 2 条分别把 `bit_b`、`bit_a` 标成空（`erase`）。这样硬件 `S_OUTPUT` 按表逐拍输出时，自然就在被穿孔的位置补了 `erase=1` 的空比特，把 3/4 还原成 Viterbi 要的 1/2 节奏。2/3、5/6 码率的处理在 [gen_deinter_lut.py:140-166](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L140-L166)，思路相同。

3. **把 erase 接到 Viterbi**。看上层 [ofdm_decoder.v:67-79](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L67-L79) 例化 `deinterleave`，其 `erase` 输出在 [ofdm_decoder.v:147](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L147) 被送给 Viterbi 的 `erase` 端口（`conv_erase <= erase`）。也就是说，`deinterleave` 标了 `erase=1` 的那些「补空比特」，Viterbi 会当成「信息未知」处理，正好实现了去穿孔。

4. **（可选）重新生成表**。在 `scripts/` 下运行 `python gen_deinter_lut.py`（注意它是 Python 2 语法，且 `import decode`，参见 u1-l2/u5-l1 的环境说明），观察它打印的 `[rate=.., mcs=..] -> 起始偏移` 与 `Total row`，确认表头偏移与 4.3.3 的分析一致。

**需要观察的现象**：

- 1/2 码率（如 6、12、24Mbps 的 1/2 档）子表里，所有表项 `null_a=null_b=0`，没有任何 erase；
- 3/4 码率（如 9、18、36Mbps）子表里，每 3 条表项出现 2 个 `null` 位被置 1；
- 每个子表都以一行 `done=1` 结束。

**预期结果**：你能拿任意一条 22 位表项，按位表拆出 `addra/addrb/bita/bitb/out_stb/done/erase`，并判断它属于 1/2 还是被穿孔的码率。

> 待本地验证：`gen_deinter_lut.py` 的实际运行依赖 Python 2 与 `decode` 模块，若本地只有 Python 3 需先按 u1-l2 的说明处理；手工解码表项不依赖运行环境，可立即做。

#### 4.3.5 小练习与答案

**练习 1**：表头为什么是 32 项？legacy 只用了 8 种速率、HT 只用了 MCS0..7，岂不是浪费？

**参考答案**：表头用「`{ht, rate[3:0]}`」作索引，legacy 的 `rate[3:0]` 是 4 位（0..15）、HT 用 `16+MCS`（16..23），合起来需要覆盖 0..31，所以表头必须至少 32 项。legacy 实际只填了 8 个速率码对应的项、HT 填了 8 个 MCS 项，其余项保持 0（未用）。这是用「稀疏但地址直接」换取「无需额外映射表」的取舍——硬件拿到 `rate` 字段就能直接拼出表头地址，不必再查一次「rate→索引」的小表。

**练习 2**：如果未来要支持一种新的穿孔图样（比如某 HT MCS 的 5/6），需要改 Verilog 吗？

**参考答案**：**不需要改 `deinterleave.v` 的逻辑**。穿孔图样完全编码在 LUT 表项的 `null_a/null_b` 位里，由 `gen_deinter_lut.py` 决定。新增穿孔图样只需在脚本的 `do_rate` 里加一个 `erase == '...'` 分支（参考 [gen_deinter_lut.py:150-166](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L150-L166) 的 5/6 处理），重新生成 `deinter_lut.mif` 重新综合即可。这正是「把智能放进表里、硬件通用化」设计的好处。唯一要确认的是新图样产生的子表项数加上表头不超过 2048（LUT 深度）。

---

## 5. 综合实践

**任务**：以一个 802.11a **24Mbps**（16-QAM、1/2 码率）符号为对象，把本讲的三个最小模块串起来，画出「数据 + 控制」的完整时序，并解释为什么这个符号的解交织全程不会出现 `erase=1`。

**建议步骤**：

1. **确定参数**。24Mbps 是 legacy 16-QAM 1/2 码率。由 [decode.py:100](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L100) 的 `RATE_PARAMETERS` 得 \(N_{BPSC}=4,\ N_{CBPS}=192,\ N_{DBPS}=96\)；数据子载波数 = \(N_{CBPS}/N_{BPSC}=48\)，与 `num_data_carrier=48` 一致。

2. **走 `S_INPUT`**。上游 `demodulate` 每个 `output_strobe` 送来一个 6-bit 字（16-QAM 实际只用低 4 位）。`deinterleave` 把它们逐拍写进 `ram_inst` 的 A 口，共需 48 个 `input_strobe` 写满一个符号（因为每拍写 1 个子载波字）。当写指针命中 `half_data_carrier-1=23` 时，带着 `lut_key={6'b0, 0, rate[3:0]}`（`ht=0`，24Mbps 速率码 `1001`=9）进入 `S_GET_BASE`。

3. **走 `S_GET_BASE`**。用 key=9 查表头，得到 24Mbps 子表的起始偏移（由 [gen_deinter_lut.py:192-205](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L192-L205) 在生成时填入）。把该偏移作为新 `lut_key`，进入 `S_OUTPUT`。

4. **走 `S_OUTPUT`**。从起始偏移顺序读子表，每条表项给出 `addra/bita` 与 `addrb/bitb`，分别从双口 RAM 的 A、B 口读出对应 6-bit 字、选出指定位，拼成 `out_bits[1:0]` 发给 Viterbi，同时 `output_strobe` 拉高。因为 24Mbps 是 1/2 码率，[gen_deinter_lut.py:126-128](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L126-L128) 里 `erase=='1/2'` 分支只产生 `mask=base`（不含任何 `null` 位），所以全程 `erase=0`。读到 `done=1` 结束行后，`addra` 复位到 24，回 `S_INPUT` 等下一个符号。

5. **统计**。`S_OUTPUT` 期间 `output_strobe` 的个数 × 2 应等于 \(N_{CBPS}=192\)（1/2 码率无补空），即约 96 个 strobe。可在波形里数 `output_strobe` 的脉冲数验证。

**交付物**：一张时序图，标注 `state`、`addra`、`lut_key`、`wea`、`addrb`、`out_bits`、`erase`、`output_strobe` 在一个 24Mbps 符号上的变化，并用一句话说明「1/2 码率为何没有 erase」。

> 待本地验证：步骤 5 的 `output_strobe` 精确计数请在本地仿真确认（不同码率会因补空比特而变化）。

## 6. 本讲小结

- **解交织是符号内全局重排**，必须先把整个符号的 \(N_{CBPS}\) 个比特写进 RAM，再按原始顺序读出——这是模块中心双口 RAM 与「写满再读」两段式的根本原因。
- [`deinterleave.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v) 是个三态机：`S_INPUT`（写满符号）→ `S_GET_BASE`（查表头拿子表起始地址）→ `S_OUTPUT`（顺序读子表、每拍出 2 比特）。
- **`lut_key` 的两级查表**：先用 `{ht, rate[3:0]}` 查 32 项表头得到子表起始偏移（legacy=`rate[3:0]`，HT=`16+MCS`），再从偏移顺序往下读。
- [`ram_2port.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/ram_2port.v) 双口 RAM 的「A 口写、B 口读」天然支持每拍取 2 个不同地址；读出带 1 级寄存，故用 `delayT(2)` 把 LUT 的位选信号对齐。
- [`deinter_lut`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/deinter_lut.v) 是 2048×22 位 ROM，由 [gen_deinter_lut.py](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py) 离线生成；22 位表项编码了「读哪个地址、选哪一位、是否 erase、是否 strobe、是否结束」的完整指令。
- **`erase`（去穿孔）**：非 1/2 码率在发射端删过比特，脚本把被删位置编码成 `null_a/null_b=1`，硬件据此输出 `erase=1` 的空比特，让 Viterbi 当「未知」处理，从而把 3/4、2/3、5/6 还原成 1/2 节奏；`erase` 经 [ofdm_decoder.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v) 直送 Viterbi。

## 7. 下一步学习建议

- **衔接 u3-l5（ofdm_decoder 子流水线与卷积解码）**：本讲的 `out_bits`/`erase` 如何被映射成 Viterbi 的 3 位软判决 `conv_in0/conv_in1`（`3'b111/3'b011`）与 `erase`，以及 `do_descramble=0` 时如何用 `flush` 补 0 让 Viterbi 吐完剩余比特——这是下一讲的核心。
- **回头看 u3-l3（demodulate）**：本讲每个 `in_bits` 的低 \(N_{BPSC}\) 位就是 demodulate 给出的格雷码比特，两讲的「rate 字段」「定点刻度」「格雷码排列」是连贯的。
- **延伸到 u5（验证与工具链）**：想亲眼看到解交织的对错，可读 u5-l1 的 Python 参考解码器（`decode.Decoder.deinterleave`，即本讲公式的可运行版本）与 u5-l2 的 `test.py` 逐阶段比对框架，把 `sim_out/deinterleave*.txt` 与 Python 期望逐位对照。
- **扩展练习（通向 u6-l5）**：若要支持新调制/MCS，重读 [gen_deinter_lut.py](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py) 的 `RATES`/`RATE_BITS` 与 `do_rate`，体会「加一种速率只需改脚本、重新生成 LUT、Verilog 几乎不动」的可扩展性。
