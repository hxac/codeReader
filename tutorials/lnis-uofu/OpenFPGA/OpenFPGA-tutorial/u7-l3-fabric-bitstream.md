# build_fabric_bitstream 与协议相关组织

## 1. 本讲目标

上一讲（u7-l2）我们生成了与 fabric 无关的 **device 级比特流**：它是一棵镜像模块层级的配置块树，叶子块上挂着 0/1 配置位，但完全没有「按什么顺序、用什么地址把这些位写进芯片」的信息。本讲要回答的正是这个问题。

`build_fabric_bitstream` 命令把 device 级比特流**按 fabric 模块层级与配置协议重新组织**，产出一个可直接下载到 FPGA 的 **fabric 级比特流**（`FabricBitstream`）。学完本讲你应当能够：

1. 说清 device 比特流 → fabric 比特流的「只读重组」过程，以及为什么用 DFS 遍历模块层级。
2. 区分五大配置协议（standalone / scan_chain / memory_bank / ql_memory_bank / frame_based）在重组时各自算什么、存什么。
3. 理解 memory_bank 下的 **`FabricBitstreamMemoryBank` 紧凑表示**（`datas` / `masks`）为什么能省下海量内存。
4. 认识 ql_memory_bank 下 BL/WL 各自可选的 **decoder / flatten / shift_register** 三种子协议，以及它们对应的不同输出形态。

本讲只讲「重组与组织」，文件格式细节（plain_text / xml）与 `fast_configuration` 留到 u7-l4。

## 2. 前置知识

本讲承接 u7-l1（两级比特流模型）和 u7-l2（device 比特流生成），并用到 u3-l4（配置协议）与 u6（ModuleManager）的概念。这里把最关键的几条回顾一下：

- **两级比特流**：device 级 `BitstreamManager` 与配置协议无关；fabric 级 `FabricBitstream` 与协议相关，靠 `ConfigBitId` 回链 device 级取位值，自身只额外存「寻址信息」。位值（0/1）只在 device 级存一份，fabric 级不重复存。
- **配置协议类型**：`standalone`（每个存储器独立引脚，不寻址）、`scan_chain`（串行链）、`memory_bank`（BL/WL 矩阵寻址，固定用译码器）、`ql_memory_bank`（QuickLogic 风格 memory bank，BL/WL 可独立选 decoder/flatten/shift_register）、`frame_based`（帧寻址）。这些在 `openfpga_arch.xml` 的 `<configuration_protocol>` 节点声明。
- **可配置子模块（configurable children）**：`ModuleManager` 里每个模块都维护一份「可配置子模块实例列表」。对顶层模块按 **配置区域（config region）** 组织（`region_configurable_children`），对其他模块则取 **PHYSICAL 类型** 的可配置子模块（`configurable_children(..., PHYSICAL)`）——它对应物理可编程存储器的真实排布，是比特流寻址的依据（见 u6-l1、u6-l4）。
- **块名 == 实例名**：device 比特流的配置块名严格等于模块管理器里的实例名。这是 device 级与 fabric 级两棵树「对账」的钥匙。

一句话概括本讲要做的事：**沿着模块层级做一次 DFS，把 device 比特流里的配置位按「物理存储器在芯片上的访问顺序」重新排成一个线性序列，并按协议给每个位附上地址。**

## 3. 本讲源码地图

本讲涉及的关键源码文件：

| 文件 | 作用 |
| --- | --- |
| `openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp` | fabric 比特流重组主体。包含顶层入口 `build_fabric_dependent_bitstream()` 与按协议分派的 `build_module_fabric_dependent_bitstream()`，以及 standalone/scan_chain/memory_bank/frame_based 四种协议的 DFS 递归实现。 |
| `openfpga/src/fpga_bitstream/build_fabric_bitstream_memory_bank.cpp` | ql_memory_bank 专用实现。负责按 tile 预计算 BL/WL 分布、为每个位算出全局 BL/WL 索引，并在 decoder/flatten/shift_register 三种地址编码之间分流。 |
| `openfpga/src/fpga_bitstream/fabric_bitstream.h` / `.cpp` | `FabricBitstream` 数据结构，以及内嵌的紧凑表示 `FabricBitstreamMemoryBank`（`datas`/`masks`）。 |
| `openfpga/src/fpga_bitstream/memory_bank_flatten_fabric_bitstream.h` / `.cpp` | flatten BL/WL 总线下的可下载比特流格式（按 WL 聚合 BL 数据）。 |
| `openfpga/src/fpga_bitstream/memory_bank_shift_register_fabric_bitstream.cpp` | shift_register 下的「字（word）」式比特流格式。 |
| `openfpga/src/utils/memory_utils.cpp` | `estimate_num_configurable_children_to_skip_by_config_protocol()`：根据协议决定顶层要跳过几个「译码器/移位寄存器」尾部子模块。 |
| `openfpga/src/base/openfpga_bitstream_template.h` | `build_fabric_bitstream` 命令的执行模板（壳）。 |
| `openfpga/src/base/openfpga_bitstream_command_template.h` | `build_fabric_bitstream` 命令的注册与依赖声明。 |

