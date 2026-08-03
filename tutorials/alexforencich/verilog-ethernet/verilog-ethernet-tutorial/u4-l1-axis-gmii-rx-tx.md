# GMII/MII 与 AXI-Stream 互转

## 1. 本讲目标

本讲精读千兆以太网 MAC 最底层的一对模块：`axis_gmii_rx` 与 `axis_gmii_tx`。它们是「物理层线缆上的比特」与「FPGA 内部 AXI-Stream 帧」之间的翻译器。

学完后你应当能够：

- 说清 GMII/MII 物理信号（`rxd`/`rx_dv`/`rx_er`、`txd`/`tx_en`/`tx_er`）与 AXI-Stream 信号（`tdata`/`tvalid`/`tlast`/`tuser`）的一一映射关系。
- 解释 `clk_enable`（时钟使能分频）与 `mii_select`（半字节模式）这两根控制线如何用同一个 125 MHz 时钟覆盖 10 / 100 / 1000 Mbps 三档速率。
- 读懂接收侧如何用一条 5 级延时流水线同时完成「剥离前导码/SFD」「逐字节累加 CRC」「帧尾比对 FCS」「报告坏帧」。
- 读懂发送侧的 `IDLE→PREAMBLE→PAYLOAD→LAST→PAD→FCS→IFG` 七状态机如何重组一帧（含前导码、SFD、可选填充、FCS、帧间隔）。
- 用项目自带的 cocotb + cocotbext-eth 测试平台实际驱动一次 `axis_gmii_rx`。

## 2. 前置知识

在进入源码前，先建立三块直觉。

**第一，GMII/MII 是什么。** GMII（Gigabit Media Independent Interface）是 MAC 与 PHY 之间的 8 位并行接口：每个时钟周期搬 1 字节数据，时钟 125 MHz，故 8 bit × 125 MHz = 1000 Mbps。MII（Media Independent Interface）是它的低速、4 位（半字节 nibble）版本，用于 10/100 Mbps。二者信号名几乎一致，只是数据宽度与速率不同。本讲的两个模块用 `DATA_WIDTH=8` 的同一套 RTL，靠两根控制线在两种接口间切换，因此严格说是「GMII/MII 双模」。

**第二，一根线上的一帧长什么样。** 以太网在线路上并不是「上来就是数据」，而是：

```
前导码(7 字节 0x55) | SFD(1 字节 0xD5) | 目的MAC | 源MAC | 类型 | 载荷 | FCS(4 字节) | IFG(帧间隔)
```

前导码 + SFD 是「起拍信号」，让接收方锁定位、对齐字节边界；FCS 是上一讲（u2-l2）讲的 CRC-32 帧校验；IFG 是帧与帧之间的强制空闲。`axis_gmii_rx`/`tx` 的核心工作就是在线路格式（含前导码/SFD/FCS）与 AXI-Stream 帧（只有 MAC 头 + 载荷）之间互转。

**第三，承接 u1-l3 的 AXI-Stream 约定与 u2-l2 的 FCS 知识。** 握手规则（`tvalid` & `tready` 同时为 1 才传一拍）、`tlast` 标帧尾、`tuser` 标坏帧、CRC-32 的「初值 `0xFFFFFFFF`、逐字节回送、末尾取反」流程，本讲不再重复，直接套用。一个关键差异先记住：**`axis_gmii_rx` 没有 `tready`（物理线缆按线速来，无法反压），而 `axis_gmii_tx` 有 `tready`（可以从 AXI 源拉数据，拉不到就报 underflow）**。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
|------|------|----------|
| [rtl/axis_gmii_rx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v) | GMII/MII 信号 → AXI-Stream 帧的接收器 | 主角之一 |
| [rtl/axis_gmii_tx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v) | AXI-Stream 帧 → GMII/MII 信号的发送器 | 主角之二 |
| [rtl/lfsr.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/lfsr.v) | 通用并行 LFSR/CRC 引擎（u2-l1） | 两模块都例化它算 CRC-32 |
| [rtl/eth_mac_1g.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v) | 千兆 MAC 顶层 | 展示这两个模块的真实集成位置 |
| [tb/axis_gmii_rx/test_axis_gmii_rx.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/axis_gmii_rx/test_axis_gmii_rx.py) | cocotb 测试 | 代码实践的依据 |

---

## 4. 核心概念与源码讲解

