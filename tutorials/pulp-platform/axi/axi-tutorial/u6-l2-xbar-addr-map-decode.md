# 地址映射、译码错误与默认端口

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 `rule_t` 规则数组写出一张 xbar 的全局地址映射，并能正确判断任意地址会被路由到哪个 master 端口。
- 说清“前闭后开区间”和“重叠时高位规则优先”这两条匹配规则的精确含义。
- 描述当地址无法匹配任何规则时，xbar 内部的译码错误从端 `axi_err_slv` 会给出怎样的 B/R 响应（`RESP_DECERR` 与 `0xBADCAB1E`）。
- 解释每个 slave 端口的“默认 master 端口”机制：它如何把未映射事务重定向到一个真实 master 端口，以及为什么在事务未完成期间不允许改动它。

本讲承接 [u6-l1 xbar 架构与配置](u6-l1-xbar-architecture.md)，把目光从“xbar 由 demux 阵列 + mux 阵列拼成”下沉到其中一个关键细节：**地址究竟是怎么被翻译成一个目标 master 端口号的，翻译不出来时又会发生什么**。

## 2. 前置知识

本讲默认你已经掌握：

- **AXI4 五通道与握手**（见 [u1-l3](u1-l3-axi-protocol-primer.md)）：AW/W/B 是写事务的三通道，AR/R 是读事务的两通道，`valid && ready` 同高才算一拍握手。
- **resp_t 响应码**：`RESP_OKAY/EXOKAY/SLVERR/DECERR`，其中 `RESP_DECERR`（2’b11）表示“译码错误／从端不存在”。
- **xbar 的整体结构**（见 [u6-l1](u6-l1-xbar-architecture.md)）：`axi_xbar_unmuxed` 为每个 slave 端口配一个 `addr_decode` + `axi_demux`，再通过 cross 矩阵把请求送到各 master 端口的 `axi_mux`。
- **“组合优于配置”**：xbar 自己不算地址翻译，地址→端口号的规则表是从外部以输入信号 `addr_map_i` 喂进来的。

一个直觉性的比喻：xbar 就像一栋大楼的收发室。每件包裹（事务）上写着目标地址（AW/AR 的 addr），收发员手里有一张“地址分区表”（`addr_map_i`），查表决定把包裹送到几号出口（master 端口）。如果查无此区，要么退回一个“查无此地”的回执（译码错误从端），要么按当班员的默认习惯送到某个指定出口（默认 master 端口）。本讲要讲的，就是这张表、这个回执、以及这个默认习惯。

## 3. 本讲源码地图

| 文件 | 作用 |
|:--|:--|
| `src/axi_xbar_unmuxed.sv` | xbar 的路由主体。为每个 slave 端口例化 `addr_decode`（地址→端口索引）、`axi_demux`（按索引分发）和译码错误从端 `axi_err_slv`。是本讲的主线。 |
| `src/axi_err_slv.sv` | 译码错误从端。吸收任何送达的事务，对写返回 `RESP_DECERR` 的 B，对读返回带 `0xBADCAB1E` 数据的 R。 |
| `src/axi_pkg.sv` | 提供 `xbar_rule_64_t`/`xbar_rule_32_t` 规则类型，以及 `xbar_cfg_t` 中的 `NoAddrRules` 字段。 |
| `doc/axi_xbar.md` | xbar 的官方文档，其中 *Address Map* 与 *Decode Errors and Default Slave Port* 两节是本讲语义的权威说明。 |
| `test/tb_axi_xbar_pkg.sv` | 测试台 monitor。它用一段“遍历所有规则、最后一个匹配者胜”的循环，旁证了“高位规则优先”的匹配规则。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**地址映射与匹配规则**、**译码错误从端**、**默认 master 端口**。三者都发生在 `axi_xbar_unmuxed` 为每个 slave 端口展开的 `gen_slv_port_demux` 生成块里。

### 4.1 全局地址映射：rule_t 规则集与匹配规则

#### 4.1.1 概念说明

