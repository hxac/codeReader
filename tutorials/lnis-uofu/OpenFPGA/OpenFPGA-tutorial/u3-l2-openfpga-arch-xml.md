# openfpga_arch.xml 总体结构

## 1. 本讲目标

在上一讲（u3-l1）中，我们建立了这样一张地图：OpenFPGA 需要吃进**两份**架构 XML——VPR 架构 XML 描述「器件长什么样」，而 `openfpga_arch.xml` 描述「用什么电路把它搭出来」。本讲要打开 `openfpga_arch.xml` 这个黑盒。

学完本讲，你应该能够：

1. 说出 `openfpga_arch.xml` 根标签 `<openfpga_architecture>` 下的主要子节点有哪些，以及各自的作用。
2. 把 XML 里每一段，对应到 C++ 数据结构 `openfpga::Arch` 里的具体字段。
3. 理解「电路模型」是如何通过 `connection_block` / `switch_block` / `routing_segment` 三段，**用名字绑定**到 VPR 的布线结构上的。
4. 读懂 `configuration_protocol` 段，并明确指出配置链（scan_chain）是在**哪个节点**声明的。
5. 看懂 `pb_type_annotations` 如何把 VPR 的逻辑块（clb/io）映射到物理电路模型，并理解 `mode_bits` 的含义。

本讲只讲**总体结构**。`circuit_library` 的细节（端口、设计技术、模型间引用）留给 u3-l3；`configuration_protocol` 的各类型对比留给 u3-l4；VPR 侧的真正绑定发生在 `link_openfpga_arch`，留给 u5-l3。

## 2. 前置知识

### 2.1 你需要先有的概念

- **XML 树状结构**：XML 用嵌套的标签表达层级关系，例如 `<父><子 属性="值"/></父>`。本讲的 `openfpga_arch.xml` 是一棵以 `<openfpga_architecture>` 为根的树。
- **电路模型（circuit model）**：一块可复用的物理电路，例如一个反相器 `INVTX1`、一个多路选择器 `mux_tree_tapbuf`、一个 D 触发器 `DFF`。它有一个 `name` 属性，是别处引用它的「身份证号」。
- **配置存储（configurable memory）**：FPGA 之所以「可编程」，是因为片上有大量存储单元（位）来控制开关、查找表的内容。这些位怎么组织、怎么写入，由**配置协议**决定。
- **pb_type**：VPR 架构里描述逻辑块内部层级结构的类型（physical block type），例如一个 `clb`（可配置逻辑块）里包含若干 `fle`（灵活逻辑单元），`fle` 里又有 `lut4` 和 `ff`。

### 2.2 承接上一讲的关键结论

u3-l1 给出了两份文件的职责边界与**同名绑定**思想。本讲要落到具体的 XML 节点和 C++ 字段上，回答三个问题：

- 根标签下到底有哪些段？（**结构**）
- 这些段里的电路模型，怎么和 VPR 的布线开关、线段对上号？（**布线绑定**）
- VPR 的逻辑块，怎么对应到物理电路？（**pb_type 注解**）

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用途 |
| --- | --- | --- |
| `openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml` | 一份真实的 `openfpga_arch.xml` 样本 | 全程用它做实例，逐段拆解 |
| `libs/libarchopenfpga/src/openfpga_arch.h` | C++ 核心数据结构 `openfpga::Arch` | 看 XML 各段对应到哪些字段 |
| `libs/libarchopenfpga/src/openfpga_arch_linker.h` / `.cpp` | 「名字 → ID」的链接器 | 看字符串名如何被解析成内部 ID |
| `libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp` | XML 解析主入口 | 看各段被读取的先后顺序 |
| `libs/libarchopenfpga/src/config_protocol.h` | 配置协议数据结构 | 理解 `configuration_protocol` 段 |
| `libs/libarchopenfpga/src/circuit_types.h` | 协议类型枚举 | 看 `scan_chain` 等字符串常量 |
| `libs/libarchopenfpga/src/pb_type_annotation.h` | pb_type 注解数据结构 | 理解 `pb_type_annotations` 段 |

> 提示：本讲引用的所有行号均对应 HEAD `a1e51333d`。

## 4. 核心概念与源码讲解

### 4.1 顶层结构：`<openfpga_architecture>` 与 C++ `Arch` 数据模型

#### 4.1.1 概念说明

一份 `openfpga_arch.xml` 的根标签固定是 `<openfpga_architecture>`。它本身不带属性，作用只是一个**容器**，把若干职责不同的子段装在一起。这些子段大致分两类：

