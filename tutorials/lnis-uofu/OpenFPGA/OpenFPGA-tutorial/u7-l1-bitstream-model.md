# 两级比特流模型：device 与 fabric

## 1. 本讲目标

经过上一单元（u6）我们已经能在内存里搭出一整张 FPGA fabric 的模块图（`ModuleManager`），并且在顶层模块上挂好了配置总线（configuration bus）与配置区域（config region）。但「fabric 长什么样」和「怎么把每个配置位（config bit）写成 0/1 串烧进芯片」是两件事。本讲就专讲后者的**数据模型**。

读完本讲，你应当能够：

- 区分 OpenFPGA 内存中的两级比特流：与协议无关的 `BitstreamManager`（device 级）与与协议相关的 `FabricBitstream`（fabric 级）。
- 解释 device 级比特流如何用「配置块树」镜像模块实例层级，以及配置位如何附着在叶子块上。
- 解释 fabric 级比特流为什么必须携带协议相关的寻址信息（scan_chain 的链顺序、memory_bank 的 BL/WL 地址、frame_based 的层级地址）。
- 复述 device→fabric 转换的核心思想：**只读地重组**，靠 `block_name == instance_name` 这条链把两级模型对接，并据此说明「为什么不直接一步生成最终比特流」。

本讲是 u7（比特流生成）的开篇，只讲**数据模型与两级抽象**；具体的 grid/routing 位解码细节留给 u7-l2，写出文件格式与 fast_configuration 留给 u7-l4。

## 2. 前置知识

本讲默认你已经学过：

- **配置协议（configuration protocol）**（u3-l4）：scan_chain（串行链）、memory_bank（BL/WL 矩阵寻址）、frame_based（帧寻址）、ql_memory_bank 等类型，它们决定「用怎样的拓扑和时序把比特流灌进芯片」。
- **ModuleManager**（u6-l1）：模块、端口、实例、网四要素，以及**可配置子模块（configurable children）**的概念。
- **顶层模块与配置总线**（u6-l4）：`build_top_module` 如何把所有可配置子模块排成一张有序列表、切成若干配置区域、并按配置协议连接配置总线。本讲的「memory index」「BL/WL」「配置区域」全部来自这一步的产物。
- **OpenfpgaContext**（u2-l3）：命令间数据的全局中枢，`bitstream_manager_` 与 `fabric_bitstream_` 是其中两个分区。
- **强类型 ID 与 SoA**（u6-l1、u3-l3）：`vtr::StrongId<Tag>` 让不同类型的 ID 在编译期不可互赋值；SoA（struct of arrays）即「每条属性一条 vector，按下标对齐」。

几个本讲反复用到的术语，先统一口径：

| 术语 | 含义 |
|---|---|
| 配置位（config bit） | 一个可编程存储单元要写的值，0 或 1（外加 `x` 表示 don't-care） |
| 配置块（config block） | `BitstreamManager` 里的层级节点，对应模块图里**某个实例**（instance） |
| device 级 / fabric 级 | 前者与配置协议无关，后者与具体协议绑定 |
| BL / WL | Bit Line / Word Line，memory_bank 协议下定位一个存储单元的「列地址 / 行地址」 |
| 配置区域（config region） | 顶层模块上可并行配置的一组存储单元（u6-l4 引入） |

## 3. 本讲源码地图

本讲涉及 4 个关键文件，外加 2 个「命令编排」佐证文件：

| 文件 | 作用 |
|---|---|
| [libs/libfpgabitstream/src/bitstream_manager.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libfpgabitstream/src/bitstream_manager.h) | **device 级**比特流的数据结构定义：配置块树 + 配置位，与协议无关 |
| [libs/libfpgabitstream/src/bitstream_manager_fwd.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libfpgabitstream/src/bitstream_manager_fwd.h) | `ConfigBlockId` / `ConfigBitId` 两个强类型 ID 的声明 |
| [openfpga/src/fpga_bitstream/fabric_bitstream.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/fabric_bitstream.h) | **fabric 级**比特流的数据结构定义：线性序列 + 协议相关寻址 + memory bank 紧凑表示 |
| [openfpga/src/fpga_bitstream/fabric_bitstream_fwd.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/fabric_bitstream_fwd.h) | `FabricBitId` / `FabricBitRegionId` 强类型 ID |
| [openfpga/src/fpga_bitstream/build_device_bitstream.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_device_bitstream.cpp) | device 级比特流构建器：遍历 grid/routing 解码出配置位 |
| [openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp) | device→fabric 转换器：按协议把配置块树重组为带地址的线性序列 |
| openfpga/src/base/openfpga_bitstream_template.h | 命令编排模板，佐证 `build_architecture_bitstream` → `build_fabric_bitstream` 的两级调用 |

