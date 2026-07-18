# 子载波解调 demodulate.v

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚「解调（demodulation）」在 OFDM 解码流水线中的位置，以及它要解决的核心问题。
- 读懂 `demodulate.v` 的端口、两级流水线结构和「数据 + strobe」握手风格。
- 根据 `rate` 字段查出当前使用的是 BPSK / QPSK / 16-QAM / 64-QAM 中的哪一种星座。
- 算出 `CONS_SCALE_SHIFT = 10` 下各 QAM 判决门限（`QAM_16_DIV`、`QAM_64_DIV_*`）的具体数值，并解释它们为什么取这些值。
- 看懂代码如何用「符号位 + 幅值门限比较」把一个复数星座点还原成 1 / 2 / 4 / 6 个比特。

本讲承接 [u3-l1 信道均衡 equalizer.v](u3-l1-equalizer.md)：均衡器（equalizer）已经把每个子载波的信道增益除掉，输出一个「归一化的复数星座点」；本讲就负责把这个复数点翻译回比特。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个直觉。

### 2.1 什么是「星座」

把一个复数 \( z = I + jQ \) 画在二维平面上（横轴 I、纵轴 Q），它就是一个点。发射机为了让一个子载波「一次多带几个比特」，预先在这个平面上摆好一组固定位置的点，每个点对应一串比特，这组点就叫**星座（constellation）**。802.11 用四种：

| 调制方式 | 每个子载波承载比特数 | 星座点数 | 典型用途 |
|----------|----------------------|----------|----------|
| BPSK     | 1 | 2  | 低速、控制字段 |
| QPSK     | 2 | 4  | 中低速 |
| 16-QAM   | 4 | 16 | 中速 |
| 64-QAM   | 6 | 64 | 高速 |

