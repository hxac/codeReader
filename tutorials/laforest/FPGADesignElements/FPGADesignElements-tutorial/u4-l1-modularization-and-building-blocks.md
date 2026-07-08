# 极致模块化与构建块库

## 1. 本讲目标

本讲是「系统设计标准与模块化哲学」单元的第一篇。学完之后，你应该能够：

- 说清楚本书为什么把设计拆到「连一个与门阵列都要做成模块」这种极致程度，以及这种拆法换来了什么。
- 复述模块分解的总原则：**把彼此无关的连接移入单独的子模块**。
- 把一个子系统拆成**处理（processing）/ 控制（control）/ 接口（interface）**三个模块，并说明三者各管什么。
- 理解「构建块库」思想：复杂模块由简单模块实例化拼装而成，源码里几乎不再出现「随机逻辑」。
- 学会用 CAD 工具的三阶段原理图（elaboration / synthesis / place-and-route）来检查缺失连接和流水线对齐。

本讲承接 [u1-l2](./u1-l2-repo-layout-and-conventions.md) 讲过的「一模块一文件、参数默认为 0、必须实例化」等仓库约定，把视角从「单个文件怎么写」抬到「整个设计怎么切分」。本讲提到的 Core/Instance/Adapter/Shim 四层架构与约束文件留给下一讲 [u4-l2](./u4-l2-system-architecture-and-constraints.md)。

## 2. 前置知识

在进入正题前，先用大白话把几个关键术语对齐：

- **模块（module）**：Verilog 里描述一块电路的基本单位，有名字、有端口（输入输出）、有内部逻辑。一个 `.v` 文件定义一个模块。
- **实例化（instantiation）**：在一个模块内部「放置」另一个模块的副本，并用连线把端口接起来，就像在原理图上摆一个芯片并走线。
- **CAD 工具**：把 Verilog 转成真实 FPGA 配置的工具链（如 Xilinx Vivado、Intel Quartus）。它会做逻辑优化、综合、布局布线。
- **综合（synthesis）**：把 Verilog 描述翻译成 FPGA 上的基本元件（LUT、触发器、BRAM、DSP 等）。
- **原理图（schematic）**：CAD 工具把综合前后的逻辑画成方块图，是检查设计的重要手段。
- **随机逻辑（random logic）**：散落在模块各处、没有名字、没有封装的零散门电路与赋值。本书的目标是尽量消灭它。

如果你已经读过 [u1-l1](./u1-l1-project-overview.md) 和 [u1-l2](./u1-l2-repo-layout-and-conventions.md)，应该知道本书自称「FPGA 的 libc」——一个可复用的硬件零件库。本讲要回答的核心问题是：**为什么要做成零件库？零件库里的零件又该多大、怎么组织？**

## 3. 本讲源码地图

本讲几乎全部内容都源自一份概念性文档 `system.html`，并用三个真实模块作为佐证：

| 文件 | 作用 |
|------|------|
| `system.html` | 本书《系统设计标准》正文。本讲主要读它的 **Modularization**（模块化）与 **Building Blocks**（构建块）两节，是本讲的理论来源。 |
| `Annuller.v` | 一个「按使能把信号清零」的极小模块。`system.html` 特意拿它当例子，说明「连一个与门阵列都做成模块」。 |
| `Constant.v` | 一个「输出常量」的极小模块。用来说明为何连常量也值得封装成模块。 |
| `Counter_Binary.v` | 一个二进制计数器，内部由 `Adder_Subtractor_Binary` 和多个 `Register` 实例化拼成。是「用构建块拼装复杂模块」的范例。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**模块化原则**（含三阶段原理图检查）、**数据/控制/接口分离**、**构建块库**。

### 4.1 模块化原则：把无关连接移入子模块

#### 4.1.1 概念说明

作者在 `system.html` 开篇就点出了全书立场的起点：

> Over time, I've found that most of the difficulty in FPGA system design comes from a lack of modularity, not having a library of building blocks, and from not using the logic optimization work done by the CAD tool to simplify design.

翻译过来：FPGA 设计的多数困难，不是因为算法难，而是因为**不够模块化、没有构建块库、没有善用 CAD 工具的逻辑优化**。

