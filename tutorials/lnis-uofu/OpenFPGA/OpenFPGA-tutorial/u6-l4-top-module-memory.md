# 顶层模块与存储器配置总线

## 1. 本讲目标

在上一讲（u6-l3）里，我们已经看到 `build_fabric` 如何自下而上地把 mux、lut、memory、grid、routing 等子模块一个个搭出来。这些子模块最终都要被「挂」到一个唯一的顶层模块 `fpga_top` 下，并且芯片里成千上万个配置位（config bit）还要被一根「配置总线」串起来，才能被外部编程。

学完本讲，你应当能够：

1. 说清 `build_top_module` 这个顶层组装器的完整步骤，以及它为什么把「统计配置位、加 SRAM 端口、连配置总线」放在「实例化所有子模块」之后。
2. 解释「可配置子模块（configurable children）」这条有序列表是怎么按物理坐标（蛇形走位）排出来的，以及它如何被切分成若干「配置区域（config region）」。
3. 掌握配置总线连接 `add_top_module_nets_memory_config_bus` 如何根据 `config_protocol` 分派到 scan_chain / memory_bank / frame_based 三条 CMOS 路径，并理解它们在「端口形状」和「连线拓扑」上的根本差异。
4. 认识 `ql_memory_bank` 在顶层如何为 BL、WL 各自独立选择 decoder / flatten / shift_register 子协议，以及配置区域之间为何要保持译码器尺寸一致以避免「寄生编程」。

---

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义）：

- **ModuleManager 的四要素**（u6-l1）：模块（ModuleId）、端口（ModulePortId）、子模块实例（instance 号）、网（ModuleNetId）。本讲会频繁出现 `add_child_module`（加实例）、`add_port`（加端口）、`create_module_source_pin_net` + `add_module_net_sink`（连一条网）这三个动作。
- **可配置子模块的两种类型**（u6-l1）：`logical`（承载逻辑位置，供 device 级比特流）与 `physical`（携带物理坐标与区域，供 fabric 级比特流）。本讲里 `e_config_child_type::PHYSICAL` 是绝对主角。
- **配置协议 configuration_protocol**（u3-l4）：`standalone` / `scan_chain` / `memory_bank` / `ql_memory_bank` / `frame_based` 五种类型，以及 `ql_memory_bank` 下的 BL/WL 子协议（`flatten` / `decoder` / `shift_register`）。这是本讲配置总线分派的唯一依据。
- **build_fabric 的自下而上顺序**（u6-l2 / u6-l3）：mux/lut/decoder/memory 必须先于 grid/routing 构建。本讲是这条链的最后一环——top。

几个本讲要用到的术语：

- **配置总线（configuration bus / config bus）**：把外部编程信号（时钟、地址、数据、使能等）从顶层模块端口一路分发到每一个配置存储器（SRAM/CCFF）的连线网络。不同协议下它的「形状」完全不同。
- **配置位（config bit）**：一个可编程存储单元里的一位，决定一个 mux 的选择、一个 LUT 的真值表等。
- **BL / WL**：Bit Line / Word Line，memory_bank 类协议里寻址存储器矩阵的两组线，类似 SRAM 的列地址与行地址。
- **配置区域（config region）**：把整个 fabric 的配置存储器切成若干独立编程序，每个区域有自己的 head/tail 或独立 BL/WL 译码电路，可并行编程以缩短配置时间。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `openfpga/src/fabric/build_top_module.cpp` | 顶层组装器 `build_top_module` 的入口，串起「建模块→实例化子模块→统计配置位→加 SRAM 端口→连配置总线→汇总全局端口」全流程。 |
| `openfpga/src/fabric/build_top_module_memory.cpp` | 顶层存储器组织的核心：编排可配置子模块、切分配置区域、按协议统计配置位数、加 SRAM 端口、连 CMOS 配置总线（scan_chain / 普通 memory_bank / frame_based）。 |
| `openfpga/src/fabric/build_top_module_memory_bank.cpp` | `ql_memory_bank` 专属：BL/WL 各自独立走 decoder / flatten / shift_register 子协议，含移位寄存器链模块的构建与连线。 |
| `openfpga/src/fabric/build_top_module_child_fine_grained_instance.cpp` | 在「细粒度（非 tile）」模式下实例化 grid/SB/CB，并在末尾触发 `organize_top_module_memory_modules`——这是可配置子模块列表的诞生地。 |
| `openfpga/src/utils/module_manager_memory_utils.cpp` | 子模块（非顶层）层面的 fabric key 加载与可配置子模块更新，是顶层 fabric key 加载逻辑的「向下延伸」版本。 |

> 本讲引用的永久链接基于当前 HEAD `a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a`。

