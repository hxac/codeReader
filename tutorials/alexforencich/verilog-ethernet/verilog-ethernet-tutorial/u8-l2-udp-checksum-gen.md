# UDP 校验和生成

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 UDP 校验和**为什么要包含 IP 伪头部**、它由哪些字段相加而成，并能手算一个 UDP 包的校验和。
- 读懂 `udp_checksum_gen`（8 位）与 `udp_checksum_gen_64`（64 位）如何**同时**算出四样东西：UDP 长度、UDP 校验和、IP 总长度、IP 协议号。
- 理解模块为什么需要**双 FIFO**（载荷 FIFO + 头部 FIFO）——核心原因是「校验和必须看到整帧才能定值，但头部又必须和载荷一起送出」。
- 掌握 `UDP_CHECKSUM_GEN_ENABLE` 参数的「可旁路」设计：开启时代理生成校验和，关闭时退化为纯连线、由应用自行提供。

本讲是 u8-l1（UDP 收发）的延续。`udp` 顶层在发送方向可选地插入本模块，它处在 `udp_ip_tx` 之前，负责把「缺了长度和校验和的 UDP 头」补全后再交给下层封装。

## 2. 前置知识

### 2.1 反码求和（one's complement sum）

UDP 校验和与 IPv4 头校验和（见 u7-l1）用的是同一套数学：**16 位反码求和**。把所有参与计算的 16 位字当作普通无符号数相加，再把「溢出进位」折回低位相加，最后整体取反。它的好处是「先取反再求和」与「先求和再取反」等价，硬件上可以边发边算。

设累加和为 \(S\)（可能超过 16 位，记其高位为 \(H\)、低位为 \(L\)），则折叠与取反为：

\[
S_{\text{folded}} = L + H,\qquad \text{checksum} = \sim S_{\text{folded}}
\]

若 \(S_{\text{folded}}\) 仍有进位，再把进位折回一次即可。本模块用一个 32 位累加器 `checksum_reg` 容纳进位，帧末统一折叠一次。

### 2.2 UDP 报文与 IP 伪头部

UDP 报文 = 8 字节 UDP 头（源端口、目的端口、长度、校验和各 2 字节）+ 载荷。其中「长度」含头本身。UDP 校验和的覆盖范围**不止 UDP 报文**，还前置了一段由 IP 层字段拼成的「伪头部」：

| 伪头部字段 | 字节数 |
|---|---|
| 源 IP | 4 |
| 目的 IP | 4 |
| 全 0 | 1 |
| 协议（UDP=17=0x11） | 1 |
| UDP 长度 | 2 |

伪头部的作用是让 UDP 校验和**额外校验 IP 层的地址与协议**，防止 IP 层把报文投递到错误的端口/协议。计算时 UDP 头里的「校验和」字段本身填 0。注意：**伪头部里的「UDP 长度」与 UDP 头里的「长度」是同一个值，因此该长度值在求和中出现两次。**

### 2.3 与 u2-l1 的关系

u2-l1 讲的 `lfsr.v` 是 CRC-32 引擎，服务于以太网 FCS（链路层）。而 UDP/IPv4 校验和**不是 CRC**，只是反码求和，实现上是一段简单的加法器，不依赖 `lfsr`。本讲的「依赖 u2-l1」指的是「同属校验类构建块、共享校验和思想」，但底层数学不同——请勿混淆 FCS（多项式除法）与反码求和（逐字相加）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [rtl/udp_checksum_gen.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v) | 8 位数据通路的主模块：边收载荷边累加，帧末定值，双 FIFO 缓存。 |
| [rtl/udp_checksum_gen_64.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen_64.v) | 64 位变体：每拍处理 8 字节，用流水线寄存器拆分宽加法。 |
| [rtl/udp.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp.v) | UDP 顶层，用 `CHECKSUM_GEN_ENABLE` 在「实例化本模块」与「直连旁路」之间切换。 |
| [rtl/udp_complete.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_complete.v) | 顶层 UDP 栈，把参数以 `UDP_CHECKSUM_GEN_ENABLE` 名字对外暴露。 |
| rtl/lfsr.v | （仅作概念对照）FCS 的 CRC-32 引擎，与本讲的反码求和无关。 |

