# FPGA-Verilog：fabric Verilog 网表生成

## 1. 本讲目标

在前一个单元（u6 Fabric 构建）里，我们已经把整颗 FPGA 的结构凝结成了一个内存中的模块图 `ModuleManager`。但 `ModuleManager` 是 C++ 对象，外部工具（仿真器、综合器、后端 PnR）看不懂它。本讲要解决的问题就是：**把 `ModuleManager` 翻译成一份完整、可编译的 fabric Verilog 网表集合**。

学完本讲，你应当能够：

- 说清 `write_fabric_verilog` 这条命令从 shell 选项到落盘文件的完整调用链。
- 解释为什么 fabric Verilog 要按「子模块 → 路由 → grid → tile → 顶层」这个固定顺序生成。
- 读懂唯一的模块写出器 `write_verilog_module_to_file`，理解它如何把一个模块图节点翻译成 `module ... endmodule`。
- 说出 `FabricVerilogOption` 各选项（`--explicit_port_mapping`、`--include_timing`、`--constant_undriven_inputs` 等）的真实作用。
- 理解 `NetlistManager` 如何充当「生成的文件清单」，以及它如何被用来生成汇总用的 `fabric_netlists.v`。

本讲只讲 **fabric 本身**的 Verilog 生成（不依赖任何具体设计），testbench 与 SPICE 的生成留到 u8-l2、u8-l3。

## 2. 前置知识