记一句话：**device 级回答「每个配置位应该是什么值、属于哪个实例」，fabric 级回答「按什么顺序、用什么地址把这些值送进芯片」**。

## 4. 核心概念与源码讲解

### 4.1 BitstreamManager：device 级、与协议无关的配置块树

#### 4.1.1 概念说明

`BitstreamManager` 是 OpenFPGA 比特流的「第一级」。它的设计目标在文件头注释里写得非常清楚：创建一个**统一**的数据结构，存下所有配置位，并标注每个位属于 fabric 的哪个模块。它刻意做成**与配置协议无关**——也就是说，它既不关心你用 scan_chain 还是 memory_bank，也不存任何 BL/WL 地址。它只关心两件事：

1. **配置块树（block tree）**：一棵层级树，每个节点叫一个配置块（`ConfigBlockId`），对应模块图里**一个具体的实例**。
2. **配置位（config bit）**：每个叶子块上挂着若干个 0/1 位（`ConfigBitId`）。

关键约束（同样写在文件头注释里）：每个块**只有一个父块、可以有多个子块**；每个位**只属于一个块**。

为什么要这样组织？因为 device 级比特流的语义来源是**逻辑**：LUT 的真值表要解码成一组配置位、布线多路选择器（routing mux）选中的输入要解码成一个选择位。这些位的「值」和「归属哪个实例」是协议无关的——同一份设计换一种配置协议，这些位的值不变，变的只是它们**最终被排成什么顺序、用什么地址送进去**。所以把这些协议无关的信息单独存成一级。

#### 4.1.2 核心流程

device 级比特流由 `build_device_bitstream()` 构建，主线流程：

1. 创建顶层块（名字 = 顶层模块名，如 `fpga_top`）；若存在 `fpga_core` wrapper，则再建一层 core 块作为后续遍历的真正顶层。
2. 递归预估块数与位数（`rec_estimate_device_bitstream_num_blocks` / `..._num_bits`），一次性 `reserve` 内存，避免反复扩容。
3. 调 `build_grid_bitstream()`：遍历布局结果里的每个逻辑块（CLB/IO），解码 LUT 真值表与 IO 方向位。
4. 调 `build_routing_bitstream()`：遍历布线结果，为每个布线 mux 决定选择位。
5. 全程把新块/新位挂进 `BitstreamManager`，块名严格等于对应实例的实例名（`instance_name`）。

数据结构上，`BitstreamManager` 用 SoA + 强类型 ID（`ConfigBlockId` / `ConfigBitId`）组织，核心成员可分三组：

```
块树（每个 ConfigBlockId 一条）
  block_names_          块名 == 模块实例名（对接 fabric 的钥匙）
  parent_block_ids_     父块
  child_block_ids_      子块列表
  block_name2ids_       按名字反查块（device→fabric 转换时大量使用）

配置位（每个 ConfigBitId 一条）
  bit_values_           0 或 1
  bit_parent_blocks_    所属块

附加标注（routing mux / net 用，可选）
  block_path_ids_       mux 选中的输入编号（-1=未用，>=0=已用）
  block_input_net_ids_  输入 net
  block_output_net_ids_ 输出 net
```

> 注意文件头的一句关键提示：这里的块图是 `ModuleGraph` 的**展平图**（flattened graph）——模块图里同一个模块可以被实例化很多次，而比特流里每个实例都是**唯一**的块。这正是它需要镜像「实例树」而非「模块树」的原因。

#### 4.1.3 源码精读

**设计意图与对接钥匙**。文件头注释把整个两级模型的设计哲学讲透了，特别是那张 `BitstreamManager ←→ ModuleManager` 的对接图和「block_name == instance_name」的硬约束——这是本讲最重要的两段：

