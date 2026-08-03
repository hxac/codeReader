# eth_mac_10g：64 位 MAC 与缺陷填充

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `eth_mac_10g` 作为「布线层」是如何把 `axis_xgmii_rx_64`、`axis_xgmii_tx_64` 以及可选的 `mac_ctrl_*`/`mac_pause_ctrl_*` 子模块拼成一个 10G/25G MAC 的；
- 理解 64 位（或 32 位）数据通路与千兆 8 位 MAC 在位宽、`tkeep`、XGMII 定界符上的关键差异；
- **讲透缺陷填充（Deficiency Idle Count, DIC）**：为什么 8 字节/拍的 XGMII 没法精确保持 12 字节帧间间隔（IFG），以及 `deficit_idle_count` + lane swap 如何在「永不低于最小 IFG」与「平均 IFG 恰好等于配置值」之间取得平衡；
- 认清 PTP 时间戳、PFC/PAUSE 流量控制在 10G MAC 中的对应接口与参数。

本讲承接 [u9-l1](u9-l1-axis-xgmii-rx-tx.md)（XGMII 与 64 位 AXI-Stream 互转，已讲过 64 位 `tkeep`、8 个魔数残留 CRC 校验、lane swap）和 [u4-l3](u4-l3-eth-mac-1g-core.md)（千兆 MAC 布线层、PAD 填充、PTP 旁带时间戳）。

## 2. 前置知识

- **XGMII 定界符**：10G/25G 的物理接口在数据总线上额外每位（lane）配一根控制位，用 4 个控制字符定界帧——`IDLE=0x07`、`START=0xfb`、`TERM=0xfd`、`ERROR=0xfe`。一帧被 `START` 与 `TERM` 夹住。详见 u9-l1。
- **64 位 AXI-Stream 与 `tkeep`**：宽通路一拍传 8 字节，末字不一定占满，用 `tkeep`（8 位，每位对应一字节有效）标记末拍有效字节。这与千兆 8 位通路（每拍 1 字节，无 `tkeep`）是最显眼的差异。
- **帧间间隔 IFG（Inter-Frame Gap）**：IEEE 802.3 规定相邻两帧之间至少要有 12 字节的空闲（IDLE），给接收方留出恢复时间。**本讲的核心难点就是「12 不是 8 的倍数」**。
- **千兆 MAC 的布线层思路**（u4-l3）：`eth_mac_1g` 本身几乎不写逻辑，只是例化 `axis_gmii_rx/tx` 并用 `generate` 按需选配流控子模块；`eth_mac_10g` 沿用完全相同的组织方式，只是把子模块换成 64 位版本。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [rtl/eth_mac_10g.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_10g.v) | 10G/25G MAC 顶层**布线层**：按 `DATA_WIDTH` 选 64/32 位子模块，按 `PAUSE_ENABLE`/`PFC_ENABLE` 选配流控，透传 PTP 时间戳与状态。 |
| [rtl/axis_xgmii_tx_64.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_64.v) | **发送翻译器**：AXI-Stream 帧 → XGMII。补前导/SFD、追加 FCS、做 PAD 填充、实现 **DIC 与 IFG**。本讲 DIC 的主角。 |
| [rtl/axis_xgmii_rx_64.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_rx_64.v) | **接收翻译器**：XGMII → AXI-Stream 帧。剥离前导/SFD/FCS、lane swap 还原、魔数残留法校验 CRC。u9-l1 已精读，本讲只点出与 10G MAC 的接口。 |
| tb/eth_mac_10g/test_eth_mac_10g.py | 仿真参考。其中 `run_test_tx_alignment` 用一段 Python 模型精确预测 DIC 下每帧的起始 lane，是理解 DIC 行为的权威依据。 |

## 4. 核心概念与源码讲解

### 4.1 64 位 MAC 通路：eth_mac_10g 布线层

#### 4.1.1 概念说明

`eth_mac_10g` 与 `eth_mac_1g` 在「身份」上完全一样——它是一个**布线层（wiring layer）**模块：自身几乎不含时序逻辑，只负责把现成的子模块按参数拼起来、把端口连出去。它解决的问题是：把「XGMII 物理信号 ↔ AXI-Stream 帧」的翻译（`axis_xgmii_rx/tx_64`）与「MAC 控制帧 / 流量控制」（`mac_ctrl_*`/`mac_pause_ctrl_*`）打包成一个对外接口整齐的成品 MAC。

