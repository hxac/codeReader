# link_openfpga_arch：把架构绑定到 VPR 数据库

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `link_openfpga_arch` 在 `vpr` 与 `build_fabric` 之间扮演的「桥梁」角色，以及为什么它不可省略。
- 逐段讲出 `link_arch_template` 的编排顺序，理解它把哪些 VPR 结果与 OpenFPGA 架构「缝合」在一起。
- 读懂 `VprDeviceAnnotation` 这个「中央账本」的数据组织方式，并能指出 pb_type、rr_switch、rr_segment 是如何被绑定到 `CircuitModelId` 的。
- 解释 `--activity_file`、`--sort_gsb_chan_node_in_edges` 等命令选项的实际含义。
- 通过注释/加 `--verbose` 的对比实验，亲眼看到 link 阶段产出的标注数据。

## 2. 前置知识

本讲是 u5（架构加载、解析与 VPR 标注）的核心一讲，承接以下已有认知：

- **OpenfpgaContext（u2-l3）**：命令之间交换数据的唯一全局中枢，提供 `const`（只读）与 `mutable`（可写）两套访问器。`link_openfpga_arch` 是典型的「读 `arch()`、写 `mutable_vpr_device_annotation()` 等多个分区」的命令。
- **两套世界（u3-l1 / u5-l1）**：VPR 的 `vpr` 命令把布局布线结果写进共享的 `g_vpr_ctx`（含 `DeviceContext`、`ClusteringContext`、`PlacementContext`、`AtomContext`）；`read_openfpga_arch`（u5-l2）把 XML 解析成只读的 `openfpga::Arch`（落在 context 的 `arch_` 分区）。这两套数据各自完整，但**互不相识**。
- **解析存名字（u5-l2）**：`read_openfpga_arch` 阶段只把电路模型、pb_type 注解里的引用以**字符串**形式存下来，尚未与 VPR 内存对象挂钩。
- **pb_type 注解（u4-l3）**：`PbTypeAnnotation` 只存名字字符串（如 pb_type 路径 `clb.fle[n1_lut4].ble4.lut4`、`circuit_model_name`），承载 operating↔physical、pb_type↔circuit_model、interconnect↔circuit_model 三类绑定。本讲要回答的就是：**这些字符串如何被翻译成 VPR 指针与 `CircuitModelId`**。
- **operating vs physical pb_type（u4-l3）**：operating pb_type 是 VPR 打包目标（可打包模式），physical pb_type 是 OpenFPGA fabric 的物理实现目标（不可打包）。

> 一句话定位：`read_openfpga_arch` 负责「读」，`link_openfpga_arch` 负责「连」。前者把 XML 变成内存里的 `Arch`，后者把 `Arch` 里的悬空名字在 VPR 已经建好的 device 里「对账」并落实成指针/ID。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [openfpga/src/base/openfpga_link_arch_template.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_link_arch_template.h) | `link_arch_template<T>` 编排器，按固定顺序调用十几个 `annotate_*` / `build_*` 子函数，是本讲的主线。 |
| [openfpga/src/base/openfpga_setup_command_template.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h) | `link_openfpga_arch` 命令的注册、选项定义与依赖声明（含对 `build_fabric` 的硬依赖边）。 |
| [openfpga/src/annotation/vpr_device_annotation.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/vpr_device_annotation.h) | `VprDeviceAnnotation` 数据结构——链接结果的「中央账本」。 |
| [openfpga/src/annotation/annotate_pb_types.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/annotate_pb_types.cpp) | pb_type 标注的全部实现：physical mode、physical pb_type、circuit model、mode bits 的「显式 + 隐式」两段式。 |
| [openfpga/src/annotation/annotate_rr_graph.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/annotate_rr_graph.cpp) | 把 rr_switch / rr_segment / direct 绑定到电路模型，并构建 `DeviceRRGSB`。 |
| [openfpga/src/base/openfpga_context.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_context.h) | `OpenfpgaContext` 中 `vpr_device_annotation_`、`device_rr_gsb_`、`mux_lib_`、`tile_direct_` 等成员与访问器。 |
| [openfpga_flow/openfpga_shell_scripts/example_script.openfpga](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_shell_scripts/example_script.openfpga) | canonical 流程脚本，第 13 行就是 `link_openfpga_arch` 的真实调用。 |

## 4. 核心概念与源码讲解

### 4.1 link_openfpga_arch 命令与整体编排

#### 4.1.1 概念说明

`build_fabric`（u6）要为 FPGA 上的每个逻辑块、每条布线开关生成 Verilog/SPICE 模块。要完成这件事，它必须同时知道两件事：

- **结构事实**（来自 VPR）：这个位置上摆的是哪个 `t_pb_type`？这条布线用的是哪个 `rr_switch` / `rr_segment`？——这些是 VPR 跑完布局布线后写进 `g_vpr_ctx` 的 C 结构体指针。
- **电路事实**（来自 `Arch`）：这个 `t_pb_type` 该用哪个电路模型实现？这个 `rr_switch` 该用哪种 mux？——这些是 `read_openfpga_arch` 解析出的 `CircuitModelId`。

问题是：VPR 的结构体里**根本没有**「电路模型」这个字段（VPR 不关心电路级实现），而 `Arch` 里的电路模型**也认识不到**具体的 `t_pb_type` 指针。两者各自完整却互不相识。

`link_openfpga_arch` 就是那根缝合的针：它遍历 VPR 的 device，用 `Arch` 里的 pb_type 注解（名字字符串）去 VPR 的 pb graph 里**反查**出对应的 `t_pb_type*` 指针，再把查到的电路模型 ID 记到一张「平行账本」——`VprDeviceAnnotation` 上。这样 `build_fabric` 只需要查这张账本，就能在「不修改 VPR 源码」的前提下拿到全部电路级信息。

