# 跨核流水与 CrossCorePipeline

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 Ascend 上 **Cube↔Vector 跨核流水（inter-core pipeline）** 解决的问题：两核经 GM/L2 workspace 中转数据时，如何用 `num_stages` 双（多）缓冲让「Cube 写 workspace」与「Vector 读 workspace」在时间上重叠，掩盖一次 GM 往返的延迟。
- 画出 **write workspace / read workspace 的时序表**，并理解 `num_stages` 控制的「领先几拍」与「消费滞后」关系。
- 掌握 **`CrossCorePipeline` pass** 的完整工作链路：它如何 **检测** 一个 `T.Pipelined` 循环是否跨核、如何把单层循环 **改写成「外层波 + 内层 stage」两层循环**、如何给 workspace 与跨阶段复用的 buffer **加 `num_stages` 维**做成环形缓冲。
- 理解 `CrossCorePipeline` 产出的两个注解（`stage_loop`、`tl_cross_interval`）如何被 u5-l1 的 `CombineCV` 消费，落地成稀疏的 `set_cross_flag` / `wait_cross_flag`。
- 牢记两条 **硬约束**：①核间流水与核内流水不能同时开启；②一个 kernel 只允许 **一条** 跨核流水。

本讲是 u5（CV 分离与跨核机制）单元的第二讲，直接承接 u5-l1（`CombineCV` 与自动核间同步）和 u3-l6（`T.Pipelined` 软件流水的三段式），把「跨核时序」这一层正式展开。

## 2. 前置知识

进入源码前，先用三段话把「跨核流水为什么必要」讲透。这三段都建立在 u5-l1 已建立的事实之上。

**事实一：Cube 与 Vector 之间只能经 GM workspace 中转，且这一步很贵。** 一个 AI Core 内 Cube（AIC）写出的结果在 L0C，Vector（AIV）要用的数据在 UB，二者物理上不直连（u5-l1）。每一次「Cube 产出 → Vector 消费」都必须走 `copy_l0c_to_gm`（Cube 写 GM）+ `copy_gm_to_ub`（Vector 读 GM）两阶段（u3-l2 的「跨 CV 搬运」），这一来一回是一次完整的 GM 往返，延迟可观。如果让 Cube 与 Vector **串行** 配合——Cube 算完一拍、写 GM、Vector 才开始读——那 GM 往返的延迟就会原样暴露在关键路径上。

**事实二：流水（pipeline）的本质是「让生产者领先消费者几拍」。** u3-l6 讲过核内 `T.Pipelined`：用搬运掩盖计算。跨核流水是同一个思想换到核间：让 Cube **连续发出多拍** `write_wk1`（写 workspace），Vector **滞后一拍** 才开始 `read_wk1`（读 workspace）。于是第 `i` 拍的 GM 往返，被第 `i+1` 拍的 Cube 计算重叠掉了。`num_stages` 就是「领先几拍」=「环形缓冲副本数」，取 2 即双缓冲。

**事实三：谁来识别「这是个跨核流水」并改写循环？** 用户侧只写一个普通的 `for k in T.Pipelined(N, num_stages=2)`，循环体里 **同时** 出现 Cube 操作（`T.gemm_v0`、`copy_l0c_to_gm`）和 Vector 操作（`copy_gm_to_ub`、`T.tile.*`）。编译器里的 **`CrossCorePipeline` pass** 负责发现「这个带 `num_stages` 的循环跨了 Cube 与 Vector 两个 scope」，于是把它从「一层顺序循环」改写成「外层按波推进、内层按 stage 推进」的两层循环，并把 workspace 加一维做成 `num_stages` 个槽的环形缓冲。改写完，u5-l1 的 `CombineCV` 再接手做 CV 拆分与核间同步插入。

> 一句话区分两种 `T.Pipelined`：循环体 **只碰单一 scope**（纯 Cube 或纯 Vector）的是 **核内流水**，由 `PipelinePlanning`/`InjectSoftwarePipeline` 处理（u3-l6）；循环体 **同时碰 Cube 和 Vector** 的是 **跨核流水**，由 `CrossCorePipeline` 抢先处理。本讲讲后者。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/transform/cross_core_pipeline.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/cross_core_pipeline.cc) | 本讲核心。`CrossCorePipeline` pass 全部在此：`CrossCoreDetector`（检测）、`LoopAnalyzer`（分 scope + 找 workspace 写点）、`LoopRewriter`（拆 stage + 重写循环）、`BufferMapTransformer`/`ExtendAllBuffers`（workspace 加维）。 |
| [examples/pipeline/flash_attn_bshd_pipeline.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/pipeline/flash_attn_bshd_pipeline.py) | 跨核流水的完整实战样本：单个 `T.Pipelined` 内 Cube 算 QK/PV、Vector 做 softmax，三块 workspace 双向中转。 |
| [tilelang/language/pipeline.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/pipeline.py) | `T.Pipelined` 前端，新增的 `cross_interval` 形参定义在此。 |
| [tilelang/engine/phase.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py) | `OptimizeForTarget` 里 `CrossCorePipeline → CombineCV → PipelinePlanning` 的顺序，解释「谁先谁后」。 |
| [src/transform/ascend_combinecv.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_combinecv.cc) | u5-l1 主角。这里只看它如何 **消费** `CrossCorePipeline` 产出的 `stage_loop`/`tl_cross_interval` 注解，落地条件化 cross flag。 |
| [docs/TileLang-Ascend Programming Guide.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md) | 编程指南 4.1.4.2 节给出跨核流水的权威时序表与两条硬约束。 |

