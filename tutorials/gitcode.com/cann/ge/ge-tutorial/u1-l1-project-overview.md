# GE 是什么：定位与系统架构总览

## 1. 本讲目标

本讲是整本 GE 学习手册的第一篇，目标是让你在「不写一行 GE 代码」的前提下，建立对 GE 项目的全局认知。读完本讲，你应该能够：

- 说清楚 **GE（Graph Engine）到底是什么**——它是一个图编译器和执行器，而不是一个深度学习框架。
- 说清 GE 的 **四大组件**（前端适配层、atc、GE Compiler、GE Executor）各自的职责。
- 画出从「前端框架 / 模型文件」到「昇腾设备上执行」的一条完整数据流，并指出这条链路上 **哪一段是 AscendIR**、**哪一段产出 OM**。
- 区分 **在线场景** 和 **离线场景**，并知道在离线场景里 **哪些步骤不需要昇腾设备**。

本讲几乎不涉及 C++ 源码细节，重点是建立「地图」。后续讲义会带你逐层深入源码。

## 2. 前置知识

本讲面向零基础读者，但有几个名词最好先有个直觉：

| 名词 | 直觉解释 |
|------|----------|
| **昇腾（Ascend）** | 华为的 AI 处理器（芯片）系列，如 Ascend 910、Ascend 310、Atlas A2 等。GE 的最终目标就是把模型跑在这些芯片上。 |
| **CANN** | Compute Architecture for Neural Networks，昇腾的完整软件栈。GE 是 CANN 里的一个组件。 |
| **计算图（Graph）** | 把一个神经网络表达成「节点（算子）+ 边（数据流动）」的有向无环图（DAG）。 |
| **IR** | Intermediate Representation，中间表示。模型从一种格式翻译成另一种格式时的「中转站」。GE 用的 IR 叫 AscendIR。 |
| **算子（Operator / Op）** | 计算图里的一个节点，对应一次计算，如矩阵乘 MatMul、卷积 Conv。 |
| **OM** | 离线模型文件格式，GE 编译的最终产物，可被直接加载到昇腾设备上执行。 |
| **Host / Device** | Host 指主机 CPU 侧，Device 指昇腾加速卡侧。理解「哪段在 Host 跑、哪段在 Device 跑」是本讲的关键之一。 |

如果你对「计算图」完全没有概念，只需记住一句话：**神经网络 = 一张节点表示算子、边表示数据流向的有向图**。GE 的工作就是拿到这张图、把它编译优化成芯片能高效执行的形式，再在芯片上执行。

## 3. 本讲源码地图

本讲只读两类「文档型源文件」，它们是理解 GE 全貌的入口：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/README.md) | 项目门面：一句话定位、快速入门入口、生态集成列表。 |
| [docs/zh/design/architecture.md](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/design/architecture.md) | GE 的官方架构说明文档：四大组件、AscendIR、编译优化、插件机制、目录结构，是本讲的主要依据。 |
| examples/acl/1_sample_resnet50_imagenet_classification/README.md | ResNet50 推理样例，本讲用它把「离线场景」落到一条真实命令上。 |

> 说明：本讲引用的行号基于当前 HEAD `4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5`。如果后续代码更新导致行号变化，请以文件实际内容为准。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **架构总览**：GE 是什么、由哪几大组件构成。
2. **前后端与编译执行链路**：一条数据从输入到设备执行，中间经过哪些环节。
3. **在线 / 离线两种场景**：GE 有两种典型用法，它们的差异在哪里。

### 4.1 架构总览：GE 的定位与四大组件

#### 4.1.1 概念说明

很多人第一次接触 GE 会问：「GE 是像 PyTorch 那样的框架吗？」**不是。** GE 不负责让你写训练代码、不负责自动求导，它的定位非常聚焦：

> GE（Graph Engine）是面向昇腾的**图编译器**和**执行器**。

这句话有两个关键词：

- **图编译器**：拿到一张计算图，对它做优化（消除冗余、融合算子、规划内存、生成调度），产出能在昇腾芯片上跑的模型。
- **执行器**：把编译好的模型加载到芯片上，控制它的执行。

