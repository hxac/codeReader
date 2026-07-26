# 软件流水线 Pipelined

## 1. 本讲目标

本讲聚焦 tilelang 的软件流水线（software pipeline）机制。读完本讲，你应当能够：

- 说清楚 **为什么** GEMM 类 kernel 需要 software pipeline，以及 `num_stages` 的物理含义。
- 用 `T.Pipelined(num_stages=N)` 让编译器**自动推断** producer/consumer 流水线。
- 用 `stage=[...]` / `order=[...]` **手动标注**流水线，并解释二者的对齐规则。
- 理解 `T.Pipelined` 在 IR 层如何被两个 C++ Pass（`PipelinePlanning` 与 `InjectSoftwarePipeline`）改写为 prologue / body / epilogue 三段循环。
- 掌握「可重放标量 `Bind` 不进入调度」这条规则，能诊断流水线推断失败并改用手动注解。

## 2. 前置知识

在进入源码前，先用通俗语言建立两个直觉。

**直觉一：访存与计算重叠。** 一个分块 GEMM 的主循环每次迭代做三件事：把 A、B 两个 tile 从显存搬进 shared memory（`T.copy`），再用张量核做矩阵乘（`T.gemm`）。如果严格串行，第 `k` 次的 `T.copy` 必须等第 `k-1` 次的 `T.gemm` 算完才开始——显存带宽在第 `k-1` 次计算时完全空闲。软件流水线的目标是：**在算第 `k` 块的同时，提前把第 `k+1`、`k+2` 块的数据搬进来**，让「搬运」与「计算」重叠，从而隐藏访存延迟。

**直觉二：多版本缓冲。** 既然要「边算当前块、边搬下一块」，shared memory 里就不能只留一份 A_shared——当前块还在读它，下一块就要覆盖它了。于是需要给 shared 缓冲开多份（双缓冲 / 三缓冲），每一「级」（stage）对应一份。`num_stages` 就是这个流水线的深度：`num_stages=3` 意味着最多同时有 3 个迭代的数据处于「搬运中 / 待算 / 计算中」的不同阶段，因此需要相应的多版本缓冲。

> 与 u3-l1 的衔接：`T.gemm` 是「计算」、`T.copy` 是「搬运」。本讲研究的就是如何让这二者在主循环里重叠。`T.Pipelined` 是 tile 级循环原语（与 `T.serial`/`T.Parallel` 同族，见 u2-l3）的一种特殊形式。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tilelang/language/loop.py` | DSL 入口 `T.Pipelined(...)` 的 Python 定义，把参数透传给 C++ builder |
| `docs/programming_guides/software_pipeline.md` | 官方用户指南，讲清 stage/order 与可重放 `Bind` 的用户模型 |
| `src/transform/pipeline_planning.cc` | Pass `tl.PipelinePlanning`：把 `num_stages` 或手动 `stage/order` 翻译成标准 IR 注解 |
| `src/transform/inject_pipeline.cc` | Pass `tl.InjectSoftwarePipeline`：把带注解的循环重写成 prologue/body/epilogue，并做多版本缓冲 |
| `tilelang/cuda/pipeline.py` | CUDA 后端 Pass 流水线，固定先 `PipelinePlanning` 再 `InjectSoftwarePipeline` |
| `testing/python/transform/test_tilelang_transform_pipeline_planning.py` | 流水线规划的真实测试，提供可复现的 before/after IR 样例 |

> 说明：讲义大纲把本讲的最小模块记为 `tilelang.language.tile_schedule` 与 `tilelang.transform(inject_pipeline)`。实际 `T.Pipelined` 的定义在 `loop.py`（由 `common.py` 统一导出），而 `tile_schedule.py` 里放的是与之配套的 `PersistentTileScheduler`——它常与 `T.Pipelined` 一起出现在 persistent kernel 中（用 `current_iter` 当流水线相位时钟）。本讲聚焦 `T.Pipelined` 本身。

## 4. 核心概念与源码讲解

### 4.1 流水线的直觉与 num_stages

#### 4.1.1 概念说明

软件流水线把一个串行循环改写成「提前发射（prologue）— 稳态（body）— 收尾（epilogue）」三段。用一个最小的「1 次拷贝 + 1 次计算」、`num_stages=2` 的例子说明：

```text
原始串行（每轮：copy 再 compute）
  k=0: copy(0); compute(0)
  k=1: copy(1); compute(1)
  k=2: copy(2); compute(2)
```

双缓冲流水线改写后，稳态里 `copy(k+1)` 与 `compute(k)` 同时进行：

```text
prologue : copy(0)                      # 灌满第 0 级
body     : copy(1); compute(0)          # k=0 算, k=1 搬
         : copy(2); compute(1)          # k=1 算, k=2 搬
