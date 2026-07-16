# OpenFPGAArch 数据模型

## 1. 本讲目标

在 u3-l2 里，我们从「读者视角」看完了 `openfpga_arch.xml` 的顶层结构，知道了这份 XML 有 7 个顶层节点，并初步建立了「解析存名字 → 链接查 ID」的两段式印象。本讲换到「引擎视角」：当 `openfpga` 二进制把这份 XML 吃进来后，它在内存里到底变成了一个什么样的 C++ 对象？

读完本讲，你应当能够：

1. 说出 `openfpga::Arch` 这个聚合结构里到底装了哪些子对象，以及它们各自来自 XML 的哪一段。
2. 解释为什么 `Arch` 一旦构建完成就被设计成**只读**的，这套约定如何支撑整个引擎的模块化。
3. 读懂 `TechnologyLibrary`（工艺库）、`ArchDirect`（直连库）、`SimulationSetting`（仿真设置）这几类辅助模型的职责与内部组织。
4. **纠正一个容易混淆的点**：`SimulationSetting` 和 `BitstreamSetting` 并不在 `Arch` 里面，它们是与 `Arch` 平级的兄弟结构——这是理解 OpenFPGA 模块化边界的关键。

## 2. 前置知识

- **聚合根（aggregate root）**：把一组紧密相关的数据打包在一个结构体里，对外只暴露这个结构体本身，内部成员协同工作。`Arch` 就是电路级架构的聚合根。
- **SoA（Structure of Arrays，结构数组）**：一种存储风格——同一类属性（比如「名字」）集中存在一个数组里，用强类型 ID 作下标。OpenFPGA 的各种库（电路库、工艺库）普遍采用 `vtr::vector<IdType, ValueType>` 这种 SoA 容器，u3-l3 讲电路库时已见过。
- **只读不变量（read-only invariant）**：对象一旦构建完成就不再被修改，所有使用者都拿到 `const` 引用。这能让多段代码安全地共享同一份数据，不必担心被别人偷偷改掉。
- **强类型 ID**：用专门的类型（如 `CircuitModelId`、`TechnologyModelId`）代替裸整数当索引用，编译期就能防止「把电路模型 ID 当成工艺模型 ID 用」这类错误。
- 本讲默认你已经读过 **u3-l2**（`openfpga_arch.xml` 总体结构与七大顶层节点）和 **u3-l3 / u3-l4**（电路库与配置协议），本讲只补充它们在 C++ 侧的「容器」，不再重复 XML 细节。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `libs/libarchopenfpga/src/openfpga_arch.h` | 定义 `openfpga::Arch` 聚合结构，是本讲的绝对主角。 |
| `libs/libarchopenfpga/src/technology_library.h` | `TechnologyLibrary` 工艺库：晶体管/RRAM 器件模型与工艺偏差。 |
| `libs/libarchopenfpga/src/arch_direct.h` | `ArchDirect` 直连库：CLB 间点对点直连的类型与方向。 |
| `libs/libarchopenfpga/src/simulation_setting.h` | `SimulationSetting` 仿真设置（注意：**不在 Arch 内**）。 |
| `libs/libarchopenfpga/src/bitstream_setting.h` | `BitstreamSetting` 比特流设置（同样**不在 Arch 内**）。 |
| `libs/libarchopenfpga/src/tile_annotation.h` | `TileAnnotation` 物理瓦片注解（全局端口等，是 Arch 的一个成员）。 |
| `libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp` | 解析入口：把 XML 各段填进 `Arch` 的对应成员。 |
| `libs/libarchopenfpga/src/read_xml_openfpga_arch.h` | 声明了 `Arch` / `SimulationSetting` / `BitstreamSetting` 三个**平级**的读取函数。 |
| `openfpga/src/base/openfpga_context.h` | `OpenfpgaContext`：证实三者是兄弟成员，不是嵌套关系。 |
| `openfpga/src/base/openfpga_read_arch_template.h` | 三条 shell 命令（`read_openfpga_arch` 等）的模板实现。 |
| `openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml` | 一个真实的 `openfpga_arch.xml` 样例，用于对照练习。 |

## 4. 核心概念与源码讲解

### 4.1 openfpga::Arch：电路级架构的只读聚合根

#### 4.1.1 概念说明

`openfpga::Arch` 是「电路级架构」在内存中的总账本。回想 u3-l1 的结论：OpenFPGA 用两份 XML 描述一个 FPGA——VPR 架构 XML 描述**结构**，`openfpga_arch.xml` 描述**电路级物理实现**。这份「电路级物理实现」解析完之后，就凝结成 `Arch` 这一个对象。

