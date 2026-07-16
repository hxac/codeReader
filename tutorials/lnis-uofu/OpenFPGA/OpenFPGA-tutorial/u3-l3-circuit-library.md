# 电路库 circuit_library 与电路模型

## 1. 本讲目标

本讲是「架构描述与输入文件」单元的第三讲。上一讲（u3-l2）我们看清了 `openfpga_arch.xml` 的总体结构，知道它解析后会冻结成只读的 C++ 结构 `openfpga::Arch`，其中有一个最重要的子对象叫做 `circuit_library`。本讲就专门把这个「电路库」拆开讲。

学完本讲你应该能够：

- 说出 `CircuitLibrary` 这个 C++ 类在 OpenFPGA 里扮演什么角色、它内部用什么数据组织方式管理「电路模型」。
- 识别 `circuit_model` 的常见类型（`mux` / `lut` / `ff` / `ccff` / `iopad` / `inv_buf` / `pass_gate` / `wire` / `chan_wire`）以及端口类型（`input` / `output` / `clock` / `sram` / `inout` 等）和端口的各类语义标志（global / reset / set / mode_select / prog）。
- 理解电路模型之间是如何通过 `circuit_model_name` 相互引用的（例如一个 `mux` 模型会引用 `INVTX1` 当输入缓冲、`TGATE` 当传输门），并能动手在 XML 里追踪一条引用链。

本讲只讲「电路库本身」，不涉及它如何绑定到 VPR 结构（那是 u4-l3 的 pb_type 注解），也不讲配置协议的细节（那是 u3-l4）。

## 2. 前置知识

- **电路模型（circuit_model）是什么**：OpenFPGA 要生成 FPGA 的 Verilog/SPICE 网表，需要知道「每种积木用什么晶体管电路实现」。一个 `circuit_model` 就是这样一个积木的电路级描述——例如「一个反相器」「一个传输门」「一棵多路选择器树」「一个配置用 D 触发器」。它不是 RTL 级的逻辑，而是物理电路级（transistor-level）的描述。
- **端口（port）**：和 Verilog 模块的端口概念类似，一个电路模型有输入、输出、时钟、配置（sram）等端口，每个端口还有位宽。
- **strong id（强类型 id）**：OpenFPGA 大量使用 VTR 提供的 `vtr::StrongId` 把一个整数包装成一个「类型专属」的 id，避免把「模型 id」和「端口 id」混用。你只需知道：`CircuitModelId` 和 `CircuitPortId` 是两种不能互相赋值的类型，本质上都是一个带类型的序号。
- **`vtr::vector<Id, T>`**：VTR 提供的「以 id 为下标」的向量，`vector[id]` 取出该 id 对应的 `T`。这是 OpenFPGA 数据结构的标准写法——每一类属性都用一个以 id 为键的 vector 存储（即「结构数组 / SoA」风格）。
- **两段式解析（解析存名字、链接查 id）**：上一讲已经提过这个设计。读 XML 时，模型之间的引用先用字符串名字记下来；全部模型都读完后，再用名字反查得到 `CircuitModelId`。本讲会看到它在代码里的具体实现。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [libs/libarchopenfpga/src/circuit_types.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_types.h) | 所有枚举类型与对应的字符串常量：模型类型、端口类型、缓冲类型、传输门类型等 |
| [libs/libarchopenfpga/src/circuit_library_fwd.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_library_fwd.h) | 定义强类型 id：`CircuitModelId` / `CircuitPortId` / `CircuitEdgeId` |
| [libs/libarchopenfpga/src/circuit_library.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_library.h) | `CircuitLibrary` 类声明：所有访问器（只读）与修改器（可写）接口，以及私有数据成员 |
| [libs/libarchopenfpga/src/circuit_library.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_library.cpp) | 类的实现，重点是「链接」（把名字翻译成 id）与「构建子模型列表」的方法 |
| [openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml) | 一份真实的 openfpga_arch 文件，含一个完整的 `circuit_library`，是我们追踪引用链的素材 |
| [openfpga/src/utils/circuit_library_utils.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/utils/circuit_library_utils.cpp) | 基于 `CircuitLibrary` 之上的工具函数，演示「别人怎么查询电路库」 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先看 `CircuitLibrary` 这个类的整体数据结构，再看「模型 id + 端口」这一对核心抽象，最后看模型之间如何互相引用。