打个比方：PyTorch / TensorFlow 像是「写源码的高级语言」，GE 像是「编译器 + 运行时」——它把别人写好的「程序（模型）」编译成「芯片能跑的二进制（OM）」，再负责把这个二进制加载到芯片上跑起来。

在 CANN 生态里，GE 处于「承上启下」的位置：

- **向上**：对接各种前端（PyTorch、TensorFlow，或 onnx/pb 模型文件）。
- **向下**：对接昇腾硬件，生成可在芯片上执行的产物。

整个系统构成了「前端框架或模型文件 → AscendIR → 编译产物 model/OM → 设备执行」的一条链路。

#### 4.1.2 核心流程

GE 逻辑上由 **四大组件** 构成，沿数据流依次出场：

```
        ┌──────────────────┐
输入 ──▶ │ ① 前端适配层 / atc │  把外部 IR 翻译成 AscendIR
        └────────┬─────────┘
                 ▼  AscendIR（GE 的统一编译入口）
        ┌──────────────────┐
        │ ② GE Compiler     │  优化 + 编译，产出 model/OM
        └────────┬─────────┘
                 ▼  OM（离线模型产物）
        ┌──────────────────┐
        │ ③ GE Executor     │  加载到设备 + 控制执行
        └────────┬─────────┘
                 ▼
        ┌──────────────────┐
        │ ④ 昇腾设备(Device) │  实际算力所在
        └──────────────────┘
```

四大组件的职责一句话概括：

| 组件 | 职责 | 出现场景 |
|------|------|----------|
| **前端适配层**（TorchAir / TF Adapter） | 把框架 IR 转成 AscendIR，在框架内部驱动 GE | 在线场景 |
| **atc**（Ascend Tensor Compiler） | 直接对模型文件（onnx/pb）做编译，转 AscendIR 并产出 OM | 离线场景 |
| **GE Compiler** | 把 AscendIR 编译成可在设备执行的 model/OM | 两种场景都用 |
| **GE Executor** | 把模型加载到设备并控制执行 | 两种场景都用 |

注意：**前端适配层和 atc 是「二选一」的入口**——在线场景走适配层，离线场景走 atc。但无论走哪条，最后都要经过 GE Compiler 编译、GE Executor 执行。

#### 4.1.3 源码精读

先看 README 里对 GE 的一句话定位：

