# 项目总览：DualPipe 是什么

## 1. 本讲目标

本讲是整本学习手册的第一篇，面向**完全没接触过 DualPipe** 的读者。读完本讲，你应当能够：

- 说清楚「流水线并行（pipeline parallelism）」是什么，以及它为什么会产生**气泡（bubble）**。
- 用一句话描述 DualPipe 的定位：它是 DeepSeek-V3 技术报告里提出的**双向流水线并行算法**，目标是让前向/反向的**计算与通信完全重叠**，从而压缩气泡。
- 读懂 README 里那张关键的「气泡与显存对比表」，并能解释 DualPipe 为什么气泡比 1F1B / ZB1P 更小、代价是什么。

本讲几乎不涉及具体 Python 代码，重点是把**全局认知**建立起来，为后续逐层剖析源码打基础。

---

## 2. 前置知识

在进入 DualPipe 之前，先用最通俗的方式补齐几个概念。

### 2.1 大模型为什么需要「并行」

训练一个超大模型时，单张 GPU 装不下整个模型，于是人们把训练任务拆开：

- **数据并行（Data Parallelism）**：每张 GPU 放一份完整模型，各自处理不同数据，再汇总梯度。
- **模型并行（Model Parallelism）**：把模型本身拆到多张 GPU 上。它又分两类：
  - **张量并行（Tensor Parallelism）**：把一层的权重矩阵切成几块，分给多张 GPU。
  - **流水线并行（Pipeline Parallelism, PP）**：把模型**沿深度方向**切成几段，每段放在一张 GPU 上，数据像流水线一样依次流过每一段。**DualPipe 属于这一类。**

### 2.2 前向、反向与「阶段（stage）」

- **前向（forward）**：输入数据流过模型，得到输出（和损失）。
- **反向（backward）**：从损失出发，反向计算每个参数的梯度，用于更新模型。
- **阶段（pipeline stage, 简称 PP stage）**：流水线上的一个「工位」。若模型被切成 `PP` 段，就有 `PP` 个阶段，分别放在 `PP` 张 GPU 上。

### 2.3 微批次（micro-batch / chunk）

为了不浪费流水线，人们把一个大批次（batch）切成许多**微批次（micro-batch）**，也叫 chunk。微批次越多，流水线越能「填满」，设备利用率越高。DualPipe README 里那张调度图用的就是「8 个 PP rank、20 个微批次」。

### 2.4 什么是「气泡」

把流水线想象成 PP 个工人站成一排，一个微批次必须依次经过工人 0 → 1 → … → PP-1（前向），再原路返回（反向）。于是：

- **启动（灌水 / fill）阶段**：刚开始只有工人 0 在干活，工人 1 要等，工人 2 要等工人 1……前面的工人在发呆。
- **收尾（排水 / drain）阶段**：结束时只有最后一个工人在收尾，前面的工人又闲下来。

这两段「设备空闲」的时间，就是**流水线气泡**。气泡越小，GPU 利用率越高。**DualPipe 的全部努力，就是压缩这个气泡。**

> 名词对照：本讲后续用 `F`、`B`、`W`、`F&B`、`PP` 这几个符号，它们的精确定义见 4.2.2 节，全部来自 README。

---

## 3. 本讲源码地图

本讲主要只读一个文件，但它是理解整个项目的「总纲」。

| 文件 | 作用 | 本讲用到哪部分 |
|------|------|----------------|
| `README.md` | 项目说明：定位、调度图、气泡/显存对比表、快速开始 | 全篇，重点是对比表 |

后续讲义才会进入这些文件，这里先留个印象，知道它们存在即可：

- `dualpipe/__init__.py`：包的公共导出（DualPipe / DualPipeV / WeightGradStore 等）。
- `dualpipe/dualpipe.py`：DualPipe 核心调度引擎。
- `dualpipe/dualpipev.py`：DualPipeV 变体（V 型调度）。
- `dualpipe/comm.py`、`dualpipe/utils.py`：通信层与零气泡等公共工具。
- `examples/example_dualpipe.py`、`examples/example_dualpipev.py`：可运行的示例。