[bitstream_manager.h:1-36](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libfpgabitstream/src/bitstream_manager.h#L1-L36) —— 说明 `BitstreamManager` 既能组织成 fabric-dependent（适配协议的序列）也能 fabric-independent（XML），以及与 `ModuleManager` 靠「块名 == 实例名」对接。

**强类型 ID**。两个 ID 都是 `vtr::StrongId<Tag>`，标签不同则编译期不可互赋值，避免把 `ConfigBitId` 当 `ConfigBlockId` 用：

[bitstream_manager_fwd.h:14-19](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libfpgabitstream/src/bitstream_manager_fwd.h#L14-L19) —— 定义 `ConfigBlockId` 与 `ConfigBitId`。

**内部数据（块树 + 位 + 标注）**。下面这组 `vtr::vector` 就是上面流程图的三组数据落地，注意 `block_name2ids_` 这个按名反查表，它是 device→fabric 转换的性能关键：

[bitstream_manager.h:226-274](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libfpgabitstream/src/bitstream_manager.h#L226-L274) —— 块树成员（`block_names_`、`parent_block_ids_`、`child_block_ids_`、`block_name2ids_`）、位的成员（`bit_values_`、`bit_parent_blocks_`）以及 mux/net 标注。

**device 级构建入口**。`build_device_bitstream()` 创建顶层块、做内存预留、再分别调 grid 与 routing 的位解码器。注意 162-169 行用模块名作块名，确立了对接钥匙；217-238 行的两步解码才是位的真正来源（细节留 u7-l2）：

[build_device_bitstream.cpp:150-247](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_device_bitstream.cpp#L150-L247) —— `build_device_bitstream()` 全函数。

函数头上方的一段注释点明了 device 级的本质——它绑定到模块图、却**不绑定协议**，因此既能服务于 fabric 也能输出通用比特流：

[build_device_bitstream.cpp:137-149](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_device_bitstream.cpp#L137-L149) —— 注释：「create a bitstream which is binding to the module graphs … But it can be used to output a generic bitstream」。

**先预估再 reserve**。递归遍历可配置子模块数块、数位，一次性预留内存，函数末尾还断言「实际块数/位数 == 预留数」，防止统计与构建不一致：

[build_device_bitstream.cpp:30-59](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_device_bitstream.cpp#L30-L59) —— `rec_estimate_device_bitstream_num_blocks`：没有可配置子模块的叶子节点（存储器本身）不计数，其余逐级累加。

#### 4.1.4 代码实践

**实践目标**：用一条命令把 device 级比特流导出成 XML，亲眼看到「配置块树 + 配置位」长什么样。

**操作步骤**（假设你已按 u1-l3 编出 `openfpga` 并按 u4-l1 跑过 `build_fabric`；下面用最小命令链，只走到 device 比特流这一步）：

1. 用 `run-task` 跑通 `basic_tests/full_testbench/configuration_chain`（u1-l4 已介绍），或直接用 `example_script.openfega` 跑到 `build_architecture_bitstream`。
2. 定位到生成的 `fabric_independent_bitstream.xml`（device 级，由 `build_architecture_bitstream --write_file` 触发，见下方模板佐证）。
3. 在文件里找到一个 LUT 块和一个布线 mux 块，观察它们的嵌套层级。

**需要观察的现象**：

- XML 的根标签是顶层模块名，下面层层嵌套子块，**嵌套路径正好对应模块实例树**（如 `fpga_top/grid_io_*/*/...`）。
- 只有叶子块里才出现 `<bit id="..." value="0|1"/>`；非叶子块只做分组，不带位。
- 文件里**看不到任何 BL/WL 地址或链顺序**——证明 device 级确实与协议无关。

**预期结果**：你能画出一条从顶层块到某个 LUT 配置位的路径，并确认它与该实例在 `ModuleManager` 中的实例名路径一致。

**佐证（命令编排）**：`build_architecture_bitstream` 命令在模板里调用 `build_device_bitstream()` 写入 `mutable_bitstream_manager()`，并用 `--write_file` 调 `write_xml_architecture_bitstream` 导出 XML：

[openfpga_bitstream_template.h:35-90](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_template.h#L35-L90) —— `fpga_bitstream_template`：调 `build_device_bitstream` 写 `mutable_bitstream_manager()`，`--write_file` 导出 device 级 XML。

> 若你无法本地运行，可标注「待本地验证」，转而阅读 [build_device_bitstream.cpp:217-238](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_device_bitstream.cpp#L217-L238) 推断产物结构。

#### 4.1.5 小练习与答案

**练习 1**：`BitstreamManager` 为什么要存 `block_name2ids_`（按名反查表），而不只存 `block_names_`？
**答案**：因为 device→fabric 转换时，fabric 构建器是按 `ModuleManager` 的实例名去 `find_child_block` 反查块的（见 4.3）。没有按名反查表就只能线性扫描，规模一大就不可接受。

**练习 2**：注释说块图是 `ModuleGraph` 的「展平图」，展平体现在哪里？
**答案**：模块图里一个模块类型可被实例化多次（共享定义），而比特流里每个实例都是独立的块，存各自的配置位。所以块的数量 = 实例数，而不是模块类型数。

---

### 4.2 FabricBitstream：fabric 级、与协议相关的线性序列

#### 4.2.1 概念说明

`FabricBitstream` 是第二级。它存的是**配置位被送进芯片时的实际序列**，外加协议要求的各种**寻址信息**。文件头注释说得很直白：存下 architecture bitstream 数据库里配置位的**序列**，以及每个位在特定配置协议下所需的**信息（如地址）**。

它与 `BitstreamManager` 的关系靠 `ConfigBitId` 对接：每个 fabric 位都记着自己源自哪个 device 级配置位（`config_bit_ids_`），但**不复制**那个位的值，而是另外存协议相关字段。所以 fabric 级是 device 级的「视图」或「重组结果」，device 级才是原始数据。

为什么 fabric 级要分这么多字段？因为不同协议要的「送货信息」完全不同：

| 协议 | fabric 级需要存什么 |
|---|---|
| scan_chain / standalone | 只需**线性顺序**（按链从前到后/从后到前），不需要地址 |
| memory_bank | 每个**位的 BL 地址 + WL 地址 + 数据输入 din** |
| frame_based | 每个**位的层级地址**（顶层地址 … 各级父模块地址）+ din |
| ql_memory_bank | 同 memory_bank，但走更紧凑的 bank 表示 |

于是 `FabricBitstream` 用一组开关（`use_address_` / `use_wl_address_`）控制「要不要分配地址数据」——scan_chain 不分配地址，省内存；memory_bank/frame_based 才分配。

#### 4.2.2 核心流程

fabric 级不是「凭空生成」，而是对 device 级的**只读重组**（详见 4.3）。重组时按协议分派，每来一个 device 配置位就：

1. `add_bit(config_bit_id)`：新建一个 fabric 位，记下它的 device 级来源（回链 `ConfigBitId`）。
2. 按协议写入送货信息：
   - scan_chain：只追加进当前 region 的序列，region 内可能再 `reverse()`。
   - memory_bank：根据 memory index 算出 BL/WL 地址，`set_bit_bl_address` / `set_bit_wl_address` + `set_bit_din`。
   - frame_based：拼出层级地址，`set_bit_address` + `set_bit_din`。
3. `add_bit_to_region`：归入某个 `FabricBitRegionId`。

memory_bank 下的**地址计算**是一个简单但关键的数学关系。设当前 region 内一个存储单元的线性编号为 \(i\)（从 0 开始），该 region 的 BL 数为 \(N_{bl}\)、WL 数为 \(N_{wl}\)，则：

\[
\text{bl\_index} = \left\lfloor \frac{i}{N_{bl}} \right\rfloor,\qquad
\text{wl\_index} = i \bmod N_{wl}
\]

即「行号除、列号模」。再把这两个整数转成定长二进制串就是 BL/WL 译码器要的地址码。

**地址的 0/1/x 编码**。frame_based 协议下，一个地址位可能取 `0`、`1` 或 `x`（don't-care，因为不同子模块地址宽度不同时会补 `x`）。`FabricBitstream` 把一个地址串压成**两个整数**：

- **1bits 数**：把 `0/1` 位按二进制读出来（`x` 当 0）。例 `101x1` → `10101` → 21。
- **xbits 数**：把 `x` 位的位置标出来。例 `101x1` → `00010` → 2。

解码时把两者合成就还原出含 `x` 的地址。地址长于 64 位时用多个 `uint64_t` 串起来。

**memory bank 的紧凑表示**。对大阵列，逐位存 BL/WL 地址串会非常费内存（一个 100K LE 的器件可能上千位地址）。于是 `FabricBitstreamMemoryBank` 用「按 region → 按 WL 分桶，每桶用 `uint8_t` 每 8 个 BL 压一位」的紧凑结构（`datas` 存值、`masks` 存「哪些 BL 真的被用到」），并维护 `wls_to_skip` 供 fast_configuration 跳过整行。

#### 4.2.3 源码精读

**设计意图**。文件头注释给出了 `ArchBitstreamManager → FabricBitstream` 的对接图，并强调「靠 `ConfigBitId` 链接」：

[fabric_bitstream.h:1-29](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/fabric_bitstream.h#L1-L29) —— 说明 fabric 级存的是「序列 + 协议寻址信息」，通过 `ConfigBitId` 与 device 级对接。

**单个 fabric 位的最小数据**。`fabric_bit_data` 记一个位的 `(region, bl, wl, bit)`，是 memory bank 紧凑表示里 `fabric_bit_datas` 的元素（供 XML 生成用）：

[fabric_bitstream.h:45-61](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/fabric_bitstream.h#L45-L61) —— `fabric_bit_data` 与 `fabric_blwl_length`。

**内部数据全貌**。这一组就是 fabric 级的全部存储：region→位 的分组、回链 `config_bit_ids_`、`use_address_`/`use_wl_address_` 开关、四条地址编码向量（BL/WL 各一对 1bits+xbits）、din、以及紧凑的 `memory_bank_data_`：

[fabric_bitstream.h:307-351](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/fabric_bitstream.h#L307-L351) —— fabric 级内部数据成员。

**地址 0/1/x 编码的注释**。这段注释用 `101x1` 的例子把编码规则讲得最清楚，是理解 frame_based 地址的关键：

[fabric_bitstream.h:325-344](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric_bitstream/fabric_bitstream.h#L325-L344) —— 地址编码策略：1bits 数 + xbits 数。

**memory bank 紧凑表示**。`datas`/`masks` 的三层结构（region → WL → BL 压位）和 `wls_to_skip` 都在这里，注释把「`uint8_t #0 = MSB{BL#7..BL#0} LSB`」的打包方式讲得很细：

[fabric_bitstream.h:66-122](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric_bitstream/fabric_bitstream.h#L66-L122) —— `FabricBitstreamMemoryBank`：紧凑的 memory bank 数据库。

**高效写入接口**。`set_memory_bank_info` 是给 memory bank 用的「一步写到位」接口，注释解释了为什么不逐位存长 BL/WL 串（大器件会到几十 GB）：

[fabric_bitstream.h:285-289](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/fabric_bitstream.h#L285-L289) —— `set_memory_bank_info`：紧凑写入 memory bank 协议数据。

#### 4.2.4 代码实践

**实践目标**：手算一遍地址编码，确认你真的读懂了 `FabricBitstream` 的存储方式。

**操作步骤**：

1. 打开 [fabric_bitstream.h:325-344](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/fabric_bitstream.h#L325-L344)，阅读 `101x1` 的例子。
2. 自己把地址串 `1x0x1` 按「1bits 数 + xbits 数」编码。
3. 对 memory_bank，假设某 region 有 \(N_{bl}=4\)、\(N_{wl}=4\)，求第 \(i=9\) 个存储单元的 BL/WL index。

**需要观察的现象 / 预期结果**：

- `1x0x1`：1bits 数把 `x` 当 0 读 → `10001` = 17；xbits 数标 `x` 位置 → `01010` = 10。
- memory_bank：\(\text{bl}=\lfloor 9/4 \rfloor = 2\)，\(\text{wl}=9 \bmod 4 = 1\)。

**结论自检**：你应当能解释「为什么 scan_chain 不需要 `use_address_`，而 memory_bank 必须开」——因为链协议靠物理串联的顺序定位每个位，不需要软件地址；memory_bank 靠译码器寻址，必须给每个位配地址。

#### 4.2.5 小练习与答案

**练习 1**：`FabricBitstream` 里 `config_bit_ids_` 的作用是什么？为什么不直接存位的值？
**答案**：它是回链 device 级 `ConfigBitId` 的指针。值的真身在 `BitstreamManager::bit_values_`，fabric 级只存「送货信息」，按需用回链去取值（如 `set_bit_din` 时调 `bitstream_manager.bit_value(config_bit)`）。这样避免两份冗余、且 device 级可被多种 fabric 视图共享。

**练习 2**：`FabricBitstreamMemoryBank` 为什么要 `masks`？只有 `datas` 不够吗？
**答案**：`datas` 的每个 `uint8_t` 里 8 个 BL 都有值，但很多 BL 是 don't-care（未被使用）。`masks` 用 1 标出「真正被使用、值有意义」的 BL，0 标 don't-care。写出比特流或做 fast_configuration 时靠 `masks` 区分「要写的位」和「可跳过的位」。

---

### 4.3 device→fabric 转换：把配置块树重组为带地址的序列

#### 4.3.1 概念说明

两级模型之间的桥梁是 `build_fabric_dependent_bitstream()`。它的定位在函数注释里一句话讲透：**重新组织**比特流，使配置位按「可直接灌进配置协议」的顺序排列；它**不修改**比特流数据库，而是为 `BitstreamManager` 里的配置位构建一组 id（即 fabric 位）。

换句话说，这一步是纯**只读 + 重组**：device 级比特流原封不动，fabric 级只是按协议把它「读出来排好序、贴上地址」。

重组的对接钥匙就是 4.1 反复强调的 **`block_name == instance_name`**：

- device 级：块名 = 模块实例名。
- fabric 级重组：沿 `ModuleManager` 的 `configurable_children`（可配置子模块列表，u6-l4 排好的顺序）做 DFS，每到一层用实例名去 `BitstreamManager` 里 `find_child_block` 找到对应的块，再下钻。
- 因为两边顺序与命名一致，DFS 走完后叶子块的配置位就按「芯片上存储器实际排列顺序」进到了 fabric 序列里。

这条链为什么成立？因为 device 级构建（`build_device_bitstream`）和顶层配置总线（`build_top_module`，u6-l4）用的是**同一份** `ModuleManager`、同一套实例名与可配置子模块顺序。两级模型共享这个「单点真相」。

#### 4.3.2 核心流程

转换由一个按协议分派的 `switch` 驱动（`build_module_fabric_dependent_bitstream`），每种协议一个递归函数，但骨架相同：

```
rec_build(parent_block, parent_module):
  if parent_block 有子块:                # 非叶子：下钻
    for child in module_manager.configurable_children(parent_module):
        instance_name = ...              # 取实例名
        child_block = bitstream_manager.find_child_block(parent_block, instance_name)  # 钥匙
        rec_build(child_block, child_module)
    # 非叶子块自身不带位（断言保证）
  else:                                  # 叶子：取位，按协议贴送货信息
    for config_bit in bitstream_manager.block_bits(parent_block):
        fabric_bit = fabric_bitstream.add_bit(config_bit)   # 回链 device 位
        <按协议写 地址/din，归入 region>
```

各协议的差异只在「叶子处怎么写送货信息」和「是否在顶层跳过译码器子模块」：

- **scan_chain / standalone**（`rec_build_module_fabric_dependent_chain_bitstream`）：叶子处只 `add_bit` + `add_bit_to_region`，不写地址；scan_chain 在每个 region 末尾额外 `reverse_region_bits`（因为链是从 tail 倒着灌的）。
- **memory_bank**（`rec_build_module_fabric_dependent_memory_bank_bitstream`）：维护一个跨整个 region 的 `cur_mem_index`，每写一位就用上面的「行除列模」公式算 BL/WL 地址；顶层会跳过列表末尾的 BL/WL 译码器两个子模块。
- **frame_based**（`rec_build_module_fabric_dependent_frame_bitstream`）：沿下钻路径把每级译码器地址拼起来，顶层地址在尾、子模块地址在头，叶子处再补本块译码器的地址；不同子模块地址宽度不等时用 `x`（don't-care）补齐。
- **ql_memory_bank**：交给单独的 `build_module_fabric_dependent_bitstream_ql_memory_bank`（留 u7-l3 / u9-l1 展开）。

顶层 `build_fabric_dependent_bitstream()` 还处理 `fpga_core` wrapper 的层级穿透（与 device 级对称），并在结尾断言 `bitstream_manager.num_bits() == fabric_bitstream.num_bits()`——保证一个 device 位都不漏、都不重。

#### 4.3.3 源码精读

**重组、只读、不修改**。函数注释把转换的本质讲得最准：重组顺序、不修改数据库、只建一组 id：

[build_fabric_bitstream.cpp:758-772](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp#L758-L772) —— `build_fabric_dependent_bitstream` 的设计说明。

**对接钥匙**。chain 协议的递归函数里，下钻时用实例名 `find_child_block` 找块，叶子处把每个 device 位 `add_bit` 进 fabric 序列。这就是「块名 == 实例名」链的落点：

[build_fabric_bitstream.cpp:37-123](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp#L37-L123) —— `rec_build_module_fabric_dependent_chain_bitstream`：DFS 下钻 + 叶子 `add_bit`。

其中叶子处的两句是两级对接的核心动作：

[build_fabric_bitstream.cpp:118-122](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp#L118-L122) —— 叶子块：把 device 配置位 `add_bit` 成 fabric 位并归入 region。

**memory_bank 地址计算**。叶子处用 `cur_mem_index` 算出 BL/WL 地址并写入，正是 4.2.2 公式的代码落地：

[build_fabric_bitstream.cpp:251-280](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp#L251-L280) —— memory_bank 叶子：`floor(index/num_bls)` 得 BL、`index % num_wls` 得 WL，写地址 + din。

**协议分派 switch**。整个转换的入口分发，五种协议各自走不同递归函数，scan_chain 多一步 `reverse_region_bits`：

[build_fabric_bitstream.cpp:549-756](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp#L549-L756) —— `build_module_fabric_dependent_bitstream`：按 `config_protocol.type()` 分派。

**收尾断言**。switch 末尾断言两级位数相等，是「不漏不重」的硬保证：

[build_fabric_bitstream.cpp:754-755](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp#L754-L755) —— `VTR_ASSERT(bitstream_manager.num_bits() == fabric_bitstream.num_bits())`。

**顶层入口**。处理 `fpga_core` 穿透、定位顶层块、触发分派：

[build_fabric_bitstream.cpp:773-821](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp#L773-L821) —— `build_fabric_dependent_bitstream`：顶层重组入口。

**命令编排佐证**。`build_fabric_bitstream` 命令在模板里读 device 级 `bitstream_manager()`、写 fabric 级 `mutable_fabric_bitstream()`，正是两级模型在 context 里的体现：

[openfpga_bitstream_template.h:95-109](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_template.h#L95-L109) —— `build_fabric_bitstream_template`：调 `build_fabric_dependent_bitstream`，读 `bitstream_manager()` 写 `mutable_fabric_bitstream()`。

#### 4.3.4 代码实践

**实践目标**：跟踪 memory_bank 协议下「一个 device 配置位变成带 BL/WL 地址的 fabric 位」的完整过程。

**操作步骤**：

1. 打开 [build_fabric_bitstream.cpp:585-641](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp#L585-L641)（`CONFIG_MEM_MEMORY_BANK` 分支），看清它如何取顶层 BL/WL 地址端口宽度与本地译码器的 BL/WL 宽度。
2. 跟到 [build_fabric_bitstream.cpp:251-280](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp#L251-L280)，确认 `cur_mem_index` 的「行除列模」。
3. 对比同文件 scan_chain 分支 [build_fabric_bitstream.cpp:570-584](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp#L570-L584)，注意它**不取任何地址端口**、只在 region 末尾 `reverse`。

**需要观察的现象**：

- memory_bank 分支会去 `find_module_port(top_module, DECODER_BL/WL_ADDRESS_PORT_NAME)` 取地址宽度——因为要算地址。
- scan_chain 分支没有任何 `find_module_port` 地址调用——因为链协议不需要地址。
- 两者都用同一个 `rec_build_...` 的「下钻找块」骨架，差别只在叶子处的送货信息。

**预期结果**：你能用一句话讲清「为什么换一个配置协议，device 级比特流不用动、只需重跑 fabric 转换」——因为 device 级与协议无关，协议只影响重组这一步。

> 本实践为**源码阅读型实践**，无需运行；如想验证，可对同一设计分别用 `cc` 与 `bank` 两套 arch（u3-l4）跑 `build_fabric_bitstream`，对比 fabric 比特流输出（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：转换函数里大量出现 `VTR_ASSERT(true == bitstream_manager.valid_block_id(child_block))`，如果这条断言失败，通常说明什么？
**答案**：说明 device 级构建时**没有**为某个可配置实例创建同名块——即 `block_name == instance_name` 的对接钥匙断了。常见原因是 device 构建与 fabric 转换用了不一致的 `ModuleManager`（例如 fabric 被改过但 device 比特流没重建），或实例名生成规则在两级间不一致。

**练习 2**：为什么 memory_bank 在顶层要 `configurable_children.size() - 2`，而 frame_based 只 `- 1`？
**答案**：memory_bank 在可配置子模块列表末尾挂了**两个**译码器（BL 译码器、WL 译码器），数位时要跳过这两个；frame_based 只挂**一个**帧译码器，所以只跳一个。这正是 u6-l4 里「顶层按协议挂译码器占位」的下游体现。

**练习 3**：scan_chain 协议为什么要 `reverse_region_bits`，而 standalone 不用？
**答案**：scan_chain 的配置链是物理串联的，灌入时从 head 一路移位到 tail，因此芯片上存储器实际的位顺序与 DFS 遍历顺序相反，需要把每个 region 的序列反转才能匹配；standalone 每位独立寻址、没有移位方向问题，所以不反转。

---

## 5. 综合实践

**任务**：完成本讲指定的核心实践——对比 `BitstreamManager` 与 `FabricBitstream` 的数据组织方式，并回答「为什么需要两级表示而不是直接生成最终比特流」。

**步骤**：

1. 并排打开两个头文件：
   - [bitstream_manager.h:226-274](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libfpgabitstream/src/bitstream_manager.h#L226-L274)（device 级内部数据）
   - [fabric_bitstream.h:307-351](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/fabric_bitstream.h#L307-L351)（fabric 级内部数据）
2. 填一张对比表：

   | 维度 | BitstreamManager（device） | FabricBitstream（fabric） |
   |---|---|---|
   | 组织方式 | 配置块**树** + 叶子上的位 | 线性**序列** + region 分组 |
   | 存不存位值 | 存（`bit_values_`） | 不存，靠 `config_bit_ids_` 回链取 |
   | 存不存地址 | 不存 | 存（BL/WL/帧地址，受 `use_address_` 控制） |
   | 是否依赖协议 | 否 | 是 |
   | 是否可改 | 构建后基本只读 | 由 device 级重组而来 |

3. 读转换函数注释 [build_fabric_bitstream.cpp:758-772](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp#L758-L772)，确认「重组、不修改、只建 id」。
4. 写出你对「为什么不直接生成最终比特流」的回答。

**参考答案要点**（自己先想再对照）：

- **关注点分离**：device 级回答「每个配置位是什么值、属于哪个实例」（语义，来自 LUT 真值表与布线 mux 选择）；fabric 级回答「按什么顺序、用什么地址送进芯片」（协议机制）。混在一起会让解码逻辑与协议逻辑纠缠。
- **可复用 / 可重排**：同一份 device 比特流可被重组成多种 fabric 序列——换协议、换 fabric key、开 fast_configuration、改 WL 顺序——都只需重跑只读的 fabric 转换，不必重新解码 LUT 与布线。若一步到位，每次改动都要从布局布线结果重新解码，代价高昂。
- **可独立导出**：device 级与协议无关，能导出成通用 XML（`fabric_independent_bitstream.xml`），脱离具体 fabric 供离线分析；fabric 级绑定具体协议地址。
- **共享单点真相**：两级都挂在同一份 `ModuleManager`（同一套实例名与可配置子模块顺序）上，靠 `block_name == instance_name` + `ConfigBitId` 回链对接，使转换成为纯只读重组，避免数据冗余与不一致。

## 6. 本讲小结

- OpenFPGA 用**两级比特流模型**：device 级 `BitstreamManager`（配置块树 + 配置位，与协议无关）和 fabric 级 `FabricBitstream`（线性序列 + 协议寻址，与协议相关）。
- `BitstreamManager` 是 `ModuleGraph` 的**展平图**：每个实例一个块、块名 == 实例名，配置位只挂在叶子块上，额外存 mux path/net 标注。
- `FabricBitstream` 靠 `ConfigBitId` **回链** device 级取值，自己只存「送货信息」；用 `use_address_`/`use_wl_address_` 开关按协议决定是否分配地址，memory_bank 还有 `FabricBitstreamMemoryBank` 紧凑表示。
- 不同协议的送货信息不同：scan_chain 只要顺序（region 内反转）、memory_bank 要 BL/WL 地址（\(\text{bl}=\lfloor i/N_{bl}\rfloor\)，\(\text{wl}=i\bmod N_{wl}\)）、frame_based 要层级地址（0/1/x 编码成 1bits + xbits 两数）。
- device→fabric 转换是**只读重组**：沿 `ModuleManager` 的 `configurable_children` DFS，用 `block_name == instance_name` 找块、`ConfigBitId` 回链取位，结尾用断言保证「不漏不重」。
- 之所以要两级而非一步到位，核心是**关注点分离 + 可复用**：换协议/fabric key/fast_config 只重跑只读的 fabric 转换，不必重新解码设计。

## 7. 下一步学习建议

- **u7-l2（build_architecture_bitstream）**：打开 device 级构建器内部，看 `build_grid_bitstream` 如何从 LUT 真值表与 pb 标注解码出配置位、`build_routing_bitstream` 如何为每个布线 mux 决定选择位——补齐本讲跳过的「位的值从哪来」。
- **u7-l3（build_fabric_bitstream 与协议相关组织）**：深入 `build_fabric_bitstream_memory_bank.cpp` 与 `ql_memory_bank`、`shift_register` 变体，看 memory bank 紧凑表示是如何被填出来的。
- **u7-l4（write_fabric_bitstream 与 fast_configuration）**：看两级模型最终如何落盘成 plain_text / XML，以及 fast_configuration 如何利用 `masks`/`wls_to_skip` 跳过大量相同位。
- 旁读 u9-l1（存储器组与移位寄存器 Bank）与 u6-l4（顶层模块与配置总线），理解本讲「memory index」「配置区域」「BL/WL 宽度」这些输入是从哪一步确定的。