> [README.md:L11-L12](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/README.md#L11-L12) —— GE 是面向昇腾的图编译器和执行器，提供计算图优化、多流并行、内存复用、模型下沉等能力；并支持 PyTorch、TensorFlow 前端接入以及 onnx、pb 模型格式解析。

这一句是整个项目的「身份声明」，点明了 GE 的两大身份（编译器 + 执行器）和它提供的核心技术手段。

架构文档对这条链路的总述更为精确：

> [architecture.md:L5-L10](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/design/architecture.md#L5-L10) —— 「整个系统形成了从前端框架或模型文件，到 AscendIR，再到编译产物 model/OM，最终在设备上执行的一条完整链路」，并配了一张逻辑结构图 `ge_arch.svg`。

GE Compiler 的核心工作（按顺序）见：

> [architecture.md:L36-L44](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/design/architecture.md#L36-L44) —— 图级优化 → 算子编译 → 流分配 → 内存分配 → 模型序列化。

这五步就是「编译器」内部的主干流程，后续单元 4 会逐阶段拆解。

GE Executor 的职责见：

> [architecture.md:L46-L53](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/design/architecture.md#L46-L53) —— 模型加载（含「下沉」执行序列）与模型执行（含分支跳转、流同步等控制逻辑）。

#### 4.1.4 代码实践：对照架构图找组件

**实践目标**：用一张现成的架构图，把上面讲的四大组件在视觉上对上号。

**操作步骤**：

1. 打开 [docs/zh/figures/ge_arch.svg](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/figures/ge_arch.svg)（架构文档引用的逻辑结构图），或在仓库里查看 `docs/zh/figures/architecture.png`（README 顶部引用的图）。
2. 在图里找三个区块：**前端 / 输入侧**、**Compiler 编译侧**、**Executor 执行侧**。
3. 用笔（或文本）给每个区块标注它对应的组件名（前端适配层 / atc / GE Compiler / GE Executor）。

**需要观察的现象**：图里应该能看出一条从左到右（或从上到下）的数据流，中间有一个「统一汇聚点」——所有前端输入都汇入 AscendIR，再进入 Compiler。

**预期结果**：你能指着图说出「这一段是前端适配层、这个汇聚点是 AscendIR、这一段是 Compiler、最后这一段是 Executor」。

> 如果环境里不方便渲染 svg/png，也可以直接对照本讲 4.1.2 的 ASCII 流程图完成标注。

#### 4.1.5 小练习与答案

**练习 1**：GE 是一个深度学习训练框架吗？为什么？

> **参考答案**：不是。GE 不提供写训练代码、自动求导等框架能力，它专注于把已有的计算图**编译**成昇腾可执行的模型，再负责**执行**。它是「编译器 + 执行器」，定位在 CANN 栈中承上（对接前端）启下（对接硬件）的位置。

**练习 2**：四大组件中，哪两个是「互斥」的入口？哪两个是「必经」的公共路径？

> **参考答案**：**前端适配层**和 **atc** 是互斥入口（在线走适配层、离线走 atc）；**GE Compiler** 和 **GE Executor** 是必经的公共路径，无论哪种场景都要经过编译和执行。

---

### 4.2 前后端与编译执行链路

#### 4.2.1 概念说明

上一节讲了「四大组件」，本节把它们串成一条**完整的链路**，重点回答一个问题：**一张图从进 GE 到在芯片上跑完，到底经过了哪些环节？**

这条链路的关键枢纽是 **AscendIR**。AscendIR 是 GE 编译流程使用的核心 IR（中间表示），采用**静态计算图**的方式表达模型。无论输入来自前端框架（经适配层）还是来自模型文件（经 atc），**所有输入都会被统一转成 AscendIR**，再进入 GE Compiler。

换句话说：

- AscendIR 是 GE 的**统一编译入口**——它把「五花八门的前端」归一成「一种图」。
- GE 后续所有的优化（融合、内存、调度）都建立在 AscendIR 这一层之上。

需要特别区分两个容易混淆的概念：

- **静态图 vs 动态图**：指**图结构**是否在运行时变化。AscendIR 是**静态图**——图结构在编译期固定，执行时不会增删节点。GE 不支持图结构动态变化。
- **静态 Shape vs 动态 Shape**：指**张量的形状**是否在多次执行间变化。AscendIR **既能表达静态 Shape 图，也能表达动态 Shape 图**，GE 对二者都支持编译与执行。

> 提醒：日常交流里「静态图 / 动态图」常常其实是「静态 Shape / 动态 Shape」的简称，因为 GE 本身只有静态图。读文档时要结合上下文判断说的是哪种。

#### 4.2.2 核心流程

以「一个 ONNX 模型文件」为输入，完整链路如下（伪流程，聚焦环节而非源码细节）：

```
resnet50.onnx  (ONNX 格式模型文件)
      │
      │  ① atc 调用解析器(parser)，把 ONNX 节点/权重翻译成 AscendIR
      ▼
   AscendIR 计算图  (Graph / Node / Tensor / Anchor，结构已固定)
      │
      │  ② GE Compiler 多阶段编译：
      │      - 图级优化(融合、常量折叠、CSE、DCE)
      │      - 算子编译(按 shape 在线编译 kernel)
      │      - 流分配(把可并发的算子分到不同 stream)
      │      - 内存分配(整图视角规划/复用张量内存)
      │      - 模型序列化(产出 OM)
      ▼
   resnet50.om  (离线模型产物，含算子 bin、权重、执行序列)
      │
      │  ③ GE Executor 加载：把 OM 的资源(算子 bin、权重、执行序列)
      │     加载到设备；下沉模型会把整条执行序列预先放到设备侧
      ▼
   设备(Device) 上的可执行模型实例(DavinciModel 等)
      │
      │  ④ 一次 launch 触发执行，模型内部调度由硬件完成
      ▼
   推理输出 (如 Top-5 分类结果)
```

几个要点：

1. **环节①是「翻译」**：解析器负责把外部格式（onnx/pb 等）映射成 AscendIR 的节点与图结构，不做硬件相关的优化。
2. **环节②是「编译」**：这才是 GE 作为编译器的核心，覆盖图级、算子级、调度、内存多个维度。
3. **环节③是「加载」**：把编译产物搬到设备，并准备好执行所需资源。
4. **环节④是「执行」**：对于「下沉模型」，一次 launch 就触发整模型执行，算子间的调度交给硬件，从而减少 Host 侧逐算子下发的开销。

AscendIR 图本身是一张 **有向无环图（DAG）**，由这些元素构成：Graph（图）、Node（算子节点）、Tensor（张量）、Attribute（属性）、Data Edge（数据边）、Control Edge（控制边）。一个有意思的实现细节是：GE 里**没有独立的 Edge 对象**，连边关系是用「锚点（Anchor）」来表达的——DataAnchor 表示数据边，CtrlAnchor 表示控制边。这个细节会在第 2 单元（AscendIR 图数据结构）专门讲，这里先有个印象即可。

另外要记住一个边界：**具体的算子定义不在 GE 仓里**。GE 只维护图的基础结构（Graph/Node/Tensor/Attribute），算子的类型、输入输出、shape 推导、kernel 实现都由独立的**算子仓**（如 ops-math、ops-transformer）提供。这种解耦让 GE 保持「图编译器」职责清晰，算子体系可独立演进。

#### 4.2.3 源码精读

架构文档对 AscendIR 的定位：

> [architecture.md:L55-L67](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/design/architecture.md#L55-L67) —— AscendIR 是核心 IR，采用静态计算图；无论来自前端框架还是模型文件，所有输入都转换为 AscendIR，构成 GE Compiler 的统一编译入口。

AscendIR 的核心图元素（DAG 的构成）：

> [architecture.md:L79-L94](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/design/architecture.md#L79-L94) —— 列出 Graph/Node/Tensor/Attribute/Data Edge/Control Edge，并说明实现中通过锚点(Anchor)而非独立 Edge 对象表达连边。

算子定义外置于 GE 仓的边界说明：

> [architecture.md:L100-L108](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/design/architecture.md#L100-L108) —— GE 不定义每个算子的语义与实现，自定义算子与内置算子均外置于 GE，通过统一接口接入。

GE Compiler 内部的五步主干：

> [architecture.md:L36-L44](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/design/architecture.md#L36-L44) —— 图级优化 / 算子编译 / 流分配 / 内存分配 / 模型序列化。

GE Executor 的「加载 + 执行」：

> [architecture.md:L46-L53](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/design/architecture.md#L46-L53) —— 模型加载（下沉模型预先把执行序列放到设备侧）与模型执行（含分支跳转、流同步）。

仓库的顶层目录结构（帮助你在源码里定位这些环节）：

> [architecture.md:L203-L217](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/design/architecture.md#L203-L217) —— `parser/`（前端 IR 转 AscendIR）、`compiler/`（图编译）、`runtime/`（图执行）、`graph_metadef/`（图数据结构定义）、`api/`（公共 API，含 atc）、`dflow/`（DataFlow 执行器）等。

#### 4.2.4 代码实践：从 ONNX 到设备执行，画出 GE 组件链

**实践目标**：把本节讲的链路落到一个真实样例上，亲手标注每一步对应的 GE 组件。

**操作步骤**：

1. 打开 ResNet50 样例 README：[examples/acl/1_sample_resnet50_imagenet_classification/README.md](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/examples/acl/1_sample_resnet50_imagenet_classification/README.md)。
2. 找到「转换模型」这一步的真实命令（在「快速开始」或「Step 1」里）：

   ```bash
   atc --model=resnet50_Opset16.onnx --framework=5 --output=resnet50 \
       --soc_version=Ascend910B1 --input_format=NCHW --output_type=FP32
   ```

   其中 `--framework=5` 表示输入是 ONNX 格式（`0=Caffe, 1=MindSpore, 3=TensorFlow, 5=ONNX`），`--output=resnet50` 会产出 `resnet50.om`。

3. 对照本节 4.2.2 的链路图，把这条命令拆解：它触发了链路中的哪几个环节？（提示：`atc` 内部会调解析器 → AscendIR → GE Compiler → 产出 OM）。
4. 再看样例里的「编译运行」步骤（`bash scripts/run.sh`），它对应链路的哪个环节？（提示：加载 OM 到设备 + 执行）。
5. **用一段话写出**：从 `resnet50_Opset16.onnx` 到拿到分类结果，依次经过了哪些 GE 组件。

**需要观察的现象**：你会发现整条链路里，**编译（atc → OM）和执行（run.sh）是分开的两个阶段**，中间的「产物」就是一个 `.om` 文件。

**预期结果**：你的那段话里应当依次出现——**解析器(parser) → AscendIR → GE Compiler → OM → GE Executor → 设备执行**。

> 待本地验证：本实践不要求你真的跑通样例（需要昇腾设备与 CANN 环境）。如果你有环境，可以实际执行上述 atc 命令观察是否生成 `resnet50.om`；没有环境则完成「画链路 + 写一段话」即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 GE 要把所有前端输入都统一转成 AscendIR，而不是每种前端各写一套编译器？

> **参考答案**：因为 AscendIR 作为统一编译入口，可以让**下游的所有优化（融合、内存、调度）只写一遍**，复用到所有前端。新增一种前端只需写「前端 → AscendIR」的翻译（适配层或解析器），不必改动编译器主体。这是一种典型的「 Narrow Waist（窄腰）」架构：上层多变、下层固定，中间用一层 IR 解耦。

**练习 2**：AscendIR 是「静态图」，那它能不能表达「每次输入 batch size 不一样」的模型？

> **参考答案**：能。要区分两个概念：AscendIR 的**图结构**是静态的（节点不增删），但它能表达**动态 Shape**（张量形状在多次执行间可变）。batch size 变化属于动态 Shape，AscendIR 支持表达，GE 也支持其编译与执行。

---

### 4.3 在线 / 离线两种场景

#### 4.3.1 概念说明

GE 有两种典型用法，理解它们的差异是本讲的重点之一。

- **在线场景（Online）**：GE 作为前端框架的**后端（backend）**被框架内部驱动。用户在 PyTorch / TensorFlow 里照常写模型、跑模型，适配层在框架内部完成 IR 转换并调用 GE，**用户无需独立调用 GE**。
- **离线场景（Offline）**：GE **不参与前端框架的执行流程**，而是直接对**模型文件**（onnx、pb 等）做编译。用户用 `atc` 工具把模型文件编译成 OM，再把 OM 拿到设备上加载执行。

两者的本质区别在于：**GE 是被「框架」驱动，还是被「人/工具」驱动**；以及**编译和执行是否在时间上分离**。

- 在线场景：编译与执行耦合在框架的一次运行里，用户感知不到「OM 文件」。
- 离线场景：编译（atc）和执行（加载 OM）是分离的两个阶段，OM 是可独立部署的产物。

#### 4.3.2 核心流程

两种场景的对比流程：

```
【在线场景】
用户在 PyTorch/TF 写模型并执行
        │
        ▼
框架内部：适配层(TorchAir / TF Adapter) 把框架 IR → AscendIR
        │
        ▼
GE Compiler 编译 AscendIR → model
        │
        ▼
GE Executor 在设备上执行
（用户全程不碰 atc，也看不到 OM 文件）


【离线场景】
阶段 A：编译（可在无设备的 Host 上完成）
  模型文件(.onnx/.pb)
        │  atc 解析 → AscendIR → GE Compiler 编译
        ▼
  OM 文件(.om)   ← 可独立部署、可复制搬运的产物

阶段 B：部署执行（在有设备的环境）
  OM 文件
        │  GE Executor 加载到设备
        ▼
  在设备上执行
```

离线场景有三个非常实用的特点：

1. **无需昇腾设备**：编译（atc）纯靠 Host 侧就能完成，不占用昂贵的算力卡。
2. **无需前端框架运行时**：编译 `.onnx` 不需要装 PyTorch/TF。
3. **产物可独立部署**：OM 文件可拿到任意一台有昇腾卡的机器上直接加载执行。

这意味着：**「编译」和「执行」可以发生在不同的机器、不同的时间**——这是离线场景最大的工程价值。

#### 4.3.3 源码精读

在线场景（前端适配层）的定义：

> [architecture.md:L12-L21](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/design/architecture.md#L12-L21) —— 适配层把 GE 作为框架的后端，在框架内部完成 IR 转换并调用 GE，称为「在线场景」；目前有 TorchAir（AtenIR → AscendIR）和 TF Adapter（GraphDef → AscendIR）两个官方适配组件。

离线场景（atc）的定义与特点：

> [architecture.md:L23-L34](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/design/architecture.md#L23-L34) —— atc 是离线编译工具链，直接对模型文件编译；离线场景的特点是「无需昇腾设备、无需前端框架运行时、产物可独立部署」。

README 的「生态集成」列出了哪些项目已经把 GE 作为后端（即在线场景的实际案例）：

> [README.md:L36-L44](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/README.md#L36-L44) —— TorchAir（PyTorch 图模式）、TFA（TensorFlow 后端）、JittorInfer、Triton GE Backend、MindSpore Lite 等已集成 GE。

#### 4.3.4 代码实践：标注链路里哪些步骤不需要昇腾设备

**实践目标**：用离线场景的真实命令，亲手验证「编译阶段不需要设备」这一论断。

**操作步骤**：

1. 重新阅读 ResNet50 样例的 atc 命令（见 4.2.4）。
2. 在本节 4.3.2 的「离线场景」流程图上，把每个环节标注为 **「需要设备」** 或 **「不需要设备」**：
   - `atc` 编译 `.onnx` → `.om`：标注为？（依据 architecture.md L28-L32）
   - 把 `.om` 复制到另一台机器：标注为？
   - `GE Executor` 加载 OM 并执行：标注为？
3. 回答：如果你只有一台没有昇腾卡的普通服务器，你能完成这条链路的哪一段？

**需要观察的现象**：你会发现 **整条「编译」链路（解析 → AscendIR → Compiler → 序列化成 OM）都不需要设备**，只有「执行」阶段才必须有昇腾卡。

**预期结果**：

| 环节 | 是否需要昇腾设备 |
|------|------------------|
| atc 把 onnx 解析成 AscendIR | 否 |
| GE Compiler 编译 AscendIR → OM | 否 |
| 复制 / 分发 OM 文件 | 否 |
| GE Executor 加载 OM 并执行 | **是** |

> 待本地验证：上述结论直接来自架构文档 [architecture.md:L28-L32](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/design/architecture.md#L28-L32)，文档已明确「无需昇腾设备（纯靠 host 侧即可完成编译）」。如果你有环境，可在无卡机器上跑 atc 验证。

#### 4.3.5 小练习与答案

**练习 1**：某公司想在一台普通 x86 服务器上把团队训练好的 ONNX 模型转成昇腾可执行产物，再统一分发到多台 Atlas 卡机器上推理。它应该用在线场景还是离线场景？为什么？

> **参考答案**：用**离线场景**。离线场景下，编译（atc）不需要昇腾设备，也不需要前端框架运行时，可以在普通 x86 服务器上完成；产出的 OM 是可独立部署的产物，能复制分发到多台 Atlas 卡机器上加载执行。这正是离线场景的典型工程价值：**编译与执行分离、产物可搬运**。

**练习 2**：在线场景下，用户能看到 `.om` 文件吗？为什么？

> **参考答案**：通常感知不到。在线场景里 GE 是被框架（经适配层）内部驱动的，编译与执行耦合在框架的一次运行中，模型以内存中的形式直接交给 Executor 执行，用户无需（通常也不会）手动生成和管理 `.om` 文件。OM 文件主要是离线场景的产物。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个综合任务。

**任务背景**：你的同事给你一个 `resnet50_Opset16.onnx` 文件，问你「这个东西怎么在昇腾卡上跑起来」。请你用本讲学到的知识，给他写一份**不超过 200 字的中文说明**，要求：

1. 画出（或用文字描述）从 `resnet50_Opset16.onnx` 到拿到分类结果的**完整 GE 组件链路**。
2. 在链路上标注 **AscendIR 出现在哪一步**、**OM 在哪一步产出**。
3. 明确指出 **哪几步不需要昇腾设备**。
4. 指出本例用的是**在线场景还是离线场景**，并说明判断依据。

**参考要点（你可以据此自检）**：

- 链路：`onnx →(atc 调 parser)→ AscendIR →(GE Compiler)→ OM →(GE Executor 加载)→ 设备执行 → 分类结果`。
- AscendIR 出现在「解析之后、编译之前」；OM 在「GE Compiler 序列化」这一步产出。
- 「解析 + 编译 + 产出 OM」都不需要昇腾设备（依据 [architecture.md:L28-L32](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/design/architecture.md#L28-L32)）；只有「加载执行」需要设备。
- 本例是**离线场景**，因为输入是模型文件（`.onnx`）、用 `atc` 编译、产物是可独立部署的 `.om`，符合 [architecture.md:L23-L34](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/design/architecture.md#L23-L34) 对离线场景的定义。

> 进阶（可选）：如果你本地有 CANN 环境，可实际运行 4.2.4 的 atc 命令，确认在**不连接昇腾设备**的情况下也能生成 `resnet50.om`，亲手验证「编译不需要设备」。

## 6. 本讲小结

- **GE 是图编译器 + 执行器**，不是深度学习框架；它在 CANN 栈里承上（对接前端）启下（对接昇腾硬件）。
- **四大组件**：前端适配层（在线入口）、atc（离线入口）、GE Compiler（编译）、GE Executor（执行）。前两者互斥，后两者必经。
- **统一枢纽是 AscendIR**：所有前端输入都转成 AscendIR 再编译；AscendIR 是静态图（结构固定），但能表达动态 Shape。
- **完整链路**：输入 → AscendIR → GE Compiler（优化/算子编译/流分配/内存/序列化）→ OM → GE Executor 加载 → 设备执行。
- **在线 vs 离线**：在线是框架驱动 GE（编译执行耦合），离线是 atc 编译模型文件产出 OM（编译执行分离，OM 可独立部署）。
- **离线编译不需要昇腾设备**：解析、编译、产出 OM 都在 Host 侧完成，只有加载执行才需要设备。

## 7. 下一步学习建议

本讲建立的是「地图」，接下来的讲义会带你「下钻」到具体模块：

- **如果想先把仓库结构摸熟**：下一讲 [u1-l2]「源码目录结构与模块划分」会对照真实目录讲 `parser/compiler/runtime/graph_metadef/api` 等顶层目录的职责，让你能在源码里快速定位本讲提到的每个组件。
- **想了解怎么把 GE 编译出来**：[u1-l3]「构建系统：build.sh 与 CMake 工程组织」讲解构建方式。
- **想先有个端到端体感**：[u1-l4]「端到端快速上手：样例运行与体验」用 ResNet50 样例走完 atc 编译 + ACL 执行的全流程。
- **想深入 AscendIR 数据结构**：进入第 2 单元（u2），从 Graph/Node/OpDesc/Tensor 四层对象模型开始，这是理解后续所有编译与执行机制的基石。

建议按 `u1-l2 → u1-l3 → u1-l4 → u2` 的顺序推进，先把「目录 + 构建 + 样例」三件事补齐，再进入 AscendIR 的源码世界。
