# 构建 grid / routing / memory / mux / lut 子模块

## 1. 本讲目标

上一讲（u6-l2）我们看清了 `build_fabric` 自下而上的整体调用链：从 constant 到 essential，再到 mux / lut / wire / memory，最后才是 grid、routing、tile、top。本讲要钻进这条链的中段，逐一打开六类「子模块构建器」的内部实现。

学完本讲你应该能够：

- 说清 `build_grid_modules` 如何沿着 VPR 的 pb graph 递归向下，最终在叶子节点用 `device_annotation` 反查到物理电路模型来实例化 primitive。
- 区分 `build_unique_routing_modules`（压缩）与 `build_flatten_routing_modules`（不压缩）两种布线模块生成方式，并理解它们都以 `device_rr_gsb` 为遍历对象。
- 理解 `build_memory_modules` 如何按 `configuration_protocol` 把同一组配置位组织成 flatten / chain / frame 三种存储器子模块。
- 说出 mux、lut、decoder 三类「纯电路模型驱动」的构建器各自的遍历对象（`mux_lib`、`circuit_lib`、`mux_lib`）与去重逻辑。
- 能用「按名字反查已存在模块」这一统一线索，解释为什么 mux/lut/decoder/memory 必须先于 grid/routing 构建。
- 理解「总线型 mux 互连」（VPR 的 `<mux bus="true"/>`）：grid 构建器如何用 `pb_graph_utils` 判定一条互连是总线型 mux，并用**单个共享存储器**驱动整条 bus 的 W 个 mux（对应一份共享配置位）。

---

## 2. 前置知识

本讲默认你已掌握以下概念（若生疏请先回看对应讲义）：

- **ModuleManager 四要素**（u6-l1）：模块 `ModuleId`、端口 `ModulePortId`、子模块实例（`add_child_module`）、网 `ModuleNet`。本讲的所有构建器都在反复做「建模块 → 加端口 → 实例子模块 → 连网」四件事。
- **build_fabric 自下而上顺序**（u6-l2）：子模块必须先于父模块存在，否则父模块在实例化时找不到子模块。本讲正是这条顺序约束的具体落地。
- **OpenfpgaContext 与 device_annotation**（u5-l3 / u5-l4）：`link_openfpga_arch` 在 VPR 的 device 上挂了一张「平行账本」`VprDeviceAnnotation`，把电路模型 `CircuitModelId`、physical mode 等信息以 VPR 对象指针为键记下来。本讲里 grid / routing 都是在「消费」这张账本。
- **circuit_library 与配置协议**（u3-l3 / u3-l4）：circuit_library 是电路级积木库，`configuration_protocol` 决定配置位如何被组织（scan_chain / memory_bank / frame_based …）。本讲的 memory / mux / lut 构建器都直接读 circuit_library。

两个本讲会反复出现、需要先记住的术语：

- **按名字反查（by-name lookup）**：父构建器不直接拿到子模块的 `ModuleId`，而是用命名规则（如 `generate_mux_subckt_name`）拼出子模块名，再用 `module_manager.find_module(name)` 找回。这正是「构建顺序即依赖」的运行机制。
- **device_rr_gsb**：设备级通用开关块（General-purpose Switch Block）标注，记录每个坐标的 SB 与 CB（见 u5-l4）。它是 routing 构建器的主要遍历对象，也是 unique module 压缩的来源。

---

## 3. 本讲源码地图

本讲涉及的源码主要在 `openfpga/src/fabric/` 目录，总线型 mux 还涉及 `openfpga/src/utils/` 下的两个辅助文件：

| 文件 | 作用 |
| --- | --- |
| `build_device_module.cpp` | 总编排器 `build_device_module_graph`，按固定顺序调用下面六类构建器（u6-l2 已讲，本讲作入口参照）。 |
| `build_grid_modules.cpp` | 构建 grid 模块：递归遍历 pb graph 生成 logical tile，再包裹成 physical tile（CLB / IO）；并处理 pb_type 内的互连（含总线型 mux）。 |
| `build_routing_modules.cpp` | 构建 routing 模块：SB（switch block）与 CB（connection block），提供 unique / flatten 两套入口。 |
| `build_memory_modules.cpp` | 构建配置存储器子模块：按协议分派 flatten / chain / frame，并提供 `add_physical_memory_module` 把物理存储器挂到父模块。 |
| `build_mux_modules.cpp` | 构建多路选择器：先建共享 branch 子电路，再建完整 mux。 |
| `build_lut_modules.cpp` | 构建 LUT：遍历 circuit_library 中所有 LUT 模型。 |
| `build_decoder_modules.cpp` | 构建译码器：mux 本地译码器 + memory 地址译码器（frame / BL / WL）。 |
| `utils/pb_graph_utils.{h,cpp}` | pb graph 工具：新增 `is_pb_graph_pin_bus_interc`（判定互连是否为总线型 mux）与 `pb_graph_interc_sink_pins`（收集一条总线型互连驱动的所有 sink 引脚）。 |
| `utils/module_manager_utils.cpp` | 模块连网工具：新增 `add_module_nets_between_logics_and_shared_memory_sram_bus`，把一个存储器的 SRAM 输出扇出到多个逻辑实例（总线型 mux 共享存储器时使用）。 |

永久链接基址（当前 HEAD `97c06e27a`）：

```
https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/
```

---

## 4. 核心概念与源码讲解

