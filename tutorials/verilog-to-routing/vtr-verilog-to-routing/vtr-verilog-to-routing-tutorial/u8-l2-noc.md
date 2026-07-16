# NoC 布局与路由

## 1. 本讲目标

本讲讲解 VPR 对片上网络（Network-on-Chip，NoC）的建模、流量描述与路由算法支持。读完本讲后，你应当能够：

- 理解 NoC 在 FPGA 中「物理路由器 / 逻辑路由器」两层建模的含义，以及它与普通布线资源图（RR Graph，见 u6-l1）的根本区别。
- 读懂 `NocStorage` 如何把 NoC 拓扑存成一张「路由器 = 节点、链路 = 有向边」的有向图。
- 读懂 `NocTrafficFlows` 如何描述「哪些逻辑路由器之间要通信、带宽与延迟约束是什么」，并理解它如何驱动布局阶段的增量重路由与代价计算。
- 说出 `noc` 目录下至少 6 种 NoC 路由算法、它们的分类（BFS vs 回转模型 Turn Model vs SAT）、以及选择入口 `--noc_routing_algorithm` 的默认值与可选值。
- 理解 NoC 路由结果（一组 `NocLinkId`）如何反过来影响模拟退火布局的代价（u5-l1），从而把「布局」和「NoC 路由」耦合在一起迭代。

## 2. 前置知识

在进入 NoC 之前，请先建立以下直觉（本手册前序讲义已铺垫）：

- **架构驱动（u2 系列）**：VPR 的算法不硬编码任何架构假设。NoC 拓扑（有哪些路由器、它们怎么连）来自**架构 XML**（被解析为 `t_noc_inf`），而非写死在代码里。
- **全局上下文 `g_vpr_ctx`（u3-4）**：VPR 所有阶段共享状态都挂在 `VprContext` 上，按主题切成多个子上下文。NoC 也有自己的子上下文 `NocContext`，里面同时住着拓扑模型、流量描述和路由器对象。
- **ClusteredNetlist（u3-3）**：打包后的聚簇网表，块用 `ClusterBlockId` 标识。NoC 的「逻辑路由器」就是网表里的一类块。
- **模拟退火布局（u5-1）**：布局通过反复试探性移动块、评估代价增量 ΔC 来最小化代价函数。NoC 的代价项是其中一组。

**什么是 NoC？** 现代 FPGA（如 Intel/AMD 的高端器件）在可编程逻辑之外，还会集成一个**硬核**的片上网络：一组固定位置的路由器（router）通过固定连线（link）互连，用于高带宽、低延迟地把片上数据从一个位置搬运到另一个位置。VPR 需要建模这种结构：用户的电路里有「路由器模块」（逻辑路由器），它们必须被放置到芯片上固定的「物理路由器瓦片」上，并且它们之间的通信要沿着 NoC 拓扑找到一条合法路径。

> **关键区分（本讲反复出现）**：
> - **物理路由器（hard / physical router）**：芯片上固定的 NoC 路由器瓦片，位置不可变，由 `NocStorage` 里的 `NocRouter` 描述。
> - **逻辑路由器（logical router）**：用户电路里的路由器模块实例，是 `ClusteredNetlist` 里的一个块（`ClusterBlockId`），布局阶段会被搬到某个物理路由器瓦片上。

NoC 的「路由」和 u6 讲的器件级布线是**两套独立**的图与算法，不要混淆——本讲末尾会专门对比。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 作用 |
| --- | --- |
| `vpr/src/noc/noc_data_types.h` | 定义 NoC 专用的类型安全 ID：`NocRouterId`、`NocLinkId`、`NocTrafficFlowId`、`NocGroupId`。 |
| `vpr/src/noc/noc_storage.h/.cpp` | `NocStorage`：NoC 拓扑模型，存所有路由器与链路及其邻接关系。 |
| `vpr/src/noc/noc_router.h` | `NocRouter`：单个物理路由器（用户 ID、网格位置、层、延迟、当前占位的逻辑块）。 |
| `vpr/src/noc/noc_link.h` | `NocLink`：两个路由器之间的有向连接（源、汇、带宽、延迟）。 |
| `vpr/src/noc/noc_traffic_flows.h/.cpp` | `NocTrafficFlows`：所有流量（通信）描述，及其到逻辑路由器块的反查索引。 |
| `vpr/src/noc/noc_routing.h` | `NocRouting`：所有 NoC 路由算法的抽象基类（接口）。 |
| `vpr/src/noc/noc_routing_algorithm_creator.h/.cpp` | 工厂：根据命令行字符串创建对应路由算法对象。 |
| `vpr/src/noc/bfs_routing.h/.cpp` | `BFSRouting`：广度优先搜索路由（默认算法）。 |
| `vpr/src/noc/turn_model_routing.h/.cpp` | `TurnModelRouting`：回转模型算法族的公共基类（XY、West-First 等都继承自它）。 |
| `vpr/src/noc/xy_routing.h`、`west_first_routing.h`、`north_last_routing.h`、`negative_first_routing.h`、`odd_even_routing.h` | 五种回转模型路由算法。 |
| `vpr/src/noc/sat_routing.h/.cpp` | `noc_sat_route`：基于 SAT 求解器的路由（编译期开关 `ENABLE_NOC_SAT_ROUTING`）。 |
| `vpr/src/base/setup_noc.h/.cpp` | 从架构 XML 构建 `NocStorage`（路由器就近绑定物理瓦片 + 建链路）。 |
| `vpr/src/base/vpr_context.h` | `NocContext`：把拓扑、流量、路由器聚合为全局子上下文。 |
| `vpr/src/place/noc_place_utils.h/.cpp` | `NocCostHandler`：布局阶段调用 NoC 路由、计算/增量更新 NoC 代价。 |
| `vpr/src/base/read_options.cpp`、`vpr/src/base/vpr_types.h` | `--noc_routing_algorithm` 选项定义与 `t_noc_opts` 选项结构体。 |
| `vpr/test/test_bfs_routing.cpp` | BFS 路由的单元测试，构造 4×4 mesh，是很好的实践素材。 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**4.1 NoC 存储模型**（拓扑怎么存）、**4.2 流量描述**（谁要通信）、**4.3 NoC 路由算法**（怎么找路），最后在**4.4 布局与 NoC 路由的耦合**里把它们串起来。

### 4.1 NoC 存储模型 NocStorage

#### 4.1.1 概念说明

`NocStorage` 把 NoC 建模成一张**有向图**：