直觉上可以这样理解 `Arch` 的角色：它是**积木清单 + 拼装规则**。

- 「积木清单」：`circuit_library`（有哪些电路模型，u3-l3 讲过）和 `technology_library`（这些电路要用什么晶体管实现）。
- 「拼装规则」：`configuration_protocol`（配置位怎么组织，u3-l4 讲过）、`connection_block`/`switch_block`/`routing_segment`（布线部件用哪个电路模型）、`pb_type_annotations`（VPR 逻辑块绑到哪个物理电路，u4-l3 讲过）、`arch_direct`（直连用哪个电路模型）和 `tile_annotations`（瓦片级全局端口）。

一句话：**`Arch` 回答「用什么电路、按什么规则，搭出这块 FPGA」**。

#### 4.1.2 核心流程

`Arch` 的生命周期很规整，分三步：

1. **构造**：`read_xml_openfpga_arch()` 创建一个空的 `Arch`，逐段把 XML 节点解析进对应成员。
2. **链接**：解析完名字后，用 `bind_*` / `link_*` 函数把「名字」翻译成跨库的强类型 ID（例如把电路模型名翻译成 `TechnologyModelId`）。
3. **冻结**：返回后存入 `OpenfpgaContext::arch_`，此后全引擎只通过 `const` 访问器 `arch()` 读取，不再修改。

```text
openfpga_arch.xml
      │  read_xml_openfpga_arch()
      ▼
┌─────────────────────────────────────────┐
│  openfpga::Arch  (只读聚合根)            │
│   ├─ circuit_lib        ← <circuit_library>
│   ├─ tech_lib           ← <technology_library>
│   ├─ circuit_tech_binding ← (链接阶段派生)
│   ├─ config_protocol    ← <configuration_protocol>
│   ├─ cb_switch2circuit  ← <connection_block>
│   ├─ sb_switch2circuit  ← <switch_block>
│   ├─ routing_seg2circuit← <routing_segment>
│   ├─ arch_direct        ← <direct_connection>（可选）
│   ├─ tile_annotations   ← <tile_annotations>（可选）
│   └─ pb_type_annotations← <pb_type_annotations>
└─────────────────────────────────────────┘
      │  存入 context.arch_，此后只读
      ▼
   下游命令（build_fabric 等）通过 arch() 读取
```

#### 4.1.3 源码精读

先看聚合根本身。头文件顶部的注释明确写出了「只读不变量」这一设计原则：

