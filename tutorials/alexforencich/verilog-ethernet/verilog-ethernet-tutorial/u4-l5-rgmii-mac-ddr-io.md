# RGMII MAC 与 DDR IO

## 1. 本讲目标

本讲讲解 verilog-ethernet 如何用 **DDR（Double Data Rate，双边沿）IO 原语** 实现 RGMII 物理接口，并把它封装成一个完整的三模（10/100/1000M）千兆 MAC。

学完后你应该能够：

- 说清 RGMII 协议为什么能在「4 根数据线 + 1 根控制线」上跑出和 8 位 GMII 一样的吞吐量，理解时钟双边沿传输的机制。
- 读懂 `iddr` / `oddr` / `ssio_ddr_in` 三个 DDR IO 原语，理解它们如何把「时钟上下沿各一个半字节」还原成「一个完整字节」、或反向把字节拆成半字节输出。
- 跟着 `rgmii_phy_if` 走一遍 RX 侧「4 位 → 8 位 GMII」的还原过程，并解释 `clk90`（90 度移相时钟）为什么能把发送时钟边沿对准数据眼的中央。
- 理解 `eth_mac_1g_rgmii` 顶层如何把 `rgmii_phy_if` 与 `eth_mac_1g` 拼起来、如何在运行中自动检测速率。

本讲承接 [u4-l3 eth_mac_1g 核心千兆 MAC](u4-l3-eth-mac-1g-core.md)（GMII MAC 本体）与 [u4-l4 PHY 接口与时钟](u4-l4-phy-if-and-tri-mode.md)（源同步 IO 与三模适配）。在那些讲里，`eth_mac_1g` 已经能处理标准的 8 位 GMII 信号、`gmii_phy_if` 已经讲过源同步时钟缓冲；本讲把这套机制搬到引脚数减半的 RGMII 接口上。

## 2. 前置知识

### GMII 的引脚负担

在 [u4-l1](u4-l1-axis-gmii-rx-tx.md) 中我们见过 GMII：发送/接收各需要 8 根数据线（`txd[7:0]`/`rxd[7:0]`）、外加 `tx_en`/`tx_er`/`rx_dv`/`rx_er` 等控制线、再各配一根 125 MHz 时钟。千兆速率下，每根数据线跑 125 MHz，这给 PCB 布线和 FPGA 引脚都带来压力。

### DDR（Double Data Rate）的基本思想

SDR（Single Data Rate）只在时钟**上升沿**采样一次；DDR 在**上升沿和下降沿各采样一次**，等于让一根线在同样的时钟频率下吞吐量翻倍。DDR-3 内存、千兆以太网 RGMII 都靠这个思想。

\[
\text{吞吐量} = \text{位宽} \times \text{每周期采样次数} \times \text{时钟频率}
\]

对 RGMII：位宽 4、每周期采样 2 次（双边沿）、时钟 125 MHz：

\[
4 \times 2 \times 125\,\text{MHz} = 1000\,\text{Mbps} = 1\,\text{Gbps}
\]

于是 4 根数据线 × 双边沿 ＝ 每周期 8 位 ＝ 与 8 位 GMII 完全相同的有效带宽，却只用一半引脚。

### 源同步（Source Synchronous）时钟

「源同步」指**发送方同时送出数据和采样该数据用的时钟**（时钟和数据从同一个源一起上路）。RGMII 就是源同步的：PHY 芯片给 FPGA 同时送来 `rgmii_rx_clk` 与 `rgmii_rxd`。接收方用收到的时钟去采收到的数据，因为两者同源、相位关系固定，能避开「全局时钟与数据到达时间不一致」的老问题。这个概念在 [u4-l4](u4-l4-phy-if-and-tri-mode.md) 的 `ssio_sdr_in`（SSIO = Source Synchronous IO）里已出现过，本讲遇到的是它的 DDR 版本 `ssio_ddr_in`。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
|------|------|----------|
| [rtl/iddr.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/iddr.v) | 通用 DDR **输入**寄存器：把一根双边沿信号拆成两根并行输出 | RX 侧最底层原语 |
| [rtl/oddr.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/oddr.v) | 通用 DDR **输出**寄存器：把两根并行信号合并到一根双边沿引脚 | TX 侧最底层原语 |
| [rtl/ssio_ddr_in.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ssio_ddr_in.v) | 源同步 DDR 输入：在 `iddr` 前面套一层时钟缓冲 | RX 收数据 + 派生 MAC 时钟 |
| [rtl/rgmii_phy_if.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/rgmii_phy_if.v) | RGMII 收发桥：用上述原语在 4 位 RGMII 与 8 位 GMII 间互转 | 把协议落到硬件 |
| [rtl/eth_mac_1g_rgmii.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_rgmii.v) | 三模 RGMII MAC 顶层：拼 `rgmii_phy_if` + `eth_mac_1g`，含速率自检 | 本讲顶层封装 |

