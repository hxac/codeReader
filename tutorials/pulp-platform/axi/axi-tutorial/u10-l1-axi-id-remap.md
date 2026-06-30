# axi_id_remap：ID 重映射

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚「为什么需要把宽 ID 重映射成窄 ID」这个真实工程问题，以及它与 `axi_serializer`（u9-l3）、`axi_id_serialize`（u10-l2）的本质区别。
- 看懂 `axi_id_remap` 内部那张「输出 ID 索引 → (输入 ID, 在途计数)」的重映射表是如何完成**分配、查找、回收**三件事的。
- 解释窄 ID 端口的容量上限（`AxiSlvPortMaxUniqIds`）如何反过来限制上游可同时并发的不同 ID 数，以及在什么条件下产生反压。
- 复述响应路径如何利用窄 ID 反查回原始宽 ID，从而保证「上游无感」。

本讲承接 u5-l2（`axi_demux_id_counters` 的在途跟踪思想），并把「ID 管控」从互联内部抬升到一个独立的、可独立例化的胶水模块。

## 2. 前置知识

阅读本讲前，请确认你已经了解：

- **AXI ID 的作用**：同一根 AXI 总线上，ID 用来给事务分组——同 ID 同方向（读或写）的事务必须**按序**返回响应，不同 ID 之间可以**乱序**交错。ID 越宽，能并行保序的「流」就越多，但下游（尤其是某些存储控制器、协议桥）往往只支持窄 ID。
- **在途事务（in flight / outstanding）**：地址拍已握手、但响应（B 或最后一拍 R）尚未握手的事务（回顾 u1-l3、u5-l2）。
- **`req_t` / `resp_t` 结构体**与「接口外壳 + 结构体内核」范式（u2-l4）。
- **ATOP 与 `ATOP_R_RESP`**：原子操作编码挂在 AW 上；其中 `ATOP_R_RESP`（atop 第 5 位）那一类原子写会**同时**产生 B 响应和 R 响应（回顾 u2-l1、u15-l1）。
- **lzc（leading-zero counter）**：输入一个位向量，输出最低位被置 1 的那个下标；若全 0 则 `empty_o` 拉高。本库用它实现「找最低的空闲槽位」。
- **`cf_math_pkg::idx_width(n)`**：返回索引 `n` 个表项所需的位宽，即 \(\lceil\log_2 n\rceil\)（\(n>1\) 时）。

> 提示：本讲的「表」和 u5-l2 的 `id_counters` 形似神不同——后者只数「同 ID 在途几笔」，前者还要建立「输入 ID ↔ 输出 ID」的双向映射。注意区分。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但它内含两个模块与一个接口外壳：

| 文件 / 模块 | 作用 |
| --- | --- |
| [src/axi_id_remap.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv) — `axi_id_remap` | 顶层：把 slave 端宽 ID 重映射为 master 端窄 ID，处理 AX 握手、ATOP、保序与反压。 |
| 同文件 — `axi_id_remap_table` | 内部子模块：维护一张「输出 ID → (输入 ID, 计数)」的表，提供 free / push / exists / pop 四种操作。 |
| 同文件 — `axi_id_remap_intf` | 接口外壳：用 `AXI_BUS.Slave` / `AXI_BUS.Master` 包裹结构体内核，方便手搭测试台。 |

它在依赖层级中处于较高位置（被 `axi_iw_converter` 例化，参见 [src/axi_iw_converter.sv:128-138](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L128-L138)），是异构网络里「窄下游接宽上游」的典型胶水。

## 4. 核心概念与源码讲解

### 4.1 为什么要重映射 ID

#### 4.1.1 概念说明

很多 AXI master（比如某些 CPU 核）声明了很宽的 ID 端口（如 8 位，理论上 256 个不同 ID），但运行期实际只用了稀疏的一小撮（可能只用 4~8 个、还不一定连续）。如果下游设备只能处理窄 ID（比如只认 2 位 ID 的存储控制器），直接相连就会**截断高位**，导致原本不同的上游 ID 被映射成同一个下游 ID——这会破坏 AXI 的保序契约（不同 ID 本可乱序，被迫同 ID 后被错误地要求保序），引发功能 bug。

