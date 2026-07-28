# 软件流水线与异步拷贝

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `T.Pipelined(..., num_stages=...)` 这个循环构造「想表达什么」、它为何能隐藏访存延迟。
- 把「软件流水线」这件事拆成两个编译 pass：先有 `PipelinePlanning`（规划），再有 `InjectSoftwarePipeline`（重写）。
- 解释 `num_stages` 如何决定缓冲副本数、prologue/body/epilogue 三段如何切分，以及 copy 与 compute 如何被错位重叠。
- 区分三种异步搬运机制：`cp.async`（CUDA Ampere+）、TMA（CUDA Hopper+）、以及 MACA 的 `memcpy_async`，并知道它们各自靠什么同步。
- 理解 `mbarrier`（共享内存屏障）的 phase/parity 原理，以及编译器为何要把单个 barrier 扩展成 `num_stages` 个槽位。
- 记住一个 metax 分支的关键事实：**MACA 后端在 `PipelinePlanning` 中被显式关闭了隐式 async-copy 流水线**（`use_async_copy = false`）。

## 2. 前置知识

本讲假定你已读过 [u4-l1 从 DSL 到 IR 的 lowering 流程](u4-l1-lowering-pipeline.md)，知道 pass 流水线与 `lower` 主流程；以及 [u2-l3 循环与控制流](u2-l3-loops-and-control-flow.md)，知道 `T.Pipelined` 是一种循环构造，会用 `num_stages` 控制流水深度。下面用通俗语言补两个前置概念。

### 2.1 为什么 GPU kernel 需要「软件流水线」

一个典型的分块 GEMM 内层循环长这样：把一块 A、一块 B 从显存搬到 shared memory，再用张量核算一次累加。如果严格串行执行，每次迭代都要「等搬完→才能算」，访存单元和计算单元轮流空闲，这是巨大的浪费。

软件流水线（software pipelining）的核心思想是**提前发射下一轮甚至下几轮的搬运**：在算第 `i` 轮的同时，把第 `i+1`、`i+2` 轮的数据先搬进来。这样访存与计算就被「错位重叠」了。代价是要同时持有多个尚未消费的数据副本——这正是 `num_stages` 控制的东西。

### 2.2 两种「重叠」的同步方式

为了让搬运与计算真正并行，搬运本身必须是**异步的**（提交后立刻返回，不阻塞），之后再用某种机制等它完成：

- **cp.async（CUDA）**：用 `cp.async.commit_group` 把若干次异步拷贝归组，用 `cp.async.wait_group N` 等到「在途」的拷贝少于 N 个。
- **TMA（CUDA Hopper+）**：用 mbarrier（内存屏障）记录「期望收到的字节数」，搬运完成时 arrive，消费端用 `mbarrier_wait_parity` 等待。
- **memcpy_async（MACA）**：把 barrier handle 写入一个 barrier buffer，消费端用 `T.maca_barrier_arrive_and_wait()` 同步。

本讲的「异步拷贝」和「mbarrier」两个模块，就是讲编译器如何自动选择这些机制并插入同步。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [docs/programming_guides/software_pipeline.md](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/programming_guides/software_pipeline.md) | 用户文档：`stage`/`order`/`num_stages` 的语义与注意事项 |
| [tilelang/language/loop.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/loop.py) | 前端：`T.Pipelined` 构造器，把参数交给 C++ 生成带 `num_stages` 注解的循环 |
| [src/transform/pipeline_planning.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/pipeline_planning.cc) | **Pass 1**：`tl.PipelinePlanning`，把 `num_stages` 规划成 `stage`/`order` 注解，并标记哪些 copy 是异步生产者 |
| [src/transform/inject_pipeline.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc) | **Pass 2**：`tl.InjectSoftwarePipeline`，把循环重写成 prologue/body/epilogue，多版本化缓冲，插入 mbarrier/cp.async 同步 |
| [tilelang/language/copy_op.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py) | 前端：`T.copy` / `T.async_copy` / `T.tma_copy` / `T.maca_async_copy` 搬运原语 |
| [src/transform/common/mbarrier.h](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/common/mbarrier.h) | mbarrier 缓冲构造辅助 |
| [src/transform/common/pipeline_utils.h](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/common/pipeline_utils.h) | 流水线注解键名与 `GetPipelineNumStages` 工具 |
| [tilelang/cuda/pipeline.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py) / [tilelang/maca/pipeline.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/pipeline.py) | 两个 pass 在编译流水线中的注册位置 |

