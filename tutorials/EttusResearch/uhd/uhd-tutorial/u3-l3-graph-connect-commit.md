# 流图连接与 commit

## 1. 本讲目标

上一讲（u3-l2）我们已经知道：RFNoC 块控制器（`noc_block_base`）由工厂按 NoC ID 制造、由 `block_id_t` 寻址。但「块被造出来」和「块能互相传样本」是两回事——你还得把它们**连起来**，并**提交（commit）**这张连接图，框架才会去做拓扑校验、属性传播与物理路由。

学完本讲，你应当能够：

1. 说清 `rfnoc_graph::connect` 的三类重载（块-块、TX 流器-块、块-RX 流器）各自做了什么，以及「物理连接」与「逻辑连接」两步的差异。
2. 解释 `commit()` 触发的两件事——拓扑校验 `_check_topology()` 与属性传播 `resolve_all_properties()`——以及它们失败时抛什么异常。
3. 理解 `release()` / `commit()` 的引用计数语义，并能用 `release → 改图 → commit` 完成一次安全的重新配置。
4. 区分 `block_container`（「图里有哪些顶点」）与 `graph_t`（「顶点之间有哪些边」）这两套独立存储。

---

## 2. 前置知识

本讲默认你已掌握 u3-l1、u3-l2 的内容，下面只做最简回顾：

- **RFNoC 流图**：FPGA 内部的块（Radio/DDC/FFT…）经流端点（SEP）由 crossbar 互连，样本以 CHDR 包搬运。软件用一张「逻辑图」描述块与块、块与主机流器之间的连接。
- **block_id**：形如 `0/Radio#0`、`0/DDC#0` 的三段式软件地址（设备/块名/实例号）。
- **noc_block_base**：所有块控制器的基类，同时是一个 `node_t`（带属性系统）。
- **静态连接 vs 动态连接**：静态连接（STATIC）在 FPGA 综合时「焊死」；动态连接（DYNAMIC）在运行时经 crossbar 路由。两类连接在软件里**都要**用 `connect()` 显式声明，否则不参与属性传播。

本讲会反复用到两个图论概念，先说人话：

- **有向图（BGL graph）**：UHD 用 Boost Graph Library（BGL）在内存里维护一张有向图。块是「顶点（vertex）」，连接是「边（edge）」。边的属性就是 `graph_edge_t`（源端口、目的端口、边类型、是否前向边）。
- **拓扑排序（topological sort）**：把有向无环图（DAG）里的顶点排成一个线性顺序，保证「所有边都从前往后」。属性传播就按这个顺序逐块推进；如果图里有环，拓扑排序会失败——这就是回环（back-edge）必须显式标记的原因。

---

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `host/include/uhd/rfnoc_graph.hpp` | 公共抽象基类 `rfnoc_graph`，声明 `connect`/`commit`/`release`/`disconnect` 等纯虚接口。 |
| `host/lib/rfnoc/rfnoc_graph.cpp` | 公共实现 `rfnoc_graph_impl`：把 `connect` 拆成「物理连 + 逻辑连」两步，持有 `_block_registry` 与 `_graph` 两个成员。 |
| `host/lib/rfnoc/graph.cpp` | **细节图** `graph_t::impl` 的实现：用 BGL 维护顶点与边，执行真正的「加边、去重、拓扑校验、属性传播」。 |
| `host/include/uhd/rfnoc/detail/graph.hpp` | 细节图 `graph_t` 的类声明（pimpl，对外只暴露 `connect/commit/release/...`）。 |
| `host/lib/rfnoc/block_container.cpp` | 块容器 `block_container_t`：用 `unordered_set` 存所有块控制器，提供注册/查找。 |
| `host/include/uhd/rfnoc/graph_edge.hpp` | 边结构 `graph_edge_t`：四种边类型枚举与 `is_forward_edge` 字段。 |
| `host/include/uhd/utils/graph_utils.hpp` | 便捷工具 `connect_through_blocks` / `get_block_chain`，自动串起一串静态连接。 |
| `host/examples/rfnoc_rx_to_file.cpp` | 真实示例：Radio → DDC → rx_streamer 的端到端建图、commit、收流。 |

> 一个容易混淆的点：仓库里有两个「graph」文件。`rfnoc_graph.cpp` 是**公共 API** 的实现（用户调的 `graph->connect(...)`），`graph.cpp` 是它内部委托的**细节图**（真正干活的那层）。本讲会始终区分「公共层」与「细节层」。

---

## 4. 核心概念与源码讲解

### 4.1 块容器 block_container：图的「顶点仓库」

#### 4.1.1 概念说明

在讲「怎么连」之前，先解决一个更基础的问题：**这张图里到底有哪些顶点（块）？**

`rfnoc_graph_impl` 把这个问题单独交给一个叫 `block_container_t` 的类。它的职责非常窄——**只负责存「块控制器存在不存在」，完全不关心块与块之间的连接**。连接关系是另一套存储（下一节的 `graph_t`）的事。

这种「存在性」与「连接关系」分离的设计带来一个重要后果：

> 一个块可以**已经注册在容器里**（`has_block` 返回 true），却**还没被加进逻辑图**（没被任何 `connect` 引用过）。只有当 `connect()` 真正触碰到它时，它才会作为顶点被加入 BGL 图。