`axi_id_remap` 解决的就是这个问题：它在运行期动态地为每个**正在在途的**上游 ID 分配一个紧凑的下游 ID，并且**保留 ID 的独立性**——两个不同的上游 ID，在下游也一定是两个不同的 ID。模块开头的设计说明把这件事讲得很清楚：

> This module is designed to remap an overly wide, sparsely used ID space to a narrower, densely used ID space. … This module retains the independence of IDs.
> —— [src/axi_id_remap.sv:19-29](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L19-L29)

「保留独立性」这条性质直接决定了下游 ID 的**宽度下限**：要能区分 `AxiSlvPortMaxUniqIds` 个不同上游 ID，下游 ID 至少得有 \(\lceil\log_2(\text{AxiSlvPortMaxUniqIds})\rceil\) 位。如果你愿意**放弃**独立性、允许把不同上游 ID 压成同一个下游 ID（牺牲乱序并行），那就该改用 `axi_id_serialize`（u10-l2）——模块注释也明确指路了（见 L28-L29）。

#### 4.1.2 核心流程

整体数据流是「请求方向改 ID、响应方向还原 ID」，其余字段（地址、数据、strobe、user、atop…）全部原样直通：

```text
            slave 端 (宽 ID)                      master 端 (窄 ID)
AW.id ─────────────────┐   ┌── AW.id  = 重映射后的窄 ID
AR.id ───┐             │   │
         │   axi_id_remap
         └── AR.id  = 重映射后的窄 ID
                         │   │
B.id  ◄── 还原成原宽 ID ◄┘   └── B.id  (来自下游, 窄 ID)
R.id  ◄── 还原成原宽 ID ◄──── R.id  (来自下游, 窄 ID)
```

请求方向：拿到上游 `aw.id`/`ar.id` → 查表 → 决定一个窄 ID → 替换进 `mst_req_o.aw.id`/`ar.id` 向下游转发。
响应方向：下游回来的 B/R 带的是窄 ID → 用窄 ID 当索引查表 → 取出原宽 ID → 还原进 `slv_resp_o.b.id`/`r.id` 还给上游。

关键参数有三个（其余是类型参数）：

| 参数 | 含义 | 约束 |
| --- | --- | --- |
| `AxiSlvPortIdWidth` | slave（上游）端 ID 宽度 | > 0 |
| `AxiSlvPortMaxUniqIds` | 单方向（读或写）最多多少个**不同**上游 ID 同时在途 | ≤ \(2^{\text{AxiSlvPortIdWidth}}\) |
| `AxiMaxTxnsPerId` | 同一个 ID 最多多少笔同时在途 | > 0 |
| `AxiMstPortIdWidth` | master（下游）端 ID 宽度 | ≥ `IdxWidth`（见下） |

参数定义与注释见 [src/axi_id_remap.sv:33-57](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L33-L57)。

#### 4.1.3 源码精读

非 ID、非流控的字段是一大段纯 `assign` 直通，AW 通道就有 10 个字段逐个搬（[L90-L100](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L90-L100)），W/B/AR/R 同理（[L102-L127](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L102-L127)）。注意：**W 通道整块直通**（`assign mst_req_o.w = slv_req_i.w;`），因为 W 拍不带 ID，它的归属完全由其前置 AW 决定，而 AW 的窄 ID 已经写进下游了——这要求下游必须按「同窄 ID 的 AW → 同窄 ID 的 W」严格配对，AXI 协议天然保证。

ID 位宽的派生关系定义在 [L131](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L131)：

```systemverilog
localparam int unsigned IdxWidth = cf_math_pkg::idx_width(AxiSlvPortMaxUniqIds);
```

即

\[
\text{IdxWidth} = \lceil \log_2(\text{AxiSlvPortMaxUniqIds}) \rceil
\]

它既是表的索引位宽，也是窄 ID 实际承载信息的位数。若 `AxiMstPortIdWidth > IdxWidth`，多余的高位补零（[L197-L200](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L197-L200)）：

```systemverilog
localparam ZeroWidth = AxiMstPortIdWidth - IdxWidth;
assign mst_req_o.ar.id = {{ZeroWidth{1'b0}}, rd_push_oup_id};
assign mst_req_o.aw.id = {{ZeroWidth{1'b0}}, wr_push_oup_id};
```

