# CPU FIFO：AXIS 到 CSR 的映射

## 1. 本讲目标

本讲聚焦控制面（CPU）与数据面（DPE）之间那条「包级」通道——`cpu_fifo`。读完本讲，你应当能够：

- 说清为什么一条 128 位的 AXI-Stream（AXIS）链路必须被拆成 4 个 32 位的 CSR 寄存器，以及这样拆的硬件/软件依据。
- 解释 `singlepulse` 触发位如何让 CPU「不必和 AXIS 时钟节拍对齐」就能完成一次传输。
- 默写出 CPU 发送/接收一个 16 字节段的约 10 步 CSR 读写序列。
- 自己算出这个 CSR 化的 FIFO 接口吞吐上限（约 170 Mbps），并解释它为何不足以承载线速用户数据，却完全够用 WireGuard 握手。

本讲是 Unit 3「CSR——软硬件唯一桥梁」的核心一环：u3-l1 讲了 SystemRDL 语法，u3-l2 讲了 PeakRDL 如何从一份 `csr.rdl` 同时生成 RTL 与 HAL，而本讲拿 `cpu_fifo` 这个最典型的「把一条总线拆成一堆寄存器」的例子，把前两讲的概念落到真实数据流上。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自 u2 与 u3 前序讲义）：

- **两个面与它们的桥**：控制面是跑在 picoRV32 软 CPU 里的 C 固件，数据面是纯 RTL 的 DPE；二者唯一物理通道就是由 `csr.rdl` 生成的 CSR 寄存器组（u2-l1、u3-l1）。
- **CSR 的 sw/hw 读写属性**：`sw=rw;hw=r` 表示「CPU 写、硬件读」（控制类），`sw=r;hw=w` 表示「硬件写、CPU 读」（状态类）；口诀是「谁是写者，谁就是数据源」（u3-l1）。
- **`singlepulse` 修饰符**：被它标记的字段，CPU 写 1 后会在下一拍自动清零，形成一个「单脉冲」触发（u3-l1）。
- **AXI-Stream（AXIS）信号**：`TVALID`/`TREADY` 握手，`TDATA` 载荷，`TLAST` 标包尾，`TKEEP` 字节有效，`TUSER`/`TID` 侧带元数据（u2-l2、u4-l1）。
- **三个时钟域**：125 MHz@8 bit 接 GMII MAC、80 MHz@32 bit 跑 CPU/CSR 控制面、80 MHz@128 bit 跑 DPE 数据面（u2-l3）。本讲的 `cpu_fifo` 就站在 80 MHz@32 bit 与 80 MHz@128 bit 两个域的接缝上。

本讲会用到一对术语：**RMW（Read-Modify-Write，读改写）**——若要改一个寄存器里的某几位，又不能直接写整个字，就得先读回、在 CPU 里改、再写回，多一轮总线访问；**HAL（硬件抽象层）**——u3-l2 生成的 `csr_hw.h`，让固件能用 `csr->cpu_fifo->rx->...` 这样的层级指针访问寄存器。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [1.hw/ip.infra/cpu_fifo.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/cpu_fifo.sv) | 本讲主角。用两个 `axis_fifo` 实例（`tx_fifo`/`rx_fifo`）把 128 位 AXIS 与 32 位 CSR 寄存器对接。 |
| [3.build/csr_build/csr.rdl](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl) | 单一真源。`cpu_fifo` regfile（L53–L306）声明了 rx/tx 各自的 data/control/trigger/status 寄存器与字段位域。 |
| [2.sw/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/README.md) | 固件侧文档。给出了 CPU 收发 16 字节段的 10 步流程与 ~170 Mbps 吞吐估算公式。 |
| [1.hw/ip.infra/dpe_if.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/dpe_if.sv) | 数据面 128 位 AXIS 接口定义（`m_axis`/`s_axis` 两个 modport）。 |
| [1.hw/top.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv) | 顶层把 `u_cpu_fifo` 的 `to_csr`/`from_csr` 接到全局 hwif，`to_cpu`/`from_cpu` 接到 DPE（L225–L230）。 |
| [1.hw/ip.dpe/dpe_multiplexer.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv) | DPE 多路复用器。`from_cpu` 是它的 CPU 输入队列（轮询入口之一），承接本讲发出去的包。 |

## 4. 核心概念与源码讲解

### 4.1 AXIS→CSR 位宽拆分与 8 位对齐

