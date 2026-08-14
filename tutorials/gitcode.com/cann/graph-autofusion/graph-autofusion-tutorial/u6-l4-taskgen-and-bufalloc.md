# 调度任务生成与内存分配

## 1. 本讲目标

本讲是 Autofuse `optimize` 流水线的「中段收尾」：在 u6-l1 讲清了 `Optimizer` 的总编排、u6-l3 讲清了 `AutoSchedule` 如何生成候选调度图之后，本讲回答两个紧接着的问题——

> 把一张融合图喂给调度器之前，它先要被「切」成若干个独立的调度任务（ScheduleTask）；切完、调度完之后，这些图还要被分配好片上内存（UB 的 queue 与 buffer）才能交给 codegen 生成内核代码。

学完本讲，你应当能够：

- 说清 `ScheduleTaskGenerator` 的真实职责：它是一个**门面（facade）**，真正干活的是按芯片平台分发的 `GenerateTasks`。
- 复述平台 `PlatformV1` 的任务生成顺序（Split → Concat → Transpose → Reduce → Recompute 兜底），并解释为什么是固定顺序。
- 对比 reduce、transpose、concat 三类算子场景在「任务生成」上的根本差异：reduce 靠插入 workspace 边界切图、transpose 靠推到 load 上消除、concat 靠转 Store 或分组。
- 解释 `BufQueAllocator` 如何用「生命周期复用 + queue 数量上限」在 UB 上分配 `TQue`/`TBuf`，并理解它消费了 u5-l2 中 `reg_func` 产出的 `TmpBufDesc`。

## 2. 前置知识

在进入源码前，先建立四点直觉。若以下概念陌生，建议先回看对应讲义。

1. **ASCIR 视图与同一份图数据**（u4-l2 / u5-l1）。本讲出现的 `ascir::HintGraph`、`ascir::ImplGraph`、`ascir::NodeView` 都是搭在核心 `ComputeGraph/Node` 之上的「带调度语义的视图」。全链路只有一份图数据，任务生成器只是在这份图上增删节点、重连边，并不会另造一张图。

2. **融合子图里的 Load/Store/Workspace 三类节点**。Autofuse 把全局内存（GM/HBM）与片上缓冲（UB）之间的搬运显式化：`Load` = GM→UB 的搬入，`Store` = UB→GM 的搬出，`Workspace` = 一段「临时全局内存」，用来在两个无法直接连成一片 UB 流水线的计算片段之间中转数据（先 Store 落到 workspace，再 Load 读回来）。本讲会反复看到这三类节点如何被插入以「切开」融合图。

3. **候选 + 打分 的两段式设计**（u6-l3）。优化器不在任务生成阶段决定「哪个模板最好」，而是**生成多个候选模板（ScheduleTask），每个附一段 `score_func`（一段 C++ 打分函数字符串），把最终选择权交给下游 ATT 的成本模型**。理解这一点，才能看懂为什么 transpose/concat 要「同时生成保留和消除两套模板」。

4. **硬件 UB 队列约束**。昇腾 AI Core 的 Vector 计算单元通过 `TQue`（队列）与 UB 交互，分 `VECIN`（搬入）和 `VECOUT`（搬出）两类；每块芯片同一时刻**并发的 queue 数量有上限**（本平台 `kMaxVecQueNum = 4`）。这是本讲内存分配要硬性满足的约束。

> 术语速查：`ScheduleTask`（调度任务）、`grouped_graphs`（按连通性切出的子图组）、`reuse_id`（UB 内存复用编号）、`TQue`/`TBuf`（AscendC 的队列/临时缓冲对象）。

## 3. 本讲源码地图

本讲围绕 `optimize` 模块下的两个子目录展开：

| 文件 | 作用 |
|------|------|
| [autofuse/optimize/task_generator/schedule_task_generator.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/schedule_task_generator.h) | `ScheduleTaskGenerator` 的对外门面声明（只有一个静态方法）。 |
| [autofuse/optimize/task_generator/schedule_task_generator.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/schedule_task_generator.cpp) | 门面实现：把工作转发给平台对象。 |
| [autofuse/optimize/platform/v1/platformv1.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/platform/v1/platformv1.cpp) | `PlatformV1::GenerateTasks`——真正编排各算子场景生成器的入口，定义了固定顺序与 queue 上限。 |
| [autofuse/optimize/optimize.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.h) | `ScheduleTask` 结构体与 `ReduceTemplateType` 枚举的定义。 |
| [autofuse/optimize/task_generator/schedule_case_generator.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/schedule_case_generator.h) | 场景生成器的抽象基类 `FusionCaseGenerator`，定义了 `Generate` + `GeneratorTask` 模板方法。 |
| [autofuse/optimize/task_generator/reduce_schedule_case_generator.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/reduce_schedule_case_generator.cpp) | reduce 场景：三种模板（通用/全载/R 轴切多核），靠插入 workspace 切图。 |
| [autofuse/optimize/task_generator/transpose_schedule_case_generator.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/transpose_schedule_case_generator.cpp) | transpose 场景：三种子情形，按尾轴 512B 阈值决定保留还是消除。 |
| [autofuse/optimize/task_generator/concat_schedule_case_generator.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/concat_schedule_case_generator.cpp) | concat 场景：首轴转 Store、UB 内拼接、分组切分三种模板。 |
| [autofuse/optimize/buffer_allocate/buf_que_allocator.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/buffer_allocate/buf_que_allocator.h) | `BufQueAllocator` 内存分配器的接口声明。 |
| [autofuse/optimize/buffer_allocate/buf_que_allocator.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/buffer_allocate/buf_que_allocator.cpp) | 复用编号分配、queue 数量约束与生命周期缩短的实现。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**任务生成策略**（门面如何分发）、**算子场景分支**（reduce/transpose/concat 的差异）、**buffer/queue 分配**（内存如何落位）。

