# u7-l2 表达式生成、轴重排与求解器

## 1. 本讲目标

上一讲（u7-l1）我们把 ATT（Auto Tiling）阶段的整体职责讲清楚了：ATT 把 AutoSchedule 枚举出来的「候选 scheduled graph」建成一份可求解的性能模型 `ModelInfo`，再交给求解器挑出最优 tiling。但那只是总账。本讲要回答四个更具体的问题：

1. `ModelInfo` 里那一张张符号表达式（目标、约束、待求解变量）到底是怎么从图「长」出来的？
2. 「执行时间」这个目标是怎么估算的？为什么同一个节点在不同切分方式下估算出的耗时不同？
3. 待求解的轴列表 `arg_list` 的**顺序**是谁定的？为什么 Reduce R 轴和尾轴谁先切会显著影响性能，ATT 如何在这两者之间做平衡（本次版本新增的 `arg_list_reorder` 轴重排机制）？
4. 求解器（solver）到底在最小化什么、受什么约束？它是不是真的「暴力枚举」？

学完本讲，你应该能够：

- 说出 `GenerateTilingExpr::Generate` 的执行顺序，并把它每一步与 `ModelInfo` 的字段对应起来；
- 解释 `BufOccupyExpr` 如何把「片上缓冲占用」算成一条 UB 约束，`ExeTimePassManager` 如何修正节点的执行时间；
- 讲清 `ArgListReorder` 如何用「Reduce R 轴字节数 vs 尾轴字节数」与两个硬件阈值（向量长度、cache line）把轴序分成四档策略；
- 写出求解器最小化的目标函数与必须满足的约束，并能区分「目标」与「约束」在求解器里的不同待遇。

## 2. 前置知识

在进入源码前，先用四段话补齐必要概念。

**（1）把 tiling 选择看成一个「优化问题」。** Autofuse 在编译期无法知道运行时的真实 shape（动态 shape），所以它不能在编译期定死 tiling，而是生成一个「求解器函数」随 kernel 一起编译、在 **kernel 下发时（运行期）** 拿到真实 shape 后再算出 tiling。ATT 做的就是：在编译期把这个优化问题用符号表达式描述清楚（目标函数 + 约束 + 决策变量），再把这些表达式「翻译」成一段 C++ 求解器代码。所以你会看到两类完全不同的代码：`expr_gen/` 用符号 `Expr` 描述问题，`solver_pass/` 把符号 `Expr` 打印成运行期的 C++ 源码。

**（2）符号表达式 `Expr`。** 贯穿本讲的数据类型是 `af::Expr`（及其别名 `att::Expr`）。它不是某个具体的数，而是一棵表达式树，叶子是 `af::Symbol("x0")`（变量）或常量，内部节点是 `Add/Mul/Max/Ceiling/...`。因为 shape 未知，所有「大小」「耗时」都用 `Expr` 表示，等运行期把变量代入具体值后才得到数值。u4-l2、u6-l3 已多次出现 `Expression`，这里不再展开。

**（3）目标（objective）vs 约束（constraint）。**

- **目标**：我们「想让它尽量小」的量，这里是 kernel 的执行时间。求解器在可行解里挑使目标最小的那个。
- **约束**：必须满足的硬性条件，否则这个解「不可行」直接丢弃。典型约束有两类：硬件容量约束（UB 里放得下所有活跃 buffer）和轴关系约束（子轴大小不能超过父轴）。

> 关键直觉：**约束决定「能不能用」，目标决定「哪个更好」**。求解器只会在满足所有约束的解里比较目标值。

**（4）轴序（arg_list 的顺序）为什么重要。** `ModelInfo::arg_list` 里待求解轴的排列顺序，就是求解器切轴的优先序——排在前面的轴先被切大块。对 elementwise 图来说顺序无关紧要，但**带 Reduce 的图例外**：假设输入是 `[M, K]`、沿 K 归约。K 是 Reduce R 轴，M（更准确说是最内层非归约轴，即「尾轴」）是尾轴。如果 R 轴很长而尾轴很短，优先切 R 轴能让每次搬入的数据被充分复用；反之如果尾轴本身已超过一条 cache line 的量、R 轴却不足一个向量长度，优先切尾轴才是对的。**轴序选错，搬运带宽会被白白浪费**——这就是本讲 4.3 节轴重排模块要解决的问题。

## 3. 本讲源码地图

本讲聚焦 ATT 的 `gen_model_info`（表达式生成 + 轴重排）与 `generator/solver_pass` 两条线，涉及下列文件：

| 文件 | 作用 |
| --- | --- |
| `autofuse/att/gen_model_info/gen_model_info.cpp` | ATT 顶层入口，串起 parser → expr_gen → pass 三步，并在其后调用 `ArgListReorder` 重排轴序 |
| `autofuse/att/gen_model_info/expr_gen/generate_tiling_expr.{h,cpp}` | **表达式生成主类** `GenerateTilingExpr`，把 TuningSpace 翻译成 ModelInfo |
| `autofuse/att/gen_model_info/expr_gen/buf_occupy_expr.{h,cpp}` | 片上缓冲占用计算，产出 UB 容量约束 |
| `autofuse/att/gen_model_info/expr_gen/pipe_perf_expr.{h,cpp}` | 按流水线（pipe）累计执行时间，产出目标函数 |
| `autofuse/att/gen_model_info/expr_gen/exe_time_pass.{h,cpp}` | 执行时间修正 pass，识别 broadcast 缓存场景并压缩节点耗时 |
| `autofuse/att/gen_model_info/expr_gen/arg_list_reorder.{h,cpp}` | **轴重排**（本次更新重点）：按 Reduce R 轴 / 尾轴平衡策略重排 `arg_list` |
| `autofuse/att/base/model_info.h` | 总账结构 `ModelInfo` 的定义 |
| `autofuse/att/generator/solver_pass_gen/solver_pass_manager.{h,cpp}` | 把 ModelInfo 喂给求解器代码生成器 |
| `autofuse/att/generator/solver_pass_gen/general_solver/general_solver_gen.{h,cpp}` | 把目标/约束打印成运行期 C++ 求解器代码 |
| `autofuse/att/generator/solver_pass/general_solver_code.h` | 运行期通用求解器 `GeneralSolver` 的全部算法源码（字符串形式） |
| `autofuse/att/generator/solver_pass/axes_reorder_solver_code.h` | AxesReorder 算法对应的运行期求解器源码（支持等序 tiling） |

读法建议：先看 `gen_model_info.cpp` 的三步骨架建立全局观，再进入 `generate_tiling_expr.cpp` 的 `Generate()` 逐行读，然后读 `arg_list_reorder.cpp` 理解轴序是怎么被调整的，最后跳到 `general_solver_code.h` 的 `Run()` 理解求解器如何消费这份模型。

## 4. 核心概念与源码讲解

### 4.1 tiling 表达式生成（GenerateTilingExpr）

#### 4.1.1 概念说明

上一讲提到 `GenerateModelInfo` 是 ATT 的顶层入口。本次版本把它重构为两层：内部私有函数 `GenerateSingleModelInfoWithContext` 完成实际工作，公开的 `GenerateModelInfo` 只是转发。核心仍是一个三段式：先用 **parser** 把 `AscGraph`（带调度语义的融合子图）翻成中间表示 `TuningSpace`，再用 **expr_gen** 把 `TuningSpace` 翻成符号化的 `ModelInfo`，最后用若干 **pass** 补充配置。本讲的主角是中间这一段——`GenerateTilingExpr`。

