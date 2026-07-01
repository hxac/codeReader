# TileLink-UL 与 AXI 桥接

## 1. 本讲目标

CoralNPU 的 SoC **内部**用 TileLink-UL（简称 TL-UL）总线把标量核、SRAM、外设串起来，而**对外**却暴露 AXI4 接口去对接 DDR、ISP 这类工业标准 IP。于是在「内 TL-UL、外 AXI」的交界处，必须有两个方向的协议桥接器。本讲学完后你应当能够：

1. 说清楚 **TL-UL 只有 A、D 两个通道**（以及它与完整 TileLink 五通道的关系），并解释 `Decoupled` 握手。
2. 读懂 `Axi2TLUL`（AXI 主机 → TL-UL 从机）如何把一次 AXI 读/写**拆**成 TL-UL 的 `Get`/`Put`，以及它如何**展开 AXI 突发**。
3. 读懂 `TLUL2Axi`（TL-UL 主机 → AXI 从机）如何把 TL-UL 的 `Put`/`Get` **合成**成 AXI 写/读，以及它为何只发**单拍**。
4. 用一张「字段映射表」讲清两个方向的逐字段对应关系，并理解「内 TL-UL、外 AXI」这一设计取舍。

---

## 2. 前置知识

- **总线与协议**：总线是多个模块共享的通信通道；协议规定了「谁发、发什么、对方何时收」的规则。
- **AXI4**：ARM 制定的工业标准片上总线，分读、写两大组，每组又有「地址」「数据」「响应」子通道，支持突发（burst）和独立的 ID。上一讲 [u3-l2](u3-l2-axi-integration.md) 已经讲过 CoralNPU 的 AXI 接口，本讲是它的「内部镜像」。
- **TileLink**：SiFive（OpenRocket/OpenTitan 生态）提出的另一套片上总线协议。**完整版**有 A、B、C、D、E 五个通道；**TL-UL（Uncached Lightweight，无缓存轻量版）**只保留 A（请求）和 D（响应）两个通道——这正是「轻量」二字的含义。CoralNPU 内部用的就是 TL-UL。
- **`Decoupled` 握手**：Chisel 标准库里的 `Decoupled` 把一组数据包成 `valid`（我有数据）+ `ready`（我能收）+ `bits`（数据本身）。只有同一拍 `valid && ready` 同时为高，这次传输（fire）才生效；任一端没准备好都能形成「反压」（backpressure）。
- **host 与 device**：发起请求的一方叫 host（主）/manager，响应的一方叫 device（从）/subordinate。TL-UL 里 host 发 A 通道、收 D 通道；device 收 A 通道、发 D 通道。

> 一个关键直觉：TL-UL 的「一次请求 → 一次响应」模型和 AXI 的「地址通道 → 数据/响应通道」模型并不一一对应，桥接的本质就是**把 AXI 的多通道事务重新打包成 TL-UL 的单 A + 单 D，或反之**。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [hdl/chisel/src/bus/TileLinkUL.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TileLinkUL.scala) | 定义 TL-UL 的参数、操作码、A/D 通道字段，是两个桥接器共用的「TL-UL 词汇表」。 |
| [hdl/chisel/src/bus/Axi.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Axi.scala) | 定义 AXI 的地址/数据/响应 Bundle 与 `AxiMasterIO`，是两个桥接器共用的「AXI 词汇表」。 |
| [hdl/chisel/src/bus/Axi2TLUL.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Axi2TLUL.scala) | **AXI 主机 → TL-UL 从机**桥接器。把 AXI 读写翻译成 TL-UL `Get`/`Put`，支持 AXI 突发展开。 |
| [hdl/chisel/src/bus/TLUL2Axi.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TLUL2Axi.scala) | **TL-UL 主机 → AXI 从机**桥接器。把 TL-UL `Put`/`Get` 翻译成 AXI 写/读，只发单拍事务。 |
| [hdl/chisel/src/bus/TlulIdRemapper.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulIdRemapper.scala) | 当 TL-UL 的 source 位宽大于 AXI id 位宽时，`TLUL2Axi` 用来做 ID 重映射的辅助模块。 |
| [hdl/chisel/src/coralnpu/CoreTlul.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreTlul.scala) | 把「说 AXI」的 `CoreAxi` 包装成「说 TL-UL」的 host+device，是两个桥接器在核内的真实用法。 |

