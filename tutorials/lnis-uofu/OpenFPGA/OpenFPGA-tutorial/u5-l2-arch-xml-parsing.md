# openfpga_arch XML 解析

## 1. 本讲目标

上一讲（u5-l1）我们打开了 `openfpga_arch.xml` 在内存中凝结成的 C++ 对象 `openfpga::Arch`，并总结了它的生命周期范式：**解析存名字 → 链接查 ID（强类型 ID）→ 只读冻结**。但那只是结论——本讲我们要打开「解析」这一步的黑盒，亲眼看一遍 XML 文本是怎样变成 `Arch` 对象的。

学完本讲，你应当能够：

- 说出 `read_openfpga_arch` 命令背后的调用链：命令模板 → `read_xml_openfpga_arch()` 总入口 → 各 `read_xml_*` 子解析器。
- 理解「解析存名字、链接查 ID」这条设计原则在真实代码里是如何落地：哪些字段在解析期只存字符串、由谁在链接期翻译成 `CircuitModelId`。
- 识别 XML 常量文件（`*_xml_constants.h` 与 `circuit_types.h` 里的字符串表）在「XML 字符串 ↔ C++ 枚举」之间扮演的桥梁角色。
- 看懂解析完成后的一组 `check_*` 一致性检查，理解它们为何被设计成**只读**，以及它们在命令返回 `CMD_EXEC_FATAL_ERROR` 中的作用。

## 2. 前置知识

在进入源码前，先用三段话补齐必要背景。

**pugixml 与 pugiutil：XML 解析的底层工具。** OpenFPGA 没有自己造 XML 解析器，而是用开源库 pugixml 把文件加载成一棵内存中的节点树（`pugi::xml_node`）。在它之上，VPR 项目又包了一层 `pugiutil`，提供带行号追踪的辅助函数：`get_single_child`（取唯一指定名字的子节点，找不到就报错）、`get_attribute`（取属性，可指定必选/可选）、`bad_tag`（遇到非法标签名时报错）。`pugiutil::loc_data` 则记录着「每个节点对应源文件第几行」，所有报错信息都能带出行号。这两个工具的头文件位于 VPR 子模块里（不在本仓库跟踪范围内），本讲只把它们当作「带行号的 XML 访问 API」来用，重点关注 OpenFPGA 自己写的解析逻辑。

**两段式设计回顾。** `openfpga_arch.xml` 里大量存在「模型 A 引用模型 B」的关系（例如一个 mux 模型声明「我的输入缓冲用 `circuit_model_name=INVTX1`」）。但在解析 XML 的那一刻，被引用的 `INVTX1` 还可能没被读到，所以**解析期只能先把名字字符串存下来**；等所有模型都读完，再统一用名字去查表、翻译成强类型 ID `CircuitModelId`。这就是上一讲反复出现的「解析存名字 → 链接查 ID」。本讲会给你至少三个具体的代码落点。

**命令 → 模板 → 库函数 三层。** OpenFPGA 的每条 shell 命令都遵循「命令注册（`*_command_template.h`）→ 执行模板（`openfpga_*_template.h`）→ 库实现（`libs/` 里）」的三层结构。`read_openfpga_arch` 也不例外：命令在 `openfpga_setup_command_template.h` 里注册，执行函数是 `read_openfpga_arch_template`（在 `openfpga_read_arch_template.h` 里），真正的解析逻辑则在库 `libarchopenfpga` 的 `read_xml_openfpga_arch.cpp` 里。本讲会沿着这三层从上往下走一遍。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `openfpga/src/base/openfpga_setup_command_template.h` | 把 `read_openfpga_arch` 命令注册进 shell，定义 `--file/-f` 选项并绑定执行函数 |
| `openfpga/src/base/openfpga_read_arch_template.h` | 命令的执行模板：取选项 → 调库解析 → 跑一致性检查 → 返回退出码 |
| `libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp` | **解析总入口** `read_xml_openfpga_arch()`，编排所有子解析器与链接器 |
| `libs/libarchopenfpga/src/read_xml_openfpga_arch.h` | 总入口与兄弟函数（读 simulation/bitstream setting）的声明 |
| `libs/libarchopenfpga/src/read_xml_circuit_library.cpp` | 子解析器：把 `<circuit_library>` 解析成 `CircuitLibrary` |
| `libs/libarchopenfpga/src/read_xml_config_protocol.cpp` | 子解析器：把 `<configuration_protocol>` 解析成 `ConfigProtocol` |
| `libs/libarchopenfpga/src/openfpga_arch_linker.h/.cpp` | 链接器：把「名字」翻译成强类型 ID（链接期） |
| `libs/libarchopenfpga/src/config_protocol_xml_constants.h` | XML 常量：协议相关标签/属性名字符串 |
| `libs/libarchopenfpga/src/circuit_types.h` | XML 常量：模型类型字符串表（字符串 ↔ 枚举） |
| `libs/libarchopenfpga/src/check_circuit_library.cpp` | 一致性检查：`check_circuit_library()`（只读） |
| `openfpga/src/utils/check_config_protocol.cpp` | 一致性检查：`check_config_protocol()`（只读） |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：① 解析总入口与编排；② 子解析器分工；③ XML 常量文件；④ 解析后的一致性检查。

