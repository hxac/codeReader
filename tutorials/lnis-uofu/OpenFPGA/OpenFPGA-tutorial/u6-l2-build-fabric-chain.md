# build_fabric 调用链

## 1. 本讲目标

`build_fabric` 是 OpenFPGA 流程里承上启下的关键命令：它吃进 `link_openfpga_arch` 已经对账好的架构与 VPR device 数据，产出一整张在内存中的 FPGA fabric 模块图（`ModuleManager`），供后续网表生成、比特流生成、SDC 约束统一消费。

学完本讲，你应当能够：

- 说清 `build_fabric` 命令从 shell 到内核的**三层调用链**，并能定位每一层所在的源码文件。
- 按**自下而上**的顺序列出 `build_device_module_graph()` 内部的各个 `build_*` 步骤，并解释为什么 memory/mux/lut 等子模块必须先于 grid/routing/top 构建。
- 识别 `build_fabric` 的全部命令行选项，重点理解 `--compress_routing`、`--duplicate_grid_pin`、`--frame_view`、`--group_tile` 等选项的作用、相互冲突与前置条件。
- 看懂顶层模块 `build_top_module` 如何在所有子模块就绪后被组装、如何挂上配置总线（configuration bus）。

本讲只讲「模块图是怎么被一步步搭起来的」，也就是**调用链与构建顺序**。至于每一类子模块（grid/routing/memory/mux/lut）内部的构建细节，留到 u6-l3；顶层配置总线在不同协议下的差异，留到 u6-l4。

## 2. 前置知识

阅读本讲前，你需要具备以下基础（均在前面讲义中建立）：

- **ModuleManager 的四要素**（u6-l1）：模块（`ModuleId`）、端口（`ModulePortId`）、子模块实例（实例号 `size_t`）、网（`ModuleNetId`）。本讲会反复出现「往 ModuleManager 里 add_module / add_child_instance / add_net」的动作，这些 API 的语义在 u6-l1 已讲过。
- **OpenfpgaContext 全局数据中枢**（u2-l3）：`build_fabric` 是一条**可写 context** 的命令，它会写入 `mutable_module_graph()`、`mutable_decoder_lib()`、`mutable_fabric_tile()`、`mutable_module_name_map()` 等分区，同时只读地读取 `arch()`、`device_rr_gsb()`、`vpr_device_annotation()` 等。
- **link_openfpga_arch 的桥梁作用**（u5-l3）：`build_fabric` 强依赖 `link_openfpga_arch`——link 阶段已经把电路模型名、pb_type 路径等悬空字符串落实成了 `CircuitModelId`、`t_pb_type*` 指针，并构建好了 `DeviceRRGSB`、`MuxLibrary`、`TileDirect`。没有这些，`build_fabric` 在 shell 层就会被依赖检查拦下。
- **自下而上（bottom-up）构造**的直觉：要实例化一个父模块，它所引用的所有子模块类型必须**已经存在**于 ModuleManager 中。这条朴素约束决定了整个调用链的顺序。

几个本讲会用到的术语：

- **GSB（General Switch Block，通用开关块）**：一个坐标点上「开关块 SB + X 向连接块 CBX + Y 向连接块 CBY」的组合，是布线资源的基本单元。
- **unique module（唯一模块）**：阵列中大量坐标的 GSB 互为镜像，只需保留一份「模板模块」即可——这就是 `--compress_routing` 压缩的对象（详见 u5-l4 的 `DeviceRRGSB`）。
- **配置总线（configuration bus / config bus）**：把成千上万个配置存储器（configurable memory）串成可编程链路的全局网络，类型由 `configuration_protocol` 决定（scan_chain / memory_bank / frame_based，详见 u3-l4）。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 角色 |
| --- | --- |
| `openfpga/src/base/openfpga_setup_command_template.h` | **命令注册层**。`add_build_fabric_command_template()` 定义 `build_fabric` 的全部选项、执行函数与命令依赖。 |
| `openfpga/src/base/openfpga_build_fabric_template.h` | **执行模板层**。`build_fabric_template<T>` 解析选项、做冲突检查、触发路由压缩，最后调用内核 `build_device_module_graph()`；并附带构建 IO location map、global port info、写出 fabric key 等收尾动作。 |
| `openfpga/src/fabric/build_device_module.cpp` | **内核编排器**。`build_device_module_graph()` 是真正的「自下而上」主函数，按固定顺序调用各 `build_*_modules`。 |
| `openfpga/src/fabric/build_top_module.cpp` | **顶层组装**。`build_top_module()` 在所有子模块就绪后实例化顶层、挂配置总线。 |

此外会旁引 `openfpga/src/fabric/` 下的 `build_essential_modules.*`、`build_decoder_modules.*`、`build_mux_modules.*`、`build_lut_modules.*`、`build_wire_modules.*`、`build_memory_modules.*`、`build_grid_modules.*`、`build_routing_modules.*`、`build_tile_modules.*` 等文件——它们各自实现一类子模块的构建，本讲只点出它们的调用顺序，不展开内部。

