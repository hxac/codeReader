# 基于连接的路由与路由树

## 1. 本讲目标

上一讲（u6-l2）我们看清了「连接路由器」（Connection Router）如何为**单个**「源 → 汇」连接在 RR Graph 上跑一次 A* 迷宫搜索。但真实布线有几千到几十万个连接，每个连接不能孤立地搜一次就完事——它们会争抢同一根导线、互相挤压，还会随迭代不断被推翻重来。

本讲把镜头拉远，回答三个问题：

1. **迭代布线主循环**：整个布线过程怎么一轮一轮地跑？为什么需要反复迭代才能消除拥塞？
2. **连接级重布线**：怎么避免「整张网表全部推翻、从头再搜」的巨大开销，只针对真正需要重布的连接动手？
3. **路由树复用**：上一轮已经找到的合法布线段如何被复用，让下一轮搜索从「已经走到一半」的地方继续？

学完后，你应该能：

- 说清 VPR 布线的 Pathfinder 协商式（negotiated congestion）迭代框架与终止条件。
- 区分「整网推翻重布」与「增量重布（剪枝复用）」两种策略，并知道何时用哪种。
- 复述强制重布（forcible reroute）的三个触发条件，以及「连接延迟下界」的作用。
- 解释 RouteTree 在迭代之间如何充当「部分布线状态的记忆体」。

## 2. 前置知识

阅读本讲前，请确保已理解：

- **RR Graph（路由资源图）**：节点是 SOURCE/SINK/IPIN/OPIN/CHANX/CHANY 等路由资源，边是开关（u6-l1）。
- **连接路由器与堆**：为单个「源 → 汇」连接做 A*/Dijkstra 搜索，代价含现时项与前瞻项（u6-l2）。
- **Pathfinder 协商式布线**的核心思想：每一轮把所有网都布上去（允许共享、允许过载），对过载的资源抬高代价（`pres_fac` 现时惩罚、`acc_fac` 历史惩罚），下一轮大家就会主动绕开热点；如此反复，直到没有任何资源过载（feasible）。

如果你对「关键度（criticality）」这个词还不熟，可先回顾 u6-l2：它把一条连接的「时序重要性」归一化到 0~1，关键度越高的连接，代价函数里延迟项权重越大、越倾向于抄近路。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [vpr/src/route/route.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route.h) | `route()` 入口声明，注释里给出了 AIR 算法论文出处。 |
| [vpr/src/route/route.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route.cpp) | 迭代布线主循环、收敛判定、强制重布的触发点。 |
| [vpr/src/route/connection_based_routing.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/connection_based_routing.h) / [.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/connection_based_routing.cpp) | `Connection_based_routing_resources`（简称 CBRR）：增量重布与强制重布的全部状态与逻辑。 |
| [vpr/src/route/route_net.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_net.h) / [.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_net.cpp) / [.tpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_net.tpp) | 单个网 / 单个连接的布线函数：`setup_net`、`should_route_net`、`route_sink`。 |
| [vpr/src/route/route_tree.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_tree.h) / [.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_tree.cpp) | `RouteTree` / `RouteTreeNode`：保存每个网的部分布线状态与延迟。 |

> 名词对照：本讲的「连接（connection）」特指一个网里「驱动端 → 某个接收端」的一段点对点布线；一个多扇出网包含多个连接。

## 4. 核心概念与源码讲解

### 4.1 迭代布线主循环

#### 4.1.1 概念说明

布线为什么不能「一次成功」？因为 FPGA 上的布线资源是**共享且有限**的。如果每个连接都只顾自己走最短路径，必然有很多连接挤在同一根导线上（过载，overuse）。Pathfinder 的解法是「**先让大家随便走，再用代价逼大家让路**」：

- 每一轮（iteration）把所有网都布完，允许共享、允许过载。
- 对发生过载的资源，抬高它的代价。两类惩罚：
  - **现时惩罚 `pres_fac`（present penalty factor）**：当前这一轮过载了就立刻变贵，随迭代不断放大。
  - **历史惩罚 `acc_fac`（accumulated congestion）**：累加历史拥塞，曾经挤过的资源永远更贵一点，防止反复横跳。
- 下一轮重布时，连接路由器的代价函数会自然避开这些变贵的资源，去绕路。
- 当某轮布完**没有任何资源过载**，称为「合法（feasible）」，布线收敛。

代价粗略可写成：

\[ \text{Cost}(n) = \bigl(\,b(n) + h(n)\,\bigr) \cdot p(n) \]

其中 \(b(n)\) 是基础代价，\(h(n)\) 由 `acc_fac` 累积，\(p(n)\) 由 `pres_fac` 放大；过载越重，\(p(n)\) 越大。这正是 u6-l2 连接路由器代价里「拥塞代价」项的来源。