在进入五个最小模块前，先用一张表锁定六类构建器在总编排器里的**调用顺序与遍历对象**，这张表是本讲所有细节的索引。顺序来自 [`build_device_module.cpp:36-194`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_device_module.cpp#L36-L194)：

| 顺序 | 调用 | 遍历对象 | 主要消费的数据 |
| --- | --- | --- | --- |
| 1 | `build_constant_generator_modules`（L56） | 固定 | VDD/GND |
| 2 | `build_user_defined_modules`（L62） | `circuit_lib` | 用户自定义网表模型 |
| 3 | `build_essential_modules`（L65） | `circuit_lib` | inv/buf/pass_gate 等基础门 |
| 4 | `build_mux_local_decoder_modules`（L69） | `mux_lib` | mux 本地译码器 |
| 5 | `build_mux_modules`（L73） | `mux_lib` | 多路选择器 |
| 6 | `build_lut_modules`（L77） | `circuit_lib` | 查找表 |
| 7 | `build_wire_modules`（L80） | `circuit_lib` | 布线段导线 |
| 8 | `build_memory_modules`（L83） | `mux_lib` + `circuit_lib` | 配置存储器 |
| 9 | `build_grid_modules`（L89） | `device_ctx` 的 tile 类型 | pb graph + `device_annotation` |
| 10 | `build_unique_routing_modules`（L109）/ `build_flatten_routing_modules`（L117） | `device_rr_gsb` | SB/CB + `device_annotation` |
| 11 | `build_fabric_tile` + `build_tile_modules`（L128/L135，可选） | `device_rr_gsb` + grid | tile 聚合 |
| 12 | `build_top_module`（L147） | grid + routing | 顶层组装 |

记住一个规律：**越靠近表的底部，模块越大、越靠近顶层；越靠上，越「原子」**。mux/lut/decoder/memory 都排在 grid/routing 之前，正是因为后两者要用 `find_module` 按名字把前者建好的模块实例化进来。

下面按五个最小模块依次展开（4.1 grid、4.2 routing、4.3 memory、4.4 mux/lut/decoder，4.5 是 grid 的扩展——总线型 mux 互连）。

### 4.1 grid 模块构建

#### 4.1.1 概念说明

grid 模块对应 FPGA 阵列里的**逻辑块阵列**——CLB（可编程逻辑块）与 IO 块。在 OpenFPGA 的模块图里，一个 grid 模块并不是凭空生成的，而是从 VPR 架构里的 **pb graph**（physical block graph，描述逻辑块内部从 clb → fle → lut/ff 的层次结构）自顶向下递归展开的。

这里有一个关键区分（u4-l3 已建立）：

- **logical tile**：pb graph 的物理实现模式（physical mode）逐层展开得到的模块树，叶子是 primitive（lut、ff、iopad 等电路模型）。
- **physical tile**：把整棵 logical tile 树包裹成一个可被顶层实例化的「瓦片」模块，CLB 一个、IO 按所在边（TOP/RIGHT/BOTTOM/LEFT）各一个。

grid 构建器要做的事就是：先 DFS 递归把 logical tile 全部建出来，再为每种 physical tile 类型建一个外层模块。

#### 4.1.2 核心流程

```
build_grid_modules
├─ 遍历 device_ctx.logical_block_types（每种逻辑块类型）
│   └─ rec_build_logical_tile_modules(pb_graph_head)        # DFS
│        ├─ 非叶子：先递归所有 physical mode 的子 pb_type
│        ├─ 叶子：build_primitive_block_module              # 实例化电路模型
│        └─ 非叶子建完子模块后：add_module + 加端口 + 实例子模块 + 配置总线
└─ 遍历 device_ctx.physical_tile_types（每种物理块类型）
    ├─ IO 块：按 find_physical_io_tile_located_sides 每条边建一个模块
    └─ CLB 等：建一个 NUM_2D_SIDES 模块
```

注意 DFS 的方向：**先递归子节点，再建当前节点**（后序）。因为当前 pb_type 模块要把子 pb_type 模块实例化进来，子模块必须先存在——这与上一讲的「自下而上」总原则一致。源码注释也明确强调必须用 DFS 而非 BFS，否则子模块无法正确注册到父模块。

#### 4.1.3 源码精读

入口 [`build_grid_modules`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_grid_modules.cpp#L1515-L1601) 分两段：先建 logical tile（L1541-L1551），再建 physical tile（L1559-L1597）。IO 块的特殊处理在 L1564-L1585——按边建多个模块：

```cpp
} else if (physical_tile.is_io()) {
  std::set<e_side> io_type_sides =
    find_physical_io_tile_located_sides(device_ctx.grid, &physical_tile);
  for (const e_side& io_type_side : io_type_sides) {
    status = build_physical_tile_module(/* ..., io_type_side, ... */);
```

递归主体 [`rec_build_logical_tile_modules`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_grid_modules.cpp#L1101-L1140) 体现了后序 DFS：先用 `device_annotation.physical_mode(physical_pb_type)`（L1115）拿到物理模式，递归子节点（L1120-L1130），叶子节点交给 `build_primitive_block_module`（L1133-L1140）：

```cpp
if (false == is_primitive_pb_type(physical_pb_type)) {
  for (int ipb = 0; ipb < physical_mode->num_pb_type_children; ++ipb) {
    rec_build_logical_tile_modules(/* ... child_pb_graph_nodes ... */);
  }
}
if (true == is_primitive_pb_type(physical_pb_type)) {
  build_primitive_block_module(/* ... */);
  return;
}
```

最关键的一行——**grid 如何把 pb_type 绑到电路模型**——在 [`build_primitive_block_module`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_grid_modules.cpp#L287-L299)：

```cpp
const CircuitModelId& primitive_model =
  device_annotation.pb_type_circuit_model(primitive_pb_graph_node->pb_type);
```

注意：grid 构建器**不直接查 `circuit_lib`**，而是通过 `device_annotation.pb_type_circuit_model()` 读取——这正是 `link_openfpga_arch`（u5-l3）在 pb_type 上挂好的电路模型 ID。pb_type 内部的 interconnect（互连）同样如此，电路模型来自 [`device_annotation.interconnect_circuit_model()`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_grid_modules.cpp#L767-L768)。

互连的具体构建在 [`add_module_pb_graph_pin_interc`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_grid_modules.cpp#L745-L806) 中按 `interc_type` 分派：直接互连短接、单驱动短接、多驱动则实例化一个 mux。mux 与其存储器的实例化被抽取成两个共享辅助函数 `add_module_pb_graph_pin_mux_instance`（L546，建 mux 并连好数据输入/输出网）与 `add_module_pb_mux_memory`（L609，建存储器并注册为 configurable child）。当一个互连带 VPR 的 `bus="true"` 属性时（总线型 mux），会走专门的 `add_module_pb_bus_mux_interc`，我们在 4.5 单独讲。

还要注意 grid 模块自己**不直接生成配置存储器电路**，而是先把「逻辑存储器」（带 SRAM 端口的子模块）实例化好，再由 [`add_physical_memory_module`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_grid_modules.cpp#L1373) 把一个物理存储器组挂上来（见 4.3.3）。

> 留意：`build_grid_modules` 的函数签名里**没有** `device_rr_gsb` 参数（见 `build_grid_modules.h`）。这是 grid 与 routing 最大的区别——grid 走 pb graph，不走 rr graph 的 GSB。

#### 4.1.4 代码实践

**目标**：验证 grid 构建器只通过 `device_annotation`（而非直接查 `circuit_lib`、也不用 `device_rr_gsb`）拿到电路模型。

**步骤**：

1. 打开 [`build_grid_modules.cpp`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_grid_modules.cpp)，在文件内搜索 `device_annotation.`，统计它被调用了哪几种访问器（`pb_type_circuit_model`、`interconnect_circuit_model`、`physical_mode` 等）。
2. 在同一文件搜索 `device_rr_gsb`，确认搜不到（签名里就没有这个参数）。
3. 跟踪 `build_primitive_block_module`（L287）：拿到 `primitive_model` 后，它用 `circuit_lib` 的哪些访问器（如 `model_ports_by_type`）来给模块加端口？

**预期现象**：grid 内对 `circuit_lib` 的使用都是「读模型属性」（端口、设计技术等），而「哪个 pb_type 用哪个模型」的决策完全来自 `device_annotation`。

**待本地验证**：若你已按 u1-l4 跑通过 `run-task`，可在生成的 fabric Verilog 的 `SRC/` 目录下找到形如 `grid_io_*.v`、`grid_clb*.v`、`lut*.v` 的网表，对照本节确认 grid 模块的命名来源（`generate_physical_block_module_name`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `rec_build_logical_tile_modules` 必须是后序 DFS（先递归子节点、再建当前节点），而不是先建当前节点再递归？

> **答案**：因为当前 pb_type 模块要用 `find_module` 按名字把子 pb_type 模块实例化进来（见 L1170-L1175 的 `find_module(child_pb_module_name)`）。子模块必须先注册进 ModuleManager，父模块才能找到它；前序遍历会导致 `find_module` 返回无效 ID，触发断言失败。

**练习 2**：IO 块为什么可能生成多个模块，而 CLB 通常只生成一个？

> **答案**：IO 块分布在 FPGA 的不同边界（TOP/RIGHT/BOTTOM/LEFT），不同边的 IO 对外端口的「朝向」不同，因此按边各建一个模块（L1576-L1585）；CLB 位于阵列内部、四面对称，只需一个 `NUM_2D_SIDES` 模块（L1586-L1596）。

---

### 4.2 routing 模块构建（unique 与 flatten）

#### 4.2.1 概念说明

routing 模块对应 FPGA 的**可编程互连**，分两种：

- **SB（switch block，开关块）**：位于布线轨道交点，内部是一组多路选择器，决定轨道之间的连接关系。
- **CB（connection block，连接块）**：把逻辑块的引脚连到布线轨道，同样是多路选择器。

一个真实 FPGA 可能有成千上万个 SB/CB，但其中大量是**互为镜像**的（u5-l4 讲过的 unique module 压缩思想）。OpenFPGA 因此提供两条入口：

- `build_unique_routing_modules`：只构建 `device_rr_gsb` 识别出的 unique 模块（压缩模式，对应 `--compress_routing on`）。
- `build_flatten_routing_modules`：为每个坐标都构建一个模块（不压缩，模块数量 = 阵列规模）。

#### 4.2.2 核心流程

两条入口的唯一区别是**遍历范围**，构建单个 SB/CB 的逻辑是共享的：

```
build_unique_routing_modules              build_flatten_routing_modules
├─ for each unique SB module              ├─ for (ix, iy) in gsb_range
│   └─ build_switch_block_module(rr_gsb)  │   └─ if SB exists: build_switch_block_module(rr_gsb)
├─ for each unique CBX module             └─ (若未 group_routing)
│   └─ build_connection_block_module        ├─ build_flatten_connection_block_modules(CHANX)
└─ for each unique CBY module               └─ build_flatten_connection_block_modules(CHANY)
    └─ build_connection_block_module
```

无论哪条入口，单个 SB 的构建都是：以 `rr_gsb` 为数据源加四边的布线轨道端口 → 遍历每条输出轨道的驱动节点 → 若只有一个驱动则短接（`build_switch_block_module_short_interc`），若多个驱动则实例化一个 mux（`build_switch_block_module_mux_module`）。

#### 4.2.3 源码精读

对比两条入口的遍历写法最能体现 unique vs flatten 的差异。flatten 版 [`build_flatten_routing_modules`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_routing_modules.cpp#L1302-L1338) 遍历**全部坐标**：

```cpp
vtr::Point<size_t> sb_range = device_rr_gsb.get_gsb_range();
for (size_t ix = 0; ix < sb_range.x(); ++ix) {
  for (size_t iy = 0; iy < sb_range.y(); ++iy) {
    const RRGSB& rr_gsb = device_rr_gsb.get_gsb(ix, iy);
    if (false == device_rr_gsb.get_gsb_edges(ix, iy).is_sb_exist(rr_gsb)) continue;
    build_switch_block_module(/* ..., rr_gsb, ... */);
  }
}
```

unique 版 [`build_unique_routing_modules`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_routing_modules.cpp#L1351-L1399) 只遍历**去重后的镜像代表**：

```cpp
for (size_t isb = 0; isb < device_rr_gsb.get_num_sb_unique_module(); ++isb) {
  const RRGSB& unique_mirror = device_rr_gsb.get_sb_unique_module(isb);
  build_switch_block_module(/* ..., unique_mirror, ... */);
}
```

压缩的效果直接体现在循环次数上：flatten 是 `O(阵列面积)`，unique 是 `O(unique 数量)`，后者通常小一两个数量级。注释（L1340-L1350）也明确：unique 版**应当且仅当** `compress_routing` 打开时调用。

那么 routing 如何决定实例化哪个 mux 子模块？答案在 [`build_switch_block_mux_module`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_routing_modules.cpp#L107-L144)：

```cpp
/* Get the circuit model id of the routing multiplexer */
CircuitModelId mux_model =
  device_annotation.rr_switch_circuit_model(switch_index);
/* ... */
std::string mux_module_name = generate_mux_subckt_name(
  circuit_lib, mux_model, datapath_mux_size, std::string(""));
ModuleId mux_module = module_manager.find_module(mux_module_name);
VTR_ASSERT(true == module_manager.valid_module_id(mux_module));
module_manager.add_child_module(sb_module, mux_module);
```

这就是「按名字反查」的标准范式：

1. 用 `device_annotation.rr_switch_circuit_model(switch_index)` 拿到这个开关对应的电路模型（switch→模型的映射在 link 阶段建立，u5-l3）。
2. 用 `generate_mux_subckt_name` 拼出 mux 模块名。
3. `find_module` 找回 mux 模块——它必须已在步骤 5（`build_mux_modules`）建好。
4. `add_child_module` 实例化进 SB。

与 grid 一样，routing 也用 [`add_physical_memory_module`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_routing_modules.cpp#L905) 把物理存储器挂到 SB/CB 上（CB 在 L1203）。

#### 4.2.4 代码实践

**目标**：体会 unique 与 flatten 在「模块数量」上的巨大差异，并验证 routing 对 `device_rr_gsb` 的依赖。

**步骤**：

1. 对比 [`build_flatten_routing_modules`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_routing_modules.cpp#L1302-L1338) 与 [`build_unique_routing_modules`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_routing_modules.cpp#L1351-L1399) 的循环上限：前者用 `get_gsb_range()`，后者用 `get_num_sb_unique_module()` / `get_num_cb_unique_module()`。
2. 在 `build_routing_modules.cpp` 内搜索 `device_rr_gsb.`，列出它提供了哪些信息（gsb 坐标范围、unique 列表、某坐标的 `RRGSB`）。
3. 在 [`build_switch_block_mux_module`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_routing_modules.cpp#L120-L132) 处确认：决定实例化哪个 mux 的依据是 `device_annotation.rr_switch_circuit_model(switch_index)`，而非直接查 `circuit_lib`。

**预期现象**：routing 的「要实例化哪些子模块」由两部分决定——**有哪些坐标/unique** 来自 `device_rr_gsb`，**每个位置实例化哪种 mux** 来自 `device_annotation`（再到 `circuit_lib` 取模型属性）。

**待本地验证**：若分别用 `--compress_routing on/off` 跑同一设计，对比生成的 `sb_*.v` / `cb_*.v` 文件数量，压缩后应大幅减少。

#### 4.2.5 小练习与答案

**练习 1**：总编排器在 L108-L123 用 `if (compress_routing) ... else ...` 选择两条入口。如果误把 `build_unique_routing_modules` 放到 `else` 分支（即不压缩时调用 unique 版），会发生什么？

> **答案**：当 `device_rr_gsb` 没有做过 unique 识别（`is_compressed_` 为假，u5-l4）时，`get_num_sb_unique_module()` 可能尚未填充或退化为全量，导致要么报错、要么实际仍是全量构建，压缩效果丧失。注释 L1348-L1350 明确要求 unique 版只在 `compact_routing_hierarchy` 打开时调用。

**练习 2**：SB 内部的一条输出轨道有 1 个驱动 vs 多个驱动，分别走哪条分支？

> **答案**：1 个驱动走 `build_switch_block_module_short_interc`（直接短接线网，不实例化 mux）；多个驱动走 `build_switch_block_mux_module`（实例化一个 N 选 1 的 mux，N = 驱动数 `datapath_mux_size`）。

---

### 4.3 memory 模块构建

#### 4.3.1 概念说明

这里的 memory 指**配置存储器**（configurable memory）——FPGA 里那些保存「这一个 mux 选第几路、这一根线通不通」的可编程位。一个 mux 有 N 个配置位，就需要一个能容纳 N 位的存储器子模块跟在它旁边。这个存储器子模块长什么样，由 `configuration_protocol`（u3-l4）决定：

- **flatten（standalone / memory_bank / ql_memory_bank）**：每位存储器独立暴露端口，memory_bank 下进一步组织成 BL/WL 矩阵。
- **chain（scan_chain）**：存储器串成移位寄存器链，靠 `din` 串入、`dout` 串出。
- **frame（frame_based）**：用地址译码器选通某帧，配合数据寄存器写入。

本节讲的 `build_memory_modules` 生成的是「**逻辑存储器**」——每个 mux 或电路模型配一个独立的小存储器模块。它们随后会被 `add_physical_memory_module` 聚合成「物理存储器组」挂到 grid/routing/top 上（对应 u6-l1 的 logical vs physical configurable children）。

#### 4.3.2 核心流程

```
build_memory_modules
├─ 遍历 mux_lib.muxes()                         # 给每个 MUX 配存储器
│   ├─ 跳过非 MUX 模型（LUT 另算）
│   └─ build_mux_memory_module
│        └─ build_memory_module(num_config_bits)
└─ 遍历 circuit_lib.models()                    # 给非 MUX（带 SRAM 端口）配存储器
    ├─ 跳过 MUX 与无 SRAM 端口的模型
    └─ build_memory_module(num_mems)

build_memory_module(num_mems) ──按 sram_orgz_type 分派──┐
   CONFIG_MEM_STANDALONE / MEMORY_BANK / QL_MEMORY_BANK → build_memory_flatten_module
   CONFIG_MEM_SCAN_CHAIN                              → build_memory_chain_module
   CONFIG_MEM_FRAME_BASED                             → build_frame_memory_module
```

注意 `num_config_bits` 不一定等于 mux 的 memory 数：当 mux 启用了本地译码器时，配置位数会被压缩成 \(\lceil \log_2(\text{memory 数}) \rceil\)。

#### 4.3.3 源码精读

入口 [`build_memory_modules`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_memory_modules.cpp#L1175-L1258) 有两段循环：先给 MUX 配（L1186-L1208，跳过 LUT 因为 LUT 的存储器含 regular + mode-select 两类，需另算），再给「带 SRAM 端口的非 MUX 模型」配（L1214-L1256）。第二段用 `find_circuit_sram_models` 拿存储模型并断言只有 1 个 SRAM 模型（L1232-L1235）。

最核心的分派逻辑在 [`build_memory_module`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_memory_modules.cpp#L937-L964)，一个 `switch(sram_orgz_type)`：

```cpp
switch (sram_orgz_type) {
  case CONFIG_MEM_STANDALONE:
  case CONFIG_MEM_QL_MEMORY_BANK:
  case CONFIG_MEM_MEMORY_BANK:
    build_memory_flatten_module(/* ... */); break;
  case CONFIG_MEM_SCAN_CHAIN:
    build_memory_chain_module(/* ... */); break;
  case CONFIG_MEM_FRAME_BASED:
    build_frame_memory_module(/* ... */); break;
}
```

这就是「同一份配置位，三种物理组织」的分流点。对 MUX 场景，存储器位数由 [`build_mux_memory_module`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_memory_modules.cpp#L1057-L1100) 先算出：

```cpp
size_t num_config_bits =
  find_mux_num_config_bits(circuit_lib, mux_model, mux_graph, sram_orgz_type);
```

另一个关键函数是 [`add_physical_memory_module`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_memory_modules.cpp#L1486-L1512)。它不建新的「逻辑存储器」，而是递归找出父模块（grid/routing/top）下所有逻辑存储器子模块，按总配置位数建一个「物理存储器组」挂上去：

```cpp
size_t module_num_config_bits =
  find_module_num_config_bits_from_child_modules(/* ... */);
if (module_num_config_bits == 0) return CMD_EXEC_SUCCESS;   // 无配置位就不挂
```

grid（`build_grid_modules.cpp` 的 [L1373](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_grid_modules.cpp#L1373)）、routing SB（[L905](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_routing_modules.cpp#L905)）、routing CB（[L1203](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_routing_modules.cpp#L1203)）都会调它。这与 u6-l1 讲的「logical 承载逻辑位置、physical 携带坐标与区域」完全对应——logical 存储器由本节的 `build_memory_modules` 产出，physical 存储器组由 `add_physical_memory_module` 产出，后者才是 fabric 比特流（u7）真正寻址的对象。

#### 4.3.4 代码实践

**目标**：确认 memory 模块的「形状」由 `configuration_protocol` 决定，而非由电路模型决定。

**步骤**：

1. 读 [`build_memory_module`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_memory_modules.cpp#L937-L964) 的 switch，列出哪三种协议共用 `build_memory_flatten_module`。
2. 读 [`build_mux_memory_module`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_memory_modules.cpp#L1057-L1066)，注意 `num_config_bits` 由 `find_mux_num_config_bits(..., sram_orgz_type)` 计算——即位数也依赖协议。
3. 对比 u3-l4 讲过的 `cc`（scan_chain）与 `frame`（frame_based）两个 arch 文件，预判它们各自会走 switch 的哪个 case。

**预期现象**：`cc` arch → `CONFIG_MEM_SCAN_CHAIN` → `build_memory_chain_module`；`frame` arch → `CONFIG_MEM_FRAME_BASED` → `build_frame_memory_module`。

**待本地验证**：分别用 cc 与 frame 两个配置跑同一基准，在 fabric Verilog 的 `SRC/` 里找形如 `*_config_chain_*.v` 与 `*_frame_*.v` 的存储器子模块，确认二者结构不同。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `build_memory_modules` 要分两段循环（先遍历 `mux_lib`，再遍历 `circuit_lib`），而不是只遍历 `circuit_lib`？

> **答案**：MUX 的存储器位数由 MuxGraph 决定（可能因本地译码器而压缩，`find_mux_num_config_bits`），且一个 MUX 模型可能对应多个不同尺寸的 mux（每种尺寸一个存储器模块），所以必须按 `mux_lib` 里的实际 mux 逐个生成；非 MUX 模型（如带 mode-select SRAM 的 IO）没有 MuxGraph，按模型本身的 SRAM 端口总数生成即可，故单独一段。

**练习 2**：`add_physical_memory_module` 在什么情况下会「什么都不做」就返回？

> **答案**：当父模块的子模块总计配置位数为 0 时（L1510-L1512），例如一个纯组合逻辑、无任何可编程位的模块，就不需要物理存储器，直接返回成功。

---

### 4.4 mux / lut / decoder 模块构建

#### 4.4.1 概念说明

这三类构建器有一个共同点：它们**只由电路模型驱动**，不直接依赖 VPR 的 device 结构（不读 pb graph，也不读 `device_rr_gsb`），因此排在构建顺序的最前面，是 grid/routing 的「弹药库」。

- **mux**：遍历 `mux_lib`（link 阶段从 rr graph 收集去重后的所有 mux，u10-l2 会详讲）。先把每个 mux 拆成共享的 **branch 子电路**（2:1 或 N:1 的一级 mux），再组装成完整 mux。branch 共享能让多个大 mux 复用同一个小子电路。
- **lut**：遍历 `circuit_lib` 中所有 `CIRCUIT_MODEL_LUT` 模型，跳过「用户自定义网表」（带 verilog/spice netlist 的 LUT 由 `build_user_defined_modules` 处理）。LUT 本质是一个大 mux 加一组配置位。
- **decoder**：分两类——mux 的**本地译码器**（当 mux 启用 `mux_use_local_encoder` 时，把 \(\lceil\log_2 N\rceil\) 位地址译成 N 位 one-hot），以及 memory 的**地址译码器**（frame / BL / WL）。

#### 4.4.2 核心流程

```
build_mux_modules
├─ 第一遍：为每个 mux 生成 branch 子电路（build_mux_branch_graphs → build_mux_branch_module）
└─ 第二遍：为每个 mux 生成完整模块（build_mux_module）

build_lut_modules
└─ 遍历 circuit_lib.models()，过滤 LUT 且非用户自定义 → build_lut_module

build_mux_local_decoder_modules
├─ 第一遍：统计所有需要本地译码器的 mux，按尺寸去重加入 DecoderLibrary
└─ 第二遍：为每个 unique decoder 生成模块（build_mux_local_decoder_module）
（另：build_frame/bl/wl_memory_decoder_module 在需要时单独调用）
```

#### 4.4.3 源码精读

mux 构建器 [`build_mux_modules`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_mux_modules.cpp#L1783-L1809) 的两遍结构很清晰：第一遍建 branch（L1789-L1800），第二遍建完整 mux（L1803-L1808）：

```cpp
for (auto mux : mux_lib.muxes()) {
  std::vector<MuxGraph> branch_mux_graphs = mux_graph.build_mux_branch_graphs();
  for (auto branch_mux_graph : branch_mux_graphs)
    build_mux_branch_module(module_manager, circuit_lib, mux_circuit_model, branch_mux_graph);
}
for (auto mux : mux_lib.muxes()) {
  build_mux_module(module_manager, circuit_lib, mux_circuit_model, mux_graph);
}
```

lut 构建器 [`build_lut_modules`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_lut_modules.cpp#L509-L526) 的过滤逻辑值得注意——**跳过用户自定义 LUT**（L519-L523）：

```cpp
if (CIRCUIT_MODEL_LUT != circuit_lib.model_type(lut_model)) continue;
if ((false == circuit_lib.model_verilog_netlist(lut_model).empty()) ||
    (false == circuit_lib.model_spice_netlist(lut_model).empty())) continue;
build_lut_module(module_manager, circuit_lib, lut_model);
```

用户自定义 LUT 在步骤 2（`build_user_defined_modules`）已整体注册，这里只生成 OpenFPGA 自己用电路模型展开的 LUT。`build_lut_module`（L33+）会把 LUT 的 SRAM 端口分成 regular 与 mode-select 两类（L61-L64），对应 u3-l3 讲的 mode-select sram。

decoder 的本地译码器构建器 [`build_mux_local_decoder_modules`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_decoder_modules.cpp#L314-L357) 同样是「先统计去重、再逐个生成」的两段式：

```cpp
for (auto mux : mux_lib.muxes()) {
  if (false == circuit_lib.mux_use_local_encoder(mux_circuit_model)) continue;  // L329
  for (auto branch_mux_graph : branch_mux_graphs) {
    size_t decoder_data_size = branch_mux_graph.num_memory_bits();
    if (0 == decoder_data_size) continue;
    add_mux_local_decoder_to_library(decoder_lib, decoder_data_size);            // L349 去重
  }
}
for (const auto& decoder : decoder_lib.decoders())
  build_mux_local_decoder_module(module_manager, decoder_lib, decoder);          // L354-L356
```

译码器模块本身的端口由「地址位数 = 输入数」决定，公式为（见 [`build_frame_memory_decoder_module`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_decoder_modules.cpp#L40-L81) 顶部注释）：

\[
\text{addr\_size} = \left\lceil \frac{\log(\text{data\_size})}{\log 2} \right\rceil
\]

因为 one-hot 编码下 data_size 个输出只需 data_size 种状态，地址位取以 2 为底的对数向上取整即可。BL 译码器（[`build_bl_memory_decoder_module`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_decoder_modules.cpp#L101-L165)）与 WL 译码器（L166+）结构类似，只是服务于 memory_bank 的两条总线。

> 顺带回应总编排器里的一处顺序约束：注释 L67-L68 强调 `build_mux_local_decoder_modules` **必须**先于 `build_mux_modules`——因为带本地译码器的 mux 在 `build_mux_module` 时要用 `find_module` 找回译码器子模块。

#### 4.4.4 代码实践

**目标**：理解三类构建器「只读 circuit_lib / mux_lib」的共同特征，以及 branch 共享如何减少模块数。

**步骤**：

1. 对比三个入口的参数列表：[`build_mux_modules`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_mux_modules.cpp#L1783-L1784) 只收 `(module_manager, mux_lib, circuit_lib)`，[`build_lut_modules`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_lut_modules.cpp#L509-L510) 只收 `(module_manager, circuit_lib)`——确认它们都没有 `device_ctx` / `device_rr_gsb` 参数。
2. 在 [`build_mux_modules`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_mux_modules.cpp#L1789-L1800) 里追踪 `build_mux_branch_graphs()`：一个 8 选 1 mux 会被拆成哪些 branch？多个不同尺寸的 mux 是否会共享同一个 2:1 branch？
3. 在 [`build_mux_local_decoder_modules`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_decoder_modules.cpp#L338-L351) 里确认：尺寸相同的译码器只生成一次（`add_mux_local_decoder_to_library` 内部去重）。

**预期现象**：mux 模块的数量等于「不同电路模型 × 不同输入尺寸」的组合数；branch 子电路数量更少且被多个 mux 复用。

**待本地验证**：在生成的 fabric Verilog 的 `SRC/` 目录下，统计 `mux*_size*.v`（完整 mux）、`mux*_size*_branch*.v`（branch）、`decoder*.v`（译码器）的文件数，验证 branch 数 < 完整 mux 数。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `build_mux_modules` 要先单独生成 branch 子电路，再生成完整 mux？

> **答案**：完整 mux 内部是多个 2:1（或一级 N:1）branch 的树状组合。先建 branch 让不同尺寸的 mux 复用同一批 branch 子电路（例如 4:1 和 8:1 都能复用 2:1 branch），既减少模块总数，也便于网表复用。完整 mux 再用 `add_child_module` 把 branch 实例化组装起来。

**练习 2**：一个带本地译码器的 16 选 1 mux，需要多少位地址输入？对应译码器数据输出多少位？

> **答案**：地址位 \(\lceil\log_2 16\rceil = 4\) 位；译码器输出 16 位 one-hot（一次只选中 1 位）。地址位 4 < 数据位 16，正是用译码器压缩配置位数的收益。

---

### 4.5 总线型 mux 互连（bus-based mux）

> 这是 4.1（grid）的扩展。本次更新（PR #2602「Bus Based MUX」）让 OpenFPGA 支持把 VPR arch 中带 `bus="true"` 属性的 `<mux>` 互连实现为「多个逻辑实例共享同一存储器/选择器」的总线型多路选择器。它的代码完全落在 `build_grid_modules.cpp` 的 pb graph 互连构建里，因此紧接 4.1 讲。

#### 4.5.1 概念说明

到目前为止，我们讲的互连里的 mux 都是「按位独立」的：一个 N 选 1 的 mux 配一份 N 位的配置存储器。但 VPR 架构里有一种特殊的互连——**总线型 mux**，在 `<mux>` 标签上加 `bus="true"`：

```xml
<!-- 一个 32 位宽的 2:1 总线 mux：32 个位共享同一个选择位 -->
<mux name="a2a"
     input="mult_32x32_slice.A_cfg mult_32x32_slice.B_cfg"
     output="mult_32x32.a" bus="true">
</mux>
```

语义上，这是一个 **W 位宽（这里 W=32）的 N:1（这里 N=2）选择器**，驱动一条位宽为 W 的输出总线，从 N 条输入总线里选一条。在 VPR 的 pb graph 里，它被「降级（lower）」成 W 个独立的单位宽 mux：第 i 位输出由第 i 位的一个 2:1 mux 产生，从两条输入总线的第 i 位中二选一。

关键约束是：**对一条「真正的」总线 mux，W 个位必须选择同一条输入总线**——也就是说这 W 个单位宽 mux 必须由同一个选择位控制。于是 OpenFPGA 在 fabric 里这样建模：

- 实例化 **W 个 mux 模块**（每个负责一位，与普通按位 mux 一样）；
- 但只实例化 **1 个共享存储器**，其选择位输出**扇出**到 W 个 mux 的 SRAM 端口。

收益是配置位从 W 份降到 1 份。例如上面这个 32 位 2:1 总线 mux，普通做法要 32 个选择位，总线型只要 1 个。

#### 4.5.2 核心流程

总线型 mux 的处理嵌在普通互连分派函数 `add_module_pb_graph_pin_interc` 的最前面：

```
add_module_pb_graph_pin_interc(des_pb_graph_pin, ...)
├─ 取 cur_interc、fan_in、interc_type、interc_circuit_model
└─ 若 cur_interc->bus && interc_type == MUX_INTERC：     # 命中总线型 mux
    ├─ bus_pins = pb_graph_interc_sink_pins(des_pin, cur_interc, physical_mode)
    │       # 收集这条 interconnect 驱动的、同端口的全部 sink 引脚（按下标升序）
    ├─ if des_pin != bus_pins.front(): return            # 只有最低位代表真正构建，其余位跳过
    └─ add_module_pb_bus_mux_interc(bus_pins, ...)
         ├─ add_module_pb_mux_memory(rep_pin=bus_pins.front(), ...)   # 建唯一共享存储器
         ├─ for each pin in bus_pins:                                 # 建每位一个 mux
         │     add_module_pb_graph_pin_mux_instance(pin, ...)         # 连好数据输入/输出
         │     断言该位 fan_in == 总线 fan_in（各位 fan-in 必须相同）
         └─ add_module_nets_between_logics_and_shared_memory_sram_bus(...)
                 # 把共享存储器的 SRAM 输出扇出到 W 个 mux
```

注意两点：① `bus` 属性**只对 `<mux>`（即 `MUX_INTERC`）有效**，crossbar 与 direct 互连不会进这条分支；② 整条 bus 只从**最低位（`bus_pins.front()`）**构建一次，其余位直接 `return`，避免重复实例化。

#### 4.5.3 源码精读

**判定与收集**：分派块在 [`add_module_pb_graph_pin_interc`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_grid_modules.cpp#L788-L806)：

```cpp
if ((true == cur_interc->bus) && (MUX_INTERC == interc_type)) {
  std::vector<t_pb_graph_pin*> bus_pins =
    pb_graph_interc_sink_pins(des_pb_graph_pin, cur_interc, physical_mode);
  VTR_ASSERT(false == bus_pins.empty());
  if (des_pb_graph_pin != bus_pins.front()) {
    return;                       // 非代表位：交由代表位统一构建
  }
  add_module_pb_bus_mux_interc(/* ..., bus_pins, cur_interc, fan_in, ... */);
  return;
}
```

这里用到的两个 `pb_graph_utils` 辅助函数（[头文件 L25-L30](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/utils/pb_graph_utils.h#L25-L30)）：

- [`is_pb_graph_pin_bus_interc`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/utils/pb_graph_utils.cpp#L80-L87)：判定驱动某 pin 的互连是否为总线型 mux——内部就是 `pb_graph_pin_interc(pin, mode)->bus`。这是个对外暴露的语义化判定谓词（fabric 路径直接读 `cur_interc->bus`，bitstream 等其它调用方可用它）。
- [`pb_graph_interc_sink_pins`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/utils/pb_graph_utils.cpp#L97-L127)：从某个 sink pin 出发，收集**同一端口**里所有由指定 interconnect 驱动的 sink 引脚，并按端口内 pin 下标升序返回。注释明确：第一个元素是最低位，因此被选作总线型 mux 共享资源的「代表 pin」（命名存储器实例时用）。

**构建主体** [`add_module_pb_bus_mux_interc`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_grid_modules.cpp#L682-L727) 清晰体现「1 个共享存储器 + W 个 mux」：

```cpp
t_pb_graph_pin* rep_pb_graph_pin = bus_pins.front();          // 代表位

/* 1) 建唯一共享存储器，以代表位命名 */
auto [mux_mem_module, mux_mem_instance] = add_module_pb_mux_memory(
  ..., interc_circuit_model, rep_pb_graph_pin, fan_in, group_config_block, verbose);

/* 2) 为每位各建一个 mux，收集其实例 id */
std::vector<size_t> mux_instances;
for (t_pb_graph_pin* des_pb_graph_pin : bus_pins) {
  VTR_ASSERT(fan_in == pb_graph_pin_inputs(des_pb_graph_pin, cur_interc).size()); // 各位 fan-in 相同
  mux_instances.push_back(add_module_pb_graph_pin_mux_instance(...));
}

/* 3) 把共享存储器的 SRAM 输出扇出到每个 mux */
add_module_nets_between_logics_and_shared_memory_sram_bus(
  module_manager, pb_module, mux_module, mux_instances,
  mux_mem_module, mux_mem_instance, circuit_lib, interc_circuit_model);
```

它复用了从普通 mux 路径抽取出来的两个共享辅助函数（这也是本次更新对普通 mux 路径的重构）：

- [`add_module_pb_graph_pin_mux_instance`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_grid_modules.cpp#L546-L607)：用 `generate_mux_subckt_name` + `find_module` 拿到 mux 模块（必须已在 `build_mux_modules` 建好），`add_child_module` 实例化，再用 `pb_graph_pin_inputs` 把数据输入逐位连上、把唯一输出连到 `des_pb_graph_pin`，返回实例 id。
- [`add_module_pb_mux_memory`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_grid_modules.cpp#L609-L680)：建存储器子模块（`group_config_block` 打开时改用 feedthrough 占位模块），把它注册为 configurable child（分组时记 `LOGICAL` 并建立 logical→physical 映射，否则记 `UNIFIED`）。

**共享连网** [`add_module_nets_between_logics_and_shared_memory_sram_bus`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/utils/module_manager_utils.cpp#L1054-L1080)（[声明 L112](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/utils/module_manager_utils.h#L112-L117)）是把「一个存储器扇出到多个逻辑实例」的工具。它分别处理非反相（`data_out`）与反相（`inverted data_out`）两类 SRAM 端口，把共享存储器的输出连到 `logic_instance_ids` 里**每一个** mux 实例的 SRAM 端口。对比 4.3 里普通 mux 用的单实例版 [`add_module_nets_between_logic_and_memory_sram_bus`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/utils/module_manager_utils.h#L106-L110)（注意名字里 `logic` 是单数、`logics` 是复数），二者的差别就是「1 对 1」与「1 对 W」。

#### 4.5.4 代码实践

**目标**：亲眼确认总线型 mux 在 fabric 里被建成了「1 个共享存储器 + W 个 mux」，并理解它如何被 `pb_graph_utils` 判定与收集。

**步骤**：

1. 打开回归任务用到的 VPR 架构 [`k4_frac_N4_tileable_adder_chain_mem1K_frac_dsp32_busmux_40nm.xml`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/vpr_arch/k4_frac_N4_tileable_adder_chain_mem1K_frac_dsp32_busmux_40nm.xml#L874-L879)（L874-L879），确认 `a2a` 互连的 `bus="true"`、输出到 `mult_32x32.a`（位宽 32）、两条输入总线 `A_cfg`/`B_cfg`。
2. 在 [`add_module_pb_bus_mux_interc`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_grid_modules.cpp#L682-L727) 里数清楚：`add_module_pb_mux_memory` 调用 **1 次**（共享存储器），`add_module_pb_graph_pin_mux_instance` 在循环里调用 **W 次**（这里 W=32），最后 `add_module_nets_between_logics_and_shared_memory_sram_bus` 调用 1 次。
3. 对照普通 mux 路径（同一函数 switch 里 `MUX_INTERC` 分支，L870 附近）：它用单实例版 `add_module_nets_between_logic_and_memory_sram_bus`，每个 mux 配自己的存储器。理解「按位独立 mux」与「总线型 mux」在存储器数量上的差别。

**预期现象**：对 `a2a` 这个 32 位 2:1 总线 mux，普通（非 bus）建模会产出 32 份 2:1 mux + 32 个存储器位；总线型建模产出 32 个 mux 但只有 **1 个共享存储器（1 位）**。

**待本地验证**：`source openfpga.sh` 后运行 `run-task basic_tests/k4_series/k4n4_frac_mult_busmux`（回归脚本 [`basic_reg_test.sh:196`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/regression_test_scripts/basic_reg_test.sh#L196) 亦含此任务；其 `task.conf` 的 `arch0` 指向上面的 busmux VPR arch）。由于 `build_grid_modules` 会为架构里**所有** physical tile 类型（含乘法器 mult_32x32）都构建模块，即便基准是 `and2`（不实例化乘法器），`a2a` 的总线型 mux 仍会被建出来。用 `--verbose` 观察是否打印形如 `Built bus-based mux 'a2a' with 32 shared bit-muxes driven by a single selector` 的日志（来自 [`build_grid_modules.cpp:716-719`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_grid_modules.cpp#L716-L719)）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `add_module_pb_graph_pin_interc` 只让 `bus_pins.front()` 真正构建，其余位 `return`？如果不做这个去重，会发生什么？

> **答案**：因为总线型 mux 的资源（共享存储器、各 mux 实例）需要一次性按整条 bus 建好。`add_module_pb_graph_pin_interc` 是「逐 sink pin」被调用的，若每位都触发一次 `add_module_pb_bus_mux_interc`，就会把同一组共享存储器和 W 个 mux 重复建 W 次，产生 W 倍冗余实例与 W 份配置位，完全违背「共享」初衷。所以用最低位做代表建一次，其余位跳过。

**练习 2**：一个 32 位 2:1 的总线型 mux，相比 32 个独立的 2:1 mux，能省下多少配置位？为什么要求「整条 bus 的所有位选择同一输入」？

> **答案**：32 个独立 2:1 mux 各需 1 个选择位，共 32 位；总线型只建 1 个共享存储器，故只需 **1 位**，省 31 位。要求「同选一输入」是因为共享了同一个选择位——若允许各位选不同输入，单一位选择信号无法表达 32 个独立选择，就退化回 32 个独立 mux，总线型 mux 的语义不成立（比特流生成阶段 u7-l2 也会对此做断言校验）。

---

## 5. 综合实践

本实践对应规格里的核心任务：**对比 `build_grid_modules.cpp` 与 `build_routing_modules.cpp`，说明它们如何利用 `circuit_library` 与 `device_rr_gsb` 来决定要实例化哪些子模块**。

### 实践目标

把四个最小模块串起来，形成一张「数据来源 → 决策 → 实例化」的对照表，彻底搞清 grid 与 routing 在数据依赖上的本质差异。

### 操作步骤

1. **填表**：按下表逐项在源码中找到证据行号。

   | 维度 | grid (`build_grid_modules.cpp`) | routing (`build_routing_modules.cpp`) |
   | --- | --- | --- |
   | 遍历对象 | `device_ctx.logical_block_types` / `physical_tile_types` | `device_rr_gsb`（unique 或全坐标） |
   | 是否用 `device_rr_gsb` | 否（签名无此参数） | 是（主遍历对象） |
   | 递归方式 | 后序 DFS（`rec_build_logical_tile_modules`） | 双重 for 循环遍历坐标/unique |
   | 取电路模型的入口 | `device_annotation.pb_type_circuit_model`（L298）/ `interconnect_circuit_model`（L767） | `device_annotation.rr_switch_circuit_model`（L122） |
   | 找子模块的方式 | `find_module(generate_physical_block_module_name(...))` | `find_module(generate_mux_subckt_name(...))` |
   | 挂物理存储器 | `add_physical_memory_module`（L1373） | `add_physical_memory_module`（L905 / L1203） |

2. **画数据流图**：分别画出 grid 与 routing 的「输入数据 → 决策点 → 实例化的子模块」。重点标注 `device_annotation` 这张 link 阶段建立的「账本」在两边的不同入口（`pb_type_circuit_model` vs `rr_switch_circuit_model`）。

3. **跟踪总线型 mux**（对应规格里的第二个任务）：打开 [`add_module_pb_graph_pin_interc`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_grid_modules.cpp#L788-L806) 的 bus 分派块，回答：
   - 它用什么条件判定「这是一条总线型 mux 互连」？（提示：`cur_interc->bus` 且 `MUX_INTERC`。）
   - `pb_graph_interc_sink_pins` 收集到的是什么？（提示：同一条 interconnect 驱动的、同一端口的所有 sink 引脚，按下标升序。）
   - 为什么只有 `bus_pins.front()`（最低位）才真正构建，其余位直接 `return`？（提示：避免重复；共享资源用最低位命名。）
   - 跟到 [`add_module_pb_bus_mux_interc`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_grid_modules.cpp#L682-L727)，确认它建了 **1 个共享存储器** + **W 个 mux**，再用 `add_module_nets_between_logics_and_shared_memory_sram_bus` 把存储器输出扇出到每个 mux 的 SRAM。

4. **回答三个问题**（写在你的学习笔记里）：
   - 为什么 grid 不需要 `device_rr_gsb`？（提示：grid 描述逻辑块内部结构，由 pb graph 决定；routing 描述布线，才需要 rr graph 的 GSB。）
   - 为什么 routing 的 mux 实例化能直接 `find_module` 成功？（提示：`build_mux_modules` 已在更早的步骤跑完。）
   - 如果把 `build_mux_modules` 从总编排器里删掉，grid 和 routing 各会在哪一行断言失败？（提示：grid 的 interconnect mux、routing 的 `build_switch_block_mux_module` L132。）

### 预期结果

你应该得出结论：**grid 与 routing 的「实例化哪些子模块」都分两层决策**——结构来源（有哪些位置/块）来自 VPR device（grid 用 pb graph，routing 用 `device_rr_gsb`），电路实现（用哪种 mux/模型）来自 `device_annotation` 这张 link 阶段的账本；两者最后都靠「按名字反查已建好的 mux/memory 子模块」完成实例化，这正是 u6-l2 自下而上顺序的运行时体现。

### 待本地验证

如果你已编出 `openfpga` 二进制（u1-l3）并跑通过任务（u1-l4），可选做：在 `example_script.openfpga` 之后追加 `write_fabric_verilog`，打开产物 `SRC/sub_module/*.v`，对照本讲确认以下命名规律：`grid_clb*` / `grid_io_*`（grid）、`sb_*` / `cb_*`（routing）、`mux*_size*` / `*_branch*`（mux）、`lut*`（lut）、`decoder*` / `*_frame_decoder*`（decoder）、`*_config_chain*` / `*_frame*`（memory）。

---

## 6. 本讲小结

- 六类子模块构建器在 [`build_device_module_graph`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_device_module.cpp#L36-L194) 中按「原子→复合→顶层」的固定顺序调用，越靠后模块越大；mux/lut/decoder/memory 必须先于 grid/routing。
- **grid** 用后序 DFS 遍历 pb graph，电路模型来自 `device_annotation.pb_type_circuit_model` / `interconnect_circuit_model`，**不依赖** `device_rr_gsb`。
- **routing** 以 `device_rr_gsb` 为遍历对象，提供 unique（压缩，`get_num_*_unique_module`）与 flatten（全坐标）两套入口，mux 模型来自 `device_annotation.rr_switch_circuit_model`。
- **memory** 由 [`build_memory_module`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_memory_modules.cpp#L944-L963) 按 `configuration_protocol` 分派成 flatten / chain / frame 三种组织；`add_physical_memory_module` 再把它们聚合成物理存储器组挂到 grid/routing/top。
- **mux / lut / decoder** 三者只由电路模型驱动（遍历 `mux_lib` 或 `circuit_lib`），是 grid/routing 的「弹药库」，普遍采用「先去重统计、再逐个生成」的两段式。
- **总线型 mux 互连**（grid 的扩展）在 [`add_module_pb_graph_pin_interc`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_grid_modules.cpp#L788-L806) 中被 `cur_interc->bus` 识别，由最低位代表整条 bus 触发 [`add_module_pb_bus_mux_interc`](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fabric/build_grid_modules.cpp#L682-L727)：建 1 个共享存储器 + W 个 mux，用 `pb_graph_utils` 收集 sink 引脚、用 `add_module_nets_between_logics_and_shared_memory_sram_bus` 扇出共享选择位。
- 贯穿所有构建器的统一范式是「**按名字反查已存在模块**」（`generate_*_name` + `find_module` + `add_child_module`），这就是构建顺序等于数据依赖的运行机制。

---

## 7. 下一步学习建议

- **向上一层**：进入 u6-l4（顶层模块与存储器配置总线），看 `build_top_module` 如何把本讲产出的 grid/routing 子模块实例化进 `fpga_top`，并用 `add_top_module_nets_memory_config_bus` 把所有物理存储器接到配置总线上。
- **横向深入**：本讲的 `add_physical_memory_module` 产出的物理存储器组，正是 u7（比特流生成）的寻址对象。建议接着读 u7-l1（两级比特流模型），理解 logical→physical 的对应关系。
- **源码延伸**：若对 unique 压缩感兴趣，可先读 u9-l5（GSB 压缩与 Unique Blocks），再回看本讲 `build_unique_routing_modules`，理解 `device_rr_gsb` 的 unique 列表是如何在 link 阶段建立的。
- **支撑库**：mux/decoder 的拓扑抽象在 `mux_lib/`（MuxGraph、DecoderLibrary），u10-l2 会系统讲解；可对照本讲 `build_mux_branch_graphs`、`add_mux_local_decoder_to_library` 理解其去重逻辑。