## 4. 核心概念与源码讲解

### 4.1 build_device_module_graph 入口：从命令到内核的三层

#### 4.1.1 概念说明

OpenFPGA 的一条用户命令（如 `build_fabric`）在代码里通常分成**三层**：

1. **命令注册层**（`*_command_template.h`）：定义命令名、选项、归属类别、执行函数、依赖。它只负责「声明」命令长什么样。
2. **执行模板层**（`openfpga_build_fabric_template.h` 里的 `build_fabric_template<T>`）：解析选项、做合法性/冲突检查、准备前置数据，然后**调用内核**。它是 shell 框架与具体算法之间的胶水。
3. **内核层**（`build_device_module.cpp` 里的 `build_device_module_graph`）：真正干活的核心算法，与 shell 框架完全解耦，只接收纯 C++ 参数。

这种分层的好处是：内核函数不依赖 `Command`/`CommandContext` 等 shell 类型，可以被单元测试或其它入口直接复用；而 shell 相关的「选项解析、冲突检查、context 读写」全部集中在模板层。

> 小贴士：u2-l2 讲过的「四步注册法」（建 Command → add_command → set_command_class → 绑定执行函数与依赖）就发生在第一层；本讲关注第二、三层。

#### 4.1.2 核心流程

`build_fabric` 命令被执行时的调用链：

```text
用户输入: build_fabric --compress_routing --verbose
        │
        ▼  shell.tpp: execute_command()  (先检查依赖 link_openfpga_arch 是否已跑)
[第一层] add_build_fabric_command_template() 已注册的执行函数
        │
        ▼
[第二层] build_fabric_template<T>(openfpga_ctx, cmd, cmd_context)
        │   1. 取出各 option id
        │   2. 选项冲突检查（group_tile vs duplicate_grid_pin 等）
        │   3. 若 --compress_routing 且尚未压缩 → compress_routing_hierarchy_template
        │   4. 读取 --load_fabric_key / --group_tile 配置文件
        │   5. 调用内核
        ▼
[第三层] build_device_module_graph(module_manager, ..., 20+ 个参数)
        │   自下而上构建所有模块（详见 4.2）
        ▼
[第二层收尾] 构建 io_location_map、fabric_global_port_info；按需写出 fabric key
```

#### 4.1.3 源码精读

**第一层：命令注册。** `add_build_fabric_command_template()` 在 `openfpga_setup_command_template.h` 中，把执行函数绑到 `build_fabric_template<T>`，并挂上依赖：

- [openfpga/src/base/openfpga_setup_command_template.h:466-469](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L466-L469)：把 `build_fabric` 命令的执行函数设为 `build_fabric_template<T>`，并把传入的 `dependent_cmds`（其中包含 `link_arch_cmd_id`）设为该命令的依赖——这就是 u2-l2 所说「build_fabric 强依赖 link_openfpga_arch」的注册源头。

**第二层：执行模板入口。** `build_fabric_template<T>` 接收三个参数：可写的 context 引用 `T&`、命令对象 `cmd`、命令上下文 `cmd_context`：

- [openfpga/src/base/openfpga_build_fabric_template.h:108-110](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L108-L110)：执行模板函数签名。注意它拿的是**可写**的 `T& openfpga_ctx`，因为 `build_fabric` 要写入 module_graph 等分区（对照 u2-l3 的 mutable 访问器与可写执行函数）。

**第三层：内核入口。** 模板层在准备好所有选项后，把 context 的各个 mutable 访问器作为参数传给内核：

- [openfpga/src/base/openfpga_build_fabric_template.h:204-217](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L204-L217)：调用 `build_device_module_graph()`。注意它传入 `openfpga_ctx.mutable_module_graph()`、`mutable_decoder_lib()`、`mutable_blwl_shift_register_banks()`、`mutable_fabric_tile()`、`mutable_module_name_map()`（这些都是写入目标），以及 `const_cast<const T&>(openfpga_ctx)` 与 `g_vpr_ctx.device()`（只读来源）。这是一条命令同时「读多个分区、写多个分区」的典型例子。

内核函数本身的签名很长，但结构清晰：

