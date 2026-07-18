# FCS 校验与 CRC32

## 1. 本讲目标

经过前面几讲，我们已经能把一个 802.11 OFDM 包从射频样本一路还原成字节流（`byte_out`）。但「还原出来」不等于「还原对了」——无线信道里一个比特翻转就可能让整个 MAC 帧作废。本讲解决最后一个闭环问题：**如何确认这一整包字节是真的完好无损？**

读完本讲，你应当能够：

- 说清 CRC-32 的生成多项式、LFSR（线性反馈移位寄存器）原理，以及 OpenOFDM 为什么用「残值（residue）比对」而不是「分别算 FCS 再比」。
- 读懂 `crc32.v` 中那张庞大的 `lfsr_c[31:0]` 异或表是怎么把「逐比特串行 CRC」展开成「逐字节并行 CRC」的。
- 解释 `byte_reversed` 为什么要把每个字节的 8 个比特镜像翻转后再喂给 CRC32。
- 解释 `fcs_reset` 为什么刚好在进入 `S_DECODE_DATA` 的那一拍拉高、`fcs_enable` 为什么必须同时满足「在 DATA 状态」和「byte_out_strobe 有效」。
- 在仿真波形里找到 `fcs_ok` 置 1 的那一刻，确认一个正确包通过了完整性校验。

## 2. 前置知识

### 2.1 为什么需要 FCS

802.11 MAC 帧的尾巴上有 4 个字节的 **FCS（Frame Check Sequence，帧校验序列）**，它是一份「指纹」：发送端把整帧数据算出一个 32 位的校验值附在末尾，接收端用同样算法重算一遍，若结果与附带值吻合，就认为这帧没有比特错误。

FCS 用的是 **CRC-32**，与有线以太网（802.3）完全相同的算法。这一点很关键——它意味着我们可以直接复用以太网世界成熟的 CRC-32 实现。

### 2.2 CRC 与 LFSR 的直觉

CRC（Cyclic Redundancy Check，循环冗余校验）把待校验的比特串看成一个巨大的二进制多项式 \( M(x) \)，再用一个约定的「生成多项式」 \( G(x) \) 去做模 2 除法，余数就是 CRC 值。模 2 除法里，加减法都是异或（XOR），没有进位借位。

硬件实现 CRC 最自然的方式是 **LFSR**：一个移位寄存器，每来一个比特就右移一次，并把某些抽头异或后反馈回去。哪些位参与反馈，由生成多项式决定。这种「来一个比特、走一拍」的就是 **串行 CRC**。

### 2.3 从串行到并行

串行 CRC 一个时钟只能吃一个比特，处理一个 8 位字节要 8 拍，太慢。**并行 CRC** 的思路是：把「连续 8 次串行移位」在数学上合并，直接用组合逻辑从「当前寄存器值 + 当前输入字节」一步算出「8 拍之后的寄存器值」。本讲的 `crc32.v` 就是这么做的——这也是它那张看似吓人的异或表的来历。

> 阅读本讲前，建议先回顾 u4-l1（dot11 顶层状态机，尤其 `S_DECODE_DATA` / `S_DECODE_DONE`）和 u3-l6（字节是如何从比特流拼出来的）。本讲只关心「字节拿到之后怎么校验」，不重复解码流程。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [verilog/crc32.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/crc32.v) | CRC-32 计算核。8 位输入、32 位输出，组合逻辑算下一状态，时序逻辑在 `crc_en` 时更新、在 `rst` 时复位为全 1。来自 OutputLogic.com 的生成器。 |
| [verilog/dot11.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) | 顶层。例化 `crc32`、做 `byte_reversed` 位反转、产生 `fcs_enable`/`fcs_reset`，并在 `S_DECODE_DATA` 末尾做残值比对。 |
| [verilog/common_params.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v) | 定义 `EXPECTED_FCS`（标准残值 `0xc704dd7b`）与错误码 `E_WRONG_FCS`。 |

---

## 4. 核心概念与源码讲解

### 4.1 CRC-32 生成多项式与「残值比对」技巧

#### 4.1.1 概念说明

802.3 / 802.11 的 CRC-32 采用生成多项式：

\[
G(x) = x^{32} + x^{26} + x^{23} + x^{22} + x^{16} + x^{12} + x^{11} + x^{10} + x^{8} + x^{7} + x^{5} + x^{4} + x^{2} + x + 1
\]

写成十六进制常数就是 `0x04C11DB7`（最高位的 \(x^{32}\) 隐含）。这就是本讲代码注释里那一长串 `1+x^1+x^2+x^4+x^5+x^7+x^8+x^10+x^11+x^12+x^16+x^22+x^23+x^26+x^32`，两者完全一致。

