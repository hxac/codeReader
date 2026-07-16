# Server 模式与并行布线

## 1. 本讲目标

本讲聚焦 VPR 的两条「外部协同与加速」支路，学完后你应当掌握：

- **Server 模式**：VPR 如何在布线完成后常驻为一个 TCP 服务器，接受外部客户端（如可视化工具）的请求，把关键路径等信息以任务（Task）的形式在「IO 线程」与「主线程」之间安全传递。
- **网表级并行布线**：`ParallelNetlistRouter` 如何用一个空间划分树（PartitionTree）把「边界框不相交」的网分到不同子区域，再用 `tbb::task_group` 让多个线程并行布不同的网。
- **连接级并行布线**：`MultiQueueDAryHeap` 这种多队列并发优先队列如何让多个线程**同时扩展一个连接**的 A\* 搜索堆，以及它「松弛 A\*（relaxed A\*）」的代价。
- 一个**关键且容易混淆**的点：VTR 有两条互相正交的并行轴——`--router_algorithm parallel`（按网并行）与 `--enable_parallel_connection_router on`（按连接并行），二者可以独立开启、组合使用。

本讲依赖你已经学过 [u6-l3](u6-l3-connection-based-routing.md)（迭代布线主循环 `route()` 与 `route_net`），因为并行的入口正是替换 `route()` 内部那一句「为每个网调 `route_net`」。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个直觉。

**直觉一：服务器模式是一个「长跑的布线结果查询台」。**
普通模式下 VPR 跑完布线就退出；Server 模式下它跑完布线后**不退出**，而是开一个 TCP 端口（默认 `60555`）守着，等外部客户端发请求来问「现在的关键路径是什么」「把第 3 条路径画出来」。请求与响应都用一种带帧头的「电报（telegram）」协议封装。

**直觉二：「并行布线」可以发生在两个不同粒度上。**
布线的最内层是一个 A\* 迷宫搜索（一个源到一个汇找最短路，靠一个最小堆驱动）。往外一层是「为整张网表的每个网各跑一次搜索」。
- **连接级并行**：让多个线程**一起推同一个堆**，加速「一次搜索」。靠的是 `MultiQueueDAryHeap`。
- **网表级并行**：让多个线程**各布不同的网**。靠的是 `PartitionTree` + `tbb::task_group`。
两者解决的是不同瓶颈，源码也分别住在不同类里——这是本讲反复强调的核心。

**直觉三：多线程下的优先队列要解决「谁来取全局最小」这个矛盾。**
单线程堆：弹堆顶永远是全局最小，A\* 据此保证最优。多线程若共用一个堆、加一把大锁，所有线程排队等锁，等于退化成串行。MultiQueue 的思路是**放弃「每次都取严格全局最小」**，换成「从随机两个子队列里取较优者」——牺牲一点点最优性，换来几乎无锁的高并发。这就叫「松弛 A\*」。

