# 配置协议 configuration_protocol

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚「配置协议（configuration protocol）」到底在解决什么问题，它和比特流是什么关系。
- 区分 OpenFPGA 支持的五种配置协议：`standalone`、`scan_chain`、`memory_bank`、`ql_memory_bank`、`frame_based`，并说出它们各自适合什么场景。
- 解释 `configuration_protocol` 节点里 `type` 与 `circuit_model_name` 两个属性的含义，以及它们在源码里如何被解析、链接、校验。
- 认识 `memory_bank`/`ql_memory_bank` 下的三种 BL/WL 子协议：`flatten`、`decoder`、`shift_register`。
- 看到 arch 文件名里的 `cc` / `bank` / `frame` / `qlbank` / `qlbanksr` 等后缀时，能立刻推断它用的是哪种配置协议。

本讲是 u3（架构描述与输入文件）的收尾，承接 u3-l2（openfpga_arch.xml 总体结构）和 u3-l3（电路库 circuit_library）。configuration_protocol 是 openfpga_arch.xml 七大顶层节点之一，它决定了「FPGA 里成千上万个配置位用什么电路存、用什么方式写进去」。

## 2. 前置知识

### 2.1 可编程存储器（configurable memory）与配置位（config bit）

FPGA 之所以「可编程」，是因为芯片里散布着大量小小的存储单元（一个bit 一个bit 地存 0 或 1），它们控制着：

- LUT 里真值表的每一项；
- 多路选择器（MUX）选哪条输入；
- 布线开关盒（switch box）里哪些开关闭合；
- IO pad 是输入还是输出方向。

这些存储单元统称**可编程存储器（configurable memory）**，每一个存的值叫一个**配置位（configuration bit，简称 config bit）**。把全部配置位串起来，就是 u7 要讲的**比特流（bitstream）**。

> 一个 k4_N4 的小阵列就有上千个配置位，真实的 FPGA 动辄几百万个。

### 2.2 配置协议（configuration protocol）= 「怎么把比特流灌进去」

配置位本身只是存储电路，但**怎么把这成千上万个 0/1 写进这些存储电路**，是一个独立的工程问题。你可以想象几种极端方式：

- **每个配置位都拉一根线出来**：写起来最直接，但引脚数 = 配置位数，对几百万位的 FPGA 完全不可行。
- **全部串成一条链，一位一位移位写入**：只要 2~3 根引脚（数据 + 时钟），但要写 N 位就得打 N 个时钟。
- **排成矩阵，用行地址 + 列地址选中某一位**：引脚数和速度都比较折中。

「**用什么样的拓扑把这些存储单元组织起来、又用什么时序去访问它们**」——这就是**配置协议**。它和「存储单元本身用什么电路」是两件事：

- 存储电路（如 D 触发器、SRAM、锁存器）由 `circuit_library` 里的电路模型描述（见 u3-l3）。
- 组织和访问方式由本讲的 `configuration_protocol` 描述。

### 2.3 BL/WL：借自 SRAM 阵列的行/列寻址

如果你接触过存储器阵列，应该熟悉 **Word Line（WL，字线）** 和 **Bit Line（BL，位线）**：存储单元排成二维矩阵，一根字线选中「某一行」，一根位线负责「往这一行的某个单元写数据 / 读数据」。选中某个单元 = 同时激活它所在的字线和位线。OpenFPGA 的 `memory_bank` 类协议就是借用这个模型来寻址配置位。

### 2.4 和上一讲的衔接

u3-l2 已经指出：`<configuration_protocol>` 是 `openfpga_arch.xml` 的七大顶层节点之一，里面的 `scan_chain` 在 `<organization type="scan_chain" circuit_model_name="DFF"/>` 处声明。本讲就是把这个节点彻底打开——讲清 `type` 有几种取值、`circuit_model_name` 指向哪种电路模型、以及背后的数据结构 `ConfigProtocol`。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `libs/libarchopenfpga/src/circuit_types.h` | 定义配置协议类型枚举 `e_config_protocol_type` 与字符串表 |
| `libs/libarchopenfpga/src/config_protocol.h` / `.cpp` | `ConfigProtocol` 数据结构：存协议类型、存储模型、区域数、BL/WL 子协议 |
| `libs/libarchopenfpga/src/read_xml_config_protocol.cpp` | 把 XML 的 `<configuration_protocol>` 解析成 `ConfigProtocol` 对象 |
| `libs/libarchopenfpga/src/openfpga_arch_linker.cpp` | 把 `circuit_model_name`（字符串）链接成真正的 `CircuitModelId` |
| `openfpga/src/utils/check_config_protocol.cpp` | 校验协议自身一致性（如编程时钟与区域数） |
| `openfpga/src/utils/circuit_library_utils.cpp` | 校验存储电路模型的端口是否符合协议要求 |
| `openfpga/src/base/openfpga_read_arch_template.h` | `read_openfpga_arch` 命令模板，在读取后调用上面的校验 |
| `openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml` | 示例：`scan_chain` 协议 |
| `openfpga_flow/openfpga_arch/k4_N4_40nm_bank_openfpga.xml` | 示例：`memory_bank` 协议 |
| `openfpga_flow/openfpga_arch/k4_N4_40nm_frame_openfpga.xml` 等 | 其余协议示例 |

