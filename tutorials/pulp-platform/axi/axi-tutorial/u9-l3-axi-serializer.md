# axi_serializer：事务序列化

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚**为什么**要把多条 AXI 事务流「序列化」成同一个 ID，以及它解决什么样的下游兼容问题。
- 看懂 `axi_serializer` 用**两个 ID FIFO** 实现「请求方向把 ID 抹零、响应方向把 ID 还原」的核心数据通路。
- 理解它如何用一个 **AtopIdle / AtopDrain / AtopExecute 三态机**安全地放行 AXI5 原子操作（ATOP）。
- 读懂 `tb_axi_serializer` 的自检 checker，并用一条 `make` 命令跑通仿真。
- 说清序列化对吞吐的代价，以及它与上一讲 `axi_burst_splitter` 串联喂养「能力受限下游」的典型链路。

## 2. 前置知识

本讲承接 [u9-l1（burst_splitter）](u9-l1-burst-splitter.md)，需要你已经建立以下认知（不讲重复内容）：

- **AXI ID 的作用**（u1-l3 / u2-l1）：同一 master、同 ID、同方向的事务必须**按序返回**响应；不同 ID 之间可以**乱序、并发**。ID 是「事务流」的标签。
- **ATOP 原子操作**（u2-l1，u15-l1 会深入）：`aw.atop` 字段编码原子操作；当 `aw.atop[5] == 1`（即 `ATOP_R_RESP`）时，一次原子写会**同时**产生 B 响应和带数据的 R 响应——它没有对应的 AR，却会读回数据。
- **req_t / resp_t 结构体**（u2-l4）：本模块内核只面对结构体端口，不面对扁平信号。
- **valid/ready 握手、in flight（在途）、pending（挂起）**（u1-l3 / u2-l3）。
- **fifo_v3**：来自外部 `common_cells` 的 FIFO 原语，提供 `push_i / pop_i / full_o / empty_o / data_i / data_o`。

本讲用到的几个通俗术语：

| 术语 | 含义 |
| --- | --- |
| **序列化（serialize）** | 让原本可并发的多条事务流共用同一个身份（ID），从而失去 ID 级并行、退化为一条有序流。 |
| **抹零** | 把发往下游的 `id` 强制写成 `'0`。 |
| **还原** | 响应返回上游之前，把 `id` 恢复成上游原本的值。 |
| **排空（drain）** | 先把所有在途事务跑完，再开始一件「特别的事」（这里指 ATOP）。 |

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/axi_serializer.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_serializer.sv) | 内核 `axi_serializer`（结构体端口）+ 接口外壳 `axi_serializer_intf`。本讲主角。 |
| [test/tb_axi_serializer.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_serializer.sv) | 定向随机自检测试台：随机多 ID 主端 + 随机从端，外加自建 checker。 |
| [src/axi_pkg.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv) | 提供 `ATOP_NONE`、`ATOP_R_RESP` 等常量。 |
| [Makefile](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Makefile) | `sim-axi_serializer.log` 仿真目标。 |

