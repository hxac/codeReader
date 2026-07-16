# OpenfpgaContext：贯穿全流程的全局数据中枢

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 **OpenfpgaContext 是什么、为什么需要它**：它是 OpenFPGA shell 中所有命令之间交换数据的「唯一枢纽」。
- 解释它**为什么只创建一次、且不允许拷贝**。
- 在 `openfpga_context.h` 中识别出与「架构、模块图、比特流、多路选择器库」对应的数据成员与访问器。
- 区分 **const 访问器（只读）** 与 **mutable 访问器（可写）** 的命名约定，并理解它们如何与命令的「执行函数类型」一一对应。
- 理解 **FlowManager** 如何记录流程状态（如是否启用了路由压缩），供下游命令查询。

本讲是 u2 单元的收尾：u2-l1 讲了程序入口，u2-l2 讲了命令分组与注册，本讲回答「这些命令之间靠什么传递数据」——答案就是这个贯穿全流程的 Context。

## 2. 前置知识

### 2.1 命令之间需要传递数据

回顾 u2-l2：一条完整的 OpenFPGA 流程由一串命令组成，例如

```text
read_openfpga_arch → link_openfpga_arch → build_fabric → build_architecture_bitstream → ...
```

这些命令各自只完成一小步，但它们**必须共享中间结果**：

- `read_openfpga_arch` 把 XML 解析出来的架构信息交给后续命令；
- `build_fabric` 既要读架构信息，又要把构建出的模块图交给后续命令；
- `build_architecture_bitstream` 又要读模块图、写出比特流……

如果每条命令都自己存一份、再互相传参，调用链会变得极其臃肿。OpenFPGA 的做法是：**设立一个全局的「数据黑板」，所有命令都往这块黑板上读/写。** 这块黑板就是 `OpenfpgaContext`。

### 2.2 C++ 里的 const 与引用（一句话回顾）

- `const T& x`：只读引用，能调用 `x` 上「不修改自身」的成员函数。
- `T& x`：可写引用，可以修改 `x` 内部数据。
- OpenFPGA 用这两种引用来**在语法层面就区分「读」和「写」**，避免误改。

### 2.3 VPR 的 Context 概念

OpenFPGA 把布局布线工作交给子模块 VPR。VPR 内部也有一个 `Context` 对象，存放器件结构（device）、时序图（timing）、聚类（clustering）、布局（placement）、布线（routing）等数据，模板代码里常以全局变量 `g_vpr_ctx` 的形式访问（如 `g_vpr_ctx.device().rr_graph`）。`OpenfpgaContext` 正是「站在 VPR Context 的肩膀上」扩展出来的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [openfpga/src/base/openfpga_context.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_context.h) | **本讲核心**。声明 `OpenfpgaContext` 类：聚合所有核心数据成员，并定义 const/mutable 两套访问器。 |
| [openfpga/src/base/openfpga_flow_manager.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_flow_manager.h) | `FlowManager` 类声明：记录流程级开关（如 `compress_routing`），供下游命令查询。 |
| [openfpga/src/base/openfpga_flow_manager.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_flow_manager.cpp) | `FlowManager` 的实现：构造函数把 `compress_routing_` 默认关闭。 |
| [openfpga/src/base/openfpga_shell.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_shell.h) | `OpenfpgaShell` 在此**唯一地持有一个 `openfpga_ctx_` 成员**，证明「只创建一次」。 |
| [openfpga/src/base/openfpga_shell.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_shell.cpp) | 把 `openfpga_ctx_` 传给每条命令执行（`execute_command(cmd_line, openfpga_ctx_)`）。 |
| [openfpga/src/base/openfpga_read_arch_template.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_read_arch_template.h) | `read_openfpga_arch` 的实现：**写 `arch_`** 的范例。 |
| [openfpga/src/base/openfpga_build_fabric_template.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h) | `build_fabric` 的实现：**读 `arch_`、写 `module_graph_`、并改写 `flow_manager_`** 的范例。 |
| [openfpga/src/base/openfpga_bitstream_template.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_template.h) | 比特流命令实现：演示 `bitstream_manager_` 与 `fabric_bitstream_` 的读写链。 |
| [openfpga/src/base/openfpga_setup_command_template.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h) | 命令注册：把执行函数绑成「mutable」或「const」两种类型。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 OpenfpgaContext 数据聚合**——它装了什么、为什么只造一个、为什么不能拷贝。
2. **4.2 访问器设计模式**——const vs mutable 两套访问器如何与命令执行函数一一对应。
3. **4.3 FlowManager 流程状态**——一个轻量的「流程开关」如何记录跨命令的状态。

