# Optimizer 总编排

> 本讲属于「Optimize 优化与调度主链路」单元的第一讲。建议先学完 [u4-l1 核心图 IR](u4-l1-core-graph-ir.md)（ComputeGraph/Node）与 [u3-l2 Autofuse 目录总览](u3-l2-autofuse-overview.md)（六大模块数据流）再来阅读。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `Optimizer::Optimize` 的**两个公有重载**分别接收什么样的图、被谁调用。
2. 按调用顺序**复述**优化调度流水线上的每一个阶段（预处理 → 图改写 Pass → 合轴 → 任务生成 → AutoSchedule → 内存分配），并能指出每个阶段的输入与输出。
3. 区分 **hint graph**（单张带调度语义的 `AscGraph`）与 **fused graph**（含多个子图的 GE 计算图），并解释 fused graph 是如何「展开」成 hint graph 再复用同一条流水线的。

一句话定位：`Optimizer` 是 Autofuse 数据流中 `optimize` 模块的**总指挥**，它把一张「逻辑计算图」一步步变成「带切分方案和内存方案的 `FusedScheduledResult`」，交给下游 ATT/codegen 去生成真正能跑的 kernel。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**第一，Autofuse 的优化对象是「调度图」而非「算子列表」。** 回顾 [u3-l2](u3-l2-autofuse-overview.md)：Autofuse 把相邻 Vector 算子聚成一个融合区域后，并不是直接生成代码，而是先在 `optimize` 模块里做一轮「调度规划」——决定每个张量沿哪些轴循环、怎么切分到多核、片上缓冲怎么复用。本讲的主角 `Optimizer` 就是这次规划的编排者。

**第二，图有两层表示，本讲会反复出现，请先记住名字：**

| 概念 | 类型 | 形态 | 谁产出 |
|---|---|---|---|
| fused graph | `af::ComputeGraphPtr` | 一张 GE 计算图，内部挂着若干 `AscGraph`/`AscBackend` 子图节点 | 框架侧（torch.compile / Inductor / GE） |
| hint graph | `af::AscGraph` | 单张、带完整调度语义（轴 axis、步幅 stride、重复 repeats）的调度图 | `Optimizer` 把 fused graph「展开」后得到 |

**第三，ASCIR 是「带调度语义的视图」。** 回顾 [u4-l1](u4-l1-core-graph-ir.md) 与 [u4-l2](u4-l2-tensor-attr-ascir.md)：全链路只有一份图数据，`AscGraph` 是搭在 `ComputeGraph` 之上的视图。所以本讲看到的所有「拷贝图」（`CopyFrom`），拷贝的是这层调度视图，而非另起炉灶重画一张算子图。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `autofuse/optimize/optimize.h` | `Optimizer` 类声明 | `GraphType`、`OptimizerOptions`、`ScheduleTask`、两个 `Optimize` 重载、各私有阶段方法 |
| `autofuse/optimize/optimize.cpp` | 全部实现 | 三个主入口（两个 `Optimize` + `OptimizeFusedAscBackend`）、核心 `OptimizeForHintGraph`、`AutoScheduler` |
| `autofuse/common/schedule_result.h` | 调度结果数据结构 | `ScheduleGroup`/`ScheduledResult`/`FusedScheduledResult`，即整条流水线的最终产物 |
| `autofuse/optimize/task_generator/schedule_task_generator.h`(+`.cpp`) | 任务生成器入口 | `GenerateTasks` 把整图拆成若干 `ScheduleTask` |
| `autofuse/optimize/autoschedule/autoschedule.h` | 自动调度入口 | `AutoSchedule::DoAutoSchedule` 产出若干 tiling 候选 |
| `autofuse/optimize/buffer_allocate/buf_que_allocator.h` | 内存分配器 | `PrepareImplGraphMemoryPlan` 等三件套，给图分配 TBuf/TQue |
| `autofuse/compiler/py_module/pyautofuse.cpp` | Python 绑定 | `ScheduleV1`/`ScheduleV2` 分别映射到两个 `Optimize` 重载 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**入口与重载** → **阶段流水线** → **hint/fused graph 差异**。三者层层递进——先知道有几扇门，再看每扇门里的走廊，最后理解两条走廊为何共用同一段。

### 4.1 Optimize 入口与两个重载

#### 4.1.1 概念说明

`Optimizer` 是一个**门面类（facade）**：它本身不实现具体优化算法，而是把预处理、图改写、调度、内存分配等子系统串成一条流水线。对外只暴露一个动词——`Optimize`（优化），但这个动词有**两个重载**，区别在于「输入的图形态不同」：

- 重载 A：`Optimize(const af::ComputeGraphPtr &fused_graph, ...)` —— 收一张 **fused graph**（GE 计算图）。
- 重载 B：`Optimize(af::AscGraph &hint_graph, ...)` —— 收一张 **hint graph**（单张调度图）。

读者此刻不必深究两者差异，第 4.3 节会专门讲。这里只需建立两个事实：① 它们都是公有方法；② 重载 A 内部最终会调用重载 B（这正是一条「展开后复用」的设计，4.3 会验证）。

