# ChitChat 串行协议

## 1. 本讲目标

本讲承接 [u5-l1（serial_io：8b/10b、GMII 与链路层）](u5-l1-serial-io-basics.md)，把链路层往「上」推一层：在已经能把字节变成线路比特的 8b/10b + GTX 通道之上，Bedrock 定义了一个轻量的、自定义的、点对点串行协议——**ChitChat**。

学完本讲，你应当能够：

- 说清 ChitChat 协议要解决什么问题（在两片 FPGA 之间搬运「不要求严格定时的旁带数据」，如腔体失谐量、联锁状态），以及它为何要自己造一个协议而不是直接用以太网。
- 画出 ChitChat 的 11 字×16 位的定长帧格式，并能解释每一字放什么、K28.5 comma 如何充当帧分隔符。
- 读懂 `chitchat_tx`（成帧 + 定时节拍 + CRC16）和 `chitchat_rx`（comma 对齐 + 解帧 + CRC 校验 + 链路 up 判定 + 错误检测）两条数据通路。
- 理解 `chitchat_txrx_wrap` 如何用一个收发器同时承载 TX 与 RX，并用 `data_xdomain`（来自 [u4-l1 的 CDC 基础](u4-l1-cdc-basics.md)）把 5 个时钟域缝在一起。
- 动手跑通 `make -C serial_io/chitchat all checks`，并能在仿真里观察到链路 up、丢帧、CRC 报错等现象。

## 2. 前置知识

在进入正题前，先用三段话对齐几个概念（详细版本见前置讲义）。

**点对点串行链路与 comma。** FPGA 之间常用光纤或高速串行收发器（MGT/GTX）相连。GTX 对外是串行比特流，对内暴露一个「并行接口」：每拍 16 位数据 + 2 位控制标志。数据在上线前要经过 8b/10b 编码，其中一类特殊的「控制字符」叫 **comma**（ChitChat 用的是 **K28.5**），它的比特模式足够独特，接收端可以靠它对齐字节边界、标定帧头。这部分是 [u5-l1](u5-l1-serial-io-basics.md) 的核心内容。

**时钟域跨越（CDC）。** 当一组信号从一个时钟域搬到另一个异步时钟域时，不能简单地逐位打两拍（多位数据会撕裂）。Bedrock 的做法是：源域用 `gate_in` 锁存数据并保持稳定，用 `flag_xdomain` 把这个 gate 资格认证到目的域，数据位只做一级同步——亚稳态防护集中在 gate 上。这正是 `data_xdomain` 模块的工作方式，在 [u4-l1](u4-l1-cdc-basics.md) 已详细讲过。本讲会大量复用它。

**CRC16。** 循环冗余校验是一种用多项式除法给一串数据算「指纹」的算法；发送方把指纹附在数据后面，接收方用同样的多项式重算并比对，能在很高概率上发现传输错误。ChitChat 用的是生成多项式 `0x1021`（CCITT），16 位。

一句话定位 ChitChat：它是一个**跑在 GTX 并行接口之上、用 K28.5 定帧、用 CRC16 校验、定长 11 字、自带帧号与往返时延测量**的轻量协议，本质是给「两点之间搬运几十位旁带数据」做一个比以太网省得多的封装。

## 3. 本讲源码地图

本讲全部源码集中在 `serial_io/chitchat/` 子目录，CDC 原语复用自 `dsp/`：

| 文件 | 作用 |
| --- | --- |
| `serial_io/chitchat/chitchat_pack.vh` | 协议级共享常量（协议号、版本、comma、链路 up 阈值） |
| `serial_io/chitchat/chitchat_tx.v` | 发送端：把用户数据装成 11 字帧、挂 CRC、按节拍送出 |
| `serial_io/chitchat/chitchat_rx.v` | 接收端：靠 comma 对齐、解帧、CRC 校验、判定链路 up、报告错误 |
| `serial_io/chitchat/chitchat_txrx_wrap.v` | 收发包装：实例化 TX+RX，并完成全部时钟域跨越 |
| `serial_io/chitchat/chitchat_txrx_wrap.md` | wrapper 的端口与时钟域说明文档 |
| `serial_io/chitchat/README.md` | 协议规格说明（帧格式、链路 up、错误检测、吞吐时延） |
| `serial_io/chitchat/chitchat_tb.v` | TX+RX 单时钟域 testbench，会注入随机误码 |
| `serial_io/chitchat/chitchat_txrx_wrap_tb.v` | 多时钟域 testbench，校验 CDC 正确性 |
| `serial_io/crc16.v` | CRC16 计算核（多项式 0x1021），TX/RX 各实例化一个 |
| `dsp/data_xdomain.v` | 多位数据跨域原语（来自 [u4-l1](u4-l1-cdc-basics.md)），wrapper 内反复使用 |

> 提示：本目录 Makefile 用 `vpath %.v $(SERIAL_IO_DIR) $(DSP_DIR)` 同时在 `serial_io` 和 `dsp` 里找 `.v`，所以 `crc16.v`、`data_xdomain.v`、`shortfifo.v` 等即便不在本目录也能被编译找到。

## 4. 核心概念与源码讲解

### 4.1 协议总览与共享常量（chitchat_pack.vh）

#### 4.1.1 概念说明

ChitChat 的设计目标是「**简单、点对点、搬运非定时关键数据**」（见 README 首段）。这意味着：