> 这种「不动 VPR、平行维护一张标注表」的手法，就是 OpenFPGA 贯穿始终的 **annotation 模式**（u5-l4 会系统讲解整个 annotation 子系统）。

#### 4.1.2 核心流程

`link_arch_template<T>` 是一个线性编排器，按下表顺序执行（每一步都对应源码里一段连续的调用）：

```
link_arch_template(openfpga_ctx, cmd, cmd_context)
│
├─ 1. 读取 GSB 版本（从 VPR device context，非命令选项）
├─ 2. build_physical_tile_pin2port_info          物理瓦片引脚 → 端口 快速查找
├─ 3. build_physical_tile_equivalent_sites       子瓦片等价 site
├─ 4. annotate_pb_types                          ★ physical mode / physical pb_type / circuit model / mode bits
├─ 5. annotate_pb_graph                          pb_graph_node 唯一索引 + operating↔physical 节点/引脚绑定
├─ 6. annotate_rr_graph_circuit_models           rr_switch / rr_segment / direct → circuit model
├─ 7. vpr_routing_annotation.init + 标注 rr_node 的 net 与 previous_node
├─ 8. is_vpr_rr_graph_supported                  合法性闸门（不支持则 FATAL 退出）
├─ 9. RRGraphInEdges + annotate_device_rr_gsb    构建 DeviceRRGSB（带反向边映射）
├─ 10.（可选）sort_device_rr_gsb_*_in_edges      对 GSB 入边排序/重排
├─ 11. build_device_mux_library                  收集器件中所有去重 mux → MuxLibrary
├─ 12. build_device_tile_direct                  构建 TileDirect（CLB 直连）
├─ 13. annotate clustering results               聚类结果同步 + 物理等价 site
├─ 14. annotate_mapped_blocks                    布局结果标注
├─ 15.（可选）read_activity                       读信号翻转率文件
├─ 16. annotate_simulation_setting               仿真设置（消耗 activity）
└─ 17. annotate_bitstream_setting                比特流设置标注
```

这条链有两个值得记住的设计点：

1. **顺序即依赖**。例如第 4 步 `annotate_pb_types` 必须先于第 5 步 `annotate_pb_graph`，因为后者要把 operating 节点绑到 physical 节点，得先知道谁是 physical；第 11 步 `build_device_mux_library` 依赖第 9 步建好的 `DeviceRRGSB`，因为 mux 是从布线结构里提取的。
2. **几乎每一步都向 `mutable_vpr_device_annotation()`（或别的 mutable 分区）写入**，而读取对象是只读的 `g_vpr_ctx.device()` 与 `openfpga_ctx.arch()`。这正是 u2-l3 强调的「const 读 / mutable 写」约定在 link 阶段的大规模落地。

#### 4.1.3 源码精读

函数签名与计时器，注释里点明了它的四大职责（physical pb_type、mode bits、circuit model、pb_graph、全局布线电路模型）：