模块依赖 `lib/axis/rtl/axis_fifo.v`（同步 FIFO），用于缓存载荷。

## 4. 核心概念与源码讲解

本讲对应三个最小模块：**UDP/IP 长度计算**、**含伪头部的校验和**、**FIFO 双缓存**。三者共用同一套状态机，因此先看整体结构，再分别拆解。

### 4.1 模块总览：一次补齐四样东西

#### 4.1.1 概念说明

`udp_checksum_gen` 是一个**带内计算 + 旁带补全**的模块。它的输入是「头部字段并行 + 载荷走 AXI-Stream」的标准 UDP 发送接口，但**故意缺少四个字段**：输入端口里没有 `s_ip_length`、`s_ip_protocol`、`s_udp_length`、`s_udp_checksum`（见 [rtl/udp_checksum_gen.v:L46-L68](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L46-L68)）。这四个值都由本模块在输出侧生成：

- `m_udp_length`：UDP 报文总长（头 + 载荷），由载荷字节计数得到。
- `m_udp_checksum`：含伪头部的反码校验和。
- `m_ip_length`：IP 总长 = UDP 长度 + 20 字节 IPv4 头，见 [rtl/udp_checksum_gen.v:L320](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L320)。
- `m_ip_protocol`：硬编码为 UDP 的协议号 `8'h11`（17），见 [rtl/udp_checksum_gen.v:L325](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L325)。

换句话说，应用层只需要给出端口、IP、载荷，长度与校验和完全由硬件兜底。

#### 4.1.2 核心流程（状态机）

8 位模块用一个 6 状态机驱动累加，见 [rtl/udp_checksum_gen.v:L145-L151](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L145-L151)：

```
IDLE          等头部握手；命中则锁存头部、初始化累加器
  │
  ▼
SUM_HEADER_1  累加源 IP（2 个 16 位字）
  │
  ▼
SUM_HEADER_2  累加目的 IP（2 个 16 位字）
  │
  ▼
SUM_HEADER_3  累加源/目的端口；frame_ptr 置 8（UDP 头长度）
  │
  ▼
SUM_PAYLOAD   逐字节累加载荷（同时写入载荷 FIFO）
  │  (tlast)
  ▼
FINISH_SUM    折叠 32 位累加器、取反，把头部（含长度/校验和）写入头部 FIFO
  │
  ▼
IDLE
```

关键设计：**载荷一边被累加，一边被写进载荷 FIFO**；等帧末算出校验和后，头部连同长度/校验和写进头部 FIFO。输出侧再让「头部」和「缓存的载荷」配对送出。

---

### 4.2 UDP/IP 长度计算

#### 4.2.1 概念说明

UDP 长度 = 8（UDP 头）+ 载荷字节数。模块用一个计数器 `frame_ptr_reg` 记录长度：在进入 `SUM_PAYLOAD` 前把它置为 8（代表 UDP 头已经占了 8 字节），随后每收到 1 字节载荷就 +1，帧末时 `frame_ptr_reg` 恰好等于 UDP 长度。IP 总长则在此基础上再加 20（标准 IPv4 头）。

#### 4.2.2 核心流程

1. `SUM_HEADER_3` 把 `frame_ptr` 置 8，见 [rtl/udp_checksum_gen.v:L480](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L480)。
2. `SUM_PAYLOAD` 每收到一字节 `frame_ptr_reg + 1`，见 [rtl/udp_checksum_gen.v:L496](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L496)。
3. 帧末把 `frame_ptr_reg` 作为 `udp_length` 写入头部 FIFO，见 [rtl/udp_checksum_gen.v:L374](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L374)。
4. 输出侧组合 `m_ip_length = m_udp_length_reg + 16'd20`，见 [rtl/udp_checksum_gen.v:L320](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L320)。

#### 4.2.3 源码精读

长度计数器的递增与帧尾判定（[rtl/udp_checksum_gen.v:L496-L502](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L496-L502)）：

```verilog
frame_ptr_next = frame_ptr_reg + 1;

if (s_udp_payload_axis_tlast) begin
    state_next = STATE_FINISH_SUM;
end else begin
    state_next = STATE_SUM_PAYLOAD;
end
```

