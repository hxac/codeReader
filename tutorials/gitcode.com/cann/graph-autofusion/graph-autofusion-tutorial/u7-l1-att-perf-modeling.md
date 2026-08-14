# ATT 性能建模与 gen_model_info

> 前置承接：本讲是 Autofuse 数据流 `graph_metadef → ascir → optimize → att → codegen → compiler` 中 **att 阶段**的开篇。在 [u6-l3](u6-l3-autoschedule.md) 里，`AutoSchedule` 已经为一张融合子图枚举出若干**候选 scheduled graph**（不同的切轴方案），但 AutoSchedule 只「生成候选、不选最优」。那么「选最优」靠什么？答案是：为每个候选建立一张**性能模型（cost model）**，让下游求解器按模型打分挑出最快的 tiling。本讲就讲清楚这张性能模型是怎么建出来的。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 ATT 为什么需要性能建模，以及它把「图 + 切轴方案」翻译成了什么形式的数学问题；
- 读懂 `gen_model_info.h` 暴露的接口，能说出 ATT 的**输入**是什么、输出的 `ModelInfo` 里都装了哪些信息；
- 理解 `api_perf_register` 子模块如何用「自注册 + 线性/带宽 cost 表」为每个算子提供耗时公式，并能指出 **cost model 的数据**究竟来自哪个子模块；
- 能够沿着源码跟踪「一个算子节点 → 它的耗时表达式」这条链路。

## 2. 前置知识

阅读本讲前，先建立两个直觉。

