# ClusteredNetlist 聚簇网表

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 **ClusteredNetlist（聚簇网表）** 在 VPR 数据流中处于什么位置、解决什么问题。
- 区分它与上一讲 **AtomNetlist（原子网表）** 的本质差异，理解「打包阶段到底把哪些信息从原子层提升到了逻辑块层」。
- 掌握一个聚簇块（CLB）在数据结构里是如何被表示的：`t_pb*`（物理块/内部层次）、`t_logical_block_type_ptr`（逻辑块类型）、`block_logical_pins_`（逻辑引脚索引）。
- 理解 **逻辑块类型 ↔ 物理瓦片类型** 的多对多映射是如何为布局布线阶段服务的。
- 看懂聚簇网表对外的「布局布线接口」：`block_type()`、`block_pb()`、`block_pin()`、`pin_logical_index()` 等访问器，以及它如何通过 `g_vpr_ctx.clustering().clb_nlist` 被全流程共享。

## 2. 前置知识

在进入本讲前，请确认你已经掌握以下概念（它们在前面几讲已建立）：

- **VPR 数据流主线**：`AtomNetlist → Prepacker → Packer → ClusteredNetlist → Placer → Router`（见 u1-l1、u1-l3）。本讲讲的是这条链上「打包之后、布局之前」的那个数据结构。
- **Netlist 泛型基类**（u3-l1）：VPR 所有网表的共同祖先是 `Netlist<BlockId, PortId, PinId, NetId>` 模板基类，用「块/端口/引脚/网」四元实体 + `StrongId` 类型安全 ID + `vtr::vector_map`（结构数组 SoA）刻画电路；增删遵循「脏标记 + 批量 `compress()`」。
- **AtomNetlist**（u3-l2）：在拓扑之上额外存了原语模型（`LogicalModelId`）、真值表（`TruthTable`）和网别名，是从 BLIF 读入的「原子级电路」（LUT/FF/进位链等单个原语）。
- **物理类型与逻辑类型**（u2-l2）：FPGA 被建模成两层抽象——物理瓦片类型 `t_physical_tile_type`（描述网格地块）与逻辑块类型 `t_logical_block_type`（持有内部 `t_pb_type` 层次根节点），两者通过 `equivalent_sites`（物理→逻辑）和 `equivalent_tiles`（逻辑→物理）构成多对多双向链接。**本讲会反复用到 `equivalent_tiles`。**
- **`g_vpr_ctx` 全局上下文**（u1-l3、u3-l4 预告）：VPR 各阶段通过全局访问器 `g_vpr_ctx` 共享状态，聚簇阶段的产物就挂在 `g_vpr_ctx.clustering().clb_nlist` 上。

一句话直觉：**AtomNetlist 描述「电路里有哪些晶体管级原语、怎么连」，而 ClusteredNetlist 描述「这些原语被塞进了哪些物理可实现的逻辑块、块与块之间怎么连」**。打包（Packing）就是做这层「装盒」提升的动作。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [vpr/src/base/clustered_netlist.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/clustered_netlist.h) | `ClusteredNetlist` 类的声明与详尽文档注释，本讲核心。 |
| [vpr/src/base/clustered_netlist.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/clustered_netlist.cpp) | `ClusteredNetlist` 的方法实现，看清各访问器与 `create_*()` 的真实逻辑。 |
| [vpr/src/base/clustered_netlist_fwd.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/clustered_netlist_fwd.h) | 四个聚簇专用 ID 类型（`ClusterBlockId` 等）的前向定义，继承自父网表 ID。 |
| [vpr/src/base/netlist.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.h) | `Netlist` 模板基类，说明派生类与基类的调用约定（NVI）。 |
| [vpr/src/base/clustered_netlist_utils.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/clustered_netlist_utils.h) | 聚簇层↔原子层的查找桥接（`ClusteredPinAtomPinsLookup`、`ClusterAtomsLookup`）。 |
| [vpr/src/base/read_netlist.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_netlist.cpp) | 从打包产物 `.net` 文件构建 `ClusteredNetlist` 的关键入口。 |
| [vpr/src/base/vpr_context.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h) | `ClusteringContext`，把 `clb_nlist` 挂进全局上下文。 |

