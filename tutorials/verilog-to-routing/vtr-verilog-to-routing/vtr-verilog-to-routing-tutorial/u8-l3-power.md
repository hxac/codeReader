# 功耗分析

## 1. 本讲目标

在前几讲里，我们已经能让 VPR 把一个电路**布通**（u6 布线）并报出**关键路径时序**（u7 时序分析）。但「速度」与「面积」之外，FPGA 还有一个同样重要的评估维度：**功耗（Power）**。本讲回答三个问题：

1. VPR 是在流程的哪个时刻、用什么入口去估算整块 FPGA 的功耗的？（**功耗估算总流程**）
2. 这个总功耗是怎么**分解**成布线 / 时钟 / 逻辑块三大块，再进一步拆成动态（dynamic）与漏电（leakage）的？（**组件功耗分解**）
3. 估算动态功耗必须知道「每根线翻转多频繁」，估算漏电必须知道「晶体管特性」——这两类 VPR 自己算不出来的外部数据从哪来？（**活动性与工艺数据**）

学完本讲，你应当能：看懂 VPR 的 `--power` 开关如何触发一次独立的分析流程；读懂 `.power` 报告里三层 breakdown 的含义；说清楚 ACE2、活动性文件 `.act` 与 CMOS 工艺 XML 三者在功耗流程里的分工。

## 2. 前置知识

本讲默认你已经掌握 u7（时序分析）的核心结论：**布线之后**才能做静态时序分析，时序分析会产出一条关键路径及其延迟 `T_crit`。功耗分析与时序分析是「同一批收尾工作」的两个分支——它们都依赖已经完成的布局布线结果。此外请回忆两个贯穿全书的概念：

