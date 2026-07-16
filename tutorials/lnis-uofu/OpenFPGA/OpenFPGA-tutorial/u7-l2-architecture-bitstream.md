# build_architecture_bitstream：grid/routing/mux 位生成

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `build_architecture_bitstream` 这条命令背后「device 级比特流」的生成入口、遍历对象（grid 与 routing 两大块），以及它与上一讲两级模型里 `BitstreamManager` 的关系。
- 解释 grid 比特流如何从 LUT 真值表、primitive 的 mode_bits、以及 pb_graph 内部互连 mux 解码出配置位。
- 解释 routing 比特流如何用布局布线结果（前驱节点 + 驱动节点 net 匹配）决定每个布线 mux 的选择位。
- 理解 `build_mux_bitstream` 这个被 grid 与 routing 共用的「mux 解码引擎」如何把一个 path id 翻译成一串存储位。
- **（本次更新重点）** 理解总线型 mux（`<mux bus="true"/>`）比特流：当一条输出 bus 共享同一个选择器时，为什么要求 bus 的所有位选择同一个输入端口，以及 OpenFPGA 如何用「代表位」只发一个共享存储块、把 W 份配置位压缩成 1 份。

> 本讲承接 u7-l1（两级比特流模型）与 u4-l3（pb_type 注解）。我们只讲 **device 级** 比特流的「位值解码」；device→fabric 的重组、文件格式与 `fast_configuration` 留给 u7-l3 与 u7-l4。

## 2. 前置知识

在进入源码前，先用三句话把关键背景钉死（这些词在 u7-l1、u4-l3 已建立）：