涉及的两个编译期开关也先记下：Server 模式依赖 Qt/EZGL（GUI 构建），未开启时整个 `server/` 目录被宏 `NO_SERVER` 抹掉；并行布线依赖 Intel TBB，未开启时宏 `VPR_USE_TBB` 不定义、`ParallelNetlistRouter` 不可用。二者都在 [vpr/CMakeLists.txt](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/CMakeLists.txt#L41-L54) 里决策。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [vpr/src/server/gateio.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/server/gateio.h) / [.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/server/gateio.cpp) | Server 的 socket 通信层 `GateIO`，单客户端、IO 在独立线程 |
| [vpr/src/server/serverupdate.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/server/serverupdate.cpp) | 主线程周期回调 `server::update`，把请求交给 `TaskResolver` |
| [vpr/src/server/taskresolver.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/server/taskresolver.cpp) | 把任务分派到具体处理函数（取关键路径、画关键路径） |
| [vpr/src/server/task.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/server/task.h) / [commcmd.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/server/commcmd.h) | 任务封装与命令枚举（只有两种命令） |
| [vpr/src/route/netlist_routers.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/netlist_routers.h) | `NetlistRouter` 接口与工厂，按 `--router_algorithm` 分派串行/并行/分解 |
| [vpr/src/route/ParallelNetlistRouter.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/ParallelNetlistRouter.h) / [.tpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/ParallelNetlistRouter.tpp) | 网表级并行路由器：PartitionTree + `tbb::task_group` |
| [vpr/src/route/partition_tree.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/partition_tree.h) | 按边界框把网表切成空间树，保证同层网不相交 |
| [vpr/src/route/SerialNetlistRouter.tpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/SerialNetlistRouter.tpp) | 串行路由器，作为并行的对照基线 |
| [vpr/src/route/multi_queue_d_ary_heap.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/multi_queue_d_ary_heap.h) / [.tpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/multi_queue_d_ary_heap.tpp) | 多队列并发优先队列 `MultiQueueDAryHeap` |
| [vpr/src/route/parallel_connection_router.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/parallel_connection_router.h) | 连接级并行路由器，内部用 MultiQueue + 每节点自旋锁 |

## 4. 核心概念与源码讲解

### 4.1 Server 通信模型

#### 4.1.1 概念说明

Server 模式让 VPR 在布线后常驻，对外暴露一个 TCP 端口。它**只服务单个客户端**，采用经典的「IO 线程 + 主线程」分工：

- **IO 线程**：跑在一个独立 `std::thread` 里，负责收发字节、拆包成电报帧、把请求装进任务对象。它**绝不直接调用 VPR 的算法**。
- **主线程**：由 Qt 的事件循环（`QTimer`）每 100ms 驱动一次回调 `server::update`，从 IO 线程那里「领走」已收到的任务、调用 `TaskResolver` 求解、再把结果「还回」IO 线程去发送。

这种分离让耗时的算法求解不阻塞网络 IO，也让网络 IO 不污染主线程的 Qt 事件循环。任务（`Task`）就是两个线程之间唯一的「交接棒」。

> 注意：Server 依赖 Qt（`QApplication`/`QTimer`），所以它和 GUI 绑定。`vpr/CMakeLists.txt` 里只有当 EZGL（GUI）开启时 `VPR_USE_SERVER` 才可能为真，否则定义 `-DNO_SERVER`，整个 `server/` 代码在编译期被 `#ifndef NO_SERVER` 抹除。

#### 4.1.2 核心流程

Server 的生命周期与一次请求的处理流程：

```text
初始化阶段 (vpr_init_server)
  --server on / --port 60555
  └─ gate_io.start(port)  ── 启动 IO 线程 + 启动 100ms QTimer

布线阶段 (vpr_route_flow)
  └─ 把 timing_info / routing_delay_calc 存进 ServerContext（供查询）

常驻阶段 (每 100ms 触发 server::update，主线程)
  ┌─ gate_io.take_received_tasks()      ← 从 IO 线程把请求搬过来
  ├─ task_resolver.own_task()           ← 入队（含去重）
  ├─ task_resolver.update(app)          ← 按 CMD 分派求解
  ├─ task_resolver.take_finished_tasks()
  ├─ gate_io.move_tasks_to_send_queue() ← 把响应还回 IO 线程
  └─ app->refresh_drawing()             ← 若处理了任务就刷新 GUI

IO 线程 (start_listening, 每 100ms 一轮)
  check_client_connection → handle_sending_data → handle_receiving_data
  → handle_telegrams(装成 Task) → handle_client_alive_tracker(ECHO 探活)
```

#### 4.1.3 源码精读

**(1) 开关与端口。** Server 模式由两个命令行参数控制，默认关闭、端口默认 `60555`：

[read_options.cpp:L3783-L3792](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L3783-L3792) — 注册 `--server` 与 `--port`，存入 `t_server_opts`。

[vpr_types.h:L1641-L1644](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L1641-L1644) — `t_server_opts` 只有 `is_server_mode_enabled` 和 `port_num` 两个字段。

**(2) 启动入口。** `vpr_init` 在初始化末尾调用 `vpr_init_server`：

[vpr_api.cpp:L1322-L1334](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1322-L1334) — 若 `is_server_mode_enabled` 为真，取出全局 `ServerContext` 里的 `gate_io` 并 `start(port_num)`。`GateIO` 在 `ServerContext` 里作为值成员持有。

**(3) GateIO 的通信层职责。** 类注释把模型说得很清楚——单客户端、IO 在独立线程、由主线程创建和管理、socket 非阻塞：

[gateio.h:L122-L138](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/server/gateio.h#L122-L138) — 强调「`GateIO` 实例由主线程创建管理，内部 IO 在单独线程异步执行」。

两条跨线程交接任务的接口是关键：

[gateio.h:L171-L182](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/server/gateio.h#L171-L182) — `take_received_tasks`（把收到的请求移交给主线程）和 `move_tasks_to_send_queue`（把主线程算好的响应送回发送队列），二者都用 `m_tasks_mutex` 保护。

**(4) start：起 IO 线程 + 起 QTimer。**

[gateio.cpp:L175-L191](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/server/gateio.cpp#L175-L191) — `start` 起独立线程跑 `start_listening`，并把 `QTimer` 的 `timeout` 信号连到 `server::update(application)`，间隔 `SERVER_UPDATE_INTERVAL_MS = 100`。注释点明必须在 `QApplication` 存在后才能起定时器（故不在构造函数里起）。

**(5) IO 线程的通信事件循环。** 这是真正收发字节的地方，一个 `while (m_is_running)` 循环，每轮睡 `LOOP_INTERVAL_MS = 100`：

[gateio.cpp:L247-L296](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/server/gateio.cpp#L247-L296) — 依次：接受连接 → 发数据 → 收数据 → 拆电报帧并装成 `Task` → 客户端探活。用 `sockpp::tcp6_acceptor`/`tcp6_socket`，socket 设为非阻塞以适配多线程。

其中 `handle_telegrams` 负责把原始电报体解析出 `job_id`、`cmd`、`options` 三段，构造 `Task` 推入 `m_received_tasks`：

[gateio.cpp:L93-L125](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/server/gateio.cpp#L93-L125) — 注意 `ECHO` 电报是探活用的，不走任务流程。

**(6) 主线程的周期回调。** `server::update` 是衔接两个线程的中枢：

[serverupdate.cpp:L11-L45](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/server/serverupdate.cpp#L11-L45) — 先 `take_received_tasks` 搬请求、`own_task` 入队；仅当 `timing_info` 与 `routing_delay_calc` 都已就绪（即布线阶段已把它们填进 `ServerContext`）才调 `task_resolver.update(app)` 求解；随后把完成的任务送回发送队列，并在确有任务被处理时 `app->refresh_drawing()`。返回 `is_running` 决定 QTimer 是否继续。

**(7) 两种命令。** 协议目前只定义了两种命令，非常克制：

[commcmd.h:L7-L11](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/server/commcmd.h#L7-L11) — `GET_PATH_LIST_ID`（取关键路径列表）和 `DRAW_PATH_ID`（画指定关键路径）。

`TaskResolver::update` 按命令分派：

[taskresolver.cpp:L62-L84](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/server/taskresolver.cpp#L62-L84) — `GET_PATH_LIST_ID` 走 `process_get_path_list_task`，`DRAW_PATH_ID` 走 `process_draw_critical_path_task`。

以取关键路径为例，它读取选项（`path_num`/`path_type`/`details_level`/`is_flat_routing`）、调 `calc_critical_path`、把结果存进 `ServerContext.crit_paths` 并把报告塞进任务的响应：

[taskresolver.cpp:L86-L122](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/server/taskresolver.cpp#L86-L122) — 求解结果通过 `task->set_success(...)` 写回，最终由 IO 线程发给客户端。

**(8) 布线阶段为查询备好数据。** Server 能查关键路径，前提是布线阶段已经算出 `timing_info`：

[vpr_api.cpp:L1059-L1064](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1059-L1064) — 在 `vpr_route_flow` 里，若 `gate_io.is_running()`，就把 `timing_info` 与 `routing_delay_calc` 存进 `mutable_server()`，这正是 `server::update` 里「上下文是否就绪」判断的来源。

**(9) 任务封装。** `Task` 把「请求 + 结果 + 状态」打包，并能生成回传的响应缓冲：

[task.h:L20-L40](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/server/task.h#L20-L40) — 一个任务由 `job_id` + `cmd` + `options` 标识，`set_success`/`set_fail` 标记结果，`response_buffer()` 是最终要发给客户端的字节流。`own_task` 还会做去重（同命令同选项的在途任务会被拒绝）：

[taskresolver.cpp:L15-L34](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/server/taskresolver.cpp#L15-L34) — 同命令同选项 → 拒新任务；同命令不同选项且新任务 `job_id` 更大 → 作废旧任务。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：把「一个请求从字节到响应」的完整链路在脑中走一遍，并定位每一棒的代码位置。

**操作步骤**：

1. 打开 [gateio.cpp:L247-L296](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/server/gateio.cpp#L247-L296)，确认 IO 线程一轮循环的五步顺序。
2. 打开 [serverupdate.cpp:L11-L45](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/server/serverupdate.cpp#L11-L45)，找到主线程把请求搬进来、求解、把响应搬出去的三次「交接」。
3. 回答：为什么 IO 线程不能直接调用 `task_resolver.update()`？（提示：`TaskResolver` 会触碰 `g_vpr_ctx` 与 Qt 控件，而 Qt 控件只能在主线程操作。）

**需要观察的现象 / 预期结果**：你能画出一条数据流——`客户端字节 → TelegramBuffer 拆帧 → Task → m_received_tasks →(mutex)→ TaskResolver.update → set_success → m_send_tasks →(mutex)→ client.write_n`——并指出其中两处 `m_tasks_mutex` 临界区分别保护哪一段交接。

> 是否能在本机真正跑起来 server：取决于是否做了 GUI 构建（`VPR_USE_SERVER`）。运行行为**待本地验证**：需 `make`（带 GUI）后执行 `vpr <circuit> <arch> --route --server on --port 60555`，再用客户端连该端口。本实践以源码阅读为主。

#### 4.1.5 小练习与答案

**练习 1**：`server::update` 里有一句「仅当 `timing_info && routing_delay_calc` 才求解」的判断。如果布线阶段还没跑到（例如用户只 `--pack`），这个判断会怎样？

<details><summary>参考答案</summary>

该判断为假，`task_resolver.update(app)` 不会被调用，任务一直停在队列里不求解；客户端拿不到响应。这符合直觉——关键路径查询必须依赖布线后才能算出的时序信息。布线阶段在 [vpr_api.cpp:L1059-L1064](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1059-L1064) 才把这两项填入 `ServerContext`。

</details>

**练习 2**：`ClientAliveTracker` 为什么要发 `ECHO` 电报？

<details><summary>参考答案</summary>

服务器只服务单个客户端。为了区分「客户端真的下线了」与「客户端只是暂时没说话」，它在客户端静默超过 `echo_interval` 后主动发一个 `ECHO` 探活；若超过 `client_timeout` 仍无任何客户端活动，就判定客户端缺席、关闭连接、回到「等待新连接」状态。见 [gateio.h:L47-L87](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/server/gateio.h#L47-L87)。

</details>

---

### 4.2 并行网表路由器（按网并行）

#### 4.2.1 概念说明

回到布线主循环。串行路由器（`SerialNetlistRouter`）的做法很直白：把所有网按汇点数排序，然后一个接一个地为每个网调 `route_net`。这是单线程的。

**网表级并行**的思路是：**不同网之间如果占用的空间区域不重叠，就可以同时布，互不干扰**。于是先按各网的边界框（bounding box）把整张网表递归切成一棵空间划分树 `PartitionTree`——树根对应整个器件，每个分支节点是一条切线（cutline）加上「被这条切线穿过的网」，左右子树是切线两边的区域。关键性质是：**同一棵子树同一层的不同节点里，网的两两边界框不相交**，因此可以安全地交给不同线程并行布。

`ParallelNetlistRouter` 用 Intel TBB 的 `tbb::task_group` 来调度这棵树：先把根节点丢进任务组，根节点里的网**串行**布完后，再把它的左右孩子作为新任务丢进任务组。于是「布根」串行，「布同一层的兄弟/叔伯节点」并行。

> 一个关键澄清：`ParallelNetlistRouter` 内部每个线程用的是 **`SerialConnectionRouter`**（普通的单堆 A\*），**不是** 4.3 节的 MultiQueue 并发堆。也就是说「按网并行」这一支并不改变单次搜索的算法，只是把多次搜索分摊到多核。两支并行是正交的。

文档里还点明一个权衡：这种并行方式「与串行等价且确定性（serially equivalent & deterministic），但在拥塞情况下可能降低 QoR（结果质量）」。原因是并行后网的处理顺序相对串行发生了变化，而协商式布线（Pathfinder）对顺序敏感。

#### 4.2.2 核心流程

```text
route_netlist(itry, pres_fac, worst_neg_slack)
  ├─ 重置各线程的 RouteIterResults
  ├─ 缓存本趟参数（_itry/_pres_fac/_worst_neg_slack）
  ├─ 首趟懒构建 PartitionTree（按边界框递归二分）
  ├─ tbb::task_group group
  ├─ route_partition_tree_node(group, root)   ← 根节点入组
  │     ├─ 本节点内的网：按汇点数降序，串行 route_net（用本线程的 _routers_th.local()）
  │     └─ 节点布完 → group.run(左孩子) + group.run(右孩子)
  ├─ group.wait()                              ← 等整棵树布完
  └─ 合并各线程的 _results_th → 单个 RouteIterResults
```

调度形状像一棵树的广度并行：根串行 → 第二层两个孩子并行 → 第三层四个并行……越往下并行度越高。

#### 4.2.3 源码精读

**(1) 算法枚举与工厂分派。** `--router_algorithm` 选哪种 NetlistRouter：

[vpr_types.h:L1235-L1240](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L1235-L1240) — `NESTED` / `PARALLEL` / `PARALLEL_DECOMP` / `TIMING_DRIVEN`（默认）四种。

[netlist_routers.h:L121-L139](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/netlist_routers.h#L121-L139) — 选 `PARALLEL` 时，若编译期有 `VPR_USE_TBB` 就 `new ParallelNetlistRouter`，否则直接 `VPR_FATAL_ERROR` 报「未编译 TBB 支持」。`PARALLEL_DECOMP` 同理走 `DecompNetlistRouter`。

**(2) NetlistRouter 接口。** 不论串并行，对外都是这四个虚函数，所以 `route()` 主循环无需感知差异：

[netlist_routers.h:L47-L66](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/netlist_routers.h#L47-L66) — `route_netlist`（跑一趟）、`handle_bb_updated_nets`（边界框变化后更新划分树）、`set_rcv_enabled`、`set_timing_info`。其中 `handle_bb_updated_nets` 对串行是空操作，对并行才是真操作——这正是二者在接口上的唯一差别之一。

**(3) 类设计：线程局部存储。** 文件头注释把思路讲透：

[ParallelNetlistRouter.h:L1-L10](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/ParallelNetlistRouter.h#L1-L10) — 「按边界框建 PartitionTree，用 `tbb::task_group` 并行布树的节点；每个任务串行布完自己节点里的网，再把孩子加入任务队列」「与串行等价且确定，但拥塞时可能降 QoR」「不支持图形化布线断点」。

线程安全靠**每线程一份**路由器和结果，避免共享可变状态：

[ParallelNetlistRouter.h:L84-L90](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/ParallelNetlistRouter.h#L84-L90) — `tbb::enumerable_thread_specific<SerialConnectionRouter<HeapType>> _routers_th` 与 `_results_th`。`.local()` 取当前线程那份。注意每个线程的路由器是 `SerialConnectionRouter`。

**(4) 主循环。**

[ParallelNetlistRouter.tpp:L11-L45](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/ParallelNetlistRouter.tpp#L11-L45) — 重置线程局部结果 → 缓存参数 → 首趟懒构建 `_tree`（`PartitionTree(_net_list)`）→ 把根节点交给 `route_partition_tree_node` → `group.wait()` → 合并各线程结果（`stats.combine`、`rerouted_nets`/`bb_updated_nets` 拼接、`is_routable` 取与）。

**(5) 单个树节点的布线 + 派发孩子。**

[ParallelNetlistRouter.tpp:L47-L113](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/ParallelNetlistRouter.tpp#L47-L113) — 把节点里的网按汇点数降序，**串行**逐个 `route_net(_routers_th.local(), ...)`；节点布完后用 `g.run(...)` 把左右孩子作为新任务丢进 `task_group`（这两句是并行的来源）。`VTR_ASSERT(!node.left && !node.right)` 保证不存在只有一个孩子的节点。

注意这里对「需要扩大边界框重试」的网（`retry_with_full_bb`）**不像串行那样立刻重试**，而是记进 `bb_updated_nets`、本轮跳过，留给下一趟由 `handle_bb_updated_nets` 在树上重定位。

**(6) PartitionTree 的空间性质。**

[partition_tree.h:L41-L69](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/partition_tree.h#L41-L69) — `PartitionTreeNode` 持有本节点认领的 `nets`、`vnets`（虚拟网，供分解路由器用）、左右子树、切线轴与位置、本节点包围盒。分支节点的 `nets` 只含「被切线穿过」的网，叶子节点的 `nets` 是落到该区域的最终网集合。

[partition_tree.h:L86-L89](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/partition_tree.h#L86-L89) — `update_nets`：边界框只会增长，所以把长大的网**沿树向上**挪，直到塞进某个能容纳它的节点。这就是 `handle_bb_updated_nets` 的实现依据。

**(7) 与串行的对照。** 串行版本极其简洁，是理解并行的最好基线：

[SerialNetlistRouter.tpp:L12-L71](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/SerialNetlistRouter.tpp#L12-L71) — 排序所有网 → for 循环逐个 `route_net(*_router, ...)`（单一路由器）→ 需要扩大边界框时 `inet--` 立刻原地重试。`handle_bb_updated_nets` 是空函数（无树可更新）。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：逐项对比串行与并行两个 `route_netlist`，找出「并行为了多线程安全/确定性，付出了哪些结构代价」。

**操作步骤**：

1. 并排打开 [SerialNetlistRouter.tpp:L12-L71](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/SerialNetlistRouter.tpp#L12-L71) 与 [ParallelNetlistRouter.tpp:L11-L45](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/ParallelNetlistRouter.tpp#L11-L45)。
2. 找出三处差异并填表：(a) 路由器是共享一份还是每线程一份？(b) 结果对象 `RouteIterResults` 是单一还是每线程一份再合并？(c) `retry_with_full_bb` 是立刻重试还是延迟到下一趟？
3. 解释为什么并行版必须把 `retry_with_full_bb` 延迟：提示——同一节点的网是串行布的，扩大边界框可能让该网侵入兄弟节点的区域，必须先在树上重定位才能继续。

**预期结果**：你应能用自己的话讲清「并行的确定性来自 PartitionTree 固定了可并行的网分组，来自每线程独立的路由器与结果，来自把顺序敏感的重试推迟到下一趟」。

> 真正运行并行布线**待本地验证**：需 TBB 构建（默认 `auto`，装了 `libtbb-dev` 即可），运行时加 `--router_algorithm parallel`。

#### 4.2.5 小练习与答案

**练习 1**：`ParallelNetlistRouter` 为什么不直接共享一个 `SerialConnectionRouter` 给所有线程？

<details><summary>参考答案</summary>

`SerialConnectionRouter` 内部维护着迷宫搜索的可变状态（堆、`rr_node_route_inf` 的路径代价与回溯边、修改列表等）。多个线程同时推同一个堆、改同一片 `rr_node_route_inf` 必然数据竞争。并行版用 `tbb::enumerable_thread_specific` 给每个线程一份独立的路由器（[ParallelNetlistRouter.h:L84-L90](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/ParallelNetlistRouter.h#L84-L90)），从根本上回避共享可变状态——前提是 PartitionTree 已保证不同线程布的网空间不相交。

</details>

**练习 2**：文件头说并行「与串行等价且确定」，又说「拥塞时可能降 QoR」，这两句矛盾吗？

<details><summary>参考答案</summary>

不矛盾。「确定」指给定相同输入、相同线程数，结果可复现（PartitionTree 固定了分组和串行顺序，`task_group` 调度不引入随机性）；「等价」指算法语义（Pathfinder 协商）不变。但「与串行的结果相同」并不保证——并行改变了网的布线先后，而 Pathfinder 对顺序敏感，拥塞网较多时顺序差异会累积成不同的最终解，故可能降 QoR。

</details>

---

### 4.3 并发堆结构 MultiQueue（按连接并行）

#### 4.3.1 概念说明

现在看另一条并行轴。连接路由器（Connection Router）为「一个源到一个汇」跑 A\* 迷宫搜索，核心是一个最小堆：每次弹堆顶（代价最小的 RR 节点）扩展其邻居。这是布线最耗时的部分。

**连接级并行**的问题：怎么让多个线程**一起推这一个堆**？
- 若所有线程共用一个堆 + 一把大锁 → 退化为串行，锁竞争吃掉所有收益。
- MultiQueue 的解法：**用很多个独立子队列**（每个子队列是一把独立自旋锁保护的小堆），入堆时随机挑一个子队列放，出堆时随机挑两个子队列、取「两者堆顶较优者」弹出。这样绝大多数操作只锁一个子队列、且选哪个子队列是随机的，冲突概率极低。

代价是**松弛**：你弹出的不再是「严格全局最小」，而是「两个随机子队列里的较优者」。对 A\* 而言这意味着可能多扩展一些节点（次优路径），所以叫「松弛 A\*」。论文（FPT'24）表明这在 FPGA 布线上能换来显著的并行加速。

> 再次区分两条轴：本节的 `ParallelConnectionRouter` 由 `--enable_parallel_connection_router on` 触发，**与** `--router_algorithm parallel` **无关**。它替换的是 `SerialNetlistRouter` 内部那一个 `SerialConnectionRouter`，把「单次连接搜索」多线程化。两者可叠加：既按网并行（多个连接同时搜）、又按连接并行（每个连接的堆多线程推）。

#### 4.3.2 核心流程

MultiQueue 的并发模型（`MultiQueueIO`）：

```text
push(item):
  i = 随机选一个子队列
  lock(queues[i]); queues[i].pq.push(item); 更新 queues[i].min; unlock

pop():
  i, j = 随机选两个不同子队列
  比较 queues[i].min 与 queues[j].min（无锁原子读）
  选较优者 k → lock(queues[k]) → 弹其堆顶 → unlock
  （故弹出的是“两个随机子队列的较优者”，非全局最小）

终止检测 (tryPop):
  若 pop 不到 → numIdle++
  循环重试，直到 numIdle >= 线程数（所有线程都同意“没活了”）才返回空
```

子队列数 `NUM_QUEUES` 通常取「线程数 × 4」，且应为 2 的幂（随机选队列用位掩码 `NUM_QUEUES - 1`）。每个子队列结构体 `alignas(64)`（缓存行对齐）以避免**伪共享（false sharing）**——否则多个线程频繁改各自子队列的 `min` 会互相 invalidate 同一缓存行。

松弛程度可粗略刻画：设共有 \(Q\) 个子队列，一次 pop 只比较 2 个，取到「这两个里较优」的概率随 \(Q\) 增大而下降，松弛变大、并行度变高。实践中 \(Q\) 是可调旋钮。

\[ P(\text{一次 pop 看到全局最优子队列}) = \frac{2}{Q} \quad (\text{当全局最优唯一时}) \]

\[ \text{松弛代价} \propto \text{因非最优弹出而多扩展的节点数} \]

#### 4.3.3 源码精读

**(1) 两条轴的分叉点。** `SerialNetlistRouter._make_router` 按开关决定用哪种连接路由器：

[SerialNetlistRouter.h:L54-L82](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/SerialNetlistRouter.h#L54-L82) — `enable_parallel_connection_router` 为假 → `SerialConnectionRouter`；为真 → `ParallelConnectionRouter`，并传入 `multi_queue_num_threads/queues/direct_draining` 三个参数。

**(2) 对应命令行参数。**

[read_options.cpp:L3187-L3245](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L3187-L3245) — `--enable_parallel_connection_router`（开关）、`--multi_queue_num_threads`（线程数，默认 1 即串行）、`--multi_queue_num_queues`（子队列数，须 ≥ 2，默认 2）、`--multi_queue_direct_draining`（排空优化）。

**(3) MultiQueueDAryHeap：实现 HeapInterface 的适配层。**

[multi_queue_d_ary_heap.h:L39-L57](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/multi_queue_d_ary_heap.h#L39-L57) — 持有一个 `MQ_IO`（即 `MultiQueueIO`）智能指针。默认 `(1 线程, 2 队列)` 即串行。注意注释里的坑：`MQ_IO` 接口里**先传 num_queues 再传 num_threads**，顺序与函数名相反。

[multi_queue_d_ary_heap.h:L65-L81](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/multi_queue_d_ary_heap.h#L65-L81) — `try_pop`/`add_to_heap` 把 `HeapNode{prio, node}` 与 `MQHeapNode` 元组互转，再委托给 `MQ_IO`。

**(4) MultiQueueIO 的子队列与无锁读 min。** 每个子队列独立加锁、独立维护「当前堆顶优先级」`min`，让别的线程能**不加锁地**比较各子队列谁更优：

[multi_queue_d_ary_heap.tpp:L63-L76](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/multi_queue_d_ary_heap.tpp#L63-L76) — `PQContainer` 用 `alignas(CACHELINE)` 缓存行对齐，含自旋锁 `queueLock`、原子 `min`、d-ary 优先队列 `pq` 与 push/pop 计数。`lock/try_lock/unlock` 是 `atomic_flag` 自旋。

**(5) push：随机入队。**

[multi_queue_d_ary_heap.tpp:L124-L141](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/multi_queue_d_ary_heap.tpp#L124-L141) — `ThreadLocalRandom` 选一个子队列，锁住、入队、刷新 `min`。`ThreadLocalRandom` 是快速的 xorshift 伪随机，用 `NUM_QUEUES - 1` 掩码（故 `NUM_QUEUES` 宜为 2 的幂）。

**(6) pop：两两比较的松弛弹出。** 这是「松弛 A\*」的核心：

[multi_queue_d_ary_heap.tpp:L194-L253](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/multi_queue_d_ary_heap.tpp#L194-L253) — 随机选 `i != j`，读 `queues[i].min` 与 `queues[j].min`（原子 acquire），取较优者 `poppingQueue`，锁住后弹堆顶。`#ifdef MQ_IO_ENABLE_CLEAR_FOR_POP` 段是「排空优化」：若堆顶优先级已劣于 `minPrioForPop`（搜索已到达目标、无需再求最优），直接 `q.pq.clear()` 整队排空以快速结束。

**(7) 终止检测。** 多线程下「堆空」不等于「全空」——别的线程可能马上又 push。用「空闲计数」让所有线程达成共识：

[multi_queue_d_ary_heap.tpp:L164-L184](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/multi_queue_d_ary_heap.tpp#L164-L184) — `tryPop`：pop 不到就 `numIdle++`，循环重试；当 `numIdle >= threadNum`（所有线程都认为没活了）才返回空。

**(8) ParallelConnectionRouter：如何把 MultiQueue 接进 A\*。** 并行搜索一个连接时，多个线程共享这个 MultiQueue，各自从堆里 pop 节点扩展邻居、把新节点 push 回去。难点是「`rr_node_route_inf`（每个 RR 节点的路径代价/回溯边）会被多线程并发改」：

[parallel_connection_router.h:L184-L193](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/parallel_connection_router.h#L184-L193) — 继承 `ConnectionRouter<MultiQueueDAryHeap<D>>`，即把基类的堆类型换成 MultiQueue。

[parallel_connection_router.h:L21-L40](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/parallel_connection_router.h#L21-L40) — 解法是**每节点一把自旋锁** `spin_lock_t`：不同线程很少同时碰同一个节点，这种细粒度锁能把冲突降到极低。

[parallel_connection_router.h:L195-L223](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/parallel_connection_router.h#L195-L223) — 构造函数里起 `multi_queue_num_threads - 1` 个辅助线程（主线程算第 0 个），它们在 `thread_barrier_` 上与主线程同步、一轮一轮地并行扩展邻居；`locks_` 大小为 `rr_node_route_inf.size()`（每节点一锁）。析构函数 [L225-L248](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/parallel_connection_router.h#L225-L248) 置 `is_router_destroying_`、过 barrier、join 所有辅助线程，注释详细解释了为何必须先 join 再析构（避免悬空访问，见 issue #3029）。

**两条轴的总览**：

| 维度 | 开关 | 类 | 并行单位 | 单位内是否多线程 |
| --- | --- | --- | --- | --- |
| 按网并行 | `--router_algorithm parallel` | `ParallelNetlistRouter` + `PartitionTree` | 一个网 | 否（每线程一个 `SerialConnectionRouter`） |
| 按连接并行 | `--enable_parallel_connection_router on` | `ParallelConnectionRouter` + `MultiQueueDAryHeap` | 一次连接搜索的堆操作 | 是（多线程共推一个 MultiQueue） |

#### 4.3.4 代码实践（源码阅读型，对应课程指定实践任务）

**实践目标**：说清 `MultiQueueDAryHeap` 如何让多线程并行扩展堆搜索，并把它和 4.2 的 `ParallelNetlistRouter` 区分开。

**操作步骤**：

1. 打开 [multi_queue_d_ary_heap.tpp:L194-L253](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/multi_queue_d_ary_heap.tpp#L194-L253)，确认 pop 只比较两个随机子队列——这就是「松弛」的来源。
2. 打开 [multi_queue_d_ary_heap.tpp:L63-L76](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/multi_queue_d_ary_heap.tpp#L63-L76)，确认每个子队列 `alignas(64)` 且各有独立自旋锁与原子 `min`——这是「高并发、低冲突」的来源。
3. 打开 [parallel_connection_router.h:L195-L223](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/parallel_connection_router.h#L195-L223)，确认它在构造时起辅助线程、用 barrier 同步、用每节点自旋锁保护 `rr_node_route_inf`。
4. 用一句话回答课程的问题：「`multi_queue_d_ary_heap` 如何让多线程并行扩展堆搜索？」——参考答案见下方小练习 1。

**预期结果**：你能解释「多个线程共享一个 MultiQueue；push 随机入某子队列、pop 从随机两个子队列取较优者；靠每子队列独立锁 + 缓存行对齐 + 原子 min 实现低冲突；靠 numIdle 终止检测判断全空」。并明确这**不**是 `ParallelNetlistRouter` 那种「按网并行」。

> 运行验证**待本地验证**：需 TBB 构建，运行时加 `--enable_parallel_connection_router on --multi_queue_num_threads 4 --multi_queue_num_queues 16`。

#### 4.3.5 小练习与答案

**练习 1**（对应课程实践任务）：用一两句话说明 `multi_queue_d_ary_heap` 如何让多线程并行扩展堆搜索。

<details><summary>参考答案</summary>

它把单一优先队列拆成多个独立加锁的子队列（`PQContainer`，缓存行对齐、各有自旋锁与原子 `min`）。多个线程共享这组子队列：入堆随机选一个子队列，出堆随机选两个子队列、读它们无锁的原子 `min` 后取较优者加锁弹出。因为不同线程几乎总是操作不同子队列、且只在最后弹/pop 那一刻短暂持锁，冲突极低，从而多个线程能真正并行地扩展 A\* 搜索。代价是弹出的是「两随机子队列的较优者」而非全局最小（松弛 A\*），用 `minPrioForPop` 排空优化和 `numIdle` 终止检测收尾。

</details>

**练习 2**：`ParallelConnectionRouter` 为什么用「每节点一把自旋锁」而不是「一把全局锁」保护 `rr_node_route_inf`？

<details><summary>参考答案</summary>

A\* 扩展邻居时，多个线程同时更新的往往是**不同的** RR 节点（各自从堆里弹出的当前节点不同、扩展的邻居也不同）。一把全局锁会让所有线程串行排队，抹掉并行收益。每节点一把自旋锁是细粒度策略：只有当两个线程碰巧同时更新同一节点时才冲突，而这种情况很少见，所以锁竞争低、并行度高。自旋锁（而非 mutex）适用于这种临界区极短的场景，避免陷入内核的开销。见 [parallel_connection_router.h:L21-L40](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/parallel_connection_router.h#L21-L40)。

</details>

**练习 3**：`--router_algorithm parallel` 和 `--enable_parallel_connection_router on` 同时开，会发生什么？

<details><summary>参考答案</summary>

两条轴叠加：多个网被 `ParallelNetlistRouter` 分发到不同线程并行布；而每个线程本来用的 `SerialConnectionRouter`……注意——`ParallelNetlistRouter` 内部**硬编码**用的是 `SerialConnectionRouter`（[ParallelNetlistRouter.h:L67-L81](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/ParallelNetlistRouter.h#L67-L81)），并不读 `enable_parallel_connection_router` 开关。所以「按连接并行」的 `ParallelConnectionRouter` 只在**串行算法** `TIMING_DRIVEN`/`NESTED` 的 `SerialNetlistRouter._make_router` 里才会被启用（[SerialNetlistRouter.h:L54-L82](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/SerialNetlistRouter.h#L54-L82)）。也就是说当前实现下二者**不会**在同一趟里叠加：选了 `parallel` 算法就走纯按网并行；要按连接并行，算法须留在 `timing_driven`。这是阅读源码才能发现的细节。

</details>

## 5. 综合实践

设计一个把本讲三块内容串起来的**源码追踪任务**（无需构建运行）：

**任务**：假设一个外部可视化客户端连上 VPR Server，请求「画出第 1 条关键路径」。请按时间顺序，列出这条请求从字节进入到画面刷新所经过的每一个函数与跨线程交接点，并指出其中哪些步骤依赖于布线阶段产出的数据。

**要求**：

1. 从 [gateio.cpp:L247-L296](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/server/gateio.cpp#L247-L296) 的 IO 循环起步，经过 `handle_telegrams` → `Task` 构造 → `m_received_tasks`。
2. 跨过 `m_tasks_mutex` 到主线程 [serverupdate.cpp:L11-L45](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/server/serverupdate.cpp#L11-L45) 的 `server::update`。
3. 进入 [taskresolver.cpp:L124-L163](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/server/taskresolver.cpp#L124-L163) 的 `process_draw_critical_path_task`，指出它如何设置 `ServerContext.crit_path_element_indexes` 并切换 GUI 控件。
4. 回答：这一路用到的「关键路径数据」最初是哪个布线后阶段、在哪一行代码存进 `ServerContext` 的？（提示：[vpr_api.cpp:L1059-L1064](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1059-L1064)。）
5. 进阶：如果这条关键路径上的某个连接当初是用 `--enable_parallel_connection_router on` 布出来的，MultiQueue 的「松弛」可能让该路径**不是严格最短**——结合 [multi_queue_d_ary_heap.tpp:L194-L253](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/multi_queue_d_ary_heap.tpp#L194-L253) 解释这对「画出来的关键路径」可信度的影响。

**预期产出**：一张含「线程归属」列的调用链表，标注两次 `m_tasks_mutex` 临界区，并写明对布线结果的依赖点。

## 6. 本讲小结

- **Server 模式**采用「IO 线程收发字节 + 主线程 Qt 定时器每 100ms 求解」的双线程模型，`Task` 是唯一交接棒，两把 `m_tasks_mutex` 临界区隔开两条线程；整个模块由 `NO_SERVER` 宏在编译期门控，依赖 GUI 构建。
- Server 目前只有两种命令（取/画关键路径），求解依赖布线阶段填入 `ServerContext` 的 `timing_info` 与 `routing_delay_calc`，未就绪则任务挂起不响应。
- **网表级并行**（`--router_algorithm parallel` → `ParallelNetlistRouter`）用 `PartitionTree` 把边界框不相交的网分组、用 `tbb::task_group` 并行布不同树节点；每线程一份 `SerialConnectionRouter` 与 `RouteIterResults` 以回避共享可变状态；与串行等价且确定，但拥塞时可能降 QoR。
- **连接级并行**（`--enable_parallel_connection_router on` → `ParallelConnectionRouter`）用 `MultiQueueDAryHeap` 把单连接的 A\* 堆拆成多个独立加锁子队列，push 随机入队、pop 从两个随机子队列取较优者（松弛 A\*），靠缓存行对齐 + 原子 `min` + `numIdle` 终止检测实现高并发。
- 两条并行轴**正交且（在当前实现里）不叠加**：选了 `parallel` 算法就走纯按网并行；按连接并行只在 `timing_driven` 的 `SerialNetlistRouter` 内生效。
- 多线程下保护「每节点代价」用**每节点自旋锁**（细粒度、冲突低），而非全局锁——这是连接级并行能真正提速的关键工程细节。

## 7. 下一步学习建议

- 若你对布线主循环还想深入，建议回看 [u6-l2](u6-l2-connection-router.md) 的单线程 A\* 与堆，对比本讲的 MultiQueue，理解「松弛」到底松弛在哪。
- 若你对 Server 背后的 GUI 与断点机制感兴趣，可学 [u8-l4 图形可视化](u8-l4-graphics.md)，二者共享同一套 Qt/EZGL 事件循环与 `application` 全局指针。
- 若想验证并行布线的实际收益，建议阅读 `vpr/src/route/parallel_connection_router.cpp` 与 `DecompNetlistRouter.tpp`（带网分解的并行，`--router_algorithm parallel_decomp`），并用 `run_reg_test.py` 在强回归集上对比串/并行的 QoR 与耗时（注意 CLAUDE.md 要求：测试仅在需要时运行）。
- 若想理解布线结果如何变成 Server 可查的关键路径，衔接 [u7-l2](u7-l2-delay-calc-and-reports.md) 的 `calc_critical_path` 与 `timing_reports`。
