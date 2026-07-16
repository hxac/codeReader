# 路由器前瞻 Router Lookahead

## 1. 本讲目标

本讲讲解 VPR 布线中的「指南针」——路由器前瞻（Router Lookahead）。

读者学完后应该能够：

1. 说清楚**前瞻代价**在连接路由器（Connection Router）迷宫搜索中扮演什么角色，以及它为什么是 A\* 定向扩展的关键。
2. 列举 `e_router_lookahead` 枚举的全部取值，说明 `classic / map / compressed_map / extended_map / simple / no_op` 各自的实现类、精度与开销权衡。
3. 看懂 `MapLookahead` 这类「代价地图」如何用 Dijkstra 预计算、如何按「平移不变性」压缩成小表、又如何用 Cap'n Proto / CSV 序列化。

本讲依赖 u6-l1（RR Graph）与 u6-l2（连接路由器与堆）。前序讲义已经讲过：RR Graph 把器件布线结构标量化为有向图；连接路由器在图上为单个「源→汇」连接做 A\* 迷宫搜索，入堆 priority 由「现时项 + 前瞻项」组成。本讲就把那个「前瞻项」彻底拆开。

## 2. 前置知识

在进入源码之前，先用三段话建立直觉。

**第一，A\* 搜索需要启发函数。** 连接路由器本质是在 RR Graph 上跑 A\*：从源点出发，每弹出一个代价最小的节点就向邻居扩散，直到命中目标汇点。A\* 的代价函数是

\[ f(n) = g(n) + h(n) \]

其中 \(g(n)\) 是「已经花掉的代价」（现时项，来自已走过的路径），\(h(n)\) 是「估计还要花多少代价才能到目标」（前瞻项，即本讲主角）。若令 \(h(n)\equiv 0\)，A\* 就退化成 Dijkstra——保证最短但会盲目向四周扩散；\(h(n)\) 越准，搜索越能朝目标定向收拢，速度越快。u6-l2 已指出：把 `astar_fac` 设为 0 就关闭前瞻、退化为 Dijkstra。

**第二，前瞻必须「快」又「相对准」。** 布线一次连接可能要入堆上百万次，每次入堆都要调用一次 `h(n)`。所以前瞻代价必须是 O(1) 查表，绝不能临时再跑一遍搜索。VPR 的做法是：**布线开始前，先花几秒到几十秒跑一遍预计算**，把「从一种线型出发、向右/上方走 (dx, dy) 距离大约要多少延迟和拥塞代价」统计成一张表；布线时只需查表。

**第三，前瞻依赖「平移不变性」假设。** 这张预计算表之所以小，是因为它假设「代价只取决于相对位移 (dx, dy) 和线型，与绝对坐标无关」。绝大多数 FPGA 布线结构在芯片内部是周期性重复的，这个假设大致成立。代价地图因此可以按 `[线层][线型][dx][dy]` 而不是按每个节点存，规模从「百万节点」降到「几十×几十」。

> 名词速查：**现时项 backward_path_cost**（已走代价）、**前瞻项 expected_cost**（预估剩余代价）、**criticality**（连接的关键度，0 表示只看拥塞、1 表示只看延迟）、**R_upstream**（上游累计电阻，用于估算 RC 延迟）。

## 3. 本讲源码地图

本讲涉及的关键文件全部位于 `vpr/src/route/router_lookahead/`，加上两处外部引用：

| 文件 | 作用 |
| --- | --- |
| `router_lookahead.h` / `.cpp` | 抽象基类 `RouterLookahead`、工厂函数 `make_router_lookahead`、缓存 `get_cached_router_lookahead`，以及 `ClassicLookahead`、`NoOpLookahead` 两个内联实现 |
| `vpr/src/base/vpr_types.h` | 定义枚举 `e_router_lookahead`（6 种前瞻类型） |
| `router_lookahead_map.h` / `.cpp` | 默认实现 `MapLookahead`：预计算、查询、序列化 |
| `router_lookahead_cost_map.h` | `CostMap` 类（被 `extended_map` 使用），按「源线型×dx×dy」组织代价表 |
| `router_lookahead_map_utils.h` | 预计算核心工具：`PQ_Entry`（Dijkstra 队列项）、`Cost_Entry`（表项）、代表性代价归约方法 |
| `router_lookahead_simple.h` / `.cpp` | `SimpleLookahead`：只读、纯距离的前瞻 |
| `router_lookahead_compressed_map.h` / `router_lookahead_extended_map.h` | 两个 `map` 变体：稀疏采样 / 更彻底采样 |
| `router_lookahead_constants.h` | 共享常量（如无路径哨兵 `ROUTER_LOOKAHEAD_NO_PATH_SENTINEL`） |
| `vpr/src/route/parallel_connection_router.cpp` | 前瞻在连接路由器中的**消费点**（计算入堆 priority） |
| `vpr/src/base/read_options.cpp` | 命令行选项 `--router_lookahead`、`--read/write_router_lookahead` |
| `vpr/src/base/vpr_api.cpp` | 前瞻对象的生命周期入口（布线开始前获取） |

