# 物理类型数据结构 physical_types

## 1. 本讲目标

上一讲（u2-l1）我们把架构 XML 读进了内存，得到了 `t_arch`、物理瓦片集合与逻辑块集合三组产物。本讲要回答的核心问题是：**这些产物到底是什么形状？它们之间如何相互指认？**

学完本讲，你应当能够：

- 区分「物理瓦片类型 `t_physical_tile_type`」与「逻辑块类型 `t_logical_block_type`」这两层抽象，并说出为什么 VTR 要把它们拆成两层。
- 读懂 `t_pb_type` / `t_mode` / `t_port` 描述的层次化逻辑块模型，能判断一个 `t_pb_type` 是根节点、中间节点还是叶子原语。
- 说出顶层 `t_arch` 持有哪些全局信息，以及 `t_arch_switch_inf` 如何刻画一个开关的电气与延迟特性。
- 看懂「物理瓦片 ⇄ 逻辑块」之间的双向链接（`equivalent_sites` 与 `equivalent_tiles`），并能在源码中定位这条链接是在哪里建立的。

---

## 2. 前置知识

在进入源码前，先用三个生活化的比喻建立直觉。

**比喻一：房子与户型。**
一片 FPGA 就像一个住宅小区。小区里有「地块」——每个地块上能盖一栋楼，地块有固定的宽和高（`width`/`height`）、固定的门牌朝向（引脚位置 `pinloc`）。这是**物理层**：它关心的是「这块地有多大、门朝哪开」，不关心里面住的是谁。

同一块地上可以放不同功能的户型（住宅、商铺、办公），只要功能接口（门口能通的水电管线）对得上就行。这种「能放在同一块地上的功能描述」就是**逻辑层**。

**比喻二：类与实例。**
`t_physical_tile_type` 和 `t_logical_block_type` 都带一个 `type`，说明它们是「类型描述」而非「实例」。就像「汽车」是一个类型，停车场第 3 号车位上停的那辆车才是实例。VTR 用类型对象（flyweight 享元）描述「这一类瓦片长什么样」，而网格上每一个具体坐标存的是对该类型的指针。

**比喻三：套娃（俄罗斯套娃）。**
一个逻辑块内部不是平铺的，而是像套娃一样层层嵌套：最外层是一个「复杂逻辑块」（如 CLB），打开一层是若干「子块」（如 BLE），再打开一层是「原语」（如 LUT、FF）。这种嵌套用 `t_pb_type` 描述，打开的「层数」用 `depth` 记录，最里层那个没有下一层的娃娃就是「原语（primitive）」。

理解了这三个比喻，下面的源码结构就好读了：物理瓦片说「地块」，逻辑块说「户型」，`t_pb_type` 说「套娃的层数」。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [libs/libarchfpga/src/physical_types.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h) | 本讲的主战场。定义了物理瓦片、逻辑块、`t_pb_type` 层次、`t_arch`、开关与线段等全部架构相关数据结构。 |
| [libs/libarchfpga/src/arch_types.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/arch_types.h) | 一组架构级常量与枚举，例如 `ARCH_FPGA_UNDEFINED_VAL`（-1）、`MAX_CHANNEL_WIDTH`、`e_arch_format`。是 `physical_types.h` 的依赖头。 |
| [libs/libarchfpga/src/arch_util.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/arch_util.cpp) | 提供 `get_equivalent_sites_set` 与 `link_physical_logical_types`，在物理瓦片与逻辑块之间建立双向链接——本讲第一个实践任务的核心。 |
| [libs/libarchfpga/src/read_xml_arch_file.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/read_xml_arch_file.cpp) | 解析 XML 时向 `sub_tile->equivalent_sites` 填入逻辑块指针，是双向链接中「物理→逻辑」方向的数据来源。 |

> 阅读建议：先扫一遍 `physical_types.h` 顶部那段注释（前 26 行），它用一段话概括了「物理瓦片、逻辑块、pb_type、pb_graph_node」四个核心概念，是整份头文件的导言。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：物理块与逻辑块类型、`t_pb_type` 层次模型、`t_arch` 与开关信息。

### 4.1 物理块与逻辑块类型：两层抽象

#### 4.1.1 概念说明

VTR 把 FPGA 的「物理资源」与「逻辑功能」建模成两层，这是 VTR 架构建模最关键的设计之一：

