# AtomNetlist 原子级网表

## 1. 本讲目标

本讲承接上一讲 [u3-l1 Netlist 泛型基类](u3-l1-netlist-base.md)。上一讲我们拆解了所有网表的共同祖先——`Netlist<BlockId,PortId,PinId,NetId>` 模板基类，建立了「块/端口/引脚/网」四元模型、`StrongId` 类型安全 ID 与「脏标记 + 批量压缩」的维护机制。本讲要把这套抽象落到 VPR 真正使用的第一份网表上：**`AtomNetlist`（原子级网表）**。

学完本讲，你应当能够：

1. 说清楚 `AtomNetlist` 在基类之外多了哪些「原子特有」的信息（原语模型、真值表、网别名）。
2. 跟踪一条完整的「`.blif` 文件 → `AtomNetlist`」读入链路：从 `read_and_process_circuit` 到 `read_blif`，再到 `BlifAllocCallback` 的回调式构造。
3. 理解 `LogicalModels` 如何把架构文件中的原语定义与网表中的具体实例绑定起来，体会「架构驱动」理念。
4. 知道 `AtomLookup` 是干什么用的：它不存电路本身，而是存「原子层实体」与「后续阶段实体（PB/CLB/时序节点）」之间的映射，是各阶段协同的桥梁。

`AtomNetlist` 是 VPR 全流程的**起点数据**（参见 [u3-l4 VprContext](u3-l4-vpr-context.md)），后续打包、布局、布线、时序分析都围绕它展开，因此把它学透是理解整个 VPR 的前提。

## 2. 前置知识

阅读本讲前，建议你先建立以下概念（不熟悉的部分可在上一讲找到详细说明）：

- **CAD 流程位置**：VPR 把一个电路「实现」到目标 FPGA 上。输入是技术映射（technology mapping）之后的网表，即电路已经被拆解成 FPGA 能直接实现的最小单元——LUT、触发器（FF）、进位链、内存块等。这些最小单元在 VPR 里叫**原子（atom）**或**原语（primitive）**。`AtomNetlist` 就是这些原子及其连线的集合。
- **Netlist 四元模型**：块（Block，这里的块就是原子）、端口（Port，一组同方向的引脚）、引脚（Pin，单根信号线）、网（Net，连线，恒有一个驱动引脚 + 若干接收引脚）。这套模型由上一讲的 `Netlist` 模板基类提供。
- **BLIF 格式**：Berkeley Logic Interchange Format，一种文本网表格式。VTR 的上游综合工具（PARMYS/ABC）产出的就是 `.blif`（或扩展版 `.eblif`）。一个 `.blif` 由若干 `.model` 组成，每个 `.model` 内有 `.inputs/.outputs/.names/.latch/.subckt/.blackbox` 等语句。
- **架构驱动**（见 [u1-l1](u1-l1-project-overview.md)）：目标 FPGA 由架构 XML 决定，XML 里 `<models>` 定义了「这个 FPGA 支持哪些原语」。这些原语定义在启动时被解析成 `LogicalModels`（见本讲 4.1.3），是判断 `.blif` 中每个块「是什么、能不能用」的依据。

> 名词提示：本讲会出现 `Atom*Id`（原子的各类 ID）、`LogicalModelId`（原语模型 ID）、`t_model`（原语模型结构）、`TruthTable`（真值表）、`AtomLookup`（原子映射表）。它们的含义会在用到时逐一展开。

## 3. 本讲源码地图

本讲涉及的关键文件与作用如下：

| 文件 | 作用 |
| --- | --- |
| [vpr/src/base/atom_netlist.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_netlist.h) | `AtomNetlist` 类声明，定义真值表、模型绑定、网别名等「原子特有」接口。 |
| [vpr/src/base/atom_netlist.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_netlist.cpp) | `AtomNetlist` 的方法实现（构造、真值表访问、`create_block` 等）。 |
| [vpr/src/base/atom_netlist_fwd.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_netlist_fwd.h) | 前向声明与 ID 类型（`AtomBlockId` 等）、`AtomBlockType` 枚举。 |
| [vpr/src/base/read_circuit.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_circuit.h) / [read_circuit.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_circuit.cpp) | 读电路的**上层入口**：格式自动推断、调用 `read_blif`、再做后处理清洗与统计。 |
| [vpr/src/base/read_blif.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_blif.h) / [read_blif.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_blif.cpp) | BLIF 读入的**核心实现**：`BlifAllocCallback` 回调式构造 `AtomNetlist`。 |
| [vpr/src/base/atom_lookup.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_lookup.h) | `AtomLookup` 类：存原子实体与后续阶段实体（PB/CLB/时序节点）的双向映射。 |
| [libs/libarchfpga/src/logic_types.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/logic_types.h) | `LogicalModels` / `t_model` / `LogicalModelId`：架构文件定义的原语模型库。 |
| [vpr/src/base/vpr_context.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h) | `AtomContext`：把 `AtomNetlist` 与 `AtomLookup` 挂入全局状态 `g_vpr_ctx`。 |
| [vpr/src/base/vpr_api.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp) | 主流程中调用 `read_and_process_circuit` 的位置（约第 331 行）。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**① 原子块与真值表**（`AtomNetlist` 在基类之上的增量）、**② BLIF 读入流程**（从文件到网表）、**③ AtomLookup 映射**（原子层与后续阶段的桥梁）。

