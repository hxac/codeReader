# 时序、流水线策略与 EDA 兼容

## 1. 本讲目标

本讲是高级阶段的「工程化」一讲：不再讲某个具体 RTL 模块的功能，而是把前面散落在各模块里的时序与流水线决策收拢成一套**设计哲学**，并解释这套哲学如何同时服务于两个工程目标——跑得快（高频率）、跑得通（多 EDA 工具）。

学完后你应当能够：

1. 根据目标频率，为 `axi_xbar` 选出合适的 `LatencyMode` 与 `FallThrough`，并说清楚每个选择**改变了哪条组合路径**。
2. 解释为什么 `axi_xbar` 内部（demux 与 mux 之间的 cross 矩阵）**禁止插任何寄存器**，以及这条禁令是死锁证明给出的硬边界，而非偷懒。
3. 理解本库为兼容多种 EDA 工具而刻意保持「简单 SystemVerilog 子集」的编码约束，并看懂 CI 用三种工具（vsim / Verilator / Synopsys DC）互相补盲的回归流程。

## 2. 前置知识

本讲默认你已经掌握以下内容（这些是前置讲义的结论，这里直接使用）：

- **spill_register 是什么**（u7-l1）：来自外部依赖 `common_cells` 的深度为 1 的最小缓冲，作用是「切断输入到输出的组合路径 + 加一拍延迟」；`Bypass=1` 时退化为组合直通。
- **`axi_xbar` 的结构**（u6-l1）：顶层 = 1 个 `axi_xbar_unmuxed`（demux 阵列 + cross 矩阵）+ `NoMstPorts` 个 `axi_mux`。
- **xbar 的死锁证明**（u6-l3）：用 Coffman 四条件审视 W 通道，得出 demux↔mux 之间不能插 spill 寄存器。
- **构建脚本与 Bender target**（u1-l4）：`compile_vsim.sh`（仿真编译）、`run_verilator.sh`（lint）、`synth.sh`（综合 elaborate）三脚本都以日志内容（`grep`）判成败。

本讲要回答的核心问题是：**既然内部不能流水，那高频 xbar 怎么调时序？** 答案全部落在「对外端口」这一个旋钮上。

## 3. 本讲源码地图

| 文件 | 作用 |
|:--|:--|
| [doc/axi_xbar.md](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md) | xbar 的官方文档，其中 *Pipelining and Latency* 与 *Design Rationale for No Pipelining Inside Crossbar* 两节是本讲时序/流水线部分的权威出处。 |
| [src/axi_pkg.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv) | 定义 `xbar_latency_e` 位掩码枚举与 `xbar_cfg_t` 配置结构体（含 `FallThrough`、`LatencyMode`、`PipelineStages`）。 |
| [src/axi_xbar.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv) | 把 `Cfg.LatencyMode` 的各个位拆给 mux 的 `SpillAw/W/B/Ar/R` 端口。 |
| [src/axi_demux.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux.sv) | demux 内部给每条通道例化 `spill_register`，`SpillX` 参数控制 `Bypass`。 |
| [README.md](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/README.md) | *Which EDA Tools Are Supported?* 一节阐述兼容性哲学。 |
| [scripts/synth.sh](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/synth.sh) / [scripts/run_verilator.sh](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_verilator.sh) / [scripts/compile_vsim.sh](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/compile_vsim.sh) | 三件 EDA 工具的调用脚本，体现「多工具回归」实践。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先讲时序旋钮（LatencyMode 位掩码），再讲 FallThrough 对 W 通道组合路径的取舍，然后讲「为何内部不能流水」这条死锁硬边界，最后讲 EDA 兼容的编码约束与多工具回归。

### 4.1 时序旋钮：spill_register 与 LatencyMode 位掩码

#### 4.1.1 概念说明

回顾 u7-l1：`spill_register` 是深度为 1 的缓冲，能把一段组合路径切成两段、代价是加一拍延迟。xbar 里大部分组合逻辑都集中在 AW / AR 通道（地址译码、ID 比较表、轮询仲裁树），W / B / R 通道的组合逻辑相对轻。

于是自然的时序策略是：**只在组合逻辑最重的通道上插 spill 寄存器，且只插在对外端口上**（demux 的输入侧、mux 的输出侧），内部 cross 矩阵保持纯组合（原因见 4.3）。这套「在哪条通道、哪个端口插寄存器」的配置，被收口成一个 10 位的位掩码 `LatencyMode`。

