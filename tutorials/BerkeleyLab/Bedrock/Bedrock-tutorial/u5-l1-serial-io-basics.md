# serial_io：8b/10b、GMII 与链路层

## 1. 本讲目标

本讲进入 Bedrock 的 **serial_io** 子系统——把数字逻辑与外部世界「串起来」的那一层。学完后你应当能够：

- 说清 **8b/10b 编码**为什么存在、它如何用「游动不均等度（running disparity）」换来直流平衡与游程受限，并看懂 `enc_8b10b.v` / `dec_8b10b.v` 这对经典编解码器。
- 读懂 `gmii_link.v` 如何把 8 位并行 GMII 接口与 10 位 8b/10b 串行侧（serdes/MGT）对接，并理解它「端口方向看起来反了」的原因。
- 理解 GMII 与 RGMII 的差异，以及 `gmii_to_rgmii.v` 用 Xilinx 的 `ODDR`/`IDDR` 原语做 DDR 转换的套路。
- 知道 `simpleuart.v` 这种低速异步串口在 Bedrock 里扮演的「调试/控制台」角色，以及它与千兆 GMII 链路的对比。

本讲是单元 5（高速串行链路与 MGT 配置）的第一讲，承接 u4-l4「Packet Badger」中以 UDP/以太网为载体的网络访问，把视角下沉到「一个字节是如何变成线路上的比特流的」。

## 2. 前置知识

在进入源码前，先用通俗语言对齐几个术语。

- **串行链路（serial link）**：数据排成一列、一位一位地在一条线上传送（光纤、千兆铜线、FPGA 的 MGT/GTX 收发器都是串行链路）。相对的是**并行总线**（如 8 位一起走）。
- **CDR（Clock and Data Recovery，时钟数据恢复）**：串行链路通常不单独传时钟，接收端要从数据本身「跳变沿」里把时钟恢复出来。这就要求数据里不能有太长的连续 0 或 1。
- **DC balance（直流平衡）**：长时间统计，链路上 1 和 0 的数量要相等，否则交流（AC）耦合的链路会产生「直流漂移」，把信号电平顶偏。
- **GMII（Gigabit Medium Independent Interface）**：千兆以太网的芯片级并行接口，8 位数据 `TXD/RXD` + 使能/有效信号，在 125 MHz 时钟下单拍传一个字节，正好 \(8 \times 125\,\text{M} = 1\,\text{Gbit/s}\)。
- **RGMII（Reduced GMII）**：把 GMII 的 8 位数据砍成 4 位、用时钟的双沿（DDR）各传 4 位，引脚数减半，常见于 FPGA 与外置 PHY 芯片之间。
- **逗号字符（comma）**：8b/10b 里一个位序独一无二、不会出现在任何对齐数据中的特殊码型，接收端靠它找到 10 位字的边界（定界 / word alignment）。
- **MGT / GTX / serdes**：FPGA 内部的高速千兆收发器硬核，负责把并行数据串行化送上线路、再解串回来；本讲的 `gmii_link` 就夹在 MAC 与 serdes 之间。

> 小提示：本讲会反复出现「 disparity（不均等度）」这个词，它在 8b/10b 里是核心概念，下面 4.1 节会专门讲透。

## 3. 本讲源码地图

`serial_io/` 目录里文件很多（chitchat 协议、EVG_EVR 事件定时、pattern generator、噪声注入……），本讲只聚焦「链路层三件套 + 一个低速串口」：

| 文件 | 作用 |
|------|------|
| [serial_io/enc_8b10b.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/enc_8b10b.v) | 8b/10b **编码器**：9 位（8 数据 + K 标志）→ 10 位线路码，维护游动不均等度。 |
| [serial_io/dec_8b10b.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/dec_8b10b.v) | 8b/10b **解码器**：10 位 → 9 位，并报告 `code_err`/`disp_err`。 |
| [serial_io/gmii_link.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/gmii_link.v) | **GMII 链路层**：把 GMII 8 位并行侧与 10 位 8b/10b 串行侧对接，含 PCS 子层与自协商。 |
| [serial_io/gmii_to_rgmii.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/gmii_to_rgmii.v) | **GMII↔RGMII 适配**：用 Xilinx `ODDR`/`IDDR` 原语在 8 位 SDR 与 4 位 DDR 间转换。 |
| [serial_io/simpleuart.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/simpleuart.v) | **低速异步串口**（PicoSoC 移植版），起止式 8N1，用于调试/控制台。 |
| 辅助文件 | `endpoint.vh`（K 码/空闲码常量）、`ep_tx_pcs.v`/`ep_rx_pcs.v`（PCS 收发状态机）、`negotiate.v`（Clause 37 自协商）、`dec_8b10b_tb.v`（解码器测试台）、`Makefile`（构建）。 |

---

## 4. 核心概念与源码讲解

### 4.1 8b/10b 编解码：enc_8b10b 与 dec_8b10b

#### 4.1.1 概念说明

为什么串行链路需要 8b/10b？因为裸的 8 位数据有三大缺陷，编码要用多出来的 2 位「买」掉它们：

