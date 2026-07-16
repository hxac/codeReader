# VPR 标注子系统（annotation）

## 1. 本讲目标

上一讲（u5-l3）我们追踪了 `link_openfpga_arch` 如何把 `openfpga::Arch` 与 VPR 的 device context「对账」，并把对账结果落到一个叫 `VprDeviceAnnotation` 的结构里。当时我们只是把它当作「中央账本」一笔带过。本讲就要打开这个账本，系统讲解整个 `openfpga/src/annotation/` 目录。

学完本讲，你应当能够：

- 说清 **annotation（标注）模式** 是什么、为什么 OpenFPGA 要用它而不直接改 VPR 源码。
- 识别 `Vpr*Annotation` 家族的六大成员（device / netlist / clustering / placement / routing / bitstream），并按「标注 VPR 结果」还是「构建 OpenFPGA 自有设备结构」给它们分类。
- 理解 `DeviceRRGSB` 如何识别并压缩重复的通用开关块（unique module），以及 `read/write_unique_blocks` 如何把压缩结果缓存到磁盘。
- 理解 `FabricTile` 如何把 grid + switch block + connection block 打包成可复用的 tile。
- 说清 `check_netlist_naming_conflict --fix` 到底解决了什么问题。

## 2. 前置知识

本讲默认你已经读过 u5-l3（`link_openfpga_arch`），知道下列概念：

- **VPR**：OpenFPGA 依赖的布局布线工具，以 git 子模块形式集成（见 u1-l3）。它的核心数据放在一组名为 `*Context`（`DeviceContext`、`PlacementContext`、`ClusteringContext` 等）的全局对象里。
- **不可侵入约束**：VPR 是第三方代码，OpenFPGA 不能也不应该去改它的源码文件。
- **OpenfpgaContext**：OpenFPGA shell 的全局数据中枢（见 u2-l3），命令之间通过它交换数据。
- **rr graph / pb graph**：VPR 用来描述布线资源和逻辑块内部的两种图。

本讲会反复出现一个词：**「以 VPR 对象的指针或 id 为键，平行挂一张副表」**。这就是 annotation 模式的全部精髓，下文会展开。

## 3. 本讲源码地图

本讲涉及的关键文件全部位于 `openfpga/src/annotation/`：

| 文件 | 作用 |
| --- | --- |
| `vpr_device_annotation.h/.cpp` | 最大的标注账本：把 VPR 的 `t_pb_type*`、`RRSwitchId` 等键映射到电路模型 id、physical mode 等 OpenFPGA 信息 |
| `vpr_netlist_annotation.h/.cpp` | 记录 netlist 中被改名的 block / net |
| `vpr_clustering_annotation.h/.cpp` | 记录聚类阶段 net 重映射、truth table 修正、physical_pb 打包结果 |
| `vpr_placement_annotation.h/.cpp` | 记录布局结果：每个 grid 坐标上放了哪些 cluster block |
| `vpr_routing_annotation.h/.cpp` | 记录布线结果：每个 rr_node 上走的 net 与前驱节点 |
| `vpr_bitstream_annotation.h/.cpp` | 记录 pb_type 的比特流来源（eblif 属性 / 固定值）等 |
| `device_rr_gsb.h/.cpp` | 设备级通用开关块（GSB）集合 + unique module 压缩 |
| `fabric_tile.h/.cpp` | 把 grid+sb+cb 聚合成可复用的 fabric tile |
| `annotate_rr_graph.h/.cpp` | 构建 `DeviceRRGSB`、把 rr_switch/rr_segment 绑定到电路模型 |
| `openfpga_annotate_routing.h/.cpp` | 把 VPR 布线结果写入 `VprRoutingAnnotation` |
| `check_netlist_naming_conflict.h/.cpp` + `check_netlist_naming_conflict_template.h` | 检测并修复 netlist 命名冲突 |
| `read_unique_blocks_xml.h` / `read_unique_blocks_bin.h` / `write_unique_blocks_xml.h` / `write_unique_blocks_bin.h` | unique blocks 的 XML / 二进制读写（缓存加速） |

聚合关系在 `openfpga/src/base/openfpga_context.h` 里一目了然——所有上述标注对象都是 `OpenfpgaContext` 的成员。

## 4. 核心概念与源码讲解

### 4.1 annotation 模式与 Vpr\*Annotation 家族

#### 4.1.1 概念说明

OpenFPGA 需要在 VPR 跑出的数据上叠加大量「OpenFPGA 专属信息」，例如：

- 这个 `t_pb_type` 对应哪个电路模型（`CircuitModelId`）？
- 这个布线开关（`RRSwitchId`）该用哪种缓冲电路？
- 这个 rr_node 上最终走的是哪条 net？

但这些信息 **VPR 的数据结构里根本没有字段存放**，而 VPR 又是不能改的第三方代码。怎么办？

OpenFPGA 的答案是 **annotation（标注）模式**：不改 VPR 的类，而是 **以 VPR 对象的指针或强类型 id 为键，在外部平行挂一张「副表」**。需要查「这个 pb_type 对应哪个电路模型」时，就拿 `t_pb_type*` 当 key 去 `VprDeviceAnnotation` 这张 map 里查。

用一个比喻：VPR 是一本不能涂改的教科书，annotation 就是你在书页边上贴的便利贴——书本身一个字没动，但你需要的信息都记在便利贴上，并和书里的段落一一对应。

这种设计带来三个好处：

1. **升级隔离**：VPR 子模块更新时，只要它公开的指针/id 语义没变，OpenFPGA 的便利贴就能继续贴。
2. **关注点分离**：OpenFPGA 的电路级信息不会污染 VPR 的纯结构数据。
3. **生命周期清晰**：VPR 对象由 VPR 管理，便利贴由 OpenFPGA 管理，互不干扰。