---

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：先认识 DualPipe 是什么（4.1），再精读气泡与显存对比表（4.2）。

### 4.1 DualPipe 是什么：定位与核心思想

#### 4.1.1 概念说明

DualPipe 是 **DeepSeek-V3 技术报告**中提出的一种**双向流水线并行（bidirectional pipeline parallelism）算法**。它要解决的核心问题就是上一节说的「气泡」。

它的两大思想：

1. **双向流水线**：数据从流水线**两端同时喂入**，相向流动。这样需要「灌满」的流水线长度从 `PP` 缩短到 `PP/2`，从源头减少气泡。
2. **计算与通信完全重叠**：把前向、反向的计算以及设备间的通信精心编排，让它们尽量同时进行（README 原话是 *full overlap of forward and backward computation-communication phases*）。调度图里，**被同一个黑色边框圈住的两个格子，就是互相重叠的一组计算与通信**。

> 直觉：传统流水线像「单车道单行线」，一头进一头出，灌满整条路很慢；DualPipe 像「双车道对向通行」，两头同时进，在中间汇合，灌满速度翻倍。

#### 4.1.2 核心流程

从高处俯瞰，DualPipe 的一个训练步（step）大致是：

1. **两端喂入**：第一个 rank 和最后一个 rank 各自接收一半微批次。
2. **相向流动**：微批次在「前向方向」从一端流向另一端；同时「反向方向」的微批次对称地从另一端流回来。
3. **逐 chunk 计算**：每个 rank 每次处理一个 chunk，做前向或反向计算。
4. **边算边通信**：计算当前 chunk 的同时，用 P2P 通信收发相邻 chunk 的数据，二者重叠。
5. **零气泡补位**：把「权重梯度」这种可以延后的计算塞进气泡里，进一步压空气泡。
6. **汇总**：第一个/最后一个 rank 收集到损失与输出，中间 rank 不持有最终结果。

这里的细节（8 步调度、状态管理、零气泡存储）会在第 3 单元逐讲拆解。本讲只需建立这个「双向 + 重叠」的直觉。

#### 4.1.3 源码精读

README 开篇一句话就点明了 DualPipe 的定位：

> DualPipe is an innovative bidirectional pipeline parallelism algorithm introduced in the DeepSeek-V3 Technical Report. It achieves full overlap of forward and backward computation-communication phases, also reducing pipeline bubbles.

