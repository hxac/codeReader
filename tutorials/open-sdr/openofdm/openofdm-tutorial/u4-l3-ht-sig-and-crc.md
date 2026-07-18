# HT-SIG 解析、CRC 与格式检测

## 1. 本讲目标

本讲是「控制平面」单元的第三篇，承接 [u4-l2 legacy SIGNAL 字段解析与校验](u4-l2-legacy-signal-field.md)。

在 legacy SIGNAL 校验通过、且 `legacy_rate == 4'b1011`（即 6 Mbps）时，接收机还不能确定这是一个真正的 802.11a 6 Mbps 包，还是一个 802.11n HT-mixed 包借 L-SIG「伪装」成 6 Mbps。要区分二者，必须去看 SIGNAL 之后的那个 OFDM 符号——也就是 **HT-SIG**。

学完本讲，你应当能够：

- 说出 HT-SIG 两个字（`ht_sig1` / `ht_sig2`）里各字段（MCS / CBW / length / STBC / FEC / SGI / CRC / tail）的位定义与含义；
- 解释 `S_DETECT_HT` 状态为什么用「`abs_eq_q > abs_eq_i` 的样本数 ≥ 4」就能认出 HT-SIG，以及随后 90° 顺时针旋转的来历；
- 写出 `ht_sig_crc.v` 对应的 CRC 生成多项式，并讲清楚 `crc[i] = ~C[7-i]` 这一「先取反、再倒序」输出背后的两个独立原因；
- 逐拍追踪 `S_CHECK_HT_SIG_CRC` 的 36 拍时序，并能定位到 `S_CHECK_HT_SIG` 的字段合法性校验。

---

## 2. 前置知识

### 2.1 HT-mixed 模式与 L-SIG 欺骗

802.11n 为了让老式 802.11a/g 站点也能「退避」，设计了 **HT-mixed 模式**：它的前导码以一段和 802.11a 完全相同的 legacy 前导 + legacy SIGNAL（L-SIG）开头。L-SIG 里的速率字段**永远被写成 6 Mbps**，LENGTH 字段则被刻意调整，使老站点据此计算出的「占用时长」恰好等于整个 HT 包的空口时长。于是老站点会安静等待，HT 站点则继续往后读 HT-SIG。

> 对接收机来说：解码完 L-SIG，如果 rate 不是 6 Mbps，那就是普通 802.11a 包，直接进 DATA；如果 rate **是** 6 Mbps，就需要进一步判断后面是不是 HT-SIG。这正是 `S_CHECK_SIGNAL` 里 `legacy_rate == 4'b1011` 分支要解决的问题（见 [u4-l2](u4-l2-legacy-signal-field.md)）。

### 2.2 BPSK 的 I 路 vs Q 路

BPSK 把每个比特映射成一个复数星座点。802.11a 的 SIGNAL 字段用 **I 路 BPSK**：比特 0/1 映射到实轴（I 轴）上的 +1/−1，Q 分量接近 0。而 HT-SIG 故意改用 **Q 路 BPSK**：星座点跑到虚轴（Q 轴）上，I 分量接近 0。这是一个非常便宜却很可靠的「格式标记」——只要看到一连串星座点的 |Q| 明显大于 |I|，就基本可以断定这是 HT-SIG 而不是 legacy 数据。

### 2.3 CRC 是什么、为什么要「初始化全 1 + 末尾取反」

CRC（循环冗余校验）把待校验的比特串看成一个大多项式，用一个固定的**生成多项式** \(G(x)\) 去除，把余数作为校验值附在数据后面。接收端用同样的 \(G(x)\) 再除一次，余数为 0（或等于附带的校验值）即认为无误。

两个通用约定会在本讲反复出现，请先记住：

1. **寄存器初始化为全 1**（这里是 `8'hff`）；
2. **最终余数按位取反**输出。

这两条配套使用，目的是让消息**前导的连续 0** 也能影响校验值（否则 XOR 0 等于没影响，前导 0 就成了「盲区」）。u4-l5 的 CRC-32 也是同一套思路（初始化 `32'hffff_ffff`、末尾异或 `0xffff_ffff`）。

### 2.4 本讲用到的状态码

HT-SIG 相关的状态码和错误码都定义在 `common_params.v`：

| 状态 | 值 | 含义 |
|------|----|------|
| `S_DETECT_HT` | 5 | 检测后面那个符号是不是 HT-SIG |
| `S_HT_SIGNAL` | 6 | 解码（旋转后的）HT-SIG 两个符号 |
| `S_CHECK_HT_SIG_CRC` | 7 | 用 36 拍算 CRC 并比对 |
| `S_CHECK_HT_SIG` | 8 | 逐字段做能力合法性校验 |
| `S_HT_SIG_ERROR` | 13 | 任一校验失败，回空闲等下一个包 |