还有一个区分「来源」的枚举 `GraphType`，它决定重载 A 走哪条内部分支：

#### 4.1.2 核心流程

```
调用方（Python / pyautofuse）
        │
        │ ScheduleV1 → 传 hint graph    ScheduleV2 → 传 fused graph
        ▼                                   ▼
  Optimize(AscGraph&) 重载B          Optimize(ComputeGraphPtr) 重载A
                                            │
                            ┌───────────────┴────────────────┐
                  graph_type==kFusedAscBackend?         否(kAscGraph/kFusedAscGraph)
                            │ 是(Inductor)                      │
                            ▼                                   ▼
                 OptimizeFusedAscBackend        展开 fused graph → hint graph
                 （逐个 AscBackend 子图）        再调用重载 B（复用同一条流水线）
```

要点：
- **重载 B 是核心**。重载 A 只负责「把 fused graph 拆/展开成 hint graph」，然后转交给重载 B。
- `GraphType` 是分叉开关：`kFusedAscBackend`（Inductor 来源）走 `OptimizeFusedAscBackend`；`kAscGraph`（GE 来源）和 `kFusedAscGraph`（concat 来源）走「展开后调重载 B」。

#### 4.1.3 源码精读

先看头文件里 `Optimizer` 的公开面与两个重载：

