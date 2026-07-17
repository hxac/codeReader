# 路由资源图 RR Graph

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「路由资源图（Routing Resource Graph，RR Graph）」到底把 FPGA 的什么东西抽象成了图，节点和边分别对应什么物理对象。
- 列举 RR 节点的全部类型（`SOURCE/SINK/IPIN/OPIN/CHANX/CHANY/CHANZ/MUX`）与边的开关类型，并解释 `ptc_num`、方向、坐标、`capacity` 等关键属性的含义。
- 理解为什么 VPR 把 RR 图拆成「可写构建器 `RRGraphBuilder`」与「只读视图 `RRGraphView`」两层，并能看懂视图里典型的「迭代节点 / 查询边」代码。
- 跟踪从命令行通道宽度到 `vpr_create_rr_graph → create_rr_graph → build_rr_graph` 的建图链路，解释通道宽度 `W` 如何决定图的规模，以及「通道宽度不变就跳过重建」这一缓存机制。

本讲是布线单元（第 6 单元）的起点：RR 图是后续连接路由器（u6-l2）、迭代布线（u6-l3）和前瞻代价（u6-l4）搜索与估算的全部基础。

## 2. 前置知识

在进入本讲前，请确认你已经掌握以下概念（来自前序讲义）：

- **器件网格 DeviceGrid**（u2-l3）：FPGA 被表示成一个 `[layer][x][y]` 的三维 `DeviceGrid`，每格是一个 `t_grid_tile`，记录其物理瓦片类型。本讲的 RR 图就「画」在这块网格之上——节点都有 `(xlow,ylow,xhigh,yhigh)` 坐标。
- **架构驱动**（u2-l1/u2-l2）：线段（segment）、开关（switch）、瓦片引脚都来自运行时解析的架构 XML。RR 图是这些架构元素在「某个通道宽度下」的实例化展开。
- **`g_vpr_ctx` 与 `DeviceContext`**（u3-l4）：VPR 全局状态总线 `g_vpr_ctx` 下挂着若干子上下文。本讲会指出一个容易被忽视的事实：RR 图其实住在 `DeviceContext`，而不是 `RoutingContext`。
- **`vpr_api` 编排与 `e_stage_action`**（u3-l5）：RR 图由主流程在布线阶段调用 `vpr_create_rr_graph` 创建，它读取上一阶段的器件网格与架构配置。

一句话直觉：**DeviceGrid 告诉 VPR「地上有哪些地块」，RR Graph 告诉 VPR「信号能在哪些地块之间、沿着哪些导线和开关走」。** 布局把逻辑块摆到地块上，布线则在 RR 图上为每个信号找路。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `libs/librrgraph/src/base/rr_node_types.h` | 定义 RR 节点类型枚举 `e_rr_type`、方向 `Direction`、RC 享元 `t_rr_rc_data`、空间查找表 `t_rr_node_indices`。本讲「节点」的语义权威。 |
| `libs/librrgraph/src/base/rr_node.h` | 节点代理类 `t_rr_node`（旧式访问）与代价索引表 `t_rr_indexed_data`。 |
| `libs/librrgraph/src/base/rr_graph_view.h` | **只读视图 `RRGraphView`**：客户端（布线器、布局器、时序、GUI）访问 RR 图的唯一接口。本讲核心。 |
| `libs/librrgraph/src/base/rr_spatial_lookup.h` | `RRSpatialLookup`：按 `(type,layer,x,y,side,ptc)` 反查节点 ID 的快速查找结构。 |
| `libs/librrgraph/src/base/rr_graph_storage.h` | `t_rr_graph_storage`：节点/边的真实存储，`RRGraphView` 与 `RRGraphBuilder` 共用的底层容器。 |
| `libs/librrgraph/src/base/rr_graph_type.h` | 通道宽度容器 `t_chan_width`、布线类型 `e_route_type`、图类型 `e_graph_type`。 |
| `vpr/src/route/rr_graph_generation/rr_graph.h` | 对外入口 `create_rr_graph` 的声明。 |
| `vpr/src/route/rr_graph_generation/rr_graph.cpp` | `create_rr_graph` 的实现，以及内部 `build_rr_graph`（非 tileable）与 `build_tileable_unidir_rr_graph` 两条建图分支。 |
| `vpr/src/base/vpr_api.cpp` | `vpr_create_rr_graph`：把命令行通道宽度翻译成 `t_chan_width`、决定图类型，并调用 `create_rr_graph`。 |
| `vpr/src/base/vpr_context.h` | `DeviceContext` 持有 `rr_graph_builder`（可写）与 `rr_graph`（只读视图）两个成员。 |
| `vpr/src/base/place_and_route.cpp` | `init_chan`：把标量通道宽度因子展开成逐通道的宽度分布。 |