epilogue :              compute(2)      # 把最后一级算完
```

`num_stages=N` 表示流水线深度，等价于「同一时刻在飞（in-flight）的最大迭代数」。它直接决定 shared 缓冲需要开几份。

#### 4.1.2 核心流程

稳态里，第 `k` 次迭代的「计算」语句实际读到的逻辑迭代号是 `k`，而同一物理循环步里和它并行的「搬运」语句服务的是逻辑迭代号 `k+1`（下一块）。 tilelang 用一个偏移来表达这件事：一个属于 `stage` 级的语句，在物理循环变量为 `i` 时，处理的逻辑迭代为

\[ \text{logical\_iter} = i - \text{stage} \]

于是 `stage=0` 的搬运语句在 `i` 步服务逻辑迭代 `i`（看起来「提前」），`stage=N` 的计算语句在 `i` 步服务逻辑迭代 `i-N`（看起来「滞后」）。prologue 负责让 `i` 从 `min` 走到 `min+max_stage`（把各级灌满），epilogue 负责把剩余的尾迭代算完。

#### 4.1.3 源码精读

这个「偏移」直接体现在改写器里：物理循环变量 `new_loop_var` 减去 `stage` 得到该语句的逻辑迭代号，再用 `inbound` 守卫判断该逻辑迭代是否落在原始区间 `[min, min+extent)` 内（prologue/epilogue 边界）。见 [src/transform/inject_pipeline.cc:2889-2906](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/inject_pipeline.cc#L2889-L2906)，其中 `skewed_loop_var = new_loop_var - stage` 即上述公式的代码化身，`inbound` 守卫对应 prologue/epilogue 的边界裁剪。

而 prologue / body / epilogue 三段的区间切分在 `PipelineRewriter::BuildPipeline()` 里完成，见 [src/transform/inject_pipeline.cc:1223-1230](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/inject_pipeline.cc#L1223-L1230)：

- `prologue`：`[min, min + max_stage)`，逐迭代展开（unroll），需要边界裁剪。
- `body`：`[min + max_stage, min + extent)`，稳态主循环，无需裁剪。
- `epilogue`：`[min + extent, min + extent + max_stage)`，逐迭代展开，裁剪。

#### 4.1.4 代码实践（源码阅读型）

1. 实践目标：在脑中（或纸面）画出 `num_stages=3`、单拷贝单计算的 prologue/body/epilogue 迭代表。
2. 操作步骤：写出 `max_stage` 的值（=2，因为 copy 在 stage 0、compute 在 stage 2），然后列出 prologue 要展开几次、body 循环区间、epilogue 要展开几次。
3. 需要观察的现象：prologue 与 epilogue 的展开次数之和应等于 `max_stage`，body 的迭代数应等于原始循环次数减去 `max_stage`。
4. 预期结果：prologue 展开 2 次（灌满 stage 0 和 stage 2 之间的级），epilogue 展开 2 次（把最后 2 个逻辑迭代的 compute 补完）。
5. 若想确认，可对照 4.3.3 的真实测试用例 `num_stages=3` 推断结果。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `num_stages` 设成 1，流水线还会生效吗？
**答**：不会。`num_stages=1` 意味着只有一级，没有可重叠的 producer/consumer，等价于串行循环。`T.Pipelined` 的 docstring 也说明 `num_stages=0` 时流水线不启用。

**练习 2**：稳态里「搬运第 k+2 块」需要 `num_stages` 至少为多少？
**答**：至少 3。深度 N 允许同一时刻在飞 N 个迭代，要看到「当前算 k、提前搬 k+2」需要 3 级。

---

### 4.2 T.Pipelined DSL 入口（tilelang.language）

#### 4.2.1 概念说明

`T.Pipelined` 是用户写流水线的唯一 DSL 入口，它在 Python 侧只是一个**薄封装**：收集参数后透传给 C++ 的 `_ffi_api.Pipelined`，生成一个带注解的串行 `For` 循环（`ForFrame`）。真正的「规划」与「重写」全部发生在后续两个 C++ Pass 里。理解这一点很重要：**DSL 层只负责「声明意图」，不负责「执行流水线」**。

它有两种用法：

- **自动推断**：只给 `num_stages=N`，编译器自己分析循环体，把 copy 归到低 stage、compute 归到高 stage。
- **手动标注**：给 `stage=[...]` 和 `order=[...]`，逐条语句指定它属于第几级、发射顺序如何。

#### 4.2.2 核心流程

`T.Pipelined` 的参数与其 IR 落点：

```text
T.Pipelined(extent, num_stages=3)
   └─ 生成 For 循环，带注解 "num_stages" = 3
       └─ PipelinePlanning 读取 num_stages，推断出
          software_pipeline_stage / software_pipeline_order

T.Pipelined(extent, stage=[0,0,1], order=[0,1,2])
   └─ 生成 For 循环，带注解 "tl_pipeline_stage" / "tl_pipeline_order"
       └─ PipelinePlanning 直接把它们规整为
          software_pipeline_stage / software_pipeline_order