[autofuse/optimize/optimize.h:49-64](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.h#L49-L64) —— 这是 `Optimizer` 类的公有部分，声明了两个 `Optimize` 重载与一个 `SetOptimizerOptions`。

```cpp
class Optimizer {
 public:
  explicit Optimizer(const OptimizerOptions &options);
  // 重载A：对 fused_graph 做前处理、auto schedule、内存分配
  Status Optimize(const af::ComputeGraphPtr &fused_graph, ::ascir::FusedScheduledResult &fused_scheduled_result);
  // 重载B：对 hint_graph 做前处理、auto schedule、内存分配
  Status Optimize(af::AscGraph &hint_graph, ::ascir::FusedScheduledResult &fused_scheduled_result);
  ...
};
```

注意两个重载的注释（源码中紧挨声明上方）几乎逐字相同——「做前处理、auto schedule、内存分配」——这正暗示它们走的是**同一套后半段流水线**。

再看 `GraphType` 枚举，它是重载 A 的内部分叉依据：

[autofuse/optimize/optimize.h:22-30](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.h#L22-L30) —— `GraphType` 用来源标注图：`kAscGraph`(ge)、`kFusedAscBackend`(inductor)、`kFusedAscGraph`(concat)；`OptimizerOptions` 只持有这一个字段。

```cpp
enum class GraphType {
  kAscGraph = 0,     // ge
  kFusedAscBackend,  // inductor
  kFusedAscGraph,    // concat
  kInvalidGraph,
};
struct OptimizerOptions {
  GraphType graph_type = GraphType::kAscGraph;
};
```

重载 A（fused graph）的实现开头就体现了「分叉」：

[autofuse/optimize/optimize.cpp:555-563](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.cpp#L555-L563) —— 重载 A 入口：先记日志、用 RAII Guard 登记图名，再用 `graph_type` 决定是否直接转给 `OptimizeFusedAscBackend`。

```cpp
Status Optimizer::Optimize(const af::ComputeGraphPtr &fused_graph, ...) {
  ascir::utils::FusedGraphNameGuard guard(fused_graph->GetName());
  ascir::utils::DumpComputeGraph(fused_graph, "BaseFusedGraph");
  if (options_.graph_type == GraphType::kFusedAscBackend) {
    return OptimizeFusedAscBackend(fused_graph, fused_scheduled_result);  // Inductor 分支
  }
  // 否则继续往下：从 AscGraphNode 反序列化出 hint graph（见 4.3）
```

最后，把视角拉到**调用方**，看 Python 绑定如何把两个入口区分开：

[autofuse/compiler/py_module/pyautofuse.cpp:366](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/compiler/py_module/pyautofuse.cpp#L366) —— `ScheduleV1` 收一张 `HintGraph`，调用**重载 B**（`Optimize(AscGraph&)`）。

[autofuse/compiler/py_module/pyautofuse.cpp:392](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/compiler/py_module/pyautofuse.cpp#L392) —— `ScheduleV2` 收一张 `HintComputeGraph`，调用**重载 A**（`Optimize(ComputeGraphPtr)`）。

> 对应关系：Python 里 `Schedule.schedule(hint_graph)` → 重载 B；`Schedule.scheduleV2(compute_graph)` → 重载 A。这就是「同一类、两个动词、两个重载」在 Python 侧的投影。

#### 4.1.4 代码实践

**实践目标**：确认两个重载的调用关系与 `GraphType` 的分叉作用。

**操作步骤**（源码阅读型）：

1. 打开 `autofuse/optimize/optimize.cpp`，定位重载 A（`Optimize(const af::ComputeGraphPtr &, ...)`）与重载 B（`Optimize(af::AscGraph &, ...)`）。
2. 在重载 A 中找到 `if (options_.graph_type == GraphType::kFusedAscBackend)` 这一行，确认 `kFusedAscBackend` 走 `OptimizeFusedAscBackend`，其余情形会继续向下构造 hint graph。
3. 在重载 A 的剩余部分找到调用重载 B 的那一行（提示：形如 `Optimize(hint_graph, ...)`），记下它在第几行（答案见小练习）。

**需要观察的现象**：重载 A 末尾确实存在 `Optimize(hint_graph, fused_scheduled_result)` 这样的「自我委托」调用，证明「fused graph 先展开成 hint graph，再复用重载 B」。

**预期结果**：你能画出一个分叉图：`重载A → (kFusedAscBackend? OptimizeFusedAscBackend : 构造hint_graph → 重载B)`。

> 本实践为源码阅读型，无需上板运行。

#### 4.1.5 小练习与答案

**练习 1**：重载 A 内部调用重载 B 的那一行，行号大约是多少？它前面的几行代码在做什么？

**参考答案**：约在 `optimize.cpp:601`，语句是 `Optimize(hint_graph, fused_scheduled_result)`。它前面先从 fused graph 的 `AscGraphNode` 节点里**反序列化**出 `AscGraph`，若不止一个子图就用 `FusedGraphUnfolder::UnfoldFusedGraph` 展开，最终合并成单张 `hint_graph`，再交给重载 B。

**练习 2**：如果想让一个 GE 来源（`kAscGraph`）的图走 `OptimizeFusedAscBackend`，能直接改 `graph_type` 吗？为什么设计上不建议？

**参考答案**：技术上可以改 `graph_type`，但不建议——`OptimizeFusedAscBackend` 假设节点类型是 `kAscBackendType`（Inductor 风格）、并依赖 `AutoFuseAttrs` 取子图，而 `kAscGraph` 来源的节点类型是 `kAscGraphNodeType`（GE 风格，靠 `ascgraph` 属性序列化）。两者取子图的协议不同，强行混用会取不到子图导致断言失败。`GraphType` 的存在正是为了让两条来源各自走自己兼容的路径。

---

### 4.2 优化调度的阶段流水线

#### 4.2.1 概念说明

理解了入口，现在进入走廊本身。重载 B（hint graph）是「最干净」的一条路径，它由两部分组成：

1. **`OptimizeForHintGraph`**：对单张 hint graph 做逐图优化与调度（这是流水线的「上半场」）。
2. **后处理阶段**：内存规划、UB 模板过滤、L2 cache hint、组并行、load 调序（这是流水线的「下半场」）。

一个关键设计：`OptimizeForHintGraph` 是**共享核心**——重载 B 调它，`OptimizeFusedAscBackend`（Inductor 分支）也调它。所以把它读懂，等于读懂了两条来源的后半段。

每个阶段都要回答三个问题：**输入是什么？做了什么？输出是什么？** 本节末尾会给一张「阶段—输入—输出」表。

#### 4.2.2 核心流程

下面是重载 B 的完整阶段序列（按调用顺序）：

```
Optimize(AscGraph&) 重载B
│
├─ ① RemoveDanglingNodes          # 清理无消费者、悬空的死节点
│
├─ OptimizeForHintGraph  ───────────── 「逐图核心」上半场
│    ├─ ② NormalizeAxisIds / SetGraphType(kImplGraph)
│    ├─ ③ CompleteApiInfo + CheckGraphValidity
│    ├─ ④ PreProcess::Run            # 仅 kFusedAscBackend 生效
│    ├─ ⑤ CopyFrom → optimize_graph  # 逻辑图 → 调度图
│    ├─ ⑥ DtypeConsistency           # 插 Cast 保证 dtype 一致
│    ├─ ⑦ GraphPass                  # 跑一批图改写 Pass（CSE/softmax/...）
│    ├─ ⑧ RemoveAllZeroStrideLoopAxis + MergeContinuousAxis   # 合并连续轴
│    ├─ ⑨ ScheduleTaskGenerator::GenerateTasks  # 整图 → 若干 ScheduleTask
│    └─ ⑩ AutoScheduler（每个 task）# 生成 tiling 候选 → ScheduledResult
│
├─ ⑪ BufQueAllocator::PrepareImplGraphMemoryPlan   # 下半场：内存规划
├─ ⑫ StaticUbTemplateFilter::Filter
├─ ⑬ BufQueAllocator::CollectFusedIoNodes
├─ ⑭ L2CacheHintManager::ParseGraph
├─ ⑮ TryEnableGroupParallel
└─ ⑯ ExecSeqAdvancedOfLoad          # load 前移调序，藏访存延迟
```

每阶段产出层层叠加，最终汇聚进同一个 `FusedScheduledResult`。可以把它理解为一条「累加器」流水线：每个阶段往里写一个维度的信息（调度结果、内存方案、IO 节点、缓存提示……）。

#### 4.2.3 源码精读

**重载 B 的骨架**，先看「下半场」如何编排：

[autofuse/optimize/optimize.cpp:966-993](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.cpp#L966-L993) —— 重载 B：先清死节点，调用 `OptimizeForHintGraph` 拿到调度结果，再做一串「内存/缓存/并行」后处理。

```cpp
Status Optimizer::Optimize(af::AscGraph &hint_graph, FusedScheduledResult &fused_scheduled_result) {
  ascir::utils::DumpGraph(hint_graph, "AutoFuseBeforeRemoveDanglingNodes");
  GE_CHK_STATUS_RET(RemoveDanglingNodes(hint_graph), ...);           // ①
  ascir::utils::DumpGraph(hint_graph, "AutoFuseBeforeOptimize");
  fused_scheduled_result.node_idx_to_scheduled_results.resize(1UL);
  ...
  GE_ASSERT_SUCCESS(OptimizeForHintGraph(hint_graph, ...), ...);     // ②~⑩
  // 内存分配（下半场）
  BufQueAllocator allocator;
  GE_CHK_STATUS_RET(allocator.PrepareImplGraphMemoryPlan(fused_scheduled_result));   // ⑪
  ...
  GE_CHK_STATUS_RET(StaticUbTemplateFilter().Filter(fused_scheduled_result));        // ⑫
  GE_CHK_STATUS_RET(allocator.CollectFusedIoNodes(fused_scheduled_result));          // ⑬
  GE_CHK_STATUS_RET(optimize::L2CacheHintManager::ParseGraph(*compute_graph, ...));  // ⑭
  TryEnableGroupParallel(fused_scheduled_result);                                    // ⑮
  ExecSeqAdvancedOfLoad(fused_scheduled_result);                                     // ⑯
  ascir::utils::DumpScheduleResult(fused_scheduled_result, "AutoFuseAfterOptimize");
  return af::SUCCESS;
}
```

注意源码里穿插的 `DumpGraph(..., "AutoFuseBeforeOptimize")` 等调用——它们是 DFX 调试点，每个标签正好对应一个阶段边界。这是 4.2.4 实践的抓手。

**逐图核心 `OptimizeForHintGraph`**，这是流水线信息密度最高的方法：

[autofuse/optimize/optimize.cpp:895-943](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.cpp#L895-L943) —— 单张图的优化与调度核心：预处理 → dtype → Pass → 合轴 → 生成任务 → 逐任务 AutoSchedule。

```cpp
Status Optimizer::OptimizeForHintGraph(af::AscGraph &hint_graph, ...) {
  ScheduleUtils::NormalizeAxisIds(hint_graph);
  hint_graph.SetGraphType(af::AscGraphType::kImplGraph);
  GE_CHK_STATUS_RET(AscGraphInfoComplete::CompleteApiInfo(hint_graph), ...);
  GE_ASSERT_SUCCESS(CheckGraphValidity(hint_graph), ...);
  if (options_.graph_type == GraphType::kFusedAscBackend) {          // ④ 仅 Inductor 生效
    GE_CHK_STATUS_RET(af::pre_process::PreProcess::Run(hint_graph), ...);
    ascir::utils::DumpGraph(hint_graph, "AfterPreProcess");
  }
  ascir::ImplGraph optimize_graph(base_graph_name.c_str());
  GE_ASSERT_TRUE(optimize_graph.CopyFrom(hint_graph));               // ⑤ 复制成调度图
  GE_CHK_STATUS_RET(DtypeConsistency::EnsureDtypeConsistency(...));  // ⑥
  GE_CHK_STATUS_RET(GraphPass(optimize_graph), ...);                 // ⑦
  ...
  if (!ScheduleUtils::HasComputeType(optimize_graph, af::ComputeType::kComputeCube)) {
    GE_ASSERT_SUCCESS(RemoveAllZeroStrideLoopAxis(optimize_graph));  // ⑧ 删零步幅轴
    GE_ASSERT_SUCCESS(MergeContinuousAxis(optimize_graph));          // ⑧ 合并连续轴
  }
  std::vector<ScheduleTask> schedule_tasks;
  GE_CHK_STATUS_RET(ScheduleTaskGenerator::GenerateTasks(...));      // ⑨ 生成任务
  for (size_t i = 0U; i < schedule_tasks.size(); ++i) {
    GE_CHK_STATUS_RET(AutoScheduler(hint_graph, schedule_tasks[i], scheduled_results), ...); // ⑩
  }
  GE_ASSERT_SUCCESS(FinalizeIndexedGraphs(scheduled_results));       # 给图名加索引后缀
  return af::SUCCESS;
}
```

三个值得记的设计点：

1. **`CopyFrom` 做了一道「逻辑图 → 调度图」的边界**。`hint_graph` 之后被「冻结」，后续所有改写都在副本 `optimize_graph` 上进行，原始 hint graph 仍可用于回溯。
2. **`PreProcess` 只对 `kFusedAscBackend` 生效**——源码注释明说「当前仅针对该流程生效，后续会全部放开」。这说明不同来源进入流水线的起点略有不同。
3. **合轴（⑧）紧接图改写 Pass（⑦）之后**。`MergeContinuousAxis` 把内存上连续的多个轴合成一个，减少循环嵌套层数，是减少调度开销的关键一步。它的实现见 [autofuse/optimize/optimize.cpp:808-893](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.cpp#L808-L893)，核心是判断「相邻轴步幅是否连续」再合并。

**任务生成与 AutoSchedule（⑨⑩）**：

[autofuse/optimize/task_generator/schedule_task_generator.cpp:15-21](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/schedule_task_generator.cpp#L15-L21) —— `GenerateTasks` 是个薄壳，真正逻辑按平台下沉到 `PlatformFactory`。

```cpp
Status ScheduleTaskGenerator::GenerateTasks(::ascir::ImplGraph &optimize_graph,
                                            std::vector<ScheduleTask> &tasks, const OptimizerOptions &options) {
  const auto &platform = PlatformFactory::GetInstance().GetPlatform();
  GE_ASSERT_SUCCESS(platform->GenerateTasks(optimize_graph, options, tasks));
  return af::SUCCESS;
}
```

生成的每个 `ScheduleTask` 长这样（[autofuse/optimize/optimize.h:39-47](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.h#L39-L47)）：

```cpp
struct ScheduleTask {
  ::ascir::ImplGraph optimize_graph;
  std::vector<::ascir::ImplGraph> grouped_graphs;   // 切分出的若干子图
  std::string score_func;
  std::map<size_t, std::vector<size_t>> groups_relations_in{};  // 子图间依赖
  ReduceTemplateType reduce_type{...};
  ::ascir::CubeTemplateType cube_type{...};
  bool has_load_store_conversion{false};
};
```

随后 `AutoScheduler` 对**每个 `grouped_graph`** 调用 `AutoSchedule::DoAutoSchedule()`，产出若干 tiling 候选（`AutoScheduleOutput`），最终封装成 `ScheduledResult`：

[autofuse/optimize/optimize.cpp:1057-1106](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.cpp#L1057-L1106) —— `AutoScheduler` 遍历 `grouped_graphs`，对 cube 类与非 cube 类分别处理；非 cube 类用 `AutoSchedule(...).DoAutoSchedule()` 生成候选并打包进 `ScheduledResult`。

```cpp
for (auto &grouped_graph : schedule_task.grouped_graphs) {
  ...
  if (ScheduleUtils::HasComputeType(grouped_graph, af::ComputeType::kComputeCube)) {
    GE_ASSERT_SUCCESS(ProcessCubeSchedules(...));   // cube 类走专用模板
    continue;
  }
  std::vector<autoschedule::AutoScheduleOutput> schedule_outputs;
  auto scheduler = autoschedule::AutoSchedule(grouped_graph, schedule_outputs, ...);
  GE_CHK_STATUS_RET(scheduler.DoAutoSchedule(), ...);   // 生成 tiling 候选
  ...
  GE_ASSERT_SUCCESS(ProcessNonReduceSchedules(...));    // 打包进 ScheduledResult
}
```

**最终产物** `FusedScheduledResult`（[autofuse/common/schedule_result.h:55-63](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/common/schedule_result.h#L55-L63)）：

```cpp
struct FusedScheduledResult {
  ge::AscendString fused_graph_name;
  std::vector<af::AscNodePtr> input_nodes;
  std::vector<af::AscNodePtr> output_nodes;
  std::vector<af::AscNodePtr> workspace_nodes;
  std::vector<af::Expression> origin_vars;
  std::vector<std::vector<ScheduledResult>> node_idx_to_scheduled_results;  // 核心字段
  GmTensorSizes gm_tensor_sizes;
};
```

其中 `node_idx_to_scheduled_results` 是「每个子图 → 多个调度结果」的二维结构，承载了上半场的全部产出；下半场再往里补内存方案、IO 节点等。

**阶段—输入—输出对照表**：

| 阶段 | 输入 | 做了什么 | 输出 |
|---|---|---|---|
| ① RemoveDanglingNodes | hint graph | 删无消费者死节点、重排拓扑 | 干净的图 |
| ④ PreProcess | hint graph | （仅 Inductor）插入标量广播等 | 规整后的图 |
| ⑥ DtypeConsistency | optimize_graph | 插 Cast 补齐 dtype | dtype 一致的图 |
| ⑦ GraphPass | optimize_graph | CSE/softmax 等 Pass 改写 | 优化后的图 |
| ⑧ MergeContinuousAxis | optimize_graph | 合并内存连续的轴 | 轴数更少的调度图 |
| ⑨ GenerateTasks | optimize_graph | 按场景（reduce/concat…）拆分 | `vector<ScheduleTask>` |
| ⑩ AutoScheduler | 每个 grouped_graph | 生成 tiling 候选 | `ScheduledResult` 列表 |
| ⑪ PrepareImplGraphMemoryPlan | FusedScheduledResult | 规划 TBuf/TQue 内存 | 带内存方案的图 |
| ⑬ CollectFusedIONodes | FusedScheduledResult | 收集 IO 节点 | input/output/workspace 节点 |
| ⑭ L2CacheHintManager | compute_graph | 计算 L2 cache 提示 | cache hint 写入结果 |
| ⑯ ExecSeqAdvancedOfLoad | FusedScheduledResult | load 前移藏延迟 | 调序后的执行序列 |

#### 4.2.4 代码实践

**实践目标**：用 DFX dump 产物，**肉眼验证**流水线各阶段的真实顺序与边界。

> 这个实践承接 [u3-l3 DFX 调测](u3-l3-enable-and-dfx.md) 的 `AUTOFUSE_DFX_FLAGS` 知识。`optimize.cpp` 里每一处 `DumpGraph` 的第二个参数（如 `"AfterGraphPass"`、`"AfterMergeAxis"`）都是一个阶段边界的「路标」。

**操作步骤**：

1. 准备一个能跑通的 Autofuse 用例（如 `autofuse/examples/pytorch/af_pointwise/af_add_ge.py`，见 [u3-l3](u3-l3-enable-and-dfx.md)）。
2. 设置环境变量打开 Autofuse 自身的 DFX dump（具体变量名与用法见 `autofuse/README.md` 的 DFX 章节；若变量名不确定，标注「待本地验证」）：
   ```bash
   export TORCH_COMPILE_DEBUG=1
   # export AUTOFUSE_DFX_FLAGS=<按 README 配置>
   ASCEND_DEVICE_ID=0 python autofuse/examples/pytorch/af_pointwise/af_add_ge.py
   ```
3. 在 `torch_compile_debug`（及 Autofuse 自身产物目录）下，按文件名时间或命名前缀找到 dump 出来的图文件。
4. 把下列「路标标签」按出现顺序排列，每找到一个就在它和下一个标签之间，标注对应的阶段编号（②~⑩）：
   - `AutoFuseBeforeRemoveDanglingNodes`
   - `AfterPreProcess`（仅 Inductor 来源会出现）
   - `AfterDtypeConsistency`
   - `AfterGraphPass`
   - `AfterMergeAxis`
   - `BeforeAutoSchedule`
   - `AutoFuseAfterOptimize`

**需要观察的现象**：相邻两个标签之间的图，应该能看出对应阶段的改造成果——例如 `AfterGraphPass` 之后若有重复的逐元素算子被合并（CSE），`AfterMergeAxis` 之后张量的轴数应**减少**。

**预期结果**：你得到一张「dump 文件 → 阶段编号」的对照清单，顺序与本节 4.2.2 的流程图一致。

> 若环境无法上板运行，可改为**源码阅读型**：在 `optimize.cpp` 中用搜索功能逐一定位上述 `DumpGraph` 标签的行号，并据此画出阶段顺序图，效果等价。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `PreProcess` 只对 `kFusedAscBackend` 生效？请从「来源差异」角度解释。

**参考答案**：`kFusedAscBackend` 来自 Inductor（torch.compile），其子图在进入 `Optimizer` 前可能带有标量广播缺失等 Inductor 特有的形态，需要 `PreProcess`（如 `scalar_broadcast_insert`）补齐；而 `kAscGraph`/`kFusedAscGraph` 来源在 GE 侧已基本规整。源码注释也写明「后续会全部放开」，说明这是过渡性区分。

**练习 2**：`AutoScheduler` 里对 `kComputeCube` 类型走了单独的 `ProcessCubeSchedules` 分支，跳过了 `AutoSchedule::DoAutoSchedule`。这对理解「cube 类算子」（matmul/conv2d）有什么启示？

**参考答案**：cube 类算子的 tiling 模型与 vector 类差异很大（涉及 M/N/K 分块、fixpip 等），不适合用通用的 `AutoSchedule` 轴循环模型去枚举 tiling 候选，因此走 `ProcessCubeSchedules` 直接套用 cube 专用模板（`CubeTemplateType`）。这正是后续 [u6-l4](u6-l4-taskgen-and-bufalloc.md) 与 v35 cube 算子（[u11-l2](u11-l2-cube-ops.md)）要展开的内容。

---

### 4.3 hint graph 与 fused graph 的差异

#### 4.3.1 概念说明

第 4.1 节埋了个伏笔：重载 A「展开 fused graph 成 hint graph，再调重载 B」。本节把这个「展开」讲透。

- **fused graph** 是框架侧给 `Optimizer` 的「打包快递」：一张 GE 计算图（`ComputeGraphPtr`），里面挂着一个或多个 `AscGraph`/`AscBackend` **子图节点**。框架并不关心调度细节，只负责把「要融合的算子区域」序列化塞进节点属性里。
- **hint graph** 是 `Optimizer` 自己拆包后得到的「单张调度图」：一张 `AscGraph`，带完整的 axis/stride/repeat 调度语义，是流水线真正能消化的形态。

所以 fused graph → hint graph 的本质是「**反序列化 +（必要时）展开合并**」。

#### 4.3.2 核心流程

重载 A 处理 fused graph 时，按子图数量分两种情况：

```
fused graph (ComputeGraphPtr)
  │
  ├─ 遍历所有节点，挑出 AscGraphNode，从其属性反序列化出 AscGraph
  │     → asc_backend_to_ascgraph (map: 节点 → 子图)
  │
  ├─ 子图数量 == 1 ?  直接把它当 hint graph
  │     否                用 FusedGraphUnfolder::UnfoldFusedGraph 展开/合并成单张 hint graph
  │
  └─ 调用重载 B: Optimize(hint_graph, ...)   # 复用 4.2 的完整流水线
```

关键直觉：**无论 fused graph 里有几个子图，最终都会被「摊平」成一张 hint graph，再走同一条流水线**。这是重载 A 能复用重载 B 的根本原因。

> 补充：`kFusedAscBackend`（Inductor）分支走 `OptimizeFusedAscBackend`，**不摊平**——它对每个 AscBackend 子图分别调 `OptimizeForHintGraph`，子图之间通过 workspace 节点连接。两种来源对「多子图」的策略不同（GE 摊平 vs Inductor 分治），这是设计上的取舍。

#### 4.3.3 源码精读

看重载 A 里「反序列化 + 展开」的关键段：

[autofuse/optimize/optimize.cpp:567-602](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.cpp#L567-L602) —— 遍历 fused graph 节点，对 `kAscGraphNodeType` 节点从 `ascgraph` 属性反序列化出 `AscGraph`；多于一个子图则 `UnfoldFusedGraph` 展开成单张 `hint_graph`，最后调用重载 B。

```cpp
std::map<af::Node *, af::AscGraph> asc_backend_to_ascgraph;
SizeVarSet original_var_set;
for (auto &node : fused_graph->GetDirectNodePtr()) {
  if (node->GetType() == kAscGraphNodeType) {              // GE 风格子图节点
    const std::string *serialized_ascgraph =
        af::AttrUtils::GetStr(node->GetOpDescBarePtr(), kAttrAscGraph);  // 取序列化串
    af::AscGraph ascgraph(graph_name.c_str());
    GE_CHK_STATUS_RET(af::AscGraphUtils::DeserializeFromReadable(*serialized_ascgraph, ascgraph), ...);
    ascgraph.SetGraphType(af::AscGraphType::kImplGraph);
    GE_CHK_STATUS_RET(AscGraphInfoComplete::CompleteApiInfo(ascgraph), ...);
    asc_backend_to_ascgraph.emplace(node, ascgraph);
  }
}
...
af::AscGraph hint_graph(fused_graph->GetName().c_str());
if (asc_backend_to_ascgraph.size() > 1UL) {
  GE_CHK_STATUS_RET(FusedGraphUnfolder::UnfoldFusedGraph(fused_graph, asc_backend_to_ascgraph, hint_graph), ...);
} else {
  hint_graph = asc_backend_to_ascgraph.begin()->second;     // 单子图直接用
}
...
GE_ASSERT_SUCCESS(Optimize(hint_graph, fused_scheduled_result), ...);   // ← 转交重载 B
```

几个要点：

1. **子图是「序列化串」藏在节点属性里**。`kAttrAscGraph`（值为 `"ascgraph"`，见文件头部匿名命名空间 [autofuse/optimize/optimize.cpp:44](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.cpp#L44)）是属性键，`DeserializeFromReadable` 负责把串还原成 `AscGraph`。这印证了 [u4-l2](u4-l2-tensor-attr-ascir.md) 讲的「属性存储」机制。
2. **节点类型是来源判别器**。GE 路径用 `kAscGraphNodeType`，Inductor 路径（`OptimizeFusedAscBackend`）用 `kAscBackendType` 并从 `AutoFuseAttrs` 取子图——两条来源取子图的协议不同（见 [autofuse/optimize/optimize.cpp:613-626](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.cpp#L613-L626)）。
3. **`UnfoldFusedGraph` 负责多子图摊平**。它把多个子图按外层 GE 图的连线拼接成单张 hint graph，细节在 `fused_graph/fused_graph_unfolder.h`（留待后续单元）。

#### 4.3.4 代码实践

**实践目标**：在源码中找到「多子图摊平」与「单子图直用」的两个分支，并理解它们的输入输出。

**操作步骤**：

1. 打开 `autofuse/optimize/optimize.cpp`，定位 `if (asc_backend_to_ascgraph.size() > 1UL)`（约 591 行）。
2. 阅读其 `true` 分支调用的 `FusedGraphUnfolder::UnfoldFusedGraph`，在 `autofuse/optimize/fused_graph/fused_graph_unfolder.h` 中找到它的签名，记录三个参数分别代表什么。
3. 阅读其 `else` 分支（`hint_graph = asc_backend_to_ascgraph.begin()->second;`），确认单子图场景直接复用反序列化结果，不做展开。

**需要观察的现象**：`UnfoldFusedGraph` 的入参同时包含「外层 fused graph」和「节点→子图 map」，说明摊平需要借助**外层图的连线信息**来决定子图如何拼接，而非简单地把子图并列。

**预期结果**：你能用一句话回答「fused graph 里有两个子图时，`Optimizer` 如何把它们变成一张 hint graph」——答：借助外层 GE 图的连线，用 `UnfoldFusedGraph` 把两个序列化子图拼接合并成单张 hint graph，再交给重载 B。

> 本实践为源码阅读型，无需上板运行。

#### 4.3.5 小练习与答案

**练习 1**：`kFusedAscBackend`（Inductor）分支为什么**不**把多个子图摊平成一张，而是逐子图调 `OptimizeForHintGraph`？

**参考答案**：Inductor 送来的多个 AscBackend 子图之间往往存在跨子图数据依赖，强行摊平成一张图会破坏 Inductor 既定的算子边界与内存复用假设；因此 `OptimizeFusedAscBackend` 选择**分治**——每个子图独立优化调度，再用 `SubgraphConnectionsToWorkspace` 在子图间插入 workspace 节点传递中间结果（见 `optimize.cpp:631`）。这是「保持框架语义」与「统一调度」之间的权衡。

**练习 2**：如果 fused graph 里**一个** `AscGraphNode` 都没有，`Optimizer` 会怎样？

**参考答案**：会断言失败。源码在反序列化循环后有一句 `GE_ASSERT_TRUE(!asc_backend_to_ascgraph.empty(), "The fused graph [...] is invalid, which has none AscBackend node.")`（`optimize.cpp:584`）。`Optimizer` 的输入契约就是「必须至少携带一个待融合子图」，空图属于非法输入。

---

## 5. 综合实践

**任务**：把本讲三块知识串起来，画一张「`Optimizer` 全景图」并标注一次真实调用的数据流。

具体要求：

1. 在一张图上同时画出：两个 `Optimize` 重载、`GraphType` 分叉、`OptimizeForHintGraph`（共享核心）、`AutoScheduler`、`BufQueAllocator`，以及 fused graph → hint graph 的「展开」箭头。
2. 用**红色**标出 `OptimizeForHintGraph` 被**两处**调用的位置（重载 B 一处、`OptimizeFusedAscBackend` 一处），印证它是共享核心。
3. 选一条具体路径（例如 GE 来源、单子图），用箭头标注数据形态的演变：`ComputeGraphPtr`(fused) → 反序列化 → `AscGraph`(hint) → `optimize_graph`(调度图副本) → `ScheduleTask` → `ScheduledResult` → `FusedScheduledResult`。
4. 在每个阶段旁标注它对应的 DFX dump 路标标签（来自 4.2.4），作为「可观测证据」。

**预期产出**：一张可交付的流程图 + 一段说明文字，解释「为什么两条来源（GE / Inductor）能共用 `OptimizeForHintGraph` 这一段，却又在前后处理上分叉」。

> 若想进一步加深理解，可以把这张图与 [u3-l2](u3-l2-autofuse-overview.md) 的 Autofuse 端到端数据流对照，指出 `Optimizer` 的输出 `FusedScheduledResult` 恰好是下游 ATT/codegen 的输入。

## 6. 本讲小结

- `Optimizer` 是 `optimize` 模块的**门面编排者**，对外只暴露 `Optimize` 一个动词，但有**两个重载**：收 fused graph（GE 计算图）的 A、收 hint graph（单张调度图）的 B。
- **重载 A 内部会调用重载 B**：fused graph 先经反序列化（必要时 `UnfoldFusedGraph` 摊平）变成 hint graph，再复用同一条流水线。Python 侧 `scheduleV2` → 重载 A，`schedule` → 重载 B。
- 流水线分上下半场：`OptimizeForHintGraph`（预处理 → dtype → Pass → 合轴 → 生成任务 → AutoSchedule）是**共享核心**，被两条来源共用；下半场做内存规划、UB 模板过滤、L2 cache hint、组并行、load 调序。
- 每个阶段边界都埋了 `DumpGraph`/`DumpScheduleResult` 路标（如 `AfterGraphPass`、`AfterMergeAxis`、`AutoFuseAfterOptimize`），可用 DFX 产物**肉眼验证**阶段顺序。
- `hint graph`（单张带调度语义）与 `fused graph`（GE 图含序列化子图节点）是两种形态；GE 来源摊平处理，Inductor 来源（`kFusedAscBackend`）分治处理，差异体现在 `OptimizeFusedAscBackend`。
- 最终产物 `FusedScheduledResult` 累加了全流程信息（调度结果 + 内存方案 + IO 节点 + cache hint），是下游 ATT/codegen 的直接输入。

## 7. 下一步学习建议

本讲只讲了「编排骨架」，流水线里每个阶段都是一座小山。建议按以下顺序继续：

1. **[u6-l2 图优化 Pass 体系](u6-l2-graph-pass-system.md)**：深入阶段 ⑦ `GraphPass`，看 `PassRunnerHandler` 如何调度 CSE、softmax 融合等具体 Pass。
2. **[u6-l3 AutoSchedule 自动调度与 tiling 生成](u6-l3-autoschedule.md)**：深入阶段 ⑩，理解 `AutoSchedule::DoAutoSchedule` 如何枚举 tiling 候选、做轴分组与对齐。
3. **[u6-l4 调度任务生成与内存分配](u6-l4-taskgen-and-bufalloc.md)**：深入阶段 ⑨⑪，看 `ScheduleTaskGenerator` 如何按 reduce/concat/transpose 场景拆任务，`BufQueAllocator` 如何分配 TBuf/TQue。
4. 若对 fused graph 的「摊平」机制感兴趣，可直接读 `autofuse/optimize/fused_graph/fused_graph_unfolder.h` 与 `fused_graph_modifier.h`。

> 阅读建议：带着本讲的「全景图」去读后续每一讲，每读深一个阶段，就在图上把那个方框「点亮」，直到整条流水线全部清晰。
