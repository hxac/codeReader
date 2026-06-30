# axi_id_serialize：序列化降 ID

> 承接：[u9-l3 axi_serializer](u9-l3-axi-serializer.md)（事务序列化到单 ID）、[u10-l1 axi_id_remap](u10-l1-axi-id-remap.md)（宽 ID 重映射为窄 ID 且**保留独立性**）。本讲把这两块拼成一个新模块，并回答一个关键问题：当下游的 ID 位宽被卡死，连 `axi_id_remap` 都装不下时怎么办？

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `axi_id_serialize` 与 `axi_id_remap` 的本质区别：**它放弃 ID 独立性**，允许把两个不同的 slave 端 ID 映射到**同一个** master 端 ID。
- 画出它的三段式数据通路：`axi_demux` → 每个 master ID 一个 `axi_serializer` → `axi_mux`，并解释每一段为何存在。
- 看懂编译期 ID 映射函数 `map_slv_ids`：默认「取模」、可选「显式 `IdMap`」、以及 `MstIdBaseOffset` 偏移。
- 理解输出端那段看似奇怪的 `id >> 1` / `id << 1` 位移在做什么。
- 在「保留独立性但要更宽 master ID」与「放弃独立性换取更窄 master ID」之间做出选型判断，并给出吞吐—面积权衡结论。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 什么是「ID 独立性」

AXI 允许同一 master 用**多个 ID**并发发送事务，下游可以**按 ID 乱序返回响应**——只要「同 ID 同方向保序」即可。这意味着 ID 是并发的「车道」：ID 越多、彼此独立的并发越多。

当你把宽 ID 压成窄 ID 时，核心问题就是：**不同 slave ID 是否还能落到不同 master ID 上？**

- 若**能**：独立性保留，并发不受影响（这是 `axi_id_remap` 的做法）。
- 若**不能**（窄 ID 位数不够，必有两个 slave ID 挤进同一 master ID）：这两个原本可乱序的事务现在被迫**保序**，相当于被「串行化」——这正是 `axi_id_serialize` 名字的由来。

### 2.2 一句话定位

源码文件头的文档注释把定位说得很直白：

[src/axi_id_serialize.sv:19-28](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_serialize.sv#L19-L28) —— 把宽 ID 空间重映射到任意窄 ID 空间；**必要时**会把两个 slave ID 映射到同一 master ID，从而**约束它们的相对顺序**。文档同时给出选型建议：若必须保留 ID 独立性、愿意付出更宽 master ID 的代价，请改用 `axi_id_remap`。

也就是说，`axi_id_serialize` 是一条「退而求其次」的路：当下游 ID 位宽无法满足 `axi_id_remap` 的下限要求时，它通过放弃部分乱序自由度，强行把事务塞进更窄的 ID 空间。

### 2.3 它是「按 master ID 分桶 + 桶内序列化」

把整条通路想成一个分拣台：

1. 来一份 slave 端事务，先按它的 ID 查表，决定它该进**哪一个桶**（桶号 = 它要被映射到的 master ID）。
2. 每个桶内部，用一个 `axi_serializer` 把该桶里所有事务的 ID 全部抹成 `0`——桶内彻底序列化、不再乱序。
3. 最后用一个 `axi_mux` 把所有桶的输出合回单一 master 端口，并用「桶号」作为 master ID 的高位标签。

这三个步骤正好对应源码里的三个实例：`axi_demux`、`gen_serializers`（`for` 循环里的 N 个 `axi_serializer`）、`axi_mux`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/axi_id_serialize.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_serialize.sv) | 本讲主角。结构体内核 `axi_id_serialize` + 接口外壳 `axi_id_serialize_intf`。 |
| [src/axi_serializer.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_serializer.sv) | 每个桶内部使用的序列化器（u9-l3 已精读）。本模块**例化 N 个**它。 |
| [src/axi_demux.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux.sv) | 入口「1 拆 N」路由器（u5-l1 已精读），负责把事务分到正确的桶。 |
| [src/axi_mux.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv) | 出口「N 合 1」汇聚器（u5-l3 已精读），负责合并并打上桶号标签。 |
| [src/axi_id_prepend.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_prepend.sv) | 退化情形（单桶）下用来扩展 ID 位宽。 |
| [src/axi_iw_converter.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv) | 唯一的「调用方」，在 remap 与 serialize 之间二选一（本讲选型结论的活样本）。 |
| [test/tb_axi_iw_converter.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_iw_converter.sv) | 间接验证本模块的测试台（本模块**没有**专属 TB）。 |

