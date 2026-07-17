# 分析式布局引擎

## 1. 本讲目标

VPR 的传统布局走的是「先打包成聚簇块（ClusteredNetlist），再用模拟退火把聚簇块摆到器件网格上」这条路（见第 4、5 单元）。本讲要介绍的是一条**替代路径**——分析式布局（Analytical Placement，简称 AP）。

它把「打包」和「布局」重新融合成一个整体：先用数学求解器在连续坐标空间里求一个全局最优的「平铺布局」（flat placement），再把这个近似的解合法化成 VPR 后续阶段能消费的聚簇网表 + 合法坐标。学完本讲，你应当能够：

- 说清楚 AP 流程的「全局布局 → 合法化 → 详细布局」三阶段各做什么、产物是什么；
- 理解 AP 自定义的 `APNetlist` 与 `PartialPlacement` 两个核心数据结构，以及它们如何复用第 3 单元的 `Netlist` 基类；
- 描述全局布局内部的 SimPL 迭代（解析求解器与部分合法化器的「上下界」耦合）；
- 解释 AP 如何替代、又如何复用传统打包/布局代码，并在 `vpr_api.cpp` 中找到它的入口分派。

## 2. 前置知识

阅读本讲前，请确保你已经理解以下概念（它们都在前序讲义中讲过）：

- **打包与聚簇网表**：`AtomNetlist`（原子级网表）经打包变成 `ClusteredNetlist`（聚簇块级网表），打包以「分子」`t_pack_molecule` 为最小搬运单元（u4-l1、u4-l2）。
- **模拟退火布局**：布局把聚簇块摆到 `DeviceGrid` 上，目标是线长 + 时序，用退火接受准则跳出局部最优（u5-l1）。
- **Netlist 泛型基类**：`Netlist<BlockId,PortId,PinId,NetId>` 用块/端口/引脚/网四元模型刻画电路，`vtr::StrongId` 保证 ID 类型安全（u3-l1）。
- **全局状态总线**：`g_vpr_ctx` 聚合各子上下文，阶段间数据靠它共享（u3-l4、u3-l5）。
- **阶段动作枚举**：`e_stage_action`（DO/LOAD/SKIP/SKIP_IF_PRIOR_FAIL）决定某阶段运行、加载还是跳过（u1-l5、u3-l5）。

两个本讲用到、但读者可能陌生的术语：

- **HPWL（Half-Perimeter Wirelength，半周长线长）**：用一个网所有引脚包围盒的「(xmax−xmin)+(ymax−ymin)」近似该网连线长度，是布局最常用的线长估计。布局优化目标常写成最小化所有网的 HPWL 之和。
- **平铺布局（flat placement）**：直接在原子（primitive）层级给出的连续坐标布局，不区分聚簇块。AP 的核心就是先求一个好的平铺布局，再据此打包。

## 3. 本讲源码地图

本讲聚焦 `vpr/src/analytical_place/` 目录，它是 VPR 中相对独立的一个子系统：

| 文件 / 目录 | 作用 |
| --- | --- |
| `analytical_place/analytical_placement_flow.h/.cpp` | AP 流程的总入口 `run_analytical_placement_flow`，串起三个子阶段。 |
| `analytical_place/ap_flow_enums.h` | 四个枚举，分别选择解析求解器、部分合法化器、全合法化器、详细布局器的具体实现。 |
| `analytical_place/common/ap_netlist.h` | AP 专用网表 `APNetlist`，继承第 3 单元的 `Netlist` 基类。 |
| `analytical_place/common/partial_placement.h` | `PartialPlacement`：连续坐标的「部分合法」布局容器。 |
| `analytical_place/common/gen_ap_netlist_from_atoms.h` | 从 `AtomNetlist` + 打包分子构造 `APNetlist`。 |
| `analytical_place/global_placement/` | 第一阶段：全局布局。含解析求解器（`analytical_solver.h`）、部分合法化器（`partial_legalizer.h`）、SimPL 主循环（`global_placer.h/.cpp`）。 |
| `analytical_place/full_legalization/` | 第二阶段：全合法化器 `FullLegalizer`（Naive / APPack / FlatRecon）。 |
| `analytical_place/detailed_placement/` | 第三阶段：详细布局器 `DetailedPlacer`（Identity / Annealer / WindowedBiMatching）。 |
| `base/vpr_api.cpp` | 主流程编排，AP 在这里的 `Analytical Place` 分支被分派。 |
| `base/setup_vpr.cpp` | 把 `--analytical_place` 开关翻译成 `doAP = DO`，并跳过传统打包/布局。 |
| `base/vpr_types.h` | `t_ap_opts` 结构体，聚合 AP 的全部命令行选项。 |

> 提醒：`analytical_place/` 下的解析求解器依赖线性代数库 **Eigen**（条件编译 `EIGEN_INSTALLED`）。未装 Eigen 时，QP/B2B 求解器不可用，只剩 `Identity` 占位求解器——这通常只出现在精简构建环境里。

## 4. 核心概念与源码讲解

### 4.1 分析式布局三阶段总览

