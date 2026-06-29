# AXI4 协议快速回顾

## 1. 本讲目标

本讲从「本库怎么用 SystemVerilog 表达 AXI4」这一视角，帮你重新建立对 AXI4 协议的直觉，为后续阅读 `axi_demux`、`axi_mux`、`axi_xbar` 等模块打协议地基。读完本讲，你应该能够：

- 说出 AXI4 的五个通道（AW/W/B/AR/R）各自承运什么信息、数据流方向是什么；
- 用源码里的精确定义复述 valid/ready 握手、in flight（在途）、pending（挂起）三个术语；
- 区分三种突发类型 `BURST_FIXED/INCR/WRAP` 与四种响应码 `RESP_OKAY/EXOKAY/SLVERR/DECERR`，并知道它们在 `axi_pkg.sv` 里的取值。

本讲**不**教你从零学完一整本 AMBA 规范，而是把后续阅读源码最常碰到的协议点抽出来，配合本库源码一一对应。协议权威依据是本库文档里点名的 *AMBA AXI and ACE Protocol Specification, Issue F.b*（本库简称 *AXI Spec*）。

## 2. 前置知识

- 你已经读过 [u1-l1 项目定位与设计哲学](u1-l1-project-overview.md)，知道本库是「积木式」AXI IP 库，知道 Master/Slave、五通道握手、in flight/pending、AXI4+ATOPs 这些词的大致含义。
- 你能看懂最基本的 SystemVerilog：`logic` 信号、`typedef`、`localparam`、`interface`/`modport`。不要求会写，只要能读懂。
- 一点点数字电路时序直觉：时钟上升沿、组合路径、寄存器。本讲会用简单的时序波形图说话。

几个名词先对齐：

- **事务（transaction）**：一次完整的 AXI 操作。一次写事务 = 1 个 AW 拍 + 若干个 W 拍 + 1 个 B 拍；一次读事务 = 1 个 AR 拍 + 若干个 R 拍。
- **拍（beat）**：一个通道上的一次 valid/ready 同时为高的传输，对应一个时钟周期里的一份数据。
- **Master / Slave**：发起事务的一方叫 Master（主），响应事务的一方叫 Slave（从）。

## 3. 本讲源码地图

本讲涉及的文件很少，但都是全库的「协议字典」：

| 文件 | 作用 |
|------|------|
| [src/axi_pkg.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv) | 全库共享的 `package axi_pkg`：所有宽度常量、typedef、`BURST_*`/`RESP_*`/`CACHE_*` localparam、地址计算函数。是协议在本库里的「权威取值表」。 |
| [src/axi_intf.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv) | 定义 `AXI_BUS` 接口，把五通道的所有信号列在一起，并用 Master/Slave/Monitor 三个 modport 区分方向。是「五个通道长什么样」的最直观样本。 |
| [doc/README.md](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/README.md) | 本库自己对 handshake / in flight / pending 的精确定义，以及遵循的规范版本。 |
| [README.md](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/README.md) | 顶层说明，点出 AXI4+ATOPs 的含义和 ATOP 的 ID 唯一性约束。 |

> 提示：本库里凡是用到突发类型、响应码、地址计算的地方，几乎都是 `import axi_pkg::*` 之后直接引用 `BURST_INCR`、`RESP_DECERR` 这些名字。所以把 `axi_pkg.sv` 的前一百多行读通，等于拿到了全库的协议词汇表。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- 4.1 五个通道与数据流方向
- 4.2 valid/ready 握手与 in flight / pending
- 4.3 突发类型 BURST_* 与地址计算
- 4.4 响应码 RESP_* 与响应优先级

### 4.1 五个通道与数据流方向

#### 4.1.1 概念说明

AXI4 把一次事务拆成**五个独立通道**，每个通道有自己的 valid/ready 握手、自己的数据线，彼此之间不共享时钟边沿以外的耦合。这五个通道是：

| 通道 | 全称 | 承运内容 | 方向 |
|------|------|----------|------|
| **AW** | Address Write | 写地址 + 写控制信息（长度、大小、突发类型、ID 等） | Master → Slave |
| **W** | Write Data | 写数据 + 字节使能（wstrb）+ 末拍标志（w_last） | Master → Slave |
| **B** | Write Response | 写响应（响应码 + ID） | Slave → Master |
| **AR** | Address Read | 读地址 + 读控制信息 | Master → Slave |
| **W** 的读对应物是 **R** | Read Data | 读数据 + 响应码 + 末拍标志（r_last） | Slave → Master |