#### 4.1.1 概念说明

数据面 DPE 内部用一条 128 位的 AXIS 总线搬数据（一个 beat = 16 字节，正好对齐到 80 MHz@128 bit 域的 ~10 Gbps 容量，见 u2-l3）。可控制面的软 CPU 是一颗 32 位的 picoRV32，它的数据总线只有 32 位、且带字节使能（byte enable）。两者位宽差 4 倍，怎么对接？

`cpu_fifo` 的答案很直接：**把每个 128 位的 AXIS beat 拆成 4 个 32 位的 CSR 寄存器**，让 CPU 用 4 次普通的 32 位访存来拼出一个 beat。其余 AXIS 侧带信号（`TKEEP`/`TLAST`/`TUSER`）也各落到自己的字段里。

但「拆」本身会引入一个性能陷阱：如果某个字段在 32 位字里占了「半截字节」，CPU 要改它就得先读回整字、在寄存器里改、再写回——这就是 RMW，每个字段多一轮总线访问。为此，`csr.rdl` 把所有字段都对齐到 8 位边界（详见本模块源码精读），让每个字段都能被「字节使能」一次性命中，单条 store 指令即可更新，杜绝 RMW。

#### 4.1.2 核心流程

一个 128 位 beat 的拆解与对齐关系（以 `cpu_fifo.rx` 为例，CPU→DPE 方向）：

```
128-bit AXIS beat (TDATA[127:0])
┌───────────────┬───────────────┬───────────────┬───────────────┐
│ TDATA[127:96] │ TDATA[95:64]  │ TDATA[63:32]  │ TDATA[31:0]   │
└───────┬───────┴───────┬───────┴───────┬───────┴───────┬───────┘
        │               │               │               │
        ▼               ▼               ▼               ▼
   data_127_96      data_95_64      data_63_32      data_31_0     ← 各 32-bit CSR 寄存器
   (sw=rw,hw=r)    (sw=rw,hw=r)    (sw=rw,hw=r)    (sw=rw,hw=r)

control 寄存器（32-bit，按字节划分，避免 RMW）：
┌───────────────┬───────────────┬───────────────┬───────────────┐
│  byte 3       │  byte 2       │  byte 1       │  byte 0       │
│ tkeep[31:24]  │ tkeep[23:16]  │ tlast[15:15]  │ tuser_*[7:0]  │
└───────────────┴───────────────┴───────────────┴───────────────┘
   ← tkeep 占 bytes[2:3]，tlast 占 byte[1]，4 个 tuser 子字段共占 byte[0] →
```

关键点：**字段不跨字节边界**。这样 CPU 写 `tuser_dst`（byte 0）时，硬件用字节使能只更新 byte 0，不会动到 `tkeep`（bytes 2-3）或 `tlast`（byte 1）；反之亦然。同一 byte 0 内的 4 个 `tuser` 子字段则由 HAL 组合成一个字节值一次写入。

#### 4.1.3 源码精读

`cpu_fifo` 模块端口只暴露两样东西：CSR 侧的 hwif 结构体（`from_csr`/`to_csr`）和数据面侧的两个 AXIS 接口（`to_cpu`/`from_cpu`）：