```

关键规则（来自用户指南 [docs/programming_guides/software_pipeline.md:83-86](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/software_pipeline.md#L83-L86)）：**手动标注时不要同时写 `num_stages`**。流水线深度由 stage 列表推断为 `max(stage) + 1`。`num_stages` 单独用于「自动推断」，`stage`/`order` 单独用于「手动调度」。

`stage` 与 `order` 两个数组按**源码顺序**与「可调度语句」对齐（见 [docs/programming_guides/software_pipeline.md:65-72](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/software_pipeline.md#L65-L72)）。依赖检查规则：同一 stage 内 producer 的 `order` 必须小于 consumer；跨 stage 时 producer 的 `stage` 必须 ≤ consumer 的 `stage`。

#### 4.2.3 源码精读

`Pipelined` 的 Python 定义在 [tilelang/language/loop.py:112-191](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/loop.py#L112-L191)。几个要点：

- 签名 `Pipelined(start, stop=None, num_stages=0, order=None, stage=None, sync=None, group=None)`（L112-120）。`num_stages` 默认 0，即不启用流水线。
- 单参数写法 `T.Pipelined(32, num_stages=3)` 会被归一化为 `start=0, stop=32`（L179-181）。
- 末尾一句 `_ffi_api.Pipelined(start, stop, num_stages, order, stage, sync, group)`（L191）把全部参数透传给 C++ builder，生成带 `num_stages` 或 `tl_pipeline_stage/order` 注解的 `For` 节点。
- docstring 里（L148-173）专门强调了「可重放标量 `Bind` 不消耗 `order`/`stage` 条目」这条规则，我们会在 4.5 详细讲。

该名字经 `tilelang/language/common.py` 统一导出，所以 `import tilelang.language as T` 后可直接用 `T.Pipelined`（见 [tilelang/language/common.py:179](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/common.py#L179)）。

#### 4.2.4 代码实践（源码阅读型）

1. 实践目标：确认「自动推断」与「手动标注」两种写法生成的初始 `For` 注解不同。
2. 操作步骤：阅读 [tilelang/language/loop.py:179-191](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/loop.py#L179-L191)；再对照 4.3.3 的测试用例，看 `num_stages=3` 在 `PipelinePlanning` 之后变成了哪些 `software_pipeline_*` 注解。
3. 需要观察的现象：DSL 层只产生 `num_stages`（自动）或 `tl_pipeline_stage/order`（手动）注解；`software_pipeline_*` 是 Pass 产物，DSL 层看不到。
4. 预期结果：理解「DSL 声明意图 → Pass 兑现调度」的两段式设计。
5. 待本地验证：可在 `PipelinePlanning` 前后各 `print(mod)` 对比注解变化（见综合实践）。

#### 4.2.5 小练习与答案

**练习 1**：写 `T.Pipelined(K, num_stages=3, stage=[0,0,1], order=[0,1,2])` 会怎样？
**答**：不建议。手动 `stage/order` 时深度由 `max(stage)+1=2` 推断，与显式 `num_stages=3` 冲突。docstring 明确说除非有意覆盖深度，否则不要混用。

**练习 2**：`stage=[0,1], order=[1,0]` 表示什么？
**答**：两条可调度语句中，第一条（源码序）是 stage 0、发射序 1；第二条是 stage 1、发射序 0。即「先发射属于下一级的 compute，再发射属于当前级的 copy」——这正是「重排流水线」的写法（见 [docs/programming_guides/software_pipeline.md:88-114](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/software_pipeline.md#L88-L114)）。

---

### 4.3 PipelinePlanning：自动推断 stage/order（tilelang.transform）

#### 4.3.1 概念说明

`tl.PipelinePlanning` 是流水线的「大脑」。它接收带 `num_stages` 注解的循环，分析循环体里每条语句读了/写了哪些缓冲，自动判定谁是 producer（搬运）、谁是 consumer（计算），并给每条可调度语句分配 `stage` 与 `order`，写成标准 IR 注解 `software_pipeline_stage` / `software_pipeline_order`。它**不改循环结构**，只贴注解；真正的循环重写交给下一个 Pass。

#### 4.3.2 核心流程

`PipelinePlanner::VisitStmt_` 遇到 `For` 节点时分三条路（见 [src/transform/pipeline_planning.cc:1093-1153](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/pipeline_planning.cc#L1093-L1153) 与 [L1156-L1181](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/pipeline_planning.cc#L1156-L1181)）：

1. **手动注解分支**：循环带 `tl_pipeline_order`/`tl_pipeline_stage` → 把用户数组过滤到「仅可调度语句」（剔除可重放 `Bind`），写成 `software_pipeline_*`。
2. **ROCm 回退分支**：若目标是 ROCm 且非 gfx950，直接剥掉 `num_stages` 注解，回退为普通串行循环（HIP async-copy 流水线目前只在 gfx950 上经过验证）。
3. **自动推断分支**：循环带 `num_stages` → 进入下面的规划算法。

自动推断算法（[src/transform/pipeline_planning.cc:1182-1347](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/pipeline_planning.cc#L1182-L1347)）大致五步：

```text
(a) AnalyzeScheduledStmts：把循环体拍平，挑出「可调度语句」，
    识别哪些是可重放标量 Bind（不计入 stage/order）。
