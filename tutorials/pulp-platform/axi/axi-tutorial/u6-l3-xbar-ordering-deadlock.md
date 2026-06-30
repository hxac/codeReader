# 排序、停顿与无死锁设计

## 1. 本讲目标

本讲聚焦 `axi_xbar` 内部最容易被忽视、却决定整个互联「能不能用」的两个性质：**事务保序** 与 **无死锁**。读完本讲你应当能够：

- 说清 AXI「同 ID 同方向必须保序」这一约束，为什么会在交叉开关里被翻译成 **slave 端口停顿**；
- 解释 `AxiIdUsedSlvPorts`（即 demux 层的 `AxiLookBits`）如何在 **面积/延迟** 与 **误冲突（false conflict）** 之间折中；
- 复述 Coffman 死锁四条件，并论证为什么 **在 demux 输出与 mux 输入之间插入 spill 寄存器** 会同时满足四条件、从而在 W 通道引发死锁；
- 知道哪些旋钮（`FallThrough`、`lock_aw_valid`、`LatencyMode`）是**可以安全调整**的，哪些是**绝对不能动**的。

本讲不重复 u6-l1（xbar 怎么由 demux + mux 拼成）、u5-l2（id_counters 怎么计数）和 u5-l3（mux 怎么用 ID 高位路由），而是在它们之上回答「这些机制为什么这么设计、边界在哪里」。

## 2. 前置知识