这也是为什么公共头文件里反复强调：哪怕是 FPGA 里焊死的静态连接，你也必须显式 `connect()` 一次，否则它「不参与属性传播」。

#### 4.1.2 核心流程

`block_container_t` 内部就一个 `std::unordered_set<noc_block_base::sptr>` 加一把互斥锁，提供五个操作：

```
register_block(blk)  → 把块指针塞进 unordered_set
find_blocks(hint)    → 遍历集合，用 block_id_t::match(hint) 过滤，结果按字典序排序
has_block(id)        → any_of：是否存在 block_id 等于 id 的块
get_block(id)        → find_if：取出指针；找不到抛 lookup_error
init_props() / shutdown() → 对每个块调用 node_accessor 的初始化/关闭
```

注意 `find_blocks` / `has_block` / `get_block` 这三个方法在公共层是 `rfnoc_graph` 的纯虚函数，而 `rfnoc_graph_impl` 直接把它们**原样转发**给 `_block_registry`（细节见 4.1.3）。也就是说，用户视角的 `graph->get_block("0/DDC#0")` 其实就是查这个容器。

#### 4.1.3 源码精读

容器声明在内部头文件，核心就是一个 `unordered_set`：

[host/lib/include/uhdlib/rfnoc/block_container.hpp:55-61](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/include/uhdlib/rfnoc/block_container.hpp#L55-L61) — `_blocks` 是真正的块注册表，用 `std::mutex` 保护并发访问。

注册时只是 `insert`：

[host/lib/rfnoc/block_container.cpp:31-38](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/block_container.cpp#L31-L38) — `register_block` 把块指针插入集合并打一条 DEBUG 日志（含 unique id 与 NoC ID）。

查找与获取是线性扫描 + 字典序排序：

[host/lib/rfnoc/block_container.cpp:40-57](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/block_container.cpp#L40-L57) — `find_blocks` 用 `block_id_t::match(hint)` 模糊匹配（u3-l2 讲过的「缺省即通配」），空 hint 表示「全部」，最后 `std::sort` 保证返回顺序确定。

[host/lib/rfnoc/block_container.cpp:68-79](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/block_container.cpp#L68-L79) — `get_block` 找不到时抛 `uhd::lookup_error`，这正是公共层 `get_block<T>` 模板里「块不存在或类型不符」异常的源头。

公共层的转发极薄：

[host/lib/rfnoc/rfnoc_graph.cpp:144-147](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/rfnoc_graph.cpp#L144-L147) — `rfnoc_graph_impl::get_block` 一行 `_block_registry->get_block(block_id)` 完事，说明「找块」纯粹是容器职责。

而 `_block_registry` 与 `_graph` 是 `rfnoc_graph_impl` 里**并列的两个成员**：

[host/lib/rfnoc/rfnoc_graph.cpp:1046-1059](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/rfnoc_graph.cpp#L1046-L1059) — `_block_registry`（顶点仓库）与 `_graph`（边仓库）分工明确：前者回答「谁存在」，后者回答「谁连谁」。

#### 4.1.4 代码实践

**目标**：理解「块存在」与「块入图」是两件事。

**操作步骤**（源码阅读型）：

1. 打开 `host/lib/rfnoc/rfnoc_graph.cpp`，看构造函数里块的注册链路：`_init_blocks`（L696 起）遍历每块主板，从 Client Zero 读出 NoC ID、端口数，调工厂造块，再 `_block_registry->register_block(...)`（L778）。
2. 注意：**注册发生在构造期**，此时 `connect()` 还没被用户调用过。
3. 再看细节图的 `_add_node`：

[host/lib/rfnoc/graph.cpp:787-794](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/graph.cpp#L787-L794) — 只有当某个块被 `connect()` 触及时，它才经 `_add_node` 作为顶点加入 BGL 图。

**需要观察的现象**：注册阶段所有块都进了 `_block_registry`，但 BGL 图 `_graph` 此时是空的（没有任何顶点/边）。顶点是「按需加入」的。

**预期结果**：你能用自己的话回答——「`has_block("0/FFT#0")` 返回 true，不代表 FFT 块已经在逻辑图里；只有对它调过 `connect()`，它才在图里。」

#### 4.1.5 小练习与答案

**练习 1**：`block_container_t` 为什么用 `unordered_set` 而不是 `map<block_id_t, sptr>`？

> 参考答案：`unordered_set` 直接以块指针为键，避免重复存 `block_id_t`；查找时用 `get_block_id()` 现算比较。容器只关心「存在性」与去重，`block_id` 是块的属性而非外部键，这种设计更简单。代价是查找是 O(n) 线性扫描，但块数量通常很小，可接受。

**练习 2**：如果两次调用 `graph->get_block("0/Radio#0")`，会创建两个块控制器对象吗？

> 参考答案：不会。`get_block` 只是从容器里取出已注册的 `shared_ptr`，返回的是同一个块控制器对象的共享指针。块控制器在构造期由工厂创建一次，之后只读不写。

---

### 4.2 graph::connect：三类重载与「物理连 + 逻辑连」两步

#### 4.2.1 概念说明

`connect` 是本讲的绝对主角。公共头声明了**三个** `connect` 重载，对应 RFNoC 流图里三类边：

| 重载 | 方向 | 边类型 |
| --- | --- | --- |
| `connect(src_blk, src_port, dst_blk, dst_port)` | 块 → 块 | STATIC 或 DYNAMIC |
| `connect(tx_streamer, strm_port, dst_blk, dst_port)` | 主机 TX 流器 → 块 | TX_STREAM |
| `connect(src_blk, src_port, rx_streamer, strm_port)` | 块 → 主机 RX 流器 | RX_STREAM |

为什么块-块连接要分 STATIC/DYNAMIC 两种？因为 FPGA 里的物理通路有两种来源：

- **静态连接**：综合时焊死（比如 Radio 的输出直连 DDC 的输入），样本走的是固定线，软件无需配置 crossbar。
- **动态连接**：运行时由软件在两个 SEP 之间建一条 crossbar 路由。

但无论哪种，**软件侧都得在逻辑图里登记一条边**，否则属性传播不知道这两块是连通的。所以 `connect` 的本质是「**物理连** + **逻辑连**」两步合一：

1. **物理连**（`_physical_connect`）：查静态边表判断是 STATIC 还是 DYNAMIC；若是 DYNAMIC，调 `_gsm->create_device_to_device_data_stream` 在 FPGA crossbar 上真正建路；若是连流器，则建主机↔设备的数据传输流。
2. **逻辑连**（`_graph->connect`）：把这条边加进 BGL 图，设置属性传播回调和 action 回调。

> 关键直觉：**物理连管「样本真能不能流过去」，逻辑连管「属性传播的依赖图」**。两者必须同时成功，`connect` 才算完成。

#### 4.2.2 核心流程

以最常用的「块 → 块」重载为例，公共层 `rfnoc_graph_impl::connect` 的流程：

```
connect(src_blk, src_port, dst_blk, dst_port, is_back_edge=false):
  1. has_block(src_blk)? has_block(dst_blk)?       // 不存在抛 lookup_error
  2. edge_type = _physical_connect(...)             // 物理连，返回 STATIC 或 DYNAMIC
       └─ _get_route_info: 查 _static_edges
            · 若 src 与 dst 在静态边表里直接相连     → STATIC
            · 否则要求 src 输出接 SEP、dst 输入接 SEP → DYNAMIC，建 crossbar 路由
            · 都不满足                              → 抛 routing_error
  3. _connect(src, src_port, dst, dst_port, edge_type, is_back_edge)
       └─ 构造 graph_edge_t，调 _graph->connect(...)   // 进入细节层
```

细节层 `graph_t::impl::connect` 接管后，做四件事：

```
_graph->connect(src_node, dst_node, edge_info):
  1. 锁 recursive_mutex；回填 edge_info 的 src/dst blockid
  2. _add_node(src)、_add_node(dst)                  // 顶点按需入图
  3. 给两端块挂三套回调：
       · set_resolve_all_callback  → 触发整图属性传播
       · set_graph_mutex_callback  → 让块能拿到同一把图锁
       · set_post_action_callback  → 把 action 入队（异步消息级联）
  4. 边冲突检测（见下）+ boost::add_edge + 环路检测
```

**边冲突检测**是细节层最值得读的一段，它定义了 connect 的「幂等与禁止」规则：

- 若完全相同的边已存在 → 记 INFO 日志，**直接返回**（幂等，重复 connect 同一条边不报错）。
- 若 src 的同一输出端口已连到**别的**目的 → 抛 `rfnoc_error`（「Attempting to reconnect output port」）。**一个输出端口只能有一条出边**。
- 若 dst 的同一输入端口已有入边 → 抛 `rfnoc_error`（「Attempting to reconnect input port」）。**一个输入端口只能有一条入边**。
- 加完边后跑一次拓扑排序，若发现意外成环 → 撤销这条边并抛错；想合法地构成回环，必须把该边标为 back-edge（`is_back_edge=true`）。

#### 4.2.3 源码精读

公共层三个重载的声明：

[host/include/uhd/rfnoc_graph.hpp:190-194](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc_graph.hpp#L190-L194) — 块→块重载，`is_back_edge` 默认 false。

[host/include/uhd/rfnoc_graph.hpp:207-211](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc_graph.hpp#L207-L211) — TX 流器→块重载，额外带 `adapter_id`（选哪条主机传输链路）。

[host/include/uhd/rfnoc_graph.hpp:224-228](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc_graph.hpp#L224-L228) — 块→RX 流器重载，这是我们实践任务里要用的那一个。

公共层「块→块」实现，清晰展示「物理连 + 逻辑连」两步：

[host/lib/rfnoc/rfnoc_graph.cpp:225-248](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/rfnoc_graph.cpp#L225-L248) — 先 `has_block` 双检，再 `_physical_connect(...)` 拿到 `edge_type`，最后 `_connect(...)` 进入逻辑层。

物理连的路由判定逻辑：

[host/lib/rfnoc/rfnoc_graph.cpp:870-927](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/rfnoc_graph.cpp#L870-L927) — `_get_route_info` 先查 src 与 dst 的静态边；若两端在静态表里直接对接则 `STATIC`，否则要求两端各自接到 SEP（否则 `routing_error`）。

[host/lib/rfnoc/rfnoc_graph.cpp:936-961](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/rfnoc_graph.cpp#L936-L961) — `_physical_connect`：仅当 `DYNAMIC` 时，才调 `_gsm->create_device_to_device_data_stream` 在两个 SEP 之间真正建路。

逻辑连的薄封装：

[host/lib/rfnoc/rfnoc_graph.cpp:850-861](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/rfnoc_graph.cpp#L850-L861) — `_connect` 构造 `graph_edge_t(src_port, dst_port, edge_type, not is_back_edge)`（注意 `is_forward_edge = not is_back_edge`），再 `_graph->connect(...)`。

「块 → RX 流器」重载，演示流器连接如何建主机传输流：

[host/lib/rfnoc/rfnoc_graph.cpp:335-394](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/rfnoc_graph.cpp#L335-L394) — 校验 src 输出端接了 SEP，从 `_sep_map` 取 SEP 地址，调 `_gsm->create_device_to_host_data_stream` 建传输流并 `connect_channel`，最后把 `RX_STREAM` 边加入逻辑图。

细节层的核心 `connect`，重点看回调和边冲突检测：

[host/lib/rfnoc/graph.cpp:101-147](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/graph.cpp#L101-L147) — 加节点、挂三套回调。注意 `set_resolve_all_callback` 绑定的 lambda 最终会调 `resolve_all_properties`，这是 4.3 节属性传播的入口。

[host/lib/rfnoc/graph.cpp:149-199](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/graph.cpp#L149-L199) — 边冲突检测：幂等返回、重连输出端口、重连输入端口三种情形。

[host/lib/rfnoc/graph.cpp:201-219](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/graph.cpp#L201-L219) — `boost::add_edge` 真正加边，随后 `_get_topo_sorted_nodes()` 验证没有意外成环（前向边不允许构成环；要构成回环必须标 back-edge）。

边类型与 `is_forward_edge` 字段：

[host/include/uhd/rfnoc/graph_edge.hpp:25-30](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/graph_edge.hpp#L25-L30) — 四种边类型枚举。

[host/include/uhd/rfnoc/graph_edge.hpp:52-54](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/graph_edge.hpp#L52-L54) — `is_forward_edge=false` 表示回边，拓扑排序时会用 `ForwardEdgePredicate` 把它过滤掉，从而允许反馈环路。

#### 4.2.4 代码实践

**目标**：体会「重复 connect 同一条边」是安全的，而「一个输出端口连两个目的」会被拒。

**操作步骤**（源码阅读型 + 伪代码）：

1. 在 [host/lib/rfnoc/graph.cpp:156-162](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/graph.cpp#L156-L162) 处确认：完全相同的边（`existing_edge_info == edge_info`）只打 `Ignoring repeated call` 日志并返回。
2. 写一段伪代码（**示例代码**，非项目原有）模拟两种情形：

```cpp
// 示例代码：仅供理解 connect 的幂等与冲突
// (1) 幂等：重复连同一条边 → 安全，框架忽略
graph->connect(radio_id, 0, ddc_id, 0);
graph->connect(radio_id, 0, ddc_id, 0); // 仅打 INFO 日志，不抛异常

// (2) 冲突：radio 输出端口 0 已经连到 ddc，再连到 fft → 抛 rfnoc_error
graph->connect(radio_id, 0, fft_id, 0); // "Attempting to reconnect output port!"
```

**需要观察的现象**：情形 (1) 不会改变图；情形 (2) 抛 `uhd::rfnoc_error`，且因为 `_physical_connect` 已经在 crossbar 上建过路，抛异常前物理层的状态需结合 GSM 实现理解（**待本地验证**：是否留下半成品路由）。

**预期结果**：能复述「一个输出端口只能有一条出边、一个输入端口只能有一条入边」这条 RFNoC 图的硬约束。

#### 4.2.5 小练习与答案

**练习 1**：为什么静态连接（FPGA 里已经焊死）还要调 `connect()`？

> 参考答案：静态连接只保证样本在硬件里能流过去，但软件的逻辑图（BGL graph）并不知道这条边的存在。属性传播、拓扑校验、`enumerate_active_connections`、`to_dot` 都依赖逻辑图。只有显式 `connect()`，框架才会把这条边登记进图，属性（如采样率、格式）才能在两端块之间传播。公共头注释里也明确说了这一点。

**练习 2**：`is_back_edge=true` 的边有什么特殊待遇？

> 参考答案：它的 `is_forward_edge` 被置为 false。在拓扑排序（`_get_topo_sorted_nodes`）和脏节点查找（`_find_dirty_nodes`）时，细节图用 `ForwardEdgePredicate` / `DirtyNodePredicate` 把回边过滤掉，使图仍可当作 DAG 处理。这样允许用户构造反馈环路（如某块的输出回送给自己或上游块）而不会触发「Cannot resolve graph because it has at least one cycle」错误。

---

### 4.3 graph::commit：拓扑校验与属性传播点火

#### 4.3.1 概念说明

`connect` 只是把边一条条加进图，**图此时还没「生效」**。要让框架真正去检查这张图、并把块与块之间的属性（采样率、数据格式、DSP 参数…）沿着边传播开去，必须显式调用 `commit()`。

`commit()` 干两件事：

1. **拓扑校验** `_check_topology()`：对图里每个块，收集它实际被连上的输入/输出端口集合，交给块自己的 `check_topology()` 判断是否合法（比如某块要求「端口 0 和端口 1 必须同时连或同时不连」）。任何一块不合法 → 抛 `runtime_error("Graph topology is not valid!")`。
2. **属性传播** `resolve_all_properties(INIT, ...)`：按拓扑序逐块调用 `resolve_props`，把脏属性（dirty properties）沿边前向、后向传播，直到收敛（没有脏属性）或达到固定迭代次数。若收敛后仍有脏属性 → 抛 `resolve_error("Could not resolve properties.")`。

属性传播用一个**引用计数** `_release_count` 控制：只有当计数归零时，`commit` 才会真正跑拓扑校验与整图传播；否则只做「局部 resolve」。这给了 `release → 改图 → commit` 的重新配置模式一个干净的开关。

\[ \text{属性传播是否启用} \iff \text{release\_count} = 0 \]

其中 `release()` 让计数 +1，`commit()` 让计数 -1。

#### 4.3.2 核心流程

细节层 `graph_t::impl::commit`：

```
commit():
  lock
  if _release_count > 0: _release_count--          // 配对 release
  if _release_count == 0:
      _check_topology()                             // (1) 拓扑校验
      resolve_all_properties(INIT, 第一个顶点)        // (2) 整图属性传播
```

属性传播主循环（`_resolve_all_properties`）的骨架：

```
1. 拓扑排序得到顶点线性序
2. 从 initial_node 开始，在序列里来回扫描（forward / backward）：
     对每个块：resolve_props() → 沿边 _forward_edge_props() → clean_props()
3. 收敛条件：_find_dirty_nodes() 为空，或达到 MAX_NUM_ITERATIONS(=2)
4. 收尾检查：若仍有脏属性 → 抛 resolve_error
```

> 为什么硬编码 2 次迭代？源码注释解释得很直白：首次传播时，块可能会动态创建新的、默认为脏的边属性；无法预知何时发生，索性固定跑 2 轮就能覆盖这种情况。

#### 4.3.3 源码精读

公共层 `commit` / `release` 同样是薄转发，但 `commit` 多打一张 dot 图便于调试：

[host/lib/rfnoc/rfnoc_graph.cpp:584-593](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/rfnoc_graph.cpp#L584-L593) — `commit` 转 `_graph->commit()` 并 TRACE 打印 dot 图；`release` 转 `_graph->release()`。

细节层 `commit` 与引用计数：

[host/lib/rfnoc/graph.cpp:287-298](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/graph.cpp#L287-L298) — 这就是上面流程图的逐行实现。注意 `_release_count` 归零时才执行 `_check_topology()` + `resolve_all_properties(...)`。

[host/lib/rfnoc/graph.cpp:300-305](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/graph.cpp#L300-L305) — `release` 只做 `_release_count++`，所以 `commit`/`release` 必须配对。

拓扑校验：

[host/lib/rfnoc/graph.cpp:906-976](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/graph.cpp#L906-L976) — 遍历每个顶点，用 `boost::in_edges`/`out_edges` 收集已连的输入/输出端口，调 `node_accessor.check_topology(node, inputs, outputs)`；任一块失败则 `topo_ok=false`，最终抛 `runtime_error("Graph topology is not valid!")` 并把「请求端口 vs 合法端口」写进错误日志。

属性传播入口（含「图未提交时只做局部 resolve」的分支）：

[host/lib/rfnoc/graph.cpp:402-429](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/graph.cpp#L402-L429) — 若 `_release_count` 非零（图未提交），只对当前节点做局部 `resolve_props`+`clean_props`，不做跨边传播；否则先 forward 再 backward 全图传播。

属性传播主循环与收敛判定：

[host/lib/rfnoc/graph.cpp:432-559](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/graph.cpp#L432-L559) — `MAX_NUM_ITERATIONS = 2`（L476），来回扫描逐块 resolve+forward+clean；收尾（L544-558）若仍有脏节点，逐条列出脏属性并抛 `resolve_error`。

#### 4.3.4 代码实践

**目标**：体验 commit 前后的差异——commit 前属性传播被禁用，commit 后属性才被解析。

**操作步骤**（源码阅读型）：

1. 打开真实示例 `rfnoc_rx_to_file.cpp`，定位建图与 commit 的位置：

[host/examples/rfnoc_rx_to_file.cpp:539-547](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rfnoc_rx_to_file.cpp#L539-L547) — `create_rx_streamer` → `connect(last_block, port, rx_stream, 0)` → `commit()` → 打印 `enumerate_active_connections()`。

2. 注意示例紧接着在 **commit 之后** 才设置频率与采样率：

[host/examples/rfnoc_rx_to_file.cpp:549-593](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rfnoc_rx_to_file.cpp#L549-L593) — 代码注释明说「We do this after commit() so we can use the property propagation」。

**需要观察的现象**：示例故意把设频率/采样率放在 commit 之后，因为只有这样，设置在 rx_streamer 上的 tune request 才能经属性传播作用到 Radio/DDC；DDC 的 `set_output_rate` 也才能在整图连通后被正确解析与回读。

**预期结果**：你能解释——「commit 前，块的属性彼此隔离；commit 后，整图属性连成一体，读回的 actual rate 才是传播后的真实值。」

> 说明：若本地无 RFNoC 硬件，无法实跑；以上为源码阅读型实践，结论来自对 `resolve_all_properties` 与示例时序的直接阅读。

#### 4.3.5 小练习与答案

**练习 1**：如果两个块的采样率属性在传播后无法收敛（互相矛盾），`commit()` 会怎样？

> 参考答案：`_resolve_all_properties` 收尾的脏节点检查会失败（`remaining_dirty_nodes` 非空），框架把每个无法解析的脏属性写入 ERROR 日志，然后抛 `uhd::resolve_error("Could not resolve properties.")`。这个异常会一路冒泡到用户的 `commit()` 调用处。

**练习 2**：为什么 `_resolve_all_properties` 里 `MAX_NUM_ITERATIONS` 取 2 而不是 1？

> 参考答案：首次传播时，块可能在 `resolve_props` 中动态创建新的边属性，这些新属性默认是脏的。固定跑 2 轮能在不增加复杂判断的前提下覆盖「第一轮产生新脏属性、第二轮把它解析掉」的情形。源码注释（L470-476）对此有明确说明。

---

### 4.4 release 与重新配置：引用计数与边的管理

#### 4.4.1 概念说明

很多应用并不是「建一次图跑到底」——运行中可能需要换采样率、换频率、甚至换一条信号链路。这就要求**安全地改图**。RFNoC 提供 `release()` / `commit()` 的引用计数机制来支持这件事：

- `release()` 让 `_release_count++`，暂时**关闭整图属性传播与 action 传播**。
- 在「释放」期间，你可以 `disconnect` 旧边、`connect` 新边，而不会触发反复的属性重算。
- 改完后 `commit()` 让 `_release_count--`；当它回到 0，框架重新跑 `_check_topology()` + 整图 `resolve_all_properties()`，相当于「重新生效」这张图。

需要注意的是 `disconnect` 的两层语义：

- **逻辑断开**：一定执行——把边从 BGL 图里移除（`boost::remove_out_edge_if`），并在节点度数归零时摘掉它的回调。
- **物理断开**：对 DYNAMIC 块-块边，当前实现是 `TODO`（源码里写明尚未实现）；对 RX 流器，物理断开发生在上游源端口被重新连接时；对 TX 流器，发生在流器析构或端口改连时。

> 直觉：`release → disconnect/connect → commit` 是 RFNoC 的「事务式改图」三段式。release 像「开始事务」，commit 像「提交事务并重新校验」。

#### 4.4.2 核心流程

重新配置的标准三段式：

```
graph->release();                       // ① 关闭传播，进入"改图"窗口
graph->disconnect(src, p, dst, p);      // ② 逻辑断开旧边（物理层视类型而定）
graph->connect(new_src, p, new_dst, p); //   连接新边
graph->commit();                        // ③ 重新拓扑校验 + 整图属性传播
```

细节层 `disconnect` 的流程：

```
disconnect(src_node, dst_node, edge_info):
  lock
  若两端都不在图里 → 直接返回
  boost::remove_out_edge_if: 删除匹配 src 端口的边
  若 src 度数归零 → _remove_node(src)，清掉它的 resolve/mutex/action 回调
  若 dst 度数归零 → 同上
```

注意一个细节：节点被移除会改变 BGL 的顶点描述符，所以 `_remove_node` 之后要**重建 `_node_map`**（见源码 L796-817）。

#### 4.4.3 源码精读

公共头对 `disconnect` 语义的说明（逻辑断开 vs 物理断开）：

[host/include/uhd/rfnoc_graph.hpp:230-246](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc_graph.hpp#L230-L246) — 注释明确：「This will logically disconnect the blocks, but the physical connection will not be changed until a new connection is made on the source port」。

[host/include/uhd/rfnoc_graph.hpp:248-269](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc_graph.hpp#L248-L269) — 流器版的 `disconnect`：RX 流器物理断开发生在上游源端口被改连时；TX 流器发生在析构或改连时。

公共层块-块 disconnect，注意它调用 `_physical_disconnect`：

[host/lib/rfnoc/rfnoc_graph.cpp:250-272](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/rfnoc_graph.cpp#L250-L272) — 先 `_physical_disconnect`（对 DYNAMIC 边目前是 TODO），再 `_graph->disconnect(...)` 做逻辑断开。

物理断开的 TODO 现状：

[host/lib/rfnoc/rfnoc_graph.cpp:970-982](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/rfnoc_graph.cpp#L970-L982) — `_physical_disconnect` 对 DYNAMIC 边只留了一句 `// TODO: Add call into _gsm to physically disconnect the SEPs`，说明物理层尚不会主动拆路。

细节层 disconnect 的边移除与节点清理：

[host/lib/rfnoc/graph.cpp:222-279](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/graph.cpp#L222-L279) — `remove_out_edge_if` 删边；度数归零时 `_remove_node` 并清空三套回调（resolve_all、graph_mutex、post_action）。

节点移除后重建 node_map：

[host/lib/rfnoc/graph.cpp:796-817](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/graph.cpp#L796-L817) — `boost::remove_vertex` 会改变顶点描述符，所以遍历全图重建 `_node_map`，注释也提醒「Removing the vertex changes the vertex descriptors」。

#### 4.4.4 代码实践

**目标**：用三段式改图，并理解 release 期间属性传播被暂停。

**操作步骤**（伪代码，**示例代码**非项目原有）：

```cpp
// 示例代码：事务式改图
graph->release();                  // 进入改图窗口：_release_count = 1，传播暂停
graph->disconnect(ddc_id, 0, rx_stream_id, 0); // 逻辑断开 DDC->RX
graph->connect(fft_id, 0, rx_stream_id, 0);    // 改连 FFT->RX（需先建 FFT->...链）
graph->commit();                   // _release_count = 0，重跑 check_topology + 属性传播
```

**需要观察的现象**：在 release 与 commit 之间，任何属性的改动都不会触发跨块传播（对应 `resolve_all_properties` 里 `_release_count` 非零的「只局部 resolve」分支）；只有 `commit()` 把计数减回 0，整图才重新解析。

**预期结果**：能复述「release/commit 是配对的引用计数；改图必须夹在 release 与 commit 之间，否则每次 connect/disconnect 都会触发一次整图重算，既慢又可能在中途出现不一致」。

> 说明：动态块-块边的物理断开当前是 TODO，若你的改图涉及拆除 DYNAMIC 路由，物理层可能不会立即释放 crossbar 资源（**待本地验证**具体设备行为）。

#### 4.4.5 小练习与答案

**练习 1**：连续调用两次 `release()` 却只 `commit()` 一次，属性传播会恢复吗？

> 参考答案：不会。`release()` 让 `_release_count` 从 0 变 1 再变 2；一次 `commit()` 只减到 1。由于「属性传播启用 ⇔ release_count=0」，此时传播仍被禁用。必须再 `commit()` 一次让计数归零，才会重新跑拓扑校验与整图传播。所以 release 与 commit 必须**严格配对**。

**练习 2**：`disconnect` 之后，被摘除的块控制器对象会立刻销毁吗？

> 参考答案：不会。块控制器由 `block_container_t` 的 `unordered_set<sptr>` 持有（`_block_registry`），其生命周期与图无关。`disconnect` 只是把边从 BGL 图移除、并在节点度数归零时清掉它在图里的回调和顶点；块对象本身仍在容器里，`has_block` 仍返回 true，直到整个 `rfnoc_graph` 析构。

---

## 5. 综合实践

把本讲四个模块串起来，完成实践任务：**编写一个 RFNoC 流图伪代码 Radio → DDC → rx_streamer，调用 commit，并解释提交前后的差异。** 我们以真实示例 `rfnoc_rx_to_file.cpp` 为蓝本，但简化成最小可读形态。

### 5.1 最小流图伪代码

```cpp
// 示例代码：Radio -> DDC -> rx_streamer 的最小建图（参考 rfnoc_rx_to_file.cpp 简化）
#include <uhd/rfnoc_graph.hpp>
#include <uhd/rfnoc/radio_control.hpp>
#include <uhd/rfnoc/ddc_block_control.hpp>
#include <uhd/utils/graph_utils.hpp>

// 1. 建图（复用 device::make 打开设备，u3-l1 讲过）
auto graph = uhd::rfnoc::rfnoc_graph::make("addr=192.168.10.2");

// 2. 取类型化块控制器
uhd::rfnoc::block_id_t radio_id(0, "Radio", 0);
auto radio = graph->get_block<uhd::rfnoc::radio_control>(radio_id);

// 3. Radio -> DDC 通常是一条静态连接，用工具自动串起来
//    get_block_chain 沿静态边遍历，connect_through_blocks 把沿途每条边都 connect 一遍
auto edges = uhd::rfnoc::get_block_chain(graph, radio_id, 0, /*source_chain=*/true);
auto ddc_id   = edges.back().src_blockid;   // 链尾通常是 DDC
auto ddc_port = edges.back().src_port;
uhd::rfnoc::connect_through_blocks(graph, radio_id, 0, ddc_id, ddc_port);
auto ddc = graph->get_block<uhd::rfnoc::ddc_block_control>(ddc_id);

// 4. 建 RX 流器（注意：RFNoC 用 create_rx_streamer，不用 stream_args.channels）
uhd::stream_args_t sargs("sc16", "sc16");
auto rx_stream = graph->create_rx_streamer(1, sargs);

// 5. 把链尾块连到 RX 流器，然后 commit
graph->connect(ddc_id, ddc_port, rx_stream, 0);
graph->commit();
```

### 5.2 工具函数：为什么用 connect_through_blocks

手写一连串静态连接很繁琐（你得知道每段中间块的名字和端口）。`graph_utils.hpp` 提供两个便捷函数：

[host/include/uhd/utils/graph_utils.hpp:48-51](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/utils/graph_utils.hpp#L48-L51) — `get_block_chain`：沿静态边遍历，返回从起始块到终止块（Radio/SEP/NullSrcSink）的边列表，**只读不改图**。

[host/include/uhd/utils/graph_utils.hpp:73-78](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/utils/graph_utils.hpp#L73-L78) — `connect_through_blocks`：找到最短路径，对每段中间连接调 `connect()`（静态的直接 connect，动态的建 SEP 路由）。

真实示例正是这么用的：

[host/examples/rfnoc_rx_to_file.cpp:438-459](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rfnoc_rx_to_file.cpp#L438-L459) — 用 `get_block_chain` + `connect_through_blocks` 自动串起 Radio 之后的所有静态连接，并顺便识别出链里的 DDC。

### 5.3 commit 前后的差异（核心交付）

| 维度 | commit **之前** | commit **之后** |
| --- | --- | --- |
| 逻辑图 | 边已加入 BGL 图 | 同左，但已被校验 |
| 拓扑合法性 | **未校验**（可能连了非法端口组合） | 已通过 `_check_topology`，否则抛 `runtime_error` |
| 属性传播 | **被禁用**（`_release_count>0` 或初始态） | 已跑完 forward+backward 传播，属性已收敛 |
| 读回 actual rate/freq | 可能是未传播的脏值 | 是传播后的真实值（见示例把设速率放在 commit 之后） |
| `enumerate_active_connections` | 能列出已 connect 的边 | 同左，但语义上「图已就绪」 |

把这张表对照示例 [rfnoc_rx_to_file.cpp:539-547](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/examples/rfnoc_rx_to_file.cpp#L539-L547) 的时序看：commit 之后立即 `enumerate_active_connections()` 打印出的连接，正是 commit 校验通过的那张图；例如示例文档里给出的典型输出：

```
Active connections:
  * 0/Radio#0:0==>0/DDC#0:0      # == 表示 STATIC 静态边
  * 0/DDC#0:0-->RxStreamer#0:0   # -- 表示 RX_STREAM 流器边
```

其中 `==>` 与 `-->` 的区分来自 `graph_edge_t::to_string`：

[host/include/uhd/rfnoc/graph_edge.hpp:83-88](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/rfnoc/graph_edge.hpp#L83-L88) — STATIC 用 `==>`，其余（DYNAMIC/RX_STREAM/TX_STREAM）用 `-->`。

> 若本地有 RFNoC 设备，可编译运行 `rfnoc_rx_to_file` 观察上述输出；若无设备，对照源码与示例文档字符串即可理解全部时序——这是「源码阅读型综合实践」。

---

## 6. 本讲小结

- `rfnoc_graph_impl` 内部并行持有 `_block_registry`（块容器，回答「谁存在」）与 `_graph`（细节图，回答「谁连谁」）；块注册在构造期完成，但只有被 `connect()` 触及的块才作为顶点进入 BGL 图。
- `connect` 有三类重载（块-块、TX 流器-块、块-RX 流器），公共层统一拆成「**物理连**（建 crossbar/传输路由，返回 STATIC/DYNAMIC）+ **逻辑连**（加边进 BGL 图）」两步。
- 细节层 `connect` 会给两端块挂三套回调（resolve_all / graph_mutex / post_action），并严格执行边冲突检测：一个输出端口只能有一条出边、一个输入端口只能有一条入边；重复连同一条边是幂等的。
- `commit()` 触发两件事：`_check_topology()` 校验每个块的端口连接是否合法，`resolve_all_properties()` 按拓扑序做 forward+backward 属性传播直到收敛；失败分别抛 `runtime_error` 与 `resolve_error`。
- `release()`/`commit()` 是引用计数开关：`release` 让 `_release_count++` 暂停整图传播，`commit` 让它 `--`，归零时才重新校验与传播。`release → 改图 → commit` 是事务式重新配置的三段式。
- `disconnect` 只保证**逻辑断开**；动态块-块边的物理断开目前是 TODO，流器的物理断开发生在改连或析构时。

---

## 7. 下一步学习建议

本讲聚焦「图的连接与提交」机制。建议接下来：

1. **u3-l4 mb_controller**：本讲的 `commit` 不涉及时间/参考源；多板时间同步与 PPS 由主板控制器负责，它是 RFNoC 设备时间管理的统一入口。
2. **u3-l5 experts 属性传播**：本讲只讲到 `resolve_all_properties` 的调度骨架；属性如何沿边前向/后向传播、脏属性如何触发重算，要进 `expert_container` / `expert_factory` 才能看清。
3. **u3-l6 常用 RFNoC 块**：本讲的 Radio/DDC 只作为连接对象出现；要真正配置它们（设频率、设 DDC 输出速率），需读 `radio_control`、`ddc_block_control` 的具体接口。
4. **u4-l3 VRT 包协议**：本讲的「物理连」最终落到 SEP 间的 CHDR 数据流；要理解样本在传输层如何被打包，需读 `vrt_if_packet` 与 `super_recv_packet_handler`。

阅读源码时，推荐按「公共层 `rfnoc_graph.cpp` → 细节层 `graph.cpp`」的顺序对照，先看用户视角的 `connect`/`commit` 转发，再进 BGL 图看真正干活的部分。