### 4.1 原子块与真值表：AtomNetlist 在基类之上的增量

#### 4.1.1 概念说明

`Netlist` 基类（上一讲）只描述「拓扑」——谁连谁。它不知道一个块「是什么器件」，也不知道一个 LUT 块「实现什么逻辑函数」。这些与具体工艺相关的信息，由派生类 `AtomNetlist` 补齐。

直观地理解：

- `Netlist` 基类 = 电路的**连线图**（只有节点和边）。
- `AtomNetlist` = 连线图 + 每个原子节点的**器件型号**（LUT5、D 触发器、进位链……）+ 每个原子的**功能描述**（LUT 的真值表 / FF 的初值）。

`AtomNetlist` 相对基类新增的核心信息有三类：

1. **原语模型绑定**：每个块关联一个 `LogicalModelId`，表示它是哪种原语；每个端口关联一个 `t_model_ports*`，表示它是该原语的哪个端口。
2. **真值表（Truth Table）**：LUT 块存它的逻辑功能；FF/Latch 块存初值。
3. **网别名（net aliases）**：记录「同一个网在 BLIF 里可能有多个名字」（来自 `.conn` 合并），方便后续按名字查找。

#### 4.1.2 核心流程

`AtomNetlist` 是单继承自 `Netlist` 模板的普通类，构造与增删查改的总体流程是：

```
AtomNetlist(name, id)            // 仅传名字与一个唯一标识（如文件摘要）
   └─ 委托基类 Netlist 构造

create_block(name, model, truth_table)
   ├─ 调用基类 Netlist::create_block(name)   // 分配 AtomBlockId、登记名字
   ├─ block_models_.push_back(model)         // 追加：这个块是什么原语
   └─ block_truth_tables_.push_back(truth_table)  // 追加：这个块的功能

create_port(blk_id, model_port) / create_pin(...) / create_net(name)
   └─ 同样「调用基类 + 追加原子特有字段」
```

> 这里沿用上一讲讲过的 **NVI（Non-Virtual Interface）** 约定：`create_*` 是公开的非虚函数，内部做前置检查后调用基类；`*_impl()` 是基类回调回来的私有虚函数，用于维护 `AtomNetlist` 自己的额外字段（如压缩时同步搬运真值表）。

#### 4.1.3 源码精读

**类声明与继承关系。** `AtomNetlist` 把基类的四个模板参数实例化为 `Atom*Id`：