参数合法性由一组 `assert` 守护，其中最关键的两条是 ID 宽度下限与「不同 ID 数不超过 ID 空间」：[L426-L433](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L426-L433)。

#### 4.1.4 代码实践

**实践目标**：用纸笔（或一段伪代码）验证「保留独立性」对窄 ID 宽度的硬约束。

**操作步骤**：
1. 假设上游声明 8 位 ID，但你估计运行期最多 5 个不同 ID 同时在途 → `AxiSlvPortMaxUniqIds = 5`。
2. 计算 `IdxWidth = ⌈log₂ 5⌉ = 3`。
3. 回答：master 端 ID 至少要几bit？若下游只给 2 位 ID 会怎样？

**预期结果**：至少 3 位（断言 `AxiMstPortIdWidth >= IdxWidth` 会在 2 位时报 `$fatal`）。若强行只用 2 位下游 ID 又要保留独立性，物理上不可能区分 5 个不同上游 ID——这正是需要切换到 `axi_id_serialize`（放弃独立性）的场景。

#### 4.1.5 小练习与答案

- **练习 1**：为什么不能用「直接截断 AW.id 的低位」来做窄化？
  - **答**：截断会让两个不同的上游 ID 落到同一个下游 ID，下游被迫按同 ID 保序，破坏协议；而且响应也无法还原回原 ID。
- **练习 2**：`AxiSlvPortMaxUniqIds = 1` 有没有意义？
  - **答**：有意义。它表示「同一时刻在途的只有 1 个不同 ID」，此时表只有 1 项、`IdxWidth=1`，相当于把任意上游 ID 都重映射成下游 ID `0`——退化为「单 ID 下游」的适配器（但仍按 ID 计数保证保序）。

---

### 4.2 重映射表：分配 / 查找 / 回收

#### 4.2.1 概念说明

重映射表 `axi_id_remap_table` 是整个模块的心脏。它是一张以**输出（窄）ID 为索引**的表，每项存两个字段：

```systemverilog
typedef struct packed {
  id_inp_t  inp_id;   // 这一项对应的原始上游 ID
  cnt_t     cnt;      // 该 (inp_id, oup_id) 配对下、当前在途几笔
} entry_t;
```

定义见 [L559-L563](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L559-L563)，表本体是 `entry_t [MaxUniqInpIds-1:0]`（[L566](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L566))。

设计要点是「**映射是单射（injective）**」：一个在途的上游 ID 在表里**最多出现一次**。因此表提供四种原子操作：

| 操作 | 触发时机 | 做什么 |
| --- | --- | --- |
| **free** | 组合查 | 给出「哪些项的 `cnt==0`（即空闲）」位图，以及最低空闲项的下标 `free_oup_id_o` |
| **exists** | 组合查 | 给定一个上游 ID，它是否已在表里？在的话对应哪个输出 ID？它的 `cnt` 是否已达 `MaxTxnsPerId` 上限？ |
| **push** | AX 握手当拍 | 把 (inp_id → oup_id) 写入/累加：若该 oup 项空闲则写入 `inp_id` 并 `cnt=1`；若已存在同 `inp_id` 项则 `cnt++` |
| **pop** | B 握手 / R.last 握手 | 用下游窄 ID 当索引，`cnt--`；`cnt` 归零即该项变空闲、输出 ID 可回收 |

模块文档对这张表的结构与复杂度有完整描述：[L476-L493](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L476-L493)。

#### 4.2.2 核心流程

**分配（push 时选输出 ID）**遵循一条优先级链：

1. 若上游 ID **已在表里**（`exists_o`）→ 必须**复用**它现有的输出 ID（否则同 ID 会被拆到两个下游 ID，破坏保序）；同时要求该项未达每 ID 上限（`!exists_full_o`）。
2. 若上游 ID **不在表里** → 取一个空闲输出 ID（`free_oup_id_o`），要求表未满（`!full_o`）。
3. 若「不在表里」且「表已满」→ **无法分配**，于是当拍**不置** `mst_req_o.ax_valid`，即对上游**反压**（`slv_resp_o.ax_ready` 保持 0）。这正是容量上限转化为反压的物理机制。

