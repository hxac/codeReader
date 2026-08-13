# ATVOSS 是什么：项目定位与价值

## 1. 本讲目标

本讲是整套 ATVOSS 学习手册的第一篇，目标是让你在完全不写一行算子代码的情况下，先建立三件事的认知：

1. **ATVOSS 到底是什么**：它是一套基于 Ascend C 的 Vector 算子模板库，名字本身就是一句定位说明。
2. **它解决了什么问题**：为什么我们不直接用 Ascend C 写算子，而是要再套一层 ATVOSS。
3. **它能在哪里用、依赖什么**：支持哪些芯片、最低的 CANN 版本、操作系统和工具链要求。

学完本讲，你应该能用自己的话向同事解释「ATVOSS 相比直接写 Ascend C 省掉了哪些事」，并准确说出它的软硬件依赖。具体到动手编译、跑样例，那是第 2 讲（目录结构与构建运行）的内容，本讲先打地基。

## 2. 前置知识

本讲假设你了解下面几个名词的大致含义，不需要深入：

- **算子（Operator）**：在 AI 框架里，神经网络中的各种数学运算（加、乘、开方、归一化等）被抽象成一个个「算子」。算子既能在 CPU 上跑，也能被编译到专用加速硬件上跑。
- **昇腾（Ascend）AI 处理器**：华为的 AI 加速芯片（如 Ascend 950 系列），上面有专门做向量和矩阵运算的计算核（AI Core）。
- **Vector 计算**：AI Core 上的一类计算单元，擅长做「把同一个操作同时作用到一大批数据上」的运算，也就是**逐元素（element-wise）计算**——比如对一个有 10000 个元素的张量，每个元素都取绝对值。
- **Ascend C**：昇腾官方提供给开发者的 C++ 编程语言/接口，用来直接写跑在 AI Core 上的算子。它功能强大，但需要开发者手动管理内存搬运（Tiling）、多核切分、流水同步等底层细节。
- **Tiling（分块）**：因为片上高速缓存（UB，Unified Buffer）容量有限，一大块数据无法一次性搬进去算，必须切成小块（Tile）分批搬运和计算。这个「怎么切、怎么搬」的过程就叫 Tiling。
- **表达式模板（Expression Template）**：一种 C++ 模板编程技巧，能在**编译期**把 `out = sqrt(a) + b` 这样的数学表达式编码进 C++ 类型系统，从而既写得像数学公式，又没有运行时开销。

如果上面某些词还陌生，不用担心，本讲会结合 ATVOSS 的实际文档逐步说明，后续讲义会逐层深入。

## 3. 本讲源码地图

本讲主要阅读两份项目文档（文档也是「源码」的一部分，尤其对第一篇总览讲义来说）：

| 文件 | 作用 | 本讲怎么用 |
|------|------|-----------|
| `README.md` | 项目门面，给出项目一句话定位、目录结构总览、快速入门与文档入口 | 建立「ATVOSS 是什么」的第一印象 |
| `docs/summary.md` | 项目分层概述，详解五层架构、三大核心特性、适用场景与软硬件要求 | 本讲的核心事实来源（架构、特性、依赖） |

此外，下列文件在本讲中会作为「路标」被提及，帮助你建立对后续讲义地图的预期，本讲不展开它们的内部实现：

- `examples/abs/abs.cpp`、`examples/muls/muls.cpp`、`examples/rms_norm/rms_norm.cpp`：三个从简到繁的算子样例。
- `docs/quick_start.md`：环境搭建与编译执行教程（第 2 讲精读）。
- `docs/tutorials/developer_guide.md`：编程指南（后续讲义精读）。

## 4. 核心概念与源码讲解

本讲对应三个最小模块：

- **4.1 ATVOSS 项目背景与定位**：名字、一句话定位、它在「开发者 ↔ 昇腾硬件」之间扮演的角色。
- **4.2 Vector 算子与 Ascend C 的关系**：为什么 Vector 算子难写，ATVOSS 如何封装 Ascend C。
- **4.3 适用场景与软硬件要求**：能用在哪、依赖什么。

### 4.1 ATVOSS 项目背景与定位

#### 4.1.1 概念说明

ATVOSS 这个名字本身就是它最准确的定位说明。展开看：

