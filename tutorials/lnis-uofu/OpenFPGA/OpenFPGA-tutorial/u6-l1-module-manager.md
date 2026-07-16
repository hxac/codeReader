# ModuleManager 核心数据结构

## 1. 本讲目标

本讲打开 OpenFPGA fabric 构建阶段最核心的一个数据结构——`ModuleManager`。它是一份**存在于内存中的、FPGA fabric 的「模块图」**：把整颗芯片表示成「模块（module）+ 端口（port）+ 子模块实例（instance）+ 网（net）」四要素构成的层次化网表，后续生成 Verilog/SPICE 网表、生成比特流、生成 SDC 约束，全部从它读出来。

学完本讲，你应当能够：

1. 说清 `ModuleManager` 的四个核心抽象：**模块、端口、实例、网**，以及它们各自用什么 ID 表示。
2. 理解它采用的 **SoA（结构数组）+ 强类型 ID（StrongId）** 存储范式，并解释为什么这样设计。
3. 读懂模块的 **usage 类型**（TOP/GRID/SB/CB/LUT/INTERC/CONFIG 等）与端口类型枚举的真实取值。
4. 认识「可配置子模块」的三种类型 **logical / physical / unified**，以及配置区域（config region）、IO 子模块这些「叠加在模块图之上的账本」是为什么服务的（为比特流与 GPIO 寻址）。
5. 能跟着源码解释一个 LUT 模块是如何作为子模块被实例化进 grid 模块、再用 net 连接起来的。

---

## 2. 前置知识

在进入本讲前，建议你已经建立以下认知（对应前置讲义 u2-l3）：