**回收（pop）**：写方向在 B 握手当拍 pop（[L162](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L162)），读方向在最后一拍 R（`r.last`）握手当拍 pop（[L183](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L183)）。pop 的索引就是下游响应里携带的窄 ID 的低 `IdxWidth` 位（`mst_resp_i.b.id[IdxWidth-1:0]`，[L163](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L163)）。pop 同时把该 entry 的 `inp_id` 经 `pop_inp_id_o` 吐回，顶层把它赋给 `slv_resp_o.b.id`——**这就是响应方向「窄 ID 还原成宽 ID」的全部魔法**。

一图总结单笔写事务的生命周期：

```text
AW 握手 ──push──► 表[oup] = {aw.id, cnt=1}      ── 同时 aw.id 被替换成 oup 下发
                                                    （W 拍随后按同 oup 走，无需查表）
B  握手 ──pop───► 表[oup].cnt-- ─► pop_inp_id = aw.id ──► 还原进 b.id 还给上游
                  （cnt 归 0，oup 槽位回收，可分配给新的上游 ID）
```

#### 4.2.3 源码精读

顶层例化了**两张独立的表**——写方向 `i_wr_table`（[L145-L165](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L145-L165))、读方向 `i_rd_table`（[L166-L186](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L166-L186))。读写在表层面完全隔离，互不挤占——这意味着读、写方向各自有独立的 `MaxUniqIds` 容量。

表内部的 `free` 判定与最低空闲下标用一个 lzc 实现（[L568-L579](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L568-L579))：`free_o[i] = (table_q[i].cnt == 0)`，`full_o` 就是 lzc 的 `empty_o`。`exists` 查找是另一个 lzc：先把「`cnt>0` 且 `inp_id` 匹配」的位置 1 得到 `match` 向量，再取最低位下标（[L588-L602](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L588-L602))。一个不变式是 `match` 必须 one-hot——「同一上游 ID 在表里唯一」，由断言 `$onehot0(match)` 守护（[L636-L637](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L636-L637))。

push / pop 的表更新逻辑只有寥寥几行（[L609-L619](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L609-L619))：

```systemverilog
always_comb begin
  table_d = table_q;
  if (push_i) begin
    table_d[push_oup_id_i].inp_id  = push_inp_id_i;
    table_d[push_oup_id_i].cnt    += 1;
  end
  if (pop_i) begin
    table_d[pop_oup_id_i].cnt -= 1;
  end
end
```

注意 push 永远 `cnt += 1`、pop 永远 `cnt -= 1`，安全的前提由 `assume` 断言兜底：push 目标必须是空项或同 ID 项、且未超 `MaxTxnsPerId`；pop 目标必须 `cnt > 0`（[L629-L635](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L629-L635))。这些 `assume` 约束的是**顶层 FSM 必须正确使用表**——表本身是个被动的数据结构，正确性由顶层保证。

复杂度（来自模块文档 [L489-L493](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L489-L493)）：

- 触发器：\(\text{MaxUniqInpIds} \times \text{InpIdWidth} \times \lceil\log_2(\text{MaxTxnsPerId}+1)\rceil\)；
- 比较器：\(\text{MaxUniqInpIds}\) 个，宽 \(\text{InpIdWidth}\)；
- 两个宽 \(\text{MaxUniqInpIds}\) 的 lzc。

#### 4.2.4 代码实践

**实践目标**：体会「读写表独立、容量各自独立」。

**操作步骤**：
1. 读 [L145-L186](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L145-L186)，确认 `i_wr_table` 的 `exists_inp_id_i` 接 `slv_req_i.aw.id`、`i_rd_table` 的接 `slv_req_i.ar.id`，两者互不影响。
2. 思考：若上游同时发 4 个不同 ID 的写、4 个不同 ID 的读，`MaxUniqIds = 4` 时是否会被反压？

**预期结果**：不会被反压。写表与读表各有 4 项，分别容纳 4 个写 ID 和 4 个读 ID，互不挤占。可见「单方向容量」是独立计数的。

#### 4.2.5 小练习与答案

