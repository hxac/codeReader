# eth_phy_10g 发送与 MAC/PHY 合一

## 1. 本讲目标

学完本讲后，你应当能够：

- 画出 10GBASE-R **发送方向**的完整数据通路：XGMII → 64b/66b 编码 → 扰码 → SERDES，并说清每一级由哪个模块负责。
- 说清 `serdes_tx_data`/`serdes_tx_hdr` 这对 SERDES 接口信号的含义，以及 `BIT_REVERSE`、`SERDES_PIPELINE`、PRBS31 测试模式各自的作用。
- 理解 `eth_phy_10g` 如何把发送链路与（上一讲 u10-l2 讲过的）接收链路拼成一个完整的 PCS/PMA PHY。
- **最重要**：讲清 `eth_mac_phy_10g` 这个「MAC/PHY 合一」顶层到底合并了什么——它并不是把 `eth_mac_10g` 和 `eth_phy_10g` 简单连起来，而是换了一条内部路径，彻底消除了两者之间那条很宽的 XGMII 接口。

本讲是 10G/25G PHY 专题的收尾：u10-l1 讲了 64b/66b 编解码，u10-l2 讲了接收链路（块锁定、BER、watchdog），本讲补齐发送链路，并把 MAC 与 PHY 合成单一顶层。

## 2. 前置知识

在进入发送链路前，先回顾两个关键概念（详细推导见依赖讲义 u10-l1、u10-l2、u9-l2）。

**64b/66b 块结构。** 10GBASE-R 每拍向 SERDES 送一个 66 位的「块」：2 位**同步头**（sync header）+ 64 位**负载**。同步头 `10` 表示数据块、`01` 表示控制块；控制块的第 1 字节是块类型码。同步头**不扰码**、明文传输，正是为了让接收侧能靠它做块对齐。详见 u10-l1。

**XGMII 接口。** 10G MAC 与 PHY 之间的标准接口是 XGMII：每拍 64 位数据 `xgmii_txd` 配 8 位逐字节控制位 `xgmii_txc`，用 `IDLE`/`START`/`TERMINATE` 等控制字符在一拍内给帧定界。`eth_mac_10g`（u9-l2）在 AXI-Stream 与 XGMII 之间翻译；`eth_phy_10g` 在 XGMII 与 SERDES 之间翻译。本讲会看到，合一模块把这条 XGMII 边界彻底抹掉了。

**扰码（scrambling）。** 64b/66b 的 64 位负载要经扰码再上线路，目的是让比特流尽量随机、避免长串连 0/连 1，方便接收侧时钟恢复。BASE-R 扰码器是一个 58 位移位寄存器，生成多项式为

\[ x^{58} + x^{39} + 1 \]

它是**自同步**的：扰码器的状态由数据流本身驱动前进，因此收发两侧无需约定初值——发送侧用明文数据驱动状态、输出「明文 ⊕ 伪随机序列」；接收侧用收到的（已扰）数据驱动同样的状态、再异或一次即可还原明文。本库的扰码器由通用 `lfsr` 模块（见 u2-l1）实现。

**SERDES。** Serial-Deserializer，串并转换器。在 FPGA 里通常对应一个硬核 transceiver（如 Xilinx GTY、Intel PHY Lite）。PCS 层输出的是 66 位并行块（64 数据 + 2 同步头），SERDES 把它打成高速串行比特流发出去。本库的 RTL 只负责到「并行 66 位块」这一层，再往下交给厂商 SERDES 原语。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 作用 |
| --- | --- |
| `rtl/eth_phy_10g_tx.v` | **发送链路顶层**：把 XGMII→64b/66b 编码→扰码三段拼起来，对外暴露 XGMII 输入与 SERDES 输出。 |
| `rtl/eth_phy_10g_tx_if.v` | **SERDES 发送接口**：对已编码的 66 位块做扰码、可选 PRBS31 测试图、位序翻转、流水线，输出最终的 `serdes_tx_data/hdr`。 |
| `rtl/eth_phy_10g.v` | **完整 PHY**：把发送链路（本讲）和接收链路（u10-l2）合成一个 PCS/PMA PHY，含双时钟域与全部状态/控制接口。 |
| `rtl/eth_mac_phy_10g.v` | **MAC/PHY 合一顶层**：AXI-Stream 直连 SERDES，内部不再经过 XGMII。 |
| `rtl/eth_mac_phy_10g_tx.v` / `rtl/eth_mac_phy_10g_rx.v` | 合一顶层内部的发送/接收子模块，分别用 `axis_baser_tx_64`/`axis_baser_rx_64` 把「MAC 成帧」与「64b/66b 编解码」合并进单个模块。 |
| `rtl/eth_mac_10g.v` | （对照组）独立的 10G MAC，对外是 XGMII；用于和合一模块做端口对比。 |