(b) 为每条语句建 PipelineStageInfo，判定 copy_stage
    （global→shared 的纯搬运）与 tma_copy（Hopper 上用 TMA）。
(c) PropagateBufferProducersForCopy / AnalyzeCopyLastUse /
    PropagateScalarProducersForCopy：算出每个 copy 的
    「最后消费者」位置 last_use_stmt_index。
(d) 分配 stage/order：
      - 主逻辑语句 → stage = num_stages，order 递增；
      - copy 语句  → stage = 0，order 紧贴在它最后消费者之前。
(e) 「copy 在尾部」优化：若所有 copy 的 order 都在非 copy 之后，
    把 copy 整体挪到开头并把非 copy 的 stage 各减 1。
```

#### 4.3.3 源码精读

**真实样例**（来自测试 [testing/python/transform/test_tilelang_transform_pipeline_planning.py:44-111](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/transform/test_tilelang_transform_pipeline_planning.py#L44-L111)）。输入是一个 `num_stages=3` 的分块 GEMM：

```python
for ko in T.Pipelined(32, num_stages=3):
    T.copy(A[by * 128, ko * 32], A_shared)   # global -> shared
    T.copy(B[ko * 32, bx * 128], B_shared)   # global -> shared
    T.gemm(A_shared, B_shared, C_local)      # compute
```

经过 `PipelinePlanning` 后，循环被贴上注解（关键部分）：

```python
"software_pipeline_order": [0, 1, 2],
"software_pipeline_stage": [0, 0, 2],
"tl_pipelined_num_stages": 3,
"software_pipeline_async_producers": [1, 1, 0],   # 两个 copy 走 async
"software_pipeline_async_stages": [0],
```

即 `copy A → stage0/order0`，`copy B → stage0/order1`，`gemm → stage2/order2`。注意 gemm 的 stage 是 **2** 而不是 1，这正是「copy 在尾部」优化的结果：先把两个 copy 放在 gemm 之后（order 1、2，gemm order 0，stage=num_stages=3），发现所有 copy 都在尾部，于是整体把 copy 挪到开头并对非 copy 的 stage 减 1，得到 gemm stage=2。这段逻辑在 [src/transform/pipeline_planning.cc:1264-1287](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/pipeline_planning.cc#L1264-L1287)。

stage/order 的赋值主逻辑在 [src/transform/pipeline_planning.cc:1226-1254](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/pipeline_planning.cc#L1226-L1254)：主逻辑语句 `stage = num_stages`，copy 语句 `stage = 0` 且 `order` 紧排在它的最后消费者之前。最终注解的生成在 [src/transform/pipeline_planning.cc:1305-1322](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/pipeline_planning.cc#L1305-L1322)（写入 `software_pipeline_stage/order` 与 `tl_pipelined_num_stages`）。

Pass 注册在 [src/transform/pipeline_planning.cc:1366-1381](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/pipeline_planning.cc#L1366-L1381)，名为 `tl.PipelinePlanning`，Python 侧封装为 `tl.transform.PipelinePlanning()`（见 [tilelang/transform/__init__.py:30-38](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/__init__.py#L30-L38)）。

#### 4.3.4 代码实践（源码阅读型）

1. 实践目标：复现上面的 GEMM 推断结果，验证「copy 在尾部」优化。
2. 操作步骤：阅读 [src/transform/pipeline_planning.cc:1226-1287](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/pipeline_planning.cc#L1226-L1287)，手算三条语句的 `(stage, order)`：先按主逻辑（gemm order0/stage3，两个 copy order1,2/stage0），再应用「copy 在尾部」优化（order 加 2 模 3，非 copy stage 减 1）。
3. 需要观察的现象：手算结果应与测试里的 `[0,0,2]`/`[0,1,2]` 完全一致。
4. 预期结果：copy A=(stage0,order0)，copy B=(stage0,order1)，gemm=(stage2,order2)。
5. 待本地验证：可用 4.5 给出的 snippet 直接跑 `PipelinePlanning` 并打印注解对照。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `copy A` 与 `copy B` 都被分到 `async_producers`，而 `gemm` 不是？
**答**：`copy A/B` 是 global→shared 的纯搬运，在 SM80+ 上可走 cp.async 异步拷贝，是 async producer 候选；`gemm` 是计算语句，不是搬运，故 `async_producers=[1,1,0]`。判定逻辑见 `IsAsyncProducerCandidate`（[src/transform/pipeline_planning.cc:500-508](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/pipeline_planning.cc#L500-L508)）。

**练习 2**：在 ROCm（gfx942 / MI300X）上写 `T.Pipelined(num_stages=3)` 会发生什么？
**答**：回退为普通串行循环。源码在 [src/transform/pipeline_planning.cc:1165-1181](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/pipeline_planning.cc#L1165-L1181)：非 gfx950 的 ROCm 目标会剥掉 `num_stages` 注解。目前只有 gfx950（CDNA4 / MI350）验证了完整 HIP async-copy 流水线。

---

### 4.4 InjectSoftwarePipeline：循环重写与多版本缓冲（tilelang.transform）

#### 4.4.1 概念说明

`tl.InjectSoftwarePipeline` 是流水线的「手脚」。它消费 `PipelinePlanning` 贴好的 `software_pipeline_stage/order` 注解，把单个串行循环物理上重写成三段（prologue/body/epilogue），并完成两件关键的善后：**(a) 多版本缓冲**——给被流水化的 shared 缓冲加一个「版本维」，用取模选址；**(b) 同步与 barrier 展开**——把 TMA/cp.async 的 mbarrier 也按级展开。它还负责把「可重放标量 `Bind`」在每个消费者处按需重放。

#### 4.4.2 核心流程

`InjectSoftwarePipeline` 的总入口在 [src/transform/inject_pipeline.cc:4030-4039](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/inject_pipeline.cc#L4030-L4039)，调用 `InjectPipeline(f)` 后再做一次 `ConvertSSA`。对每个带流水线注解的 `For`，它依次：

```text
Step 1  解析 stage/order 注解 → PipelineInfo (block -> {stage, order, async,...})
Step 2  RewritePipelineTmaBarriers：TMA copy 改用流水线级 mbarrier
Step 3  ExpandPipelineBarriers：把所有相关 barrier 缓冲从 [N] 扩成 [N*num_stages]
Step 4  PipelineRewriter::BuildPipeline：
          (a) GetBufferAccessInfo + ComputeBufferVersions：算每个缓冲需要几份
          (b) RewriteAllocBuffer：给缓冲加版本维
          (c) 按 order 排序语句，EmitImpl 出 prologue/body/epilogue
          (d) PipelineBodyRewriter：每次访问插 floormod 选版本
          (e) ReplayScalarBindings：在每个消费者前重放标量 Bind
