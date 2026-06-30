# axi_mux：多路汇聚与 ID 扩展

## 1. 本讲目标

本讲是「多路复用与路由核心」单元的第三讲，紧接 u5-l1（demux）。我们要回答一个和 demux 镜像的问题：**当多个 AXI 主设备（多个 slave 端口）要把事务送到同一条下游总线（一个 master 端口）时，该用什么模块、它怎么保证「响应能正确回到发请求的那个主设备」？**

读完本讲，你应该能够：

- 说清楚 `axi_mux` 如何把 `NoSlvPorts` 个 slave 端口**汇聚**成一个 master 端口，以及它为什么比 demux **更简单**（无需 ID 在途计数器）。
- 解释 **ID 扩展路由策略**：mux 在每个 slave 端口上用 `axi_id_prepend` 把「端口号」拼进 AXI ID 的高位，于是响应只要看 ID 高位就能知道该回哪个端口——这是 mux 区别于 demux 的核心机制。
- 读懂请求方向（AW/AR）的**轮询仲裁**、W 通道用 **W FIFO** 跟随其 AW 保序转发的过程，以及一个叫 `lock_aw_valid` 的小机制如何切断 AW→W 的组合依赖、避免死锁。
- 读懂响应方向（B/R）用 **one-hot 解码 ID 高位**把响应精准送回唯一 slave 端口的过程。
- 看懂 mux 与 demux 在 `axi_xbar` 里**对称搭档**的角色：xbar = demux 阵列（每 slave 一个）+ mux 阵列（每 master 一个）。

本讲不修改任何 RTL，重点是「读懂一个 N 汇 1 的复用器，并理解它和 demux 的对偶关系」。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前置讲义）：

- **五通道与 valid/ready 握手**（u1-l3）：AW/W/B/AR/R；写事务用 AW/W/B、读事务用 AR/R；每拍只有 `valid` 与 `ready` 同高才算一次握手；`in flight`（在途）/`pending`（挂起）两个术语会反复出现。
- **AXI 的 W 突发保序规则**（u1-l3、u5-l1）：W 拍**必须和 AW 的顺序一致**，不同写突发的 W 拍**不允许交错**。这是本讲 W FIFO 能成立的协议前提。
- **axi_demux 的结构与术语**（u5-l1，**本讲最重要的前置**）：demux 做 1 拆 N，请求方向用 `select` 选端口、响应方向用 `rr_arb_tree` 回收；demux 需要用 `axi_demux_id_counters` 跟踪同 ID 在途事务来保序。本讲会不断把 mux 和它对照。
- **axi_id_prepend**（u4-l2）：一个把来源标签拼到 AXI ID 最高位、响应方向再把高位剥离的纯组合连接器。mux 内部正是靠它实现「ID 扩展」。
- **typedef / assign 宏体系与「接口外壳 + 结构体内核」范式**（u2-l4）：`req_t`/`resp_t` 内核 + `AXI_BUS` 接口外壳。
- **组合路径与 spill 寄存器**（u4-l1、u5-l1）：`spill_register` 切断一条通道的组合路径，代价是增加一拍延迟、不损吞吐。

本讲还会用到两个被 mux 例化（而非自己实现）的外部积木，先点名：

- **`rr_arb_tree`**（来自 `common_cells 1.39.0`）：轮询（round-robin）仲裁器，在多个请求者里挑一个授权；u5-l1 讲 demux 响应回收时已经见过它。
- **`fifo_v3`**（来自 `common_cells 1.39.0`）：一个带 `FALL_THROUGH` 选项的标准 FIFO，本讲里它存放「正在排队的 AW 各自来自哪个 slave 端口」。

## 3. 本讲源码地图

本讲涉及的核心源码文件如下：

| 文件 | 编译层级 | 角色 |
| --- | --- | --- |
| `src/axi_mux.sv` | Level 2 | **本讲主角**：把 N 个 slave 端口汇聚成 1 个 master 端口的复用器，含结构体内核 `axi_mux` 与接口外壳 `axi_mux_intf`。 |
| `doc/axi_mux.md` | — | mux 的官方文档：ID 扩展路由原理、W FIFO、参数表、原子事务说明。 |
| `src/axi_id_prepend.sv` | Level 2 | 被 mux 每个 slave 端口例化一次，负责把端口号拼进 ID 高位（u4-l2 已精读）。 |
| `src/axi_xbar.sv` | Level 5 | mux 的「最典型用法」：每个 master 端口配一个 `axi_mux`，是本讲 4.4 节与综合实践的蓝本。 |

层级关系：`axi_mux`（L2）依赖 `axi_id_prepend`（L2，同层、但 id_prepend 无内部本包依赖所以可同层）以及 `common_cells` 里的 `rr_arb_tree` / `fifo_v3` / `spill_register`。注意 mux 和 demux_simple 同处 Level 2，但 demux（带 spill 外壳）在 Level 3、xbar 在 Level 5——**「层级数字 = 它站在多少层积木之上」**。

