# axi_from_mem / axi_lite_from_mem：让 SRAM-like 接口成为 AXI 发起方

## 1. 本讲目标

学完本讲，读者应该能够：

- 说清 `axi_lite_from_mem` 与 `axi_from_mem` 各自扮演什么角色：把一个极简的 **SRAM-like 请求/授予/响应（req/gnt/rvalid）协议**「反向适配」成下游的 AXI4-Lite / AXI4 事务，使任何只会说 SRAM 协议的模块（如 DMA 搬运前端、自定义加速器）都能直接发起 AXI 访问。
- 掌握它与上一讲 `axi_to_mem` 的**对称关系**：`to_mem` 把 AXI 拆成 SRAM，`from_mem` 把 SRAM 合成 AXI；两者共享同一套 req/gnt/rvalid 信号语义，只是方向对调。
- 读懂 `axi_lite_from_mem` 的两条核心通路：请求通路的「写 AW/W 解耦小状态机」，以及响应通路用一个 **1-bit 标志位 FIFO** 在「没有 ID 的 Lite 协议」上实现保序响应回收的技巧。
- 理解 `axi_from_mem` 为何只是一层薄包装——它是 `axi_lite_from_mem` + `axi_lite_to_axi` 的组合，是本库「组合优于配置」哲学的又一个活样本。
- 能够搭建一个「本地存储 → axi_from_mem → axi_sim_mem」的最小搬移拓扑并验证数据完整性。

## 2. 前置知识

在进入本讲前，读者应已了解（对应前置讲义）：

- **AXI4 / AXI4-Lite 五通道与握手**（u1-l3）：AW/W/B/AR/R 通道，valid/ready 同高才算一次握手。
- **typedef / assign 宏体系**（u2-l4）：`AXI_LITE_TYPEDEF_ALL`、`axi_req_t` / `axi_resp_t` 结构体。
- **axi_sim_mem 与 scoreboard**（u3-l2）：`axi_sim_mem` 是仿真专用的「无限忠实 AXI 从端存储」，按字节建表，是验证 AXI 主端的标准下游。
- **axi_to_mem 与 req/gnt/rvalid 协议**（u14-l1）：`axi_to_mem` 把 AXI 突发翻译成存储流，存储侧用 `mem_req_o` / `mem_gnt_i` / `mem_rvalid_i` / `mem_addr_o` / `mem_we_o` / `mem_wdata_o` / `mem_strb_o` / `mem_rdata_i` 这一组信号。

本讲会反复对照 `axi_to_mem`，所以请先回忆它的存储侧端口方向。下面用一个表格帮助快速回忆 **req/gnt/rvalid** 这套 SRAM-like 协议：

| 信号 | 方向（从存储控制器看） | 含义 |
|------|------------------------|------|
| `req` | 主→从 | 一次访问请求有效（含地址、写使能、写数据、字节使能） |
| `gnt` | 从→主 | 从端本周期接受了这个请求（请求方下一拍可以撤掉或发下一个） |
| `rvalid` | 从→主 | 响应有效（读返回 `rdata`；写也回一个 `rvalid` 表示完成） |
| `rdata` | 从→主 | 读响应数据（仅读请求有意义） |

> 关键提醒：在本库这套协议里，**`rvalid` 与 `req` 之间没有固定的拍数关系**。源码注释明确强调「响应延迟不固定、绝不是 1」。下游 AXI 系统有多深，`rvalid` 就可能等多久。因此不能用「请求后一拍必有响应」的朴素 SRAM 模型来对接。

## 3. 本讲源码地图

| 文件 | 编译层级 | 作用 |
|------|----------|------|
| [src/axi_lite_from_mem.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv) | Level 2 | **真正干活的核心模块**。把 SRAM-like 请求翻译成 AXI4-Lite 事务，并用一个 FIFO 在无 ID 的 Lite 协议上保序回收响应。 |
| [src/axi_from_mem.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_from_mem.sv) | Level 3 | **薄包装**。内部例化 `axi_lite_from_mem`（Level 2）+ `axi_lite_to_axi`（Level 2），把 Lite 升成完整 AXI4。 |
| [src/axi_lite_to_axi.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_axi.sv) | Level 2 | 被 `axi_from_mem` 复用的 Lite→AXI4 纯组合上变换器（详见 u13-l1）。 |
| [src/axi_sim_mem.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_sim_mem.sv) | simulation target | 仿真专用下游存储，本讲综合实践用它做搬移目的地。 |

> 关于编译层级：`axi_lite_from_mem` 只依赖 `axi_pkg`（Level 0）与外部 `common_cells` 的 `fifo_v3`，故为 Level 2；`axi_from_mem` 依赖 `axi_lite_from_mem` 与 `axi_lite_to_axi`（都属本包 Level 2），其包内最长依赖链为 2，再加 1 即 Level 3。这也从依赖图上印证了「`axi_from_mem` 是在 Lite 实现之上再叠一层」的组合关系。

## 4. 核心概念与源码讲解

### 4.1 axi_lite_from_mem：作为 SRAM 从端的 AXI-Lite 主端