xbar 的地址映射是一张**全局共享**的规则表：所有 slave 端口用同一张表（而不是每端口一张）。这张表是一个 `rule_t` 结构体数组，每条规则把“一段地址区间”映射到“一个 master 端口索引”。一条规则只有三个字段：

```
idx         // 这段区间映射到第几个 master 端口
start_addr  // 区间起点（包含）
end_addr    // 区间终点（不包含）
```

两个关键语义（见 [doc/axi_xbar.md:18-29](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L18-L29)）：

1. **前闭后开区间**：地址 `addr` 匹配某条规则，当且仅当
   \[
   \text{start\_addr} \le \text{addr} < \text{end\_addr}
   \]
   即包含 `start_addr`、不包含 `end_addr`。这样两条相邻规则可以写成 `[…, 0x4000)` 与 `[0x4000, …)` 而不会重叠或留缝。要求 `start_addr <= end_addr`。

2. **重叠时高位规则优先**：两条规则的区间允许重叠。一旦重叠，**数组下标更大（更高有效位位置）的那条规则胜出**。这意味着你可以用一条靠后的小区间去“打洞”覆盖一条靠前的大区间。

#### 4.1.2 核心流程

对每个 slave 端口 `i`，地址译码在请求方向独立做两次——AW 与 AR 各一次，流程完全对称：

```
1. 取 slv_ports_req_i[i].aw.addr（或 ar.addr）作为待译码地址 addr。
2. 在 addr_map_i[NoAddrRules-1:0] 中查找：
   - 若 en_default_mst_port_i[i] == 1 且无规则命中  -> 输出 default_mst_port_i[i]，无错（见 4.3）。
   - 否则若有规则命中                                      -> 输出该规则 idx，dec_valid=1，dec_error=0。
   - 否则（无规则命中且未启用默认端口）                    -> dec_error=1。
3. demux 拿到 select，把整笔事务路由到对应 master 端口；
   若 dec_error，select 被改写为 NoMstPorts（即译码错误从端那一槽，见 4.2）。
```

注意 xbar 本身**不做地址翻译**（不修改 addr），它只做“分类路由”：决定这笔事务去哪个出口。规则的 `idx` 就是出口编号。

#### 4.1.3 源码精读

规则的类型由 `axi_pkg` 提供两套常用别名，对应 64 位与 32 位地址（[src/axi_pkg.sv:524-536](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L524-L536)）：

```systemverilog
typedef struct packed {
  int unsigned idx;
  logic [63:0] start_addr;
  logic [63:0] end_addr;
} xbar_rule_64_t;
// xbar_rule_32_t 同构，只是 start_addr/end_addr 为 logic [31:0]
```

`axi_xbar_unmuxed` 把规则类型做成参数 `rule_t`（默认就是 `xbar_rule_64_t`），并在端口上声明规则数组（[src/axi_xbar_unmuxed.sv:41-51](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L41-L51) 给出所需字段说明，[src/axi_xbar_unmuxed.sv:69-72](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L69-L72) 声明输入）：

```systemverilog
input  rule_t [Cfg.NoAddrRules-1:0]  addr_map_i;  // 全局地址映射，数组方向 [NoAddrRules-1:0]
```

真正执行查找的是外部原语 `addr_decode`（来自 `common_cells`），每个 slave 端口例化两份，分别服务 AW 与 AR。AW 这一份见 [src/axi_xbar_unmuxed.sv:101-114](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L101-L114)：

```systemverilog
addr_decode #(
  .NoIndices  ( Cfg.NoMstPorts  ),   // 可选出口数 = master 端口数
  .NoRules    ( Cfg.NoAddrRules ),
  .addr_t     ( addr_t          ),
  .rule_t     ( rule_t          )
) i_axi_aw_decode (
  .addr_i           ( slv_ports_req_i[i].aw.addr ),  // 待译码地址
  .addr_map_i       ( addr_map_i                 ),  // 全局规则表
  .idx_o            ( dec_aw                     ),  // 命中的 master 端口索引
  .dec_valid_o      ( dec_aw_valid               ),  // 命中（有规则匹配）
  .dec_error_o      ( dec_aw_error               ),  // 未命中（无规则匹配）
  .en_default_idx_i ( en_default_mst_port_i[i]   ),  // 见 4.3
  .default_idx_i    ( default_mst_port_i[i]      )
);
```

