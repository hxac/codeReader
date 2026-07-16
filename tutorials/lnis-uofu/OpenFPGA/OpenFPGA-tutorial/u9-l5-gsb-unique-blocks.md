# GSB 压缩与 Unique Blocks

## 1. 本讲目标

在前面的单元里（u5-l4、u6-l3），我们已经知道 OpenFPGA 在 link 阶段会把 VPR 布好的 RR graph 收集成一张「设备级通用开关块」标注 `DeviceRRGSB`，并在 build_fabric 阶段用它来实例化 SB/CB 模块。一颗大阵列可能有成千上万个坐标，但绝大多数 GSB 的内部结构是**完全相同**的。如果每个坐标都生成一个独立的 Verilog 模块，网表数量会爆炸、后端 PnR 也会被淹没。

本讲要解决的就是「**怎么把这些重复的 GSB 识别出来、压成少量 unique 模块，并把结果缓存到磁盘加速下次构建**」。学完本讲，你应当能够：

- 说清 `DeviceRRGSB` 内部如何用「镜像（mirror）判定」把整张 GSB 阵列去重为少量 unique 模块，并理解「GSB = SB + CBX + CBY 三个 unique id 全相等」这一汇总准则。
- 解释 `--compress_routing` 选项与 `is_compressed_` 标志如何联动，让 build_fabric 既能现场压缩、也能跳过已压缩的结果。
- 读懂 `read_unique_blocks` / `write_unique_blocks` 两条命令如何用 XML（人可读）或二进制（capnp）把 unique 块缓存落盘与回灌，从而把昂贵的镜像比较变成廉价的 preload。
- 说清「GSB 版本（V1/V2）」从哪里来——它现在**不再是一条命令选项**，而是由 VPR device context 在 RR graph 生成阶段写入、link 阶段读出，并用 `gsb_version_set_` 守卫防止「未设置即读取」。

本讲是 u9（高级机制）的收尾之一：u9-l4 讲了 tile 直连与 fabric tile 的「聚合」，本讲则讲布线侧的「去重」，两者共同决定了大阵列下网表与模块的规模。

## 2. 前置知识

- **DeviceRRGSB（u5-l4）**：OpenFPGA 自有的设备级结构，它为每个 `(x,y)` 坐标收集一个 `RRGSB`（= 该坐标的 Switch Block + 两条 Connection Block CBX/CBY 的视图），是 fabric 构建时实例化 SB/CB 模块的依据。本讲全程在讨论它的内部如何去重。
- **annotation 模式（u5-l4）**：OpenFPGA 不侵入 VPR 源码，而是在自己的 context 上挂「副表」。`DeviceRRGSB` 正是这样一个挂在 `OpenfpgaContext` 上的设备级标注，由 mutable 访问器写入、const 访问器读取。
- **build_fabric 调用链（u6-l2、u6-l3）**：`build_device_module_graph()` 自下而上构建模块；其中 routing 模块（SB/CB）的构建分 **unique（压缩）** 与 **flatten（全坐标）** 两条入口，选哪条由 `is_compressed()` 决定。这正是本讲要打开的开关。
- **命令依赖机制（u2-l2）**：命令在注册时可声明对前置命令的依赖，shell 会在执行前检查。本讲的 `read_unique_blocks` 就声明了对 `link_openfpga_arch` 的依赖。
- **OpenfpgaContext（u2-l3）**：`device_rr_gsb_` 是其中一个分区，`read_unique_blocks` 写它、`build_fabric` 读它的 `is_compressed()`。

> 一个贯穿全讲的关键直觉：**unique 压缩是「算法 + 缓存」两件事**。算法是「逐坐标两两比较，找镜像」；缓存是「把找好的镜像清单写到磁盘，下次直接 preload」。OpenFPGA 把这两件事解耦成 `build_unique_module()`（算）和 `read_unique_blocks()`（直接装），后者正是大阵列加速的关键。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [openfpga/src/annotation/device_rr_gsb.h](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/device_rr_gsb.h) | `DeviceRRGSB` 类声明：unique 模块列表、镜像 id 矩阵、`is_compressed_` 标志、GSB 版本成员与 `gsb_version_set_` 守卫。 |
| [openfpga/src/annotation/device_rr_gsb.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/device_rr_gsb.cpp) | 镜像识别（`build_sb_unique_module` / `build_cb_unique_module` / `build_gsb_unique_module`）、preload 函数、`get/set_gsb_version` 的实现。 |
| [openfpga/src/annotation/annotate_rr_graph.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/annotate_rr_graph.cpp) | `annotate_device_rr_gsb()`：遍历每个坐标构建 `RRGSB`，按 GSB 版本选择 V1（`build_rr_gsb`）或 V2（`build_rr_gsb2`，side-agnostic）路径。 |
| [openfpga/src/base/openfpga_link_arch_template.h](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/base/openfpga_link_arch_template.h) | `link_arch_template`：**从 VPR device context 读出 GSB 版本**（不再是命令选项），并把版本传给 `annotate_device_rr_gsb`。 |
| [openfpga/src/base/openfpga_build_fabric_template.h](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/base/openfpga_build_fabric_template.h) | `compress_routing_hierarchy_template`（现场压缩）、build_fabric 里用 `is_compressed()` 守门，以及 `read/write_unique_blocks_template`。 |
| [openfpga/src/annotation/read_unique_blocks_xml.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/read_unique_blocks_xml.cpp) | `read_xml_unique_blocks()`：解析 `<unique_blocks>` XML，分派到 preload 函数，跳过镜像比较。 |
| [openfpga/src/annotation/write_unique_blocks_xml.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/write_unique_blocks_xml.cpp) | `write_xml_unique_blocks()`：把 unique 模块及其所有镜像实例坐标写出为 XML。 |
| [openfpga/src/utils/device_rr_gsb_utils.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/utils/device_rr_gsb_utils.cpp) | `find_device_rr_gsb_num_*_modules()`：统计压缩前的「总模块数」，用于计算压缩率报告。 |
| [openfpga/src/base/openfpga_setup_command_template.h](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/base/openfpga_setup_command_template.h) | `read_unique_blocks` / `write_unique_blocks` 命令的注册（选项、类别、依赖）。 |