- 它**不追求线速吞吐**，而追求简单可靠——所以采用定长帧，而不是变长包。
- 它自带一些**系统集成的旁带信息**：帧号、协议标识、往返时延——这些是以太网裸帧里没有、但对调试两台 FPGA 间链路很有用的东西。
- 它**依赖 8b/10b 线路编码**提供的直流平衡与 comma 对齐能力，自己不再做位级同步。

把所有「会被 TX 和 RX 同时引用、且编译期固定」的常量抽到一个头文件 `chitchat_pack.vh`，用 `include` 进两个模块，是避免两边数值漂移的标准做法。

#### 4.1.2 核心流程

协议的几个关键数字都集中在这个头里，关系如下：

- `CC_PROTOCOL_CAT` / `CC_PROTOCOL_VER`：协议「类别」与「版本」。RX 会校验它们，版本不符就报错。这相当于给线路上的帧贴一个「我是 ChitChat、第几版」的标签。
- `CC_K28_5`：K28.5 comma 的 8 位编码（`8'b1011_1100` = `0xBC`），TX 把它放进每帧第一个字的低字节、并用 `gtx_k` 标记，RX 靠它对齐帧头。
- `LINK_UP_CNT`：连续无误解码多少帧后才认定「链路 up」。

成帧与吞吐的关系是一个贯穿全讲的约束。一帧 = 11 个 16 位字 = 176 位，其中只有 64 位是用户载荷（两个 32 位字 `data0/data1`），其余是头部 + CRC。因此有效载荷吞吐为

\[
\text{payload throughput} = \frac{64\ \text{bit}}{11\ \text{cycle}} \times f_{\text{gtx\_clk}}
\]

按 README 的标称（16 位并行 + 4 位 8b/10b 开销，125 MHz，即 2.5 GBd 线路率），有效载荷约为 \(64/11 \times 125\,\text{MHz} \approx 727\,\text{Mbit/s}\)，载荷效率约 \(64/176 \approx 36\%\)。这就是「简单」的代价。

#### 4.1.3 源码精读

整个头文件只有四个常量：

```verilog
localparam [3:0] CC_PROTOCOL_CAT = 4'h6;
localparam [3:0] CC_PROTOCOL_VER = 4'h1;
localparam [7:0] CC_K28_5 = 8'b10111100;
localparam LINK_UP_CNT = 6;  // Number of consecutive frames until link is deemed up
```

