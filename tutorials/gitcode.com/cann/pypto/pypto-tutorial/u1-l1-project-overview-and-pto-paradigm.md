# PyPTO 是什么：项目定位与 PTO 编程范式

## 1. 本讲目标

本讲是整套 PyPTO 学习手册的第一讲，不要求你写过任何算子，也不要求你了解 NPU 硬件。学完本讲，你应该能够：

1. 说出 PyPTO 的定位（它是什么、为谁服务、解决什么问题）。
2. 解释 PTO（Parallel Tensor/Tile Operation）编程范式的核心思想，以及"基于 Tile 的编程模型"为什么能同时带来开发效率和性能。
3. 描述 PyPTO 的多层次编译流程：Tensor Graph → Tile Graph → Block Graph → Execution Graph → 可执行代码，并说出每一层各自解决什么问题。
4. 独立画出"从用户 Python 代码到设备执行"的完整流程框图，并标注每一步的中间表示（IR）名称。

本讲以官方 README 和两篇官方介绍文档为"源码"，重在建立全局认知；从下一讲开始才会真正运行代码和进入 Python/C++ 源码。

## 2. 前置知识

本讲需要的背景知识非常少，遇到下面的术语时按这里的解释理解即可：

- **算子（Operator / Op）**：深度学习中的基本计算单元，例如矩阵乘法（matmul）、加法（add）、softmax。所谓"算子开发"就是编写这些能跑在加速器上的高性能计算函数。
- **Tensor（张量）**：多维数组，是深度学习数据的基本载体，有形状（shape）、数据类型（dtype，如 FP16/FP32/INT32）等属性。
- **NPU / AI 加速器**：专门用于 AI 计算的芯片（本项目中特指 Ascend 系列）。芯片上有大量处理器核，每个核有自己的私有缓存（如 UB、L1）。
- **IR（Intermediate Representation，中间表示）**：编译器在"源代码"和"机器代码"之间使用的中间数据结构。PyPTO 使用多层 IR，一层比一层更贴近硬件。
- **Pass（编译遍）**：编译器中对 IR 做一次特定变换或优化的处理步骤，例如"消除冗余操作"、"插入同步"。多个 Pass 串联起来组成编译流水线。
- **JIT（Just-In-Time，即时编译）**：函数在第一次被调用时才编译，而不是提前编译。
- **SPMD / MPMD**：两种多核并行模型。SPMD（Single Program Multiple Data）是"同一段程序复制到多个核上跑同一逻辑"；MPMD（Multiple Program Multiple Data）是"不同核可以跑不同的程序，任务之间靠依赖关系组织"。
- **CodeGen（代码生成）**：编译器后端，把优化后的 IR 翻译成目标代码。PyPTO 的 CodeGen 产出的是"PTO 虚拟指令"，再由底层编译器编译成平台可执行代码。

如果你暂时分不清这些概念也没关系，后文结合 PyPTO 的具体设计逐个展开。

## 3. 本讲源码地图

本讲涉及的关键文件如下（均为仓库中真实存在的文件）：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/README.md) | 项目门面：概述、核心特性、目标用户、样例入口、目录结构 |
| [docs/zh/tutorials/introduction/introduction.md](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/introduction.md) | 官方简介：四层架构、多层图编译流程、设计理念、支持的硬件型号 |
| [docs/zh/tutorials/introduction/program_paradigms.md](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/program_paradigms.md) | 编程范式文档：PTO 范式、Tensor/Tile 数据结构、编程示例、MPMD 执行模型 |
| [examples/00_hello_world/hello_world.py](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/00_hello_world/hello_world.py) | 仓库自带的第一个可运行示例，本讲末尾做阅读预习，下一讲运行它 |

> 说明：本讲是"项目认知"讲，三份核心材料是文档而非 `.py`/`.cpp` 源码。这是刻意的——先建立地图，再进源码。从第 2 单元起，讲义引用的将主要是 `python/pypto/`、`framework/` 下的真实代码。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. 项目定位、目标用户与核心特性（README.md）
2. 分层架构与多层次编译流程（introduction.md）
3. PTO 编程范式与基于 Tile 的编程模型（program_paradigms.md）
4. 从计算图到 MPMD 设备执行（program_paradigms.md + introduction.md）

### 4.1 模块一：项目定位、目标用户与核心特性

#### 4.1.1 概念说明

PyPTO（发音：pai p-t-o）是华为 CANN 生态推出的一款**面向 AI 加速器的高性能编程框架**。它要解决的问题可以用一句话概括：

> 让开发者用接近数学表达的方式写算子，同时框架自动把它编译成能在 NPU 上高效执行的代码。