辅助参考：`read_unique_blocks_xml.h` / `write_unique_blocks_xml.h` 仅是上述实现的头文件声明；另有 `read_unique_blocks_bin.*` / `write_unique_blocks_bin.*` 提供二进制（capnp）格式的等价读写。`write_xml_device_rr_gsb.h` 声明的 `write_device_rr_gsb_to_xml()` 是另一回事——它把**每个坐标的 GSB 逐个** dump 成 XML，用于人工调试单个 GSB，**不**做去重，注意与 unique blocks 缓存区分。

## 4. 核心概念与源码讲解

### 4.1 DeviceRRGSB：设备级 GSB 标注的数据骨架

#### 4.1.1 概念说明

`DeviceRRGSB` 是 OpenFPGA 在 `OpenfpgaContext` 里持有的「整张芯片的 GSB 总账」。它要回答两个问题：

1. **每个坐标 (x,y) 上，GSB 长什么样？** —— 由 `rr_gsb_[x][y]`（一个 `RRGSB` 对象）和与之平行的 `rr_gsb_edges_[x][y]`（保存做镜像比较所需的入边信息）记录。
2. **每个坐标的 GSB，是哪个 unique 模块的镜像（实例）？** —— 由三张「id 矩阵」记录：`sb_unique_module_id_`、`cbx_unique_module_id_`、`cby_unique_module_id_`，每张矩阵给出「该坐标 → 它属于第几个 unique 模块」的映射；同时用三个向量 `sb_unique_module_` / `cbx_unique_module_` / `cby_unique_module_` 记录「每个 unique 模块的代表坐标」。

一句话：**`rr_gsb_` 存「内容」，三个 `*_unique_module_id_` 存「归类」**。压缩（build）或回灌（read）完成后，`is_compressed_` 置为 `true`，表示「归类已完成，可以按 unique 模块来实例化模块了」。

#### 4.1.2 核心流程

`DeviceRRGSB` 的生命周期分四步：

```text
1. reserve(gsb_range)            // 按 (width-1)×(height-1) 预分配 rr_gsb_ 二维数组
2. add_rr_gsb(coord, rr_gsb)     // link 阶段逐坐标填入 RRGSB（annotate_device_rr_gsb）
3. build_unique_module(...)      // 压缩：两两比较，填三张 unique_module_id_ 矩阵
   └─ read_unique_blocks(...)    // 或：跳过比较，直接 preload 三张矩阵（缓存命中）
4. is_compressed() == true       // build_fabric 据此走 unique 实例化路径
```

注意第 1 步里有一个**版本耦合点**：`reserve()` 在创建每个 `RRGSB` 元素时需要知道 GSB 版本（V1/V2），因此版本必须在 `reserve()` **之前**设置好——这正是本次更新引入 `gsb_version_set_` 守卫的原因之一。

#### 4.1.3 源码精读

先看 `is_compressed_` 标志与版本成员的声明：

