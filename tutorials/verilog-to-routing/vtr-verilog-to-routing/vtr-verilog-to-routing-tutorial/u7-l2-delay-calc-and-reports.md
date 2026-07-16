# 时序约束、延迟计算与布线后报告

## 1. 本讲目标

本讲承接 [u7-l1 时序图构建与 Tatum 集成](./u7-l1-timing-graph-tatum.md)。在上一讲里，我们把静态时序分析（STA）拆成了两件事：**VPR 负责建图与供延迟，Tatum 负责在图上算 setup/hold**。本讲回答紧随其后的三个问题：

1. **约束从哪里来**：用户写的 SDC 文件如何变成 Tatum 的 `TimingConstraints`？没写 SDC 时 VPR 又如何兜底？
2. **延迟从哪里来**：时序图上每条边的延迟，在打包前、布局、布线、布线后这四个时点分别由谁计算？为什么 VTR 看起来有「四种延迟计算器」？
3. **最终报告怎么生成**：布线完成后的那条关键路径报告（`report_timing.setup.rpt`）究竟由哪个文件、哪段代码写出来？

学完后，你应当能够：

- 说清 `read_sdc` 的输入输出与三种兜底路径；
- 看懂「延迟计算器家族」其实是「一个独立类 + 一个被复用三次的类（三个别名）」的真实结构，并能解释为何如此设计；
- 跟踪 `net_delay`（每条网在每个 sink pin 上的延迟）这条数据是如何在路由器、延迟计算器、时序分析器之间以**引用**传递的；
- 指出关键路径报告与逐网时序 CSV 报告的生成入口。

## 2. 前置知识

本讲假设你已经掌握 u7-l1 的结论。为方便阅读，重述三个关键事实：

- **时序图静态、边延迟动态**：时序图的拓扑（节点与边）在 `vpr_init_with_options` 里一次性建好，布局布线期间几乎不变；真正随实现状态变化的是**每条边上的延迟**。Tatum 通过 `TimingInfo::invalidate_delay(edge)` 标记变脏、`update()` 重算来增量更新。
- **VPR 与 Tatum 的分工**：VPR 提供「图 + 约束 + 延迟计算器」，Tatum 提供「分析器」在图上遍历。延迟计算器是二者之间的桥：Tatum 每查一条边，就回调 VPR 给的 `DelayCalculator::max_edge_delay(...)`。
- **关键术语**：
  - **SDC**（Synopsys Design Constraints）：业界标准的时序约束格式，用 TCL 语法书写，命令如 `create_clock`、`set_input_delay`、`set_false_path` 等。
  - **setup / hold**：建立时间检查（数据要在时钟沿之前稳定）/ 保持时间检查（数据要在时钟沿之后仍稳定）。前者约束最大延迟路径，后者约束最小延迟。
  - **net_delay**：一条网（net）从驱动端到每个接收端（sink pin）的信号延迟，按网 id 与 pin 索引二维存储。
  - **关键路径（critical path）**：所有 setup 路径中余量（slack）最小、即最逼近约束的那条路径，它的总延迟决定了电路能跑多快。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [vpr/src/timing/read_sdc.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/read_sdc.h) / [read_sdc.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/read_sdc.cpp) | 读 SDC 文件（或兜底）→ 生成 `tatum::TimingConstraints` |
| [vpr/src/timing/PreClusterDelayCalculator.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/PreClusterDelayCalculator.h) | 打包**前**的独立延迟计算器（用架构原语延迟 + 估计的连线延迟） |
| [vpr/src/timing/PostClusterDelayCalculator.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/PostClusterDelayCalculator.h) / [.tpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/PostClusterDelayCalculator.tpp) | 打包**后**的延迟计算器，消费外部传入的 `net_delay` |
| [vpr/src/timing/PlacementDelayCalculator.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/PlacementDelayCalculator.h) / [RoutingDelayCalculator.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/RoutingDelayCalculator.h) / [AnalysisDelayCalculator.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/AnalysisDelayCalculator.h) | 三个 `PostClusterDelayCalculator` 的别名（同一类） |
| [vpr/src/timing/net_delay.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/net_delay.h) / [net_delay.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/net_delay.cpp) | `load_net_delay_from_routing`：从布线路由树反算每条网的真实延迟 |
| [vpr/src/timing/timing_info.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/timing_info.h) / [concrete_timing_info.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/concrete_timing_info.h) | `TimingInfo` 接口族 + `make_setup_hold_timing_info` 工厂（桥接延迟计算器与分析器） |
| [vpr/src/analysis/timing_reports.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analysis/timing_reports.h) / [timing_reports.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analysis/timing_reports.cpp) | 布线后 setup/hold/逐网时序报告的生成入口 |
| [vpr/src/base/vpr_api.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp) | 主动脉：`read_sdc`、各延迟计算器、报告的调用点全在这里 |

---

## 4. 核心概念与源码讲解

### 4.1 SDC 约束读入

#### 4.1.1 概念说明

时序分析的「目标线」由约束决定：一条数据路径要走多快才算合格？这条线来自 SDC 文件。典型约束包括：

- `create_clock`：定义一个时钟域（名字、周期、波形、源头引脚）。
- `set_input_delay` / `set_output_delay`：描述外部 I/O 相对某时钟的延迟。
- `set_clock_groups -asynchronous` / `set_false_path`：声明两组时钟之间不必做时序检查。
- `set_max_delay` / `set_min_delay`：直接覆盖某时钟对之间的 setup/hold 约束值。
- `set_multicycle_path`：允许路径用多个周期完成。
- `set_clock_uncertainty`、`set_clock_latency -source`、`set_disable_timing` 等。

