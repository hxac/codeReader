# 仓库目录结构与组件地图

## 1. 本讲目标

通过本讲，你应当能够：

- 看懂 VTR 仓库**顶层**每一个目录的用途，知道遇到哪类问题应该进哪个目录。
- 掌握核心工具 VPR 的内部子目录（`vpr/src/`）是如何按「CAD 阶段」划分责任的。
- 区分**本项目自主维护的代码**与**外部子树（external subtree）代码**，知道哪些目录绝对不能直接改。
- 拿到任何一个报错或需求，能快速在庞大的代码树里定位到对应的目录。

本讲只讲「目录与组件地图」，不深入任何一个算法。它是一张导航图，后续所有讲义都会在这张图上展开。

## 2. 前置知识

阅读本讲前，你需要已经建立以下认知（来自前序讲义）：

- **VTR 是什么**：一个开源 FPGA CAD 框架，输入是电路的 Verilog 描述和目标 FPGA 的架构 XML，输出是速度（时序）与面积评估。流程分三段：**PARMYS → ABC → VPR**。
- **「架构驱动」设计理念**：目标 FPGA 由运行时传入的 XML 决定，算法代码不硬编码架构假设；架构在启动时被解析进共享库 `libarchfpga` 的数据结构。
- **构建方式**：在仓库根目录运行 `make`，构建产物落在 `build/`，`vpr` 可执行文件默认在 `build/vpr/vpr`。

本讲会在这套认知之上，把「源码到底放在哪里」这件事讲清楚。你只需要会用命令行列目录即可。

> 名词速查：
> - **CAD（Computer-Aided Design）**：计算机辅助设计。FPGA CAD 指把高层电路描述自动映射到 FPGA 物理结构的一系列工具。
> - **子树（subtree）**：用 `git subtree` 从另一个上游仓库「拷贝」进来的一整棵目录，能跟随上游更新。
> - **BLIF**：一种描述逻辑网表的文本格式（.blif），是很多 CAD 工具之间的交换格式。

## 3. 本讲源码地图

本讲主要阅读以下文件，它们是 VTR 仓库布局的「权威说明」：

| 文件 | 作用 |
|------|------|
| [`doc/agents/codebase.md`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/codebase.md) | 官方撰写的代码库布局说明，包含顶层组件表、VPR 内部结构表、共享库表、外部子树规则。本讲的主干。 |
| [`README.md`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/README.md) | 项目总览，说明 PARMYS/ABC/VPR 三段流程各自做什么。 |
| [`libs/README.md`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/README.md) | 共享库目录说明，明确「EXTERNAL 下的库不得在 VTR 树内直接修改」。 |

此外，本讲用实际目录列表与若干辅助文件（`utils/CMakeLists.txt`、`blifexplorer/README.md`、`dev/` 下的脚本）对官方文档做了**核对与补充**——官方表格是一张精简的概念图，实际代码树更丰富。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **顶层组件地图**：仓库根目录下每个目录是什么。
2. **VPR 内部子目录职责**：核心工具 `vpr/src/` 是怎么按 CAD 阶段切分的。
3. **外部子树 `libs/EXTERNAL` 规则**：哪些代码属于「不可直接修改」的外部依赖。

### 4.1 顶层组件地图

#### 4.1.1 概念说明

VTR 是一个**多工具协作**的框架，不是一个单一程序。它的根目录本质上是一个「工具集合 + 共享库 + 流程脚本 + 文档」的工作区。你可以把它理解成一座小工厂：

- 几台**机器**（各阶段的工具：综合器、优化器、打包/布局/布线器）。
- 一个**公共零件库**（`libs/`，所有机器共享的数据结构与解析器）。
- 一条**流水线调度台**（`vtr_flow/`，把机器按顺序串起来跑）。
- 一本**操作手册**（`doc/`）和一个**工具间**（`dev/`、`utils/`）。

理解顶层目录的关键，是先把「VTR 三段流程」与「具体目录」对应起来。回顾前序讲义，三段流程是：