- **练习 1**：表为什么以**输出 ID**为索引，而不是以输入 ID 为索引？
  - **答**：因为 pop（回收）发生在响应路径，而下游响应只携带窄（输出）ID，没有原宽 ID。以输出 ID 为索引才能在 pop 当拍 O(1) 定位表项并取回 `inp_id`。若以输入 ID 为索引，响应路径根本无从查起。
- **练习 2**：`exists_full_o`（某 ID 已达 `MaxTxnsPerId`）触发时，新事务会怎样？
  - **答**：该上游 ID 已在表里但计数到顶，新同 ID 事务被反压（不置 valid），直到其中一笔完成 pop 使 `cnt` 下降。

---

### 4.3 请求调度：FSM、保序与 ATOP

#### 4.3.1 概念说明

知道了表怎么用，剩下的问题是：**谁在什么时刻 push / pop，并决定输出 ID？** 这就是顶层那段 `always_comb` + 四状态 FSM 的职责（[L202-L415](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L202-L415))。

它要同时满足三条硬约束：

1. **AXI 握手铁律**：`valid` 一旦拉高，在握手完成前其载荷（尤其是 `id`）必须**逐拍稳定**。
2. **保序**：同 ID 必须复用同一输出 ID（4.2 已述）。
3. **ATOP 一致性**：`ATOP_R_RESP` 类原子写没有 AR 却会产生 R，因此它必须占用一个**读、写两个方向都空闲**的输出 ID，并同时 push 进读表和写表——否则它的 R 响应回来时读表查不到、无法还原 ID。

#### 4.3.2 核心流程

四状态：`Ready` / `HoldAR` / `HoldAW` / `HoldAx`（[L203](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L203))。`Ready` 是常态；`Hold*` 三个状态处理「我已向下游置了 valid 但下游当拍没 ready」的情况——这时必须把当拍选定的输出 ID **锁存**起来，下一拍继续用同一个 ID，满足「valid 期间 id 稳定」。

`Ready` 状态的请求处理优先级如下：

- **读（AR）**：若 `ar_valid` 且（`exists && !exists_full`）或（`!exists && !full`），则选定输出 ID（`exists ? exists_id : free_id`），置 `ar_valid`、`rd_push`（[L224-L236](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L224-L236))。
- **写（AW）**：
  - 非 `ATOP_R_RESP`：与读对称，只在写表里操作（[L238-L251](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L238-L251))。
  - `ATOP_R_RESP`：需要 `both_free`（读、写表都空闲的位图，由 `wr_free & rd_free` 得到，[L187](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L187))，并用其最低下标 `both_free_oup_id`（一个 lzc 算出，[L188-L195](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L188-L195))同时 push 读写两表（[L252-L273](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L252-L273))。
- **公平性**：`ATOP_R_RESP` 会「抢占」一个本可给 AR 的 `both_free` 槽位。为防止 ATOP 无限饿死 AR，顶层用 `ar_prio_q` 标志——一旦为 ATOP 让位了等待中的 AR，下一拍就强制让 AR 优先（[L255-L258](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L255-L258))。

`Hold*` 状态的核心动作是「用锁存值重驱输出 ID 并保持 valid」：

```systemverilog
// HoldAR：用锁存的 ar_id_q 继续驱动 ar.id（L333-L336）
rd_push_oup_id      = ar_id_q;
mst_req_o.ar_valid  = 1'b1;
slv_resp_o.ar_ready = mst_resp_i.ar_ready;
```

`HoldAW`（[L353-L393](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L353-L393))、`HoldAx`（[L395-L411](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L395-L411))同理——`HoldAx` 表示 AR、AW 同时在等握手。状态转移完全由 `{ar_valid, ar_ready, aw_valid, aw_ready}` 的握手结果决定（见 `Ready` 末尾 [L289-L298](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L289-L298))。

#### 4.3.3 源码精读

锁存的必要性来自一条协议断言：valid 未握手时，下一拍的 `ar.id`/`aw.id` 必须与上一拍相同（`$stable`，[L467-L470](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L467-L470))。因为选定的输出 ID 是表状态的组合函数，而表状态会随其他事务的 push/pop 逐拍变化，所以一旦决定了一个 ID 并置了 valid，就**必须把它寄存下来**，否则下一拍组合逻辑可能给出不同的值，违反 `$stable`。这就是 `ar_id_q`/`aw_id_q` 寄存器（[L418-L420](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L418-L420))与整套 `Hold*` 状态机存在的根本原因。