## 4. 核心概念与源码讲解

### 4.1 聚簇块模型：从原子到 CLB

#### 4.1.1 概念说明

原子网表里的「块」是一个个独立原语：一个 LUT、一个触发器、一个进位链单元……它们在 FPGA 上并不一定各自占用一个物理位置。真实 FPGA 的基本逻辑单元（比如经典的 LAB / LAB簇 / CLB）内部能容纳**多个原语 + 它们之间的局部互连**。

**打包（Packing / Clustering）** 就是把逻辑上紧密相关、且能塞进同一个物理逻辑单元的一组原语，合并成一个 **CLB（Clustered Logic Block，聚簇逻辑块）**。打包后：

- 块的数量大幅减少（成百上千个 LUT/FF → 几十个 CLB）。
- 每个块不再是「单个原语」，而是「一整盒原语 + 盒内连线」。
- 块与块之间的连线（网）也相应变粗变少，更接近物理布线的粒度。

`ClusteredNetlist` 就是用来描述这个「装盒之后」网表的数据结构。它在 `Netlist` 基类的「块/端口/引脚/网」四元拓扑之上，额外为每个块记录三件 AtomNetlist 没有的东西：

1. **`t_pb*`（物理块）**：这块 CLB 内部的完整层次结构（盒里装了哪些原语、原语之间怎么连），即 `t_pb/t_pb_route`。
2. **逻辑块类型指针**：这块 CLB 是哪种逻辑块类型（如 `LAB`、`RAM`、`DSP`）。
3. **逻辑引脚索引**：把全局引脚 ID 映射回「该逻辑块类型上的引脚编号」，供布局布线对齐物理引脚。

> 重要区别：ClusteredNetlist 的注释明确指出——**它基本不使用 Port（端口）**。端口在原子网表里有意义（一个多位 adder 的 A/B/OUT），但在聚簇层，块的外部接口就是逻辑块类型定义好的那一组物理引脚，端口只是个薄壳。真正承载连接信息的是 **Pin（引脚）** 和 **Net（网）**。

#### 4.1.2 核心流程

一个聚簇块在数据结构里是这样被「装」出来的（伪代码）：

```
对每一个由 packer 产出的 CLB：
    1. 确定它的逻辑块类型 type（如 "LAB"）
    2. 构造它内部的 t_pb 层次（盒内原语 + 盒内布线 pb_route）
    3. clb_nlist.create_block(name, pb, type)
         ├─ 调用基类 Netlist::create_block(name) 注册块拓扑
         ├─ 记录 block_pbs_[blk]      = pb
         ├─ 记录 block_types_[blk]    = type
         └─ block_logical_pins_[blk]  = 预分配 get_max_num_pins(type) 个空位
    4. 为它的每个外部引脚 create_pin(...)
         └─ 记录 pin_logical_index_[pin] = 该引脚在逻辑块类型上的编号
              并 block_logical_pins_[blk][pin_index] = pin
    5. 为块间连线 create_net(...) 并把驱动/接收引脚挂上去
```

注意第 3 步里 `block_logical_pins_` 被预分配成 `get_max_num_pins(type)` 个槽位——也就是该逻辑块类型**可能拥有的最大引脚数**。实际用到的引脚在对应槽位填上 `ClusterPinId`，没用到（OPEN）的槽位保持 `INVALID`。这样就能用引脚编号做 O(1) 随机访问，而不必线性扫描。

#### 4.1.3 源码精读

先看类的继承关系与本讲会反复提到的访问器声明：