每个有效载荷拍 `frame_ptr` 加 1；`tlast` 一到就转入 `FINISH_SUM`。由于 `frame_ptr` 进入此状态前已被置 8，处理 N 字节载荷后值为 \(8 + N\)，正是 UDP 长度。这个值随后被写进头部 FIFO 的 `udp_length_mem`（L374），输出时同时派生出 IP 长度（L320）。

#### 4.2.4 代码实践

**目标**：确认长度公式与计数起点。

1. 打开 [rtl/udp_checksum_gen.v:L480](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L480)，看到 `frame_ptr_next = 8`。
2. 在 L496 看到每拍 `+1`。
3. 假设发送 100 字节载荷，手算 `frame_ptr` 终值应为 \(8 + 100 = 108\)。
4. 查 L320 `m_ip_length = m_udp_length_reg + 20`，确认输出 IP 长度应为 \(108 + 20 = 128\)。

**预期结果**：UDP 长度 = 载荷字节数 + 8，IP 长度 = UDP 长度 + 20。无需运行仿真即可从源码确认这两条公式；若要验证，可在仿真中给 100 字节载荷，断言 `m_udp_length==108 && m_ip_length==128`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `frame_ptr` 在 `SUM_HEADER_3` 被置为 8 而不是 0？
**答案**：因为 UDP 长度包含 8 字节 UDP 头本身。把它预置为 8，再对载荷字节逐个 +1，终值就直接等于 UDP 长度，无需帧末再补加 8。

**练习 2**：IP 长度为什么固定加 20，而不是 `(IHL-5)*4` 之类？
**答案**：本库的 IPv4 实现不支持 IP 选项（IHL 恒为 5，见 u7-l1），IP 头固定 20 字节，故 `m_ip_length = m_udp_length + 20`。

---

### 4.3 含伪头部的校验和

#### 4.3.1 概念说明

如前置知识所述，UDP 校验和 = 反码求和（伪头部 + UDP 头 + 载荷），其中 UDP 头的校验和字段计为 0，且「UDP 长度」在伪头部和 UDP 头里各出现一次（共两次）。模块并没有按教科书「先组好伪头部再统一求和」，而是把求和**摊到状态机的每一拍**：头部字段分三拍加完，载荷逐字节加，最后折叠取反。

#### 4.3.2 核心流程：累加器的「分期」与「预扣」

累加器 `checksum_reg` 是 32 位（容纳进位）。关键是一个**精巧的预扣技巧**：

- 进入 `IDLE` 时把累加器初始化为 `16'h0011 + 16'h0010`（[L460](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L460)）：
  - `0x0011` = 伪头部的「全 0 + 协议 0x11」那个 16 位字。
  - `0x0010` = 16 = UDP 头长度 8 的两倍，对应「两个长度字段各自包含的 8 字节头部部分」。
- 之后每收到 1 字节载荷，累加器额外 `+2`（[L491/L493](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L491)），对应「两个长度字段各自包含的载荷部分」。

如此一来，N 字节载荷对两个长度字段的总贡献 = 预扣的 16 + 逐字节累加的 \(2N\) = \(2(8+N)\) = \(2 \times \text{UDP\_长度}\)，恰好等于「长度值出现两次」。这样模块**无需等帧末知道总长再回头补加**，而是在流式处理中自然累计完成。

帧末 `FINISH_SUM` 把 32 位累加器折叠成 16 位并取反（[L507-L513](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L507-L513)）：

\[
\text{part} = S[15:0] + S[31:16],\qquad \text{checksum} = \sim(\text{part}[15:0] + \text{part}[16])
\]

其中 `part[16]` 是折叠产生的进位，再加回一次，保证最终无残留进位。

#### 4.3.3 源码精读

**(a) 头部分三拍累加**（伪头部 + UDP 头的端口部分），见 [rtl/udp_checksum_gen.v:L467-L482](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L467-L482)：

