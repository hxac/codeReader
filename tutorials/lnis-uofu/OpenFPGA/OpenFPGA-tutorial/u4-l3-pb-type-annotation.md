# pb_type 注解：把 VPR 结构绑定到物理电路

## 1. 本讲目标

在 u3-l2 里我们已经知道：`openfpga_arch.xml` 的七个顶层节点中，`pb_type_annotations` 负责「把 VPR 架构里的逻辑块用名字绑定到电路模型」。本讲就把这个节点彻底打开。读完本讲，你应当能够：

1. 说清 **operating pb_type**（运行/可打包模式）与 **physical pb_type**（物理实现模式）的区别，以及 OpenFPGA 为什么要把这两者分开。
2. 看懂 `pb_type` 路径语法，例如 `clb.fle[n1_lut4].ble4.lut4` 中每一级的含义，并能判断它指向的是 operating 还是 physical pb_type。
3. 理解 `mode_bits` 如何决定一个多模式物理电路（如 IO 的 GPIO）到底工作在哪种模式。
4. 结合 [PbTypeAnnotation](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/pb_type_annotation.h#L33-L191) 数据结构与真实 XML，画出「VPR pb_type → circuit_model」的绑定关系图。

## 2. 前置知识

- **VPR 的 pb_type 与 mode**：VPR 用一棵 pb_type 树描述逻辑块内部结构。一个 pb_type 可以有多个 `<mode>`，每个 mode 下挂一组子 pb_type。打包器（packer）会把用户网表里的单元塞进「可打包」的 mode。
- **circuit_model 与 circuit_library**：这是 u3-l3 讲过的内容。`circuit_library` 是 OpenFPGA 电路级架构的中央仓库，每个 `circuit_model`（如 `lut4`、`DFFSRQ`、`GPIO`）描述一种物理积木。本讲要做的事，就是把 VPR 的 pb_type「指针」指到这些 circuit_model 上。
- **解析存名字、链接查 ID 的两段式**：u3-l3 提过，电路模型之间用 `circuit_model_name`（字符串）相互引用，解析阶段只存名字，链接阶段（`build_model_links()`）才把名字翻译成 `CircuitModelId`。`pb_type_annotations` 里的 `circuit_model_name` 也是这个套路——解析期存字符串，链接期对账。

一句话定位：**u3-l1 讲了两套架构文件，u3-l2 列出了 `pb_type_annotations` 这个节点，u3-l3 讲清了被引用的 circuit_library，本讲则负责把它们「焊」在一起。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [libs/libarchopenfpga/src/pb_type_annotation.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/pb_type_annotation.h#L33-L191) | `PbTypeAnnotation` 类：每条 `<pb_type>` 注解解析后的内存表示，存放三种绑定（operating↔physical、pb_type↔circuit_model、interconnect↔circuit_model）。 |
| [libs/libarchopenfpga/src/read_xml_pb_type_annotation.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_pb_type_annotation.cpp#L159-L288) | XML 解析器：把 `<pb_type>` 节点翻译成 `PbTypeAnnotation` 对象，并用 `name`/`physical_pb_type_name` 两个属性区分 operating 与 physical。 |
| [openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L174-L190) | 真实示例：io 与 clb 的 `pb_type_annotations` 段，是本讲的主要观察对象。 |
| [openfpga_flow/vpr_arch/k4_N4_tileable_40nm.xml](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/vpr_arch/k4_N4_tileable_40nm.xml#L174-L226) | 配套 VPR 架构：定义了 io、clb 的 pb_type 树与各 mode，是「被绑定」的那一侧。 |

---

## 4. 核心概念与源码讲解

### 4.1 PbTypeAnnotation 数据结构：一条注解的内存表示

#### 4.1.1 概念说明

`openfpga_arch.xml` 里 `<pb_type_annotations>` 下可以有任意多条 `<pb_type>` 子节点。每一条 `<pb_type>` 注解解析后，在内存里就是**一个** `PbTypeAnnotation` 对象。设计原则写在头文件注释里：

> Keep this data structure as general as possible. It is supposed to contain the raw data from architecture XML!

也就是说，`PbTypeAnnotation` 只存「原始数据」，不直接持有别的数据结构（比如它不存 `CircuitModelId`，只存 `circuit_model_name` 字符串）。把字符串翻译成 ID 是链接阶段（u5）的工作。这个「只存名字、不建指针」的原则与 u3-l3 讲的 circuit_library 完全一致。

一个 `PbTypeAnnotation` 最多承载三类绑定：

1. **operating pb_type ↔ physical pb_type**：告诉 OpenFPGA「用户网表被打包进这个 operating pb_type 时，实际由哪个 physical pb_type 实现」。
2. **physical pb_type ↔ circuit_model**：告诉 OpenFPGA「这个物理 pb_type 用哪个电路模型搭」。
3. **interconnect ↔ circuit_model**：把 pb_type 内部的某条 interconnect（如 crossbar）绑定到一个 mux 类电路模型。

#### 4.1.2 核心流程：解析器如何区分 operating 与 physical

解析一条 `<pb_type>` 节点的关键，是看它有没有 `physical_pb_type_name` 属性：

- **同时有 `name` 和 `physical_pb_type_name`** → 这是一条 **operating pb_type** 注解（描述「运行模式下的 pb_type 指向哪个物理 pb_type」）。
- **只有 `name`** → 这是一条 **physical pb_type** 注解（描述「物理 pb_type 本身，并可绑定 circuit_model」）。
- **只有 `name` 且出现在父级 pb_type（如 `io`、`clb`）上** → 通常用来声明 `physical_mode_name` / `idle_mode_name`，把整个 complex block 的物理模式标出来。

`name` 和 `physical_pb_type_name` 都是带层级的路径串，由 `openfpga::PbParser` 拆成「叶子名 + 祖先 pb_type 名 + 经过的 mode 名」三部分分别存储。流程伪代码如下：

```
read_xml_pb_type_annotation(xml_pb_type):
    解析 name, physical_pb_type_name 两个属性
    if name 非空 且 physical_pb_type_name 非空:
        → operating：用 PbParser 拆 name 存到 operating_*，拆 physical_pb_type_name 存到 physical_*
    elif name 非空 且 physical_pb_type_name 为空:
        → physical：用 PbParser 拆 name 存到 physical_*
    继续解析 physical_mode_name / idle_mode_name / mode_bits
    若是 physical：可选解析 circuit_model_name
    若是 operating：可选解析 index factor/offset、<port>、
    push 到结果 vector
```

#### 4.1.3 源码精读

`PbTypeAnnotation` 类把三类绑定分开存为成员，下面是 operating/physical 绑定部分，注释里直接给了路径语法的例子：

[pb_type_annotation.h:87-109](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/pb_type_annotation.h#L87-L109) —— 这段注释说明 `clb.fle[frac_logic].frac_lut6` 这种串里，方括号 `[frac_logic]` 是**经过的 mode 名**，`frac_lut6` 是叶子 pb_type 名。解析后，叶子名存进 `operating_pb_type_name_`（或 `physical_pb_type_name_`），各级祖先 pb_type 名与 mode 名分别存进 `*_parent_pb_type_names_` 与 `*_parent_mode_names_`。

[read_xml_pb_type_annotation.cpp:172-210](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_pb_type_annotation.cpp#L172-L210) —— 解析器用 `name`/`physical_pb_type_name` 的有无来判定类型，并用 `PbParser` 把路径拆成 leaf/parents/modes 三段。

类型判定最终落到两个访问器上：

[pb_type_annotation.cpp:35-56](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/pb_type_annotation.cpp#L35-L56) —— `is_operating_pb_type()` 当 operating 名和 physical 名都非空时为真；`is_physical_pb_type()` 当 operating 名为空、physical 名非空时为真。注意一个对象**不会**同时是两者。

interconnect↔circuit_model 绑定用一个 map 存：

[pb_type_annotation.h:188-190](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/pb_type_annotation.h#L188-L190) —— `interconnect_circuit_model_names_`：key 是 interconnect 名（如 `crossbar`），value 是 circuit_model 名（如 `mux_tree`）。

#### 4.1.4 代码实践：给五条注解分类

**实践目标**：学会用「有没有 `physical_pb_type_name`」这一条规则，判别一条 `<pb_type>` 注解的类型。

**操作步骤**：

1. 打开 [k4_N4_40nm_cc_openfpga.xml 的 pb_type_annotations 段](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L174-L190)（第 174–190 行）。
2. 对其中每一条 `<pb_type ...>`，看它是否带 `physical_pb_type_name` 属性。
3. 按 operating / physical / 模式声明 三类归档。

**预期结果**（一张分类表）：

| 行号 | `name` | 是否有 `physical_pb_type_name` | 分类 |
| --- | --- | --- | --- |
| 176 | `io` | 无 | 模式声明（声明 io 的 physical/idle mode） |
| 177 | `io[physical].iopad` | 无 | physical（绑定 GPIO） |
| 178 | `io[inpad].inpad` | 有（=`io[physical].iopad`） | operating |
| 179 | `io[outpad].outpad` | 有（=`io[physical].iopad`） | operating |
| 183 | `clb` | 无 | 模式声明（含子 `<interconnect>`） |
| 187 | `clb.fle[n1_lut4].ble4.lut4` | 无 | physical（绑定 lut4） |
| 188 | `clb.fle[n1_lut4].ble4.ff` | 无 | physical（绑定 DFFSRQ） |

**需要观察的现象**：注意第 187、188 行虽然 `name` 是带层级的路径，但因为**没有** `physical_pb_type_name`，它们被判定为 physical 而非 operating。这印证了「判定只看 `physical_pb_type_name` 的有无，与路径长短无关」。

#### 4.1.5 小练习与答案

**练习 1**：如果一条 `<pb_type>` 同时写了 `name` 和 `physical_pb_type_name`，`is_physical_pb_type()` 返回什么？

**答案**：返回 `false`。因为 `is_physical_pb_type()` 要求 operating 名为空；同时填两个名字时它是一条 operating 注解，`is_operating_pb_type()` 才为真。

**练习 2**：为什么 `PbTypeAnnotation` 里存的是 `circuit_model_name`（字符串）而不是 `CircuitModelId`？

**答案**：头文件注释明确要求该结构「只存架构 XML 的原始数据」。把名字翻译成 ID 属于链接阶段（u5）的事，且解析期 circuit_library 可能尚未完全建好，过早建指针会破坏模块化与解析/链接的两段式分离。

---

### 4.2 物理模式映射：operating 与 physical 的桥梁

#### 4.2.1 概念说明

VPR 的 pb_type 可以有多个 mode。以 [VPR 架构里的 io 块](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/vpr_arch/k4_N4_tileable_40nm.xml#L174-L226) 为例，`io` 有三个 mode：

- `physical`（第 180 行，`disable_packing="true"`）→ 含子 pb_type `iopad`；
- `inpad`（第 199 行）→ 含子 pb_type `inpad`；
- `outpad`（第 209 行）→ 含子 pb_type `outpad`。

关键在于 `disable_packing="true"`：**physical 模式不可打包**。VPR 打包器永远不会把用户网表塞进 physical 模式；它只会塞进 `inpad`/`outpad` 这种可打包（operating）模式。那 physical 模式有什么用？它是写给 OpenFPGA 看的——OpenFPGA 生成 fabric 时，**真正实现到电路里的是 physical 模式下的 pb_type**（即 `iopad` → GPIO）。

于是出现一个映射问题：用户网表被 VPR 打包进了 `inpad`（operating），但 fabric 里只有 `iopad`（physical）这一种硬件。OpenFPGA 需要知道「`inpad` 由 `iopad` 实现」——这就是 operating↔physical 映射要解决的事。

- **physical pb_type**：physical 模式下的 pb_type，代表真实硬件电路（如 `io[physical].iopad`）。
- **operating pb_type**：可打包模式下的 pb_type，代表用户网表被打包进去的形态（如 `io[inpad].inpad`、`io[outpad].outpad`）。

对 clb 则是另一种简单情况：`fle` 只有一个 mode `n1_lut4`。**当只有一个 mode 时，该 mode 默认就是 physical 模式**（XML 注释 [第 182 行](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L182) 也写了「physical mode will be the default mode if not specified」）。所以 clb 下的 `lut4`、`ff` 直接是 physical，无需 operating↔physical 桥接。

#### 4.2.2 核心流程：一条 operating→physical→circuit_model 的绑定链

一条完整的绑定链分三步：

1. **声明物理模式**：在父 pb_type（`io`）上用 `physical_mode_name="physical"` 指明「physical 模式叫这个名字」；`idle_mode_name="inpad"` 指明「空闲时让 io 停在 inpad 模式」。
2. **绑定 physical pb_type → circuit_model**：`io[physical].iopad` → `circuit_model_name="GPIO"`，告诉 OpenFPGA 物理硬件用 GPIO 这个电路模型搭。
3. **绑定 operating pb_type → physical pb_type**：`io[inpad].inpad` 用 `physical_pb_type_name="io[physical].iopad"` 指向第 2 步的 physical pb_type。打包进 `inpad` 的网表，最终落在 `iopad`/GPIO 上。

路径语法的形式化表述：

> `pb0[mode1].pb1[mode2]. ... .pbN`
>
> —— `pb0` 是顶层 pb_type；`[mode1]` 是 **pb1 所在的 mode 名**；`pb1` 是该 mode 下的子 pb_type；依此类推，`pbN` 是叶子。

这正是 [pb_type_annotation.h:91-96](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/pb_type_annotation.h#L91-L96) 注释里 `clb.fle[frac_logic].frac_lut6` 的读法：`fle` 是顶层 clb 下的子 pb_type，`[frac_logic]` 是 `frac_lut6` 所在的 mode。

**进阶：operating↔physical 的索引与端口对齐**。当一个 operating pb_type 实例（`num_pb>1`）映射到同一个 physical pb_type 时，二者的实例下标和引脚号未必一一对应。OpenFPGA 用三个量做线性对齐，设 operating 下标为 \(i\)，则映射到 physical 下标为：

\[ i_{\text{phys}} = i \times \text{factor} + \text{offset} \]

引脚号还可用「初始偏移 + 轮转偏移」对齐。这些机制主要服务于可裂变 LUT（fracturable LUT）、加法器、乘法器、BRAM 这类多 operating 共享一物理的场景，k4_N4 这种不可裂变架构用不上。

#### 4.2.3 源码精读

physical/idle 模式名与索引对齐字段：

[pb_type_annotation.h:111-148](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/pb_type_annotation.h#L111-L148) —— `physical_mode_name_` 标记哪个 mode 是物理实现；`physical_pb_type_index_factor_` 与 `physical_pb_type_index_offset_` 实现上面的线性下标对齐（注释里给了 adder 的例子：factor=2 把 `adder[5]` 映射到 `adder[10]`）。

operating→physical 的端口对齐表：

[pb_type_annotation.h:150-186](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/pb_type_annotation.h#L150-L186) —— `operating_pb_type_ports_` 是「operating 端口名 → (physical 端口 → [初始偏移, 引脚轮转偏移, 端口轮转偏移])」的嵌套 map。注释里 bram `dout[32]→dout_a[0]` 的初始偏移 `-32`、乘法器引脚轮转偏移 `9` 都是经典例子。

解析侧，`physical_mode_name` / `idle_mode_name` 对两类 pb_type 都适用，而 index factor/offset 只对 operating 解析：

[read_xml_pb_type_annotation.cpp:212-254](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_pb_type_annotation.cpp#L212-L254) —— 注意第 236 行 `circuit_model_name` **只在 physical 分支里解析**（因为 operating 不直接对应电路模型），第 245 行 index factor/offset **只在 operating 分支里解析**。

#### 4.2.4 代码实践：追踪 io 的三步绑定链

**实践目标**：跨两份架构文件，把 `inpad` 这一个 operating pb_type 一路追到 GPIO 电路模型。

**操作步骤**：

1. 在 [k4_N4_40nm_cc_openfpga.xml:176](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L176) 找到 io 的 `physical_mode_name="physical"`。
2. 在 [VPR 架构 io 块:180-193](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/vpr_arch/k4_N4_tileable_40nm.xml#L180-L193) 确认 physical 模式下确有 `iopad` 子 pb_type，且 `disable_packing="true"`。
3. 在 [openfpga_arch:177](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L177) 看到 `io[physical].iopad` → `GPIO`。
4. 在 [openfpga_arch:178](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L178) 看到 `io[inpad].inpad` 的 `physical_pb_type_name="io[physical].iopad"`。

**预期结果**（绑定链）：

```
io[inpad].inpad  (operating, VPR 把 .input 打包到这里)
        |  physical_pb_type_name
        v
io[physical].iopad  (physical, disable_packing)
        |  circuit_model_name
        v
       GPIO  (circuit_library 里的 iopad 模型)
```

**需要观察的现象**：`outpad`（第 179 行）也指向同一个 `io[physical].iopad`。也就是说，inpad 和 outpad 两种 operating 形态共享同一块 GPIO 硬件——它们靠什么区分？这正是下一节 `mode_bits` 的主题。

> 待本地验证：若环境已 `source openfpga.sh`，可对照 `run-task basic_tests/full_testbench/configuration_chain` 跑通后，在生成的 grid Verilog 里找到 `iopad`/`GPIO` 实例确认该链路。

#### 4.2.5 小练习与答案

**练习 1**：`clb.fle[n1_lut4].ble4.lut4` 这条路径里，`n1_lut4` 是什么？`lut4` 又是什么？

**答案**：`n1_lut4` 是 `ble4` 所在的 **mode 名**（即 fle 的一个可打包模式）；`lut4` 是 ble4 下的叶子 **pb_type 名**。整条路径定位到「fle 在 n1_lut4 模式下、ble4 里的 lut4」。

**练习 2**：为什么 io 块需要 operating↔physical 映射，而 clb 里的 lut4 不需要？

**答案**：io 有多个 mode（physical/inpad/outpad），physical 不可打包，inpad/outpad 可打包，需要把可打包形态映射到物理形态。而 clb 的 fle 只有一个 mode `n1_lut4`，按默认规则该 mode 即 physical 模式，lut4 直接是 physical，无需桥接，所以第 187 行直接用 `circuit_model_name` 绑定即可。

---

### 4.3 mode_bits：决定多模式物理电路的配置位

#### 4.3.1 概念说明

回到 inpad/outpad 共享 GPIO 的问题。看 [GPIO 电路模型定义:152-160](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L152-L160)，注意第 157 行那个端口：

```xml
<port type="sram" prefix="DIR" size="1" mode_select="true" circuit_model_name="DFF" default_val="1"/>
```

这是一个 `mode_select="true"` 的 sram 端口，名叫 `DIR`（方向）。它就是用来「选择 GPIO 工作模式」的配置位——置 1 是一个方向（输入），置 0 是另一个方向（输出）。**`mode_bits` 就是写给这些 `mode_select` sram 的值。**

于是注解里：

- `io[inpad].inpad` → `mode_bits="1"`：当网表打包进 inpad，把 DIR 配成 `1`，GPIO 当输入用。
- `io[outpad].outpad` → `mode_bits="0"`：当网表打包进 outpad，把 DIR 配成 `0`，GPIO 当输出用。
- `io[physical].iopad` → `mode_bits="1"`：physical 形态自身的默认配置位。

这就解释了「同一 GPIO 模型、不同 mode_bits」的本质：mode_bits 不是用来选电路模型，而是**写入物理电路内部的模式选择 sram**，从而复用同一硬件实现多种功能。

注意区分两类配置位（u3-l3 已铺垫）：

| sram 端口标志 | 含义 | 取值来源 |
| --- | --- | --- |
| `mode_select="true"` | 模式选择位，决定电路工作模式 | 来自 `pb_type` 注解的 `mode_bits` |
| 普通 sram（无 `mode_select`） | 功能配置位，如 LUT 的真值表 | 来自综合/布局布线结果（见 u7） |

以 [lut4 模型:131-141](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L131-L141) 为对照：它的 `sram` 端口（第 140 行，size=16）没有 `mode_select`，这 16 位是 LUT 真值表，由用户逻辑决定，不由 `mode_bits` 决定——所以第 187 行 `lut4` 注解也没有 `mode_bits`。

#### 4.3.2 核心流程：mode_bits 从字符串到配置位

`mode_bits` 在 XML 里是一个字符串（如 `"1"`、`"0"`，复杂场景也可能是 `"10"`、带分隔符或含 `x`）。解析流程：

1. 解析器调用 `parse_mode_bits()`，内部用 `openfpga::BitsParser` 把字符串解析成 `std::vector<char>`（每个元素是 `'0'`/`'1'`/`'x'`）。
2. 存入 `PbTypeAnnotation::mode_bits_`。
3. 链接阶段（u5）与比特流生成阶段（u7）消费它：当某 operating pb_type 被打包，就把它的 mode_bits 写到对应 physical pb_type 的 `mode_select` sram 里。

一个细节：**don't-care 位（`x`）只允许出现在 operating 注解里**。看 [解析调用:224-231](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_pb_type_annotation.cpp#L224-L231)，第 4 个参数 `accept_dont_care_bits` 取的是 `!is_physical_pb_type()`——即 operating 允许 `x`，physical 不允许。因为 physical 的 mode_bits 最终要变成确定的配置位，必须完全确定；而 operating 只约束它关心的那几位，其余可「不在乎」。

#### 4.3.3 源码精读

`mode_bits_` 字段与访问器：

[pb_type_annotation.h:118-119](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/pb_type_annotation.h#L118-L119) —— `mode_bits_` 注释写明「Configuration bits to select an operating mode for the circuit model」。

解析入口：

[read_xml_openfpga_arch_utils.cpp:24-35](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_openfpga_arch_utils.cpp#L24-L35) —— `parse_mode_bits()` 构造 `BitsParser` 并返回 `result()`；格式非法时抛错。

BitsParser 的字符规则：

[openfpga_bits_parser.cpp:58-74](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libopenfpgautil/src/openfpga_bits_parser.cpp#L58-L74) —— `'0'`/`'1'` 直接收；`'x'` 只有在 `accept_dont_care_bits_` 为真时才接受。这正对应上面「physical 不允许 x」的约束。

真实注解对照：

[k4_N4_40nm_cc_openfpga.xml:177-179](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L177-L179) —— iopad/inpad 是 `mode_bits="1"`，outpad 是 `mode_bits="0"`。

#### 4.3.4 代码实践：解释 inpad/outpad 为何同模型不同 mode_bits

**实践目标**：亲手把「同一 GPIO、不同 mode_bits」的机制讲透。

**操作步骤**：

1. 读 [GPIO 模型:152-160](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L152-L160)，确认它只有一个 `mode_select` sram 端口 `DIR`（size=1）。
2. 读 inpad/outpad 两条注解（[第 178、179 行](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L178-L179)），它们的 `physical_pb_type_name` 都是 `io[physical].iopad`，但 mode_bits 一个 `1`、一个 `0`。
3. 推理：`DIR=1` 与 `DIR=0` 各对应一个 IO 方向。

**预期结论**：inpad 和 outpad 复用同一块 GPIO 硬件；它们并不需要两个不同的电路模型，而是通过 `mode_bits` 给 GPIO 内部的方向选择位 `DIR` 写入不同值，从而把同一个物理 pad 配置成输入或输出。这就是 `mode_bits` 的核心价值——**用一个物理电路 + 少量配置位实现多种工作模式**。

**需要观察的现象**：如果删掉 `DIR` 端口的 `mode_select="true"`（仅作思想实验，不要改源码），`mode_bits` 就找不到写入目标，链接阶段会报不匹配。这说明 mode_bits 与 `mode_select` sram 是一一对应的契约关系。

> 待本地验证：可在生成的 fabric 比特流里查找与 IO 相关的 mode-select 位，观察输入 pad 与输出 pad 的 DIR 位确实分别是 1 和 0。

#### 4.3.5 小练习与答案

**练习 1**：为什么 [lut4 注解:187](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L187) 没有 `mode_bits`？

**答案**：因为 lut4 的 16 位 sram（[第 140 行](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L140)）不是 `mode_select`，而是存 LUT 真值表的功能位，取值由用户逻辑决定（u7 详述），不由架构静态指定，所以不需要 `mode_bits`。

**练习 2**：如果把 `io[outpad].outpad` 的 `mode_bits` 从 `0` 改成 `1`（仅思想实验），会发生什么？

**答案**：outpad 仍绑定到 GPIO，但 DIR 会被配成 `1`（与 inpad 相同），GPIO 方向配置错误，输出 pad 在物理上被当成输入，功能与仿真都会出错。这反过来说明 mode_bits 是真正进入比特流的配置数据，不是装饰。

---

## 5. 综合实践：画出 io 与 clb 的完整绑定关系图

本任务把三个模块串起来，产出一张「VPR pb_type → circuit_model」的全景图。

**任务**：在 [k4_N4_40nm_cc_openfpga.xml:174-190](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L174-L190) 找到 io 与 clb 的全部 `pb_type_annotations`，结合 [VPR 架构 io/clb 定义](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/vpr_arch/k4_N4_tileable_40nm.xml#L174-L323) 与 [circuit_library](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L121-L160)，画出绑定关系图，并解释 inpad/outpad 同模型不同 mode_bits。

**参考答案（io 部分）**：

```
VPR io 块 (3 modes)
├── mode "physical" (disable_packing) ── pb_type iopad
│        └─ pb_type注解: io[physical].iopad  ──circuit_model_name──▶  GPIO (mode_bits=1)
├── mode "inpad"  ── pb_type inpad           (VPR 把 .input 打包到这)
│        └─ 注解: io[inpad].inpad  ──physical_pb_type_name──▶ io[physical].iopad
│                                └─ mode_bits="1" 写入 GPIO.DIR (mode_select)  ▶ 当输入
└── mode "outpad" ── pb_type outpad          (VPR 把 .output 打包到这)
         └─ 注解: io[outpad].outpad ──physical_pb_type_name──▶ io[physical].iopad
                                 └─ mode_bits="0" 写入 GPIO.DIR (mode_select)  ▶ 当输出

父注解: io  physical_mode_name="physical"  idle_mode_name="inpad"
```

**参考答案（clb 部分）**：

```
VPR clb 块 → fle (num_pb=4) → mode "n1_lut4"(唯一mode=默认physical) → ble4
├── 父注解: clb  下挂 <interconnect name="crossbar" circuit_model_name="mux_tree"/>
│           （clb 内部全交叉矩阵 crossbar 用 mux_tree 电路实现）
├── ble4.lut4  ──circuit_model_name──▶ lut4   (16位 sram = 真值表, 非 mode_select, 无 mode_bits)
└── ble4.ff    ──circuit_model_name──▶ DFFSRQ (set/reset 触发器)
```

**关键解释（为什么 inpad/outpad 同 GPIO 却 mode_bits 不同）**：FPGA 的 IO pad 在物理上是同一个双向焊盘（GPIO），既可输入也可输出。OpenFPGA 不为输入、输出各做一个电路模型，而是用一个 GPIO 模型 + 一个 `mode_select` 方向位 `DIR`。inpad/outpad 是 VPR 打包侧的两种 operating 形态，它们都映射到同一个 physical `iopad`（即 GPIO），靠 `mode_bits`（1 或 0）把 DIR 配成不同方向。于是「同一模型、不同 mode_bits」就实现了「同一硬件、两种功能」。

**可选验证**（待本地验证）：

1. `source openfpga.sh` 后执行 `run-task basic_tests/full_testbench/configuration_chain`。
2. 在结果目录的 grid 网表里搜索 `GPIO` 实例，确认 io 块用的是它。
3. 在 fabric 比特流 / testbench 里观察 IO 相关的 mode-select 位，确认输入 pad 与输出 pad 的 DIR 位取值相反。

## 6. 本讲小结

- 一条 `<pb_type>` 注解解析成一个 [PbTypeAnnotation](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/pb_type_annotation.h#L33-L191)，只存原始字符串（名字），不存 ID；它承载 operating↔physical、pb_type↔circuit_model、interconnect↔circuit_model 三类绑定。
- **判定类型的唯一依据**是 `physical_pb_type_name` 的有无：有则 operating，无则 physical。
- **operating pb_type** 在可打包 mode 里（VPR 打包目标），**physical pb_type** 在不可打包的 physical mode 里（OpenFPGA fabric 实现目标）；单 mode 时该 mode 默认即 physical。
- 路径语法 `a[mode].b[mode].c` 中，方括号是下一级 pb_type 所在的 **mode 名**。
- `mode_bits` 写入物理电路的 `mode_select` sram，从而用一个物理模型 + 配置位实现多种模式；inpad/outpad 共享 GPIO、靠 `mode_bits` 1/0 区分方向就是典型应用。
- `mode_bits` 中的 `x`（don't-care）只允许出现在 operating 注解里，physical 必须完全确定。

## 7. 下一步学习建议

- 本讲只讲了 `pb_type_annotations` 的**解析与数据结构**（架构侧）。这些名字字符串如何被翻译成真正的 `CircuitModelId`、又如何与 VPR 的 pb graph 对账，是 u5（架构加载、解析与 VPR 标注）的主题，尤其是 u5-l3「link_openfpga_arch」与 u5-l4「VPR 标注子系统」。
- `mode_bits` 最终如何进入比特流，将在 u7-l2「build_architecture_bitstream：grid/routing/mux 位生成」中看到——届时会再次提到 `mode_select` 位。
- 若你想立刻看到一个多模式、可裂变 LUT 的复杂例子（用到 index factor/offset 与 port 轮转偏移），可以跳读 `openfpga_flow/openfpga_arch/` 下带 `frac` 字样的架构文件，作为本讲进阶机制的对照。
