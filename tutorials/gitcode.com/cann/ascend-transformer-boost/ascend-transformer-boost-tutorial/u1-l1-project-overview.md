# 项目定位、加速原理与整体架构

## 1. 本讲目标

本讲是整套 ATB 学习手册的第一篇，目标是帮读者建立一个"鸟瞰图"。读完本讲，你应当能够：

- 说清楚 **ATB（Ascend Transformer Boost）是什么**、解决什么问题、谁在用它。
- 理解深度学习里 **Host（CPU）与 Device（NPU）协作下发算子** 的基本模型，以及为什么会出性能瓶颈。
- 区分 ATB 的**三大能力**：融合算子、图算子、插件机制，知道它们各自适用什么场景。
- 建立 ATB **从公共 API 头文件到内部各层** 的整体架构认知，能看懂 `include/atb/` 下每个头文件大致负责什么。

本讲**不要求你写或跑任何代码**，主要任务是阅读文档与源码、建立心智模型。后续篇章才会进入具体算子的调用与实现。

---

## 2. 前置知识

本讲尽量从零讲起，但有几个名词先解释清楚会更顺：

- **Transformer 模型**：当前主流的大语言模型（如 LLaMA、DeepSeek 等）所基于的网络结构，核心由自注意力（Self-Attention）、前馈网络（MLP）、归一化等模块堆叠而成。
- **昇腾（Ascend）/ NPU**：华为自研的 AI 处理器，NPU 是它的设备（Device）侧计算单元，对应 GPU 概念。
- **CANN**：昇腾的异构计算架构软件栈，提供驱动、运行时（acl/aclnn）和算子库，ATB 依赖它。
- **算子（Operator / Operation）**：计算图里的一个节点，比如矩阵乘、LayerNorm、注意力，都是算子。
- **Kernel**：真正跑在 NPU 硬件核（AI Core）上的函数，是算子在设备侧的最终执行体。
- **Host / Device**：Host 指 CPU 侧（控制程序），Device 指 NPU 侧（实际计算）。模型推理时，CPU 负责准备参数、把算子"下发"给 NPU 执行。

如果你对这些还完全陌生也不用担心，本讲会在用到时再展开。

---

## 3. 本讲源码地图

