# Packing 总览与 PB 类型图

## 1. 本讲目标

本讲是「打包 Packing」单元的开篇。学完之后，你应该能够：

- 说清楚**打包到底要解决什么问题**：把原子级网表 `AtomNetlist` 里的细粒度原语（LUT、FF、加法器等）按逻辑相关性装进 FPGA 的物理逻辑块（CLB），产出聚簇网表 `ClusteredNetlist`。
- 理解 **PB 类型图（PB Graph）** 是什么、为什么必须在打包之前就建好：它是把架构 XML 里 `t_pb_type` 层次「按实例展开」后得到的可布线模板，是聚簇合法化的判定依据。
- 读懂入口函数 `try_pack` 的主要步骤：它如何把 `ClusterLegalizer` 与 `GreedyClusterer` 组装起来，并用一个状态机反复重打包，直到结果能装进器件。

本讲只做**总览**，不深入聚类算法细节（留给 u4-l2/u4-l3）和簇内布线细节（留给 u4-l4）。

## 2. 前置知识

本讲直接建立在前几讲的认知之上，请确认你已经理解以下概念：

- **AtomNetlist（u3-l2）**：技术映射后的原子级电路，块是 LUT/FF 等原语，是打包的**输入**。
- **ClusteredNetlist（u3-l3）**：聚簇后的逻辑块级网表，每个块携带一个 `t_pb*` 描述盒内层次与盒内布线 `pb_route`，是打包的**输出**、布局布线的输入。
- **`t_pb_type` 层次树（u2-l2）**：架构 XML 里 `t_pb_type → t_mode → t_pb_type` 的递归套娃树，根节点 `parent_mode == nullptr`，叶子原语 `num_modes == 0`。
- **`t_vpr_setup` 与 `g_vpr_ctx`（u3-l4、u3-l5）**：配置对象走 `t_vpr_setup`，运行时状态走全局总线 `g_vpr_ctx`，打包既读 `atom_ctx` 又写 `clustering_ctx`。

一句话回顾数据流：

```
AtomNetlist → [Prepacker] → [Packer/聚类] → ClusteredNetlist → Placer → Router
```

本讲聚焦的就是中间方括号这一段。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `vpr/src/base/vpr_api.cpp` | 主流程编排。`vpr_pack_flow` 按阶段动作分派，`vpr_pack` 准备好打包所需全部输入对象后调用 `try_pack`。 |
| `vpr/src/pack/pack.h` | 声明入口函数 `try_pack` 与器件尺寸探测 `try_size_device_grid`。 |
| `vpr/src/pack/pack.cpp` | `try_pack` 的实现、打包状态机 `e_packer_state`、迭代重打包主循环。 |
| `vpr/src/pack/pb_type_graph.h` / `pb_type_graph.cpp` | PB 类型图的构建入口 `alloc_and_load_all_pb_graphs` 与递归展开函数 `alloc_and_load_pb_graph`。 |
| `vpr/src/base/setup_vpr.cpp` | 在 `setup` 阶段调用 `alloc_and_load_all_pb_graphs`，即 PB 图在打包之前就已经建好。 |
| `libs/libarchfpga/src/physical_types.h` | `t_pb_type`、`t_mode`、`t_pb_graph_node` 的结构定义，是理解 PB 图的数据基础。 |

## 4. 核心概念与源码讲解

### 4.1 打包目标与流程

#### 4.1.1 概念说明

经过 ABC 技术映射后，`AtomNetlist` 里的块是**很细**的：一个个独立的 LUT、触发器、进位链单元。但真实的 FPGA 不会把每个 LUT 单独放在一个网格位置上——它们被组织成更大的**逻辑块**（例如 Intel 的 LAB、Xilinx 的 CLB），一个逻辑块内部能容纳几十上百个原语，并且**块内部有专门的快速互连**（进位链、局部走线）。

因此布局布线阶段如果把每个原子都当成独立对象来处理，规模会爆炸，而且会**浪费**块内快速互连带来的好处。**打包（Packing）** 就是在布局之前做的一次「按逻辑相关性装箱」：