- **device 级比特流（`BitstreamManager`）**：与配置协议、与 fabric 模块层级强绑定，但**与寻址/协议无关**。它的配置块树镜像 `ModuleManager` 的展平实例层级，0/1 位只挂在叶子块上。本讲解码的就是这些 0/1 位从哪来。
- **物理 pb（`PhysicalPb`）**：repack 之后，网表被打包到「物理 pb」上。`PhysicalPb` 记录了每个 pb_graph_pin 当前承载的 atom net、每个 LUT 的真值表、mode_bits 等。它是 grid 比特流解码的**数据来源**。
- **布局布线结果（routing annotation）**：`VprRoutingAnnotation` 记录每个 rr_node 上当前走的 net（`rr_node_net`）以及它的前驱节点（`rr_node_prev_node`）。它是 routing 比特流解码的**数据来源**。
- **path id**：一个 N:1 mux 的「被选中的输入编号」。device 比特流生成的核心，就是把每个 mux 的 path id 解出来，再交给 `build_mux_bitstream` 翻译成存储位。`DEFAULT_PATH_ID = -1` 表示「未映射/使用默认路径」，定义在 [openfpga/src/fpga_bitstream/mux_bitstream_constants.h:8](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/mux_bitstream_constants.h#L8)。

一句话总览：device 比特流生成的本质，是**两套遍历 + 一种解码**——遍历 grid（沿 pb_graph 走到 LUT/primitive/内部互连 mux）、遍历 routing（沿 GSB 走到 SB/CB 里的布线 mux），每遇到一个 mux 就问一句「当前选的是第几个输入？」，再把答案编码成存储位。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [openfpga/src/base/openfpga_bitstream_template.h](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/base/openfpga_bitstream_template.h) | 命令模板层。`build_architecture_bitstream` 命令绑定到 `fpga_bitstream_template`，负责解析选项、调用内核、写出 `fabric_independent_bitstream.xml`。 |
| [openfpga/src/fpga_bitstream/build_device_bitstream.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_device_bitstream.cpp) | device 比特流的**总入口** `build_device_bitstream`：建顶层块、预估容量、依次调用 grid 与 routing 两路解码。 |
| [openfpga/src/fpga_bitstream/build_grid_bitstream.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_grid_bitstream.cpp) | grid 比特流：递归走 pb_graph，解码 LUT 真值表、primitive mode_bits、pb_graph 内部互连 mux。**本次更新的总线型 mux 逻辑就加在这里。** |
| [openfpga/src/fpga_bitstream/build_routing_bitstream.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_routing_bitstream.cpp) | routing 比特流：遍历每个 GSB，解码 Switch Block 与 Connection Block 里的布线 mux 选择位。 |
| [openfpga/src/fpga_bitstream/build_mux_bitstream.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_mux_bitstream.cpp) | **共用的 mux 解码引擎** `build_mux_bitstream`：把 path id 翻译成一串存储位（含 local encoder 编码）。grid 与 routing 都调它。 |
| [openfpga/src/utils/pb_graph_utils.h](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/utils/pb_graph_utils.h) / [.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/utils/pb_graph_utils.cpp) | pb_graph 辅助函数。**本次新增** `is_pb_graph_pin_bus_interc` 与 `pb_graph_interc_sink_pins`，专门服务于总线型 mux 的判定与 sink pin 收集。 |
| [openfpga_flow/vpr_arch/k4_frac_N4_tileable_adder_chain_mem1K_frac_dsp32_busmux_40nm.xml](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/vpr_arch/k4_frac_N4_tileable_adder_chain_mem1K_frac_dsp32_busmux_40nm.xml) | 回归用 VPR 架构。内含一个 `<mux bus="true"/>`（32 位宽 2:1 总线 mux `a2a`），是理解总线型 mux 的真实样例。 |

## 4. 核心概念与源码讲解

### 4.1 build_device_bitstream：device 级比特流的总入口

#### 4.1.1 概念说明

`build_architecture_bitstream` 命令在 context 里写的是 **device 级** `BitstreamManager`（见 u7-l1）。它的任务很纯粹：拿已经布完局布完线、且 repack 完的器件，把每一个可配置存储位（LUT 位、mode 位、mux 选择位）的 0/1 值解出来，挂到与模块层级同构的配置块树上。这一步**完全不关心配置协议**（scan_chain / memory_bank / frame_based 都不影响位的值），协议只影响后面 device→fabric 的重组。

#### 4.1.2 核心流程

`build_device_bitstream` 的骨架可以概括为「一建、两预估、两解码、一断言」：

1. **建顶层块**：以 `fpga_top`（或 `fpga_core`，若存在）为根，创建顶层 `ConfigBlock`。
2. **预估容量**：递归数一遍可配置子模块个数（`rec_estimate_device_bitstream_num_blocks`）与叶子位数（`rec_estimate_device_bitstream_num_bits`），预先 `reserve`，避免频繁扩容。
3. **grid 解码**：调用 `build_grid_bitstream`，遍历所有 grid（CLB / IO / 异构块），沿 pb_graph 递归解码。
4. **routing 解码**：调用 `build_routing_bitstream`，遍历所有 GSB，解码 SB/CB 的布线 mux。
5. **断言对账**：实际生成的块数、位数必须与预估值完全相等，否则说明遍历漏了或多了一处。

注意 `in_edges` 这个对象：它是 RR graph 入边的惰性索引，在 grid 解码前 `init`，再传给 routing 解码用——这是 RR graph 大对象按需构造、避免重复建索引的常见手法。

#### 4.1.3 源码精读

总入口与两路解码的衔接在 [openfpga/src/fpga_bitstream/build_device_bitstream.cpp:150-153](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_device_bitstream.cpp#L150-L153)：

```cpp
BitstreamManager build_device_bitstream(const VprContext& vpr_ctx,
                                        const OpenfpgaContext& openfpga_ctx,
                                        const std::string& unused_mux_config,
                                        const bool& verbose) {
```

注意第三个参数 `unused_mux_config`——它决定「未使用的 mux 选哪条默认路径」（见 4.4），从命令选项一路透传到每个 mux 的解码。

容量预估递归数块、数位的逻辑见 [build_device_bitstream.cpp:30-59](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_device_bitstream.cpp#L30-L59) 与 [build_device_bitstream.cpp:67-135](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_device_bitstream.cpp#L67-L135)：叶子（无可配置子模块）记 1 位，否则沿 `PHYSICAL` 可配置子模块下钻；顶层还要按配置区域遍历，并按协议跳过译码器占位（`estimate_num_configurable_children_to_skip_by_config_protocol`）。

随后是 grid 与 routing 两路解码的调用，这是本讲的「分叉点」：[build_device_bitstream.cpp:212-238](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_device_bitstream.cpp#L212-L238)：

```cpp
build_grid_bitstream(bitstream_manager, top_block, /* … */ vpr_ctx.atom(),
    openfpga_ctx.vpr_device_annotation(), openfpga_ctx.vpr_clustering_annotation(),
    openfpga_ctx.vpr_placement_annotation(),
    openfpga_ctx.vpr_bitstream_annotation(), unused_mux_config, verbose);
/* … */
build_routing_bitstream(bitstream_manager, top_block, /* … */
    vpr_ctx.atom(), openfpga_ctx.vpr_device_annotation(),
    openfpga_ctx.vpr_routing_annotation(),
    vpr_ctx.device().rr_graph, in_edges, openfpga_ctx.device_rr_gsb(),
    openfpga_ctx.flow_manager().compress_routing(), unused_mux_config, verbose);
```

可以看到：grid 这一路吃的是 `atom` / `clustering` / `placement` / `bitstream` 标注（即 PhysicalPb 那一套），routing 这一路吃的是 `routing_annotation` / `rr_graph` / `device_rr_gsb`（即布线结果那一套）。两路数据来源不同，但最终都把位挂进同一棵配置块树。

命令模板层把这一切包起来：[openfpga/src/base/openfpga_bitstream_template.h:36-65](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/base/openfpga_bitstream_template.h#L36-L65) 里 `fpga_bitstream_template` 校验 `unused_mux_config` 取值（`auto/first/last/unused_input`），调用 `build_device_bitstream`，再用 `overwrite_bitstream` 叠加 `bitstream_setting` 的强制位，最后 `--write_file` 时由 `write_xml_architecture_bitstream` 落盘成 `fabric_independent_bitstream.xml`（[openfpga_bitstream_template.h:71-83](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/base/openfpga_bitstream_template.h#L71-L83)）。文件名里的 `fabric_independent` 正点题：device 级、与 fabric 协议无关。

#### 4.1.4 代码实践

1. **目标**：确认 device 比特流的「两路解码 + 协议无关」性质。
2. **步骤**：
   - 在 [example_script.openfpga:37](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/openfpga_shell_scripts/example_script.openfpga#L37) 看到 `build_architecture_bitstream --verbose --write_file fabric_independent_bitstream.xml`。
   - 运行任意一个 full_testbench 任务（如 `run-task basic_tests/full_testbench/configuration_chain`），在结果目录打开 `fabric_independent_bitstream.xml`。
   - 把同一个 benchmark 换用 `bank` 协议（换 `openfpga_arch_file`）再跑一次，对比两份 `fabric_independent_bitstream.xml`。
3. **观察**：顶层 `<bitstream_block>` 的树形结构与每个叶子 `<bit>` 的值，在两种协议下应当**完全一致**（位值与协议无关）。
4. **预期结果**：两份 XML 的位值相同；差异只出现在后面 `build_fabric_bitstream`/`write_fabric_bitstream` 产生的、带地址的 fabric 比特流里。
5. 运行结果：待本地验证（取决于本地是否完成 `make compile` 与子模块 checkout）。

#### 4.1.5 小练习与答案

- **练习 1**：为什么顶层预估位数时要 `estimate_num_configurable_children_to_skip_by_config_protocol` 跳过译码器，而 grid/routing 内部模块递归时不需要？
  - **答案**：译码器只出现在**顶层按配置区域组织**的地方（frame/memory_bank 协议在每个 region 前挂了译码器子模块），它们本身不是叶子存储位、不应计入位数；而非顶层模块的可配置子模块都是真实存储器，无需跳过。
- **练习 2**：`build_device_bitstream` 的返回值类型是 `BitstreamManager`（按值返回），这个大开销对象为什么不担心拷贝？
  - **答案**：它由命令模板直接 `mutable_bitstream_manager() = build_device_bitstream(...)` 移动赋值给 context（[openfpga_bitstream_template.h:62-64](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/base/openfpga_bitstream_template.h#L62-L64)），返回值优化（RVO）/移动语义下不会产生深拷贝。

### 4.2 grid 比特流：LUT 真值表、mode_bits 与 pb_graph 内部互连

#### 4.2.1 概念说明

grid 比特流处理的是「逻辑块内部」的配置位，分三类来源：

1. **LUT**：配置位来自真值表（truth table）。一个 K 输入 LUT 有 \(2^K\) 个配置位，每个位决定某一组输入组合下输出是 0 还是 1。
2. **primitive（IO / FF / hardlogic）**：配置位来自 mode_bits（决定工作模式，如 IO 的输入/输出方向，见 u4-l3）。
3. **pb_graph 内部互连 mux**：逻辑块内部把子 pb 的引脚连起来用的 mux，配置位来自「当前选了哪个输入」。

这三类对应三个静态函数：`build_lut_bitstream`、`build_primitive_bitstream`、`build_physical_block_pin_interc_bitstream`，由递归骨架 `rec_build_physical_block_bitstream` 按到达的叶子类型分派。

#### 4.2.2 核心流程

递归骨架 [build_grid_bitstream.cpp:859](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_grid_bitstream.cpp#L859) 的执行过程：

```text
rec_build_physical_block_bitstream(node):
  取 node 的 physical_pb_type 与 physical_mode
  若该 pb 模块无 LOGICAL 可配置子模块 → 直接返回（剪枝）
  若有 PHYSICAL 可配置子模块 → 建一块、挂到父块下
  若不是 primitive：
      按 physical_mode 的 children 顺序递归每个子 pb_graph_node
  若是 primitive：
      LUT        → build_lut_bitstream       （真值表 → 位）
      FF/IO/逻辑 → build_primitive_bitstream  （mode_bits → 位）
      返回
  否则（非叶子、非 primitive 的中间 pb）：
      build_physical_block_interc_bitstream   （该 pb 内部互连 mux → 位）
```

关键约束：**遍历顺序必须与模块构建时 `rec_build_physical_block_modules` 完全一致**，否则配置块树与模块实例树对不上、device→fabric 转换时会错位（见 u7-l1 的「块名==实例名」对接钥匙）。

#### 4.2.3 源码精读

**LUT 解码**的核心是 `build_frac_lut_bitstream`，它把真值表灌进 mux_graph 反查出每位取值，见 [build_grid_bitstream.cpp:726-735](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_grid_bitstream.cpp#L726-L735)：

```cpp
MuxId lut_mux_id = mux_lib.mux_graph(lut_model, (size_t)pow(2., lut_size));
const MuxGraph& mux_graph = mux_lib.mux_graph(lut_mux_id);
VTR_ASSERT(mux_graph.num_memory_bits() == lut_size);
VTR_ASSERT(mux_graph.num_inputs() == (size_t)pow(2., lut_size));
lut_bitstream = build_frac_lut_bitstream(
  circuit_lib, mux_graph, device_annotation,
  physical_pb.truth_tables(lut_pb_id),
  circuit_lib.port_default_value(lut_regular_sram_ports[0]));
```

这里的直觉：一个 K-LUT 在电路实现上就是一个 \(2^K:1\) 的 mux（输入是 \(2^K\) 根常量线，由真值表决定）。所以「LUT 配置位」其实就是「这个 \(2^K:1\) mux 的存储位」。当 LUT 未被使用（`lut_pb_id` 非法）时，则填满默认值（[build_grid_bitstream.cpp:709-721](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_grid_bitstream.cpp#L709-L721)）。

**primitive mode_bits 解码**：`generate_mode_select_bitstream` 把 `0/1/x` 串解析成 bool 串，`x` 位用 physical mode 的 base mode_bits 兜底，见 [build_grid_bitstream.cpp:39-81](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_grid_bitstream.cpp#L39-L81)。这正是 u4-l3 讲的「physical 必须完全确定、x 只能出现在 operating」在比特流侧的落点。

**内部互连 mux 解码**：`build_physical_block_pin_interc_bitstream`（[build_grid_bitstream.cpp:298](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_grid_bitstream.cpp#L298)）先按 interconnect 物理类型分派：`DIRECT_INTERC` 直接连线无需位；`COMPLETE_INTERC`/`MUX_INTERC` 才需要解码 mux，见 [build_grid_bitstream.cpp:321-338](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_grid_bitstream.cpp#L321-L338)。这部分（path id 怎么求、怎么交给 `build_mux_bitstream`）会在 4.3、4.5 详细展开。

#### 4.2.4 代码实践

1. **目标**：在 `fabric_independent_bitstream.xml` 里定位一个 LUT 的配置块，验证它由真值表解码而来。
2. **步骤**：跑 `run-task basic_tests/full_testbench/configuration_chain`（benchmark 是 `and2`，即 `out = a & b`）。打开 `fabric_independent_bitstream.xml`，找到一个名字含 `lut` 的叶子 `<bitstream_block>`（其路径形如 `fpga_top/grid_clb_.../.../mem_right_...`）。
3. **观察**：该块下应有 16 个 `<bit>`（4 输入 LUT → \(2^4=16\) 位）。
4. **预期结果**：`and2` 的 LUT 真值表只有 `a=1,b=1` 时输出 1，其余 15 种组合输出 0；因此 16 位里恰好有 1 位为 1（对应输入组合 `11` 的那一行）、15 位为 0（默认值若为 0）。具体哪一位为 1 取决于 LUT 输入排列顺序。
5. 运行结果：待本地验证（位的位置随 mux_graph 内部编号而定，但「恰好 1 位为 1」应成立）。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 LUT 的 `mux_graph.num_memory_bits()` 恰好等于 `lut_size`（K），而不是 \(2^K\)？
  - **答案**：LUT 用的是「树形 mux + 编码地址」实现：\(2^K\) 个输入只需要 K 个存储位来编码选中的那一行（\(K=\log_2(2^K)\)）。真值表的 \(2^K\) 个输出值是通过 K 个地址位去「查表」得到的，而不是逐位存放。
- **练习 2**：`build_primitive_bitstream` 里若 `primitive_mode_select_ports.size() == 0` 会怎样？
  - **答案**：直接 `return`，不产生任何位（[build_grid_bitstream.cpp:118-120](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_grid_bitstream.cpp#L118-L120)）。说明该 primitive 没有模式选择端口，自然没有 mode 位可生成。

### 4.3 routing 比特流：SB/CB 布线 mux 的选择位

#### 4.3.1 概念说明

routing 比特流处理的是「全局布线架构」里的 mux——即 Switch Block（SB）和 Connection Block（CB）中把多条布线通道汇聚到一个输出的那些 mux。与 grid 内部互连 mux 不同，这里的 mux 不在 pb_graph 里，而在 RR graph（rr_node/rr_edge）里。

routing 比特流的核心问题同样是「当前选了第几个输入」，但答案的来源不同：grid 侧问 `PhysicalPb`，routing 侧问 **`VprRoutingAnnotation`**——它记录了每个 rr_node 上当前走的 net，以及驱动它的前驱 rr_node。

#### 4.3.2 核心流程

`build_routing_bitstream`（[build_routing_bitstream.cpp:707](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_routing_bitstream.cpp#L707)）按 GSB 坐标两重循环：

```text
for 每个 GSB (ix, iy):
    build_switch_block_bitstream(...):     # SB
        for 每条 side、每条 track:
            若是输出方向且 fan-in>1 → 是个布线 mux
            build_switch_block_mux_bitstream(...)   # 解选择位
    （若 group_routing，CB 与 SB 同模块，跳过单独 CB）
for CHANX、CHANY 两类：
    build_connection_block_bitstreams(...):  # CB
        for 每个 IPIN：
            若驱动边数>1 → 是个布线 mux
            build_connection_block_mux_bitstream(...)  # 解选择位
```

判断「是不是 mux」的规则很朴素：**fan-in > 1 即为 mux**（[build_routing_bitstream.cpp:233](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_routing_bitstream.cpp#L233) 与 [build_routing_bitstream.cpp:433](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_routing_bitstream.cpp#L433)）。

#### 4.3.3 源码精读

SB mux 的 path id 求解——这是 routing 比特流最关键的一段——见 [build_routing_bitstream.cpp:87-105](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_routing_bitstream.cpp#L87-L105)：

```cpp
int path_id = DEFAULT_PATH_ID;
AtomNetId output_atom_net = atom_ctx.lookup().atom_net(output_net);
if (true == atom_ctx.netlist().valid_net_id(output_atom_net)) {
  VTR_ASSERT(routing_annotation.rr_node_prev_node(cur_rr_node));
  for (size_t inode = 0; inode < drive_rr_nodes.size(); ++inode) {
    if ((input_nets[inode] == output_net) &&
        (drive_rr_nodes[inode] ==
         routing_annotation.rr_node_prev_node(cur_rr_node))) {
      path_id = (int)inode;
      break;
    }
  }
}
```

直觉解读：当前输出节点 `cur_rr_node` 走的是 `output_net`；它的前驱节点 `rr_node_prev_node` 告诉我们「信号实际是从哪个驱动节点来的」。在所有候选驱动节点里，找到那个「net 相同 **且** 就是前驱节点」的，它的下标 `inode` 就是 path id。两个条件缺一不可：单看 net 可能有多条同 net 路径，加上前驱节点才唯一确定。

CB mux 的 path id 求法同构，只是遍历对象从 `drive_rr_nodes` 换成了 `driver_rr_edges`（[build_routing_bitstream.cpp:295-308](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_routing_bitstream.cpp#L295-L308)）。

求出 path id 后，SB 与 CB 都调用共用的 `build_mux_bitstream(...)`（[build_routing_bitstream.cpp:139-141](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_routing_bitstream.cpp#L139-L141) 与 [build_routing_bitstream.cpp:343-345](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_routing_bitstream.cpp#L343-L345)）把 path id 翻译成存储位——这正是下一节的主角。

另外注意 `unused_mux_config == "unused_input"` 这一分支（[build_routing_bitstream.cpp:106-131](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_routing_bitstream.cpp#L106-L131)）：当输出未映射时，它会把默认路径指向「第一个也没有 net 的输入」，这样未使用的 mux 不会无谓地把某个正在用的输入「锁死」。

#### 4.3.4 代码实践

1. **目标**：在 `fabric_independent_bitstream.xml` 里定位一个 SB 布线 mux 的配置块，验证其 path id 来自前驱节点匹配。
2. **步骤**：沿用上一个 `configuration_chain`/`and2` 的运行结果。在 XML 里找到名字形如 `sb_.../mem_*` 的叶子块（SB 存储块带有 `path_id` 属性）。
3. **观察**：该块的 `path_id` 属性值，以及 `<input_net>` / `<output_net>` 标注。
4. **预期结果**：被使用的 SB mux 的 `path_id` 是一个非负整数，且 `<output_net>` 与某一条 `<input_net>` 同名（信号从该输入穿过）；未被使用的 mux 的 `path_id` 为默认值（0 或 last，取决于 `unused_mux_config`），`<output_net>` 为 `unmapped`。
5. 运行结果：待本地验证。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 path id 判定里 `(input_nets[inode] == output_net)` 还要再叠加 `(drive_rr_nodes[inode] == rr_node_prev_node)`？
  - **答案**：同一个 net 可能在布线中被扇出/复制到多条边，仅凭 net 名会匹配到多个候选 inode；叠加「前驱节点」这个唯一物理来源，才能锁定真正驱动当前输出的那一条边。
- **练习 2**：`group_routing()` 为真时，为什么 routing 比特流会「跳过单独的 CB 生成」？
  - **答案**：`group_routing` 把 CB 的布线 mux 合并进了 SB 模块（同一物理模块），若再单独遍历 CB 会重复生成同一批位。代码在 [build_routing_bitstream.cpp:836-841](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_routing_bitstream.cpp#L836-L841) 提前 return。

### 4.4 mux 比特流：从 path id 到存储位（build_mux_bitstream）

#### 4.4.1 概念说明

grid 内部互连 mux 与 routing 布线 mux，尽管数据来源不同，**最终都把「path id」交给同一个解码引擎 `build_mux_bitstream`**。它负责把「选第几个输入」翻译成「每个存储位写 0 还是 1」。这是 device 比特流里被调用最频繁的函数之一。

一个 N:1 mux 的存储位数量取决于实现：

- 若是树形 mux、直接存每级选择位，需要 \(N-1\) 位（每级 1 位，共 \(\lceil\log_2 N\rceil\) 级、每级若干 2:1）；
- 若用 **local encoder**（每级一个小译码器），则每级存的是「该级选中的输入编号」的二进制地址，位数为各级地址位数之和。

具体位数由 `MuxGraph` 的拓扑决定，`build_mux_bitstream` 只是把 path id「灌」进去反查。

#### 4.4.2 核心流程

```text
build_mux_bitstream(mux_model, mux_size, path_id, unused_mux_config):
  switch 设计工艺:
    CMOS → build_cmos_mux_bitstream(...)
    ReRAM → （暂未实现）
  返回 vector<bool>

build_cmos_mux_bitstream:
  implemented_mux_size = 考虑常量输入后的实际输入数
  if path_id == DEFAULT_PATH_ID:
      datapath_id = find_mux_default_path_id(...)   # 按 unused_mux_config 定默认
  取 mux_graph.decode_memory_bits(datapath_id, output_id) → raw_bitstream
  if 不用 local encoder：直接返回 raw_bitstream
  否则逐级编码：每级把「被选中的 mem_index」转成二进制地址位，拼成最终位串
```

#### 4.4.3 源码精读

默认路径策略 `find_mux_default_path_id` 见 [build_mux_bitstream.cpp:34-59](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_mux_bitstream.cpp#L34-L59)：

```cpp
if (unused_mux_config == "first")        default_path_id = 0;
else if (unused_mux_config == "last")    default_path_id = mux_size - 1;
else if (unused_mux_config == "auto" ||
         unused_mux_config == "unused_input") {
  if (circuit_lib.mux_add_const_input(mux_model)) default_path_id = mux_size - 1;
  else                                            default_path_id = DEFAULT_MUX_PATH_ID; // 0
}
```

核心解码——把 path id 变成 raw 位——只有一行实质调用，见 [build_mux_bitstream.cpp:103-104](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_mux_bitstream.cpp#L103-L104)：

```cpp
vtr::vector<MuxMemId, bool> raw_bitstream = mux_graph.decode_memory_bits(
  MuxInputId(datapath_id), mux_graph.output_id(mux_graph.outputs()[0]));
```

local encoder 逐级编码见 [build_mux_bitstream.cpp:112-164](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_mux_bitstream.cpp#L112-L164)：对每一级，找出该级哪一个 memory 为 1（即被选中），把它的下标转成 `find_mux_local_decoder_addr_size(...)` 位宽的二进制地址，追加到最终位串。例如某级有 4 个候选、选中第 3 个，则编码为 2 位二进制 `11`。

#### 4.4.4 代码实践

1. **目标**：直观感受 `unused_mux_config` 对未使用 mux 位的影响。
2. **步骤**：复制 `configuration_chain` 任务为自定义任务，在 `task.conf` 的 `[OpenFPGA_SHELL]` 段通过脚本参数把 `build_architecture_bitstream` 改为 `build_architecture_bitstream --verbose --unused_mux_config last --write_file ...`，另存一份用 `--unused_mux_config first`，分别跑。
3. **观察**：对比两份 `fabric_independent_bitstream.xml` 中那些 `path_id` 为默认（未使用）的 mux 存储块。
4. **预期结果**：未使用 mux 的位串会随 `first`/`last` 不同而不同（默认选中第 0 个输入 vs 最后一个输入），被实际使用的 mux 位串不变。
5. 运行结果：待本地验证。

> 说明：`example_script.openfpga` 默认不带 `--unused_mux_config`，此时取 `"auto"`（[openfpga_bitstream_template.h:48-51](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/base/openfpga_bitstream_template.h#L48-L51)）。要改它需用自定义脚本模板或通过 `task.conf` 的 `SCRIPT_PARAM_*` 注入。

#### 4.4.5 小练习与答案

- **练习 1**：`build_mux_bitstream` 为什么要区分 `mux_size`（datapath 大小）与 `implemented_mux_size`？
  - **答案**：电路模型可能声明了常量输入（`mux_add_const_input`），实际实现的 mux 输入数比 datapath 需要的多一位；用 `implemented_mux_size` 才能从 `mux_lib` 里取到正确的、已实例化的 mux_graph（[build_mux_bitstream.cpp:79-86](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_mux_bitstream.cpp#L79-L86)）。
- **练习 2**：若 `mux_use_local_encoder(mux_model)` 为 false，输出的位数等于什么？
  - **答案**：等于 `mux_graph.num_memory_bits()`（raw 位，每级每个 memory 一位，直接返回，不做二进制地址编码）。

### 4.5 总线型 mux 比特流（本次更新重点：Bus Based MUX）

> 本节对应本次增量提交「Bus Based MUX (#2602)」。它扩展的是 **4.2 里 grid 内部互连 mux** 的解码路径（`build_physical_block_pin_interc_bitstream`），不涉及 routing 侧。

#### 4.5.1 概念说明：什么是总线型 mux，为什么需要特殊处理

普通 pb_graph 互连 mux 是「1 位输出」的 N:1 mux：一个存储块、一份配置位。但 VPR 架构里可以写一种**总线型**互连：

```xml
<mux name="a2a" input="slice.A_cfg slice.B_cfg" output="mult_32x32.a" bus="true">
```

它表示一个 **W 位宽** 的 N:1 mux（例子里 W=32、N=2）：输出 `mult_32x32.a` 是 32 位 bus，每一位都从 `A_cfg` 或 `B_cfg` 里二选一。VPR 会把它展开成 32 个单 bit mux。

关键设计决策（与 u6-l3 的模块构建侧呼应）：OpenFPGA **不**把这 32 个单 bit mux 各配一份存储器，而是让它们**共享同一个选择器、同一个存储块**——只要这 32 位都选同一个输入端口，一个选择位就能同时控制全部 32 位。于是配置位从「32 份」压缩成「1 份」。真实样例见 [k4_frac_N4_tileable_adder_chain_mem1K_frac_dsp32_busmux_40nm.xml:874-879](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/vpr_arch/k4_frac_N4_tileable_adder_chain_mem1K_frac_dsp32_busmux_40nm.xml#L874-L879)（注释明确写了「32 single-bit muxes that share one selector (one config bit)」）。

这给 device 比特流带来一个强约束：

> **整条 bus 的所有位必须选择同一个输入端口**，否则无法用单一共享选择器实现。

#### 4.5.2 核心流程

`build_physical_block_pin_interc_bitstream` 现在按 `cur_interc->bus` 分两路（[build_grid_bitstream.cpp:356](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_grid_bitstream.cpp#L356)）：

```text
求单个 pin 的 path id → 抽成 find_physical_block_pin_mux_path_id()  （新工具函数）

if cur_interc->bus == true:                        # 总线型
    bus_pins = pb_graph_interc_sink_pins(...)      # 收集 bus 所有 sink pin（按位序）
    rep_pin = bus_pins.front()                     # 最低位作「代表位」
    if 当前 des_pin != rep_pin: return             # 只有代表位发块，其余位跳过
    遍历 bus 每一位，各自求 path id：
        跳过未映射位（DEFAULT_PATH_ID）
        若发现某位选的输入 ≠ 其他位选的输入 → 报致命错并退出
    mux_input_pin_id = bus 共识的 path id
    共享存储块以 rep_pin 命名（与 fabric 模块名对齐）
else:                                               # 普通单 bit mux
    mux_input_pin_id = find_physical_block_pin_mux_path_id(...)
```

之后两种情况都汇合：调 `build_mux_bitstream(...)` 解码 → 建一个以代表位命名的存储块 → 挂位。

#### 4.5.3 源码精读

**① 单 pin path id 工具函数（本次新增）**：`find_physical_block_pin_mux_path_id` 把原先内联在 `build_physical_block_pin_interc_bitstream` 里的「求一个 pin 选了哪个输入」逻辑抽成独立函数，供 bus 各位复用，见 [build_grid_bitstream.cpp:252-285](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_grid_bitstream.cpp#L252-L285)。它在三种情况下返回 `DEFAULT_PATH_ID`：目标 pb 未映射、目标 pin 无 net、遍历所有输入都没找到承载该 net 的源 pin。

**② bus sink pin 收集（本次新增辅助）**：`pb_graph_interc_sink_pins` 从目标 pin 出发，收集同一端口下、由同一 interconnect 驱动的所有 pin，并**按位序返回**（首元素即最低位，作为代表位），见 [openfpga/src/utils/pb_graph_utils.cpp:97-127](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/utils/pb_graph_utils.cpp#L97-L127)。配套的 `is_pb_graph_pin_bus_interc` 直接返回 `interc->bus`，见 [pb_graph_utils.h:80-87](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/utils/pb_graph_utils.h#L80-L87)。

**③ 「各位须同选一输入」约束的强制**：这是总线型 mux 比特流最核心的检查，见 [build_grid_bitstream.cpp:368-389](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_grid_bitstream.cpp#L368-L389)：

```cpp
int bus_path_id = DEFAULT_PATH_ID;
for (t_pb_graph_pin* bus_pin : bus_pins) {
  int pin_path_id = find_physical_block_pin_mux_path_id(
    physical_pb, bus_pin, cur_interc, fan_in);
  if (DEFAULT_PATH_ID == pin_path_id) { continue; }      // 该位未映射，跳过
  if (DEFAULT_PATH_ID == bus_path_id) {
    bus_path_id = pin_path_id;                            // 首个映射位定基准
    net_pb_graph_pin = bus_pin;
  } else if (bus_path_id != pin_path_id) {
    VTR_LOGF_ERROR(__FILE__, __LINE__,
      "Bus-based mux '%s' (Arch[LINE%d]) requires all bits of the "
      "output bus to be routed from the same input port. Bit '%s' "
      "selects input %d while another bit of the same bus selects "
      "input %d. Check the packing/routing results.\n", /* … */);
    exit(CMD_EXEC_FATAL_ERROR);
  }
}
```

逻辑：以第一个被映射的 bus 位建立基准 `bus_path_id`，后续每个被映射位都必须与之相同；一旦发现某位选了不同输入端口，立即报致命错退出——因为共享选择器物理上无法同时满足两个不同选择。

**④ 共享存储块以代表位命名**：见 [build_grid_bitstream.cpp:437-438](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_grid_bitstream.cpp#L437-L438)，块名用 `rep_pb_graph_pin`（最低位）生成。这一步至关重要：模块构建侧（u6-l3）也是以最低位代表命名那个唯一的共享存储器实例，比特流侧必须用同样的名字，device→fabric 转换时才能按「块名==实例名」对上号（见 u7-l1）。

**⑤ net 上报取首个映射位**：当 bus 只被部分映射时，代表位（最低位）可能恰好未用，于是用 `net_pb_graph_pin`（首个映射位）来记录输入/输出 net，见 [build_grid_bitstream.cpp:401-418](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_grid_bitstream.cpp#L401-L418)。这保证比特流里 `<input_net>`/`<output_net>` 标注对部分映射的 bus 也有意义。

#### 4.5.4 代码实践：总线型 mux 的「1 份 vs 32 份」配置位

本次更新专门落了两个回归任务，本实践直接用它们的**已提交黄金产出**（无需自己跑也能验证；想跑则按下方步骤）。

1. **目标**：证实总线型 mux 把 32 份配置位压缩成了 1 份共享选择位。
2. **步骤 A（直接看黄金产出）**：打开回归任务的 bitstream 分布黄金文件 [openfpga_flow/tasks/basic_tests/no_time_stamp/frac_dsp_busmux/golden_outputs_no_time_stamp/bitstream_distribution.xml](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/tasks/basic_tests/no_time_stamp/frac_dsp_busmux/golden_outputs_no_time_stamp/bitstream_distribution.xml)。找到 DSP 网格块：
   ```xml
   <block name="grid_mult_32_4__1_" number_of_bits="5">
   ```
3. **观察与解读**：该 `grid_mult_32` 块总共只有 **5** 个配置位。其中 `a2a` 这个 32 位宽 2:1 总线 mux 只贡献 **1** 个共享选择位（加上 mult 的少量 mode 位合计 5）。
4. **反证（关键）**：如果总线型 mux 支持被回退成「32 个独立单 bit mux」，则 `a2a` 一项就要 32 个位，`grid_mult_32_4__1_` 的 `number_of_bits` 会从 5 跳到约 \(5 - 1 + 32 = 36\)。回归脚本 `basic_reg_test.sh` 对该黄金文件做 `git diff`，任何这种回退都会让 CI 失败——这正是任务配置注释里写的守护意图（[task.conf:5-11](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/tasks/basic_tests/no_time_stamp/frac_dsp_busmux/config/task.conf#L5-L11)）。
5. **步骤 B（本地实跑，可选）**：`source openfpga.sh` 后运行 `run-task basic_tests/k4_series/k4n4_frac_mult_busmux`，进入结果目录查看 `fabric_independent_bitstream.xml` 与 `bitstream_distribution.xml`，重复步骤 3 的核对。
6. **预期结果**：`grid_mult_32` 块位数为 5（与黄金一致）；在 `fabric_independent_bitstream.xml` 里，`a2a` 总线 mux 只有一个以代表位命名的共享存储块，而不是 32 个。
7. 运行结果：步骤 A 的 `5` 直接来自已提交黄金文件，可立即核对；步骤 B 待本地验证。

> 补充说明：`k4n4_frac_mult_busmux` 与 `frac_dsp_busmux` 两个任务的 benchmark 都是 `and2`，并不真正使用乘法器。总线 mux `a2a` 之所以仍出现在比特流里，是因为含乘法器的 `grid_mult_32` 物理块即便未被使用，递归骨架也会为它的内部互连 mux 生成**默认路径**位（4.2.2 的剪枝只跳过「无 LOGICAL 可配置子模块」的模块，而 `mult_32x32_slice` 有可配置互连）。这恰好让「共享 1 位 vs 独立 32 位」的差异体现在分布文件里，成为廉价而稳定的回归守护。

#### 4.5.5 小练习与答案

- **练习 1**：为什么总线型 mux 选「最低位」作为代表位来命名共享存储块，而不是任意一位？
  - **答案**：因为模块构建侧（u6-l3 的 `add_module_pb_bus_mux_interc`）也用最低位代表来命名那个唯一的共享存储器实例。比特流侧必须用**相同**的命名，device→fabric 转换时才能按块名对齐（[build_grid_bitstream.cpp:437-438](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_grid_bitstream.cpp#L437-L438)）。两侧约定一致是正确性的硬要求。
- **练习 2**：若一条 8 位 bus mux 中，第 0、1、2 位选输入 A，第 3 位选输入 B，会发生什么？
  - **答案**：触发 [build_grid_bitstream.cpp:378-388](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_grid_bitstream.cpp#L378-L388) 的 `VTR_LOGF_ERROR`，进程以 `CMD_EXEC_FATAL_ERROR` 退出。因为共享选择器无法让部分位选 A、部分位选 B，这是物理上不可实现的打包/布线结果。
- **练习 3**：把单 bit path id 求解抽成 `find_physical_block_pin_mux_path_id` 除了「bus 各位复用」之外，还带来了什么好处？
  - **答案**：它统一了「单 bit mux」与「bus 各位」两条路径的求解口径（普通 mux 的 else 分支也调它，[build_grid_bitstream.cpp:393-399](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/fpga_bitstream/build_grid_bitstream.cpp#L393-L399)），消除了重复代码，也使「未映射/无 net/无匹配输入」三类 `DEFAULT_PATH_ID` 判定只维护一份。

## 5. 综合实践

把本讲四条主线（device 入口、grid、routing、共用 mux 引擎、总线型 mux）串成一个端到端追踪任务。

**任务**：以 `basic_tests/full_testbench/configuration_chain`（and2）为主样本，再以 `basic_tests/no_time_stamp/frac_dsp_busmux` 为对照样本，完成下列追踪并把结论填进一张表。

1. **入口层**：在 [example_script.openfpga](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/openfpga_shell_scripts/example_script.openfpga) 中定位 `build_architecture_bitstream`，画出「命令模板 `fpga_bitstream_template` → `build_device_bitstream` → `build_grid_bitstream` + `build_routing_bitstream`」的调用链，并标注每层读写 context 的哪些分区。
2. **grid 层**：在 and2 的 `fabric_independent_bitstream.xml` 里挑一个被使用的 LUT 块，写出它的 16 位如何由 `and2` 真值表经 `build_frac_lut_bitstream` 解码；再挑一个未使用的 LUT，说明它为何是全默认值。
3. **routing 层**：挑一个 SB mux 块，用它的 `path_id` 与 `<input_net>`/`<output_net>` 反推：是哪两个 rr_node（前驱→当前）的 net 匹配定出了这个 path id？
4. **总线型 mux 层**：打开 `frac_dsp_busmux` 的黄金 `bitstream_distribution.xml`，记录 `grid_mult_32_4__1_` 的 `number_of_bits`，并解释若总线 mux 回退成 32 个独立位，这个数字会变成多少、为什么会被 CI 捕获。
5. **汇总表**（示例列：层 / 数据来源 / 关键函数 / 一个具体位的来源）填好后，你应当能用一句话回答：「device 比特流的每一位，要么来自真值表，要么来自 mode_bits，要么来自某个 mux 的 path id。」

**验收标准**：能不看源码讲清「一个 LUT 位」和「一个 SB mux 位」各自的解码链；能说清总线型 mux 为何只产生 1 个共享位、以及它何时会报致命错。

## 6. 本讲小结

- `build_architecture_bitstream` 生成的是 **device 级、协议无关** 的 `BitstreamManager`：入口 `build_device_bitstream` 先建顶层块、两路预估容量，再分交 grid 与 routing 解码，最后断言块数/位数与预估一致。
- **grid 比特流**沿 pb_graph 递归，三类来源：LUT（真值表 → \(2^K:1\) mux_graph 反查）、primitive（mode_bits，`x` 用 physical base 兜底）、pb_graph 内部互连 mux。
- **routing 比特流**遍历每个 GSB 的 SB/CB，用「输出 net + 前驱节点」双重匹配定出每个布线 mux 的 path id；fan-in>1 即判为 mux。
- grid 与 routing 殊途同归，都把 path id 交给**共用引擎 `build_mux_bitstream`**：`decode_memory_bits` 反查 raw 位，local encoder 再逐级二进制编码；未使用 mux 按 `unused_mux_config`（first/last/auto/unused_input）选默认路径。
- **总线型 mux（本次更新）**：带 `bus="true"` 的 W 位 N:1 mux 共享单个选择器，配置位从 W 份压成 1 份；强约束是「整条 bus 各位须选同一输入端口」，否则报致命错；共享存储块以最低代表位命名，与 fabric 模块侧对齐，并新增 `find_physical_block_pin_mux_path_id` 与 `pb_graph_interc_sink_pins` 两个工具函数支撑。

## 7. 下一步学习建议

- **u7-l3（build_fabric_bitstream 与协议相关组织）**：本讲产出的 device 级 `BitstreamManager` 在那里被按 fabric 模块层级与配置协议重组，加上 BL/WL 或帧地址——建议接着读 `build_fabric_bitstream.cpp` 与 `build_fabric_bitstream_memory_bank.cpp`，看「位值不变得到寻址」如何实现。
- **u7-l4（write_fabric_bitstream 与 fast_configuration）**：关心输出文件格式与「跳过大量相同位」的 fast_configuration 策略，可继续读 `write_text_fabric_bitstream.cpp` 与 `fast_configuration.cpp`。
- **u6-l3（构建 grid/routing 子模块）**：若想彻底搞懂总线型 mux 在 fabric 侧如何建成「1 个共享存储器 + W 个 mux」，回看 `build_grid_modules.cpp` 的 `add_module_pb_bus_mux_interc` 与 `module_manager_utils` 的 `add_module_nets_between_logics_and_shared_memory_sram_bus`——本讲的比特流命名正是为了与它对齐。
- **进阶练习**：仿照 `k4n4_frac_mult_busmux` 任务，在一个最小 VPR 架构里自己加一条 `<mux bus="true"/>`，故意让两位选不同输入，观察本讲 4.5.3 ③的致命错误是否如预期触发（这能加深对「共享选择器」物理约束的理解）。