两个 pass 的调用顺序，可见 CUDA 与 MACA 流水线完全一致——都先 `PipelinePlanning` 再 `InjectSoftwarePipeline`，且都在 `LayoutInference` 之前（这样布局推断看到的就是已经流水化后的结构）：见 [tilelang/cuda/pipeline.py:102-117](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py#L102-L117) 与 [tilelang/maca/pipeline.py:39-54](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/pipeline.py#L39-L54)。

## 4. 核心概念与源码讲解

### 4.1 Pipelined：用户接口与软件流水线的直觉

#### 4.1.1 概念说明

`T.Pipelined` 是 TileLang 表达软件流水线的唯一前端入口。它和 `T.serial` 一样产生一个 `for` 循环，但额外告诉编译器：「这个循环体里有生产者（搬数据）和消费者（算数据），请把它们重叠起来」。重叠的「深度」由 `num_stages` 控制——它表示生产者与消费者之间最多隔几轮迭代，等价于同时持有的数据缓冲份数。

最常见、也是推荐的写法是**让编译器自动推断**：只给 `num_stages`，循环体里照常写 `T.copy` 和 `T.gemm`。TileLang 会自动识别哪些是 copy、哪些是 compute，并分派到合适的 stage。下面的 GEMM 示例（`num_stages=3`）就是这种用法：

[examples/gemm/example_gemm.py:19-24](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py#L19-L24) — 这是本讲贯穿始终的示例。

```python
for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
    T.copy(A[by * block_M, k * block_K], A_shared)   # 生产者
    T.copy(B[k * block_K, bx * block_N], B_shared)   # 生产者
    T.gemm(A_shared, B_shared, C_local)              # 消费者
```

对于循环体顺序特殊、需要手动分组的场景，TileLang 也允许显式标注 `stage` 和 `order` 两个数组，见 [docs/programming_guides/software_pipeline.md:44-86](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/programming_guides/software_pipeline.md#L44-L86)。`stage` 是逻辑流水段号（越小越早执行），`order` 是发射顺序；手动标注时不要同时设 `num_stages`，深度按 `max(stage)+1` 推断。

#### 4.1.2 核心流程

`T.Pipelined` 在前端做的事非常薄，它只是把参数打包后调用 C++：

[tilelang/language/loop.py:112-191](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/loop.py#L112-L191) — 注意 `num_stages`、`order`、`stage` 都被透传给 `_ffi_api.Pipelined`。C++ 端据此生成一个带 `num_stages`（或 `tl_pipeline_stage`/`tl_pipeline_order`）注解的普通串行 `For` 节点。真正的工作留给两个 pass：

```text
T.Pipelined(extent, num_stages=N)
        │  前端打包参数
        ▼
带 "num_stages" 注解的普通 for 循环
        │
        ▼  tl.PipelinePlanning        （本讲 4.2）
带 software_pipeline_stage / software_pipeline_order 注解的循环
（+ async producer / tma copy 标记）
        │
        ▼  tl.InjectSoftwarePipeline  （本讲 4.3 / 4.4）
prologue + steady-state body + epilogue
（缓冲多版本化 + cp.async/TMA/mbarrier 同步）
```

#### 4.1.3 num_stages 的数学含义

设 `num_stages = N`，外层循环变量为 `i`（取值 `0..T-1`，T 为迭代总数）。软件流水线把「生产第 i 轮数据」和「消费第 i 轮数据」错开 N 步：

- 生产者：在第 `i` 轮发射搬运，搬的是第 `i` 轮的数据。
- 消费者：在第 `i` 轮执行计算，算的是第 `i - N` 轮的数据（因为数据提前 N 轮搬好了）。

为了让前后轮的数据不互相覆盖，缓冲需要 `N` 个副本，运行时用取模选出当前轮的槽位：

\[
\text{version}(i) = i \bmod N
\]

当 `num_stages = 0`（或推导为 1）时，不存在错位重叠，等价于普通串行循环。这正是后面 `InjectSoftwarePipeline` 里用 `floormod` 给缓冲加一维版本号的依据。

#### 4.1.4 代码实践

1. **实践目标**：直观感受 `num_stages` 改变的是「重叠深度」而非计算结果。
2. **操作步骤**：打开 `examples/gemm/example_gemm.py`，把第 19 行的 `num_stages=3` 分别改成 `1`、`2`、`4`，每次重新 `python examples/gemm/example_gemm.py` 运行。
3. **需要观察的现象**：输出的 `c` 与 `ref_c` 在所有取值下都应数值一致（结果不变）；只有末尾打印的 `tilelang Latency` 变化。
4. **预期结果**：`num_stages=1` 时无重叠、延迟最高；`2/3` 通常显著下降；`4` 在 shared memory 够用时可能再降一点或持平（副本过多会吃光 shared memory，反而可能失败）。
5. 如果当前环境无 GPU，本步骤为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`num_stages=1` 时，软件流水线退化成什么？
**答**：退化为普通串行循环——没有提前搬运，生产者与消费者在同一轮紧邻执行，不存在重叠。

**练习 2**：为什么 `num_stages` 不能无限增大？
**答**：每多一级就要多一份 shared memory 副本（`version(i)=i mod N`），shared memory 容量有限（如每 SM 48–228 KB），过大会导致分配失败或占用过多而降低占用率（occupancy）。

---

### 4.2 pipeline_planning：从 num_stages 到 stage/order

#### 4.2.1 概念说明

`PipelinePlanning` 是软件流水线的**第一个 pass**，职责是把循环上那个朴素的 `num_stages` 注解，翻译成下游能理解的、精确到每条语句的 `software_pipeline_stage` / `software_pipeline_order` 两个数组，并附带「哪些 copy 该异步发射」的标记。它的关键设计是：**先分析每条语句读了/写了哪些 buffer、谁是 copy、谁消费它，再据此决定 stage 和 order**。

它注册为 `tl.PipelinePlanning`，入口在 [src/transform/pipeline_planning.cc:1334-1348](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/pipeline_planning.cc#L1334-L1348)：

```cpp
tvm::transform::Pass PipelinePlanning() {
  auto pass_func = [=](PrimFunc f, const IRModule &m, PassContext ctx) {
    bool use_async_copy =
        ctx->GetConfig<Bool>("tirx.use_async_copy", Bool(true)).value();
    auto target = f->GetAttr<Target>(tvm::attr::kTarget);
    if (TargetIsMaca(target.value())) {
      use_async_copy = false;   // ★ MACA 显式关闭隐式 async-copy 流水线
    }
    PrimFuncNode *fptr = f.CopyOnWrite();
    fptr->body = PipelinePlanner::Substitute(f, use_async_copy);
    return f;
  };
  return CreatePrimFuncPass(pass_func, 0, "tl.PipelinePlanning", {});
}
```

这一段是 metax 分支的**核心差异点之一**：MACA 即便硬件支持 async copy（`TargetMacaHasAsyncCopy` 恒返回 `true`，见 [src/maca/target_utils.cc:35-39](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/target_utils.cc#L35-L39)），但隐式 async-copy 流水线尚未验证，于是被强制退化为普通重叠（仍会多版本化缓冲、错位发射 copy，只是 copy 不走异步通道）。若要异步搬运，MACA 需用显式的 `T.maca_async_copy`（见 4.3）。

#### 4.2.2 核心流程

`PipelinePlanner::VisitStmt_(ForNode)` 处理两种入口（[src/transform/pipeline_planning.cc:1061-1316](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/pipeline_planning.cc#L1061-L1316)）：

- **显式 `stage`/`order` 注解**（1065–1122 行）：用户已写好两个数组，pass 只需把可重放的标量 `Bind` 过滤掉，保留可调度语句的标注。
- **`num_stages` 自动推断**（1124–1316 行）：核心路径，流程如下：

```text
1. 取出 num_stages（1126 行），校验 ≥1 且为 kSerial 循环。
2. ROCm 特判：非 gfx950 时退化为普通串行（剥掉 num_stages 注解），1133-1149 行。
3. 扁平化循环体为语句列表，分析出哪些是「可调度语句」、哪些是可重放 Bind
   （AnalyzeScheduledStmts，1158 行）。
4. 为每条可调度语句建 PipelineStageInfo：收集读/写区域、判定是否 copy 阶段
   （MakePipelineStageInfo + ClassifyCopyLikeStage）。
5. 传播 copy 的生产者依赖（PropagateBufferProducersForCopy）。
6. 分析每个 copy 的「最后使用者」下标（AnalyzeCopyLastUse）。
7. 按 stage 分配 + 把 copy 排到其消费者前（1194-1222 行）。
8. 若所有 copy 都在尾部，整体前移并把 stage 偏移减 1（1230-1255 行）。
9. 生成 software_pipeline_stage / software_pipeline_order 注解；
   记 tl_pipelined_num_stages；标记 TMA copy；EmitImplicitAsyncAnnotations。
```

**第 7 步**是理解错位的关键（[src/transform/pipeline_planning.cc:1194-1222](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/pipeline_planning.cc#L1194-L1222)）：非 copy 语句被赋予 `stage = num_stages`（最深的消费段），而 copy 语句被赋予 `stage = 0`（最早的生产段），并按「谁最后消费它」决定其 `order`。这样 copy（stage 0）与 compute（stage N）天然分到了不同的逻辑段，下游重写时才能错位。

**第 6 步 `AnalyzeCopyLastUse`**（[src/transform/pipeline_planning.cc:612-652](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/pipeline_planning.cc#L612-L652)）会顺着 use-def 链找到某份 shared 数据「最后一次被读」的语句下标 `last_use_stmt_index`，这个值决定了 copy 应排在哪——尽量贴近它的最终消费者，从而最小化需要持有的副本数。

#### 4.2.3 源码精读：如何识别一个 copy

`BufferRegionCollector::HandleTileOp` 负责判定一条 tile op 是不是「global → shared 的 copy」，从而打上 `copy_stage` 标记，[src/transform/pipeline_planning.cc:109-147](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/pipeline_planning.cc#L109-L147)：

```cpp
if (const auto *copy = tile_op.as<CopyNode>()) {
  if (IsGlobalLikeBuffer(copy->src) && IsSharedBuffer(copy->dst)) {
    is_global_copy_pattern_ = true;     // 这是一条 global->shared copy
  }
}
// Im2Col 在 Hopper 上走 TMA
if (const auto *im2col = tile_op.as<Im2ColOpNode>()) {
  if (IsGlobalLikeBuffer(im2col->src_) && IsSharedBuffer(im2col->dst_)) {
    is_global_copy_pattern_ = true;
    if (TargetIsHopper(target_)) is_tma_copy_ = true;   // 标记走 TMA 通道
  }
}
```

这段说明：只有 `global → shared` 的搬运才会被当作可异步的生产者；`shared → fragment` 这类搬运不算（它通常被吸收进 compute）。TMA 的判定依赖 target（Hopper 才有 TMA）。

#### 4.2.4 源码精读：隐式 async 注解的发射

`EmitImplicitAsyncAnnotations`（[src/transform/pipeline_planning.cc:835-904](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/pipeline_planning.cc#L835-L904)）把「异步生产者」信息写进注解。注意它最顶上的守卫：

```cpp
bool EmitImplicitAsyncAnnotations(...) const {
  if (!TargetHasAsyncCopy(target_) || !use_async_copy_) {
    return false;          // 不满足就完全不发 async 注解
  }
  ...
  annotations->Set(kPipelineAsyncProducers, ...);        // 每条语句是否异步生产者
  annotations->Set(kPipelineAsyncProducerGroups, ...);   // 异步分组 id
  annotations->Set(s_tir::attr::software_pipeline_async_stages, ...);
  return true;
}
```

由于 MACA 时 `use_async_copy_ = false`，这里直接返回 `false`，于是 MACA 的循环不会带 `software_pipeline_async_producers` 注解——下游 `InjectSoftwarePipeline` 就不会把它当 cp.async 异步通道处理。这正是「MACA 有 async 硬件、但隐式 async 流水线被关」的实现落点。

> 说明：`TargetHasAsyncCopy` 的统一分发在 [src/backend/common/target_utils.cc:15-26](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/backend/common/target_utils.cc#L15-L26)，CUDA 要求 arch≥80（[src/cuda/target_utils.cc:85-90](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/target_utils.cc#L85-L90)），MACA 恒为 true。注解键名定义在 [src/transform/common/pipeline_utils.h:29-40](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/common/pipeline_utils.h#L29-L40)。

#### 4.2.5 代码实践

1. **实践目标**：理解 `PipelinePlanning` 只做「规划」，不改语义。
2. **操作步骤（源码阅读型）**：在 [src/transform/pipeline_planning.cc:1196-1222](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/pipeline_planning.cc#L1196-L1222) 处，对照 GEMM 示例的三条语句（`copy A`、`copy B`、`gemm`），手算它们最终得到的 `stage` 和 `order`：假设 `num_stages=3`，两个 copy 应得 `stage=0`，gemm 得 `stage=3`。
3. **需要观察的现象**：你会看到 copy 的 `order` 紧贴 gemm（因为 gemm 是它们的消费者），而不是简单按源码顺序。
4. **预期结果**：两条 copy 在 stage 0，gemm 在 stage 3；这正是「提前 3 轮搬数据」的编码方式。
5. 若想直接验证，可借助 `tilelang/tools/pass_visualizer`（[tilelang/tools/pass_visualizer/core.py:124-125](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tools/pass_visualizer/core.py#L124-L125)）把 `PipelinePlanning` 之后的 IR 打印出来查看注解。

#### 4.2.6 小练习与答案

**练习 1**：为什么 `PipelinePlanning` 要把 copy 排到「它的最后消费者」之前，而不是循环开头？
**答**：排到消费者前可以最小化 buffer 需要同时持有的副本数（`use - def + 1`），从而省 shared memory；副本数计算由下游 `ComputeBufferVersions` 完成（见 4.4）。

**练习 2**：MACA 下 `EmitImplicitAsyncAnnotations` 返回什么？为什么？
**答**：返回 `false`。因为 `PipelinePlanning` 入口把 MACA 的 `use_async_copy` 置为 `false`，守卫 `!use_async_copy_` 命中，于是不发任何 async producer 注解——MACA 的隐式 async-copy 流水线被关闭。

---

### 4.3 异步拷贝机制：cp.async / TMA / maca_async_copy

#### 4.3.1 概念说明

「异步拷贝」指提交后不阻塞、稍后再同步的搬运。TileLang 在前端提供四个相关原语，它们对应不同的硬件通道和同步方式：

| 原语 | 通道 | 适用 target | 同步方式 |
|------|------|------------|---------|
| `T.copy` | 自动选择（TMA / cp.async / 普通循环） | 全部 | 自动（在流水线内由 pass 插入） |
| `T.async_copy` | `cp.async` | CUDA Ampere+ | 显式 `commit_group` + `wait_group` |
| `T.tma_copy` | TMA | CUDA Hopper+ | 显式 mbarrier（`expect_tx` + `wait_parity`） |
| `T.maca_async_copy` | `memcpy_async` | MACA | 显式 barrier（`maca_barrier_arrive_and_wait`） |

注意「自动」与「显式」的区别：在 `T.Pipelined` 内写 `T.copy`，编译器会自动判定它是不是 `global → shared` 的异步生产者（4.2 已述），并选择通道；而 `T.async_copy` / `T.tma_copy` / `T.maca_async_copy` 是给需要手动掌控同步（如 warp specialization、自定义 barrier）的高级用户用的。

#### 4.3.2 核心流程

四个原语在 [tilelang/language/copy_op.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py) 里都很薄——本质是把 src/dst 规整成 `BufferRegion`，再 `call_intrin` 发射对应的 tile op：

- `T.copy`（[tilelang/language/copy_op.py:53-133](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py#L53-L133)）：发射 `tl.tileop.copy`，带 `disable_tma` / `prefer_instruction` 等注解。
- `T.async_copy`（[tilelang/language/copy_op.py:189-230](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py#L189-L230)）：发射 `tl.tileop.async_copy`，文档明言「发射 `ptx_cp_async(...)` + `ptx_commit_group()`，**不自动插 wait**」。
- `T.tma_copy`（[tilelang/language/copy_op.py:233-309](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py#L233-L309)）：发射 `tl.tileop.tma_copy`，要求传入 `barrier`，发射 `expect_tx + tma_load`，wait 交给用户。
- `T.maca_async_copy`（[tilelang/language/copy_op.py:496-540](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py#L496-L540)）：发射 `tl.tileop.maca_async_copy`，把 barrier handle 写入提供的 barrier buffer，需用户调 `T.maca_barrier_arrive_and_wait()` 同步。

#### 4.3.3 源码精读：T.async_copy 的契约

[tilelang/language/copy_op.py:189-230](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py#L189-L230) 的关键约定写在 docstring 里：

> The backend enforces cp.async constraints and emits: `ptx_cp_async(...)` + `ptx_commit_group()`. **No wait is auto-inserted for `T.async_copy`; synchronization is explicit.**

这说明：单独使用 `T.async_copy` 时你必须自己管 wait；但当它出现在 `T.Pipelined` 内、且被 `PipelinePlanning` 识别为异步生产者时，同步（commit/wait）会由 `InjectSoftwarePipeline` 自动插入（见 4.4 的 `AsyncCommitWaitAttrLowerer`）。两套机制是互补的。

#### 4.3.4 源码精读：T.tma_copy 与 barrier 的绑定

[tilelang/language/copy_op.py:290-309](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py#L290-L309) 把用户传入的 barrier 转成 `BufferLoad` 放进注解：

```python
if barrier is not None:
    from .builtin import _mbar_to_buffer_load
    ann["barrier"] = _mbar_to_buffer_load(barrier)
...
return tirx.call_intrin("handle", tirx.op.Op.get("tl.tileop.tma_copy"), src, dst, annotations=ann if ann else None)
```

TMA 的同步模型是「期望字节数」：load 前先 `expect_tx(N)`，搬运完成时硬件 arrive 这个 barrier 并减去 N 字节，消费端用 `mbarrier_wait_parity` 等到 phase 翻转。这个 parity 的计算正是 4.4 要讲的。

#### 4.3.5 代码实践

1. **实践目标**：对比「自动 T.copy」与「显式 T.async_copy」在生成代码上的差异。
2. **操作步骤**：复制 `examples/gemm/example_gemm.py` 为临时脚本，把 `T.copy(A[...], A_shared)` 改成 `T.async_copy(A[...], A_shared)`（同样改 B），编译并打印 `kernel.get_kernel_source()`。
3. **需要观察的现象**：在 `num_stages=3` 的流水线内，两者生成的 `cp.async` 指令形态接近；若把它移出 `T.Pipelined`，`T.async_copy` 不会自动插 wait，需要你手动加同步，否则读到未完成的数据。
4. **预期结果**：流水线内编译器自动管理 commit/wait；流水线外需手动同步。
5. 无 GPU 时，至少完成「打印生成的 CUDA 源码并找到 `cp.async`/`mbarrier` 字样」这一步；若 target 是 maca，预期看不到自动 cp.async（因 4.2 所述），可改用 `T.maca_async_copy`。

#### 4.3.6 小练习与答案

**练习 1**：在 `T.Pipelined` 内用 `T.copy`，需要自己写 `commit_group` / `wait_group` 吗？
**答**：不需要。`PipelinePlanning` 会把 `global→shared` 的 copy 标为异步生产者，`InjectSoftwarePipeline` 会自动插入 commit/wait（或 mbarrier）。

**练习 2**：为什么 MACA 用的是 `memcpy_async` 而不是 `cp.async`？
**答**：`cp.async` 是 NVIDIA PTX 的指令；MACA（MetaX）有自己的异步搬运硬件，暴露为 `memcpy_async` 语义，配套的同步是 barrier handle + `maca_barrier_arrive_and_wait`，由 `T.maca_async_copy` 暴露。

---

### 4.4 mbarrier 同步与 InjectSoftwarePipeline 重写

#### 4.4.1 概念说明

`InjectSoftwarePipeline`（`tl.InjectSoftwarePipeline`）是软件流水线的**第二个 pass**，承担真正改写 IR 的工作：把那条带 `stage`/`order` 注解的循环，拆成 **prologue（预热）+ steady-state body（稳态）+ epilogue（收尾）** 三段，给需要的缓冲加一维「版本」下标，并在适当位置插入 commit/wait 或 mbarrier 同步。它注册在 [src/transform/inject_pipeline.cc:4020-4029](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L4020-L4029)，结尾还跑一次 `ConvertSSA` 保证变量名干净。

「mbarrier」（memory barrier）是 Hopper 引入的共享内存屏障原语，存在 `shared.barrier` 作用域里。它用 **phase（相位）** 计数：每次到达预期字节数就翻转 phase，等待方用 `mbarrier_wait_parity(phase_parity)` 阻塞到指定位（0 或 1）。软件流水线里多个轮次复用同一 barrier，必须靠 phase 区分，否则会与上一轮的到达混淆。

#### 4.4.2 核心流程

主驱动是 `PipelineInjector::VisitStmt_(ForNode)`（[src/transform/inject_pipeline.cc:3363-3786](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L3363-L3786)），流程如下：

```text
1. 递归改写子节点；若循环无 pipeline 注解则原样返回。
2. 抽出循环体语句流；剥离 AllocBuffer/DeclBuffer 声明（仍登记为本地分配）。
3. 区分「可重放标量 Bind」与「可调度语句」，校验 stage/order 合法性
   （ValidatePipelineBody / ValidateScheduledBindDependencies）。
4. 若无可重叠 stage（所有语句同段），剥掉流水线注解、原样返回。
5. TMA barrier 改写：RewritePipelineTmaBarriers（仅当有 TMA copy）。
6. barrier 扩展：ExpandPipelineBarriers（把所有 shared.barrier 从 [N] 扩到 [N*depth]）。
7. RewritePipeline：缓冲多版本化 + 发射 prologue/body/epilogue。
8. 更新 barrier_init 注解；LowerAsyncCommitWaitAttrs 把 async 属性降级为具体指令。
```

**第 7 步**的核心是 `PipelineRewriter::BuildPipeline`（[src/transform/inject_pipeline.cc:1192-1268](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L1192-L1268)）与 `EmitImpl`（[src/transform/inject_pipeline.cc:2828-3041](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L2828-L3041)）。三段的边界由 `max_stage_`（最大 stage 号）决定：

```text
prologue  : [min,            min + max_stage)     预热，逐轮展开
body      : [min + max_stage, min + extent)        稳态，完整一轮
epilogue  : [min + extent,    min + extent + max_stage)  收尾，逐轮展开
```

在 `EmitImpl` 里，每条语句的「逻辑迭代号」由当前循环变量按其 stage **反向偏移**得到——这正是错位的实现（[src/transform/inject_pipeline.cc:2893-2898](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L2893-L2898)）：

```cpp
PrimExpr skewed_loop_var = new_loop_var - stage;   // ← 按 stage 错位
if (need_bound_check)
  inbound = (pipeline_loop_->min <= skewed_loop_var) &&
            (skewed_loop_var < pipeline_loop_->min + pipeline_loop_->extent);
```

也就是说，stage 越大的语句（compute）「看到」的迭代号越小，于是它消费的是更早搬进来的数据——与 4.1 的数学描述完全对应。

#### 4.4.3 源码精读：缓冲多版本化

缓冲需要几份副本由 `ComputeBufferVersions`（[src/transform/inject_pipeline.cc:1472-1527](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L1472-L1527)）用活跃性分析决定：

```cpp
int num_versions = buffer_info.use - buffer_info.def + 1;   // 上界：最后用 - 最早定义 + 1
if (num_versions >= 2) {
  // 特判：若不存在跨 stage 的读后写冲突，可减一份
  ...
}
```

确定份数后，`RewriteAllocBuffer`（[src/transform/inject_pipeline.cc:1535-1544](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L1535-L1544)）在 shape 最前面加一维 `num_versions`；随后 `PipelineBodyRewriter` 把每次访问的下标前置一个 `floormod` 版本号（[src/transform/inject_pipeline.cc:1099-1112](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L1099-L1112)）：

```cpp
PrimExpr version = floormod((pipeline_loop_->loop_var - pipeline_loop_->min),
                            new_buffer->shape[0]);
n->indices.insert(n->indices.begin(), version);   // ← 取模选当前轮副本
```

这就是 4.1 里 \(\text{version}(i)=i\bmod N\) 的代码落地：同一份 shared buffer 被物理复制成 N 份，第 i 轮访问第 `i mod N` 份，于是提前搬的数据不会被覆盖。

#### 4.4.4 源码精读：mbarrier 的扩展与 parity

为了让多个流水轮次复用同一组 barrier，`ExpandPipelineBarriers`（[src/transform/inject_pipeline.cc:769-824](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L769-L824)）把每个 `shared.barrier` 缓冲从 `[N]` 扩成 `[N * num_stages]`，并把所有访问下标改成带 stage 偏移：

```cpp
PrimExpr stage_expr  = FloorMod(loop_var - loop_min, ns);                       // 当前轮的槽
PrimExpr parity_cycle= FloorMod(FloorDiv(loop_var - loop_min, ns), 2);          // 当前轮的 phase
...
new_node->shape = {PrimExpr(num_stages) * buf->shape[0]};                       // 扩容
n->indices.Set(0, stage_expr * old_size + n->indices[0]);                        // 下标加偏移
```

注意 `parity_cycle` 这个表达式——它就是 `mbarrier_wait_parity` 要等的位。barrier 的 phase 每 `ns` 轮翻转一次，取模 2 得到 0/1：

\[
\text{phase}(i) = \left\lfloor \frac{i - \text{loop\_min}}{N} \right\rfloor \bmod 2
\]

随后 `BarrierIndexRewriter` 会把 `mbarrier_wait_parity` 的第二参数（parity）改写成上述表达式（[src/transform/inject_pipeline.cc:719-748](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L719-L748)），这样等待方就能正确等到「当前轮」的那次到达，而不是上一轮残留的到达。

对于 TMA 路径，`RewritePipelineTmaBarriers`（[src/transform/inject_pipeline.cc:906-994](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L906-L994)）会创建一个共享的 `pipeline_mbar`，把 `tl.tileop.copy` 改写成 `tl.tileop.tma_copy`（带 barrier 与 `emit_arrive`），并在第一个消费段前插入 `mbarrier_wait_parity`。barrier 缓冲本身由 `CreateMBarrierBuffer` 构造在 `shared.barrier` 作用域（[src/transform/common/mbarrier.h:23-28](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/common/mbarrier.h#L23-L28)）。

#### 4.4.5 源码精读：cp.async 的 commit/wait 降级

对非 TMA 的异步 copy（cp.async），pass 用 `async_commit_queue_scope` / `async_wait_queue_scope` 属性语句表达「归组」与「等待」，最后由 `AsyncCommitWaitAttrLowerer`（[src/transform/inject_pipeline.cc:346-385](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L346-L385)）降级成真实的 PTX 内建：

```cpp
Stmt commit = Evaluate(Call(..., builtin::ptx_commit_group(), {}));     // cp.async.commit_group
Stmt wait   = Evaluate(Call(..., builtin::ptx_wait_group(), {N}));      // cp.async.wait_group N
```

等待计数 N 由 `PopulateWaitCounts` + `CompletePipelineLoopStatements` 推导（基于「生产者头」与「消费者访问号」之差），并有 `AsyncPipelineLoopWaitRelaxer` / `RelaxTrailingConsumerWaits` 做松弛优化，尽量把 wait 往后挪以隐藏更多延迟。这一整套机制**只在 `use_async_copy` 为真时生效**——MACA 因为被关掉，走的是普通同步 copy + 多版本缓冲的路径。

#### 4.4.6 代码实践

1. **实践目标**：在生成的设备源码里「看见」软件流水线的三段结构与同步指令。
2. **操作步骤**：编译 `examples/gemm/example_gemm.py`（`num_stages=3`），打印 `kernel.get_kernel_source()`，在 CUDA 源码里搜索：循环开头的多次 `cp.async`（prologue 预热）、稳态里的 `cp.async.commit_group` / `cp.async.wait_group`、以及结尾的收尾计算。
3. **需要观察的现象**：你会看到搬运次数多于计算次数（因为预热和收尾各多搬了几轮）；`wait_group` 的在途计数与 `num_stages` 相关。
4. **预期结果**：`num_stages=3` 时稳态里典型出现 `wait_group 2`（允许 2 个在途），从而 copy 与 compute 重叠。
5. 若 target 为 maca，由于 4.2 所述，预期**不会**出现自动 `cp.async`/`mbarrier`，而是普通同步搬运 + 多版本 shared buffer——可据此验证 metax 分支的差异。

#### 4.4.7 小练习与答案

**练习 1**：为什么 mbarrier 要按 `num_stages` 扩展成多个槽，而不是共用一个？
**答**：流水线里同时有多个轮次的搬运在途，它们 arrive 的是不同轮的数据；共用一个 barrier 会把不同轮的到达混在一起。扩展成 `num_stages` 个槽并用 `stage_expr` 选槽、用 `parity_cycle` 选相位，才能让等待方精确等到当前轮的那次到达。

**练习 2**：`ComputeBufferVersions` 算出的副本数一定等于 `num_stages` 吗？
**答**：不一定。它的上界是 `use - def + 1`（最后使用 stage − 最早定义 stage + 1），若不存在跨 stage 的读后写冲突还会再减一。只有当 copy 在 stage 0、其消费者在 stage `num_stages` 时，副本数才接近 `num_stages`。

**练习 3**：把 `num_stages` 从 3 调到 1，`InjectSoftwarePipeline` 还会生成 prologue/epilogue 吗？
**答**：不会。`num_stages=1` 时没有可重叠的 stage（`HasOverlappableStages` 为假），pass 在第 4 步就剥掉流水线注解、原样返回普通循环（[src/transform/inject_pipeline.cc:3592-3601](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L3592-L3601)）。

---

## 5. 综合实践

把本讲的知识串起来，做一个「num_stages 扫参 + 源码印证」的小任务。

**任务**：基于 `examples/gemm/example_gemm.py`，固定 `M=N=K=1024`、`block_M=block_N=128`、`block_K=32`，只变 `num_stages`（取 1、2、3、4），完成下表并解释。

| num_stages | 延迟 (ms) | shared 副本数 | 是否出现 cp.async 异步 | prologue 搬运次数 |
|------------|----------|--------------|----------------------|------------------|
| 1          | ?        | 1            | 否                   | 0                |
| 2          | ?        | ?            | ?                    | ?                |
| 3          | ?        | ?            | ?                    | ?                |
| 4          | ?        | ?            | ?                    | ?                |

**步骤**：

1. 编写一个脚本，循环 `for ns in [1,2,3,4]`，每次 `matmul.compile(..., )` 时把 `num_stages` 传进去（注意 `num_stages` 是 kernel 定义里的参数，需在 `@tilelang.jit` 函数签名里暴露它，或为每个 ns 单独定义函数）。
2. 用 `kernel.get_profiler().do_bench(backend="cupti")` 记录延迟填表（CUDA 环境）。无 GPU 则此项标「待本地验证」。
3. 对每个 ns 调 `kernel.get_kernel_source()`，搜索 `cp.async`、`commit_group`、`wait_group`（或 maca 下的对应指令）确认异步通道是否启用、在途计数是多少。
4. 结合 4.4.2 的三段切分公式解释 prologue 的搬运次数：prologue 长度为 `max_stage`，故预热阶段会多搬 `max_stage` 轮。

**预期结论**：

- `num_stages=1`：无流水线，延迟最高，无 cp.async 异步。
- `num_stages=2/3`：副本数与异步在途计数随 ns 增长，延迟显著下降。
- `num_stages=4`：若 shared memory 仍够，延迟可能再略降；若吃紧则可能报错或占用率下降导致反而变慢——这是「副本数 vs shared 容量」的权衡。
- MACA target：表格第 4 列预期全为「否」（隐式 async 被关），印证 metax 分支的差异。

> 提示：若要在不改动源码的前提下扫参，可参考 `examples/gemm/example_gemm_autotune.py` 用 `tilelang.autotuner` 定义 `num_stages` 参数空间（自动调优将在 [u8-l1](u8-l1-autotuner.md) 详述）。

## 6. 本讲小结

- `T.Pipelined(..., num_stages=N)` 是表达软件流水线的唯一入口；`num_stages` 控制 copy/compute 错位的深度与缓冲副本数，结果不变只变性能。
- 软件流水线分两个 pass：`PipelinePlanning` 把 `num_stages` 规划成 `stage`/`order` 注解并标记异步生产者；`InjectSoftwarePipeline` 据此把循环重写成 prologue/body/epilogue，并插入同步。
- 缓冲多版本化靠 `floormod(loop_var, num_versions)` 选当前轮副本；副本数由活跃性分析 `use - def + 1` 决定，不一定等于 `num_stages`。
- 异步搬运有三条通道：`cp.async`（CUDA，commit/wait）、TMA（Hopper，mbarrier + expect_tx）、`memcpy_async`（MACA，barrier handle）；流水线内用 `T.copy` 时编译器自动选通道与同步。
- mbarrier 靠 phase/parity 区分不同轮次的到达；pass 把 barrier 扩展成 `num_stages` 个槽，并用 \(\lfloor(i-\text{min})/N\rfloor\bmod 2\) 计算等待相位。
- **metax 关键差异**：MACA 在 `PipelinePlanning` 中被显式置 `use_async_copy=false`，隐式 async-copy 流水线被关闭，退化为普通同步 copy + 多版本缓冲；要异步需显式用 `T.maca_async_copy`。

## 7. 下一步学习建议

- 想看流水线 + 布局推断如何配合？继续读 [u4-l3 内存布局推断 Layout/Fragment](u4-l3-layout-inference.md)，注意两个流水线 pass 都跑在 `LayoutInference` 之前。
- 想理解 warp specialization（生产者/消费者 warp 分离）这条更强的重叠路径？它由 `allow_warp_specialized` 在 CUDA 流水线里开启（[tilelang/cuda/pipeline.py:94-95](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/pipeline.py#L94-L95)），是软件流水线的「升级版」。
- 想用扫参找最优 `num_stages`？进入 [u8-l1 自动调优 autotuner](u8-l1-autotuner.md) 与 [u8-l3 性能剖析与基准测试](u8-l3-profiling-and-benchmark.md)。
- 想深入 MACA 后端的同步原语？阅读 `tilelang/maca/` 下的 intrinsics 与 [src/maca/transform/lower_maca_intrin.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/transform/lower_maca_intrin.cc)（详见 [u7-l4 MACA 编译流水线与 transform](u7-l4-maca-pipeline.md)）。