---

## 4. 核心概念与源码讲解

### 4.1 前瞻代价原理

#### 4.1.1 概念说明

「前瞻」回答一个问题：**当前节点 `node` 到目标节点 `target_node`，估计还要付出多少代价？** 这个估计值被连接路由器用作 A\* 的 \(h(n)\)，让搜索朝目标定向扩展，而不是均匀地向四面八方铺开。

VPR 用一个抽象基类统一所有前瞻实现，核心查询接口只有两个：

- `get_expected_cost(node, target_node, params, R_upstream)`：返回一个合并后的标量代价（延迟代价 + 拥塞代价）。
- `get_expected_delay_and_cong(...)`：返回 `(delay, congestion)` 一对，分别用 `criticality` 和 `1 - criticality` 加权——这样调用方既能合并，也能拆开看延迟与拥塞。

`criticality` 体现了「时序驱动布线」的思想：关键连接（criticality 接近 1）让前瞻偏重延迟；不关键连接（criticality 接近 0）让前瞻偏重拥塞。

#### 4.1.2 核心流程

前瞻在连接路由器入堆时的调用流程（伪代码）：

```
当把路由树上的某个节点 node 准备入堆时：
    backward_path_cost = criticality * node.累计延迟(Tdel)   # 现时项 g(n)
    R_upstream         = node.上游电阻
    expected_cost      = lookahead.get_expected_cost(node, target, params, R_upstream)  # 前瞻项 h(n)
    tot_cost = backward_path_cost + astar_fac * max(0, expected_cost - astar_offset)    # f(n)
    把 (tot_cost, node) 入堆
```

要点：

1. 现时项与前瞻项**相加**构成 A\* 的 \(f(n)\)。
2. `astar_fac` 是前瞻权重（u6-l2 已介绍）；`max(0, ...)` 保证前瞻不会为负。
3. 前瞻必须**只读、O(1)、可被百万次调用**——这正是把「跑搜索」提前到布线前预计算的根本原因。

#### 4.1.3 源码精读

**① 抽象基类定义查询接口。** 基类 `RouterLookahead` 把所有前瞻实现统一起来，`get_expected_cost` 是纯虚函数；注意头文件明确强调「调用前必须先 `compute` 或 `read`」，即表必须先建好才能查：