传统上，"算法"和"高性能算子"是两拨人写的：算法人员写公式，算子人员要把公式翻译成对硬件友好的搬运、切分、同步。PyPTO 的野心是用编译技术吞掉后一半工作，让同一个人兼顾开发效率和运行性能。

理解一个框架，先理解它**为谁设计**。README 明确列出三类目标用户，这直接决定了 PyPTO 的"分层抽象"设计：

| 目标用户 | 使用的抽象层次 | 关注点 |
| --- | --- | --- |
| 算法开发者 | Tensor 层次 | 快速实现和验证算法，专注算法逻辑 |
| 性能优化专家 | Tile / Block 层次 | 深度性能调优，追求极致性能 |
| 系统开发者 | Tensor/Tile/Block + PTO 虚拟指令集 | 三方框架对接、集成、工具链开发 |

#### 4.1.2 核心流程

PyPTO 对外宣称的核心特性可以归纳成一条主线：

```
Python 友好 API（Tensor 层次）
        │  开发者只描述"做什么"
        ▼
多层次 IR 编译（Tensor Graph → Tile Graph → Block Graph → Execution Graph）
        │  框架决定"怎么做"：切分、布局、调度、同步
        ▼
自动代码生成（CodeGen → PTO 虚拟指令 → 平台可执行代码）
        │
        ▼
MPMD 设备执行 + 全流程工具链可视化
```

也就是：**API 层隐藏复杂度，编译层保留优化空间，执行层释放硬件算力，工具链负责可观测性。**

#### 4.1.3 源码精读

README 的概述段一句话点出了项目全貌——PTO 范式、Tile 编程模型、多层次 IR 三个关键词都在这里出现：

