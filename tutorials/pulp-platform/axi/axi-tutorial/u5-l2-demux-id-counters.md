# axi_demux_id_counters：outstanding 跟踪

## 1. 本讲目标

本讲聚焦 `axi_demux` 内部那块「看不见但最关键」的电路——`axi_demux_id_counters`。学完本讲，你应该能够：

- 说清楚 **为什么** 一个「1 拆 N」的 demux 必须为每个 AXI ID 维护在途（in-flight）事务计数，以及不维护会出什么错。
- 读懂 `axi_demux_id_counters` 模块本身：它的 \(2^{\text{AxiLookBits}}\) 个计数器阵列、每个计数器配一个「目标端口」寄存器、以及 lookup / push / pop / inject 四种操作。
- 看懂 `axi_demux_simple` 如何例化**两个**计数器实例（AW 方向、AR 方向），并用它们的输出构造「同 ID 同方向保序」停顿条件，以及 ATOP 原子写如何向 AR 计数器「注入」ID。
- 掌握三个关键参数 `AxiLookBits`、`MaxTrans`、`UniqueIds` 的取舍：面积 vs. 误冲突、并发上限、以及在什么前提下可以安全地把整个计数器阵列省掉。

承接上一讲（u5-l1）：上一讲我们从外部认识了 `axi_demux_simple` 的「译码在外、路由在内」「W 突发队列」「rr_arb_tree 回收 B/R」，并提到它内部例化了 `axi_demux_id_counters`。本讲就把这块黑盒打开。

## 2. 前置知识

- **AXI4 的 ID 与保序规则**：同一 ID、同一方向（都读或都写）的事务，从端必须**按发起顺序返回响应**。不同 ID 之间则可以乱序。这是 demux 必须跟踪 ID 的根本原因（详见 u1-l3、u2-l1）。
- **在途事务（in-flight / outstanding）**：地址拍已握手、但响应拍（B 或最后一拍 R）尚未握手的事务。一个 ID 同时可能有多笔这样的事务。
- **delta_counter / counter**：来自外部 `common_cells` 的可加减计数器原语，本模块的计数逻辑都建立在它之上。
- **上一讲的分工**（u5-l1）：`axi_demux_simple` 的 W 通道路由靠的是 `i_counter_open_w` + `w_select_q` 这一组**独立的**「W 突发队列」，**不是**本讲的 id_counters。id_counters 服务的是 B/R 响应的保序——这一点是本讲要反复强调的常见混淆点。

## 3. 本讲源码地图

| 文件 | 作用 |
|:--|:--|
| [src/axi_demux_id_counters.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_id_counters.sv) | 本讲主角：一个参数化的「按 ID 索引的在途计数器阵列」，提供 lookup/push/pop/inject 四种操作。 |
| [src/axi_demux_simple.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv) | demux 的纯组合内核。例化两个 id_counters 实例（AW/AR），用它们的输出构造保序停顿，并实现 ATOP 注入。 |
| [src/axi_demux.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux.sv) | demux 的接口外壳：给 AW/W/B/AR/R 五通道各加一级可选 `spill_register`，再例化 `axi_demux_simple`。本身不含 id 计数逻辑。 |
| [doc/axi_demux.md](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_demux.md) | 官方文档。*Ordering and Stalls*、*Implementation*、*Atomic Transactions* 三节是本讲问题的权威表述。 |

## 4. 核心概念与源码讲解

### 4.1 为什么 demux 必须跟踪在途事务

#### 4.1.1 概念说明

先建立一个直觉。假设 demux 把一个 slave 端口拆成两个 master 端口（port 0、port 1），地址译码规则是：`addr` 最高位为 0 去 port 0，为 1 去 port 1。

现在 master 连续发两笔**同一个 ID** 的写事务：第一笔 `AW` 地址 `0x0000`（去 port 0），第二笔 `AW` 地址 `0x1000`（去 port 1）。两笔事务都成功握手发出去了。

问题来了：它们的 B 响应可能**先由 port 1 返回、再由 port 0 返回**——因为 port 0 后面可能挂着一个慢设备。可 AXI 规定「同 ID 同方向必须保序」，于是 slave 收到的 B 响应顺序就违反了协议。