阅读建议：先看 `iddr.v` 头部的波形注释（它一图说清 DDR 输入在做什么），再看 `rgmii_phy_if.v` 如何把 5 位宽的 DDR 实例接成「数据 + 控制」，最后看 `eth_mac_1g_rgmii.v` 的布线。

## 4. 核心概念与源码讲解

### 4.1 RGMII 协议与双边沿数据

#### 4.1.1 概念说明

RGMII（Reduced Gigabit Media Independent Interface）是 GMII 的「瘦身版」：把 8 位数据砍成 4 位，把 `tx_en`/`tx_er` 两根控制线合并成 1 根 `ctl`，靠 DDR 在一个时钟周期内分两次把这 8 位信息和控制位都传完。

RGMII 在一个时钟周期内传输的内容（以发送为例）：

| 时刻 | `rgmii_txd[3:0]` | `rgmii_tx_ctl` |
|------|------------------|----------------|
| 上升沿 | 字节低半字节 `txd[3:0]` | `tx_en`（发送使能） |
| 下降沿 | 字节高半字节 `txd[7:4]` | `tx_en ^ tx_er`（使能异或错误） |

注意控制线的巧妙设计：上升沿传 `tx_en`，下降沿传 `tx_en ^ tx_er`。接收端只要把这两次采样**异或**起来，就能还原出 `tx_er`：

\[
(\,tx\_en\,) \oplus (\,tx\_en \oplus tx\_er\,) = tx\_er
\]

这样只用 1 根控制线（而非 GMII 的 2 根）就同时携带了使能与错误两个信号。接收侧同理：`rx_ctl` 上升沿是 `rx_dv`，下降沿是 `rx_dv ^ rx_er`，异或还原 `rx_er`。

#### 4.1.2 核心流程：一拍还原成一个 GMII 字节

接收方向，每个 `rgmii_rx_clk` 周期要做的事：

1. 上升沿采样：得到低半字节 `rxd[3:0]` 与 `rx_dv`。
2. 下降沿采样：得到高半字节 `rxd[7:4]` 与 `rx_dv ^ rx_er`。
3. 把两次采样的半字节拼成完整 8 位 GMII 字节 `{rxd[7:4], rxd[3:0]}`。
4. 用异或还原 `rx_er`，直接取 `rx_dv`。
5. 把这个字节交给 `eth_mac_1g` 里的 `axis_gmii_rx`（见 [u4-l1](u4-l1-axis-gmii-rx-tx.md)）做帧边界检测与 FCS 校验。

发送方向是逆过程：MAC 给出 8 位 GMII 字节与 `tx_en`/`tx_er`，PHY 接口在上升沿送出低半字节 + `tx_en`、下降沿送出高半字节 + `tx_en ^ tx_er`。

#### 4.1.3 代码实践：手算一个 RGMII 字节

**实践目标**：用纸笔验证「4 位 × 双边沿 = 8 位」与「ctl 异或还原 er」两个机制。

**操作步骤**：

1. 假设 GMII 侧要发送字节 `0x6A`（二进制 `0110_1010`），`tx_en=1`，`tx_er=0`。
2. 拆半字节：低半字节 `txd[3:0] = 0xA`，高半字节 `txd[7:4] = 0x6`。
3. 算 `ctl`：上升沿 `ctl = tx_en = 1`；下降沿 `ctl = tx_en ^ tx_er = 1 ^ 0 = 1`。
4. 接收端拼回：`{高半字节, 低半字节} = {0x6, 0xA} = 0x6A` ✓。
5. 接收端还原 `er`：`ctl_上升 ^ ctl_下降 = 1 ^ 1 = 0 = tx_er` ✓。

**需要观察的现象**：当发送方把 `tx_er` 置 1 时，下降沿 `ctl` 应翻转为 0，接收端异或结果变为 1，正确还原错误标志。

**预期结果**：无论 `tx_en`/`tx_er` 取何值，拼接与异或都能无损还原原字节与控制信号——这就是 RGMII 用一半引脚做到 GMII 全部信息量的核心。

#### 4.1.4 小练习与答案

**练习 1**：GMII 在 125 MHz 下用 8 位 SDR 达到 1 Gbps。RGMII 把数据线减到 4 位，为何还能保持 1 Gbps？
**答**：RGMII 用 DDR，每个时钟周期采样两次（上升沿 + 下降沿），4 位 × 2 = 每周期 8 位有效数据，与 GMII 等效带宽相同。

**练习 2**：为什么 RGMII 把 `tx_en` 和 `tx_er` 编码到同一根 `ctl` 线上，而不是像 GMII 那样各占一根？
**答**：上升沿传 `tx_en`、下降沿传 `tx_en ^ tx_er`，接收端两者异或即可还原 `tx_er`，省下一根控制引脚，且不丢失任何信息。

### 4.2 DDR IO 原语：iddr / oddr

`iddr` 与 `oddr` 是两个**跨厂商的薄封装**：上层只看到「输入拆二 / 输出合一」的统一接口，内部用 `generate` 在编译期按 `TARGET` 选 Xilinx（`IDDR`/`ODDR` 原语）、Altera（`altddio_in`/`altddio_out`）或 GENERIC（纯 RTL）。GENERIC 分支保证仿真器（iverilog）也能跑——这点在本讲实践中很关键。