---

## 4. 核心概念与源码讲解

### 4.1 TileLink-UL 总线定义（与 AXI Bundle 词汇表）

#### 4.1.1 概念说明

桥接器的两边各说一种「方言」。要读懂桥接，先得把两种方言的「字母表」背下来：

- **TL-UL 字母表**（`TileLinkUL.scala`）：只有 **A 通道**（host 发给 device 的请求）和 **D 通道**（device 回给 host 的响应）。请求有三种操作码：`Get`（读）、`PutFullData`（整拍写）、`PutPartialData`（带字节掩码的部分写）；响应有两种：`AccessAckData`（带数据的读回应）、`AccessAck`（不带数据的写回应）。
- **AXI 字母表**（`Axi.scala`）：读有 AR（读地址）+ R（读数据）；写有 AW（写地址）+ W（写数据）+ B（写响应）。每种地址事务还带 `len`（突发长度）、`size`（每拍字节）、`burst`（FIXED/INCR/WRAP）、`id` 等字段。

> ⚠️ 关于「A/B/D 通道」的澄清：完整 TileLink 确实有 A/B/C/D/E 五个通道（B/C 用于缓存一致性）。但 **TL-UL 是无缓存轻量版，只保留 A 与 D**。本仓库 `TileLinkUL.scala` 全文只定义了 A 和 D，没有 B/C。所以读到「TL-UL 的通道」时，请默认就是 A（请求）+ D（响应）这两个。

#### 4.1.2 核心流程

TL-UL 一次完整事务的流程极简：

```
host                          device
  │── A 通道: opcode=Get/Put, address, data, mask, source ──▶│
  │                                                          │
  │◀── D 通道: opcode=AccessAck/AccessAckData, data, error ──│
```

- host 在 A 通道上发请求，`source` 字段相当于 AXI 的 `id`，用来在响应里「认领」自己的那次请求。
- device 处理完后在 D 通道回响应，把同一个 `source` 带回来；读请求回 `AccessAckData`（带数据），写请求回 `AccessAck`（只带成功/失败的 `error`）。
- 两侧都走 `Decoupled` 握手：A 通道由 host 驱动 `valid`、device 驱动 `ready`；D 通道方向相反。

AXI 则把上面这一来一回**拆成更多通道**，并允许一个地址事务对应多拍数据（突发）。桥接就是把这两种「粒度」对齐。

#### 4.1.3 源码精读

先看 TL-UL 的参数推导。所有位宽都不是写死的，而是从全局 `Parameters` 算出来的：