> **关于文档与代码命名的一处差异（以源码为准）**：`doc/axi_mux.md` 的参数表把 ID 宽度参数写成 `AxiIdWidth`（[doc/axi_mux.md:27](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_mux.md#L27)），但当前 HEAD 的 `axi_mux` 源码里该参数叫 `SlvAxiIDWidth`，且 master 端口 ID 宽度是派生量 `MstAxiIDWidth`（[src/axi_mux.sv:30](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L30)、[src/axi_mux.sv:68-69](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L68-L69)）。本讲一律以源码命名为准。

## 4. 核心概念与源码讲解

### 4.1 mux 整体架构：N 汇 1 与 ID 扩展策略

#### 4.1.1 概念说明

一个 mux（复用器）做的是 **N 合 1**：上游有 N 个 AXI 主设备（接在 mux 的 N 个 slave 端口上），下游只有一条 AXI 总线（mux 的 1 个 master 端口）。mux 要把 N 路请求**交错（interleave）**着送到同一条下游总线上。

这件事里最难的并不是「请求怎么合」——N 选 1 本质就是个仲裁问题，一棵 `rr_arb_tree` 就够。真正的难点是：**响应怎么原路退回？** 下游 slave 返回一个 B 或 R 时，mux 怎么知道它该回给上游 N 个主设备里的哪一个？

demux 不存在这个问题，因为它只有 1 个 slave 端口，所有响应天然回到同一个上游。mux 有 N 个 slave 端口，响应必须**精准分发**。

mux 的解法极其优雅，叫 **ID 扩展路由**：

- 每个slave 端口 `i` 上，用一个 `axi_id_prepend` 把端口号 `i` 拼进 AXI ID 的**最高 MstIdxBits 位**。于是 master 端口看到的 ID = `{端口号, 原始 ID}`。
- master 端口 ID 比slave 端口 ID 宽 \( \lceil \log_2(\text{NoSlvPorts}) \rceil \) 位。
- 当下游返回 B/R 时，mux 只看响应 ID 的高 MstIdxBits 位，就能解码出「它来自第几个 slave 端口」，从而把响应送回正确的上游。

这套机制带来一个极其重要的副作用：**mux 完全不用关心 AXI 的「同 ID 保序」约束**。因为不同 slave 端口的主设备，它们的 ID 高位被强行写成不同的端口号，在 master 端口侧永远不可能撞 ID——保序的责任被推回给「每个 slave 端口上挂着的那一个主设备」。这正是文档反复强调的设计意图（[doc/axi_mux.md:18-21](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_mux.md#L18-L21)）。对比 demux 必须维护一组 `axi_demux_id_counters`（u5-l1、u5-l2），mux **根本不需要任何 ID 在途计数器**——这是它比 demux 简单的根本原因。

#### 4.1.2 核心流程

```
                ┌──────────────────────── axi_mux ────────────────────────┐
  上游 N 个      │  slave[0] ── id_prepend(0) ─┐                            │
  主设备         │  slave[1] ── id_prepend(1) ─┤  AW/AR: rr_arb_tree 选源   │──> 1 个
  (slave 端口)   │  ...                       ┤  W : 按 W FIFO 队头选源     │     master
                │  slave[N-1] id_prepend(N-1)┘                            │     端口
                │                                                        │
                │  B/R: 解码 id 高位 → one-hot → 只送给对应的 slave[i]     │
                └────────────────────────────────────────────────────────┘
```

把职责对应到方向上：

- **请求方向（AW、AR、W）**：先给每个 slave 端口的 ID 拼上端口号；AW/AR 用一棵 `rr_arb_tree` 在 N 路里轮询挑一路转发；W 没有自己的仲裁，而是用一个 **W FIFO** 记住「当前正在排队的 AW 各来自哪个 slave 端口」，按队头转发 W 拍。
- **响应方向（B、R）**：不需要仲裁（下游只有 1 路响应），只需要**分发**：解码 B/R 的 ID 高位，得到目标 slave 端口号，用 one-hot 只把 valid 拉给那一个端口。

> 这张图和 u5-l1 的 demux 数据流图正好「上下颠倒」：demux 是请求方向 1→N（按 select）、响应方向 N→1（仲裁）；mux 是请求方向 N→1（仲裁）、响应方向 1→N（按 ID 解码）。

#### 4.1.3 源码精读

**(a) 模块参数与 ID 宽度派生**

[src/axi_mux.sv:28-69](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L28-L69) —— 模块声明。几个要点：

- slave 与 master 两套通道类型成对出现（`slv_aw_chan_t` / `mst_aw_chan_t` 等），因为两侧 ID 宽度不同；W 通道只有一种类型（`w_chan_t`），因为 W 不带 ID。
- 两个最关键的派生参数在 [src/axi_mux.sv:68-69](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L68-L69)：

```systemverilog
localparam int unsigned MstIdxBits    = $clog2(NoSlvPorts);     // 端口号占几位
localparam int unsigned MstAxiIDWidth = SlvAxiIDWidth + MstIdxBits; // master ID 宽度
```

即 mux 的 master 端口 ID 宽度 = slave 端口 ID 宽度 + 端口号位数。这正是 u1-l1 提到的「xbar 的 master 端口 ID 比 slave 端口宽 ⌈log2(NoSlvPorts)⌉ 位」的物理来源——因为 xbar 的每个 master 端口都挂着一个 `axi_mux`。

- `MaxWTrans`（默认 8）是 W FIFO 的深度，即「最多允许多少个 AW 的 W 突发同时在排队」；`FallThrough`（默认 0）传给 W FIFO；五个 `SpillXX`（默认 AW/AR 开、W/B/R 关）控制各通道的 spill 寄存器，与 demux 完全同构。

**(b) `NoSlvPorts == 1` 的退化直通**

[src/axi_mux.sv:72-148](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L72-L148) —— 当只有一个 slave 端口时，`gen_no_mux` 分支不例化任何仲裁器或 FIFO，只给五通道各放一个 `spill_register`（可 Bypass）做纯直通。这与 demux_simple 在 `NoMstPorts==1` 时退化成连线的优化（u5-l1 练习 2）是同一思想：**退化情形零开销**。

**(c) 给每个 slave 端口拼端口号**

[src/axi_mux.sv:211-259](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L211-L259) —— `gen_id_prepend` 循环，对每个 slave 端口 `i` 例化一个 `axi_id_prepend`，关键是这句：

```systemverilog
.pre_id_i ( switch_id_t'(i) ),   // 把端口号 i 拼进 ID 高位（[src/axi_mux.sv:227](.../src/axi_mux.sv#L227)）
```

`switch_id_t` 是 `logic [MstIdxBits-1:0]`（[src/axi_mux.sv:153](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L153)）。于是经过这一层，slave 端口 `i` 的请求 ID 从 `SlvAxiIDWidth` 位变成了 `{i, 原ID}` 共 `MstAxiIDWidth` 位。这层之后，所有内部信号（`slv_aw_chans` 等）就已经是「带端口号的 master 宽度 ID」了。

**(d) 参数合法性断言**

[src/axi_mux.sv:466-494](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L466-L494) —— 仿真期断言，强制：`SlvAxiIDWidth>0`、`NoSlvPorts>0`、`MaxWTrans>0`，以及 `MstAxiIDWidth >= SlvAxiIDWidth + $clog2(NoSlvPorts)`（[src/axi_mux.sv:473-474](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L473-L474)）——后者确保 master 端口 ID 真的塞得下端口号。如果你自己接线时两侧 ID 宽度不满足这个关系，仿真会在 `initial` 里 `$fatal`。

> 文档对这套 ID 扩展策略有一句关键警告（[doc/axi_mux.md:13](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_mux.md#L13)）：ID 高位**必须**对应 slave 端口号，否则响应会送错主设备、功能崩坏。好消息是：只要你用的是 `axi_mux` 本体，这套扩展由内部的 `axi_id_prepend` 自动完成，你无需也不会去手设高位。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：用「追踪 ID 宽度变化」的方式，亲手验证 4.1.1 的 ID 扩展公式。

1. 打开 [src/axi_mux.sv:68-69](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L68-L69)，记下 `MstIdxBits = $clog2(NoSlvPorts)` 和 `MstAxiIDWidth = SlvAxiIDWidth + MstIdxBits`。
2. 假设一个具体配置：`NoSlvPorts = 4`、`SlvAxiIDWidth = 6`。手算：`MstIdxBits = 2`，`MstAxiIDWidth = 8`。
3. 打开 [src/axi_mux.sv:227](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L227)，确认 slave 端口 0~3 分别被拼上 `2'b00`、`2'b01`、`2'b10`、`2'b11`。
4. **需要观察的现象**：源码注释 [src/axi_mux.sv:21-22](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L21-L22) 给了一个例子——4 个 slave 端口时，一个 ID 为 `6'b100110` 的响应会被送到 slave 端口 2（因为高位 `2'b10` = 2）。把你手算的配置套进去复述一遍。
5. **预期结果**：能口头复述「N 个 slave 端口时，master 端口 ID 多出 \( \lceil \log_2 N \rceil \) 位，这若干位就是目标 slave 端口号」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 mux 不需要 demux 那样的 `axi_demux_id_counters`？
**答案**：因为 mux 用 ID 高位把不同 slave 端口的主设备强行隔离开——它们在 master 端口侧的 ID 永远不同（高位 = 端口号不同），根本不存在「同 ID 事务跨端口乱序」的问题。AXI 同 ID 保序的责任被下放给每个 slave 端口上挂的那一个主设备自己。所以 mux 无需跟踪在途 ID，省掉了一整组计数器。

**练习 2**：如果 `NoSlvPorts` 不是 2 的幂（例如 3），`$clog2(NoSlvPorts)` 是多少？端口号 3 会出现吗？
**答案**：`$clog2(3) = 2`，所以 `MstIdxBits = 2`，能编码 0~3。但 mux 只有 3 个 slave 端口（编号 0、1、2），端口号 3 不会出现在 `pre_id_i` 里。这意味着 ID 高位为 `2'b11` 的响应在正常工作时不会出现——若下游因某种原因返回了高位为 3 的响应，会被 one-hot 解码到一个不存在的端口（详见 4.3 的练习）。

---

### 4.2 请求方向：ID 前置、轮询仲裁与 W FIFO 保序

#### 4.2.1 概念说明

经过 4.1 的 `axi_id_prepend` 之后，N 路 slave 请求都已经是「带端口号的统一宽度 ID」。请求方向剩下的工作是 **N 选 1**：

- **AW 与 AR**：各用一棵 `rr_arb_tree` 在 N 路里轮询挑一路转发到 master 端口。和 demux 回收 B/R 用的仲裁器是同一种（`LockIn=1`），只是方向相反——demux 是把 N 路响应合成 1 路，mux 是把 N 路请求合成 1 路。
- **W**：W 通道**没有自己的仲裁器**。原因有二：① W 不带 ID，仲裁器没法用它区分来源；② AXI 规定 W 突发必须和 AW 同序、且不交错。所以 mux 用一个 **W FIFO** 记住「按 AW 仲裁顺序排队中的每个 AW 各来自哪个 slave 端口」，W 拍按 FIFO 队头的端口号转发即可。

W FIFO 的入队/出队规则：AW 在 master 端口握手时，把它的（已扩展）ID 的高 MstIdxBits 位——也就是 slave 端口号——**压入** W FIFO；W 通道每送完一个突发的最后一拍（`w.last && 握手`），**弹一次** FIFO。因为 W 突发按 AW 顺序到达、且不交错，一个简单 FIFO（而非随机访问结构）就足以跟踪「当前 W 拍该走哪个 slave 端口」。文档对此有明确说明（[doc/axi_mux.md:15](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_mux.md#L15)）。

> 这与 demux 的 W 处理形成有趣对照：demux 只有 1 个 W 源（单个 slave 端口），用一个 `w_open` 计数器 + 一个 `w_select_q` 寄存器跟踪「队头端口」（u5-l1 4.2.3）；mux 有 N 个 W 源，必须用一个真正的 FIFO 把「队头端口号」按 AW 顺序存起来。

#### 4.2.2 核心流程

```
AW 方向：
  slv_aw_valids[N] ──> rr_arb_tree(LockIn=1) ──> aw_valid / mst_aw_chan(已带端口号)
                                          │
                                  (master AW 握手时)
                                          ├──> 把 mst_aw_chan.id 的高位 push 进 W FIFO
                                          └──> lock_aw_valid_q：若当拍 master 没接住，锁住下一拍重发（不重复 push）

W 方向：
  slv_w_chans[N]  ──> mst_w_chan = slv_w_chans[ W FIFO 队头端口号 ]
  W FIFO 队头端口的 w_valid  ──> mst_w_valid
  当 (w_valid & w_ready & w.last) ──> pop W FIFO（一个突发出队一次）

AR 方向：与 AW 同构，但不需要 W FIFO（读没有数据拍）。
```

#### 4.2.3 源码精读

**(a) AW 仲裁**

[src/axi_mux.sv:264-281](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L264-L281) —— 一棵 `rr_arb_tree`，`NumIn = NoSlvPorts`，`AxiVldRdy=1`（按 valid/ready 协议授权），`LockIn=1`（锁定胜者直到其握手完成）。输入是 N 路 `slv_aw_valids`（已带端口号），输出 `aw_valid` 与胜者的 AW 通道 `mst_aw_chan`。注意输出握手用的是内部信号 `aw_ready` 而非直接接 master 端口——因为中间还隔着 W FIFO 满判断和 spill 寄存器。

**(b) AW 控制与 `lock_aw_valid`（切断 AW→W 依赖）**

[src/axi_mux.sv:284-315](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L284-L315) —— 这是请求方向最精巧的一段。逻辑分两态：

```
if (lock_aw_valid_q) begin                    // 上一拍已经仲裁过、但 master 没接住
  mst_aw_valid = 1'b1;                        // 继续举 valid，但不再 push W FIFO（避免重复入队）
  if (mst_aw_ready) begin aw_ready=1; 解锁; end
end else begin
  if (!w_fifo_full && aw_valid) begin         // W FIFO 有空间且仲裁器有请求
    mst_aw_valid = 1'b1;
    w_fifo_push  = 1'b1;                      // 把端口号压入 W FIFO
    if (mst_aw_ready) aw_ready = 1'b1;        // master 当拍接住，完成
    else begin lock_aw_valid_d = 1'b1; end    // master 没接住 → 锁定，下拍重发（不再 push）
  end
end
```

注释 [src/axi_mux.sv:176-177](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L176-L177) 一语道破目的：**「This FF removes AW to W dependency」**。如果没有这个锁存，AW 能否通过会同时依赖「W FIFO 没满」和「master AW ready」，而 W FIFO 的弹出又依赖 W 通道推进——一旦下游 W 堵住、FIFO 满，AW 就死锁。引入 `lock_aw_valid_q` 后，AW 的 push 决策只在「进入锁定」那一刻做一次，之后即使 FIFO 状态变化也不再重复 push，从而把 AW 推进与 W 推进**解耦**。这是 mux 内部防死锁的关键一笔。

**(c) W FIFO**

[src/axi_mux.sv:317-333](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L317-L333) —— 一个 `fifo_v3`，`FALL_THROUGH = FallThrough`，`DEPTH = MaxWTrans`，数据类型是 `switch_id_t`（端口号）。注意 push 的数据（[src/axi_mux.sv:329](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L329)）：

```systemverilog
.data_i ( mst_aw_chan.id[SlvAxiIDWidth+:MstIdxBits] )  // 取已扩展 ID 的高位 = slave 端口号
```

即「把胜出 AW 的端口号压队」。`MaxWTrans` 决定最多能有多少个 AW 的 W 突发同时在排队——它直接限制 mux 的写并发度（outstanding 写事务数）。在 xbar 里它被设成 `Cfg.MaxSlvTrans`（见 4.4.3）。

**(d) W 通道路由**

[src/axi_mux.sv:353-367](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L353-L367) —— 三行核心：

```systemverilog
assign mst_w_chan = slv_w_chans[w_fifo_data];                 // 队头端口的 W 载荷
...
mst_w_valid                = slv_w_valids[w_fifo_data];       // 队头端口的 valid
slv_w_readies[w_fifo_data] = mst_w_ready;                     // ready 只回给队头端口
w_fifo_pop = slv_w_valids[w_fifo_data] & mst_w_ready & mst_w_chan.last; // 最后一拍弹队
```

清清楚楚：W FIFO 队头存的是「当前该转发哪个 slave 端口的 W」，载荷/valid/ready 三件套全部只指向那个端口；当且仅当队头端口送出一个突发的 `last` 拍并握手，才弹队。**这保证了不同 slave 端口的 W 突发绝不交错**，完全符合 AXI。

**(e) AR 仲裁**

[src/axi_mux.sv:409-440](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L409-L440) —— AR 与 AW 同构（一棵 `rr_arb_tree` + 一个 spill 寄存器），但没有 W FIFO 那套——因为读事务没有数据拍要跟随，AR 一旦仲裁通过就完事。

> 请求方向的 AW/AR 默认开 spill（`SpillAw=1`、`SpillAr=1`，[src/axi_mux.sv:50-54](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L50-L54)），目的是切断仲裁树到 master 端口的组合路径；W/B/R 默认关。这与 demux 的默认 spill 策略完全一致（u5-l1 4.3）。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：把一个「AW 赢得仲裁 → 其 W 突发被转发 → 弹队」的完整生命周期在源码上走一遍。

1. 从 [src/axi_mux.sv:301](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L301) 的 `if (!w_fifo_full && aw_valid)` 开始：假设 slave 端口 1 的一个 AW 赢得了仲裁，`w_fifo_push=1`，[src/axi_mux.sv:329](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L329) 把端口号 `1` 压入 W FIFO。
2. 跳到 [src/axi_mux.sv:353](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L353)：只要这个端口号还在队头，`mst_w_chan = slv_w_chans[1]`，即 master 端口的 W 拍全部来自 slave 端口 1。
3. 跟到 [src/axi_mux.sv:365](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L365)：当 slave 端口 1 的 `w_valid` 与 master 的 `w_ready` 同高、且这拍是 `last`，`w_fifo_pop=1`，端口号 `1` 出队，队头前进到下一个 AW 的端口。
4. **需要观察的现象**：在端口号 `1` 占据队头期间，即使 slave 端口 0 也有 W 拍要发（`slv_w_valids[0]=1`），它也**拿不到** `mst_w_ready`（因为 `slv_w_readies` 只在 `[w_fifo_data]` 即 `[1]` 处被置 1）。这就是「不交错」的强制力。
5. **预期结果**：能画出「AW 端口号入队 → W 按队头转发 → last 出队」的状态迁移，并指出哪一行代码实现了「只给队头端口回 ready」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 W FIFO 的深度 `MaxWTrans` 会限制 mux 的写并发度？把它设成 1 会有什么后果？
**答案**：W FIFO 存的是「已发 AW、但 W 突发尚未送完」的端口号队列，每多一个这样的 AW 就占一个 FIFO 项。所以 `MaxWTrans` 就是允许同时「挂着未发完 W」的 AW 数，即写并发度上限。设成 1 意味着同一时刻只能有一个 AW 的 W 在排队，上一个突发的 `last` 弹队之前，新的 AW 会被 `w_fifo_full` 卡住（[src/axi_mux.sv:301](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L301)），写吞吐大幅下降。

**练习 2**：如果删掉 `lock_aw_valid_q` 这个锁存（即每拍都重新判断 push），会出什么问题？
**答案**：会出现「一个 AW 被重复 push 进 W FIFO」或「AW 推进与 W FIFO 满 之间形成组合依赖」的风险。具体说，若某拍 AW 因 `!w_fifo_full` 通过并 push，但 master 端口当拍没接住（`mst_aw_ready=0`），下一拍若无锁存就会再次判断 push——要么重复入队导致 W 路由错乱，要么因 FIFO 状态变化而进退两难。锁存确保「push 决策只做一次、之后只重发 valid 不重发 push」，这正是注释说的「removes AW to W dependency」。

---

### 4.3 响应方向：B/R 按 ID 高位 one-hot 路由

#### 4.3.1 概念说明

响应方向比请求方向简单得多。下游只有 1 个 master 端口，B 和 R 都从这一路回来，**不需要仲裁**。mux 唯一要做的，是把每个返回的响应**分发**到正确的 slave 端口。

而「正确的 slave 端口」就写在响应 ID 的高 MstIdxBits 位里——因为请求方向已经用 `axi_id_prepend` 把端口号拼了进去，下游原样回带这个扩展 ID。所以 mux 只需：

1. 从 B/R 的 ID 里取出高位 `switch_id`；
2. 用 `1 << switch_id` 生成一个 one-hot 向量，**只**把 valid 拉给第 `switch_id` 个 slave 端口；
3. 把那个端口的 ready 回授给 master 端口的 B/R ready。

这套「**复制载荷给所有端口 + one-hot 选择性拉 valid**」的手法，正是 demux 请求方向「广播载荷 + 选择性拉 valid」（u5-l1 4.2.3）的镜像——只不过 demux 用 select 选、mux 用 ID 高位解出来的 one-hot 选。

#### 4.3.2 核心流程

```
B 方向（R 方向完全对称）：
  master 端口返回 mst_b_chan（其 id 的高位 = 源 slave 端口号）
       │
       ├─ switch_b_id = mst_b_chan.id[SlvAxiIDWidth +: MstIdxBits]   // 取高位
       │
       ├─ slv_b_chans  = {NoSlvPorts{mst_b_chan}}                    // 载荷复制给所有端口
       │
       ├─ slv_b_valids = (mst_b_valid) ? (1 << switch_b_id) : '0     // ★ one-hot 只点亮一个
       │
       └─ master 的 b_ready ← slv_b_readies[switch_b_id]             // 只听目标端口的 ready
```

#### 4.3.3 源码精读

**(a) B 通道分发**

[src/axi_mux.sv:386-390](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L386-L390) —— 三行说完全部：

```systemverilog
assign slv_b_chans  = {NoSlvPorts{mst_b_chan}};                       // 载荷全复制
assign switch_b_id  = mst_b_chan.id[SlvAxiIDWidth+:MstIdxBits];       // 解码 ID 高位
assign slv_b_valids = (mst_b_valid) ? (1 << switch_b_id) : '0;        // one-hot
```

[src/axi_mux.sv:392-404](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L392-L404) —— B 通道的 spill 寄存器，注意它的 `ready_i` 接的是 `slv_b_readies[switch_b_id]`（[src/axi_mux.sv:402](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L402)）：即「只有被 one-hot 选中的那个 slave 端口的 b_ready 才会被回授给 master 端口」。载荷与 valid/ready 三者严格指向同一个端口。

**(b) R 通道分发**

[src/axi_mux.sv:445-463](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L445-L463) —— 与 B 完全同构：`switch_r_id = mst_r_chan.id[SlvAxiIDWidth+:MstIdxBits]`，`slv_r_valids = (mst_r_valid) ? (1 << switch_r_id) : '0`，spill 的 `ready_i` 接 `slv_r_readies[switch_r_id]`。R 是多拍突发，但因为同一个 R 突发的所有拍 ID 相同，`switch_r_id` 在整个突发期间稳定不变，所以 one-hot 会一直点亮同一个端口直到 `last`——天然把一个完整 R 突发送到同一个上游。

> 文档把这一整套机制总结得很到位（[doc/axi_mux.md:16](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_mux.md#L16)）：所有响应都用「看 ID 高位」的同一套方案路由。这之所以能成立，全靠请求方向的 `axi_id_prepend` 提前把端口号编码进了 ID——请求与响应两侧对称使用同一组位。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：跟踪一个 B 响应，确认它只会被「ID 高位指明的那个」slave 端口看到。

1. 假设 `NoSlvPorts=4`、`SlvAxiIDWidth=6`，下游返回一个 B，其 `id = 8'b10_100110`（高位 `2'b10`）。
2. 在 [src/axi_mux.sv:389](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L389) 算出 `switch_b_id = 2'b10 = 2`。
3. 在 [src/axi_mux.sv:390](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L390) 算出 `slv_b_valids = 4'b0100`——只有 slave 端口 2 的 `b_valid` 被点亮，端口 0/1/3 的 `b_valid` 都是 0。
4. **需要观察的现象**：[src/axi_mux.sv:387](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L387) 把 `mst_b_chan` 复制给了**所有** 4 个端口的 `slv_b_chans`，但因为只有端口 2 的 valid 为真，最终只有端口 2 会接住这个 B。载荷复制是「免费」的（同一份驱动），选择权完全落在 valid 的 one-hot 上。
5. **预期结果**：能指出「哪一行取 ID 高位、哪一行做 one-hot、哪一行只回授目标端口的 ready」。

#### 4.3.5 小练习与答案

**练习 1**：B 载荷被 `{NoSlvPorts{mst_b_chan}}` 复制给了所有端口，这会不会造成多个上游同时收到同一个 B？
**答案**：不会。虽然载荷被复制到所有端口的 `slv_b_chans`，但 `slv_b_valids` 是 one-hot——只有 `switch_b_id` 那一位为 1，其余端口的 valid 为 0。AXI 握手要求 valid 与 ready 同高才算一次传输，其余端口 valid=0，根本不会发生握手，自然收不到。

**练习 2**：一个多拍 R 突发会不会出现「前几拍送到端口 A、后几拍送到端口 B」的错乱？
**答案**：不会。同一个 R 突发的所有拍携带相同的 ID，因此 `switch_r_id` 在整个突发期间恒定，one-hot 一直点亮同一个端口，直到 `last`。所以一个完整 R 突发必然整体送到同一个 slave 端口。

**练习 3**（呼应 4.1.5 练习 2）：若 `NoSlvPorts=3` 而下游返回了一个 ID 高位为 `2'b11`（=3）的响应，`1 << 3 = 3'b1000`，但只有 3 个端口，会发生什么？
**答案**：`slv_b_valids` 会是 `3'b000`（one-hot 的第 4 位超出 `[NoSlvPorts-1:0]` 范围被截断），所有端口的 valid 都为 0，这个响应无人接手，master 端口的 B 握手会卡住（`b_ready` 拉不起来）。正常工作时不会出现这种响应——因为只有端口 0/1/2 会发出请求，下游理应只回带这三种高位的响应。这反过来说明：**保证「ID 高位 ∈ 合法端口号集合」是系统其余部分的责任**，mux 自身的 `axi_id_prepend` 已经在请求侧保证了这一点。

---

### 4.4 mux 与 demux 的对偶：xbar 中的对称角色

#### 4.4.1 概念说明

把 4.1~4.3 读完，你会发现 mux 和 demux（u5-l1）是一对严丝合缝的**对偶（dual）**：一个做 1 拆 N、一个做 N 合 1；一个请求方向选端口、响应方向仲裁，另一个请求方向仲裁、响应方向解码。把它们背靠背组合，就能搭出任意「M 个主设备 × N 个从设备」的全连接交叉开关（crossbar）。

这正是 `axi_xbar` 的构造方式（u6-l1 会专门讲，这里先建立直觉）：

- **每 slave 端口一个 demux**：把进来的请求按地址译码分发到 N 个 master 端口方向（`axi_xbar_unmuxed`，即「demux 阵列」）。
- **每 master 端口一个 mux**：把来自 M 个 slave 端口方向、要去同一个下游 slave 的请求汇聚成一条 master 端口（「mux 阵列」）。

于是请求方向走 `demux → mux`，响应方向走 `mux → demux`。两个阵列之间是一个 `NoMstPorts × NoSlvPorts` 的内部连接矩阵。

#### 4.4.2 核心流程：mux vs demux 对照表

| 维度 | axi_demux（u5-l1） | axi_mux（本讲） |
| --- | --- | --- |
| 方向 | 1 slave → N master（1 拆 N） | N slave → 1 master（N 合 1） |
| 请求方向（AW/AR）路由依据 | 外部 `select` 信号 | `rr_arb_tree` 轮询仲裁 |
| 请求方向是否改 ID | 不改 | **改**：`axi_id_prepend` 把端口号拼进 ID 高位 |
| W 通道路由 | `w_open` 计数器 + `w_select_q`（单源） | W FIFO（N 源，按 AW 顺序存端口号） |
| 响应方向（B/R）合并/分发 | `rr_arb_tree` 仲裁合并 N→1 | **解码 ID 高位 → one-hot 分发** 1→N |
| 同 ID 保序机制 | 需要 `axi_demux_id_counters`（O(2^I)） | **不需要**（ID 高位已隔离各端口） |
| ATOP 支持 | 靠向 AR 计数器 inject 原子 ID | 天然支持（ID 隔离使保序责任下放） |
| 退化情形优化 | `NoMstPorts==1` 纯直通 | `NoSlvPorts==1` 纯直通（spill only） |
| 默认 spill | AW/AR 开 | AW/AR 开 |

最后一行值得强调：mux 和 demux 都默认在 AW/AR 上插 spill、W/B/R 不插，原因是这两个地址通道的组合路径最深（mux 的 AW 要经过 `rr_arb_tree` + W FIFO 满判断 + lock 逻辑；demux 的 AW 要经过 ID 计数器 lookup + W 队列判断）。

#### 4.4.3 源码精读：mux 在 xbar 里的真实接法

[src/axi_xbar.sv:122-155](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L122-L155) —— `gen_mst_port_mux` 循环：对**每个 master 端口**（共 `Cfg.NoMstPorts` 个）例化一个 `axi_mux`。关键参数映射：

- `NoSlvPorts = Cfg.NoSlvPorts`：每个 mux 把来自所有 slave 端口方向的请求汇聚（即「所有主设备都可能访问这个下游 slave」）。
- `SlvAxiIDWidth = Cfg.AxiIdWidthSlvPorts`：xbar 对外的 slave 端口 ID 宽度。
- `MaxWTrans = Cfg.MaxSlvTrans`：写并发度上限从 xbar 配置透传。
- 五个 spill 开关由 `Cfg.LatencyMode` 的各个位驱动（[src/axi_xbar.sv:141-145](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L141-L145)）：

```systemverilog
.SpillAw ( Cfg.LatencyMode[4] ),   // = axi_pkg::MuxAw
.SpillW  ( Cfg.LatencyMode[3] ),   // = axi_pkg::MuxW
.SpillB  ( Cfg.LatencyMode[2] ),   // = axi_pkg::MuxB
.SpillAr ( Cfg.LatencyMode[1] ),   // = axi_pkg::MuxAr
.SpillR  ( Cfg.LatencyMode[0] ),   // = axi_pkg::MuxR
```

这五个 one-hot 常量定义在 `axi_pkg` 里（[src/axi_pkg.sv:461-469](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L461-L469)），并可组合成 `CUT_MST_AX`（= `MuxAw | MuxAr`）、`CUT_MST_PORTS`（= 五个 mux spill 全开）等预设（[src/axi_pkg.sv:474-477](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L474-L477)）。文档推荐整片 xbar 用 `CUT_ALL_AX`（demux 与 mux 的 AW/AR 都切），u6 以后会详述。

再看 xbar 顶层注释 [src/axi_xbar.sv:93-95](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L93-L95)：

```systemverilog
// signals into the axi_muxes, are of type slave as the multiplexer extends the ID
slv_req_t  [Cfg.NoMstPorts-1:0][Cfg.NoSlvPorts-1:0] mst_reqs;
```

这句注释「**as the multiplexer extends the ID**」一语点破 4.1 的核心：进入 mux 之前的内部信号是 slave 宽度 ID，mux 在内部把它扩展成 master 宽度 ID。整个 xbar 的「master 端口 ID 比 slave 端口宽 ⌈log2(NoSlvPorts)⌉ 位」这条规律，就源自这一层 mux。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：在 xbar 源码里数清「几个 demux、几个 mux」，建立 xbar = demux 阵列 + mux 阵列的直觉。

1. 打开 [src/axi_xbar.sv:97-120](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L97-L120)，这是 `axi_xbar_unmuxed`（demux 阵列）的例化——它在内部对**每个 slave 端口**配一个 demux。所以 demux 的数量 = `Cfg.NoSlvPorts`。
2. 打开 [src/axi_xbar.sv:122-155](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L122-L155)，确认 mux 的数量 = `Cfg.NoMstPorts`（循环上界）。
3. **需要观察的现象**：内部连接矩阵 `mst_reqs[NoMstPorts][NoSlvPorts]`（[src/axi_xbar.sv:94](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L94)）正是 demux 阵列（第一维由 demux 输出索引）到 mux 阵列（每个 mux 吃掉 `NoSlvPorts` 路）的交汇点。
4. **预期结果**：能口头复述「一个 `NoSlvPorts=S`、`NoMstPorts=M` 的 xbar 内部有 S 个 demux 和 M 个 mux，它们之间是一个 S×M 的连接矩阵」。
5. **待本地验证**：若手头能综合，可对 `2×2` 与 `4×4` 两种 xbar 配置跑 `scripts/synth.sh` 的 elaborate，对比面积——面积应大致随 `S×M` 增长。

#### 4.4.5 小练习与答案

**练习 1**：为什么 xbar 里「响应方向」不需要额外的路由逻辑，只要让响应穿过 mux 和 demux 即可？
**答案**：因为 mux 和 demux 各自已经内置了响应路由能力，且方向相反、刚好接力。响应从下游 slave 回来，先进 mux：mux 用 ID 高位 one-hot 把它分发给对应的 slave 端口方向；再进 demux：demux 用 `rr_arb_tree` 把多路响应合并回发起请求的那个 xbar slave 端口。两层叠加，响应自然回到正确的主设备，无需 xbar 顶层再加逻辑。

**练习 2**：xbar 顶层注释说「进入 mux 的信号是 slave 类型，因为 mux 会扩展 ID」。这句话和 4.1 的 ID 宽度公式如何对应？
**答案**：xbar 对外暴露的 slave 端口 ID 宽度是 `Cfg.AxiIdWidthSlvPorts`，这也是 demux 阵列输出、送进 mux 的信号宽度（即 mux 的 `SlvAxiIDWidth`）。mux 在内部通过 `axi_id_prepend` 把它扩展成 `AxiIdWidthSlvPorts + $clog2(NoSlvPorts)` 位（mux 的 `MstAxiIDWidth`），也就是 xbar 对外 master 端口的 ID 宽度。所以「信号进 mux 前是 slave 宽度、出 mux 后是更宽的 master 宽度」完全对应 4.1 的派生公式。

---

## 5. 综合实践

**任务**：用 `axi_mux_intf` 搭一个 **2 汇 1** 的复用器，上游接两个随机主设备，下游接一个忠实存储，验证两个上游的响应能按扩展后的 ID 正确回到各自端口。

**设计思路**（模仿 `axi_xbar` 里 `gen_mst_port_mux` 的结构，只取其中一个 mux）：

- 选 `NO_SLV_PORTS = 2`，于是 `MstIdxBits = $clog2(2) = 1`，master 端口 ID 宽度 = slave 端口 ID 宽度 + 1。
- 上游两个 `AXI_BUS` slave 端口各接一个 `axi_rand_master`（u3-l2），它们的 ID 宽度是 `SLV_AXI_ID_WIDTH`。mux 内部会自动给端口 0 的请求 ID 拼上 `1'b0`、端口 1 拼上 `1'b1`。
- 下游一个 master 端口接一个 `axi_sim_mem`（u3-l2），其 ID 宽度是 `MST_AXI_ID_WIDTH`。
- 用两个 `axi_scoreboard`（u3-l2，每个上游一个）做自检，确认各自发出的请求都收到了正确的响应。

**示例代码**（这是讲义为说明结构而写的示意 testbench 片段，**不是仓库已有文件**，标注为「示例代码」）：

```systemverilog
// ===== 示例代码：tb_mux_2to1.sv（片段，仅示意结构）=====
  // 时序三参数（沿用 u3-l3 的约定：0 < TA < TT < T_clk）
  localparam time CyclTime = 10ns;
  localparam time ApplTime = 2ns;
  localparam time TestTime = 8ns;

  localparam int unsigned NO_SLV_PORTS    = 2;
  localparam int unsigned SLV_AXI_ID_WIDTH = 6;
  localparam int unsigned MST_AXI_ID_WIDTH = SLV_AXI_ID_WIDTH + $clog2(NO_SLV_PORTS); // = 7
  localparam int unsigned AXI_ADDR_WIDTH   = 32;
  localparam int unsigned AXI_DATA_WIDTH   = 64;
  localparam int unsigned AXI_USER_WIDTH   = 0;

  // 两个上游 slave 端口（接 rand_master），一个下游 master 端口（接 sim_mem）
  AXI_BUS #(.AXI_ID_WIDTH(SLV_AXI_ID_WIDTH), .AXI_ADDR_WIDTH(AXI_ADDR_WIDTH),
            .AXI_DATA_WIDTH(AXI_DATA_WIDTH), .AXI_USER_WIDTH(AXI_USER_WIDTH)) slv[1:0]();
  AXI_BUS #(.AXI_ID_WIDTH(MST_AXI_ID_WIDTH), .AXI_ADDR_WIDTH(AXI_ADDR_WIDTH),
            .AXI_DATA_WIDTH(AXI_DATA_WIDTH), .AXI_USER_WIDTH(AXI_USER_WIDTH)) mst();

  // ★ mux：内部自动把端口号拼进 ID 高位，故两侧 ID 宽度不同
  axi_mux_intf #(
    .SLV_AXI_ID_WIDTH( SLV_AXI_ID_WIDTH ),
    .MST_AXI_ID_WIDTH( MST_AXI_ID_WIDTH ),
    .AXI_ADDR_WIDTH  ( AXI_ADDR_WIDTH   ),
    .AXI_DATA_WIDTH  ( AXI_DATA_WIDTH   ),
    .AXI_USER_WIDTH  ( AXI_USER_WIDTH   ),
    .NO_SLV_PORTS    ( NO_SLV_PORTS     )
    // MAX_W_TRANS / FALL_THROUGH / SPILL_* 用默认值
  ) i_mux (
    .clk_i, .rst_ni, .test_i,
    .slv    ( slv ),   // 两个 slave 端口
    .mst    ( mst )    // 一个 master 端口
  );

  // 上游：两个 rand_master 分别驱动 slv[0]、slv[1]；
  // 下游：一个 axi_sim_mem 驱动 mst（ID 宽度 MST_AXI_ID_WIDTH）；
  // 自检：两个 scoreboard 分别挂在 slv[0]、slv[1] 的 monitor 侧（接线略，参见 test/tb_axi_xbar.sv）
```

**操作步骤**：

1. 按上面的结构补全两个 `axi_rand_master`、一个 `axi_sim_mem`、两个 `axi_scoreboard` 的例化与接线。**最直接的参考是 `test/tb_axi_xbar.sv`**——它用同样的组件验证了一个完整 xbar（其中就包含 mux），你可以把它「裁剪」成只有一个 mux 的版本。
2. 让两个 rand_master 在同一地址空间内并发发随机读写，制造「两个上游同时争抢同一条下游总线」的场景。
3. 仿真运行若干事务后检查。

**需要观察的现象 / 预期结果**：

- 下游 `axi_sim_mem` 收到的所有请求 ID，其最高位要么是 `0`（来自 slv[0]）要么是 `1`（来自 slv[1]）——这正是 `axi_id_prepend` 拼上去的端口号。
- 每个响应返回时，mux 解码其 ID 最高位，把 B/R 只送给对应的上游：最高位 0 的响应回 slv[0]、最高位 1 的回 slv[1]。
- 两个 `axi_scoreboard` 全程各自无 mismatch——证明响应各自正确回到发起请求的那个主设备，**没有串台**。
- 日志出现 `Errors: 0,`（u1-l4 讲过的判据）。
- 因 `axi_mux_intf` 默认 `SPILL_AW=1/SPILL_AR=1`，AW/AR 各有一拍延迟；如想观察纯组合行为，可把 `SPILL_AW/SPILL_AR` 显式置 0 再跑一次对比。

**待本地验证**：本讲无法替你跑仿真，实际「两个上游的响应按 ID 正确回到各自端口」需你在本地用 `scripts/run_vsim.sh` 跑 `tb_mux_2to1` 验证。若暂时没有仿真器，也可降级为**源码阅读型验证**：把上面 `MST_AXI_ID_WIDTH = SLV_AXI_ID_WIDTH + $clog2(NO_SLV_PORTS)` 与 [src/axi_mux.sv:68-69](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L68-L69) 对照，再与 [src/axi_mux.sv:389-390](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L389-L390) 的 one-hot 分发对照，手工论证「ID 最高位为 0 的响应只会点亮 slv[0] 的 valid」。

## 6. 本讲小结

- `axi_mux` 做 **N 合 1**：把 `NoSlvPorts` 个 slave 端口汇聚成 1 个 master 端口，是 demux 的对偶模块。
- **核心机制是 ID 扩展路由**：每个 slave 端口 `i` 用 `axi_id_prepend` 把端口号拼进 AXI ID 的最高 `MstIdxBits = $clog2(NoSlvPorts)` 位，于是 master 端口 ID 宽度 = slave 端口 ID 宽度 + MstIdxBits。
- **请求方向**：AW/AR 各用一棵 `rr_arb_tree`（`LockIn=1`）轮询挑一路；W 没有仲裁器，用一个深 `MaxWTrans` 的 W FIFO 按队头端口号转发 W 拍，在 `last` 拍弹队，**杜绝跨端口 W 交错**。
- **`lock_aw_valid` 锁存**切断 AW 推进与 W FIFO 状态的组合依赖（「removes AW to W dependency」），是请求方向防死锁的关键一笔。
- **响应方向**：B/R 不需仲裁，只解码 ID 高位得到 `switch_id`，用 `1 << switch_id` 的 one-hot **只**把 valid 拉给目标 slave 端口；载荷复制给所有端口，但只有目标端口会发生握手。
- 因为 ID 高位天然隔离了各端口的主设备，mux **完全不需要** demux 那样的同 ID 在途计数器，这是它比 demux 简单的根本原因，也是「xbar 的保序责任下放给各主设备」的体现。
- 在 `axi_xbar` 里，**每个 master 端口配一个 mux**，与「每个 slave 端口配一个 demux」对称，二者通过一个 `NoMstPorts × NoSlvPorts` 的内部矩阵相连；xbar「master 端口 ID 更宽」正是由这层 mux 的 ID 扩展造成。
- 默认 spill 策略与 demux 一致：AW/AR 开（切组合路径）、W/B/R 关；`NoSlvPorts==1` 时退化成五通道纯直通（仅 spill）。

## 7. 下一步学习建议

- **u6-l1** 把 demux（u5-l1）+ 本讲的 mux 组合成 `axi_xbar` 全连接交叉开关，届时你会再次看到本讲 4.4 的 `gen_mst_port_mux` 调用链，并理解 `Cfg.LatencyMode` 如何统一控制两侧 spill。
- **u6-l3** 会深入 `doc/axi_xbar.md` 的 *Ordering and Stalls*，解释「为何 demux 与 mux 之间不能插 spill/FIFO 寄存器」（W 通道死锁四条件）——本讲的 `lock_aw_valid` 只是 mux **内部**的防死锁，xbar 层面还有更深层的保序/死锁边界要讨论。
- 想加深对 ID 扩展的理解，可重读 **u4-l2** 的 `axi_id_prepend` 精读，把它与本讲 4.1.3 的 `gen_id_prepend` 循环对照。
- 若你对「ID 宽度不匹配时怎么办」感兴趣，**u10-l1**（`axi_id_remap`）与 **u10-l3**（`axi_iw_converter`）会讲如何在两端 ID 宽度无法直接拼接时做转换，那是比 mux 的「无脑拼端口号」更复杂的场景。