- **架构驱动**：目标 FPGA 的物理结构来自架构 XML，VPR 算法绝不硬编码架构假设。功耗同样如此：晶体管尺寸、线电容、PN 比例这些「物理量」都来自外部 XML，VPR 只负责「在给定物理量下算账」。
- **全局状态总线 `g_vpr_ctx`**：每个阶段把自己的产物写进对应的子上下文。功耗阶段读写的是 [`PowerContext`](#)，它和 AtomContext、RoutingContext、PlacementContext 并列。

下面补充三个本讲会用到的、但前序讲义没细讲的物理常识：

- **动态功耗（dynamic power）**：电容每次充放电都消耗能量，和「信号翻转有多频繁」成正比。一次 0→1→0 的翻转给负载电容 $C$ 充了又放了 $C V_{dd}$ 的电荷，消耗能量 $\tfrac{1}{2} C V_{dd}^2$。所以动态功耗既依赖工艺（$C$、$V_{dd}$），又依赖**电路行为**（翻转频率）。后者正是 VPR 算不出来的部分。
- **漏电功耗 / 静态功耗（leakage power）**：晶体管关断时仍有的亚阈值漏电流，只要上电就一直存在，和翻转无关，只依赖工艺与器件尺寸。
- **信号活动性（activity）**：用两个量刻画一根线长期的行为——**静态概率** $P$（长期来看取值为 1 的概率）与**翻转密度** $D$（平均每个时钟周期翻转几次，时钟本身 $D=2$）。两者都需要在综合后通过**仿真/概率传播**估计，VPR 不做这件事，交给 ACE2。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [`vpr/src/power/power.h`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power.h) | 功耗模块顶层头文件：返回码枚举、工艺/晶体管数据结构、`power_init`/`power_total`/`power_uninit` 三大入口声明 |
| [`vpr/src/power/power.cpp`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power.cpp) | 顶层实现：`power_init` 初始化、`power_total` 跑三大组件并把结果相加、`power_usage_routing/blocks/clock` 各组件估算函数 |
| [`vpr/src/power/power_components.h`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power_components.h) | 组件分解：`e_power_component_type` 枚举定义所有功耗类别，`t_power_breakdown` 容器，各类原语（FF/LUT/MUX/buffer）的功耗函数声明 |
| [`vpr/src/power/power_components.cpp`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power_components.cpp) | 组件分解实现：`power_component_add_usage` 把每个组件功耗累加进全局跟踪表 |
| [`vpr/src/power/power_cmos_tech.h`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power_cmos_tech.h) | CMOS 工艺数据：`power_tech_init` 读 XML，`power_find_transistor_info` 按尺寸查表 |
| [`vpr/src/power/power_util.cpp`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power_util.cpp) | 工具函数：`t_power_usage` 的归零、累加、求和（dynamic+leakage） |
| [`vpr/src/base/vpr_api.cpp`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp) | 主流程编排：`vpr_power_estimation` 是功耗分析在 VPR 中的唯一入口；初始化期还负责读入活动性文件 |
| [`vpr/src/base/read_activity.cpp`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_activity.cpp) | 活动性文件读入：把 ACE2 产出的 `.act` 解析成 `AtomNetId → t_net_power` 映射 |
| [`vpr/src/base/vpr_context.h`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h) | `PowerContext`：功耗阶段的全局状态容器 |
| [`libs/libarchfpga/src/physical_types.h`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h) | `t_power_usage`（dynamic/leakage）、`t_power_arch`（架构 XML 中的功耗参数）、`t_net_power`（概率/密度）三结构定义 |
| [`vtr_flow/tech/PTM_45nm/45nm.xml`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/tech/PTM_45nm/45nm.xml) | CMOS 工艺 XML 样例：晶体管漏电、电容、Vdd、温度 |

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：先讲清「总流程」是怎么被触发、怎么走完一遍；再讲它内部如何「分解」；最后讲它依赖的两类「外部数据」从哪来。

---

### 4.1 功耗估算总流程

#### 4.1.1 概念说明

功耗估算在 VPR 中是一个**可选的、布线后的独立分析步骤**。它不是布局布线算法的一部分（不会反过来影响布局布线决策），而是布线完成后「看一眼这块已经布好的 FPGA 大概耗多少电」。因此它：

- **必须是后置的**：需要已经完成的布局（每块在哪）、布线（每根网走哪些导线、驱动哪些 mux）、时序（关键路径周期 `T_crit` 决定时钟频率）。
- **必须是可选的**：默认关闭，由 `--power` 开关开启；而且开启它必须额外喂两个外部文件（活动性 + CMOS 工艺），缺一不可。
- **有自己的生命周期**：和时序分析一样，走一条 `init → 跑 → uninit` 的三步路，用完即释放内存。

#### 4.1.2 核心流程

整个功耗估算在 VPR 内部的调用链如下（从触发到结束）：

```
vpr_flow()
  └─ vpr_analysis_flow()              # 布线后的分析阶段（含时序报告）
       └─ if (PowerOpts.do_power):    # 由 --power 开关控制
            vpr_power_estimation(...)  # 本讲的唯一入口
              ├─ 1. 取 T_crit（关键路径延迟）与 channel_width 写入 solution_inf
              ├─ 2. power_init(PowerFile, CmosTechFile, Arch, RoutingArch)
              │      ├─ power_tech_init(CmosTechFile)   # 读 CMOS 工艺 XML
              │      ├─ power_components_init()          # 准备组件跟踪表
              │      ├─ power_calibrate()                # 用 SPICE 校准数据标定
              │      └─ power_sizing_init()              # 为各组件定尺寸
              ├─ 3. power_total(...)                     # 真正算：布线+时钟+逻辑块
              └─ 4. power_uninit()                       # 释放
```

注意一个**时机细节**：活动性文件（`.act`）的读入**不在** `vpr_power_estimation` 里，而在更早的初始化阶段 `vpr_init_with_options` 里就完成了——因为读活动性需要已经建好的原子网表 `AtomNetlist`，而那是在读 BLIF 之后立刻就有的。

#### 4.1.3 源码精读

**入口声明**——功耗模块对外只暴露三个函数，定义在 [`power.h:L304-L312`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power.h#L304-L312)。`power_init` 接收功耗输出文件路径、CMOS 工艺文件路径、架构指针与路由架构；`power_total` 是真正算总功耗的函数；`power_uninit` 负责释放。

**触发点**——在主流程的 `vpr_analysis_flow` 末尾，由 `PowerOpts.do_power` 守卫，见 [`vpr_api.cpp:L1641-L1643`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1641-L1643)。这说明功耗与时序报告是同一批「收尾分析」的两个并列分支。

**入口实现 `vpr_power_estimation`**——见 [`vpr_api.cpp:L1656-L1716`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1656-L1716)。它先把布线/时序结果里的两个关键标量写进 `PowerContext.solution_inf`：

- [`vpr_api.cpp:L1668`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1668)：`T_crit = timing_info.least_slack_critical_path().delay()`——关键路径延迟，决定时钟频率 $f = 1/T_{crit}$，进而决定动态功耗里的 $f$ 项。
- [`vpr_api.cpp:L1672`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1672)：`channel_width = route_status.chan_width()`——最终布线用的通道宽度，影响布线功耗规模。

接着 [`vpr_api.cpp:L1681-L1682`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1681-L1682) 调用 `power_init`，两个文件实参正是来自 `FileNameOpts.PowerFile`（功耗报告输出路径）和 `FileNameOpts.CmosTechFile`（CMOS 工艺文件）。随后 [`vpr_api.cpp:L1693`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1693) 调用 `power_total` 跑估算。注意第 1661 行还有一个**前置约束**：功耗分析只支持单时钟电路，多时钟直接 `VPR_FATAL_ERROR`。

**活动性文件读入时机**——见 [`vpr_api.cpp:L333-L338`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L333-L338)：读 BLIF 建好 `AtomNetlist` 之后，若 `do_power` 为真，立刻 `read_activity` 把 `.act` 解析进 `PowerContext.atom_net_power`。这发生在所有阶段开始之前，可见活动性数据是「预先备好」的全局输入。

**`power_total` 主算函数**——见 [`power.cpp:L1705-L1772`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power.cpp#L1705-L1772)。它依次算三大组件、各自累加进 `total_power`，最后写报告。第 1717 行还拒绝双向布线架构（`BI_DIRECTIONAL`），是模型适用性的硬约束。

#### 4.1.4 代码实践

**实践目标**：验证「功耗估算所需输入」的来源，把第 4.1.2 节的调用链落到具体代码行。

**操作步骤**：

1. 打开 [`vpr_api.cpp:L1656`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1656) 的 `vpr_power_estimation`。
2. 列出它的四个形参来源：`vpr_setup`（配置）、`Arch`（架构）、`timing_info`（时序→`T_crit`）、`route_status`（布线→`channel_width`）。
3. 定位 `power_init` 的两个文件实参 `FileNameOpts.PowerFile` 与 `FileNameOpts.CmosTechFile`，再回到 [`vpr_api.cpp:L337`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L337) 找到活动性文件 `FileNameOpts.ActFile`。
4. 用 [`read_options.cpp:L3631-L3643`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L3631-L3643) 确认这三个文件分别对应命令行 `--power`（开关）、`--tech_properties`（CMOS 工艺）、`--activity_file`（活动性）。

**需要观察的现象**：`vpr_power_estimation` 自身只显式读了 `PowerFile` 与 `CmosTechFile` 两个文件；活动性文件 `ActFile` 在更早的初始化阶段就被消费了。

**预期结果**：能画出一张「输入来源表」：

| 输入 | 命令行选项 | 在哪里读入 | 来源 |
|------|-----------|-----------|------|
| 活动性 `.act` | `--activity_file` | `vpr_init_with_options`（`vpr_api.cpp:337`） | ACE2 估算 |
| CMOS 工艺 XML | `--tech_properties` | `power_init`（`vpr_api.cpp:1681`） | PTM 工艺模型 |
| 功耗报告 `.power` | （输出） | `power_init` 内 `fopen` | VPR 自己写 |
| `T_crit` | （非文件） | `vpr_power_estimation:1668` | 时序分析 |
| `channel_width` | （非文件） | `vpr_power_estimation:1672` | 布线结果 |

> 本实践为源码阅读型实践，无需运行；若想实地触发，参考 4.3.4 节的综合实践。

#### 4.1.5 小练习与答案

**练习 1**：为什么功耗分析放在 `vpr_analysis_flow` 而不是 `vpr_route_flow` 里？

> **答案**：功耗估算依赖已经完成的**最终布线结果**（每根网的实际走线、驱动 mux）与**时序结果**（`T_crit`）。`analysis_flow` 是布线之后的收尾阶段，此时这两个产物都已就绪；若放进 `route_flow`，布线还在迭代、结果未定，无法估算。

**练习 2**：`power_total` 的返回类型是 `e_power_ret_code`（见 [`power.h:L38-L42`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power.h#L38-L42)），它有哪三种取值，分别表示什么？

> **答案**：`POWER_RET_CODE_SUCCESS`（无错误无警告）、`POWER_RET_CODE_WARNINGS`（完成但有警告）、`POWER_RET_CODE_ERRORS`（失败）。判定依据是输出日志里 error/warning 计数器（见 `power.cpp:1765-1770`）。

---

### 4.2 组件功耗分解

#### 4.2.1 概念说明

`power_total` 算出的不是一个「整片 FPGA 耗 X 瓦」的孤零零数字——那对架构研究者毫无用处。VTR 的目标受众是 FPGA 架构研究者，他们需要回答「功耗到底花在哪了，改架构哪里能省电」。因此 VPR 把总功耗**层层分解**：

- **第一层**：按物理大件拆成**布线（routing）/ 时钟（clock）/ 逻辑块（logic blocks, PB）**三大块。
- **第二层**：每大块再细分，例如布线拆成开关盒（SB）、连接盒（CB）、全局连线（GLB_WIRE）；逻辑块拆成原语（LUT/FF）、本地互联 mux、本地 buffer/线。
- **第三层**：每一项又拆成**动态功耗（dynamic）**与**漏电功耗（leakage）**两个标量。

这套分解用两个数据结构承载：`t_power_usage` 描述「一项」的功耗，`e_power_component_type` 枚举固定了「有哪些项」。

#### 4.2.2 核心流程

功耗分解的工作机制是「**边算边记账**」：

```
power_total()
  total_power = 0
  for 每个大组件 (routing / clock / blocks):
      sub = power_usage_<组件>(...)        # 算这一大块的 t_power_usage
      power_add_usage(&total_power, &sub)  # 累加进总数
      power_component_add_usage(&sub, <组件枚举>)  # 同时记进组件账本
  power_component_add_usage(&total_power, POWER_COMPONENT_TOTAL)
  power_print_breakdown_summary(...)       # 把账本打印成报告
```

关键点：

- **组件账本** `by_component.components[]` 是一个以 `e_power_component_type` 为下标的定长数组，每个槽存一个 `t_power_usage`。`power_component_add_usage` 就是往某个槽里加（见 [`power_components.cpp:L71-L76`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power_components.cpp#L71-L76)）。
- **`t_power_usage` 的代数**：`power_add_usage` 逐项相加（[`power_util.cpp:L52-L55`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power_util.cpp#L52-L55)），`power_sum_usage` 返回 dynamic+leakage（[`power_util.cpp:L62-L64`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power_util.cpp#L62-L64)）。账本里所有「子项之和」应当等于「父项」，例如 `ROUTE_SB + ROUTE_CB + ROUTE_GLB_WIRE ≈ ROUTING`。

动态功耗的核心物理公式（把工艺量与活动性量联系起来）：

\[
P_{\text{dynamic}} = \sum_{\text{net}} \frac{D_{\text{net}} \cdot C_{\text{load}} \cdot V_{dd}^2}{2 \cdot T_{\text{period}}}
\]

其中 $D_{\text{net}}$ 是该网每周期翻转次数（来自活动性文件）、$C_{\text{load}}$ 是负载电容（来自工艺 + 架构）、$V_{dd}$ 与 $T_{\text{period}}=T_{\text{crit}}$ 来自工艺与时序。漏电功耗则与 $D$ 无关：

\[
P_{\text{leakage}} = \sum_{\text{transistor}} I_{\text{subthreshold}} \cdot V_{dd}
\]

#### 4.2.3 源码精读

**组件枚举**——[`power_components.h:L42-L63`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power_components.h#L42-L63) 定义了 `e_power_component_type`，注意它的层次设计：`POWER_COMPONENT_TOTAL` 是全 FPGA 总功耗；下面 `ROUTING`（总）再细分 `ROUTE_SB`/`ROUTE_CB`/`ROUTE_GLB_WIRE`；`CLOCK` 细分 `CLOCK_BUFFER`/`CLOCK_WIRE`；`PB`（逻辑块）细分 `PB_PRIMITIVES`/`PB_INTERC_MUXES`/`PB_BUFS_WIRE`/`PB_OTHER`。这个枚举的顺序就是 `.power` 报告里 breakdown 表的骨架。

**账本容器**——[`power_components.h:L67-L71`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power_components.h#L67-L71)：`t_power_breakdown` 内部就是一个 `t_power_usage*` 数组，`t_power_components` 只是它的别名。它被 [`PowerContext`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L373-L387) 以 `by_component` 成员持有（第 386 行）。

**`t_power_usage` 结构**——定义在 [`physical_types.h:L316-L323`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L316-L323)，只有 `dynamic` 和 `leakage` 两个 float。这就是「一项功耗」的全部信息。

**边算边记账**——在 `power_total` 中，三大组件各算一次后立刻 `power_component_add_usage`，见 [`power.cpp:L1725-L1739`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power.cpp#L1725-L1739)：布线 → `POWER_COMPONENT_ROUTING`、时钟 → `POWER_COMPONENT_CLOCK`、逻辑块 → `POWER_COMPONENT_PB`，最后总数 → `POWER_COMPONENT_TOTAL`。

**逻辑块功耗函数 `power_usage_blocks`**——见 [`power.cpp:L597-L636`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power.cpp#L597-L636)：遍历器件网格**每个根瓦片位置**的每个 capacity 槽（第 609-618 行），取出摆在那里的聚簇块 `iblk`（第 622 行，来自 `PlacementContext`），再调 `power_usage_pb` 算这个块内部的功耗（第 632 行）。注意空位置也要算（用 `pick_logical_type` 选个默认逻辑块类型，对应未使用的逻辑块的漏电）——这正是漏电功耗「只要上电就有」的体现。

**时钟功耗函数 `power_usage_clock`**——见 [`power.cpp:L641-L671`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power.cpp#L641-L671)：对单时钟电路，若架构没指定时钟活动性，默认 `dens=2, prob=0.5`（即标准时钟，每周期翻转两次）、`period=T_crit`（第 658-665 行）。

**原语级估算函数**——[`power_components.h:L82-L92`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power_components.h#L82-L92) 声明了 FF、LUT、多级 mux、buffer 的功耗函数。以 FF 为例，`power_usage_ff` 的参数 `D_prob/D_dens`（数据端概率/密度）、`Q_prob/Q_dens`（输出端）、`clk_prob/clk_dens`（时钟）正是把活动性喂给原语模型。

#### 4.2.4 代码实践

**实践目标**：理解「账本」如何保证子项之和等于父项。

**操作步骤**：

1. 读 [`power.cpp:L1725-L1737`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power.cpp#L1725-L1737)，确认每个大组件都被同时加进了 `total_power` 和对应的组件槽。
2. 读 [`power_components.cpp:L48-L56`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power_components.cpp#L48-L56) 的 `power_components_init`：账本数组长度是 `POWER_COMPONENT_MAX_NUM`，全部初始化为 0。
3. 跟踪一个具体原语：在 `power_usage_blocks` → `power_usage_pb` → `power_usage_primitive` 调用链中，找到一个 FF 原语调用 `power_usage_ff`（[`power_components.h:L82`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power_components.h#L82)），确认它的 `D_dens` 参数最终来自活动性数据 `atom_net_power`。

**需要观察的现象**：`power_component_add_usage` 只往「一个大组件槽」加（如 `POWER_COMPONENT_PB`），而该大组件内部的细分槽（`PB_PRIMITIVES` 等）是在各 `power_usage_*` 内部更细的调用里分别加的。

**预期结果**：能解释为何报告里 `PB = PB_PRIMITIVES + PB_INTERC_MUXES + PB_BUFS_WIRE + PB_OTHER`（在数值上近似成立）。**待本地验证**：实际跑一次带 `--power` 的流程，打开 `.power` 报告核对这一等式。

#### 4.2.5 小练习与答案

**练习 1**：`power_usage_blocks` 为什么要遍历器件网格的**空位置**（`iblk` 为空时）并算功耗？

> **答案**：空位置上没有用户电路，但**物理逻辑块仍存在、仍漏电**。漏电功耗只取决于「有哪些器件、尺寸多大」，与是否被使用无关。所以空位置也要按默认逻辑块类型估算漏电，否则总漏电会偏低。

**练习 2**：如果要新增一种组件类型（例如「NoC 路由器功耗」）到 breakdown 里，需要改哪几处？

> **答案**：在 `e_power_component_type` 枚举（`POWER_COMPONENT_MAX_NUM` 之前）加一项；在 `power_total` 里加一段对应的 `power_usage_*` 与 `power_component_add_usage`；报告打印函数会自动涵盖（因为它遍历枚举）。`POWER_COMPONENT_MAX_NUM` 作为数组长度会自动变大。

---

### 4.3 活动性与工艺数据

#### 4.3.1 概念说明

回到 4.2.2 的动态功耗公式，VPR 自己能算 $C_{\text{load}}$（从架构 + 工艺），但有两个量它**算不出来**：

1. **每根网的翻转密度 $D_{\text{net}}$**——这取决于电路在跑什么应用、输入是什么波形，是个**行为量**。VPR 只做综合/布局/布线，不做功能仿真。这个量由外部工具 **ACE2**（Activity Estimation）估算。
2. **晶体管级的漏电电流 $I_{\text{subthreshold}}$、电容 $C_g/C_s/C_d$、$V_{dd}$、温度**——这些是**工艺物理量**，取决于用哪个晶圆厂、多少纳米。VPR 也不做 TCAD/SPICE 仿真。这个量来自 **CMOS 工艺 XML**（基于 PTM 预测模型预先算好）。

所以功耗估算的「外部依赖」有两条线，分别由两个文件承载：活动性 `.act` 和 CMOS 工艺 XML。此外还有第三类藏在**架构 XML** 里的功耗参数（线电容、晶体管尺寸等），由 `t_power_arch` 承载。

#### 4.3.2 核心流程

**活动性这条线**（在 VTR 流水线层面）：

```
# run_vtr_flow.py 层面（5 阶段: odin/parmys/abc/ace/vpr）
若用户提供 -cmos_tech <工艺XML>:
    1. ACE 阶段(第4阶段) 对 ABC 输出的网表做翻转活动估计
       → 产出 <电路>.act  (每根网一行: 名字 概率 密度)
    2. VPR 阶段(第5阶段) 被自动加上 --power 与 --tech_properties
       → VPR 内部 read_activity 读 .act 进 atom_net_power
```

**工艺这条线**（在 VPR 内部）：

```
power_init()
  └─ power_tech_init(CmosTechFile)
       └─ power_tech_load_xml_file()   # pugixml 解析
            ├─ process_tech_xml_load_transistor_info()  # 晶体管漏电/电容
            ├─ power_tech_xml_load_multiplexer_info()   # mux 电压传递特性
            └─ power_tech_xml_load_components()         # SPICE 校准数据
       → 写入 PowerContext.tech (t_power_tech*)
```

**架构里的功耗参数**：架构 XML 的 `<power>` 段（线电容、mux 晶体管尺寸、FF 尺寸、LUT 晶体管尺寸等）在 u2 架构解析阶段就被读进 `t_arch.power`（一个 `t_power_arch*`），`power_init` 第一件事就是 [`power.cpp:L1305`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power.cpp#L1305) 把它赋给 `PowerContext.arch`。

#### 4.3.3 源码精读

**活动性结构 `t_net_power`**——[`physical_types.h:L406-L416`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L406-L416)：只有 `probability`（静态概率）和 `density`（每周期翻转次数）两个 float，注释明确「时钟的 density=2」。

**活动性读入 `read_activity`**——见 [`read_activity.cpp:L28-L71`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_activity.cpp#L28-L71)。它的逻辑很直白：

1. 先把网表里**每个网**的活动性初始化为 -1（第 39-42 行），作为「尚未赋值」哨兵。
2. 逐行读 `.act`，每行三个 token：`网名 概率 密度`，交给 [`add_activity_to_net`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_activity.cpp#L12-L26)（第 55 行）按网名查 `AtomNetId` 后填值。
3. 读完后**校验完整性**（第 62-68 行）：任何一根网若概率或密度仍 < 0，说明 `.act` 里漏了它，直接 `VPR_FATAL_ERROR`。这保证后续估算不会用到未定义的活动性。

**活动性映射的存放**——[`PowerContext.atom_net_power`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L385) 是 `std::unordered_map<AtomNetId, t_net_power>`。注意它**键是 AtomNetId（原子层网）**，而布线层用的是 `ClusterNetId`——所以还有一张 `clb_net_power`（第 382 行）在打包后建立聚簇层活动性。这种「原子层 vs 聚簇层」的双份存储是 VPR 数据流分层（u3-l2/u3-l3）在功耗侧的延续。

**工艺结构 `t_power_tech`**——[`power.h:L131-L152`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power.h#L131-L152)：顶层有 `PN_ratio`（反相器 PMOS/NMOS 比）、`Vdd`、`tech_size`（纳米）、`temperature`，以及 NMOS/PMOS 的晶体管信息 `t_transistor_inf`（漏电、电容随尺寸变化）、mux 电压信息、buffer 信息。每个晶体管尺寸的物理量在 [`t_transistor_size_inf`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power.h#L94-L107)（第 94-107 行）：`leakage_subthreshold`、`leakage_gate`、`C_g/C_s/C_d`。

**工艺读入入口**——[`power_cmos_tech.h:L30`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power_cmos_tech.h#L30) 声明 `power_tech_init`，实现见 [`power_cmos_tech.cpp:L63-L65`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power_cmos_tech.cpp#L63-L65)，内部调 `power_tech_load_xml_file` 用 pugixml 解析。按尺寸查表用 [`power_find_transistor_info`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power_cmos_tech.h#L31-L34)（返回插值用的上下界两个指针）。

**工艺 XML 样例**——[`vtr_flow/tech/PTM_45nm/45nm.xml:L1-L11`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/tech/PTM_45nm/45nm.xml#L1-L11) 就是 `t_power_tech` 的源头：`<operating_point temperature="85" Vdd="0.9"/>`、`<p_to_n ratio="2"/>`，每个 `<size W=.. L=..>` 给出该尺寸的 `<leakage_current>` 与 `<capacitance>`。这些数字由 PTM 模型 + SPICE 仿真离线生成（仓库里 `generate_cmos_tech_data.pl` 脚本）。

**架构功耗参数 `t_power_arch`**——[`physical_types.h:L294-L313`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libarchfpga/src/physical_types.h#L294-L313)：`C_wire_local`（本地互联线电容，每米）、`mux_transistor_size`、`FF_size`、`LUT_transistor_size` 等。这些是**架构作者**在架构 XML 里指定的、与工艺相对独立的尺寸/电容偏好，由 u2 的架构解析填充。

#### 4.3.4 代码实践

**实践目标**：实地跑通一次功耗流程，观察三类外部数据的真实样子。

**操作步骤**：

1. 用 `run_vtr_flow.py` 跑一个带功耗的示例（参考 u1-l4 的全流程跑法），关键是传 `-cmos_tech`：

   ```shell
   source .venv/bin/activate
   python3 vtr_flow/scripts/run_vtr_flow.py \
       <电路.v> <架构.xml> \
       -cmos_tech vtr_flow/tech/PTM_45nm/45nm.xml \
       -route_chan_width 100
   ```

   `-cmos_tech` 一旦给定，脚本会自动启用 ACE 阶段、生成 `.act`，并给 VPR 加上 `--power` 与 `--tech_properties`（见 [`flow.py:L258-L283`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/flow.py#L258-L283)）。

2. 打开中间产物里的 `.act` 文件（若被清理，加 `--temp_dir` 或保留中间文件），确认它是「网名 概率 密度」三列纯文本，对应 `read_activity` 的解析格式。
3. 打开输出的 `.power` 报告，对照 [`power.cpp:L1778-L1783`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power.cpp#L1778-L1783) 的 `power_print_breakdown_summary`，找到 Total 行及其 dynamic/leakage 分量。

**需要观察的现象**：
- `.act` 文件每行三个字段；时钟网（若有）密度为 2、概率为 0.5。
- `.power` 报告开头有 Summary（总功耗），后面是按 PB 类型的细分表，每项标了估算方法（Transistor Auto-Size / Pin-Toggle / Absolute 等，见 [`power.cpp:L1800-L1814`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power.cpp#L1800-L1814) 的方法说明）。

**预期结果**：能指出「动态功耗依赖 `.act`（活动性），漏电功耗依赖 CMOS 工艺 XML 与架构 `t_power_arch`，时钟频率依赖 `T_crit`」。

> 注意：本实践需要先按 u1-l2 构建好 VTR（`make`），并激活 Python 虚拟环境。若本地未构建，**待本地验证**。若只想做源码阅读，可跳过运行，直接用 `grep` 在 `vtr_flow/tech/PTM_45nm/45nm.xml` 里数有多少种晶体管 `<size>`，并对照 `t_transistor_inf` 理解其结构。

#### 4.3.5 小练习与答案

**练习 1**：`read_activity` 为什么要先初始化所有网为 -1，读完再扫描一遍找 < 0 的？

> **答案**：为了**完整性校验**。动态功耗对每根网都需要概率和密度，若 `.act` 漏了某根网，用 0 或随机值会得到错误结果且不易察觉。用 -1 作哨兵可以精确捕获「漏网」，直接报致命错误，避免静默的错误结果。

**练习 2**：CMOS 工艺 XML 和架构 XML 里的功耗参数（`t_power_arch`）有什么分工？

> **答案**：工艺 XML（`t_power_tech`）描述**与晶圆厂/工艺节点相关**的物理量：晶体管漏电电流、栅电容、$V_{dd}$、温度、PN 比——换工艺就换这个文件。架构 XML 的 `t_power_arch` 描述**架构设计选择**：本地线电容、mux/FF/LUT 用多大晶体管——同一工艺下改架构就改这里。两者正交，组合起来才能算功耗。

---

## 5. 综合实践

把三个最小模块串起来，做一次「**从输入到报告**的完整追踪」。

**任务**：给定一次带功耗的 VTR 运行，回答下列问题，每个答案都要给出对应的源码行作为证据。

1. **触发**：VPR 是在哪一行判断「要不要做功耗」的？对应哪个命令行开关？（提示：`vpr_api.cpp` 的 `vpr_analysis_flow` + `read_options.cpp` 的 `--power`）
2. **输入来源**：功耗估算消耗了哪三类外部数据？分别由哪个函数读入、写进 `PowerContext` 的哪个成员？（提示：`read_activity` → `atom_net_power`；`power_tech_init` → `tech`；`power_init` → `arch`）
3. **分解**：总功耗在 `power_total` 里被拆成哪三大组件？这三个组件的功耗分别由哪个 `power_usage_*` 函数算出？（提示：routing/clock/blocks 三函数）
4. **报告**：最终报告由哪个函数写入 `.power` 文件？它打印了哪几个层级的 breakdown？

**操作建议**：在 `power_total`（[`power.cpp:L1705`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power.cpp#L1705)）处设一个阅读「锚点」，向上追溯到 `vpr_power_estimation` 的三个输入文件，向下追踪到三大组件函数与报告打印函数，画出完整的调用图。

**进阶（可选）**：阅读 `power_usage_routing`（[`power.cpp:L776`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power.cpp#L776)），理解它如何**遍历路由树 `route_trees`**、把每根 RR 节点关联的网的活动性（`clb_net_density`）和节点电容相乘，算出该段布线的动态功耗。这是「活动性数据如何流进布线功耗」的关键衔接点，串联了 u6（路由树）与本讲。

## 6. 本讲小结

- **功耗估算是布线后的可选分析步骤**，由 `--power` + `-cmos_tech` 触发，入口是 `vpr_power_estimation`，走 `power_init → power_total → power_uninit` 三步生命周期。
- **它依赖时序与布线结果**：`T_crit`（关键路径延迟）决定时钟频率与动态功耗的 $f$ 项，`channel_width` 影响布线规模；这两者在布线/时序完成后才可得。
- **总功耗被层层分解**：先按 routing/clock/PB 三大块，每块再细分，每项又拆成 dynamic/leakage；用 `e_power_component_type` 枚举定骨架、`t_power_usage{dynamic,leakage}` 描一项、`by_component` 账本累加、边算边记账。
- **动态功耗 = 概率密度 × 电容 × V² × 频率**，漏电功耗与翻转无关、只与器件尺寸工艺有关；空位置也要算漏电。
- **两类外部数据**：活动性 `.act`（每根网的概率/密度，由 ACE2 估算、`read_activity` 读入）与 CMOS 工艺 XML（晶体管漏电/电容/Vdd，由 PTM 生成、`power_tech_init` 读入）；此外架构 XML 的 `t_power_arch` 提供架构级尺寸/线电容。
- **活动性按原子层（`atom_net_power`）与聚簇层（`clb_net_power`）双份存储**，体现 VPR 数据流分层；`read_activity` 用 -1 哨兵做完整性校验，漏网即致命报错。

## 7. 下一步学习建议

- **接 u8-l5（Server 模式与并行布线）**：了解 VPR 在集成场景下如何被外部工具驱动，与功耗这种「分析型」输出的关系。
- **深入原语级模型**：精读 [`power_components.cpp`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/power_components.cpp) 中 `power_usage_lut`/`power_usage_mux_multilevel` 的实现，理解 LUT 内部节点翻转密度如何用 SRAM 值过滤（`POWER_LUT_SLOW`/`FAST` 两种精度，见 `power_components.h:L36-L39`）。
- **校准机制**：阅读 [`PowerSpicedComponent.h`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/power/PowerSpicedComponent.h) 与 `power_calibrate.cpp`，理解 VPR 如何用少量 SPICE 仿真结果标定解析模型，在精度与速度间取平衡。
- **ACE2 内部**：若关心活动性如何从无到有被估计，可读 `ace2/sim.c`（蒙特卡洛仿真得到 `static_prob`/`switch_prob`）与 `ace2/io_ace.c`（输出 `.act`），把活动性这条线的源头补全。
