# 源码目录结构剖析

## 1. 本讲目标

本讲带你「看懂 HCCL 仓库长什么样」。读完之后，你应该能够：

- 说出仓库顶层每个目录（`src`、`include`、`experimental`、`test`、`docs`、`examples`、`build.sh`）各自的职责。
- 解释 `src/ops` 下「一个算子一个目录」的组织方式，并能在 `all_reduce` 算子目录里找到 `selector`、`executor`、`template` 三类文件。
- 说清 `src/ops/op_common` 的四大通用组件（`executor`、`selector`、`template`、`topo`）分别做什么，以及为什么 `topo` 是**全体算子共享**的，而不是每个算子各有一份。
- 区分 `src`（生产级代码）与 `experimental`（社区试验代码）的边界，理解这条边界背后的架构硬约束。

本讲不深入任何算法实现，只解决一个问题：**拿到 HCCL 源码后，每个文件该去哪里找**。这是后续所有源码阅读的基础地图。

## 2. 前置知识

本讲默认你已经读过 [u1-l1 HCCL 项目定位与 CANN 软件栈](./u1-l1-project-overview.md)，知道两件事实：

1. HCCL 由两个仓库组成：本仓 `cann/hccl` 负责集合通信**算子**（算子入口、入参校验、算法选择与执行编排）；独立仓 `cann/hcomm` 负责**通信域与拓扑管理**及底层搬数据原语。
2. 两仓通过 `dlsym` 动态加载 `libhcomm.so` 解耦，可独立编译、独立版本演进。

如果你还读过 [u1-l2 集合通信核心概念与算法](./u1-l2-collective-comm-concepts.md)，会更容易理解目录里反复出现的几个词：算子（operator，如 AllReduce）、算法（algorithm，如 Ring）、引擎（engine，如 AICPU）。不过即使没读，本讲也会在遇到时用一句话解释。

下面用到的几个术语先统一：

| 术语 | 一句话解释 |
|------|-----------|
| 算子（op） | 一个对外暴露的通信操作，如 `HcclAllReduce` |
| 算法（alg） | 完成一个算子的具体方法，如 Ring、Mesh |
| 引擎（engine） | 执行通信任务的硬件抽象，如 AICPU_TS、AIV、CCU |
| 组件（component） | 算子内部按职责拆出的模块，本讲主角是 `selector/executor/template/topo` |

## 3. 本讲源码地图

本讲只看「目录与文档」，不读算法实现。关键文件如下：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/README.md) | 项目首页，含一版「目录结构说明」 |
| [AGENTS.md](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/AGENTS.md) | AI Agent 治理主入口，含目录骨架与四条架构硬约束 |
| [docs/zh/architecture/architecture-brief.md](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md) | 架构简介，3.2 节给出「目标目录结构」 |
| [experimental/README.md](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/experimental/README.md) | 社区试验目录的规则说明 |

我们会对照三份文档（README、AGENTS.md、architecture-brief）的目录说明，再用真实目录逐一印证。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- 4.1 顶层目录全貌（README 视角）
- 4.2 `src` 分层与 AGENTS.md 的架构硬约束
- 4.3 `src/ops`：算子目录与 `op_common` 四大组件
- 4.4 `include` / `experimental` / `test`：对外头文件、社区试验与测试的边界

### 4.1 顶层目录全貌（README 视角）

#### 4.1.1 概念说明

任何一个大型 C++ 项目，第一步都是「摸清顶层目录」。README 在「目录结构说明」一节给出了一版骨架树，是认识 HCCL 最快的入口。这棵树把仓库分成「源码（`src`）」「对外头文件（`include`）」「测试（`test`）」「文档（`docs`）」「样例（`examples`）」「构建脚本（`build.sh`）」几大块，对应了软件工程里「写代码、定接口、写测试、写文档、给样例、能构建」的标准分工。

#### 4.1.2 核心流程

README 的目录树按「从外到内、从总到分」组织，阅读顺序建议：

1. 先看顶层 6 个目录 + 1 个脚本，建立总览。
2. 再钻进 `src`，看到它只有两个子目录：`common`（通用逻辑）和 `ops`（算子实现）。
3. 最后在 `ops` 下平铺列出每个算子目录（`all_reduce`、`all_gather`、…）和通用组件 `op_common`。

用文字流程表达就是：