### 4.1 read_xml_openfpga_arch：解析总入口与编排

#### 4.1.1 概念说明

`read_xml_openfpga_arch()` 是整个架构解析的**总入口**，可以把它理解成一条流水线的工长：它自己不亲自拧每一颗螺丝，而是负责「先做哪一步、后做哪一步、中间穿插哪些链接动作」。它的输入是一个 XML 文件路径，输出是一个填好的 `openfpga::Arch` 对象。

这条流水线最重要的设计是**「解析」和「链接」交替进行、且有严格顺序**：

- 必须先解析 `circuit_library`，因为后面所有绑定段（配置协议、连接块、开关块、布线段、pb_type 注解）都要用名字去引用电路模型。
- 解析完 `circuit_library` 后立刻调用 `build_model_links()` 把模型间的名字引用翻译成 ID。
- `technology_library` 解析完后要 `bind_circuit_model_to_technology_model()` 把电路模型绑定到晶体管器件模型。
- `configuration_protocol` 解析完只存了「用哪个内存模型」的名字，要靠 `link_config_protocol_to_circuit_library()` 去查 ID。

这个顺序就是上一讲「解析存名字 → 链接查 ID」在编排层面的体现。

#### 4.1.2 核心流程

伪代码描述 `read_xml_openfpga_arch()` 的执行过程：

```
function read_xml_openfpga_arch(文件名):
    创建空 Arch 对象 openfpga_arch
    try:
        loc_data = load_xml(doc, 文件名)                 # 加载并记录行号
        root = get_single_child(doc, "openfpga_architecture")  # 根节点

        # —— 定义类：先有积木 ——
        解析 circuit_library  -> openfpga_arch.circuit_lib
        auto_detect_default_models() / build_model_links() / build_timing_graphs()

        解析 technology_library -> openfpga_arch.tech_lib
        link_models_to_variations()
        bind_circuit_model_to_technology_model()         # 链接：电路模型↔器件模型

        # —— 绑定类：把积木用在何处 ——
        解析 configuration_protocol -> openfpga_arch.config_protocol
        link_config_protocol_to_circuit_library()        # 链接：协议内存模型名→ID
        config_circuit_models_sram_port_to_default_sram_model(...)  # 应用默认 SRAM

        解析 cb_switch / sb_switch / routing_segment / arch_direct
        解析 tile_annotations / pb_type_annotations
    catch XmlError:
        archfpga_throw(文件名, 行号, 信息)               # 带行号抛出
    return openfpga_arch
```

一条贯穿始终的约束：**子解析器只负责「读 XML、填字段」，不负责「跨对象对账」**；跨对象对账（名字→ID、类型匹配）全部交给紧随其后的 `link_*` / `bind_*` 函数。这样每个函数职责单一，也方便定位错误。

#### 4.1.3 源码精读

总入口的函数签名与计时器（用 `vtr::ScopedStartFinishTimer` 打印耗时），返回值就是填好的 `Arch`：