```
PARMYS（综合与部分映射） → ABC（逻辑优化与技术映射） → VPR（打包/布局/布线/时序）
```

这三段分别对应仓库里不同的顶层目录。

#### 4.1.2 核心流程

给定一个电路与一个架构，VTR 顶层各目录的协作关系大致如下：

```text
          Verilog 电路                       FPGA 架构 XML
              │                                    │
              ▼                                    │
        ┌───────────┐                              │
        │ parmys/   │  PARMYS：综合 + 部分映射       │
        └─────┬─────┘                              │
              ▼  (.blif 网表)                       │
        ┌───────────┐                              │
        │  abc/     │  ABC：逻辑优化 + 技术映射        │
        └─────┬─────┘                              │
              ▼  (原子级网表)              架构 XML──┘
        ┌───────────┐                              │
        │  vpr/     │  VPR：打包/布局/布线/时序  ←── 架构由 libarchfpga 解析
        └───────────┘
```

- `parmys/` 产出网表，`abc/` 优化网表，`vpr/` 把网表落到具体 FPGA 架构上。
- 全程共享 `libs/` 里的数据结构与解析器。
- `vtr_flow/` 负责把上面三段用脚本串成一条可一键运行的流水线。

#### 4.1.3 源码精读

官方文档 [`doc/agents/codebase.md`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/codebase.md) 用一张表概括了顶层组件：