```

**多版本缓冲的数学**：对一个在 `def` 级被写、在 `use` 级被最后读的缓冲，所需版本数上界为

\[ V = \text{use} - \text{def} + 1 \]

代码里还会做一个优化：当 `V=2` 时，只有存在「reader 的 order < writer 的 order 且 reader 的 stage < writer 的 stage 且区域相交」才真的需要双缓冲，否则减一（见 [src/transform/inject_pipeline.cc:1472-1527](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/inject_pipeline.cc#L1472-L1527)）。确定版本数后，`RewriteAllocBuffer` 在 shape 最前面加一维 `V`（[src/transform/inject_pipeline.cc:1535-1544](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/inject_pipeline.cc#L1535-L1544)），每次访问按下式选版本：

\[ \text{version}(i) = (i - i_{\min}) \bmod V \]

#### 4.4.3 源码精读

**多版本选址**由 `PipelineBodyRewriter` 完成。以 `BufferStore` 为例，它在索引最前面插入 `floormod((loop_var - min), shape[0])` 来选版本，见 [src/transform/inject_pipeline.cc:1099-1112](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/inject_pipeline.cc#L1099-L1112)（`BufferLoad` 同理，L1114-1127）。这就是上式的代码化身——在生成的 CUDA 里你会看到 shared 缓冲多了一维、访问时带 `% num_stages`。

**三段发射**由 `PipelineRewriter::BuildPipeline` 编排，见 [src/transform/inject_pipeline.cc:1192-1268](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/inject_pipeline.cc#L1192-L1268)：先 `GetBufferAccessInfo` + `ComputeBufferVersions` 决定版本数，再按 `order` 排序语句，最后 `EmitImpl` 出三段（L1223-1230）。`EmitImpl` 内部对 prologue/epilogue 会按常量 extent 展开（[src/transform/inject_pipeline.cc:2838-2856](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/inject_pipeline.cc#L2838-L2856)），对每条语句算 `skewed_loop_var = new_loop_var - stage` 并做 `inbound` 边界裁剪。

**Barrier 展开**：`ExpandPipelineBarriers` 把 `shared.barrier` 缓冲从 `[N]` 扩成 `[N*num_stages]`，并把 `mbarrier_wait_parity` 的 parity 参数改写为 `(⌊(i-min)/num_stages⌋ mod 2)`，保证每级等待正确的相位，见 [src/transform/inject_pipeline.cc:769-884](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/inject_pipeline.cc#L769-L884)。TMA 路径则在 `RewritePipelineTmaBarriers`（[src/transform/inject_pipeline.cc:906-994](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/inject_pipeline.cc#L906-L994)）里把 `tl.tileop.copy` 改写为带 barrier 的 `tl.tileop.tma_copy`。

Pass 注册名为 `tl.InjectSoftwarePipeline`，见 [src/transform/inject_pipeline.cc:4041-4045](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/inject_pipeline.cc#L4041-L4045)；Python 封装 `tl.transform.InjectSoftwarePipeline()`，见 [tilelang/transform/__init__.py:63-71](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/__init__.py#L63-L71)。

两个 Pass 在每个后端的顺序都是**先规划后重写**，以 CUDA 为例见 [tilelang/cuda/pipeline.py:108-110](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/pipeline.py#L108-L110)（`PipelinePlanning` → `InjectSoftwarePipeline` → `Simplify`），metal/rocm/webgpu 后端完全一致。

#### 4.4.4 代码实践（源码阅读型）

1. 实践目标：在生成的 kernel 源码里定位「多版本缓冲」与「prologue 展开」的痕迹。
2. 操作步骤：编译 4.3.3 的 GEMM，调用 `kernel.get_kernel_source()`；在 CUDA 源码里查找 shared 缓冲声明是否多了一维、主循环是否变成「先若干次预取 + 一个稳态 for + 若干次收尾」。
3. 需要观察的现象：`A_shared`/`B_shared` 的尺寸放大（多了版本维）；prologue 里能看到提前的 `cp.async`/`TMA` 拷贝；稳态 for 内有 `wait_group` 之类的同步。
4. 预期结果：能指认出 prologue、body、epilogue 三段的边界，以及版本选址的取模运算。
5. 待本地验证：依赖本机 GPU 与编译后端。

#### 4.4.5 小练习与答案

**练习 1**：一个缓冲在 stage 0 写、stage 2 读，版本数上界是多少？是否一定需要这么多？
**答**：上界 `V = 2 - 0 + 1 = 3`。不一定需要这么多——仅当 `V>=2` 且存在 reader/writer 的 order 与 stage 同时满足「reader 更早」且访问区域相交时才必须多版本，否则会减一（[src/transform/inject_pipeline.cc:1483-1525](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/inject_pipeline.cc#L1483-L1525)）。

**练习 2**：为什么 `InjectSoftwarePipeline` 之后还要跑一次 `ConvertSSA`？
**答**：因为重写过程会复制语句、重放标量 `Bind`、重命名缓冲，可能产生重复的变量定义；`ConvertSSA` 把 IR 规整回 SSA 形式，保证后续 Pass 看到干净的 IR（见 [src/transform/inject_pipeline.cc:4034-4035](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/inject_pipeline.cc#L4034-L4035)）。

---

### 4.5 可重放标量 Bind 规则与诊断

#### 4.5.1 概念说明

这是本讲最容易踩坑的规则。循环体里有两类语句：

- **可调度语句（scheduled statement）**：有副作用、会读写缓冲、有明确执行点——如 `T.copy`、`T.fill`、`T.gemm`、`T.reduce_*`、store/atomic。它们**才**消耗 `stage`/`order` 条目。
- **可重放标量 Bind（replayable scalar Bind）**：形如 `base: T.int32 = ko * BK` 的纯标量别名。它没有副作用、不拥有缓冲、可能被多个不同 stage 的语句引用，且其正确值依赖**消费者所在的逻辑迭代**。

tilelang 的规则是：**可重放标量 Bind 不进入调度**，编译器在每个消费者之前按需重放它（[docs/programming_guides/software_pipeline.md:116-180](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/software_pipeline.md#L116-L180)）。

#### 4.5.2 核心流程

判定的关键在 `IsReplayableScalarBind`：如果一个 `Bind` 读取了**流水线体内会写的缓冲**，它就**不是**可重放的——因为那个 load 依赖流水线 producer，不能自由重放，必须留在可调度列表里、占用 `stage`/`order` 条目，且必须与所有消费者同 stage（[docs/programming_guides/software_pipeline.md:194-208](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/software_pipeline.md#L194-L208)）。

```text
base = ko * BK              # 只读循环变量 → 可重放，不占条目
idx  = Ids[ko]              # Ids 不在体内写 → 可重放，不占条目
val  = A_shared[tx]         # A_shared 在体内被 copy 写 → 不可重放，占条目！
```

重放时按依赖顺序展开：若 `offset = base + tx` 依赖 `base`，则在消费者前先重放 `base` 再重放 `offset`（[docs/programming_guides/software_pipeline.md:210-229](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/software_pipeline.md#L210-L229)）。

#### 4.5.3 源码精读

C++ 侧的判定与重放都在 `inject_pipeline.cc`。`IsReplayableScalarBindBlock` 配合 `CollectPipelineWriteBuffers` 判定一个 block 是否是可重放 Bind（[src/transform/inject_pipeline.cc:389-403](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/inject_pipeline.cc#L389-L403)）。重放逻辑在 `PipelineRewriter::ReplayScalarBindings`：它先用 `RequiredScalarBindings` 做依赖排序（带环检测，[src/transform/inject_pipeline.cc:1317-1366](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/inject_pipeline.cc#L1317-L1366)），再在消费者前用消费者的逻辑迭代号替换并重放（[src/transform/inject_pipeline.cc:1378-1396](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/inject_pipeline.cc#L1378-L1396)）。

在 `pipeline_planning.cc` 一侧，`AnalyzeScheduledStmts` 同样把可重放 Bind 从可调度列表里剔除（产生 `replayable_bind_mask`），并把用户传入的过多数组条目过滤掉（[src/transform/pipeline_planning.cc:318-368](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/pipeline_planning.cc#L318-L368)）。这就是「legacy 写法（给 Bind 也留了条目）仍被接受、但该条目被忽略」的实现原因（[docs/programming_guides/software_pipeline.md:231-259](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/software_pipeline.md#L231-L259)）。

跨 stage 的标量依赖会被 `ValidateScalarDependencies` 拦下报错（[src/transform/pipeline_planning.cc:837-865](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/pipeline_planning.cc#L837-L865)），这是诊断「手动注解不一致」的主要报错点。

#### 4.5.4 代码实践（可运行）

1. 实践目标：亲手触发一次「手动注解条目数不匹配」的诊断，理解可重放 Bind 的计数规则。
2. 操作步骤：把下面的 snippet 保存为 `pipeline_probe.py` 并运行（仅做 Pass 级实验，不需要 GPU）。

   ```python
   # 示例代码：仅演示 Pass 行为，不依赖 GPU
   import tilelang as tl
   import tilelang.language as T
   from tilelang.backend.target import determine_target
   import tvm

   target = tvm.target.Target({"kind": "cuda", "arch": "sm_80"})

   @T.prim_func
   def probe(A: T.Tensor((64,), T.float16), C: T.Tensor((64,), T.float16)):
       with T.Kernel(1, threads=128):
           S = T.alloc_shared((16,), T.float16)
           # base 是可重放标量 Bind：不计入 stage/order
           for i in T.Pipelined(4, num_stages=2):
               base: T.int32 = i * 16
               T.copy(A[base], S)
               T.copy(S, C[base])

   mod = tvm.IRModule.from_expr(probe.with_attr("global_symbol", "main"))
   mod = tvm.tirx.transform.BindTarget(target)(mod)
   mod = tl.transform.PipelinePlanning()(mod)
   print(mod["main"].script())
   ```

3. 需要观察的现象：打印出的 IR 里，循环只有**两条**可调度语句（两个 `T.copy`）的 `software_pipeline_stage/order`，`base` 不占条目；`base` 会被重放在消费者之前。
4. 预期结果：`software_pipeline_order=[0,1]`、`software_pipeline_stage=[0,1]`（两个 copy 各一条），长度为 2 而非 3。
5. 若把 `base` 改成读取体内会写的缓冲（例如 `v: T.float16 = S[0]`），它会变成可调度语句，条目数随之变化——可用此对照理解规则。

#### 4.5.5 小练习与答案

**练习 1**：下面这段手动注解对吗？`stage=[0,0,1], order=[0,1,2]`，循环体是 `base=i*BK; copy(A[base]); copy(S,C[base])`。
**答**：条目数错了。`base` 是可重放标量 Bind，不占条目；只有两个 `copy` 是可调度语句，所以 `stage`/`order` 应各只有 **2** 个条目，而不是 3 个。legacy 写法（给 3 个）会被接受但 `base` 条目被忽略。

**练习 2**：什么时候一个 `Bind` **必须**留在可调度列表里？
**答**：当它读取了流水线体内会被写的缓冲时（如 `v = A_shared[tx]` 而 `A_shared` 被 `T.copy` 写）。因为该 load 依赖 producer，不能跨迭代自由重放，必须占 `stage`/`order` 条目，并与所有消费者同 stage（[docs/programming_guides/software_pipeline.md:194-208](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/software_pipeline.md#L194-L208)）。

---

## 5. 综合实践

把 u3-l1 的分块 GEMM 用 `T.Pipelined` 武装起来，对比「自动推断」与「手动注解」两种写法生成的 kernel 源码与延迟。参考 `examples/gemm/example_gemm.py` 的结构，写一个最小可运行版本（示例代码，需本地 GPU 验证）：

```python
# 示例代码：在 CUDA 机器上运行
import tilelang
import tilelang.language as T
import torch

