# 源码目录结构与模块划分

## 1. 本讲目标

上一篇（u1-l1）我们建立了 GE 的全局认知：知道它是一个图编译器 + 执行器，知道了「输入 → AscendIR → 编译 → OM → 执行」这条链路，也知道了在线 / 离线两种场景。但那张图是「逻辑视图」，真正打开代码仓库时，你面对的是几十个顶层目录和成千上万个文件。

本讲的目标是把「逻辑链路」落到了「物理目录」上。读完本讲你应该能够：

- 看着仓库根目录，说出 `api`、`base`、`compiler`、`runtime`、`parser`、`graph_metadef`、`dflow`、`inc`、`examples`、`tests` 等顶层目录各自负责什么。
- 理解 GE 仓与「算子仓」是解耦的：GE 维护图的**基础结构**和**注册接口**，而具体算子定义放在外部独立仓。
- 在源码中快速定位「解析 / 编译 / 执行」三大主模块的入口文件，并各举出一个代表性源文件。

## 2. 前置知识

本讲承接 u1-l1 已经建立的概念，下面几个术语会直接用到，这里只做最短的回顾，不再展开：

- **AscendIR**：GE 的核心中间表示（IR），所有前端输入最终都转换成它。本讲会看到它的代码定义放在哪个目录。
- **OM**：编译产出的离线模型文件，由「编译」模块生成、由「执行」模块加载。
- **Host / Device**：Host 指主机 CPU，Device 指昇腾芯片。编译大多在 Host 完成，执行在 Device 上。
- **算子定义外置**：u1-l1 提到「算子的定义并不位于 GE 仓」。本讲会解释这个「解耦」在目录层面是怎么体现的。
- **Anchor（锚点）**：GE 用锚点而不是独立的「边」对象来表达节点连线，相关的头文件就在图基础结构目录里。

如果你对这些词还完全陌生，建议先回到 u1-l1 把架构总览读一遍再来。

## 3. 本讲源码地图

本讲主要对照仓库自身的说明文档来讲解目录划分，涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| `AGENTS.md` | 仓内的 agent 工作指南，开头就有一张「关键目录」速查表，是本讲最直接的目录索引。 |
| `docs/zh/design/architecture.md` | GE 架构说明文档，末尾的「项目结构」小节给出了顶层目录的官方注释。 |

为了让你「看得到真代码」，本讲还会顺带引用几个**代表性源文件**（用于演示各大模块的入口长什么样）：

| 代表性源文件 | 所属模块 |
|------|------|
| `inc/graph_metadef/graph/compute_graph.h` | 图基础结构（AscendIR 的图对象） |
| `inc/graph_metadef/register/op_registry.h` | 算子注册接口 |
| `parser/parser/onnx/onnx_parser.cc` | 解析模块（ONNX 入口） |
| `compiler/graph/manager/graph_manager.h` | 编译模块（图管理器） |
| `runtime/v1/graph/load/model_manager/davinci_model.h` | 执行模块（模型实例） |

> 说明：本讲引用的都是**实际存在**的文件，行号基于当前 HEAD。下面凡是 `[路径:L起-L止]` 形式的链接，点击即可跳转到对应代码行。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先看顶层目录职责，再理解 GE 与算子仓的解耦，最后学会在源码里导航三大主模块。

### 4.1 顶层目录职责

#### 4.1.1 概念说明

GE 仓库的目录不是随便切的，它基本是**沿着数据流动的链路**来组织的。回忆 u1-l1 的链路：

```
前端输入  →  AscendIR  →  GE Compiler 编译  →  OM  →  GE Executor 执行
            （图结构）     （图优化/调度/内存）         （加载/下发到设备）
```

把这条链路直接映射成目录，就得到了：

- 输入怎么进来 → `parser/`（把外部模型格式解析成 AscendIR）
- AscendIR 的结构定义在哪 → `graph_metadef/`（图元数据定义）
- 编译在哪做 → `compiler/`（图编译器）
- 执行在哪做 → `runtime/`（图执行器）
- 对外暴露的接口在哪 → `api/` 和 `inc/`（公共 API 与头文件）
- 公共支撑（工具、格式转换、主机 CPU 引擎）→ `base/`
- 异步流水框架 → `dflow/`
- 学习与测试入口 → `examples/`、`tests/`