一个关键直觉：**写事务用 AW + W + B 三条通道，读事务用 AR + R 两条通道**。也就是说，对一个纯写操作，AR/R 通道是空闲的；对一个纯读操作，AW/W/B 通道是空闲的。后续做综合实践画 4 拍 INCR 写的时序时，你会看到 AR/R 整段都是 0。

为什么要拆成五通道而不是像 APB 那样一条总线？因为五通道彼此独立握手，可以让「地址」先于「数据」发出（outstanding，在途），也可以让多个事务的地址和数据交错，从而大幅提高带宽。这是 AXI 高性能的根本来源。

#### 4.1.2 核心流程

一次写事务在通道上的顺序：

```
Master                              Slave
  |--- AW (地址+控制) --------------->|
  |--- W  (数据, 可多拍) ------------>|
  |<-- B  (写响应) -------------------|
```

一次读事务：

```
Master                              Slave
  |--- AR (地址+控制) --------------->|
  |<-- R  (数据+响应, 可多拍) --------|
```

注意方向：AW/W/AR 由 Master 发出（Master 端是 `output` valid），B/R 由 Slave 发出。每个通道各自独立握手，AW 和 W 之间没有强制的时序先后（地址可以晚于第一拍数据到达，只要最终能配对），这是 AXI 与简单总线的一个明显区别。

#### 4.1.3 源码精读

五个通道的信号集合，最直观的样本就是 `AXI_BUS` 接口的信号声明。以 AW 通道为例：

