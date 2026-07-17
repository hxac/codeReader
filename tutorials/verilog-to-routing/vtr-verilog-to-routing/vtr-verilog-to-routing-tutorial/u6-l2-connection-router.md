# 连接路由器与堆结构

## 1. 本讲目标

本讲深入 VPR 布线阶段最核心的算法部件——**连接路由器（Connection Router）**。学完后你应当能够：

- 说清楚「迷宫布线（maze routing）」与「堆（优先队列）」之间是什么关系，以及它如何退化成 Dijkstra、又如何升级成 A\*；
- 复述一个 RR 节点被压入堆时，它的代价（priority）由哪几部分相加而成：现时项（backward cost）+ 前瞻项（lookahead）；
- 区分拥塞代价、延迟代价、拐弯代价，以及 `criticality` 如何在「时序」与「可布线性」之间做加权;
- 看懂堆数据结构的可插拔设计：`HeapNode`、`HeapInterface`、`DAryHeap`，以及为什么 VPR 默认用 4 叉堆而非二叉堆；
- 知道如何通过 `--router_heap` 命令行参数切换堆类型，并理解切换对性能与质量的影响。

本讲承接 u6-l1（路由资源图 RR Graph）。RR Graph 给出了「能走哪些节点、哪些边」；连接路由器回答的是「在这张图上，从一个源到一个汇，怎么走出代价最低的一条路」。

## 2. 前置知识

在进入源码前，先用最朴素的语言把三个概念讲清楚。

**图上的最短路问题。** 把 FPGA 上的可编程布线结构看成一张有向图：节点是路由资源（导线段、引脚、虚拟源汇），边是开关。布线就是把一条「线网（net）」从它的驱动端（source）连到每一个接收端（sink）。对其中任意一个「源 → 汇」的连接，我们要找一条代价最小的路径。这本质上是图上的最短路问题。

**Dijkstra 与迷宫布线。** 经典的 Dijkstra 算法用一个**最小优先队列（堆）**：每次从堆里取出当前代价最小的节点进行扩展，扩展时把它的邻居压回堆。这样一圈圈扩散、像洪水泛滥一样，直到碰到目标——这正是 FPGA 领域所说的「迷宫布线（maze routing / Lee 算法）」。所以「迷宫布线」≈「在 RR Graph 上跑 Dijkstra」。

**从 Dijkstra 升级到 A\*。** Dijkstra 向四面八方均匀扩散，会浪费大量扩展在不相关方向上的节点。A\* 算法的关键改进是加一个**启发式的前瞻项（heuristic）**：估算「从当前节点到目标还剩多少代价」，把它加进总代价里。这样堆会优先扩展「既便宜、又离目标近」的节点，搜索朝目标定向收敛，大幅减少扩展数。VPR 的连接路由器默认就是 A\*（迷宫 + 前瞻），把前瞻关闭（`astar_fac = 0`）就退化成纯 Dijkstra。

> 关键直觉：**堆是迷宫布线的发动机**。布线质量的差别，往往就体现在「代价函数怎么算」和「堆用什么结构实现」这两件事上。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `vpr/src/route/connection_router_interface.h` | 抽象接口 `ConnectionRouterInterface` 与代价参数结构体 `t_conn_cost_params`，定义连接路由器对外契约。 |
| `vpr/src/route/connection_router.h` | 模板类 `ConnectionRouter<HeapImplementation>`，把「堆」作为模板参数，封装串/并行路由器共有的成员与辅助函数。 |
| `vpr/src/route/connection_router.tpp` | 上述模板的实现，含代价计算 `evaluate_timing_driven_node_costs` 与命中判定主入口。 |
| `vpr/src/route/serial_connection_router.cpp` | 串行连接路由器的核心迷宫搜索循环 `timing_driven_find_single_shortest_path_from_heap` 及扩展/入堆细节。 |
| `vpr/src/route/serial_connection_router.h` | `SerialConnectionRouter<Heap>` 子类声明，以及工厂 `make_serial_connection_router`。 |
| `vpr/src/route/heap_type.h` | `HeapNode`、比较器、抽象 `HeapInterface`、堆类型枚举 `e_heap_type` 与工厂 `make_heap`。 |
| `vpr/src/route/d_ary_heap.h` | D 叉堆实现 `DAryHeap<D>`，及 `BinaryHeap`、`FourAryHeap` 别名。 |
| `vpr/src/route/route_common.h` | 单节点拥塞代价公式 `get_single_rr_cong_cost` 与每节点路由状态 `t_rr_node_route_inf`。 |

