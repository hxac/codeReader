# 时序图构建与 Tatum 集成

## 1. 本讲目标

VPR 在打包、布局、布线每个阶段都要反复回答同一个问题：「这次实现的最坏路径（关键路径）有多慢？」回答这个问题的学科叫**静态时序分析（Static Timing Analysis, STA）**。VPR 自己并不写 STA 算法，而是把这件事委托给一个外部的、高性能的 STA 引擎——**Tatum**。要让 Tatum 工作，VPR 必须先把网表「翻译」成 Tatum 能理解的**时序图（Timing Graph）**。

本讲读完之后，你应该能够：

1. 说清 VPR 为什么把 STA 委托给外部库 Tatum，二者如何分工。
2. 说出时序图的节点（tnode）有哪五种类型、边（tedge）有哪四种类型，以及「外部/内部 tnode」的区分意义。
3. 看懂 `TimingGraphBuilder` 如何把 `AtomNetlist` 一步步变成 `tatum::TimingGraph`。
4. 说出 `TimingContext` 里 `graph` 与 `constraints` 分别在主流程的哪一步被赋值、为什么此后拓扑不再重建。
5. 区分「时序图构建（一次）」与「时序分析触发（多次）」这两件事。

本讲承接 u3-l4（VprContext 全局状态）与 u3-l2（AtomNetlist），为 u7-l2（延迟计算器、SDC 约束与布线后报告）打基础。

## 2. 前置知识

### 2.1 什么是静态时序分析（STA）

「静态」是相对于「动态仿真」而言：STA **不施加激励、不跑真值**，只看电路的**拓扑结构**和每条连线的**延迟**，算出「信号最早什么时候到、最晚什么时候到」，从而判断电路在给定时钟下能否跑通。

两个核心概念：

- **Setup（建立）检查**：信号必须在时钟沿到来**之前**一段时间（建立时间）就稳定到达寄存器数据端。这是「最大延迟（max-delay）」分析——路径太慢就违例。
- **Hold（保持）检查**：信号必须在时钟沿之后**保持**一段时间不变。这是「最小延迟（min-delay）」分析——信号到得太快也会出错。

STA 把电路抽象成一张**有向无环图（DAG）**：

- **节点（node）**：代表时序上的「事件点」，通常对应引脚或逻辑上的源/汇。
- **边（edge）**：代表时序依赖——「信号必须先到 A，才能到 B」，边上挂着延迟。

只要有了这张图和每条边的延迟，STA 就能在图上做正向（到达时间 arrival）和反向（要求时间 required）遍历，二者的差就是**余量（slack）**：

\[
\text{slack} = \text{required\_time} - \text{arrival\_time}
\]

slack 为负即时序违例。对一条路径，关键路径（critical path）就是 slack 最小（最负）的那条。

### 2.2 与前面讲义的衔接

- **u3-l2 AtomNetlist**：时序图的「原材料」就是原子级网表的块（block）、引脚（pin）、网（net）。
- **u3-l4 VprContext**：时序图和约束最终住进 `TimingContext`，经 `g_vpr_ctx` 全流程共享。
- **u3-l5 vpr_api**：本讲会看到 `vpr_init_with_options` 在初始化阶段调用 `TimingGraphBuilder`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `vpr/src/timing/timing_graph_builder.h` / `.cpp` | 把 `AtomNetlist` 翻译成 `tatum::TimingGraph` 的构建器，本讲主角 |
| `vpr/src/base/vpr_context.h` | 定义 `TimingContext`（持有 graph 与 constraints） |
| `vpr/src/base/vpr_api.cpp` | 在 `vpr_init_with_options` 中真正调用构建器、给 `TimingContext` 赋值 |
| `libs/EXTERNAL/libtatum/libtatum/tatum/TimingGraphFwd.hpp` | Tatum 的节点类型 `NodeType`、边类型 `EdgeType`、各种 ID 的权威定义 |
| `vpr/src/base/atom_lookup.h` / `atom_lookup_fwd.h` | `AtomLookup` 维护「原子引脚 ↔ 时序节点」的双向映射，以及 `BlockTnode` 枚举 |
| `libs/EXTERNAL/libtatum/README.md` | Tatum 的定位与设计目标（外部库，不可在本仓库直接改） |
| `vpr/src/timing/timing_info.h` | `TimingInfo` 接口，定义「如何触发一次时序更新」 |

## 4. 核心概念与源码讲解

### 4.1 Tatum 集成：VPR 为什么把 STA 委托给外部引擎

#### 4.1.1 概念说明

