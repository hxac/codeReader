# PTO Tile Library 项目总览：是什么、为什么、为谁服务

## 1. 本讲目标

学完本讲，你应该能够：

- 用自己的话说出 PTO（Parallel Tile Operation）虚拟 ISA 是什么、它要解决什么问题。
- 说出仓库支持的平台矩阵（A2 / A3 / A5 / Kirin / CPU 仿真）以及各平台对应的实现目录。
- 说出仓库顶层目录的职责划分，知道去哪里找指令实现、算子示例和文档。
- 了解 PyPTO、TileLang Ascend、PTOAS、pto-dsl 等上下游生态项目分别扮演什么角色。

本讲是整本学习手册的第一篇，不要求你写任何代码，重点是建立"地图感"——后续每一讲都会频繁引用本讲建立的目录结构和平台概念。

## 2. 前置知识

本讲面向零基础读者，但有几个术语提前解释一下，读起来会更顺：

- **ISA（Instruction Set Architecture，指令集架构）**：硬件向软件暴露的"指令合同"。软件按 ISA 写指令，硬件负责执行。常见的例子有 x86、ARM。
- **虚拟 ISA**：不直接对应某一款物理硬件，而是一层稳定的中间指令抽象。上层代码面向虚拟 ISA 编程，再由后端映射到不同代际的真实硬件。它的价值在于"写一次，多代硬件都能跑"。
- **Tile（块/瓦片）**：一小块固定形状的二维数据（例如 128×128 的 float16 矩阵），是 PTO 中数据搬运和计算的基本单位。可以类比为"把大矩阵切成的小瓷砖"。
- **Ascend（昇腾）**：华为的 AI 加速硬件系列。A2（910B）、A3（910C）、A5（950）是不同代际的芯片。
- **CANN**：昇腾的计算架构生态（驱动、编译器、工具链的总称），PTO 由 Ascend CANN 定义。
- **CPU 仿真（CPU-SIM）**：用普通电脑的 CPU 模拟指令行为，让你在没有昇腾硬件的情况下也能验证算子逻辑正确性。

## 3. 本讲源码地图

本讲涉及的关键文件都是文档型入口文件：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/README.md) | 项目门面：定位、特性、快速上手、目录结构、路线图 |
| [docs/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/README.md) | 文档导航入口：推荐阅读路径、文档分类索引 |
| [include/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/README.md) | 头文件目录说明 + 每条指令在各后端的实现状态总表 |
| [docs/isa/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/README.md) | PTO 指令列表，按类别组织（本讲做分类练习时要用） |

## 4. 核心概念与源码讲解

### 4.1 项目定位

#### 4.1.1 概念说明

PTO 全称 **Parallel Tile Operation**，是由 Ascend CANN 定义的**面向 tile 编程的虚拟 ISA**。这个仓库提供的内容包括：PTO Tile 指令的实现（头文件）、示例算子、测试和文档。

它解决的核心问题是**跨代迁移**：昇腾硬件不断迭代（A2 → A3 → A5 → ……），每一代的底层实现细节都有差异。如果算子开发者直接面向某一代硬件的底层接口写代码，换代时就要大面积重写。PTO 用一个更高层的 tile 编程模型把这些差异"桥接"起来。

注意 README 里一句很关键的话——PTO 的目标**不是隐藏底层能力，而是在保留性能调优空间的同时抬高抽象层级**。也就是说，它保证固定 tile 形状下的行为正确性，同时把 tile 大小、tile 形状、指令排序这些调优维度留给开发者。这决定了 PTO 的用户是"想榨性能的人"，而不仅仅是"想跑通的人"。

#### 4.1.2 核心流程

从项目自身演进的视角（News 栏目）看 PTO 的能力扩张路径：

```text
2025-12-27  开源
2026-01-30  + 规约指令、MX 指令
2026-02-28  + 卷积指令、量化指令、kernel 间通信指令
2026-03-30  + A5 支持、异步通信指令、CostModel 性能模拟
```

可以概括为：先有计算与搬运基础，再补规约/量化等算力指令，然后扩展卷积等专用通路，最后加上通信指令集与性能建模——一个"从计算到通信、从功能到性能"的完整化过程。

#### 4.1.3 源码精读

项目定位的官方表述在 README 的 Project Positioning 一节：