[openfpga/src/base/openfpga_link_arch_template.h:37-49](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_link_arch_template.h#L37-L49) —— 顶部注释列出 link 要完成的全部绑定；构造 `ScopedStartFinishTimer` 打印「Link OpenFPGA architecture to VPR architecture」耗时。

命令选项被取出后用于后续分支判断（注意 GSB 版本不是命令选项，而是直接读 VPR device context，注释解释了原因——避免 VPR 选项与本命令不一致）：

[openfpga/src/base/openfpga_link_arch_template.h:51-62](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_link_arch_template.h#L51-L62) —— 取出 `activity_file`、`sort_gsb_chan_node_in_edges`、`reorder_incoming_edges`、`allow_gsb_dangling_opin`、`verbose` 五个选项；`gsb_version` 来自 `g_vpr_ctx.device().gsb_version`。

pb_type 与 pb_graph 标注（本讲的两个主角，4.3 详解）：

[openfpga/src/base/openfpga_link_arch_template.h:75-92](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_link_arch_template.h#L75-L92) —— 先 `annotate_pb_types` 写物理模式/电路模型/mode bits，再 `annotate_pb_graph` 给每个 pb_graph_node 分配唯一索引并完成 operating↔physical 节点、引脚的绑定。两者都把结果写进 `mutable_vpr_device_annotation()`。

布线电路模型标注 + DeviceRRGSB 构建：

[openfpga/src/base/openfpga_link_arch_template.h:94-131](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_link_arch_template.h#L94-L131) —— `annotate_rr_graph_circuit_models` 把 switch/segment/direct 绑定到电路模型；随后用 `is_vpr_rr_graph_supported` 做闸门；再构建 OpenFPGA 自己的反向边映射 `RRGraphInEdges`（因为 VPR 只存 fan-out 边），并调用 `annotate_device_rr_gsb` 生成 `DeviceRRGSB`。第二个实参 `!openfpga_ctx.clock_arch().empty()` 是「是否存在时钟架构」标志（源码 FIXME 标注待加固）。

mux 库与 tile direct（注意 `build_device_mux_library` 以 `const_cast<const T&>(openfpga_ctx)` 只读方式读取 context，再用返回值赋给 `mutable_mux_lib()`）：

[openfpga/src/base/openfpga_link_arch_template.h:143-150](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_link_arch_template.h#L143-L150) —— `mutable_mux_lib() = build_device_mux_library(...)` 与 `mutable_tile_direct() = build_device_tile_direct(...)`。

activity 文件读取（注释说明它何时为必需）：

[openfpga/src/base/openfpga_link_arch_template.h:175-185](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_link_arch_template.h#L175-L185) —— 仅当用户给了 `--activity_file` 才调用 `read_activity`，返回 `std::unordered_map<AtomNetId, t_net_power>`；注释指出 activity 在「让工具从实现结果推断时钟周期数」或「启用 FPGA-SPICE」两种场景下是必需的。

末尾的 simulation/bitstream 设置标注：

[openfpga/src/base/openfpga_link_arch_template.h:196-210](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_link_arch_template.h#L196-L210) —— `annotate_simulation_setting` 消费上一步的 `net_activity`；`annotate_bitstream_setting` 把比特流设置标注到 `mutable_vpr_bitstream_annotation()`。任一返回 `CMD_EXEC_FATAL_ERROR` 则整条命令失败。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 link 阶段被 shell 当作 `build_fabric` 的硬前置，并直接观察它产出的标注日志。

**操作步骤**（基于 u1-l4 的 `run-task` 方式，以最简微基准 `and2` 为例）：

1. `source openfpga.sh`，跑通一个现成任务确认环境正常：
   ```bash
   run-task basic_tests/full_testbench/configuration_chain
   ```
2. 找到该任务使用的脚本模板（一般在 `latest/.../SRCK4N4_.../` 上层的临时脚本），或在仓库里直接编辑一份 `example_script.openfpga` 的副本用于实验。
3. **实验 A（验证硬依赖）**：在副本中把 `link_openfpga_arch ...` 整行注释掉（行首加 `#`），保留其后的 `build_fabric`，重新跑该任务。
4. **实验 B（观察链接产物）**：恢复 `link_openfpga_arch`，并给它追加 `--verbose`：
   ```
   link_openfpga_arch --activity_file ${ACTIVITY_FILE} --sort_gsb_chan_node_in_edges --verbose
   ```
   重新跑任务，重定向 openfpga 的 stdout 到日志文件。

**需要观察的现象**：

- 实验 A：`build_fabric` 不会真正执行，而是在它之前就被 shell 的依赖检查拦下，报类似「command should NOT be executed before 'link_openfpga_arch'」的错误并返回非零退出码。
- 实验 B：日志里会出现大量形如 `Annotate pb_type '...' with physical mode '...'`、`Bind pb type '...' port '...' to circuit model '...' port '...'`、`Implicitly infer physical mode '...' for pb_type '...'` 的行。

**预期结果**：

- 实验 A 证明：link 不是「可选优化」，而是被命令依赖机制（见 4.1.5 与下文命令注册）强制的前置——缺它，`build_fabric` 连启动都不允许。
- 实验 B 直接揭示了 link 的产物：每条 `Bind ... to circuit model ...` 日志对应一次「名字字符串 → CircuitModelId」的对账落账。

> 待本地验证：具体错误文案与日志行数取决于所用 arch 与基准；若环境未编译 openfpga，可退化为「源码阅读型实践」——只读 `link_arch_template` 的调用顺序即可。

#### 4.1.5 小练习与答案

**练习 1**：`gsb_version` 为什么不设计成 `link_openfpga_arch` 的命令选项，而要从 VPR device context 读取？

**参考答案**：因为 GSB 版本在 VPR 生成 RR graph 时就已确定，属于「既成事实」。若再做成命令选项，用户可能传一个与实际 RR graph 不一致的值，造成 GSB 构建错乱。源码注释明确写道「It is not a command option to avoid any mismatch between the VPR options and this command」。

**练习 2**：`build_device_mux_library` 为什么要用 `const_cast<const T&>(openfpga_ctx)` 把 context 转成只读引用再传入？

**参考答案**：因为它需要**读取** context 里的 `vpr_device_annotation`、`device_rr_gsb` 等多个分区来统计 mux，但本身只应**写回** `mux_lib`。用 `const` 引用传入可在函数内部阻止误写其它分区，而 `mux_lib` 通过返回值 + 外层 `mutable_mux_lib() = ...` 赋值完成写入，符合 const 读 / mutable 写约定。

**练习 3**：`annotate_pb_graph`（第 5 步）为什么必须排在 `annotate_pb_types`（第 4 步）之后？

**参考答案**：`annotate_pb_graph` 要把 operating pb_graph_node 绑到它的 physical pb_graph_node、把引脚一一配对；而「谁是 physical pb_type」正是 `annotate_pb_types` 写进 `VprDeviceAnnotation` 的。顺序反了，physical 查询会得到空指针。

---

### 4.2 VprDeviceAnnotation：链接结果的中央账本

#### 4.2.1 概念说明

link 阶段把 VPR 结构与 OpenFPGA 电路「缝合」的全部结果，都汇入一个叫 `VprDeviceAnnotation` 的对象。它是 OpenFPGA 注解子系统的核心成员，落在新 `OpenfpgaContext` 的 `vpr_device_annotation_` 分区。

它的设计哲学只有一句话：**绝不修改 VPR 的 `t_pb_type`、`t_rr_node` 等原生结构，而是用一张张 `std::map` 以 VPR 对象指针为键，平行地挂上 OpenFPGA 需要的额外信息**。这样一来：

- VPR 升级、改结构时，OpenFPGA 不用改 VPR 源码（这正是 OpenFPGA 能紧跟 VPR 上游的关键）。
- 「这个 pb_type 用哪个电路模型」这种 OpenFPGA 专属问题，统一通过查 `VprDeviceAnnotation` 解决，`build_fabric` 不必直接碰 VPR 结构。

#### 4.2.2 核心流程与数据组织

`VprDeviceAnnotation` 内部几乎全是「`t_xxx*` → OpenFPGA 值」的映射。按下表理解（节选自源码私有数据段）：

| 键（VPR 对象指针） | 值（OpenFPGA 标注） | 由 link 的哪一步写入 | 典型消费者 |
| --- | --- | --- | --- |
| `t_pb_type*` | `t_pb_type*`（physical）`physical_pb_types_` | `annotate_pb_types` | repack、build_grid_modules |
| `t_pb_type*` | `t_mode*`（physical mode）`physical_pb_modes_` | `annotate_pb_types` | build_grid_modules、repack |
| `t_pb_type*` | `CircuitModelId` `pb_type_circuit_models_` | `annotate_pb_types` | build_grid/lut/ff modules |
| `t_interconnect*` | `CircuitModelId` `interconnect_circuit_models_` | `annotate_pb_types` | build pb 内部互连 mux |
| `t_pb_type*` | `std::vector<char>`（mode bits）`pb_type_mode_bits_` | `annotate_pb_types` | bitstream 生成 |
| `t_port*` | `CircuitPortId` `pb_circuit_ports_` | `annotate_pb_types` | 端口网表生成 |
| `t_pb_graph_node*` | `t_pb_graph_node*`（physical）`physical_pb_graph_nodes_` | `annotate_pb_graph` | repack |
| `RRSwitchId` | `CircuitModelId` `rr_switch_circuit_models_` | `annotate_rr_graph_circuit_models` | build_routing/mux modules |
| `RRSegmentId` | `CircuitModelId` `rr_segment_circuit_models_` | `annotate_rr_graph_circuit_models` | build_routing modules（线段） |
| `size_t`（direct 编号） | `ArchDirectId` `direct_annotations_` | `annotate_rr_graph_circuit_models` | build_tile_direct |
| `t_pb_graph_node*` | `LbRRGraph` `physical_lb_rr_graphs_` | （repack 阶段构建/挂载） | repack |
| `t_physical_tile_type_ptr` + pin index | `BasicPort` / subtile index 等快速查找 | `build_physical_tile_pin2port_info` 等 | IO 引脚映射 |

逻辑上，这张账本回答的都是同一类问题：「给定一个 VPR 对象，OpenFPGA 给它附加了什么电路级含义？」

> 细节：operating pb_type 的物理引脚映射还涉及「旋转偏移」与「累加偏移」。源码注释明确区分了用户指定的 `pin_rotate_offset` 与配对过程中自动累加的 `accumulated offset`，后者在超过物理端口 MSB 时归零（见 `physical_pb_port_offsets_` 注释）。这与 repack 阶段的引脚配对直接相关。

#### 4.2.3 源码精读

类顶部的注释精确概括了它的四项查询职责（判断 physical/operating、查 circuit model、查 physical pb_type、查 physical mode）：

[openfpga/src/annotation/vpr_device_annotation.h:30-38](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/vpr_device_annotation.h#L30-L38) ——「This is the critical data structure to link the pb_type in VPR to openfpga annotations」。

关键的只读访问器（访问器名即意图）：

[openfpga/src/annotation/vpr_device_annotation.h:44-56](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/vpr_device_annotation.h#L44-L56) —— `is_physical_pb_type`、`physical_mode`、`physical_pb_type`、`pb_type_circuit_model`、`interconnect_circuit_model`、`pb_type_mode_bits` 等，全部以 VPR 指针为入参返回 OpenFPGA 标注。

布线侧的两个绑定访问器：

[openfpga/src/annotation/vpr_device_annotation.h:91-93](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/vpr_device_annotation.h#L91-L93) —— `rr_switch_circuit_model(const RRSwitchId&)` 与 `rr_segment_circuit_model(const RRSegmentId&)`：`build_fabric` 生成布线模块时就靠它们查「这个开关用哪个 mux、这段线用哪个 chan_wire 模型」。

私有数据段（账本本体）：

[openfpga/src/annotation/vpr_device_annotation.h:169-285](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/vpr_device_annotation.h#L169-L285) —— 全部 `std::map`，每段注释都说明约束（如「physical mode MUST be a child mode of the pb_type」「pb_type MUST be a physical pb_type itself」）。注意末尾还有一组物理瓦片（physical tile）相关的快速查找表与等价 site 表，由 link 的第 2、3 步填充。

在 `OpenfpgaContext` 中的挂载（注意 `device_rr_gsb_` 的构造依赖 `vpr_device_annotation_`）：

[openfpga/src/base/openfpga_context.h:199-226](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_context.h#L199-L226) —— `vpr_device_annotation_` 在 L199；`device_rr_gsb_{vpr_device_annotation_}`（L217）把账本引用喂给 `DeviceRRGSB`，因为 GSB 在构建/查询时也要读物理引脚信息；`mux_lib_`（L220）、`tile_direct_`（L226）也在同一片区域。

#### 4.2.4 代码实践

**实践目标**：动手梳理「访问器 ↔ 修改器」配对，理解账本的写入路径。

**操作步骤**：

1. 打开 `vpr_device_annotation.h`，在「Public accessors」段（L43–105）与「Public mutators」段（L107–167）中，为下表每一行找到对应的访问器与修改器：

   | 标注 | 访问器（读） | 修改器（写） |
   | --- | --- | --- |
   | pb_type 的 physical mode | `physical_mode()` | `add_pb_type_physical_mode()` |
   | pb_type 的 circuit model | `pb_type_circuit_model()` | `add_pb_type_circuit_model()` |
   | rr_switch 的 circuit model | `rr_switch_circuit_model()` | `add_rr_switch_circuit_model()` |
   | mode bits | `pb_type_mode_bits()` | `add_pb_type_mode_bits()` |

2. 在 `annotate_pb_types.cpp` 与 `annotate_rr_graph.cpp` 里 `grep` 这些 `add_*` 修改器，确认它们的调用点都集中在 link 阶段。

**需要观察的现象**：每个「写」修改器只在 link 链路的某一两处被调用；而「读」访问器则散布在 `fabric/`、`repack/`、`fpga_bitstream/` 等多个下游子系统。

**预期结果**：你会看到清晰的「link 写一次，下游读多处」的单向数据流——这正是 `VprDeviceAnnotation` 作为中央账本的价值。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `VprDeviceAnnotation` 用 `t_pb_type*`（裸指针）作为 map 的键，而不是 pb_type 的名字字符串？

**参考答案**：因为 link 之后，下游（build_fabric、repack 等）拿到的是 VPR 内存里的 `t_pb_type*` 指针本身。用指针做键可以 O(log n) 直接查到标注，无需再做字符串比较；而且 VPR 的 pb graph 在进程生命周期内地址稳定。名字字符串只在 link 的「反查」阶段（把 XML 名字落实成指针）使用，一旦落账就不再需要。

**练习 2**：账本里有 `physical_pb_type_index_factors_` 和 `physical_pb_type_index_offsets_` 两个 map，它们的用途是什么？（提示：operating 与 physical 的实例数量可能不同。）

**参考答案**：一个 operating pb_type 实例可能对应多个 physical pb_type 实例（或反之），需要把 operating 的实例下标换算成 physical 的实例下标。换算关系为 \( i_{\text{phys}} = i_{\text{op}} \times f + o \)，其中 \( f \) 是 `index_factor`、\( o \) 是 `index_offset`。这两个 map 存的就是 \( f \) 和 \( o \)，由 `pair_operating_and_physical_pb_types` 写入。

**练习 3**：`DeviceRRGSB` 的构造为什么要把 `vpr_device_annotation_` 作为实参传进去？

**参考答案**：`DeviceRRGSB` 在构建和查询通用开关块时，需要知道物理瓦片的引脚→端口映射等信息，而这些恰由 `VprDeviceAnnotation` 维护。把账本引用注入进去，可避免 GSB 重复实现一套同样的查找逻辑，也保证两边数据一致。

---

### 4.3 pb_type 标注详解：从路径字符串到电路模型

#### 4.3.1 概念说明

`annotate_pb_types` 是 link 链路里最复杂的一步，也是「解析存名字 → 链接查 ID」范式的集大成者。它的输入是 `Arch::pb_type_annotations`——一组 `PbTypeAnnotation`（u4-l3），每条只存名字字符串；输出是写满的 `VprDeviceAnnotation`。

它要完成 **5 类绑定**，每类都遵循一个统一套路：

- **显式（explicit）**：按 XML 里用户写明的内容，在 VPR pb graph 里逐条反查落实。
- **隐式（implicit）**：对用户没写的，按「合理默认」自动推断（典型如单模式 pb_type 默认其唯一模式为 physical mode）。
- **检查（check）**：每类绑定结束后跟一道 `check_*` 闸门，失败即报错。

这 5 类依次是：① physical mode；② interconnect 的 physical type；③ physical pb_type（operating↔physical 配对）；④ circuit model（pb_type + interconnect）；⑤ mode bits。

#### 4.3.2 核心流程

`annotate_pb_types` 的顶层流水（每段都配 `VTR_LOG` 进度提示与 `check_*` 闸门）：

```
annotate_pb_types
├─ ① physical mode
│   ├─ build_vpr_physical_pb_mode_explicit_annotation   遍历注解，用 physical_mode_name 反查 t_mode*
│   ├─ build_vpr_physical_pb_mode_implicit_annotation   单 mode→默认；多 mode→必须已显式，否则报错
│   └─ check_vpr_physical_pb_mode_annotation
├─ ② interconnect physical type   annotate_pb_graph_interconnect_physical_type
│      （必须在推断 interconnect 电路模型之前完成）
├─ ③ physical pb_type
│   ├─ build_vpr_physical_pb_type_explicit_annotation   operating↔physical 配对 + 端口/索引
│   ├─ build_vpr_physical_pb_type_implicit_annotation   原始 pb_type 自配对（self-pair）
│   └─ check_vpr_physical_pb_type_annotation
├─ ④ circuit model
│   ├─ link_vpr_pb_type_to_circuit_model_explicit_annotation      pb_type→circuit_lib.model(name)
│   ├─ link_vpr_pb_interconnect_to_circuit_model_explicit_annotation
│   ├─ link_vpr_pb_interconnect_to_circuit_model_implicit_annotation  MUX_INTERC→MUX, DIRECT→WIRE ...
│   └─ check_vpr_pb_type_circuit_model_annotation
└─ ⑤ mode bits
    ├─ link_vpr_pb_type_to_mode_bits_explicit_annotation
    └─ check_vpr_pb_type_mode_bits_annotation
```

两个贯穿始终的「反查」原语：

1. **pb_type 路径反查**：把 `clb.fle[n1_lut4].ble4.lut4` 这样的路径（type 名 + mode 名逐级下钻）在 VPR 的 `logical_block_types` 里逐层匹配，找到唯一的 `t_pb_type*`。实现是 `try_find_pb_type_with_given_path`。
2. **电路模型名反查**：用 `circuit_lib.model(name)` 把字符串名翻译成强类型 `CircuitModelId`（u3-l3 讲过 CircuitLibrary 的 SoA 风格）。

#### 4.3.3 源码精读

顶层流水与各段进度/闸门：

[openfpga/src/annotation/annotate_pb_types.cpp:1098-1189](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/annotate_pb_types.cpp#L1098-L1189) —— 五段依次执行，段间穿插 `check_*`。注意 L1122-L1132 的注释强调：interconnect 的 physical type 标注「必须在推断其电路模型之前」完成。

physical mode 的显式反查（典型「解析存名字 → 链接查指针」三段式）：

[openfpga/src/annotation/annotate_pb_types.cpp:24-110](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/annotate_pb_types.cpp#L24-L110) —— 遍历 `pb_type_annotations`，跳过没声明 `physical_mode_name` 的；按 operating/physical 两种情况收集目标路径；在 `logical_block_types` 里用 `try_find_pb_type_with_given_path` 找到 `t_pb_type*`，再用 `find_pb_type_mode` 拿到 `t_mode*`，最后 `add_pb_type_physical_mode` 落账。找不到则 `VTR_LOG_ERROR`。

physical mode 的隐式推断（单模式自动默认、多模式强制要求显式）：

[openfpga/src/annotation/annotate_pb_types.cpp:122-159](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/annotate_pb_types.cpp#L122-L159) —— 当 `num_modes == 1` 且尚未被显式标注时，把唯一 mode 当作 physical mode（L138-L146）；当 `num_modes > 1` 且未被显式标注，直接报错并提示「请在 OpenFPGA architecture 里指定」（L149-L157）。这条规则解释了为什么简单的单模式架构几乎不用写 physical_mode_name。

operating↔physical 配对（端口逐个配对 + index factor/offset）：

[openfpga/src/annotation/annotate_pb_types.cpp:213-287](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/annotate_pb_types.cpp#L213-L287) —— `pair_operating_and_physical_pb_types`：遍历 operating 的每个端口，若注解里没显式给出物理端口映射，就默认「同名同宽」；逐端口写入 `add_physical_pb_port` / `add_physical_pb_port_range` / `add_physical_pb_pin_initial_offset` / `add_physical_pb_pin_rotate_offset`；最后写入 pb_type 配对与 index factor/offset。

pb_type → circuit model 的链接（含端口名/宽度/类型三重校验）：

[openfpga/src/annotation/annotate_pb_types.cpp:568-607](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/annotate_pb_types.cpp#L568-L607) —— `link_physical_pb_type_to_circuit_model`：先断言这是 physical pb_type（operating 不允许绑电路模型，L576-L582）；用 `circuit_lib.model(name)` 查 ID，找不到报错；再调 `link_physical_pb_port_to_circuit_port` 做「端口同名 + 宽度一致 + 类型匹配」三重检查（见 L502-L562），通过后 `add_pb_type_circuit_model` 落账。

interconnect 电路模型的隐式推断（按互连类型推电路模型类型）：

[openfpga/src/annotation/annotate_pb_types.cpp:900-950](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/annotate_pb_types.cpp#L900-L950) —— 对用户没显式指定的 interconnect，按其 physical type 推断所需电路模型类型（源码顶部 L889-L898 注释给出映射：`MUX_INTERC→MUX`、`DIRECT_INTERC→WIRE`、单输入 `COMPLETE_INTERC→WIRE`、多输入 `COMPLETE_INTERC→MUX`），再用 `circuit_lib.default_model(type)` 取默认模型。这就是为什么大多数架构无需为每个 interconnect 显式写 circuit_model_name。

#### 4.3.4 代码实践

**实践目标**：跟踪一条真实的 pb_type 注解，看它如何从 XML 字符串走完 link 全程变成 `CircuitModelId`。

**操作步骤**：

1. 打开 `openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml`，在 `<pb_type_annotations>` 里找到针对 `clb` 的注解（典型是一条 operating 注解，把 `fle` 的某个 lut 模式映射到物理模式，并指向 `lut4` 物理电路模型）。
2. 在 `annotate_pb_types.cpp` 里定位这条注解会经过的函数链：
   - `build_vpr_physical_pb_mode_explicit_annotation`（路径反查 → `add_pb_type_physical_mode`）
   - `build_vpr_physical_pb_type_explicit_annotation` → `pair_operating_and_physical_pb_types`（端口配对）
   - `link_vpr_pb_type_to_circuit_model_explicit_annotation` → `link_physical_pb_type_to_circuit_model`（`circuit_lib.model("lut4")` → `CircuitModelId` → `add_pb_type_circuit_model`）
3. 重新跑 4.1.4 实验 B（带 `--verbose`），在日志里搜 `lut4`，确认能看到 `Bind pb type ... to circuit model 'lut4'` 一类行。

**需要观察的现象**：同一条 XML 注解在日志里产生多行输出——先是 physical mode 标注，再是 operating↔physical 配对，最后是 circuit model 绑定。

**预期结果**：你会直观看到「一条 `pb_type_annotations` 条目 → 多次 `VprDeviceAnnotation::add_*` 落账」的一对多关系，以及「名字 → 指针/ID」的最终落实。

> 待本地验证：日志行取决于 arch；若无法运行，可通过阅读上述三个函数的注释与 `grep` 调用链完成「源码阅读型实践」。

#### 4.3.5 小练习与答案

**练习 1**：如果一个 pb_type 有两个模式，但用户没在 `pb_type_annotations` 里声明 `physical_mode_name`，link 阶段会怎样？

**参考答案**：隐式推断函数 `rec_infer_vpr_physical_pb_mode_annotation` 在 `num_modes > 1` 且未被显式标注时，会先强行把 `modes[0]` 当作 physical mode 写入，随即 `VTR_LOG_ERROR` 报「Unable to find a physical mode for a multi-mode pb_type ... Please specify in the OpenFPGA architecture」。也就是说：多模式 pb_type **必须**显式声明 physical mode。

**练习 2**：`link_physical_pb_port_to_circuit_port` 对每个 pb_type 端口做了哪三重校验？任一不满足会怎样？

**参考答案**：① 在电路模型里能找到同名端口（否则报「not found in any port of circuit model」）；② 端口宽度一致（否则报「does not match ... in size」）；③ 端口类型相容，由 `circuit_port_require_pb_port_type` 判定（否则报「type does not match」）。任一不满足都记为失败，最终导致该 pb_type 绑定电路模型失败并报错。

**练习 3**：为什么 interconnect 电路模型的「隐式推断」要等到 physical mode 标注完成之后才能跑？

**参考答案**：因为推断需要遍历「physical mode 下的 interconnect」，而「哪个 mode 是 physical mode」正是前一步刚写入 `VprDeviceAnnotation` 的。隐式推断函数 `rec_infer_vpr_pb_interconnect_circuit_model_annotation` 一开头就通过 `vpr_device_annotation.physical_mode(cur_pb_type)` 取 physical mode 再下钻子节点，所以必须排在 physical mode 标注之后。

---

### 4.4 布线标注、GSB、mux 库与 activity 文件

pb_type 之外，link 还要完成布线侧绑定、GSB 构建、mux/直连收集，以及可选的 activity 读取。这些产物共同构成 `build_fabric` 的全部输入。

#### 4.4.1 概念说明

- **布线电路模型标注**：VPR 的 rr graph 用 `RRSwitchId`（开关）和 `RRSegmentId`（线段）描述布线，但不知道每个开关/线段该用哪个电路模型。link 用 `Arch` 里 connection_block/switch_box/routing_segment 三段绑定（u3-l2）把名字翻译成 `CircuitModelId`，写入 `rr_switch_circuit_models_` / `rr_segment_circuit_models_`。
- **DeviceRRGSB**：把整个器件的通用开关块（General Switch Block）抽象出来，供 `build_routing_modules` 与 mux 库构建使用。它需要一个 OpenFPGA 自维护的「反向边映射」`RRGraphInEdges`，因为 VPR 的 rr graph 只存 fan-out 边、不存 fan-in 边。
- **MuxLibrary**：从布线结构里收集所有不同尺寸的 mux 并去重（u10-l2 详解），结果写入 `mux_lib_`。
- **TileDirect**：CLB 之间的点对点直连（arch_direct），写入 `tile_direct_`。
- **activity 文件**：描述信号翻转率（toggle rate），用于功耗/仿真。link 用它配合 `annotate_simulation_setting` 推断仿真所需参数。

#### 4.4.2 核心流程

布线标注的内部结构很清晰（三件套）：

```
annotate_rr_graph_circuit_models
├─ annotate_rr_switch_circuit_models      每个 rr_switch → circuit model
├─ annotate_rr_segment_circuit_models     每个 rr_segment → circuit model
└─ annotate_direct_circuit_models         每条 direct → arch_direct 电路模型
```

GSB 与 mux 构建则在模板里紧随其后，顺序为：合法性闸门 → 反向边映射 → `annotate_device_rr_gsb` → （可选）入边排序 → `build_device_mux_library` → `build_device_tile_direct`。

#### 4.4.3 源码精读

布线电路模型标注的编排（switch + segment + direct 三步）：

[openfpga/src/annotation/annotate_rr_graph.cpp:1277-1292](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/annotate_rr_graph.cpp#L1277-L1292) —— 三步分别绑定开关、线段、直连，全部写入 `VprDeviceAnnotation`。

模板里 GSB 构建与 mux/直连收集的一段：

[openfpga/src/base/openfpga_link_arch_template.h:115-150](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_link_arch_template.h#L115-L150) —— `is_vpr_rr_graph_supported` 闸门；`RRGraphInEdges::init` 建反向边；`annotate_device_rr_gsb` 生成 `DeviceRRGSB`；随后构建 mux 库与 tile direct。

activity 文件的可选读取（何时必需见注释）：

[openfpga/src/base/openfpga_link_arch_template.h:175-201](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_link_arch_template.h#L175-L201) —— `read_activity` 返回 `net_activity`，交给 `annotate_simulation_setting`。注释指出 activity 在「推断时钟周期数」或「FPGA-SPICE」场景下是必需的。

命令选项定义（注意 `--sort_gsb_chan_node_in_edges` 等都默认关闭）：

[openfpga/src/base/openfpga_setup_command_template.h:207-227](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L207-L227) —— `activity_file`（需字符串值）、`sort_gsb_chan_node_in_edges`、`reorder_incoming_edges`、`allow_gsb_dangling_opin`、`verbose` 共五个选项；只有 `activity_file` 带必填值。

`build_fabric` 对 `link_openfpga_arch` 的硬依赖声明：

[openfpga/src/base/openfpga_setup_command_template.h:1437-1521](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L1437-L1521) —— `link_arch_dependent_cmds` 先依赖 `read_arch_cmd_id`（L1438-L1440）；随后 `build_fabric_dependent_cmds` 又把 `link_arch_cmd_id` 列为依赖（L1518-L1521）。这就是 4.1.4 实验 A 中「注释 link 后 build_fabric 被拦下」的根因。

canonical 脚本里的真实调用：

[openfpga_flow/openfpga_shell_scripts/example_script.openfpga:11-13](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_shell_scripts/example_script.openfpga#L11-L13) —— 注释「Annotate the OpenFPGA architecture to VPR data base」；调用带 `--activity_file` 与 `--sort_gsb_chan_node_in_edges`，正落在 `vpr`/`read_openfpga_arch` 之后、`check_netlist_naming_conflict`/`build_fabric` 之前。

#### 4.4.4 代码实践

**实践目标**：对比 `--sort_gsb_chan_node_in_edges` 开关对 GSB 构建产物的影响。

**操作步骤**：

1. 复制一份任务配置，分别跑两次：一次保留 `--sort_gsb_chan_node_in_edges`，一次去掉它。
2. 对两次运行加 `--verbose`，在日志里搜 `GSB` 或 `edge` 相关行。
3. 若任务含 `write_gsb_to_xml`（依赖 link，见命令模板），可对比导出的 GSB XML 里入边的顺序。

**需要观察的现象**：开启排序时，每个 routing track 输出节点的入边会被排序（可选地「重排」`reorder_incoming_edges`）；关闭时则保留 rr graph 的原始顺序。

**预期结果**：排序与否不影响 fabric 功能，但会影响生成的 mux 模块输入引脚顺序，进而影响网表的可读性与下游 PnR 的可复现性。这也是 canonical 脚本默认开启它的原因。

> 待本地验证：若没有 `write_gsb_to_xml` 任务，可改为在 `link_arch_template` 的 `sort_device_rr_gsb_chan_node_in_edges` 调用处（L133-L141）阅读其前后条件，理解开关作用。

#### 4.4.5 小练习与答案

**练习 1**：为什么 OpenFPGA 要自己建一份 `RRGraphInEdges`（反向边映射），而不是直接用 VPR 的 rr graph？

**参考答案**：VPR 的 rr graph 出于存储与算法考虑，只保存每个节点的 fan-out（出边），不保存 fan-in（入边）。而 GSB 在构建与入边排序时需要频繁查「谁驱动了这个节点」，因此 OpenFPGA 用 `RRGraphInEdges` 额外维护一份反向边映射，作为 VPR 数据的只读补充（仍是 annotation 思路：不动 VPR，平行建表）。

**练习 2**：`--activity_file` 在哪些场景下是「必需」的？不提供会怎样？

**参考答案**：源码注释列出两种必需场景——① 用户要求时钟周期数从 FPGA 实现结果推断；② 启用了 FPGA-SPICE。不提供时 `net_activity` 为空，`annotate_simulation_setting` 会按无 activity 的路径处理；若下游（如 SPICE）确实需要 activity，会在更后面报错。

**练习 3**：`annotate_rr_graph_circuit_models` 的三步（switch/segment/direct）有先后依赖吗？

**参考答案**：三者各自独立——switch 标注只写 `rr_switch_circuit_models_`，segment 标注只写 `rr_segment_circuit_models_`，direct 标注只写 `direct_annotations_`，互不读取彼此结果，因此源码里按 switch→segment→direct 顺序调用只是组织习惯，并非数据依赖。

---

## 5. 综合实践

把本讲全部知识串起来，完成一次「link 阶段全景追踪」。

**任务**：以 `k4_N4_40nm_cc_openfpga.xml` + `and2` 基准为对象，画出一张从 XML 到 `VprDeviceAnnotation` 的完整数据流图，并用实验验证其中关键环节。

**步骤**：

1. **画静态图**。在一张图上标出三类节点：
   - 输入：`vpr` 写入的 `g_vpr_ctx.device()`（`t_pb_type*`、`rr_switch`、`rr_segment`）；`read_openfpga_arch` 写入的 `arch_`（`pb_type_annotations`、`circuit_lib`、布线三段绑定）。
   - 中间：`link_arch_template` 的 5 个关键步骤（pb_types、pb_graph、rr_graph circuit models、device_rr_gsb、mux_lib）。
   - 输出：`VprDeviceAnnotation`（及 `device_rr_gsb_`、`mux_lib_`、`tile_direct_`）。
   用箭头标注每条边对应源码里的哪个函数（如 `pair_operating_and_physical_pb_types` → `add_physical_pb_type`）。

2. **动态验证**。按 4.1.4 实验 B 跑一次带 `--verbose` 的 link，把日志里的 `Bind ...` / `Annotate ...` / `Implicitly infer ...` 行按 pb_type 归类，与你在 `k4_N4_40nm_cc_openfpga.xml` 的 `pb_type_annotations` 一一对应。

3. **断点式思考**。回答：如果 `and2` 用的是 `frame` 协议（`k4_N4_40nm_frame_openfpga.xml`），link 阶段的哪些产物会不同？（提示：configuration_protocol 不直接影响 pb_type 标注，但会影响后续 build_fabric 的存储器组织——link 主要是把 arch 绑到 VPR，配置协议差异更多在 build 阶段体现。）

**预期成果**：一张能解释「为什么 build_fabric 离了 link 就寸步难行」的全景图，以及对 `VprDeviceAnnotation` 作为「VPR 结构 ↔ OpenFPGA 电路」桥梁地位的直观体会。

## 6. 本讲小结

- `link_openfpga_arch` 是 `vpr` 与 `build_fabric` 之间的桥梁：它把 `read_openfpga_arch` 解析出的「悬空名字」在 VPR 已建好的 device 里对账，落实成指针与 `CircuitModelId`。
- `link_arch_template` 是一个 17 步的线性编排器，顺序即依赖：先 physical tile，再 pb_types、pb_graph，再布线电路模型、GSB、mux 库、tile direct，最后聚类/布局标注与（可选）activity/仿真/比特流设置。
- 链接结果几乎全部汇入 `VprDeviceAnnotation`——一个「不动 VPR、平行挂载」的中央账本，以 VPR 对象指针为键，存电路模型、physical mode、mode bits、physical pb_type、rr_switch/segment 电路模型等。
- `annotate_pb_types` 是最复杂的一步，按「physical mode → interconnect physical type → physical pb_type → circuit model → mode bits」五段推进，每段都走「显式反查 + 隐式推断 + check 闸门」套路；单模式 pb_type 可自动推断 physical mode，多模式必须显式声明。
- 命令依赖机制把 `link_openfpga_arch` 列为 `build_fabric`/`write_gsb`/`read_unique_blocks` 等命令的硬前置——缺 link，下游命令在 shell 层就被拦下，这从工程上保证了 link 产物的不可省略。
- `--activity_file`（仿真/SPICE 场景必需）、`--sort_gsb_chan_node_in_edges`（影响 GSB 入边顺序与可复现性）是两个最值得掌握的命令选项。

## 7. 下一步学习建议

- **u5-l4（VPR 标注子系统）**：本讲只聚焦 `VprDeviceAnnotation`；下一讲会系统遍历 `openfpga/src/annotation/` 全目录，讲清 `VprClusteringAnnotation`、`VprPlacementAnnotation`、`VprRoutingAnnotation`、`VprBitstreamAnnotation` 以及 `DeviceRRGSB`、`FabricTile` 等设备级标注的分工。
- **u6（Fabric 构建与 ModuleManager）**：link 的产物最终被 `build_fabric` 消费。学完 u6 你会清楚看到 `VprDeviceAnnotation::pb_type_circuit_model()` 等访问器如何驱动 grid/routing/mux 模块的生成。
- **u9-l3（Repack 内部机制）**：operating↔physical pb_type 的配对、端口旋转偏移、物理 lb rr graph 等，都会在 repack 阶段被实际使用，届时回看本讲的 `pair_operating_and_physical_pb_types` 会有更深的理解。
- **延伸阅读**：在仓库里直接打开 [openfpga/src/annotation/annotate_pb_graph.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/annotate_pb_graph.cpp) 与 [openfpga/src/annotation/annotate_rr_graph.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/annotation/annotate_rr_graph.cpp)，对照本讲的调用顺序精读这两个文件的实现细节。