### 4.1 任务生成策略：从门面到平台分发

#### 4.1.1 概念说明

「任务生成（Task Generation）」要解决的问题是：一张融合子图里可能同时含有 reduce、transpose、concat 等不同性质的算子，它们各自需要**不同的调度模板（template）**。如果把整张图当成一种模板处理，要么覆盖不全，要么模板爆炸。

Autofuse 的做法是：把这张图交给一连串「场景生成器（case generator）」，每个生成器只认自己擅长的那类算子，**各自产出一组候选 `ScheduleTask`**，最后由 ATT 打分挑选。`ScheduleTaskGenerator` 本身只做一件事——**按芯片平台找到对应的平台对象，把活转发出去**。这种「接口层极薄、实现下沉到平台」的设计，正是 u1-l2 讲过的「平台扩展」在调度阶段的落点。

> 关键认知：**不要在 `schedule_task_generator.cpp` 里找业务逻辑**——它只有 6 行有效代码。真正的编排逻辑在 `platformv1.cpp` 的 `GenerateTasks`。

#### 4.1.2 核心流程

```
Optimizer::OptimizeForHintGraph        （u6-l1 流水线中段）
        │
        ▼
ScheduleTaskGenerator::GenerateTasks(graph, tasks, options)   ← 门面
        │  PlatformFactory::GetInstance().GetPlatform()
        ▼
platform->GenerateTasks(graph, options, tasks)                ← 平台实现
        │  （PlatformV1）
        ▼
固定顺序依次调用场景生成器：
   SplitFusionCaseGenerator   .GeneratorTask()
   ConcatFusionCaseGenerator  .GeneratorTask()
   TransposeFusionCaseGenerator.GeneratorTask()
   ReducePartitionCaseGenerator.GeneratorTask()
   —— 仅当 tasks 为空 ——
   RecomputeCaseGenerator     .GeneratorTask()   ← 兜底
```

每个生成器都把结果**追加（append）**进同一个 `tasks` 向量，互不覆盖；最终 `tasks` 里可能同时存在来自不同场景的多个候选任务。

#### 4.1.3 源码精读