**直觉一：什么是「Pipe（流水线）」。**
昇腾 AI Core 上 Vector 类算子的执行被拆成多条并行的硬件流水线（pipe）。本讲会频繁出现这几个 `PipeType` 枚举（[autofuse/att/base/base_types.h:56-65](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/base/base_types.h#L56-L65)）：

| PipeType | 含义（直觉） |
|----------|--------------|
| `AIV_MTE2` | Vector 侧把数据从全局内存搬进片上缓冲（GM→UB） |
| `AIV_VEC`  | Vector 计算单元真正做运算（Add/Mul/Cast…） |
| `AIV_MTE3` | Vector 侧把结果从片上缓冲搬回全局内存（UB→GM） |
| `AIC_FIXPIPE` / `AICORE_MTE1` / `AICORE_MTE2` | Cube 侧的相关流水线（v35 matmul 等用到） |

关键认识：一个算子节点的耗时**不是单一数字**，而是「按 pipe 分桶」的若干个表达式。一条 pipe 内的耗时是该 pipe 上所有节点贡献之和，不同 pipe 之间则会受硬件并行/抢占影响（下一讲的求解器会综合这些桶）。

**直觉二：为什么耗时要用「符号表达式（Expr）」而不是先算成数字。**
Autofuse 支持**动态 shape**——编译期并不知道运行时 tensor 的具体维度。因此 cost model 不能先算出一个数，而是要产出一个**关于未知变量（轴大小、block_dim 等）的符号表达式**。最终在运行时拿到真实 shape 再代入求值。所以你会看到 `Expr`、`Mul`、`Div`、`af::sym::Ceiling` 这类符号运算遍布性能公式，它们其实是「在拼公式」，而不是「在做算术」。

> 你可以暂时把 `Expr` 当成「一段待求值的数学公式」，把 `Div(a, b)` 当成 `a / b`。具体符号引擎细节不影响本讲主线。

## 3. 本讲源码地图

本讲聚焦 `autofuse/att/gen_model_info/` 子目录，外加它的输出类型与 cost 数据源：

| 文件 | 作用 |
|------|------|
| [autofuse/att/gen_model_info/gen_model_info.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/gen_model_info.h#L23-L38) | ATT 对外的顶层接口声明（多个 `GenerateModelInfo` 重载） |
| [autofuse/att/gen_model_info/gen_model_info.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/gen_model_info.cpp#L173-L205) | `GenerateModelInfo` 实现：parser → expr 生成 → pass 三步 |
| [autofuse/att/base/model_info.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/base/model_info.h#L279-L314) | `ModelInfo` 结构——ATT 输出的「性能 + 约束」总账本 |
| [autofuse/att/gen_model_info/parser/ascend_graph_parser.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/parser/ascend_graph_parser.h#L26-L31) | `AscendGraphParser`：把 `AscGraph` 翻译成建模输入 `TuningSpace` |
| [autofuse/att/gen_model_info/expr_gen/pipe_perf_expr.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/expr_gen/pipe_perf_expr.cpp#L547-L585) | `PipePerfExpr`：遍历节点、查 cost 函数、把耗时表达式累加进 `objects` |
| [autofuse/att/gen_model_info/api_perf_register/ascendc_api_perf.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/ascendc_api_perf.h#L22-L68) | `EvalCosts` 单例 + 注册宏：cost 函数的注册与查询 |
| [autofuse/att/gen_model_info/api_perf_register/api_perf_factory.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/api_perf_factory.h#L24-L75) | `ApiPerfFactory` + `ApiPerfRegister`：把算子名映射到 cost 实现类 |
| [autofuse/att/gen_model_info/api_perf_register/v1/perf_param_v1.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/v1/perf_param_v1.cpp#L68-L562) | **cost 数据源**：v1 平台各算子的线性/带宽系数（JSON） |

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：**性能建模思想**、**gen_model_info 接口**（含 parser）、**api_perf_register（cost 注册与数据）**。

### 4.1 性能建模思想：把 tiling 选择变成「带约束的优化问题」

#### 4.1.1 概念说明

回看 u6-l3：`AutoSchedule` 给一张融合子图枚举了多个候选（沿不同轴切分、不同的 tile 大小），并附带一个 `score_func`。但「哪个候选最快」这个问题，AutoSchedule 自己不回答——它只把候选摊在桌上。

ATT（Autofuse 中负责**自动 Tiling** 的子模块）的工作，就是为每个候选建立一张**可求解的性能模型**，让下游求解器（下一讲 u7-l2 的 expr_gen / solver）能算出「在该 tiling 下，这个融合 kernel 大约要跑多少 cycle」，从而选出最优 tiling。

性能建模要同时表达两件事：

1. **目标（objective）**：这个 tiling 方案大概多耗时。ATT 用「按 pipe 分桶的 cycle 表达式」来表达——即 `ModelInfo::objects`（`map<PipeType, Expr>`）。
2. **约束（constraints）**：硬件限制和切轴语义限制。例如片上 UB 容量上限、轴之间的整除关系（`NO_TAIL`）、对齐要求等。这些进 `hardware_cons` / `eq_exprs` / `leq_exprs`。

把目标和约束写齐，就得到一个「**最小化执行时间，受限于硬件与对齐约束**」的优化问题。注意：由于 shape 动态，目标和约束此刻都还是**符号表达式**，求解发生在后面。

#### 4.1.2 核心流程：一次算子耗时如何累加成 pipe 成本

单个算子节点的耗时由两部分组成：**单次调用耗时**（来自 cost model）乘以**调用次数**（外层循环展开了多少次）。设节点 \(n\) 在 pipe \(p\) 上的单次耗时为 \(c_{n,p}\)，它的执行次数为 \(t_n\)，则该 pipe 的总耗时为：

\[
\text{Cost}_p \;=\; \sum_{n} t_n \cdot c_{n,p} \;+\; H_p
\]

其中：

- \(c_{n,p}\) 来自算子的 cost model（4.3 详述）。对 elementwise 类算子是一个线性公式：

\[
c_{n,p} \;=\; k \cdot \text{repeat\_time} + b
\]

  \(k\) 是每处理「一个 repeat 数据量」的 cycle 成本，\(b\) 是指令启动开销，\(\text{repeat\_time}\) 是该节点单次调用处理的 repeat 数。

- \(t_n\) 是节点外层循环的总次数，等于该节点各循环轴（loop axis）大小的连乘积。

- \(H_p\) 是整条 pipe 的**启动头开销**（pipe head cost），例如 MTE2 的头开销随核数 `block_dim` 线性增长。

最终 `Cost_p` 对每个 `PipeType` 得到一个表达式，全部存进 `ModelInfo::objects`。这条公式在本讲 4.2.3 的源码里能逐字找到对应。

#### 4.1.3 源码精读：cost 表里的两类数学模型

下面是 cost 数据（[autofuse/att/gen_model_info/api_perf_register/v1/perf_param_v1.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/v1/perf_param_v1.cpp#L84-L90)）中 `Add` 算子的条目，它正是 4.1.2 公式里线性模型的系数来源：

```json
"Add": {
    "model_type": "SimpleLinear",
    "model_params": {
        "float16tofloat16": {"k": 0.0103, "b": 22.2173},
        "float32tofloat32": {"k": 0.0206, "b": 23.2225}
    }
}
```

读法：`Add` 算子是 `SimpleLinear`（简单线性）模型，按「输入 dtype→输出 dtype」查到 \(k\)、\(b\) 两个系数——fp16 下 \(k=0.0103, b=22.2173\)，fp32 下 \(k\) 翻倍（因为一个 repeat 处理的元素数减半）。把这两个数代入 \(c = k\cdot\text{repeat\_time}+b\) 即得单次调用耗时。

另一类是搬运类（Load/Store）的**带宽模型**，见 `Load` 条目（[同文件:L487-L496](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/v1/perf_param_v1.cpp#L487-L496)）：

```json
"Load": {
    "model_type": "LoadStoreFunc",
    "model_params": {
        "float32tofloat32": {"h": 27.01, "a": 9.9074, "b": 15.8960, "hl": 0, "data_type_size": 4},
        ...
    }
}
```

对应源码注释里的公式（[autofuse/att/gen_model_info/api_perf_register/v1/ascir_api_perf_v1.cpp:L177-L190](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/v1/ascir_api_perf_v1.cpp#L177-L190)）：

\[
T = a + \frac{b}{\text{block\_dim}}, \qquad \text{mte2} = \frac{S}{T} + h
\]

其中 \(S\) 是搬运字节数，\(T\) 是单核峰值带宽（核越多、抢占越严重，故随 `block_dim` 递减并收敛到 \(a\)），\(h\) 是单次调用头开销。这个 `block_dim` 随核数变化的建模，正是 4.1.2 公式中 \(H_p\)（pipe 头开销）随核数增长的根源，见 [perf_param_v1.cpp:L48-L50](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/v1/perf_param_v1.cpp#L48-L50)。

#### 4.1.4 代码实践：用 cost 表预估一个 Add 节点的耗时

1. **实践目标**：不跑代码，仅凭 cost 表手算一个 Add 节点的「单次调用」耗时，建立对线性模型的直感。
2. **操作步骤**：
   - 打开 [perf_param_v1.cpp:L84-L90](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/v1/perf_param_v1.cpp#L84-L90)，取 fp16 的 \(k=0.0103, b=22.2173\)。
   - 假设该 Add 单次调用处理 `repeat_time = 64`（即一次 repeat 处理 64 个 fp16 元素）。
3. **需要观察的现象**：代入 \(c = 0.0103 \times 64 + 22.2173\)。
4. **预期结果**：\(c \approx 22.88\) cycle。可见 \(b\)（启动开销）占了绝大多数——这正是 elementwise 算子「计算便宜、调度头开销显眼」的特征，也是 Autofuse 要把它们融合在一起减少调用次数的动机。
5. 本结果为按公式的手工估算，实际 cycle 受硬件流水线并行影响，**待本地用 profiling 进一步验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 cost model 不直接返回一个 `double`，而是返回 `Expr`？
**答案**：因为 Autofuse 要支持动态 shape，编译期不知道轴的具体大小，必须把耗时写成「关于未知变量的符号表达式」，留到运行时拿到真实 shape 再代入求值。

**练习 2**：fp16 与 fp32 下 Add 的 \(k\) 系数为什么大约是 1:2 的关系？
**答案**：硬件一次 repeat 处理固定的**字节数**，fp32 单元素字节数是 fp16 的两倍，故同样 repeat 下能处理的 fp32 元素数减半，每元素的 cycle 成本 \(k\) 相应约翻倍。

---

### 4.2 gen_model_info 接口：ATT 的入口与输出总账本

#### 4.2.1 概念说明

`gen_model_info` 是 ATT 对外的**顶层入口**。它把一个（或一组）已经带好调度语义的 `AscGraph`（即 u6-l3 产出的候选 scheduled graph），翻译成一份 `ModelInfo`——后者是 4.1 说的「目标 + 约束」总账本，直接喂给下一讲的求解器。

这里的输入 `AscGraph` 就是 [u4-l2](u4-l2-tensor-attr-ascir.md) 讲过的 ASCIR 视图：它搭在核心 `ComputeGraph/Node/Anchor` 之上（`AscNode : public Node`），只是带上了轴/步幅/重复等调度语义。所以 ATT 并不另造一张图，而是在同一份数据上做建模。

#### 4.2.2 核心流程：parser → expr 生成 → pass 三步

单图入口 `GenerateModelInfo(graph, model_info, tuning_space, tiling_case_id)` 固定三步（见 [gen_model_info.cpp:L173-L205](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/gen_model_info.cpp#L173-L205)）：

```text
step1  AscendGraphParser.GraphParser(graph)
         → 把 AscGraph 翻译成 TuningSpace（节点/轴/tensor/container）
step2  GenerateTilingExpr.Generate(model_info)
         → 生成约束表达式 + 调用 PipePerfExpr 生成 pipe 性能表达式（objects）
         → RefreshCommonUbExprContext 刷新 UB 容量约束
step3  ATTPassMgr 逐个跑 pass，收集 config
```

而上游真正调用的是**批量重载**（带 `options`、`enable_group_parallel` 等，[gen_model_info.cpp:L407-L452](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/gen_model_info.cpp#L407-L452)）：它对一组候选图逐个调上面的单图函数，再叠加轴排序（`ArgListReorder`）、高阶 API tiling 信息（`GetApiTilingInfo`）、可选的 dump（`kDumpDebugInfo`）。最终每个候选图得到一个 `ModelInfo`，组成 `model_info_list`。

#### 4.2.3 源码精读

**入口接口**（[gen_model_info.h:L24-L38](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/gen_model_info.h#L24-L38)）声明了一组重载：单图版（L24-25）、批量版（L26）、带选项与 group 并行的批量版（L27-31），外加 `GetModelInfoMap`（L32-34，把整个 `FusedScheduledResult` 展开成多层 model_info）、`MakeJson`（L38，序列化）。

**step1 parser**：`AscendGraphParser`（[ascend_graph_parser.h:L26-L31](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/parser/ascend_graph_parser.h#L26-L31)）只对外暴露 `GraphParser(graph)`，内部把 `AscGraph` 的轴（`ParserOriginAxis`）、调度信息（`ParserSchedInfo`）、tensor 内存（`ConstructQueueContainer` 等）逐项解析进 `TuningSpace`（`ConvertToTuningSpace`，[L97](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/parser/ascend_graph_parser.h#L97)）。`TuningSpace` 就是建模的中间表示——`PipePerfExpr` 后续直接遍历它的 `node_infos`。

**step2 性能表达式落地**：`GenerateTilingExpr::Generate` 内部调用 `GetPipePerformance(model_info.objects, ...)`（[generate_tiling_expr.cpp:L612-L614](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/expr_gen/generate_tiling_expr.cpp#L612-L614)），它把算出的各 pipe 耗时直接写进 `ModelInfo::objects`。这一步的内部实现是 `PipePerfExpr::GetPerfExpr`（[pipe_perf_expr.cpp:L553-L585](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/expr_gen/pipe_perf_expr.cpp#L553-L585)）：

```cpp
for (const auto &node : tuning_space_->node_infos) {
  ...
  GE_ASSERT_SUCCESS(GetNodeExeTime(node, exe_time_mgr, node_exe_times), ...);   // 求 t_n（调用次数）
  GE_ASSERT_SUCCESS(GetNodePerfInternal(node, node_perf, ...), ...);            // 求 c_{n,p}（单次耗时）
  GE_ASSERT_SUCCESS(AddNodePerfToPipeCost(node, exe_var, node_perf, ...), ...); // 累加 t_n * c_{n,p}
}
GE_ASSERT_SUCCESS(UpdatePipeHead(pipe_costs, ternary_ops));                      // 加上 pipe 头开销 H_p
```

其中 `AddPerf`（[pipe_perf_expr.cpp:L476-L498](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/expr_gen/pipe_perf_expr.cpp#L476-L498)）把 `node_exe_times * node_perf` 累加进对应 pipe——这正是 4.1.2 公式的源码落点；`UpdatePipeHead`（[L536-L545](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/expr_gen/pipe_perf_expr.cpp#L536-L545)）再叠加 \(H_p\)。

**输出 `ModelInfo`**（[model_info.h:L279-L314](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/base/model_info.h#L279-L314)）关键字段一览：

| 字段 | 类型 | 含义 |
|------|------|------|
| `objects` | `map<PipeType, Expr>` | **目标表达式**：每个 pipe 的总耗时（4.1.2 公式结果） |
| `hardware_cons` | `map<HardwareDef, Expr>` | 硬件约束，关键是 `UB` 片上容量上限 |
| `eq_exprs` / `leq_exprs` | map | 切轴的等式（整除）/不等式约束 |
| `arg_list` | `vector<AttAxisPtr>` | 待求解的轴及其 size 表达式（求解变量） |
| `workspace_size_map` | `map<int64_t, Expr>` | 各 workspace tensor 的大小 |
| `head_cost` | `Expr` | 多核头开销 |
| `tiling_case_id` / `graph_name` / `score_func` | 标量 | 候选标识与打分函数名 |
| `perf_breakdowns` | `vector<PerfBreakdownGroup>` | 性能公式的**语义化拆解**（供 DFX 阅读） |

一句话：**`objects` 是「要最小化的目标」，其余多为「求解时必须满足的约束」**。

#### 4.2.4 代码实践：跟踪 objects 的填充链

1. **实践目标**：把「一个节点 → `ModelInfo.objects`」的调用链亲手走一遍。
2. **操作步骤**：
   - 从 [gen_model_info.cpp:L184-L185](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/gen_model_info.cpp#L184-L185) 的 `GenerateTilingExpr.Generate` 出发；
   - 跳到 [generate_tiling_expr.cpp:L612-L614](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/expr_gen/generate_tiling_expr.cpp#L612-L614)，确认 `objects` 就是 `GetPipePerformance` 的输出；
   - 再到 [pipe_perf_expr.cpp:L553-L585](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/expr_gen/pipe_perf_expr.cpp#L553-L585) 看节点循环。
3. **需要观察的现象**：`for` 循环里每个节点依次算 `exe_time`、`node_perf`，再 `AddNodePerfToPipeCost`。
4. **预期结果**：能画出 `Generate → GetPipePerformance → GetPerfExpr → (GetNodeExeTime + GetNodePerfInternal + AddNodePerfToPipeCost) → objects` 这条链。
5. **待本地验证**：若开启 dump（`options` 里设 `kDumpDebugInfo`），可在产物 `model_info.json` 中看到 `objects` 字段的真实符号表达式（见 [gen_model_info.cpp:L447-L450](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/gen_model_info.cpp#L447-L450) 与 `to_json` [L320-L329](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/gen_model_info.cpp#L320-L329)）。

#### 4.2.5 小练习与答案

**练习 1**：`GenerateModelInfo` 的单图版和批量版分别被谁调用？
**答案**：批量版（带 options）被 ATT 总入口 `gen_tiling_impl.cpp` 调用（见 [gen_tiling_impl.cpp:L150](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_tiling_impl.cpp#L150)）；批量版内部对每个候选图再调单图版（[gen_model_info.cpp:L428](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/gen_model_info.cpp#L428)）。

**练习 2**：`ModelInfo::objects` 为什么用 `map<PipeType, Expr>` 而不是单个 `Expr`？
**答案**：因为不同 pipe（MTE2/VEC/MTE3）在硬件上并行/抢占情况不同，必须分桶记录各自的耗时表达式，让求解器综合多条 pipe 来评估总执行时间，而不是简单求和。

---

### 4.3 api_perf_register：cost 函数的注册与数据来源

#### 4.3.1 概念说明

4.2 里 `GetNodePerfInternal` 拿到「单次调用耗时 \(c_{n,p}\)」靠的是查表：**给一个算子类型，返回一个算 cycle 的函数**。这套「算子类型 → cost 函数」的映射，就由 `api_perf_register` 子模块提供，这正是本讲标题里的另一个关键词。

这里用了 Autofuse 项目里反复出现的**自注册（self-registration）**模式（与 [u5-l1](u5-l1-ascir-registration-framework.md) 的 ASCIR 注册同构）：

- 一个全局单例容器（`EvalCosts` / `ApiPerfFactory`）持有「名字 → 函数」映射；
- 每个算子在自己的 .cpp 里用一个**全局静态对象**在 `main` 之前把自己登记进去；
- 建模时按算子类型查表取出函数。

这样做的好处是：新增一个算子的 cost model，只需在它自己的文件里加一行注册，不必改中央调度代码。

#### 4.3.2 核心流程：两层注册 + 一个 JSON 数据源

ATT 的 cost 注册其实有**两层**，要分清：

1. **`EvalCosts` 单例**（[ascendc_api_perf.h:L22-L53](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/ascendc_api_perf.h#L22-L53)）：两张表——`func_container_`（ASCIR 层 `Perf` 函数）与 `ascendc_func_container_`（AscendC 层 `AscendCPerf` 函数）。`Perf` 与 `AscendCPerf` 都是函数指针类型（[api_perf.h:L70-L73](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/api_perf.h#L70-L73)）：输入是节点的形状/属性，输出是 `PerfOutputInfo`（按 pipe 分桶的耗时表达式）。
2. **`ApiPerfFactory` + `ApiPerfRegister`**（[api_perf_factory.h:L24-L75](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/api_perf_factory.h#L24-L75)）：把算子名映射到一个 `ApiPerf` 对象，后者**捆绑了** cost 函数、`PerfParamTable`（系数表）和 `TilingScheduleConfigTable`（调度配置）。

`PipePerfExpr` 取数的顺序（[pipe_perf_expr.cpp:L380-L435](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/expr_gen/pipe_perf_expr.cpp#L380-L435)）是：先 `GetApiPerf(node.node_type)` 从 Factory 拿带配置的 `ApiPerf`，取它的 `GetPerfFunc()`；若取不到，退回 `EvalCosts::Instance().GetFunc(node_unit)` 兜底；最后调用 `perf_func(inputs, outputs, node, perf_res)` 得到 `PerfOutputInfo`。

**cost 数据来自哪里？**——来自 `v1/perf_param_v1.cpp` 里的 `kParamV1Info`（[L68-L558](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/v1/perf_param_v1.cpp#L68-L558)）：一个内嵌的 JSON 字符串，记录了 v1 平台（昇腾 2201）每个算子按 dtype 的线性/带宽系数。它由 `PerfParamTableV1::GetAscendCApiPerfTable()` 返回（[L560-L562](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/v1/perf_param_v1.cpp#L560-L562)），是抽象基类 `PerfParamTable::GetAscendCApiPerfTable()`（[perf_param.h:L35](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/perf_param.h#L35)）的平台实现。**所以本讲练习题「cost model 数据来自哪个子模块」的答案是：`api_perf_register` 子模块（具体是 `v1/perf_param_v1.cpp` 的 `kParamV1Info`）**。

#### 4.3.3 源码精读

**注册宏**（[ascendc_api_perf.h:L129-L136](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/ascendc_api_perf.h#L129-L136)）：

```cpp
#define REGISTER_EVAL_FUNC(op_type, func_name)        FuncRegister eval_##op_type(op_type, func_name)
#define REGISTER_ASCENDC_EVAL_FUNC(op_type, func_name) AscendcFuncRegister ascendc_eval_##op_type(op_type, func_name)
```

`FuncRegister` 的构造函数（[L57-L59](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/ascendc_api_perf.h#L57-L59)）一构造就把 `(op_type, func)` 塞进 `EvalCosts::Instance()`——只要这个翻译单元被链接进来，注册就自动完成。

**平台 v1 的批量注册**：`ascir_api_perf_v1.cpp` 末尾用 `ApiPerfRegister<ApiPerf>` 把每个 ASCIR 算子名绑定到 cost 函数 + v1 系数表（[L1822-L1853](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/v1/ascir_api_perf_v1.cpp#L1822-L1853)），例如：

```cpp
ApiPerfRegister<ApiPerf> add_api_perf(kAdd, GetPerfFunc(kAdd), nullptr, &perf_param_table_v1,
                                      &tiling_schedule_config_table_v1);
ApiPerfRegister<ApiPerf> tanh_api_perf(kTanh, GetPerfFunc(kTanh), nullptr, &perf_param_table_v1, ...);
ApiPerfRegister<ApiPerf> load_api_perf(kLoad, GetPerfFunc(kLoad), nullptr, &perf_param_table_v1, ...);
```

读法：`kAdd` 算子的 cost 函数是 `GetPerfFunc(kAdd)`（从 `EvalCosts` 取出 `AddApi`），系数表是 `perf_param_table_v1`（即上面那个 JSON）。注意 reduce 类（`kMax`/`kSum` 等）绑的是 `tiling_schedule_config_table_v1_heavy_op`（[L1859-L1868](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/v1/ascir_api_perf_v1.cpp#L1859-L1868)）——「重算子」用更激进的 tradeoff 配置（见 [perf_param_v1.h:L70-L81](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/v1/perf_param_v1.h#L70-L81)）。

**一个完整 cost 函数长什么样**：`LoadApi`（[ascir_api_perf_v1.cpp:L208-L216](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/v1/ascir_api_perf_v1.cpp#L208-L216)）把节点合并连续维后交给 `GetDmaPerf`，最终写入 `perf_res.pipe_res[PipeType::AIV_MTE2]`——即「Load 节点的耗时只挂在 MTE2 这条 pipe 上」。这与直觉一致：搬运（GM→UB）走 MTE2，计算走 VEC。

**未建模算子的兜底**：注册表里有一批用 `DefaultGetPerf`（[api_perf.h:L108-L113](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/api_perf.h#L108-L113)，直接返回 `SUCCESS` 不写 pipe_res）或 `kUnitVector` 注册的算子（[ascir_api_perf_v1.cpp:L1870-L1920](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/v1/ascir_api_perf_v1.cpp#L1870-L1920)），如 `Data`/`Scalar`/`Pow`/`Gelu` 等——它们要么是纯搬运/标量（开销已算在上下游），要么是「目前无精确建模」，先用占位公式，求解器对它们的耗时估计偏保守。

#### 4.3.4 代码实践：跟踪一个算子从注册到被查询

1. **实践目标**：验证自注册模式，确认 `Add` 算子的 cost 数据确实来自 `kParamV1Info`。
2. **操作步骤**：
   - 在 [ascir_api_perf_v1.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/v1/ascir_api_perf_v1.cpp) 中搜 `add_api_perf`，确认它用 `&perf_param_table_v1` 注册；
   - 在 [perf_param_v1.cpp:L84-L90](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/v1/perf_param_v1.cpp#L84-L90) 的 JSON 里找 `"Add"`，记下 fp16 的 \(k,b\)；
   - 在 [api_perf_utils.cpp:L540-L550](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/utils/api_perf_utils.cpp#L540-L550) 看 `GetApiPerf(node_type)`：它通过 `GetAscIrAttImpl(node_type)->GetApiPerf()` 拿到注册时填的算子名，再用 `ApiPerfFactory::Instance().Create(...)` 取回那个带系数表的 `ApiPerf`。
3. **需要观察的现象**：注册侧（静态对象）与查询侧（Factory）之间没有任何硬编码的 `if (op == "Add")` 分支，完全靠「同名」对接。
4. **预期结果**：能说清「注册时填 `perf_param_table_v1` → 查询时 `Create` 出来的 `ApiPerf` 自带这张表 → cost 函数从表里取 `k,b`」这条数据流。
5. **待本地验证**：可在 `ascendc_api_perf.cpp`（`AddPerf` 实现处）加一行日志打印查到的 \(k,b\)，跑一个含 Add 的用例观察输出是否符合 `kParamV1Info`。

#### 4.3.5 小练习与答案

**练习 1**：reduce 类算子为什么绑 `tiling_schedule_config_table_v1_heavy_op` 而不是普通表？
**答案**：reduce 是「重算子」，计算量大、对 UB/核数 tradeoff 更敏感，故用 `TilingScheduleConfigTableV1HeavyOp`（开启 multicore-ub tradeoff、`core_num_ratio=0.4`）让求解器在多核与 UB 占用间做更激进的权衡（[perf_param_v1.h:L70-L81](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/v1/perf_param_v1.h#L70-L81)）。

**练习 2**：如果一个全新算子没在 `kParamV1Info` 里登记，建模会怎样？
**答案**：若它注册时用 `DefaultGetPerf`（不写 `pipe_res`），则该节点对 pipe 耗时贡献为 0（被当作近似无开销）；若连 cost 函数都没注册，`GetNodePerf` 会因 `perf_func == nullptr` 断言失败（[pipe_perf_expr.cpp:L434-L435](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/expr_gen/pipe_perf_expr.cpp#L434-L435)）。

---

## 5. 综合实践

**任务：为「Add → Exp」两算子融合子图，手工产出一份迷你 `ModelInfo`。**

把本讲三个模块串起来：

1. **建模目标（4.1）**：写出该融合图在 `AIV_MTE2`、`AIV_VEC`、`AIV_MTE3` 三条 pipe 上的耗时表达式骨架（用 \(t\) 表示外层循环次数、\(S\) 表示搬运字节数、\(r\) 表示 repeat 数）。
   - 参考答案：MTE2 = Load 一份输入 \(= t \cdot (S/T_{\text{mte2}} + h)\)；VEC = Add + Exp \(= t \cdot (k_{\text{add}} r + b_{\text{add}} + k_{\text{exp}} r + b_{\text{exp}})\)；MTE3 = Store 一份输出 \(= t \cdot (S/T_{\text{mte3}} + h)\)。
2. **查系数（4.3）**：从 [perf_param_v1.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/api_perf_register/v1/perf_param_v1.cpp#L68-L558) 的 JSON 里查出 fp16 下 `Add`、`Exp` 的 \(k,b\) 与 `Load`、`Store` 的 \(h,a,b\)，填进上式。
3. **落地到 objects（4.2）**：对照 [pipe_perf_expr.cpp:L553-L585](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/expr_gen/pipe_perf_expr.cpp#L553-L585)，确认你写的表达式与源码「逐节点算 perf → 乘 exe_time → 累加 → 加 pipe head」的流程一致。
4. **反思**：如果把这俩算子**不融合**（各自独立 kernel），MTE2/MTE3 的搬运次数会增加多少？这正是 Autofuse 融合能省 Memory Bound 的量化体现（呼应 [u3-l1](u3-l1-autofuse-principle.md)）。

完成本任务后，你应当能独立读懂任何一个新算子被 ATT 建模时的完整链路。

## 6. 本讲小结

- ATT 把 u6-l3 产出的候选 scheduled graph，建成一份「**目标 `objects` + 约束**」的性能模型 `ModelInfo`，供下一讲求解器挑选最优 tiling。
- `gen_model_info` 是 ATT 顶层入口，单图建模固定三步：**parser 翻图 → expr 生成（含 pipe 性能）→ pass 收集 config**；批量重载再叠加轴排序、API tiling、dump。
- 算子耗时按 `PipeType` 分桶（MTE2/VEC/MTE3…），总耗时 \(= \sum_n t_n \cdot c_{n,p} + H_p\)，全部用符号 `Expr` 表达以支持动态 shape。
- cost 函数靠 `api_perf_register` 子模块的**自注册**（`EvalCosts` + `ApiPerfFactory`）提供；cost 数据来自 `v1/perf_param_v1.cpp` 的内嵌 JSON（`PerfParamTableV1::GetAscendCApiPerfTable`）。
- 每个算子登记时把「算子名 → cost 函数 + 系数表 + 调度配置」三者捆绑；reduce 等重算子用 `heavy_op` 配置；未建模算子用 `DefaultGetPerf` 兜底。

## 7. 下一步学习建议

本讲只产出了**模型（`ModelInfo`）**，还没真正「求解」。下一讲 [u7-l2 表达式生成与求解器](u7-l2-expr-gen-and-solver.md) 将进入：

- `expr_gen` 如何在 `ModelInfo` 基础上生成 buf 占用约束（`buf_occupy_expr`）与执行时间 pass（`exe_time_pass`）；
- solver_pass 如何在约束下**求解**最优 tiling 变量，把本讲的 `objects` 从「符号表达式」变成「具体的 tiling 取值」。

建议预先浏览 [autofuse/att/gen_model_info/expr_gen/](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/att/gen_model_info/expr_gen/) 目录，重点看 `generate_tiling_expr.h`、`buf_occupy_expr.h`、`exe_time_pass.h`，为下一讲做准备。