ATOP_R_RESP 的双向 push 是本模块最精巧之处（[L252-L273](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L252-L273))：它把**同一个** `both_free_oup_id` 同时写进读表和写表，`inp_id` 都是 `slv_req_i.aw.id`。这样：

- 下游用这个窄 ID 处理原子写，回 B 响应时 → 写表 pop → 还原成 `aw.id`；
- 同时产生的 R 响应也带同一个窄 ID → 读表 pop（在 `r.last`）→ 同样还原成 `aw.id`。
- 两表都回收后，该输出 ID 才真正空闲，可再分配。

模块还有一组贯穿性质断言保证「上游握手 ⟺ 下游握手」的对应（[L457-L466](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L457-L466))，例如 `slv_req_i.aw_valid && slv_resp_o.aw_ready |-> mst_req_o.aw_valid && mst_resp_i.aw_ready`。

#### 4.3.4 代码实践

**实践目标**：通过跟踪状态机，预言「下游不 ready 时输出 ID 是否稳定」。

**操作步骤**：
1. 假设上游发一笔 AR，`mst_req_o.ar_valid` 当拍拉高但 `mst_resp_i.ar_ready=0`。
2. 跟踪 [L277-L294](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L277-L294)：进入 `HoldAR`，`ar_id_d = rd_push_oup_id` 把当拍选定的输出 ID 锁进 `ar_id_q`。
3. 下一拍在 `HoldAR`（[L333-L336](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L333-L336))用 `ar_id_q` 重驱 `ar.id`，直到握手。

**需要观察的现象**：在握手完成前的每一拍，`mst_req_o.ar.id` 逐拍不变（满足 `$stable` 断言）。
**预期结果**：若把 `ar_id_q`/`aw_id_q` 这两个寄存器去掉，`$stable` 断言会在仿真中失败——这验证了 `Hold*` 状态机不可或缺。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `ATOP_R_RESP` 必须用「读写都空闲」的 ID，而不能复用一个仅写空闲的 ID？
  - **答**：因为它会产生 R 响应，R 走读表。若输出 ID 在读表里已被另一个在读 ID 占用，R 回来时会查到错误的 `inp_id`；且读表需要 push 才能在 pop 时还原 ID。所以必须读写两表同步占用同一个 ID。
- **练习 2**：`ar_prio_q` 解决什么问题？
  - **答**：防止连续的 `ATOP_R_RESP` 反复抢占 `both_free` 槽位而饿死普通 AR。一旦某 AR 因 ATOP 让位，下一拍 AR 获得优先权。

---

### 4.4 容量、反压与参数取舍

#### 4.4.1 概念说明

「窄 ID 端口容量」其实由两个参数共同决定：

- `AxiSlvPortMaxUniqIds`：单方向最多多少个**不同**上游 ID 同时在途 → 决定表的项数、也决定窄 ID 的位宽下限。
- `AxiMaxTxnsPerId`：同一个 ID 最多多少笔同时在途 → 决定每项计数器的位宽与每 ID 的并发深度。

容量与反压的关系：

\[
\text{单方向同时在途上限} = \text{AxiSlvPortMaxUniqIds} \times \text{AxiMaxTxnsPerId}
\]

但反压的触发更精细——是「**新事务的上游 ID 既不在表里、表又满了**」时才反压。也就是说：

- 若新事务的 ID 已在表里且未到 `MaxTxnsPerId` → 不反压，复用现有输出 ID；
- 若新事务的 ID 不在表里但还有空闲项 → 不反压，分配新输出 ID；
- 若新事务的 ID 不在表里且表满 → **反压**（`ax_valid` 不置位）。

#### 4.4.2 核心流程

把容量约束画成一张状态表：

| 新事务 ID 状态 | 表未满 | 表已满 |
| --- | --- | --- |
| 已在表里、未到每 ID 上限 | ✅ 复用输出 ID，不反压 | ✅ 复用输出 ID，不反压 |
| 已在表里、已达上限 | ❌ 反压（`exists_full`） | ❌ 反压 |
| 不在表里 | ✅ 分配新输出 ID | ❌ 反压（`full`） |