#### 4.1.2 核心流程

`LatencyMode` 是 `bit [9:0]`，高 5 位管 demux 侧、低 5 位管 mux 侧，每一位对应一条 AXI 通道：

\[
\text{LatencyMode} = \underbrace{\text{DemuxAw}\, \text{DemuxW}\, \text{DemuxB}\, \text{DemuxAr}\, \text{DemuxR}}_{\text{bit }9..5\ \text{(demux 侧)}}
\;
\underbrace{\text{MuxAw}\, \text{MuxW}\, \text{MuxB}\, \text{MuxAr}\, \text{MuxR}}_{\text{bit }4..0\ \text{(mux 侧)}}
\]

某一位为 1，表示该通道在该端口插一级 spill 寄存器（切组合路径 + 加一拍延迟）；为 0 表示纯组合直通。

判断一条端到端通道被切了几刀，看它**同时穿过 demux 和 mux 各几位**。例如 AW 通道穿过 bit9（DemuxAw）和 bit4（MuxAw），所以一条 AW 的总延迟 = bit9 + bit4（单位：拍）。

`axi_pkg` 用 10 个 `localparam` 命名每一个位，再用一个枚举把常用组合命名出来：

```systemverilog
localparam bit [9:0] DemuxAw = (1 << 9);  // bit9: demux 侧 AW
// ... 其余 9 位同理
typedef enum bit [9:0] {
  NO_LATENCY    = 10'b000_00_000_00,
  CUT_SLV_AX    = DemuxAw | DemuxAr,
  CUT_MST_AX    = MuxAw | MuxAr,
  CUT_ALL_AX    = DemuxAw | DemuxAr | MuxAw | MuxAr,   // 推荐配置
  CUT_SLV_PORTS = DemuxAw | DemuxW | DemuxB | DemuxAr | DemuxR,
  CUT_MST_PORTS = MuxAw | MuxW | MuxB | MuxAr | MuxR,
  CUT_ALL_PORTS = 10'b111_11_111_11
} xbar_latency_e;
```

完整定义见 [axi_pkg.sv:L450-L479](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L450-L479)，其中 `localparam` 给出每个位的命名（L450-L469），`enum` 给出预设组合（L471-L479）。

**推荐配置 `CUT_ALL_AX`**：只切 AW 和 AR（bit 9/6/4/1）。因为 AW 穿过 demux+mux 两级，所以 AW 总延迟 = 2 拍；AR 同理 = 2 拍。W / B / R 保持 0 延迟。这正是文档原话——「recommended configuration (`CUT_ALL_AX`) is to have a latency of 2 on the AW and AR channels because these channels have the most combinatorial logic on them」。

#### 4.1.3 源码精读

**`LatencyMode` 如何被拆给 mux**：在 xbar 顶层，每例化一个 `axi_mux`，就把 `Cfg.LatencyMode` 的低 5 位逐位接到 mux 的 `SpillAw/W/B/Ar/R`：

```systemverilog
// src/axi_xbar.sv, gen_mst_port_mux 内
.FallThrough   ( Cfg.FallThrough    ),
.SpillAw       ( Cfg.LatencyMode[4] ),  // MuxAw
.SpillW        ( Cfg.LatencyMode[3] ),  // MuxW
.SpillB        ( Cfg.LatencyMode[2] ),  // MuxB
.SpillAr       ( Cfg.LatencyMode[1] ),  // MuxAr
.SpillR        ( Cfg.LatencyMode[0] ),  // MuxR
```

见 [axi_xbar.sv:L140-L145](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L140-L145)。demux 侧则由 `axi_xbar_unmuxed` 把高 5 位（bit9..5）喂给每个 `axi_demux` 的同名端口（结构与 mux 对称）。

