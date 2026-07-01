# 存储网关与地址空间序列化

## 1. 本讲目标

本讲承接 u4-l1（时钟域跨越）留下的一个关键悬念：**localbus 的读侧为什么难**。u4-l1 的结论是——localbus 写侧是单向数据流，一行 `data_xdomain` 就能搬过时钟域；而读侧需要「请求—响应」握手，可 Bedrock 的 localbus 偏偏「刻意拒绝任何握手与等待状态」。那么，网络上的主机到底怎么通过 UDP 读到 FPGA 里的寄存器？

本讲给出的答案是一个精巧的工程妥协：**把读延迟在综合时固定下来**。只要 localbus 读的往返周期数在编译期就完全确定，就根本不需要握手——按约定的拍数去取数据即可。

学完本讲，你应当能够：

- 说清楚 **LASS（Lightweight Address Space Serialization，轻量地址空间序列化）** 协议如何把一组「读/写 + 地址 + 数据」打包进一个 UDP 包；
- 读懂 [`badger/mem_gateway.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gateway.v) 如何把字节流拆解成 localbus 周期、如何用一个移位寄存器实现的「读流水线」在固定拍数后回填读数据；
- 用源码里的真实参数 `read_pipe_len` 与 `n_lat` 解释「固定延迟」在综合时是如何被钉死的；
- 理解 [`badger/xformer.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/xformer.v) 与 [`badger/lb_gateway.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/lb_gateway.v) 在数据通路中各自扮演的角色。

## 2. 前置知识

在进入源码前，先用大白话对齐几个概念。

- **localbus（回顾 u2-l2 / u4-l1）**：Bedrock 自用的极简片上总线，24 位字地址、32 位读写数据、一根 `strobe`（周期有效）+ 一根读/写方向线。**没有 ready、没有 waitstate、没有握手**。它的哲学是「周期数在综合时定死，不在运行时协商」。
- **请求—响应握手**：像 AXI 那样的总线，主设备发读请求后要等从设备拉起 `ready`/`rvalid` 才能取数据，延迟可变。localbus 拒绝这套，所以「读」必须靠别的方式对齐。
- **固定延迟（fixed latency）**：如果主设备知道「发出读地址后，从设备的数据一定会在第 N 拍出现在总线上」，那 N 拍后直接采样即可，无需握手。本讲的主角就是把 N 变成一个综合期参数。
- **UDP / 以太网帧 / 网络字节序（大端）**：LASS 协议跑在 UDP 之上，按 RFC-791 惯例「在线上」按字节发送，高位字节先发。`mem_gateway` 处理的是已经从以太网帧里剥出来的字节流（`idata`），以太网/IP/UDP 的解析由 Packet Badger 的其它模块完成（u4-l4 详讲）。
- **Packet Badger 的 client（客户端插件）接口**：每个 UDP 端口对应一个 client。client 收到的是对齐好的字节流 `idata` + 数据有效选通 `raw_s` + 包级有效 `raw_l`，回送的也是字节流 `odata`。`mem_gateway` 就是其中一个 client——把 UDP 字节流翻译成 localbus 主设备。

> 名词澄清：本讲学习规格里提到的 `LB_READ_DELAY` 是大纲对「固定读延迟」的**概念性称呼，并非源码里的真实符号**。源码中真正决定这个延迟的参数是 `read_pipe_len`（以及约束它的 `n_lat`）。本讲会全程用源码里的真实名字，并在第 5 节综合实践里把它们对应起来。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [`badger/mem_gate.md`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gate.md) | LASS 协议的权威文档：包结构、命令编码、实用约束。 |
| [`badger/mem_gateway.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gateway.v) | 核心模块。把 UDP 字节流 ↔ localbus 主端口互转，用固定延迟实现无握手读。 |
| [`badger/lb_gateway.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/lb_gateway.v) | `mem_gateway` 的薄包装：把端口名重命名成 newad.py / picorv32 / DRP 等「更眼熟」的 localbus 命名。 |
| [`badger/xformer.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/xformer.v) | 上游变换器：把收到的包重排、分发选通到 7 个 client，并把 client 回送的 `odata` 与 ICMP/UDP 校验和拼回成发送包。 |
| [`projects/ctrace/wctrace_top.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/wctrace_top.v) | 一个真实的轻量工程顶层，里面的「手写解码器」最能直观说明固定延迟读的契约。 |
| [`dsp/reg_delay.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/reg_delay.v) | 移位寄存器延迟原语 \(z^{-n}\)，`mem_gateway` 内部多次实例化它来做流水线对齐。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**LASS 协议** → **mem_gateway 主桥（核心）** → **xformer / lb_gateway 适配层**。