此外，`openfpga/src/fpga_bitstream/write_text_fabric_bitstream.cpp` 虽属 u7-l4，但本讲会用它来对照「fabric 比特流被如何消费」，帮助理解不同组织形态的输出差异。

## 4. 核心概念与源码讲解

### 4.1 build_fabric_bitstream：device→fabric 的协议相关重组

#### 4.1.1 概念说明

`build_fabric_bitstream` 命令做的事情可以用一句话概括：**不重新解码任何 LUT 真值表或布线 mux，只把已经算好的 device 比特流重新排个序、补上地址。**

为什么要「重排」？因为 device 比特流是按**模块层级**组织的（先 grid 再 routing，每个模块一个块），而真正下载比特流时，芯片是按**配置协议的物理访问顺序**一位一位读入的：

- scan_chain 是一条长链，位的顺序就是「链上存储器的物理顺序」。
- memory_bank 是矩阵寻址，每个位需要知道自己的 (BL 地址, WL 地址, 数据输入)。
- frame_based 是帧寻址，每个位需要知道自己的层级地址。

所以同一个 device 比特流，配不同协议会得到完全不同的 fabric 比特流。这正是 u7-l1 强调的「换协议只需重跑只读的 fabric 转换」的落点。

#### 4.1.2 核心流程

整个重组是一个**带协议分派的有向流程**：

```
build_fabric_bitstream 命令
        │  (执行模板 openfpga_bitstream_template.h)
        ▼
build_fabric_dependent_bitstream()          ← 顶层入口
        │
        ├─ 定位顶层模块（fpga_top，或 fpga_core wrapper）
        ├─ 在 device 比特流里找同名顶层块
        │
        └─ build_module_fabric_dependent_bitstream()   ← 按 config_protocol.type() 分派
                │
                ├─ CONFIG_MEM_STANDALONE   → DFS 链式收集，不算地址
                ├─ CONFIG_MEM_SCAN_CHAIN   → DFS 链式收集，区域内部再反转
                ├─ CONFIG_MEM_MEMORY_BANK  → DFS + 每位算 (BL,WL) 二进制地址
                ├─ CONFIG_MEM_QL_MEMORY_BANK → 委托给 ql_memory_bank 专用函数
                └─ CONFIG_MEM_FRAME_BASED  → DFS + 每位算层级地址（含 'x' 通配）
```

每种协议都共享同一个 **DFS 骨架**：

1. 从顶层块出发，按模块的「可配置子模块列表」逐个下钻。
2. 顶层用 `region_configurable_children(top, config_region)`（按配置区域），其余模块用 `configurable_children(parent, PHYSICAL)`（物理子模块）。
3. 用**实例名**在 device 比特流里 `find_child_block(parent_block, instance_name)` 找到对应的子块（块名 == 实例名）。
4. 递归到叶子块时，取出其上的 `ConfigBitId`，调 `fabric_bitstream.add_bit(config_bit)` 生成一个 `FabricBitId`，并按协议附加地址 / 数据输入信息。
5. 结尾断言 `bitstream_manager.num_bits() == fabric_bitstream.num_bits()`，保证位不漏不重。

#### 4.1.3 源码精读