- [README.md:L10-L12](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/README.md#L10-L12)：项目概述。注意三个加粗关键词——**PTO 编程范式**、**基于 Tile 的编程模型**、**多层次中间表示（IR）系统**，正好对应本讲 4.2、4.3 两个模块。

核心特性清单（共 7 条）值得逐条读过，它们是后续所有讲义的"目录页"：

- [README.md:L16-L24](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/README.md#L16-L24)：核心特性。第 2 条"多层级计算图转换"对应本手册第 4 单元（C++ Pass 体系）；第 3 条"自动化代码生成"对应第 5 单元（CodeGen）；第 4 条"MPMD 执行调度"对应 4.4 模块；第 7 条"分层抽象设计"就是上面那张三类用户表。

目标用户与官方样例入口：

- [README.md:L26-L30](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/README.md#L26-L30)：三类目标用户的官方定义。
- [README.md:L36-L41](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/README.md#L36-L41)：大模型实现样例（DeepSeek V3.2 稀疏 Flash Attention、GLM V4.5 Attention 等），是本手册第 7 单元实战讲的素材。
- [README.md:L43-L49](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/README.md#L43-L49)：examples 目录的三级学习路径（beginner / intermediate / advanced），这是本手册设计练习题的主要取材地。

仓库目录结构与快速入门入口：

- [README.md:L76-L115](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/README.md#L76-L115)：目录结构。现在只需记住四个顶层目录：`python/`（Python 前端源码）、`framework/`（C++ 编译框架源码）、`examples/`（示例）、`models/`（大模型算子实现）。第 3 讲会逐目录精读。
- [README.md:L62-L68](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/README.md#L62-L68)：快速入门三篇文档（环境部署、编译安装、样例运行）的链接，下一讲的实操就沿这条线走。

#### 4.1.4 代码实践

**实践目标**：建立"我该去仓库哪里找什么"的检索能力。

**操作步骤**：

1. 打开 [README.md](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/README.md)，通读一遍"概述 → 核心特性 → 目标用户"三节。
2. 在本地仓库中用文件管理器或 `ls` 查看 `examples/01_beginner/`、`examples/02_intermediate/`、`examples/03_advanced/` 三个目录各有哪些子目录，各挑一个最感兴趣的文件名记下来。
3. 查看 `models/` 目录，确认 README 提到的 `glm_attention.py`、`deepseekv32_sparse_flash_attention_quant.py` 确实存在。

**需要观察的现象**：examples 的三级目录内容难度递增；models 里放的是真实大模型算子而非教学示例。

**预期结果**：你得到一张自己整理的"样例索引表"（3 个 examples 文件 + 2 个 models 文件），后续讲义练习可以直接从这张表里选题。

**待本地验证**：目录内容以你本地检出的代码为准。

#### 4.1.5 小练习与答案

**练习 1**：PyPTO 的三类目标用户分别使用哪个抽象层次？为什么系统开发者需要用到"PTO 虚拟指令集"层次？

**参考答案**：算法开发者用 Tensor 层次；性能优化专家用 Tile/Block 层次；系统开发者用 Tensor/Tile/Block 加 PTO 虚拟指令集层次（见 [README.md:L26-L30](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/README.md#L26-L30)）。系统开发者要做三方框架对接和工具链开发，必须理解编译最终产物（虚拟指令）的形态，才能与底层编译器和运行时交互。

**练习 2**：README 核心特性中"分层抽象设计"与"Python 友好 API"两条是否矛盾？

**参考答案**：不矛盾。"Python 友好 API"说的是**默认路径**简单——算法开发者只写 Tensor 层代码；"分层抽象"说的是**逃生通道**丰富——需要极致性能或系统级集成时可以下沉到 Tile/Block/指令层次。前者保证入门门槛低，后者保证性能上限高。

### 4.2 模块二：分层架构与多层次编译流程

#### 4.2.1 概念说明

introduction.md 是理解 PyPTO 全局架构最重要的文档。它把框架分成四层：

1. **用户接口层**：Python API（Tensor 操作、Function、JIT 编译）。
2. **计算图编译层**：把用户表达的计算逻辑在四种 IR 之间逐层下降（Lowering）。
3. **代码生成层**：把优化后的图变成 PTO 虚拟指令，再编译成平台代码。
4. **调度执行层**：把可执行代码加载到设备，以 MPMD 方式调度到处理器核。

四种 IR 是本讲最重要的知识点，先用一个比喻建立直觉：把"用户写的算子"送到芯片上执行，就像把一份中文合同翻译成英文法律文书——

| IR 层次 | 比喻中的角色 | 关键动作 |
| --- | --- | --- |
| Tensor Graph | 中文口语原稿 | 贴近数学表达，保留全部优化空间 |
| Tile Graph | 按硬件能力切块的初译 | 切成能放进核内缓存的数据块 |
| Block Graph | 分派给不同律师的章节 | 子图分区、资源管理 |
| Execution Graph | 带签字顺序的最终流程 | 依赖关系 + 调度信息 |

#### 4.2.2 核心流程

完整的编译下降流程（每一步由一组 Pass 完成）：

```
用户 Python 代码（@pypto.jit 装饰的函数）
        │  前端解析（Python AST → PIL → IR，本手册第 3 单元）
        ▼
┌─────────────────┐
│  Tensor Graph    │  硬件无关优化：冗余操作消除、类型转换(auto_cast)、
│                  │  内存冲突推断、tile shape 推导……
└───────┬─────────┘
        │  Tiling：按 TileShape 切分
        ▼
┌─────────────────┐
│  Tile Graph      │  Tile 级优化：Tile 展开、内存类型分配、
│                  │  移动操作（load/store）生成、子图切分……
└───────┬─────────┘
        │  分区：切成可并行的计算子图
        ▼
┌─────────────────┐
│  Block Graph     │  Block 级优化：乱序调度、内存重用、
│                  │  同步点插入……
└───────┬─────────┘
        │  编排：整合子图、规划全局资源
        ▼
┌─────────────────┐
│ Execution Graph  │  最终执行图：依赖关系 + 调度信息
└───────┬─────────┘
        │  CodeGen
        ▼
   PTO 虚拟指令代码 ──► 编译器 ──► 目标平台可执行代码 ──► MPMD 设备执行
```

注意一个关键设计取舍：**上层保留优化空间，下层兑现性能**。Tensor Graph 不急于绑定硬件细节，因此"内存布局优化、数据搬运优化、多算子融合"这些全局优化才有发挥余地；越往下越贴近硬件，优化粒度越细。

#### 4.2.3 源码精读

- [introduction.md:L3](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/introduction.md#L3)：官方一句话定义，串起了"PTO 范式 → Tile 编程模型 → 多层图 → 硬件指令 → MPMD 执行"整条链路。
- [introduction.md:L9-L31](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/introduction.md#L9-L31)：官方 mermaid 架构图，四层架构与每层职责的一一对应关系（用户接口层↔Tensor 操作/JIT，计算图编译层↔四种 Graph，代码生成层↔虚拟指令，调度执行层↔MPMD）。
- [introduction.md:L36-L46](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/introduction.md#L36-L46)：四种 Graph 的定义，以及每个编译阶段由哪些类型的 Pass 组成。这段是第 4 单元（C++ Pass 体系）的总纲——未来你在 `framework/src/passes/tensor_graph_pass/`、`tile_graph_pass/`、`block_graph_pass/` 目录里读到的每个 Pass，都能在这里找到一句话定位。
- [introduction.md:L48-L54](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/introduction.md#L48-L54)：代码生成层与调度执行层的职责。
- [introduction.md:L75-L79](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/introduction.md#L75-L79)：设计理念的出发点——传统"算法/算子"分工的根源是高性能算子开发太复杂，PyPTO 用编译框架消除这份复杂度（类比 CPU 早期程序员手动排流水线指令，后来交给乱序执行和编译器）。
- [introduction.md:L81-L99](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/introduction.md#L81-L99)：计算层与编译层设计。计算层用 Tensor 描述保留"内存布局优化、数据搬运优化、多算子联合优化"三类潜力；编译层通过三段 Lowering Pipeline 把潜力兑现。
- [introduction.md:L127-L139](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/introduction.md#L127-L139)：支持的硬件型号（Ascend 950 系列、Atlas A3、Atlas A2 等）。

#### 4.2.4 代码实践

**实践目标**：把"四种 Graph"从名词变成你能复述的流程。

**操作步骤**：

1. 精读 [introduction.md:L36-L46](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/introduction.md#L36-L46) 两遍：第一遍逐行读，第二遍遮住右边，只看"Tensor Graph / Tile Graph / Block Graph / Execute Graph"四个名字，尝试回忆各自的定位和 Pass 类型。
2. 打开本地仓库的 `framework/src/passes/` 目录，观察其子目录命名。

**需要观察的现象**：`framework/src/passes/` 下存在与文档四种 Graph 相对应的子目录（如 tensor_graph_pass、tile_graph_pass、block_graph_pass 等）。

**预期结果**：你发现文档中的架构描述与源码目录结构一一对应——这是验证"文档没骗人"的最直接方式，也是后续读源码时的导航锚点。

**待本地验证**：目录清单以本地 `ls framework/src/passes/` 的实际输出为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Tensor Graph 阶段做的是"和硬件无关"的优化？如果把这个阶段的优化推迟到 Block Graph 再做，会损失什么？

**参考答案**：Tensor Graph 保持硬件无关，才能让"冗余消除、类型转换"这类基于纯计算语义的优化在最高抽象层自由发挥；此时图还没被切分和绑定资源，优化空间最大。推迟到 Block Graph 意味着图已经被 Tile 切分、分区、绑定了内存和调度信息，很多全局优化（如跨算子融合、整体布局调整）的时机已经错过，只能做局部修补。

**练习 2**：Execution Graph（执行图）相比 Block Graph 多了什么信息？

**参考答案**：依赖关系和调度信息（见 [introduction.md:L39-L46](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/introduction.md#L39-L46)）。Execution Graph 整合各计算子图、分析依赖、规划全局资源并生成调度提示，是 CodeGen 的直接输入。

**练习 3**：对应四层架构，`python/pypto/` 源码大致属于哪一层？`framework/src/codegen/` 属于哪一层？

**参考答案**：`python/pypto/` 主要属于用户接口层（并提供编译入口编排）；`framework/src/codegen/` 属于代码生成层。依据是 [README.md:L99-L104](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/README.md#L99-L104) 对 framework 子目录的注释。

### 4.3 模块三：PTO 编程范式与基于 Tile 的编程模型

#### 4.3.1 概念说明

PTO（Parallel Tensor/Tile Operation）编程范式的核心思想：**以 Tensor 为基本数据单位、以 Tensor 运算为基本计算单位来描述整个计算流程**，所有运算以 Tensor 为输入输出，天然形成可追溯的计算图。它是"声明式"的——开发者只说"做什么"（`out[:] = a + b`），框架负责"怎么做"（切分、搬运、调度）。

范式包含四个要点：

1. **Tensor 级别抽象**：以 Tensor 而非单个元素描述计算。
2. **声明式编程**：只描述计算意图。
3. **基于 Tile 的计算**：所有计算最终落在 Tile 上执行。
4. **计算图驱动**：框架基于图自动优化、调度、执行。

其中 **Tile** 是理解 PyPTO 性能模型的钥匙：Tile 是 Tensor 的子区间（sub-Tensor），大小经过设计，**恰好能放进处理器核的私有缓存（如 UB、L1）**。硬件执行计算前必须先把数据从大内存搬进核内缓存，Tile 就是"搬运的基本集装箱"。集装箱尺寸选得好，缓存命中率高、并行度高，性能就好。

在 Tensor 层次编程中，开发者通常**不需要手工切 Tile**——框架自动完成 Tiling，开发者只需（可选地）通过配置接口指定 TileShape。这正是 4.1 中"算法开发者只用 Tensor 层次"的技术保障。

PyPTO 提供三层编程接口，当前版本只开放 Tensor 层次：

| 层次 | 表达方式 | 特点 |
| --- | --- | --- |
| Tensor 层次（已开放） | Tensor + Tensor Operation | 框架自动 Tiling，隐藏硬件细节 |
| Tile 层次 | Tile + Tile Operation | 显式体现访存与依赖 |
| Block 层次 | 单核计算图 + 多次实例化 | 精确控制每个核做什么 |

#### 4.3.2 核心流程

一个 Tensor 层次算子从编写到执行：

```
①（可选）配置 Tiling：pypto.set_vec_tile_shapes(64)
② 用 @pypto.jit 装饰计算函数，参数用 pypto.Tensor[...] 标注
③ 函数体内写 Tensor 运算：result = a + b
④ 用 output[:] = result 写回输出（算子无返回值）
⑤ 第一次调用时触发 JIT 编译：Python → AST → PIL → IR → 四层图下降 → 指令
⑥ 后续调用直接复用编译产物执行
```

如果 Tensor 太大放不进核内缓存，Tiling 会把它切成多个 Tile。设一个 Tensor 形状为 \((M, N)\)，切分用的 TileShape 为 \((T_m, T_n)\)，则 Tile 总数为：

\[
\left\lceil \frac{M}{T_m} \right\rceil \times \left\lceil \frac{N}{T_n} \right\rceil
\]

其中 \(\lceil \cdot \rceil\) 表示向上取整——最后一个不足整块的 Tile 也要被处理（这也是后续学习中"边界处理"问题的来源）。TileShape 不是越大越好：太大放不进 UB/L1，太小则搬运次数增多、并行度不足。这个权衡是第 7 单元性能主题的核心。

#### 4.3.3 源码精读

- [program_paradigms.md:L3-L20](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/program_paradigms.md#L3-L20)：PTO 范式四要点与三层编程接口；L20 明确说明"当前版本仅开放 Tensor 层次编程"。
- [program_paradigms.md:L24-L31](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/program_paradigms.md#L24-L31)：Tensor 数据结构的四个属性（dtype、shape、format、name）及操作组合方式。
- [program_paradigms.md:L33-L39](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/program_paradigms.md#L33-L39)：Tile 的定义与三大设计目的（放进核内私有缓存提升局部性、利用并行能力、优化访存模式），以及"Tensor 层次中 Tiling 由框架自动完成"。
- [program_paradigms.md:L41-L43](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/program_paradigms.md#L41-L43)：View（不复制数据的子区间视图）与 Assemble（把多个子 Tensor 组合回大 Tensor），处理动态 Shape 和循环计算的两个利器。
- [program_paradigms.md:L49-L68](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/program_paradigms.md#L49-L68)：Tensor 层次基本编程模式的标准模板（set_vec_tile_shapes → @pypto.frontend.jit → output[:] = ...）。
- [program_paradigms.md:L82-L109](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/program_paradigms.md#L82-L109)：控制流写法——`pypto.loop(...)` 循环与 `pypto.cond(...)` 条件，配合 View 切片处理动态维度。注意与普通 Python 的 `for`/`if` 不同，这些是**编译期可分析的循环与条件结构**（第 2 单元第 5 讲展开）。
- [program_paradigms.md:L111-L121](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/program_paradigms.md#L111-L121)：符号化标量（SymbolicScalar）——动态 Shape 的表达手段，shape 维度可先填 -1，运行期才确定数值。

一个真实的对照：仓库自带的 hello_world 示例与文档模板几乎逐行对应——

- [hello_world.py:L26-L36](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/00_hello_world/hello_world.py#L26-L36)：真实的 `add_kernel`。L26 `@pypto.jit(runtime_options=runtime_options)` 装饰；L27 参数用 `pypto.Tensor[...]` 标注以自动推断 shape/dtype；L32 `pypto.set_vec_tile_shapes(32, 32)` 设置 TileShape；L36 `out[:] = x + y` 写回。注释还点明：pypto kernel 不支持返回值，`[:]` 是写回输出 Tensor 的语法糖。**文档模板与真实代码能对上**——这是本讲最重要的验证。

#### 4.3.4 代码实践

**实践目标**：把文档中的编程模式模板与真实示例逐行对齐，确认你读懂了每一行的作用。

**操作步骤**：

1. 打开 [program_paradigms.md:L49-L68](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/program_paradigms.md#L49-L68) 的编程模式模板。
2. 打开 [hello_world.py:L26-L36](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/00_hello_world/hello_world.py#L26-L36)，制作一张两列对照表：左列抄模板行，右列抄示例行，第三列用一句话写"这行在做什么"。
3. 特别回答：示例中 `set_vec_tile_shapes(32, 32)` 传了两个 32，而文档模板里只传了一个 64，为什么？（提示：读示例 L30-L31 的注释——向量 TileShape 的 rank 必须与张量 `x`、`y` 匹配。）

**需要观察的现象**：模板的四个要素（Tiling 配置、jit 装饰、Tensor 运算、`[:]` 写回）在真实示例中一个不缺。

**预期结果**：得到一张约 5 行的三列对照表，你能指着示例任何一行说出它对应模板的哪一步。运行示例本身留到下一讲（需要先装环境）。

#### 4.3.5 小练习与答案

**练习 1**：Tile 是什么？为什么它的大小要"能放进处理器核的私有缓存"？

**参考答案**：Tile 是 Tensor 的子区间，是计算和搬运的基本数据块（[program_paradigms.md:L33-L39](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/program_paradigms.md#L33-L39)）。处理器核计算时只能高效访问自己的私有缓存（UB/L1），数据必须先搬进来；Tile 恰好装满缓存能最大化数据局部性、减少重复搬运，并充分利用核的并行计算能力。

**练习 2**：形状为 \((128, 96)\) 的 Tensor 用 \((32, 32)\) 的 TileShape 切分，会得到多少个 Tile？其中有多少个"不完整的边界 Tile"？

**参考答案**：Tile 数 = \(\lceil 128/32 \rceil \times \lceil 96/32 \rceil = 4 \times 3 = 12\) 个。因为 96 不能被 32 整除？实际上 \(96 = 3 \times 32\) 恰好整除，所以 12 个 Tile 全部是完整的，没有边界 Tile。若 N 维是 100，则 \(\lceil 100/32 \rceil = 4\)，最后一列 Tile 只有 \(128 \times 4\) 有效，此时出现 4 个边界 Tile。

**练习 3**：`output[:] = result` 里的 `[:]` 是数组切片语法吗？

**参考答案**：不是普通的切片。它是对输出 Tensor 整体写回的语法糖——PyPTO kernel 不支持返回值，必须显式指明结果写到哪个输出 Tensor（见 [hello_world.py:L34-L36](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/00_hello_world/hello_world.py#L34-L36) 的注释，也可用 `pypto.assemble(x + y, [0, 0], out)` 等价表达）。

### 4.4 模块四：从计算图到 MPMD 设备执行

#### 4.4.1 概念说明

编译产出可执行代码后，最后一环是**在设备上调度执行**。PyPTO 采用 **MPMD（Multiple Program Multiple Data，多程序多数据）** 执行模型，与之对照的是 GPU 编程里常见的 SPMD（Single Program Multiple Data，单程序多数据）：

- **SPMD**：同一段 kernel 程序实例化到多个核上，每个核处理不同数据。逻辑简单，但所有核执行相同控制流，容易出现"快的核等慢的核"的全局同步开销。
- **MPMD**：把整个计算抽象成**一组异构任务**，任务之间用依赖关系组织。运行时调度器按依赖把不同任务分配到合适的核，不同核可以执行不同的程序片段，避免全局同步。

一个直觉类比：SPMD 像全班同学做同一张试卷的不同题目（结束时间被最慢的人拖住）；MPMD 像把一个项目拆成不同工种的活儿，谁先完成就领下一个没有前置依赖的任务。

计算图层面还有一组术语需要分清（来自 [program_paradigms.md:L125-L132](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/program_paradigms.md#L125-L132)）：

- **Tensor Op**：作用于 Tensor，逻辑上不受存储位置和规模约束。
- **Tile Op**：Tensor Op 的子集，限定输入输出位于同一核的 L1 内存，确保数据局部性。

#### 4.4.2 核心流程

MPMD 的一次执行流程（对应官方 mermaid 图）：

```
计算图 ──► 任务划分 ──► 依赖分析 ──► 任务调度 ──► 任务派发 ──► 并行执行 ──► 任务结果
```

结合前三个模块，现在可以拼出 PyPTO 的**全景链路**：

```
【写】  用户写 @pypto.jit 函数（Tensor 层次，声明式）
【译】  Python AST → PIL → IR（前端，python/pypto/）
        IR → Tensor Graph ──Pass──► Tile Graph ──Pass──► Block Graph ──Pass──► Execution Graph
        （编译框架，framework/src/passes/）
【生】  Execution Graph ──CodeGen──► PTO 虚拟指令 ──编译器──► 平台可执行代码
        （framework/src/codegen/ + 底层编译器）
【跑】  可执行代码加载到设备 ──MPMD 调度──► 各处理器核并行执行 ──► 结果写回
        （framework/src/machine/）
【看】  全流程中间产物 + 运行时性能数据 ──► 工具链可视化、控制编译与调度
```

#### 4.4.3 源码精读

- [program_paradigms.md:L125-L132](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/program_paradigms.md#L125-L132)：计算图的组成——Tensor 是数据节点，Op 分 Tensor Op 与 Tile Op。
- [program_paradigms.md:L134-L141](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/program_paradigms.md#L134-L141)：官方"计算图转换流程"mermaid 图：Tensor Graph → Tile Graph → Block Graph → Execution Graph → 可执行代码。本模块和 4.2 模块的流程图都源于此。
- [program_paradigms.md:L143-L148](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/program_paradigms.md#L143-L148)：查看计算图的两种方式——JSON 导出与 PyPTO Toolkit 可视化插件。调试编译产物时会反复用到（第 7 单元调试讲展开）。
- [program_paradigms.md:L150-L162](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/program_paradigms.md#L150-L162)：SPMD 与 MPMD 的官方对比，以及 MPMD 的四大优势：灵活调度（避免全局同步）、更好的资源利用、细粒度并行、适配多核 NPU 架构。
- [program_paradigms.md:L164-L169](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/program_paradigms.md#L164-L169)：MPMD 执行流程七步图。
- [introduction.md:L107-L115](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/introduction.md#L107-L115)：执行层设计——代码生成、目标平台编译、MPMD 调度三步，以及"自动化代码生成根据硬件特性生成最优指令"的设计意图。
- [introduction.md:L117-L125](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/introduction.md#L117-L125)：工具链设计——中间产物可视化（各阶段计算图）、运行时性能泳道图、编译与调度控制。

#### 4.4.4 代码实践

**实践目标**：在真实仓库中找到"执行层"对应的源码位置，验证架构文档与代码的对应关系。

**操作步骤**：

1. 读 [introduction.md:L107-L115](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/introduction.md#L107-L115)，记下执行层三步：代码生成、目标平台编译、MPMD 调度。
2. 在本地仓库查看 `framework/src/` 下的目录名，寻找与"加载/启动/执行"语义匹配的目录（提示：关注 `codegen/` 与 `machine/`）。
3. 进一步查看 `framework/src/machine/` 下的子目录命名（如 host、runtime 等），猜测各自职责并记录。

**需要观察的现象**：仓库中存在承载"设备侧加载与执行"的源码目录，且命名与文档描述的执行层职责可以对应起来。

**预期结果**：你得到一张"执行层三步 → 源码目录"的草图。第 5 单元第 2 讲（机器层三端架构）会精确剖析这些目录，届时用你的草图对照修正。

**待本地验证**：目录名以本地 `ls framework/src/`、`ls framework/src/machine/` 的实际输出为准。

#### 4.4.5 小练习与答案

**练习 1**：SPMD 与 MPMD 的本质区别是什么？MPMD 为什么更适合多核 NPU？

**参考答案**：SPMD 把同一段程序复制到多个核，靠全局同步协调；MPMD 把计算抽象为一组异构任务，靠依赖关系组织，运行时调度器把任务分配到合适的核（[program_paradigms.md:L152-L155](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/program_paradigms.md#L152-L155)）。多核 NPU 上任务粒度、耗时差异大，MPMD 避免全局同步限制，能提升整体利用率与效率，且支持细粒度并行。

**练习 2**：Tensor Op 和 Tile Op 的区别是什么？"Tile Op 限定输入输出位于同一核的 L1 内存"这一约束换来的是什么？

**参考答案**：Tensor Op 作用于 Tensor，逻辑上不受存储位置和规模约束；Tile Op 是其子集，限定输入输出在同一核的 L1 内存（[program_paradigms.md:L130-L132](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/tutorials/introduction/program_paradigms.md#L130-L132)）。该约束换来**数据局部性**：核内计算不再跨内存层次取数，访存延迟和带宽压力最小化，这正是 Tile 编程模型性能收益的来源。

## 5. 综合实践

**任务**：阅读 README 与两篇介绍文档后，用自己的话画出 PyPTO 从用户 Python 代码到设备执行的完整流程框图，并标注每一步对应的中间表示（IR）名称。

**要求**：

1. 框图必须包含以下要素，缺一不可：
   - 用户侧入口（`@pypto.jit` 装饰的函数）；
   - 前端处理（Python 代码变成框架 IR）；
   - 四种计算图（Tensor Graph → Tile Graph → Block Graph → Execution Graph）及每层图上至少一类典型优化/变换；
   - 代码生成（PTO 虚拟指令 → 平台可执行代码）；
   - 设备侧 MPMD 调度执行；
   - 工具链的位置（它横向贯穿编译与执行）。
2. 每个箭头旁用一句话写清"这一步发生了什么变换"。
3. 画完后自查：能否对着图向一个没读过文档的同事讲 3 分钟？讲不顺的地方就是你自己还没懂的地方，回到 4.2/4.4 重读。

**参考答案**（文字版框图，可与你的图对照）：

```
用户 Python 代码：@pypto.jit 装饰、pypto.Tensor[...] 标注、out[:] 写回
      │  前端解析：Python AST → PIL → 框架 IR（声明式 → 图表示）
      ▼
Tensor Graph ──（硬件无关 Pass：冗余操作消除、auto_cast 类型转换、
      │         内存冲突推断、tile shape 推导）──► 降级
      ▼
Tile Graph ──（按 TileShape 展开、内存类型分配、移动操作生成、子图切分）──► 分区
      ▼
Block Graph ──（乱序调度、内存重用、同步点插入）──► 编排
      ▼
Execution Graph（含依赖关系与调度信息的最终执行图）
      │  CodeGen：执行图 → PTO 虚拟指令代码
      ▼
PTO 虚拟指令 ──（底层编译器）──► 目标平台可执行代码
      │  加载到设备侧
      ▼
MPMD 调度：任务划分 → 依赖分析 → 任务调度 → 任务派发 → 多核并行执行 → 结果
══════════ 工具链：横向贯穿以上全流程（中间产物可视化 / 性能泳道图 / 编译调度控制）══════════
```

**交付物**：你的手绘或文本框图 + 每步一句话说明。这张图建议保留，后续每一讲学到新内容（如某个具体 Pass、CodeGen 细节）都往图上补充，学完整套手册后它就是你的 PyPTO 全景知识图。

## 6. 本讲小结

- **PyPTO 定位**：面向 AI 加速器（Ascend 系列）的高性能编程框架，用编译技术把"算法表达"和"硬件友好执行"之间的翻译工作自动化，服务算法开发者（Tensor 层）、性能专家（Tile/Block 层）、系统开发者（全层次+指令集）三类用户。
- **PTO 编程范式**：以 Tensor 为基本数据与计算单位、声明式描述计算，所有计算最终基于 Tile（能放进核内私有缓存的数据块）执行；当前版本开放 Tensor 层次编程，Tiling 由框架自动完成。
- **四层架构**：用户接口层 → 计算图编译层 → 代码生成层 → 调度执行层，文档描述与仓库 `python/`、`framework/src/passes/`、`framework/src/codegen/`、`framework/src/machine/` 等目录结构可一一对应。
- **多层次 IR**：Tensor Graph（硬件无关优化）→ Tile Graph（Tile 展开与访存优化）→ Block Graph（子图分区、调度、同步、内存复用）→ Execution Graph（依赖+调度），每层由一组模块化 Pass 完成变换。
- **执行模型**：CodeGen 产出 PTO 虚拟指令再编译为平台代码，设备侧以 MPMD（多程序多数据、依赖驱动调度）方式执行，相比 SPMD 避免全局同步、提升多核利用率。
- **验证方法**：本讲用"文档 ↔ 真实代码对照"（编程模板 vs hello_world、架构图 vs 源码目录）建立了不轻信文档、到仓库里核对的习惯。

## 7. 下一步学习建议

下一讲（u1-l2「环境搭建、编译安装与运行第一个算子」）将动手跑通第一个算子，需要提前阅读：

- [docs/zh/install/prepare_environment.md](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/install/prepare_environment.md)：基础环境准备。
- [docs/zh/install/build_and_install.md](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/install/build_and_install.md)：软件包获取与安装。
- [examples/00_hello_world/hello_world.py](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/00_hello_world/hello_world.py)：本讲已精读其 L26-L36，下一讲完整运行它，重点区分 `RunMode.NPU`（真机）与 `RunMode.SIM`（仿真，见 [hello_world.py:L39-L54](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/00_hello_world/hello_world.py#L39-L54)）两种运行模式。

更长期的路线：第 2 单元练 Tensor 层编程（算子、控制流、动态 Shape），第 3 单元进 Python 前端源码（AST→PIL→IR），第 4 单元读 C++ 多层图 Pass，第 5 单元看 CodeGen 与运行时，第 6 单元学 pypto_pro 低层编程，第 7 单元攻性能、精度、调试与大模型实战。每讲学完，记得回到本讲综合实践那张全景图上补一笔。