### 4.1 地址空间序列化协议（LASS）

#### 4.1.1 概念说明

「地址空间序列化」要做的事情很朴素：把一组对传统 VME 式地址空间的读/写操作，**串行化**成一串字节，好塞进一个 UDP 包里传过去；FPGA 端处理完，再把结果塞进**结构完全相同**的回包里传回来。

文档 [`badger/mem_gate.md`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gate.md) 开宗明义：

> LASS is an encoding of reads and writes to a traditional VME-like address space. It is designed for transport over UDP …

它刻意与具体总线解耦——AXI、Wishbone、还是 Bedrock 的 localbus 都能用——底层总线固定为 32 位数据 + 24 位字地址，**不支持字节访问**（见 [mem_gate.md:1-12](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gate.md#L1-L12)）。

#### 4.1.2 核心流程

一个 LASS 包 = **64 位事务 ID** + 一串连续事务。事务分两类：

1. **单拍事务（single-beat）**：一次读或写，编码成 64 位 = 8 位命令 + 24 位地址（共 32 位）+ 32 位数据。
2. **块传输事务（burst）**：对连续地址的一串读或写，用「命令 + 重复计数」打头，后面跟多个 32 位数据字。

关键设计取舍（见 [mem_gate.md:153-183](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gate.md#L153-L183)）：

- **回包与请求包结构、长度完全相同**。读请求在「将来填结果的位置」放填充；写请求的回包原样回显写入的数据。以太网是全双工对称带宽，这种「等长回包」天然不会让回送通道过载，FPGA 因此能保证「每个请求必应」。
- 64 位 ID 由主机自选，FPGA 原样回显，软件据此把回包与未决请求配对。
- 1500 MTU 下，UDP 净荷上限 1472 字节，扣掉 64 位 ID 后最多 183 个单拍事务，或一个含 364 个数据字的块传输。

#### 4.1.3 源码精读

单拍事务的包结构（[mem_gate.md:28-44](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gate.md#L28-L44)）：

```
|                    Transaction ID [31:0]                      |
|                    Transaction ID [63:32]                     |
|    Command    |                    Address 0                  |
|                           Data 0                              |
|    Command    |                    Address 1                  |
|                           Data 1                              |
```

注意「命令 + 24 位地址」挤在同一个 32 位字里——高 8 位是命令，低 24 位是地址，正好对齐到 32 位边界，省掉了任何跨字拼接。

8 位命令字段里只有最低 2 位的 `OP` 子字段有意义（[mem_gate.md:113-126](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gate.md#L113-L126)）：

```
OP [1:0]:
   2'b00 - Write
   2'b01 - Read
   2'b10 - Burst
   2'b11 - Reserved
```

> ⚠️ **文档与实现的差异（重要）**：`mem_gate.md` 把 `OP` 标注在命令字节的 `[2:1]` 位；而 `mem_gateway.v` 实际取的读/写位是 `big_r[60]`、块传输检测位是 `next_isr[29]`（即命令字节的另两个比特）。这是 Bedrock 常见的「文档描述协议轮廓、代码落地具体比特位」的轻微错位。**对本讲而言，你只需记住：命令字节里有一个「读/写」位和一个「块传输」位**，精确比特以 [`mem_gateway.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gateway.v) 为准。

文档末尾还点明了它的历史与定位（[mem_gate.md:185-211](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gate.md#L185-L211)）：LASS 自 2010 年起在 LBNL 使用，2018 年由 Packet Badger 重写以适应高流量网络；它与 EtherBone 思路高度相似，只是更细粒度、地址限 24 位、不专绑 Wishbone。

#### 4.1.4 代码实践

**目标**：用 `mem_gate.md` 的包图，手工拼一个「两个单拍读」的请求包，验证你真的看懂了字节布局。

**步骤**：

1. 读 [mem_gate.md:28-44](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gate.md#L28-L44) 的单拍事务图。
2. 假设事务 ID = `0x0000000000000000`，第一笔读地址 `0x000000`，第二笔读地址 `0x000001`，数据段填 `0x00000000` 占位。按网络字节序（大端）逐字节写出整个包。
3. 对照 README 给的真实示例（[`badger/README.md:153`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/README.md#L153)）：字符串 `sillyone` 是 8 字节 ID，随后 `T\x1\x0\x0yyyy` 里 `T`(=0x54) 是命令字节、`\x1\x0\x0` 是 24 位地址、`yyyy` 是 32 位数据占位。

**需要观察的现象**：命令字节 `0x54` = 二进制 `01010100`。结合上面「文档与实现差异」的提醒，想一想为什么软件可以用一个固定的命令字节同时表达「读」。

**预期结果**：你能逐字节解释示例包里每一笔事务的边界。命令字节具体哪一位是 R/W，留到 4.2.3 在源码里确认（答案是 `big_r[60]`）。

#### 4.1.5 小练习与答案

**Q1**：为什么 LASS 强制「回包与请求包等长」？
**A**：以太网全双工、上下行带宽对称；等长回包让回送通道负载与请求通道对称，FPGA 可以保证「每请求必应」而不会在回送侧拥塞丢包（见 [mem_gate.md:155-164](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gate.md#L155-L164)）。

**Q2**：1500 MTU 下，一个包最多能塞几个单拍事务？为什么块传输能塞 364 个字却更「划算」？
**A**：单拍事务每笔占 8 字节（命令+地址 4 字节 + 数据 4 字节），(1472−8)/8 ≈ 183 笔；块传输只需一个命令头 + 一个重复计数头，之后每拍只占 4 字节数据，所以同样净荷能塞约 364 个数据字（见 [mem_gate.md:169-175](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gate.md#L169-L175)）。

---

### 4.2 mem_gateway：固定延迟的 localbus 主桥

#### 4.2.1 概念说明

这是本讲的核心。`mem_gateway` 要同时干两件事：

1. **下行（请求）**：把 LASS 字节流拆成一次次的 localbus 周期——每识别出一个「命令+地址+数据」，就向 localbus 发一个 `strobe`（读或写）。
2. **上行（响应）**：把 localbus 读回的数据，**塞回字节流里原来占位的位置**，随回包发出去。

难点全在第 2 件的「塞回原位」。localbus 没有握手，`mem_gateway` 怎么知道读数据什么时候到？

答案是 4.1 节埋的伏笔——**把读往返延迟在综合时固定下来**。源码顶部的注释写得很直白（[mem_gateway.v:20-29](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gateway.v#L20-L29)）：

> Local bus read latency is fixed, configurable at compile time.

用一个参数 `read_pipe_len`（默认 3）规定：**读选通发出后，恰好经过 `read_pipe_len` 拍，读数据一定出现在 `data_in` 上**，到点直接采样，无需任何应答信号。这正是 u4-l1 留下的「localbus 读侧难」的破解之道——**用综合期常量替代运行期握手**。

这也回扣了 u4-l1 的另一条结论：因为延迟固定、且写侧天然单向，整个 `mem_gateway` 可以和 localbus 处于同一时钟域，跨域交给 u4-l1 讲过的 `data_xdomain` 在别处完成（参见 u6-l3 中 `cryomodule.v` 的 `lb_to_1x`）。

#### 4.2.2 核心流程

`mem_gateway` 内部是一条「字节进、字节出」的流水线，中间夹着 localbus 周期。用伪代码描述主流程：

```
# —— 下行：字节流 → localbus 周期 ——
每来一个有效字节 (raw_s):
    用 c4 计数拼 32 位字；跳过前 8 字节(=64位 ID)后进入 body
    body 内每凑齐一个 32 位字:
        if 处于命令+地址相位:  big_r[63:32] <= 命令+地址; 进入数据相位
        else:                   big_r[31:0] <= 数据;       产生一次 do_op

把 big_r 连线成 localbus 输出:
    addr        = big_r[55:32]   # 24 位地址
    data_out    = big_r[31:0]    # 32 位写数据
    control_rd  = big_r[60]      # 读/写位
    control_strobe = do_op        # 周期选通（已寄存一拍）

# —— 上行：固定延迟回填读数据 ——
read_op = control_strobe & control_rd
把 read_op 推入一条 read_pipe_len 级的标记移位寄存器
当标记从末端冒出 (capture) 时:  osr <= data_in   # 到点采样读数据

# —— 字节流对齐与回送 ——
align:  把输入字节流延迟 read_pipe_len+1 拍得到 pdata
osr:    平时持续左移吞入 pdata（原样回显占位）;
        capture 拍到来时整体覆写成 data_in（读结果替换占位）
finale: 再延迟若干拍，让本 client 的 odata 与其它 client 对齐
```

固定延迟的核心数学关系只有两条：

- **读往返延迟** = `read_pipe_len` 拍（从 `control_strobe` 到 `capture`）。
- **总 client 延迟约束**：`n_lat ≥ 5 + read_pipe_len`（`n_lat` 默认 8，`read_pipe_len` 默认 3）。

即：

\[
T_{\text{read}} = \text{read\_pipe\_len}\quad(\text{周期})
\]

\[
\text{n\_lat} \geq 5 + \text{read\_pipe\_len}
\]

#### 4.2.3 源码精读

**模块端口与参数**（[mem_gateway.v:39-62](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gateway.v#L39-L62)）：

```verilog
module mem_gateway #(
    parameter read_pipe_len=3,  // minimum allowed value is 1
    parameter n_lat=8,          // minimum allowed value is 5 + read_pipe_len
    parameter enable_bursts=0
) (
    input clk,
    input [10:0] len_c,  input [7:0] idata,  input raw_l, raw_s,  // client 侧
    output [7:0] odata,
    output [23:0] addr,                                    // localbus 主端口
    output control_strobe, control_rd, control_write,
    output control_rd_valid, control_prefill,
    output [read_pipe_len:0] control_pipe_rd,
    output [31:0] data_out,  input [31:0] data_in
);
```

注意 `control_pipe_rd` 的宽度是 `read_pipe_len+1`——这是固定延迟机制的「外露接口」，下游可以看到读选通在流水线里逐拍移动。`enable_bursts` 默认关闭，是为了兼容可能把保留位写成非零的老软件（见 [mem_gate.md:193-196](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gate.md#L193-L196)）。

**下行：拆字节流为 localbus 周期**（[mem_gateway.v:70-114](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gateway.v#L70-L114)）。核心是一组小寄存器协作：

```verilog
reg [23:0] isr=0;             // 输入移位寄存器：拼 32 位字
wire [31:0] next_isr = {isr, idata};
reg [63:0] big_r=0;           // {命令+地址, 数据} 两级
reg [1:0] c4=0;               // 32 位字内的字节计数
reg data_phase=0, pre_body=0, body=0;
wire next_do_op = body & &c4 & data_phase;  // 凑齐命令+地址+数据后触发
...
assign addr          = big_r[55:32];   // 24 位地址（命令字节的低 24 位）
assign data_out      = big_r[31:0];
assign control_rd    = big_r[60];      // 命令字节中的读/写位
assign control_strobe= do_op;
assign control_write = control_strobe & ~control_rd;
```

`c4` 是个 2 位计数器，`raw_s` 有效时每拍 `+1`，到 3（`&c4` 为真）表示凑齐一个 32 位字；`pre_body`/`body` 用来跳过开头的 64 位事务 ID；`data_phase` 在「命令+地址」与「数据」两个 32 位字之间切换。`do_op` 比「数据字到位」再晚一拍（寄存输出），所以 `control_strobe` 是干净的单拍选通。

**上行：读流水线 + 固定延迟采样**（[mem_gateway.v:116-122](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gateway.v#L116-L122)）——这是本讲的灵魂：

```verilog
reg [read_pipe_len-1:0] read_pipe_markers=0;
wire read_op = control_strobe & control_rd;
always @(posedge clk) read_pipe_markers <= {read_pipe_markers[read_pipe_len-2:0], read_op};
assign control_pipe_rd = {read_pipe_markers, read_op};   // 长 read_pipe_len+1
wire capture = control_pipe_rd[read_pipe_len];            // 标记走完 read_pipe_len 拍
assign control_cd_valid = capture;
```

`read_op` 是当前拍的读选通，被推入一条 `read_pipe_len` 级的移位寄存器；当它从末端冒出（`capture`）时，正好过去了 `read_pipe_len` 拍——此刻 `data_in` 上的数据就被视为有效。**没有任何 ready/valid 信号参与**，延迟完全由参数钉死。

紧接着把读结果写进输出移位寄存器（[mem_gateway.v:124-130](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gateway.v#L124-L130)）：

```verilog
reg [31:0] osr=0;
always @(posedge clk) begin
    osr <= {osr[23:0], pdata};   // 默认: 左移吞入回显字节(占位)
    if (capture) osr <= data_in;  // 到点: 用读结果整体覆写
end
wire [7:0] xdata = osr[31:24];   // 大端先发最高字节
```

为了让「覆写」恰好落在原占位位置，输入字节流先被 `align` 延迟了 `read_pipe_len+1` 拍（[mem_gateway.v:64-67](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gateway.v#L64-L67)）。两者节拍对齐，于是 `capture` 那一拍 `osr` 被读数据整体替换，后续字节再继续左移吐出——读结果就这样天衣无缝地填进了回包的占位段。

最后是 `finale` 对齐级（[mem_gateway.v:132-135](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gateway.v#L132-L135)）：

```verilog
reg_delay #(.len(n_lat-read_pipe_len-5), .dw(8)) finale(... .din(xdata), .dout(odata));
```

这里把「总 client 延迟预算」`n_lat` 减去本模块内部已经吃掉的 `read_pipe_len+5` 拍，剩余的部分用 `reg_delay` 补足，使 `mem_gateway` 的 `odata` 与 Packet Badger 的其它 client（ARP/ICMP/SPI Flash…）拥有**统一、可预测的输出延迟**。这就是 `n_lat ≥ 5 + read_pipe_len` 的由来：补足长度不能为负。`reg_delay` 本体是 [dsp/reg_delay.v:19-37](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/reg_delay.v#L19-L37) 的一段移位寄存器，在 Xilinx 上会被推断成 SRL16。

**看一个真实工程怎么守这条契约**：[`projects/ctrace/wctrace_top.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/wctrace_top.v) 实例化 `mem_gateway` 并手写了一个极简 localbus 从端解码器（[wctrace_top.v:26-39 与 72-89](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/wctrace_top.v#L72-L89)）：

```verilog
mem_gateway #(.n_lat(n_lat)) mem_gateway_i ( ... .addr(addr),
    .control_strobe(control_strobe), .control_rd(control_rd),
    .data_out(data_out), .data_in(data_in) );

reg [31:0] lb_din=0;
assign data_in = lb_din;
always @(posedge clk) begin
    if (control_strobe & (~control_rd)) begin ... end   // 写: 译码 addr
    casez (addr[15:0])                                   // 读: 组合译码后寄存
        16'h1002:  lb_din <= {{32-AW{1'b0}}, pc_mon};
        16'h1001:  lb_din <= {31'h0, running};
        default:   lb_din <= 32'hdeadbeef;
    endcase
end
```

注意从端**没有**任何「我准备好了」的信号回送。它只是保证：在 `control_strobe`/`addr` 出现后，`lb_din` 在若干拍内（这里一拍寄存即稳定）就绪；`mem_gateway` 会在约定的 `read_pipe_len` 拍后自行采样。这就是「固定延迟读」的完整契约——**从端只管按时把数据摆上总线，主端按拍取样**。

#### 4.2.4 代码实践

**目标**：亲手数出「一次 localbus 读从发出到数据返回的固定周期数」，并解释它如何在综合时被钉死。这正是本讲规格里要求的核心实践。

**步骤**：

1. 打开 [`badger/mem_gateway.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gateway.v)。
2. 找到读选通 `read_op = control_strobe & control_rd`（[L118](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gateway.v#L118)）。
3. 顺着 `read_pipe_markers` 移位寄存器（[L117-119](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gateway.v#L117-L119)）数：`read_op` 从 `control_pipe_rd[0]` 走到 `control_pipe_rd[read_pipe_len]` 共经过 `read_pipe_len` 拍，触发 `capture`（[L121-122](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gateway.v#L121-L122)），此刻 `osr <= data_in`（[L128](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gateway.v#L128)）。
4. 回答：默认参数下，**读往返固定延迟 = `read_pipe_len` = 3 拍**。它由综合期参数 `read_pipe_len`（[L40](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gateway.v#L40)）决定，并受总预算 `n_lat`（[L41](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gateway.v#L41)，约束 `n_lat ≥ 5 + read_pipe_len`）钳制。
5. （可选，待本地验证）跑现成的 testbench 复现：`make -C badger/tests mem_gateway_check`（iverilog 路径，依赖 `udp-vpi.vpi`），或 Verilator 路径 `make -C badger/tests Vmem_gateway_check`；想看波形用 `make -C badger/tests mem_gateway.vcd`。这些目标的来源见 [`badger/tests/Makefile:62,157-162,321-326`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/tests/Makefile#L157-L162)。

**需要观察的现象**：在波形里把 `control_strobe`、`control_rd`、`control_pipe_rd[*]`、`capture`、`data_in`、`osr` 放一起。你会看到一次读选通之后，`control_pipe_rd` 的「活动位」逐拍左移，第 `read_pipe_len` 拍上 `capture` 跳起、`osr` 被读数据整体替换。

**预期结果**：读延迟严格等于 `read_pipe_len` 拍，与运行时无关。把它换成本讲规格里的概念名，就是 `LB_READ_DELAY`——**它不是源码符号，而是由 `read_pipe_len` 实现的那个综合期常量**。

#### 4.2.5 小练习与答案

**Q1**：如果把 `read_pipe_len` 从 3 调大到 6，对系统和从端各有什么影响？
**A**：读往返延迟变成 6 拍——`mem_gateway` 更晚采样 `data_in`，从端有更多时间译码（可支持更深的组合逻辑或更慢的远端寄存器）；但 `n_lat` 也必须同步增大（至少 `5+6=11`），否则 `finale` 的 `reg_delay` 长度 `n_lat-read_pipe_len-5` 会变负，综合报错。代价是整体回包延迟变长。

**Q2**：为什么 `control_pipe_rd` 宽度是 `read_pipe_len+1` 而不是 `read_pipe_len`？
**A**：因为它同时包含了「当前拍」的 `read_op`（最低位）和「过去 `read_pipe_len` 拍」的标记历史，共 `read_pipe_len+1` 位（见 [mem_gateway.v:23-29, 120](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gateway.v#L23-L29)）。`capture` 取最高位（`[read_pipe_len]`），正是「标记走完整个流水线」的那一拍。

**Q3**：`mem_gateway` 自己会检查写操作是否完成吗？
**A**：不会。源码注释明说「there is no check that they complete」（[mem_gateway.v:35-37](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gateway.v#L35-L37)）。写侧天然单向、localbus 无握手，所以写「发出去就算完成」；任何更深层的写流水化都在本模块视野之外。

---

### 4.3 xformer 与 lb_gateway：协议适配与改名包装

#### 4.3.1 概念说明

`mem_gateway` 是一个「裸」client：吃字节流、吐字节流 + localbus 主端口。要把它真正接进 Packet Badger，还需要两层胶水：

- **上游 [`xformer.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/xformer.v)**：Packet Badger 收到一个包后，由 `xformer` 把包重排、判断类别（ICMP/UDP/…）、按命中的 UDP 端口号（`udp_sel`）把选通信号分发到对应 client，并在回送侧把各 client 的 `odata` 与重算/抑制的校验和拼回成完整的发送包。
- **下游 [`lb_gateway.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/lb_gateway.v)**：`mem_gateway` 的端口名（`control_strobe`/`control_rd`/`data_out`…）是「通用 client」风格；当它要对接 newad.py 生成的解码器、picorv32 存储总线、Xilinx DRP 或未来的 AXI-Lite/Wishbone 时，换成 `lb_addr`/`lb_write`/`lb_read`/`lb_wdata`/`lb_rdata` 这类「总线味」更浓的名字会更顺手——`lb_gateway` 就是这个改名包装。

#### 4.3.2 核心流程

**xformer 的分发逻辑**（[xformer.v:87-92](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/xformer.v#L87-L92)）：

```
mask = 1 << udp_sel            # 哪个 client 命中
raw_l = mask[7:1] & {7{eth_strobe_long}}   # 包级有效，只发给命中 client
raw_s = mask[7:1] & {7{pdata_down}}        # 数据有效选通，只发给命中 client
```

`xformer` 最多驱动 7 个 client（`raw_l`/`raw_s` 各 7 位），同一时刻只有 `udp_sel` 选中的那一个会收到有效选通——这正是 Packet Badger「最多 8 个 UDP 端口插件」机制的落点（见 [`badger/README.md:124-127`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/README.md#L124-L127)）。回送侧，`xformer` 用一个多路选择器在「ICMP 通路 / 原样拷贝 / 命中 client 的 `odata`」之间选（[xformer.v:106-113](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/xformer.v#L106-L113)），并对非零 `udp_sel` 的 UDP 包把校验和强制清零（[xformer.v:64-70](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/xformer.v#L64-L70)）——因为 FPGA 来不及重算 UDP 校验和，干脆置零（UDP 校验和允许为 0）。

`xformer` 自己也有个 `n_lat`（[xformer.v:28](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/xformer.v#L28)），并同样用 `reg_delay` 做流水线对齐（[xformer.v:96-100](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/xformer.v#L96-L100)）——整条 Packet Badger 数据通路的「固定延迟」哲学是一以贯之的。

**lb_gateway 的纯组合翻译**（[lb_gateway.v:53-56](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/lb_gateway.v#L53-L56)）：

```verilog
assign lb_clk    = clk;
assign lb_write  = control_strobe & ~control_rd;
assign lb_read   = |control_pipe_rd;                       // 读窗口内拉高
assign lb_pre_rvalid = control_pipe_rd[read_pipe_len-1];   // 预告数据将到
```

它把 `mem_gateway` 的 `control_pipe_rd` 这条标记流水线翻译成更直观的 localbus 信号：`lb_read` 在整个读窗口内有效，`lb_pre_rvalid` 在数据真正到位的前一拍拉起（给远端 DPRAM/寄存器阵列做「预填充」准备，与 u4-l3 的 jit_rad 主题呼应）。

#### 4.3.3 源码精读

`lb_gateway` 几乎只是「改名 + 一层组合逻辑」，内部直接例化 `mem_gateway`（[lb_gateway.v:33-51](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/lb_gateway.v#L33-L51)），把 `addr→lb_addr`、`data_out→lb_wdata`、`data_in←lb_rdata` 等一一对应。它的存在印证了 Bedrock 的接口规范（u1-l4）：**同一接口的信号共用前缀**——`mem_gateway` 用 `control_*` 通用前缀，`lb_gateway` 改成 `lb_*` localbus 前缀，让两类使用场景各自的代码都好读。

#### 4.3.4 代码实践

**目标**：理解 `lb_gateway` 如何把读窗口信号翻译出来。

**步骤**：

1. 读 [`lb_gateway.v:53-57`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/lb_gateway.v#L53-L57)。
2. 设 `read_pipe_len=3`。画出一次读操作中 `control_pipe_rd[3:0]` 各位的波形（最低位先起，逐拍左移），并据此画出 `lb_read`（= 或运算）与 `lb_pre_rvalid`（=`[read_pipe_len-1]`=`[2]`）的波形。

**需要观察的现象**：`lb_read` 会在连续 4 拍（`read_pipe_len+1`）内为高；`lb_pre_rvalid` 恰好在 `data_in` 被采样（`capture`）的前一拍拉高。

**预期结果**：你能在波形上指出「数据真正有效的那一拍」与 `lb_pre_rvalid` 的时序关系。（若要实际跑，可仿照 `badger/tests/` 里 `mem_gateway_wrap.v` 的方式包一层 testbench；运行结果待本地验证。）

#### 4.3.5 小练习与答案

**Q1**：`xformer` 为什么对 `udp_sel != 0` 的 UDP 包把校验和字段替换为 0？
**A**：FPGA 在固定延迟内来不及重算 UDP 校验和（校验和对数据敏感、需要全包累加，因果上来不及）；而 UDP 允许校验和为 0 表示「不校验」，于是用置零规避（见 [xformer.v:58-70](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/xformer.v#L58-L70)）。`udp_sel==0` 时只是换了 IP/端口顺序，校验和与顺序无关，可原样透传。

**Q2**：既然 `mem_gateway` 已经能输出 `control_*` 信号，为什么还要 `lb_gateway` 这一层？
**A**：纯粹为了对接不同下游时的命名便利——newad.py 生成的解码器、picorv32 总线桥、Xilinx DRP 桥都习惯 `lb_*`/类 AXI-Lite 命名（见 [lb_gateway.v:1-6](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/lb_gateway.v#L1-L6)）。它不增加时序级数（仅组合 `assign`），是把「通用 client 接口」适配成「总线主接口」的零成本糖衣。

## 5. 综合实践

把本讲三条主线串起来：**LASS 协议 → mem_gateway 固定延迟读 → 真实工程的从端契约**。

任务：以 [`projects/ctrace/wctrace_top.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/wctrace_top.v) 为对象，完成一份「端到端读路径分析」。

1. **协议层**：写出「主机发一个含两笔单拍读（地址 `0x1001`、`0x1002`）的 UDP 包」的字节布局（事务 ID 自选，数据段填 0 占位）。
2. **主桥层**：在 [`mem_gateway.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gateway.v) 中标注：字节流如何被 `isr/c4/body` 拆成 `big_r`，`do_op` 如何变成 `control_strobe`，读选通如何经 `read_pipe_markers` 在 `read_pipe_len` 拍后触发 `capture` 并覆写 `osr`。
3. **从端层**：在 [`wctrace_top.v:72-89`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/ctrace/wctrace_top.v#L72-L89) 的手写解码器里，确认 `0x1001→running`、`0x1002→pc_mon` 的读数据是被**寄存**到 `lb_din` 上的，且没有任何应答信号回送。
4. **延迟核算**：默认 `read_pipe_len=3`，写出从 `control_strobe` 拉高到 `mem_gateway` 采样 `data_in` 的固定拍数；再说明 `wctrace_top.v` 的从端用 1 拍完成译码+寄存，是否在 3 拍预算内（答案是：是，富余 2 拍）。
5. **概念对齐**：用一句话把本讲规格里的 `LB_READ_DELAY` 与源码符号对应起来——它就是由参数 `read_pipe_len` 实现的那个综合期常量。

完成后，你应当能向别人讲清楚：「为什么一个没有握手的 localbus，能被一个 UDP 包可靠地读出寄存器」。

## 6. 本讲小结

- **LASS 协议**把「读/写 + 24 位地址 + 32 位数据」串行化进 UDP 包，回包与请求包**结构、长度完全相同**，靠 64 位 ID 配对（[`mem_gate.md`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gate.md)）。
- `mem_gateway` 是 Packet Badger 的 localbus 主桥 client：下行把字节流拆成 localbus 周期，上行把读数据塞回回包占位（[`mem_gateway.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gateway.v)）。
- **核心机制是固定延迟读**：读选通发出后，恰好经 `read_pipe_len` 拍（默认 3）采样 `data_in`，**无需任何握手**——这就是 u4-l1「localbus 读侧难」的破解之道。
- 总 client 延迟预算 `n_lat ≥ 5 + read_pipe_len`，`finale` 用 `reg_delay` 把内部延迟补足到统一值，让所有 client 输出对齐。
- `xformer` 在上游按 `udp_sel` 把选通分发到最多 7 个 client 并重组回包；`lb_gateway` 在下游把 `control_*` 改名翻译成 `lb_*` 总线信号，纯组合、零额外延迟。
- 大纲里的 `LB_READ_DELAY` 是概念名，源码里由 `read_pipe_len`（受 `n_lat` 钳制）实现；本讲规格中提到的 `LB_READ_DELAY` 即指此综合期常量。

## 7. 下一步学习建议

- **u4-l3（jit_rad）**：本讲解决的是「localbus 主端如何用固定延迟读」；下一讲解决对偶问题——当被读数据在**另一个时钟域**、且没有预警时间时，如何在 UDP 包到来前的几百纳秒里把数据预快照进 DPRAM。两讲合起来，就是 Bedrock 跨域读回的完整图景。
- **u4-l4（Packet Badger）**：本讲把 `mem_gateway` 当作一个 client 黑盒；想了解 `xformer` 之外的 `scanner`/`construct`/`rtefi_center` 如何剥以太网/IP/UDP、如何做 ARP/ICMP，请进入 Packet Badger 整体架构讲。
- **延伸阅读**：[`badger/README.md`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/README.md) 的「Example live test run」给了一条从 `nc` 发 UDP 包、经 `mem_gateway` 读 `fake_config_romx` 的完整命令，可作为本讲的实跑印证；`badger/doc/mem_gateway.svg` 是对应的时序图。