- **目标**：把逻辑相关的原子聚成一个个**簇（cluster）**，每个簇恰好能装进一个逻辑块，并且簇内的连线能用块内互连布通。
- **输入**：`AtomNetlist`（原子级电路）、架构（`t_arch` 与逻辑块类型）、PB 图。
- **输出**：`ClusteredNetlist`（聚簇网表）。

这里有一个关键的**分层解耦**思想（承接 u3-l3）：聚簇块只携带**逻辑块类型**，至于它最终落在哪种物理瓦片上，是通过 `equivalent_tiles`（逻辑→物理的多对多映射）推迟到布局阶段才决定。所以打包阶段完全不需要关心物理网格。

#### 4.1.2 核心流程

打包不是「一次成型」，而是「**试装 + 反复放宽约束**」的过程，大致流程如下：

```text
准备阶段（vpr_pack）：
  ① 读取可选的 flat placement（扁平布局提示）
  ② 构造 Prepacker —— 把原子按 pack pattern 组成「打包分子」
  ③ 构造 PreClusterTimingManager —— 预算打包前的时序，告知哪些路径关键
  ④ （可选）构造 RamMapper —— 推断 RAM 并指定物理类型
  → 调用 try_pack(...)

迭代聚簇（try_pack）：
  while 尚未成功 且 尚未失败:
    ⑤ GreedyClusterer.do_clustering()  贪心聚出一簇簇逻辑块
    ⑥ try_size_device_grid()           检查结果能否装进器件（资源够不够）
    ⑦ get_next_packer_state()          装不下？放宽约束（开无关聚类、提引脚利用率…）
  成功则把 ClusteredNetlist 写入 g_vpr_ctx.clustering()
```

注意第 ⑤ 步内部每装一个簇，都要做一次**簇内布线合法化**（由 `ClusterLegalizer` 基于 `lb_type_rr_graph` 完成），不合法的候选会被拒绝——这正是 u4-l4 的主题，本讲只点到为止。

#### 4.1.3 源码精读

打包的「准备阶段」全部发生在 [`vpr/src/base/vpr_api.cpp:720-779`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L720-L779) 的 `vpr_pack` 中。这段代码的价值在于：它清清楚楚地列出了 `try_pack` 之前必须构造好的全部输入对象。

构造 `Prepacker`——只要「分子」被使用，这个对象就必须存活（注释明确说明）：

[vpr/src/base/vpr_api.cpp:733-735](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L733-L735) — 用原子网表、架构模型、逻辑块类型构造预打包器，产出打包分子。

构造 `PreClusterTimingManager` 并生成一次 setup 时序报告：

[vpr/src/base/vpr_api.cpp:738-750](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L738-L750) — 在聚类之前预算时序，让聚类器知道哪些路径是关键路径，从而优先把关键原语放进同一个簇（减少跨簇关键延迟）。

最后把所有准备好的对象塞进 `try_pack`：

[vpr/src/base/vpr_api.cpp:771-778](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L771-L778) — 真正的打包入口：传入打包/分析/AP 选项、架构、簇内布线图 `PackerRRGraph`、分子器、时序管理器、flat 布局、配置、RAM 映射器。