## 4. 核心概念与源码讲解

### 4.1 PHY 发送链路：XGMII → 64b/66b → SERDES

#### 4.1.1 概念说明

`eth_phy_10g_tx` 是 10GBASE-R **发送方向**的入口。它的职责很纯粹：吃进 MAC 给的 XGMII 信号（`xgmii_txd`/`xgmii_txc`），吐出能给 SERDES 用的 66 位并行块（`serdes_tx_data`/`serdes_tx_hdr`）。

这条链路分两段，对应两个内联子模块：

1. **编码段** `xgmii_baser_enc_64`：把 XGMII 的数据/控制字符翻译成 64b/66b 块（决定同步头是数据块还是控制块、按块类型表打包控制字符）。这一段在 u10-l1 已详细讲过。
2. **接口段** `eth_phy_10g_tx_if`：对编码出的 64 位负载做 BASE-R 扰码（同步头不动），再交给 SERDES。

`eth_phy_10g_tx` 自己几乎不含逻辑，只做布线与参数透传——它是典型的「布线层」模块（与 `eth_mac_1g`、`ip_complete` 同风格）。

#### 4.1.2 核心流程

发送方向一拍数据走过的路径：

```
xgmii_txd[63:0], xgmii_txc[7:0]   (MAC 给的 XGMII 帧/IDLE)
            │
            ▼
   xgmii_baser_enc_64              (64b/66b 编码：识别 START/TERM，选块类型)
            │
            ▼  encoded_tx_data[63:0] + encoded_tx_hdr[1:0]
   eth_phy_10g_tx_if               (扰码 64 位负载；同步头明文；可选 PRBS31)
            │
            ▼
   serdes_tx_data[63:0] + serdes_tx_hdr[1:0]   (送给厂商 SERDES)
```

注意一个关键的不对称：**同步头不扰码**。`encoded_tx_hdr` 直接透传到 `serdes_tx_hdr`，只有 `encoded_tx_data` 进扰码器。这是 64b/66b 协议的硬性要求——同步头必须明文，接收侧才能在解扰之前先靠它做块对齐（见 u10-l2 的 frame_sync）。

#### 4.1.3 源码精读

模块端口与参数。输入是 XGMII，输出是 SERDES，并带一个 `cfg_tx_prbs31_enable` 测试使能：

