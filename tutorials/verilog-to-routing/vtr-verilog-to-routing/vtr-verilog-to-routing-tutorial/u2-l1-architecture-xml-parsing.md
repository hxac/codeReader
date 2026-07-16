# FPGA 架构 XML 格式与解析入口

## 1. 本讲目标

本讲是第 2 单元「FPGA 架构描述与解析」的起点。学完本讲后，你应该能够：

- 说清楚 VTR 架构 XML 的整体结构，知道根节点 `<architecture>` 下都有哪些顶层元素、它们各自描述 FPGA 的哪个方面。
- 掌握解析入口 `xml_read_arch` 的函数签名：它吃什么（一个 XML 文件名）、吐出什么（一个 `t_arch` 结构体 + 两组块类型集合）。
- 理解 VTR 如何用 pugixml + 一层 `pugiutil` 封装来解析 XML，以及解析失败时错误信息是如何带着「文件名 + 行号」抛出来的。
- 建立「架构驱动」的直觉：布局布线算法几乎所有的行为差异，最终都能追溯到这份 XML。

本讲只聚焦「XML 是怎么被读进内存的」这一条链路；XML 里那些复杂结构（`t_pb_type` 层次、DeviceGrid 网格）的具体含义，留给后续讲义（u2-l2、u2-l3）深入。

## 2. 前置知识

在进入源码前，先用最朴素的语言把几个概念讲清楚。

**FPGA 不是一块「固定的」芯片。** 通用 CPU 出厂时结构就定死了，而 FPGA 的魅力在于：它的逻辑单元（LUT、触发器）、互连开关、布线通道，都可以由研究者自定义。VTR 把这种「可定制」做到极致——它不针对某一款具体 FPGA，而是接受一份**架构描述文件**，把「这是一块什么样的 FPGA」当作运行时输入。

**架构描述用 XML 写。** 为什么是 XML？因为它天然是树状的，而 FPGA 的逻辑块（比如一个包含 10 个 LUT 的逻辑单元，每个 LUT 又能拆成更小的 fracturable LUT）本身就是一棵层次树。XML 的标签嵌套正好能映射这种层次。

**「架构驱动」是什么意思。** 这是 u1-l1 已经强调过的核心理念：VPR 的算法代码（打包、布局、布线）**绝不硬编码**任何架构假设。它不写「假设有 10 个 LUT」「假设线长是 4」。这些数字全部来自运行时解析的 XML。所以同一份 vpr 二进制，喂不同的 XML，跑出来是完全不同器件上的结果。这也意味着：**当 VPR 行为反常时，第一反应应当是怀疑架构 XML，而不是算法。**