- **定义类**：声明有哪些「积木」可用——`technology_library`（晶体管工艺器件）、`circuit_library`（电路模型库）。
- **绑定类**：声明这些积木**用在哪里**——`configuration_protocol`（配置存储怎么组织）、`connection_block` / `switch_block` / `routing_segment`（布线结构用哪些电路）、`pb_type_annotations`（逻辑块用哪些电路）。

这棵 XML 树最终会被解析成一个 C++ 结构体 `openfpga::Arch`。它的注释明确写着：**这个结构一旦由 `read_xml_openfpga_arch()` 建好，就应当是只读的**。这是一条贯穿 OpenFPGA 的设计纪律——架构数据解析后冻结，保证下游模块拿到的都是不可变快照。

#### 4.1.2 核心流程

整体流向是「XML 文件 → 解析器 → 内存中的 `Arch` 结构」：

```text
openfpga_arch.xml
   <openfpga_architecture>            ──►  openfpga::Arch
     ├─ technology_library            ──►    tech_lib            （工艺器件库）
     ├─ circuit_library               ──►    circuit_lib         （电路模型库）
     │                                        + circuit_tech_binding（电路↔工艺绑定）
     ├─ configuration_protocol        ──►    config_protocol     （配置协议）
     ├─ connection_block              ──►    cb_switch2circuit   （CB开关名→电路）
     ├─ switch_block                  ──►    sb_switch2circuit   （SB开关名→电路）
     ├─ routing_segment               ──►    routing_seg2circuit （线段名→电路）
     └─ pb_type_annotations           ──►    pb_type_annotations （pb_type→电路）
```

解析顺序由 `read_xml_openfpga_arch()` 决定，**有先后约束**：必须先读 `circuit_library`，才能去解析其它「绑定类」段——因为后者要用名字（如 `DFF`、`mux_tree_tapbuf`）反查电路库里对应的模型 ID。

#### 4.1.3 源码精读

先看 XML 的根与第一层子节点（节选关键骨架）：

[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:10-10](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L10-L10) 是根标签 `<openfpga_architecture>` 的起点，整份文件从这一行开始装容器。

[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:11-30](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L11-L30) 是 `technology_library` 段；[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:31-161](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L31-L161) 是 `circuit_library` 段；[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:162-164](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L162-L164) 是 `configuration_protocol` 段；[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:165-173](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L165-L173) 是三个布线绑定段 `connection_block`/`switch_block`/`routing_segment`；[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:174-190](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L174-L190) 是 `pb_type_annotations` 段。

再看 C++ 这一侧，`Arch` 结构把上面这些段一一对应成成员：