- [device_rr_gsb.h:197-198](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/device_rr_gsb.h#L197-L198)：`is_compressed_` 默认 `false`，注释说「True if the unique blocks have been preloaded **or** built」——即无论是现场压缩还是缓存回灌，完成后都置真。
- [device_rr_gsb.h:221-224](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/device_rr_gsb.h#L221-L224)：GSB 版本成员。本次更新新增的注释点明 `gsb_version_set_`「guards against reading the version before `set_gsb_version()` is called」。注意 `gsb_version_` 默认 `GSB_V1`，但这只是占位，**真正的值在 link 阶段才注入**。

再看 `is_compressed()` 与版本访问/修改器：

- [device_rr_gsb.cpp:91](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/device_rr_gsb.cpp#L91)：`is_compressed()` 仅返回标志，是 build_fabric 唯一查询的「是否已压缩」入口。
- [device_rr_gsb.cpp:807-820](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/device_rr_gsb.cpp#L807-L820)：这是本次更新的核心改动点。`get_gsb_version()` 现在用 `VTR_ASSERT_OPT_MSG(gsb_version_set_, ...)` 守门——若 `set_gsb_version()` 还没被调用过就直接读版本，会触发断言失败并给出明确提示；`set_gsb_version()` 在写入版本的同时把 `gsb_version_set_` 置真。

最后看版本在 `reserve()` 里的使用，这是守卫最关键的防护点：

- [device_rr_gsb.cpp:281-284](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/device_rr_gsb.cpp#L281-L284)：`reserve()` 创建每个 `RRGSB` 元素时调用 `RRGSB(get_gsb_version())`（本次更新从直接用成员 `gsb_version_` 改为走带守卫的 `get_gsb_version()`）。`resize_upon_need()` 在 [device_rr_gsb.cpp:323-325](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/device_rr_gsb.cpp#L323-L325) 同样改走 `get_gsb_version()`。

> **为什么这个守卫重要？** 在旧代码里，`rr_gsb_` 的元素如果意外先于 `set_gsb_version()` 被创建，会静默地用默认值 `GSB_V1` 构造，导致「该用 V2 的阵列被当成 V1」这种难以排查的隐患。守卫把这个隐患从「静默错误」变成「快速失败 + 明确报错」。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认 `gsb_version_` 的默认值、守卫标志，以及 `is_compressed_` 的初始状态。
2. **操作步骤**：打开 `openfpga/src/annotation/device_rr_gsb.h`，定位到第 197 行与第 221–224 行；再打开 `.cpp` 的第 807–820 行。
3. **需要观察的现象**：注意 `gsb_version_` 虽然写了 `= e_gsb_version::GSB_V1`，但 `gsb_version_set_` 默认 `false`，所以即便默认值是 V1，`get_gsb_version()` 在未 set 之前也会断言失败。
4. **预期结果**：你会理解「默认值」与「已设置」是两件事——OpenFPGA 宁可要求显式设置，也不接受「碰巧正确的默认值」。
5. 待本地验证：若你本地已编译 OpenFPGA，可尝试构造一个 `DeviceRRGSB` 后**不**调 `set_gsb_version()` 就调 `reserve()`，观察是否触发断言（注意这需要写一个最小测试程序，命令行无法直接触发）。

#### 4.1.5 小练习与答案

- **练习 1**：`is_compressed()` 在 build_fabric 里只读一个 `bool`，为什么 OpenFPGA 不直接用 `get_num_gsb_unique_module() > 0` 来判断「是否已压缩」？
  - **答案**：因为 `is_compressed_` 的语义是「归类完成」，它对「现场压缩（build）」和「缓存回灌（read）」两种途径都成立。而 `get_num_gsb_unique_module()` 反映的是 unique 模块数量，在阵列极小、确实只有少数 unique 模块时数值可能很小甚至为 0，无法可靠区分「没压过」与「压完恰好很少」。用一个独立的标志位语义最清晰。

- **练习 2**：`set_gsb_version()` 在 `annotate_device_rr_gsb` 里被调用了两次（见 4.4.3），为什么要调两次？
  - **答案**：一次在 `reserve()` 之前，一次在 `reserve()` 之后。前者是为了让 `reserve()` 内部 `RRGSB(get_gsb_version())` 能取到正确版本；后者是防御性补设，保证后续任何 `resize_upon_need()` 或其它读取都拿到正确值。核心约束是「版本必须在 reserve 之前设置」。

---

### 4.2 Unique block 压缩：镜像识别与去重

#### 4.2.1 概念说明

「压缩」的本质是**把结构相同的 GSB 归为同一类，只生成一个模块，其余作为它的实例复用**。判定两个 GSB 是否「结构相同」用的是一个**镜像（mirror）判定函数**：若两个 GSB 的开关块/连接块在拓扑上互为镜像（节点、边、驱动关系一致），就认为它们是同一个 unique 模块的不同实例。

OpenFPGA 把压缩分成**三层独立判定**：

- **SB 层**：`build_sb_unique_module` 逐坐标判定每个开关块是否是某个已有 unique SB 的镜像。
- **CB 层**：`build_cb_unique_module` 对 CBX 和 CBY 各做一遍同样的镜像判定。
- **GSB 层**：`build_gsb_unique_module` 不再做拓扑比较，而是直接看「一个坐标的 SB/CBX/CBY 三个 unique id 是否与某个已有 GSB 代表坐标的三 id **完全相等**」——三者全等则视为同一 GSB。

这种「先分别去重，再用三 id 汇总」的设计，把「比较一个大 GSB」拆成了「比较三个小部件」，既减少了比较代价，也让 GSB 的 unique 划分有了清晰的组合语义。

#### 4.2.2 核心流程

以 SB 为例，压缩算法是一个朴素但有效的「在线归类」：

```text
for 每个坐标 (ix, iy):
    is_unique = true
    for 每个 已有的 unique 模块 id:
        if is_sb_mirror(当前坐标的 SB, 该 unique SB 的代表):   # 拓扑完全相同？
            is_unique = false
            sb_unique_module_id_[ix][iy] = id                 # 归到该类
            break
    if is_unique:
        把当前坐标加入 unique 列表
        sb_unique_module_id_[ix][iy] = 新 id
```

CBX/CBY 同理。三张 id 矩阵填好后，`build_gsb_unique_module` 用「三 id 元组相等」做 GSB 级汇总，并把 `is_compressed_` 置真。

压缩率由工具报告，公式为：

\[
\text{compression\_rate}\% = 100 \times \left(\frac{\text{总模块数}}{\text{unique 模块数}} - 1\right)
\]

例如阵列里有 980 个 SB、识别出 7 个 unique，则压缩率 = \(100 \times (980/7 - 1) = 13900\%\)，意思是「平均每个 unique 模块复用了约 140 次」。

#### 4.2.3 源码精读

SB 的镜像识别与归类（逻辑最清晰，CB 与之同构）：

- [device_rr_gsb.cpp:409-451](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/device_rr_gsb.cpp#L409-L451)：`build_sb_unique_module`。注意它比较的不是裸 `RRGSB`，而是把 `RRGSB` 与 `RRGSBEdges`（入边信息）一起交给 `is_sb_mirror()`——因为镜像判定要看「驱动关系」，而驱动关系存在 edges 里（VPR 只存 fan-out，OpenFPGA 在 link 阶段用 `RRGraphInEdges` 反推出了入边，见 u5-l3）。命中镜像就把当前坐标的 id 记为该 unique id 并 `break`；都没命中则新建一个 unique。

CB 的镜像识别（对 CHANX/CHANY 各跑一次）：

- [device_rr_gsb.cpp:360-405](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/device_rr_gsb.cpp#L360-L405)：`build_cb_unique_module`。结构与 SB 版几乎一致，差别仅在按 `cb_type` 取对应的 `cbx_/cby_` 矩阵，以及 `is_cb_mirror()` 的比较。

GSB 级汇总（**不做拓扑比较，只比三 id**）：

- [device_rr_gsb.cpp:457-500](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/device_rr_gsb.cpp#L457-L500)：`build_gsb_unique_module`。关键判据在第 475–483 行——当前坐标的 `sb/cbx/cby` 三个 unique id 与某个已有 GSB 代表坐标的三个 id **全部相等**，才判为镜像。第 499 行 `is_compressed_ = true` 是唯一的置位点。

总入口：

- [device_rr_gsb.cpp:502-510](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/device_rr_gsb.cpp#L502-L510)：`build_unique_module` 依次调用 SB → CBX → CBY → GSB，注释明确 `is_compressed_` 在 `build_gsb_unique_module` 内部翻转。

压缩触发与压缩率报告：

- [openfpga_build_fabric_template.h:43-103](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/base/openfpga_build_fabric_template.h#L43-L103)：`compress_routing_hierarchy_template`。第 52–53 行调用 `build_unique_module`；随后用 `find_device_rr_gsb_num_*_modules` 拿到「总模块数」，按上面的公式打印 CBX/CBY/SB/GSB 四行压缩率。
- [device_rr_gsb_utils.cpp:18-66](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/utils/device_rr_gsb_utils.cpp#L18-L66)：三个统计函数，遍历整个 `rr_gsb_` 阵列数「真实存在的模块总数」，是压缩率公式的分子来源。

build_fabric 里的守门逻辑（决定要不要现场压缩）：

- [openfpga_build_fabric_template.h:149-157](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/base/openfpga_build_fabric_template.h#L149-L157)：这是「算法 + 缓存」联动的关键。若用户传了 `--compress_routing` 且**尚未**压缩 → 现场跑一次压缩；若 `is_compressed()` 已为真（说明前面 `read_unique_blocks` 已经回灌过）→ 跳过比较，只把 `flow_manager` 的 `compress_routing` 标志置真。两条路径殊途同归：都让后续 `build_device_module_graph` 走 unique 实例化。

#### 4.2.4 代码实践

1. **实践目标**：观察 `--compress_routing` 对生成网表数量的实际影响。
2. **操作步骤**：`source openfpga.sh` 后，分别跑两次同一任务（如 `basic_tests/full_testbench/configuration_chain`）：
   - 第一次：编辑该任务用的 `.openfpga` 脚本，把 `build_fabric` 的 `--compress_routing` 去掉（或用 `generate_fabric_example_script.openfpga` 作参照）。
   - 第二次：保留 `--compress_routing --verbose`。
3. **需要观察的现象**：关注 build_fabric 的 verbose 日志中形如 `Detected N unique X-direction connection blocks from a total of M (compression rate=...)` 的四行；以及 `SRC/` 目录下生成的 routing 子模块网表数量。
4. **预期结果**：开压缩时 routing 网表数量显著少于不开时，且日志里 unique 数远小于 total。
5. 待本地验证（取决于是否已完成 `make compile` 与子模块 checkout，见 u1-l3）。

#### 4.2.5 小练习与答案

- **练习 1**：`build_gsb_unique_module` 为什么不直接对整个 GSB 做镜像比较，而是「比三个 unique id」？
  - **答案**：因为 SB 和 CB 的 unique 划分已经在前面两步完成且已去重。一个 GSB 由「一个 SB + 一个 CBX + 一个 CBY」组合而成，所以「两个 GSB 全等」等价于「它们的 SB、CBX、CBY 各自属于同一个 unique 类」，即三 id 元组相等。复用已有结果比再做一遍 GSB 级拓扑比较更省、语义也更清晰。

- **练习 2**：如果阵列里每个 GSB 都互不相同（极端无规律），`--compress_routing` 还有意义吗？
  - **答案**：此时 unique 数 ≈ 总数，压缩率接近 0，模块数量不会减少，反而多花了比较的时间。但 `is_compressed_` 仍会置真、flow_manager 仍记为 compressed routing——这会让 fabric 走 unique 实例化路径（每个坐标一个独立模块名），与 flatten 路径在模块命名与网表组织上仍有差异。所以压缩的意义取决于阵列的规律性。

---

### 4.3 Unique blocks 读写：XML / 二进制缓存

#### 4.3.1 概念说明

镜像比较是 \(O(\text{坐标数} \times \text{unique 数})\) 的开销，在大阵列上不便宜。但同一个架构（同一份 VPR arch + 同一个 device 尺寸）的 unique 划分是**确定且可复现**的——只要 RR graph 没变，压缩结果就一样。于是 OpenFPGA 提供「缓存」机制：第一次构建时把 unique 划分写到文件，后续构建直接读回来 preload，跳过比较。

两条命令负责这件事：

- `write_unique_blocks --file <path> --type xml|bin`：把当前 `DeviceRRGSB` 的 unique 模块及其所有实例坐标写出。
- `read_unique_blocks --file <path> --type xml|bin`：把文件读回，直接填三张 `*_unique_module_id_` 矩阵（preload），随后 `build_gsb_unique_module` 汇总并把 `is_compressed_` 置真。

两种格式：**XML** 人可读、便于调试与版本对比；**bin** 用 capnp 序列化、体积更小、加载更快。命令用 `--type` 选择，默认 XML。

#### 4.3.2 核心流程

XML 格式是一棵简单的树，每个 unique 块是一个 `<block>`，列出它的代表坐标与所有镜像实例：

```xml
<unique_blocks>
  <block type="sb" x="1" y="1">     <!-- 代表坐标 (1,1) 的 unique SB -->
    <instance x="1" y="1"/>
    <instance x="3" y="1"/>          <!-- (3,1) 的 SB 与 (1,1) 互为镜像 -->
    ...
  </block>
  <block type="cbx" x="..." y="..."> ... </block>
  <block type="cby" x="..." y="..."> ... </block>
  ...
</unique_blocks>
```

读回流程：

```text
read_xml_unique_blocks:
  clear_unique_modules() + reserve_unique_modules()   # 清空旧归类、预留容量
  for 每个 <block>:
      preload_unique_sb/cbx/cby_module(代表坐标, [实例坐标...])   # 直接填 id 矩阵
  build_gsb_unique_module()           # 用三 id 汇总，置 is_compressed_ = true
```

注意 preload **不做任何镜像比较**——它无条件相信文件里写的归类，因此速度极快。这也意味着：**缓存文件必须与当前架构/device 严格匹配**，否则会把错误的归类灌进去。

#### 4.3.3 源码精读

XML 写出（每个 unique 块 → 代表坐标 + 实例列表）：

- [write_unique_blocks_xml.cpp:36-66](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/write_unique_blocks_xml.cpp#L36-L66)：`write_xml_atom_block` 写一个 `<block type=... x=... y=...>` 及其内部若干 `<instance x=... y=.../>`。总入口 `write_xml_unique_blocks`（[L117](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/write_unique_blocks_xml.cpp#L117)）分别遍历 SB/CBX/CBY 的 unique 列表，用 `get_sb_unique_block_instance_coord` / 对应的 CB 访问器取出每个 unique 的全部镜像实例，逐个调用 `write_xml_atom_block`。

XML 读入（解析 + 分派到 preload）：

- [read_unique_blocks_xml.cpp:103-158](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/read_unique_blocks_xml.cpp#L103-L158)：`read_xml_unique_blocks`。先 `clear_unique_modules()` + `reserve_unique_modules()`（[L114-115](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/read_unique_blocks_xml.cpp#L114-L115)），再对每个 `<block>` 按其 `type`（sb/cby/cbx）调对应的 preload 函数（[L130-138](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/read_unique_blocks_xml.cpp#L130-L138)），非法 type 抛错。最后调 `build_gsb_unique_module()`（[L150](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/read_unique_blocks_xml.cpp#L150)）汇总——注意此处复用了与现场压缩**同一个**汇总函数，保证「缓存回灌」与「现场压缩」得到的 GSB 划分在语义上完全一致。

preload 函数（无条件填 id 矩阵，无比较）：

- [device_rr_gsb.cpp:786-805](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/device_rr_gsb.cpp#L786-L805)：`preload_unique_sb_module`（CBX/CBY 版结构相同，见 [L733-782](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/device_rr_gsb.cpp#L733-L782)）。它把代表坐标登记为新 unique，再把所有实例坐标的 id 都设成同一个值——这就是「跳过比较、直接归类」的全部魔法。

命令模板（文件类型分派 xml/bin）：

- [openfpga_build_fabric_template.h:581-610](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/base/openfpga_build_fabric_template.h#L581-L610)：`read_unique_blocks_template`，按 `--type` 选 `read_xml_unique_blocks` 或 `read_bin_unique_blocks`。`write_unique_blocks_template` 在 [L612-642](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/base/openfpga_build_fabric_template.h#L612-L642) 结构对称。

命令注册与依赖：

- [openfpga_setup_command_template.h:1165-1197](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/base/openfpga_setup_command_template.h#L1165-L1197)：`read_unique_blocks` 注册，带 `--file/--type/--verbose`。它的命令依赖在 [L1619-1623](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/base/openfpga_setup_command_template.h#L1619-L1623) 声明为「必须先跑过 `link_openfpga_arch`」——因为 preload 需要一个已经 reserve 好的 `device_rr_gsb` 骨架。
- [openfpga_setup_command_template.h:1204-1236](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/base/openfpga_setup_command_template.h#L1204-L1236)：`write_unique_blocks` 注册。注意它的依赖在 [L1628-1629](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/base/openfpga_setup_command_template.h#L1628-L1629) 传了**空依赖**——shell 不强制前置命令，但若 device_rr_gsb 尚未压缩，写出的内容就是空的/无意义的。

> **别混淆**：`write_gsb` 命令（由 [write_xml_device_rr_gsb.h:22-26](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/write_xml_device_rr_gsb.h#L22-L26) 声明的 `write_device_rr_gsb_to_xml`）是把**每个坐标**的 GSB 详细内容 dump 成 XML，供人工排查某一个 GSB 的拓扑，**不做去重、不用于缓存加速**。它和 unique blocks 是两套不同的产物。

#### 4.3.4 代码实践

1. **实践目标**：跑通「读-写 unique blocks」完整闭环，体会缓存如何绕过镜像比较。
2. **操作步骤**：`source openfpga.sh` 后，参考并运行 `openfpga_flow/openfpga_shell_scripts/read_write_unique_blocks_full_flow_example_script.openfpga`。该脚本的关键三步是：
   - `read_unique_blocks --file ${READ_UNIQUE_BLOCKS} --type ${OPENFPGA_UNIQUE_BLOCK_FILE_READ}`（在 build_fabric 之前 preload）；
   - `build_fabric --group_tile ...`（注意 `--group_tile` 隐式要求 unique blocks 已就绪，见 4.2.3 守门逻辑）；
   - `write_unique_blocks --file ${WRITE_UNIQUE_BLOCKS} --type ${OPENFPGA_UNIQUE_BLOCK_FILE_WRITE}`（把这次结果写回，供下次用）。
3. **需要观察的现象**：日志里 `read_unique_blocks` 的 verbose 输出会报告「Read N unique ... from a total of M (compression rate=...)」，且 `build_fabric` **不会**再打印「Detected N unique ...」的现场压缩日志——因为 `is_compressed()` 已为真，直接走了 preload 路径。
4. **预期结果**：你能在输出目录看到写出的 `unique_blocks.xml`，其内容是上文那种 `<unique_blocks>` 树。对比「先 write 再 read」与「只现场压缩」两次构建，最终生成的 fabric 网表应完全一致。
5. 待本地验证。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `write_unique_blocks` 声明的是空依赖，而 `read_unique_blocks` 却依赖 `link_openfpga_arch`？
  - **答案**：write 只是把「当前 device_rr_gsb 里已有的 unique 归类」序列化，空依赖意味着 shell 不拦截，但如果没压缩过，写出的就是空结果（责任在调用者）。read 则必须在 `device_rr_gsb` 已经 reserve 好骨架之后才能 preload——而骨架由 link 阶段的 `annotate_device_rr_gsb` 建立，所以 read 强依赖 link。两者的「前置条件严格度」反映了数据流方向。

- **练习 2**：如果你修改了 VPR 架构（比如换了一组布线线段），却仍沿用旧的 `unique_blocks.xml`，会发生什么？
  - **答案**：preload 会无条件接受旧归类，把与新架构不匹配的镜像关系灌进 `device_rr_gsb`，`is_compressed_` 照样置真，后续 build_fabric 会基于错误归类实例化 SB/CB 模块，产出结构错误的 fabric。**缓存文件必须与架构/device 严格配套**——换架构就要重新 `write_unique_blocks` 生成新缓存。

---

### 4.4 GSB 版本来源：从 VPR device context 注入

#### 4.4.1 概念说明

「GSB 版本」是一个枚举 `e_gsb_version`，取值 `GSB_V1` 与 `GSB_V2`，它决定了 `RRGSB` 用哪种内部模型来组织通道节点与 OPIN/IPIN 的方向信息。V2（本次相关的较新路径）采用 side-agnostic 的通道模型，从 track 的 pass-through 行为推导通道节点与端口方向，OPIN/IPIN 按各自所在的 grid 边添加——这与 V1「先建完整 GSB 再按排序好的入边过滤」的做法不同。

本次更新的关键变化是：**GSB 版本不再是一条命令选项**。在旧版本里它可能由 link 命令行参数控制，但这会带来风险——如果用户给的 GSB 版本与 VPR 实际构建 RR graph 时用的版本不一致，OpenFPGA 就会用错误的模型去解读 RR graph，产生隐蔽 bug。因此现在改为「**VPR 在生成 RR graph 时把版本写进 device context，OpenFPGA 在 link 阶段直接读出来用**」，从源头消除不一致。

#### 4.4.2 核心流程

```text
VPR 生成 RR graph 时:        device_ctx.gsb_version = 实际使用的版本(V1 或 V2)
                              （写入 VPR device context）

link_openfpga_arch:
  gsb_version = g_vpr_ctx.device().gsb_version        # 直接读，不是命令选项
  annotate_device_rr_gsb(..., gsb_version, ...):
      device_rr_gsb.set_gsb_version(gsb_version)      # 必须在 reserve 之前设
      device_rr_gsb.reserve(...)                        # 内部用 get_gsb_version() 构造 RRGSB
      for 每个坐标:
          if gsb_version == GSB_V2:
              rr_gsb = build_rr_gsb2(...)              # side-agnostic 路径
          else:
              rr_gsb = build_rr_gsb(...)               # V1 路径
```

#### 4.4.3 源码精读

从 VPR device context 取版本（**本次更新的核心**）：

- [openfpga_link_arch_template.h:58-61](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/base/openfpga_link_arch_template.h#L58-L61)：`e_gsb_version gsb_version = g_vpr_ctx.device().gsb_version;`。注释明确说明「It is not a command option to avoid any mismatch between the VPR options and this command」。这正是 u5-l3 里提到的「GSB 版本由 VPR device context 注入而非命令选项」的落点。
- [openfpga_link_arch_template.h:125-130](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/base/openfpga_link_arch_template.h#L125-L130)：`annotate_device_rr_gsb(...)` 的调用点，`gsb_version` 作为参数传入。

annotate 阶段按版本选构建路径：

- [annotate_rr_graph.cpp:892-898](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/annotate_rr_graph.cpp#L892-L898)：`annotate_device_rr_gsb` 签名，`gsb_version` 是参数之一。
- [annotate_rr_graph.cpp:910-914](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/annotate_rr_graph.cpp#L910-L914)：**关键**——`set_gsb_version()` 被调用了两次，分别在 `reserve()` 之前与之后（注释写明「Must set version before reserve. Other RRGSB version is not passed into actual data」）。这保证了 `reserve()` 内部 `RRGSB(get_gsb_version())` 能取到正确版本，同时 4.1.3 的守卫不会误触发。
- [annotate_rr_graph.cpp:932-952](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/annotate_rr_graph.cpp#L932-L952)：V1/V2 分支。`GSB_V2` 走 `build_rr_gsb2`（side-agnostic，注释解释了它如何从 pass-through 行为推导通道节点、并把 OPIN/IPIN 按入边过滤），否则走 `build_rr_gsb`（V1，先建完整 GSB 再按排序入边过滤）。

守卫的最终落点（呼应 4.1.3）：

- [device_rr_gsb.cpp:807-814](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/annotation/device_rr_gsb.cpp#L807-L814)：`get_gsb_version()` 的断言。把「版本来源是 VPR context、且必须先 set 后用」这条不变式用代码钉死。

> 顺带一提：本次更新里 `annotate_rr_graph.cpp` 与 `openfpga_link_arch_template.h` 的 diff 大多是空行/注释整理（如 [openfpga_link_arch_template.h:55-57](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/base/openfpga_link_arch_template.h#L55-L57) 处删了一行空行），真正影响行为的是 `device_rr_gsb.{h,cpp}` 里新增的 `gsb_version_set_` 守卫与 `reserve/resize_upon_need` 改走 `get_gsb_version()`。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：在源码里走通「VPR context → link → annotate → reserve」这条版本传递链，确认它不经过任何命令选项。
2. **操作步骤**：
   - 打开 `openfpga/src/base/openfpga_link_arch_template.h` 第 58–61 行，确认 `gsb_version` 来自 `g_vpr_ctx.device().gsb_version`；
   - 顺着第 125–130 行的 `annotate_device_rr_gsb(...)` 调用，打开 `annotate_rr_graph.cpp` 第 910–914 行，确认 `set_gsb_version()` 在 `reserve()` 前后被调用；
   - 再打开 `device_rr_gsb.cpp` 第 281–284 行，确认 `reserve()` 通过 `get_gsb_version()` 取版本。
3. **需要观察的现象**：整条链路上没有任何 `cmd.option(...)` 读取 GSB 版本——它纯粹来自 VPR device context。
4. **预期结果**：你能用一句话总结「GSB 版本为何不能是命令选项」——因为它是 RR graph 的事实属性，必须与 VPR 实际构建时用的版本一致，单点来源（VPR context）是唯一安全的做法。
5. 待本地验证（纯阅读，无需运行）。

#### 4.4.5 小练习与答案

- **练习 1**：如果有人想在 `link_openfpga_arch` 上加一个 `--gsb_version` 选项让用户覆盖版本，为什么是个坏主意？
  - **答案**：因为 GSB 版本描述的是「VPR 实际如何构建 RR graph」，是既成事实。若允许用户覆盖，就可能让 OpenFPGA 用 V2 模型去解读一个 V1 的 RR graph（或反之），`build_rr_gsb2`/`build_rr_gsb` 会基于错误假设推导通道方向，产出错误 GSB。把来源钉在 VPR device context，保证「解读方式 = 构建方式」，从根本上杜绝不一致。

- **练习 2**：`set_gsb_version()` 在 `annotate_device_rr_gsb` 里调了两次（reserve 前 + reserve 后），如果只保留 reserve 前那一次，会出什么问题？
  - **答案**：理论上 reserve 前那次已足够让 `reserve()` 正确。第二次是防御性的——它确保「即便未来有人重排代码顺序、或 `resize_upon_need()` 在 reserve 之后被触发，版本也仍然是被显式设置过的状态」（`gsb_version_set_` 为真）。删掉它不会立刻出错，但会削弱对后续代码变动的健壮性。

---

## 5. 综合实践

把本讲四个最小模块串起来，设计一个完整的「压缩 + 缓存 + 版本」验证任务：

**任务**：在一个大阵列上对比三种构建路径的产出与耗时。

1. **准备**：`source openfpga.sh`，选一个网格较大的任务（如某个 k4n 系列任务，或把现有任务的 device 尺寸调大）。确认 `make compile` 已完成（u1-l3）。
2. **路径 A（基线，不压缩）**：编辑 `.openfpga` 脚本，`build_fabric` 不加 `--compress_routing`，记录 SRC/ 下 routing 网表数量与 build_fabric 耗时。
3. **路径 B（现场压缩）**：加 `build_fabric --compress_routing --verbose`，记录日志里四行压缩率与 routing 网表数量，应远少于 A。
4. **路径 C（缓存加速）**：在 B 的脚本基础上，先跑一次 `write_unique_blocks --file unique_blocks.xml --type xml` 得到缓存；再新开一次构建，在 `build_fabric` 之前插入 `read_unique_blocks --file unique_blocks.xml --type xml --verbose`，build_fabric 仍带 `--compress_routing`。观察：read 的 verbose 报告与 B 的压缩报告数值应一致；且 build_fabric **不再**打印现场压缩日志（因为 `is_compressed()` 已为真，走了 4.2.3 的第二条路径）。
5. **验证等价性**：用 `diff` 对比路径 B 与路径 C 生成的 fabric 网表集合，应完全一致（模块名、实例关系相同），证明缓存是「无损加速」。
6. **反思版本来源**：在整个过程中，你不需要、也无法通过命令行指定 GSB 版本。结合 4.4 的讲解，写下「为什么这反而更安全」。

> 待本地验证：本任务依赖一次完整构建（含 VPR 综合/布线），耗时取决于阵列规模。若时间有限，可只做路径 B 与路径 C 的对比，跳过路径 A。

## 6. 本讲小结

- `DeviceRRGSB` 用三张「unique id 矩阵」（SB/CBX/CBY）记录每个坐标归属哪个 unique 模块，并用一个 `is_compressed_` 标志标记「归类已完成（无论现场压缩还是缓存回灌）」。
- 压缩分三层：SB/CB 各自用 `is_sb_mirror`/`is_cb_mirror` 做拓扑镜像判定，GSB 层则用「三 id 全等」汇总，`build_gsb_unique_module` 是 `is_compressed_` 的唯一置位点。
- `--compress_routing` 与 `is_compressed()` 联动：未压缩则现场 `build_unique_module`，已压缩（被 `read_unique_blocks` 预填）则跳过比较、只置 flow_manager 标志。
- `read/write_unique_blocks` 用 XML 或 capnp 二进制缓存 unique 划分，preload 无条件相信文件、不做比较，把昂贵的镜像识别变成可复现的廉价回灌——前提是缓存与架构严格配套。
- **本次更新的核心**：GSB 版本（V1/V2）不再是命令选项，而由 VPR device context 在 RR graph 生成时写入、link 阶段读出；新增的 `gsb_version_set_` 守卫让「未设置即读取」从静默错误变成快速失败，`annotate_device_rr_gsb` 据此在 V2 走 side-agnostic 的 `build_rr_gsb2` 路径。
- 注意区分三套产物：`write_unique_blocks`（去重缓存）、`write_gsb`（逐坐标调试 dump）、以及 fabric 网表本身——它们用途不同，不可混用。

## 7. 下一步学习建议

- **回到 fabric tile（u9-l4）**：fabric tile 的 `equivalent_tile` 判定依赖 `DeviceRRGSB`，理解了 unique 压缩后，你会更清楚「为什么 `--group_tile` 隐式要求 compress routing」（见 4.2.3 的守门逻辑）。
- **深入 RR graph 标注（u5-l3 / u5-l4）**：本讲的镜像比较依赖 `RRGSBEdges`（入边信息），而入边是 link 阶段用 `RRGraphInEdges` 从 VPR 的 fan-out 边反向构建的。若想理解「为什么需要 in_edges」，可重读 u5-l3 的 `annotate_device_rr_gsb` 调用点与本讲的 `is_sb_mirror`。
- **阅读 VPR 侧的 RR graph 构建**：GSB 版本由 VPR 写入 device context。若你已 checkout 子模块（u1-l3），可到 `vtr-verilog-to-routing/` 里搜索 `gsb_version` 的写入点，看 VPR 在何种条件下选择 V1/V2，这是理解「版本为何是 RR graph 事实属性」的最直接途径。
- **动手扩展**：仿照 `write_unique_blocks_xml.cpp` 的结构，尝试写一个最小工具统计「每个 unique 模块被复用了多少次」，输出一张复用直方图——这将加深你对 unique 压缩在实际阵列中分布规律的理解。