[rtl/eth_phy_10g_tx.v:34-69](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_tx.v#L34-L69) —— 模块声明：`DATA_WIDTH` 固定 64、`HDR_WIDTH` 固定 2；XGMII 输入 `xgmii_txd/txc`，SERDES 输出 `serdes_tx_data/hdr`，状态 `tx_bad_block`。

模块体只有两件事：例化编码器，再例化接口模块。中间用 `encoded_tx_data`/`encoded_tx_hdr` 两根线把两段串起来：

[rtl/eth_phy_10g_tx.v:89-123](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_tx.v#L89-L123) —— 先例化 `xgmii_baser_enc_64` 完成 XGMII→64b/66b 编码并输出 `tx_bad_block`（遇到非法 XGMII 控制字符时报错）；再例化 `eth_phy_10g_tx_if` 把编码结果扰码后送 SERDES。

模块开头还有一段 `initial` 断言（L72-87），用 `$error` 强制 `DATA_WIDTH==64`、`CTRL_WIDTH*8==DATA_WIDTH`、`HDR_WIDTH==2`，保证接口字节对齐——这是全库 10G 模块统一的宽度护栏。

#### 4.1.4 代码实践

**目标**：在仿真里亲眼看到「XGMII 输入 → SERDES 输出」的端到端通路，并确认同步头未被扰码。

**操作步骤**：

1. 进入 `tb/eth_phy_10g/`，阅读 `test_eth_phy_10g.py`。这个 testbench 同时挂了 XGMII 源/宿（`XgmiiSource`/`XgmiiSink`）和 SERDES 源/宿（`BaseRSerdesSource`/`BaseRSerdesSink`，来自同目录的 `baser.py`），正是用来驱动 `eth_phy_10g` 的。
2. 配好 cocotb + iverilog 后运行 `make`（详见 u1-l4）。
3. 用 `WAVES=1 make` 重新跑一次生成波形，打开 `.fst`，跟踪发送方向：从 `xgmii_txd/txc` → 内部 `encoded_tx_data/hdr` → `serdes_tx_data/hdr`。

**需要观察的现象**：

- `serdes_tx_hdr`（2 位同步头）与 `encoded_tx_hdr` **逐拍完全相同**（明文透传）。
- `serdes_tx_data` 与 `encoded_tx_data` **不同**（已被扰码）；如果暂时把参数 `SCRAMBLER_DISABLE` 设成 1 重跑，两者才会相同——这能反向验证扰码确实在工作。

**预期结果**：扰码开启时 SERDES 侧数据看似随机但可被对端 `BaseRSerdesSink` 正确解扰还原；扰码关闭时 SERDES 侧直接等于编码输出。

> 待本地验证：具体波形随测试种子而变，以你本地仿真输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么同步头 `serdes_tx_hdr` 不进扰码器？

**参考答案**：接收侧必须先靠同步头判定 2 位同步头位置、做块对齐（块锁定），然后才能正确解扰 64 位负载。若同步头也被扰码，接收侧将无法在不预知扰码状态的情况下找到块边界，整个自同步机制失效。

**练习 2**：`eth_phy_10g_tx` 自身几乎没有时序逻辑，这种「薄布线层」设计有什么好处？

**参考答案**：把「编码」和「扰码/接口」分成两个独立子模块，便于单独复用与单独测试——例如 `xgmii_baser_enc_64` 也能被别的自定义 PHY 复用，而 `eth_phy_10g_tx_if` 也能接到别的编码器后端。顶层只负责按协议把两者顺序接好，职责单一、易维护。

---

### 4.2 eth_phy_10g_tx_if：扰码、PRBS31 与 SERDES 接口

#### 4.2.1 概念说明

`eth_phy_10g_tx_if` 是真正触碰到 SERDES 引脚的那一层。它接收已经编码好的 66 位块，做四件事后输出：

1. **BASE-R 扰码**：仅对 64 位负载异或一段伪随机序列。
2. **PRBS31 测试模式**：链路连通性测试时，用伪随机比特流（PRBS31）替换正常数据，便于对端做误码率扫描。
3. **位序翻转**（`BIT_REVERSE`）：适配某些 SERDES 的比特序约定。
4. **流水线延迟**（`SERDES_PIPELINE`）：给布局布线留余量，可插入若干级寄存器。

它还定义了「SERDES 接口」这一贯穿全库 10G 模块的接口契约：64 位数据 + 2 位同步头，以及（接收侧的）`bitslip`/`reset_req` 控制线。本讲从发送侧理解数据线，控制线在 4.3 节连同接收侧一起讲。

#### 4.2.2 核心流程

扰码器与 PRBS31 生成器都建立在通用 `lfsr` 模块（u2-l1）之上，区别在于**状态如何驱动**：

```
                encoded_tx_data ──┐
                                  ▼
                         scrambler (lfsr, FIBONACCI, 反馈)
                                  │ scrambled_data
                                  ▼
   正常模式：serdes_tx_data ← scrambled_data      (hdr ← encoded_tx_hdr 明文)
   测试模式：serdes_tx_data ← ~prbs31 序列         (hdr ← ~prbs31 序列)
```

- **扰码器**（`LFSR_CONFIG="FIBONACCI"`、`LFSR_FEED_FORWARD=0`、`REVERSE=1`、`LFSR_POLY=58'h8000000001`）：`data_in` 接 `encoded_tx_data`，即**明文数据反馈进移位寄存器**驱动状态前进——这正是「自同步」的实现。状态寄存器 `scrambler_state_reg` 跨拍保存，每拍把 `lfsr` 算出的 `state_out` 写回（见下文 always 块）。
- **PRBS31 生成器**（`LFSR_WIDTH=31`、`LFSR_POLY=31'h10000001`）：`data_in` 接全 0，**只靠 `state_in` 驱动**——这个 LFSR 才是真正「自由运行」的，与数据流无关，用来产生标准测试图样。

两者并存，由 `cfg_tx_prbs31_enable` 在运行时二选一送到输出寄存器。

> 辨析：扰码器是「自同步」（状态耦合到数据），PRBS31 生成器是「自由运行」（状态自激）。两者都叫 LFSR，但工作方式不同，别混淆。

#### 4.2.3 源码精读

扰码器例化，多项式 `58'h8000000001` 对应 \(x^{58}+x^{39}+1\)，`data_in` 接明文编码数据：

[rtl/eth_phy_10g_tx_if.v:135-149](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_tx_if.v#L135-L149) —— BASE-R 扰码器：`data_in(encoded_tx_data)`、`state_in(scrambler_state_reg)`，输出 `scrambled_data` 与下一状态 `scrambler_state`。

PRBS31 生成器，`data_in` 接全 0、`DATA_WIDTH` 取 `DATA_WIDTH+HDR_WIDTH=66`，一次产出 66 位测试图样：

[rtl/eth_phy_10g_tx_if.v:151-165](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_tx_if.v#L151-L165) —— PRBS31 生成器：自由运行，输出 `prbs31_data[65:0]`。

核心的输出选择与状态推进，全部集中在一个 `always @(posedge clk)` 里：

[rtl/eth_phy_10g_tx_if.v:167-179](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_tx_if.v#L167-L179) —— 每拍把扰码器的 `state_out` 写回 `scrambler_state_reg`（保持自同步链不断）；若 `PRBS31_ENABLE && cfg_tx_prbs31_enable`，则数据与同步头都输出取反的 PRBS31 序列；否则数据取扰码结果（`SCRAMBLER_DISABLE` 时取明文）、同步头始终取 `encoded_tx_hdr`（明文）。

`BIT_REVERSE` 与 `SERDES_PIPELINE` 两段 `generate` 负责物理适配：

[rtl/eth_phy_10g_tx_if.v:92-133](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_tx_if.v#L92-L133) —— `BIT_REVERSE` 把输出位序反转；`SERDES_PIPELINE>0` 时插入若干级 `srl_style="register"` 的移位寄存器做延迟，给 SERDES 对接留时序余量，默认 0 表示直连。

#### 4.2.4 代码实践

**目标**：用 PRBS31 测试模式验证链路连通，并理解 `SERDES_PIPELINE` 的作用。

**操作步骤**：

1. 阅读 `tb/eth_phy_10g/Makefile` 与 `test_eth_phy_10g.py`，确认 DUT 例化时 `PARAM_PRBS31_ENABLE` 是否为 1、`PARAM_TX_SERDES_PIPELINE` 取值多少。
2. 运行 `make`，观察测试用例里是否有「先开 PRBS31 自检、再切回正常数据」的流程。
3. 在 testbench 里手动把 `cfg_tx_prbs31_enable` 拉高（若已有用例则直接看其波形），观察 `serdes_tx_data` 是否变成近似随机的 PRBS31 图样。

**需要观察的现象**：

- PRBS31 模式下 `serdes_tx_data`/`serdes_tx_hdr` 与正常帧完全无关，呈现伪随机分布。
- 对端（`BaseRSerdesSink` 配合 PRBS 检查器）可统计误码数；理想链路误码为 0。

**预期结果**：PRBS31 是 10G 链路的标准连通性/误码率自检手段，开启后收发两侧的 PRBS 状态会自动对齐（PRBS31 也是自同步的），无需额外握手。

> 待本地验证：误码统计的具体数值依赖仿真注入，以本地结果为准。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `SCRAMBLER_DISABLE` 设为 1，链路还能正常工作吗？会有什么风险？

**参考答案**：功能上数据仍能收发（对端只要也关掉解扰即可还原），但负载不再被随机化，可能出现长串连 0/连 1，导致接收侧 SERDES 的时钟恢复（CDR）失锁、直流漂移增大。标准 10GBASE-R 要求扰码常开，`SCRAMBLER_DISABLE` 只用于调试。

**练习 2**：`SERDES_PIPELINE` 设成 0 和设成 2 的区别是什么？为什么要让它可配？

**参考答案**：设 0 表示 SERDES 输出寄存器直接驱动引脚、无额外延迟；设 2 表示在输出路径上插入 2 级寄存器。可配是因为不同器件、不同板级的 SERDES 引脚时序余量不同——当输出寄存器到引脚走线较长时，插入流水线寄存器可以改善建立/保持时间，帮助时序收敛。

---

### 4.3 eth_phy_10g：完整 PHY 收发合一

#### 4.3.1 概念说明

`eth_phy_10g` 把发送链路（本讲 4.1/4.2）和接收链路（u10-l2）拼成一个完整的 10GBASE-R PCS/PMA PHY。它的对外接口非常对称：一侧是 XGMII（连 MAC），另一侧是 SERDES（连厂商 transceiver），再加上一整套状态与配置信号。

关键特征：

- **双时钟域**：`rx_clk`/`tx_clk` 独立。接收与发送各自有自己的复位（`rx_rst`/`tx_rst`），子模块分别挂在各自时钟上。这是因为实际板子上收发参考时钟往往不同源。
- **参数透传**：扰码、PRBS31、位序、流水线、块锁定时序（`BITSLIP_*`、`COUNT_125US`）等参数统一在顶层声明，再分发到收发两个子模块。

#### 4.3.2 核心流程

```
              ┌─────────────── eth_phy_10g ───────────────┐
  XGMII TX ─► │ eth_phy_10g_tx ──► serdes_tx_data/hdr     │ ─► SERDES
             │                                              │
  XGMII RX ◄─ │ eth_phy_10g_rx ◄── serdes_rx_data/hdr     │ ◄─ SERDES
              │   (块锁定/BER/watchdog，见 u10-l2)          │
              │   serdes_rx_bitslip / serdes_rx_reset_req  │
              └────────────────────────────────────────────┘
```

接收侧的 `serdes_rx_bitslip`（让 SERDES 滑动比特边界以找块对齐）和 `serdes_rx_reset_req`（链路长时间异常时复位 SERDES）由 u10-l2 讲过的 frame_sync/watchdog 产生，本模块只是把它们引到顶层对外。

#### 4.3.3 源码精读

顶层端口：双时钟域、XGMII、SERDES 收发、状态、配置一应俱全：

[rtl/eth_phy_10g.v:34-88](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g.v#L34-L88) —— 注意 SERDES 接口契约：发送 `serdes_tx_data[63:0]`+`serdes_tx_hdr[1:0]`，接收 `serdes_rx_data`+`serdes_rx_hdr`，外加 `serdes_rx_bitslip`/`serdes_rx_reset_req` 两条控制线；状态含 `tx_bad_block`、`rx_error_count`、`rx_block_lock`、`rx_high_ber`、`rx_status` 等。

模块体只是并列例化收发两个子模块，并把顶层参数透传下去：

[rtl/eth_phy_10g.v:90-138](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g.v#L90-L138) —— 接收实例挂 `rx_clk`/`rx_rst`、占用 `RX_SERDES_PIPELINE`；发送实例挂 `tx_clk`/`tx_rst`、占用 `TX_SERDES_PIPELINE`。两者共享同一对 SERDES 引脚与 `PRBS31_ENABLE` 等链路层参数。

> 小贴士：`COUNT_125US = 125000/6.4` 是 125 µs 窗口对应的时钟周期数（6.4 ns 是 156.25 MHz 的周期），BER 监测与 watchdog 都按这个窗口计数，详见 u10-l2。

#### 4.3.4 代码实践

**目标**：梳理 `eth_phy_10g` 对外接口的「数据面」与「控制/状态面」，为下一节对比合一模块做准备。

**操作步骤**：

1. 打开 `rtl/eth_phy_10g.v`，把端口分成三类列出：XGMII 数据线、SERDES 数据/控制线、状态/配置线。
2. 数一下 XGMII 这一侧的信号位数：`xgmii_txd`(64)+`xgmii_txc`(8)+`xgmii_rxd`(64)+`xgmii_rxc`(8)。

**需要观察的现象与预期结果**：XGMII 侧合计 144 位数据线。这就是 MAC 与 PHY 分立时，两者之间必须布的内部连线宽度——下一节会看到合一模块如何把它省掉。

> 本实践为源码阅读型，无需运行仿真。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `eth_phy_10g` 要分 `rx_clk`/`tx_clk` 两个时钟域，而不是像千兆 `eth_mac_1g` 那样基本单时钟？

**参考答案**：10G SERDES 的收发参考时钟通常来自不同的 PLL/晶振（如线路侧恢复时钟 vs 本地参考时钟），并不保证同源同频。把收发放在独立时钟域，让 RTL 直接适配真实的多时钟环境；需要桥接到统一逻辑时钟域时，再在 MAC 外面加 `_fifo` 变体（用异步 FIFO，见 u5-l1）。

**练习 2**：`serdes_rx_bitslip` 和 `serdes_rx_reset_req` 分别在什么情况下被拉起？

**参考答案**：`serdes_rx_bitslip` 由 frame_sync 在未锁定且检测到非法同步头时发出脉冲，滑动 SERDES 的字边界以尝试重新对齐（连续 64 个合法头后获 `rx_block_lock`）；`serdes_rx_reset_req` 由 watchdog 在连续约 2 ms（多个 125 µs 坏窗）仍异常时拉起，请求复位整个 SERDES 重新建链。详见 u10-l2。

---

### 4.4 eth_mac_phy_10g：MAC/PHY 合一顶层

#### 4.4.1 概念说明

`eth_mac_phy_10g` 是本讲的重头戏。直观上你会以为「MAC/PHY 合一」就是把 `eth_mac_10g`（AXI↔XGMII）和 `eth_phy_10g`（XGMII↔SERDES）背靠背连起来、把中间的 XGMII 藏到模块内部。**但源码不是这么做的。**

真正的实现是：合一顶层例化 `eth_mac_phy_10g_tx` 与 `eth_mac_phy_10g_rx`，而这两个子模块内部用的是 `axis_baser_tx_64`/`axis_baser_rx_64`——它们把「MAC 成帧」（原本由 `axis_xgmii_tx_64` 在 `eth_mac_10g` 里做）和「64b/66b 编解码」（原本由 `xgmii_baser_enc/dec_64` 在 `eth_phy_10g` 里做）**合并进同一个模块**。于是数据从 AXI 直接走到 64b/66b 块，XGMII 这一中间表示根本不出现。

这样做有两个直接收益，也有一个代价：

- **收益 1：消除宽接口。** 分立设计要在 MAC 与 PHY 之间布 144 位 XGMII 线（见 4.3.4）；合一设计直接 AXI↔SERDES，对外完全没有 XGMII。
- **收益 2：简化 SERDES 对接。** 顶层只需关心一对 SERDES 数据/同步头引脚加两条控制线，MAC 成帧与 64b/66b 编码共享同一模块，少一层信号翻译。
- **代价：丢弃流量控制。** `eth_mac_phy_10g` **没有** `eth_mac_10g` 里那套 `mac_ctrl_*`/`mac_pause_ctrl_*`（PAUSE/PFC）子模块——它的端口里找不到 `tx_lfc_req`、`tx_pfc_req`、`cfg_mcf_*`、`stat_tx_*` 等任何流控信号。需要 PAUSE/PFC 的场合必须用分立的 `eth_mac_10g`。

> 教学提示：这是典型的「接口宽度 vs 功能完整性」取舍。合一模块换来了极简的对外接口，代价是砍掉了链路级流量控制。

#### 4.4.2 核心流程

分立 vs 合一两条路径的对照：

```
分立（eth_mac_10g + eth_phy_10g）：
  AXI ─► axis_xgmii_tx_64 ─► XGMII(144位) ─► xgmii_baser_enc_64 ─► tx_if ─► SERDES
        └──── eth_mac_10g ────┘            └──────── eth_phy_10g ────────────┘

合一（eth_mac_phy_10g）：
  AXI ─► axis_baser_tx_64 ─► tx_if ─► SERDES
        └─ eth_mac_phy_10g_tx ─┘
        （成帧+64b66b 编码合并；无 XGMII；无 PAUSE/PFC）
```

接收方向对称：`eth_mac_phy_10g_rx` 内部是 `eth_phy_10g_rx_if`（解扰、frame_sync/BER/watchdog，u10-l2）→ `axis_baser_rx_64`（64b66b 解码 + MAC 成帧合并）→ AXI。

#### 4.4.3 源码精读

合一顶层只是并列例化收发两个子模块，端口从 AXI 直接到 SERDES：

[rtl/eth_mac_phy_10g.v:34-125](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_phy_10g.v#L34-L125) —— 注意端口里**没有**任何 `xgmii_*` 信号：只有 AXI 收发（`tx_axis_*`/`rx_axis_*`）、SERDES 收发（`serdes_tx_data/hdr`/`serdes_rx_data/hdr`/`serdes_rx_bitslip`/`serdes_rx_reset_req`）、PTP、状态、配置（`cfg_ifg`/`cfg_tx_enable`/`cfg_rx_enable`/`cfg_tx_prbs31_enable`/`cfg_rx_prbs31_enable`）。对比 `eth_mac_10g` 那一大片 `cfg_mcf_*`/`stat_tx_*`/`tx_lfc_*`/`tx_pfc_*` 端口（[rtl/eth_mac_10g.v:96-184](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_10g.v#L96-L184)），合一模块的控制面明显精简。

发送子模块把成帧与编码合并：

[rtl/eth_mac_phy_10g_tx.v:117-169](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_phy_10g_tx.v#L117-L169) —— 例化 `axis_baser_tx_64`（吃 AXI、吐已编码 64b/66b 块、并处理 PAD/DIC/PTP 时间戳），再接 `eth_phy_10g_tx_if`（扰码→SERDES）。中间只有 `encoded_tx_data/hdr` 两根线，**全程没有 XGMII**。

接收子模块对称：

[rtl/eth_mac_phy_10g_rx.v:117-169](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_phy_10g_rx.v#L117-L169) —— 先 `eth_phy_10g_rx_if`（SERDES→解扰/对齐→已编码块），再 `axis_baser_rx_64`（解码+成帧→AXI）。

佐证：`tb/eth_mac_phy_10g/Makefile` 的源文件清单里列出了 `axis_baser_rx_64.v`/`axis_baser_tx_64.v` 和 `eth_phy_10g_*_if.v`，但**没有** `eth_mac_10g.v`、`eth_phy_10g.v`、`axis_xgmii_*_64.v`、`xgmii_baser_enc_64.v`——说明合一模块根本不依赖 XGMII 那条路径。

| 对比项 | 分立 `eth_mac_10g` + `eth_phy_10g` | 合一 `eth_mac_phy_10g` |
| --- | --- | --- |
| MAC↔PHY 内部接口 | XGMII，144 位（`txd/txc/rxd/rxc`） | 无（AXI 直接进 `axis_baser_*`） |
| 成帧模块 | `axis_xgmii_tx_64`（在 MAC 内） | `axis_baser_tx_64`（成帧+编码合并） |
| 64b/66b 编码 | `xgmii_baser_enc_64`（在 PHY 内） | 同上，合并进 `axis_baser_tx_64` |
| PAUSE / PFC 流控 | 有（`mac_ctrl_*`/`mac_pause_ctrl_*`） | **无** |
| PTP 时间戳 | 有 | 有（参数同构） |
| 对外数据接口 | AXI ↔ XGMII ↔ SERDES | AXI ↔ SERDES |

#### 4.4.4 代码实践（本讲核心实践）

**目标**：通过端口与源文件清单的对比，亲手验证「合一模块如何减少对外接口、简化 SERDES 对接」，并理解其代价。

**操作步骤**：

1. 打开 `rtl/eth_mac_phy_10g.v` 与 `rtl/eth_mac_10g.v`，把两者的端口逐段对照，分别统计：
   - 数据面端口：合一模块的 AXI + SERDES；分立 MAC 的 AXI + XGMII。
   - 控制面端口：列出 `eth_mac_10g` 独有的流控端口（`tx_lfc_req`、`tx_pfc_req`、`cfg_mcf_*`、`stat_tx_pfc_*` 等）。
2. 打开 `tb/eth_mac_phy_10g/Makefile`，核对 `VERILOG_SOURCES`——确认它依赖 `axis_baser_*` 与 `eth_phy_10g_*_if`，而**不**依赖 `eth_mac_10g`/`eth_phy_10g`/`axis_xgmii_*`。
3. 在 `tb/eth_mac_phy_10g/` 运行 `make`（前置：装好 cocotb + iverilog，见 u1-l4）。这个 testbench 用 `BaseRSerdesSource`/`BaseRSerdesSink` 直接驱动 SERDES 引脚、用 AXI 源/宿驱动应用侧，端到端验证 AXI↔SERDES。

**需要观察的现象**：

- 合一模块端口里**搜不到**任何 `xgmii` 字样的信号；分立 MAC 必须额外接一个 PHY 才能到 SERDES。
- 合一模块端口里**搜不到** `lfc`/`pfc`/`mcf` 字样的信号——流控被整体移除。
- 仿真中 AXI 侧发一帧，SERDES 侧（经 `BaseRSerdesSink` 解扰解码）能收到对应帧；反之亦然。

**预期结果**：你会得出结论——合一模块把 144 位的 XGMII 内部接口和整套流控配置面都省掉了，对外只暴露 AXI + 极简 SERDES + 少量 cfg/status；代价是失去 PAUSE/PFC。这正是「在不需要流量控制、追求接口最简的场合（如纯数据通路）」选用 `eth_mac_phy_10g` 的理由；需要无损以太网/PAUSE 时则回到分立的 `eth_mac_10g`。

> 待本地验证：仿真是否通过以你本地工具链为准；端口清单的对比不依赖仿真，可直接从源码完成。

#### 4.4.5 小练习与答案

**练习 1**：假如你的 10G 设计需要支持 PFC（用于 RoCE 无损网络），应该选 `eth_mac_phy_10g` 还是 `eth_mac_10g` + `eth_phy_10g`？为什么？

**参考答案**：必须选分立的 `eth_mac_10g` + `eth_phy_10g`。因为 `eth_mac_phy_10g` 完全没有例化 `mac_ctrl_*`/`mac_pause_ctrl_*`，其端口里不存在任何 PFC/PAUSE 信号，无法收发优先级流量控制帧。只有 `eth_mac_10g` 在 `PFC_ENABLE=1` 时才会综合出流控子模块（见 u4-l2、u9-l2）。

**练习 2**：既然合一模块省掉了 XGMII，为什么库仍然保留 `eth_mac_10g` 和 `eth_phy_10g` 两个分立模块？

**参考答案**：分立设计提供灵活性与功能完整性。其一，有些场合 MAC 需要对接**非 BASE-R** 的外部 PHY（例如 XLAUI/背板 PHY 或厂商提供的 XGMII PHY），这时只需 `eth_mac_10g` 的 XGMII 输出。其二，分立 MAC 才支持完整 PAUSE/PFC。其三，分立便于在 MAC 与 PHY 之间插入自定义逻辑（如时间戳注入点、抓包）。合一模块是「接口最简」的特化路径，分立模块是「功能完整」的通用路径，二者互补。

**练习 3**：合一模块接收侧的 `serdes_rx_bitslip`/`serdes_rx_reset_req` 与分立 `eth_phy_10g` 的同名信号行为一致吗？

**参考答案**：一致。合一模块的接收子模块 `eth_mac_phy_10g_rx` 内部例化的就是 `eth_phy_10g_rx_if`，其 frame_sync/watchdog/BER 逻辑与分立 `eth_phy_10g` 完全相同（u10-l2）。合一只是替换了「成帧 + 编解码」的实现（用 `axis_baser_*` 合并），PCS 的对齐与监测逻辑原封不动。

---

## 5. 综合实践

把本讲的三条主线串起来，完成一次「自底向上」的源码追踪与方案选型。

**任务**：给定一个需求——在 FPGA 上实现一个 10G UDP 数据通路，不需要 PAUSE/PFC 流量控制，希望对外接口尽量简单。请完成以下分析与验证。

1. **选型**：在 `eth_mac_phy_10g` 与 `eth_mac_10g` + `eth_phy_10g` 之间选择，并用本讲的「接口宽度 + 功能代价」论证你的选择。（提示：选 `eth_mac_phy_10g`，因为它消除了 144 位 XGMII、无流控恰好满足需求。）
2. **追踪发送通路**：从 `eth_mac_phy_10g` 顶层出发，沿 `tx_axis_*` → `eth_mac_phy_10g_tx` → `axis_baser_tx_64` → `eth_phy_10g_tx_if` → `serdes_tx_*`，画出每一级模块名、它做的事、以及级间信号名（如 `encoded_tx_data`）。确认途中**没有** XGMII。
3. **对比扰码行为**：分别说明正常模式下 `serdes_tx_data` 的来源（扰码后的负载）与 PRBS31 测试模式下的来源（自由运行 PRBS31 序列取反），并指出同步头 `serdes_tx_hdr` 在两种模式下分别是什么。
4. **仿真验证**：进入 `tb/eth_mac_phy_10g/` 运行 `make`，确认 AXI↔SERDES 端到端测试通过；再用 `WAVES=1 make` 看一次波形，在发送方向上确认 `serdes_tx_hdr == encoded_tx_hdr`（明文）而 `serdes_tx_data != encoded_tx_data`（已扰码）。

**完成标志**：你能不看资料说出「合一模块省掉了什么、保留了什么、为什么」，并能画出从 AXI 到 SERDES 的完整发送数据通路图。

> 待本地验证：步骤 4 的仿真结果依赖本地工具链；步骤 1-3 为纯源码分析，可直接完成。

## 6. 本讲小结

- **发送链路**分两段：`xgmii_baser_enc_64` 把 XGMII 编码成 64b/66b 块，`eth_phy_10g_tx_if` 再扰码送 SERDES；`eth_phy_10g_tx` 是把它们串起来的薄布线层。
- **扰码只动 64 位负载、不动 2 位同步头**；扰码器是自同步的（明文数据反馈驱动状态，多项式 \(x^{58}+x^{39}+1\)），PRBS31 生成器才是自由运行的自激 LFSR，两者由 `cfg_tx_prbs31_enable` 运行时二选一。
- **SERDES 接口契约**：64 位数据 + 2 位同步头，外加接收侧的 `bitslip`/`reset_req` 控制线；`BIT_REVERSE` 适配位序、`SERDES_PIPELINE` 留时序余量。
- **`eth_phy_10g`** 把发送与接收拼成完整 PCS/PMA PHY，双时钟域（`rx_clk`/`tx_clk`），对外暴露 XGMII、SERDES 与全套状态/配置。
- **`eth_mac_phy_10g` 不是把 `eth_mac_10g` 与 `eth_phy_10g` 连起来**，而是改用 `axis_baser_*` 把「成帧 + 64b/66b 编解码」合并进单模块，从而**消除 144 位 XGMII 内部接口**、大幅简化对外端口。
- **取舍**：合一模块换来了最简的 AXI↔SERDES 接口，代价是**移除了 PAUSE/PFC 流量控制**；需要流控或非 BASE-R 外部 PHY 时仍应使用分立的 `eth_mac_10g` + `eth_phy_10g`。

## 7. 下一步学习建议

- 若关心 **PTP 精确时间同步**：本讲的 `eth_mac_phy_10g` 与 `eth_mac_10g` 都已暴露 `PTP_TS_ENABLE`/`tx_axis_ptp_ts` 等接口，下一单元 u11（PTP）将讲解 `ptp_clock` 如何产生时间戳、时间戳如何经 `tuser` 旁带进 MAC。建议先读 u11-l1（ptp_clock）与 u11-l3（PTP 时间戳标记与 MAC 集成）。
- 若关心 **跨时钟域与 FIFO 集成**：`eth_phy_10g` 与 `eth_mac_phy_10g` 都是双时钟域、接收方向线速不可反压；要桥接到统一逻辑时钟域并加缓冲，请阅读 `eth_mac_phy_10g_fifo`（带 `_fifo` 后缀的变体）并复习 u5-l1（MAC FIFO 与异步 CDC）。
- 若要**为新 10G 模块写仿真**：参照 `tb/eth_phy_10g/` 与 `tb/eth_mac_phy_10g/` 的三件套（Makefile + test_*.py + 本地 `baser.py` 的 `BaseRSerdesSource/Sink`），方法学见 u13（测试方法学）。
- 继任项目 **taxi** 把本库的 MAC/PHY 重组为更模块化的包结构，接口理念与本讲一致，可作为进阶对照阅读。