详见 [verilog/common_params.v#L32-L41](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L32-L41) 与错误码 [verilog/common_params.v#L56-L65](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L56-L65)。

---

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [verilog/dot11.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) | 顶层状态机。HT-SIG 解析段：字段声明（顶部 assign）、`S_DETECT_HT` → `S_HT_SIGNAL` → `S_CHECK_HT_SIG_CRC` → `S_CHECK_HT_SIG` 四个状态 |
| [verilog/ht_sig_crc.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ht_sig_crc.v) | 独立的 CRC-8 计算模块：逐 bit 更新 8 级寄存器、输出取反+倒序 |
| [verilog/common_params.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v) | HT 相关状态码 `S_*` 与错误码 `E_UNSUPPORTED_*` / `E_WRONG_CRC` |
| [docs/source/sig.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/sig.rst) | HT-SIG 字段格式与 CRC 数学定义的权威文档 |

数据流全景（本讲覆盖粗框部分）：

```
L-SIG 校验通过 & rate==6Mbps
        │
        ▼
 S_DETECT_HT   ── 计数 |Q|>|I| ──≥4──► 疑似 HT-SIG
        │ (否则 normal_eq_count>4 → 当作 legacy DATA)
        ▼
 S_HT_SIGNAL   ── 90° 顺时针旋转 ──► 喂 ofdm_decoder ──► 收齐 6 字节
        │                                           (ht_sig1 / ht_sig2)
        ▼
 S_CHECK_HT_SIG_CRC  ── 36 拍算 CRC-8 ──► crc_out ^ crc == 0 ?
        │
        ▼
 S_CHECK_HT_SIG  ── MCS/CBW/rsvd/STBC/FEC/SGI/num_ext/tail 合法性
        │
        ▼
 S_HT_STS → S_HT_LTS → S_DECODE_DATA（与 legacy 分支汇合）
```

---

## 4. 核心概念与源码讲解

### 4.1 HT-SIG 字段结构与两字节拼装

#### 4.1.1 概念说明

HT-SIG 跨 **两个 OFDM 符号**，共 48 个数据比特（卷积编码后 96 比特，正好两个符号）。这 48 比特在解码后被拼成 6 个字节，前 3 字节装进 `ht_sig1`、后 3 字节装进 `ht_sig2`。两个字各 24 位，按「先收到的 bit 落低位」的约定排布（与 u4-l2 的 `signal_bits` 拼装方式一致）。

字段分布（位宽请对照下方源码的 assign）：

| 寄存器 | 字段 | 位 | 含义 |
|--------|------|----|------|
| `ht_sig1` | MCS | `[6:0]` | 调制编码方案，OpenOFDM 只支持 0–7 |
| `ht_sig1` | CBW | `[7]` | 0=20MHz，1=40MHz（OpenOFDM 仅 20MHz） |
| `ht_sig1` | Length | `[23:8]` | HT 长度（用于推算 OFDM 符号数） |
| `ht_sig2` | Smoothing | `[0]` | 平滑提示 |
| `ht_sig2` | Not Sounding | `[1]` | 非探测 |
| `ht_sig2` | Reserved | `[2]` | 保留位 |
| `ht_sig2` | Aggregation | `[3]` | 聚合指示 |
| `ht_sig2` | STBC | `[5:4]` | 空时分组码，OpenOFDM 仅支持 0 |
| `ht_sig2` | FEC | `[6]` | 0=BCC，1=LDPC（OpenOFDM 仅 BCC） |
| `ht_sig2` | SGI | `[7]` | 短保护间隔 |
| `ht_sig2` | NumExt | `[9:8]` | 扩展空间流数，仅支持 0 |
| `ht_sig2` | CRC | `[17:10]` | 前 34 比特的 CRC-8 |
| `ht_sig2` | Tail | `[23:18]` | 6 位卷积尾，必须全 0 |

其中 **CRC 覆盖的是前 34 个比特** = `ht_sig1[0..23]`（24 位）+ `ht_sig2[0..9]`（10 位）。

#### 4.1.2 核心流程

1. `S_HT_SIGNAL` 状态下，子流水线 `ofdm_decoder` 逐字节吐出 HT-SIG 的 6 个字节；
2. 每来一个 `byte_out_strobe`，按 `{byte_out, ht_sigN[23:8]}` 左移拼接（先到字节最终落低位）；
3. `byte_count` 从 0 数到 6 时，`ht_sig1`（字节 0–2）和 `ht_sig2`（字节 3–5）都已装满，转入 CRC 校验。

#### 4.1.3 源码精读

字段声明在顶层模块的信号声明区，全部是组合 `assign`，本质就是对 `ht_sig1` / `ht_sig2` 做位切片：

[verilog/dot11.v#L209-L228](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L209-L228) — HT-SIG 字段切片与 CRC/tail 抽取：

```verilog
// HT-SIG information
reg [23:0] ht_sig1;
reg [23:0] ht_sig2;

assign ht_mcs        = ht_sig1[6:0];
assign ht_cbw        = ht_sig1[7];
assign ht_len        = ht_sig1[23:8];
...
assign ht_stbc       = ht_sig2[5:4];
assign ht_fec_coding = ht_sig2[6];
assign ht_sgi        = ht_sig2[7];
assign ht_num_ext    = ht_sig2[9:8];

wire ht_rsvd         = ht_sig2[2];
wire [7:0] crc       = ht_sig2[17:10];   // 接收到的 CRC 字段
wire [5:0] ht_sig_tail = ht_sig2[23:18];
```

两字节的拼装发生在 `S_HT_SIGNAL`：注意 `byte_count < 3` 时写入 `ht_sig1`，否则写入 `ht_sig2`：

[verilog/dot11.v#L648-L655](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L648-L655) — 收字节并按位置装入 ht_sig1 / ht_sig2：

```verilog
if (byte_out_strobe) begin
    if (byte_count < 3) begin
        ht_sig1 <= {byte_out, ht_sig1[23:8]};
    end else begin
        ht_sig2 <= {byte_out, ht_sig2[23:8]};
    end
    byte_count <= byte_count + 1;
end
```

> **「先收到的 bit 落低位」是怎么形成的？** 每次 `{byte_out, ht_sigN[23:8]}` 都把新字节塞到最高 8 位、老内容整体右移 8 位。连续做 3 次后，最早到的字节被挤到最低 8 位 `[7:0]`，最晚到的留在 `[23:16]`。再加上解码器是 LSB 先出，所以 `ht_sig1[0]` 就是 HT-SIG 物理上第一个发送的比特。这一点对 4.3 节理解 CRC 输入顺序很关键。

#### 4.1.4 代码实践

**实践目标**：确认字段切片与拼装方向一致。

**操作步骤**：

1. 打开 [verilog/dot11.v#L209-L228](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L209-L228)，逐行核对上表的位宽。
2. 在 [verilog/dot11.v#L648-L655](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L648-L655) 旁手算：假设 `ofdm_decoder` 依次吐出 `byte0,byte1,byte2`，写出 3 拍后 `ht_sig1` 的值。

**需要观察的现象**：3 拍后 `ht_sig1 = {byte2, byte1, byte0}`，即 `ht_sig1[7:0] == byte0`。

**预期结果**：`ht_mcs`（`ht_sig1[6:0]`）正好等于 `byte0` 的低 7 位，也就是 HT-SIG 物理上最先发送的 7 个比特 = MCS。这与「MCS 是 HT-SIG 第一个字段」的规范一致。待本地验证：用一个 dot11n 样本仿真，从 `DEBUG_PRINT` 打印的 `[HT SIGNAL]` 行读取 `mcs` 是否落在 0–7。

#### 4.1.5 小练习与答案

**练习 1**：`ht_len` 是 16 位，能表示的最大字节数是多少？它和 legacy 的 `legacy_len`（12 位）相比，为什么 HT 需要更宽？

**参考答案**：\(2^{16}-1 = 65535\)。HT 单包可聚合大量数据（AMPDU），且 length 的语义在 HT 里被重新定义为以时间为单位的值，取值范围更大，故用 16 位。

**练习 2**：`crc` 字段为什么放在 `ht_sig2[17:10]` 而不是某个字的尾部？

**参考答案**：因为 CRC 必须覆盖它**之前**的所有比特（前 34 比特），所以它紧跟在最后一个被校验字段 `ht_num_ext[9:8]` 之后，再后面才是 6 位 tail。这种「校验值紧跟被保护数据、tail 垫后」的布局和 legacy SIGNAL 一致。

---

### 4.2 HT 格式检测与 90° 旋转

#### 4.2.1 概念说明

`S_DETECT_HT` 要回答一个问题：L-SIG 后面那个 OFDM 符号，到底是不是 HT-SIG？判据非常便宜却有效——**HT-SIG 用 Q 路 BPSK**，于是均衡器输出的星座点会呈现 \(|Q| > |I|\)；而 legacy 数据是普通 I 路 BPSK/QAM，\(|I|\) 与 \(|Q|\) 相当。只要数到足够多的「\(|Q| > |I|\)」样本，就认定是 HT-SIG；反之若「\( |I| \ge |Q| \)」样本居多，就把它当作 legacy DATA 继续解。

一旦认定是 HT-SIG，接下来的 `S_HT_SIGNAL` 必须**先把星座点顺时针旋转 90°**，把 Q 路数据搬到 I 路，才能复用与 legacy 完全相同的 BPSK/QAM 解调流水线。

#### 4.2.2 核心流程

检测阶段（`S_DETECT_HT`）：

```
每个 equalizer_out_strobe 样本：
    abs_eq_i = |I|      // 补码取绝对值：~x+1
    abs_eq_q = |Q|
    if (abs_eq_q > abs_eq_i):  rot_eq_count   ++   // 疑似旋转过
    else:                      normal_eq_count ++
    if (rot_eq_count >= 4):   → 认定 HT-SIG，进 S_HT_SIGNAL
    if (normal_eq_count > 4): → 当作 legacy，进 S_DECODE_DATA
```

旋转阶段（`S_HT_SIGNAL`）：对每个送入 `ofdm_decoder` 的复数样本做

\[
(I,\, Q) \;\longrightarrow\; (Q,\, -I)
\]

这等价于复数乘 \(-j\)（顺时针 90°），把原本在虚轴上的 Q 路 BPSK 点搬到实轴。

#### 4.2.3 源码精读

[verilog/dot11.v#L605-L636](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L605-L636) — `S_DETECT_HT`：用绝对值比较计数做格式判定：

```verilog
S_DETECT_HT: begin
    legacy_sig_stb <= 0;
    if (equalizer_out_strobe) begin
        abs_eq_i <= eq_out_i[15]? ~eq_out_i+1: eq_out_i;   // |I|
        abs_eq_q <= eq_out_q[15]? ~eq_out_q+1: eq_out_q;   // |Q|
        if (abs_eq_q > abs_eq_i) begin
            rot_eq_count   <= rot_eq_count + 1;             // |Q|>|I|：疑似 HT
        end else begin
            normal_eq_count <= normal_eq_count + 1;
        end
    end

    if (rot_eq_count >= 4) begin
        // HT-SIG detected
        ...
        num_bits_to_decode <= 96;        // 两个 OFDM 符号 = 96 编码比特
        state <= S_HT_SIGNAL;
    end else if (normal_eq_count > 4) begin
        ...
        state <= S_DECODE_DATA;          // 当 legacy 数据
    end
end
```

两点值得注意：

- `eq_out_i = equalizer_out[31:16]`、`eq_out_q = equalizer_out[15:0]`（见 [verilog/dot11.v#L174-L175](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L174-L175)），取绝对值用的是全项目统一的补码取反加一 `~x+1`。
- 阈值「4」是经验值：sig.rst 指出 HT-SIG 几乎所有 48 个数据子载波都呈 \(|Q|>|I|\)，所以只要 4 个样本命中即可高置信认定；而 `normal_eq_count > 4` 才回退到 legacy，回退门槛稍高一点，避免误判。

旋转在 `S_HT_SIGNAL` 里完成。注意它用的是**延时 6 拍后的** `eq_out_i_delayed / eq_out_q_delayed`（由 `delayT` 实例对齐 `ofdm_decoder` 流水线延时，见 [verilog/dot11.v#L347-L353](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L347-L353)）：

[verilog/dot11.v#L643-L646](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L643-L646) — 顺时针旋转 90°，把 Q 路 BPSK 搬到 I 路：

```verilog
ofdm_in_stb <= eq_out_stb_delayed;
// rotate clockwise by 90 degree
ofdm_in_i <= eq_out_q_delayed;          // 新 I = 老 Q
ofdm_in_q <= ~eq_out_i_delayed+1;       // 新 Q = -老 I
```

验证：复数 \(z = I + jQ\)，乘 \(-j\) 得 \(z\cdot(-j) = Q - jI\)，即新 \((I',Q') = (Q, -I)\)，与代码一致——这正是顺时针（−90°）旋转。

#### 4.2.4 代码实践

**实践目标**：体会「旋转前后星座点位置」，并理解为什么旋转后能直接复用 legacy 解调。

**操作步骤**：

1. 在 `dot11_tb.v` 的 `$dumpvars` 里（若未导出）临时把 `equalizer_out`、`ofdm_in_i`、`ofdm_in_q` 加进波形探针；
2. 用一个 dot11n 样本（如 `testing_inputs` 下的 `dot11n` 样本）跑 `make simulate`；
3. 在 `S_HT_SIGNAL` 区间，对比同一时刻的 `equalizer_out`（旋转前）与 `ofdm_in_i/ofdm_in_q`（旋转后）。

**需要观察的现象**：旋转前 HT-SIG 样本的 Q 分量幅度明显大于 I；旋转后 `ofdm_in_i`（新 I）承载了原来的 Q 路数据，`ofdm_in_q` 接近 0——即变成了普通 I 路 BPSK，下游 `demodulate` 无需任何修改即可解。

**预期结果**：`ofdm_in_i` ≈ 旋转前的 `eq_out_q`，`ofdm_in_q` ≈ −旋转前的 `eq_out_i`。待本地验证具体数值。

#### 4.2.5 小练习与答案

**练习 1**：为什么检测门槛 `rot_eq_count >= 4` 设得这么低（HT-SIG 一个符号有 48 个数据子载波）？

**参考答案**：因为合法 HT-SIG 的几乎全部子载波都满足 \(|Q|>|I|\)，命中比例极高，少量样本就足以高置信判定；同时低门槛也能尽快进入 `S_HT_SIGNAL`，减少漏检带来的时序压力。

**练习 2**：如果把 `ofdm_in_q <= ~eq_out_i_delayed+1` 改成 `ofdm_in_q <= eq_out_i_delayed`（少个取负），会发生什么？

**参考答案**：变成逆时针 90° 旋转（乘 \(+j\)），Q 路数据会被搬到 −I 轴方向，解出的 BPSK 比特全部取反，导致 HT-SIG 字段全错、CRC 必然失败、状态机进 `S_HT_SIG_ERROR`。

---

### 4.3 CRC-8 计算原理（多项式、取反、倒序）

#### 4.3.1 概念说明

`ht_sig_crc.v` 是一个独立的 CRC-8 模块，对输入的 34 个比特串行计算校验值。它有两处看起来「奇怪」的设计，本节专门把它们讲透：

1. **寄存器更新式**只动 `C[0..2]` 并整体移位 `C[7:3] <= C[6:2]`——这对应一个具体的生成多项式；
2. **输出**是 `crc[i] = ~C[7-i]`，同时做了「取反」和「倒序」两件事，二者原因不同。

#### 4.3.2 核心流程

把代码的更新式改写成数学。设当前输入比特为 \(m\)，定义反馈位 \(f = m \oplus C_7\)，则下一拍：

\[
\begin{aligned}
C_0' &= f \\
C_1' &= C_0 \oplus f \\
C_2' &= C_1 \oplus f \\
C_3' &= C_2,\; C_4' = C_3,\; \ldots,\; C_7' = C_6
\end{aligned}
\]

反馈 \(f\) 被异或进新寄存器的第 0、1、2 位，这正是生成多项式

\[
\boxed{\,G(x) = x^8 + x^2 + x + 1\,}
\]

的标志（\(x^8\) 隐含在「移位出最高位」里，低 8 位系数 `0000_0111` = `0x07` 指明反馈接到第 0、1、2 级）。寄存器初值 `8'hff`（全 1），34 拍后输出

\[
\text{crc}_i = \overline{C_{7-i}}, \quad i=0,\ldots,7
\]

即「先按位取反，再把位序倒过来」。

#### 4.3.3 源码精读

[verilog/ht_sig_crc.v#L13-L34](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ht_sig_crc.v#L13-L34) — CRC 寄存器更新主体：

```verilog
reg [7:0] C;
genvar i;

generate
for (i = 0; i < 8; i=i+1) begin: reverse
    assign crc[i] = ~C[7-i];        // 取反 + 倒序输出
end
endgenerate

always @(posedge clock) begin
    if (reset) begin
        C <= 8'hff;                  // 初值全 1
    end else if (enable) begin
        if (input_strobe) begin
            C[0] <= bit ^ C[7];
            C[1] <= bit ^ C[7] ^ C[0];
            C[2] <= bit ^ C[7] ^ C[1];
            C[7:3] <= C[6:2];
        end
    end
end
```

这段与 docs/source/sig.rst 给出的数学定义完全对应（推荐对照阅读）：

[docs/source/sig.rst#L112-L128](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/sig.rst#L112-L128) — CRC 更新方程与「取反输出」的规范描述。

> **注意一个 Verilog 时序细节**：`C[1] <= bit ^ C[7] ^ C[0];` 右边的 `C[0]` 是**非阻塞赋值**，用的是更新前的旧值，所以它确实表示 \(C_1' = C_0 \oplus f\)，而不是 \(C_0' \oplus f\)。`C[2]` 行同理。这一点和数学式严格一致。

现在解释 `crc[i] = ~C[7-i]` 里的两个独立操作：

**（a）取反 `~` —— 来自 CRC 通用约定。** 寄存器初始化为全 1（`8'hff`），最终余数再按位取反。配套使用可让消息前导的连续 0 也参与校验（前文 2.3 节）。这与 u4-l5 的 CRC-32「初始化 `0xffff_ffff` + 末尾异或 `0xffff_ffff`」是同一种手法。

**（b）倒序 `C[7-i]` —— 来自接收端存储布局。** 这是「为了让算出来的期望 CRC 与收到的 CRC 字段在比特上一一对齐，从而只需一次 `XOR` 比对」。

具体推导：

- 802.11n 规范规定 HT-SIG 的 CRC 比特**按发送顺序**是 \(\overline{C_7}, \overline{C_6}, \ldots, \overline{C_0}\)，即最先发送的是 \(\overline{C_7}\)；
- 而 `ht_sig2` 是「先收到的 bit 落低位」（4.1 节），所以最先收到的 CRC 比特落在 `ht_sig2[10]`（CRC 字段最低位），最后收到的落在 `ht_sig2[17]`；
- `wire crc = ht_sig2[17:10]`，于是 `crc[0]` 就是第一个收到的 CRC 比特 = \(\overline{C_7}\)，`crc[7]` = 最后一个 = \(\overline{C_0}\)。

所以要构造期望值与接收值逐位对齐，就必须令 `crc_out[0] = ~C[7]`、`crc_out[7] = ~C[0]`，即 `crc_out[i] = ~C[7-i]`——正是代码的 `assign crc[i] = ~C[7-i]`。这样 `crc_out ^ crc == 0` 就是最简单的「逐位相等」判定，无需任何额外置换。

#### 4.3.4 代码实践（本讲主实践之一）

**实践目标**：把 CRC 多项式写出来，并向自己解释清楚 `~C[7-i]` 的两层含义。

**操作步骤**：

1. 打开 [verilog/ht_sig_crc.v#L23-L34](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ht_sig_crc.v#L23-L34)。
2. 在纸上写出 8 条更新式，把反馈位 \(f = m \oplus C_7\) 代入，化简成 4.3.2 节那种「只有 \(f\) 和旧 \(C_i\)」的形式。
3. 标出 \(f\) 被异或进的位（0、1、2），据此写出 \(G(x) = x^8 + x^2 + x + 1\)。
4. 仿照下表，写出 `crc[i] = ~C[7-i]` 的全部 8 行：

   | 输出位 | 表达式 | 含义 |
   |--------|--------|------|
   | `crc[0]` | `~C[7]` | 第 1 个发送的 CRC 比特（落 `ht_sig2[10]`） |
   | `crc[1]` | `~C[6]` | 第 2 个 |
   | ... | ... | ... |
   | `crc[7]` | `~C[0]` | 第 8 个（落 `ht_sig2[17]`） |

**需要观察的现象**：化简后只有 `C[0..2]` 依赖 \(f\)，`C[3..7]` 纯移位——与多项式只有 \(x^0,x^1,x^2\) 三个低阶非零项一致。

**预期结果**：你能向别人讲清楚「`~` 来自初始化全 1 的通用约定、`7-i` 来自接收端 LSB 先存导致发送顺序与存储顺序相反」。这是本讲的核心理解点。

**延伸（可选）**：手工用初值 `C=0xff`、输入序列全 0（34 个 0）走一遍，验证最终 `crc` 不等于 `0x00`——这正说明取反+全 1 初值让「全 0 消息」也能产生非平凡校验值。

#### 4.3.5 小练习与答案

**练习 1**：把更新式里的 `C[2] <= bit ^ C[7] ^ C[1];` 改成 `C[2] <= bit ^ C[7];`（去掉 `^ C[1]`），多项式会变成什么？

**参考答案**：反馈只进第 0、1 级，对应 \(G'(x) = x^8 + x + 1\)。这不再是 802.11n 规定的多项式，所有合法包的 CRC 都会失败。

**练习 2**：为什么 `assign crc[i] = ~C[7-i]` 用组合逻辑（`assign` + `generate`）而不是时序逻辑？

**参考答案**：因为「取反+倒序」是对当前寄存器值的纯组合映射，不涉及状态；放在 `assign` 里可以让 `crc_out` 在最后一个数据 bit 被消化后**立即**稳定可用，`S_CHECK_HT_SIG_CRC` 在下一拍就能直接读它比对，省掉额外的一拍寄存器延时。

---

### 4.4 CRC 计算时序与字段合法性校验

#### 4.4.1 概念说明

CRC 模块本身只会「来一个 bit、更新一次」，那 34 个 bit 怎么喂、喂完何时比对、比对完还要查什么，全由 `dot11.v` 的状态机调度。本节把 `S_CHECK_HT_SIG_CRC` 的 **36 拍时序**逐拍拆开，并接上 `S_CHECK_HT_SIG` 的字段合法性校验。

#### 4.4.2 核心流程

`S_HT_SIGNAL` 收齐 6 字节（`byte_count == 6`）后，置 `crc_reset <= 1`、`crc_count <= 0` 并进入 `S_CHECK_HT_SIG_CRC`。随后用一个计数器 `crc_count` 调度：

```
crc_count == 0       : 复位 CRC 寄存器到 0xff（本拍 crc_reset 仍有效）
crc_count == 0..23   : 喂 ht_sig1[0..23]   （24 bit）
crc_count == 24..33  : 喂 ht_sig2[0..9]    （10 bit）
crc_count == 34      : 停止喂（crc_in_stb <= 0）
crc_count == 35      : 读 crc_out，与接收到的 crc 比较，决定去留
```

总共 `crc_count` 取 0..35 共 **36 个值**，即状态停留 36 拍。其中 34 拍真正喂入数据（共 34 bit），1 拍收尾断流，1 拍比对。

CRC 通过后进 `S_CHECK_HT_SIG`，按规范逐项检查 OpenOFDM **不支持**的特性，任一不满足即进 `S_HT_SIG_ERROR`。

#### 4.4.3 源码精读

先看 CRC 模块的实例化——注意它的 `reset` 是 `reset | crc_reset`，所以顶层可以用 `crc_reset` 脉冲单独复位它：

[verilog/dot11.v#L384-L392](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L384-L392) — ht_sig_crc 实例化：

```verilog
ht_sig_crc crc_inst (
    .clock(clock),
    .enable(enable),
    .reset(reset | crc_reset),
    .bit(crc_in),
    .input_strobe(crc_in_stb),
    .crc(crc_out)
);
```

进入 CRC 状态前的准备工作（在 `S_HT_SIGNAL` 末尾）：

[verilog/dot11.v#L672-L676](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L672-L676) — 收齐 6 字节后清计数、拉复位、进入 CRC 状态：

```verilog
crc_count <= 0;
crc_reset <= 1;
crc_in_stb <= 0;
ht_sig_crc_ok <= 0;
state <= S_CHECK_HT_SIG_CRC;
```

`S_CHECK_HT_SIG_CRC` 主体——本讲主实践要追踪的 36 拍：

[verilog/dot11.v#L680-L707](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L680-L707) — 逐拍喂入 34 bit 并比对：

```verilog
S_CHECK_HT_SIG_CRC: begin
    crc_reset <= 0;
    crc_count <= crc_count + 1;

    if (crc_count < 24) begin
        crc_in_stb <= 1;
        crc_in     <= ht_sig1[crc_count];          // 喂 ht_sig1[0..23]
    end else if (crc_count < 34) begin
        crc_in_stb <= 1;
        crc_in     <= ht_sig2[crc_count-24];       // 喂 ht_sig2[0..9]
    end else if (crc_count == 34) begin
        crc_in_stb <= 0;                           // 断流
    end else if (crc_count == 35) begin
        if (crc_out ^ crc) begin                   // 不等 → 错
            status_code <= E_WRONG_CRC;
            ht_sig_stb  <= 1;
            state <= S_HT_SIG_ERROR;
        end else begin                             // 相等 → 通过
            ht_sig_crc_ok <= 1;
            ht_sig_stb    <= 1;
            ofdm_reset    <= 1;
            state <= S_CHECK_HT_SIG;
        end
    end
end
```

把这段时序整理成表（`crc_count` 是「本拍 case 里读到的值」，即上一拍写入的值）：

| 本拍读到的 `crc_count` | FSM 动作 | CRC 模块在本拍消化到的 bit |
|---|---|---|
| 0 | 清 `crc_reset`；准备喂 `ht_sig1[0]` | 复位为 `0xff`（`crc_reset` 仍为 1） |
| 1 | 喂 `ht_sig1[1]` | `ht_sig1[0]`（stb 有 1 拍寄存器延时） |
| … | … | … |
| 24 | 喂 `ht_sig2[0]` | `ht_sig1[23]` |
| … | … | … |
| 34 | 断流（`crc_in_stb<=0`） | `ht_sig2[9]`（最后一个数据 bit） |
| 35 | 比对 `crc_out ^ crc` | 不再消化，`crc_out` 已稳定 |

> 之所以数据 bit 的「消化」比 `crc_count` 滞后 1 拍，是因为 `crc_in`/`crc_in_stb` 都是非阻塞赋值，下一拍才被 CRC 模块采样。这正是为什么比对要放在 `crc_count == 35`（而不是 34）：到 35 拍时，第 34 个 bit 已在 34 拍被消化完毕，组合输出的 `crc_out` 已反映全部 34 bit。

CRC 通过后，`S_CHECK_HT_SIG` 做「能力围栏」式校验——把 OpenOFDM 不支持的特性逐个拒掉：

[verilog/dot11.v#L709-L740](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L709-L740) — 字段合法性校验：

```verilog
S_CHECK_HT_SIG: begin
    ofdm_reset <= 0;
    ht_sig_stb <= 0;
    if (ht_mcs > 7)            begin ... E_UNSUPPORTED_MCS;    ... end
    else if (ht_cbw)           begin ... E_UNSUPPORTED_CBW;    ... end
    else if (ht_rsvd == 0)     begin ... E_HT_WRONG_RSVD;      ... end
    else if (ht_stbc != 0)     begin ... E_UNSUPPORTED_STBC;   ... end
    else if (ht_fec_coding)    begin ... E_UNSUPPORTED_FEC;    ... end
    else if (short_gi)         begin ... E_UNSUPPORTED_SGI;    ... end
    else if (ht_num_ext != 0)  begin ... E_UNSUPPORTED_SPATIAL;... end
    else if (ht_sig_tail != 0) begin ... E_HT_WRONG_TAIL;      ... end
    else begin
        sync_long_out_count <= 0;
        state <= S_HT_STS;                          // 进入 HT-STS / HT-LTS
    end
end
```

逐条解读：

- `ht_mcs > 7`：OpenOFDM 只支持 MCS 0–7（单流）；
- `ht_cbw`：仅支持 20MHz，40MHz 直接拒；
- `ht_rsvd == 0`：**注意这一条**。代码把 `ht_rsvd == 0` 当作错误，意味着该保留位**必须为 1** 才能通过。
  > ⚠️ 这与 [docs/source/sig.rst#L93-L94](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/sig.rst#L93-L94) 文字「Reserved: must be 0」**相反**。代码是运行时的真实行为（保留位须为 1），文档此处疑似笔误。请用真实 dot11n 抓包验证该位的实际取值。
- `ht_stbc != 0`：不支持空时分组码；
- `ht_fec_coding`：只支持 BCC（0），不支持 LDPC；
- `short_gi`：注意这里查的是顶层寄存器 `short_gi`（在 `S_HT_LTS` 才被赋值为 `ht_sgi`，见 [verilog/dot11.v#L759](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L759)），此处它仍保持上一包的值——待本地验证该时序对首包的影响；
- `ht_num_ext != 0`：不支持多空间流扩展；
- `ht_sig_tail != 0`：6 位尾必须全 0（让卷积网格回零）。

全部通过后进 `S_HT_STS`（跳过一个 HT-STS 符号），再到 `S_HT_LTS`（用 HT-LTS 重做信道估计，`ht_next <= 1` 让 `equalizer` 切到 52 子载波模式，见 [verilog/dot11.v#L747-L775](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L747-L775)），最终在 `S_DECODE_DATA` 与 legacy 分支汇合。

#### 4.4.4 代码实践（本讲主实践之二）

**实践目标**：在源码里亲眼「数」出 36 拍，并定位 CRC 通过/失败的分流。

**操作步骤**：

1. 从 [verilog/dot11.v#L672-L676](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L672-L676)（进入 CRC 状态）读到 [verilog/dot11.v#L680-L707](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L680-L707)（CRC 状态体）。
2. 列出 `crc_count` 从 0 到 35 每一拍进入哪个 `if` 分支、喂的是哪个 bit，按 4.4.3 节的表格填满。
3. 数总拍数：`crc_count` 取值集合是 \(\{0,1,\ldots,35\}\)，共 36 个。
4. 用 `make simulate` 跑一个 dot11n 样本，开 `DEBUG_PRINT`（在 `dot11_tb.v` 或编译选项里定义 `DEBUG_PRINT`），观察终端是否打印 `[HT SIGNAL] CRC OK`。

**需要观察的现象**：状态在 `S_CHECK_HT_SIG_CRC` 停留 36 拍后，要么打印 `CRC OK` 并进 `S_CHECK_HT_SIG`，要么 `status_code` 变成 `E_WRONG_CRC`（=9）并进 `S_HT_SIG_ERROR`。

**预期结果**：合法 dot11n 样本应走「CRC OK → 字段校验通过 → `S_HT_STS`」路径。若你故意改坏 `ht_sig2`（仿真里翻转某 bit），应看到 `E_WRONG_CRC`。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么比对放在 `crc_count == 35` 而不是 `== 34`？

**参考答案**：第 34 个数据 bit（`ht_sig2[9]`）是在 `crc_count == 33` 那拍被送上 `crc_in`、在 `crc_count == 34` 那拍才被 CRC 模块采样消化。所以只有到 `crc_count == 35` 时，组合输出的 `crc_out` 才反映了全部 34 个 bit，此时比对才有效。

**练习 2**：`S_CHECK_HT_SIG` 里 `short_gi` 的检查为什么可能有「首包陷阱」？

**参考答案**：`short_gi` 这个顶层寄存器要到后面的 `S_HT_LTS` 才被赋值为当前包的 `ht_sgi`（[verilog/dot11.v#L759](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L759)）。所以在 `S_CHECK_HT_SIG` 这一刻，它持有的是**上一包**的 SGI 值（复位后为 0）。若上一包用了 SGI、本包没用，理论上可能误判。实际是否触发取决于包间状态保留情况，待本地验证。

---

## 5. 综合实践

**任务：给一个 dot11n 包画出完整的「HT-SIG 控制平面时间线」。**

把本讲四个最小模块串起来，做一次端到端的源码追踪：

1. **准备**：在 `verilog/` 下用 `make compile` 编译，确保 `testing_inputs` 下有一个 dot11n 样本（参考 [u1-l2](u1-l2-environment-and-simulation.md) 的环境搭建）。
2. **开调试打印**：在 `dot11_tb.v` 顶部或编译命令里定义 `DEBUG_PRINT` 宏（若 Makefile 未提供开关，可临时在 [verilog/dot11.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) 的 `\`ifdef DEBUG_PRINT` 处确认宏名）。
3. **跑仿真**：`make simulate`，从终端输出里摘出 `[SIGNAL]` 和 `[HT SIGNAL]` 两行。
4. **画时间线**：在一张图上标出下列事件及其相对节拍：
   - `S_CHECK_SIGNAL` 判定 `legacy_rate == 4'b1011` → 进 `S_DETECT_HT`；
   - `S_DETECT_HT` 累计到 `rot_eq_count == 4` → 进 `S_HT_SIGNAL`（记下 90° 旋转开始）；
   - `S_HT_SIGNAL` 收齐 6 字节（`byte_count` 0→6）→ 进 `S_CHECK_HT_SIG_CRC`；
   - `S_CHECK_HT_SIG_CRC` 停留 36 拍，标注「喂 ht_sig1×24 / 喂 ht_sig2×10 / 断流 / 比对」四段；
   - `S_CHECK_HT_SIG` 逐项过 8 道围栏 → `S_HT_STS`。
5. **手算验证 CRC**：从打印的 `[HT SIGNAL]` 行读出 MCS/CBW/length 及各字段，手算前 34 bit 的 CRC-8（用 \(G(x)=x^8+x^2+x+1\)、初值 `0xff`、末尾取反+倒序），与你实现的脚本或在线 CRC-8（poly=0x07, init=0xff, refin/refout 视工具而定）对照，确认与打印的 `crc` 字段一致。
6. **故障注入**（可选）：在 `dot11_tb.v` 加载样本后，人为翻转 `ht_sig2` 中 CRC 字段的一 bit，重跑，确认状态机进 `S_HT_SIG_ERROR` 且 `status_code == E_WRONG_CRC`（=9）。

**交付物**：一张 HT-SIG 控制平面时间线图 + 一份 CRC 手算过程。能完成第 5 步，说明你真正吃透了「多项式 + 取反 + 倒序」三件事。

---

## 6. 本讲小结

- HT-SIG 跨两个 OFDM 符号、48 数据比特，拼成 `ht_sig1`（MCS/CBW/Length）与 `ht_sig2`（能力位 + CRC + tail），字段全部用顶层 `assign` 切片，遵循「先收到 bit 落低位」。
- `S_DETECT_HT` 用极便宜的判据——`|Q| > |I|` 样本数 ≥ 4——认出 Q 路 BPSK 的 HT-SIG，否则回退当 legacy DATA。
- 认定后 `S_HT_SIGNAL` 做 \((I,Q)\to(Q,-I)\) 的顺时针 90° 旋转（乘 \(-j\)），把 Q 路数据搬到 I 路以复用 legacy 解调流水线。
- `ht_sig_crc.v` 实现的是 \(G(x)=x^8+x^2+x+1\)（低 8 位 `0x07`）的 CRC-8，寄存器初值 `0xff`。
- 输出 `crc[i] = ~C[7-i]` 包含两层含义：`~` 来自「初值全 1 + 末尾取反」的通用 CRC 约定；`7-i` 来自「发送顺序与 ht_sig2 存储顺序相反」，使期望值与接收字段逐位对齐，比对退化为一次 `XOR`。
- `S_CHECK_HT_SIG_CRC` 停留 36 拍（`crc_count` 0..35）：喂 24+10=34 bit、断流 1 拍、比对 1 拍；通过后 `S_CHECK_HT_SIG` 用 8 道围栏拒掉所有 OpenOFDM 不支持的 HT 特性，再进 HT-STS/HT-LTS。
- ⚠️ 代码要求 `ht_rsvd` 为 1，而 sig.rst 文字说「must be 0」，二者矛盾，需以真实抓包为准。

---

## 7. 下一步学习建议

- **向下游走**：`S_HT_STS` → `S_HT_LTS` → `S_DECODE_DATA` 这段在 [verilog/dot11.v#L747-L775](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L747-L775)，重点看 `ht_next <= 1` 如何让 `equalizer` 从 48 子载波切到 52 子载波（回顾 [u3-l1 equalizer](u3-l1-equalizer.md)）。
- **向校验族走**：本讲的 CRC-8 与 u4-l5 的 CRC-32 是同一类思想的不同实例，建议两篇对照阅读，巩固「初值全 1 + 末尾取反」与位序约定的通用模式。
- **回头补全景**：若想再看一次 HT-mixed 的整体帧结构与 L-SIG 欺骗机制，可重读 [docs/source/sig.rst#L33-L63](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/sig.rst#L33-L63)。
- **动手扩展（预告 u6-l5）**：若要支持 MCS 8/9（256-QAM），需要同时改 `demodulate` 星座、`deinterleave`/`equalizer` 的子载波参数、并放开 `S_CHECK_HT_SIG` 里的 `ht_mcs > 7` 围栏——本讲的字段校验段正是扩展的「闸门」所在。