- **物理瓦片类型 `t_physical_tile_type`**：描述网格上一个可放置地块的**物理**特征——它的名字、宽高、引脚数量、引脚在瓦片四周的分布、开关盒配置、面积等。它关心「这块地长什么样」，不关心里面实现什么功能。一个物理瓦片内含若干 **子瓦片 `t_sub_tile`**，子瓦片才真正说明「这个物理位置上能放哪些逻辑功能」。
- **逻辑块类型 `t_logical_block_type`**：描述一个**逻辑功能**及其内部层次——它持有一个 `t_pb_type` 指针（即下一节要讲的套娃层次根节点），说明「这种户型内部怎么组织」。逻辑块类型是**打包阶段**（Packing）的主要消费者。

为什么要分两层？因为同一个物理瓦片可以承载多种逻辑功能（异构放置），而同一种逻辑功能也可以落到多种物理瓦片上。这种多对多关系用一层结构表达不清，于是 VTR 用两条指针列表把它们连起来：

- 从物理瓦片看：「我这块地上能放哪些逻辑功能？」——`sub_tile.equivalent_sites`（等价站点列表）。
- 从逻辑块看：「我这个功能能放到哪些物理瓦片上？」——`logical_block.equivalent_tiles`（等价瓦片列表）。

这正是本讲实践任务要梳理的「双向链接」。

#### 4.1.2 核心流程

物理瓦片与逻辑块的链接，是在架构解析完成后才建立的，分两步：

1. **解析阶段（XML → 结构）**：读 `<tile>` 里的子瓦片时，把该子瓦片声明的可放置逻辑块名字，查找成 `t_logical_block_type*` 指针，存进 `sub_tile->equivalent_sites`。这一步建立了「物理→逻辑」方向。
2. **链接阶段（`link_physical_logical_types`）**：遍历所有物理瓦片，把它的全部等价站点汇总，再反向回填到每个逻辑块的 `equivalent_tiles` 列表里。这一步建立了「逻辑→物理」方向，并校验「每个逻辑块至少有一个等价瓦片」，否则报致命错。

用伪代码表示：

```
# 解析阶段（read_xml_arch_file.cpp）
for sub_tile in physical_tile.sub_tiles:
    for name in sub_tile 声明的可放置站点名:
        sub_tile.equivalent_sites.push_back( 查找得到的 t_logical_block_type* )

# 链接阶段（arch_util.cpp::link_physical_logical_types）
for physical_tile in PhysicalTileTypes:
    eq_sites = 汇总(physical_tile 各 sub_tile 的 equivalent_sites)   # get_equivalent_sites_set
    for logical_block in LogicalBlockTypes:
        if logical_block 出现在 eq_sites 中:
            logical_block.equivalent_tiles.push_back(&physical_tile)
for logical_block in LogicalBlockTypes:
    if logical_block.equivalent_tiles 为空:
        报致命错  # 逻辑块无处可放
```

#### 4.1.3 源码精读

先看物理瓦片类型的核心字段。[t_physical_tile_type](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L541-L632) 定义了瓦片的全部物理特征，关键成员：

- `name` / `num_pins` / `width` / `height`：瓦片名、引脚总数、占地宽高（行 542–554）。
- `pinloc`：一个三维布尔矩阵 `[宽][高][4 边][引脚]`，标记某引脚是否出现在瓦片的某条边上（行 556）。这就是「门朝哪开」的精确记录。
- `class_inf` / `pin_class`：逻辑等价引脚类，把可互换的引脚归为一组（行 558、568）。
- `sub_tiles`：子瓦片列表（行 591）——真正承载「能放什么逻辑功能」的容器。
- `index`：该类型在类型数组中的下标，方便用整数快速引用（行 588）。

注意物理瓦片自己**不直接**持有 `equivalent_sites`，而是把这件事下放给子瓦片 `t_sub_tile`。看 [t_sub_tile](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L682-L720)：

- [sub_tile.equivalent_sites](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L691-L692) 是一个 `std::vector<t_logical_block_type_ptr>`，即「能在本子瓦片放置的逻辑块指针列表」——这正是「物理→逻辑」方向的链接。
- `capacity`：本子瓦片在同一物理位置上的可放置实例数范围（行 694–698），注释给出的例子是「容量范围 4 到 7 表示有 4 个可放置实例」。

再看逻辑块类型。[t_logical_block_type](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L888-L921) 的关键成员：