#### 4.1.1 概念说明

传统流程是「**先打包，再布局**」：打包阶段必须在不知道物理位置的情况下决定哪些原子进同一个聚簇块，布局阶段再被动地摆放这些已经定型的块。一旦打包把关系紧密的原子错误地分到不同块里，布局再怎么退火也救不回来。

分析式布局把这个顺序倒过来：**先在原子层做一个全局布局，再根据物理位置来打包**。直觉上，先用连续坐标把每个原子放到一个「线长/时序都很好」的位置（此时允许重叠、允许落在非法瓦片上），然后再把这些原子按物理邻近性聚拢成合法的聚簇块。这样打包决策就有了全局位置信息做依据。

VPR 的 AP 流程把这件事拆成三个子阶段：

1. **全局布局（Global Placement, GP）**：用解析求解器在连续空间最小化 HPWL（可选兼顾时序），再用部分合法化器把过密区域摊开，得到一个「大部分合理但还不严格合法」的平铺布局。产物是 `PartialPlacement`。
2. **全合法化（Full Legalization）**：把平铺布局翻译成 VPR 能用的形式——构造聚簇块（生成 `ClusteredNetlist`）并把每个聚簇块放到器件网格的合法瓦片上。产物写入全局 `PlacementContext`。
3. **详细布局（Detailed Placement）**：在保持合法的前提下，用局部优化（退火或二分匹配）进一步降低线长/时序。

每个子阶段都有多个可替换实现，由四个枚举选择，集中定义在 `ap_flow_enums.h`：

- 解析求解器 `e_ap_analytical_solver`：`Identity`（占位，不优化）、`QP_Hybrid`（二次 HPWL）、`LP_B2B`（线性 HPWL）。
- 部分合法化器 `e_ap_partial_legalizer`：`Identity`、`BiPartitioning`、`FlowBased`。
- 全合法化器 `e_ap_full_legalizer`：`Naive`、`APPack`、`FlatRecon`。
- 详细布局器 `e_ap_detailed_placer`：`Identity`、`Annealer`、`WindowedBiMatching`。

#### 4.1.2 核心流程

AP 的总入口 `run_analytical_placement_flow` 用一条线把三个子阶段串起来，外加若干「准备」与「收尾」步骤：

```
run_analytical_placement_flow(vpr_setup):
  准备阶段：
    Prepacker            # 复用打包的分子识别（见 u4-l2）
    PreClusterTimingManager  # 预聚类时序，供 GP 做时序驱动
    DeviceSizeEstimator  # 估计器件尺寸（auto-sizing 时给 GP 真实网格）
    RamMapper (可选)     # 识别物理 RAM 组
    APNetlist            # 由原子网表 + 分子生成（见 4.2）
    PlaceDelayModel (可选) # GP 用它把延迟近似进目标函数

  第一阶段 全局布局：
    PartialPlacement = run_global_placer(...)   # SimPL 迭代（见 4.3）
    校验 p_placement

  第二阶段 全合法化：
    make_full_legalizer(...).legalize(p_placement)
    # 此处产生 ClusteredNetlist + 合法坐标，写入 g_vpr_ctx

  第三阶段 详细布局：
    make_detailed_placer(...).optimize_placement()

  收尾：
    清理 placement / floorplanning 上下文
```

注意一个关键点：**AP 自己创建器件网格**。在传统流程里，`vpr_flow` 会调用 `vpr_create_device` 建网格；但走 AP 时这一步被跳过，因为 AP 在全合法化阶段（auto 尺寸时）会重新确定网格大小。

#### 4.1.3 源码精读

AP 总入口的声明只有一行，接受 `t_vpr_setup` 配置对象：