这里的「模块化」不是「随便分几个模块」，而是要做到一种近乎偏执的细粒度——**细到连一个与门阵列都要封装成模块**。这么做的理由有四条，都写在 Modularization 一节里：

1. **代码即设计**：当一个设计完全由模块组成时，代码表达的是「设计本身」而不是「实现细节」，并且顺带送你多种免费的文档与检查手段。
2. **通用意图 vs. 具体意图**：模块**定义**表达的是「一般性的设计意图」；而模块**实例**（通过它的名字、参数、连线）告诉你「在这个具体位置上的设计意图」。没有充分的模块化，读者就必须不断把零散逻辑「反推」回设计含义。
3. **保持设计层次**：把设计切成模块，能在原理图里保住逻辑层次（尽管最后在硬件里通常会被铺平）。
4. **局部聚焦**：在每个模块内部，你只需要关心当下相关的逻辑和连线——这正是模块分解的总原则。

#### 4.1.2 核心流程

模块分解的总原则只有一句话，也是 `system.html` 唯一用斜体强调的「指导原则」：

> *move unrelated connections into separate modules*（把彼此无关的连接移入单独的子模块）。

把它翻译成一个可操作的方法：

```text
1. 在一段逻辑里，找到若干行互相连接、却与周围逻辑不相连的代码。
2. 把这些行提取出来，参数化后封装成一个子模块。
3. 于是，留在原模块里的连接「必然」彼此更紧密，被更清晰地封装。
4. 重复，直到每个模块内部只剩下「当下相关」的逻辑。
```

这条原则之所以成立，可以用一个简单的反证来理解：如果一段逻辑里的连线彼此无关，那把它们放在同一个 `always` 块里只会制造噪音，让你在阅读时被迫同时处理多件本不相干的事；而一旦封进子模块，模块名本身就成了一句「这句话在说什么」的注释。

#### 4.1.3 源码精读

