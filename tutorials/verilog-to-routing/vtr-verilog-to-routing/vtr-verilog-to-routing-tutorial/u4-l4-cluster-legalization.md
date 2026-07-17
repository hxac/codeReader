# 聚簇合法化与簇内布线

## 1. 本讲目标

上一讲（u4-l3）我们看到 `GreedyClusterer` 的 `do_clustering` 主循环如何用种子选择器与候选选择器，把分子一个一个往簇里塞。但「塞进去」并不等于「合法」——一个簇（CLB）并不是一口可以随意装原语的口袋，原子之间必须能通过架构 `<interconnect>` 描述的内部连线真正连通，且不超出引脚、面积、布局、NoC 分组等约束。

本讲聚焦这一道「门禁」：**聚簇合法化（cluster legalization）与簇内布线（intra-cluster routing）**。读完本讲，你应当能够：

1. 说清簇内布线图 `lb_type_rr_graph` 是什么、它和器件级 RR Graph（u6-l1）有何不同、它是如何由 PB 图构造出来的。
2. 描述 `ClusterLegalizer::try_pack_molecule` 这条由便宜到昂贵的合法化流水线，以及 `FULL` / `SKIP_INTRA_LB_ROUTE` 两种策略的差别。
3. 理解三层「可行性过滤」：快速兼容性检查、引脚可行性过滤、簇内 Pathfinder 布线，以及合法化失败时分子级与簇级的回退路径。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，为什么打包需要「布线」？** 打包把若干原子原语（LUT、FF、进位链等）装进一个逻辑块。但逻辑块内部的连线资源是有限的：架构 XML 用 `<interconnect>`（`complete`、`direct`、`mux`）描述了块内引脚之间「谁能连到谁」。一组原子即便各自都有空位，也可能因为内部连线走不通而无法共处一簇。因此「装得下」≠「连得通」，必须做一次簇内布线来确认。

**第二，什么是 Pathfinder 协商式布线？** 这是 VPR 在器件级布线（u6）和簇内布线都采用的核心算法。简言之：先允许同一个布线资源被多个线网「争用」（产生拥塞），再通过逐轮抬高拥塞资源的代价（present cost + historical cost），逼迫线网改走别的路径，直到所有线网都无冲突或确认无解。本讲看到的是它在「簇内」这一小图上的实例。

**第三，关键术语速查：**

| 术语 | 含义 |
|------|------|
| 分子（molecule） | 打包的最小搬运单元，u4-l2 讲过，可含一个或多个原子 |
| `t_pb` / `t_pb_graph_node` | 物理块实例层 / 模板层，u4-l1 讲过 |
| `lb_type_rr_graph` | 本讲主角一：逻辑块**类型**内部的布线资源图 |
| `ClusterLegalizer` | 本讲主角二：判定分子能否进入簇的「门禁」类 |
| `ClusterRouter` | 本讲主角三：在 `lb_type_rr_graph` 上跑 Pathfinder 的簇内路由器 |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [vpr/src/pack/pack_types.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pack_types.h) | 定义簇内布线图的节点结构 `t_lb_type_rr_node` 与节点类型枚举 `e_lb_rr_type` |
| [vpr/src/pack/lb_type_rr_graph.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/lb_type_rr_graph.h) / [.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/lb_type_rr_graph.cpp) | 簇内布线图的构造、释放与访问函数 |
| [vpr/src/pack/cluster_router.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_router.h) / [.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec62818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_router.cpp) | 簇内路由器 `ClusterRouter`，跑 Pathfinder 判定可布线性 |
| [vpr/src/pack/cluster_legalizer.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.h) / [.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.cpp) | 合法化管理器 `ClusterLegalizer`，串起所有合法性检查 |
| [vpr/src/pack/greedy_clusterer.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_clusterer.cpp) | 调用合法化器的上层（u4-l3），含两级回退 |
| [vpr/src/base/setup_vpr.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_vpr.cpp) | 在 setup 阶段一次性建好所有类型的簇内布线图 |

---

## 4. 核心概念与源码讲解

### 4.1 簇内布线图 lb_type_rr_graph

#### 4.1.1 概念说明

