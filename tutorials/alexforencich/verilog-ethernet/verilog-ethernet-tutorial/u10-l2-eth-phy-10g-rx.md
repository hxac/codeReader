# eth_phy_10g 接收链路：帧同步与 BER 监测

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 10GBASE-R 接收链路从 SERDES 原始比特流到 XGMII 帧的完整通路，以及 `eth_phy_10g_rx`、`eth_phy_10g_rx_if`、`frame_sync`、`ber_mon`、`watchdog` 各自在其中的职责。
- 解释「块锁定（block lock）」是什么、为什么靠 2 位同步头就能找到块边界、`bitslip` 脉冲何时触发以及它如何驱动 SERDES 重新对齐字边界。
- 掌握 `rx_high_ber` 的判定阈值（125 µs 窗口内 16 个非法同步头）与 BER 监测的窗口机制。
- 理解 `rx_bad_block`、`rx_sequence_error`、`serdes_rx_reset_req`、`rx_status` 这一组状态/错误信号的来源，以及看门狗在「链路已通 / 链路在收 / 链路彻底坏掉需复位」三态之间的迁移逻辑。

## 2. 前置知识

本讲是 u10-l1（64b/66b 编解码与 BASE-R）的直接续篇，承接其中的关键事实：

- **64b/66b 块结构**：每块 = 2 位同步头 + 64 位负载。同步头只有两个合法值：`01`（控制块，本库记作 `SYNC_CTRL = 2'b01`）和 `10`（数据块，`SYNC_DATA = 2'b10`）；`00`/`11` 非法。
- **同步头不参与扰码**：发送侧只对 64 位负载做自同步扰码，2 位同步头保持明文发送。这是接收侧能够「先锁定、再解扰」的根本前提——锁定靠的是裸同步头，根本不需要先解开扰码器。
- **扰码器是自同步的**：多项式 \(x^{58}+x^{39}+1\)，接收侧用 feed-forward LFSR 从数据流本身重建状态，无需约定初值（详见 u10-l1）。

如果你还不熟悉上述概念，请先读 u10-l1。此外本讲用到几个硬件术语：

- **SERDES（Serializer/Deserializer）**：把 FPGA 内部并行数据与线路上的高速串行比特流互转的硬核。它有一个「字边界对齐」机制：上电时它并不知道哪 66 个比特构成一个块，需要靠 `bitslip` 信号让它逐比特滑动，直到对齐。
- **bitslip（位滑移）**：大多数 SERDES 原语提供的控制信号。拉一拍 `bitslip`，SERDES 在后续输出里把字边界整体挪一位。本讲的 `frame_sync` 模块就是靠反复发 `bitslip` 来「试」出正确边界的。
- **BER（Bit Error Rate，误码率）**：线路比特出错的比例。10G 链路正常工作时 BER 极低（如 \(10^{-12}\) 量级）；一旦高到某个阈值，说明链路已不可用，需要告警甚至复位重收。

## 3. 本讲源码地图

本讲涉及 5 个核心文件，全部位于 `rtl/` 下：

| 文件 | 作用 |
|------|------|
| [rtl/eth_phy_10g_rx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx.v) | 接收侧顶层布线层：例化 `rx_if`（处理 SERDES 接口、解扰、监测）与 `xgmii_baser_dec_64`（64b/66b→XGMII 译码），本身几乎无逻辑。 |
| [rtl/eth_phy_10g_rx_if.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx_if.v) | SERDES 接口适配：可选位序反转、可选流水线延时、自同步解扰器、PRBS31 检测器，并例化下面三个监测子模块。 |
| [rtl/eth_phy_10g_rx_frame_sync.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx_frame_sync.v) | **块锁定与位滑移**：靠同步头匹配判定块边界，失锁时发 `bitslip`，锁定后输出 `rx_block_lock`。 |
| [rtl/eth_phy_10g_rx_ber_mon.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx_ber_mon.v) | **BER 监测**：在 125 µs 窗口内统计非法同步头数，超过阈值时拉高 `rx_high_ber`。 |
| [rtl/eth_phy_10g_rx_watchdog.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx_watchdog.v) | **看门狗与状态机**：聚合 `bad_block`/`sequence_error`/`block_lock`/`high_ber`，连续多窗口异常时发 `serdes_rx_reset_req`，连续多窗口正常时拉高 `rx_status`。 |

三个监测子模块有一个共同点：**它们都只看 `serdes_rx_hdr`（2 位裸同步头），不碰负载**。因为同步头是明文，所以在解扰之前就能完成全部对齐与监测工作。

## 4. 核心概念与源码讲解

### 4.1 接收链路总体架构与数据通路

#### 4.1.1 概念说明