- [README.md:21-28](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/README.md#L21-L28)：PTO ISA 构建在昇腾底层软硬件抽象之上，定义了 90 多条标准 tile 指令，用更高层的 tile 编程模型桥接代际差异；并给出四条定位——统一跨代 tile 抽象、平衡可移植性与性能、服务框架/算子/工具链、持续可扩展。

除了计算与搬运指令，PTO 还提供**通信扩展指令集**：

- [README.md:30-32](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/README.md#L30-L32)：说明通信指令集覆盖 NPU 间数据搬运与同步，分为点对点通信、信号同步、集合通信三类，且与计算指令遵循同样的 tile 级抽象，可驱动多个数据搬运硬件引擎。

目标用户在 Intended Audience 一节：

- [README.md:49-55](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/README.md#L49-L55)：三类人——直接对接昇腾硬件的框架/编译器后端开发者、需要跨平台迁移复用实现的高性能算子开发者、需要显式控制 tile/缓冲/流水线的性能工程师。

#### 4.1.4 代码实践

**实践目标**：把"定位"从一段英文描述变成你自己的判断。

**操作步骤**：

1. 通读 [README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/README.md) 的 Project Positioning、Core Features、Intended Audience 三节。
2. 通读 [docs/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/README.md) 的 Recommended Reading Path。
3. 写下三句话回答："如果我是一个从 A3 迁移到 A5 的算子工程师，PTO 替我省掉了什么？我仍然要自己关心什么？"

**需要观察的现象**：你会注意到定位里反复出现 "not to hide low-level capabilities" 和 "preserving room for performance tuning" 的措辞——省掉的是"每代硬件不同的搬运/计算细节"，仍要关心的是 tile 尺寸、形状与指令排序。

**预期结果**：形成一段 100 字左右的心得，后续单元会不断验证它。

**待本地验证**（本实践为阅读理解型，无需运行命令）。

#### 4.1.5 小练习与答案

**练习 1**：PTO 是"虚拟 ISA"，它虚拟的是什么？

**参考答案**：虚拟的是指令合同——上层代码面向 PTO 的 90+ 条标准 tile 指令编程，这些指令不直接绑定某一代昇腾硬件，而是由各后端（CPU 仿真、A2/A3、A5、Kirin）分别映射到自己的底层实现。

**练习 2**：PTO 抬高抽象层级的代价是什么？它如何把这个代价降到最低？

**参考答案**：代价是可能损失底层控制自由度。PTO 的做法是"不隐藏底层能力"：固定 tile 形状下保证行为正确，同时把 tile 大小、tile 形状、指令排序留给开发者调优。

**练习 3**：PTO 的通信指令集和计算指令集是什么关系？

**参考答案**：通信指令集是 PTO ISA 的扩展，与计算指令遵循相同的 tile 级抽象和跨平台设计，可驱动多个数据搬运硬件引擎，用于构建计算-通信深度融合的算子。

### 4.2 平台矩阵

#### 4.2.1 概念说明

"平台矩阵"指的是：同一条 PTO 指令，在仓库里有哪些后端实现。这是理解整个仓库目录组织的钥匙。

仓库目前涉及六个后端概念：

| 后端 | 对应目录 | 说明 |
| --- | --- | --- |
| CPU 仿真（`__CPU_SIM`） | `include/pto/cpu/` | 在 x86_64 / AArch64 上模拟指令功能，用于无硬件开发调试 |
| CostModel（`__COSTMODEL`） | `include/pto/costmodel/` | A2/A3 性能造价模型（stub / fit 两条路径） |
| A2（910B）/ A3（910C） | `include/pto/npu/a2a3/` | 两代硬件共用一套实现 |
| A5（950） | `include/pto/npu/a5/` | 新一代实现，含 MX 指令等新特性 |
| Kirin | `include/pto/npu/kirin9030/` | Kirin 平台实现 |
| 通信指令 | `include/pto/comm/` | NPU 间通信的指令实现 |

另外 `include/pto/common/` 存放各后端共享的类型与公共定义。这些目录的内部细节属于下一讲（u1-l2）的内容，这里只需记住"一个指令 × 多个后端"的矩阵结构。

#### 4.2.2 核心流程

开发者写一份 kernel 代码，编译期通过宏选择后端：

```text
            一份 kernel 代码（#include <pto/pto-inst.hpp>）
                        │
        ┌───────────────┼────────────────┐
   __CPU_SIM        __CCE_AICORE__    __COSTMODEL
        │               │                │
   CPU 功能仿真      NPU 真机实现     性能模拟
   （先验证逻辑）  （A2/A3/A5/Kirin） （估周期）
```

推荐工作流（README 的 Recommended Learning Path）正是沿着这条链：先在 CPU 仿真上验证指令语义与结果 → 再移植到昇腾硬件验证正确性、收集性能数据 → 最后定位瓶颈（CUBE Bound / MTE Bound / Vector Bound）做调优。

#### 4.2.3 源码精读

- [README.md:168-175](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/README.md#L168-L175)：平台支持清单——Ascend A2（910B）、A3（910C）、A5（950）、CPU（x86_64 / AArch64），并指向 include/README.md 查看详情。
- [include/README.md:24-33](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/README.md#L24-L33)：解释实现状态表各列的含义——`__CPU_SIM` 是 CPU 仿真后端；Costmodel 含 stub / fit 两条路径；A2 与 A3 共用 `include/pto/npu/a2a3/`；A5 用 `include/pto/npu/a5/`；Kirin 用 `include/pto/npu/kirin9030/`。
- [include/README.md:34-45](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/README.md#L34-L45)：逐指令矩阵的表头与开头几行。例如 `TADD` 一行是 CPU: Yes / Costmodel: Yes / A2: Yes / A3: Yes / A5: Yes / Kirin: Yes——全平台支持的"模范指令"；而 `TMATMUL_MX` 只在 CPU、A5、Kirin 有实现，`TADDC` 只有 CPU 实现。这张表是后续查任何指令支持情况的第一入口。
- [include/README.md:181-186](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/README.md#L181-L186)：状态图例——`Yes` 有实现、`TODO` 已在公共 API/文档面上但该后端尚未实现、`No` 明确不支持。

仓库顶层目录职责（后面单元会逐个深入）：

- [README.md:198-218](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/README.md#L198-L218)：`include/`（公共头文件与各后端实现）、`kernels/`（manual 手工优化算子 + custom 自定义算子）、`docs/`（ISA 参考 / 编程模型 / 汇编 / 文档站源码）、`demos/`（Auto Mode / baseline / torch_jit 示例）、`tests/`（CPU/NPU 测试与脚本）、`scripts/`、`cmake/`、`build.sh`、`CMakeLists.txt`。

#### 4.2.4 代码实践

**实践目标**：亲手从平台矩阵里读出"指令 × 后端"的支持差异。

**操作步骤**：

1. 打开 [include/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/README.md) 的实现状态表。
2. 分别找出：一条全平台支持的指令（提示：`TADD`）、一条只有 CPU 支持的指令（提示：`TADDC`）、一条只在 A5/Kirin 支持的 NPU 指令（提示：`TMATMUL_MX`）。
3. 统计 CPU 列中 `Yes` 的条目数量级，与"90+ 标准指令"的说法互相印证。
4. 在本地克隆中运行 `ls include/pto/npu/`，确认 `a2a3`、`a5`、`kirin9030` 三个子目录真实存在。

**需要观察的现象**：不同指令的后端覆盖差异很大；CPU 仿真覆盖最广，是因为它是功能验证的基础设施。

**预期结果**：得到 3 条指令的平台支持记录，并确认目录结构与文档描述一致。`ls` 命令的输出需在本地环境执行后确认（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 A2 和 A3 两列的状态总是完全一样？

**参考答案**：因为 A2（910B）和 A3（910C）目前共用同一套实现目录 `include/pto/npu/a2a3/`，实现相同，状态自然相同。

**练习 2**：`TMATMUL_MX` 在矩阵中 CPU 是 Yes、A2/A3 是 TODO、A5 是 Yes，这说明什么？

**参考答案**：说明 MX（microscaling 混合精度）矩阵乘是 A5 一代引入的新能力，CPU 仿真已同步支持它做功能验证，而旧代硬件 A2/A3 尚无实现。这正体现了"新特性先落 CPU 仿真，再随新硬件落地"的模式。

**练习 3**：如果你没有昇腾硬件，想验证一个 PTO 算子的计算结果对不对，应该走哪条路径？

**参考答案**：走 `__CPU_SIM` 路径，用 `python3 tests/run_cpu.py` 在 x86_64 / AArch64 的普通电脑上跑 CPU 功能仿真（详见 u1-l3 讲）。

### 4.3 生态集成

#### 4.3.1 概念说明

PTO 不是一座孤岛，它是 Ascend CANN 生态中的"公共接口层"。README 明确说它 serving as a common interface for upper-layer frameworks, operator implementations, and compiler toolchains（作为上层框架、算子实现与编译器工具链的公共接口）。

围绕 PTO 已经形成的上下游项目：

| 项目 | 角色 |
| --- | --- |
| [PyPTO](https://gitcode.com/cann/pypto/) | PTO 生态的上层编程框架 |
| [TileLang Ascend](https://github.com/tile-ai/tilelang-ascend/) | 已集成 PTO 指令的 tile 级编程框架 |
| [PTOAS](https://github.com/PTO-ISA/PTOAS/) | PTO 汇编器与编译器后端 |
| [pto-dsl](https://github.com/PTO-ISA/pto-dsl/) | Pythonic 前端与 JIT 工作流探索 |

Roadmap 中还能看到生态的未来方向：BiSheng 编译器支持的 Auto Mode（自动 tile 缓冲分配与同步插入）、Tile Fusion（自动融合）、PTO-AS 字节码等。

#### 4.3.2 核心流程

本仓库在生态中的位置可以画成：

```text
   PyPTO / TileLang Ascend / pto-dsl        ← 上层框架与前端
                │  发射 PTO 指令
                ▼
        PTO Tile Library（本仓库）           ← 指令定义 + 多后端实现
                │  编译期路由（pto-inst.hpp）
    ┌───────────┼───────────┐
    ▼           ▼           ▼
  CPU-SIM    A2/A3/A5     CostModel        ← 执行/模拟目标
  （功能）   （真机）     （性能预估）
```

#### 4.3.3 源码精读

- [README.md:34-38](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/README.md#L34-L38)：列出已集成 PTO 指令的框架——PyPTO、TileLang Ascend，并注明更多语言和前端支持在持续完善。
- [README.md:177-192](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/README.md#L177-L192)：Roadmap 表格——Auto Mode / Tile Fusion（BiSheng 编译器）、PTO-AS 字节码、卷积扩展、集合通信扩展、系统调度扩展、微指令、基础指令增强、CostModel 与 CPU-SIM 的持续演进。
- [README.md:220-229](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/README.md#L220-L229)：Related Information——PyPTO、PTOAS、pto-dsl 的仓库链接，以及贡献指南、安全披露、Release Notes。
- [docs/README.md:16-25](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/README.md#L16-L25)：官方推荐阅读路径——Getting Started（先跑通 CPU 仿真）→ ISA Overview（建立整体认识）→ 指令列表 → Tile 编程模型 → 事件与同步 → 性能优化。本学习手册的整体编排与这条路径一致。
- [include/README.md:7-13](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/README.md#L7-L13)：上层框架/算子代码的接入方式——只需 `#include <pto/pto-inst.hpp>`，由该统一入口头按构建配置选择 CPU 仿真或 NPU 后端。这是生态项目消费本仓库的最小契约。

#### 4.3.4 代码实践

**实践目标**：完成本讲的综合热身——整理 PTO 与传统 Ascend C 编程模型的差异，并给 90+ 指令画分类草图（这也是本讲规格中指定的实践任务）。

**操作步骤**：

1. 重读 [README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/README.md) 的 Project Positioning 与 Core Features 两节（[README.md:21-47](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/README.md#L21-L47)）。
2. 打开 [docs/isa/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/README.md)，浏览它的分类标题（先不要看具体指令）。
3. 在不看答案的情况下，自己先按直觉给指令分类（例如：搬运类、计算类、同步类……）。
4. 对照 docs/isa/README.md 的实际分类修正你的草图。实际的分类包括：Synchronization、Manual / Resource Binding、Elementwise (Tile-Tile)、Tile-Scalar / Tile-Immediate、Axis Reduce / Expand、Memory (GM <-> Tile)、Matrix Multiply、Data Movement / Layout、Complex、Cross-core Communication、Communication。
5. 写出 PTO 与传统 Ascend C 编程模型的三点差异。可参考的角度：① tile 级虚拟指令 vs 直接操作硬件缓冲；② 跨代可移植性从何而来；③ CPU 仿真先行的工作流。

**需要观察的现象**：你的直觉分类与官方分类的重合度——尤其注意官方把"通信"单列成扩展指令集、把"规约/扩展（Reduce/Expand）"单列为一族，这些是传统 CPU 编程里不会出现的类别。

**预期结果**：产出两份笔记——三点差异列表 + 一张指令分类草图（手绘或 Markdown 列表均可）。本实践为阅读理解型，无需运行命令（**待本地验证**的只是你的分类是否与官方索引一致）。

#### 4.3.5 小练习与答案

**练习 1**：上层框架要使用 PTO 指令，最小代码改动是什么？

**参考答案**：`#include <pto/pto-inst.hpp>`。这个统一入口头会根据构建配置（如 `__CPU_SIM`、`__CCE_AICORE__`）自动选择 CPU 仿真或 NPU 后端实现。

**练习 2**：PyPTO 和 PTOAS 分别在生态的哪一层？

**参考答案**：PyPTO 是 PTO 生态的上层编程框架（在 PTO 之上）；PTOAS 是 PTO 的汇编器和编译器后端（把 PTO 工作流向下游编译）。

**练习 3**：官方推荐的新手阅读路径的第一步是什么？为什么？

**参考答案**：第一步是 Getting Started——搭建环境并先跑通 CPU 仿真。因为没有硬件也能验证逻辑，先用仿真建立对指令语义和结果的直觉，是成本最低的学习方式。

## 5. 综合实践

**任务**：为你的团队写一页《PTO 入门导读》。

要求这一页纸包含：

1. **一段定位**（≤80 字）：说清 PTO 是什么、解决什么问题。
2. **一张平台矩阵表**：从 [include/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/README.md) 的实现状态表中挑 5 条有代表性的指令（覆盖全平台型、CPU-only 型、A5 新特性型），填出它们在 CPU / A2 / A3 / A5 的支持状态。
3. **一张目录地图**：照着 [README.md:198-218](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/README.md#L198-L218) 的目录结构，标注"想看指令实现去哪、想看算子示例去哪、想跑测试去哪"。
4. **一条学习路径**：结合 [docs/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/README.md) 的推荐阅读路径和本手册后续单元，规划你未来两周的学习顺序。

检验标准：把这一页纸给一个没接触过 PTO 的同事看，对方能在 5 分钟内说出 PTO 是干嘛的、去哪里找指令实现——就算合格。

## 6. 本讲小结

- PTO（Parallel Tile Operation）是 Ascend CANN 定义的 tile 级虚拟 ISA，用 90+ 条标准指令桥接不同代际昇腾硬件的实现差异，核心价值是降低跨代迁移成本。
- 它"抬高抽象但不隐藏底层"：固定 tile 形状下保证行为正确，tile 大小、形状、指令排序仍是留给开发者的调优维度。
- 平台矩阵是仓库组织的钥匙：CPU 仿真（`__CPU_SIM`）、CostModel（`__COSTMODEL`）、A2/A3（共用 `npu/a2a3/`）、A5（`npu/a5/`）、Kirin（`npu/kirin9030/`）各是一列，逐指令支持状态见 [include/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/README.md)。
- 除计算与搬运指令外，PTO 还有通信扩展指令集（点对点、信号同步、集合通信），与计算指令同用 tile 级抽象。
- 生态上下游：PyPTO（上层框架）、TileLang Ascend（已集成框架）、PTOAS（汇编器/编译器后端）、pto-dsl（Pythonic 前端）；上层接入只需包含 `pto/pto-inst.hpp`。
- 推荐工作流是"CPU 仿真验证逻辑 → 真机验证正确性并测性能 → 定位 CUBE/MTE/Vector Bound 调优"。

## 7. 下一步学习建议

下一讲（u1-l2《源码目录结构导览》）将带你深入 [include/pto/pto-inst.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/pto-inst.hpp) 和 [CMakeLists.txt](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/CMakeLists.txt)，弄清 `common/cpu/npu/comm/costmodel` 各层的分工与统一入口的路由机制。

在此之前，建议你先自行浏览：

- [docs/isa/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/README.md)：按类别浏览指令列表，为分类草图补全细节。
- [include/pto/npu/a2a3/](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/)：扫一眼指令头文件的命名，感受"一条指令一个头文件"的组织方式。