簇内布线图（lb = logic block，rr = routing resource）是**针对每一种逻辑块类型**预先建好的一张有向图，刻画「这个类型的逻辑块内部，哪些引脚能通过内部互连连到哪些引脚」。它是 `ClusterRouter` 跑 Pathfinder 的「棋盘」。

务必把它和第 6 单元要讲的**器件级 RR Graph** 区分开：

| 维度 | 簇内布线图 `lb_type_rr_graph` | 器件级 RR Graph |
|------|------------------------------|------------------|
| 范围 | 单个逻辑块**类型**的内部 | 整颗 FPGA 芯片 |
| 节点 | 块内 `pb_graph_pin` + 几个虚拟源/汇 | 器件上所有线段、引脚、开关盒 |
| 用途 | 打包阶段判定一簇原子能否内部连通 | 布线阶段为整网表寻径 |
| 数量 | 每种逻辑块类型一张 | 全局一张 |
| 生命周期 | setup 建一次，打包全程复用 | 布线前按通道宽度建 |

一句话：器件级 RR Graph 管「块与块之间怎么走」，簇内布线图管「一个块内部怎么走」。本讲只谈后者。

#### 4.1.2 核心流程

簇内布线图的构造规则，源码注释写得非常清楚（值得逐字读）：

> 每个 `pb_graph_pin` 对应一个节点，按该 pin 的 `pin_count_in_cluster` 编号；
> 驱动进逻辑块的外部线网来自一个「外部源」，编号 = 总引脚数；
> 逻辑块驱动出去的外部线网汇到一个「外部汇」，编号 = 总引脚数 + 1；
> 每个原语输入引脚驱动一个汇节点，因输入引脚本身已有节点，故这些汇追加在向量末尾；
> 每个原语输出引脚是一个源。