**顶层入口**：[openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp:773-821](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp#L773-L821) —— 构造一个空的 `FabricBitstream`，定位顶层模块名（经 `module_name_map` 解析，支持 `fpga_core` wrapper 改名），在 device 比特流里找到唯一顶层块并断言其名字与顶层模块一致，然后交给分派器。

注意 L797-810 这段：如果存在 `fpga_core` 核心模块（一个可选的、把硬核 IP 包起来的内层 wrapper），则把「顶层」下钻一层，改用 core 模块和 core 块作为后续重组的起点。这是「fabric 层级」的真实起点。

**协议分派器**：[openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp:549-756](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp#L549-L756) —— 一个大 `switch`，按 `config_protocol.type()` 走 5 个分支。每个分支先做协议相关的准备（找地址端口宽度、开关 `use_address_`/`use_wl_address_`、`reserve_bits`），再按配置区域循环调用各自的 DFS 递归函数。结尾 L755 的断言保证 fabric 比特流位数等于 device 比特流位数。

**scan_chain 分支**：[openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp:570-584](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp#L570-L584) —— 调 `rec_build_module_fabric_dependent_chain_bitstream` 收集位，然后 L581 调 `fabric_bitstream.reverse_region_bits(region)` 把**每个区域内部的位序反转**。这是因为配置链是「头进尾出」，最后写入的位会被推到链的最远端，所以下载顺序与存储器物理顺序恰好相反。standalone 分支（L555-569）与 scan_chain 几乎一样，唯一差别就是**不反转**。

**链式 DFS 递归**：[openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp:37-123](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp#L37-L123) —— 把上面「DFS 骨架」完整实现了一遍。注意 L110-111 断言「非叶子块上不能直接挂配置位」，L118-122 在叶子块上把每个 `ConfigBitId` 加进 fabric 比特流并归入当前区域。这就是「位值不重复存、只存 ConfigBitId 引用」的地方。

**命令执行壳**：[openfpga/src/base/openfpga_bitstream_template.h:95-109](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_template.h#L95-L109) —— `build_fabric_bitstream_template` 把 context 里的 `bitstream_manager`（device 级，读）、`module_graph`、`module_name_map`、`arch().circuit_lib`、`arch().config_protocol` 一起喂给 `build_fabric_dependent_bitstream()`，结果写入 `mutable_fabric_bitstream()`。命令本身只有一个 `--verbose` 选项，没有文件参数——它只产出内存数据，落盘是 `write_fabric_bitstream` 的事。

**命令依赖**：[openfpga/src/base/openfpga_bitstream_command_template.h:350-356](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_command_template.h#L350-L356) —— `build_fabric_bitstream` 把 `build_architecture_bitstream` 列为硬前置依赖。这构成 u2-l2 提到的教科书级依赖链：`repack → build_architecture_bitstream → build_fabric_bitstream → write_fabric_bitstream`。少了 `build_architecture_bitstream`，device 比特流为空，重组自然无米下炊。

#### 4.1.4 代码实践

**实践目标**：用「源码阅读 + 局部实验」的方式，验证 device→fabric 重组是只读的、且 scan_chain 会反转位序。

**操作步骤**：

1. 打开 [build_fabric_bitstream.cpp:37-123](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp#L37-L123)，确认整个递归过程中**没有任何对 `bitstream_manager` 的写操作**（没有 `mutable`、没有 set），只有读访问器（`block_children`、`find_child_block`、`block_bits`、`bit_value`）。这印证了「只读重组」。
2. 在 [build_fabric_bitstream.cpp:581](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp#L581) 的 `reverse_region_bits` 调用前后各加一行日志（**示例代码，勿提交**）：

   ```cpp
   VTR_LOG("Before reverse: region %lu has %lu bits\n",
           (size_t)config_region, fabric_bitstream.region_bits(fabric_bitstream_region).size());
   fabric_bitstream.reverse_region_bits(fabric_bitstream_region);
   VTR_LOG("After reverse: region %lu bits reversed\n", (size_t)config_region);
   ```

3. 重新编译（`make compile`），跑一个 scan_chain 任务（如 `run-task basic_tests/full_testbench/configuration_chain`），用 `--verbose` 观察日志。

**需要观察的现象**：日志会打印每个区域反转前后的位数（应相等），证明反转只改顺序、不改数量。

**预期结果**：位数不变，但 `write_fabric_bitstream` 输出的 0/1 序列相比「不反转」时是区域内部倒序的。**若不实际改代码运行，此处「待本地验证」具体位序，但「位数守恒」可由 L755 断言直接保证。**

#### 4.1.5 小练习与答案

**练习 1**：为什么 `build_fabric_bitstream` 的命令依赖里必须有 `build_architecture_bitstream`，却没有 `build_fabric`？

**参考答案**：`build_fabric_bitstream` 消费的是 device 级 `BitstreamManager`，而 `BitstreamManager` 由 `build_architecture_bitstream` 生成，所以前者硬依赖后者。`build_fabric`（构造 `ModuleManager`）虽然也是上游，但它的产物通过命令依赖链间接保证（`build_architecture_bitstream` 自身依赖 `repack`，而 `repack` 依赖 `build_fabric`），shell 的依赖检查只查一级，故这里不必重复声明。

**练习 2**：如果存在 `fpga_core` wrapper，顶层重组起点是哪个模块？

**参考答案**：是 `fpga_core` 核心模块及其对应的核心块（见 build_fabric_bitstream.cpp:802-810），而非最外层的 `fpga_top`。`fpga_top` 仅作为壳，真正的可配置 fabric 在 core 层。

---

### 4.2 memory bank 紧凑表示：FabricBitstreamMemoryBank 的 datas/masks

#### 4.2.1 概念说明

memory_bank 协议下，每个配置位都需要带 (BL 地址, WL 地址, 数据输入)。最朴素的存法是「每个位存一份完整的 BL/WL 地址字符串」——但这会爆炸：一个 10 万 LE 的 FPGA，BL/WL 地址都可能上千位，每个位都存一份完整地址会消耗几十 GB 内存。

OpenFPGA 的解决办法是把整张「BL × WL 矩阵」存成一个**位图（bitmap）**：

- 把每个 (区域, WL) 看成一行，每行用一个 `uint8_t` 数组表示所有 BL 的取值（每个 `uint8_t` 装 8 个 BL）。
- 用两张同构的表：`datas` 存真实数据输入值，`masks` 存「这个 BL 是否被用到」（未用到的 BL 是 don't-care）。

这就是内嵌在 `FabricBitstream` 里的 `FabricBitstreamMemoryBank`。注释里写得直白：「100K LE FPGA only need few mega bytes」。

#### 4.2.2 核心流程

紧凑表示的填充与使用流程：

```
ql_memory_bank 重组（build_fabric_bitstream_memory_bank.cpp）
        │
        ├─ 预计算每列/行的 BL/WL 起始索引（bl_start_index_per_tile 等）
        ├─ DFS 到叶子块，为每个位算全局 (bl_index, wl_index)
        │
        ├─ 若 BL 与 WL 都是 flatten：
        │     调 set_memory_bank_info() → 写入 datas/masks 紧凑表   ← 新方式
        │
        └─ 否则（至少一方是 decoder 或 shift_register）：
              调 set_bit_bl/wl_address() → 存成字符串地址         ← 旧方式

写出阶段（write_text_fabric_bitstream.cpp）
        │
        └─ 调 fabric_bitstream.memory_bank_info(fast, skip)
              └─ 内部按需调 fast_configuration()，算出 wls_to_skip
                 然后逐 WL 打印 datas/masks
```

紧凑表的数据布局（来自头文件注释）：

```
datas[region][wl] = std::vector<uint8_t>   // 每个 uint8_t 装 8 个 BL 的 din
masks[region][wl] = std::vector<uint8_t>   // 同构，1=该 BL 被使用，0=don't-care
```

每个 BL 在字节里的定位用位运算：**字节下标 = `bl >> 3`，字节内位 = `1 << (bl & 7)`**。

#### 4.2.3 源码精读

**数据结构定义**：[openfpga/src/fpga_bitstream/fabric_bitstream.h:66-122](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/fabric_bitstream.h#L66-L122) —— `FabricBitstreamMemoryBank` 结构体。L85 `blwl_lengths` 记录每个区域的 BL/WL 宽度；L93 `fabric_bit_datas` 是「逐位的原始记录」(region, bl, wl, bit)，仅供 XML 输出用；L109 `datas` 与 L119 `masks` 才是真正的紧凑数据；L121 `wls_to_skip` 记录 fast_configuration 要跳过的 WL。注释 L96-108 详细画出了 `datas[region][wl]` 的字节排布。

**写入一个位**：[openfpga/src/fpga_bitstream/fabric_bitstream.cpp:17-64](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/fabric_bitstream.cpp#L17-L64) —— `add_bit()`。L52-55 按需扩张 `datas[region]`/`masks[region]` 到能容纳当前 `wl`，每个 WL 行分配 `(bl_addr_size + 7) / 8` 个字节；L57 断言「同一个 (region,wl,bl) 不能被设两次」；L58-61 若位值为真则在 `datas` 里置位；L63 在 `masks` 里置位标记「已使用」。注意 `masks` 永远会被置位，`datas` 只在位值为 1 时置位——所以读出时「mask=0」即 don't-care，「mask=1 且 data=1」即写 1，「mask=1 且 data=0」即写 0。

**FabricBitstream 的转发接口**：[openfpga/src/fpga_bitstream/fabric_bitstream.cpp:377-396](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/fabric_bitstream.cpp#L377-L396) —— `set_memory_bank_info()` 把 (bit_id, region, bl, wl, bl/wl 宽度, bit) 转发给 `memory_bank_data_.add_bit()`。

**惰性计算 wls_to_skip**：[openfpga/src/fpga_bitstream/fabric_bitstream.cpp:254-261](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/fabric_bitstream.cpp#L254-L261) —— `memory_bank_info()` 是只读访问器，但内部用 `const_cast` 触发一次 `fast_configuration()`，按「整行 WL 的有效位是否都等于要跳过的值」决定哪些 WL 可跳过。实现在 [fabric_bitstream.cpp:66-99](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/fabric_bitstream.cpp#L66-L99)：只有当某 WL 上**所有被 mask 标记的有效位**都等于 `bit_value_to_skip` 时，该 WL 才进 `wls_to_skip`。

**写出阶段的消费**：[openfpga/src/fpga_bitstream/write_text_fabric_bitstream.cpp:276-434](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/write_text_fabric_bitstream.cpp#L276-L434) —— `fast_write_memory_bank_flatten_fabric_bitstream_to_text_file()`。L286-287 取出 `memory_bank_info()`；L314 起外层循环按「最长有效 WL 数」逐行输出；L378-388 内层逐 BL 读 `masks`/`datas`：mask 命中则按 `data` 打 0/1，否则打 don't-care。这段把「紧凑位图」翻译回人类/下载器可读的逐 WL 行。L361-377 的注释表格把 `bl >> 3` 与 `1 << (bl & 7)` 的含义讲得非常清楚，建议直接读这段注释。

#### 4.2.4 代码实践

**实践目标**：通过阅读源码，亲手算出一个位在 `datas` 字节数组里的落点，验证位运算的正确性。

**操作步骤**：

1. 假设某区域有 20 个 BL（`bl_addr_size = 20`），那么每个 WL 行需要 `(20 + 7) / 8 = 3` 个 `uint8_t`（即 24 位，高 4 位闲置）。
2. 现在要写入 `(region=0, wl=2, bl=11, bit=true)`。手算：
   - 字节下标 `bl >> 3 = 11 >> 3 = 1`（第 2 个字节，下标从 0 起）。
   - 字节内掩码 `1 << (bl & 7) = 1 << (11 & 7) = 1 << 3 = 0x08`。
   - 所以应执行 `datas[0][2][1] |= 0x08;` 与 `masks[0][2][1] |= 0x08;`。
3. 对照 [fabric_bitstream.cpp:57-63](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/fabric_bitstream.cpp#L57-L63) 与写出端 [write_text_fabric_bitstream.cpp:378-388](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/write_text_fabric_bitstream.cpp#L378-L388)，确认写和读用的是同一套位运算。
4. 阅读头文件注释 [fabric_bitstream.h:110-118](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/fabric_bitstream.h#L110-L118) 中 `masks` 的例子（`0x41` 表示 BL#0 与 BL#6 被使用），理解 mask 区分「don't-care」与「真实 0」的作用。

**需要观察的现象**：写入端与读出端的位运算完全对称；mask 决定「是否有效」，data 决定「有效时的取值」。

**预期结果**：`bl=11` 落在第 1 字节的 bit3，手算与源码一致。若你不方便改代码运行，可只做手算对照，结论「待本地验证」运行时字节值。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `masks` 永远会被置位，而 `datas` 只在位值为 1 时置位？

**参考答案**：`masks` 表达「这个 BL 在本设计中是否被用到」，被用到就必须标记，否则写出时会被当成 don't-care（可能被全局 reset/set 覆盖）。`datas` 表达「被用到时的真实取值」，初值全 0，所以位值为 0 时无需操作，只有位值为 1 才需置位——这是一种节省写入操作的约定。

**练习 2**：为什么不直接用 `fabric_bit_datas`（逐位原始记录）来输出比特流，而要另造 `datas`/`masks`？

**参考答案**：`fabric_bit_datas` 是逐位的 `(region, bl, wl, bit)`，输出时要按 WL 聚合并填上 don't-care，需反复查找；`datas`/`masks` 已按 `(region, wl)` 预排成密集位图，写出时直接顺序遍历字节即可，省去查找与重排，是大阵列下「1 秒 vs 600 秒」的关键（见 write_text_fabric_bitstream.cpp:252-260 的注释）。

---

### 4.3 flatten 与 shift_register：memory bank 的两种组织变体

#### 4.3.1 概念说明

`memory_bank`（CONFIG_MEM_MEMORY_BANK）固定用 BL/WL **译码器（decoder）**：顶层只有 `log2(num_bl)` 位 BL 地址线和 `log2(num_wl)` 位 WL 地址线，译码器把它们展开成 one-hot 的 BL/WL 选择信号。地址是**二进制编码**的。

而 `ql_memory_bank` 更灵活：BL 和 WL 可以**各自独立**选择三种驱动方式之一：

| 子协议 | 含义 | 地址编码 | 顶层是否有译码器子模块 |
| --- | --- | --- | --- |
| `decoder` | 用译码器压缩地址线 | 二进制 | 有（占 1 个尾部子模块） |
| `flatten` | 直接拉出全部 BL/WL 总线 | one-hot | 无 |
| `shift_register` | 用移位寄存器串行加载 BL/WL | 移位链 | 无（移位寄存器不计入可配置子模块） |

这些在 `openfpga_arch.xml` 的 `<configuration_protocol>` 里用 `<bl protocol="..."/>` 和 `<wl protocol="..."/>` 声明（仅 ql_memory_bank 可用，见 u3-l4）。

由此产生两种重要的组织变体：

- **flatten 变体**：BL/WL 是扁平总线。输出按 WL 行聚合，每行把该 WL 上所有 BL 的数据一并写出，可一次写整行——这正是 4.2 节紧凑表示 `datas`/`masks` 的用武之地。
- **shift_register 变体**：BL/WL 由移位寄存器驱动，地址需串行移入。输出组织成一个个「字（word）」，每个 word 含若干 BL 移位头和 WL 移位头的数据。

#### 4.3.2 核心流程

ql_memory_bank 重组的关键决策点是「为每个位算出地址后，按子协议选择存储方式」：

```
对叶子块上的每个 config_bit：
    bl_index = bl_start_index_per_tile[x] + (mem_index % num_bls_cur_tile)
    wl_index = wl_start_index_per_tile[y] + (mem_index / num_bls_cur_tile)

    if (BL==flatten 且 WL==flatten):
        set_memory_bank_info(...)            ← 紧凑 datas/masks（新方式）
    else:
        if BL==decoder:  bl_addr = itobin_charvec(bl_index)      ← 二进制
        else (flatten/shift_register): bl_addr = ito1hot_charvec(bl_index)  ← one-hot
        同理处理 WL
        set_bit_bl/wl_address(...)           ← 字符串地址（旧方式）
    set_bit_din(...)                          ← 两种方式都要存 din
```

注意三个细节：

1. **全局 BL/WL 索引**：用 `bl_start_index_per_tile[x]` 把每个 tile 内的局部 BL 累加成全局 BL（BL 随 tile 的 x 坐标累加，WL 随 y 坐标累加），所以同一位的全局地址是「tile 起点偏移 + tile 内偏移」。
2. **flatten/shift_register 用 one-hot**：`ito1hot_charvec` 生成只有一个 1 的向量；decoder 用 `itobin_charvec` 生成二进制。
3. **紧凑表只对「双 flatten」启用**：只有 BL 与 WL 都是 flatten 时才走 `set_memory_bank_info`。源码注释解释这是「能轻松支持 shift_register，但 decoder 还需进一步评估」。

写出阶段会按协议选不同函数（见 4.3.3 的 writer 分派）。

#### 4.3.3 源码精读

**ql_memory_bank 入口**：[openfpga/src/fpga_bitstream/build_fabric_bitstream_memory_bank.cpp:259-460](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_fabric_bitstream_memory_bank.cpp#L259-L460) —— `build_module_fabric_dependent_bitstream_ql_memory_bank()`。它先按 BL/WL 子协议分别求地址端口宽度（L280-310 算 BL，L325-355 算 WL；decoder 用顶层地址端口，flatten 用各区域最大总线宽，shift_register 用每区域唯一 BL/WL 数），再 L424-445 预计算 BL/WL 在每个 tile 的起始索引分布，最后按区域 DFS。

**叶子的地址计算与存储分流**：[openfpga/src/fpga_bitstream/build_fabric_bitstream_memory_bank.cpp:175-253](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_fabric_bitstream_memory_bank.cpp#L175-L253) —— 这是本节的核心。L189-190 算全局 `cur_bl_index`，L212-214 算 `cur_wl_index`。L191-209 与 L215-232 是「旧方式」：当 BL 或 WL 不是 flatten 时，按 decoder(`itobin_charvec`) 或 flatten/shift_register(`ito1hot_charvec`) 生成地址字符串，调 `set_bit_bl/wl_address`，第三个参数 `tolerant_short_address` 在非 decoder 时为 true（允许 one-hot 向量比地址长度短）。L233-241 是「新方式」：双 flatten 时调 `set_memory_bank_info` 写紧凑表。

**顶层跳过几个尾部译码器**：[openfpga/src/utils/memory_utils.cpp:467-497](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/utils/memory_utils.cpp#L467-L497) —— `estimate_num_configurable_children_to_skip_by_config_protocol()`。memory_bank/ql_memory_bank 默认跳过 2 个尾部子模块（BL 译码器 + WL 译码器）；若 BL 用 flatten 或 shift_register，则少跳 1 个（BL 总线/移位寄存器不占译码器子模块位）；WL 同理。所以 `qlbankflatten`（双 flatten）跳过 0 个，`qlbanksr`（双 shift_register）也跳过 0 个，而 `qlbank`（双 decoder）跳过 2 个。这个数在 [build_fabric_bitstream_memory_bank.cpp:71-75](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_fabric_bitstream_memory_bank.cpp#L71-L75) 被用来裁剪顶层可配置子模块列表。`frame_based` 协议则跳过 1 个（帧译码器），见 [build_fabric_bitstream.cpp:359-388](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp#L359-L388)。

> 对照：CONFIG_MEM_MEMORY_BANK（非 ql）走的是 [build_fabric_bitstream.cpp:163-164](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_fabric_bitstream.cpp#L163-L164)，硬编码 `size() - 2` 跳过 2 个译码器，因为它永远是双 decoder。

**flatten 输出格式**：[openfpga/src/fpga_bitstream/memory_bank_flatten_fabric_bitstream.h:20-50](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/memory_bank_flatten_fabric_bitstream.h#L20-L50) —— `MemoryBankFlattenFabricBitstream`。内部是一张 `std::map<vector<string>, vector<string>>`，**以 WL 向量为键、BL 向量为值**（注释 L45-48 解释：WL 必须唯一，BL 数据可不唯一，故用 WL 当键）。这是「旧 flatten 写出」用的中间结构。注意还有一个**更快的**写出函数 `fast_write_memory_bank_flatten_fabric_bitstream_to_text_file`（4.2.3 已述），它直接用紧凑 `datas`/`masks`，绕过这个中间 map。

**shift_register 输出格式**：[openfpga/src/fpga_bitstream/memory_bank_shift_register_fabric_bitstream.cpp:58-85](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/memory_bank_shift_register_fabric_bitstream.cpp#L58-L85) —— `MemoryBankShiftRegisterFabricBitstream`。数据按「字」组织：`create_word()` 建一个 word（L58-71），再用 `add_bl_vectors(word, bl_vec)` / `add_wl_vectors(word, wl_vec)` 逐移位头追加（L73-85）。每个 word 对应一个配置周期，里面有若干 BL 移位头数据和 WL 移位头数据。L18-44 的 `bl_word_size`/`wl_word_size`/`bl_width`/`wl_width` 都用「取最后一个元素」快速获取尺寸，注释说明「尺寸一致性由 validator 保证」。

**写出端按子协议分派**：[openfpga/src/fpga_bitstream/write_text_fabric_bitstream.cpp:636-669](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/write_text_fabric_bitstream.cpp#L636-L669) —— ql_memory_bank 分支：BL 用 decoder → `write_memory_bank_fabric_bitstream_to_text_file`（逐位地址）；双 flatten → `fast_write_memory_bank_flatten_fabric_bitstream_to_text_file`（紧凑表，按 WL 行）；仅 BL 是 flatten → `write_memory_bank_flatten_fabric_bitstream_to_text_file`（中间 map）；BL 是 shift_register → `write_memory_bank_shift_register_fabric_bitstream_to_text_file`（字式）。把这段分派与 4.3.1 的表格对照，能立刻看清「子协议如何决定输出形态」。

shift_register 的实际写出见 [write_text_fabric_bitstream.cpp:444-494](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/write_text_fabric_bitstream.cpp#L444-L494)：先打印 word 总数、BL/WL 字尺寸、移位头位数，再逐 word 打印 `// BL part` 和 `// WL part`，每个移位头一行 0/1 串。

#### 4.3.4 代码实践

**实践目标**：对比 `qlbank`（双 decoder）与 `qlbankflatten`（双 flatten）两个 arch，看子协议如何改变「跳过译码器数」与「地址编码」。

**操作步骤**：

1. 打开 `openfpga_flow/openfpga_arch/k4_N4_40nm_qlbank_openfpga.xml` 与 `k4_N4_40nm_qlbankflatten_openfpga.xml`，对比它们的 `<configuration_protocol>` 段。`qlbank` 是 `<organization type="ql_memory_bank" .../>` 不带 `<bl>/<wl>` 子节点（默认 decoder），`qlbankflatten` 是：

   ```xml
   <organization type="ql_memory_bank" circuit_model_name="SRAM">
     <bl protocol="flatten"/>
     <wl protocol="flatten"/>
   </organization>
   ```

2. 阅读 [memory_utils.cpp:467-497](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/utils/memory_utils.cpp#L467-L497)，手算两个 arch 各跳过几个尾部子模块（预期：qlbank=2，qlbankflatten=0）。
3. 阅读 [build_fabric_bitstream_memory_bank.cpp:191-241](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/build_fabric_bitstream_memory_bank.cpp#L191-L241)，确认 `qlbank` 走「旧方式」（`set_bit_bl/wl_address` + `itobin_charvec` 二进制地址），`qlbankflatten` 走「新方式」（`set_memory_bank_info` 紧凑表）。

**需要观察的现象**：两个 arch 用的是同一份 VPR 架构、同一个基准设计、同一种 SRAM 存储模型，区别只在 BL/WL 驱动方式。

**预期结果**：`qlbank` 的 fabric 比特流每个位带二进制 BL/WL 地址（短），`qlbankflatten` 的 fabric 比特流按 WL 行输出全 BL 数据（one-hot 隐含在位置里）。具体输出对照「待本地验证」，但存储路径的差异可由源码直接确认。

#### 4.3.5 小练习与答案

**练习 1**：`qlbanksr`（BL/WL 都用 shift_register）在顶层会跳过几个尾部可配置子模块？为什么？

**参考答案**：0 个。memory_bank/ql_memory_bank 默认跳过 2（BL 译码器 + WL 译码器），但 shift_register 不占用顶层译码器子模块（移位寄存器不计入 configurable children），所以 BL、WL 各减 1，最终跳过 0 个。见 memory_utils.cpp:489-496。

**练习 2**：为什么紧凑 `datas`/`masks` 表示「能轻松支持 shift_register」，却只对「双 flatten」启用？

**参考答案**：flatten 与 shift_register 的地址本质上都是 one-hot（shift_register 只是串行移入 one-hot 字），所以「按 (region, wl) 存 BL 位图」的模型对两者都成立，写出时再决定是并行总线还是串行字。decoder 的地址是二进制压缩的，无法直接套用「每 WL 一行 BL 位图」的模型（一个二进制地址同时涉及多个 WL 的译码），所以双 decoder 时仍用逐位字符串地址。源码注释（build_fabric_bitstream_memory_bank.cpp:235-237）坦言 decoder 场景「需要进一步评估」。

## 5. 综合实践

**任务**：对同一个基准设计（`and2`），分别用 **cc（scan_chain）** 与 **bank（memory_bank）** 两种配置协议跑完整流程，对比 `write_fabric_bitstream` 的输出，亲手验证「bank 版如何体现 BL/WL 寻址」。

**操作步骤**：

1. 确认已 `source openfpga.sh`，且 `openfpga` 二进制已编译（见 u1-l3、u1-l4）。

2. 跑 cc 任务：

   ```bash
   run-task basic_tests/full_testbench/configuration_chain
   ```

   完成后 `goto-task` 进入最新 run 目录，找到 `fabric_bitstream.bit`（具体路径以实际 run 目录为准）。

3. 跑 bank 任务：

   ```bash
   run-task basic_tests/full_testbench/memory_bank
   ```

   同样找到它的 `fabric_bitstream.bit`。这个任务用的 arch 是 `k4_N4_40nm_bank_openfpga.xml`（`<organization type="memory_bank" circuit_model_name="SRAM"/>`，见 [task.conf:20](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/tasks/basic_tests/full_testbench/memory_bank/config/task.conf#L20)），协议是 CONFIG_MEM_MEMORY_BANK（双 decoder）。

4. 用 `head` 各看前几行，对比两个 `.bit` 文件的头部注释与每行结构。

**需要观察与解释的现象**：

- **cc（scan_chain）输出**：头部形如 `// Bitstream length: N` 与 `// Bitstream width (LSB -> MSB): <区域数>`，正文每行是若干区域的位拼成的 0/1 串，**没有任何地址**——因为链式协议靠「位在链上的物理位置」寻址，顺序即地址（且每个区域内部已反转）。对照写出函数 [write_text_fabric_bitstream.cpp:78-122](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/write_text_fabric_bitstream.cpp#L78-L122)。

- **bank（memory_bank, decoder）输出**：头部形如 `// Bitstream width (LSB -> MSB): <bl_address X bits><wl_address Y bits><data input 1 bits>`，正文每行是 **BL 地址 + WL 地址 + 1 位数据输入**。每行对应一个 (BL, WL) 存储单元，地址是二进制编码。这正是「bank 版体现 BL/WL 寻址」的直接证据。对照写出函数 [write_text_fabric_bitstream.cpp:132-196](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_bitstream/write_text_fabric_bitstream.cpp#L132-L196)。

- **位总数应相等**：两份比特流描述的是同一个 `and2` 设计的同一组配置位，只是组织方式不同。可以数一下两个文件去掉注释后的总位数是否一致（「待本地验证」具体数值）。

**进阶**（可选）：再跑一个 `basic_tests/full_testbench` 下使用 `qlbanksr` 或 `qlbankflatten` arch 的任务（可在 `openfpga_flow/tasks` 下搜索，或复制 memory_bank 任务改 `openfpga_arch_file` 指向 `k4_N4_40nm_qlbankflatten_openfpga.xml`），观察双 flatten 下输出变成「按 WL 行、每行全 BL 数据」、双 shift_register 下输出变成「word / BL part / WL part」的字式结构，印证 4.3 节的三态分派。

## 6. 本讲小结

- `build_fabric_bitstream` 是一次**只读重组**：沿模块层级 DFS，用「块名 == 实例名」把 device 比特流的配置位按物理访问顺序重排成 fabric 比特流，位值不重复存（只存 `ConfigBitId` 引用）。
- 重组方式由 `config_protocol.type()` 分派：standalone 直接收集、scan_chain 收集后区域内部反转、memory_bank/frame_based 额外算地址、ql_memory_bank 委托专用函数。
- memory_bank 下的 `FabricBitstreamMemoryBank` 用 `datas`/`masks` 两张 `(region, wl)` 位图紧凑存储，每个 `uint8_t` 装 8 个 BL，把「逐位存地址」的几十 GB 压缩到几 MB。
- 紧凑表示只对 ql_memory_bank 的「双 flatten」启用；只要 BL 或 WL 用 decoder/shift_register，就退回逐位字符串地址（二进制或 one-hot）。
- ql_memory_bank 的 BL/WL 可各自选 decoder/flatten/shift_register，子协议决定了「跳过几个尾部译码器子模块」与「输出形态」（逐位地址 / 按 WL 行 / 字式）。
- 教科书级命令依赖链 `repack → build_architecture_bitstream → build_fabric_bitstream → write_fabric_bitstream` 在本讲落地：前一步产 device 比特流，本步重组，下一步落盘。

## 7. 下一步学习建议

- **u7-l4（write_fabric_bitstream 与 fast_configuration）**：本讲多次提到写出函数，下一讲会完整讲 `write_fabric_bitstream` 的 plain_text/xml 格式差异，以及 `fast_configuration` 如何利用全局 set/reset 配合 `wls_to_skip` 跳过整行相同位——届时你会看到本讲的 `masks`/`datas` 如何被进一步用来判定「可跳过的 WL」。
- **u9-l1（存储器组与移位寄存器 Bank）**：本讲的 shift_register 变体只讲了「输出形态」，移位寄存器 bank 的物理组织（`MemoryBankShiftRegisterBanks`，每区域的 BL/WL bank、列/行共享）留待专家层。
- **延伸阅读**：想看清「tile 起始索引如何累加成全局 BL/WL」，可读 `openfpga/src/utils/memory_bank_utils.cpp`（`compute_memory_bank_regional_blwl_start_index_per_tile` 等函数），它们是 ql_memory_bank 地址计算的数学基础。