[src/axi_intf.sv:35-48](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv#L35-L48) —— 这里列出了 AW 通道的全部信号：`aw_id/aw_addr/aw_len/aw_size/aw_burst/aw_lock/aw_cache/aw_prot/aw_qos/aw_region/aw_atop/aw_user` 加上 `aw_valid/aw_ready`。注意 `aw_len`、`aw_size`、`aw_burst` 等类型直接来自 `axi_pkg`（如 `axi_pkg::len_t`、`axi_pkg::burst_t`）。

[src/axi_intf.sv:85-99](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv#L85-L99) —— `Master` 和 `Slave` 两个 modport。看 `Master` modport：`output aw_..., aw_valid, input aw_ready`，而 `Slave` modport 正好相反 `input aw_..., aw_valid, output aw_ready`。B/R 通道方向对调：B 通道在 Master 端是 `input b_..., b_valid, output b_ready`。**modport 的 input/output 列表就是「数据流方向」的可执行定义**。

如果想量化「每个通道到底有多宽」，`axi_pkg` 还提供了对应的宽度计算函数：

[src/axi_pkg.sv:321-356](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L321-L356) —— `aw_width`/`w_width`/`b_width`/`ar_width`/`r_width` 五个函数，把各通道所有信号位宽求和。例如 `w_width` 是 `data_width + data_width/8 + 1 + user_width`，其中 `data_width/8` 是 strobe（字节使能）宽度，`+1` 是 `w_last`。这从侧面印证了每个通道包含哪些字段。

#### 4.1.4 代码实践

**实践目标**：亲手验证「写事务只用 AW/W/B、读事务只用 AR/R」。

**操作步骤**（源码阅读型）：

1. 打开 [src/axi_intf.sv:50-83](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv#L50-L83)。
2. 把 W、B、AR、R 四段信号的信号名抄下来，分两列：一列「写相关通道（AW/W/B）」，一列「读相关通道（AR/R）」。
3. 找出只在写侧出现的信号（如 `w_strb`、`w_last`、`b_resp`）和只在读侧出现的信号（如 `r_last`、`r_data`）。

**需要观察的现象**：B 通道只有响应码 `b_resp` 和 ID，没有数据；R 通道既有数据 `r_data` 又有响应码 `r_resp`，因为读数据每一拍都可能带响应。

**预期结果**：你会得到一张清晰的「写三通道 / 读两通道」对照表，这正是后面画写事务时序图的依据。

#### 4.1.5 小练习与答案

**练习 1**：B 通道和 R 通道都带 `resp`，为什么 B 通道没有 `data` 信号？

> **参考答案**：B 是写响应，写操作的数据已经由 W 通道发出去了，响应只需要告诉 Master「这次写成功还是失败」，所以只有 `b_resp`（加 ID）。R 是读响应，Slave 必须把读出来的数据连同每拍的响应一起送回，所以既有 `r_data` 又有 `r_resp`。

**练习 2**：在 `Master` modport 里，`aw_valid` 是 `output` 还是 `input`？`b_valid` 呢？

> **参考答案**：`aw_valid` 是 `output`（地址由 Master 发出），`b_valid` 是 `input`（写响应由 Slave 发出）。这正是「方向」在源码里的体现，见 [src/axi_intf.sv:85-91](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv#L85-L91)。

---

### 4.2 valid/ready 握手与 in flight / pending

#### 4.2.1 概念说明

AXI 的每个通道都是一对握手信号：发送方拉 **valid**，接收方拉 **ready**，**同一个时钟上升沿上 valid 和 ready 同时为高**，这次传输（一拍）才算完成。这是 AXI 一切时序的根基。

围绕这个握手，本库文档精确定义了三个术语，后面读 `axi_demux`、`axi_xbar` 时会反复出现：

- **Handshake（握手）**：某通道上 valid 与 ready 在某个时钟沿同时为高。
- **In flight（在途）**：一个事务的 `Ax` 拍（AW 或 AR 地址拍）已经握手，但它的（最后一拍）响应还没握手，这个事务就处于 in flight。它衡量「同时有多少个未完成事务」，是 outstanding（并发）的同义词。
- **Pending（挂起）**：某通道上 valid 已经拉高、但 ready 还是低，这一拍正「卡着等对方」。pending 描述的是单拍的等待状态。

区分 in flight 和 pending 很重要：in flight 是**事务级**概念（地址发了、响应没回），pending 是**通道拍级**概念（一拍数据等握手）。`axi_demux` 内部的 id_counters（后续讲义会讲）就是用来跟踪 in flight 事务数的。

#### 4.2.2 核心流程

握手的关键规则（AXI Spec A3.2.1）：

1. **valid 一旦拉高，在握手完成前不能撤**：发送方不能因为 ready 没来就把 valid 拉低，否则接收方可能漏接。
2. **ready 可以等 valid 再决定**：接收方允许在看到 valid 之后才决定要不要拉 ready（组合依赖），也允许提前拉 ready。
3. **握手 = 同一沿 valid & ready**：二者在同一个上升沿同时为高，数据才算被取走，下一拍发送方可以更新数据。

握手前后有四种典型组合：

| valid | ready | 状态 |
|-------|-------|------|
| 0 | 任意 | 空闲，无传输 |
| 1 | 0 | **pending**（挂起），等待 |
| 1 | 1 | **handshake**（握手），本拍完成 |

in flight 的生命周期（写事务）：

```
AW 握手 ────────────── 事务 in flight 开始
   ...（W 拍陆续传，可能多个时钟周期）...
B  握手 ────────────── 事务 in flight 结束
```

从 AW 握手到 B 握手之间，这个事务都算 in flight。如果这期间 Master 又发了别的事务，就会有多个 in flight 事务并存。

#### 4.2.3 源码精读

三个术语的权威定义就在本库文档里，一字不差：

[doc/README.md:22-32](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/README.md#L22-L32) —— 逐字定义了 Handshake、In Flight、Pending。注意 In Flight 的措辞是「`Ax` beat 已握手但（最后一拍）响应未握手」，Pending 的措辞是「valid 高、ready 低」。这正是你后续读源码注释时遇到的 in flight / pending 的确切含义。

握手信号本身长什么样，看 `AXI_BUS`：每个通道都是一组 `<ch>_*` 数据信号配 `valid`/`ready` 一对。例如 B 通道：

[src/axi_intf.sv:57-61](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv#L57-L61) —— `b_id`、`b_resp`、`b_user` 配 `b_valid`、`b_ready`。这一对 valid/ready 就是这条通道的握手。

> 后续 hook：`axi_pkg` 里 `xbar_cfg_t` 的 `MaxMstTrans` / `MaxSlvTrans` 字段（[src/axi_pkg.sv:491-494](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L491-L494)）就是「每个端口最多允许多少个 in flight 事务」，这是 in flight 概念在配置结构体里的直接落地。

#### 4.2.4 代码实践

**实践目标**：在波形/纸面上识别 handshake 与 pending。

**操作步骤**（纸面时序分析）：

1. 在纸上画一段假想时序：一个通道的 `clk`、`valid`、`ready` 三行。
2. 设定 `valid` 在第 2 拍拉高，`ready` 在第 2 拍为 0、第 3 拍为 0、第 4 拍为 1。
3. 标出哪一拍处于 pending，哪一拍发生 handshake。

**需要观察的现象**：第 2、3 拍 valid=1 且 ready=0 → pending；第 4 拍 valid & ready 同高 → handshake。

**预期结果**：你能用本库定义指出「第 2、3 拍是 pending，第 4 拍握手完成」，并能解释为什么发送方在第 2~4 拍都不能撤掉 valid。若在真实仿真器（如 vsim，见 u1-l4）里观察任意一个 AXI 通道波形，对应关系完全一致。

#### 4.2.5 小练习与答案

**练习 1**：一个读事务从什么时刻开始算 in flight，到什么时刻结束？

> **参考答案**：从 AR 通道的地址拍握手开始，到 R 通道的最后一拍（`r_last` 的那一拍）握手结束。依据见 [doc/README.md:26-28](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/README.md#L26-L28)。

**练习 2**：某通道 valid=1、ready=0 持续了 5 拍，第 6 拍 ready 才变 1。这 6 拍里有几拍是 pending？几次 handshake？

> **参考答案**：第 1~5 拍都是 pending（valid 高、ready 低），共 5 拍 pending；第 6 拍发生 1 次 handshake。注意握手只算「同时为高」的那一拍。

---

### 4.3 突发类型 BURST_* 与地址计算

#### 4.3.1 概念说明

一次突发（burst）= 一个地址拍 + 若干个数据拍。地址拍里的 `aw_burst` / `ar_burst` 字段告诉 Slave「后续数据拍的地址怎么演进」。AXI4 定义了三种突发类型，本库在 `axi_pkg.sv` 里给了精确取值：

| 取值 | 名字 | 地址演进 | 典型用途 |
|------|------|----------|----------|
| `2'b00` | `BURST_FIXED` | 每拍地址不变 | 反复访问同一位置，如 FIFO 口 |
| `2'b01` | `BURST_INCR` | 每拍地址 += 每拍字节数 | 顺序访问普通内存（最常见） |
| `2'b10` | `BURST_WRAP`  | 像 INCR，但越过上界后回卷到下界 | cache 行填充 |

几个配套字段（都在地址拍里）：

- `aw_len` / `ar_len`：突发长度，**8 位宽**，值 = 拍数 − 1。所以一次突发最多 256 拍（len=255）。
- `aw_size` / `ar_size`：每拍字节数的 log2，**3 位宽**。`size=0` → 1 字节/拍，`size=2` → 4 字节/拍，`size=3` → 8 字节/拍，依此类推。

`num_bytes(size) = 2^size`，这是本库 `axi_pkg::num_bytes` 函数的语义。

#### 4.3.2 核心流程

INCR 突发的地址演进（最常用）：第 N 拍（N 从 0 计）地址为

\[
\text{Address}_N = \text{AlignedAddr} + N \times \text{NumberBytes}
\]

其中 \(\text{NumberBytes} = 2^{\text{size}}\)，\(\text{AlignedAddr}\) 是起始地址按 `size` 对齐后的值。例如 size=2（4 字节）、起始地址 0x0000、len=3（4 拍），则四拍地址依次为 0x0000、0x0004、0x0008、0x000C。

WRAP 突发多了一条约束：**长度必须是 2、4、8 或 16 拍**（即 len ∈ {1,3,7,15}）。当地址递增越过「回卷边界」时，地址绕回下界。这正是 cache 行填充的语义——它保证一次突发不会跨出某个对齐块。

FIXED 突发最简单：所有拍地址相同。

`axi_pkg` 把这三种地址计算都写成了函数（`aligned_addr`、`beat_addr`、`wrap_boundary`），下游模块（如 `axi_dw_converter`、`axi_to_mem`）直接调用它们来重算地址。

#### 4.3.3 源码精读

三个突发类型的取值定义：

[src/axi_pkg.sv:68-87](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L68-L87) —— `BURST_FIXED = 2'b00`、`BURST_INCR = 2'b01`、`BURST_WRAP = 2'b10`。注意注释里还说明了 WRAP 的两条限制（起始地址要按 size 对齐、长度只能是 2/4/8/16）。

配套的位宽与类型：

[src/axi_pkg.sv:24-39](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L24-L39) —— `BurstWidth=2`、`LenWidth=8`、`SizeWidth=3`，对应 `burst_t`、`len_t`、`size_t`。`len_t` 是 8 位所以最多 256 拍，`size_t` 是 3 位所以 size ∈ 0..7。

地址计算函数：

[src/axi_pkg.sv:116-118](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L116-L118) —— `num_bytes(size) = 1 << size`，即每拍字节数。

[src/axi_pkg.sv:126-128](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L126-L128) —— `aligned_addr`，把地址按 size 对齐。

[src/axi_pkg.sv:165-196](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L165-L196) —— `beat_addr`，计算第 i 拍地址。核心就是上面的 INCR 公式（第 179 行），并在 `BURST_WRAP` 且越过边界时回卷（第 191-193 行）。这是本库里「突发地址演进」的可执行定义。

#### 4.3.4 代码实践

**实践目标**：用一个最小调用验证 `beat_addr` 对 INCR 的计算。

**操作步骤**（源码阅读 + 心算）：

1. 假设起始地址 `addr = 64'h0000`、`size = 2`（4 字节）、`len = 3`（4 拍）、`burst = BURST_INCR`。
2. 用 `num_bytes(2) = 4` 和 `aligned_addr(0x0000, 2) = 0x0000`。
3. 对 `i_beat = 0,1,2,3` 分别套用 `beat_addr` 第 179 行的公式：`aligned + i_beat * num_bytes`。

**需要观察的现象**：四拍地址应当是 0x0000、0x0004、0x0008、0x000C。

**预期结果**：地址每拍递增 4，等差数列。如果你想跑真值，可以在一个最小 testbench 里 `import axi_pkg::*` 后 `$display(beat_addr(...))` 打印，但因为这需要搭仿真环境，这里标注「待本地验证」——心算结果已足以确认理解。

#### 4.3.5 小练习与答案

**练习 1**：`BURST_WRAP` 为什么不允许长度为 3 拍或 5 拍？

> **参考答案**：WRAP 的回卷语义要求突发恰好填满一个对齐块，回卷边界 = `NumberBytes × BurstLength`。只有长度为 2 的幂（2/4/8/16）时，回卷边界才是 size 对齐的，地址才能干净地绕回。本库在 `wrap_boundary` 里还加了 `assume` 断言，见 [src/axi_pkg.sv:139-141](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L139-L141)，传非法长度会报 `$error`。

**练习 2**：`len=255` 的 INCR 突发一共有多少拍数据？

> **参考答案**：256 拍。因为 `len` = 拍数 − 1，且 `len_t` 是 8 位（最大 255），所以一次突发最多 256 拍。

---

### 4.4 响应码 RESP_* 与响应优先级

#### 4.4.1 概念说明

每次事务完成时，Slave 通过响应码告诉 Master 结果。AXI4 有四种响应码，本库定义在 `axi_pkg.sv`：

| 取值 | 名字 | 含义 |
|------|------|------|
| `2'b00` | `RESP_OKAY` | 普通成功；对独占访问也表示「独占失败」 |
| `2'b01` | `RESP_EXOKAY` | 独占访问成功（Exclusive OKay） |
| `2'b10` | `RESP_SLVERR` | 到达了 Slave，但 Slave 报错 |
| `2'b11` | `RESP_DECERR` | 译码错误，通常是互联组件发现「这个地址没有 Slave」 |

写响应在 B 通道（每事务一拍 `b_resp`），读响应在 R 通道（每拍数据都带一个 `r_resp`）。

直觉记忆：

- OKAY / EXOKAY 都是「成功」级别，区别只在独占访问；
- SLVERR 是「Slave 自己说不行」；
- DECERR 是「根本没找到 Slave」，典型场景是访问了互联里没映射的地址——本库 `axi_xbar` 在地址无法路由时就回 `RESP_DECERR`（后续讲义 u6-l2 会讲）。

#### 4.4.2 核心流程

响应码本身的语义是协议规定的，但「当多个响应需要合并成一个时按什么优先级」是**实现自定义**的。比如 `axi_xbar` 里某个事务被同时送到多个译码路径、或者错误从端和正常从端都可能回响应时，需要把两个响应码合并成一个。

本库 `axi_pkg` 专门提供了一个 `resp_precedence` 函数来统一这种合并，优先级是：

\[
\text{DECERR} > \text{SLVERR} > \text{OKAY} > \text{EXOKAY}
\]

也就是说：任何错误（DECERR/SLVERR）都压过成功（OKAY/EXOKAY）；两个错误之间 DECERR 更优先（因为 DECERR 发生得更早，连 Slave 都没找到）；两个成功之间 OKAY 压过 EXOKAY（因为 OKAY 表示独占访问其实没成功）。

#### 4.4.3 源码精读

四个响应码取值：

[src/axi_pkg.sv:89-100](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L89-L100) —— `RESP_OKAY=2'b00`、`RESP_EXOKAY=2'b01`、`RESP_SLVERR=2'b10`、`RESP_DECERR=2'b11`，每个都带详细注释。

响应合并的优先级函数：

[src/axi_pkg.sv:282-319](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L282-L319) —— `resp_precedence(resp_a, resp_b)`。第 282-291 行的注释把「为什么 DECERR>SLVERR>OKAY>EXOKAY」的理由讲得很清楚：EXOKAY 表示独占成功而 OKAY 表示没成功，所以合并时 OKAY 优先；DECERR 比 SLVERR 更早发生，所以 DECERR 优先。这是后续读 `axi_xbar` 错误处理时绕不开的函数。

响应码类型：

[src/axi_pkg.sv:49-50](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L49-L50) —— `resp_t` 是 2 位 logic，宽度由 `RespWidth=2`（[src/axi_pkg.sv:27](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L27)）决定。你在 `AXI_BUS` 里看到的 `b_resp`、`r_resp` 就是这个类型。

#### 4.4.4 代码实践

**实践目标**：理解 `resp_precedence` 的合并行为。

**操作步骤**（源码阅读 + 心算）：

1. 打开 [src/axi_pkg.sv:292-319](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L292-L319)。
2. 心算三组输入的返回值：
   - `resp_precedence(RESP_OKAY, RESP_DECERR)` → ?
   - `resp_precedence(RESP_SLVERR, RESP_DECERR)` → ?
   - `resp_precedence(RESP_OKAY, RESP_EXOKAY)` → ?

**需要观察的现象**：第一组返回 DECERR（DECERR 压过 OKAY）；第二组返回 DECERR（DECERR 压过 SLVERR）；第三组返回 OKAY（OKAY 压过 EXOKAY）。

**预期结果**：三组都符合优先级 DECERR > SLVERR > OKAY > EXOKAY。这告诉你：在 xbar 里，只要有一条路径回了 DECERR，最终合并响应就是 DECERR——这正是访问未映射地址时拿到 DECERR 的内部原因。真要在仿真里打印确认，可在 testbench 里调用该函数并 `$display`（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：访问一个互联里完全没有映射的地址，会收到哪种响应？由谁产生？

> **参考答案**：`RESP_DECERR`。在本库 `axi_xbar` 里，无法路由的事务会被送到一个默认的错误从端（`axi_err_slv`），由它回 `RESP_DECERR`。后续 u6-l2 会详细讲。

**练习 2**：为什么 `resp_precedence(RESP_OKAY, RESP_EXOKAY)` 返回 OKAY 而不是 EXOKAY？

> **参考答案**：EXOKAY 表示一次独占访问成功，OKAY 表示（在独占语境下）独占失败。当两个响应要合并时，OKAY 说明「至少有一路没让独占成功」，所以整体不能算成功，于是 OKAY 优先。理由见 [src/axi_pkg.sv:285-287](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L285-L287)。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个任务（对应本讲规格里的代码实践任务）：

**任务**：在 [src/axi_pkg.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv) 里找到 `BURST_*` 与 `RESP_*` 的 localparam 定义，然后画一张「4 拍 INCR 写事务在五个通道上」的时序草图。

**建议步骤**：

1. **查参数**：从源码确认 `BURST_INCR = 2'b01`（[L81](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L81)）。4 拍 → `len = 3`（拍数−1）。设 size=2（4 字节/拍，对应 32 位数据），则 `num_bytes(2)=4`。
2. **算地址**：用 4.3 的公式，四拍 W 数据对应的写地址依次为 0x0000、0x0004、0x0008、0x000C（AW 通道只发一次起始地址 0x0000 + len/size/burst 控制）。
3. **画波形**：画出 `clk`、AW、W、B、AR、R 共 6 组（clk + 五通道的 valid/ready），让 AW 握手 1 拍、W 握手 4 拍（第 4 拍 `w_last=1`）、B 握手 1 拍（`b_resp=RESP_OKAY`），AR/R 全程为 0。

下面是一个示例时序草图（示例代码 / 示意图，`▲` 表示该沿发生握手）：

```
cycle #:   0    1    2    3    4    5    6    7
clk     _─|─|─|─|─|─|─|─|─|─|─|─|─|─|─|─|─|─|─|─|─
aw_valid ____|─────1────────1|____0________________      aw_addr=0x0, len=3, size=2, burst=INCR
aw_ready ____|─────1────────1|____0________________      ▲ cycle1: AW 握手（事务 in flight 开始）
w_valid  ________|─────1─────1─────1─────1|____0___      w_data=D0,D1,D2,D3
w_last   ________|_______0___0_____0_____1|____0___      仅第4拍 w_last=1
w_ready  ________|─────1────1─────1─────1|____0___      ▲ cycle2~5: 四拍 W 依次握手
b_valid  ___________________________|────1────1|___      b_resp=RESP_OKAY(=2'b00)
b_ready  ___________________________|────1────1|___      ▲ cycle6: B 握手（事务 in flight 结束）
ar_valid ___________________________________________      0（写事务不用 AR）
r_valid  ___________________________________________      0（写事务不用 R）
```

**自检要点**（做完后对照）：

- AW 通道只握手 **1 次**（地址只发一次，length 信息在 `aw_len` 里）。
- W 通道握手 **4 次**，且只有最后一拍 `w_last=1`。
- B 通道握手 **1 次**，`b_resp` 用 `RESP_OKAY` 表示成功。
- 从 AW 握手到 B 握手之间，这个写事务处于 **in flight**。
- AR/R 通道整段为 0，印证「写事务只用 AW/W/B」。
- 如果你想验证草图正确，可在 u3 学到 `axi_test` 的 driver 后，用 `axi_driver` 发一次 len=3 的 INCR 写，对比仿真波形与你的草图（待本地验证）。

## 6. 本讲小结

- AXI4 有五个独立握手通道：**AW/W/B** 给写事务，**AR/R** 给读事务；AW/W/AR 由 Master 发，B/R 由 Slave 发，方向在 `AXI_BUS` 的 modport 里以 input/output 明确标注。
- **valid/ready 同时为高才算一次握手**；valid 一旦拉高在握手前不能撤。in flight 是事务级（地址发了、响应没回），pending 是拍级（valid 高、ready 低），二者定义见 `doc/README.md`。
- 三种突发类型 `BURST_FIXED/INCR/WRAP` 取值 `2'b00/01/10`；INCR 地址每拍加 `2^size`，WRAP 长度只能是 2/4/8/16 且会回卷；`len` = 拍数−1，最多 256 拍。
- 四种响应码 `RESP_OKAY/EXOKAY/SLVERR/DECERR` 取值 `2'b00/01/10/11`；本库用 `resp_precedence` 统一合并优先级为 DECERR > SLVERR > OKAY > EXOKAY。
- 以上所有取值和函数都集中在 `axi_pkg.sv`，它是全库的「协议权威表」，后续读任何模块都离不开它。
- 一次 4 拍 INCR 写只在 AW（1 拍）/W（4 拍，末拍 `w_last`）/B（1 拍）上活动，AR/R 全程空闲。

## 7. 下一步学习建议

- 下一讲 [u1-l4 如何编译、仿真与综合](u1-l4-compile-sim-synth.md) 会讲怎么用 `Makefile` 和 `scripts/` 把一个 testbench 真正跑起来，让你能亲眼看到本讲画的握手波形。
- 进入单元 2 后，[u2-l1 axi_pkg：类型与常量](u2-l1-axi-pkg-types.md) 会把本讲只扫了一眼的 `axi_pkg.sv` 完整精读（包括 `xbar_cfg_t`、`xbar_latency_e` 等配置结构体），建议把本讲对 `BURST_*`/`RESP_*` 的理解直接带过去。
- 想提前体会协议在模块里的作用，可以先翻一眼 [src/axi_err_slv.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_err_slv.sv)，看它如何固定回 `RESP_DECERR`/`RESP_SLVERR`，这是 4.4 节最直白的应用。