与千兆版本相比，最本质的差异不在布线思路，而在**数据通路宽度**：从 8 位/拍变成 64 位/拍（可选 32 位），因此引入 `tkeep`、`tlast` 之外的宽通路信号，XGMII 定界也改成「一拍内用控制字符夹住帧」。

#### 4.1.2 核心流程

`eth_mac_10g` 的组装分两步 `generate`：

1. **按位宽选数据通路子模块**（`DATA_WIDTH == 64` 或 `32`）：
   - 64 位：例化 `axis_xgmii_rx_64` + `axis_xgmii_tx_64`；
   - 32 位：例化 `axis_xgmii_rx_32` + `axis_xgmii_tx_32`，并把 `start_packet[1]` 拉零（32 位不支持 lane 4 起始）。
2. **按是否启用流控选配 `mac_ctrl_*`/`mac_pause_ctrl_*`**：
   - `MAC_CTRL_ENABLE = PAUSE_ENABLE || PFC_ENABLE` 为真时，在 TX/RX 通路上分别串入 `mac_ctrl_tx`/`mac_ctrl_rx`（识别/插入 MAC 控制帧）和 `mac_pause_ctrl_tx`/`mac_pause_ctrl_rx`（PAUSE/PFC 倒计时）；
   - 为假时，走 `else` 分支把 TX/RX 内部总线**直连**到对外端口，所有流控状态/统计信号常置 0（零面积）。

收发各走独立时钟域 `rx_clk`/`tx_clk`，与千兆 MAC 一致。

#### 4.1.3 源码精读

**参数与位宽断言**。`eth_mac_10g` 只接受 32 或 64 位通路，并要求字节粒度（`KEEP_WIDTH*8 == DATA_WIDTH`），否则在 `initial` 里直接 `$error` 并 `$finish`：