> 数据流向：XML →（解析）→ `ConfigProtocol`（存名字）→（链接）→ 填上真正的 `CircuitModelId` →（校验）→ 冻结为只读的 `openfpga::Arch`。

## 4. 核心概念与源码讲解

### 4.1 配置协议要解决什么：面积、引脚、配置时间的三角权衡

#### 4.1.1 概念说明

不同配置协议本质上是在三个目标之间做取舍：

- **芯片面积**：地址译码电路、移位寄存器链本身都占面积。
- **封装引脚数**：把配置接口引到芯片外面要占引脚，引脚很贵。
- **配置时间**：上电后把比特流写完需要多少个时钟周期，直接影响「FPGA 能多快开始干活」。

没有哪种协议是绝对最优的，所以要按场景选。下面用直觉对比三种典型思路：

- **串行链（scan_chain）**：所有配置位串成一条（或几条）移位寄存器链。引脚极少（数据 + 时钟 + 复位），但 N 位要 N 拍。
- **存储体（memory_bank）**：配置位排成 BL/WL 矩阵，靠地址译码选中。引脚数和速度都比较折中，是商用 FPGA 最常见的做法之一。
- **帧寻址（frame_based）**：把配置位按「帧（frame）」分组，给一个地址选一帧，一次写一帧。介于链式和矩阵之间。

#### 4.1.2 核心流程

不论哪种协议，配置过程在抽象层面都可以写成：

```
准备：比特流（一串 0/1）+ 配置协议（决定写法）
循环：
  1. 用协议规定的寻址方式，定位到下一个（或下一组）配置位
  2. 把比特流对应位的值写进存储电路
  3. 推进地址 / 移位链
直到所有配置位写完
```

差别只在「寻址方式」和「一次写几位」。举个量化的直觉（仅用于理解，不是精确公式）：

- 设芯片共有 \(N\) 个配置位。
- 串行链：配置时间约 \(T_{\text{chain}} \approx N \cdot T_{\text{clk}}\)，引脚数 \(O(1)\)。
- \(\sqrt{N}\times\sqrt{N}\) 的存储体 + 译码：BL/WL 地址共需约 \(2\sqrt{N}\) 根内部线，但外部引脚只要 \(O(\log_2 \sqrt{N})\) 根地址线，配置时间约 \(O(\sqrt{N})\) 量级（按字写）。

这就是为什么大阵列几乎不会用纯串行链，而小阵列或测试用例用串行链就足够简单。

#### 4.1.3 源码精读

协议「类型」在源码里是一个枚举，它就是上面几种思路的直接对应：