[analytical_placement_flow.h:12-17](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/analytical_placement_flow.h#L12-L17) — 声明 `run_analytical_placement_flow`，参数是用户配置 `t_vpr_setup`。

四个可替换实现的枚举定义在同一文件，便于一次看全：

[ap_flow_enums.h:16-20](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/ap_flow_enums.h#L16-L20) — `e_ap_analytical_solver`，选择 GP 内部的解析求解器。

[ap_flow_enums.h:29-33](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/ap_flow_enums.h#L29-L33) — `e_ap_partial_legalizer`，选择 GP 内部的部分合法化器。

[ap_flow_enums.h:41-45](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/ap_flow_enums.h#L41-L45) — `e_ap_full_legalizer`，选择第二阶段的全合法化器。

[ap_flow_enums.h:53-57](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/ap_flow_enums.h#L53-L57) — `e_ap_detailed_placer`，选择第三阶段的详细布局器。

三个子阶段的调用点集中在 `run_analytical_placement_flow` 实现的后半段，顺序就是「GP → 全合法化 → 详细布局」：

[analytical_placement_flow.cpp:296-302](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/analytical_placement_flow.cpp#L296-L302) — 第一阶段：调用 `run_global_placer` 得到 `PartialPlacement`。

[analytical_placement_flow.cpp:321-330](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/analytical_placement_flow.cpp#L321-L330) — 第二阶段：工厂 `make_full_legalizer` 创建全合法化器并 `legalize(p_placement)`。

[analytical_placement_flow.cpp:348-354](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/analytical_placement_flow.cpp#L348-L354) — 第三阶段：工厂 `make_detailed_placer` 创建详细布局器并 `optimize_placement()`。

AP 的命令行选项聚合在 `t_ap_opts`，字段与上面四个枚举一一对应，另有 `ap_timing_tradeoff`（时序 vs 线长权衡）、`ap_high_fanout_threshold`（高扇出网忽略阈值）等：

[vpr_types.h:1186-1200](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L1186-L1200) — `t_ap_opts` 的前几个字段：`doAP` 开关 + 四个实现类型选择。

#### 4.1.4 代码实践

**实践目标**：在主流程里定位 AP 的入口分派，并验证「开启 AP 会跳过传统打包/布局」这件事。

**操作步骤**：

1. 打开 `vpr/src/base/vpr_api.cpp`，定位到 `Analytical Place` 注释块（约第 457 行）。
2. 观察它如何用 `doAP == e_stage_action::DO` 做分派，并调用 `run_analytical_placement_flow`。
3. 再看紧接着的 `if (vpr_setup.APOpts.doAP != e_stage_action::DO)` 分支，理解为什么 AP 流程不走 `vpr_create_device`。
4. 打开 `vpr/src/base/setup_vpr.cpp` 第 322 行附近，看 `--analytical_place` 如何把 `doPacking`、`do_placement` 置为 `SKIP`、把 `doAP` 置为 `DO`。

关键源码点：

[vpr_api.cpp:457-466](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L457-L466) — 主流程里的 `Analytical Place` 分支：`doAP == DO` 时读入可选的平铺布局文件，然后调用 `run_analytical_placement_flow`。

[vpr_api.cpp:498-503](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L498-L503) — 仅当「不走 AP」时才调用 `vpr_create_device`；AP 自己负责建网格。

[setup_vpr.cpp:322-328](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/setup_vpr.cpp#L322-L328) — `--analytical_place` 打开后，传统打包与布局被 `SKIP`，`doAP` 被设为 `DO`，证明 AP 整体替代了「打包 + 布局」两阶段。

**需要观察的现象**：你会看到 AP 与「建器件网格」「打包」「布局」三处在代码里互斥——这正是「AP 是一条独立支路」的直接证据。

**预期结果**：能口述出「`--analytical_place` 触发 `doAP = DO`，进而跳过 `vpr_create_device`、跳过传统 `Packer`/`Placer`，改走 `run_analytical_placement_flow`」。

#### 4.1.5 小练习与答案

**练习 1**：四个枚举（求解器/部分合法化器/全合法化器/详细布局器）分别属于 AP 的哪个子阶段？

> **答案**：解析求解器与部分合法化器都属于**第一阶段（全局布局 GP）**内部——GP 用求解器求下界、用部分合法化器求上界；全合法化器属于**第二阶段**；详细布局器属于**第三阶段**。

**练习 2**：为什么 AP 流程里 `vpr_create_device` 不会被调用？

> **答案**：因为 AP 在全合法化阶段会自己重建器件网格（尤其 auto 尺寸时，打包前后所需尺寸可能变化），所以在 `vpr_api.cpp` 中用 `doAP != DO` 守卫，只在非 AP 流程里调 `vpr_create_device`。

---

### 4.2 APNetlist 与 PartialPlacement 建模

#### 4.2.1 概念说明

AP 不直接在 `AtomNetlist` 上求解，而是先把它抽象成一张更小的 `APNetlist`。原因有二：一是解析求解器要解线性方程组，规模越小越快；二是原子太细，需要把「想一起移动」的原子合并成一个搬运单元。

`APNetlist` 里的「块」是这样定义的（直接引自源码注释）：

> 在 AP 语境下，一个块是一组想要一起移动的原子（primitive）。例如，一个典型的块就是打包分子——预打包在一起的若干原子。

也就是说，AP 的块 = 一个或几个打包分子（进位链、LUT-FF 对等，见 u4-l2）。网的语义则是「块与块之间的逻辑连接」，由原子级连接推导而来；AP 不关心的网（高扇出、未使用）会被忽略，简化求解。

布局的中间结果存在 `PartialPlacement` 里。它的关键特点是**连续坐标**：x/y/layer 都是 `double`，因为解析求解器的输出是连续空间的最优解，不受整数网格约束；只有 `sub_tile` 是 `int`（它由合法化器决定，不在连续空间）。这张表「不必合法」，但保证所有块都在器件范围内、且固定块的位置被尊重。

#### 4.2.2 核心流程

`APNetlist` 的构造链：

```
AtomNetlist
   │  Prepacker 识别 pack pattern → t_pack_molecule（u4-l2）
   ▼
分子集合
   │  RamMapper 把同组 RAM 原子合并；UserPlaceConstraints 处理固定约束
   │  gen_ap_netlist_from_atoms(...)
   ▼
APNetlist（块 = 分子组，网 = 块间连接，忽略高扇出/无用网）
```

`PartialPlacement` 的生命周期：

```
构造时：所有块初始化为 (-1, -1, layer=0, sub_tile=0)；固定块从 APNetlist 载入位置
GP 中：解析求解器写入连续坐标 → 部分合法化器摊开重叠
查询时：get_containing_tile_loc() 用 floor 把连续坐标映射回整数瓦片
```

AP 把瓦片 `(1,1)` 视为中心在 `(1.5, 1.5)` 的格子；从 `double` 还原回整数瓦片时取 `floor`，即瓦片 `(1,1)` 接收 `[(1,1),(2,2))` 区间内的所有点。

#### 4.2.3 源码精读

`APNetlist` 直接继承第 3 单元的泛型基类，复用块/端口/引脚/网四元模型与 StrongId 设计：

[ap_netlist.h:58-75](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/common/ap_netlist.h#L58-L75) — `APNetlist` 类声明，继承 `Netlist<APBlockId, APPortId, APPinId, APNetId>`；类注释解释了「块 = 一组想一起移动的原子」。

AP 在基类拓扑之上新增的「原子语义」字段，集中在私有数据区：

[ap_netlist.h:199-210](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/common/ap_netlist.h#L199-L210) — 私有成员：每个块的分子列表 `block_molecules_`、块的可动性 `block_mobilities_`、固定块位置 `block_locs_`、AP 引脚↔原子引脚映射 `pin_atom_pin_`、AP 网↔原子网映射 `net_atom_net_`。

块的可动性只有两种，固定块的位置用一个带「未固定」哨兵值的结构表示：

[ap_netlist.h:37-55](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/common/ap_netlist.h#L37-L55) — `APFixedBlockLoc`（`-1` 表示该维未固定）与 `APBlockMobility`（`MOVEABLE`/`FIXED`）。

从原子网表生成 APNetlist 的入口，注释里点明了「同组 RAM 原子被合并成单个 AP 块」这一折叠规则：

[gen_ap_netlist_from_atoms.h:17-39](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/common/gen_ap_netlist_from_atoms.h#L17-L39) — `gen_ap_netlist_from_atoms`：用预打包结果生成 `APNetlist`，同 `PhysicalRamGroup` 的 RAM 原子折叠成一个可动块，高扇出网被忽略。

`PartialPlacement` 是个纯数据结构（struct），用结构数组（SoA）布局存放连续坐标：

[partial_placement.h:51-59](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/common/partial_placement.h#L51-L59) — `PartialPlacement` 的四个 SoA 成员：`block_x_locs`、`block_y_locs`、`block_layer_nums`（均为 `double`）与 `block_sub_tiles`（`int`）。

构造函数把所有块初始化为「未放置」并把固定块位置载入：

[partial_placement.h:72-96](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/common/partial_placement.h#L72-L96) — 构造函数：x/y 初值 −1、layer/sub_tile 初值 0；遍历固定块载入其约束位置。

连续坐标到整数瓦片的映射规则，源码里有详细注释：

[partial_placement.h:98-134](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/common/partial_placement.h#L98-L134) — `get_containing_tile_loc`：对 x/y/layer 取 `floor` 得到包含该点的瓦片；注释解释了「瓦片中心在 (x+0.5, y+0.5)」的约定。

#### 4.2.4 代码实践

**实践目标**：跟踪「原子 → 分子 → AP 块」的抽象过程，理解 APNetlist 比原子网表小多少。

**操作步骤**：

1. 在 `analytical_placement_flow.cpp` 的 `run_analytical_placement_flow` 中找到 `gen_ap_netlist_from_atoms` 调用与紧随其后的 `print_ap_netlist_stats(ap_netlist)`。
2. 阅读 `print_ap_netlist_stats` 实现（同文件顶部），它统计可动块数、固定块数、平均/最高扇出。

[analytical_placement_flow.cpp:43-77](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/analytical_placement_flow.cpp#L43-L77) — `print_ap_netlist_stats`：遍历块与网，打印 AP 块总数、可动/固定块数、平均与最高扇出。

3. （可选运行）若本地已构建 VPR，对一个中等规模电路分别跑传统流程和 AP 流程，对比日志里 `AP Blocks`/`AP Nets` 与原子网表的块数。

**需要观察的现象**：AP 块数通常远小于原子块数（因为多个原子被合并成分子、再合并成 AP 块），这正是解析求解器能在可接受时间内收敛的前提。

**预期结果**：能说清「APNetlist 通过分子折叠和高扇出网忽略，把求解规模从原子级降到块级」。如果无法本地运行，**待本地验证** AP 块数与原子块数的具体比值。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `PartialPlacement` 的 x/y 用 `double` 而 `sub_tile` 用 `int`？

> **答案**：x/y 由解析求解器在连续空间求出（还允许重叠、落在非法瓦片），所以必须是连续的 `double`；`sub_tile` 不由求解器决定，而是由合法化器在整数空间分配，因此用 `int` 即可。

**练习 2**：`APNetlist` 的块与 `AtomNetlist` 的块是什么关系？

> **答案**：一个 AP 块通常对应一个或几个打包分子（`PackMoleculeId`），每个分子又含若干原子（`AtomBlockId`）。AP 块内部存了 `block_molecules_` 列表，可逐级下溯到原子；同组 RAM 原子还会被进一步合并成单个 AP 块。

---

### 4.3 全局布局的 SimPL 迭代

#### 4.3.1 概念说明

第一阶段「全局布局」的核心算法是 **SimPL**（源自 ASIC 布局经典工作，源码注释里给了论文链接）。它的精髓是用两个「解」相互逼近：

- **下界解（lower-bound）**：解析求解器的输出。它在连续空间最小化 HPWL，质量很好，但严重非法——大量块重叠、落在错误瓦片。它代表「理论最优」，是解质量的下界。
- **上界解（upper-bound）**：把下界解喂给部分合法化器，摊开重叠、落到合法瓦片后的结果。它合法（或接近合法），但 HPWL 更差。它代表「当前能达成的实际质量」，是上界。

每一轮迭代，部分合法化器产生的上界解被当作「提示（hint）」反喂给求解器——告诉它「合法解长这样」。求解器据此加「锚点（anchor）」把可动块拉向上界位置，产生一个新的下界解；这个新下界解又会被合法化成新的上界解。随着迭代推进，锚点力越来越强，上下界相互靠近，最终收敛到一个「质量好且大致合法」的布局。

锚点力的强度随迭代指数增长，公式形如：

\[
\text{anchor\_w} = \text{mult}\cdot e^{\,\text{iter}/\text{exp\_fac}}
\]

迭代前期锚点弱，求解器自由优化 HPWL；后期锚点强，求解器被迫贴近合法解。

VPR 提供两种解析求解器：

- **QP_Hybrid**：最小化**二次** HPWL 目标 \(\sum ((x_{\max}-x_{\min})^2+(y_{\max}-y_{\min})^2)\)，用混合 Clique/Star 网模型（源自 FastPlace）。
- **LP_B2B**（默认）：最小化**线性** HPWL 目标 \(\sum ((x_{\max}-x_{\min})+(y_{\max}-y_{\min}))\)，用 Bound2Bound 网模型（源自 Kraftwerk2）。线性目标更贴近真实线长，但需要迭代重构方程组。

两者都用 Eigen 稀疏矩阵 + 共轭梯度法（CG）解大型线性方程组 \(Ax=b\)。

#### 4.3.2 核心流程

SimPL 主循环（`SimPLGlobalPlacer::place`）：

```
初始化 p_placement
for i in 0 .. max_num_iterations(=100):
    solver.solve(i, p_placement)              # 求下界解
    lb_hpwl = p_placement.get_hpwl()          # 下界 HPWL
    partial_legalizer.legalize(p_placement)   # 求上界解
    ub_hpwl = p_placement.get_hpwl()          # 上界 HPWL
    更新时序信息 & net 权重（关键网加权）
    若 ub_hpwl 优于历史最优：保存 best
    若 (ub_hpwl - lb_hpwl) / ub_hpwl < 0.01：收敛，退出
返回 best
```

收敛判据是上下界 HPWL 的相对间隙（relative gap）小于 `0.01`（经验值），或达到 100 次迭代上限。

> 小细节：如果用户用 `--read_flat_place` 提供了平铺布局文件，GP 会被整个跳过，直接把文件里的坐标转成 `PartialPlacement`（见 `run_global_placer` 里的 `flat_placement_info().valid` 分支）。这让你可以「外部工具求布局、VPR 做合法化」。

#### 4.3.3 源码精读

`GlobalPlacer` 是抽象基类，工厂目前只产出一种实现 `SimPLGlobalPlacer`：

[global_placer.h:94-118](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/global_placement/global_placer.h#L94-L118) — 类注释详细解释了 SimPL 的「下界=求解器、上界=合法化器、迭代逼近」思想。

`SimPLGlobalPlacer` 内部正好持有这三件套：求解器、密度管理器、部分合法化器：

[global_placer.h:120-148](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/global_placement/global_placer.h#L120-L148) — `SimPLGlobalPlacer` 类与它的三个关键成员 `solver_`、`density_manager_`、`partial_legalizer_`，以及两个收敛常数 `max_num_iterations_=100`、`target_hpwl_relative_gap_=0.01`。

主循环里「求解器求下界、合法化器求上界」的成对调用，是 SimPL 最核心的几行：

[global_placer.cpp:408-424](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/global_placement/global_placer.cpp#L408-L424) — 迭代体：`solver_->solve` 后取 `lb_hpwl`，`partial_legalizer_->legalize` 后取 `ub_hpwl`。

每轮还要刷新时序与网权重，让关键网在下一轮求解中获得更高权重（时序驱动）：

[global_placer.cpp:429-436](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/global_placement/global_placer.cpp#L429-L436) — 用 GP 当前布局更新时序信息，再让求解器据此更新网权重。

收敛判据与退出：

[global_placer.cpp:470-483](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/global_placement/global_placer.cpp#L470-L483) — 计算上下界 HPWL 的相对间隙，小于 `target_hpwl_relative_gap_` 即 `break`。

两种解析求解器的目标函数与网模型，源码注释写得很清楚：

[analytical_solver.h:231-253](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/global_placement/analytical_solver.h#L231-L253) — `QPHybridSolver`：最小化二次 HPWL，混合 Clique/Star 网模型（FastPlace）。

[analytical_solver.h:440-455](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/global_placement/analytical_solver.h#L440-L455) — `B2BSolver`：最小化线性 HPWL，Bound2Bound 网模型（Kraftwerk2），需迭代重构方程组。

求解器的统一接口是 `solve(iteration, p_placement)`——迭代号越大，锚点（来自上一轮上界解）拉力越强：

[analytical_solver.h:72-90](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/global_placement/analytical_solver.h#L72-L90) — `solve` 接口注释：用传入的 `p_placement` 作为「合法解提示」，迭代号越大求解器越受其影响。

#### 4.3.4 代码实践

**实践目标**：通过运行 AP 流程，观察 SimPL 上下界 HPWL 逐轮逼近的过程。

**操作步骤**：

1. 确保已用 `make -j8 vpr` 构建（且环境装了 Eigen，否则求解器退化为 Identity，看不到收敛现象）。
2. 选一个示例电路与架构（参考 u1-l4 的 `run_vtr_flow.py` 或 quickstart），直接调 vpr 并开 AP 与详细日志：

   ```shell
   ./build/vpr/vpr <architecture.xml> <circuit.blif> \
       --analytical_place on \
       --ap_verbosity 5 \
       --route_chan_width 100
   ```

3. 在日志里找 `AP Global Placer` 段落，观察每个迭代号对应的 `lb_hpwl`（下界）与 `ub_hpwl`（上界）两列。

**需要观察的现象**：随着迭代号增大，`lb_hpwl` 与 `ub_hpwl` 应当相互靠近；当相对间隙跌破 1% 时循环提前结束（未必跑满 100 轮）。

**预期结果**：能在日志中指认出哪一列是下界、哪一列是上界，并解释二者为何会收敛。若本地未装 Eigen 或无法运行，**待本地验证** 具体数值。

> 命令行选项速查（默认值见 `read_options.cpp`）：`--ap_analytical_solver`（默认 `lp-b2b`）、`--ap_partial_legalizer`（默认 `bipartitioning`）、`--ap_timing_tradeoff`（默认 `0.5`，0=纯线长、1=纯时序）、`--ap_high_fanout_threshold`（默认 `256`）。

#### 4.3.5 小练习与答案

**练习 1**：SimPL 为什么要把上界解反喂给求解器？

> **答案**：纯求解器的下界解严重非法（大量重叠）。把上一轮合法化后的上界解作为「锚点提示」喂回去，求解器在优化 HPWL 的同时被迫贴近一个已知合法的解；迭代越多次，锚点拉力越强，最终的下界解就更接近合法，从而下一轮上界解质量也更好——上下界互相拉动收敛。

**练习 2**：QP_Hybrid 与 LP_B2B 在目标函数上的根本区别是什么？

> **答案**：QP_Hybrid 最小化二次 HPWL（平方和，等价于最小化方差，有闭式线性系统可一次求解）；LP_B2B 最小化线性 HPWL（更贴近真实线长，但目标不可导，需用 Bound2Bound 网模型把方程组反复重构、迭代逼近）。线性目标质量通常更好但更慢，故被设为默认。

---

### 4.4 与传统打包布局流程的衔接

#### 4.4.1 概念说明

AP 的前两阶段都在「原子层 + 连续坐标」上工作，但 VPR 的布线器（第 6 单元）只认聚簇块级（`ClusteredNetlist`）和整数网格上的合法坐标。因此**第二阶段「全合法化」不仅要摆位置，还必须把原子打包成聚簇块**——这正是 AP「整合打包与布局」的体现：打包决策发生在看到全局布局之后。

三种全合法化器代表了三种打包策略：

- **APPack（默认）**：直接复用 VPR 的传统 `Packer`（u4-l3 的贪心聚簇器），但把平铺布局作为额外线索喂进去——让聚簇器倾向于把物理邻近的原子装进同一块、排斥离得远的原子。打包完再用布局器摆块。
- **FlatRecon**：不调用传统 Packer，而是自己设计的三趟聚类（自聚类 → 邻域聚类 → 孤儿窗口聚类），目标是重建一个「尽量贴近输入平铺布局」的聚簇布局。
- **Naive**：把落在同一瓦片的原子直接聚成一簇，再尝试把簇摆到那个瓦片；找不到位置就退而求其次找任何能放下的地方。

第三阶段「详细布局」则在合法解上做局部精修，复用传统 VPR 的退火器或新增的二分匹配器：

- **Annealer（默认）**：把合法解当作退火器的初始布局，复用布局阶段的退火选项（u5-l1）跑一遍，相当于「AP 给一个好起点，退火精修」。
- **WindowedBiMatching**：在局部窗口内用二分匹配优化。
- **Identity**：什么都不做（占位/调试用）。

#### 4.4.2 核心流程

衔接关系（AP 替代了哪些传统阶段）：

```
传统流程：  Pack(Packer) ──→ ClusteredNetlist ──→ Place(退火) ──→ Route
AP 流程：   run_analytical_placement_flow:
              GP(求解器+部分合法化) ──→ PartialPlacement
              Full Legalize(打包+落位) ──→ ClusteredNetlist + 合法坐标  ┘ 二者替代
              Detailed Place(退火/匹配精修)                                  ┘ Pack+Place
            ──→ Route（与传统流程完全相同）
```

全合法化器写出的产物直接进入全局上下文：`g_vpr_ctx.clustering().clb_nlist`（聚簇网表）与 `g_vpr_ctx.placement()`（块坐标），此后布线阶段无感知地消费它们。

#### 4.4.3 源码精读

`FullLegalizer` 抽象基类定义了「输入部分布局、输出完全合法的聚簇与布局」契约：

[full_legalizer.h:29-64](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/full_legalization/full_legalizer.h#L29-L64) — `FullLegalizer` 类与纯虚方法 `legalize(const PartialPlacement&)`；类注释点明产物是「可被 VTR 其余流程布线的合法聚簇与布局」。

三种全合法化器中，FlatRecon 的三趟聚类策略在类注释里描述得最完整（自聚类先 SKIP_INTRA_LB_ROUTE 再 FULL、邻域聚类查 8 邻接瓦片、孤儿窗口 BFS）：

[full_legalizer.h:119-159](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/full_legalization/full_legalizer.h#L119-L159) — `FlatRecon`：用平铺布局重建聚簇布局，三趟聚类 + 按原子质心初摆。

APPack 的设计意图——「用平铺布局改进 Packer 与 Placer」：

[full_legalizer.h:340-367](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/full_legalization/full_legalizer.h#L340-L367) — `APPack`：平铺布局引导打包与布局的思路说明。

`DetailedPlacer` 抽象基类与默认实现 `AnnealerDetailedPlacer`（直接复用 VPR 退火器）：

[detailed_placer.h:16-33](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/detailed_placement/detailed_placer.h#L16-L33) — `DetailedPlacer` 基类与纯虚 `optimize_placement`。

[detailed_placer.h:59-97](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/detailed_placement/detailed_placer.h#L59-L97) — `AnnealerDetailedPlacer`：把合法解作为初始布局喂给 VPR 退火器，复用布局阶段选项。

全合法化在主入口里的调用，紧接着会打印资源用量——因为此时聚簇网表已成型：

[analytical_placement_flow.cpp:332-336](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/analytical_placement_flow.cpp#L332-L336) — 全合法化后打印资源用量与器件利用率（聚簇网表此时已可用）。

收尾时清理布局与地板规划上下文，与传统布局阶段结束时的清理一致：

[analytical_placement_flow.cpp:356-359](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analytical_place/analytical_placement_flow.cpp#L356-L359) — 流程末尾清理 `placement` 与 `floorplanning` 上下文。

回到主流程，AP 结束后会像传统布局一样写出 `.place` 文件、设置时钟网络，随后布线阶段照常进行——证明 AP 产物与传统布局产物在接口上完全等价：

[vpr_api.cpp:477-495](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L477-L495) — AP 完成后打印 `.place` 文件、设置时钟网络，为后续布线做准备。

#### 4.4.4 代码实践

**实践目标**：对比「传统流程」与「AP 流程」在同一电路上的布局结果（线长/时序），直观感受 AP 的价值与代价。

**操作步骤**：

1. 选定一个电路 `<circuit.blif>` 与架构 `<arch.xml>`（可用 quickstart 示例）。
2. 跑传统流程（基线）：

   ```shell
   ./build/vpr/vpr <arch.xml> <circuit.blif> --route_chan_width 100 \
       --outfile_prefix trad_
   ```

3. 跑 AP 流程：

   ```shell
   ./build/vpr/vpr <arch.xml> <circuit.blif> --analytical_place on \
       --route_chan_width 100 --outfile_prefix ap_
   ```

4. 对比两次运行日志末尾的最终线长（HPWL/ routed WL）与时序（critical path delay）；对比 `trad_*.place` 与 `ap_*.place` 的块分布。

**需要观察的现象**：AP 通常在大电路上线长更优（因为全局求解优于贪心退火），但 GP 阶段会多花时间（解线性方程组）；小电路上二者差异可能不明显。

**预期结果**：列出一张「传统 vs AP」的线长/时序/运行时间对比表。若本地无法构建运行，**待本地验证** 具体数值，但应能说清「AP 用更长 compile 时间换更优布局质量」这一权衡。

> 想只看「外部平铺布局 + VPR 合法化」？用 `--read_flat_place <file.fplace>` 跳过 GP，再选 `--ap_full_legalizer flat-recon` 观察重建效果；或用 `--write_flat_place` 把 GP 产物导出供下次复用。

#### 4.4.5 小练习与答案

**练习 1**：为什么说 AP「整合了打包与布局」，而传统流程没有？

> **答案**：传统流程里打包在布局之前、且看不到任何物理位置信息，聚簇决策是「盲打」。AP 的全合法化器（尤其 APPack/FlatRecon）在做聚簇决策时已经手握全局布局给出的每个原子的连续坐标，能按物理邻近性聚簇；打包与布局在同一阶段内协同完成。

**练习 2**：AP 流程结束后，布线阶段（第 6 单元）需要为它做特殊适配吗？

> **答案**：不需要。全合法化器已经把结果写进标准的 `g_vpr_ctx.clustering().clb_nlist`（聚簇网表）与 `g_vpr_ctx.placement()`（合法坐标），布线器照常消费这两个全局上下文，与处理传统布局产物完全一样——这正是 AP 在主流程里仅作为一个分派分支就能接入的原因。

## 5. 综合实践

把本讲的三阶段串起来，完成一次「读懂 + 改参数 + 看效果」的小研究：

1. **读图**：在 `analytical_place/` 目录下，画出 `run_analytical_placement_flow` 的数据流图，标出 `APNetlist`、`PartialPlacement`、`ClusteredNetlist`、`PlacementContext` 各自在哪个子阶段被创建、被消费。
2. **改参数**：选一个能跑通的示例电路，固定其余选项，分别用三组配置跑 AP：
   - 求解器对比：`--ap_analytical_solver qp-hybrid` vs `lp-b2b`（默认）；
   - 全合法化器对比：`--ap_full_legalizer naive` vs `appack`（默认）vs `flat-recon`；
   - 时序权衡对比：`--ap_timing_tradeoff 0.0` vs `0.5`（默认）vs `1.0`。
3. **看效果**：记录每组的最终线长、关键路径延迟、GP 迭代轮数、总运行时间，整理成表，尝试解释每组差异的成因（例如：`naive` 为何通常线长最差？`timing_tradeoff=1.0` 为何线长可能变差但时序变好？）。

通过这个实践，你应当能用自己的话讲清「AP 三阶段各自如何影响最终质量与运行时间」，并为后续阅读 GP 内部求解器（`analytical_solver.cpp`）、密度管理器（`flat_placement_density_manager`）等更深的源码打下基础。

## 6. 本讲小结

- AP 是一条**替代**传统「打包 + 布局」的独立支路：开启 `--analytical_place` 后，`setup_vpr.cpp` 把传统打包/布局置 `SKIP`、`doAP` 置 `DO`，主流程改走 `run_analytical_placement_flow`。
- AP 分三阶段：**全局布局**（解析求解器 + 部分合法化器，SimPL 上下界迭代，产出连续坐标的 `PartialPlacement`）→ **全合法化**（把平铺布局打包成聚簇块并落到合法瓦片，产出 `ClusteredNetlist` + 合法坐标）→ **详细布局**（合法解上局部精修）。
- 核心数据结构 `APNetlist` 复用第 3 单元的 `Netlist` 泛型基类，块 = 一组想一起移动的原子（打包分子/RAM 组），网 = 块间连接；`PartialPlacement` 用 SoA 的 `double` 连续坐标存放「不必合法」的中间布局。
- 全局布局的灵魂是 SimPL：求解器求高质量但非法的下界解，部分合法化器求合法但较次的上界解，两者通过锚点反喂相互逼近，收敛判据是上下界 HPWL 相对间隙 < 1%。
- 每个子阶段都有多种可替换实现，由 `ap_flow_enums.h` 的四个枚举选择，默认组合是 `lp-b2b` + `bipartitioning` + `appack` + `annealer`。
- AP 的产物（聚簇网表 + 合法坐标）写入标准全局上下文，布线阶段无感知地复用——这正是它能作为主流程一个分派分支存在的原因。

## 7. 下一步学习建议

- **深入 GP 内部**：阅读 `global_placement/analytical_solver.cpp` 中 `QPHybridSolver::solve` 与 `B2BSolver::b2b_solve_loop`，理解 Clique/Star 与 Bound2Bound 网模型如何组装稀疏线性系统、锚点如何随迭代加强。
- **部分合法化机制**：读 `partial_legalizer.cpp` 与 `flat_placement_density_manager.h`，理解「质量（mass）/容量（capacity）」模型与 `--ap_partial_legalizer_target_density` 如何控制摊开程度。
- **FlatRecon 的三趟聚类**：读 `full_legalizer.cpp` 中 `FlatRecon::create_clusters`，对照 u4-l4 的簇内合法化（`SKIP_INTRA_LB_ROUTE` vs `FULL`）看它如何复用 `ClusterLegalizer`。
- **APPack 与传统 Packer 的关系**：对比 `full_legalizer.cpp` 的 APPack 实现与 u4-l3 的 `GreedyClusterer`，看平铺布局线索如何转化为聚簇增益项。
- **退火器复用**：读 `detailed_placer.cpp` 的 `AnnealerDetailedPlacer`，结合 u5-l1 的退火框架，理解「AP 提供好初解、退火精修」如何衔接。