### 4.1 OpenfpgaContext 数据聚合

#### 4.1.1 概念说明

`OpenfpgaContext` 是 OpenFPGA shell 环境里**命令之间交换数据的唯一中枢**。它的定位可以用源码顶部的注释一句话概括：

> If a command of OpenFPGA needs to exchange data with other commands, it must use this data structure to access/mutate.
> （任何需要与其他命令交换数据的命令，都必须通过这个数据结构来读/写。）

它有三个关键设计约束：

1. **只创建一次**：整个程序生命周期里只有一个 `OpenfpgaContext` 实例，由 `OpenfpgaShell` 持有；命令自己**绝不**再 new 一个。
2. **不可拷贝**：因为它很大（包含整张模块图、整条比特流等），拷贝会浪费内存且容易造成数据不一致。
3. **继承自 VPR 的 `Context`**：在 VPR 布局布线数据的基础上，再聚合 OpenFPGA 自己的全部核心数据。

#### 4.1.2 核心流程

可以把 `OpenfpgaContext` 想象成一块被所有命令共享的大黑板，黑板分若干分区，每个分区对应流程的一个阶段：

```text
┌──────────────────────── OpenfpgaContext（唯一实例）────────────────────────┐
│                                                                            │
│  架构区    arch_ / sim_setting_ / clock_arch_         ← read_openfpga_arch │
│            bitstream_setting_                                                │
│                                                                            │
│  VPR 标注区 vpr_device_annotation_ / vpr_netlist_annotation_  ← link_openfpga_arch
│             vpr_clustering_annotation_ / ...（继承自 VPR Context 的 device/ │
│             timing/clustering/placement/routing 经 g_vpr_ctx 访问）          │
│                                                                            │
│  设备区    device_rr_gsb_  mux_lib_  decoder_lib_  tile_direct_             │
│            blwl_sr_banks_                                                    │
│                                                                            │
│  Fabric 区 module_graph_  fabric_tile_  io_location_map_      ← build_fabric│
│            module_name_map_  fabric_global_port_info_                        │
│                                                                            │
│  比特流区  bitstream_manager_  fabric_bitstream_  ← build_*_bitstream       │
│                                                                            │
│  网表区    verilog_netlists_  spice_netlists_     ← write_fabric_verilog/.. │
│                                                                            │
│  流程区    flow_manager_                          ← 记录 compress_routing 等 │
└────────────────────────────────────────────────────────────────────────────┘
```

命令执行的统一模式是：

```text
命令执行函数(ctx, cmd, cmd_context):
    读  = ctx.<只读访问器>()      # 例如 ctx.arch()
    写  = ctx.<可写访问器>()      # 例如 ctx.mutable_module_graph()
    调用底层算法(读, 写, ...)
    返回 CMD_EXEC_SUCCESS / CMD_EXEC_FATAL_ERROR
```

`OpenfpgaShell` 在启动时创建这块黑板，并在执行每条命令时把黑板传进去——所以命令之间天然共享同一份数据，无需手动传参。

#### 4.1.3 源码精读

**① 继承关系与设计约束（头部注释）**