```verilog
STATE_SUM_HEADER_1: // 源 IP 高低两个字
    checksum_next = checksum_reg + ip_source_ip_reg[31:16] + ip_source_ip_reg[15:0];
STATE_SUM_HEADER_2: // 目的 IP 高低两个字
    checksum_next = checksum_reg + ip_dest_ip_reg[31:16] + ip_dest_ip_reg[15:0];
STATE_SUM_HEADER_3: // 源端口 + 目的端口
    checksum_next = checksum_reg + udp_source_port_reg + udp_dest_port_reg;
    frame_ptr_next = 8;
```

注意校验和里**不包含**以太网 MAC 地址、EtherType，也**不包含** IPv4 头本身（IP 头有自己的头校验和，见 u7-l1）。这里只加伪头部需要的 IP 地址 + 端口。

**(b) 载荷逐字节累加 + 长度预扣**，见 [rtl/udp_checksum_gen.v:L490-L494](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L490-L494)：

```verilog
if (frame_ptr_reg[0]) begin
    checksum_next = checksum_reg + {8'h00, s_udp_payload_axis_tdata} + 2;  // 奇字节→低位
end else begin
    checksum_next = checksum_reg + {s_udp_payload_axis_tdata, 8'h00} + 2;  // 偶字节→高位
end
```

用 `frame_ptr_reg[0]`（字节序奇偶）决定本字节放进 16 位字的高 8 位还是低 8 位——这正是「网络字节序两两配对」的硬件实现：相邻两字节组成一个 16 位字，首字节在高 位。每拍固定的 `+2` 即上文所述的两个长度字段的载荷贡献。若载荷字节数为奇数，最后一字节落在高位、低位隐含为 0，与规范一致。

**(c) 帧末折叠取反**，见 [rtl/udp_checksum_gen.v:L507-L513](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L507-L513)：

```verilog
STATE_FINISH_SUM: begin
    // add MSW (twice!) for proper ones complement sum
    checksum_part = checksum_reg[15:0] + checksum_reg[31:16];
    checksum_next = ~(checksum_part[15:0] + checksum_part[16]);
    hdr_valid_next = 1;          // 触发头部 FIFO 写入
    state_next = STATE_IDLE;
end
```

注释里的「add MSW (twice!)」指：把 32 位累加器的高 16 位（即累积的进位）折回低位（第一次 `+`），再把这次折叠自身产生的进位 `part[16]` 也折回（第二次 `+`）。最后取反得到校验和，同时拉高 `hdr_valid_next` 把头部连同校验和写入头部 FIFO。

#### 4.3.4 代码实践（含手算比对）

**目标**：用一组具体字段手算 UDP 校验和，理解伪头部的构成。本例为「源码阅读 + 手算」型，结论可在后续仿真中验证。

设：
- 源 IP = `192.168.1.100` = `C0A8 0164`
- 目的 IP = `192.168.1.200` = `C0A8 01C8`
- 源端口 = `0x1234`，目的端口 = `0x0BAD`
- 载荷 = `"Hi"` = `0x48 0x69`（2 字节）
- UDP 长度 = 8 + 2 = `0x000A`

参与反码求和的 16 位字（校验和字段计 0）：

```
0xC0A8  0x0164   源 IP
0xC0A8  0x01C8   目的 IP
0x0011           全0 + 协议 17
0x000A           伪头部 UDP 长度
0x1234  0x0BAD   端口
0x000A           UDP 头长度
0x0000           校验和字段（置 0）
0x4869           载荷
```

逐字相加：

\[
0xC20C + 0xC270 + 0x0011 + 0x000A + 0x1234 + 0x0BAD + 0x000A + 0x4869 = \text{0x1EAEB}
\]

折叠并取反：

\[
\text{0xEAEB} + \text{0x0001} = \text{0xEAEC},\qquad \sim\text{0xEAEC} = \text{0x1513}
\]

**预期结果**：UDP 校验和 = `0x1513`。模块用「分期预扣」算法得到的累加终值同样为 `0x1EAEB`（预扣的 `0x0010` + 逐字节 `+2×2` 与两个长度字段 `0x000A+0x000A` 等价），折叠取反后也是 `0x1513`。两种算法殊途同归。