#### 4.2.1 iddr：DDR 输入寄存器

`iddr` 接收一根在时钟双边沿都会变化的数据线 `d`，输出两根并行信号 `q1`、`q2`。文件头部的波形注释把语义讲得最清楚：

```
              _____       _____       _____       _____       ____
    clk  ____/     \_____/     \_____/     \_____/     \_____/
    d    ___X_D0__X_D1__X_D2__X_D3__X_D4__X_D5__X_D6__X_D7__X___
    q1   _______X___________X__D0_____X__D2_____X__D4_____X___
    q2   _______X___________X__D1_____X__D3_____X__D5_____X___
```

[rtl/iddr.v:L54-L66](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/iddr.v#L54-L66) —— 这段注释说明：输入 `d` 在每个半周期换一次值（D0、D1、D2…），`q1` 取上升沿采样值（D0、D2、D4…偶数），`q2` 取下降沿采样值（D1、D3、D5…奇数），两者都被打一拍后在同一个上升沿对齐输出。所以一个时钟周期后，`{q2, q1}` 就是这一周期内 `d` 上先后出现的两位数据。

**GENERIC 实现**（仿真路径）用三个 `always` 块实现这种「同沿对齐」：

[rtl/iddr.v:L143-L157](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/iddr.v#L143-L157) —— `d_reg_1` 在上升沿采 `d`（抓到偶数拍 D0、D2…），`d_reg_2` 在下降沿采 `d`（抓到奇数拍 D1、D3…），随后在下一个上升沿把两者一起搬进 `q_reg_1`/`q_reg_2`，从而让 `q1`、`q2` 同拍出现。Xilinx 路径（[rtl/iddr.v:L72-L87](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/iddr.v#L72-L87)）用 `IDDR` 原语配 `SAME_EDGE_PIPELINED` 模式达到同样语义，只是落在专用的 IOB（IO Block）寄存器上，时序更紧。

#### 4.2.2 oddr：DDR 输出寄存器

`oddr` 是 `iddr` 的逆运算：输入两根并行信号 `d1`、`d2`，合并到一根双边沿输出 `q`——上升沿送 `d1`、下降沿送 `d2`：

```
    d1   ___X____D0_____X____D2_____X____D4_____X____D6_____X___
    d2   ___X____D1_____X____D3_____X____D5_____X____D7_____X___
    q    _____X_D0__X_D1__X_D2__X_D3__X_D4__X_D5__X_D6__X_D7___
```

[rtl/oddr.v:L54-L66](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/oddr.v#L54-L66) —— 注释说明 `d1` 的内容（D0、D2…）出现在 `q` 的上升沿段，`d2` 的内容（D1、D3…）出现在下降沿段。GENERIC 实现（[rtl/oddr.v:L120-L140](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/oddr.v#L120-L140)）：上升沿把 `d1` 装进 `q_reg`、下降沿把先前锁存的 `d2` 装进 `q_reg`，于是输出引脚在两个边沿交替呈现 `d1`/`d2`。

#### 4.2.3 代码实践：在仿真里观察 iddr 拆位

**实践目标**：确认 `iddr` 把一根 DDR 输入正确拆成 `q1`（上升沿样本）与 `q2`（下降沿样本）。

**操作步骤**（源码阅读型，无需新工程）：

1. 打开 [rtl/iddr.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/iddr.v)，定位 GENERIC 分支（`TARGET != "XILINX"` 且 `!= "ALTERA"`）。
2. 跟踪一次数据：设 `clk` 上升沿时 `d=D0`，则 `d_reg_1 <= D0`；随后下降沿 `d=D1`，则 `d_reg_2 <= D1`；再下一个上升沿 `q_reg_1 <= d_reg_1(=D0)`、`q_reg_2 <= d_reg_2(=D1)`。
3. 结论：`q1=D0`、`q2=D1`，与头部波形注释一致。

**预期结果**：`q1` 永远是上升沿那一拍的数据，`q2` 永远是下降沿那一拍的数据，且两者延迟一个周期同拍输出。若无法运行仿真，可标注「待本地验证」后用上面的纸笔推演代替。

#### 4.2.4 小练习与答案

**练习 1**：为什么 `iddr` 不直接在下降沿就把 `q2` 输出，而要再多打一拍、让 `q1`/`q2` 在同一上升沿对齐？
**答**：这是 Xilinx `SAME_EDGE_PIPELINED` 语义。让 `q1`/`q2` 同拍出现，下游逻辑就能在一个普通上升沿里同时拿到「低半字节 + 高半字节」拼成一个完整字节，而不必跨时钟沿处理，时序更简单。

**练习 2**：`iddr`/`oddr` 的 GENERIC 分支为什么重要？
**答**：它用纯 RTL（普通 `always` 块）实现 DDR 语义，使 iverilog 等开源仿真器无需厂商原语库即可仿真，这正是 `tb/eth_mac_1g_rgmii` 能跑起来的前提。

### 4.3 ssio_ddr_in 与 rgmii_phy_if：用 DDR 实现 RGMII 收发

有了 `iddr`/`oddr` 这两个原语，`rgmii_phy_if` 把 RGMII 协议落地：RX 用 `ssio_ddr_in`（内含 `iddr`），TX 用 `oddr`。

#### 4.3.1 概念说明

`ssio_ddr_in`（Source Synchronous IO DDR input）在 `iddr` 之前加了一层**时钟缓冲**：源同步时钟从 PHY 进来，既要驱动 IOB 里的输入触发器（`clk_io`）、又要派生出给 MAC 内部逻辑用的时钟（`output_clk`）。Xilinx 上按 `CLOCK_INPUT_STYLE` 选 `BUFG`/`BUFR`/`BUFIO`；GENERIC 上全部退化为 `assign` 直通，保证可仿真。

`rgmii_phy_if` 则把 5 位宽（4 位数据 + 1 位控制）的 DDR 通道接好：RX 把 5 位 DDR 输入还原成 8 位 GMII 字节 + `dv` + `er`；TX 把 GMII 字节拆成两个半字节经 `oddr` 送出，并用 `clk90` 给发送时钟做 90 度移相。

#### 4.3.2 RX 核心流程：4 位 → 8 位还原

[rtl/ssio_ddr_in.v:L63-L135](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ssio_ddr_in.v#L63-L135) —— 时钟缓冲 `generate` 块。XILINX 分支按 `CLOCK_INPUT_STYLE` 例化 `BUFG`/`BUFIO`/`BUFR`；`else`（含 GENERIC 与 SIM）分支用 `assign` 把 `input_clk` 直通为 `clk_io` 与 `output_clk`，使仿真可跑。

[rtl/ssio_ddr_in.v:L137-L147](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ssio_ddr_in.v#L137-L147) —— 把缓冲后的时钟 `clk_io` 喂给 `iddr`，对 `WIDTH` 位输入做 DDR 拆分，输出 `q1`（上升沿样本）与 `q2`（下降沿样本）。

`rgmii_phy_if` 以 `WIDTH=5` 例化它，一次处理「4 位数据 + 1 位控制」：

[rtl/rgmii_phy_if.v:L90-L103](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/rgmii_phy_if.v#L90-L103) —— RX 实例。`input_d` 是 `{phy_rgmii_rxd, phy_rgmii_rx_ctl}`（5 位）；`output_q1` 还原出 `{mac_gmii_rxd[3:0], rgmii_rx_ctl_1}`（上升沿：低半字节 + 控制位 1），`output_q2` 还原出 `{mac_gmii_rxd[7:4], rgmii_rx_ctl_2}`（下降沿：高半字节 + 控制位 2）。注意位拼接顺序：`q2` 提供高半字节、`q1` 提供低半字节，合起来正好是完整 GMII 字节。

[rtl/rgmii_phy_if.v:L105-L106](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/rgmii_phy_if.v#L105-L106) —— 控制信号还原：`mac_gmii_rx_dv = rgmii_rx_ctl_1`（上升沿控制位即 `rx_dv`），`mac_gmii_rx_er = rgmii_rx_ctl_1 ^ rgmii_rx_ctl_2`（两次控制位异或即 `rx_er`）。这正是 4.1.1 里推导的异或还原。

整条 RX 链路可画成：

```
rgmii_rxd[3:0] ┐
               ├─► ssio_ddr_in(WIDTH=5) ─► iddr ─► q1={rxd[3:0], ctl_1}
rgmii_rx_ctl   ┘                                   q2={rxd[7:4], ctl_2}
rgmii_rx_clk ─► 时钟缓冲 ─► output_clk(=rx_clk 给 MAC)
                                              │
                            ┌─────────────────┴────────────────┐
                            ▼                                  ▼
              mac_gmii_rxd = {rxd[7:4], rxd[3:0]}   rx_dv=ctl_1, rx_er=ctl_1^ctl_2
```

#### 4.3.3 TX 核心流程与 clk90 的作用

发送方向用组合逻辑（按速率做半字节拆分），再经两个 `oddr` 把数据与时钟送出。

[rtl/rgmii_phy_if.v:L190-L198](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/rgmii_phy_if.v#L190-L198) —— 千兆（`speed == 2'b10`）分支：`rgmii_txd_1 = mac_gmii_txd[3:0]`（上升沿送低半字节），`rgmii_txd_2 = mac_gmii_txd[7:4]`（下降沿送高半字节），`rgmii_tx_ctl_1 = mac_gmii_tx_en`，`rgmii_tx_ctl_2 = mac_gmii_tx_en ^ mac_gmii_tx_er`。与 RX 还原严格互逆。10M/100M 分支则把低半字节复制到两个边沿、并配合 `gmii_clk_en` 选通，实现降速（见 4.4）。

[rtl/rgmii_phy_if.v:L204-L226](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/rgmii_phy_if.v#L204-L226) —— 两个 `oddr` 实例。**关键差别在于时钟**：数据 `oddr`（`data_oddr_inst`）用 `clk`（即 `gtx_clk`，125 MHz）作时钟；而**时钟** `oddr`（`clk_oddr_inst`）用 `USE_CLK90 == "TRUE" ? clk90 : clk` 作时钟——默认用移相 90° 的 `clk90`。

**clk90 的作用**：`oddr` 的输出在它自身时钟的两个边沿翻转。数据 `oddr` 由 `clk` 驱动，所以 `rgmii_txd` 在 `clk` 的边沿变化；时钟 `oddr` 由 `clk90`（`clk` 延迟 90°，即四分之一周期）驱动，所以 `rgmii_tx_clk` 的边沿比数据边沿**晚四分之一周期**出现。结果是：发送时钟的边沿落在每段数据眼的**正中央**，远端的 PHY 用这个时钟采数据时，建立/保持时间余量最大。这就是 RGMII 规范里的「内部延迟」（RGMII-ID）约定。`USE_CLK90` 设为 `"FALSE"` 时改为同相时钟，留给不支持内部延迟的对端 PHY。

> 时序直觉（千兆，`clk` 周期 8 ns，`clk90` 落后 `clk` 2 ns）：
> ```
> clk     ─┐─1─┐─0─┐─1─┐─0─┐─1─     数据边沿（t=0,2,4...ns）
> clk90     └─延迟2ns─┐─1─┐─0─┐─1─   发送时钟边沿（t=2,4,6...ns）
> rgmii_txd 在 clk 边沿刷新 ─► 数据稳定段 = [0..4)ns
> rgmii_tx_clk 在 clk90 边沿翻转 ─► 采样点在 2ns、6ns ≈ 数据眼中央
> ```

[rtl/rgmii_phy_if.v:L228-L230](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/rgmii_phy_if.v#L228-L230) —— `mac_gmii_tx_clk = clk`、`mac_gmii_tx_clk_en = gmii_clk_en`：把内部 TX 时钟与字节使能交回给 MAC（千兆下 `gmii_clk_en=1`，低速率下选通）。

#### 4.3.4 代码实践：跑 RGMII testbench 并定位还原点

**实践目标**：用现成的 cocotb testbench 端到端验证 RGMII 收发，并在源码中指认「4 位 → 8 位」还原的确切位置。

**操作步骤**：

1. 按 [u1-l4](u1-l4-testbench-and-simulation.md) 配好 cocotb + cocotbext-eth + iverilog。
2. 进入 `tb/eth_mac_1g_rgmii`，运行 `make`（或 `pytest tb/eth_mac_1g_rgmii`）。该目录的 [Makefile](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g_rgmii/Makefile#L32-L40) 用 `VERILOG_SOURCES` 显式列出了 `eth_mac_1g_rgmii.v`、`iddr.v`、`oddr.v`、`ssio_ddr_in.v`、`rgmii_phy_if.v`、`eth_mac_1g.v`、`axis_gmii_rx.v`、`axis_gmii_tx.v`、`lfsr.v` 九个文件——正好是本讲的完整依赖链。
3. testbench 用 `cocotbext.eth` 的 `RgmiiPhy` 驱动 RGMII 引脚（见 [test_eth_mac_1g_rgmii.py:L47-L48](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g_rgmii/test_eth_mac_1g_rgmii.py#L47-L48)）：它把一个 `GmiiFrame` 拆成 DDR 半字节从 `rgmii_rxd`/`rgmii_rx_ctl`/`rgmii_rx_clk` 注入 DUT，再从 AXI 输出口 `rx_axis_*` 收回整帧并断言 `tdata` 与原载荷一致、`tuser==0`（好帧）。
4. 在 [rgmii_phy_if.v:L101-L102](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/rgmii_phy_if.v#L101-L102) 处指认还原点：`output_q1` 给出 `mac_gmii_rxd[3:0]`（上升沿半字节），`output_q2` 给出 `mac_gmii_rxd[7:4]`（下降沿半字节），二者拼成完整字节。
5. 在 [rgmii_phy_if.v:L210](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/rgmii_phy_if.v#L210) 处确认 clk90：时钟 `oddr` 的 `.clk(USE_CLK90 == "TRUE" ? clk90 : clk)`。

**需要观察的现象**：testbench 在 `speed=1000e6` 下断言 `dut.speed == 2`（[test...py:L99-L104](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g_rgmii/test_eth_mac_1g_rgmii.py#L99-L104)），且从 AXI 口收到的帧与注入帧逐字节相等。

**预期结果**：所有用例通过（PASS），证明 DDR 通路把 4 位 RGMII 正确还原成了 8 位 GMII，再被 MAC 解析成 AXI 帧。若环境未装好，可标注「待本地验证」并先做 4.3.5 的源码跟踪。

#### 4.3.5 小练习与答案

**练习 1**：在 `rgmii_phy_if` 的 RX 实例里，为什么 `output_q2` 接的是 `mac_gmii_rxd[7:4]`（高半字节）而不是 `[3:0]`？
**答**：`q2` 是下降沿样本，对应 RGMII 协议里下降沿传输的高半字节 `rxd[7:4]`；`q1` 是上升沿样本，对应低半字节 `rxd[3:0]`。拼接 `{q2 高位, q1 低位}` 才能还原原始字节顺序。

**练习 2**：把 `USE_CLK90` 从 `"TRUE"` 改成 `"FALSE"`，发送时钟与数据的关系会怎样变化？对端 PHY 会遇到什么风险？
**答**：时钟 `oddr` 改用 `clk`，发送时钟边沿与数据边沿对齐（同相）。此时发送时钟边沿不再落在数据眼中央，对端 PHY 在数据跳变瞬间采样，建立/保持余量骤减，可能采错。因此只有当对端 PHY 自带内部延迟补偿时才用 `"FALSE"`。

**练习 3**：`ssio_ddr_in` 里 `BUFG`/`BUFR`/`BUFIO` 三种缓冲分别派生什么？GENERIC 下如何退化？
**答**：它们都是 Xilinx 时钟缓冲原语，分工是给 IOB 输入触发器（`clk_io`，常用 `BUFIO`）和给内部逻辑（`output_clk`，常用 `BUFG`/`BUFR`）提供低偏斜时钟。GENERIC/SIM 下没有这些原语，分支退化为 `assign clk_io = input_clk; output_clk = input_clk;`，保证仿真可跑。

### 4.4 eth_mac_1g_rgmii：三模 RGMII MAC 封装

#### 4.4.1 概念说明

`eth_mac_1g_rgmii` 是一个「布线层」顶层（与 [u4-l3](u4-l3-eth-mac-1g-core.md) 的 `eth_mac_1g` 角色、[u4-l4](u4-l4-phy-if-and-tri-mode.md) 的 `eth_mac_1g_gmii` 角色同类）。它把两件事拼在一起：

1. `rgmii_phy_if`：负责 RGMII 引脚 ↔ 8 位 GMII 的 DDR 转换与时钟。
2. `eth_mac_1g`：负责 GMII ↔ AXI-Stream 的成帧、FCS、流量控制。

此外它还内置**速率自检**：测量 `rgmii_rx_clk` 相对 125 MHz `gtx_clk` 的频率比，自动判定 10/100/1000M 并生成 `speed` 与 `mii_select`。这承接 [u4-l4](u4-l4-phy-if-and-tri-mode.md) 三模适配的思想，但实现位置不同：`eth_mac_1g_gmii` 靠 `gmii_phy_if` 的 `BUFGMUX` 换时钟；这里时钟始终用收到的 `rgmii_rx_clk`（缓冲后），降速靠 `rgmii_phy_if` 内部的时钟分频 + `clk_en` 选通 + `mii_select` 切半字节模式。

#### 4.4.2 核心流程：速率检测与跨域同步

[rtl/eth_mac_1g_rgmii.v:L128-L181](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_rgmii.v#L128-L181) —— 速率检测。`rx_prescale` 是 `rx_clk` 域里的 3 位自由计数器，其最高位经 3 级移位寄存器同步进 `gtx_clk` 域（`rx_prescale_sync`）；`rx_speed_count_1` 数 `gtx_clk` 周期数，`rx_speed_count_2` 数 `rx_prescale` 翻转次数。两个计数器谁先溢出决定速率：

- `rx_speed_count_1`（7 位）先溢出 → `rx_clk` 很慢 → **10M**，`speed=2'b00`，`mii_select=1`。
- `rx_speed_count_2`（2 位）先溢出 → 看 `rx_speed_count_1[6:5]`：非零 → **100M**（`speed=2'b01`），为零 → **1000M**（`speed=2'b10`）；10M/100M 时 `mii_select=1`，千兆时为 0。

[rtl/eth_mac_1g_rgmii.v:L114-L126](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_rgmii.v#L114-L126) —— `mii_select` 是在 `gtx_clk` 域算出来的，而 MAC 的 TX/RX 分别跑在 `tx_clk`/`rx_clk` 域，所以各用一个 2 级同步器（`tx_mii_select_sync`/`rx_mii_select_sync`）把它安全地搬过去，取同步后的第 1 位送 MAC。

#### 4.4.3 子模块实例化

[rtl/eth_mac_1g_rgmii.v:L185-L216](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_rgmii.v#L185-L216) —— 例化 `rgmii_phy_if`，把检测到的 `speed` 传入；它把 RGMII 引脚转成 GMII 信号并交回 `mac_gmii_rxd`/`mac_gmii_rx_dv`/`mac_gmii_rx_er` 与 TX 侧的 `mac_gmii_txd`/`mac_gmii_tx_en`/`mac_gmii_tx_er`。

[rtl/eth_mac_1g_rgmii.v:L218-L252](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_rgmii.v#L218-L252) —— 例化 `eth_mac_1g`，完成 GMII ↔ AXI 转换。注意几根关键接线：

- `.rx_clk_enable(1'b1)`（[L242](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_rgmii.v#L242)）：RX 恒使能——因为 `rx_clk` 本身已是源同步时钟缓冲后的产物，速率由 PHY 决定。
- `.tx_clk_enable(mac_gmii_tx_clk_en)`（[L243](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_rgmii.v#L243)）：TX 使能来自 `rgmii_phy_if`，千兆下恒 1，10M/100M 下按分频选通。
- `.rx_mii_select(rx_mii_select_sync[1])`、`.tx_mii_select(tx_mii_select_sync[1])`（[L244-L245](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_rgmii.v#L244-L245)）：把同步后的半字节模式开关送进 MAC，让 `axis_gmii_rx`/`tx` 切到 4 位半字节拼装（详见 [u4-l1](u4-l1-axis-gmii-rx-tx.md)）。

顶层端口（[rtl/eth_mac_1g_rgmii.v:L52-L101](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_rgmii.v#L52-L101)）对外的时钟域很清晰：`gtx_clk`/`gtx_clk90` 是板载 125 MHz 全局时钟及其 90° 移相（驱动 TX DDR）；`rx_clk`/`tx_clk` 由 `rgmii_phy_if` 从 PHY 时钟与 `gtx_clk` 派生并输出；AXI 收发分别跑在 `tx_clk`/`rx_clk` 域；`speed[1:0]` 是给外部（如 LED/状态寄存器）用的速率指示。

#### 4.4.4 代码实践：对比 RGMII MAC 与 GMII MAC 的端口

**实践目标**：通过对比 `eth_mac_1g_rgmii` 与 [u4-l3](u4-l3-eth-mac-1g-core.md) 的 `eth_mac_1g`，看清「封装层」加进了什么。

**操作步骤**：

1. 打开 [rtl/eth_mac_1g_rgmii.v:L52-L101](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_rgmii.v#L52-L101) 的端口表。
2. 与 `eth_mac_1g` 的 GMII 端口（`gmii_rxd`/`gmii_rx_dv`/`gmii_txd`/`gmii_tx_en` 等 8 位接口）对比。
3. 列出 RGMII 封装新增/替换的内容：把 8 位 GMII 引脚换成 4 位 + ctl 的 RGMII 引脚（`rgmii_rxd[3:0]`、`rgmii_rx_ctl`、`rgmii_txd[3:0]`、`rgmii_tx_ctl`、`rgmii_rx_clk`、`rgmii_tx_clk`）；新增 `gtx_clk`/`gtx_clk90` 输入与 `speed[1:0]` 输出；AXI 侧端口保持不变（说明对上层而言，RGMII 与 GMII 是可互换的 PHY 接口）。

**预期结果**：你会得出结论——`eth_mac_1g_rgmii` 在 AXI 侧与 `eth_mac_1g` 完全一致，差别全在 PHY 侧（RGMII 引脚 + DDR 时钟 + 速率自检）。这解释了为何 README 把它列为「Tri-mode Ethernet RGMII MAC」而把 MAC 本体单独列出。

#### 4.4.5 小练习与答案

**练习 1**：`rx_clk_enable` 为什么直接固定为 `1'b1`，而 `tx_clk_enable` 要接 `mac_gmii_tx_clk_en`？
**答**：RX 时钟（`rx_clk`）直接来自 PHY 的源同步时钟缓冲，其频率已随对端速率而定（千兆 125 MHz、低速率更低），MAC 每个周期处理一个有效字节即可，故恒使能。TX 用的是本地 `gtx_clk`（恒 125 MHz），在 10M/100M 时必须用 `tx_clk_en` 选通（只在部分周期允许发送）来降速，所以接可变的 `mac_gmii_tx_clk_en`。

**练习 2**：为什么 `mii_select` 必须经 2 级同步器才能送进 MAC 的 TX/RX 域？
**答**：`mii_select` 在 `gtx_clk` 域由速率检测逻辑产生，而 MAC 的 TX/RX 分别跑在 `tx_clk`/`rx_clk` 域。跨时钟域传递单 bit 控制信号若不同步，可能采到亚稳态值，导致 MAC 在半字节/全字节模式间误切。2 级触发器同步器是标准做法（见 [u4-l4](u4-l4-phy-if-and-tri-mode.md) 关于跨域同步的讨论）。

## 5. 综合实践

**任务**：端到端跟踪一个字节从 RGMII 引脚到 AXI 输出的完整旅程，并解释每一跳。

**背景**：你要向一位刚学完 [u4-l1](u4-l1-axis-gmii-rx-tx.md) 的同学解释「为什么 4 根 RGMII 数据线最终能变成 MAC 里一个 8 位的 AXI 字节」。请用本讲源码画出数据通路的每一级。

**操作步骤**：

1. **起点**：假设 PHY 在 `rgmii_rx_clk` 上升沿送出 `rgmii_rxd=0xA`、`rgmii_rx_ctl=1`；下降沿送出 `rgmii_rxd=0x6`、`rgmii_rx_ctl=1`。
2. **ssio_ddr_in**（[rgmii_phy_if.v:L90-L103](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/rgmii_phy_if.v#L90-L103)）：`iddr` 在上升沿采到 `q1={0xA, 1}`、下降沿采到 `q2={0x6, 1}`（延迟一拍同拍输出）。
3. **拼接**（同处 L101-L102）：`mac_gmii_rxd = {0x6, 0xA} = 0x6A`。
4. **控制还原**（[L105-L106](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/rgmii_phy_if.v#L105-L106)）：`rx_dv = 1`，`rx_er = 1 ^ 1 = 0`。
5. **进 MAC**（[eth_mac_1g_rgmii.v:L236-L238](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_rgmii.v#L236-L238)）：`gmii_rxd=0x6A`、`gmii_rx_dv=1`、`gmii_rx_er=0` 喂给 `eth_mac_1g`。
6. **MAC 内部**：`axis_gmii_rx`（见 [u4-l1](u4-l1-axis-gmii-rx-tx.md)）剥离前导/SFD、累加 CRC、在帧尾校验 FCS，最终在 `rx_axis_tdata` 上输出 `0x6A`。
7. **验证**：跑 `tb/eth_mac_1g_rgmii`（`make`），观察 testbench 注入 `GmiiFrame` 后从 `rx_axis_*` 收回的 `tdata` 与原帧一致、`tuser==0`。

**需要观察的现象**：

- 在第 2 步，`q1`/`q2` 比输入晚一个 `rx_clk` 周期同拍出现（`SAME_EDGE_PIPELINED` 语义）。
- 在第 4 步，`rx_er` 仅当两次 `ctl` 不同时才为 1。
- 在第 7 步，testbench 报告所有变长帧用例 PASS。

**预期结果**：你能用一句话概括——「4 位 DDR 输入经 `iddr` 拆成两个半字节、拼回 8 位 GMII 字节、再由 MAC 转成 AXI 字节」，并指出 `clk90` 让发送时钟边沿对准数据眼中央以保证对端采样余量。若尚未配好仿真环境，第 1–6 步可作纯源码跟踪完成，第 7 步标注「待本地验证」。

## 6. 本讲小结

- **RGMII 用 DDR 把引脚减半**：4 根数据线在时钟上升/下降沿各传一个半字节，等效 8 位 GMII 的带宽；1 根 `ctl` 线在两个边沿分别传 `dv` 与 `dv^er`，异或即可还原 `er`。
- **`iddr`/`oddr` 是跨厂商 DDR 原语**：`iddr` 把一根双边沿输入拆成 `q1`（上升沿样本）+ `q2`（下降沿样本），`oddr` 反向合并；GENERIC 分支用纯 RTL 实现，保证 iverilog 可仿真。
- **`ssio_ddr_in` 给 `iddr` 套源同步时钟缓冲**：PHY 送来的 `rgmii_rx_clk` 既驱动 IOB 输入触发器，又派生出给 MAC 的 `rx_clk`。
- **`rgmii_phy_if` 落地协议**：RX 用 `WIDTH=5` 的 `ssio_ddr_in` 一次还原「4 位数据 + 1 位控制」，拼出 8 位 GMII 字节与 `dv`/`er`；TX 用组合逻辑拆半字节 + 两个 `oddr` 送出数据与时钟。
- **`clk90` 实现 RGMII 内部延迟**：时钟 `oddr` 用 90° 移相时钟，使发送时钟边沿落在数据眼中央，最大化对端 setup/hold 余量。
- **`eth_mac_1g_rgmii` 是布线顶层**：拼 `rgmii_phy_if` + `eth_mac_1g`，内置 `rx_clk` 频率比检测实现 10/100/1000M 自动适配，经同步器下发 `mii_select`；AXI 侧与 `eth_mac_1g` 完全一致，PHY 侧换成 RGMII 引脚。

## 7. 下一步学习建议

- 想看 RGMII MAC 如何接入「logic 时钟域」并解耦反压，继续读 [rtl/eth_mac_1g_rgmii_fifo.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_rgmii_fifo.v)（对应 u5-l1「MAC FIFO 与跨时钟域」），它在本讲顶层前后各加一个 `axis_async_fifo`。
- 想理解 RGMII 在综合工具里的时序如何被约束，阅读 `syn/quartus/rgmii_phy_if.sdc` 与 `syn/vivado/eth_mac_1g_rgmii.tcl`，这是 u12-l2「综合约束与时序」的素材。
- 若要进入 10G/25G 世界，本讲的「DDR + 源同步」思想会以 64b/66b 编码的形式再现，可预习 `rtl/axis_baser_tx_64.v`（对应 u10 PCS/PMA PHY）。
- 实践上，建议把 `tb/eth_mac_1g_rgmii` 跑通后，再用 `cocotbext.eth` 的 `RgmiiPhy` 改写一个只发单帧、打印 `q1`/`q2` 的最小用例，亲眼观察 DDR 拆位过程。