[openfpga_context.h:61-61](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_context.h#L61) 声明了继承关系：`OpenfpgaContext` 继承自 VPR 的 `Context`。

紧随其上的大段注释 [openfpga_context.h:35-60](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_context.h#L35-L60) 把四条使用规则写得很清楚（链接以实际文件为准）：

- 规则 1：**只在 `main()` 里创建一次**，不要在模块里复制（"Do NOT create or duplicate in your own module!"）。
- 规则 2：想清楚是要**读**还是要**写**——只读用 `const OpenfpgaContext&`，可写用 `OpenfpgaContext&`。
- 规则 3：保持 `OpenfpgaContext` 定义简短，**只放高度模块化的数据结构**。
- 规则 4：基于 VPR 的 `Context` 构建，**不允许拷贝内部成员**（因为数据量巨大）。

**② 「只创建一次」的证据**

`OpenfpgaShell` 把 context 作为**成员变量**持有，且只此一处：

[openfpga_shell.h:43-44](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_shell.h#L43-L44) 声明了 `shell_`（命令引擎）和 `openfpga_ctx_`（全局数据中枢）。由于是类成员，它在 `OpenfpgaShell` 构造时生成一次，随对象消亡而销毁——天然满足「只创建一次」。

执行命令时，这个唯一的实例被传进命令引擎：

[openfpga_shell.cpp:45-45](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_shell.cpp#L45) 中 `run_command` 直接调用 `shell_.execute_command(cmd_line, openfpga_ctx_)`。

**③ 内部数据成员总览**

数据成员集中在 [openfpga_context.h:189-252](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_context.h#L189-L252)，本讲重点关注与「架构 / 模块图 / 比特流 / 多路选择器库」对应的四个：

| 含义 | 私有成员 | 所在大致行 |
| --- | --- | --- |
| 架构（电路库、配置协议、技术库等） | `openfpga::Arch arch_;` | [L191](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_context.h#L191) |
| Fabric 模块图 | `openfpga::ModuleManager module_graph_;` | [L234](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_context.h#L234) |
| 设备级比特流 | `openfpga::BitstreamManager bitstream_manager_;` | [L242](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_context.h#L242) |
| 多路选择器库 | `openfpga::MuxLibrary mux_lib_;` | [L220](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_context.h#L220) |

> 小细节：[L217](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_context.h#L217) 的 `device_rr_gsb_{vpr_device_annotation_}` 用了**成员初始化列表**，把同类的 `vpr_device_annotation_` 作为构造参数传给 `device_rr_gsb_`——这说明成员之间也存在内部引用关系，这也是它们必须放在同一个聚合类里的原因之一。

#### 4.1.4 代码实践

**实践目标**：亲手在头文件里把「四类核心数据」与「它们的成员声明」对应起来，建立全局地图。

**操作步骤**：

1. 打开 `openfpga/src/base/openfpga_context.h`，定位到 `private:` 内部数据区（约 L189 起）。
2. 找到并抄下这四个成员的**确切声明行**：`arch_`、`module_graph_`、`bitstream_manager_`、`mux_lib_`。
3. 在仓库根目录用只读检索确认它们在命令模板里被如何使用（示例命令）：
   ```bash
   grep -rn "mutable_module_graph()\|mutable_arch()\|mutable_bitstream_manager()\|mutable_mux_lib()" openfpga/src/base/
   ```

**需要观察的现象**：你会看到这四个成员分别在不同的命令模板里被「写」（`mutable_*`），印证了「一个成员由某个命令写入、被后续命令读取」的分工。

**预期结果**：得到一张「成员 → 写入它的命令」的小表，例如：

| 成员 | 写入它的典型命令 |
| --- | --- |
| `arch_` | `read_openfpga_arch` |
| `module_graph_` | `build_fabric` |
| `bitstream_manager_` | `build_architecture_bitstream` |
| `mux_lib_` | `build_fabric`（构建过程中收集） |

> 本实践为源码阅读型，命令的精确输出取决于仓库当前状态，可对照源码理解，无需运行二进制。

#### 4.1.5 小练习与答案

**练习 1**：为什么 OpenFPGA 不让每条命令自己 `new` 一个 context，而要强制「全局唯一」？

> **参考答案**：因为命令之间是流水线关系，前一步的产物（如架构、模块图）正是后一步的输入。如果各建各的，数据就无法共享，且 context 体积巨大、重复构造既慢又易不一致。全局唯一实例 = 一块共享黑板，命令天然衔接。

**练习 2**：注释规则 4 说「不允许拷贝内部成员」，这与「只创建一次」有什么关系？

> **参考答案**：互为因果。正因为数据量大、拷贝代价高，才规定不可拷贝；而不可拷贝又决定了只能有一个实例在程序里流动，不能值传递、只能引用传递。这也是后面访问器要用「引用」返回的原因。

---

### 4.2 访问器设计模式（const 只读 vs mutable 可写）

#### 4.2.1 概念说明

`OpenfpgaContext` 用**两套名字对应的访问器**来表达「读」与「写」：

- **const 访问器**：形如 `arch()`、`module_graph()`、`bitstream_manager()`，返回 `const T&`，只能读。
- **mutable 访问器**：形如 `mutable_arch()`、`mutable_module_graph()`、`mutable_bitstream_manager()`，返回 `T&`，可以改。

这是一种把「意图写进函数名」的约定：看到 `mutable_` 前缀，就知道这条命令要**修改**黑板上的这一项；没有 `mutable_`，就是**只读**。编译器也会帮你把关——对 `const T&` 调用 `mutable_*()` 会直接编译失败。

更重要的是，这套访问器约定与 u2-l2 讲过的**命令执行函数类型**是打通的：

| 执行函数绑定 API | 命令函数签名 | 能调用的访问器 | 语义 |
| --- | --- | --- | --- |
| `set_command_execute_function` | `int f(T& ctx, ...)` | 可调 `mutable_*()` | 可写 context |
| `set_command_const_execute_function` | `int f(const T& ctx, ...)` | 只能调 const 访问器 | 只读 context |

于是，「这条命令会不会改数据」在**注册命令时就已声明**，不必读函数体也能判断。

#### 4.2.2 核心流程

```text
注册阶段（构造 Shell 时）：
  add_command("read_openfpga_arch")
    → set_command_execute_function(read_openfpga_arch_template)   # 可写：T&
  add_command("write_openfpga_arch")
    → set_command_const_execute_function(write_openfpga_arch_template)  # 只读：const T&

运行阶段：
  shell.execute_command(line, openfpga_ctx_)
    → 根据注册类型，把 openfpga_ctx_ 以 T& 或 const T& 传给执行函数
    → 执行函数内部用 const/mutable 访问器读/写黑板
```

读 vs 写的判断口诀：**「谁产出数据，谁用 `mutable_`；谁只是消费数据，谁用 const。」**

#### 4.2.3 源码精读

**① const 访问器（只读）**

[openfpga_context.h:62-125](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_context.h#L62-L125) 列出全部 const 访问器，全部返回 `const T&`。例如：

```cpp
const openfpga::Arch& arch() const { return arch_; }                       // L63
const openfpga::ModuleManager& module_graph() const { return module_graph_; } // L101
const openfpga::BitstreamManager& bitstream_manager() const { return bitstream_manager_; } // L103-L105
const openfpga::MuxLibrary& mux_lib() const { return mux_lib_; }           // L94
```

**② mutable 访问器（可写）**

[openfpga_context.h:127-187](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_context.h#L127-L187) 是与之一一对应的 mutable 访问器，返回 `T&`。例如：

```cpp
openfpga::Arch& mutable_arch() { return arch_; }                          // L128
openfpga::ModuleManager& mutable_module_graph() { return module_graph_; }  // L165
openfpga::BitstreamManager& mutable_bitstream_manager() { return bitstream_manager_; } // L167-L169
openfpga::MuxLibrary& mutable_mux_lib() { return mux_lib_; }              // L159
```

注意：mutable 访问器**不带 `const` 修饰**（它是非 const 成员函数），这正是它能返回可写引用的关键。

**③ 写数据的范例：read_openfpga_arch 写 `arch_`**

[openfpga_read_arch_template.h:46-47](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_read_arch_template.h#L46-L47) 中，`read_openfpga_arch_template` 接收的是 `T&`（可写），于是它直接给 `mutable_arch()` 赋值：

```cpp
openfpga_context.mutable_arch() = read_xml_openfpga_arch(arch_file_name.c_str());
```

紧接着 [L55](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_read_arch_template.h#L55) 用 const 访问器 `arch().circuit_lib` **读**刚写入的数据做校验——同一个函数里既写又读，体现了「写完即可被自己/后续命令读」。

**④ 读数据的范例：build_fabric 读 `arch_`、写 `module_graph_`**

[openfpga_build_fabric_template.h:204-217](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L204-L217) 调用 `build_device_module_graph(...)` 时，把多个 `mutable_*()` 作为**输出参数**（要写），同时把整份 context 以 `const_cast<const T&>(openfpga_ctx)` 作为**只读输入**——这种「同一次调用里既给可写出口、又给只读入口」的写法，正是 const/mutable 区分的典型用法：

```cpp
build_device_module_graph(
  openfpga_ctx.mutable_module_graph(),          // 输出：写模块图
  openfpga_ctx.mutable_decoder_lib(),
  openfpga_ctx.mutable_blwl_shift_register_banks(),
  openfpga_ctx.mutable_fabric_tile(),
  openfpga_ctx.mutable_module_name_map(),
  const_cast<const T&>(openfpga_ctx),           // 输入：只读地读 arch_/device_rr_gsb_ 等
  g_vpr_ctx.device(),
  ...);
```

**⑤ const 执行函数的注册：write_openfpga_arch 只读**

[openfpga_setup_command_template.h:72-73](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L72-L73) 把 `write_openfpga_arch` 绑成 **const 执行函数**——因为它只是把 `arch_` 导出成文件，绝不修改黑板：

```cpp
shell.set_command_const_execute_function(shell_cmd_id, write_openfpga_arch_template<T>);
```

对照之下，[L46-L47](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L46-L47) 的 `read_openfpga_arch` 用的是不带 const 的 `set_command_execute_function`，因为它要写 `arch_`。

#### 4.2.4 代码实践

**实践目标**：验证「const/mutable 访问器」与「const/非 const 执行函数」的对应关系，亲手发现一两条只读命令。

**操作步骤**：

1. 统计哪些命令被注册成「只读」（const 执行函数）：
   ```bash
   grep -rn "set_command_const_execute_function" openfpga/src/base/
   ```
2. 对每个命中行，向上回溯找到对应的命令名（`Command shell_cmd("xxx")`）。
3. 任选其中一个（例如 `write_openfpga_arch`），打开它的模板实现，确认函数签名是 `const T& openfpga_context`，且函数体内**只出现 `arch()` 这类 const 访问器、绝不出现 `mutable_`**。

**需要观察的现象**：只读命令的实现里，参数是 `const T&`，函数体只能调用无 `mutable_` 前缀的访问器；若有人误写 `mutable_arch()`，会编译报错。

**预期结果**：你应能列出至少 3 条「只读命令」，例如 `write_openfpga_arch`、`write_gsb`、`report_bitstream_distribution`，并确认它们的执行函数都接收 `const T&`。

> 本实践为源码阅读型，无需运行二进制；命令名以你检索到的实际结果为准。

#### 4.2.5 小练习与答案

**练习 1**：如果我想新增一条「打印当前模块图里模块数量」的命令，应该用 `set_command_execute_function` 还是 `set_command_const_execute_function`？函数签名该怎么写？

> **参考答案**：该命令只是查看、不修改，应使用 `set_command_const_execute_function`；执行函数签名写成 `int print_module_count(const T& openfpga_ctx, const Command& cmd, const CommandContext& cmd_context)`，函数体内用 `openfpga_ctx.module_graph().modules().size()` 这类 const 访问器。

**练习 2**：为什么 `mutable_module_graph()` 不能加 `const` 成员函数修饰？

> **参考答案**：因为它要返回可写引用 `ModuleManager&`，意味着它可能修改对象内部状态；C++ 规定 `const` 成员函数不能返回修改自身数据的非 const 引用。不加 `const` 修饰，正是为了让它在「只读 context（`const T&`）」上无法被调用——编译期就挡住误写。

---

### 4.3 FlowManager 流程状态

#### 4.3.1 概念说明

大部分数据通过 const/mutable 访问器流动，但有一类信息是**「流程级开关」**：它不是某条命令的产物，而是某条命令的**副作用**，需要被后续命令查询。例如「路由压缩是否已启用」——`build_fabric` 可能开了 `--compress_routing`，后续命令需要知道这一点来决定自身行为。

`FlowManager` 就是存放这类流程状态的小对象。它也挂在 `OpenfpgaContext` 上（成员 `flow_manager_`），因此同样是全局共享的。

#### 4.3.2 核心流程

```text
build_fabric --compress_routing
   ├── 真正执行压缩：device_rr_gsb_.build_unique_module(...)
   └── 副作用：mutable_flow_manager().set_compress_routing(true)   ← 记下开关

后续命令
   └── if (flow_manager().compress_routing()) { 走压缩后的分支 }
```

关键点：**「数据」放对应成员，「流程开关」放 `flow_manager_`**。两者都在 context 里，只是职责不同。

#### 4.3.3 源码精读

**① FlowManager 的职责说明**

[openfpga_flow_manager.h:10-16](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_flow_manager.h#L10-L16) 的注释写明：`FlowManager` 用于**解决功能模块之间的依赖**，为下游模块提供「它依赖的数据结构是否已构建」的标志位。

**② FlowManager 的结构**

[openfpga_flow_manager.h:17-29](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_flow_manager.h#L17-L29) 定义了这个类，目前只维护一个布尔标志 `compress_routing_`：

```cpp
class FlowManager {
 public:
  FlowManager();
  bool compress_routing() const;              // 查询
  void set_compress_routing(const bool& enabled); // 设置
 private:
  bool compress_routing_;
};
```

**③ 默认关闭**

[openfpga_flow_manager.cpp:14-17](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_flow_manager.cpp#L14-L17) 的构造函数把 `compress_routing_` 默认置为 `false`——即「除非有人显式开启，否则认为没压缩」。

**④ build_fabric 如何写入这个开关**

[openfpga_build_fabric_template.h:149-157](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L149-L157) 在真正做完压缩后，顺手把状态记进 `flow_manager_`：

```cpp
if (true == cmd_context.option_enable(cmd, opt_compress_routing) &&
    false == openfpga_ctx.device_rr_gsb().is_compressed()) {
  compress_routing_hierarchy_template<T>(openfpga_ctx, ...);
  openfpga_ctx.mutable_flow_manager().set_compress_routing(true);   // 记录开关
} else if (true == openfpga_ctx.device_rr_gsb().is_compressed()) {
  openfpga_ctx.mutable_flow_manager().set_compress_routing(true);
}
```

这段代码同时展示了 context 的两种用法：用 `mutable_device_rr_gsb()`/`device_rr_gsb()` 处理「数据」，用 `mutable_flow_manager()` 处理「流程开关」。

#### 4.3.4 代码实践

**实践目标**：跟踪一个流程开关从「被设置」到「可能被查询」的完整路径。

**操作步骤**：

1. 在仓库根目录检索谁会**读取**这个开关：
   ```bash
   grep -rn "compress_routing()" openfpga/src/
   ```
2. 区分两类命中：一类是 `flow_manager().compress_routing()`（读流程开关），另一类是 `device_rr_gsb().is_compressed()`（直接查数据本身的压缩状态）。
3. 打开 `openfpga_build_fabric_template.h` 的 L149–L157，对照理解：当用户带 `--compress_routing` 时，先压缩数据，再用 `set_compress_routing(true)` 把事实记到 `flow_manager_`。

**需要观察的现象**：你会发现流程开关与数据状态「双写」——既改了 `device_rr_gsb_` 本身，又更新了 `flow_manager_`，这样下游命令无论查哪一边都能得到一致答案。

**预期结果**：能用一句话说明 `FlowManager` 相对于普通数据成员的定位——「它是跨命令的流程级元数据，而不是流水线的产物本身」。

> 本实践为源码阅读型，命中行数以实际检索结果为准；如不确定某处语义，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：既然 `device_rr_gsb_.is_compressed()` 已经能查到压缩状态，为什么还要再在 `flow_manager_` 里存一份 `compress_routing_`？

> **参考答案**：两者侧重不同。`device_rr_gsb_.is_compressed()` 查的是「这块数据自身是否已被压缩」；`flow_manager_.compress_routing()` 表达的是「整个流程是否启用了压缩路由」这个全局意图，便于那些不直接接触 `device_rr_gsb_` 的模块快速判断流程状态。双写保证了「数据状态」与「流程意图」一致，也方便后续扩展更多流程开关。

**练习 2**：如果要新增一个流程开关（例如 `verbose_global_`），需要在哪几个地方改动？

> **参考答案**：① 在 `openfpga_flow_manager.h` 加私有成员与 const 访问器、mutator；② 在构造函数（`openfpga_flow_manager.cpp`）里给它一个默认值；③ 在产生该副作用的命令模板里用 `mutable_flow_manager().set_xxx(...)` 写入；④ 在需要查询的命令里用 `flow_manager().xxx()` 读取。无需改动 `OpenfpgaContext` 的聚合结构，因为 `flow_manager_` 已经是它的成员。

---

## 5. 综合实践：画出命令之间通过 Context 传递数据的示意图

把本讲三个模块串起来，完成下面的「数据流追踪」任务。

**任务**：以 `OpenfpgaContext` 为中心，画出下面五条命令如何读写黑板的对应分区，并标注每条命令是「只读（const）」还是「可写（mutable）」。

```text
read_openfpga_arch
  → build_fabric
    → build_architecture_bitstream
      → build_fabric_bitstream
        → write_fabric_bitstream
```

**操作步骤**：

1. 打开下列源码，逐条记录「读了哪个访问器、写了哪个访问器」：
   - `read_openfpga_arch_template`：见 [openfpga_read_arch_template.h:46-47](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_read_arch_template.h#L46-L47)（写 `arch_`）。
   - `build_fabric_template`：见 [openfpga_build_fabric_template.h:204-217](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L204-L217)（读 `arch_`、写 `module_graph_` 等）。
   - `fpga_bitstream_template`（即 `build_architecture_bitstream`）：见 [openfpga_bitstream_template.h:62-63](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_template.h#L62-L63)（写 `bitstream_manager_`）。
   - `build_fabric_bitstream_template`：见 [openfpga_bitstream_template.h:101-102](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_template.h#L101-L102)（读 `bitstream_manager_` + `module_graph_`、写 `fabric_bitstream_`）。
   - `write_fabric_bitstream_template`：见 [openfpga_bitstream_template.h:180-187](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_template.h#L180-L187)（只读 `bitstream_manager_` + `fabric_bitstream_`）。
2. 画出如下示意图（参考答案）：

```text
                   ┌────────────── OpenfpgaContext ──────────────┐
 read_openfpga_arch ──write──▶ arch_
                                │ read
 build_fabric ◀─────────────────┘──write──▶ module_graph_(, decoder_lib_, ...)
                                                │ read                 ▲
 build_architecture_bitstream ──write──▶ bitstream_manager_          │
                                                │ read                │ read
 build_fabric_bitstream ◀───────────────────────┘────read(module_graph_)──┘
                       └──write──▶ fabric_bitstream_
                                        │ read (const 命令)
 write_fabric_bitstream ◀───────────────┘──── 也 read bitstream_manager_ ──
```

3. 在图上用不同颜色/标记区分「写（mutable）」与「读（const）」。

**需要观察的现象**：上游命令写下的分区，恰好是下游命令读取的分区；`write_fabric_bitstream` 只读不写，因此它是 const 执行函数。

**预期结果**：你能指着图解释——「为什么 `build_fabric` 必须在 `build_fabric_bitstream` 之前」「为什么 `write_fabric_bitstream` 是只读命令」——而这些顺序约束，本质上就是 `OpenfpgaContext` 各分区的读写依赖。

## 6. 本讲小结

- **OpenfpgaContext 是命令间的唯一数据中枢**：所有需要跨命令交换的数据都挂在它上面，由 `OpenfpgaShell` 全局唯一地持有一个实例。
- **只创建一次、不可拷贝**：因为它聚合了模块图、比特流等大体量数据，继承自 VPR 的 `Context` 并沿用其「禁止拷贝」约定。
- **核心数据成员**：`arch_`（架构）、`module_graph_`（Fabric 模块图）、`bitstream_manager_`/`fabric_bitstream_`（两级比特流）、`mux_lib_`（多路选择器库）等，集中在头文件的 `private:` 区。
- **const vs mutable 访问器**：`arch()` 只读、`mutable_arch()` 可写，命名即意图；并与命令的 `set_command_const_execute_function`（只读）/`set_command_execute_function`（可写）一一对应。
- **FlowManager 记录流程开关**：如 `compress_routing` 这类「跨命令的流程级元数据」，由产生副作用的命令写入、供下游命令查询。
- **数据流即依赖**：命令之间的先后顺序，本质就是 Context 各分区的读写依赖（如 `build_fabric` 写 `module_graph_` 在前，`build_fabric_bitstream` 读它在后）。

## 7. 下一步学习建议

- **进入 u3 单元**：本讲把 `arch_` 当作「黑盒数据」来举例；u3 将打开这个黑盒，讲解两套架构文件（VPR arch 与 `openfpga_arch.xml`）以及电路库、配置协议——也就是 `read_openfpga_arch` 到底往 `arch_` 里写了什么。
- **深入访问器背后的数据结构**：想了解 `module_graph_` 的内部，可提前浏览 [openfpga/src/fabric/module_manager.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/module_manager.h)，它将在 u6 详讲。
- **回顾命令依赖机制**：本讲的「数据读写依赖」与 u2-l2 讲的「命令依赖声明（`set_command_dependency`）」是一体两面——前者是数据层面的因果，后者是 shell 层面的硬性校验，建议对照复习。