[read_xml_openfpga_arch.cpp:35-38](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp#L35-L38) —— 函数开头创建空 `Arch` 对象，准备逐步填充。

第一步是加载 XML 并定位根节点 `<openfpga_architecture>`。注意 `get_single_child` 第二个参数是必选子节点名——如果根节点名字写错（比如拼成 `openfpga_arch`），这里就会带着行号报错：

[read_xml_openfpga_arch.cpp:46-51](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp#L46-L51) —— `pugiutil::load_xml` 加载文件，`get_single_child` 取根节点。

接着是**定义类**的解析：先 `circuit_library`，紧跟三个后处理（自动选默认模型、建模型间链接、建 timing graph）。注意 `build_model_links()` 正是把电路模型之间的名字引用翻译成 ID 的「链接」动作，是两段式设计的关键一步：

[read_xml_openfpga_arch.cpp:56-68](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp#L56-L68) —— 解析电路库后立即做三个后处理。

然后是 `technology_library` 与它的链接动作 `bind_circuit_model_to_technology_model`——把电路模型绑到晶体管工艺器件上（SPICE 仿真会用到）：

[read_xml_openfpga_arch.cpp:71-79](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp#L71-L79) —— 工艺库解析 + 电路-工艺绑定。

再之后是**绑定类**的第一块、也是本讲的重点之一：`configuration_protocol`。注意它解析完只得到一个「内存模型名字」，真正的查 ID 由下一行的 `link_config_protocol_to_circuit_library()` 完成，随后还要把默认 SRAM 模型套到所有 SRAM 端口上：

[read_xml_openfpga_arch.cpp:82-93](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp#L82-L93) —— 协议解析 + 协议-电路库链接 + 应用默认 SRAM 模型。

随后是连接块/开关块/布线段/直连的电路绑定，以及 tile 与 pb_type 注解。注意这里把布线结构（来自 VPR 架构）的名字绑到电路模型上：

[read_xml_openfpga_arch.cpp:96-121](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp#L96-L121) —— 连接块/开关块/布线段/直连/tile 注解/pb_type 注解依次解析。

> 🔍 **源码阅读彩蛋（真实代码，非示例）**：上面这段里，连接块的解析 `read_xml_cb_switch_circuit(...)` 被连续调用了两次（第 96–97 行与第 99–101 行），注释和调用都重复了一遍，第二次只是把同样的结果再赋值一次。这是一个无害的冗余——`cb_switch2circuit` 被相同函数、相同入参覆盖赋值两次，结果不变，但确实多解析了一遍 XML。读到这种地方不必怀疑自己看错，大型工程里这类小冗余真实存在；这正是「源码阅读型实践」能培养出的观察力。

最后，整个解析包在 `try { ... } catch (pugiutil::XmlError& e) { archfpga_throw(...) }` 里。所有 pugi/pugiutil 抛出的 XML 错误都会被捕获，转成带文件名+行号的 `archfpga_throw`（OpenFPGA/VPR 的统一错误抛出宏），保证用户看到的报错永远带着准确的源文件位置：

[read_xml_openfpga_arch.cpp:123-125](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp#L123-L125) —— 捕获 XML 异常并带行号重新抛出。

> 补充：`read_xml_openfpga_arch.h` 里还声明了两个**兄弟**函数 `read_xml_openfpga_simulation_settings` 与 `read_xml_openfpga_bitstream_settings`，它们由另外的命令（`read_simulation_setting` / `read_bitstream_setting`）调用，**不在** `read_openfpga_arch` 的链路里。这印证了 u5-l1 的结论：simulation/bitstream setting 是与 `Arch` 平级的兄弟结构、各有独立文件，而不是 `Arch` 的一部分。见 [read_xml_openfpga_arch.h:16-23](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.h#L16-L23)。

#### 4.1.4 代码实践

**实践目标**：沿着命令 → 模板 → 库三层，亲手把 `read_openfpga_arch` 的调用链走一遍，确认「编排骨在引擎里、解析肉在库里」的分层。

**操作步骤**：

1. 在 `openfpga_setup_command_template.h` 中定位命令注册，确认它只有一个 `--file`（短名 `-f`）选项，并绑定到执行模板 `read_openfpga_arch_template<T>`：见 [openfpga_setup_command_template.h:34-47](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L34-L47)。
2. 跳到执行模板，确认它只做三件事：取 `--file` 的值、调用 `read_xml_openfpga_arch(...)` 把结果写进 `mutable_arch()`、然后跑三个检查：见 [openfpga_read_arch_template.h:43-68](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_read_arch_template.h#L43-L68)。
3. 最后进入 `read_xml_openfpga_arch.cpp` 的总入口，对照 4.1.3 的逐行讲解，把 8 个 `read_xml_*` 调用按出现顺序列成一张表。

**需要观察的现象**：执行模板里第 55、59、64 行的三个 `if (false == check_*(...)) return CMD_EXEC_FATAL_ERROR;`——它们说明「解析成功」不等于「命令成功」；任何一个一致性检查失败，命令都会返回致命错误，shell 会据此终止（参见 u2 关于依赖检查的讨论）。

**预期结果**：你会得到一张「命令层（无解析逻辑）→ 模板层（编排+检查）→ 库层（真正解析）」的清晰分层图。运行结果如需确认，可用 `openfpga -x "read_openfpga_arch -f <某个 openfpga_arch.xml>; exit"` 观察日志中的 `Read OpenFPGA architecture` 计时行与 `Checking circuit library passed.` 行（**待本地验证**，取决于是否已 `make compile`）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `link_config_protocol_to_circuit_library()`（[第 87 行](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp#L87)）这一行删掉，会发生什么？

> **参考答案**：`configuration_protocol` 此时只保存了「内存模型的名字字符串」，没有翻译成 `CircuitModelId`。下游凡是访问协议内存模型 ID 的地方（例如把默认 SRAM 套到端口的第 92–93 行、以及 fabric 构建期）都会拿到无效 ID 或断言失败。这正是「链接期」存在的意义。

**练习 2**：为什么 `circuit_library` 必须在 `configuration_protocol` 之前解析？

> **参考答案**：因为配置协议只声明「用哪个内存模型」的名字，链接期需要到已建好的 `circuit_library` 里按名字查 ID。若电路库还没解析，链接就无处可查。这体现了总入口里「定义类先于绑定类」的固定顺序。

### 4.2 子解析器分工：circuit_library 与 configuration_protocol

#### 4.2.1 概念说明

总入口只负责编排，真正「读 XML 节点、填 C++ 字段」的工作分散在一组 `read_xml_*` 子解析器里。它们有两个共同特征：

1. **统一签名**：几乎都是 `返回类型 read_xml_xxx(pugi::xml_node& Node, const pugiutil::loc_data& loc_data, ...)`，前两个参数永远是「当前 XML 节点」和「行号上下文」，保证任何报错都能定位到行。
2. **只填当前对象、不跨对象对账**：子解析器读到的「引用型」字段（如 `circuit_model_name`）一律先存字符串，跨对象对账交给链接器。

本模块挑两个最具代表性的子解析器精读：`read_xml_circuit_library`（定义类，体量最大）与 `read_xml_config_protocol`（绑定类，最能体现「存名字」）。

#### 4.2.2 核心流程

**circuit_library 的解析**是一个三层嵌套循环：

```
read_xml_circuit_library(Node):              # Node = <circuit_library>
    for each child <circuit_model>:
        read_xml_circuit_model(model_node)   # 解析单个模型
            ├── 读 type/name/prefix 等基本属性 → add_model() 得到 CircuitModelId
            ├── read_xml_model_design_technology()  # <design_technology>
            ├── read_xml_buffer() 三连（input/output/LUT 专用 buffer） # 只存名字
            ├── read_xml_model_pass_gate_logic()    # 只存名字
            ├── for each <port>: read_xml_circuit_port()
            └── for each <delay_matrix>: read_xml_delay_matrix()
```

**configuration_protocol 的解析**则是一个按协议类型分支的过程：

```
read_xml_config_protocol(Node):              # Node = <configuration_protocol>
    取 <organization> 节点
    读 type          → set_type()                # 存枚举
    读 circuit_model_name → set_memory_model_name()  # ⚠️ 只存名字！
    读 num_regions   → set_num_regions()
    if type == scan_chain:
        两遍遍历 <programming_clock>，收集编程时钟端口
    if type == ql_memory_bank:
        解析 <bl>/<wl> 子协议（flatten/decoder/shift_register）
```

关键点：第 164–165 行的 `set_memory_model_name(... .as_string())` 只把模型**名字**存下来，名字到 `CircuitModelId` 的翻译发生在 4.1 讲过的链接器 `link_config_protocol_to_circuit_library()` 里。

#### 4.2.3 源码精读

**circuit_library 子解析器**。外层 `read_xml_circuit_library` 遍历 `<circuit_library>` 的每个 `<circuit_model>` 子节点，遇到名字不匹配的标签就用 `bad_tag` 报错：

[read_xml_circuit_library.cpp:892-908](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_circuit_library.cpp#L892-L908) —— 逐个 `<circuit_model>` 调 `read_xml_circuit_model`。

单个模型的解析入口先读 `type` 与 `name`：`type` 经 `string_to_circuit_model_type`（见 4.3）翻译成枚举后 `add_model()` 拿到 `CircuitModelId`，随后所有属性都往这个 ID 上挂：

[read_xml_circuit_library.cpp:699-714](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_circuit_library.cpp#L699-L714) —— 读 `type`/`name`，新建模型。

最能体现「只存名字」的是缓冲解析 `read_xml_buffer`：它读 `exist` 与 `circuit_model_name` 两个属性，**只返回名字字符串**（不存在则返回空串），并不去查 ID：

[read_xml_circuit_library.cpp:384-396](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_circuit_library.cpp#L384-L396) —— 缓冲解析只返回模型名字符串。

这些名字字符串随后被 `set_model_input_buffer(model, exist, name)` 等接口存进电路库，最终由总入口调用的 `build_model_links()`（[read_xml_openfpga_arch.cpp:65](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp#L65)）统一翻译成 ID。这是两段式设计的第二个落点。

端口的解析 `read_xml_circuit_port` 是单个模型里最长的函数，因为它要处理十几个可选布尔/数值属性（`is_global`/`is_reset`/`is_set`/`is_prog`/`is_mode_select`/`default_val` 等）。这些属性在 u3-l3 里已讲过语义，这里只看它的解析风格：先读必选的 `type` 翻译成端口类型枚举，`add_model_port` 拿到 `CircuitPortId`，再依次读各属性挂到端口上：

[read_xml_circuit_library.cpp:447-462](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_circuit_library.cpp#L447-L462) —— 端口解析开头：读 `type`、新建端口、读 `prefix`。

**configuration_protocol 子解析器**。入口取 `<configuration_protocol>` → `<organization>`，交给 `read_xml_config_organization`：

[read_xml_config_protocol.cpp:281-291](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_config_protocol.cpp#L281-L291) —— 取 `organization` 节点并解析。

在 `read_xml_config_organization` 里，`type` 经 `string_to_config_protocol_type` 翻译，内存模型只存名字（`set_memory_model_name`），`num_regions` 可选默认 1。随后按 `type()` 分支：scan_chain 走两遍 `<programming_clock>`，ql_memory_bank 走 `<bl>`/`<wl>`：

[read_xml_config_protocol.cpp:161-179](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_config_protocol.cpp#L161-L179) —— 设协议类型、存内存模型**名字**、读 region 数。

这是两段式设计的第三个、也是最典型的落点：协议对象在解析完成时只知道「我要用一个叫 DFF 的模型」，却不知道 DFF 是哪个 `CircuitModelId`。

#### 4.2.4 代码实践

**实践目标**：验证「子解析器只存名字、链接器才查 ID」——用配置协议作为证据。

**操作步骤**：

1. 打开 `read_xml_config_protocol.cpp`，在 `read_xml_config_organization` 里找到 `set_memory_model_name(...)`（[第 164–165 行](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_config_protocol.cpp#L164-L165)），确认它存的是 `.as_string()`，即字符串。
2. 打开链接器声明 [openfpga_arch_linker.h:6](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/openfpga_arch_linker.h#L6)，确认 `link_config_protocol_to_circuit_library(openfpga::Arch&)` 接收整个 `Arch`（这样它既能看到协议存的名字，也能看到电路库去查 ID）。
3. 回到总入口 [read_xml_openfpga_arch.cpp:82-87](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp#L82-L87)，确认「解析（存名字）」与「链接（查 ID）」是相邻的两步。

**需要观察的现象**：`set_memory_model_name` 与链接器之间没有任何 ID 查找——证明解析器确实「偷懒」只存了名字。

**预期结果**：你能在源码里画出一条「XML `circuit_model_name="DFF"` → 字符串存进 `ConfigProtocol` → 链接器查 `CircuitLibrary` → 得到 `CircuitModelId`」的证据链。

#### 4.2.5 小练习与答案

**练习 1**：`read_xml_buffer` 为什么返回 `std::string` 而不是 `CircuitModelId`？

> **参考答案**：因为解析到缓冲这一步时，被引用的缓冲模型可能还没被解析（XML 节点顺序不保证）。返回字符串把「查 ID」推迟到所有模型都读完之后的 `build_model_links()` 链接期，避免顺序依赖。

**练习 2**：`read_xml_config_organization` 里，BL/WL 子协议的解析在什么条件下才会发生？

> **参考答案**：仅当 `config_protocol.type() == CONFIG_MEM_QL_MEMORY_BANK` 时（[第 228 行](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_config_protocol.cpp#L228)）。这与 u3-l4 讲的「只有 ql_memory_bank 才能用 `<bl>`/`<wl>` 显式选子协议」一致；其它协议类型不会进入这段分支。

### 4.3 XML 常量文件：字符串与枚举的桥梁

#### 4.3.1 概念说明

解析器要把 XML 里的字符串（如 `type="mux"`、`type="scan_chain"`、标签名 `programming_clock`）翻译成 C++ 枚举（`CIRCUIT_MODEL_MUX`、`CONFIG_MEM_SCAN_CHAIN`）。如果把这些字符串字面量散落在各处 `.cpp` 里，既容易拼错，也难和写 XML 的写出端保持一致。

OpenFPGA 的做法是把这些字符串集中到两类「常量文件」里：

1. **字符串表（在枚举定义旁边）**：如 `circuit_types.h` 里的 `CIRCUIT_MODEL_TYPE_STRING`，与枚举 `e_circuit_model_type` 一一对应、同下标。解析器用一个简单的 for 循环比对字符串，命中即返回下标对应的枚举。
2. **XML 标签/属性名常量**：如 `config_protocol_xml_constants.h`，把 `num_regions`、`programming_clock`、`port` 等字符串定义成 `constexpr const char*`，读端（`read_xml_*`）和写端（`write_xml_*`）共用，保证读写对称。

这种「字符串 ↔ 枚举」的映射函数（如 `string_to_circuit_model_type`、`string_to_config_protocol_type`）有一个统一的错误约定：**找不到匹配就返回哨兵值 `NUM_XXX_TYPES`**（即枚举末尾的计数项），调用方据此报「Invalid attribute」错。

#### 4.3.2 核心流程

字符串转枚举的通用模式（以模型类型为例）：

```
function string_to_circuit_model_type(s):
    for i in 0 .. NUM_CIRCUIT_MODEL_TYPES:
        if CIRCUIT_MODEL_TYPE_STRING[i] == s:
            return e_circuit_model_type(i)     # 命中
    return NUM_CIRCUIT_MODEL_TYPES             # 哨兵：表示非法

# 调用方：
type = string_to_circuit_model_type(attr)
if type == NUM_CIRCUIT_MODEL_TYPES:
    archfpga_throw(..., "Invalid 'type' attribute '%s'", attr)
```

这样设计的好处是：新增一种模型类型，只需在枚举和字符串表里各加一项，所有解析/检查/写出的代码自动对齐，不会漏改。

#### 4.3.3 源码精读

模型类型字符串表，与枚举严格同序、同下标（下标 2 是 `mux`，对应 `CIRCUIT_MODEL_MUX`）：

[circuit_types.h:40-42](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_types.h#L40-L42) —— 模型类型字符串表（u3-l3 提到的 12 类电路模型名都在这里）。

设计技术（CMOS/RRAM）的字符串表同样成对出现：

[circuit_types.h:44-51](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_types.h#L44-L51) —— `e_circuit_model_design_tech` 枚举与其字符串表。

利用这张表做字符串→枚举转换的标准实现，找不到返回 `NUM_CIRCUIT_MODEL_TYPES`：

[read_xml_circuit_library.cpp:26-36](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_circuit_library.cpp#L26-L36) —— `string_to_circuit_model_type`：for 循环比对字符串表。

协议类型同理，使用 `CONFIG_PROTOCOL_TYPE_STRING`（定义在 `config_protocol.h`/相关头里）：

[read_xml_config_protocol.cpp:25-34](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_config_protocol.cpp#L25-L34) —— `string_to_config_protocol_type`：同样的「表查找 + 哨兵」模式。

XML 标签/属性名常量则集中在 `config_protocol_xml_constants.h`。这些 `constexpr const char*` 同时被读端和写端引用，避免拼错：

[config_protocol_xml_constants.h:5-11](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/config_protocol_xml_constants.h#L5-L11) —— 协议相关标签/属性名常量。

读端如何使用这些常量：解析 `<programming_clock>` 时用 `XML_CONFIG_PROTOCOL_CCFF_PROG_CLOCK_NODE_NAME` 校验标签名、用 `XML_CONFIG_PROTOCOL_CCFF_PROG_CLOCK_PORT_ATTR` 取 `port` 属性，全程不出现裸字符串：

[read_xml_config_protocol.cpp:54-71](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_config_protocol.cpp#L54-L71) —— `read_xml_ccff_prog_clock` 使用常量而非裸字符串访问 XML。

> 同目录下还有 `bitstream_setting_xml_constants.h`、`tile_annotation_xml_constants.h` 等同类文件，分别服务于各自的子解析器，作用完全一致。

#### 4.3.4 代码实践

**实践目标**：亲手确认「枚举、字符串表、转换函数、常量名」四者的一一对应关系，体会集中管理字符串的好处。

**操作步骤**：

1. 在 `circuit_types.h` 找到 `CIRCUIT_MODEL_TYPE_STRING`（[第 40–42 行](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_types.h#L40-L42)），数一下字符串个数（应为 12）。
2. 找到对应的 `e_circuit_model_type` 枚举（同文件靠前位置），确认枚举项数也是 12（含末尾的 `NUM_CIRCUIT_MODEL_TYPES` 计数项）。
3. 在 `read_xml_circuit_library.cpp` 搜索所有 `string_to_*` 静态函数（[第 26 行起](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_circuit_library.cpp#L26)），统计一共有多少个这种「表查找 + 哨兵」转换器。

**需要观察的现象**：每一个 `string_to_*` 转换器都长得几乎一样——只是换了一张字符串表和一种枚举类型。这是高度一致的代码模式。

**预期结果**：你应能总结出「新增一种枚举值 = 枚举加一项 + 字符串表加一项，转换器与检查器自动适配」的扩展规律，不需要改动任何 `string_to_*` 的循环逻辑。

#### 4.3.5 小练习与答案

**练习 1**：为什么转换函数找不到匹配时返回 `NUM_CIRCUIT_MODEL_TYPES` 而不是直接抛异常？

> **参考答案**：把「判定非法」和「报错」解耦。转换函数保持纯函数性质（只做查表），由调用方决定如何报错——调用方能带上**当前节点的行号**（通过 `loc_data`）抛出 `archfpga_throw`，给出精确的出错位置。若转换函数自己抛异常，反而丢失了行号上下文。

**练习 2**：`XML_CONFIG_PROTOCOL_CCFF_PROG_CLOCK_NODE_NAME` 这个常量除了被 `read_xml_config_protocol.cpp` 使用，还会被谁使用？

> **参考答案**：还会被对应的写出端 `write_xml_config_protocol.cpp`（写 fabric/arch XML 时）使用。读写共用同一个常量，保证写出的标签名和读入时校验的标签名永远一致，不会因为拼写偏差导致读写不对称。

### 4.4 一致性检查：解析之后的守门员

#### 4.4.1 概念说明

XML 解析完成、链接也完成，并不代表架构「合法」。比如：电路库里可能根本没有 MUX 模型、两个模型可能重名、一个 scan_chain 协议声明了编程时钟却不是 ccff 模型的端口……这些都不是 XML 语法错误（pugi 不会报），而是**语义错误**。

OpenFPGA 用一组 `check_*` 函数做语义校验，它们在执行模板 `read_openfpga_arch_template` 里被串联调用（参见 4.1.4）。它们有三个共同设计原则：

1. **只读**：检查函数接收 `const CircuitLibrary&` 等 const 引用，绝不修改对象（源码注释明确写「NO modification ... read-only!!!」）。
2. **累计错误计数**：每个检查返回错误数 `num_err`，汇总后只要有错就返回 `false`，命令据此返回 `CMD_EXEC_FATAL_ERROR`。
3. **带类型的硬约束**：例如「必须有 MUX」「必须有 SRAM 或 CCFF」「FF 必须有 clock+input+output 端口」，把架构的语义底线代码化。

#### 4.4.2 核心流程

执行模板里的三道检查闸门：

```
arch = read_xml_openfpga_arch(file)           # 解析+链接（在库内完成）

if not check_circuit_library(arch.circuit_lib):        return FATAL   # 闸门 1
if not check_config_protocol(arch.config_protocol, arch.circuit_lib): return FATAL  # 闸门 2
if not check_tile_annotation(arch.tile_annotations, arch.circuit_lib, physical_tile_types): return FATAL  # 闸门 3
return SUCCESS
```

每道闸门内部都是「遍历 + 计数 + 报错」模式。以 `check_circuit_library` 为例，它依次检查：模型名唯一、模型 prefix 唯一、端口属性自洽、各类必备模型（IOPAD/MUX/SRAM/CCFF/FF/LUT）存在且端口齐全、必备类型有默认模型等十多项。

#### 4.4.3 源码精读

执行模板里三道检查闸门的真实代码——注意它们都读 `arch()`（const）且任一失败即返回致命错误：

[openfpga_read_arch_template.h:55-68](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_read_arch_template.h#L55-L68) —— 三个 `check_*` 闸门，失败即 `CMD_EXEC_FATAL_ERROR`。

`check_circuit_library` 的聚合函数与它的「只读」约定（注释里强调不允许修改）：

[check_circuit_library.cpp:872-879](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/check_circuit_library.cpp#L872-L879) —— 注释声明只读，函数开头起计时器并初始化错误计数。

具体的硬约束示例：「必须有 MUX」、每个 MUX 必须有 input+output+SRAM 端口、「必须有 SRAM 或 CCFF」：

[check_circuit_library.cpp:903-926](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/check_circuit_library.cpp#L903-L926) —— MUX 必备检查与「SRAM/CCFF 至少一个」检查。

最终汇总：有任何一个错误就返回 `false`，否则打印 `Checking circuit library passed.` 并返回 `true`：

[check_circuit_library.cpp:982-989](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/check_circuit_library.cpp#L982-L989) —— 错误计数收尾。

配置协议的检查 `check_config_protocol` 在另一个目录（`openfpga/src/utils/`，因为它是引擎层对库数据的跨结构校验）。它做三件事：自检（`validate()`）、检查可配置内存模型与电路库一致、检查编程时钟端口确实是 ccff 模型的全局 clock+prog 端口：

[check_config_protocol.cpp:82-99](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/utils/check_config_protocol.cpp#L82-L99) —— `check_config_protocol` 汇总三项检查。

其中编程时钟检查最能体现「跨结构对账」：它拿着协议里声明的编程时钟端口名，去电路库的 ccff 模型里逐个核对「是不是 global、是不是 clock、是不是 prog」，任一不符就报错：

[check_config_protocol.cpp:36-63](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/utils/check_config_protocol.cpp#L36-L63) —— 编程时钟端口与 ccff 模型端口的三重核对。

> 声明位置对比：`check_circuit_library.h` 在库内（[第 51 行](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/check_circuit_library.h#L51)），因为它只依赖 `CircuitLibrary` 自身；而 `check_config_protocol.h` 在引擎层（[第 17–18 行](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/utils/check_config_protocol.h#L17-L18)），因为它要同时看 `ConfigProtocol` 和 `CircuitLibrary` 两个对象、做跨结构校验。这种「放在哪一层」的取舍本身就反映了依赖关系。

#### 4.4.4 代码实践

**实践目标**：用「故意写错架构」的方式，观察一致性检查如何拦截语义错误（源码阅读型 + 可选运行）。

**操作步骤**：

1. 复制一份 `openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml` 到临时位置。
2. **场景 A**：把 `circuit_library` 里那个 `type="mux"` 的模型删掉（或改名），模拟「缺少 MUX」。
3. 在 `check_circuit_library.cpp` 第 908 行附近（[check_circuit_library.cpp:903-916](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/check_circuit_library.cpp#L903-L916)）阅读 `check_circuit_model_required` 与 `check_one_circuit_model_port_required` 的逻辑，预测会报什么错。
4. （可选，**待本地验证**）`make compile` 后执行 `openfpga -x "read_openfpga_arch -f <改坏的.xml>; exit"`，对比日志里的 `VTR_LOG_ERROR` 与你的预测是否一致。

**需要观察的现象**：日志应出现形如 `At least one mux circuit model is required!` 的错误，并且 `check_circuit_library` 返回 `false`，命令以 `CMD_EXEC_FATAL_ERROR` 退出。

**预期结果**：你会直观看到「XML 语法合法（能解析）但语义非法（检查不过）」这一类错误，正是 `check_*` 守门员拦截的对象。

#### 4.4.5 小练习与答案

**练习 1**：为什么所有 `check_*` 函数都接收 const 引用、且源码注释反复强调「read-only」？

> **参考答案**：检查是「只读验证」，绝不能在检查阶段偷偷修改数据——否则同一份架构在不同时机检查可能得到不同结果，破坏可重现性。const 引用在编译期就禁止了修改，注释则是给维护者的契约提醒。

**练习 2**：`check_config_protocol` 为什么要同时传入 `config_protocol` 和 `circuit_lib` 两个参数？

> **参考答案**：因为它要跨结构对账：协议声明「我用某 ccff 模型的某端口做编程时钟」，而该端口是否真的是 ccff 模型、是否真的是 global/clock/prog 属性，只有查 `circuit_lib` 才知道。单一对象无法完成这种跨结构校验，所以必须同时拿到两个对象。

## 5. 综合实践

把本讲四个模块串起来，完成一次「全链路追踪」。

**任务**：以 `openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml` 为对象，画出一张从「命令行输入」到「检查通过」的完整时序图，并标注每一类证据。

要求在你的图里至少标出以下 8 个节点，并附上对应的源码行号证据：

1. 用户输入 `read_openfpga_arch -f ...`（命令注册：[openfpga_setup_command_template.h:34-47](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L34-L47)）。
2. 执行模板取 `--file` 值并调库解析（[openfpga_read_arch_template.h:43-47](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_read_arch_template.h#L43-L47)）。
3. 加载 XML、取根节点 `<openfpga_architecture>`（[read_xml_openfpga_arch.cpp:46-51](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp#L46-L51)）。
4. 解析 `circuit_library`（指出它调用 [read_xml_circuit_library.cpp:892-908](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_circuit_library.cpp#L892-L908)）。
5. 解析 `configuration_protocol`，标出「存名字」的那一步（[read_xml_config_protocol.cpp:164-165](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_config_protocol.cpp#L164-L165)）。
6. 链接期：`link_config_protocol_to_circuit_library`（[read_xml_openfpga_arch.cpp:82-87](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.cpp#L82-L87)）。
7. 三道检查闸门（[openfpga_read_arch_template.h:55-68](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_read_arch_template.h#L55-L68)）。
8. 在图中用一种颜色专门标出「字符串 ↔ 枚举」转换发生的位置（提示：每个 `string_to_*` 调用点）。

**进阶思考**（写在图下方）：如果让你新增一种电路模型类型 `e_circuit_model_type`（例如一种新型存储器），你需要改动本讲涉及的哪几类文件？按本讲的模式，答案应是「枚举 + 字符串表 + 对应检查项」，而不需要改动任何 `string_to_*` 的循环逻辑——这正是集中管理字符串的回报。

## 6. 本讲小结

- `read_openfpga_arch` 遵循「命令注册 → 执行模板 → 库实现」三层：编排在引擎里，真正的解析在 `libarchopenfpga` 的 `read_xml_openfpga_arch()` 里。
- 总入口是一个**编排器**：按「定义类（circuit_library/technology_library）→ 绑定类（config_protocol/布线段/pb_type 注解）」的固定顺序调用各 `read_xml_*`，并在每段后穿插 `link_*`/`bind_*` 链接动作。
- 「解析存名字 → 链接查 ID」在代码里有三个清晰落点：电路模型间引用（`read_xml_buffer` 存名 + `build_model_links` 查 ID）、配置协议内存模型（`set_memory_model_name` 存名 + `link_config_protocol_to_circuit_library` 查 ID）、电路-工艺绑定。
- XML 字符串与 C++ 枚举之间靠两类常量桥接：`circuit_types.h` 的字符串表（与枚举同下标）+ `*_xml_constants.h` 的标签/属性名常量；转换函数统一用「表查找 + 哨兵 `NUM_XXX_TYPES`」模式。
- 解析完成后，执行模板用 `check_circuit_library` / `check_config_protocol` / `check_tile_annotation` 三道**只读**闸门做语义校验，任一失败即返回 `CMD_EXEC_FATAL_ERROR`。
- 所有 XML 错误都被 `try/catch (XmlError)` 捕获并经 `archfpga_throw` 带文件名+行号重新抛出，保证报错永远可定位。

## 7. 下一步学习建议

本讲只覆盖了「读 `openfpga_arch.xml`」这一条命令。要继续深入架构加载，建议：

- **下一讲 u5-l3（link_openfpga_arch）**：本讲的 `link_*` 只解决了「OpenFPGA 架构内部」的名字→ID 链接；u5-l3 会讲 `link_openfpga_arch` 命令如何把已建好的 `Arch` 与 **VPR 跑完后**的 device context 关联起来（pb graph、rr graph 的 switch/segment 映射），那是更复杂的「跨工具链接」。建议先复习 u2-l3 的 `OpenfpgaContext` 再读。
- **顺读写出端**：对照阅读 `write_xml_openfpga_arch.cpp`，体会读写两端如何共享同一套 `*_xml_constants.h`，这对理解「可往返（round-trip）」的 XML 序列化很有帮助。
- **阅读兄弟命令**：`read_simulation_setting` 与 `read_bitstream_setting`（声明见 [read_xml_openfpga_arch.h:18-23](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch.h#L18-L23)）的解析套路与本讲完全一致，可作为自测练习——若你能不看答案讲清它们的入口与检查，说明本讲已掌握。