这段话定义了三件事：来源（DeepSeek-V3 技术报告）、性质（双向流水线并行）、目标（前后向计算-通信完全重叠 + 缩小气泡）。来源：[README.md:L1-L3](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/README.md#L1-L3)（项目开篇定位）。

紧接着 README 给出 DualPipe 的调度图说明（8 个 PP rank、20 个微批次、两个方向；黑色边框圈住的两个格子互相重叠）：

[README.md:L5-L12](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/README.md#L5-L12)（DualPipe 调度图的文字说明：双向 + 重叠）。

> 提示：本仓库的 `images/dualpipe.png` 就是这张调度图。结合「双向、对称、黑色边框=重叠」三点去看图，会比单看公式直观得多。

README 还介绍了 DualPipeV——它是 Sea AI Lab 用「切半（cut-in-half）」方法从 DualPipe 推导出的一个**简洁的 V 型调度变体**。本讲先知道「有这么个变体」，它的细节留到第 4 单元讲：

[README.md:L14-L22](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/README.md#L14-L22)（DualPipeV：V 型调度，4 个 PP rank = 8 个 PP stage、10 个微批次）。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，不需要 GPU。

1. **实践目标**：通过阅读调度图说明，建立「双向 + 重叠」的直观画面。
2. **操作步骤**：
   - 打开仓库里的 `images/dualpipe.png`（或直接在 GitHub 上看 README 渲染出的图）。
   - 对照 [README.md:L5-L12](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/README.md#L5-L12) 的文字说明。
3. **需要观察的现象**：图里数据从**左右两端**同时进入；同一个黑色粗边框里有两个格子（一个前向、一个反向），表示它们在时间上重叠执行。
4. **预期结果**：你能在图上指出「灌水阶段」（开头空着的三角形区域）明显比单方向流水线更窄。
5. **说明**：本步无需运行命令；若暂时看不到图片，仅凭文字说明理解「双向 + 重叠」即可。

#### 4.1.5 小练习与答案

**练习 1**：用一句话向没听过 DualPipe 的同事解释它的核心思想。

> **参考答案**：DualPipe 让数据从流水线两端同时进入（双向），并把每个阶段的前向/反向计算与设备间通信安排在同一时间进行（重叠），从而把流水线里设备空闲的「气泡」压到很小。

**练习 2**：调度图里「被同一个黑色边框圈住的两个格子」代表什么？

> **参考答案**：代表一组**互相重叠的计算与通信**（或一次前向与一次反向），它们被刻意排在同一时间段内同时执行，这正是 DualPipe 消除气泡的关键手段。

---

### 4.2 气泡与显存对比表精读

这是本讲的重头戏。README 里有一张表，把 DualPipe 和 1F1B、ZB1P 在「气泡大小」和「显存占用」上做了直接对比。看懂它，就理解了 DualPipe 的全部价值与代价。

#### 4.2.1 概念说明

要先认识两个被拿来对比的「前辈」调度方法：

- **1F1B（One Forward One Backward）**：经典的流水线调度。做一个前向就紧跟一个反向，目的是省激活显存。它的气泡较大。
- **ZB1P（Zero Bubble, 1 Phase）**：来自「零气泡流水线并行」研究。它把反向拆成「输入梯度反向 B」和「权重梯度反向 W」两段，把可延后的 W 塞进气泡，从而把气泡进一步缩小。

DualPipe 在这两者之上，叠加「双向 + 完全重叠 + 零气泡」，把气泡压得更低。对比的前提是：**相同的 PP 阶段数**。

#### 4.2.2 核心流程（公式与符号）

先把符号定义清楚（全部来自 README 末尾的图例）：

| 符号 | 含义 |
|------|------|
| `PP` | 流水线阶段数（**偶数**） |
| `F` | 一个**前向 chunk** 的执行时间 |
| `B` | 一个**完整反向 chunk** 的执行时间 |
| `W` | 一个**「权重梯度反向」chunk** 的执行时间（B 的一部分，可延后） |
| `F&B` | 一对**互相重叠**的前向与反向 chunk 的执行时间（重叠后，明显小于 F + B） |

四种方法的**气泡**公式（来自对比表）：

\[ \text{Bubble}_{1F1B} = (PP-1)(F + B) \]

\[ \text{Bubble}_{ZB1P} = (PP-1)(F + B - 2W) \]

\[ \text{Bubble}_{DualPipe} = (PP/2 - 1)(F\&B + B - 3W) \]

\[ \text{Bubble}_{DualPipeV} = (PP/2 - 1)(F\&B + B - 3W) \]

**为什么 DualPipe 的气泡更小？** 拆成三股力量：

1. **系数从 (PP−1) 降到 (PP/2−1)**：双向流水线让需要「灌满」的有效长度减半，这是最显著的省气泡来源。
2. **F&B 取代 F + B**：前向与反向重叠后，`F&B < F + B`，每个 chunk 占用的时间变短。
3. **−3W 的零气泡补位**：把权重梯度 W 延后并塞进气泡，比 ZB1P 的 `−2W` 更激进（多了 1 个 W 的填充空间）。

**代价是什么？** 看表里后三列（以相同 PP 阶段数为前提）：

| 方法 | 每设备参数量 | 每设备激活显存 | 所需设备数 |
|------|--------------|----------------|------------|
| 1F1B | 1× | `PP` | `PP` |
| ZB1P | 1× | `PP` | `PP` |
| DualPipe | **2×** | `PP+1` | `PP` |
| DualPipeV | **2×** | `PP+1` | **`PP/2`** |

- **每设备参数量 2×**：因为 DualPipe 里**每个设备要持有两个阶段**（一个前向方向、一个反向方向），所以参数翻倍。
- **激活显存 PP+1**：略高于 1F1B 的 `PP`，因为双向往返带来了少量额外在途激活。
- **DualPipeV 只需 PP/2 设备**：它的 V 型「切半」结构让同样多的 PP 阶段只用一半设备即可承载（每个设备仍持 2 个阶段），代价是同一套参数/激活画像。

> 说明：DualPipe 与 DualPipeV 的**气泡公式完全相同**，差异只在「所需设备数」——这正是第 4 单元 u4-l2 要专门对比的工程取舍。本讲先记住结论即可。

> 关于公式系数的来源：`−3W`、`(PP/2−1)` 等系数的严格推导出自 DeepSeek-V3 技术报告（README 顶部给出的 arXiv 链接）。README 在这里只呈现最终公式，本讲讲清「直觉」，推导细节建议结合论文阅读。

#### 4.2.3 源码精读

对比表是 README 的核心，位于：

[README.md:L24-L36](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/README.md#L24-L36)（气泡与显存对比表 + 符号图例，本讲最重要的源码片段）。

表里 DualPipe 一行的四个值，含义对照：

- 气泡 `(PP/2-1)(F&B+B-3W)`：上一节已拆解。
- 参数 2×、激活 `PP+1`、设备 `PP`：上一节表格已说明。

「每个设备持有两个阶段」这件事，可以在示例代码里直接看到佐证（本讲仅作佐证，深入留到 u1-l2 / u2-l1）：

[examples/example_dualpipe.py:L138-L142](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L138-L142)（每个 rank 用 `nn.Sequential(stage, stage)` 持有两个 `PipelineStage`，对应「2× 参数」的来源）。

顺带一提，README 还给出了运行方式与依赖（PyTorch 2.0+），它们是下一讲 u1-l2 的内容，这里先留个入口：

[README.md:L38-L47](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/README.md#L38-L47)（快速开始：`python examples/example_dualpipe.py`）。

[README.md:L49-L51](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/README.md#L49-L51)（依赖：PyTorch 2.0 及以上）。

#### 4.2.4 代码实践

这是本讲的主实践任务（**源码阅读 + 数值代入型**，无需 GPU）。

1. **实践目标**：亲手算出 1F1B 与 DualPipe 在相同 PP 下的气泡，用自己的话解释 DualPipe 气泡更小的原因。
2. **操作步骤**：
   - 取 `PP = 8`。假设各 chunk 耗时为 `F = 1`、`B = 2`、`W = 0.5`，并设 `F&B = 2`（反向较长，前向藏在重叠里，故约等于 B；这只是**便于比较的假设值**）。
   - 代入公式，分别计算 1F1B、ZB1P、DualPipe 的气泡。
3. **需要观察的现象**：DualPipe 的气泡数值应明显小于 1F1B。
4. **预期结果**（按上述假设）：
   - 1F1B：\((8-1)(1+2) = 21\)
   - ZB1P：\((8-1)(1+2-2 \times 0.5) = 7 \times 2 = 14\)
   - DualPipe：\((8/2-1)(2+2-3 \times 0.5) = 3 \times 2.5 = 7.5\)
   - 即 DualPipe 的气泡约为 1F1B 的 36%。
5. **解释要点**：写出三点原因——①系数 `(PP/2−1)` 比 `(PP−1)` 小（双向减半）；②`F&B` 比 `F+B` 小（重叠）；③`−3W` 把权重梯度塞进气泡（零气泡补位）。同时说明代价是每设备参数 2×、激活 `PP+1`。
6. **说明**：上面的 `F/B/W/F&B` 比例是教学假设；真实比例需在具体硬件上 profile（README 提到可参考 profile-data 仓库），故数值结论标注为「**基于假设的示例，真实值待本地 profile 验证**」。

#### 4.2.5 小练习与答案

**练习 1**：在 `PP = 4` 时，1F1B 和 DualPipe 的气泡系数分别是多少？

> **参考答案**：1F1B 系数 \((PP-1) = 3\)；DualPipe 系数 \((PP/2-1) = 1\)。可见阶段数越多，双向带来的「系数减半」优势越明显。

**练习 2**：DualPipe 用「2× 参数」和「PP+1 激活」换来了更小的气泡。如果你显存极度紧张、但不在乎气泡大小，你会选 1F1B 还是 DualPipe？为什么？

> **参考答案**：选 1F1B。它每设备只要 1× 参数、`PP` 激活，显存更省；代价是气泡更大。工程选型本质是在「气泡（吞吐）」和「显存」之间权衡。

**练习 3**：DualPipe 和 DualPipeV 的气泡公式一样，那它们的区别体现在表的哪一列？

> **参考答案**：体现在「#Devices（所需设备数）」一列——DualPipe 需 `PP` 个设备，DualPipeV 只需 `PP/2` 个。

---

## 5. 综合实践

把本讲内容串起来，完成下面这个小任务（纸笔即可，无需 GPU）：

> **任务**：你是团队里负责训练 infra 的人，模型要切成 `PP = 8` 个阶段。请撰写一份**半页内的选型说明**，包含：
>
> 1. 用 4.2.4 的假设（`F=1, B=2, W=0.5, F&B=2`）算出 1F1B、ZB1P、DualPipe 三者的气泡数值。
> 2. 列出三者各自的「每设备参数 / 激活显存 / 所需设备数」。
> 3. 给出一个选型建议：什么场景优先 DualPipe，什么场景退回 1F1B。
> 4. 在结尾点出：若想进一步省设备，可以用 DualPipeV，并说明它能把设备数降到多少。

**参考要点**：气泡 21 / 14 / 7.5；参数 1×/1×/2×；激活 `PP`=8 / 8 / `PP+1`=9；设备都是 `PP`=8。气泡敏感、显存宽裕 → DualPipe；显存紧张、不在意吞吐 → 1F1B；想再省一半设备 → DualPipeV（设备数降到 `PP/2 = 4`）。

---

## 6. 本讲小结

- DualPipe 是 DeepSeek-V3 提出的**双向流水线并行**算法，目标是让前后向的**计算与通信完全重叠**，压缩流水线气泡。
- 流水线气泡来自「灌水（fill）」和「排水（drain）」阶段的设备空闲。
- DualPipe 的两个核心招数：**双向流动**（有效深度减半）+ **计算通信重叠 + 零气泡**（把 W 塞进气泡）。
- 气泡公式：1F1B `(PP-1)(F+B)`；ZB1P `(PP-1)(F+B-2W)`；DualPipe/DualPipeV `(PP/2-1)(F&B+B-3W)`。
- 代价：DualPipe 每设备 **2× 参数**、`PP+1` 激活显存；DualPipeV 在同样气泡下只需 **PP/2 设备**。
- 本讲只建立了全局认知，**没有任何源码细节**——这些都是后续讲义的内容。

---

## 7. 下一步学习建议

- 想动手跑起来：进入 **u1-l2《运行示例与环境准备》**，学习如何安装 DualPipe 并用 `python examples/example_dualpipe.py` 启动多 GPU 示例。
- 想了解代码结构：进入 **u1-l3《目录结构与包导出》**，认识 `dualpipe/__init__.py` 导出的五个公共符号。
- 想深入原理：等学完第 1 单元后，第 2、3 单元会从通信层、零气泡机制一路拆到 8 步调度引擎 `step()`。
- 配套阅读：建议同时打开 [DeepSeek-V3 技术报告](https://arxiv.org/pdf/2412.19437) 中关于 DualPipe 的章节，对照本讲的公式直觉去读论文里的推导。