> 这段注释与结构体声明：[libs/libarchopenfpga/src/openfpga_arch.h:L17-L64](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/openfpga_arch.h#L17-L64) —— 注释强调「一旦由 `read_xml_openfpga_arch()` 构建，就应当是只读的，以保证一切良好模块化」；下面的 `struct Arch` 列出全部成员。

`struct Arch` 的成员清单（共 10 项，逐行对应）：

| 成员 | 类型 | 来自 XML 的哪一段 |
| --- | --- | --- |
| `circuit_lib` | `CircuitLibrary` | `<circuit_library>` |
| `tech_lib` | `TechnologyLibrary` | `<technology_library>` |
| `circuit_tech_binding` | `map<CircuitModelId, TechnologyModelId>` | 链接阶段派生（来自每个电路模型的 `<device_technology device_model_name=.../>`） |
| `config_protocol` | `ConfigProtocol` | `<configuration_protocol>` |
| `cb_switch2circuit` | `map<string, CircuitModelId>` | `<connection_block>` |
| `sb_switch2circuit` | `map<string, CircuitModelId>` | `<switch_block>` |
| `routing_seg2circuit` | `map<string, CircuitModelId>` | `<routing_segment>` |
| `arch_direct` | `ArchDirect` | `<direct_connection>`（可选） |
| `tile_annotations` | `TileAnnotation` | `<tile_annotations>`（可选） |
| `pb_type_annotations` | `vector<PbTypeAnnotation>` | `<pb_type_annotations>` |

注意一个细节：注释里写着 "including circuit library, technology library and **simulation parameters**"，但下面的结构体里**并没有** simulation 相关成员。这是历史遗留的注释——`SimulationSetting` 如今已经独立出去成了兄弟结构（见 4.4）。**读源码时，以结构体定义为准、注释只作参考**，这是一个很典型的例子。

再看解析入口如何逐段填充。`read_xml_openfpga_arch.cpp` 是「XML 段 → Arch 成员」的权威映射，节选关键几步：

> 解析 `<circuit_library>` 并构建内部链接：[libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp:L56-L68](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp#L56-L68) —— 先 `read_xml_circuit_library` 填 `circuit_lib`，再依次调用 `auto_detect_default_models()`、`build_model_links()`、`build_timing_graphs()` 完成「解析 → 链接」两段式。

> 解析 `<technology_library>` 并绑定到电路模型：[libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp:L70-L79](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp#L70-L79) —— 填 `tech_lib` 后调用 `link_models_to_variations()`，再由 `bind_circuit_model_to_technology_model()` 派生出 `circuit_tech_binding`（这就是上表里唯一一个不直接对应单一 XML 节点的成员）。

> 解析配置协议并建立与电路库的链接：[libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp:L81-L93](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp#L81-L93) —— 填 `config_protocol`，链接到电路库，再用默认配置存储模型回填所有电路模型的 sram 端口。

> 解析布线三段 + 直连 + 瓦片/pb 注解：[libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp:L95-L121](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp#L95-L121) —— 依次填 `cb_switch2circuit`、`sb_switch2circuit`、`routing_seg2circuit`、`arch_direct`、`tile_annotations`、`pb_type_annotations`。（顺带一提：源码里 `cb_switch2circuit` 的赋值出现了两次重复，属于无害的冗余，正好说明读源码也会遇到小瑕疵。）

#### 4.1.4 代码实践

这是本讲的主实践——把 `Arch` 成员和真实 XML 段对上号。

1. **实践目标**：亲手验证「XML 段 → Arch 成员 → 解析函数」的映射，确认 `Arch` 的真实组成。
2. **操作步骤**：
   - 打开 [libs/libarchopenfpga/src/openfpga_arch.h:L25-L64](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/openfpga_arch.h#L25-L64)，抄下 `struct Arch` 的全部成员。
   - 打开样例 [openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml)，逐个顶层节点对照。
   - 对照 [read_xml_openfpga_arch.cpp:L35-L128](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp#L35-L128)，确认每个成员由哪个 `read_xml_*` / `bind_*` 填充。
3. **需要观察的现象**：样例 XML 里会出现 `<technology_library>`(L11)、`<circuit_library>`(L31)、`<configuration_protocol>`(L162)、`<connection_block>`(L165)、`<switch_block>`(L168)、`<routing_segment>`(L171)、`<pb_type_annotations>`(L174) 七个顶层节点；但**找不到** `<direct_connection>` 和 `<tile_annotations>`。
4. **预期结果**：`k4_N4_40nm_cc_openfpga.xml` 能填满 `Arch` 的 8 个成员，而 `arch_direct` 与 `tile_annotations` 因 XML 缺省而保持为空——这本身就是有效状态，说明这两个成员是可选的。
5. **待本地验证**：可选地，用一个更复杂的架构文件（含 `<tile_annotations>` 的较大 arch）重做对照，确认这两个成员也能被填上。

#### 4.1.5 小练习与答案

**练习 1**：`circuit_tech_binding` 没有专属的 XML 节点，它的数据从哪里来？
**答案**：来自每个 `<circuit_model>` 内部的 `<device_technology device_model_name="logic"/>` 子节点。解析阶段先收集这些名字，链接阶段由 `bind_circuit_model_to_technology_model()` 把名字翻译成 `CircuitModelId → TechnologyModelId` 的映射，所以它是「派生成员」而非「直填成员」。

**练习 2**：为什么把 `Arch` 设计成构建后只读？
**答案**：`Arch` 被引擎里大量子系统（fabric 构建、网表生成、比特流生成）共享读取。一旦冻结为只读，任何下游都拿到 `const Arch&`，编译器就能在编译期阻止误写，多段代码共享同一份数据时也无需加锁或担心被改坏——这正是注释里「keep everything well modularized」的含义。

### 4.2 TechnologyLibrary：工艺器件库

#### 4.2.1 概念说明

`circuit_library` 回答「用什么电路」（比如一个反相器 `INVTX1`），但它不关心这个反相器用什么晶体管做。`TechnologyLibrary` 回答更底层的问题：**这个 FPGA 用哪条工艺线、晶体管沟道多长、宽长比多少、有多少工艺偏差**。它是生成 SPICE 网表（u8-l3）时把电路模型「下蛋」到晶体管级的依据。

工艺库里有两种「东西」：

- **器件模型（device model）**：一组 PMOS+NMOS 晶体管，或一个 RRAM。每个模型声明它来自工业库（`.lib`，可指定工艺角 corner）还是学术库（`.pm`，PTM 模型）。
- **工艺偏差（variation）**：器件参数的涨落，用绝对偏差值和 σ 个数描述，供蒙特卡洛仿真使用。

#### 4.2.2 核心流程

`TechnologyLibrary` 同样走 SoA + 强类型 ID 的套路（和 u3-l3 的电路库同构）：

```text
<technology_library>
   ├─ <device_library>      ─►  TechnologyModelId 列表
   │      <device_model>           每个模型：name / type(transistor|rram)
   │         <lib .../>               lib_type(industry|academia) / corner / ref / lib_path
   │         <design vdd pn_ratio/>   vdd / pn_ratio
   │         <pmos/><nmos/>           晶体管名/沟道/最小最大宽度/绑定的 variation 名
   └─ <variation_library>   ─►  TechnologyVariationId 列表
          <variation .../>          name / abs_deviation / num_sigma

解析存名字 → link_models_to_variations() 把 variation 名翻译成 VariationId
```

器件与偏差之间靠「名字」松耦合：晶体管节点上写 `variation="logic_transistor_var"`，解析期只存字符串，链接期再用 `link_models_to_variations()` 查到真正的 `TechnologyVariationId`。这和电路库的「解析存名字 → 链接查 ID」是完全一致的两段式（u3-l2 已建立这个印象，这里再次印证）。

#### 4.2.3 源码精读

三个枚举先把工艺库的「类别空间」定死：

> 库类型 industry/academia：[libs/libarchopenfpga/src/technology_library.h:L27-L34](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/technology_library.h#L27-L34) —— 工业库用 `.lib` 引入、可带工艺角；学术库用 `.include` 引入 PTM 模型。

> 模型类型 transistor/rram：[libs/libarchopenfpga/src/technology_library.h:L41-L48](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/technology_library.h#L41-L48)。

> 晶体管类型 pmos/nmos：[libs/libarchopenfpga/src/technology_library.h:L55-L62](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/technology_library.h#L55-L62)。

再看内部存储。器件的基本属性逐字段落在各自的 `vtr::vector` 里，晶体管参数用 `std::array<..., 2>`（下标 0=PMOS、1=NMOS）紧凑存放：

> 器件基本信息字段：[libs/libarchopenfpga/src/technology_library.h:L196-L242](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/technology_library.h#L196-L242) —— 包含 `model_ids_`、`model_names_`、`model_types_`、`model_lib_types_`、`model_corners_`、`model_refs_`、`model_lib_paths_`、`model_vdds_`、`model_pn_ratios_`。

> 晶体管级参数（PMOS/NMOS 各一）：[libs/libarchopenfpga/src/technology_library.h:L253-L287](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/technology_library.h#L253-L287) —— 晶体管名、沟道长度、最小/最大宽度，以及绑定的 variation 名（`..._variation_names_`，链接后变成 `..._variation_ids_`）。注释还解释了：当电路模型要求宽度超过 `max_width` 时，会实例化多个晶体管 bin；对 FinFET 则 max_width 应等于 min_width。

最后是名字到 ID 的快查表，让链接阶段 O(log n) 反查：

> variation 字段与 name→id 快查：[libs/libarchopenfpga/src/technology_library.h:L304-L317](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/technology_library.h#L304-L317) —— `variation_abs_values_`、`variation_num_sigmas_`，以及 `model_name2ids_` / `variation_name2ids_` 两张快查表。

对照真实 XML 里的器件声明会更直观：样例里 `logic` 器件模型声明了 industry 库、`vdd=0.9`、`pn_ratio=2`，并分别给 pmos/nmos 指定沟道与宽度，variation 名指向 `logic_transistor_var`：

> 样例 `logic` 器件模型：[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:L13-L18](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L13-L18) —— 正好对应上面 `model_lib_types_/model_vdds_/model_pn_ratios_/transistor_model_*` 这几列。

#### 4.2.4 代码实践

1. **实践目标**：把 XML 的 `<device_model>` 各属性逐一对应到 `TechnologyLibrary` 的内部字段。
2. **操作步骤**：
   - 在 [k4_N4_40nm_cc_openfpga.xml:L11-L30](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L11-L30) 找到两个 `device_model`（`logic`、`io`）与两个 `variation`。
   - 对照 technology_library.h 的字段，写一张小表：`type="transistor"` → `model_types_`；`lib type="industry"` → `model_lib_types_`；`corner="TOP_TT"` → `model_corners_`；`ref="M"` → `model_refs_`；`vdd`/`pn_ratio` → `model_vdds_`/`model_pn_ratios_`；`<pmos>/<nmos>` → `transistor_model_*`（下标 0/1）。
3. **需要观察的现象**：`logic` 用 industry 库 + vdd=0.9，`io` 用 academia 库 + vdd=2.5，两者 PN 比（`pn_ratio`）也不同。
4. **预期结果**：你应当能用一句话说清「同一个 `TechnologyLibrary` 对象可以同时容纳多条工艺线，每条线一组独立的晶体管参数与偏差」。
5. **待本地验证**：可选——在仓库里搜 `device_model_name=`，看哪些电路模型引用了 `logic`、哪些引用了 `io`（如 IO pad 模型通常用 `io` 器件）。

#### 4.2.5 小练习与答案

**练习 1**：晶体管参数为什么用 `std::array<..., 2>` 而不是两个独立字段？
**答案**：因为 PMOS 和 NMOS 是成对出现的、语义对称的属性（名字、沟道、宽度、偏差）。用大小为 2 的数组、以 `e_tech_lib_transistor_type` 枚举当下标，能统一访问模式（`transistor_model_chan_lengths_[id][type]`），避免为每对属性写两套访问器，扩展和遍历都更整洁。

**练习 2**：`pn_ratio`（PN 比）影响什么？
**答案**：它是 PMOS 与 NMOS 的宽度比，决定了为达到对称驱动能力时 PMOS 要比 NMOS 宽多少（电子迁移率高于空穴，所以 PMOS 通常更宽）。这个比值在 SPICE 网表展开、晶体管尺寸计算时被直接消费。

### 4.3 ArchDirect：CLB 间的直连库

#### 4.3.1 概念说明

VPR 架构 XML 自带一种 `<direct>` 连接，用于两个相邻 CLB 之间的「点对点」硬连线（不走通用布线）。`ArchDirect` 是 OpenFPGA 对它的电路级补充：每条直连要用哪个电路模型实现、是列内/行内连接还是跨列/跨行连接、朝哪个方向连。

它和 `cb_switch2circuit` / `sb_switch2circuit` / `routing_seg2circuit` 一样，都是「把 VPR 侧的名字绑定到电路模型」的桥，只不过它绑定的是**直连**而非布线开关/线段。注意 `arch_direct.h` 自己的注释强调：这只是**解析期**用的数据结构，引擎核心另有自己的表示。

#### 4.3.2 核心流程

```text
<direct_connection>（openfpga_arch.xml，可选节点）
   <direct name="..." circuit_model_name="..." type="..." x_dir="..." y_dir="..."/>
        │  read_xml_direct_circuit() 解析
        ▼
   ArchDirect：每条 direct 一个 ArchDirectId
        - name           反查 VPR 里的 <direct>（用名字对账）
        - circuit_model  链接成 CircuitModelId
        - type           inner_column_or_row | part_of_cb | inter_column | inter_row
        - x_dir / y_dir  positive | negative
```

`type` 决定连接的几何属性，`x_dir`/`y_dir` 决定连接的朝向（正 x 表示连到右邻列、正 y 表示连到下邻行）。这些信息在 tile 直连构建（u9-l4）时被消费。

#### 4.3.3 源码精读

直连类型枚举——这是 VPR 原生 direct 之外 OpenFPGA 的扩展（跨列/跨行）：

> `e_direct_type` 与字符串表：[libs/libarchopenfpga/src/arch_direct.h:L17-L26](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/arch_direct.h#L17-L26) —— 四种类型：`inner_column_or_row`、`part_of_cb`、`inter_column`、`inter_row`。

方向枚举：

> `e_direct_direction`：[libs/libarchopenfpga/src/arch_direct.h:L28-L30](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/arch_direct.h#L28-L30) —— `positive`/`negative`，注释说明了正方向在列/行上的几何含义。

`ArchDirect` 类的内部存储依然是熟悉的 SoA + 名字快查：

> 内部数据与快查表：[libs/libarchopenfpga/src/arch_direct.h:L82-L107](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/arch_direct.h#L82-L107) —— `direct_ids_`、`names_`、`circuit_models_`、`types_`、`directions_`（用 `vtr::Point<e_direct_direction>` 把 x/y 方向打包成一个点），外加 `direct_name2ids_` 快查。

顺带一提，布线三段的绑定是 `Arch` 里三个独立的 `map<string, CircuitModelId>`，结构比 `ArchDirect` 简单（只需「名字 → 电路模型」一张表），见 [openfpga_arch.h:L41-L47](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/openfpga_arch.h#L41-L47)。

#### 4.3.4 代码实践

1. **实践目标**：理解直连绑定与布线绑定的差异。
2. **操作步骤**：
   - 在仓库里用搜索找含 `<direct_connection>` 或 `<direct ` 的 `openfpga_arch.xml`（`k4_N4_40nm_cc_openfpga.xml` 里**没有**这个节点，需要找更大的架构文件）。
   - 找到后，对照 arch_direct.h 的字段，标注 `name/type/x_dir/y_dir` 各落到哪个 `vtr::vector`。
3. **需要观察的现象**：直连节点同时声明了「用哪个电路模型」和「几何类型 + 方向」，而 `<connection_block>`/`<switch_block>` 节点只声明「用哪个电路模型」。
4. **预期结果**：能解释为什么直连需要 `ArchDirect` 这么一个专门的类（带 type/方向），而布线开关只需一个简单 map——因为直连有几何语义，布线开关没有。
5. **待本地验证**：待本地搜索确认哪些示例 arch 真的用了直连（如某些 carry-chain / 高密度架构）。

#### 4.3.5 小练习与答案

**练习 1**：`inter_column` 和 `part_of_cb` 有什么区别？
**答案**：`part_of_cb` 表示这条直连其实属于连接块（CB）的一部分，复用 CB 的电路；`inter_column` 表示这是一条真正跨越列的独立点对点连接，需要它自己的电路模型来实现。两者在 tile 直连构建时被区别对待。

**练习 2**：为什么 `directions_` 用 `vtr::Point<e_direct_direction>` 存？
**答案**：因为一条直连同时有 x 方向和 y 方向两个分量。用 `Point` 把 (x_dir, y_dir) 打包成一个对象，比维护两个并列数组更内聚，访问时一次取出整个「方向对」。

### 4.4 SimulationSetting 与 BitstreamSetting：Arch 之外的兄弟结构

> **本节是最容易踩坑的地方**。不少资料（甚至 `openfpga_arch.h` 的旧注释）会暗示「simulation 参数属于 Arch」，但当前源码并非如此。请以本节的源码证据为准。

#### 4.4.1 概念说明

OpenFPGA 把「电路级架构」和「流程级配置」分了家：

- `Arch`：描述 FPGA **本身长什么样**（用什么电路、怎么搭）——与具体某次运行无关，是器件的固有属性。
- `SimulationSetting`：描述**这次仿真**怎么跑（时钟频率、仿真精度、测量阈值、蒙特卡洛点数等）——同一块 FPGA 可以用不同仿真设置跑很多次。
- `BitstreamSetting`：描述**这次生成比特流**的额外约束（给某些 pb_type 硬编码比特、给某些 interconnect 指定默认路径、非 fabric 比特流等）——同一块 FPGA 也可以有不同的比特流设置。

因为这三者「变化频率」和「关注点」不同，把它们拆成三个**平级**结构、由**三个独立命令**从**三个独立文件**读取，是合理的解耦。这才是 OpenFPGA 模块化的真正边界。

#### 4.4.2 核心流程

三者的读取入口在同 一个头文件里**并列声明**，但函数签名已经透露了它们的平级关系——各自返回/填充各自的结构，互不嵌套：

```text
read_xml_openfpga_arch.h 声明了三个平级函数：
   Arch             read_xml_openfpga_arch(file)            ← 根标签 <openfpga_architecture>
   SimulationSetting read_xml_openfpga_simulation_settings(file)  ← 根标签 <openfpga_simulation_setting>
   int              read_xml_openfpga_bitstream_settings(file, BitstreamSetting&) ← 根标签 <openfpga_bitstream_setting>
```

三条 shell 命令把结果分别写进 `OpenfpgaContext` 的三个**独立**分区，互不干扰。

#### 4.4.3 源码精读

第一份证据：三个读取函数平级声明，各自对应不同根标签：

> `read_xml_openfpga_arch.h` 的三个声明：[libs/libarchopenfpga/src/read_xml_openfpga_arch.h:L16-L23](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.h#L16-L23) —— `read_xml_openfpga_arch` 返回 `Arch`；`read_xml_openfpga_simulation_settings` 返回独立的 `SimulationSetting`；`read_xml_openfpga_bitstream_settings` 填充独立的 `BitstreamSetting`。

第二份证据：`OpenfpgaContext` 里三者是平级私有成员、平级访问器：

> 三个平级 const 访问器：[openfpga/src/base/openfpga_context.h:L63-L69](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_context.h#L63-L69) —— `arch()`、`simulation_setting()`、`bitstream_setting()` 并列，互不嵌套。

> 三个平级 private 成员：[openfpga/src/base/openfpga_context.h:L191-L193](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_context.h#L191-L193) —— `arch_`、`sim_setting_`、`bitstream_setting_` 是兄弟。

第三份证据：三条 shell 命令的模板实现各自只写自己的分区：

> `read_openfpga_arch` 只写 `mutable_arch()`：[openfpga/src/base/openfpga_read_arch_template.h:L46-L47](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_read_arch_template.h#L46-L47)，并在读完后做 `check_circuit_library` / `check_config_protocol` / `check_tile_annotation` 三项一致性检查（[L55-L68](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_read_arch_template.h#L55-L68)）。

> `read_simulation_setting` 只写 `mutable_simulation_setting()`：[openfpga/src/base/openfpga_read_arch_template.h:L121-L122](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_read_arch_template.h#L121-L122)。

> `read_bitstream_setting` 只写 `mutable_bitstream_setting()`：[openfpga/src/base/openfpga_read_arch_template.h:L183-L193](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_read_arch_template.h#L183-L193)，还支持 `--append` 选项决定是追加还是先清空再读。

现在打开 `SimulationSetting` 的内容。它管理「运行期」时钟与仿真控制量：

> 仿真信号类型与精度类型枚举：[libs/libarchopenfpga/src/simulation_setting.h:L20-L41](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/simulation_setting.h#L20-L41) —— `rise`/`fall` 信号方向，`frac`/`abs` 精度类型（精度可取时钟频率的分数，或绝对值）。

> 运行/编程时钟频率（用 `vtr::Point<float>` 把两者打包）：[libs/libarchopenfpga/src/simulation_setting.h:L156-L163](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/simulation_setting.h#L156-L163) —— `x()` 存运行时钟频率、`y()` 存编程时钟频率。这正呼应了 u3-l4 的概念：配置协议有自己的（较慢的）编程时钟。

> 多仿真时钟（每个时钟有独立名字/端口/频率/是否编程时钟/是否移位寄存器时钟）：[libs/libarchopenfpga/src/simulation_setting.h:L173-L178](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/simulation_setting.h#L173-L178)。

运行时钟频率 \(f_{op}\) 与编程时钟频率 \(f_{prog}\) 是两个独立量：通常 \(f_{prog} \ll f_{op}\)，因为把比特流串行/半并行灌进配置位远慢于正常工作。如果用频率分数表示精度，仿真步长 \(\Delta t\) 与时钟周期 \(T\) 的关系为：

\[
\Delta t = \alpha \cdot T = \alpha \cdot \frac{1}{f}, \quad 0 < \alpha < 1
\]

其中 \(\alpha\) 即 `frac` 精度下用户给的分数（如 0.5 表示每半周期一个点）。

`BitstreamSetting` 的内容更贴近「比特流生成的约束」，关键字段可从访问器一眼看出：pb_type 硬编码比特、interconnect 默认路径、clock routing、非 fabric 比特流、比特流覆写——见 [libs/libarchopenfpga/src/bitstream_setting.h:L96-L169](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/bitstream_setting.h#L96-L169)。它的根标签是 `<openfpga_bitstream_setting>`，与 `Arch` 的 `<openfpga_architecture>` 完全不同。

#### 4.4.4 代码实践

1. **实践目标**：用源码证明 `SimulationSetting` / `BitstreamSetting` 与 `Arch` 平级、不在 `Arch` 内。
2. **操作步骤**：
   - 在 [openfpga_arch.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/openfpga_arch.h) 全文搜 `simulation` / `Simulation` / `bitstream`（不算注释），确认 `struct Arch` 内部确实没有这两个成员。
   - 在 [openfpga_context.h:L191-L193](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_context.h#L191-L193) 确认三者是并列 private 成员。
   - 在 [read_xml_openfpga_arch.cpp:L133-L196](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp#L133-L196) 确认仿真/比特流设置各有独立解析函数、独立根标签。
3. **需要观察的现象**：`read_xml_openfpga_arch()` 的函数体里只构造并返回 `Arch`，从不触碰 simulation/bitstream。
4. **预期结果**：你能明确说出「`Arch` 在 context 里的分区是 `arch_`，仿真设置是 `sim_setting_`，比特流设置是 `bitstream_setting_`，三者是兄弟」。
5. **待本地验证**：可选——跑 `openfpga` 交互模式，分别 `read_openfpga_arch -f ...`、`read_simulation_setting -f ...`、`read_bitstream_setting -f ...` 三条命令，观察它们是三条独立命令（再次印证三个独立入口）。

#### 4.4.5 小练习与答案

**练习 1**：为什么把 `SimulationSetting` 从 `Arch` 里拆出来？
**答案**：因为「FPGA 是什么」和「这次仿真怎么跑」是两个正交的关心点。同一份 `Arch`（同一块 FPGA）可以用无数种仿真设置反复跑；如果把仿真设置塞进 `Arch`，每次换设置都得重建/复制整个 `Arch`，既浪费也破坏 `Arch` 的只读不变量。拆开后，`Arch` 保持稳定只读，仿真设置可以随意更换。

**练习 2**：运行时钟和编程时钟为什么频率不同？
**答案**：运行时钟驱动用户电路的正常翻转，频率尽量高；编程时钟驱动配置位的写入（scan chain 串行移位、或 memory bank 寻址写入），受配置电路拓扑限制通常慢得多。所以 `SimulationSetting` 把两者分别存为 `vtr::Point` 的 x/y。

**练习 3**：`BitstreamSetting` 的 `--append` 选项有什么用？
**答案**：允许把多份比特流设置文件累积读入同一个 `BitstreamSetting`（不清空已有内容）。这样可以把「通用约束」和「某次运行特有约束」拆成多个文件，按需叠加。

## 5. 综合实践

把本讲的知识串成一条「从 XML 到内存对象」的追踪线。

**任务**：选定样例 [k4_N4_40nm_cc_openfpga.xml](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml)，画一张「三栏对照图」：

| XML 顶层节点 | Arch 成员（C++） | 填充它的解析/链接函数 |
| --- | --- | --- |

填表时要求：

1. 覆盖 `Arch` 全部 10 个成员，对样例里**缺失**的节点（`<direct_connection>`、`<tile_annotations>`）也要列出，并标注「样例未提供，成员为空」。
2. 对 `circuit_tech_binding`，写出它的数据来源（每个 `<circuit_model>` 内的 `<device_technology>`）和派生函数（`bind_circuit_model_to_technology_model`）。
3. 单独画第二张表，列出**不在 Arch 内**的两个兄弟结构 `SimulationSetting`、`BitstreamSetting`：它们的根标签、读取命令、在 `OpenfpgaContext` 中的成员名。
4. 最后用一段话回答：「如果我想改仿真时钟频率，要动 `Arch` 吗？」——用本讲的结论给出否定的理由。

这道题做完，你就真正掌握了 `Arch` 的组成边界、它与兄弟结构的区分，以及 OpenFPGA「解析存名字→链接查 ID→只读冻结」的统一套路。

## 6. 本讲小结

- `openfpga::Arch` 是电路级架构的**只读聚合根**，含 10 个成员，分别由 `openfpga_arch.xml` 的各顶层节点解析填充，构建后全引擎只读、以保证模块化。
- `circuit_tech_binding` 是唯一不直填的派生成员，由 `bind_circuit_model_to_technology_model()` 在链接阶段从每个电路模型的 `<device_technology>` 派生。
- `TechnologyLibrary` 用 SoA + 强类型 ID 组织工艺器件（晶体管/RRAM）与工艺偏差，靠「名字 → ID」两段式链接，是 SPICE 网表展开的依据。
- `ArchDirect` 描述 CLB 间直连的电路模型 + 几何类型/方向，比布线三段的简单 map 多了 type/方向语义。
- **关键纠正**：`SimulationSetting` 与 `BitstreamSetting` **不属于 `Arch`**，它们是 `OpenfpgaContext` 里的平级兄弟结构，有各自的根标签、读取命令与文件；`openfpga_arch.h` 旧注释里「simulation parameters」的说法已过时，以结构体定义为准。
- 「解析存名字 → 链接查 ID → 只读冻结」是 `Arch` 及其各子库共享的统一设计范式。

## 7. 下一步学习建议

- 想看「链接阶段」到底怎么把名字翻译成跨库 ID，进 **u5-l2（openfpga_arch XML 解析）**，那里会逐个打开 `read_xml_*` 子解析器与 `*_xml_constants.h`。
- 想看 `Arch` 如何与 VPR 跑完的 device 数据对接，进 **u5-l3（link_openfpga_arch）**——那是 `Arch` 从「孤立的电路级账本」变成「绑定到具体器件的账本」的桥梁。
- 想深入 `pb_type_annotations` 如何与 VPR pb graph 对账，回顾 **u4-l3**，并留意 u5-l3/l4 里 `VprDeviceAnnotation` 如何承载对账结果。
- 对 `SimulationSetting` 如何被 testbench 生成消费感兴趣，可提前翻 **u8-l2（Testbench 生成）**，看仿真时钟与测量阈值如何写进生成的 testbench。