VPR 的布局退火一次迭代要评估成千上万次移动，布线更是在百万次量级上估算延迟。每一次评估都想知道「这会不会让关键路径变慢」。如果每次都从头算一遍全芯片时序，根本跑不动。因此 VPR 需要：

1. 一个**极快**的 STA 引擎；
2. 一个**可增量更新**的接口——延迟变了，只重算受影响的部分；
3. 一个**正确、经过同行检验**的实现，避免自己重造轮子。

VTR 团队为此开发了独立的库 **Tatum**，并以 `libtatum` 形式集成进 VPR。注意 Tatum 位于 `libs/EXTERNAL/` 外部子树下，按照 u1-l3 讲过的规则，**不能在 VTR 仓库内直接修改**，必须先改上游再用 `dev/external_subtrees.py` 同步。

Tatum 的 README 给出了它的定位与三大性能特性——这是一段非常值得先读的总览：

> Tatum is a block-based Static Timing Analysis (STA) engine suitable for integration with Computer-Aided Design (CAD) tools... Tatum operates on an abstract *timing graph* constructed by the host application, and can be configured to use an application defined delay calculator.
>
> [README.md:5-12](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/EXTERNAL/libtatum/README.md#L5-L12) — Tatum 的定位：宿主程序（VPR）负责建图并提供延迟计算器，Tatum 负责在图上做时序遍历。

关键的一句是分工：**「Tatum 操作的是宿主程序构建的抽象时序图」**。这意味着 VPR 与 Tatum 之间有一个清晰的契约——VPR 的职责是「把网表变成图 + 告诉每条边延迟是多少」，Tatum 的职责是「在图上跑 setup/hold、多时钟、时序例外」。

#### 4.1.2 核心流程

VPR 与 Tatum 的协作可以画成三步：

```text
   VPR（宿主）                              Tatum（STA 引擎）
┌─────────────────────┐                ┌──────────────────────┐
│ AtomNetlist          │                │                      │
│   + AtomLookup       │                │                      │
│   + LogicalModels    │                │                      │
│        │             │                │                      │
│        ▼  TimingGraphBuilder          │                      │
│  tatum::TimingGraph ────────────────► │  TimingAnalyzer      │
│  (节点 + 边的拓扑)    │                │   (setup/hold 遍历)   │
│                      │  延迟回调        │                      │
│  DelayCalculator ───────────────────► │  每个 edge 的延迟      │
│                      │                │        │              │
│  TimingConstraints ─────────────────► │  时钟周期、假/真路径    │
│  (时钟周期等约束)     │                │        ▼              │
│                      │  ◄──────────── │  arrival/required/slack│
└─────────────────────┘   时序结果       └──────────────────────┘
```

Tatum 的三大性能特性（README 第 13–16 行）解释了为什么这种分工能跑得动：

1. **一次遍历算完所有时钟与 setup/hold**——不是每个时钟、每种检查各跑一遍。
2. **数据结构面向 CPU 缓存优化**（这正是下一节 `opt_memory_layout` 要配合的）。
3. **支持多核并行分析**。

> [README.md:13-16](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/EXTERNAL/libtatum/README.md#L13-L16) — Tatum 的三大性能特性：单次遍历、缓存友好、并行。

#### 4.1.3 源码精读

VPR 一侧对「如何驱动 Tatum 做一次分析」抽象出了 `TimingInfo` 接口。它的两个核心方法是理解「触发时机」的钥匙：

> [timing_info.h:30-34](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/timing_info.h#L30-L34) — `invalidate_delay(edge)` 标记某条边延迟已变（「失效」），`update()` 重算所有受影响的时序信息。

注意这个接口刻意**只暴露更新、不暴露结果细节**（注释第 16–21 行说明：调用方知道自己改了实现、需要刷新时序，但不必关心刷新的是 setup 还是 hold）。本讲先建立这个「图是静态的、延迟和结果是动态的」直觉，具体的延迟计算器与结果访问留到 u7-l2。

#### 4.1.4 代码实践

1. **实践目标**：亲手确认 Tatum 与 VPR 的职责边界。
2. **操作步骤**：打开 `libs/EXTERNAL/libtatum/README.md`，阅读 Overview、Why was Tatum created、Projects using Tatum 三节。
3. **需要观察的现象**：注意 README 明确写着 Tatum「operates on an abstract timing graph **constructed by the host application**」——这正是 `TimingGraphBuilder` 存在的根本理由。
4. **预期结果**：用一句话写出「Tatum 负责 ___，VPR 负责 ___」的分工（参考答案见小练习）。

#### 4.1.5 小练习与答案

**练习 1**：Tatum 的三大性能特性分别对应 STA 的哪个痛点？

**参考答案**：
- 「单次遍历算完所有时钟与分析」→ 对应「多时钟 FPGA 反复分析太慢」；
- 「缓存友好」→ 对应「图很大、遍历是内存密集型」；
- 「多核并行」→ 对应「单核算不动超大规模器件」。

**练习 2**：如果要在 VTR 树内修改 Tatum 的一个 bug，正确流程是什么？

**参考答案**：Tatum 在 `libs/EXTERNAL/` 下属于外部子树，不能直接改 VTR 树里的副本；应先在 Tatum 上游修，再用 `dev/external_subtrees.py` 同步进来（见 u1-l3 的外部子树规则）。

---

### 4.2 时序图的节点与边模型

#### 4.2.1 概念说明

要把网表变成 Tatum 的时序图，必须先约定「网表里的东西，分别对应图里的什么」。Tatum 在 `TimingGraphFwd.hpp` 里定义了**五种节点类型**和**四种边类型**，这是整个时序分析的「词汇表」。

**节点类型 `tatum::NodeType`**：

> [TimingGraphFwd.hpp:18-24](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/EXTERNAL/libtatum/libtatum/tatum/TimingGraphFwd.hpp#L18-L24) — 五种节点类型的权威定义。

| 类型 | 含义 | 典型来源 |
|------|------|----------|
| `SOURCE` | 一条时钟/数据路径的**起点** | 触发器 Q 端、主输入、时钟生成器（PLL）输出 |
| `SINK` | 一条时钟/数据路径的**终点** | 触发器 D 端、主输出 |
| `IPIN` | 块的**组合输入**引脚（中间节点） | LUT 的输入引脚 |
| `OPIN` | 块的**组合输出**引脚（中间节点） | LUT 的输出引脚 |
| `CPIN` | 块的**时钟输入**引脚（中间节点） | 触发器的 clk 引脚 |

一个直觉记法：`SOURCE`/`SINK` 是「逻辑端点」（路径从这里出发、到这里结束），`IPIN`/`OPIN`/`CPIN` 是「中间引脚」。所以 `SOURCE` 不该有入边（除非来自 `CPIN`），`SINK` 不该有出边。

**边类型 `tatum::EdgeType`**：

> [TimingGraphFwd.hpp:28-33](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/EXTERNAL/libtatum/libtatum/tatum/TimingGraphFwd.hpp#L28-L33) — 四种边类型的权威定义。

| 类型 | 含义 |
|------|------|
| `PRIMITIVE_COMBINATIONAL` | 原语内部的组合通路（如 LUT 输入→输出） |
| `PRIMITIVE_CLOCK_LAUNCH` | 时钟「发射」数据：从 `CPIN` 到数据 `SOURCE` |
| `PRIMITIVE_CLOCK_CAPTURE` | 时钟「捕获」数据：从 `CPIN` 到数据 `SINK` |
| `INTERCONNECT` | 原语之间的网连接（块到块的布线） |

`NodeId`、`EdgeId`、`LevelId`、`DomainId` 都是 `tatum::StrongId`（见 u3-l1 讲过的类型安全 ID 设计）：

> [TimingGraphFwd.hpp:46-48](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/EXTERNAL/libtatum/libtatum/tatum/TimingGraphFwd.hpp#L46-L48) — `NodeId`/`EdgeId`/`LevelId` 均为 StrongId 模板实例，编译期杜绝混用。

#### 4.2.2 核心流程：一个原语如何变成子图

`timing_graph_builder.cpp` 顶部那段超长的 ASCII 注释是理解整个模型的「教科书」，它演示了一个含两个触发器（A、B）和两团组合逻辑（C、D）的原语块，如何被翻译成时序图。核心规则有两条：

1. **组合逻辑**用两个引脚节点 + 一条 `PRIMITIVE_COMBINATIONAL` 边表示。例如组合逻辑 D 就是 `IPIN e ──► OPIN g`，边代表穿过 D 的延迟。
2. **时序元件（触发器）**用三个节点表示：数据端 `SINK`（D 端）、数据输出端 `SOURCE`（Q 端）、时钟端 `CPIN`。时钟到数据的发射/捕获关系用 `PRIMITIVE_CLOCK_LAUNCH` / `PRIMITIVE_CLOCK_CAPTURE` 边表示。

```text
        原语块内部（含触发器 A、B 与组合逻辑 C、D）

   e ────────────────► [组合 D] ──────────────► g          (组合通路)
   e ──┐                                       ┌──► h
       ▼                                        │
   [触发器 A] ──► [组合 C] ──► [触发器 B] ──────┘          (时序 + 组合)
       ▲                          ▲
       │                          │
   clk ───────────────────────────┘                       (同一个 clk)

        翻译成时序子图（简化版，详见 .cpp 注释）

   IPIN e ──────────(PRIMITIVE_COMBINATIONAL)──────────► OPIN g
     │                                                       ▲
     │              (PRIMITIVE_COMBINATIONAL)                │
     └────► SRC A ──────────────────────────► SINK B          │
              ▲                                  ▲            │
              │  LAUNCH                          │  CAPTURE   │
              │                                  │            │
              CPIN clk ──────────────────────────┴────────────┘
```

#### 4.2.3 「外部 / 内部 tnode」的区分

VPR 在 Tatum 的节点类型之上，额外给每个 tnode 打了一个**外部/内部**标签（`BlockTnode`），这是 VPR 自己为了管理「原子引脚 ↔ tnode」映射而引入的：

> [atom_lookup_fwd.h:5-8](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/atom_lookup_fwd.h#L5-L8) — `BlockTnode` 枚举：`INTERNAL`（原语内部路径的 tnode）与 `EXTERNAL`（原语外部接口的 tnode）。

规则（出自 `.cpp` 第 155–216 行注释）：

- **组合引脚**（LUT 输入/输出）：internal 与 external **指向同一个 tnode**，只是打两份标签方便查询。
- **时序引脚**（触发器 D/Q 端）：除了 external tnode，还会多建一个 internal tnode，用来表示「原语内部从 Q 出发或到达 D 的那条路径」。`.cpp` 注释里 SRC f/SRC B 那种标 `(internal)` 的就是这种。

这个区分**不影响时序分析结果**（结果只取决于图的拓扑），只影响 VPR 内部「网表引脚 ↔ tnode」的查表。理由：VPR 的大部分阶段只关心原语的外部接口，少数阶段（如延迟计算器）才需要内部 tnode。

#### 4.2.4 代码实践

1. **实践目标**：把一个「LUT + 触发器」的原子块映射到节点/边类型。
2. **操作步骤**：在 `timing_graph_builder.cpp` 阅读第 31–218 行的 ASCII 教学注释，对照本节的类型表。
3. **需要观察的现象**：触发器 A 的数据输入 f、数据输出、时钟 clk 分别被建模成哪种 `NodeType`。
4. **预期结果**：f → `SINK`（external）+ 一个 `SOURCE`（internal，若存在内部组合出口）；输出 → `SOURCE`（external）；clk → `CPIN`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `SOURCE` 节点「原则上没有入边，除非来自 `CPIN`」？

**参考答案**：`SOURCE` 是一条数据/时钟路径的起点，按定义信号从这里「发源」。唯一合法的入边是时钟引脚 `CPIN` 通过 `PRIMITIVE_CLOCK_LAUNCH` 建立的「时钟发射数据」关系（见 `.cpp` 注释第 12–15 行）。

**练习 2**：组合引脚为什么要把 internal 与 external 标签指向同一个 tnode？

**参考答案**：组合引脚既参与原语内部通路（IPIN→OPIN），又是原语对外接口（被 INTERCONNECT 边连到别的块）。指向同一个 tnode 能让「查内部映射」和「查外部映射」都命中，免去维护两套等价节点。

---

### 4.3 时序图构建：TimingGraphBuilder

#### 4.3.1 概念说明

`TimingGraphBuilder` 就是 4.1 里那张图中的「翻译官」——它吃进 `AtomNetlist`（+ `AtomLookup` + `LogicalModels`），吐出一个 `tatum::TimingGraph`。它的设计哲学（见头文件注释）是：**先建每个原语的内部子图，再用网把它们缝合起来**。

> [timing_graph_builder.h:12-22](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/timing_graph_builder.h#L12-L22) — 类注释：从 `AtomNetlist` 构造 `tatum::TimingGraph`，并回填 `AtomLookup` 的引脚→tnode 映射。

#### 4.3.2 核心流程

构建分两大阶段，外加优化与校验。主入口 `timing_graph()` 的骨架：

> [timing_graph_builder.cpp:261-271](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/timing_graph_builder.cpp#L261-L271) — `timing_graph()`：先 `build()` 建图，再 `opt_memory_layout()` 重排，最后 `validate()` + 自检。

真正的建图逻辑在 `build()`：

> [timing_graph_builder.cpp:274-310](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/timing_graph_builder.cpp#L274-L310) — `build()` 的四步：遍历块建子图 → 遍历网缝合 → 断组合环 → 层级化。

可以拆成下面这条流水线：

```text
build()
  │
  ├─ 1. 遍历 AtomNetlist.blocks()
  │     ├─ INPAD/OUTPAD → add_io_to_timing_graph()    (主输入=SOURCE，主输出=SINK)
  │     └─ BLOCK        → add_block_to_timing_graph() (建子图节点 + 内部边)
  │
  ├─ 2. 遍历 AtomNetlist.nets()
  │     └─ add_net_to_timing_graph()  (为每条网加 INTERCONNECT 边，缝合子图)
  │
  ├─ 3. fix_comb_loops()              (若不是 DAG，断掉组合环)
  │
  └─ 4. tg_->levelize()               (算拓扑序与每层节点，供遍历器用)
```

**第一步：每个原语的子图。** `add_block_to_timing_graph()` 把建节点和建边分开：

> [timing_graph_builder.cpp:360-397](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/timing_graph_builder.cpp#L360-L397) — 先 `create_block_timing_nodes()` 建节点，再分别建数据边与时钟边。

建节点的核心是「看引脚的端口模型（`t_model_ports`）决定它是什么类型」——这正体现了 u3-l2 讲过的「原子块类型由模型 ID 推导」：

> [timing_graph_builder.cpp:402-517](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/timing_graph_builder.cpp#L402-L517) — `create_block_timing_nodes()`：输入引脚按 `model_port->clock` 是否为空区分组合（IPIN）/时序（SINK）；时钟引脚建 CPIN；输出引脚按是否为时钟源、是否挂时钟区分 SOURCE/OPIN。

三种关键判定（节选）：

- 输入引脚：`model_port->clock` 为空 → 组合输入，建 `IPIN`；否则 → 时序数据端，建 `SINK`。
- 输出引脚：若是网表时钟源（`is_netlist_clock_source`）→ 建 `SOURCE`（PLL 之类时钟生成器）；否则无时钟 → `OPIN`，有时钟 → `SOURCE`。
- 时钟引脚：一律建 `CPIN`。

**第二步：用网缝合。** 子图之间还互不相连，`add_net_to_timing_graph()` 为每条网从驱动引脚的 tnode 到每个接收引脚的 tnode 加一条 `INTERCONNECT` 边：

> [timing_graph_builder.cpp:670-690](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/timing_graph_builder.cpp#L670-L690) — `add_net_to_timing_graph()`：无驱动则跳过（无时序依赖）；否则对每个 sink 加 `INTERCONNECT` 边。注意此时尚不赋延迟——延迟在分析阶段由延迟计算器提供。

**第三步：断组合环。** STA 要求图是 DAG。如果电路里有纯组合的反馈环（如某些自激振荡或 latch 组合环），`fix_comb_loops()` 用强连通分量（SCC）找出环并禁用其中一条边：

> [timing_graph_builder.cpp:692-709](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/timing_graph_builder.cpp#L692-L709) — `fix_comb_loops()`：`tatum::identify_combinational_loops()` 找 SCC，`find_scc_edge_to_break()` 选边，`disable_edge()` 禁用，循环直到无环。

**优化与校验。** 建完图后，`opt_memory_layout()` 调 Tatum 的 `optimize_layout()` 把节点/边按遍历顺序重排以提升缓存命中（对应 4.1 提到的「缓存友好」特性），随后 `remap_ids()` 把 `AtomLookup` 里旧的 tnode 编号更新成重排后的新编号：

> [timing_graph_builder.cpp:313-324](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/timing_graph_builder.cpp#L313-L324) — `opt_memory_layout()` + `remap_ids()`：重排后旧 NodeId 失效，必须同步更新 AtomLookup 里的映射。

最后 `tg_->validate()` 与 `validate_netlist_timing_graph_consistency()`（`.cpp` 第 753–804 行）做双向一致性自检：每个原子引脚都有 external tnode、引脚↔tnode 双向查表一致、组合/时序引脚的 internal/external 类型配对正确。任何不一致都直接 `VPR_ERROR`。

#### 4.3.3 源码精读：构造函数与接口

构建器本身是无状态的工具对象，构造时只持有引用，并把「网表里哪些引脚是逻辑时钟驱动」提前算好缓存：

> [timing_graph_builder.h:23-29](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/timing_graph_builder.h#L23-L29) — 构造函数接收 `AtomNetlist&`、`AtomLookup&`、`LogicalModels&`；公开入口只有 `timing_graph(bool allow_dangling_combinational_nodes)`。

> [timing_graph_builder.cpp:252-259](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/timing/timing_graph_builder.cpp#L252-L259) — 构造函数初始化三个引用成员，并调用 `find_netlist_logical_clock_drivers()` 预先算出网表时钟驱动引脚集合（供 `is_netlist_clock_source()` 判定时钟生成器用）。

#### 4.3.4 代码实践

1. **实践目标**：跟踪一次完整的建图过程，把 `build()` 的四步与具体函数对上号。
2. **操作步骤**：
   - 打开 `timing_graph_builder.cpp`，定位 `build()`（约 274 行）。
   - 在第一步循环里，分别找到 `add_io_to_timing_graph`（约 327 行）和 `add_block_to_timing_graph`（约 360 行）。
   - 在第二步循环里，找到 `add_net_to_timing_graph`（约 670 行），确认它建的是 `INTERCONNECT` 边。
   - 确认 `fix_comb_loops`（约 692 行）与 `levelize`（约 309 行）的顺序。
3. **需要观察的现象**：注意 `add_net_to_timing_graph` **完全不读延迟**——它只建拓扑。延迟是分析阶段才注入的。
4. **预期结果**：能用四个动词概括建图：「建子图 → 缝合 → 断环 → 分层」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `add_net_to_timing_graph()` 对「无驱动引脚的网」直接跳过？

**参考答案**：无驱动的网没有任何信号源，也就没有时序依赖，自然没有边可建。代码里会打一条警告「Net has no driver and will be ignored for timing purposes」（`.cpp` 第 675–679 行）。

**练习 2**：`opt_memory_layout()` 之后为什么必须 `remap_ids()`？

**参考答案**：Tatum 的 `optimize_layout()` 重排了节点顺序，旧的 `NodeId` 不再有效。而 VPR 的 `AtomLookup` 里存的是旧编号的「引脚→tnode」映射，不更新就会查错节点，所以必须用返回的 `id_mapping` 重写一遍（`.cpp` 第 318–323、733–747 行）。

---

### 4.4 TimingContext 状态与赋值时机

#### 4.4.1 概念说明

建好的时序图需要有个「家」让全流程访问。这个家就是 `TimingContext`，它是 u3-l4 讲过的 `VprContext` 聚合体中的一个子上下文。它的成员非常精简：

> [vpr_context.h:133-156](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L133-L156) — `TimingContext`：`graph`（时序图）、`constraints`（SDC 约束）、`stats`（分析统计）、`terminate_if_timing_fails`（违例是否终止）。

两个核心成员是用 `shared_ptr` 持有的：

- `std::shared_ptr<tatum::TimingGraph> graph` —— 时序依赖的**拓扑**。
- `std::shared_ptr<tatum::TimingConstraints> constraints` —— 从 SDC 读入的约束（目标时钟周期、假/真路径等）。

之所以用 `shared_ptr`，是因为 Tatum 的分析器、延迟计算器等也要持有这两个对象的引用，`shared_ptr` 让生命周期由引用计数统一管理。

`TimingContext` 继承自不可拷贝的基类 `Context`（u3-l4 已讲）：

> [vpr_context.h:68-74](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L68-L74) — `Context` 基类删除拷贝构造/赋值，保证巨型状态只能按引用传递、不能被意外深拷贝。

全工程通过 `g_vpr_ctx` 取用，遵循「生产者取 mutable、消费者取 const」：

> [vpr_context.h:877-878](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h#L877-L878) — `timing()` 返回 `const TimingContext&`（只读），`mutable_timing()` 返回 `TimingContext&`（可写）。

#### 4.4.2 核心流程：graph 与 constraints 何时被赋值

这是本讲最重要的一段源码，位于 `vpr_init_with_options()`。时序图与约束**只在初始化阶段构建一次**：

> [vpr_api.cpp:340-360](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L340-L360) — 时序图与约束的构建：第 345 行把构建器产物赋给 `timing_ctx.graph`，第 355 行把 `read_sdc` 的产物赋给 `timing_ctx.constraints`。

把这段浓缩成关键事实：

| 对象 | 赋值位置 | 赋值语句（精简） | 触发条件 |
|------|----------|------------------|----------|
| `timing_ctx.graph` | `vpr_api.cpp:345` | `TimingGraphBuilder(netlist, lookup, models).timing_graph(...)` | `vpr_setup->TimingEnabled` 为真 |
| `timing_ctx.constraints` | `vpr_api.cpp:355` | `read_sdc(...)` | 同上 |

而 `TimingEnabled` 来自命令行开关 `--timing_analysis`：

> [vpr_api.cpp:247](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L247) — `vpr_setup->TimingEnabled = options->timing_analysis;`

```text
vpr_init_with_options()
  │
  ├─ 读 BLIF，建 AtomNetlist                    (line 330-331)
  │
  ├─ if (TimingEnabled) {                       (line 341)
  │     ├─ graph = TimingGraphBuilder(...).timing_graph(...)   ← 拓扑，只建一次
  │     ├─ 打印 Nodes/Edges/Levels 数量
  │     ├─ constraints = read_sdc(...)           ← 约束，只读一次
  │     └─ set_terminate_if_timing_fails(...)
  │   }
  │
  └─ ...（继续 floorplan/route 约束等）
```

#### 4.4.3 关键结论：图是静态的，分析是动态的

这是理解 VPR 时序机制最容易被新手搞混的一点，请特别留意：

1. **时序图的拓扑在整个 place/route 过程中不变。** 网表的连接关系（哪个引脚连哪个引脚）在初始化时就固定了，所以 `graph` 只在 init 赋值一次，之后不再重建。你可以在 `vpr_api.cpp` 里搜 `mutable_timing().graph =`，只会命中第 345 行这一处。

2. **变化的是每条边上的延迟。** 随着布局（块的位置变了）和布线（走线变了），`INTERCONNECT` 边的延迟不断变化。VPR 的做法是：延迟变了就调 `TimingInfo::invalidate_delay(edge)` 标记失效，再调 `update()` 让 Tatum 在**同一张图**上增量重算。这正是 4.1.3 看到的接口。

3. **不同阶段用不同的延迟计算器**，但喂的是同一张图：打包阶段用 PreCluster 延迟、布局阶段用布局延迟、布线后用 RoutingDelayCalculator。这部分细节留到 u7-l2。

一句话总结触发时机：

\[
\underbrace{\text{建图（一次）}}_{\text{init，TimingGraphBuilder}} \;\;+\;\; \underbrace{\text{反复分析（多次）}}_{\text{各阶段 invalidate\_delay + update}}
\]

#### 4.4.4 代码实践（本讲主实践任务）

> 对应规格中的实践要求：在 `timing_graph_builder.h` 中找出构建时序图所需输入，并说明 `TimingContext` 中的 `graph` 与 `constraints` 何时被赋值。

1. **实践目标**：亲自确认建图的三类输入，以及 graph/constraints 的赋值时机。
2. **操作步骤**：
   - 打开 `timing_graph_builder.h`，找到构造函数声明（第 25–27 行），列出它的三个参数及其语义：
     - `const AtomNetlist& netlist` —— 原子网表，建图的拓扑来源；
     - `AtomLookup& netlist_lookup` —— 建好的「引脚↔tnode」映射要回填到这里（注意是非 const 引用，会被修改）；
     - `const LogicalModels& models` —— 原语逻辑模型，用来判定引脚是组合/时序/时钟。
   - 打开 `vpr/src/base/vpr_api.cpp` 第 340–360 行，确认：
     - `graph` 在第 345 行由 `TimingGraphBuilder(...).timing_graph(...)` 赋值；
     - `constraints` 在第 355 行由 `read_sdc(...)` 赋值；
     - 两者都受第 341 行 `if (vpr_setup->TimingEnabled)` 保护。
   - 用 `grep` 或编辑器在 `vpr_api.cpp` 中搜索 `timing_ctx.graph =` 与 `mutable_timing().graph =`，确认 graph 在主流程中**只赋值这一次**（拓扑静态）。
3. **需要观察的现象**：赋值后立即打印 Nodes/Edges/Levels 数量（第 346–348 行），说明图已经「成形且固定」。
4. **预期结果**：能写出「graph 在 init 由 `TimingGraphBuilder` 一次性构建并赋给 `timing_ctx.graph`，之后不重建；constraints 紧随其后由 `read_sdc` 赋值；二者都受 `TimingEnabled` 开关控制」。

#### 4.4.5 小练习与答案

**练习 1**：`graph` 与 `constraints` 为什么用 `shared_ptr` 而不是直接值成员？

**参考答案**：Tatum 的分析器（`TimingAnalyzer`）、延迟计算器（`DelayCalculator`）等也要引用这两个对象，`shared_ptr` 让 `TimingContext` 与这些分析组件共享同一份图/约束，生命周期由引用计数统一管理，避免悬空指针或重复拷贝。

**练习 2**：如果运行 VPR 时加了 `--timing_analysis off`，会发生什么？

**参考答案**：`vpr_setup->TimingEnabled` 为假，第 341 行的 `if` 整段跳过，`timing_ctx.graph` 与 `constraints` 保持空 `shared_ptr`。后续任何依赖时序的阶段都要么跳过、要么走非时序路径——这印证了 u3-l5 讲过的「阶段间数据依赖由 `g_vpr_ctx` 决定」。

---

## 5. 综合实践

把本讲的三块知识（节点/边模型、构建流程、TimingContext）串起来，完成下面这个「从原子引脚到时序边」的追踪任务：

**任务**：选取一个最简单的含组合 + 时序的设计（例如一个 LUT 输出直接驱动一个 D 触发器的 D 端，触发器 Q 端连到一个输出端口）。

1. **画原子网表**：写出涉及的原子块、引脚、网（例如 `lut.out ── net1 ── ff.D`，`ff.Q ── net2 ── outpad`）。
2. **画时序子图**：对照 4.2 的规则，为 LUT 画 `IPIN → OPIN`（`PRIMITIVE_COMBINATIONAL` 边），为触发器画 `SINK(D) / SOURCE(Q) / CPIN(clk)` 及 `PRIMITIVE_CLOCK_LAUNCH`、`PRIMITIVE_CLOCK_CAPTURE` 边。标出哪些是 external tnode、哪些还要补 internal tnode。
3. **缝合**：用 `INTERCONNECT` 边把 `lut.OPIN → ff.SINK(D)`、`ff.SOURCE(Q) → outpad` 连起来（对应 `add_net_to_timing_graph`）。
4. **定位赋值**：在 `vpr_api.cpp` 第 345 行确认这张图最终被赋给 `g_vpr_ctx.mutable_timing().graph`。
5. **回答触发问题**：如果布线后 `net1` 的延迟变了，VPR 不会重建图，而是走哪两个 `TimingInfo` 方法来重算？（答案：`invalidate_delay` + `update`。）

完成后，你应当能用一张图同时讲清「拓扑（静态）」与「延迟（动态）」的分离——这正是 VPR 把 STA 委托给 Tatum 的工程精髓。

## 6. 本讲小结

- **Tatum 是外部 STA 引擎**：位于 `libs/EXTERNAL/`，VPR 负责建图 + 提供延迟计算器，Tatum 负责在图上做 setup/hold、多时钟遍历；三大特性是单次遍历、缓存友好、并行。
- **节点五类、边四类**：`NodeType` = SOURCE/SINK/IPIN/OPIN/CPIN；`EdgeType` = PRIMITIVE_COMBINATIONAL / PRIMITIVE_CLOCK_LAUNCH / PRIMITIVE_CLOCK_CAPTURE / INTERCONNECT，权威定义在 `TimingGraphFwd.hpp`。
- **构建分四步**：`TimingGraphBuilder::build()` 先建每个原语子图，再用 `INTERCONNECT` 边按网缝合，接着 `fix_comb_loops` 断组合环，最后 `levelize` 分层；之后 `opt_memory_layout` 重排并 `remap_ids` 同步 `AtomLookup`。
- **外部/内部 tnode**：组合引脚 internal=external 同一节点，时序引脚额外补 internal 节点；区分只影响 VPR 的引脚映射，不影响分析结果。
- **TimingContext 持有 graph + constraints**：二者以 `shared_ptr` 存放，继承不可拷贝基类 `Context`，经 `g_vpr_ctx.timing()/mutable_timing()` 访问。
- **图静态、分析动态**：`graph` 与 `constraints` 在 `vpr_init_with_options` 中各赋值一次（受 `TimingEnabled` 保护），place/route 期间拓扑不变，只通过 `invalidate_delay` + `update` 增量重算。

## 7. 下一步学习建议

本讲只解决了「时序图怎么建、住在哪里」。但图建好后，每条边的**延迟**到底从哪来、SDC 约束如何读入、布线后的关键路径报告怎么生成——这些是下一讲 u7-l2「时序约束、延迟计算与布线后报告」的内容。建议接着：

1. 读 `vpr/src/timing/read_sdc.h`，看 SDC 约束如何变成 `TimingConstraints`（本讲第 355 行 `read_sdc` 的展开）。
2. 读 `vpr/src/timing/PreClusterTimingManager` 与 `RoutingDelayCalculator`，对比不同阶段如何给同一张图的边注入延迟。
3. 读 `vpr/src/analysis/timing_reports.h`，看布线后的关键路径与 slack 报告如何从 Tatum 结果生成。

在进入 u7-l2 前，回看本讲第 4.4.3 节「图静态、分析动态」这一结论，它会是你理解所有阶段延迟计算的统一视角。