> 说明：规格里提到「主搜索循环在 `connection_router.h`」。严格地说，`connection_router.h` 只声明了接口与模板成员，**真正的 `while` 主循环在 `serial_connection_router.cpp` 的 `timing_driven_find_single_shortest_path_from_heap` 中**；`connection_router.tpp` 的 `timing_driven_route_connection_from_heap` 负责调用它。本讲会把两者都指给你看。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：迷宫布线搜索、代价函数、堆数据结构。

### 4.1 迷宫布线搜索：从堆里弹、向邻居扩

#### 4.1.1 概念说明

连接路由器的任务很单一：给定一个**已经存在的路由树（route tree）的根** `rt_root` 和一个**目标汇节点** `sink_node`，在 RR Graph 上找出从路由树到汇的最低代价路径，并把路径回溯信息写进 `rr_node_route_inf` 供后续回溯。

注意三个设计要点：

1. **源不是单点，而是「整棵已有路由树」**。因为 VPR 采用协商式（Pathfinder）布线，同一个 net 之前布过的线段会被复用——路由树上每一个允许重新扩展的节点都可以作为这次连接的起点。这就是「增量布线」的基础（详见 u6-l3）。
2. **搜索是 A\*，不是纯 Dijkstra**。代价里含前瞻项，朝目标定向搜索。
3. **命中即停**。一旦从堆里弹出的节点就是 `sink_node`，立即停止——因为堆保证弹出的是当前全局最小代价节点，第一个到达 sink 的路径就是最优的。

接口层 `ConnectionRouterInterface` 把这一切抽象成一个纯虚函数（连接路由器对外的核心入口）：