**待本地验证**：在仿真中给 `udp_checksum_gen` 喂入上述头部与载荷，捕获 `m_udp_checksum`，确认等于 `0x1513`。可参考既有测试 `tb/test_udp_checksum_gen.py`（注意：该文件是 myhdl 时代的遗留测试，用 `iverilog` 直接编译 `rtl/udp_checksum_gen.v` + `lib/axis/rtl/axis_fifo.v` + testbench；当前 cocotb 流程需按 u1-l4/u13-l2 的方式新写 Makefile 三件套）。

#### 4.3.5 小练习与答案

**练习 1**：若载荷改为 3 字节（奇数），最后一字节在求和时如何处理？
**答案**：按 `frame_ptr_reg[0]` 判定，最后一字节落在 16 位字的高 8 位（`{data, 8'h00}`），低 8 位隐含为 0。这与规范「奇数长度末尾补 0 字节」一致。

**练习 2**：为什么累加器是 32 位而不是 16 位？
**答案**：反码求和会产生进位。用 32 位累加器把进位暂存到高 16 位，帧末再统一折叠，避免每拍都要做「加完立刻折回」的组合逻辑，时序更友好。

**练习 3**：模块算出的校验和里，以太网 MAC 地址和 IPv4 头校验和参与了吗？
**答案**：都没有。UDP 校验和只覆盖「IP 伪头部 + UDP 头 + 载荷」。以太网 MAC 地址属链路层，IPv4 有独立的头校验和，二者均不进入 UDP 校验和。

---

### 4.4 FIFO 双缓存

#### 4.4.1 概念说明

这是本模块最值得理解的设计。矛盾在于：

- 校验和与长度**必须看到整帧最后一字节**才能定值（长度依赖总字节数，求和依赖全部载荷）。
- 但头部又必须和载荷**配对送出**给下游 `udp_ip_tx`，不能等帧结束才开始发。

解决办法是**两路 FIFO**：

- **载荷 FIFO**：把流入的载荷先存起来，延迟一拍/若干拍再输出，给「算校验和」争取时间。
- **头部 FIFO**：帧末算完校验和后，把头部字段（含 `udp_length`、`udp_checksum`）存起来，等下游取。

两者配合：头部先就绪（在头部 FIFO 里排队），载荷随后从载荷 FIFO 跟上，下游按「头部 + 载荷流」消费。头部 FIFO 深度只有 8，意味着最多允许 8 帧的头部「已算完、待输出」在排队，而它们的载荷可能还在载荷 FIFO（深 2048）里慢慢排空。

#### 4.4.2 核心流程

1. 载荷进入 `SUM_PAYLOAD` 时，`shift_payload_in` 拉高，载荷被写入**载荷 FIFO**（[L242-L243](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L242-L243)）。
2. 帧末 `FINISH_SUM` 拉高 `hdr_valid_next`，触发**头部 FIFO** 写入（[L340-L348](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L340-L348)）。
3. 头部 FIFO 的读出与下游 `m_udp_hdr_ready` 握手（[L387-L399](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L387-L399)）。
4. `IDLE` 时只有头部 FIFO 不满才接收新头部（`s_udp_hdr_ready_next = header_fifo_ready`，[L453](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L453)）。

#### 4.4.3 源码精读

**(a) 载荷 FIFO**：实例化 `lib/axis` 的同步 `axis_fifo`，深度 2048、8 位数据、`FRAME_FIFO(0)`（普通流式 FIFO，非整帧模式），见 [rtl/udp_checksum_gen.v:L203-L239](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L203-L239)：

```verilog
axis_fifo #(
    .DEPTH(PAYLOAD_FIFO_DEPTH),
    .DATA_WIDTH(8),
    .KEEP_ENABLE(0),
    ...
    .FRAME_FIFO(0)
) payload_fifo ( ... );
```

`shift_payload_in` 同时控制写请求与上游 `tready`（[L242-L243](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L242-L243)）：只有在 `SUM_PAYLOAD` 状态才接收载荷，保证头部握手与载荷流严格同步。

**(b) 头部 FIFO**：手写的存储器阵列（不调 `axis_fifo`），每个槽存一份完整头部 + `udp_length` + `udp_checksum`，见 [rtl/udp_checksum_gen.v:L259-L276](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L259-L276)。指针比地址多 1 位作「回卷标志」，用经典的「多余位」方案判满判空（[L300-L303](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L300-L303)）：