**XML 解析的两种风格。** 一种叫 SAX（边读边触发回调，不建树），一种叫 DOM（先把整个 XML 文件读进内存，建成一棵「文档对象模型」树，再遍历）。VTR 用的是 DOM 风格，底层库是 [pugixml](https://pugixml.org/)。DOM 的好处是：解析代码可以反复地、随机地访问任意节点，写起来直观；代价是要把整棵树放进内存（对 FPGA 架构文件来说完全可接受）。

**一个关键术语：永久链接。** 本讲所有源码引用都带「文件路径 + 行号」的 GitHub 链接，锚定在当前 HEAD `c3ad1ec`。你可以点开链接直接看到对应代码。

## 3. 本讲源码地图

本讲涉及的源码集中在 `libs/libarchfpga/`（架构解析的共享库）和它的两个依赖库，外加一个调用点：

| 文件 | 作用 |
| --- | --- |
| `libs/libarchfpga/src/read_xml_arch_file.h` | 解析入口 `xml_read_arch` 的函数声明。 |
| `libs/libarchfpga/src/read_xml_arch_file.cpp` | `xml_read_arch` 的实现：按顺序解析 `<architecture>` 下的每一个顶层元素。本讲的「主角」。 |
| `libs/libarchfpga/src/arch_types.h` | 架构相关的常量与枚举（如 `e_arch_format`，区分 VTR 格式与 FPGA Interchange 格式）。 |
| `libs/libarchfpga/src/physical_types.h` | 解析产物 `t_arch`、`t_arch_switch_inf` 等核心数据结构的定义。 |
| `libs/libarchfpga/src/read_xml_util.h` | 架构解析专用的辅助函数（如 `bad_tag`、`BoolToReqOpt`、`find_switch_by_name`）。 |
| `libs/libpugiutil/src/pugixml_util.hpp` | 对 pugixml 的封装层 `pugiutil`：`load_xml`、`get_single_child`、`get_attribute`、`XmlError` 等。错误处理的关键所在。 |
| `libs/libarchfpga/src/arch_error.h` | `archfpga_throw`：把解析异常转成带文件名行号的致命错误。 |
| `vpr/src/base/setup_vpr.cpp` | 调用点：VPR 启动时在这里调用 `xml_read_arch`。 |
| `vtr_flow/arch/common/arch.xml` | 一个极简但完整的示例架构文件，本讲实践用它做对照。 |

> 提示：`libs/EXTERNAL/` 之外的库（如 `libpugiutil`、`libarchfpga`）是 VTR 自己维护的，可以改；但 pugixml 本身是外部库。这一规则在 u1-l3 已讲过。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：① 架构 XML 顶层元素；② `xml_read_arch` 的签名与产物；③ pugixml 封装与错误处理。

### 4.1 架构 XML 顶层元素

#### 4.1.1 概念说明

一份架构 XML 的根节点永远是 `<architecture>`。在它之下，VTR 期望看到一组**有固定顺序、各司其职**的顶层元素。每一个顶层元素描述 FPGA 的一个侧面：

- `<models>`：原语的行为模型（比如一个 D 触发器 `DFF` 长什么样、有哪些端口）。这些模型供综合前端（ABC/PARMYS）做技术映射时参照。
- `<layout>`：器件网格怎么排布——哪些位置放逻辑块、哪些放 IO、四角放什么。可以是固定网格，也可以是 `auto_layout`（按电路规模自动生长）。
- `<device>`：全局器件参数——晶体管最小宽度电阻 `R_minW_nmos/pmos`、逻辑单元面积、连接盒/开关盒的全局设置、布线通道宽度分布等。
- `<switchlist>`：所有**互连开关**的定义（mux、tristate buffer 等），含电阻、电容、延迟。
- `<segmentlist>`：所有**布线线段**的定义（长度、单向/双向、金属层电容电阻、开关盒/连接盒连接模式）。
- `<switchblocklist>`（可选）：自定义开关盒拓扑。只有当 `<device>` 里把开关盒类型设为 `CUSTOM` 时才必需。
- `<complexblocklist>`：**逻辑块类型**（`pb_type`）的层次化定义——这是最复杂、最核心的部分，描述「一个逻辑块内部能装什么」。
- `<tiles>`：**物理瓦片**（tile）的定义，描述器件网格上每个位置实际摆放的物理实体，并通过 `equivalent_sites` 与逻辑块类型关联。
- 其余如 `<directlist>`（直连链）、`<clocknetworks>`（时钟网络）、`<noc>`（片上网络）、`<power>` 等都是可选的扩展。

为什么要强调「顺序」？因为有些解析步骤存在依赖：比如 `<segmentlist>` 的解析需要先知道 `<switchlist>` 里有哪些开关（线段要引用开关名），`<switchblocklist>` 只有在开关盒类型是 `CUSTOM` 时才必需。源码里就是严格按这个顺序一行行调用 `process_xxx` 的。

#### 4.1.2 核心流程

把 `xml_read_arch` 对顶层元素的处理抽象成伪代码：

```
load_xml(doc, arch_file)            # 把整个 XML 读进 DOM 树
architecture = doc 的 <architecture> 根节点
process models        (必需)
process layout        (必需)
process vib_layout    (可选)
process device        (必需)
process switchlist    (必需)        # 先有开关
process segmentlist   (必需)        # 线段引用开关
process switchblocklist (条件必需)  # 仅当 device 开关盒类型为 CUSTOM
process complexblocklist (必需)     # 逻辑块类型
process tiles         (必需)        # 物理瓦片
link_physical_logical_types()       # 把瓦片与逻辑块关联起来
process directlist    (可选)
... (clocknetworks / power / clocks / noc / scatter_gather 等可选)
SyncModelsPbTypes / check_models / mark_IO_types   # 收尾校验
```

注意「先 `complexblocklist`（逻辑层），后 `tiles`（物理层），再 `link`」这个顺序：VTR 先读入抽象的逻辑块类型，再读入物理瓦片，最后把两者绑定（u2-l2 会专门讲「物理 vs 逻辑」二分）。收尾的 `SyncModelsPbTypes` 会把 `<models>` 里的模型与 `pb_type` 里 `blif_model` 引用的模型对上号，`check_models` 做完整性校验，`mark_IO_types` 标记哪些瓦片是 IO 类型。

#### 4.1.3 源码精读

入口实现的开头先做两件准备工作：校验扩展名、计算架构文件的哈希指纹。

这段代码检查文件扩展名，并基于文件内容算出一个安全哈希作为该架构的唯一 ID（[read_xml_arch_file.cpp:421-429](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/read_xml_arch_file.cpp#L421-L429)）：

```cpp
if (!vtr::check_file_name_extension(arch_file, ".xml")) {
    VTR_LOG_WARN("Architecture file '%s' may be in incorrect format. ...", ...);
}
// Create a unique identifier for this architecture file based on it's contents
arch->architecture_id = vtr::secure_digest_file(arch_file);
```

`architecture_id` 后续会被写进 VPR 的结果文件里，用来唯一标识「这次跑的是哪份架构」——避免不同架构的结果被误比对。

接下来是一长串「取顶层子节点 → 调对应处理函数」。下面摘取最核心的几行（[read_xml_arch_file.cpp:443-498](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/read_xml_arch_file.cpp#L443-L498)）：

```cpp
auto architecture = get_single_child(doc, "architecture", loc_data);
// Process models
next = get_single_child(architecture, "models", loc_data);
process_models(next, arch, loc_data, device_model_warnings);
// Process layout
next = get_single_child(architecture, "layout", loc_data);
process_layout(next, arch, loc_data, num_of_avail_layers);
// Process device
next = get_single_child(architecture, "device", loc_data);
process_device(next, *arch, arch_def_fc, loc_data);
// Process switches
next = get_single_child(architecture, "switchlist", loc_data);
arch->switches = process_switches(next, timing_enabled, loc_data);
// Process segments. This depends on switches
next = get_single_child(architecture, "segmentlist", loc_data);
arch->Segments = process_segments(next, arch->switches, ...);
// Process logical block types
next = get_single_child(architecture, "complexblocklist", loc_data);
process_complex_blocks(next, logical_block_types, *arch, ...);
// Process physical tiles
next = get_single_child(architecture, "tiles", loc_data);
process_tiles(next, physical_tile_types, logical_block_types, ...);
// Link Physical Tiles with Logical Blocks
link_physical_logical_types(physical_tile_types, logical_block_types);
```

可以清楚看到：每一段顶层元素都由一个独立的 `process_xxx` 函数负责。`get_single_child`（来自 `pugiutil`，4.3 节细讲）负责「在父节点下找到唯一一个名为 X 的子节点」。

可选元素则带上 `ReqOpt::OPTIONAL` 参数，比如自定义开关盒列表（[read_xml_arch_file.cpp:487-490](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/read_xml_arch_file.cpp#L487-L490)）：

```cpp
next = get_single_child(architecture, "switchblocklist", loc_data, SWITCHBLOCKLIST_REQD);
if (next) {
    process_switch_blocks(next, arch, loc_data);
}
```

这里的 `SWITCHBLOCKLIST_REQD` 是动态决定的——只有当 `arch->sb_type == CUSTOM` 时才设为 `REQUIRED`，否则是 `OPTIONAL`（[read_xml_arch_file.cpp:480-481](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/read_xml_arch_file.cpp#L480-L481)）。这就是「条件必需」的实现方式。

对照一个真实例子，看 `vtr_flow/arch/common/arch.xml` 的顶层骨架（[arch.xml:1-13](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/arch/common/arch.xml#L1-L13) 等）：

```xml
<architecture>
  <models> ... </models>
  <tiles> ... </tiles>
  <complexblocklist> ... </complexblocklist>
  <layout> ... </layout>
  <device> ... </device>
  <switchlist> ... </switchlist>
  <segmentlist> ... </segmentlist>
</architecture>
```

这份示例虽然简单，却包含了所有**必需**顶层元素。注意 XML 里元素的书写顺序不必和源码解析顺序完全一致——解析是按标签名 `get_single_child` 取的，但源码处理顺序有依赖关系（开关先于线段），这点要和「文件书写顺序」区分开。

#### 4.1.4 代码实践

**实践目标**：用眼睛把一份真实架构 XML 的顶层结构对上源码的解析顺序。

**操作步骤**：

1. 打开 [vtr_flow/arch/common/arch.xml](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/arch/common/arch.xml)。
2. 同时打开 [read_xml_arch_file.cpp 的 457–498 行](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/read_xml_arch_file.cpp#L457-L498)。
3. 在 XML 里逐个找到 `<models>`、`<layout>`、`<device>`、`<switchlist>`、`<segmentlist>`、`<complexblocklist>`、`<tiles>` 七个标签，确认它们都存在。
4. 特别留意：这份 XML 里**没有** `<switchblocklist>`。回到源码第 480 行确认原因——它的 `<device>` 里开关盒类型是 `universal`（[arch.xml:167](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/arch/common/arch.xml#L167)），不是 `CUSTOM`，所以 `<switchblocklist>` 是可选的、可以缺省。

**需要观察的现象**：XML 顶层元素个数与源码里 `get_single_child` 调用个数的对应关系；可选元素缺省时不会报错。

**预期结果**：你能画出一张「XML 标签 → process 函数 → 写入 t_arch 哪个字段」的对照表，例如 `<switchlist>` → `process_switches` → `arch->switches`，`<segmentlist>` → `process_segments` → `arch->Segments`。

#### 4.1.5 小练习与答案

**练习 1**：为什么源码里 `process_segments` 必须在 `process_switches` 之后调用？

**参考答案**：因为线段（segment）的解析需要引用开关（switch）的名字——比如 `<wire_switch>` 和 `<opin_switch>` 指明线段在开关盒和连接盒里用哪个开关。`process_segments` 的参数里就传入了 `arch->switches`（见源码第 485 行），以便校验线段引用的开关名确实存在。所以必须先把 `<switchlist>` 解析成 `arch->switches`，再解析 `<segmentlist>`。

**练习 2**：架构文件里没有 `<switchblocklist>` 一定会报错吗？

**参考答案**：不一定。源码第 480 行表明，只有当 `<device>` 把开关盒类型设为 `CUSTOM` 时，`<switchblocklist>` 才是必需的（`SWITCHBLOCKLIST_REQD = REQUIRED`）；否则是 `OPTIONAL`，缺省完全合法。`arch.xml` 用的是 `universal`，所以不需要它。

---

### 4.2 `xml_read_arch` 函数签名与产物

#### 4.2.1 概念说明

`xml_read_arch` 是架构解析的**唯一对外入口**。它的职责很纯粹：吃一个 XML 文件路径，吐出三样东西——

1. 一个 `t_arch` 结构体：装「全局/器件级」的信息（开关、线段、开关盒拓扑、直连链、功耗参数、模型等）。这些信息不属于某个具体的块，而是整个器件共享的。
2. 一个 `std::vector<t_physical_tile_type>`：所有**物理瓦片类型**。物理瓦片是「器件网格上实际摆的那个方块」，关心的是尺寸（宽高）、引脚在四周怎么分布。
3. 一个 `std::vector<t_logical_block_type>`：所有**逻辑块类型**。逻辑块是「抽象的功能容器」（`pb_type` 层次），关心的是内部能装什么原语、怎么互连。

为什么要把物理瓦片和逻辑块分开成两个集合？因为 FPGA 存在「一个物理位置可以等价地实现多种逻辑块」（equivalent sites）的情况。u2-l2 会深入讲这两者的关系，这里只需记住：`xml_read_arch` 同时产出这两组，并在最后用 `link_physical_logical_types` 把它们绑定。

`timing_enabled` 参数控制是否解析时序相关属性（如开关延迟 `Tdel`）。如果用户跑的是 `--timing_analysis off`，解析器会跳过部分时序字段——但架构文件本身不变，只是有些字段被忽略。

#### 4.2.2 核心流程

`xml_read_arch` 的整体生命周期：

```
1. 扩展名校验 (.xml)        —— 非致命警告
2. architecture_id = hash    —— 唯一指纹
3. try {
     load_xml → 建 DOM
     依次 process 各顶层元素   —— 填充 t_arch / 两组块类型
     link / sync / check / mark_IO   —— 关联与校验
   }
4. catch (XmlError) { archfpga_throw(文件名, 行号, 信息) }   —— 转致命错误
```

它在 VPR 启动时被调用。调用链是：`main` → `vpr_init` → `vpr_setup` → `setup_vpr`，在 `setup_vpr.cpp` 里实际发起调用。注意 `setup_vpr` 还会根据 `options->arch_format` 在两种格式之间分派：VTR 经典 XML 格式走 `xml_read_arch`，FPGA Interchange 设备格式走另一条路径。

#### 4.2.3 源码精读

函数声明在头文件里，签名清晰展示了输入输出（[read_xml_arch_file.h:20-25](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/read_xml_arch_file.h#L20-L25)）：

```cpp
void xml_read_arch(std::string_view arch_file,
                   const bool timing_enabled,
                   t_arch* arch,
                   std::vector<t_physical_tile_type>& physical_tile_types,
                   std::vector<t_logical_block_type>& logical_block_types,
                   bool device_model_warnings);
```

读这张签名就能看出职责分工：`arch_file` 是输入；`arch`、`physical_tile_types`、`logical_block_types` 三个都是**输出参数**（指针/引用），由函数内部填充。

调用点在 `setup_vpr.cpp`，被包在一个 `switch (options->arch_format)` 里（[setup_vpr.cpp:193-201](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_vpr.cpp#L193-L201)）：

```cpp
switch (options->arch_format) {
    case e_arch_format::VTR:
        xml_read_arch(options->ArchFile.value().c_str(),
                      timingenabled,
                      arch,
                      device_ctx.physical_tile_types,
                      device_ctx.logical_block_types,
                      options->device_model_warnings);
        break;
    case e_arch_format::FPGAInterchange:
        VTR_LOG("Use FPGA Interchange device\n");
        ...
```

这里 `arch_format` 的两种取值定义在 `arch_types.h`（[arch_types.h:18-21](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/arch_types.h#L18-L21)）：

```cpp
enum class e_arch_format {
    VTR,            ///<VTR-specific device XML format
    FPGAInterchange ///<FPGA Interchange device format
};
```

注意调用点把解析产物直接写进了 `device_ctx.physical_tile_types` 和 `device_ctx.logical_block_types`——这正是 u3-l4 要讲的 `DeviceContext`。也就是说，`xml_read_arch` 的产出从一开始就挂载到了 VPR 的全局上下文上，后续所有阶段都从这里读架构信息。

产物结构体 `t_arch` 装的就是「全局器件信息」。挑几个关键字段看（[physical_types.h:1959-2018](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L1959-L2018)）：

```cpp
struct t_arch {
    mutable vtr::string_internment strings;   // <metadata> 标签的字符串驻留（省内存）
    std::string architecture_id;              // 上面算出的哈希指纹
    ...
    t_chan_width_dist Chans;                  // 布线通道宽度分布（来自 <device> 的 chan_width_distr）
    e_switch_block_type sb_type;              // 开关盒类型（来自 <device> 的 switch_block）
    std::vector<t_switchblock_inf> switchblocks;
    float R_minW_nmos;                        // 晶体管参数（来自 <sizing>）
    float R_minW_pmos;
    int Fs;                                   // 开关盒连通度 Fs
    float grid_logic_tile_area;               // 逻辑单元面积
    std::vector<t_segment_inf> Segments;      // 所有线段（来自 <segmentlist>）
    std::vector<t_arch_switch_inf> switches;  // 所有开关（来自 <switchlist>）
    std::vector<t_direct_inf> directs;        // 直连链（来自 <directlist>）
    LogicalModels models;                     // 原语模型（来自 <models>）
    ...
};
```

每个顶层 XML 元素基本都能在 `t_arch` 里找到对应的字段。比如开关盒类型、晶体管电阻、线段向量、开关向量，它们最终会成为布线图构建（u6-l1）和时序分析（u7）的输入。

至于单个开关的细节，看 `t_arch_switch_inf`（[physical_types.h:1760-1798](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L1760-L1798)），里面有名 `name`、等效电阻 `R`、输入/输出/内部电容 `Cin/Cout/Cinternal`、buffer 类型与面积等——这些正是 `<switchlist>` 里 `<switch name="sw" type="mux" R="1" Cin="..." .../>` 那些属性的去处。

#### 4.2.4 代码实践

**实践目标**：跟踪「命令行架构文件参数 → 内存里的 `t_arch`」这条链路。

**操作步骤**：

1. 从 [setup_vpr.cpp:193-201](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_vpr.cpp#L193-L201) 的调用点出发，确认 `options->ArchFile` 来自命令行 `--arch_file`（如需，可回看 u1-l5 讲的 `t_options`）。
2. 确认 `arch` 指针指向的对象、`device_ctx.physical_tile_types`、`device_ctx.logical_block_types` 这三个输出容器在调用前是空的，调用后被填充。
3. 打开 [physical_types.h:1959](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L1959) 的 `t_arch` 定义，对照 `arch.xml` 里的 `<device>`、`<switchlist>`、`<segmentlist>`，说出每个 XML 段落会落到 `t_arch` 的哪个成员（如 `R_minW_nmos`←`<sizing>`，`switches`←`<switchlist>`，`Segments`←`<segmentlist>`）。

**需要观察的现象**：解析产物的去向——它们并非停留在 `libarchfpga` 内部，而是被「搬」进了 `DeviceContext`。

**预期结果**：你能写出一张映射表，例如：

| XML 段落 | process 函数 | 产物落点 |
| --- | --- | --- |
| `<switchlist>` | `process_switches` | `arch->switches` |
| `<segmentlist>` | `process_segments` | `arch->Segments` |
| `<models>` | `process_models` | `arch->models` (LogicalModels) |
| `<tiles>` | `process_tiles` | `device_ctx.physical_tile_types` |
| `<complexblocklist>` | `process_complex_blocks` | `device_ctx.logical_block_types` |

> 待本地验证：若你已按 u1-l2 构建了 vpr，可在 `build/` 下用 gdb 给 `xml_read_arch` 下断点，跑一个最小设计，观察 `arch->Segments.size()` 与 XML 里 `<segment>` 个数是否一致。

#### 4.2.5 小练习与答案

**练习 1**：`xml_read_arch` 为什么要把 `physical_tile_types` 和 `logical_block_types` 作为两个**独立的输出参数**，而不是都塞进 `t_arch`？

**参考答案**：因为物理瓦片和逻辑块是两个不同抽象层次的概念，且它们的集合分别服务于不同阶段。物理瓦片（`t_physical_tile_type`）描述器件网格上的物理实体，是布局（placement）和布线图构建关心的；逻辑块（`t_logical_block_type`）描述功能容器，是打包（packing）关心的。把它们放在 `DeviceContext` 的两个并列成员里（而不是埋在 `t_arch` 内部），更符合 VPR 「按阶段/按上下文组织状态」的设计。`t_arch` 只保留真正的「全局器件级」信息。

**练习 2**：如果用户在命令行指定了 `FPGAInterchange` 格式，`xml_read_arch` 会被调用吗？

**参考答案**：不会。`setup_vpr.cpp` 的 `switch` 会在 `case e_arch_format::FPGAInterchange` 分支走另一条解析路径，`xml_read_arch`（VTR XML 格式专用）只在 `case VTR` 分支被调用。`xml_read_arch` 是 VTR 经典 XML 格式的入口，不负责 FPGA Interchange 格式。

---

### 4.3 pugixml 封装与错误处理

#### 4.3.1 概念说明

VTR 不直接裸用 pugixml，而是在外面套了一层叫 `pugiutil` 的封装（在 `libpugiutil` 库里）。这层封装主要解决两件事：

**第一，把「找不到节点/属性」这种常见情况变成异常。** 裸 pugixml 里，找一个不存在的子节点会返回一个「空节点」，你得手动判断 `if (!node)`。解析代码里有几十上百处取节点/属性，每处都手写判断会很啰嗦。`pugiutil` 的做法是：取节点/属性时传一个 `ReqOpt`（REQUIRED 或 OPTIONAL）参数，如果是 REQUIRED 又没找到，**直接抛 `XmlError` 异常**，带上文件名和行号。这样正常路径的代码非常干净。

**第二，维护「行号查询」能力。** pugixml 的 DOM 节点默认不记录它在原文件的第几行。但报错时我们极度需要行号（「第 42 行的 `<switch>` 缺少 `name` 属性」比「某个 switch 出错」有用得多）。`pugiutil::load_xml` 在加载时会额外构建一张「节点 → 行号」的映射表 `loc_data`，之后所有取节点/属性的调用都带上 `loc_data`，报错时就能给出精确行号。

几个核心封装函数：

- `load_xml(doc, filename)`：加载文件，返回 `loc_data`（行号查询表）。
- `get_single_child(node, name, loc_data, ReqOpt)`：取唯一指定名字的子节点；多于一个或（必需时）没有就抛异常。
- `get_attribute(node, name, loc_data, ReqOpt)`：取节点的某个属性；同上。
- `expect_only_children(node, names, loc_data)`：校验某节点的子节点**只允许**是给定名字之一——这是防止用户写错标签名的利器。

#### 4.3.2 核心流程

错误处理的链路是「抛 → 接 → 重新抛成致命错误」：

```
pugiutil 封装函数
    └─ 发现问题（缺节点/属性、多余标签、值非法）
         └─ throw XmlError(信息, 文件名, 行号)

xml_read_arch 的 try 块包住所有解析
    └─ catch (XmlError& e)
         └─ archfpga_throw(arch_file, e.line(), e.what())   // 致命，终止程序
```

`archfpga_throw`（在 `arch_error.h` 声明）标记了 `[[noreturn]]`——一旦走到这里，程序会打印带文件名和行号的错误信息并退出。这就是为什么 VPR 在架构文件有错时，总是能给出非常精确的定位。

此外，`read_xml_util.h` 里还有几个解析过程中主动报错的辅助函数，比如 `bad_tag`（遇到不允许的标签时调用）、`bad_attribute` / `bad_attribute_value`（属性名或值非法时调用），它们内部最终也是抛 `XmlError`。`BoolToReqOpt` 则是个小工具，把布尔值翻译成 `ReqOpt` 枚举，让调用点更可读。

#### 4.3.3 源码精读

异常类型 `XmlError` 继承自 `std::runtime_error`，额外携带文件名和行号（[pugixml_util.hpp:27-46](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libpugiutil/src/pugixml_util.hpp#L27-L46)）：

```cpp
//An error produced while getting an XML node/attribute
class XmlError : public std::runtime_error {
  public:
    XmlError(std::string msg = "", std::string new_filename = "", size_t new_linenumber = -1)
        : std::runtime_error(msg), filename_(new_filename), linenumber_(new_linenumber) {}
    std::string filename() const { return filename_; }
    size_t line() const { return linenumber_; }
  private:
    std::string filename_;
    size_t linenumber_;
};
```

`load_xml` 返回行号查询表 `loc_data`（[pugixml_util.hpp:48-52](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libpugiutil/src/pugixml_util.hpp#L48-L52)）：

```cpp
//Loads the XML file specified by filename into the passed pugi::xml_docment
//
//Returns loc_data look-up for xml node line numbers
loc_data load_xml(pugi::xml_document& doc, const std::string filename);
```

「必需 vs 可选」用枚举 `ReqOpt` 表达，避免裸布尔参数的歧义（[pugixml_util.hpp:67-70](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libpugiutil/src/pugixml_util.hpp#L67-L70)）：

```cpp
enum ReqOpt {
    REQUIRED,
    OPTIONAL
};
```

取子节点的核心封装，默认 REQUIRED，找不到就抛异常（[pugixml_util.hpp:83-93](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libpugiutil/src/pugixml_util.hpp#L83-L93)）：

```cpp
//Gets the child element of the given name and returns it.
//Errors if more than one matching child is found.
pugi::xml_node get_single_child(const pugi::xml_node node,
                                const std::string& child_name,
                                const loc_data& loc_data,
                                const ReqOpt req_opt = REQUIRED);
```

`xml_read_arch` 里的整个解析过程都被一个 `try` 包住，末尾统一捕获 `XmlError` 并转成致命错误（[read_xml_arch_file.cpp:438](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/read_xml_arch_file.cpp#L438) 与 [read_xml_arch_file.cpp:584-586](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/read_xml_arch_file.cpp#L584-L586)）：

```cpp
pugiutil::loc_data loc_data = pugiutil::load_xml(doc, arch_file.data());
...
} catch (pugiutil::XmlError& e) {
    archfpga_throw(arch_file.data(), e.line(), e.what());
}
```

`archfpga_throw` 是个不返回的致命函数（[arch_error.h:9](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/arch_error.h#L9)）：

```cpp
[[noreturn]] void archfpga_throw(const char* filename, int line, const char* fmt, ...);
```

最后看架构解析专用的辅助函数集合（[read_xml_util.h:8-22](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/read_xml_util.h#L8-L22)）：

```cpp
pugiutil::ReqOpt BoolToReqOpt(bool b);

void bad_tag(const pugi::xml_node node,
             const pugiutil::loc_data& loc_data,
             const pugi::xml_node parent_node = pugi::xml_node(),
             const std::vector<std::string>& expected_tags = std::vector<std::string>());

void bad_attribute(const pugi::xml_attribute attr,
                   const pugi::xml_node node,
                   const pugiutil::loc_data& loc_data, ...);
void bad_attribute_value(...);
```

`bad_tag` 在解析器遇到「不该出现的标签」时被调用，并把「期望的标签名列表」一起带进错误信息——所以 VPR 的报错常常是「第 N 行：遇到标签 X，期望的是 [A, B, C] 之一」，非常利于排错。这些函数内部最终都汇聚到 `XmlError` → `archfpga_throw` 这条链。

#### 4.3.4 代码实践

**实践目标**：亲手触发一次架构解析错误，观察 VPR 报错信息的精确程度。

**操作步骤**：

1. 把 `vtr_flow/arch/common/arch.xml` 复制一份到临时目录，比如 `temp_arch.xml`。
2. 故意制造一个错误：把根节点下的 `<switchlist>` 改成 `<switchlit>`（少个 `s`，typo），保存。
3. 用这份错误架构跑 vpr（命令格式参考 u1-l4/u1-l5，把 `--arch_file` 指向 `temp_arch.xml`，电路用任意一个最小 blif 即可）。
4. 观察 vpr 的报错输出。

**需要观察的现象**：报错信息里是否包含**文件名**和**行号**；是否提示「期望 `<switchlist>` 但找不到」之类的语义。

**预期结果**：因为 `<switchlist>` 在源码里是 `REQUIRED`（见 [read_xml_arch_file.cpp:476](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/read_xml_arch_file.cpp#L476)），改错名字后 `get_single_child` 找不到它，会抛 `XmlError`，最终 `archfpga_throw` 打印形如 `temp_arch.xml:line N: ...` 的错误并退出。

> 待本地验证：精确的报错文案取决于 `get_single_child` 内部实现，需实际运行确认。若尚未构建 vpr，至少能在源码层面确认「REQUIRED + 缺失 → 抛异常 → archfpga_throw」这条链必然被触发。

#### 4.3.5 小练习与答案

**练习 1**：`pugiutil` 为什么要发明 `ReqOpt` 枚举，而不是直接用 `bool required`？

**参考答案**：为了**调用点的可读性**。如果用布尔，`get_single_child(node, "power", loc_data, false)` 里的 `false` 是什么意思，不看函数签名根本猜不到（是「不必需」还是「不递归」？）。换成 `ReqOpt::OPTIONAL` 则一目了然。pugixml_util.hpp 第 54–66 行的注释专门解释了这一点。

**练习 2**：为什么 `load_xml` 要额外返回一个 `loc_data`，而 pugixml 原生的 `doc.load_file` 不需要？

**参考答案**：因为 pugixml 默认不在 DOM 节点上保存「原文件行号」信息（为了节省内存/加快解析）。但架构文件报错时行号至关重要。所以 `pugiutil::load_xml` 在加载的同时构建并返回一张「节点 → 行号」的查询表 `loc_data`，后续所有 `get_single_child`/`get_attribute`/`bad_tag` 都带上它，以便在出错时能取出精确行号写进 `XmlError`。

---

## 5. 综合实践

把三个最小模块串起来，完成本讲规格里给定的实践任务：**阅读一个示例架构 XML，并对照 `read_xml_arch_file` 实现，列出该架构定义的器件类型、开关盒与线段。**

**任务步骤**：

1. **选样本**：打开 [vtr_flow/arch/common/arch.xml](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/arch/common/arch.xml)（它足够小，适合手工通读）。如果你想要更有代表性的架构，可改选 `vtr_flow/arch/COFFE_22nm/k6_frac_N10_4add_2chains_depop50_mem20K_22nm.xml`（含 LUT、进位链、内存、DSP），但工作量更大。

2. **列器件类型**：找到 `<tiles>` 段，列出所有 `<tile>` 的 `name`。对 `arch.xml` 而言是 `ff_tile`、`io_tile`。再确认 `<layout>` 里它们各自被摆在什么位置（`fill`、`perimeter`、`corners`），以及虚拟的 `EMPTY` 类型用在四角。思考：这些类型最终会落到 `device_ctx.physical_tile_types` 还是 `logical_block_types`？（答：`<tiles>` → `physical_tile_types`，`<complexblocklist>` → `logical_block_types`。）

3. **列开关与开关盒**：找到 `<switchlist>`，列出每个 `<switch>` 的 `name`、`type`、`R`、`Tdel` 等关键属性。`arch.xml` 只有一个开关 `sw`（`type="mux"`）。再找到 `<device>` 里的 `<switch_block fs="3" type="universal"/>`，记录开关盒连通度 `Fs=3`、类型 `universal`。把这些对应到 `t_arch` 的 `switches`（`t_arch_switch_inf`）、`Fs`、`sb_type` 字段。

4. **列线段**：找到 `<segmentlist>`，列出每个 `<segment>` 的 `name`、`type`、`length`、`Rmetal`、`Cmetal`、`<sb>`/`<cb>` 模式。`arch.xml` 只有一条线段 `wire`（`type="bidir"`，`length="1"`）。对应到 `t_arch::Segments`（`t_segment_inf`）。

5. **对照源码验证**：打开 [read_xml_arch_file.cpp:456-498](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/read_xml_arch_file.cpp#L456-L498)，确认你列出的每个 XML 段落，在源码里都有对应的 `get_single_child` + `process_xxx` 调用，且写入 `t_arch` 的正确字段。

**交付物**：一份针对该架构的小结表，形如：

| 类别 | XML 来源 | 内容 | 落入的数据结构 |
| --- | --- | --- | --- |
| 器件类型（物理瓦片） | `<tiles>` | `ff_tile`, `io_tile` | `device_ctx.physical_tile_types` |
| 逻辑块类型 | `<complexblocklist>` | `ff_tile`, `io_tile`（pb_type 层次） | `device_ctx.logical_block_types` |
| 开关 | `<switchlist>` | `sw`（mux, R=1, Tdel=58e-12） | `arch->switches` |
| 开关盒 | `<device>/<switch_block>` | fs=3, universal | `arch->Fs`, `arch->sb_type` |
| 线段 | `<segmentlist>` | `wire`（bidir, length=1） | `arch->Segments` |

做完这张表，你就把「XML 文本 → 解析函数 → 内存结构」三层打通了——这正是后续 u2-l2（物理/逻辑类型）、u2-l3（DeviceGrid）的起点。

## 6. 本讲小结

- 架构 XML 的根节点是 `<architecture>`，其下有一组按依赖顺序处理的顶层元素：`models`、`layout`、`device`、`switchlist`、`segmentlist`、（可选 `switchblocklist`）、`complexblocklist`、`tiles`，以及若干可选扩展段。
- `xml_read_arch` 是解析的唯一入口，输入一个 XML 文件路径，输出三样东西：`t_arch`（全局器件信息）、`physical_tile_types`（物理瓦片）、`logical_block_types`（逻辑块）。
- 解析产物被直接写进 `DeviceContext`（`device_ctx.physical_tile_types` 等），成为后续所有阶段共享的架构真相来源——这就是「架构驱动」在代码层面的落点。
- VTR 用 pugixml 做 DOM 解析，外面包了一层 `pugiutil`，用 `ReqOpt`（REQUIRED/OPTIONAL）+ `loc_data`（行号表）+ `XmlError` 异常把「取节点/属性」简化成一行调用。
- 解析错误经 `XmlError` 抛出，被 `xml_read_arch` 的 `catch` 接住，再由 `archfpga_throw` 转成带「文件名 + 行号」的致命错误——这就是 VPR 报错总是精确到行的原因。
- 关键依赖关系：`<switchlist>` 必须先于 `<segmentlist>` 解析；`<switchblocklist>` 仅在开关盒类型为 `CUSTOM` 时必需。

## 7. 下一步学习建议

本讲只回答了「XML 是怎么被读进来的」。接下来应该深入「读进来的那些结构到底是什么意思」：

- **u2-l2 物理类型数据结构 physical_types**：深入 `t_physical_tile_type`、`t_logical_block_type`、`t_pb_type` 的层次模型，搞懂物理瓦片与逻辑块如何通过 `equivalent_sites` 关联——这正是本讲 `link_physical_logical_types` 做的事。
- **u2-l3 器件网格 DeviceGrid 的生成**：看 `<layout>` 解析出的信息如何变成二维网格 `DeviceGrid`，以及 `auto_layout` 如何按电路规模自动决定器件尺寸。

阅读源码时建议顺着本讲建立的映射表（XML 段 → process 函数 → t_arch 字段）往下钻：挑一个你感兴趣的 `process_xxx` 函数（比如 `process_complex_blocks` 或 `process_tiles`），看它如何把一个 XML 节点翻译成对应的结构体。这种「自顶向下追一个 process 函数」的练习，是从读懂解析器过渡到能改解析器的最快路径。