因此 demux 不能「无脑转发」。它必须知道：**当前有没有一笔相同 ID 的事务还在途？如果有的话，它正在去往哪个端口？** 只有当新事务要么 ID 不冲突、要么和在途的那笔去往**同一个端口**时，才允许放行；否则就停顿（stall）AW/AR 通道，直到前者完成。这正是 `axi_demux_id_counters` 存在的意义。

> 注意区分：W 通道的保序（W 拍要跟在其 AW 之后、不同突发的 W 拍不能交错）由上一讲介绍的 `i_counter_open_w` + `w_select_q` 负责；id_counters 负责的是 **B/R 响应**的保序。源码注释也明说："The counters are there for the Handling of the B responses."（见 [src/axi_demux_simple.sv:173-175](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L173-L175)）。

#### 4.1.2 核心流程

保序判定的决策树（针对一笔新到来的 AW；AR 同理）：

```
新 AW 到来，取其 ID 的低 AxiLookBits 位作为索引 i，查 id_counter[i]：
  ├─ occupied[i] == 0            → 该 ID 没有在途事务 → 放行
  └─ occupied[i] == 1：
        ├─ lookup_select[i] == 本 AW 的目标端口 → 同端口，B 不会乱序 → 放行
        └─ lookup_select[i] != 本 AW 的目标端口 → 不同端口，会乱序 → 停顿 AW
```

被停顿的 AW 会在后续周期里反复重试，直到在途那笔收到 B 响应、计数器归位。

#### 4.1.3 源码精读

上述判定正是 AW 通道放行条件的一部分。看 `axi_demux_simple` 的 AW 控制块（`axi_demux.sv` 只是加 spill，逻辑都在 simple 里）：

[src/axi_demux_simple.sv:175-177](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L175-L177) —— 注意第二个 `||` 分支里的 `(!aw_select_occupied || (slv_aw_select_i == lookup_aw_select))`，这就是上面决策树的直接翻译：要么该 ID 没占用，要么占用但目标端口一致。

AR 通道的对应判定在 [src/axi_demux_simple.sv:325-326](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L325-L326)，结构完全对称。

#### 4.1.4 代码实践

**实践目标**：用源码确认「同 ID 不同端口会被 stall」这件事确实由 id_counters 驱动。

**操作步骤**：

1. 打开 [src/axi_demux_simple.sv:164-194](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L164-L194) 的 AW 控制块。
2. 找到决定 `aw_valid` 是否拉高的那一段（约 175-177 行）。
3. 列出该 `if` 里的三个串联条件：`!aw_id_cnt_full`、`w_open` 相关的 W 队列条件、以及 id_counter 的占用/端口一致条件。

**需要观察的现象 / 预期结果**：你会看到 AW 要放行，必须同时满足「计数器没满」「W 队列不冲突」「ID 计数器不冲突或端口一致」三类条件。其中第三类直接来自 `i_aw_id_counter` 的 `lookup_mst_select_occupied_o` 与 `lookup_mst_select_o` 两个输出。

#### 4.1.5 小练习与答案

**练习 1**：如果两个 master 端口后面挂的设备速度一样快、B 响应永远按序返回，demux 还需要 id_counters 吗？

> **答案**：仍然需要。demux 自己无法预知下游设备的行为，更无法保证下游一定按序；它必须从结构上保证「无论下游如何，同 ID 同方向的事务只可能被路由到同一个端口」，从而把保序义务收敛到单端口内。id_counters 就是这个结构性保证。

**练习 2**：为什么 W 通道的保序需要**另一套**机制（`i_counter_open_w`）而不是复用 id_counters？

> **答案**：因为 W 拍没有自己的 ID，也没有 select 输入，它必须「跟随」自己的 AW。id_counters 跟踪的是事务级（AW→B）的在途与端口归属，而 W 通道需要的是「按 AW 顺序排队的端口选择 FIFO」，粒度不同、信息来源也不同（W 的目标端口来自 AW 时刻的 `slv_aw_select_i`），所以用独立的 `w_open` 计数器 + `w_select_q` 寄存器实现。

### 4.2 axi_demux_id_counters 模块精读

#### 4.2.1 概念说明

`axi_demux_id_counters` 是一个**自包含的、可被复用的**计数器阵列模块。它的设计目标是：用一个模块同时服务 AW 方向和 AR 方向（demux 例化两份），因此把所有「AXI 语义」剥离到外部，自身只暴露通用的四组操作端口。