> 这段是顶层目录的权威定义：[doc/agents/codebase.md:3-15](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/codebase.md#L3-L15) —— 列出了 `vpr/`、`parmys/`、`odin_ii/`、`abc/`、`ace2/`、`libs/`、`vtr_flow/`、`utils/`、`doc/` 九个核心目录及其用途。

三段流程则写在项目总览里：[README.md:8-11](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/README.md#L8-L11) —— 明确「综合与部分映射 → 逻辑优化与技术映射 → 打包/布局/布线/时序」三步。

把官方表格与实际目录列表结合，得到下面这张**核对后的顶层地图**（★ 表示核心开发目录）：

| 目录 | 用途 | 维护方 | 备注 |
|------|------|--------|------|
| ★ `vpr/` | 核心 FPGA CAD 工具：打包、布局、布线、时序 | 本仓库 | **主开发目标** |
| ★ `parmys/` | 基于 Yosys 的综合前端（取代 Odin） | 本仓库 | PARMYS 段 |
| `odin_ii/` | 旧版综合前端（默认禁用） | 本仓库 | 历史遗留，已被 parmys 取代 |
| `abc/` | 逻辑优化 | **外部** | 不要直接改 |
| `ace2/` | 功耗分析所需的活动性（activity）估计 | 本仓库 | 为 `vpr/` 的功耗阶段提供输入 |
| `blifexplorer/` | BLIF/Odin II 可视化工具 | 本仓库 | 可视化浏览网表与仿真 |
| ★ `libs/` | 共享库（架构解析、通用工具、RR 图等） | 混合 | 见模块 4.3 |
| `vtr_flow/` | 流程脚本、架构文件、基准电路、回归任务 | 本仓库 | 流水线调度台 |
| `utils/` | 独立小工具：`fasm`、`route_diag`、`vqm2blif` | 本仓库 | 见下方验证 |
| `doc/` | RST 文档（发布于 docs.verilogtorouting.org） | 本仓库 | 手册 |
| `dev/` | 开发者脚本（格式化、lint、子树同步等） | 本仓库 | 工具间 |
| `verilog_preprocessor/` | 一个极简的 Verilog 预处理器（单文件 C++） | 本仓库 | 辅助工具 |
| `cmake/` | CMake 构建辅助模块 | 本仓库 | 支撑构建系统 |

`utils/` 里到底有什么，可以由它的构建文件直接证实：[utils/CMakeLists.txt:1-3](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/utils/CMakeLists.txt#L1-L3) —— 构建三个子工具 `fasm`（生成 FASM 比特流描述）、`route_diag`（布线诊断）、`vqm2blif`（VQM 转 BLIF）。

`blifexplorer/` 的用途见其自述：[blifexplorer/README.md:1-5](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/blifexplorer/README.md#L1-L5) —— 可视化 BLIF 文件、浏览网表并做分步仿真。

> 经验法则：官方 `codebase.md` 是一张**精简概念图**，实际代码树多出 `blifexplorer/`、`dev/`、`verilog_preprocessor/`、`cmake/` 等辅助目录。定位问题时先信概念图，再用 `ls` 核对实际目录。

#### 4.1.4 代码实践

**实践目标**：用命令行亲自核对顶层目录，建立「文档表格 ↔ 实际目录」的对应关系。

**操作步骤**：

1. 在仓库根目录执行列目录命令：

   ```bash
   ls -d */
   ```

2. 对照本讲 4.1.3 的顶层地图表，把列出的目录逐一分类：
   - 哪些属于「PARMYS/ABC/VPR 三段流程」？
   - 哪些属于辅助工具 / 脚本 / 文档？
   - 哪些标记为外部不可改？

3. 进入 `utils/` 查看：

   ```bash
   ls utils/
   ```

   确认 `fasm/`、`route_diag/`、`vqm2blif/` 三个子目录确实存在。

**需要观察的现象**：`ls -d */` 的输出数量会**多于**官方 `codebase.md` 表格里的 9 个目录（至少多出 `blifexplorer/`、`dev/`、`verilog_preprocessor/`、`cmake/`）。

**预期结果**：你得到一份与实际仓库一致的目录清单，并能口头解释每个目录的职责。

> 本实践为只读操作，不修改任何源码。

#### 4.1.5 小练习与答案

**练习 1**：如果你要修改「布局算法」，应该进哪个顶层目录？如果只是想换个综合前端呢？

**参考答案**：布局算法在核心工具里，进 `vpr/src/place/`（详见模块 4.2）。换综合前端涉及 PARMYS 段，进 `parmys/`（旧版 `odin_ii/` 默认禁用）。

**练习 2**：`ace2/` 和 `abc/` 都是三个字母的顶层目录，但维护方不同。请说明区别。

**参考答案**：`abc/` 是**外部**逻辑优化工具，不要直接改；`ace2/` 是本仓库维护的**活动性估计**工具，为 VPR 功耗分析提供输入，可以改。

**练习 3**：官方 `codebase.md` 的顶层表里没有 `blifexplorer/`，但它在仓库里存在。这说明什么？

**参考答案**：说明官方文档是一张精简概念图，只列核心组件；实际代码树还包含辅助目录。定位问题时要文档与 `ls` 核对相结合。

---

### 4.2 VPR 内部子目录职责

#### 4.2.1 概念说明

`vpr/` 是 VTR 的核心，也是绝大多数开发工作的目标。它本身又按 **CAD 阶段** 切成多个子目录。理解这种切分的关键，是先记住 VPR 内部的数据流：

```
AtomNetlist → Prepacker → Packer → ClusteredNetlist → Placer → Router
```

这条流的含义是：网表被不断改写、逐层抽象——从「原子级原语（LUT/FF）」打包成「逻辑块」，再布局到器件网格上的具体位置，最后用导线把逻辑块连起来。VPR 的每个子目录，基本就对应这条流里的一个阶段。

官方文档对内部结构的说明在这里：[doc/agents/codebase.md:17-32](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/codebase.md#L17-L32) —— 列出 `base/`、`pack/`、`place/`、`route/`、`timing/`、`analysis/`、`draw/`、`noc/`、`analytical_place/`、`power/`、`util/`、`server/` 各子目录的职责。

#### 4.2.2 核心流程

把子目录沿数据流摆开，就得到 VPR 的内部地图：

```text
            输入网表 + 架构
                  │
                  ▼
   ┌───────────────────────────────┐
   │ base/   核心数据结构 + 全局上下文 │  ← AtomNetlist、ClusteredNetlist、DeviceGrid
   └───────────────┬───────────────┘     g_vpr_ctx（所有阶段共享状态）
                   ▼
            ┌────────────┐
            │ pack/      │  打包：原子 → 逻辑块（ClusteredNetlist）
            └─────┬──────┘
                  ▼
   ┌──────────────────────┐   ┌────────────────────────┐
   │ place/  模拟退火布局   │   │ analytical_place/      │
   │（传统路径）            │   │ 解析式布局（替代路径）   │
   └──────────┬───────────┘   └────────────┬───────────┘
              ▼                            │
        ┌──────────┐                       │
        │ route/   │  迷宫布线：用 RR Graph 连线
        └────┬─────┘
             ▼
   ┌─────────────────────────────────┐
   │ timing/ + analysis/              │  静态时序分析 + 布线后报告
   └─────────────────────────────────┘
```

围绕这条主干，还有若干**横切**目录为各阶段服务：

| 子目录 | 角色 |
|--------|------|
| `base/` | 核心数据结构与全局上下文（**探索 VPR 的起点**） |
| `pack/` | 打包：原子网表 → 聚簇网表 |
| `place/` | 布局：模拟退火；含 `delay_model/`、`move_generators/`、`timing/` |
| `route/` | 布线：迷宫/连接路由器；含 `router_lookahead/`、`rr_graph_generation/` |
| `timing/` | 静态时序分析（用外部库 Tatum） |
| `analysis/` | 布线后分析与报告 |
| `analytical_place/` | 解析式布局引擎（传统打包布局的替代路径） |
| `noc/` | 片上网络（Network-on-Chip）支持 |
| `power/` | 功耗估计 |
| `draw/` | OpenGL/EZGL 图形可视化 |
| `server/` | Server 模式，供外部工具集成 |
| `util/` | 跨阶段共享工具（`vpr_utils.h`） |

数据流的官方表述：[doc/agents/codebase.md:34-50](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/codebase.md#L34-L50) —— 逐项说明 `AtomNetlist → Prepacker → Packer → ClusteredNetlist → Placer → Router`，并指出所有主要数据结构都通过 `g_vpr_ctx` 访问。

#### 4.2.3 源码精读

`base/` 被官方标注为「探索数据结构时的起点」：[doc/agents/codebase.md:21](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/codebase.md#L21) —— 说明 `base/` 放核心数据结构（atom netlist、clustered netlist、device grid），其中 `vpr_context.h` 定义所有主要上下文，`globals.h` 暴露全局访问器 `g_vpr_ctx`。

各阶段的「入口约定」也写在同一张表里，例如：

- 打包入口 `pack/`：[doc/agents/codebase.md:22](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/codebase.md#L22) —— 入口函数 `pack.h` / `try_pack()`。
- 布局入口 `place/`：[doc/agents/codebase.md:23](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/codebase.md#L23) —— 入口 `place.h` / `try_place()`，子目录 `delay_model/`、`move_generators/`、`timing/`。
- 布线入口 `route/`：[doc/agents/codebase.md:24](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/codebase.md#L24) —— 入口 `route.h` / `route()`，子目录 `router_lookahead/`、`rr_graph_generation/`。

关于全局状态的约定：[doc/agents/codebase.md:50](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/codebase.md#L50) —— 所有主要 VPR 数据结构都放在可通过 `g_vpr_ctx`（声明于 `vpr/src/base/globals.h`）访问的上下文里，`vpr_context.h` 是理解「每个阶段存在哪些数据」的最佳起点。

> 一句话记忆：**`base/` 是地基（数据结构 + 全局状态），其余子目录按 CAD 阶段一字排开，每个阶段都有自己的 `xxx.h` 入口函数。**

#### 4.2.4 代码实践

**实践目标**：亲自核对 `vpr/src/` 的子目录，并把它们对应到数据流的阶段。

**操作步骤**：

1. 列出 VPR 内部子目录：

   ```bash
   ls -d vpr/src/*/
   ```

2. 对每个子目录，在本讲 4.2.2 的地图表里找到对应行，标注它是「主干阶段」还是「横切服务」。

3. 挑选布局阶段，确认它的入口与子目录：

   ```bash
   ls vpr/src/place/
   ```

   应能看到 `place.h`、`placer.h` 等入口，以及 `delay_model/`、`move_generators/` 等子目录。

**需要观察的现象**：`vpr/src/` 下恰好有 12 个子目录，与官方 `codebase.md` 表格一致。

**预期结果**：你能指着 `vpr/src/` 下任意一个子目录，说出它属于数据流的哪个位置。

> 本实践为只读操作。运行结果以本地实际为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么官方文档建议「探索 VPR 数据结构时从 `base/` 开始」？

**参考答案**：因为 `base/` 集中了核心数据结构（AtomNetlist、ClusteredNetlist、DeviceGrid）和全局上下文（`vpr_context.h`、`globals.h` 里的 `g_vpr_ctx`），所有阶段都建立在这些结构之上。先看懂地基，再看各阶段才不会迷路。

**练习 2**：`analytical_place/` 与 `place/` 都做布局，它们是什么关系？

**参考答案**：`place/` 是传统的模拟退火布局路径；`analytical_place/` 是解析式布局引擎，是一条**替代路径**，它先用解析求解器做全局布局，再做合法化，可以绕过或包裹传统打包流程。

**练习 3**：时序分析依赖外部库，这个库叫什么？在哪里？

**参考答案**：叫 **Tatum**，位于 `libs/EXTERNAL/libtatum/`（见模块 4.3），VPR 的 `timing/` 子目录负责集成它。

---

### 4.3 外部子树 `libs/EXTERNAL` 规则

#### 4.3.1 概念说明

`libs/` 是「公共零件库」，所有工具共享其中的数据结构与解析器。但它内部有两类性质完全不同的代码：

1. **本仓库维护的库**：如 `libarchfpga`（架构 XML 解析）、`libvtrutil`（通用工具）。你可以直接改。
2. **外部子树库**：放在 `libs/EXTERNAL/` 下，是从上游仓库用 `git subtree` 拉进来的。**绝对不能在 VTR 树内直接改**。

这条规则极其重要：改了外部子树里的代码，会让 VTR 的副本与上游「分叉（diverge）」，以后再同步上游就会冲突。官方措辞很明确，见 [libs/README.md:1-5](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/README.md#L1-L5) —— 「EXTERNAL 库不应在 VTR 源码树中修改，否则会偏离其主线实现」。

#### 4.3.2 核心流程

外部子树的正确工作流是「上游优先」：

```text
   发现 bug / 想改功能
          │
          ▼
   代码在 libs/EXTERNAL/ 下吗？
          │
   ┌──────┴───────┐
   │ 是           │ 否
   ▼              ▼
 改上游仓库     直接在 VPR 树内改
   │
   ▼
 用 dev/external_subtrees.py 把上游改动同步进 VTR
```

- 官方点名的外部子树库有四个：`libargparse`、`libblifparse`、`libsdcparse`、`libtatum`（见 [doc/agents/codebase.md:64-66](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/codebase.md#L64-L66)）。
- 同步工具是 `dev/external_subtrees.py`（核对：该脚本确实存在于 `dev/` 目录）。
- 仓库根目录的 `abc/` 同样属于外部，规则一致：不要直接改。

> 注意：实际 `libs/EXTERNAL/` 下还有很多**第三方依赖**（如 `capnproto`、`libpugixml`、`libezgl`、`yaml-cpp`、`yosys`、`sockpp` 等），它们是构建所需的 vendored 依赖。无论是否「子树」，凡在 `EXTERNAL/` 下的都遵循「不在 VTR 树内直接改」的总原则。

#### 4.3.3 源码精读

共享库总览：[doc/agents/codebase.md:52-62](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/codebase.md#L52-L62) —— 列出本仓库维护的主要库：

| 库 | 用途 |
|----|------|
| `libarchfpga` | FPGA 架构 XML 解析器与数据结构（核心头文件 `arch_types.h`） |
| `libvtrutil` | 通用工具：日志、`vtr_vector`、`vtr_ndmatrix` 等数据结构 |
| `librrgraph` | 路由资源图（RRG）数据结构与序列化 |
| `libpugiutil` | 封装 pugixml 的 XML 解析助手 |
| `libvtrcapnproto` | RR 图与 router lookahead 的 Cap'n Proto 二进制序列化 |
| `liblog` | 日志基础设施 |
| `librtlnumber` | RTL 数值表示 |

> 实际 `libs/` 下还有 `libvqm/`（VQM 相关），不在官方表内，属于补充库。

外部子树规则原文：[doc/agents/codebase.md:64-66](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/codebase.md#L64-L66) —— 点名 `libargparse`、`libblifparse`、`libsdcparse`、`libtatum` 四个子树库，说明改动必须先在上游做，再用 `dev/external_subtrees.py` 同步，并强调根目录 `abc/` 同样外部不可直接改。

同一条规则的另一处表述：[libs/README.md:3-5](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/README.md#L3-L5) —— 明确「EXTERNAL 库不应在 VTR 源码树中修改，否则会偏离主线实现」。

#### 4.3.4 代码实践

**实践目标**：亲手核对 `libs/` 的两类库，并找到外部子树的同步工具。

**操作步骤**：

1. 列出本仓库维护的库：

   ```bash
   ls -d libs/*/
   ```

   应看到 `libarchfpga`、`libvtrutil`、`librrgraph`、`libpugiutil`、`libvtrcapnproto`、`liblog`、`librtlnumber`、`libvqm` 等目录。

2. 列出外部子树 / 第三方依赖：

   ```bash
   ls -d libs/EXTERNAL/*/
   ```

   注意它比官方点名的四个要多。

3. 确认同步工具存在：

   ```bash
   ls dev/external_subtrees.py
   ```

**需要观察的现象**：`libs/EXTERNAL/` 下的目录数量远多于官方文档点名的四个；`dev/external_subtrees.py` 确实存在。

**预期结果**：你能清楚地说出「哪些库可以直接改、哪些不能」，并知道改外部库的正确路径是「先改上游，再同步」。

> 本实践为只读操作，运行结果以本地实际为准。

#### 4.3.5 小练习与答案

**练习 1**：你在 `libs/EXTERNAL/libtatum/` 里发现一个时序分析的 bug，可以直接在 VTR 树里改掉吗？应该怎么做？

**参考答案**：不能直接改。正确做法是先在 libtatum 的**上游仓库**修复，然后用 `dev/external_subtrees.py` 把上游改动同步进 VTR，否则 VTR 副本会与上游分叉。

**练习 2**：`libarchfpga` 和 `libtatum` 都在 `libs/` 下，但维护方式不同。请说明区别。

**参考答案**：`libarchfpga` 是本仓库**自主维护**的库（架构 XML 解析），可直接改；`libtatum` 在 `libs/EXTERNAL/` 下，是**外部子树**，不可在 VTR 树内直接改。

**练习 3**：仓库根目录的 `abc/` 与 `libs/EXTERNAL/` 下的库在「可否直接修改」上有什么共同点？

**参考答案**：两者都属于外部代码，遵循同一条规则——不在 VTR 树内直接修改（见 [doc/agents/codebase.md:64-66](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/codebase.md#L64-L66)）。

---

## 5. 综合实践

**任务**：制作一张属于你自己的「VTR 目录职责速查表（cheatsheet）」，并标注「外部不可改」目录。这是本讲实践任务的核心产出，后续阅读源码时可以随时查阅。

**操作步骤**：

1. 在仓库根目录运行：

   ```bash
   ls -d */            # 顶层目录
   ls -d vpr/src/*/    # VPR 内部子目录
   ls -d libs/EXTERNAL/*/  # 外部依赖
   ```

2. 用 Markdown 画三张表：
   - **表 A：顶层目录速查表**——包含「目录 / 用途 / 所属流程段 / 是否外部」四列。至少覆盖 `vpr/`、`parmys/`、`abc/`、`ace2/`、`libs/`、`vtr_flow/`、`utils/`、`doc/`，并补上 `blifexplorer/`、`dev/`、`verilog_preprocessor/`、`cmake/`。
   - **表 B：VPR 内部子目录速查表**——把 12 个子目录对应到数据流 `AtomNetlist → Prepacker → Packer → ClusteredNetlist → Placer → Router` 的某个阶段，并写出每个阶段的入口头文件（如 `pack.h` / `place.h` / `route.h`）。
   - **表 C：外部不可改清单**——列出所有 `libs/EXTERNAL/` 下的目录，标注官方点名的四个子树库（`libargparse`、`libblifparse`、`libsdcparse`、`libtatum`）以及根目录的 `abc/`，并写下「想改这里该怎么办」的一句话（答：先改上游，再用 `dev/external_subtrees.py` 同步）。

3. **自查**：随机挑两个目录，不看答案，口头说出它的用途与维护方；如果说错了，回头查本讲对应的永久链接。

**预期结果**：你得到一份与实际仓库一致的速查表，能把任何一个目录快速归类。这份表会伴随你读完整个学习手册。

> 本实践全程只读，不修改任何源码。运行结果以本地实际为准。

## 6. 本讲小结

- VTR 根目录是一个**多工具工作区**：`vpr/` 是核心 CAD 工具，`parmys/`、`abc/`、`ace2/` 对应三段流程的不同阶段，`libs/` 是共享零件库，`vtr_flow/` 是流水线调度台，`doc/`、`dev/`、`utils/` 等是辅助。
- VPR 内部 `vpr/src/` 按 **CAD 阶段**切分：`base/` 是地基（核心数据结构 + 全局上下文 `g_vpr_ctx`），`pack/`、`place/`、`route/`、`timing/`、`analysis/` 沿数据流 `AtomNetlist → … → Router` 一字排开，每个阶段都有自己的 `xxx.h` 入口函数。
- `libs/` 分两类：本仓库维护的库（如 `libarchfpga`、`libvtrutil`）可直接改；`libs/EXTERNAL/` 下的外部子树与第三方依赖（含根目录 `abc/`）**不可直接改**，改动要先在上游做，再用 `dev/external_subtrees.py` 同步。
- 官方 `codebase.md` 是一张**精简概念图**，实际代码树更丰富（多出 `blifexplorer/`、`dev/`、`verilog_preprocessor/`、`cmake/` 等）——定位问题时文档与 `ls` 要结合。
- 遇到问题先按「三段流程」选顶层目录，再按「数据流阶段」选 VPR 子目录，最后判断是否落在不可改的外部子树里。

## 7. 下一步学习建议

有了这张目录地图，下一讲（**u1-l4 一键跑通 VTR 全流程**）会让你用 `vtr_flow/scripts/run_vtr_flow.py` 把 `parmys → abc → vpr` 这条流水线真正跑起来，亲眼看到各阶段产出的中间文件。

随后建议按以下顺序深入源码：

- 想理解「地基」：直接读 `vpr/src/base/`，从 `vpr_context.h` 与 `globals.h` 的 `g_vpr_ctx` 入手（对应 u3 单元）。
- 想理解「架构怎么被读进来」：读 `libs/libarchfpga/`（对应 u2 单元）。
- 想理解某个具体阶段：进入对应子目录的入口头文件（如 `vpr/src/pack/pack.h` 的 `try_pack()`、`vpr/src/place/place.h` 的 `try_place()`、`vpr/src/route/route.h` 的 `route()`），再顺藤摸瓜。

记住：**先用本讲的速查表定位目录，再打开具体源码**——这是高效阅读 VTR 这类大型项目的基本节奏。
