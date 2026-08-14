# AutoSchedule 自动调度与 tiling 生成

> 前置讲义：本讲承接 [u6-l1 Optimizer 总编排](u6-l1-optimizer-orchestration.md)。在 u6-l1 中我们看到 `Optimizer::OptimizeForHintGraph` 这条流水线的下半场会「逐任务 AutoSchedule」。本讲就拆开这「逐任务」里发生的事。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `AutoSchedule` 在 optimize 流水线中的位置、它要解决的问题，以及它「只生成候选、不选最优」的设计哲学。
- 解释「轴分组（AxisGroup / tiling group）」如何把图里杂乱的轴归类为 X / Y / R / N 四类，以及 `TilingCase` 如何由这些分组的笛卡尔积枚举出来。
- 说明「对齐处理（alignment）」为什么是 TilingCase 的硬约束，未通过对齐的候选如何被丢弃。
- 描述 `AutoSchedule` 的输出（`AutoScheduleOutput`）包含哪些信息，以及它如何被下游的 task 生成（u6-l4）与 ATT 自动 tiling（u7）消费。

## 2. 前置知识

在读本讲前，建议你已经理解：

- **融合图与调度图**：Autofuse 输入一张「融合计算子图」，要把多个相邻 Vector 算子缝进一个 kernel（见 [u3-l1](u3-l1-autofuse-principle.md)）。
- **ASCIR 视图**：全链路只有一份数据图，optimize/att/codegen 通过带调度语义的 `ASCIR` 视图访问它（见 [u4-l2](u4-l2-tensor-attr-ascir.md)）。本讲里的 `ImplGraph` / `AscGraph` 就是这份数据图的别名。
- **Optimizer 总编排**：u6-l1 讲过 `Optimizer` 把 hint graph 切成多个 `ScheduleTask`，再对每个任务调用 `AutoScheduler`。本讲精确到 `AutoScheduler` 内部。
- **两级存储与块对齐**：昇腾 AI Core 有全局内存（HBM）与片上统一缓冲（UB）两级存储；Vector 指令以 **32 字节（`ONE_BLK_SIZE`）为一个块** 对齐读写（见 [u5-l3](u5-l3-ascendc-api.md) 中 `BlkSize` / `RptSize` 的换算）。

#### 两个直觉：UB 切分 vs Block 切分

一个多轴计算要在硬件上跑起来，需要回答两个问题：

1. **UB 切分（tile split）**：UB 容量有限，一次放不下整个张量。需要把某个轴切成「外层循环（tile-outer，逐次搬运）」与「内层向量化（tile-inner，一次性放进 UB）」两段。这决定了**每次循环搬多少数据**。
2. **Block 切分（multi-core split）**：一块 AI Core 芯片有多个核（block）。需要把 tile-outer 再切给不同核并行。这决定了**哪个核算哪一段**。

本讲要讲的 `AutoSchedule`，核心就是「**自动决定沿哪些轴做 UB 切分、沿哪些轴做 Block 切分**」。而它给出的不是一个答案，而是**一组候选答案**，交给下游 ATT（u7）去用性能模型挑最优。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [autofuse/optimize/autoschedule/autoschedule.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/autoschedule.h) | `AutoSchedule` 类声明，本讲主角 |
| [autofuse/optimize/autoschedule/autoschedule.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/autoschedule.cpp) | `DoAutoSchedule` 主流程、TilingCase 枚举与剪枝 |
| [autofuse/optimize/autoschedule/autoschedule_defs.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/autoschedule_defs.h) | 输出结构 `AutoScheduleOutput` |
| [autofuse/inc/autoschedule/axis_group.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/autoschedule/axis_group.h) | 轴分组结构 `AxisGroup`（X/Y/R/N 四组） |
| [autofuse/optimize/autoschedule/tiling_group.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/tiling_group.h) / [.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/tiling_group.cpp) | 按算子类型生成并合并轴分组 |
| [autofuse/optimize/autoschedule/schedule.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/schedule.h) / [.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/schedule.cpp) | `Scheduler`：对单个 TilingCase 真正做 UB/Block 切分与对齐 |
| [autofuse/optimize/autoschedule/alignment_handler.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/alignment_handler.h) / [.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/alignment_handler.cpp) | 对齐处理，委托给平台策略 |
| [autofuse/optimize/optimize.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.cpp) | `Optimizer::AutoScheduler`，本讲与 u6-l1 的衔接点 |

