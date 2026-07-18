# legacy SIGNAL 字段解析与校验

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 802.11a/g 的 SIGNAL 字段在 24 个比特里是如何划分的（rate / rsvd / length / parity / tail）。
- 在 `dot11.v` 中追踪 `signal_bits` 这个 24 位移位寄存器是如何把 3 个字节「拼」成完整 SIGNAL 的，并解释「先到的字节落在低位」的原因。
- 手算 SIGNAL 的偶校验位，并对照代码 `~^signal_bits[17:0]` 理解它为什么等价于「bits 0–17 中 1 的个数为偶数」。
- 解释 `S_CHECK_SIGNAL` 状态里的三重校验（parity / rsvd / tail）以及对应的错误码 `E_PARITY_FAIL` / `E_WRONG_RSVD` / `E_WRONG_TAIL`。
- 理解 `(legacy_len+3)<<4` 这个长度公式的拆解，以及它在 `ofdm_decoder.v` 里到底被用来做什么。

本讲是 u4-l1（dot11 顶层状态机）的承接篇，只聚焦控制平面里「SIGNAL 解析与校验」这一小段，不再展开前端同步与数据解码的算法细节。

## 2. 前置知识

在进入源码前，先用通俗语言把三个概念讲清楚。

### 2.1 SIGNAL 字段是干什么用的

802.11 OFDM 包在长训练序列（LTS）之后的第一个 OFDM 符号叫 **SIGNAL**。它只干两件事：

1. 告诉接收端这个包用什么**调制速率**（rate）发送。
2. 告诉接收端这个包的**载荷有多长**（length）。