`try_pack` 的声明在 [`vpr/src/pack/pack.h:49-58`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pack.h#L49-L58)，每个形参都有注释，**强烈建议把这份注释当作打包输入清单来读**。器件尺寸探测 `try_size_device_grid` 的声明在 [`vpr/src/pack/pack.h:77-82`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pack.h#L77-L82)。

#### 4.1.4 代码实践

**实践目标**：亲手把「打包前必须准备好的输入对象」清单整理出来，建立对 `try_pack` 输入的完整认知。

**操作步骤**：

1. 打开 [`vpr/src/base/vpr_api.cpp:720`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L720) 的 `vpr_pack`，从函数开头读到 `try_pack(...)` 调用。
2. 打开 [`vpr/src/pack/pack.h:49`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pack.h#L49) 的 `try_pack` 形参注释。
3. 列一张表，左列写 `try_pack` 的每个形参名，右列写：它由谁构造、来自哪个上下文、解决什么问题。

**需要观察的现象**：

- `Prepacker`、`PreClusterTimingManager`、`RamMapper` 这三者都是**在 `vpr_pack` 里临时构造的局部对象**，构造它们的原料（`netlist`、`lookup`、`arch`、`models`）几乎都来自 `g_vpr_ctx.atom()` 和 `g_vpr_ctx.device()`。
- `PackerRRGraph`（簇内布线图）来自 `vpr_setup.PackerRRGraph`——它并不是打包阶段才建的，而是 `setup_vpr` 早就建好的（见 4.2 节）。

**预期结果**：你会得到类似下面这张表（请自行核对补全）：

| try_pack 形参 | 由谁构造 | 来源 | 作用 |
|---|---|---|---|
| `prepacker` | `Prepacker(...)` | atom netlist + arch.models + 逻辑块类型 | 把原子组成打包分子 |
| `pre_cluster_timing_manager` | `PreClusterTimingManager(...)` | atom netlist/lookup + 选项 + arch | 告知关键路径 |
| `lb_type_rr_graphs` | （来自 `vpr_setup.PackerRRGraph`） | setup 阶段 | 簇内布线合法性判定 |
| `flat_placement_info` | `g_vpr_ctx.atom().flat_placement_info()` | 可选 flat 布局文件 | 聚类提示 |
| `ram_mapper` | `RamMapper(...)`（可选） | netlist + prepacker + 时序 | 指导 RAM 簇打包 |

如果某些形参的来源你一时对不上，**待本地验证**或留作 u4-l2/u4-l4 再回填。

#### 4.1.5 小练习与答案

**练习 1**：为什么打包阶段不直接操作 `ClusteredNetlist`，而是要先有 `AtomNetlist`？
> **答案**：因为打包的本质就是从原子层「升维」到聚簇层。`ClusteredNetlist` 是打包的**产物**而非输入；输入必须是细粒度的 `AtomNetlist`，聚类器才能根据原子间的连线关系决定把谁和谁装进同一个簇。

**练习 2**：`Prepacker` 的注释里说「As long as the molecules are used, this object must persist.」，为什么它必须存活到 `try_pack` 执行结束？
> **答案**：`Prepacker` 内部用 `vtr::vector_map` 持有全部 `t_pack_molecule`（见 `prepack.h`），聚类器在每一步都通过引用读取分子结构。若它在 `try_pack` 返回前析构，聚类器就会访问悬空引用。

### 4.2 PB 类型图构建

#### 4.2.1 概念说明

要判断「这一簇原子能否装进某个逻辑块并布通」，打包器需要一个该逻辑块的**完整可布线模板**——知道这个块有哪些引脚、有哪些子块、子块之间能怎么连。这个模板就是 **PB 类型图（PB Graph）**。

理解 PB 图的关键是区分**两层**（承接 u2-l2）：

- **`t_pb_type` 是「类型树」**：描述层次结构，但 `num_pb=3` 这种「有三个实例」只写了一个数字，没有真正展开。
- **`t_pb_graph_node` 是「实例树」**：把 `num_pb` 真正展开成 3 个独立的图节点，每个节点都有自己的引脚 `t_pb_graph_pin`、边 `t_pb_graph_edge`。

用一个具体例子：假设架构里一个 `clb`（根 pb_type）在 `default` 模式下含 `num_pb=10` 个 `ble`，每个 `ble` 含一个 LUT 和一个 FF。那么：

- 类型树：`clb → (mode default) → ble → (mode ...) → lut/ff`，只有 1 个 `ble` 类型节点。
- 实例树（PB 图）：`clb[0]` 下有 `ble[0..9]` 共 10 个图节点，每个再展开成具体的 `lut[x]`、`ff[x]`，引脚和互连边全部铺好。

PB 图在**打包之前**就建好，且**全工程共享**（存在 `DeviceContext` 里）。这样聚类器在装一个候选原子时，可以直接在模板上「试布线」，判断合法性。

#### 4.2.2 核心流程

PB 图的构建时机不是打包阶段，而是更早的 `setup` 阶段。调用链很短：

```text
setup_vpr()
  └─ alloc_and_load_all_pb_graphs(do_power, flat_routing)   # 顶层循环：每个逻辑块类型
       └─ 对每个 type.pb_type：
            new t_pb_graph_node() 作为根
            alloc_and_load_pb_graph(root, parent=null, pb_type, index=0, ...)   # 递归
              ├─ 为本节点分配 input/output/clock 引脚
              ├─ for 每个模式 m:
            │     for 每个子 pb_type c:
            │       for k in [0, c.num_pb):
            │         alloc_and_load_pb_graph(child[k], parent=this, &c, k, ...)  # 递归展开
              └─ alloc_and_load_mode_interconnect(...)   # 铺本模式的互连边
       └─ check_pb_graph()   # 校验，有错直接 exit(1)
```

注意 `pin_count_in_cluster` 是一个**引用传递**的累加器：每铺设一个引脚就自增，于是簇内每个引脚都拿到一个全局唯一的「簇内编号」，这正是后续簇内布线（u4-l4）和时序分析赖以工作的扁平索引。

#### 4.2.3 源码精读

PB 图的构建调用点在 [`vpr/src/base/setup_vpr.cpp:345-349`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_vpr.cpp#L345-L349)：在一个计时作用域里同时建 PB 图和簇内布线图 `lb_type_rr_graph`。注意它**先于**打包执行。

顶层循环 [`vpr/src/pack/pb_type_graph.cpp:177-218`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pb_type_graph.cpp#L177-L218) 遍历 `device_ctx.logical_block_types`，为每个有 `pb_type` 的逻辑块 `new` 一个根 `t_pb_graph_node` 并递归构建：

[pb_type_graph.cpp:183-206](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pb_type_graph.cpp#L183-L206) — 逐个逻辑块类型构造 PB 图根节点，递归填充引脚，并在扁平路由模式下额外铺设 sink 信息与逻辑类。构建后用 `check_pb_graph()` 校验，有错即 `exit(1)`。

递归展开的核心在 [`vpr/src/pack/pb_type_graph.cpp:391-409`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pb_type_graph.cpp#L391-L409)，三层嵌套对应「模式 × 子类型 × 实例编号」：

```cpp
pb_graph_node->child_pb_graph_nodes = calloc(pb_type->num_modes, ...);
for (i = 0; i < pb_type->num_modes; i++) {                         // 每个模式
    for (j = 0; j < pb_type->modes[i].num_pb_type_children; j++) { // 每种子类型
        int num_children_of_type = pb_type->modes[i].pb_type_children[j].num_pb;
        for (k = 0; k < num_children_of_type; k++) {               // 展开 num_pb 个实例
            alloc_and_load_pb_graph(&child[i][j][k], pb_graph_node,
                                    &pb_type->modes[i].pb_type_children[j], k, ...);
        }
    }
}
```

这正是「把 `num_pb` 真正展开成实例」的实现：`child_pb_graph_nodes[i][j][k]` 三维下标分别是模式、子类型、实例编号，与 `t_pb_graph_node` 的成员声明一一对应（见下）。

支撑这一切的数据结构在 `physical_types.h`：

- [`t_pb_type`（L976-1028）](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L976-L1028)：类型节点，持有 `modes`、`ports`、`num_pb`，并提供 `is_root()`（`parent_mode==nullptr`）与 `is_primitive()`（`num_modes==0`）两个判定。
- [`t_mode`（L1050-1066）](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L1050-L1066)：持有 `pb_type_children` 和 `interconnect`，是 `t_pb_type` 套娃的「胶水」。
- [`t_pb_graph_node`（L1283-1357）](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L1283-L1357)：实例节点，持有 `input_pins/output_pins/clock_pins` 二维数组与 `child_pb_graph_nodes[mode][child][instance]`，并维护 `illegal_modes`（簇内布线时记录哪些模式会产生冲突，留给 u4-l4 详讲）。

`t_pb_graph_node` 的注释给出一个典型层次名：`clb[0][default]/lab[0][default]/fle[3][n1_lut6]/ble6[0][default]/lut6[0]`——这正是实例树从根到叶的路径。

#### 4.2.4 代码实践

**实践目标**：亲眼确认「类型树 vs 实例树」的展开关系，理解 PB 图为何必须在打包前建好。

**操作步骤**：

1. 打开 [`vpr/src/pack/pb_type_graph.cpp:391`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pb_type_graph.cpp#L391)，确认三层循环如何把 `num_pb` 展开成实例。
2. 在 `physical_types.h` 里比对 `t_pb_type::num_pb`（L978）与 `t_pb_graph_node::child_pb_graph_nodes`（L1329）的维度注释 `[0..num_modes-1][0..num_pb_type_in_mode-1][0..num_pb-1]`，体会三维下标的含义。
3. 思考：若某 `pb_type` 在某模式下 `num_pb=8`，类型树里有几个该子类型？PB 图里有几个？分别是哪些下标？

**需要观察的现象**：类型树里**始终只有 1 个**该子类型节点（`num_pb` 只是它身上的一个整数属性）；而 PB 图里有 **8 个**实例节点 `child[m][j][0..7]`，每个都有独立引脚。

**预期结果**：你会清楚地看到，「展开」完全发生在 `alloc_and_load_pb_graph` 的递归里；`t_pb_type` 只是「图纸」，`t_pb_graph_node` 才是搭好的「实物」。打包器和簇内布线器只操作后者。

#### 4.2.5 小练习与答案

**练习 1**：`alloc_and_load_all_pb_graphs` 为什么遍历的是 `logical_block_types` 而不是 `physical_tile_types`？
> **答案**：PB 图描述的是**逻辑块内部**的可布线结构（`t_pb_type` 层次），而物理瓦片只是网格地块的几何容器。承接 u2-l2/u3-l3，逻辑块类型才持有 `pb_type` 根节点，所以 PB 图按逻辑块类型来建。

**练习 2**：`pin_count_in_cluster` 用引用传递并在铺设引脚时不断自增，这样做有什么好处？
> **答案**：它让簇内每一个引脚获得一个**全局唯一且连续**的簇内编号（`pin_count_in_cluster`），并填进 `t_pb_graph_pin::pin_count_in_cluster`。后续簇内布线图 `lb_type_rr_graph` 和时序分析都依赖这套扁平索引来 O(1) 定位引脚，避免在嵌套层次里反复寻址。

### 4.3 入口函数 try_pack

#### 4.3.1 概念说明

`try_pack` 是打包的真正心脏。它做的事可以概括为两件：

1. **组装**：把 `ClusterLegalizer`（负责判定一个簇是否合法、能否布通）和 `GreedyClusterer`（负责贪心地选种子、选候选、生成簇）连接起来。
2. **迭代**：用一个小型状态机反复重打包——如果一次聚簇的结果装不下器件或违反 floorplan 约束，就**逐步放宽约束**（开启无关聚类、提高引脚利用率目标、加强吸引力组等）再试，直到成功或确认失败。

这种「先严后松」的策略很好理解：宽松的约束虽然更容易装下，但聚类质量更差（比如把无关原语硬塞进一个簇会浪费面积、增加延迟）。所以打包器宁可先用紧约束试，实在不行再退让。

#### 4.3.2 核心流程

迭代主循环由一个有限状态机驱动，状态定义在 `e_packer_state`：

```text
DEFAULT（初始）
  │  do_clustering → try_size_device_grid
  ▼
能装下且 floorplan 没溢出？ ── 是 ──▶ SUCCESS（成功，退出）
  │否
  ▼
SET_UNRELATED_AND_BALANCED（开无关聚类 + 平衡类型利用率）
  │仍装不下
  ▼
INCREASE_OVERUSED_TARGET_PIN_UTILIZATION（提高过载类型的引脚利用率目标）
  │
  ▼
CREATE_ATTRACTION_GROUPS / CREATE_MORE_... （floorplan 溢出时逐步加强吸引力组）
  │
  ▼
AP_INCREASE_MAX_DISPLACEMENT / AP_USE_HIGH_EFFORT_UC（APPack 相关放宽）
  │仍不行
  ▼
FAILURE（失败，退出）
```

每次迭代都执行相同的两步：`clusterer.do_clustering(...)` 聚出一组簇，`try_size_device_grid(...)` 检查能否装进器件。`get_next_packer_state(...)` 根据当前结果决定是 SUCCESS、某个放宽状态，还是 FAILURE。

#### 4.3.3 源码精读

`try_pack` 的实现签名与开头见 [`vpr/src/pack/pack.cpp:231-240`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pack.cpp#L231-L240)。

状态机的全部状态定义在 [`vpr/src/pack/pack.cpp:37-66`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pack.cpp#L37-L66)，每个枚举值都有中文可对照的注释（成功、开无关聚类、提引脚利用率、各档吸引力组、APPack 放宽、失败）。

组装两个核心对象：先建 `ClusterLegalizer`（合法化器），再建 `GreedyClusterer`（聚类器）并把合法化器交给它：

[pack.cpp:320-330](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pack.cpp#L320-L330) — 用原子网表、分子器、簇内布线图、引脚利用率目标等初始化合法化器，策略选 `SKIP_INTRA_LB_ROUTE`。

[pack.cpp:338-347](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pack.cpp#L338-L347) — 构造贪心聚类器，传入时序管理器与 APPack 上下文。

迭代主循环 [`vpr/src/pack/pack.cpp:354`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pack.cpp#L354) 的条件就是「未成功且未失败」。循环体里两步关键调用：

[pack.cpp:361-367](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pack.cpp#L361-L367) — `do_clustering` 聚类，返回每种逻辑块类型用掉的实例数 `num_used_type_instances`，并把改动写进 `mutable_device_ctx`。

[pack.cpp:371-376](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pack.cpp#L371-L376) — 调 `try_size_device_grid` 判断聚簇结果能否装进器件。

资源利用率的判定本质是一个面积比，可记作：

\[
\text{utilization} = \frac{\text{所需实例折算的面积}}{\text{器件网格提供的面积}}
\]

若任意一种逻辑块类型的利用率超过 1.0，`fits_on_device` 即为假，`get_next_packer_state` 便会返回某个放宽状态。`get_next_packer_state` 的实现见 [`vpr/src/pack/pack.cpp:97`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pack.cpp#L97) 起：第 106-110 行是「能装下且 floorplan 没溢出就 SUCCESS」的快路径。

#### 4.3.4 代码实践

**实践目标**：通过阅读状态机与主循环，建立「打包是一个会反复放宽约束的迭代过程」的直觉。

**操作步骤**：

1. 读 [`vpr/src/pack/pack.cpp:37-66`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pack.cpp#L37-L66) 的 `e_packer_state`，把每个状态抄成一张「放宽阶梯」表。
2. 读 [`pack.cpp:354`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pack.cpp#L354) 起的主循环体，确认每轮都执行 `do_clustering` + `try_size_device_grid` + `get_next_packer_state`。
3. 在循环体里找到日志输出 `VTR_LOG("Packing failed to fit on device. Re-packing with: ...")`（约 L426），看它打印了哪些被放宽的开关。

**需要观察的现象**：日志会明确告诉你「这一轮因为装不下，重新打包时打开了 `unrelated_logic_clustering=true`、`balance_block_type_util=true`」。这正是状态机从 `DEFAULT` 切到 `SET_UNRELATED_AND_BALANCED` 的外部表现。

**预期结果**（运行行为**待本地验证**）：用一个较大电路跑 `vpr`，若首轮流式聚类装不下器件，标准输出里应能看到上述「Re-packing」日志，证明迭代确实发生。如果你暂时无法编译运行，也可以只做源码阅读：沿着 `get_next_packer_state` 的返回值，把每个 `case` 在 `switch` 里对应的「放宽动作」一一对应起来。

#### 4.3.5 小练习与答案

**练习 1**：为什么打包要先紧后松，而不是一开始就用最宽松的约束？
> **答案**：宽松约束（如允许无关聚类）会让无关的原语被塞进同一个簇，增加面积与跨簇延迟，降低聚类质量。先紧后松可以尽可能保住高质量解，只在确实装不下时才牺牲质量换取可行性。

**练习 2**：`try_pack` 同时取了 `const AtomContext&` 和 `DeviceContext& mutable_device_ctx`，为什么要拿一个可写的 device 上下文？
> **答案**：因为聚类过程会改变对器件资源的需求，聚类器可能需要**扩大器件尺寸**才能装下结果（见 `vpr_pack` 中关于 auto-sizing 的注释）。所以它持有可写 device 上下文，以便必要时调整网格规模。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「打包调用链追踪」任务：

1. **起点**：从 [`vpr/src/base/vpr_api.cpp:679`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L679) 的 `vpr_pack_flow` 出发，记录 `doPacking` 三种取值（`DO/LOAD/SKIP`）分别走到哪条分支（可对照 u1-l5/u3-5 的 `e_stage_action`）。
2. **准备**：进入 `vpr_pack`（L720），列出它在调用 `try_pack` 前构造的全部对象及其数据来源。
3. **PB 图定位**：跳到 [`setup_vpr.cpp:347`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_vpr.cpp#L347)，确认 PB 图早就建好（不在打包阶段），并解释「为什么聚类器能在打包时直接拿来判合法性」。
4. **迭代**：在 `try_pack` 主循环里，画出 DEFAULT → SET_UNRELATED_AND_BALANCED → … → SUCCESS/FAILURE 的状态转移，并在每个状态旁标注它放宽了哪个约束。

最终产出一张「从 `vpr_pack_flow` 到 `try_pack` 成功/失败」的完整调用与状态图。这张图就是你接下来三讲（u4-l2 分子、u4-l3 聚类器、u4-l4 合法化）要逐层展开的骨架。

## 6. 本讲小结

- 打包解决「把原子级 `AtomNetlist` 按逻辑相关性装箱成 `ClusteredNetlist`」的问题，本质是一次从原子层到聚簇层的升维，且只关心逻辑块类型、不碰物理瓦片。
- PB 图（`t_pb_graph_node`）是把架构里 `t_pb_type` 类型树的 `num_pb` **真正展开成实例**的可布线模板，在 `setup` 阶段由 `alloc_and_load_all_pb_graphs` 一次性建好并放进 `DeviceContext`，是簇内合法化的依据。
- `vpr_pack` 在调用 `try_pack` 前，必须准备好 `Prepacker`、`PreClusterTimingManager`、（可选）`RamMapper`，并传入预先建好的簇内布线图 `PackerRRGraph` 与可选 flat 布局。
- `try_pack` 的核心是一个「先紧后松」的迭代状态机 `e_packer_state`：每轮 `do_clustering` + `try_size_device_grid`，装不下就逐步放宽约束（无关聚类、引脚利用率、吸引力组、APPack），直到 SUCCESS 或 FAILURE。

## 7. 下一步学习建议

本讲是打包单元的骨架，接下来的讲义会往骨架里填肉：

- **u4-l2 Prepacker 与打包分子**：深入 `Prepacker` 如何根据 pack pattern（如进位链）把原子绑成 `t_pack_molecule`，理解本讲 4.1 里 `Prepacker` 的真正产出。
- **u4-l3 贪心聚簇器 GreedyClusterer**：拆解 `do_clustering` 的种子选择与候选增益评估，看清本讲主循环里每轮到底怎么生成簇。
- **u4-l4 聚簇合法化与簇内布线**：展开 `ClusterLegalizer` + `lb_type_rr_graph`，理解本讲反复提到的「簇内布线合法性」如何判定，以及 `t_pb_graph_node::illegal_modes` 如何参与其中。

建议先把本讲的「调用与状态图」画稳，再带着它进入 u4-l2，你会更容易把分子放进 `try_pack` 的输入清单里。