---

## 4. 核心概念与源码讲解

### 4.1 AutoSchedule 调度模型

#### 4.1.1 概念说明

`AutoSchedule` 是 optimize 模块里「**自动调度**」这一阶段的实现者。它接收的是 `Optimizer` 已经切好的、单个任务内的一张 `ImplGraph`（融合子图带调度语义的视图），输出的不是一张图，而是**一组候选的「已调度图（scheduled graph）」**。

这里有个关键的设计哲学，理解它就理解了 AutoSchedule 的一半：

> **AutoSchedule 负责「生成候选」，ATT（u7）负责「挑选最优」。**

为什么不在这一步直接挑最优？因为「哪条轴切得最好」取决于具体的硬件耗时，而精确的耗时评估是 ATT 的成本模型（cost model）的工作。AutoSchedule 只做一件事：把**所有合理且不冲突**的切分方式都列出来，每种切分做成一份 scheduled graph，让 ATT 去打分。所以你会看到它的输出是 `std::vector<AutoScheduleOutput>`，一个 TilingCase 对应一份输出。

在 u6-l1 讲过的流水线里，`AutoSchedule` 出现在「生成 ScheduleTask」与「BufQueAlloc 内存分配」之间：

```
... 图改写 Pass → 合轴 → 生成 ScheduleTask → ★ 逐任务 AutoSchedule ★ → 内存规划 → L2 hint → 组并行 ...
```

对应到 [optimize.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.cpp) 的 `Optimizer::AutoScheduler`：它遍历任务里的每张 `grouped_graph`，构造一个 `AutoSchedule` 并调用 `DoAutoSchedule()`。

#### 4.1.2 核心流程

`DoAutoSchedule()` 的步骤可以画成一条流水线：

```
1. 标记图类型为 ImplGraph；对纯广播轴做重排优化
2. PrepareTilingCases：                ← 生成所有候选 TilingCase
     ├─ GenTilingGroup：按算子类型给轴分组，再合并成一张统一的 AxisGroup
     ├─ NormGroup：规范化分组
     ├─ GenTilingCase：X×Y×R 笛卡尔积枚举候选
     └─ PruneTilingCase：剪掉退化候选（如切 size=1 的轴）
3. 对每个 TilingCase 执行 ProcessOneTilingCase：
     ├─ 拷贝一份图
     ├─ 用 Scheduler 真正做 UB/Block 切分 + 对齐 + 缓存标记
     ├─ 若对齐失败（UNSUPPORTED）→ 丢弃这份候选
     └─ 选 loop 轴，把结果 push 进 schd_outputs_
4. TemplateGeneratorHandler::GenerateTemplates：按平台补充模板
5. （仅 cube=UBFuse 时）GenUBFuseTemplates：再派生一组 non-db 模板
```

要特别注意第 3 步的「丢弃」语义：**不是每个候选都活下来**。一份候选如果在 `Scheduler` 里对齐失败，会被静默跳过，最终输出的候选数可能少于枚举数。

#### 4.1.3 源码精读

先看主角的声明。`AutoSchedule` 把所有依赖通过构造函数注入，并禁用了默认构造：