- **OpenfpgaContext 是命令间数据交换的唯一全局中枢**。其中就有一个成员 `module_graph_`，类型正是本讲的 `ModuleManager`（[openfpga/src/base/openfpga_context.h:234](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_context.h#L234)）。它有成对的访问器：只读的 `module_graph()` 与可写的 `mutable_module_graph()`（[openfpga/src/base/openfpga_context.h:101](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_context.h#L101) 与 [openfpga/src/base/openfpga_context.h:165](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_context.h#L165)）。`build_fabric` 命令通过 mutable 访问器把模块图填满，其它生成类命令通过 const 访问器只读消费。

补充几个本讲会用到的通用概念：

- **SoA（Structure of Arrays，结构数组）**：把对象的每个属性各存一条数组，而不是把整个对象存一条数组。`ModuleManager` 给每个属性（名字、用途、子模块列表……）各开一条 `vtr::vector`，全部以模块 ID 为下标对齐。
- **强类型 ID（StrongId）**：VPR 提供的 `vtr::StrongId<Tag>` 模板，给一个整数套上「类型标签」，让编译器拒绝不同类型 ID 互相赋值/比较。`ModuleId`、`ModulePortId`、`ModuleNetId` 都是它的实例（见 [module_manager_fwd.h:24-31](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager_fwd.h#L24-L31)）。
- **fabric**：FPGA 中「可编程的硬件底座」，由可编程逻辑块（grid）、开关盒（SB）、连接盒（CB）、配置存储器等组成。`ModuleManager` 就是这块底座的模块化表示。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [openfpga/src/fabric/module_manager.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h) | `ModuleManager` 类声明，定义全部枚举、访问器、修改器与内部 SoA 数据成员。本讲的主战场。 |
| [openfpga/src/fabric/module_manager.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp) | 上述方法的实现，能看到 SoA 是怎么一条条 `push_back`/`emplace_back` 维护的。 |
| [openfpga/src/fabric/module_manager_fwd.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager_fwd.h) | 强类型 ID 的前向声明（`ModuleId` 等）。 |
| [openfpga/src/utils/module_manager_utils.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/utils/module_manager_utils.h) | 在 `ModuleManager` 之上封装的高层便捷函数（如「把电路模型加成模块」「把配置总线连成网」）。 |
| [openfpga/src/utils/module_manager_utils.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/utils/module_manager_utils.cpp) | 上述函数实现，含 usage 类型如何由电路模型类型决定的映射。 |
| [openfpga/src/fabric/build_grid_modules.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_grid_modules.cpp) | 真实调用点：把 LUT/逻辑模块、存储器模块、pb 模块实例化进 primitive / grid 模块。 |

> 说明：本讲只讲 `ModuleManager` 这个**数据结构本身**，即「它存了什么、怎么存、怎么读写」。至于「这条 fabric 是由哪些命令、按什么顺序构建出来的」，是下一讲 u6-l2（build_fabric 调用链）的内容。

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

- **4.1** ModuleManager 是什么：FPGA fabric 的「模块图」与 SoA 存储
- **4.2** 端口与子模块实例
- **4.3** ModuleNet：用「源点-汇点」模型连接引脚
- **4.4** 可配置子模块、配置区域与 IO 子模块

### 4.1 ModuleManager 是什么：FPGA fabric 的「模块图」

#### 4.1.1 概念说明

`ModuleManager` 的定位，写在了它自己的文件头注释里：维护一份「已生成模块的清单 + 每个模块的端口表 + 模块间的父子关系」，从而方便「带显式端口映射地实例化模块、并输出模块层次」（[module_manager.h:19-32](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L19-L32)）。

换句话说，它把一颗 FPGA 抽象成四类要素：

| 要素 | 含义 | 例子 |
| --- | --- | --- |
| **模块 Module** | 一个可例化的电路单元，有唯一名字与一组端口 | 顶层 `fpga_top`、某个 LUT、某个开关盒 SB |
| **端口 Port** | 模块对外暴露的一组引脚（可多位宽） | `in[3:0]`、`cin`、`sram[0:15]` |
| **实例 Instance** | 子模块在父模块里的一次「摆放」 | grid 里例化的第 0 号 LUT |
| **网 Net** | 把若干引脚连在一起的一条线 | 把顶层输入 `a` 连到 LUT 实例的 `in[0]` |

这四个要素共同组成一张**层次化模块图**：父模块包含若干子模块实例，实例的引脚之间用网相连。后续 Verilog 生成器遍历这张图打出网表，比特流生成器遍历其中的「可配置子模块」打出配置位。

#### 4.1.2 核心流程

`ModuleManager` 的生命周期可以概括为三步：

1. **创建模块**：`add_module(name)` 分配一个新的 `ModuleId`，并为它初始化所有并行的 SoA 数组（名字、用途、子模块列表、端口表、网表……）。
2. **逐步填充**：
   - `add_port(...)` 给模块加端口；
   - `add_child_module(parent, child)` 把一个子模块例化进父模块（同一子模块可例化多次）；
   - `create_module_net(...)` + `add_module_net_source/sink(...)` 用网把引脚连起来；
   - 需要参与配置的，再用 `add_configurable_child(...)` 登记。
3. **只读消费**：fabric 构建完成后，整张模块图对后续命令只读，通过 `modules()` / `module_ports()` / `child_modules()` / `module_nets()` 等访问器遍历。

#### 4.1.3 源码精读

类的定义从 [module_manager.h:33](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L33) 开始。注意它在 public 区先定义了三组枚举（端口类型、模块用途、可配置子模块类型），再定义强类型 ID 的迭代器与区间类型，最后才到访问器/修改器。三类枚举分别回答三个问题，本讲后续会逐一展开。

**关键观察：所有数据都是「以 `ModuleId` 为下标的并行数组」。** 看 `private` 区的内部数据（[module_manager.h:560-736](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L560-L736)），模块级数据长这样：

```cpp
vtr::vector<ModuleId, ModuleId>           ids_;     // 唯一标识，决定模块总数
vtr::vector<ModuleId, std::string>        names_;   // 模块名（唯一）
vtr::vector<ModuleId, e_module_usage_type> usages_; // 模块用途
vtr::vector<ModuleId, std::vector<ModuleId>> children_;        // 子模块清单
vtr::vector<ModuleId, std::vector<size_t>>  num_child_instances_; // 每个子模块的实例数
```

（见 [module_manager.h:562-574](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L562-L574)）

这就是典型的 **SoA**：每条属性一条 `vtr::vector<ModuleId, T>`，下标对齐。要拿模块 `m` 的名字，就 `names_[m]`；要拿它的子模块清单，就 `children_[m]`。强类型 ID 保证你**写不出 `names_[somePortId]` 这种串台错误**——编译期就被拒。

> **为什么用 SoA 而不是 `struct Module { string name; ... }`？** 因为 fabric 里模块动辄上千上万个，SoA 让同类数据连续存放，缓存命中率高，而且新增一个属性只需再加一条平行数组，不必改动既有字段。

`add_module` 的实现清楚地展示了「为每个属性各 push 一格」的对齐过程（[module_manager.cpp:721-794](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L721-L794)），核心片段：

```cpp
ModuleId module = ModuleId(ids_.size());   // 新 ID = 当前模块数
ids_.push_back(module);
names_.push_back(name);
usages_.push_back(NUM_MODULE_USAGE_TYPES); // 默认“未指定用途”
parents_.emplace_back();
children_.emplace_back();
// ... 其它每条属性都 emplace_back 一格
name_id_map_[name] = module;               // 名字→ID 的快速反查
```

注意两点：一是模块名必须**唯一**，重名时 `add_module` 直接返回 `ModuleId::INVALID()`（[module_manager.cpp:722-727](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L722-L727)）；二是它同时维护了一张 `name_id_map_`（[module_manager.h:711](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L711)），供 `find_module(name)`（[module_manager.cpp:357-364](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L357-L364)）做 O(log n) 的名字反查——这在 fabric 构建里被大量使用（「我先 find_module 找到 LUT 模块，再把它加成子模块」）。

#### 4.1.4 代码实践

**实践目标**：从源码确认「模块总数由谁决定、模块名唯一性如何保证、SoA 如何对齐」。

**操作步骤**：

1. 打开 [module_manager.cpp:721](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L721) 的 `add_module`，数一数它一共 `push_back`/`emplace_back` 了多少条属性（建议对着 [module_manager.h:560-736](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L560-L736) 的私有数据成员清单核对）。
2. 打开 [module_manager.cpp:257](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L257) 的 `num_modules()`，确认它直接返回 `ids_.size()`——即「模块数 = `ids_` 数组长度」。
3. 在仓库里搜索 `add_module(` 的调用点（例如 `grep -rn "module_manager.add_module" openfpga/src/fabric/`），观察真实代码如何先取名字、再拿 ID。

**需要观察的现象**：`add_module` 末尾每次都同步更新了 `name_id_map_`；私有数据成员的数量与 `add_module` 中 push 的次数应当一一对应。

**预期结果**：你会清楚看到「每新增一个模块，所有并行数组各长一格」这一对齐关系，并对「为什么拿属性是 `names_[m]`」建立直觉。命令运行结果待本地验证（本实践为源码阅读型，无需编译运行）。

#### 4.1.5 小练习与答案

**练习 1**：`ModuleId` 是普通 `int` 吗？如果我误把一个 `ModulePortId` 当作 `ModuleId` 传给 `names_[...]`，会发生什么？

> **答案**：不是普通 int，而是 `vtr::StrongId<module_id_tag>`（[module_manager_fwd.h:24-25](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager_fwd.h#L24-L25)）。不同标签的 StrongId 互不兼容，串台会在**编译期**报错，而不是运行期出 bug。

**练习 2**：为什么 `ModuleManager` 内部要维护一张 `name_id_map_`（名字→ID）？

> **答案**：fabric 构建过程中，下游模块经常需要通过名字反查上游已建好的模块（例如先建好 LUT 模块 `lut4`，之后在 grid 里用 `find_module("lut4")` 取回它的 ID 再例化）。`name_id_map_` 把这个反查做成 O(log n)，避免每次线性扫 `names_`（对比 `find_module_port` 在 [module_manager.cpp:332-346](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L332-L346) 就是线性扫描端口的旧式实现）。

---

### 4.2 端口与子模块实例

#### 4.2.1 概念说明

光有模块还不够，还要能描述「模块有哪些引脚」与「子模块在父模块里摆了几次」。本模块讲两件事：

- **端口（Port）**：模块对外的引脚组。每个端口有一个类型（输入/输出/时钟/全局……）、一个位宽（由 `BasicPort` 描述），以及若干可选标志（是否 wire、是否 register、默认值、所在物理边等）。
- **实例（Instance）**：子模块在父模块中的一次摆放。**同一个子模块可以在同一父模块里被例化多次**（例如一个 grid 里有 N 个相同的 LUT），实例之间用一个递增的 `instance_id`（普通的 `size_t`）区分，并可各自取一个实例名。

这里要特别澄清一个常见误解：`ModuleManager` 里**没有**专门的「端口实例」或「引脚」对象。注释明确写道：「为避免巨大内存开销，我们**不**创建 pin 对象」（[module_manager.h:671-675](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L671-L675)）。引脚是用「(模块, 实例, 端口, pin 下标)」四元组隐式定位的，配合一张快速查找表（见 4.3）。

#### 4.2.2 核心流程

给模块加端口的流程：

```
add_module(name)           → 得到 ModuleId
add_port(module, BasicPort, port_type) → 得到 ModulePortId
set_port_is_wire / set_port_side / set_port_default_val / ...（可选修饰）
```

把子模块例化进父模块的流程：

```
find_module(child_name)            → 得到子模块的 ModuleId
add_child_module(parent, child)   → 子模块在 parent 下的实例数 +1
                                    （返回的“实例 id” = 增加前的实例数）
set_child_instance_name(parent, child, instance_id, name)（可选命名）
```

注意 `add_child_module` 返回实例 id 的方式很巧妙：调用方在调用**之前**先读 `num_instance(parent, child)`（此时还是旧值），然后调用 `add_child_module`，旧值正好就是新实例的编号（见真实用法 [build_grid_modules.cpp:353-356](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_grid_modules.cpp#L353-L356)）。

#### 4.2.3 源码精读

**端口类型枚举**（[module_manager.h:39-50](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L39-L50)）一共有 8 种，比「输入/输出」要细，因为它要服务于 testbench 生成（注释 L35-38 说明 testbench 生成器据此决定要驱动哪些信号）：

| 枚举值 | 含义 |
| --- | --- |
| `MODULE_GLOBAL_PORT` | 全局输入（如全局复位、全局时钟使能） |
| `MODULE_GPIN_PORT` / `MODULE_GPOUT_PORT` | 通用目的输入 / 输出（输出可作 spypad） |
| `MODULE_GPIO_PORT` | fabric 的数据 IO |
| `MODULE_INOUT_PORT` | 普通双向端口 |
| `MODULE_INPUT_PORT` / `MODULE_OUTPUT_PORT` | 普通输入 / 输出 |
| `MODULE_CLOCK_PORT` | 普通时钟端口 |

`module_port_type_str` 把这些枚举转成可读字符串，且字符串数组与枚举同下标（[module_manager.cpp:281-287](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L281-L287)）——这是 OpenFPGA 里常见的「枚举 ↔ 字符串同下标」套路。

**模块用途枚举**（[module_manager.h:56-71](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L56-L71)）有 12 种，注释说它「帮助 FPGA-SPICE 知道该给模块套哪种 VDD/VSS 端口」。这里有一个**必须澄清的准确性问题**：

> 大纲里提到「usage 类型（TOP/GRID/SB/CB/LUT/MUX/MEMORY 等）」，但**枚举里并没有独立的 `MODULE_MUX` 或 `MODULE_MEMORY`**。真实的归类是：
> - **多路选择器（MUX）** 归到 `MODULE_INTERC`（可编程互连，注释「e.g., routing multiplexer」，见 [module_manager.h:60-62](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L60-L62)）；
> - **配置存储器（SRAM/CCFF）、译码器** 归到 `MODULE_CONFIG`（[module_manager.h:58-59](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L58-L59)）。

这个映射有源码证据：`add_circuit_model_to_module_manager` 根据电路模型类型决定模块 usage——`LUT→MODULE_LUT`、`SRAM/CCFF→MODULE_CONFIG`、`IOPAD→MODULE_IO`、`WIRE/CHAN_WIRE→MODULE_INTERC`、其余→`MODULE_HARD_IP`（[module_manager_utils.cpp:146-160](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/utils/module_manager_utils.cpp#L146-L160)）。也就是说 usage 是一个比电路类型**更粗**的分类。

**加端口** `add_port`（[module_manager.cpp:797-826](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L797-L826)）同样遵循 SoA 对齐——给 `port_ids_/ports_/port_types_/port_sides_/...` 每条各 push 一格，并顺手给网查找表预留宽度。注意端口数据是**嵌套 SoA**：`vtr::vector<ModuleId, vtr::vector<ModulePortId, BasicPort>>`（[module_manager.h:642-649](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L642-L649)），即「先按模块、再按端口」两级下标。

**加子模块实例** `add_child_module`（[module_manager.cpp:934-987](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L934-L987)）的核心逻辑：在 `children_[parent]` 里找子模块是否已存在——

- **不存在**：push 进 `children_`，实例计数置 1，新实例 id = 0；
- **已存在**：实例计数 +1，新实例 id = 加之前的旧计数。

它同时反向登记父模块（`parents_[child]`），并在 `child_instance_names_` 里给新实例留一个空名字位。还有一个细节：第三个参数 `is_io_child` 默认为 `true`，即**默认把新实例也登记成 IO 子模块**（[module_manager.h:388-399](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L388-L399)）；不需要时显式传 `false`（grid 例化 pb 模块时就传了 `false`，见 [build_grid_modules.cpp:1218](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_grid_modules.cpp#L1218)，随后单独调用 `add_io_child` 自定义坐标，见 [build_grid_modules.cpp:1220](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_grid_modules.cpp#L1220)）。

#### 4.2.4 代码实践

**实践目标**：解释「一个 LUT 模块如何作为子模块被实例化进 grid 模块」。这是本讲的主实践任务之一。

**操作步骤**：在 [build_grid_modules.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_grid_modules.cpp) 中跟踪 primitive 模块的构建（一个「primitive」对应 pb 树叶子的物理电路，典型就是 LUT + FF）：

1. 第一步：先用名字反查逻辑模块（即电路模型对应的 LUT 模块）的 ID（[build_grid_modules.cpp:350-352](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_grid_modules.cpp#L350-L352)）。
2. 第二步：用「先读旧实例数 → 再 add_child_module」拿到新实例 id，把 LUT 模块加成 primitive 模块的子模块（[build_grid_modules.cpp:353-356](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_grid_modules.cpp#L353-L356)）。
3. 第三步：调用 `add_primitive_pb_type_module_nets` 把 LUT 的端口与 primitive 的端口用网连起来（[build_grid_modules.cpp:359-361](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_grid_modules.cpp#L359-L361)）。
4. 第四步（更高层）：grid 模块再把整个 primitive/pb 模块加成自己的子模块，并取一个带坐标的实例名（[build_grid_modules.cpp:1216-1228](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_grid_modules.cpp#L1216-L1228)）。

**需要观察的现象**：「LUT 模块」本身只创建一次（在电路模型→模块那一步），但在 fabric 各处被多次 `add_child_module` 例化；每次例化都得到一个递增的 `instance_id`，并可挂上独立的实例名。

**预期结果**：你能画出一条「电路模型 → LUT 模块（唯一）→ primitive 模块里例化为子模块 → grid 模块里再例化」的层次链。命令运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`add_child_module` 默认会把新实例登记为 IO 子模块（`is_io_child=true`）。为什么 grid 在例化 pb 模块时要显式传 `false`？

> **答案**：因为 grid 里 IO 子模块的坐标需要按 VPR 坐标系自定义（每个逻辑块有自己的 `z` 坐标），所以先关掉默认登记，再用带坐标参数的 `add_io_child(grid_module, pb_module, pb_instance_id, vtr::Point<int>(iz, 0))` 单独登记（[build_grid_modules.cpp:1218-1221](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_grid_modules.cpp#L1218-L1221)）。

**练习 2**：同一个 LUT 子模块在一个 grid 里被例化了 8 次，`ModuleManager` 内部 `children_` 这条数组会增长几次？

> **答案**：只增长 1 次。`children_[grid]` 里每个**子模块类型**占一项（[module_manager.cpp:954-961](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L954-L961)）；8 次例化体现在对应的 `num_child_instances_[grid][child_index]` 从 1 涨到 8，`child_instance_names_[grid][child_index]` 长到 8。实例的区分靠 `instance_id`（0..7），不靠把子模块 push 8 次。

---

### 4.3 ModuleNet：用「源点-汇点」模型连接引脚

#### 4.3.1 概念说明

有了模块、端口、实例，还差最后一块：**怎么把引脚连起来**。`ModuleManager` 不直接存「线」，而是用 **网（Net）** 来建模。每张网由若干**源点（source）**和若干**汇点（sink）**组成，每个源点/汇点都是一个引脚四元组：

> (模块 ModuleId, 实例 instance_id, 端口 ModulePortId, pin 下标 size_t)

一条网通常有 1 个源点、N 个汇点（扇出），但 API 允许多源。这种「源点-汇点」模型就是网表里 wire 的标准表达：源点驱动这条线，汇点从这条线接收。

这个模型有一个直接后果：**`ModuleManager` 不显式建 pin 对象**。引脚是被「(模块,实例,端口,pin)」四元组**隐式引用**的。为了反过来查「某个引脚在哪条网上」，它专门维护了一张 5 层嵌套的快速查找表 `net_lookup_`（[module_manager.h:718-724](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L718-L724)）。

#### 4.3.2 核心流程

连一条网的标准三步：

```
ModuleNetId net = create_module_net(parent);                    // 建网
add_module_net_source(parent, net, src_module, src_inst,
                       src_port, src_pin);                      // 加源点
add_module_net_sink(parent, net, sink_module, sink_inst,
                    sink_port, sink_pin);                       // 加汇点
```

注意：网是**挂在父模块名下**的（`create_module_net(module)`），源点/汇点的模块既可以是父模块自己（表示连到模块边界端口），也可以是它的某个子模块实例。

反向查询：「某个引脚在哪条网上」用 `module_instance_port_net(parent, child_module, child_instance, child_port, child_pin)`（[module_manager.cpp:482-508](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L482-L508)）。

#### 4.3.3 源码精读

**建网** `create_module_net`（[module_manager.cpp:1234-1261](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L1234-L1261)）只是给 `num_nets_` 加 1，并给每条源/汇并行数组各 `emplace_back` 一格。注意网的存储与模块/端口略有不同：它用 `num_nets_`（计数）+ `invalid_net_ids_`（无效集合）的组合，配合一个 `lazy_id_iterator`（[module_manager.h:97-142](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L97-L142)）做惰性迭代——迭代时跳过无效 id（`module_nets` 在 [module_manager.cpp:49-57](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L49-L57)）。

**加源点** `add_module_net_source`（[module_manager.cpp:1285-1337](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L1285-L1337)）和**加汇点** `add_module_net_sink`（[module_manager.cpp:1352-1404](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L1352-L1404)）结构对称，核心做了三件事：

1. **校验**：源/汇模块必须合法，端口必须存在于该模块，pin 下标必须在端口位宽内（一连串 `VTR_ASSERT`）。
2. **去重存储终端**：把 `(src_module, src_port)` 这对组合存进全局的 `net_terminal_storage_`（[module_manager.h:729](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L729)），已存在就复用下标——这是个内存优化，避免每条源点都重复存一份 (模块,端口)。
3. **更新快速查找表**：`net_lookup_[module][src_module][src_instance_id][src_port][src_pin] = net;`（[module_manager.cpp:1334](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L1334)），让「引脚 → 网」反查 O(1)。

这里有一个**容易被忽略的约定**：当源点/汇点的模块就是父模块自己（`src_module == module`）时，实例 id 被强制成 0（[module_manager.cpp:1320-1322](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L1320-L1322)）。也就是说，**模块自己的边界端口，被当作「它在自己的实例 0」**。`add_module` 在初始化时已经为这个「自指实例 0」预留了位置（[module_manager.cpp:789-790](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L789-L790)），`add_port` 也据此给 `net_lookup_` 预留了宽度（[module_manager.cpp:821-823](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L821-L823)）。

> 直觉化理解：在父模块 `P` 的网表里，「`P` 自己的端口」就像「实例 0 那个虚拟子模块的端口」。连顶层输入 `a` 到某个子模块实例的引脚，就是「源点 = (P, 实例0, port_a, pin)」的一条网。

`module_instance_port_net` 正是利用这张表做反查（[module_manager.cpp:506-507](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L506-L507)）：`net_lookup_[parent][child][instance][port][pin]`。

#### 4.3.4 代码实践

**实践目标**：用一个现成的便捷函数 `add_module_bus_nets`，看一次「端口对端口」的整总线连接是怎么落到 source/sink 上的。

**操作步骤**：

1. 读 [module_manager_utils.h:192-196](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/utils/module_manager_utils.h#L192-L196) 中 `add_module_bus_nets` 的签名：它接收两个「(模块, 实例, 端口)」端点，把源端口的每一位逐一连到目的端口对应位。
2. 想象一次调用：`add_module_bus_nets(mm, parent, lut, 0, lut_in_port, parent, 0, parent_in_port)`，对应到内部应当是「对每个 pin：`create_module_net` + `add_module_net_source(parent, net, parent, 0, parent_in_port, pin)` + `add_module_net_sink(parent, net, lut, 0, lut_in_port, pin)`」。
3. 在仓库里搜索 `add_module_net_source` 的真实调用（例如 [build_grid_modules.cpp:227-253](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_grid_modules.cpp#L227-L253)），观察它如何把 primitive 模块的端口与逻辑子模块的端口用网对接。

**需要观察的现象**：源点和汇点的「模块」参数一个是父模块（边界端口）、一个是子模块（实例端口），正好对应「把顶层引脚引入到子模块」的连线语义。

**预期结果**：你能用自己的话讲清「一条网 = 1 个源点引脚 + N 个汇点引脚，且源/汇用四元组定位」。命令运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `add_module_net_source` 要把 `(src_module, src_port)` 存进 `net_terminal_storage_` 而不是直接存进每条源点？

> **答案**：去重以省内存。fabric 里同一个 (模块,端口) 组合会被成千上万条源点/汇点引用，全局只存一份、各处记下标（`net_src_terminal_ids_`），见 [module_manager.cpp:1306-1316](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L1306-L1316)。读取时再用 `net_source_modules/ports` 把下标还原成 (模块,端口)（[module_manager.cpp:520-556](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L520-L556)）。

**练习 2**：`module_instance_port_net(parent, child, inst, port, pin)` 是线性扫网表来找引脚所在的网吗？

> **答案**：不是。它走的是 5 层嵌套查找表 `net_lookup_`，复杂度近似 O(1)（[module_manager.cpp:506-507](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L506-L507)）。这正是注释所说「为了快速查找 pin，我们建了一张快速查找表」（[module_manager.h:671-675](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L671-L675)）。

---

### 4.4 可配置子模块、配置区域与 IO 子模块

#### 4.4.1 概念说明

到目前为止讲的「模块 + 端口 + 实例 + 网」是一张**通用模块图**，任何数字电路都能用它表示。但 FPGA fabric 还有一类特殊需求：成千上万个**配置存储器（configurable memory）**需要按特定顺序、特定拓扑被「编程」。为此，`ModuleManager` 在通用模块图之上，又叠加了三套**专门为比特流与 GPIO 服务的「账本」**：

1. **可配置子模块（configurable children）**：记录一个模块下「有哪些子模块实例携带配置位、按什么顺序编程」。它的顺序可以与 `children_` 完全不同，因为编程顺序由配置协议决定（注释见 [module_manager.h:576-590](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L576-L590)）。
2. **配置区域（config region）**：把可配置子模块**分组**。多区域配置（multi-region）时，每个区域可独立编程，是 memory_bank / frame_based 等协议下 BL/WL 寻址的基础。
3. **IO 子模块（io children）**：记录 GPIO 的索引顺序与坐标，用于生成 IO location map（哪个用户 IO 引脚对应 fabric 哪个坐标）。

**本模块最重要的概念是 logical / physical / unified 三种可配置子模块**（[module_manager.h:73-80](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L73-L80)）：

- **logical**：表示「逻辑上的」可配置块，**可能并不真正含物理存储器**（例如一个 feedthrough/虚拟存储器块）。它承载「这块配置位对应哪个可编程资源」的逻辑位置信息，是**架构级（device）比特流生成**必需的。
- **physical**：表示「物理上的」可配置块，**真正含物理存储器**，并带坐标与所属区域，是**fabric 级比特流生成**必需的。
- **unified**：两者合一——逻辑存储器就是物理存储器，常见于最简单的情况。

为什么要分两套？因为在 memory_bank 等协议下，一个物理存储器 bank 可能合并/重组了多个逻辑存储器（logical→physical 是多对一的映射），所以需要 `logical2physical_configurable_children_` 这张映射表（[module_manager.h:598-604](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L598-L604)）。是否「合一」可以用 `unified_configurable_children(module)` 判定（[module_manager.cpp:678-696](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L678-L696)）。

#### 4.4.2 核心流程

登记一个可配置子模块（以 unified 为例）：

```
// 前置：该子模块必须已被 add_child_module 加成普通子模块
add_configurable_child(parent, child_module, child_instance,
                       e_config_child_type::UNIFIED, coord);
```

需要分组时：

```
ConfigRegionId region = add_config_region(parent);
add_configurable_child_to_region(parent, region, child_module,
                                 child_instance, config_child_id);
```

> 约束：一个可配置子模块**只能归入一个区域**；`add_configurable_child_to_region` 会检查并拒绝重复归组（[module_manager.cpp:1163-1178](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L1163-L1178)）。

需要 GPIO 索引时：

```
add_io_child(parent, child_module, child_instance, coord);
```

#### 4.4.3 源码精读

**类型枚举**定义得很克制（[module_manager.h:73-80](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L73-L80)）：

```cpp
enum class e_config_child_type { LOGICAL, PHYSICAL, UNIFIED, NUM_TYPES };
```

**`add_configurable_child`** 的分支逻辑清晰地体现了三类的区别（[module_manager.cpp:1014-1051](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L1014-L1051)）：

- `LOGICAL` 或 `UNIFIED` → push 进 `logical_configurable_children_` 及其实例列表；
- `PHYSICAL` 或 `UNIFIED` → push 进 `physical_configurable_children_`，**并额外记录坐标** `physical_configurable_child_coordinates_` 与区域占位 `physical_configurable_child_regions_`（初始化为 `ConfigRegionId::INVALID()`）；
- `UNIFIED` → 额外把自身登记进 `logical2physical_` 映射；`LOGICAL` 则给映射留空位，等待日后用 `set_logical2physical_configurable_child` 填上真实物理模块（[module_manager.cpp:1053-1064](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L1053-L1064)）。

**`add_config_region` + `add_configurable_child_to_region`**（[module_manager.cpp:1130-1183](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L1130-L1183)）：注意它的前提——子模块必须**先**作为 **physical** 可配置子模块登记过（一连串 `VTR_ASSERT`，[module_manager.cpp:1154-1161](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L1154-L1161)）。这也解释了为什么区域相关的查询（如 `region_configurable_children`）都返回 physical 子模块（[module_manager.cpp:195-212](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L195-L212)）。

**真实调用点**回到 build_grid_modules.cpp：primitive 模块把它的存储器模块加成可配置子模块时，用的是 **LOGICAL** 类型（[build_grid_modules.cpp:386-389](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_grid_modules.cpp#L386-L389)）——因为这是在「逻辑层」记录「这个 primitive 有一块配置位」，至于它物理上落到哪个存储器 bank，是更上层（top module / memory bank 构建）才决定的。这正是 logical/physical 分离的价值。

**清空接口（destructor 区）**：`clear_configurable_children` / `clear_config_region` / `clear_io_children`（[module_manager.h:517-537](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L517-L537)）被标注为「**除非你知道自己在干什么，否则不要用**」，因为它们主要用于**加载 fabric key 时重建存储器布局**——加载一份外部 fabric key 时，需要先清掉构建期默认排好的可配置子模块，再按 key 重新排（见 [module_manager.cpp:1497-1524](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L1497-L1524)）。

#### 4.4.4 代码实践

**实践目标**：从源码确认「logical / physical / unified 三类可配置子模块分别写到哪些内部数组，以及 region 与 io 的约束」。

**操作步骤**：

1. 打开 [module_manager.cpp:1014](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L1014) 的 `add_configurable_child`，画一张表：`LOGICAL` / `PHYSICAL` / `UNIFIED` 各自会 push 哪几条数组。
2. 打开 [module_manager.cpp:678](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L678) 的 `unified_configurable_children`，读它判断「是否合一」的条件（logical 与 physical 的数量与逐项 id、实例 id 都相同）。
3. 打开 [build_grid_modules.cpp:386-389](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_grid_modules.cpp#L386-L389)，确认 primitive 登记存储器时用的是 `LOGICAL`，并思考「为什么这里不用 PHYSICAL」。

**需要观察的现象**：`UNIFIED` 会同时写 logical 和 physical 两套数组；`LOGICAL` 只写 logical 那套，并给 logical→physical 映射留空位。

**预期结果**：你能解释「为什么 primitive 层用 LOGICAL，而区域划分必须基于 PHYSICAL」。命令运行结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`add_configurable_child_to_region` 要求子模块必须先作为哪种类型的可配置子模块登记过？为什么？

> **答案**：必须是 **PHYSICAL**（[module_manager.cpp:1154-1161](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.cpp#L1154-L1161)）。因为配置区域服务于物理存储器的 BL/WL 寻址与坐标分组，只有物理可配置子模块才携带坐标（`physical_configurable_child_coordinates_`）和区域归属。

**练习 2**：`clear_configurable_children` 这种「强力清空」接口主要给谁用？

> **答案**：主要给**加载 fabric key** 用（注释 [module_manager.h:518-522](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L518-L522)）。fabric key 允许用户指定存储器的外部布局/打乱顺序，加载时需要先把构建期默认排好的可配置子模块与区域清空，再按 key 重新登记。

---

## 5. 综合实践

**综合任务**：把本讲四个最小模块串起来，在源码里完整跟踪「一个 LUT 配置位如何从模块图走到可配置子模块登记」。这是本讲规格要求的主实践：阅读 `module_manager.h`，列出关键 API，并解释 LUT 模块如何作为子模块被实例化进 grid 模块。

**操作步骤**：

1. **列 API 速查表**。打开 [module_manager.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h)，按四类整理「增加 …」的修改器：
   - 增模块：`add_module`（[L348](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L348)）
   - 增端口：`add_port`（[L350](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L350)）
   - 增子模块实例：`add_child_module`（[L397](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L397)）、`set_child_instance_name`（[L401](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L401)）
   - 增网：`create_module_net`（[L462](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L462)）、`add_module_net_source`（[L474](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L474)）、`add_module_net_sink`（[L487](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L487)）
   - 登记可配置子模块：`add_configurable_child`（[L411](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L411)）、`add_config_region`（[L431](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L431)）、`add_configurable_child_to_region`（[L437](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h#L437)）

2. **跟踪 LUT 例化链**。在 [build_grid_modules.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_grid_modules.cpp) 中走一遍：
   - LUT 模块由电路模型生成（`add_circuit_model_to_module_manager`，usage 被设为 `MODULE_LUT`，见 [module_manager_utils.cpp:146-147](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/utils/module_manager_utils.cpp#L146-L147)）；
   - primitive 模块用 `find_module` 取回它（[build_grid_modules.cpp:350-352](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_grid_modules.cpp#L350-L352)）；
   - `add_child_module` 把它例化成 primitive 的子模块（[L356](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_grid_modules.cpp#L356)），并用网连端口（[L359-361](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_grid_modules.cpp#L359-L361)）；
   - primitive 再把它的存储器模块加成 **LOGICAL** 可配置子模块（[L386-389](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_grid_modules.cpp#L386-L389)）；
   - 更上层，grid 模块把 primitive/pb 模块例化为子模块并命名（[L1216-1228](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_grid_modules.cpp#L1216-L1228)）。

3. **（可选，运行型）观察真实产物**。若你已按 u1-l4 跑通过 `run-task basic_tests/full_testbench/configuration_chain`，进结果目录查看生成的 Verilog（`SRC/` 子目录）与 `fabric_hierarchy.txt`：你会看到本讲所述的「模块 + 端口 + 实例 + 网」最终被打成了层次化 Verilog 网表与层次文本。具体路径与产出以本地运行为准。

**预期结果**：你能不看讲义，用自己的话讲清「LUT 模块被加成模块 → 加端口 → 被 primitive 例化为子模块 → 用网连端口 → 其存储器被登记为可配置子模块 → primitive 再被 grid 例化」这条完整链路，并指出每一步用的是哪个 API、写到了哪条 SoA 数组。

---

## 6. 本讲小结

- `ModuleManager` 是 fabric 在内存中的**层次化模块图**，四个核心抽象是 **模块（Module）、端口（Port）、实例（Instance）、网（Net）**，分别用 `ModuleId` / `ModulePortId` / `size_t instance_id` / `ModuleNetId` 表示。
- 它采用 **SoA + 强类型 ID（StrongId）** 范式：每个属性一条 `vtr::vector`，下标对齐；不同类型 ID 编译期互不兼容，写不出串台代码。模块数由 `ids_.size()` 决定，名字唯一性由 `name_id_map_` 保证。
- 端口有 8 种类型（`e_module_port_type`）；模块用途有 12 种（`e_module_usage_type`），但**没有独立的 MUX/MEMORY**——MUX 归 `MODULE_INTERC`、存储器/译码器归 `MODULE_CONFIG`，映射依据见 `add_circuit_model_to_module_manager`。
- 网用「**源点-汇点**」模型，引脚由「(模块, 实例, 端口, pin)」四元组隐式定位（不建 pin 对象）；模块自身边界端口被视为「实例 0」；引脚→网的反查靠 5 层嵌套的 `net_lookup_`，终端 (模块,端口) 在 `net_terminal_storage_` 中去重存储。
- 可配置子模块分 **logical / physical / unified** 三类：logical 承载逻辑位置（device 比特流），physical 携带坐标与区域（fabric 比特流），unified 是两者合一；二者不一致时靠 `logical2physical_` 映射。
- 配置区域（config region）只能基于 **physical** 可配置子模块分组，每个子模块只能归一个区域；IO 子模块（io children）记录 GPIO 索引与坐标；`clear_*` 强力清空接口主要服务于 fabric key 的重新加载。

---

## 7. 下一步学习建议

- **u6-l2 build_fabric 调用链**：本讲只讲了「数据结构」，下一讲讲「谁来填它」——`build_device_module_graph` 如何按 essential → mux → lut → memory → grid → routing → tile → top 的自下而上顺序，调用本讲的 `add_module`/`add_port`/`add_child_module`/`add_module_net_*` 把整张模块图建起来。
- **u6-l3 构建各类子模块**：看 `build_grid_modules.cpp`、`build_routing_modules.cpp`、`build_memory_modules.cpp` 等如何把电路库与 device_rr_gsb 翻译成 `ModuleManager` 里的模块与实例。
- **u6-l4 顶层模块与配置总线**：看本讲的「可配置子模块 + 配置区域」如何在顶层被 `add_module_nets_memory_config_bus` 串成 scan_chain / memory_bank / frame 配置网络。
- **u7-l1 两级比特流模型**：本讲的 logical/physical 区分，正是 device 级与 fabric 级两级比特流划分的根因。