完整的 802.3 FCS 过程有三步约定：

1. **初值全 1**：寄存器初始化为 `0xFFFFFFFF`，而不是 0。
2. **逐字节、低位先入**：每个字节的最低位（LSB）最先进入 CRC 运算。
3. **结尾取反**：算完后把寄存器按位取反，得到正式的 FCS，再附到帧尾发送。

如果老老实实照做，接收端需要：先对「数据」算一遍 CRC（得到期望 FCS），再与帧尾那 4 个字节单独比对。这需要额外的「拆出 FCS 字段」逻辑。

OpenOFDM 用了一个更聪明的等价做法——**残值（residue）比对**：

> 把「数据 + 末尾 4 字节 FCS」**全部**喂进同一个 CRC-32（初值仍全 1），**不做结尾取反**。当且仅当帧正确时，寄存器最终会停在一个固定常数 `0xC704DD7B` 上。

这个常数叫做 CRC-32 的「magic residue」，是 802.3 标准下「数据+FCS」经过上述运算后必然出现的余数。于是接收端不必知道 FCS 到底放在哪 4 个字节、也不必拆字段，只要看寄存器是否等于 `EXPECTED_FCS` 即可。这就是 `common_params.v` 里那个常量的来历。

#### 4.1.2 核心流程

```
发送端（规范定义）：            接收端（OpenOFDM 实现）：
  reg ← 0xFFFFFFFF                reg ← 0xFFFFFFFF   （复位）
  对每个数据字节更新 reg           对每个字节（含末4字节FCS）更新 reg
  FCS ← ~reg                      跳过结尾取反
  发送 数据 + FCS(4字节)          比较 reg == 0xC704DD7B ?
                                       是 → fcs_ok=1（整包正确）
                                       否 → fcs_ok=0（有比特错）
```

为什么能这样？因为「发送端附上的 FCS」恰好是把「正确数据的 CRC」取反后的值；当接收端把数据与这份 FCS 一起再算一遍（仍初值全 1、不取反）时，数学上残留的就是那个固定魔数。任何 1 比特错误都会让残值偏离 `0xC704DD7B`。

#### 4.1.3 源码精读

`EXPECTED_FCS` 与错误码定义在 [verilog/common_params.v:67-71](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L67-L71)，这一段先定义 FCS 错误码，再给出标准残值：

```verilog
// fcs error
localparam E_WRONG_FCS =            1;


localparam EXPECTED_FCS = 32'hc704dd7b;
```

- `E_WRONG_FCS=1`：FCS 比对失败时写入 `status_code`（注意它与 `E_OK=0` 对应，状态码的具体含义需结合当前 `state` 一起读，这是 u4-l1 已建立的约定）。
- `EXPECTED_FCS = 32'hc704dd7b`：正是 802.3 CRC-32 的 magic residue。