注意「表满」只看**这一方向**：写表满不挡读、读表满不挡写。唯一的例外是 `ATOP_R_RESP`，它要求读、写两表**同时**有空闲项（`both_free`），所以它可能比普通事务更早遇到反压。

#### 4.4.3 源码精读

反压条件直接写在 `Ready` 的判定里（读方向 [L227](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L227)、写方向 [L244](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L244))：

```systemverilog
if ((rd_exists && !rd_exists_full) || (!rd_exists && !rd_full)) begin
  ... // 唯有满足此条件才置 ar_valid、push
end
```

`rd_full`/`wr_full` 来自表的 `full_o`（lzc 的 `empty_o`），表满即所有项 `cnt>0`。参数合法性断言保证 `AxiSlvPortMaxUniqIds <= 2**AxiSlvPortIdWidth`（[L432-L433](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L432-L433))，即「宣称的不同 ID 数不超过上游 ID 空间」。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：配置一个 8→2 位 ID 的 remap，发送**多于**窄 ID 容量的并发事务，观察何时反压并解释原因。

**操作步骤**：

1. **推导参数**：
   - `AxiSlvPortIdWidth = 8`（上游 8 位 ID）。
   - `AxiMstPortIdWidth = 2`（下游只给 2 位 ID）→ `IdxWidth ≤ 2` → `AxiSlvPortMaxUniqIds ≤ 2^2 = 4`。
   - 取 `AxiSlvPortMaxUniqIds = 4`、`AxiMaxTxnsPerId = 1`（最严格，每 ID 只准 1 笔在途）。