#### 4.1.2 核心流程

annotation 的典型使用流程是「命令写入 → 下游命令只读消费」：

```
   某个产生副作用的命令                       下游消费命令
   (link_openfpga_arch / repack / ...)       (build_fabric / build_bitstream / ...)
            │                                          │
            ▼                                          ▼
   mutable_vpr_xxx_annotation()             vpr_xxx_annotation()   ← 只读
   往 map 里塞 <VPR 键, OpenFPGA 值>         用 VPR 键查 OpenFPGA 值
            │                                          │
            └──────────── 都存在 OpenfpgaContext 里 ──┘
```

这与 u2-l3 讲过的「const 访问器（只读）+ mutable 访问器（可写）」一一对应：写入方拿 mutable 引用，读取方拿 const 引用，编译期就杜绝了误写。

#### 4.1.3 源码精读

先看 `OpenfpgaContext` 是怎么聚合这些标注对象的。每个标注都有成对的 const/mutable 访问器：

[openfpga/src/base/openfpga_context.h:75-93](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_context.h#L75-L93) —— const 访问器组：`vpr_device_annotation()`、`vpr_netlist_annotation()`、…、`vpr_bitstream_annotation()`、`device_rr_gsb()`，全部返回 `const T&`。

[openfpga/src/base/openfpga_context.h:140-158](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_context.h#L140-L158) —— 对应的 mutable 访问器组：`mutable_vpr_device_annotation()` 等，返回可写引用 `T&`。

[openfpga/src/base/openfpga_context.h:199-217](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_context.h#L199-L217) —— 真正的成员变量声明。注意第 217 行：

```cpp
openfpga::DeviceRRGSB device_rr_gsb_{vpr_device_annotation_};
```

`DeviceRRGSB` 在构造时就把 `vpr_device_annotation_` 的引用缓存进去（下文 4.3 会用到），所以二者是强绑定的——这也解释了为什么这些标注对象不能脱离 `OpenfpgaContext` 单独复制。

再看 annotation 的「键」长什么样。以家族里最庞大的 `VprDeviceAnnotation` 为例：

[openfpga/src/annotation/vpr_device_annotation.h:39-41](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/vpr_device_annotation.h#L39-L41) —— 类声明，注释里点明了它的四项核心职责（识别 physical/operating pb_type、查电路模型、查 physical pb_type、查 physical mode）。

[openfpga/src/annotation/vpr_device_annotation.h:169-284](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/vpr_device_annotation.h#L169-L284) —— 内部数据，清一色的 `std::map<VPR键, OpenFPGA值>`。挑几条最具代表性的：

- 第 171 行 `std::map<t_pb_type*, t_pb_type*> physical_pb_types_;` —— 以 VPR 的 `t_pb_type*` 为键，记录它对应的 physical pb_type。
- 第 186 行 `std::map<t_pb_type*, CircuitModelId> pb_type_circuit_models_;` —— pb_type → 电路模型 id。
- 第 250 行 `std::map<RRSwitchId, CircuitModelId> rr_switch_circuit_models_;` —— 布线开关 → 电路模型（这条在 4.1.4 实践里会被填充）。
- 第 260 行 `std::map<t_pb_graph_node*, LbRRGraph> physical_lb_rr_graphs_;` —— 把 repack 要用的物理 lb rr graph 挂在 pb graph 头节点上。

> 注意：`vpr_routing_annotation.h`、`vpr_netlist_annotation.h`、`vpr_clustering_annotation.h` 顶部都残留着一段「This is the critical data structure to link the pb_type …」的注释——那是历史复制粘贴留下的陈旧文档，与文件实际职责不符。**判断每个 annotation 真正存什么，要看它的成员变量与访问器，而不是这段注释**（这是读源码时的一个重要习惯：代码是事实，注释可能滞后）。

#### 4.1.4 代码实践

**实践目标**：亲眼看一次「VPR 键 → OpenFPGA 值」是怎么被写入 annotation 的，并把它和下游读取串起来。

**操作步骤**（源码阅读型实践）：

1. 打开 [openfpga/src/annotation/annotate_rr_graph.cpp:1277-1292](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/annotate_rr_graph.cpp#L1277-L1292)，`annotate_rr_graph_circuit_models()` 依次调用三个子函数，分别标注 rr_switch、rr_segment、direct。
2. 跟进第 1137 行 `vpr_device_annotation.add_rr_switch_circuit_model(RRSwitchId(rr_switch_id), circuit_model);`——这一句就是把「VPR 的 `RRSwitchId`」当 key、「OpenFPGA 的 `CircuitModelId`」当 value，塞进 `VprDeviceAnnotation` 的那张 map。
3. 这一步在 `link_openfpga_arch` 里执行（见 [openfpga/src/annotation/annotate_rr_graph.h:38-40](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/annotate_rr_graph.h#L38-L40) 的声明）。
4. 下游 `build_fabric` 构建 routing 模块时，会通过 `rr_switch_circuit_model(rr_switch)` 这个 const 访问器（[vpr_device_annotation.h:91](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/vpr_device_annotation.h#L91)）把同一个 `RRSwitchId` 查回来，拿到该用哪个电路模型实例化。

**需要观察的现象**：整条链路里，`RRSwitchId` 这个 VPR 的 id 是贯穿的「钥匙」——它由 VPR 生成，OpenFPGA 不修改它，只用它做 map 的 key。

**预期结果**：你能画出「`annotate_rr_graph_circuit_models` 写入 → `build_routing_modules` 读取」这条以 `RRSwitchId` 为纽带的读写链。

> 本实践为源码阅读型，未执行编译，行为结论基于源码静态分析。

#### 4.1.5 小练习与答案

**练习 1**：`VprDeviceAnnotation` 为什么用 `std::map<t_pb_type*, …>` 而不是给 `t_pb_type` 加一个 `circuit_model` 字段？

**参考答案**：因为 `t_pb_type` 是 VPR（第三方子模块）的结构体，OpenFPGA 不能改它的定义。用 `t_pb_type*` 作外部 map 的 key，既能在不改 VPR 的前提下挂载 OpenFPGA 信息，又能在 VPR 升级时保持兼容（只要指针语义不变）。

**练习 2**：`mutable_vpr_device_annotation()` 与 `vpr_device_annotation()` 返回类型有何不同？为什么 build_fabric 应该用后者？

**参考答案**：前者返回 `VprDeviceAnnotation&`（可写），后者返回 `const VprDeviceAnnotation&`（只读）。build_fabric 只是消费已有标注，不应再修改它，用 const 版本能在编译期防止误写，符合「写入方与读取方分离」的约定。

---

### 4.2 Vpr\*Annotation 家族分类：标注结果 vs 自有结构

#### 4.2.1 概念说明

`annotation/` 目录里其实混着 **两类截然不同** 的东西，初学者很容易混为一谈：

1. **「标注 VPR 结果」的便利贴**：键是 VPR 对象，值是 OpenFPGA 对 VPR 结果的补充说明或改写记录。这类结构的共同点是——**它们描述的对象本来就是 VPR 算出来的**，OpenFPGA 只是加注。
2. **「构建 OpenFPGA 自有设备结构」的容器**：这些不是给 VPR 贴便利贴，而是 OpenFPGA 自己新建的、VPR 里根本不存在的设备级数据结构（如 GSB 集合、fabric tile）。

区分这两类，是读懂整个目录的钥匙。下面把它们逐一归类。

#### 4.2.2 核心流程

用一张表把六大 `Vpr*Annotation` 与两个设备级结构分到两类里：

| 结构 | 键（VPR 对象） | 存什么 | 归类 |
| --- | --- | --- | --- |
| `VprDeviceAnnotation` | `t_pb_type*`、`RRSwitchId`、`RRSegmentId`… | 电路模型 id、physical mode、mode bits、pin 偏移 | 标注 VPR **device** |
| `VprNetlistAnnotation` | `AtomBlockId`、`AtomNetId` | 改名后的 block/net 名字 | 标注 VPR **netlist** |
| `VprClusteringAnnotation` | `ClusterBlockId`、`t_pb*` | net 重映射、修正后的 truth table、`PhysicalPb` 打包结果 | 标注 VPR **clustering** |
| `VprPlacementAnnotation` | `vtr::Point<size_t>`（grid 坐标） | 每个 grid 位置上的 cluster block 列表 | 标注 VPR **placement** |
| `VprRoutingAnnotation` | `RRNodeId` | rr_node 上的 net、前驱 rr_node | 标注 VPR **routing** |
| `VprBitstreamAnnotation` | `t_pb_type*`、`t_interconnect*` | 比特流来源（eblif 属性/固定值）、默认路径、mode-select 位 | 标注 VPR 结果供 **bitstream** 用 |
| `DeviceRRGSB` | （设备坐标 `(x,y)`） | 整个器件的 GSB 集合 + unique module 压缩 | **OpenFPGA 自有结构** |
| `FabricTile` | `FabricTileId`（OpenFPGA 自产 id） | tile 内含的 pb/cbx/cby/sb 坐标 + unique tile | **OpenFPGA 自有结构** |

一句话总结：名字以 `Vpr` 开头的六个是「便利贴」；`DeviceRRGSB`、`FabricTile` 是「OpenFPGA 自己造的设备图」。下文 4.3、4.4 专门讲这两个自有结构。

#### 4.2.3 源码精读

逐个验证表中的「键」与「值」。

**netlist 标注**——记录改名，键是 VPR 的 atom netlist id：

[openfpga/src/annotation/vpr_netlist_annotation.h:39-42](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/vpr_netlist_annotation.h#L39-L42) —— `block_names_`、`net_names_` 两张 map，把 `AtomBlockId`/`AtomNetId` 映射到新名字。注意它 **不修改** VPR 的 netlist，只是记下「这个 block 该被当成叫这个名字」。

**placement 标注**——比 VPR 多记了「未占用的格子」：

[openfpga/src/annotation/vpr_placement_annotation.h:33-46](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/vpr_placement_annotation.h#L33-L46) —— 注释明确指出：VPR 的 `PlacementContext` 只记录 **已映射** 的 block，而这张标注表 `blocks_` 连 **未映射** 的格子也标成 invalid id 一并保留。这就是「便利贴比原书信息更全」的典型例子，OpenFPGA fabric 生成需要知道每个格子的状态。

**routing 标注**——把布线结果拍平到 rr_node 上：

[openfpga/src/annotation/vpr_routing_annotation.h:41-47](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/vpr_routing_annotation.h#L41-L47) —— `rr_node_nets_`（每个 rr_node 走哪条 net）和 `rr_node_prev_nodes_`（每个 rr_node 的前驱）。它由 [openfpga/src/annotation/openfpga_annotate_routing.h:24-28](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/openfpga_annotate_routing.h#L24-L28) 的 `annotate_vpr_rr_node_nets()` 填充，是后续生成 routing 比特流（哪个布线 mux 选哪条路径）的关键输入。

**clustering 标注**——repack 的产物落在这里：

[openfpga/src/annotation/vpr_clustering_annotation.h:56-67](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/vpr_clustering_annotation.h#L56-L67) —— 第 62 行 `std::map<ClusterBlockId, PhysicalPb> physical_pbs_;` 把 repack 重打包后的物理 pb 挂在 cluster block 上；第 59 行 `block_truth_tables_` 存修正后的真值表（u9-l3 会深入）。

#### 4.2.4 代码实践

**实践目标**：亲手把 `annotation/` 目录里的文件按两类归类，强化「便利贴 vs 自有结构」的直觉。

**操作步骤**（源码阅读型实践）：

1. 用编辑器打开 `openfpga/src/annotation/` 目录。
2. 对每个 `.h`，只看它的 **私有数据成员**（`private:` 下的字段）和 **类名前缀**：
   - 类名以 `Vpr` 开头、键是 VPR 类型（`t_pb_type*`、`RRNodeId`、`ClusterBlockId` 等）→ 归「标注 VPR 结果」。
   - 类名不以 `Vpr` 开头（如 `DeviceRRGSB`、`FabricTile`），或键是 OpenFPGA 自产 id → 归「自有设备结构」。
3. 把结果填进一张两列的表。

**预期结果**：你会得到六条「标注 VPR 结果」（device/netlist/clustering/placement/routing/bitstream）和若干条「自有结构」（DeviceRRGSB、FabricTile，以及 `annotate_*` / `read_unique_blocks` 这类**构建/填充函数**而非数据结构）。注意区分「数据结构」和「填充它的函数」——`annotate_pb_types.cpp`、`annotate_rr_graph.cpp` 等是**动作**，不是容器。

> 本实践为源码阅读型，无需运行命令。

#### 4.2.5 小练习与答案

**练习 1**：`VprPlacementAnnotation` 的注释说它和 VPR 的 `PlacementContext` 有什么区别？为什么要保留这个区别？

**参考答案**：VPR 的 `PlacementContext` 只存已映射的 block，而 `VprPlacementAnnotation` 的 `blocks_` 连未映射的格子（标 invalid id）也保留。因为 OpenFPGA 生成 fabric 时需要对器件里 **每一个** grid 位置都有交代（哪怕它是空的），所以需要一张比 VPR 更全的表。

**练习 2**：`annotate_rr_graph.cpp` 属于哪一类？为什么？

**参考答案**：它属于「填充动作」，不是数据结构也不是容器归类对象。它负责把 rr_switch/rr_segment 的电路模型写进 `VprDeviceAnnotation`、并构建 `DeviceRRGSB`，本身不持有状态。

---

### 4.3 DeviceRRGSB：通用开关块的压缩与缓存

#### 4.3.1 概念说明

`DeviceRRGSB` 是「Device-level Routing Resource General Switch Block」的缩写，它把整个 FPGA 阵列上 **每一个坐标 (x,y)** 的通用开关块（GSB）收集起来。一个 GSB 把该坐标上的三类东西打包到一起：

- 一个 switch block（SB，开关盒，负责布线线段之间的连接）；
- 一个 CHANX 方向的 connection block（CBX，把布线连到逻辑块）；
- 一个 CHANY 方向的 connection block（CBY）。

为什么 OpenFPGA 要自己造这个结构？因为 VPR 的 rr graph 是「为布线算法服务」的扁平图，而 OpenFPGA 生成网表时需要「按模块（SB/CB）组织、能识别重复、能生成 Verilog/SPICE 实例」的视图。`DeviceRRGSB` 就是这个面向网表生成的视图。

它最强大的能力是 **unique module（唯一模块）压缩**：在一个规整的 FPGA 阵列里，绝大多数 GSB 在结构上是 **完全相同** 的（只是坐标不同）。如果每个坐标都生成一份 Verilog，网表会爆炸。`DeviceRRGSB` 会识别出「互为镜像（mirror）」的 GSB，只保留一份唯一的，从而把模块数量从「网格面积」级压到「独特结构数」级。

#### 4.3.2 核心流程

unique module 的识别分两步走：

```
第一步：分类                    第二步：汇总
build_sb_unique_module()        build_gsb_unique_module()
build_cb_unique_module(CHANX)   ───────────────────────►  is_compressed_ = true
build_cb_unique_module(CHANY)     gsb_unique_module_  (唯一 GSB 列表)
        │                        sb/cbx/cby_unique_module_id_[x][y]  (每坐标→唯一id)
        ▼
  每个坐标的 SB/CB 先各自找镜像
```

判断「两个 GSB 是否互为镜像」的判据，体现在 `build_gsb_unique_module` 里：如果两个坐标的 SB 唯一 id、CBX 唯一 id、CBY 唯一 id **三者都相同**，那它们的 GSB 就是同一个唯一模块的不同实例。

压缩比可以粗略表示为：

\[
\text{压缩比} \;=\; \frac{\text{器件中的 GSB 总数}}{\text{unique GSB 数量}}
\]

对规整的 tileable 阵列，这个比值通常很大（几十甚至上百），直接决定了生成网表的规模。

还有一条重要支线：**缓存**。识别 unique module 是个耗时的比较过程，`DeviceRRGSB` 允许把识别结果（哪个坐标是 unique、哪些坐标是它的实例）通过 `read_unique_blocks` / `write_unique_blocks` 落盘成 XML 或二进制（capnp），下次重建同一器件时直接 `preload`，跳过比较。

#### 4.3.3 源码精读

[openfpga/src/annotation/device_rr_gsb.h:33-35](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/device_rr_gsb.h#L33-L35) —— 类声明，构造函数接收一个 `VprDeviceAnnotation` 引用（这就是 context 里 `device_rr_gsb_{vpr_device_annotation_}` 那行构造的来源）。

[openfpga/src/annotation/device_rr_gsb.h:194-224](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/device_rr_gsb.h#L194-L224) —— 内部数据。注意这几条：

- 第 195 行 `std::vector<std::vector<RRGSB>> rr_gsb_;` —— 按 `[x][y]` 存放每个坐标的完整 GSB。
- 第 197 行 `bool is_compressed_ = false;` —— 「unique module 是否已经识别完」的总开关。
- 第 200、204、208、215 行的 `*_unique_module_id_` —— 二维矩阵，`[x][y]` 查这个坐标对应第几个唯一模块。
- 第 202、206、210、217 行的 `*_unique_module_` —— 唯一模块的坐标列表。

核心压缩算法在实现文件里：

[openfpga/src/annotation/device_rr_gsb.cpp:454-497](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/device_rr_gsb.cpp#L454-L497) —— `build_gsb_unique_module()`：双重循环遍历每个坐标，对当前坐标去已有的唯一模块列表里找「SB+CBX+CBY 三者 id 都相同」的镜像；找到就复用 id，找不到就新增一个唯一模块。循环结束第 496 行把 `is_compressed_` 置 true。

[openfpga/src/annotation/device_rr_gsb.cpp:499-508](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/device_rr_gsb.cpp#L499-L508) —— `build_unique_module()` 是对外入口，依次构建 SB、CBX、CBY 的唯一模块，最后汇总成 GSB 唯一模块。

而「从磁盘预加载」走的是另一条路：

[openfpga/src/annotation/device_rr_gsb.cpp:783-802](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/device_rr_gsb.cpp#L783-L802) —— `preload_unique_sb_module()`：直接把读到的「唯一块坐标 + 它的所有实例坐标」灌进 `sb_unique_module_id_` 矩阵，**跳过了比较过程**。`preload_unique_cbx_module`、`preload_unique_cby_module` 同理（见 [device_rr_gsb.h:133-149](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/device_rr_gsb.h#L133-L149) 的声明与注释，明确写着「called when read_unique_blocks command invoked」）。

最后注意一个版本细节：

[openfpga/src/annotation/device_rr_gsb.h:221](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/device_rr_gsb.h#L221) —— `e_gsb_version gsb_version_ = e_gsb_version::GSB_V1;`。这是近版本引入的 GSB V1/V2 区分（见近期提交 `GSB V2`），`annotate_device_rr_gsb` 会根据 VPR device context 的 `gsb_version` 选择构建路径（[annotate_rr_graph.cpp:312-315](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/annotate_rr_graph.cpp#L312-L315) 在 V2 分支断言并构造 V2 版 RRGSB）。

#### 4.3.4 代码实践

**实践目标**：观察 unique module 压缩对模块数量的影响，理解 `is_compressed_` 的含义。

**操作步骤**：

1. 确认已按 u1-l3 编出 `openfpga` 二进制并 `source openfpga.sh`。
2. 参照 `openfpga_flow/tasks/` 下任一 `full_testbench` 任务跑一次完整流程（例如 `run-task basic_tests/full_testbench/configuration_chain`）。
3. 在生成的 fabric Verilog 目录（通常在 `latest/.../SRC/`）下统计 switch block / connection block 模块文件的数量。
4. 再运行 `read_write_unique_blocks_full_flow_example_script.openfpga`（位于 `openfpga_flow/openfpga_shell_scripts/`），它会写出 unique blocks 文件；检查该 XML/二进制里记录的 unique SB/CB 数量。
5. 对比「器件坐标总数（grid 宽×高）」与「unique 模块数」，计算压缩比。

**需要观察的现象**：unique 模块数远小于坐标总数；fabric 网表里每种 SB/CB 只生成一份模块定义，其余靠实例化复用。

**预期结果**：你能给出该器件的压缩比 \(\text{坐标总数}/\text{unique 数}\)，并理解 `is_compressed_=true` 之后 `DeviceRRGSB` 才能被 `build_routing_modules` 安全查询。

> 若本地未配置运行环境，可改为源码阅读型实践：在 `build_gsb_unique_module`（device_rr_gsb.cpp:454）里跟踪「三 id 相同即判为镜像」的逻辑，并解释为何 tileable 阵列的压缩比远高于非 tileable 阵列。运行结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`is_compressed_` 何时被置 true？它对下游有什么保护意义？

**参考答案**：在 `build_gsb_unique_module()` 末尾（device_rr_gsb.cpp:496）或 preload 完成后置 true，`clear()` / `clear_unique_modules()` 时置回 false。它表示「unique module 已经识别完毕」，下游查询 `*_unique_module_id_` 矩阵前应确认其为 true，否则查到的是未初始化的数据。

**练习 2**：`preload_unique_sb_module` 相比 `build_sb_unique_module` 节省了什么？

**参考答案**：节省了「逐一比较每个坐标的 SB 拓扑以找镜像」的耗时计算。preload 直接把外部已算好的「唯一块→实例列表」映射灌进去，用空间（磁盘缓存文件）换时间（跳过比较），尤其利于大阵列的反复构建。

---

### 4.4 FabricTile：把 grid+sb+cb 打包成可复用 tile

#### 4.4.1 概念说明

`FabricTile` 是比单个 GSB 更高一层的抽象。它把 **一个可编程逻辑块（programmable block，即 grid）和它周围的 switch block、connection block** 打包成一个整体「tile」。这样做的动机有二：

1. **层次化后端**：现代 FPGA 后端流程（PnR）希望以 tile 为单位摆放和连线，而不是面对成千上万个散落的 SB/CB。
2. **进一步压缩**：和 GSB 一样，整块 tile 也可以找「unique tile」——结构相同的 tile 只生成一份模块，其余实例化。

`FabricTile` 与 `DeviceRRGSB` 是协作关系：判断两个 tile 是否等价（`equivalent_tile`）时，需要查 `DeviceRRGSB` 里这些 SB/CB 是否互为镜像。

#### 4.4.2 核心流程

```
build_unique_tiles(grids, device_rr_gsb)
        │
        ├── 对每个 tile，收集它含的 pb / cbx / cby / sb 坐标
        ├── equivalent_tile(tile_a, tile_b, grids, device_rr_gsb)
        │       └─ 逐项比较 pb、cbx、cby、sb 的结构（含查 DeviceRRGSB 的 unique id）
        └── 结构全相同 → 归为同一 unique tile
结果：unique_tile_ids_（唯一 tile 列表）+ tile_coord2unique_tile_ids_[x][y]（坐标→唯一tile）
```

`FabricTile` 维护多套 `coord2id_lookup`，因为同一个 tile 既能用「tile 坐标」定位，也能用「它内部的 pb 坐标 / cb 坐标 / sb 坐标」反查到所属 tile——下游不同场景手里拿的坐标类型不同。

#### 4.4.3 源码精读

[openfpga/src/annotation/fabric_tile.h:25-26](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/fabric_tile.h#L25-L26) —— 类声明，开头注释点明它含「一组 tile + 一组 unique tile」。

[openfpga/src/annotation/fabric_tile.h:166-193](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/fabric_tile.h#L166-L193) —— 内部数据。注意：

- 第 177-181 行 `pb_coords_`、`pb_gsb_coords_`、`cbx_coords_`、`cby_coords_`、`sb_coords_`：每个 tile 内部存的 pb/cb/sb 坐标列表。其中 pb 同时存「device grid 坐标」和「gsb 坐标」两套（第 169-176 行注释解释：因为客户代码有时拿 device grid 坐标、有时拿 gsb 坐标来查）。
- 第 184-189 行五张 `coord2id_lookup_`：分别用 pb/cbx/cby/sb/tile 坐标反查 tile id。
- 第 190-193 行 `tile_coord2unique_tile_ids_` 与 `unique_tile_ids_`：unique tile 的核心结果。

[openfpga/src/annotation/fabric_tile.h:128-137](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/fabric_tile.h#L128-L137) —— `build_unique_tiles()` 的两个重载：无参版本仅比对坐标，带 `DeviceGrid` + `DeviceRRGSB` 的版本才会调用 `equivalent_tile` 做真正的结构比对。

[openfpga/src/annotation/fabric_tile.h:143-147](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/fabric_tile.h#L143-L147) —— `equivalent_tile()`：判断两 tile 是否等价，参数里带上 `DeviceGrid` 和 `DeviceRRGSB`，说明它要借助前者的 grid 信息和后者的 unique module id 来逐项比对 pb/cbx/cby/sb。

#### 4.4.4 代码实践

**实践目标**：理解 tile 化如何把「grid+sb+cb 组合」压缩成可复用模块。

**操作步骤**（源码阅读型实践）：

1. 在 `openfpga_flow/openfpga_shell_scripts/` 下找到 `group_tile_*` 系列脚本（如 `group_tile_preconfig_full_testbench_example_script.openfpga`）。
2. 对比它与普通 `example_script.openfpga` 的差异：tile 版多了一步把 grid+sb+cb 聚成 tile 的命令。
3. 阅读 [fabric_tile.h:143-147](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/fabric_tile.h#L143-L147) 的 `equivalent_tile` 签名，确认它依赖 `DeviceRRGSB`。
4. 思考：如果没有 `DeviceRRGSB` 提供的 unique module id，`equivalent_tile` 要怎么判断两个 CB 是否等价？（答：得逐边逐节点比对 CB 内部拓扑，代价高昂。）

**预期结果**：你能解释 tile 化对 fabric 规模（模块数、网表行数）的缩减作用，以及它为何必须建立在 `DeviceRRGSB` 之上。

> 运行结果「待本地验证」。可阅读 u9-l4（tile direct 与 fabric tile）获取更深入的构建侧细节。

#### 4.4.5 小练习与答案

**练习 1**：`FabricTile` 为什么要在 pb 上同时存 device grid 坐标和 gsb 坐标两套？

**参考答案**：因为下游不同代码路径手里拿的坐标类型不同——有的从 device grid 出发定位逻辑块，有的从 GSB 出发定位。两套坐标都存，让任意一种查询都能 O(1) 命中所属 tile，而不必做坐标转换。

**练习 2**：`equivalent_tile` 为什么要把 `DeviceRRGSB` 作为参数传入？

**参考答案**：判断两个 tile 等价，需要确认它们内部的 pb、cbx、cby、sb 在结构上完全相同。而 SB/CB 是否同构，最省事的判据就是查 `DeviceRRGSB` 里它们的 unique module id 是否一致——所以必须把 `DeviceRRGSB` 传进来。

---

### 4.5 netlist 命名冲突检查：check_netlist_naming_conflict

#### 4.5.1 概念说明

这是 annotation 目录里少数 **面向用户的命令** 之一（前面几个多是内部数据结构）。它解决一个非常实际的问题：

用户的 Verilog 经 Yosys 综合成 BLIF/EBLIF netlist 后，里面的 block 名、net 名可能含有 Verilog/SPICE 标识符 **不允许** 的字符，例如 `counter[0]`、`a+b`、`clk~`。OpenFPGA 的 fabric Verilog / SPICE 生成器（u8）要把这些名字直接写进网表，一旦遇到非法字符，生成的网表就无法被后续工具（iverilog、HSPICE）解析。

`check_netlist_naming_conflict` 就是这道合法性闸门：先 **检测** 有哪些非法字符，必要时 **自动修复**（把非法字符替换成下划线），并把改名记录写进 `VprNetlistAnnotation`——下游生成器从这里查「这个名字其实应该写成什么」，从而既不破坏 VPR 原始 netlist、又能输出合法网表。

这正是 annotation 模式的又一个范例：**不改 VPR netlist，只在旁边记一张改名表**。

#### 4.5.2 核心流程

```
check_netlist_naming_conflict [--fix] [--report <file>]
        │
        ├─ 未给 --fix：detect_netlist_naming_conflict()
        │     遍历所有 block / net 名，统计命中敏感字符的数量
        │     有冲突 → 返回 CMD_EXEC_MINOR_ERROR（轻微错误）
        │
        └─ 给了 --fix：fix_netlist_naming_conflict()
              遍历所有 block / net 名，把敏感字符替换成 '_'（按位置对应）
              把「原名→新名」写进 VprNetlistAnnotation
              若给了 --report，再打印一份改名报告
```

默认敏感字符表和替换表是一一对应的（第 i 个敏感字符 → 第 i 个替换字符），替换字符统一是下划线。

#### 4.5.3 源码精读

命令的注册（选项与依赖）在 setup 命令模板里：

[openfpga/src/base/openfpga_setup_command_template.h:303-327](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L303-L327) —— `add_check_netlist_naming_conflict_command_template`：定义 `--fix`、`--report` 两个选项，绑定执行函数。

[openfpga/src/base/openfpga_setup_command_template.h:1484-1491](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L1484-L1491) —— 声明它依赖 `vpr` 命令（必须在 vpr 跑完、有了 netlist 之后才能检查）。这是 u2-l2 讲过的「命令依赖只查一级」的典型例子。

执行模板里定义了默认敏感字符表：

[openfpga/src/annotation/check_netlist_naming_conflict_template.h:33-35](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/check_netlist_naming_conflict_template.h#L33-L35) —— `sensitive_chars(".,:;\'\"+-<>()[]{}!@#$%^&*~`?/")` 与 `fix_chars("____________________________")`，两者长度相等，按位置一一对应替换。

[openfpga/src/annotation/check_netlist_naming_conflict_template.h:41-60](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/check_netlist_naming_conflict_template.h#L41-L60) —— 检测分支：未开 `--fix` 时调 `detect_netlist_naming_conflict`，有冲突则记 `CMD_EXEC_MINOR_ERROR` 并打印提示让用户去修。

[openfpga/src/annotation/check_netlist_naming_conflict_template.h:63-76](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/check_netlist_naming_conflict_template.h#L63-L76) —— 修复分支：开 `--fix` 时调 `fix_netlist_naming_conflict`，把改名写进 `mutable_vpr_netlist_annotation()`；若再给 `--report` 则输出改名报告。

具体的检测与改名算法在实现文件里：

[openfpga/src/annotation/check_netlist_naming_conflict.cpp:83-112](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/check_netlist_naming_conflict.cpp#L83-L112) —— `detect_netlist_naming_conflict`：遍历 netlist 的每个 block 名和 net 名，用 `name_contain_sensitive_chars` 找出命中的非法字符并计数。

[openfpga/src/annotation/check_netlist_naming_conflict.cpp:122-160](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/check_netlist_naming_conflict.cpp#L122-L160) —— `fix_netlist_naming_conflict`：对命中非法字符的名字调 `rename_block` / `rename_net` 把新名写进 `VprNetlistAnnotation`（注意它 **没有** 改 VPR 的 atom netlist 本身）。

#### 4.5.4 代码实践

**实践目标**：亲手触发一次命名冲突并观察 `--fix` 的修复结果。

**操作步骤**：

1. 在一个基准 Verilog 里故意引入带非法字符的信号名，例如 `wire [1:0] a+b;`（含 `+`）。
2. 准备一个最小的 `task.conf` 或直接用 `openfpga -f` 跑脚本，脚本里在 `vpr` 之后、`build_fabric` 之前先执行：
   ```
   check_netlist_naming_conflict --fix --report ${PWD}/naming_fix.xml
   ```
3. 检查控制台日志里的 `Fixed N naming conflicts` 行。
4. 打开生成的 `naming_fix.xml`，查看 `<block previous="a+b" current="a_b"/>` 这类条目。

**需要观察的现象**：未加 `--fix` 时命令返回轻微错误（`CMD_EXEC_MINOR_ERROR`），流程在 `-batch` 模式下会因此中断；加 `--fix` 后冲突被自动改名，流程继续，下游 fabric Verilog 里这些名字以合法形式出现。

**预期结果**：你能用一句话说出 `--fix` 做了什么——**它把 netlist 里 Verilog/SPICE 标识符不允许的字符自动替换成下划线，并把改名对照记录到 `VprNetlistAnnotation`，供下游网表生成器使用，而不改动 VPR 的原始 netlist。**

> 运行结果「待本地验证」。若不便改基准，可直接阅读 `check_netlist_naming_conflict.cpp:122-160` 的 `fix_netlist_naming_conflict`，确认它只调 `rename_block`/`rename_net` 写标注、未触碰 atom netlist。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `fix_netlist_naming_conflict` 把改名写进 `VprNetlistAnnotation`，而不是直接改 VPR 的 `AtomNetlist`？

**参考答案**：因为 `AtomNetlist` 属于 VPR（第三方子模块）的数据，直接改它既违反「不侵入 VPR」原则，也可能破坏 VPR 内部以原名建立的其他索引。把改名记在外部标注表里，下游生成器查表即可，既隔离了风险，又保留了「原名↔新名」的可追溯性（还能出报告）。

**练习 2**：未加 `--fix` 检测到冲突时，命令返回 `CMD_EXEC_MINOR_ERROR` 而不是 `CMD_EXEC_FATAL_ERROR`，为什么这样设计？

**参考答案**：命名冲突不是「数据彻底坏掉」的致命错误，而是一个可修复的轻微问题——用户加上 `--fix` 就能继续。用 minor error 既提示了问题，又把「是否自动修复」的决定权留给用户，符合命令的「检测 + 可选修复」两段式语义。

## 5. 综合实践

把本讲全部知识串起来，完成下面这个归类与串联任务。

**任务**：

1. **归类**：在 `openfpga/src/annotation/` 目录中，把所有文件分成三组——
   - A 组「标注 VPR 结果的便利贴」（数据结构，键是 VPR 对象）；
   - B 组「OpenFPGA 自有设备结构」（容器，VPR 里没有的设备图）；
   - C 组「填充/检查动作」（函数文件，不持有状态）。
   把每个 `.h`/`.cpp` 文件名填进对应分组。

2. **画数据流**：画一张图，体现下列命令如何通过 `OpenfpgaContext` 里的 annotation 交换数据：
   - `vpr` → 产生 VPR 的 device/netlist/clustering/placement/routing context；
   - `check_netlist_naming_conflict --fix` → 写 `VprNetlistAnnotation`；
   - `link_openfpga_arch` → 写 `VprDeviceAnnotation`，构建 `DeviceRRGSB`；
   - `build_fabric` → 读上述全部标注，构建 `module_graph`；
   - `repack` → 写 `VprClusteringAnnotation` 的 `physical_pbs_`；
   - `build_architecture_bitstream` → 读 `VprRoutingAnnotation` 的 rr_node net 信息。

3. **一句话**：用一句话写出 `check_netlist_naming_conflict --fix` 解决了什么问题（参考 4.5.4 的预期结果）。

**参考要点**：

- A 组：`vpr_device_annotation`、`vpr_netlist_annotation`、`vpr_clustering_annotation`、`vpr_placement_annotation`、`vpr_routing_annotation`、`vpr_bitstream_annotation`。
- B 组：`device_rr_gsb`、`fabric_tile`（以及配套的 `fabric_tile_fwd`、`rr_gsb_writer_option`）。
- C 组：`annotate_*`（pb_types、pb_graph、rr_graph、physical_tiles、clustering、placement、routing、simulation_setting、bitstream_setting）、`openfpga_annotate_routing`、`check_netlist_naming_conflict`（检测/修复动作）、`read/write_unique_blocks_*`、`write_xml_device_rr_gsb`、`append_clock_rr_graph`、`route_clock_rr_graph`、`check_pb_type_annotation`、`check_pb_graph_annotation`。
- 数据流图的关键：annotation 是命令间的「侧信道」——写入方拿 mutable 访问器、读取方拿 const 访问器，所有标注都挂在 `OpenfpgaContext` 这一个对象上（openfpga_context.h:199-217）。

## 6. 本讲小结

- **annotation 模式**：不改 VPR 源码，以 VPR 对象的指针/id 为键，在外部挂平行副表（便利贴）来叠加 OpenFPGA 专属信息，从而隔离第三方代码升级、分离关注点。
- `OpenfpgaContext` 用成对的 **const/mutable 访问器** 聚合全部标注（openfpga_context.h:75-158），写入方与读取方在编译期就被区分；`DeviceRRGSB` 在构造时即绑定 `VprDeviceAnnotation` 引用。
- `Vpr*Annotation` 家族分两类：**标注 VPR 结果**（device/netlist/clustering/placement/routing/bitstream 六张便利贴）与 **OpenFPGA 自有设备结构**（`DeviceRRGSB`、`FabricTile`）。
- `DeviceRRGSB` 用「SB+CBX+CBY 三 unique id 都相同即互为镜像」识别 unique module，把模块数从坐标总数级压到独特结构数级；`is_compressed_` 标记识别完成；`read/write_unique_blocks` 用 preload 跳过耗时的比较、缓存加速。
- `FabricTile` 把 grid+sb+cb 打包成可复用 tile，`equivalent_tile` 依赖 `DeviceRRGSB` 判断 tile 等价，服务于层次化后端与进一步压缩。
- `check_netlist_naming_conflict --fix` 是少数面向用户的 annotation 命令：检测并自动修复 netlist 中 Verilog/SPICE 非法字符，把改名写入 `VprNetlistAnnotation` 而不动 VPR 原始 netlist。

## 7. 下一步学习建议

本讲把 annotation 目录的全貌梳理完毕，但只把 `VprClusteringAnnotation.physical_pbs_`、`VprBitstreamAnnotation`、`DeviceRRGSB` 的压缩与缓存等当成「黑盒」提及。后续建议：

- **u6（Fabric 构建）**：看 `build_routing_modules`、`build_grid_modules` 如何大量读取 `VprDeviceAnnotation` 与 `DeviceRRGSB` 来决定实例化哪些模块——你会真正看到本讲的「便利贴」被消费。
- **u7-l1/l2（比特流模型）**：看 `VprRoutingAnnotation` 的 rr_node net 信息如何变成布线 mux 的选择位。
- **u9-l3（repack 内部机制）**：深入 `VprClusteringAnnotation` 的 `PhysicalPb` 与 `physical_lb_rr_graphs_`，理解逻辑→物理 pb 的重打包。
- **u9-l5（GSB 压缩与 unique blocks）**：从工程角度细化本讲 4.3 的 `read/write_unique_blocks` 缓存机制与 GSB V2。
- 想验证本讲结论，可挑一个 `basic_tests` 任务，在 `build_fabric` 前后用调试器或日志观察各 annotation 的 map 大小变化。