---

## 4. 核心概念与源码讲解

### 4.1 build_top_module：顶层模块的组装流程

#### 4.1.1 概念说明

`fpga_top` 是整个 fabric 模块图的根：所有 grid、switch block、connection block、memory、译码器最终都是它的（直接或间接）子模块实例。`build_top_module` 就是这个根的「组装车间」。

它和上一讲的子模块构建器有一个本质区别：子模块构建器解决「这个模块内部长什么样」，而 `build_top_module` 解决「把这些模块摆到芯片坐标系里的哪个位置、它们之间怎么连线、外部怎么把配置位灌进来」。正因为要回答后两个问题，**统计配置位、添加 SRAM 端口、连接配置总线这三件事，必须在所有子模块实例化完成之后才能做**——因为只有实例化完了，才知道芯片里到底有多少个配置位、它们各自的端口叫什么。

#### 4.1.2 核心流程

`build_top_module` 的步骤（按源码顺序）：

1. **建模块并命名**：用 `generate_fpga_top_module_name()` 取名（即 `fpga_top`），`add_module` 加入 ModuleManager，标记用途为 `MODULE_TOP`。
2. **实例化子模块**：根据是否启用 `fabric_tile`，二选一：
   - 细粒度模式（`fabric_tile.empty()`）：`build_top_module_fine_grained_child_instances` 逐坐标摆 grid/SB/CB 并连数据通道，**末尾会编排可配置子模块列表**。
   - tile 模式：`build_top_module_tile_child_instances` 把已聚合的 tile 模块摆上去。
3. **可选：随机打乱可配置子模块**（`generate_random_fabric_key` 时），用于安全/打乱存储器布局。
4. **同步移位寄存器 bank 设置**（为 `ql_memory_bank` + shift_register 准备）。
5. **统计并添加 shared config bits 的保留 SRAM 端口**。
6. **按区域统计配置位数** → **添加顶层 SRAM 端口**（`add_top_module_sram_ports`）。
7. **连接配置总线**（`add_top_module_nets_memory_config_bus`），除非 `frame_view` 为真或没有可配置子模块。
8. **多 prog-clock 处理**（`num_prog_clocks() > 1` 时加专用网）。
9. **汇总全局端口**（`add_module_global_ports_from_child_modules`，须在配置总线之后，因为后者可能新增子模块）。

注意第 7 步有一个 `frame_view` 守卫：当用户只想快速看 fabric 结构、不关心可编程连线时，可跳过配置总线，省下大量建网开销。

#### 4.1.3 源码精读