1. **可能有长串连续 0/1**：例如字节 `0x00` 全是 0，CDR 拿不到跳变沿就会失锁。
2. **直流不平衡**：`0xFF` 会让线路长期偏 1，AC 耦合链路产生直流漂移。
3. **没有定界标记**：接收端解串后不知道哪 10 位是一个字。

8b/10b 编码（IBM 的 Widmer & Franaszek）把每 8 位编成 10 位线路码，用 25% 的开销换来三项保证：**直流平衡**、**最大连续相同比特 ≤ 5**（游程受限）、以及**独一无二的 comma 码型**用于定界。

Bedrock 的编解码器是 Chuck Benz 2002 年发布的经典开源实现，文件头标注 `per Widmer and Franaszek`：

> 链接：[serial_io/enc_8b10b.v:L1-L11](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/enc_8b10b.v#L1-L11) —— 版权说明与算法出处。

几个关键术语：

- **disparity（不均等度）**：一段码里 1 比 0 多几个。例如 6 个 1、4 个 0，disparity = +2。
- **running disparity, RD（游动不均等度）**：累积的不均等度。编码器实时维护它，使其始终有界（在 ±1 之间）。
- **D 码 / K 码**：数据字符记作 `D.x.y`（x = 低 5 位 5b/6b 编码后的值，y = 高 3 位 3b/4b 编码后的值）；控制字符记作 `K.x.y`，只有 12 个合法值。
- **comma**：最常用的是 **K28.5（字节值 `0xBC`）**，它的码型里含一段「只此一家」的比特序列，接收端扫到它就能锁定字边界。

#### 4.1.2 核心流程

**编码**的关键思想是：每个 10 位码要么完全平衡（5 个 1、5 个 0，disparity = 0），要么 disparity = ±2。对于不平衡码，规范为它定义了**两个极性相反的变体**（RD− 版与 RD+ 版）。编码器根据当前 RD 挑选那个能把 RD 拉回 0 的变体：

\[ \text{RD}_{n+1} = \begin{cases} \text{RD}_n & \text{若本码平衡（5/5），不改变 RD} \\ -\text{RD}_n & \text{若本码不均等（}\pm 2\text{），选反向极性，把 RD 翻回去} \end{cases} \]

因为每个不平衡码都「主动纠正」RD，所以 RD 永远被钳制在 \( \{-1, +1\} \)。于是：

- 任意两个相邻码之后，累计 1/0 数量差有界 → **直流分量为 0**。
- 选码时保证跨码边界也不出现 6 连同 → **最大游程 ≤ 5**。

8b/10b 的拆分技巧是把 8 位拆成 **低 5 位（5b→6b）** 与 **高 3 位（3b→4b）** 两段独立编码，这样组合表从 256 行缩小到 32 + 8 行，便于用查找逻辑实现。编码流水：

1. 输入 `{ki, data[7:0]}` 共 9 位 + 当前 `dispin`。
2. 低 5 位做 5b/6b 编码，高 3 位做 3b/4b 编码。
3. 根据各段的 disparity 与 `dispin`，决定是否对输出**取反**（complement）。
4. 输出 10 位 `dataout` + 新的 `dispout`。

**解码**做逆变换，并多出两根检错线：

- `code_err`：收到的 10 位不是任何合法码字（说明线路有错位/跳变错）。
- `disp_err`：是合法码字，但与输入声称的 `dispin` 矛盾（说明 RD 跟丢了，可能丢了一位）。

#### 4.1.3 源码精读

**编码器端口**——注意 `dispin` 的注释明确说明 0 = 负、1 = 正：

[serial_io/enc_8b10b.v:L13-L17](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/enc_8b10b.v#L13-L17) —— 编码器接口：9 位输入（8 数据 + K 标志）、输入游动不均等度 `dispin`、10 位输出、输出游动不均等度 `dispout`。

5b/6b 编码产生 6 位中的 `xao..xio`，每一项都是输入位的布尔表达式（纯组合逻辑，无状态）：

[serial_io/enc_8b10b.v:L42-L53](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/enc_8b10b.v#L42-L53) —— 5b/6b 编码主体。`xio` 那一行里能看到对 `ki & ei & di & ci & !bi & !ai` 的特殊处理，这正是 **K28.x** 系列控制字符的识别点。

「是否取反」的决策（核心）：当需要把 RD 拉回时，对 6 位段或 4 位段整体异或一个 complement 位：

[serial_io/enc_8b10b.v:L98-L100](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/enc_8b10b.v#L98-L100) —— `compls6 = (pd1s6 & !dispin) | (nd1s6 & dispin)`：仅当本段是不平衡码且当前 RD 与「期望的假设 RD」相反时，才触发取反。

最后输出：`dataout` 的每一位都与对应的 complement 异或，`dispout` 由两段 disparity 异或而成：

[serial_io/enc_8b10b.v:L109-L118](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/enc_8b10b.v#L109-L118) —— `disp6`、`dispout` 的级联计算，以及最终 `dataout = {(xjo^compls4), ..., (xao^compls6)}` 的按位异或输出。

**解码器端口**比编码器多了两根检错线：

[serial_io/dec_8b10b.v:L13-L19](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/dec_8b10b.v#L13-L19) —— 解码器接口，输出 `code_err` 与 `disp_err`。

K 码识别（`xko`）——解码端靠几个特征码型判定这是不是控制字符：

[serial_io/dec_8b10b.v:L105-L107](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/dec_8b10b.v#L105-L107) —— `xko` 的计算，正是 comma / K 码的判定逻辑。

`code_err` 把所有「不可能出现的码型」（如全 0、全 1、跨段 disparity 冲突等）或起来，命中即报错：

[serial_io/dec_8b10b.v:L136-L151](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/dec_8b10b.v#L136-L151) —— `code_err` 的完整或表达式，覆盖 5b/6b 与 3b/4b 的所有非法码型。

最终数据输出与 disparity 校验：

[serial_io/dec_8b10b.v:L153](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/dec_8b10b.v#L153) —— `dataout = {xko, xho, xgo, xfo, xeo, xdo, xco, xbo, xao}`，最高位是 K 标志。

[serial_io/dec_8b10b.v:L156-L163](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/dec_8b10b.v#L156-L163) —— `disp_err`：合法码字但与 `dispin` 矛盾时拉高，注释里作者坦言它对非法码也可能误报。

#### 4.1.4 代码实践

**实践目标**：跑通 serial_io 的自检，亲手确认 comma（K28.5）能被正确收发，并解释 8b/10b 如何保持直流平衡。

**操作步骤**：

1. 在仓库根目录运行：
   ```bash
   make -C serial_io all checks
   ```
   其中 `all` 会编译三个测试台（`dec_8b10b_tb`、`patt_gen_tb`、`gmii_link_tb`）并对 `gmii_to_rgmii.v` 做 Verilator lint；`checks` 会逐个跑自校验，见 [serial_io/Makefile:L12-L19](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/Makefile#L12-L19)。
2. 阅读解码器测试台 [serial_io/dec_8b10b_tb.v:L1-L6](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/dec_8b10b_tb.v#L1-L6)。它在注释里说明：用 Wikipedia 公布的 8b/10b 表做交叉校验，并能把一个 10 位码文件翻译成人读的 `D/K.x.y`。
3. 这个测试台通过 `+init_file=self_sim.dat` 读取一批 10 位码（注释说明该文件是从真实 `gmii_link_view` 波形手工誊抄的空闲流量，里面满是 K28.5 comma），逐行解码并比对，见 [serial_io/dec_8b10b_tb.v:L181-L197](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/dec_8b10b_tb.v#L181-L197)。
4. 单独跑解码器检查并打开详细打印：
   ```bash
   make -C serial_io dec_8b10b_check
   ```
   （Makefile 在 [serial_io/Makefile:L24-L26](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/Makefile#L24-L26) 为它注入 `+init_file=self_sim.dat +init_disp=1`。）

**需要观察的现象**：

- `dec_8b10b_check` 末尾应打印 `PASS`；测试台对每个 10 位码打印一行类似 `... K.28.5 ... .`（最后的 `.` 表示与黄金参考一致，`*` 表示不一致）。
- 在 K28.5 那几行里，注意 `K28.5` 的 5b/6b 段在测试台查找表 [serial_io/dec_8b10b_tb.v:L105-L106](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/dec_8b10b_tb.v#L105-L106) 中对应两个互为取反的码型（`001111` 与 `110000`）——这就是「RD− 版 / RD+ 版」两个极性变体。

**预期结果**：所有行末尾都是 `.`、最终 `PASS`。K28.5 反复出现且其 10 位码型在两个极性间交替——这正是直流平衡的直接证据：每次 RD 偏了，下一个 comma 就取反向极性把它拉回来。

**直流平衡的源码落点**（结合 4.2 的 `gmii_link` 看会更清楚）：编码器的 `dispout` 会被反馈成下一拍的 `dispin`，于是 RD 被持续追踪、钳制有界。具体反馈语句在 [serial_io/gmii_link.v:L57](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/gmii_link.v#L57) 与 [serial_io/gmii_link.v:L86](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/gmii_link.v#L86)（收发各一处）。

> 说明：本仓库**没有独立的 `enc_8b10b` 测试台**，编码器是经由 `gmii_link_tb` 把「enc → 物理回环 → dec」串起来间接验证的（见 4.2 与综合实践）。这一点和 u1-l3 提到的「并非每模块都有独立测试台」一致。

#### 4.1.5 小练习与答案

**练习 1**：如果连续发送字节 `0xFF`（8 个 1），8b/10b 编码后线路上还会出现长串 1 吗？为什么？

> **答案**：不会出现长串 1。`0xFF` 对应 `D31.7`，是不平衡码，编码器会交替使用它的 RD− / RD+ 两个变体，使连续相同比特被切断（游程 ≤ 5），同时 RD 被拉回、直流平衡得以保持。

**练习 2**：解码器的 `code_err` 和 `disp_err` 各自意味着什么？哪个更严重？

> **答案**：`code_err` 表示收到的 10 位根本不是合法码字（位错位/失锁，较严重）；`disp_err` 表示是合法码字但与声称的 RD 矛盾（说明此前丢过位、RD 跟丢了）。两者都应被上层（PCS）计入链路错误统计。

**练习 3**：为什么 K28.5 适合做 comma？

> **答案**：因为它的 10 位码型里含一段独一无二的比特序列（在两个极性变体里都不会出现在任何对齐的数据码或其跨码边界上），接收端只要在比特流里滑窗搜到这段序列，就能确定 10 位字的起始边界。

---

### 4.2 GMII 链路层：gmii_link

#### 4.2.1 概念说明

`gmii_link.v` 是一块**夹在以太网 MAC 与 serdes/MGT 之间的「PHY 模拟器」**。它的职责是：把 MAC 给出的 8 位 GMII 字节流，加工成 10 位 8b/10b 线路码交给串行器；反过来把解串器送来的 10 位码还原成 GMII 字节。

它还承担以太网 PCS（Physical Coding Sublayer）的活：**插入帧间空闲码（K28.5 comma）、帧起始/结束定界符、以及自协商（autonegotiation）信令**。这是 1000BASE-X 光纤链路的标准行为。

一个反直觉但很重要的点：因为 `gmii_link` **扮演 PHY 的角色**，所以从它自己看，GMII 的接收数据 `RXD` 是它的**输出**（它要「产出」接收数据给 MAC），而 `TXD` 是它的**输入**（MAC 喂给它发送数据）。源码第一行注释就强调了这点。

#### 4.2.2 核心流程

`gmii_link` 内部有两条对称的数据通路 + 一个自协商机：

```text
发送方向 (GTX_CLK 域):
  GMII TXD/TX_EN ──► ep_tx_pcs ──► {tx_is_k, tx_odata}[9:0]
                                   (插入 K28.5 空闲 / K27.7 起始 / K29.7,K23.7 结束)
                       └─► enc_8b10b ──► txdata[9:0] ──► serdes

接收方向 (RX_CLK 域):
  serdes ──► rxdata[9:0] ──► dec_8b10b ──► rxdata_dec_out[9:0]
                                              └─► ep_rx_pcs ──► GMII RXD/RX_DV/RX_ER

自协商: negotiate.v 在两条链路间用 LACR 寄存器帧交换能力 (Clause 37)
```

两条通路刻意分处**两个独立的时钟域** `GTX_CLK`（发）与 `RX_CLK`（收），因为实际链路两端的时钟可能有约 100 ppm 的频差，收发必须独立。复位用各自时钟采样后释放，避免复位跨域。

发送 PCS（`ep_tx_pcs`）的状态机决定当前拍送什么：空闲时连续发 K28.5 comma（让对端持续锁字边界）；来帧时发 K27.7 起始符；发完发 K29.7、K23.7 结束符；自协商期间发 LACR 配置帧。这些特殊字节都是 K 码，定义在 `endpoint.vh`：

[serial_io/endpoint.vh:L1-L11](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/endpoint.vh#L1-L11) —— `c_k28_5 = 8'b10111100`（comma / 空闲）、`c_k27_7`（帧起始）、`c_k29_7`/`c_k23_7`（帧结束）等常量定义。

#### 4.2.3 源码精读

模块头部注释与端口——注意 `RXD/RX_DV/RX_ER` 是 `output`、`TXD/TX_EN/TX_ER` 是 `input`：

[serial_io/gmii_link.v:L1-L25](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/gmii_link.v#L1-L25) —— 端口「看起来反了」的原因在此：本模块是 PHY，向 MAC 提供接收侧、消费 MAC 的发送侧。`txdata`/`rxdata` 是与 serdes 对接的 10 位侧。

参数与复位释放（两个时钟域各采样一拍解复位）：

[serial_io/gmii_link.v:L30-L35](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/gmii_link.v#L30-L35) —— `DELAY=1250000` 是自协商定时器（见 `negotiate.v`），`ENC_DISPINIT=1` 给编码器一个确定的初始 RD。

**发送通路**：`ep_tx_pcs` 产出 8 位数据 + K 标志，拼成 9 位送入 `enc_8b10b`；编码器的 `dispout` 反馈给下一拍的 `dispin`（这就是 4.1 说的直流平衡反馈环）：

[serial_io/gmii_link.v:L42-L62](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/gmii_link.v#L42-L62) —— `txdata_in_enc={tx_is_k, tx_odata}`；`always @(posedge GTX_CLK) enc_dispin <= enc_dispout & ~tx_rst;` 把 disparity 串成一条持续追踪的链。

**接收通路**：解串器来的 10 位 `rxdata` 先过 `dec_8b10b` 还原成 9 位（含 K 标志、码错、disparity 错），再由 `ep_rx_pcs` 切成 GMII 字节：

[serial_io/gmii_link.v:L68-L92](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/gmii_link.v#L68-L92) —— 解码器的 `code_err`/`disp_err` 分别接到 `rx_err_code`/`rx_err_rdisp` 并上送给 PCS；`dec_dispin <= dec_dispout` 同样在接收侧维持 RD 追踪。

**自协商**实例：

[serial_io/gmii_link.v:L94-L104](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/gmii_link.v#L94-L104) —— `negotiate` 模块跨 `rx_clk`/`tx_clk` 两个域，通过 LACR 帧与对端协商链路能力，协商成功才拉高 `operate`（允许发数据）。

#### 4.2.4 代码实践

**实践目标**：用 `gmii_link_tb` 验证「发什么、收什么」的回环正确性，并看清自协商握手。

**操作步骤**：

1. 运行：
   ```bash
   make -C serial_io gmii_link_check
   ```
   该测试台把 `txdata` 物理回环到 `rxdata`（`assign gtx_rxdata_10 = phys_en ? gtx_txdata_10 : 0;`），见 [serial_io/gmii_link_tb.v:L52](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/gmii_link_tb.v#L52)。
2. 阅读测试台激励：先断开物理链路（`phys_en=0`）确认无自协商活动，再接通链路、等待 `operate` 拉高，见 [serial_io/gmii_link_tb.v:L66-L108](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/gmii_link_tb.v#L66-L108)。
3. 看 scoreboard：测试台用一个 `shortfifo` 把发送侧的字节（`link.tx.tx_data_p2`）延迟对齐后，与接收侧 `RXD` 逐一比对，见 [serial_io/gmii_link_tb.v:L142-L170](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/gmii_link_tb.v#L142-L170)。

**需要观察的现象**：

- 接通链路后，`operate` 拉高，打印 `PASS: Link is up and auto-negotiation completed successfully.`。
- scoreboard 在 `rx_dv` 有效期间，比较 `scb_data_out == rx_data`，全程不应出现 `Data transmission error`。

**预期结果**：测试以 `PASS` 结束。这等价于间接验证了 `enc_8b10b` 与 `dec_8b10b` 这一对在真实 PCS 流量下的正确性——所有发送字节（含 idle、起始、数据、结束 K 码）经编码、回环、解码后被无损还原。

> 若环境缺 iverilog 等工具，该检查会被跳过（参见 u1-l2 关于可选依赖的说明）；此时可改为纯源码阅读型实践——手动跟踪一个字节 `tx_data` 从 `ep_tx_pcs` 到 `RXD` 的完整路径。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `gmii_link` 把收发放在两个不同的时钟域，而不是共用一个时钟？

> **答案**：因为链路对端的时钟与本地时钟独立（典型差约 100 ppm），接收侧必须用从对端数据恢复出的 `RX_CLK`，发送侧用本地 `GTX_CLK`。共用时钟会导致采样错相、丢字节。

**练习 2**：链路空闲时 `ep_tx_pcs` 持续发 K28.5，这有什么用？

> **答案**：①让接收端 CDR 持续有跳变沿、保持时钟锁定；②让接收端持续看到 comma、维持字边界对齐；③保持 RD 追踪不丢失。一旦真正来帧，起始符 K27.7 立刻可被识别。

**练习 3**：`operate` 信号不拉高时，发送通路里 `TX_EN` 会被怎样处理？

> **答案**：见 [serial_io/gmii_link.v:L46](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/gmii_link.v#L46)，`tx_enable` 实际取 `TX_EN & (operate | an_bypass)`——即自协商未完成（`operate=0`）时，即便 MAC 拉高 `TX_EN`，数据也不会被送上线路，PCS 只会继续发空闲/协商码。

---

### 4.3 GMII↔RGMII 适配：gmii_to_rgmii

#### 4.3.1 概念说明

`gmii_link` 给出的是标准 GMII（8 位 SDR）。但 FPGA 与板载 PHY 芯片之间的物理引脚往往用 **RGMII**——它是 GMII 的「引脚压缩版」：数据从 8 位砍到 4 位，靠时钟的**上升沿传低 4 位、下降沿传高 4 位**（DDR，双数据率）；同时把 `TX_EN` 和 `TX_ER` 复用到一根 `ctl` 线上（上升沿是 `TX_EN`，下降沿是 `TX_EN ^ TX_ER`），接收侧同理。

`gmii_to_rgmii.v` 就是这个 SDR↔DDR 的转换桥。它**直接例化 Xilinx 原语**（`ODDR`/`IDDR`/`OBUF`/`IBUF`/`BUFIO`/`BUFR`/`IDELAYE2`），因而是 serial_io 里**唯一与厂家强绑定**的文件——这也解释了为什么它没有 iverilog 仿真测试，只有 Verilator lint（见 4.3.4）。

#### 4.3.2 核心流程

发送（GMII→RGMII）：

1. 把 8 位 `gmii_txd` 拆成低 4 位（`gmii_txd_rise`，上升沿送）和高 4 位（`gmii_txd_fall`，下降沿送）。
2. 每根数据线用一个 `ODDR` 原语，`D1` 接低 4 位、`D2` 接高 4 位，在时钟双沿输出。
3. `ctl` 线的 `ODDR`：`D1 = TX_EN`，`D2 = TX_EN ^ TX_ER`。
4. 发送时钟 `rgmii_tx_clk` 可选 90° 相移（多数 PHY 要求数据在时钟沿中央对齐）。

接收（RGMII→GMII）：

1. `rgmii_rx_clk` 经 `BUFIO`（快速时钟，驱动 `IDDR`）+ `BUFR`（驱动逻辑）。
2. 每根数据线用 `IDDR` 把 DDR 还原成两拍 4 位，再拼回 8 位。
3. `ctl` 线的 `IDDR`：`Q1` 还原 `RX_DV`，`Q2` 配合算出 `RX_ER = RX_DV ^ Q2`。
4. 可选用 `IDELAYE2` 给数据/ctl 线做延时校准（deskew）。

#### 4.3.3 源码精读

参数与端口——三个参数控制时钟相位与延时校准：

[serial_io/gmii_to_rgmii.v:L3-L34](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/gmii_to_rgmii.v#L3-L34) —— `in_phase_tx_clk`（时钟是否与数据同相）、`idelay_value`、`use_idelay`；注释引用 Xilinx PG051（Tri-Mode Ethernet MAC LogiCORE 产品指南）。

**发送时钟**的相位选择与 `ODDR` 生成（用 `D1=1, D2=0` 在双沿产生方波时钟）：

[serial_io/gmii_to_rgmii.v:L84-L104](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/gmii_to_rgmii.v#L84-L104) —— `rgmii_tx_clk_ = in_phase_tx_clk ? gmii_tx_clk : gmii_tx_clk90`；注释提到 Marvell 88E1512 默认要同相时钟，多数其他 PHY 要 90° 相移。

**发送数据**的 DDR 拆分——`generate` 循环给 4 根线各实例化一个 `ODDR`：

[serial_io/gmii_to_rgmii.v:L121-L140](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/gmii_to_rgmii.v#L121-L140) —— `gmii_txd_rise = gmii_txd[3:0]`、`gmii_txd_fall = gmii_txd[7:4]`，分别接 `ODDR` 的 `D1`/`D2`。

**接收**的时钟缓冲（`BUFIO`+`BUFR`）与可选 `IDELAYE2`：

[serial_io/gmii_to_rgmii.v:L178-L233](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/gmii_to_rgmii.v#L178-L233) —— `generate if (use_idelay)` 分支实例化 `IDELAYE2`（`VAR_LOAD` 模式，可运行期改延时），`else` 分支则是直通连线（注释说明：不例化时该硬件块仍在数据通路里，只是设为最小延时；仿真时省去 `IDELAYCTRL` 更快）。

**接收 ctl** 还原 `RX_DV`/`RX_ER`：

[serial_io/gmii_to_rgmii.v:L239-L253](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/gmii_to_rgmii.v#L239-L253) —— `IDDR` 的 `Q1` 为 `gmii_rx_dv_int`，`Q2` 配合解出 `gmii_rx_er = gmii_rx_dv_int ^ rgmii_rx_ctl_int`。

**接收数据**拼回 8 位：

[serial_io/gmii_to_rgmii.v:L257-L275](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/gmii_to_rgmii.v#L257-L275) —— `gmii_rxd = {gmii_rxd_fall, gmii_rxd_rise}`（高 4 位在下沿、低 4 位在上沿）。

#### 4.3.4 代码实践

**实践目标**：理解为什么这个文件只能 lint、不能仿，并确认它依赖的 Xilinx 原语模型确实存在。

**操作步骤**：

1. 看 Makefile 里它的「测试」目标：
   ```bash
   make -C serial_io gmii_to_rgmii_lint
   ```
   规则见 [serial_io/Makefile:L34-L35](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/Makefile#L34-L35)：
   ```
   $(VERILATOR) --lint-only -Wno-TIMESCALEMOD -Wno-UNUSED $< -y $(FPGA_FAMILY_DIR)/xilinx
   ```
2. 理解 `-y $(FPGA_FAMILY_DIR)/xilinx`：Verilator 在该目录（`fpga_family/xilinx/`）里按模块名查找 `ODDR`/`IDDR`/`OBUF`/`IBUF`/`BUFIO`/`BUFR`/`IDELAYE2` 的行为模型，以便 lint 时能解析这些原语。

**需要观察的现象**：lint 通过、无报错；该目录下确实存在与原语同名的 `.v` 文件（如 `ODDR.v`、`IDDR.v`、`BUFIO.v`、`BUFR.v`、`IDELAYE2.v`）。

**预期结果**：lint 干净通过。这印证了 `gmii_to_rgmii.v` 是 serial_io 中**唯一厂家绑定**的文件——其余文件（编解码器、`gmii_link`、`simpleuart`）都是纯 RTL、可被 iverilog 仿真。

> 若机器未装 Verilator，此目标会被跳过（u1-l2）。此时可改为源码阅读型实践：在 `gmii_to_rgmii.v` 里数出它例化了哪几种 Xilinx 原语，并到 `fpga_family/xilinx/` 下逐一找到对应的行为模型文件。

#### 4.3.5 小练习与答案

**练习 1**：RGMII 为什么能把 8 位 GMII 砍到只用 4 根数据线？

> **答案**：靠 DDR——时钟上升沿传低 4 位、下降沿传高 4 位，同样 125 MHz 下数据率仍为 \(4 \times 2 \times 125\,\text{M} = 1\,\text{Gbit/s}\)，但引脚数减半。

**练习 2**：`in_phase_tx_clk` 参数为什么需要存在？

> **答案**：不同 PHY 对发送时钟与数据的相位关系要求不同。多数 PHY 要时钟相对数据偏移 90°（使采样点落在数据中央）；而 Marvell 88E1512 默认模式要同相时钟。该参数让同一 RTL 适配两种 PHY。

**练习 3**：为什么接收侧用 `BUFIO` + `BUFR` 两个缓冲，而不是一个 `BUFG`？

> **答案**：`BUFIO` 能跑在更高速度上、专门驱动 I/O 列里的 `IDDR`（做精确的逐沿采样），`BUFR` 则把同一时钟分频/缓冲后送给 FPGA 内部逻辑。两者配合才能既保证采样精度、又让逻辑域拿到稳定时钟；`BUFG`（全局时钟树）不适合直接驱动 `IDDR` 的精确时序。

---

### 4.4 低速串口：simpleuart

#### 4.4.1 概念说明

并非所有串行通信都要千兆。FPGA 板上常常还需要一个**慢速、 ubiquitous 的异步串口（UART）**做调试控制台、启动加载、低速遥测。`simpleuart.v` 就是这个角色——它来自 PicoSoC（Clifford Wolf），由 Bedrock 的 L. Doolittle 轻量改造。

它与前面三个模块形成鲜明对比：

| 维度 | gmii_link（+8b/10b） | simpleuart |
|------|---------------------|------------|
| 速率 | 1 Gbit/s | 9600 ~ M bit/s 量级 |
| 编码 | 8b/10b，需 CDR | 无（异步，靠波特率约定） |
| 时钟 | 125 MHz，收发独立域 | 单一 `clk`，软件分频 |
| 复杂度 | PCS 状态机 + 自协商 | 一个移位状态机 |

`simpleuart` 是典型的起止式（start-stop）异步串口：每个字节 = 1 个起始位（低）+ 8 个数据位 + 1 个停止位（高），线路空闲为高。

#### 4.4.2 核心流程

波特率由 `cfg_divider`（20 位）决定：**一个比特的时间 = 时钟周期 × cfg_divider**。例如 `clk = 125 MHz`、要 9600 baud：

\[ \text{cfg\_divider} = \frac{125\,\text{MHz}}{9600} \approx 13021 \]

发送：CPU 写一个字节（`b_we` 脉冲）→ 装入 `{1'b1, b_di, 1'b0}`（停止位、8 数据、起始位）→ 每过一个比特时间右移一位、从 `ser_tx` 甩出最低位。

接收：监测 `ser_rx` 的下降沿（起始位）→ 在每个比特中央采样 → 把 8 个数据位移入 `recv_pattern` → 写进 `recv_buf_data`，置 `b_dv`（数据有效），等 CPU 来读（`b_re`）。

#### 4.4.3 源码精读

端口与波特率注释：

[serial_io/simpleuart.v:L19-L50](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/simpleuart.v#L19-L50) —— 头注释给出波特率换算公式与 9600 baud 的例子；`cfg_divider` 是直接输入（Doolittle 的改造之一：不挂在主机总线上）。`b_busy`/`b_dv` 是新增输出。

发送移位——`ser_tx` 始终输出移位寄存器最低位，写脉冲触发装载：

[serial_io/simpleuart.v:L110](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/simpleuart.v#L110) —— `assign ser_tx = send_pattern[0];`。

[serial_io/simpleuart.v:L127-L130](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/simpleuart.v#L127-L130) —— `b_we && !send_bitcnt` 时装入 `{1'b1, b_di, 1'b0}` 并启动 10 位移位。

接收状态机——空闲等起始位、逐位居中采样：

[serial_io/simpleuart.v:L80-L106](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/simpleuart.v#L80-L106) —— `state 0` 检测 `ser_rx` 拉低（起始位），`state 1` 在半比特延时后对齐到第一个数据位中央，之后每整比特采样一位并右移，`state 10` 收完置 `recv_buf_valid`。

#### 4.4.4 代码实践

**实践目标**：把 `simpleuart` 的波特率算清楚，并理解它在 Bedrock 中的位置。

**操作步骤**：

1. 源码阅读型实践（serial_io 目录没有独立的 simpleuart 测试台，它在 SoC 工程里被间接使用）：阅读 [serial_io/simpleuart.v:L28-L32](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/simpleuart.v#L28-L32) 的波特率说明。
2. 用公式 \( \text{cfg\_divider} = \text{clk}/\text{baud} \) 计算：`clk = 125 MHz` 时，115200 baud 对应的 `cfg_divider` 应是多少？

**需要观察的现象**：算出 \( 125\,000\,000 / 115200 \approx 1085 \)，与 9600 baud 时的 13021 形成对比——波特率翻倍、分频值减半。

**预期结果**：`cfg_divider ≈ 1085`（取整）。该值会被 SoC 软件写入 `simpleuart` 的分频寄存器。

> 「待本地验证」：本仓库顶层未提供独立的 simpleuart 仿真入口；如需运行验证，可参考 u7-l1（picorv32 SoC），那里 `simpleuart` 作为控制台串口被实际例化与测试。

#### 4.4.5 小练习与答案

**练习 1**：为什么接收端在起始位后要等「半个比特」再开始采样数据？

> **答案**：为了把采样点对齐到每个数据位的中央（远离两端的跳变沿），最大化抗抖动余量。`state 1` 用 `2*recv_divcnt > cfg_divider` 实现半比特延时。

**练习 2**：`simpleuart` 与 `gmii_link` 都叫「串口」，本质区别是什么？

> **答案**：`simpleuart` 是**异步**串口（无独立时钟线、靠约定波特率、起止位定界、低速）；`gmii_link` 背后是**同步**高速串行链路（靠 CDR 从 8b/10b 数据恢复时钟、comma 定界、千兆级）。两者在 Bedrock 中分别服务「调试控制台」与「网络数据」两类完全不同的需求。

---

## 5. 综合实践

**任务**：跟踪一个以太网字节从 MAC 到对端 MAC 的完整旅程，串起本讲全部知识点。

在 `gmii_link_tb` 的回环环境里，发送激励在 [serial_io/gmii_link_tb.v:L56-L63](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/gmii_link_tb.v#L56-L63) 产生 `tx_data`（含前导码 `0x55`/`0xD5` 与递增数据）。请按顺序回答：

1. **PCS 加工**：`tx_data` 进入 `ep_tx_pcs` 后，在 [serial_io/ep_tx_pcs.v:L105-L109](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/ep_tx_pcs.v#L105-L109)（`TX_COMMA` 发 K28.5）、[serial_io/ep_tx_pcs.v:L151-L156](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/ep_tx_pcs.v#L151-L156)（`TX_SPD` 发 K27.7 起始）、[serial_io/ep_tx_pcs.v:L165-L178](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/ep_tx_pcs.v#L165-L178)（`TX_EPD`/`TX_EXTEND` 发 K29.7、K23.7 结束）被加上定界 K 码。画出这个状态机的状态转移图。
2. **编码**：每个 8 位字节（含 K 标志）经 `enc_8b10b` 编成 10 位，`dispout` 反馈成下一拍 `dispin`（[serial_io/gmii_link.v:L57](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/gmii_link.v#L57)）。指出哪几拍输出的是 K 码、哪几拍是 D 码。
3. **回环与解码**：10 位 `txdata` 经测试台的物理回环变成 `rxdata`，被 `dec_8b10b` 还原（[serial_io/gmii_link.v:L88-L92](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/gmii_link.v#L88-L92)），再由 `ep_rx_pcs` 剥掉定界 K 码、还原成 GMII `RXD`。
4. **比对**：scoreboard（[serial_io/gmii_link_tb.v:L159-L170](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/serial_io/gmii_link_tb.v#L159-L170)）确认发送字节 == 接收字节。

**交付**：一张标注了「字节 → K 码定界 → 10 位编码 → 回环 → 解码 → 字节」的完整数据通路图，并指出直流平衡（RD 反馈）和字边界定界（comma）分别在哪一步起作用。

> 若想再进一步：把 `gmii_to_rgmii` 接到 `gmii_link` 的 GMII 侧（画出框图），说明此时数据要再经过一次 8→4 DDR 转换才能送到外部 PHY 芯片——这就把本讲四个模块全串起来了。

## 6. 本讲小结

- **8b/10b** 用 25% 开销换来直流平衡、游程 ≤ 5 与 comma 定界三项保证；核心机制是**游动不均等度（RD）的反馈追踪**——不平衡码主动取反向极性把 RD 拉回，Bedrock 的 `enc_8b10b`/`dec_8b10b` 是 Chuck Benz 的经典实现。
- **`gmii_link`** 是 GMII 8 位并行侧与 10 位 8b/10b 串行侧之间的桥，扮演 PHY（故端口方向「反」），内含 PCS 状态机（用 K28.5 空闲、K27.7 起始、K29.7/K23.7 结束定界）与 Clause 37 自协商；收发分处 `GTX_CLK`/`RX_CLK` 两个时钟域。
- **直流平衡的硬件落点**是 `dispout → dispin` 的反馈寄存器（收发各一）；解码器额外给出 `code_err`/`disp_err` 两级检错。
- **`gmii_to_rgmii`** 用 Xilinx `ODDR`/`IDDR` 原语做 GMII(8 SDR)↔RGMII(4 DDR) 转换，是 serial_io 中**唯一厂家绑定**的文件，因此只做 Verilator lint、不做 iverilog 仿真。
- **`simpleuart`** 是低速异步串口（起止式 8N1，软件分频定波特率），服务于调试控制台/启动加载，与千兆同步链路形成「调试 vs 数据」的分工。
- serial_io 的构建统一走 `make -C serial_io all checks`；测试台与依赖清单见 `serial_io/Makefile`。

## 7. 下一步学习建议

- **u5-l2（ChitChat 串行协议）**：本讲讲的是「如何把字节可靠地变成线路比特」；下一讲进入 ChitChat——Bedrock 自定义的、跑在这些 8b/10b 链路之上的轻量应用层协议，重点看 `chitchat_tx/rx` 与多时钟域跨越。
- **u5-l3（TCL 驱动的 MGT 配置）**：本讲的 `txdata`/`rxdata` 10 位侧最终要接到 FPGA 的 MGT/GTX 收发器；下一讲以 `comms_top` 为例，讲 `mgt_gen.tcl`/`qgt_wrap.v` 如何用 TCL 在一个 Quad 里配置多条协议。
- **延伸阅读**：若对 PCS/自协商细节感兴趣，可细读 `serial_io/negotiate.v`（Clause 37 状态机）与 `serial_io/ep_sync_detect.v`（comma 同步检测）；若对 8b/10b 想要权威表，可对照 `dec_8b10b_tb.v` 注释里指向的 Wikipedia 8b/10b 词条。
