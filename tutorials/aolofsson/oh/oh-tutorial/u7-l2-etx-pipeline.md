# 发送通路 etx 流水线

## 1. 本讲目标

在上一讲（u7-l1）里，我们把 elink 当作一个"黑盒子"——只知道它对外是 24 对 LVDS 差分线、对内是六条 emesh 通道。本讲要打开这个黑盒的**发送（TX）半边**，顺着一颗写数据包，从"系统侧 104 位并行包"一路追到芯片引脚上的 `txo_data_p/n` 差分比特。

学完后你应当能够：

- 画出 etx 的**逐层数据通路框图**，说清楚每一级做了什么变换、跑在哪个时钟域。
- 解释 **TX 仲裁器**如何在写/读请求/读响应三路之间做固定优先级判决与反压。
- 解释 **etx_protocol** 如何把一个 104 位 emesh 包拆成两个 64 位并行字，并打出 FRAME 起始标记。
- 解释 **etx_io** 如何用"慢速加载 + 快速移位 + DDR + 差分缓冲"把并行字串化到 LVDS 引脚，并反向同步对端 WAIT。
- 解释 **etx_clocks** 如何用一个 MMCM 同时生成快时钟、90° 相移时钟、4 分频慢时钟与 CCLK，并管理上电复位序列。

## 2. 前置知识

本讲默认你已经掌握以下概念（来自依赖讲义）：

- **emesh 104 位包格式与 access/wait 握手**（u5-l1）：包宽 `PW = 2*AW+40`，`access≈valid`，`wait` 高有效表示反压（`~wait≈ready`），事务成立的条件是同一拍 `access=1 且 wait=0`。
- **CDC FIFO 与格雷码指针**（u3-l2）：跨时钟域 FIFO 用格雷码指针 + 同步器，外层 `oh_fifo_cdc` 把底层封装成 valid/ready（或 access/wait）握手。
- **固定优先级仲裁器**（u3-l4）：`oh_arbiter` 用 `grants = requests & ~waitmask` 产生 one-hot grant，bit0 优先级最高。
- **DDR（双数据率）与差分信号**（u7-l1）：源同步时钟 LCLK + FRAME + 8 位 DDR 数据，一个 emesh 事务在线上被串行化为字节流 B00–B13。

两个名词先澄清：

- **源同步（source synchronous）**：发送方不但发数据，还一并发出与数据对齐的时钟（LCLK），接收方用这个时钟采样数据，避免收发双方各自时钟偏差带来的抖动。
- **DDR（Dual Data Rate）**：时钟的上升沿和下降沿各传一次数据，一个时钟周期传 2 bit/线，带宽翻倍。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `elink/hdl/` 下，外加几个 stdlib 原语。先看整体职责：

| 文件 | 角色 | 所处时钟域 |
|------|------|-----------|
| [elink/hdl/etx.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx.v) | TX 顶层，把 clocks/fifo/core/io 四大块拼起来 | 跨域 |
| [elink/hdl/etx_fifo.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_fifo.v) | 三路跨域 FIFO（写/读请求/读响应） | sys_clk → tx_lclk_div4 |
| [elink/hdl/etx_core.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_core.v) | 核心逻辑容器：仲裁 + 重映射 + MMU + 协议 + 配置 | tx_lclk_div4 |
| [elink/hdl/etx_arbiter.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_arbiter.v) | 三通道固定优先级仲裁 | tx_lclk_div4 |
| [elink/hdl/etx_protocol.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_protocol.v) | 104 位包 → 64 位并行字 + 帧信号 + 状态机 | tx_lclk_div4 |
| [elink/hdl/etx_io.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_io.v) | 慢速并行字 → DDR → LVDS 差分引脚；反向同步 WAIT | tx_lclk_io（快） |
| [elink/hdl/etx_clocks.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_clocks.v) | MMCM 时钟生成 + 复位状态机 | sys_clk |

把这条通路画成一张图，就是本讲的"总纲"：

```
  系统侧 (sys_clk 域, 104位emesh包 + access/wait)
  txwr_access/packet   txrd_access/packet   txrr_access/packet
        │                    │                    │
        ▼                    ▼                    ▼
  ┌─────────────────────────────────────────────────────┐
  │ etx_fifo : 3 × oh_fifo_cdc   (sys_clk → tx_lclk_div4)│   ← 跨域缓冲
  └─────────────────────────────────────────────────────┘
        │                    │                    │
        ▼                    ▼                    ▼
  ┌─────────────────────────────────────────────────────┐
  │ etx_core (clk = tx_lclk_div4)                        │
  │   etx_arbiter ──► etx_remap ──► etx_mmu(emmu)       │   ← 选包+地址重映射
  │        └──► etx_protocol  (104→64位 + FRAME + FSM)   │   ← 协议成帧
  │   etx_cfg (配置: tx_enable/burst_enable/ctrlmode…)   │
  └─────────────────────────────────────────────────────┘
        │ tx_data_slow[63:0], tx_frame_slow[3:0]
        ▼
  ┌─────────────────────────────────────────────────────┐
  │ etx_io (tx_lclk_io 快域, 300MHz)                     │
  │   oh_edgealign → 加载/移位 → ODDR(DDR) → OBUFDS      │   ← 串化到引脚
  │   IBUFDS ← txi_wr_wait/txi_rd_wait (反向同步)        │
  └─────────────────────────────────────────────────────┘
        │ txo_data_p/n[7:0], txo_frame_p/n, txo_lclk_p/n
        ▼
                    LVDS 引脚 (到对端芯片)

  etx_clocks : MMCM(sys_clk) → tx_lclk_io / tx_lclk90 / tx_lclk_div4 / cclk
               + 复位状态机 + oh_rsync
```