入口签名很长，但都是「只读输入 + 可写 ModuleManager」。[build_top_module.cpp:48-62](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module.cpp#L48-L62) 定义了这个函数，其中 `config_protocol`、`sram_model`、`fabric_key`、`blwl_sr_banks` 是本讲反复出现的几个关键参数。

创建顶层模块并标记用途：

[build_top_module.cpp:67-75](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module.cpp#L67-L75) —— 注意 `set_module_usage(top_module, ModuleManager::MODULE_TOP)`，这行决定了该模块在网表生成、比特流生成时的「根」身份。

子模块实例化的二分支（细粒度 vs tile）：

[build_top_module.cpp:77-96](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module.cpp#L77-L96) —— 这一步不仅摆放子模块，还在内部完成了「可配置子模块列表」的编排（见 4.2）。

「统计配置位 → 加 SRAM 端口 → 连配置总线」这三步的顺序与守卫，是本讲最值得记的一处：

[build_top_module.cpp:121-152](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module.cpp#L121-L152) —— 先 `find_top_module_regional_num_config_bit` 得到每个区域的配置位数，再 `add_top_module_sram_ports` 据此开出顶层端口，最后 `add_top_module_nets_memory_config_bus` 把端口与子模块连起来。第 142 行的 `if (false == frame_view)` 守卫，以及第 143 行要求 `PHYSICAL` 类可配置子模块非空，才真正去连配置总线。

#### 4.1.4 代码实践

**实践目标**：理解 `frame_view` 选项如何影响顶层构建的「最后一步」。

**操作步骤**：

1. 打开 [build_top_module.cpp:142-152](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module.cpp#L142-L152)，确认配置总线连接被 `if (false == frame_view)` 包住。
2. 在仓库中搜索 `frame_view` 的来源——它由 `build_fabric` 命令的 `--frame_view` 选项传入（参见 u6-l2 讲过的 `build_fabric_template`）。
3. 用一个现有 task（如 `basic_tests/full_testbench/configuration_chain`）对比：分别在脚本里加 `build_fabric --frame_view on` 与 `build_fabric --frame_view off`，跑 `run-task`。

**需要观察的现象**：开启 `--frame_view` 时，生成的 `fpga_top` 网表里**不会出现** `ccff_head/ccff_tail`（或 BL/WL 地址端口）到各存储器之间的连线网，模块端口也可能更少；关闭时这些配置连线齐全。

**预期结果**：`--frame_view on` 构建更快、产物更小，但无法编程（仅用于快速查看结构）；`--frame_view off` 是默认的完整 fabric。

> 若本地尚未编译 `openfpga` 二进制，相关现象标注为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `add_top_module_sram_ports` 必须在子模块实例化之后调用，而不能在 `build_top_module` 一开头就调？

**参考答案**：顶层 SRAM 端口的位宽（如 BL/WL 地址宽度、ccff head/tail 是否多位）取决于芯片里实际配置位的数量与组织方式，而这些信息只有把所有 grid/SB/CB 子模块实例化、并统计完它们各自的配置位数之后才能确定。一开头调会因为「还没有可配置子模块」而算出 0 位。

**练习 2**：第 9 步 `add_module_global_ports_from_child_modules` 的注释特意写明「须在 `add_top_module_nets_memory_config_bus()` 之后，因为后者可能新增子模块」。这是指哪类子模块？

**参考答案**：指配置总线构建过程中**新加入顶层的译码器模块**——memory_bank 会追加 BL/WL 译码器、frame_based 会追加帧译码器、ql_memory_bank 的 shift_register 会追加移位寄存器链模块。这些新子模块可能带全局端口（如编程时钟），必须在它们存在后再汇总。

---

### 4.2 可配置子模块编排与配置区域（configurable regions）

#### 4.2.1 概念说明

「配置总线」要连的对象，是一个被精心排序的列表——**可配置子模块列表（configurable children）**。它的每一项是顶层模块下的一个子模块实例（一个 grid、一个 SB、一个 CB，或它们的 tile 聚合体），且该实例必须含有配置位。

这个列表的顺序非常重要：scan_chain 协议下，比特流就是按这个顺序一位位移进来的，顺序错了配置就全错；即便 memory_bank 协议不依赖物理顺序，列表顺序也决定了 fabric key 与比特流的稳定可复现性。

**配置区域（config region）** 则是把这个列表切成若干段（由 `config_protocol.num_regions()` 决定），每段共享一套独立的编程电路（独立 head/tail 或独立 BL/WL 译码器）。多区域可以并行编程，大幅缩短配置时间。

#### 4.2.2 核心流程

`organize_top_module_memory_modules`（无 fabric key 时）的编排策略是一个**外围蛇形 + 核心蛇形**的双层遍历：

1. **外围 I/O**：按 BOTTOM → RIGHT → TOP → LEFT 四条边依次遍历，每条边内部按确定方向（如 BOTTOM 从左到右、LEFT 从上到下）推进，目的是让相邻 I/O 在列表里也相邻，缩短 scan_chain 的连线长度。
2. **核心 tile**：从底行开始，逐行蛇形（第 0 行从左到右、第 1 行从右到左……），同样为了减少跨行长线。
3. 每个 tile 内部固定顺序：**SB → CBX → CBY → Grid**（缺哪块就跳过）。
4. 最后调用 `build_top_module_configurable_regions` 把整条列表均匀切成 `num_regions()` 段。

坐标到「配置坐标」的映射用一个把 (x,y) 拉伸 2 倍的网格，让 Grid/SB/CBX/CBY 各占一个不冲突的位置：

- Grid：\((2x, 2y)\)
- SB：\((2x+1, 2y+1)\)
- CBX：\((2x, 2y+1)\)
- CBY：\((2x+1, 2y)\)

配置区域的均分有一个细节：在统计待分配子模块数时，memory_bank / ql_memory_bank 会**预先扣除 2**（对应稍后追加的 BL、WL 译码器），frame_based 扣除 1（帧译码器），保证译码器追加进列表尾端时刚好落入最后一个区域而不破坏均分。

#### 4.2.3 源码精读

单个 tile 内把 SB/CBX/CBY/Grid 注册为可配置子模块，并带上配置坐标：

[build_top_module_memory.cpp:170-180](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory.cpp#L170-L180)（SB 分支，注意 `config_coord` 取 \((2x+1, 2y+1)\)）与 [build_top_module_memory.cpp:222-229](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory.cpp#L222-L229)（Grid 分支，`config_coord` 取 \((2x, 2y)\)）。这里用 `e_config_child_type::UNIFIED` 注册——表示 logical 与 physical 视图合一。

> 源码函数顶部的 ASCII 图（[build_top_module_memory.cpp:95-132](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory.cpp#L95-L132)）画出了 Grid/CBX/CBY/SB 在 2 倍坐标网格里的相对位置，建议读者打开对照。

配置区域的均分逻辑，以及译码器占位的扣除：

[build_top_module_memory.cpp:287-302](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory.cpp#L287-L302) —— 关键是 `num_children_per_region = num_configurable_children / config_protocol.num_regions()`，其中 `num_configurable_children` 已按协议扣除了将要追加的译码器数量。

均分后还有一个断言校验区域数与定义一致：

[build_top_module_memory.cpp:338-341](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory.cpp#L338-L341)。

外围 + 核心的蛇形遍历主体：

[build_top_module_memory.cpp:456-538](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory.cpp#L456-L538) —— 注意 `positive_direction` 在每行结束后翻转，实现核心 tile 的蛇形走位。

无 fabric key 走 `organize_top_module_memory_modules`、有 fabric key 走 `load_top_module_memory_modules_from_fabric_key` 的分流发生在子模块实例化函数末尾：

[build_top_module_child_fine_grained_instance.cpp:525-562](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_child_fine_grained_instance.cpp#L525-L562) —— 这也解释了为什么 `build_top_module` 本身看不到 `organize_*` 调用：它在更内层的子模块实例化函数里完成了。

#### 4.2.4 代码实践

**实践目标**：用 fabric key 覆盖默认编排，观察可配置子模块顺序的可定制性。

**操作步骤**：

1. 阅读 `load_top_module_memory_modules_from_fabric_key`：[build_top_module_memory.cpp:613-702](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory.cpp#L613-L702)。注意它先 `clear_configurable_children`，再按 fabric key 里 region/key 的顺序逐一 `add_configurable_child`。
2. 在一个已跑通的任务上，先用 `build_fabric` 生成默认 fabric，再用 `write_fabric_key` 导出 `fabric_key.xml`。
3. 把 `fabric_key.xml` 中若干 key 的顺序人为调换，用 `build_fabric --load_fabric_key <改后的key>` 重建 fabric。

**需要观察的现象**：重建后的 `fpga_top` 网表里，存储器实例化的物理位置不变，但 scan_chain（若用 cc 协议）的 head→tail 串接顺序、以及生成的 fabric 比特流中位的排列顺序会随之改变。

**预期结果**：fabric key 是「同构 fabric、不同比特流映射」的开关，证实可配置子模块的顺序可由外部完全指定。

> 若本地未编译，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：把 (x,y) 坐标统一乘 2 再加偏移得到配置坐标，好处是什么？

**参考答案**：让 Grid、SB、CBX、CBY 四类在逻辑上同处一个网格但各占不同奇偶位置（Grid 占偶偶、SB 占奇奇、CBX 占偶奇、CBY 占奇偶），互不冲突。这样一个二维整数坐标就能唯一、紧凑地编码任何一个可配置子模块的物理位置，供 memory_bank 的 BL/WL 分配、fabric key 的坐标标注等下游使用。

**练习 2**：`organize_top_module_memory_modules` 开头有一个断言，要求进入时顶层的 PHYSICAL 可配置子模块列表必须为空（[build_top_module_memory.cpp:450-454](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory.cpp#L450-L454)）。为什么必须为空？

**参考答案**：因为它要从零按蛇形顺序重建整条列表。如果进入时已有残留的子模块，会造成重复注册、顺序错乱。这个断言保证了「编排」是一次性的、确定性的动作。

---

### 4.3 配置总线连接：add_top_module_nets_memory_config_bus

#### 4.3.1 概念说明

有了顶层 SRAM 端口和排好序的可配置子模块列表后，还差最后一步：**用网（ModuleNet）把端口和子模块实际连起来**。这就是配置总线连接。它的难点在于：不同 `config_protocol` 下，「端口形状」和「连线拓扑」完全不同：

- **scan_chain**：一条（或多条，按区域）首尾相连的移位链。head 进、tail 出，子模块之间 tail→head 串成糖葫芦。
- **memory_bank**：BL/WL 两组译码器，译码器输出扇出到每个子模块的 BL/WL 端口，类似 SRAM 矩阵寻址。
- **frame_based**：一个帧译码器，用地址选通某一个子模块的使能，数据线广播给所有子模块。

所以这个函数的核心是一个**两级 switch 分派**：先按存储器设计工艺（CMOS / RRAM）分，再在 CMOS 分支里按 `config_protocol.type()` 分。目前 RRAM 分支是空的 `TODO`，实际生效的只有 CMOS 路径。

#### 4.3.2 核心流程

分派树如下（CMOS 路径）：

```
add_top_module_nets_memory_config_bus(mem_tech, config_protocol)
 └─ CMOS → add_top_module_nets_cmos_memory_config_bus
     ├─ standalone      → 直接把 BL/WL 扁平连到每个子模块
     ├─ scan_chain      → 链式串接 head→...→tail（按区域各一条）
     ├─ memory_bank     → 建 BL/WL 译码器并扇出
     ├─ ql_memory_bank  → 见 4.4（BL/WL 子协议独立选择）
     └─ frame_based     → 建帧译码器；1 个子模块时短路直连，否则译码
```

scan_chain 链式连接的「寻址」不需要地址，只需要顺序：第 0 个子模块的 head 来自顶层 head 端口，之后每个子模块的 head 来自上一个的 tail，最后一个子模块的 tail 回到顶层 tail 端口。多区域时，每个区域独立一条链，head/tail 端口的第 *r* 位对应第 *r* 个区域。

memory_bank 与 frame_based 都要建译码器，译码器地址位宽由数据输出数决定：

\[
\text{addr\_size} = \lceil \log_2 N \rceil
\]

其中 \(N\) 是译码器的数据输出数（memory_bank 里是 num_bls 或 num_wls，frame_based 里是可配置子模块数）。memory_bank 为了让 BL、WL 数量均衡，通常取：

\[
\text{num\_bls} = \text{num\_wls} = \lceil \sqrt{M} \rceil
\]

其中 \(M\) 是该区域的配置位总数，这样总编程步数约为 \(2\lceil\log_2\sqrt{M}\rceil\)，远少于 scan_chain 的 \(M\) 步。

#### 4.3.3 源码精读

最外层的两级分派——先工艺、后协议：

[build_top_module_memory.cpp:2044-2066](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory.cpp#L2044-L2066) 是入口；内层 CMOS 分派见 [build_top_module_memory.cpp:1974-2011](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory.cpp#L1974-L2011)。

scan_chain 的链式串接：第 0 个子模块的 head 来自顶层 head 端口的第 `config_region` 位：

[build_top_module_memory.cpp:1439-1458](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory.cpp#L1439-L1458)；其余子模块 head 来自上一个的 tail；最后一个子模块的 tail 回到顶层 tail 端口：[build_top_module_memory.cpp:1507-1527](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory.cpp#L1507-L1527)。注意 `net_src_pin_id = size_t(config_region)`——这就是「每个区域独立一条链」的来源。

普通 memory_bank 的译码器构建与扇出：先按区域建 BL 译码器（`find_decoder` 复用 / `add_decoder` 新建），实例化进顶层，再把译码器数据输出连到每个子模块的 BL 端口：

[build_top_module_memory.cpp:1131-1162](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory.cpp#L1131-L1162)（建 BL 译码器并实例化）与 [build_top_module_memory.cpp:1273-1313](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory.cpp#L1273-L1313)（译码器输出 → 子模块 BL 端口）。连完所有常规子模块后，BL/WL 译码器自身被追加为可配置子模块：[build_top_module_memory.cpp:1365-1391](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory.cpp#L1365-L1391)。

frame_based 的二分策略——区域里只有 1 个子模块且地址位宽正好匹配时短路直连，否则建帧译码器：

[build_top_module_memory.cpp:1903-1927](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory.cpp#L1903-L1927)。帧译码器的高位地址对齐到顶层地址的 MSB，注释 [build_top_module_memory.cpp:1636-1649](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory.cpp#L1636-L1649) 解释了这是为避免多区域地址碰撞。

#### 4.3.4 代码实践

**实践目标**：对比 scan_chain 与 memory_bank 两种协议在顶层配置总线连接上的差异（本讲的核心实践任务）。

**操作步骤**：

1. 打开 scan_chain 连线函数 [build_top_module_memory.cpp:1419-1548](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory.cpp#L1419-L1548)，数一数它「新建子模块（译码器/移位链）」的次数：答案是 0，它只连网。
2. 打开 memory_bank 连线函数 [build_top_module_memory.cpp:1083-1392](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory.cpp#L1083-L1392)，数一数它新建子模块的次数：每个区域建 1 个 BL 译码器 + 1 个 WL 译码器。
3. 分别用 `cc`（scan_chain）和 `bank`（memory_bank）两套 `openfpga_arch`（如 `k4_N4_40nm_cc_openfpga.xml` 与 `k4_N4_40nm_bank_openfpga.xml`）跑同一个 `and2` 基准。

**需要观察的现象**：

| 维度 | scan_chain (cc) | memory_bank (bank) |
| --- | --- | --- |
| 顶层新增端口 | `ccff_head` / `ccff_tail`（按区域多位） | `en` / `bl_addr` / `wl_addr` / `din` |
| 新增子模块 | 无 | 每区域 1 个 BL 译码器 + 1 个 WL 译码器 |
| 子模块间连线 | tail→head 链式串接 | 译码器输出扇出到各 BL/WL |
| 编程步数（量级） | \(O(M)\)（每个配置位 1 拍） | \(O(\log M)\)（地址寻址） |

**预期结果**：cc 版网表里 `fpga_top` 内部能看到一条长长的 `ccff_head→...→ccff_tail` 串行链；bank 版则能看到 `bl_decoder` / `wl_decoder` 实例，且 `sram` 的 BL/WL 端口按矩阵方式被寻址。

> 现象中「编程步数」一项需要配合 testbench 仿真才能精确观测，标注「待本地验证」；前两项可直接读生成的 `fpga_top.v` 确认。

#### 4.3.5 小练习与答案

**练习 1**：scan_chain 的链式连接函数里，为什么第 0 个子模块和「最后一个子模块」要单独处理？

**参考答案**：第 0 个子模块的 head 来自**顶层 head 端口**（而非上一个子模块的 tail，因为它没有上一个）；最后一个子模块的 tail 要回到**顶层 tail 端口**（而非下一个子模块的 head，因为它没有下一个）。中间所有子模块统一为「上一个 tail → 自己 head」。这两端是链的边界，必须特判。

**练习 2**：memory_bank 在把译码器追加为可配置子模块时，用的是 `e_config_child_type::PHYSICAL`（[build_top_module_memory.cpp:1370-1372](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory.cpp#L1370-L1372)），而不是 UNIFIED。为什么？

**参考答案**：译码器是纯物理配置电路，只在 fabric 级比特流里有意义（它自己也存了固定的使能/地址译码逻辑相关位），在 device 级（与 fabric 无关的）比特流模型里不应出现。用 PHYSICAL 表示它「仅属于物理视图」，与 u6-l1 讲过的 logical/physical 区分一致。

---

### 4.4 memory bank 顶层组织与 BL/WL 子协议

#### 4.4.1 概念说明

`ql_memory_bank`（QuickLogic memory bank）是普通 `memory_bank` 的增强版。它的关键特征是：**BL 和 WL 可以各自独立选择子协议**——decoder、flatten、shift_register 三选一，且 BL 和 WL 的选择互不依赖。这带来了 9 种组合（3×3），远比普通 memory_bank 灵活。

- **decoder**：和 4.3 的普通 memory_bank 一样，用地址译码器寻址，引脚最少。
- **flatten**：完全不要译码器，每个 BL/WL 直接拉到顶层端口。引脚极多，但编程最快、电路最简单。
- **shift_register**：用一条移位寄存器链串行加载 BL（或 WL），再并行输出到各条 BL/WL 线。介于两者之间——引脚少、但需要多拍移位。

另一个贯穿本模块的关键约束是**「避免寄生编程」**：多个配置区域在同一个编程时钟周期被编程，若各区域的译码器尺寸不同，给 A 区编程时的高位地址可能在 B 区也命中某个单元，造成误写。因此 OpenFPGA 强制**所有区域的 BL/WL 译码器尺寸取各区域的最大值（统一尺寸）**，地址从 LSB 对齐，未用高位留空。

#### 4.4.2 核心流程

`add_top_module_nets_cmos_ql_memory_bank_config_bus` 的分派是两个**独立**的 switch：

```
按 bl_protocol_type 分派 BL 电路：
  DECODER       → 建 BL 译码器，扇出到子模块 BL
  FLATTEN       → 顶层 BL 端口直连子模块 BL
  SHIFT_REGISTER→ 建移位寄存器链模块，串行加载→并行输出到 BL

按 wl_protocol_type 分派 WL 电路：（同上三种，独立选择）
```

shift_register 子协议的处理最复杂，分四步：

1. 找出所有「唯一的移位链长度」，为每个唯一长度建一个移位寄存器链子模块（复用）。
2. 在顶层实例化这些链模块（每个 bank 一个实例）。
3. 预计算每行/列的 BL/WL 起始下标，把每个子模块的每个 BL/WL pin 反查到驱动它的 bank 与数据端口，记录 sink 关系。
4. 连 head（顶层→bank）、tail（bank→顶层）、以及 BL/WL 输出（bank→子模块）。

移位链模块内部本身又是一条 scan_chain：head 进、若干 CCFF 串接、tail 出，每个 CCFF 的 BL（或 WL）输出拉到模块边界端口。

#### 4.4.3 源码精读

两个独立 switch 的分派入口：

[build_top_module_memory_bank.cpp:1983-2038](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory_bank.cpp#L1983-L2038) —— 注意 BL 的 switch 和 WL 的 switch 是并列的，BL 选 decoder、WL 选 shift_register 完全合法。

「统一译码器尺寸避免寄生编程」的注释与实现，以普通 memory_bank 为例（ql 的 decoder 路径同理）：

[build_top_module_memory.cpp:1116-1128](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory.cpp#L1116-L1128) —— `num_bls`/`num_wls` 取所有区域的 `std::max`；详细反例（Bank A 36 单元、Bank B 16 单元误写）画在 [build_top_module_memory.cpp:1024-1048](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory.cpp#L1024-L1048)。

shift_register 子协议下，为每个唯一链长度构建移位链子模块并在顶层实例化：

[build_top_module_memory_bank.cpp:1733-1760](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory_bank.cpp#L1733-L1760)。移位链模块本身的内部结构（head/tail/BL_OUT 端口 + CCFF 串接）见 `build_bl_shift_register_chain_module`：[build_top_module_memory_bank.cpp:196-288](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory_bank.cpp#L196-L288)。

> 注意此处模块用途被标为 `MODULE_CONFIG`（[build_top_module_memory_bank.cpp:221](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory_bank.cpp#L221)），与 u6-l1 讲过的「配置存储器与译码器归 MODULE_CONFIG」一致。

各子协议对应的**顶层端口**在 `add_top_module_ql_memory_bank_sram_ports` 里同样按 BL/WL 独立 switch 添加：

[build_top_module_memory_bank.cpp:2069-2216](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory_bank.cpp#L2069-L2216) —— 例如 decoder 加 `en/bl_addr/din`，flatten 加每区域独立的 `bl_<region>` 端口，shift_register 加每区域的 `bl_sr_head_<region>` / `bl_sr_tail_<region>` 端口。

bank 数量与每 bank 数据端口的「均分」策略（无 fabric key 时）：

[build_top_module_memory_bank.cpp:2293-2320](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory_bank.cpp#L2293-L2320) —— 按 `config_protocol.bl_num_banks()` 把区域内的 BL 平均切给若干 bank，最后一个 bank 吃余数。

#### 4.4.4 代码实践

**实践目标**：体会 BL/WL 子协议独立选择带来的端口形状差异。

**操作步骤**：

1. 在 `openfpga_flow/openfpga_arch/` 下找到三套 qlbank 系列 arch：`qlbank`（decoder）、`qlbankflatten`（flatten）、`qlbanksr`（shift_register）。打开它们的 `configuration_protocol` 段，对比 `<bl>` / `<wl>` 子节点的 `type` 属性（参见 u3-l4）。
2. 对照 [build_top_module_memory_bank.cpp:2069-2216](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory_bank.cpp#L2069-L2216)，逐个 arch 预测 `fpga_top` 会出现哪些端口。
3. 分别跑这三个 arch（若仓库提供对应 task，如 `basic_tests` 下相关用例），检查生成的 `fpga_top.v` 端口列表。

**需要观察的现象**：

| arch | BL 子协议 | WL 子协议 | 预期顶层端口 |
| --- | --- | --- | --- |
| qlbank | decoder | decoder | `en`, `bl_addr`, `wl_addr`, `din` |
| qlbankflatten | flatten | flatten | 每区域 `bl_<r>`, `wl_<r>`（位宽=区域 BL/WL 数） |
| qlbanksr | shift_register | shift_register | 每区域 `bl_sr_head_<r>`/`bl_sr_tail_<r>` + WL 同理 |

**预期结果**：端口形状与上表一致；flatten 版端口位宽最大、shift_register 版端口数最多但每位只 1 bit、decoder 版端口最紧凑。

> 端口形状可直接读 `fpga_top.v` 验证；具体 arch 文件名与 task 是否可用标注「待本地确认」。

#### 4.4.5 小练习与答案

**练习 1**：为什么「统一译码器尺寸」能避免寄生编程？用 4.4.1 里 Bank A/B 的例子说明。

**参考答案**：若 A 区译码器 3 位地址（6 输出）、B 区 2 位（4 输出），给 A 区写地址 `3'b110` 时，B 区译码器只看低 2 位 `2'b10`，会误选中 B 区第 2 根线，若此时数据线也被驱动就会误写 B 区。统一成 3 位地址后，B 区也有 6 输出（多余的空着），地址 `3'b110` 在两区指向相同编号的线，B 区那条线要么不存在要么不被该数据驱动，从而避免寄生写。代码里用 `std::max` 取各区域最大值来实现统一。

**练习 2**：shift_register 子协议下，移位链模块内部为什么也要 `add_configurable_child` 把每个 CCFF 注册进去（[build_top_module_memory_bank.cpp:259-261](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory_bank.cpp#L259-L261)）？

**参考答案**：移位链里的 CCFF 本身也是配置存储器——它们存的不是用户电路的配置位，而是「正在被移位加载的 BL/WL 数据」。把这些 CCFF 注册为可配置子模块，才能在 fabric 级比特流里正确表达「需要往某条移位链移入什么数据序列」，并保证比特流生成（u7）能覆盖到它们。

---

## 5. 综合实践

**综合任务**：用一张完整的「配置总线对比表 + 流程图」把本讲四个模块串起来，并验证你最关心的一种协议。

1. **画流程图**：以 `build_top_module` 为起点，画出从「实例化子模块 → organize 可配置子模块 → 切分配置区域 → 统计配置位 → 加 SRAM 端口 → 连配置总线」的数据流，标注每一步读写的 ModuleManager 部分（可配置子模块列表、区域、端口、网）。
2. **填对比表**：选 scan_chain、memory_bank、frame_based、ql_memory_bank(shift_register) 四种协议，分别填出：顶层新增端口、是否新增子模块（译码器/移位链）、子模块间连线拓扑、配置区域如何独立、编程步数量级。
3. **验证一种**：挑一种协议（推荐 cc 或 bank），用对应 arch 跑 `and2`，打开生成的 `fpga_top.v`，在网表里**亲手定位**配置总线相关的端口与连线实例，与你画的流程图/表格对账。

**验收标准**：

- 流程图能解释「为什么配置总线必须最后连」。
- 对比表能说清 scan_chain 的链式串接与 memory_bank 的译码器扇出在源码里的不同函数。
- 能在真实网表里指出至少一个由本讲代码生成的连线（如 cc 版的某条 `ccff_head` 连线，或 bank 版的 `bl_decoder` 实例）。

> 若本地未编译 OpenFPGA，第 3 步可用「源码阅读型验证」替代：在 [build_top_module_memory.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_top_module_memory.cpp) 里跟踪某一协议的连线函数，逐行说明每条 `create_module_source_pin_net` + `add_module_net_sink` 连的是哪两个端口。

---

## 6. 本讲小结

- `build_top_module` 是顶层组装器：建 `fpga_top` → 实例化子模块 → 统计配置位 → 加 SRAM 端口 → 连配置总线 → 汇总全局端口；后三步必须在子模块实例化之后，因为它们依赖于「芯片里到底有多少配置位」。
- 可配置子模块列表是配置总线的作用对象，按「外围 I/O 蛇形 + 核心 tile 蛇形 + tile 内 SB→CBX→CBY→Grid」的确定顺序编排，用 2 倍坐标网格给每项一个唯一物理坐标；再均匀切成 `num_regions()` 个配置区域，并预留译码器占位。
- `add_top_module_nets_memory_config_bus` 是两级 switch（工艺 → 协议）：scan_chain 只连网不建模块、memory_bank/frame_based 会新建译码器，三者端口形状与连线拓扑迥异。
- `ql_memory_bank` 让 BL、WL 各自独立选 decoder/flatten/shift_register，共 9 种组合；shift_register 用移位链模块串行加载、并行输出。
- 跨区域统一译码器尺寸（取各区域最大值）是避免「寄生编程」的关键约束，地址从 LSB 对齐。
- `frame_view` 选项可跳过配置总线连接，用于快速查看 fabric 结构；fabric key 可完全覆盖默认的可配置子模块顺序。

---

## 7. 下一步学习建议

- **向下游——比特流生成（u7）**：本讲连好的配置总线是 u7「两级比特流模型」的物理基础。建议先读 u7-l1 的 `BitstreamManager` / `FabricBitstream`，再回看本讲的「可配置子模块 PHYSICAL 列表 + 配置区域」，理解比特流里的位为何要按本讲确定的顺序与区域组织。
- **深入 memory_bank shift register（u9-l1）**：本讲只讲到移位链模块的构建与顶层连线，`MemoryBankShiftRegisterBanks` 这个数据结构如何组织每区域的 BL/WL bank、列共享/行共享优化，是 u9-l1 的主题。
- **fabric key 全流程（u6-l5）**：本讲的 `load_top_module_memory_modules_from_fabric_key` 与 `load_top_module_shift_register_banks_from_fabric_key` 只是 fabric key 的「消费端」，u6-l5 会讲 fabric key 的写出与设计意图（安全、可复现）。
- **源码延伸阅读**：`openfpga/src/utils/module_manager_memory_utils.cpp` 里子模块层面的 fabric key 加载逻辑（`load_submodules_memory_modules_from_fabric_key`），是本讲顶层加载逻辑向 grid/SB/CB 内部的递归延伸，值得对照阅读。