## 4. 核心概念与源码讲解

### 4.1 跨核流水的时序：write workspace 与 read workspace 如何重叠

#### 4.1.1 概念说明

先把「跨核流水」的物理图像建立起来。假设一次循环里 Cube 要算出一块结果交给 Vector：Cube 侧的工作记作 `write_wk1`（含 `copy_l0c_to_gm`，把 L0C 写进 GM 的 workspace_1），Vector 侧的工作记作 `read_wk1`（含 `copy_gm_to_ub`，从 workspace_1 读进 UB 再做后续向量计算）。

若 **不开流水**，两核只能一前一后串行：Cube 写完第 0 拍，Vector 才能读第 0 拍，GM 往返延迟全程暴露。开 `num_stages=2` 后，Cube 会 **连续发出两拍** `write_wk1`，Vector 从第二拍才开始消费，于是「写第 i+1 拍」与「读第 i 拍」在时间上重叠——这就是跨核流水的全部目的。

#### 4.1.2 核心流程

编程指南 4.1.4.2 节以 `T.ceildiv(seq_len, block_N)=4`、`num_stages=2` 给出了权威时序表（这正是本讲综合实践的参考答案）：

| Time | Write Workspace（Cube） | Read Workspace（Vector） |
| ---- | ----------------------- | ------------------------ |
| t₀   | **write_wk1_0**         |                          |
| t₁   | **write_wk1_1**         | **read_wk1_0**           |
| t₂   | **write_wk1_2**         | **read_wk1_1**           |
| t₃   | **write_wk1_3**         | **read_wk1_2**           |
| t₄   |                         | **read_wk1_3**           |

> —— [docs/TileLang-Ascend Programming Guide.md:1160-1168](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L1160-L1168)，中文说明：`num_stages=2` 时 Cube 与 Vector 在 t₁~t₃ 之间存在重叠，掩盖了 GM 往返延迟。

关键量：

- **领先拍数 = `num_stages - 1`**。`num_stages=2` 时 Cube 领先 1 拍，t₀ 先独自写一拍，t₁ 起 Vector 才跟上。
- **环形缓冲副本数 = `num_stages`**。因为 Cube 在写第 `i+1` 拍时 Vector 还在读第 `i` 拍，两拍的数据 **同时存活**，所以 workspace 必须有 `num_stages` 个槽，按 `stage % num_stages` 取模复用——这与 u3-l6 核内流水的 `floormod` 缓冲版本化是同构的。
- **尾段**：t₄ Cube 已无新数据可写，只剩 Vector 收尾读第 3 拍。

#### 4.1.3 源码精读

这个时序的源头，是用户写的一个普通 `T.Pipelined` 循环，体里同时含 Cube 与 Vector 操作。以 FlashAttention 为例（综合实践会逐句读它）：