- `pb_type`：指向内部层次根节点（行 892），下一节细讲。
- `pb_graph_head`：指向展开后的 PB 图根节点（行 893），是 `t_pb_type` 层次的「实例化展开」产物。
- [equivalent_tiles](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L897-L898) 是 `std::vector<t_physical_tile_type_ptr>`，即「本逻辑块可放到哪些物理瓦片」——这是「逻辑→物理」方向的链接，在链接阶段被回填。

这两条列表互为反向，构成多对多关系。

解析阶段的数据来源在 [read_xml_arch_file.cpp:3349](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/read_xml_arch_file.cpp#L3347-L3352)：当子瓦片声明的逻辑块名与某 `logical_block_type->pb_type->name` 匹配时，把该逻辑块指针 `push_back` 进 `sub_tile->equivalent_sites`。

链接阶段在 [link_physical_logical_types](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/arch_util.cpp#L992-L1022)。它先用 [get_equivalent_sites_set](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/arch_util.cpp#L411-L421) 把一个物理瓦片所有子瓦片的 `equivalent_sites` 汇总成一个集合，再在第 [1017](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/arch_util.cpp#L1014-L1021) 行反向回填 `logical_block.equivalent_tiles`。

#### 4.1.4 代码实践

**实践目标**：亲手画出「物理瓦片 ⇄ 逻辑块」的双向链接关系图，验证你真的理解了两层抽象。

**操作步骤**：

1. 打开 [physical_types.h:541](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L541)，定位 `t_physical_tile_type`，找到它的 `sub_tiles` 成员。
2. 跳到 `t_sub_tile`（行 682），找到 `equivalent_sites` 成员，确认其类型是「逻辑块指针的 vector」。
3. 跳到 `t_logical_block_type`（行 888），找到 `equivalent_tiles` 成员，确认其类型是「物理瓦片指针的 vector」。
4. 打开 [arch_util.cpp:411](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/arch_util.cpp#L411)，读 `get_equivalent_sites_set`，确认它把物理瓦片各子瓦片的 `equivalent_sites` 合并。
5. 画一张关系图（建议用纸笔或任意画图工具），形如：

   ```
   t_physical_tile_type "IO"  ──sub_tiles──▶  t_sub_tile
                                                    │
                              equivalent_sites (物理→逻辑)
                                                    ▼
                                  t_logical_block_type "inpad"
                                  t_logical_block_type "outpad"
                                                    ▲
                              equivalent_tiles (逻辑→物理)  ← link_physical_logical_types 回填
                                                    │
   t_physical_tile_type "IO"  ◀────────────────────┘
   ```

**需要观察的现象**：物理瓦片 → 逻辑块用的是 `equivalent_sites`（且存放在 `sub_tile` 而非瓦片本身），逻辑块 → 物理瓦片用的是 `equivalent_tiles`（直接存放在 `t_logical_block_type`）。两边命名不对称、存放位置也不对称。

**预期结果**：你应当能用一句话解释这个不对称：「物理瓦片把放置能力按子瓦片细分，所以等价站点挂在子瓦片上；逻辑块的等价瓦片列表只是个汇总，无需细分，故直接挂在逻辑块上。」

**说明**：本实践为源码阅读型，无需运行；若你已按 u1-l2 构建过 VPR，可自行在 `arch_util.cpp` 的第 1030 行附近加一行 `VTR_LOG(...)` 打印每个逻辑块的 `equivalent_tiles.size()`，重新构建后跑任意架构，观察输出——这属于「加日志观察行为」的扩展实践。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `equivalent_sites` 挂在 `t_sub_tile` 上，而 `equivalent_tiles` 直接挂在 `t_logical_block_type` 上？

**参考答案**：一个物理瓦片可以包含多个异构子瓦片，每个子瓦片能放的逻辑功能不同，所以「能放什么」必须按子瓦片分别记录，于是 `equivalent_sites` 挂在 `t_sub_tile` 上。反过来，对逻辑块而言，它只关心「我能去哪些瓦片」这个汇总信息，不需要区分瓦片内部的子瓦片，所以 `equivalent_tiles` 直接挂在 `t_logical_block_type` 上即可。

**练习 2**：如果一个逻辑块的 `equivalent_tiles` 为空，会发生什么？

**参考答案**：`link_physical_logical_types` 会在遍历时检测到 `equivalent_tiles.size() <= 0`（arch_util.cpp:1029），调用 `archfpga_throw` 抛出致命错误「Logical Block X does not have any equivalent tiles」。这符合 u2-l1 讲过的「错误经 `archfpga_throw` 转成带文件名行号的致命报错」机制。

---

### 4.2 t_pb_type 层次模型：套娃结构

#### 4.2.1 概念说明

逻辑块内部不是平的，而是一个树状层次，由四类对象描述，它们与架构 XML 的标签一一对应（见 `physical_types.h` 第 935–943 行的注释表）：

| 结构 | XML 标签 | 含义 |
| --- | --- | --- |
| `t_pb_type` | `<pb_type/>` | 一层物理块类型，套娃的一层壳。 |
| `t_mode` | `<mode/>` | 一个 pb_type 的工作模式（同一块电路的不同实现方式）。 |
| `t_interconnect` | `<interconnect/>` | 父子 pb_type 之间的连线（complete/direct/mux）。 |
| `t_port` | `<port/>` | pb_type 上的 I/O 与时钟端口。 |

关键的递归关系是：

- 一个 `t_pb_type` 有若干 `t_mode`（`modes` 数组）。
- 每个 `t_mode` 又包含若干子 `t_pb_type`（`pb_type_children` 数组）。
- 于是 `pb_type → mode → pb_type → mode → ...` 形成一棵树。

这棵树有三种角色：

- **根 `t_pb_type`**：`parent_mode == nullptr`，对应顶层逻辑块（如 CLB）。
- **中间 `t_pb_type`**：有父、也有子模式。
- **叶子/原语 `t_pb_type`**：`num_modes == 0`，不能再打开，对应一个原子电路单元（LUT、FF、进位链等），通常带 `blif_model` / `model_id`。

注意「类型 vs 实例」：一个 `t_pb_type` 用 `num_pb` 表示「父节点下最多有几个这种类型的实例」。`t_pb_type` 是类型（享元，全工程唯一），实例层面由 `t_pb_graph_node` 表示（下一讲会更细，本讲只要知道 `pb_graph_head` 是展开后的根即可）。

#### 4.2.2 核心流程

`t_pb_type` 树的构建与判读流程：

1. **构建**：解析 `<pb_type/>` 标签时递归创建 `t_pb_type`，遇到 `<mode/>` 创建 `t_mode` 并把子 `<pb_type/>` 挂到 `t_mode::pb_type_children`。
2. **角色判定**：
   - `is_root()` ⟺ `parent_mode == nullptr`。
   - `is_primitive()` ⟺ `num_modes == 0`。
3. **深度**：`depth` 字段记录从根到该节点的层数。
4. **展开**：构造完成后，由 PB 图构建器（后续讲义）把类型树按 `num_pb` 展开为实例树 `t_pb_graph_node`。

判定的真值表：

| `parent_mode` | `num_modes` | 角色 |
| --- | --- | --- |
| `nullptr` | `> 0` | 根（顶层块） |
| 非 `nullptr` | `> 0` | 中间节点 |
| 任意 | `0` | 叶子原语 |

> 注意：根节点一般 `num_modes > 0`，但理论上根也可能是原语（极简单架构）。

#### 4.2.3 源码精读

[t_pb_type](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L976-L1028) 的关键成员：

- `name` / `num_pb` / `blif_model` / `model_id`：名字、实例数、BLIF 模型串、逻辑模型 ID（行 977–980）。`blif_model` 非空通常意味着这是叶子原语。
- `modes` / `num_modes`：模式数组及其数量（行 983–984）——这是递归向下的「抓手」。
- `ports` / `num_ports`：本层 pb_type 的端口（行 985–986）。
- `parent_mode` / `depth`：父模式指针、层级深度（行 994–995），`parent_mode` 是否为空决定了根/非根。

两个内联判定函数是本模块的核心：

- [is_root()](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L1012-L1014)：`return parent_mode == nullptr;`
- [is_primitive()](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L1021-L1023)：`return num_modes == 0;`

再看模式节点 [t_mode](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L1050-L1066)：

- `pb_type_children` / `num_pb_type_children`：本模式下包含的子 pb_type 数组（行 1052–1053）——递归向下的另一半「抓手」。
- `interconnect` / `num_interconnect`：父子 pb_type 之间的连线（行 1054–1055）。
- `disable_packing`：用户可声明某模式对打包器不可见（行 1060），被打包器忽略的模式不会有逻辑映射进来。

于是递归骨架可写作：

```cpp
// 示例代码：遍历 pb_type 子树的伪代码
void walk(const t_pb_type* pb) {
    if (pb->is_primitive()) { /* 叶子：LUT/FF 等 */ return; }
    for (int m = 0; m < pb->num_modes; ++m) {
        const t_mode* mode = &pb->modes[m];
        for (int c = 0; c < mode->num_pb_type_children; ++c) {
            walk(&mode->pb_type_children[c]);   // 递归向下
        }
    }
}
```

> 标注：上面是「示例代码」，用于说明遍历思路，并非项目原有函数。

端口 [t_port](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L1171-L1203) 与簇内连线 [t_interconnect](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L1118-L1150) 的字段含义已在源码注释中给出，这里不展开，关键知道：连线有三种类型，由枚举 `e_interconnect`（`COMPLETE_INTERC`/`DIRECT_INTERC`/`MUX_INTERC`，见行 176–181）区分——分别对应「全连接」「一对一」「多选一」。

#### 4.2.4 代码实践

**实践目标**：在一个真实的 `t_pb_type` 层次里数清楚「根 / 中间 / 原语」各有哪些，加深对递归结构的肌肉记忆。

**操作步骤**：

1. 打开 [physical_types.h:976](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L976)，确认 `is_root()` 与 `is_primitive()` 的判定条件。
2. 在仓库里找一个有真实层次的架构文件，例如 `vtr_flow/arch/` 下任意复杂架构（可用 `grep -rl "<pb_type" vtr_flow/arch/ | head` 列出候选）。
3. 打开该架构 XML，找到根 `<pb_type>`（通常在 `<complexblocklist>` 下），画出它的 `pb_type → mode → pb_type` 嵌套树（用缩进表示深度）。
4. 在树上标注：哪些是根、哪些是叶子（叶子通常带 `blif_model="..."` 属性），哪些是中间节点。

**需要观察的现象**：叶子 `<pb_type>` 都带有 `blif_model` 属性（如 `.names`、`.latch`），且自身不再含 `<mode>`；中间和根节点不含 `blif_model` 但含一个或多个 `<mode>`，每个 `<mode>` 下又有子 `<pb_type>`。

**预期结果**：你能画出至少三层深的树，并指出每层对应的结构体（`t_pb_type` / `t_mode`），且能说出「叶子的 `num_modes` 在解析后被置为 0，因此 `is_primitive()` 返回 true」。

**说明**：若不确定某个架构文件是否合适，`vtr_flow/arch/equivalent_sites/` 目录下的架构专门演示「等价站点」，层次通常较清晰，可作为首选。本实践为源码与配置阅读型，无需运行 VPR。

#### 4.2.5 小练习与答案

**练习 1**：给定一个 `t_pb_type*`，如何用一次判断知道它是叶子原语？如何知道它是不是根？

**参考答案**：调用 `is_primitive()`，它返回 `num_modes == 0`；调用 `is_root()`，它返回 `parent_mode == nullptr`。注意两个判定相互独立：一个原语既可能是根（极简架构，`parent_mode == nullptr` 且 `num_modes == 0`），也可能是叶子。

**练习 2**：`t_pb_type` 与 `t_pb_graph_node` 的区别是什么？（提示见本节概念说明与头文件第 16–17 行注释）

**参考答案**：`t_pb_type` 是「类型」，描述某一层物理块类型长什么样，全工程唯一（享元）；`t_pb_graph_node` 是 `t_pb_type` 按 `num_pb` **展开后的实例**——一个 `num_pb=10` 的 pb_type 会展开成 10 个 pb_graph_node，因为每个实例在簇内的位置不同、可访问的簇内布线资源也不同（见头文件第 1268–1272 行注释）。本讲只关注类型层，实例层留给后续 PB 图讲义。

---

### 4.3 t_arch 与开关信息：顶层容器

#### 4.3.1 概念说明

前面两节讲的是「逻辑块与物理瓦片」这条线，本节看「布线架构」这条线。`t_arch` 是整个架构解析的**顶层容器**，它把所有全局信息收拢在一起，最终整体写入 `DeviceContext`（见 u3-l4）。

`t_arch` 主要装三类东西：

1. **布线架构全局开关**：如 `sb_type`（开关盒类型）、`Fs`（开关盒扇出）、`Chans`（通道宽度分布）、是否 `tileable`（可平铺）、`through_channel`（通道能否穿过大块）等。
2. **线段与开关集合**：`Segments`（线段类型数组）、`switches`（开关类型数组）、`directs`（直接链连接数组）。
3. **逻辑模型与杂项**：`models`（逻辑模型，如 LUT/FF/乘法器）、`power`（功耗架构）、`clocks`（时钟网络）、`noc`（片上网络）、`grid_layouts`（候选布局规格）等。

其中最需要单独理解的是**开关 `t_arch_switch_inf`**。开关是 FPGA 里连接两段线或线与引脚的可配置元件（多路选择器、三态缓冲、传输门、硬短接线、缓冲器）。VTR 用 `e_switch_type` 枚举区分这几类，并统一用 `t_arch_switch_inf` 描述其电气（电阻 R、电容 Cin/Cout/Cinternal）与延迟（Tdel）特性。

#### 4.3.2 核心流程

开关的延迟模型值得单独说明。一个开关的本征延迟 `Tdel` 可能随其扇入（fanin，输入个数）变化，也可能不变。`t_arch_switch_inf` 用一个 `std::map<int,double> Tdel_map_` 存储「扇入 → 延迟」的映射：

- 若只有一条 `UNDEFINED_FANIN`（-1）的记录，则延迟与扇入无关，是常数。
- 否则按扇入查表，必要时插值/外推。

判定是否为常数延迟用 `fixed_Tdel()`。延迟取值用 `Tdel(int fanin = UNDEFINED_FANIN)`。

开关的电气分类逻辑：

- `buffered()`：是否把输入输出隔离成两个直流不相连的子电路（缓冲型开关 yes，传输门 no）。
- `configurable()`：是否可配置（MUX/三态/传输门 yes，SHORT 硬短接 no）。
- `type()`：返回 `e_switch_type`。

这些布尔属性并非独立字段，而是从 `type_` 推导出来的（见 `switch_type_is_buffered` 等自由函数，行 1733–1735）。

#### 4.3.3 源码精读

[t_arch](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L1959-L2086) 的代表性成员：

- `architecture_id`：架构文件的安全哈希摘要（行 1966），用于唯一标识一份架构。
- `tileable` / `through_channel` / `perimeter_cb` / `shrink_boundary`：可平铺与通道相关的布线架构选项（行 1973–1983）。
- `Chans` / `sb_type` / `Fs`：通道宽度分布、开关盒类型、开关盒扇出（行 2005–2010）。
- `Segments`：线段类型数组，元素是 `t_segment_inf`（行 2012）。
- `switches`：开关类型数组，元素是 `t_arch_switch_inf`（行 2015）。
- `directs`：直接链连接数组（行 2018），对应架构文件的 `<directlist>`，用于 CLB 间硬连线（如进位链）。
- `models`：逻辑模型集合（行 2020），定义了原子电路单元的「功能原型」。
- `grid_layouts`：一组候选布局规格（行 2060），由命令行 `--device` 选择其一。

线段类型 [t_segment_inf](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L1579-L1687) 描述一段布线导线：

- `name` / `length` / `frequency`：名字、长度（跨多少个逻辑块）、在该通道中的出现比例（行 1581–1587）。
- `Rmetal` / `Cmetal`：单位长度导线的电阻/电容（行 1632–1635）——决定 RC 延迟。
- `frac_cb` / `frac_sb`：连接盒/开关盒连接比例（行 1621–1627）。
- `arch_wire_switch` / `arch_opin_switch`：导线/输出引脚连接用的开关索引（行 1594–1601），指向 `switches` 数组。

开关类型枚举 [e_switch_type](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L1721-L1731) 列出五种开关：`MUX`（可配置多选一，单驱动）、`TRISTATE`（可配置三态缓冲，多驱动）、`PASS_GATE`（传输门，多驱动）、`SHORT`（不可配置硬短接，多驱动）、`BUFFER`（不可配置非三态缓冲，单驱动）。

开关结构 [t_arch_switch_inf](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L1760-L1846)：

- `R` / `Cin` / `Cout` / `Cinternal`：等效电阻与三处电容（行 1766–1791）。注意第 1773–1790 行的注释画出了「传输门串缓冲」的等效电路，解释了为什么会有一个内部电容 `Cinternal`。
- `Tdel(int fanin)` 与私有 `Tdel_map_`：延迟查表（行 1823、1843）。
- `type()` / `buffered()` / `configurable()`：从 `type_` 推导的属性查询（行 1810–1817）。

两个常量字符串也值得知道：`VPR_DELAYLESS_SWITCH_NAME`（`__vpr_delayless_switch__`，VPR 内部的零延迟开关，用于 SOURCE/SINK 之间和 `<directlist>` 连接，行 1746）与 `VPR_INTERNAL_SWITCH_NAME`（扁平路由器自动添加的簇内开关，行 1749）。它们不是用户在 XML 里声明的，而是 VPR 内部生成。

#### 4.3.4 代码实践

**实践目标**：把一份架构 XML 里的线段和开关，对应到 `t_arch` / `t_segment_inf` / `t_arch_switch_inf` 的字段，建立「XML 属性 ⇄ 结构体成员」的对应能力。

**操作步骤**：

1. 打开一个架构 XML，找到 `<switchlist>` 与 `<segmentlist>` 两段（u2-l1 讲过这两段的解析顺序）。
2. 在 `<switchlist>` 下挑一个 `<switch>`，记下它的 `name`、`R`、`Cin`、`Cout`、`Tdel`、`buf`/`mux`/`pass_gate` 等属性。
3. 打开 [t_arch_switch_inf](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L1760)，逐个把 XML 属性映射到结构体成员（`name→name`、`R→R`、`Cin→Cin`、`Cout→Cout`、`Tdel→Tdel_map_`、开关形态→`type_`）。
4. 在 `<segmentlist>` 下挑一个 `<segment>`，记下 `name`、`length`、`freq`、`Rmetal`、`Cmetal`、`frac_cb`、`frac_sb`。
5. 打开 [t_segment_inf](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L1579)，做同样的字段映射。
6. 最后，确认线段里的 `arch_opin_switch` 指向 `<switchlist>` 中的哪一个开关（用名字或索引对应）。

**需要观察的现象**：`<switchlist>` 必须先于 `<segmentlist>` 解析（u2-l1 结论），因为线段需要引用已解析的开关索引；你能在两段之间看到这种「先后依赖」。

**预期结果**：你能产出一张「XML 属性 → 结构体字段」对照表，并解释「为什么开关要在线段之前解析」——因为线段的 `arch_wire_switch`/`arch_opin_switch` 是对开关数组的下标引用，必须先有开关数组。

**说明**：本实践为配置与源码阅读型，无需运行 VPR。若想进一步验证，可在 `read_xml_arch_file.cpp` 中开关解析函数（处理 `<switch>` 标签处）加日志打印每个开关的 `name` 与 `R/Cin/Cout`，重新构建后跑一次架构，对照你手填的表格。

#### 4.3.5 小练习与答案

**练习 1**：一个开关的 `Tdel` 在什么情况下是常数、什么情况下随扇入变化？如何用代码判断？

**参考答案**：当 `Tdel_map_` 只含一条键为 `UNDEFINED_FANIN`（-1）的记录时，延迟与扇入无关，是常数；否则 `Tdel_map_` 存了「扇入 → 延迟」的多条记录，延迟随扇入变化（可能插值/外推）。代码上用 `t_arch_switch_inf::fixed_Tdel()` 判断是否为常数，用 `Tdel(int fanin)` 取值。

**练习 2**：`buffered()` 与 `configurable()` 这两个开关属性，分别排除掉哪一类开关？

**参考答案**：`configurable()` 返回 false 的只有 `SHORT`（硬短接线，不可配置）。`buffered()` 返回 false 的是 `PASS_GATE` 和 `SHORT`（它们不把输入输出隔离成两个直流不相连的子电路）；`MUX`、`TRISTATE`、`BUFFER` 是缓冲型的。这两个属性都是从 `e_switch_type` 经 `switch_type_is_buffered` / `switch_type_is_configurable` 推导出来的，不是独立存储的字段。

---

## 5. 综合实践

把三个最小模块串起来，做一个「架构体检」小任务。

**任务**：选一个真实架构文件（推荐 `vtr_flow/arch/equivalent_sites/` 下的架构，或任意 `k6_N10_*.xml` 类架构），完成下列「体检表」，全部用源码字段名作答：

1. **物理层**：该架构定义了几个物理瓦片类型？挑一个瓦片，写出它的 `name`、`width`、`height`、`num_pins`，并指出它有几个 `t_sub_tile`。
2. **链接**：在该瓦片的某个子瓦片里，`equivalent_sites` 列出了哪些逻辑块？再反向验证：在源码层面，这些逻辑块的 `equivalent_tiles` 是否会回填到本瓦片（说明依据是 `link_physical_logical_types` 的哪一段逻辑）。
3. **逻辑层**：挑一个等价站点（逻辑块），进入它的 `pb_type`，画出根 → mode → 子 pb_type 的两层树，标注哪个节点 `is_primitive()` 为真、为什么。
4. **布线层**：在 `t_arch` 的 `switches` 与 `Segments` 里，各挑一个元素，写出它的 `name` 及 3 个关键字段；并指出某个线段的 `arch_opin_switch` 指向哪个开关。
5. **一处自检**：用一句话回答——如果把这个架构的某个物理瓦片的全部子瓦片的 `equivalent_sites` 都删空，VPR 会在哪一步、以什么错误信息崩溃？

**预期产出**：一张填满源码字段名的架构体检表，外加一句对崩溃点的判断（答案应是：`link_physical_logical_types` 会因受影响的逻辑块 `equivalent_tiles` 为空而抛出「Logical Block X does not have any equivalent tiles」致命错——前提是该逻辑块除了这个瓦片无处可去）。

**说明**：这是源码与配置阅读型综合实践，无需运行 VPR；但若你已构建 VPR，可故意编辑架构 XML 删空某子瓦片的可放置站点，重新运行 VPR 观察崩溃信息，作为「破坏式验证」（注意：破坏的是你本地的工作副本，不要提交）。

---

## 6. 本讲小结

- VTR 把 FPGA 建模成两层：**物理瓦片 `t_physical_tile_type`**（地块：尺寸、引脚位置、子瓦片）与**逻辑块 `t_logical_block_type`**（户型：内部 `pb_type` 层次）。
- 两层通过子瓦片的 `equivalent_sites`（物理→逻辑）与逻辑块的 `equivalent_tiles`（逻辑→物理）建立**多对多双向链接**，后者由 `link_physical_logical_types` 回填并校验。
- 逻辑块内部是 **`t_pb_type` 套娃树**：`pb_type → mode → pb_type` 递归，根用 `parent_mode == nullptr` 判定，叶子原语用 `num_modes == 0` 判定；`t_pb_graph_node` 是其展开的实例层（本讲略过，留给后续）。
- **`t_arch`** 是架构解析的顶层容器，聚合布线架构选项、线段数组 `Segments`、开关数组 `switches`、直接链 `directs`、逻辑模型 `models` 等。
- **`t_arch_switch_inf`** 统一刻画开关的电气（R/Cin/Cout/Cinternal）与延迟（`Tdel` 可随扇入变化），由 `e_switch_type`（MUX/TRISTATE/PASS_GATE/SHORT/BUFFER）区分形态，并派生出 `buffered()`/`configurable()` 属性。
- 调试直觉：架构相关的反常行为，先回到本讲这些结构里查「链接是否建立、层次是否正确、开关延迟是否随扇入变化」，再怀疑算法。

---

## 7. 下一步学习建议

- 下一讲 **u2-l3 器件网格 DeviceGrid 的生成** 会把本讲的物理瓦片铺到二维网格上，理解 `create_device_grid` 如何决定器件尺寸与瓦片排布。届时你会看到 `t_physical_tile_type` 如何被网格上的坐标引用，巩固「类型 vs 实例」的区分。
- 想提前看 `t_pb_type` 如何展开成实例层，可先浏览 `physical_types.h` 中 `t_pb_graph_node`（行 1283 起）与 `t_pb_graph_pin`（行 1381 起）的注释——这是 PB 图讲义（u4-l1）的预习材料。
- 若你对「等价站点」的异构放置机制感兴趣，可直接读 `vtr_flow/arch/equivalent_sites/` 目录下的架构与对应回归测试，对照本讲的 `equivalent_sites`/`equivalent_tiles` 字段理解其工程效果。
- 建议同步回顾 u2-l1：本讲的全部结构都是 `xml_read_arch` 的「产物」，把「解析过程（u2-l1）」与「产物形状（本讲）」对照阅读，能形成完整闭环。