节点类型只有三种（[pack_types.h:L20-L25](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pack_types.h#L20-L25)）：`LB_SOURCE`（源，能驱动别人）、`LB_SINK`（汇，被驱动到此终止）、`LB_INTERMEDIATE`（中间节点，既被驱动也驱动别人）。一个簇内图大致长这样：

```
外部源 ──> 顶层输入/时钟引脚 ──> [内部互连 mux/direct] ──> 原语输入引脚 ──> 原语输入汇
                                     ^                         |
                                     |                         v
                                  (模式相关)              原语输出引脚(源)
                                     |                         |
顶层输出引针 <── [内部互连] <─────────┴─────────────────────────┘
   │
   └──> 外部汇   (块输出送出给别的块)
   └──> 外部 rr (反馈节点：把块输出绕回块输入，代价极高，尽量不用)
```

其中**「模式相关」（mode-dependent）** 是这张图最精巧的地方：一个引脚的下游连边取决于其父块当前处于哪个 `mode`（u4-l1 讲过 `t_pb_type → t_mode → t_pb_type` 的递归）。因此节点的出边不是一维数组，而是 `outedges[imode][iedge]`——按模式分桶。

整张图在 setup 阶段一次性建好，调用点是 [setup_vpr.cpp:L346-L348](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_vpr.cpp#L346-L348)，紧跟在 PB 图构建之后。

#### 4.1.3 源码精读

**节点结构 `t_lb_type_rr_node`**（[pack_types.h:L57-L77](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pack_types.h#L57-L77)）：

```cpp
struct t_lb_type_rr_node {
    short capacity;            // 该节点能同时被几个线网使用
    int num_modes;             // 模式数（决定 outedges 的第一维）
    short* num_fanout;         // [0..num_modes-1] 每个模式下的出边数
    enum e_lb_rr_type type;    // LB_SOURCE / LB_SINK / LB_INTERMEDIATE
    t_lb_type_rr_node_edge** outedges; // [mode][edge] 二维出边表
    t_pb_graph_pin* pb_graph_pin;      // 关联的 pb_graph_pin（虚拟节点为空）
    float intrinsic_cost;
};
```

每条出边只是一个目标节点索引加固有代价（[pack_types.h:L51-L54](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pack_types.h#L51-L54)）：

```cpp
struct t_lb_type_rr_node_edge {
    int node_index;
    float intrinsic_cost;
};
```

`capacity` 是合法性的命脉：簇内布线成功与否，最终就看是否每个节点的占用数 `occ` 都 ≤ `capacity`（见 4.3.3）。

**构造入口**（[lb_type_rr_graph.cpp:L53-L70](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/lb_type_rr_graph.cpp#L53-L70)）：遍历 `device_ctx.logical_block_types`，对每个非空类型调用 `alloc_and_load_lb_type_rr_graph_for_type` 建一张图，结果存进按类型索引的数组。文件头部的长注释（[lb_type_rr_graph.cpp:L1-L18](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/lb_type_rr_graph.cpp#L1-L18)）即 4.1.2 的构造规则原文。

**三个特殊虚拟节点**（[lb_type_rr_graph.cpp:L181-L184](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/lb_type_rr_graph.cpp#L181-L184)）：外部源、外部汇、外部 rr（反馈）分别落在 `total_pb_pins`、`+1`、`+2`。注意外部 rr 连回块内输入引脚的代价被刻意设为 `1000`（[lb_type_rr_graph.cpp:L267](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/lb_type_rr_graph.cpp#L267)），注释写明「set cost high to avoid using external interconnect unless necessary」——即「能不把信号绕出块外就别绕」。

**原语输入引脚→汇**（[lb_type_rr_graph.cpp:L303-L340](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/lb_type_rr_graph.cpp#L303-L340)）：每个原语输入 pin 建一个 `LB_INTERMEDIATE` 节点，再追加一个 `LB_SINK`；若端口声明了引脚等价（`PortEquivalence`），则同端口的多个 pin 共享一个 capacity = 端口宽度的汇——这正是「等价引脚可互换」在图上的体现。

**对外访问函数**（[lb_type_rr_graph.h:L16-L18](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/lb_type_rr_graph.h#L16-L18)）：`get_lb_type_rr_graph_ext_source_index` / `ext_sink_index`（实现 [lb_type_rr_graph.cpp:L108-L115](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/lb_type_rr_graph.cpp#L108-L115)）给出虚拟源/汇的索引；`get_lb_type_rr_graph_edge_mode`（[L117-L127](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/lb_type_rr_graph.cpp#L117-L127)）反查两节点间的连边属于哪个模式。

#### 4.1.4 代码实践

**目标**：亲眼看到一张簇内布线图长什么样。

**步骤**：

1. 打开 [lb_type_rr_graph.cpp:L641-L674](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/lb_type_rr_graph.cpp#L641-L674) 的 `print_lb_type_rr_graph`，读懂它打印的格式：节点号、关联的 `pb_graph_node[port][pin]`、类型、容量、每个模式的出边 `(目标, 代价)`。
2. 找到 `echo_lb_type_rr_graphs`（[lb_type_rr_graph.cpp:L134-L150](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/lb_type_rr_graph.cpp#L134-L150)），它把所有类型的图写入一个文件。
3. 在 `vpr` 源码里搜索 `echo_lb_type_rr_graphs` 的调用处（grep 整个仓库），看它是被哪个 echo 开关触发的。

**需要观察的现象**：在打印结果中，前 `total_pb_pins` 个节点一一对应 `pb_graph_pin`；编号 `total_pb_pins` / `+1` / `+2` 是三个虚拟节点；末尾追加的是各原语输入/时钟引脚的汇节点。某个原语输出 pin 的 `outedges` 会按模式分成多组。

**预期结果**：你能指着打印输出说出「这条边属于 mode X，代价是 Y」。

> 说明：本实践为源码阅读型，是否能在某次运行中产出该 echo 文件取决于具体的 echo 命令行开关，**待本地验证**触发方式。

#### 4.1.5 小练习与答案

**练习 1**：簇内布线图的节点编号 0..N-1 是按什么分配的？为什么虚拟源/汇要排在所有真实引脚之后？

**答案**：真实引脚节点按 `pb_graph_pin::pin_count_in_cluster` 编号（u4-l1 讲过的簇内扁平索引），保证「引脚 ↔ 节点」一一对应、O(1) 互查；虚拟源/汇/反馈不对应任何具体引脚，故追加在 `total_pb_pins` 之后，避免占用引脚编号空间。

**练习 2**：为什么原语输出引脚的 `outedges` 是 `outedges[imode][iedge]` 二维的，而原语输入引脚的出边却只有一维？

**答案**：原语输出连向的内部互连（`mux`/`complete`/`direct`）属于其**父块的某个 mode**，父块处于不同 mode 时下游拓扑不同，故按模式分桶；原语输入引脚只连到自己的汇节点，与模式无关，故单一出边即可（见 [lb_type_rr_graph.cpp:L313-L339](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/lb_type_rr_graph.cpp#L313-L339)）。

---

### 4.2 聚簇合法化流程

#### 4.2.1 概念说明

`ClusterLegalizer`（[cluster_legalizer.h:L223](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.h#L223)）是聚簇阶段的「门禁」。它自己内部维护一族 `LegalizationCluster`，对外提供 `start_new_cluster` / `add_mol_to_cluster` / `check_cluster_legality` / `destroy_cluster` 等接口。它的设计目标是**自包含**——注释明确写到「able to be called externally to the Packer」，即可以脱离 `GreedyClusterer` 单独使用。

合法化的本质是一条**由便宜到昂贵的检查流水线**：先用几乎免费的检查（有没有空原语、布局约束、NoC 分组）筛掉明显不行的分子，再用稍贵的引脚可行性过滤，最后才动用最贵的簇内 Pathfinder 布线。这样能把昂贵的布线检查降到最少。

合法化器支持两种策略（[cluster_legalizer.h:L68-L71](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.h#L68-L71)）：

- `SKIP_INTRA_LB_ROUTE`：每次加分子**不做**簇内布线，只在最后统一做一次。便宜但可能把「其实走不通」的分子也塞进去，直到终检才暴露。
- `FULL`：每加一个分子就跑一次完整簇内布线。昂贵但保证簇在建过程中始终合法。

#### 4.2.2 核心流程

合法化的核心是 `try_pack_molecule`（[cluster_legalizer.cpp:L1206](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.cpp#L1206)），它对单个分子执行如下流水线：

```
try_pack_molecule(molecule, cluster)
  │
  ├─ 1. 长链冲突检查：本簇已有长链 & 该分子也是长链 → FAIL_FEASIBLE
  ├─ 2. 布局约束检查：逐原子与簇 PartitionRegion 求交 → FAIL_FLOORPLANNING
  ├─ 3. NoC 分组检查：逐原子 NoC 组一致性 → FAIL_NOC_GROUP
  │
  ├─ 4. 构造候选原语优先队列 primitives_alive
  └─ while (未 PASS 且队列非空):           ← 换不同根原语重试
        ├─ 弹出一个候选根原语 root
        ├─ 5. 逐原子 try_place_atom_block_rec：把分子各原子落到 PB 树空位
        ├─ 6. [若都放下] 引脚可行性过滤 check_lookahead_pins_used
        │      └─ 失败 → FAIL_FEASIBLE，换下一个 root
        ├─ 7. [若策略=FULL] 簇内布线 try_intra_lb_route (循环到无模式冲突)
        │      └─ 不可布 → FAIL_ROUTE，换下一个 root
        └─ 8. 全过 → 提交(commit)；否则 → 回退(rollback) 见 4.3
```

注意第 4 步那个 `while` 循环：分子可能因为选错了根原语位置而失败，合法化器会**换一个候选根原语再试**，直到成功或候选耗尽。`start_new_cluster`（[L1572](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.cpp#L1572)）与 `add_mol_to_cluster`（[L1633](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.cpp#L1633)）都只是 `try_pack_molecule` 的薄包装。

#### 4.2.3 源码精读

**簇的载体 `LegalizationCluster`**（[cluster_legalizer.h:L88-L157](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.h#L88-L157)）：一个簇持有 `molecules`（已合法装入的分子表）、`pb`（块内物理块层次根）、`type`（逻辑块类型）、`pr`（合法布局区域 PartitionRegion）、`noc_grp_id`（NoC 分组）、`cluster_router`（本簇专属的簇内路由器）、`placement_stats`（原子当前落位统计）。注意 `cluster_router` 是**每簇一个**——这样每簇保留各自的布线状态，便于增量重布线与回退。

**长链冲突检查**（[cluster_legalizer.cpp:L1249-L1253](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.cpp#L1249-L1253)）：若簇里已有一条长链（`has_long_chain`）且当前分子也属长链，直接 `BLK_FAILED_FEASIBLE`。注释解释：避免产生布局约束不兼容或过长的布局宏，损害布局灵活性。

**候选根原语 while 循环**（[cluster_legalizer.cpp:L1309-L1345](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.cpp#L1309-L1345)）：从优先队列 `primitives_alive` 弹出一个根原语，对分子里每个原子调 `try_place_atom_block_rec` 尝试落位；任一原子落位失败就记下 `failed_location` 并换下一个根。

**详细布线分派**（[cluster_legalizer.cpp:L1429-L1455](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.cpp#L1429-L1455)）：只有策略为 `FULL` 时才跑簇内布线。代码用 `do { reset; routed = try_intra_lb_route(...); } while (mode_status.is_mode_issue());` 反复重布，直到没有模式冲突；并据 `PackingSignatureTree`（合法化记忆树，可选）跳过已知合法/非法的模式。布线失败置 `BLK_FAILED_ROUTE`。

**最终合法性闸门**：`check_cluster_legality`（[cluster_legalizer.cpp:L1746-L1759](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.cpp#L1746-L1759)）**无视当前策略**，强制跑一次簇内布线来下最终结论。`ensure_legal_final_routing`（[L1761-L1786](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.cpp#L1761-L1786)）是它的带快进版本：先用 `is_saved_route_valid()` 判断「上次的合法布线是否仍覆盖当前所有线网」，若覆盖则直接返回真，省掉重布线。

#### 4.2.4 代码实践

**目标**：跟踪一个分子从「候选」到「合法装入」或「被拒」的全过程。

**步骤**：

1. 打开 [cluster_router.h:L222-L244](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_router.h#L222-L244) 的 `ClusterRouter` 用法示例注释——它给出了一条标准的「构造→add_atom_as_target→try_intra_lb_route→alloc_and_load_pb_route→clean」使用链。
2. 在 `try_pack_molecule` 里对照 [cluster_legalizer.cpp:L1309](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.cpp#L1309) 的 while 循环，回答：当 `try_place_atom_block_rec` 失败后，代码会做哪些清理才进入下一次循环尝试？

**需要观察的现象**：失败分支（见 4.3.3）会先把已经塞进 `cluster.molecules` 的分子 `pop_back`，再对已落位的原子调 `remove_atom_from_target` 和 `revert_place_atom_block`，然后 `cleanup_pb`、把失败原语挪进 tried 集合，最后回到 while 顶部换一个根原语重试。

**预期结果**：你能画出「分子被拒 → 清理现场 → 换根重试 → 重试耗尽返回失败状态」的状态转换。

#### 4.2.5 小练习与答案

**练习 1**：`start_new_cluster` 在尝试装第一个种子分子时，用的外部引脚利用率上限是什么？为什么？

**答案**：用 `FULL_EXTERNAL_PIN_UTIL(1., 1.)`（[cluster_legalizer.cpp:L1601-L1606](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.cpp#L1601-L1606)），即允许使用全部簇引脚。因为种子是簇里的第一个分子，此刻没有任何理由限制引脚使用，应当让它尽可能装下。

**练习 2**：`SKIP_INTRA_LB_ROUTE` 策略下，分子加进去时不会跑布线，那簇的合法性最终由谁保证？

**答案**：由上层在加完所有分子后调用 `ensure_legal_final_routing`（即 `check_cluster_legality`）做一次总检来保证；若总检失败，上层会销毁整个簇（见 4.3.4）。

---

### 4.3 可行性过滤与失败回退

#### 4.3.1 概念说明

合法化流水线里，除了最后的簇内布线，前面每一站都是「可行性过滤」——用远低于布线的代价尽早剔除注定不行的分子。失败处理则分两层：**分子级回退**（分子在某个簇里装不下，清理现场、换根或换簇重试）与**簇级回退**（整个簇最终建不出来，销毁重建）。

失败原因被规整成一个枚举 `e_block_pack_status`（[cluster_legalizer.h:L74-L81](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.h#L74-L81)）：

| 状态 | 含义 |
|------|------|
| `BLK_PASSED` | 通过全部检查 |
| `BLK_FAILED_FEASIBLE` | 快速可行性不过（无空原语、引脚不足、长链冲突） |
| `BLK_FAILED_ROUTE` | 簇内布线走不通 |
| `BLK_FAILED_FLOORPLANNING` | 与簇当前布局区域不兼容 |
| `BLK_FAILED_NOC_GROUP` | NoC 分组冲突 |

这套分类让上层能据此决定回退策略。

#### 4.3.2 核心流程：三层可行性过滤

```
[第 0 层] is_molecule_compatible          必要不充分：每原子有空原语？  O(快)
            │ 不过 → 上层根本不把它列为候选
            ▼
[第 1 层] try_pack_molecule 内的静态检查    长链 / 布局 / NoC / 引脚可行性过滤
            │ 不过 → BLK_FAILED_FEASIBLE / FLOORPLANNING / NOC
            ▼
[第 2 层] try_intra_lb_route (Pathfinder)  簇内布线，定论性  O(贵)
            │ 不过 → BLK_FAILED_ROUTE
            ▼
        BLK_PASSED
```

**第 0 层**是最便宜的「必要不充分」检查。注释原话（[cluster_legalizer.h:L478-L480](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.h#L478-L480)）：「This is a necessary but not sufficient test ... you can save runtime for impossible cases vs. calling the full checks.」

**引脚可行性过滤**（第 1 层里的重头戏）：开启 `enable_pin_feasibility_filter_` 时，分子被临时加入后，调 `try_update_lookahead_pins_used` 统计簇内各类互相连通引脚的剩余量，再用 `check_lookahead_pins_used` 比对外部引脚利用率上限 `max_external_pin_util`（[cluster_legalizer.cpp:L1354-L1365](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.cpp#L1354-L1365)）。它用「前瞻」估计未来引脚需求，能在不跑布线的前提下淘汰引脚不够的分子，是性能关键。

#### 4.3.3 核心流程：簇内 Pathfinder 布线

`ClusterRouter::try_intra_lb_route`（[cluster_router.cpp:L444](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_router.cpp#L444)）是协商式布线的簇内版。每条簇内线网 `t_intra_lb_net`（[cluster_router.h:L67-L87](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_router.h#L67-L87)）有若干终端（0 号是源，其余是汇）。算法主循环：

```
for iter in 0..max_iterations:
    pres_con_fac *= pres_fac_mult            # 逐轮抬高现时拥塞代价
    for each net inet:
        从源开始，对每个汇用优先队列做 maze 搜索 (try_expand_nodes_)
        把搜到的路径加回路由树 (add_to_rt_)
    if 无「不可能」标记:
        if is_route_success_(): 成功           # 所有节点 occ <= capacity
成功 → save_and_reset_lb_route_() 保存解
```

每个节点维护一份统计 `t_lb_rr_node_stats`（[cluster_router.h:L30-L45](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_router.h#L30-L45)）：`occ`（当前占用）、`mode`（所处模式）、`historical_usage`（历史占用，用于 hist_cost）。成功的判据极简——没有任何节点超容（[cluster_router.cpp:L1256-L1266](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_router.cpp#L1256-L1266)）：

```cpp
bool ClusterRouter::is_route_success_() {
    for (size_t inode = 0; inode < lb_type_graph.size(); inode++)
        if (lb_rr_node_stats_[inode].occ > lb_type_graph[inode].capacity)
            return false;     // 有节点被多个线网争用 → 仍拥塞
    return true;
}
```

为防死循环，迭代次数受 `params_.max_iterations` 上限约束；模式冲突（`mode_status.is_mode_issue()`）时扩大搜索到「所有模式」重试（[cluster_router.cpp:L499-L503](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_router.cpp#L499-L503)）。

#### 4.3.4 核心流程：两层回退

**分子级回退**——`try_pack_molecule` 失败分支（[cluster_legalizer.cpp:L1526-L1558](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.cpp#L1526-L1558)）做了完整的现场清理：

1. `cluster.molecules.pop_back()`——把临时塞进去的分子移除；
2. 对已落位原子调 `cluster_router.remove_atom_from_target`——从路由器目标集移除；
3. 对已落位原子调 `revert_place_atom_block`——回退 PB 树占位；
4. `reset_molecule_info`——清除分子链信息；
5. （若启用 PST）`rollback_to_checkpoint`——回退合法化记忆树游标；
6. `cleanup_pb`——释放未用的 pb 并重置模式；
7. `move_inflight_to_tried`——把失败的原语标记为「试过」。

随后回到 while 顶部换一个根原语重试；若候选耗尽，`try_pack_molecule` 返回失败状态给上层。

**簇级回退**——`GreedyClusterer` 的两级策略（[greedy_clusterer.cpp:L179-L204](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_clusterer.cpp#L179-L204)）：先用便宜的 `SKIP_INTRA_LB_ROUTE` 长一遍簇；若最终 `ensure_legal_final_routing` 不过（[L341-L350](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_clusterer.cpp#L341-L350)），就 `destroy_cluster` + `compress` 销毁，再用 `FULL` 策略重新长一遍。这呼应 u4-l3 讲过的「先快后慢」合法化。

**路由器级回退**——`ClusterRouter` 内部用 `saved_lb_nets_` 保存上一次成功解（`save_and_reset_lb_route_`），并通过 `is_saved_route_valid`（[cluster_router.cpp:L161](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_router.cpp#L161)）与「热启动」（`enable_hot_start_`）在重布线时复用未变线网的路由树，避免重复劳动。

#### 4.3.5 源码精读：第 0 层快速检查

`is_molecule_compatible`（[cluster_legalizer.cpp:L1950-L1982](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.cpp#L1950-L1982)）逐原子调用 `exists_free_primitive_for_atom_block`，只要有一个原子找不到空原语就返回假。注释坦承它是「fast but not robust」——只查单原子，不保证整个分子放得下，因此是「必要不充分」。

#### 4.3.6 代码实践

**目标**：让合法化失败真实地「被看见」。

**步骤**：

1. 阅读 [cluster_legalizer.cpp:L1232-L1242](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.cpp#L1232-L1242) 与 [L1244-L1253](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.cpp#L1244-L1253) 的日志输出，注意 `VTR_LOGV(log_verbosity_ > 2, ... "FAILED pack molecule reason: ...")` 这一类语句——它们用 `log_verbosity_` 控制是否打印。
2. 找到合法化器构造函数里 `log_verbosity_` 的来源（[cluster_legalizer.cpp:L1827](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.cpp#L1827)），再往上追到 `read_options`，确认提升它对应的命令行参数。
3. （可选运行）把该 verbosity 调高，跑一个 `--pack` 任务，在日志里 grep `FAILED pack molecule reason`，统计各类失败原因的出现次数。

**需要观察的现象**：日志会逐分子打印失败原因（`floorplanning_conflict`、`noc_group_conflict`、`long_chain_conflict`、`Pin Feasibility Filter`、`Detailed Routing Legality` 等），对应 `e_block_pack_status` 的各个取值。

**预期结果**：你能把日志里某条 `FAILED` 与本讲流水线的某一站精确对上号。命令行的确切参数名**待本地验证**。

#### 4.3.7 小练习与答案

**练习 1**：`is_route_success_` 只检查 `occ <= capacity`，那「所有线网都连上了汇」这件事由谁保证？

**答案**：由主循环里 `try_expand_nodes_` 的返回值 `is_impossible` 保证——若某个汇在扩展过程中始终搜不到，会置 `is_impossible`，循环提前判定本次布线失败（[cluster_router.cpp:L497-L503](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_router.cpp#L497-L503)）。`is_route_success_` 只在「没有不可达汇」的前提下，再确认没有拥塞。

**练习 2**：分子级回退里，为什么要先 `remove_atom_from_target` 再 `revert_place_atom_block`，顺序不能反？

**答案**：`remove_atom_from_target` 先把原子从路由器的目标线网集里摘掉，使后续布线不再考虑它；`revert_place_atom_block` 再回收 PB 树里的物理占位。若反过来先回收占位，路由器里仍残留指向已被释放 pb 的目标引用，会读到悬空状态——先摘目标、再回收占位是安全的依赖顺序。

---

## 5. 综合实践

把本讲三块知识串起来，完成一次「簇内布线图 → 合法化 → 失败回退」的端到端跟踪。

**任务**：假设你要回答「为什么某个分子没能和某个种子装进同一个 CLB？」请按下面顺序取证：

1. **图结构侧**：用 4.1.4 的方法导出该 CLB 类型的簇内布线图，找到种子原子输出引脚对应的 `LB_SOURCE` 节点，以及候选分子输入引脚对应的 `LB_SINK` 节点，人工判断它们之间是否存在**模式相关**的连通路径。
2. **合法化侧**：在 `try_pack_molecule` 里设断点或在 [cluster_legalizer.cpp:L1354-L1365](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.cpp#L1354-L1365)（引脚过滤）与 [L1429-L1455](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.cpp#L1429-L1455)（详细布线）附近加临时日志，确认分子究竟死在哪一站、返回哪个 `e_block_pack_status`。
3. **回退侧**：跟踪失败后 [cluster_legalizer.cpp:L1526-L1558](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.cpp#L1526-L1558) 的清理是否完整执行，以及上层 `GreedyClusterer` 是否触发了 `FULL` 策略重试（[greedy_clusterer.cpp:L194-L204](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/greedy_clusterer.cpp#L194-L204)）。

**交付**：一张包含「失败站点 → 状态码 → 触发的回退动作」三列的表格，并用一句话给出该分子被拒的根本原因（例如「两个原子需经 mode 1 的 mux 连通，但簇已固定在 mode 0」）。

> 注意：本实践涉及加日志或断点调试，需自行构建 VPR（参考 u1-l2 的 `make -j8 vpr`）。若仅做静态阅读，至少应能凭源码推理填写出表格前两列。

## 6. 本讲小结

- **簇内布线图 `lb_type_rr_graph`** 是每种逻辑块类型内部连线的有向图，节点为 `pb_graph_pin` 加三个虚拟源/汇/反馈节点，节点类型仅 `LB_SOURCE` / `LB_SINK` / `LB_INTERMEDIATE`，出边按父块**模式**分桶（`outedges[imode]`），在 setup 阶段一次性建好。
- **`ClusterLegalizer`** 是自包含的「门禁」，用 `try_pack_molecule` 跑一条由便宜到贵的检查流水线：长链/布局/NoC 静态检查 → 引脚可行性过滤 → 簇内 Pathfinder 布线；支持 `SKIP_INTRA_LB_ROUTE`（先塞后检）与 `FULL`（边塞边检）两种策略。
- **三层可行性过滤**层层省钱：`is_molecule_compatible`（必要不充分的空原语检查）→ 静态约束与引脚可行性过滤 → 定论性的簇内布线；失败原因被 `e_block_pack_status` 枚举归一。
- **簇内布线**用协商式 Pathfinder：逐轮抬高拥塞代价直到所有节点 `occ ≤ capacity`（`is_route_success_`），受 `max_iterations` 与模式冲突重试约束，成功解存入 `saved_lb_nets_` 供回退/热启动复用。
- **失败回退分两级**：分子级（`pop_back` + `remove_atom_from_target` + `revert_place_atom_block` + PST 回滚 + `cleanup_pb`，然后换根原语重试）；簇级（`GreedyClusterer` 先 `SKIP` 后 `FULL`，仍不过则 `destroy_cluster` + `compress`）。

## 7. 下一步学习建议

本讲结束，「打包 Packing」单元（u4）的四个最小模块——总览与 PB 图、分子、贪心聚簇器、合法化与簇内布线——已全部讲完，你已具备从 `AtomNetlist` 到 `ClusteredNetlist` 的完整打包视角。接下来建议：

1. **横向对照布线算法**：进入第 6 单元（u6-l2 连接路由器、u6-l3 连接级布线），把本讲的**簇内 Pathfinder** 与器件级 Pathfinder 对比阅读——同样是协商式拥塞，但器件级面对的是 `rr_graph_view` 与通道宽度，规模与前瞻机制（router lookahead）都更复杂。
2. **纵向追产物**：合法化成功后，`ClusterRouter::alloc_and_load_pb_route` 产出 `t_pb_route`，它被写入 `ClusteredNetlist` 的 `t_pb`（u3-l3）。可顺这条线阅读 `clustered_netlist.h`，看簇内布线结果如何成为布局阶段的输入。
3. **回看合法化记忆树**：本讲多次提到可选的 `PackingSignatureTree`（合法化记忆树）。若你对「如何用记忆避免重复布线检查」感兴趣，可阅读 `packing_signature_tree.h`，它是本讲流水线的一项重要运行时优化。