#### 4.1.1 概念说明

很多自研 IP（DMA 引擎、视频裁切器、神经网络取指单元）内部已经有一套极简的存储访问接口——「给地址、给数据、要个回执」——让它们直接去说 AXI4 是又重又容易出错的（要管五通道、突发、ID、保序……）。`axi_lite_from_mem` 就是这座桥：**它面向上游表现得像一块 SRAM**，面向下游表现得像一个合法的 AXI4-Lite 主端。

它和上一讲的 `axi_to_mem` 是**严格对称**的镜像：

| | `axi_to_mem`（u14-l1） | `axi_lite_from_mem`（本讲） |
|---|---|---|
| AXI 侧角色 | **Slave**（收 AXI） | **Master**（发 AXI） |
| 存储侧角色 | **Master**（驱动 SRAM：`mem_req_o`/`mem_gnt_i`…） | **Slave**（被 SRAM 驱动：`mem_req_i`/`mem_gnt_o`…） |
| 用途 | 把 AXI 突发落到本地 SRAM | 让本地 SRAM-like 主端去访问 AXI 子系统 |
| 数据流向 | AXI → 本地存储 | 本地存储 → AXI |

把两者背靠背串联，就能在两个 AXI 域之间插一段自定义存储流水线；单独用 `from_mem`，则常作为「DMA 风格搬移前端」。

#### 4.1.2 核心流程

整个模块可以拆成「请求通路」和「响应通路」两半，对应 AXI-Lite 的两个方向：

```text
            ┌──────────── axi_lite_from_mem ────────────┐
 mem 侧     │                                            │   AXI-Lite 侧
 (被驱动)   │   请求通路: mem_req → AW/W/AR              │   (主动发起)
 req_i ─────►│                                            │────► aw_valid/w_valid/ar_valid
 we_i ──────►│   · 读:  ar_valid, gnt=ar_ready            │
            │   · 写:  AW/W 解耦小状态机, gnt=两者都握    │
            │                                            │
            │   响应通路: B/R → mem_rsp_valid             │
            │   · 1-bit FIFO 记录「写=1/读=0」请求顺序    │
 rdata_o ◄──│   · 队头标志决定本拍等 B 还是等 R           │◄──── b_valid/r_valid
 valid_o ◄──│   · 读数据直通, 错误码解码                  │
            └────────────────────────────────────────────┘
```

要点：

1. **每个 mem 请求 → 恰好一笔 AXI-Lite 单拍事务**。无论读写，长度恒为 1（Lite 本就没有突发），所以这是「逐字访问」的桥，不是「批量搬移」的桥（批量要靠上游连续发多个请求）。
2. **支持多笔在途（multiple outstanding）**，上限由参数 `MaxRequests` 决定——它正是内部响应 FIFO 的深度。
3. **响应延迟不定**：`mem_rsp_valid_o` 可能在 `mem_gnt_o` 之后很多拍才出现，取决于下游 AXI 系统。

#### 4.1.3 源码精读

模块声明与端口见 [src/axi_lite_from_mem.sv:23-91](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L23-L91)。其中 mem 侧从端口（[L60-77](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L60-L77)）：

```systemverilog
input  logic          mem_req_i,      // 请求有效
input  mem_addr_t     mem_addr_i,     // 字节地址
input  logic          mem_we_i,       // 0=读, 1=写
input  data_t         mem_wdata_i,    // 写数据
input  strb_t         mem_be_i,       // 字节使能（高有效）
output logic          mem_gnt_o,      // 本拍接受了请求
output logic          mem_rsp_valid_o,// 响应有效（每请求恰好一次）
output data_t         mem_rsp_rdata_o,// 读返回数据（仅读有效）
output logic          mem_rsp_error_o;// 命中 SLVERR/DECERR
```

注意地址注释（[L47-49](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L47-L49)）：`mem_addr_i` 是**字节地址**，会被 `axi_addr_t'(mem_addr_i)` 截断或零扩展到 `AxiAddrWidth`（见 [L104](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L104) 与 [L112](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L112)）。模块只搬运地址、不做对齐或译码。

模块顶部文档（[src/axi_lite_from_mem.sv:15-22](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L15-L22)）明确写了两条非常重要的使用约束，务必记牢：

> - 支持 read **and** write 的响应；
> - **响应延迟不固定，绝不是 1**，取决于下游 AXI4-Lite 存储系统。

#### 4.1.4 代码实践（源码阅读型）

**目标**：用源码回答「`from_mem` 和 `to_mem` 在存储侧端口上到底是不是镜像」。

**步骤**：