BM, BN, BK = 128, 128, 32
M = N = K = 1024

# 写法 A：自动推断
@tilelang.jit
def gemm_auto():
    @T.prim_func
    def main(A: T.Tensor((M, K), T.float16),
             B: T.Tensor((K, N), T.float16),
             C: T.Tensor((M, N), T.float16)):
        with T.Kernel(T.ceildiv(N, BN), T.ceildiv(M, BM), threads=128) as (bx, by):
            A_sh = T.alloc_shared((BM, BK), T.float16)
            B_sh = T.alloc_shared((BK, BN), T.float16)
            C_fr = T.alloc_fragment((BM, BN), T.float32)
            T.clear(C_fr)
            for ko in T.Pipelined(T.ceildiv(K, BK), num_stages=3):
                T.copy(A[by * BM, ko * BK], A_sh)
                T.copy(B[ko * BK, bx * BN], B_sh)
                T.gemm(A_sh, B_sh, C_fr)
            T.copy(C_fr, C[by * BM, bx * BN])
    return main

# 写法 B：手动注解（两个 copy 在 stage 0，gemm 在 stage 1，深度=2）
@tilelang.jit
def gemm_manual():
    @T.prim_func
    def main(A: T.Tensor((M, K), T.float16),
             B: T.Tensor((K, N), T.float16),
             C: T.Tensor((M, N), T.float32)):
        with T.Kernel(T.ceildiv(N, BN), T.ceildiv(M, BM), threads=128) as (bx, by):
            A_sh = T.alloc_shared((BM, BK), T.float16)
            B_sh = T.alloc_shared((BK, BN), T.float16)
            C_fr = T.alloc_fragment((BM, BN), T.float32)
            T.clear(C_fr)
            # stage/order 各 2... 不对：这里有 3 条可调度语句
            for ko in T.Pipelined(
                T.ceildiv(K, BK),
                stage=[0, 0, 1],
                order=[0, 1, 2],
            ):
                T.copy(A[by * BM, ko * BK], A_sh)
                T.copy(B[ko * BK, bx * BN], B_sh)
                T.gemm(A_sh, B_sh, C_fr)
            T.copy(C_fr, C[by * BM, bx * BN])
    return main