2. **预言容量**：单方向最多 4 个不同 ID 在途；第 5 个**不同的**上游 ID 到来时，表满（4 项 `cnt>0`），应当被反压，直到其中一笔完成 B（写）或 R.last（读）使某项 `cnt` 归零。
3. **可选仿真验证**：仓库没有 `tb_axi_id_remap`，但 `test/tb_axi_iw_converter.sv` 会例化 `axi_iw_converter`，后者在「master ID 比 slave ID 窄、且 unique ID 数能塞下」时内部例化 `axi_id_remap`（见 [src/axi_iw_converter.sv:127-138](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L127-L138))。可参考 u1-l4 的 `make`/`run_vsim.sh` 流程，用 plusarg 覆盖 `TbAxiSlvPortIdWidth=8`、`TbAxiMstPortIdWidth=2`、`TbAxiSlvPortMaxUniqIds=4` 等参数（这些 `parameter` 在 [test/tb_axi_iw_converter.sv:42-48](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_iw_converter.sv#L42-L48))跑回归。

**需要观察的现象**：
- 用波形或 `axi_dumper`（u16-l2）观察：当第 5 个不同写 ID 出现时，`slv_resp_o.aw_ready` 是否保持为 0，直到某笔 B 握手。
- 确认被反压的并非「绝对无空闲」，而是「无空闲**输出 ID 槽位**」。

**预期结果 / 待本地验证**：第 5 个不同上游 ID 在表满期间被反压；一旦某笔事务的 B（或 R.last）握手释放一个槽位，被反压事务即获得输出 ID 并下发。仿真结果请以本地实际跑出的日志 `Errors: 0,` 为准（**待本地验证**）。

**解释（核心结论）**：反压的根因不是下游 ID「位不够」（2 位确实能编码 4 个值），而是**可同时映射的不同上游 ID 数被 `AxiSlvPortMaxUniqIds` 钉死为 4**，且映射必须单射、必须保留独立性。一旦在途的不同 ID 超过这个数，模块宁可停顿上游也不破坏独立性——这正是 `axi_id_remap` 与 `axi_id_serialize`（会把多个上游 ID 压成一个下游 ID、放弃独立性换取更窄 ID）的分水岭。

#### 4.4.5 小练习与答案

- **练习 1**：把 `AxiMaxTxnsPerId` 从 1 调到 4，对反压行为有什么影响？
  - **答**：同一上游 ID 现在可叠 4 笔在途，复用同一输出 ID，不再因「同 ID 第二笔」而反压；但表项数（不同 ID 数）不变，第 5 个**不同** ID 仍会被反压。
- **练习 2**：若实际运行期在途的不同上游 ID 永远 ≤ 2，把 `AxiSlvPortMaxUniqIds` 设成 4 会浪费什么？
  - **答**：浪费面积——表多出 2 项空闲触发器与比较器（复杂度见 4.2.3），且 `IdxWidth` 仍是 2，窄 ID 位宽无法借此进一步收窄到 1。应按真实上限紧凑配置。

## 5. 综合实践

把本讲三个要点（表的分配/回收、FSM 的稳定保持、ATOP 双向占用）串起来：

**任务**：为一个「8 位宽 ID、但运行期最多 4 个不同 ID 在途」的上游，设计到「2 位窄 ID 下游」的适配，并验证三件事。

1. **参数设计**：写出 `axi_id_remap`（或 `axi_id_remap_intf`）的完整参数列表，论证 `AxiMstPortIdWidth=2` 恰好满足下限。
2. **反压追踪**：构造 5 个不同写 ID 的事务序列，在源码里标出第 5 笔被反压的精确判定行（应指向 [L244](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L244) 的 `wr_full` 分支），并画出哪一笔 B 握手释放槽位后它才放行。
3. **ATOP 场景**：发一笔 `ATOP_R_RESP` 原子写，在 [L252-L273](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv#L252-L273) 处确认它同时 push 了读、写两表，并解释为什么在它的 R 响应全部回来之前、该输出 ID 不会被判为「空闲」（因为读表 `cnt` 尚未归零，`free_o` 仍为 0）。

> 若有仿真环境，把上述拓扑接到 `axi_sim_mem` + `axi_scoreboard`（u3-l2）做自检；若仅做源码阅读，至少把每一步的判定行号和预期表状态写清楚。

## 6. 本讲小结

- `axi_id_remap` 把上游稀疏的宽 ID 动态重映射为下游紧凑的窄 ID，**保留 ID 独立性**——不同上游 ID 必对应不同下游 ID，这是它区别于 `axi_id_serialize` 的根本性质。
- 核心数据结构是一张以**输出 ID 为索引**的表 `axi_id_remap_table`，每项存 `(inp_id, cnt)`；读、写方向各一张、容量独立。
- 四种操作 free / exists / push / pop 完成槽位分配、同 ID 复用（保序）、握手当拍入表、响应当拍（B 或 R.last）出表与 ID 还原。
- 容量由 `AxiSlvPortMaxUniqIds`（不同 ID 数）与 `AxiMaxTxnsPerId`（每 ID 并发数）共同决定；超出即反压，反压条件精确写在 `Ready` 状态的 `full` / `exists_full` 判定里。
- 四状态 FSM（`Ready`/`HoldAR`/`HoldAW`/`HoldAx`）的唯一目的是满足「valid 期间 id 稳定」铁律——把组合选定的输出 ID 锁存到握手完成。
- `ATOP_R_RESP` 原子写占用一个读、写两表都空闲的输出 ID，并双向 push，从而其 B 与 R 响应都能正确还原成原 ID。

## 7. 下一步学习建议

- **u10-l2 `axi_id_serialize`**：对照学习「放弃独立性、把多个上游 ID 压成更少下游 ID」的另一种取舍，理解何时该用 serialize 而非 remap。
- **u10-l3 `axi_iw_converter`**：看统一的 ID 宽度转换入口如何在「unique ID 数 ≤ \(2^{\text{MstIdWidth}}\)」时自动选用 `axi_id_remap`、否则选用 `axi_id_serialize`（参见 [src/axi_iw_converter.sv:127-138](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L127-L138))。
- **回到 u5-l2**：把 `axi_demux_id_counters` 的「计数在途」与本讲的「带映射的计数在途」对比，巩固 AXI 保序机制的不同实现层次。
- 想跑实际仿真，可结合 **u3-l2**（rand_master / scoreboard / sim_mem）与 **u1-l4**（`run_vsim.sh` 流程），用 `tb_axi_iw_converter` 作为承载本模块的真实测试台。