**`SpillX` 参数如何变成 spill 寄存器**：以 demux 为例，`SpillAw` 等参数默认值是 `SpillAw=1, SpillW=0, SpillB=0, SpillAr=1, SpillR=0`（见 [axi_demux.sv:L56-L60](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux.sv#L56-L60)），内部对每条通道例化一个 `spill_register`，并把 `Bypass` 接成 `~SpillX`：

```systemverilog
// src/axi_demux.sv
spill_register #(.Bypass(~SpillAw)) i_aw_spill (...);  // AW 通道
spill_register #(.Bypass(~SpillW))  i_w_spill  (...);  // W 通道
// ... B / AR / R 同理
```

见 [axi_demux.sv:L89-L177](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux.sv#L89-L177)。于是 `SpillX=1`（即对应 LatencyMode 位为 1）→ `Bypass=0` → 真插寄存器；`SpillX=0` → `Bypass=1` → 组合直通。

> **口径统一**：xbar 顶层用 `LatencyMode` 位掩码统一下发，demux / mux 内部仍叫 `SpillAw..SpillR`，二者通过「位掩码位 ↔ Spill 参数」一一对应衔接。这是 u2-l2 讲过的「LatencyMode 高 5 位给 demux、低 5 位给 mux」的物理实现。

#### 4.1.4 代码实践

1. **实践目标**：学会手工读出任意 `LatencyMode` 值切了哪些通道、每条端到端通道延迟几拍。
2. **操作步骤**：
   - 打开 [axi_pkg.sv:L450-L479](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L450-L479)，对照位定义表。
   - 取 `CUT_SLV_PORTS = DemuxAw | DemuxW | DemuxB | DemuxAr | DemuxR = bit 9,8,7,6,5`，写成二进制：`10'b011_11_000_00`（高 5 位全 1，低 5 位全 0）。
   - 逐通道算端到端延迟：AW = bit9(DemuxAw)=1 + bit4(MuxAw)=0 = 1 拍；W = bit8(DemuxW)=1 + bit3(MuxW)=0 = 1 拍；B = bit7 + bit2 = 1 拍；AR = bit6 + bit1 = 1 拍；R = bit5 + bit0 = 1 拍。
3. **需要观察的现象**：`CUT_SLV_PORTS` 把所有 5 条通道各切一刀（全在 demux 侧），mux 侧零延迟；而 `CUT_ALL_AX` 只切 AW/AR 但各切两刀（demux+mux）。
4. **预期结果**：你应得到一张表——`CUT_SLV_PORTS` 下每条通道延迟均为 1 拍；`CUT_ALL_AX` 下 AW/AR 延迟 2 拍、W/B/R 延迟 0 拍。
5. 待本地验证：可用仿真在 master 端口观察同一事务在 AW 与对应 W 之间相隔的周期数来印证。

#### 4.1.5 小练习与答案

**练习 1**：把 `LatencyMode` 设成 `CUT_MST_PORTS`，AW 通道端到端延迟是几拍？为什么？

**答案**：`CUT_MST_PORTS = MuxAw|MuxW|MuxB|MuxAr|MuxR = bit 4,3,2,1,0`。AW 穿过 DemuxAw(bit9)=0 与 MuxAw(bit4)=1，共 1 拍。因为 mux 侧切了、demux 侧没切。

**练习 2**：为什么推荐配置不是 `CUT_ALL_PORTS`（全切）？

**答案**：`CUT_ALL_PORTS` 会给 W/B/R 也插寄存器，面积更大、延迟更高，而 W/B/R 的组合逻辑本来就不重，没必要切；`CUT_ALL_AX` 只切最重的 AW/AR，性价比最高。只有在两个 xbar 互联可能成环时（见 4.1 末与 4.2 末），才被迫用 `CUT_*_PORTS`。

---

### 4.2 FallThrough：W 通道组合路径的取舍

#### 4.2.1 概念说明

`FallThrough` 是 `xbar_cfg_t` 里的一个 `bit` 字段，控制 mux 内部 FIFO（用来按 AW 顺序转发 W 拍的那个 W FIFO）是「直通模式」（fall-through，输出与输入同拍可用）还是「普通模式」（输出晚一拍）。

它看似只是 mux 的一个内部细节，却直接决定了 **W 通道的组合路径有多长**——这是高频设计里最敏感的一根弦。

#### 4.2.2 核心流程

文档对 `FallThrough` 的定义是：

> Routing decisions on the AW channel fall through to the W channel. Enabling this allows the crossbar to accept a W beat in the same cycle as the corresponding AW beat, but it increases the combinatorial path of the W channel with logic from the AW channel.

两种模式的取舍：

| `FallThrough` | W 可与 AW 同拍接受？ | W 通道组合路径 | 适用场景 |
|:--:|:--:|:--|:--|
| `1` | 可以（延迟低、吞吐高） | **长**：AW 的路由决策（地址译码 + ID 比较 + 仲裁）会延伸进 W 路径 | 低频、追求最低延迟 |
| `0`（推荐） | 不可以（W 晚一拍） | **短**：W 路径不沾 AW 逻辑 | 高频 |

直觉理解：W 通道本身没有地址，它要「跟对」自己的 AW 才能去到正确的 master 端口。`FallThrough=1` 时，W 必须在 **同一拍** 内根据刚到的 AW 算出路由并转发，于是 AW 的全部决策逻辑被串到 W 的组合路径上；`FallThrough=0` 时，路由决策晚一拍稳定下来再放行 W，W 路径就干净了。

文档给出的推荐组合是 **`CUT_ALL_AX` + `FallThrough = 0`**，并明确「`FallThrough` should be set to `0` to prevent logic on the AW channel from extending combinatorial paths on the W channel」。

#### 4.2.3 源码精读

`FallThrough` 是配置结构体的一个字段：

```systemverilog
// src/axi_pkg.sv, xbar_cfg_t 内
/// Determine if the internal FIFOs of the crossbar are instantiated in fallthrough mode.
/// 0: No fallthrough   1: Fallthrough
bit            FallThrough;
```

见 [axi_pkg.sv:L495-L505](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L495-L505)（`FallThrough`、`LatencyMode`、`PipelineStages` 三个时序相关字段连在一起）。

它在 xbar 顶层被原样透传给每个 mux：

```systemverilog
.FallThrough   ( Cfg.FallThrough    ),
```

见 [axi_xbar.sv:L140](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L140)。

文档的两处关键表述：`FallThrough` 字段定义在配置表里（[doc/axi_xbar.md:L48](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L48)），推荐 `FallThrough=0` + `CUT_ALL_AX` 的那段在 *Pipelining and Latency* 一节（[doc/axi_xbar.md:L59-L65](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L59-L65)）。

> 补充一个易被忽略的边界条件（同节，[doc/axi_xbar.md:L65](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L65)）：若两个 xbar 互相把对方的 master 端口接进自己的 slave 端口（双向互联），两者的 `LatencyMode` 都必须取 `CUT_SLV_PORTS` / `CUT_MST_PORTS` / `CUT_ALL_PORTS` 之一，否则未切的通道会形成 **时序环路（timing loop）**。这条与 4.3 的死锁环路不同：死锁环路是协议层的（插寄存器→W FIFO 循环等待），时序环路是物理层的（组合路径成环→静态时序分析报组合环）。

#### 4.2.4 代码实践

1. **实践目标**：建立「FallThrough 影响 W 通道组合路径」的直觉，而非仅记结论。
2. **操作步骤**：
   - 阅读文档表述 [doc/axi_xbar.md:L48](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L48) 与 [doc/axi_xbar.md:L63](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L63)。
   - 在一张纸上画出 `FallThrough=1` 时 W 拍的组合路径：`slv_ports_w_valid` → 依赖「本拍 AW 的译码结果」→ 决定转发到哪个 mux → `mst_ports_w_valid`。
   - 再画 `FallThrough=0`：W 路径只依赖上一拍已经寄存好的路由选择，与当前拍 AW 无关。
3. **需要观察的现象**：两条路径里，前者多串了「地址译码 + ID 比较 + 仲裁」一大块组合逻辑。
4. **预期结果**：你能用一句话指出——`FallThrough=1` 把 AW 的决策逻辑并进了 W 的关键路径，这是高频设计的禁忌。
5. 待本地验证：若有综合工具，可分别对两种配置跑 `elab` 后的时序报告对比 W 通道关键路径长度。

#### 4.2.5 小练习与答案

**练习 1**：某设计频率不高、追求最低访问延迟，应如何设 `FallThrough` 与 `LatencyMode`？

**答案**：`FallThrough=1`（W 与 AW 同拍接受）+ `LatencyMode=NO_LATENCY`（全组合）+ 配合文档原话「it is possible to run the crossbar in a fully combinatorial configuration by setting `LatencyMode` to `NO_LATENCY` and `FallThrough` to `1`」。这是延迟最低但频率上限最低的配置。

**练习 2**：为什么把 `FallThrough` 设成 0 不能用「在 W 通道插 spill 寄存器」（`CUT_ALL_PORTS`）来等效替代？

**答案**：两者都能压短 W 的组合路径，但代价不同。`FallThrough=0` 只是让 mux 内部 FIFO 不直通，W 路径干净且无额外端口级寄存器；而给 W 通道插 spill（`CUT_ALL_PORTS` 等）会多一拍 W 延迟和额外寄存器面积，且在双向 xbar 互联场景下 W 被切还可能卷入 4.3 的死锁边界。所以调 `FallThrough` 是更轻的旋钮。

---

### 4.3 无内部寄存器的死锁边界

#### 4.3.1 概念说明

读到这里你可能会有一个很自然的想法：既然 spill 寄存器能切组合路径、AW/AR 又那么重，**为什么不干脆在 demux 输出和 mux 输入之间（cross 矩阵里）也插几级 spill 寄存器，把内部路径也切短？**

u6-l3 已经用 Coffman 四条件证明过这条路走不通会死锁。本讲只复述结论并强调它对**时序策略的约束意义**：这条禁令是一条硬边界，它决定了「xbar 的时序只能靠对外端口的 LatencyMode 调，内部 cross 永远是纯组合」。

#### 4.3.2 核心流程

Coffman 死锁四条件，前三条由 AXI 协议与 mux 本性决定、不可改，唯一能打破的是第④条：

1. **互斥（Mutual Exclusion）**：W 拍必须按 AW 顺序到达，不同 master 端口的 mux 互斥（顺序由 AW 仲裁树给定）。
2. **占有等待（Hold and Wait）**：valid 必须保持到 ready 拉高。
3. **不可抢占（No Preemption）**：AXI 不允许 W 拍交错，W burst 必须与 AW 同序。
4. **循环等待（Circular Wait）**：唯一可下手的一条。

如果在 demux→mux 之间插 spill 寄存器，配合 mux 内 AW 通道 `rr_arb_tree`「优先级逐拍推进一位」的机制，会在多个 mux 的 W FIFO 之间形成循环依赖（文档举了一个 10 输入仲裁树的例子：两个请求同拍竞争，胜者优先级只推进一位，下一拍可能同一端口再次胜出，与其他 mux 的仲裁树共同把 FIFO 推成环）。

**结论**：移除 demux↔mux 之间的 spill，强制「切换决策在 W FIFO 同一拍内发生」，使切换决策严格有序，从而打破循环等待。因此 cross 矩阵必须是纯组合直连。

#### 4.3.3 源码精读

这段权威论述在文档专门一节 *Design Rationale for No Pipelining Inside Crossbar*：

> Inserting spill registers between demuxers and muxers seems attractive to further reduce the length of combinatorial paths in the crossbar. However, this can lead to deadlocks in the W channel ... In fact, spill registers between the switching modules causes all four deadlock criteria to be met.

见 [doc/axi_xbar.md:L94-L109](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L94-L109)。四条件的逐条归属见 [doc/axi_xbar.md:L96-L109](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L96-L109)。

> **对时序策略的约束意义**（本讲视角）：正因为内部不能流水，xbar 想跑更高频率只能走两条路——(a) 用对外端口的 `LatencyMode`（4.1）+ `FallThrough=0`（4.2）压短**边界上**的关键路径；(b) 把单个大 xbar 拆成多个小 xbar 级联，每个 xbar 自成一段延迟孤岛，级联处用 `CUT_*_PORTS` 切断。这是为什么 u15-l4 异构网络里 xbar 总是「纯组合骨干」、所有状态化转换器只能挂在 xbar 对外端口上的根本原因。

#### 4.3.4 代码实践

1. **实践目标**：把抽象的死锁条件落到一张可指认的拓扑图上。
2. **操作步骤**：
   - 画一个最小场景：2 个 mux（M0、M1）× 2 个 demux（D0、D1），假设在 D0→M0、D1→M1 之间各插一个 spill 寄存器（即被禁的配置）。
   - 在图上标注：两条 W FIFO 各自持有了一个 W 拍（条件②占有等待），都在等对方的仲裁让出（条件①互斥），valid 不能撤（条件③），两个 FIFO 互相等（条件④循环等待）。
3. **需要观察的现象**：移除两个 spill 后，切换决策被强制压进同一拍，环被打破。
4. **预期结果**：你能指出条件④是图中唯一可通过「不插寄存器」消除的一条。
5. 待本地验证：本实践的目的是建立图示直觉，无需运行；详细的形式化推导见 u6-l3。

#### 4.3.5 小练习与答案

**练习 1**：有人说「既然内部不能插 spill，那我插 `axi_fifo`（深度更大）总可以吧？」这个说法对吗？

**答案**：不对。死锁证明针对的是「在 demux 与 mux 之间引入任何状态化缓冲（无论深度 1 的 spill 还是更深的 fifo）」，因为问题的根因是「切换决策被延迟了一拍以上」，与缓冲深度无关。任何状态化中间环节都会让四条件同时成立。

**练习 2**：那 `Cfg.PipelineStages`（[axi_pkg.sv:L503-L505](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L503-L505)）控制的「cross 处的 `axi_multicut`」不会触发死锁吗？

**答案**：待确认 / 需谨慎。`PipelineStages` 控制的是 cross 矩阵处的 `axi_multicut` 级数，注释明确警告「Having multiple stages can potentially add a large number of FFs!」。从死锁证明看，cross 处的寄存器与「demux↔mux 之间」的寄存器属于同一段路径，启用时务必确认你的拓扑（单 xbar 还是双向级联）不会让 W 通道成环——这也是文档把 `PipelineStages` 留作用户自担风险项、默认推荐 `CUT_ALL_AX`（靠端口 spill）而非堆 `PipelineStages` 的原因。

---

### 4.4 EDA 兼容：简单 SV 子集与多工具回归

#### 4.4.1 概念说明

前三个模块讲的是「如何让设计跑得快」，这一模块讲「如何让设计跑得通」——在 Mentor/Questa、Synopsys、Verilator、各 FPGA 厂商工具之间尽量不挑食。

本库的兼容性哲学很朴素：**代码写的是标准 SystemVerilog（IEEE 1800-2012），但真正的问题不是标准本身，而是「你的 EDA 工具实现了 SV 的哪个子集」**。因此可综合模块刻意只用最简单的语言构造，宁可写得啰嗦也不用花哨特性。

#### 4.4.2 核心流程

兼容性靠两条腿走路：**编码约束** + **多工具回归**。

编码约束（来自 README）：

- 可综合模块尽量用最简单的 SV 构造；欢迎进一步简化代码以兼容更多工具的贡献。
- 接受针对特定工具的 workaround，但要同时满足四条：该工具广泛使用、影响的是近期版本、不破坏其他工具、不显著增加维护负担。
- 遇到工具的 SV 支持问题，建议直接把本库代码作为 testcase 报给 EDA 厂商。

多工具回归（CI 同时跑三类工具，互相补盲）：

| 脚本 | 工具 | 目标 / Top | 查什么 |
|:--|:--|:--|:--|
| `compile_vsim.sh` | Mentor/Questa vsim | `-t test -t rtl` | 仿真器能否编译 + `axi_pkg` 严格 lint |
| `run_verilator.sh` | Verilator | `-t synthesis -t synth_test`，top `axi_synth_bench` | 开源 lint（`--lint-only --timing`） |
| `synth.sh` | Synopsys DC | `-t synth_test`，`elaborate axi_synth_bench` | 综合器能否 elaborate（可综合性回归） |

三者的共同点：都以 `axi_synth_bench`（综合用例）为参照、都用 `grep` 日志内容（而非进程返回码）判成败。

#### 4.4.3 源码精读

**兼容性哲学的权威表述**在 README 专节：

> Our code is written in standard SystemVerilog ... so the more important question is: Which subset of SystemVerilog does your EDA tool support? ... we strive to use as simple language constructs as possible, especially for our synthesizable modules.

见 [README.md:L124-L137](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/README.md#L124-L137)，兼容性作为四大设计目标之一见 [README.md:L13](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/README.md#L13)。

**仿真器编译脚本** `compile_vsim.sh`：用 Bender 生成按 Level 0–6 排序的编译脚本，并只对全库根基 `axi_pkg` 单独加 `-lint -pedanticerrors`（因为它被几乎所有模块 import，错一个全库崩），其余文件宽松：

```bash
# scripts/compile_vsim.sh
bender script vsim -t test -t rtl \
    --vlog-arg="-svinputport=compat" \
    --vlog-arg="-override_timescale 1ns/1ps" \
    --vlog-arg="-suppress 2583" > compile.tcl
# 仅给 axi_pkg 注入 -lint -pedanticerrors（用 awk 改编译脚本）
for x in axi_pkg; do ... done
```

见 [compile_vsim.sh:L21-L45](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/compile_vsim.sh#L21-L45)。注意几个兼容性 hint：`-svinputport=compat`（端口声明兼容旧式）、`-override_timescale`（统一时间精度）、`-suppress 2583`（压制特定告警）——这些都是为兼容不同工具/版本而打的补丁。

**Verilator lint 脚本** `run_verilator.sh`：以综合用例为 top 做纯 lint，`-Wno-fatal` 把警告降级为非致命，便于在「警告很多的现实代码」上仍能跑通：

```bash
# scripts/run_verilator.sh
bender script verilator -t synthesis -t synth_test > ./verilator.f
VERILATOR_FLAGS=(-Wno-fatal)
$VERILATOR --top-module axi_synth_bench --lint-only --timing -f verilator.f ${VERILATOR_FLAGS[@]}
```

见 [run_verilator.sh:L24-L29](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_verilator.sh#L24-L29)。

**综合 elaborate 脚本** `synth.sh`：用 Synopsys DC 把综合用例 elaborate 出来（不做完整综合、只查可综合性与连线），以 `grep "error:"` 判成败：

```bash
# scripts/synth.sh
echo 'remove_design -all' > ./synth.tcl
bender script synopsys -t synth_test >> ./synth.tcl
echo 'elaborate axi_synth_bench' >> ./synth.tcl
cat ./synth.tcl | $SYNOPSYS_DC | tee synth.log 2>&1
grep -i "warning:" synth.log || true
grep -i "error:" synth.log && false     # 有 error 即失败
```

见 [synth.sh:L22-L28](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/synth.sh#L22-L28)。该脚本被 Makefile 的 `elab.log` 目标包装（`make elab.log`），见 [Makefile:L72-L73](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Makefile#L72-L73)。

> **为什么用三种工具**：每类工具擅长查的问题不同——Verilator 是开源 lint，对常见 SV 陷阱灵敏但语法支持窄；vsim 是工业仿真器，语法支持广；DC elaborate 是综合器，能抓出「仿真能过但不可综合」的构造（如某些 initial、动态数组）。三者交叉覆盖，才敢说「代码在多 EDA 工具上兼容」。

#### 4.4.4 代码实践

1. **实践目标**：亲手跑一次综合 elaborate 回归，理解它查的是「可综合性」而非「功能」。
2. **操作步骤**：
   - 确认环境有 Synopsys DC（无则跳到「源码阅读型」分支）。
   - 在仓库根目录执行 `make elab.log`（它会在 `build/` 下调用 `scripts/synth.sh`）。
   - 打开生成的 `elab.log`，搜索 `Error:` 与 `Warning:`。
3. **需要观察的现象**：日志末尾应无 `Error:`；`axi_synth_bench` 顶层会被 elaborate 出来（`Current design...`），可看到大量不同宽度/参数的 xbar 配置被实例化。
4. **预期结果**：`make elab.log` 成功（脚本最后 `touch synth.completed`），证明当前 HEAD 的可综合模块在 Synopsys DC 下可 elaborate。
5. 待本地验证：若无 DC，改为阅读 [synth.sh:L22-L28](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/synth.sh#L22-L28) 与 [run_verilator.sh:L24-L29](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_verilator.sh#L24-L29)，说明二者分别用 DC 与 Verilator 查的是哪类问题（可综合性 vs 语法/lint）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `compile_vsim.sh` 只对 `axi_pkg` 一个文件开 `-lint -pedanticerrors`，而不是全库都开？

**答案**：`axi_pkg` 是全库共享类型的 `package`、被几乎所有模块 import，又无内部依赖（Level 0），错一个全库崩，所以必须最严格。其余文件若全开 `-pedanticerrors` 会因大量既有告警而无法编译，反而挡住正常流程；严格性按「影响半径」分级。

**练习 2**：你新增了一个可综合模块，CI 三脚本里哪个最可能先抓出「这段代码仿真能过但综合会报错」？

**答案**：`synth.sh`（Synopsys DC elaborate）。它专门做可综合性检查；vsim 与 Verilator 主要查仿真/语法层。这就是 CI 同时保留三类工具的意义——分工补盲。

---

## 5. 综合实践

**任务**：假设你要把一个 `axi_xbar`（2 slave 端口 × 4 master 端口）用在一个目标频率较高的子系统中，请完成时序配置并分析其对 W 通道组合路径的影响。

请产出：

1. **配置选择**：写出你选的 `LatencyMode`（用 `xbar_latency_e` 枚举名）与 `FallThrough` 值，并用一句话说明理由（提示：高频 → 压短 AW/AR 关键路径 + 不让 AW 逻辑污染 W）。
2. **位掩码展开**：把所选 `LatencyMode` 写成 `10'b...` 二进制，并指出 AW、AR、W、B、R 五条通道各自的端到端延迟拍数（参考 4.1 的算法：AW 延迟 = bit9 + bit4，依此类推）。
3. **W 通道组合路径分析**：结合 4.2，说明在你的配置下，W 通道组合路径上**是否包含** AW 的路由决策逻辑；并与「`FallThrough=1` + `NO_LATENCY`」的极限低延迟配置做对比，指出后者 W 路径长在哪。
4. **边界自检**：确认你的配置没有违反 4.3 的死锁边界（即你没有在 cross 矩阵内部插任何 spill/fifo），也没有在双向 xbar 互联场景下留下未切通道（4.2 末的时序环路）。

**参考答案要点**：

1. 选 `LatencyMode = CUT_ALL_AX` + `FallThrough = 0`。理由：CUT_ALL_AX 把最重的 AW/AR 各切两刀（demux+mux）压成 2 拍延迟，W/B/R 保持 0 拍不被打扰；FallThrough=0 阻止 AW 决策逻辑延伸进 W 组合路径。这是文档明示的高频推荐组合。
2. `CUT_ALL_AX = DemuxAw|DemuxAr|MuxAw|MuxAr = bit 9,6,4,1 = 10'b010_00_010_10`。端到端延迟：AW = bit9+bit4 = 2 拍；AR = bit6+bit1 = 2 拍；W = bit8+bit3 = 0 拍；B = bit7+bit2 = 0 拍；R = bit5+bit0 = 0 拍。
3. `FallThrough=0` 下，W 路由决策晚一拍稳定，W 通道组合路径**不含**当前拍 AW 的译码/比较/仲裁逻辑，路径短、利于高频。对比 `FallThrough=1` + `NO_LATENCY`：W 必须与 AW 同拍算出路由，W 组合路径上多串了「地址译码 + ID 比较 + 仲裁树」一大块，关键路径显著变长——这是用频率换延迟。
4. 死锁边界：CUT_ALL_AX 只在对外端口（demux 输入侧 / mux 输出侧）插 spill，cross 矩阵内部纯组合，满足 4.3。时序环路：本例是单 xbar、非双向互联，不触发 4.2 末的环路条件；若日后把它与另一个 xbar 双向互联，则需把双方 LatencyMode 改成 `CUT_*_PORTS` 之一。

## 6. 本讲小结

- **时序旋钮收口于 `LatencyMode`**：一个 10 位位掩码，高 5 位给 demux、低 5 位给 mux，每一位决定一条通道在该端口是否插 spill 寄存器；推荐 `CUT_ALL_AX` 只切最重的 AW/AR（各 2 拍），W/B/R 保持 0 拍。
- **`FallThrough` 控制 W 通道组合路径**：`FallThrough=0`（推荐）让 W 路由决策晚一拍，W 路径不沾 AW 逻辑、利于高频；`FallThrough=1` 让 W 与 AW 同拍决策，延迟低但 W 关键路径变长。
- **内部不能流水是死锁硬边界**：Coffman 四条件里前三条由协议决定不可改，唯一能打破的「循环等待」要求 demux↔mux 之间纯组合直连；所以 xbar 调时序只能靠对外端口 LatencyMode，或拆成多个小 xbar 级联。
- **双向 xbar 互联有额外约束**：必须双方都用 `CUT_SLV/MST/ALL_PORTS`，否则未切通道形成时序环路（与死锁环路不同，这是物理组合环）。
- **EDA 兼容靠「简单 SV 子集 + 多工具回归」**：可综合模块刻意只用最简构造；CI 同时跑 vsim（编译 + axi_pkg 严格 lint）、Verilator（开源 lint）、Synopsys DC（elaborate 可综合性），三工具分工补盲。

## 7. 下一步学习建议

- 接着读 **u16-l4 贡献流程与 CI**：把本讲的三脚本放回 `.gitlab-ci.yml` / GitHub Actions 的完整 CI 上下文，看清一次 PR 必须通过哪些检查。
- 回看 **u15-l4 异构网络设计实战**：把本讲的「xbar 纯组合、转换器挂对外端口」结论用到跨时钟域、跨宽度、跨 ID 的完整互联拓扑里。
- 想深入死锁证明的细节，重读 **u6-l3** 的 Coffman 四条件分析；想看 LatencyMode 各位的来源定义，重读 **u2-l2** 的 `xbar_cfg_t` / `xbar_latency_e` 部分。
- 建议继续阅读的源码：[doc/axi_xbar.md](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md) 的 *Pipelining and Latency* 与 *Design Rationale* 两节，以及 [scripts/](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/) 下的三个工具脚本。