> ATVOSS（Ascend C Templates for Vector Operator Subroutines）

逐词拆解：

- **Ascend C**：它构建在 Ascend C 之上，不是另起炉灶，而是对 Ascend C 的封装。
- **Templates**：它用 C++ 模板技术实现，大量逻辑在编译期完成。
- **Vector Operator**：它针对的是 **Vector 类算子**，也就是逐元素计算那一类算子。
- **Subroutines**：它把这些算子封装成可复用的「子程序/算子库」。

一句话定位：**ATVOSS 是一套基于 Ascend C 开发的 Vector 算子模板库，目标是让昇腾硬件上的 Vector 类融合算子开发变得极简、高效、高性能、高扩展**。

它解决的核心痛点是：直接用 Ascend C 写一个 Vector 融合算子，开发者要同时操心「计算逻辑」和「硬件调度细节」（多核切分、Tile 分块、内存搬运、流水同步），代码量大、易错、难复用。ATVOSS 把后者封装起来，让你只描述「算什么」，调度细节交给框架。

#### 4.1.2 核心流程

从「开发者想写一个算子」到「算子跑在昇腾芯片上」，ATVOSS 在中间插入了一层抽象。可以这样理解职责划分：

```text
开发者的目标：  我要算 out = sqrt(in)        （计算逻辑）
                    │
        ┌───────────▼────────────┐
        │  ATVOSS 表达式层        │  ← 你只写这一层：用表达式描述计算
        │  （声明式描述）          │
        └───────────┬────────────┘
                    │  ATVOSS 在编译期 + 运行期自动处理
        ┌───────────▼────────────┐
        │  Tiling / 多核切分 /     │  ← 传统 Ascend C 要你手写
        │  内存搬运 / 流水同步      │
        └───────────┬────────────┘
                    │
        ┌───────────▼────────────┐
        │  Ascend C 底层 API      │  ← 最终落到这一层
        │  （DataCopy / Compute）  │
        └───────────▼────────────┘
                    │
              昇腾 AI Core 执行
```

关键点：ATVOSS **不是替代** Ascend C，而是在它之上做封装。最底层（Basic 层）依然调用的是 Ascend C 基础 API，因此 ATVOSS 的性能上限等于 Ascend C 的上限，它不会「少做什么」，只是让你「少写很多」。

#### 4.1.3 源码精读

**项目门面定位**——README 开篇一句话点明 ATVOSS 是什么：