生成多项式则在 [verilog/crc32.v:12](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/crc32.v#L12) 的注释里直接写出，作为整张异或表的「规格说明」。

#### 4.1.4 代码实践

**实践目标**：确认 `0xC704DD7B` 这个残值确实是 IEEE CRC-32 的 magic residue，建立「数据+FCS 一起算会得到固定常数」的直觉。

**操作步骤**：

1. 打开任意能跑 Python 的环境（这是离线数学验证，不影响 Verilog 仿真）。
2. 写一段示例代码（**示例代码，非项目原有文件**）：

   ```python
   # 示例代码：演示 CRC-32 残值
   import struct
   data = b"hello-openofdm"               # 任意「数据」
   fcs = (~__import__("binascii").crc32(data)) & 0xffffffff  # 模拟发送端取反
   pkt = data + struct.pack("<I", fcs)    # 数据 + 4 字节 FCS（小端，低位先发）
   reg = __import__("binascii").crc32(pkt) & 0xffffffff     # 数据+FCS 一起算
   print("reg        = %08x" % reg)
   print("EXPECTED   = %08x" % 0xc704dd7b)
   ```

3. 这里复用了标准库 `binascii.crc32`（它内部已做「初值全 1 + 结尾取反」），所以手动对数据那次取反、对整包那次不取反，正好复刻 OpenOFDM 的接收侧约定。

**需要观察的现象 / 预期结果**：`reg` 应严格等于 `c704dd7b`。改动 `data` 内容，残值不变；只要把 `pkt` 里任意一个比特翻转，残值就会变成别的值。

> 若本地没有 Python，可标注「待本地验证」并用在线 CRC-32 工具手工核对：对同一份「数据+FCS」连续计算，结果恒为 `0xC704DD7B`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 CRC 寄存器初值要取全 1（`0xFFFFFFFF`）而不是全 0？

> **答**：若初值为 0，则数据开头任意长的一串前导 0 都不会改变寄存器（0 与 0 异或仍为 0），于是「帧前面多几个 0」和「少几个 0」会得到相同的 CRC，无法区分。初值取全 1 让前导 0 也立刻「污染」寄存器，消除这种歧义；这也是 802.3 标准的硬性约定。

**练习 2**：OpenOFDM 没有在 `crc32.v` 里做「结尾取反」，却仍然能得到正确的校验结果，为什么？

> **答**：因为它走的是残值路线——把发送端附在末尾的 FCS（其本质是「正确数据 CRC 的取反」）和数据一起再算一遍 CRC。在这种「不取反」的约定下，正确帧必然残留固定魔数 `0xC704DD7B`。它不需要、也不能再做结尾取反，否则就破坏了这个残值性质。

---

### 4.2 crc32 模块：并行 CRC 核

#### 4.2.1 概念说明

`crc32.v` 是一个「逐字节」的 CRC-32 计算核：每个时钟吃一个字节（`data_in[7:0]`），在 `crc_en` 有效时更新 32 位寄存器（`crc_out[31:0]`），在 `rst` 有效时复位。它把原本需要 8 拍的串行移位「折叠」成 1 拍的组合运算，所以能在字节吞吐率为 1 byte/cycle 的流水线里实时跟算。

这个文件并非作者手写算法，而是由 OutputLogic.com 的 CRC 生成器按指定多项式自动产出的（见文件头版权注释），属于业界常用的并行 CRC 模板。

#### 4.2.2 核心流程

```
组合逻辑块（always @(*)）：
  根据 当前寄存器 lfsr_q[31:0] 和 输入 data_in[7:0]
  一步算出 「连续 8 次串行移位后的下一状态」 lfsr_c[31:0]

时序逻辑块（always @(posedge clk, posedge rst)）：
  if (rst)      lfsr_q ← 0xFFFFFFFF      // 初值全 1
  else if (crc_en) lfsr_q ← lfsr_c       // 来一个字节就更新
  else          lfsr_q ← lfsr_q           // 空拍保持

输出：
  crc_out = lfsr_q                        // 直接把寄存器引出
```

注意三个要点：**复位是异步的**（`posedge clk, posedge rst`，复位立即生效不等时钟边沿）、**没有结尾取反**、**没有输出寄存器打拍**（`assign crc_out = lfsr_q` 直通）。

#### 4.2.3 源码精读

模块端口见 [verilog/crc32.v:14-19](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/crc32.v#L14-L19)：`data_in` 为输入字节，`crc_en` 为更新使能，`crc_out` 为当前 32 位寄存器值，`rst`/`clk` 为异步复位与时钟。

核心是 [verilog/crc32.v:25-59](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/crc32.v#L25-L59) 的组合逻辑块。这里只摘两行体会一下风格：

```verilog
always @(*) begin
    lfsr_c[0] = lfsr_q[24] ^ lfsr_q[30] ^ data_in[0] ^ data_in[6];
    lfsc_c[1] = lfsr_q[24] ^ lfsr_q[25] ^ lfsr_q[30] ^ lfsr_q[31]
              ^ data_in[0] ^ data_in[1] ^ data_in[6] ^ data_in[7];
    ...
```

每一行 `lfsr_c[k]` 就是「8 拍之后第 k 位应该是什么」的异或表达式，等号右边只出现 `lfsr_q`（当前状态）和 `data_in`（当前字节）——这正是「串行 8 拍折叠成 1 拍」的结果。32 行恰好覆盖 32 个寄存器位，整张表共同构成一次完整的字节级 CRC 推进。

时序与复位在 [verilog/crc32.v:61-68](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/crc32.v#L61-L68)：

```verilog
always @(posedge clk, posedge rst) begin
    if(rst) begin
        lfsr_q <= {32{1'b1}};            // 复位 → 0xFFFFFFFF
    end
    else begin
        lfsr_q <= crc_en ? lfsr_c : lfsr_q;   // 仅 crc_en 时更新
    end
end
```

- `{32{1'b1}}` 即 `0xFFFFFFFF`，符合 802.3 初值约定。
- `crc_en ? lfsr_c : lfsr_q`：使能时吃进新字节、否则冻结。这一点对本讲很要紧——它让上层能用「只在 `byte_out_strobe` 那一拍给 `crc_en`」的方式精准控制「哪些字节算、哪些字节不算」。

输出在 [verilog/crc32.v:23](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/crc32.v#L23)：`assign crc_out = lfsr_q;`，把寄存器直通为模块输出，没有额外打拍，时序上「所见即当前残值」。

#### 4.2.4 代码实践

**实践目标**：体会「并行 CRC = 串行 CRC 的 8 步折叠」，建立对那张异或表的信任。

**操作步骤**：

1. 打开 [verilog/crc32.v:26-27](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/crc32.v#L26-L27)。
2. 在草稿纸上画一个最简化的「1 位串行 CRC」示意：一个 1 位移位寄存器 `q`，输入 `d`，反馈 `q_next = q ^ d`。走 2 拍后 `q` 等于什么？答案是 `q0 ^ d0 ^ d1`。
3. 对照 `lfsr_c[0]` 那一行：它把 `lfsr_q[24] ^ lfsr_q[30]`（当前状态的某些位）与 `data_in[0] ^ data_in[6]`（输入字节的某些位）异或到一起——本质和你草稿纸上那行 `q0 ^ d0 ^ d1` 完全同类，只是多项式更长、折叠了 8 拍。

**需要观察的现象 / 预期结果**：你能向自己解释清楚「为什么 `lfsr_c[k]` 的等号右边只可能是 `lfsr_q` 与 `data_in` 的异或组合」——因为模 2 运算下，多拍线性反馈展开后只剩异或，不会有进位/乘法。

**预期结果**：无需运行仿真即可接受这张表的正确性（它是生成器按多项式机械展开的）；真要逐行验证，需对照标准 LFSR 反馈抽头一位位推导，工作量很大，工程上通常直接信任生成器，再用「整包残值是否等于 `0xC704DD7B`」做端到端验证（见 4.4 的实践）。

#### 4.2.5 小练习与答案

**练习 1**：`crc32.v` 的复位写成 `always @(posedge clk, posedge rst)`，这是同步复位还是异步复位？为什么这里要这样选？

> **答**：异步复位（敏感列表里同时有 `posedge rst`，复位一来立即生效，不等时钟边沿）。异步复位的好处是：上层（`dot11.v`）一旦决定「现在要重新开始算一个新包」（`fcs_reset` 拉高），CRC 寄存器立刻清回 `0xFFFFFFFF`，不必等到下一个时钟沿，时序控制更简单、更不易踩到「这一拍到底算没算上一个字节」的歧义。

**练习 2**：如果把 `crc_en` 恒接 1（每个时钟都更新），会发生什么？

> **答**：寄存器会在没有真实字节的空拍里也被推进，相当于反复「吃进当时的 `data_in`」。由于 `byte_out` 只在 `byte_out_strobe` 那一拍才是有效字节、其余拍是过期值，恒使能会把垃圾字节算进 CRC，最终残值必然错误。这就是上层必须用 `fcs_enable = (state==S_DECODE_DATA) && byte_out_strobe` 精准 gating 的原因。

---

### 4.3 位反转 byte_reversed 与使能时序

#### 4.3.1 概念说明

把字节送进 CRC 核之前，`dot11.v` 做了一件看似多余的事：**把每个字节的 8 个比特镜像翻转**（bit 0 ↔ bit 7、bit 1 ↔ bit 6……）。这个 `byte_reversed` 不是装饰，而是为了弥合两种「比特序」约定的差异。

回顾 u3-l6：`bits_to_bytes` 把最先到达的比特放进字节的最低位（LSB）。而 802.3 CRC-32 规定「每个字节的最低位先进入运算」，但 `crc32.v` 这个并行核在内部把 `data_in[i]` 映射到特定的串行位置（其生成时的输入比特序约定与 MAC 字节的自然排列相反）。两者直接对接时比特顺序对不上，算出来的残值就不会是 `0xC704DD7B`。`byte_reversed` 的作用就是事先把字节翻一下，让「喂进 CRC 核的有效比特顺序」恰好匹配核所期待的顺序，从而拿到正确的标准残值。

此外，整条 FCS 链路还靠两根控制线精准节拍：

- `fcs_reset`：每个新包开始时把 CRC 寄存器打回 `0xFFFFFFFF`。
- `fcs_enable`：只有「正在解码 DATA」且「这一拍真的吐出了一个字节」时，才允许 CRC 更新。

#### 4.3.2 核心流程

```
                         ┌──────────────────────────┐
byte_out ──►【位反转】──► byte_reversed ──► data_in │
                         │                          │
state==S_DECODE_DATA ─┐  │                          │
byte_out_strobe ──────┼─►【与门】────► fcs_enable ─►│ crc_en
                      │  │                          │
state_changed ─┐      │  │                          │
state==S_DECODE_DATA ──┴►【与门】────► fcs_reset ──►│ rst
                      │  │                          │
                      └──┴──────────────────────────┘
                                      │
                                   pkt_fcs ──►（下一步比对 EXPECTED_FCS）
```

#### 4.3.3 源码精读

控制线与中间信号声明在 [verilog/dot11.v:239-242](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L239-L242)：

```verilog
wire fcs_enable = state == S_DECODE_DATA && byte_out_strobe;
wire fcs_reset = state_changed && state == S_DECODE_DATA;
wire [7:0] byte_reversed;
wire [31:0] pkt_fcs;
```

- `fcs_enable`：两个条件相与。`state==S_DECODE_DATA` 把 CRC 活动严格限制在 DATA 阶段（SIGNAL、HT-SIG 等阶段各有各的校验机制，不走这个 `crc32`）；`byte_out_strobe` 进一步保证只在「真字节到达」的那一拍推进，空拍不更新。
- `fcs_reset`：`state_changed`（定义于 [verilog/dot11.v:195](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L195) 的 `state != old_state`）与「当前已进入 `S_DECODE_DATA`」相与——也就是「刚刚转入 `S_DECODE_DATA` 的那一拍」。这一拍拉高，把 CRC 打回全 1；下一拍 `state_changed` 自然消失，`fcs_reset` 回零，CRC 开始正常吃字节。

位反转在 [verilog/dot11.v:244-251](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L244-L251)，逐位把 `byte_out` 镜像到 `byte_reversed`：

```verilog
assign byte_reversed[0] = byte_out[7];
assign byte_reversed[1] = byte_out[6];
...
assign byte_reversed[7] = byte_out[0];
```

最后在 [verilog/dot11.v:394-400](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L394-L400) 例化 `crc32`，把上面三路信号接好：

```verilog
crc32 fcs_inst (
    .clk(clock),
    .crc_en(enable & fcs_enable),
    .rst(reset | fcs_reset),
    .data_in(byte_reversed),
    .crc_out(pkt_fcs)
);
```

注意三个细节：

- `.rst(reset | fcs_reset)`：全局 `reset` 与「每包起始」的 `fcs_reset` 取或——既服从系统复位，又能在每个新包开头独立重置。
- `.crc_en(enable & fcs_enable)`：再叠加一层全局 `enable`（u1-l4 讲过的模块使能），保证模块被冻结时 CRC 完全不动。
- `.data_in(byte_reversed)`：喂进去的是翻转后的字节；`pkt_fcs` 即当前残值，供下一步比对。

#### 4.3.4 代码实践

**实践目标**：亲手验证「位反转是必需的」，并解释两条使能线的时机。

**操作步骤**：

1. 在 [verilog/dot11.v:244-251](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L244-L251) 处追踪 `byte_out` 到 `byte_reversed` 的逐位映射，确认它是纯粹的镜像翻转、不改变比特内容只改变顺序。
2. 在 [verilog/dot11.v:394-400](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L394-L400) 处确认 `byte_reversed` 接到 `data_in`、`fcs_enable` 接到 `crc_en`、`reset | fcs_reset` 接到 `rst`。
3. **思考实验（不改动源码也能推理）**：若把 `data_in` 直接接 `byte_out`（跳过反转），由于 `bits_to_bytes` 产出字节的比特序与 `crc32` 核期待的输入序不一致，第一个字节起残值就会偏离标准轨迹，最终 `pkt_fcs` 几乎不可能等于 `0xC704DD7B`，于是 `fcs_ok=0`。若你想实测：可在自己的一份 `dot11.v` 副本里临时把 `byte_reversed` 改成 `byte_out` 重新仿真（注意：这是你本地副本的探索性修改，不要提交到仓库）。
4. 解释时机：
   - **为何 `fcs_reset` 在进入 `S_DECODE_DATA` 时拉高？** 因为 `crc32` 全局只例化一次、跨包复用，每个新包都必须把寄存器重置回 `0xFFFFFFFF`，否则上一包的残值会污染本包。`state_changed && state==S_DECODE_DATA` 精确锁定「转入 DATA 的那一拍」做一次性复位，之后自动撤除。
   - **为何 `fcs_enable` 仅在 `byte_out_strobe` 有效？** 因为 `byte_out` 是脉冲式握手（u1-l4），只有 `byte_out_strobe=1` 那一拍的 `byte_out` 才是有效字节，其余拍是过期数据；只在 strobe 拍推进 CRC，才能保证「算进 CRC 的恰好是这一包的真实字节序列」。

**需要观察的现象 / 预期结果**：

- 正常（带反转）情况下，正确包的 `pkt_fcs` 在包尾应等于 `0xC704DD7B`。
- 若做思考实验里那个「去掉反转」的副本仿真，`pkt_fcs` 会偏离 `0xC704DD7B`，`fcs_ok` 保持 0。

> 若本地不便改副本重仿，可标注「待本地验证」，但「位反转是必需的」这一结论可由 4.1 的残值约定严格推出。

#### 4.3.5 小练习与答案

**练习 1**：`fcs_reset` 只在转入 `S_DECODE_DATA` 的那一拍为高，下一拍自动变低，这是怎么实现的？

> **答**：靠 `state_changed = (state != old_state)`。每个时钟 `old_state <= state`（[dot11.v:461](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L461)），于是 `state_changed` 仅在「状态刚刚跳转的那一拍」为 1，次拍 `old_state` 追上 `state` 后即归零。`fcs_reset = state_changed && state==S_DECODE_DATA` 因此自然形成一个「单拍复位脉冲」，无需手动清零。

**练习 2**：`fcs_enable` 里为什么不只写 `byte_out_strobe`，还要再与上 `state == S_DECODE_DATA`？

> **答**：`byte_out_strobe` 在解码 SIGNAL、HT-SIG 时也会脉冲（那时也在产出字节）。但那些阶段的字节不该进入帧 FCS 计算——SIGNAL 走的是 parity 校验、HT-SIG 走的是单独的 `ht_sig_crc`。再加 `state==S_DECODE_DATA` 这一闸门，就保证只有真正的 MAC 数据（MPDU，含末尾 FCS）才喂进 `crc32`。

---

### 4.4 S_DECODE_DATA 的 FCS 比对流程

#### 4.4.1 概念说明

字节一边吐出、一边喂进 CRC，那么「什么时候停下来做比对」？答案是「数够了一包的字节数」。`S_DECODE_DATA` 用 `byte_count` 计数已收字节，当它达到 `pkt_len`（SIGNAL/HT-SIG 里解析出的 PSDU 长度，**已包含末尾 4 字节 FCS**）时，认为整包（含 FCS）都已喂完，此时 CRC 寄存器里正是最终残值，立即与 `EXPECTED_FCS` 比对，给出 `fcs_ok`，并发出一次性的 `fcs_out_strobe` 脉冲通知上游「这一包校验完了」。

#### 4.4.2 核心流程

```
进入 S_DECODE_DATA：byte_count=0，pkt_len 已设好（=legacy_len 或 ht_len）

每个时钟：
  if (byte_out_strobe) byte_count++              // 来一个字节计一个
  （同一拍，byte_reversed 经 fcs_enable 喂进 crc32）

  if (byte_count >= pkt_len):                    // 整包字节（含FCS）已喂完
      fcs_out_strobe ← 1
      if (pkt_fcs == EXPECTED_FCS):
          fcs_ok ← 1;  status_code ← E_OK
      else:
          fcs_ok ← 0;  status_code ← E_WRONG_FCS
      state ← S_DECODE_DONE

S_DECODE_DONE：
  fcs_out_strobe ← 0                             // 撤销脉冲
  state ← S_WAIT_POWER_TRIGGER                   // 回去等下一个包
```

#### 4.4.3 源码精读

`S_DECODE_DATA` 的主体在 [verilog/dot11.v:777-808](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L777-L808)。字节计数与比对分两段，注意第二段并**没有**包在 `if (byte_out_strobe)` 里：

```verilog
S_DECODE_DATA: begin
    pkt_begin <= 0;
    legacy_sig_stb <= 0;

    if (ofdm_reset) ofdm_reset <= 0;

    ofdm_in_stb <= eq_out_stb_delayed;          // 继续喂数据给解码子流水线
    ofdm_in_i  <= eq_out_i_delayed;
    ofdm_in_q  <= eq_out_q_delayed;

    if (byte_out_strobe) begin                  // 段①：字节计数
        byte_count <= byte_count + 1;
    end

    if (byte_count >= pkt_len) begin            // 段②：FCS 比对（与 strobe 无关）
        fcs_out_strobe <= 1;
        if (pkt_fcs == EXPECTED_FCS) begin
            fcs_ok <= 1;
            status_code <= E_OK;
        end else begin
            fcs_ok <= 0;
            status_code <= E_WRONG_FCS;
        end
        state <= S_DECODE_DONE;
    end
end
```

两个段落的耦合关系值得细看：

- 段①里的 `byte_count` 用非阻塞赋值（`<=`），其新值要等下一拍才生效。
- 段②的判定 `byte_count >= pkt_len` 读的是**本拍开始时**的旧值。因此当「第 `pkt_len` 个字节的 strobe」把 `byte_count` 更新到 `pkt_len` 之后，是在**次拍**（此时 CRC 寄存器 `pkt_fcs` 也已含入这最后一个字节）触发段②，时机刚好——既不漏算最后一个字节，也不多等。

进入 `S_DECODE_DONE`（[verilog/dot11.v:810-821](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L810-L821)）后，唯一动作是撤销 `fcs_out_strobe`（它是个单拍脉冲）并回到 `S_WAIT_POWER_TRIGGER`：

```verilog
S_DECODE_DONE: begin
    fcs_out_strobe <= 0;
    state <= S_WAIT_POWER_TRIGGER;
end
```

复位初值在 [verilog/dot11.v:458-459](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L458-L459)，保证上电时 `fcs_out_strobe`/`fcs_ok` 为 0：

```verilog
fcs_out_strobe <= 0;
fcs_ok <= 0;
```

#### 4.4.4 代码实践

**实践目标**：在仿真里亲眼确认 `fcs_ok` 在一个正确包上被置 1。

**操作步骤**：

1. 按 u1-l2 的方法编译并仿真默认样本（`dot11a_24mbps_qos_data_...txt`）：

   ```bash
   cd verilog
   make compile
   make simulate
   ```

2. 仿真会生成 `dot11.vcd`。测试台 `dot11_tb.v` **当前并未把 `fcs_ok` / `fcs_out_strobe` 接到顶层、也未落盘**（可在 [verilog/dot11_tb.v:233-281](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L233-L281) 的例化处确认这两个端口未连线）。但好在那里有 `$dumpvars`（[verilog/dot11_tb.v:91-92](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L91-L92)），会把 DUT 内部**所有**信号（含 `fcs_ok`、`pkt_fcs`、`state`）都写进 VCD。

3. 用 gtkwave 打开波形：

   ```bash
   gtkwave dot11.vcd
   ```

4. 在信号树里找到 `dot11_tb.dot11_inst` 实例，加入 `state`、`byte_count`、`pkt_len`、`pkt_fcs`、`fcs_ok`、`fcs_out_strobe`、`byte_out_strobe`。

5. 把时间光标移到 `state` 跳进 `S_DECODE_DONE`（=14）的那一拍。

**需要观察的现象 / 预期结果**：

- 在 `state` 从 `S_DECODE_DATA`（=11）跳到 `S_DECODE_DONE`（=14）的边界上，`fcs_out_strobe` 出现一个单拍脉冲。
- 同一时刻 `fcs_ok` 被置为 `1`、`status_code` 为 `E_OK`（=0）。
- 此刻 `pkt_fcs`（即 `crc32` 寄存器 `lfsr_q`）的值正好是 `0xC704DD7B`，与 `EXPECTED_FCS` 相等。
- 作为旁证：默认样本是「正确包」，`sim_out/byte_out.txt` 的全部字节应能被 `scripts/test.py` 的 BYTE 比对通过（打印 `BYTE works!`），间接印证 `fcs_ok` 应为 1。

> 若你手上没有 gtkave 图形环境，可标注「待本地验证」。退化方案：运行 `python scripts/test.py <样本>` 看到 `BYTE works!`，即可认为整包字节（含末尾 FCS）正确，逻辑上 `fcs_ok` 必为 1。

#### 4.4.5 小练习与答案

**练习 1**：段②「`byte_count >= pkt_len`」为什么没写在 `if (byte_out_strobe)` 里面？写进去会怎样？

> **答**：因为要在「最后一个字节的 strobe 把 `byte_count` 更新到 `pkt_len` 之后」的次拍触发比对，而那一拍不一定还有 `byte_out_strobe`（字节是稀疏脉冲）。若把它塞进 `if (byte_out_strobe)`，就得等到下一个字节 strobe 才比对，但那时已经多算了一个字节、且 DATA 段可能已无更多字节，比对永远不发生或时机错误。把它放在外面、让它每拍都判一次，就能在 `byte_count` 刚到 `pkt_len` 的次拍立即触发。

**练习 2**：`pkt_len` 里到底含不含那 4 个 FCS 字节？这对残值比对为什么至关重要？

> **答**：含。`pkt_len = legacy_len`（或 `ht_len`），即 SIGNAL/HT-SIG 里给出的 PSDU 长度，按 802.11 规范已包含末尾 4 字节 FCS。残值比对的前提是「把数据 + FCS 全部喂进 CRC」，所以必须数到 `pkt_len`（含 FCS）才停。若 `pkt_len` 不含 FCS，就会在 FCS 字节喂完之前提前比对，残值自然不是 `0xC704DD7B`。

---

## 5. 综合实践

**任务**：把本讲四个最小模块串成一条完整的「FCS 校验链路」走查，并设计一个「故意制造 1 比特错误」的对照实验，验证残值机制的有效性。

**要求**：

1. **正向走查（纸面 + 波形）**：从 `byte_out_strobe` 出发，画出数据经过 `byte_reversed`（[dot11.v:244-251](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L244-L251)）→ `crc32` 的 `data_in`（[dot11.v:394-400](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L394-L400)）→ `pkt_fcs` → 与 `EXPECTED_FCS` 比对（[dot11.v:797-807](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L797-L807)）的完整路径，并在图上标注 `fcs_enable`、`fcs_reset` 的产生位置。

2. **对照实验（建议本地副本，勿提交）**：
   - 在一份 `dot11.v` 的本地探索副本里，找到 `S_DECODE_DATA` 中 `byte_out` 送往 FCS 的通路，在 `byte_reversed` 之后插入「把最低位强制异或 1」的一行（即把每个字节的 bit0 翻转），等效于「每个字节都错了 1 个比特」。
   - 重新 `make compile && make simulate`，用 gtkwave 观察 `pkt_fcs`。
   - **预期**：`pkt_fcs` 不再等于 `0xC704DD7B`，`fcs_ok=0`、`status_code=E_WRONG_FCS`（=1），状态机仍正常进入 `S_DECODE_DONE` 但报告校验失败。
   - 实验完毕务必丢弃该副本，**不要改动仓库源码**。

3. **写一份简短结论**：用 100 字以内说明「为什么残值比对能同时检测出『数据错』和『FCS 字节本身错』」——因为无论错误落在数据区还是 FCS 区，都会让最终的 CRC 余数偏离固定魔数。

> 这个综合实践把「读源码、读波形、做受控扰动、回归验证」四件事都串了起来，是理解硬件校验链路的典型套路。

## 6. 本讲小结

- OpenOFDM 用 **802.3 CRC-32**（生成多项式 `0x04C11DB7`）做帧校验，但采用 **残值比对** 技巧：把「数据 + 末尾 4 字节 FCS」全部喂进 CRC（初值全 1、不取反），正确包必然残留固定魔数 `0xC704DD7B = EXPECTED_FCS`，省去了「拆出 FCS 字段再单独比」的麻烦。
- `crc32.v` 是 OutputLogic 生成的 **并行 CRC 核**：32 行异或等式把「8 拍串行移位」折叠成「1 拍组合运算」，复位异步载入 `0xFFFFFFFF`，仅在 `crc_en` 时更新。
- `byte_reversed` 把每个字节镜像翻转，弥合 `bits_to_bytes` 的输出比特序与 `crc32` 核输入约定之间的差异，是拿到正确残值的前提。
- `fcs_enable = state==S_DECODE_DATA && byte_out_strobe`：只在 DATA 阶段、且真实字节到达的那一拍推进 CRC；`fcs_reset = state_changed && state==S_DECODE_DATA`：在每个新包转入 DATA 的单拍内把寄存器重置回全 1。
- 比对发生在 `S_DECODE_DATA`：当 `byte_count >= pkt_len`（长度含 FCS）时，比较 `pkt_fcs==EXPECTED_FCS`，置 `fcs_ok` 与 `status_code`，发一次 `fcs_out_strobe` 脉冲，随后 `S_DECODE_DONE` 撤脉冲并回到等待状态。
- 测试台目前未把 `fcs_ok` 接出，但 `$dumpvars` 会把 DUT 内部信号写进 VCD，可用 gtkave 在 `state` 跳入 `S_DECODE_DONE` 处确认 `fcs_ok=1`、`pkt_fcs=0xC704DD7B`。

## 7. 下一步学习建议

- **横向对比另一种校验**：本讲是「整包 CRC-32」，而 HT-SIG 字段用的是 8 位 CRC（见 u4-l3 的 `ht_sig_crc.v`）、legacy SIGNAL 用的是 1 位偶校验（见 u4-l2 的 `legacy_sig_parity_ok`）。建议对比这三种校验机制的实现复杂度与覆盖能力，体会「为什么偏偏 MPDU 要用最重的 CRC-32」。
- **回到状态机全局**：本讲的 `S_DECODE_DATA` / `S_DECODE_DONE` 是 u4-l1 顶层状态机的末端两环。建议重读 u4-l1，把「检测 → 同步 → SIGNAL/HT-SIG 校验 → DATA 解码 → FCS 校验 → 完成」整条控制流在脑中连成一条线，理解每一处 `status_code` / `E_*` 错误码在什么条件下产生、又如何让状态机回到 `S_WAIT_POWER_TRIGGER` 等待下一包。
- **进入验证单元**：若你想亲手验证「FCS 真的对每个样本都通过」，可进入 u5-l2（交叉验证框架 `test.py`）与 u5-l3（测试台 `dot11_tb.v`），学习如何把 `fcs_ok` 这类信号也纳入自动化比对流水，并尝试在测试台里补一个把 `fcs_ok` 落盘的探针（作为练习，注意只在本地副本操作）。