**门面实现**只有一步：取平台、转发。[autofuse/optimize/task_generator/schedule_task_generator.cpp:15-21](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/schedule_task_generator.cpp#L15-L21) 中，`GenerateTasks` 通过 `PlatformFactory` 拿到当前平台，调用 `platform->GenerateTasks`。注意它不关心是哪个平台——这正是门面与实现解耦的体现。

**平台编排**在 [autofuse/optimize/platform/v1/platformv1.cpp:74-89](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/platform/v1/platformv1.cpp#L74-L89)。这段代码就是「固定顺序」的物证：

```cpp
GE_CHK_STATUS_RET(SplitFusionCaseGenerator().GeneratorTask(...), ...);
GE_CHK_STATUS_RET(ConcatFusionCaseGenerator().GeneratorTask(...), ...);
GE_CHK_STATUS_RET(TransposeFusionCaseGenerator().GeneratorTask(...), ...);
GE_CHK_STATUS_RET(ReducePartitionCaseGenerator().GeneratorTask(...), ...);
if (tasks.empty()) {
  GE_CHK_STATUS_RET(RecomputeCaseGenerator().GeneratorTask(...), ...);
}
```

要点有三：

1. 顺序是 **Split → Concat → Transpose → Reduce**。这个顺序不是随意的：reduce 的处理会把图切碎、transpose 的处理会改写 load 的轴语义，它们都依赖前面 concat/split 已经把「拼接/切分」的边界处理干净。
2. **Recompute 是兜底**：只有当前四类场景一个任务都没生成（`tasks.empty()`，即图中没有任何特殊算子，是纯 elementwise）时，才走 `RecomputeCaseGenerator` 做计算重排。
3. 同文件 [platformv1.cpp:23](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/platform/v1/platformv1.cpp#L23) 定义 `kMaxVecQueNum = 4UL`，它是本讲第 3 个模块内存分配的硬件上限来源。

**产物的载体**是 `ScheduleTask` 结构体，定义在 [autofuse/optimize/optimize.h:39-47](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.h#L39-L47)：

| 字段 | 含义 |
|------|------|
| `optimize_graph` | 该任务对应的整张（可能已改写的）实现图。 |
| `grouped_graphs` | 把上图按**连通性**切出的若干子图（见 4.1.4）。 |
| `score_func` | 给 ATT 用的打分函数源码字符串；空串表示无打分。 |
| `reduce_type` | reduce 模板类型（见 4.2.1 枚举）。 |
| `has_load_store_conversion` | 标记本任务是否做了 load/store 形态转换（transpose 场景会置真）。 |

> 注意 `groups_relations_in` 字段：它记录「子图组之间的关系」，主要服务于 reduce 的 R 轴分核两阶段模板（phase1 → phase2 的衔接），本讲 4.2 会再点到。

#### 4.1.4 代码实践：跟踪一次任务生成的调用链

**实践目标**：亲眼确认「门面 → 平台 → 场景生成器」的三级跳，并理解 `grouped_graphs` 是怎么来的。

**操作步骤**：

1. 打开 [autofuse/optimize/optimize.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.cpp)，定位 `OptimizeForHintGraph` 中约 [optimize.cpp:930-937](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.cpp#L930-L937)：`GenerateTasks` 产出 `schedule_tasks` 后，用一个 `for` 循环把每个 `ScheduleTask` 逐个喂给 `AutoScheduler`（u6-l3）。
2. 打开 [schedule_case_generator.h:29-62](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/schedule_case_generator.h#L29-L62)，阅读基类 `FusionCaseGenerator` 的非虚方法 `GeneratorTask`：它在调用完子类的 `Generate` 拿到若干候选图后，对每张图执行 `ScheduleGroupGraphPartitioner::PartitionByConnectivity(graph, task.grouped_graphs)`——**这就是 `grouped_graphs` 的来源**：按数据依赖的连通性，把一张大图切成若干个可独立调度的小子图。

**需要观察的现象**：`GeneratorTask` 是一个「模板方法（template method）」——固定的骨架（生成候选 → 分组 → 入 tasks）写死在基类，子类只需实现 `Generate` 这个纯虚函数提供「如何按算子形态生成候选图」的差异。reduce 生成器没有复用这个基类骨架（它自己写了 `GeneratorTask`），但其余场景都走这条公共骨架。

**预期结果**：你能用一句话讲清 `grouped_graphs` 的来历——「连通分量切分」，并能指出 reduce 生成器是唯一不继承 `FusionCaseGenerator` 骨架的场景生成器。

#### 4.1.5 小练习与答案

**练习 1**：如果新增一块芯片平台 v2，要让它的任务生成顺序与 v1 不同，应该改哪里？要不要动 `schedule_task_generator.cpp`？

**参考答案**：不需要动门面。门面只调 `platform->GenerateTasks`，所以只需为新平台实现一个 `PlatformV2::GenerateTasks`（在 `platform/v2/` 下），通过 `PlatformFactory` 注册即可。门面与业务顺序彻底解耦。

**练习 2**：为什么 `RecomputeCaseGenerator` 用 `if (tasks.empty())` 保护，而前四个生成器没有这层保护？

**参考答案**：前四个生成器各自只处理图中存在的特定算子（无该算子时 `Generate` 早退、不产生任务），追加是安全的、幂等的。Recompute 面向的是「没有任何特殊算子的纯 elementwise 图」，若前四类已经识别出场景并产出了任务，就不应再叠加重排任务，否则会产生冗余候选；故只在 `tasks` 为空时兜底。

---

### 4.2 算子场景分支：reduce / transpose / concat 的候选生成

#### 4.2.1 概念说明

不同算子对调度器提出的要求截然不同：

- **reduce（归约）**：把一个轴上的多个值折叠成一个。它天然是一道「屏障」——屏障前的所有输入必须先到齐，屏障后的计算才能开始。Autofuse 处理 reduce 的核心手段是**在 reduce 处把图切开**，用 workspace 节点把前后两段衔接起来。
- **transpose（转置）**：只是改变了访问内存的轴顺序。如果能把这个「重排」推到最前面的 `Load` 上（即让 load 按转置后的顺序读 GM），就可以**把 transpose 节点整个删掉**，省一次 UB 内重排。
- **concat（拼接）**：把多个张量沿某轴拼成一个。若拼在首轴，每个输入可以直接按偏移 `Store` 回同一块 GM；若拼在非首轴，要么在 UB 内拼好，要么按输入分组分批拼。

本模块的关键共性是：**它们都不立即决定最优方案，而是生成「多个候选模板」+「打分函数」，把选择推迟到 ATT**。理解这一点，就看懂了为什么这些生成器动辄生成 2~3 套模板。

reduce 场景额外引入了模板分类。见 [autofuse/optimize/optimize.h:32-37](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.h#L32-L37) 的枚举：

| 枚举值 | 含义 |
|--------|------|
| `kDefault` | 非 reduce 场景。 |
| `kCommon` | 通用模板：reduce 后融合切分。 |
| `kAllLoad` | 全载模板：被归约轴较短、可一次性全载入 UB。 |
| `kRCore` | R 轴切多核模板：把归约轴（R 轴）切到多个核上分两阶段做。 |

#### 4.2.2 核心流程

三类场景的「改图」手法对比：

```
reduce：插入 workspace 边界，把一张图切成「屏障前 / 屏障后」两段
        reduce ──Store──▶ Workspace_pre           （落盘）
        Workspace_post ──Load──▶ 后续计算         （回读）
        （多模板：kCommon / kAllLoad / kRCore）

transpose：把转置语义「推」到 Load 上，删掉 Transpose 节点
        ┌─ 单个 transpose：生成「保留」+「消除」两套模板，ATT 打分二选一
        └─ 多个 transpose：只生成「消除」模板，全部推到 load
        （阈值：尾轴 512 字节，决定是否需要 UB 重排）

concat：按拼接轴位置分流
        ├─ 首轴：转 Store，按偏移直接写回 GM
        ├─ 非首轴、UB 可全载：UB 内拼接（可能再分小尾轴模板）
        └─ 非首轴、UB 不全载：把 concat 按输入分组切分
```

reduce 是三者中最复杂的，它的主入口 `ReducePartitionCaseGenerator::GeneratorTask` 在 [reduce_schedule_case_generator.cpp:295-318](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/reduce_schedule_case_generator.cpp#L295-L318)，依次生成三类模板：`GeneratorGeneralTask`（kCommon）→ `GeneratorRCoreTask`（kRCore）→ `GeneratorAllLoadTask`（kAllLoad），全部追加到 `tasks`。

#### 4.2.3 源码精读

**(a) reduce：靠 workspace 切图**

reduce 生成器的灵魂函数是 `PartitionReduceNode`，见 [reduce_schedule_case_generator.cpp:446-480](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/reduce_schedule_case_generator.cpp#L446-L480)。它在 reduce 节点周围插入四个节点并重连边：

```cpp
// 新建：两个 workspace、一个 load、一个 store
auto workspace_pre_node  = impl_graph.AddNode(workspace_pre);
auto workspace_post_node = impl_graph.AddNode(workspace_post);
auto load_node  = impl_graph.AddNode(load);
auto store_node = impl_graph.AddNode(store);
// 把 reduce 原本连向下游的边，改由 load_node 接管
// ... RemoveEdge(src->dst); AddEdge(load_node->dst);
// 再接上：reduce ──Store──▶ workspace_pre  （屏障前落盘）
//         workspace_post ──Load──▶ 下游    （屏障后回读）
```

读这段代码时抓住一个心智模型：**reduce 像一道闸，闸前闸后不能共享 UB 流水线**，于是必须在闸口插入「先 Store 落到 workspace、再 Load 读回」的中转。这正是融合图里出现 `Workspace` 节点的根因。

是否每个 reduce 都要切，由 `IsNotPartitionReduce` 判定，见 [reduce_schedule_case_generator.cpp:141-198](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/reduce_schedule_case_generator.cpp#L141-L198)。它用 BFS 遍历 reduce 之后的所有节点，要求：①节点总数不超过阈值 `NODE_COUNT_AFTER_REDUCE = 4`（含 store）；②后继必须都是 elementwise。一旦后继过多或出现非 elementwise 算子，就判定「不宜在 reduce 处切分」，从而走全载（kAllLoad）等其它模板。`R 轴切多核`（kRCore）则更进一步，用 `RMulticorePhase2Graph::Construct` 把图切成 phase1 / phase2 两张子图（[reduce_schedule_case_generator.cpp:773-830](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/reduce_schedule_case_generator.cpp#L773-L830)），用 `groups_relations_in` 记录它们的先后关系。

**(b) transpose：推到 load 上消除**

transpose 的核心是文件顶部 [transpose_schedule_case_generator.cpp:25](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/transpose_schedule_case_generator.cpp#L25) 的阈值常量 `kTransposeNoNeedUBConvertSize = 512`，以及 `Generate` 函数开头那段中文注释（[transpose_schedule_case_generator.cpp:100-108](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/transpose_schedule_case_generator.cpp#L100-L108)）列出的三种子情形：

- **场景 1（尾轴转置）**：需要 UB 重排，保留 Transpose 节点。
- **场景 2（非尾轴转置 + 尾轴 ≥ 512B）**：不需 UB 重排，**删掉 Transpose**，把重排刷新到 load/store。
- **场景 3（非尾轴转置 + 尾轴 ≤ 512B）**：需要 UB 重排，保留 Transpose 节点。

消除 transpose 的动作在 `TransposeConvertProcess`（[transpose_schedule_case_generator.cpp:84-96](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/transpose_schedule_case_generator.cpp#L84-L96)）：调用 `UpdateAxis` 沿数据流向上把转置后的轴顺序刷到 load 等节点，然后 `RemoveNode` 删掉 transpose 自身。

`Generate` 的关键决策在 [transpose_schedule_case_generator.cpp:127-153](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/transpose_schedule_case_generator.cpp#L127-L153)：**单个 transpose 时生成两套模板**（原图保留 + 消除后的副本），并对「保留」那套生成打分函数 `GenerateScoreFuncForUbReorder`，交给 ATT 在运行期按真实 shape 选优；**多个 transpose 时只生成消除模板**。此外，若图中存在 reduce（`cache.HasReduce()`），由于 reduce 不支持与 transpose 融合，则直接把所有 transpose 都消除（[transpose_schedule_case_generator.cpp:116-125](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/transpose_schedule_case_generator.cpp#L116-L125)）。

**(c) concat：按拼接轴位置分流**

concat 的总入口 `Generate` 在 [concat_schedule_case_generator.cpp:85-138](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/concat_schedule_case_generator.cpp#L85-L138)，注释里直接标了三个 case：

- **case1（首轴 concat）**：转 Store。因为首轴拼接意味着各输入在 GM 上是「上下摞在一起」的关系，每个输入算完后按各自偏移 `Store` 回同一块输出 GM 即可——`ConvertConcatToStores` 把 concat 节点替换成一组带偏移的 store（偏移由 `Prepare` 累加各输入沿拼接轴的长度得到，见 [concat_schedule_case_generator.cpp:316-325](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/concat_schedule_case_generator.cpp#L316-L325)）。
- **case2（非首轴、输入数 ≤ `kMaxInputNum=48` 且 UB 可全载）**：UB 内直接拼接，保留 concat 节点。
- **case3（UB 不全载）**：`AddTemplateForSplitConcat` 把 concat 按输入**分组**（`ConcatGroupPartitioner`），每组单独成一个 concat，缓解 UB 装不下的问题。

动态 shape 时还会额外追加一个「强制小尾轴」模板（`AddTemplateForSmallTail`），并在末尾为多模板生成打分函数（`GenerateScoreFunctions`），同样把选择权交给 ATT。

> 三类场景对比一句话总结：**reduce 用「插节点切图」制造屏障边界；transpose 用「删节点+刷轴」把重排推到 load；concat 用「换节点形态（转 Store）或分组」化解 UB 容量压力。** 三者都遵守「生成候选 + 打分」的统一范式。

#### 4.2.4 代码实践：对比 reduce 与 transpose 的任务生成差异

**实践目标**：亲手对照两类场景的「改图」动作，验证本模块的核心结论。

**操作步骤**：

1. 打开 [reduce_schedule_case_generator.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/reduce_schedule_case_generator.cpp)，定位 `PartitionReduceNode`（[第 446 行起](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/reduce_schedule_case_generator.cpp#L446-L480))。数一下它**新增**了几个节点、**新增**了几条边。预期：新增 4 个节点（workspace_pre/post、load、store），把 reduce 的下游边改接到 load，并接上 reduce→store→workspace_pre、workspace_post→load→下游。
2. 打开 [transpose_schedule_case_generator.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/transpose_schedule_case_generator.cpp)，定位 `TransposeConvertProcess`（[第 84 行起](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/transpose_schedule_case_generator.cpp#L84-L96))。观察它**不新增中转节点**，而是先 `UpdateAxis` 沿数据流向上刷新轴语义，再 `RemoveNode` 删除 transpose。
3. 在终端对两个文件做一次 `grep`：`grep -nE "AddNode|RemoveNode" reduce_schedule_case_generator.cpp transpose_schedule_case_generator.cpp`（示例命令），统计各自 Add/Remove 的次数。

**需要观察的现象**：reduce 场景里 `AddNode` 远多于 `RemoveNode`（净增节点以制造边界）；transpose 场景相反，`RemoveNode` 占主导、`AddNode` 很少（净删节点以消除搬运）。这正是「插节点 vs 删节点」两种风格的可观测证据。

**预期结果**：你能填出下表——

| 维度 | reduce | transpose |
|------|--------|-----------|
| 改图手法 | 插入 workspace/load/store 切图 | 推轴 + 删除 transpose 节点 |
| 候选模板数 | 3 类（kCommon/kAllLoad/kRCore） | 单 transpose 时 2 套（保留+消除） |
| 是否制造屏障 | 是（reduce 即屏障） | 否（重排推到 load） |
| 打分函数 | 多模板各自带 | 保留模板带（决定是否需 UB 重排） |

> 待本地验证：第 3 步 grep 的精确计数取决于代码版本，若与上文描述的「净增/净删」趋势不符，请以你本地 HEAD 为准并重新核对函数边界。

#### 4.2.5 小练习与答案

**练习 1**：为什么单个 transpose 要同时生成「保留」和「消除」两套模板，而多个 transpose 只生成「消除」一套？

**参考答案**：单个 transpose 时，是否需要 UB 重排取决于尾轴大小，而尾轴大小在动态 shape 下编译期未知，故生成两套模板并附带 `score_func`，让 ATT 在运行期按真实 shape 打分选优。多个 transpose 时，逐个保留会造成 UB 重排代价累积、且相互难以协调，统一消除（全部推到 load）更优，因此只生成消除模板。

**练习 2**：reduce 的 `IsNotPartitionReduce` 在什么情况下返回 `false`，此时 reduce 会落到哪个模板？

**参考答案**：当 reduce 之后的节点数超过阈值 `NODE_COUNT_AFTER_REDUCE`（含 store 共 4 个），或后继中出现非 elementwise 算子，或多输入节点的某个输入并非 reduce 后节点时，返回 `false`（即「不宜在此 reduce 处切分」）。此时该 reduce 不走 kCommon 的切分模板，而由 `GeneratorAllLoadTask` 尝试 kAllLoad 全载模板（若 `CanFullLoadReduceFuse` 满足）。

---

### 4.3 buffer/queue 内存分配：BufQueAllocator

#### 4.3.1 概念说明

任务生成与 AutoSchedule（u6-l3）产出的是「调度好的子图」，但图上每个中间张量还不知道**住进 UB 的哪块内存**。`BufQueAllocator` 就是把这片「虚的调度结果」落到「实的内存方案」上。

它要平衡两个矛盾的目标：

1. **尽量复用**：UB 容量有限（百 KB 级），中间张量应尽可能共享同一块 UB 内存，避免每个张量独占一片。
2. **不超并发上限**：同时活跃的 `TQue`（VECIN/VECOUT）数量不能超过 `max_que_num`（本平台为 4），否则硬件放不下。

这两个目标本质是一个经典的**区间图着色（interval graph coloring）**问题：每个张量有一个生命周期（从定义到最后使用的区间），生命周期不重叠的张量可以染同一种颜色（共享一个 buffer）；而「同一时刻重叠的张量数」就是并发数，必须 ≤ 上限。

\[ \text{并发 queue 数} = \max_t \big|\{\,\text{tensor} \mid \text{lifetime}(tensor) \ni t\,\}\big| \le \text{max\_que\_num} \]

`BufQueAllocator` 还承接了 u5-l2 的成果：每个算子的 `reg_func` 当初只给出了一串 `TmpBufDesc`（含符号化的 `size` 和 `life_time_axis_id`），现在 `BufQueAllocator` 要把这些占位的临时缓冲需求真正分配成 `TBuf`。

#### 4.3.2 核心流程

`BufQueAllocator` 的三个公开入口分别在不同场景被调用，主入口是 `PrepareImplGraphMemoryPlan`：

```
Optimizer::Optimize  （u6-l1 流水线下半场，[optimize.cpp:976-984]）
   │
   ▼  allocator.PrepareImplGraphMemoryPlan(fused_scheduled_result)
对每个 impl_graph：
   ├─ InitTensorInfo      —— 计算每个张量的生命周期、是否可复用
   ├─ AllocateReuseId     —— 给可共享的张量编同一个 reuse_id
   ├─ AllocateWithinGroup —— 统计并发 vecin/vecout queue 数
   ├─ 若 vecin 超限 ─▶ ShortenVecinLifetime   —— 缩短生命周期以降并发
   ├─ 若 vecout 超限 ─▶ ShortenVecoutLifetime
   └─ GetAndSetNodeTempBuffer —— 用 reg_func 的 TmpBufDesc 分配 TBuf
```

`reuse_id` 的语义见 [buf_que_allocator.cpp:600](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/buffer_allocate/buf_que_allocator.cpp#L600) 的注释——**reuse_id 相同表示共用同一块 UB，不同表示各自占用**。

#### 4.3.3 源码精读

**接口与上限**：[buf_que_allocator.h:22-26](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/buffer_allocate/buf_que_allocator.h#L22-L26) 声明了三个公开方法；上限 `max_que_num` 由平台配置传入（`PlatformV1` 构造时设为 `kMaxVecQueNum = 4`，见 [platformv1.cpp:25-28](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/platform/v1/platformv1.cpp#L25-L28)）。

**queue 约束与生命周期缩短**：核心函数 `AllocBufQueForSingleImplGraph`，见 [buf_que_allocator.cpp:134-161](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/buffer_allocate/buf_que_allocator.cpp#L134-L161)。它的逻辑就是一个「分配 → 超限则缩短 → 再分配」的循环：

```cpp
AllocateWithinGroup(impl_graph, total_vecin_nums, total_vecout_nums, ...);
if (total_vecin_nums > max_que_num) {
  ShortenVecinLifetime(impl_graph, max_que_num);   // 缩短 vecin 生命周期
  AllocateWithinGroup(...);                         // 重新统计
}
if (total_vecout_nums > max_que_num) {
  ShortenVecoutLifetime(impl_graph, max_que_num);  // 同理处理 vecout
  AllocateWithinGroup(...);
}
GE_ASSERT_TRUE(total_vecin_nums <= max_que_num && total_vecout_nums <= max_que_num, ...);
```

「缩短生命周期」的算法依据是 `FindOverlappingNodeSets`，见 [buf_que_allocator.cpp:105-132](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/buffer_allocate/buf_que_allocator.cpp#L105-L132)：它把生命周期相互重叠、且共同占用的 queue 数超过上限的节点归为一组，随后由 `ShortenVecinLifetime` / `ShortenVecoutLifetime`（[buf_que_allocator.cpp:713](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/buffer_allocate/buf_que_allocator.cpp#L713) 与 [buf_que_allocator.cpp:767](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/buffer_allocate/buf_que_allocator.cpp#L767)）对这些节点做拆分，降低同时活跃的 queue 数。直觉上就是把「占着 UB 不用太久」的张量提前释放，给后来者腾位置。

**复用编号分配**：`AllocateReuseId` 见 [buf_que_allocator.cpp:601-628](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/buffer_allocate/buf_que_allocator.cpp#L601-L628)。它遍历每个非 buffer 节点的输出张量，给 `kPositionVecIn` 的张量按「下游是否单一且可复用」决定是否与下游共享编号（典型如 `load → 计算节点`，load 的输出可被计算节点的输出复用），其余情况递增新编号。

**承接 reg_func 的临时缓冲**：`GetAndSetNodeTempBuffer` 见 [buf_que_allocator.cpp:386-400](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/buffer_allocate/buf_que_allocator.cpp#L386-L400)。它读取每个节点上的 `TmpBufDesc`（其中 `size` 是符号表达式、`life_time_axis_id` 标识 API 级 `-1` 或 loop 级），把这块临时缓冲的需求落位——这正是 u5-l2 里 `CalcTmpBufSize` 当初埋下的占位契约，现在由 `BufQueAllocator` 兑现成 `TBuf`。

**在流水线中的位置**：[optimize.cpp:976-984](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.cpp#L976-L984) 是 `BufQueAllocator` 被调用的现场——在 `OptimizeForHintGraph`（含任务生成 + AutoSchedule）完成之后、`StaticUbTemplateFilter` 与 `CollectFusedIoNodes` 之前。也就是说，**内存分配在调度之后、codegen 之前**，产出的 `reuse_id` / `que.id` / `TBuf` 信息会原样传给 u8 的 codegen，由它生成 `TQue<TPosition::VECIN>`、`TBuf` 声明与 `AllocTensor` 调用。

#### 4.3.4 代码实践：观察「分配—缩短—再分配」与 reg_func 的衔接

**实践目标**：确认内存分配的约束满足机制，并验证它消费了 u5-l2 的 `TmpBufDesc`。

**操作步骤**：

1. 打开 [buf_que_allocator.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/buffer_allocate/buf_que_allocator.cpp)，阅读 `AllocBufQueForSingleImplGraph`（[第 134 行起](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/buffer_allocate/buf_que_allocator.cpp#L134-L161))，找到两处 `if (... > max_que_num)` 分支与最后的 `GE_ASSERT_TRUE`。
2. 阅读同文件 `GetAndSetNodeTempBuffer`（[第 386 行起](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/buffer_allocate/buf_que_allocator.cpp#L386-L400))，注意它读取的 `buf_desc.size` 与 `buf_desc.life_time_axis_id` 两个字段。
3. 回看 u5-l2 提到的 `TmpBufDesc`（在 reduce/compare 等 reg_func 中由 `CalcTmpBufSize` 产出），对照确认这两个字段就是当初埋下的占位。

**需要观察的现象**：`max_que_num` 在 `AllocBufQueForSingleImplGraph` 中出现 4 次（两次判断、两次断言），是整段逻辑的「红线」；而 `life_time_axis_id == -1` 表示 API 级临时缓冲、`>= 0` 表示 loop 级，这一区分直接决定了 `TBuf` 是在循环外分配还是在循环内复用。

**预期结果**：你能解释 `BufQueAllocator` 的作用——「在并发 queue 数 ≤ 4 的硬约束下，用生命周期复用为每个中间张量分配 UB 内存，并把 reg_func 登记的算子临时缓冲需求兑现成 TBuf」，并指出它的产物（reuse_id、que.id、TBuf）全部流向 u8 codegen。

> 待本地验证：步骤 2 中字段名的精确拼写以你本地 HEAD 的 `tensor_mem_defs.h` 为准；若 `life_time_axis_id` 的取值含义与文档不符，请以源码与 UT 断言为准。

#### 4.3.5 小练习与答案

**练习 1**：若一张融合图同时有 6 个 vecin queue 活跃，`BufQueAllocator` 会如何处理？最终是否一定满足上限？

**参考答案**：`AllocBufQueForSingleImplGraph` 会调用 `ShortenVecinLifetime` 对生命周期重叠且超限的节点集合做拆分（缩短部分张量的生命周期，让它们提前释放 UB），然后重新 `AllocateWithinGroup` 统计。若仍超限则继续调整，并在末尾用 `GE_ASSERT_TRUE` 断言 `total_vecin_nums <= max_que_num`。所以正常路径下最终一定满足上限；若调整后仍超限，断言会失败并报错（属异常图）。

**练习 2**：`reuse_id` 与 `que.id` 是同一个东西吗？依据是什么？

**参考答案**：不是。依据 [buf_que_allocator.cpp:600](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/buffer_allocate/buf_que_allocator.cpp#L600) 的注释：「reuse id 和 que id 是独立编码」。`reuse_id` 标识 UB 内存复用分组（同号共享一块 buffer），`que.id` 标识该张量挂在哪条 queue 上；分配 reuse_id 时不考虑是否在同一 queue 内。

---

## 5. 综合实践

**任务**：用一张 reduce + transpose 混合的融合图，端到端跟踪「任务生成 → 内存分配」两阶段对它的改造，画出改造前后的图差异。

**操作步骤**：

1. 在 [autofuse/optimize/task_generator/reduce_schedule_case_generator.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/reduce_schedule_case_generator.cpp) 中确认 reduce 的切图动作（`PartitionReduceNode` 插入 workspace/load/store）。
2. 在 [autofuse/optimize/task_generator/transpose_schedule_case_generator.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/transpose_schedule_case_generator.cpp) 中确认：当图中**同时存在 reduce**时，transpose 生成器走的是 [第 116-125 行](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/transpose_schedule_case_generator.cpp#L116-L125) 的「全部消除」分支（`cache.HasReduce()` 为真），即先把所有 transpose 推到 load 上删掉，再把图交给 reduce 生成器。
3. 结合 [platformv1.cpp:74-89](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/platform/v1/platformv1.cpp#L74-L89) 解释「为什么 Transpose 排在 Reduce 之前」：先消除 transpose，reduce 才能拿到一张没有 transpose 干扰的图，`IsNotPartitionReduce` 的后继 elementwise 判定才不会被 transpose 打断。
4. 最后在 [buf_que_allocator.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/buffer_allocate/buf_que_allocator.cpp) 中确认：reduce 切图时插入的 workspace 是 GM 上的（不计入 UB queue 并发），而 load/store 之间的 UB 中间张量才会参与 `reuse_id` 分配与 queue 上限约束。

**预期产出**：

- 一张「原始图（含 reduce + transpose）」→「transpose 消除后（无 transpose，load 带转置轴语义）」→「reduce 切图后（reduce 处插入 workspace/load/store 边界）」→「内存分配后（UB 中间张量标注 reuse_id）」的四阶段演化图（手绘或文字描述均可）。
- 一句话结论：**任务生成阶段负责「把图改写成可调度的形态」（删 transpose、切 reduce），内存分配阶段负责「在硬件约束下为改写后的图分配 UB」（复用 + queue 上限）**，两者是改图与落位的关系。

## 6. 本讲小结

- `ScheduleTaskGenerator::GenerateTasks` 是一个**门面**，真正的编排逻辑在平台对象的 `GenerateTasks`，目前实现是 `PlatformV1`。
- `PlatformV1::GenerateTasks` 以**固定顺序** Split → Concat → Transpose → Reduce 调用各场景生成器，Recompute 仅在无任何场景命中时兜底；顺序固定是正确性需要（reduce 依赖 transpose 已被消除）。
- 三类算子场景改图手法各异：**reduce 插节点切图**（workspace 边界）、**transpose 删节点推轴**（推到 load）、**concat 换节点形态**（转 Store 或分组），但都遵循「生成候选模板 + 附打分函数、把选择交给 ATT」的统一范式。
- reduce 有三类模板（`kCommon` / `kAllLoad` / `kRCore`），是否切分由 `IsNotPartitionReduce` 按后继节点数与类型判定。
- `BufQueAllocator` 在「调度之后、codegen 之前」运行，在 **`max_que_num = 4` 的并发上限**下用**生命周期复用**分配 UB，超限时通过 `ShortenVecin/VecoutLifetime` 缩短生命周期来降并发。
- `BufQueAllocator` 兑现了 u5-l2 中 `reg_func` 埋下的 `TmpBufDesc` 占位，把算子临时缓冲需求落成 `TBuf`；其产物（`reuse_id`、`que.id`、`TBuf`）全部流向 u8 codegen。

## 7. 下一步学习建议

- **衔接 u7（ATT 自动 Tiling）**：本讲反复出现的 `score_func` 字段，正是 ATT 在运行期对多候选模板打分选优的依据。下一讲会讲清 ATT 如何用性能 cost model 求解最优 tiling，并把打分函数真正执行起来。
- **衔接 u8（Codegen）**：本讲 `BufQueAllocator` 产出的 `reuse_id`、`que.id`、`TBuf` 信息，会在 codegen 里变成 `TQue<TPosition::VECIN/VECOUT>` 与 `TBuf` 的声明及 `AllocTensor` 调用。阅读 codegen 时可回看本讲，理解「内存方案从何而来」。
- **源码延伸阅读**：若想了解 `grouped_graphs` 的连通性切分细节，可读 [schedule_group_partitioner.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/schedule_group_partitioner.cpp) 与 `concat_group_partitioner.cpp`；想了解 split 场景（本讲点到未展开），可对照 `split_schedule_case_generator.cpp`，它与 concat 互为镜像。