### 4.1 GMII/MII 物理信号映射

#### 4.1.1 概念说明

这对模块最表层的工作就是「信号翻译」：把 GMII/MII 的物理信号与 AXI-Stream 的数据信号对应起来。GMII 一侧只有三根关键信号——数据、有效、错误；AXI 一侧是标准的帧流。收发两个方向各有一种错误语义需要厘清。

#### 4.1.2 核心流程

接收方向（`axis_gmii_rx`）的端口映射：

```
gmii_rxd[7:0]   ──►  每周期 1 字节线路数据
gmii_rx_dv      ──►  数据有效（高：帧内；低：帧间）
gmii_rx_er      ──►  物理层报告的错误（载波错误/发送错误）
                    │
                    ▼  逐字节累积 + 帧边界处理
m_axis_tdata    ──►  AXI 数据（仅 MAC 头 + 载荷，已剥离前导码/SFD/FCS）
m_axis_tvalid   ──►  始终随帧内字节拉高（无 tready）
m_axis_tlast    ──►  帧最后一个字节拉高
m_axis_tuser    ──►  末拍有意义：0=好帧，1=坏帧（FCS 错或 rx_er）
```

发送方向（`axis_gmii_tx`）是镜像：

```
s_axis_tdata/tvalid/tready/tlast/tuser  ──►  AXI 帧（MAC 头 + 载荷）
                    │
                    ▼  加前导码/SFD、可选填充、算并追加 FCS、插 IFG
gmii_txd[7:0]   ──►  线路数据
gmii_tx_en      ──►  发送使能（帧内为高）
gmii_tx_er      ──►  发送错误（仅在 FCS 阶段，若该帧被判为坏帧时拉高）
```

一个容易混淆的点：`gmii_rx_er` 是**对端/PHY 主动报告**的错误（比如线路上发生了冲突），而 AXI 侧的 `tuser=1` 是**本模块自己判定**的坏帧（FCS 校验失败也算）。两者都会让 `tuser=1`，但来源不同。

#### 4.1.3 源码精读

`axis_gmii_rx` 的端口声明把上面的映射写死，并且用 `initial` 断言强制 `DATA_WIDTH` 必须是 8（GMII 的物理宽度），见 [axis_gmii_rx.v:34-90](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L34-L90)：

```verilog
module axis_gmii_rx #(
    parameter DATA_WIDTH = 8,
    parameter PTP_TS_ENABLE = 0,
    parameter PTP_TS_WIDTH = 96,
    parameter USER_WIDTH = (PTP_TS_ENABLE ? PTP_TS_WIDTH : 0) + 1
)(
    input  wire [DATA_WIDTH-1:0]    gmii_rxd,
    input  wire                     gmii_rx_dv,
    input  wire                     gmii_rx_er,
    output wire [DATA_WIDTH-1:0]    m_axis_tdata,
    output wire                     m_axis_tvalid,
    output wire                     m_axis_tlast,
    output wire [USER_WIDTH-1:0]    m_axis_tuser,
    ...
```

注意 `USER_WIDTH` 的算法：坏帧标志恒占 1 位（最低位），当 `PTP_TS_ENABLE=1` 时高位拼接时间戳——这正是 u1-l3 提到的「`axis_gmii_rx` 把 `tuser` 扩展为时间戳 + 坏帧位」。具体的拼接在 [axis_gmii_rx.v:146](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L146)：

```verilog
assign m_axis_tuser = PTP_TS_ENABLE ? {ptp_ts_reg, m_axis_tuser_reg} : m_axis_tuser_reg;
```