> 提示：本模块没有 `tb_axi_id_serialize.sv`。它的功能正确性是在 `tb_axi_iw_converter` 里、当参数组合触发 `gen_serialize` 分支时被间接覆盖的（见第 5 节综合实践）。

## 4. 核心概念与源码讲解

### 4.1 模块定位：为什么需要「放弃 ID 独立性」

#### 4.1.1 概念说明

回忆 [u10-l1](u10-l1-axi-id-remap.md) 的 `axi_id_remap`：它把宽 ID 重映射为窄 ID，但有一条铁律——**不同 slave ID 必须映到不同 master ID**。这迫使其 master ID 位宽有下限：

\[ W_{\text{mst}} \ge \lceil \log_2(\text{AxiSlvPortMaxUniqIds}) \rceil \]

当下游 IP 把 master ID 位宽**钉死**在一个很小的值（例如某协议桥只接受 2 位 ID），而上游又可能同时出现多于 `2^{W_{\text{mst}}}` 个不同 ID 时，`axi_id_remap` 就无能为力——窄 ID 装不下这么多独立 ID。`axi_id_serialize` 正是为这个缺口设计的：它**主动放弃独立性**，允许两个不同 slave ID 共享一个 master ID，代价是这两组事务被迫相互保序（被序列化）。

#### 4.1.2 核心流程

模块对外仍是「一进一出」的 AXI 桥（slave 端口 → master 端口），但内部把上游事务按目标 master ID **分桶**，桶内序列化：

```
slave 端口 ──► axi_demux ──┬──► [桶0] axi_serializer ──┐
                            ├──► [桶1] axi_serializer ──┤
                            ├──► ...                    ├──► axi_mux ──► master 端口
                            └──► [桶N-1] axi_serializer ─┘
        select = SlvIdMap[slave.id]                          id 高位 = 桶号
```