一句话：**目录 = 链路的一个切片**。理解了链路，目录就记住了。

#### 4.1.2 核心流程

下面这张表对照了链路阶段与目录，你可以把它当成一张「源码地图」来记：

| 链路阶段 | 对应顶层目录 | 一句话职责 |
|----------|--------------|-----------|
| 外部模型 → AscendIR | `parser/` | 解析 ONNX / PB / Caffe / MindSpore 模型为 AscendIR |
| AscendIR 结构定义 | `graph_metadef/` | 图、节点、张量、锚点、算子注册等数据结构 |
| 图编译 | `compiler/` | 图优化、融合、引擎分区、流/内存规划、算子编译 |
| 图执行 | `runtime/` | 模型加载、任务下发、在设备上执行 |
| 对外 API | `api/` | ACL、ATC、Session、Python 绑定等对外接口实现 |
| 公共头文件 | `inc/` | 对外/对内头文件（`inc/external` 是稳定对外接口） |
| 公共基础 | `base/` | 工具方法、格式转换、主机 CPU 引擎等基础组件 |
| 异步流水 | `dflow/` | 异构模型串接、数据驱动流水（LLM 数据分发、UDF） |
| 样例 | `examples/` | 端到端使用样例（ResNet50、LLM、融合 Pass 等） |
| 测试 | `tests/` | UT（单元测试）/ ST（系统测试）/ 基准测试 |

用伪代码表达「一个离线模型在仓库目录间的旅行」：

```text
onnx 文件
  └─ 进入 parser/  被解析成 AscendIR（compute_graph.h 定义的图对象）
       └─ 进入 compiler/  做优化、分区、流/内存规划，产出 model
            └─ 序列化为 OM 文件（api/atc 驱动这一步）
                 └─ 进入 runtime/  加载 OM、下发到昇腾设备执行
```

注意：`graph_metadef/` 不在主链路上「流动」，它是**被所有人依赖的地基**——`parser`、`compiler`、`runtime` 都要用它定义的图对象。

#### 4.1.3 源码精读

GE 自己在两处给出了顶层目录的说明，本讲直接对照官方注释来讲。

**第一处**是 `AGENTS.md` 开头的「关键目录」速查表，它用一句话概括了每个目录：