[examples/pipeline/flash_attn_bshd_pipeline.py:79-83](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/pipeline/flash_attn_bshd_pipeline.py#L79-L83) —— 中文说明：`T.Pipelined(num_stages=2)` 循环里，`T.gemm_v0(...)` 是 Cube 操作，紧接的 `T.copy(acc_s_l0c, workspace_1[...])` 是 Cube 写 GM workspace——这就是 `write_wk1`。

[examples/pipeline/flash_attn_bshd_pipeline.py:86-88](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/pipeline/flash_attn_bshd_pipeline.py#L86-L88) —— 中文说明：随后 `T.copy(workspace_1[cid, vid*...], acc_s_ub_)` 把同一块 workspace 读进 UB——这就是 Vector 侧的 `read_wk1`。Cube 写、Vector 读同一块 workspace，是跨核的标志。

#### 4.1.4 代码实践

**实践目标**：用纸笔把「不开流水」与「`num_stages=2`」两种时序画出来，直观感受重叠。

**操作步骤**：

1. 假设循环长度 `N=4`，仿照上面的表，先画 **串行版**：t₀ write_0, t₁ read_0, t₂ write_1, t₃ read_1, ...，共 8 个时间槽。
2. 再画 **`num_stages=2` 版**（即上表），共 5 个时间槽。
3. 计算两种情况的总时间与「GM 往返被掩盖的比例」。

**需要观察的现象**：串行版里每个 `write` 与 `read` 各占独立时间槽、无重叠；流水版里 t₁~t₃ 同时存在一写一读。

**预期结果**：流水版总时间约 `N + (num_stages - 1)` 拍，相比串行版的 `2N` 拍，节省接近一半；`num_stages` 越大领先越多，但 workspace 占用与片上缓冲也线性增长。

#### 4.1.5 小练习与答案

**练习 1**：把 `num_stages` 从 2 调到 3，时序表会变成什么样？workspace 需要几个槽？

**参考答案**：Cube 连发 3 拍后 Vector 才开始消费，t₀ write_0、t₁ write_1、t₂ write_2+read_0、t₃ write_3+read_1、t₄ read_2、t₅ read_3，总时间 `N + (num_stages-1) = 4+2 = 6` 拍。workspace 需要 3 个槽，因为第 i、i+1、i+2 拍的数据可能同时存活。可见 `num_stages` 是「延迟掩盖」与「缓冲占用」之间的权衡旋钮。

**练习 2**：为什么跨核流水的 workspace 必须放在 GM，而不能放在 L1 或 UB？

**参考答案**：因为 L1 属 Cube、UB 属 Vector，两者是各自核的私有片上存储，对方核访问不到（u5-l1）。GM/L2 是两核共享的唯一数据通道，所以跨核交换的数据中转点只能在 GM。这也正是跨核流水要专门处理「GM 往返延迟」的原因——核内流水的搬运在 GM↔L1/UB，跨核流水的「搬运」退化为 GM workspace 的写读往返。

---

### 4.2 T.Pipelined 的跨核形态与 cross_interval

#### 4.2.1 概念说明

用户侧的写法极其简单：`for k in T.Pipelined(N, num_stages=2)`。前端并不会区分「核内」还是「跨核」——这个判定完全交给编译器（见 4.3）。`T.Pipelined` 只是建出一个带 `num_stages` 注解的 TIR `For` 节点（u3-l6 已建立）。

唯一的跨核专用旋钮是 **`cross_interval`**：它控制「核间同步的稀疏度」。默认 `cross_interval=1`，表示 Cube 与 Vector **每一拍** 都要同步一次（写完一拍就置 flag、读前一拍就等 flag）。把它设成 `N`，则 **每 N 拍** 才同步一次，减少 flag 总数、降低同步开销——代价是 workspace 要能容纳更多在途数据。这个旋钮在 4.4 里会看到它如何落到条件化的 `set/wait_cross_flag`。

#### 4.2.2 核心流程

`T.Pipelined` 前端签名里，`cross_interval` 是最后一个形参，默认 1，透传给 FFI：

```text
T.Pipelined(start, stop, num_stages, order, stage, sync, group, cross_interval=1)
                              ↓ _ffi_api.Pipelined(...)
        建出带 num_stages 注解的 For 节点（跨核判定在 pass 侧做）
```

`num_stages` 注解是后续一切的关键：`CrossCorePipeline` 靠它定位候选循环，`CombineCV` 靠它知道缓冲要开几份。

#### 4.2.3 源码精读

`cross_interval` 的定义与文档：

[tilelang/language/pipeline.py:32-35](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/pipeline.py#L32-L35) —— 中文说明：`cross_interval` 形参的语义——1 表示每拍同步、N 表示每 N 拍同步一次。

[tilelang/language/pipeline.py:52](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/pipeline.py#L52) —— 中文说明：`cross_interval` 连同其它参数一起透传给 C++ 的 `Pipelined` 构造，最终落到 `For` 节点的注解里。

#### 4.2.4 代码实践

**实践目标**：理解「不指定 `cross_interval`」时走的是默认每拍同步。

**操作步骤**：

1. 打开 `examples/pipeline/flash_attn_bshd_pipeline.py`，定位第 79 行的 `T.Pipelined(..., num_stages=2)`，确认它 **没有** 传 `cross_interval`。
2. 想象把它改成 `T.Pipelined(T.ceildiv(seq_len, block_N), num_stages=2, cross_interval=2)`。
3. 对照 4.4 的同步条件公式，推断 flag 会在哪些 stage 触发。

**需要观察的现象**：默认 `cross_interval=1` 时，每个 stage 都有 set/wait flag；改成 2 后，flag 数减半（每两个 stage 才一对）。

**预期结果**：`cross_interval` 增大会减少核间同步次数，但同时要求 workspace 能容纳更多在途拍数的数据。本例仅做源码阅读推断，运行验证「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`cross_interval` 与 `num_stages` 是同一个概念吗？

**参考答案**：不是。`num_stages` 是「环形缓冲副本数 / 领先拍数」，决定能重叠几拍；`cross_interval` 是「同步频率」，决定每几拍插一对 cross flag。二者正交：可以 `num_stages=2, cross_interval=1`（双缓冲、每拍同步），也可以 `num_stages=2, cross_interval=4`（双缓冲、每 4 拍同步一次）。

---

### 4.3 CrossCorePipeline pass：检测与循环改写

#### 4.3.1 概念说明

`CrossCorePipeline` 是本讲主角。它做三件事：

1. **检测**：扫所有带 `num_stages` 注解的循环，看循环体里是否 **同时** 触碰 Cube buffer（`shared.l1`、`wmma.*`）与 Vector buffer（`shared.ub`、`local.var`）。若都碰到，标记为「跨核流水」。
2. **改写循环**：把单层 `for k in [0, N)` 拆成 **外层波循环** `for outer in [0, N/num_stages)` + **内层 stage 循环** `for i in [0, num_stages)`，并用 `k = outer*num_stages + i` 还原原下标。
3. **加维缓冲**：给 workspace（GM 参数，名字以 `workspace` 开头）与跨 stage 复用的片上 buffer 都在最前面加一维 `num_stages`，做成环形缓冲；每个 stage `i` 访问第 `i` 槽。

改写后，下游的 `CombineCV`（u5-l1）再做 CV 拆分与核间同步。

#### 4.3.2 核心流程

```text
CrossCorePipeline::Transform(f):
  1. 收集 location_map_（每个 buffer 的 scope）
  2. CrossCoreDetector.DetectCrossCorePipelines(f.body)
       → 遍历所有带 num_stages 注解的 For
       → 体里既见 cube buffer 又见 vec buffer → is_cross_core=true
  3. ICHECK(cross_core_pipelines_.size() == 1)   # 只允许一条跨核流水
  4. BufferMapTransformer.TransformBufferMap     # GM workspace 参数加 num_stages 维
  5. LoopAnalyzer.Analyze                        # 体里每句标 cube/vec，记 workspace 写点
  6. LoopRewriter.Rewrite:
       SplitIntoStages       # 按 scope 变化切成若干 stage
       AnalyzeSharedBuffers  # 找出跨 stage 复用的 buffer
       CreateStagedLoops     # 外层波 + 内层 stage，注解 stage_loop / tl_cross_interval
  7. ExtendAllBuffers         # 片上 workspace/shared buffer 加 num_stages 维
  8. AdjustBuffersAndAccess   # 每个 stage 的访存偏移 += stage_var * size（A5 平台跳过）
```

检测的核心判据是「循环体里同时出现两个 scope 的 buffer」。这靠一张 scope 映射表：

[src/transform/cross_core_pipeline.cc:43-46](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/cross_core_pipeline.cc#L43-L46) —— 中文说明：`callnodeMapPos_` 把每个 buffer scope 映射到 `cube`（`wmma.matrix_a/b`、`wmma.accumulator`、`shared.l1`）或 `vec`（`shared.ub`、`local.var`），这是「一句话属于哪个核」的权威依据。

#### 4.3.3 源码精读

**检测器 `CrossCoreDetector`**：遍历每个带 `num_stages` 注解的循环，记下它的 `PipelineInfo`，再扫描体内语句。

[src/transform/cross_core_pipeline.cc:79-101](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/cross_core_pipeline.cc#L79-L101) —— 中文说明：进入一个带 `num_stages` 注解的 `For` 时，新建 `PipelineInfo`（默认 `is_cross_core=false`、`scene=INVALID_SCOPE`），递归访问循环体，结束后回填。

[src/transform/cross_core_pipeline.cc:103-126](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/cross_core_pipeline.cc#L103-L126) —— 中文说明：对每条 `Evaluate` 语句，取其参数 buffer 的 scope；若第一次见到某 scope 就记进 `scene`，若再见到 **不同** scope 就把 `is_cross_core` 置真——这就是「跨核」的判定瞬间。

**「只允许一条」的硬约束**：

[src/transform/cross_core_pipeline.cc:1283-1286](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/cross_core_pipeline.cc#L1283-L1286) —— 中文说明：`ICHECK(cross_core_pipelines_.size() == 1)`，一个 kernel 只能有一条跨核流水，多于一条直接编译报错。

**GM workspace 参数加维**：函数参数里名字以 `workspace` 开头的 GM buffer，在最前面插一维 `num_stages`（并重算 stride），做成多槽环形缓冲。

[src/transform/cross_core_pipeline.cc:156-174](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/cross_core_pipeline.cc#L156-L174) —— 中文说明：`IsWorkspaceBuffer` 按「名字以 `workspace` 开头」识别 GM 中转 buffer；`CreateResizedBuffer` 给它 `shape` 前插 `num_stages` 维、相应补 `stride[0]`，从而变成 `num_stages` 个槽的环形缓冲。

**循环体分析 `LoopAnalyzer`**：把体内每条语句按 scope 分到 `all_statements_C_`（Cube）或 `all_statements_V_`（Vector），并识别「写 GM workspace」的语句。

[src/transform/cross_core_pipeline.cc:259-264](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/cross_core_pipeline.cc#L259-L264) —— 中文说明：两张表——`IS_WRITE_GM`（`copy_l0c_to_gm`/`copy_ub_to_gm`/`atomic_add_*_to_gm`）与 `IS_PIPE_WRITE`（`copy_l0c_to_pipe`/`copy_ub_to_pipe`）——用来判定哪些语句是「向 GM/pipe 写出一拍数据」，这些就是跨核同步的生产者。

**按 scope 切 stage**：把语句序列按 scope 变化切成若干连续同 scope 的段，每段一个 stage。

[src/transform/cross_core_pipeline.cc:1142-1168](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/cross_core_pipeline.cc#L1142-L1168) —— 中文说明：`SplitIntoStages` 顺序扫描语句，scope 一变就开新 stage，于是 `[C,C,V,V,C,V]` 被切成 `[CC][VV][C][V]` 四个 stage——每个 stage 内部是单一 scope 的连续语句。

**两层循环重写**：外层波循环把原 `N` 次迭代压成 `N/num_stages` 次，内层 `stage_loop` 跑 `num_stages` 次，原下标 `k` 由 `outer*num_stages + i` 还原。

[src/transform/cross_core_pipeline.cc:1630-1648](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/cross_core_pipeline.cc#L1630-L1648) —— 中文说明：`ModifyOuterLoop` 把外层循环 extent 改成 `原extent / num_stages`，并打 `tl_original_extent` 注解保留原值。

[src/transform/cross_core_pipeline.cc:1096-1125](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/cross_core_pipeline.cc#L1096-L1125) —— 中文说明：`AddLetStmtBinding` 用 `LetStmt` 绑定 `k_transformed = outer*num_stages + i`，再把体内所有 `k` 替换成 `k_transformed`——于是原循环变量被两层循环的线性组合精确还原。

[src/transform/cross_core_pipeline.cc:1127-1140](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/cross_core_pipeline.cc#L1127-L1140) —— 中文说明：内层 `stage_loop` 带三个关键注解——`stage_loop=True`（让 `CombineCV` 识别它是跨核 stage 循环）、`tl_cross_interval`（同步稀疏度）、`tl_original_loop_var`（原循环变量名）。这三个注解是 `CrossCorePipeline` 与 `CombineCV` 之间的握手协议。

**片上缓冲加维**：对 `alloc_buffers` 里是 workspace 或「跨 stage 复用」（shared_buffers）的 buffer，在最前面加 `num_stages` 维。

[src/transform/cross_core_pipeline.cc:1401-1435](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/cross_core_pipeline.cc#L1401-L1435) —— 中文说明：`ExtendAllBuffers` 对 workspace 与 shared buffer 的 `shape` 前插 `num_stages` 维，并把它登记进 `collected_buffer_versions_`（`buffer_versions` 函数属性），供下游访存调整使用。

**按 stage 调整访存偏移**：每个 stage `i` 访问环形缓冲的第 `i` 槽，靠给偏移加 `stage_var * total_size` 实现（A5 仿真平台跳过此步）。

[src/transform/cross_core_pipeline.cc:1499-1518](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/cross_core_pipeline.cc#L1499-L1518) —— 中文说明：`BufferAccessAdjuster` 在 `stage_loop` 作用域内，对 workspace/shared buffer 的首个下标加上 `stage_var * total_size`，从而让 stage 0/1/... 各自落到环形缓冲的不同槽——这是「领先几拍」在 IR 层的落点。

**pass 在流水线中的位置**：`CrossCorePipeline` 排在 `CombineCV` **之前**，更在 `PipelinePlanning`/`InjectSoftwarePipeline`（核内软件流水）之前。

[tilelang/engine/phase.py:98-101](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L98-L101) —— 中文说明：`OptimizeForTarget` 里 `CrossCorePipeline` 先把跨核循环改写成两层 stage 循环，紧接着 `CombineCV` 做 CV 拆分，再由 `PipelinePlanning`/`InjectSoftwarePipeline` 处理剩余的核内流水——顺序保证了「跨核循环不会被核内流水 pass 重复处理」。

**pass 注册**：

[src/transform/cross_core_pipeline.cc:1659-1669](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/cross_core_pipeline.cc#L1659-L1669) —— 中文说明：注册为 `CreatePrimFuncPass`，名字 `tl.CrossCorePipeline`，并暴露为全局 `tl.transform.CrossCorePipeline`。

#### 4.3.4 代码实践

**实践目标**：用 `get_kernel_source()` 直接观察 `CrossCorePipeline` 改写后的循环结构。

**操作步骤**：

1. 打开 `examples/pipeline/flash_attn_bshd_pipeline.py`，在文件末尾追加（示例代码）：
   ```python
   src = func.get_kernel_source()[0].source
   print(src)
   ```
2. 在打印的 C++ 源码里，定位原本的 K 循环。你会看到它已经变成 **两层嵌套**：外层按波推进、内层是一个 `num_stages=2` 次的小循环。
3. 在源码里搜索 workspace 的访问，确认同一块 workspace 在不同 stage 被偏移到不同地址（即环形缓冲的不同槽）。

**需要观察的现象**：原本一层 `for k` 变成两层；workspace 访问的下标里出现与 stage 相关的偏移；循环体被拆成了 Cube 段与 Vector 段（由后续 `CombineCV` 完成，表现为 `if ASCEND_IS_AIC` / `if ASCEND_IS_AIV` 两分支）。

**预期结果**：生成代码里 K 循环呈两层结构，Cube 分支含 `copy_l0c_to_gm`（写 workspace），Vector 分支含 `copy_gm_to_ub`（读 workspace），两者通过 `CrossCoreSetFlag/WaitFlag` 协调。无 NPU 时可只编译看源码，运行验证「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`CrossCoreDetector` 是凭什么判定「这是个跨核流水」的？它看不看 `num_stages` 的具体数值？

**参考答案**：先看循环是否带 `num_stages` 注解（无注解直接跳过，不当作流水），再看循环体里是否 **同时** 出现 Cube scope buffer 与 Vector scope buffer（靠 `callnodeMapPos_` 把 `shared.l1`/`wmma.*` 判为 cube、`shared.ub`/`local.var` 判为 vec）。它只关心 `num_stages` 「有没有」，不关心「是几」——具体的 `num_stages` 值在后面 `ProcessCrossCorePipeline` 里才取出来用于切外层波数和加维。

**练习 2**：为什么 `CrossCorePipeline` 必须排在 `CombineCV` 之前？

**参考答案**：因为 `CrossCorePipeline` 要在「循环还是完整的单层」时做改写（切 stage、加维、打 `stage_loop` 注解）；而 `CombineCV` 会把循环体拆进两个 `resource_scope` 分支。若 `CombineCV` 先跑，循环体被打散到 Cube/Vector 两份代码里，`CrossCorePipeline` 就再也拼不回一个完整的跨核循环来做两层改写了。同时，`CombineCV` 的自动核间同步要依赖 `CrossCorePipeline` 留下的 `stage_loop`/`tl_cross_interval` 注解（见 4.4）。

---

### 4.4 跨核同步的下发：CombineCV 消费 stage_loop

#### 4.4.1 概念说明

`CrossCorePipeline` 自己 **不插同步**——它只做「改写循环 + 加维 + 打注解」，把 `stage_loop` 和 `tl_cross_interval` 两个注解留在 IR 里。真正插 `set_cross_flag`/`wait_cross_flag` 的是 u5-l1 讲过的 `CombineCV`（开关 `TL_ASCEND_AUTO_CV_SYNC`）。

这里补上 u5-l1 没展开的一环：当 `cross_interval > 1` 时，同步不是每拍都插，而是 **条件化** 的。条件就由 `stage_loop` 的循环变量 `stage_var` 表达：

- **写方（Cube）置位条件**：`stage_var % cross_interval == cross_interval - 1` 或最后一拍；
- **读方（Vector）等待条件**：`stage_var % cross_interval == 0`。

`cross_interval=1`（默认）时，条件恒真，退化为每拍同步。

#### 4.4.2 核心流程

```text
CombineCV::AutoInsertCrossCoreSync:
  CrossCoreSyncCollector 遍历 cube_code / vec_code:
    遇到 stage_loop 注解的 For  → 记下 current_stage_loop_
    从 stage_loop 取 tl_cross_interval 注解 → sp.cross_interval
  对每个 workspace 写/读点:
    若 cross_interval > 1:
      写方: 在数据搬运之后插【条件】set_cross_flag
      读方: 在数据搬运之前插【条件】wait_cross_flag
    否则: 无条件插 flag
```

#### 4.4.3 源码精读

**识别 stage 循环并取 cross_interval**：

[src/transform/ascend_combinecv.cc:126-151](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_combinecv.cc#L126-L151) —— 中文说明：`CrossCoreSyncCollector` 进入一个带 `stage_loop` 注解的 `For` 时记下它，`GetCrossInterval` 从该循环的 `tl_cross_interval` 注解读取同步稀疏度——这正是消费 `CrossCorePipeline` 产出的握手点。

**条件化同步公式**：

[src/transform/ascend_combinecv.cc:291-316](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_combinecv.cc#L291-L316) —— 中文说明：`GenSyncCondition` 用 `stage_var`（内层 stage 循环变量）表达稀疏同步——写方在 `stage_var % interval == interval-1` 或最后一拍置位、读方在 `stage_var % interval == 0` 等待。`cross_interval=1` 时两个条件都恒真，即每拍同步。

**插 flag 的方向与条件化**：

[src/transform/ascend_combinecv.cc:270-289](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_combinecv.cc#L270-L289) —— 中文说明：数据搬运语句 **始终执行**，只有 set/wait flag 被 `GenSyncCondition` 包进条件分支——写方「先搬、后条件置位」，读方「先条件等待、后搬」。

#### 4.4.4 代码实践

**实践目标**：对比 `cross_interval=1`（默认）与 `cross_interval=2` 时，生成代码里 cross flag 的数量与条件。

**操作步骤**：

1. 复制 `flash_attn_bshd_pipeline.py` 为 `flash_attn_bshd_pipeline_ci2.py`（示例代码，本地练习用）。
2. 把第 79 行改成 `for k in T.Pipelined(T.ceildiv(seq_len, block_N), num_stages=2, cross_interval=2)`。
3. 两份脚本各 `print(func.get_kernel_source()[0].source)`，搜索 `CrossCoreSetFlag`/`WaitFlag`（或 PTO 的 `set_cross`）。
4. 数一数 flag 的出现次数与外层的 `if`/条件。

**需要观察的现象**：默认版每拍一对 flag（无条件）；`cross_interval=2` 版 flag 被包进 `stage_var % 2 == ...` 条件，总数减半。

**预期结果**：`cross_interval` 翻倍，核间同步开销近似减半；但 workspace 在途数据增多。运行正确性「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么写方的置位条件里要额外加一个「或最后一拍」？

**参考答案**：因为 `cross_interval` 不整除总拍数时，最后几拍可能落不到 `stage_var % interval == interval-1` 这个条件上；若最后一拍不置位，读方会永远等不到最后一个 flag 而死锁。所以写方在「常规周期点」**或** 「最后一拍」都置位，保证读方一定能等到。读方只在周期起点等待，配合写方的周期置位即可推进。

---

### 4.5 两条硬约束

#### 4.5.1 概念说明

跨核流水有两条必须遵守的约束，编程指南把它们写在 4.1.4.2 节末尾：

> - 核间流水线与核内流水线不能同时开启。
> - 使用核间流水线时，必须开启自动 CV 分离和自动 CV 间同步插入功能。

第一条是 **机制约束**：一个 `T.Pipelined` 循环要么是核内（体只碰单一 scope，由 `PipelinePlanning`/`InjectSoftwarePipeline` 处理），要么是跨核（体跨两个 scope，由 `CrossCorePipeline` 处理）。二者不能在同一条流水线上并存——因为 `CrossCorePipeline` 会把跨核循环改写成两层 stage 循环，原 `num_stages` 注解已被消费，核内流水 pass 不会再处理它。同时 `ICHECK` 保证全函数只有一条跨核流水。

第二条是 **配套约束**：跨核流水离不开 CV 分离（把 Cube/Vector 拆进两个核）与核间同步（让两核协调）。这两个开关（`TL_ASCEND_AUTO_CV_COMBINE`、`TL_ASCEND_AUTO_CV_SYNC`）必须同时开——这也是 u5-l1 反复强调的「两开关必须同开」。

#### 4.5.2 核心流程

```text
约束一：核内 / 跨核 二选一
  单 scope 体  → PipelinePlanning / InjectSoftwarePipeline（核内三段式）
  跨 scope 体  → CrossCorePipeline（跨核两层 stage）   ← 二者互斥，ICHECK 全函数仅一条

约束二：跨核流水必开两开关
  TL_ASCEND_AUTO_CV_COMBINE = True   # 拆 CV
  TL_ASCEND_AUTO_CV_SYNC    = True   # 插核间 cross flag
```

#### 4.5.3 源码精读

**约束一的代码体现**：`CrossCorePipeline` 抢在核内流水 pass 之前消费掉跨核循环，且只认一条。

[tilelang/engine/phase.py:98-101](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L98-L101) —— 中文说明：`CrossCorePipeline` 在 `PipelinePlanning`/`InjectSoftwarePipeline` 之前执行，跨核循环被它改写后不再带原始 `num_stages` 形态，故核内流水 pass 不会重复处理。

[src/transform/cross_core_pipeline.cc:1283-1286](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/cross_core_pipeline.cc#L1283-L1286) —— 中文说明：`ICHECK` 保证全函数仅一条跨核流水。

**约束二的文档与代码体现**：

[docs/TileLang-Ascend Programming Guide.md:1174-1175](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md#L1174-L1175) —— 中文说明：编程指南明确两条注意事项——核间/核内流水不可同开；用核间流水必须同时开自动 CV 分离与自动 CV 同步。

`flash_attn_bshd_pipeline.py` 把这两个开关与 `tl.ascend_auto_sync` 一起全开，正是约束二的范例：

[examples/pipeline/flash_attn_bshd_pipeline.py:13-17](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/pipeline/flash_attn_bshd_pipeline.py#L13-L17) —— 中文说明：示例同时开 `ascend_auto_cv_combine`、`ascend_auto_cross_core_sync`、`ascend_auto_sync` 三个开关，满足跨核流水的全部配套要求。

#### 4.5.4 代码实践

**实践目标**：验证「关掉 CV 同步」会让跨核流水失去数据可见性。

**操作步骤**：

1. 复制 `flash_attn_bshd_pipeline.py` 为 `flash_attn_bshd_nosync.py`（示例代码，本地练习用）。
2. 把 `tl.ascend_auto_cross_core_sync` 改成 `False`，其余不变。
3. 运行，观察结果。

**需要观察的现象**：生成代码里不再有 `CrossCoreSetFlag/WaitFlag`；Cube 写完 workspace 与 Vector 读 workspace 之间失去协调。

**预期结果**：由于 Cube/Vector 间无数据可见性保证，结果大概率错误或偶发错误（取决于两核时序），反证核间同步的必要性。运行结果「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：能否在一个 kernel 里同时写一条跨核流水（`T.Pipelined` 跨 scope）和一条核内流水（另一个 `T.Pipelined` 单 scope）？

**参考答案**：不行，这违反「核间与核内流水不能同时开启」的约束。机制上，`CrossCorePipeline` 的 `ICHECK` 只约束「跨核流水至多一条」，但核内与跨核并存的语义在编程指南里被明确禁止——因为两者都依赖 `num_stages` 注解且改写策略冲突，并存会导致缓冲版本化与同步插入互相干扰。实际工程中应把跨核流水作为 kernel 的主循环，其余计算并入它的 Cube 或 Vector 段。

---

## 5. 综合实践

本讲的综合任务是 **读懂 `flash_attn_bshd_pipeline.py`，画出它的跨核流水时序表，并标注 `num_stages` 与三块 workspace 的数据流向**。这个示例是跨核流水的「教科书级」样本，因为它在 **同一条** `T.Pipelined` 里实现了 Cube↔Vector 的 **双向** 数据交换。

**操作步骤**：

1. 打开 [examples/pipeline/flash_attn_bshd_pipeline.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/pipeline/flash_attn_bshd_pipeline.py)，先看第 19 行的 `@tilelang.jit(..., workspace_idx=[4,5,6])`——三块 workspace 由运行时自动分配（`workspace_idx` 指定哪些参数是 workspace）。

2. 看第 44-46 行的三块 workspace 声明，记下它们的 shape 与用途：
   - `workspace_1`：`[block_num, block_M, block_N]`，accum_dtype——存 Cube 算出的 QK 分数（`acc_s_l0c`）。
   - `workspace_2`：`[block_num, block_M, block_N]`，float16——存 Vector 算完 softmax 后的分数（`acc_s_half`），**回传给 Cube** 做第二个 gemm。
   - `workspace_3`：`[block_num, block_M, dim]`，accum_dtype——存 Cube 算出的 PV 累加（`acc_o_l0c`）。

3. 进入第 79 行的 `T.Pipelined(num_stages=2)` 循环，按「谁写、谁读」把三块 workspace 的流向填进下表：

   | workspace | 写方（哪句、哪个核） | 读方（哪句、哪个核） | 方向 |
   |----------|----------------------|----------------------|------|
   | workspace_1 | 第 82 行 `T.copy(acc_s_l0c, workspace_1)`（Cube） | 第 86-88 行 `T.copy(workspace_1[cid,vid*...], acc_s_ub_)`（Vector） | Cube→Vector |
   | workspace_2 | 第 102-104 行 `T.copy(acc_s_half, workspace_2)`（Vector） | 第 106 行 `T.copy(workspace_2[cid,:,:], acc_s_l1)`（Cube） | Vector→Cube |
   | workspace_3 | 第 109 行 `T.copy(acc_o_l0c, workspace_3)`（Cube） | 第 113-115 行 `T.copy(workspace_3[cid,vid*...], acc_o_ub)`（Vector） | Cube→Vector |

4. 仿照 4.1 的时序表，假设 `T.ceildiv(seq_len, block_N) = 4`、`num_stages=2`，画出 `write_wk1`（Cube 写三块 workspace）与 `read_wk1`（Vector 读 workspace_1/3、写 workspace_2）的重叠时序，**标注 `num_stages=2`**。

5. 运行脚本，确认 `Test Passed!`，并在末尾追加 `print(func.get_kernel_source()[0].source)`，在生成代码里验证：
   - K 循环呈两层（外层波 + 内层 stage）结构；
   - 三块 workspace 各被加了 `num_stages=2` 维（环形缓冲）；
   - Cube 段与 Vector 段被 `if ASCEND_IS_AIC/AIV` 分到两核；
   - Cube 写 workspace 后、Vector 读 workspace 前各有一对 `CrossCoreSetFlag/WaitFlag`。

**预期结果**：时序表应呈现 t₀ Cube 独写、t₁~t₃ Cube 与 Vector 重叠、t₄ Vector 收尾的形态（与编程指南 4.1.4.2 一致）；三块 workspace 构成 Cube↔Vector 的双向环形交换。无 NPU 时至少完成 1-4 步的源码阅读与时序绘制，运行验证「待本地验证」。

## 6. 本讲小结

- **跨核流水解决的是 GM 往返延迟**：Cube↔Vector 经 GM workspace 中转数据，`num_stages` 让 Cube 领先几拍连续写 workspace、Vector 滞后消费，从而把 GM 往返重叠掉；`num_stages=2` 即双缓冲，时序为「t₀ 独写、t₁~t_{N} 重叠、末拍收尾」。
- **用户侧只需 `T.Pipelined(N, num_stages=k)`**，循环体里同时出现 Cube 与 Vector 操作即被认定为跨核；`cross_interval` 是同步稀疏度旋钮（默认 1 = 每拍同步）。
- **`CrossCorePipeline` pass**（[src/transform/cross_core_pipeline.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/cross_core_pipeline.cc)）做三件事：①`CrossCoreDetector` 检测循环体是否跨两个 scope；②把单层循环改写成「外层波 + 内层 stage」两层循环，原下标由 `outer*num_stages + i` 还原；③给 workspace 与跨 stage 复用 buffer 加 `num_stages` 维做成环形缓冲。
- **它不插同步**，只留下 `stage_loop` 与 `tl_cross_interval` 两个注解，由 u5-l1 的 `CombineCV`（开关 `TL_ASCEND_AUTO_CV_SYNC`）消费，落地条件化的 `set/wait_cross_flag`（写方 `stage_var % interval == interval-1` 或末拍、读方 `stage_var % interval == 0`）。
- **两条硬约束**：①核间流水与核内流水不能同时开启（`CrossCorePipeline` 抢在核内流水 pass 之前消费跨核循环，且 `ICHECK` 保证全函数仅一条跨核流水）；②跨核流水必须同时开 `TL_ASCEND_AUTO_CV_COMBINE` 与 `TL_ASCEND_AUTO_CV_SYNC`。
- **实战样本** `flash_attn_bshd_pipeline.py` 在一条 `T.Pipelined` 内用三块 workspace 实现 Cube↔Vector **双向** 交换（workspace_1/3 为 Cube→Vector、workspace_2 为 Vector→Cube），是理解跨核流水的最佳读物。

## 7. 下一步学习建议

- **u5-l3 Vid 消除与自动 CV 配比**：本讲示例里 Vector 段大量出现 `vid * block_M // 2` 的偏移（如第 87、104、114 行），下一讲解释 `threads=2` 如何让这些手动切分由 `AscendVidReduction` pass 自动完成，并联动 CV 配比。
- **u5-l4 Workspace 消除**：本讲的三块 workspace 是用户在 kernel 参数里 **显式** 声明并由 `workspace_idx` 自动分配的；下一讲讲解 `AscendWorkspaceReduction` 如何把 `copy_l0c_to_ub` 这类隐式跨核拷贝自动翻译成两阶段 GM 搬运并自动分配 workspace，进一步减少手写。
- **u7-l1 FlashAttention 实现案例**：本讲的 `flash_attn_bshd_pipeline.py` 是 FA 的「跨核流水版」，第七单元会从 online softmax 算法层面完整拆解 FA，把本讲的 Cube(QK)/Vector(softmax+PV) 数据流放到算法上下文里。
- 建议同步精读：[docs/TileLang-Ascend Programming Guide.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md) 4.1.4.2 节（T.Pipelined 的 intra/inter-core 两 case 与时序表），与本讲的时序推导互相印证。