「解调」就是接收端在平面上收到一个带噪声的点后，判断它**离哪一个标准星座点最近**，从而还原出对应的比特。这本质上就是发射端星座映射的逆操作（见 [decode.rst:24-43](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/decode.rst#L24-L43)）。

### 2.2 为什么能「只比符号位和幅值」

BPSK / QPSK 的星座点只落在坐标轴的正负方向上，所以只要看 I、Q 各自的正负号（符号位）就能判比特。而 16-QAM / 64-QAM 在每条轴上有多个幅度等级（例如 16-QAM 的 I 轴有 ±1、±3 四个等级），因此除了符号位，还要比较**幅值**落在哪一段区间。OpenOFDM 的做法是：把星座点归一化到一种定点刻度上，再用几个常数门限去切这些区间。

### 2.3 什么是定点刻度（fixed-point scaling）

真实硬件不用浮点数，而是把小数放大成整数来算。`common_defs.v` 里定义的 `CONS_SCALE_SHIFT` 就是这种「放大倍数」的约定：它规定星座点在进入 `demodulate` 时，已经被放大了 \( 2^{\text{CONS\_SCALE\_SHIFT}} \) 倍。理解了这个刻度，才能看懂后面那些门限常数。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [verilog/demodulate.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v) | 本讲主角。把均衡后的复数星座点判成最多 6 个比特。 |
| [verilog/common_defs.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_defs.v) | 全局宏定义，其中 `CONS_SCALE_SHIFT` 决定解调的定点刻度。 |
| [verilog/rate_to_idx.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rate_to_idx.v) | 把 `rate` 字段翻译成解交织表索引；其注释说明了 `rate` 字段的位格式，可帮助理解 `demodulate` 的 rate 解析。 |
| [verilog/ofdm_decoder.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v) | 解码子流水线顶层；在此实例化 `demodulate`，能看到它的输入来自哪里。 |
| [docs/source/decode.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/decode.rst) | 解调/解交织/卷积解码/解扰的官方文档，给出各调制方式与速率的对应表。 |

## 4. 核心概念与源码讲解

### 4.1 demodulate 模块：位置、接口与流水线

#### 4.1.1 概念说明

在八步解码流水线里，解调是「频域还原」转向「比特还原」的转折点：均衡器输出的是「干净」的复数频域样本（每个子载波一个点），解调把这些点一颗一颗地翻译成比特，再交给下游的解交织（deinterleave）。所以 `demodulate` 的职责非常单一：**给定调制方式和一颗复数点，输出它对应的比特**，不关心信道、不关心交织。

#### 4.1.2 核心流程

`demodulate` 对每颗输入样本做三件事：

1. **查调制方式**：根据 `rate` 字段，决定当前符号用的是 BPSK / QPSK / 16-QAM / 64-QAM 中的哪一种。
2. **取绝对值**：把有符号的 I、Q 各自取绝对值（用补码 `~x+1`），供 QAM 的幅值比较使用；同时把原始 I、Q 延时一拍，供符号位判决使用。
3. **判决比特**：按调制方式，用符号位和幅值门限比较，拼出最多 6 个比特。

整个模块是一个两级流水线，输入 strobe 经过一个 `delayT`（延时 2 拍）后成为输出 strobe，保证握手信号与数据对齐——这是全项目「数据 + strobe」单向握手风格的又一个范例（参考 [u3-l2 复数运算原语](u3-l2-complex-primitives.md)）。

#### 4.1.3 源码精读

模块端口只有一组数据输入和一组比特输出：

[demodulate.v:3-15 端口声明](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v#L3-L15)

```verilog
module demodulate (
    input clock, input enable, input reset,
    input [7:0]  rate,        // 调制 / 速率指示，由上层（dot11 状态机）送来
    input [15:0] cons_i,      // 均衡后的 I 路（有符号定点）
    input [15:0] cons_q,      // 均衡后的 Q 路（有符号定点）
    input input_strobe,
    output reg [5:0] bits,    // 最多 6 比特（64-QAM），低位有效
    output output_strobe
);
```

- `cons_i` / `cons_q` 就是均衡器归一化后的星座点坐标（接 `ofdm_decoder` 的 `sample_in[31:16]` 与 `[15:0]`，详见 [ofdm_decoder.v:54-65 demodulate 实例化](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L54-L65)）。
- `bits` 固定 6 位：BPSK 只用 `bits[0]`，QPSK 用 `bits[1:0]`，16-QAM 用 `bits[3:0]`，64-QAM 用 `bits[5:0]`，高位补 0。

输出 strobe 由输入 strobe 延时 2 拍得到，与两级流水线对齐：

[demodulate.v:38-44 strobe 延时](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v#L38-L44)

```verilog
delayT #(.DATA_WIDTH(1), .DELAY(2)) stb_delay_inst (
    .clock(clock), .reset(reset),
    .data_in(input_strobe),
    .data_out(output_strobe)
);
```

> 小提示：这里用 `delayT`（每个时钟无条件移位的固定延时链）而不是 `delay_sample`（按样本推进的延时）。两者区别见 [u3-l2](u3-l2-complex-primitives.md)。`demodulate` 内部数据走的是时钟级流水，所以 strobe 也按时钟延时。

主体逻辑里，第一拍先把绝对值、延时样本、调制方式都寄存下来：

[demodulate.v:56-59 取绝对值并延时](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v#L56-L59)

```verilog
abs_cons_i <= cons_i[15]? ~cons_i+1: cons_i;   // 有符号补码取绝对值
abs_cons_q <= cons_q[15]? ~cons_q+1: cons_q;
cons_i_delayed <= cons_i;                        // 延时一拍，供符号位判决
cons_q_delayed <= cons_q;
```

`~x+1` 就是「按位取反再加一」，即二进制补码的取负；对一个负数取负就得到绝对值。这种写法在全项目里反复出现（如 `power_trigger`、`sync_short`）。

第二拍再用这些寄存好的值判决比特（见 4.4）。由于所有量（`mod`、`abs_cons_*`、`cons_*_delayed`）都在同一个 `always` 块里被非阻塞赋值寄存，它们在 `case(mod)` 中引用时指向的是**同一颗输入样本**对应的那拍数据，因此幅值与符号位天然对齐。

#### 4.1.4 代码实践

**目标**：确认 `demodulate` 在解码链里的上下游接线。

1. 打开 [ofdm_decoder.v:54-65](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L54-L65)，找到 `demod_inst` 的实例化。
2. 确认它的 `cons_i/cons_q` 接的是 `input_i/input_q`（即 `sample_in` 的高低 16 位），而 `sample_in` 又来自 `dot11.v` 中 `equalizer` 的输出（参考 [u3-l1](u3-l1-equalizer.md)）。
3. 确认它的 `bits` 输出 `demod_out` 直接喂给 `deinterleave_inst` 的 `in_bits`（[ofdm_decoder.v:67-79](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L67-L79)）。

**预期结果**：你能画出 `equalizer → demodulate → deinterleave` 这一小段数据通路，并指出 `demodulate` 既不读信道、也不做交织，只做「复数 → 比特」。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `bits` 端口固定 6 位，而不是按调制方式动态变宽？
  - **答**：因为 64-QAM 单个子载波最多承载 6 比特；对更低阶调制，高几位直接补 0 即可。固定宽度让下游 `deinterleave` 的缓冲区（每行 6 比特，见 [decode.rst:149-152](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/decode.rst#L149-L152)）能用统一的位宽，简化设计。

---

### 4.2 rate → 调制方式的映射表

#### 4.2.1 概念说明

同一个 `demodulate` 硬件要支持四种星座，它怎么知道当前符号该用哪一种？答案是上层状态机（`dot11.v`）在解析完 SIGNAL / HT-SIG 字段后，把一个 8 位的 `rate` 信号伴随数据送进来。`demodulate` 用一个 `case` 表把 `rate` 翻译成内部的 `mod`（BPSK / QPSK / QAM_16 / QAM_64 四个 localparam）。

`rate` 字段的位格式在 [rate_to_idx.v:1-4](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rate_to_idx.v#L1-L4) 有注释：

- 最高位 `rate[7] = 0`：802.11a/g 的 legacy 速率，`rate[3:0]` 是速率编码；
- 最高位 `rate[7] = 1`：802.11n 的 MCS，`rate[6:0]` 是 MCS 号。

所以 `demodulate` 用 `{rate[7], rate[3:0]}` 这 5 位做 case 选择键，正好同时区分 legacy / HT 与具体调制。

#### 4.2.2 核心流程

`case` 表分两组：先判 `rate[7]` 区分 legacy / HT，再按 `rate[3:0]` 选调制；命中后给 `mod` 寄存器赋一个 localparam 值，未命中走 `default`（BPSK）。

[demodulate.v:25-28 调制方式 localparam](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v#L25-L28)

```verilog
localparam BPSK   = 1;
localparam QPSK   = 2;
localparam QAM_16 = 3;
localparam QAM_64 = 4;
```

[demodulate.v:61-83 rate → mod 的 case 表](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v#L61-L83)

把源码 case 表与 [decode.rst:57-78](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/decode.rst#L57-L78) 给出的「调制方式 ↔ 比特率」权威表对照，可整理出下表（Mbps 与调制方式的对应来自 decode.rst；具体 `rate[3:0]` 编码到 Mbps 的逐位映射属于 SIGNAL 字段解析，留到 [u4-l2 legacy SIGNAL 字段](u4-l2-legacy-signal-field.md) 详讲）：

| 标准 | 速率 / MCS | 调制方式 | `demodulate` case 键 `{rate[7], rate[3:0]}` |
|------|-----------|----------|----------------------------------------------|
| 802.11a | 6 / 9 Mbps   | BPSK   | `5'b0_1011` / `5'b0_1111` |
| 802.11a | 12 / 18 Mbps| QPSK   | `5'b0_1010` / `5'b0_1110` |
| 802.11a | 24 / 36 Mbps| 16-QAM | `5'b0_1001` / `5'b0_1101` |
| 802.11a | 48 / 54 Mbps| 64-QAM | `5'b0_1000` / `5'b0_1100` |
| 802.11n | MCS 0       | BPSK   | `5'b1_0000` |
| 802.11n | MCS 1 / 2   | QPSK   | `5'b1_0001` / `5'b1_0010` |
| 802.11n | MCS 3 / 4   | 16-QAM | `5'b1_0011` / `5'b1_0100` |
| 802.11n | MCS 5 / 6 / 7 | 64-QAM | `5'b1_0101` / `5'b1_0110` / `5'b1_0111` |

> 注意：HT 分支用 `rate[3:0]` 正好覆盖 MCS 0–7（OpenOFDM 只支持到 MCS 7，见项目概述 [u1-l1](u1-l1-project-overview.md)）。MCS 8/9（256-QAM）不在支持范围内——这正是 [u6-l5 扩展实践](u6-l5-extend-rates.md) 要讨论的改造点。

#### 4.2.3 代码实践

**目标**：把 `rate_to_idx.v` 的速率注释和 `demodulate.v` 的 case 表交叉对照，验证两者一致。

1. 读 [rate_to_idx.v:23-60](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rate_to_idx.v#L23-L60)：它用 `{rate[7], rate[2:0]}` 把 legacy 速率映射成解交织表索引（6→0, 9→1, 12→2, …, 54→7）。
2. 把 `demodulate.v` 中 BPSK 两条（6/9 Mbps）对应的 `rate[3:0]` 取低 3 位，与 `rate_to_idx` 中 6/9 Mbps 的 `rate[2:0]` 对照。
3. **预期结果**：两处对同一速率给出的低位编码一致，说明 `rate` 字段在两个模块里的解读是统一的；`demodulate` 多用的 `rate[3]` 位只是为了让 case 键更明确地区分各调制档位。

#### 4.2.4 小练习与答案

- **练习 1**：如果一个未在表中出现的 `rate` 值进来，会发生什么？
  - **答**：走 `default: mod <= BPSK;`（[demodulate.v:82](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v#L82)），即默认按 BPSK 解。这是一种保守降级，但正常情况下上层状态机会先校验 SIGNAL 字段的合法性（parity / CRC），不会让非法 rate 走到这里。

---

### 4.3 CONS_SCALE_SHIFT 与归一化门限常数

#### 4.3.1 概念说明

这是本讲最关键、也最容易看走眼的一节。`demodulate` 对 QAM 的判决靠几个常数门限（`QAM_16_DIV`、`QAM_64_DIV_*`），这些门限的数值完全由一个宏 `CONS_SCALE_SHIFT` 决定。要理解门限为什么取这些值，必须先弄懂这套**定点刻度约定**。

#### 4.3.2 核心流程与数学

定点刻度定义在 [common_defs.v:9](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_defs.v#L9)：

```verilog
`define CONS_SCALE_SHIFT            10
```

`demodulate` 据此定义最大刻度：

[demodulate.v:17-23 门限常数](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v#L17-L23)

```verilog
localparam MAX = 1<<`CONS_SCALE_SHIFT;          // = 2^10 = 1024

localparam QAM_16_DIV   = MAX*2/3;              // = 682

localparam QAM_64_DIV_0 = MAX*2/7;              // = 292
localparam QAM_64_DIV_1 = MAX*4/7;              // = 585
localparam QAM_64_DIV_2 = MAX*6/7;              // = 877
```

具体数值（Verilog 整数除法）：

\[
\text{MAX} = 2^{10} = 1024
\]

\[
\text{QAM\_16\_DIV} = \left\lfloor \frac{1024 \times 2}{3} \right\rfloor = \left\lfloor 682.67 \right\rfloor = 682
\]

\[
\text{QAM\_64\_DIV\_0} = 292,\quad \text{QAM\_64\_DIV\_1} = 585,\quad \text{QAM\_64\_DIV\_2} = 877
\]

**为什么是 2/3 和 2/7、4/7、6/7 这些比例？** 因为它们正好是星座相邻幅度等级的中点：

- **16-QAM**：每条轴上的标准点在 \( \pm 1, \pm 3 \)（外层点幅度为 3）。内层点（幅度 1）与外层点（幅度 3）的判决边界在它们的中点幅度 2。若约定「最外层点幅度对应 MAX」，则边界为 \( \frac{2}{3}\text{MAX} = 682 \)。
- **64-QAM**：每条轴上的标准点在 \( \pm 1, \pm 3, \pm 5, \pm 7 \)（外层点幅度为 7）。三个判决边界在幅度 \( 2, 4, 6 \)，对应 \( \frac{2}{7}\text{MAX}, \frac{4}{7}\text{MAX}, \frac{6}{7}\text{MAX} = 292, 585, 877 \)。

换句话说，`demodulate` 与 `equalizer` 之间有一个隐含契约：**均衡器输出的星座点被缩放成「最外层星座点幅度 = MAX = 1024」的定点表示**。门限常数就是在这个刻度下、位于相邻星座等级正中间的「等距判决线」。把刻度从 10 改成别的值，所有门限都要等比例变化——这也是为什么 `MAX` 用 `1<<CONS_SCALE_SHIFT` 表达式、而不是写死 1024 的原因。

> 关于「最外层点幅度 = MAX」这一缩放是如何由均衡器建立的，其细节（LTS 参考、复数除法、左移 `CONS_SCALE_SHIFT` 位）属于 [u3-l1 信道均衡](u3-l1-equalizer.md) 的内容。本讲只需记住这个契约即可。绝对幅值是否符合理论值，建议通过仿真实测确认（见综合实践）。

#### 4.3.3 代码实践

**目标**：亲手验算门限常数，建立对刻度的直觉。

1. 在 [common_defs.v:9](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_defs.v#L9) 确认 `CONS_SCALE_SHIFT = 10`。
2. 用 Python 或计算器算 `1024*2//3`、`1024*2//7`、`1024*4//7`、`1024*6//7`，得到 682、292、585、877，与 [demodulate.v:19-23](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v#L19-L23) 对照。
3. **思考题**：如果某天把 `CONS_SCALE_SHIFT` 改成 11，`QAM_16_DIV` 会变成多少？
   - **答**：`MAX = 2048`，`QAM_16_DIV = 2048*2/3 = 1365`。可见门限随刻度等比例放大。

**预期结果**：你能解释「门限 = 相邻星座等级中点占最外层幅度的比例 × MAX」这一关系。

#### 4.3.4 小练习与答案

- **练习 1**：16-QAM 的 `QAM_16_DIV = 682`，那么理想的「内层点（幅度 1）」和「外层点（幅度 3）」在这个刻度下分别落在什么数值附近？
  - **答**：内层点 \( \approx 1024/3 \approx 341 \)，外层点 \( = 1024 \)。门限 682 正好处在 341 与 1024 之间（中点 682.5），实现等距判决。
- **练习 2**：64-QAM 为什么需要三个门限、而 16-QAM 只需要一个？
  - **答**：每条轴上的判决区间数 = （点数 − 1）。16-QAM 每轴 4 个点（±1,±3）→ 1 条内部边界；64-QAM 每轴 8 个点（±1,±3,±5,±7）→ 3 条内部边界。符号位另算（负责正/负）。

---

### 4.4 各星座的比特判决实现

#### 4.4.1 概念说明

有了调制方式 `mod` 和门限常数，最后一步就是「按星座做判决」。四种星座的判决复杂度递增：BPSK 只看一个符号位，QPSK 看两个符号位，16-QAM 每条轴加一个幅值比较，64-QAM 每条轴加两个幅值比较。OpenOFDM 用非常紧凑的 `case(mod)` 把这四种情况写在一段里。

#### 4.4.2 核心流程

判决统一遵循一个模式：

- **符号位**：`~cons_i_delayed[15]` 取反符号位作为最低位。因为 `cons_i` 是有符号补码，`[15]=0` 表示正、`=1` 表示负；取反后「I 路为正 → 该比特为 1，为负 → 为 0」。
- **幅值比较**：`abs_cons_i < QAM_x_DIV ? 1 : 0`，用取过绝对值的幅值与门限比大小，判断它落在内层还是外层区间。

#### 4.4.3 源码精读

[demodulate.v:85-112 四种星座的判决](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v#L85-L112)

逐段拆开看：

**BPSK**（1 比特）——只判 I 路符号：

```verilog
BPSK: begin
    bits[0] <= ~cons_i_delayed[15];   // I 路符号位取反
    bits[5:1] <= 0;
end
```

**QPSK**（2 比特）——I、Q 各判一个符号位：

```verilog
QPSK: begin
    bits[0] <= ~cons_i_delayed[15];
    bits[1] <= ~cons_q_delayed[15];
    bits[5:2] <= 0;
end
```

**16-QAM**（4 比特）——每条轴 1 个符号位 + 1 个幅值位（内/外）：

```verilog
QAM_16: begin
    bits[0] <= ~cons_i_delayed[15];                          // I 符号
    bits[1] <= abs_cons_i < QAM_16_DIV? 1: 0;                // I 内(<682)/外(≥682)
    bits[2] <= ~cons_q_delayed[15];                          // Q 符号
    bits[3] <= abs_cons_q < QAM_16_DIV? 1: 0;                // Q 内/外
    bits[5:4] <= 0;
end
```

`bits[1] = 1` 表示幅值落在内层（幅度 1，数值 ~341），`= 0` 表示外层（幅度 3，数值 ~1024）。以 682 为界，两边等距。

**64-QAM**（6 比特）——每条轴 1 个符号位 + 2 个幅值位（三位格雷码的后两位）。以 I 路 `bits[2:1]` 为例：

```verilog
bits[1] <= abs_cons_i < QAM_64_DIV_1? 1: 0;                  // 以 585 为界
bits[2] <= abs_cons_i > QAM_64_DIV_0 &&
           abs_cons_i < QAM_64_DIV_2? 1: 0;                  // 落在 (292, 877) 之间
```

把四个理想幅度（\( 1,3,5,7 \) → 数值 \( 146,439,731,1024 \)）代入，可得 `bits[2:1]` 的输出：

| 理想幅度 | 数值 | `bits[1]`（<585?） | `bits[2]`（292<x<877?） | `bits[2:1]` |
|---------|------|--------------------|--------------------------|-------------|
| 1（内） | 146 | 1 | 0（146<292） | `10` |
| 3       | 439 | 1 | 1（在区间内） | `11` |
| 5       | 731 | 0（731>585） | 1（在区间内） | `01` |
| 7（外） | 1024| 0 | 0（1024>877） | `00` |

得到序列 `10 → 11 → 01 → 00`，相邻幅度之间只差 1 个比特——这正是**格雷码（Gray code）**，与 802.11 星座比特标注一致：相邻星座点只翻转一个比特，可把判决误差造成的比特错误降到最小。Q 路 `bits[5:4]` 用 `abs_cons_q` 完全对称地产生。

#### 4.4.4 代码实践

**目标**：用一个真实样本，在波形里观察一颗 64-QAM 星座点被还原成 6 比特的全过程。

1. 按 [u1-l2](u1-l2-environment-and-simulation.md) 跑通一个 802.11a 48Mbps 或 54Mbps（64-QAM）样本仿真。
2. 用 gtkwave 打开 `dot11.vcd`，加入 `ofdm_decoder_inst.demod_inst.cons_i`、`cons_q`、`abs_cons_i`、`mod`、`bits`、`output_strobe`。
3. 在 `output_strobe` 为高的那些拍上，读出 `cons_i`、`abs_cons_i`、`bits[2:1]`，对照上表验证：`abs_cons_i` 落在哪个数值段，`bits[2:1]` 是否等于对应的格雷码。
4. **预期结果**：由于真实样本含噪声，`abs_cons_i` 不会精确等于 146/439/731/1024，但应落在对应区间内，`bits` 输出应与最近星座点的格雷码一致。
5. 如果暂时无法运行仿真，本步骤标注为「待本地验证」——但 4.3 节的门限数值与 4.4 节的格雷码推导均可纯由源码推出，不依赖仿真。

#### 4.4.5 小练习与答案

- **练习 1**：BPSK 判决只用了 `cons_i`（I 路），完全没看 `cons_q`，为什么够用？
  - **答**：BPSK 的两个星座点只沿实轴分布（+1 和 −1），所有信息都在 I 路的符号里，Q 路理论上为 0（含噪声），不影响判决。
- **练习 2**：把 `bits[0] <= ~cons_i_delayed[15];` 改成 `bits[0] <= cons_i_delayed[15];`（不取反），整条解码链还会工作吗？
  - **答**：会导致每个符号位整体取反，解出的比特流系统性翻转，后续卷积解码/解扰几乎必然失败、`fcs_ok` 拉不高。这说明「取反符号位」是项目与 802.11 比特极性约定的一部分，不能随意改。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「从 rate 到比特」的小任务。

**任务**：给定一段 802.11a 24Mbps 的数据符号，手工模拟 `demodulate` 对一颗样本的判决。

1. **查调制**：24Mbps 属于 16-QAM（见 4.2 表），对应 `mod = QAM_16`。
2. **备门限**：算出 `MAX = 1024`、`QAM_16_DIV = 682`（见 4.3）。
3. **判一颗点**：假设均衡器输出 `cons_i = 16'shFC00`、`cons_q = 16'sh0380`（十六进制补码）。
   - `cons_i = 0xFC00` 是负数，绝对值 \( = \sim\texttt{0xFC00}+1 = \texttt{0x0400} = 1024 \)。
   - `cons_q = 0x0380 = 896 \)（正数，绝对值即 896）。
4. **套判决**（对照 [demodulate.v:95-101](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v#L95-L101)）：
   - `bits[0] = ~cons_i[15] = ~1 = 0`（I 路负）。
   - `bits[1] = (abs_cons_i=1024 < 682?) → 0`（I 外层）。
   - `bits[2] = ~cons_q[15] = ~0 = 1`（Q 路正）。
   - `bits[3] = (abs_cons_q=896 < 682?) → 0`（Q 外层）。
   - 结果 `bits = 6'b00_0100`（即 `bits[3:0] = 4'b0100`）。
5. **进阶**：把上面对 `cons_i/cons_q` 的取值改成你自选的四个不同象限/内外层组合，分别手算 `bits`，再对照 16-QAM 星座图（[decode.rst:35-40](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/decode.rst#L35-L40) 引用的 mod.png）确认你算出的 4 比特正是该象限点的标注比特。

**预期结果**：你能不依赖电脑，仅凭「符号位 + 一个幅值门限 682」手工把任何 16-QAM 定点样本判成 4 个比特，并解释每一步。

## 6. 本讲小结

- `demodulate` 是八步解码流水线里「频域复数 → 比特」的转折点，接 `equalizer` 的归一化输出，喂给 `deinterleave`；本身不涉及信道或交织。
- 它用 `case({rate[7], rate[3:0]})` 把 `rate` 字段映射到 BPSK / QPSK / 16-QAM / 64-QAM 四种调制之一，覆盖 802.11a 全速率与 802.11n MCS 0–7。
- 判决门限全部由 `CONS_SCALE_SHIFT`（=10，故 `MAX=1024`）派生：`QAM_16_DIV = 682`，`QAM_64_DIV_{0,1,2} = 292 / 585 / 877`。
- 这些门限的比值 \( 2/3 \) 与 \( 2/7, 4/7, 6/7 \) 正好是星座相邻幅度等级的中点占最外层幅度的比例——这背后是 `demodulate` 与 `equalizer` 之间「最外层星座点幅度 = MAX」的定点契约。
- 判决实现统一为「取反符号位 + 幅值门限比较」：BPSK/QPSK 只用符号位，16-QAM 每轴加 1 个比较，64-QAM 每轴加 2 个比较并自然形成格雷码。
- 模块是两级流水线，输出 strobe 由 `delayT` 延时 2 拍对齐，延续全项目「数据 + strobe」单向握手风格。

## 7. 下一步学习建议

`demodulate` 把每颗子载波翻译成了最多 6 比特，但这些比特在符号内是被**交织**过的——接下来应该读 [u3-l4 解交织 deinterleave.v](u3-l4-deinterleave.md)，看 `deinter_lut` + 双口 RAM 如何把一个符号内的比特按 802.11 规则重排回原始顺序。

如果想更系统地理解「rate 字段从哪里来、怎么被校验」，可跳读 [u4-l2 legacy SIGNAL 字段解析](u4-l2-legacy-signal-field.md) 与 [u4-l3 HT-SIG 解析](u4-l3-ht-sig-and-crc.md)。

若你对定点刻度的全局观感兴趣（`CONS_SCALE_SHIFT` 与 ATAN / ROTATE 刻度的关系、为何「改 shift 必改 PI」），可预先浏览 [u6-l1 定点数与缩放约定](u6-l1-fixed-point-scaling.md)。

建议继续精读的源码：[deinterleave.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v) 与生成解调/解交织查找表的 [scripts/gen_deinter_lut.py](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py)。
