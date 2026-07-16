# 两套架构文件：VPR arch 与 openfpga_arch

## 1. 本讲目标

OpenFPGA 在「读架构」这一步要吃进**两份 XML**：一份给 VPR，一份给自己。这两份文件长得完全不一样、根标签不同、描述的东西也不同，初学者很容易把它们搞混。

学完本讲，你应该能够：

1. 说出为什么 OpenFPGA 需要**两套**架构文件，而不是一套。
2. 区分 **VPR 架构 XML**（描述器件**结构**）与 **openfpga_arch.xml**（描述电路级**物理实现**）的职责边界。
3. 在 `task.conf` 中认出 `arch0` 与 `openfpga_arch_file` 这两个配置项，并指出它们分别指向哪一种文件。
4. 解读架构文件的命名约定（如 `k4`、`N4`、`cc`、`tileable`、`40nm`）。
5. 理解两套文件是靠「**按名字绑定**」联系在一起的——这是后续 u3-l2、u3-l3、u3-l4 的共同地基。

本讲是 u3 单元（架构描述与输入文件）的**总览入口**：它只搭框架、讲清两份文件各管什么；电路库（circuit_library）的细节留给 u3-l3，配置协议（configuration_protocol）的细节留给 u3-l4。

## 2. 前置知识

- **FPGA 是可编程的**：芯片造好后，它的逻辑功能和连线由「配置存储器（config memory）」里的比特决定。所以描述一个 FPGA，既要描述它的**结构**（有哪些逻辑块、怎么布线），也要描述它的**物理实现**（这些逻辑块和布线开关具体用哪些晶体管电路搭出来）。
- **VPR 与 OpenFPGA 的分工**（来自 u1-l1）：VPR 负责「综合后的网表 → 布局布线」，它只关心**结构层面的抽象**（一个多路选择器就是一个有电阻电容延迟的黑盒）；OpenFPGA 负责把这套结构展开成真正的 **fabric Verilog 网表与比特流**，它必须知道每个黑盒**底层是什么电路**。
- **OpenfpgaContext 与 `arch_`**（来自 u2-l3）：命令之间通过全局数据中枢 `OpenfpgaContext` 交换数据，其中架构信息存在 `arch_` 分区。本讲就是打开这个 `arch_` 黑盒的第一步——搞清楚它的**输入来源**。
- **task.conf 与笛卡尔积**（来自 u1-l4）：一个任务由「架构 × 基准 × 脚本参数」组合而成，本讲的 `arch0` 正是其中「架构」这一维。

## 3. 本讲源码地图

本讲涉及三个关键文件：

| 文件 | 角色 | 顶层标签 |
| --- | --- | --- |
| `openfpga_flow/vpr_arch/k4_N4_tileable_40nm.xml` | **VPR 架构 XML**：描述器件结构 | `<architecture>` |
| `openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml` | **openfpga_arch.xml**：描述电路级物理实现 | `<openfpga_architecture>` |
| `openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf` | 任务配置：把两套架构文件**分别**登记进来 | INI 段落 |

> 注意根标签的细微差别：VPR 的是 `<architecture>`（单数），OpenFPGA 的是 `<openfpga_architecture>`。光看根标签就能立刻判断手里这份 XML 属于哪一套。

## 4. 核心概念与源码讲解

### 4.1 为什么需要两套架构文件

#### 4.1.1 概念说明

一个最自然的疑问是：既然都是描述「同一个 FPGA」，为什么不合并成一个大文件？

原因是 **VPR 和 OpenFPGA 关心的抽象层级不同**：

- **VPR 只需要「结构 + 延迟/面积」**。它在布局布线时，把一个多路选择器当成一个带 `R`（电阻）、`Cin`/`Cout`（电容）、`Tdel`（延迟）的黑盒即可，根本不关心这个 mux 是用传输门（transmission gate）还是标准单元搭的。这部分信息放在 **VPR 架构 XML** 里。
- **OpenFPGA 需要「电路级物理实现」**。它要生成可综合的 fabric Verilog，就必须知道：这个 mux 用什么 pass-gate 电路、输入/输出加什么 buffer、配置位用什么触发器存、配置存储器怎么组织（串行链？矩阵寻址？）。这部分信息放在 **openfpga_arch.xml** 里。