[libs/libarchopenfpga/src/openfpga_arch.h:25-64](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/openfpga_arch.h#L25-L64) 定义了整个 `struct Arch`。注意几个关键映射：`circuit_lib`（第 27 行）对应 `circuit_library` 段；`tech_lib`（第 30 行）对应 `technology_library` 段；`config_protocol`（第 36 行）对应 `configuration_protocol` 段；`cb_switch2circuit` / `sb_switch2circuit`（第 41–42 行）对应 `connection_block` / `switch_block` 段；`routing_seg2circuit`（第 47 行）对应 `routing_segment` 段；`pb_type_annotations`（第 63 行）对应同名段。文件头部的注释（第 17–24 行）再次强调「建好后只读」。

解析入口把 XML 节点喂给各子解析器，顺序如下：

[libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp:49-51](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp#L49-L51) 拿到根节点 `<openfpga_architecture>`。[libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp:56-68](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp#L56-L68) 先解析 `circuit_library` 并构建其内部链接与时序图；之后第 71–79 行才解析 `technology_library` 并完成绑定。布线三段与 pb_type 注解在第 96–121 行才解析——此时 `circuit_lib` 已经就绪，名字反查才有意义。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：建立「XML 段 ↔ C++ 字段」的对照表。
2. **操作步骤**：打开 `libs/libarchopenfpga/src/openfpga_arch.h` 第 25–64 行，逐个字段对照 `k4_N4_40nm_cc_openfpga.xml` 的顶层子节点。
3. **观察现象**：你会发现几乎每个顶层 XML 节点都能在 `Arch` 里找到一个同义成员；只有 `technology_library` 比较特殊，它没有单独的「绑定段」，而是通过 `circuit_tech_binding`（第 33 行）这张表，把每个电路模型挂到工艺器件上。
4. **预期结果**：得到一张 7 行左右的对照表（XML 节点 → C++ 字段）。
5. 若想进一步验证，可在 `read_xml_openfpga_arch.cpp` 第 35–128 行确认每个字段被赋值的位置。

#### 4.1.5 小练习与答案

**练习 1**：如果有人把 `circuit_library` 段整个删掉，但保留 `configuration_protocol` 段，解析还能成功吗？为什么？

**参考答案**：不能成功。`configuration_protocol` 里的 `circuit_model_name="DFF"` 要在电路库里反查模型 ID（详见 4.3）。电路库缺失会导致名字解析失败、链接器报错退出。

**练习 2**：`Arch` 结构里有 `tile_annotations`（第 57 行）字段，但本讲的样本 XML 里没有 `<tile_annotations>` 段。这说明什么？

**参考答案**：说明该段是**可选的**。`tile_annotations` 用于声明 tile 级全局端口，简单架构不需要，故缺省。`pb_type_annotations` 则是几乎所有 `openfpga_arch.xml` 都会出现的段。

---

### 4.2 物理基础：`technology_library` 与 `circuit_library`

#### 4.2.1 概念说明

这两段回答「积木本身是什么」。

- **`technology_library`**：描述晶体管级的**工艺器件**。例如本例声明了 `logic` 和 `io` 两套器件模型，分别引用 PTM 45nm 工艺库，给出 VDD、沟道长度、最小宽度、工艺偏差等参数。它主要服务于 SPICE 仿真（生成晶体管级网表时需要知道用哪个管子），对纯数字 Verilog 流程影响较小。
- **`circuit_library`**：描述**电路模型**。这是 OpenFPGA 真正频繁引用的「积木库」。本例定义了反相器/缓冲器（`INVTX1`、`buf4`、`tap_buf4`）、传输门（`TGATE`）、布线线（`chan_segment`）、直连导线（`direct_interc`）、多路选择器（`mux_tree`、`mux_tree_tapbuf`）、触发器（`DFFSRQ`、`DFF`）、查找表（`lut4`）、IO 焊盘（`GPIO`）。

每个电路模型通过 `<device_technology device_model_name="logic"/>` 声明它用哪套工艺器件——这就是「电路 ↔ 工艺」的绑定。

#### 4.2.2 核心流程

```text
technology_library                  circuit_library
  device_model "logic" ──────────── circuit_model 的 device_technology
  device_model "io"                  device_model_name="logic"/"io"
        │                                   │
        └────────────► Arch.circuit_tech_binding[CircuitModelId] = TechnologyModelId
```

`bind_circuit_model_to_technology_model()` 遍历每个电路模型，读它的 `device_model_name`，在工艺库里查到对应的 `TechnologyModelId`，填进 `circuit_tech_binding` 这张映射表。

#### 4.2.3 源码精读

工艺库骨架：

[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:13-18](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L13-L18) 声明了 `logic` 这套器件模型：引用 PTM 45nm 库，VDD 0.9V，PMOS/NMOS 沟道 40nm。第 19–24 行另声明一套 `io` 器件（VDD 2.5V，用于 IO 焊盘的更高电压域）。

电路模型示例：

[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:32-43](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L32-L43) 定义反相器 `INVTX1`，注意它有 `is_default="true"`——意味着「在需要反相器/缓冲但没显式指定时，默认用它」。第 34 行 `<device_technology device_model_name="logic"/>` 把它挂到 `logic` 工艺器件上。

绑定逻辑：

[libs/libarchopenfpga/src/openfpga_arch_linker.cpp:62-85](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/openfpga_arch_linker.cpp#L62-L85) 是 `bind_circuit_model_to_technology_model`：遍历所有电路模型（第 66–67 行），取其 `device_model_name`（第 68–69 行），在工艺库查找（第 74–75 行），查不到就报错退出（第 76–81 行），查到则写入绑定表（第 83 行）。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：验证「电路模型 → 工艺器件」绑定关系。
2. **操作步骤**：在 XML 中找出所有 `<device_technology device_model_name="..."/>` 行，统计它们分别引用了哪套器件；再读 linker 的第 66–84 行确认这张表是怎么填的。
3. **观察现象**：你会看到所有逻辑电路都引用 `logic`，唯独 IO 相关电路会引用 `io`。
4. **预期结果**：明确「两套器件分别服务谁」。

#### 4.2.5 小练习与答案

**练习**：`INVTX1` 和 `buf4` 都是 `inv_buf` 类型，它们都引用 `logic` 器件。如果你想做一颗低功耗 FPGA，把核心反相器换成更小尺寸的工艺，应该改 XML 的哪一段？

**参考答案**：应改 `technology_library` 里 `logic` 这套 `device_model` 的 `pmos`/`nmos` 尺寸参数（如 `min_width`），或干脆引用另一套工艺库文件。由于所有逻辑电路都通过 `device_model_name="logic"` 间接引用它，改这一处即可全局生效——这正是「工艺库与电路库解耦」的好处。

---

### 4.3 配置协议 `configuration_protocol`：scan_chain 在这里声明

#### 4.3.1 概念说明

`configuration_protocol` 段回答一个核心问题：**片上那么多可编程位，用什么电路来存储、用什么方式来写入？** 本讲样本用的是最经典的**配置链（scan_chain）**：把所有配置位串成一条长长的移位寄存器链，从一头逐位移入。这正是文件名里的 `cc`（configuration chain）的含义（见 u3-l1）。

> **关键结论（直接回答本讲核心问题）**：配置链 `scan_chain` 是在 **`<configuration_protocol>`** 节点下声明的，具体是它的子节点 `<organization type="scan_chain" circuit_model_name="DFF"/>`。`type` 决定组织方式，`circuit_model_name` 决定用哪个电路模型当配置存储单元。

#### 4.3.2 核心流程

`configuration_protocol` 段的解析涉及一次重要的「**名字 → ID**」转换：

```text
XML:  <organization type="scan_chain" circuit_model_name="DFF"/>
        │                              │
        │  type 字符串                  │  名字字符串 "DFF"
        ▼                              ▼
   e_config_protocol_type          circuit_lib.model("DFF")
   = CONFIG_MEM_SCAN_CHAIN         = CircuitModelId
        │                              │
        └──────────► Arch.config_protocol.type_ / .memory_model_
```

这一步由链接器 `link_config_protocol_to_circuit_library()` 完成：它把字符串 `"DFF"` 在电路库里查成真正的模型 ID，再存回 `ConfigProtocol`。**名字到 ID 的解析集中在链接阶段**，是 OpenFPGA 解析架构的统一风格。

#### 4.3.3 源码精读

XML 中配置协议的声明：

[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:162-164](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L162-L164) 是整个 `configuration_protocol` 段。其中第 163 行 `<organization type="scan_chain" circuit_model_name="DFF"/>` 就是配置链的声明处：`type="scan_chain"` 表示用配置链组织，`circuit_model_name="DFF"` 表示每个配置位用一个 `DFF`（ccff 类型）电路实现。

被引用的 `DFF` 模型本身定义在电路库里：

[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:143-151](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L143-L151) 定义了 `DFF`，注意它的 `type="ccff"`（configuration-chain flip-flop，专用于配置链），并有一个 `is_prog="true"` 的 `prog_clk` 端口——这是配置链移位所用的编程时钟。

字符串 `type` 到枚举的映射规则定义在：

[libs/libarchopenfpga/src/circuit_types.h:140-153](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_types.h#L140-L153) 给出了枚举 `e_config_protocol_type`（第 140–148 行）和对应的字符串常量数组 `CONFIG_PROTOCOL_TYPE_STRING`（第 150–153 行）。可以看到合法的 `type` 取值有 `standalone`、`scan_chain`、`memory_bank`、`ql_memory_bank`、`frame_based`、`feedthrough`。`scan_chain` 正好对应枚举 `CONFIG_MEM_SCAN_CHAIN`（第 142 行）。

解析该段的代码：

[libs/libarchopenfpga/src/read_xml_config_protocol.cpp:146-166](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_config_protocol.cpp#L146-L166) 的 `read_xml_config_organization` 读取 `<organization>` 节点，第 164–165 行把 `circuit_model_name` 属性（即 `"DFF"`）作为字符串存进 `memory_model_name_`。

随后链接器把字符串解析成 ID：

[libs/libarchopenfpga/src/openfpga_arch_linker.cpp:14-26](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/openfpga_arch_linker.cpp#L14-L26) 的 `link_config_protocol_to_circuit_library`：第 15–16 行用 `memory_model_name()`（字符串 `"DFF"`）调用 `circuit_lib.model(...)` 查到 `CircuitModelId`；查不到（第 19–24 行）就报错退出；查到则在第 26 行 `set_memory_model()` 写回真正的 ID。

存这些数据的 C++ 类：

[libs/libarchopenfpga/src/config_protocol.h:25-144](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/config_protocol.h#L25-L144) 定义了 `ConfigProtocol` 类。其中第 106 行 `type_` 存协议类型枚举，第 109–110 行同时存了名字（`memory_model_name_`）和解析后的 ID（`memory_model_`）。第 121–140 行的 BL/WL 相关字段**只对 memory_bank 类协议有效**——scan_chain 用不到，本讲不展开（留 u3-l4）。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：亲手验证 scan_chain 的声明位置与名字解析链路。
2. **操作步骤**：
   - 在 `k4_N4_40nm_cc_openfpga.xml` 第 162–164 行确认配置协议段。
   - 跟着 `DFF` 这个名字，跳到电路库第 143 行看它的定义。
   - 再读 `openfpga_arch_linker.cpp` 第 14–26 行，理解 `"DFF"` 如何变成 ID。
3. **观察现象**：一条配置链协议，在 XML 里只需一行声明 + 电路库里一个 ccff 模型，就足够了。
4. **预期结果**：能用一句话讲清「scan_chain 在哪声明、用什么模型、模型在哪定义、名字怎么变成 ID」。
5. **待本地验证**：若想看运行效果，可在 build 后用 `openfpga -f` 跑一个 cc 任务，观察日志中是否出现 `Read OpenFPGA architecture` 与配置协议相关的计时信息。

#### 4.3.5 小练习与答案

**练习 1**：如果把第 163 行的 `circuit_model_name="DFF"` 改成一个不存在的名字（如 `"DFF_xyz"`），会发生什么？

**参考答案**：解析阶段不会立即报错（因为只是存了个字符串），但链接器 `link_config_protocol_to_circuit_library` 在第 19–24 行会发现 `model("DFF_xyz")` 返回 `INVALID`，打印 `Invalid memory model name` 并 `exit(1)`。这就是「解析存名字、链接查 ID」两段式设计的报错点。

**练习 2**：`type` 一共可以取哪几种值？请从 `circuit_types.h` 找出来。

**参考答案**：六种——`standalone`、`scan_chain`、`memory_bank`、`ql_memory_bank`、`frame_based`、`feedthrough`（见 `circuit_types.h` 第 151–153 行）。

---

### 4.4 布线绑定三段：`connection_block` / `switch_block` / `routing_segment`

#### 4.4.1 概念说明

这三段回答「VPR 布线结构里的开关和线段，分别用电路库里的哪个模型实现」。它们是 u3-l1 所说的**同名绑定**的主战场：

- **`connection_block`**：连接块（CB）把布线通道连到逻辑块的输入引脚。VPR 架构里给 CB 开关起了名字（本例叫 `ipin_cblock`），这里声明它用 `mux_tree_tapbuf` 这个多路选择器电路实现。
- **`switch_block`**：开关盒（SB）是布线通道之间的交汇点。VPR 给 SB 开关命名（本例名字就是 `"0"`），这里声明它也用 `mux_tree_tapbuf` 实现。
- **`routing_segment`**：布线线段。VPR 定义了长度为 4 的线段 `L4`，这里声明它用 `chan_segment` 这个布线线电路实现。

> 关键点：这三段里，**`name` 是 VPR 侧的名字**（要和 VPR 架构 XML 里的 `<switch>`/`<segment>` 名字对得上），**`circuit_model_name` 是 OpenFPGA 电路库里的名字**。解析阶段，`circuit_model_name` 会被解析成 `CircuitModelId`；而 `name` 作为字符串键**原样保留**，留到 `link_openfpga_arch`（u5-l3）阶段再去和 VPR 的 device context 对账——如果 VPR 那边根本没有 `ipin_cblock` 这个开关，链接阶段才会报错。

#### 4.4.2 核心流程

```text
  <connection_block>                      <switch_block>                  <routing_segment>
   switch name="ipin_cblock"               switch name="0"                 segment name="L4"
          circuit_model_name=                circuit_model_name=            circuit_model_name=
             "mux_tree_tapbuf"                  "mux_tree_tapbuf"              "chan_segment"
        │                                          │                              │
        │  name 字符串保留 / model 名解析成 ID      │ 同左                          │ 同左
        ▼                                          ▼                              ▼
   Arch.cb_switch2circuit                Arch.sb_switch2circuit        Arch.routing_seg2circuit
   {"ipin_cblock": CircuitModelId}       {"0": CircuitModelId}         {"L4": CircuitModelId}
        │                                          │                              │
        └────────── 待 link_openfpga_arch 阶段，用 name 去 VPR device context 对账 ──┘
```

这三个映射在 C++ 里都是 `std::map<std::string, CircuitModelId>`：键是 VPR 名字，值是电路模型 ID。

#### 4.4.3 源码精读

XML 三段：

[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:165-167](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L165-L167) 是 `connection_block`：第 166 行声明 CB 开关 `ipin_cblock` 用 `mux_tree_tapbuf` 实现。[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:168-170](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L168-L170) 是 `switch_block`：第 169 行声明 SB 开关 `"0"` 用 `mux_tree_tapbuf` 实现。[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:171-173](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L171-L173) 是 `routing_segment`：第 172 行声明线段 `L4` 用 `chan_segment` 实现。

被反复引用的 `mux_tree_tapbuf`：

[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:111-119](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L111-L119) 定义了 `mux_tree_tapbuf`，它被 `is_default="true"` 标记为默认 mux 模型，输入/输出 buffer 分别用 `INVTX1` 与 `tap_buf4`，传输门用 `TGATE`。CB 和 SB 都复用它——这就是「同一种电路模型被多处绑定」的典型。

C++ 这一侧，三个 `map` 字段：

[libs/libarchopenfpga/src/openfpga_arch.h:41-47](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/openfpga_arch.h#L41-L47) 定义了 `cb_switch2circuit`（第 41 行）、`sb_switch2circuit`（第 42 行）、`routing_seg2circuit`（第 47 行）三个映射，注释（第 38–46 行）明确说明它们是「从布线开关/线段的**名字**到电路库模型的映射」。

解析这三段的入口：

[libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp:96-109](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp#L96-L109) 依次调用 `read_xml_cb_switch_circuit`、`read_xml_sb_switch_circuit`、`read_xml_routing_segment_circuit` 填充上述三个 map（注意第 96–101 行 CB 段被读取了两次，属于源码中的冗余调用，不影响结果）。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：体会「VPR 名字 ↔ 电路模型」绑定，并理解为何 CB/SB 能共用同一模型。
2. **操作步骤**：
   - 在 XML 第 165–173 行找到三段绑定。
   - 打开配套的 VPR 架构文件 `openfpga_flow/vpr_arch/k4_N4_tileable_40nm.xml`，在其中搜索 `ipin_cblock`、开关 `0`、线段 `L4`，确认这些名字确实来自 VPR 侧。
3. **观察现象**：VPR 架构里定义了开关/线段的名字与电气参数，而 `openfpga_arch.xml` 只是用这些名字「指认」该用哪个电路。
4. **预期结果**：明确「`name` 来自 VPR，`circuit_model_name` 来自 OpenFPGA 电路库」。
5. **待本地验证**：把某段里的 `name` 改成 VPR 里不存在的名字，跑流程时应在 `link_openfpga_arch` 阶段（而非 `read_openfpga_arch`）报绑定失败。

#### 4.4.5 小练习与答案

**练习**：为什么 `connection_block` 和 `switch_block` 可以都用 `mux_tree_tapbuf`，而 `routing_segment` 却用 `chan_segment`？类型上有什么本质区别？

**参考答案**：CB 和 SB 的核心元件都是**多路选择器**（决定把哪个输入连到输出），所以都用 mux 类电路模型；而布线线段本质是一段**导线**（带 RC 寄生），用 `chan_wire` 类型的 `chan_segment` 电路描述其 RC 模型。二者类型不同，绑定的电路模型自然不同。

---

### 4.5 `pb_type_annotations`：从 VPR 逻辑块到物理电路模型

#### 4.5.1 概念说明

VPR 的 pb_type 描述逻辑块的**逻辑层级**（一个 clb 怎样由若干 fle 组成、fle 怎样含 lut4 和 ff），但它不关心这些用具体什么电路实现。`pb_type_annotations` 段就是补上这一层：**把 VPR 的 pb_type 路径，绑定到 `openfpga_arch.xml` 电路库里的具体电路模型**。

这里有两个关键概念（u4-l3 会深入）：

- **operating pb_type vs physical pb_type**：VPR 的 pb_type 可能有多种「模式」（mode），其中一种模式是真正的物理实现（physical mode），其它是「工作模式」（operating mode）。注解要把工作模式映射回物理模式。
- **`mode_bits`**：当一个电路有多种工作模式时，需要几位配置位来「选模式」。`mode_bits` 就是这些选择位的取值。例如 IO 焊盘是输入还是输出，由 `mode_bits` 决定。

#### 4.5.2 核心流程

以本例的 IO 块为例，三个注解协同完成映射：

```text
  ① <pb_type name="io" physical_mode_name="physical" idle_mode_name="inpad"/>
        ── 声明 io 的物理模式叫 "physical"，空闲模式叫 "inpad"

  ② <pb_type name="io[physical].iopad" circuit_model_name="GPIO" mode_bits="1"/>
        ── 物理模式下的 iopad 用 GPIO 电路实现

  ③ <pb_type name="io[inpad].inpad"  physical_pb_type_name="io[physical].iopad" mode_bits="1"/>
     <pb_type name="io[outpad].outpad" physical_pb_type_name="io[physical].iopad" mode_bits="0"/>
        ── 把工作模式 inpad/outpad 都映射到同一个物理 iopad，
           靠 mode_bits（1=输入, 0=输出）来区分方向
```

CLB 这边更简单：直接把 `lut4`、`ff` 绑定到电路模型 `lut4`、`DFFSRQ`。

#### 4.5.3 源码精读

XML 中的 IO 注解：

[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:174-180](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L174-L180) 是 IO 块的 pb_type 注解。第 176 行声明 `io` 的物理模式与空闲模式；第 177 行把物理 `iopad` 绑定到 `GPIO`；第 178–179 行把工作模式的 `inpad`/`outpad` 映射回物理 `iopad`，并用 `mode_bits` 区分方向（输入 `1`、输出 `0`）。这也解释了 u3-l1 提到的「inpad/outpad 为何都映射到同一个 GPIO」——同一物理焊盘，靠配置位切换方向。

CLB 注解：

[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:183-188](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L183-L188) 是 CLB 的注解。第 185 行把 `clb` 内部名为 `crossbar` 的 interconnect（互连）绑定到 `mux_tree` 模型；第 187 行把 `lut4` 绑定到 `lut4` 电路；第 188 行把 `ff` 绑定到 `DFFSRQ`。注意第 187 行的路径语法 `clb.fle[n1_lut4].ble4.lut4`——方括号里 `n1_lut4` 是沿途的**模式名**，这正是 u3-l1 提到的 pb_type 路径语法。

C++ 数据结构：

[libs/libarchopenfpga/src/pb_type_annotation.h:33-191](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/pb_type_annotation.h#L33-L191) 定义了 `PbTypeAnnotation` 类。注意第 119 行 `mode_bits_`（配置选择位）、第 124 行 `circuit_model_name_`（绑定的电路模型名）。文件顶部注释（第 17–28 行）说明：这个类**只存 XML 的原始数据**（operating/physical 名字、mode_bits、电路模型名、interconnect 绑定），不做跨结构链接——链接交给后续专门的标注模块（u5）。第 185–186 行的 `operating_pb_type_ports_` 与第 190 行的 `interconnect_circuit_model_names_` 分别保存端口映射和互连绑定。

解析入口：

[libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp:120-121](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp#L120-L121) 调用 `read_xml_pb_type_annotations`，把每个 `<pb_type>` 节点解析成一个 `PbTypeAnnotation` 对象，存入 `Arch.pb_type_annotations`（一个 vector）。

#### 4.5.4 代码实践（源码阅读型）

1. **实践目标**：读懂 operating↔physical 映射与 mode_bits 的作用。
2. **操作步骤**：
   - 对照 XML 第 176–179 行，画出 `io` 的三种 pb_type（physical 的 `iopad`、operating 的 `inpad`/`outpad`）之间的映射箭头。
   - 在 `PbTypeAnnotation` 类里找到存放 `physical_mode_name`、`mode_bits`、`circuit_model_name` 的成员（分别是第 113、119、124 行）。
3. **观察现象**：一个 `PbTypeAnnotation` 对象只描述**一个** pb_type 节点的注解；要表达 IO 这套映射，需要 3 个对象（第 176、177、178/179 行各对应一个或多个）。
4. **预期结果**：能解释「为什么 inpad 和 outpad 共用一个 GPIO 焊盘却有不同的 mode_bits」。

#### 4.5.5 小练习与答案

**练习 1**：路径 `clb.fle[n1_lut4].ble4.lut4` 里的 `n1_lut4` 是 pb_type 名还是模式名？

**参考答案**：是**模式名**。pb_type 路径语法中，方括号 `[...]` 标注的是上一级 pb_type 进入下一级时所走的模式。`PbTypeAnnotation` 用 `operating_parent_mode_names_` / `physical_parent_mode_names_`（见头文件第 105、109 行）分别保存沿途的模式名序列。

**练习 2**：如果只写了第 187 行（lut4 绑定），漏掉第 188 行（ff 绑定），会发生什么？

**参考答案**：`ff` 这个 pb_type 没有被绑定到任何物理电路模型。在后续 `link_openfpga_arch` / 构建阶段，OpenFPGA 要么报「未绑定电路模型」的错误，要么对未注解的 pb_type 走默认处理。总之 ff 无法被正确物化——这正说明 `pb_type_annotations` 是 VPR 结构落地为电路的关键粘合层。

---

## 5. 综合实践：画出 openfpga_arch.xml 的结构树

把本讲三处重点（根结构、布线绑定、pb_type 注解）串起来，完成下面这个贯穿性任务。

### 5.1 实践目标

以 `k4_N4_40nm_cc_openfpga.xml` 为对象，画出一棵完整的「顶层节点结构树」，标注每个节点的作用，并**明确指出 scan_chain 在哪个节点声明**。这张树将是你日后阅读任何 `openfpga_arch.xml` 的速查骨架。

### 5.2 操作步骤

1. 打开 [openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml)，从根 `<openfpga_architecture>`（第 10 行）开始，自上而下遍历每个顶层子节点。
2. 为每个顶层节点画一个树形分支，并在旁边用一句话标注职责。参考下表填空：

   | 顶层节点 | 行号 | 作用一句话 | 对应 C++ 字段 |
   | --- | --- | --- | --- |
   | `technology_library` | 11–30 | （自填：描述什么？） | `tech_lib` |
   | `circuit_library` | 31–161 | （自填） | `circuit_lib` |
   | `configuration_protocol` | 162–164 | （自填：声明什么？scan_chain 在此） | `config_protocol` |
   | `connection_block` | 165–167 | （自填） | `cb_switch2circuit` |
   | `switch_block` | 168–170 | （自填） | `sb_switch2circuit` |
   | `routing_segment` | 171–173 | （自填） | `routing_seg2circuit` |
   | `pb_type_annotations` | 174–190 | （自填） | `pb_type_annotations` |

3. 在结构树上用**高亮**标出 `configuration_protocol → organization type="scan_chain"`（第 163 行）这一处，注明「配置链在此声明，使用 `DFF` 模型」。
4. 在 `circuit_library` 分支下，额外标出三个被频繁引用的模型：`DFF`（第 143 行，配置存储）、`mux_tree_tapbuf`（第 111 行，CB/SB 共用）、`GPIO`（第 152 行，IO 焊盘），用箭头把它们指向各自的「绑定处」（configuration_protocol / connection_block / switch_block / pb_type_annotations）。
5. 最后，对照 [libs/libarchopenfpga/src/openfpga_arch.h:25-64](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/openfpga_arch.h#L25-L64)，给树上的每个节点标出对应的 C++ 字段名。

### 5.3 需要观察的现象

- 树的「定义类」两段（technology/circuit）在上，「绑定类」五段在下，**顺序恰好对应解析器 `read_xml_openfpga_arch` 的读取顺序**。
- `mux_tree_tapbuf` 被两处（CB、SB）绑定，`DFF` 被两处（configuration_protocol、pb_type 的 ff 路径附近）引用——同一个电路模型可被多处复用。
- scan_chain 的声明**只有一行**（第 163 行），却决定了整颗 FPGA 的配置存储组织方式。

### 5.4 预期结果

得到一张包含 1 个根 + 7 个顶层子节点的结构树，每个节点带「作用 + 行号 + C++ 字段」三重标注，且 scan_chain 的声明处被明确高亮。这棵树就是本讲的全局心智模型。

### 5.5 待本地验证（可选）

完成编译（见 u1-l3）后，可执行类似下面的命令观察解析日志：

```bash
# 示例命令：仅读取并链接架构（具体命令名与选项以实际 shell help 为准）
# openfpga 内部会打印 "Read OpenFPGA architecture" 计时行
```

> 注意：本讲不假定你已能跑通完整流程。若暂无运行环境，上面的「结构树绘制」本身就是合格的源码阅读型实践产物。

## 6. 本讲小结

- `openfpga_arch.xml` 的根标签是 `<openfpga_architecture>`，它是 7 个顶层子节点的容器；这些节点最终被解析进 C++ 的 `openfpga::Arch` 结构，且**建好后只读**。
- 7 个顶层节点分两类：**定义类**（`technology_library`、`circuit_library`）声明有哪些积木；**绑定类**（`configuration_protocol`、`connection_block`、`switch_block`、`routing_segment`、`pb_type_annotations`）声明积木用在哪里。
- 解析顺序有依赖：必须先读 `circuit_library`，绑定类段才能用名字反查模型 ID；这套「解析存名字、链接查 ID」的两段式设计贯穿全程。
- **配置链 scan_chain 在 `<configuration_protocol>` 节点下的 `<organization type="scan_chain" circuit_model_name="DFF"/>` 处声明**（第 162–164 行）。
- 布线绑定三段里，`name` 是 VPR 侧名字（字符串保留、待链接期对账），`circuit_model_name` 是电路库里的模型名（解析期即转成 ID）；CB 与 SB 共用 `mux_tree_tapbuf`，线段用 `chan_segment`。
- `pb_type_annotations` 用 pb_type 路径把 VPR 逻辑块绑到物理电路模型，并用 `mode_bits` 区分多模式（如 IO 输入/输出方向）。

## 7. 下一步学习建议

- 想深入电路模型的端口、设计技术、模型间引用（如 `mux_tree_tapbuf` 为何引用 `INVTX1`/`tap_buf4`/`TGATE`），请进入 **u3-l3 电路库 circuit_library 与电路模型**。
- 想对比 scan_chain 与 memory_bank、frame_based 等协议的差异，请进入 **u3-l4 配置协议 configuration_protocol**。
- 想看上述「字符串名 → 真正绑定到 VPR device context」的最后一步对账，请跳到 **u5-l2 openfpga_arch XML 解析** 与 **u5-l3 link_openfpga_arch**。
- 建议同时翻看 `openfpga_flow/openfpga_arch/` 目录下其它命名的 arch 文件（如 `k4_N4_40nm_bank_openfpga.xml`），对照本讲的结构树，体会「换配置协议只改少数几段」的解耦效果。