这四行分别定义协议类别（`4'h6`）、协议版本（`4'h1`）、K28.5 字节值、以及链路 up 所需的连续无误帧数（见 [serial_io/chitchat/chitchat_pack.vh:6-9](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_pack.vh#L6-L9)）。

帧格式则写在 README 的表里，TX 成帧代码 1:1 对应（见 [serial_io/chitchat/README.md:18-34](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/README.md#L18-L34)）：

| 字号 | 内容（16 位） |
| --- | --- |
| 0 | `PROTOCOL_CAT[3:0]`, `PROTOCOL_VER[3:0]`, `COMMA[7:0]`（低字节是 K28.5） |
| 1 | `GATEWARE_TYPE[2:0]`, `TX_LOCATION[2:0]`, `RESERVED[9:0]` |
| 2–3 | `REVISION_ID[31:0]`（大端：高 16 位在字 2） |
| 4–5 | `TX_DATA0[31:0]` |
| 6–7 | `TX_DATA1[31:0]` |
| 8 | `TX_FRAME_COUNT[15:0]` |
| 9 | `TX_LOOPBACK_FRAME_COUNT[15:0]`（用于往返时延测量） |
| 10 | `CRC_CHECKSUM[15:0]` |

> 一个**文档与代码不一致**、需要以源码为准的细节：README 第 38–40 行写「连续 4 帧无误」才 up，但 `chitchat_pack.vh` 里 `LINK_UP_CNT = 6`，而 `chitchat_rx` 的判定逻辑用的是这个常量（见 4.3.3）。本讲一律以源码的 **6** 为准（见 [serial_io/chitchat/README.md:36-40](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/README.md#L36-L40) 与 [serial_io/chitchat/chitchat_pack.vh:9](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_pack.vh#L9)）。

#### 4.1.4 代码实践

1. **目标**：确认协议常量与帧格式，并算出有效吞吐。
2. **步骤**：
   - 打开 `serial_io/chitchat/chitchat_pack.vh` 与 `README.md`。
   - 把 README 帧表里 11 个字按「头部 / 载荷 / CRC」三类归并，数一下载荷占几个字。
   - 用上面公式代入 \(f_{\text{gtx\_clk}} = 125\,\text{MHz}\) 算有效载荷速率。
3. **观察**：载荷字 = 字 4、5、6、7 共 4 个 = 64 位；其余 7 个字是开销。
4. **预期结果**：约 727 Mbit/s；README「Throughput and latency」一节也明说「64-bit / 11 clock-cycles」（见 [serial_io/chitchat/README.md:58-64](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/README.md#L58-L64)）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 ChitChat 把 K28.5 固定放在「低字节」而不是高字节？
**答**：因为 GTX 的控制字符标志 `gtx_k` 是按字节给出的，协议只用 `gtx_k[0]` 标记低字节 comma（`gtx_k[1]` 恒 0）。固定在低字节，TX/RX 都只需处理最低位，逻辑最简，也避开了双 comma 的歧义。

**练习 2**：若把 `LINK_UP_CNT` 改大（比如 20），对系统行为有何影响？
**答**：链路上电后需要更多连续无误帧才会宣告 up、才会输出 `rx_valid`，即「容忍抖动」更强但「启动收敛」更慢；运行中一旦出错 `link_up_cnt` 清零，又要重新累计。

---

### 4.2 chitchat_tx：发送端成帧

#### 4.2.1 概念说明

`chitchat_tx` 的任务很纯粹：**周期性地、以固定速率**把当前输入端的 `tx_data0/tx_data1` 等打包成一帧，挂上 CRC，逐字送到 GTX 接口。

注意一个反直觉点（代码顶部注释强调）：这个模块**没有 valid/strobe 输入**——它在 `tx_transmit_en` 拉高期间，按固定节拍**采样**当前输入并发送。也就是说，「要发哪一拍的数据」这件事由调用方（wrapper）用 `tx_valid` 在外部锁存好再喂进来，`chitchat_tx` 自己只负责「不停地成帧」。这简化了模块，把「采样策略」上推给了 wrapper（见 4.4）。

#### 4.2.2 核心流程

TX 内部是一条「节拍器 → 装帧 → CRC → 输出」的小流水：

```
word_count: 0→1→2→...→10→0   (自由运行的节拍计数器)
   |
   +-- start   : 在 word_count==1 拍产生一个脉冲  ──┐
   +-- sync    : 标记「该发 comma 字（字0）」        │ 装入新帧、帧号+1
   +-- last    : 在 word_count==10 标记末字          │
   +-- crc_time: last 之后一拍，把算好的 CRC 切到输出 ┘
```

伪代码：

1. `word_count` 在 `tx_transmit_en` 有效时从 0 计到 10 循环；每个值对应输出帧的一个字。
2. `start` 脉冲到来时，把 `{协议头, rev_id, data0, data1, 帧号, 回环帧号, CRC占位}` 一次拼进 176 位的 `frame` 移位寄存器，同时 `frame_counter` 自增。
3. 之后每拍把 `frame` 顶部 16 位（`inner_data`）送出；`frame` 整体下移 16 位，露出下一个字。
4. 与此同时，`crc16_tx` 对每拍送出的 `inner_data` 持续累算 CRC；在 `crc_time` 那一拍，把算出的 `crc_tx` 切换到输出，替代末字的占位。
5. comma：在字 0 那拍，`gtx_k[0]` 置 1（其余拍为 0），告诉 GTX「这拍低字节是 K28.5 控制字符」。

#### 4.2.3 源码精读

**节拍器**——用一个 `always` 块同时派生出 `start/sync/last/crc_time` 五个时间点（见 [serial_io/chitchat/chitchat_tx.v:43-55](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_tx.v#L43-L55)）：

```verilog
reg [3:0] word_count=0;
wire increment = tx_transmit_en | (word_count!=0);
always @(posedge clk) begin
   word_count <= word_count == 10 ? 0 : word_count + increment;
   start      <= word_count == 1;
   sync       <= start;
   sync_r     <= sync;
   last       <= word_count == 10;
   last_r     <= last;
   crc_time   <= last_r;
end
```

要点：`increment = tx_transmit_en | (word_count!=0)` 保证一旦开始发一帧（`word_count!=0`）就会把它发完，即便中途 `tx_transmit_en` 掉了；只有发完回到 0 且 `tx_transmit_en` 为低时才停拍。

**装帧**——`start` 为真时整体装入新帧，否则逐拍下移（见 [serial_io/chitchat/chitchat_tx.v:70-83](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_tx.v#L70-L83)）。拼接顺序与 4.1 的帧表逐字对应，MSB 在前：

```verilog
frame <= start ? { protocol_cat, protocol_ver_fix, comma_pad,   // 字0
                   gateware_type_fix, tx_location, reserved,    // 字1
                   rev_id_fix,                                    // 字2-3
                   tx_data0,                                      // 字4-5
                   tx_data1,                                      // 字6-7
                   frame_counter,                                 // 字8
                   tx_loopback_frame_counter,                     // 字9
                   crc_pad }                                       // 字10(占位)
                 : { frame[10*16-1:0] , 16'b0 };  // 否则下移16位
```

`crc_pad` 先占着字 10 的位置，真正 CRC 在下面算好后切换上去（注释提醒：32 位输入按**大端**排布，高 16 位在先）。

**CRC 与输出切换**——`crc16_tx` 持续吃 `inner_data`（每拍露在顶部的字），在 `sync` 时清零重启；`outer_data` 在 `crc_time` 拍取 CRC、否则取 `inner_data`（见 [serial_io/chitchat/chitchat_tx.v:85-105](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_tx.v#L85-L105)）：

```verilog
crc16 crc16_tx(.clk(clk), .din(inner_data), .zero(sync), .crc(crc_tx));
always @(posedge clk) outer_data <= crc_time ? crc_tx : inner_data;
assign gtx_d = outer_data;
assign gtx_k = {1'b0, sync_r};   // 仅低字节可标 comma，高字节恒 0
assign local_frame_counter = frame_counter;
```

`gtx_k = {1'b0, sync_r}` 正是「comma 只在低字节」的硬件体现。`crc16` 核用的是 `0x1021` 多项式（见 [serial_io/crc16.v:14-16](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/crc16.v#L14-L16) 的注释）。

#### 4.2.4 代码实践

1. **目标**：在仿真里看到 TX 持续成帧、comma 周期出现。
2. **步骤**：在仓库根运行 `make -C serial_io/chitchat chitchat_check`（即编译并仿真 `chitchat_tb`）。需要波形则用 `make -C serial_io/chitchat chitchat.vcd` 后用 gtkwave 打开 `chitchat.gtkw`。
3. **观察**：`word_count` 在 0..10 循环；每轮 `start` 脉冲一次，`frame_counter` 自增；`gtx_k[0]` 周期性拉高（每帧一次）；`gtx_d` 在末字位置出现 CRC 值。
4. **预期结果**：testbench 跑完打印 `PASS`（见 [serial_io/chitchat/chitchat_tb.v:44-51](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_tb.v#L44-L51)，要求收到 ≥300 次更新）。
5. 若本机未装 iverilog，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`tx_transmit_en` 在一帧发到一半时被拉低，这一帧会被发完吗？
**答**：会。因为 `increment = tx_transmit_en | (word_count!=0)`，只要 `word_count!=0` 就继续计数直到回到 0，保证帧不会被截断。

**练习 2**：为什么 `outer_data` 要在 `crc_time`（即 `last_r`）那一拍才切到 CRC，而不是在 `last`？
**答**：CRC 是对前 10 个字（字0..字9）累算的结果，必须等字 9 真正进入 `crc16` 后才算完。`last` 标在字 10 的位置，`last_r` 再延一拍正好让最后一个有效字算完，时序对齐。

---

### 4.3 chitchat_rx：接收端对齐与校验

#### 4.3.1 概念说明

`chitchat_rx` 是协议里更复杂的一端，因为它要从「连续不断、可能夹杂噪声」的 GTX 数据流里，**自己找回帧边界**。它的核心策略是：**靠 comma 对齐**。每收到一个 K28.5（`gtx_k[0]=1`），就把内部的 `word_count` 复位到 0，从此处开始按字号 0→9 解帧。

它还要做四件事：CRC 校验、协议/版本核对、帧号连续性核对、以及「链路 up 判定」。任何一项不过都会丢帧并报告错误类型。

#### 4.3.2 核心流程

```
gtx_k[0]==1 (comma) ──> word_count := 0   (帧头对齐)
                         |
        每拍 word_count++，按字号把 gtx_d 写进对应字段：
   word_count==0 : 抓字0(协议/版本/comma) + 字1(gateware/location)
   word_count==2 : 字2-3 = rev_id
   word_count==4 : 字4-5 = data0
   word_count==6 : 字6-7 = data1
   word_count==7 : 字8   = frame_counter（并核对是否 = 上一帧+1）
   word_count==8 : 字9   = loopback_frame_counter
   word_count==9 : last  : 字10=CRC，整帧校验
                         |
   (last | timeout) 拍：
       有任意 fault  ──> link_up_cnt:=0，丢帧，锁存 fault
       无 fault      ──> link_up_cnt++（封顶 LINK_UP_CNT）
                         若已 link_up：rx_valid 脉冲，输出本帧数据
   timeout(word_count 到 15 仍无 comma) ──> 判定失步
```

注意 RX 的字号是「每两个 16 位字并成一个 32 位字段」来抓的（用 `gtx_dd`，即上一拍的 `gtx_d`，拼上本拍的 `gtx_d`），所以 32 位字段只需在偶数 `word_count` 检查一次。

#### 4.3.3 源码精读

**comma 对齐**——comma 一到就把计数器归零（见 [serial_io/chitchat/chitchat_rx.v:54-63](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_rx.v#L54-L63)）：

```verilog
wire gtx_k_lo = gtx_k[0];
reg [3:0] word_count=0;
wire timeout   = &word_count;       // word_count==15
wire increment = ~timeout;
always @(posedge clk)
   word_count <= gtx_k_lo ? 0 : (word_count + increment);
```

`timeout = &word_count`（全 1，即 15）表示「连续 15 拍没见到 comma」，判失步。

**链路 up 判定与错误检测**——四类 fault 拼成一位向量，任一非零就清零计数并丢帧；否则计数累加，到 `LINK_UP_CNT` 才 up（见 [serial_io/chitchat/chitchat_rx.v:76-110](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_rx.v#L76-L110)）：

```verilog
wire    link_up     = link_up_cnt == LINK_UP_CNT;
wire    link_up_inc = (link_up_cnt < LINK_UP_CNT) ? 1 : 0;
wire    crc_fault = last & ~crc_zero;          // 末字 CRC 校验非零即错
wire [3:0]  faults = {wrong_frame, crc_fault, wrong_prot, timeout};
always @(posedge clk) begin
   if (last | timeout) begin
      if (|faults) begin
         link_up_cnt <= 0;  frame_drop_r <= last;  fault_r <= faults;
         fault_cnt_r <= fault_cnt_r + 1;
      end else begin
         link_up_cnt <= link_up_cnt + link_up_inc;
         rx_valid_r  <= link_up;                  // 只在 link up 后才出 valid
         frame_drop_r<= ~link_up;                 // up 之前的好帧也「丢」
      end
   end
end
```

四类错误对应 `ccrx_fault[3:0]` 的四位（见端口注释 [serial_io/chitchat/chitchat_rx.v:25-32](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_rx.v#L25-L32)）：`[0]` 超时/失步、`[1]` 协议或版本不符、`[2]` CRC 错、`[3]` 帧号不连续。

**解帧**——按 `word_count` 把 `gtx_d`（及延迟一拍的 `gtx_dd`）拆进各字段（见 [serial_io/chitchat/chitchat_rx.v:128-163](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_rx.v#L128-L163)）：

```verilog
wire mismatch_protocol = gtx_dd[15:15-3] != CC_PROTOCOL_CAT;
wire mismatch_gatetype = gtx_d[15:15-2] != RX_GATEWARE_TYPE;
always @(posedge clk) begin
   ...
   if (word_count==0) begin
      {protocol_cat, rx_protocol_ver_r, comma_pad}  <= gtx_dd;  // 字0
      {rx_gateware_type_r, rx_location_r, reserved} <= gtx_d;   // 字1
      wrong_prot <= mismatch_protocol | mismatch_gatetype;
   end
   if (word_count==2) rx_rev_id_r <= {gtx_dd, gtx_d};   // 字2-3
   if (word_count==4) rx_data0_r  <= {gtx_dd, gtx_d};   // 字4-5
   if (word_count==6) rx_data1_r  <= {gtx_dd, gtx_d};   // 字6-7
   if (word_count==7) begin
      rx_frame_counter_r <= gtx_d;                       // 字8
      if (~|fault_r)                                       // 上一帧无误才检查
         wrong_frame <= (gtx_d != next_frame_counter);     // 帧号连续性
   end
   if (word_count==8) rx_loopback_frame_counter_r <= gtx_d;  // 字9
   if (word_count==9) last <= 1;                              // 字10 CRC
end
```

CRC 侧则对收到的每一拍 `gtx_d` 持续累算，到末字时若余数为 0 即通过（见 [serial_io/chitchat/chitchat_rx.v:65-74](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_rx.v#L65-L74)）。

> 上面的 `if (~|fault_r)` 仅作示意说明（实代码在第 155 行）；含义是「上一帧没出错时才检查帧号连续性」，避免在出错恢复期连锁误报。

#### 4.3.4 代码实践

1. **目标**：观察 RX 在噪声下的失步、丢帧与恢复。
2. **步骤**：运行带噪声的仿真——在 testbench 支持的 `+noise` plusarg 下启动：先 `make -C serial_io/chitchat chitchat_tb` 生成可执行，再用 `vvp chitchat_tb +noise +seed=7`（或按本机 iverilog 用法传 plusarg）运行。
3. **观察**：仿真启动阶段（前 10000 ns）注入白噪声（见 [serial_io/chitchat/chitchat_tb.v:145-149](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_tb.v#L145-L149)），`ccrx_los` 应为高、`rx_valid` 不出；噪声撤去后，连续无误帧累计到阈值，`ccrx_los` 变低、`rx_valid` 开始脉冲。testbench 还会随机翻转发送比特（`corrupt`）来触发 CRC 错与 `ccrx_frame_drop`。
4. **预期结果**：仿真结束仍打印 `PASS`（收到 ≥300 次更新），过程中可在波形里看到 `ccrx_fault_cnt` 随机增长。
5. 具体 plusarg 传法依 iverilog 版本而定，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 RX 用 `gtx_dd`（延迟一拍的 `gtx_d`）配合 `gtx_d` 来拼 32 位字段，而不是直接用连续两拍的值？
**答**：因为下一个 comma（下一帧的 K28.5）会在当前帧还没完全解完时就到来（见代码注释 [serial_io/chitchat/chitchat_rx.v:141-142](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_rx.v#L141-L142)）。把当前字延迟一拍成 `gtx_dd`，再在同一个 `word_count` 拍同时拿到「上一字」和「当前字」，可以提前一拍完成字 0 的解码，腾出节拍余量。

**练习 2**：链路已经 up 之后，单帧 CRC 错误会立刻让 `ccrx_los` 拉高吗？
**答**：不会立刻。单帧错误只会 `link_up_cnt <= 0` 并丢这一帧（`ccrx_frame_drop` + `ccrx_fault`）；`ccrx_los`（= `~link_up`）要等计数被清零后才会变高。也就是说单帧错是「丢帧」，连续错到无法重新累计 up 才「失步」。

---

### 4.4 chitchat_txrx_wrap：多时钟域包装与 CDC

#### 4.4.1 概念说明

一片 FPGA 上的一个 GTX 收发器是**全双工**的：它有一组并行发送引脚（`gtx_tx_*`）和一组并行接收引脚（`gtx_rx_*`），分别由 `gtx_tx_clk`、`gtx_rx_clk` 驱动。`chitchat_txrx_wrap` 就是把一个 `chitchat_tx` 和一个 `chitchat_rx` 装进同一个壳里，共享一个 GTX，并对**所有用户接口做时钟域跨越**。

为什么需要跨越？因为「用户数据所在的时钟域」「寄存器状态所在的时钟域」「GTX 收发时钟域」几乎从来不一样：

- 用户给 TX 喂数据，往往在系统/逻辑时钟 `tx_clk` 上；
- 用户从 RX 取数据，在 `rx_clk` 上；
- 状态/统计给 localbus 读，在 `lb_clk` 上；
- 而 TX/RX 引擎本身跑在 GTX 的 `gtx_tx_clk` / `gtx_rx_clk` 上。

wrapper 的价值就是把这 5 个域的搬运全部包好，并提供 3 个编译期开关，让你在「这些时钟其实是同一个」时关掉不必要的同步器（省资源、减延迟）。

#### 4.4.2 核心流程

wrapper 内部三条 CDC 通道，全部用 `data_xdomain`（[u4-l1](u4-l1-cdc-basics.md) 讲过的「gate 锁存 + flag 跨域 + 数据一级同步」）：

```
              tx_clk 域                  gtx_tx_clk 域
 tx_data0/1 ──┐                     ┌──> chitchat_tx ──> gtx_tx_d/k
 tx_valid0/1 ─┤── data_xdomain ─────>│   (TX 引擎)
              │   (TX CDC, 可关)     │
              │                      │   <── 往返时延计算
              │   data_xdomain       │
              │   (gtx_rx_clk→       │
              │    gtx_tx_clk)       │
              │                      │
              │                  gtx_rx_clk 域
              │                     └── gtx_rx_d/k ──> chitchat_rx ──┐
              │                                          (RX 引擎)  │
              │                                                     │
              │   data_xdomain (RX CDC, 可关)                        │
              └─────────────────────────────────────────────────────>│ rx_clk 域
                                                                   rx_data0/1, rx_valid
              │   data_xdomain (LB CDC, 可关)
              └─────────────────────────────────────────────────────>│ lb_clk 域
                                                              帧号/故障等状态
```

三类信号的跨域策略不同：

- **快变多位数据**（TX 的 data0/1、RX 的 data0/1、帧号、故障码）：走 `data_xdomain`，用 `valid`/`rx_valid` 当 gate。
- **准静态信号**（`tx_location`、`tx_transmit_en`、各类版本号、`ccrx_los` 计数等）：变化极慢，直接在目的域打一拍寄存器即可，无需握手。
- **可关闭的同步**：每个 `data_xdomain` 外面套 `generate if (XXX_CDC)`，参数为 0 时改用直连 `assign`（并把 TX 侧「按 valid 锁存」的逻辑在 `else` 分支里复制一份，保证关掉 CDC 后行为不变）。

#### 4.4.3 源码精读

**5 个时钟域**由端口声明给出（见 [serial_io/chitchat/chitchat_txrx_wrap.v:21-64](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_txrx_wrap.v#L21-L64)）：`tx_clk`、`rx_clk`、`lb_clk`、`gtx_tx_clk`、`gtx_rx_clk`。文档也强调「用户不必都独立驱动」（见 [serial_io/chitchat/chitchat_txrx_wrap.md:3-11](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_txrx_wrap.md#L3-L11)）。

**3 个 CDC 开关**是参数（见 [serial_io/chitchat/chitchat_txrx_wrap.v:10-17](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_txrx_wrap.v#L10-L17)）：

```verilog
parameter TX_TO_GTX_CDC = 1,  // tx_clk  -> gtx_tx_clk
parameter GTX_TO_RX_CDC = 1,  // gtx_rx_clk -> rx_clk
parameter GTX_TO_LB_CDC = 1   // gtx_rx_clk -> lb_clk
```

**TX CDC**——用 `data_xdomain` 既跨域又「按 valid 锁存」（注释明说此同步器一物两用，见 [serial_io/chitchat/chitchat_txrx_wrap.v:106-148](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_txrx_wrap.v#L106-L148)）：

```verilog
generate if (TX_TO_GTX_CDC) begin : G_TX_CDC
   data_xdomain #(.size(32)) i_tx0_sync (
      .clk_in(tx_clk), .gate_in(tx_valid0), .data_in(tx_data0),
      .clk_out(gtx_tx_clk), .gate_out(tx_valid0_x_tgtx), .data_out(tx_data0_x_tgtx));
   data_xdomain #(.size(32)) i_tx1_sync ( /* tx_data1 同理 */ );
   // 把对端帧号/回环帧号从 gtx_rx_clk 搬到 gtx_tx_clk，用于往返时延
   data_xdomain #(.size(16*2)) i_tx_latency_sync (...);
end else begin
   // 关闭 CDC 时：在 tx_clk 域自己按 valid 锁存，再直连
   always @(tx_clk) if (tx_valid0) tx_data0_r <= tx_data0;
   ...
end endgenerate
```

准静态的 `tx_location`/`tx_transmit_en` 则只在 `gtx_tx_clk` 打一拍（见 [serial_io/chitchat/chitchat_txrx_wrap.v:150-154](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_txrx_wrap.v#L150-L154)）。

**往返时延测量**——把本端 TX 的本地帧号，减去「对端回环回来的、其实是自己早先发出去的」帧号，差值就是往返帧数（见 [serial_io/chitchat/chitchat_txrx_wrap.v:172-176](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_txrx_wrap.v#L172-L176)）：

```verilog
always @(posedge gtx_tx_clk)
   if (rx_valid_x_tgtx)
      txrx_latency_x_tgtx <= tx_local_frame_counter_x_tgtx - rx_lback_frame_counter_x_tgtx;
```

这条链路的语义（见 `chitchat_rx` 顶部注释 [serial_io/chitchat/chitchat_rx.v:49-52](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_rx.v#L49-L52)）：把本模块的 `rx_frame_counter` 接到对端 TX 的 `tx_loopback_frame_counter`，于是对端回环字段里放的就是「它从本端收到的帧号」；本端再用「自己当前帧号 − 这个回环帧号」得到往返时延。`chitchat_txrx_wrap_tb` 断言它应收敛到 4（见 [serial_io/chitchat/chitchat_txrx_wrap_tb.v:316-319](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_txrx_wrap_tb.v#L316-L319)）。

**RX CDC**——把 `{rx_data1, rx_data0, ccrx_frame_drop}` 打包成 65 位一起跨到 `rx_clk`，再在目的域拆包（见 [serial_io/chitchat/chitchat_txrx_wrap.v:199-222](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_txrx_wrap.v#L199-L222)）：

```verilog
assign rx_pack_x_rgtx = {rx_data1_x_rgtx, rx_data0_x_rgtx, ccrx_frame_drop_x_rgtx};
data_xdomain #(.size(RX_PACK_WI)) i_rx_sync (
   .clk_in(gtx_rx_clk), .gate_in(rx_valid_x_rgtx | ccrx_frame_drop_x_rgtx),
   .data_in(rx_pack_x_rgtx), .clk_out(rx_clk), .gate_out(rx_valid_l_rx), .data_out(rx_pack));
...
assign rx_valid        = rx_valid_l_rx & ~rx_pack[0];   // 拆出「有效」
assign ccrx_frame_drop = rx_valid_l_rx &  rx_pack[0];   // 拆出「丢帧」
assign {rx_data1, rx_data0} = rx_pack[RX_PACK_WI-1:1];
```

把 `valid` 和 `frame_drop` 两种事件复用同一根 `gate_out`、再用打包位区分，是省一个同步器的巧思。

**LB CDC**——同理把 `{rx_frame_counter, ccrx_fault}`（20 位）打包跨到 `lb_clk`，准静态状态信号直接打拍（见 [serial_io/chitchat/chitchat_txrx_wrap.v:224-264](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_txrx_wrap.v#L224-L264)）。

文档还提醒一个重要的吞吐约束：wrapper **无法全速发送**，因为成帧 + 上链需要若干拍，所以 `tx_valid` 的速率不能高于实际发包速率，否则丢数据（见 [serial_io/chitchat/chitchat_txrx_wrap.md:23-32](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_txrx_wrap.md#L23-L32) 及端口注释 [serial_io/chitchat/chitchat_txrx_wrap.v:25-28](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_txrx_wrap.v#L25-L28)）。

#### 4.4.4 代码实践（本讲主实践）

1. **目标**：跑通多时钟域仿真，并说清 wrapper 的时钟域与 TX 速率变换。
2. **操作步骤**：
   - 在仓库根运行 `make -C serial_io/chitchat all checks`（会同时编译仿真 `chitchat_tb` 与 `chitchat_txrx_wrap_tb` 并各自 `*_check`）。
   - 打开 `serial_io/chitchat/chitchat_txrx_wrap.md` 的 Clocking 一节。
   - 对照 `chitchat_txrx_wrap_tb.v` 第 19–22、65–71 行看测试台实际用了哪些时钟周期（`cc_clk=10.5ns`、`gtx_tx_clk=8ns`、`gtx_rx_clk=8ns`、`lb_clk=20ns`，且 `tx_clk=cc_clk`、`rx_clk=lb_clk`）。
3. **需要观察的现象**：`chitchat_txrx_wrap_check` 打印 `PASS`；testbench 内部专门检查了 TX 侧跨域数据一致性（[serial_io/chitchat/chitchat_txrx_wrap_tb.v:223-243](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_txrx_wrap_tb.v#L223-L243)）。
4. **预期结果 / 参考答案**：
   - **wrapper 涉及的全部时钟域（共 5 个）**：`tx_clk`（用户送 TX 数据）、`rx_clk`（用户取 RX 数据）、`lb_clk`（状态/寄存器接口）、`gtx_tx_clk`（GTX 发送并行通道）、`gtx_rx_clk`（GTX 接收并行通道）。文档明说「用户不必都独立驱动」——仿真里 `tx_clk` 就和 `cc_clk` 同源、`rx_clk` 就和 `lb_clk` 同源，实际只有 4 个独立时钟。
   - **TX 数据从 `tx_clk`（系统域）到 GTX 发送速率之间的「速率变换」是什么**：这里发生的是**两件事，而不是一个重定时（retimer）**。
     1. **时钟域跨越（CDC）**：`tx_data0/1` + `tx_valid` 经 `data_xdomain` 从 `tx_clk` 搬到 `gtx_tx_clk`，靠 `tx_valid` 当 gate 锁存数据、靠 `flag_xdomain` 跨域资格认证（正是 [u4-l1](u4-l1-cdc-basics.md) 的机制）。它不改变数据率，只换时钟域。
     2. **成帧带来的吞吐降速**：`chitchat_tx` 在 `gtx_tx_clk` 上把每两个 32 位用户字装进一帧 11 字，每拍只发 16 位，所以有效载荷吞吐被压成 \(64\text{ bit}/11\text{ cycle}\)。因此**用户送数的速率（`tx_valid` 频率）必须低于实际发包速率，否则丢数据**——这就是文档所说「cannot transmit at full-rate」的本质。
   - 换言之：时钟域之间是「CDC 同步」，速率之间是「64/11 的成帧降速」，两者叠加才是完整的「速率变换」。
5. 若本机缺 iverilog，仿真部分**待本地验证**；时钟域清单与速率分析是纯源码阅读，可直接得出。

#### 4.4.5 小练习与答案

**练习 1**：把 `TX_TO_GTX_CDC` 设成 0（假定 `tx_clk` 与 `gtx_tx_clk` 同源），wrapper 还能正确按 `tx_valid` 锁存数据吗？
**答**：能。`generate` 的 `else` 分支专门复制了「在 `tx_clk` 域按 `tx_valid` 锁存」的逻辑（见 [serial_io/chitchat/chitchat_txrx_wrap.v:139-148](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/chitchat/chitchat_txrx_wrap.v#L139-L148)），所以关掉 CDC 只是去掉同步器、减延迟，锁存语义保留。

**练习 2**：为什么把 `{rx_data1, rx_data0, ccrx_frame_drop}` 打包成一根宽总线一起跨域，而不是分三路各跨各的？
**答**：因为这三者必须**原子地**一起到达——若 `rx_data` 先到、`frame_drop` 后到，用户会拿到「数据已变但 valid 还按旧含义解释」的撕裂态。打包后用同一个 `gate` 跨域，保证三者同拍更新；同时复用一根 `gate_out`、再用打包里的 1 位区分「有效」与「丢帧」，省下一个同步器。

**练习 3**：往返时延 `txrx_latency` 为什么是「帧数」而不是「纳秒」？
**答**：它直接是「本端当前帧号 − 对端回环帧号」的差，单位是帧（一帧 11 个 `gtx` 拍）。要换算成时间，再乘以单帧时长即可。用帧号作单位的好处是与具体时钟频率解耦，且天然整数、无需定点。

## 5. 综合实践

把本讲四块知识串起来，做一个「**读懂一条 ChitChat 帧的完整一生**」的小任务。

**任务**：在 `serial_io/chitchat/` 下完成下列阅读 + 仿真，画出一张「从用户 `tx_valid` 脉冲，到对端用户 `rx_valid` 脉冲」的端到端时序与数据通路图。

1. **运行**：`make -C serial_io/chitchat all checks`，确认两个 testbench 都 `PASS`（无工具则标注待本地验证）。
2. **追踪一帧**（建议结合 `chitchat_txrx_wrap.v` 与 `chitchat_tx.v`）：
   - 用户在 `tx_clk` 拉高 `tx_valid0/1` 并给出 `tx_data0/1` →
   - `data_xdomain` 把数据锁存并跨到 `gtx_tx_clk` →
   - `chitchat_tx` 在下一个 `start` 拍把数据装进帧、`frame_counter++`、逐字输出，末字挂 CRC，字 0 标 comma →
   - 比特经 GTX/8b/10b 上链（本讲范围外，见 [u5-l1](u5-l1-serial-io-basics.md)）→
   - 对端 `chitchat_rx` 靠 comma 对齐，按字号解帧、CRC 校验、帧号连续性核对 →
   - 链路 up 后 `rx_valid` 脉冲，`{rx_data1, rx_data0}` 经 RX CDC 跨到 `rx_clk` 给用户；帧号/故障经 LB CDC 跨到 `lb_clk` 供状态读取。
3. **标注**：在图上标出每一跳所在的**时钟域**、用到的 **CDC 原语**、以及「11 拍/帧」「64 bit/11 cycle」这两个数字出现的位置。
4. **进阶**：把 `chitchat_txrx_wrap_tb.v` 第 316 行的时延断言（应收敛到 4）和你画的图对应起来——解释为什么往返时延是 4 帧（提示：本端发 → 对端收 → 对端在回环字段里把它带回 → 本端收，跨越了若干级流水与跨域）。

完成这张图，你就把「成帧、comma 对齐、CRC、多时钟域 CDC、往返时延」全部串成了一条链。

## 6. 本讲小结

- ChitChat 是跑在 GTX 并行接口之上的**轻量定长帧协议**：11 字×16 位，K28.5 comma 定帧，CRC16（`0x1021`）校验，自带帧号与往返时延测量，用于两片 FPGA 间搬运非定时关键的旁带数据。
- `chitchat_pack.vh` 集中存放协议常量；注意 `LINK_UP_CNT` 源码为 **6**，README 写的 4 已过时，以源码为准。
- `chitchat_tx` 用一个 0→10 的 `word_count` 节拍器驱动「装帧→移位输出→CRC 切换」，无 valid 输入、按固定速率采样；`gtx_k[0]` 只在字 0 标 comma。
- `chitchat_rx` 靠 comma 把 `word_count` 归零对齐帧头，按字号解帧，做 CRC/协议版本/帧号连续性三类校验，累计 `LINK_UP_CNT` 个无误帧才宣告 up 并输出 `rx_valid`。
- `chitchat_txrx_wrap` 把 TX+RX 装进一个全双工 GTX，用 `data_xdomain`（[u4-l1](u4-l1-cdc-basics.md)）缝好 `tx_clk/rx_clk/lb_clk/gtx_tx_clk/gtx_rx_clk` 五个域，并提供 3 个编译期 CDC 开关；准静态信号直接打拍。
- 「速率变换」= CDC 同步 + 64bit/11cycle 的成帧降速；用户 `tx_valid` 频率不得高于发包速率，否则丢数据。

## 7. 下一步学习建议

- **往协议栈更上层 / 工程集成走**：阅读 [u5-l3（TCL 驱动的 MGT 配置流程）](u5-l3-mgt-tcl-flow.md)，看 `comms_top` 如何把 ChitChat 与以太网-over-fiber 两条链路放进同一个 Quad MGT；再到 [u7-l4（工程集成实战）](u7-l4-projects-integration.md) 看完整上板工程如何把 localbus、Packet Badger、外设与板级 shell 组装起来。
- **往 CDC 与验证深挖**：本讲 wrapper 大量使用 `data_xdomain`，可回看 [u4-l1（CDC 基础）](u4-l1-cdc-basics.md) 与 [u6-l1（cdc_snitch 形式化跨域验证）](u6-l1-cdc-snitch-verification.md)，了解 Bedrock 如何用 yosys 静态检查 CDC 正确性。
- **建议继续阅读的源码**：`serial_io/chitchat/chitchat_tb.v`（单域、注入误码的对照实验）、`serial_io/chitchat/chitchat_txrx_wrap_tb.v`（多域 CDC 校验）、`serial_io/crc16.v`（CRC 实现），以及 `dsp/shortfifo.v`（testbench 记分牌用的 FIFO）。