[clustered_netlist.h:111-130](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/clustered_netlist.h#L111-L130) —— `ClusteredNetlist` 继承自 `Netlist`，并用四个聚簇专用 ID 实例化模板；`block_pb()` 返回盒内物理块 `t_pb*`，`block_type()` 返回逻辑块类型指针。

私有数据成员是本模块的核心，一共五张表：

[clustered_netlist.h:326-337](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/clustered_netlist.h#L326-L337) —— 这五张表就是 ClusteredNetlist 相对基类的全部「增量信息」：

- `block_pbs_`：每个块的 `t_pb*`（盒内层次与布线）。
- `block_types_`：每个块的逻辑块类型。
- `block_logical_pins_`：把「逻辑引脚索引」映射到 `ClusterPinId`（即引脚编号 → 引脚实体）。
- `blocks_per_type_`：按物理块类型分桶的块 ID 列表，**布局阶段用来快速移动某一类块**。
- `pin_logical_index_`：反向映射，`ClusterPinId` → 逻辑引脚索引。

> 💡 一个需要提醒你的「文档与现实」差异：`clustered_netlist.h` 顶部的长注释（约 16–70 行）描述了 `block_nets_` 和 `block_pin_nets_` 两个成员。**但当前代码里这两个成员并不存在**，真实实现用的是 `block_logical_pins_`。原来的「块→网」「块引脚在网中的序号」查询，现在通过 `block_pin()` 拿到 `ClusterPinId`，再走基类的 `pin_net()` / `pin_net_index()` 完成。读源码时以 `.cpp` 的真实实现为准，注释保留的是历史设计。

接下来看块的创建实现，它最能说明「装盒」做了什么：

[clustered_netlist.cpp:111-131](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/clustered_netlist.cpp#L111-L131) —— `create_block()` 先调基类 `Netlist::create_block(name)` 注册拓扑，再插入三块聚簇特有信息（`pb`、`type`、预分配的 `block_logical_pins_`），并用 `VTR_ASSERT(validate_block_sizes())` 等断言保证各表尺寸一致。

引脚创建则把全局引脚与「逻辑引脚索引」双向绑定：

[clustered_netlist.cpp:150-162](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/clustered_netlist.cpp#L150-L162) —— `create_pin()` 在基类创建引脚后，记下 `pin_logical_index_`，并把该引脚写进所属块的 `block_logical_pins_[block_id][pin_index]` 槽位。

`block_pin()` 提供按「逻辑引脚索引」反查 `ClusterPinId` 的能力：

[clustered_netlist.cpp:57-62](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/clustered_netlist.cpp#L57-L62) —— 这是一次 O(1) 数组下标访问，正是 `block_logical_pins_` 预分配槽位带来的好处。

#### 4.1.4 代码实践

**实践目标**：亲手验证「聚簇块的引脚槽位是按逻辑块类型的最大引脚数预分配的」。

**操作步骤**：

1. 打开 `clustered_netlist.cpp`，定位到 `create_block()`（[第 111 行](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/clustered_netlist.cpp#L111)）。
2. 跟进它调用的 `get_max_num_pins(type)`，其实现见 [physical_types_util.cpp:541-549](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types_util.cpp#L541-L549)。阅读后回答：它为什么遍历的是 `logical_block->equivalent_tiles`，并对每个物理瓦片取 `num_pins` 的最大值？
3. 想象一个逻辑块类型 `LAB` 可放在两种物理瓦片上（一种 26 引脚、一种 40 引脚），用伪代码推演 `block_logical_pins_[blk]` 会被分配多少个槽位、哪些槽位在只放在 26 引脚瓦片上时会是 `INVALID`。

**需要观察的现象 / 预期结果**：

- `block_logical_pins_` 的尺寸 = `get_max_num_pins(type)`，即**所有等价物理瓦片中最大的引脚数**。
- 这样设计保证：无论这块 CLB 最终被布局到哪个等价瓦片，引脚编号都不会越界；引脚数较少的瓦片对应的高位槽位保持 `INVALID`（OPEN）。
- 这一条直接体现了「逻辑块（打包产物）↔ 物理瓦片（布局目标）」解耦的设计：打包阶段不知道也无需知道具体会落在哪种瓦片上，于是按最大可能预留空间。

> 运行结果：本实践为源码阅读型，无需编译执行；若你想跑通，可结合 u1-l4 用 `run_vtr_flow.py` 跑一个小设计，再用调试器查看 `clb_nlist.block_logical_pins_` 的尺寸。

#### 4.1.5 小练习与答案

**练习 1**：`ClusteredNetlist` 相对 `Netlist` 基类，为「块」额外存了哪三组信息？

**参考答案**：`t_pb*`（盒内物理块层次与布线）、`t_logical_block_type_ptr`（逻辑块类型）、`block_logical_pins_`（逻辑引脚索引到 `ClusterPinId` 的映射，预分配为最大引脚数）。

**练习 2**：为什么 `block_logical_pins_` 要按 `get_max_num_pins(type)` 而不是按「实际用到的引脚数」来分配？

**参考答案**：因为一块逻辑块在打包时还没决定最终落在哪种等价物理瓦片上，而不同等价瓦片引脚数不同。按最大引脚数分配并以下标（逻辑引脚索引）直接寻址，能保证 O(1) 访问且对任何合法瓦片都不越界，未用到的槽位留 `INVALID`。

---

### 4.2 逻辑块↔物理类型映射

#### 4.2.1 概念说明

聚簇块只携带 **逻辑块类型**（`block_types_` 存的是 `t_logical_block_type_ptr`），而不直接携带「它在芯片网格上的物理瓦片类型」。这是 VTR「架构驱动」哲学的又一次体现：

- **打包阶段关心的是「逻辑上能不能装下」**：一块 CLB 是 `LAB` 类型，意味着它内部按 `t_pb_type` 层次组织了一堆 LUT/FF。这一层与具体摆在芯片哪个位置无关。
- **布局阶段才关心「物理上能放在哪」**：`LAB` 这个逻辑块类型可以放在 `lab_tile`、也许还能放在 `lab_tile_fast` 等若干种**等价物理瓦片**上。这个「逻辑块 → 可放置物理瓦片集合」的关系，就是 u2-l2 讲过的 `equivalent_tiles`（逻辑→物理方向）。

所以 ClusteredNetlist 把「逻辑块类型指针」存进 `block_types_`，是把「这块东西是什么」的信息固化下来；而「它能去哪儿」则交给布局算法在运行时通过 `equivalent_tiles` 查询。这种分层让打包与布局各自独立、可单独演化。

#### 4.2.2 核心流程

```
打包产物：每个 ClusterBlockId blk 都有 block_type(blk) = 某 t_logical_block_type*

布局时想知道 blk 能放在哪些物理瓦片：
    block_type(blk)->equivalent_tiles   →  vector<t_physical_tile_type*>
                                          （逻辑块类型 → 等价物理瓦片列表）

器件网格（DeviceGrid，u2-l3）的每个地块持有一个 t_physical_tile_type*。
布局算法在网格上为每个 blk 找一个其 equivalent_tiles 中出现的瓦片位置。

辅助：blocks_per_type_  把「物理块类型 index」分桶到「块 ID 列表」，
      方便布局器成批地移动/统计同一类块。
```

#### 4.2.3 源码精读

`get_max_num_pins` 是这层映射最直接的证据：

[physical_types_util.cpp:541-549](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types_util.cpp#L541-L549) —— 它从逻辑块类型出发，遍历 `logical_block->equivalent_tiles`（即「这个逻辑块能放到的所有物理瓦片」），取它们 `num_pins` 的最大值。这正是 u2-l2 建立的 `equivalent_tiles`（逻辑→物理）链接在聚簇层的使用。

聚簇块的类型访问器只是对私有表的简单封装：

[clustered_netlist.cpp:31-35](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/clustered_netlist.cpp#L31-L35) —— `block_type()` 返回 `block_types_[id]`，即 `t_logical_block_type_ptr`。注意它返回的是**逻辑**块类型，物理瓦片的确定推迟到布局。

构建聚簇块时，逻辑块类型是怎么确定的？看 `.net` 文件读入处：

[read_netlist.cpp:381-383](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_netlist.cpp#L381-L383) —— 这里 `complex_block_type` 是按块名从 `device_ctx.logical_block_types` 查到的逻辑块类型（见同文件 [第 344–354 行](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_netlist.cpp#L344-L354)），随后连同已构造好的 `pb` 一起传给 `clb_nlist.create_block(...)`。也就是说：**`.net` 文件里写的是逻辑块类型名，读入时映射成逻辑块类型指针，物理瓦片留给布局去选。**

#### 4.2.4 代码实践

**实践目标**：理清「逻辑块类型名 → 逻辑块类型指针 → 等价物理瓦片」这条链。

**操作步骤**：

1. 在 [read_netlist.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_netlist.cpp) 第 127–138 行，看 `logical_block_types` 与名字→索引表 `logical_block_type_name_to_index` 是如何从 `g_vpr_ctx.device()` 取出并建立的。
2. 跟到第 344–354 行，看 `.net` 里的类型名如何经这张表解析为 `complex_block_type`（一个 `t_logical_block_type`）。
3. 最后对照 [physical_types_util.cpp:544](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types_util.cpp#L544)，确认由这个逻辑块类型能拿到 `equivalent_tiles`（物理瓦片集合）。

**需要观察的现象 / 预期结果**：

- 数据流向是：`device_ctx.logical_block_types`（由架构 XML 解析得到，u2-l2）→ `ClusteredNetlist::block_types_`（聚簇层）→ `equivalent_tiles`（布局层查询物理瓦片）。
- 全程没有任何「写死的瓦片类型」；换一份架构 XML，这条链就指向完全不同的物理瓦片。这正是「架构驱动」在数据结构层面的落地。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ClusteredNetlist` 存的是**逻辑块类型**（`t_logical_block_type_ptr`）而不是**物理瓦片类型**？

**参考答案**：因为打包阶段只决定「逻辑上是什么」（盒内 `t_pb` 层次属于哪种逻辑块类型），不决定「物理上落在哪种瓦片」。把物理瓦片的选择推迟到布局，让打包与布局解耦；而逻辑块类型通过 `equivalent_tiles` 已经能在布局时反查到所有可放置瓦片。

**练习 2**：`blocks_per_type_` 这个「按物理块类型分桶」的表，为什么放在聚簇层而非布局层？

**参考答案**：它是布局算法（移动某一类块、按类型统计资源）的常用索引，但它的内容完全由聚簇块的类型决定、在聚簇网表建立后即可生成。把它作为聚簇层的派生索引，避免布局阶段重复构建，是一次以空间换布局阶段查找时间的常见取舍。

---

### 4.3 聚簇网表与布局布线接口

#### 4.3.1 概念说明

ClusteredNetlist 的最终使命是**成为布局（Place）和布线（Route）阶段的输入**。布局要回答「每个 CLB 摆在芯片哪个网格位置」，布线要回答「每个 CLB 之间的网怎么走线」。要支持这两件事，聚簇网表必须提供一套稳定、高效的对外接口：

- **遍历**：`blocks()`、`nets()`、`pins()`，让算法扫遍所有块/网。
- **块属性**：`block_name()`、`block_type()`、`block_pb()`、`block_input_pins()` 等。
- **拓扑追踪**：基类提供的 `pin_net()`、`net_pins()`、`pin_block()` 等（u3-l1），让算法从一个引脚跳到它的网、再跳到网上其他块。
- **聚簇特有反查**：`pin_logical_index()`、`net_pin_logical_index()`、`block_pin()`，用于把全局引脚对齐到逻辑块类型/物理瓦片的引脚编号——这是与 RR Graph（布线资源图，见 u6-l1）对接的关键。

此外，聚簇层↔原子层之间还需要**双向桥接**：布局布线完成后，要把结果「下放」回原子层做时序分析（原子引脚才有真实的延迟模型），这由 `clustered_netlist_utils.h` 里的查找类完成。

#### 4.3.2 核心流程

```
全局共享：
    g_vpr_ctx.clustering().clb_nlist   ←  ClusteringContext 持有的唯一 ClusteredNetlist
    （只读用 clustering()，可变用 mutable_clustering()）

布局器使用接口（示意）：
    for (blk : clb_nlist.blocks()) {
        type = clb_nlist.block_type(blk);   // 逻辑块类型 → 查 equivalent_tiles 找可放瓦片
        pb  = clb_nlist.block_pb(blk);      // 必要时查看盒内结构
        for (pin : clb_nlist.block_pins(blk)) {
            net = clb_nlist.pin_net(pin);    // 该引脚连到哪条网
            ipin = clb_nlist.pin_logical_index(pin); // 在物理瓦片上的引脚编号
        }
    }

桥接（聚簇 ↔ 原子）：
    ClusteredPinAtomPinsLookup：聚簇引脚 ↔ 它内部承载的原子引脚集合
    ClusterAtomsLookup：        聚簇块   ↔ 它内部包含的原子块集合
```

#### 4.3.3 源码精读

聚簇网表通过全局上下文被全流程共享：

[vpr_context.h:395-401](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L395-L401) —— `ClusteringContext` 的核心成员就是 `ClusteredNetlist clb_nlist`，外加一些布线后用于引脚修复的映射表。结合 [vpr_context.h:883-884](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L883-L884) 的 `clustering()` / `mutable_clustering()` 访问器，可见 `g_vpr_ctx.clustering().clb_nlist` 是全流程读写聚簇网表的统一入口。

聚簇层与原子层的桥接查找类：

[clustered_netlist_utils.h:10-28](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/clustered_netlist_utils.h#L10-L28) —— `ClusteredPinAtomPinsLookup` 维护两张表：一个聚簇引脚对应哪些原子引脚、一个原子引脚对应哪个聚簇引脚。它需要同时持有聚簇网表、原子网表和盒内引脚查找表 `IntraLbPbPinLookup` 才能初始化——可见它正是建立在「聚簇块内部有完整 `t_pb` 层次」这一事实之上。

[clustered_netlist_utils.h:36-47](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/clustered_netlist_utils.h#L36-L47) —— `ClusterAtomsLookup` 提供「一个聚簇块里装了哪些原子块」的查询，供把布线/时序结果下放回原子层使用。

派生类与基类的调用约定（理解接口实现方式的钥匙）：

[netlist.h:400-410](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.h#L400-L410) —— 基类注释明确：`create_*()` 由派生类（Atom/Clustered）调基类；而 `remove_*()`、`clean_*()`、`validate_*_sizes()`、`shrink_to_fit()` 采用 **NVI（Non-Virtual Interface，非虚接口）惯用法**，由基类回调派生类的 `*_impl()` 版本。所以你在 `clustered_netlist.h` 里看到的大量 `*_impl()` 私有方法（如 `clean_blocks_impl`、`rebuild_block_refs_impl`），就是被基类 `compress()` 流程在「脏标记压缩」时回调的——这与 u3-l1 讲的批量压缩机制一脉相承。

举一个 NVI 的实例：当压缩重排了块 ID 后，聚簇层必须重建 `block_logical_pins_`：

[clustered_netlist.cpp:219-228](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/clustered_netlist.cpp#L219-L228) —— `rebuild_block_refs_impl()` 在压缩后被基类回调，它先把每个块的 `block_logical_pins_` 重置为全 `INVALID`，再用每个引脚的 `pin_logical_index` 把它们重新填回正确槽位。这正是聚簇层在基类「重排 ID」后保持自身索引一致性的关键钩子。

#### 4.3.4 代码实践

**实践目标**：跟踪一条「布局器读取聚簇网表」的调用，确认接口与全局访问方式。

**操作步骤**：

1. 在仓库内搜索对聚簇网表的典型只读使用，例如打包报告处 [pack_report.cpp:31-35](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pack_report.cpp#L31-L35)，它通过 `cluster_ctx.clb_nlist.blocks()` 遍历块，并用 `block_type()`、`block_input_pins()`、`block_output_pins()`、`block_clock_pins()` 统计每类块的引脚占用。
2. 再看布线后引脚修复处 [post_routing_pb_fixup.cpp:203-207](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/post_routing_pb_fixup.cpp#L203-L207)（`post_routing_pb_pin_fixup.cpp`），它用 `clb_nlist.block_net(blk_id, pb_type_pin)` 由「块 + 逻辑引脚索引」直接取到 `ClusterNetId`，验证块内引脚与网的关系。
3. 把这两处用法与 [clustered_netlist.h:127-165](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/clustered_netlist.h#L127-L165) 的访问器声明对照，确认它们都属于「聚簇网表对布局布线的对外接口」。

**需要观察的现象 / 预期结果**：

- 这些消费方从不直接碰 `block_pbs_` / `block_types_` 等私有表，而是通过 `block_pb()`、`block_type()`、`block_net()`、`pin_logical_index()` 等访问器——即典型的「近乎不透明 ID + 访问器」封装（u3-l1 讲过的设计）。
- 所有访问都以 `g_vpr_ctx.clustering().clb_nlist` 为入口，印证聚簇网表是「一份全局共享、多数阶段只读」的状态。

> 运行结果：本实践为源码阅读型，无需执行命令；如需运行时确认，可在 u9-l2 的单测环境下断点观察 `clb_nlist` 内容。

#### 4.3.5 小练习与答案

**练习 1**：聚簇网表为什么需要 `pin_logical_index()` 这个接口？布局布线阶段用它做什么？

**参考答案**：聚簇引脚的 `ClusterPinId` 是全局的、近乎不透明的 ID，但物理瓦片上的引脚是按「逻辑引脚索引」编号的（对应 `t_logical_block_type`/`t_physical_tile_type` 的引脚表）。`pin_logical_index()` 把全局 ID 映射回这个物理编号，布局布线才能把一个聚簇引脚对齐到 RR Graph（u6-l1）里的具体节点。

**练习 2**：`rebuild_block_refs_impl()` 在什么时候被调用？为什么需要它？

**参考答案**：它在 `compress()`（批量压缩）重排块/引脚 ID 之后被基类以 NVI 方式回调。因为压缩会改变 ID 顺序，聚簇层自有的「逻辑引脚索引 → ClusterPinId」表 `block_logical_pins_` 必须按新 ID 重建，才能继续支持 O(1) 按引脚编号反查。

---

## 5. 综合实践

**任务：用一张对比表讲清「打包阶段把哪些信息从原子层提升到了逻辑块层」，并写一段伪代码模拟一次聚簇查询。**

请完成以下三步：

1. **建表对比 AtomNetlist 与 ClusteredNetlist**。阅读本讲源码与 u3-l2 回忆，填出下表（建议自己画在笔记里）：

   | 维度 | AtomNetlist | ClusteredNetlist |
   | ---- | ----------- | ---------------- |
   | 「块」代表什么 | 单个原语（LUT/FF/…） | ？ |
   | 块内是否存内部层次/布线 | 否（仅真值表/模型） | ？（关键：`t_pb*`、`pb_route`） |
   | 块携带的类型信息 | `LogicalModelId`（原语模型） | ？（`t_logical_block_type_ptr`） |
   | 是否重度使用 Port | 是（多位端口有意义） | ？（基本不用） |
   | 引脚映射 | 原子引脚 ID | ？（额外存 `pin_logical_index_`） |
   | 下游消费者 | 打包器（Prepacker/Packer） | ？（布局/布线/时序） |

   预期答案（要点）：块的粒度从「单原语」升为「一盒原语 CLB」；新增了盒内 `t_pb` 层次与 `pb_route` 盒内布线；类型从原语模型升为逻辑块类型；引脚从原子引脚升为「带逻辑引脚索引、可直接对齐物理瓦片」的聚簇引脚。

2. **写一段伪代码**：给定一个 `ClusterBlockId blk`，依次回答——它是什么逻辑块类型？能放在哪些物理瓦片？它的第 5 号逻辑引脚连到哪条网？参考本讲 4.1.3、4.2.3 的源码。

   预期伪代码（要点）：
   ```
   type   = clb_nlist.block_type(blk)                 // 逻辑块类型
   tiles  = type->equivalent_tiles                    // 可放置的物理瓦片集合
   pin    = clb_nlist.block_pin(blk, 5)               // 第 5 号逻辑引脚 → ClusterPinId
   net    = clb_nlist.pin_net(pin)                    // 该引脚连到的网
   ```

3. **定位桥接点**：在 [clustered_netlist_utils.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/clustered_netlist_utils.h) 中找到两个类，说明它们分别解决「聚簇引脚↔原子引脚」「聚簇块↔原子块」的哪个方向的下放问题，并指出它们初始化时为什么必须同时拿到聚簇网表和原子网表。

> 提示：这一步把 4.1（聚簇块模型）、4.2（逻辑↔物理映射）、4.3（接口与桥接）三个模块串起来，是本讲的验收点。

## 6. 本讲小结

- `ClusteredNetlist` 继承 `Netlist<ClusterBlockId, ClusterPortId, ClusterPinId, ClusterNetId>`，描述**打包后**的「逻辑块级」网表，是布局布线阶段的输入。
- 相对基类，它为每个块额外存了三组信息：`t_pb*`（盒内层次与 `pb_route`）、逻辑块类型指针、按最大引脚数预分配的 `block_logical_pins_`；基本不使用 Port。
- 聚簇块只携带**逻辑块类型**，物理瓦片的选择通过 `equivalent_tiles`（逻辑→物理）推迟到布局，体现架构驱动的分层解耦。
- 全流程通过 `g_vpr_ctx.clustering().clb_nlist` 共享这一份网表；消费方一律用 `block_pb()`/`block_type()`/`block_pin()`/`pin_logical_index()` 等访问器，不碰私有表。
- 派生类与基类遵循约定：`create_*()` 派生调基类；`remove_*/clean_*/rebuild_*` 走 NVI 由基类回调 `*_impl()`，例如压缩后 `rebuild_block_refs_impl()` 重建 `block_logical_pins_`。
- `clustered_netlist_utils.h` 的 `ClusteredPinAtomPinsLookup` / `ClusterAtomsLookup` 负责把聚簇层结果下放回原子层，供时序分析使用。
- 一处「文档与现实」差异：`clustered_netlist.h` 顶部注释提到的 `block_nets_`/`block_pin_nets_` 已不存在，真实实现是 `block_logical_pins_` + 基类 `pin_net()`/`pin_net_index()`。

## 7. 下一步学习建议

- **下一步自然进入 u3-l4（VprContext 与全局状态管理）**：本讲已反复使用 `g_vpr_ctx.clustering().clb_nlist`，下一讲会系统讲清 `VprContext` 聚合的各子上下文（Atom/Device/Clustering/Placement/Routing/Timing）与 mutable/immutable 访问模式，把「全局状态」这层彻底打通。
- **接着读 u3-l5（主流程编排 vpr_api）**：看 `vpr_flow` 如何把打包产物 `ClusteredNetlist` 交接给布局、布线，理解阶段间数据如何经 Context 传递。
- **想直接看消费方**：可先跳读 u5 单元（布局）开头，确认布局器如何用 `block_type()->equivalent_tiles` 选瓦片；以及 u6-l1（RR Graph），看 `pin_logical_index()` 如何对齐到布线节点。
- **延伸阅读**：[clustered_netlist.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/clustered_netlist.h) 顶部 1–104 行的官方注释（注意其中 `block_nets_`/`block_pin_nets_` 为历史描述）与 [netlist.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/netlist.h) 的「Interactions with other netlists」一节，是理解两层网表分工的最佳一手资料。