核心数据结构有两块：

1. **计数器阵列**：\( \text{NoCounters} = 2^{\text{AxiIdBits}} \) 个加减计数器，每个跟踪某个（截断后的）ID 当前有多少笔在途事务。
2. **端口选择寄存器阵列**：`mst_select_q[i]`，记忆「ID `i` 的在途事务正在去往哪个 master 端口」。只有当计数器从 0 变 1（首次 push）时才写入，之后同 ID 的后续事务只读它来比对。

#### 4.2.2 核心流程

模块把操作分成四类，每一类都用「一位热码（one-hot）使能」作用到对应索引的计数器上：

```
push_axi_id_i   ──> push_en   = (1 << push_axi_id_i)    // 该 ID 计数器 +1
inject_axi_id_i ──> inject_en = (1 << inject_axi_id_i)  // 该 ID 计数器 +1（ATOP 注入）
pop_axi_id_i    ──> pop_en    = (1 << pop_axi_id_i)     // 该 ID 计数器 -1
lookup_axi_id_i ──> 只读 mst_select_q[lookup_axi_id_i] 与 occupied[lookup_axi_id_i]
```

每个计数器在**同一个周期**里可能同时被多种操作命中（例如同时 inject 和 pop），所以内部用一个 `unique case ({push_en[i], inject_en[i], pop_en[i]})` 算出该周期的净增量 `cnt_delta` 和方向 `cnt_down`，再交给 `delta_counter` 一次更新。

#### 4.2.3 源码精读

**模块端口与参数**——注意 `AxiIdBits` 决定计数器个数，`CounterWidth` 决定每个计数器能数到多大，`mst_port_select_t` 是「目标端口索引」的类型（demux 里就是 `select_t`）：