```

任务步骤：

1. 分别编译 `gemm_auto` 与 `gemm_manual`，用 `kernel.get_kernel_source()` 打印两份设备源码。
2. 在源码里定位：shared 缓冲的版本维（多出来的那一维）、prologue 的预取次数、稳态 for 内的 `wait_group`/`cp.async`/TMA、epilogue 的收尾。
3. 用 `kernel.get_profiler().do_bench()`（见 u1-l4）测两种写法的延迟，记录差异。
4. 把手动注解改成 `stage=[0,1], order=[1,0]`（重排：先发 compute 再发 copy），观察 prologue/epilogue 结构的变化与是否仍能正确运行。
5. 试着**故意**把 `stage`/`order` 写错（例如条目数给错、或 producer 落后于 consumer），阅读 `ValidateScalarDependencies` 与 `FilterAnnotationsForScheduledStmts` 抛出的报错信息。

> 现象预期：自动推断（`num_stages=3`）会得到 `gemm` 在 stage 2（「copy 在尾部」优化后的结果），需要三版本缓冲；手动 `stage=[0,0,1]` 深度为 2，只需双缓冲。两者的 prologue/epilogue 展开次数与 shared 内存占用应不同。延迟差异依赖具体硬件，待本地验证。

## 6. 本讲小结

- **动机**：软件流水线让 `T.copy`（搬运）与 `T.gemm`（计算）在主循环里重叠，`num_stages` 表示流水线深度，决定 shared 缓冲的开份数。
- **DSL 入口**：`T.Pipelined` 只是薄封装，把意图（`num_stages` 或 `stage/order`）写进 `For` 注解；真正的活儿在两个 C++ Pass。
- **两个 Pass**：`PipelinePlanning`（大脑，贴 `software_pipeline_stage/order` 注解，含「copy 在尾部」优化与 ROCm 回退）→ `InjectSoftwarePipeline`（手脚，重写为 prologue/body/epilogue，做多版本缓冲与 barrier 展开）。每个后端都按此顺序（`tilelang/<backend>/pipeline.py`）。
- **多版本缓冲**：版本数上界 `use - def + 1`，访问时用 `floormod(loop_var - min, V)` 选版本。
- **可重放 Bind 规则**：纯标量别名不占 `stage`/`order` 条目，编译器按消费者逻辑迭代重放；读体内会写缓冲的 `Bind` 必须占条目且与消费者同 stage。
- **诊断**：条目数不匹配会被 `FilterAnnotationsForScheduledStmts` 过滤或 `ValidateScalarDependencies` 报错；推断失败时可改用手动 `stage`/`order`。

## 7. 下一步学习建议

- **u3-l4（布局、swizzle 与 L2 优化）**：软件流水线解决了「搬运/计算重叠」，swizzle 则解决「多 block 间的 L2 局部性」，二者常组合使用。注意 `PersistentTileScheduler`（`tile_schedule.py`）用 `current_iter` 作为流水线相位时钟，是把持久化 kernel 与 `T.Pipelined` 结合的关键。
- **u6-l2（关键 lowering Pass 解读）**：本讲聚焦流水线两个 Pass；`lower_tile_op`、`storage_rewrite` 等其它 Pass 共同决定最终代码质量。
- **u9-l1（调试工具：lower trace）**：用 `lower_trace` / `pass_visualizer` 可以逐 Pass 看到 `PipelinePlanning` 与 `InjectSoftwarePipeline` 前后的 IR 差异，是诊断流水线问题的利器。
- **阅读建议**：通读 `docs/programming_guides/software_pipeline.md` 全文，再对照 `testing/python/transform/test_tilelang_transform_pipeline_planning.py` 与 `test_tilelang_transform_Inject_software_pipeline.py` 两个测试文件，能把「规则」与「IR 产物」一一对应起来。