[router_lookahead.h:14-26](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/router_lookahead/router_lookahead.h#L14-L26) —— `RouterLookahead` 基类，`get_expected_cost` 与 `get_expected_delay_and_cong` 两个查询接口（纯虚）。

**② 基类还定义了生命周期方法。** `compute`（预计算建表）、`compute_intra_tile`（簇内/瓦片内前瞻）、`read` / `write`（序列化读写），每个子类按自身能力实现或抛「未实现」异常：

[router_lookahead.h:28-66](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/router_lookahead/router_lookahead.h#L28-L66) —— 生命周期接口 `compute / compute_intra_tile / read / write` 等，注释说明「未实现时应抛异常」。

**③ 连接路由器中的消费点。** 这是「前瞻项如何进入 A\*」最直接的证据。`add_route_tree_node_to_heap` 计算入堆 priority，`expected_cost` 即 \(h(n)\)：

[parallel_connection_router.cpp:416-444](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/parallel_connection_router.cpp#L416-L444) —— `backward_path_cost` 为现时项，`expected_cost = router_lookahead_.get_expected_cost(...)` 为前瞻项，二者按 `astar_fac` 组合成 `tot_cost` 入堆。

可以看到 `f(n) = g(n) + astar_fac · max(0, h(n) − offset)`，与第 4.1.2 节伪代码完全一致。

**④ 合并 vs 拆分两种返回。** 默认 `MapLookahead::get_expected_cost` 内部其实是先拿到拆分形式再相加；拆分形式让延迟项乘 `criticality`、拥塞项乘 `1 - criticality`，体现时序驱动：

[router_lookahead_map.cpp:184-206](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/router_lookahead/router_lookahead_map.cpp#L184-L206) —— 对 CHAN/OPIN/SOURCE 调 `get_expected_delay_and_cong` 取 `(delay_cost, cong_cost)` 后相加；对 IPIN 直接返回 SINK 的 base_cost。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「关闭前瞻会让连接路由器退化为 Dijkstra（盲目扩散）」，从而理解前瞻项的作用。

**操作步骤（源码阅读型）**：

1. 打开 [parallel_connection_router.cpp:429-430](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/parallel_connection_router.cpp#L429-L430)，确认 `expected_cost` 来自 `router_lookahead_.get_expected_cost(...)`。
2. 设想把 `astar_fac` 改成 0：此时 `tot_cost = backward_path_cost + 0`，priority 完全由现时项决定，搜索只按「已花代价」展开——这正是 Dijkstra。
3. （**待本地验证**）若你已按 u1-l2 完成构建，可对比两种布线：

   ```shell
   # 默认（map 前瞻，定向搜索）
   ./build/vpr/vpr <arch.xml> <circuit.blif> --route_chan_width 100
   # 关闭前瞻权重（退化为 Dijkstra，观察布线时间明显变长）
   ./build/vpr/vpr <arch.xml> <circuit.blif> --route_chan_width 100 --astar_fac 0
   ```

**需要观察的现象**：`--astar_fac 0` 时布线迭代次数与运行时间显著上升（因为搜索不再朝目标定向）。若本地无法运行，则记为「待本地验证」，但源码逻辑已可确认。

**预期结果**：能口述「`astar_fac` 控制前瞻项权重；为 0 时 A\* 退化为 Dijkstra」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `get_expected_cost` 必须是 O(1)？如果它内部也跑一次 Dijkstra 会怎样？

> **答案**：布线一个连接可能入堆上百万次，每次入堆都调用一次前瞻。若前瞻本身是 O(N) 搜索，总复杂度会从「一次 A\*」恶化成「百万次 Dijkstra」，完全不可接受。所以 VPR 把搜索提前到布线前的 `compute` 阶段，查询时只查表。

**练习 2**：`get_expected_delay_and_cong` 返回的延迟项与拥塞项分别乘什么系数？

> **答案**：延迟项乘 `params.criticality`，拥塞项乘 `(1 - params.criticality)`。关键连接偏重延迟，非关键连接偏重拥塞。

---

### 4.2 代价地图类型

#### 4.2.1 概念说明

VPR 提供 6 种可切换的前瞻实现，由枚举 `e_router_lookahead` 描述。它们的差别在于**用什么数据结构估算 h(n)**：从「解析公式（不建表）」到「稀疏采样表」到「稠密采样表」到「只读外部表」，精度与开销依次变化。

| 枚举值 | 实现类 | 怎么算 h(n) | 精度 | 预计算开销 |
| --- | --- | --- | --- | --- |
| `CLASSIC` | `ClassicLookahead` | 解析公式，假设线型均匀 | 低（多线型架构失准） | 无 |
| `MAP` | `MapLookahead` | 稠密 Dijkstra 采样表（**默认**） | 高 | 中高 |
| `COMPRESSED_MAP` | `CompressedMapLookahead` | 稀疏采样表 | 中 | 低 |
| `EXTENDED_MAP` | `ExtendedMapLookahead` | 按连接盒目标更彻底采样 | 更高 | 高 |
| `SIMPLE` | `SimpleLookahead` | 只从文件读一张距离表 | 中（仅通道段） | 无（不能自己算） |
| `NO_OP` | `NoOpLookahead` | 恒返回 0 | 无（等同 Dijkstra） | 无 |

> 注意：枚举定义并不在 `router_lookahead.h`（那只是消费它的地方），而在 `vpr_types.h`。`--router_lookahead` 的默认值是 `map`。

#### 4.2.2 核心流程

工厂函数根据枚举值 new 出对应子类，再决定「预计算」还是「从文件读」：

```
make_router_lookahead(type, write, read, segment_inf, ...)
    └─ make_router_lookahead_object(type)   # 按 type new 子类对象
    └─ if read 为空:  对象.compute(segment_inf)   # 跑预计算建表
       else:          对象.read(read)             # 从文件加载表
    └─ if write 非空: 对象.write(write)           # 顺便把表存盘
    └─ 返回 unique_ptr<RouterLookahead>
```

类型→类的分派逻辑集中在 `make_router_lookahead_object`。

#### 4.2.3 源码精读

**① 枚举定义。** 6 个值都有中文注释说明用途：

[vpr_types.h:100-114](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L100-L114) —— `enum class e_router_lookahead { CLASSIC, MAP, COMPRESSED_MAP, EXTENDED_MAP, SIMPLE, NO_OP }`，注释点明 `MAP` 出自 Oleg Petelin 论文、`COMPRESSED_MAP` 用稀疏采样。

**② 工厂分派。** 一个 `if/else` 链把每个枚举值映射到一个具体子类：

[router_lookahead.cpp:23-45](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/router_lookahead/router_lookahead.cpp#L23-L45) —— `make_router_lookahead_object`：`CLASSIC→ClassicLookahead`、`MAP→MapLookahead`、`COMPRESSED_MAP→CompressedMapLookahead`、`EXTENDED_MAP→ExtendedMapLookahead`、`SIMPLE→SimpleLookahead`、`NO_OP→NoOpLookahead`。

**③ compute 还是 read。** 创建对象后，按 `read_lookahead` 是否为空决定建表还是读表，再按 `write_lookahead` 决定是否存盘：

[router_lookahead.cpp:47-74](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/router_lookahead/router_lookahead.cpp#L47-L74) —— `make_router_lookahead`：`read` 为空调 `compute(segment_inf)`，否则调 `read(file)`；`write` 非空调 `write(file)`。

**④ CLASSIC：不建表的解析公式。** 它假设「到达目标只需若干条与当前同类型的线」，用 `get_expected_segs_to_target` 估算同向/正交方向各需多少段，再乘以 `rr_indexed_data` 里预存的每段 base_cost、T_linear、T_quadratic、C_load 等系数算出延迟与拥塞。延迟里还含 `R_upstream * C_load` 的 RC 项：

[router_lookahead.cpp:82-118](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/router_lookahead/router_lookahead.cpp#L82-L118) —— `ClassicLookahead::get_expected_delay_and_cong`：`cong_cost = 同向段数×base + 正交段数×ortho_base + ipin + sink`；`Tdel` 含线性项、平方项与 `R_upstream·C_load` 项。

正因为它假设线型均匀，对「多种线型混合」的现代架构会失准——这正是 `MAP` 系列被设计出来的原因。

**⑤ MAP：稠密代价表。** `MapLookahead` 持有按位移索引的代价表 `chann_distance_based_min_cost`（通道线）与 `opin_distance_based_min_cost`（OPIN→各处），查询时按 (dx, dy) 直接取表项：

[router_lookahead_map.h:13-48](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/router_lookahead/router_lookahead_map.h#L13-L48) —— `MapLookahead` 类，成员含 `chann_distance_based_min_cost`（`[from_layer][to_layer][dx][dy]`）与 `opin_distance_based_min_cost`（再加 `physical_tile_idx` 维），即「平移不变」的查表结构。

底层表是一个 6 维数组 `[from_layer][to_layer][chan_type][seg_type][dx][dy]`，文档注释详细解释了每一维：

[router_lookahead_map.h:50-70](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/router_lookahead/router_lookahead_map.h#L50-L70) —— `t_wire_cost_map` 即 `NdMatrix<Cost_Entry, 6>`，注释说明平移不变性：代价只随 dx/dy 变化，与绝对坐标无关。

**⑥ EXTENDED_MAP：按连接盒目标的 CostMap。** `extended_map` 用专门的 `CostMap` 类，索引为 `cost_map_[0][segment_index][delta_x][delta_y]`，并把「源线型」与「目标连接盒」分开建模，从而对有专用布线的架构更准：

[router_lookahead_cost_map.h:13-21](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/router_lookahead/router_lookahead_cost_map.h#L13-L21) —— `CostMap` 主表索引说明：`cost_map_[0][segment_index][delta_x][delta_y]`，第一维（曾用于区分 CHANX/CHANY）已被折叠但保留以贴近原始实现。

查询入口 `find_cost(from_seg_index, delta_x, delta_y)` 返回一个 `Cost_Entry`（含 delay 与 congestion）：

[router_lookahead_cost_map.h:39-47](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/router_lookahead/router_lookahead_cost_map.h#L39-L47) —— `CostMap::find_cost`：按线型与 (dx, dy) 查表。

`CostMap` 还要做「填洞」（`fill_holes`）：预计算是相对某个参考坐标做的，跨芯片距离的表项可能没被算到，需要用相邻最近的有效项补齐（`get_nearby_cost_entry`），并对落在包围盒外的位移施加惩罚 `penalty_`。

**⑦ SIMPLE：只读、纯距离。** 它不能自己建表（`compute` 直接抛异常），只能 `read` 一张只涉及通道段的表；查询时按 (dx, dy) 取表项，若无效则返回「无路径」哨兵：

[router_lookahead_simple.h:14-42](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/router_lookahead/router_lookahead_simple.h#L14-L42) —— `SimpleLookahead`：`compute` 抛「不支持，请从文件加载」，`read/write` 是真实实现，无 OPIN/簇内表。

`get_expected_delay_and_cong` 的查询逻辑：通道节点查 `get_wire_cost_entry`，否则返回哨兵值 `ROUTER_LOOKAHEAD_NO_PATH_SENTINEL`：

[router_lookahead_simple.cpp:61-94](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/router_lookahead/router_lookahead_simple.cpp#L61-L94) —— `SimpleLookahead::get_expected_delay_and_cong`：按 `from_seg_index` 与 (dx, dy) 查 `get_wire_cost_entry`，分别乘 `criticality` 与 `1-criticality`。

#### 4.2.4 代码实践

**实践目标**：列出全部前瞻类型，并对比 `cost_map` 类（`MAP`）与 `simple` 类的精度与开销。

**操作步骤（源码阅读型）**：

1. 在 [vpr_types.h:100-114](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L100-L114) 抄下 6 个枚举值。
2. 在 [router_lookahead.cpp:23-45](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/router_lookahead/router_lookahead.cpp#L23-L45) 确认每个值对应的实现类。
3. 填写下面的对比表：

| 维度 | `MAP`（MapLookahead） | `SIMPLE`（SimpleLookahead） |
| --- | --- | --- |
| 能否自己预计算 | 能（`compute` 跑 Dijkstra） | **不能**（`compute` 抛异常，必须 `read` 文件） |
| 表覆盖范围 | 通道线 + OPIN→各处 + 簇内/瓦片内 | 仅通道段（CHANX/CHANY） |
| 多层 3D 架构 | 支持 | **不支持**（`read` 断言 `num_layers == 1`） |
| 精度 | 高（区分线型、含 OPIN/簇内代价） | 中（只有距离，缺 OPIN/簇内） |
| 预计算开销 | 中高（要跑 Dijkstra 采样） | 无（只读） |
| 典型用途 | 默认主力前瞻 | 加载别人算好的表，省去预计算 |

**需要观察的现象**：理解 `SIMPLE` 是 `MAP` 的「轻量子集」——表结构相似（都用 `t_wire_cost_map`/`get_wire_cost_entry`），但 `SIMPLE` 砍掉了 OPIN 表与簇内表，且不能自己生成。

**预期结果**：能口述「`MAP` 精度更高但需预计算；`SIMPLE` 精度较低但只读、零预计算，常用于加载预生成表」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ClassicLookahead` 的 `compute` / `read` / `write` 都是空实现或抛异常？

> **答案**：CLASSIC 不依赖任何预计算表，`h(n)` 直接由解析公式（`get_expected_segs_to_target` + `rr_indexed_data` 系数）实时算出，因此没有表可建、可读、可写。它的「预计算」其实早就在 RR Graph 生成阶段落进了 `rr_indexed_data` 的每段系数里。

**练习 2**：`COMPRESSED_MAP` 相比 `MAP` 用了什么手段降低开销？

> **答案**：稀疏采样（sparse sampling）。`MAP` 在每个参考位置都采样（`sample_all_locs=true`），`COMPRESSED_MAP` 只在芯片上稀疏地选若干位置采样，再用一张「(x,y)→压缩索引」的表把任意位移映射到采样结果。代价是精度略降，换来更短的建表时间与更小的内存占用。

---

### 4.3 预计算与序列化

#### 4.3.1 概念说明

`MAP` 系列前瞻之所以能在布线时 O(1) 查表，靠的是**布线前一次性预计算**。预计算的本质是：在 RR Graph 上从若干「起始线」出发跑 Dijkstra，把「走到 (dx, dy) 处的延迟与拥塞」统计下来，浓缩成一张小表。

「浓缩」分两步：

1. **采样**：因为假设平移不变，不必对芯片上每个节点都跑一遍，只需选若干代表性起始位置（`sample_all_locs` 控制是否全采样）。
2. **归约**：同一个 (dx, dy) 可能被多条路径走到、记录到多个代价，需要归约成一个代表性表项。`e_representative_entry_method` 给出 `FIRST / SMALLEST / AVERAGE / GEOMEAN / MEDIAN` 五种策略（默认取最小延迟项 `SMALLEST`）。

序列化则是把这张来之不易的表存盘复用：同一个架构反复布线时，不必每次都花几十秒重建，用 `--read_router_lookahead` 直接加载即可。VPR 用 Cap'n Proto（`.capnp` / `.bin`）做二进制序列化，用 `.csv` 做人类可读导出。

#### 4.3.2 核心流程

`MapLookahead::compute` 的预计算流程（伪代码）：

```
MapLookahead::compute(segment_inf):
    1. compute_router_wire_lookahead(segment_inf)
         对每个 [线层][线型(CHANX/CHANY/CHANZ)][线段类型]:
             从代表性起始线出发跑 Dijkstra，收集 routing_cost_map
             按代表性方法归约成单个 Cost_Entry，写入 f_wire_cost_map[...][dx][dy]
             fill_in_missing_lookahead_entries(...)   # 补洞
    2. compute_router_src_opin_lookahead()            # OPIN/SOURCE → 各线型的代价
    3. min_chann_global_cost_map(...)                 # 浓缩成 [dx][dy] 最小代价表
    4. min_opin_distance_cost_map(...)                # 浓缩 OPIN 表
    5. (若存在 interposer 切割) 构建 InterposerLookahead
```

Dijkstra 扩展用的优先队列项 `PQ_Entry` 同时携带「向后延迟、上游电阻 R、上游拥塞」，这样每个被扩展到的节点都能记录下完整的代价三元组。

序列化方向：

```
write(file):  .csv → 人类可读导出；.capnp/.bin → 二进制存盘
read(file):   从二进制表加载到内存表（SimpleLookahead 还断言只支持单层）
```

#### 4.3.3 源码精读

**① 模块设计文档。** `router_lookahead_map_utils.h` 顶部注释直接点明了「经典前瞻在多线型架构上的缺陷」与「本模块用预计算表改进」的动机，并引用了 Oleg Petelin 的硕士论文：

[router_lookahead_map_utils.h:1-15](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/router_lookahead/router_lookahead_map_utils.h#L1-L15) —— 注释说明：经典前瞻假设「最少条同型线即可到达」，多线型架构会出问题；本模块预计算 `{CHANX, CHANY}` × 各线型的延迟/拥塞表。

**② Dijkstra 队列项携带三元组。** `PQ_Entry` 在代价之外还存 `delay / R_upstream / congestion_upstream`，扩展时把上游累计值传给子节点，从而能算出含 RC 的延迟：

[router_lookahead_map_utils.h:37-54](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/router_lookahead/router_lookahead_map_utils.h#L37-L54) —— `PQ_Entry`：`cost` 排序用，`delay / R_upstream / congestion_upstream` 是向后传播的代价状态；`operator<` 故意反向以适配最大堆（弹出最小代价）。

**③ 表项与归约方法。** `Cost_Entry` 就是一个 (delay, congestion, fill) 三元组；`e_representative_entry_method` 定义五种把多条记录浓缩成一条的策略：

[router_lookahead_map_utils.h:66-106](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/router_lookahead/router_lookahead_map_utils.h#L66-L106) —— `Cost_Entry` 与归约枚举 `e_representative_entry_method { FIRST, SMALLEST, AVERAGE, GEOMEAN, MEDIAN }`。

`Expansion_Cost_Entry::add_cost_entry` 在 `SMALLEST` 模式下只保留最小延迟项，节省内存（见 [router_lookahead_map_utils.h:130-146](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/router_lookahead/router_lookahead_map_utils.h#L130-L146)）。

**④ compute 编排。** `MapLookahead::compute` 串起「线代价 → OPIN 代价 → 浓缩表」三步，每步都有定时器：

[router_lookahead_map.cpp:422-442](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/router_lookahead/router_lookahead_map.cpp#L422-L442) —— `MapLookahead::compute`：先 `compute_router_wire_lookahead`（通道线表），再 `compute_router_src_opin_lookahead`（OPIN 表），最后 `min_chann_global_cost_map` / `min_opin_distance_cost_map` 浓缩。

线表的构建细节在 `compute_router_wire_lookahead`：对每个 (线层, 线段类型, 通道方向) 调 `get_routing_cost_map(..., sample_all_locs=true, ...)` 跑采样 Dijkstra，再 `set_lookahead_map_costs` 落表、`fill_in_missing_lookahead_entries` 补洞：

[router_lookahead_map.cpp:557-595](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/router_lookahead/router_lookahead_map.cpp#L557-L595) —— 对每线型调 `get_routing_cost_map` 采样、`set_lookahead_map_costs` 归约落表、`fill_in_missing_lookahead_entries` 补洞；`longest_seg_length` 只计 `frequency != 0` 的线段，避免长特殊线把采样推到芯片边缘外。

**⑤ 序列化格式。** `MapLookahead::write` 按扩展名分流：`.csv` 走人类可读导出，`.capnp` / `.bin` 走二进制：

[router_lookahead_map.cpp:486-497](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/router_lookahead/router_lookahead_map.cpp#L486-L497) —— `MapLookahead::write`：`.csv` → `dump_readable_router_lookahead_map`；否则断言 `.capnp`/`.bin` → `write_router_lookahead`。

`SimpleLookahead::read` 加载时断言「只支持单层 2D 架构」，然后调用共享的 `read_router_lookahead`（内部用 Cap'n Proto 反序列化，见 [router_lookahead_simple.cpp:96-101](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/router_lookahead/router_lookahead_simple.cpp#L96-L101)）。

**⑥ 命令行选项。** `--router_lookahead` 控制类型，默认 `map`；`--read_router_lookahead` / `--write_router_lookahead` 控制读写文件：

[read_options.cpp:3334-3351](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L3334-L3351) —— `--router_lookahead` 帮助文本逐项解释 6 种类型，`.default_value("map")`。

[read_options.cpp:2069-2083](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L2069-L2083) —— `--read_router_lookahead`（从文件读，省去计算）与 `--write_router_lookahead`（把表写盘）。

**⑦ 生命周期与缓存。** 布线开始前在 `vpr_route_fixed_W` 里获取前瞻对象；`get_cached_router_lookahead` 以 `(类型, read_lookahead, segment_inf)` 为键在 `RouterContext` 缓存，避免对同一架构重复预计算：

[vpr_api.cpp:1171-1180](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1171-L1180) —— `vpr_route_fixed_W` 开头调 `get_cached_router_lookahead`，传入 `lookahead_type`、`read/write_router_lookahead`、`Segments` 等。

[router_lookahead.cpp:220-252](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/router_lookahead/router_lookahead.cpp#L220-L252) —— `get_cached_router_lookahead`：用 `cache_key` 查 `RouterContext` 缓存，命中直接返回指针，未命中才 `make_router_lookahead` 并 `set` 进缓存。

> 旁注：前瞻不只布线用。布局延迟模型（u5-l3 的 simple delay model）也复用同一前瞻表（见 `PlacementDelayModelCreator.cpp` 调 `get_cached_router_lookahead`），`router_delay_profiling` 则用 `NO_OP` 前瞻做纯 profiling——这正是把前瞻设计成可缓存共享对象的原因。

#### 4.3.4 代码实践

**实践目标**：把默认 `map` 前瞻的代价表导出成人类可读的 `.csv`，直观看到「平移不变的代价地图」长什么样。

**操作步骤**：

1. 确认 `--write_router_lookahead` 的两个分支（见 [router_lookahead_map.cpp:486-497](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/router_lookahead/router_lookahead_map.cpp#L486-L497)）：传 `.csv` 会走可读导出。
2. （**待本地验证**）若已构建，运行：

   ```shell
   ./build/vpr/vpr <arch.xml> <circuit.blif> \
       --route_chan_width 100 \
       --router_lookahead map \
       --write_router_lookahead lookahead.csv
   ```

3. 打开 `lookahead.csv`，观察：表是按 `[from_layer][to_layer][chan_type][seg_type][dx][dy]` 组织的；同一 (seg_type) 下，代价随 dx/dy 增大而增大；不同 seg_type（短线/长线）的代价增长斜率不同——这正是 `map` 比 `classic`（不分线型）准的根源。
4. （**待本地验证**）再用 `.capnp` 存一份，下次同架构直接 `--read_router_lookahead lookahead.capnp` 加载，对比「重新 compute」与「read」的布线启动时间差异。

**需要观察的现象**：`.csv` 能用文本编辑器/表格软件打开，看到代价随距离单调变化；`.capnp` 是二进制、体积小、加载快。

**预期结果**：能口述「预计算把搜索代价浓缩成 (dx,dy) 表；序列化让同架构可复用，省去重复 Dijkstra 采样」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `get_routing_cost_map` 在算 `longest_seg_length` 时要排除 `frequency == 0` 的线段？

> **答案**：`frequency == 0` 的线段（如 scatter-gather 或未用线段）可能非常长。若把它们计入最长长度，采样会从离芯片边缘很远的参考点出发，导致跨芯片距离的表项采集不到、出现大量空洞。排除它们能让采样更贴近实际布线距离。

**练习 2**：缓存键为什么是 `(类型, read_lookahead, segment_inf)` 三元组？

> **答案**：前瞻表的内容由这三者唯一决定：类型决定算法、`read_lookahead` 决定是否从文件加载（加载哪份表）、`segment_inf`（线段定义）决定表的数值。三者相同即可复用同一张表，避免对同一架构在最小通道宽度二分搜索等反复布线场景中重复预计算。

**练习 3**：`NO_OP` 前瞻恒返回 0，它会出现在什么场景？

> **答案**：用于「不需要前瞻引导」的纯 profiling，例如 `router_delay_profiling` 只想测量延迟分布、不希望前瞻影响结果，于是用 `NO_OP`（见 `router_delay_profiling.cpp` 调 `make_router_lookahead(..., e_router_lookahead::NO_OP, ...)`）。此时 A\* 退化为 Dijkstra。

---

## 5. 综合实践

**任务**：为同一架构分别用 `classic`、`map`、`compressed_map` 三种前瞻跑布线，对比「预计算耗时、布线耗时、最终关键路径延迟」三项指标，把结论写进一张表。

**步骤**：

1. 选一个中等规模电路与一个多线型架构（如 `vtr_flow/arch` 下含长短线的架构文件）。
2. （**待本地验证**）依次运行（已按 u1-l2 构建 `./build/vpr/vpr`）：

   ```shell
   for LA in classic map compressed_map; do
       ./build/vpr/vpr <arch.xml> <circuit.blif> \
           --route_chan_width 100 --router_lookahead $LA \
           > log_${LA}.txt 2>&1
   done
   ```

3. 从日志中提取三项指标：
   - 预计算耗时：日志里 `Computing router lookahead map` 计时行（`ScopedStartFinishTimer`）。
   - 布线耗时：`Routing` 计时行。
   - 关键路径延迟：布线后时序报告的最终 `critical path delay`。
4. 回答：多线型架构上 `classic` 是否在延迟上明显劣于 `map`？`compressed_map` 的预计算是否比 `map` 快、而延迟损失是否可接受？

**若无法本地运行**：则把本任务降级为源码阅读——只比较三者的 `compute` 实现与表结构（参考 4.2 与 4.3 的源码链接），推导出预期的「精度/开销」排序，并标注「指标待本地验证」。

> 这个任务把三个最小模块串起来：你既要理解前瞻在 A\* 中的作用（4.1），又要清楚三种代价地图的表结构差异（4.2），还要看得懂预计算计时与序列化（4.3）。

## 6. 本讲小结

- **前瞻 = A\* 的 h(n)**：连接路由器入堆时 `tot_cost = backward_path_cost + astar_fac · max(0, expected_cost − offset)`；前瞻必须 O(1) 查表，所以 VPR 把搜索提前到布线前预计算。
- **统一接口**：抽象基类 `RouterLookahead` 定义 `get_expected_cost` / `get_expected_delay_and_cong`（后者按 `criticality` 拆延迟与拥塞）以及 `compute / read / write` 生命周期方法。
- **六种类型**：`e_router_lookahead` 分 `CLASSIC`（解析公式，不建表）/ `MAP`（稠密采样表，默认）/ `COMPRESSED_MAP`（稀疏采样）/ `EXTENDED_MAP`（按连接盒目标更彻底采样，用 `CostMap`）/ `SIMPLE`（只读距离表）/ `NO_OP`（恒 0）。工厂 `make_router_lookahead_object` 分派。
- **预计算靠 Dijkstra 采样 + 归约**：从代表性起始线出发跑 Dijkstra，把 (dx, dy) 的代价浓缩成单个 `Cost_Entry`（默认取最小延迟），再补洞；依赖「平移不变性」假设使表很小。
- **序列化与缓存**：表可用 `.capnp/.bin` 二进制或 `.csv` 可读存盘，由 `--read/write_router_lookahead` 控制；`get_cached_router_lookahead` 以 `(类型, read, segment_inf)` 为键在 `RouterContext` 缓存，供布线与布局延迟模型共享。
- **精度/开销权衡主线**：从 `CLASSIC`（零开销、低精度）到 `MAP`（中开销、高精度）到 `EXTENDED_MAP`（高开销、更高精度），`SIMPLE` 则用「只读」换「零预计算」。

## 7. 下一步学习建议

- 本讲聚焦「前瞻如何估 h(n)」。要看完连接路由器的完整代价函数，回到 u6-l2 复习现时项（`backward_path_cost`、criticality、拥塞代价 `base×acc×pres`）如何与前瞻项组合。
- 接下来建议进入 u6-l3（基于连接的路由与路由树），看前瞻如何与「路由树复用、增量重布线」协同——前瞻为单连接定向，路由树决定从哪些已布线段出发。
- 对「前瞻表的数值准不准」感兴趣的读者，可阅读 `router_lookahead_report.cpp` 与 `--generate_router_lookahead_report` 选项：它会生成 `report_router_lookahead.rpt`，把前瞻估计值与真实布线代价逐点比对，是调架构/调前瞻类型时的利器。
- 若想理解布局为何也能用路由前瞻，回顾 u5-l3（布局代价与延迟模型）中 `simple_delay_model` 复用 `MAP` 前瞻表的设计。