**（1）连与门阵列都做成模块**——`system.html` 在引出极致模块化时，直接点名 `Annuller` 作为例子：[system.html:L50-L62](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/system.html#L50-L62) 这段说明：把设计细分成模块，细到「一个简单的与门向量」为止，代码就表达了设计本身。

打开 `Annuller.v` 看它的注释，作者亲口解释了「为什么这么简单的东西也要做成模块」：[Annuller.v:L4-L7](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Annuller.v#L4-L7) ——要点是两点：**传达设计意图**（例如「把这个操作码变成空操作」），以及**避免 RTL 原理图被一堆零散的门电路塞满**。这就是上面「模块名即注释」的实证。

`Annuller` 的模块体本身极简，就是一个按 `annul` 信号把数据清零的组合逻辑：[Annuller.v:L49-L80](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Annuller.v#L49-L80)（通过 `IMPLEMENTATION` 参数可在 MUX 与 AND 两种实现间切换）。注意它遵守了 [u1-l2](./u1-l2-repo-layout-and-conventions.md) 讲过的规矩：文件开头 `default_nettype none`、`WORD_WIDTH` 参数默认为 0。

**（2）连常量都做成模块**——`Constant.v` 是另一个极端例子。它只做一件事：输出一个常量值。作者同样在注释里说明了「为什么」：[Constant.v:L4-L7](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Constant.v#L4-L7) ——通常这只是一句 `localparam`，但在需要与 IP Integrator 之类的图形化系统对接、必须「喂一个模块」时，把它做成模块就有了意义。这印证了「模块化」不只是为了好看，也是为了**对接外部的、基于模块的工程化系统**。

**（3）分解的总原则**——回到 `system.html`，作者把上面那句指导原则完整写在了带边框的段落里：[system.html:L94-L101](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/system.html#L94-L101)。读这段时抓住一个关键词：**unrelated connections**（无关的连接）。模块化的判据不是「这段代码有多长」，而是「这段代码的连线是否与周围无关」。

**（4）模块化的回报：三阶段原理图**——这是本模块最实用的一部分。`system.html` 指出，把设计切成模块后，依次在 CAD 工具里看三种原理图，可以检查很多东西：[system.html:L64-L92](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/system.html#L64-L92)。三种原理图各管一件事：

| 原理图阶段 | 看什么 | 能发现什么问题 |
|-----------|--------|---------------|
| **post-elaboration（精化后）** | 源码的方块图视图 | 缺失的连接、流水线级数与对齐 |
| **post-synthesis（综合后）** | 逻辑优化后的结果 | 代码是否如预期综合；逻辑是否从一个模块消失或迁移；整个模块消失往往意味着它因缺失连接被优化掉了 |
| **post-place-and-route（布局布线后，又称 implementation）** | 逻辑最终落在 FPGA 上的样子，常带延时标注 | 关键路径（critical path）如何在模块间流动，反过来指导架构 |

作者还提醒：**如果不切模块**，随着从 elaboration 走到 implementation，原理图会迅速退化成「一堆随机逻辑」，越来越难和源码对应起来。切模块就是为了在原理图里**保住设计层次**。

#### 4.1.4 代码实践

> **实践类型：源码阅读 + CAD 工具观察（若有条件）**

1. **实践目标**：体会「模块名即设计意图」，并理解为何 `Annuller` 这么简单的逻辑也要封装成模块。
2. **操作步骤**：
   - 打开 `Annuller.v`，找到 [Annuller.v:L4-L7](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Annuller.v#L4-L7) 的注释，圈出「传达设计意图」和「避免原理图被门电路塞满」这两条理由。
   - 在仓库里搜索 `Annuller` 被哪些模块实例化（可用 `grep -n "Annuller" *.v`），看看调用处是否真的比「直接写一句 `data & {WIDTH{~annul}}`」更易读。
   - 若本地有 Vivado/Quartus：把任意一个含 `Annuller` 实例的设计跑 elaboration，在 post-elaboration 原理图里找到这个 `Annuller` 方块，对比「不封装时同一逻辑会变成一堆零散的与门」。
3. **需要观察的现象**：封装后，原理图里出现一个名为 `Annuller`（或你给的实例名，如 `turn_opcode_to_nop`）的方块；实例名本身就在说明意图。
4. **预期结果**：实例名能直接当作一句话注释来读；原理图层次清晰，不再是「一堆门」。
5. 若无条件运行 CAD 工具，则**待本地验证**原理图部分，源码阅读部分仍可完成。

#### 4.1.5 小练习与答案

**练习 1**：本书把「一个与门阵列」都做成了 `Annuller` 模块。请用一句话说明这样做在 post-synthesis 原理图里带来的好处。

> **参考答案**：封装后，与门逻辑在原理图里呈现为一个有名字的方块，而不是一堆零散的门；若某个 `Annuller` 整个从综合结果里消失，能立刻提示「它的输出没人用或输入被固定，被优化掉了」，便于定位缺失连接。

**练习 2**：作者给出的模块分解指导原则是「把彼此无关的连接移入子模块」。请判断：下面哪种情况*最适合*先抽成子模块？(a) 一段很长的算术表达式；(b) 一段与周围逻辑无连接、自成一小团的译码逻辑；(c) 一个只有一行赋值的组合逻辑。

> **参考答案**：选 (b)。判据是「连线是否与周围无关」，而不是「代码长短」。正是 (b) 这种自成一体、与周围解耦的部分，封装后能让外层模块的连接变得更紧密、更易读。(a) 长但若与周围紧耦合就不必拆；(c) 太短且若与周围相关也不必拆。

### 4.2 数据/控制/接口分离：处理、控制与接口模块

#### 4.2.1 概念说明

「极致模块化」回答了「拆到多细」，接下来要回答「拆出来的零件怎么归类摆放」。`system.html` 在 Building Blocks 一节给出了一个非常实用的三分法：把一个子系统拆成三类模块——

- **处理（processing）/ 存储（storage）**：真正干活的数据通路，例如计数、运算、存数据。
- **控制（control）**：通常是那个有限状态机（FSM），决定处理通路何时做何事。
- **接口（interface）**：负责与系统其余部分打交道，例如把数据存储和配置寄存器「内存映射」出去，供 CPU 读写。

这个三分法和 4.1 的「无关连接移入子模块」是同一个原则的不同切面：**处理、控制、接口三者关心的信号本就不同，自然该分开**。把它们混在一个模块里，你会被迫同时读数据流、状态转移和外设协议——而这恰恰是「无关连接塞在一起」的反面教材。

#### 4.2.2 核心流程

把一个子系统拆成三模块，可以按下面的顺序思考：

```text
1. 先划出「数据通路」：数据从哪进、经过哪些运算/存储、从哪出。
   -> 这部分进 processing/storage 模块。
2. 再划出「指挥」：谁在什么条件下让数据通路前进、停止、清零、加载？
   -> 这部分进 control 模块（通常是 FSM）。
3. 最后划出「对外脸面」：外部（如 CPU 总线）如何读写上面的存储与配置？
   -> 这部分进 interface 模块（例如地址译码 + 内存映射）。
4. 三个模块之间只用手头最简单的信号（valid/ready、bank select 等）相连。
```

需要注意：这三个模块的**边界要落在「信号含义改变」的地方**。例如，从「地址总线」到「某个寄存器的片选」是一次含义转换，正是 interface 模块该干的事；从「片选 + 写数据」到「存储器写端口」则交给 processing 模块。

#### 4.2.3 源码精读

三分法本身写在 Building Blocks 一节的最后：[system.html:L119-L123](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/system.html#L119-L123)。原文把「控制/处理/接口分离」明确列为使用构建块的一大收益。

虽然本书大多数单个模块只体现三者之一（一个模块要么是数据通路、要么是控制、要么是接口），但 `Counter_Binary.v` 是一个很好的**微缩样本**，能让你看到「处理通路」和「控制信号」如何在源码层面分开：

- **处理/存储通路**：计数器的「下一个值」由一个独立的 `Adder_Subtractor_Binary` 实例算出：[Counter_Binary.v:L49-L74](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Counter_Binary.v#L49-L74)。注意注释 [Counter_Binary.v:L49-L53](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Counter_Binary.v#L49-L53) 直接说出分离的好处：**把加法器做成专门模块，让我们能改变计数方式（比如改成 BCD）而不动其他逻辑，并隐藏正确推断算术逻辑所需的技巧**——这正是「处理通路独立」的回报。
- **控制信号**：`run`、`load`、`clear`、`up_down` 这些是控制信号；它们如何组合成「是否写寄存器」的逻辑，被单独放在一个组合 `always` 块里：[Counter_Binary.v:L84-L90](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Counter_Binary.v#L84-L90)。这里的 `load_counter`、`clear_counter` 等就是「控制→处理」的握手信号。
- **存储**：最终的寄存由 `Register` 实例完成：[Counter_Binary.v:L92-L106](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Counter_Binary.v#L92-L106)。

在 `Counter_Binary` 内部，「处理（加法器）」「控制（run/load/clear 组合）」「存储（Register）」已经各自成块。若再外面包一层 CPU 读写接口，那就是一个完整的「处理 + 控制 + 接口」三模块子系统。

#### 4.2.4 代码实践

> **实践类型：源码阅读 + 重构方案设计**

1. **实践目标**：在真实模块里辨认出处理、控制、接口三类职责的边界。
2. **操作步骤**：
   - 打开 `Counter_Binary.v`，用三种颜色（或三种注释标记）分别标出：处理通路（`Adder_Subtractor_Binary` 实例、`incremented_count` 等）、控制信号组合（`always @(*)` 块里的 `load_counter` 等）、存储（各 `Register` 实例）。
   - 设想要给这个计数器加一个「CPU 可读写」的接口：CPU 给出地址 + 读写信号，要能读 `count`、写 `load_count`。请草拟一个 `Counter_Binary_Interface` 模块的端口表，说明它如何把 CPU 总线信号翻译成 `Counter_Binary` 的 `load`/`load_count`/`run` 等控制信号。
3. **需要观察的现象**：你会发现接口模块只做「地址译码 + 信号翻译」，完全不碰计数运算；计数运算仍在 `Counter_Binary` 里。三者职责互不串味。
4. **预期结果**：画出三个模块的连线草图，接口模块在左、`Counter_Binary`（含处理+存储）在右、控制 FSM（若需要复杂时序）居中或并到接口里。
5. 接口模块的完整实现**待本地编写验证**；本步只要求给出端口表与连线方案。

#### 4.2.5 小练习与答案

**练习 1**：为什么作者主张把「处理」「控制」「接口」分成三个模块，而不是放在一个大模块里用注释分区？

> **参考答案**：因为三者关心的信号本就不同类（数据流 vs. 状态转移 vs. 总线协议），属于「彼此无关的连接」。分开后，读处理模块时不必同时关心总线协议，读控制 FSM 时不必关心数据位宽；并且每个模块都能在原理图里独立出现、独立检查，缺失连接更容易暴露。

**练习 2**：在 `Counter_Binary.v` 里，`run`、`load`、`clear` 属于哪一类职责？算「下一个计数值」又属于哪一类？

> **参考答案**：`run`/`load`/`clear` 是**控制**信号（决定何时计数、何时加载、何时清零）；算「下一个计数值」（`Adder_Subtractor_Binary` 实例）属于**处理/存储**通路（数据运算）。两者通过 `load_counter`、`clear_counter` 这类控制信号汇合到 `Register` 实例上。

### 4.3 构建块库：用简单模块拼出复杂模块

#### 4.3.1 概念说明

前两模块讲了「怎么拆」和「怎么归类」。本模块讲全书的核心主张：**用拆出来的小模块，逐步拼装成一个可复用的「构建块库（building blocks）」**。

`system.html` 对构建块库的定位是：用模块化过程产出一批**注释良好、经过测试的构建块**，从而在大多数场合消除「随机逻辑」，并把设计知识文档化留给将来。换句话说，库里的每一块都是「未来自己/同事」可以直接拿来用的零件；新设计就是「把零件连起来」，而不是每次从零写门电路。

这里有一条非常重要的工程思想：构建块是**通用**的（写一次、处处用），而每次实例化时可以通过**调参数、固定某些输入、悬空某些输出**，让 CAD 工具把通用逻辑优化成当次的特化逻辑——**同时源码里仍保留清晰的设计意图**。于是设计就变成了「由线相连的模块层次」，任何残留的随机逻辑都只代表「非常局部的特殊情况」（比如把两个标志位合并成一个）。

#### 4.3.2 核心流程

构建块库的运作方式可以画成一条「自底向上」的流水线：

```text
最底层：最小构件（Constant, Annuller, Register, Adder_Subtractor_Binary, ...）
              │  实例化 + 连线
              ▼
中层构件：Counter_Binary = Adder_Subtractor_Binary + Register × N
              │  实例化 + 连线
              ▼
更高层构件：Accumulator, FIFO, 握手流水线, ...
              │
              ▼
最终：一个完整子系统 = 处理 + 控制 + 接口 三类模块的组合
```

每一层都不「发明」底层逻辑，而是**复用**下一层的构建块。这就是为什么本书能号称「FPGA 的 libc」——libc 里的 `printf` 调用 `malloc`，`malloc` 调用更底层的系统调用；本书里的 `Counter_Binary` 调用 `Adder_Subtractor_Binary` 和 `Register`，层层向下，最终落到最小的门级构件。

#### 4.3.3 源码精读

**（1）构建块库的总纲**——见 `system.html` 的 Building Blocks 小节：[system.html:L103-L114](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/system.html#L103-L114)。重点抓三句：构建块要「注释良好且经过测试」；通过调参数/固定输入/悬空输出让 CAD 优化通用逻辑而**源码意图不变**；最终设计是「由线相连的模块层次」，残留随机逻辑只代表局部特例。

**（2）构建块应该多小**——[system.html:L116-L118](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/system.html#L116-L118)：构建块应当相当小，实现大多数设计都常用到的逻辑功能，也就是本书列出的那些 FPGA Design Elements。

**（3）拼装的实证：Counter_Binary**——`Counter_Binary.v` 是「用构建块拼复杂模块」最干净的范例。它本身不写任何加法逻辑，而是实例化 `Adder_Subtractor_Binary` 算下一个值，再用多个 `Register` 存储计数值与各标志位：[Counter_Binary.v:L60-L74](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Counter_Binary.v#L60-L74)（加法器实例）与 [Counter_Binary.v:L94-L148](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Counter_Binary.v#L94-L148)（四个 `Register` 实例）。注释 [Counter_Binary.v:L49-L53](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Counter_Binary.v#L49-L53) 直接点明了复用的回报：换计数方式（如 BCD）时，只换加法器那一块，其余逻辑不动；并且把「正确推断算术逻辑的技巧」藏进了 `Adder_Subtractor_Binary`，使用者不必操心。

把这个例子和 4.1 的 `Annuller`、`Constant` 连起来看，整条链路就清晰了：

| 层次 | 构建块 | 作用 |
|------|--------|------|
| 最小 | `Constant` | 输出常量 |
| 最小 | `Annuller` | 按使能清零（传达「变成 no-op」的意图） |
| 最小 | `Register` | 存一个字（封装复位哲学，见 [u3-l2](./u3-l2-resets-and-register-module.md)） |
| 小 | `Adder_Subtractor_Binary` | 加减法（隐藏算术推断技巧） |
| 中 | `Counter_Binary` | 计数器 = 加法器 + 寄存器 |

#### 4.3.4 代码实践

> **实践类型：源码阅读 + 调用链追踪**

1. **实践目标**：亲眼看到「复杂模块 = 简单模块的实例化组合」，建立对构建块库的直觉。
2. **操作步骤**：
   - 打开 `Counter_Binary.v`，列出它实例化了哪些子模块（答案：1 个 `Adder_Subtractor_Binary`、4 个 `Register`）。
   - 任选其中一个 `Register` 实例（如 `count_storage`），读懂它的参数与端口连线：[Counter_Binary.v:L94-L106](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Counter_Binary.v#L94-L106)。注意 `RESET_VALUE` 被设成 `INITIAL_COUNT`，`.clock_enable` 接的是控制信号 `load_counter`——这就是 4.2 说的「控制信号汇合到存储寄存器上」。
   - 进一步追踪：`Adder_Subtractor_Binary` 内部又实例化了什么？（用 `grep -n "Adder_Subtractor_Binary" Adder_Subtractor_Binary.v` 或直接读该文件。）你会发现它内部又复用了更底层的进位/谓词构件。于是你看到了一条「计数器→加法器→更底层构件」的复用链。
3. **需要观察的现象**：每往下一层，模块都更小、更通用；上一层只负责「连线 + 设参数」，几乎不写新的门级逻辑。
4. **预期结果**：能画出 `Counter_Binary` 的实例层次树，并指认每一处实例「为什么用这个构建块、它替代了什么样的随机逻辑」。
5. 子模块内部细节若暂读不懂，标注**待确认**即可，重点是看清「实例化拼装」这一结构。

#### 4.3.5 小练习与答案

**练习 1**：`Counter_Binary` 内部为什么用 `Adder_Subtractor_Binary` 实例，而不是直接写 `count <= count + INCREMENT;`？

> **参考答案**：因为把加法做成专门的构建块，既能隐藏「让综合器正确推断加减法器/进位链所需的编码技巧」，又能让计数方式（如改成 BCD、饱和计数）只换这一块、不动其他逻辑；同时 `Adder_Subtractor_Binary` 本身经过测试、可被其他模块复用，符合构建块库思想。

**练习 2**：作者说「通过调参数、固定某些输入、悬空某些输出，让 CAD 工具优化通用逻辑，同时源码意图不变」。请结合 `Annuller` 的 `IMPLEMENTATION` 参数举一个例子。

> **参考答案**：`Annuller` 用 `IMPLEMENTATION` 参数（`"MUX"` 或 `"AND"`）选择两种实现：[Annuller.v:L66-L78](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Annuller.v#L66-L78)。实例化时设成固定值，综合后只留下选中的那一种实现，另一种被优化掉——源码里「这是一个可配置的 annuller」的意图保留，而硬件只付出所需的那点逻辑。这就是「通用构建块 + CAD 优化 = 特化实现」。

## 5. 综合实践

把本讲三个最小模块串起来，做一个综合练习。

**任务**：下面是一段「全塞在一起」的反面逻辑（**示例代码**，非仓库内文件），它同时做了计数（处理）、地址译码（控制/接口）、输出寄存（接口）三件事：

```verilog
// 示例代码：一段混在一起的逻辑（不要照抄，这是反面教材）
always @(posedge clock) begin
    if (clear)            count <= 16'h0000;
    else if (run)         count <= count + 16'h0001;   // 处理：计数
    select <= (count == 16'hFFFF);                     // 控制/接口：地址译码
    data_out <= memory[address];                       // 接口：读存储并输出
end
```

**请你完成**：

1. 按 4.2 的三分法，把这段逻辑拆成「**处理 + 控制 + 接口**」三个模块，画出（或用文字描述）三者的端口与连线。提示：处理模块可复用 `Counter_Binary`（见 4.3）；接口模块负责 `select` 译码与 `data_out` 输出寄存。
2. 指出这样拆分后，**在哪一阶段的原理图里能看到好处**，并具体说明能看到什么。
3. 对照 4.1 的指导原则，说明这三段逻辑为什么「本就该分开」（即：它们的连线如何「彼此无关」）。

**参考思路**：

- 处理模块：实例化 `Counter_Binary`，端口接 `clock`/`clear`/`run`，输出 `count`。计数细节不再写在顶层。
- 接口模块：用 `count == 16'hFFFF`（或更规范的地址译码构建块，如后续单元的 `Address_Decoder_*`）生成 `select`；用 `Register` 给 `data_out` 做输出寄存。
- 控制模块：若时序简单可并入接口；若 `run`/`clear` 来自复杂 FSM，则单独成一个 FSM 模块，输出控制信号给处理模块。
- **原理图好处**：在 **post-elaboration 原理图**里，顶层会呈现为「Counter_Binary 方块 + 译码方块 + 输出 Register 方块」三个清晰方块，能一眼数出处理通路有几级流水线寄存（这里 `data_out` 多了一级，对齐关系一目了然）；若 `data_out` 那个 `Register` 整个在 post-synthesis 里消失了，立刻提示「输出没人用或被优化」，从而发现缺失连接——这正是 `system.html` [L64-L92](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/system.html#L64-L92) 所说的检查手段。
- **为何该分开**：`count` 的递增只依赖 `count` 自身与 `run`/`clear`；`select` 只依赖 `count`；`data_out` 只依赖 `memory`/`address`。三条数据流彼此「连线无关」，混在同一个 `always` 块里就是典型的「无关连接塞在一起」，违反 4.1 的分解原则。

> 说明：以上重构方案**待本地在 CAD 工具中验证**原理图表现；源码阅读与方案设计部分现在即可完成。

## 6. 本讲小结

- 本书主张把设计模块化到极致——细到「一个与门阵列（`Annuller`）、一个常量（`Constant`）」都做成模块，因为模块名能传达设计意图、避免原理图被零散门电路塞满。
- 模块分解的总原则只有一句：**把彼此无关的连接移入单独的子模块**；留在原模块里的连接因此必然更紧密。
- 一个子系统应按职责拆成**处理（数据通路/存储）、控制（FSM）、接口（与外部打交道）**三类模块，三者信号本就不同类。
- 本书的核心主张是**构建块库**：用注释良好、经过测试的小模块，自底向上拼装出复杂模块（如 `Counter_Binary` = `Adder_Subtractor_Binary` + 多个 `Register`），让设计变成「由线相连的模块层次」，尽量消灭随机逻辑。
- 通过调参数、固定输入、悬空输出，CAD 工具会把通用构建块优化成特化实现，而源码里的设计意图保持不变。
- 模块化的直接回报是：依次查看 **post-elaboration / post-synthesis / post-place-and-route** 三种原理图，可以分别发现缺失连接与流水线对齐、逻辑被优化或整模块消失、以及关键路径如何跨模块流动。

## 7. 下一步学习建议

- **下一讲 [u4-l2](./u4-l2-system-architecture-and-constraints.md)** 会把本讲的「子系统内三模块」视角抬到「整个 FPGA 工程」层面，讲解自底向上的 **Core / Instance / Adapter / Shim** 四层架构，以及为何要按类型把约束分文件、又有哪些约束（如 `ASYNC_REG`）必须写进源码。本讲的构建块库思想是 Core 层的基础。
- 若想立刻看更多「构建块拼装」的实例，可读 `Accumulator_Binary.v`（累加器如何复用加法器与寄存器）、`Adder_Subtractor_Binary.v`（加法器内部又复用了什么）。
- 若对「用原理图检查设计」感兴趣，建议本地装一个免费版 Vivado/Quartus，把仓库里任一模块跑一遍 elaboration→synthesis→implementation，对照本讲 4.1.3 的表格观察三阶段原理图的差异。
- 后续进阶单元（u5 组合逻辑基础构件、u6 寄存器与流水线寄存器）会逐个深入本讲提到的最小构建块，届时你会看到「极致模块化」在零件层面的具体写法。
