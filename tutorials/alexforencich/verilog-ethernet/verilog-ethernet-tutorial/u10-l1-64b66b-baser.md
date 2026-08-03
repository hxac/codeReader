# 64b/66b 编解码与 BASE-R

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 64b/66b 编码的块结构：2 位同步头 + 64 位负载，以及「数据块」与「控制块」的区别。
- 看懂 10GBASE-R 的 16 种块类型码（block type），并能解释 XGMII 控制字符（IDLE / START / TERM / ERROR / 序列有序集等）是如何被映射进控制块的。
- 理解发送侧自同步扰码器（x^58 + x^39 + 1）、接收侧解扰器的工作原理，以及为什么同步头不参与扰码、块对齐发生在哪一层。
- 分清本库提供的两套 BASE-R 接口：面向 XGMII MAC 的 `xgmii_baser_enc_64`/`dec_64`，与面向 AXI-Stream、自带成帧的 `axis_baser_tx_64`/`rx_64`。
- 能把编码器与解码器背靠背连接，跑通一个含控制字符的块序列往返（round-trip）验证。

## 2. 前置知识

本讲是专家层（advanced），需要你已经掌握：

- **AXI-Stream 与 XGMII 接口约定**（见 u1-l3、u9-l1）。XGMII 是 10G 的 MAC↔PHY 标准接口，每个 lane 有 8 位数据 `rxd` 加 1 位控制标志 `rxc`；当 `rxc=1` 时该 lane 传的是控制字符（如 `IDLE=0x07`、`START=0xfb`、`TERM=0xfd`、`ERROR=0xfe`）。
- **LFSR / CRC 引擎**（见 u2-l1）。本讲的扰码器本质就是一个用 `lfsr.v` 实现的 Fibonacci 线性反馈移位寄存器，理解 u2-l1 的 `state_in/state_out`、`LFSR_CONFIG`、`REVERSE` 等参数是基础。
- **FCS / CRC-32**（见 u2-l2、u9-l1）。`axis_baser_rx_64` 用「魔数残留法」一次性校验 FCS，与 64 位 MAC 接收侧同源。

几个关键术语先建立直觉：

- **PCS / PMA**：以太网 PHY 分两层。PCS（Physical Coding Sublayer，物理编码子层）负责 64b/66b 编解码、扰码、块对齐；PMA（Physical Medium Attachment）负责串化/解串、对接串行收发器（serdes）。本讲讨论的全是 PCS 的事。
- **10GBASE-R**：10G 以太网在 PCS 层使用 64b/66b 编码的统称（R = 64b/66b 编码家族，如 10GBASE-R、40GBASE-R）。
- **块（block）**：64b/66b 的基本传输单位，每「拍」66 比特。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [rtl/xgmii_baser_enc_64.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/xgmii_baser_enc_64.v) | XGMII → 64b/66b 编码器。把 64 位 XGMII 数据+控制翻译成一个 64b/66b 块（数据块或控制块）。 |
| [rtl/xgmii_baser_dec_64.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/xgmii_baser_dec_64.v) | 64b/66b → XGMII 解码器，编码器的逆运算，并检测坏块与序列错误。 |
| [rtl/axis_baser_tx_64.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_baser_tx_64.v) | AXI-Stream → 64b/66b 发送器。把成帧（前导码/SFD、FCS、IFG、DIC）与 64b/66b 编码「内联」在一个模块里，不调用上面的 enc。 |
| [rtl/axis_baser_rx_64.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_baser_rx_64.v) | 64b/66b → AXI-Stream 接收器，内联解码+拆帧+FCS 校验。 |
| [rtl/eth_phy_10g_tx_if.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_tx_if.v) | 发送侧扰码器（PCS→PMA 之间），基于 `lfsr` 的 x^58+x^39+1。 |
| [rtl/eth_phy_10g_rx_if.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx_if.v) | 接收侧解扰器，扰码器的逆运算（feed-forward 自同步）。 |
| [rtl/lfsr.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/lfsr.v) | 通用并行 LFSR/CRC 引擎，扰码器的底层（u2-l1 已精读）。 |

> 阅读提示：`xgmii_baser_enc/dec_64` 与 `axis_baser_tx/rx_64` 这两组模块**都做 64b/66b 编解码**，但定位不同（见 4.1.3）。`enc/dec` 是「纯编解码器」，`axis_baser_tx/rx` 是「编解码 + MAC 成帧合一」。理解时要分清它们各自被哪个顶层调用：`eth_phy_10g`（带 XGMII 接口的 PCS）用前者，`eth_mac_phy_10g`（MAC+PHY 合一）用后者。

## 4. 核心概念与源码讲解

### 4.1 64b/66b 编码基础：同步头与块结构

#### 4.1.1 概念说明

千兆以太网用 8b/10b 编码，开销高达 25%（每 8 位数据编成 10 位线路码），而且跑 10G/25G 时直流平衡与游程约束会带来不小的代价。64b/66b 编码用一个更聪明的办法：**每 64 位负载只加 2 位同步头，开销仅 2/66 ≈ 3.125%**，但代价是需要靠扰码来保证线路信号的统计随机性（直流平衡由扰码器近似保证，而非编码本身）。

一个 64b/66b「块」的物理结构是：

\[ \underbrace{2\text{ bit 同步头 (sync header)}}_{\text{不扰码}} \;+\; \underbrace{64\text{ bit 负载 (payload)}}_{\text{扰码}} \]