[connection_router_interface.h:54-60](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/connection_router_interface.h#L54-L60) — 连接路由器最核心的对外方法：从路由树 `rt_root` 找到 `sink_node` 的路径，返回三元组（路径是否存在 / 是否需要用全器件包围盒重试 / 命中的汇节点）。

#### 4.1.2 核心流程

主搜索循环可以概括成下面的伪代码（省略统计、调试日志与 RCV 分支）：

```
function timing_driven_route_connection_from_route_tree(rt_root, sink_node, cost_params, bb):
    把整棵路由树中「可重扩展」的节点按代价压入堆          # 见 4.2 的代价
    build_heap()                                        # 批量建堆
    if 堆为空: 返回 (不存在路径)

    timing_driven_find_single_shortest_path_from_heap(sink_node, cost_params, bb):
        while try_pop(cheapest):                        # 弹出当前最小代价节点 inode
            if inode == sink_node: break                # 命中目标，停止
            timing_driven_expand_cheapest(inode, cheapest.cost, sink_node, ...)
                └─ 若 inode 的弹出代价 == 其存储的最优代价（否则是过期堆项，跳过）:
                     timing_driven_expand_neighbours(current, ...)
                        └─ 对每条出边 to_node:
                             BB 剪枝 + 目标 IPIN 剪枝
                             timing_driven_add_to_heap(...)   # 算代价、预剪枝、压堆

    if rr_node_route_inf[sink_node].path_cost 仍为无穷:
        返回 (不存在路径, 可能要求用全器件包围盒重试)
    else:
        返回 (找到, 回溯边 prev_edge)
```

这里有**两层剪枝**是理解迷宫布线效率的关键：

- **堆后剪枝（lazy / post-heap pruning）**：堆里可能同时存在多个指向同一节点的过期条目（先压了一个较贵的路径，后来又压了更便宜的）。弹出时把「弹出的代价」与「该节点当前记录的最优 `path_cost`」比较，若不相等，说明这是个过期条目，直接跳过、不再扩展。
- **堆前剪枝（pre-heap pruning）**：扩展邻居算出新代价后，只有当新路径比该节点已知的最好代价更优，才真正压入堆，从源头限制堆的膨胀。

#### 4.1.3 源码精读

主循环本身非常短，是整个连接路由器最该先读的地方：

[serial_connection_router.cpp:21-55](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/serial_connection_router.cpp#L21-L55) — 迷宫布线主循环：不断 `try_pop` 出最小代价节点，命中 `sink_node` 即 `break`，否则调用 `timing_driven_expand_cheapest` 继续向外扩散。

`try_pop` 失败（堆空）意味着源与汇在图上不连通，是「硬失败」。命中的判断用 `inode == sink_node`，并 `break`。

`timing_driven_expand_cheapest` 实现的就是堆后剪枝——只有当弹出的 `new_total_cost` 恰好等于该节点存储的 `path_cost` 时才真正扩展：

[serial_connection_router.cpp:147-178](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/serial_connection_router.cpp#L147-L178) — 堆后剪枝：用 `new_total_cost` 作为「身份标识」，判断弹出的 (节点, 代价) 是否仍是该节点最近一次压入的最优条目；否则视为过期，跳过邻居扩展以减少冗余工作。

真正向外扩展邻居时，有一个值得注意的**预取（prefetch）优化**：

[serial_connection_router.cpp:216-233](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/serial_connection_router.cpp#L216-L233) — 先遍历一遍所有出边，预取目标 RR 节点数据与开关数据，再正式扩展。注释指出这在 Titan 大电路上能减少约 6–8% 的墙钟时间。

邻居扩展 `timing_driven_expand_neighbour` 做两件事：包围盒（BB）剪枝、以及「只保留通向目标块的 IPIN」剪枝：

[serial_connection_router.cpp:251-297](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/serial_connection_router.cpp#L251-L297) — 两类剪枝：节点走出 net 的包围盒则剪掉；节点是 IPIN 但不落在目标块的包围盒内也剪掉（这同时让「穿块而过」的 route-through 变不可能）。

最后，`timing_driven_add_to_heap` 计算代价、做堆前剪枝，并真正压堆：

[serial_connection_router.cpp:346-384](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/serial_connection_router.cpp#L346-L384) — 堆前剪枝与压堆：仅当新路径的 `backward_cost` 优于已知最优时，才更新 `rr_node_route_inf` 并把 `{new_total_cost, to_node}` 压入堆；否则不扩展。

回到入口侧，`timing_driven_route_connection_from_heap`（在模板实现里）负责计算目标包围盒并启动搜索，同时累计路径搜索耗时：

[connection_router.tpp:164-205](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/connection_router.tpp#L164-L205) — 搜索前置：为 sink 求出目标块包围盒 `target_bb`（供 IPIN 剪枝用），计时后调用纯虚 `timing_driven_find_single_shortest_path_from_heap`，把真正的循环交给具体子类（串行实现见上）。

注意它调用的 `timing_driven_find_single_shortest_path_from_heap` 在基类是**纯虚函数**，由 `SerialConnectionRouter` 提供具体实现。这正是「接口与实现分离、串/并行共享同一套骨架」的设计。

#### 4.1.4 代码实践

**实践目标**：在源码中亲眼看一次「弹 → 命中判定 → 扩展」的循环，确认搜索停止条件。

**操作步骤（源码阅读型实践）**：

1. 打开 `vpr/src/route/serial_connection_router.cpp`，定位 `timing_driven_find_single_shortest_path_from_heap`（约第 13–56 行）。
2. 找到 `while (this->heap_.try_pop(cheapest))` 这一行，记下它。
3. 在循环体里找到「命中目标就 `break`」的判断（`if (inode == sink_node)`）。
4. 追一步 `timing_driven_expand_cheapest`，找到其中 `if (best_total_cost == new_total_cost)` 的堆后剪枝判断。

**需要观察的现象**：循环里**没有任何对「已访问节点集合」的维护**——重复访问同一个节点是被允许的，重复扩展则靠堆后剪枝阻止。

**预期结果**：你会确认 VPR 的迷宫布线不需要显式 `visited` 标记，而是靠「弹出代价 == 存储最优代价」这一惰性判定来避免对过期堆条目的重复扩展。这是一个比教科书 Dijkstra 更节省内存的实现技巧。

> 运行行为相关结论属于「待本地验证」：若想真实观察循环次数，可在 `timing_driven_find_single_shortest_path_from_heap` 内临时加一行计数日志，用一个小电路（如 quickstart 的 blink）跑 `run_vtr_flow.py` 后查看统计输出。

#### 4.1.5 小练习与答案

**练习 1**：为什么主循环命中 `sink_node` 后可以直接 `break`，而不用继续弹出更小的节点？
**答案**：堆保证每次 `try_pop` 出的都是当前全局最小代价节点；既然这个最小代价节点已经是目标 sink，不可能再有比它更小的到达 sink 的路径了，因此第一条命中即最优。

**练习 2**：`timing_driven_expand_cheapest` 中 `best_total_cost == new_total_cost` 的判断若被去掉，搜索结果会变错吗？为什么 VPR 仍要保留它？
**答案**：结果一般不会变错（最终仍会找到最优路径，因为更优路径已被记录），但会做大量冗余的邻居扩展——堆里残留的过期条目都会被重新展开一遍，严重拖慢布线。该判断是**性能优化**而非正确性所必需。

**练习 3**：接口注释说「`astar_fac = 0` 时等价于 Dijkstra」（见 `connection_router_interface.h` 第 86–89 行）。请解释原因。
**答案**：代价公式为 `total_cost = backward_path_cost + astar_fac × max(0, expected_cost − offset)`。当 `astar_fac = 0` 时前瞻项消失，`total_cost` 退化为纯已知代价，搜索就变成均匀代价搜索（Dijkstra）；注释还建议此时配套使用 `NoOpLookahead`，因为既然前瞻不参与代价，再算它纯属浪费。

### 4.2 代价函数：现时项 + 前瞻项

#### 4.2.1 概念说明

迷宫布线质量好坏，几乎全看代价函数。VPR 给每个被扩展的 RR 节点算两个代价：

- **现时项 `backward_path_cost`**：从起点（路由树某节点）沿当前路径走到「这个节点」的**已知**累积代价。它由拥塞代价、延迟代价、拐弯代价三部分加权而成，是已经真实发生、不会再变的成本。
- **前瞻项 `expected_cost`**：由路由前瞻（Router Lookahead，详见 u6-l4）给出的「从这个节点到目标 sink 的**估计**剩余代价」。它只是个估计值，用来引导搜索方向。

最终压入堆的 priority 是二者之和（A\* 形式）：

\[
\text{total\_cost} = \text{backward\_path\_cost} + \text{astar\_fac} \times \max(0,\ \text{expected\_cost} - \text{astar\_offset})
\]

`astar_fac`（默认 1.2）是前瞻项的权重：越大，搜索越「贪心」地直奔目标（扩展少、速度快，但可能错过更优路径）；越小，越接近 Dijkstra（更精确、但更慢）。

现时项内部又有时序与拥塞的权衡，由 `criticality`（关键度）控制：

\[
\text{backward\_path\_cost} \mathrel{+}= (1-\text{criticality}) \times \text{cong\_cost} + \text{criticality} \times T_{\text{del}}
\]

- `criticality` 接近 1：这条连接时序很关键，代价几乎只看延迟 `Tdel`；
- `criticality` 接近 0：这条连接时序不关键，代价几乎只看拥塞 `cong_cost`，鼓励它绕开拥挤区域。

所有这些参数都集中在一个结构体里：

[connection_router_interface.h:19-33](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/connection_router_interface.h#L19-L33) — 代价参数 `t_conn_cost_params`：`criticality`、`astar_fac`、`astar_offset`、`bend_cost`、`pres_fac` 等都在此，并给出默认值（如 `astar_fac` 默认 1.2）。

#### 4.2.2 核心流程

单个邻居节点代价的计算流程（`evaluate_timing_driven_node_costs`）：

```
输入: from_node（当前节点）, to_node（邻居）, target_node（目标 sink）
1. 取 from→to 这条边用的开关 iswitch
   - switch_buffered / reached_configurably / switch_R / switch_Cinternal
2. 更新上游电阻 R_upstream:
   - 若开关带缓冲: R_upstream = 0（隔离了上游）
   - 否则: R_upstream += switch_R + node_R   （保留上游电阻，供 Elmore 延迟用）
3. 计算本节点延迟 Tdel = get_rr_node_delay_cost(to_node, prev_edge)
   - 并按开关内部电容对上游做延迟修正: Tdel += Rdel_adjust × switch_Cinternal
4. 计算拥塞代价 cong_cost:
   - 可配置到达: cong_cost = get_rr_cong_cost(to_node, pres_fac)
   - 不可配置到达（同属一个 non-config 节点集）: cong_cost = 0  （集合代价只在首次进入时计一次）
5. 累加现时项 backward_path_cost:
   - += (1 - criticality) × cong_cost      （拥塞项）
   - += criticality × Tdel                 （延迟项）
   - 若发生 CHANX↔CHANY 拐弯: += bend_cost （拐弯项）
6. 算总代价 total_cost:
   - expected_cost = router_lookahead_.get_expected_cost(to_node, target_node, ...)
   - total_cost = backward_path_cost + astar_fac × max(0, expected_cost - astar_offset)
```

其中单节点拥塞代价是 Pathfinder 协商式布线的核心——它让拥挤的节点越来越贵，迫使后续 net 绕行：

\[
\text{cong\_cost} = \text{base\_cost} \times \text{acc\_cost} \times \text{pres\_cost}
\]

\[
\text{pres\_cost} = \begin{cases} 1 & \text{若未过载（overuse} < 0\text{）} \\ 1 + \text{pres\_fac} \times (\text{overuse}+1) & \text{若过载} \end{cases}
\]

`acc_cost` 是历史拥塞累积（跨多轮 Pathfinder 迭代），`pres_cost` 是当前轮次的即时拥塞惩罚。

#### 4.2.3 源码精读

代价计算的主体在模板实现里，注释把 backward / total / R_upstream 三个量的含义讲得很清楚：

[connection_router.tpp:265-272](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/connection_router.tpp#L265-L272) — 三个代价量的语义注释：`backward_cost` 是已知部分（已走过的拥塞+延迟），`total_cost` 是已知部分 + 到目标的估计，`R_upstream` 是到该节点为止的上游电阻。

拥塞项与延迟项的加权累加，以及拐弯代价：

[connection_router.tpp:340-350](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/connection_router.tpp#L340-L350) — 现时项的累加：`(1-criticality)×cong_cost` 为拥塞项、`criticality×Tdel` 为延迟项；当 `bend_cost != 0` 且发生 CHANX↔CHANY 拐弯时再加 `bend_cost`。

总代价（A\* 形式）的组装：

[connection_router.tpp:362-372](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/connection_router.tpp#L362-L372) — 总代价 `total_cost = backward_path_cost + astar_fac × max(0, expected_cost − astar_offset)`，其中 `expected_cost` 来自 `router_lookahead_`（前瞻，u6-l4 详述）。

单节点拥塞代价公式：

[route_common.h:134-157](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_common.h#L134-L157) — `get_single_rr_cong_cost`：`cost = base_cost × acc_cost × pres_cost`，其中 `pres_cost` 在过载时为 `1 + pres_fac×(overuse+1)`、未过载时为 1。

每个 RR 节点在搜索期间的路由状态（这些字段就是上面公式读写的对象）：

[route_common.h:17-53](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_common.h#L17-L53) — `t_rr_node_route_inf`：`prev_edge`（回溯用边）、`acc_cost`（历史拥塞）、`path_cost`（含前瞻的总代价，即堆 priority 的对照基准）、`backward_path_cost`（已知代价）、`R_upstream`（上游电阻）、`occ_`（当前占用）。

#### 4.2.4 代码实践

**实践目标**：回答规格里的问题——「每个 RR 节点入堆时考虑的代价由哪几部分组成」。

**操作步骤（源码阅读型实践）**：

1. 在 `connection_router.tpp` 的 `evaluate_timing_driven_node_costs` 中，找到三处给 `backward_path_cost` 累加的 `+=`（拥塞项、延迟项、拐弯项）。
2. 找到组装 `total_cost` 的那一行，确认前瞻项 `expected_cost` 来自 `router_lookahead_`。
3. 回到 `serial_connection_router.cpp` 的 `timing_driven_add_to_heap`，确认真正压堆的是 `{new_total_cost, to_node}`（即把上面算出的 `total_cost` 当作堆 priority）。

**需要观察的现象**：注意 `reached_configurably` 为假（不可配置边）时 `cong_cost` 被置 0 的分支——同一组「不可配置节点集」的拥塞代价只在首次进入时计一次。

**预期结果**：你能写出一条完整公式——一个 RR 节点入堆时的 priority 为

\[
\underbrace{(1-c) \cdot C_{\text{cong}} + c \cdot T_{\text{del}} + C_{\text{bend}}}_{\text{backward\_path\_cost}} + \text{astar\_fac} \cdot \max(0,\ \text{expected} - \text{offset})
\]

其中 `c` 是 `criticality`。这就是「现时项（拥塞+延迟+拐弯）+ 前瞻项」。

> 想观察拥塞代价如何随迭代上涨：可在 `get_single_rr_cong_cost` 返回前打印 `pres_cost` 与 `acc_cost`。具体数值「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么拥塞项乘以 `(1 - criticality)`、延迟项乘以 `criticality`，而不是反过来？
**答案**：`criticality` 表示该连接的时序关键程度。关键连接（`criticality` 大）应优先选低延迟路径，所以延迟项权重大、拥塞项权重小；不关键连接则应让出低延迟资源、优先避开拥挤，所以拥塞项权重大。这样把「稀缺的低延迟资源」分配给真正需要它的连接。

**练习 2**：`astar_fac` 设得非常大（比如 100）会怎样？
**答案**：前瞻项主导代价，搜索会几乎「贪心」地直奔目标，扩展节点数锐减、布线速度变快；但容易陷入局部最优、错过真正最短路径，导致布线质量（时序/线长）下降。极端地，`astar_fac → ∞` 退化为类似「最佳优先」的贪心搜索，不再保证最优。

**练习 3**：不可配置边（non-configurable edge）对应的邻居为什么 `cong_cost = 0`？
**答案**：不可配置边连接的多个节点属于同一个「不可配置节点集」，一旦路径进入这个集合，它们必然被一起占用。为避免重复计费，集合的拥塞代价只在路径首次进入该集合时计一次（在那次扩展里已经计入），后续集内邻居都置 0。

### 4.3 堆数据结构：可插拔的优先队列

#### 4.3.1 概念说明

迷宫布线每一步都在「取最小 + 插入」，这是一个典型的优先队列负载。VPR 把堆设计成**可插拔**的：连接路由器是一个模板类 `ConnectionRouter<HeapImplementation>`，堆是它的模板参数。换堆类型不需要改路由器逻辑，只需换模板实参。

堆的最小工作单元是 `HeapNode`，刻意做得很小：

[heap_type.h:20-26](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/heap_type.h#L20-L26) — `HeapNode` 只有 `prio`（float，代价）和 `node`（RRNodeId）两个字段，共 8 字节；并用 `static_assert` 保证 `RRNodeId` 是 32 位，从而整个节点恰好 64 位，缓存友好。

为什么死磕「8 字节」？因为现代 CPU 一条缓存线通常是 64 字节，能正好塞下 8 个 `HeapNode`。堆操作（父子比较、兄弟比较）的高度局部性会让这种紧凑布局吃到缓存红利——这正是 VPR 偏好「D 叉堆」的根本原因。

#### 4.3.2 核心流程

堆的抽象接口 `HeapInterface` 定义了路由器用到的全部操作：

[heap_type.h:40-106](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/heap_type.h#L40-L106) — `HeapInterface`：`init_heap`（按器件尺寸预分配）、`try_pop`（弹最小）、`add_to_heap`（保持堆序地插入）、`push_back` + `build_heap`（批量插入后一次性建堆）、`empty_heap` / `is_empty_heap`。

注意它区分了两类插入：

- `add_to_heap`：单插入，**保持**堆性质（用于主循环里逐个邻居入堆）；
- `push_back` + `build_heap`：先批量无序插入，最后一次性 `build_heap`（下沉建堆）。后者用于「把整棵路由树重新压堆」这种大批量场景，效率更高。你在 `connection_router.tpp` 的 `timing_driven_route_connection_common_setup` 里能看到 `add_route_tree_to_heap(...)` 之后紧跟 `heap_.build_heap()` 的用法。

具体实现是 D 叉堆 `DAryHeap<D>`，用 STL 风格的比较器实现最小堆：

[d_ary_heap.h:20-73](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/d_ary_heap.h#L20-L73) — `DAryHeap<D>`：D 叉最小堆，`init_heap` 按 `(width-1)×(height-1)` 预留容量；末尾给出 `BinaryHeap = DAryHeap<2>` 与 `FourAryHeap = DAryHeap<4>` 两个别名。

二叉堆 vs 四叉堆的取舍，源码注释讲得很透彻（也解释了为什么默认选四叉）：

- 四叉堆树高更低（log₄N < log₂N），单次 pop/push 的比较轮数更少；
- 8 字节的 `HeapNode` × 8 恰好一条缓存线，四叉堆父子/兄弟比较都落在同一缓存线内，缓存命中率高；
- 实测在大型 Koios 基准电路上，四叉堆比二叉堆快约 5%。

堆类型用枚举表达，并提供工厂函数：

[heap_type.h:108-117](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/heap_type.h#L108-L117) — `e_heap_type` 枚举（`INVALID_HEAP`/`BINARY_HEAP`/`FOUR_ARY_HEAP`）与工厂 `make_heap`，把枚举值映射到具体堆实例。

#### 4.3.3 源码精读

连接路由器把堆作为模板参数持有，这是「可插拔」的落点：

[connection_router.h:37-38](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/connection_router.h#L37-L38) — `template<typename HeapImplementation> class ConnectionRouter`，堆是模板参数。

[connection_router.h:346-347](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/connection_router.h#L346-L347) — 成员 `HeapImplementation heap_;` 注释点明它可以是二叉堆、4 叉堆或基于 MultiQueue 的并行堆。

堆类型的选择入口（规格要求比较的「选择入口」）在两个层面：

1. **命令行层**：`--router_heap` 选项，默认 `four_ary`：

[read_options.cpp:3417-3427](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L3417-L3427) — `--router_heap` 选项注册，`.default_value("four_ary")`，故 VPR 默认用四叉堆。

2. **工厂层**：`make_serial_connection_router` 按 `e_heap_type` 实例化对应模板实参的 `SerialConnectionRouter`：

[serial_connection_router.cpp:470-492](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/serial_connection_router.cpp#L470-L492) — `switch (heap_type)`：`BINARY_HEAP` 构造 `SerialConnectionRouter<BinaryHeap>`，`FOUR_ARY_HEAP` 构造 `SerialConnectionRouter<FourAryHeap>`，未知值报致命错。

> 一个需要诚实指出的细节：`--router_heap` 的帮助文本与 `ParseRouterHeap::default_choices()` 里还列了一个 `"bucket"`（桶堆），但 [read_options.cpp:1370-1379](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L1370-L1379) 的 `from_str` 只识别 `"binary"` 与 `"four_ary"`，且 `e_heap_type` 枚举与 `make_heap` 工厂（[heap_type.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/heap_type.cpp)）都只处理这两种。也就是说 `bucket` 目前是「帮助文本里有、代码里未落地」的条目——若你传入 `--router_heap bucket`，实际会在解析时报错。阅读时以枚举与工厂为准。

#### 4.3.4 代码实践

**实践目标**：比较不同堆类型，并找到堆类型的选择入口。

**操作步骤**：

1. 打开 `vpr/src/route/heap_type.h`，找到 `e_heap_type` 枚举与 `make_heap` 工厂，确认目前只有二叉与四叉两种。
2. 打开 `vpr/src/route/d_ary_heap.h`，阅读顶部注释关于「四叉堆更快」的解释。
3. 打开 `vpr/src/base/read_options.cpp` 第 3417 行附近，确认 `--router_heap` 默认值是 `four_ary`。
4. 打开 `vpr/src/route/serial_connection_router.cpp` 的 `make_serial_connection_router`，看清枚举到模板实例的 `switch`。
5. **可选实验（待本地验证）**：用 `run_vtr_flow.py` 跑同一个电路两次，分别加 `--router_heap binary` 与默认（`four_ary`），对比布线阶段的耗时（可看 VPR 输出里 `Serial Connection Router ... Time spent on path search` 这一行，由析构函数打印，见 `serial_connection_router.h` 第 26–29 行）。

**需要观察的现象**：四叉堆版本在大电路上路径搜索累计时间应更短；在小电路上两者差异可忽略（与 `d_ary_heap.h` 注释一致）。

**预期结果**：你能画出「命令行 `--router_heap` → `ParseRouterHeap` 解析成 `e_heap_type` → `make_serial_connection_router` 的 switch → `SerialConnectionRouter<BinaryHeap|FourAryHeap>`」这条完整选择链。

#### 4.3.5 小练习与答案

**练习 1**：为什么把堆设计成 `ConnectionRouter` 的模板参数，而不是一个基类指针？
**答案**：迷宫布线的内层循环极其频繁地调用 `try_pop`/`add_to_heap`，模板参数让这些调用在编译期**单态化（monomorphization）**、可被内联，零虚函数开销；而基类指针会引入每次调用的虚分派成本。对热路径，模板是更优选择。

**练习 2**：`init_heap` 为什么要按 `(width-1)×(height-1)` 预留容量？
**答案**：单层器件上 RR 节点数与网格面积同量级，预留容量避免在搜索中反复 `std::vector` 扩容（扩容会触发整块拷贝）。这只是 `reserve`（预留空间），不改变堆的大小。

**练习 3**：除了 `BinaryHeap` 和 `FourAryHeap`，VPR 还有没有别的堆实现？它如何与连接路由器结合？
**答案**：有。并行布线路径用的是基于 MultiQueue 的并行堆（`multi_queue_d_ary_heap.h`），它同样实现 `HeapInterface`，从而能作为 `ConnectionRouter` 的另一个模板实参，由 `ParallelConnectionRouter` 使用（详见 u8-l5）。这就是「可插拔堆」设计的红利——同一套路由器骨架能搭配串行堆或并行堆。

## 5. 综合实践

把三个模块串起来，做一次「单连接全链路追踪」。

**任务**：选定一个连接（source route tree → 某 sink），沿下面的链路读一遍源码，画出数据流图，并在每一站标注「读/写了 `t_rr_node_route_inf` 的哪个字段」。

链路如下：

1. **种子入堆**：`connection_router.tpp::timing_driven_route_connection_common_setup` → `add_route_tree_to_heap` → `serial_connection_router.cpp::add_route_tree_node_to_heap`。注意它用 `push_back`（不保序）+ `build_heap`（批量建堆），种子代价 = `criticality×Tdel + astar_fac×max(0, expected−offset)`。
2. **主循环**：`serial_connection_router.cpp::timing_driven_find_single_shortest_path_from_heap` 的 `while(try_pop)`。
3. **堆后剪枝**：`timing_driven_expand_cheapest` 的 `best_total_cost == new_total_cost` 判定。
4. **邻居扩展**：`timing_driven_expand_neighbours`（含预取）→ `timing_driven_expand_neighbour`（BB 剪枝 + IPIN 剪枝）。
5. **代价计算与堆前剪枝**：`timing_driven_add_to_heap` → `connection_router.tpp::evaluate_timing_driven_node_costs` → `route_common.h::get_single_rr_cong_cost`，最后 `heap_.add_to_heap({new_total_cost, to_node})`。
6. **命中回溯**：循环 `break` 后，调用方用 `rr_node_route_inf[sink_node].prev_edge` 沿边一路回溯得到完整路径。

**交付物**：

- 一张流程图，标出每一步压入堆的 `HeapNode` 的 `prio` 是怎么算出来的（应能写出 4.2.4 的那条公式）；
- 一张表，列出 `t_rr_node_route_inf` 的字段（`prev_edge`/`acc_cost`/`path_cost`/`backward_path_cost`/`R_upstream`/`occ`）分别在哪一步被读、哪一步被写；
- 一句话回答：如果把 `--router_heap` 从默认的 `four_ary` 换成 `binary`，这条链路的**逻辑**会变吗？**性能**呢？

> 参考答案要点：逻辑完全不变（同一套路由器骨架，仅模板实参不同）；性能上四叉堆在大电路更快、小电路差异可忽略。具体的耗时数字「待本地验证」。

## 6. 本讲小结

- 连接路由器在 RR Graph 上跑的是 **A\* 迷宫布线**：用最小堆不断弹出代价最小节点、向邻居扩散，命中 sink 即停；关闭前瞻（`astar_fac=0`）即退化为 Dijkstra。
- 主循环在 `serial_connection_router.cpp::timing_driven_find_single_shortest_path_from_heap`；它不维护显式 `visited` 集，而靠**堆后剪枝**（弹出代价 == 存储最优代价）丢弃过期堆条目，靠**堆前剪枝**（新路径优于已知才入堆）控制堆膨胀。
- 入堆 priority 由「现时项 + 前瞻项」组成：`total = backward + astar_fac×max(0, expected−offset)`；现时项 = `(1−criticality)×拥塞 + criticality×延迟 [+拐弯]`，由 `criticality` 在时序与可布线性之间加权。
- 拥塞代价 `base×acc×pres` 体现 Pathfinder 协商：过载节点越用越贵，`pres_fac` 控制当轮惩罚、`acc_cost` 记录历史累积。
- 堆是**可插拔**的：`ConnectionRouter<HeapImplementation>` 把堆作模板参数，`HeapNode` 仅 8 字节以追求缓存友好；默认 `--router_heap four_ary`，因四叉堆树更低、缓存命中更好，大电路比二叉堆快约 5%。
- 堆类型选择链：命令行 `--router_heap` → `ParseRouterHeap` → `e_heap_type` → `make_serial_connection_router` 的 `switch` → `SerialConnectionRouter<BinaryHeap|FourAryHeap>`；`bucket` 目前仅存在于帮助文本，代码未落地。

## 7. 下一步学习建议

- **u6-l3（基于连接的路由与路由树）**：本讲的连接路由器是被「逐连接」调用的——去看上层 `route()` 如何调度每个连接、`route_tree` 如何让已布线段被复用（增量布线），以及 `connection_based_routing` 何时强制重布某连接。
- **u6-l4（Router Lookahead）**：本讲里反复出现的 `expected_cost` 来自前瞻；去读 `router_lookahead.h` 与 `router_lookahead_cost_map.h`，理解前瞻代价是怎么预计算成一张代价地图的。
- **u8-l5（Server 模式与并行布线）**：去看 `ParallelNetlistRouter` 与 `multi_queue_d_ary_heap.h`，理解同一套路由器骨架如何换上多队列并行堆、把单线程的 `while(try_pop)` 变成多线程并行扩展。
- **延伸阅读**：连接路由器的代价与剪枝思想源自 AIR（Adaptive Information Routing）与经典 Pathfinder；`serial_connection_router.h` 顶部注释将其标注为 "AIR's serial timing-driven connection router"，可据此检索相关论文对照理解。