10GBASE-R 的接收侧要解决一个「鸡生蛋」的问题：要把 64b/66b 块译码成 XGMII 帧，必须先知道每个块的起点；而要找块起点，只能靠每块开头那 2 位同步头。但 SERDES 上电时字边界是随机的——它可能从一块的中间开始切，于是你看到的「2 位同步头」其实是某块负载里的任意 2 比特，几乎不可能恰好是 `01` 或 `10`。

解决思路分三层，正好对应三个监测子模块：

1. **先对齐字边界（frame_sync）**：逐比特滑动 SERDES 输出，直到连续看到大量合法同步头 → `rx_block_lock`，认定找到了块边界。
2. **持续监测信号质量（ber_mon）**：锁定后若同步头仍频繁出错，说明链路 BER 太高 → `rx_high_ber`。
3. **综合判定链路状态（watchdog）**：若长期异常就复位 SERDES 重收，若长期正常就宣告链路可用 → `rx_status`。

解扰与 64b/66b 译码发生在对齐**之后**，且与监测并行运行。

#### 4.1.2 核心流程

数据通路自上而下（从 SERDES 管脚到 XGMII）：

```
serdes_rx_data[63:0], serdes_rx_hdr[1:0]   (来自 SERDES 硬核，字边界未知)
        │
        ▼
  eth_phy_10g_rx_if
   ├── BIT_REVERSE?  位序反转 (generate)
   ├── SERDES_PIPELINE?  流水线延时 (generate)
   ├── frame_sync  ── serdes_rx_bitslip ──►  SERDES (滑动字边界)
   ├── ber_mon    ── rx_high_ber
   ├── watchdog   ── serdes_rx_reset_req, rx_status
   ├── 解扰器 (LFSR x^58+x^39+1, feed-forward) ──► encoded_rx_data[63:0]
   └── encoded_rx_hdr[1:0] = serdes_rx_hdr (同步头原样透传，不解扰)
        │
        ▼
  xgmii_baser_dec_64  ──► xgmii_rxd[63:0], xgmii_rxc[7:0]
                          rx_bad_block, rx_sequence_error
```

要点：三个监测模块与解扰器**并行**工作，且都吃 `serdes_rx_hdr_int`（位序反转与流水线处理之后、解扰之前的裸同步头）；解扰器只处理 64 位负载，2 位同步头不经解扰直接作为 `encoded_rx_hdr` 送往后级译码器。

#### 4.1.3 源码精读

顶层 `eth_phy_10g_rx` 是纯布线层，只例化两块——`rx_if` 与 `xgmii_baser_dec_64`，并把后者的 `rx_bad_block`/`rx_sequence_error` 反馈给前者（喂给看门狗）：