[TileLinkUL.scala:22-28](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TileLinkUL.scala#L22-L28)：`TLULParameters` 从 `Parameters` 推导 TL-UL 各字段宽度。`w` 是数据字节数（`axi2DataBits/8`），`a` 是地址位宽，`z=log2Ceil(w)` 是 `size` 字段位宽，`o` 是 source（≈AXI id）位宽。

在 CoralNPU 默认配置下（见 `Parameters.scala`，`axi2DataBits=256`、`axi2AddrBits=32`、`axi2IdBits=6`），可得：\( w=32 \) 字节、\( a=32 \) 位地址、\( z=5 \)（所以 `size` 最大编码 32 字节）、\( o=6 \)（source 有 64 个值）。

接着是操作码定义，这是后面两个桥接器反复引用的常量：

[TileLinkUL.scala:30-41](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TileLinkUL.scala#L30-L41)：A 通道操作码 `PutFullData=0 / PutPartialData=1 / Get=4`；D 通道操作码 `AccessAck=0 / AccessAckData=1`。

A/D 通道的具体字段（注意 D 比 A 多一个 `sink` 和一个 `error`，A 比 D 多 `mask`）：

[TileLinkUL.scala:61-81](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TileLinkUL.scala#L61-L81)：`TileLink_A_ChannelBase` 与 `TileLink_D_ChannelBase`。A 通道有 `opcode/param/size/source/address/mask/data/user`；D 通道有 `opcode/param/size/source/sink/data/user/error`。

host/device 两种朝向的封装（注意 `Flipped` 的位置决定了谁是输出方）：

[TileLinkUL.scala:86-94](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TileLinkUL.scala#L86-L94)：`TLULHost2Device` 里 `a` 是输出、`d` 是输入（Flipped）；`TLULDevice2Host` 正好相反。

再看 AXI 侧的「字母表」。最常用的是两个枚举和几个 Bundle：

[Axi.scala:20-31](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Axi.scala#L20-L31)：`AxiResponseType`（OKAY/EXOKAY/SLVERR/DECERR）与 `AxiBurstType`（FIXED/INCR/WRAP）。两个桥接器都会把 TL-UL 的 `error` 翻译成 `OKAY` 或 `SLVERR`。

[Axi.scala:34-71](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Axi.scala#L34-L71)：`AxiAddress`（含 `addr/id/len/size/burst/...`）与 `AxiWriteData`（含 `data/last/strb`）。桥接时 `len/size/burst` 决定突发，`strb` 对应 TL-UL 的 `mask`。

[Axi.scala:124-138](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Axi.scala#L124-L138)：`AxiMasterIO` 把写（AW/W/B）和读（AR/R）打包成一个 master 端口。两个桥接器都用它做 AXI 侧接口。

#### 4.1.4 代码实践

**实践目标**：把 TL-UL 与 AXI 的字段对齐成一张速查表，为后两节的映射打基础。

**操作步骤**：
1. 打开 `TileLinkUL.scala`，在 A/D 通道定义里数清每个字段及其位宽（用 `p.w/p.a/p.z/p.o/p.i` 代入默认值算出具体位数）。
2. 打开 `Axi.scala`，对照 `AxiAddress`/`AxiWriteData`/`AxiReadData`/`AxiWriteResponse`，列出 AXI 各通道字段。
3. 自己画一张「TL-UL ↔ AXI 概念对应」表：例如 TL-UL `source` ↔ AXI `id`，TL-UL `mask` ↔ AXI `strb`，TL-UL `Get` ↔ AXI 读（AR+R），TL-UL `PutFull` ↔ AXI 写（AW+W+B）。

**预期结果**：你会得到一张类似下表的概念对照（细节在 4.2/4.3 再精确到字段）：

| 含义 | TL-UL | AXI |
|---|---|---|
| 事务标识 | `source` | `id` |
| 字节使能 | `mask` | `strb` |
| 单拍大小 | `size` | `size` |
| 读请求 | `Get`（A 通道） | AR 通道 |
| 写请求 | `PutFull/PutPartial`（A 通道） | AW + W 通道 |
| 读响应（带数据） | `AccessAckData`（D 通道） | R 通道 |
| 写响应 | `AccessAck`（D 通道） | B 通道 |
| 出错 | `error`（D 通道） | `resp`（R/B 通道） |

#### 4.1.5 小练习与答案

**练习 1**：TL-UL 为什么没有 AXI 那种「地址通道」和「数据通道」之分？
**参考答案**：TL-UL 把请求的所有信息（地址、数据、掩码）都塞进**一个 A 通道**里一次发完，响应也只用**一个 D 通道**回。AXI 则按读/写把地址、数据、响应拆成独立通道以支持并发与突发。模型不同，所以需要桥接。

**练习 2**：默认配置下 TL-UL 的 `source` 有多少个取值？`size` 字段最大能表示多大的单拍？
**参考答案**：`source` 位宽 `o=6`，共 64 个取值；`size` 位宽 `z=5`，最大编码 \( 2^5=32 \) 字节（即 256 位），正好等于数据总线宽度。

---

### 4.2 Axi2TLUL：AXI 主机 → TL-UL 从机桥接

#### 4.2.1 概念说明

`Axi2TLUL` 的角色是：一个 **AXI master** 想访问一个 **TL-UL device**。它把 AXI 的读翻译成 TL-UL 的 `Get`，把 AXI 的写翻译成 TL-UL 的 `PutFull/PutPartial`，再把 TL-UL 的 D 通道响应打包回 AXI 的 R/B 通道。

这个方向的真实使用场景有两处（见第 5 节综合实践与 [u3-l1](u3-l1-soc-subsystem.md)）：
- 在 SoC 顶层，**外部 ISP AXI 主机**经 `Axi2TLUL` 把数据注入内部 TL-UL fabric；
- 在核内 `CoreTlul`，标量核自己的 AXI master（取指/访存）经 `Axi2TLUL` 转成 TL-UL host 去访问 SRAM/外设。

#### 4.2.2 核心流程

```
          ┌────────────────────── Axi2TLUL ──────────────────────┐
AXI master│  read_addr_q ─▶[读展开]─▶ Get ─┐                    │ ─▶ TL-UL A 通道
─ AR/WB/W ┤  write_addr_q+write_data_q ─▶ Put ─┤(RR 仲裁,读优先)│
          │  ◀─ [D 解复用] ◀─────────────── ┤                   │ ◀─ TL-UL D 通道
          └── R(读数据) / B(写响应) ◀───────────────────────────┘
```

要点：
1. **入队缓冲**：AXI 的 AR/AW/W 先进深度为 2 的 `Queue`，吸收两侧时序差。
2. **突发展开（Burst Unroll）**：AXI 一个地址事务可能带 \( \text{len}+1 \) 拍数据；TL-UL 的 A 通道每拍只能描述一次单拍访问，所以要把一次 AXI 突发**展开成多拍 TL-UL 请求**，地址按 `size` 递增（INCR）或不变（FIXED）。
3. **读优先仲裁**：展开后的读流（`Get`）和写流（`Put`）经 `CoralNPURRArbiter` 仲裁，读口接在 `in(0)` 享有优先级。
4. **D 通道解复用**：TL-UL 的 `AccessAckData` 走 AXI R 通道、`AccessAck` 走 AXI B 通道；用每个 id 的 `beats_left` 计数判断是否最后一拍（`last`）。

#### 4.2.3 源码精读

模块端口：AXI 侧是 `Flipped(AxiMasterIO)`（即一个 AXI **slave** 端口，接 AXI master），TL-UL 侧给出 `tl_a`（输出请求）和 `tl_d`（输入响应）。

[Axi2TLUL.scala:36-42](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Axi2TLUL.scala#L36-L42)：端口定义。注意 `io.axi` 用 `Flipped`，所以这个桥接器对外是个 AXI 从机。

一个有特色的细节：用 AXI 的 **id 来标注「这是取指还是访存」**。id==1 是 IBus（取指），其余是 DBus（数据），写进 TL-UL A 通道的 `user.instr_type` 字段（多比特 MuBi4 真/假）。这与 [u3-l2](u3-l2-axi-integration.md) 讲的「读通道用 id 区分取指/数据」一脉相承。

[Axi2TLUL.scala:44-46](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Axi2TLUL.scala#L44-L46)：`idToInstrType` 把 AXI id 映射成 TL-UL 的 `instr_type`（id==1 为指令）。

读路径的核心是「展开状态机」。一组 `r_unroll_*` 寄存器记录当前突发是否在展开、当前地址/id/size/剩余 len/突发类型：

[Axi2TLUL.scala:52-102](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Axi2TLUL.scala#L52-L102)：读突发展开。`r_unroll_busy` 标记是否正在展开；`r_addr_inc = 1 << size` 是每拍地址增量；FIXED 突发地址不变，INCR 突发地址累加；`r_beats_left`/`r_burst_active` 是**按 id 索引**的向量（共 \( 2^{\text{idBits}}=64 \) 项），用来跟踪每个 id 还剩几拍、是否在途，并用 `id_conflict` 阻止同一 id 的事务重叠。

展开后产出的 `read_stream` 就是标准的 TL-UL `Get`：

[Axi2TLUL.scala:74-84](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Axi2TLUL.scala#L74-L84)：把 AXI 读地址翻译成 TL-UL `Get`——`opcode=Get`、`mask` 全 1（读不看掩码）、`data=0`、`source=id`、`address=当前地址`、`user.instr_type` 来自 id。

> 关于文件头注释：`Axi2TLUL.scala` 第 31-32 行的注释说「只处理单拍（len=0）事务」。但上面的展开状态机（`r_unroll_len`/`w_unroll_len`、FIXED/INCR 分支、`beats_left`）明确实现了**突发展开**。注释偏保守，以代码为准——后续的 cocotb 测试 `test_read_burst`/`test_write_burst` 也验证了突发路径。

写路径结构对称，但多了「整拍 vs 部分写」的判断：当 AXI 的 `strb` 全 1 时发 `PutFullData`，否则发 `PutPartialData`，并把 `strb` 直接当成 TL-UL 的 `mask`：

[Axi2TLUL.scala:128-140](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Axi2TLUL.scala#L128-L140)：写流构造。`is_full = strb 全 1` 决定 `PutFull/PutPartial`；`mask = strb`、`data = w_data.data`。

读/写两路经轮询仲裁器汇成单一 A 通道，读口（`in(0)`）优先：

[Axi2TLUL.scala:164-170](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Axi2TLUL.scala#L164-L170)：`CoralNPURRArbiter` 把 `read_stream`(0) 与 `write_stream`(1) 仲裁到 `io.tl_a`。

响应方向把 TL-UL D 通道解复用到 AXI R/B 通道，并按 id 计数判断 `last`：

[Axi2TLUL.scala:172-191](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Axi2TLUL.scala#L172-L191)：`AccessAckData`→R 通道（带 `data`、按 `beats_left` 产 `last`），`AccessAck`→B 通道（仅在 `w_d_last` 时拉高 `resp.valid`）；`error` 翻译成 `SLVERR`，否则 `OKAY`。

#### 4.2.4 代码实践

**实践目标**：亲手画出「一次 AXI 读 → TL-UL `Get` → TL-UL `AccessAckData` → AXI R」的逐字段映射表。

**操作步骤**：
1. 对照上面 [Axi2TLUL.scala:74-84](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Axi2TLUL.scala#L74-L84) 与 [172-191 行](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Axi2TLUL.scala#L172-L191)，把 AXI AR 通道每个字段填到 TL-UL A 通道的哪个字段。
2. 反向再把 D 通道响应字段填回 AXI R 通道。

**预期结果**（请求方向）：

| AXI 读地址 (AR) | → | TL-UL A 通道 (`Get`) |
|---|---|---|
| `AR.addr` | → | `A.address` |
| `AR.id` | → | `A.source` |
| `AR.size` | → | `A.size` |
| —（固定） | → | `A.opcode = Get(4)` |
| —（固定） | → | `A.mask = 全 1` |
| `AR.id==1 ?` | → | `A.user.instr_type`（指令/数据） |

（响应方向）：

| TL-UL D 通道 (`AccessAckData`) | → | AXI 读数据 (R) |
|---|---|---|
| `D.source` | → | `R.id` |
| `D.data` | → | `R.data` |
| `D.error` | → | `R.resp`（OKAY / SLVERR） |
| `beats_left==0` | → | `R.last` |

**待本地验证**：若你想确认突发展开，把 AXI 读配置成 `len=3, size=5`（4 拍 ×32 字节），数一数 TL-UL A 通道应连续出现 4 个 `Get`、地址依次 \( +32 \)。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Axi2TLUL` 要用「按 id 索引」的 `beats_left`/`burst_active` 向量，而不是单个计数器？
**参考答案**：AXI 允许多个不同 id 的事务在途（outstanding），它们的响应可能交错返回。只有按 id 分别计数，才能正确判断「某个 id 的第几拍是 last」、并防止同一 id 的新事务与旧突发撞车（`id_conflict`）。

**练习 2**：AXI 的 `strb`（写字节使能）在 TL-UL 侧变成了什么？
**参考答案**：直接变成 TL-UL A 通道的 `mask`；并且 `strb` 全 1 时映射为 `PutFullData`，否则映射为 `PutPartialData`。

---

### 4.3 TLUL2Axi：TL-UL 主机 → AXI 从机桥接

#### 4.3.1 概念说明

`TLUL2Axi` 是反方向：一个 **TL-UL host** 想访问一个 **AXI device**。它把 TL-UL 的 `Put` 翻译成 AXI 的 AW+W、`Get` 翻译成 AR，再把 AXI 的 R/B 响应打包成 TL-UL 的 `AccessAckData`/`AccessAck`。

典型用法（[u3-l1](u3-l1-soc-subsystem.md) 已提过）：内部 TL-UL fabric 访问**外部 DDR 控制器 / DDR 内存**时，先（可选地）经宽度桥，再经 `TLUL2Axi` 转成对外的 AXI4。

#### 4.3.2 核心流程

```
          ┌──────────────────── TLUL2Axi ─────────────────────┐
TL-UL host│  tl_a ─▶ [tl_a_q] ─┬─ is_put ─▶ AW + W (len=0)    │ ─▶ AXI AW/W
─ A 通道 ─┤                    └─ is_get ─▶ AR        (len=0) │ ─▶ AXI AR
          │  ◀─ [D 仲裁] ◀─ read_response(AccessAckData) ────│ ◀─ AXI R
─ D 通道 ─▶│  ◀─            write_response(AccessAck) ────────│ ◀─ AXI B
          └──────────────────────────────────────────────────┘
```

与 4.2 的关键**不对称**：
- `Axi2TLUL`（进）会**展开 AXI 突发**成多拍 TL-UL；
- `TLUL2Axi`（出）却**只发单拍**——它把 AXI 的 `len` 恒置 0（见下）。也就是说，每一拍 TL-UL 请求都独立变成一次 AXI 单拍事务，不在 AXI 侧聚合突发。
- 此外，当 TL-UL 的 source 位宽大于 AXI id 位宽时，会插入一个 `TlulIdRemapper` 做 ID 收窄与还原。

#### 4.3.3 源码精读

端口：TL-UL 侧 `tl_a`（Flipped 输入）、`tl_d`（输出），AXI 侧是 `AxiMasterIO`（即一个 AXI **master** 端口，驱动外部 AXI slave）。注意它接收**两套参数** `p_tl` 和 `p_axi`，因为两侧位宽可能不同。

[TLUL2Axi.scala:35-41](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TLUL2Axi.scala#L35-L41)：端口定义。AXI 侧是 master 输出。

可选的 ID 重映射：当 TL-UL source 位宽 > AXI id 位宽时实例化 `TlulIdRemapper`——把 source 截断到 AXI id 宽度发出，并用一张表记住「截断后的 id ←→ 原 source」，等响应回来再还原。

[TLUL2Axi.scala:43-69](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TLUL2Axi.scala#L43-L69)：ID 重映射分支。`TlulIdRemapper` 的实现见 [TlulIdRemapper.scala:43-67](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulIdRemapper.scala#L43-L67)：用 `truncated_id` 发出，`outstanding_reg` 防止截断后撞号，`saved_source_id_map` 在响应时还原原 source。

判断请求类型，决定走写路径还是读路径：

[TLUL2Axi.scala:78-80](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TLUL2Axi.scala#L78-L80)：`is_get`/`is_put`。注意 `PutFullData` 和 `PutPartialData` 都被当作写。

写路径：把 TL-UL `Put` 拆成 AXI 的 AW 和 W 两个队列，**`len` 恒为 0**、`burst=INCR`、`strb=mask`、`last=true`：

[TLUL2Axi.scala:84-105](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TLUL2Axi.scala#L84-L105)：`aw_q`/`w_q` 构造。`aw_q.enq.bits.len := 0.U`（单拍）、`w_q.enq.bits.last := true.B`、`strb := mask`。

读路径：把 `Get` 变成 AXI AR，同样 `len=0`：

[TLUL2Axi.scala:107-114](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TLUL2Axi.scala#L107-L114)：`io.axi.read.addr` 构造，`len := 0.U // No bursting`。这就是「出方向只发单拍」的源头。

> 一个细节：`size` 在 AXI 数据宽 256 位时被强制写成 5（即 32 字节），见 [TLUL2Axi.scala:132-136](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TLUL2Axi.scala#L132-L136)。它用 `read_tx_info_q`/`write_tx_info_q` 把「这次请求的 size」存到响应阶段，以便 D 通道的 `size` 字段回填正确。

响应方向：AXI R → TL-UL `AccessAckData`，AXI B → `AccessAck`，二者再经 `CoralNPURRArbiter` 仲裁到单一 D 通道：

[TLUL2Axi.scala:142-165](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TLUL2Axi.scala#L142-L165)：读响应 `AccessAckData`（带 `data`，`error = resp != 0`），写响应 `AccessAck`（`error = resp != 0`）；`source` 都来自 AXI 的 `id`。

#### 4.3.4 代码实践

**实践目标**：画出「一次 TL-UL `Put` → AXI AW+W → AXI B → TL-UL `AccessAck`」的逐字段映射表。

**操作步骤**：
1. 对照 [TLUL2Axi.scala:84-105](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TLUL2Axi.scala#L84-L105) 与 [153-162 行](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TLUL2Axi.scala#L153-L162)，把 TL-UL A 通道每个字段填到 AXI AW/W 通道的哪个字段。
2. 反向把 AXI B 响应填回 TL-UL D 通道。

**预期结果**（请求方向）：

| TL-UL A 通道 (`PutFull/PutPartial`) | → | AXI 写地址 (AW) / 写数据 (W) |
|---|---|---|
| `A.address` | → | `AW.addr` |
| `A.source` | → | `AW.id` |
| `A.size` | → | `AW.size` |
| —（固定） | → | `AW.len = 0`（单拍） |
| —（固定） | → | `AW.burst = INCR` |
| `A.data` | → | `W.data` |
| `A.mask` | → | `W.strb` |
| —（固定） | → | `W.last = 1` |

（响应方向）：

| AXI 写响应 (B) | → | TL-UL D 通道 (`AccessAck`) |
|---|---|---|
| `B.id` | → | `D.source` |
| `B.resp != 0` | → | `D.error` |
| —（固定） | → | `D.opcode = AccessAck(0)` |

**待本地验证**：连续发 4 个地址相邻的 `Put`（每个 32 字节），观察 AXI AW 通道是否出现 4 次 `len=0` 的独立事务（而不是一次 `len=3` 的突发）——这正是「出方向不聚合」的表现。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `TLUL2Axi` 把 `len` 恒置 0，而不是像 `Axi2TLUL` 那样处理突发？
**参考答案**：TL-UL 的每拍 A 通道请求在协议层就是「自包含的单拍访问」，桥接器无法预知后续若干拍是否属于同一突发、也无法保证它们连续到达。最稳妥的做法是每拍都独立发一次 AXI 单拍事务（`len=0`）。代价是 AXI 侧效率略低（没有突发），但换来正确性与简单性。

**练习 2**：什么情况下 `TLUL2Axi` 会实例化 `TlulIdRemapper`？它解决什么问题？
**参考答案**：当 TL-UL 的 source 位宽大于 AXI 的 id 位宽时（`idWidthMismatch`）。它把较宽的 source 截断成较窄的 AXI id 发出，并用一张表记录映射、在响应时还原原 source，同时用 `outstanding` 位图防止两个不同 source 截断后撞到同一个 AXI id。

---

## 5. 综合实践：跑通两个桥接器的 cocotb 测试，并对照核内/SoC 接线

CoralNPU 为这两个桥接器各准备了一套 cocotb 测试，把上面的字段映射表**直接断言**成了代码。本实践把它们跑起来，并把源码接线串成一条完整链路。

### 5.1 跑桥接器单元测试（可运行）

测试定义在 [tests/cocotb/tlul/BUILD](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/BUILD)：

- `tlul2axi_cocotb_test`（DUT = `TLUL2Axi`）：`test_put_request`、`test_get_request`、`test_backpressure`、`test_put_then_get`。
- `axi2tlul_cocotb_test`（DUT = `Axi2TLUL`）：`test_write_request`、`test_read_request`、`test_read_error`。

**操作步骤**：

1. 跑 `TLUL2Axi` 的 Put 测试（验证 4.3 的映射表）：
   ```bash
   bazel test //tests/cocotb/tlul:tlul2axi_cocotb_test --test_filter=test_put_request
   ```
2. 跑 `Axi2TLUL` 的读测试（验证 4.2 的映射表）：
   ```bash
   bazel test //tests/cocotb/tlul:axi2tlul_cocotb_test --test_filter=test_read_request
   ```

**需要观察的现象**：打开测试源码 [tlul2axi_cocotb_test.py:130-134](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/tlul2axi_cocotb_test.py#L130-L134)，你会看到它发的 TL-UL `Put` 之后，立刻断言 AXI 侧 `AW.addr/AW.id/AW.size/W.data/W.strb` 与输入逐字段相等——这正是 4.3.4 那张表的代码化。同样 [axi2tlul_cocotb_test.py:251-257](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/axi2tlul_cocotb_test.py#L251-L257) 断言 AXI 读被翻译成了 TL-UL `Get`。

**预期结果**：两条 `bazel test` 全部 `PASSED`（若环境无 Verilator/VCS，则标记为「待本地验证」，转而做下面的源码阅读型实践）。

> 拓展：测试文件里还有 `test_write_burst`/`test_read_burst`（[axi2tlul_cocotb_test.py:370-547](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/axi2tlul_cocotb_test.py#L370-L547)），可用来验证「AXI 突发被展开成多拍 TL-UL」——注意 `BUILD` 默认 testcase 列表里未列它们，需要自行 `--test_filter` 指定。

### 5.2 源码阅读型实践：把两个桥接器放回真实接线（必做）

即使不跑仿真，也请完成这条「接线追踪」，它把本讲和 [u3-l1](u3-l1-soc-subsystem.md)/[u3-l2](u3-l2-axi-integration.md) 串起来：

1. **核内用法**：读 [CoreTlul.scala:45-69](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreTlul.scala#L45-L69)。你会看到：`hostBridge = Axi2TLUL` 把核的 AXI master 转成 TL-UL host（`io.tl_host`），`deviceBridge = TLUL2Axi` 把外部 TL-UL device 请求转成核的 AXI slave。也就是说，**`CoreAxi` 这个「说 AXI」的核被一对桥接器包装成了「说 TL-UL」的 host+device**，从而能融进内部 TL-UL fabric。
2. **SoC 顶层用法**：读 [CoralNPUChiselSubsystem.scala:298](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala#L298)（`ddr_ctrl_axi_conv = TLUL2Axi`）、[第 327 行](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala#L327)（`ddr_mem_axi_conv = TLUL2Axi`）、[第 355 行](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala#L355)（ISP 用 `Axi2TLUL`）。

**产出**：画一张图，标出三处边界——
- 核内：`CoreAxi.axi_master ──Axi2TLUL──▶ TL-UL fabric`；
- DDR：`TL-UL fabric ──(宽度桥)──TLUL2Axi──▶ io.ddr_mem_axi`；
- ISP：`io.isp_axi ──Axi2TLUL──▶ TL-UL fabric`。

**结论**：**「内 TL-UL、外 AXI」**——内部用 TL-UL 是为了对接 OpenTitan 生态、享受其 integrity（ECC）/user（instr_type）等元数据位、并用更简单的两通道模型；对外用 AXI 是为了对接 DDR/ISP 这类工业标准 IP。两个桥接器就守在这条边界上做翻译。

---

## 6. 本讲小结

- CoralNPU 内部 fabric 用 **TL-UL**（只有 **A、D 两个通道**，请求 `Get/PutFull/PutPartial`、响应 `AccessAckData/AccessAck`），对外用 **AXI4**；二者在边界由 `Axi2TLUL`/`TLUL2Axi` 翻译。
- `TileLinkUL.scala` 定义了 TL-UL 的全部字段（默认 256 位数据、32 位地址、6 位 source、5 位 size），`Axi.scala` 定义了 AXI 侧 Bundle；两套「词汇表」是读桥接器的前提。
- `Axi2TLUL`（AXI→TL-UL）把 AXI 读/写翻译成 `Get`/`Put`，**会展开 AXI 突发**成多拍 TL-UL 请求，用按 id 的计数器跟踪 `last`，并用 `instr_type` 标注取指/数据。
- `TLUL2Axi`（TL-UL→AXI）把 `Put`/`Get` 翻译成 AW+W / AR，但**只发单拍**（`len=0`）；当 source 比 AXI id 宽时插入 `TlulIdRemapper` 收窄并还原 ID。
- 关键映射：`source↔id`、`mask↔strb`、`size↔size`、`error↔resp`、`Get↔AR/R`、`Put↔AW/W/B`；这些已被 cocotb 测试逐字段断言。
- 核内 `CoreTlul` 用一对桥接器把 AXI 核包装成 TL-UL host+device；SoC 顶层 DDR 用 `TLUL2Axi` 出、ISP 用 `Axi2TLUL` 进。

---

## 7. 下一步学习建议

- **[u3-l4 总线互联 Crossbar 与 Socket](u3-l4-crossbar-fabric.md)**：本讲的桥接器产出/消费 TL-UL，下一步看 `CoralNPUXbar`/`TlulSocket1N`/`TlulSocketM1` 如何把这些 TL-UL host/device 路由与仲裁到一起。
- **[u9-l3 总线完整性与 SECDED](u9-l3-secded-integrity.md)**：本讲多次提到 TL-UL 的 `user`（`cmd_intg/data_intg`）字段，那是 OpenTitan 的 SECDED 完整性编码，后续会专门讲 `TlulIntegrity` 如何生成与校验。
- 若想看更底层的握手细节，可读 `common/CoralNPUArbiter.scala` 里的 `CoralNPURRArbiter`（本讲两个桥接器都用它做读/写或读响应/写响应的仲裁）。