[src/axi_demux_id_counters.sv:19-44](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_id_counters.sv#L19-L44) —— 四组端口：lookup（只读）、push（+1 并登记端口）、inject（ATOP 注入 +1）、pop（-1），外加状态输出 `full_o` 与 `any_outstanding_trx_o`。

**计数器个数与端口选择寄存器阵列**：

[src/axi_demux_id_counters.sv:45-52](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_id_counters.sv#L45-L52) —— `NoCounters = 2**AxiIdBits`；`mst_select_q` 是每个计数器配一个的「目标端口」寄存器。

**Lookup（纯组合读）与 one-hot 使能生成**：

[src/axi_demux_id_counters.sv:57-65](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_id_counters.sv#L57-L65) —— `lookup_mst_select_o` 直接索引 `mst_select_q`；`push_en/inject_en/pop_en` 用移位把 ID 变成 one-hot 向量。`full_o = |cnt_full` 只要任一计数器满即拉高。

**每个计数器的净增量决策**——这是模块最精巧的部分，用一个 `unique case` 覆盖同周期多操作同时命中的所有组合：

[src/axi_demux_id_counters.sv:76-110](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_id_counters.sv#L76-L110) —— 例如 `3'b110`（push+inject）算 `+2`，`3'b111`（三者同时）算净 `+1`，注释里明确标出每个编码的语义。

**底层计数器与状态派生**：

[src/axi_demux_id_counters.sv:112-128](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_id_counters.sv#L112-L128) —— 例化 `delta_counter`（`STICKY_OVERFLOW=0`），`occupied[i] = |in_flight`（非零即占用），`cnt_full[i] = overflow | (&in_flight)`（溢出或全 1 即满）。

**端口选择寄存器的写入**——只有 push 时写，inject/pop 不改端口归属：

[src/axi_demux_id_counters.sv:131](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_id_counters.sv#L131) —— `` `FFLARN(mst_select_q[i], push_mst_select_i, push_en[i], '0, clk_i, rst_ni) ``，即「在 `push_en[i]` 有效时把 `push_mst_select_i` 锁进寄存器」。

**下溢断言**——pop 把计数器减到溢出说明协议被破坏（响应多于请求），仿真期直接 `$fatal`：

[src/axi_demux_id_counters.sv:137-140](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_id_counters.sv#L137-L140)。

#### 4.2.4 代码实践

**实践目标**：把模块的「四操作」与 demux 实际接线对应起来。

**操作步骤**：

1. 在 [src/axi_demux_id_counters.sv:62-64](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_id_counters.sv#L62-L64) 记下 `push_en/inject_en/pop_en` 的生成公式。
2. 跳到 [src/axi_demux_simple.sv:210-229](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L210-L229)（AW 计数器例化），逐一对照：
   - `push_i` 连的是 `w_cnt_up`（AW 决策做出时 +1）；
   - `pop_i` 连的是 `slv_resp_o.b_valid & slv_req_i.b_ready`（B 握手时 -1）；
   - `inject_i` 连的是常量 `1'b0`（AW 计数器不需要注入）。

**预期结果**：你会看到「每一笔放行的 AW 对应一次 push、每一次 B 握手对应一次 pop」，这正是「在途计数」的字面含义。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `mst_select_q[i]` 只在 `push_en[i]` 时写入，而 inject 不写？

> **答案**：push 发生在一笔新事务被 demux 接收并确定目标端口的时刻，此时「ID `i` 的端口归属」可能首次确立或变更（计数器从 0 到 1 时确立）。inject 是 AW 通道向 AR 计数器的「借用」——它告诉 AR 计数器「即将有一笔没有对应 AR 的 R 响应回来」，目的是抵消未来的 pop，并不改变任何读事务的实际目标端口（读事务的端口归属仍由真实的 AR push 决定），所以不写 `mst_select_q`。

**练习 2**：`cnt_full[i] = overflow | (&in_flight)` 里，`(&in_flight)`（全 1）和 `overflow` 有何区别？为什么要或起来？

> **答案**：`delta_counter` 的 `overflow_o` 在「本次加法超出位宽」时拉高一拍；而 `&in_flight` 是「当前值已到全 1」的稳态判断。两者或起来保证：无论计数器是「刚刚溢出」还是「已经顶满」，对外都报 full，从而让 demux 提前停顿 AW/AR，避免再 push。

### 4.3 demux 主体如何挂载两个计数器实例（AW / AR + ATOP 注入）

#### 4.3.1 概念说明

`axi_demux_simple` 例化**两份** `axi_demux_id_counters`：一份给写方向（`i_aw_id_counter`，跟踪 AW→B），一份给读方向（`i_ar_id_counter`，跟踪 AR→R）。两份的参数 `AxiIdBits` 都绑成 `AxiLookBits`（即只用 ID 的低位做索引），`CounterWidth` 都绑成 `IdCounterWidth`。

关键差异在 **inject** 端口：

- AW 计数器的 `inject_i` 恒为 `1'b0`——写方向没有「无源头响应」。
- AR 计数器的 `inject_i` 接 `atop_inject`，而 `atop_inject` 在「一笔带 `ATOP_R_RESP`（原子读改写，会产生 R 响应）的 AW 握手」时拉高。这就是文档所说的 *AW channel can "inject" the ID of an atomic load to the ID counter of the AR channel*。

为什么需要注入？原子读改写（atomic load）会在 R 通道产生响应，却**没有对应的 AR**。如果不通知 AR 计数器，当这个 R 响应回来触发 pop 时，AR 计数器就会下溢（变成 −1），触发 [src/axi_demux_id_counters.sv:137-140](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_id_counters.sv#L137-L140) 的 fatal 断言。注入一笔 +1 正好抵消未来的那一次 pop。

AXI 规定原子事务的 ID 对**所有在途事务（读+写）**唯一，因此把 AW 的 ID 注入 AR 计数器不会与真实读事务的 ID 冲突——这是注入安全性的前提。

#### 4.3.2 核心流程

```
AW 通道来了一笔 aw.atop[ATOP_R_RESP] == 1 的原子写：
  1. AW 握手成功 (aw_valid & aw_ready) 时，控制块置 atop_inject = 1
  2. atop_inject 同时作用于：
       - i_aw_id_counter：正常 push（+1），用 AW 的 id
       - i_ar_id_counter：inject（+1），用同一 AW 的 id（注意是 AW 的 id，不是 AR 的）
  3. 后续这笔原子写的 R 响应回来时，AR 计数器 pop（-1），与之前的 +1 抵消，不下溢
```

注意 [src/axi_demux_simple.sv:367-368](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L367-L368)：AR 计数器的 `inject_axi_id_i` 接的是 `slv_req_i.aw.id[...]`（AW 的 id），`inject_i` 接 `atop_inject`。

#### 4.3.3 源码精读

**`IdCounterWidth` 的派生**——计数器位宽由 `MaxTrans` 决定：

[src/axi_demux_simple.sv:69-70](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L69-L70) —— `IdCounterWidth = cf_math_pkg::idx_width(MaxTrans)`。按文档 *Implementation* 节，每个计数器可计到（含）`MaxTrans`。

**`atop_inject` 的产生**——在 AW 握手成功的两个分支里各置一次：

[src/axi_demux_simple.sv:162](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L162) 与 [src/axi_demux_simple.sv:185](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L185) —— 两者都写成 `atop_inject = slv_req_i.aw.atop[axi_pkg::ATOP_R_RESP] & AtopSupport`，即「本笔 AW 是会产生 R 响应的原子写，且本 demux 支持原子」。

**AW 计数器例化（无注入）**：

[src/axi_demux_simple.sv:210-229](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L210-L229) —— `AxiIdBits(AxiLookBits)`、`CounterWidth(IdCounterWidth)`；`inject_i(1'b0)`；push 用 AW 的 id，pop 用 B 的 id。

**AR 计数器例化（带 ATOP 注入）**：

[src/axi_demux_simple.sv:356-375](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L356-L375) —— 注意 `inject_axi_id_i(slv_req_i.aw.id[...])`、`inject_i(atop_inject)`；push 用 AR 的 id；pop 用 R 的 id **且加了 `& slv_resp_o.r.last`**——只有一拍的普通读或突发的最后一拍才 pop 一次。

**保序停顿条件**——AR 方向，复用计数器的两个输出：

[src/axi_demux_simple.sv:323-326](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L323-L326) —— `!ar_id_cnt_full`（计数器没满）且 `(!ar_select_occupied || slv_ar_select_i == lookup_ar_select)`（同 ID 不冲突或同端口）才放行 AR。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 ATOP 原子写在两个计数器里分别引发的变动。

**操作步骤**：

1. 假设一笔 `aw.id = 5'd3`、`aw.atop[ATOP_R_RESP] = 1` 的原子写在周期 T 握手成功。
2. 在 [src/axi_demux_simple.sv:210-229](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L210-L229) 推断：`i_aw_id_counter` 对 id 索引 3 做一次 push（+1），并把端口选择写入 `mst_select_q[3]`。
3. 在 [src/axi_demux_simple.sv:356-375](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L356-L375) 推断：同一周期 `i_ar_id_counter` 对 id 索引 3 做一次 inject（+1），但**不**写 `mst_select_q`。
4. 设想这笔原子写的 R 响应在周期 T+k 回来（`r.last=1`）：AR 计数器对索引 3 pop（−1）；某周期 B 响应回来：AW 计数器对索引 3 pop（−1）。两者各自归零。

**需要观察的现象 / 预期结果**：因为 push/inject 与 pop 一一配对，两个计数器都不会下溢；若没有 inject，AR 计数器会在 R 响应时下溢并触发 fatal 断言。待本地验证（用 `test/tb_axi_atop_filter.sv` 同类激励或自建最小 tb 观察断言是否触发）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 AR 计数器的 pop 条件要多一个 `& slv_resp_o.r.last`，而 AW 计数器的 pop 不需要类似条件？

> **答案**：一笔读事务可能产生**多拍** R 响应（突发），但只对应一次「事务完成」，所以只在最后一拍 `r.last` pop 一次。写事务的 B 响应永远只有**一拍**，所以 B 握手即事务完成，不需要额外条件。

**练习 2**：如果 `AtopSupport = 0`，注入逻辑会发生什么？

> **答案**：`atop_inject = ... & AtopSupport` 恒为 0，AR 计数器的 `inject_i` 永不拉高。同时模块有断言 `NoAtopAllowed`（[src/axi_demux_simple.sv:507](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L507)）要求此时 AW 不得携带非零 atop——即关闭 ATOP 支持时，上游必须保证不送原子事务。

### 4.4 参数取舍：AxiLookBits、MaxTrans、UniqueIds

#### 4.4.1 概念说明

demux 暴露三个与 id_counters 强相关的参数，它们直接决定面积、并发上限与正确性前提：

| 参数 | 含义 | 取舍 |
|:--|:--|:--|
| `AxiLookBits` | 用 ID 的低多少位做计数器索引，决定计数器个数 \(2^{\text{AxiLookBits}}\) | 大 → 区分 ID 更精细、误冲突少，但面积指数增长；小 → 面积小但不同 ID 可能被当作同 ID 而误停顿。必须 ≤ `AxiIdWidth`。 |
| `MaxTrans` | slave 端口允许的最大在途事务数，决定每个计数器的位宽 `IdCounterWidth` | 大 → 并发高、吞吐好，但计数器更宽、`mst_select_q` 等资源略增；同时受下游容量约束。 |
| `UniqueIds` | 若能保证「同方向在途事务 ID 唯一」或「同 ID 同方向必去同一端口」，可置 1 | 置 1 → **整个计数器阵列被省掉**，面积从 \(O(2^{I})\) 降到 \(O(I)\)；但前提不满足时行为未定义。 |

`UniqueIds` 的优化尤其激进：它不是「简化」计数器，而是**整组删除**，用三个简单 assign 替代。

#### 4.4.2 核心流程

当 `UniqueIds = 1` 时，原本由计数器提供的三个信号改为：

```
lookup_*_select    = slv_*_select_i   // 目标端口就是当前 select，无需查表
*_select_occupied  = 1'b0             // 永远不报「占用」，即不触发保序停顿
*_id_cnt_full      = 1'b0             // 永远不报「满」，即不因计数器满而停顿
```

由于 `*_select_occupied = 0`，4.1 节决策树里的「占用且端口不同」分支永远不会走到，AW/AR 不再因同 ID 保序而停顿——这在前提满足时是合法的，因为同 ID 同方向既然只去同一个端口（或根本唯一），就不存在跨端口乱序风险。

面积上，文档给出明确结论：置 `UniqueIds=1` 把复杂度从 \( O(2^{I}) \) 降到 \( O(I) \)，其中 \( I \) 是 ID 宽度（见 [doc/axi_demux.md:68](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_demux.md#L68)）。

#### 4.4.3 源码精读

**AW 方向的 UniqueIds 分支**——用 `if (UniqueIds) begin : gen_unique_ids_aw ... end else begin : gen_aw_id_counter`：

[src/axi_demux_simple.sv:200-208](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L200-L208) —— 三行 assign 替代整个 `i_aw_id_counter` 实例。

[src/axi_demux_simple.sv:209-231](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L209-L231) —— `else` 分支才真正例化 `axi_demux_id_counters`。

**AR 方向完全对称**：

[src/axi_demux_simple.sv:346-354](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L346-L354) —— `gen_unique_ids_ar` 同样三行 assign。

**`UniqueIds` 的安全前提**——文档用 "if and only if" 列出三个条件（任一满足即可）：

[src/axi_demux.md:60-66](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_demux.md#L60-L66) —— (1) 同方向在途事务 ID 两两不同；或 (2) 同 ID 同方向的事务都去同一个端口；或两者兼有。文档明确：「Setting the `UniqueIds` parameter to `1'b1` when those conditions are not always met leads to undefined behavior.」

**xbar 如何向 demux 传递这些参数**——证实它们是设计层面的可配置项：

[src/axi_xbar_unmuxed.sv:175-177](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L175-L177) —— `MaxTrans(Cfg.MaxMstTrans)`、`AxiLookBits(Cfg.AxiIdUsedSlvPorts)`、`UniqueIds(Cfg.UniqueIds)`。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：对照源码说清「`UniqueIds=1` 时 demux 省掉了哪些逻辑」，并解释安全置位的前提。这是本讲规格指定的代码实践任务。

**操作步骤**：

1. 打开 [src/axi_demux_simple.sv:200-231](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L200-L231)（AW 方向）和 [src/axi_demux_simple.sv:346-376](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L346-L376)（AR 方向）。
2. 列出 `UniqueIds=1` 时被省掉的硬件，逐一对应到 [src/axi_demux_id_counters.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_id_counters.sv) 的内部资源。
3. 对照 [doc/axi_demux.md:60-66](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_demux.md#L60-L66) 写下安全前提。

**需要观察的现象 / 预期结果**（应得到类似下面的清单）：

`UniqueIds=1` 时，AW 与 AR 各省掉一个 `axi_demux_id_counters` 实例，等价于删除：

- \( 2 \times 2^{\text{AxiLookBits}} \) 个 `delta_counter`（每个 `CounterWidth` 位宽）；
- \( 2 \times 2^{\text{AxiLookBits}} \) 个 `mst_select_q` 端口选择寄存器（每个 `select_t` 位宽）；
- 每个 counter 的 `unique case` 增量决策逻辑、`occupied`/`cnt_full` 派生逻辑、以及下溢断言。

替换为 6 条 `assign`（AW/AR 各 3 条）。保序停顿条件里的 `*_select_occupied` 恒为 0，使得「同 ID 不同端口」停顿分支永远不被触发；`*_id_cnt_full` 恒为 0，使得「计数器满」停顿永不发生。

**安全置位 `UniqueIds=1` 的前提**（满足任一即可，来自 [doc/axi_demux.md:61-64](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_demux.md#L61-L64)）：

1. 每笔事务的 ID 在同方向所有在途事务中**唯一**（最常见：上游经 `axi_serializer`/`axi_id_remap` 已把 ID 压到唯一）；或
2. 对任意 ID，所有该 ID 同方向的事务**永远去同一个 master 端口**（例如地址映射保证某 ID 区间只命中一个下游）；或
3. 以上两者兼有。

若任一前提都不成立（例如两个 CPU 用同一 ID 并发访问两个不同地址区间），置位会导致 B/R 响应跨端口乱序——即文档所说的 undefined behavior。

#### 4.4.5 小练习与答案

**练习 1**：把 `AxiLookBits` 从 3 调到 4，计数器总数变化多少？误冲突概率如何变？

> **答案**：计数器总数从 \(2 \times 2^{3} = 16\) 增加到 \(2 \times 2^{4} = 32\)，翻倍。更多计数器意味着 ID 区分更细，两个不同 ID 被当作同 ID（低位相同）而误停顿的概率降低，但面积指数增长。这是典型的面积 vs. 性能折中。

**练习 2**：一个设计里上游已经接了 `axi_id_remap`，把所有事务的 ID 重映射成在途唯一。这时 demux 的 `UniqueIds` 可以置 1 吗？为什么？

> **答案**：可以。`axi_id_remap` 保证在途事务 ID 唯一（满足前提 1），因此 demux 不可能看到「同 ID 同方向、不同端口」的情况，保序停顿永远不需要触发，id_counters 可安全删除。这正是 id_remap + demux 的常见组合优化点（参见 u10-l1）。

## 5. 综合实践

把本讲四块知识（动机、模块、协作、参数）串成一条完整的调用链追踪。

**任务**：为一个 `NoMstPorts=2`、`AxiLookBits=2`、`MaxTrans=8`、`UniqueIds=0` 的 demux，分析下面场景并在源码中标注每一步的依据：

> 周期 T0：slave 收到 `AW0`，`id=4'd3`，`addr` 译码后 `slv_aw_select_i=0`（去 port 0）。假设此时 id_counters 全空。
> 周期 T1：`AW0` 的 W 突发尚未发完，slave 又收到 `AW1`，`id=4'd3`（**同 ID**），`slv_aw_select_i=1`（去 port 1）。

**要求**：

1. 指出 T0 时 `i_aw_id_counter` 发生了什么操作（push/inject/pop？作用于哪个索引？写入哪个 `mst_select_q`？）。
2. 指出 T1 时 [src/axi_demux_simple.sv:175-177](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_simple.sv#L175-L177) 的判定结果：`aw_select_occupied` 为何值？`lookup_aw_select` 为何值？`AW1` 是否被放行？为什么？
3. 说明 `AW1` 在哪个事件发生后才会被放行（即计数器何时归位）。
4. 若把 `UniqueIds` 改为 1 重新跑同样激励，会出什么问题？用 [doc/axi_demux.md:60-66](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_demux.md#L60-L66) 解释为什么此前提不成立。

**参考答案要点**：

1. T0：`AW0` 触发一次 **push**，作用于 id 索引 `3`（`aw.id[0+:2]`），把 `mst_select_q[3]` 写为 `0`（port 0）；`occupied[3]` 变为 1。
2. T1：`aw_select_occupied = occupied[3] = 1`，`lookup_aw_select = mst_select_q[3] = 0`，而 `slv_aw_select_i = 1`。两者不等，`(!aw_select_occupied || ...)` 为假，`AW1` 被 **stall**（`aw_valid` 不拉高，slave 侧 `aw_ready` 也不给）。
3. 当 `AW0` 的 B 响应回到 slave（`b_valid & b_ready`，且 `b.id[0+:2]==3`）时，`i_aw_id_counter` 对索引 3 **pop**，若该 ID 没有其他在途事务则 `occupied[3]` 归 0，`AW1` 解除停顿。
4. `UniqueIds=1` 时 `aw_select_occupied` 恒为 0，`AW1` 会立即放行去 port 1，于是 port 0 与 port 1 各有一笔 `id=3` 的写在途，B 响应可能乱序返回——违反保序。此时场景不满足前提（同 ID 同方向既不唯一、又去了不同端口），故行为未定义。

> 进阶（可选）：参考 `test/tb_axi_xbar.sv` 的拓扑（xbar 内部就含 demux），把上述 demux 单独抽出搭一个 `rand_master → axi_demux(2 ports) → 2×axi_sim_mem` 的最小测试台，用 scoreboard 跑随机读写。先确认 `UniqueIds=0` 通过；再改成 `UniqueIds=1` 复跑，观察是否会出现 B 响应顺序异常（scoreboard 报错或协议断言触发）。这一步待本地验证。

## 6. 本讲小结

- demux 必须为每个（截断后的）AXI ID 跟踪在途事务数与目标端口，否则同 ID 同方向的事务被拆到不同端口后，B/R 响应可能跨端口乱序，违反 AXI 保序规则。
- `axi_demux_id_counters` 是一个 \(2^{\text{AxiLookBits}}\) 元的计数器阵列，每元含一个 `delta_counter` 和一个 `mst_select_q` 端口寄存器，提供 lookup/push/pop/inject 四操作；`unique case` 让同周期多操作命中时也能算出正确的净增量。
- `axi_demux_simple` 例化**两份**该模块：AW 方向（push 于 AW 决策、pop 于 B 握手）、AR 方向（push 于 AR 握手、pop 于最后一拍 R、并接收来自 AW 的 ATOP inject）。两份的 `AxiIdBits` 都是 `AxiLookBits`，`CounterWidth` 都是 `cf_math_pkg::idx_width(MaxTrans)`。
- ATOP 原子读改写会「无 AR 而有 R」，所以 AW 通道在握手时向 AR 计数器 inject 一笔 +1 抵消未来 pop；其安全性建立在「原子事务 ID 对所有在途事务唯一」之上。
- `AxiLookBits` 调面积/误冲突，`MaxTrans` 调并发上限/计数器位宽，`UniqueIds=1` 则用 6 条 assign **整组删除**计数器阵列（面积 \(O(2^{I}) \to O(I)\)），但仅在「同方向在途 ID 唯一」或「同 ID 同方向必去同端口」时才安全。
- 本讲澄清了一个常见混淆：W 通道保序由独立的 `i_counter_open_w` + `w_select_q` 负责，id_counters 只服务 B/R 响应的保序。

## 7. 下一步学习建议

- **向「汇聚」侧走**：下一篇 u5-l3 讲 `axi_mux`（多路汇聚），它和 demux 是 xbar 的对称两半。建议对照阅读，特别注意 mux 如何用 ID 高位携带源端口索引——这与本讲 demux 的 `mst_select_q` 是一对镜像关系。
- **向「上层组合」走**：u6-l1（xbar 架构）会把 demux 阵列 + mux 阵列拼成完整交叉开关，届时你会看到 `AxiLookBits`（即 `Cfg.AxiIdUsedSlvPorts`）和 `UniqueIds` 在系统级的影响。
- **向「ID 管理」走**：u10-l1（`axi_id_remap`）正是制造「在途 ID 唯一」这一前提的模块，理解它之后你会更清楚何时能把 demux 的 `UniqueIds` 安全置 1。
- **源码延伸阅读**：重读 [doc/axi_demux.md](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_demux.md) 的 *Ordering and Stalls* 与 *Atomic Transactions* 两节，并顺着 [src/axi_demux_id_counters.sv:112-126](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux_id_counters.sv#L112-L126) 去看外部依赖 `delta_counter` 的实现（位于 `common_cells`），巩固对底层计数原语的理解。