`axis_gmii_tx` 端口方向相反，且**多了 `s_axis_tready`**（[axis_gmii_tx.v:46-91](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v#L46-L91)）。它还有一组 PTP 时间戳回送端口（`m_axis_ptp_ts` 等），用于把「这帧实际发送的时刻」报给上层——这部分留给 u11 讲，本讲先认得它们是「TX 侧的 PTP 旁路」即可。

两个模块都例化了同一个名为 `eth_crc_8` 的 `lfsr` 实例做 CRC-32（多项式 `0x4c11db7`、Galois、`REVERSE=1`），这正是 u2-l1/u2-l2 讲过的那套参数，见 [axis_gmii_rx.v:152-166](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L152-L166) 与 [axis_gmii_tx.v:162-176](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v#L162-L176)。区别只是 `data_in` 喂的字节不同：RX 喂延时后的 `gmii_rxd_d4`，TX 喂锁存的 `s_tdata_reg`。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认「RX 无 `tready`、TX 有 `tready`」这一不对称，并理解其物理原因。

**步骤**：
1. 打开 [axis_gmii_rx.v:34-82](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L34-L82)，在端口列表里找 `m_axis_*`，确认**没有** `tready`。
2. 打开 [axis_gmii_tx.v:46-91](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v#L46-L91)，确认有 `output wire s_axis_tready`。
3. 想一想：线缆上的比特是按线速到达的，MAC 拒收不了，所以 RX 只能源源不断吐到 AXI（`tvalid` 始终随帧拉高）；而 TX 是 MAC 主动从内存里取数据往外发，取不到就是「欠载（underflow）」，所以能反压。

**预期结果**：你能用一句话向别人解释为什么 RX 不能反压、TX 能。

#### 4.1.5 小练习与答案

**练习 1**：`gmii_rx_er` 和 AXI 侧 `tuser=1` 都表示「错误」，它们的来源有何不同？

**参考答案**：`gmii_rx_er` 由 PHY 或对端在物理层报告（如载波错误、冲突）；`tuser=1` 是 `axis_gmii_rx` 自己判定的坏帧结论，触发原因有两个——`gmii_rx_er` 在帧内拉高，或帧尾 FCS 校验失败。

**练习 2**：为什么 `axis_gmii_rx` 用 `initial` 断言 `DATA_WIDTH == 8`？

**参考答案**：GMII 物理接口固定是 8 位/周期，本模块的状态机、CRC 实例（`DATA_WIDTH(8)`）、MII 半字节重组逻辑都按 8 位硬编码，换宽度会破坏字节对齐与 FCS 计算，故用断言在仿真期直接报错终止。

---

### 4.2 时钟使能与 MII 选择：三模速率适配

#### 4.2.1 概念说明

一块网卡常常要同时支持 10 / 100 / 1000 Mbps（例如 RGMII PHY 会用两根速度配置脚告诉 MAC 当前链路速率）。如果为每档速率各写一套 RTL 会很浪费。本库的解法是：**所有速率共用同一个 125 MHz 时钟和同一套 8 位数据通路，靠两根控制线 `clk_enable` 与 `mii_select` 来「减速」**。

- `clk_enable`：时钟使能。为 0 时整个模块「原地踏步」（保持状态与输出不变），等于把这个周期「跳过」。于是有效工作周期变稀，速率按比例下降。
- `mii_select`：半字节模式。为 1 时，每个有效周期只处理 4 位（低半字节），高低两个半字节拼成 1 字节，等于再砍一半速率，并切到 MII 的 4 位接口风格。

#### 4.2.2 核心流程

三档速率与控制线的对应关系（125 MHz 基准时钟下）：

| 速率 | `clk_enable` | `mii_select` | 每有效周期搬的数据 | 等效 |
|------|--------------|--------------|--------------------|------|
| 1000 Mbps | 每周期都为 1 | 0 | 8 位 | 8 bit × 125 MHz |
| 100 Mbps | 每 10 周期 1 次为 1（1/10 占空） | 0 | 8 位 | 8 bit × 12.5 MHz |
| 10 Mbps | 每 5 周期 1 次为 1（1/5 占空） | 1 | 4 位 | 4 bit × 12.5 MHz ÷ 2 |

> 注：实际的 `clk_enable` / `mii_select` 由更上层的 PHY 接口模块（如 `eth_mac_1g_gmii`，见 u4-l4）根据 PHY 报告的链路速度自动生成。本模块只负责「收到这两根线就按规则减速」。

两种减速的实现都是「跳过周期」：在每个状态机的最外层先判断，若本周期不该工作就直接 `state_next = state_reg`（保持原状态），其余逻辑不执行。MII 模式还要额外处理「两个半字节拼一字节」。

MII 半字节重组（接收侧）的要点：

```
线路每有效周期来 4 位（gmii_rxd 的低半字节）
  cycle A: 收到低半字节 N_low  ──► 暂存
  cycle B: 收到高半字节 N_high ──► 拼成完整字节 {N_high, N_low}，推进流水线
```

#### 4.2.3 源码精读

`clk_enable` 与 `mii_select` 的「跳过周期」逻辑在 RX 状态机最外层，见 [axis_gmii_rx.v:182-188](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L182-L188)：

```verilog
if (!clk_enable) begin
    // clock disabled - hold state
    state_next = state_reg;
end else if (mii_select && !mii_odd) begin
    // MII even cycle - hold state
    state_next = state_reg;
end else begin
    case (state_reg) ...
```

- `!clk_enable`：本周期被「跳过」，状态机不动。
- `mii_select && !mii_odd`：MII 模式下，奇偶交替（`mii_odd` 每有效周期翻转）。只有奇周期（收齐两个半字节）才推进，偶周期保持。所以 MII 实际有效推进频率再降一半。

MII 半字节重组在 RX 的时序块里，见 [axis_gmii_rx.v:263-297](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L263-L297)：

```verilog
if (mii_select) begin
    mii_odd <= !mii_odd;
    ...
    // 把当前低半字节拼到高位，旧的高半字节挪到低位
    gmii_rxd_d0 <= {gmii_rxd[3:0], gmii_rxd_d0[7:4]};
    if (mii_odd) begin
        // 两个半字节到齐，推进深层流水线 d1..d4
        gmii_rxd_d1 <= gmii_rxd_d0; ...
    end
end
```

`{gmii_rxd[3:0], gmii_rxd_d0[7:4]}` 的效果：每来一个 4 位输入，新的低半字节进高位、上次的低半字节（存在 `d0[7:4]`）落低位，两次拼成 `{高半字节, 低半字节}` 的完整字节。SFD 检测也用同样的拼接式 `{gmii_rxd[3:0], gmii_rxd_d0[7:4]} == ETH_SFD`（[axis_gmii_rx.v:269](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L269)）。

TX 侧的减速完全对称（[axis_gmii_tx.v:223-240](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v#L223-L240)），而「拆字节为半字节」在状态机末尾统一处理，见 [axis_gmii_tx.v:398-401](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v#L398-L401)：

```verilog
if (mii_select) begin
    mii_msn_next = gmii_txd_next[7:4];   // 存高半字节
    gmii_txd_next[7:4] = 4'd0;            // 本周期只输出低半字节
end
```

下一个奇周期再通过 `gmii_txd_next = {4'd0, mii_msn_reg}`（[axis_gmii_tx.v:232](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v#L232)）把高半字节送出。收发拆/拼方向相反但互为逆运算。

#### 4.2.4 代码实践（阅读 + 推理型）

**目标**：验证三模速率分解公式。

**步骤**：
1. 打开旧版 myhdl 测试 [tb/test_axis_gmii_rx.py:169](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_axis_gmii_rx.py#L169)，注意它遍历的三种组合 `for rate, mii in [(1, 0), (10, 0), (5, 1)]`——分别对应 1000M / 100M / 10M。
2. 对每一组手算等效速率：`125 MHz × (1/rate) × (mii? 4 : 8) bit × (mii? 1/2 : 1)`。
3. 代入验证：(1,0)→125×8=1000M；(10,0)→125/10×8=100M；(5,1)→125/5×4/2=10M。

**预期结果**：三组都吻合 1000/100/10 Mbps，说明 `clk_enable` + `mii_select` 两根线足以覆盖三档速率。

> 说明：`tb/test_axis_gmii_rx.py`（顶层）是 myhdl 时代的历史遗留，当前流程不编译它（见 u1-l4），但它的 `clk_enable` 分频与三模遍历逻辑最直观，适合用来理解速率分解。实际回归用 4.3 节的 cocotb 版。

#### 4.2.5 小练习与答案

**练习 1**：如果只把 `clk_enable` 设成 1/10 占空、`mii_select=0`，得到的是哪档速率？为什么不能用这种方法下到 10 Mbps？

**参考答案**：得到 100 Mbps。因为 `clk_enable` 1/10 + 8 位/周期 = 100 Mbps。要再到 10 Mbps 必须同时 `mii_select=1` 把每周期数据砍到 4 位并隔周期推进（再除以 2），否则单靠 `clk_enable` 要 1/100 占空，分频比过大、实现不便。

**练习 2**：RX 里 `gmii_rxd_d0 <= {gmii_rxd[3:0], gmii_rxd_d0[7:4]}`，为什么用「新半字节进高位」而不是进低位？

**参考答案**：因为 TX 侧先发低半字节、再发高半字节（见 `mii_msn` 逻辑）。RX 收到第一个（低）半字节后暂存，收到第二个（高）半字节时拼到高位，才能还原成正确的 `{高, 低}` 字节顺序，否则字节里的两个半字节会颠倒，导致后续 MAC 地址、FCS 全错。

---

### 4.3 帧边界检测与 FCS 校验

#### 4.3.1 概念说明

这是本讲最精巧的部分。接收侧要在一条连续的字节流里完成四件事，且**事先不知道帧长**：

1. 找到帧头（跳过前导码，识别 SFD）。
2. 逐字节输出 MAC 头 + 载荷到 AXI。
3. 边收边累加 CRC。
4. 在帧尾比对最后 4 字节是否等于期望的 FCS，据此判定好帧/坏帧。

发送侧则是逆过程：吃进一帧 AXI（无前导码、无 FCS），补齐前导码/SFD、可选填充、计算并追加 FCS、再留出帧间隔。

#### 4.3.2 核心流程

**接收侧的三状态机**（`STATE_IDLE` → `STATE_PAYLOAD` → `STATE_WAIT_LAST`）配合一条 **5 级延时流水线** 是关键设计：

```
原始输入 gmii_rxd ──► d0 ──► d1 ──► d2 ──► d3 ──► d4
                   (1)    (2)    (3)    (4)    (5)   ← 延时拍数
                                          │
                                          └─ CRC 的 data_in 用 d4
```

巧妙之处在于「延时 5 拍」与「FCS 恰好 4 字节」的配合。当原始 `gmii_rx_dv` 拉低（帧结束）那一刻：

- 浅层 taps `d0,d1,d2,d3` 正好装着这帧的最后 4 字节——也就是 **FCS**。
- 深层 tap `d4` 装的是 FCS 之前的那个字节，而 CRC 引擎一直用 `d4` 喂入，所以此时 `crc_next` = **对「FCS 之前所有字节」算出的 CRC**。

于是接收侧的 FCS 校验归结为一行比较：

\[ \text{收到 FCS} \;=\; \{d_0, d_1, d_2, d_3\} \;\stackrel{?}{=}\; \sim \text{crc\_next} \]

这正符合以太网标准：FCS = CRC（除 FCS 外整帧）取反（承接 u2-l2 的「末尾取反」）。相等即好帧。这条流水线让模块**无需预知帧长**就能在正确时刻比对，并且 AXI 输出（用 `d4`）天然停在 FCS 之前——FCS 被自动剥离，不会出现在 AXI 帧里。

**发送侧的七状态机**（`IDLE→PREAMBLE→PAYLOAD→LAST→PAD→FCS→IFG`）：

```
IDLE      : 复位 CRC，等 AXI 首字节；来了就发前导码 0x55
PREAMBLE  : 发 7 个 0x55，第 7 字节位置发 SFD 0xD5，并预取首字节
PAYLOAD   : 每拍发 1 字节并喂 CRC；直到 tlast 或欠载 → LAST
LAST      : 发最后一个载荷字节；若太短且开填充 → PAD，否则 → FCS
PAD       : 发 0x00 补到最小帧长（默认 64），补的字节也喂 CRC
FCS       : 发 4 字节 ~crc_state（小端，低位先发），坏帧时同时拉 tx_er
IFG       : 发 cfg_ifg 个空闲字节（帧间隔），回 IDLE
```

填充的「最小帧长」用 `frame_min_count` 倒计数，初值 `MIN_FRAME_LENGTH-4-1`（[axis_gmii_tx.v:250](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v#L250)）——减 4 是为预留 FCS 的 4 字节，减 1 是因为最后一字节在 LAST 状态单独处理。

#### 4.3.3 源码精读

**RX：等待 SFD**（[axis_gmii_rx.v:190-199](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L190-L199)）。`IDLE` 态每拍复位 CRC，当延时后的信号检测到 SFD（`0xD5`）且 `cfg_rx_enable` 打开时进入 `PAYLOAD`：

```verilog
STATE_IDLE: begin
    reset_crc = 1'b1;
    if (gmii_rx_dv_d4 && !gmii_rx_er_d4 && gmii_rxd_d4 == ETH_SFD && cfg_rx_enable) begin
        state_next = STATE_PAYLOAD;
    end
end
```

注意它用的是延时 5 拍后的 `gmii_rxd_d4`——这样「进入 PAYLOAD」与「CRC 从哪个字节开始累加」在时间上严格对齐。

**RX：帧尾判定与 FCS 校验**（[axis_gmii_rx.v:200-233](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L200-L233)）。帧尾用**原始未延时**的 `!gmii_rx_dv` 检测，确保第一时间响应：

```verilog
STATE_PAYLOAD: begin
    update_crc = 1'b1;
    m_axis_tdata_next = gmii_rxd_d4;
    m_axis_tvalid_next = 1'b1;
    ...
    end else if (!gmii_rx_dv) begin
        m_axis_tlast_next = 1'b1;
        if (gmii_rx_er_d0 || ... || gmii_rx_er_d3) begin
            m_axis_tuser_next = 1'b1;            // 物理层报错 → 坏帧
        end else if ({gmii_rxd_d0, gmii_rxd_d1, gmii_rxd_d2, gmii_rxd_d3} == ~crc_next) begin
            m_axis_tuser_next = 1'b0;            // FCS 比对通过 → 好帧
        end else begin
            m_axis_tuser_next = 1'b1;            // FCS 错 → 坏帧
            error_bad_fcs_next = 1'b1;
        end
    end
```

这正是上面公式 \(\{d_0,d_1,d_2,d_3\} = \sim\text{crc\_next}\) 的代码化身。若帧内出现过 `gmii_rx_er`（载波/冲突错误），则进入 `STATE_WAIT_LAST`，丢弃剩余字节直到 `dv` 拉低（[axis_gmii_rx.v:234-242](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L234-L242)）。

`ETH_PRE`/`ETH_SFD` 两个常量定义在 [axis_gmii_rx.v:92-94](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L92-L94)。

**TX：FCS 字节发出**（[axis_gmii_tx.v:360-382](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v#L360-L382)）。4 字节按小端（低位先发）输出 `~crc_state`，与 RX 的大端拼接 `{d0,d1,d2,d3}` 互为逆运算：

```verilog
STATE_FCS: begin
    case (frame_ptr_reg)
        2'd0: gmii_txd_next = ~crc_state[7:0];
        2'd1: gmii_txd_next = ~crc_state[15:8];
        2'd2: gmii_txd_next = ~crc_state[23:16];
        2'd3: gmii_txd_next = ~crc_state[31:24];
    endcase
    gmii_tx_er_next = frame_error_reg;   // 坏帧则在 FCS 阶段拉错误线
end
```

**TX：欠载与丢帧**。当 `PAYLOAD` 中 AXI 源突然 `!s_axis_tvalid`（数据供不上）或收到 `tlast`，进入 `LAST`；若是欠载，置 `error_underflow` 并把该帧标记为错误（[axis_gmii_tx.v:310-318](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v#L310-L318)）。后续 `PAD`/`FCS`/`IFG` 状态里反复出现 `s_axis_tready_next = frame_next`（[axis_gmii_tx.v:342](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v#L342) 等）——当 `frame_next=0`（本帧已判错）时不再接收后续字节，实现「整帧丢弃」，避免把坏帧的尾巴当成新一帧。

#### 4.3.4 代码实践（cocotb 实跑型）

**目标**：用项目自带的 cocotb + cocotbext-eth 平台驱动 `axis_gmii_rx`，发一帧，验证 AXI 输出 `tlast` 正确、`tuser` 最低位（好帧标志）为 0。这正是本讲规格指定的实践。

**步骤**：

1. 按 u1-l4 装好工具链（cocotb、cocotbext-axi、cocotbext-eth、Icarus Verilog）。
2. 进入测试目录并跑仿真：

   ```bash
   cd tb/axis_gmii_rx
   make
   ```

   [Makefile](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/axis_gmii_rx/Makefile) 里 `VERILOG_SOURCES` 只列了 `axis_gmii_rx.v` 与 `lfsr.v`，`TOPLEVEL = axis_gmii_rx`——cocotb 直接把 DUT 本身当顶层驱动，**不需要手写 Verilog 例化壳**。

3. 测试主体在 [test_axis_gmii_rx.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/axis_gmii_rx/test_axis_gmii_rx.py)。关键驱动与断言（节选自 [test_axis_gmii_rx.py:54-56](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/axis_gmii_rx/test_axis_gmii_rx.py#L54-L56) 与 [test_axis_gmii_rx.py:110-130](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/axis_gmii_rx/test_axis_gmii_rx.py#L110-L130)）：

   ```python
   from cocotbext.eth import GmiiFrame, GmiiSource
   from cocotbext.axi import AxiStreamBus, AxiStreamSink

   # GMII 源：注意参数顺序是 rxd, er, dv
   self.source = GmiiSource(dut.gmii_rxd, dut.gmii_rx_er, dut.gmii_rx_dv,
                            dut.clk, dut.rst, dut.clk_enable, dut.mii_select)
   self.sink = AxiStreamSink(AxiStreamBus.from_prefix(dut, "m_axis"), dut.clk, dut.rst)

   # 发一帧：from_payload 自动加前导码+SFD 并算好 FCS
   test_frame = GmiiFrame.from_payload(test_data, tx_complete=tx_frames.append)
   await tb.source.send(test_frame)

   # 收并断言
   rx_frame = await tb.sink.recv()
   assert rx_frame.tdata == test_data        # AXI 输出 = 原 payload（前导码/SFD/FCS 已剥离）
   assert rx_frame.tuser & 1 == 0            # 好帧 → tuser 最低位为 0
   ```

**需要观察的现象**：
- 仿真应打印若干 `test_*` 用例（`TestFactory` 会自动展开多种长度、`ifg`、`enable_gen`、`mii_sel` 组合）。
- 每个用例的 `rx_frame.tdata` 与原始 `test_data` 完全相等——证明 RX 剥掉了前导码/SFD/FCS，只留帧体。
- `rx_frame.tuser & 1 == 0`——证明好帧通过 FCS 校验。
- 最后断言 `tb.sink.empty()`——证明没有多吐字节（FCS 没有泄漏进 AXI）。

**预期结果**：全部用例通过，退出码 0。

**待本地验证**：若你的环境未装 cocotbext-eth，`make` 会在 import 阶段报 `ModuleNotFoundError`；按 u1-l4 装齐依赖后重试。若想看波形，用 `make WAVES=1` 生成 `.fst` 文件。

#### 4.3.5 小练习与答案

**练习 1**：为什么 RX 检测帧尾用**原始**的 `!gmii_rx_dv`，而 SFD 检测却用**延时 5 拍后**的 `gmii_rxd_d4`？

**参考答案**：帧尾检测要尽快响应，且此刻浅层 taps `d0–d3` 恰好是 FCS、深层 `d4` 恰好是 FCS 前一字节，用原始 `dv` 能让 CRC 累加与 FCS 比对在同一拍对齐；SFD 用延时后的 `d4`，是为了让「进入 PAYLOAD 状态」与「CRC 开始按 `d4` 累加」时间严格一致，否则 CRC 会多算或少算帧头的若干字节，导致 FCS 永远校验失败。

**练习 2**：TX 在 `PAYLOAD` 状态如果 AXI 源突然停止送数（`s_axis_tvalid=0` 但还没到 `tlast`），模块怎么处理？

**参考答案**：这判为「欠载（underflow）」，置 `error_underflow_next=1'b1`、`frame_error_next=1'b1`，状态进入 `LAST`；之后该帧的 FCS 阶段会拉高 `gmii_tx_er`，把整帧标成坏帧在线路上发出，并在后续状态用 `s_axis_tready_next = frame_next` 丢弃残留字节，避免污染下一帧。

**练习 3**：把 `ENABLE_PADDING` 设为 0 会怎样？

**参考答案**：`STATE_LAST` 里 `if (ENABLE_PADDING && frame_min_count_reg)` 条件不再成立，短帧不再走 `PAD` 状态补零，直接进 `FCS`。于是短于 64 字节的帧会原样发出（仍带 FCS），可能违反以太网最小帧长要求，但模块本身不报错。

---

## 5. 综合实践：背靠背验证 RX/TX 互为逆运算

把本讲三块知识串起来，做一个端到端的小验证。

**任务**：确认 `axis_gmii_tx` 与 `axis_gmii_rx` 在字节级互为逆运算——TX 加的前导码/SFD/FCS，RX 能精确剥除并复原原帧。

**思路**（两种任选其一）：

- **仿真型**：参考 [tb/axis_gmii_tx/](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/axis_gmii_tx/test_axis_gmii_tx.py) 的写法，用一个 `AxiStreamSource` 喂 `axis_gmii_tx`，把它的 GMII 输出接到一个 `GmiiSink`（或直接用 cocotbext-eth 的 `GmiiSource`/`GmiiSink` 对接两个 DUT），再把 `axis_gmii_rx` 的 AXI 输出收进 `AxiStreamSink`。发一段已知 `test_data`，断言两端 `tdata` 相等、两端 `tuser & 1 == 0`。
- **阅读型（无法跑仿真时）**：对照 [axis_gmii_tx.v:261-292](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v#L261-L292)（发前导码+SFD）与 [axis_gmii_rx.v:190-199](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L190-L199)（检 SFD 跳过前导码）；对照 [axis_gmii_tx.v:360-372](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v#L360-L372)（发 `~crc_state` 小端 4 字节）与 [axis_gmii_rx.v:220](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L220)（比对 `{d0,d1,d2,d3} == ~crc_next`），手推一遍字节如何在两者间往返。

**自检点**：
- TX 发出的 FCS 字节顺序（小端）是否与 RX 拼接 `{d0,d1,d2,d3}` 的大端序一致？（提示：发送低位先 → `d0` 是最低字节 → 拼到 32 位的低位 → 与 `~crc_state[7:0]` 对应。）
- 若人为把 TX 发出的一帧某个载荷字节翻转再送入 RX，RX 的 `tuser` 最低位应变成多少？（应为 1，且 `error_bad_fcs` 脉冲一次。）

> 待本地验证：综合实践的精确波形与断言结果需在装好 cocotbext-eth 的本地环境运行后确认。

## 6. 本讲小结

- `axis_gmii_rx`/`tx` 是 GMII/MII 物理信号（`rxd`/`dv`/`er`）与 AXI-Stream 帧之间的双向翻译器；RX 无 `tready`（线速不可反压），TX 有 `tready`（可反压，欠载报 `error_underflow`）。
- 三模速率（10/100/1000M）靠同一 125 MHz 时钟 + 两根控制线实现：`clk_enable` 分频跳过周期、`mii_select` 切 4 位半字节模式；两者组合给出 1/1、1/10、1/100 的速率比。
- MII 模式下，RX 用 `{gmii_rxd[3:0], gmii_rxd_d0[7:4]}` 把两个半字节拼成一字节，TX 用 `mii_msn` 把一字节拆成先低后高两个半字节，方向互逆。
- RX 用一条 5 级延时流水线 + 三状态机，在不知帧长的情况下同时完成剥前导码/SFD、逐字节累加 CRC、帧尾比对 `{d0,d1,d2,d3} == ~crc_next`、报告坏帧；FCS 自动剥离，不出现在 AXI 输出里。
- TX 用七状态机（IDLE/PREAMBLE/PAYLOAD/LAST/PAD/FCS/IFG）重组完整线路帧：补前导码+SFD、可选填充到最小帧长、追加小端 `~crc_state` 4 字节 FCS、留 `cfg_ifg` 帧间隔。
- 两个模块都内嵌同一个 `eth_crc_8`（`lfsr` 实例）做 CRC-32，直接承接 u2-l1/u2-l2 的参数与「末尾取反」约定；它们被 `eth_mac_1g` 顶层在 [eth_mac_1g.v:205-262](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L205-L262) 直接例化。

## 7. 下一步学习建议

- **u4-l2 MAC 控制帧与流量控制**：在 GMII 之上再叠一层——MAC 控制帧（PAUSE/PFC）的解析与生成。
- **u4-l3 eth_mac_1g 核心千兆 MAC**：看 `eth_mac_1g` 如何把本讲的 `axis_gmii_rx`/`tx` 与控制帧、PAUSE、FCS、PTP 时间戳等子模块组装成完整 MAC，本讲的两个实例就在 [eth_mac_1g.v:205-262](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L205-L262) 被装配。
- **u4-l4 PHY 接口与时钟**：了解 `clk_enable`/`mii_select` 这两根线到底是谁、根据什么生成的（PHY 速度协商 → `gmii_phy_if`/`mii_phy_if` 的源同步时钟与 IO 原语）。
- 若你对 10G 感兴趣，可对照 [axis_xgmii_rx_64.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_rx_64.v) 预习 64 位 XGMII 版本（u9 单元），它与本讲的 8 位 GMII 版本是「同构不同宽」的关系。