```verilog
// full when first MSB different but rest same
wire header_fifo_full  = ((wr_ptr[MSB] != rd_ptr[MSB]) &&
                          (wr_ptr[LSBs] == rd_ptr[LSBs]));
// empty when pointers match exactly
wire header_fifo_empty = wr_ptr == rd_ptr;
```

因为是单时钟同步 FIFO，可用二进制指针；若是跨时钟域则需 Gray 码指针（对比 u5-l1 的 `axis_async_fifo`）。

**(c) 帧末写入**：`hdr_valid_reg` 由 `FINISH_SUM` 置位后，下一拍把当时的 `frame_ptr_reg`（长度）与 `checksum_reg[15:0]`（校验和）连同头部字段写入头部 FIFO，见 [rtl/udp_checksum_gen.v:L374-L375](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L374-L375)。注意此时 `checksum_reg` 已是 `FINISH_SUM` 取反后的最终值，`[15:0]` 只是截取（最终值本就落在 16 位内）。

#### 4.4.4 代码实践

**目标**：定位两个 FIFO 并理解它们的分工与时序关系。

1. 打开 [rtl/udp_checksum_gen.v:L203](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L203)（载荷 FIFO 实例）与 [L259](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L259)（头部 FIFO 存储器）。
2. 跟踪 `shift_payload_in`（[L485](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L485)）与 `hdr_valid_next`（[L511](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L511)）何时被拉高。
3. 在脑中画一条时间轴：第 0 拍头部握手 → 第 1~3 拍加 IP/端口 → 第 4 拍起每拍「累加 1 字节 + 写载荷 FIFO」→ 帧末拍折叠取反 + 写头部 FIFO。

**预期结果**：载荷 FIFO 在 `SUM_PAYLOAD` 期间持续写入；头部 FIFO 在 `FINISH_SUM` 后的下一拍写入一次（每帧一次）。输出侧的 `m_udp_hdr_valid` 由头部 FIFO 非空驱动（[L387-L399](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L387-L399)），与载荷 FIFO 的输出在下游自然配对。

#### 4.4.5 小练习与答案

**练习 1**：为什么载荷 FIFO 用现成的 `axis_fifo`，而头部 FIFO 用手写存储器阵列？
**答案**：载荷是标准 AXI-Stream 流，`axis_fifo` 正好匹配；头部是大量并行字段（MAC、IP、端口、长度、校验和），手写一组并行存储器阵列更直接，也避免把头部塞进 AXI 接口的额外封装开销。

**练习 2**：如果头部 FIFO 满了会发生什么？
**答案**：`header_fifo_ready` 变 0，`IDLE` 状态下 `s_udp_hdr_ready_next` 也变 0（[L453](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L453)），模块通过反压告诉上游「暂时别给新头部」，从而实现流控。

---

### 4.5 64 位变体与可旁路设计

#### 4.5.1 概念说明