后文四个最小模块就按这条数据流的顺序展开：**仲裁 → 成帧 → IO → 时钟**。`etx_fifo` 这一级复用了 u3-l2 讲过的 `oh_fifo_cdc`，我们在 4.1 里顺带说明它与上下游的衔接。

> **先打个预防针**：elink 这一层有不少"接口漂移"和"占位桩"，与前面几讲的发现一致——
> - `etx_fifo.v` 用 `.access_in/.access_out/.wait_in/.wait_out` 端口名实例化 `oh_fifo_cdc`，但 stdlib 里 `oh_fifo_cdc` 实际声明的是 `valid_in/valid_out/ready_in/ready_out`。
> - `etx_arbiter.v`、`etx_protocol.v`、`etx_remap.v` 都实例化了 `packet2emesh`（包↔字段拆拼），但**该模块在仓库中不存在**（全仓库 `Glob packet2emesh*` 无结果）。
>
> 因此 etx 这一层**不能脱离 elink 仿真平台的库替换直接"原样"编译**。读源码时以各文件实际文本为准，把 `packet2emesh` 理解为"把 104 位包拆成 write/datamode/dstaddr/... 字段（或反向拼回）"的功能占位即可。

## 4. 核心概念与源码讲解

### 4.1 TX 仲裁：etx_arbiter 三通道选包

#### 4.1.1 概念说明

系统侧送进 elink 的事务分三类，各自走独立通道、互不阻塞：

- **txwr**：写事务（CPU 要往外设/远端写数据）。
- **txrd**：读请求（CPU 发起的读，发出后等对方回数据）。
- **txrr**：读响应（本端作为主设备时，把收到的读请求的数据"回送"给请求方）。

这三路在跨域 FIFO 之后汇到**同一个**协议成帧模块，但物理通路（线、帧、时钟）只有一条。于是需要一个仲裁器：每一拍从三路里挑一路送出去。elink 选择的是**固定优先级**，优先级在文件头注释里写得很清楚：

```
Arbitration Priority:
 1) read responses (highest)
 2) host writes
 3) read requests from host (lowest)
```

读响应优先级最高，是因为读响应往往对应已经在等待的读请求，拖延会让整条读通路堵住；读请求最低，是因为发出去也得等对方回，早一拍晚一拍影响不大。

#### 4.1.2 核心流程

仲裁由两步组成，与 u3-l4 讲过的 `oh_arbiter` 范式完全一致：

1. **选包**：`oh_arbiter` 对三位 `requests` 做固定优先级判决，产出 one-hot 的 `grants`；再用 `oh_mux3` 按 grant 选出当前拍要发的 104 位包（`etx_mux`）。
2. **反压（stall）**：把下游的 `etx_wait` 反向"分发"回三个上游 FIFO。分发不是均分，而是**叠加优先级**——低优先级通道要等高优先级通道也空闲才能推进。

```
requests = {txrd_access, txwr_access, txrr_access}   // bit 排列决定优先级
         ↓ oh_arbiter (固定优先级, one-hot)
grants   = {txrd_grant, txwr_grant, txrr_grant}
         ↓ oh_mux3 (按 grant 选包)
etx_mux  = 被选中的 104 位包
         ↓ 打一拍寄存器 (组合逻辑太深, 流水化)
etx_access / etx_packet
```

#### 4.1.3 源码精读

仲裁器与选包 mux 的实例化，注意 `requests/grants` 的位序直接编码了优先级——`txrr`（读响应）被放在决定优先级的最低 bit：