1. 打开 [src/axi_to_mem.sv:57-75](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_to_mem.sv#L57-L75)，列出 `mem_*` 信号的方向（`output` 还是 `input`）。
2. 打开 [src/axi_lite_from_mem.sv:60-86](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L60-L86)，列出同名信号的方向。
3. 把两组方向列成表对照。

**需要观察的现象**：同名信号（`mem_req`、`mem_gnt`、`mem_addr`、`mem_we`、`mem_wdata`、`mem_strb/be`、`mem_rvalid/rsp_valid`、`mem_rdata/rsp_rdata`）在两个模块里方向应当**逐根相反**——`to_mem` 是 `output` 的，`from_mem` 是 `input`，反之亦然。

**预期结果**：你得到一张完美镜像表，直观印证「两者是同一协议的正反两面」。唯一的小差异是命名（`mem_be_i` vs `mem_strb_o`、`mem_rsp_valid_o` vs `mem_rvalid_i`），但语义一一对应。本结论「待本地验证」：建议亲手画一遍这张表。

#### 4.1.5 小练习与答案

**练习 1**：`axi_lite_from_mem` 能否把一次 8 拍的 INCR 突发直接转发到下游？为什么？

**参考答案**：不能。它面向上游是逐字 SRAM 接口，每个 `mem_req_i` 只携带一个地址与一拍数据，内部也只产生一笔单拍 Lite 事务（Lite 协议本身无突发）。要搬 8 拍，上游必须连续发 8 个 `mem_req_i`，下游就会看到 8 笔独立的单拍 Lite 事务。

**练习 2**：模块注释为何要专门强调「响应延迟绝不是 1」？如果上游用一个「请求后第 1 拍就采 `rvalid`」的朴素 SRAM 模型来对接，会发生什么？

**参考答案**：因为下游是 AXI 系统，响应要等 B/R 通道握手，路径可能跨越多级互联与存储，延迟不定且通常远大于 1。朴素模型在第 1 拍采样会采到无效的 `rvalid`（多半是 0），从而漏掉真正的响应，或把无关数据当成读结果。正确做法是**用 `mem_rsp_valid_o` 作为同步信号**，它有效时才采样 `mem_rsp_rdata_o`。

---

### 4.2 请求通路：读单拍与写 AW/W 解耦

#### 4.2.1 概念说明

AXI4-Lite 的写事务有 **AW（写地址）** 和 **W（写数据）** 两条独立通道，各自有自己的 ready——它们可能在不同的周期握手。而上游 mem 侧的一次写请求是「原子的」：一个 `mem_req_i` 同时带着地址和数据，期望一次 `mem_gnt_o` 就完成。

因此请求通路需要一个**小状态机**来解耦 AW 与 W：只有当 AW 和 W 都被下游接收后，才认为这次写请求「发出完成」、才拉 `mem_gnt_o`、才往响应 FIFO 里记一笔。读请求则简单——只有 AR 一条通道，握手即授予。

这套逻辑全部写在一个 `always_comb` 块里，配合两个状态位 `aw_sent_q` / `w_sent_q`。

#### 4.2.2 核心流程

```text
mem_req_i && !fifo_full ?
├─ 否 → 不发任何 valid，mem_gnt_o=0
└─ 是
   ├─ !mem_we_i（读）
   │     ar_valid=1
   │     mem_gnt_o = ar_ready        // AR 握手即授予，并 push FIFO(0)
   │
   └─ mem_we_i（写）→ 看 {aw_sent_q, w_sent_q}:
         2'b00（两条都没发）
         │  aw_valid=1, w_valid=1
         │  ├─ {aw_ready,w_ready}=01 → W 先握: w_sent_d=1（下一拍进 2'b01）
         │  ├─               =10 → AW 先握: aw_sent_d=1（下一拍进 2'b10）
         │  └─               =11 → 两条同拍握: mem_gnt_o=1（直接完成）
         2'b10（AW 已发，剩 W）
         │  w_valid=1; w_ready 则 aw_sent_d=0, mem_gnt_o=1
         2'b01（W 已发，剩 AW）
         │  aw_valid=1; aw_ready 则 w_sent_d=0, mem_gnt_o=1
         default → 回 IDLE（failsafe）
```

注意 `mem_gnt_o` 只在「写请求的 AW 与 W 都已握手」那一刻拉高一拍——它同时充当响应 FIFO 的 push 信号，所以「一次写请求 = 一次 push」严格成立。

#### 4.2.3 源码精读

整个请求翻译逻辑在 [src/axi_lite_from_mem.sv:101-171](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L101-L171)。先看总闸门与读分支（[L122-126](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L122-L126)）：

```systemverilog
if (mem_req_i && !fifo_full) begin           // FIFO 满则停接受，反压上游
  if (!mem_we_i) begin                       // —— 读请求 ——
    axi_req_o.ar_valid = 1'b1;
    mem_gnt_o          = axi_rsp_i.ar_ready; // AR 握手即授予
  end else begin                             // —— 写请求 ——
    unique case ({aw_sent_q, w_sent_q}) ...
```

写分支的 `unique case` 在 [L129-168](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L129-L168)。以「两条都没发」的子状态为例（[L130-146](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L130-L146)）：

```systemverilog
2'b00 : begin
  axi_req_o.aw_valid = 1'b1;
  axi_req_o.w_valid  = 1'b1;
  unique case ({axi_rsp_i.aw_ready, axi_rsp_i.w_ready})
    2'b01 : w_sent_d  = 1'b1;   // 只 W 握了
    2'b10 : aw_sent_d = 1'b1;   // 只 AW 握了
    2'b11 : mem_gnt_o = 1'b1;   // 两条都握了 → 本次写请求授予完成
    default : /* do nothing */;
  endcase
end
```

两个状态位用 `FFARN`（异步复位寄存器，来自 `common_cells`）保存，见 [src/axi_lite_from_mem.sv:173-174](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L173-L174)：

```systemverilog
`FFARN(aw_sent_q, aw_sent_d, 1'b0, clk_i, rst_ni)
`FFARN(w_sent_q,  w_sent_d,  1'b0, clk_i, rst_ni)
```

载荷赋值在块开头一次写好（[L103-114](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L103-L114)）：AW/W/AR 的 `addr`、`prot`、`data`、`strb` 都直接来自 mem 输入，其余字段清零，valid 由状态机按需拉高。`AxiProt` 参数（[L33](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L33)）让上层统一指定保护位。

#### 4.2.4 代码实践（参数微调型）

**目标**：理解 `MaxRequests` 如何同时充当「在途上限」与「反压阈值」。

**步骤**：

1. 在 [src/axi_lite_from_mem.sv:179-195](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L179-L195) 找到 `fifo_v3` 的例化，确认 `.DEPTH(MaxRequests)`。
2. 回到 [L122](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L122) 看 `!fifo_full` 这个门。
3. 假设把 `MaxRequests` 从 2 改成 1，推演：上游连发两笔读请求、下游恰好长时间不回响应时，第二笔请求会怎样。

**需要观察的现象**：FIFO 深度 = `MaxRequests`，FIFO 满时 `fifo_full=1`，请求通路整体停摆，`mem_gnt_o` 保持 0。

**预期结果**：`MaxRequests` 决定了「在下游回响应之前，最多能先发出去几笔请求」。设为 1 退化为「严格一来一回」；设大则允许更多在途、提高吞吐，但面积更大。「待本地验证」：用仿真观察 `MaxRequests=1` 与 `=4` 下同一批请求的总耗时差异。

#### 4.2.5 小练习与答案

**练习 1**：为什么写请求要等 AW 和 W **都**握手才拉 `mem_gnt_o`，而不是 AW 握手就拉？

**参考答案**：因为 `mem_gnt_o` 同时是响应 FIFO 的 push 信号，语义是「这笔写请求已完整发出，下游欠我一个 B 响应」。只有 AW 与 W 都到了下游，下游才真正欠一个 B；只发 AW 就 push 会导致响应计数与实际待回收的 B 数对不上，FIFO 里读/写标志位的顺序就会错乱。

**练习 2**：状态位 `aw_sent_q`/`w_sent_q` 复位值是 0，对应的初始状态 `2'b00` 表示什么？什么情况下会进入 `2'b10`？

**参考答案**：`2'b00` 表示当前写请求的 AW 与 W 都尚未握手。当处于 `2'b00` 且本拍只有 `aw_ready` 有效（`w_ready` 无效），即 `{aw_ready,w_ready}=2'b10` 时，AW 被单独接收，`aw_sent_d=1`，下一拍状态变为 `2'b10`（AW 已发、剩 W 待发）。

---

### 4.3 响应通路：用 1-bit FIFO 实现「无 ID」保序

#### 4.3.1 概念说明

AXI4-Lite **没有 ID**。当多笔读/写请求在途时，B 通道（写响应）和 R 通道（读响应）会交错返回，但响应里没有任何标识告诉你「这一拍对应哪一笔请求」。那 `axi_lite_from_mem` 怎么把交错回来的 B/R 流还原成与请求**同序**的单一 `mem_rsp_valid_o` 流？

它的办法极其巧妙：**用一个深度为 `MaxRequests`、宽度仅 1-bit 的 FIFO，按请求授予的顺序记录「这笔是写（1）还是读（0）」**。队头（FIFO 输出）就告诉你「下一拍该等的是 B 还是 R」。这样无需 ID，仅靠请求顺序就完成了响应的多路分解（demultiplex）。

这套机制成立的前提是：**下游按请求顺序返回响应**。对于 AXI-Lite，由于没有 ID、且 Lite 子系统通常单笔串行处理，这个前提在工程上基本成立（也是本模块的设计假设）。

#### 4.3.2 核心流程

```text
请求授予时(mem_gnt_o↑)：把 mem_we_i(1=写/0=读) push 进 rsp FIFO
                                  │
                                  ▼
            ┌── rsp FIFO (深度=MaxRequests, 宽=1bit) ──┐
            │  队头 rsp_sel = 1 → 下一响应应是写(B)     │
            │  队头 rsp_sel = 0 → 下一响应应是读(R)     │
            └──────────────────────────────────────────┘
                                  │
   rsp_sel ? b_ready=1 : r_ready=1   ← 只在「期待的那一类」上拉 ready
                                  │
   b_valid&&b_ready 或 r_valid&&r_ready
            → mem_rsp_valid_o=1（同时是 FIFO 的 pop）
            → mem_rsp_error_o = 该通道 resp 是否为 SLVERR/DECERR
            → 读时 mem_rsp_rdata_o = r.data（直通）
```

因为 FIFO 严格按 push 顺序 pop，且每笔请求恰好产生一次 push 与一次 pop，响应顺序被强制对齐到请求顺序——这就是「保序」。

#### 4.3.3 源码精读

FIFO 例化见 [src/axi_lite_from_mem.sv:179-195](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L179-L195)：

```systemverilog
fifo_v3 #(
  .FALL_THROUGH ( 1'b0        ), // 非直通：保证 AXI 侧 ready 前有一拍缓冲
  .DEPTH        ( MaxRequests ),
  .dtype        ( logic       ) // 只存 1 bit：写=1/读=0
) i_fifo_rsp_mux (
  .full_o     ( fifo_full       ), // → 反压请求通路
  .empty_o    ( fifo_empty      ),
  .data_i     ( mem_we_i        ), // push 的是「这笔是不是写」
  .push_i     ( mem_gnt_o       ), // 每授予一笔请求 push 一次
  .data_o     ( rsp_sel         ), // 队头：下一响应应为 B(1) 还是 R(0)
  .pop_i      ( mem_rsp_valid_o )  // 每回一个响应 pop 一次
);
```

基于 `rsp_sel` 的 ready 选通与响应合成在 [src/axi_lite_from_mem.sv:199-211](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L199-L211)：

```systemverilog
// FIFO 非空时，按队头标志只在一类通道上拉 ready
assign axi_req_o.b_ready = !fifo_empty &&  rsp_sel;
assign axi_req_o.r_ready = !fifo_empty && !rsp_sel;
// 读数据直通（仅读响应时有意义）
assign mem_rsp_rdata_o = axi_rsp_i.r.data;
// 错误：取当前期待通道的 resp 是否为 SLVERR/DECERR
assign mem_rsp_error_o = rsp_sel ?
    (axi_rsp_i.b.resp inside {axi_pkg::RESP_SLVERR, axi_pkg::RESP_DECERR}) :
    (axi_rsp_i.r.resp inside {axi_pkg::RESP_SLVERR, axi_pkg::RESP_DECERR});
// 响应有效 = 当拍在期待通道上完成握手；同时充当 FIFO pop
assign mem_rsp_valid_o = (axi_rsp_i.b_valid && axi_req_o.b_ready) ||
                         (axi_rsp_i.r_valid && axi_req_o.r_ready);
```

注意 `FALL_THROUGH(1'b0)` 的用意（[L180](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L180)）：非直通模式让 `data_o`（`rsp_sel`）在 push 后下一拍才出现，避免了「同一拍既决定 ready 又采样响应」的组合环，保证 AXI 侧 ready 相对 valid 有一拍干净时序。

另外，模块还用一组 `assert property` 约束上游必须遵守 SRAM 协议契约：未授予前不得撤请求、地址/写使能/写数据/字节使能必须保持稳定，见 [src/axi_lite_from_mem.sv:240-249](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L240-L249)。这等价于「valid 在握手前不可撤」的 AXI 铁律被搬到了 mem 侧。

#### 4.3.4 代码实践（调用链追踪型）

**目标**：跟踪「一笔读请求」从 mem 侧到响应回收的完整数据通路，确认读数据是「直通」而非寄存。

**步骤**：

1. 从 [L122-126](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L122-L126) 看到：读请求 → `ar_valid` 拉高，握手时 push 一个 `0`（读标志）进 FIFO。
2. 从 [L199-200](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L199-L200) 看到：当队头 `rsp_sel=0` 时拉 `r_ready`。
3. 从 [L202](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L202) 看到：`mem_rsp_rdata_o = axi_rsp_i.r.data`，纯 assign、零寄存。

**需要观察的现象**：读返回数据没有任何中间寄存器，下游 `r.data` 一旦伴随 `r_valid` 出现并在本模块 `r_ready` 拉高时握手，同拍就直接出现在 `mem_rsp_rdata_o` 上，同时 `mem_rsp_valid_o` 拉高、FIFO pop。

**预期结果**：你画出一条无寄存的直通路径 `axi_rsp_i.r.data → mem_rsp_rdata_o`；这意味着本模块对读数据**不增加延迟**，延迟完全来自下游 AXI 系统。

#### 4.3.5 小练习与答案

**练习 1**：如果下游某次先返回了一个 B，但 FIFO 队头标志是 `0`（期待 R），会发生什么？

**参考答案**：因为 `rsp_sel=0` 时 `b_ready=0`（[L199](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L199)），本模块不会接收这个 B，下游的 `b_valid` 会一直挂着直到队头变成 `1`。这等价于本模块强制「响应必须按请求顺序回收」。若下游真的乱序返回，会造成死锁——这也正是模块假设「下游保序」的根源。

**练习 2**：把响应 FIFO 的 `data_i` 从 `mem_we_i` 改成常量 `1'b0` 会出现什么故障？

**参考答案**：FIFO 将永远认为下一响应是读（R），于是 `r_ready` 始终有效而 `b_ready` 恒为 0。所有写请求的 B 响应都无法被接收，写请求会卡死、FIFO 最终填满并反压整个通路。可见这 1-bit 标志承担了区分 B/R、决定 ready 方向的关键作用。

---

### 4.4 axi_from_mem：组合 axi_lite_from_mem + axi_lite_to_axi

#### 4.4.1 概念说明

`axi_from_mem` 要把 SRAM 协议适配成**完整 AXI4**（而非 Lite）。最直接的做法是从头写一套支持五通道的实现——但本库选择了「组合优于配置」：**已有的 `axi_lite_from_mem` 已经解决了全部难题（AW/W 解耦、无 ID 保序、多笔在途），只要把它输出的 Lite 再用 `axi_lite_to_axi` 升成完整 AXI4 即可**。

`axi_lite_to_axi`（u13-l1）是一个**纯组合、无损**的上变换器：Lite 是 AXI4 的严格子集，补上默认值（`id=0`、`len=0`、`burst=FIXED`、`size=$clog2(数据宽度/8)`、`w.last=1`）即可。于是 `axi_from_mem` 内部一句话也不写逻辑，只做两件事：例化 `axi_lite_from_mem`，再例化 `axi_lite_to_axi`。

#### 4.4.2 核心流程

```text
          ┌─── axi_from_mem (Level 3) ──────────────────────────┐
          │                                                     │
 mem 侧 ──┼──► i_axi_lite_from_mem ──► axi_lite_req ──►         │
          │   (Level 2, 干全部活)        (Lite struct)  \        │
          │                                              \       │
          │   ◄── axi_lite_rsp ◄────────                 \      │
          │                          \                   \      │
          │                           \                   ► i_axi_lite_to_axi ──► axi_req_o (完整 AXI4)
          │                            \                 /  ◄── axi_rsp_i
          │                             ►  (纯组合上变换) ►
          └─────────────────────────────────────────────────────┘
```

两块子模块之间用 Lite 结构体 `axi_lite_req` / `axi_lite_rsp` 直连，`axi_lite_to_axi` 再把它翻译成完整 AXI4 的 `axi_req_o` / `axi_rsp_i`。

#### 4.4.3 源码精读

整模块仅约 100 行，先在 [src/axi_from_mem.sv:81-83](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_from_mem.sv#L81-L83) 用 `AXI_LITE_TYPEDEF_ALL` 生成中间 Lite 类型：

```systemverilog
`AXI_LITE_TYPEDEF_ALL(axi_lite, logic [AxiAddrWidth-1:0],
                      logic [DataWidth-1:0], logic [DataWidth/8-1:0])
axi_lite_req_t  axi_lite_req;
axi_lite_resp_t axi_lite_rsp;
```

随后例化干全部活的 `axi_lite_from_mem`（[src/axi_from_mem.sv:85-107](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_from_mem.sv#L85-L107)）：

```systemverilog
axi_lite_from_mem #(
  .MemAddrWidth ( MemAddrWidth ), .AxiAddrWidth ( AxiAddrWidth ),
  .DataWidth    ( DataWidth    ), .MaxRequests  ( MaxRequests  ),
  .AxiProt      ( AxiProt      ),
  .axi_req_t    ( axi_lite_req_t ), .axi_rsp_t ( axi_lite_resp_t )
) i_axi_lite_from_mem (
  .clk_i, .rst_ni,
  .mem_req_i, .mem_addr_i, .mem_we_i, .mem_wdata_i, .mem_be_i,  // 直通到 mem 端口
  .mem_gnt_o, .mem_rsp_valid_o, .mem_rsp_rdata_o, .mem_rsp_error_o,
  .axi_req_o ( axi_lite_req ),  .axi_rsp_i ( axi_lite_rsp )     // Lite struct 对内
);
```

最后例化纯组合上变换器 `axi_lite_to_axi`（[src/axi_from_mem.sv:109-122](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_from_mem.sv#L109-L122)）：

```systemverilog
axi_lite_to_axi #(
  .AxiDataWidth ( DataWidth ), .req_lite_t  ( axi_lite_req_t  ),
  .resp_lite_t  ( axi_lite_resp_t ),
  .axi_req_t    ( axi_req_t  ), .axi_resp_t ( axi_rsp_t )
) i_axi_lite_to_axi (
  .slv_req_lite_i ( axi_lite_req ), .slv_resp_lite_o ( axi_lite_rsp ),
  .slv_aw_cache_i,  .slv_ar_cache_i,   // cache 必须从外部喂入
  .mst_req_o ( axi_req_o ), .mst_resp_i ( axi_rsp_i )
);
```

注意 `slv_aw_cache_i` / `slv_ar_cache_i` 这两个 cache 输入（[src/axi_from_mem.sv:72-74](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_from_mem.sv#L72-L74)）：因为 AXI4-Lite 没有 cache 字段，cache 属性只能由上层通过 `axi_from_mem` 的端口从外部提供，再交给 `axi_lite_to_axi` 填进完整 AXI4 的 AW/AR（参见 [src/axi_lite_to_axi.sv:45](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_axi.sv#L45) 与 [L62](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_axi.sv#L62)）。

模块顶部文档（[src/axi_from_mem.sv:17-23](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_from_mem.sv#L17-L23)）同样强调「支持读写响应、延迟不固定、绝非 1」——这条性质从 `axi_lite_from_mem` 原样继承。

#### 4.4.4 代码实践（依赖关系阅读型）

**目标**：用源码印证「`axi_from_mem` 自己不写任何握手逻辑」。

**步骤**：

1. 通读 [src/axi_from_mem.sv:80-124](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_from_mem.sv#L80-L124) 全文。
2. 统计：模块体内有没有 `always_comb` / `always_ff` / `assign`（除了端口连线）？
3. 数一数它例化了几个子模块、各叫什么。

**需要观察的现象**：除两处例化与 typedef 外，模块体内**没有任何行为级逻辑**——没有 always 块、没有对 AXI 信号的 assign 运算。所有握手与状态都在 `i_axi_lite_from_mem` 内部。

**预期结果**：确认它只例化了 `axi_lite_from_mem` 与 `axi_lite_to_axi` 两个子模块，是纯粹的「胶水」。这正是它排在 Level 3、而真正干活的 Lite 版在 Level 2 的原因。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `axi_from_mem` 不直接支持 AXI5 原子操作（ATOP）？

**参考答案**：它的实现路径经过 `axi_lite_from_mem` → `axi_lite_to_axi`，而 AXI4-Lite 没有 `atop` 字段；`axi_lite_to_axi` 也只补默认零值（[src/axi_lite_to_axi.sv:39-68](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_axi.sv#L39-L68)），不会产生原子操作。要发起 ATOP，需要绕过这条 Lite 路径另写主端。

**练习 2**：既然 `axi_lite_to_axi` 是纯组合的，那么 `axi_from_mem` 相比 `axi_lite_from_mem` 增加了多少关键路径延迟？

**参考答案**：理论上只增加了 `axi_lite_to_axi` 那一级纯组合 mux/struct 拼装的延迟（一档组合逻辑），因为上变换只是字段重排与补默认值。握手时序、保序、在途管理完全相同，不额外增加状态或寄存器级数。

---

## 5. 综合实践

**任务**：用 `axi_from_mem` 把一段「本地源存储」的内容搬到下游 `axi_sim_mem`，再读回比对，验证数据完整到达且顺序正确。这是典型的 DMA 风格搬移前端用法。

> 说明：本库**没有**为 `from_mem` 提供现成测试台（仓库里不存在 `tb_axi_from_mem.sv`，相关文件检索可确认）。因此本实践为**自建最小测试台**，下方代码是「示例代码」，需要本地仿真器（vsim/verilator 等）运行，部分细节「待本地验证」。

### 5.1 拓扑

```text
   行为级「源 SRAM 读/写驱动进程」
              │ mem_req_i / mem_addr_i / mem_we_i / mem_wdata_i / mem_be_i
              ▼
        ┌──────────────┐  axi_req_o (完整 AXI4 struct)   ┌──────────────┐
        │ axi_from_mem │ ──────────────────────────────► │ axi_sim_mem  │
        │   (DUT)      │ ◄────────────────────────────── │ (下游存储)   │
        └──────────────┘  axi_rsp_i (完整 AXI4 struct)   └──────────────┘
              ▲ mem_gnt_o / mem_rsp_valid_o / mem_rsp_rdata_o / mem_rsp_error_o
              │
   驱动进程据此完成「先连写 N 个字 → 再连读 N 个字 → 逐字比对」
```

因为 `axi_from_mem` 的 AXI 侧与 `axi_sim_mem` 都使用 `axi_req_t` / `axi_rsp_t` 结构体端口，二者可直接 struct-to-struct 连线，无需接口外壳与 assign 宏（参见 [src/axi_sim_mem.sv:52-60](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_sim_mem.sv#L52-L60)）。

### 5.2 操作步骤

1. **声明类型**：用 `AXI_TYPEDEF_ALL` 生成完整 AXI4 的 `axi_req_t`/`axi_resp_t`（参考 u2-l4），位宽取 `AxiAddrWidth=32`、`DataWidth=32`、`IdWidth=4`（下游 sim_mem 需要 ID 宽度）。
2. **例化 DUT**：`axi_from_mem #(.MemAddrWidth(32), .AxiAddrWidth(32), .DataWidth(32), .MaxRequests(2), .AxiProt(3'b000), .axi_req_t(...), .axi_rsp_t(...))`。
3. **例化下游**：`axi_sim_mem #(.AddrWidth(32), .DataWidth(32), .IdWidth(4), .axi_req_t(...), .axi_rsp_t(...))`，把 DUT 的 `axi_req_o` 接到 sim_mem 的 `axi_req_i`，sim_mem 的 `axi_rsp_o` 接回 DUT 的 `axi_rsp_i`。
4. **写一个 mem 侧驱动 task**：参数为 `(addr, we, wdata)`，按 SRAM 协议契约驱动——拉高 `mem_req_i` 并保持地址/数据稳定，等到 `mem_gnt_o` 拉高后，再等 `mem_rsp_valid_o` 拉高才返回（满足 [src/axi_lite_from_mem.sv:240-249](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_from_mem.sv#L240-L249) 的稳定性与不可撤请求断言）。
5. **搬移阶段**：对 `i = 0..N-1`，调用 `do_mem(BASE + 4*i, we=1, wdata=source[i])`，把源数组写入下游。
6. **回读校验阶段**：对 `i = 0..N-1`，调用 `do_mem(BASE + 4*i, we=0, wdata=0)`，返回时在 `mem_rsp_valid_o` 有效的拍采样 `mem_rsp_rdata_o`，与 `source[i]` 比对，不等则 `errors++`。
7. **结束**：`$display("Errors: %0d", errors)`，以是否为 0 判定通过（与本库「日志判成败」一致，参见 u1-l4）。

### 5.3 驱动 task 示例代码

下列为**示例代码**（非仓库原有文件），仅示意 mem 侧驱动写法，省略时钟/复位与类型声明，需自行补全后「待本地验证」：

```systemverilog
// 示例代码：mem 侧 SRAM 协议驱动 task（行为级）
task automatic do_mem(input [31:0] addr, input we, input [31:0] wdata,
                      output [31:0] rdata);
  // 1. 施加请求并保持稳定（断言要求：未授予前请求与载荷不可变）
  mem_req_i   = 1'b1;
  mem_addr_i  = addr;
  mem_we_i    = we;
  mem_wdata_i = wdata;
  mem_be_i    = '1;
  // 2. 等到授予（写要等 AW/W 都握完；读等 AR 握完）
  while (!mem_gnt_o) @(posedge clk_i);
  // 3. 授予后即可撤请求；等响应（延迟不定，绝不要假设是 1 拍！）
  mem_req_i = 1'b0;
  while (!mem_rsp_valid_o) @(posedge clk_i);
  // 4. 响应有效拍采样读数据与错误
  rdata        = mem_rsp_rdata_o;
  error_flag   = mem_rsp_error_o;
endtask
```

### 5.4 需要观察的现象与预期结果

- **写阶段**：每个 `do_mem(...,we=1,...)` 都能先后看到 `mem_gnt_o` 与 `mem_rsp_valid_o` 各拉高一次（写也回响应）；`mem_rsp_error_o` 应为 0。
- **读阶段**：每个读 `do_mem` 返回的 `rdata` 应与当初写入的 `source[i]` **逐字相等**，且顺序与请求顺序一致。
- **吞吐观察**（进阶）：把 `MaxRequests` 从 1 调到 4，观察 N 个写请求的总周期数下降——这直观体现「多笔在途」的收益，对应 4.2.4 的推演。
- **判定**：若最终 `Errors: 0`，则数据完整、顺序正确，搬移成功。

> 若在 `MaxRequests>1` 下偶发错误，最先怀疑下游是否真的保序返回响应（见 4.3.5 练习 1）——`axi_sim_mem` 单端口是保序的，适合做本实践的下游。

## 6. 本讲小结

- `axi_lite_from_mem`（Level 2）是真正干活的模块：面向上游是 SRAM-like 从端（req/gnt/rvalid），面向下游是 AXI4-Lite 主端，常作 DMA 风格搬移前端。
- 它与 `axi_to_mem` 是**镜像对称**：AXI/存储角色互换，mem 侧信号同名但方向逐根相反。
- 请求通路用 `aw_sent_q`/`w_sent_q` 两位小状态机解耦 Lite 的 AW/W 通道，**两条都握手才授予**（授予信号兼任响应 FIFO 的 push）。
- 响应通路用一个 **1-bit、深度=MaxRequests 的 FIFO** 记录「写=1/读=0」的请求顺序，在**没有 ID 的 Lite 协议**上实现保序回收——前提是下游按序响应。
- 每笔 mem 请求恰好产生一笔单拍 Lite 事务；**响应延迟不固定、绝非 1**；不支持突发与 ATOP。
- `axi_from_mem`（Level 3）是「组合优于配置」的薄包装：`axi_lite_from_mem` + 纯组合 `axi_lite_to_axi`，自身不写任何握手逻辑；cache 属性需从外部端口喂入。

## 7. 下一步学习建议

- **补全搬移实践**：按第 5 节自建测试台并跑通，体会「逐字访问、延迟不定」的真实波形；这是理解所有 `to_mem`/`from_mem` 族模块的钥匙。
- **对比 to_mem 全家桶**（u14-l1）：阅读 `axi_to_mem_banked` / `axi_to_mem_interleaved` / `axi_to_mem_split`，看反向（AXI→SRAM）方向如何为带宽与面积做 bank/交错/读写分离优化；思考为何 `from_mem` 侧没有这些变体（提示：上游 SRAM 协议本身已是单端口逐字，优化空间在下游 AXI 一侧而非本模块）。
- **进入协议转换与系统级设计**：结合 u13-l1（`axi_to_axi_lite` / `axi_lite_to_axi`）回看本讲的组合关系；学完 u15 的异构网络后，尝试把 `axi_from_mem` 作为一个自定义主端接入 `axi_xbar`，构造一个完整的小型片上网络。