AR 那份结构完全相同，只是把 `aw.addr` 换成 `ar.addr`（[src/axi_xbar_unmuxed.sv:116-129](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L116-L129)）。“高位规则优先”的语义由 `addr_decode` 内部实现，而本仓库的 monitor 用一段朴素的“遍历、最后匹配者覆盖前者”的循环给出了等价旁证（[test/tb_axi_xbar_pkg.sv:166-172](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_xbar_pkg.sv#L166-L172)）：循环下标 `j` 从 `0` 递增到 `NoAddrRules-1`，每次命中都用 `AddrMap[j].idx` 覆盖结果，因此**最大下标**的命中规则最终胜出——这正对应“数组中更高位优先”。

`NoAddrRules` 这个数量在配置结构体里声明，文档明确“每个 master 端口可有多条规则，但总体至少应有一条；若事务无法路由，xbar 会回答 `RESP_DECERR`”（[src/axi_pkg.sv:518-521](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L518-L521)）。

#### 4.1.4 代码实践

**目标**：用一个真实 testbench 的地址表生成函数，理解“前闭后开 + 等分区间”的写法。

**步骤**：

1. 打开 `test/tb_axi_xbar.sv` 的 `addr_map_gen` 函数（[test/tb_axi_xbar.sv:108-117](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_xbar.sv#L108-L117)）。它把第 `i` 个 slave 端口的区间设为 `[i*0x2000, (i+1)*0x2000)`，并把 `idx` 也设为 `i`。
2. 阅读这段代码，回答：当 `i=0` 时，区间是多少？地址 `0x0000_1FFF` 与 `0x0000_2000` 分别命中哪条规则？

**需要观察的现象 / 预期结果**：

- `i=0`：`start_addr = 0x0000_0000`，`end_addr = 0x0000_2000`，区间 `[0x0, 0x2000)`。
- `0x0000_1FFF`：满足 `0x0 <= 0x1FFF < 0x2000`，命中规则 0，路由到 master 端口 0。
- `0x0000_2000`：`0x2000 < 0x2000` 不成立，**不**命中规则 0；它恰好是规则 1 的 `start_addr`，命中规则 1。这正是“前闭后开”的体现：右端点属于下一条规则。

> 说明：本实践为源码阅读型，无需运行仿真即可得出确定结论；若要运行确认，可用 `make sim-axi_xbar.log`（待本地验证具体命令名）。

#### 4.1.5 小练习与答案

**练习 1**：一张表有两条规则——规则 A（下标 0）：`[0x0, 0x10000) → idx=0`；规则 B（下标 1）：`[0x2000, 0x3000) → idx=1`。地址 `0x2500` 命中哪个端口？地址 `0x4000` 呢？

**答案**：`0x2500` 同时落在 A 和 B 的区间内（重叠），按“高位规则优先”，下标更大的 B 胜出，去端口 1。`0x4000` 只落在 A 内，去端口 0。

**练习 2**：若两条规则区间完全相同、`idx` 不同，哪个 `idx` 生效？

**答案**：数组下标更大的那条生效（其 `idx` 胜出）。“高位规则优先”只看规则在数组中的位置，与 `idx` 数值大小无关。

---

### 4.2 译码错误从端 axi_err_slv：DECERR 与 0xBADCAB1E

#### 4.2.1 概念说明

当一个 slave 端口收到的地址**不匹配任何规则、又没有启用默认 master 端口**时，xbar 必须给这笔事务一个“合理的结局”——不能让它悬空。AXI 协议规定这种情况应返回 `RESP_DECERR`（译码错误）。xbar 的做法是：**每个 slave 端口都在内部挂一个专属的译码错误从端 `axi_err_slv`**，它无条件吸收送达的事务并回错。

关键设计：这个错误从端在 `axi_demux` 看来就是“多出来的一个 master 端口”。demux 本来把请求分发到 `NoMstPorts` 个真实 master 端口，这里却例化成 `NoMstPorts + 1` 路，第 `NoMstPorts` 路（即下标等于端口数的这一槽）专门接 `axi_err_slv`。译码出错时，select 被改写为 `NoMstPorts`，事务便自然流进错误从端。

#### 4.2.2 核心流程

**路由到错误从端**（[src/axi_xbar_unmuxed.sv:131-134](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L131-L134)）：当 `dec_aw_error`（或 `dec_ar_error`）为真，select 取 `Cfg.NoMstPorts`：

```
slv_aw_select = dec_aw_error ? NoMstPorts : dec_aw
```

**错误从端的响应行为**（`axi_err_slv`，参数 `Resp = RESP_DECERR`、`RespData = 64'hCA11AB1EBADCAB1E`）：

- **写事务**：吸收 AW（记下 `aw.id`）→ 吃掉所有 W 拍（`w_ready` 拉高）→ 在最后一拍 W 时把 `id` 推入 B 队列 → 回一个 B，其中 `b.resp = RESP_DECERR`、`b.id` 来自队列。
- **读事务**：吸收 AR（记下 `ar.id` 与 `ar.len`）→ 用一个递减计数器从 `len` 数到 0，产生 `len+1` 拍 R → 每拍 `r.resp = RESP_DECERR`、`r.data = RespData`，最后一拍拉高 `r.last`。
- **原子操作**：若 `ATOPs=1`，错误从端前置一个 `axi_atop_filter`，把原子写过滤成普通写后再吸收，避免不支持 ATOP 的内核逻辑出错。

读数据那个标志性魔数 `0xBADCAB1E` 来自参数 `RespData` 的默认值 `64'hCA11AB1EBADCAB1E`，按 `RespWidth=64` 声明，赋值给 `r.data` 时按数据总线宽度“零扩展或截断”。于是在 32 位数据总线上看到的是其低 32 位 `0xBADCAB1E`（这正是 [doc/axi_xbar.md:31-36](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L31-L36) 所述）；在 64 位总线上则是完整的 `0xCA11AB1EBADCAB1E`。

#### 4.2.3 源码精读

先看 demux 如何被“多路一路”例化以容纳错误从端。`axi_xbar_unmuxed` 把分发请求/响应数组多开一槽（[src/axi_xbar_unmuxed.sv:84-90](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L84-L90)）：

```systemverilog
localparam int unsigned MstPortsIdxWidthOne =
    (Cfg.NoMstPorts == 32'd1) ? 32'd1 : unsigned'($clog2(Cfg.NoMstPorts + 1));
typedef logic [MstPortsIdxWidthOne-1:0] mst_port_idx_t;   // 能容纳 0..NoMstPorts（含错误槽）
...
req_t  [Cfg.NoSlvPorts-1:0][Cfg.NoMstPorts:0]  slv_reqs;  // 维度 [NoMstPorts:0] = NoMstPorts+1 槽
```

随后 demux 例化时把 `NoMstPorts` 参数写成 `Cfg.NoMstPorts + 1`（[src/axi_xbar_unmuxed.sv:164-193](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L164-L193)，关键行 `.NoMstPorts ( Cfg.NoMstPorts + 1 )`），译码错误从端接在多出的那一槽（[src/axi_xbar_unmuxed.sv:195-211](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L195-L211)）：

```systemverilog
axi_err_slv #(
  .AxiIdWidth ( Cfg.AxiIdWidthSlvPorts ),
  .axi_req_t  ( req_t                  ),
  .axi_resp_t ( resp_t                 ),
  .Resp       ( axi_pkg::RESP_DECERR   ),   // 明确返回译码错误
  .ATOPs      ( ATOPs                  ),
  .MaxTrans   ( 4                      )    // 事务在此终止，少接几笔以省资源
) i_axi_err_slv (
  .slv_req_i  ( slv_reqs[i][Cfg.NoMstPorts]  ),   // 第 NoMstPorts 槽 = 错误从端
  .slv_resp_o ( slv_resps[i][cfg_NoMstPorts] )
);
```

再看 `axi_err_slv` 内部。参数区给出 `Resp`/`RespData`/`MaxTrans` 等（[src/axi_err_slv.sv:19-28](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_err_slv.sv#L19-L28)）。写路径用一个 FIFO 记录在途写的 `id`，吃掉所有 W 拍，在 `w.last` 时把 `id` 推入 B 队列（[src/axi_err_slv.sv:83-121](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_err_slv.sv#L83-L121)），B 通道回 `Resp`：

```systemverilog
err_resp.b.id    = b_fifo_data;
err_resp.b.resp  = Resp;          // = RESP_DECERR
```

读路径用一个递减计数器（`counter`，初值装入 `ar.len`）产生 `len+1` 拍 R，每拍数据与响应码固定（[src/axi_err_slv.sv:183-221](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_err_slv.sv#L183-L221)）：

```systemverilog
err_resp.r.id    = r_fifo_data.id;
err_resp.r.data  = RespData;      // = 64'hCA11AB1EBADCAB1E，截断/扩展到 r.data 宽度
err_resp.r.resp  = Resp;          // = RESP_DECERR
err_resp.r.last  = (r_current_beat == '0);
```

模块还用一条断言限制 `Resp` 只能是 `DECERR` 或 `SLVERR`（[src/axi_err_slv.sv:247-249](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_err_slv.sv#L247-L249)），防止误把它配置成 OKAY。

> 补充：`axi_xbar_unmuxed` 还在另一处用到 `axi_err_slv`——当 `Connectivity` 矩阵把某条 slave→master 链路剪掉时（`gen_no_connection` 分支），那条链路也会挂一个 `MaxTrans=1` 的 `axi_err_slv` 兜底（[src/axi_xbar_unmuxed.sv:236-252](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L236-L252)）。即“地址译码成功、但该链路被禁用”同样会落到错误从端。

#### 4.2.4 代码实践

**目标**：在不跑仿真的前提下，依据源码预测一次“未映射读”将收到什么。

**步骤**：

1. 假设一张仅覆盖 `[0x0, 0x1000)` 的表，数据总线为 32 位。
2. 让一个 master 模块向 `0x0000_5000`（未映射）发起一次 `len=2` 的 INCR 读（即 3 拍）。
3. 对照 [src/axi_err_slv.sv:183-221](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_err_slv.sv#L183-L221) 推演 R 通道输出。

**预期结果**（确定性结论）：

- 地址译码失败 → 路由到内部 `axi_err_slv`。
- 收到 **3 拍** R（`len+1 = 3`），每拍 `r.resp = RESP_DECERR`（2’b11）。
- 每拍 `r.data = 0xBADCAB1E`（`RespData` 低 32 位）。
- 第 3 拍 `r.last = 1`。
- 若是写而非读，则不产生 R，只产生一个 `b.resp = RESP_DECERR` 的 B。

> 运行层面的确认“待本地验证”：可参考 `test/tb_axi_xbar.sv` 搭随机回归，向表外地址发读，观察 R 数据。

#### 4.2.5 小练习与答案

**练习 1**：为什么译码错误从端的 `MaxTrans` 在 xbar 里只设成 4，而不是和真实从端一样大？

**答案**：因为事务到了错误从端就“到此为止”——它只回错、不转发，不需要缓存大量在途数据。注释（[src/axi_xbar_unmuxed.sv:201-203](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L201-L203)）明确说是为了省资源；`MaxTrans=4` 仅限制“同时接收几笔后开始反压”。

**练习 2**：一次未映射的 `len=0` 读（单拍读），R 通道是几拍？

**答案**：`len+1 = 1` 拍。这一拍 `r.last` 直接为 1（计数器初值 0，立即命中 `r_current_beat == '0`）。

---

### 4.3 默认 master 端口：运行期重定向与切换限制

#### 4.3.1 概念说明

“默认 master 端口”是译码错误的**另一种归宿**。每个 slave 端口可以单独启用一个默认端口：当某个地址不匹配任何规则时，与其送到内部错误从端回 `DECERR`，不如改送到一个**真实存在的** master 端口（由 `default_mst_port_i[i]` 指定）。典型用途是把“未映射地址”统一引流到一个能兜底处理的端口（例如再接一层 xbar，或一个默认外设）。

注意它与错误从端的区别：默认端口是一个真实的 master 端口，事务会真正离开 xbar 去往下游；下游收到的地址就是原始地址（xbar 不改地址），下游若自己也认不得，仍可能回错。

#### 4.3.2 核心流程

两个 per-slave-port 输入控制这一行为（[src/axi_xbar_unmuxed.sv:73-78](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L73-L78)）：

```
en_default_mst_port_i[i]   // 是否为 slave 端口 i 启用默认端口（1 bit）
default_mst_port_i[i]      // 默认端口的索引（MstPortsIdxWidth bit）
```

它们直接接到 `addr_decode` 的 `en_default_idx_i` / `default_idx_i`。`addr_decode` 的约定是：**若启用了默认端口，则未命中地址不再报 `dec_error`，而是输出 `default_idx_i` 且 `dec_valid=1`**。于是 select 取到的就是一个合法的真实端口索引，事务正常路由出去，错误从端这一槽不会收到东西。

**运行期切换限制**：地址映射、`en_default_mst_port_i`、`default_mst_port_i` 都是输入信号，原则上可在运行期改变（[doc/axi_xbar.md:28-36](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L28-L36)），但**当任一 slave 端口的 AW 或 AR 通道存在未完成握手（`valid && !ready`）时，不得改动这些信号**。否则一笔已按旧表译码的事务可能在中途看到新表，导致行为不可预测。

#### 4.3.3 源码精读

`axi_xbar_unmuxed` 用一组 `assert property` 把上述限制钉死在仿真里（[src/axi_xbar_unmuxed.sv:140-161](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L140-L161)）。以 AW 为例：

```systemverilog
default_aw_mst_port_en: assert property(
  @(posedge clk_i) (slv_ports_req_i[i].aw_valid && !slv_ports_resp_o[i].aw_ready)
      |=> $stable(en_default_mst_port_i[i]))
  else $fatal (1, "It is not allowed to change the default mst port enable, \
                   when there is an unserved Aw beat.");
default_aw_mst_port: assert property(
  @(posedge clk_i) (slv_ports_req_i[i].aw_valid && !slv_ports_resp_o[i].aw_ready)
      |=> $stable(default_mst_port_i[i]))
  else $fatal (1, "It is not allowed to change the default mst port \
                   when there is an unserved Aw beat.");
```

含义：一旦某拍 `aw_valid` 高而 `aw_ready` 还没高（即这一拍 AW 处于 pending、尚未握手），那么**下一拍**这两个默认端口信号必须保持稳定（`$stable`），否则仿真直接 `$fatal`。AR 方向有完全对称的两条断言。注意这些断言包在 `pragma translate_off` 与 `ifndef VERILATOR / XSIM` 里，仅供仿真检查、不进入综合。

`addr_decode` 把默认端口逻辑与译码逻辑合在同一模块内，因此“启用默认”与“规则命中”共用同一个 `idx_o`/`dec_valid_o`/`dec_error_o` 输出口：启用且未命中时 `dec_error_o=0`、`idx_o=default_idx_i`；这正是 select 不再被改写到错误槽的原因（见 4.1.3 的 `dec_aw_error ? NoMstPorts : dec_aw`）。

#### 4.3.4 代码实践

**目标**：理解默认端口与错误从端的“二选一”关系。

**步骤**：

1. 阅读 [src/axi_xbar_unmuxed.sv:101-114](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L101-L114) 与 [src/axi_xbar_unmuxed.sv:131-134](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L131-L134)。
2. 设想 slave 端口 0 的 `en_default_mst_port_i[0] = 1`、`default_mst_port_i[0] = 2`，地址表只覆盖 `[0x0, 0x1000) → idx=0`。
3. 推演：地址 `0x9000`（未映射）会去哪里？若把 `en_default_mst_port_i[0]` 改回 0 呢？

**预期结果**：

- 启用默认端口：`0x9000` 送到真实 master 端口 2（不再进错误从端）。下游端口 2 拿到的地址仍是 `0x9000`。
- 关闭默认端口：`0x9000` 译码失败，select = `NoMstPorts`，进入内部 `axi_err_slv`，读返回 `0xBADCAB1E` / `RESP_DECERR`。
- 两种模式下，只要当前有一拍 AW/AR 处于 `valid && !ready`，就**不能**在此时翻转 `en_default_mst_port_i[0]` 或 `default_mst_port_i[0]`，否则触发上面的 `$fatal`。

#### 4.3.5 小练习与答案

**练习 1**：默认 master 端口的索引可以为 `NoMstPorts`（即错误从端那一槽）吗？

**答案**：不应这样设。`default_mst_port_i` 的位宽是 `MstPortsIdxWidth = $clog2(NoMstPorts)`，只能表示真实端口 `0..NoMstPorts-1`；它指向一个会真正接收事务的 master 端口，而不是内部错误从端。若想让未映射地址回 `DECERR`，正确做法是**关闭**默认端口（`en_default_mst_port_i = 0`）。

**练习 2**：为什么切换默认端口的限制用“`valid && !ready`”作为判据，而不是“有在途事务”？

**答案**：因为译码发生在 AW/AR **握手的那一拍**。只要这一拍的 `valid` 还没配上 `ready`（pending），译码就还没“落定”，此时改表会让这一拍在新旧表之间产生歧义。一旦握手完成（`valid && ready`），这笔事务的 select 已被 demux 锁存（参见 [u5-l1](u5-l1-demux-simple-and-demux.md) 的 W 突发队列与 id_counters），后续再改表不影响它，所以限制只需覆盖到“pending 那一拍”。

---

## 5. 综合实践

把本讲三个模块串起来：**为 3 个 master 端口写一张覆盖 `0x0–0x10000`（64 KiB）的地址映射规则数组，并说明访问一个未映射地址时的返回值。**

设数据总线 32 位、地址 32 位，选用 `axi_pkg::xbar_rule_32_t`。把 64 KiB 分给 3 个 master 端口（示例代码，非项目原有）：

```systemverilog
// 示例代码：覆盖 [0x0, 0x1_0000) 的三规则地址映射
localparam axi_pkg::xbar_rule_32_t [2:0] MyAddrMap = '{
  // 数组方向 [2:0]：下标越大优先级越高；这里三条区间不重叠，顺序不影响结果
  '{idx: 2, start_addr: 32'h0000_C000, end_addr: 32'h0001_0000}, // master 端口 2: [0xC000, 0x1_0000) 16 KiB
  '{idx: 1, start_addr: 32'h0000_4000, end_addr: 32'h0000_C000}, // master 端口 1: [0x4000, 0xC000) 32 KiB
  '{idx: 0, start_addr: 32'h0000_0000, end_addr: 32'h0000_4000}  // master 端口 0: [0x0000, 0x4000) 16 KiB
};
```

逐项核验：

| 地址 | 命中规则 | 路由到 |
|:--|:--|:--|
| `0x0000_3FFF` | 规则 0（`< 0x4000`） | master 端口 0 |
| `0x0000_4000` | 规则 1（`>= 0x4000`，前闭后开右端点归下一条） | master 端口 1 |
| `0x0000_BFFF` | 规则 1 | master 端口 1 |
| `0x0000_C000` | 规则 2 | master 端口 2 |
| `0x0000_FFFF` | 规则 2 | master 端口 2 |

**访问一个未映射地址**：这张表完整覆盖了 `[0x0, 0x1_0000)`，所以“未映射”地址只能在其之外，例如 `0x0002_0000`。在**未启用**默认端口（`en_default_mst_port_i = '0`）时：

- `0x0002_0000` 在三条规则中都不命中 → `dec_error = 1` → select = `NoMstPorts` → 进入内部 `axi_err_slv`。
- 若是**写**：收到一个 B，`b.resp = RESP_DECERR`（2’b11）。
- 若是**读**：收到 `len+1` 拍 R，每拍 `r.resp = RESP_DECERR`、`r.data = 0xBADCAB1E`（`RespData = 64'hCA11AB1EBADCAB1E` 在 32 位总线上的低 32 位）。
- 若改为**启用**默认端口（如 `en_default_mst_port_i[0]=1`、`default_mst_port_i[0]=1`），则 `0x0002_0000` 不再回错，而是被原样送到 master 端口 1，由端口 1 的下游决定如何处理。

**进阶自检**：把上表里规则 0 的 `end_addr` 故意改成 `0x0000_5000`（与规则 1 的 `[0x4000,0xC000)` 形成重叠区 `[0x4000,0x5000)`），然后判断地址 `0x4500` 的去向——依据“高位规则优先”，应去下标更大的规则 1（端口 1）。这能验证你是否真正理解了重叠语义。

> 运行确认“待本地验证”：可仿照 `test/tb_axi_xbar.sv` 的 `addr_map_gen`（[test/tb_axi_xbar.sv:108-117](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_xbar.sv#L108-L117)）把 `MyAddrMap` 接到 xbar，用随机主端向表内/表外地址发读写，观察 scoreboard 与 R/B 响应。

## 6. 本讲小结

- xbar 用**一张全局共享**的 `rule_t` 规则数组做地址→端口的路由，每条规则只有 `idx / start_addr / end_addr` 三字段；`axi_xbar_unmuxed` 为每个 slave 端口例化两份 `addr_decode`（AW、AR 各一）。
- 区间是**前闭后开**：`start_addr <= addr < end_addr`；区间**允许重叠**，重叠时**数组下标更大（更高有效位）的规则胜出**。
- 译码失败时事务被改写到 demux 的“第 `NoMstPorts` 槽”——即每个 slave 端口专属的 `axi_err_slv`。demux 因此被例化成 `NoMstPorts + 1` 路。
- `axi_err_slv` 吸收事务并回 `RESP_DECERR`：写回一个 B，读回 `len+1` 拍 R，读数据为 `RespData`（默认 `64'hCA11AB1EBADCAB1E`，32 位总线上即 `0xBADCAB1E`）。
- 每个 slave 端口可启用一个**默认 master 端口**，把未映射事务改送到一个真实端口而非错误从端；该机制由 `en_default_mst_port_i` / `default_mst_port_i` 控制。
- 地址映射与默认端口都可在运行期改动，但**任一 AW/AR 处于 `valid && !ready` 期间必须保持稳定**，否则触发 `$fatal` 断言。

## 7. 下一步学习建议

- 接下来阅读 [u6-l3 排序、停顿与无死锁设计](u6-l3-xbar-ordering-deadlock.md)，了解“同 ID 同方向必须保序”这一约束如何在 demux/mux 之间引发停顿，以及为什么 demux 与 mux 之间不能插 spill 寄存器（W 通道四条死锁条件）。
- 想更深入理解 select 信号如何被 demux 消费、同 ID 跨端口为何会停顿，可回看 [u5-l1 axi_demux](u5-l1-demux-simple-and-demux.md) 与 [u5-l2 axi_demux_id_counters](u5-l2-demux-id-counters.md)。
- 若对 `addr_decode` 的内部实现（高位优先的具体判决逻辑）感兴趣，可到 `common_cells` 仓库阅读 `addr_decode.sv` 源码——本讲只从 xbar 的端口行为层面描述了它。