### 4.1 CircuitLibrary 数据结构

#### 4.1.1 概念说明

`CircuitLibrary` 是 OpenFPGA 电路级架构的**中央仓库**。`openfpga_arch.xml` 里 `<circuit_library>` 节点下的每一个 `<circuit_model>`，解析后都变成这个仓库里的一个条目。仓库对外提供两套接口：

- **访问器（accessor）**：只读查询，如「这个模型叫什么名字」「它的输入端口有哪些」。
- **修改器（mutator）**：构建期写入，如「新增一个模型」「给它设个名字」。XML 解析器只会在构建期调用修改器，构建完成并「冻结」后，全流程都只用访问器。

它的核心设计思想是 **SoA（Structure of Arrays，结构数组）**：不是把每个模型打包成一个 `struct CircuitModel` 再放进 `vector<CircuitModel>`，而是为**每一类属性**单独开一个以 `CircuitModelId` 为下标的 `vtr::vector`。这样查询某一类属性时缓存友好，也方便给某类属性单独加快速查找表。

> 直觉：把 `CircuitLibrary` 想成一张「以 id 为行号、以属性为列」的大表格。模型 id 是行号，`model_names_`、`model_types_`、端口集合等都是各自的列。要查「id 为 5 的模型的名字」，就去 `model_names_` 这一列取第 5 行。

#### 4.1.2 核心流程

一个 `CircuitLibrary` 的生命周期分三步：

1. **构建（解析 XML）**：XML 解析器对每个 `<circuit_model>` 调 `add_model(type)` 拿到一个新 `CircuitModelId`，再用一串 `set_*` / `add_model_port` 把名字、类型、端口、缓冲、传输门等填进去。此时模型之间的引用还是**字符串名字**，对应的 id 字段是 `INVALID`。
2. **链接（build_model_links）**：所有模型都建完后，调一次 `build_model_links()`，把所有「名字」翻译成「真正的 `CircuitModelId`」，并构建子模型（sub_models）列表和快速查找表。
3. **冻结（只读使用）**：此后整个流程只读地使用它——生成 Verilog 时遍历模型，生成比特流时查 sram 端口，等等。

伪代码示意：

```
CircuitLibrary lib;
// 1. 构建：解析器逐个 model 写入
for each <circuit_model> in XML:
    id = lib.add_model(type)
    lib.set_model_name(id, name)
    lib.add_model_port(id, port_type) ...
    lib.set_model_input_buffer(id, true, "INVTX1")   // 名字
    lib.set_model_pass_gate_logic(id, "TGATE")        // 名字

// 2. 链接：名字 → id
lib.build_model_links();     // 内部调 link_buffer_model / link_pass_gate_logic_model / ...
lib.build_timing_graphs();   // 用 delay_matrix 建立时序图
lib.auto_detect_default_models();

// 3. 冻结：后续只读
for model in lib.models(): ... // 生成网表、比特流等
```

#### 4.1.3 源码精读

**强类型 id 的定义**——避免模型 id 与端口 id 混用：