- **in flight（在途）**：某接口上，事务的 `Ax` 地址拍已握手，但（最后一拍）响应尚未握手。详见 [doc/README.md:L26-L28](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/README.md#L26-L28)。
- **pending（挂起）**：某通道上 `valid` 为高而 `ready` 为低——一次「尚未发生的握手」。详见 [doc/README.md:L30-L32](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/README.md#L30-L32)。
- **保序（ordering）**：AXI 规定，同一个 master 发出的、**ID 相同且方向相同**（同为读或同为写）的事务，其响应必须按发起顺序返回给该 master。
- **reorder buffer（重排序缓冲）**：把乱序回来的响应按序重新排好再送出的硬件。本库的 xbar **没有**它，这是本讲一切取舍的源头。
- **Coffman 四条件**：操作系统课本里判定死锁的经典四条——互斥（Mutual Exclusion）、占有并等待（Hold and Wait）、不可抢占（No Preemption）、循环等待（Circular Wait）。四条同时成立 ⟹ 死锁。
- **spill 寄存器**：来自 `common_cells` 的原语，切断一条通道的组合路径，代价是一拍延迟。见 u4-l1 / u7-l1。

## 3. 本讲源码地图

| 文件 | 作用 |
|:--|:--|
| [doc/axi_xbar.md](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md) | xbar 的权威说明，含 *Ordering and Stalls*（保序与停顿）与 *Design Rationale for No Pipelining Inside Crossbar*（为何内部不流水）两节，是本讲的主线。 |
| [src/axi_demux.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux.sv) | 1 拆 N 的路由器外壳，内含可配置的 spill 寄存器与对 `axi_demux_simple` 的例化。 |
| [src/axi_demux_simple.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv) | demux 的组合内核，AW/AR 的「停顿门」、W 突发队列、id_counters 例化都在这里——本讲引用它来精确定位停顿条件。 |
| [src/axi_mux.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv) | N 合 1 的汇聚器，W FIFO、`rr_arb_tree` 仲裁器、`lock_aw_valid` 锁存都在这里——是分析 W 通道死锁的现场。 |
| [doc/axi_demux.md](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_demux.md) | demux 文档，补充 `UniqueIds` 的面积复杂度结论 `O(2^I) → O(I)`。 |

## 4. 核心概念与源码讲解

### 4.1 同 ID 同方向保序：为什么会产生停顿

#### 4.1.1 概念说明

AXI 的排序模型规定：对同一个 master，**ID 相同、方向相同**（都读或都写）的事务，其响应必须按发起顺序返回。这是软件能看到的最强排序保证之一——例如 CPU 发了两条 `ID=5` 的读，它假定先发的那条数据先到。

现在把这个约束放进交叉开关：一个 slave 端口先后收到两个事务 `T1`、`T2`，二者 **ID 相同、方向相同**，但地址译码后要去往 **两个不同的 master 端口**（即两个物理上不同的下游 slave）。一旦 `T1`、`T2` 都放行，它们就进入了两个独立slave 的队列，谁先返回响应完全不可控——`T2` 的下游可能比 `T1` 的快，于是 `T2` 的响应先回到 slave 端口，**破坏了保序**。

修复办法只有两种：

1. **带 reorder buffer**：放行两个事务，等响应回来后重排。正确但贵。
2. **不带 reorder buffer**：干脆不让 `T2` 进来——`T1` 没完成前，停顿 `T2` 所在的 AW/AR 通道。

本库选了第 2 条，理由写在文档里：出于效率/面积考虑，**这个交叉开关没有 reorder buffer**。于是「保序」就被翻译成了「同 ID 跨端口的停顿」。

> 常见误解：停顿不是因为带宽不够，也不是因为 FIFO 满，而是 **纯粹为了满足 AXI 排序模型**。停顿期间下游可能完全空闲。

#### 4.1.2 核心流程

slave 端口收到一个新的 AW（或 AR）时，demux 的决策流程：

```text
新请求 (id=I, 方向=D, 目标端口=P_new)
   │
   ├─ 用 id_counters 查：ID=I、方向=D 当前是否已有在途事务？
   │     ├─ 否  ─► 放行，计数器 +1，登记端口 P_new
   │     └─ 是  ─► 在途事务登记的端口 P_old 是否 == P_new？
   │                ├─ 是 ─► 放行（同端口，天然保序，不破坏顺序）
   │                └─ 否 ─► 停顿：不拉 aw_ready/ar_ready，死等 P_old 那笔完成
   │
   └─ 待在途事务响应握手 ─► 计数器 -1；若减到 0，解除对 ID=I 的占用
```

关键点：**同 ID 去往同一端口不会停顿**（因为它们走同一条物理路径，下游自己就会保序）；**只有同 ID 去往不同端口才会停顿**。这就是 id_counters 必须记录「端口」而不仅仅是「计数」的原因。

#### 4.1.3 源码精读

文档把这条规则写得很直接（中文旁注：同 ID 同方向、不同 master 端口 ⟹ 第二笔要等第一笔完成，期间停顿 AW/AR；原因是库无 reorder buffer）：

> [doc/axi_xbar.md:L82-L87](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L82-L87) —— *Ordering and Stalls*：同 ID 同方向但去往不同 master 端口时，第二笔事务在前一笔完成前不被接收，期间 crossbar 停顿该 slave 端口的 AR/AW；原因是本 crossbar 没有 reorder buffer。

停顿在 RTL 里就是一个 `always_comb` 里的放行条件。AW 方向的放行门（中文旁注：第三行就是排序停顿门——ID 被占用且目标端口与在途端口不同则不进入 `if`，于是 `aw_valid` 保持 0、`aw_ready` 保持 0，即停顿）：

```systemverilog
// src/axi_demux_simple.sv:175-177  AW 放行条件
if (slv_req_i.aw_valid &&
      ((w_open == '0) || (w_select == slv_aw_select_i)) &&
      (!aw_select_occupied || (slv_aw_select_i == lookup_aw_select))) begin
```

- `aw_select_occupied`：ID 计数器报告「该 ID 已有在途写事务」；
- `lookup_aw_select`：该在途事务登记的目标端口；
- 仅当「未被占用」或「目标端口与在途端口一致」时才放行——这正是「同 ID 跨端口才停顿」的字面实现。

AR 方向有一个对称的、更简洁的门（中文旁注：读方向同样的停顿逻辑）：

```systemverilog
// src/axi_demux_simple.sv:325-326  AR 放行条件
if (slv_req_i.ar_valid && (!ar_select_occupied ||
   (slv_ar_select_i == lookup_ar_select))) begin
```

这套占用信息来自哪里？来自 u5-l2 讲过的 `axi_demux_id_counters`，AW/AR 各例化一份（中文旁注：AW 握手时 push、B 握手时 pop；push 时把目标端口 `slv_aw_select_i` 登记进计数器）：

> [src/axi_demux_simple.sv:L210-L229](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L210-L229) —— `i_aw_id_counter` 例化：`lookup_*` 查占用、`push_*` 登记、`pop_*` 回收。

> ⚠️ 注意区分：`w_open` / `w_select`（第 175 行第二行）管的是 **W 通道的突发队列**，避免不同端口的 W 拍交错（见 4.3）；它和 id_counters 的排序停顿是**两套独立机制**，不要混淆。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：在脑子里跑一次「同 ID 跨端口停顿」的场景，验证你对停顿触发条件的理解。
2. **步骤**：
   - 假设一个 2 master 端口的 demux，`slv_aw_select_i` 由地址最高位决定（地址 `< 0x8000_0000` 选端口 0，否则选端口 1）。
   - 设想 slave 端口按顺序到来两笔写：`T1 = (id=3, addr=0x0000)`、`T2 = (id=3, addr=0x9000_0000)`。注意 **ID 都是 3，但分别去端口 0 和端口 1**。
   - 打开 [src/axi_demux_simple.sv:L138-L194](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L138-L194) 的 AW `always_comb`，逐步代入 `T2` 时刻的状态：此时 `T1` 在途，`aw_select_occupied=1`、`lookup_aw_select=0`，而 `slv_aw_select_i=1`。
3. **需要观察的现象**：第 177 行的条件 `(!aw_select_occupied || (slv_aw_select_i == lookup_aw_select))` 求值为 `(!1 || (1==0)) = 0`，整个 `if` 不进入，`aw_valid` 与 `slv_resp_o.aw_ready` 都保持 0。
4. **预期结果**：`T2` 被钉在 slave 端口直到 `T1` 的 B 响应握手（pop 掉计数器、`aw_select_occupied` 归 0）。这正是「同 ID 跨端口停顿」。
5. 若要眼见为实，可在 `test/tb_axi_xbar.sv` 里用定向激励复现该场景并加 `$display` 观察 `aw_ready` 被压低若干拍——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `T2` 的 ID 改成 4（与 `T1` 不同），还会停顿吗？
**答**：不会。不同 ID 之间没有保序约束，id_counters 查 `id=4` 发现未被占用，直接放行；两笔事务可在两个端口并发在途。

**练习 2**：假设 `T1`、`T2` 同 ID 且都去端口 0（地址都在低半区），会停顿吗？
**答**：不会。第 177 行 `slv_aw_select_i == lookup_aw_select` 成立（都是 0），放行；二者沿同一物理路径到同一下游，由下游自身保序。

**练习 3**：为什么本库宁可引入「跨端口停顿」也不加 reorder buffer？
**答**：reorder buffer 需要为每个在途 ID 缓存响应、维护顺序指针，面积与延迟随 ID 宽度和并发度爆炸；而「停顿」只需一组计数器，零额外存储。停顿只在「同 ID 跨端口」这种相对少见的模式下才发生，综合代价低得多。

---

### 4.2 AxiIdUsedSlvPorts：面积与误冲突的折中

#### 4.2.1 概念说明

4.1 用 `id_counters` 判同 ID，但「ID 宽度」可能很宽（例如 8 位甚至更多）。如果对**全宽度**建计数器，每方向需要 \(2^{I}\) 个计数器（\(I\) 为 ID 位宽），面积随 ID 宽度指数增长——通常不可接受。

本库的做法是：**只比较 ID 的最低 N 位**。在 xbar 层这个 N 叫 `AxiIdUsedSlvPorts`，在 demux 层叫 `AxiLookBits`（同一个东西，只是所处层级不同）。计数器数量降为每方向 \(2^{N}\) 个：

\[ \text{每方向计数器数} = 2^{N}, \quad N = \text{AxiIdUsedSlvPorts} = \text{AxiLookBits} \]

代价是 **误冲突（false conflict / aliasing）**：两个事务的完整 ID 不同，但最低 N 位恰好相同，硬件就把它们误判为「同 ID」，触发本不该有的停顿。注意：误冲突**只损失吞吐、绝不破坏正确性**——把两个本可并发的事务串行化，结果依然正确，只是慢。

于是 `AxiIdUsedSlvPorts` 成了一个可调旋钮：

- **N = 全宽度**：零误冲突，但面积 \(O(2^{I})\)，通常太贵；
- **N 较小**：面积/延迟小，但误冲突多、吞吐有损；
- **`UniqueIds = 1`**：若能保证「同方向在途 ID 唯一」或「同 ID 必去同端口」，则**整组计数器可删除**，面积从 \(O(2^{I})\) 降到 \(O(I)\)。

#### 4.2.2 核心流程

| 取值 | 计数器面积 | 误冲突 | 适用场景 |
|:--|:--|:--|:--|
| `N = AxiIdWidthSlvPorts`（满宽） | \(2^{I}\) | 无 | ID 空间利用率高、对延迟敏感 |
| `0 < N < I`（典型 3） | \(2^{N}\) | 有，与 ID 使用模式相关 | 大多数通用场景的默认折中 |
| `UniqueIds=1` | \(O(I)\) | 无（前提成立时） | 上游保证 ID 唯一（如经 id_remap 后） |

折中本质：用「偶尔多停顿一下」换「省一片计数器存储」。由于误冲突只在同方向且低位碰撞时偶发，对整体吞吐的影响通常远小于省下的面积。

#### 4.2.3 源码精读

xbar 配置结构体里该字段的定义（中文旁注：取 ID 低位若干比特用于判唯一性，必须 ≤ `AxiIdWidthSlvPorts`）：

> [doc/axi_xbar.md:L51](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L51) —— `AxiIdUsedSlvPorts`：crossbar 用以判定 ID 唯一性的 slave 端口 ID 低位比特数。

demux 文档把同样的折中讲得更直白（中文旁注：满宽避免误冲突、降位省面积但增误冲突），并给出面积结论：

> [doc/axi_demux.md:L56](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_demux.md#L56) —— *Ordering and Stalls*：比较 `AxiLookBits` 个最低位；设为满宽可消除误冲突，设小可省面积/延迟但增加误冲突。

> [doc/axi_demux.md:L68](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_demux.md#L68) —— 置 `UniqueIds=1` 把面积复杂度从 \(O(2^{I})\) 降到 \(O(I)\)。

RTL 里「只取低位」的体现：id_counters 例化时 ID 输入用截位 `aw.id[0+:AxiLookBits]`（中文旁注：push、lookup、pop 全部只看低 `AxiLookBits` 位）：

> [src/axi_demux_simple.sv:L217-L227](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L217-L227) —— `lookup_axi_id_i`、`push_axi_id_i`、`pop_axi_id_i` 全部接 `aw.id[0+:AxiLookBits]`，高位被丢弃，这就是误冲突的物理来源。

`UniqueIds=1` 时直接绕过计数器（中文旁注：把占用/满标志硬接 0，整组阵列被编译优化掉）：

> [src/axi_demux_simple.sv:L200-L208](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L200-L208) —— `gen_unique_ids_aw`：`aw_select_occupied=1'b0; aw_id_cnt_full=1'b0;`，第 177 行的停顿门恒为放行。

#### 4.2.4 代码实践（参数对比型）

1. **目标**：体会「N 越小、误冲突越多」的定量关系。
2. **步骤**：
   - 设想 ID 宽度为 8 位，同时有 16 笔在途写事务，ID 分别为 `0x00, 0x10, 0x20, …, 0xF0`（高位各不相同）。
   - 分别取 `AxiLookBits = 8`（满宽）和 `AxiLookBits = 4`。
   - 在 `AxiLookBits = 4` 下，这 16 个 ID 的低 4 位都是 `0x0`，互相视为「同 ID」。
3. **需要观察的现象**：满宽时 16 笔可在 16 个端口/周期内并发；`N=4` 时它们被当作同一个 ID，两两之间只要跨端口就停顿，吞吐骤降。
4. **预期结果**：低 `N` 在「ID 高位分散、低位集中」的工作负载下会显著退化；这正是参数选择的依据——**需要了解上游 ID 的分布**。
5. 用 `test/tb_axi_xbar.sv` 的 `rand_master` 改 ID 宽度与并发做对照回归可量化该影响——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`AxiIdUsedSlvPorts = 0` 会怎样？
**答**：所有 ID 的低 0 位都相等（恒为「同 ID」），任何两个跨端口的同方向事务都会停顿——退化为几乎串行。库一般要求该值 ≥ 1 且 ≤ `AxiIdWidthSlvPorts`。

**练习 2**：误冲突会破坏数据正确性吗？为什么？
**答**：不会。误冲突只是把「本可并发」误判为「需保序」从而多停顿；响应顺序仍被严格保持，写入/读出的数据完全正确，只是吞吐降低。

**练习 3**：什么前提下可以安全地置 `UniqueIds=1`？
**答**：当且仅当满足以下之一（见 [doc/axi_demux.md:L60-L64](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_demux.md#L60-L64)）：(a) 每笔事务的 ID 在同方向所有在途事务中唯一；(b) 对任意 ID，所有同 ID 同方向事务都去往同一 master 端口；(c) 两者兼具。否则行为未定义。

---

### 4.3 W 通道四条件：为什么 demux 与 mux 之间不能插寄存器

这是本讲的核心，也是文档 *Design Rationale for No Pipelining Inside Crossbar* 的全部内容。

#### 4.3.1 概念说明

时序紧张时，工程师的本能是在 demux 输出和 mux 输入之间插一级寄存器（或 FIFO）来切组合路径。文档明确警告：**这会引发 W 通道死锁**。

要理解为什么，先把 AXI 的 W 通道特殊性记住：

- W 拍 **必须** 按对应 AW 的顺序到达 slave 端口，**不同 W 突发之间不允许交错**（这是 AXI 硬性规定）；
- mux 在 W 通道上是一个「多选一」：任一时刻只能转发一个 slave 端口来的 W 突发——**互斥**；
- AXI 规定 `valid` 一旦拉高在握手前不可撤——**不可抢占**。

把 Coffman 死锁四条件逐条对照 W 通道：

| Coffman 条件 | 在 W 通道的体现 | 是否可打破 |
|:--|:--|:--|
| ① 互斥 (Mutual Exclusion) | mux 的 W 多选一：不同 master 端口的 W 突发互相排斥，顺序由 AW 仲裁树决定 | 否（AXI W 不可交错） |
| ② 占有并等待 (Hold and Wait) | mux 占着自己的 W FIFO（持有仲裁决定）等待下游 `w_ready`；AXI 要求 `valid` 不撤 | 否（AXI 协议铁律） |
| ③ 不可抢占 (No Preemption) | 不能把已开始的 W 突发让给别人，W 必须按 AW 顺序 | 否（AXI 协议铁律） |
| ④ 循环等待 (Circular Wait) | demux→mux 之间一旦插入寄存器，多个 mux 的 W FIFO 之间可能形成等待环 | **是——唯一可下手处** |

前三条都是 AXI 协议本身决定的、不可改变；**唯一能避免死锁的就是确保第④条「循环等待」永不成立**。而插入 spill 寄存器恰恰会制造循环等待，所以禁止。

#### 4.3.2 核心流程

为什么寄存器会制造循环等待？关键在 mux 的 AW 仲裁树 `rr_arb_tree` 的**优先级推进方式**：

```text
rr_arb_tree 工作方式：
  - 给每个输入一个优先级，成对比较向下选出赢家；
  - 赢家被转发后，优先级状态「向前推进一位」（防止饿死）；
  - 推进仅一位 ⟹ 上一周期的赢家所在输入，本周仍可能再次胜出。
```

文档举的例子：一个 10 输入的仲裁树，同一周期有两个请求；优先级高的赢、状态推进一位。下一周期同样的两个请求还在，**由于优先级只推进了一位，上一轮的赢家可能再次赢**。当多个 mux 的仲裁树都以这种方式各自推进，且 demux 与 mux 之间隔了一级寄存器（FIFO），就会出现：mux A 的 W FIFO 等着 mux B 释放某笔、mux B 的 W FIFO 又等着 mux A——**FIFO 内部形成环**。

文档给的解药（也是本库的实际做法）：**删掉 demux 与 mux 之间的寄存器**，让「切换决定」在**同一个时钟周期**内进入 mux 的 W FIFO。这样所有 mux 的切换决定被强制串行化，循环等待无从产生，第④条被打破，死锁消失。

> 一句话总结：**前三条是协议给的、改不了；只能靠「不插内部寄存器」来保证第④条永远不成立。** 这就是为什么 xbar 内部 demux/mux 之间永远是纯组合连线，而把 spill 寄存器只放在 xbar 的**对外**边界（由 `LatencyMode` 控制）。

#### 4.3.3 源码精读

文档的完整论证（中文旁注：在 demux 与 mux 之间插 spill 寄存器看似能切路径，却会在 W 通道引起两个 mux 对两个 demux 的循环等待；四条件全部成立）：

> [doc/axi_xbar.md:L94-L102](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L94-L102) —— *Design Rationale for No Pipelining Inside Crossbar*：列出 Coffman 四条件并指出前三条由 AXI/mux 性质决定。

四条件各自的归属（中文旁注：①互斥来自 mux 在 W 上的多选一与 AW 仲裁顺序；②占有来自 `valid` 必须保持到 `ready`；③不可抢占来自 AXI 禁止 W 交错、要求 W 与 AW 同序）：

> [doc/axi_xbar.md:L103-L109](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L103-L109) —— ④循环等待是唯一可打破者；插寄存器会让 W FIFO 形成环；移除寄存器把切换决定强制同周期进入 FIFO，从而严格排序、避免循环等待。

对应到 RTL，mux 在 W 通道确实是一个「按 W FIFO 队头选源」的多选一，且 W FIFO 存的就是 AW 仲裁的赢家端口（中文旁注：W FIFO 推入的是 `mst_aw_chan.id` 的高位，即被 prepend 的源端口号；W 拍按队头选择转发源，`last` 拍才 pop）：

> [src/axi_mux.sv:L317-L333](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L317-L333) —— `i_w_fifo`：`fifo_v3`，深度 `MaxWTrans`，存 `switch_id_t`；`data_i` 取 `mst_aw_chan.id[SlvAxiIDWidth+:MstIdxBits]`（源端口号）。

```systemverilog
// src/axi_mux.sv:353-366  W 通道：按队头选源，last 拍 pop
assign mst_w_chan = slv_w_chans[w_fifo_data];          // 队头源端口的 W 数据
...
w_fifo_pop = slv_w_valids[w_fifo_data] & mst_w_ready & mst_w_chan.last;  // last 才出队
```

AW 仲裁树用的正是文档描述的「推进一位」型轮询（中文旁注：`LockIn=1` 锁定赢家直到握手完成；`rr_arb_tree` 内部优先级按上述方式推进）：

> [src/axi_mux.sv:L264-L281](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L264-L281) —— `i_aw_arbiter`：`rr_arb_tree #(NumIn=NoSlvPorts, LockIn=1)`。

正因为此，mux 内部额外加了一个 `lock_aw_valid` 锁存（见 4.4）来**切断 AW 与 W FIFO 之间的反向依赖**——但注意，这是 mux **内部**的解耦，与「禁止在 demux/mux **之间**插寄存器」不矛盾。

#### 4.3.4 代码实践（讲解型，对应主实践任务）

1. **目标**：用一段话向同事讲清「demux 输出与 mux 输入之间加 FIFO 会死锁」，并自举一个两输入仲裁的例子。
2. **步骤**：按下表先把四条件与 W 通道的对应关系背熟，再构造下面的两输入场景。
3. **两输入仲裁示例**（用来直观说明「循环等待」如何形成）：
   - 设 xbar 有两个 slave 端口 `S0`、`S1`，两个 master 端口 `M0`、`M1`。
   - `S0` 先后发两笔写：`A`（去 `M0`）、`C`（去 `M1`）；`S1` 先后发两笔写：`B`（去 `M1`）、`D`（去 `M0`）。
   - mux@`M0` 的 AW 仲裁树先选了 `S0` 的 `A`，于是其 W FIFO 队头锁定为「等 `S0` 的 W」；mux@`M1` 先选了 `S1` 的 `B`，W FIFO 队头锁定为「等 `S1` 的 W」。
   - 现在假设 demux→mux 之间插了一级 FIFO：`S0` 的 W 突发 `A` 被卡在去 `M0` 的 FIFO 后面还没就绪，而 `S0` 又得先送完 `A` 才轮到 `C`（去 `M1`）；对称地，`S1` 得先送完 `B`（去 `M1`）才轮到 `D`（去 `M0`）。
   - 于是出现环：`M0` 等 `S0` 的 `A` 的 W；`S0` 的 `A` 卡在 FIFO；`S0` 的 demux 又因为 W FIFO/仲裁推进在等；`M1` 同理等 `S1` 的 `B`……两个 mux 互相等待对方释放 W 流，谁也动不了——**循环等待成立**。
4. **需要观察的现象**：移除 demux→mux 之间的 FIFO 后，AW 的切换决定与 W FIFO 的推入在同一周期发生，`M0`/`M1` 的选择被强制串行化，上述环无法闭合。
5. **预期结果**：你能向同事复述——「前三条（互斥/占有等待/不可抢占）由 AXI 协议决定改不了，只能靠不在内部插寄存器来打破第④条循环等待；所以 xbar 内部 demux 与 mux 之间永远直连」。
6. 该讲解可直接对照 [doc/axi_xbar.md:L103-L109](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L103-L109) 验证你的叙述与官方一致。

#### 4.3.5 小练习与答案

**练习 1**：前三条 Coffman 条件为什么「改不了」？
**答**：①互斥来自 W 通道 mux 的多选一本性（同一时刻只能转发一个端口的 W 突发）；②占有并等待来自 AXI「`valid` 必须保持到 `ready`」；③不可抢占来自 AXI「W 不可交错、须与 AW 同序」。三者都是协议级硬约束。

**练习 2**：能不能用「加大 W FIFO 深度」来避免死锁？
**答**：不能。FIFO 深度只影响能缓冲多少拍，不影响「循环等待」是否成立；只要 demux/mux 之间存在寄存器并配合 `rr_arb_tree` 的推进方式，环就会形成，深 FIFO 只是延迟死锁显现。

**练习 3**：文档为什么把 `FallThrough` 推荐设为 `0`？
**答**：`FallThrough=1` 会让 AW 的路由决定「穿透」到 W 通道同周期生效，从而把 AW 的组合逻辑挂到 W 的组合路径上，拉长 W 关键路径。设为 `0` 切断该穿透，保护 W 时序。这与「不插内部寄存器」不冲突——它管的是 mux **内部** AW→W 的组合穿透，不是 demux/mux **之间**的寄存器。

---

### 4.4 你「可以」动的旋钮：FallThrough 与 lock_aw_valid

4.3 说了一堆「不能动」。本节补充两个**可以安全调整**、且与 W 通道死锁边界强相关的机制，避免你误以为 W 通道什么都改不了。

#### 4.4.1 概念说明

- **`FallThrough`**（mux 与 demux 都有此参数）：决定 AW 的路由决定是否「穿透」到 W 通道——即同一周期内，AW 握手的同时就允许对应的 W 拍被转发。开启可少一拍延迟，但**会把 AW 的组合逻辑（仲裁、id 查找等）并入 W 的组合路径**，拉长 W 关键路径。文档与 demux 文档都推荐在高频下设 `0`。
- **`lock_aw_valid`**（mux 内部）：mux 在 AW 上做出仲裁决定后，会**立刻把赢家端口号推入 W FIFO**（`w_fifo_push`），不等下游 `mst_aw_ready`。如果下游这一拍没准备好，就用 `lock_aw_valid` 寄存器把 AW `valid` 锁住，下拍继续。其作用是**切断「AW 等 W FIFO」与「W FIFO 等 AW」之间的组合环**——这是 mux 内部自带的、防止单个 mux 自锁死锁的解耦，和 4.3 的「demux/mux 之间不插寄存器」是两个层面。

#### 4.4.2 核心流程

```text
mux 的 AW 处理（无 lock_aw_valid 会出问题）：
  仲裁出赢家 ─► 想推 W FIFO ─► 但 W FIFO 的 push 又依赖 mst_aw_ready？
            └─ 有环风险。解法：推 FIFO 与等 ready 解耦。

实际实现：
  仲裁赢家 + W FIFO 不满 ─► 立即 w_fifo_push、置 mst_aw_valid
     ├─ 本拍 mst_aw_ready=1 ─► aw_ready=1，完成，仲裁树可放下一笔
     └─ 本拍 mst_aw_ready=0 ─► lock_aw_valid_q 置 1，下拍继续顶 mst_aw_valid
                                 （W FIFO 已记录决定，W 通道可独立推进）
```

注释把意图写得很清楚：「This FF removes AW to W dependency」。

#### 4.4.3 源码精读

`lock_aw_valid` 的声明与意图（中文旁注：锁存 AW 仲裁决定，让 W FIFO 的推入不再依赖下游 AW 握手，从而移除 AW→W 的组合依赖）：

> [src/axi_mux.sv:L172-L184](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L172-L184) —— `lock_aw_valid_d/q`、`load_aw_lock`、W FIFO 满/空/push/pop 信号声明；注释明言「This FF removes AW to W dependency」。

对应的控制逻辑（中文旁注：仲裁赢家且 FIFO 不满 ⟹ 立即 `w_fifo_push` 并顶 `mst_aw_valid`；下游不 ready 则进 `lock` 态）：

> [src/axi_mux.sv:L284-L315](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L284-L315) —— AW 通道 `always_comb` + `FFLARN(lock_aw_valid_q, ...)`：把「推 FIFO」与「等下游 ready」解耦。

`FallThrough` 透传到 W FIFO（中文旁注：`FALL_THROUGH` 参数直连 `i_w_fifo`，影响 AW 决定与 W 拍是否同周期生效）：

> [src/axi_mux.sv:L317-L320](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L317-L320) —— `fifo_v3 #(.FALL_THROUGH(FallThrough), .DEPTH(MaxWTrans))`。

文档对 `FallThrough` 的推荐（中文旁注：推荐 `CUT_ALL_AX` + `FallThrough=0`，以防 AW 逻辑延长 W 组合路径）：

> [doc/axi_xbar.md:L63](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L63) —— 推荐配置：AW/AR 各 2 拍延迟、`FallThrough=0`。

#### 4.4.4 代码实践（思考型）

1. **目标**：理解若删掉 `lock_aw_valid` 会发生什么。
2. **步骤**：阅读 [src/axi_mux.sv:L284-L315](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L284-L315)，假设把 `lock` 分支去掉、让 `w_fifo_push` 直接依赖 `mst_aw_ready`。
3. **需要观察的现象**：此时 AW 仲裁树要等下游 `mst_aw_ready` 才能完成握手并推 FIFO，而下游又可能因 W 通道未推进而压低 `mst_aw_ready`，形成 mux 内部的组合/握手环。
4. **预期结果**：单个 mux 在某些激励下会自锁（功能死锁），`lock_aw_valid` 的存在正是为了打破它。
5. 这是「源码阅读型」推理，无需运行；如需验证可在 tb 中构造下游持续反压的激励对比有/无锁存——**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`lock_aw_valid` 与「demux/mux 之间不插寄存器」矛盾吗？
**答**：不矛盾。前者是 mux **内部** 解耦 AW 与 W FIFO 的握手锁存，属同一模块内；后者禁止的是 demux 与 mux **两个模块之间** 的寄存器，那是会触发循环等待的层面。两者一内一外，共同保证 W 通道无死锁。

**练习 2**：什么时候值得开 `FallThrough=1`？
**答**：当目标频率较低、W 通道组合路径余量充足，且希望减少一拍写延迟时。代价是 W 关键路径变长，高频下应关闭。

**练习 3**：两个 xbar 互联时 `LatencyMode` 有何额外限制？
**答**：见 [doc/axi_xbar.md:L65](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L65)：双向互联的两个 xbar 必须各选 `CUT_SLV_PORTS`、`CUT_MST_PORTS` 或 `CUT_ALL_PORTS` 之一，否则未切通道会形成跨 xbar 的时序环路。

## 5. 综合实践

把本讲三件事串成一个给同事的「10 分钟讲解」：

1. **保序与停顿**：用 4.1 的 `T1/T2` 同 ID 跨端口场景，画出 slave 端口 `aw_ready` 被压低的时序，说明这是「没有 reorder buffer」的直接代价。
2. **面积折中**：用 4.2 的 16 笔 `0x00/0x10/…` 例子，说明把 `AxiIdUsedSlvPorts` 从 8 调到 4 会怎样误冲突、何时该上 `UniqueIds`。
3. **死锁边界**：用 4.3 的两输入仲裁例子（`S0→{M0,M1}`、`S1→{M1,M0}`）讲清 Coffman 四条件，强调「前三条改不了、只能靠不在内部插寄存器打破第④条」，并指出 `lock_aw_valid` 是 mux **内部** 的合法解耦、`FallThrough` 是可调旋钮。

交付物：一张图（含两个 slave 端口、两个 master 端口、demux 阵列、mux 阵列、W FIFO 与 id_counters 的位置）+ 一段说明（哪条连线必须直连、哪些寄存器允许加在哪里）。这张图应能让你一眼看出「内部 demux↔mux 必须直连，对外边界才能加 spill」。

## 6. 本讲小结

- AXI「同 ID 同方向保序」在没有 reorder buffer 的 xbar 里被翻译成 **slave 端口停顿**：同 ID 去往不同 master 端口的第二笔事务必须等第一笔完成。
- 停顿由 `axi_demux_id_counters` 的占用/端口登记驱动，RTL 体现为 [axi_demux_simple.sv:175-177](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L175-L177) 的 AW 放行门与 AR 对称门。
- `AxiIdUsedSlvPorts`/`AxiLookBits` 只比较 ID 低位，用「偶发误冲突」换「指数级省面积」；`UniqueIds=1` 在前提下把面积从 \(O(2^{I})\) 降到 \(O(I)\)。
- Coffman 四条件中前三条（互斥/占有等待/不可抢占）由 AXI 协议决定不可改，**唯一能避免 W 通道死锁的是打破第④条循环等待**。
- 因此 xbar **内部 demux 与 mux 之间禁止插 spill 寄存器/FIFO**，spill 只能放在对外边界（由 `LatencyMode` 控制）；mux 内部的 `lock_aw_valid` 是合法的 AW↔W 解耦。
- 推荐配置：`CUT_ALL_AX` + `FallThrough=0`；双向互联两个 xbar 时必须用 `CUT_*_PORTS` 系列以避免时序环路。

## 7. 下一步学习建议

- **U7（流控与缓冲）**：本讲强调「内部不能插寄存器」，U7 会讲在**对外边界**如何用 `axi_fifo`/`spill_register` 安全地切路径与缓冲，正是这里的合法补充。
- **U15-l1（ATOPs）**：本讲多次提到 ATOP 注入（AW 向 AR 计数器 inject +1）以避免 R 通道下溢，U15-l1 会系统讲解原子操作如何引入读写通道间的依赖。
- **U15-l4（异构网络）**：把多个 xbar、cdc、转换器拼成大网络时，「哪些位置能加缓冲、哪些会成环」是核心难题，本讲的死锁边界是其直接前置。
- 继续精读 [doc/axi_xbar.md](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md) 全文，并对照 `test/tb_axi_xbar.sv` 的随机回归看这些停顿/死锁边界如何被验证覆盖。