[rtl/eth_phy_10g_rx.v:L99-L145](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx.v#L99-L145) — `encoded_rx_data/hdr` 是 `rx_if` 解扰后的输出，作为 `xgmii_baser_dec_64` 的输入；`rx_bad_block`/`rx_sequence_error` 由译码器产生、回送给 `rx_if` 内部的 watchdog。

注意顶层的参数中与本讲密切相关者：`BITSLIP_HIGH_CYCLES=1`、`BITSLIP_LOW_CYCLES=8` 控制 bitslip 脉冲的时序；`COUNT_125US=125000/6.4` 是 125 µs 折合的时钟周期数，喂给 `ber_mon` 与 `watchdog`。

`eth_phy_10g_rx_if` 内部，三个监测子模块的例化集中在一处，输入都是同一个 `serdes_rx_hdr_int`：

[rtl/eth_phy_10g_rx_if.v:L236-L274](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx_if.v#L236-L274) — `frame_sync`/`ber_mon`/`watchdog` 三实例并排例化；watchdog 额外接收 `rx_bad_block`、`rx_sequence_error`、`rx_block_lock`、`rx_high_ber` 四路监测输入。

接口适配层有两段 `generate` 值得留意：

[rtl/eth_phy_10g_rx_if.v:L99-L110](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx_if.v#L99-L110) — `BIT_REVERSE` 在不同 SERDES 极性定义下反转 data 与 hdr 的位序；默认 0 即直通。

[rtl/eth_phy_10g_rx_if.v:L112-L135](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx_if.v#L112-L135) — `SERDES_PIPELINE` 用移位寄存器给整个 SERDES 输入加一段可编程延时，用于把监测/解扰路径与下游时序对齐（testbench 里设为 2）。

解扰器与同步头透传：

[rtl/eth_phy_10g_rx_if.v:L204-L208](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx_if.v#L204-L208) — `encoded_rx_data` 在 `SCRAMBLER_DISABLE` 时取原始数据、否则取解扰结果；而 `encoded_rx_hdr` **恒等于** `serdes_rx_hdr_int`（同步头不解扰）。

#### 4.1.4 代码实践

**目标**：建立「同步头是裸传的、监测先于解扰」的直觉。

**步骤**：

1. 打开 `rtl/eth_phy_10g_rx_if.v`，找到解扰器实例 `descrambler_inst`（约 L158-L172）与 `encoded_rx_hdr_reg` 的赋值（L208）。
2. 确认解扰器的 `data_in` 是 `serdes_rx_data_int`（仅负载），而 `serdes_rx_hdr_int` 没有进入任何解扰 LFSR。
3. 找到三个监测子模块的输入，确认它们都接 `serdes_rx_hdr_int`，而不是解扰后的信号。

**预期**：你会看到一条清晰的分界——同步头这条「副信道」绕过解扰器直达所有监测逻辑与译码器，这正是块锁定能在解扰前完成的设计根源。

#### 4.1.5 小练习与答案

**练习**：为什么 `frame_sync` 必须看解扰**之前**的同步头，而不能等解扰完再看？

**答案**：因为解扰器是自同步的 feed-forward 结构，它需要正确的 64 位负载按块对齐才能从数据流中重建扰码状态；而块对齐（找块边界）本身恰恰要靠同步头。如果先解扰再找边界，就陷入循环依赖——没有边界就没有正确的块切片，解扰也就无从谈起。同步头明文传输正是为了打破这个循环：先用裸同步头锁定边界，然后才能正确切片并解扰负载。

---

### 4.2 块锁定与位滑移（frame_sync）

#### 4.2.1 概念说明

`eth_phy_10g_rx_frame_sync` 实现 IEEE 802.3 第 49 章（10GBASE-R）的块同步状态机。它要做的事可以一句话概括：**反复滑动 SERDES 字边界，直到连续看到 64 个合法同步头，就宣布「块锁定」；锁定后若短期内频繁出现非法同步头，就宣布失锁、重新滑移。**

为什么是「连续 64 个合法同步头」？因为合法同步头只有 `01`/`10` 两种，非法的也是 `00`/`11` 两种。若字边界完全错位，某一拍恰好等于合法值的概率是 \(2/4 = 1/2\)，连续 64 拍都合法的概率是 \((1/2)^{64} \approx 5.4\times10^{-20}\)，几乎不可能假锁。反之，正确边界下每拍都合法，连续 64 拍很快就能凑齐。于是「64 个合法」就是假锁概率可忽略、真锁确认又足够快的折中——这正是 IEEE 规范选定的阈值。

#### 4.2.2 核心流程

模块核心是一段组合 `always @*` 计算下一状态，配合三个计数器与 bitslip 脉冲发生器：

- `sh_count_reg[5:0]`：窗口计数器，每来一个同步头（无论合法非法）+1，满 64（`&sh_count_reg` 即全 1，值 63）时溢出归零——这就是「64 个一窗」。
- `sh_invalid_count_reg[3:0]`：本窗内非法同步头计数，满 16 溢出。
- `bitslip_count_reg`：bitslip 脉冲之间的间隔定时器，防止滑得太快。
- `rx_block_lock_reg`：锁定标志。

伪代码（以「未锁定」为初始态）：

```
若 正在 bitslip 计时:        仅递减计时
否则 若 上一拍刚发了 slip:    撤销 slip 脉冲, 启动 LOW_CYCLES 间隔
否则 若 同步头合法(01/10):
        sh_count++; 若 sh_count 满 64 且 本窗无非法:  rx_block_lock = 1   ← 获得锁定
否则 (同步头非法):
        sh_count++; sh_invalid_count++;
        若 (未锁定) 或 (本窗非法已达 16):
            失锁; rx_block_lock = 0; 发一次 bitslip; 启动 HIGH_CYCLES 计时   ← 滑一位重试
        否则 若 sh_count 满 64:   归零（容忍少量错码，保持锁定）
```

`BITSLIP_HIGH_CYCLES=1` 表示 slip 脉冲只拉高 1 拍；`BITSLIP_LOW_CYCLES=8` 表示撤销后再等 8 拍才允许下一次 slip——给 SERDES 留出重新对齐的响应时间，避免连续 slip 把字边界冲过头。

#### 4.2.3 源码精读

合法/非法同步头用 `localparam` 定义：

[rtl/eth_phy_10g_rx_frame_sync.v:L67-L80](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx_frame_sync.v#L67-L80) — `SYNC_DATA=2'b10`、`SYNC_CTRL=2'b01` 为仅有的两个合法值。

合法同步头分支——累积 64 个合法且无非法即锁定：

[rtl/eth_phy_10g_rx_frame_sync.v:L96-L106](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx_frame_sync.v#L96-L106) — `if (&sh_count_reg)` 判窗口满 64；此时若 `!sh_invalid_count_reg`（本窗一个非法都没有）则 `rx_block_lock_next = 1`。

非法同步头分支——决定何时发 bitslip、何时失锁：

[rtl/eth_phy_10g_rx_frame_sync.v:L107-L125](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx_frame_sync.v#L107-L125) — 关键判定 `if (!rx_block_lock_reg || &sh_invalid_count_reg)`：**未锁定时**遇到任何一个非法头就 slip 一位、重新开始（所以获取锁定需要 64 个连续合法头）；**已锁定时**只有当本窗累计 16 个非法头（`&sh_invalid_count_reg`，即 4 位全 1 = 15 再来一个）才认定失锁、slip 重收，否则只要窗口满 64 就归零，容忍零星错码。

bitslip 脉冲时序由三段 if-else-if 级联实现：

[rtl/eth_phy_10g_rx_frame_sync.v:L91-L95](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx_frame_sync.v#L91-L95) — 若 `bitslip_count_reg` 非零则只递减（冷却期）；否则若上一拍 `serdes_rx_bitslip_reg` 仍为 1，则撤销脉冲并载入 `BITSLIP_LOW_CYCLES-1` 作下一冷却。配合 L118-L119 在判定失锁时置 `serdes_rx_bitslip_next=1`、载入 `BITSLIP_HIGH_CYCLES-1`，就构成了「拉高 1 拍 → 冷却 8 拍」的脉冲节拍。

#### 4.2.4 代码实践

**目标**：本讲的核心实践——对照源码说清「从失锁到 block_lock」的完整路径，以及 bitslip 的触发条件。

**步骤**：

1. 打开 `rtl/eth_phy_10g_rx_frame_sync.v`，定位 L82 起的组合 `always @*` 块。
2. 复位后 `rx_block_lock_reg=0`、所有计数器为 0，模拟「未锁定」初态。
3. 假设此时字边界错位、`serdes_rx_hdr` 基本随机。跟踪第一拍非法头：进入 L107 的 `else` 分支，因 `!rx_block_lock_reg` 为真 → `rx_block_lock_next=0`、`serdes_rx_bitslip_next=1`、载入 `BITSLIP_HIGH_CYCLES-1=0`、`sh_count/sh_invalid_count` 归零。
4. 下一拍 slip 脉冲已发出，进入 L91 的冷却分支递减，直到冷却结束才能再 slip。每个 slip 让 SERDES 把字边界挪 1 位（testbench 里用 `bit_offset += 1` 模拟）。
5. 当某次 slip 后字边界恰好对齐，`serdes_rx_hdr` 开始持续合法。进入 L96 分支，`sh_count` 逐拍 +1；当 `sh_count_reg` 从 63 再来一个合法头触发 `&sh_count_reg`，且全程 `sh_invalid_count==0` → `rx_block_lock_next=1`，**锁定！**
6. 锁定后再跟踪一次「容忍 vs 失锁」：若 64 窗内偶发 1~2 个非法头，因 `&sh_invalid_count_reg` 未达全 1，走 L120 的 `else if` 仅归零、保持锁定；若某窗内凑齐 16 个非法头，则 `&sh_invalid_count_reg` 为真 → 失锁、重新 slip。

**需要观察的现象**：在波形上，`serdes_rx_bitslip` 应是一串等间隔（间隔约 `HIGH+LOW+1`=10 拍）的单拍脉冲；`rx_block_lock` 在某拍跳变为 1 后稳定，直到下一次严重错码才回落。

**预期结果**：你能用一张状态迁移图把「未锁定（持续 slip）→ 64 合法 → 锁定 → 偶发错码容忍 / 16 非法 → 失锁重收」四条路径画完整，且每条路径都能指到源码的具体行。

> 说明：本实践为源码阅读型，未实际运行仿真；若要观察真实波形，可运行 4.4 节综合实践里的 testbench。

#### 4.2.5 小练习与答案

**练习 1**：获取锁定需要「连续 64 个合法同步头」，这个 64 来自哪个计数器、哪一行判定？

**答案**：来自 `sh_count_reg[5:0]`（6 位，最大 63）。判定在 [L99](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx_frame_sync.v#L99) 的 `if (&sh_count_reg)`——当 6 位全 1（值 63）时，下一个合法头使 `sh_count_next=0` 并检查 `sh_invalid_count`。

**练习 2**：为什么失锁阈值用「16 个非法」而不是「1 个非法」？

**答案**：真实链路即使边界正确，也会因噪声偶发出错。若 1 个非法就失锁，链路会在正常工作时频繁抖动式重收。16 个非法/64 窗（即 25% 同步头出错）远高于正常 BER，只有真正严重劣化才触发重收，在「灵敏度」与「稳定性」间取得平衡，这也是 IEEE 802.3 规定的阈值。

---

### 4.3 BER 监测（ber_mon）

#### 4.3.1 概念说明

`eth_phy_10g_rx_ber_mon` 监测链路误码率，输出 `rx_high_ber`。它与 `frame_sync` 用的是同一个输入信号（裸同步头），但关注点不同：`frame_sync` 关心「能不能找到边界」，`ber_mon` 关心「找到边界后信号质量够不够好」。

它的工作方式很朴素：在一个固定时间窗口（125 µs）内数非法同步头的个数，若超过阈值就拉高 `rx_high_ber`。125 µs 是 10GBASE-R 的标准监测窗口（与 IEEE 第 49 章 BER 告警窗口一致）。窗口结束清零，开始下一窗。

阈值与计数：非法计数器 `ber_count_reg[3:0]` 只有 4 位，到 15 即饱和；当已饱和（`==15`）又来一个非法头时拉高 `rx_high_ber`。也就是说约 **16 个非法同步头 / 125 µs** 触发告警。

#### 4.3.2 核心流程

10GBASE-R 块率 = 10.3125 Gbps ÷ 66 bit ≈ 156.25 MHz，故一个时钟周期约 6.4 ns，125 µs 折合周期数为：

\[
N_{125\mu s} = \frac{125000\,\text{ns}}{6.4\,\text{ns}} \approx 19531
\]

这正是参数 `COUNT_125US = 125000/6.4`。伪代码：

```
每拍: time_count 递减
若 同步头合法:
    若 ber_count 未饱和 且 time_count==0:  rx_high_ber = 0   (本窗错误少, 清告警)
否则 (非法):
    若 ber_count 已饱和(==15):  rx_high_ber = 1            (错误太多, 锁存告警)
    否则: ber_count++; 若 time_count==0: rx_high_ber = 0
若 time_count==0:                 (125 µs 窗口到期)
    ber_count = 0; time_count = COUNT_125US    (开下一窗)
```

注意 `rx_high_ber` 一旦在窗内被置 1，就要等到下一个窗口边界（`time_count==0`）才可能被清 0——它是「按窗口锁存」的告警，不是逐拍抖动的。

#### 4.3.3 源码精读

时间窗口与计数器声明：

[rtl/eth_phy_10g_rx_ber_mon.v:L62-L73](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx_ber_mon.v#L62-L73) — `COUNT_WIDTH = $clog2($rtoi(COUNT_125US))` 由窗口长度自动推导计数器位宽；`time_count_reg` 初值即 `COUNT_125US`。

告警置位与清除的核心逻辑：

[rtl/eth_phy_10g_rx_ber_mon.v:L85-L102](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx_ber_mon.v#L85-L102) — 合法头分支在窗口末尾、且错误未饱和时清告警（L88-L90）；非法头分支在 `ber_count_reg==4'd15`（饱和）时置告警（L93-L95），否则递增计数。

窗口到期归零：

[rtl/eth_phy_10g_rx_ber_mon.v:L103-L107](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx_ber_mon.v#L103-L107) — `time_count_reg==0` 时把 `ber_count_next=0`、`time_count_next=COUNT_125US`，重启窗口。

#### 4.3.4 代码实践

**目标**：理解 `rx_high_ber` 的「窗口锁存」语义，并算出大致告警阈值。

**步骤**：

1. 读 `rtl/eth_phy_10g_rx_ber_mon.v` L75-L108，确认 `rx_high_ber` 只在两处被改写：非法头且 `ber_count==15` 时置 1（L95），窗口末尾且未饱和时清 0（L89/L99）。
2. 计算：125 µs 内约 19531 块，每块 2 位同步头。16 个非法头/窗相当于同步头出错率约 \(16/(19531\times2)\approx 4\times10^{-4}\)。
3. 对比 `frame_sync` 的失锁阈值（64 窗内 16 非法 ≈ 25%）：`high_ber` 阈值（约 0.04%）其实**更灵敏**，所以一条劣化中的链路会先报 `high_ber`，进一步恶化到边界都保不住时才丢 `block_lock`。

**预期结果**：你能说清「`high_ber` 是早期告警、`block_lock` 丢失是晚期告警」的层次关系，并指出两者都基于同一个 `serdes_rx_hdr`、仅窗口与阈值不同。

> 说明：阈值计算为手算推导，具体仿真波形见综合实践。

#### 4.3.5 小练习与答案

**练习**：为什么 `ber_mon` 用同步头出错数而不是真正逐比特统计 BER？

**答案**：逐比特 BER 需要已知发送序列（如 PRBS 测试模式）才能比对，正常通信时无法做。而同步头只有 2 位、合法值仅 2 种，是一个不依赖发送内容的「内置探针」——它出错必然意味着线路出错。用同步头出错率近似 BER，是一种在正常业务流量下也能持续监测的轻量方案。需要精确 BER 时，改用本模块的 PRBS31 检测路径（见 4.4 节）。

---

### 4.4 看门狗、错误聚合与状态机（watchdog）

#### 4.4.1 概念说明

`eth_phy_10g_rx_watchdog` 是接收链路的「总管」。它把来自各方的信号聚合成两个对外输出：

- `serdes_rx_reset_req`：当链路长期不可恢复时，脉冲复位整个 SERDES，强制从零重新锁定。
- `rx_status`：当链路长期稳定时拉高，告诉上层「接收通路已就绪」。

它聚合的输入有：`rx_bad_block`（译码器发现未知块类型）、`rx_sequence_error`（译码器发现 START/TERM 时序错乱）、`rx_block_lock`（是否锁定）、`rx_high_ber`（信号是否劣化）。

判定同样按 125 µs 窗口进行：每个窗口统计「本窗是否健康」，连续多窗健康则 `rx_status` 置位，连续多窗异常则发复位请求。

#### 4.4.2 核心流程

每个 125 µs 窗口，看门狗记录两件事：

- `saw_ctrl_sh`：本窗是否至少见过一个控制块同步头（`SYNC_CTRL`）。正常链路空闲时发的是控制块（IDLE），若一整个窗口连一个控制块都没有，说明链路异常。
- `block_error_count[9:0]`：本窗内 `rx_bad_block || rx_sequence_error` 的累计次数（饱和到 1023）。

窗口到期时的判定：

```
若 time_count==0 (窗口到期):
    若 (未见过控制块) 或 (块错误计数已饱和):
        error_count++ ; status_count=0       (本窗判为"坏")
    否则:
        error_count=0 ; 若 status_count 未饱和: status_count++   (本窗判为"好")
    若 error_count 饱和(==15 再来一窗):   serdes_rx_reset_req = 1 (脉冲)   ← 连续 16 个坏窗, 复位 SERDES
    若 status_count 饱和:                 rx_status = 1                    ← 连续多个好窗, 链路就绪
    清 saw_ctrl_sh 与 block_error_count, 开下一窗
```

同时，只要 `rx_block_lock` 为 0，立即把 `rx_status` 拉低、`status_count` 清零（没锁定谈不上就绪）。注意 `serdes_rx_reset_req` 用了独立的、带异步复位的 always 块（L162-L168），保证它是一个干净的脉冲。

阈值换算：连续 16 个坏窗 = \(16 \times 125\,\mu s = 2\,\text{ms}\) 持续异常才发复位；`status_count` 同样饱和后才置 `rx_status`，意味着链路要稳定若干毫秒才宣告就绪，避免在临界状态下抖动。

#### 4.4.3 源码精读

块锁定下的窗口内统计：

[rtl/eth_phy_10g_rx_watchdog.v:L103-L113](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx_watchdog.v#L103-L113) — 锁定时见到 `SYNC_CTRL` 即置 `saw_ctrl_sh_next=1`；`rx_bad_block||rx_sequence_error` 时递增 `block_error_count`（饱和保护 `!(&block_error_count_reg)`）；未锁定则直接清 `rx_status` 与 `status_count`。

窗口到期的好/坏判定与复位/状态输出：

[rtl/eth_phy_10g_rx_watchdog.v:L115-L141](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx_watchdog.v#L115-L141) — L120 判定本窗好坏（`!saw_ctrl_sh_reg || &block_error_count_reg` 即坏）；L130-L133 在 `&error_count_reg`（连续坏窗饱和）时发 `serdes_rx_reset_req_next=1`；L135-L137 在 `&status_count_reg` 时置 `rx_status_next=1`。

复位脉冲的独立寄存器：

[rtl/eth_phy_10g_rx_watchdog.v:L162-L168](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx_watchdog.v#L162-L168) — `serdes_rx_reset_req_reg` 单独用 `posedge clk or posedge rst` 描述，且组合侧每拍默认 `serdes_rx_reset_req_next=0`（L99），所以它一定是一个单拍脉冲而非电平。

#### 4.4.4 代码实践

**目标**：把 `bad_block`/`sequence_error` 的来源与 `rx_status` 的就绪条件串起来。

**步骤**：

1. 在 `rtl/eth_phy_10g_rx.v`（L131-L145）确认 `rx_bad_block`/`rx_sequence_error` 由 `xgmii_baser_dec_64` 产生，回送给 `rx_if`，再喂进 watchdog（`rx_if` L260-L274）。
2. 在 `rtl/eth_phy_10g_rx_watchdog.v` 找到 `rx_status` 置位的两个必要条件：(a) `rx_block_lock` 持续为 1（L103 vs L110-L112）；(b) 连续若干窗口「见过控制块且块错误未饱和」（L120-L137）。
3. 找到 `serdes_rx_reset_req` 的触发链：连续 16 个坏窗 → L130-L133 脉冲 → 经 `rx_if` L234 输出（注意 PRBS31 测试模式下会被屏蔽，见下）。

**需要观察的现象**：链路刚锁定时 `rx_status` 并不立即拉高，要等若干个 125 µs 好窗累积后才置位；`serdes_rx_reset_req` 是稀疏的单拍脉冲。

**预期结果**：你能解释「`block_lock=1` 是 `rx_status=1` 的必要非充分条件」——锁定只代表找到了边界，链路真正「就绪」还要看连续窗口的健康度。

#### 4.4.5 小练习与答案

**练习 1**：`rx_status` 与 `rx_block_lock` 有何区别？

**答案**：`rx_block_lock` 表示「字边界已对齐、能识别块」的瞬时事实，由 `frame_sync` 给出；`rx_status` 表示「链路已稳定就绪可供业务使用」的持续结论，由 `watchdog` 在连续多个健康窗口后才置位，且未锁定时立即撤销。前者是后者的必要条件。

**练习 2**：在 `rx_if` 里，`serdes_rx_bitslip` 和 `serdes_rx_reset_req` 都被 `!(PRBS31_ENABLE && cfg_rx_prbs31_enable)` 与了一下（L233-L234），为什么？

**答案**：PRBS31 是一种「线路测试模式」——发送侧只发伪随机序列、不发正常以太网帧。此时同步头不再是规律的 `01`/`10`，`frame_sync` 会判定失锁、watchdog 会判坏窗并不断复位 SERDES，干扰测试。所以在 PRBS31 测试模式下显式屏蔽 slip 与 reset_req，让链路停在测试态，专心用 `rx_error_count`（见下）统计误码。

---

### 4.5 PRBS31 误码检测（rx_if 内置）

> 本节作为补充，帮助你理解 testbench 里 test 5 的 `rx_error_count`。

`eth_phy_10g_rx_if` 内部还集成了一个 PRBS31 检测器，用于在线路测试模式下精确统计误码：

[rtl/eth_phy_10g_rx_if.v:L174-L188](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx_if.v#L174-L188) — 用一个 31 位 Fibonacci、feed-forward LFSR（多项式 `31'h10000001`，即 \(x^{31}+x^{28}+1\)）对输入 `{data,hdr}` 取反后做预期推演；输入取反是因为发送侧 PRBS31 习惯取反输出。

[rtl/eth_phy_10g_rx_if.v:L192-L202](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_phy_10g_rx_if.v#L192-L202) — 把 66 位预期序列与实际逐比特比对，错位累加，奇偶位分两路（`rx_error_count_1/2`）再合并成 7 位 `rx_error_count`，即每拍线路上的错比特数。

只有 `PRBS31_ENABLE=1` 且 `cfg_rx_prbs31_enable` 打开时才计入；否则 `rx_error_count` 恒 0。这是比同步头探针精确得多的真 BER 测量手段，但要求发送侧配合发 PRBS31。

## 5. 综合实践

**任务**：运行 `tb/test_eth_phy_10g_rx_64.py` 的 test 4「test frame sync」，在波形上把 `frame_sync`、`ber_mon`、`watchdog` 三个模块的协作链完整读出来。这是把本讲四个模块串起来的端到端验证。

**背景**：该 testbench（基于 myhdl，但逻辑可直接读）在 `tb/test_eth_phy_10g_rx_64.py` 的 `shift_bits` 协程里模拟了 SERDES 的 bitslip 行为——每当 DUT 发出 `serdes_rx_bitslip`，就把数据流整体移 1 位（`bit_offset += 1`，L158-L161）。`load_bit_offset` 则可人为注入一个相位偏移来制造失锁。

**步骤**：

1. 配好 cocotb/iverilog（见 u1-l4）。该 testbench 是 myhdl 时代的双用途文件，若直接用 `make` 跑不通，可改读 `tb/eth_phy_10g/`（cocotb 版完整 PHY testbench，Makefile 在 [tb/eth_phy_10g/Makefile](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_phy_10g/Makefile)），它把 `PARAM_COUNT_125US := 195`（L56）以加速仿真，并把 `PARAM_PRBS31_ENABLE := 1`（L51）。
2. 阅读 [tb/test_eth_phy_10g_rx_64.py:L273-L293](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_eth_phy_10g_rx_64.py#L273-L293) 的 test 4：
   - 起始 `assert rx_block_lock`（L276）——确认已锁定。
   - `load_bit_offset.append(33)`（L278）——人为偏移 33 位（半个块），破坏字边界。
   - `delay(600)` 后 `assert not rx_block_lock`（L282）——`frame_sync` 检测到 16 个非法头，失锁。
   - 同时刻 `assert rx_high_ber`（L283）——`ber_mon` 也在告警。
   - `delay(3000)` 后 `assert rx_block_lock`（L287）——`frame_sync` 靠反复 bitslip 重新对齐、再次锁定。
   - `delay(2000)` 后 `assert not rx_high_ber`（L291）——重新锁定后过了若干 125 µs 窗口，错误回落，告警清除。
3. （可选）阅读 test 5（L295-L317）：打开 `rx_prbs31_enable`，随机数据下 `rx_error_count > 0`；切到真正 PRBS31 源（`prbs_en=True`）后 `rx_error_count == 0`，验证 4.5 节的 PRBS31 检测器。

**需要观察的现象（开 WAVES=1 抓波形）**：

- 失锁瞬间 `serdes_rx_bitslip` 开始按 ~10 拍节拍发脉冲，直到 `rx_block_lock` 重新跳变。
- `rx_high_ber` 滞后于失锁出现、且在重新锁定后仍持续若干窗口才清除（窗口锁存语义）。
- `serdes_rx_reset_req` 在 test 4 这种「可恢复」场景下**不**出现（只连续 slip 即可恢复）；它只在连续 16 个坏窗（2 ms 级）不可恢复时才发。

**预期结果**：你能用一张时间轴把 `bitslip 脉冲串 → block_lock 跳变 → high_ber 告警/清除 → (可能) reset_req` 的因果链画出来，并把每个跳变对应到 `frame_sync`/`ber_mon`/`watchdog` 的具体源码行。test 4 里的 4 条断言全部通过即说明你对三个模块协作的理解正确。

> 说明：testbench 实际运行结果取决于本地工具链是否就绪；若 myhdl 版无法直接跑，`tb/eth_phy_10g/` 的 cocotb 版是更可靠的入口。具体通过输出待本地验证。

## 6. 本讲小结

- `eth_phy_10g_rx` 是接收侧布线层，靠 `rx_if`（SERDES 适配 + 解扰 + 三监测子模块）与 `xgmii_baser_dec_64`（64b/66b→XGMII）两块拼成；三个监测子模块都只看 2 位**裸同步头** `serdes_rx_hdr`，与解扰并行、且先于解扰。
- `frame_sync` 实现块锁定：未锁定时遇任何非法头就发一次 `bitslip`（拉高 1 拍、冷却 8 拍）滑动 SERDES 字边界；连续 64 个合法同步头即获 `rx_block_lock`；锁定后 64 窗内累计 16 个非法头才失锁重收。
- `ber_mon` 在 125 µs 窗口内数非法同步头，约 16 个/窗即拉高 `rx_high_ber`（约 \(4\times10^{-4}\) 同步头出错率），比失锁阈值更灵敏，是「早期告警」；告警按窗口锁存。
- `watchdog` 聚合 `bad_block`/`sequence_error`/`block_lock`/`high_ber`：每窗判好坏（见过控制块且块错误未饱和为好），连续多坏窗（≈2 ms）发 `serdes_rx_reset_req` 脉冲复位 SERDES，连续多好窗置 `rx_status=1` 宣告就绪。
- `rx_status` 是 `rx_block_lock` 的「加强版」：锁定是就绪的必要条件，但还要连续健康窗口累积才置位。
- PRBS31 检测器（`rx_if` 内）在测试模式下提供逐比特精确误码计数 `rx_error_count`，并在此模式下屏蔽 slip/reset 以免干扰测试。

## 7. 下一步学习建议

- 下一讲 **u10-l3（eth_phy_10g 发送与 MAC/PHY 合一）** 讲发送侧链路 `eth_phy_10g_tx`/`tx_if` 与 `eth_phy_10g`、`eth_mac_phy_10g` 顶层，与本讲接收侧对称，建议对照阅读。
- 想深入 64b/66b 译码细节（`rx_bad_block`/`rx_sequence_error` 的具体产生条件）可回看 u10-l1 的 `xgmii_baser_dec_64`。
- 想理解整条 MAC+PHY 如何上板，可读 **u12-l1（组装完整 UDP 回显系统）** 与 `example/` 下的 10G 参考设计。
- 对扰码器/PRBS 的 LFSR 数学原理感兴趣，可回看 **u2-l1（lfsr：通用并行 LFSR/CRC 引擎）**，本讲的解扰器与 PRBS31 检测器都是它的实例。