依赖层级：`axi_serializer.sv` 在 `src_files.yml` 中位于 **Level 2**——它只依赖 Level 0 的 `axi_pkg` 与外部 `common_cells` 的 `fifo_v3`，不依赖本库任何 Level 1/2 模块。在库内，它被 [src/axi_id_serialize.sv:218](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_serialize.sv#L218) 按「每个 master 端口 ID 一个」地例化，是 u10-l2 的积木。

## 4. 核心概念与源码讲解

### 4.1 序列化的动机：为什么要让所有事务共用一个 ID

#### 4.1.1 概念说明

AXI 用 ID 给事务分组：不同 ID 可以乱序、并发，从而榨取带宽。但现实里有一类**能力受限的下游**，它们只认得**单一 ID**，例如：

- 某些协议桥（如 AXI↔AXI-Lite 桥，见 u13-l1）内部只维护一条在途事务；
- 只支持单一 outstanding 的简单外设 / 存储接口；
- 要求所有事务严格保序的专用 IP。

如果把一个会发出**多个不同 ID**的 master 直接接到这种下游，下游会因为看到陌生 ID、或无法区分乱序响应而出错。`axi_serializer` 的职责就是在两者之间当「翻译」：**无论上游用多少个 ID，对下游一律只呈现 ID 0**。

它的做法直白得惊人——把发往下游的 AW/AR 的 `id` 强制改写为 `'0`：

[src/axi_serializer.sv:71-73](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_serializer.sv#L71-L73) 把下游 AW/AR 的 id 抹零。

但这里藏着一个**致命约束**：上游发来的 ID **绝对不能丢**。因为 B/R 响应必须按原 ID 还原给上游——上游正是靠不同 ID 区分自己的多条事务流，若返回的 ID 全变 0，上游就完全无法把响应和请求对上号。所以 serializer 必须在内部**记住**每个事务的原 ID，等响应回来时再**还原**回去。这就是下一节要讲的双 FIFO 机制。

**吞吐代价**：一旦所有事务共用 ID 0，下游看到的就是**单一 ID 流**。由于 AXI 规定同 ID 同方向必须保序，这些事务对下游构成一条**有序流**（响应按请求顺序返回）。原本可乱序并发的不同 ID 事务，现在被约束成保序——这就是「序列化」之名。注意：serializer 本身**并不禁止并发下发**（在途上限由 FIFO 深度决定，见 4.2），它去掉的是 **ID 级并行**与**乱序优化**的空间。

**与 `axi_burst_splitter` 的协作**：两者都是「**事务降级器**」，夹在能力强的 master 和能力弱的 slave 之间，职责正交、可背靠背串联：

| 模块 | 降级的维度 | 下游因此获得的保证 |
| --- | --- | --- |
| `axi_burst_splitter` | 突发长度（`len`） | 每段至多 1 拍（或定粒度） |
| `axi_serializer` | ID 种类 | 只剩单一 ID |

当一个下游**同时**只吃单拍突发、又只吃单一 ID 时（典型如 AXI-Lite 桥下游），就把 `burst_splitter` 与 `serializer` 串联使用。它们在源码里互不引用，是由集成者按需拼接的独立原语——这正是本库「组合优于配置」哲学的又一次体现（回顾 u1-l1）。

#### 4.1.2 核心流程

把 serializer 看成一个「改 ID 的中继」，一条写事务经过它的高层流程：

```text
请求方向（master -> slave）：
  上游 AW(id=X) ──► [抹零] ──► 下游 AW(id=0)
                          │
                          └──► 把 X 压入「写 ID FIFO」备忘

响应方向（slave -> master）：
  下游 B(id=0) ──► [从写 ID FIFO 头取出 X] ──► 上游 B(id=X)
                          │
                          └──► 弹出 FIFO 一个槽
```

读事务完全对称，只是用「读 ID FIFO」，且因为一次读突发有多拍 R，要等 `r.last` 才弹出 FIFO。

#### 4.1.3 源码精读

serializer 的组合逻辑一开始就把所有信号整体透传，然后只改两个字段：

[src/axi_serializer.sv:67-73](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_serializer.sv#L67-L73) 先 `mst_req_o = slv_req_i` 整块透传，再把下游 `aw.id / ar.id` 抹零。

还原侧同样先透传、再覆盖两个字段：

[src/axi_serializer.sv:75-78](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_serializer.sv#L75-L78) 用 `ar_id`、`b_id`、`r_id` 三个本地变量（后两者来自 FIFO 输出）覆盖响应的 id 字段。

> 关键直觉：**整个模块只改 `id`，其余载荷（addr/data/strb/last/…）和 valid/ready 都沿用透传值，再在下方按需门控握手**。读懂这一点，后面的 FIFO 与状态机都只是在为「改 id」这件小事服务。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：亲手定位「抹零」与「还原」各发生在哪几行，建立对数据通路的整体印象。
2. **步骤**：
   - 打开 `src/axi_serializer.sv`，找到 4.1.3 引用的两段代码。
   - 在第 68 行 `mst_req_o = slv_req_i;` 处确认「整块透传」。
   - 在第 72–73 行确认抹零；在第 77–78 行确认还原用的是 `b_id`、`r_id`（这两个值来自哪里？答案在 4.2）。
3. **观察**：注意抹零只发生在**请求方向**（`mst_req_o`），还原只发生在**响应方向**（`slv_resp_o`）——方向绝不能搞反。
4. **预期结果**：你能用一句话向同事说明「serializer 改了哪两个字段、分别在哪个方向」。

#### 4.1.5 小练习与答案

**Q1**：如果 serializer 只抹零、不还原（删掉第 77–78 行），上游会观察到什么现象？
**答**：上游发出的 AW/AR 带各种 id，但收到的 B/R 全是 `id=0`，无法把响应与请求对应，功能彻底错乱。

**Q2**：serializer 能不能用来「提高」吞吐？为什么？
**答**：不能。它只做降级——把多 ID 收敛为单 ID，去掉的是并行/乱序空间，吞吐只会持平或下降。

---

### 4.2 双 ID FIFO：请求抹零与响应还原的数据通路

#### 4.2.1 概念说明

serializer 内部有**两个独立的 ID FIFO**，读写各一个：

- **写 ID FIFO**（`i_wr_id_fifo`）：AW 握手时压入 `slv_req_i.aw.id`，B 握手时弹出，输出 `b_id` 还原写响应。
- **读 ID FIFO**（`i_rd_id_fifo`）：AR 握手时压入 `slv_req_i.ar.id`，R 的 `last` 拍握手时弹出，输出 `r_id` 还原读响应。

这两个 FIFO 同时承担两个职责，缺一不可：

1. **备忘录**：按到达顺序记下每个事务的原 ID，响应回来时按同样顺序还原。由于 serializer 把所有事务都映射到同一个下游 ID 0，**响应本身不再携带能区分事务的信息**，唯一能还原的就是「请求与响应的到达顺序一致」——这正是 FIFO 的天然语义。
2. **流量闸门**：FIFO 的 `full` 用来**反压**请求方向（满了就不能再接新事务，否则原 ID 会被覆盖丢失）；FIFO 的 `empty` 用来**屏蔽**响应方向（空说明根本没有待响应的事务，下游此时若冒出 B/R 必然是错配，必须挡住）。

两个 FIFO 的深度由参数 `MaxReadTxns` / `MaxWriteTxns` 决定，它们就是各自方向的**在途事务上限**：

\[ \text{某方向同时在途事务数} \le \text{对应 FIFO 深度} \in \{\textit{MaxReadTxns},\ \textit{MaxWriteTxns}\} \]

#### 4.2.2 核心流程

请求方向（以读为例）的门控与压入：

```text
rd_fifo_full = 1 ?  则 ar_valid 拉低（反压上游）、ar_ready 拉低（不接 AW）
AR 握手成功      ?  rd_fifo_push = 1，把 ar_id 压入读 FIFO
```

响应方向（以读为例）的门控与弹出：

```text
rd_fifo_empty = 1 ? 则 r_valid 拉低（屏蔽下游 R）、r_ready 拉低
R 握手 且 r.last ?  rd_fifo_pop = 1，弹出一个原 ID
```

写方向把 `rd_fifo_*` 换成 `wr_fifo_*`、把 `r.last` 换成「B 握手即弹」（因为一个写只有一个 B）。

#### 4.2.3 源码精读

**请求方向——AR 用 `rd_fifo_full` 门控并在握手时压栈**：

[src/axi_serializer.sv:100-103](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_serializer.sv#L100-L103) AR 的 valid/ready 都 `& ~rd_fifo_full`，握手成功则 `rd_fifo_push=1`。

[src/axi_serializer.sv:104-110](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_serializer.sv#L104-L110) 普通写（`atop[5:4]==ATOP_NONE`）的 AW 同样用 `wr_fifo_full` 门控。这里 `atop` 的判断把 ATOP 单独分流到 4.3 讲。

**响应方向——B/R 用对应 FIFO 的 `empty` 门控**：

[src/axi_serializer.sv:140-146](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_serializer.sv#L140-L146) B 的 valid/ready 都 `& ~wr_fifo_empty`，R 的 valid/ready 都 `& ~rd_fifo_empty`。

**弹出条件**：

[src/axi_serializer.sv:166-167](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_serializer.sv#L166-L167) 读 FIFO 只在 R 握手**且** `r.last` 时弹出——一个读突发有多拍 R，原 ID 只在第一拍押入，必须等末拍才弹。

[src/axi_serializer.sv:186-187](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_serializer.sv#L186-L187) 写 FIFO 在每次 B 握手时弹出。

**两个 FIFO 实例**：

[src/axi_serializer.sv:149-165](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_serializer.sv#L149-L165) 读 ID FIFO：`DEPTH=MaxReadTxns`，`FALL_THROUGH=0`，输入 `ar_id`，输出 `r_id`。

[src/axi_serializer.sv:169-185](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_serializer.sv#L169-L185) 写 ID FIFO：`DEPTH=MaxWriteTxns`，输入 `slv_req_i.aw.id`，输出 `b_id`。

> 关于 `FALL_THROUGH(0)`：代码注释（第 150 行）写得很明白——"No fall-through as response has to come a cycle later anyway"。压栈发生在**请求握手当拍**，弹出与还原发生在**响应握手当拍**，两者天然隔至少一拍；用普通（非直通）FIFO，其输出恰好晚一拍出现，正好对齐响应的到来时机。

#### 4.2.4 代码实践（修改观察型）

1. **目标**：直观感受 FIFO 深度就是在途上限，以及满时如何反压上游。
2. **步骤**：
   - 打开 `test/tb_axi_serializer.sv`，找到 `NoPendingDut = 4`（[第 23 行](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_serializer.sv#L23)），它被传给 DUT 的 `MAX_READ_TXNS/MAX_WRITE_TXNS`（[第 116–117 行](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_serializer.sv#L116-L117)），即 FIFO 深度为 4。而随机主端的并发上限 `MaxAW=MaxAR=30`（[第 25–26 行](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_serializer.sv#L25-L26)）远大于 4。
   - 在 `src/axi_serializer.sv` 的两个 FIFO 实例上，把 `usage_o` 的 `/*not used*/` 接到一个临时输出或 `$display`，仿真中打印读/写 FIFO 的实时占用。
3. **观察**：占用会在 0–4 之间波动；当某 FIFO 占到 4（满），对应方向的 `ar_valid/aw_valid` 应被压低，主端在途事务被限制在 ≤4。
4. **预期结果**：你会看到主端虽然「想」并发 30 笔，但被 serializer 的 FIFO 反压到每方向至多 4 笔在途——这就是 FIFO 深度对吞吐的硬约束。**待本地验证**（需要 vsim/questasim 环境）。

#### 4.2.5 小练习与答案

**Q1**：为什么读 FIFO 用 `r.last` 才弹出，写 FIFO 却握手即弹？
**答**：一次读突发有 `len+1` 拍 R，但只押入一次原 ID（在 AR 那拍），必须等最后一拍 R 才能弹；一次写只有 1 个 B，握手即弹。

**Q2**：把 `MaxReadTxns` 设成 1 会怎样？
**答**：读 FIFO 深度为 1，任意时刻只允许 1 笔读在途，第二笔 AR 会被反压直到前一笔的 `r.last` 返回——读吞吐被压到最低。

---

### 4.3 ATOP 排空三态机

#### 4.3.1 概念说明

普通事务好办，但 AXI5 原子操作（ATOP）会破坏 4.2 那套「请求与响应一一对应」的简单假设。难点有二：

1. **`ATOP_R_RESP` 型原子写没有 AR，却有 R**：当 `aw.atop[5]==1`（即 [axi_pkg::ATOP_R_RESP](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L445-L447)），一次 AW 会同时产生 B（写回应）**和** R（读回数据）。可读 ID FIFO 是靠 AR 押栈的，ATOP 根本没有 AR——那它的 R 响应由谁来还原 ID？
2. **所有事务都是 ID 0**：serializer 把一切抹零后，ATOP 的 R 和普通读的 R 在下游都标 ID 0，若不加隔离，serializer 靠「顺序」还原 ID 的机制会被打乱。

`axi_serializer` 的解法是**「排空后独占执行」**：收到 ATOP 时，先把所有在途事务（读、写 FIFO 都空）跑完，然后**独占地**发出这笔 ATOP，并**人为地把 AW 的原 ID 押进读 ID FIFO**（补上缺失的那次「AR 押栈」），让 ATOP 的 R 也能被正确还原；等 ATOP 的 B 和 R 都回来，再恢复正常放行。

这套逻辑由一个三态有限状态机管理：

[src/axi_serializer.sv:49-53](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_serializer.sv#L49-L53) 定义 `AtopIdle / AtopDrain / AtopExecute` 三态。

#### 4.3.2 核心流程

```text
AtopIdle （正常放行普通事务）
   │  收到 atop[5:4] != ATOP_NONE 的 AW
   │  且当前没有未完成的 AR（或 AR 本拍正好被接走）
   ▼
AtopDrain（等读写 FIFO 全空 = 所有在途事务已排空）
   │  wr_fifo_empty && rd_fifo_empty
   │  → 独占发出这笔 ATOP 的 AW
   │  → 若 atop[ATOP_R_RESP]，把 aw.id 押进读 ID FIFO（补缺失的 AR 押栈）
   ▼
AtopExecute（等 ATOP 的 B 与 R 都返回 = 两个 FIFO 重新变空）
   │  回到 AtopIdle，恢复正常放行
```

#### 4.3.3 源码精读

**判定是否为 ATOP**：用 `aw.atop[5:4]` 的高两位与 [axi_pkg::ATOP_NONE](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L398-L400)（`2'b00`）比较。

[src/axi_serializer.sv:104-117](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_serializer.sv#L104-L117) `aw_valid` 时分支：`atop[5:4]==ATOP_NONE` 走普通写（4.2 已讲）；否则准备进入 `AtopDrain`，但要先等当前 AR 通道干净。

**AtopDrain——排空后独占发送**：

[src/axi_serializer.sv:121-136](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_serializer.sv#L121-L136) 仅当 `wr_fifo_empty && rd_fifo_empty`（在途全清）才拉起 AW；若 `atop[ATOP_R_RESP]`，把 `ar_id` 改写成 `slv_req_i.aw.id` 并 `rd_fifo_push`（[第 127–131 行](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_serializer.sv#L127-L131)）——这正是为 ATOP 补上那次「缺失的 AR 押栈」，使其 R 响应能从读 FIFO 还原出原 AW 的 ID。AW 握手后进入 `AtopExecute`。

**AtopExecute——等响应回齐**：

[src/axi_serializer.sv:89-95](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_serializer.sv#L89-L95) 当读写 FIFO 再次都空（说明 ATOP 的 B 和 R 都已被消费），回到 `AtopIdle`。

**状态寄存器**：

[src/axi_serializer.sv:189](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_serializer.sv#L189) 用外部宏 `FFARN` 把 `state_d` 打成 `state_q`，复位值为 `AtopIdle`。

> 设计取舍：排空会带来一次 ATOP 的额外延迟（要等在途事务清零），但换来的是**正确性**——在「全部 ID 被收敛为 0」的前提下，这是唯一能保证 ATOP 的 R 不与普通读的 R 串味的安全做法。注意：serializer **支持** ATOP（不丢弃、不替换响应），这与 u15-l1 的 `axi_atop_filter`（直接过滤掉 ATOP）是不同策略。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：在测试台中确认 ATOP 路径被真实激励到，并理解 checker 如何处理 ATOP 的「无 AR 有 R」。
2. **步骤**：
   - 在 `tb_axi_serializer.sv` 确认主端开启了 ATOP：`EnAtop = 1'b1`（[第 27 行](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_serializer.sv#L27)），并传给 `axi_rand_master` 的 `AXI_ATOPS`（[第 52 行](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_serializer.sv#L52)）。
   - 在 checker 里看 ATOP 如何被记账：当一笔 AW 的 `atop[ATOP_R_RESP]` 有效时，它的原 ID 也被压进 `ar_queue`（[第 191–193 行](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_serializer.sv#L191-L193)）——这与 DUT 在 `AtopDrain` 里向读 FIFO 押栈完全对称。
3. **观察**：checker 用同一套 `ar_queue` 同时服务普通 AR 和 ATOP 的隐式读，验证两者的 R 都被还原成正确原 ID。
4. **预期结果**：你能解释「为什么 DUT 和 checker 都要在 ATOP_R_RESP 时往『读侧』队列押一次 ID」——因为这是唯一能还原其 R 响应 ID 的途径。

#### 4.3.5 小练习与答案

**Q1**：为什么 ATOP 必须在 `AtopDrain` 里等 FIFO 全空才发，而不能像普通写那样直接发？
**答**：因为所有事务下游 ID 都是 0，若不排空，ATOP 的 R 会和已在途的普通读的 R 混在同一个 ID 上、顺序不可分，serializer 靠顺序还原 ID 的机制会失效。排空保证 ATOP 执行期间没有其他事务干扰。

**Q2**：一笔 `ATOP_R_RESP` 原子写最终在读写两个 FIFO 里各押入几次？
**答**：写 FIFO 押 1 次（AW 那拍，还原 B 用）；读 FIFO 也押 1 次（在 `AtopDrain` 里人为补，还原 R 用）。

---

### 4.4 tb_axi_serializer：定向随机自检

#### 4.4.1 概念说明

`tb_axi_serializer.sv` 是一台完整的「定向随机验证」机器（回顾 u3-l3 的四件套：时钟复位、激励、自检、停机）。它的拓扑正是本讲主题的活样本：

```text
axi_rand_master (4-bit ID, 多 ID 并发, 可发 ATOP)
      │  AXI_ASSIGN
      ▼
master / master_dv ──► [ axi_serializer_intf ] ──► slave / slave_dv
      │                                          │
      └────────────── checker（双侧采样） ───────┘
                                                  │
                                          axi_rand_slave（单下游）
```

关键配置（[第 18–38 行](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_serializer.sv#L18-L38)）：主端发 `NoWrites=5000` / `NoReads=3000` 笔，ID 宽 4（即上游可有多达 16 个不同 ID）；DUT 的 FIFO 深度 `NoPendingDut=4`；时序三参数 `CyclTime=10ns / ApplTime=2ns / TestTime=8ns`（满足 0<TA<TT<T_clk，回顾 u3-l1/u3-l3）。

checker 的自检思路极其精准地呼应了 serializer 的机制：**期望下游 AW/AR 的 id 为 `'0`，期望上游 B/R 的 id 等于原值**。

#### 4.4.2 核心流程

checker（`proc_checker`）在每个时钟沿的 `#TestTime` 采样点做两类事：

1. **建期望队列**：在 master 侧每看到一次 AW/AR 握手，就生成一份「下游应看到的」期望（id 置 0），并把**原 id** 压进 `aw_queue/ar_queue` 备用；在 slave 侧每看到一次 B 握手，从 `aw_queue` 弹出原 id，生成「上游应看到的」期望 B。
2. **比对**：在 slave 侧比对实际 AW/W/AR（断言 id 确实为 0、载荷未变）；在 master 侧比对实际 B/R（断言 id 被正确还原、载荷未变）。

#### 4.4.3 源码精读

**激励与停机**（`proc_axi_master`）：

[test/tb_axi_serializer.sv:129-141](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_serializer.sv#L129-L141) 主端先 `add_memory_region` 配三段地址区（DEVICE/WTHRU/WBACK，回顾 u3-l2 的 cache 类型），再 `run(NoReads, NoWrites)` 发激励，结束后置 `end_of_sim`。

**checker——请求方向建期望（id 置 0）**：

[test/tb_axi_serializer.sv:185-194](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_serializer.sv#L185-L194) AW 握手时 `aw_exp.id='0`，原 id 进 `aw_queue`；若 `atop[ATOP_R_RESP]`，原 id 也进 `ar_queue`（呼应 4.3）。

[test/tb_axi_serializer.sv:205-211](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_serializer.sv#L205-L211) AR 握手时 `ar_exp.id='0`，原 id 进 `ar_queue`。

**checker——响应方向还原原 id**：

[test/tb_axi_serializer.sv:199-204](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_serializer.sv#L199-L204) B 握手时从 `aw_queue` 弹出原 id 作为期望 B 的 id。

[test/tb_axi_serializer.sv:212-221](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_serializer.sv#L212-L221) R 握手时：`r.last` 弹出 `ar_queue`，否则取队头 `ar_queue[0]`（同一读突发的中间拍共享同一个 id）。

**断言比对**：

[test/tb_axi_serializer.sv:223-227](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_serializer.sv#L223-L227) 在 slave 侧断言实际 AW == 期望（即下游 id 确为 0）；B/R 的比对在 [第 233–247 行](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_serializer.sv#L233-L247) 于 master 侧进行（断言 id 被正确还原）。

此外，DUT 自身带有协议断言（`aw_lost/w_lost/b_lost/ar_lost/r_lost`，[src/axi_serializer.sv:200-217](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_serializer.sv#L200-L217)），保证 serializer 永不丢拍、不破坏握手铁律。

#### 4.4.4 代码实践（可运行——本讲主实践）

1. **目标**：跑通官方测试台，亲眼确认「上游多 ID → 下游单 ID → 响应按原 ID 还原」整条链路无误。
2. **操作步骤**：
   - 在仓库根目录执行（回顾 u1-l4 的 make 约定）：
     ```bash
     make sim-axi_serializer.log
     ```
     该目标（[Makefile:82-85](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Makefile#L82-L85)）调用 `scripts/run_vsim.sh --random-seed axi_serializer`，因 `axi_serializer` 不在脚本的特殊 case 列表里，会落到默认分支 [run_vsim.sh:244-246](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L244-L246) 跑 `tb_axi_serializer`，种子为 `0` 和一个 `random`。
   - 仿真结束前会周期性打印 `Transmit AW <n> of 5000.` / `Transmit AR <n> of 3000.`（[test/tb_axi_serializer.sv:269-275](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_serializer.sv#L269-L275)）。
3. **需要观察的现象**：
   - vsim 每次运行后打印一行 `# Errors: 0, ...`（脚本靠 `grep "Errors: 0,"` 判活，[run_vsim.sh:33](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L33)）。
   - 日志末尾出现 `# ** Info: All transactions completed.`（[第 286 行](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_serializer.sv#L286)）。
   - Makefile 兜底：日志中不出现 `Error:` / `Fatal:`（[Makefile:84-85](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Makefile#L84-L85)）。
4. **预期结果**：两次运行（seed 0、random）均通过，checker 无任何 `$error` 触发——这证明上游 5000 写 + 3000 读（含 ATOP、多 ID）经 serializer 后下游只见 ID 0，且响应全部被正确还原。
5. 若本地无 vsim/questasim，此步标注「待本地验证」；可改为纯阅读 checker（4.4.3）理解其断言意图。

**进阶观察**：把 `NoPendingDut` 从 4 改为 1（[test/tb_axi_serializer.sv:23](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_serializer.sv#L23)）再跑一次，仿真应依然通过，但完成同样事务量的墙钟时间会明显变长——直观印证 4.2.5 Q2 的结论：FIFO 越浅，在途上限越低，吞吐越低。

#### 4.4.5 小练习与答案

**Q1**：checker 为什么在 slave 侧比对 AW/AR，却在 master 侧比对 B/R？
**答**：AW/AR 的「抹零」效果体现在**下游**（slave 侧）看到的 id 应为 0；B/R 的「还原」效果体现在**上游**（master 侧）看到的 id 应为原值。两边各查自己该看到的真相。

**Q2**：为什么 checker 对 R 的处理要区分 `r.last`？
**答**：与 DUT 的 FIFO 弹出条件一致——一个读突发的多拍 R 共享同一个原 id，只有 `last` 拍才推进队列，中间拍都用队头 `ar_queue[0]`。

---

## 5. 综合实践

把本讲知识串起来，完成下面这个「喂养能力受限下游」的小设计（源码阅读 + 拓扑设计型）：

**场景**：你有一个能发出 4 位 ID、并发 8 笔、含长突发的 master，要接到一个**只支持单一 ID、且每段突发不超过 1 拍**的下游 IP。

**任务**：

1. 画出 `axi_burst_splitter` 与 `axi_serializer` 的串联拓扑，说明两者的先后顺序是否影响功能正确性、是否影响吞吐（提示：splitter 在前会把长突发拆成多笔单拍事务，每笔仍带原 ID；serializer 在后会把它们统一收敛为 ID 0）。
2. 决定 serializer 的 `MaxReadTxns/MaxWriteTxns` 应取多少，才能让下游在任意时刻只看到 ≤N 笔在途（回顾 4.2 的 FIFO = 在途上限）。
3. 借鉴 `tb_axi_serializer.sv` 的 checker 写法，写一段伪代码：如何在下游侧断言「所有 AW/AR 的 id 恒为 0」，如何在上游侧用 ID 队列还原期望的 B/R id。
4. **进阶（待本地验证）**：参考 4.4.4，把 `axi_sim_mem`（u3-l2）接在下游当忠实存储，用 `axi_scoreboard` 在 master 侧自检，跑若干随机读写，确认串联后功能仍正确。

预期产物：一张拓扑框图 + 一段参数取值说明 + 一段 checker 伪代码。重点不是跑通，而是讲清「为什么需要这两件降级器、它们的 FIFO 深度如何决定下游看到的在途数」。

## 6. 本讲小结

- `axi_serializer` 把发往下游的所有 AW/AR 的 `id` **抹零**，使能力受限的下游（单 ID 桥/外设）只见到单一 ID 流。
- 上游原 ID 不能丢：模块用**读/写两个 ID FIFO** 按到达顺序备忘，响应返回时按同顺序**还原**——FIFO 的 `full` 反压请求、`empty` 屏蔽响应，FIFO 深度（`MaxReadTxns/MaxWriteTxns`）即各方向在途上限。
- 读 FIFO 在 `r.last` 才弹（多拍 R），写 FIFO 握手即弹（单 B）；`FALL_THROUGH=0` 让输出晚一拍，恰好对齐响应到来时机。
- AXI5 原子操作（ATOP）由 **AtopIdle→AtopDrain→AtopExecute** 三态机处理：先排空在途事务，再独占执行，并为 `ATOP_R_RESP` 人为向读 FIFO 补一次押栈，使其 R 响应能被正确还原。
- 序列化的代价是**吞吐**：去掉 ID 级并行与乱序空间；它与 `axi_burst_splitter`（降突发长度）正交，可串联喂养「单 ID + 单拍」的最简下游。
- `tb_axi_serializer.sv` 用随机多 ID 主端 + 自检 checker，精确验证「下游 id=0、上游响应 id=原值」，`make sim-axi_serializer.log` 一键回归。

## 7. 下一步学习建议

- **u10-l2 axi_id_serialize**：直接承接本讲。它为「每个 master 端口 ID」各配一个 `axi_serializer`，是在 serializer 基础上构建的、面积与吞吐更优的「降 ID」方案，学完本讲你已具备读懂它的全部零件。
- **u10 ID 宽度管理**：把 serializer 放进 `axi_id_remap` / `axi_iw_converter` 的家族里对比，理解「重映射 vs 序列化 vs 折中」三种降 ID 策略的差异。
- **u13-l1 axi_to_axi_lite**：serializer + burst_splitter 串联的典型消费者——把完整 AXI4 降级到 AXI-Lite（单拍、无 ID），届时你会再次看到本讲这两个降级器的协作。
- **源码延伸阅读**：精读 [src/axi_id_serialize.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_serialize.sv)，看它如何为每个 ID 例化本讲的 `axi_serializer`，以及它如何处理 serializer 之间的仲裁。