- **ModuleManager（u6-l1）**：OpenFPGA 在内存中的 FPGA fabric 模块图，由模块（ModuleId）、端口（ModulePortId）、子模块实例、网（ModuleNet）四要素组成。本讲的所有代码都在「读 ModuleManager、写文本」。
- **build_fabric（u6-l2 ~ u6-l4）**：`write_fabric_verilog` 的硬前置命令（依赖见 [openfpga_verilog_command_template.h:690-691](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_verilog_command_template.h#L690-L691)）。没有 `build_fabric` 建好的模块图，就没有东西可写。
- **命令注册四步法（u2-l2）**：建 `Command` 加选项 → `add_command` → `set_command_class` → 绑定执行函数并声明依赖。本讲的 `write_fabric_verilog` 就是这么注册的。
- **OpenfpgaContext（u2-l3）**：命令间数据交换的全局中枢。`write_fabric_verilog` 从中取出 `module_graph`、`mux_lib`、`device_rr_gsb` 等，把产出的文件清单写回 `verilog_netlists`。
- **fabric 与设计无关（u4-l1）**：fabric Verilog 描述的是「这颗 FPGA 长什么样」，与用户烧什么设计无关；设计相关的部分（testbench、比特流）是另外的命令。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [openfpga/src/fpga_verilog/verilog_api.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_api.cpp) | FPGA-Verilog 的总入口，`fpga_fabric_verilog()` 在这里编排整个 fabric 网表生成顺序。 |
| [openfpga/src/fpga_verilog/verilog_module_writer.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_module_writer.cpp) | 唯一的「单模块写出器」`write_verilog_module_to_file`，被所有类型的网表写函数复用。 |
| [openfpga/src/fpga_verilog/verilog_top_module.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_top_module.cpp) | 顶层模块（`fpga_top` / `fpga_core`）的写出，是单模块写出器的一个典型调用者。 |
| [openfpga/src/fpga_verilog/fabric_verilog_options.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/fabric_verilog_options.cpp) | `FabricVerilogOption` 数据模型，承载所有 fabric Verilog 选项的默认值与访问器。 |
| [openfpga/src/base/netlist_manager.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/netlist_manager.h) | `NetlistManager`：记录「生成了哪些文件、每个文件属于哪一类、每个模块落在哪个文件」。 |
| [openfpga/src/base/openfpga_verilog_template.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_verilog_template.h) | 命令执行模板 `write_fabric_verilog_template`，把 shell 选项翻译成 `FabricVerilogOption`。 |
| [openfpga/src/base/openfpga_verilog_command_template.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_verilog_command_template.h) | `write_fabric_verilog` 命令本身的注册（选项定义、类别、依赖）。 |
| [openfpga/src/fpga_verilog/verilog_auxiliary_netlists.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_auxiliary_netlists.cpp) | 汇总文件 `fabric_netlists.v` 与预处理标志文件的生成，是 `NetlistManager` 的主要消费者。 |

辅助参考：目录名常量定义在 [libs/libopenfpgautil/src/openfpga_reserved_words.h:76-79](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libopenfpgautil/src/openfpga_reserved_words.h#L76-L79)（`sub_module/`、`lb/`、`routing/`、`tile/`），文件名后缀常量定义在 [openfpga/src/fpga_verilog/verilog_constants.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_constants.h)。

## 4. 核心概念与源码讲解

### 4.1 verilog_api 入口：fpga_fabric_verilog 编排

#### 4.1.1 概念说明

`fpga_fabric_verilog()` 是 FPGA-Verilog 子系统里专门负责 **fabric 本身**（不含 testbench）的顶层函数。它的角色是一个**编排器（orchestrator）**：自己几乎不写 Verilog，而是按固定顺序调用一组 `print_verilog_*` 子函数，让每个子函数负责一类模块（子模块、路由、grid、tile、顶层），最后再调一个汇总函数生成 `fabric_netlists.v`。

源码注释把它的职责说得很清楚：生成 primitive modules（LUT/MUX/门/传输门）、routing modules（SB/CB）、logic block modules（CLB）和 FPGA 顶层模块，并强调「不要把任何 testbench 生成塞进这个函数」——因为 fabric 与具体设计无关，而 testbench 依赖具体设计（见 [verilog_api.cpp:38-58](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_api.cpp#L38-L58)）。

#### 4.1.2 核心流程

`fpga_fabric_verilog` 的执行顺序是一条**严格的自下而上流水线**，对应 u6 里 build_fabric 的构建顺序——先有子模块，父模块才能实例化它们：

```text
fpga_fabric_verilog()
 ├─ 0. 建目录：SRC/{sub_module, lb, routing, tile}/
 ├─ 1. 写预处理标志文件 fpga_defines.v（含 ENABLE_TIMING 等）
 ├─ 2. print_verilog_submodule()   ← primitives，必须最先！
 ├─ 3. print_verilog_unique/flatten_routing_modules()  ← 看 compress_routing
 ├─ 4. print_verilog_grids()       ← logical tile + physical grid
 ├─ 5. print_verilog_tiles()       ← 仅当 fabric_tile 非空
 ├─ 6. print_verilog_core_module() ← fpga_core（若存在）
 ├─ 7. print_verilog_top_module()  ← fpga_top
 └─ 8. print_verilog_fabric_include_netlist() ← 用 NetlistManager 生成 fabric_netlists.v
```

两个要点：

1. **子模块必须最先**。`print_verilog_submodule` 不仅写文件，还会**把 user-defined 电路模型对应的模块补进 ModuleManager**（见 [verilog_api.cpp:102-108](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_api.cpp#L102-L108) 的注释）。这也是 `fpga_fabric_verilog` 形参里 `module_manager` 不是 `const` 的原因。
2. **路由压缩是二选一**。`compress_routing` 为真走 `print_verilog_unique_routing_modules`（只写 unique 镜像模块），为假走 `print_verilog_flatten_routing_modules`（全坐标都写），由 `VTR_ASSERT` 保证二者互斥（见 4.1.3）。

#### 4.1.3 源码精读

入口签名——注意它吃进了一大堆 context 数据，并把 `module_manager` 与 `netlist_manager` 都作为可变引用传入（[verilog_api.cpp:59-66](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_api.cpp#L59-L66)）：

```cpp
int fpga_fabric_verilog(
  ModuleManager &module_manager, NetlistManager &netlist_manager,
  const MemoryBankShiftRegisterBanks &blwl_sr_banks,
  const CircuitLibrary &circuit_lib, const MuxLibrary &mux_lib,
  const DecoderLibrary &decoder_lib, const DeviceContext &device_ctx,
  const VprDeviceAnnotation &device_annotation,
  const DeviceRRGSB &device_rr_gsb, const FabricTile &fabric_tile,
  const ModuleNameMap &module_name_map, const FabricVerilogOption &options)
```

四个子目录在 SRC 下创建，目录名是常量字符串（[verilog_api.cpp:76-97](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_api.cpp#L76-L97)）：

```cpp
std::string submodule_dir_path = src_dir_path + std::string(DEFAULT_SUBMODULE_DIR_NAME); // "sub_module/"
std::string lb_dir_path        = src_dir_path + std::string(DEFAULT_LB_DIR_NAME);        // "lb/"
std::string rr_dir_path        = src_dir_path + std::string(DEFAULT_RR_DIR_NAME);        // "routing/"
std::string tile_dir_path      = src_dir_path + std::string(DEFAULT_TILE_DIR_NAME);      // "tile/"
```

路由生成的二选一分支（[verilog_api.cpp:114-126](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_api.cpp#L114-L126)）：

```cpp
if (true == options.compress_routing()) {
  print_verilog_unique_routing_modules(...);
} else {
  VTR_ASSERT(false == options.compress_routing());
  print_verilog_flatten_routing_modules(...);
}
```

最后三步——core + top + 汇总文件（[verilog_api.cpp:145-156](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_api.cpp#L145-L156)）。注意 `print_verilog_fabric_include_netlist` 的入参是 `NetlistManager`——它不重新枚举模块，而是消费前面各子函数登记进 `netlist_manager` 的文件清单。

收尾打印总模块数（[verilog_api.cpp:158-161](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_api.cpp#L158-L161)），这个数字来自 `module_manager.num_modules()`，可以用来和实际生成的文件做对照（见综合实践）。

#### 4.1.4 代码实践

**实践目标**：在源码层面确认「子模块最先」这一约束的来源。

**操作步骤**：

1. 打开 [openfpga/src/fpga_verilog/verilog_submodule.cpp:35-95](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_submodule.cpp#L35-L95)，阅读 `print_verilog_submodule` 的内部调用顺序。
2. 注意 [verilog_submodule.cpp:54-64](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_submodule.cpp#L54-L64) 的注释：「local decoders generation must go before the MUX generation!!! because local decoders modules will be instanciated in the MUX modules」。

**需要观察的现象**：`print_verilog_submodule` 内部也是一个有依赖的序列：essentials → arch decoders → **mux local decoders → muxes**（注意 decoder 在 mux 之前）→ luts → wires → memories → shift register banks → 可选的 user-defined template。

**预期结果**：你能用一句话解释「为什么 mux 的 local decoder 必须先于 mux 生成」——因为 mux 模块要实例化 local decoder 子模块，子模块必须先存在于 ModuleManager 中。这与 u6 讲的「子模块先于父模块」是同一条原则。

#### 4.1.5 小练习与答案

**练习 1**：`fpga_fabric_verilog` 里 `print_verilog_tiles` 被一个 `if (!fabric_tile.empty())` 包住（[verilog_api.cpp:135-143](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_api.cpp#L135-L143)）。如果不启用 tile 化（`build_fabric` 时不加 `--group_tile`），tile 目录会被创建吗？

**答案**：不会。tile 目录的 `create_directory` 同样在 `if (!fabric_tile.empty())` 内（[verilog_api.cpp:95-97](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_api.cpp#L95-L97)），且 `print_verilog_tiles` 也不会被调用。

**练习 2**：为什么 `fpga_fabric_verilog` 的 `module_manager` 形参不是 `const`，而到了 `print_verilog_grids` 等调用处却包了一层 `const_cast<const ModuleManager &>(module_manager)`（[verilog_api.cpp:129-132](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_api.cpp#L129-L132)）？

**答案**：因为 `print_verilog_submodule` 这一步可能向 ModuleManager 补充 user-defined 模块，所以入口必须可写；但 grid/routing/tile/top 这些**纯读**步骤为了表达「不再修改模块图」的意图，主动转成 `const` 引用传入，这是一种「写注释不如加 const」的防御性编程。

### 4.2 模块网表写出：write_verilog_module_to_file

#### 4.2.1 概念说明

整个 fabric Verilog 生成的真正「翻译器」只有一个函数：`write_verilog_module_to_file`。无论是 submodule、grid、routing、tile 还是 top 模块，最终都汇聚到它（见 4.2.3 的调用点统计）。它的工作是：**给定一个 `ModuleId`，按 Verilog 语法把这个模块及其内部实例化关系打印到一个已打开的文件流**。

源码开头有一条重要约定（[verilog_module_writer.cpp:1-9](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_module_writer.cpp#L1-L9)）：这些 writer 只是 ModuleManager 的「输出器（outputter）」，**不应修改模块图内容**，请用 `const` 约束。这与 4.1 里 `const_cast` 加 `const` 的做法一脉相承。

#### 4.2.2 核心流程

`write_verilog_module_to_file` 把一个模块翻译成 6 个段落，对应一个标准 Verilog 模块的结构：

```text
write_verilog_module_to_file(fp, module_manager, module_id, options)
 ├─ 1. 模块声明  print_verilog_module_declaration(...)   ← module 名 + 端口列表
 ├─ 2. 内部线网声明  find_verilog_module_local_wires(...)  ← wire 声明
 ├─ 3. （可选）用常量驱动悬空输入  constant_undriven_inputs
 ├─ 4. 局部短连接  local short connections + output short connections
 ├─ 5. 实例化所有子模块  write_verilog_instance_to_file(...)  ← 遍历 child_modules
 └─ 6. 模块结尾  print_verilog_module_end(...)   ← endmodule
```

其中第 2 步「内部线网」是难点：ModuleManager 用「源点-汇点」网模型隐式表达连接，并不显式声明 wire。writer 需要为每条 net 推断出一个 wire 名，规则是（见 [verilog_module_writer.cpp:50-60](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_module_writer.cpp#L50-L60) 的注释）：

- 若 net 的源或汇落在**本模块自己的端口**上 → 不是 local wire，直接用端口名。
- 否则 → 是 local wire，命名为 `<源模块名>_<实例号>_<源端口名>`，并把相邻 pin 合并（merge）成总线。

第 5 步实例化时，端口连接顺序固定为 `global → inout → input → output → clock`（见 [verilog_module_writer.cpp:510](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_module_writer.cpp#L510)），保证生成的网表可读且稳定。

#### 4.2.3 源码精读

核心写出函数的主体（[verilog_module_writer.cpp:581-693](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_module_writer.cpp#L581-L693)），关键段落如下。

模块声明与内部线网推断：

```cpp
// 段1：声明
print_verilog_module_declaration(fp, module_manager, module_id,
                                 options.default_net_type(), options.little_endian());
// 段2：推断并打印 local wires
std::map<std::string, std::vector<BasicPort>> local_wires =
  find_verilog_module_local_wires(module_manager, module_id);
```

悬空输入接常量（受 `constant_undriven_inputs` 选项控制，[verilog_module_writer.cpp:617-648](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_module_writer.cpp#L617-L648)）——当选项不是 `none` 时，把子模块上没有 net 驱动的输入端口接到常量 0/1，区分 `bus`（`{1'b0,1'b0,...}`）与 `bit`（逐位 blast）两种写法。

实例化所有子模块（[verilog_module_writer.cpp:671-682](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_module_writer.cpp#L671-L682)）：

```cpp
for (ModuleId child_module : module_manager.child_modules(module_id)) {
  for (size_t instance : module_manager.child_module_instances(module_id, child_module)) {
    write_verilog_instance_to_file(fp, module_manager, module_id, child_module, instance,
                                   options.explicit_port_mapping(), options.little_endian());
  }
}
```

`write_verilog_instance_to_file` 里 `explicit_port_mapping` 的作用（[verilog_module_writer.cpp:524-526](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_module_writer.cpp#L524-L526)）：开启后会输出 `.port_name(signal)` 的命名连接，关闭时是位置连接 `module inst ( sig0, sig1, ... )`。命名连接更安全（端口顺序变了也不出错），但网表更长。

**复用证据**：在 fpga_verilog 目录内 grep `write_verilog_module_to_file`，能看到它被 16 处调用，覆盖 routing（[verilog_routing.cpp:124](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_routing.cpp#L124)、[243](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_routing.cpp#L243)）、grid（[verilog_grid.cpp:119](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_grid.cpp#L119)、[238](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_grid.cpp#L238)、[351](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_grid.cpp#L351)）、tile（[verilog_tile.cpp:61](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_tile.cpp#L61)）、top（[verilog_top_module.cpp:64](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_top_module.cpp#L64)、[128](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_top_module.cpp#L128)）、mux/lut/memory/shift_register_banks 等所有类别。**理解了这一个函数，就理解了整个 fabric Verilog 的输出机制。**

#### 4.2.4 代码实践

**实践目标**：对照顶层模块写出器，看清「单模块写出器」如何被复用。

**操作步骤**：

1. 阅读 [verilog_top_module.cpp:97-148](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_top_module.cpp#L97-L148) 的 `print_verilog_top_module`。
2. 它的流程是：用 `generate_fpga_top_module_name()` 取模块名 → 经 `module_name_map` 重命名 → `find_module` → 打开文件 → 写文件头 → **`write_verilog_module_to_file(fp, module_manager, top_module, options)`** → 关文件 → 把文件登记进 NetlistManager。
3. 注意 [verilog_top_module.cpp:137-145](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_top_module.cpp#L137-L145)：顶层网表统一标记为 `NetlistManager::TOP_MODULE_NETLIST`，且文件名根据 `use_relative_path` 决定存相对名还是绝对路径。

**需要观察的现象**：`print_verilog_top_module` 自己不处理任何端口或线网，全部委托给 `write_verilog_module_to_file`；它额外做的只是「取名字、开文件、写头、登记」。

**预期结果**：你能说出 grid/routing/tile 的写出函数与 top 的写出函数**结构几乎一致**，区别只在于「模块名怎么来、文件放哪个子目录、登记成哪种 netlist 类型」。

#### 4.2.5 小练习与答案

**练习 1**：`generate_verilog_port_for_module_net`（[verilog_module_writer.cpp:61-140](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_module_writer.cpp#L61-L140)）如何区分一条 net 是 local wire 还是接到模块端口上？

**答案**：它先遍历 net 的所有源，若某个源模块就是当前模块，说明 net 接到本模块端口，返回端口名；否则遍历汇，同理；若源和汇都不在本模块端口上，则判定为 local wire，按「源模块名_实例号_源端口名」命名。

**练习 2**：当 `default_net_type` 是 `wire` 时，单 bit 且 LSB 为 0 的 local wire 会被跳过声明（[verilog_module_writer.cpp:606-609](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_module_writer.cpp#L606-L609)）。为什么？

**答案**：Verilog 默认 net 类型为 `wire` 时，未声明的标识符会自动隐式声明为单 bit wire（implicit net）。跳过这类声明能让网表更简洁；但默认 net 类型设为 `none` 时（OpenFPGA 默认），所有 wire 都必须显式声明，因此不能跳过。

### 4.3 fabric verilog 选项：FabricVerilogOption

#### 4.3.1 概念说明

`FabricVerilogOption` 是 fabric Verilog 生成的**选项数据模型**。它存在的目的（源码注释说是「intermediate data structure designed to modularize the FPGA-Verilog」，见 [openfpga_verilog_template.h:42-44](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_verilog_template.h#L42-L44)）：把 shell 命令的解析与真正的生成逻辑解耦——命令模板只负责「把选项塞进 `FabricVerilogOption`」，`fpga_fabric_verilog` 只认 `FabricVerilogOption`，两边互不依赖。这与 u7-l4 里 `BitstreamWriterOption` 的设计思路完全一致。

#### 4.3.2 核心流程

选项分三类：

**（A）输出与路径类**

| 选项 | 默认 | 作用 |
|------|------|------|
| `output_directory`（`--file`/`-f`） | 空 | SRC 目录，所有网表的根 |
| `use_relative_path` | false | 汇总文件里用相对路径 include |
| `time_stamp`（`--no_time_stamp` 反向） | true | 文件头是否打印时间戳 |
| `verbose_output` | false | 详细日志 |

**（B）Verilog 语法类**

| 选项 | 默认 | 作用 |
|------|------|------|
| `explicit_port_mapping` | false | 实例用 `.port(sig)` 命名连接（见 4.2.3） |
| `default_net_type` | `none` | Verilog `default_nettype`，可为 `none`/`wire` |
| `little_endian` | false（即大端） | 总线位序 |
| `include_timing` | false | 生成 `ENABLE_TIMING` 预处理标志，给时序标注用 |
| `constant_undriven_inputs` | `none` | 把悬空输入接常量：`none`/`bus0`/`bus1`/`bit0`/`bit1` |
| `print_user_defined_template` | false | 为 user-defined 电路模型生成模板文件 |

**（C）非命令行、自动推断类（重点）**

- `compress_routing`：**不是 `write_fabric_verilog` 的命令行选项**，而是从 `OpenfpgaContext` 的 `flow_manager().compress_routing()` 读取（见 4.3.3）。它继承自 `build_fabric --compress_routing` 时设的全局流程开关。
- `perimeter_cb` 自动接常量：若 VPR 架构启用了 perimeter connection block（边界布线），且用户没显式指定 `constant_undriven_inputs`，则自动设为 `bus0`（见 [openfpga_verilog_template.h:69-78](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_verilog_template.h#L69-L78)）——因为边界 CB 会引入大量无驱动的布线输入，不接常量会导致仿真出现 `z`。

#### 4.3.3 源码精读

构造函数里的默认值（[fabric_verilog_options.cpp:15-29](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/fabric_verilog_options.cpp#L15-L29)）：

```cpp
include_timing_ = false;
explicit_port_mapping_ = false;
compress_routing_ = false;
print_user_defined_template_ = false;
default_net_type_ = VERILOG_DEFAULT_NET_TYPE_NONE;
time_stamp_ = true;
use_relative_path_ = false;
constant_undriven_inputs_ = e_undriven_input_type::NONE;
little_endian_ = false;
```

`constant_undriven_inputs` 的枚举定义（[fabric_verilog_options.h:19-26](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/fabric_verilog_options.h#L19-L26)）：

```cpp
enum class e_undriven_input_type {
  NONE = 0, BUS0, BUS1, BIT0, BIT1, NUM_TYPES
};
```

`compress_routing` 与 `perimeter_cb` 的自动推断（[openfpga_verilog_template.h:62-78](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_verilog_template.h#L62-L78)）：

```cpp
options.set_compress_routing(openfpga_ctx.flow_manager().compress_routing());  // 来自流程状态，非命令行
...
if (g_vpr_ctx.device().arch->perimeter_cb) {
  if (FabricVerilogOption::e_undriven_input_type::NONE == options.constant_undriven_inputs()) {
    options.set_constant_undriven_inputs(FabricVerilogOption::e_undriven_input_type::BUS0);
    VTR_LOG("Automatically enable the constant_undriven_input option as perimeter connection blocks are seen...\n");
  }
}
```

命令本身的选项注册——注意 `compress_routing` **不在**这个列表里（[openfpga_verilog_command_template.h:28-81](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_verilog_command_template.h#L28-L81)），验证了它是流程级开关而非本命令选项：

```cpp
Command shell_cmd("write_fabric_verilog");
shell_cmd.add_option("file", ...);                    // -f
shell_cmd.add_option("constant_undriven_inputs", ...);
shell_cmd.add_option("explicit_port_mapping", ...);
shell_cmd.add_option("include_timing", ...);
shell_cmd.add_option("print_user_defined_template", ...);
shell_cmd.add_option("default_net_type", ...);
shell_cmd.add_option("no_time_stamp", ...);
shell_cmd.add_option("use_relative_path", ...);
shell_cmd.add_option("little_endian", ...);            // 短名 -le
shell_cmd.add_option("verbose", ...);
```

#### 4.3.4 代码实践

**实践目标**：亲手对比 `--explicit_port_mapping` 开关对生成网表的影响。

**操作步骤**：

1. 参照 [generate_fabric_example_script.openfpga:33](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_shell_scripts/generate_fabric_example_script.openfpga#L33)，跑一次带 `--explicit_port_mapping` 的 `write_fabric_verilog`。
2. 再跑一次去掉 `--explicit_port_mapping`，输出到另一个目录。
3. 各挑一个 grid 或 routing 模块文件（如 `lb/grid_*.v` 或 `routing/sb_*.v`），对比子模块实例化段。

**需要观察的现象**：开启时实例形如 `mux_tree_tapbuf mux_0 (.in(in_bus), .out(out_wire), ...)`；关闭时形如 `mux_tree_tapbuf mux_0 (in_bus, out_wire, ...)`。

**预期结果**：命名连接（开启）端口顺序变了也不会错连，网表更鲁棒但更长；位置连接（关闭）更紧凑但脆弱。OpenFPGA 的官方示例脚本默认开启它。

**说明**：本实践的运行依赖先完成 `make compile`（见 u1-l3）并跑通 `vpr → read_openfpga_arch → link_openfpga_arch → build_fabric` 前置链。若本地未编译，运行结果为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`--include_timing` 并不直接在网表里写时延数字，那它到底做了什么？

**答案**：它只是让 `print_verilog_preprocessing_flags_netlist` 在 `fpga_defines.v` 里写一行 `` `define ENABLE_TIMING 1 ``（见 [verilog_auxiliary_netlists.cpp:323-327](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_auxiliary_netlists.cpp#L323-L327)）。真正的时延标注代码在各子模块网表里用 `` `ifdef ENABLE_TIMING `` 包住，编译时由这个宏决定是否生效。

**练习 2**：用户在 `write_fabric_verilog` 命令行里加了 `--compress_routing`，会发生什么？

**答案**：命令解析阶段就报错——`write_fabric_verilog` 根本没注册 `compress_routing` 选项。是否压缩路由由 `build_fabric --compress_routing` 决定，并通过 `flow_manager` 传递给本命令。

### 4.4 NetlistManager：网表集合管家

#### 4.4.1 概念说明

fabric 有成百上千个模块，最终会落到几十个 `.v` 文件里。`NetlistManager` 就是管理「**生成了哪些文件、每个文件属于哪一类、每个模块落在哪个文件**」的管家。它的目的在头文件注释里说得很直白（[netlist_manager.h:6-9](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/netlist_manager.h#L6-L9)）：跟踪依赖、方便生成 include 文件。

它和 ModuleManager 是**正交**的两套数据：ModuleManager 管模块图拓扑，NetlistManager 管「文件 ↔ 模块」的归档。头文件的叉引图（[netlist_manager.h:11-19](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/netlist_manager.h#L11-L19)）画的就是 NetlistManager 用 ModuleId 指向 ModuleManager。

#### 4.4.2 核心流程

`NetlistManager` 的数据模型有三张表：

```text
NetlistId ──→ { 文件名, 类型(SUBMODULE/LOGIC_BLOCK/ROUTING/TILE/TOP/TESTBENCH), 内含的 ModuleId 列表 }
name_id_map_        : 文件名  → NetlistId   （查重 + 反查）
module_netlist_map_ : ModuleId → NetlistId   （一个模块只能归一个文件）
```

每个 `print_verilog_*` 写函数的固定三步：

```text
1. add_netlist(fname)            → 拿到 NetlistId（重名返回 INVALID）
2. set_netlist_type(id, 类型)     → 打上类型标签
3. （内部）add_netlist_module(id, moduleId) → 记录这个文件含哪些模块
```

最后 `print_verilog_fabric_include_netlist` 按**类型**遍历 NetlistManager，依次 `\`include` 所有文件，生成 `fabric_netlists.v`。这个分类是关键：它保证了 include 顺序是「定义 → 使用」——子模块文件一定排在 grid/top 文件之前。

#### 4.4.3 源码精读

netlist 类型枚举（[netlist_manager.h:38-46](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/netlist_manager.h#L38-L46)）：

```cpp
enum e_netlist_type {
  SUBMODULE_NETLIST, LOGIC_BLOCK_NETLIST, ROUTING_MODULE_NETLIST,
  TILE_MODULE_NETLIST, TOP_MODULE_NETLIST, TESTBENCH_NETLIST, NUM_NETLIST_TYPES
};
```

`add_netlist` 的查重逻辑（[netlist_manager.cpp:112-133](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/netlist_manager.cpp#L112-L133)）——名字已存在就返回 `INVALID()`，因此各写函数拿到 id 后通常会 `VTR_ASSERT(nlist_id)` 断言成功（如 [verilog_top_module.cpp:79](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_top_module.cpp#L79)）。

`add_netlist_module` 保证「一个模块只归一个文件」（[netlist_manager.cpp:142-167](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/netlist_manager.cpp#L142-L167)）：若 `module_netlist_map_` 里已存在该 ModuleId，返回 false 拒绝重复登记。

**消费端**：`print_verilog_fabric_include_netlist` 反复用 `netlists_by_type(...)` 按类型取文件清单并 include（[verilog_auxiliary_netlists.cpp:75-170](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_auxiliary_netlists.cpp#L75-L170)），顺序固定为 define → user-defined → submodule → logic block → routing → tile → top：

```cpp
for (const NetlistId& nlist_id :
     netlist_manager.netlists_by_type(NetlistManager::SUBMODULE_NETLIST)) {
  print_verilog_include_netlist(fp, netlist_manager.netlist_name(nlist_id));
}
```

**登记端**：各子写函数里随处可见同样的「add_netlist + set_netlist_type」模式，例如 shift register banks（[verilog_shift_register_banks.cpp:80-85](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_shift_register_banks.cpp#L80-L85)）、decoders（[verilog_decoders.cpp:261-266](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_decoders.cpp#L261-L266)）、grid（[verilog_grid.cpp:128-133](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_grid.cpp#L128-L133)）。

#### 4.4.4 代码实践

**实践目标**：验证「NetlistManager 的类型分类决定了 `fabric_netlists.v` 的 include 顺序」。

**操作步骤**：

1. 打开 [verilog_auxiliary_netlists.cpp:123-166](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_auxiliary_netlists.cpp#L123-L166)，数一下它按类型 include 的次数与顺序。
2. 跑过一次 fabric Verilog 生成后，打开输出目录里的 `fabric_netlists.v`。

**需要观察的现象**：`fabric_netlists.v` 由若干 `\`include` 行组成，分组注释（`------ Include primitive module netlists -----` 等）与源码里的类型遍历一一对应；primitive（sub_module/*.v）一定排在 logic block / routing / top 之前。

**预期结果**：你能解释「为什么 include 顺序很重要」——Verilog 编译需要先看到子模块定义，才能解析父模块里的实例化；NetlistManager 的类型标签正是为了把这件事做对。

#### 4.4.5 小练习与答案

**练习 1**：如果一个模块被 `add_netlist_module` 试图登记到第二个文件，会发生什么？

**答案**：返回 false 拒绝（[netlist_manager.cpp:156-160](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/netlist_manager.cpp#L156-L160)）。设计上每个模块的定义只应出现在一个文件里，这是防止重复定义的护栏。

**练习 2**：`fabric_netlists.v` 这个文件名从哪里来？

**答案**：常量 `FABRIC_INCLUDE_VERILOG_NETLIST_FILE_NAME = "fabric_netlists.v"`，定义在 [verilog_constants.h:14-15](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_constants.h#L14-L15)，在 [verilog_auxiliary_netlists.cpp:86-87](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_auxiliary_netlists.cpp#L86-L87) 拼成输出路径。

## 5. 综合实践

**任务**：跑通一次 fabric Verilog 生成，然后把「内存模块图」与「磁盘文件集合」对上账，验证二者是同一份 fabric 的两种表示。

**前置**：已完成 u1-l3 的 `make compile`，得到 `openfpga` 二进制，并能跑通 `source openfpga.sh`。

**步骤**：

1. 运行一个现成任务生成 fabric Verilog（沿用 u1-l4 的方式）：

   ```bash
   source openfpga.sh
   run-task basic_tests/generate_fabric
   ```

   该任务的脚本里就含本讲的核心命令（见 [generate_fabric_example_script.openfpga:33](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_shell_scripts/generate_fabric_example_script.openfpga#L33)）：

   ```text
   write_fabric_verilog --file ${OPENFPGA_VERILOG_OUTPUT_DIR}/SRC \
     --explicit_port_mapping --include_timing --print_user_defined_template --verbose
   ```

2. 用 `goto-task` 进入最新运行目录，找到 `SRC/`，确认它有四个子目录 `sub_module/ lb/ routing/`（以及可能的 `tile/`），外加 `fpga_defines.v`、`fabric_netlists.v`、`fpga_top.v`（可能还有 `fpga_core.v`）。

3. **对账**：观察 `--verbose` 日志里 `Written N Verilog modules in total` 这一行（来自 [verilog_api.cpp:160-161](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_api.cpp#L160-L161)），记下 N。再用一条命令统计 SRC 下所有 `.v` 文件里 `module ... (` 声明的总数（示例命令，非项目原有）：

   ```bash
   grep -rh '^module ' SRC/ | wc -l
   ```

   两个数字应当相等——这正是「ModuleManager 的每个模块都被写到了文件、且没遗漏」的证据。

4. **结构验证**：打开 `fabric_netlists.v`，确认它的 `\`include` 分组顺序为 submodule → logic block → routing → tile → top，与 [verilog_auxiliary_netlists.cpp:123-166](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_auxiliary_netlists.cpp#L123-L166) 的类型遍历顺序一致。

5. **进阶**：把任务里的 `--explicit_port_mapping` 去掉重跑，diff 任意一个 `routing/sb_*.v`，观察实例化段从命名连接变成位置连接（呼应 4.2.3 与 4.3.4）。

**预期结果**：你能在一张表里填出「SRC 下每个子目录对应哪类模块、由哪个 `print_verilog_*` 生成、登记成哪种 NetlistManager 类型」。如果某个数字对不上，优先检查是否启用了 `--compress_routing`（routing 目录文件数会大幅减少，因为只写 unique 镜像）。

**说明**：步骤 1 的任务路径名以仓库当前 `openfpga_flow/tasks` 实际目录为准；若该路径不存在，可改用任一含 `write_fabric_verilog` 的任务（参考 u4-l2 的任务清单）。未在本机编译运行时，运行现象为「待本地验证」。

## 6. 本讲小结

- `write_fabric_verilog` 的真正入口是 `fpga_fabric_verilog()`，它是一个**编排器**，按 submodule → routing → grid → tile → top → include 的固定自下而上顺序调用各 `print_verilog_*`，对应 u6 的模块构建顺序。
- 所有类型的网表最终都汇聚到**唯一的单模块写出器** `write_verilog_module_to_file`，它把一个 ModuleManager 节点翻译成「声明 + 线网 + 实例化 + endmodule」六段式 Verilog。
- `FabricVerilogOption` 是选项数据模型，起到「命令解析与生成逻辑解耦」的作用；其中 `compress_routing` 来自 `flow_manager`（非命令行），`perimeter_cb` 会自动把悬空输入接 `bus0`。
- `NetlistManager` 是「文件 ↔ 模块」的归档管家，用类型标签（SUBMODULE/LOGIC_BLOCK/ROUTING/TILE/TOP）组织文件，保证最终 `fabric_netlists.v` 的 include 顺序是「定义先于使用」。
- fabric Verilog 与具体设计**无关**，是 FPGA 器件本身的网表；设计相关的 testbench、比特流是后续命令的产物。
- `write_fabric_verilog` 硬依赖 `build_fabric`，没有模块图就没有东西可写。

## 7. 下一步学习建议

- **u8-l2 Testbench 生成**：fabric 网表生成后，下一步是生成验证用的 testbench（`write_full_testbench` / `write_preconfigured_fabric_wrapper`）。注意它们复用了本讲的 `write_verilog_module_to_file`，但选项模型换成 `VerilogTestbenchOption`，且依赖具体设计（atom_ctx、bitstream）。
- **u8-l3 FPGA-SPICE**：对比 `spice_api.cpp` 与本讲的 `verilog_api.cpp`——你会发现两者结构高度相似（同样的子目录、同样的自下而上顺序），只是输出格式和依赖（SPICE 多吃 `technology_library`）不同。
- **源码延伸阅读**：想看清某一类模块怎么生成的，可按需精读 `verilog_grid.cpp`（grid，DFS 遍历 pb graph）、`verilog_routing.cpp`（SB/CB，unique vs flatten）、`verilog_submodule.cpp`（primitives 的编排顺序）。
- **回看 u6-l5**：本讲生成的 fabric Verilog 与 u6-l5 讲的 fabric key、fabric hierarchy、io_location_map 是配套产物，可结合起来理解「build_fabric 之后导出了哪些 fabric 元信息」。