[vpr/src/base/atom_netlist.h:79-87](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_netlist.h#L79-L87) 说明它继承自 `Netlist<AtomBlockId, AtomPortId, AtomPinId, AtomNetId>`，构造函数只接收 `name`（如顶层模块名）和 `id`（如输入文件的安全摘要）。

**真值表的类型定义。** 真值表被定义为一个嵌套 `typedef`，本质是「二维逻辑值数组」：

[vpr/src/base/atom_netlist.h:93](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_netlist.h#L93) 给出 `typedef std::vector<std::vector<vtr::LogicValue>> TruthTable;`。其中 `vtr::LogicValue` 是一个枚举（`TRUE/FALSE/DONT_CARE/UNKNOWN` 等），每一行是一个「输入取值组合 → 输出」的覆盖项（cover）。

**真值表的语义（重要）。** 注释明确区分了 LUT 与 FF/Latch 两种用法：

[vpr/src/base/atom_netlist.h:102-112](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_netlist.h#L102-L112) 说明：对 LUT，真值表存的是描述逻辑函数的「单输出覆盖（single-output cover）」；对 FF/Latch，真值表只有一项，用来记录**初始状态**。换句话说，同一个 `TruthTable` 字段，在不同原语上承载不同含义。

**块的器件类型推导。** `block_type` 并不单独存一个枚举，而是**由模型 ID 推导**：

[vpr/src/base/atom_netlist.cpp:24-36](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_netlist.cpp#L24-L36) 显示：若块的模型是输入模型则返回 `INPAD`，是输出模型则返回 `OUTPAD`，否则是 `BLOCK`。对应的枚举定义在：

[vpr/src/base/atom_netlist_fwd.h:66-70](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_netlist_fwd.h#L66-L70)（`INPAD/OUTPAD/BLOCK` 三态）。

**模型与真值表的存储。** 这两个「原子特有」字段以 `vtr::vector_map`（上一讲讲过的 StrongId 索引容器）挂在每个块上：

[vpr/src/base/atom_netlist.h:259-266](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_netlist.h#L259-L266) 定义了私有成员 `block_models_`（每个块的模型 ID）、`block_truth_tables_`（每个块的真值表）、`port_models_`（每个端口的模型端口指针）、以及网别名映射 `net_aliases_map_`。

`block_truth_table` 的访问实现直接按下标取：

[vpr/src/base/atom_netlist.cpp:44-48](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_netlist.cpp#L44-L48) 返回 `block_truth_tables_[id]`。

`create_block` 是「调用基类 + 追加字段」的典型：

[vpr/src/base/atom_netlist.cpp:124-136](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_netlist.cpp#L124-L136)：先 `Netlist::create_block(name)` 拿到块 ID，再把 `model` 与 `truth_table` push 进各自的容器（第 128、129 行）。声明处 [atom_netlist.h:162-172](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_netlist.h#L162-L172) 还强调：`truth_table` 是可选参数，仅对 LUT/FF 有意义。

**原语模型从哪来：LogicalModels。** 块的「型号」不是一个字符串，而是一个类型安全的 `LogicalModelId`。它指向架构文件解析得到的原语模型库 `LogicalModels`：

[libs/libarchfpga/src/logic_types.h:83](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/logic_types.h#L83) 定义 `typedef vtr::StrongId<struct logical_model_id_tag, size_t> LogicalModelId;`。

`LogicalModels` 类内置 4 个**库模型（library model）**，对应 BLIF 里最常见的原语：

[libs/libarchfpga/src/logic_types.h:108-118](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/logic_types.h#L108-L118) 给出名字常量 `.names/.latch/.input/.output` 和对应的 ID 常量 `MODEL_INPUT_ID(0)/MODEL_OUTPUT_ID(1)/MODEL_LATCH_ID(2)/MODEL_NAMES_ID(3)`。库模型在 `LogicalModels` 构造函数里就建好，用户自定义模型（如 RAM、DSP、加法器）由架构文件解析时追加在后面（见 [logic_types.h:165-177](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/logic_types.h#L165-L177) 的 `library_models()` 与 `user_models()` 划分）。这正是「架构驱动」的体现：网表里的块能不能用，取决于架构 XML 是否声明了对应模型。

`t_model` 描述一个原语模型的端口（输入/输出链表）：

[libs/libarchfpga/src/logic_types.h:72-80](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/logic_types.h#L72-L80)，端口结构 `t_model_ports` 见 [logic_types.h:53-66](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/logic_types.h#L53-L66)。

#### 4.1.4 代码实践

**实践目标**：亲手验证「真值表字段在不同原语上含义不同」，并看清模型绑定如何决定块类型。

**操作步骤**：

1. 打开 [atom_netlist.h:259-266](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_netlist.h#L259-L266)，确认真值表存储在私有成员 `block_truth_tables_`（类型 `vtr::vector_map<AtomBlockId, TruthTable>`）。**这就是本讲实践任务要回答的「真值表存放在哪个数据结构」的答案。**
2. 打开 [atom_netlist.cpp:24-36](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_netlist.cpp#L24-L36)，观察 `block_type` 完全由 `block_model` 推导，没有任何独立存储的「类型字段」。
3. 思考一个反直觉点：一个 `.names`（LUT）块和一个 `.latch`（FF）块，它们都调用同一个 `create_block(name, model, truth_table)`，区别仅在第 2、3 个参数。请用一句话写下：LUT 的 `truth_table` 描述什么，FF 的 `truth_table` 又描述什么。

**预期结果**：LUT 的真值表是多行的单输出覆盖（逻辑函数）；FF 的真值表只有 1 行 1 项，存初值。二者共用同一个 `TruthTable` 容器，靠模型 ID 区分语义。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `AtomNetlist` 不单独存一个 `AtomBlockType` 字段，而要从 `LogicalModelId` 推导 `block_type`？

> **答案**：因为「是不是 IO」本质上由「是不是输入/输出模型」决定，这是模型信息的派生属性，单独存一份会带来冗余与不一致风险。让数据只有一份真相（模型 ID），派生属性按需计算，符合上一讲「单一数据源」的设计取向。

**练习 2**：架构文件里没有定义某个用户模型（比如 `MULT18x18`），但 `.blif` 里出现了 `.subckt MULT18x18 ...`，会发生什么？

> **答案**：读入时会因为 `get_model_by_name` 找不到模型而抛出致命错误（见 4.2.3 的 `subckt` 回调）。这体现了「架构驱动」：网表能用什么原语，由架构 XML 决定。

### 4.2 BLIF 读入流程：从 .blif 文件到 AtomNetlist

#### 4.2.1 概念说明

`.blif` 是文本文件，`AtomNetlist` 是内存中的对象。把前者变成后者，需要解决两个问题：

1. **文本解析**：把 `.names`、`.latch`、`.subckt` 等文本语句拆成结构化数据。这一步由外部库 **`blifparse`** 负责（位于 `libs/EXTERNAL/libblifparse`，属外部子树，不可在 VPR 内直接改）。
2. **结构构造**：把解析出的结构化数据「装」进 `AtomNetlist`。这一步由 VPR 自己的 **`BlifAllocCallback`** 负责。

`blifparse` 采用**回调（callback）模式**：解析器在扫到某种语句时，调用一个回调对象上对应的成员函数。`BlifAllocCallback` 就是这个回调对象，它的每个成员函数（`names()`、`latch()`、`subckt()`…）负责「遇到这种语句，就在 `AtomNetlist` 里建出对应的块/端口/引脚/网」。这种「解析与构造分离」的设计让文本格式与数据结构解耦。

#### 4.2.2 核心流程

完整的读入链路分两层，外层做「入口调度 + 后处理」，内层做「回调构造」：

```
vpr_api.cpp (主流程, ~L331)
  └─ atom_ctx.mutable_netlist() = read_and_process_circuit(format, setup, arch)   ── 外层入口
        ├─ ① 格式自动推断 (.blif / .eblif / FPGA-Interchange)        [read_circuit.cpp]
        ├─ ② netlist = read_blif(format, file, arch.models)          ── 真正读 BLIF
        │     ├─ 计算 netlist_id（文件摘要）
        │     ├─ 构造 BlifAllocCallback(format, netlist, netlist_id, models)
        │     ├─ blifparse::blif_parse_filename(file, callback)      ── 解析器驱动回调
        │     │     回调序列：begin_model → inputs/outputs/names/latch/subckt … → end_model
        │     └─ 返回填好的 netlist
        ├─ ③ process_circuit：吸收缓冲、删除 dangling、压缩、校验
        └─ ④ show_circuit_stats：打印块/网统计
```

回调内部的关键约定：

- **每个 `.model` 一个临时 `AtomNetlist`**：`BlifAllocCallback` 用 `blif_models_`（一个 vector）存放所有解析到的 model，`.end` 之后判定哪个是顶层（非 blackbox）。
- **块按驱动网命名**：`.names`/`.subckt` 块用「它的输出网名」作为块名；`.subckt` 若无输出则用 `unique_subckt_name()` 起名。
- **常量识别**：`.names` 的真值表若为空或单 `0` → 常量 0；单 `1` → 常量 1，会把输出引脚标记为常量源（`is_const=true`）。

#### 4.2.3 源码精读

**外层入口 `read_and_process_circuit`。** 它先做格式推断，再分派到 `read_blif`（BLIF/EBLIF）或 `read_interchange_netlist`（FPGA-Interchange）：

[vpr/src/base/read_circuit.cpp:36-48](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_circuit.cpp#L36-L48) 是格式自动推断（按扩展名 `.blif`/`.eblif`）；[read_circuit.cpp:54-67](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_circuit.cpp#L54-L67) 是分派 switch；注意 `read_blif` 的第三个实参是 `arch.models`，即把架构里的原语模型库传进去做绑定。

格式枚举定义在：

[vpr/src/base/read_circuit.h:8-13](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_circuit.h#L8-L13)（`AUTO/BLIF/EBLIF/FPGA_INTERCHANGE`）。

**后处理 `process_circuit`。** 读入的原始网表还要清洗：

[vpr/src/base/read_circuit.cpp:93-146](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_circuit.cpp#L93-L146)：包括吸收 LUT 缓冲（`absorb_buffer_luts`）、移除特殊的 `unconn` 网/块、清扫悬空逻辑（`sweep_iterative`），最后 `remove_and_compress()` 压缩无效项并 `verify()` 自检。这一步把「上游工具留下的冗余」清干净，给后续阶段一份规整的网表。

**核心读入函数 `read_blif`。** 函数很短，职责清晰：

[vpr/src/base/read_blif.cpp:716-726](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_blif.cpp#L716-L726)：① 算文件摘要 `netlist_id`（`vtr::secure_digest_file`）；② 构造 `BlifAllocCallback`，它**引用**外部传入的 `netlist` 对象；③ 调 `blifparse::blif_parse_filename` 启动解析；④ 返回填好的 `netlist`。函数签名见 [read_blif.h:13-15](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_blif.h#L13-L15)。

**回调对象 `BlifAllocCallback`。** 它继承自 `blifparse::Callback`，构造时拿到 `AtomNetlist& main_netlist_`（用户对象）、`netlist_id_`、`models_` 与格式：

[vpr/src/base/read_blif.cpp:41-50](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_blif.cpp#L41-L50) 是构造函数，并断言格式只能是 BLIF 或 EBLIF。

**`.names` 回调——LUT 与真值表的诞生地（重点）。** 这是「真值表如何从文本变成数据结构」的核心：

[vpr/src/base/read_blif.cpp:112-184](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_blif.cpp#L112-L184)。关键三段：

- 第 130-136 行：把解析器给出的 `so_cover`（single-output cover，二维 `blifparse::LogicValue`）逐项转换为 `AtomNetlist::TruthTable`（用 `to_vtr_logic_value` 做枚举映射）。
- 第 138 行：`create_block(nets[最后一个], MODEL_NAMES_ID, truth_table)`——`.names` 的最后一个网名是输出，用做块名，模型固定为 `MODEL_NAMES_ID`，真值表随块存入。
- 第 149-178 行：常量识别——空真值表或单 `0` → 常量 0；单 `1` → 常量 1，并把输出引脚的 `is_const` 置真。

**`.latch` 回调——FF 的真值表是初值。** 对比 `.names`，能深刻体会「同一个 `TruthTable` 字段、不同语义」：

[vpr/src/base/read_blif.cpp:218-222](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_blif.cpp#L218-L222)：`TruthTable truth_table(1); truth_table[0].push_back(to_vtr_logic_value(init));`——把 BLIF `.latch` 的 `init` 初值包成「1 行 1 项」的真值表。完整回调见 [read_blif.cpp:186-239](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_blif.cpp#L186-L239)，注意它强制要求上升沿、且必须有时钟。

**`.subckt` 回调——用户模型实例。** 用名字查架构模型，找不到就致命报错：

[vpr/src/base/read_blif.cpp:244-249](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_blif.cpp#L244-L249)：`models_.get_model_by_name(subckt_model)`，若 `!is_valid()` 则 `vpr_throw`。完整逻辑见 [read_blif.cpp:241-313](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_blif.cpp#L241-L313)。

**顶层模型判定。** 一个 BLIF 可含多个 `.model`，但只有一个能含真实原语（其余须是 blackbox）：

[vpr/src/base/read_blif.cpp:415-447](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_blif.cpp#L415-L447) 的 `determine_main_netlist_index` 遍历所有 model，找到唯一非 blackbox 的作为顶层；若有多个则报错。解析结束时把选中的顶层 model 移交给用户对象：[read_blif.cpp:57-63](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_blif.cpp#L57-L63)。

#### 4.2.4 代码实践

**实践目标**：跟踪一条 `.blif` 语句到 `AtomNetlist` 中一个块的诞生过程。

**操作步骤**：

1. 打开示例 BLIF 文件 [vtr_flow/benchmarks/blif/2/C17.blif](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/benchmarks/blif/2/C17.blif)。它只有一个 `.model top`，5 个输入、2 个输出、6 个 `.names`（全是 2 输入 LUT）。
2. 选其中一条 `.names`，例如第 4-6 行：

   ```text
   .names [5] [6] p_22gat_10_
   1- 1
   -1 1
   ```

   它表示一个 2 输入 LUT：输入网 `[5]`、`[6]`，输出网 `p_22gat_10_`，真值表两行 `1-` 和 `-1`（或逻辑）。
3. 对照 [read_blif.cpp:112-184](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_blif.cpp#L112-L184)，写出这条 `.names` 被处理时会依次调用：
   - `create_block("p_22gat_10_", MODEL_NAMES_ID, [[T,D],[D,T]])`（块名=输出网名，真值表由两行覆盖转换而来，`-` 变 `DONT_CARE`）；
   - `create_port(...)` 建输入/输出端口；
   - 对 `[5]`、`[6]` 各建一个 SINK 引脚，对 `p_22gat_10_` 建一个 DRIVER 引脚。
4. 写下从 `.blif` 文件到 `AtomNetlist` 的主要步骤（这正是本讲规格里的实践任务）：

   ```
   .blif 文件
     → read_and_process_circuit （格式推断 + 入口）
       → read_blif （算摘要、构造 BlifAllocCallback、blifparse 解析）
         → BlifAllocCallback 回调：names/latch/subckt/inputs/outputs …
           → curr_model().create_block / create_port / create_pin / create_net
       → 确定顶层 model 并移交给用户对象
     → process_circuit （吸收缓冲、清扫悬空、压缩、校验）
     → 返回填好的 AtomNetlist，写入 AtomContext
   ```

**需要观察的现象**：`.names` 的真值表会被原样（经过逻辑值映射）存进 `block_truth_tables_`；而 `.latch` 的「真值表」其实只有初值一项。

**预期结果**：你能指出**真值表存储在 `AtomNetlist` 的私有成员 `block_truth_tables_`**（`vtr::vector_map<AtomBlockId, TruthTable>`，见 [atom_netlist.h:260](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_netlist.h#L260)），由 `.names` 回调第 138 行的 `create_block` 写入。

> 说明：以上为源码阅读型实践，不需要运行；若想看真实运行效果，可在构建后对 C17 跑一次 VPR 并开启 `--echo_file on`，观察 `E_ECHO_ATOM_NETLIST_ORIG` 回显文件（见 [read_circuit.cpp:70-72](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_circuit.cpp#L70-L72)）。具体回显开关名称以本地构建版本为准，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`.names gnd`（真值表为空）会被处理成什么？

> **答案**：见 [read_blif.cpp:151-167](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_blif.cpp#L151-L167)。空真值表对应常量 0 生成器，会打印 `Found constant-zero generator` 并把输出引脚 `is_const` 置真。

**练习 2**：`read_blif` 里的 `BlifAllocCallback` 构造时接收的是 `AtomNetlist&`（引用），但解析过程中它又往自己的 `blif_models_` 里塞临时 model，最后才「移交给」这个引用对象。为什么要这么做？

> **答案**：因为一个 BLIF 可能有多个 `.model`，只有解析完所有 model、用 `determine_main_netlist_index` 判定出唯一的非 blackbox 顶层之后，才能确定哪一个是最终结果（见 [read_blif.cpp:57-63](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_blif.cpp#L57-L63)）。用 `std::move` 移交还能避免同时持有两份网表、浪费内存。

### 4.3 AtomLookup 映射：原子层与后续阶段的桥梁

#### 4.3.1 概念说明

`AtomNetlist` 一旦读入，在整个流程中基本是**只读参考**——后续阶段不会改写它，而是围绕它工作。但后续阶段会产生大量「与原子相关的新实体」：

- 打包阶段（见 [u4 Packing](u4-l1-packing-overview.md)）会把每个原子放进某个复杂逻辑块（CLB）的某个 `t_pb` 里；
- 布局阶段会给每个 CLB 分配一个网格位置；
- 时序分析阶段会把每个原子引脚映射成时序图（Tatum graph）的一个节点 `tnode`。

这些「原子 ↔ 后续实体」的关系需要被记录下来，供跨阶段查询。**`AtomLookup` 就是专门存这些映射的类。** 它不存电路本身（电路在 `AtomNetlist` 里），只存「指针关系」。

关键映射包括：

| 映射 | 含义 |
| --- | --- |
| `AtomBlockId ↔ t_pb*` | 原子块 ↔ 打包后所在叶子 PB（双向，用 bimap） |
| `AtomPinId ↔ t_pb_graph_pin*` | 原子引脚 ↔ 物理图引脚 |
| `AtomBlockId ↔ ClusterBlockId` | 原子块 ↔ 所在 CLB |
| `AtomNetId ↔ ClusterNetId` | 原子网 ↔ 聚簇网（可能一对多） |
| `AtomPinId ↔ tatum::NodeId` | 原子引脚 ↔ 时序图节点（分内部/外部） |

#### 4.3.2 核心流程

`AtomLookup` 的使用模式是「**随着阶段推进，逐步填表**」：

```
读入完成      : AtomNetlist 就绪，AtomLookup 基本为空
打包 (u4)     : 填 atom↔pb、atom_pin↔pb_graph_pin、atom↔clb、atom_net↔clb_net
时序图构建(u7): 填 atom_pin↔tnode   (TimingGraphBuilder 接收 atom_ctx.mutable_lookup())
```

它由全局上下文 `AtomContext` 持有（见 [u3-l4](u3-l4-vpr-context.md)），通过 `g_vpr_ctx.mutable_atom().mutable_lookup()` 获取可写引用、`.lookup()` 获取只读引用。

一个值得注意的设计：`atom↔pb` 的 bimap 有**访问锁**。在某些局部算法里，为了防止误用全局状态，会把锁设为 true，此时访问 bimap 会触发断言失败。这是一种「强制单一数据源」的工程约束。

#### 4.3.3 源码精读

**类的定位与职责。** 注释一句话概括了它的作用：

[vpr/src/base/atom_lookup.h:15-18](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_lookup.h#L15-L18)：描述 `AtomNetlist` 组件与其他实体（`t_pb`、`clb`）之间的映射。

**atom↔pb bimap 与访问锁。** 这是打包阶段最核心的映射：

[vpr/src/base/atom_lookup.h:36-39](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_lookup.h#L36-L39) 是设锁方法 `set_atom_pb_bimap_lock`，注释（[atom_lookup.h:44-48](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_lookup.h#L44-L48)）说明：上锁后任何（读或写）访问 bimap 都会触发断言失败，目的是在应当使用局部数据结构的地方「禁止偷偷读全局状态」。可变/只读访问见 [atom_lookup.h:52-62](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_lookup.h#L52-L62)。

bimap 实体类型 `AtomPBBimap` 定义在打包目录：

[vpr/src/pack/atom_pb_bimap.h:24-39](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/atom_pb_bimap.h#L24-L39)：提供 `atom_pb(blk_id)`（原子→叶子 pb）与 `pb_atom(pb)`（pb→原子）双向查询。

**块与网的 CLB 映射。**

[vpr/src/base/atom_lookup.h:85-93](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_lookup.h#L85-L93) 是 `atom_clb` / `set_atom_clb`（原子块↔CLB 双向）；[atom_lookup.h:99-114](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_lookup.h#L99-L114) 是原子网↔CLB 网（注意一个原子网可能对应多个 CLB 网，所以是 `vector<ClusterNetId>`）。

**时序节点映射。**

[vpr/src/base/atom_lookup.h:121-130](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_lookup.h#L121-L130)：`atom_pin_tnode` / `set_atom_pin_tnode`。参数 `BlockTnode block_tnode_type` 区分「外部节点」（块边界，用于布线后时序）与「内部节点」（块内时序），对应私有成员 `atom_pin_tnodeExternal_` 与 `atom_pin_tnodeInternal_`（[atom_lookup.h:148-150](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_lookup.h#L148-L150)）。

**在全局上下文中的挂载。** `AtomLookup` 与 `AtomNetlist` 一起被 `AtomContext` 持有：

[vpr/src/base/vpr_context.h:92-95](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L92-L95) 定义私有成员 `nlist_` 与 `lookup_`；访问器 [vpr_context.h:104-116](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L104-L116) 提供 `netlist()/mutable_netlist()` 与 `lookup()/mutable_lookup()`。

**时序图构建消费 lookup 的实例。** 主流程构建时序图时，把 `mutable_lookup()` 传给 `TimingGraphBuilder`：

[vpr/src/base/vpr_api.cpp:345](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L345)：`TimingGraphBuilder(atom_ctx.netlist(), atom_ctx.mutable_lookup(), arch->models).timing_graph(...)`。这就是 `AtomLookup` 的 `atom_pin↔tnode` 映射被填写的入口之一（时序详见 [u7-l1](u7-l1-timing-graph-tatum.md)）。

#### 4.3.4 代码实践

**实践目标**：理解 `AtomLookup`「只存映射、不存电路」的定位，并看清它在主流程里的填表时机。

**操作步骤**：

1. 在 [atom_lookup.h:132-150](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_lookup.h#L132-L150) 列出私有成员，确认它**没有任何**「块/网拓扑」数据，只有各种 `*_to_*` 映射表与一个 bimap。
2. 在 `vpr/src` 下搜索 `mutable_lookup()` 的调用点（用 IDE 或 `grep`），观察哪些阶段在「写」它。预期会看到：打包相关代码（写 atom↔pb、atom↔clb）、时序图构建（写 atom_pin↔tnode）。
3. 对照 [vpr_api.cpp:345](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L345)，确认时序图构建同时接收 `netlist()`（只读电路）和 `mutable_lookup()`（可写映射），体会「电路不变、映射增长」的模式。

**预期结果**：你应当能用一句话总结——`AtomNetlist` 是「电路真相」，`AtomLookup` 是「电路在各阶段的投影坐标」，二者都挂在 `AtomContext` 上供全流程共享。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `atom_net ↔ clb_net` 是「一对多」（一个原子网可能对应多个 CLB 网），而 `atom_block ↔ clb` 是「一对一」？

> **答案**：一个原子只能整体放进一个 CLB（块不可拆分），所以块↔CLB 一对一；但一条原子网在被打包后，可能因为穿过多个 CLB 而被拆成多段 CLB 级网（每个 CLB 边界一段），所以是「一个原子网 → 多个 CLB 网」。对应数据结构 [atom_lookup.h:145](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_lookup.h#L145) 的 `atom_net_to_clb_nets_` 存的是 `vector<ClusterNetId>`。

**练习 2**：`set_atom_pb_bimap_lock(true)` 之后访问 bimap 会怎样？为什么要提供这个锁？

> **答案**：会触发断言失败（见 [atom_lookup.h:52-55](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_lookup.h#L52-L55) 与 [44-48](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_lookup.h#L44-L48) 注释）。目的是在某些局部算法中强制使用局部数据结构、禁止偷偷读全局上下文，保证「单一数据源」、避免全局/局部状态不一致。

## 5. 综合实践

把本讲三个模块串起来，完成一个「**端到端追踪一个 LUT 的诞生与映射**」的任务：

1. **读入层**：打开 [C17.blif](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/benchmarks/blif/2/C17.blif) 第 4-6 行那条 2 输入 LUT。对照 [read_blif.cpp:112-184](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_blif.cpp#L112-L184)，写出它产生的 `create_block/create_port/create_pin/create_net` 调用序列，并标注真值表 `[[T,D],[D,T]]` 最终落到哪个成员（`block_truth_tables_`）。

2. **数据层**：用 [atom_netlist.cpp:24-36](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_netlist.cpp#L24-L36) 说明：现在调用 `block_type(this_block)` 会返回什么？为什么？（答：`BLOCK`，因为模型是 `MODEL_NAMES_ID`，既非输入也非输出模型。）

3. **映射层**：这个 LUT 块在打包后会被装进某个 CLB 的某个叶子 PB 里。依据 [atom_lookup.h:85-93](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_lookup.h#L85-L93) 与 [atom_pb_bimap.h:33-39](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/atom_pb_bimap.h#L33-L39)，写出打包完成后能从这个块的 `AtomBlockId` 查到的两类「后续实体」（CLB 与叶子 PB）。

4. **画一张时序图**：画出 `read_and_process_circuit → read_blif → BlifAllocCallback::names → AtomNetlist::create_block` 的调用栈，并在旁边标注每一步读写的数据结构（`blif_models_` → `block_models_/block_truth_tables_` → 最终移交 `main_netlist_`）。

> 这个任务覆盖了「读入 → 原子数据 → 阶段映射」全链路。完成后，你就为下一单元 [u4 Packing](u4-l1-packing-overview.md)（打包如何把原子装进 CLB 并填满 `AtomLookup`）打下了完整的数据基础。

## 6. 本讲小结

- `AtomNetlist` 是 `Netlist` 基类的派生类，在「拓扑」之上补齐了三类原子特有信息：**原语模型绑定**（`LogicalModelId` / `t_model_ports*`）、**真值表**（`TruthTable`）、**网别名**。
- 真值表类型是 `vector<vector<vtr::LogicValue>>`，存在私有成员 `block_truth_tables_`；它对 LUT 存「单输出覆盖（逻辑函数）」，对 FF/Latch 存「初值」——同一个字段、不同语义，靠模型 ID 区分。
- 「`.blif` → `AtomNetlist`」分两层：外层 `read_and_process_circuit` 做格式推断与后处理清洗，内层 `read_blif` 用 `blifparse` 解析文本、由 `BlifAllocCallback` 回调式地把每条语句建为块/端口/引脚/网。
- 原语模型来自架构文件解析得到的 `LogicalModels`（含 4 个库模型 `.names/.latch/.input/.output` 与若干用户模型），`.subckt` 找不到模型即致命报错——这是「架构驱动」的直接体现。
- `AtomLookup` 不存电路，只存「原子实体 ↔ 后续阶段实体（PB/CLB/tnode）」的映射，由 `AtomContext` 持有，随打包、时序图构建等阶段逐步填表。
- `AtomNetlist`（电路真相）+ `AtomLookup`（阶段映射）共同构成 `AtomContext`，通过 `g_vpr_ctx` 供全流程共享。

## 7. 下一步学习建议

- 接下来学 **[u3-l3 ClusteredNetlist 聚簇网表](u3-l3-clustered-netlist.md)**：它是 `Netlist` 基类的另一个派生类，表示「打包后」的逻辑块级网表，与 `AtomNetlist` 形成对比，能加深你对「同一基类、不同抽象层」的理解。
- 之后学 **[u3-l4 VprContext 与全局状态管理](u3-l4-vpr-context.md)**：把本讲的 `AtomContext` 放回 `g_vpr_ctx` 全局上下文中，看清各阶段如何共享 `AtomNetlist` 与 `AtomLookup`。
- 想提前了解「`AtomLookup` 的映射如何被填满」，可跳读 **[u4-l1 Packing 总览](u4-l1-packing-overview.md)** 与 **[u7-l1 时序图构建与 Tatum 集成](u7-l1-timing-graph-tatum.md)**，那里会用到本讲的 `atom↔pb` 与 `atom_pin↔tnode` 映射。
- 推荐顺手读一眼 [vpr/src/base/read_blif.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_blif.cpp) 里 `inputs()/outputs()/blackbox()` 等回调，补全「BLIF 各种语句如何建块」的全貌。