`GenerateTilingExpr` 的职责可以用一句话概括：**把「图 + 调度语义」翻译成「一个优化问题的符号描述」**。它产出的 `ModelInfo` 就是一张总账，记录了三样东西：

- **决策变量 `arg_list`**：运行期要求解的那些轴大小（通常是 tile-inner 切分轴），每个变量带上下界、对齐、取值范围；
- **目标 `objects`**：每个流水线（pipe）的执行时间表达式，求解器要最小化它；
- **约束**：`hardware_cons`（硬件容量，主要是 UB）、`eq_exprs`/`leq_exprs`（轴之间的大小/整除关系）。

`ModelInfo` 的字段定义集中在一处，建议先扫一眼建立印象：

[`model_info.h:279-314`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/base/model_info.h#L279-L314) —— 这是整张总账，注释直接说明了每个字段的用途（`objects` 描述目标、`hardware_cons` 描述硬件约束、`eq_exprs`/`leq_exprs` 描述等式/不等式约束、`arg_list` 描述待求解轴）。

#### 4.1.2 核心流程

ATT 顶层三步现在写在内层函数 `GenerateSingleModelInfoWithContext` 里：

[`gen_model_info.cpp:173-209`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/gen_model_info.cpp#L173-L209) —— `step1` 调 `AscendGraphParser::GraphParser` 得到 `tuning_space`，并把 Codegen 路径门禁 `is_cv_ub_fusion` 回填进每个 `node_info`（本次新增，详见 u7-l1）；`step2` 调 `GenerateTilingExpr::Generate` 填充 `model_info`；`step3` 跑一组 pass 收集配置。

公开入口退化为薄封装：

[`gen_model_info.cpp:211-214`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/gen_model_info.cpp#L211-L214) —— `GenerateModelInfo`（单图版本）直接转发并传 `is_cv_ub_fusion = false`。真正的上下文由 [`gen_model_info.cpp:411-417`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/gen_model_info.cpp#L411-L417) 的文件内私有结构 `ModelGenerationContext` 携带，避免为 Codegen 路径门禁扩公开接口。

本讲聚焦 `step2`。`Generate()` 内部的步骤顺序如下（伪代码）：

```
Generate(model_info):
  LoadArgList()                  # 建立「轴名 -> 符号变量」映射（决策变量的命名）
  GetBufConstraint()             # -> hardware_cons[UB]        （约束：缓冲占用）
  GetCoreConstraint()            # -> hardware_cons[CORENUM]   （约束：分核数）
  GetReservedUbSize()            # -> reserved_ub_size
  GetPipePerformance()           # -> objects（目标）、head_cost、perf_breakdowns
  GetWorkSpaceSize()             # -> workspace_size_map
  GetSubAxisArgs()               # -> arg_list（决策变量 + 上下界/对齐）
  GetAxisConstraints()           # -> eq_exprs / leq_exprs（轴关系约束）
  GetOutputSize()                # -> output_size
  UpdateNeedUBMCTradeoff()       # 多核-UB 权衡开关
  ApplyPenaltyConfigToModelInfo()# Reduce 分核惩罚
```

一个重要观察：**每一步都只填 `ModelInfo` 的某几个字段，彼此几乎不交叉**。所以理解 `Generate()` 的最佳方式就是「每一步对应一张表的一个格子」。

#### 4.1.3 源码精读

先看 `Generate()` 的真实主体：

[`generate_tiling_expr.cpp:596-633`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/generate_tiling_expr.cpp#L596-L633) —— 逐行调用 `GetBufConstraint / GetCoreConstraint / GetReservedUbSize / GetPipePerformance / GetWorkSpaceSize / GetSubAxisArgs / GetAxisConstraints`，把结果写进 `model_info` 的不同字段。

我们挑三个最关键的步骤看其内部实现。

**(a) 缓冲占用约束 `GetBufConstraint`**

[`generate_tiling_expr.cpp:42-54`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/generate_tiling_expr.cpp#L42-L54) —— 它只是个薄封装，真正的计算委托给 `BufOccupyExpr`（4.2 节详讲），产出 `hardware_cons`（按硬件类型分桶，UB 桶就是求解器要守的容量约束）。

**(b) 分核数约束 `GetCoreConstraint`**

[`generate_tiling_expr.cpp:77-95`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/generate_tiling_expr.cpp#L77-L95) —— 遍历所有 `block_dims`，把每组分核轴的轴大小连乘得到该组 `block_dim`，再对所有组取 `Max`，写入 `hardware_cons[CORENUM]`。含义是「不管怎么切，总核数不能超过芯片核数」。

```cpp
Expr block_dim_expr = CreateExpr(1U);
for (auto &block_axis : core_info) {
  block_dim_expr = af::sym::Mul(block_dim_expr, axis_size);  // 该组分核轴连乘
}
block_dim_max_expr = af::sym::Max(block_dim_expr, block_dim_max_expr); // 多组取最大
hardware_cons[HardwareDef::CORENUM] = block_dim_max_expr;
```

**(c) 轴关系约束 `GetAxisConstraints`**

[`generate_tiling_expr.cpp:201-222`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/generate_tiling_expr.cpp#L201-L222) —— 这是「父轴与子轴」关系的来源。对每个有父轴的子轴：

- 父轴大小 = 各父轴连乘；
- 若**不允许尾块**（`enable_tail == false`）：加等式约束 `NO_TAIL`，要求父轴大小能被子轴整除（无余数）；
- 若**允许尾块**：加不等式约束 `NORMAL`，要求 `子轴大小 − 父轴大小 ≤ 0`（子轴不超过父轴）。

这两类约束的 key 名 `kFatherToChildNoTail="NO_TAIL"`、`kFatherToChildLarger="NORMAL"` 在 [`model_info.h:23-25`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/base/model_info.h#L23-L25) 定义，后续求解器据此识别约束类型。

> 小结：`GenerateTilingExpr` 不做任何「求解」，它只负责把问题**描述清楚**——变量是哪些、目标是什么、约束有哪些。轴的「先后顺序」它也不管——那是 4.3 节 `ArgListReorder` 的职责。真正的「挑选」发生在 4.4 节的求解器里。

#### 4.1.4 代码实践

**实践目标**：把 `Generate()` 的每一步与 `ModelInfo` 的字段建立一一对应，建立「读 Generate 就能预测 model_info.json 内容」的能力。

**操作步骤**：

1. 打开 [`generate_tiling_expr.cpp:596`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/generate_tiling_expr.cpp#L596) 的 `Generate()`。
2. 准备一张三列表格：`步骤函数 | 写入的 model_info 字段 | 该字段的语义`。
3. 逐行填表，例如第一行是 `LoadArgList → variable_expr_map / variable_name_map → 轴名与符号变量的双向映射`。
4. 填完后，对照 [`model_info.h:279-314`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/base/model_info.h#L279-L314) 的字段注释校验。

**需要观察的现象**：你会发现 `Generate()` 里**没有**直接计算 `objects`（目标）的算式——目标是通过 `GetPipePerformance()` 委托给 `PipePerfExpr` 算的（见 4.2）。这正是 ATT 模块化分工的体现。

**预期结果**：表格大约 8~10 行，覆盖 `hardware_cons / reserved_ub_size / objects / workspace_size_map / arg_list / eq_exprs / leq_exprs / output_size`。若某字段你在 `Generate()` 里找不到来源，标注「待确认」并在 pass 阶段（[`gen_model_info.cpp:191-206`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/gen_model_info.cpp#L191-L206)）补找。

#### 4.1.5 小练习与答案

**练习 1**：`GetCoreConstraint` 为什么对所有 `block_dims` 组取 `Max` 而不是求和？

> **答案**：不同组分核方案互斥（运行期只会选一组 tiling），求解器需要在「最坏的那组」下保证总核数不超过物理核数，因此取最大值作为单一约束上界。求和会把互斥方案当成同时发生，导致约束过紧。

**练习 2**：若一个子轴 `enable_tail == true` 且 `enable_pad == true`，`GetAxisConstraints` 会产生什么约束？

> **答案**：都不产生——代码里 `enable_pad == true` 的分支直接 `continue`（注释「目前不需要」）。说明 pad 场景的轴关系目前不进 ModelInfo，留待后续处理。

**练习 3**：本次重构把 `GenerateModelInfo` 拆成「薄公开入口 + `GenerateSingleModelInfoWithContext` 内层函数」的动机是什么？

> **答案**：引入文件内私有结构 `ModelGenerationContext`（携带 `enable_gather_reduce_penalty` 与 `is_cv_ub_fusion`），让 Codegen 路径门禁等内部上下文在函数间传递，而**不必扩公开接口 `GenerateModelInfo` 的签名**——这是「公开 API 最小面」的常规做法。

---

### 4.2 buf 占用与执行时间 pass

`Generate()` 里有两个步骤最依赖「对硬件行为的建模」：缓冲占用（决定 UB 约束）和执行时间（决定目标）。它们分别由 `BufOccupyExpr` 与 `PipePerfExpr` 承担，而 `PipePerfExpr` 内部又调用 `ExeTimePassManager` 做耗时修正。本节拆这三者。

#### 4.2.1 概念说明

**缓冲占用（buffer occupy）解决「UB 放不放得下」。** 昇腾 AI Core 的统一缓冲（UB）容量有限。Autofuse 把多个算子融合进一个 kernel 后，这些算子的中间 tensor 都要在 UB 里共存。如果求解器选了一个过大的 tile，UB 就装不下——所以「所有活跃 tensor 的占用之和 ≤ UB 容量」是一条硬约束。难点在于：哪些 tensor 真正「同时存活」？这正是 u6-l4 里 `BufQueAllocator` 用区间图着色算出来的「共存关系」（co-tensor），本节直接消费它的结果。

**执行时间（execution time）解决「这一份 tiling 大概要跑多久」。** ATT 不可能真跑一遍 kernel，只能用成本模型（cost model）估算。估算分两层：

- **底层单次开销** `c_{n,p}`：每个算子 `n` 在流水线 `p`（MTE2 搬入 / VEC 计算 / MTE3 搬出）上每调用一次的耗时，来自 u7-l1 的 `api_perf_register`（线性模型 `k·repeat+b` 或带宽模型）；
- **上层循环次数** `t_n`：该算子在一份 tiling 下被循环执行多少次，等于其各循环轴大小的连乘。

于是每个 pipe 的总耗时是：

\[
\text{Cost}_p = \sum_{n} t_n \cdot c_{n,p} + H_p
\]

其中 \(H_p\) 是多核/流水线头开销（`head_cost`）。求解器最小化的目标就是把各 pipe 的 `Cost_p` 聚合后的标量。

**为什么需要「执行时间 pass」修正？** 因为 naive 的 `t_n = ∏ loop_axis.repeat` 在 broadcast（广播）场景下会高估。当 broadcast 内联缓存（brc inline cache）生效时，一份缓存数据可被多份输出复用，节点的有效循环次数应当除以广播切分轴的重复数。`ExeTimePassManager` 就是来识别这种场景并压缩 `t_n` 的。

#### 4.2.2 核心流程

**缓冲占用（BufOccupyExpr）**：

```
GetTotalBufferOccup:
  for 每个 container（TQue/TBuf，由 BufQueAllocator 产出）:
      occup_per_tensor = Max( 同存 tensor size 之和, 单个非共存 tensor size )   # 同存取和、非同存取 max
      若是 TBuf: 再与 tmp_buffer size 取 Max，并兑现最小 8KB
      occup_total = occup_per_tensor * buffer_num                              # 乘以队列深度
      按 container 所在 scope（UB/L1/...）累加进 buffer_occup[scope]
  额外把 内置 tmp_buffer、kernel 初始化预留 UB 也累加进 UB
```

**执行时间（PipePerfExpr + ExeTimePassManager）**：

```
GetPerfExpr:
  构造 ExeTimePassManager(tuning_space)            # 先扫一遍图，分类 B/R/A 轴、标记 brc 缓存节点
  for 每个 非数据/非占位 节点 node:
      t_n = ∏ node.loop_axes.repeat                # naive 循环次数
      t_n = exe_time_mgr.UpdateNodeExeTime(node, t_n)   # 广播缓存场景下压缩 t_n
      c_n,p = api_perf(node)(输入输出 shape)        # 单次开销（按 pipe 分桶）
      for p: pipe_costs[p] += t_n * c_n,p           # 累加进目标
  UpdatePipeHead(): 每个 pipe 叠加头开销 H_p
  GetOpHeadCost(): 汇总 head_cost
```

#### 4.2.3 源码精读

**(a) 缓冲占用的「同存取和、非同存取 max、再乘 buffer_num」**

核心在 `GetOccupInContainer`：

[`buf_occupy_expr.cpp:58-108`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/buf_occupy_expr.cpp#L58-L108) —— 先用 `GetCoTensorSizeExpr` 把「同存（co-tensor）」组的 size **相加**（它们必须同时驻留），再对非共存 tensor 取 `Max`（它们可复用同一块空间，取最大者即可），最后 `occup_total = occup_per_tensor * buffer_num`。

```cpp
// 同存组：组内求和，组间取 max（见 GetCoTensorSizeExpr: buf_occupy_expr.cpp:32-56）
// occup_total = 最大单份占用 × 队列深度（buffer_num）
occup_total = occup_per_tensor;
if (IsValid(buffer_num_expr)) {
  occup_total = af::sym::Mul(occup_per_tensor, buffer_num_expr);
}
```

随后 `GetBufferOccupInContainer` 按 container 的 `buf_location`（它位于 UB 还是 L1 等）把 `occup_total` 累加进对应 scope 的桶：

[`buf_occupy_expr.cpp:110-142`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/buf_occupy_expr.cpp#L110-L142) —— 注意它还额外把 `tmp_buffer`、内置 tmp buffer、kernel 初始化预留空间累加进 UB 桶，确保 UB 约束覆盖所有占用来源。这一步产出的 `hardware_cons[UB]` 就是 4.4 节求解器要守的容量约束。

**(b) 执行时间的累加与头开销**

`GetPerfExpr` 是目标函数的装配车间：

[`pipe_perf_expr.cpp:547-585`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/pipe_perf_expr.cpp#L547-L585) —— 跳过 `Data/Workspace/Output/TbufData/Scalar` 等非计算节点，对每个计算节点：先 `GetNodeExeTime` 拿到修正后的循环次数 `t_n`，再 `GetNodePerfInternal` 拿到按 pipe 分桶的单次开销 `c_n,p`，最后 `AddNodePerfToPipeCost` 把 `t_n · c_n,p` 累加进 `pipe_costs`。

关键的乘法与累加在 `AddPerf`：

[`pipe_perf_expr.cpp:476-498`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/pipe_perf_expr.cpp#L476-L498) —— `pipe_cost = node_exe_times * node_perf`，正是公式里的 \(t_n \cdot c_{n,p}\)，按 pipe 累加进 `pipe_costs[p]`。

循环次数的「修正」入口在 `GetNodeExeTime`：

[`pipe_perf_expr.cpp:445-457`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/pipe_perf_expr.cpp#L445-L457) —— 先算 naive 的 `exe_time = ∏ loop_axis.repeat`，再交给 `exe_time_mgr.UpdateNodeExeTime` 修正。

**(c) 执行时间 pass 的轴分类与压缩**

`ExeTimePassManager` 在构造时就完成图的预扫描：

[`exe_time_pass.h:19-59`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/exe_time_pass.h#L19-L59) —— 构造函数遍历所有节点，调 `CheckBroadcast` / `CheckReduce` 把每个 data 节点的轴归类为 B 轴（广播）、R 轴（归约）、A 轴（非归约），并用 `UpdateBufNode` 标记受广播缓存影响的节点集合 `brc_buf_node_`。

源码里有一段非常清晰的算法说明注释，建议直接读：

[`exe_time_pass.cpp:192-217`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/exe_time_pass.cpp#L192-L217) —— 注释总结了「brc 缓存执行逻辑」：扫描 Load 到 Broadcast 之间的节点，若其循环轴里存在 B 切分轴，则：(1) 若该轴同时是 R 轴或非 A 轴，性能公式除以该切分轴循环次数；(2) 若该轴是 A 轴且相关 R 轴循环次数为 1，则除以 B 切分轴循环次数，否则不压缩。`CheckAxisSplit` 正是这一判定。

真正的耗时改写发生在 `UpdateNodeExeTime`：

[`exe_time_pass.cpp:245-286`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/exe_time_pass.cpp#L245-L286) —— 若节点不在 `brc_buf_node_` 里，直接返回原 `exe_time`（不修正）；否则按上述规则返回一个 `TernaryOp`（可能是 `exe_time / repeat`，或带条件的三目表达式 `r_loop==1 ? exe_time/repeat : exe_time`）。返回类型是 `TernaryOp` 而非纯 `Expr`，正是因为压缩结果可能依赖运行期条件（如 R 轴循环是否为 1）。

> 设计要点：**`ExeTimePassManager` 不是独立的优化 pass，而是 `PipePerfExpr` 的「前置分析器」**。它把「哪些节点、哪些轴会影响有效循环次数」这一先验信息提前算好，供目标函数装配时一次性使用。

#### 4.2.4 代码实践

**实践目标**：通过 dump 出来的 `model_info.json`，亲眼看到 UB 约束和目标表达式的形态，把符号公式与源码对应起来。

**操作步骤**：

1. ATT 在开启 dump 时会把 `ModelInfo` 序列化成 `model_info.json`，序列化字段见 [`gen_model_info.cpp:329-337`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/gen_model_info.cpp#L329-L337)（包含 `hardware_cons / eq_exprs / leq_exprs / objects / arg_list`）。
2. 用 Autofuse 的 DFX 开关（u3-l3 讲过 `AUTOFUSE_DFX_FLAGS`）或 `TORCH_COMPILE_DEBUG` 跑一个 pointwise 用例（如 `af_add_ge.py`）。
3. 在产物目录里找到 `model_info.json`，用 `python -m json.tool` 格式化。
4. 在 `hardware_cons` 里找到 `UB` 桶，读出它的符号表达式（应形如若干 `container_size * buffer_num` 之和）。
5. 在 `objects` 里找到各 pipe（`aiv_mte2 / aiv_vec / aiv_mte3`）的目标表达式，确认每项都是「`exe_time * 单次perf`」的累加。

**需要观察的现象**：

- `hardware_cons[UB]` 是一个**和式**，体现「所有共存 buffer 占用相加」；
- `objects` 里每个 pipe 是一个**乘积之和**，体现 \(t_n \cdot c_{n,p}\)；
- `arg_list` 里每个变量都带 `value_range`（上下界）和 `align`（对齐）。

**预期结果**：你能用一句话解释 json 里每个顶层字段的来源函数（`hardware_cons[UB]`←`GetBufConstraint`、`objects`←`GetPipePerformance`、`arg_list`←`GetSubAxisArgs`、`eq/leq_exprs`←`GetAxisConstraints`）。若某用例未生成 `model_info.json`，说明未触发 dump 路径（[`gen_model_info.cpp:479-482`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/gen_model_info.cpp#L479-L482) 受 `kDumpDebugInfo` 选项控制），标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `GetCoTensorSizeExpr` 对同存 tensor 组内求和、组间取 `Max`？

> **答案**：同存组（co-tensor）里的 tensor 生命周期重叠，必须同时驻留 UB，所以占用相加；不同组之间不重叠，可复用同一块 UB 空间，所以只需取最大那组。这是区间图着色（u6-l4）的语义在占用计算上的直接体现。

**练习 2**：`UpdateNodeExeTime` 为什么返回 `TernaryOp` 而不是 `Expr`？

> **答案**：广播缓存的有效压缩依赖运行期条件（如「相关 R 轴循环次数是否为 1」），这个条件在编译期未知，只能用三目运算 `cond ? a : b` 表达。`TernaryOp` 就是这种条件表达式的载体，最终会被求解器代码生成器打印成运行期的 `?:` 语句。

**练习 3**：若一个节点既不在 `brc_buf_node_` 里、也没有被 `CheckSingleCut` 判为单切轴，它的耗时如何计入目标？

> **答案**：`UpdateNodeExeTime` 直接返回原 `exe_time`（不压缩），`AddNodePerfToPipeCost` 走 `AddPerf(exe_var, "contrib", ctx)` 分支（不拆尾块），即 `pipe_costs[p] += exe_time * c_{n,p}`，是最朴素的累加。

---

### 4.3 arg_list_reorder 轴重排（Reduce R 轴与尾轴 tiling 平衡）

本节是本次更新的重点：讲解 `ArgListReorder` 如何在 `ModelInfo` 生成之后重排 `arg_list`，特别是**新增的 Reduce R 轴与尾轴（tail axis）tiling 平衡机制**。

#### 4.3.1 概念说明

`Generate()` 产出的 `arg_list` 只是「待求解轴的集合」，它们进 `ModelInfo` 时的顺序来自 parser 的自然顺序，未必是求解器切轴的最优优先序。`ArgListReorder` 的职责就是**在求解之前调整这个顺序**。

为什么顺序重要？回到 4.1 的 Reduce 例子：输入 `[M, K]` 沿 K 归约，K 是 R 轴、M 是尾轴（节点最内层非归约轴 `is_node_innerest_dim`）。两个硬件量决定谁该优先被切：

- **向量长度（vector length，`GetVectorLenSize()`）**：Vector 单元一次能并行处理的字节数。R 轴的字节数（`repeat × dtype_size`）**超过**它，说明一次归约就能吃满向量通道；
- **cache line（`GetCacheLineSize()`）**：一次内存访问的有效粒度。尾轴字节数**小于**它，说明按尾轴切会让每次搬入的数据不足一条 cache line，带宽浪费。

两者组合出四种情形，对应本次新增的 `ReduceTailTilePolicy` 四档策略（定义在 [`arg_list_reorder.h:181-189`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/arg_list_reorder.h#L181-L189)）：

| 情形 | 策略 | 含义 |
| --- | --- | --- |
| R 轴字节 > 向量长度 且 尾轴字节 < cache line | `kPreferTail` | 尾轴太碎切了浪费，让尾轴排前、优先保 R 轴整块归约 |
| R 轴字节 < 向量长度 且 尾轴字节 > cache line | `kKeepDefault` | R 轴不足一条向量，维持默认「Reduce 优先」顺序 |
| 其余静态可算的情形 | `kEqual` | 两轴各有利弊，设为**等序**（相同 order），把选择权交给求解器在运行期权衡 |
| 表达式动态/无法求出常量字节 | `kFallback` | 回退旧的单一模板逻辑（静态 preferred / 动态 runtime rule） |

其中 `kEqual`（等序）是本次机制的核心创新：不再由编译期硬性裁定谁先谁后，而是让 Reduce R 轴和尾轴拥有相同的切分优先级，由运行期求解器（配合 `axes_reorder_solver_code.h` 里的等序 tiling 支持，见 [`axes_reorder_solver_code.h:44-45`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass/axes_reorder_solver_code.h#L44-L45) 的 `enable_equal_order_tiling` 参数）按真实 shape 在两轴之间分配 tiling 空间。

#### 4.3.2 核心流程

`ArgListReorder` 的总入口是 `SortArgList`，由 ATT 顶层在 `Generate()` 之后调用：

```
GenerateModelInfoWithContext:                       # gen_model_info.cpp:436-466
  GenerateSingleModelInfoWithContext(...)            # 4.1 节：填 ModelInfo
  if IsAxesReorderAlgorithm():                      # tiling_algorithm == "AxesReorder" 时才重排
      ArgListReorder(tuning_space).SortArgList(model_info.arg_list, ...)

SortArgList(arg_list):                              # arg_list_reorder.cpp:786-826
  1. FindSpecialArgs()                              # 扫图识别特殊轴
       RecordSpecialArgs: 标记 reduce/broadcast/innermost 轴
       load/store 节点: 记录 load_store_inner_most_dims_
       RecordReduceTileTemplateSelection:           # ★ 本次新增逻辑的挂载点
           TryGetReduceTailTileInfo     -> 找唯一的 R tile 轴 + 唯一尾轴 + dtype size
           ClassifyReduceTailTilePolicy -> 四档策略
           kEqual     -> equal_order_reduce_tail_axes_ 记录两轴名
           kPreferTail-> prefer_reduce_tile_ = true
           kKeepDefault / kFallback -> 维持默认或走旧逻辑
  2. BuildArgListPriorityGraph(arg_list, prefer_reduce_tile_)
       父轴 -> 子轴的边（硬约束，防子轴先切超 UB）
       ApplyPriorityRules:
         normal 序: reduce > broadcast > innermost
         tiling_R 序: innermost > broadcast > reduce   # 连边顺序不同，配合判环决定谁赢
  3. TopologicalSort() -> GetNewArgList()           # 得到重排后的 arg_list
  4. MakeSureLoadStoreInnerestSameOrder()           # ★ 新增：处理等序组
       kEqual 时把 R 轴与尾轴的 order 拉齐（SetAxesSameOrder）
       与 transpose 的 load/store 等序组冲突（合并后超过 2 轴）时放弃，保 Transpose 等序
```

字节判定的数学很简单。设 R 轴原始长度为 \(r\)、尾轴原始长度为 \(t\)、dtype 单字节大小为 \(s\)，则：

\[
B_{\text{reduce}} = r \cdot s, \qquad B_{\text{tail}} = t \cdot s
\]

分类即比较 \(B_{\text{reduce}}\) 与向量长度 \(V\)、\(B_{\text{tail}}\) 与 cache line \(C\)：

\[
\text{policy} =
\begin{cases}
\text{kPreferTail}, & B_{\text{reduce}} > V \ \text{且}\ B_{\text{tail}} < C \\
\text{kKeepDefault}, & B_{\text{reduce}} < V \ \text{且}\ B_{\text{tail}} > C \\
\text{kEqual}, & \text{其余（且两端字节数均可静态求值）} \\
\text{kFallback}, & \text{动态 shape 求不出常量字节}
\end{cases}
\]

#### 4.3.3 源码精读

**(a) 调用点：ATT 顶层何时触发重排**

[`gen_model_info.cpp:461-466`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/gen_model_info.cpp#L461-L466) —— `IsAxesReorderAlgorithm()`（tiling 算法为 `AxesReorder`）为真时，构造 `ArgListReorder` 并调 `SortArgList` 重排 `model_info.arg_list`，同时可能产出 `runtime_reorder_rules`（运行期才决定的轴序规则）。

**(b) 四档策略的判定：TryGetReduceTailTileInfo + ClassifyReduceTailTilePolicy**

先看信息收集。`TryGetReduceTailTileInfo` 要找到「唯一的 R tile 轴」和「唯一的尾轴」：

[`arg_list_reorder.cpp:231-256`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/arg_list_reorder.cpp#L231-L256) —— `FindReduceTileAxis` 在 `sub_axes` 里找 `INNER` 且未绑多核的 Reduce 原始轴；出现**多于一个** R tile 轴就直接放弃（保序）。`CollectReduceTailAxis` 遍历 R 轴所在节点的输入 tensor，收集最内层且非 R 的维作为尾轴，同样要求**唯一**且各输入 **dtype 一致**，否则放弃。

然后是四档判定本体：

[`arg_list_reorder.cpp:316-336`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/arg_list_reorder.cpp#L316-L336) —— `GetExprBytes`（[`arg_list_reorder.cpp:21-34`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/arg_list_reorder.cpp#L21-L34)，`axis_bytes = repeat * data_type_size`，要求 repeat 是常量）分别算出 `reduce_bytes / tail_bytes`，再与 `GetVectorLenSize() / GetCacheLineSize()`（来自 `tuning_space_->tiling_schedule_config_table`，平台相关配置表）比较，得到四档之一。

**(c) 策略的消费：RecordReduceTileTemplateSelection**

[`arg_list_reorder.cpp:421-471`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/arg_list_reorder.cpp#L421-L471) —— 前置条件是 Reduce 轴确实被 Tile 切分（`IsReduceAxisTileSplit`）。拿到四档策略后：

- `kEqual`：把 R 轴名与尾轴名都塞进 `equal_order_reduce_tail_axes_`，直接 `return`；
- `kPreferTail`：置 `prefer_reduce_tile_ = true`（变量名有点反直觉，它使第 2 步建图走 `tiling_R` 连边序，效果是**尾轴排在 R 轴前面**），`return`；
- `kKeepDefault`：什么都不做（维持 Reduce 优先的默认序），`return`；
- 都不命中（`kFallback` 或信息收集失败）：落入**旧逻辑** `HasSmallTailLargeReduceTile`——静态的「小尾大 R」直接选 preferred 模板；动态的生成 `RuntimeReorderRule`（条件轴/比较轴 + 阈值），运行期再裁定，这会在 `SortArgList` 里经 `SetRuntimePreferredOrder` 产出 `runtime_reorder_rules`。

**(d) 优先级图与连边顺序：ApplyPriorityRules**

[`arg_list_reorder.cpp:602-619`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/arg_list_reorder.cpp#L602-L619) —— 两个分支的唯一区别是**三类边（reduce/broadcast/innermost）的添加顺序**：normal 序先加 reduce 边，tiling_R 序先加 innermost 边。由于 `ArgPriorityGraph::AddEdge` 会判环拒绝成环边（见 [`arg_list_reorder.h:60-83`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/arg_list_reorder.h#L60-L83)），先连的边在冲突时获胜——这就是 `prefer_reduce_tile_` 一个布尔就能翻转 R 轴/尾轴优先级的全部机关。父轴→子轴边（[`arg_list_reorder.cpp:115-133`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/arg_list_reorder.cpp#L115-L133) 的注释解释：子轴优先级若高于父轴，子轴切太大容易超 UB，属于功能性问题）永远最先建立。

**(e) 等序落地：SetAxesSameOrder + MakeSureLoadStoreInnerestSameOrder**

旧的 `MakeSureLoadStoreInnerestSameOrder` 只负责把 Load/Store 节点的 Tile 切分轴拉到同一 order（transpose 场景）。本次重构抽出通用函数 `SetAxesSameOrder`，并让 Reduce/tail 等序组也走这条路：

[`arg_list_reorder.cpp:647-664`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/arg_list_reorder.cpp#L647-L664) —— `SetAxesSameOrder` 把 `axis_names` 集合内所有「INNER 且未绑多核」的轴的 `order` 统一改成它们中的最小值，即「等序」。

[`arg_list_reorder.cpp:666-705`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/arg_list_reorder.cpp#L666-L705) —— 新版 `MakeSureLoadStoreInnerestSameOrder`：等序组必须恰好 2 根轴且都在 arg_list 里是 Tile 切分轴，否则保原序；若 Reduce/tail 等序组与 Load/Store 等序组**有重叠**，合并后超过 2 根轴就放弃 Reduce/tail 平衡、保 Transpose 等序；无重叠则两组各自等序。这条门禁保证了新机制不会破坏既有 transpose 等序 tiling 的行为。

> 小结：`ArgListReorder` 改的是 `ModelInfo::arg_list` 的**顺序与 order 值**，不改变任何变量、目标或约束本身。它是「求解之前的一道轴序整形」，让 4.4 节的求解器在更好的搜索次序上工作。

#### 4.3.4 代码实践

**实践目标**：给定一个具体 Reduce shape，手工推演它会命中四档策略中的哪一档，并能在日志中验证。

**操作步骤**：

1. 打开 [`arg_list_reorder.cpp:316-336`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/arg_list_reorder.cpp#L316-L336)，抄下两个阈值获取函数 `GetVectorLenSize()` / `GetCacheLineSize()` 的定义（它们查 `tuning_schedule_config_table`，具体数值平台相关，本仓库内未硬编码——具体值「待本地验证」，可在 NPU 环境用日志确认）。
2. 推演三个例子（假设 dtype 为 fp16，`s = 2` 字节）：
   - 例 A：`[M=64, K=4096]` 沿 K 归约 → \(B_{\text{reduce}} = 8192\) 字节，\(B_{\text{tail}} = 128\) 字节；
   - 例 B：`[M=8192, K=32]` 沿 K 归约 → \(B_{\text{reduce}} = 64\) 字节，\(B_{\text{tail}} = 16384\) 字节；
   - 例 C：`[M=?, K=?]` 动态 shape → repeat 求不出常量。
3. 对每个例子写出命中的策略：A → `kPreferTail`（R 轴远超向量长度、尾轴小于 cache line），B → `kKeepDefault`，C → `kFallback`。
4. 若有 NPU 环境，跑一个 reduce 用例并把日志级别开到 INFO，`grep "\[ATT\]\[ReduceTailBalance\]"`，应能看到 `Set Reduce axis [...] and tail axis [...] to equal order.` / `Prefer tail axis [...] before Reduce axis [...]` 等四档日志（日志埋点见 [`arg_list_reorder.cpp:432-444`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/arg_list_reorder.cpp#L432-L444)）；无环境则标注「待本地验证」。

**需要观察的现象**：四档策略是**互斥且穷尽**的——静态可算的字节必落前三档之一；日志只会打出其中一条，不会叠加。

**预期结果**：你能向别人解释「同样是 Reduce 图，为什么 `[64, 4096]` 和 `[8192, 32]` 会得到相反的轴序，而动态 shape 会退回旧的 runtime rule 机制」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `FindReduceTileAxis` 发现多个 R tile 轴时直接返回 `nullptr` 放弃重排？

> **答案**：四档策略建立在「一根 R 轴对一根尾轴」的二元平衡模型上；多根 R tile 轴时无法用单一 `(reduce_bytes, tail_bytes)` 刻画收益方向，瞎排反而可能劣化。保原序（fallback）是保守但安全的选择。同理 `CollectReduceTailAxis` 要求尾轴唯一、dtype 一致。

**练习 2**：`kEqual`（等序）与 `kPreferTail`（翻转轴序）的本质区别是什么？为什么 `kEqual` 需要下游求解器配合？

> **答案**：`kPreferTail` 是**编译期裁定**的固定先后（改连边顺序直接改变拓扑序）；`kEqual` 只是让两根轴的 `order` 相同，**具体怎么切留给运行期求解器**按真实 shape 权衡，因此需要 `axes_reorder_solver_code.h` 里 `enable_equal_order_tiling` 的求解器代码（见 [`axes_reorder_solver_code.h:44-45`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass/axes_reorder_solver_code.h#L44-L45)）支持在等序轴之间分配 tiling 空间。

**练习 3**：`MakeSureLoadStoreInnerestSameOrder` 里「合并后超过 2 根轴就放弃 Reduce/tail 平衡」的门禁在防什么？

> **答案**：防止新机制破坏既有 transpose 场景的等序 tiling。Load/Store 等序组是 transpose 模板正确性的前提；若 Reduce/tail 组与它重叠且合并集合超过 2 根轴，说明同一批轴上 transpose 语义更关键，此时保 Transpose 等序、跳过 Reduce R/尾轴平衡，是「新优化不回归老场景」的守门逻辑。

---

### 4.4 solver 求解（GeneralSolver）

前三节把优化问题「描述」清楚并整好了轴序。本节看求解器如何「求解」——也就是 ATT 真正做选择的地方。

#### 4.4.1 概念说明

先澄清一个常见误解：**ATT 的求解器不是在编译期跑的，而是在 kernel 运行期跑的。** 编译期，`solver_pass` 把 `ModelInfo` 里的目标与约束「打印」成一段 C++ 求解器源码，随 kernel 一起编译进 `.o`；运行期 kernel 下发时，求解器拿到真实 shape（即把 `arg_list` 里那些符号变量代入具体值域），在可行域内搜索使目标最小的 tiling。这种「编译期生成求解器、运行期执行搜索」的设计，正是 Autofuse 支持动态 shape 的关键——shape 不必在编译期已知。4.3 节的轴重排发生在这个翻译之前：它决定了求解器枚举切分时的轴优先序（以及等序组），会随求解器代码一起被「烤」进产物。

求解器要解决的优化问题形式化如下：

\[
\begin{aligned}
\min\ & f(\mathbf{x}) = \text{聚合各 pipe 的 } \text{Cost}_p \\
\text{s.t.}\ & \text{leq}_i(\mathbf{x}) \le 0, \quad i=1,\dots,m \quad \text{（含 UB 容量约束 + 轴关系约束）}
\end{aligned}
\]

其中 \(\mathbf{x}\) 是 `arg_list` 里的决策变量（tile 切分轴大小，顺序已经 4.3 节整形）。

**目标与约束在求解器里的不同待遇**（这是理解 `GeneralSolver` 的钥匙）：

- **约束**（`leq`）：是「可行性判据」。`CheckValid()` 要求所有 `leq_i ≤ 0` 才算可行解；不满足就被丢弃或驱动变量回到可行域。
- **目标**（`obj`）：是「择优判据」。在可行解之间，目标值越小越好。
- **缓冲约束的特殊性**：UB 占用 − 容量 = 余量 `cons_expr`。求解器把它的正部 `penalty = Max(cons_expr, 0)` 当作「越界惩罚」，负部 `remain = Min(cons_expr, 0)` 当作「可行冗余」。这样 UB 既是硬约束（`CheckValid` 用 `cons_expr ≤ 0`），又在微调时用「冗余」指导搜索（`DILATED` 策略）。

ATT 的求解器 `GeneralSolver` 是一个**启发式迭代搜索器**（非精确求解），核心是两阶段循环：**定域（LocateRegion）** 把变量从不可行域拉进可行域，**微调（FineTune）** 在可行域内沿目标下降方向找更优解。它带动量（momentum）、访问去重（visited node）、早停（early stop）和 top-N 解保留。

#### 4.4.2 核心流程

**编译期：把 ModelInfo 翻译成求解器代码**

```
SolverPassManager.GenFuncPass():
  solver_gen.SetObj( args_manager.GetObjectFunc() )        # 目标 objects -> GetObj/GetSmoothObj
  solver_gen.SetBufferCons( args_manager.GetTotalHardwareCons() )  # hardware_cons -> BUFFER 约束
  solver_gen.SetCutCons( args_manager.GetTotalCutCons() )  # 轴关系 -> LEQ 约束
  ... 生成 GeneralSolver<SpecificCase> 的 C++ 源码 ...
```

**运行期：求解器主循环（GeneralSolver::Run）**

```
Run(solution_num, solutions):
  has_feasible = false
  for iter in 1..cfg_iterations:
      Initialize(iter)                       # 重置动量、更新约束值、判断当前是否可行
      if 当前解不可行:
          LocateRegion()                     # 定域：由约束驱动变量走入可行域
          若找不到有价值的更新 -> 早停 break
      else:
          if 该可行解已被访问过 -> 早停 break
          FineTune()                         # 微调：沿目标下降方向找更优可行解
  result.GetResult(solution_num, solutions)  # 输出 top-N 解
```

#### 4.4.3 源码精读

**(a) ModelInfo → 求解器的接线点**

`SolverPassManager` 把 `ModelInfo` 的三大要素喂给代码生成器，这一步是「问题」与「求解器」的咬合点：

[`solver_pass_manager.cpp:37-42`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass_gen/solver_pass_manager.cpp#L37-L42) —— `SetObj`（目标）、`SetBufferCons`（硬件约束）、`SetCutCons`（轴切分约束）三连，分别对应 `args_manager_` 从 `ModelInfo` 取出的 `objects`、`hardware_cons`、轴关系约束。

**(b) 缓冲约束如何变成「余量 + 惩罚」**

`SetBufferCons` 是理解「UB 约束」最关键的一段：

[`general_solver_gen.cpp:101-125`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass_gen/general_solver/general_solver_gen.cpp#L101-L125) —— 对每个硬件约束，`cons_expr = 占用 − 容量`：

```cpp
cons_expr   = af::sym::Sub(pair.second, hardware_expr);   // 占用 − 容量（>0 即越界）
remain      = af::sym::Min(cons_expr, 0);                  // 可行冗余（≤0）
penalty     = af::sym::Max(cons_expr, 0);                  // 越界惩罚（≥0）
leqs_.emplace_back(cons_expr);                             // 作为一条 leq 约束登记
```

这说明求解器守的 UB 约束就是 **`占用 − 容量 ≤ 0`**，即「UB 占用不得超过 UB 容量」。`SetObj` 则简单地把每 pipe 目标收集起来：

[`general_solver_gen.cpp:58-66`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass_gen/general_solver/general_solver_gen.cpp#L58-L66) —— `obj_[pipetype] = pair.second`，供后续生成 `GetObj`/`GetSmoothObj` 聚合成标量目标。

**(c) 求解器主循环与可行性判据**

`GeneralSolver::Run` 是运行期搜索的入口：

[`general_solver_code.h:1770-1817`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass/general_solver_code.h#L1770-L1817) —— 最多迭代 `cfg_iterations` 次：不可行时调 `LocateRegion()`，可行时调 `FineTune()`；命中「重复访问的可行解」或「找不到有价值更新」即早停；最后 `result_->GetResult` 输出解。

可行性判据 `CheckValid` 极简：

[`general_solver_code.h:822-842`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass/general_solver_code.h#L822-L842) —— 遍历所有 `leq`，只要有一条 `leqs[i] > 0` 就返回 `false`。这正是「所有约束 `≤ 0` 才可行」的形式化落地。

每轮找到可行解后记录目标与缓冲冗余，用于 top-N 排序：

[`general_solver_code.h:1733-1748`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass/general_solver_code.h#L1733-L1748) —— `RecordBestVarVal` 调 `GetObj`（目标值）与 `GetBuffCost`（缓冲冗余），通过 `result_->AddVarVal(vars, obj, cons)` 入选；`AddVarVal`（见 `general_solver_code.h:608-632`）按 `obj` 升序、`obj` 相同则按 `cons` 升序保留前 `top_n` 个。这说明**求解器主排序键就是目标值 obj（执行时间），次排序键是缓冲冗余**。

**(d) 求解器超参数**

求解器质量与耗时的权衡由一组常量控制：

[`general_solver_code.h:17-42`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass/general_solver_code.h#L17-L42) —— `cfg_top_num=5`（保留最优 5 个解）、`cfg_search_length=1`（局部搜索范围）、`cfg_iterations=100`（迭代上限）、`cfg_simple_ver=true`（高效率版/高性能版切换）、`cfg_momentum_factor=0.9`（动量因子）。注释明确：搜索范围/迭代数越大越可能更优但更慢。

> 小结：求解器最小化的是**聚合后的执行时间目标**，必须满足的约束是**所有 `leq ≤ 0`**（含 UB 占用 ≤ 容量、轴关系）。它用启发式两阶段搜索（定域+微调）在运行期求出一个近似最优 tiling，保留 top-N 解。4.3 节重排过的轴序决定了它的搜索起点与方向。

#### 4.4.4 代码实践

**实践目标**：搞清「求解器最小化的目标」与「必须满足的约束」在源码里的具体形态，能向别人讲清二者的区别。

**操作步骤**：

1. 打开 [`solver_pass_manager.cpp:37-42`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass_gen/solver_pass_manager.cpp#L37-L42)，确认 `SetObj / SetBufferCons / SetCutCons` 三者分别取自 `args_manager_` 的哪个方法。
2. 打开 [`general_solver_gen.cpp:101-125`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass_gen/general_solver/general_solver_gen.cpp#L101-L125)，把 `cons_expr / remain / penalty` 三个量各自的含义写下来。
3. 打开 [`general_solver_code.h:822-842`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass/general_solver_code.h#L822-L842) 的 `CheckValid`，回答：「一个候选 tiling 满足什么条件才算可行？」
4. 打开 [`general_solver_code.h:1733-1748`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass/general_solver_code.h#L1733-L1748) 的 `RecordBestVarVal`，回答：「两个都可行的 tiling，求解器靠什么字段比优劣？」

**需要观察的现象**：

- 目标 `obj` 只在「都可行」时才用来比优劣（`RecordBestVarVal` 仅在 `is_feasible_` 时调用）；
- 约束 `leq`（含 UB）先决定可行性，不满足的根本进不了 top-N。

**预期结果**：你能写出——**「solver 最小化的是 `GetObj` 返回的执行时间标量；必须满足 `CheckValid` 即所有 `leq ≤ 0`，其中 UB 约束形如『占用 − 容量 ≤ 0』。」** 如果想进一步看运行期求解过程，可在生成的 kernel host 代码里打开 `OP_LOGD`（求解器内大量 `OP_LOGD(OP_NAME, "iter : %lu", iter)` 日志），标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`SetBufferCons` 里 `remain = Min(cons_expr, 0)` 和 `penalty = Max(cons_expr, 0)` 分别有什么用？

> **答案**：`penalty`（越界量，≥0）用于在不可行时驱动变量减小占用、回到可行域（定域阶段的下降方向）；`remain`（冗余，≤0）用于在可行时判断还有多少 UB 余量可「膨胀」（`DILATED` 微调策略沿缓冲边界探索，尽量用满 UB 以换取更优目标）。两者来自同一条 `cons_expr`，分别服务「修复不可行」与「可行内择优」。

**练习 2**：为什么 `RecordBestVarVal` 只在 `is_feasible_ == true` 时才把解加入 `Result`？

> **答案**：求解器只保留**可行解**。不可行的候选（违反某条 `leq`）即使目标值再小也不入库，因为运行期无法使用。`CheckValid()` 是入库的闸门。

**练习 3**：把 `cfg_iterations` 从 100 调到 500，求解结果会怎样？

> **答案**：迭代上限提高，求解器有更多机会逃离局部最优、逼近更优解（目标值可能更小），但 kernel 下发时的求解耗时也会增加。`cfg_simple_ver` 同理：高性能版（`false`）检查搜索范围内所有可行解、变量顺序更精细，解更优但更慢。这是「求解质量 vs 下发耗时」的权衡。

---

## 5. 综合实践

把本讲四节串起来，完成一次「从图到轴序再到 tiling」的完整追踪。

**任务**：选取仓库里一个 pointwise 示例（如 `autofuse/examples/pytorch/af_pointwise/af_add_ge.py`，u3-l3 已介绍），再找一个带 Reduce 的用例（可在 `autofuse/tests/` 下检索 `reduce` 相关 e2e 用例），开启 DFX dump 跑通后，回答下列问题，并把答案与源码位置一一对应：

1. **变量**：该用例的 `model_info.json` 里 `arg_list` 有几个变量？它们对应图里的哪些轴？→ 对照 `GetSubAxisArgs`（[`generate_tiling_expr.cpp:173-199`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/generate_tiling_expr.cpp#L173-L199)）。
2. **目标**：`objects` 里出现了哪几个 pipe？每个 pipe 的表达式里能找到「`exe_time * perf`」的乘积项吗？→ 对照 `AddPerf`（[`pipe_perf_expr.cpp:476-498`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/pipe_perf_expr.cpp#L476-L498)）。
3. **约束**：`hardware_cons` 里 `UB` 桶的表达式由几项相加？它们分别对应哪些 container？→ 对照 `GetBufferOccupInContainer`（[`buf_occupy_expr.cpp:110-142`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/buf_occupy_expr.cpp#L110-L142)）。
4. **轴序**：Reduce 用例里 `arg_list` 的顺序是否把 R 轴排在了尾轴之前？日志里能 grep 到哪条 `[ATT][ReduceTailBalance]` 记录？对应四档策略中的哪一档？→ 对照 `RecordReduceTileTemplateSelection`（[`arg_list_reorder.cpp:421-471`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/gen_model_info/expr_gen/arg_list_reorder.cpp#L421-L471)）。
5. **求解**：在生成的 host 代码里定位 `GeneralSolver::Run`，统计实际迭代了几轮（看 `iter : %lu` 日志），并解释为什么没有跑满 `cfg_iterations`（早停触发）。

**交付物**：一张表，左列是上述 5 个问题，右列是「json 字段值/日志行 + 对应源码行号 + 一句话解释」。若本地无 NPU 环境无法实跑，第 1~4 题可纯靠「读源码 + 推演」完成，第 5 题标注「待本地验证」。

## 6. 本讲小结

- **表达式生成（`GenerateTilingExpr::Generate`）** 是 ATT 的「翻译官」：把 `TuningSpace` 翻成 `ModelInfo`，每一步只填一个字段——`GetBufConstraint→hardware_cons[UB]`、`GetCoreConstraint→hardware_cons[CORENUM]`、`GetPipePerformance→objects`、`GetAxisConstraints→eq/leq_exprs`、`GetSubAxisArgs→arg_list`；本次重构把它包进私有函数 `GenerateSingleModelInfoWithContext`，用 `ModelGenerationContext` 传递内部门禁而不扩公开接口。
- **缓冲占用（`BufOccupyExpr`）** 用「同存组内求和、组间取 max、再乘 buffer_num」算出每个硬件 scope 的占用，累加成 UB 容量约束 `占用 ≤ 容量`。
- **执行时间（`PipePerfExpr`）** 按 pipe 累加 `t_n · c_{n,p}` 并叠加头开销 \(H_p\)，得到目标 `objects`；其中循环次数 `t_n` 由 `ExeTimePassManager` 在广播缓存场景下压缩。
- **轴重排（`ArgListReorder`，本次新增重点）** 用 `R 轴字节数 vs 向量长度`、`尾轴字节数 vs cache line` 把 Reduce R/尾轴的切分优先级分成 `kPreferTail / kKeepDefault / kEqual / kFallback` 四档；`kEqual` 通过 `SetAxesSameOrder` 让两根轴等序、把最终取舍交给运行期求解器，且对多 R 轴、多尾轴、dtype 不一致、与 Transpose 等序组冲突等场景设了保守门禁。
- **求解器（`GeneralSolver`）** 在运行期执行，最小化聚合后的执行时间目标，必须满足所有 `leq ≤ 0`（含 UB 占用 ≤ 容量与轴关系）；用「定域（拉入可行域）+ 微调（可行内择优）」两阶段启发式搜索，保留 top-N 解。
- **目标 vs 约束**是理解整个 ATT 的钥匙：约束（`leq`/`CheckValid`）决定可行性，目标（`obj`/`RecordBestVarVal`）决定择优，UB 约束通过 `penalty/remain` 同时服务两者。
- ATT 是「编译期生成求解器、运行期执行搜索」的架构，这是支持动态 shape 的根本机制。

## 7. 下一步学习建议

- 下一讲 **u7-l3「Tiling 代码生成」** 将讲解 `att/generator/` 如何把求解器挑出的 tiling 落成最终的 tiling 代码与 tiling 数据（含 `axes_reorder` 求解代码如何携带本讲的轴序/等序信息），并衔接到下游 codegen（u8）。建议先回顾本讲的 `SolverPassManager` 与 4.3 的等序机制，它们是 u7-l3 的直接上游。
- 若想深入求解器算法，可精读 [`general_solver_code.h`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass/general_solver_code.h) 的 `LocateRegion`（定域）与 `FineTune`（微调）两个函数链，结合其中大量中文注释理解 `Locality`/`TunePriority` 优先级体系；AxesReorder 算法对应的 [`axes_reorder_solver_code.h`](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/att/generator/solver_pass/axes_reorder_solver_code.h) 是其变体。
- 若想理解单次开销 `c_{n,p}` 的来源，回看 u7-l1 的 `api_perf_register` 与 `perf_param_v1.cpp`，本讲的 `PipePerfExpr::GetNodePerf` 正是它的消费方。