> 注意命名陷阱：本讲的练习会让你去找「RR 节点的所有类型」。它**不在** `rr_types.h`（那个文件存的是线段细节 `t_seg_details`、引脚-线道查找表等），而在 `rr_node_types.h` 里的 `e_rr_type`。这是真实代码组织的反映，本讲会沿用正确路径。

## 4. 核心概念与源码讲解

### 4.1 RR 图节点与边

#### 4.1.1 概念说明

RR 图是一张有向图 \( G=(V,E) \)：

- **节点 \( v \)** 表示一个**路由资源**——信号可以经过的一个具体可编程点，包括逻辑块的虚拟源/汇、I/O 引脚、以及布线轨道（routing track，即一段导线）。
- **边 \( e=(u \to v) \)** 表示资源之间的**可编程连接**，本质是一个**开关**（switch）：多路选择器、三态缓冲、传输门、短路（金属直连）或普通缓冲。

布线器在 RR 图上为每个线网（net）从 `SOURCE` 出发找一条到各 `SINK` 的路径，路径上经过的每条边就是最终要打开的开关。因此 RR 图**就是把 FPGA 的可编程布线结构完整地、一次性地标量化成了一张搜索图**——架构 XML 描述「类型」，DeviceGrid 提供「位置」，RR 图给出「具体有哪些节点、谁连谁」。

#### 4.1.2 核心流程

把 FPGA 抽象成 RR 图的过程可以概括为「先摆节点，再连边」：

1. **建块级节点**：为每个网格瓦片的逻辑引脚等价类创建 `SOURCE/SINK`，为每个物理引脚创建 `IPIN/OPIN`。
2. **建轨道节点**：按通道宽度 `W`，在每个水平/竖直通道位置铺设 `W` 条（可能再按线段长度分段）`CHANX/CHANY` 轨道节点；多层器件还会有 `CHANZ` 连接不同层。
3. **连边（三类开关盒/连接盒逻辑）**：
   - OPIN → CHAN（输出引脚驱动轨道，连接盒 CB）；
   - CHAN ↔ CHAN（轨道之间在开关盒 SB 里互连）；
   - CHAN → IPIN（轨道喂给输入引脚，CB）；
   - SOURCE → OPIN 与 IPIN → SINK（块内无延迟直连）。
4. **打属性**：每个节点登记类型、坐标、方向、`ptc_num`、RC 索引、代价索引；每条边登记所用开关 ID。

> 共有边 vs 非共有边：能被配置位控制的开关（mux/tristate/pass-gate）产生的边叫 **configurable edge**，硬连线（short / 不可关闭 buffer）产生的叫 **non-configurable edge**。这个区分对时序图构建（u7）至关重要，因为非可配置连接会被「折叠」成同一时序路径。

#### 4.1.3 源码精读

RR 节点的全部类型由枚举 `e_rr_type` 定义，每个取值都有清晰的中文语义：