`udp_checksum_gen_64`（10G/25G 数据通路）处理 64 位（8 字节）数据，每拍处理一个完整字。它的状态机更短（[L147-L152](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen_64.v#L147-L152)），但因为每拍要把 8 字节拆成 4 个 16 位字相加，加法器很宽，于是用两个流水线寄存器 `checksum_temp1/2_reg` 把加法拆到两拍，换来更好的时序。

另一个层面，`udp` 顶层通过 `CHECKSUM_GEN_ENABLE` 参数决定是否实例化本模块：关闭时整段逻辑被 `generate` 跳过，模块退化为纯 `assign` 连线（零面积），由应用自己提供 `s_udp_checksum` 与 `s_udp_length`。

#### 4.5.2 核心流程

64 位版状态机：

```
IDLE          锁存头部，把源 IP 两半预存入 temp1/temp2
  │
  ▼
SUM_HEADER    把 temp1+temp2 折入主累加器，同时算好 dstIP/端口进 temp
  │
  ▼
SUM_PAYLOAD   每拍把 8 字节拆成 4 个 16 位字累加（用 temp 流水一拍），按 tkeep 计有效字节数
  │ (tlast)
  ▼
FINISH_SUM_1  排空流水线（把最后一拍还在 temp 里的和并入主累加器）
  │
  ▼
FINISH_SUM_2  折叠取反（同 8 位版的 FINISH_SUM）
```

#### 4.5.3 源码精读

**(a) 宽加法的流水化**：每拍把 64 位 `tdata` 按 `tkeep` 拆成低 4 字节（进 `checksum_temp1_next`）与高 4 字节（进 `checksum_temp2_next`），用字节内奇偶决定高低位配对，见 [rtl/udp_checksum_gen_64.v:L502-L520](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen_64.v#L502-L520)。下一拍再把 temp 并入主累加器（[L523](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen_64.v#L523)）：

```verilog
// 上一拍算好的 temp 在本拍并入主累加器；同时算本拍的新 temp
checksum_next = checksum_reg + checksum_temp1_reg + checksum_temp2_reg + (word_cnt << 1);
```

`(word_cnt << 1)` 即「本拍有效字节数 × 2」，对应两个长度字段的贡献——与 8 位版「每字节 +2」等价，只是按字聚合。

**(b) 有效字节数 `word_cnt`**：由 `tkeep` 推断本拍有几个有效字节（用于长度），见 [rtl/udp_checksum_gen_64.v:L494-L497](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen_64.v#L494-L497)。它假设 `tkeep` 是从最低位起连续的 1（即末字非整字时高位为 0），符合本库 AXI 约定。

**(c) 排空流水线**：因 temp 滞后主累加器一拍，`tlast` 后必须多一个 `FINISH_SUM_1` 把残留的 temp 并入，再见 [rtl/udp_checksum_gen_64.v:L536-L540](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen_64.v#L536-L540)。这是 64 位版比 8 位版多一个收尾状态的原因。

**(d) 可旁路**：在 `udp.v` 里，`generate if (CHECKSUM_GEN_ENABLE)` 实例化本模块（[rtl/udp.v:L256-L321](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp.v#L256-L321)），`else` 分支则把输入输出逐根 `assign` 直连，并把 `tx_udp_checksum = s_udp_checksum`（[rtl/udp.v:L323-L351](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp.v#L323-L351)）。顶层 `udp_complete` 以 `UDP_CHECKSUM_GEN_ENABLE`（默认 1）对外暴露该参数，见 [rtl/udp_complete.v:L39](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_complete.v#L39) 与 [L522](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_complete.v#L522)。

#### 4.5.4 代码实践

**目标**：对比 8 位与 64 位两版的收尾状态，并验证旁路分支。

1. 对比 [rtl/udp_checksum_gen.v:L507-L513](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L507-L513)（8 位，单状态 `FINISH_SUM`）与 [rtl/udp_checksum_gen_64.v:L536-L547](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen_64.v#L536-L547)（64 位，两状态 `FINISH_SUM_1/2`）。
2. 打开 [rtl/udp.v:L323](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp.v#L323)，浏览到 L351，确认 `else` 分支里 `tx_udp_checksum = s_udp_checksum`——即关闭后由应用负责提供校验和。

**预期结果**：8 位版单拍收尾；64 位版因流水线需两拍收尾。旁路开启时模块零面积、纯组合连线。

**待本地验证**：可在仿真中分别用 `CHECKSUM_GEN_ENABLE=1/0` 例化 `udp`，对比资源或行为差异。

#### 4.5.5 小练习与答案

**练习 1**：64 位版为什么需要 `FINISH_SUM_1`，而 8 位版不需要？
**答案**：64 位版用 `checksum_temp1/2_reg` 把宽加法拆成两拍流水，`tlast` 到来时最后一拍的有效数据还停留在 temp 寄存器里没并入主累加器，需要 `FINISH_SUM_1` 排空；8 位版每拍即时并入，无需排空。

**练习 2**：什么时候应该把 `UDP_CHECKSUM_GEN_ENABLE` 设为 0？
**答案**：当应用层已经自行算好（或不需要）UDP 校验和时——例如某些专用协议在受限环境里为省资源跳过 UDP 校验和（UDP 校验和在 IPv4 下可选），或上游已提供正确值。关闭后模块完全消失，由 `s_udp_checksum/s_udp_length` 直通输出。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「输入 → 计算 → 验证」的完整跟踪。

**任务**：给定一个 UDP 发包场景，跟踪 `udp_checksum_gen` 内部信号的变化，并确认输出。

场景：源 IP `192.168.1.128`、目的 IP `192.168.1.10`、源端口 `0x1234`、目的端口 `0x04D2`(1234)、载荷为 4 字节 `DE AD BE EF`。

步骤：

1. **算长度**：UDP 长度 = 8 + 4 = `0x000C`(12)；IP 长度 = 12 + 20 = `0x0020`(32)。确认 `frame_ptr` 在 `SUM_HEADER_3` 置 8，处理 4 字节后到 12（[L480](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L480)、[L496](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L496)）。
2. **手算校验和**：列出伪头部 + UDP 头 + 载荷的所有 16 位字（源/目的 IP、`0x0011`、两个 `0x000C`、端口、`0xDEAD`、`0xBEEF`），反码求和后折叠取反。
3. **对照源码路径**：在脑中走一遍 `IDLE → SUM_HEADER_1/2/3 → SUM_PAYLOAD(4 拍) → FINISH_SUM`，确认累加器初值 `0x0021`（[L460](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L460)）、每拍 `+2`（[L491/L493](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L491)）。
4. **确认双 FIFO 时序**：4 拍载荷写入载荷 FIFO（[L242](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L242)）；`FINISH_SUM` 后头部（含长度 12 与算出的校验和）写入头部 FIFO（[L374-L375](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L374-L375)）。
5. **（可选，待本地验证）** 写一个最小 cocotb testbench：例化 `udp_checksum_gen`，用 `udp_ep`/`axis_ep` 注入上述头部与 4 字节载荷，捕获 `m_udp_checksum` 与 `m_udp_length`，断言与手算一致。

**预期结果**：`m_udp_length = 0x000C`，`m_ip_length = 0x0020`，`m_udp_checksum` 与第 2 步手算值相等；`busy` 在 `IDLE` 之外为高（[L532](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_checksum_gen.v#L532)）。

## 6. 本讲小结

- `udp_checksum_gen` 一次补齐四样东西：UDP 长度（载荷计数）、UDP 校验和（反码求和）、IP 总长（UDP 长度 +20）、IP 协议号（硬编码 `0x11`）。
- UDP 校验和覆盖**伪头部 + UDP 头 + 载荷**，伪头部含源/目的 IP、协议、UDP 长度；其中 UDP 长度在求和中出现两次。
- 模块用「分期预扣」技巧：IDLE 预扣 `0x0011+0x0010`、每字节 `+2`，使长度贡献在流式处理中自然累计，无需帧末回头补加。
- 帧末用 32 位累加器折叠取反得到校验和；8 位版单状态收尾，64 位版因宽加法流水化而需两状态收尾。
- **双 FIFO** 是核心结构：载荷 FIFO（深 2048）缓存流式载荷，头部 FIFO（深 8）缓存算完的头部，二者配对输出，化解「校验和需看完整帧」与「头部须与载荷同行」的矛盾。
- `UDP_CHECKSUM_GEN_ENABLE`（默认开）控制可旁路：关闭时 `udp` 顶层用 `generate else` 把模块退化为纯连线，零面积，由应用自供校验和。

## 7. 下一步学习建议

- **u8-l3（udp_complete 顶层 UDP 协议栈）**：看本模块如何被 `udp` → `udp_complete` 串进完整 UDP 栈，端到端验证端口、长度、校验和的处理。
- **对比 u7-l1 的 IPv4 头校验和**：同样是反码求和，但覆盖范围不同（IP 头校验和只覆盖 IP 头本身，UDP 校验和覆盖伪头部 + UDP），体会「为什么 UDP 要额外引入伪头部」。
- **阅读 `tb/test_udp_checksum_gen.py`**：虽然是 myhdl 遗留测试，但其中的驱动方式与字段构造可作为编写现代 cocotb 版 testbench 的参考（结合 u13-l2 的三件套方法）。
- **延伸到 `lib/axis/rtl/axis_fifo.v`**：本模块的载荷 FIFO 直接复用它，理解其同步 FIFO 实现有助于看清本模块的缓存细节。