本讲只引用下面三个文件，它们足够勾勒出 ATB 的全貌：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L1-L309) | 项目首页：定位、架构、目录、快速上手、自定义算子与贡献流程。 |
| [docs/ATB加速原理.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/ATB加速原理.md#L1-L181) | 工作原理：算子下发过程、Host/Device 瓶颈、ATB 的三项优化与组图实例。 |
| [include/atb/atb_infer.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/atb_infer.h#L1-L28) | 公共 API 总入口头文件：用一组 `#include` 把所有对外头文件汇总起来。 |

后续第 4 节会围绕这三个文件展开，并补充一张由仓库目录推导出的分层架构图。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，依次回答四个问题：ATB 是什么 → 为什么需要它（加速原理）→ 它提供哪三大能力 → 它的软件架构与公共 API 长什么样。

### 4.1 ATB 是什么：定位与适用场景

#### 4.1.1 概念说明

ATB 的全名是 **Ascend Transformer Boost（昇腾 Transformer 加速库）**。一句话定位：

> 一款基于昇腾 AI 处理器、专门为 Transformer 模型的**训练和推理**而设计的高效加速库。

它不是一个通用深度学习框架（那是 PyTorch / MindSpore / Paddle 的角色），而是一个介于框架与底层 CANN 算子之间的**加速库**：框架或业务代码调用 ATB 提供的算子，ATB 内部再把任务高效地下发到昇腾 NPU 上。

它的定位可以从三个角度理解：

- **面向模型**：聚焦 Transformer 类模型，把 Transformer 里高频出现的结构（自注意力、KV Cache、RoPE、MLA、MoE 路由等）做成高性能的**融合算子**。
- **面向硬件**：充分利用昇腾的算力、存储带宽与内存带宽，做硬件加速与数据复用。
- **面向框架**：支持 PyTorch、MindSpore、Paddle 等多种框架接入。

#### 4.1.2 核心流程

从用户视角，ATB 在系统中的位置可以粗略画成：

```
┌──────────────────────────────────────────────┐
│  上层模型代码 / 深度学习框架 (PyTorch …)        │
└──────────────────────┬───────────────────────┘
                       │  调用算子 (C++ API / torch_atb)
                       ▼
┌──────────────────────────────────────────────┐
│  ATB 加速库  ← 本项目                          │
│  · 融合算子 / 图算子 / 插件机制                  │
│  · 执行框架 (Operation → Runner → Kernel)      │
└──────────────────────┬───────────────────────┘
                       │  下发到设备
                       ▼
┌──────────────────────────────────────────────┐
│  CANN 运行时 (acl/aclnn) + 昇腾 NPU 硬件        │
└──────────────────────────────────────────────┘
```

用户既可以**直接用 C++ 调** ATB 的算子，也可以通过 **`torch_atb` 这个 Python 模块**在 PyTorch 里调。两种入口在本讲后面会各看一段真实示例。

#### 4.1.3 源码精读

README 开篇就给出了 ATB 的定位介绍：

> [README.md:7-9](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L7-L9) —— Ascend Transformer Boost 加速库（简称为 ATB）是基于华为 Ascend AI 处理器、专门为 Transformer 模型的训练和推理而设计的加速库。

README 还列出了"为什么选择 ATB"的几条理由，集中体现了它的价值主张：

> [README.md:56-61](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L56-L61) —— 对 Transformer 模型的高效加速、高性能和高效率、底层高性能算子与高效算子组合技术、支持 PyTorch/MindSpore/Paddle 等多种框架。

值得注意的是 README 第 3 行提到项目时间线："2025/09 Ascend Transformer Boost 项目首次上线"，说明这是一个相对年轻的、仍在快速演进的开源库。

#### 4.1.4 代码实践

这是一次**源码阅读型实践**，帮助你从官方文案里提取 ATB 的定位。

1. **实践目标**：用一句话向一个没听过 ATB 的同事解释它是什么。
2. **操作步骤**：阅读 [README.md:56-61](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L56-L61) 的"为什么选择 ATB"四条。
3. **观察现象**：注意这四条分别强调"模型""硬件""底层算子""框架"四个维度。
4. **预期结果**：你能写出类似"ATB 是一个把 Transformer 高频结构做成融合算子、针对昇腾硬件优化、可被多种框架调用的加速库"的一句话总结。
5. 运行结果：本实践无需运行命令。

#### 4.1.5 小练习与答案

**练习 1**：ATB 是一个深度学习框架吗？请结合 README 说明。
**参考答案**：不是。ATB 是"加速库"，定位在框架与 CANN/硬件之间，提供算子与组图能力；框架（PyTorch/MindSpore/Paddle）是它的调用方。

**练习 2**：ATB 同时支持训练和推理吗？
**参考答案**：是。README 第 9 行明确写"为 Transformer 模型的训练和推理而设计"，仓库里 `src/ops/ops_infer`（推理算子）与 `src/ops/ops_train`（训练算子）两个目录也分别对应这两类。

---

### 4.2 为什么需要 ATB：加速原理与算子下发过程

#### 4.2.1 概念说明

要理解 ATB 为何存在，必须先理解**深度学习模型在 Host/Device 上是怎么跑的**。`docs/ATB加速原理.md` 把这件事讲得很清楚。

模型可以抽象成一张**计算图**：节点是算子，边是张量（数据依赖）。推理时：

- **Host（CPU）**上跑模型主体程序，逐个把算子"下发"给 **Device（NPU）** 执行；
- 必要时进行**同步**，等设备侧算完。

#### 4.2.2 核心流程

这种工作模式下，性能瓶颈有两种，理解这两种瓶颈是理解 ATB 所有优化动机的钥匙：

> [docs/ATB加速原理.md:9-17](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/ATB加速原理.md#L9-L17)

- **Host Bound（主机瓶颈）**：Host 下发太慢、NPU 太快，导致 NPU 上算子之间出现"空泡"（idle），算力没被用满。profiling 图上表现为 Kernel 之间存在间隙。
- **Device Bound（设备瓶颈）**：Host 下发很快、NPU 算得慢，设备算力已被吃满。这时要继续提速就得优化 Kernel 本身。

> **ATB 主要解决的是 Host Bound 问题**：模型越大、算子越多，Host 下发就越容易成为瓶颈，ATB 正是为此而生。

那么"下发一个算子"到底要经过哪些步骤？加速原理文档列出了六步（简化的算子下发过程）：

> [docs/ATB加速原理.md:19-80](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/ATB加速原理.md#L19-L80)

1. **合法性检查**：校验输入、输出、参数是否符合算子要求，防止错误参数提交到设备。
2. **InferShape（输出形状推导）**：由输入的 shape 和 dtype 推导输出的 shape 与 dtype。
3. **计算 Tiling（切分策略）**：单个 AI Core 一次处理的数据有限，需要把输入切成多块分批计算，这个切分算法叫 Tiling。
4. **获取 Workspace 大小**：算子内部常需要额外的 HBM 内存做数据交换或缓存，这部分空间叫 Workspace，需提前算出大小。
5. **分配 Workspace**：由执行框架（如 TorchNPU）分配，便于统一管理 HBM 资源。
6. **算子下发（Launch Kernel）**：把输入输出地址、Tiling、Workspace 地址等封装成参数列表，调用 Launch Kernel 通知设备执行。

其中 InferShape 的核心思想可以用一个最简单的矩阵乘例子说明。设左矩阵 shape 为 \(M \times K\)、右矩阵为 \(K \times N\)，则输出矩阵 shape 推导为：

\[ \text{out\_shape} = M \times N \]

对应文档里的图示：

> [docs/ATB加速原理.md:29-33](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/ATB加速原理.md#L29-L33) —— 通过算子的输入 Shape 和 Data type 推导输出 Shape 和 Data Type。

Tiling 策略则分两层：先**多核切分**（按核数切 M/K/N 得到单核 shape），再**核内切分**（按 Local Memory 大小进一步切成一次指令能处理的 baseM/baseN/baseK）。文档强调："同一个算子在不同 Tiling 策略下可能有 **10 倍性能差异**"，所以 Tiling 是 ATB 性能的关键之一。

#### 4.2.3 源码精读

加速原理文档在"算子下发过程"里给出的 Tiling 数据结构示例（仅作说明，非本项目头文件里的真实结构体）：

> [docs/ATB加速原理.md:51-60](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/ATB加速原理.md#L51-L60) —— `struct matmulTilingData { singleCoreM; singleCoreK; singleCoreN; baseM; baseK; baseN; }`，用于保存多核与核内切分结果。

这帮助你理解：ATB 在 Setup 阶段算出的 Tiling，本质上就是这样一个描述"如何切分数据"的结构体，后续会传给 Kernel 使用。

#### 4.2.4 代码实践

这是一次**阅读 + 排序型实践**，巩固对算子下发六步的顺序记忆。

1. **实践目标**：不看文档，按正确顺序复述算子下发的六步。
2. **操作步骤**：
   - 先在纸上写下你记忆中的六步顺序。
   - 再对照 [docs/ATB加速原理.md:19-80](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/ATB加速原理.md#L19-L80) 核对。
   - 标出哪几步是在 **Host（CPU）** 上做的，哪一步是真正在 **Device（NPU）** 上执行的。
3. **观察现象**：前五步（合法性检查、InferShape、Tiling、算 Workspace 大小、分配 Workspace）都在 Host 上完成；只有第六步 Launch Kernel 之后才真正让 NPU 开始算。
4. **预期结果**：你应能说出"合法性检查、InferShape、Tiling、Workspace 这四件 Host 侧准备工作越慢，Host Bound 就越严重"。
5. 运行结果：本实践无需运行命令。

#### 4.2.5 小练习与答案

**练习 1**：profiling 图上 NPU 的 Kernel 之间出现明显空泡，最可能是哪种瓶颈？应从哪个方向优化？
**参考答案**：是 Host Bound（Host 下发慢导致设备等待）。应从优化 Host 侧的算子下发来入手（这正是 ATB 的主战场），而不是去改 Kernel。

**练习 2**：Tiling 的"多核切分"和"核内切分"分别受什么约束？
**参考答案**：多核切分受当前核数约束（把 M/K/N 分给各核）；核内切分受 Local Memory 大小约束（确定一次矩阵乘指令处理多大的 baseM/baseN/baseK）。

---

### 4.3 三大能力：融合算子、图算子、插件机制

#### 4.3.1 概念说明

ATB 的接口功能被明确分成三部分，这是理解整个项目最关键的一张"能力地图"：

> [README.md:11-18](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L11-L18)

- **融合算子（Operation）**：经过优化的、可直接调用的算子，如 PageAttention、Linear 等。它们通常是把多个基础运算融合成一个 Kernel，性能高。
- **图算子（Graph Operation）**：用上述算子（或自定义算子）**组合成一张图**，然后像操作单个算子一样操作整张图，方便在不同模型、不同 layer 之间复用。
- **插件机制（Plugin）**：当 ATB 内置算子不够用时，用户可以**创建自己的算子**嵌入 ATB 执行框架。

加速原理文档从"如何解决 Host Bound"的角度，把这三种能力归纳为三点（用词略有不同，但一一对应）：

> [docs/ATB加速原理.md:82-95](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/ATB加速原理.md#L82-L95)

- **定制化融合算子**（[L88](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/ATB加速原理.md#L88)）：提供 Transformer 常用算子，精心设计、性能高。
- **轻量级组图**（[L90](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/ATB加速原理.md#L90)）：支持组图后像操作单算子一样操作图，可复用。
- **运行时优化**（[L92-L95](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/ATB加速原理.md#L92-L95)）：包含 **Tiling Cache（以存代算减少重复计算）**、**调度优化（让设备侧算子无间隙运行）**、**内存优化（中间 Tensor 复用，平均节省 Workspace 50%）** 三类手段。

#### 4.3.2 核心流程

三者不是并列的"功能开关"，而是面向不同需求的递进层次：

```
                需求：我有一个现成的、高频的 Transformer 子结构
                         │
                         ▼
              ① 融合算子（ATB 内置高性能 Operation）
                         │  找不到我想要的？需要把多个算子拼起来？
                         ▼
              ② 图算子（用 GraphOpBuilder 把算子组成图，整体调度）
                         │  还不够，我要写一个全新算子？
                         ▼
              ③ 插件机制（自定义 Operation + 自定义 Kernel 接入框架）
```

**融合算子 vs 图算子**是本讲最重要的区分点，务必记牢：

| 维度 | 融合算子（Operation） | 图算子（Graph Operation） |
|------|----------------------|---------------------------|
| 本质 | 一个算子（通常 Kernel 级融合） | 多个算子组合成的一张图 |
| 是否融合 Kernel | 是，常把多步计算融进一个 Kernel | **否**，只是单算子的组合调度，不做 Kernel 融合 |
| 复用粒度 | 单个算子 | 整段子结构（如一整个 MLP 层） |
| 典型例子 | Linear、PagedAttention、RMSNorm | 把 Linear + Split + Swish + Mul 组成一个 Llama MLP 图算子 |

特别强调：加速原理文档明确指出"**图算子只是单算子的组合，不涉及 Kernel 融合**"，这是它和融合算子最本质的区别。

> [docs/ATB加速原理.md:137-141](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/ATB加速原理.md#L137-L141) —— 图算子的 Setup 和 Execute 与单算子类似，区别仅在于 Setup 阶段做了 Workspace 复用优化。

#### 4.3.3 源码精读

加速原理文档给了一个非常直观的**组图实例**，把 Llama 的 MLP 子结构组成了一个图算子。核心逻辑（节选）：

> [docs/ATB加速原理.md:102-127](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/ATB加速原理.md#L102-L127) —— 用 `GraphOpBuilder` 的 `Init` / `Reshape` / `AddOperation` / `Build`，把 `Linear → Split → Swish → Mul` 四个算子串成一张图。

把文档里这段伪代码的逻辑翻译成流程：

```
hidden_states ──Reshape──> hidden_states_
                                   │
                      Linear(weight)│  → linear_out
                                   │
                          Reshape   │  → linear_out_
                                   │
                        Split ──────┼──> gate_out ──Swish──> swish_out ─┐
                                   └──> up_out ────────────────────────  ┤
                                                                      Mul → mlp_out
```

这就是一个**图算子**：四个算子被组装进一个 `GraphOpBuilder`，`Build()` 之后对外就是一个可像单算子一样 Setup/Execute 的整体。这种"拼好的子结构"可以在不同 layer / 模型间复用，而不用每次重新拼。

文档还描述了图算子的内部数据结构：用两个 vector 分别存放"算子节点"和"算子的输入输出"。

> [docs/ATB加速原理.md:133-135](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/ATB加速原理.md#L133-L135) —— ATB 内部用两个 Vector 容器分别存放算子节点和算子的输入输出。

#### 4.3.4 代码实践

这是一次**源码阅读型实践**，目标是把"三种能力"对应到真实文档段落。

1. **实践目标**：建立一个"需求 → 选择哪种能力"的判断直觉。
2. **操作步骤**：
   - 阅读 [README.md:14-18](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L14-L18) 的三条架构说明。
   - 阅读 [docs/ATB加速原理.md:88-95](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/ATB加速原理.md#L88-L95) 的三点能力。
   - 给三个场景各选一种能力：①只想用一个高性能的注意力算子；②想把一整层 MLP 打包复用；③需要一个 ATB 没提供的新算子。
3. **观察现象**：注意"图算子不做 Kernel 融合"这一点——它解决的是"组合调度与复用"，不是"单算子极限性能"。
4. **预期结果**：① → 融合算子；② → 图算子；③ → 插件机制（自定义算子）。
5. 运行结果：本实践无需运行命令。

#### 4.3.5 小练习与答案

**练习 1**：把两个 Linear 串联做成一个图算子，会比分别调用两个融合 Linear 算子更"融合"吗（即更少的 Kernel）？
**参考答案**：不会。图算子只是单算子的组合调度，**不做 Kernel 融合**，所以两个 Linear 在图算子里仍是两个 Kernel；它的收益来自统一调度（减少 Host 下发空泡）和 Workspace 复用，而不是 Kernel 数量减少。

**练习 2**：运行时优化的三项（Tiling Cache / 调度优化 / 内存优化）各自主要解决什么？
**参考答案**：Tiling Cache 解决重复 Tiling 计算（以存代算）；调度优化解决 Host 下发慢导致的 NPU 空泡（Host Bound）；内存优化通过中间 Tensor 复用降低 Workspace 占用（平均节省约 50%，可提升大 Batch 推理上限）。

---

### 4.4 软件架构分层与公共 API 头文件总览

#### 4.4.1 概念说明

理解了"三大能力"，还要知道这些能力在代码里**落在哪一层**。ATB 是一个**分层架构**的 C++ 库，从上到下大致是：

```
┌─────────────────────────────────────────────────────────┐
│ ① 公共 API 层      include/atb/*.h                       │
│    Operation / Context / GraphOpBuilder / Tensor / Param  │  ← 用户接口
├─────────────────────────────────────────────────────────┤
│ ② Operation 框架层 src/atb/operation                      │
│    OperationBase / GraphOperation / PluginOperation       │  ← 算子抽象、组图、插件
├─────────────────────────────────────────────────────────┤
│ ③ Runner 执行层    src/atb/runner                         │
│    OpsRunner / AclnnRunner / HcclRunner / GraphRunner     │  ← 执行单元、CANN/通信适配
├─────────────────────────────────────────────────────────┤
│ ④ 算子实现层       src/ops/ops_infer, ops_train           │
│    各具体算子的 Operation（Linear, SelfAttention…）        │  ← 算子的"业务实现"
├─────────────────────────────────────────────────────────┤
│ ⑤ Kernel 层        src/kernels                            │
│    AscendC 核函数 + Tiling + MKI 注册                      │  ← 真正在 NPU 上跑的代码
├─────────────────────────────────────────────────────────┤
│ ⑥ 框架绑定层       src/torch_atb, torch_atb               │
│    PyTorch / TorchNPU 绑定                                │  ← 给 Python 用
└─────────────────────────────────────────────────────────┘
```

> 说明：这张分层图是根据仓库目录结构（见 4.4.3）与公共头文件归纳出来的逻辑视图，不是项目里官方标注的图，主要用于建立心智模型。后续每个单元会逐层深入。

用户日常打交道最多的是**第 ① 层公共 API**，所以本讲把它单独拎出来讲清楚。

#### 4.4.2 核心流程

公共 API 全部以 C++ 头文件形式暴露在 `include/atb/` 下，并通过一个**总入口头文件 `atb_infer.h`** 汇总。用户只需 `#include "atb/atb_infer.h"` 就能拿到推理所需的全部接口。

典型的单算子调用流程贯穿这几层：

```
CreateOperation(Param)        [① API]
   → 构造一个 Operation 对象   [② 框架层]
context->... / op->Setup(...)  [① API]：算 Workspace、Tiling
   → Runner 准备 KernelGraph   [③ Runner 层]
op->Execute(...)               [① API]：下发
   → Runner 调用 Kernel / aclnn [③→⑤]
   → NPU 执行
DestroyOperation(op)          [① API]
```

后面第 4.4.3 会用 README 里真实的 C++ Demo 把这条链路对应到具体代码行。

#### 4.4.3 源码精读

先看**总入口头文件** `atb_infer.h`，它用一组 `#include` 把所有对外头文件聚到一起：

> [include/atb/atb_infer.h:12-20](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/atb_infer.h#L12-L20) —— `#include` 了 `context.h`、`graph_op_builder.h`、`operation.h`、`svector.h`、`types.h`、`utils.h`、`infer_op_params.h`、`train_op_params.h`、`common_op_params.h`。

这就是"公共 API 头文件总览"最直接的来源——`atb_infer.h` 里 include 了哪些，对外 API 就是哪些。下面这张表把 `include/atb/` 下的每个头文件（结合各文件里的主导出符号）归纳出来，**这是本讲最重要的速查表**：

| 头文件 | 主导出符号 | 负责 |
|--------|-----------|------|
| `atb_infer.h` | （汇总 include） | **推理 API 总入口**，include 了下面大部分头文件 |
| `types.h` | `Tensor`(118)、`TensorDesc`(103)、`VariantPack`(136)、`Dims`(89)、`Node`(170)、`GraphParam`(188) | 基础数据类型：张量描述、输入输出打包、图节点 |
| `svector.h` | `SVector<T>`(42) | ATB 自带的小型定长向量容器（用于装 Tensor 列表等） |
| `context.h` | `Context`(57)、`CreateContext`(153)、`DestroyContext`(194) | 执行上下文：管理全局资源、执行流 |
| `operation.h` | `Operation`(34)、`InferShape`(56)、`Setup`(83)、`Execute`(97)、`CreateOperation`(109)、`DestroyOperation`(120) | **算子抽象基类**与工厂函数（融合算子的核心接口） |
| `infer_op_params.h` | `infer` 命名空间下大量 `XxxParam`（如 `LinearParam`、`SelfAttentionParam`、`PagedAttentionParam`、`RmsNormParam`） | 各**推理算子的参数结构** |
| `train_op_params.h` | 训练相关 `XxxParam`（如 `RmsNormBackwardParam`、`LaserAttentionGradParam`） | 各**训练算子的参数结构** |
| `common_op_params.h` | `EventParam`(38)、`IfCondParam`(81) 等公共枚举/参数 | 跨算子共用的参数与枚举 |
| `graph_op_builder.h` | `GraphOpBuilder`(36)、`CreateGraphOpBuilder`(134)、`DestroyGraphOpBuilder`(143) | **图算子组图 API**（三大能力之"图算子"） |
| `operation_infra.h` | `OperationInfra : public Operation`(32) | **插件/算子基础设施基类**（与三大能力之"插件机制"相关） |
| `comm.h` | 通信域接口（如 `DestoryHcclComm`(79)） | **集合通信**（AllReduce/AllGather 等，HCCL 通信域） |
| `utils.h` | `Utils`(30) | 工具函数集合 |
| `atb_acl.h` | acl 适配相关 | 与 CANN acl 接口对接的辅助声明 |

> 表中括号内的数字是该符号在头文件里的行号，便于你直接跳转核对。例如 `Operation` 类定义在 `operation.h` 第 34 行。

> 小提示：表里 `comm.h` 的函数名是 `DestoryHcclComm`（源码里就是如此拼写，疑似笔误但确是真实接口名），引用时请照原样，不要"纠正"成 `Destroy`。

再看 README 里**真实的 C++ 单算子调用 Demo**，它把上面 ①→③→⑤ 的分层串了起来（节选关键阶段）：

> [README.md:207-254](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L207-L254) —— 完整的 faupdate demo：初始化设备与 Context → 创建 Operation → 装填 `VariantPack` → `Setup` 算 Workspace → `Execute` 下发 → 流同步 → 释放资源。

对应到分层：
- 创建上下文与算子：`aclInit` / `aclrtSetDevice` / `atb::CreateContext` / `CreateFaUpdateOperation`（[L209-220](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L209-L220)）——用的是 **① API 层**的 `Context`、`Operation`。
- 装填输入输出到 `atb::VariantPack`（[L222-227](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L222-L227)）——用的是 **① types 层**的 `VariantPack`、`Tensor`。
- `Setup` 算 workspaceSize 并分配（[L231-235](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L231-L235)）——对应加速原理里的第 3~5 步（Tiling + Workspace）。
- `Execute` 下发并 `aclrtSynchronizeStream` 同步（[L237-238](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L237-L238)）——对应第 6 步 Launch Kernel。
- 资源释放：先释放 `Operation`，再释放 `Context`（[L250-253](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L250-L253)）——注意**对象先释放、全局资源后释放**的顺序。

README 也给了等价的 **Python（`torch_atb`）调用**示例，省去了显式 Context/Setup/Workspace，由框架代劳：

> [README.md:174-192](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L174-L192) —— `import torch_atb` → 创建 `LinearParam` → `torch_atb.Operation(param)` → `op.forward([x, y])` → `torch.npu.synchronize()`。

可以看出：Python 入口更简洁，但底下走的还是同一套 ①→②→③→⑤ 的分层。

#### 4.4.4 代码实践

这是一次**源码阅读 + 建表型实践**，帮你把"头文件总览"内化为自己的地图。

1. **实践目标**：不查表，能说出 `include/atb/` 下每个头文件大致负责什么。
2. **操作步骤**：
   - 打开 [include/atb/atb_infer.h:12-20](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/atb_infer.h#L12-L20)，抄下被 include 的头文件清单。
   - 对清单里的每个头文件，去文件里找到它的主导出 `class`/`struct`（可参考 4.4.3 的表）。
   - 把 README 的 C++ Demo（[README.md:207-254](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L207-L254)）里出现的 `atb::Context`、`atb::Operation`、`atb::VariantPack`、`atb::Tensor` 分别对应到 `context.h`、`operation.h`、`types.h`。
3. **观察现象**：你会发现 Demo 里用到的类型，恰好都来自 `atb_infer.h` include 的那几个头文件——这正是"总入口头文件"的意义。
4. **预期结果**：你产出一张"头文件 → 主导出符号 → Demo 里哪里用到"的三列对照表。
5. 运行结果：本实践无需运行命令（运行 demo 需要昇腾环境，留到 u2-l1 再做）。

#### 4.4.5 小练习与答案

**练习 1**：如果只想用推理算子，`#include "atb/atb_infer.h"` 够吗？为什么？
**参考答案**：够。`atb_infer.h` 已经 include 了 `context.h`、`operation.h`、`types.h`、`infer_op_params.h` 等推理所需头文件（见 [atb_infer.h:12-20](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/atb_infer.h#L12-L20)），它是推理 API 的总入口。

**练习 2**：README C++ Demo 里为什么"先 DestroyOperation，再 DestroyContext"？
**参考答案**：`Operation` 是依赖 `Context`（全局资源/执行流）的对象，必须先释放依赖方（Operation），再释放被依赖的全局资源（Context），否则会出现使用已释放资源的问题。这与"构造时先建 Context 后建 Operation，销毁时逆序"的对象生命周期规则一致。

---

## 5. 综合实践

本讲的综合实践把四个模块串起来，完成你本讲的"毕业作品"——一张属于自己的 ATB 认知地图。

**任务**：基于本讲引用的真实文档与源码，完成下面三件事。

1. **画一张 ATB 分层架构图**
   - 参考 4.4.1 的六层模型与 [README.md:24-54](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L24-L54) 的仓库目录结构，画出从"公共 API → Operation 框架 → Runner → 算子实现 → Kernel → 框架绑定"的分层图，并在每层标注对应的仓库目录（如 `include/atb`、`src/atb`、`src/ops`、`src/kernels`、`src/torch_atb`）。
   - 标注清楚：用户最常用的是哪一层？（答：第 ① 层公共 API。）

2. **用自己的话写出"融合算子"与"图算子"的区别**
   - 至少覆盖三个维度：①本质（单算子 vs 多算子组合）；②是否做 Kernel 融合；③复用粒度与典型例子。
   - 关键判据必须写到：**图算子不做 Kernel 融合**，它解决的是组合调度与 Workspace 复用（依据 [docs/ATB加速原理.md:137-141](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/ATB加速原理.md#L137-L141)）。

3. **把三大能力对应到仓库入口**
   - 融合算子 → 哪个头文件？（`operation.h` + `infer_op_params.h`）
   - 图算子 → 哪个头文件？（`graph_op_builder.h`）
   - 插件机制 → 哪个头文件？（`operation_infra.h`）
   - 说明你分别在 `atb_infer.h` 的 include 清单里能否找到它们。

> 本实践全程不需要昇腾硬件，纯文档/源码阅读即可完成。完成它，你就具备了进入第 u1-l2（目录结构）和 u2（算子调用实战）的背景知识。

---

## 6. 本讲小结

- **ATB 是什么**：面向昇腾处理器、专为 Transformer 训练/推理设计的加速库，位于框架与 CANN/硬件之间。
- **加速原理**：模型在 Host 上逐个把算子下发到 Device；当 Host 下发慢于 NPU 执行时出现 **Host Bound**，ATB 主要解决的就是它。
- **算子下发六步**：合法性检查 → InferShape → Tiling → 算 Workspace 大小 → 分配 Workspace → Launch Kernel；前五步在 Host，最后一步才让 NPU 真正计算。
- **三大能力**：融合算子（高性能单算子）、图算子（多算子组合调度，**不做 Kernel 融合**）、插件机制（自定义算子接入框架）。
- **运行时优化**：Tiling Cache、调度优化、内存优化（中间 Tensor 复用，平均省 50% Workspace）。
- **公共 API**：全部在 `include/atb/` 下，由总入口 `atb_infer.h` 汇总；最常用的四类是 `Operation`、`Context`、`Tensor/VariantPack`、`GraphOpBuilder`。

---

## 7. 下一步学习建议

本讲建立了全局认知，接下来建议按手册顺序继续：

- **u1-l2 仓库目录结构与代码组织**：把本讲的"分层架构"对应到仓库里每个真实目录，学会快速定位源码。
- **u1-l3 构建系统与编译运行**：学会用 `bash scripts/build.sh` 把 ATB 编译出来。
- **u1-l4 ~ u1-l6**：依次深入 `types.h`（Tensor/VariantPack）、`context.h`（Context）、`operation.h`（Operation 接口），为 u2 的"算子调用实战"打基础。

如果你想立刻看一段可运行的算子调用，可以暂时跳到 **u2-l1（C++ 单算子调用 Demo 实战）**，跑通 README 里的 faupdate demo；但理解了本讲的分层与下发流程后，再看 Demo 会顺畅得多。