#### 4.1.2 核心流程

`route()` 的主循环（`for itry = 1 .. max_router_iterations`）每一轮做这些事：

1. 重置每个网的「已布」标记。
2. 调用 `netlist_router->route_netlist(itry, pres_fac, ...)` 把**所有网**都布一遍（内部就是 4.2、4.3 讲的逐网逐连接布线）。
3. `feasible_routing()` 判断是否还有过载资源。
4. 更新代价惩罚：第 1 轮 `acc_fac=0`，之后按 `router_opts.acc_fac` 累加历史拥塞。
5. `timing_info->update()` 跑一次时序分析，得到新的关键路径与各连接的关键度。
6. 判定是否收敛（`is_iteration_complete`）；若收敛则保存当前布线为「最佳」。
7. 准备下一轮：放大 `pres_fac`（`pres_fac *= pres_fac_mult`，封顶 `max_pres_fac`），必要时收紧强制重布的容差。

收敛后还会做一件关键事：**收紧容差，逼那些时序次优的连接再被强制重布一次**，以提升质量（详见 4.2）。整个循环退出后，恢复保存过的「最佳布线」作为最终结果。

#### 4.1.3 源码精读

入口 `route()` 实现的是 AIR 算法，注释直接给出论文出处：

> [vpr/src/route/route.h:9-19](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route.h#L9-L19)：`Attempts a routing via the AIR algorithm`，引用 K. Murray 等人「AIR: A fast but lazy timing-driven FPGA router」（ASPDAC 2020）。AIR 的关键词是 **fast but lazy（快但懒）**——后面 4.2/4.3 会看到「懒」体现在哪里。

主循环本身：

> [vpr/src/route/route.cpp:265](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route.cpp#L265)：`for (itry = 1; itry <= router_opts.max_router_iterations; ++itry)` —— 迭代布线主循环起点。

每一轮的核心三步：

> [vpr/src/route/route.cpp:294](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route.cpp#L294)：`RouteIterResults iter_results = netlist_router->route_netlist(...)` —— 把全部网布一遍。
>
> [vpr/src/route/route.cpp:310](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route.cpp#L310)：`bool routing_is_feasible = feasible_routing();` —— 是否已无过载。
>
> [vpr/src/route/route.cpp:314-318](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route.cpp#L314-L318)：第 1 轮用 `acc_cost=0`，其余轮按 `router_opts.acc_fac` 累加历史拥塞惩罚。

收敛判定与保存最佳：

> [vpr/src/route/route.cpp:367-403](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route.cpp#L367-L403)：`is_iteration_complete(...)` 为真时，把当前 `route_trees` 存为 `best_routing`，并把强制重布的容差收紧：

```cpp
//Decrease pres_fac so that critical connections will take more direct routes
pres_fac = router_opts.first_iter_pres_fac;
//Reduce timing tolerances to re-route more delay-suboptimal signals
connections_inf.set_connection_criticality_tolerance(0.7);
connections_inf.set_connection_delay_tolerance(1.01);
```

注意这里把延迟容差从默认 1.1 收到 1.01：意思是「只要某连接的延迟比下界高 1%，就强制重布」，从而在收敛后进一步打磨时序质量。

准备下一轮——放大现时惩罚：

> [vpr/src/route/route.cpp:463-471](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route.cpp#L463-L471)：第 1 轮结束后置 `pres_fac = initial_pres_fac`；之后每轮 `pres_fac *= pres_fac_mult`，并用 `max_pres_fac` 封顶。

退出循环后恢复最佳布线：

> [vpr/src/route/route.cpp:590-611](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route.cpp#L590-L611)：先减去当前布线的拥塞计数、再加回 `best_routing` 的拥塞计数，把全局占用状态恢复成最佳那轮。

#### 4.1.4 代码实践

**实践目标**：观察一次真实布线中迭代轮次、`pres_fac` 与过载节点数如何随轮次变化，验证「协商式收敛」过程。

**操作步骤**：

1. 按 u1-l2 完成最小构建（`make -j8 vpr`）。
2. 任选一个回归电路（例如 `vtr_flow/benchmarks` 下的小电路，配合一个架构 XML），用固定通道宽度跑一次：
   ```shell
   ./build/vpr/vpr <arch.xml> <circuit.blif> --route_chan_width 100 --route_verbosity CRITICAL
   ```
3. 观察终端打印的每轮状态行（由 `print_route_status` 输出），它包含迭代号、`pres_fac`、过载节点数（overused nodes）、线长、关键路径延迟等。

**需要观察的现象**：

- 前几轮过载节点数较大，随 `pres_fac` 增大而下降。
- 某一轮过载节点数降到 0 时，日志会提示进入收敛（re-converge），随后 `pres_fac` 被重置、容差收紧，再迭代若干轮打磨时序。
- 最终打印 `Successfully routed after N routing iterations.`。

**预期结果**：小电路通常 10~30 轮收敛；过载节点数曲线单调（或近似单调）下降。

> 若本地尚未构建，运行结果「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果 `feasible_routing()` 始终为假，主循环靠什么退出？
**答案**：靠 `itry` 达到 `router_opts.max_router_iterations` 上限；此外还有提前放弃机制——当路由失败预测器（`routing_predictor`）预测的成功轮次超过阈值（`abort_iteration_threshold`）时会 `break`（[route.cpp:431-438](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route.cpp#L431-L438)）。

**练习 2**：为什么第 1 轮要把 `acc_cost` 设为 0？
**答案**：第 1 轮还没有任何拥塞历史，让所有连接先按「纯时序/纯最短」自由布线，以暴露真实的拥塞热点；从第 2 轮起才开始累积历史惩罚逼大家让路。

---

### 4.2 连接级重布线

#### 4.2.1 概念说明

「协商式收敛」最朴素的实现是：**每一轮把每个网的所有连接全部推翻、从头再搜一遍**。这对小扇出网没问题，但对成百上千扇出的网（如复位线、时钟使能）是灾难——绝大部分连接上一轮已经合法，没必要重搜。

AIR 的「懒」正体现在此，它把重布分两类：

1. **增量重布（incremental reroute，靠路由树剪枝）**：对大扇出网，不推翻整棵路由树，只**剪掉**（prune）那些过载或被标记强制重布的分支，保留合法部分；下一轮只补搜没合法到达的那些汇（sink）。
2. **强制重布（forcible / targeted reroute）**：即使某条连接**已经合法**，如果它既「时序关键」又「延迟次优」，就主动把它撕掉重布，去抢更快的路径。这是为了在拥塞缓解后继续优化时序。

承载这两类逻辑的状态机就是 `Connection_based_routing_resources`（CBRR）。它的注释开门见山：

> encompasses both incremental rerouting through route tree pruning and targeted reroute of connections that are critical and suboptimal.

#### 4.2.2 核心流程

**增量重布**（在 `setup_net` 中，每个网每轮开始时调用一次）：

- 若网扇出小（`num_sinks < min_incremental_reroute_fanout`，默认 **16**），或第 1 轮，或需要修 hold —— 整网推翻重布。
- 否则（大扇出网）—— **剪枝复用**：复制一份路由树，剪掉过载/强制重布分支，保留合法部分；只对「尚未合法到达」的汇补搜。

**强制重布**（在主循环每轮末尾由 `forcibly_reroute_connections` 决定）：对每条连接打一个 `forcible_reroute_connection_flag`，当且仅当**同时**满足三个条件才置真：

1. 当前关键路径延迟相对「上次稳定关键路径延迟」**显著增长**；
2. 该连接**足够关键**（关键度 ≥ 阈值）；
3. 该连接**延迟次优**（相对其「延迟下界」偏离过多）。

被标记的连接会在下一轮被 `should_route_net` 与 `prune` 识别出来，强制撕掉重搜。三个条件用阈值参数化：

- 条件 1 阈值 `critical_path_growth_tolerance`（默认 **1.001**）：\( D_{\text{cur}} > D_{\text{stable}} \cdot 1.001 \)
- 条件 2 阈值 `connection_criticality_tolerance`（默认 **0.9**）：\( \text{crit}_{\text{pin}} \geq \text{max\_criticality} \cdot 0.9 \)
- 条件 3 阈值 `connection_delay_optimality_tolerance`（默认 **1.1**）：\( D_{\text{conn}} \geq D_{\text{lb}} \cdot 1.1 \)

「延迟下界」\( D_{\text{lb}} \) 来自第 1 轮：第 1 轮几乎不考虑拥塞、只优化时序延迟，因此第 1 轮得到的各连接延迟近似是该连接的「最快可能延迟」，作为后续判断次优与否的基准；之后若发现更优也会更新。

#### 4.2.3 源码精读

CBRR 类的职责说明与关键数据成员：

> [vpr/src/route/connection_based_routing.h:11-17](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/connection_based_routing.h#L11-L17)：注释说明 CBRR 同时涵盖「靠剪枝的增量重布」与「针对关键且次优连接的目标重布」。
>
> [vpr/src/route/connection_based_routing.h:33-44](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/connection_based_routing.h#L33-L44)：强制重布标志 `forcible_reroute_connection_flag`（按 `[net][sink_rr_node]` 索引的 bool 表）与延迟下界 `lower_bound_connection_delay`（`[net][ipin]`）。注释直接写明强制重布的三个条件。

三个容差与「上次稳定关键路径延迟」：

> [vpr/src/route/connection_based_routing.h:46-57](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/connection_based_routing.h#L46-L57)：`last_stable_critical_path_delay`（最近一次稳定关键路径延迟）、`critical_path_growth_tolerance`、`connection_criticality_tolerance`、`connection_delay_optimality_tolerance`。

「显著增长」的判定：

> [vpr/src/route/connection_based_routing.h:69-71](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/connection_based_routing.h#L69-L71)：`critical_path_delay_grew_significantly(new) = new > last_stable * critical_path_growth_tolerance`。

容差默认值（构造函数）：

> [vpr/src/route/connection_based_routing.cpp:13-16](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/connection_based_routing.cpp#L13-L16)：`critical_path_growth_tolerance{1.001f}`、`connection_criticality_tolerance{0.9f}`、`connection_delay_optimality_tolerance{1.1f}`。

强制重布判定的完整实现，三个 `continue` 分别对应「跳过块内零延迟连接」「跳过低关键度连接」「跳过已接近最优的连接」，剩下的才打标记：

```cpp
// connection_based_routing.cpp:97-110
// skip if connection criticality is too low (not a problem connection)
if (pin_criticality < (max_criticality * connection_criticality_tolerance))
    continue;
// skip if connection's delay is close to optimal
if (net_delay[net_id][ipin] < (lower_bound_connection_delay[net_id][ipin - 1]
                               * connection_delay_optimality_tolerance))
    continue;
forcible_reroute_connection_flag[net_id][rr_sink_node] = true;
any_connection_rerouted = true;
```

> [vpr/src/route/connection_based_routing.cpp:67-118](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/connection_based_routing.cpp#L67-L118)：`forcibly_reroute_connections` 全文。注意它返回 `!any_connection_rerouted`——只要有任何连接被标记，整体布线配置就视为「不稳定」，需要再来一轮。

延迟下界在第 1 轮建立：

> [vpr/src/route/connection_based_routing.cpp:46-58](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/connection_based_routing.cpp#L46-L58)：`set_lower_bound_connection_delays` 把第 1 轮的 `net_delay` 拷贝为各连接的延迟下界。
>
> [vpr/src/route/route.cpp:532-535](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route.cpp#L532-L535)：主循环第 1 轮末尾调用 `set_stable_critical_path_delay` 与 `set_lower_bound_connection_delays`。

强制重布在主循环中的触发点：

> [vpr/src/route/route.cpp:550-564](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route.cpp#L550-L564)：

```cpp
bool should_ripup_for_delay = (router_opts.incr_reroute_delay_ripup == ON);
should_ripup_for_delay |= (router_opts.incr_reroute_delay_ripup == AUTO
                           && router_congestion_mode == NORMAL);
if (should_ripup_for_delay) {
    if (connections_inf.critical_path_delay_grew_significantly(critical_path.delay())) {
        stable_routing_configuration = connections_inf.forcibly_reroute_connections(...);
    }
}
```

可见强制重布受 `--incremental_reroute_delay_ripup` 选项控制（默认 `AUTO`，且仅在不拥塞的 `NORMAL` 模式下生效——拥塞激烈时优先解决冲突，不纠结时序打磨）。选项定义见 [vpr/src/base/vpr_types.h:1286-1290](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L1286-L1290)（`ON/OFF/AUTO`）与 [vpr/src/base/read_options.cpp:3269-3272](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L3269-L3272)（默认 `auto`）。

增量重布（整网推翻 vs 剪枝复用）的分发：

> [vpr/src/route/route_net.cpp:16-21](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_net.cpp#L16-L21)：`setup_net` 签名。
>
> [vpr/src/route/route_net.cpp:32-45](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_net.cpp#L32-L45)：`if (num_sinks < min_incremental_reroute_fanout || itry == 1 || ripup_high_fanout_nets)` 分支——整网推翻，并 `clear_force_reroute_for_net`。
>
> [vpr/src/route/route_net.cpp:46-93](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_net.cpp#L46-L93)：`else` 分支——剪枝复用（细节见 4.3）。扇出阈值默认 16，见 [vpr/src/base/read_options.cpp:3092-3095](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L3092-L3095)。

判定「一个网本轮到底要不要布」——`should_route_net`，其中也读取强制重布标志：

> [vpr/src/route/route_net.cpp:126-173](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_net.cpp#L126-L173)：只要出现「无布线 / 过载 / 强制重布标志 / 还有未到达的汇」之一就返回 `true`。

```cpp
// route_net.cpp:158-165
if (rt_node.is_leaf()) { //End of a branch
    if (if_force_reroute) {
        if (connections_inf.should_force_reroute_connection(net_id, inode)) {
            return true; // 这个连接被标记强制重布 → 整个网都要重布
        }
    }
}
```

#### 4.2.4 代码实践

**实践目标（本讲指定任务）**：阅读 `connection_based_routing.h` 中关于 `forcible_reroute` 与 `lower_bound_connection_delay` 的注释，说清何时触发对某连接的强制重布。

**操作步骤**：

1. 打开 [vpr/src/route/connection_based_routing.h:33-57](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/connection_based_routing.h#L33-L57)。
2. 阅读第 35~39 行的注释（三条件）与第 42~57 行的字段说明。
3. 对照 [connection_based_routing.cpp:67-118](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/connection_based_routing.cpp#L67-L118) 的实现，确认注释里的三个条件分别对应代码里的哪几行。
4. 思考：为什么条件 3 要用「下界 × 容差」而不是绝对延迟阈值？（提示：不同连接的「最快可能延迟」差别极大，必须各自有基准。）

**需要观察的现象 / 结论**：注释把强制重布描述为三个**同时成立**的条件——「关键路径显著变长」+「该连接足够关键」+「该连接相对其下界明显次优」。代码里 `forcibly_reroute_connections` 先剔除零延迟（块内）连接、剔除低关键度连接、剔除已接近下界的连接，剩下的才打标记。

**预期结果**：你能用一句话复述三条件，并指出第 1 轮建立的 `lower_bound_connection_delay` 是判断「次优」的基准。

**动手拓展**：用 `--incremental_reroute_delay_ripup off` 关掉强制重布再跑同一个电路，比较关键路径延迟变化（预期：关掉后时序略差但布线可能更快收敛；结果「待本地验证」）。

#### 4.2.5 小练习与答案

**练习 1**：强制重布的三个条件中，缺哪一条最危险？为什么？
**答案**：缺条件 2（足够关键）。若不限制关键度，所有次优连接都会被反复撕掉重布，布线会在不同合法解之间振荡、难以稳定收敛，且白白浪费运行时间。关键度门槛确保只对「值得优化」的连接动刀。

**练习 2**：`forcibly_reroute_connections` 返回 `false`（即有连接被标记）时，主循环会如何反应？
**答案**：`stable_routing_configuration` 变 `false`，于是不会更新 `last_stable_critical_path_delay`（[route.cpp:566-569](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route.cpp#L566-L569)），下一轮这些被标记的连接会被强制重布，直到没有任何连接需要被标记才算稳定。

**练习 3**：为什么大扇出网要走「剪枝复用」而不是整网推翻？
**答案**：大扇出网连接数多，整网推翻再全搜代价巨大；而上一轮绝大多数分支已合法，只需剪掉过载/强制重布的少数分支、补搜未到达的汇，可大幅减少堆搜索量——这正是 AIR「lazy」的核心收益。

---

### 4.3 路由树复用

#### 4.3.1 概念说明

`RouteTree` 是每个网在迭代之间保存「部分布线状态」的数据结构。可以这样理解：

- 一个网从它的 SOURCE 节点出发，长出一棵树，树的每个节点是一个 RR 节点，叶子是各个已被到达的 SINK。
- 树上每个节点缓存了从源到该节点的累计延迟 `Tdel`、上游电阻 `R_upstream`、下游电容 `C_downstream`，供时序分析与下一轮搜索复用。
- 每个网恰好有一棵 `RouteTree`，存在 `RoutingContext::route_trees` 里。

复用发生在两个地方：

1. **连接路由器从路由树出发**：给某个汇布线时，连接路由器的起点不是「空」，而是把**当前路由树的根**（连同已布的合法节点）当作搜索的初始前沿（frontier）——已经找到的好路径直接作为搜索起点，不必重走。这就是 AIR 论文里 `timing_driven_route_connection_from_route_tree` 名字中「from_route_tree」的含义。
2. **剪枝保留合法部分**：迭代之间，把过载或强制重布的分支剪掉，留下合法的「骨架」，下一轮在骨架上继续补长。

> 名词：**isink** = 该网内汇的 1 起始引脚索引（input sink index）。`RouteTree` 用一个位集 `_is_isink_reached` 记录哪些汇已「合法到达」，布线时只需遍历尚未到达的那些。

#### 4.3.2 核心流程

逐网布线（`route_net` 模板，位于 `route_net.tpp`）的流程：

1. `should_route_net` 先判断本网是否需要布（见 4.2）。
2. `setup_net` 决定整网推翻 or 剪枝复用，构造好「部分路由树」。
3. 计算 `remaining_targets`：`~tree.get_is_isink_reached()` 取反，即尚未合法到达的汇。
4. 按关键度从高到低排序 `remaining_targets`。
5. 对每个目标汇调用 `route_sink`：
   - 调 `timing_driven_route_connection_from_route_tree(tree.root(), sink_node, ...)`，从现有路由树出发做一次 A* 搜索。
   - 搜到后 `tree.update_from_heap(...)` 把新找到的路径**提交**进路由树。
   - 更新该汇对应的占用计数与延迟。

剪枝（`RouteTree::prune`）的流程：

1. 复制一份路由树（因为剪枝依赖当前全局占用计数，必须先剪再减计数）。
2. 递归遍历：遇到 `occ > capacity`（过载）或 `should_force_reroute_connection`（强制重布）的子树，标记 `force_prune`，整支剪掉。
3. 合法到达的 SINK 在位集里置位 `_is_isink_reached`。
4. 若整棵树都被剪光，返回 `nullopt`，由 `setup_net` 退化为「只初始化到 SOURCE」。

> 高扇出优化：大扇出网默认只把**空间上邻近**目标的路由树节点放回堆（省时间）；但若该汇很关键（criticality > 0.9），则把**整棵**路由树放回堆，给它最大自由度找最快路径。这是「lazy」与「critical 时认真」的折中。

#### 4.3.3 源码精读

`RouteTree` 的整体设计与用法（头文件顶部有一段很完整的说明，强烈建议通读）：

> [vpr/src/route/route_tree.h:1-80](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_tree.h#L1-L80)：说明 RouteTree 保存部分/完整布线状态与延迟；布线本身不直接用它表示，而是把它推入堆、搜出新路径后再用 `update_from_heap()` 提交；拥塞路径用 `prune()` 在迭代间剪除。

单个节点的关键字段：

> [vpr/src/route/route_tree.h:113-148](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_tree.h#L113-L148)：`inode`（对应 RR 节点）、`parent_switch`、`re_expand`（是否作为后续连接的搜索源）、`Tdel`（源到本节点延迟）、`R_upstream`、`C_downstream`、`net_pin_index`（哪个汇）。

两类构造与核心写操作：

> [vpr/src/route/route_tree.h:377-381](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_tree.h#L377-L381)：两种构造函数——用 `RRNodeId` 或 `ParentNetId`。注意注释强调：`prune()` 只有用 `ParentNetId` 构造时才可用。
>
> [vpr/src/route/route_tree.h:394-395](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_tree.h#L394-L395)：`update_from_heap`——把刚搜到的、堆顶的连线段提交进路由树，并更新 `Tdel` 等。
>
> [vpr/src/route/route_tree.h:434-438](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_tree.h#L434-L438)：`prune`——剪掉过载节点，整树都被剪掉时返回 `nullopt`。

「哪些汇已合法到达」的位集与访问器：

> [vpr/src/route/route_tree.h:504-525](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_tree.h#L504-L525)：`get_is_isink_reached()` 返回 1 起始位集；`get_reached_isinks()` / `get_remaining_isinks()` 分别给出已到达 / 待到达的汇索引。

剪枝实现——过载与强制重布都会触发 `force_prune`：

> [vpr/src/route/route_tree.cpp:646-664](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_tree.cpp#L646-L664)：`RouteTree::prune` 入口，断言根是 SOURCE、且用 `ParentNetId` 构造。
>
> [vpr/src/route/route_tree.cpp:683-691](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_tree.cpp#L683-L691)：

```cpp
if (congested) {            // occ > capacity
    force_prune = true;     // 过载 → 剪
}
if (connections_inf.should_force_reroute_connection(_net_id, rt_node.inode)) {
    force_prune = true;     // 被标记强制重布 → 剪
}
```

逐网布线里，「只布剩余汇」「从路由树出发搜」「提交进树」三段：

> [vpr/src/route/route_net.tpp:115-119](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_net.tpp#L115-L119)：`remaining_targets_mask = ~tree.get_is_isink_reached()`，得到待布汇。
>
> [vpr/src/route/route_net.tpp:208-242](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_net.tpp#L208-L242)：按关键度降序遍历 `remaining_targets`，逐个调 `route_sink`。

`route_sink` 里从路由树根出发搜索、再 `update_from_heap` 提交：

> [vpr/src/route/route_net.tpp:326-332](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_net.tpp#L326-L332) 与 [vpr/src/route/route_net.tpp:458-464](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_net.tpp#L458-L464)：`router.timing_driven_route_connection_from_route_tree(tree.root(), sink_node, ...)`——注意第一个参数是 `tree.root()`，即从现有路由树出发。
>
> [vpr/src/route/route_net.tpp:485](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_net.tpp#L485)：`tree.update_from_heap(&cheapest, target_pin, ...)`——把搜到的路径提交进树。

高扇出 vs 关键汇的折中（注释把动机讲得很清楚）：

> [vpr/src/route/route_net.tpp:446-464](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_net.tpp#L446-L464)：

```cpp
//We normally route high fanout nets by only adding spatially close-by
//routing to the heap (reduces run-time). However, if the current sink is
//'critical', we put the entire route tree back onto the heap...
if (high_fanout && !sink_critical && ...) {
    // high_fanout 变体：只放邻近节点
} else {
    // 普通或关键汇：整棵路由树放回堆
}
```

`setup_net` 里剪枝复用的「先复制 → 剪 → 减旧计数 → 加新计数」流程（注意顺序很讲究，因为 `prune` 依赖剪枝**前**的占用计数）：

> [vpr/src/route/route_net.cpp:54-78](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_net.cpp#L54-L78)：

```cpp
RouteTree tree2 = tree.value();                         // 复制
vtr::optional<RouteTree&> pruned_tree2 = tree2.prune(connections_inf); // 用旧占用计数剪
pathfinder_update_cost_from_route_tree(tree->root(), -1); // 减去旧树占用
if (pruned_tree2) {                                     // 部分保留
    pathfinder_update_cost_from_route_tree(pruned_tree2->root(), 1); // 加回剪后树占用
    tree = std::move(pruned_tree2.value());
} else {                                                // 全剪光
    tree = RouteTree(net_id);                           // 退化为只有 SOURCE
    pathfinder_update_cost_from_route_tree(tree->root(), 1);
}
```

#### 4.3.4 代码实践

**实践目标**：通过阅读 `setup_net` 与 `prune` 的协作，理解「剪枝复用」如何避免整网重搜，并看清占用计数的增减顺序。

**操作步骤**：

1. 打开 [vpr/src/route/route_net.cpp:46-93](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_net.cpp#L46-L93)，画出 `setup_net` 的 `else` 分支流程图：复制 → 剪 → 减旧 → （部分保留则加新，否则重建）→ `reload_timing` → `is_valid` / `is_uncongested` 断言。
2. 打开 [vpr/src/route/route_tree.cpp:671-720](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_tree.cpp#L671-L720)，确认 `prune_x` 对「过载」和「强制重布」两种情况都把 `force_prune` 置真，并把合法 SINK 在 `_is_isink_reached` 置位。
3. 思考：为什么必须「先复制、用旧占用计数剪」，而不能「先减占用计数再剪」？

**需要观察的现象**：剪枝发生在占用计数变更**之前**；`prune` 读取的是 `rr_node_route_inf[inode].occ()`，它反映的是剪枝前那一刻的全局占用。

**预期结果**：你能解释——若先减计数，原本「过载」的节点可能瞬间变成「不过载」，`prune` 就不会剪它，导致这个过载分支被错误保留，下一轮仍占用冲突。所以顺序必须是「先剪后减」。

> 运行验证「待本地构建后进行」；可开启 `--route_verbosity DEBUG` 并结合 profiling 输出观察 `route_tree_preserved` 与 `route_tree_pruned` 计数（见 [route_net.cpp:66-73](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_net.cpp#L66-L73) 的 `profiling::route_tree_preserved()` / `route_tree_pruned()`）。

#### 4.3.5 小练习与答案

**练习 1**：连接路由器搜索的起点是「空堆」还是「已有路由树」？体现在哪个参数？
**答案**：是已有路由树。体现在 `timing_driven_route_connection_from_route_tree(tree.root(), ...)` 的第一个参数 `tree.root()`——已合法的树节点会被作为搜索前沿放回堆，从而复用上一轮的好路径。

**练习 2**：什么情况下 `RouteTree::prune` 会返回 `nullopt`？调用方如何处理？
**答案**：当整棵树（除 SOURCE 外）都被剪光时返回 `nullopt`。调用方 `setup_net` 走 `else` 分支的「Fully destroyed」路径，把树退化为只剩 SOURCE、重新占用计数，相当于这个网本轮从头布（[route_net.cpp:72-78](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_net.cpp#L72-L78)）。

**练习 3**：为什么用 `RRNodeId` 构造的 RouteTree 不能 `prune()`？
**答案**：因为 `prune` 内部要按 `_net_id` 查询 `connections_inf.should_force_reroute_connection`，并把合法 SINK 写入 `_is_isink_reached`，这些都依赖 `ParentNetId`。只有用 `ParentNetId` 构造的树才有这些信息（见 [route_tree.h:377-380](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_tree.h#L377-L380) 与 [route_tree.h:645-650](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_tree.h#L645-L650) 注释）。

## 5. 综合实践

**任务**：追踪一个多扇出网从「第 1 轮」到「收敛后打磨」的完整生命周期，把本讲三个模块串起来。

请按以下步骤完成一份「布线追踪笔记」：

1. **选定追踪对象**：在 [vpr/src/route/route.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route.cpp) 中标注：
   - 主循环入口（L265）、第 1 轮建立延迟下界（L532-535）、强制重布触发（L550-564）、收敛后收紧容差（L396-397）。
2. **画一张时序图**：横轴是迭代轮次 `itry`，针对一个假想的大扇出网 N，标出每一轮它在 `setup_net` 走哪条分支（第 1 轮必走「整网推翻」，之后走「剪枝复用」），以及它的路由树在第 1、2、… 轮分别被剪掉/保留了什么。
3. **标注强制重布的诞生与消亡**：
   - 诞生：在第 `k` 轮末尾，若 N 的某连接 c 满足三条件，`forcibly_reroute_connections`（[connection_based_routing.cpp:97-110](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/connection_based_routing.cpp#L97-L110)）把它的标志置真。
   - 生效：第 `k+1` 轮，`should_route_net`（[route_net.cpp:158-165](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_net.cpp#L158-L165)）发现该标志 → N 被重布；`prune_x`（[route_tree.cpp:688-691](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route_tree.cpp#L688-L691)）把 c 所在分支剪掉。
   - 消亡：c 被合法重布后，标志在下一轮 `forcibly_reroute_connections` 开头被清零（[connection_based_routing.cpp:84-85](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/connection_based_routing.cpp#L84-L85)）。
4. **回答收尾问题**：若把 `--min_incremental_reroute_fanout` 调到一个极大值（如 1e9），所有网都会走「整网推翻」分支，这会对运行时间与结果质量各产生什么影响？

**预期产出**：一份能向同伴讲清「AIR 在每一轮对一个大扇出网到底做了什么」的笔记，并把 `route()`、`setup_net`、`should_route_net`、`prune`、`forcibly_reroute_connections` 五个函数串成一条因果链。

## 6. 本讲小结

- **迭代主循环**：`route()` 用 Pathfinder 协商式框架反复「布所有网 → 抬高过载资源代价（`pres_fac`/`acc_fac`）」直到 `feasible_routing()` 无过载；收敛后收紧强制重布容差、再打磨若干轮，最终恢复最佳布线。
- **增量重布**：大扇出网（≥ `min_incremental_reroute_fanout` 默认 16）不整网推翻，而是剪掉路由树中过载/强制重布的分支、保留合法骨架，下一轮只补搜 `get_remaining_isinks()` 中尚未到达的汇。
- **强制重布三条件**：关键路径显著增长（×1.001）**且**连接足够关键（≥ max_criticality×0.9）**且**延迟相对下界明显次优（≥ lower_bound×1.1），三者同时成立才撕掉重布；受 `--incremental_reroute_delay_ripup`（默认 AUTO，仅 NORMAL 模式）控制。
- **延迟下界**：第 1 轮（几乎只优化时序）的各连接延迟被存为「最快可能延迟」基准，是判断「次优」的标尺，发现更优会更新。
- **路由树复用**：`RouteTree` 保存每网的部分布线与延迟；连接路由器从 `tree.root()` 出发搜索（复用已布好段），新路径用 `update_from_heap` 提交；`prune` 在迭代间剔除过载分支——这就是 AIR「fast but lazy」的实现基础。
- **剪枝顺序**：必须「先复制、用旧占用计数剪，再减旧计数、加新计数」，否则过载节点会被错误保留。

## 7. 下一步学习建议

- **下一步讲义 u6-l4（Router Lookahead）**：本讲反复出现的「连接路由器从路由树出发估算代价」依赖前瞻代价（lookahead），下一讲讲清 `router_lookahead` 如何提供「到达目标还剩多少代价」的估计，使迷宫搜索定向。
- **深入时序**：本讲的 `timing_info->update()`、关键度、关键路径来自第 7 单元（u7-l1/l2）的 Tatum 时序分析；想彻底理解「为什么这条连接关键」可接着读 `vpr/src/timing/`。
- **延伸阅读**：直接读 AIR 论文（K. Murray, S. Zhong, V. Betz, *AIR: A fast but lazy timing-driven FPGA router*, ASPDAC 2020，出处见 [route.h:17](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/route.h#L17)），对照本讲代码理解「lazy」的设计动机。
- **并行版**：u8-l5 会讲到 `ParallelNetlistRouter` 与多队列堆，它是本讲单线程 `route_netlist` 的并行扩展，建议在吃透本讲后再读。