[1.hw/ip.infra/cpu_fifo.sv:L43-L50](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/cpu_fifo.sv#L43-L50) —— 模块端口。`to_cpu` 是 `dpe_if.s_axis`（接收 DPE 来的数据，即 DPE→CPU），`from_cpu` 是 `dpe_if.m_axis`（向 DPE 送出数据，即 CPU→DPE）。

**注意命名陷阱**：`rx`/`tx` 标签是相对 DPE 而言的，不是相对 CPU。即 `rx` = CPU→DPE（CPU 写），`tx` = DPE→CPU（CPU 读）。这一点 `2.sw/README.md` L224 有明确说明，初学时极易搞反。

拆分的「现场」就在 `tx_fifo` 实例的 `m_axis_tdata` 连接里——这是 DPE→CPU 方向，把 4 个 32 位 data 寄存器在硬件侧重新拼接成 128 位 `TDATA` 喂给 FIFO 输出：

[1.hw/ip.infra/cpu_fifo.sv:L73-L84](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/cpu_fifo.sv#L73-L84) —— `tx_fifo` 的 `m_axis` 信号把 `{data_127_96, data_95_64, data_63_32, data_31_0}` 四个 `.next`（CPU 写入的下一拍值）拼成 128 位 `TDATA`，并把 `tkeep`/`tlast`/`tuser_*` 各自接到 `control` 寄存器的对应字段。这段拼接就是「32 位 CSR 寄存器 ↔ 128 位 AXIS」的物理焊接点。

反过来，`rx_fifo` 实例（CPU→DPE 方向）的 `s_axis` 从 CSR 寄存器的 `.value`（硬件当前值）读取并拼成 128 位喂进 FIFO：

[1.hw/ip.infra/cpu_fifo.sv:L107-L118](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/cpu_fifo.sv#L107-L118) —— `rx_fifo` 的 `s_axis` 用 `{...data_127_96.tdata.value, ...}` 把 CPU 写进 4 个寄存器的值拼回 128 位 `TDATA`。

「8 位对齐」的规格源头在 `csr.rdl` 的 `control` 寄存器。以 `rx.control` 为例，每个字段的 `[hi:lo]` 都落在整字节内：

[3.build/csr_build/csr.rdl:L109-L154](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L109-L154) —— `rx.control` 寄存器。`tkeep[31:16]` 占 bytes 2-3，`tlast[15:15]` 在 byte 1，`tuser_bypass_all[7:7]` / `tuser_bypass_stage[6:6]` / `tuser_src[5:3]` / `tuser_dst[2:0]` 全部塞在 byte 0。没有任何字段跨越字节边界。固件文档把这个设计决策归因于 [issue #9](https://github.com/chili-chips-ba/wireguard-fpga/issues/9)：见 [2.sw/README.md:L224](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/README.md#L224)。

> 备注：byte 0 内的 4 个 `tuser` 子字段虽然彼此共享同一字节，但它们是「同一次配置一起写」的元数据（src/dst/bypass），HAL 会把四者组合成单字节一次 `sb`（store byte）写入，所以同样不需要 RMW。

#### 4.1.4 代码实践

**实践目标**：亲手验证「128 位 beat ↔ 4 个 32 位寄存器」的拼接顺序与字节排布。

**操作步骤**（源码阅读型，无需上板）：

1. 打开 `cpu_fifo.sv` 的 `tx_fifo` 实例（L73-L84），记下 `m_axis_tdata` 的拼接顺序是 `{data_127_96, data_95_64, data_63_32, data_31_0}`（高位在前）。
2. 打开 `csr.rdl` 的 `rx.control`（L109-L154），在一张纸上画一个 32 位的格子，按 `[31:0]` 把 `tkeep`/`tlast`/4 个 `tuser` 字段填进去。
3. 假设 CPU 要发一个 beat，其中 `tuser_dst=2`（去 eth2）、`tuser_bypass_all=1`、其余 tuser=0。算出 `control` 寄存器 byte 0 的值：`tuser_dst[2:0]=010`、`tuser_bypass_all[7]=1` → byte 0 = `0b10000010` = `0x82`。

**需要观察的现象**：`tuser_dst` 与 `tuser_src` 在 byte 0 内是相邻但独立的位域；写 `0x82` 时，硬件字节使能只命中 byte 0，`tkeep`（bytes 2-3）保持上次的值不变。

**预期结果**：你应当得到 byte 0 = `0x82`，并能解释「为什么这一步是单次 store 而不是 RMW」——因为 `tuser_*` 整组就在 byte 0 内、与其它字段不共用字节。

**待本地验证**：若有生成的 `csr_hw.h`，确认 HAL 的 `csr->cpu_fifo->rx->control->tuser_dst(...)` 写入会驱动 byte 0 的字节使能。

#### 4.1.5 小练习与答案

**练习 1**：若把 `tuser_dst` 从 `[2:0]` 改成放在 bit `[9:7]`（跨入 byte 1），会引入什么问题？

**参考答案**：它会与 `tlast`（byte 1 的 bit 15）共处 byte 1，但更糟的是若字段跨字节（如 bit 6..9），CPU 无法用单条带字节使能的 store 原子更新它，必须 RMW——读回、改、写回，吞吐下降。

**练习 2**：4 个 `tdata` 寄存器（`data_31_0` 等）本身需要担心 RMW 吗？

**参考答案**：不需要。每个 `tdata` 是完整的 32 位字（4 字节），CPU 用一条 `sw`（store word）一次性写满 4 个字节使能，天然不涉及读回。

---

### 4.2 singlepulse 触发与「无时钟同步」握手

#### 4.2.1 概念说明

AXIS 的传输靠 `TVALID`/`TREADY` 握手——二者同拍为 1 才算完成一次传输。问题是：CPU 是软件，跑多少条指令才到「下一拍」根本不可预测；它没法把「置 `TVALID=1` 一拍再清零」精确对齐到 AXIS 的 125/80 MHz 时钟节拍。

解决手段是 SystemRDL 的 `singlepulse` 修饰符（u3-l1 已介绍）。把它标在触发字段上，PeakRDL 生成的 RTL 会在 CPU 写入 1 后，**自动在下一拍把该字段清零**。于是 CPU 只管「写一次 1」，硬件自己产生一个干净的单拍脉冲去驱动 AXIS 握手，CPU 完全不必关心时钟节拍对齐。

本接口里有两个 singlepulse 字段：
- `cpu_fifo.rx.trigger.tvalid`：CPU→DPE 方向，CPU 写 1 触发一次「我这个 beat 有效」。
- `cpu_fifo.tx.trigger.tready`：DPE→CPU 方向，CPU 写 1 触发一次「我取走了这个 beat」。

#### 4.2.2 核心流程

CPU 发一个 beat 的握手时序（CPU→DPE）：

```
CPU 侧动作                  CSR 字段                  硬件 (singlepulse)
─────────────────────────  ─────────────────────    ─────────────────────────
1) CPU 写 4×data + control   data_*, control = 新值   (字段已就位)
2) CPU 写 trigger.tvalid =1  tvalid = 1  ──────────▶  下一拍自动清 0
                                                     同时产生 1 拍 TVALID=1 脉冲
                                                     与 FIFO 的 TREADY 握手
3) CPU 轮询/继续下一 beat    tvalid 已回 0            传输已完成
```

关键：CPU 不需要知道「那一拍」是哪个时钟沿，也不需要在固定节拍内清零——singlepulse 替它做了。

#### 4.2.3 源码精读

规格在 `csr.rdl` 的两个 trigger 寄存器里，二者都标了 `singlepulse = true`：

[3.build/csr_build/csr.rdl:L156-L167](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L156-L167) —— `rx.trigger.tvalid`，描述明确写着 "single pulse trigger"，`singlepulse = true`。CPU 写 1 → 下一拍硬件自动清 0。

[3.build/csr_build/csr.rdl:L282-L292](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L282-L292) —— `tx.trigger.tready`，同样 `singlepulse = true`，用于 CPU 确认取走 DPE 送来的 beat。

这两个脉冲在 `cpu_fifo.sv` 里直接当 AXIS 的 `TVALID`/`TREADY` 用：

[1.hw/ip.infra/cpu_fifo.sv:L78-L79](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/cpu_fifo.sv#L78-L79) —— `tx_fifo` 的 `m_axis_tvalid` 接 `from_csr.cpu_fifo.tx.trigger.tready.value`、`m_axis_tready` 接 `...tx.status.tvalid`（注：此处命名以代码为准）。singlepulse 产生的单拍脉冲正是 AXIS 握手所需的「这一拍有效」。

固件文档对这一设计的原话见 [2.sw/README.md:L226](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/README.md#L226)：「triggering a data transfer cycle on the AXIS interface is achieved using single-pulse TVALID/TREADY signals. This frees the CPU from the requirement to synchronize the AXIS clock cycle with its instruction cycle.」

#### 4.2.4 代码实践

**实践目标**：理解 singlepulse 字段「写 1 后自动归零」的行为。

**操作步骤**（源码阅读 + 推理）：

1. 在 `csr.rdl` 找到 `rx.trigger.tvalid` 的 `singlepulse = true`（L163）。
2. 想象 CPU 执行 `csr->cpu_fifo->rx->trigger->tvalid(1)` 后立刻再读回 `tvalid`。
3. 思考：读回的值是 1 还是 0？为什么？

**需要观察的现象**：由于 singlepulse 在写入后的下一拍自动清零，而 CPU 读回至少要隔数个时钟周期（一条 load 指令就要 4 拍左右），读回的几乎必然是 0。

**预期结果**：读回为 0。这正是 singlepulse 的用意——CPU 不需要、也不应该自己去「清零」它。

**待本地验证**：在仿真（VUserMain0，见 u7-l2）里对一个 singlepulse 字段先写 1、隔几拍再读，打印其值确认已归零。

#### 4.2.5 小练习与答案

**练习 1**：如果 `tvalid` 不用 singlepulse，CPU 写 1 后必须做什么？

**参考答案**：CPU 必须在「恰好一拍」后写 0，否则 `TVALID` 会持续为 1，FIFO 会把同一个 beat 重复当作多次有效传输。但 CPU 软件时序无法精确对齐到单拍，所以做不到——这正是必须用 singlepulse 的原因。

**练习 2**：`status.tready`（rx）和 `status.tvalid`（tx）为什么不用 singlepulse？

**参考答案**：它们是 `sw=r;hw=w` 的**状态**字段，反映 FIFO 当前的硬件状态（能否接收 / 是否有有效数据），由硬件持续驱动，CPU 只读。状态位需要持续保持真实电平，不能写后即清，所以不能用 singlepulse。

---

### 4.3 CPU 收发的 10 步流程

#### 4.3.1 概念说明

有了「4×32 位拼 128 位」「singlepulse 触发」两件工具，CPU 收发一个 16 字节段（= 1 个 AXIS beat）的完整流程就固定下来了。固件文档把它总结成约 10 步。注意包可能由多个 beat 组成，所以这 10 步是「每个 beat 重复一次」，直到发出/收到 `TLAST=1` 的尾 beat。

#### 4.3.2 核心流程

**发送（CPU→DPE，操作 `cpu_fifo.rx.*`）**，每个 beat：

1. 读 `rx.status.tready`，为 1 才继续（drop-on-full 模式下它恒为 1）。
2–5. 依次写 `rx.data_31_0` / `data_63_32` / `data_95_64` / `data_127_96`（4 次写拼出 128 位 `TDATA`）。
6. 写 `rx.control.tkeep`（除尾 beat 外全 1）。
7. 写 `rx.control.tlast`（仅尾 beat 为 1）。
8. 写 `rx.control.tuser_bypass_all`（CPU 发的握手包通常 `=1`，绕过加密流水线直送）。
9. 写 `rx.control.tuser_dst`（1=eth1 … 4=eth4，7=广播）。
10. 写 `rx.trigger.tvalid = 1`（singlepulse 触发本次传输）。
11. 若 `tlast==0`，回到第 1 步发下一个 beat。

**接收（DPE→CPU，操作 `cpu_fifo.tx.*`）**，每个 beat：

1. 读 `tx.status.tvalid`，为 0 就停（store-and-forward：没有整帧就等）。
2–5. 依次读 `tx.data_31_0` / `data_63_32` / `data_95_64` / `data_127_96`。
6. 读 `tx.control.tlast`；为 1 进到 7，否则跳到 9。
7. 读 `tx.control.tkeep`（尾 beat 的有效字节掩码）。
8. 读 `tx.control.tuser_src`（包来自哪个口）。
9. 写 `tx.trigger.tready = 1`（singlepulse，确认取走本 beat）。
10. 若 `tlast==0`，回到第 1 步取下一个 beat。

#### 4.3.3 源码精读

权威流程来自固件文档：

[2.sw/README.md:L228-L239](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/README.md#L228-L239) —— 发送的 11 步（含循环判断），逐条列出 CPU 写哪些寄存器、`tuser_dst` 的取值含义（1–4 对应 eth1–4，7 为广播）。

[2.sw/README.md:L241-L251](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/README.md#L241-L251) —— 接收的 10 步，含 `tlast` 分支与 store-and-forward 等待。

这些步骤对应的寄存器全部在 `csr.rdl` 的 `cpu_fifo` regfile 里：

[3.build/csr_build/csr.rdl:L53-L180](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L53-L180) —— `cpu_fifo.rx` 子树：4 个 data 寄存器（`sw=rw;hw=r`，CPU 写硬件读）、control（同）、trigger.tvalid（singlepulse）、status.tready（`sw=r;hw=w`，硬件写 CPU 读）。`tx` 子树（L182-L305）方向镜像：data 变成 `sw=r;hw=w`，trigger 变成 tready，status 变成 tvalid。

发出的包最终落到哪里？`from_cpu` 这条 AXIS 流被送进 DPE 的多路复用器，作为它的 CPU 输入队列之一参与轮询：

[1.hw/ip.dpe/dpe_multiplexer.sv:L82-L97](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L82-L97) —— 多路复用器 FSM 的 `R0`/`S0` 态处理 `from_cpu` 队列：看到 `from_cpu.tvalid` 就把这个 beat 转发给 `to_dpe_sbuff`，并在 `from_cpu.tlast` 时结束本包。CPU 发出的握手包经此进入 DPE 流水线（详见 u4-l2）。

#### 4.3.4 代码实践

**实践目标**：写出 CPU 发送**一个 16 字节段**（单 beat，`tlast=1`）的完整 CSR 读写序列，并填入示例值。

**操作步骤**（纸面推演，承接本讲总实践任务）：

假设要发一个 16 字节的 WireGuard 报文头，去往 eth2，绕过加密流水线（`bypass_all=1`），载荷示意 `TDATA = 0x00112233_44556677_8899AABB_CCDDEEFF`（小端，`data_31_0` 存低 32 位）：

```
1. 读  csr.cpu_fifo.rx.status.tready   // 期望 = 1
2. 写  csr.cpu_fifo.rx.data_31_0.tdata = 0xCCDDEEFF   // TDATA[31:0]
3. 写  csr.cpu_fifo.rx.data_63_32.tdata = 0x8899AABB  // TDATA[63:32]
4. 写  csr.cpu_fifo.rx.data_95_64.tdata = 0x44556677  // TDATA[95:64]
5. 写  csr.cpu_fifo.rx.data_127_96.tdata = 0x00112233 // TDATA[127:96]
6. 写  csr.cpu_fifo.rx.control.tkeep = 0xFFFF         // 全 16 字节有效（尾 beat）
7. 写  csr.cpu_fifo.rx.control.tlast = 1              // 这是最后一 beat
8. 写  csr.cpu_fifo.rx.control.tuser_bypass_all = 1   // 绕过 DPE 加密
9. 写  csr.cpu_fifo.rx.control.tuser_dst = 2          // 目的 = eth2
10.写  csr.cpu_fifo.rx.trigger.tvalid = 1             // singlepulse，触发传输
```

**需要观察的现象**：第 2–9 步都是「写」、只命中各自字节（data 各占独立字、control 各字段按字节对齐），全程无读回；第 10 步写后 `tvalid` 自动归零，一个 128 位 beat 进入 `from_cpu` → 多路复用器 → eth2 的 Tx FIFO。

**预期结果**：eth2 端口应能看到一个 16 字节的明文帧（因为 `bypass_all=1`）。

**待本地验证**：在 u7-l4 的 udpIpPg 仿真里把 CPU 换成 VUserMain0 跑这段序列，或在实板用 Wireshark 抓 eth2 口确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么第 1 步读 `tready`「几乎总是 1」，而接收流程第 1 步读 `tvalid` 却常常需要等待？

**参考答案**：发送方向（`rx` FIFO）配为 drop-on-full，满了就丢而不是反压，所以 `tready` 恒为 1；接收方向（`tx` FIFO）配为 store-and-forward（整帧才提交），DPE 没有送来完整帧时 `tvalid` 为 0，CPU 必须等。

**练习 2**：若一个包有 48 字节（= 3 个 beat），前两个 beat 的第 6/7 步该写什么？

**参考答案**：前两个 beat 的 `tkeep = 0xFFFF`（全有效）、`tlast = 0`（非尾）；只有第 3 个 beat `tlast = 1`，且其 `tkeep` 按实际有效字节设置。

---

### 4.4 CPU 接口吞吐估算

#### 4.4.1 概念说明

把一条 128 位线速总线「降级」成 4 次 32 位 CSR 访问是要付代价的：CPU 要执行一大堆 load/store 指令才能搬动一个 beat。本模块算清楚这笔账，得出结论：这个接口撑不起线速数据面，但跑握手绰绰有余。

#### 4.4.2 核心流程

固件文档给的估算模型：每个 beat 走完约 10 步，每步平均 ~1.5 条指令，picoRV32 每条指令约 4 个时钟周期。于是搬一个 128 位 beat 耗时：

\[ T_{\text{beat}} = N_{\text{steps}} \times \bar{N}_{\text{instr}} \times N_{\text{cycles}} = 10 \times 1.5 \times 4 = 60 \;\text{周期} \]

吞吐（每秒搬多少 bit）：

\[ \text{吞吐} = \frac{f_{\text{clk}} \times W}{N_{\text{steps}} \times \bar{N}_{\text{instr}} \times N_{\text{cycles}}} \]

代入控制面时钟域参数（80 MHz@32 bit，见 u2-l3）：\(f_{\text{clk}} = 80\times10^{6}\,\text{Hz}\)，\(W = 128\,\text{bit}\)：

\[ \text{吞吐} = \frac{80\times10^{6} \times 128}{60} = \frac{10.24\times10^{9}}{60} \approx 170.7\,\text{Mbps} \]

对比：单口千兆 datapath 是 1 Gbps，四口聚合 4 Gbps。\(170\,\text{Mbps} \ll 1\,\text{Gbps}\)，所以**绝不能让用户数据穿 CPU**。

#### 4.4.3 源码精读

权威估算来自固件文档：

[2.sw/README.md:L253](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/README.md#L253) —— 原文公式与结论："80 MHz × 128 bits ÷ (10 steps × 1.5 instructions × 4 cycles per instruction) = 170 Mbps ... this CSR-based FIFO interface cannot be used to implement a 1G datapath through the CPU, but since the CPU will only process WireGuard handshake messages, this will be more than sufficient."

80 MHz 这个数字来自控制面时钟域（u2-l3 的红色域：80 MHz@32 bit 接 CPU/CSR）。FIFO 自身容量很大（DEPTH=4096 beat），不是瓶颈：

[1.hw/ip.infra/cpu_fifo.sv:L51-L56](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/cpu_fifo.sv#L51-L56) —— 两个 `axis_fifo` 都配 `DEPTH=4096`、`DATA_WIDTH=128`、`FRAME_FIFO=1`（整帧存储转发）、`DROP_WHEN_FULL=1`（满则丢不反压）。瓶颈不在 FIFO 深度，而在 CPU 搬数据的指令吞吐。

> 设计含义：这就是为什么本项目的架构（u2-l1）坚持把用户数据全程留在 DPE 线速闭环、只让稀疏的握手包进 CPU——握手报文小且低频（WireGuard 大约每两分钟才重协商一次密钥），170 Mbps 的接口对它而言是「杀鸡用牛刀」。

#### 4.4.4 代码实践

**实践目标**：自己重算吞吐，并测试「让 CPU 跑满 1 Gbps 需要多高主频」。

**操作步骤**：

1. 用上面的公式，把步数从 10 改成「理想极限」2 步（一次写 data、一次写 trigger），重算吞吐。
2. 反解：要让吞吐达到 1 Gbps（\(10^9\) bps），在 10 步模型下需要 \(f_{\text{clk}}\) 多少？

**需要观察的现象**：

- 步数降至 2 时：\(\text{吞吐} = 80\times10^6 \times 128 / (2\times1.5\times4) \approx 853\) Mbps——即便「作弊」到 2 步仍跑不满 1 Gbps，因为单 beat 还要 12 周期。
- 反解主频：\(f_{\text{clk}} \ge 10^9 \times 60 / 128 \approx 469\) MHz——picoRV32 在 Artix-7 上根本到不了这个主频（核心逻辑 < 100 MHz，见 u1-l3）。

**预期结果**：两个角度都证明「CSR 化的 CPU 接口不可能线速」，从而坐实「数据面必须靠 RTL、CPU 只管握手」的架构选择。

**待本地验证**：若有 picoRV32 的实际 CPI（cycles per instruction）数据，可把 4 替换成实测均值，得到更精确的 170 Mbps 估计。

#### 4.4.5 小练习与答案

**练习 1**：若把控制面时钟域提到 200 MHz（假设器件允许），吞吐变成多少？够 1 Gbps 吗？

**参考答案**：\(200\times10^6 \times 128 / 60 \approx 427\) Mbps，仍远低于 1 Gbps。结论不变：位宽拆分 + 多步访问的固有开销，单靠提频无法弥补。

**练习 2**：为什么 FIFO 深度做到 4096 也救不了吞吐？

**参考答案**：FIFO 深度解决的是「突发」与「反压」问题，不解决「CPU 每beat要执行约 15 条指令」的指令吞吐瓶颈。搬数据的速率被 CPU 执行速度卡死，与 FIFO 多深无关。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「端到端」的纸面推演：

**任务**：一个 WireGuard 握手 initiation 报文（假设 92 字节 = 6 个 16 字节 beat，最后一个 beat 仅 12 字节有效）要从 CPU 经 `cpu_fifo` 发往 eth3，全程绕过加密流水线（`bypass_all=1`）。请：

1. **拆 beat**：列出 6 个 beat 各自的 `tkeep` 与 `tlast` 取值（提示：前 5 个 `tkeep=0xFFFF, tlast=0`；最后一个 `tkeep=0x0FFF, tlast=1`）。
2. **写序列**：写出**最后一个 beat**的 10 步 CSR 读写序列，标注 `tuser_dst=3`、`tuser_bypass_all=1`，并指出 `control` 寄存器 byte 0 的合成值（`tuser_dst[2:0]=011`、`bypass_all[7]=1` → byte 0 = `0b10000011` = `0x83`）。
3. **标握手**：在第 10 步旁注明 singlepulse 如何把 `tvalid=1` 变成一拍脉冲，并说明 CPU 为何不必去清零它。
4. **算时间**：用 4.4 的公式估算发完整个 92 字节报文耗时（6 beat × 60 周期 / 80 MHz ≈ 4.5 µs），并与「一次握手每两分钟才发生一次」对照，说明 170 Mbps 接口完全够用。
5. **画路径**：画出 `CPU → 4×data 寄存器拼接成 128 位 → rx_fifo(DROP_WHEN_FULL) → from_cpu(AXIS) → dpe_multiplexer 的 R0/S0 队列 → eth3 Tx FIFO` 的完整数据通路，并标注「用户数据绝不走这条路」。

**预期产出**：一张完整的「寄存器序列 + 时序标注 + 数据通路框图」。完成后，你应当能向别人讲清：为什么这条 ~170 Mbps 的窄桥，恰恰是整个 SoC 软硬件分工得以成立的关键。

## 6. 本讲小结

- `cpu_fifo` 用两个 `axis_fifo` 把数据面 128 位 AXIS 与控制面 32 位 CSR 对接：每个 128 位 beat 被拆成 4 个 32 位 `data_*` 寄存器，`TKEEP`/`TLAST`/`TUSER` 落到 `control` 寄存器的各字段。
- 命名易混：`rx`/`tx` 是相对 DPE 的——`rx`(CPU→DPE，CPU 写)、`tx`(DPE→CPU，CPU 读)；`to_cpu`/`from_cpu` 接口同理。
- 所有字段对齐到 8 位边界（issue #9），让 CPU 用带字节使能的单次 store 更新字段，避免 RMW 拖慢吞吐。
- 两个触发位 `rx.trigger.tvalid` / `tx.trigger.tready` 用 `singlepulse`，CPU 写 1 后硬件自动清零，产生单拍 AXIS 握手脉冲，免去软件对齐时钟节拍。
- CPU 收发一个 16 字节段是固定的约 10 步 CSR 读写序列，逐 beat 重复直到 `TLAST=1`；发送走 drop-on-full（`tready` 恒 1），接收走 store-and-forward（等整帧 `tvalid`）。
- 该接口吞吐 ≈ 170 Mbps（80 MHz × 128 bit ÷ 60 周期），远低于 1 Gbps datapath，故只承载稀疏的 WireGuard 握手，用户数据全程在 DPE 线速闭环。

## 7. 下一步学习建议

- **u3-l4（FCR 流控寄存器与原子更新）**：本讲只讲了「包级」通道。当 CPU 改完握手、要更新路由表/密钥表时，怎么保证 DPE 不会读到半成品表项？答案就是 FCR 的 pause/idle 原子握手——它是 `cpu_fifo` 之外的「控制级」CSR 通道，下一讲专题讲解。
- **u4-l1（DPE 总体结构与 AXIS 元数据）**：本讲发出的包进入 `dpe_multiplexer` 后怎么被路由？`TUSER`/`TID` 元数据的完整编码（src/dst/bypass）在 U4 系统讲透。
- **u4-l6（路由表与密钥表的 tdp_ram 实现）**：本讲提到 `rx`/`tx` 是「包级」桥梁，而路由/密钥表是「表级」桥梁——后者用 `tdp_ram` 双口 RAM 实现，是 cpu_fifo 之外的另一类 CSR 通道。
- **延伸阅读**：可对照 PeakRDL regblock 文档对 [singlepulse](https://peakrdl-regblock.readthedocs.io/en/latest/props/field.html#singlepulse) 字段的生成逻辑，亲手看一眼生成的 `csr.sv` 里 `tvalid` 字段如何「写 1 后下一拍清零」，把本讲的概念落到门级。