[autofuse/optimize/autoschedule/autoschedule.h:23-58](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/autoschedule.h#L23-L58) —— `AutoSchedule` 类声明。关键点：
- 对外只暴露一个动词 `DoAutoSchedule()`（L36）；其余都是私有辅助。
- 构造参数里有三个「模板类型」开关：`is_reduce_first_stage`、`reduce_template`、`cube_template`。它们决定了走哪条枚举分支，下文 4.2 会展开。
- 成员 `axes_group_`（L54）是当前任务的统一轴分组，是 4.2 的主角。

`DoAutoSchedule()` 的本体很短，因为它把工作都下放给了几个辅助方法：

[autofuse/optimize/autoschedule/autoschedule.cpp:350-376](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/autoschedule.cpp#L350-L376) —— `DoAutoSchedule` 主流程。注意三处：
- L356 先 `PrepareTilingCases` 把候选清单造出来；
- L361-364 对每个候选调 `ProcessOneTilingCase`；
- L367 调 `TemplateGeneratorHandler::GenerateTemplates` 补平台模板。

本模块与 u6-l1 的衔接点在 `Optimizer::AutoScheduler`：

[autofuse/optimize/optimize.cpp:1086-1094](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.cpp#L1086-L1094) —— 在 `Optimizer::AutoScheduler` 内构造 `AutoSchedule`、调用 `DoAutoSchedule`，并断言 `schedule_outputs` 非空。注意它把 `schedule_task.reduce_type`（即 `ReduceTemplateType`）和 `schedule_task.cube_type`（即 `CubeTemplateType`）透传进来——这两个枚举就是上面提到的模板开关。

两个枚举的取值（用来理解分支条件）：

- [autofuse/optimize/optimize.h:32-37](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.h#L32-L37) —— `ReduceTemplateType`：`kDefault`（非 reduce）/ `kCommon`（通用）/ `kAllLoad`（全载）/ `kRCore`（R 轴切多核）。
- [autofuse/common/schedule_result.h:30-36](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/common/schedule_result.h#L30-L36) —— `CubeTemplateType`：`kDefault`（无 cube）/ `kFixpip` / `kCommon` / `kUBFuse` / `kL2Fuse`。

#### 4.1.4 代码实践

**实践目标**：确认 `AutoSchedule` 的「输入 → 输出」边界，并能指出它在 optimize 流水线里的精确位置。

**操作步骤**：
1. 打开 [autoschedule.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/autoschedule.h)，对照构造函数，列出它消费的 5 个输入（图、输出容器、`is_reduce_first_stage`、`reduce_template`、`cube_template`）。
2. 用 Grep 在 `optimize.cpp` 里搜索 `AutoSchedule(`，确认它在 `Optimizer::AutoScheduler`（约 L1087）被构造，且调用 `DoAutoSchedule()`（约 L1089）。
3. 阅读其紧邻的循环结构（`for (auto &grouped_graph : schedule_task.grouped_graphs)`），确认「逐任务、逐子图」调用。

**需要观察的现象**：每个 `grouped_graph` 都会得到一个**独立的** `std::vector<AutoScheduleOutput> schedule_outputs`，互不污染。

**预期结果**：你能用一句话概括——「`AutoSchedule` 把单张融合子图变成一组带切分语义的候选 scheduled graph」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DoAutoSchedule` 的输出是 `vector<AutoScheduleOutput>` 而不是单个 `AutoScheduleOutput`？

> **答案**：因为 AutoSchedule 只「生成候选」不「选最优」。一张子图沿不同轴切分会得到多份合理的 scheduled graph，全部交给下游 ATT 用成本模型挑选。所以输出是「一组候选」。

**练习 2**：`cube_template_ == kUBFuse` 时，`DoAutoSchedule` 末尾会额外调用 `GenUBFuseTemplates()`（[autoschedule.cpp:371-373](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/autoschedule.cpp#L371-L373)）。结合函数名（生成 `_non_db` 后缀的副本），猜猜「db / non-db」可能指什么？

> **答案**：`db` 通常指 double-buffer（双缓冲）。UBFuse 模板下会派生一组「非双缓冲」的副本候选，让 ATT 在「双缓冲换并行」与「不双缓冲省 UB」之间做权衡（具体语义待本地结合 codegen 确认）。

---

### 4.2 tiling group：轴分组与 TilingCase 生成

#### 4.2.1 概念说明

要枚举「沿哪些轴切」，第一步得先把图里杂乱的轴**归类**。这就是 **tiling group（轴分组）** 的工作。Autofuse 把每个算子的轴归到四类，用一个结构体 `AxisGroup` 表达：

| 分组 | 含义 | 典型来源 |
|------|------|----------|
| `y_group` | **elementwise 主轴**，UB 切分的默认候选 | elewise / load / store 算子的循环轴 |
| `r_group` | **Reduce 轴**，被归约（求和/取最大）的轴 | reduce 算子的输入比输出多出来的那些轴 |
| `x_group` | 需要和 y_group **分开切**的轴 | transpose 前后需要独立处理的轴 |
| `n_group` | **不可切分轴**，只能作向量化内层轴 | 受约束的小轴、Softmax 的尾轴等 |

此外还有 `axes_order`，记录「这些轴在张量里的原始相对顺序」，用于合并分组时保持轴序一致。

为什么要分组？因为融合 kernel 里**多个算子要共用同一套切分**——如果算子 A 想沿轴 2 切、算子 B 想沿轴 3 切，但它们被缝在同一个 kernel 里，就必须折中出一个对大家都成立的切分方案。所以「轴分组」是先**逐算子生成**、再**合并**成全图一致的方案。

分好组之后，`GenTilingCase` 做「**从每个可切分组里各取一个轴，组成一份切分方案**」的枚举——本质是笛卡尔积。若 X、Y、R 组分别有 \( |X|、|Y|、|R| \) 个轴，则通用候选数约为：

\[
\text{候选数} \approx |X| \times |Y| \times |R|
\]

（实际还会因 reduce-first-stage、cube、gather 等特化分支而变形，详见 4.2.3。）

#### 4.2.2 核心流程

tiling group 的生成与消费分四步：

```
① GenTilingGroup
   for 每个非 buffer 算子:
       GenAxisGroupForSingleNode  ← 按 compute_type 分发到 GenElewise/Reduce/Transpose/...TilingGroup
       收集该算子的 n_group
   合并所有算子的 X/Y/R 分组 → 全图统一 AxisGroup
② NormGroup                      ← 规范化分组
③ GenTilingCase                  ← 笛卡尔积枚举候选（含 reduce/cube/gather 特化）
④ PruneTilingCase                ← 剪掉退化候选
```

**按算子类型分发**是理解 ① 的关键。`GenAxisGroupForSingleNode` 用一张「计算类型 → 生成函数」的映射表分发：

| ComputeType | 生成函数 | 分组逻辑 |
|-------------|----------|----------|
| Elewise / Broadcast / Gather / Load / Store / Cube | `GenElewiseTilingGroup` | 循环轴直接进 y_group |
| Reduce | `GenReduceTilingGroup` | 循环轴拆成 y_group（保留）+ r_group（被归约） |
| Reduce（全载模式） | `GenReduceTilingGroupFullLoad` | 被归约轴进 n_group（不切） |
| Transpose | `GenTransposeTilingGroup` | 区分前/后轴，部分进 x_group |
| Concat / Split | `GenConcat/SplitTilingGroup` | 按拼接/切分维度特殊处理 |

#### 4.2.3 源码精读

先看四类分组的定义：

[autofuse/inc/autoschedule/axis_group.h:19-33](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/inc/autoschedule/axis_group.h#L19-L33) —— `AxisGroup` 结构。注释把四组的含义讲得很清楚：`x_group`「为了和 elemwise 的 ygroup 分开切分」（如 transpose 前后）、`y_group`「elemwise 轴」、`r_group`「Reduce 轴」、`n_group`「不可切分轴，或只能作向量化轴」。

再看最简单的 elewise 分组——所有循环轴都进 y_group：

[autofuse/optimize/autoschedule/tiling_group.cpp:509-516](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/tiling_group.cpp#L509-L516) —— `GenElewiseTilingGroup`：取算子的循环轴（`GetLoopAxis`）作为 `y_group`，`axes_order` 按自然顺序填充。

reduce 分组则要把「被归约的轴」挑出来。它通过比较 reduce 算子**输入**与**输出**的 stride，算出哪些轴在归约中被吃掉了：

[autofuse/optimize/autoschedule/tiling_group.cpp:518-536](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/tiling_group.cpp#L518-L536) —— `GenReduceTilingGroup`：`CalcReduceAxes` 算出 r_group，其余循环轴进 y_group；`axes_order` 把 y 轴排在前面、r 轴排在后面。

分发总表在这里——它就是 4.2.2 那张映射表的源码：

[autofuse/optimize/autoschedule/tiling_group.cpp:730-756](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/tiling_group.cpp#L730-L756) —— `GenAxisGroupForSingleNode`：用 `std::map<ComputeType, AxisGroupGenFunc>` 分发；全载模式（`is_reduce_ar_fullLoad`）优先走 `GenReduceTilingGroupFullLoad`。注意 Cube 类也算 elewise 分组（L741）。

逐算子生成后，`GenTilingGroup` 把它们合并成全图一份：

[autofuse/optimize/autoschedule/tiling_group.cpp:482-507](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/tiling_group.cpp#L482-L507) —— `GenTilingGroup`：遍历非 buffer 节点逐个生成、收集 n_group，最后用 `MergeAxesGroup` 把每个算子的 X/Y/R 合进 `tiling_group`。**合并失败会直接报错**（L501-503），意思是「这张融合图的轴无法统一切分，不能融合」——这是融合可行性的一道闸。

合并完成后，`PrepareTilingCases` 串起「生成→规范→枚举→剪枝」：

[autofuse/optimize/autoschedule/autoschedule.cpp:378-392](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/autoschedule.cpp#L378-L392) —— `PrepareTilingCases`。末尾 L388 断言「至少要有一个合法 TilingCase」，否则图非法。

**枚举的核心**在 `GenTilingCase`。它先处理三种特化分支（IndirectLoad / Cube / Gather），再走通用笛卡尔积：

[autofuse/optimize/autoschedule/autoschedule.cpp:279-297](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/autoschedule.cpp#L279-L297) —— `GenTilingCase` 的通用分支：三层 `for` 遍历 `x_group × y_group × r_group`，每个组合生成一个 `TilingCase`（设好 `ub_tiling_id_x/y/r` 与 `block_tiling_id=0`）。reduce-first-stage 时还会 `append_reduce_case` 追加一份「R 轴切多核」的候选（L255-260、L289-291）。

单个 `TilingCase` 长什么样？它是一份「**沿哪几条轴切**」的清单：

[autofuse/optimize/autoschedule/schedule.h:25-42](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/schedule.h#L25-L42) —— `TilingCase` 结构。注意它有两层信息：
- `*_id` 字段（如 `ub_tiling_id_y`）记录「沿哪个轴 id 切」——枚举阶段填；
- `*_tiling` 字段（如 `ub_tiling_y`，是 `pair<AxisPtr,AxisPtr>`）记录「切出来的外/内两条新轴」——等 `Scheduler` 真正执行切分时填。

最后是剪枝，去掉「切了等于没切」的退化候选：

[autofuse/optimize/autoschedule/autoschedule.cpp:300-320](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/autoschedule.cpp#L300-L320) —— `PruneTilingCase`：对单切分场景，若被切的轴 size 恒为 1 且候选数 > 1，就删掉这条候选（L311-314）。

#### 4.2.4 代码实践

**实践目标**：亲手验证「轴分组 → TilingCase 数量」的关系。

**操作步骤**：
1. 打开单元测试 [autofuse/tests/ut/optimize/autoschedule/test_reduce.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/tests/ut/optimize/autoschedule/test_reduce.cpp)。
2. 阅读 `Autoschedule_reduce_rara_tilingcase`（[L258-280](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/tests/ut/optimize/autoschedule/test_reduce.cpp#L258-L280)）：构造一张 RARA（Reduce 交替排列轴）图，依次调用 `GenTilingGroup` → `NormGroup` → `GenTilingCase`。
3. 对照断言 `ASSERT_EQ(tiling_cases.size(), 4)`，验证 4.2.1 的笛卡尔积公式。

**需要观察的现象**：第一条候选 `tiling_cases[0]` 的 `ub_tiling_id_x == -1`（无 X 组）、`ub_tiling_id_y == axes[1]`、`ub_tiling_id_r == axes[0]`、`reduce_is_block == false`——与「从 Y 组取 1、从 R 组取 1」的枚举结果吻合。

**预期结果 / 待本地验证**：RARA 图有 2 个 Y 轴、2 个 R 轴，笛卡尔积 \( 2 \times 2 = 4 \) 个候选，与断言一致。若你在本地修改 `Construct_Reduce_RARA` 增加一个 Y 轴，候选数应变为 6，可据此验证你对枚举的理解。

#### 4.2.5 小练习与答案

**练习 1**：`GenElewiseTilingGroup` 把循环轴全放进 `y_group`，而 `GenReduceTilingGroup` 把它们拆成 `y_group` 与 `r_group`。请解释为什么 reduce 算子需要单独的 r_group。

> **答案**：reduce 算子的输入比输出多出「被归约的轴」。这些轴在计算时需要跨轴累加（求和/取最大），切分方式与普通 elementwise 轴不同（例如 R 轴切多核时要保证每核的部分和能合并）。所以必须把它们单列为 r_group，让后续 `Scheduler` 用专门的 reduce 切分逻辑处理。

**练习 2**：`GenTilingGroup` 在合并时若 `MergeAxesGroup` 失败会直接 `GE_ASSERT_TRUE` 报错（[tiling_group.cpp:501-503](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/tiling_group.cpp#L501-L503)）。这反映了 Autofuse 的哪条设计约束？

> **答案**：融合的可行性约束——被缝进同一个 kernel 的算子必须能共享一套轴切分。如果各算子的轴分组无法合并（轴序冲突、切分要求矛盾），这张图就「不可融合」，直接报错而不是生成错误代码。这与 u6-l2 讲的「图改写确定性」红线一致。

---

### 4.3 对齐处理与单候选的真正切分

#### 4.3.1 概念说明

到目前为止，`TilingCase` 只是一份「沿哪条轴切」的**意图**。要变成一份真正能生成代码的 scheduled graph，还得由 `Scheduler` 把意图**落地**：真正在图上把轴一分为二（tile-outer / tile-inner），并把切分应用到每个算子节点。

而落地过程中有一道**硬约束——对齐（alignment）**：

> AscendC Vector 指令按 32 字节（`ONE_BLK_SIZE`）的块对齐读写。如果某个 TilingCase 切出来的向量化 stride 不对齐，代码要么无法生成，要么需要 mask/补齐导致性能劣化。

所以 `Scheduler` 在切分后会调用 `AlignmentHandler` 检查对齐。**对齐失败 → 该候选返回 `UNSUPPORTED` → 被 `ProcessOneTilingCase` 丢弃**。这是候选被「自然淘汰」的主要机制。

`Scheduler::DoScheduler` 的流水线（精简）：

```
RemoveDuplicatedAxisFromGroup
TileSplit              ← UB 切分：把选定轴切成 tile-outer/tile-inner
BlockSplit             ← Block 切分：把 tile-outer 再切给多核
ApplyBlockSplit        ← 把切分应用到每个节点
RemoveRedundantBroadcast
AlignVectorizedStrides ← 对齐检查：失败则整份候选作废（返回 UNSUPPORTED）
NodeCacheMarker        ← 标记哪些中间结果需要缓存进 UB
ModifyVectorizedStrides← 对齐优化
```

#### 4.3.2 核心流程

`ProcessOneTilingCase` 是「意图 → 落地 → 收集」的总管：

```
拷贝一份图（CopyFrom）→ 构造 Scheduler → DoScheduler()
   ├─ 返回 UNSUPPORTED → 记日志、跳过这份候选（不计入输出）
   └─ 成功 → 若 reduce_is_block 则 BindBlock 并记录 var_relations
SelectLoopAxis（为每个算子选定 loop 轴）
emplace_back 进 schd_outputs_
```

`AlignmentHandler` 本身只是个**门面**——它不实现具体对齐逻辑，而是从 `PlatformFactory` 取当前平台的对齐策略（`AlignmentStrategy`）来执行。这是因为不同芯片代（v1=2201、v2=950/v35）的对齐能力不同，逻辑被下沉到平台层（与 u6-l2 讲的 Pass runner 平台分发同理）。

#### 4.3.3 源码精读

先看 `Scheduler` 的切分入口——它把 UB 切分与 Block 切分串起来，对齐在其中起「一票否决」作用：

[autofuse/optimize/autoschedule/schedule.cpp:770-805](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/schedule.cpp#L770-L805) —— `Scheduler::DoScheduler`。注意三处：
- L779 `TileSplit()` 做 UB 切分；L783-786 `BlockSplit` + `ApplyBlockSplit` 做多核切分；
- L789-792 `AlignmentHandler::AlignVectorizedStrides` 是**关键否决点**——失败时直接 `return align_ret`（即 `UNSUPPORTED`），让上层丢弃这份候选（注释 L791 明说「返回 UNSUPPORTED 让上层跳过这个模板」）；
- L793 `NodeCacheMarker` 标记缓存，L794 `ModifyVectorizedStrides` 做对齐优化。

对齐门面的实现极其简洁——纯粹委托给平台策略：

[autofuse/optimize/autoschedule/alignment_handler.cpp:14-28](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/alignment_handler.cpp#L14-L28) —— `AlignmentHandler::AlignVectorizedStrides` 与 `ModifyVectorizedStrides` 都走「`PlatformFactory::GetPlatform()` → `GetAlignmentStrategy()` → 调对应方法」。具体对齐规则在各平台的 `AlignmentStrategy` 子类里。

候选落地与丢弃的总管在 `ProcessOneTilingCase`：

[autofuse/optimize/autoschedule/autoschedule.cpp:394-427](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/autoschedule.cpp#L394-L427) —— `ProcessOneTilingCase`。注意：
- L398 `CopyFrom` 拷一份图，保证每个候选互不干扰；
- L408-412 `scheduler.DoScheduler()` 若返回 `UNSUPPORTED`，仅 `GELOGW` 警告并 `return SUCCESS`——**这份候选不被加入 `schd_outputs_`**，等价于丢弃；
- L415-420 reduce 切多核时，把 `Rm_org_size` / `A_org_size` 写进 `var_relations_`，供下游生成 tiling data；
- L422 `SelectLoopAxis` 给每个算子定 loop 轴；L425 才把幸存的候选 `emplace_back`。

候选被丢弃是「静默」的——这解释了为什么最终候选数（`schd_outputs_.size()`）常常少于枚举数。

最后，看 `Scheduler` 把 TilingCase 的「轴 id」翻译成「切出来的轴对」的小工具，它体现了 `TilingCase` 两层字段（id 与 tiling 对）的衔接：

[autofuse/optimize/autoschedule/schedule.h:91-102](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/schedule.h#L91-L102) —— `TileTiling` 调 `graph_.TileSplit(tile_id)` 把一条轴切成一对（外/内），`ApplyTiling` 再调 `graph_.ApplySplit` 把切分作用到具体节点。

#### 4.3.4 代码实践

**实践目标**：理解「对齐失败 → 候选丢弃」这一淘汰机制，以及它在代码里的体现。

**操作步骤**：
1. 在 [schedule.cpp:770-805](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/schedule.cpp#L770-L805) 定位 L789-792 的对齐检查与提前返回。
2. 在 [autoschedule.cpp:408-412](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/autoschedule.cpp#L408-L412) 确认 `UNSUPPORTED` 时函数直接 `return SUCCESS` 而**不** `emplace_back`。
3. 对比「枚举数」与「输出数」：在 `test_reduce.cpp` 的 `Autoschedule_reduce_rara_fusion`（[L282-293](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/tests/ut/optimize/autoschedule/test_reduce.cpp#L282-L293)）里，`DoAutoSchedule()` 后 `impl_graphs.size() == 4`，与 `tilingcase` 测试里的枚举数 4 一致——说明此例无候选被丢弃。

**需要观察的现象**：枚举阶段（`GenTilingCase`）产出的候选数，可能大于最终 `schd_outputs_.size()`，差额就是被对齐淘汰的。

**预期结果 / 待本地验证**：若你能在本地构造一张「某轴 size 非 32 整数倍」的图并跑 `DoAutoSchedule`，应能观察到某些候选被 `GELOGW` 跳过、`impl_graphs.size()` 小于枚举数。

#### 4.3.5 小练习与答案

**练习 1**：`AlignmentHandler` 为什么不自己实现对齐逻辑，而是委托给平台的 `AlignmentStrategy`？

> **答案**：不同芯片代（v1 与 v2/v35）的对齐能力与约束不同（如是否支持非对齐 load、补齐粒度）。把对齐规则下沉到平台层，可以让同一套 AutoSchedule 调度框架跨平台复用，只需替换 `AlignmentStrategy` 子类——这与 u6-l2 的 Pass runner 平台分发是同一种「公共框架 + 平台策略」设计。

**练习 2**：`ProcessOneTilingCase` 在 `DoScheduler()` 返回 `UNSUPPORTED` 时，为何返回 `SUCCESS` 而不是把错误往上抛？

> **答案**：`UNSUPPORTED` 表示「**这份**候选不可行」，不是「整张图不可调度」。丢弃这份候选、继续尝试其它候选才是正确行为；返回 `SUCCESS` 让 `DoAutoSchedule` 的循环继续。只有当**所有**候选都被丢弃、`schd_outputs_` 为空时，`Optimizer::AutoScheduler` 的断言（`optimize.cpp` L1091）才会真正报错。

---

## 5. 综合实践

**任务**：画出「一张融合子图 → 一组 scheduled graph」的完整数据流，并把 `AutoScheduleOutput` 的三个字段逐一对应到它如何影响下游。

**步骤**：

1. **输入端**。从 [optimize.cpp:1086-1090](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.cpp#L1086-L1090) 出发，标出 `AutoSchedule` 的输入是 `grouped_graph`（单任务子图）+ 两个模板开关。

2. **输出端**。打开 [autoschedule_defs.h:16-23](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/autoschedule_defs.h#L16-L23)，列出 `AutoScheduleOutput` 的三个字段：
   - `scheduled_graph`（`AscGraph`）：带切分语义的图——下游 codegen（u8）据此生成 kernel；
   - `var_relations_`（`map<string, Expression>`）：变量名到符号表达式的映射——下游生成 tiling data 时用（reduce 切多核的 `Rm_org_size`/`A_org_size` 就在这里）；
   - `score_func`（`string`）：打分函数名——ATT（u7）用它给这份候选打分排序。

3. **填充点**。对照 [autoschedule.cpp:394-427](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/autoschedule.cpp#L394-L427)，标注：`scheduled_graph` 由 `CopyFrom` + `Scheduler.DoScheduler` 填；`var_relations_` 由 reduce 切多核分支（L418-419）填；`score_func` 在本函数里**不**填，留空，由后续 `TemplateGeneratorHandler::GenerateTemplates` 与 `Optimizer` 的打分函数注册（`RegisterScoreFuncInScheduleGroup`，`optimize.cpp` 约 L329）填充。

4. **下游衔接**。追踪 [optimize.cpp:1095-1104](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.cpp#L1095-L1104)：`schedule_outputs` 被转换成 `ScheduledResult`（见 [schedule_result.h:38-46](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/common/schedule_result.h#L38-L46)，含 `schedule_groups` / `var_relations` / `score_func`），再交给 u6-l4 的 task 生成与 buffer 分配，最终到 ATT/codegen。

**产出物**：一张数据流图 + 一张「字段 → 填充位置 → 下游消费者」三列表。这张表就是本讲与 u6-l4（task 切分）、u7（ATT）的接口契约。

## 6. 本讲小结

- `AutoSchedule` 是 optimize 流水线「逐任务调度」的实现者，输入单张融合子图，输出**一组**候选 scheduled graph——它只生成候选，挑选最优交给 ATT（u7）。
- 轴分组（`AxisGroup`）把杂乱的轴归为 **Y（elementwise）/ R（reduce）/ X（独立切）/ N（不可切）** 四类，先逐算子按 `compute_type` 生成、再合并成全图一致方案；合并失败即「不可融合」。
- `TilingCase` 由分组的笛卡尔积（\( |X|\times|Y|\times|R| \)）枚举，再经 `PruneTilingCase` 剪掉退化候选；每个 `TilingCase` 是一份「沿哪几条轴切」的意图清单。
- `Scheduler` 把意图落地为真正的 UB 切分 + Block 多核切分；其中 `AlignmentHandler` 的对齐检查是**一票否决**——对齐失败的候选返回 `UNSUPPORTED` 被 `ProcessOneTilingCase` 静默丢弃。
- `AutoScheduleOutput` 三字段（`scheduled_graph` / `var_relations_` / `score_func`）分别服务 codegen、tiling data 生成、ATT 打分，经 `Optimizer::AutoScheduler` 转成 `ScheduledResult` 流向 u6-l4 的 task 切分。

## 7. 下一步学习建议

- **[u6-l4 调度任务生成与内存分配](u6-l4-taskgen-and-bufalloc.md)**：本讲产出的 `ScheduledResult` 如何被 `ScheduleTaskGenerator` 针对 concat/split/reduce/transpose 等场景细化成最终任务，以及 `BufQueAllocator` 如何据切分结果分配 TBuf/TQue。这是 AutoSchedule 输出的直接下游。
- **[u7 ATT 自动 Tiling](u7-l1-att-perf-modeling.md)**：本讲「生成候选」的另一半——ATT 如何用成本模型（`gen_model_info` / `api_perf_register`）给每份候选的 `score_func` 打分、求解最优 tiling 参数。
- **进阶源码**：想深入「合并分组」的细节，可读 `TilingGroup::MergeAxesGroup`（[tiling_group.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/autoschedule/tiling_group.cpp)）与平台策略 `AlignmentStrategy`；想理解广播轴重排，可读 `autoschedule.cpp` 顶部的匿名命名空间 `ReorderBroadcastAxesInner`。