[libs/libarchopenfpga/src/circuit_library_fwd.h:19-21](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_library_fwd.h#L19-L21) 用 `vtr::StrongId` 把整数包装成三个互不兼容的类型，注释写得很直白：「Create strong id for Circuit Models/Ports to avoid illegal type casting」。

**模型类型枚举与字符串**——这是「XML 字符串 ↔ C++ 枚举」的对照表：

[libs/libarchopenfpga/src/circuit_types.h:23-42](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_types.h#L23-L42) 定义了 `e_circuit_model_type`（`MUX`/`LUT`/`FF`/`CCFF`/`IOPAD`/`INVBUF`/`PASSGATE`/`WIRE`/`CHAN_WIRE`/`SRAM`/`HARDLOGIC`/`GATE`）和与之**位置一一对应**的字符串数组 `CIRCUIT_MODEL_TYPE_STRING`（`"mux"`/`"lut"`/...）。解析器读 XML 里的 `type="mux"` 字符串，靠这个数组反查到枚举值。本文件用同样的模式定义了端口类型、缓冲类型、传输门类型、门类型等所有枚举。

**SoA 数据成员**——每一类属性一个 vector：

[libs/libarchopenfpga/src/circuit_library.h:627-638](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_library.h#L627-L638) 列出了模型级的基本属性列：`model_ids_`、`model_types_`、`model_names_`、`model_prefix_`、`model_verilog_netlists_`、`model_spice_netlists_`、`model_is_default_`，以及聚合了所有「被引用子模型」的 `sub_models_`。文件头部的长注释（行 37–210）逐条解释了每列的含义，是理解整个类最好的入口。

**新增一个模型**——典型的「推入各列」操作：

[libs/libarchopenfpga/src/circuit_library.cpp:1270-1285](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_library.cpp#L1270-L1285) `add_model(type)` 先生成一个新 id（取当前 `model_ids_.size()`），再往**每一个**属性 vector 末尾 `push_back`/`emplace_back` 一个默认值。这就是 SoA 风格的代价：新增一行要碰很多列，但换来按列查询的高效。

**按名字查模型（链接期的基石）**：

[libs/libarchopenfpga/src/circuit_library.cpp:1212-1229](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_library.cpp#L1212-L1229) `model(name)` 线性扫描 `model_names_` 找匹配项，并用 `VTR_ASSERT((0 == num_found) || (1 == num_found))` 断言「要么没找到、要么只找到一个」——所以**电路库里同名模型是非法的**。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是让你亲手看清 SoA 数据结构。

1. **实践目标**：验证「`CircuitLibrary` 用一组以 id 为下标的 vector 存属性」这一说法。
2. **操作步骤**：
   - 打开 [libs/libarchopenfpga/src/circuit_library.h:627](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_library.h#L627) 起的私有数据段。
   - 数一数：与「模型级」属性对应的 `vtr::vector<CircuitModelId, ...>` 有多少个（即有多少「列」）。
   - 再对比 [libs/libarchopenfpga/src/circuit_library.cpp:1270](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_library.cpp#L1270) 的 `add_model`，看它是否对你在头文件里数出的几乎每一列都做了 `push_back`/`emplace_back`。
3. **需要观察的现象**：`add_model` 函数体很长，因为它要给每一列都补一个默认值；这正是 SoA 的特征。
4. **预期结果**：你会看到 `add_model` 里依次 `push_back` 了 type / name / prefix / verilog_netlist / spice_netlist / is_default / sub_models 等十几项，与头文件的私有成员一一对应。
5. 运行结果：待本地验证（本实践只需阅读源码，无需编译运行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `CircuitLibrary` 不直接用 `struct CircuitModel { string name; enum type; vector<Port> ports; ... };` 加 `vector<CircuitModel>`，而要用 SoA？

> **参考答案**：SoA 把同一类属性连续存储，按属性查询（如「列出所有模型的名字」「按类型过滤」）缓存友好、易于加快速查找表（如 `model_lookup_` 按 type 分桶）。代价是新增模型要碰多个 vector。OpenFPGA 选 SoA 是因为它在构建后几乎只做查询，查询性能更重要。

**练习 2**：`model(name)` 查找里有一句 `VTR_ASSERT((0 == num_found) || (1 == num_found));`，它禁止什么情况？

> **参考答案**：禁止电路库里出现两个同名模型。如果同名，`num_found` 会大于 1，断言失败、程序终止。这保证「名字 → id」的映射是单值的，模型间引用才能无歧义地用名字解析。

### 4.2 CircuitModelId 与端口模型

#### 4.2.1 概念说明

「电路模型」描述一个积木**有什么端口、用什么电路技术**，但不展开成晶体管级网表（那是网表生成阶段的事）。一个模型由这几部分信息组成：

- **基本属性**：`name`（必须与用户 Verilog 模块名一致，除非自动生成）、`prefix`（实例化时的前缀）、可选的 `verilog_netlist`/`spice_netlist`（指向用户自定义网表文件，非自动生成时用）、`is_default`（是否为该类型的默认模型）。
- **设计技术（design_technology）**：`cmos` 还是 `rram`。
- **类型专属参数**：mux 的结构（tree/one_level/multi_level/crossbar）、buffer 的大小与级数、传输门的晶体管尺寸、wire 的 RC 等。
- **端口集合**：一组 `CircuitPortId`，每个端口有类型、位宽、前缀、以及一堆语义标志。

端口类型与标志是本模块的重点。**端口类型**决定一个端口在网表里扮演什么角色；**语义标志**则告诉 OpenFPGA 这个端口需要特殊处理（例如 reset/set 端口在 testbench 里要有特定脉冲宽度）。

#### 4.2.2 核心流程

端口类型在 [circuit_types.h:103-119](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_types.h#L103-L119) 定义。完整的端口类型表如下：

| 枚举值 | XML 字符串 | 含义 |
| --- | --- | --- |
| `CIRCUIT_MODEL_PORT_INPUT` | `input` | 普通输入 |
| `CIRCUIT_MODEL_PORT_OUTPUT` | `output` | 普通输出 |
| `CIRCUIT_MODEL_PORT_INOUT` | `inout` | 双向端口（典型：IO 的 PAD） |
| `CIRCUIT_MODEL_PORT_CLOCK` | `clock` | 时钟 |
| `CIRCUIT_MODEL_PORT_SRAM` | `sram` | 配置位端口（接配置存储器） |
| `CIRCUIT_MODEL_PORT_BL` / `BLB` | `bl` / `blb` | memory_bank 的位线 / 位线反 |
| `CIRCUIT_MODEL_PORT_WL` / `WLB` / `WLR` | `wl` / `wlb` / `wlr` | 字线及其变体 |

端口的语义标志（布尔属性）在头文件私有段逐一列出，关键的几个：

- `port_is_global`：全局信号（如时钟、复位），不走正常布线，所有模型共享。
- `port_is_reset` / `port_is_set`：复位 / 置位，testbench 里需要专门脉冲。
- `port_is_mode_select`：该 sram 端口用于选择工作模式（而非普通配置位），影响比特流组织。
- `port_is_prog`：编程阶段使用的端口（如配置时钟 `prog_clk`）。
- `port_is_io` / `port_is_data_io`：是否为 IO 端口 / 是否可映射到 netlist 信号。

> 这些标志不是装饰：比特流生成时会用 `port_is_mode_select` 把 sram 端口分成「普通配置位」和「模式选择位」两组（见下面的源码精读）。

#### 4.2.3 源码精读

**端口类型枚举与字符串**：

[libs/libarchopenfpga/src/circuit_types.h:103-119](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_types.h#L103-L119) 定义 `e_circuit_model_port_type`，并把每个枚举值对应到一个 XML 字符串。注意 `CIRCUIT_MODEL_PORT_TYPE_STRING` 的顺序与枚举**严格一致**，解析器靠下标互换。

**端口的访问器**——标志位如何暴露：

[libs/libarchopenfpga/src/circuit_library.h:355-385](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_library.h#L355-L385) 集中声明了端口查询接口：`is_input_port` / `is_output_port` / `port_type` / `port_size` / `port_is_global` / `port_is_reset` / `port_is_set` / `port_is_mode_select` / `port_is_prog` 等。注意它们都接收一个 `CircuitPortId`（不是 `CircuitModelId`），因为端口是一等公民，有自己的 id 空间。

**真实 XML 端口示例**——`GPIO` 模型：

[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:152-160](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L152-L160) 定义了 IO 焊盘模型 `GPIO`，它的端口几乎用到了所有常见标志：

- `<port type="inout" prefix="PAD" is_global="true" is_io="true" is_data_io="true"/>`：双向焊盘，全局、IO、可映射数据。
- `<port type="sram" prefix="DIR" size="1" mode_select="true" circuit_model_name="DFF" default_val="1"/>`：方向控制位，标了 `mode_select`（模式选择位，不是普通配置位），并直接绑定到 `DFF` 电路模型。

**标志位如何被消费**——区分普通 sram 与 mode-select sram：

[openfpga/src/utils/circuit_library_utils.cpp:49-82](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/utils/circuit_library_utils.cpp#L49-L82) `find_circuit_regular_sram_ports` 和 `find_circuit_mode_select_sram_ports` 一对函数，先取出所有 sram 端口，再用 `port_is_mode_select(port)` 把它们一分为二。这就是 `mode_select` 标志的真正用途：决定比特流里哪些位属于「功能配置」、哪些位属于「模式选择」。

#### 4.2.4 代码实践

1. **实践目标**：用一个真实模型，把它的每个端口的「类型 + 关键标志」对上号。
2. **操作步骤**：
   - 打开 [k4_N4_40nm_cc_openfpga.xml:143-151](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L143-L151) 的配置 DFF 模型 `DFF`（类型 `ccff`）。
   - 画出一张表，列出它的 4 个端口 `D` / `Q` / `QN` / `prog_clk`，每一行写：XML `type`、对应枚举、`is_global`、`is_prog`、`default_val`。
3. **需要观察的现象**：`prog_clk` 端口同时标了 `is_global="true"` 和 `is_prog="true"`，而数据端口 `D`/`Q`/`QN` 没有这些标志。
4. **预期结果**：
   - `D` → input，无特殊标志。
   - `Q` / `QN` → output，无特殊标志。
   - `prog_clk` → clock，`is_global=true`、`is_prog=true`、`default_val=0`。
5. 运行结果：待本地验证（源码/配置阅读型实践，无需运行）。

#### 4.2.5 小练习与答案

**练习 1**：`sram` 端口和 `input` 端口有什么本质区别？为什么要把配置位单独算一类端口？

> **参考答案**：`input` 是数据通路上的普通输入；`sram` 端口专门用来接配置存储器（决定电路功能的那批位）。单独分类是因为配置位的处理方式完全不同：它们不参与正常仿真功能、会被组织成比特流、还会受配置协议（scan_chain / memory_bank / frame）影响——这些都需要在网表与比特流生成阶段特殊对待。

**练习 2**：`mode_select="true"` 的 sram 端口（如 GPIO 的 `DIR`）和普通 sram 端口在比特流里会被怎样区别对待？

> **参考答案**：见 [circuit_library_utils.cpp:49-82](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/utils/circuit_library_utils.cpp#L49-L82)。工具函数会把两者分别归入「regular sram ports」和「mode select sram ports」两个列表，比特流生成据此把功能配置位和模式选择位分开组织。

### 4.3 电路模型间引用

#### 4.3.1 概念说明

很多电路模型**不是孤立的**，它们会引用别的模型作为自己的子部件。最典型的就是 `mux`（多路选择器）：一棵 mux 树不是凭空用晶体管搭出来的，而是由

- **输入缓冲（input_buffer）**：通常一个反相器，例如 `INVTX1`；
- **输出缓冲（output_buffer）**：一个缓冲器，例如 `tap_buf4`；
- **传输门（pass_gate_logic）**：mux 每一级的选择开关，例如 `TGATE`；

这三个**已被单独定义**的模型组合而成。`mux` 模型只描述「结构是一棵树、有几级、有没有常量输入」，而真正的晶体管电路来自它引用的那三个子模型。

类似的，`lut` 模型会引用输入反相器（`lut_input_inverter`）、输入缓冲（`lut_input_buffer`）、传输门；端口本身也能引用模型——`GPIO` 的 `DIR` 端口标了 `circuit_model_name="DFF"`，表示这个配置位用 `DFF` 触发器实现，并可用 `port_tri_state_model_name` 让某端口由一个模型三态控制。

**引用在 XML 里写成名字，在 C++ 里翻译成 id**——这就是上一讲提到的「两段式」在本模块的具体落地。

#### 4.3.2 核心流程

引用解析的完整链路：

1. **写名字**：解析 XML 时，`set_model_input_buffer(id, exist, "INVTX1")`、`set_model_pass_gate_logic(id, "TGATE")` 把名字存进 `*_model_names_` 列，对应的 `*_model_ids_` 列留 `INVALID`。

   一个细节在 [circuit_library.cpp:2139-2160](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_library.cpp#L2139-L2160)：`set_model_buffer` 会按需把存放「5 种缓冲位（INPUT/OUTPUT/LUT_INPUT_BUFFER/LUT_INPUT_INVERTER/LUT_INTER_BUFFER）」的子向量 `resize` 到足够长，写上名字，并把 id 设成 `INVALID`——明确注释「which will be linked later」。

2. **链接 id**：全部模型建完后，`build_model_links()` 遍历每个模型，调用：

   - `link_buffer_model(model_id)`：把每种缓冲的名字翻译成 id，见 [circuit_library.cpp:2201-2214](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_library.cpp#L2201-L2214)。
   - `link_pass_gate_logic_model(model_id)`：翻译传输门（含可选的「末级传输门」），见 [circuit_library.cpp:2220-2237](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_library.cpp#L2220-L2237)。
   - 还有端口级别的 `link_port_tri_state_model()` / `link_port_inv_model()`。

   它们统统调用 `model(name)` 把字符串解析成 `CircuitModelId`。

3. **构建子模型清单**：`build_submodels()` 汇总每个模型引用到的所有子模型（缓冲 + 传输门 + 端点的 tri-state/inv 模型），去重后存入 `sub_models_[model]`，供网表生成时「先输出子模型再输出父模型」使用。见 [circuit_library.cpp:2257-2302](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_library.cpp#L2257-L2302)。

伪代码（链接一个 mux 模型）：

```
set_model_input_buffer(mux_id, true, "INVTX1")     // buffer_model_names_[mux][INPUT] = "INVTX1"
                                                   // buffer_model_ids_[mux][INPUT]  = INVALID
set_model_output_buffer(mux_id, true, "tap_buf4")  // 同上，OUTPUT 槽
set_model_pass_gate_logic(mux_id, "TGATE")          // pass_gate_logic_model_names_[mux] = "TGATE"
...
build_model_links():
  for each model:
    link_buffer_model(model):     buffer_model_ids_[model][*] = model(name)   // 名字→id
    link_pass_gate_logic_model(model): pass_gate_logic_model_ids_[model] = model(name)
  build_submodels():  sub_models_[mux] = 去重{INVTX1, tap_buf4, TGATE}
```

#### 4.3.3 源码精读

**XML 里的引用写法**——`mux_tree_tapbuf` 模型：

[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:111-119](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L111-L119) 这是一个被设为 `is_default="true"` 的 mux 模型。三处引用一目了然：

```xml
<input_buffer exist="true" circuit_model_name="INVTX1"/>
<output_buffer exist="true" circuit_model_name="tap_buf4"/>
<pass_gate_logic circuit_model_name="TGATE"/>
```

注意它和同文件里另一个 mux `mux_tree`（[行 102-110](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L102-L110)）的唯一区别：输出缓冲前者用 `tap_buf4`（三级缓冲，驱动能力更强），后者用 `INVTX1`（单级反相）。这是「同一类模型，引用不同子模型」的典型例子。

**被引用的两个子模型本身就定义在同一个库里**：

- `INVTX1`（[行 32-43](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L32-L43)）：`inv_buf` 类型，反相器，`is_default="true"`，所以「裸写 `inv_buf` 却不指定名字」时会默认用它。
- `TGATE`（[行 68-83](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L68-L83)）：`pass_gate` 类型，传输门，明确把输入/输出缓冲都关掉（`exist="false"`），因为它自己就是最底层的开关。

**链接实现——名字翻译成 id**：

[libs/libarchopenfpga/src/circuit_library.cpp:2335-2350](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_library.cpp#L2335-L2350) `build_model_links()` 是总入口：先对每个模型链接缓冲与传输门，再链接端口级模型，最后 `build_submodels()`。它必须在「所有模型都已 `add_model`」之后才能调用，否则 `model(name)` 会查不到。

[libs/libarchopenfpga/src/circuit_library.cpp:2201-2214](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_library.cpp#L2201-L2214) `link_buffer_model` 对该模型的每一种缓冲槽，跳过空名字，把名字送进 `model()` 得到 id 写回 `buffer_model_ids_`。

**子模型清单的用途**：

[libs/libarchopenfpga/src/circuit_library.cpp:2257-2302](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_library.cpp#L2257-L2302) `build_submodels` 把每个模型引用的缓冲/传输门/端口三态模型汇总、用 `is_unique_submodel` 去重，存进 `sub_models_[model]`。网表生成器据此保证「先写被引用的子模型，再写引用者」，避免前向引用。

**默认模型的自动补全**：

[libs/libarchopenfpga/src/circuit_library.cpp:2366-2381](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_library.cpp#L2366-L2381) `auto_detect_default_models()` 遍历按类型分桶的 `model_lookup_`，若某类型只有唯一一个模型且未声明 `is_default`，就自动把它设为默认并打 warning。这解释了为什么 XML 里有些类型只定义一个模型时不必写 `is_default="true"`。

#### 4.3.4 代码实践（对应本讲主任务）

1. **实践目标**：在 `k4_N4_40nm_cc_openfpga.xml` 的 `circuit_library` 中追踪 `mux_tree_tapbuf` 模型，写出它引用的全部子模型及其角色。
2. **操作步骤**：
   - 定位 [k4_N4_40nm_cc_openfpga.xml:111-119](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L111-L119) 的 `mux_tree_tapbuf`。
   - 读它的三个引用节点 `input_buffer` / `output_buffer` / `pass_gate_logic` 的 `circuit_model_name`。
   - 回到同一个 `<circuit_library>` 里分别找到 `INVTX1`、`tap_buf4`、`TGATE` 的定义，记下它们的 `type` 和关键参数。
3. **需要观察的现象**：三个被引用模型都**定义在同一个 `circuit_library` 内**，且类型各不相同（`inv_buf` / `inv_buf` / `pass_gate`）。
4. **预期结果**——引用链如下：

   ```
   mux_tree_tapbuf (type=mux, structure=tree)
     ├── input_buffer  → INVTX1   (type=inv_buf, inverter, is_default)
     ├── output_buffer → tap_buf4 (type=inv_buf, buffer, num_level=3, f_per_stage=4)
     └── pass_gate_logic → TGATE  (type=pass_gate, transmission_gate, nmos_size=1, pmos_size=2)
   ```

   这条链经 `build_model_links()` 翻译成 id 后，`sub_models_[mux_tree_tapbuf]` 里会去重存放 `{INVTX1, tap_buf4, TGATE}` 三个 `CircuitModelId`。
5. **延伸思考**：对比 `mux_tree`（[行 102-110](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L102-L110)），它的输出缓冲是 `INVTX1` 而非 `tap_buf4`——这正是两个 mux 模型的唯一差异，说明 OpenFPGA 通过「换引用」就能生成驱动能力不同的 mux 变体。
6. 运行结果：待本地验证（本实践为源码/配置阅读型，无需运行）。若想确认 `sub_models_` 的实际内容，需要编写调试代码或在 `build_model_links` 处加日志后重新编译，属于可选的进阶验证。

#### 4.3.5 小练习与答案

**练习 1**：如果 XML 里 `mux_tree_tapbuf` 的 `<output_buffer circuit_model_name="tap_buf4"/>` 写成了一个不存在的名字（比如拼成 `tapbuf4`），会在什么时候、什么地方报错？

> **参考答案**：解析阶段不会报错（只是把字符串 `"tapbuf4"` 存进 `buffer_model_names_`）。报错发生在链接阶段：`build_model_links()` → `link_buffer_model()` → `model("tapbuf4")` 返回 `INVALID`，随后一致性检查（`check_circuit_library` 等）或下游使用该 id 时会失败。这正是「解析存名字、链接查 id」两段式设计的副作用——引用错误被推迟到链接期才暴露。

**练习 2**：为什么 `TGATE` 模型要把 `input_buffer` 和 `output_buffer` 都设成 `exist="false"`？

> **参考答案**：因为 `TGATE` 是**被引用的最底层子模型**（传输门本身就是开关，由 NMOS+PMOS 组成）。如果它再套一层缓冲，就会变成「缓冲→传输门→缓冲」的冗余结构。缓冲应该由引用它的上层模型（如 mux）按需添加，而不是塞进最底层开关里。这也体现了引用关系带来的层次化组合：上层决定要不要加缓冲，下层只提供纯开关。

## 5. 综合实践

把本讲三个模块串起来，完成下面这张「电路库全景表」。以 [k4_N4_40nm_cc_openfpga.xml](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml) 的 `<circuit_library>` 为对象：

1. **建表**：列出全部 10 个 `circuit_model`（`INVTX1`/`buf4`/`tap_buf4`/`TGATE`/`chan_segment`/`direct_interc`/`mux_tree`/`mux_tree_tapbuf`/`DFFSRQ`/`lut4`/`DFF`/`GPIO`——请以你在文件里实际数到的为准），每行填：`name`、`type`、`is_default`、`is_default` 为 true 的理由。
2. **标端口**：挑 `lut4`（[行 131-141](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L131-L141)）和 `GPIO`（[行 152-160](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L152-L160)），把每个端口的 `type`、`size`、以及所有布尔标志（`is_global`/`is_reset`/`is_set`/`is_mode_select`/`is_io`/`is_data_io`/`is_prog`）填进表里。
3. **画引用图**：以 `mux_tree_tapbuf`、`lut4`、`GPIO` 为顶层节点，画出它们各自引用了哪些子模型（输入缓冲、输出缓冲、传输门、LUT 输入缓冲/反相、端口的 `circuit_model_name`），用箭头标出「引用者 → 被引用者」并注明角色（input_buffer / output_buffer / pass_gate_logic / lut_input_buffer / port-sram）。
4. **回归代码**：对照 [circuit_library.cpp:2335-2350](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_library.cpp#L2335-L2350) 的 `build_model_links()` 与 [2257-2302](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_library.cpp#L2257-L2302) 的 `build_submodels()`，在你的引用图旁边写下：这条边是经哪个 `link_*` 函数、由哪个 `*_names_` 列翻译到哪个 `*_ids_` 列的。

完成这张表与图，你就把「数据结构（SoA）→ 端口模型（类型 + 标志）→ 模型间引用（名字→id）」三件事打通了。

## 6. 本讲小结

- `CircuitLibrary` 是电路级架构的中央仓库，采用 **SoA（结构数组）** 风格：每个属性是一条以 `CircuitModelId` 为下标的 `vtr::vector`，新增模型要给每一列都补默认值。
- 模型与端口都用 **强类型 id**（`CircuitModelId`/`CircuitPortId`）标识，避免类型混用；`model(name)` 提供名字→id 查找并保证名字唯一。
- `circuit_model` 有 12 种 `type`（`mux`/`lut`/`ff`/`ccff`/`iopad`/`inv_buf`/`pass_gate`/`wire`/`chan_wire`/`sram`/`hard_logic`/`gate`），每种类型在 XML 字符串与 C++ 枚举间靠位置一一对应。
- 端口有 10 种 `port_type`（`input`/`output`/`inout`/`clock`/`sram`/`bl`/`blb`/`wl`/`wlb`/`wlr`）外加一批语义标志（`is_global`/`is_reset`/`is_set`/`is_mode_select`/`is_prog`/`is_io`...），这些标志在比特流与 testbench 生成时被直接消费。
- 模型之间通过 `circuit_model_name` **引用**子模型（缓冲、传输门、端口三态模型），遵循「解析存名字 → `build_model_links()` 翻译成 id → `build_submodels()` 汇总去重」的两段式流程。
- 典型引用链：`mux_tree_tapbuf` → `{INVTX1(输入缓冲), tap_buf4(输出缓冲), TGATE(传输门)}`；换引用即可生成不同驱动强度的 mux 变体。

## 7. 下一步学习建议

- **下一讲 u3-l4（配置协议）**：本讲出现了 `ccff`（`DFF`）、`sram` 端口、`mode_select` 等概念，这些都服务于配置协议。u3-l4 会讲 `configuration_protocol` 如何决定配置存储器用 `scan_chain` / `memory_bank` / `frame_based` 组织，以及它如何挑选本讲的某个模型（如 `DFF`）当配置存储单元。
- **后续 u4-l3（pb_type 注解）**：本讲的电路模型是「积木」，但还没绑到 VPR 的逻辑块上。u4-l3 讲 `pb_type_annotations` 如何用名字把这些 `circuit_model` 绑到 VPR 的 `clb`/`io`/`lut`/`ff` 上。
- **深入阅读**：若想看「别人怎么查询电路库」，继续读 [openfpga/src/utils/circuit_library_utils.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/utils/circuit_library_utils.cpp) 的其余函数（如 `find_circuit_num_config_bits`、`find_circuit_library_global_ports`），以及 [libs/libarchopenfpga/src/check_circuit_library.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/check_circuit_library.h)，它检查引用一致性，是理解「链接失败如何被发现」的关键。