[README.md:6](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/README.md#L6)：说明 ATVOSS 是一套基于 Ascend C 开发的 Vector 算子模板库，致力于为昇腾硬件上的 Vector 类融合算子提供极简、高效、高性能、高拓展的编程方式。注意「融合算子」一词——它指把多个连续的逐元素运算（如先平方、再求和、再开方）融合进一个算子里一次性算完，避免中间结果反复搬运。

**项目首次上线**——README 的 Latest News 区：

[README.md:2-4](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/README.md#L2-L4)：记录 ATVOSS 项目于 2025 年 11 月首次上线。这是一个较新的项目，了解这一点有助于你在遇到资料较少的情况时心中有数。

**一句话价值主张**——summary 的「ATVOSS 简介」段：

[docs/summary.md:16](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/summary.md#L16)：明确指出 ATVOSS 通过封装 Ascend C 的底层 API 和复杂的 Tiling 计算，大幅降低算子开发复杂度，让开发者用声明式方式描述计算逻辑。这一句浓缩了 ATVOSS 的全部价值。

#### 4.1.4 代码实践

**实践类型：源码阅读型实践**

1. **实践目标**：用自己的话把「ATVOSS 是什么」浓缩成一句话，并区分它和 Ascend C 的边界。
2. **操作步骤**：
   - 打开 `README.md` 第 6 行，抄下官方对 ATVOSS 的定义。
   - 打开 `docs/summary.md` 第 16 行，对照「封装了什么」。
   - 回答两个问题：（a）ATVOSS 最底层最终调用的是谁？（b）它替开发者挡掉了哪些工作？
3. **需要观察的现象**：你会注意到，两份文档都没有说 ATVOSS「重新实现」了硬件计算，而是反复强调「封装」「降低复杂度」。
4. **预期结果**：你应该能写出类似「ATVOSS 是基于 Ascend C 的 Vector 算子模板库；它最终仍调用 Ascend C 底层 API，但替开发者封装了 Tiling 切分、内存搬运和多核调度，让人只需声明式描述计算逻辑」这样的总结。
5. **说明**：本实践不涉及运行命令，属于阅读理解型任务，重在建立准确认知。

#### 4.1.5 小练习与答案

**练习 1**：把缩写 ATVOSS 还原成完整英文，并说明其中哪个词决定了它「只服务 Vector 类算子」？

> **参考答案**：Ascend C Templates for **Vector** Operator Subroutines。其中 **Vector** 一词限定它面向 Vector 计算单元擅长的逐元素类算子，而不是 Matrix（矩阵乘）类算子。

**练习 2**：判断对错并说明理由——「用 ATVOSS 写算子，性能会比直接写 Ascend C 差，因为多了一层封装」。

> **参考答案**：错。ATVOSS 采用表达式模板，在**编译期**构建抽象语法树，属于零运行时开销的封装；且最底层仍调用 Ascend C 基础 API，性能上限与手写 Ascend C 一致。它省的是「开发成本」，不是「用性能换便利」。

### 4.2 Vector 算子与 Ascend C 的关系

#### 4.2.1 概念说明

要理解 ATVOSS 为何有价值，先要理解直接用 Ascend C 写一个 Vector 算子有多繁琐。

一个跑在昇腾 AI Core 上的算子，从数据角度看要经历这样一趟旅程：

1. 数据一开始在 **GM（Global Memory，全局显存）** 里，容量大但慢。
2. 要把它**搬运（Copy）**到 AI Core 内部的 **UB（Unified Buffer，统一缓冲区）**，容量小但快。
3. 在 UB 里用 **Vector 计算单元**做逐元素运算。
4. 把结果**搬回 GM**。

这趟旅程里，开发者必须手动决定：

- **多核切分**：昇腾一颗芯片有多个 AI Core，这一大块任务怎么分给各个核？
- **Tiling 分块**：UB 装不下整块数据，要切成多小的 Tile？切几块？
- **流水同步**：搬运（MTE2/MTE3 通道）和计算（V 通道）是不同硬件流水线，如何让它们重叠执行（double buffer）以隐藏延迟？
- **边界处理**：总元素数不能被 Tile 大小整除时，尾巴那块怎么处理？

这些全是「和计算逻辑无关」的工程负担。ATVOSS 的分层架构正是为了把这些负担逐层吸收掉。

#### 4.2.2 核心流程

ATVOSS 把上面这些职责拆进**五层架构**，每层只管一件事，抽象程度从高到低递减：

| 层级 | 职责 | 解决的具体问题 |
|------|------|----------------|
| **Device 层** | Host 侧调用总入口 | ACL 资源管理、Host↔Device 数据搬运、Kernel 调用 |
| **Kernel 层** | 多核任务分解 | 把任务分给多个 AI Core |
| **Block 层** | 单核任务分解 | 把单核任务切成多个 Tile，编排流水 |
| **Tile 层** | Ascend C 封装 | 封装 DataCopy、Add、Sqrt、ReduceSum 等基础操作 |
| **Basic 层** | 基础操作 | 直接调用 Ascend C 基础 API |

用一个比喻：你要送一批快递（计算任务）。
- **Device 层**是「调度中心」，负责接收订单、分配车辆。
- **Kernel 层**是「车队队长」，把货分给几辆车（几个核）。
- **Block 层**是「单辆车的司机」，把货装成小件（Tile）一趟趟送，安排装卸重叠。
- **Tile 层**是「标准化的搬运工具」，提供现成的叉车（DataCopy）和打包机（计算）。
- **Basic 层**是「最底层的螺丝钉」，就是 Ascend C 本身。

作为算子开发者，你**主要和最上面的表达式层打交道**，描述「算什么」；下面四层（Kernel/Block/Tile/Basic）由 ATVOSS 框架自动驱动。这五层的内部实现是进阶篇（U2）和专家篇（U3）的内容，本讲只需记住「分层是为了让你专注计算逻辑」。

#### 4.2.3 源码精读

**五层架构总表**——summary 用一张表浓缩了五层职责：

[docs/summary.md:22-28](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/summary.md#L22-L28)：列出 Device/Kernel/Block/Tile/Basic 五层各自的职责与主要功能。这是理解整个 ATVOSS 的「总纲」，后续每一篇讲义几乎都在展开这张表的某一行。

**分层的价值判断**——紧接总表后的一句结论：

[docs/summary.md:32](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/summary.md#L32)：明确指出这种分层架构使得开发者可以专注于计算逻辑描述，而无需关注底层硬件细节和并行调度策略。这一句是 ATVOSS 设计哲学的概括。

**极简编程的实证**——summary 给出的 RMSNorm 算子示例，展示「声明式描述」长什么样：

[docs/summary.md:78-101](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/summary.md#L78-L101)：展示实现 RMSNorm 算子时，`RmsNormCompute` 内只需用 `PlaceHolder` 声明输入输出，再用 `ReduceSum`、`Broadcast`、`Sqrt`、`Divs` 串成一条表达式 `out = in2 * (in1 / Sqrt(Divs<WIDTH>(Broadcast(ReduceSum(in1*in1)))))`。**注意**：这里没有出现任何 Tiling、多核切分、DataCopy 的代码——这些都被框架隐藏了。这就是「极简编程」的具体含义。（关于这段表达式的精确语法，会在 U2 表达式系统、U3 rms_norm 样例讲义中逐符号拆解，本讲只需感受「短」。）

#### 4.2.4 代码实践

**实践类型：源码阅读型实践（代码对比）**

1. **实践目标**：直观感受「直接写 Ascend C」和「写 ATVOSS 表达式」在代码量上的差距。
2. **操作步骤**：
   - 阅读 `docs/summary.md` 第 78–101 行的 RMSNorm 表达式实现，数一数核心计算逻辑大约几行。
   - 打开 `examples/rms_norm/rms_norm.cpp`，找到 `Compute()` 函数体，确认它和文档里的写法一致。
   - 思考：如果用原生 Ascend C 实现同样的 RMSNorm，你需要额外手写哪些部分？（提示：分块搬运、ReduceSum 的同步、多核切分、double buffer。）
3. **需要观察的现象**：`rms_norm.cpp` 里 `Compute()` 的主体几乎只有「输入输出占位 + 一条计算表达式」，没有显式的内存搬运和调度代码。
4. **预期结果**：你能列出至少 3 项「ATVOSS 帮你省掉的工作」，例如：（1）手动 Tiling 分块；（2）GM↔UB 数据搬运代码；（3）多核任务切分；（4）流水线/双缓冲编排。
5. **待本地验证**：若要量化对比，可在具备 CANN 环境时用第 2 讲的方法编译原生 Ascend C 版本与 ATVOSS 版本，比较源码行数。本讲暂不要求运行。

#### 4.2.5 小练习与答案

**练习 1**：五层架构中，哪一层负责「把单核任务切成多个 Tile 块」？哪一层负责「把任务分给多个 AI Core」？

> **参考答案**：**Block 层**负责把单核任务切成多个 Tile 块并编排流水；**Kernel 层**负责多核间任务分解、把任务分给多个 AI Core。

**练习 2**：为什么说 ATVOSS 的 Basic 层「保证了灵活性和性能上限」？

> **参考答案**：因为 Basic 层直接使用 Ascend C 基础 API，是整个架构的底层支撑。ATVOSS 的所有高层抽象最终都编译成 Basic 层的 Ascend C 调用，所以它的能力边界和性能上限完全由 Ascend C 决定；当高层封装不够用时，理论上可以下沉到 Basic 层。开发者通常不直接写这一层，但它是一切的地基。

### 4.3 适用场景与软硬件要求

#### 4.3.1 概念说明

知道 ATVOSS「是什么、为什么」之后，还要知道「它适合干什么、在什么环境能干」。这部分信息直接来自 `docs/summary.md` 的「适用场景」一节，是动手前必须核对的清单。

#### 4.3.2 核心流程

适用性可以从三个维度判断：

1. **算法维度**：你的算子是不是「逐元素计算」为主？ATVOSS 专门服务 Vector 类算子（数学运算、类型转换、激活函数等），并通过归约（ReduceSum）和广播（Broadcast）扩展到 RMSNorm 这类需要沿某一轴汇总再还原的场景。如果你的算子核心是矩阵乘（GEMM）这类非逐元素计算，那它**不在** ATVOSS 的主战场。
2. **开发模式维度**：你是在做快速原型验证，还是要把算子产品化？ATVOSS 的声明式写法特别适合快速原型开发。
3. **环境维度**：你的硬件、CANN 版本、编译器是否满足最低要求。

#### 4.3.3 源码精读

**适用场景**——summary 明确列出的两类用途：

[docs/summary.md:122-124](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/summary.md#L122-L124)：指出 ATVOSS 适用于（1）需要在昇腾硬件上开发**逐元素计算**的 Vector 类算子（数学运算、类型转换、激活函数等）；（2）需要快速实现算子原型并验证的快速原型开发场景。其中「逐元素计算」是关键词，划定了 ATVOSS 的主战场。

**硬件支持**——目前支持的芯片型号：

[docs/summary.md:126-127](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/summary.md#L126-L127)：当前支持 **Ascend 950PR** 与 **Ascend 950DT** 两款型号。注意 README 的目录结构里样例路径（如 `examples/python_extension/csrc/abs/ascend950/`）也对应这个 `ascend950` 系列。

**软件依赖**——最低版本门槛：

[docs/summary.md:129-133](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/summary.md#L129-L133)：列出软件依赖为 **CANN 8.5.0 及以上**、GCC 7.3.0 及以上、CMake 3.16.0 及以上、Python 3.7.0 及以上（建议不超过 3.10）。其中 CANN 8.5.0 是最关键的一条——它是能否使用 ATVOSS 的硬门槛。

**系统要求**——操作系统与架构：

[docs/summary.md:135-138](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/summary.md#L135-L138)：要求主流 Linux 发行版（如 Ubuntu、CentOS），支持 x86_64 与 aarch64 架构，并需安装对应型号的 NPU 驱动和固件。

> 小提示：summary 末尾 [docs/summary.md:140](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/summary.md#L140) 提到「ATVOSS 持续更新以支持更多昇腾硬件型号」，所以支持的芯片列表将来可能扩展，实际动手前请以仓库最新说明为准。

#### 4.3.4 代码实践

**实践类型：阅读 + 自查清单实践（本讲的核心实践任务）**

1. **实践目标**：完成「ATVOSS 相比直接用 Ascend C 写算子节省了什么」的总结，并整理出支持芯片与最低 CANN 版本的清单，确认自己的环境是否达标。
2. **操作步骤**：
   - 重读 `README.md` 第 6 行与 `docs/summary.md` 第 16、22–28、122–124 行。
   - 用**一段话**写下：相比直接用 Ascend C，ATVOSS 帮开发者省去了哪些工作。
   - 列出 ATVOSS 支持的目标芯片型号，以及最低 CANN 版本。
   - 检查你本机环境：CANN 版本是否 ≥ 8.5.0？GCC、CMake、Python 是否达标？（可在终端用 `gcc --version`、`cmake --version`、`python3 --version` 查看。）
3. **需要观察的现象**：你会发现自己能否使用 ATVOSS，主要取决于 CANN 版本和芯片型号这两条。
4. **预期结果**：
   - **总结示例**：「ATVOSS 通过分层架构和表达式模板，把多核切分、Tiling 分块、GM↔UB 数据搬运、流水同步等硬件调度细节封装在 Device/Kernel/Block/Tile/Basic 五层之中，让开发者只需用声明式表达式描述逐元素计算逻辑，从而把精力从『怎么调度硬件』转移到『算什么』上。」
   - **清单示例**：支持芯片 = Ascend 950PR / Ascend 950DT；最低 CANN = 8.5.0。
5. **待本地验证**：若你当前没有昇腾环境或 CANN 版本低于 8.5.0，本项的「环境检查」部分会显示不达标——这正常，记录下来即可，真正编译运行留到第 2 讲在有环境时进行。

#### 4.3.5 小练习与答案

**练习 1**：ATVOSS 官方文档列出的最低 CANN 版本是多少？为什么说它是「硬门槛」？

> **参考答案**：最低 **CANN 8.5.0**。说它是硬门槛，是因为 ATVOSS 的 Basic 层依赖 Ascend C 基础 API，而这些 API 的能力随 CANN 版本提供；版本不够，对应的底层接口可能不存在或行为不一致，ATVOSS 无法正常编译运行。

**练习 2**：下面三个算子，哪些更适合用 ATVOSS 开发？为什么：（a）逐元素取绝对值；（b）大型矩阵乘 GEMM；（c）逐元素 `a*b + c` 的融合运算。

> **参考答案**：（a）和（c）适合，（b）不适合。ATVOSS 专攻 Vector 类的**逐元素**计算；（a）是典型的逐元素运算，（c）是多个逐元素运算的融合算子，正是 ATVOSS 的强项。而（b）矩阵乘属于 Matrix 类计算，不是逐元素运算，不在 ATVOSS 的主战场。

## 5. 综合实践

**综合任务：制作一份「ATVOSS 一页速览」**

把本讲三个模块的知识串成一张可以用作团队分享的「一页速览」，要求包含四个区块：

1. **一句话定位**：用 `README.md:6` 的信息写出 ATVOSS 的标准定义。
2. **架构示意图**：仿照本讲 4.1.2 的流程图，画出「表达式层 → Tiling/调度 → Ascend C → 昇腾芯片」的分层，并标注 ATVOSS 帮你封装了哪几层（参考 `docs/summary.md:22-28` 的五层表）。
3. **能力边界**：用两句话写清「擅长什么（逐元素/融合 Vector 算子）」和「不擅长什么（如矩阵乘）」（参考 `docs/summary.md:122-124`）。
4. **环境门槛**：用表格列出支持芯片、最低 CANN 版本、编译器/构建工具/Python/操作系统的要求（参考 `docs/summary.md:126-138`）。

完成后再回头看你 4.3.4 里写的「省去了什么」那段话，确认它能自然落在第 2 区块里。这份速览也将成为你后续学习每篇讲义时的「定位罗盘」——每学一层，就回头给这张图补一个细节。

> 说明：本综合实践是阅读与归纳型任务，无需运行代码。若想在有环境后进一步动手，可进入第 2 讲「目录结构与构建运行」，亲手编译第一个 `abs` 样例。

## 6. 本讲小结

- **ATVOSS 的定位**：Ascend C Templates for Vector Operator Subroutines，基于 Ascend C 的 Vector 算子模板库，服务昇腾硬件上的 Vector 类融合算子。
- **核心价值**：通过封装 Ascend C 底层 API 和复杂的 Tiling 计算，让开发者用声明式表达式描述计算逻辑，把多核切分、Tile 分块、内存搬运、流水同步等硬件调度细节交给框架。
- **五层架构**：Device（Host 入口）> Kernel（多核）> Block（单核 Tile）> Tile（Ascend C 封装）> Basic（Ascend C 基础 API），分层让开发者专注计算逻辑。
- **三大特性**：极简编程（表达式模板）、高效性能（编译期优化、零运行时开销）、高扩展性（模板可自由组合扩展）。
- **能力边界**：擅长逐元素计算与融合 Vector 算子（含 Reduce/Broadcast 扩展），不主攻矩阵乘等非逐元素计算。
- **环境门槛**：支持 Ascend 950PR / 950DT；最低 CANN 8.5.0；需 GCC 7.3+/CMake 3.16+/Python 3.7~3.10，运行于主流 Linux（x86_64/aarch64）并装好 NPU 驱动固件。

## 7. 下一步学习建议

本讲建立了「ATVOSS 是什么、为什么、能在哪用」的认知。下一步建议：

1. **第 2 讲《目录结构与构建运行》**（`u1-l2-directory-and-build.md`）：把仓库目录逐个拆开看懂，并学会用 `scripts/build.sh` 编译并运行第一个 `abs` 样例——这是把「纸上认知」变成「能跑起来」的关键一步。**强烈建议作为下一篇学习。**
2. **第 3 讲《五层架构总览》**（`u1-l3-five-layer-architecture.md`）：把本讲只是点到为止的五层架构展开，配合 `atvoss.h` 主入口头文件，看清每层对应哪些头文件。
3. 在阅读后续讲义前，可以先把 `examples/abs/abs.cpp`（最短样例）和 `examples/abs/README.md` 浏览一遍，对「一个 ATVOSS 算子长什么样」有个直观印象，为第 4 讲《从 abs 样例看用户编程模型》做准备。