文档明确点出实现方式：「每个 master 端口 ID 对应一个 `axi_serializer`，数量由 `AxiMstPortMaxUniqIds` 给出」（见 [src/axi_id_serialize.sv:27-28](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_serialize.sv#L27-L28)）。

#### 4.1.3 源码精读

模块的参数列表揭示了它的「双端口」契约与几个关键的「桶」参数：

[src/axi_id_serialize.sv:29-83](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_serialize.sv#L29-L83) —— 模块声明。值得注意的参数：

- `AxiSlvPortIdWidth` / `AxiMstPortIdWidth`：两侧 ID 宽度，且断言要求 `AxiMstPortIdWidth <= AxiSlvPortIdWidth`（**只降不升**，见 [L351-L352](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_serialize.sv#L351-L352)）。
- `AxiSlvPortMaxTxns`：slave 端口最大在途事务数（读、写分开计；ATOP 同时算读写）。
- `AxiMstPortMaxUniqIds`：master 端口能同时出现的**不同 ID 数**，也是「桶数」N，最大取 `2**AxiMstPortIdWidth`（见 [L345-L346](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_serialize.sv#L345-L346)）。
- `AxiMstPortMaxTxnsPerId`：每个 master ID（每个桶）下的最大在途事务数。
- `MstIdBaseOffset` / `IdMap` / `IdMapNumEntries`：自定义映射（见 4.3）。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认「何时该用 serialize 而非 remap」。
2. **步骤**：打开 [src/axi_iw_converter.sv:25-43](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L25-L43)，阅读文档对两种「降 ID」方案的描述；再看 [src/axi_iw_converter.sv:127-168](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L127-L168) 的 `if/else`。
3. **观察**：判别条件就是 `AxiSlvPortMaxUniqIds <= 2**AxiMstPortIdWidth`——能装下就 `remap`，装不下就 `serialize`。
4. **预期结果**：`serialize` 分支只在「窄 ID 装不下所有独立 ID」时被选中，这正是它存在的唯一理由。
5. 运行命令：无需运行，纯阅读。

#### 4.1.5 小练习与答案

**练习**：假设 slave 端最多有 5 个不同 ID 在途，master 端 ID 只有 2 位。`axi_iw_converter` 会选 `remap` 还是 `serialize`？为什么？

**答案**：选 `serialize`。因为 `AxiSlvPortMaxUniqIds=5 > 2**AxiMstPortIdWidth=4`，2 位 master ID 最多表示 4 个独立 ID，装不下 5 个，`remap` 的独立性前提不成立，只能走 `serialize` 放弃独立性。

---

### 4.2 三段式架构：demux → serializer 阵列 → mux

#### 4.2.1 概念说明

这是本模块的全部数据通路。三段各自承接前序讲义的一个积木：

- **入口 demux**：把单一 slave 端口按 `select` 拆到 N 个「桶」。`select` 由 slave ID 经映射表得到（4.3 详解）。
- **桶内 serializer**：u9-l3 学过的 `axi_serializer`，把该桶所有事务的 ID 强制抹零，靠两个 ID FIFO（读、写各一）在响应方向**还原原上游 ID**。
- **出口 mux**：把 N 个桶合回一个 master 端口，并把「桶号」前置进 master ID 高位（u5-l3 的 `axi_id_prepend` 机制）。

关键在于：**桶号 = master ID**。同一个桶里的事务，无论上游 ID 是什么，下游看到的 master ID 都相同，因此彼此保序——这就是「序列化」的物理实现。

#### 4.2.2 核心流程

请求方向（AW/W/AR）：

1. demux 根据 `SlvIdMap[slave.id]` 把事务送进桶 `i`（`i` 即目标 master ID）。
2. 桶 `i` 的 serializer 把 `aw.id`/`ar.id` 置 `0` 后发给 mux。
3. mux 把桶号 `i` 前置进 ID 高位，输出到 master 端口。

响应方向（B/R）：

1. master 端口返回的 B/R 带有 mux 拼好的 ID（高位 = 桶号）。
2. mux 解码 ID 高位，把响应送回桶 `i`。
3. 桶 `i` 的 serializer 用自己的 ID FIFO 弹出**原上游 ID** 还原，经 demux 回到 slave 端口。

W 通道无 ID，由 mux 内部的 W FIFO 按队头桶号转发（u5-l3 已讲）。

#### 4.2.3 源码精读

**① 入口 demux**（[src/axi_id_serialize.sv:182-210](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_serialize.sv#L182-L210)）：

```systemverilog
axi_demux #(
  .NoMstPorts  ( AxiMstPortMaxUniqIds ),   // 桶数 N
  .MaxTrans    ( AxiSlvPortMaxTxns    ),   // slave 端在途上限
  .AxiLookBits ( AxiSlvPortIdWidth    ),   // 用全部 ID 位建计数器
  .SpillAw(1'b1), .SpillAr(1'b1)           // 仅 AW/AR 切组合路径
) i_axi_demux (
  .slv_aw_select_i ( slv_aw_select ),      // = SlvIdMap[slave.id]
  .slv_ar_select_i ( slv_ar_select ),
  .mst_reqs_o      ( to_serializer_reqs )  // N 路输出到各桶
);
```

要点：`AxiLookBits = AxiSlvPortIdWidth` 意味着用**全部** slave ID 位做在途跟踪，不会有误冲突（false conflict）。由于 `select` 是 slave ID 的纯函数，**同一 slave ID 永远进同一桶**，天然保序。

**② 桶内 serializer 阵列**（[src/axi_id_serialize.sv:217-242](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_serialize.sv#L217-L242)）：

```systemverilog
for (genvar i = 0; i < AxiMstPortMaxUniqIds; i++) begin : gen_serializers
  axi_serializer #(
    .MaxReadTxns  ( AxiMstPortMaxTxnsPerId ),  // 桶内读在途上限
    .MaxWriteTxns ( AxiMstPortMaxTxnsPerId ),  // 桶内写在途上限
    .AxiIdWidth   ( AxiSlvPortIdWidth      )   // FIFO 存原上游 ID（全宽）
  ) i_axi_serializer ( /* 接第 i 桶的 req/resp */ );
```

每个 serializer 的 ID FIFO 存的是**全宽 slave ID**，这样响应才能精确还原。serializer 输出侧的 ID 被**截断到 1 位**（因为下游 ID 恒为 `0`），响应侧再**零扩展**回全宽，见 [src/axi_id_serialize.sv:232-241](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_serialize.sv#L232-L241)：

```systemverilog
from_serializer_reqs[i].aw.id = tmp_serializer_reqs[i].aw.id[0]; // 截到 1 位
// ...
tmp_serializer_resps[i].b.id = {{AxiSlvPortIdWidth-1{1'b0}}, from_serializer_resps[i].b.id}; // 零扩展
```

**③ 出口 mux**（[src/axi_id_serialize.sv:247-278](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_serialize.sv#L247-L278)）：

```systemverilog
axi_mux #(
  .SlvAxiIDWidth ( 32'd1                ),  // 每个桶的输入 ID 只有 1 位（恒 0）
  .NoSlvPorts    ( AxiMstPortMaxUniqIds ),  // N 个桶
  .MaxWTrans     ( AxiMstPortMaxTxnsPerId )
) i_axi_mux ( .slv_reqs_i ( from_serializer_reqs ), ... );
```

mux 把桶号前置进 ID 高位（u5-l3 机制），输出 `mux_id_t` 宽度的 ID。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：确认「三段实例各有多少个」。
2. **步骤**：在 [src/axi_id_serialize.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_serialize.sv) 里数 `axi_demux`、`axi_serializer`、`axi_mux` 的实例。
3. **观察**：demux 与 mux 各 1 个；serializer 是 `for` 循环生成的 N=`AxiMstPortMaxUniqIds` 个。
4. **预期结果**：1 个 demux + N 个 serializer + 1 个 mux，共 N+2 个 AXI 子模块实例。
5. 运行命令：无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么每个 `axi_serializer` 的 `AxiIdWidth` 要设成**全宽** `AxiSlvPortIdWidth`，而不是窄的 master ID 宽度？

**答案**：因为 serializer 必须在响应方向把**原始上游 ID** 还原给 slave 端口。它的 ID FIFO 是「备忘录」，押入时记下全宽 slave ID，弹出时填回 B/R 的 id 字段。若只存窄 ID，就无法还原上游 ID。

**练习 2**：demux 配置 `AxiLookBits = AxiSlvPortIdWidth`（用全部 ID 位跟踪）。结合「select 是 slave ID 的纯函数」这一事实，这会不会产生跨端口的同 ID 停顿？

**答案**：不会。因为同一 slave ID 的 `select` 恒定，永远路由到同一个桶（同一个 master 端口），demux 的 id_counters 不会观察到「同 ID 去往不同端口」的情形，自然不会触发保序停顿。

---

### 4.3 ID 映射：map_slv_ids 与默认取模 + 显式 IdMap

#### 4.3.1 概念说明

「slave ID → 桶号」的映射是本模块的灵魂。模块提供两种映射方式，可叠加：

1. **默认取模映射**：`out = (in + MstIdBaseOffset) % AxiMstPortMaxUniqIds`。把整个 ID 空间均匀摊到 N 个桶。
2. **显式 IdMap**：对指定输入 ID 精确指定输出 ID，覆盖默认值。同一输入 ID 出现在多条 `IdMap` 规则中时，**最后一条**生效。

这张表在**编译期**一次性算好，做成 `localparam`，运行期不再变化——零运行期开销。

#### 4.3.2 核心流程

映射函数 `map_slv_ids` 遍历全部 `2**AxiSlvPortIdWidth` 个可能输入 ID，先填默认取模值，再用 `IdMap` 逐条覆盖：

```
for each input id i in [0, 2**SlvIdWidth):
    ret[i] = (i + MstIdBaseOffset) % N        // 默认
for each explicit entry e in IdMap:
    ret[IdMap[e][0]] = IdMap[e][1]            // 覆盖（后写胜）
```

随后 select 信号取这张表查询结果的低位：

```
slv_aw_select = SlvIdMap[slv_req_i.aw.id]  (截到 SelectWidth 位)
```

#### 4.3.3 源码精读

先看位宽推导（[src/axi_id_serialize.sv:85-91](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_serialize.sv#L85-L91)）：

```systemverilog
localparam int unsigned SelectWidth = cf_math_pkg::idx_width(AxiMstPortMaxUniqIds);
typedef logic [SelectWidth-1:0] select_t;
localparam int unsigned MuxIdWidth = (AxiMstPortMaxUniqIds > 1) ? SelectWidth + 1 : 1;
```

`cf_math_pkg::idx_width(N)` 给出索引 N 个表项所需的位宽（对 N>1 即 \(\lceil\log_2 N\rceil\)）。`MuxIdWidth = SelectWidth + 1` 的 +1 来自 mux 总会在 1 位 serializer ID 前再前置 `SelectWidth` 位桶号（4.4 详解）。

映射函数与表（[src/axi_id_serialize.sv:157-177](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_serialize.sv#L157-L177)）：

```systemverilog
function automatic slv_id_map_t map_slv_ids();
  slv_id_map_t ret = '0;
  for (int unsigned i = 0; i < 2**AxiSlvPortIdWidth; ++i)
    ret[i] = (i + MstIdBaseOffset) % AxiMstPortMaxUniqIds;   // 默认取模 + 偏移
  for (int unsigned i = 0; i < IdMapNumEntries; ++i)
    ret[IdMap[i][0]] = IdMap[i][1];                          // 显式覆盖
  return ret;
endfunction
localparam slv_id_map_t SlvIdMap = map_slv_ids();

assign slv_aw_select = select_t'(SlvIdMap[slv_req_i.aw.id]);
assign slv_ar_select = select_t'(SlvIdMap[slv_req_i.ar.id]);
```

注意 `slv_id_map_t` 是 `mst_id_t` 的数组、长度 `2**AxiSlvPortIdWidth`（[L158](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_serialize.sv#L158)），所以当 slave ID 很宽时这张编译期表会很大——这是把映射做成纯组合查表的代价。

> 关于 `IdMap` 参数形状（[L67-69](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_serialize.sv#L67-L69)）：它是一个二维 `int unsigned` 数组，每条 `[input_id, output_id]`；上界用 `axi_pkg::iomsb(IdMapNumEntries)` 给出（即 `IdMapNumEntries-1`，见 [axi_pkg.sv:539](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L539) 的 `iomsb` 函数）。

#### 4.3.4 代码实践（源码阅读 + 推算）

1. **目标**：手算一个具体配置的默认映射。
2. **配置**：`AxiMstPortMaxUniqIds = 2`（N=2，桶号为 0/1），`MstIdBaseOffset = 0`，无 `IdMap`，slave ID 宽 3 位（0..7）。
3. **步骤**：套用 `ret[i] = i % 2`。
4. **预期结果**：slave ID `0,2,4,6 → 桶0`；`1,3,5,7 → 桶1`。即所有偶数 ID 被序列化到 master ID 0，所有奇数 ID 被序列化到 master ID 1。
5. **观察结论**：原本 8 个独立的上游 ID 被压成 2 个 master ID，偶/奇两组各自内部失去乱序自由度。运行命令：无需运行。

#### 4.3.5 小练习与答案

**练习**：若希望 slave ID `5` 单独映到桶 `0`（其余仍按取模），应如何配置 `IdMap`？`MstIdBaseOffset` 对它有影响吗？

**答案**：设 `IdMapNumEntries = 1`，`IdMap = '{ {5, 0} }`。函数会先用取模填表，再用这条规则把 `ret[5]` 改写为 `0`。`MstIdBaseOffset` **不影响**显式映射的输入 ID——参数文档（[L60-62](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_serialize.sv#L60-L62)）明确：偏移只对走默认取模的输入 ID 生效，被 `IdMap` 覆盖的输入 ID 忽略它。

---

### 4.4 输出端 ID 位移与并发—面积权衡

#### 4.4.1 概念说明

本模块有两处「看着别扭」的细节，理解了它们才算真正读懂：

**(a) 输出端的 `id >> 1` / `id << 1` 位移**。serializer 输出 ID 恒为 `0`（1 位），mux 在其高位前置 `SelectWidth` 位桶号，于是 mux 输出 ID = `{桶号, 1'b0}`，共 `SelectWidth+1 = MuxIdWidth` 位。最低位那个 `0` 是冗余的（永远是 serializer 留下的 `0`）。输出时 `>> 1` 丢弃它，得到干净的桶号填入 master ID；响应时 `<< 1` 再补回那个 `0`，供 mux 解码。

**(b) 退化分支 `gen_no_id_shift`**。当 N=1（`MuxIdWidth=1`）时所有事务挤进单桶、彻底序列化，此时没有桶号可前置，改用 `axi_id_prepend`（`pre_id='0`）单纯把 1 位 ID 零扩展到 `AxiMstPortIdWidth`。

#### 4.4.2 核心流程

并发能力（每方向，读/写分开）近似为：

\[ \text{master 端在途} \approx N \times T \]

其中 \(N = \text{AxiMstPortMaxUniqIds}\)（桶数），\(T = \text{AxiMstPortMaxTxnsPerId}\)（每桶在途上限），整体再受 slave 端 `AxiSlvPortMaxTxns` 封顶。但**同一桶内的事务被迫保序**——这正是相比 `axi_id_remap` 损失的乱序并行度。

面积主要由 N 个 serializer 的 ID FIFO 占据：每个 serializer 有读、写两个 FIFO，深度 T、字宽 `AxiSlvPortIdWidth`，故

\[ \text{serialize 面积} \propto N \times 2 \times T \times W_{\text{slv}} \quad(\text{外加 1 个 demux + 1 个 mux}) \]

对照 `axi_id_remap`（u10-l1）：其核心是一张读/写各自独立的重映射表，约

\[ \text{remap 面积} \propto 2 \times U \times (W_{\text{slv}} + \text{cnt\_bits}),\quad U=\text{AxiSlvPortMaxUniqIds} \]

两者谁大取决于参数，没有绝对赢家。

#### 4.4.3 源码精读

**位移分支**（[src/axi_id_serialize.sv:280-289](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_serialize.sv#L280-L289)）：

```systemverilog
if (MuxIdWidth > 32'd1) begin : gen_id_shift
  always_comb begin
    `AXI_SET_REQ_STRUCT(mst_req_o, axi_mux_req)
    mst_req_o.aw.id = mst_id_t'(axi_mux_req.aw.id >> 32'd1);  // 丢掉冗余的最低 0 位
    mst_req_o.ar.id = mst_id_t'(axi_mux_req.ar.id >> 32'd1);
    `AXI_SET_RESP_STRUCT(axi_mux_resp, mst_resp_i)
    axi_mux_resp.b.id = mux_id_t'(mst_resp_i.b.id << 32'd1);  // 响应方向补回那个 0
    axi_mux_resp.r.id = mux_id_t'(mst_resp_i.r.id << 32'd1);
  end
end
```

**退化分支**（[src/axi_id_serialize.sv:290-338](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_serialize.sv#L290-L338)）：用 `axi_id_prepend`、`pre_id_i='0`，把 1 位 mux 输出零扩展成 `AxiMstPortIdWidth`。

**接口外壳** `axi_id_serialize_intf`（[src/axi_id_serialize.sv:370-454](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_serialize.sv#L370-L454)）：标准的「`AXI_BUS` 接口 + `AXI_TYPEDEF_*`/`AXI_ASSIGN_*` 宏 + 结构体内核」三明治，与 u2-l4 范式一致，供 `tb_axi_iw_converter_intf` 这类带接口的测试台使用。

#### 4.4.4 代码实践（源码阅读 + 推算）

1. **目标**：解释 `MuxIdWidth = SelectWidth + 1` 里那个 `+1` 从哪来。
2. **步骤**：顺着数据通路数 ID 位宽——serializer 输出 1 位（恒 0）→ mux 输入 `SlvAxiIDWidth=1`（[L248](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_serialize.sv#L248)）→ mux 前置 `SelectWidth` 位桶号 → mux 输出 `SelectWidth+1` 位。
3. **预期结果**：`+1` 就是 serializer 留下的那个恒 0 最低位；`>>1` 后正好剩下桶号。
4. 运行命令：无需运行。

#### 4.4.5 小练习与答案

**练习**：在 N=1（`AxiMstPortMaxUniqIds=1`）的极端配置下，整个模块的行为退化成什么？为什么此时不用 `>>1` 而用 `axi_id_prepend`？

**答案**：N=1 时所有事务进同一个桶、全部序列化到单一 master ID `0`，等价于直接放一个 `axi_serializer`。此时 `MuxIdWidth=1`，没有桶号（`SelectWidth` 退化为 1 但只有一个桶），`>>1` 会把仅有的位丢光，故改用 `axi_id_prepend`(`pre_id='0`) 把这位 `0` 安全地零扩展到目标 `AxiMstPortIdWidth`。

---

## 5. 综合实践

**任务**：通过 `tb_axi_iw_converter` 在**相同窄 master ID 宽度**下对比 `axi_id_remap` 与 `axi_id_serialize` 的最大并发，写出吞吐—面积权衡结论。

> 为什么用它：本模块没有专属 TB，`axi_iw_converter` 是唯一会在 `gen_serialize` 分支例化它的入口，且同一个 TB 也能跑 `gen_remap` 分支，天然适合做对照。

### 5.1 找到两条分支的参数

打开 [scripts/run_vsim.sh:91-146](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L91-L146)，定位 `axi_iw_converter)` 块。脚本依据 `MAX_UNIQ_SLV_PORT_IDS` 与 `MAX_MST_PORT_IDS=2**MST_PORT_IW` 的关系分流：

- **remap 配置**（[L112-120](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L112-L120)）：`MAX_UNIQ_SLV_PORT_IDS <= MAX_MST_PORT_IDS`，只传 `TbAxiSlvPortMaxTxnsPerId=5`。
- **serialize 配置**（[L121-132](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L121-L132)）：`MAX_UNIQ_SLV_PORT_IDS > MAX_MST_PORT_IDS`，额外传 `TbAxiSlvPortMaxTxns=31`、`TbAxiMstPortMaxUniqIds=2**MST_PORT_IW`、`TbAxiMstPortMaxTxnsPerId=7`。

### 5.2 构造一对对照配置

固定 `TbAxiMstPortIdWidth=2`（master ID 2 位，最多 4 个独立 ID），`TbAxiSlvPortIdWidth=4`（slave ID 4 位）：

| 配置 | `TbAxiSlvPortMaxUniqIds` | 走哪条分支 | master 端理论并发(每方向) |
|------|--------------------------|-----------|--------------------------|
| A（remap） | `4`（≤ 4） | `gen_remap` | 4 个独立 ID × 5 = 高并发、可乱序 |
| B（serialize） | `8`（> 4） | `gen_serialize` | 4 桶 × 7 = 高并发，但**同桶内保序** |

### 5.3 操作步骤

1. 跑全量回归（会枚举大量参数组合，较慢）：
   ```bash
   make sim-axi_iw_converter.log
   ```
   判据：日志中出现 `Errors: 0,`（u1-l4 讲过的通过判据）。
2. 若只想跑上面两个对照点，仿照脚本里的 `call_vsim` 自行拼两条 `vsim` 命令，分别带上表中的 `-g` 参数与一个固定 `sv_seed`。
3. 在波形或日志里观察：配置 B 中，**偶数 slave ID 之间**、**奇数 slave ID 之间**的响应是否出现强制保序（不再乱序），而配置 A 中不同 slave ID 仍可乱序返回。

### 5.4 需要观察的现象与预期

- **功能**：两种配置都应 `Errors: 0`——`tb_axi_iw_converter` 的 scoreboard（[test/tb_axi_iw_converter.sv:210-441](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_iw_converter.sv#L210-L441)）会校验「除 ID 外字段一致 + 同上游 ID 必映到同下游 ID + 响应按上游 ID 正确回送」。
- **吞吐**：当上游大量使用**彼此独立**的 ID 时，配置 A（remap）因保留独立性，可乱序并行，吞吐更高；配置 B（serialize）会把碰撞到同桶的 ID 强制保序，吞吐下降。
- **面积**：配置 B 多出 N 个 serializer 的 ID FIFO（每个存全宽 slave ID）；配置 A 是一张重映射表。具体谁大取决于参数，**待本地综合后确认**。

> 说明：以上吞吐/面积的定性结论可由源码结构直接推出；具体数值（拍数、门数）依赖工具与参数，标注为「待本地验证」。

## 6. 本讲小结

- `axi_id_serialize` 把宽 slave ID 压到任意窄 master ID，**核心代价是放弃 ID 独立性**：允许两个不同 slave ID 映射到同一 master ID，从而被迫相互保序（序列化）。
- 它是 `axi_id_remap` 的「退路」：当 `AxiSlvPortMaxUniqIds > 2**AxiMstPortIdWidth`、窄 ID 装不下所有独立 ID 时，`axi_iw_converter` 自动改用它。
- 数据通路是标准三段式：`axi_demux`（按映射分桶）→ N 个 `axi_serializer`（桶内抹零 + FIFO 还原原 ID）→ `axi_mux`（合并 + 桶号前置）。
- ID 映射由编译期函数 `map_slv_ids` 一次性算好：默认 `(id+base) % N` 取模，可用显式 `IdMap` 覆盖（后写胜），`MstIdBaseOffset` 不影响被覆盖项。
- 输出端的 `id >> 1` / `id << 1` 是为丢弃/补回 serializer 留下的那个恒 0 最低位；N=1 退化时改用 `axi_id_prepend` 零扩展。
- 选型口诀：**能保留独立性就用 `remap`（吞吐好），被迫放弃才用 `serialize`（master ID 更窄）**；面积两者无绝对优劣，看参数。

## 7. 下一步学习建议

- 阅读 [src/axi_iw_converter.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv) 的三路 `if/else`（降→remap/serialize、升→id_prepend、等宽→直通），它是本讲与 u10-l1 的统一点，对应下一讲 **u10-l3 axi_iw_converter**。
- 结合 u9-l3 的 `axi_serializer` 源码，确认「桶内序列化 + ID FIFO 还原」的细节与本讲的例化参数一致。
- 在 u15-l4（异构网络）中，你会看到 `iw_converter` 如何在跨 ID 宽度的子网间充当胶水——本讲是其「降 ID」半边的完整理论。