同步头只有两种合法取值（在本库中定义为 [xgmii_baser_enc_64.v:110-112](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/xgmii_baser_enc_64.v#L110-L112)）：

| 同步头 | 含义 | 本库 localparam |
|---|---|---|
| `2'b10` | **数据块**：64 位负载全是数据 | `SYNC_DATA` |
| `2'b01` | **控制块**：64 位负载含块类型 + 控制字符/数据混合 | `SYNC_CTRL` |
| `2'b00` / `2'b11` | 非法（接收侧报坏块） | — |

关键设计点：**同步头不参与扰码**（扰码只作用于 64 位负载）。正因为如此，接收端才能靠每 66 位出现一次的、固定的同步头跳变（`01` 或 `10`）来锁定块边界——这就是「块对齐」得以实现的物理基础（4.3 再讲）。

为什么要区分数据块和控制块？因为以太网线路上除了用户数据，还必须传空闲字符（IDLE，帧间填充）、帧定界符（START/TERM）、错误标记（ERROR）、以及用于链路自检的有序集（ordered set，如 Sequence）。这些控制信息只能塞进控制块。

#### 4.1.2 核心流程

编码器 `xgmii_baser_enc_64` 每个时钟把一拍 64 位 XGMII 数据（`xgmii_txd`，8 个 lane 各 8 位）连同 8 位控制标志（`xgmii_txc`）翻译成一个 64b/66b 块：

```
每拍：
  if (xgmii_txc == 8'h00)：
      # 8 个 lane 全是数据 → 数据块
      encoded_data = xgmii_txd          # 负载原样
      encoded_hdr  = SYNC_DATA (2'b10)
  else：
      # 至少一个 lane 是控制字符 → 控制块
      # 先把每个控制 lane 的 XGMII 字节翻译成 7 位控制码 encoded_ctrl[]
      # 再根据「控制字符出现在哪些 lane」选择一种块类型
      # 按块类型把 encoded_ctrl / 数据字节 / 有序集码打包成 64 位负载
      encoded_hdr  = SYNC_CTRL (2'b01)
  寄存一拍输出
```

解码器 `xgmii_baser_dec_64` 是逆过程：看同步头，数据块直接还原 8 字节数据；控制块则按块类型把 64 位负载重新拆成 XGMII 数据+控制。

#### 4.1.3 两套 BASE-R 接口：enc/dec vs axis_baser

在精读源码前，先理解本库为什么有两套模块。这关系到你在系统里怎么接线。

**第一套：`xgmii_baser_enc_64` / `xgmii_baser_dec_64`（纯编解码器）**
- 输入/输出是 XGMII 接口（`xgmii_txd`/`xgmii_txc`）和 64b/66b 编码接口（`encoded_tx_data`/`encoded_tx_hdr`）。
- 只做「XGMII 控制字符 ↔ 64b/66b 块」的翻译，**不碰成帧、不碰 FCS、不碰扰码**。
- 被 `eth_phy_10g`（PCS/PMA PHY，对外暴露 XGMII 给独立的 MAC）调用。也就是说：系统里已经有一个独立的 XGMII MAC（如 `eth_mac_10g`），PHY 只负责把 XGMII 翻译成串行比特流。

**第二套：`axis_baser_tx_64` / `axis_baser_rx_64`（成帧+编解码合一）**
- 输入/输出是 AXI-Stream（`tdata`/`tkeep`/`tvalid`/...）和 64b/66b 编码接口。
- 把 MAC 成帧（前导码/SFD 插入、FCS 计算、最小帧长填充、IFG、DIC）**和** 64b/66b 编码**合并进同一个模块**，内部直接用 `case(output_type_reg)` 生成块（不再例化 enc）。
- 被 `eth_mac_phy_10g`（MAC+PHY 合一的顶层）调用，适合想从 AXI-Stream 一步到串行比特流的场景。

可以验证这一点（grep 结果）：

- [rtl/eth_phy_10g_tx.v:92](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_tx.v#L92) 例化 `xgmii_baser_enc_64`；
- [rtl/eth_mac_phy_10g_tx.v:117](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_phy_10g_tx.v#L117) 例化 `axis_baser_tx_64`。

本讲的 4.1、4.2 以 enc/dec 为主线讲清 64b/66b 的编码规则（它们最「纯粹」，没有成帧干扰），4.2 末尾再点出 axis_baser 是如何把同样规则内联进去的。

#### 4.1.4 源码精读

**数据块的判定**。编码器用最外层的 `if (xgmii_txc == 8'h00)` 判定数据块——只要 8 个 lane 的控制标志全为 0，就是纯数据，直接把 64 位数据原样塞进负载、同步头置 `SYNC_DATA`：

[xgmii_baser_enc_64.v:201-204](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/xgmii_baser_enc_64.v#L201-L204) —— 数据块分支，负载原样、同步头 `SYNC_DATA`。

否则进入控制块分支，最后统一把同步头置为 `SYNC_CTRL`：

[xgmii_baser_enc_64.v:271](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/xgmii_baser_enc_64.v#L271) —— 控制块同步头赋值。

**解码侧的同步头判定**。解码器对称地用同步头最低位分流，再用块类型（高 4 位，用于降低扇入）选择解码方式：

[xgmii_baser_dec_64.v:203-208](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/xgmii_baser_dec_64.v#L203-L208) —— 同步头分流：`hdr[0]==0`（即 `10`）走数据块、否则按块类型码走控制块。

注意解码器还做了**双重校验**：先在 [xgmii_baser_dec_64.v:369-399](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/xgmii_baser_dec_64.v#L369-L399) 用完整的 8 位块类型对照表（`case (encoded_rx_data[7:0])`）复核，命中未知块类型或非法同步头（`00`/`11`）都会把输出全置为 `XGMII_ERROR` 并拉高 `rx_bad_block`。这是为了把「降低扇入用的高 4 位快速译码」可能漏掉的错误兜住。

**axis_baser_tx_64 内联编码**。在合一模块里，同步头和数据/控制块的判定被直接写进输出多路选择：[axis_baser_tx_64.v:699-702](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_baser_tx_64.v#L699-L702) 的 `OUTPUT_TYPE_DATA` 分支输出数据块（`SYNC_DATA`），其余 `OUTPUT_TYPE_*` 分支输出控制块（`SYNC_CTRL`）。

#### 4.1.5 代码实践

**实践目标**：确认数据块的判定条件与输出格式。

**操作步骤**（源码阅读型）：
1. 打开 [rtl/xgmii_baser_enc_64.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/xgmii_baser_enc_64.v)。
2. 找到 `if (xgmii_txc == 8'h00)` 分支（第 201 行），确认数据块下 `encoded_tx_data_next = xgmii_txd`、`encoded_tx_hdr_next = SYNC_DATA`。
3. 假设输入 `xgmii_txd = 64'h0706050403020100`、`xgmii_txc = 8'h00`。

**需要观察的现象 / 预期结果**：一拍后 `encoded_tx_data` 应等于 `0x0706050403020100`，`encoded_tx_hdr` 应等于 `2'b10`，`tx_bad_block` 应为 0。

> 待本地验证：你可以在第 5 节的综合实践 testbench 里临时把输入设成纯数据拍，用 `$display` 打印这三项核对。

#### 4.1.6 小练习与答案

**练习 1**：为什么同步头不能也参与扰码？
**答案**：接收端必须先靠同步头锁定块边界（每 66 位一次的 `01`/`10` 跳变是对齐的唯一锚点）。若同步头被扰码，它的统计特性会被破坏，块对齐就无法实现；而且扰码后的负载本就需要先对齐才能正确解扰，二者构成鸡生蛋问题，所以标准规定同步头明文传输。

**练习 2**：64b/66b 相对 8b/10b 的线路开销各是多少？
**答案**：64b/66b 开销为 \(2/66 \approx 3.125\%\)；8b/10b 开销为 \(2/10 = 20\%\)（每 8 位数据编为 10 位，多出 2 位）。所以同样数据率下 64b/66b 需要的线路带宽显著更低。

---

### 4.2 控制块类型与 XGMII 控制字符映射

#### 4.2.1 概念说明

控制块要在一个 64 位负载里同时表达「控制字符出现在哪几个 lane、是什么字符、有没有数据、有没有有序集」这许多信息。64b/66b 的做法是：**把负载的最低字节（byte 0，bit 7:0）固定用作块类型（block type）**，剩余 56 位按块类型决定的格式，打包 7 位控制码（C）、8 位数据字节（D）或 4 位有序集类型码（O）。

本库定义了全部 16 种块类型（[xgmii_baser_enc_64.v:114-129](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/xgmii_baser_enc_64.v#L114-L129)）。注释里的记号从最高字节（byte 7）写到 byte 0（块类型 BT）：

| 块类型 | 码值 | 负载布局（byte7 … byte1, byte0=BT） | 含义 |
|---|---|---|---|
| `BLOCK_TYPE_CTRL` | 0x1e | C7 C6 C5 C4 C3 C2 C1 C0 **BT** | 8 个控制字符（如全 IDLE） |
| `BLOCK_TYPE_OS_4` | 0x2d | D7 D6 D5 O4 C3 C2 C1 C0 **BT** | 有序集在 lane4，lane0-3 控制 |
| `BLOCK_TYPE_START_4` | 0x33 | D7 D6 D5 -- C3 C2 C1 C0 **BT** | 帧起始在 lane4 |
| `BLOCK_TYPE_OS_START` | 0x66 | D7 D6 D5 -- O0 D3 D2 D1 **BT** | 有序集 lane0 + 起始 lane4 |
| `BLOCK_TYPE_OS_04` | 0x55 | D7 D6 D5 O4 O0 D3 D2 D1 **BT** | 双有序集 lane0+lane4 |
| `BLOCK_TYPE_START_0` | 0x78 | D7 D6 D5 D4 D3 D2 D1 -- **BT** | 帧起始在 lane0 |
| `BLOCK_TYPE_OS_0` | 0x4b | C7 C6 C5 C4 O0 D3 D2 D1 **BT** | 有序集在 lane0 |
| `BLOCK_TYPE_TERM_0` | 0x87 | C7 C6 C5 C4 C3 C2 C1 -- **BT** | 帧终止在 lane0 |
| `BLOCK_TYPE_TERM_1` | 0x99 | C7 C6 C5 C4 C3 C2 -- D0 **BT** | 帧终止在 lane1 |
| `BLOCK_TYPE_TERM_2` | 0xaa | C7 C6 C5 C4 C3 -- D1 D0 **BT** | 帧终止在 lane2 |
| `BLOCK_TYPE_TERM_3` | 0xb4 | C7 C6 C5 C4 -- D2 D1 D0 **BT** | 帧终止在 lane3 |
| `BLOCK_TYPE_TERM_4` | 0xcc | C7 C6 C5 -- D3 D2 D1 D0 **BT** | 帧终止在 lane4 |
| `BLOCK_TYPE_TERM_5` | 0xd2 | C7 C6 -- D4 D3 D2 D1 D0 **BT** | 帧终止在 lane5 |
| `BLOCK_TYPE_TERM_6` | 0xe1 | C7 -- D5 D4 D3 D2 D1 D0 **BT** | 帧终止在 lane6 |
| `BLOCK_TYPE_TERM_7` | 0xff | -- D6 D5 D4 D3 D2 D1 D0 **BT** | 帧终止在 lane7 |

其中 `--` 是保留位（填 0）。读这张表的方法：**BT 永远在 byte0；START 表示一个帧从该 lane 开始（其后是数据）；TERM 表示一个帧在该 lane 结束（TERM 之前是该帧最后几个数据字节，TERM 之后是 IDLE 控制码）**。`TERM_n` 里的 `n` 正是终止字符所在的 lane 号——8 个 TERM 类型覆盖了帧可能在 8 个 lane 中任意一个结束的全部情况。

而每个「C」（控制码）是 7 位，由 XGMII 控制字节查表得到（[xgmii_baser_enc_64.v:95-104](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/xgmii_baser_enc_64.v#L95-L104)）：

| XGMII 控制字节 | 含义 | 7 位控制码 |
|---|---|---|
| 0x07 | IDLE | 0x00 (`CTRL_IDLE`) |
| 0x06 | LPI（低功耗） | 0x06 (`CTRL_LPI`) |
| 0xfe | ERROR | 0x1e (`CTRL_ERROR`) |
| 0x1c | Reserved 0 | 0x2d (`CTRL_RES_0`) |
| 0x3c | Reserved 1 | 0x33 (`CTRL_RES_1`) |
| 0x7c | Reserved 2 | 0x4b (`CTRL_RES_2`) |
| 0xbc | Reserved 3 | 0x55 (`CTRL_RES_3`) |
| 0xdc | Reserved 4 | 0x66 (`CTRL_RES_4`) |
| 0xf7 | Reserved 5 | 0x78 (`CTRL_RES_5`) |

有序集则用一个 4 位类型码 `O` 区分（[xgmii_baser_enc_64.v:107-108](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/xgmii_baser_enc_64.v#L107-L108)）：`O_SEQ_OS=0x0`（序列有序集，对应 XGMII 的 `0x9c`）、`O_SIG_OS=0xf`（信号有序集，对应 `0x5c`）。注意 4 位 `O` 只占半个字节，所以一个 56 位区里能同时塞下两个有序集（如 `OS_04`）。

#### 4.2.2 核心流程

**编码（XGMII → 块）**分两步：

1. **逐 lane 翻译控制码**：遍历 8 个 lane，凡 `xgmii_txc[i]==1` 的，把该 lane 的 XGMII 控制字节查表成 7 位码塞进 `encoded_ctrl[7*i+:7]`；同时记下「非法控制字节」标志 `encode_err[i]`（数据字节出现在控制位、或遇到未定义控制码时置 1）。
2. **按模式选块类型并打包**：用一长串 `if-else` 匹配 `(xgmii_txc, xgmii_txd)` 的具体模式，选出对应的块类型，并把 `encoded_ctrl`/数据字节/有序集码按该块类型的布局拼成 64 位。匹配不到任何合法模式时，输出全 `CTRL_ERROR` 控制块并拉高 `tx_bad_block`。

**解码（块 → XGMII）**是对称的逆运算，额外维护一个 `frame_reg`：每见到 START 块置 1、见到 TERM 块清 0，用以检测序列错误（如连续两个 START、或未开始帧就收到 TERM），出错时拉高 `rx_sequence_error`。

#### 4.2.3 源码精读

**逐 lane 控制码翻译**。编码器用一个 `for` 循环把每个控制 lane 的 XGMII 字节映射成 7 位码（IDLE/LPI/ERROR/RES_0..5），非法则映射成 `CTRL_ERROR` 并置 `encode_err[i]`：

[xgmii_baser_enc_64.v:149-199](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/xgmii_baser_enc_64.v#L149-L199) —— 控制字符逐 lane 翻译与错误检测。注意第 194-198 行：当 `xgmii_txc[i]==0`（数据 lane）但整体被判为控制块时，也记为 `encode_err`，因为数据字节本不该出现在控制位置。

**帧终止在 lane 7 的编码**。这是最「干净」的终止情形——TERM 之前是 7 个数据字节（D0..D6），TERM 之后没有 IDLE，所以负载就是「D6 D5 D4 D3 D2 D1 D0 BT」：

[xgmii_baser_enc_64.v:258-261](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/xgmii_baser_enc_64.v#L258-L261) —— `BLOCK_TYPE_TERM_7` 分支：`{xgmii_txd[55:0], BLOCK_TYPE_TERM_7}`，7 字节数据 + 块类型。

**全控制块（典型如全 IDLE）**。当 8 个 lane 全是控制字符（`xgmii_txc == 8'hff`）时，负载就是 8 个 7 位控制码 + 块类型 `0x1e`，这是链路空闲时最常出现的块：

[xgmii_baser_enc_64.v:262-265](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/xgmii_baser_enc_64.v#L262-L265) —— `BLOCK_TYPE_CTRL` 分支：`{encoded_ctrl, BLOCK_TYPE_CTRL}`。

**解码侧的终止 lane 0**。解码器从 `BLOCK_TYPE_TERM_0` 还原出：lane0 是 TERM，lane1-7 是 IDLE 控制码（来自 `decoded_ctrl`），同时清 `frame_reg` 并检测「未开始帧却收到终止」的序列错误：

[xgmii_baser_dec_64.v:295-302](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/xgmii_baser_dec_64.v#L295-L302) —— `BLOCK_TYPE_TERM_0` 解码：`{decoded_ctrl[63:8], XGMII_TERM}`、`xgmii_rxc=8'hff`、`rx_sequence_error_next = !frame_reg`。

**axis_baser_tx_64 内联同样的块类型表**。合一发送器在 `case (output_type_reg)` 里直接生成每种块，例如终止 lane 7（`OUTPUT_TYPE_TERM_7`）：

[axis_baser_tx_64.v:731-734](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_baser_tx_64.v#L731-L734) —— 与 enc 的 `TERM_7` 完全一致的打包方式 `{output_data_reg[55:0], BLOCK_TYPE_TERM_7}`。可见两套模块用的是同一张块类型表，只是 axis_baser 把成帧逻辑和编解码耦合在了一起。

它还在内部例化了 **8 个并行 CRC 引擎**（应对末拍可能 1~8 字节任意宽度），用 `generate` 展开，每个引擎处理不同字节宽度：

[axis_baser_tx_64.v:244-265](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_baser_tx_64.v#L244-L265) —— 8 个 `lfsr`（CRC-32）实例，`DATA_WIDTH` 从 8 到 64，共享 `crc_state_reg[7]` 状态。

#### 4.2.4 代码实践

**实践目标**：手工跟踪一个「帧终止在 lane 7」的编码过程。

**操作步骤**（源码阅读 + 手算）：
1. 假设当前拍 XGMII 为：lane0..lane6 = 数据字节 `D0..D6`（即 `xgmii_txd[55:0]` 为某已知值），lane7 = `TERM (0xfd)`；对应 `xgmii_txc = 8'b1000_0000 = 0x80`。
2. 在 [xgmii_baser_enc_64.v:258-261](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/xgmii_baser_enc_64.v#L258-L261) 找到匹配分支 `xgmii_txc == 8'h80 && xgmii_txd[63:56] == XGMII_TERM`。
3. 写出 `encoded_tx_data_next = {xgmii_txd[55:0], BLOCK_TYPE_TERM_7}`。

**需要观察的现象 / 预期结果**：负载低字节为 `0xff`（`BLOCK_TYPE_TERM_7`），高 56 位是 7 个数据字节；同步头 `SYNC_CTRL`；`tx_bad_block` 为 0（因为 TERM 之前没有需要校验的控制 lane）。

**练习延伸**：把同样的输入喂给 [xgmii_baser_dec_64.v:351-358](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/xgmii_baser_dec_64.v#L351-L358) 的 `TERM_7` 分支，应当还原出 `xgmii_rxd[63:56]=TERM`、`xgmii_rxc=8'h80`，与原始输入一致。

> 待本地验证：可在综合实践的 testbench 里构造此拍核对。

#### 4.2.5 小练习与答案

**练习 1**：为什么需要 `TERM_0` 到 `TERM_7` 共 8 种终止块类型？
**答案**：10G MAC 每拍传 8 字节，一帧的最后一个数据字节可能落在 8 个 lane 中的任意一个。终止字符紧跟其后，因此终止字符本身也可能落在任意 lane。8 种 TERM 类型分别声明「TERM 在 lane n」，让接收端知道 TERM 之后的 lane 填 IDLE、之前的 lane 是数据，从而精确恢复帧尾位置与 `tkeep`。

**练习 2**：编码器的 `encode_err` 与 `tx_bad_block` 是什么关系？
**答案**：`encode_err[i]` 是逐 lane 的非法标志（该 lane 是控制位但字节非法、或控制块里出现了数据字节）。不同块类型在打包时会检查**相关 lane 子集**的 `encode_err`（例如 `TERM_7` 检查无、`CTRL` 检查全部 8 个），若任一相关 lane 非法则 `tx_bad_block` 拉高一拍，向上层报告当前块无法正确编码。

**练习 3**：链路完全空闲（持续发 IDLE）时，线上传的是哪种块？
**答案**：`BLOCK_TYPE_CTRL`（0x1e）控制块，8 个 lane 全是 `CTRL_IDLE`（0x00），同步头 `SYNC_CTRL`。它是空闲态的「carrier」，扰码后看起来像随机比特。

---

### 4.3 扰码与块对齐

#### 4.3.1 概念说明

64b/66b 的 3.125% 低开销是有代价的：编码本身**不保证直流平衡**（8b/10b 靠有限的码字表强制做到直流平衡）。如果数据里出现长串 0 或长串 1，线路上的直流分量会漂移，serdes 的 CDR（时钟数据恢复）会失锁。解决办法是**扰码（scrambling）**：用一个伪随机序列与负载异或，把任何输入都「打散」成统计上近似 50/50 的 0/1 流。

10GBASE-R 采用**自同步扰码器（self-synchronizing scrambler）**，多项式为：

\[ p(x) = x^{58} + x^{39} + 1 \]

「自同步」是它的精髓：

- **发送侧**：LFSR **自由运行**（状态只取决于自身上一拍，与数据无关），输出 `data_out = data_in XOR lfsr`。LFSR 的下一状态由它自己的反馈决定。
- **接收侧**：解扰器是 **feed-forward** 结构，LFSR 的状态**从接收到的（已扰）数据本身**推导。于是收发两端不需要事先约定扰码器初值、不需要任何带内同步信令——只要解扰器收到正确的比特流，它的 LFSR 状态就会自动「追上」发送端。

代价是误码扩散：线路上一比特错误，解扰后会扩散成大约 2 比特错误（因为 `x^58+x^39+1` 有两个抽头）。但以太网有 FCS 兜底，这个代价可以接受。

**关键边界**：

1. **同步头（2 位）不扰码**——否则无法对齐。只有 64 位负载扰码。
2. **块对齐（block alignment）不在 enc/dec 内做**。enc/dec 假设输入的 66 位块已经对齐好了。真正的「从串行比特流里找回 66 位块边界」由 PHY 接收侧的 **frame_sync（块锁定）** 模块完成：它靠统计同步头的合法性（连续若干个 66 位窗口都出现合法的 `01`/`10`）来滑动对齐、声明 `rx_block_lock`。这部分是下一讲 u10-l2 的主题，本讲只点到为止。
3. **扰码/解扰在 `eth_phy_10g_tx_if` / `rx_if` 里做**，不在 enc/dec 或 axis_baser 内。换句话说：enc/dec 和 axis_baser 输出的都是**未扰码**的 64b/66b 块（明文同步头 + 明文负载），扰码是 PCS 最后一道工序，紧贴 serdes。

#### 4.3.2 核心流程

完整发送通路（以 `eth_phy_10g` 为例）：

```
XGMII MAC ──64位 txd/txc──▶ xgmii_baser_enc_64 ──▶ 64b/66b 块(明文)
                                                      │
                                              eth_phy_10g_tx_if
                                                      │ 扰码(只扰64位负载)
                                                      ▼
                                              64b/66b 块(已扰) ──▶ serdes ──▶ 串行线路
```

接收通路反过来：serdes 解串 → `eth_phy_10g_rx_if` 解扰 → `xgmii_baser_dec_64` 解码 → XGMII MAC。块对齐（frame_sync）插在解串与解扰之间。

若是 `eth_mac_phy_10g`（合一方案），则 `axis_baser_tx_64` 一步从 AXI-Stream 生成明文 64b/66b 块，再交给 `tx_if` 扰码。

#### 4.3.3 源码精读

**发送侧扰码器**。`eth_phy_10g_tx_if` 用一个 58 位 Fibonacci LFSR 实现扰码，多项式 `0x8000000001`（bit39 与 bit0 置 1，对应 \(x^{39}\) 与 \(x^0\)，最高次 \(x^{58}\) 由 `LFSR_WIDTH=58` 隐含补回）：

[eth_phy_10g_tx_if.v:135-149](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_tx_if.v#L135-L149) —— 扰码器 `lfsr` 实例：`data_in=encoded_tx_data`（只扰负载）、`state_in=scrambler_state_reg`、`data_out=scrambled_data`、`state_out=scrambler_state`。注意它的 `LFSR_CONFIG="FIBONACCI"`、`REVERSE=1`，且**没有**喂 `encoded_tx_hdr`——同步头不扰码。

随后一个二选一决定是否真的扰码（方便测试与调试）：

[eth_phy_10g_tx_if.v:176-177](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_tx_if.v#L176-L177) —— `SCRAMBLER_DISABLE ? encoded_tx_data : scrambled_data`，同步头始终直通。

**接收侧解扰器**。`eth_phy_10g_rx_if` 用**相同多项式**但 `LFSR_FEED_FORWARD=1` 实现自同步解扰：

[eth_phy_10g_rx_if.v:158-172](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx_if.v#L158-L172) —— 解扰器 `lfsr` 实例：`data_in=serdes_rx_data_int`（用收到的已扰数据驱动 LFSR 状态）、`LFSR_FEED_FORWARD(1)`，输出 `descrambled_rx_data`。这正是「自同步」的实现：解扰器状态从数据流本身重建，无需与发送端约定初值。

**axis_baser_rx_64 的 FCS 校验**。接收侧除了解码块，还要校验 FCS。由于 64 位通路上 FCS 的位置随帧尾 lane 变化，它沿用 64 位 MAC 的「魔数残留法」——整帧（含 FCS）喂入 CRC-32，正确帧的残留应是 8 个固定魔数之一（取决于终止 lane）：

[axis_baser_rx_64.v:207-214](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_baser_rx_64.v#L207-L214) —— 8 个 `crc_valid[k]`，每个比对一个 `~32'h....` 魔数（如 `~32'h2144df1c`），对应终止在 lane0..lane7 之一的正确 FCS 残留。这与 u9-l1 讲过的 64 位 MAC 接收侧同源。

> 说明：扰码/解扰底层就是 u2-l1 精读过的 [rtl/lfsr.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/lfsr.v)。`FIBONACCI` 配置「先反馈后移位」适合做扰码/PRBS；`LFSR_FEED_FORWARD` 让下一状态取自 `data_in` 而非自身反馈，从而实现自同步解扰。

#### 4.3.4 代码实践

**实践目标**：理解扰码的可旁路性，并确认扰码器/解扰器互逆。

**操作步骤**（源码阅读 + 思考）：
1. 读 [eth_phy_10g_tx_if.v:39](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_tx_if.v#L39) 的参数 `SCRAMBLER_DISABLE`，以及 [eth_phy_10g_rx_if.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx_if.v) 中对应的旁路逻辑（`SCRAMBLER_DISABLE ? serdes_rx_data_int : descrambled_rx_data`，见 rx_if 第 207 行附近）。
2. 思考：当 `SCRAMBLER_DISABLE=1` 时，`enc→serdes→dec` 链路上负载保持明文，便于用波形直接观察 64b/66b 块结构。

**需要观察的现象 / 预期结果**：本库的 enc/dec testbench（`tb/xgmii_baser_enc_64`、`tb/xgmii_baser_dec_64`）用的 Python 端 `BaseRSerdesSink`/`Source` 都带 `scramble=False` 参数（见 [tb/xgmii_baser_enc_64/test_xgmii_baser_enc_64.py:61](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/xgmii_baser_enc_64/test_xgmii_baser_enc_64.py#L61)），正是因为单独测 enc/dec 时不该把扰码搅进来。

> 待本地验证：若你想观察扰码效果，可在 `tb/baser.py` 里把 `BaseRSerdesSink(scramble=True)`，对比输出波形里负载是否被「打散」。

#### 4.3.5 小练习与答案

**练习 1**：自同步扰码器为什么「不需要约定初值」？
**答案**：因为接收侧解扰器是 feed-forward 的，它的 LFSR 状态完全由**收到的比特流**决定（`state_out` 由 `data_in` 驱动）。只要线路连通、比特正确，解扰器的状态会自动收敛到与发送端扰码器一致，无需任何带内同步信令或预置初值。

**练习 2**：块对齐为什么必须放在解扰之前？
**答案**：解扰依赖正确的 64 位负载边界（必须知道哪 64 位是一个块才能驱动 LFSR）。而块边界靠同步头（明文、未扰码）来锁定。所以顺序是：解串 → 靠同步头锁定块边界（frame_sync）→ 按块解扰 → 按块解码。对齐错了，解扰和后续解码全错。

**练习 3**：`eth_phy_10g_tx_if` 里还有一个 PRBS31 发生器（[eth_phy_10g_tx_if.v:151-165](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_tx_if.v#L151-L165)），它的作用是什么？
**答案**：用于链路自检。当 `PRBS31_ENABLE` 且 `cfg_tx_prbs31_enable` 有效时，发送侧用 PRBS31 伪随机序列替代正常数据发出，便于对端做误码率测试（BERT），验证 serdes 链路质量。它和扰码器共用 `lfsr` 模块，只是多项式不同（\(x^{31}+x^{28}+1\)，`0x10000001`）。

---

## 5. 综合实践：编码器↔解码器背靠背往返验证

**任务**：把 `xgmii_baser_enc_64` 与 `xgmii_baser_dec_64` 背靠背连接，构造一组含控制字符（IDLE、START、数据、TERM）的 XGMII 拍序列，验证解码还原出的 `xgmii_rxd`/`xgmii_rxc` 与送入的 `xgmii_txd`/`xgmii_txc` 完全一致，且 `rx_bad_block`、`rx_sequence_error` 在合法序列下保持为 0。

**设计思路**：enc 与 dec 各自有一拍寄存（都是 `posedge clk` 后输出），背靠背直连会自然形成 2 拍流水线延时；不需要扰码（明文块直达）。我们写一个最小 testbench 驱动 enc 输入、采 dec 输出。

### 5.1 操作步骤

1. 新建一个仿真目录（本讲只读源码，下面是**示例代码**，供你在自己的沙箱里运行，不要写入本仓库）：

   ```verilog
   // 示例代码：tb_roundtrip.v —— enc/dec 背靠背往返（不写入仓库）
   `timescale 1ns/1ps
   module tb_roundtrip;
       reg clk = 0; reg rst = 1;
       always #3.2 clk = ~clk;            // ~156.25 MHz，对应 10G 64 位通路

       // enc 输入（XGMII）
       reg  [63:0] xgmii_txd; reg [7:0] xgmii_txc;
       wire [63:0] encoded_data; wire [1:0] encoded_hdr; wire tx_bad;

       // dec 输出（XGMII）
       wire [63:0] xgmii_rxd; wire [7:0] xgmii_rxc;
       wire rx_bad, rx_seq_err;

       xgmii_baser_enc_64 enc (
           .clk(clk), .rst(rst),
           .xgmii_txd(xgmii_txd), .xgmii_txc(xgmii_txc),
           .encoded_tx_data(encoded_data), .encoded_tx_hdr(encoded_hdr),
           .tx_bad_block(tx_bad)
       );
       xgmii_baser_dec_64 dec (
           .clk(clk), .rst(rst),
           .encoded_rx_data(encoded_data), .encoded_rx_hdr(encoded_hdr),
           .xgmii_rxd(xgmii_rxd), .xgmii_rxc(xgmii_rxc),
           .rx_bad_block(rx_bad), .rx_sequence_error(rx_seq_err)
       );

       // 驱动一串拍：全IDLE → START lane0 + 7字节数据 → 数据 → TERM lane7
       integer i;
       reg [63:0] exp_q0, exp_q1;   // 2 拍延时对齐用
       reg [7:0]  expc_q0, expc_q1;
       initial begin
           xgmii_txd = 0; xgmii_txc = 0;
           #20 rst = 0;
           // 拍0：全 IDLE 控制块
           @(posedge clk); xgmii_txd <= {8{8'h07}}; xgmii_txc <= 8'hff;
           // 拍1：START 在 lane0 + 7 字节数据
           @(posedge clk); xgmii_txd <= {64'hAA,8'hFB}; xgmii_txc <= 8'h01;
           // 拍2：纯数据
           @(posedge clk); xgmii_txd <= 64'h1122334455667788; xgmii_txc <= 8'h00;
           // 拍3：TERM 在 lane7（前 7 字节是数据）
           @(posedge clk); xgmii_txd <= {8'hFD, 56'h99_88_77_66_55_44_33}; xgmii_txc <= 8'h80;
           // 再补几拍 IDLE
           for (i=0;i<4;i=i+1) begin
               @(posedge clk); xgmii_txd <= {8{8'h07}}; xgmii_txc <= 8'hff;
           end
           @(posedge clk); $finish;
       end

       // 每拍打印 enc 输入与 dec 输出（延时 2 拍）
       always @(posedge clk) begin
           $display("t=%0t in txd=%h txc=%h | enc-> %h hdr=%b | dec rxd=%h rxc=%h bad=%b seqerr=%b",
                    $time, xgmii_txd, xgmii_txc, encoded_data, encoded_hdr,
                    xgmii_rxd, xgmii_rxc, rx_bad, rx_seq_err);
       end
   endmodule
   ```

2. 编译并仿真（iverilog 示例，待本地验证）：

   ```bash
   iverilog -g2012 -o sim.vvp tb_roundtrip.v rtl/xgmii_baser_enc_64.v rtl/xgmii_baser_dec_64.v
   vvp sim.vvp
   ```

### 5.2 需要观察的现象与预期结果

- dec 输出比 enc 输入晚 2 个时钟（enc 1 拍 + dec 1 拍）。
- 把 dec 输出 `xgmii_rxd`/`xgmii_rxc` 平移 2 拍后，应与 enc 输入逐拍相等。
- 合法序列下 `rx_bad_block` 全程为 0；`rx_sequence_error` 也应为 0（START 置帧内、TERM 清帧内，时序合法）。
- 拍0（全 IDLE）对应控制块 `BLOCK_TYPE_CTRL=0x1e`、同步头 `01`；拍3（TERM lane7）对应 `BLOCK_TYPE_TERM_7=0xff`。

### 5.3 进阶（可选）

- **故意制造序列错误**：连续送两个 START 块（中间没有 TERM），观察 `rx_sequence_error` 是否在第二个 START 处拉高（对应 [xgmii_baser_dec_64.v:234](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/xgmii_baser_dec_64.v#L234) `rx_sequence_error_next = frame_reg`）。
- **对照官方 testbench**：本库已为 enc/dec 各自提供了 cocotb 测试（[tb/xgmii_baser_enc_64/](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/xgmii_baser_enc_64/test_xgmii_baser_enc_64.py)、[tb/xgmii_baser_dec_64/](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/xgmii_baser_dec_64/test_xgmii_baser_dec_64.py)），它们用 Python 端的 `BaseRSerdesSink`/`Source`（[tb/baser.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/baser.py)）做软件侧编/解码来对拍。可参照 `run_test_alignment` 用例（[test_xgmii_baser_enc_64.py:104](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/xgmii_baser_enc_64/test_xgmii_baser_enc_64.py#L104)）理解 DIC 与起始 lane 的覆盖测试。

## 6. 本讲小结

- **64b/66b 块结构**：每拍 66 位 = 2 位同步头（不扰码）+ 64 位负载（扰码）。同步头 `10`=数据块、`01`=控制块；开销仅 3.125%。
- **两套接口**：`xgmii_baser_enc/dec_64` 是纯 XGMII↔64b/66b 编解码器（被 `eth_phy_10g` 用）；`axis_baser_tx/rx_64` 把 MAC 成帧与编解码内联合一（被 `eth_mac_phy_10g` 用），内部用同一张 16 种块类型表与 8 个并行 CRC 引擎。
- **控制块映射**：块类型占 byte0，负载按类型打包 7 位控制码（IDLE/LPI/ERROR/RES）、8 位数据、4 位有序集码；`TERM_0..7` 覆盖帧尾落在任意 lane；`START_0/4` 覆盖帧头；`OS_*` 携带序列/信号有序集。
- **自同步扰码**：多项式 \(x^{58}+x^{39}+1\)，发送侧自由运行、接收侧 feed-forward 从数据流自建状态，无需约定初值；误码扩散约 2 倍，由 FCS 兜底。
- **分层边界**：enc/dec 与 axis_baser 都输出**明文**块；扰码/解扰在 `eth_phy_10g_tx_if/rx_if`；块对齐（块锁定）在 `eth_phy_10g_rx_frame_sync`，是 u10-l2 的主题。
- **校验**：编码侧用 `tx_bad_block` 报非法控制字符，解码侧用 `rx_bad_block` 报未知块类型/非法同步头、用 `rx_sequence_error`（靠 `frame_reg`）报 START/TERM 时序错乱。

## 7. 下一步学习建议

- **u10-l2** 将进入 `eth_phy_10g_rx` 的接收链路：重点看 `eth_phy_10g_rx_frame_sync` 如何靠同步头统计实现**块锁定（block lock）**与 bitslip、`eth_phy_10g_rx_ber_mon` 如何做误码率监测。本讲留下的「块对齐在哪里做」的伏笔会在那里收口。
- **u10-l3** 讲 `eth_phy_10g_tx`/`tx_if` 发送链路与 `eth_mac_phy_10g` 合一顶层，把本讲的 enc/dec/axis_baser 放进完整 PCS 数据通路。
- 想巩固扰码/PRBS 的 LFSR 细节，可回看 **u2-l1** 的 `lfsr.v` 精读；想巩固 64 位 FCS 魔数残留法，可回看 **u9-l1** 的 `axis_xgmii_rx_64`。
- 建议阅读：IEEE 802.3 Clause 49（64b/66b 编码与 10GBASE-R PCS 子层），它是本讲所有块类型表与扰码多项式的权威出处。