[libs/libarchopenfpga/src/circuit_types.h:130-153](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_types.h#L130-L153) 定义了 `e_config_protocol_type` 枚举和它对应的字符串表。注释（第 131–139 行）逐项说明了每种类型「可编程存储器如何被组织与访问」，枚举值（第 140–148 行）依次是 `CONFIG_MEM_STANDALONE`、`CONFIG_MEM_SCAN_CHAIN`、`CONFIG_MEM_MEMORY_BANK`、`CONFIG_MEM_QL_MEMORY_BANK`、`CONFIG_MEM_FRAME_BASED`、`CONFIG_MEM_FEEDTHROUGH`，字符串表（第 150–153 行）给出 XML 里 `type=` 能写的取值：`"standalone"`、`"scan_chain"`、`"memory_bank"`、`"ql_memory_bank"`、`"frame_based"`、`"feedthrough"`。

注意第 137–138 行的注释特别说明 `feedthrough`「目前仅供内部使用」，普通 arch 文件里不会出现，本讲也不展开。

> 这个枚举是本讲后续一切的「根」：XML 里写的字符串、数据结构里存的类型、校验时的分支判断，全都围绕它。

#### 4.1.4 代码实践

1. **实践目标**：建立「协议类型 ↔ 物理直觉」的直觉映射。
2. **操作步骤**：
   - 打开 [libs/libarchopenfpga/src/circuit_types.h:130-153](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/circuit_types.h#L130-L153)。
   - 对着枚举的 6 个值，按下表填写你自己的判断（先填，再对答案）：

     | 枚举值 | XML 字符串 | 引脚数（多/少） | 配置速度（快/慢） |
     | --- | --- | --- | --- |
     | `CONFIG_MEM_STANDALONE` | ? | ? | ? |
     | `CONFIG_MEM_SCAN_CHAIN` | ? | ? | ? |
     | ... | | | |

3. **需要观察的现象**：你会发现枚举顺序与字符串表顺序完全一致，这是 OpenFPGA 里常见的「枚举 ↔ 字符串表」一一对应约定（u3-l3 见过同样的写法）。
4. **预期结果**：能说出 `scan_chain` 引脚少但慢、`memory_bank`/`frame_based` 引脚与速度较折中、`standalone` 每位独立（适合极小规模测试）。
5. 结论部分属于设计直觉判断，**待本地结合实际比特流长度验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么真实的大规模 FPGA 几乎不用纯 `scan_chain`？
**参考答案**：纯 scan_chain 配置时间与配置位数成线性关系 \(O(N)\)，对百万级配置位的芯片，上电配置耗时太长；且单链一旦断开整条失效。大阵列更倾向 memory_bank/frame_based 这类可并行、可按字/按帧写入的协议。

**练习 2**：`feedthrough` 为什么标注「仅供内部使用」？
**参考答案**：它不是面向用户的配置方式，而是 OpenFPGA 内部用于特殊布线/直连场景的占位类型，普通用户架构不应选用；从字符串表可见它虽可被解析，但没有对应的公开 arch 示例。

---

### 4.2 ConfigProtocol 数据结构与五种配置协议类型

#### 4.2.1 概念说明

XML 里的 `<configuration_protocol>` 被解析后，落成一个 C++ 对象 `ConfigProtocol`。它是 `openfpga::Arch` 的一个成员，在架构读取完成后被冻结为只读。

`ConfigProtocol` 要同时承载「所有协议都有的公共信息」和「只在某种协议下才有意义的信息」：

- **公共信息**：协议类型 `type`、配置存储电路模型 `memory_model`、可配置区域数 `num_regions`。
- **仅 scan_chain 用**：编程时钟（programming clock）相关信息。
- **仅 memory_bank 类用**：BL/WL 子协议类型、各自的存储模型、bank 数量。

设计上，它把这些都放进同一个类，但在访问器和修改器里用 `if (type_ == ...)` 做约束——不该用的协议下调用相关接口会打日志报错。这是一种典型的「宽存储 + 窄校验」做法。

#### 4.2.2 核心流程

`ConfigProtocol` 在生命周期里经历的三个阶段：

```
阶段 1：解析（read_xml_config_protocol）
  - 从 XML 读 type、circuit_model_name（此时只是字符串）、num_regions
  - scan_chain：额外读 <programming_clock>
  - ql_memory_bank：额外读 <bl>/<wl> 子节点
  → 得到一个「存了名字、还没链接」的对象

阶段 2：链接（openfpga_arch_linker）
  - 用 circuit_library 把 memory_model_name 字符串翻译成真正的 CircuitModelId
  → 对象现在同时持有「名字」和「ID」

阶段 3：校验（check_config_protocol / check_configurable_memory_circuit_model）
  - 检查协议自身一致性（如 scan_chain 的编程时钟数 ≤ 区域数）
  - 检查存储电路模型的端口是否符合该协议要求（如 scan_chain 要 CCFF 端口）
  → 全部通过，才冻结为只读 Arch
```

#### 4.2.3 源码精读

先看 `ConfigProtocol` 的公共接口。它采用「const 访问器（只读）+ mutable 修改器（可写）」两套接口，这一点和 u2-l3 讲过的访问器约定一致。

[libs/libarchopenfpga/src/config_protocol.h:29-56](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/config_protocol.h#L29-L56) 是公共访问器。第 30–33 行是所有协议都要用的四件套：`type()`（协议类型）、`memory_model_name()`（存储模型名字，字符串）、`memory_model()`（存储模型，已链接的 `CircuitModelId`）、`num_regions()`（配置区域数）。第 35–47 行是 scan_chain 专用的编程时钟访问器；第 49–56 行是 memory_bank 类专用的 BL/WL 访问器。

再看内部数据成员，注意它们的默认值和适用条件注释：

[libs/libarchopenfpga/src/config_protocol.h:101-143](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/config_protocol.h#L101-L143) 是内部数据。第 106 行 `type_` 是协议类型；第 109–110 行 `memory_model_name_` / `memory_model_` 同时存了「名字」和「ID」（对应阶段 1 和阶段 2）；第 113 行 `num_regions_` 是区域数。第 121–140 行是 BL/WL 相关数据，**注意第 133、137 行**：`bl_protocol_type_` 和 `wl_protocol_type_` 的默认值都是 `BLWL_PROTOCOL_DECODER`——也就是说，memory_bank 类协议「不写子协议时默认用 decoder」。第 142–143 行是 `ql_memory_bank` 专用的额外设置。

修改器里的「窄校验」体现在这里：

[libs/libarchopenfpga/src/config_protocol.cpp:182-190](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/config_protocol.cpp#L182-L190) 的 `set_bl_protocol_type()`：第 183–188 行检查，只有当整体协议类型是 `CONFIG_MEM_QL_MEMORY_BANK` 时才允许设置 BL 协议类型，否则打 `VTR_LOG_ERROR` 并直接 `return`（不修改数据）。同文件 `set_bl_memory_model_name()`（第 192–201 行）则进一步约束「只有 shift_register 子协议才需要 BL 存储模型」。这就是「宽存储 + 窄校验」的具体实现：数据字段都在，但写操作按当前协议类型和子协议类型把关。

#### 4.2.4 代码实践

1. **实践目标**：在源码层面确认「哪些字段是公共的、哪些是某种协议专属」。
2. **操作步骤**：
   - 打开 [libs/libarchopenfpga/src/config_protocol.h:101-143](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/config_protocol.h#L101-L143)。
   - 把每个内部数据成员按下表分类：

     | 成员 | 公共 | 仅 scan_chain | 仅 memory_bank 类 |
     | --- | --- | --- | --- |
     | `type_` | ✓ | | |
     | `memory_model_name_` / `memory_model_` | ✓ | | |
     | `num_regions_` | ✓ | | |
     | `prog_clk_port_` 等 | | ✓ | |
     | `bl_protocol_type_` 等 | | | ✓ |
3. **需要观察的现象**：注释明确写出每个字段「only applicable to ...」，与上表一致。
4. **预期结果**：能指着源码说出 `num_regions_` 是公共字段（所有协议都能分多区域），而 BL/WL 字段只在 memory_bank 类下有意义。
5. 字段归属属于静态事实，可直接由源码确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `memory_model_name_` 和 `memory_model_` 要同时存在？
**参考答案**：解析阶段（阶段 1）XML 里只能拿到字符串名字，此时 `circuit_library` 还没参与，无法得到 `CircuitModelId`；链接阶段（阶段 2）才用名字去查到 ID 并填入 `memory_model_`。两者并存正是 u3-l2 强调的「解析存名字、链接查 ID」两段式设计的体现。

**练习 2**：把 `bl_protocol_type_` 的默认值设成 `BLWL_PROTOCOL_DECODER` 有什么好处？
**参考答案**：这样 `memory_bank`/`ql_memory_bank` 的 arch 即使不在 XML 里写 `<bl>`/`<wl>` 子节点，也能得到一个合理、可用的默认子协议（decoder 译码），减少用户必填项，向后兼容老 arch 文件。

---

### 4.3 BL/WL 子协议：memory_bank 的三种访问方式

#### 4.3.1 概念说明

memory_bank 类协议（`memory_bank` 和 `ql_memory_bank`）都把配置位排成 BL/WL 矩阵。但「BL 和 WL 这两组线本身怎么被驱动」还有三种选择，这就是 **BL/WL 子协议**：

- **flatten（扁平）**：每一根 BL、每一根 WL 都直接引到顶层，没有任何译码。引脚最多，但控制最直接；适合规模很小、或需要对每一根线单独建模的场景。
- **decoder（译码器）**：用一组地址线经译码器选中某根 WL/BL，大幅减少外部地址引脚数。这是最常见的折中方案，也是默认值。
- **shift_register（移位寄存器）**：BL、WL 各自由一条（或几条）移位寄存器链驱动，往链里移位来选通某根线。引脚很少，但要「移位→锁定」两步操作。这种方案下，移位寄存器本身也是配置存储电路，因此需要额外指定一个 CCFF 电路模型来搭这条链。

> 注意：`memory_bank`（普通存储体）在 XML 里**不写** `<bl>`/`<wl>` 子节点，固定走内部译码；只有 `ql_memory_bank`（QuickLogic 存储体，更灵活的版本）才允许用 `<bl>`/`<wl>` 子节点显式选择子协议。这是两种 memory_bank 的关键区别。

#### 4.3.2 核心流程

BL/WL 子协议在 XML 里的样子（以 `ql_memory_bank` 为例）：

```xml
<organization type="ql_memory_bank" circuit_model_name="SRAM">
  <bl protocol="shift_register" circuit_model_name="BL_DFFRQ"/>
  <wl protocol="shift_register" circuit_model_name="WL_DFFRQ"/>
</organization>
```

解析时：

1. 读到 `type="ql_memory_bank"` → 进入 BL/WL 子协议解析分支。
2. 遇到 `<bl protocol="..."/>` → 设置 BL 子协议类型；若为 `shift_register`，还要读它的 `circuit_model_name`（搭移位链用的 CCFF 模型）和可选的 `num_banks`。
3. 对 `<wl>` 做同样处理。
4. 校验 BL 与 WL 子协议类型必须相同（源码里目前不支持两者不同）。

对应的枚举和字符串：

[libs/libarchopenfpga/src/config_protocol.h:13-20](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/config_protocol.h#L13-L20) 定义了 `e_blwl_protocol_type`（第 13–18 行，三个值 `FLATTEN`/`DECODER`/`SHIFT_REGISTER`）与字符串表（第 19–20 行 `"flatten"`/`"decoder"`/`"shift_register"`）。

#### 4.3.3 源码精读

XML 解析的分支逻辑在 `read_xml_config_organization`：

[libs/libarchopenfpga/src/read_xml_config_protocol.cpp:146-250](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_config_protocol.cpp#L146-L250)。重点看三处：

- 第 150–161 行：读 `type` 属性并用 `string_to_config_protocol_type`（见第 25–34 行）翻译成枚举；非法字符串会抛 `archfpga_throw`。
- 第 164–165 行：读 `circuit_model_name`，此刻只存字符串。
- 第 167–179 行：读可选的 `num_regions`，默认 1，且必须 ≥1。
- 第 228–249 行：**只有 `CONFIG_MEM_QL_MEMORY_BANK` 才解析 `<bl>`/`<wl>` 子节点**；第 243–248 行还强制要求 BL 与 WL 子协议类型必须相同，否则抛错。

`<bl>`/`<wl>` 子节点的具体解析：

[libs/libarchopenfpga/src/read_xml_config_protocol.cpp:76-106](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_config_protocol.cpp#L76-L106) 的 `read_xml_bl_protocol`：第 80–84 行读 `protocol` 属性并翻译成 `e_blwl_protocol_type`；第 97–105 行**仅当子协议是 `shift_register` 时**，才读 `circuit_model_name`（搭移位链的 CCFF 模型）和可选 `num_banks`（默认 1，见第 101–104 行）。`read_xml_wl_protocol`（第 111–141 行）对 WL 做完全对称的事。

把这套规则对照真实 arch，可以一眼看出区别：

- `flatten` 版 [openfpga_flow/openfpga_arch/k4_N4_40nm_qlbankflatten_openfpga.xml:171-176](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_qlbankflatten_openfpga.xml#L171-L176)：`<bl protocol="flatten"/>`、`<wl protocol="flatten"/>`，子节点里**没有** `circuit_model_name`（flatten 不需要搭移位链）。
- `shift_register` 版 [openfpga_flow/openfpga_arch/k4_N4_40nm_qlbanksr_openfpga.xml:194-199](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_qlbanksr_openfpga.xml#L194-L199)：`<bl protocol="shift_register" circuit_model_name="BL_DFFRQ"/>`、`<wl protocol="shift_register" circuit_model_name="WL_DFFRQ"/>`，**各自指定了**一个 CCFF 模型来搭移位链。
- `decoder`（默认）版 [openfpga_flow/openfpga_arch/k4_N4_40nm_qlbank_openfpga.xml:171-173](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_qlbank_openfpga.xml#L171-L173)：连 `<bl>`/`<wl>` 子节点都没有，走默认 decoder。

#### 4.3.4 代码实践

1. **实践目标**：通过对比 arch 文件，亲眼看清三种 BL/WL 子协议在 XML 上的差异。
2. **操作步骤**：
   - 同时打开上面三个文件（`qlbank_openfpga.xml`、`qlbankflatten_openfpga.xml`、`qlbanksr_openfpga.xml`）的 `<configuration_protocol>` 段。
   - 用 `Grep` 搜索 `protocol=`：

     ```
     Grep pattern: protocol=
     path: openfpga_flow/openfpga_arch/k4_N4_40nm_qlbank*_openfpga.xml
     ```
3. **需要观察的现象**：
   - `qlbank`（decoder）里搜不到 `protocol=`（因为没写子节点）。
   - `qlbankflatten` 里是 `protocol="flatten"` 且无 `circuit_model_name`。
   - `qlbanksr` 里是 `protocol="shift_register"` 且带 `circuit_model_name="BL_DFFRQ"`/`"WL_DFFRQ"`。
4. **预期结果**：三类 arch 的命名后缀（`qlbank`/`qlbankflatten`/`qlbanksr`）与子协议一一对应，能据此反推 XML 内容。
5. 源码静态事实，可直接确认；移位链 `BL_DFFRQ`/`WL_DFFRQ` 模型本身的端口结构属于 u3-l3 范畴，**待结合该讲确认**。

#### 4.3.5 小练习与答案

**练习 1**：为什么只有 `shift_register` 子协议才需要 `circuit_model_name`，而 `flatten`/`decoder` 不需要？
**参考答案**：`flatten` 把每根线直接引出，`decoder` 用译码器选通，二者都不需要额外的存储电路来「产生」BL/WL 信号；而 `shift_register` 要用一串 CCFF 触发器搭成移位链来逐位选通 BL/WL，这条链本身就是配置存储电路，所以必须指定一个 CCFF 模型。

**练习 2**：如果 XML 里 BL 写 `flatten`、WL 写 `shift_register`，会发生什么？
**参考答案**：在 [read_xml_config_protocol.cpp:243-248](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_config_protocol.cpp#L243-L248) 会因 BL/WL 协议类型不同而抛 `archfpga_throw`，提示「Expect same type of protocol for both BL and WL!」。

**练习 3**：普通 `memory_bank`（不是 `ql_memory_bank`）能否写 `<bl>` 子节点来选 flatten？
**参考答案**：不能。XML 解析的 BL/WL 分支只在 `CONFIG_MEM_QL_MEMORY_BANK` 下触发（见第 228 行的 `if`），普通 `memory_bank` 即便写了 `<bl>` 也不会被读入，固定走默认 decoder。

---

### 4.4 type 与 circuit_model_name：解析、链接与校验的完整链路

#### 4.4.1 概念说明

`<organization>` 节点上有两个最重要的属性：`type`（协议类型）和 `circuit_model_name`（配置存储电路模型的名字）。本节把这两个属性从「XML 字符串」到「可用、已校验的 C++ 对象」的完整路径讲清楚。这是 u3-l2 提到的「两段式设计」在配置协议上的具体落地。

关键点：不同协议对存储电路模型的**端口要求不同**——

- `scan_chain` 需要一个 **CCFF**（配置触发器）模型，端口形如 `D`/`Q`/`QN`/`prog_clk`。
- `memory_bank`/`ql_memory_bank`/`frame_based`/`standalone` 需要一个 **SRAM**（或锁存器）模型，端口带 `bl`/`wl`/`out` 等。

这就是为什么每个 arch 的 `circuit_library` 里要配对应的存储模型：cc arch 里是 `DFF`（ccff 型），bank arch 里是 `SRAM`（sram 型），frame arch 里是 `LATCH`。校验阶段会按协议类型检查这些端口是否齐全。

#### 4.4.2 核心流程

```
① read_openfpga_arch 命令
   └─ read_xml_config_protocol()        解析 XML → ConfigProtocol（存名字）
   └─ link_config_protocol_to_circuit_library()  名字 → CircuitModelId
   └─ check_config_protocol()           校验协议一致性 + 存储模型端口
       └─ 全部通过 → 写入 OpenfpgaContext.arch().config_protocol（只读）
       └─ 有错 → read_openfpga_arch 返回 CMD_EXEC_FATAL_ERROR
```

#### 4.4.3 源码精读

**第一步：解析**。[libs/libarchopenfpga/src/read_xml_config_protocol.cpp:281-303](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/read_xml_config_protocol.cpp#L281-L303) 是入口 `read_xml_config_protocol`：第 286–287 行找到 `<configuration_protocol>` 子节点，第 289–291 行找到 `<organization>` 并交给 `read_xml_config_organization`（4.3 节已读）。第 294–300 行还说明：只有 `ql_memory_bank` 且 BL/WL 都为 `flatten` 时，才额外读 `<ql_memory_bank_config_setting>`。

**第二步：链接**。[libs/libarchopenfpga/src/openfpga_arch_linker.cpp:14-56](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/openfpga_arch_linker.cpp#L14-L56) 的 `link_config_protocol_to_circuit_library`：第 15–16 行用 `memory_model_name()` 字符串调用 `circuit_lib.model(...)` 查 `CircuitModelId`；查不到（第 19–24 行，`CircuitModelId::INVALID()`）就 `VTR_LOG` 报错并 `exit(1)`；查到则在第 26 行 `set_memory_model()` 写回真正的 ID。第 29–55 行对 BL/WL 的移位链模型做同样链接（仅在名字非空时，即 shift_register 子协议）。

**第三步：校验入口**。校验在 `read_openfpga_arch` 命令模板里被调用：

[openfpga/src/base/openfpga_read_arch_template.h:59-61](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_read_arch_template.h#L59-L61) 调用 `check_config_protocol(openfpga_context.arch().config_protocol, openfpga_context.arch().circuit_lib)`，返回 `false` 即返回 `CMD_EXEC_FATAL_ERROR`，整个 `read_openfpga_arch` 命令失败。

[openfpga/src/utils/check_config_protocol.cpp:82-99](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/utils/check_config_protocol.cpp#L82-L99) 的 `check_config_protocol` 汇总三类检查：第 86–88 行调 `config_protocol.validate()`（协议自身一致性，主要查 scan_chain 编程时钟）；第 90–92 行调 `check_configurable_memory_circuit_model`（存储模型端口是否符合协议）；第 94–95 行调 `check_config_protocol_programming_clock`（编程时钟必须是 CCFF 模型上的全局、时钟、编程端口）。

`validate()` 的具体内容：

[libs/libarchopenfpga/src/config_protocol.cpp:333-339](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libarchopenfpga/src/config_protocol.cpp#L333-L339)：目前只在 `CONFIG_MEM_SCAN_CHAIN` 时调用 `validate_ccff_prog_clocks()`（第 271–328 行），它检查编程时钟数 ≤ 区域数、每个区域恰好被一个编程时钟驱动、无重叠。这对应多链 scan_chain（`num_regions > 1`）场景。

最后是最关键的「按协议类型检查存储模型端口」：

[openfpga/src/utils/circuit_library_utils.cpp:309-364](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/utils/circuit_library_utils.cpp#L309-L364) 的 `check_configurable_memory_circuit_model` 用一个 `switch` 按协议分流：第 315–318 行 `CONFIG_MEM_SCAN_CHAIN` → 检查 CCFF 端口（`check_ccff_circuit_model_ports`）；第 319–346 行 `CONFIG_MEM_QL_MEMORY_BANK` → 检查 SRAM 端口，并在 shift_register 子协议下额外检查 BL/WL 移位链 CCFF 模型端口；第 347–353 行 `CONFIG_MEM_STANDALONE`/`CONFIG_MEM_MEMORY_BANK`/`CONFIG_MEM_FRAME_BASED` → 检查 SRAM 端口（`check_sram_circuit_model_ports`）。这正是「不同协议要求不同存储模型」的代码体现。

把这个链路对照真实 arch：

- cc（scan_chain）：[k4_N4_40nm_cc_openfpga.xml:162-164](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L162-L164) 用 `circuit_model_name="DFF"`，而该 arch 的 `DFF` 模型是 ccff 型、带 `prog_clk` 端口（见 [同文件:143-151](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L143-L151)），能通过 CCFF 端口检查。
- bank（memory_bank）：[k4_N4_40nm_bank_openfpga.xml:171-173](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_bank_openfpga.xml#L171-L173) 用 `circuit_model_name="SRAM"`，该 `SRAM` 模型是 sram 型、带 `bl`/`wl` 端口（见 [同文件:152-160](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_bank_openfpga.xml#L152-L160)），能通过 SRAM 端口检查。

#### 4.4.4 代码实践（本讲主实践）

1. **实践目标**：对比 cc（scan_chain）与 bank（memory_bank）两个 arch 的 `configuration_protocol` 段，写出组织类型差异，并说明各自使用的配置存储电路模型及其端口特征。
2. **操作步骤**：
   - 打开 [k4_N4_40nm_cc_openfpga.xml:162-164](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L162-L164) 与 [k4_N4_40nm_bank_openfpga.xml:171-173](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_bank_openfpga.xml#L171-L173)。
   - 在各自的 `circuit_library` 里找到被引用的模型：cc 引用 `DFF`，bank 引用 `SRAM`。
   - 记录两个模型的 `type` 与端口列表。
3. **需要观察的现象**：
   - cc：`type="scan_chain"`，存储模型 `DFF` 的 `type="ccff"`，端口含 `D`/`Q`/`QN`/`prog_clk`（`prog_clk` 是 `is_global` + `is_prog` 的时钟端口）。
   - bank：`type="memory_bank"`，存储模型 `SRAM` 的 `type="sram"`，端口含 `bl`/`wl`/`out`/`outb`，**没有** `prog_clk`。
4. **预期结果**：能填出下表——

   | 项 | cc（scan_chain） | bank（memory_bank） |
   | --- | --- | --- |
   | organization type | `scan_chain` | `memory_bank` |
   | circuit_model_name | `DFF` | `SRAM` |
   | 存储模型 type | `ccff` | `sram` |
   | 关键端口 | `D`/`Q`/`QN`/`prog_clk` | `bl`/`wl`/`out` |
   | BL/WL 子节点 | 无（不适用） | 无（走默认 decoder） |
5. 如要进一步确认校验真的生效，可在本地故意把 cc arch 的 `circuit_model_name` 改成一个 sram 型模型，运行 `read_openfpga_arch`，观察 CCFF 端口检查报错——**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：如果在 cc arch 里把 `circuit_model_name="DFF"` 改成 `circuit_model_name="SRAM"`，会在哪一步报错？
**参考答案**：链接阶段（`openfpga_arch_linker.cpp`）能成功（`SRAM` 模型确实存在），但校验阶段 `check_configurable_memory_circuit_model` 会因 `CONFIG_MEM_SCAN_CHAIN` 走 CCFF 端口检查分支（`check_ccff_circuit_model_ports`），而 `SRAM` 模型没有 `D`/`QN`/`prog_clk` 等端口，检查失败，`read_openfpga_arch` 返回致命错误。

**练习 2**：`memory_model_name` 解析后存的是字符串，那校验阶段用的是字符串还是 ID？
**参考答案**：用的是 ID。链接阶段（`link_config_protocol_to_circuit_library`）已把字符串翻译成 `CircuitModelId` 并写回 `memory_model_`，校验阶段 `check_configurable_memory_circuit_model` 第 312 行 `config_protocol.memory_model()` 取的就是这个 ID，再用它去查端口。

**练习 3**：`num_regions` 在 scan_chain 校验里起什么作用？
**参考答案**：`num_regions` 表示把配置链分成几条独立链（多链 scan_chain）。`validate_ccff_prog_clocks()` 用它做边界检查：编程时钟数不能超过区域数，每个区域（每条链）必须恰好被一个编程时钟驱动，且不能重叠。

---

## 5. 综合实践

把本讲三个模块串起来的小任务：**为同一份 k4_N4 器件，编排一张「配置协议选择决策表」**。

1. **目标**：综合运用「协议类型」「存储模型类型」「BL/WL 子协议」「arch 命名后缀」四类知识，建立一张可查询的对照表。
2. **操作步骤**：
   - 用 `Grep` 在 `openfpga_flow/openfpga_arch/` 下搜索所有 `<organization type=`，收集每个 arch 的协议类型与存储模型名：

     ```
     Grep pattern: <organization type=
     glob: openfpga_flow/openfpga_arch/*.xml
     output_mode: content
     ```
   - 对每条结果，按文件名后缀归类（`_cc_`、`_bank_`、`_frame_`、`_qlbank_`、`_qlbankflatten_`、`_qlbanksr_`、`_standalone_`）。
   - 对 `qlbank*` 这一组，进一步搜索其 `<bl`/`<wl>` 子节点，确认子协议。
   - 整理成下表（请自行补全）：

     | 文件名后缀 | 协议 type | 存储模型类型 | BL/WL 子协议 |
     | --- | --- | --- | --- |
     | `_cc_` | `scan_chain` | ccff | — |
     | `_bank_` | `memory_bank` | sram | decoder（默认） |
     | `_frame_` | ? | ? | — |
     | `_qlbank_` | ? | ? | ? |
     | `_qlbankflatten_` | ? | ? | ? |
     | `_qlbanksr_` | ? | ? | ? |
     | `_standalone_` | ? | ? | — |
3. **需要观察的现象**：文件名后缀与协议类型/子协议高度相关，OpenFPGA 用命名约定承载了协议选择。
4. **预期结果**：拿到一个陌生的 arch 文件名（如 `k4_N4_40nm_qlbanksr_wlr_openfpga.xml`），能仅凭后缀推断出它用的是 `ql_memory_bank` + `shift_register` 子协议，且带 `wlr`（一种 WL 端口变体）。
5. 表格内容为源码静态事实，可由 Grep 直接确认；`wlr` 端口变体的具体语义涉及 u3-l3，**待结合该讲理解**。

## 6. 本讲小结

- **配置协议回答「怎么把比特流灌进 FPGA」**：它和「配置位用什么电路存」是两件事，前者由 `configuration_protocol` 决定，后者由 `circuit_library` 决定。
- **五种协议类型** `standalone`/`scan_chain`/`memory_bank`/`ql_memory_bank`/`frame_based`（外加内部用的 `feedthrough`）定义在 `circuit_types.h` 的 `e_config_protocol_type` 枚举里，是面积、引脚数、配置时间三角权衡的不同选择。
- **`ConfigProtocol` 类**用「宽存储 + 窄校验」：公共字段（`type`/`memory_model`/`num_regions`）与协议专属字段（scan_chain 的编程时钟、memory_bank 类的 BL/WL）共存，修改器按当前类型把关。
- **BL/WL 子协议**有 `flatten`/`decoder`/`shift_register` 三种，只有 `ql_memory_bank` 能在 XML 里用 `<bl>`/`<wl>` 子节点显式选择，默认是 `decoder`；`shift_register` 还需指定搭移位链用的 CCFF 模型。
- **完整链路是「解析存名字 → 链接查 ID → 校验端口」三段式**：`read_xml_config_protocol` 解析，`link_config_protocol_to_circuit_library` 链接，`check_config_protocol`（在 `read_openfpga_arch` 模板里调用）按协议类型校验存储模型端口是否齐全。
- **协议类型与存储模型类型绑定**：`scan_chain` 要 ccff 模型，`memory_bank`/`ql_memory_bank`/`frame_based`/`standalone` 要 sram 类模型；arch 文件名后缀（`cc`/`bank`/`frame`/`qlbank*`/`standalone`）是协议选择的速记。

## 7. 下一步学习建议

- **纵向——比特流如何按协议组织**：本讲只讲到「协议如何描述」，u7-l1（两级比特流模型）和 u7-l3（build_fabric_bitstream 与协议相关组织）会讲这些协议如何影响最终比特流的排布，尤其是 memory_bank 下的 BL/WL 地址生成。
- **纵向——顶层模块如何按协议连线**：u6-l4（顶层模块与存储器配置总线）讲 `build_top_module` 如何根据 config_protocol 把所有配置存储器连成 chain/frame/memory_bank 总线，是本讲协议在 fabric 构建阶段的落地。
- **进阶——多链与移位寄存器 bank**：u9-l1（存储器组与移位寄存器 Bank）深入 `ql_memory_bank` + `shift_register` 下的 `MemoryBankShiftRegisterBanks` 数据结构，是本讲 4.3 节的高级延伸。
- **横向——回到电路库**：如果想搞清 ccff/sram 模型端口为何如此规定，回到 u3-l3（电路库 circuit_library）读 `CIRCUIT_MODEL_PORT_TYPE_STRING` 与各端口语义标志（`is_prog`/`is_global`/`mode_select`）。