两套文件**解耦**的好处，在 `openfpga_arch/README.md` 里写得明明白白：

> Note that an OpenFPGA architecture can be applied to multiple VPR architecture files.

也就是说，**一份 `openfpga_arch.xml` 可以配对多份不同的 VPR 架构 XML**（只要器件结构对得上）。如果把两者焊死在一起，这种复用就不可能了。

#### 4.1.2 核心流程：两套文件如何被「分别」喂进流程

下面用伪流程描述从任务配置到架构加载的链路：

```
task.conf
  ├── [ARCHITECTURES] arch0      ──►  VPR 架构 XML  ──►  vpr 命令（布局布线）
  │                                    （器件结构）
  └── [OpenFPGA_SHELL]
       └── openfpga_arch_file    ──►  openfpga_arch.xml ──► read_openfpga_arch 命令
                                        （电路级实现）          ──► 写入 context.arch_
```

关键点：**两个配置项指向两类不同的文件**。`arch0` 喂给 VPR，`openfpga_arch_file` 喂给 OpenFPGA 自己的 `read_openfpga_arch` 命令（该命令在 u5-l2 精读）。

#### 4.1.3 源码精读：task.conf 里的两个登记项

先看 `[ARCHITECTURES]` 段——`arch0` 指向 **VPR 架构 XML**：

[openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf:L25-L26](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf#L25-L26)

```ini
[ARCHITECTURES]
arch0=${PATH:OPENFPGA_PATH}/openfpga_flow/vpr_arch/k4_N4_tileable_40nm.xml
```

这里的 `arch0` 可以有 `arch1`、`arch2`……多个架构，它们会与基准（benchmarks）做笛卡尔积，每个组合跑一个 job（详见 u1-l4）。

再看 `[OpenFPGA_SHELL]` 段——`openfpga_arch_file` 指向 **openfpga_arch.xml**：

[openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf:L18-L21](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf#L18-L21)

```ini
[OpenFPGA_SHELL]
openfpga_shell_template=${PATH:OPENFPGA_PATH}/openfpga_flow/openfpga_shell_scripts/write_full_testbench_example_script.openfpga
openfpga_arch_file=${PATH:OPENFPGA_PATH}/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml
openfpga_sim_setting_file=${PATH:OPENFPGA_PATH}/openfpga_flow/openfpga_simulation_settings/auto_sim_openfpga.xml
```

注意：`openfpga_arch_file` 是给 `.openfpga` 脚本模板里 `read_openfpga_arch` 命令做**变量替换**用的（替换机制见 u1-l4 提到的 `safe_substitute`），它和 `arch0` 走的是两条不同的路径。

最后看一眼 `[GENERAL]` 段确认流程类型：

[openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf:L9-L16](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf#L9-L16)

`fpga_flow=yosys_vpr` 表示「Yosys 综合 + VPR 布局布线」，这正是需要 VPR 架构 XML 的原因。

#### 4.1.4 代码实践：核对配置项与文件的对应关系

1. **实践目标**：亲手确认两个配置项确实指向两类不同文件。
2. **操作步骤**：
   - 打开 `openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf`。
   - 找到 `arch0`，记下它指向的路径里的目录名（应该是 `vpr_arch`）。
   - 找到 `openfpga_arch_file`，记下它指向的路径里的目录名（应该是 `openfpga_arch`）。
3. **需要观察的现象**：两个路径分别落在 `vpr_arch/` 与 `openfpga_arch/` 两个不同目录下，文件名也不同（一个没有 `_openfpga` 后缀，一个有）。
4. **预期结果**：你能不假思索地说出「`arch0` → VPR 架构，`openfpga_arch_file` → OpenFPGA 架构」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `task.conf` 里的 `openfpga_arch_file` 指向了一个 `vpr_arch/` 目录下的文件，会发生什么？

> **参考答案**：`.openfpga` 脚本里的 `read_openfpga_arch` 命令会尝试用 OpenFPGA 的 XML 解析器去读它。因为 VPR 架构 XML 的根标签是 `<architecture>` 而不是 `<openfpga_architecture>`，解析会失败或报「找不到预期节点」的错误。这正好印证了两套文件格式互不通用。

**练习 2**：为什么 `openfpga_arch/README.md` 要强调「一份 OpenFPGA 架构能配多个 VPR 架构」？

> **参考答案**：因为电路级实现（用哪些 mux、buffer、配置协议）与器件结构（LUT 多大、几个逻辑单元、怎么布线）是正交的两件事。同一套电路库可以挂在不同尺寸/布线结构的 VPR 架构上，分开成两份文件才能复用。

---

### 4.2 VPR 架构 XML：器件结构描述

#### 4.2.1 概念说明

VPR 架构 XML 回答的是「**这个 FPGA 长什么样**」——一个纯结构问题：

- 芯片是几乘几的网格？边界放 IO，中间填什么？
- 每个逻辑块（CLB）里有几个基本逻辑单元（BLE）？每个 BLE 是几个输入的 LUT？
- 布线用什么线段（segment）？多长？单向还是双向？
- 开关盒（switch block）用什么拓扑？连接块（connection block）怎么连？
- 各个开关/线段的**电阻、电容、延迟**是多少？（用于时序分析）

它**不回答**「这个 mux 用什么晶体管电路实现」——那是 openfpga_arch 的事。

#### 4.2.2 核心流程：VPR 架构 XML 的七大段

VPR 架构 XML 的根是 `<architecture>`，下面大致分七段：

```
<architecture>
  ├── <models>           ① 综合前端（ODIN II）认识的网表块模型
  ├── <tiles>            ② 物理瓦片：引脚、引脚位置、fc
  ├── <layout>           ③ 二维网格布局：tileable、边界 IO、填充 CLB
  ├── <device>           ④ 晶体管尺寸、开关盒拓扑、连接块、沟道宽度
  ├── <switchlist>       ⑤ 布线用的开关（mux/ipin_cblock）及其 R/C/Tdel
  ├── <segmentlist>      ⑥ 布线线段（L4 等）
  └── <complexblocklist> ⑦ 逻辑块层级（io / clb → fle → ble4 → lut4/ff）
```

其中 ②③④⑤⑥ 是**布线与器件级**描述，⑦ 是**逻辑块内部层级**（pb_type 树）描述。本讲只讲结构骨架，pb_type 树的语义在 u4-l3（pb_type 注解）深入。

#### 4.2.3 源码精读

**根标签 `<architecture>`**：

[openfpga_flow/vpr_arch/k4_N4_tileable_40nm.xml:L17](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/vpr_arch/k4_N4_tileable_40nm.xml#L17)

文件头注释已经把关键参数交代清楚了（K=4, N=4，L=4，40nm）：

[openfpga_flow/vpr_arch/k4_N4_tileable_40nm.xml:L2-L8](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/vpr_arch/k4_N4_tileable_40nm.xml#L2-L8)

**③ layout——网格布局，关键字 `tileable="true"` 就藏在这里**：

[openfpga_flow/vpr_arch/k4_N4_tileable_40nm.xml:L70-L77](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/vpr_arch/k4_N4_tileable_40nm.xml#L70-L77)

`<perimeter type="io">` 表示四周放 IO，`<fill type="clb">` 表示中间填 CLB。`tileable="true"` 是文件名里 `tileable` 的来源——它意味着布线结构是「可铺砖」的（阵列里大量位置结构相同，便于压缩，详见 u9-l5 的 GSB 压缩）。

**④ device——开关盒拓扑 `wilton`、`fs=3`**：

[openfpga_flow/vpr_arch/k4_N4_tileable_40nm.xml:L139-L140](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/vpr_arch/k4_N4_tileable_40nm.xml#L139-L140)

`switch_block type="wilton" fs="3"` 表示用 Wilton 拓扑、每个进入开关盒的线段能连到 3 个方向。`connection_block input_switch_name="ipin_cblock"` 指定连接块用名为 `ipin_cblock` 的开关——**这个名字会在 openfpga_arch 里被绑定到具体电路**（见 4.3）。

**⑤ switchlist——开关的「抽象延迟模型」**：

[openfpga_flow/vpr_arch/k4_N4_tileable_40nm.xml:L156-L158](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/vpr_arch/k4_N4_tileable_40nm.xml#L156-L158)

这里有两个开关：`name="0"`（布线 mux）和 `name="ipin_cblock"`（连接块输入 mux）。注意它们只给了 `R`、`Cin`、`Cout`、`Tdel` 这些**电气参数**，完全没有说底层电路。这两个名字 `0` 与 `ipin_cblock` 就是后续与 openfpga_arch 绑定的「钥匙」。

**⑥ segmentlist——线段 L4**：

[openfpga_flow/vpr_arch/k4_N4_tileable_40nm.xml:L164-L168](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/vpr_arch/k4_N4_tileable_40nm.xml#L164-L168)

线段名 `L4`、长度 4、单向（`unidir`），它也只是一个带金属电阻/电容的抽象线段，名字 `L4` 同样是绑定钥匙。

**⑦ complexblocklist——逻辑块 pb_type 树**：

[openfpga_flow/vpr_arch/k4_N4_tileable_40nm.xml:L238-L257](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/vpr_arch/k4_N4_tileable_40nm.xml#L238-L257)

`clb`（10 输入 4 输出）里有 4 个 `fle`，每个 `fle` 在 `n1_lut4` 模式下含一个 `ble4`，`ble4` 里又含 `lut4`（4 输入 LUT）和 `ff`（触发器）。这条路径 `clb.fle[n1_lut4].ble4.lut4` 正是 openfpga_arch 里 pb_type 注解要绑定的目标路径。

> 小结：VPR 架构 XML 通篇在讲「**结构 + 电气参数**」，它给出大量**有名字的部件**（开关 `0`、`ipin_cblock`、线段 `L4`、pb_type 路径），却从不说明这些部件的电路实现。这些名字就是交给 openfpga_arch 的「接口契约」。

#### 4.2.4 代码实践：数一数 fc 与沟道宽度的关系

1. **实践目标**：用一个简单公式理解 VPR 架构里的 `fc`（连接块扇出比例）参数。
2. **操作步骤**：
   - 在 `k4_N4_tileable_40nm.xml` 的 `<tiles>` 段找到 `fc in_val="0.15" out_val="0.10"`（[L46](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/vpr_arch/k4_N4_tileable_40nm.xml#L46) 附近）。
   - 结合 `task.conf` 里 `bench0_chan_width = 300`（沟道宽度 W=300）。
3. **需要观察的现象 / 计算**：`fc_in = 0.15` 表示每个输入引脚连接到沟道里 15% 的线道。可连接线道数为
   \[ n_{pins} = \lceil f_c \cdot W \rceil = \lceil 0.15 \times 300 \rceil = 45 \]
4. **预期结果**：你能用「结构 + 比例」的语言解释 `fc` 的含义，并意识到这是 VPR 关心的布线连通性问题，与电路实现无关。

#### 4.2.5 小练习与答案

**练习 1**：文件名里的 `tileable` 由 XML 里的哪个属性决定？

> **参考答案**：由 `<layout tileable="true" ...>` 决定（L70）。它表示布线结构可铺砖，便于 OpenFPGA 做唯一块压缩。

**练习 2**：`<switchlist>` 里的开关 `0` 有 `R`、`Cin`、`Tdel`，但没有「用什么电路」的信息。这对谁够用、对谁不够用？

> **参考答案**：对 VPR 够用（布局布线与时序分析只需要延迟/电容模型）；对 OpenFPGA 不够用（生成 fabric Verilog 必须知道底层电路，这部分由 openfpga_arch 通过 `name="0"` 绑定补上）。

---

### 4.3 openfpga_arch.xml：电路级物理实现

#### 4.3.1 概念说明

如果 VPR 架构 XML 是「**结构蓝图**」，那么 openfpga_arch.xml 就是「**施工详图**」。它回答「**这些结构用什么电路搭出来**」：

- 用什么工艺（technology_library：PTM 45nm 模型、vdd、pmos/nmos 尺寸）？
- 有哪些可用电路模型（circuit_library：反相器、传输门、mux、LUT、触发器、IO pad）？各自的端口、buffer、设计技术是什么？
- 配置存储器怎么组织（configuration_protocol：串行链 scan_chain？矩阵 memory_bank？帧 frame_based？）？
- **把 VPR 架构里那些「有名字的部件」绑定到具体电路模型**（connection_block / switch_block / routing_segment / pb_type_annotations）。

最后一条是本讲的核心枢纽：openfpga_arch **不重新定义结构**，而是**引用 VPR 已经定义好的结构名字**，给每个名字配上电路实现。

#### 4.3.2 核心流程：openfpga_arch.xml 的六大段

```
<openfpga_architecture>
  ├── <technology_library>      ① 工艺：晶体管器件模型（45nm）
  ├── <circuit_library>          ② 电路模型库（mux/lut/ff/ccff/iopad/inv_buf/...）
  ├── <configuration_protocol>   ③ 配置协议（scan_chain / memory_bank / frame_based / standalone）
  ├── <connection_block>         ④ 绑定：VPR 连接块开关  ──► 电路模型
  ├── <switch_block>             ⑤ 绑定：VPR 开关盒开关  ──► 电路模型
  ├── <routing_segment>          ⑥ 绑定：VPR 线段        ──► 电路模型
  └── <pb_type_annotations>      ⑦ 绑定：VPR pb_type 路径 ──► 电路模型 + mode_bits
```

①②③ 是「**自带的物理资产**」，④⑤⑥⑦ 是「**与 VPR 架构的绑定契约**」。本讲重点是绑定的存在与原理；②③ 的细节分别在 u3-l3、u3-l4 展开。

#### 4.3.3 源码精读

**根标签 `<openfpga_architecture>`**（注意与 VPR 的 `<architecture>` 区分）：

[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:L10](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L10)

**③ configuration_protocol——文件名 `cc` 的来源**：

[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:L162-L164](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L162-L164)

```xml
<configuration_protocol>
  <organization type="scan_chain" circuit_model_name="DFF"/>
</configuration_protocol>
```

`type="scan_chain"` 就是「配置链（configuration chain）」，缩写 **`cc`**；它用名为 `DFF` 的电路模型（一个扫描链 D 触发器）做配置存储。对比一下其他协议（已在源码中核对）：

- `k4_N4_40nm_bank_openfpga.xml` 里是 `<organization type="memory_bank" circuit_model_name="SRAM"/>`（缩写 `bank`，矩阵寻址）。
- `k4_N4_40nm_frame_openfpga.xml` 里是 `<organization type="frame_based" circuit_model_name="LATCH"/>`（缩写 `frame`，帧寻址）。

协议类型的细节（含 BL/WL 子协议）在 u3-l4 详讲。

**④⑤⑥ 绑定段——按名字把 VPR 部件接到电路上**：

[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:L165-L173](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L165-L173)

把这段与 4.2 里 VPR 架构的部件名一一对照，就能看清「绑定」机制：

| openfpga_arch 绑定项 | 引用的 VPR 部件名（来自 4.2） | 绑定到的电路模型 |
| --- | --- | --- |
| `<switch_block><switch name="0" ...>` | VPR switchlist 的 `name="0"`（L156） | `mux_tree_tapbuf` |
| `<connection_block><switch name="ipin_cblock" ...>` | VPR switchlist 的 `name="ipin_cblock"`（L158） | `mux_tree_tapbuf` |
| `<routing_segment><segment name="L4" ...>` | VPR segmentlist 的 `name="L4"`（L164） | `chan_segment` |

**名字必须严格一致**——这就是两套文件的「接口契约」。如果 VPR 架构里把开关改名为 `mux0`，而 openfpga_arch 里仍写 `name="0"`，绑定就会失败。

**⑦ pb_type_annotations——绑定逻辑块层级**：

[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:L187-L188](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L187-L188)

```xml
<pb_type name="clb.fle[n1_lut4].ble4.lut4" circuit_model_name="lut4"/>
<pb_type name="clb.fle[n1_lut4].ble4.ff" circuit_model_name="DFFSRQ"/>
```

`name="clb.fle[n1_lut4].ble4.lut4"` 这条路径正好对应 4.2 里 VPR 架构的 pb_type 树（L238–L297），它被绑定到 circuit_library 里的 `lut4` 电路模型（[L131](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L131)）。于是 VPR 抽象的「4 输入 LUT」就有了具体的传输门 + buffer 电路实现。

**② circuit_library 一瞥——`mux_tree_tapbuf` 引用了谁**（说明电路模型之间也存在引用，详情见 u3-l3）：

[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:L111-L119](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L111-L119)

这个 mux 的输入 buffer 用 `INVTX1`、输出 buffer 用 `tap_buf4`、pass-gate 用 `TGATE`——全是 circuit_library 里**其他**电路模型的名字。所以电路模型自身也构成一张引用图（u3-l3 精读）。

> 小结：openfpga_arch.xml 做两件事——**定义物理资产**（工艺、电路库、配置协议），以及**用名字把这些资产绑定到 VPR 架构的部件上**。两套文件靠「同名绑定」拧成一股，共同完整描述一个 FPGA。

#### 4.3.4 代码实践：跟踪一条「开关 → 电路模型」绑定链

1. **实践目标**：亲手走通从 VPR 部件名到电路模型的完整绑定。
2. **操作步骤**：
   - 在 VPR 架构 `k4_N4_tileable_40nm.xml` 的 switchlist 找到 `name="0"` 的开关（L156）。
   - 在 openfpga_arch `k4_N4_40nm_cc_openfpga.xml` 的 `<switch_block>` 找到同名 `name="0"`（L169），记下它的 `circuit_model_name="mux_tree_tapbuf"`。
   - 回到 circuit_library 找到 `mux_tree_tapbuf`（L111），看它的输入/输出 buffer 与 pass-gate 分别引用了哪几个电路模型。
3. **需要观察的现象**：你会得到一条链 `VPR 开关 "0"` → `mux_tree_tapbuf` → `{INVTX1, tap_buf4, TGATE}`。
4. **预期结果**：你能用自己的话讲清「VPR 眼里一个有 R/C/Tdel 的抽象 mux，在 OpenFPGA 眼里是一棵用 INVTX1/tap_buf4/TGATE 搭的树形 mux」。

#### 4.3.5 小练习与答案

**练习 1**：openfpga_arch 里 `<switch_block><switch name="0" .../>` 的 `name="0"` 是从哪来的？

> **参考答案**：必须与 VPR 架构 `<switchlist>` 里的 `<switch name="0">`（L156）同名。这是绑定契约，名字不一致则绑定失败。

**练习 2**：把 `cc` 版 openfpga_arch 换成 `bank` 版，需要改 openfpga_arch 的哪一段？VPR 架构要不要改？

> **参考答案**：只需改 openfpga_arch 的 `<configuration_protocol>` 段（从 `scan_chain` 改成 `memory_bank`，并换对应的配置存储电路模型）。VPR 架构**通常不用改**，因为配置协议属于电路级实现，与器件结构无关——这正是两套文件解耦的价值。

---

### 4.4 架构文件命名约定

#### 4.4.1 概念说明

`openfpga_flow/` 下有上百个架构文件，靠一套**命名约定**来「望文生义」。这套约定分别写在两个目录的 README 里：

- `openfpga_flow/vpr_arch/README.md`：VPR 架构文件的命名约定。
- `openfpga_flow/openfpga_arch/README.md`：openfpga_arch 文件的命名约定。

两者共享一部分前缀（如 `k4`、`N4`、`40nm`），但各自有独有字段——这本身就反映了两套文件关注点不同。

#### 4.4.2 核心流程：拆解一个文件名

以本讲两份文件为例，逐段拆解：

```
VPR 架构:   k4 _ N4 _ tileable _ 40nm .xml
OpenFPGA:   k4 _ N4 _ 40nm _ cc _ openfpga .xml
```

| 字段 | 含义 | 出现在 | 来源（README） |
| --- | --- | --- | --- |
| `k4` | LUT 输入数 K=4（最大 LUT 输入为 4） | 两套都有 | 「k<lut_size>」 |
| `N4` | 每个 CLB 含 4 个逻辑单元（BLE） | 两套都有 | 「N<le_size>」 |
| `40nm` | 工艺节点（延迟参数抽取自 40nm） | 两套都有 | 「<feature_size>」 |
| `tileable` | 布线结构可铺砖（VPR 侧的布线属性） | **仅 VPR** | 「tileable<IO\|ConcatWire>」 |
| `cc` | 配置协议 = configuration chain（scan_chain） | **仅 OpenFPGA** | 「<bank\|cc\|frame\|standalone>」 |
| `_openfpga` | 标识这是 openfpga_arch 文件 | **仅 OpenFPGA** | 文件名后缀约定 |

几个要点：

- **`cc` 是 OpenFPGA 侧的专属字段**。配置协议只在 openfpga_arch 里定义，所以 VPR 架构文件名里永远不会出现 `cc`/`bank`/`frame`。
- **`tileable` 是 VPR 侧的专属字段**。它是 `<layout>` 的属性，所以 openfpga_arch 文件名里不会出现它。
- **`cc` 可带后缀**：README 指出，当配置链由多于 1 个时钟控制时，会加 `<int>clk` 后缀（如 `cc2clk`）。可在 `openfpga_arch/` 目录里找到诸如 `k4_N4_40nm_GlobalTileClk_cc_openfpga.xml` 这类带时钟网络前缀的变体。
- **配置协议四个取值**：`bank`（memory_bank，矩阵寻址）、`cc`（configuration chain，串行）、`frame`（frame_based，帧寻址）、`standalone`（vanilla，直连不可编程）。这与 4.3.3 在源码里核对的结果一致。

#### 4.4.3 源码精读：命名约定的权威出处

VPR 架构命名约定（节选）：

[openfpga_flow/vpr_arch/README.md:L4-L8](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/vpr_arch/README.md#L4-L8)

这里明确 `k<lut_size>` 与 `N<le_size>` 的含义。`tileable` 的定义在同文件：

[openfpga_flow/vpr_arch/README.md:L8-L10](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/vpr_arch/README.md#L8-L10)

OpenFPGA 架构命名约定里，配置协议字段（`cc`/`bank`/`frame`/`standalone`）的定义：

[openfpga_flow/openfpga_arch/README.md:L19-L23](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/README.md#L19-L23)

而那句关键的「一份 OpenFPGA 架构可配多个 VPR 架构」：

[openfpga_flow/openfpga_arch/README.md:L3-L4](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_arch/README.md#L3-L4)

#### 4.4.4 代码实践：用命名约定反向猜文件内容

1. **实践目标**：不打开文件，仅凭文件名推断它的关键特性。
2. **操作步骤**：在 `openfpga_flow/openfpga_arch/` 目录下，挑三个文件名（例如 `k4_N4_40nm_bank_openfpga.xml`、`k4_N4_40nm_frame_openfpga.xml`、`k4_N4_40nm_cc_openfpga.xml`），仅凭命名约定写出它们的配置协议类型。
3. **需要观察的现象**：你的推断应该分别是 memory_bank、frame_based、scan_chain。
4. **验证**：用 `grep` 看每个文件 `<configuration_protocol>` 的 `organization type=`（见 4.3.3 已核对的结果），确认与推断一致。

> 说明：本实践为「源码阅读型实践」，不需要编译运行；若要在本机执行 grep，请在仓库根目录运行，命令为示例代码（非项目原有命令）：
> ```bash
> grep -n "organization" openfpga_flow/openfpga_arch/k4_N4_40nm_bank_openfpga.xml \
>   openfpga_flow/openfpga_arch/k4_N4_40nm_frame_openfpga.xml
> ```

#### 4.4.5 小练习与答案

**练习 1**：看到一个名为 `k6_frac_N10_40nm_bank_openfpga.xml` 的文件，你能推断出什么？

> **参考答案**：`k6`=6 输入 LUT（最大输入），`frac`=可分裂 LUT，`N10`=每 CLB 10 个逻辑单元，`40nm`=工艺节点，`bank`=memory_bank 配置协议，`_openfpga`=这是 openfpga_arch 文件。

**练习 2**：为什么 `k4_N4_40nm_cc_openfpga.xml` 这个文件名里**没有** `tileable`？

> **参考答案**：因为 `tileable` 是 VPR 架构 `<layout>` 的属性，属于器件结构；而 openfpga_arch 关心的是电路级实现，不关心布线是否可铺砖。`tileable` 只会出现在 VPR 架构文件名（如 `k4_N4_tileable_40nm.xml`）里。

---

## 5. 综合实践

**任务：把两套文件「拼」成一张完整的 FPGA 描述图。**

回到本讲开头的两个主角文件：

- VPR 架构：`openfpga_flow/vpr_arch/k4_N4_tileable_40nm.xml`
- OpenFPGA 架构：`openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml`

请完成下面四件事，把本讲的知识串起来：

1. **顶层标签对比**：列出两者的根标签（`<architecture>` vs `<openfpga_architecture>`），并用一句话概括「VPR arch 描述了什么、openfpga_arch 补充描述了什么」。
2. **跟踪三条绑定链**：任选 VPR 架构里的开关 `0`、线段 `L4`、pb_type 路径 `clb.fle[n1_lut4].ble4.lut4`，分别在 openfpga_arch 里找到对应的绑定项，写出每条链的「VPR 部件 → 电路模型」。
3. **解读文件名**：解释 `k4`、`N4`、`40nm`、`tileable`、`cc` 各自的含义，并指出哪些字段是 VPR 侧专属、哪些是 OpenFPGA 侧专属。
4. **协议替换**：假设你想把这套设计从配置链（cc）改成存储器矩阵（bank），回答：要改 `task.conf` 的哪个配置项？要换成哪个 openfpga_arch 文件？VPR 架构文件要不要换？为什么？

完成后再回头看 u2-l3 的 `OpenfpgaContext`——你应该能理解，`read_openfpga_arch` 命令读进来的正是上面这份 openfpga_arch.xml，它最终落进 context 的 `arch_` 分区，供 `build_fabric` 等下游命令使用。

## 6. 本讲小结

- OpenFPGA 需要**两套架构文件**：VPR 架构 XML 描述**器件结构**（布局、布线、逻辑块层级、电气参数），openfpga_arch.xml 描述**电路级物理实现**（工艺、电路库、配置协议）。
- 在 `task.conf` 里它们被**分别登记**：`[ARCHITECTURES]` 段的 `arch0` 指向 VPR 架构，`[OpenFPGA_SHELL]` 段的 `openfpga_arch_file` 指向 openfpga_arch。
- 两套文件靠「**同名绑定**」拧在一起：openfpga_arch 的 `connection_block`/`switch_block`/`routing_segment`/`pb_type_annotations` 用名字引用 VPR 架构里的开关、线段、pb_type，再配上具体电路模型。
- 根标签一眼可辨：`<architecture>` 是 VPR 的，`<openfpga_architecture>` 是 OpenFPGA 的。
- 命名约定里，`k4`/`N4`/`40nm` 两套共享；`tileable` 是 VPR 侧专属（布线属性），`cc`/`bank`/`frame`/`standalone` 是 OpenFPGA 侧专属（配置协议）。
- 解耦的价值：换配置协议通常只改 openfpga_arch，不动 VPR 架构；一份 openfpga_arch 可配多种 VPR 架构。

## 7. 下一步学习建议

本讲只搭了「两套文件各管什么」的骨架，接下来按依赖顺序深入：

- **u3-l2 openfpga_arch.xml 总体结构**：逐段精读 `<openfpga_architecture>` 的所有顶层子节点，本讲的六大段会在那里展开。
- **u3-l3 电路库 circuit_library 与电路模型**：深入 4.3 提到的 `mux_tree_tapbuf → INVTX1/tap_buf4/TGATE` 引用图。
- **u3-l4 配置协议 configuration_protocol**：深入 `scan_chain` / `memory_bank` / `frame_based` 的差异与 BL/WL 子协议。
- **u4-l3 pb_type 注解**：深入 4.3.3 提到的 `clb.fle[n1_lut4].ble4.lut4` 绑定语法与 `mode_bits`。
- **u5-l2 openfpga_arch XML 解析**：看 `read_openfpga_arch` 命令如何把这份 XML 解析成内存中的 `Arch` 对象（即 context 的 `arch_`）。