```text
仓库根目录
 ├── src           ← 源码：common(通用) + ops(算子)
 ├── include       ← 对外头文件
 ├── test          ← ut(单元测试) + st(系统测试)
 ├── docs          ← 资料
 ├── examples      ← 样例
 └── build.sh      ← 一键编译
```

#### 4.1.3 源码精读

README 的「目录结构说明」从 [README.md:29](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/README.md#L29) 开始，整棵骨架树见 [README.md:33-63](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/README.md#L33-L63)。其中两处最关键：

- [README.md:34-36](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/README.md#L34-L36)：说明 `src` 分成 `common`（类型定义、日志模块等通用逻辑）和 `ops`（算子实现）。
- [README.md:45-49](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/README.md#L45-L49)：第一次点出 `op_common` 下有 `executor`（执行器）、`selector`（算法选择器）、`template`（算法模板）、`topo`（通信域拓扑信息获取和转换）四个子目录——这就是贯穿全本的「四大组件」。

注意一个细节：README 的目录树里**没有** `experimental` 顶层目录，但真实仓库里它是存在的（见 4.4）。这正说明：README 是面向最终用户的「概览」，AGENTS.md 和 architecture-brief 才是面向开发者的「完整骨架」。

#### 4.1.4 代码实践

- **实践目标**：建立顶层目录的肌肉记忆。
- **操作步骤**：在仓库根目录用 `ls -1` 列出顶层条目，再逐条对照 README 的骨架树。
- **需要观察的现象**：顶层应同时出现 `src`、`include`、`test`、`docs`、`examples`、`experimental`、`build.sh` 等条目。
- **预期结果**：你能不查文档，指出每个顶层条目属于「源码 / 头文件 / 测试 / 文档 / 样例 / 试验 / 构建」中的哪一类。
- **命令**（只读，不改任何文件）：

```bash
ls -1
```

> 说明：本讲所有实践都是「源码阅读 / 文件定位型」，不编译、不运行、不改动源码，因此不需要 NPU 环境，结果可即时验证。

#### 4.1.5 小练习与答案

**练习 1**：README 的目录树里，`test` 下面有哪两个子目录？分别代表什么？
**答案**：`ut`（单元测试，unit test）和 `st`（系统测试，system test）。对应 [README.md:57-59](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/README.md#L57-L59)。

**练习 2**：`src` 下只有哪两个子目录？
**答案**：`common`（通用逻辑）和 `ops`（算子实现）。对应 [README.md:35-36](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/README.md#L35-L36)。

### 4.2 `src` 分层与 AGENTS.md 的架构硬约束

#### 4.2.1 概念说明

README 告诉你「目录长什么样」，AGENTS.md 告诉你「为什么必须长成这样」。AGENTS.md 是 HCCL 仓的 AI Agent 治理主入口，它的第 2 节给出目录骨架，第 3 节给出**架构硬约束**——这些约束决定了目录为什么这样划分，也决定了你以后改代码时哪些事不能做。理解了这一层，你才不会在阅读源码时「看到文件却不懂它的边界」。

#### 4.2.2 核心流程

HCCL 的软件分三层，依赖严格自上而下：

```text
L1  HCCL 集合通信算子 (coll_comm_ops)   ← 本仓 cann/hccl
        │  依赖（dlsym 动态加载）
        ▼
L2  HCOMM 集合通信域管理 (HCCM)          ← 独立仓 cann/hcomm
        │  依赖
        ▼
L3  HCOMM 基础通信 (base_comm)           ← 独立仓 cann/hcomm
```

围绕这条依赖链，AGENTS.md 立了四条**不可违反**的硬约束，它们直接塑造了目录结构：

1. **分层依赖方向**：上层依赖下层，下层不能反向依赖上层。
2. **控制面/数据面分离**：资源管理、拓扑查询（控制面）与数据搬运、同步（数据面）接口独立演进。
3. **HCCL 与 HCOMM 解耦**：跨仓调用走 `src/common/hcomm_dlsym/`，不在编译期硬依赖。
4. **新算子落标准结构**：官方算子落 `src/ops/<op>/`，社区试验算子落 `experimental/ops/<op>/`，都按 `selector/executor/template` 组织。

约束 4 正是「为什么每个算子目录结构都长得一样」的根本原因——它是被架构规定出来的，不是随意的。

#### 4.2.3 源码精读

AGENTS.md 第 2 节「目录结构」见 [AGENTS.md:13-27](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/AGENTS.md#L13-L27)，其中骨架树 [AGENTS.md:15-25](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/AGENTS.md#L15-L25) 比 README 多列了两个关键信息：

- [AGENTS.md:19](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/AGENTS.md#L19)：点明 `src/common` 下含 `adapter_acl / alg_env_config / log / param_check / sal / hcomm_dlsym / op_graph / utils / hccl_mc2` 等横切模块——这些是后续进阶层（Unit 4、Unit 6）的主角。
- [AGENTS.md:20-21](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/AGENTS.md#L20-L21)：说明 `experimental` 与 `src` 结构一致但不保证兼容、不编入商用版本；`include` 暴露 `hccl.h` 与 `hccl_mc2.h`。

四条架构硬约束见 [AGENTS.md:43-50](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/AGENTS.md#L43-L50) 的约束表。其中约束 4「新算子落标准结构」的原文在 [AGENTS.md:50](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/AGENTS.md#L50)，它要求每个新算子都必须提供 `selector`（算法选择）与 `template`（引擎模板），这就是 4.3 节要展开的「算子目录标准结构」的来历。

> 这张约束表还有一个隐藏用途：AGENTS.md 自己声明「架构事实以 architecture-brief.md 为权威来源」（见 [AGENTS.md:31](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/AGENTS.md#L31)）。所以当 README、AGENTS.md、architecture-brief 三者说法有出入时，以 architecture-brief 为准。

#### 4.2.4 代码实践

- **实践目标**：确认 `src/common` 真的含有 AGENTS.md 列出的那些横切模块，并定位跨仓解耦的入口目录。
- **操作步骤**：
  1. 列出 `src/common` 下所有条目，对照 AGENTS.md §2 的清单。
  2. 特别确认 `hcomm_dlsym` 目录存在——这是「HCCL 调 HCOMM」的唯一合法通道。
- **需要观察的现象**：`src/common` 下应能看到 `hcomm_dlsym`、`log`、`param_check`、`sal`、`adapter_acl`、`alg_env_config`、`hccl_mc2` 等条目。
- **预期结果**：你能指出「跨仓调用」相关代码都集中在 `src/common/hcomm_dlsym/`，而不会散落到各算子目录。
- **命令**：

```bash
ls -1 src/common/
```

#### 4.2.5 小练习与答案

**练习 1**：四条架构硬约束中，哪一条直接规定了「每个算子目录都要有 selector 和 template」？
**答案**：第 4 条「新算子落标准结构」。对应 [AGENTS.md:50](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/AGENTS.md#L50)。

**练习 2**：为什么 HCCL 算子不能在编译期直接 `#include` HCOMM 的私有头文件？
**答案**：因为「HCCL 与 HCOMM 解耦」约束要求两仓独立编译、独立版本演进，跨仓调用必须走 `src/common/hcomm_dlsym/` 的符号表 + `dlsym` 动态加载。对应 [AGENTS.md:49](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/AGENTS.md#L49)。

### 4.3 `src/ops`：算子目录与 `op_common` 四大组件

#### 4.3.1 概念说明

`src/ops` 是 HCCL 的心脏。它采用两条极为规整的组织原则：

1. **一个算子一个目录**：每个通信算子（AllReduce、AllGather、Broadcast、Send、Recv……）都在 `src/ops/<算子名>/` 下有一个独立目录。
2. **算子目录结构高度一致**：每个算子目录内部都按「算子入口 + selector + executor + template」组织，组件代码则复用 `src/ops/op_common` 提供的通用基类与注册表。

`op_common` 是「四大通用组件」的统称，对应四个子目录：

| 子目录 | 角色 | 一句话职责 |
|--------|------|-----------|
| `selector` | 算法选择器 | 根据入参、拓扑、设备，**决定**用哪种算法（产出 algName） |
| `executor` | 算法执行器 | 拿到 algName 后，**编排**多级子通信域的执行步骤 |
| `template` | 算法模板 | 一段**具体**的数据搬运算法（如 Mesh 1D、NHR），按引擎分 aicpu/aiv/ccu |
| `topo` | 拓扑适配 | 获取并转换 rankGraph 拓扑信息，匹配出多级子通信域 |

这里有一个**最容易踩坑**的事实：`selector`、`executor`、`template` 是**每个算子各有一份**（在算子自己的目录里），而 `topo` 是**全体算子共享**的——它只存在于 `src/ops/op_common/topo/`，不在任何单个算子目录下。原因是：拓扑是「通信域」的属性，属于多个算子共用的基础设施，按「控制面/数据面分离」约束，它天然该是公共组件而非某个算子的私产。

#### 4.3.2 核心流程

一个算子从入口到执行，目录上的体现是：

```text
src/ops/<op>/<op>_op.cc            ← 算子入口（如 HcclAllReduce），做入参校验、引擎分发
        └── selector/              ← 该算子的算法选择器：产出 algName
        └── executor/              ← 该算子的执行器：按 algName 编排
        └── template/{aicpu,aiv,ccu}/  ← 该算子的算法模板：具体搬运算法
                ↑ 复用
src/ops/op_common/                 ← 四大组件的通用基类、注册表、共享实现
        ├── selector/   executor/   template/   topo/
```

为什么 `template` 下面还要再分 `aicpu`、`aiv`、`ccu`？因为同一套算法（比如 Mesh 1D AllReduce）可以跑在三种不同引擎上（AICPU_TS、AIV、CCU），三种引擎的下发方式完全不同（详见 Unit 5），所以模板按引擎分子目录。用集合关系表达：

\[
\text{模板} = \text{算法(如 Mesh1D)} \times \text{引擎(如 aicpu/aiv/ccu)}
\]

即在 `template/` 下，你看到的是「算法 × 引擎」的笛卡尔积中实际落地的那几格。

#### 4.3.3 源码精读

**（1）架构简介的「目标目录结构」**

architecture-brief 3.2 节是最权威的目录说明，见 [architecture-brief.md:200-228](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md#L200-L228)。其中 `op_common` 四大组件的原文在 [architecture-brief.md:215-219](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md#L215-L219)：`executor`（算法执行器）、`selector`（算法选择器）、`template`（算法模板）、`topo`（通信算子的 rankGraph 拓扑信息适配）。

> 对比 README 与 architecture-brief 对四大组件的措辞，能看出层次差异：README 用「执行器 / 算法选择器 / 算法模板 / 拓扑信息获取和转换」，偏用户视角；architecture-brief 用「算法执行器 / 算法选择器 / 算法模板 / rankGraph 拓扑信息适配」，偏开发者视角，明确点出 `topo` 适配的是 rankGraph。

**（2）真实算子目录：以 `all_reduce` 为例**

进入 `src/ops/all_reduce`，能看到算子入口与三类组件目录：

- 算子入口：[src/ops/all_reduce/all_reduce_op.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.h)，对外声明 `HcclAllReduce`，见 [all_reduce_op.h:26-28](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/all_reduce_op.h#L26-L28)。
- selector 示例：[src/ops/all_reduce/selector/all_reduce_auto_selector.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/selector/all_reduce_auto_selector.h)（`all_reduce` 专属的算法选择器）。
- executor 示例：[src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.h)（顺序编排的执行器之一）。
- template 示例：[src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.h)（AICPU 引擎上的 Mesh 1D 一次性模板）。

注意：`all_reduce` 目录下**没有** `topo/` 子目录——`topo` 是共享组件，要去 `op_common` 找。

**（3）共享组件目录：`src/ops/op_common`**

四大组件的通用基类与注册表都在这里：

- selector 通用层：[src/ops/op_common/selector/selector_registry.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/selector/selector_registry.h)、`execute_selector.h`、`auto_selector_base.h`。
- executor 通用层：[src/ops/op_common/executor/executor_v2_base.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/executor/executor_v2_base.h) 与 `registry/` 注册表目录。
- template 通用层：[src/ops/op_common/template/alg_v2_template_base.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/alg_v2_template_base.h) 与 `aicpu/`、`aiv/`、`ccu/`、`registry/` 等子目录。
- topo（共享）：[src/ops/op_common/topo/topo.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/topo/topo.h) 与一整套 `topo_match_*.h`（如 `topo_match_1d.h`、`topo_match_multilevel.h`、`topo_match_ubx.h`）。

四个组件的协作总入口在 [src/ops/op_common/op_common.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.h)，其中声明了把 algName 交给执行器的 `HcclExecOp`，见 [op_common.h:35-37](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/op_common.h#L35-L37)。这就是「selector 选算法 → executor 执行」的接缝点。四大组件的内部机制（注册表、优先级遍历、模板生命周期）是 Unit 3 的主题，本讲只要记住它们的目录位置即可。

#### 4.3.4 代码实践

- **实践目标**：亲手验证「selector/executor/template 是每算子一份，topo 是共享一份」。
- **操作步骤**：
  1. 看 `all_reduce` 算子目录有哪些子目录：

     ```bash
     find src/ops/all_reduce -maxdepth 1 -type d | sort
     ```

     预期看到 `selector`、`executor`、`template`、`op_graph`，**没有** `topo`。
  2. 确认 `topo` 在 `op_common` 下、且是全体算子共用：

     ```bash
     ls -1 src/ops/op_common/topo/ | head
     ```

  3. 各取一个真实文件，确认命名规律（入口 `_op`、选择器 `_selector`、执行器 `_executor`、模板 `_temp_`/`ins_temp_`）。
- **需要观察的现象**：`all_reduce` 有自己的 `selector/executor/template`，但没有 `topo`；`topo` 只在 `op_common/topo` 出现一次。
- **预期结果**：你能画出「算子私有组件（selector/executor/template）+ 共享组件（topo）」的归属图，并解释为什么 topo 是共享的（它属于通信域基础设施，受控制面/数据面分离约束）。
- **补充**：`all_reduce` 还有一个 `op_graph/` 子目录，那是图模式算子的 proto 注册（与单算子入口并列），将在 [u7-l2 图模式执行路径](./u7-l2-graph-mode.md) 讲解。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `all_reduce` 目录下没有 `topo` 子目录？
**答案**：因为 `topo` 适配的是通信域的 rankGraph 拓扑，属于多算子共享的基础设施（控制面），按「控制面/数据面分离」约束集中放在 `src/ops/op_common/topo/`，而不挂在任何单个算子目录下。

**练习 2**：`template` 下面为什么要再分 `aicpu`、`aiv`、`ccu`？
**答案**：因为同一算法（如 Mesh 1D AllReduce）可跑在 AICPU_TS、AIV、CCU 三种引擎上，三者的内核下发方式不同（Task 描述符 / Vector Core / Mission+URMA），所以模板按引擎分子目录。即 \(\text{模板} = \text{算法} \times \text{引擎}\)。

**练习 3**：`op_common` 的四个子目录里，哪一个「决定算法」、哪一个「编排执行」、哪一个「搬运数据」、哪一个「提供拓扑」？
**答案**：`selector` 决定算法、`executor` 编排执行、`template` 搬运数据、`topo` 提供拓扑。

### 4.4 `include` / `experimental` / `test`：对外头文件、社区试验与测试的边界

#### 4.4.1 概念说明

`src` 之外还有三个边界目录需要分清：

- **`include`**：对外头文件。这是 HCCL 对「外部」（AI 框架、自定义算子开发者）承诺的稳定 API 面，变更需向后兼容。
- **`experimental`**：社区试验代码。结构与 `src` 一致，但不保证兼容、不编入商用版本，是「快速原型」的试验场。
- **`test`**：测试代码，分 `ut`（单元测试）与 `st`（系统测试）。

这三者的边界不是随便画的，而是架构约束的直接产物：`include` 是「契约」，`src` 是「兑现契约的生产代码」，`experimental` 是「不保证兑现的试验」，`test` 是「验证兑现」。

#### 4.4.2 核心流程

把它们放到软件分层里看：

```text
include/hccl.h        ── L1 对外算子 API（面向 AI 框架）
include/hccl_mc2.h    ── MC2 自定义算子框架（面向算子开发者）
        │ 实现于
        ▼
src/ops/...           ── 生产级算子实现（编入商用版本）
experimental/ops/...  ── 试验性算子（结构同 src，不编入商用版本）
        │ 验证于
        ▼
test/ut  test/st      ── 单元测试 / 系统测试
```

`src` 与 `experimental` 的对照是本模块的重点：

| 维度 | `src/` | `experimental/` |
|------|--------|-----------------|
| 目标 | 生产级代码 | 快速原型验证 |
| 质量 | 生产级 | 原型级 |
| 稳定性 | 承诺 API 稳定 | 不保证 |
| 是否编入商用版本 | 是 | 否 |

#### 4.4.3 源码精读

**（1）`include`：两个对外头文件**

[AGENTS.md:21](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/AGENTS.md#L21) 点明 `include` 暴露 `hccl.h`（算子 API）与 `hccl_mc2.h`（MC2 自定义算子框架）。真实目录只有这两个文件：[include/hccl.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl.h)、[include/hccl_mc2.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/include/hccl_mc2.h)。architecture-brief 的对外 API 分层见 [architecture-brief.md:259-272](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md#L259-L272)。

**（2）`experimental`：试验目录的规则**

`experimental/` 的规则见 [experimental/README.md](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/experimental/README.md)，它与 `src` 的对照表说明了边界。architecture-brief 也在目录树里标注：`experimental` 内部结构与 `src` 保持一致，不保证兼容、不编入商用版本，见 [architecture-brief.md:227](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md#L227)。

真实结构印证了「与 src 一致」：`experimental/ops/` 下有 `op_common`（对应 `src/ops/op_common`）和 `reduce_scatter`（一个试验性算子）。`reduce_scatter` 内部按 `birs/`（一种试验算法）组织，含 `reduce_scatter_birs_selector.cc`、`reduce_scatter_birs_executor.cc`、`template/reduce_scatter_birs.cc`——与 `src` 下算子的 `selector/executor/template` 结构完全对应。这正是 AGENTS.md 约束 4「结构与 src 一致」的实例，详见 [experimental/ops/reduce_scatter](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/experimental/ops/reduce_scatter)。

**（3）`test`：ut 与 st**

README 标注 `test` 下分 `ut`（单元测试）与 `st`（系统测试），见 [README.md:57-59](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/README.md#L57-L59)。真实目录印证：`test/ut` 与 `test/st` 各自独立，`test/st` 下有 `algorithm`（系统测试按算子组织）。两者的运行方式（`build.sh -u` 跑 UT、`build.sh -s` 跑 ST）见 [AGENTS.md:62-71](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/AGENTS.md#L62-L71)，测试体系的深入讲解在 [u7-l4 测试体系——UT 与 ST](./u7-l4-testing.md)。

#### 4.4.4 代码实践

- **实践目标**：验证 `experimental` 的结构与 `src` 同构，并确认 `include` 只有稳定对外头文件。
- **操作步骤**：
  1. 列出对外头文件：

     ```bash
     ls -1 include/
     ```

     预期只有 `hccl.h` 和 `hccl_mc2.h`。
  2. 列出 `experimental/ops` 与某个试验算子的结构：

     ```bash
     ls -1 experimental/ops/
     find experimental/ops/reduce_scatter -name "*.cc" | sort
     ```

  3. 对照 `src/ops/all_reduce` 的文件命名，确认 `experimental` 的 `_selector/_executor/template` 后缀与 `src` 一致。
- **需要观察的现象**：`include` 干净到只有两个头文件；`experimental/ops/reduce_scatter/birs/` 里的文件命名规律与 `src` 算子一致。
- **预期结果**：你能说清「为什么改 `include` 要比改 `src` 谨慎得多」——前者是公开契约，后者是内部实现；以及「为什么试验算子放 `experimental` 而非 `src`」——它不保证兼容、不编入商用版本。
- **注**：本实践只读，不编译、不运行。

#### 4.4.5 小练习与答案

**练习 1**：`include` 下有几个对外头文件？分别面向谁？
**答案**：两个。`hccl.h` 面向 AI 框架适配层（标准算子 API），`hccl_mc2.h` 面向自定义通信算子开发者（MC2 框架）。对应 [AGENTS.md:21](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/AGENTS.md#L21) 与 [architecture-brief.md:263-269](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/docs/zh/architecture/architecture-brief.md#L263-L269)。

**练习 2**：如果你写了一个还不确定要不要长期维护的新算法，应该放进 `src/ops/` 还是 `experimental/ops/`？为什么？
**答案**：放进 `experimental/ops/`。因为 `experimental` 不保证 API 稳定、不编入商用版本，适合快速原型验证；待方案成熟、通过 RFC/SIG 评审后再迁入 `src/ops/` 的标准结构。

## 5. 综合实践

本讲的综合实践是一个**端到端的文件定位任务**，把四个模块串起来。

**任务**：在仓库中找到 `all_reduce` 算子的 `selector`、`executor`、`template` 三类文件各一个，并说明 `topo` 类文件为什么不在 `all_reduce` 目录下、应该去哪里找；最后列出 `src/ops/op_common` 下的四个子目录及各自职责。

**操作步骤**：

1. **定位 `all_reduce` 的三类组件文件**（各取一个真实文件，记下相对路径）：

   ```bash
   echo "== selector ==" ; ls -1 src/ops/all_reduce/selector/*.h
   echo "== executor ==" ; ls -1 src/ops/all_reduce/executor/*.h | head -1
   echo "== template ==" ; ls -1 src/ops/all_reduce/template/aicpu/*.h | head -1
   ```

   预期参考答案（以实际文件名为准）：
   - selector：`src/ops/all_reduce/selector/all_reduce_auto_selector.h`
   - executor：`src/ops/all_reduce/executor/ins_v2_all_reduce_sequence_executor.h`
   - template：`src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.h`

2. **解释 topo 的归属**：确认 `all_reduce` 下没有 `topo`，`topo` 在共享目录：

   ```bash
   find src/ops/all_reduce -maxdepth 1 -name topo    # 应无输出
   ls -1 src/ops/op_common/topo/ | head
   ```

   说明：`topo` 适配的是通信域的 rankGraph 拓扑，属控制面基础设施，受「控制面/数据面分离」约束集中放在 `src/ops/op_common/topo/`，供所有算子共用。

3. **列出 `op_common` 四大子目录及职责**：

   ```bash
   ls -1d src/ops/op_common/{executor,selector,template,topo}
   ```

   对照本讲 4.3.1 的表格填写职责：selector 决定算法、executor 编排执行、template 搬运数据、topo 提供拓扑。

**预期产出**：一张包含「类别 → 文件相对路径 → 所属（私有/共享）」的小表，外加一段对「topo 为何共享」的解释。这张表就是你后续阅读 HCCL 任何算子源码时的索引模板——遇到任何算子，都可以照着「入口 `_op` → selector → executor → template（+ 共享 topo）」的顺序找下去。

> 待本地验证：文件名可能随版本微调，若上述 `ls` 输出与本讲示例不完全一致，以仓库实际文件为准，目录结构（selector/executor/template + 共享 topo）是稳定的。

## 6. 本讲小结

- HCCL 顶层分为 `src`（源码）、`include`（对外头文件）、`experimental`（社区试验）、`test`（测试）、`docs`（文档）、`examples`（样例）和 `build.sh`（构建脚本）。
- `src` 只分 `common`（通用逻辑，含 `hcomm_dlsym` 等横切模块）和 `ops`（算子实现）；跨仓调用统一走 `src/common/hcomm_dlsym/`。
- `src/ops` 遵循「一个算子一个目录」，每个算子目录按「入口 `_op` + selector + executor + template」组织，这是架构硬约束（约束 4）规定出来的标准结构。
- `op_common` 提供四大通用组件：selector（决定算法）、executor（编排执行）、template（搬运数据）、topo（提供拓扑）。
- 关键区别：selector/executor/template 是每个算子私有，topo 是全体算子共享（在 `src/ops/op_common/topo/`），因为拓扑属于通信域的控制面基础设施。
- `include` 只有 `hccl.h` 与 `hccl_mc2.h` 两个稳定对外头文件；`experimental` 与 `src` 同构但不保证兼容、不编入商用版本。

## 7. 下一步学习建议

掌握了目录地图后，建议按以下顺序继续：

1. **先动手运行一次**：读 [u1-l4 构建、安装与运行](./u1-l4-build-and-run.md)，用 `build.sh` 把 HCCL 编出来，对「目录 → 产物」建立感性认识。
2. **跑通第一个样例**：读 [u1-l5 第一个 HCCL 程序](./u1-l5-first-hccl-program.md)，对照 `examples/02_collectives/01_allreduce`，把本讲的 `all_reduce_op.cc` 入口与一个能运行的样例对应起来。
3. **进入主链路**：之后进入 Unit 2（[u2-l2 单算子入口与兼容分发](./u2-l2-op-entry-dispatch.md)），从 `all_reduce_op.cc` 的 `HcclAllReduce` 入口逐行往下读，这时你会发现本讲的目录地图就是那张「在哪个文件里找哪段逻辑」的索引。
4. **延伸阅读**：想立刻深入四大组件机制，可直接跳到 Unit 3（[u3-l1 op_common 架构与三大注册表总览](./u3-l1-opcommon-overview.md)），但建议先把 Unit 1、Unit 2 走完再读，理解会更顺。