[elink/hdl/etx_arbiter.v:79-93](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_arbiter.v#L79-L93) —— 用 `oh_arbiter` 产生 one-hot grant，再用 `oh_mux3` 选出当前包。

反压分发逻辑是本模块最巧妙之处。`etx_all_wait` 是"下游真的忙"；之后每一路的 wait 在此基础上**叠加**更高优先级通道的 access——只要更高优先级通道有包，本路就被挡住：

[elink/hdl/etx_arbiter.v:98-111](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_arbiter.v#L98-L111) —— 读响应只看下游；写还要让读响应；读请求还要让读响应和写。

换句话说：

- `txrr_wait = etx_all_wait`（最高优先级，只被下游挡）。
- `txwr_wait = etx_all_wait | txrr_access`（读响应要发时，写让位）。
- `txrd_wait = etx_all_wait | txrr_access | txwr_access`（上面两路任何一个要发，读请求都让位）。

这正是 u3-l4 讲过的"调用方据下游 wait 用逐级累积 OR 自行构造 ready/反压"的标准范式。

此外还有一处"配置环回"：当事务的目标地址高 12 位等于本 elink 的 `ID`（`dstaddr[31:20]==ID`）时，事务不送出芯片，而是通过 `cfg_access/cfg_packet` 折回给本芯片的 RX 配置口（典型用途是写本地的 mailbox 寄存器）：

[elink/hdl/etx_arbiter.v:135-150](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_arbiter.v#L135-L150) —— `cfg_match` 按地址高位判定，命中则走 `cfg_access` 分支而非 `etx_access`。

#### 4.1.4 代码实践

**实践目标**：确认优先级与位序的对应关系。

**操作步骤**：

1. 打开 `elink/hdl/etx_arbiter.v`，找到第 79–87 行的 `oh_arbiter` 实例。
2. 对照 u3-l4 讲过的 `oh_arbiter` 语义（bit0 优先级最高），推断 `requests/grants` 拼接里谁对应 bit0。
3. 再读第 98–111 行的三条 `assign`，验证三条 wait 的"累积或"顺序与注释里的优先级（读响应 > 写 > 读请求）一致。

**需要观察的现象**：`requests` 拼接是 `{txrd_access, txwr_access, txrr_access}`，即 `txrr_access` 在最低位 → 它优先级最高，与文件头注释一致。

**预期结果**：你能用一句话说清"为什么把某路放在拼接的低位就等于给它最高优先级"。

#### 4.1.5 小练习与答案

**练习 1**：源码注释里写着 `TODO: change to round robin!!! (live lock hazard)`。固定优先级在什么情况下会让低优先级通道"饿死"？

> **答案**：当高优先级通道（如 txrr）持续不断地有事务时，低优先级通道（txrd）的 `txrd_wait` 永远为 1，永远拿不到发送机会。轮询（round-robin）能保证公平，但需要记住"上一轮发到谁"，实现更复杂，且若各路总处于活跃则可能反复空转（活锁）。

**练习 2**：`etx_arbiter` 里 `etx_access` 是寄存器输出（`always @posedge clk`），而 `oh_arbiter` 本身是纯组合的。为什么还要额外打一拍？

> **答案**：因为 `oh_arbiter` + `oh_mux3` 这条组合路径较深（判决 + 104 位 mux），文件注释 `Pipeline stage (arbiter+mux takes time..)` 明说为了时序把它流水化；代价是反压与选包延迟一拍，由后面的 wait 传播逻辑兜底。

---

### 4.2 协议成帧：etx_protocol 把 104 位包拆成 64 位并行字

#### 4.2.1 概念说明

仲裁器吐出的还是一个完整的 104 位 emesh 包，但 `etx_io` 最终要在线上发字节流。如果让 IO 直接处理 104 位包，所有"切字节、插帧"的逻辑都得跑在 300MHz 快时钟域里，面积和功耗都很贵。

`etx_protocol` 的设计目标（写在文件头注释里）就是：**把高位宽的包预先拆好，让快时钟域只做最简单的"移位 + DDR"**。具体做法是把一个包拆成两个 64 位并行字（`tx_data_slow`），再配一个 4 位的帧信号（`tx_frame_slow`），一起交给 IO。

#### 4.2.2 核心流程

成帧由一个发送状态机驱动。状态用 3 位编码，关键状态四个：

```
TX_IDLE ──etx_valid──► TX_START ──► TX_ACK ──┬─tx_burst─► TX_BURST ──┐
   ▲                                        │                       │
   │                                        └─etx_valid─► TX_START │
   └──────────────────────────────── 无有效事务 / 突发结束 ◄──────────┘
```

- **TX_START**：发第一个 64 位字（头部 + 地址），同时打出帧起始标记。
- **TX_ACK**：发第二个 64 位字（数据 + 源地址）。
- **TX_BURST**：突发模式下持续发数据字（64 位写连续地址）。

每个状态对应一组 `{tx_data_slow, tx_frame_slow}` 输出：

| 状态 | tx_data_slow | tx_frame_slow | 含义 |
|------|-------------|---------------|------|
| IDLE | — | 0000 | 空闲，FRAME 全低 |
| START | cycle1（头部/地址） | 0111 | 先低后高，制造 FRAME 上升沿作为 B00 起点 |
| ACK / BURST | cycle2（数据/源地址） | 1111 | FRAME 保持高，表示事务延续 |

此外，模块还要做"突发检测"——当**当前**事务和**下一个**事务都是 64 位写（`datamode==11`）、控制模式为 0、且目标地址正好相差 +8 时，置 `tx_burst`，让状态机进入连续发数据字的模式，省掉每个包的头部开销。

#### 4.2.3 源码精读

状态机本身，注意 `case` 带满 `default` 之外的显式分支、复位回到 `TX_IDLE`，符合 OH! 编码规范：

[elink/hdl/etx_protocol.v:148-161](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_protocol.v#L148-L161) —— TX 发送状态机：IDLE→START→ACK→（BURST 或回 IDLE）。

两个 64 位并行字的拼装。`cycle1` 装头部与地址（含 `write/access/datamode/ctrlmode/dstaddr`），`cycle2` 装数据与回送源地址；输出由状态选择——这是本模块"把包拆成并行字"的核心：

[elink/hdl/etx_protocol.v:178-192](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_protocol.v#L178-L192) —— `tx_cycle1`（头部字）、`tx_cycle2`（数据字），以及按状态二选一输出 `tx_data_slow`。

帧信号生成。`TX_START` 拍给 `0111`（先低后高，制造上升沿），其余非空闲拍给 `1111`：

[elink/hdl/etx_protocol.v:174-176](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_protocol.v#L174-L176) —— 帧信号 `tx_frame_slow` 的状态译码。

> **字段映射说明**：`tx_cycle1/cycle2` 的位拼装对应 elink README 的字节表 B00–B13（`R/ctrlmode/dstaddr/datamode/write/access/data/srcaddr`）。本讲不逐位罗列该映射——权威表在 [elink/README.md](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md) 的 "Packet format / IO interface" 两节，读源码时拿 README 的字节表对照上面这两段拼接即可。

最后是反压回传。`etx_protocol` 把 IO 侧回来的 `tx_wr_wait/tx_rd_wait` 加工成对上游的 `etx_wr_wait/etx_rd_wait/etx_wait`，其中 `tx_ack_wait`（即处于 `TX_START` 拍）会把反压多挂一拍，保证"已经开始发送的事务不被中途打断"——这正是 elink README 里那条规则"事务传到一半不打断"的实现：

[elink/hdl/etx_protocol.v:199-204](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_protocol.v#L199-L204) —— wait 反向传播，`etx_wait = etx_wr_wait | etx_rd_wait`。

**配置接入点**：`etx_cfg` 给本模块送来 `tx_enable`（总发送开关）、`burst_enable`（突发使能，对应 `ELINK_TXCFG[10]`）、`ctrlmode/ctrlmode_bypass`（强制路由模式，对应 `ELINK_TXCFG[7:4]` 与 `[9]`）。`ctrlmode_bypass=1` 时用配置寄存器里的 ctrlmode 覆盖包自带的——见第 86–87 行的 `ctrlmode_mux`：

[elink/hdl/etx_protocol.v:86-87](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_protocol.v#L86-L87) —— ctrlmode 旁路选择（来自包 or 来自配置寄存器）。

#### 4.2.4 代码实践

**实践目标**：把"状态 → 输出字"的对应关系读成一张表。

**操作步骤**：

1. 打开 `elink/hdl/etx_protocol.v`，定位 `TX_IDLE/TX_START/...` 的 `define（第 142–146 行）。
2. 读状态机（148–161 行），画出 IDLE/START/ACK/BURST 之间的迁移条件。
3. 对照第 174–192 行，填出"每个状态下 `tx_data_slow` 取 cycle1 还是 cycle2、`tx_frame_slow` 是 0111 还是 1111"。

**需要观察的现象**：`tx_access`（送给 TXMON 的有效指示）只在 `TX_START` 和 `TX_BURST` 为真（第 164–165 行）。

**预期结果**：你能解释"为什么 START 拍 frame=0111、而 ACK 拍 frame=1111"——前者制造 FRAME 上升沿标记新包起点，后者保持高电平表示延续。

**待本地验证**：`tx_burst` 的组合条件里第 120 行注释了 `//BUG: should be valid?`，说明突发检测条件可能不完整；若跑 `tests/test_burst.emf` 仿真，留意突发序列是否如期进入 `TX_BURST`。

#### 4.2.5 小练习与答案

**练习 1**：为什么把 104 位包拆成"两个 64 位字"而不是直接送 104 位？

> **答案**：为了让"切字节、对齐帧"这类较复杂的逻辑只跑在慢时钟 `tx_lclk_div4`（75MHz）里，快时钟 `tx_lclk_io`（300MHz）只做最廉价的"加载 + 移位 + DDR"。文件头注释明说目标是"minimize the amount of logic done on the high speed domain"。64 位是 8 字节 × DDR 刚好对应 16 个引脚位，便于 IO 移位。

**练习 2**：`burst_addr_match = ((tx_dstaddr+8) == etx_dstaddr)`。为什么是 +8？

> **答案**：突发模式只对 64 位（8 字节）写有效（`datamode==11`）。64 位 = 8 字节，连续地址每次步进 8，所以"当前地址 + 8 等于下一地址"才判定为连续突发。

---

### 4.3 DDR IO：etx_io 串化到 LVDS 差分对

#### 4.3.1 概念说明

到这一级，数据是 `tx_data_slow[63:0]`（慢域，每 4 个快周期才换一次内容）。`etx_io` 要做三件事：

1. **跨到快域并串化**：找到慢时钟在快时钟里的对齐沿，把 64 位"加载"进移位寄存器，然后每个快周期移出 16 位。
2. **DDR 打出去**：用 Xilinx 的 `ODDR`/`ODDRE1` 原语，在时钟上下沿各送一位，8 根数据线一个快周期送 16 位。
3. **差分化 + 反向同步 WAIT**：用 `OBUFDS` 把单端信号变成 LVDS 差分对（p/n）；用 `IBUFDS` 接收对端回来的 WAIT，并做两级同步。

最终对外就是 elink 的物理引脚：`txo_lclk_p/n`（时钟）、`txo_frame_p/n`（帧）、`txo_data_p/n[7:0]`（8 位 DDR 数据）。

#### 4.3.2 核心流程

串化的关键在于**慢时钟与快时钟同源且整数倍相关**（`tx_lclk_div4 = tx_lclk_io / 4`），所以可以用 `oh_edgealign` 找到"慢时钟上升沿落在快时钟的哪一拍"，作为每 4 拍一次的加载脉冲：

```
每 4 个 tx_lclk_io 周期：
  拍0 (firstedge=1): tx_data[63:0] <= tx_data_slow   ← 整体加载
  拍1: tx_data <= {16'b0, tx_data[63:16]}              ← 右移 16 位
  拍2: 继续右移
  拍3: 继续右移
  → 每拍露出最低 16 位 tx_data[15:0]，送 DDR

DDR (每根线):
  上沿送 tx_data16[i+8]，下沿送 tx_data16[i]
  → 8 根线 × 2 bit/周期 = 16 bit/周期
  → 4 周期 × 16 bit = 64 bit = 一个慢速并行字
```

引脚上的时钟 `txo_lclk` 不是直接送 `tx_lclk_io`，而是送一个 **90° 相移**的版本（`tx_lclk90`），目的是让时钟沿落在数据眼的**正中间**，给接收方最大的采样裕度——这是源同步接口的经典手法。

反向回来的 WAIT 信号（对端说"我只能再收一个了"）相位不确定，必须用**两级同步器**采样（呼应 u7-l1 与 u2-l4 的亚稳态知识）；elink 还额外把可能很短的 WAIT 脉冲"展宽"避免漏检。

#### 4.3.3 源码精读

边沿对齐。`oh_edgealign` 比较 `fastclk` 与 `slowclk`，每 4 个快周期产出一个 `firstedge` 脉冲：

[elink/hdl/etx_io.v:76-79](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_io.v#L76-L79) —— 实例化 `oh_edgealign` 找慢时钟在快时钟里的对齐沿。

加载/移位寄存器。`firstedge` 为真时整体加载 64 位数据 + 4 位帧，否则右移 16 位（数据）和 1 位（帧）；每拍取最低 16 位送 DDR：

[elink/hdl/etx_io.v:82-95](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_io.v#L82-L95) —— 慢到快的串化：load/shift + 取低 16 位。

DDR 发送。用 `generate` 在 `ULTRASCALE`（用 `ODDRE1`）与其它（Zynq，用 `ODDR`）两种平台间二选一；8 根数据线各一个 ODDR，`D1` 喂高字节、`D2` 喂低字节， FRAME 与 LCLK 同理。注意 LCLK 用的时钟是 `tx_lclk90`（90° 相移），而数据用 `tx_lclk_io`——这正是"时钟摆在数据眼中央"的实现：

[elink/hdl/etx_io.v:122-186](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_io.v#L122-L186) —— 平台二选一的 DDR 发送，注意 `oddr_lclk` 用的是 `tx_lclk90`。

差分缓冲。`OBUFDS` 把单端变 LVDS 差分对（p/n），分别用于 data/frame/lclk：

[elink/hdl/etx_io.v:189-199](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_io.v#L189-L199) —— 三组 `OBUFDS` 驱动差分引脚。

WAIT 反向同步。差分输入 `IBUFDS` 还原出单端 WAIT，先在快时钟**下降沿**采一拍（`tx_*_wait_sync`），再到慢时钟域两级寄存器同步，最后 `reg | reg2` 展宽防漏：

[elink/hdl/etx_io.v:100-117](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_io.v#L100-L117) —— WAIT 的跨域同步与展宽。

> **厂商原语说明**：`ODDR/ODDRE1/BUFG/OBUFDS/IBUFDS/MMCME2_ADV` 都是 Xilinx 厂商原语，iverilog 本身不认识。仿真时靠 `xilibs`（见 u9-l3）提供行为模型，或靠 `CFG_PLATFORM`/`CFG_TARGET` 宏切换到 `GENERIC` 分支。这是 FPGA 设计 RTL 的常态：可综合代码里嵌入厂商原语，仿真用模型替换。

#### 4.3.4 代码实践

**实践目标**：验证"64 位慢速字 → 4 个快周期 → 16 位/周期 → DDR"的串化时序。

**操作步骤**：

1. 打开 `elink/hdl/etx_io.v` 第 82–95 行，把 `firstedge` 为真/假两个分支抄在纸上。
2. 假设加载后 `tx_data = 64'hAABBCCDDEEFF0011`（高位在左），按"右移 16、取低 16"写出 4 个快周期各自露出的 16 位值。
3. 对照第 127–133 行的 ODDR，确认每根数据线在一个快周期里上下沿各送哪一位。

**需要观察的现象**：4 个周期送出的 16 位片段依次是 `0x0011 → 0xEEFF → 0xCCDD → 0xAABB`（最低 16 位先发）。

**预期结果**：你能算出一个 64 位慢速字恰好占用 4 个 `tx_lclk_io` 周期；两个慢速字（一个包的 cycle1+cycle2）占 8 个周期，与 elink README 的 B00–B13 字节流（约 14 字节 ≈ 7 个周期 + 帧标记）量级吻合。

**待本地验证**：上面"最低位先发"的结论取决于移位方向与 `tx_data16` 取值，建议在仿真波形里对照 `txo_data_p` 与 `tx_lclk_io` 确认实际在线字节顺序。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `txo_lclk` 要送 90° 相移版本，而数据不送？

> **答案**：源同步链路里，时钟和数据由同一器件发出、走相近的走线，到达对端时相对关系基本保持。让时钟沿（采样点）落在数据眼正中央（即相对数据中心偏 90°），接收方的建立/保持时间裕度最大。数据本身不需要移相，它的跳变沿对齐发送时钟即可。

**练习 2**：`tx_wr_wait = tx_wr_wait_reg | tx_wr_wait_reg2`（第 116 行）为什么用"或"而不是直接取一级？

> **答案**：跨时钟域采到的 WAIT 脉冲可能很窄（注释提到 "legacy elink puts out short wait pulses"），只采一级可能恰好漏掉。用相邻两拍寄存器"或"起来，相当于把脉冲至少展宽到能被慢域可靠识别的宽度，防止短脉冲"溜过去"导致反压丢失。

---

### 4.4 时钟：etx_clocks 产生 LCLK、相位与复位

#### 4.4.1 概念说明

整条 TX 通路其实跑在**三个同源时钟**上：

- `tx_lclk_io`（快，默认 300 MHz）：IO 的 DDR 与移位。
- `tx_lclk_div4`（慢，默认 75 MHz）：FIFO 读侧、仲裁、协议成帧。
- `tx_lclk90`（快，300 MHz，但相位偏 90°）：驱动 ODDR 送出 `txo_lclk`。

外加给 Epiphany 芯片的 `cclk`（默认 600 MHz）。这四个时钟都由**一个 MMCM**（Xilinx 的混合模式时钟管理器，相当于一个可编程 PLL+移相器）从输入 `sys_clk`（默认 100 MHz）倍频分频得到。`etx_clocks` 还顺带管上电**复位序列**——因为 PLL 锁定前时钟不稳，必须按"等锁定 → 放 CCLK → 解复位"的顺序来。

#### 4.4.2 核心流程

频率派生（参数默认值下）：

\[
\mathrm{VCO} = \mathrm{FREQ\_SYSCLK} \times \mathrm{MMCM\_VCO\_MULT} = 100 \times 12 = 1200\ \mathrm{MHz}
\]

\[
\mathrm{tx\_lclk\_io} = \mathrm{VCO} / \mathrm{TXCLK\_DIVIDE} = 1200 / 4 = 300\ \mathrm{MHz}
\]

\[
\mathrm{tx\_lclk\_div4} = \mathrm{VCO} / (\mathrm{TXCLK\_DIVIDE}\times 4) = 1200 / 16 = 75\ \mathrm{MHz}
\]

\[
\mathrm{cclk} = \mathrm{VCO} / \mathrm{CCLK\_DIVIDE} = 1200 / 2 = 600\ \mathrm{MHz}
\]

复位状态机（跑在 `sys_clk`，因为它是"常在"时钟）：

```
TX_RESET_ALL ──(!soft_reset)──► TX_START_CCLK   (启动 MMCM)
                ──(mmcm_locked)──► TX_STOP_CCLK   (停一下, 准备)
                                   ──► TX_DEASSERT_RESET  (释放 chip_nreset)
                                       ──► TX_HOLD_IT      (再等锁定)
                                           ──(locked)──► TX_ACTIVE  (tx_active=1, 正常工作)
soft_reset=1 时从任意态回到 TX_RESET_ALL
```

复位释放后，还要用 `oh_rsync`（u2-l4 讲过的"异步生效、同步释放"复位同步器）把复位分别同步进快域（`etx_io_nreset`）和慢域（`etx_nreset`），避免复位释放沿恰好落在时钟沿附近引发亚稳态。

#### 4.4.3 源码精读

频率/相位的派生参数——注意 `TXCLK_PHASE=90` 决定了 `tx_lclk90` 的相位偏移：

[elink/hdl/etx_clocks.v:11-30](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_clocks.v#L11-L30) —— 频率/相位参数与派生的 MMCM 分频比。

复位状态机。一个自由运行的计数器产生 `heartbeat`，状态机借心跳推进；注释里 `//works b/c of free running counter!` 强调这依赖 `sys_clk` 一直在跑：

[elink/hdl/etx_clocks.v:93-136](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_clocks.v#L93-L136) —— 心跳计数器 + 复位序列状态机。

由状态机驱动各复位与 `tx_active`：

[elink/hdl/etx_clocks.v:139-153](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_clocks.v#L139-L153) —— `mmcm_reset / chip_nreset / tx_nreset / tx_active` 的状态译码。

复位同步到两个时钟域：

[elink/hdl/etx_clocks.v:159-169](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_clocks.v#L159-L169) —— 两路 `oh_rsync`，分别同步进 `tx_lclk_io` 与 `tx_lclk_div4`。

MMCM 实例。`MMCME2_ADV` 一个 CLKOUT 给 cclk（`CLKOUT0_DIVIDE=CCLK_DIVIDE`），三路给 TX 时钟（同频但 `CLKOUT2_PHASE=90` 实现相移，`CLKOUT3_DIVIDE=TXCLK_DIVIDE*4` 实现 4 分频）：

[elink/hdl/etx_clocks.v:178-248](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_clocks.v#L178-L248) —— MMCM 配置 + 三路 `BUFG` 全局时钟缓冲。

非 XILINX 目标（如纯仿真/Generic）的降级分支——把所有时钟都直接绑成 `sys_clk`，便于不用 MMCM 也能仿真：

[elink/hdl/etx_clocks.v:287-294](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_clocks.v#L287-L294) —— `else` 分支：时钟全等于 `sys_clk`。

**配置接入点**：MMCM 的分频比目前由**参数**（编译期）决定，对应 README 里 `ELINK_CLK`（0xF0204）标注的 `(NOT IMPLEMENTED)`——运行时改频率尚未打通。`TARGET`/`PLATFORM` 宏（来自 `elink_constants.vh` 的 `CFG_TARGET`/`CFG_PLATFORM`）在编译期选择 XILINX/ULTRASCALE/Zynq 的具体原语。

#### 4.4.4 代码实践

**实践目标**：核对"快时钟是慢时钟的 4 倍"这一整条通路的前提。

**操作步骤**：

1. 打开 `elink/hdl/etx_clocks.v` 第 27–30 行，读出 `MMCM_VCO_MULT`、`TXCLK_DIVIDE`、`CCLK_DIVIDE` 的表达式。
2. 代入默认 `FREQ_SYSCLK=100, FREQ_TXCLK=300, FREQ_CCLK=600`，算出 VCO 与三路时钟频率。
3. 确认 `CLKOUT3_DIVIDE = TXCLK_DIVIDE*4`，即 `tx_lclk_div4` 恰为 `tx_lclk_io` 的 1/4——这正是 `etx_io` 用 `oh_edgealign` 做"每 4 拍加载一次"的前提。

**需要观察的现象**：`tx_lclk_io : tx_lclk_div4 = 4 : 1`，二者与 `tx_lclk90` 同频（仅相位差 90°）。

**预期结果**：你能在纸上写出 300 / 75 / 300(@90°) / 600 这组频率，并解释为什么 `oh_edgealign` 的注释敢断言 "clocks are aligned and synchronous"。

**待本地验证**：仿真里若 `TARGET` 非 XILINX，会走第 287–294 行降级分支，所有时钟退化为 `sys_clk`，此时 4:1 关系不再成立、`oh_edgealign` 行为也会变化——做 IO 时序实验时务必确认实际选中的 `TARGET`。

#### 4.4.5 小练习与答案

**练习 1**：为什么复位状态机跑在 `sys_clk` 而不是 `tx_lclk_io`？

> **答案**：`tx_lclk_io` 是由 MMCM 产生的，MMCM 在锁定前根本没有稳定输出。复位状态机要负责"等 MMCM 锁定"，它必须跑在一个**常在、不依赖 MMCM** 的时钟上——板载输入 `sys_clk` 就是这个角色。这就是 README 说的 "asynchronous and synchronous reset out of necessity"。

**练习 2**：`etx_clocks` 同时产生 cclk（给 Epiphany 芯片）和 TX 时钟，为什么把它们放在一起？

> **答案**：二者同源（同一个 MMCM 的不同 CLKOUT），能保证 cclk 与 elink 链路时钟有确定的频率/相位关系；而且 MMCM 锁定、复位释放这条时序对二者是同一件事，集中管理能避免一个先起来、一个还没锁定的不一致状态。

---

## 5. 综合实践

> 这个任务对应本讲的 `practice_task`：画一张 etx 数据通路框图，标注一个写包从系统侧到 `txo_data_p/n` 经过的每一级。

**任务**：以 `tests/test_hello.emf` 第二行那条写事务

```
00000000_00000000_80800000_05_0010   //32 位写
```

为追踪对象（含义：srcaddr=0x00000000, data=0x00000000, dstaddr=0x80800000, ctrlmode=0x0/datamode=2(32位)/write=1, delay=0x10），画出并标注它从进入 elink 到出现在 `txo_data_p/n` 引脚所经过的每一级，要求：

1. **画出完整通路**：系统侧入口 → `etx_fifo`（标明时钟域跨越 sys_clk→tx_lclk_div4）→ `etx_arbiter`（标明它从三路里选了 txwr）→ `etx_remap`/`etx_mmu`（标明默认直通）→ `etx_protocol`（标明拆成 cycle1 头部字 + cycle2 数据字，并打出 frame=0111）→ `etx_io`（标明 load/shift → DDR → OBUFDS）→ `txo_data_p/n`、`txo_frame_p/n`、`txo_lclk_p/n`。旁路标出 `etx_clocks` 提供的三路时钟与复位。

2. **标注每一级的位宽与时钟域**：例如 `etx_fifo` 入口 104 位（sys_clk）、出口 104 位（tx_lclk_div4）；`etx_protocol` 入口 104 位、出口 64 位 + 4 位帧；`etx_io` 入口 64+4 位（慢域）、出口 8 位 DDR（快域）。

3. **画出反向反压通路**：对端拉高 `txi_wr_wait` → `etx_io` 的 IBUFDS + 两级同步 → `etx_protocol` 的 `etx_wr_wait` → `etx_arbiter` 的 `txwr_wait` → `etx_fifo` → 系统侧。

**操作步骤**：

1. 先在 `elink/hdl/etx.v` 的第 90–211 行核对五大块的实例化顺序与连线（这是框图的权威来源）。
2. 再到 `elink/hdl/etx_core.v` 第 84–195 行核对 core 内部 arbiter→remap→mmu→protocol 的顺序。
3. 自己在纸上画图，再与第 3 节的草图对照查漏。

**进阶（可选，需本地环境）**：若已按 u1-l3 装好 iverilog，可在 `elink/dv` 下尝试：

```sh
cd elink/dv
./build.sh                 # 编译出 elink.vvp（注意可能需修 libs.cmd 的历史路径, 见 u1-l3）
./run.sh tests/test_hello.emf
gtkwave waveform.vcd       # 观察波形
```

> 说明：`run.sh` 会把传入的 `.emf` 复制成 `test_0.emf` 再跑 `./elink.vvp`。由于前述 `packet2emesh` 缺失与 `libs.cmd` 历史路径问题，**未必能一次跑通**；跑不通时退回"源码阅读型实践"——在波形或源码里定位 `txo_data_p` 随 `tx_lclk_io` 的跳变，验证一个写包确实被串化成连续字节流即可。本步骤**待本地验证**。

## 6. 本讲小结

- etx 是一条**分层流水线**：`etx_fifo`（跨域缓冲）→ `etx_core`（仲裁 + 重映射 + 协议成帧）→ `etx_io`（串化 + DDR + 差分），时钟与复位由 `etx_clocks` 统一供给。
- **TX 仲裁**是固定优先级（读响应 > 写 > 读请求），靠 `oh_arbiter` 产 one-hot grant、`oh_mux3` 选包，反压用"逐级累积或"分发回各路。
- **协议成帧** `etx_protocol` 把 104 位包拆成两个 64 位并行字（头部字 + 数据字），用 `TX_IDLE/START/ACK/BURST` 状态机驱动，FRAME 在 START 拍打 0111 制造上升沿起点；复杂逻辑只跑在慢域，快域只做移位。
- **DDR IO** `etx_io` 用 `oh_edgealign` 找加载沿，64 位加载后每拍移 16 位，经 `ODDR` 双沿送 16 位/周期，`OBUFDS` 变 LVDS 差分；时钟送 90° 相移版以落在数据眼中央；反向 WAIT 用 IBUFDS + 两级同步 + 展宽。
- **时钟** `etx_clocks` 用一个 MMCM 从 sys_clk 生成 `tx_lclk_io(300)/tx_lclk90(300,@90°)/tx_lclk_div4(75)/cclk(600)`，并跑一个复位序列状态机，最后用 `oh_rsync` 把复位同步进各时钟域。
- 整条链路坚持"**让快时钟域尽量笨**"：所有重活（仲裁、成帧、地址译码）都在 75MHz 慢域做，300MHz 快域只做移位与 DDR，这是 elink 能跑到 Gbit/s 级的关键工程取舍。
- 一如既往：elink 这一层存在 `packet2emesh` 缺失、`oh_fifo_cdc` 端口名漂移等历史遗留，**不能脱离仿真平台库替换直接编译**，读源码以实际文本为准。

## 7. 下一步学习建议

- **下一篇 u7-l3（接收通路 erx 流水线）**：erx 是 etx 的"镜像"——`erx_io` 做 LVDS 输入 + IDDR 解串、`erx_clocks` 做时钟数据恢复、`erx_protocol` 把字节流还原成 104 位包、`erx_arbiter` 分发到 rxwr/rxrd 通道。读完本讲再去读 erx，会有"对称结构"的强烈对照感，重点比较发送的"串化"与接收的"解串/对齐"如何互为逆过程。
- **横向阅读**：`elink/hdl/etx_core.v` 把仲裁、`etx_remap`、`etx_mmu`、协议、配置串成一条完整核心逻辑，是理解 TX"地址重映射/MMU 在哪一级介入"的关键；建议把它与 u6-l4 的 emmu 讲义对照阅读。
- **时钟域深化**：若对"90° 相移落在数据眼中央""源同步采样"还觉得抽象，可回头读 u2-l3（时钟原语）与 u2-l4（CDC 同步），并把 elink README 的 "Clocking and reset" 一节与 `etx_clocks.v` 的 MMCM 参数对照看。