- [openfpga/src/fabric/build_device_module.cpp:36-45](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_device_module.cpp#L36-L45)：`build_device_module_graph()` 的签名。前 5 个参数是写入目标（`module_manager`、`decoder_lib`、`blwl_sr_banks`、`fabric_tile`、`module_name_map`），其余大多是只读的 context 数据和一组 `bool` 选项。函数一开始用 `vtr::ScopedStartFinishTimer timer("Build fabric module graph")` 包裹整个构建过程，日志里看到这行就是它。

#### 4.1.4 代码实践

**实践目标**：用「打断点式」的源码阅读，确认三层调用链的真实存在与参数流向。

**操作步骤**：

1. 在 `openfpga_setup_command_template.h:466` 处确认执行函数绑定。
2. 跳转到 `openfpga_build_fabric_template.h` 的 `build_fabric_template`（约 108 行），观察它如何用 `cmd.option("...")` 取出每个选项 id、用 `cmd_context.option_enable(...)` 读取布尔值。
3. 跳转到 `build_device_module.cpp:36` 的 `build_device_module_graph`，对照参数表，标出哪些参数对应「写入」、哪些对应「只读」。

**需要观察的现象**：

- 第一层只声明、不计算；第二层做选项解析与冲突检查；第三层才是算法主体。
- 第二层调用第三层时，`mutable_*()` 与 `const` 引用同时出现，印证「build_fabric 是可写命令」。

**预期结果**：你能画出一张「shell 输入 → 第一层注册 → 第二层模板 → 第三层内核」的箭头图，并在每个节点标出所在文件与行号。若无法运行调试器，本实践为纯源码阅读型，**待本地验证**你实际跟踪时的跳转路径。

#### 4.1.5 小练习与答案

**练习 1**：为什么把 `build_device_module_graph` 设计成接收纯 C++ 参数（`ModuleManager&`、`const DeviceContext&` 等），而不是直接接收 `Command`/`CommandContext`？

> **参考答案**：为了让内核算法与 shell 框架解耦。内核不依赖命令解析相关的类型，既可被 shell 模板调用，也可被单元测试或其它入口直接调用，便于复用与测试。

**练习 2**：在 [openfpga_build_fabric_template.h:204-217](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L204-L217) 中，`const_cast<const T&>(openfpga_ctx)` 这一行的目的是什么？

> **参考答案**：`openfpga_ctx` 在模板层是**可写**引用（要写入 module_graph 等），但内核函数只读取 context 的只读分区（如 `arch()`、`device_rr_gsb()`），所以用一个 `const` 引用传入以表达「内核只读 context」的契约；`const_cast` 仅用于把可写引用降级为只读引用传递，不真正修改对象。

---

### 4.2 自下而上的构建顺序：为什么子模块必须先于父模块

#### 4.2.1 概念说明

`build_device_module_graph()` 是一个**线性编排器（orchestrator）**：它本身不实现任何一类模块的细节，而是按固定顺序调用一堆 `build_*_modules()` 函数。这个顺序的核心原则只有一条——

> **要实例化一个父模块，它引用的所有子模块类型必须先存在于 ModuleManager 中。**

这就是「自下而上（bottom-up）」：先造最底层的叶子积木（门、缓冲、传输门、存储单元），再造由它们组合成的中间模块（mux、lut、memory、grid、routing），最后造把它们全部实例化进去的顶层模块（top）。

这条原则源自 ModuleManager 的实例化机制（u6-l1）：`add_child_module(parent, child)` 要求 `child` 这个 `ModuleId` 已经存在。如果父模块引用了一个尚未构建的子模块，构建就会失败。

#### 4.2.2 核心流程

`build_device_module_graph()` 的完整构建顺序（严格自下而上）：

```text
0. 取出配置协议指定的 sram_model（配置位用什么电路存）
1. build_constant_generator_modules  → VDD/GND 恒定电平模块
2. build_user_defined_modules        → 用户在 circuit_library 里自定义的模块
3. build_essential_modules           → 基础门级电路（inv/buf/pass_gate/wire 等）
4. build_mux_local_decoder_modules    → mux 的本地译码器（必须在 mux 之前！）
5. build_mux_modules                 → 多路选择器（引用译码器与 essential 模块）
6. build_lut_modules                 → 查找表（引用 mux）
7. build_wire_modules                → 布线线段电路
8. build_memory_modules              → 配置存储器（sram/ccff 阵列，引用 essential）
9. build_grid_modules                → 逻辑块/IO（引用 lut、mux、memory）
10. build_unique/flatten_routing_modules → 开关块/连接块（引用 mux、wire、memory）
11. build_fabric_tile + build_tile_modules → （可选）把 grid+sb+cb 打包成 tile
12. build_top_module                 → 顶层，实例化以上全部
13. rename_primitive_module_port_names → 用 lib_name 改名叶模块端口（对接标准单元）
14. init_fabric_module_name_map      → 建立「内部名 ↔ 用户名」映射表
```

为什么是这个顺序？关键是几条「引用箭头」：

- **mux 引用 essential 与 decoder**：mux 由传输门（pass_gate）、缓冲（inv_buf）和本地译码器组成，所以 `build_mux_local_decoder_modules`（第 4 步）**必须**先于 `build_mux_modules`（第 5 步）。源码注释也明确写了 "this MUST be called before multiplexer building"。
- **lut 引用 mux**：LUT 的内部就是一个查找表多路选择器，所以 `build_lut_modules`（第 6 步）在 mux 之后。
- **grid 引用 lut/mux/memory**：一个逻辑块（clb）内部实例化 LUT、MUX、配置存储器，所以 grid（第 9 步）必须在它们之后。
- **routing 引用 mux/wire/memory**：开关块和连接块的核心是布线 mux，还有布线线段和配置存储器，所以 routing（第 10 步）在它们之后。
- **top 引用 grid 与 routing**：顶层把整个阵列的 grid 与 routing 全部实例化，所以 top（第 12 步）必须最后。

一句话：**依赖箭头决定了顺序，顺序就是依赖的拓扑排序。**

#### 4.2.3 源码精读

下面把 `build_device_module_graph()` 的每一步对照真实源码列出来。函数开头先取出 sram_model 并校验：

- [openfpga/src/fabric/build_device_module.cpp:50-53](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_device_module.cpp#L50-L53)：从 `config_protocol.memory_model()` 取出配置位所用的电路模型 `sram_model`，并用 `valid_model_id` 断言它合法。这一步把 u3-l4 讲的「配置协议绑定到某个 circuit_model」落到 fabric 构建里。

接着是自下而上的构建序列：

- [openfpga/src/fabric/build_device_module.cpp:55-65](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_device_module.cpp#L55-L65)：依次构建 constant generator（VDD/GND）、user-defined 模块、essential 模块。注释强调 user-defined 模块要**先于其它步骤**注册，因为它们会被后续 primitive 模块实例化。

- [openfpga/src/fabric/build_device_module.cpp:67-74](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_device_module.cpp#L67-L74)：`build_mux_local_decoder_modules` 紧接在 mux 构建之前，注释 "this MUST be called before multiplexer building" 是顺序约束的直接证据；随后 `build_mux_modules`。

- [openfpga/src/fabric/build_device_module.cpp:76-86](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_device_module.cpp#L76-L86)：`build_lut_modules`（在 mux 之后）、`build_wire_modules`、`build_memory_modules`（传入 `config_protocol.type()` 与 `group_config_block`）。

- [openfpga/src/fabric/build_device_module.cpp:88-98](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_device_module.cpp#L88-L98)：`build_grid_modules`——逻辑块/IO 模块，依赖前面已建好的 lut/mux/memory。若返回 `CMD_EXEC_FATAL_ERROR` 则立即向上返回。

然后是 routing 的二选一分支（与 `--compress_routing` 对应）：

- [openfpga/src/fabric/build_device_module.cpp:100-123](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_device_module.cpp#L100-L123)：先构造 `RRGraphInEdges in_edges`（u5-l3 提到的「VPR 只存出边，OpenFPGA 自维护入边」映射），再依据 `compress_routing` 走 `build_unique_routing_modules`（压缩）或 `build_flatten_routing_modules`（全展开）。这里的 `module_manager.set_group_routing(group_routing)` 是 `--group_routing` 选项的落点。

可选的 tile 化与最终的顶层：

- [openfpga/src/fabric/build_device_module.cpp:125-144](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_device_module.cpp#L125-L144)：仅当 `tile_config.is_valid()`（即用户给了 `--group_tile`）时，先 `build_fabric_tile` 生成 tile 级信息，再 `build_tile_modules` 把 grid+sb+cb 打包成可复用 tile 模块。

- [openfpga/src/fabric/build_device_module.cpp:146-161](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_device_module.cpp#L146-L161)：`build_top_module`——在所有子模块就绪后组装顶层。它接收最多参数（含 `config_protocol`、`fabric_key`、`compress_routing`、`duplicate_grid_pin` 等），是整条链的终点。

最后是两个收尾动作：

- [openfpga/src/fabric/build_device_module.cpp:169-191](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_device_module.cpp#L169-L191)：`rename_primitive_module_port_names` 把叶模块端口从架构前缀名改成标准单元的 `lib_name`（对接 foundry 工艺库），随后 `init_fabric_module_name_map` 建立「内部模块名 ↔ 用户可定制名」的映射（支持 `--name_module_using_index` 与后续 `rename_modules` 命令）。

#### 4.2.4 代码实践

**实践目标**：亲手把 `build_device_module.cpp` 里的 `build_*` 调用按顺序抄成一张表，并标注每一步「引用了哪些更底层的模块」，从而直观验证自下而上顺序的合理性。

**操作步骤**：

1. 打开 [build_device_module.cpp:55-161](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_device_module.cpp#L55-L161)。
2. 制作一张三列表格：`步骤号 | 调用的函数 | 它依赖（实例化）的更底层模块`。
3. 对 memory/mux/lut 三行，写出它们必须在 grid/routing 之前的理由。

**需要观察的现象**：

- `build_mux_local_decoder_modules` 与 `build_mux_modules` 是相邻的两步，且 decoder 在前。
- grid、routing、top 都在 memory/mux/lut/wire 之后，没有任何一个「叶子模块」出现在 grid 之后。
- 每个会失败的步骤都检查 `CMD_EXEC_FATAL_ERROR == status` 并提前返回，保证失败不会污染后续步骤。

**预期结果**：你的表格应能说明——「mux 依赖 essential+decoder，lut 依赖 mux，grid 依赖 lut+mux+memory，routing 依赖 mux+wire+memory，top 依赖 grid+routing」，因此顺序必然是 `essential → decoder → mux → lut → wire → memory → grid → routing → tile → top`。这是本讲的核心实践，纯源码阅读型，可立即完成。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `build_memory_modules`（第 8 步）挪到 `build_top_module`（第 12 步）之后会发生什么？

> **参考答案**：`build_grid_modules` 与 `build_routing_modules` 在实例化逻辑块/开关块时会引用配置存储器子模块；若 memory 尚未构建，`add_child_module` 会找不到对应的 `ModuleId`，grid/routing 构建将失败。即便侥幸过了，`build_top_module` 挂配置总线时也会因找不到可配置子模块而出错。所以 memory 必须在 grid/routing 之前。

**练习 2**：`build_mux_local_decoder_modules` 为什么 MUST 在 `build_mux_modules` 之前？

> **参考答案**：一个多路选择器模块内部会实例化它专用的本地译码器（local decoder）来把少量地址位译成 mux 的选择信号。如果译码器模块还没构建，mux 模块就无法把译码器作为子模块实例化进去。

---

### 4.3 build_fabric 选项体系

#### 4.3.1 概念说明

`build_fabric` 命令的选项可以分为四组：

| 分组 | 选项 | 作用 |
| --- | --- | --- |
| **路由压缩** | `--compress_routing` | 识别 unique GSB，压缩布线模块数量（详见 u5-l4 / u9-l5） |
| **引脚/命名** | `--duplicate_grid_pin`、`--name_module_using_index` | 复制 grid 同侧引脚 / 用索引而非坐标给模块命名 |
| **fabric key** | `--load_fabric_key`、`--write_fabric_key`、`--generate_random_fabric_key` | 读入/写出/随机打乱配置存储器布局（用于安全与可复现） |
| **聚合（group）** | `--group_tile`、`--group_config_block`、`--group_routing` | 把 grid+sb+cb 聚成 tile、把配置存储器下沉到 CLB/SB/CB、把布线资源聚进 tile |
| **其它** | `--frame_view`、`--verbose` | 只建框架不连网 / 打印详细日志 |

其中最常用、也最容易踩坑的是 `--compress_routing` 与一组聚合选项，因为它们之间存在**硬性冲突与前置条件**。

#### 4.3.2 核心流程

`build_fabric_template<T>` 在调用内核之前，会做三件与选项有关的事：

1. **冲突检查**：某些选项组合非法，直接返回 `CMD_EXEC_FATAL_ERROR`。
2. **路由压缩**：若 `--compress_routing` 且 `device_rr_gsb` 尚未压缩，则先调用 `compress_routing_hierarchy_template` 识别 unique GSB，并把流程开关写入 `FlowManager`。
3. **读配置文件**：`--load_fabric_key` 读 fabric key、`--group_tile` 读 tile 配置。

关键约束：

- `--group_tile` **与** `--duplicate_grid_pin` **冲突**，二者不能同时开。
- `--group_tile` **要求** `--compress_routing` 已生效（即 unique blocks 必须先识别出来）。
- `--duplicate_grid_pin` **要求**架构里没有任何需要合并的 tile 端口（`tile_annotations.tiles_to_merge_ports()` 必须为空）。

#### 4.3.3 源码精读

**选项注册（第一层）。** 全部选项在 `add_build_fabric_command_template()` 里逐条声明，这里摘最重要的几条：

- [openfpga/src/base/openfpga_setup_command_template.h:412-415](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L412-L415)：`--compress_routing` 选项，描述是 "Compress the number of unique routing modules by identifying the unique GSBs"。
- [openfpga/src/base/openfpga_setup_command_template.h:417-419](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L417-L419)：`--duplicate_grid_pin`，复制 grid 同侧引脚。
- [openfpga/src/base/openfpga_setup_command_template.h:436-441](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L436-L441)：`--group_tile`（需附带文件名值），把可编程块与布线块聚成 tile。

**冲突检查（第二层）。**

- [openfpga/src/base/openfpga_build_fabric_template.h:125-147](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L125-L147)：两段冲突检查。第一段：开了 `--group_tile` 就不能开 `--duplicate_grid_pin`；第二段：开了 `--duplicate_grid_pin` 就不允许架构里有待合并的 tile 端口。任一冲突都打印 `VTR_LOG_ERROR` 并返回致命错误。

**路由压缩（第二层）。**

- [openfpga/src/base/openfpga_build_fabric_template.h:149-157](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L149-L157)：若 `--compress_routing` 打开且 `device_rr_gsb().is_compressed()` 为假，则调用 `compress_routing_hierarchy_template` 识别 unique GSB，并通过 `mutable_flow_manager().set_compress_routing(true)` 把这个流程级开关写入 `FlowManager`（对照 u2-l3：FlowManager 记录跨命令的流程级开关，供下游查询）。如果 GSB 已经被压缩过（例如之前跑过 `read_unique_blocks` 预加载），同样把开关置真。
- [openfpga/src/base/openfpga_build_fabric_template.h:159-166](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L159-L166)：`--group_tile` 的前置条件——若 GSB 未压缩，直接报错返回。

`compress_routing_hierarchy_template` 本身负责识别 unique 模块并打印压缩率统计：

- [openfpga/src/base/openfpga_build_fabric_template.h:43-54](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L43-L54)：构造 `RRGraphInEdges` 后调用 `mutable_device_rr_gsb().build_unique_module(...)`——这就是 u5-l4 讲的「SB+CBX+CBY 三 unique id 相同即互为镜像」的去重入口。

**fabric key 与 tile 配置（第二层）。**

- [openfpga/src/base/openfpga_build_fabric_template.h:175-181](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L175-L181)：`--load_fabric_key` 读入预定义 fabric key。
- [openfpga/src/base/openfpga_build_fabric_template.h:190-202](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L190-L202)：`--group_tile` 读 tile 配置文件到 `TileConfig`。

**收尾动作（第二层）。** 内核返回后，模板层还会构建几个附属产物：

- [openfpga/src/base/openfpga_build_fabric_template.h:225-228](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L225-L228)：构建 IO location map（记录每个 IO 引脚在阵列里的物理坐标）。
- [openfpga/src/base/openfpga_build_fabric_template.h:248-252](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L248-L252)：构建 fabric global port info。
- [openfpga/src/base/openfpga_build_fabric_template.h:254-269](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L254-L269)：若 `--write_fabric_key`，把当前 fabric key 写到 XML。

#### 4.3.4 代码实践

**实践目标**：通过阅读源码与一次真实运行，验证 `--compress_routing` 带来的「模块数压缩」效果，并复现一个选项冲突报错。

**操作步骤**：

1. 准备：参照 u1-l4，`source openfpga.sh` 后跑通一个最小任务（如 `run-task basic_tests/full_testbench/configuration_chain`），确认环境可用。
2. 找到该任务使用的 `.openfpga` 脚本（如 `example_script.openfpga`），定位其中 `build_fabric` 那一行，观察它默认带了哪些选项（通常带 `--compress_routing --verbose`）。
3. **冲突复现**：手动写一个一行脚本 `build_fabric --group_tile tile_config.xml --duplicate_grid_pin`（前置命令照抄 example 脚本），观察终端是否打印 `Option 'group_tile' requires options 'duplicate_grid_pin' to be disabled due to a conflict!`（对应 [openfpga_build_fabric_template.h:131-136](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L131-L136)）。
4. **压缩效果观察**：在 `build_fabric` 后加 `write_fabric_hierarchy --file hier.txt`，分别对「带 `--compress_routing`」与「不带」两次运行，统计 `hier.txt` 中布线模块（名字含 `sb`/`cbx`/`cby`）的数量差异。

**需要观察的现象**：

- 冲突组合会被第二层拦下，根本不会进入内核。
- 开启 `--compress_routing` 时，终端会打印类似 `Detected N unique switch blocks from a total of M (compression rate=...)` 的统计（对应 [openfpga_build_fabric_template.h:82-91](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L82-L91)）。
- 关闭 `--compress_routing` 时，每个坐标的 SB/CB 都会生成一个独立模块，模块总数远大于开启时。

**预期结果**：你能用具体数字说明 `--compress_routing` 把布线模块数从「逐坐标」降到「逐 unique 模块」。若本地未完成编译或无法运行完整流程，把第 3、4 步标注为**待本地验证**，但第 1、2 步的源码阅读部分仍可独立完成。

#### 4.3.5 小练习与答案

**练习 1**：用户同时给 `--group_tile foo.xml` 和 `--compress_routing`，但没有事先让 GSB 被压缩（`device_rr_gsb().is_compressed()` 为假）。流程会发生什么？

> **参考答案**：第二层会先在 [L149-157](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L149-L157) 检测到 `--compress_routing` 且未压缩，于是调用 `compress_routing_hierarchy_template` 完成 unique GSB 识别，并把 FlowManager 的 `compress_routing` 置真。随后 [L159-166](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L159-L166) 的前置条件检查就会通过，`--group_tile` 得以继续。也就是说 `--compress_routing` 会被 `--group_tile` 隐式满足。

**练习 2**：`--frame_view` 选项（"Build only frame view of the fabric, nets are skipped"）在内核里如何体现？

> **参考答案**：`frame_view` 作为布尔参数一路传到 `build_top_module` 等函数。在 [build_top_module.cpp:142-152](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module.cpp#L142-L152) 可以看到 `if (false == frame_view)` 才会调用 `add_top_module_nets_memory_config_bus` 挂配置总线网——`frame_view` 为真时跳过所有连线，只保留模块骨架，用于只关心模块清单、不关心连通性的场景。

---

### 4.4 顶层模块 build_top_module 的组装

#### 4.4.1 概念说明

当所有子模块（grid、routing、tile、memory、mux、lut……）都已经就位后，`build_top_module` 负责把它们实例化到一个名为 `fpga_top` 的顶层模块下，并完成三件事：

1. **实例化全部子模块**：把阵列里每个坐标的 grid、每个 SB/CB（或 tile）作为子模块实例加到顶层。
2. **添加配置端口与配置总线**：统计顶层需要的 SRAM/shared-config 位数，添加端口，再用 `add_top_module_nets_memory_config_bus` 把所有配置存储器按 `config_protocol` 串成全局配置网络。
3. **汇总全局端口**：从子模块向上汇聚全局端口（时钟、复位等）。

本讲只看 top 的「组装骨架」，配置总线在不同协议下的细节（chain/frame/memory_bank）是 u6-l4 的主题。

#### 4.4.2 核心流程

`build_top_module` 的内部流程：

```text
1. add_module("fpga_top") + set_module_usage(MODULE_TOP)
2. 实例化子模块：
     - fabric_tile 为空 → build_top_module_fine_grained_child_instances（逐坐标实例化 grid+sb+cb）
     - fabric_tile 非空 → build_top_module_tile_child_instances（逐 tile 实例化）
3. （可选）shuffle 可配置子模块顺序   ← --generate_random_fabric_key
4. 同步移位寄存器 bank 连接            ← memory_bank shift_register 子协议
5. 统计并添加 shared/reserved SRAM 端口
6. 统计并添加 SRAM 端口（按区域/协议）
7. （非 frame_view）add_top_module_nets_memory_config_bus 挂配置总线
8. （多编程时钟时）add_top_module_nets_prog_clock
9. add_module_global_ports_from_child_modules 汇总全局端口
```

#### 4.4.3 源码精读

- [openfpga/src/fabric/build_top_module.cpp:67-73](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module.cpp#L67-L73)：用 `generate_fpga_top_module_name()` 取得顶层名（`fpga_top`），`add_module` 创建它，并把用途标为 `MODULE_TOP`（对照 u6-l1 的 12 种模块用途）。

- [openfpga/src/fabric/build_top_module.cpp:77-92](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module.cpp#L77-L92)：依据 `fabric_tile.empty()` 选择两种实例化路径——细粒度（逐坐标）或 tile 化（逐 tile）。这正是前面 `--group_tile` 是否生效的最终分叉点。

- [openfpga/src/fabric/build_top_module.cpp:98-106](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module.cpp#L98-L106)：`--generate_random_fabric_key` 打乱可配置子模块顺序（用于加密/打乱存储器物理地址），随后同步移位寄存器 bank 的详细连接。

- [openfpga/src/fabric/build_top_module.cpp:121-136](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module.cpp#L121-L136)：统计顶层每个配置区域的所需的配置位数 `top_module_num_config_bits`，并据此 `add_top_module_sram_ports` 添加 SRAM 端口。注意"after adding sub modules"——必须先实例化子模块，才能从子模块向上汇总位数。

- [openfpga/src/fabric/build_top_module.cpp:138-152](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module.cpp#L138-L152)：非 `frame_view` 时，若顶层有可配置子模块，调用 `add_top_module_nets_memory_config_bus` 挂配置总线——这是把全芯片配置存储器串成可编程网络的关键一步，具体协议差异由 `config_protocol` 决定（u6-l4 展开）。

- [openfpga/src/fabric/build_top_module.cpp:168-176](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module.cpp#L168-L176)：`add_module_global_ports_from_child_modules` 从子模块向上汇总全局端口。注释特意提醒：它必须在 `add_top_module_nets_memory_config_bus` **之后**调用，因为后者可能新增子模块——这又一次体现了「顺序即依赖」。

#### 4.4.4 代码实践

**实践目标**：在 `build_top_module.cpp` 中验证「实例化子模块 → 统计位数 → 挂配置总线 → 汇总全局端口」这个顺序的合理性。

**操作步骤**：

1. 阅读 [build_top_module.cpp:67-176](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module.cpp#L67-L176)。
2. 找到所有 "after adding sub modules" / "called after ..." 的注释，把每条注释与其调用位置对应起来。
3. 回答：为什么 `add_module_global_ports_from_child_modules` 必须在 `add_top_module_nets_memory_config_bus` 之后？

**需要观察的现象**：

- 凡是「从子模块向上汇总」的动作（统计位数、统计 shared config、汇总全局端口），全部出现在「实例化子模块」之后。
- 注释里明确写出 "called after the add_top_module_nets_memory_config_bus() because it may add some sub modules"。

**预期结果**：你能用一句话解释——「汇总类操作依赖子模块已经实例化完毕，而挂配置总线可能新增子模块，所以汇总全局端口必须排到最后」。纯源码阅读型实践，可立即完成。

#### 4.4.5 小练习与答案

**练习 1**：`build_top_module` 为什么要区分 `fabric_tile.empty()` 两种实例化路径？

> **参考答案**：若启用了 `--group_tile`，阵列被聚合成一组 tile 模块，顶层只需逐 tile 实例化（`build_top_module_tile_child_instances`），实例数大幅减少，利于层次化后端；否则顶层要逐坐标实例化所有 grid 与 SB/CB（`build_top_module_fine_grained_child_instances`）。

**练习 2**：`find_top_module_regional_num_config_bit` 为什么要在「实例化子模块之后」才能调用？

> **参考答案**：它统计的是顶层每个配置区域（config region）下属所有可配置子模块贡献的位数总和。只有子模块已经作为实例挂到顶层之后，才能遍历它们求和；实例化之前顶层没有任何子模块，无从统计。

---

## 5. 综合实践

**综合任务**：仿照 `openfpga_flow/openfpga_shell_scripts/example_script.openfpga`，画出 `build_fabric` 在「命令→模板→内核→顶层」四个层面的完整数据流图，并预测一个选项改动带来的下游影响。

具体要求：

1. **画分层调用图**：在一张图里标出
   - shell 输入 `build_fabric --compress_routing --write_fabric_key key.xml`；
   - 第一层注册（`openfpga_setup_command_template.h` 的选项与执行函数绑定）；
   - 第二层模板（`build_fabric_template` 的冲突检查、路由压缩、读 fabric key、调用内核、构建 IO map/global port、写 fabric key）；
   - 第三层内核（`build_device_module_graph` 的自下而上序列）；
   - 顶层组装（`build_top_module`）。
   每个节点标注文件名与行号区间。

2. **解释读写关系**：用两种颜色标出 `build_fabric` 对 `OpenfpgaContext` 的「写入」（module_graph、decoder_lib、fabric_tile、module_name_map、io_location_map、fabric_global_port_info）与「只读」（arch、device_rr_gsb、vpr_device_annotation、mux_lib、tile_direct、clock_arch）。

3. **预测下游影响**：回答如下场景——「把 `--compress_routing` 去掉后，ModuleManager 里的布线模块数量、后续 `write_fabric_verilog` 生成的网表数量、fabric key 中可配置子模块的排布，分别会发生什么变化？」

4. **顺序约束自查**：在图中用箭头标出至少 3 处「顺序即依赖」的约束（例如 mux-local-decoder→mux、memory→grid、实例化子模块→统计配置位数→挂配置总线）。

**验收标准**：

- 调用图的每一层都能对应到本讲给出的真实源码行号；
- 读写分区清单与 [openfpga_build_fabric_template.h:204-217](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L204-L217) 的参数一致；
- 能正确预测「去掉 `--compress_routing` 会让布线模块从 unique 数量膨胀到逐坐标数量」。若你尚未本地编译，第 3 问的量化数字标注**待本地验证**，但定性结论应能给出。

## 6. 本讲小结

- `build_fabric` 是一条**三层命令**：注册层（`openfpga_setup_command_template.h`）声明选项与依赖；模板层（`openfpga_build_fabric_template.h` 的 `build_fabric_template`）解析选项、做冲突检查、触发路由压缩、调用内核并做收尾；内核层（`build_device_module.cpp` 的 `build_device_module_graph`）才是自下而上的算法主体。
- `build_device_module_graph` 的构建顺序是 `constant → user_defined → essential → mux-local-decoder → mux → lut → wire → memory → grid → routing → tile → top`，本质是对「子模块必须先于父模块存在」这条依赖的**拓扑排序**。
- 关键顺序约束有三处可考证：mux 本地译码器必须先于 mux（源码注释明确写 MUST）、memory/mux/lut 必须先于 grid/routing、所有子模块必须先于 top。
- `--compress_routing` 在内核里体现为 `build_unique_routing_modules` 与 `build_flatten_routing_modules` 的二选一，并在模板层把流程开关写入 `FlowManager`；它也是 `--group_tile` 的隐式前置条件。
- 选项之间存在硬冲突：`--group_tile` 与 `--duplicate_grid_pin` 互斥，`--duplicate_grid_pin` 不允许有待合并的 tile 端口；冲突在模板层被拦下，不会进入内核。
- `build_top_module` 在所有子模块就绪后实例化顶层、统计配置位数、挂配置总线（`add_top_module_nets_memory_config_bus`）、汇总全局端口；凡是「从子模块向上汇总」的动作都必须排在实例化之后。

## 7. 下一步学习建议

- **u6-l3 构建各类子模块**：本讲把 `build_grid_modules`、`build_routing_modules`、`build_memory_modules`、`build_mux_modules`、`build_lut_modules`、`build_decoder_modules` 当作黑盒，只关心调用顺序。下一讲打开这些黑盒，看每一类模块如何从 circuit_library、device_rr_gsb、pb graph 实例化而来。
- **u6-l4 顶层模块与存储器配置总线**：本讲只点到 `add_top_module_nets_memory_config_bus`，下一讲展开 scan_chain / frame / memory_bank 三种协议在顶层配置总线连接上的差异，以及配置区域（config regions）的组织。
- **u9-l5 GSB 压缩与 Unique Blocks**：若你想深入 `--compress_routing` 背后的 unique block 识别算法与 `read/write_unique_blocks` 缓存机制，直接跳读 u9-l5。
- **延伸阅读**：对照 [openfpga_build_fabric_template.h:43-103](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L43-L103) 的 `compress_routing_hierarchy_template`，结合 u5-l4 的 `DeviceRRGSB`，理解压缩率统计的含义。