没有这两条信息，接收端既不知道「该怎么解」，也不知道「要解多久」。所以解码器必须先把 SIGNAL 这个符号解出来、校验通过，才能继续解后面的 DATA。文档 [docs/source/sig.rst:4-7](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/sig.rst#L4-L7) 一开篇就点明了这一点。

### 2.2 卷积码与「尾比特」

发射端用 1/2 码率的卷积码对 SIGNAL 编码：24 个原始比特编成 48 个编码比特，恰好塞满一个 OFDM 符号（48 个数据子载波 × 1 比特）。为了让卷积码的网格在符号末尾**回到零状态**（方便接收端 Viterbi 译码），SIGNAL 的最后 6 个比特固定为 0，称为 **tail（尾比特）**。这正是后面 `tail` 校验的依据。

### 2.3 偶校验（even parity）

**偶校验**就是额外加 1 个比特，让「数据位 + 校验位」里 1 的总个数变成偶数。例如数据有 5 个 1（奇数），校验位就设成 1，凑成 6 个 1（偶数）。接收端只要数一下 1 的个数是不是偶数，就能发现单比特错误。SIGNAL 的 bit 17 就是这样一个偶校验位，覆盖它前面的 17 个比特（bit 0–16）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [verilog/dot11.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) | 顶层模块。本讲关注其中 `signal_bits` 的声明、SIGNAL 字段位域的 `assign`，以及 `S_DECODE_SIGNAL` / `S_CHECK_SIGNAL` / `S_SIGNAL_ERROR` 三个状态。 |
| [verilog/common_params.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v) | 状态码 `S_*` 与错误码 `E_*` 的定义，包括本讲的 `E_PARITY_FAIL` 等。 |
| [docs/source/sig.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/sig.rst) | SIGNAL / HT-SIG 字段格式与校验项的权威说明。 |
| [verilog/rate_to_idx.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rate_to_idx.v) | rate 4 比特到速率（6/9/.../54 Mbps）的映射，用来核对 SIGNAL 里 rate 的取值。 |
| [verilog/dot11_tb.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v) | 测试台。本讲关注它如何把解析出的 SIGNAL 五个字段写进 `sim_out/signal_out.txt`。 |
| [verilog/ofdm_decoder.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v) | 子流水线。本讲只用它来印证 `num_bits_to_decode` 这个量到底被谁消费。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：SIGNAL 的位域结构、字节拼装移位寄存器、三重校验与错误状态、以及长度公式 `(legacy_len+3)<<4`。

### 4.1 SIGNAL 字段的位域结构

#### 4.1.1 概念说明

802.11a/g 的 SIGNAL 一共 24 比特，文档 [docs/source/sig.rst:18-20](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/sig.rst#L18-L20) 说明它经 1/2 卷积编码后展开成 48 比特，正好装满一个 OFDM 符号。这 24 比特按从低位到高位分成 5 段：

| 比特位 | 字段 | 位宽 | 含义 | 接收端期望 |
|--------|------|------|------|-----------|
| 0–3 | RATE | 4 | 调制速率编码 | 必须是合法速率 |
| 4 | RSVD | 1 | 保留位 | 应为 0 |
| 5–16 | LENGTH | 12 | PSDU 字节数 | 决定要解多少数据 |
| 17 | PARITY | 1 | bits 0–16 的偶校验 | 使 bits 0–17 中 1 的个数为偶 |
| 18–23 | TAIL | 6 | 卷积码尾比特 | 应全为 0 |

文档把校验项列得很清楚 [docs/source/sig.rst:25-27](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/sig.rst#L25-L27)：bit 17 是前 17 位的偶校验，bit 4 保留位应为 0，最后 6 位应全 0。

#### 4.1.2 rate 4 比特到底对应哪个速率

在 OpenOFDM 里，SIGNAL 的 4 个比特是**按接收顺序、低位在前**存进 `signal_bits` 的（bit 0 先到）。把 [verilog/rate_to_idx.v:23-55](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rate_to_idx.v#L23-L55) 里的标注还原，可以得到 `legacy_rate`（即 `signal_bits[3:0]`）与速率的对应关系：

| `legacy_rate[3:0]` | 速率 |
|--------------------|------|
| `1011` | 6 Mbps |
| `1111` | 9 Mbps |
| `1010` | 12 Mbps |
| `1110` | 18 Mbps |
| `1001` | 24 Mbps |
| `1101` | 36 Mbps |
| `1000` | 48 Mbps |
| `1100` | 54 Mbps |

注意两点：

1. 所有合法 802.11a 速率的 `legacy_rate[3]`（最高位）都是 1。这也是 `rate_to_idx` 只看 `rate[2:0]` 就能区分 8 个速率的原因——最高位恒为 1，没有信息量。
2. **6 Mbps 对应的是 `4'b1011`，不是直觉里的 `0110`**。这是低位在先的存储方式造成的。这点非常关键，因为顶层状态机正是用 `legacy_rate == 4'b1011` 来识别「这可能是 802.11n 的 HT-mixed 包」（详见 u4-l1 与 4.3 节）。

#### 4.1.3 源码精读：位域就是一行 `assign`

SIGNAL 的 5 段在 `dot11.v` 里并不是临时拼出来的，而是把 24 位的 `signal_bits` 直接按位切片 `assign` 出来，见 [verilog/dot11.v:198-206](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L198-L206)：

```verilog
// SIGNAL information
reg [23:0] signal_bits;          // 24 位拼装寄存器
...
assign legacy_rate       = signal_bits[3:0];   // RATE
assign legacy_sig_rsvd   = signal_bits[4];     // RSVD
assign legacy_len        = signal_bits[16:5];  // LENGTH（12 位）
assign legacy_sig_parity = signal_bits[17];    // PARITY
assign legacy_sig_tail   = signal_bits[23:18]; // TAIL（6 位）
assign legacy_sig_parity_ok = ~^signal_bits[17:0]; // 偶校验结果
```

这段代码做的事情：声明一个 24 位寄存器 `signal_bits`，然后用 6 行 `assign` 把它「切片」成对外可见的 5 个字段加 1 个校验结果。位的划分和上表完全一致。其中 `legacy_sig_parity_ok` 的写法 `~^signal_bits[17:0]` 是本讲的核心，4.3 节会专门拆开。

> 永久链接：[verilog/dot11.v:201-206](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L201-L206) ——这 6 行就是 SIGNAL 位域定义的全部。

#### 4.1.4 代码实践：核对位宽

**实践目标**：确认 5 个字段的位宽加起来正好是 24。

**操作步骤**：

1. 打开 [verilog/dot11.v:201-205](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L201-L205)。
2. 数每个切片的位宽：`[3:0]`=4、`[4]`=1、`[16:5]`=12、`[17]`=1、`[23:18]`=6。
3. 求和：\( 4+1+12+1+6 = 24 \)。

**需要观察的现象**：5 段恰好无重叠、无遗漏地覆盖了 `signal_bits[23:0]` 的全部 24 位，且彼此相邻（`[3:0]` 之后紧接着 `[4]`，再 `[16:5]`……）。

**预期结果**：位宽之和为 24，字段在比特轴上首尾相接，证明位域划分是「紧凑无空洞」的。

#### 4.1.5 小练习与答案

**练习 1**：如果 `legacy_len` 显示是 `000000010000`（二进制），对应的字节数是多少？

**答案**：`000000010000`₂ = 16，所以 PSDU 长度是 16 字节。

**练习 2**：为什么 `rate_to_idx.v` 只比较 `rate[2:0]` 而不看完整的 4 位 rate？

**答案**：因为 8 个合法 802.11a 速率的 `legacy_rate[3]` 恒为 1（见 4.1.2 的表），最高位不携带区分信息，只用低 3 位就能唯一识别速率，省去一个比较位。

---

### 4.2 字节拼装：signal_bits 移位寄存器

#### 4.2.1 概念说明

SIGNAL 在物理上是从 `ofdm_decoder` 子流水线**一个字节一个字节**吐出来的（Viterbi 译码 → 解扰 → 组字节，见 u3-l5/u3-l6）。而 SIGNAL 字段是 24 比特 = 3 字节。所以 `dot11.v` 需要一个地方把这 3 个字节**按正确的顺序拼回** 24 比特——这就是 `signal_bits` 移位寄存器的任务。

关键直觉：802.11 的 SIGNAL 是**低位先发**（bit 0 最先到），所以最先收到的字节应当落在 24 位结果的**低位**。

#### 4.2.2 核心流程

`S_DECODE_SIGNAL` 状态做的事可以概括成三步伪代码：

```
进入 S_DECODE_SIGNAL 时：byte_count = 0
每收到一个 byte_out_strobe：
    把 byte_out 塞进 signal_bits 的最高字节，原内容整体右移 8 位
    byte_count++
当 byte_count 计到 3（收满 3 字节）：
    进入 S_CHECK_SIGNAL 做校验
```

「塞进最高字节、整体右移 8 位」用 Verilog 写就是拼接：

```verilog
signal_bits <= {byte_out, signal_bits[23:8]};
```

它的含义是：新的 `signal_bits[23:16] = byte_out`，而新的 `signal_bits[15:0] = 原来的 signal_bits[23:8]`（即原内容向下挪了 8 位）。

跟踪 3 次写入（B0 最先到，B2 最后到）：

| 时刻 | 操作 | signal_bits 结果 |
|------|------|------------------|
| 收到 B0 | `{B0, old[23:8]}` | `B0` 在 `[23:16]`，低位为 0 |
| 收到 B1 | `{B1, prev[23:8]}` | `B1` 在 `[23:16]`，`B0` 被挤到 `[15:8]` |
| 收到 B2 | `{B2, prev[23:8]}` | `B2` 在 `[23:16]`，`B1` 在 `[15:8]`，`B0` 在 `[7:0]` |

最终 `signal_bits = {B2, B1, B0}`：**最先到的 B0 落在最低字节 `[7:0]`**，正好对应「bit 0 先发」的约定，于是 `signal_bits[3:0]`（RATE）就正确地落在 B0 的低位。

#### 4.2.3 源码精读：S_DECODE_SIGNAL 状态

完整状态见 [verilog/dot11.v:532-561](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L532-L561)：

```verilog
S_DECODE_SIGNAL: begin
    if (ofdm_reset) begin
        ofdm_reset <= 0;
    end

    if (equalizer_reset) begin
        equalizer_reset <= 0;
    end

    // 把均衡器输出接力给 ofdm_decoder
    ofdm_in_stb <= equalizer_out_strobe;
    ofdm_in_i  <= eq_out_i;
    ofdm_in_q  <= eq_out_q;

    // 每收到一个字节就移位拼装
    if (byte_out_strobe) begin
        signal_bits <= {byte_out, signal_bits[23:8]};
        byte_count  <= byte_count + 1;
    end

    // 收满 3 字节，转去校验
    if (byte_count == 3) begin
        byte_count <= 0;
        ofdm_reset <= 1;
        state <= S_CHECK_SIGNAL;
    end
end
```

要点：

- `ofdm_in_*` 那三行只是把均衡后的复数样本转交给 `ofdm_decoder`，属于数据平面接力，本讲不展开。
- 真正的拼装就是 `signal_bits <= {byte_out, signal_bits[23:8]}` 这一行 [verilog/dot11.v:546](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L546)。
- `byte_count == 3` 的判断 [verilog/dot11.v:550](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L550) 读取的是**本拍之前**的 `byte_count`（非阻塞赋值，判断用的是旧值），所以恰好等 3 个 `byte_out_strobe` 脉冲收齐 B0/B1/B2 后再跳转。

还有一个细节值得注意：进入 `S_DECODE_SIGNAL` 之前，`pkt_rate` 被预置成了 6 Mbps 的编码值，见 [verilog/dot11.v:517-519](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L517-L519)：

```verilog
pkt_rate <= {1'b0, 3'b0, 4'b1011};   // 6 Mbps = BPSK 1/2
do_descramble <= 0;
num_bits_to_decode <= 48;            // SIGNAL: 24 数据位 ×2(1/2) = 48 编码位
```

这是因为**在解出 SIGNAL 之前，接收端根本不知道速率**，只能按 802.11 的强制速率 6 Mbps（BPSK 1/2）去解这个符号——SIGNAL 永远用 6 Mbps 发送。这里同时把 `num_bits_to_decode` 设成 48，呼应 4.4 节的长度公式。

#### 4.2.4 代码实践：用波形验证「先到的字节落低位」

**实践目标**：在仿真波形里亲眼看到 B0 落在 `signal_bits[7:0]`。

**操作步骤**：

1. 在 `verilog/` 目录执行 `make compile && make simulate`（默认样本就是 24 Mbps 的 802.11a 包，见 [verilog/dot11_tb.v:82-83](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L82-L83)）。
2. 用 `gtkwave dot11.vcd` 打开波形。
3. 把 `byte_out`、`byte_out_strobe`、`signal_bits[7:0]`、`signal_bits[15:8]`、`signal_bits[23:16]`、`state` 一起拖进波形窗口。
4. 把时间光标移到 `state == S_DECODE_SIGNAL`（状态码 3）的区间。

**需要观察的现象**：

- 第 1 个 `byte_out_strobe` 脉冲后，`signal_bits[23:16]` 出现第 1 个字节，`[15:0]` 仍为 0。
- 第 2 个脉冲后，第 1 个字节「下移」到 `[15:8]`，`[23:16]` 变成第 2 个字节。
- 第 3 个脉冲后，第 1 个字节出现在 `[7:0]`。

**预期结果**：3 个字节按「新字节进高位、旧字节往下挤」的方式积累，最先到的字节最终落在 `signal_bits[7:0]`。

**说明**：如果本地没有装 `gtkwave`，也可以只跑 `make simulate` 然后读 `sim_out/byte_out.txt` 的前 3 行（SIGNAL 的 3 字节），手动验证它们与 4.4 节 `signal_out.txt` 里解析出的字段一致即可。

#### 4.2.5 小练习与答案

**练习 1**：如果把拼装语句改成 `signal_bits <= {signal_bits[15:0], byte_out}`（左移），SIGNAL 还能正确解析吗？

**答案**：不能。这样最先到的字节会落在**高位**，与「bit 0 先发、应落低位」的约定相反，`legacy_rate`（`[3:0]`）会取到错误的比特。

**练习 2**：为什么判断条件是 `byte_count == 3` 而不是 `== 2`？明明 SIGNAL 只有 3 字节。

**答案**：`byte_count` 在每个 `byte_out_strobe` 后自增，但判断用的是自增**之前**的值。从 0 开始，依次读到 0、1、2 时各接收一个字节（共 3 个），等到读到 3 时说明 3 个字节已收齐，此时才跳转。所以 `== 3` 恰好对应「已收满 3 字节」。

---

### 4.3 三重校验与错误状态 E_PARITY_FAIL 等

#### 4.3.1 概念说明

3 个字节拼好后，`dot11.v` 进入 `S_CHECK_SIGNAL` 状态做**合法性校验**。文档 [docs/source/sig.rst:29-31](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/sig.rst#L29-L31) 说得很直白：任一项校验失败就立刻停止解码、等待下一个包。OpenOFDM 检查三项：

1. **偶校验**（parity）：bits 0–17 中 1 的个数应为偶数。
2. **保留位**（rsvd）：bit 4 应为 0。
3. **尾比特**（tail）：bits 18–23 应全为 0。

校验的目的是**尽早剔除解码错误的包**：SIGNAL 解错了，后面 DATA 一定也是错的，没必要浪费资源继续解。

#### 4.3.2 核心流程：偶校验的数学

偶校验位 \(p\)（bit 17）由发射端按下面式子生成，使包含 \(p\) 在内的 18 位中 1 的个数为偶：

\[
p = b_0 \oplus b_1 \oplus \cdots \oplus b_{16}
\]

接收端的通过条件是：

\[
b_0 \oplus b_1 \oplus \cdots \oplus b_{16} \oplus p = 0
\]

也就是 bits 0–17 共 18 位的「异或约简」等于 0（1 的个数为偶）。代码里这一步写成：

```verilog
assign legacy_sig_parity_ok = ~^signal_bits[17:0];
```

- `^signal_bits[17:0]` 是**约简异或**：把 18 个比特异或在一起，结果为 1 表示 1 的个数是奇数。
- `~` 取反：结果为 1 表示 1 的个数是偶数 = 校验通过。

所以 `legacy_sig_parity_ok` 为 1 就是「偶校验通过」。

#### 4.3.3 源码精读：S_CHECK_SIGNAL 状态

完整状态见 [verilog/dot11.v:563-599](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L563-L599)。它的结构是一个 `if / else if / else if / else` 链，按顺序查三项，任一失败就跳到 `S_SIGNAL_ERROR`：

```verilog
S_CHECK_SIGNAL: begin
    if (ofdm_reset) ofdm_reset <= 0;

    if (~legacy_sig_parity_ok) begin               // ① 偶校验
        status_code <= E_PARITY_FAIL;
        state <= S_SIGNAL_ERROR;
    end else if (legacy_sig_rsvd) begin            // ② 保留位应为 0
        status_code <= E_WRONG_RSVD;
        state <= S_SIGNAL_ERROR;
    end else if (|legacy_sig_tail) begin           // ③ 尾比特应全 0
        status_code <= E_WRONG_TAIL;
        state <= S_SIGNAL_ERROR;
    end else begin                                  // 全部通过
        legacy_sig_stb <= 1;
        status_code <= E_OK;
        if (legacy_rate == 4'b1011) begin           // 6 Mbps → 可能是 HT 包
            ...
            state <= S_DETECT_HT;
        end else begin                              // 普通 legacy DATA
            pkt_rate <= {1'b0, 3'b0, legacy_rate};
            num_bits_to_decode <= (legacy_len+3)<<4;
            ...
            state <= S_DECODE_DATA;
        end
    end
end
```

逐条说明：

- **① parity**：`~legacy_sig_parity_ok` 为真（即校验失败）→ `E_PARITY_FAIL` [verilog/dot11.v:568-570](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L568-L570)。
- **② rsvd**：`legacy_sig_rsvd`（bit 4）非 0 → `E_WRONG_RSVD` [verilog/dot11.v:571-573](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L571-L573)。
- **③ tail**：`|legacy_sig_tail` 是 6 位尾比特的约简或，非 0（即有任一比特为 1）→ `E_WRONG_TAIL` [verilog/dot11.v:574-576](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L574-L576)。
- **全部通过**：拉高 `legacy_sig_stb`（通知外部「SIGNAL 有效」，测试台就是靠它写 `signal_out.txt` 的），然后按 rate 分流。

错误码定义在 [verilog/common_params.v:50-54](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/common_params.v#L50-L54)（注：这些路径是仓库内的 `verilog/common_params.v`）：

```verilog
// errors in SIGNAL
localparam E_PARITY_FAIL      = 1;
localparam E_UNSUPPORTED_RATE = 2;
localparam E_WRONG_RSVD       = 3;
localparam E_WRONG_TAIL       = 4;
```

> 永久链接：[verilog/common_params.v:50-54](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L50-L54)。注意这里还定义了一个 `E_UNSUPPORTED_RATE=2`，但在当前 `S_CHECK_SIGNAL` 里并没有显式用到——因为所有 8 个合法速率都能通过 parity/rsvd/tail 三项检查，非法速率会先在 parity 或 tail 上暴露。这是一个「定义了但当前路径未触发」的错误码，了解即可。

`S_SIGNAL_ERROR` 状态本身很简单，就是回到起点等下一个包 [verilog/dot11.v:601-603](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L601-L603)：

```verilog
S_SIGNAL_ERROR: begin
    state <= S_WAIT_POWER_TRIGGER;
end
```

#### 4.3.4 代码实践：手算 parity 并和仿真比对

**实践目标**：从 `signal_out.txt` 读出 5 个字段，手算偶校验，确认 `legacy_sig_parity_ok`。

**操作步骤**：

1. 在 `verilog/` 执行 `make compile && make simulate`。
2. 打开 `verilog/sim_out/signal_out.txt`。它的格式由 [verilog/dot11_tb.v:199-203](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L199-L203) 决定：

   ```verilog
   $fwrite(signal_fd, "%04b %b %012b %b %06b",
           legacy_rate, legacy_sig_rsvd, legacy_len, legacy_sig_parity, legacy_sig_tail);
   ```

   也就是一行 5 个字段，空格分隔：`RATE(4) RSVD(1) LEN(12) PARITY(1) TAIL(6)`。

3. 把前 17 位（RATE 4 + RSVD 1 + LEN 12）抄出来，数其中 1 的个数，记为 \(c\)。
4. 看 PARITY 位：若 \(c\) 为奇数，PARITY 应为 1；若 \(c\) 为偶数，PARITY 应为 0——这才能让 18 位中 1 的总数为偶。
5. 数 bits 0–17（前 17 位 + PARITY）里 1 的总数，确认是偶数。

**需要观察的现象**：

- 默认 24 Mbps 样本的 RATE 应为 `1001`（对应 24 Mbps，见 4.1.2）。
- 手算得到的「应有 PARITY」与文件里的 PARITY 位一致。
- bits 0–17 的 1 的总数为偶数 → 等价于 `legacy_sig_parity_ok == 1`。

**预期结果**：手算 PARITY 与文件 PARITY 一致，校验通过。同时 `signal_out.txt` 只有在三项校验**全部通过**时才会被写入（因为 `legacy_sig_stb` 只在 `else` 分支拉高），所以你能读到这一行本身就说明校验已过。

**说明**：若你担心 TAIL 是否为 0，直接看 `signal_out.txt` 最后的 6 位，应为 `000000`。RSVD（第 5 列）应为 `0`。

#### 4.3.5 小练习与答案

**练习 1**：假设 `signal_out.txt` 某行为 `1001 0 000000010000 1 000000`，手算 parity 是否通过？（RATE=`1001`、RSVD=`0`、LEN=`000000010000`、PARITY=`1`）

**答案**：前 17 位 = `1001` + `0` + `000000010000`，数 1 的个数：`1001` 有 2 个 1，`0` 有 0 个，`000000010000` 有 1 个，合计 \(c=3\)（奇数）。偶校验要求 PARITY=1 凑成偶数，文件里 PARITY 正是 1，所以 18 位里 1 的总数 = 4（偶），**校验通过**。

**练习 2**：为什么 `S_CHECK_SIGNAL` 里没有对 `legacy_rate` 是否合法做显式判断（没有用到 `E_UNSUPPORTED_RATE`）？

**答案**：所有 8 个合法速率编码都能通过 parity/rsvd/tail 三项检查；而一个随机/错误的 rate 几乎必然会让 parity 或 tail 校验失败（因为 parity 覆盖了 rate 这 4 位）。所以三项检查已经隐式过滤了大部分非法速率，`E_UNSUPPORTED_RATE` 在当前实现里是「定义了但主路径未显式触发」的备用码。

---

### 4.4 长度公式 num_bits_to_decode = (legacy_len+3)<<4

#### 4.4.1 概念说明

校验通过且判定为普通 legacy DATA 后，`S_CHECK_SIGNAL` 要告诉 `ofdm_decoder`「这个包要解多少比特」。这个量就是 `num_bits_to_decode`，对 DATA 分支它的值是：

\[
\text{num\_bits\_to\_decode} = (\text{legacy\_len} + 3) \ll 4 = (\text{legacy\_len} + 3) \times 16
\]

理解这个公式要先理解 `num_bits_to_decode` 的单位：它被传给 `ofdm_decoder.v`，与那里的 `deinter_out_count` 比较（`deinter_out_count` 每次 `deinterleave_out_strobe` 加 2，统计的是**解交织输出端的比特数**）。解交织做了去穿孔（de-puncture），把任意码率的比特流还原成 1/2 码率的节奏，所以这个计数是在「1/2 码率节奏的编码比特」意义上。

#### 4.4.2 核心流程：公式拆解

把公式按物理意义拆开：

\[
(\text{legacy\_len} + 3) \times 16 = (\text{legacy\_len} + 3) \times 8 \times 2
\]

- `legacy_len`：SIGNAL 的 LENGTH 字段，PSDU 的字节数。
- `+3`：PSDU 之外的**开销字节**近似。一个 802.11 DATA 域除了 PSDU 本身，还包含 SERVICE 字段（16 位 = 2 字节）和 6 个尾比特（≈ 1 字节），合计约 3 字节。这里的 `+3` 是把这些开销按字节取整后的近似值。
- `×8`：字节换算成比特。
- `×2`：1/2 码率下，每个数据比特展开成 2 个编码比特。

**与 SIGNAL 的对照**：SIGNAL 没有 LENGTH 字段，它是固定的 24 数据位、1/2 码率 → \(24 \times 2 = 48\) 编码比特，所以 `S_DECODE_SIGNAL` 阶段直接写死 `num_bits_to_decode <= 48` [verilog/dot11.v:519](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L519)。这与 DATA 的 `(len+3)×16` 是同一种「数据位 ×2」的规律，只是 SIGNAL 的「数据位」是常数 24。

#### 4.4.3 源码精读：DATA 分支的长度计算

DATA 分支见 [verilog/dot11.v:586-597](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L586-L597)：

```verilog
end else begin
    pkt_rate <= {1'b0, 3'b0, legacy_rate};
    num_bits_to_decode <= (legacy_len+3)<<4;   // ← 长度公式
    do_descramble <= 1;                         // DATA 需要解扰
    ofdm_reset <= 1;
    byte_count <= 0;
    pkt_len <= legacy_len;                       // DATA 解码的「硬停止」靠它
    pkt_begin <= 1;
    pkt_ht <= 0;
    state <= S_DECODE_DATA;
end
```

关键一行：`num_bits_to_decode <= (legacy_len+3)<<4` [verilog/dot11.v:588](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L588)。`<<4` 就是 ×16。

**重要细节——这个量在 DATA 路径里其实不是「硬停止」条件**。看 `ofdm_decoder.v` 里 `num_bits_to_decode` 的唯一用处 [verilog/ofdm_decoder.v:139](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v#L139)：

```verilog
// wait for finishing deinterleaving current symbol
// only do flush for non-DATA bits, such as SIG and HT-SIG, which are not scrambled
if (~do_descramble && deinter_out_count >= num_bits_to_decode) begin
    flush <= 1;
end
```

这段代码的注释说得很清楚：`flush`（用确信的 0 把 Viterbi 回溯延迟里的判决顶出来，见 u3-l5）**只对非 DATA 比特（SIGNAL / HT-SIG）做**，条件里有 `~do_descramble`。而 DATA 分支 `do_descramble=1`，所以这个 flush 永远不会因 DATA 触发。

那么 DATA 到底靠什么停止？靠 `dot11.v` 里的 `byte_count >= pkt_len`，见 [verilog/dot11.v:797](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L797)：

```verilog
if (byte_count >= pkt_len) begin   // pkt_len = legacy_len
    fcs_out_strobe <= 1;
    ...
    state <= S_DECODE_DONE;
end
```

也就是说：DATA 解码的实际停止依据是「已经解出了 `legacy_len` 个字节」（`pkt_len = legacy_len`），而不是 `num_bits_to_decode`。对 DATA 而言，`(legacy_len+3)<<4` 是一个**名义预算值**，表达了「这个包大致要消化多少 1/2 节奏的编码比特」，但当前实现的硬停止交给字节计数。对 SIGNAL（`do_descramble=0`）而言，`num_bits_to_decode=48` 才真正驱动 flush。

> 一句话区分：SIGNAL 用 `num_bits_to_decode` 触发 flush；DATA 用 `byte_count >= pkt_len` 触发结束。两者共用 `num_bits_to_decode` 这个端口，但消费方式不同。

#### 4.4.4 代码实践：验证长度与字节数

**实践目标**：从仿真输出确认「实际解出的字节数 = legacy_len」，并理解 `(legacy_len+3)*16` 的量级。

**操作步骤**：

1. 跑 `make compile && make simulate`（默认 24 Mbps 样本）。
2. 打开 `sim_out/signal_out.txt`，读出 `legacy_len`（第 3 个字段，12 位二进制），换算成十进制 \(L\)。
3. 计算 \((L+3) \times 16\)，记为预算比特数。
4. 统计 `sim_out/byte_out.txt` 的行数（每行一个解码字节）。注意它由 [verilog/dot11_tb.v:225-228](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L225-L228) 在 `S_DECODE_DATA` 且 `byte_out_strobe` 时写入。

**需要观察的现象**：

- `byte_out.txt` 的行数应等于 `legacy_len`（因为 `pkt_len = legacy_len`，`byte_count` 达到即停）。
- \((L+3) \times 16\) 与「实际解出的字节数 ×8」相比，大约是 2 倍多一点（多出来的部分对应 `+3` 的 SERVICE/尾比特开销和 ×2 的 1/2 码率展开）。

**预期结果**：

- `byte_out.txt` 行数 == `legacy_len`（可直接核对，结论确定）。
- \((L+3) \times 16\) 与「该速率下实际编码比特总数 \(N_\text{SYM} \times N_\text{DBPS}\)」**不一定严格相等**——因为实际编码比特数依赖速率（24 Mbps 时 \(N_\text{DBPS}=192\)），而公式是速率无关的名义值，且包含取整近似。所以「\((legacy\_len+3)\times16\) 是否精确等于实际编码比特」属于**待本地验证**：你可以把 \((L+3)\times16\) 与从波形数到的 `deinterleave_out_strobe` 脉冲数 ×2 做对比，观察两者的差距。

**说明**：这条实践的「确定可验证」部分是 `byte_out.txt 行数 == legacy_len`；「(legacy_len+3)*16 的精确性」部分需要你本地数 deinterleave 输出脉冲才能下结论，不要假设它一定相等。

#### 4.4.5 小练习与答案

**练习 1**：对 SIGNAL，`num_bits_to_decode` 为什么是 48 而不是 24？

**答案**：SIGNAL 是 24 个**数据**比特，经 1/2 卷积编码后变成 48 个**编码**比特。`num_bits_to_decode` 计的是解交织输出端的编码比特（1/2 节奏），所以是 48。

**练习 2**：既然 DATA 的停止靠 `byte_count >= pkt_len`，那 `num_bits_to_decode <= (legacy_len+3)<<4` 对 DATA 是不是「没用」？

**答案**：在**当前实现**里，DATA 路径确实不靠 `num_bits_to_decode` 停止（flush 条件含 `~do_descramble`，DATA 不触发）。它更像一个名义预算值/留给一致性表达的字段。但它对 SIGNAL/HT-SIG（`do_descramble=0`）是真正驱动 flush 的关键。所以不能说「完全没用」，只是对 DATA 的角色不同。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「SIGNAL 全流程人工解码」：

1. **跑仿真**：在 `verilog/` 下 `make compile && make simulate`，默认样本是 24 Mbps 的 802.11a 包。
2. **读字段**：打开 `sim_out/signal_out.txt`，按 `RATE(4) RSVD(1) LEN(12) PARITY(1) TAIL(6)` 的格式拆出 5 个字段。
3. **判速率**：用 4.1.2 的表把 RATE 翻译成 Mbps（默认样本应为 `1001` → 24 Mbps）。
4. **手算校验**：
   - 数前 17 位（RATE+RSVD+LEN）中 1 的个数 \(c\)，推出应有 PARITY，与文件里的 PARITY 比对。
   - 确认 bits 0–17 的 1 总数为偶（等价 `legacy_sig_parity_ok=1`）。
   - 确认 RSVD=0、TAIL=`000000`。
5. **算长度**：把 LEN 换算成十进制 \(L\)，计算 \((L+3)\times16\)，并数 `sim_out/byte_out.txt` 的行数验证它等于 \(L\)。
6. **画时序**：在 `dot11.vcd` 里定位 `state` 从 `S_DECODE_SIGNAL`(3) → `S_CHECK_SIGNAL`(4) → `S_DECODE_DATA`(11) 的三次跳变，标注每次跳变的触发条件（`byte_count==3`、三项校验通过、`legacy_rate != 1011`）。

完成后，你应该能用一张图说清楚：「3 个字节 → signal_bits → 位域切片 → 三项校验 → 长度计算 → 进入 DATA」这条控制平面链路。

## 6. 本讲小结

- SIGNAL 是 LTS 之后的第一个 OFDM 符号，24 比特分 5 段：RATE(4)/RSVD(1)/LENGTH(12)/PARITY(1)/TAIL(6)。
- `signal_bits` 是 24 位移位寄存器，靠 `signal_bits <= {byte_out, signal_bits[23:8]}` 把 3 个字节拼起来，**先到的字节落低位**，对应 802.11「bit 0 先发」。
- 5 个字段就是 `signal_bits` 的位切片 `assign`；偶校验用一行 `~^signal_bits[17:0]` 实现——约简异或为 1 表示 1 的个数为奇，取反后为 1 表示偶 = 通过。
- `S_CHECK_SIGNAL` 按顺序查 parity / rsvd / tail 三项，失败分别给 `E_PARITY_FAIL` / `E_WRONG_RSVD` / `E_WRONG_TAIL` 并跳到 `S_SIGNAL_ERROR` 等下一个包。
- `num_bits_to_decode = (legacy_len+3)<<4`：单位是「1/2 节奏的编码比特」，`+3` 近似 SERVICE+尾开销，`×16` = `×8×2`。它驱动 SIGNAL 的 flush；DATA 的硬停止实际靠 `byte_count >= pkt_len`（`pkt_len=legacy_len`）。
- 默认仿真下 `signal_out.txt` 一行五字段、`byte_out.txt` 行数应等于 `legacy_len`——这两个是可直接核对的事实。

## 7. 下一步学习建议

- **下一篇 u4-l3** 会进入 HT-SIG：当 `legacy_rate == 4'b1011` 时，状态机转入 `S_DETECT_HT`，用「Q 路 BPSK、|Q|>|I|」识别 HT-SIG，再做 CRC-8 校验。本讲的 SIGNAL 分流（`legacy_rate == 4'b1011` 这条边）正是通往 u4-l3 的入口。
- 想彻底搞清 `num_bits_to_decode` 如何驱动 flush，建议回看 **u3-l5（ofdm_decoder 子流水线与卷积解码）** 里软判决与 flush 的部分。
- 想了解 DATA 解码结束后的整包校验，继续读 **u4-l5（FCS 校验与 CRC32）**，看 `byte_count >= pkt_len` 之后如何用 `crc32` 与 `EXPECTED_FCS` 比对给出 `fcs_ok`。
- 建议动手扩展：在 `dot11_tb.v` 里仿照 [verilog/dot11_tb.v:199-203](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L199-L203) 增加一个 `$fwrite`，把每次进入 `S_SIGNAL_ERROR` 时的 `status_code` 落盘，用来统计仿真中 SIGNAL 校验失败的次数与原因分布。