- **节点 = 路由器 `NocRouter`**：代表芯片上一个物理路由器瓦片，是进入/离开 NoC 的出入口。
- **边 = 链路 `NocLink`**：连接两个路由器的有向连接。注意链路**不是双向的**——合法的遍历方向只能从源路由器到汇路由器。两个方向各算一条独立链路。

每个路由器和链路都有一个**内部稠密 ID**（`NocRouterId` / `NocLinkId`，从 0 开始连续，用于索引容器）。但用户在架构文件里用的是另一套任意整数 ID，因此还需要一张「用户 ID ↔ 内部 ID」的转换表。

`NocStorage` 还承担一个布局期关键职责：**根据网格坐标反查物理路由器**。布局时，当一个逻辑路由器块被搬到某网格位置，VPR 只知道坐标，需要借此定位到「坐在那个坐标上的物理路由器是谁」，才能确定相关流量流的起止点并重路由（详见 [noc_storage.h:78-101](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/noc_storage.h#L78-L101) 的注释说明）。

类型安全 ID 由 `noc_data_types.h` 定义，复用了第 9 单元会讲的 `vtr::StrongId`（用幽灵 tag 在编译期区分种类，零运行时开销）：

```cpp
// vpr/src/noc/noc_data_types.h
typedef vtr::StrongId<struct noc_router_id_tag, int> NocRouterId;
typedef vtr::StrongId<struct noc_link_id_tag, int> NocLinkId;
typedef vtr::StrongId<struct noc_traffic_flow_id_tag, int> NocTrafficFlowId;
typedef vtr::StrongId<struct noc_group_id_tag, int> NocGroupId;
```

详见 [noc_data_types.h:13-23](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/noc_data_types.h#L13-L23)。

#### 4.1.2 核心流程

`NocStorage` 的生命周期分两阶段：

1. **构建期（可写）**：依次调用 `add_router(...)` 添加每个物理路由器、`add_link(...)` 添加每条有向链路。`add_link` 会把链路同时登记到源路由器的「出边表」和汇路由器的「入边表」。
2. **冻结（只读）**：调用 `finished_building_noc()` 置 `built_noc=true`，此后任何增删都会抛错。该函数还会检查是否所有链路/路由器具有相同的延迟与带宽——若不一致，则置 `detailed_*_latency` 标志，启用逐元素细粒度模型而非全局单一值。

布局期的反查链路：

```
逻辑路由器块 ClusterBlockId
   → 查 PlacementContext 得到它当前所在网格坐标 t_pl_loc
   → NocStorage::get_router_at_grid_location(loc)
   → 得到物理路由器 NocRouterId（流量流的真正起/止点）
```

其中网格坐标到路由器 ID 的映射键由 `generate_router_key_from_grid_location(x, y, layer)` 生成：`key = layer * layer_num_grid_locs + y * device_grid_width + x`（支持 2D 与多层 3D NoC，详见 [noc_storage.h:529-550](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/noc_storage.h#L529-L550)）。

#### 4.1.3 源码精读

`NocStorage` 的核心私有成员——四张表撑起整张图（节点表、边表、出边邻接表、入边邻接表）：

```cpp
// vpr/src/noc/noc_storage.h
class NocStorage {
  private:
    vtr::vector<NocRouterId, NocRouter> router_storage;              // 所有路由器（节点）
    vtr::vector<NocRouterId, std::vector<NocLinkId>> router_outgoing_links_list; // 出边邻接表
    vtr::vector<NocRouterId, std::vector<NocLinkId>> router_incoming_links_list; // 入边邻接表
    vtr::vector<NocLinkId, NocLink> link_storage;                    // 所有链路（边）
    std::unordered_map<int, NocRouterId> router_id_conversion_table; // 用户ID → 内部ID
    std::unordered_map<int, NocRouterId> grid_location_to_router_id; // 网格坐标键 → 物理路由器
    bool built_noc;                                                  // 冻结标志
    double noc_link_latency, noc_router_latency;                     // 全局延迟（粗粒度）
    bool detailed_router_latency_, detailed_link_latency_;           // 是否启用逐元素延迟
    bool multi_layer_noc_;                                           // 是否 3D NoC
    ...
};
```

详见 [noc_storage.h:44-169](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/noc_storage.h#L44-L169)。这里复用了 `vtr::vector<StrongId, T>`（u3-1、u9-1 讲过），以强类型 ID 作下标，内存紧凑、缓存友好。

构建接口 `add_router` / `add_link` 的签名——注意 `add_router` 接收的是**用户 ID + 网格位置**，内部自动换算稠密 ID 并建立两张映射表：

```cpp
// vpr/src/noc/noc_storage.h
void add_router(int id, int grid_position_x, int grid_position_y,
                int layer_position, double latency);
void add_link(NocRouterId source, NocRouterId sink, double bandwidth, double latency);
```

详见 [noc_storage.h:384-402](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/noc_storage.h#L384-L402)。

冻结函数 `finished_building_noc()` 的语义：构建完成后调用，锁死结构并判定是否启用细粒度延迟模型，见 [noc_storage.h:452-467](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/noc_storage.h#L452-L467)。

物理路由器 `NocRouter` 的字段——注意它持有 `router_block_ref`（当前坐在该瓦片上的逻辑块 ID），这是物理↔逻辑双向锚定的关键：

```cpp
// vpr/src/noc/noc_router.h
class NocRouter {
    int router_user_id;                 // 架构文件里的用户 ID（报错用）
    int router_grid_position_x, router_grid_position_y, router_layer_position; // 物理瓦片坐标
    double router_latency;              // 零负载延迟
    ClusterBlockId router_block_ref;    // 当前占位的逻辑路由器块
};
```

详见 [noc_router.h:35-57](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/noc_router.h#L35-L57)。

链路 `NocLink` 的字段——有向边，存源/汇路由器 ID、带宽容量与延迟：

```cpp
// vpr/src/noc/noc_link.h
class NocLink {
    NocLinkId id;
    NocRouterId source_router;  // 出边端
    NocRouterId sink_router;    // 入边端
    double bandwidth;           // 无拥塞下的最大带宽（bps）
    double latency;             // 零负载延迟（秒）
};
```

详见 [noc_link.h:40-49](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/noc_link.h#L40-L49)。

> **拓扑从哪来？** `NocStorage` 不是凭空出现的。它在 `setup_noc.cpp` 的 `generate_noc()` 里被填充：先 `clear_noc()`，再 `create_noc_routers(...)` 把架构里每个用户描述的路由器**就近绑定**到最近的物理路由器瓦片（用欧氏距离，逐层匹配），再 `create_noc_links(...)` 按拓扑加边，最后 `finished_building_noc()` 冻结。这再次体现「架构驱动」——拓扑完全由 `t_arch::noc`（即架构 XML 的 `<noc>` 段）决定。见 [setup_noc.cpp:110-129](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_noc.cpp#L110-L129) 与就近绑定的 [setup_noc.cpp:143-215](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_noc.cpp#L143-L215)。

#### 4.1.4 代码实践

**实践目标**：亲手用 `NocStorage` 的公开 API 在内存里搭一个最小 NoC，理解「构建 → 冻结 → 查询」三步。

**操作步骤**（源码阅读型 + 可选本地验证）：

1. 打开单元测试 [test_bfs_routing.cpp:11-64](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/test/test_bfs_routing.cpp#L11-L64)，它构造了一个 4×4 mesh NoC。
2. 跟踪它如何：先 `set_device_grid_spec(4, 0)` 设置网格宽（用于生成坐标键），再两层循环 `add_router(...)` 加 16 个路由器，`make_room_for_noc_router_link_list()` 预留邻接表，最后对每个路由器向左/上/右/下四个方向 `add_link(...)`。
3. 对照本讲 4.1.3 的成员表，在脑中画出：「这 16 次 `add_router` 填充了 `router_storage` 与 `grid_location_to_router_id`；每次 `add_link` 同时更新了 `link_storage`、源路由器的出边表、汇路由器的入边表。」

**需要观察的现象**：
- 该测试**没有**调用 `finished_building_noc()`——因为它只测路由、不需要冻结保护。但生产代码（`setup_noc.cpp:128`）一定会调用，确保运行期只读。
- mesh 中相邻两个路由器之间各有一条**单向**链路（a→b 与 b→a 是两条不同的 `NocLink`），印证「链路有向」。

**预期结果**：你能用一句话说清「`NocStorage` 用哪四张表表示一张有向图，以及 `grid_location_to_router_id` 在布局期的作用」。

**待本地验证**：若你已按 u1-l2 构建了项目，可运行 `./run_reg_test.py` 中带 NoC 的回归任务，或在 `vpr/test/test_noc_storage` 上用 `--list-tests` 查看存储相关用例（具体用例名待本地确认）。

#### 4.1.5 小练习与答案

**练习 1**：`NocStorage` 里 `router_id_conversion_table` 和 `grid_location_to_router_id` 都把 `int` 映射到 `NocRouterId`，它们的键含义有何不同？

**答案**：`router_id_conversion_table` 的键是**用户在架构文件里给的整数 ID**（任意、可能不连续），用于在报错信息里显示用户能看懂的编号；`grid_location_to_router_id` 的键是由网格坐标 `(x, y, layer)` 计算出的**唯一空间键**，用于布局期从「逻辑块当前坐标」反查「坐在该坐标的物理路由器」。

**练习 2**：为什么 `add_link` 的源、汇用的是 `NocRouterId`（内部 ID），而 `add_router` 用的是 `int`（用户 ID）？

**答案**：`add_router` 是用户描述进入系统的入口，自然用用户 ID；建好路由器后系统已分配内部稠密 ID，`add_link` 处于同一构建流程的后续步骤，故用内部 ID 直接索引，避免重复转换、保证稠密性。

---

### 4.2 流量描述 NocTrafficFlows

#### 4.2.1 概念说明

`NocStorage` 描述了「NoC 长什么样」（拓扑），而 `NocTrafficFlows` 描述「**谁要和谁通信、通信多少、约束是什么**」——也就是**流量（traffic flow）**的集合。

一条流量 `t_noc_traffic_flow` 是两个**逻辑路由器**之间的一次通信，携带：

- 源/汇逻辑路由器模块名（字符串，部分匹配，须唯一标识网表中的块）及其 `ClusterBlockId`；
- **带宽** `traffic_flow_bandwidth`（字节/秒）：布线后累加到所经过链路的占用上，用于衡量拥塞；
- **最大延迟** `max_traffic_flow_latency`（秒）：评估该流量是否满足时序约束；
- **优先级** `traffic_flow_priority`：越高越优先被降低延迟、满足约束。

流量由用户在一个独立的 `.flows` XML 文件里描述（命令行 `--noc_flows_file`），由 `read_xml_noc_traffic_flows_file` 解析（解析器在 [read_xml_noc_traffic_flows_file.h:37-48](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/read_xml_noc_traffic_flows_file.h#L37-L48)）。

`NocTrafficFlows` 的设计动机是**布局期的快速增量重路由**：退火每试一次移动（u5-1），若移动的是逻辑路由器块，则只有「以该块为源或汇」的流量需要重路由。为此它维护了一张 `ClusterBlockId → vector<NocTrafficFlowId>` 的反查表，O(1) 拿到受影响流量。

#### 4.2.2 核心流程

流量的录入与查询流程：

```
解析 .flows 文件
   → 对每条流量调用 create_noc_traffic_flow(...)
       → 追加到 noc_traffic_flows（主存储，SoA 风格 vtr::vector）
       → 同时登记到 traffic_flows_associated_to_router_blocks[source] 和 [...sink]
   → 全部录完调用 finished_noc_traffic_flows_setup()（置 built_traffic_flows=true，锁死）

布局期移动逻辑路由器块 blk：
   → check_if_cluster_block_has_traffic_flows(blk)   // O(1) 判断是否需要重路由
   → 若是：get_traffic_flows_associated_to_router_block(blk) // O(1) 拿受影响流量列表
   → 对每条受影响流量重路由（见 4.3 与 4.4）
```

延迟约束有一个重要默认值：用户不提供时，`max_traffic_flow_latency` 取 `DEFAULT_MAX_TRAFFIC_FLOW_LATENCY = 1.0` 秒。这个值故意远大于 NoC 内部纳秒级延迟，使得「无约束」流量对布局代价的贡献为 0（见 [noc_traffic_flows.h:298-310](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/noc_traffic_flows.h#L298-L310)）。

#### 4.2.3 源码精读

流量结构 `t_noc_traffic_flow` 的字段（注意源/汇同时存了模块名与 `ClusterBlockId`）：

```cpp
// vpr/src/noc/noc_traffic_flows.h
struct t_noc_traffic_flow {
    std::string source_router_module_name;   // 源逻辑路由器名（部分匹配）
    std::string sink_router_module_name;     // 汇逻辑路由器名
    ClusterBlockId source_router_cluster_id; // 源块 ID（建表后回填）
    ClusterBlockId sink_router_cluster_id;   // 汇块 ID
    double traffic_flow_bandwidth;           // 带宽（字节/秒）
    double max_traffic_flow_latency;         // 最大允许延迟（秒）
    int traffic_flow_priority;               // 优先级 [0, +inf)
};
```

详见 [noc_traffic_flows.h:44-79](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/noc_traffic_flows.h#L44-L79)。

`NocTrafficFlows` 的私有成员——主存储 + 反查索引 + 网表内全部逻辑路由器块：

```cpp
// vpr/src/noc/noc_traffic_flows.h
class NocTrafficFlows {
    vtr::vector<NocTrafficFlowId, t_noc_traffic_flow> noc_traffic_flows;          // 所有流量
    std::vector<NocTrafficFlowId> noc_traffic_flows_ids;                          // 全部流量 ID（便于遍历）
    std::vector<ClusterBlockId> router_cluster_in_netlist;                        // 网表内所有逻辑路由器块
    std::unordered_map<ClusterBlockId, std::vector<NocTrafficFlowId>>
        traffic_flows_associated_to_router_blocks; // 块 → 相关流量（增量重路由的钥匙）
    bool built_traffic_flows;
};
```

详见 [noc_traffic_flows.h:81-118](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/noc_traffic_flows.h#L81-L118)。

`create_noc_traffic_flow` 的实现——关键在于一条流量会被同时登记到**源和汇**两个块的关联表，所以无论移动哪一端都能被找到：

```cpp
// vpr/src/noc/noc_traffic_flows.cpp
void NocTrafficFlows::create_noc_traffic_flow(...) {
    VTR_ASSERT_MSG(!built_traffic_flows, "...");     // 锁死检查
    noc_traffic_flows.emplace_back(...);             // 追加到主存储
    NocTrafficFlowId id = (NocTrafficFlowId)(noc_traffic_flows.size() - 1);
    noc_traffic_flows_ids.emplace_back(id);
    add_traffic_flow_to_associated_routers(id, source_router_cluster_id); // 登记到源块
    add_traffic_flow_to_associated_routers(id, sink_router_cluster_id);   // 登记到汇块
}
```

详见 [noc_traffic_flows.cpp:55-80](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/noc_traffic_flows.cpp#L55-L80)。

布局期判断「这个块是否参与 NoC 通信」的 O(1) 接口：

```cpp
// vpr/src/noc/noc_traffic_flows.h
bool check_if_cluster_block_has_traffic_flows(ClusterBlockId block_id) const;
static constexpr double DEFAULT_MAX_TRAFFIC_FLOW_LATENCY = 1.; // 无约束流量的默认延迟上限
```

详见 [noc_traffic_flows.h:269-310](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/noc_traffic_flows.h#L269-L310)。

#### 4.2.4 代码实践

**实践目标**：理解流量如何驱动 NoC 路由决策——即「流量 + 当前布局 → 起止物理路由器 → 路由算法产出链路序列」。

**操作步骤**（源码阅读型）：

1. 阅读 [noc_place_utils.cpp:216-236](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/noc_place_utils.cpp#L216-L236) 的 `route_traffic_flow`，这是「流量 → 路由」的核心胶水函数。跟踪它：
   - 从 `NocTrafficFlows` 取出流量的源/汇 `ClusterBlockId`；
   - 用 `block_locs_ref[...].loc` 查到这两个逻辑块**当前**所在网格坐标；
   - 用 `noc_model.get_router_at_grid_location(...)` 把坐标翻译成**物理**路由器 ID；
   - 调用 `noc_flows_router.route_flow(src, sink, flow_id, route, noc_model)`，得到一条 `vector<NocLinkId>` 路径。
2. 思考：为什么这里要先「逻辑块 → 坐标 → 物理路由器」再路由？因为 NoC 路由算法工作在**物理拓扑图**上，而流量的源汇是**逻辑块**；布局改变了块的位置，物理起止点随之变化，这正是 NoC 路由必须在布局期反复执行的原因。

**需要观察的现象**：同一流量在不同布局下会被路由到**不同的物理路径**（因为起止物理路由器变了）。

**预期结果**：你能画出「流量 → 起止物理路由器 → route_flow → 链路序列」这条调用链，并解释 `NocTrafficFlows` 的反查表如何把「移动一个块」缩减为「只重路由相关流量」。

**待本地验证**：本步骤为静态阅读，无需运行；若要观察实际流量文件格式，可在仓库内搜索 `*.flows` 示例（待确认是否存在）。

#### 4.2.5 小练习与答案

**练习 1**：为什么一条流量要同时登记到源块和汇块的关联表，而不是只登一处？

**答案**：因为布局期移动的可能是源路由器，也可能是汇路由器（退火随机移动）。两端都登记后，无论移动哪一端，都能 O(1) 找到该流量并重路由；只登一处会导致移动另一端时漏掉重路由。

**练习 2**：`DEFAULT_MAX_TRAFFIC_FLOW_LATENCY` 为什么取 1 秒这样「离谱地大」的值？

**答案**：NoC 内部延迟在纳秒量级，1 秒远大于任何真实延迟。这样「未指定约束」的流量永远不会触发延迟越限（latency overrun）代价，对布局目标函数的贡献恒为 0，等价于「该流量没有延迟约束」，从而把「有约束」与「无约束」统一进同一套代价计算。

---

### 4.3 NoC 路由算法

#### 4.3.1 概念说明

NoC 路由要解决的问题是：给定源、汇两个**物理**路由器，在 NoC 拓扑图上找一条由 `NocLinkId` 组成的合法路径。这与 u6 讲的器件级布线（Pathfinder 协商式迷宫布线）是**完全不同**的问题域：

- 器件级布线（u6）：在巨大 RR Graph 上为成千上万条网协商式找路径，目标是可布线性与时序。
- NoC 路由：在小的 NoC 拓扑图上为每条流量找路径，目标是**最短/最小跳数**或**死锁无关（deadlock-free）**，且要在退火的数百万次迭代中被反复快速调用。

VPR 把所有 NoC 路由算法抽象为一个接口 `NocRouting`，其唯一核心方法是纯虚函数 `route_flow`。算法分三大类：

1. **广度优先搜索（BFS）**：`BFSRouting`。找**最少跳数**路径，适用于任意拓扑，但**不保证死锁无关**。这是默认算法。
2. **回转模型（Turn Model）族**：`XYRouting`、`WestFirstRouting`、`NorthLastRouting`、`NegativeFirstRouting`、`OddEvenRouting`。通过**禁止特定转向**来保证 mesh/torus 拓扑下的死锁无关性，路径是最短的。它们共享基类 `TurnModelRouting`。
3. **SAT 求解**：`noc_sat_route`（自由函数，非 `NocRouting` 子类）。把路由建模为布尔可满足性问题，找死锁无关、无拥塞、且最小化总带宽的解。由编译期宏 `ENABLE_NOC_SAT_ROUTING` 控制，默认不编译进二进制。

> **关于死锁（deadlock）**：NoC 中数据以分组（packet/flit）沿链路逐跳传输，若一组流量形成「循环信道依赖」（A 等 B 释放链路、B 等 A 释放链路），网络会永久卡死。回转模型算法通过在网格拓扑上**禁止至少一个顺时针转向和一个逆时针转向**来打破所有可能的环，从理论上保证无死锁。BFS 只管最短，可能产生环依赖，所以官方说明它不保证死锁无关。

#### 4.3.2 核心流程

**工厂分派**：命令行 `--noc_routing_algorithm <名字>` 给出字符串，`NocRoutingAlgorithmCreator::create_routing_algorithm` 用一串 `if/else` 匹配并 `std::make_unique` 出对应子类对象，返回 `unique_ptr<NocRouting>`。不匹配则 `VPR_FATAL_ERROR`。

**统一调用**：无论哪种算法，调用方都只调 `route_flow(src, sink, flow_id, flow_route, noc_model)`，算法把找到的路径（一组 `NocLinkId`）写回 `flow_route`。

**BFS 算法流程**（典型图搜索）：

```
初始化：队列 ← {src}，visited ← {src}，parent_link ← {}，found ← (src==sink)
while 队列非空 且 未找到:
    curr ← 队首出队
    for curr 的每条出边 link:
        next ← link.sink_router
        if next 未访问:
            标记访问、入队、记录 parent_link[next] = link
            if next == sink: found=true; break
if found: generate_route(sink)  // 沿 parent_link 回溯成路径
else:     VPR_FATAL_ERROR（源汇不连通）
```

**回转模型算法流程**（在 `TurnModelRouting.route_flow` 里实现，子类只提供两个钩子）：

```
curr ← src
while curr != sink:
    legal_dirs ← get_legal_directions(src, curr, sink, prev_dir, noc_model)  // 子类实现
    dir ← select_next_direction(legal_dirs, ...)                              // 默认：朝距离更长的轴偏置选
    link ← move_to_next_router(curr, dir, ...)                                // 找一条指向 dir 的出边
    flow_route.push_back(link)
    curr ← link.sink_router
```

两个由子类实现的纯虚钩子是：`get_legal_directions`（依据 src/curr/sink 位置返回合法方向，子类借此「禁止特定转向」）和 `is_turn_legal`（用于枚举所有非法转向）。`TurnModelRouting` 的 `route_flow` 本身是公共的，所有子类共享（见 [turn_model_routing.h:55-90](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/turn_model_routing.h#L55-L90)）。

#### 4.3.3 源码精读

抽象接口 `NocRouting`——只有一个纯虚方法 `route_flow`，注释明确它「应作为基类/接口使用」：

```cpp
// vpr/src/noc/noc_routing.h
class NocRouting {
  public:
    virtual ~NocRouting() = default;
    virtual void route_flow(NocRouterId src_router_id,
                            NocRouterId sink_router_id,
                            NocTrafficFlowId traffic_flow_id,
                            std::vector<NocLinkId>& flow_route,
                            const NocStorage& noc_model) = 0;
};
```

详见 [noc_routing.h:27-62](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/noc_routing.h#L27-L62)。

工厂分派——6 个运行期可选算法，字符串不匹配即致命错误：

```cpp
// vpr/src/noc/noc_routing_algorithm_creator.cpp
std::unique_ptr<NocRouting> NocRoutingAlgorithmCreator::create_routing_algorithm(
    const std::string& routing_algorithm_name, const NocStorage& noc_model) {
    if (routing_algorithm_name == "xy_routing")              return std::make_unique<XYRouting>();
    else if (routing_algorithm_name == "bfs_routing")        return std::make_unique<BFSRouting>();
    else if (routing_algorithm_name == "west_first_routing") return std::make_unique<WestFirstRouting>();
    else if (routing_algorithm_name == "north_last_routing") return std::make_unique<NorthLastRouting>();
    else if (routing_algorithm_name == "negative_first_routing") return std::make_unique<NegativeFirstRouting>();
    else if (routing_algorithm_name == "odd_even_routing")   return std::make_unique<OddEvenRouting>(noc_model);
    else VPR_FATAL_ERROR(VPR_ERROR_OTHER, "The provided NoC routing algorithm '%s' is not supported.", ...);
}
```

详见 [noc_routing_algorithm_creator.cpp:11-32](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/noc_routing_algorithm_creator.cpp#L11-L32)。注意 `OddEvenRouting` 构造时需要 `noc_model`（因为它的合法方向依赖各列奇偶性，需预计算），其余不需要。

命令行选项——默认 `bfs_routing`，且 `argparse` 的 `.choices(...)` 限定了 6 个合法取值（与工厂一一对应）：

```cpp
// vpr/src/base/read_options.cpp
noc_grp.add_argument<std::string>(args.noc_routing_algorithm, "--noc_routing_algorithm")
    .help("Controls the algorithm used by the NoC to route packets.\n"
          "* xy_routing: ... recommended with mesh ...\n"
          "* bfs_routing: ... minimum number of links, NOT deadlock-free, any topology\n"
          "* west_first_routing / north_last_routing / negative_first_routing / odd_even_routing: ...")
    .default_value("bfs_routing")
    .choices({"xy_routing","bfs_routing","west_first_routing","north_last_routing",
              "negative_first_routing","odd_even_routing"});
```

详见 [read_options.cpp:3662-3675](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L3662-L3675)。

BFS 主循环——标准广度优先，用 `parent_link` 记录每个路由器是由哪条边首次到达的，以便回溯：

```cpp
// vpr/src/noc/bfs_routing.cpp
std::queue<NocRouterId> routers_to_process;
std::unordered_map<NocRouterId, NocLinkId> router_parent_link;
std::unordered_set<NocRouterId> visited_routers;
routers_to_process.push(src_router_id); visited_routers.insert(src_router_id);
while (!routers_to_process.empty() && !found_sink_router) {
    NocRouterId processing_router = routers_to_process.front(); routers_to_process.pop();
    for (auto link : noc_model.get_noc_router_outgoing_links(processing_router)) {
        NocRouterId connected_router = noc_model.get_single_noc_link(link).get_sink_router();
        if (visited_routers.find(connected_router) == visited_routers.end()) {
            visited_routers.insert(connected_router);
            routers_to_process.push(connected_router);
            router_parent_link.insert({connected_router, link});   // 记录到达边
            if (connected_router == sink_router_id) { found_sink_router = true; break; }
        }
    }
}
if (found_sink_router) generate_route(sink_router_id, flow_route, noc_model, router_parent_link);
else VPR_FATAL_ERROR(...);
```

详见 [bfs_routing.cpp:56-103](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/bfs_routing.cpp#L56-L103)。回溯建路 `generate_route` 从汇出发，沿 `parent_link` 一路取源路由器、把链路插到路径头部，见 [bfs_routing.cpp:105-133](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/bfs_routing.cpp#L105-L133)。

回转模型基类 `TurnModelRouting`——注意它**自己实现了** `route_flow`，并暴露方向枚举与非法转向枚举接口；死锁无关性来自「禁止特定转向」的理论保证：

```cpp
// vpr/src/noc/turn_model_routing.h
class TurnModelRouting : public NocRouting {
  public:
    void route_flow(...) override;                       // 公共算法骨架
    std::vector<std::pair<NocLinkId,NocLinkId>> get_all_illegal_turns(...) const; // 枚举非法转向
    virtual bool is_turn_legal(const std::array<...,3>& noc_routers, ...) const = 0; // 子类判定
  protected:
    enum class Direction { WEST, EAST, NORTH, SOUTH, UP, DOWN, N_DIRECTIONS, INVALID };
  private:
    virtual const std::vector<Direction>& get_legal_directions(...) = 0; // 子类核心钩子
};
```

详见 [turn_model_routing.h:55-131](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/turn_model_routing.h#L55-L131)。文件头注释解释了核心思想：「若至少禁止一个顺时针转向和一个逆时针转向，就不可能形成循环依赖，从而杜绝死锁」。

具体算法举例——`XYRouting` 继承 `TurnModelRouting`，只实现钩子，先走 X 再走 Y：

```cpp
// vpr/src/noc/xy_routing.h
class XYRouting : public TurnModelRouting {
  private:
    const std::vector<Direction>& get_legal_directions(...) override; // 水平未对齐→返回X方向；列对齐→返回Y方向
    Direction select_next_direction(...) override;
    bool is_turn_legal(...) const override;
};
```

详见 [xy_routing.h:88-117](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/xy_routing.h#L88-L117)。其余四个算法（[west_first_routing.h:18-44](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/west_first_routing.h#L18-L44)、north_last、negative_first、odd_even）结构相同，只是 `get_legal_directions`/`is_turn_legal` 的「禁止哪些转向」策略不同。

SAT 路由——编译期门控，且是自由函数而非 `NocRouting` 子类，因此**不在**运行期工厂的 6 个选项里：

```cpp
// vpr/src/noc/sat_routing.h
#ifdef ENABLE_NOC_SAT_ROUTING
vtr::vector<NocTrafficFlowId, std::vector<NocLinkId>> noc_sat_route(
    bool minimize_aggregate_bandwidth, const t_noc_opts& noc_opts, int seed);
#endif
```

详见 [sat_routing.h:13-44](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/sat_routing.h#L13-L44)。它把每个「(流量, 链路)」对关联一个布尔变量，用一组约束保证路径连续、无死锁、最小化拥塞，参考论文 *The Road Less Traveled: Congestion-Aware NoC Placement and Packet Routing for FPGAs*。

#### 4.3.4 代码实践

**实践目标**：列出 `noc` 目录支持的全部路由算法，对比它们的适用场景，并理解 `noc_traffic_flows` 描述的流量如何驱动路由决策。

**操作步骤**：

1. **列算法**：打开 [noc_routing_algorithm_creator.cpp:11-32](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/noc_routing_algorithm_creator.cpp#L11-L32)，列出工厂支持的 6 个字符串名与对应类。再打开 [read_options.cpp:3662-3675](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L3662-L3675) 确认它们就是 `--noc_routing_algorithm` 的合法取值。最后注意到 `sat_routing` 因编译期门控而**不在**这 6 个里。
2. **分类**：把这 6 个分成两类——BFS（`bfs_routing`，唯一非 TurnModel）与 TurnModel 族（其余 5 个，都继承 `TurnModelRouting`，见各头文件 `: public TurnModelRouting`）。
3. **驱动关系**：重新跟踪 4.2.4 的 `route_traffic_flow`（[noc_place_utils.cpp:216-236](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/noc_place_utils.cpp#L216-L236)）：`noc_traffic_flows` 提供「源/汇逻辑块 + 带宽/延迟/优先级」，布局把逻辑块翻译成物理路由器，路由算法在物理拓扑上找路。**流量的带宽决定路径上每条链路的占用增量，流量的延迟约束决定路径的代价**——这就是「流量驱动路由决策」的两条因果线。

**需要观察的现象**：
- BFS 路径跳数最少，但若拓扑是 mesh，它和 XY 可能给出不同路径（BFS 不区分方向，XY 严格先 X 后 Y）。
- 把 `--noc_routing_algorithm xy_routing` 用于**非 mesh** 拓扑时，官方注释警告「可能找不到路由或不再保证死锁无关」。

**预期结果**：你能在不查代码的情况下，说出 6 个算法名、它们的基类、默认值是 `bfs_routing`，以及 SAT 路由需要单独编译开关。

**待本地验证**：若已构建项目，可在带 NoC 的架构上分别用 `--noc_routing_algorithm bfs_routing` 与 `xy_routing` 各跑一次，对比 `.net`/报告中的 NoC 路径统计（具体输出字段名待本地确认）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `--noc_routing_algorithm` 的 `.choices(...)` 列表里**没有** `sat_routing`？

**答案**：SAT 路由由编译期宏 `ENABLE_NOC_SAT_ROUTING` 门控，默认不编译进二进制；而且它是自由函数 `noc_sat_route`，并不继承 `NocRouting`、不参与运行期工厂的字符串分派。因此它不能通过这个命令行选项选择，需要在构建时启用宏并在专门路径调用。

**练习 2**：BFS 和回转模型族（如 XY）在「死锁」与「拓扑适用性」上有何取舍？

**答案**：BFS 适用于**任意**拓扑（只要源汇连通就能找到路），且天然最短跳数，但**不保证死锁无关**；回转模型族在 mesh/torus 上**保证死锁无关**且最短，但依赖网格方向语义，对非 mesh 拓扑可能失效。所以默认值选 `bfs_routing`（最通用），而对确定是 mesh 的设计可换用 XY 等以获得死锁安全性。

---

### 4.4 布局与 NoC 路由的耦合（串联模块）

#### 4.4.1 概念说明

前面三个模块分别讲了拓扑、流量、路由。本模块把它们串起来，回答：**NoC 路由结果如何反过来影响布局代价，从而使「布局」和「NoC 路由」在退火中协同迭代？**

答案是：`NocStorage` 与 `NocTrafficFlows` 都住在全局 `NocContext`（[vpr_context.h:731-764](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L731-L764)）里，布局器用一个专门的 `NocCostHandler`（[noc_place_utils.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/noc_place_utils.h)）在每次退火移动后：

1. 找到受影响的逻辑路由器块，重路由其相关流量；
2. 用新路径计算 NoC 代价增量 ΔC，并入 u5-1 的总代价；
3. 按退火接受准则决定接受/回滚，并相应提交或撤销链路带宽占用。

#### 4.4.2 核心流程

```
退火试探一次移动（u5-1）
  → NocCostHandler::find_affected_noc_routers_and_update_noc_costs(...)
      → 对每个被移动的逻辑路由器块 blk：
          get_traffic_flows_associated_to_router_block(blk)   // 4.2 的反查表
          对每条受影响流量：re_route_traffic_flow(...)         // 4.3 的 route_flow
          用 find_affected_links_by_flow_reroute 求新旧路径的对称差
          调整这些链路的 proposed 带宽占用
      → 计算 NoC 代价项的 ΔC（聚合带宽 / 延迟 / 延迟越限 / 拥塞）
  → 退火判断 ACCEPTED / REJECTED / ABORTED
      → ACCEPTED: commit_noc_costs()            （转正 proposed 表）
      → REJECTED: revert_noc_traffic_flow_routes()（撤销，恢复旧路径）
```

NoC 代价有四个分量，对应 `t_noc_opts`（[vpr_types.h:1468-1485](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L1468-L1485)）里的四组权重：

| 代价项 | 含义 | 对应权重选项 |
| --- | --- | --- |
| aggregate bandwidth（聚合带宽） | 所有流量所用链路的带宽总和 | `--noc_aggregate_bandwidth_weighting` |
| latency（延迟） | 流量的零负载路径延迟 | `--noc_latency_weighting` |
| latency overrun（延迟越限） | 超出 `max_traffic_flow_latency` 的部分 | `--noc_latency_constraints_weighting` |
| congestion（拥塞） | 链路占用超出容量的比例 | `--noc_congestion_weighting` |

再由顶层 `--noc_placement_weighting` 把整组 NoC 代价相对线长/时序做加权。

#### 4.4.3 源码精读

`NocContext` 把三件套聚合为全局子上下文（拓扑 + 流量 + 路由器对象）：

```cpp
// vpr/src/base/vpr_context.h
struct NocContext : public Context {
    NocStorage noc_model;                       // 拓扑（4.1）
    NocTrafficFlows noc_traffic_flows_storage;  // 流量（4.2）
    std::unique_ptr<NocRouting> noc_flows_router; // 由 --noc_routing_algorithm 创建（4.3）
};
```

详见 [vpr_context.h:731-764](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L731-L764)。它继承不可拷贝的 `Context`（u3-4），避免巨型状态被意外深拷贝。

增量重路由的核心——只重路由受影响流量，并用对称差只更新变化的链路：

```cpp
// vpr/src/place/noc_place_utils.cpp
void NocCostHandler::re_route_associated_traffic_flows(ClusterBlockId moved_block_router_id, ...) {
    const auto& assoc = noc_traffic_flows_storage.get_traffic_flows_associated_to_router_block(moved_block_router_id);
    for (NocTrafficFlowId fid : assoc) {
        if (updated_traffic_flows.find(fid) == updated_traffic_flows.end()) {   // 每条流量至多重路由一次
            std::vector<NocLinkId> prev = traffic_flow_routes[fid];
            re_route_traffic_flow(fid, ...);                                    // 调 route_flow
            updated_traffic_flows.insert(fid);
            std::vector<NocLinkId> curr = traffic_flow_routes[fid];
            auto unique_links = find_affected_links_by_flow_reroute(prev, curr); // 新旧路径对称差
            ...                                                                  // 据此调整带宽占用与代价
        }
    }
}
```

详见 [noc_place_utils.cpp:253-279](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/noc_place_utils.cpp#L253-L279)。

单条流量的逐流代价结构（聚合带宽 + 延迟 + 延迟越限三项，拥塞在链路层单独算）：

```cpp
// vpr/src/place/noc_place_utils.h
struct TrafficFlowPlaceCost {
    double aggregate_bandwidth = INVALID_NOC_COST_TERM;  // 该流量所用链路带宽之和
    double latency = INVALID_NOC_COST_TERM;             // 该流量零负载路径延迟
    double latency_overrun = INVALID_NOC_COST_TERM;     // 超出约束的部分
};
```

详见 [noc_place_utils.h:508-522](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/noc_place_utils.h#L508-L522)。首次全量路由则遍历所有流量并逐流调用 `route_flow`，见 [noc_place_utils.cpp:530-550](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/noc_place_utils.cpp#L530-L550)。

#### 4.4.4 代码实践

**实践目标**：把「移动 → 重路由 → ΔC → 接受/回滚」这条耦合链在源码里走通。

**操作步骤**（源码阅读型）：

1. 从 [noc_place_utils.h:9-22](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/noc_place_utils.h#L9-L22) 的类注释读起，它把 `NocCostHandler` 的使用顺序写得很清楚：`initial_noc_routing` → `comp_noc_*_cost` → 每次移动 `find_affected_noc_routers_and_update_noc_costs` → `commit_noc_costs` 或 `revert_noc_traffic_flow_routes`。
2. 跟踪「proposed vs actual 双缓冲」：代价与带宽占用都维护当前值和提议值两套，退火接受才转正、拒绝就丢弃。这正是 u5-3 讲的「事务化增量代价」在 NoC 上的对应物。

**需要观察的现象**：NoC 代价计算是**增量**的——每次移动只动相关流量与对称差链路，而非全图重算，这是退火百万次迭代可行的前提。

**预期结果**：你能解释「为什么 NoC 路由必须可插拔、低开销」（被退火反复调用），以及 `NocTrafficFlows` 的反查表为何是这套增量机制的效率基石。

**待本地验证**：本步为静态阅读。

#### 4.4.5 小练习与答案

**练习**：NoC 路由（本讲）和器件级布线（u6）都叫「routing」，它们在 VPR 中是同一套代码吗？为何要分开？

**答案**：不是同一套。器件级布线在 `vpr/src/route/`，工作在巨大的 RR Graph 上，用 Pathfinder 协商式迷宫搜索为成千上万条网找路径，目标是可布线性与时序；NoC 路由在 `vpr/src/noc/`，工作在小得多的 NoC 拓扑图上，为每条流量找最短或死锁无关路径，且需被布局退火高频调用。两者问题规模、目标函数（可布线性 vs 死锁/拥塞）和调用频率都不同，所以 VPR 用两套独立的数据结构与算法分别处理，仅在「都受架构驱动、都挂全局上下文」这一架构层面相通。

---

## 5. 综合实践

**任务**：在脑中（或纸面上）为一次「NoC 布局移动」完整复盘数据流，并用代码验证你对算法分发的理解。

1. **建图（架构驱动）**：阅读 [setup_noc.cpp:110-129](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_noc.cpp#L110-L129)，写出 `NocStorage` 是如何从 `t_arch::noc` 被填充并冻结的。列出 `NocContext` 三个成员各自的数据来源（架构 XML、`.flows` 文件、命令行）。
2. **流量驱动路由**：假设退火把逻辑路由器块 R 从坐标 (2,2) 移到 (5,5)，跟踪 `re_route_associated_traffic_flows`（[noc_place_utils.cpp:253-279](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/noc_place_utils.cpp#L253-L279)）→ `route_traffic_flow`（[noc_place_utils.cpp:216-236](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/noc_place_utils.cpp#L216-L236)）→ `route_flow`，画出新旧物理起止点如何变化、路径如何随之改变。
3. **算法对照**：打开 [test_bfs_routing.cpp:11-64](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/test/test_bfs_routing.cpp#L11-L64) 的 4×4 mesh。手工选定源路由器 0、汇路由器 15，分别用 BFS（[bfs_routing.cpp:56-103](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/bfs_routing.cpp#L56-L103)）和 XY（[xy_routing.h:88-117](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/xy_routing.h#L88-L117)）推演路径，验证：BFS 给出任一最短路径，XY 严格给出「先向右到第 3 列、再向上到第 3 行」的唯一路径。
4. **选项实验**（可选，待本地验证）：若已按 u1-l2 构建，对一个带 NoC 的设计分别加 `--noc_routing_algorithm bfs_routing` 与 `--noc_routing_algorithm xy_routing` 运行，对比运行日志中 NoC 相关代价（聚合带宽、拥塞）是否因路径不同而变化。

**验收标准**：你能不查代码地回答——`NocStorage` 的四张表是什么、`NocTrafficFlows` 如何把「移动一个块」缩减为「重路由少数流量」、6 个运行期算法如何分派、SAT 为何不在其中、NoC 代价有哪四个分量。

## 6. 本讲小结

- **NoC 用有向图建模**：`NocStorage` 以路由器为节点、链路为有向边，靠「节点表 + 边表 + 出/入边邻接表 + 两张 int→NocRouterId 映射表」撑起整张图，构建后用 `finished_building_noc()` 冻结为只读。
- **物理路由器 vs 逻辑路由器**：物理路由器是芯片固定瓦片（`NocRouter`，带网格坐标），逻辑路由器是网表里的块（`ClusterBlockId`）；布局把逻辑块搬到物理瓦片上，靠 `grid_location_to_router_id` 完成坐标→物理路由器的反查。
- **流量是通信需求**：`NocTrafficFlows` 用 `t_noc_traffic_flow` 描述源/汇逻辑路由器及带宽、延迟约束、优先级，并用 `ClusterBlockId → 流量列表` 的反查表支撑布局期增量重路由；默认延迟约束 1 秒等价于「无约束」。
- **路由算法统一接口**：所有算法实现 `NocRouting::route_flow`，产出 `vector<NocLinkId>`；运行期工厂按 `--noc_routing_algorithm`（默认 `bfs_routing`）分派 6 种算法：1 个 BFS + 5 个继承 `TurnModelRouting` 的回转模型算法。
- **死锁与拓扑的取舍**：BFS 任意拓扑可用但不保证死锁无关；回转模型族在 mesh/torus 上靠「禁止特定转向」保证死锁无关；SAT 路由由编译期宏门控、不在运行期选项里。
- **布局与路由耦合迭代**：`NocCostHandler` 在退火每次移动后重路由受影响流量、用对称差增量更新四个 NoC 代价项（聚合带宽、延迟、延迟越限、拥塞），经 proposed/actual 双缓冲实现提交或回滚，使布局与 NoC 路由协同收敛。

## 7. 下一步学习建议

- **回到布局主循环**：结合 u5-1（模拟退火框架）与 u5-3（增量代价与事务回滚），你会更清晰地看到 `NocCostHandler` 是如何作为「第四组代价项」嵌入 `t_placer_costs` 的；可继续阅读 `vpr/src/place/annealer.cpp` 中 NoC 相关的移动评估。
- **NoC 专用移动策略**：阅读 `vpr/src/place/initial_noc_placement.cpp` 与 `move_generators/centroid_move_generator.cpp`，看 `--noc_swap_percentage` 与 `--noc_centroid_weight` 如何让退火偏向 NoC 路由器块、用质心移动引导它们靠近目标物理瓦片。
- **SAT 路由的深度**：若你对约束求解感兴趣，启用 `ENABLE_NOC_SAT_ROUTING` 宏后阅读 [sat_routing.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/noc/sat_routing.cpp) 与 `channel_dependency_graph.*`，理解死锁无关性如何被编码为信道依赖图上的无环约束。
- **架构侧入口**：阅读架构 XML 解析中 NoC 段的读入（`t_noc_inf` 的填充），把 `setup_noc` 与 u2 的架构解析串联，完整理解「架构 XML → t_arch → NocStorage」这条架构驱动链路。
- **测试与回归**：参考 u9-2，运行 `vpr/test/test_bfs_routing`、`test_noc_storage`、`test_noc_place_utils`、`test_read_xml_noc_traffic_flows_file` 等单元测试，用断言验证你对各算法行为的推断。