[AGENTS.md:L9-L21](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/AGENTS.md#L9-L21) —— GE 官方的「关键目录」表，列出了 `api`、`base`、`compiler`、`runtime`、`dflow`、`parser`、`graph_metadef`、`tests`、`examples` 各自的用途。

**第二处**是架构文档末尾的「项目结构」小节，它用一棵目录树给出了带注释的顶层结构：

[docs/zh/design/architecture.md:L201-L217](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/design/architecture.md#L201-L217) —— 项目结构目录树，每个目录后面都带一句中文注释，例如 `parser` 标注「当前支持 tensorflow/onnx/caffe/mindspore」。

这两处合起来，就是仓库目录的「官方说明书」。值得注意的一个细节：`dflow/` 在架构文档里被注明「未来将与 GE 解耦，独立仓运作」——也就是说它现在虽然在 GE 仓内，但定位上是一个相对独立、将来会拆出去的子系统。这点在导航时心里有数即可。

#### 4.1.4 代码实践

**实践目标**：亲手在仓库里核对一遍顶层目录，验证文档说的是否和磁盘一致。

**操作步骤**：

1. 在仓库根目录执行 `ls`，查看所有顶层条目。
2. 对照上面 [AGENTS.md:L9-L21](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/AGENTS.md#L9-L21) 的目录表，逐个确认它们真实存在。
3. 找一个文档里提到、但你还没见过的目录（例如 `base/`），`ls base/` 看看它下面有什么。

**需要观察的现象**：

- 根目录除了源码目录外，还有 `CMakeLists.txt`、`build.sh`、`README.md`、`CONTRIBUTING.md`、`version.cmake` 等工程文件——这些不是模块，而是构建与项目治理文件。
- `base/` 下会出现 `common`、`formats`、`graph`、`host_cpu_engine` 等子目录，印证它「基础工具 + 主机 CPU 引擎」的定位。

**预期结果**：文档表格里的每个目录都能在磁盘上找到，且子目录与「一句话职责」大体吻合。如果遇到对不上的情况（例如文档写的是简称、磁盘是全称），把它记下来——这正是你需要建立「文档 ↔ 源码」对应关系的地方。

#### 4.1.5 小练习与答案

**练习 1**：架构文档「项目结构」里出现的 `cmake` 和 `scripts` 两个目录，分别负责什么？

> **参考答案**：`cmake/` 是 cmake 公共脚本目录（构建系统的复用片段）；`scripts/` 是打包脚本目录。它们属于工程支撑，不在「数据流链路」上。

**练习 2**：下面哪个目录**不属于**主数据流链路（解析→编译→执行），而是被所有人依赖的地基？
- A. `parser/`　B. `compiler/`　C. `graph_metadef/`　D. `runtime/`

> **参考答案**：C。`graph_metadef/` 定义图的基础数据结构，是被 `parser`/`compiler`/`runtime` 共同依赖的地基，本身不处于「流动」链路上。

### 4.2 GE 与算子仓的解耦

#### 4.2.1 概念说明

u1-l1 已经埋下一个关键点：**算子的具体定义不在 GE 仓**。这一节我们把它讲透。

GE 作为一个「图编译器 + 执行器」，它的职责是：

- 做图级优化（融合、常量折叠……）；
- 做调度、内存规划；
- 生成并序列化可执行模型（OM）。

它**不负责定义每个算子的语义和实现**（比如 `Add` 到底怎么算、`MatMul` 的 shape 怎么推导）。这些算子定义放在独立的「算子仓」（如 `ops-math`、`ops-transformer` 等，都是 GE 仓**之外**的独立仓）。

这种解耦的好处是：

- 算子可以独立于 GE 升级发布；
- 自定义算子和内置算子走同一套接入机制，地位平等；
- 算子定义同时服务于「入图（Graph 编译）」和「aclnn（原生 API 调用）」两种场景，保证语义和精度一致。

那么 GE 仓里有什么？GE 仓里只有**两样东西**与算子相关：

1. 图的**基础结构**（Graph / Node / Tensor / Anchor 等），由 `graph_metadef/` 维护；
2. **注册接口**（让外部算子仓把算子「登记」进来的机制），由 `graph_metadef/register` 提供。

#### 4.2.2 核心流程

算子「入图」的协作流程可以简化为：

```text
[外部算子仓]                  [GE 仓 graph_metadef]
  算子定义                       图基础结构
  (类型/输入/输出/属性)  ──登记──▶  register 注册接口
  shape 推导/Kernel              ▲
                                │ 编译时 GE 通过注册表查算子定义、做合法性检查
                                │ 编译时 GE 调用算子实现（shape 推导、编译）
```

关键点：

- **登记动作**由外部算子仓在加载时完成（通过 `register` 提供的注册宏 / 接口）。
- **查询动作**由 GE 在编译时完成——GE 拿到一个 AscendIR 节点，根据它的算子类型去注册表里找定义、做检查和推导。
- GE 自身不持有算子语义，只持有「如何登记、如何查询」这套机制。

#### 4.2.3 源码精读

架构文档专门有一节解释这套解耦，是理解本模块的一手资料：

[docs/zh/design/architecture.md:L96-L128](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/design/architecture.md#L96-L128) ——「算子定义体系」小节，说明了 AscendIR 基础结构由 GE 仓维护、而算子定义由独立算子仓维护的设计，以及二者如何协作。

对应到代码，**地基**（图对象）的定义在这里：

[inc/graph_metadef/graph/compute_graph.h:L46](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/compute_graph.h#L46) —— `class ComputeGraph` 的声明，这是 AscendIR 里「一张图」的核心类，承载节点、锚点、输入输出描述。它由 GE 仓维护，但**不**包含任何具体算子的实现。

**注册接口**在这里：

[inc/graph_metadef/register/op_registry.h:L47](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/register/op_registry.h#L47) —— `class OpRegistry` 的声明，它是算子注册表，外部算子仓通过它把算子类型、输入输出、属性等信息登记进来，编译时 GE 又通过它查询算子定义。

> 提示：注意这两个文件都在 `inc/graph_metadef/` 下，一个在 `graph/` 子目录（结构），一个在 `register/` 子目录（注册）。这条「结构 + 注册」的边界，就是 GE 仓为算子提供的全部「容器」，真正的算子「内容」在仓外。

#### 4.2.4 代码实践

**实践目标**：在源码里亲眼看到「GE 只提供注册机制、不提供算子实现」这条边界。

**操作步骤**：

1. `ls graph_metadef/register/`，浏览注册相关文件（你会看到大量 `*_registry.cc`，它们都是「登记各类信息」的机制文件）。
2. 在该目录下尝试用搜索找具体算子（例如 `Add`、`MatMul`）的**实现**代码——你会发现自己很难找到完整的算子 kernel 实现。
3. 对照 [inc/graph_metadef/register/op_registry.h:L47](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/register/op_registry.h#L47) 的 `OpRegistry`，理解它是「表」而不是「内容」。

**需要观察的现象**：

- `graph_metadef/register/` 里多是注册表、注册函数、上下文（context）类的文件，**几乎没有**像 `Add`、`MatMul` 这类具体算子的计算实现。
- 这印证了「算子定义外置」：仓库里只有「登记入口」，没有「算子内容」。

**预期结果**：你能清楚地指出——如果要找一个具体算子的语义实现，应该去外部算子仓，而不是在 GE 仓里翻；GE 仓里能找到的只有注册接口和图结构。

#### 4.2.5 小练习与答案

**练习 1**：为什么 GE 要把算子定义放在仓外？说出两条理由。

> **参考答案**：① 保持 GE「图编译器」职责清晰，不与算子实现耦合；② 算子可独立于 GE 升级发布，同时让「入图」和「aclnn」两种场景共用同一份算子定义，保证语义/精度一致。

**练习 2**：判断对错——「`OpRegistry` 里存放着 `Add` 算子的具体计算代码」。

> **参考答案**：错。`OpRegistry` 是注册表/查询入口，提供「登记」和「查询」算子定义的机制；具体算子的计算实现（kernel、shape 推导）在外部算子仓。编译时 GE 通过注册表去「找」算子定义，而非自己持有算子实现。

### 4.3 核心模块导航

#### 4.3.1 概念说明

仓库很大，初学者最需要的能力是「看到一个需求，能立刻知道该去哪个目录读代码」。本模块给你一套**三大主模块**的导航法：

- **解析（parser）**：负责「入口翻译」，把外部模型格式变成 AscendIR。
- **编译（compiler）**：负责「中间加工」，对 AscendIR 做优化、分区、流/内存规划、算子编译。
- **执行（runtime）**：负责「出口执行」，加载模型、下发到设备、跑出结果。

这三个模块加上公共地基 `graph_metadef`，构成了 GE 代码阅读的「四大名山」。后面整本手册的进阶篇，基本都是在逐个深入这四座山。

#### 4.3.2 核心流程

三大模块加上地基的协作顺序如下：

```text
        外部模型文件
            │
            ▼
      ┌───────────┐
      │  parser   │  解析：ONNX/PB/... → AscendIR(ComputeGraph)
      └─────┬─────┘
            │  （图对象来自 graph_metadef 地基）
            ▼
      ┌───────────┐
      │ compiler  │  编译：优化/分区/流/内存/算子编译 → model
      └─────┬─────┘
            │  （期间通过 register 查算子定义）
            ▼
         OM 产物
            │
            ▼
      ┌───────────┐
      │  runtime  │  执行：加载 OM、下发到设备、执行
      └───────────┘
```

要点：

- 三个模块是**串行**的上下游关系，这正是 u1-l1 链路在源码里的真实分工。
- `graph_metadef`（地基）被三者共享，所以它最常被你读到。
- `parser` 和 `compiler` 主要在 Host 运行；`runtime` 负责把结果搬到 Device 并执行。

#### 4.3.3 源码精读

下面给出每个模块的**代表性入口文件**，作为你打开源码的第一站。

**地基：图对象 `ComputeGraph`**

[inc/graph_metadef/graph/compute_graph.h:L46](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/compute_graph.h#L46) —— AscendIR「一张图」的核心类，parser 产出它、compiler 加工它、runtime 加载它的编译产物。后两篇（u2 系列）会专门讲它和它的子对象。

**解析模块：ONNX 主解析入口**

[parser/parser/onnx/onnx_parser.cc:L1126](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L1126) —— `OnnxModelParser::Parse` 的实现起点，这是 ONNX 文件被读入并开始转换成 AscendIR 的关键函数。`parser/parser/onnx/` 目录里还有 `onnx_data_parser.cc`、`onnx_constant_parser.cc` 等分别处理数据节点和常量权重。

**编译模块：图管理器 `GraphManager`**

[compiler/graph/manager/graph_manager.h:L43](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/compiler/graph/manager/graph_manager.h#L43) —— `class GraphManager` 的声明，它是编译流程的「总调度」，组织后续的预处理、优化、分区、构建等多个阶段（u4 系列会展开）。`compiler/graph/` 下的 `preprocess`、`optimize`、`partition`、`build` 子目录就对应这些阶段。

**执行模块：模型实例 `DavinciModel`**

[runtime/v1/graph/load/model_manager/davinci_model.h:L145](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/runtime/v1/graph/load/model_manager/davinci_model.h#L145) —— `class DavinciModel` 的声明，它是 v1 执行器里「一个加载好的模型」的表示，持有算子二进制、权重、任务序列等设备侧资源（u6 系列会展开）。注意 `runtime/` 下有 `v1`（静态 shape 执行器）和 `v2`（动态 shape 执行器 RT2.0）两套并存的执行架构。

> 小贴士：`compiler/engines/` 目录下你还能看到 `nn_engine`、`hccl_engine`、`cpu_engine`、`local_engine` 等多个「引擎」——这对应 u1-l1 提到的「不同算子可分配到不同执行引擎」，这部分会在编译的「引擎分区」阶段（u4-l4）细讲，现在只要知道引擎实现在这里即可。

#### 4.3.4 代码实践

**实践目标**：亲手给三大主模块（外加地基）各写一句职责，并各举一个代表性源文件——这是本讲的核心练习。

**操作步骤**：

1. 分别进入 `compiler/`、`runtime/`、`parser/`、`graph_metadef/` 四个目录，用 `ls` 浏览它们的子目录结构。
2. 在每个目录里挑**一个**最能代表该模块职责的源文件（可以参考本讲 4.3.3 给出的代表性文件）。
3. 用一句话写下每个目录「它负责什么」。
4. 把结果整理成一张四行的小表：`目录 | 一句话职责 | 代表性源文件`。

**需要观察的现象**：

- `compiler/graph/` 下能看到 `preprocess / optimize / partition / build` 等子目录——这暗示编译内部是多阶段的（为 u4 埋伏笔）。
- `runtime/` 下能看到 `v1` 与 `v2` 并存——这印证了「两套执行架构并存」（为 u6 埋伏笔）。
- `parser/parser/` 下能看到 `onnx / tensorflow / caffe` 等子目录——这对应「多种前端格式」。
- `graph_metadef/` 下 `graph`（结构）与 `register`（注册）两个子目录清晰分开——这正是 4.2 节讲的「结构 + 注册」边界。

**预期结果**：你能写出类似下面这样的导航表（参考答案）：

| 目录 | 一句话职责 | 代表性源文件 |
|------|-----------|-------------|
| `compiler/` | 把 AscendIR 编译成可在设备执行的模型（优化/分区/流/内存/算子编译） | `compiler/graph/manager/graph_manager.h` |
| `runtime/` | 加载模型并下发到昇腾设备执行 | `runtime/v1/graph/load/model_manager/davinci_model.h` |
| `parser/` | 把外部模型格式（ONNX/PB/...）解析成 AscendIR | `parser/parser/onnx/onnx_parser.cc` |
| `graph_metadef/` | 定义图的基础结构与算子注册接口（全栈地基） | `inc/graph_metadef/graph/compute_graph.h` |

> 说明：本表是「源码阅读型实践」的参考结论。如果你本机已按 u1-l3 的方式完成构建，也可以进一步用 `grep`/`ls` 验证这些文件确实存在于当前 HEAD；若尚未构建环境，本练习以「阅读 + 整理导航表」为主，不需要运行命令。

#### 4.3.5 小练习与答案

**练习 1**：如果你想了解「ONNX 模型是怎么变成 AscendIR 的」，应该优先打开哪个目录的哪个文件？

> **参考答案**：`parser/parser/onnx/` 目录，优先看 `onnx_parser.cc`（主解析入口，如 [parser/parser/onnx/onnx_parser.cc:L1126](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/parser/parser/onnx/onnx_parser.cc#L1126) 的 `OnnxModelParser::Parse`）。

**练习 2**：`runtime/v1` 和 `runtime/v2` 为什么会同时存在？

> **参考答案**：因为 GE 同时维护两套执行架构——v1 是静态 shape 执行器（模型下沉、硬件调度），v2 是动态 shape 执行器（RT2.0，基于 Lowering）。二者适用场景不同，所以并存。详细对比见 u6 系列。

**练习 3**：`graph_metadef/` 为什么几乎在所有模块的依赖链上？

> **参考答案**：因为它定义了 AscendIR 的基础数据结构（Graph/Node/Tensor/Anchor）和算子注册接口。parser 产出这些对象、compiler 加工这些对象、runtime 加载它们的编译产物，所以它是被全局共享的「地基」。

## 5. 综合实践

**任务：画一张「目录 → 链路阶段」对照导航图，并标注一处让你意外的发现。**

把本讲学到的内容串起来：

1. **画目录树骨架**：在一张纸上（或文本里）画出 GE 仓库的顶层目录树，只列模块目录（`api/base/compiler/dflow/graph_metadef/inc/parser/runtime/examples/tests`），不必列工程文件。
2. **标注链路阶段**：在每个模块目录旁，标注它在「解析 → AscendIR 结构 → 编译 → 执行」链路上对应的阶段，以及它属于「主链路」还是「地基 / 支撑」。
3. **补代表性文件**：给三大主模块（parser/compiler/runtime）和地基（graph_metadef）各补一个代表性源文件路径。
4. **记录一个意外**：浏览过程中，挑一处「和你的直觉不一样」的地方写下来。例如：
   - 算子实现不在 GE 仓（4.2 节）；
   - `runtime` 有 v1/v2 两套（4.3 节）；
   - `dflow` 将来会从 GE 仓拆出去（4.1.3 节）。

**验收标准**：

- 导航图能让一个没读过 GE 的人，看着它就知道「我想读解析去哪、想读编译去哪、想读执行去哪」。
- 「意外发现」能说出**为什么**它让你意外，以及它反映了 GE 的什么设计取舍（解耦、双版本、未来拆分等）。

这个任务把「目录职责」「解耦思想」「模块导航」三个最小模块融在了一起。完成后，你就拥有了一张属于自己的 GE 源码地图。

## 6. 本讲小结

- GE 仓库的顶层目录是**沿数据流链路**组织的：`parser`（解析）→ `graph_metadef`（AscendIR 地基）→ `compiler`（编译）→ `runtime`（执行），另有 `api/inc`（接口）、`base`（基础）、`dflow`（异步流水）、`examples/tests`（样例/测试）。
- `AGENTS.md` 和 `architecture.md` 是官方的目录说明书，遇到不确定的目录优先查这两处。
- **GE 与算子仓是解耦的**：GE 只提供图的「基础结构」（`graph_metadef/graph`）和「注册接口」（`graph_metadef/register`），具体算子定义在外部独立仓。
- 三大主模块的导航入口分别是：解析 `parser/parser/onnx/onnx_parser.cc`、编译 `compiler/graph/manager/graph_manager.h`、执行 `runtime/v1/graph/load/model_manager/davinci_model.h`；地基是 `inc/graph_metadef/graph/compute_graph.h`。
- 几个值得记住的「意外」：算子外置、`runtime` 有 v1/v2 两套执行架构、`dflow` 未来会拆分成独立仓。

## 7. 下一步学习建议

有了目录地图，接下来的学习有两条路：

- **如果你想先把「地基」夯实**：进入 u2 单元（基石：AscendIR 图数据结构），从 [inc/graph_metadef/graph/compute_graph.h:L46](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/compute_graph.h#L46) 的 `ComputeGraph` 开始，深入 Graph/Node/OpDesc/Tensor 四层对象模型。这是后面所有模块的共同语言。
- **如果你想先把项目跑起来**：进入 u1-l3（构建系统：build.sh 与 CMake）和 u1-l4（端到端快速上手），亲手用 `build.sh` 构建组件、用 atc 编译一个模型样例，把本讲的「源码地图」和「能跑的产物」对应起来。

建议的阅读顺序是：u1-l3 → u1-l4 → u2 系列。先把项目跑通、再深入数据结构，理解会更踏实。