[libs/librrgraph/src/base/rr_node_types.h:26-38](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/librrgraph/src/base/rr_node_types.h#L26-L38) —— 定义 `SOURCE/SINK/IPIN/OPIN/CHANX/CHANY/CHANZ/MUX` 八类节点。其中 `SOURCE` 是「块内某信号的逻辑输出端」、`SINK` 是「逻辑输入端」，二者都是**虚拟节点**，用来给布线提供统一的起止点；`CHANX/CHANY` 是真正的布线导线段；`CHANZ` 用于 3D 多层器件跨层互连；`MUX` 描述不跨显著距离的布线多路选择器节点。

紧随其后是一组**编译期判定函数**，把节点按用途归类，是全代码库里到处复用的「判断是哪类资源」的便捷谓词：

[libs/librrgraph/src/base/rr_node_types.h:40-43](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/librrgraph/src/base/rr_node_types.h#L40-L43) —— `is_pin`（IPIN/OPIN）、`is_chanxy`（CHANX/CHANY）、`is_chanz`、`is_src_sink` 四个 `constexpr` 函数。注意它们是编译期函数，零运行时开销，这与 u3-l1 讲过的 `StrongId` 类型安全哲学一脉相承。

节点方向（仅对导线有意义）由 `Direction` 枚举刻画，决定布线器沿导线扩展的合法方向：

[libs/librrgraph/src/base/rr_node_types.h:87-93](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/librrgraph/src/base/rr_node_types.h#L87-L93) —— `INC`（驱动在低坐标端）、`DEC`（驱动在高坐标端）、`BIDIR`（双向，多驱动）、`NONE`（无方向，如 IPIN/OPIN）。单向布线架构（UNIDIR）大量使用 INC/DEC，这是它区别于双向架构（BIDIR）的本质。

边的「开关类型」并不存在节点里，而是通过**享元（flyweight）**存储——整张图可能有上百万条边，但只有少数几种开关：

[libs/librrgraph/src/base/rr_graph_view.h:686-699](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/librrgraph/src/base/rr_graph_view.h#L686-L699) —— 注释明确要求：所有开关的 R/C/类型只能放在 `rr_switch_inf` 表里，边只存开关 ID。RC 数据同样享元化在 `rr_rc_data`，每个节点只持有一个 `rc_index` 指向具体 R/C 值：

[libs/librrgraph/src/base/rr_node_types.h:163-168](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec62818c9db312e24967de9d6283410ab19/libs/librrgraph/src/base/rr_node_types.h#L163-L168) —— `t_rr_rc_data`，`R` 是端到端金属电阻、`C` 是含金属电容与所挂开关输入/输出电容的总电容。享元让百万节点表的内存占用可控。

每个节点的代价相关聚合属性（基础代价、线性/二次延迟系数、归一化长度等）也存在单独的索引表里：

[libs/librrgraph/src/base/rr_node.h:135-144](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/librrgraph/src/base/rr_node.h#L135-L144) —— `t_rr_indexed_data`，节点通过 `cost_index` 指向它。`base_cost` 是布线该节点的基础代价，`T_linear/T_quadratic` 用来预测「再走 N 段还剩多少延迟」，这正是 u6-l4 路由前瞻会用到的量。注释对每个字段都有解释，值得逐行读。

#### 4.1.4 代码实践

**实践目标**：亲手列出 RR 节点的全部类型并确认「类型→字符串」映射，纠正「在 `rr_types.h` 找节点类型」的命名误解。

**操作步骤**：

1. 打开 `libs/librrgraph/src/base/rr_node_types.h`，定位 `enum class e_rr_type`，写下每个取值及其注释含义。
2. 找到紧邻的 `RR_TYPES` 数组与 `rr_node_typename` 数组，确认它们与枚举一一对应（一个用于遍历、一个用于日志打印）。
3. 再打开 `vpr/src/route/rr_graph_generation/rr_types.h`，确认这里**没有**节点类型枚举，只有 `t_seg_details`、`t_pin_to_track_lookup` 等「线段细节」结构。

**需要观察的现象**：`rr_types.h` 与 `rr_node_types.h` 名字相近但内容完全不同；节点类型权威在后者。`e_rr_type` 的取值数等于 `RR_TYPES.size()`，也等于 `rr_node_typename` 的大小。

**预期结果**：得到一张 8 行的「节点类型 → 中文含义」表（SOURCE/SINK/IPIN/OPIN/CHANX/CHANY/CHANZ/MUX），并理解为何 `get_rr_type("...")` 能在解析外部 RR 图文件时把字符串还原成枚举。

> 若本地未构建，无法直接运行；以上为源码阅读型实践，不依赖运行二进制。

#### 4.1.5 小练习与答案

**练习 1**：`SOURCE` 和 `SINK` 为什么叫「虚拟节点」？布线器从哪个开始、到哪个结束？

**参考答案**：它们不对应物理导线，而是逻辑块内信号的发生/消费端点，给布线提供统一的起止接口。一个线网的布线起点是驱动该信号的 `SOURCE`，终点是该信号每个接收端的 `SINK`。

**练习 2**：`is_pin(x)` 为真、`is_chanxy(x)` 为假时，`x` 可能是哪些类型？

**参考答案**：`is_pin` 为真意味着 `x` 是 `IPIN` 或 `OPIN`，二者本身就 `is_chanxy` 为假，所以答案就是 `IPIN` 或 `OPIN`。

---

### 4.2 只读视图 rr_graph_view

#### 4.2.1 概念说明

RR 图一旦建好就**不再被算法改写**（布线只是「使用」图、把路径选择记在别处，并不增删节点/边）。VPR 据此把 RR 图分成两层：

- **`RRGraphBuilder`（可写）**：建图阶段用它添加节点、连边、登记属性，是 `create_rr_graph` 的唯一数据库。
- **`RRGraphView`（只读）**：布线器、布局器、时序分析器、GUI 等所有客户端访问图的唯一接口。它**不拥有存储**，只是把构建期产出的若干只读子结构（节点存储、空间查找表、享元 RC/开关/线段表、元数据）「框」在一起。

这个设计的收益有三：① 防止客户端误改图（const 正确性）；② 让存储格式可以独立演进（视图只是协议）；③ 为将来给不同客户端提供更紧凑的「迷你视图」留出空间。注意 `RRGraphView` 删除了拷贝构造与赋值——因为整张图可达 GB 级，按值传递是灾难。

#### 4.2.2 核心流程

客户端使用 `RRGraphView` 的典型三件事：

1. **遍历节点**：用 `nodes()` 拿到全体 `RRNodeId` 的范围，for-range 遍历。
2. **查询节点属性**：`node_type()`、`node_xlow()/node_xhigh()/node_ylow()/node_yhigh()`、`node_direction()`、`node_capacity()`、`node_R()/node_C()`、`node_ptc_num()`（及特化的 `node_pin_num/node_track_num/node_class_num`）。
3. **遍历/查询边**：用 `edge_range(node)` 或 `edges(node)` 遍历某节点的出边，用 `edge_sink_node(node, iedge)` 与 `edge_switch(node, iedge)` 取每条边的目标与开关；用 `configurable_edges(node)` / `non_configurable_edges(node)` 拿到按可配置性分好序的两段。

反向（已知位置查节点）由 `node_lookup()` 返回的 `RRSpatialLookup` 承担：`find_node(layer, x, y, type, ptc, side)`。这套「正向按节点查属性 / 反向按坐标查节点」的双向访问，是布线器高效搜索的基础。

#### 4.2.3 源码精读

`RRGraphView` 的构造函数接受一堆 const 引用，印证了「不拥有存储，只是框视图」：

[libs/librrgraph/src/base/rr_graph_view.h:77-88](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/librrgraph/src/base/rr_graph_view.h#L77-L88) —— 构造参数包括节点存储 `t_rr_graph_storage`、空间查找表 `RRSpatialLookup`、节点/边元数据、代价索引表 `rr_indexed_data`、RC 享元 `rr_rc_data`、线段表 `rr_segments`、开关表 `rr_switch_inf`，全部是 const 引用。

随后用 `= delete` 禁止拷贝：

[libs/librrgraph/src/base/rr_graph_view.h:96-97](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/librrgraph/src/base/rr_graph_view.h#L96-L97) —— 删除拷贝构造与赋值，编译期阻止「巨型对象按值传递」。

遍历节点与计数是最高频操作：

[libs/librrgraph/src/base/rr_graph_view.h:118-126](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/librrgraph/src/base/rr_graph_view.h#L118-L126) —— `nodes()` 返回 `[0, num_nodes())` 的 `RRNodeId` 范围；`num_nodes()` 转发给底层存储的 `size()`。客户端惯用写法是 `for (const RRNodeId& node : rr_graph.nodes()) { ... }`。

`node_type` 是查询的入口，同样直接转发存储：

[libs/librrgraph/src/base/rr_graph_view.h:141-143](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/librrgraph/src/base/rr_graph_view.h#L141-L143) —— `node_type()` 调用 `node_storage_.node_type(node)`。存储侧的实现就是直接取结构体字段：

[libs/librrgraph/src/base/rr_graph_storage.h:172-174](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/librrgraph/src/base/rr_graph_storage.h#L172-L174) —— `return node_storage_[id].type_;`。这正是 u3-l1 讲过的「以 `StrongId` 为下标的 `vtr_vector_map`」式存储在 RR 图上的体现。

遍历某节点出边并取目标节点与开关：

[libs/librrgraph/src/base/rr_graph_view.h:135-137](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/librrgraph/src/base/rr_graph_view.h#L135-L137) —— `edge_range()` 返回该节点出边 ID 区间。

[libs/librrgraph/src/base/rr_graph_view.h:476-499](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/librrgraph/src/base/rr_graph_view.h#L476-L499) —— `edge_switch(id, iedge)` 取该边用的开关、`edge_sink_node(id, iedge)` 取目标节点。注意它们返回的 `switch` 是个 `short`（开关表的下标），目标节点是 `RRNodeId`。

按「可配置 / 非可配置」把出边切成两段，是布线器和时序分析都要用的关键 API：

[libs/librrgraph/src/base/rr_graph_view.h:556-574](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/librrgraph/src/base/rr_graph_view.h#L556-L574) —— `configurable_edges(node)` 返回前 `num_configurable_edges` 条、`non_configurable_edges(node)` 返回剩余部分。该顺序由建图末尾的 `partition_edges` 保证，因此布线器可安全假设「前面都是可配置开关」。

反向定位节点靠 `node_lookup()` 返回的空间查找表：

[libs/librrgraph/src/base/rr_graph_view.h:715-721](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/librrgraph/src/base/rr_graph_view.h#L715-L721) —— `node_lookup()` 返回 `RRSpatialLookup`，`rr_nodes()` 返回底层存储。

[libs/librrgraph/src/base/rr_spatial_lookup.h:70-75](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec62818c9db312e24967de9d6283410ab19/libs/librrgraph/src/base/rr_spatial_lookup.h#L70-L75) —— `find_node(layer, x, y, type, ptc, side)`。其底层正是 [libs/librrgraph/src/base/rr_node_types.h:170-172](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/librrgraph/src/base/rr_node_types.h#L170-L172) 定义的 `t_rr_node_indices`：一个 `[type][layer][x][y][side][ptc]` 的多维向量表，使「给我这个位置上第 k 条 CHANX 轨道」成为 O(1) 查找。

#### 4.2.4 代码实践

**实践目标**：学会像布线器那样「正向遍历 + 反向查找」RR 图。

**操作步骤**：

1. 在 `rr_graph_view.h` 中找到 `nodes()`、`node_type()`、`edge_range()`、`edge_sink_node()`、`edge_switch()`，抄写一段标准遍历模式（见下方示例代码）。
2. 找到 `configurable_edges()` 与 `non_configurable_edges()`，确认二者拼接起来等于全部出边。
3. 通过 `node_lookup()` → `find_node(...)`，理解「已知 `(x,y,type,ptc)` 反查节点」的用法。

**示例代码**（非项目原代码，标注为示例代码）：

```cpp
// 示例代码：遍历一个节点的可配置出边，打印目标节点类型与所用开关 ID
const RRGraphView& rr_graph = g_vpr_ctx.device().rr_graph;  // 只读视图
for (RRNodeId src : rr_graph.nodes()) {                     // 遍历所有节点
    for (t_edge_size ie : rr_graph.configurable_edges(src)) {  // 仅可配置出边
        RRNodeId sink = rr_graph.edge_sink_node(src, ie);
        short sw_id   = rr_graph.edge_switch(src, ie);
        // ... 布线器据此评估扩展代价
    }
}
```

**需要观察的现象**：视图的全部访问器都是 `inline` 转发，没有内部状态变更；`configurable_edges` 与 `non_configurable_edges` 的长度之和等于 `num_edges`。

**预期结果**：你能凭空写出「取某 CHANX 节点全部下游可配置目标」的代码片段，并说出每条边为何只存一个 `short` 开关 ID（享元）。

> 运行型验证待本地构建后进行；本步为源码阅读型实践。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `RRGraphView` 把拷贝构造删除？如果允许拷贝会有什么后果？

**参考答案**：RR 图可达 GB 级，按值拷贝会瞬间翻倍内存并触发巨大开销；更危险的是多份拷贝会导致「状态分裂」，客户端看到不一致的图。删除拷贝在编译期杜绝这两类隐患（与 u3-4 的 `Context` 不可拷贝基类同理）。

**练习 2**：已知一个 `RRNodeId`，如何得到「它的出边里有多少条是可配置的」？

**参考答案**：调用 `rr_graph.num_configurable_edges(node)`，或等价地用 `configurable_edges(node)` 的区间长度。该计数在底层通过开关的 `configurable()` 属性结合 `partition_edges` 的排序得到。

---

### 4.3 通道宽度与建图

#### 4.3.1 概念说明

RR 图不是「架构确定就唯一确定」的——它还依赖一个**通道宽度（channel width）`W`**：每个通道里铺多少条轨道。同一架构、不同 `W`，建出的 RR 图规模差异巨大。这正是 VPR 能做「最小可布线通道宽度二分搜索」（见 u1-l5 的 `--route_chan_width` 默认 `-1`）的前提：每次换一个 `W` 就重建一次 RR 图，再跑布线看是否成功。

`W` 影响图的规模是近乎线性的：每个通道位置的轨道数 ∝ `W`，所以轨道节点总数大致与 `W × 通道位置数` 成正比：

\[
N_{\text{track}} \;\propto\; W \times \big((W_{\text{grid}}-1)\cdot H_{\text{grid}} + (H_{\text{grid}}-1)\cdot W_{\text{grid}}\big)
\]

其中右侧括号是水平与竖直通道位置数（约为网格面积量级）。边数同样随 `W` 放大，因为每个新轨道都要在连接盒/开关盒里接上引脚和其他轨道。这就是为什么增大 `W` 会让 RR 图的构建耗时与布线耗时都显著上升。

#### 4.3.2 核心流程

把命令行的通道宽度变成一张 RR 图，主链路是：

```
命令行 --route_chan_width (int, -1=自动搜索)
   │
   ▼
vpr_route_flow ── vpr_create_rr_graph(chan_width_fac)        # vpr_api.cpp
   │   ├─ 据 route_type/directionality 决定 e_graph_type     # GLOBAL/BIDIR/UNIDIR/UNIDIR_TILEABLE
   │   ├─ init_chan(cfactor, ...) 把标量因子展开成 t_chan_width  # 逐通道宽度分布
   │   └─ create_rr_graph(...)                                # rr_graph.h/cpp
   ▼
create_rr_graph
   ├─ 若 chan_width 未变且图非空 → 直接 return（跳过重建）
   ├─ 否则 free_rr_graph() 后建新图：
   │     └─ build_rr_graph(...nodes_per_chan...)              # 非 tileable
   │        或 build_tileable_unidir_rr_graph(...)            # tileable
   ▼
DeviceContext.rr_graph (RRGraphView) 就绪，供布线器读取
```

三个关键决策点：

1. **图类型 `e_graph_type`**：全局布线（`GLOBAL`） vs 详细布线（`BIDIR`/`UNIDIR`/`UNIDIR_TILEABLE`），由 `route_type` 与架构 `directionality` 决定。
2. **通道宽度分布**：架构文件可指定通道宽度沿位置的分布（中间宽、边缘窄等），`init_chan` 把一个标量 `cfactor` 与该分布结合，算出每个具体通道的实际轨道数。
3. **重建缓存**：若本次 `nodes_per_chan` 与上次相同，且非扁平布线需要补簇内资源，则**跳过重建**——这是多次布线迭代时的关键加速。

#### 4.3.3 源码精读

入口 `vpr_create_rr_graph` 把命令行因子翻译成建图调用：

[vpr/src/base/vpr_api.cpp:1272-1311](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1272-L1311) —— 先决定 `graph_type` 与 `graph_directionality`，再调用 `init_chan` 得到 `t_chan_width`，`free_rr_graph()` 清理旧图，最后 `create_rr_graph(...)`。注意它取的是 `g_vpr_ctx.mutable_device()`：**RR 图是 `DeviceContext` 的成员，而非 `RoutingContext`**——因为它刻画的是「器件在给定通道宽度下的可布线结构」，本质是器件属性。

图类型的判定逻辑在开头几行：

[vpr/src/base/vpr_api.cpp:1279-1290](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1279-L1290) —— `GLOBAL` 路由强制 `BIDIR`；否则按架构 `directionality` 选 `BIDIR`/`UNIDIR`，单向且 `tileable` 时升级为 `UNIDIR_TILEABLE`。`init_chan(chan_width_fac, arch.Chans, graph_directionality)` 把标量因子展开成逐通道宽度。

通道宽度容器 `t_chan_width` 既存最大值，也存每行/每列的具体宽度列表：

[libs/librrgraph/src/base/rr_graph_type.h:11-32](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/librrgraph/src/base/rr_graph_type.h#L11-L32) —— `max/x_max/y_max/x_min/y_min` 与逐行 `x_list`（长度为 `grid.height()`）、逐列 `y_list`（长度为 `grid.width()`）。注释强调：建图完成后，每通道实际轨道数会从 RR 图中反提取并存进 `DeviceContext`。

`init_chan` 的实现展示了「标量因子 → 逐通道分布」的归一化算法：

[vpr/src/base/place_and_route.cpp:452-496](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/place_and_route.cpp#L452-L496) —— 对每一行/列，用 `compute_chan_width(cfactor, dist, normalized_pos, separation, directionality)` 结合架构的通道宽度分布 `dist` 算出该通道轨道数，并强制下限为 1。也就是说，`W`（`cfactor`）是一个**全局缩放因子**，最终各通道宽度还要乘以架构给定的形状分布。

`create_rr_graph` 的实现核心是「缓存判定 + 分派建图」：

[vpr/src/route/rr_graph_generation/rr_graph.cpp:326-336](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/rr_graph_generation/rr_graph.cpp#L326-L336) —— 函数签名，接收 `t_chan_width nodes_per_chan`（建图的关键输入之一）。

[vpr/src/route/rr_graph_generation/rr_graph.cpp:343-350](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/rr_graph_generation/rr_graph.cpp#L343-L350) —— **重建缓存**：`if (device_ctx.chan_width == nodes_per_chan && !device_ctx.rr_graph.empty())` 则直接 `return`，跳过重建。这是最小通道宽度二分搜索里每轮换 `W` 时只有「真正变化的轮」才重建的原因。

[vpr/src/route/rr_graph_generation/rr_graph.cpp:384-407](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/rr_graph_generation/rr_graph.cpp#L384-L407) —— 两条建图分支：非 tileable 走 `build_rr_graph(...)`，tileable 走 `build_tileable_unidir_rr_graph(...)`。注意两者都把 `nodes_per_chan`（通道宽度）作为核心参数传入。

真正「按通道宽度铺轨道」的代码在 `build_rr_graph` 开头：

[vpr/src/route/rr_graph_generation/rr_graph.cpp:595-652](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/rr_graph_generation/rr_graph.cpp#L595-L652) —— `max_chan_width = nodes_per_chan.max`（全局布线时强制为 1）；随后 `alloc_and_load_seg_details(&max_chan_width_x, max_dim, segment_inf_x, ...)` 会按各线段类型把 `max_chan_width` 条轨道分配给不同 segment，必要时（`use_full_seg_groups`）还会**回写调整** `max_chan_width`，这就是警告位 `RR_GRAPH_WARN_CHAN_X_WIDTH_CHANGED` 的由来：实际轨道数可能与请求的不完全一致。

建好之后，视图与构建器在 `DeviceContext` 里并肩存在：

[vpr/src/base/vpr_context.h:235-247](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L235-L247) —— `RRGraphBuilder rr_graph_builder`（建图专用可写库）与 `RRGraphView rr_graph{...}`（用构建器的存储等子结构构造的只读视图）。布线器读 `g_vpr_ctx.device().rr_graph`，建图代码写 `rr_graph_builder`。

#### 4.3.4 代码实践

**实践目标**：跟踪通道宽度从 CLI 到 RR 图规模的完整链路，并解释「为何 `W` 翻倍图也近线性放大」。

**操作步骤**：

1. 在 `vpr_api.cpp` 找到 `vpr_create_rr_graph`（约 1272 行），记录它如何由 `router_opts.route_type` 与 `det_routing_arch.directionality` 推出 `e_graph_type`。
2. 跟进 `init_chan`（`place_and_route.cpp:452`），看清 `cfactor` 如何与架构分布结合成逐通道 `x_list/y_list`。
3. 在 `rr_graph.cpp` 的 `create_rr_graph`（326 行）里定位「跳过重建」判断（343 行）与 `build_rr_graph` 分支（386 行）。
4. 在 `build_rr_graph`（558 行）里找到 `max_chan_width` 的来源（595 行）与 `alloc_and_load_seg_details` 调用（637 行），理解「按 segment 分配轨道」如何决定 CHANX/CHANY 节点数量。

**需要观察的现象**：

- `init_chan` 输出一个标量 `cfactor`（即命令行的 `--route_chan_width`），输出一张逐通道宽度表 `t_chan_width`。
- `create_rr_graph` 在 `chan_width` 没变时直接 `return`，不再重建。
- `build_rr_graph` 中 `max_chan_width` 直接进入 `alloc_and_load_seg_details`，后者决定每个通道位置铺多少轨道节点。

**预期结果**：你能画出一张数据流图：`CLI W → cfactor → init_chan → t_chan_width → build_rr_graph → alloc_and_load_seg_details → CHANX/CHANY 节点数 ∝ W`。并用一句话回答「通道宽度如何影响图的规模」：**通道宽度 `W` 是每通道的轨道数，轨道节点与相关连接盒/开关盒边都随 `W` 近线性增长，故 `W` 翻倍则图规模近线性放大、构建与布线耗时随之上升**。

> 若本地已构建，可尝试对同一电路用 `--route_chan_width 32` 与 `64` 各跑一次 VPR，观察日志中 `Build routing resource graph` 计时与 `num_rr_nodes` 的差异（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：最小通道宽度二分搜索每轮都会重建 RR 图吗？为什么？

**参考答案**：不一定。`create_rr_graph` 开头有缓存判定：只有当本次 `nodes_per_chan` 与 `DeviceContext.chan_width` 不同（或需要补簇内资源）时才重建；若某轮搜索得到与上一轮相同的 `W`，则直接复用旧图。

**练习 2**：为什么 RR 图存在 `DeviceContext` 而不是 `RoutingContext`？

**参考答案**：RR 图刻画的是「器件在给定通道宽度下的可布线物理结构」，本质由器件网格、架构线段/开关与通道宽度共同决定，属于器件属性而非某次布线的临时状态；布线结果（哪些边被选中）才属于 `RoutingContext`。把它放在 `DeviceContext` 也方便布局、时序、GUI 等非布线客户端共享。

**练习 3**：`alloc_and_load_seg_details` 可能「回写」改变 `max_chan_width` 并触发一个警告，这通常发生在什么场景？

**参考答案**：当 `use_full_seg_groups` 为真（即 tileable 单向图 `UNIDIR_TILEABLE`）时，为了让轨道数能被线段长度整除、形成完整的线段组，实际通道宽度会被向上调整到最近的合法值，导致它与请求值不符，于是设置 `RR_GRAPH_WARN_CHAN_X_WIDTH_CHANGED`（或 Y）警告位。

## 5. 综合实践

**任务：绘制「通道宽度 → RR 图」全链路说明图，并量化一次规模变化。**

把本讲三个模块串起来：

1. **节点类型速查表**：从 `rr_node_types.h` 抄出 `e_rr_type` 的 8 个取值，补上中文含义、是否对应物理导线、`ptc_num` 在该类型下的语义（参考 `RRGraphView::node_ptc_num` 的注释）。
2. **访问双视图**：用一段示例代码演示「正向 `for (node : rr_graph.nodes())` + 反向 `rr_graph.node_lookup().find_node(...)`」，并解释 `RRGraphView` 为何删除拷贝。
3. **建图链路图**：画出 `vpr_create_rr_graph → init_chan → create_rr_graph → build_rr_graph → alloc_and_load_seg_details` 的调用顺序，标注每一步的输入输出，并高亮「chan_width 未变则跳过」这一缓存分支。
4. **量化（选做，待本地验证）**：用一个示例架构，分别以 `--route_chan_width 30` 和 `60` 跑 VPR，记录两次的 `num_rr_nodes`、`num_rr_edges` 与 `Build routing resource graph` 耗时，验证节点/边数是否近似线性翻倍。

完成此实践后，你应当能向他人讲清：「VPR 的布线器搜索的不是 FPGA 本身，而是它一次性预建好的 RR 图；这张图的规模由架构和通道宽度共同决定，通道宽度不变就不必重建。」

## 6. 本讲小结

- RR 图把 FPGA 的可编程布线结构标量化成有向图：**节点 = 路由资源**（`SOURCE/SINK/IPIN/OPIN/CHANX/CHANY/CHANZ/MUX`），**边 = 开关**（mux/tristate/pass-gate/short/buffer），节点类型权威在 `rr_node_types.h` 的 `e_rr_type`，而非名字相近的 `rr_types.h`。
- 开关、RC、代价等重复量一律**享元**存储（`rr_switch_inf` / `rr_rc_data` / `t_rr_indexed_data`），边只存开关 ID，节点只存 RC/cost 索引——这是百万节点图内存可控的关键。
- RR 图分两层：`RRGraphBuilder`（建图期可写）与 `RRGraphView`（运行期只读、不可拷贝）。客户端一律用 `RRGraphView` 的 `nodes()/edge_range()/edge_sink_node()/node_lookup()` 等 inline 转发访问器。
- 通道宽度 `W` 是每通道轨道数，是 RR 图规模的**主旋钮**：轨道节点与相关边随 `W` 近线性增长；同一架构换 `W` 就要重建图。
- 建图主链路为 `vpr_create_rr_graph → init_chan → create_rr_graph → build_rr_graph`，其中 `create_rr_graph` 在「通道宽度未变」时直接 `return` 跳过重建，这是最小通道宽度二分搜索的加速点。
- RR 图住在 `DeviceContext`（`rr_graph` 只读视图 + `rr_graph_builder` 可写构建器），因为它本质是「器件在给定通道宽度下的可布线结构」，被布线、布局、时序、GUI 共享。

## 7. 下一步学习建议

掌握了 RR 图之后，建议按数据流继续：

- **u6-l2 连接路由器与堆结构**：布线器如何在 RR 图上跑 Dijkstra/maze 搜索，并用堆维护待扩展节点——你会看到本讲的 `edge_sink_node/edge_switch`、`node_R/node_C`、`t_rr_indexed_data` 的延迟系数如何被代价函数实时调用。
- **u6-l3 基于连接的路由与路由树**：迭代布线如何反复使用同一张 RR 图化解拥塞，以及路由树如何复用已布线段。
- **u6-l4 路由器前瞻 Router Lookahead**：`t_rr_indexed_data` 里的 `T_linear/T_quadratic` 如何被预计算成「到目标还剩多少代价」的前瞻地图，引导搜索定向扩展。

若想加深对「图本身」的理解，可继续精读 `libs/librrgraph/src/base/rr_graph_storage.h`（节点/边的紧凑存储布局）与 `vpr/src/route/rr_graph_generation/` 下各 `rr_graph_*.cpp`（开关盒、连接盒边如何被逐类生成）。