[rtl/eth_mac_10g.v:34-52](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_10g.v#L34-L52) —— 参数表，注意 `ENABLE_DIC`、`ENABLE_PADDING`、`MIN_FRAME_LENGTH`、`PTP_TS_*`、`PFC_ENABLE`/`PAUSE_ENABLE` 都在这里，与本讲三个主题一一对应。

[rtl/eth_mac_10g.v:190-200](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_10g.v#L190-L200) —— 位宽断言，只允许 32/64 位。

**第一步 generate：按位宽选子模块**。64 位分支例化 `axis_xgmii_rx_64`/`axis_xgmii_tx_64`，把 `ENABLE_DIC`、`ENABLE_PADDING`、`MIN_FRAME_LENGTH`、PTP 参数透传给 TX，把 `cfg_ifg`/`cfg_tx_enable`/`start_packet`/`error_underflow` 连到 TX：

[rtl/eth_mac_10g.v:217-279](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_10g.v#L217-L279) —— 64 位分支：RX 把 `xgmii_rxd/rxc` 翻成 `rx_axis_*`，TX 把 `tx_axis_*` 翻成 `xgmii_txd/txc`。注意 DIC 相关的 `cfg_ifg` 只接 TX 侧（IFG 是发送方责任）。

[rtl/eth_mac_10g.v:281-347](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_10g.v#L281-L347) —— 32 位分支，结构同构，只是子模块名带 `_32`，并把 `start_packet[1]` 强制置 0。

**第二步 generate：按流控选配**。`MAC_CTRL_ENABLE` 是开关，关闭时整段流控逻辑零面积：

[rtl/eth_mac_10g.v:186](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_10g.v#L186) —— `MAC_CTRL_ENABLE = PAUSE_ENABLE || PFC_ENABLE`，与千兆 MAC 完全相同的「按需综合」模式。

[rtl/eth_mac_10g.v:687-725](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_10g.v#L687-L725) —— 流控关闭时的 `else` 分支：TX/RX 内部总线直接 `assign` 到对外端口，所有 `stat_*` 与 `rx_lfc_req`/`rx_pfc_req`/`tx_pause_ack` 常置 0。

> 小结：**`eth_mac_10g` 本身一行 DIC/IFG 逻辑都没写**，真正的功臣是它例化的 `axis_xgmii_tx_64`。下一节我们就钻进 TX 看个究竟。

#### 4.1.4 代码实践

**目标**：用现成的 `tb/eth_mac_10g` 仿真，确认 `eth_mac_10g` 在 `DATA_WIDTH=64`、流控全开（`PFC_ENABLE=1`）下的子模块实例化关系。

**步骤**：

1. 配好 cocotb + iverilog（见 [u1-l4](u1-l4-testbench-and-simulation.md)）。
2. 查看 `tb/eth_mac_10g/Makefile`，注意它把 9 个 RTL 文件都列进 `VERILOG_SOURCES`（`eth_mac_10g` + rx/tx 的 32/64 版 + 4 个流控模块 + `lfsr`）。
3. 进入目录运行：`cd tb/eth_mac_10g && make`（默认 `PARAM_DATA_WIDTH=64`、`PARAM_ENABLE_DIC=1`、`PARAM_PFC_ENABLE=1`，见 Makefile 顶部）。

**观察**：仿真应全部通过。打开生成的波形（`make WAVES=1`）或在 iverilog 中用 `$display`，可看到 DUT 内部例化了 `axis_xgmii_rx_inst`/`axis_xgmii_tx_inst`/`mac_ctrl_tx_inst`/`mac_ctrl_rx_inst`/`mac_pause_ctrl_tx_inst`/`mac_pause_ctrl_rx_inst` 六个子模块——这就是布线层的全部内容。

**预期结果**：所有用例 PASS，证明布线正确。如失败，多半是 `VERILOG_SOURCES` 漏文件（iverilog 不跨目录自动找子模块，见 u1-l4）。

#### 4.1.5 小练习与答案

**练习 1**：把 `tb/eth_mac_10g/Makefile` 里的 `PARAM_PFC_ENABLE` 改成 0，重新仿真。DUT 内部例化的子模块数量会发生什么变化？

**答案**：`MAC_CTRL_ENABLE` 变 0，`mac_ctrl_*`/`mac_pause_ctrl_*` 四个实例不再综合（走 `else` 直连分支），DUT 内部只剩 `axis_xgmii_rx_inst`/`axis_xgmii_tx_inst` 两个数据通路子模块。相关 `stat_*` 端口恒为 0。

**练习 2**：为什么 `eth_mac_10g` 的 `cfg_ifg` 只连到 TX 实例，而不连 RX？

**答案**：IFG 是**发送方**的义务——发送方必须在两帧之间插入足够 IDLE。接收方只需识别帧边界（`START`/`TERM`），不关心对方有没有插够间隔，所以 `cfg_ifg` 与 RX 无关。

---

### 4.2 缺陷填充 DIC 与帧间间隔 IFG

#### 4.2.1 概念说明

这是本讲的核心，也是 `axis_xgmii_tx_64` 与千兆 `axis_gmii_tx` 最大的差别。

**问题从何而来**：IEEE 802.3 规定相邻两帧间至少 12 字节 IFG。千兆 GMII 是 8 位/拍，一个 IFG 字节占一拍，凑 12 字节很自然。但 XGMII 是 **8 字节/拍**——一拍就传 8 个字节。12 既不是 8 的倍数，也无法用「整数拍 IDLE」精确表达。

举个具体例子：假设一帧恰好在某拍的 lane 3 结束（`TERM` 在 lane 3），那么这一拍 lane 4–7 已经是 4 字节 IDLE。要凑满 12 字节 IFG，还差 8 字节，正好一整拍——没问题。但如果 `TERM` 在 lane 5，本拍只剩 lane 6–7 共 2 字节 IDLE，还差 10 字节；10 不是 8 的倍数，你只能再发一整拍（8 字节）凑到 10，或发两整拍凑到 18——**无论怎么发，实际 IFG 都会偏离 12**。

**两种策略**：

- **不开 DIC（`ENABLE_DIC=0`）**：简单粗暴——每帧独立地把 IFG **向上取整**到 4 字节边界（lane 0 或 lane 4 起始）。结果：单帧 IFG 永远合规（≥12），但**平均 IFG > 12**，浪费带宽。
- **开 DIC（`ENABLE_DIC=1`，默认）**：IEEE 802.3ae 允许的「缺陷填充」机制。维护一个 2 位的**累计亏空计数 `deficit_idle_count`**（取值 0–3）。当前帧的 IFG 可以**短一点**（少发的部分记入亏空），下一帧的 IFG 再**长一点**把亏空补回来。只要保证「任意单帧 IFG 不低于下限」且「多帧平均 IFG 恰好等于配置值」，就既合规又省带宽。

一句话总结 DIC 的精髓：**用「记账」换「带宽」——把 12 字节的非整数拍难题，转换成跨帧的累加与偿还。**

#### 4.2.2 核心流程

`axis_xgmii_tx_64` 用一个 7 状态机管理发送：

```
IDLE → PAYLOAD → (PAD?) → FCS_1 → (FCS_2?) → IFG → IDLE
                                   └─ DIC 在这里决策 ─┘
```

涉及 IFG/DIC 的关键寄存器有三个：

| 寄存器 | 位宽 | 含义 |
| --- | --- | --- |
| `ifg_count_reg` | 8 | 本帧还差多少字节 IDLE 没发完。 |
| `deficit_idle_count_reg` | 2 | 跨帧累计亏空（0–3 字节）。DIC 专用。 |
| `swap_lanes_reg` | 1 | 为 1 时，下一帧的 `START` 放在 **lane 4**（而非 lane 0），让 lane 0–3 充当 IFG 尾巴的 4 字节 IDLE。 |

**IFG/DIC 决策时机**：在帧尾的 `STATE_FCS_1` 算出本帧需要多少 IFG（`ifg_count_next`），然后在 `STATE_FCS_2` 或 `STATE_IFG` 里根据 `ENABLE_DIC` 决定：

- DIC 开：若剩余 IFG 在 4–7 字节，直接进 `IDLE` 并置 `swap_lanes=1`（用 lane swap 吸收 4 字节），把超出的部分记入 `deficit_idle_count` 留给下一帧；若 <4 字节，全部记入亏空、本帧不补。
- DIC 关：若剩余 IFG >4，继续发 IDLE 拍；否则进 `IDLE`，不记亏空（每帧独立取整）。

#### 4.2.3 源码精读

**关键寄存器声明**：

[rtl/axis_xgmii_tx_64.v:153-154](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_64.v#L153-L154) —— `ifg_count_reg` 与 `deficit_idle_count_reg`，DIC 的两个核心账本。

**IFG 需求的计算公式**（在 `STATE_FCS_1` 末尾）：

[rtl/axis_xgmii_tx_64.v:439](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_64.v#L439) —— 核心公式：

```verilog
ifg_count_next = (cfg_ifg > 8'd12 ? cfg_ifg : 8'd12)   // 基准：至少 12
               - ifg_offset                              // FCS 收尾拍已贡献的 IDLE 字节数
               + (swap_lanes_reg ? 8'd4 : 8'd0)          // 上一帧若 lane swap，本帧起算多 4
               + deficit_idle_count_reg;                 // 加上上一帧留下的亏空
```

四项含义：基准 IFG（取 `cfg_ifg` 与 12 的较大值）减去 FCS 收尾拍里已经出现的 IDLE（`ifg_offset`），再加上上一帧 lane swap 带来的 4 字节偏移和累计亏空。`ifg_offset` 由一张按末拍空字节数 `s_empty_reg` 索引的表给出：

[rtl/axis_xgmii_tx_64.v:237-296](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_64.v#L237-L296) —— FCS 收尾拍布局表，同时给出每个 `s_empty_reg` 取值下 FCS 如何与 `TERM`/`IDLE` 拼进 1–2 拍，以及对应的 `ifg_offset`（如 `s_empty=0` 时 FCS 溢出到第二拍、`ifg_offset=4`；`s_empty=7` 时全塞在第一拍、`ifg_offset=3`）。

**DIC 决策（`STATE_FCS_2`）**：

[rtl/axis_xgmii_tx_64.v:446-477](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_64.v#L446-L477) —— DIC 开/关两条分支的决策。DIC 开（`ENABLE_DIC`）时：

```verilog
if (ifg_count_next > 8'd7) begin
    state_next = STATE_IFG;               // 还差 >7 字节，老实发 IDLE 拍
end else begin
    if (ifg_count_next >= 8'd4) begin
        deficit_idle_count_next = ifg_count_next - 8'd4;  // 只还 4，余下记账
        swap_lanes_next = 1'b1;                           // 下一帧从 lane 4 起
    end else begin
        deficit_idle_count_next = ifg_count_next;          // 全部记账
        ifg_count_next = 8'd0;
        swap_lanes_next = 1'b0;
    end
    s_axis_tready_next = cfg_tx_enable;                    // 立刻可接下一帧
    state_next = STATE_IDLE;
end
```

DIC 关（`else`）时没有亏空记账，只做 4 字节边界的向上取整。`STATE_IFG` 里的 DIC 逻辑与之对称（发完 IDLE 拍后做同样的退出决策）：

[rtl/axis_xgmii_tx_64.v:504-528](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_64.v#L504-L528) —— `STATE_IFG` 的 DIC 决策，每发一拍 IDLE 就从 `ifg_count_reg` 扣 8，直到可以按亏空策略退出。

**lane swap 的物理实现**：`swap_lanes_reg` 为 1 时，把输出做半字（32 位）轮换，使下一帧的 `START` 落在 lane 4：

[rtl/axis_xgmii_tx_64.v:616-625](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_64.v#L616-L625) —— `swap_txd` 暂存上拍高 32 位，`swap_lanes_reg` 时输出 `{低 32 位, 上拍高 32 位}`，等效于把帧起点从 lane 0 平移到 lane 4，lane 0–3 留给 IDLE。

**配套：8 个并行 CRC 引擎**。因为末拍有效字节数（1–8）随帧长变化，FCS 的「最后一段」宽度不固定，TX 例化了 8 个 `lfsr`（`DATA_WIDTH` 分别为 8/16/.../64），按 `s_empty_reg` 选用对应宽度的结果，与 FCS 收尾拍布局表配合：

[rtl/axis_xgmii_tx_64.v:189-210](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_64.v#L189-L210) —— 8 个 CRC 引擎，第 n 个处理 `8*(n+1)` 位宽，输出 `crc_state_next[n]`，被 FCS 收尾拍按 `s_empty_reg` 选用。

**权威参考模型**：仿真里的 `run_test_tx_alignment` 用 Python 精确复现了这套 lane/亏空算法，是验证你理解的最佳依据：

[tb/eth_mac_10g/test_eth_mac_10g.py:279-306](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_10g/test_eth_mac_10g.py#L279-L306) —— DIC 参考模型。关键片段：

```python
lane = 0
deficit_idle_count = 0
for test_data in test_frames:
    start_lane_ref.append(lane)
    lane = (lane + len(test_data) + 4 + ifg) % byte_width   # 推进到帧尾 +FCS +IFG
    if enable_dic:
        offset = lane % 4
        if deficit_idle_count + offset >= 4:
            offset += 4                # 亏空 + 本次偏移超 4，多吃一拍 IDLE
        lane = (lane - offset) % byte_width
        deficit_idle_count = (deficit_idle_count + offset) % 4   # 亏空滚动
    else:
        offset = lane % 4
        if offset > 0:
            offset += 4                # 不开 DIC：直接向上取整到 4 边界
        lane = (lane - offset) % byte_width
assert start_lane_ref == start_lane     # 与硬件实测逐帧比对
```

读这段就能看出开/关 DIC 的本质差别：**开 DIC 时亏空 `deficit_idle_count` 跨帧滚动**（平均 IFG 精确收敛到配置值）；**关 DIC 时每帧独立取整**（平均 IFG 偏大、浪费带宽）。

#### 4.2.4 代码实践

**目标**：在同一份 `tb/eth_mac_10g` 仿真上，分别令 `PARAM_ENABLE_DIC=1` 与 `0`，对比连续变长帧的帧间间隔字节数与起始 lane 序列。

**步骤**：

1. 进入 `tb/eth_mac_10g`，先跑 DIC 开：
   ```bash
   make PARAM_ENABLE_DIC=1
   ```
   重点看 `run_test_tx_alignment` 用例的日志（脚本里 `tb.log.info("start_lane: %s", start_lane)` 与 `"start_lane_ref: %s"`），记录长度 60–91 各档的 `start_lane` 序列。
2. 再跑 DIC 关：
   ```bash
   make clean && make PARAM_ENABLE_DIC=0
   ```
   同样记录 `start_lane` 序列。
3.（可选，看真波形）两次都加 `WAVES=1`，用 GTKWave 打开 `eth_mac_10g.fst`，在 `xgmii_txd`/`xgmii_txc` 上找连续两帧之间的 `IDLE(0x07)` 字节数。

**观察**：

- **DIC 开**：`start_lane` 在 0 与 4 之间较自由地跳动，相邻帧挤得更紧；多帧平均 IFG ≈ `cfg_ifg`（默认 12）。
- **DIC 关**：`start_lane` 更规律地回到 4 字节边界（取整），相邻帧间距更大；平均 IFG 明显 > 12。

**预期结果**：两轮仿真的 `start_lane_ref`（Python 模型预测）都应与硬件实测 `start_lane` 逐帧相等（否则用例 `assert` 失败）。对比两轮的 `start_lane` 序列，能直接看到 DIC 对帧排布的收紧作用。

> 若无法本地运行：上述行为由 [tb/eth_mac_10g/test_eth_mac_10g.py:228-310](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_10g/test_eth_mac_10g.py#L228-L310) 的参考模型精确描述，可作为「源码阅读型实践」直接研读——重点理解 `deficit_idle_count` 的滚动更新那一行。

#### 4.2.5 小练习与答案

**练习 1**：DIC 关闭时，`deficit_idle_count_reg` 还会变化吗？为什么平均 IFG 会偏大？

**答案**：DIC 关闭时，`STATE_FCS_2`/`STATE_IFG` 走 `else` 分支，从不写 `deficit_idle_count_next`（复位后恒为 0）。每帧独立把 IFG 向上取整到 4 字节边界，无法用「上一帧少发、下一帧补」来抵消取整带来的多余字节，所以平均 IFG 大于配置值，带宽利用率更低。

**练习 2**：`swap_lanes_reg=1` 时，下一帧的 `START` 落在 lane 4。这 4 个 lane（0–3）此刻传的是什么？对 IFG 有什么贡献？

**答案**：lane 0–3 传 `IDLE(0x07)`，充当本帧 IFG 的最后 4 字节。这样 MAC 不必为这 4 字节单独发一整拍 IDLE，从而把 IFG 收紧到接近 12 字节，正是 DIC 省带宽的手段之一。

**练习 3**：`ifg_count_next` 公式里那一项 `+ deficit_idle_count_reg` 的物理含义是什么？去掉它会怎样？

**答案**：它把「上一帧欠下的 IDLE」加回到本帧应发的 IFG 上，即「还账」。去掉后亏空永远不偿还，多帧平均 IFG 会小于配置值，可能违反 IEEE 802.3 的最小 IFG 规定——这正是 DIC 必须跨帧记账的原因。

---

### 4.3 PTP 时间戳与 PFC/PAUSE 在 10G MAC 中的接口

#### 4.3.1 概念说明

10G MAC 的 PTP 与流控接口在**设计哲学**上与千兆 MAC（u4-l3/u4-l2）完全同源，本节只点出 10G 下的对应关系与参数差异，不重复千兆版的细节：

- **PTP 时间戳**：`PTP_TS_ENABLE` 打开后，RX 时间戳在检测到帧起始（`START`）时锁存、随帧尾搭车进 `tuser` 高位（带内）；TX 时间戳因发送时 AXI 输入已结束而走**旁带总线** `tx_axis_ptp_ts`/`tx_axis_ptp_ts_tag`/`tx_axis_ptp_ts_valid` 异步回送。格式由 `PTP_TS_FMT_TOD` 决定（1→96 位 ToD，0→64 位相对）。这些都与千兆版一致。
- **PFC/PAUSE**：`mac_pause_ctrl_rx/tx` 在 10G 下做完全相同的量子倒计时；区别只在 `cfg_quanta_step` 按 64 位通路重新计算。

#### 4.3.2 核心流程

- 收发各一个独立 PTP 时间戳输入：`tx_ptp_ts`、`rx_ptp_ts`。
- TX 侧：`axis_xgmii_tx_64` 在 `frame_start_reg` 拍锁存 `ptp_ts`（若上一帧做了 lane swap，则取半拍补偿 `ts_inc_reg>>1`），连同 tag 从旁带总线输出。
- RX 侧：`axis_xgmii_rx_64` 在检测到 `START`（lane 0 或 lane 4）时锁存 `ptp_ts`，帧尾塞进 `tuser`。
- PFC/PAUSE 的量子步长：`cfg_quanta_step = (DATA_WIDTH*256)/512`。

#### 4.3.3 源码精读

**PTP 端口与参数**：

[rtl/eth_mac_10g.v:42-49](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_10g.v#L42-L49) —— PTP 参数：`PTP_TS_ENABLE`、`PTP_TS_FMT_TOD`、`PTP_TS_WIDTH`、`TX_PTP_TAG_ENABLE` 等，`RX_USER_WIDTH`/`TX_USER_WIDTH` 随之自动膨胀（与千兆 MAC 同样的 tuser 位宽自适应）。

[rtl/eth_mac_10g.v:88-93](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_10g.v#L88-L93) —— PTP 旁带总线：`tx_ptp_ts`/`rx_ptp_ts` 输入，`tx_axis_ptp_ts`/`_tag`/`_valid` 输出，直接连到 `axis_xgmii_tx_64` 的 `m_axis_ptp_ts*`。

**TX 时间戳锁存（含 lane swap 半拍补偿）**：

[rtl/axis_xgmii_tx_64.v:564-598](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_64.v#L564-L598) —— `frame_start_reg` 拍锁存时间戳。`swap_lanes_reg` 时帧实际起始在半拍后，故取 `ptp_ts + (ts_inc_reg >> 1)`（`ts_inc_reg` 是相邻两拍 `ptp_ts` 之差，>>1 即半拍）做补偿，并把 `start_packet` 编码为 `2'b10`（lane 4 起始）或 `2'b01`（lane 0 起始）。

**RX 时间戳锁存与 lane swap**：

[rtl/axis_xgmii_rx_64.v:393-409](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_rx_64.v#L393-L409) —— 检测到 lane 0 或 lane 4 的 `START` 时锁存 `ptp_ts`；lane swap（lane 4 起始）同样取 `ptp_ts + (ts_inc_reg>>1)` 做半拍补偿，`start_packet` 编码 `2'b10`/`2'b01` 与 TX 对称。

**PFC/PAUSE 量子步长按 64 位通路换算**：

[rtl/eth_mac_10g.v:616](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_10g.v#L616) 与 [rtl/eth_mac_10g.v:671](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_10g.v#L671) —— `.cfg_quanta_step((DATA_WIDTH*256)/512)`。代入 `DATA_WIDTH=64` 得 `64*256/512 = 32`。含义：1 个 PAUSE 量子 = 512 比特时间；在 64 字节/拍通路下，倒计时器每拍应扣的「步长」被换算成 32，使倒计时与速率自动匹配（千兆版按 8 位通路换算，原理见 u4-l2）。`cfg_quanta_clk_en` 恒接 1（10G 下每个时钟周期都计）。

#### 4.3.4 代码实践

**目标**：在默认仿真配置（`PARAM_PTP_TS_ENABLE=1`、`PARAM_PFC_ENABLE=1`）下，观察 TX PTP 时间戳与帧起始时刻的对应关系。

**步骤**：

1. `cd tb/eth_mac_10g && make`（默认即开 PTP 与 PFC）。
2. 重点看 `run_test_tx_alignment` 的断言（[test_eth_mac_10g.py:259-272](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_10g/test_eth_mac_10g.py#L259-L272)）：它把旁带回来的 `ptp_ts` 换算成 ns，与接收帧的 SFD 仿真时刻比对，要求误差 < 0.01 ns。

**观察**：当帧从 lane 4 起始时，测试会把 SFD 时刻减去半个时钟周期（3.2 ns）再比对——这正是 lane swap 半拍补偿在仿真层的体现，与 RTL 里 `ptp_ts + (ts_inc_reg>>1)` 一一对应。

**预期结果**：所有帧的 `|rx_frame_sfd_ns - ptp_ts_ns - clk_period| < 0.01` 断言通过，证明 PTP 时间戳精确标记了发送/接收起始时刻，且 lane swap 补偿正确。

> 若无法本地运行：阅读 [rtl/axis_xgmii_tx_64.v:564-598](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_64.v#L564-L598)，跟踪 `frame_start_reg → m_axis_ptp_ts_reg` 的锁存路径与 `start_packet` 编码即可理解。

#### 4.3.5 小练习与答案

**练习 1**：为什么 TX 时间戳走旁带总线，而 RX 时间戳能塞进 `tuser`？

**答案**：RX 侧收到帧时，`tuser` 与帧数据同拍输出，时间戳可随帧尾搭车（带内）。TX 侧发送完成时，AXI 输入端的帧数据早已传完，没有 `tuser` 拍可搭载时间戳，只能用独立的旁带总线 `tx_axis_ptp_ts` 连同 `tag` 异步回送给上层去匹配。（与 u4-l3 千兆 MAC 同理。）

**练习 2**：把 `DATA_WIDTH` 从 64 改成 32，`cfg_quanta_step` 会变成多少？为什么需要随位宽换算？

**答案**：`(32*256)/512 = 16`。1 个 PAUSE 量子 = 512 比特时间，而倒计时器按「每拍扣多少」工作；位宽减半意味着每拍承载的比特数减半，所以每拍应扣的步长也要减半，才能让「量子到期」对应到相同的绝对时间。

---

## 5. 综合实践

把本讲三个主题串起来，完成一个「DIC 行为分析」小任务：

1. **研读参考模型**：打开 [tb/eth_mac_10g/test_eth_mac_10g.py:279-306](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_10g/test_eth_mac_10g.py#L279-L306)，手算一组：`ifg=12`、连续 10 个 64 字节帧、`enable_dic=1`，逐步推出每帧的 `start_lane` 与 `deficit_idle_count` 演化（注意 `byte_width=8`）。
2. **对照 RTL**：在你的手算结果里找到「亏空累积 → 触发多吃一拍 IDLE」的那一帧，回到 [rtl/axis_xgmii_tx_64.v:453-467](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_64.v#L453-L467) 确认硬件在那帧确实走了「`offset += 4`」对应的 `state_next = STATE_IFG` 路径。
3. **仿真验证**：`make PARAM_ENABLE_DIC=1 WAVES=1`，在波形里定位你手算的那一帧，确认其 `start_lane`、前后 `IDLE` 字节数与你的推算一致。
4. **对比**：同样一组帧用 `make PARAM_ENABLE_DIC=0` 重跑，记录平均 IFG 字节数，量化 DIC 带来的带宽收益。

**交付物**：一张表，列出 DIC 开/关两种情况下这 10 帧各自的 `start_lane`、IFG 字节数、平均 IFG；并指出 DIC 下 `deficit_idle_count` 在哪几帧非零、如何偿还。

> 提示：若本地暂无法仿真，步骤 1–2 的纯源码手算已能完整验证你对 DIC 的理解——重点是 `deficit_idle_count` 的滚动更新那一行。

## 6. 本讲小结

- `eth_mac_10g` 是**布线层**：自身不含 DIC/IFG 逻辑，靠两段 `generate` 拼装——按 `DATA_WIDTH`（32/64）选 `axis_xgmii_rx/tx`，按 `PAUSE_ENABLE||PFC_ENABLE` 选配 `mac_ctrl_*`/`mac_pause_ctrl_*`，关闭则零面积直连。
- 64 位通路与千兆 8 位的核心差异：引入 `tkeep` 标记末字有效字节、XGMII 用 `START`/`TERM` 控制字符在一拍内定界帧、8 个并行 CRC 引擎应对变宽末拍。
- **DIC 是本讲核心**：8 字节/拍无法精确表达 12 字节 IFG。`ENABLE_DIC=1` 用 2 位 `deficit_idle_count` 跨帧记账 + lane swap（`START` 放 lane 4），让单帧 IFG 不违规、多帧平均 IFG 恰好等于配置值；`ENABLE_DIC=0` 每帧独立向上取整，简单但浪费带宽。
- IFG 计算公式 `max(cfg_ifg,12) - ifg_offset + (swap?4:0) + deficit` 四项分别对应：基准、FCS 收尾拍已贡献的 IDLE、lane swap 偏移、累计亏空。
- PTP/PFC/PAUSE 接口与千兆 MAC 同源；10G 特有点是 `cfg_quanta_step=(DATA_WIDTH*256)/512` 按位宽换算（64 位时为 32），以及 PTP 时间戳在 lane swap 时做半拍补偿。
- `tb/eth_mac_10g/test_eth_mac_10g.py` 的 `run_test_tx_alignment` 提供了 DIC 的权威 Python 参考模型，是验证理解的最佳依据。

## 7. 下一步学习建议

- **向下钻 PHY**：`eth_mac_10g` 对外是 XGMII，但真实 10G/25G 链路用 64b/66b 编码走 SERDES。下一讲 [u10-l1](u10-l1-64b66b-baser.md) 讲 `xgmii_baser_enc_64`/`dec_64` 如何把 XGMII 进一步编码成 64b/66b 块，届时你会看到本讲的 `START`/`TERM`/`IDLE` 控制字符如何映射成控制块类型。
- **看 MAC/PHY 合一**：[rtl/eth_mac_phy_10g.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_phy_10g.v) 把 `eth_mac_10g` 与 `eth_phy_10g` 合并成一个对外是 SERDES 的顶层，参数（含 `ENABLE_DIC`）与本讲完全对应，可作为综合实践的综合目标。
- **复习千兆对照**：若对布线层、PAD 填充、PTP 旁带仍有疑问，回看 [u4-l3](u4-l3-eth-mac-1g-core.md) 与 [u4-l2](u4-l2-mac-flow-control.md)，10G 版几乎是千兆版的宽位宽镜像。