VPR 自己不解析 TCL 文本，而是依赖外部库 **LibSDCParse**（位于 `libs/EXTERNAL/`，不可在 VTR 树内直接改，见 u1-l3 的子树规则）。VPR 只提供一个回调对象 `SdcParseCallback`：每当 LibSDCParse 识别出一条 SDC 命令，就调用回调里对应的方法，回调把它翻译成对 `tatum::TimingConstraints` 的修改。

#### 4.1.2 核心流程

`read_sdc` 是整个约束读入的入口，签名为：

```cpp
std::unique_ptr<tatum::TimingConstraints> read_sdc(const t_timing_inf& timing_inf,
                                                   const AtomNetlist& netlist,
                                                   const AtomLookup& lookup,
                                                   const LogicalModels& models,
                                                   tatum::TimingGraph& timing_graph);
```

[read_sdc.h:14-18](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/read_sdc.h#L14-L18) —— 这段代码声明了入口：输入是时序配置 `t_timing_inf`（含 SDC 文件名与开关）、原子网表、原子查找表、逻辑模型、时序图本身；输出是一个堆上分配的约束对象。

它的工作流程是「**一条主干 + 三条兜底**」：

1. **时序分析被关掉**（`timing_inf.timing_analysis_enabled == false`）→ 直接套默认约束。
2. **SDC 文件不存在** → 打印告警，套默认约束。
3. **SDC 文件存在但解析出 0 条命令** → 套默认约束。
4. **正常解析** → 构造 `SdcParseCallback`，交给 `sdcparse::sdc_parse_filename` 逐行解析。

```text
            ┌─ timing_analysis_enabled == false ─┐
read_sdc ───┤─ SDCFile 不存在                   ├──→ apply_default_timing_constraints
            ├─ 解析出 0 条命令                   ┘
            └─ 正常解析 → SdcParseCallback → sdcparse → TimingConstraints
```

默认约束 `apply_default_timing_constraints` 又会按「逻辑时钟驱动源有几个」分三岔：0 个（纯组合电路，造一个 `virtual_io_clock`）、1 个（单时钟）、多个（多时钟 + 一个虚拟 I/O 时钟，且**互不分析跨域路径**）。

#### 4.1.3 源码精读

主入口的三条兜底分支在：

[read_sdc.cpp:1661-1691](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/read_sdc.cpp#L1661-L1691) —— 这段代码先 `make_unique` 一个空约束容器；若分析被关掉则打日志并套默认；若文件不存在则打「not found」并套默认；若文件存在则构造回调并解析，解析后若命令数为 0 仍套默认，否则打「Applied N SDC commands」。注意无论走哪条路，最后都返回同一个 `timing_constraints` 对象。

默认约束的分支决策：

[read_sdc.cpp:1718-1735](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/read_sdc.cpp#L1718-L1735) —— 这段代码先用 `find_netlist_logical_clock_drivers` 在原子网表里找出所有「逻辑时钟驱动源」，然后按数量分派到组合/单时钟/多时钟三套默认约束。这体现了「架构驱动 + 网表驱动」的双重理念：约束的兜底完全由网表自身的时钟结构推断，不需要用户干预。

单条命令的翻译以 `create_clock` 为例：

[read_sdc.cpp:156-229](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/read_sdc.cpp#L156-L229) —— 这段代码处理 `create_clock`：区分虚拟时钟（无目标、须有名字）与真实时钟（目标是 port/pin/net），逐个目标收集驱动引脚，建立 Tatum 时钟域并设置时钟源。可以看到 VPR 对 SDC 的支持是「子集 + 严格校验」——不合法的参数组合一律 `vpr_throw` 致命报错。

最后，SDC 里的时间数值默认以**纳秒**书写，VPR 内部用**秒**，转换由单位缩放完成：

[read_sdc.cpp:1593-1599](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/read_sdc.cpp#L1593-L1599) —— 这段代码把 SDC 数值乘以 `unit_scale_`（其默认值为 `1e-9`，见同文件 [read_sdc.cpp:1638](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/read_sdc.cpp#L1638)）换算成秒。

关于 setup/hold 约束值的推导：`calculate_setup_constraint` 把「launch 到 capture 的最小正边沿差」作为基准约束，再叠加多周期路径的额外周期数，最后允许被 `set_max_delay` 覆盖。其核心是 `calculate_launch_to_capture_edge_times`：它在两个时钟周期的**最小公倍数（LCM）窗口**内枚举所有 launch/capture 边沿对，取全局最小 setup 差与逐 launch 边沿最大 hold 差。用整数放大（`CLOCK_SCALE = 1000`）规避浮点误差。设 launch 周期为 \(T_l\)、capture 周期为 \(T_c\)，则 LCM 窗口长为：

\[
\mathrm{LCM}(T_l, T_c)
\]

在该窗口内，setup 约束（基准）为所有 launch 边沿 \(e_l\) 之后第一个 capture 边沿 \(e_c\) 之差的最小值：

\[
\mathrm{setup}_0 = \min_{e_l}\bigl(\min\{e_c - e_l \mid e_c > e_l\}\bigr)
\]

约束配置结构体本身只有两个关键字段：

[vpr_types.h:359-362](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L359-L362) —— 这段代码定义 `t_timing_inf`，包含 `timing_analysis_enabled` 开关与 `SDCFile` 文件名，正是 `read_sdc` 判断走哪条路径的依据。

`read_sdc` 在主流程中的唯一调用点在初始化阶段：

[vpr_api.cpp:354-356](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L354-L356) —— 这段代码在 `vpr_init_with_options` 里紧接建图之后调用 `read_sdc`，把结果存入 `timing_ctx.constraints`。也就是说约束在整条流程里只读入一次，随后被所有阶段共享（见 u7-l1 关于 `TimingContext` 的讨论）。

#### 4.1.4 代码实践

**实践目标**：用一个现成的 SDC 样例，对照源码确认「SDC 命令 → TimingConstraints」的映射。

**操作步骤**：

1. 打开仓库自带的样例 [vtr_flow/sdc/samples/multiclock_default.sdc](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/sdc/samples/multiclock_default.sdc)，它包含 5 行：

   ```tcl
   create_clock -period 0 *
   create_clock -period 0 -name virtual_io_clock
   set_clock_groups -exclusive -group {clk} -group {clk2}
   set_input_delay -clock virtual_io_clock -max 0 [get_ports {*}]
   set_output_delay -clock virtual_io_clock -max 0 [get_ports {*}]
   ```

2. 对每一行，在 [read_sdc.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/read_sdc.cpp) 里找到对应的回调方法（`create_clock`、`set_clock_groups`、`set_io_delay`），记录它最终调用的 `tc_.set_*` 系列方法。
3. 特别注意 `set_clock_groups -exclusive`（[read_sdc.cpp:636-692](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/read_sdc.cpp#L636-L692)）：它把每对跨组时钟加入 `disabled_domain_pairs_`，最终在 `resolve_clock_constraints` 里被跳过，从而不对它们生成 setup/hold 约束。

**需要观察的现象**：第 3 行用 `set_clock_groups` 抑制了 `clk` 与 `clk2` 之间的跨域检查；如果没有这一行，多时钟默认约束（[read_sdc.cpp:1791-1834](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/read_sdc.cpp#L1791-L1834)）会主动把所有跨域路径设为 0 周期约束。

**预期结果**：你能画出一张「SDC 命令 → 回调方法 → `tc_` 调用」的对照表。

**待本地验证**：若想实际跑通，可用 `run_vtr_flow.py` 配合该 SDC 样例（通过 `--sdc_file` 等参数，具体参数名以最新 `read_options` 实现为准）运行，观察日志中 `Applied N SDC commands` 与 `Timing constraints created K clocks` 两行。

#### 4.1.5 小练习与答案

**练习 1**：如果一个电路完全没有时钟，VPR 会生成什么样的默认约束？

**答案**：走组合电路分支 `apply_combinational_default_timing_constraints`（[read_sdc.cpp:1739-1758](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/read_sdc.cpp#L1739-L1758)）：创建一个周期为 0 的虚拟时钟 `virtual_io_clock`，把所有输入/输出以 0 延迟约束到它上面，目标是让组合路径的延迟最小化。

**练习 2**：`set_max_delay` 设定的值与基于周期的默认约束冲突时，哪个生效？

**答案**：`set_max_delay` 覆盖生效（见 [read_sdc.cpp:1118-1135](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/read_sdc.cpp#L1118-L1135)）。如果覆盖值比默认更宽松，VPR 还会打一条 warning 提示「覆盖了一个更紧的默认约束」。

---

### 4.2 延迟计算器的家族与层次

#### 4.2.1 概念说明

时序图的边分两大类（见 u7-l1）：**原语内部边**（如 LUT 的输入到输出、FF 的时钟到 Q、setup/hold 时间）和**互连边 INTERCONNECT**（原子引脚之间经布线网络的连线）。原语内部延迟来自架构 XML 里的 `pb_graph_pin` 标注，相对固定；真正随实现状态剧烈变化的是**互连延迟**——布局布线每动一下，它就变。

「延迟计算器」就是给 Tatum 提供这两类边延迟的对象，统一实现 Tatum 的抽象基类 `tatum::DelayCalculator`，核心方法就四个：

- `max_edge_delay(graph, edge)`：该边的最大延迟（setup 用）。
- `min_edge_delay(graph, edge)`：该边的最小延迟（hold 用）。
- `setup_time(graph, edge)`：捕获寄存器的建立时间。
- `hold_time(graph, edge)`：捕获寄存器的保持时间。

初学者容易被一排名字相近的头文件吓到：`PreClusterDelayCalculator`、`PlacementDelayCalculator`、`RoutingDelayCalculator`、`AnalysisDelayCalculator`、`PostClusterDelayCalculator`。本模块的关键认知是：**这五个名字背后只有两个真正的类**。

#### 4.2.2 核心流程：两个类 + 三个别名

打开三个「阶段专用」头文件，你会看到惊人地一致的内容：

[RoutingDelayCalculator.h:3-5](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/RoutingDelayCalculator.h#L3-L5) —— 这段代码只有一行：`using RoutingDelayCalculator = PostClusterDelayCalculator;`。同理 [PlacementDelayCalculator.h:3-5](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/PlacementDelayCalculator.h#L3-L5) 与 [AnalysisDelayCalculator.h:3-5](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/AnalysisDelayCalculator.h#L3-L5) 也只是别名。

也就是说，布局、布线、布线后分析三个阶段用的是**同一个类** `PostClusterDelayCalculator`。它们之间的差别，完全来自构造时传入的那份 `net_delay` 数据不同：

| 名字 | 真实类型 | 互连延迟来源 | 使用阶段 | 构建位置 |
|---|---|---|---|---|
| `PreClusterDelayCalculator` | **独立类** | `timing_arc_delays_`（按 sink pin 的估计值） | 打包前 | `PreClusterTimingManager` |
| `PlacementDelayCalculator` | `PostClusterDelayCalculator` | `net_delay`（布局延迟模型估计） | 布局 | 布局阶段内部 |
| `RoutingDelayCalculator` | `PostClusterDelayCalculator` | `net_delay`（路由器增量估计） | 布线 | `vpr_api.cpp` 路由流 |
| `AnalysisDelayCalculator` | `PostClusterDelayCalculator` | `net_delay`（`load_net_delay_from_routing` 实测值） | 布线后分析 | `vpr_analysis` |

为什么这样设计？因为打包前后，网表的「粒度」不同：

- **打包前**：还没有逻辑块，电路就是一堆原子（LUT/FF）。互连延迟只能用「架构里的线延迟 + 直连（如进位链）」来粗估，而且这些估计按 sink pin 存放。这一阶段用的是独立的 `PreClusterDelayCalculator`。
- **打包后**：电路已被装箱成逻辑块（CLB），互连延迟可以分解成「块内延迟 + 块间布线延迟」。块间布线延迟正是 `net_delay`，由外部填好后按引用喂进来。同一套「分解 + 查表」逻辑在布局、布线、分析三个阶段都适用，所以复用同一个类，只换数据。

这是一种典型的「**数据多态、代码复用**」设计：与其为每个阶段写一个计算器，不如写一个通用的、数据驱动的计算器。

#### 4.2.3 源码精读

**先看打包前的独立类**。它的构造函数吃的是 `timing_arc_delays`（一个按 `AtomPinId` 索引的浮点向量），而不是 `net_delay`：

[PreClusterDelayCalculator.h:22-38](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/PreClusterDelayCalculator.h#L22-L38) —— 这段代码定义 `PreClusterDelayCalculator`，构造参数包括原子网表、查找表、逻辑模型、`timing_arc_delays`、以及一个 `Prepacker`（用于定位每个原子预期的最低代价 PB 节点，从而读到正确的原语延迟）。它还断言「每条 timing arc（以 sink pin 唯一标识）都必须有对应的延迟」。

它的 `max_edge_delay` 按边类型分派，INTERCONNECT 边直接读 `timing_arc_delays_`：

[PreClusterDelayCalculator.h:40-62](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/PreClusterDelayCalculator.h#L40-L62) —— 这段代码：组合边走 `prim_comb_delay`（从 `pb_graph_pin` 的 `pin_timing_del_max` 读架构标注延迟），时钟启动边走 `prim_tcq_delay`（读 `tco_max`），**互连边**直接返回 `timing_arc_delays_[atom_sink_pin]`——即外部估计好的连线延迟。注意它目前 `min_edge_delay`/`hold_time` 直接复用 max/setup（文件内有 `TODO: use true min delay` 注释）。

这些估计延迟由谁填？答案是 `PreClusterTimingManager`：

[PreClusterTimingManager.h:123-136](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/PreClusterTimingManager.h#L123-L136) —— 这段代码提供 `set_timing_arc_delay(sink_pin, delay)`（只改内部变量，不做 STA）与 `update_timing_info()`（批量改完后才统一重算），正是给打包器在改变估计延迟后高效刷新时序用的。

**再看打包后的复用类**。它的构造函数第四个参数是对 `net_delay` 的 **const 引用**：

[PostClusterDelayCalculator.h:15-20](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/PostClusterDelayCalculator.h#L15-L20) —— 这段代码定义 `PostClusterDelayCalculator`，构造参数为原子网表、查找表、`const NetPinsMatrix<float>& net_delay`、以及 `is_flat` 标志。引用语义意味着：**外部更新 `net_delay`，计算器下一次查边就能立刻看到新值**——这是时序增量的根基。

它的私有成员把外部数据与内部辅助计算器都保存下来：

[PostClusterDelayCalculator.h:66-73](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/PostClusterDelayCalculator.h#L66-L73) —— 这段代码：持有 `net_delay_` 引用、两个块内延迟助手 `ClbDelayCalc`/`AtomDelayCalc`、以及若干 `mutable` 缓存（按 `tatum::EdgeId` 缓存最大/最小延迟、块内延迟、pin 对，避免重复计算）。`mutable` + 缓存让「逻辑上 const 的查询」能带记忆，是性能关键。

#### 4.2.4 代码实践

**实践目标**：亲手验证「三个阶段专用名 = 同一个类」。

**操作步骤**：

1. 用 `git grep` 在仓库内搜索 `class PlacementDelayCalculator`、`class RoutingDelayCalculator`、`class AnalysisDelayCalculator`，确认它们都**没有**真正的类定义，只有 `using ... = PostClusterDelayCalculator;`。
2. 再搜索 `class PostClusterDelayCalculator`，确认它有完整的类定义与 `.tpp` 实现。
3. 在 [vpr_api.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp) 里分别搜索 `RoutingDelayCalculator`（路由流，[vpr_api.cpp:1057](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1057)）与 `AnalysisDelayCalculator`（分析流，[vpr_api.cpp:1604](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1604)），观察它们 `std::make_shared<...>` 的构造参数列表是否一致（都是 `netlist, lookup, net_delay, is_flat` 四件套）。

**需要观察的现象**：两处构造调用的形参完全相同，差别只在于传入的 `net_delay` 内容（路由流用的是路由器维护的增量估计；分析流用的是 `load_net_delay_from_routing` 重新算出的实测值）。

**预期结果**：你得出结论——「延迟计算器的阶段差异 = net_delay 数据的差异」，而不是类的差异。

#### 4.2.5 小练习与答案

**练习 1**：为什么打包前不能用 `PostClusterDelayCalculator`？

**答案**：因为打包前还没有逻辑块（CLB），`PostClusterDelayCalculator` 依赖的「块内延迟分解（launch_cluster + inter_cluster + capture_cluster）」无意义；那时电路只有原子，互连延迟只能用按 sink pin 的估计 `timing_arc_delays` 表达，故需要独立的 `PreClusterDelayCalculator`。

**练习 2**：`PostClusterDelayCalculator` 持有 `net_delay_` 的是引用而非拷贝，这样做的好处是什么？

**答案**：路由器在布线过程中不断更新 `net_delay`，计算器按引用持有就能在 `timing_info->update()` 重算时自动读到最新值，无需重建计算器；同时省去大矩阵的深拷贝开销。

---

### 4.3 net_delay：延迟数据如何在阶段间流动

#### 4.3.1 概念说明

上一模块点明「阶段差异就是 `net_delay` 的差异」。本模块专门讲清 `net_delay` 这份数据：它是什么形状、由谁填写、如何被计算器消费。

`net_delay` 的类型是 `NetPinsMatrix<float>`，可以理解为二维表 `net_delay[net_id][pin_index]`：行是网 id，列是该网的引脚索引（0 号是驱动端，1..N 是各 sink）。每个格子存「该网从驱动端到该 sink 的总延迟」。

它有两个截然不同的「生产者」：

1. **路由器自己在布线时增量维护**（驱动 `RoutingDelayCalculator`，用于时序驱动布线）。
2. **`load_net_delay_from_routing` 在布线完成后从路由树整体重算**（驱动 `AnalysisDelayCalculator`，用于最终报告）。重算的目的是**校验**——文件头注释明确写道：重算值用来「对照布线过程中增量算出的延迟」。

#### 4.3.2 核心流程

`load_net_delay_from_routing` 对每条网二选一：

```text
for net in net_list:
    if net 被忽略(ideal/常量等):
        net_delay[net][*] = 0            # 常量填充
    else:
        由 traceback 重建完整路由树
        递归遍历树，把每个 SINK 节点的 Elmore 延迟 Tdel
            写入 ipin_to_Tdel_map[ipin]
        把 map 里的值拷进 net_delay[net][ipin]
```

这里的关键是「从 traceback 重建完整路由树」并重新计算 R/C/Tdel——这就是为什么它是「真实」的布线后延迟，而不是路由器增量估计的近似。

计算器消费这份数据时，把一条原子互连边的延迟分解成三段：

\[
\text{delay} = \text{launch\_cluster\_delay} + \text{inter\_cluster\_delay} + \text{capture\_cluster\_delay}
\]

其中 `inter_cluster_delay` 直接就是 `net_delay[net][sink_pin_index]`。若连接被完全吸收在块内（没走出逻辑块），后两项为 0。

#### 4.3.3 源码精读

填充入口：

[net_delay.h:6](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/net_delay.h#L6) —— 这段代码声明 `load_net_delay_from_routing(net_list, net_delay)`，唯一的公开接口。

实现主循环：

[net_delay.cpp:44-58](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/net_delay.cpp#L44-L58) —— 这段代码遍历所有网：被忽略的用 `load_one_constant_net_delay` 填 0；其余调用 `load_one_net_delay`，后者从 `g_vpr_ctx.routing().route_trees[net_id]` 取出路由树，递归收集每个 sink 的 `Tdel`。

递归收集：

[net_delay.cpp:92-102](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/net_delay.cpp#L92-L102) —— 这段代码深度遍历路由树：遇到带 `net_pin_index`（非 `UNDEFINED`）的节点（即 sink 节点），把它的 `Tdel` 写进 `ipin_to_Tdel_map`，遍历完即可按 pin 索引回填 `net_delay`。

计算器如何消费：

[PostClusterDelayCalculator.tpp:193-206](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/PostClusterDelayCalculator.tpp#L193-L206) —— 这段代码的注释把三段分解讲得最清楚：`launch_cluster_delay`（原语输出→块输出）、`inter_cluster_delay`（块间布线）、`capture_cluster_delay`（块输入→原语输入）；并说明若 `is_flat` 为真（扁平布线），块内两段视为 0。

最终落到查表的一行：

[PostClusterDelayCalculator.tpp:383-388](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/PostClusterDelayCalculator.tpp#L383-L388) —— 这段代码：`inter_cluster_delay` 断言源端 pin 索引为 0（驱动端），直接返回 `net_delay_[net_id][sink_net_pin_index]`。这正是 `load_net_delay_from_routing` 与计算器之间的「数据插座」。

#### 4.3.4 代码实践

**实践目标**：跟踪一份 `net_delay` 矩阵从「创建 → 填充 → 被计算器引用」的完整路径。

**操作步骤**：

1. 在分析流 [vpr_api.cpp:1600-1606](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1600-L1606) 观察三步：`make_net_pins_matrix<float>(net_list)` 创建空矩阵 → `load_net_delay_from_routing(net_list, net_delay)` 填充 → `std::make_shared<AnalysisDelayCalculator>(..., net_delay, ...)` 把它按引用交给计算器。
2. 对比路由流 [vpr_api.cpp:1048-1058](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1048-L1058)：同样是先 `make_net_pins_matrix`、再构造 `RoutingDelayCalculator` 绑定它，但**填充发生在路由器内部**（`vpr_route_min_W`/`vpr_route_fixed_W` 期间增量更新），而非 `load_net_delay_from_routing`。

**需要观察的现象**：路由流里 `net_delay` 是「先绑定、后由路由器边布线边填」；分析流里是「先填好、再绑定」。

**预期结果**：你能用一句话说清差异——「路由阶段的 `net_delay` 是路由器边走边估的增量值，分析阶段的 `net_delay` 是布线完成后从路由树整体重算的校验值；两者都用同一份 `PostClusterDelayCalculator` 代码消费，因为该类只认 `net_delay_` 引用，不关心谁填的」。

#### 4.3.5 小练习与答案

**练习 1**：为什么分析阶段不直接复用路由阶段那个 `net_delay` 矩阵，而要重新建一个并用 `load_net_delay_from_routing` 重填？

**答案**：路由阶段的 `net_delay` 是增量估计，可能有累积误差；`load_net_delay_from_routing` 从最终路由树用 Elmore（R/C/Tdel）整体重算，得到精确的布线后延迟，作为正式时序报告与功耗分析的依据。文件头注释明确说重算值用于「对照增量值」。

**练习 2**：一条被声明为 ideal（`net_is_ignored` 为真）的网，其 `net_delay` 是什么？

**答案**：全 0（见 [net_delay.cpp:53](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/net_delay.cpp#L53) 与 [net_delay.cpp:104-112](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/net_delay.cpp#L104-L112)），表示该网在时序上被当作零延迟的理想连线。

---

### 4.4 布线后分析与时序报告

#### 4.4.1 概念说明

前面三模块凑齐了 STA 的全部输入：**图**（u7-l1）、**约束**（4.1）、**延迟计算器**（4.2 + 4.3）。本模块把它们组装起来跑出最终结果，并落盘成报告。

组装发生在工厂函数 `make_setup_hold_timing_info`：它用一个延迟计算器，配合已就绪的图与约束，造出一个 Tatum 分析器，再包进 `SetupHoldTimingInfo`。之后调用 `timing_info->update()` 触发一次完整 STA，结果就是每个引脚的 setup/hold slack、各时钟域对的关键路径等。

报告分三类：

- **关键路径报告**：`report_timing.setup.rpt` / `report_timing.hold.rpt`，列出最紧的若干条路径及其上每一级延迟。
- **未约束端点报告**：`report_unconstrained_timing.{setup,hold}.rpt`，列出未被任何约束覆盖的起点/终点（用于发现漏约束）。
- **逐网时序 CSV**：`report_net_timing.csv`，每行一条原子网，含扇出、包围盒、源端 slack、各 sink 的 slack 与延迟。

此外日志里还有一行简明的 setup/hold 摘要（如最差负余量 WNS、总负余量 TNS）。

#### 4.4.2 核心流程

`vpr_analysis_flow`（被 `vpr_flow` 在最后调用）按 `e_stage_action` 决定是否运行，然后调 `vpr_analysis`，其时序部分流程：

```text
vpr_analysis:
  routing_stats(...)                      # 布线统计（面积、线长等）
  if TimingEnabled:
    net_delay = make_net_pins_matrix()    # 1. 建空矩阵
    load_net_delay_from_routing(net_delay)# 2. 填实测延迟
    analysis_delay_calc = AnalysisDelayCalculator(... net_delay ...)
    timing_info = make_setup_hold_timing_info(analysis_delay_calc, ...)
    timing_info->update()                 # 3. 跑 STA
    generate_hold_timing_stats(...)       # 4a. hold 报告
    generate_setup_timing_stats(...)      # 4b. setup 报告（含关键路径）
    if 选项开启: generate_net_timing_report(...)  # 4c. 逐网 CSV
    if do_power: vpr_power_estimation(...)        # 5. 功耗（依赖 critical path）
```

注意 `setup`/`hold` 报告是**分别**由两个函数生成的，但底层都借助 Tatum 的 `TimingReporter` 与 VPR 的 `VprTimingGraphResolver`（后者负责把 Tatum 的节点 id 翻译回可读的网表引脚名）。

#### 4.4.3 源码精读

报告生成的对外接口：

[timing_reports.h:9-21](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analysis/timing_reports.h#L9-L21) —— 这段代码声明 `generate_setup_timing_stats` 与 `generate_hold_timing_stats`，参数都是「前缀、timing_info、延迟计算器、分析选项、is_flat、块位置注册表」。前缀通常为空串，因此文件名就是 `report_timing.setup.rpt`。

setup 报告实现：

[timing_reports.cpp:128-152](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analysis/timing_reports.cpp#L128-L152) —— 这段代码：先 `print_setup_timing_summary` 打日志摘要；然后构造 `VprTimingGraphResolver`（设置报告详略级别）与 `tatum::TimingReporter`；调用 `report_timing_setup` 写出 `report_timing.setup.rpt`，可选 `report_skew_setup.rpt`，最后 `report_unconstrained_setup` 写未约束端点报告。**关键路径报告就由这一段生成**。

hold 报告结构对称：

[timing_reports.cpp:154-178](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analysis/timing_reports.cpp#L154-L178) —— 这段代码生成 `report_timing.hold.rpt` 等文件，逻辑与 setup 版镜像。

逐网 CSV 报告：

[timing_reports.h:44-46](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analysis/timing_reports.h#L44-L46) 与 [timing_reports.cpp:180-192](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analysis/timing_reports.cpp#L180-L192) —— 这段代码声明并实现 `generate_net_timing_report`，写出 `report_net_timing.csv`，表头为 `netname,Fanout,bb_xmin,...,src_pin_name,src_pin_slack,sinks`。

报告详略与开关都来自分析选项结构体：

[vpr_types.h:1430-1442](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L1430-L1442) —— 这段代码定义 `t_analysis_opts` 的若干字段：`gen_post_synthesis_netlist`、`timing_report_npaths`（报告多少条路径）、`timing_report_detail`（详略枚举）、`write_timing_summary`、`generate_net_timing_report`。详略枚举见 [vpr_types.h:1256-1261](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L1256-L1261)：`NETLIST`/`AGGREGATED`/`DETAILED_ROUTING`/`DEBUG`。

延迟计算器与分析器的桥接工厂：

[concrete_timing_info.h:485-499](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/concrete_timing_info.h#L485-L499) —— 这段代码是 `make_setup_hold_timing_info` 模板工厂：根据 `e_timing_update_type`（FULL/AUTO 用并行 walker、INCREMENTAL 用增量 walker）用 `tatum::AnalyzerFactory` 造出 `SetupHoldTimingAnalyzer`，再把「图 + 约束 + 延迟计算器 + 分析器」打包进 `ConcreteSetupHoldTimingInfo`。这一步把本讲三块积木（约束、延迟计算器、图）焊在了一起。

`TimingInfo` 接口族定义了上层算法查询时序结果的统一入口：

[timing_info.h:22-50](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/timing_info.h#L22-L50) —— 这段代码定义抽象基类 `TimingInfo`：`invalidate_delay(edge)` 标脏、`update()` 重算、并提供对底层分析器/计算器/图/约束的访问。布局布线算法只依赖这个抽象接口，不关心背后是 setup、hold 还是两者都算。

[timing_info.h:56-98](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/timing_info.h#L56-L98) —— 这段代码定义 `SetupTimingInfo`，提供 `setup_pin_slack`、`setup_pin_criticality`、`least_slack_critical_path`、`setup_worst_negative_slack` 等，正是布局布线做时序驱动时反复查询的方法。

最后，把整条分析链放回主流程：

[vpr_api.cpp:1597-1637](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1597-L1637) —— 这段代码是 `vpr_analysis` 的时序主体：建矩阵→`load_net_delay_from_routing`→构造 `AnalysisDelayCalculator`→`make_setup_hold_timing_info`→`update()`→生成 hold/setup 报告→可选写后综合网表、逐网 CSV、功耗分析。它是本讲所有概念的「合龙之处」。

#### 4.4.4 代码实践

**实践目标**：定位「关键路径报告由哪个文件、哪段代码生成」，并尝试让 VPR 产出更详尽的报告。

**操作步骤**：

1. 在 [timing_reports.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analysis/timing_reports.cpp) 中确认：setup 关键路径报告由 `generate_setup_timing_stats`（[timing_reports.cpp:128-152](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/analysis/timing_reports.cpp#L128-L152)）调用 `timing_reporter.report_timing_setup(...)` 写入 `report_timing.setup.rpt`；该函数被 `vpr_analysis`（[vpr_api.cpp:1618-1619](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1618-L1619)）调用。
2. 阅读 `VprTimingGraphResolver` 如何把 Tatum 的 `tnode` 翻译回网表引脚名（这是报告可读性的关键），并注意 `set_detail_level` 来自 `timing_report_detail` 选项。
3. 找到对应的命令行开关（在 `read_options` 实现里搜索 `timing_report_npaths`、`timing_report_detail`），了解如何让报告输出更多路径、更细粒度的布线资源信息。

**需要观察的现象**：把 `timing_report_detail` 设为 `DETAILED_ROUTING` 后，报告里每条路径会展开到所用布线资源；设为 `NETLIST` 则只列网表级元素。

**预期结果**：你能闭着眼睛回答——「关键路径报告由 `vpr/src/analysis/timing_reports.cpp` 的 `generate_setup_timing_stats` 生成，文件名 `report_timing.setup.rpt`」。

**待本地验证**：实际运行一次完整流程后打开 `report_timing.setup.rpt`，对照 `timing_report_npaths` 与 `timing_report_detail` 两个参数，验证报告内容随参数变化。

#### 4.4.5 小练习与答案

**练习 1**：如果布线失败（`route_status.success() == false`），`vpr_analysis_flow` 还会跑分析吗？

**答案**：取决于 `doAnalysis`。若为 `SKIP` 则直接返回；若为 `SKIP_IF_PRIOR_FAIL` 且布线失败则跳过；若为 `DO` 或「布线成功 + SKIP_IF_PRIOR_FAIL」则会运行，但会先打印一条 warning 说明「以下分析结果对应非法实现」（见 [vpr_api.cpp:1512-1525](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1512-L1525)）。

**练习 2**：`make_setup_hold_timing_info` 的第二个参数 `timing_update_type` 在什么场景下选 `INCREMENTAL`？

**答案**：当算法频繁做小改动（如布局退火每步移动）并希望复用上次分析结果、只重算受影响部分时，用 `INCREMENTAL`（基于 `SerialIncrWalker`）更省时间；若改动大或想要最稳妥的完整重算，用 `FULL`/`AUTO`（基于 `ParallelWalker`）。注意增量更新需要配合 `invalidate_delay(edge)` 标脏（见 [concrete_timing_info.h:491-496](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/concrete_timing_info.h#L491-L496)）。

---

## 5. 综合实践

**任务**：把本讲四块知识串成一条「SDC → 约束 → 延迟 → 报告」的完整链路，并写一份一页纸的「时序数据流图」说明。

请按顺序完成：

1. **约束侧**：选一个仓库自带 SDC（如 [vtr_flow/sdc/samples/multiclock_default.sdc](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/sdc/samples/multiclock_default.sdc)），逐行标注它在 [read_sdc.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/read_sdc.cpp) 中命中的回调方法，最终落到哪些 `tc_.set_*` 调用。

2. **延迟侧**：画出从 `vpr_init_with_options`（建图 + read_sdc）到 `vpr_analysis`（最终 STA）之间，「延迟计算器」与「net_delay」的演化时间线，标注四个时点（打包前/布局/布线/分析）各自用哪个名字、`net_delay` 从哪来。务必体现「布局/布线/分析三个名字是同一类」这一关键事实。

3. **报告侧**：明确指出 `report_timing.setup.rpt` 的生成函数（`generate_setup_timing_stats`）及其在 `vpr_analysis` 中的调用行（[vpr_api.cpp:1618-1619](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1618-L1619)），并说明它依赖的 `analysis_delay_calc` 是用 `load_net_delay_from_routing` 填充的 `net_delay` 构造的。

4. **自检问题**：如果把布线后分析的 `load_net_delay_from_routing` 那一行注释掉（仅作思维实验），`net_delay` 会全为初始值，分析结果会怎样？请基于 [PostClusterDelayCalculator.tpp:383-388](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/PostClusterDelayCalculator.tpp#L383-L388) 推断后果。

**预期产出**：一张时序数据流图 + 一段对自检问题的推断。自检问题的参考答案：`net_delay` 未填充则 `inter_cluster_delay` 读到的是矩阵初始值（`make_net_pins_matrix` 默认构造的值，通常为 0），所有互连边延迟趋近于 0，STA 会严重低估路径延迟、给出过度乐观（偏小）的负余量或虚假的「满足时序」结论——这正是为什么布线后必须用真实延迟重算。

## 6. 本讲小结

- **约束一次性读入**：`read_sdc` 在 `vpr_init_with_options` 里调用一次，结果存进 `TimingContext::constraints`；无 SDC / 0 命令 / 关闭分析三种情况都走默认约束，默认约束按网表的逻辑时钟数量分三岔。SDC 解析依赖外部库 LibSDCParse，VPR 仅提供 `SdcParseCallback`。
- **延迟计算器只有两个类**：`PreClusterDelayCalculator`（打包前，吃 `timing_arc_delays` + 架构原语延迟）与 `PostClusterDelayCalculator`（打包后，吃 `net_delay` 引用）；`Placement`/`Routing`/`Analysis` 三个名字都只是后者的别名。
- **阶段差异 = net_delay 数据差异**：同一份 `PostClusterDelayCalculator` 代码在布局、布线、分析三阶段被复用，区别只在传入的 `net_delay`（布局估计 / 路由器增量估计 / 布线后实测）。
- **net_delay 是引用传递的共享数据**：`load_net_delay_from_routing` 从路由树用 Elmore 重算每条网到各 sink 的延迟，填进 `NetPinsMatrix`，计算器经 `inter_cluster_delay` 查表消费；引用语义让增量更新零拷贝可见。
- **报告在分析流合龙**：`vpr_analysis` 用「实测 net_delay → `AnalysisDelayCalculator` → `make_setup_hold_timing_info` → `update()` → 报告」跑出最终 STA，关键路径报告由 `analysis/timing_reports.cpp` 的 `generate_setup_timing_stats` 写入 `report_timing.setup.rpt`。
- **统一接口 `TimingInfo`**：布局布线算法只依赖抽象的 `TimingInfo`/`SetupTimingInfo`，通过 `invalidate_delay` + `update` 增量刷新，不感知背后用的是哪个延迟计算器或哪种 walker。

## 7. 下一步学习建议

- **向上承接**：本讲已把时序分析的「输入三件套（图/约束/延迟）+ 报告」讲完。若想看时序结果如何反向驱动算法，回到 [u5-l3 布局代价与延迟模型](./u5-l3-delay-model.md)（布局用 `simple`/`delta` 延迟模型）与 [u6-l3 基于连接的路由与路由树](./u6-l3-connection-based-routing.md)（路由器的「延迟下界」与关键度驱动），体会它们都通过 `TimingInfo` 查询 `setup_pin_criticality` 等接口。
- **横向扩展**：阅读 [vpr/src/timing/VprTimingGraphResolver.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/VprTimingGraphResolver.h)，理解报告里 tnode 如何被翻译回可读引脚名；这是把「机器视角的时序图」变成「人能读的关键路径」的关键一环。
- **深入 Tatum**：本讲多次提到 Tatum 的分析器与 `TimingReporter`，它们位于外部子树 `libs/EXTERNAL/libtatum/`（不可直接改）。建议读其 README，理解 setup/hold 遍历与增量 walker 的实现，把 u7-l1 与本讲建立的「VPR 供图供延迟、Tatum 算」的分工在引擎内部落实。
- **下一单元**：进入 [u8 高级特性与扩展机制](./)，其中 u8-l3 功耗分析会直接消费本讲 `vpr_analysis` 产出的 `least_slack_critical_path().delay()` 作为 `T_crit`（见 [vpr_api.cpp:1658-1668](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1658-L1668)），是时序结果的下游消费者。
