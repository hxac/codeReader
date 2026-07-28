# 软件流水线与异步拷贝

## 1. 本讲目标

本讲承接 u4-l1（lowering 流程）与 u2-l3（循环与控制流），专门拆解 `T.Pipelined` 背后那条「软件流水线」编译链。学完后你应当能够：

- 说清 **软件流水线（software pipeline）** 为什么能加速 GEMM 一类的访存密集 kernel，以及 `num_stages` 到底控制了什么。
- 区分两个紧密配合又职责不同的 pass：`tl.PipelinePlanning`（规划）与 `tl.InjectSoftwarePipeline`（改写）。
- 理解 **异步拷贝**（CUDA 的 `cp.async`、Hopper 的 TMA）是如何让「搬数据」和「算」重叠起来的，以及 `commit_group` / `wait_group` 的作用。
- 掌握 **mbarrier**（显存屏障）的「抵达—等待奇偶」握手模型，以及它为何要被复制成 `num_stages` 份。
- 知道在 **metax 分支**上 MACA 后端的流水线有什么特殊之处（异步拷贝被关闭）。

---

## 2. 前置知识

### 2.1 为什么要流水线

考虑一个最朴素的分块 GEMM 循环：

```python
for k in range(num_tiles):
    copy(A_tile, A_shared)   # 搬：global -> shared
    copy(B_tile, B_shared)
    gemm(A_shared, B_shared, C_local)  # 算：shared -> fragment
```

这里「搬」和「算」是**串行**的：每个 `k` 都要先等数据搬完、再开始算，算的时候搬运单元（copy engine / 张量核加载通路）闲着，算的时候搬运通路又没活干。访存延迟被完全暴露，性能被「带宽 + 延迟」双重拖累。

软件流水线的核心想法是：**把下一个（甚至下好几个）迭代的搬运，提前到当前迭代的计算里去做**，让搬运和计算在时间上重叠。直观地说：

```text
k=0: copy0
k=1:        copy1   compute0   ← compute0 用的是 copy0 的数据
k=2:               copy2   compute1
...
```

要这么做，必须给 shared buffer 准备**多份副本**（否则 compute0 还在读 A_shared，copy1 就要往同一块 A_shared 里写，数据就乱了）。`num_stages` 就是「生产者（copy）和消费者（compute）之间最多保留几份缓冲」。

### 2.2 生产者—消费者模型

TileLang 把流水线体里的语句分成两类：

- **生产者（producer）**：通常是 global→shared 的 copy，它「制造」一份 shared 数据。
- **消费者（consumer）**：通常是 gemm/reduction，它「消费」那份 shared 数据。

每个语句有两个调度编号（u2-l3 已经提过）：

- `stage`：逻辑流水线阶段，越小越早。
- `order`：在同一调度序列里的发射顺序。

### 2.3 本讲用到的关键术语

| 术语 | 含义 |
|------|------|
| `num_stages` | 用户在 `T.Pipelined` 上指定的流水线深度，即 copy/compute 之间保留的缓冲份数 |
| prologue / steady / epilogue | 流水线改写后的三段：预热、稳态、收尾 |
| 多版本化（multi-versioning） | 把 shared buffer 复制成 N 份，用 `floormod` 下标选版本 |
| `cp.async` | Ampere+ 的异步 global→shared 拷贝指令 |
| `commit_group` / `wait_group` | 把若干异步拷贝编组、并在需要时等待其完成 |
| TMA | Hopper 的张量内存加速器，配合 mbarrier 同步 |
| mbarrier | 64 位显存屏障，「抵达—奇偶等待」握手 |
| parity（奇偶） | mbarrier 的状态位，每完成一轮翻转一次 |

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tilelang/language/loop.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/loop.py) | DSL 层的 `Pipelined` 构造器，把 `num_stages`/`stage`/`order` 转成 `for` 循环上的注解 |
| [tilelang/language/copy_op.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py) | 各种 copy 原语：`copy`/`async_copy`/`tma_copy`/`maca_async_copy` |
| [src/transform/pipeline_planning.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/pipeline_planning.cc) | **规划 pass**：分析循环体，为每条语句算出 `stage`/`order`，并标注异步生产者 |
| [src/transform/inject_pipeline.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc) | **改写 pass**：把单个循环重写成 prologue/steady/epilogue，做多版本化、插 commit/wait、管 mbarrier |
| [src/backend/common/target_utils.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/backend/common/target_utils.cc) | `TargetHasAsyncCopy`：按 target 分发「是否支持异步拷贝」 |
| [tilelang/maca/pipeline.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/pipeline.py) | MACA 的 pass 流水线，里面能看到这两个 pass 的注册位置 |
| [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py) | 实践用的 GEMM 示例 |

> 提醒：两个 pass 在 [docs/programming_guides/software_pipeline.md](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/programming_guides/software_pipeline.md) 有一份官方的用户向说明，本讲在其基础上补全「编译器内部到底做了什么」。

---

## 4. 核心概念与源码讲解

### 4.1 `T.Pipelined`：用户视角的软件流水线

#### 4.1.1 概念说明

`T.Pipelined` 是一个长得像 `T.serial` 的循环构造器，但它额外带一个 `num_stages` 参数，告诉编译器「请把这个循环软件流水化，深度为 N」。它是软件流水线**最常用、最推荐**的入口：你只要照常写 copy + gemm，编译器会自动推断出谁是生产者、谁是消费者、各自在第几阶段。

它也支持**手动调度**模式：用 `stage=[...]` 和 `order=[...]` 两个数组显式给每条「可调度语句」指定阶段与发射顺序。两种模式不要混用——要么只给 `num_stages`（让编译器推断），要么只给 `stage`/`order`（手动排程，深度由 `max(stage)+1` 推出）。

#### 4.1.2 核心流程

`T.Pipelined` 在前端做的事很薄，本质只是把参数打包，转成一个带 `num_stages`（或 `tl_pipeline_stage`/`tl_pipeline_order`）注解的普通 `for` 循环：

```text
for k in T.Pipelined(num_tiles, num_stages=3):
    T.copy(...); T.copy(...); T.gemm(...)
        │  前端
        ▼
for (k, 0, num_tiles, annotations={"num_stages": 3}) {
    copy(...); copy(...); gemm(...)
}
```

真正的「流水线化」不在前端，而在后续两个 pass。注解只是个**记号**，告诉 `tl.PipelinePlanning`「这个循环需要被规划」。

#### 4.1.3 源码精读

`Pipelined` 的签名与默认值在 [tilelang/language/loop.py:112-191](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/loop.py#L112-L191)。关键几行：

```python
def Pipelined(
    start, stop=None,
    num_stages: int = 0,        # 默认 0 = 不开启流水线
    order: list[int] | None = None,
    stage: list[int] | None = None,
    sync=None, group=None,
) -> frame.ForFrame:
    ...
    return _ffi_api.Pipelined(start, stop, num_stages, order, stage, sync, group)
```

注意两点：

1. `num_stages` 默认为 `0`，文档明确「`num_stages` 为 0 时不开启流水线」（[loop.py:129-134](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/loop.py#L129-L134)）。
2. 它把所有参数交给 C++ 侧的 `_ffi_api.Pipelined`，后者产出一个 `ForFrame`，落成带注解的 `for` 循环。

> 文档 [software_pipeline.md:7-12](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/programming_guides/software_pipeline.md#L7-L12) 给出的典型用法，正是本讲实践要改的 GEMM：

```python
for ko in T.Pipelined(T.ceildiv(K, BK), num_stages=3):
    T.copy(A[by * BM, ko * BK], A_shared)
    T.copy(B[ko * BK, bx * BN], B_shared)
    T.gemm(A_shared, B_shared, C_local)
```

#### 4.1.4 代码实践

**实践目标**：直观感受 `num_stages=0` 与 `num_stages≥1` 的差别。

1. 打开 [examples/gemm/example_gemm.py:19](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py#L19)，把 `num_stages=3` 改成 `num_stages=0`。
2. 编译并打印 kernel 源码（`kernel.get_kernel_source()`）。
3. **需要观察的现象**：`num_stages=0` 时，生成的代码里 K 循环退化成普通串行循环，**没有** prologue/epilogue，shared buffer 也没有多版本化（不会出现 `floormod` 选版本的下标）。
4. **预期结果**：`num_stages=0` 的延迟应明显高于 `num_stages=3`（待本地验证，因为依赖具体 GPU 与带宽）。

> 如果当前没有 GPU，可只做「读源码」部分：对比两种设置下 `get_kernel_source()` 的差异即可。

#### 4.1.5 小练习与答案

**练习 1**：`num_stages=1` 和 `num_stages=2` 哪个才是「经典双缓冲（double buffering）」？

> **答案**：`num_stages=2`。`num_stages` 是 copy 与 compute 之间保留的缓冲份数；2 份就是经典双缓冲，copy 和 compute 各占一份交替使用。`num_stages=1` 只有一份缓冲，producer 和 consumer 实际上无法真正重叠。

**练习 2**：为什么文档建议「手动 `stage`/`order` 时不要再设 `num_stages`」？

> **答案**：手动模式下，流水线深度由 `max(stage)+1` 推断（见 [software_pipeline.md:83-86](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/programming_guides/software_pipeline.md#L83-L86)）。若同时给 `num_stages`，二者可能冲突，导致调度歧义；只有需要显式覆盖深度时才同时使用。

---

### 4.2 `tl.PipelinePlanning`：规划 stage 与 order

#### 4.2.1 概念说明

`PipelinePlanning` 是规划 pass。它的输入是一个带 `num_stages` 注解的循环，输出是**同一个循环，但每条可调度语句都被打上了 `software_pipeline_stage` / `software_pipeline_order` 注解**，外加一些异步生产者标记。换言之，它把「我要深度 3 的流水线」这个高层意图，翻译成「copy A 在 stage 0 order 1、gemm 在 stage 3 order 0」这样的具体调度表。

它还要做一件重要的事：**识别哪些语句是异步拷贝的生产者**，并标注出来，供下一个 pass 插入 `commit`/`wait`。

#### 4.2.2 核心流程

`PipelinePlanning` 对每个带 `num_stages` 的循环执行：

```text
1. 展平循环体，剔除「可重放的标量 Bind」（replayable scalar bind）
2. 为每条语句建 PipelineStageInfo：分析它的读/写区域、是否条件执行、是否是 copy
3. 传播 copy 的生产者依赖（PropagateBufferProducersForCopy）
4. 算每个 copy 的「最后使用者」(AnalyzeCopyLastUse)
5. 按规则分配 stage / order
6. 若 target 支持异步拷贝，发射隐式异步注解（EmitImplicitAsyncAnnotations）
7. 把 stage/order 写回循环注解
```

**stage 分配规则**（关键直觉）：消费者（gemm）分到 `stage = num_stages`；为它喂数据的 copy 分到 `stage = 0`，并且被排到 gemm **之后**发射（`order` 更大）——这正是「下一轮的 copy 提前到本轮 compute 之后」的体现。

#### 4.2.3 源码精读

pass 的入口在 [src/transform/pipeline_planning.cc:1334-1348](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/pipeline_planning.cc#L1334-L1348)。注意 metax 分支的关键改动——**MACA 强制关闭异步拷贝**：

```cpp
tvm::transform::Pass PipelinePlanning() {
  auto pass_func = [=](PrimFunc f, const IRModule &m, PassContext ctx) {
    bool use_async_copy =
        ctx->GetConfig<Bool>("tirx.use_async_copy", Bool(true)).value();
    auto target = f->GetAttr<Target>(tvm::attr::kTarget);
    if (TargetIsMaca(target.value())) {
      use_async_copy = false;          // ← MACA 不走 cp.async 流水
    }
    ...
    fptr->body = PipelinePlanner::Substitute(f, use_async_copy);
    return f;
  };
  return CreatePrimFuncPass(pass_func, 0, "tl.PipelinePlanning", {});
}
```

这条 `use_async_copy = false` 决定了 MACA 流水线的命运（见 4.3.4）。

每条语句的分析信息收集在 `PipelineStageInfo` 结构里（[pipeline_planning.cc:398-417](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/pipeline_planning.cc#L398-L417)），其中 `copy_stage`、`tma_copy`、`last_use_stmt_index` 是后续调度的依据：

```cpp
struct PipelineStageInfo {
  Array<BufferRegion> reads, writes;
  int order = -1, stage = -1;
  bool copy_stage = false;
  bool tma_copy = false;              // true = 用 TMA 而非 cp.async
  int last_use_stmt_index = -1;       // 谁最后消费了我的输出
  ...
};
```

**stage/order 分配**在 [pipeline_planning.cc:1196-1222](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/pipeline_planning.cc#L1196-L1222)。消费者拿到 `stage = num_stages`，喂数据的 copy 被放到 `stage = 0` 且排在它服务的消费者之后发射：

```cpp
for (auto &pinfo : pipeline_stage_infos) {
  if (pinfo.IsFirstStage() && pinfo.IsLastUseStmtIndexValid()) continue; // copy 稍后处理
  pinfo.order = order_idx++;
  pinfo.stage = num_stages;                        // 消费者：stage = num_stages
  for (auto &pinfo_1 : pipeline_stage_infos) {
    if (pinfo_1.IsFirstStage() &&
        pinfo_1.last_use_stmt_index == pinfo.original_stmt_index) {
      pinfo_1.order = order_idx++;                 // copy：排到消费者之后
      pinfo_1.stage = 0;                           // copy：stage 0
    }
  }
}
```

对 GEMM（`num_stages=3`，两条 copy + 一条 gemm），结果是：`gemm → stage 3, order 0`；`copy A → stage 0, order 1`；`copy B → stage 0, order 2`。

随后还有一个小优化（[pipeline_planning.cc:1232-1255](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/pipeline_planning.cc#L1232-L1255)）：若所有 copy 都被排到了序列末尾，就把它们整体「折」回开头，并把非 copy 语句的 stage 减 1，使调度更紧凑。

**异步生产者注解的发射**在 `EmitImplicitAsyncAnnotations`（[pipeline_planning.cc:835-904](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/pipeline_planning.cc#L835-L904)）。它先检查前置条件：

```cpp
if (!TargetHasAsyncCopy(target_) || !use_async_copy_) {
  return false;                       // MACA / 不支持异步拷贝 → 直接返回
}
```

只有支持异步拷贝的 target（CUDA/HIP 部分型号）才会把 copy 语句标成异步生产者，并按 `(stage, last_use_stmt_index)` 分配 `async_group_id`，写入 `software_pipeline_async_stages` 等注解。

#### 4.2.4 代码实践

**实践目标**：确认 stage/order 的分配结果。

1. 阅读上面的分配循环，对 GEMM（2 条 copy + 1 条 gemm，`num_stages=3`）手算每条语句的 `stage`/`order`。
2. 操作步骤：在纸上列出 `pipeline_stage_infos` 的 `original_stmt_index`，逐条套用规则。
3. **需要观察的现象**：copy 的 `order` 比 gemm 大（copy 排在 gemm 之后发射），但 copy 的 `stage=0` 比 gemm 的 `stage=3` 小。
4. **预期结果**：`gemm(stage=3,order=0)`、`copyA(stage=0,order=1)`、`copyB(stage=0,order=2)`。
5. 进一步思考：为什么 copy 的 `stage` 小、`order` 大，二者并不矛盾？（提示：stage 决定逻辑迭代偏移，order 决定同一轮里的发射先后。）

#### 4.2.5 小练习与答案

**练习 1**：`PipelineStageInfo::last_use_stmt_index` 有什么用？

> **答案**：它记录「这份 copy 产出的数据，最后被第几条语句消费」。分配 stage 时，copy 会依附到它的「最后消费者」身边被调度（见分配循环里的 `last_use_stmt_index == pinfo.original_stmt_index` 判断），从而让 copy 尽量贴近消费者，减少不必要的缓冲版本。

**练习 2**：为什么 MACA 要把 `use_async_copy` 关掉？

> **答案**：MACA 的异步拷贝通路（`memcpy_async` + barrier 句柄）与 CUDA 的 `cp.async` 在语义和可用性上不同，metax 分支当前没有把异步拷贝接入流水线的 commit/wait 机制（见 4.3），因此规划阶段就不发射异步生产者注解，避免下游 pass 生成不兼容的 `cp.async`/barrier 代码。

---

### 4.3 异步拷贝机制：`cp.async` 与 TMA

#### 4.3.1 概念说明

「搬运」要和「计算」重叠，光有多份缓冲还不够——**搬运本身必须是非阻塞的**。如果 `copy` 是一条会卡住线程的同步指令，那么即使有 3 份缓冲，线程在搬数据时依然干不了别的。

GPU 提供了两类异步搬运指令：

- **`cp.async`（Ampere+）**：异步把 global 数据搬到 shared，不阻塞线程。线程可以继续发计算指令。配套有 `cp.async.commit_group`（把若干次 cp.async 编成一组）和 `cp.async.wait_group N`（等到在飞的组数 ≤ N），用来在真正读 shared 之前确保数据就位。
- **TMA（Hopper）**：更强力的张量搬运单元，用 **mbarrier**（见 4.4）做同步，而非 commit/wait。

TileLang 的策略是：**普通的 `T.copy` 在流水线里会被自动改写成异步形式**——CUDA 走 `cp.async`，Hopper（且满足条件）走 TMA。用户通常不需要手写异步。

#### 4.3.2 核心流程

异步拷贝在编译流水线里的生命周期：

```text
PipelinePlanning:
  标注哪些 copy 是「异步生产者」(software_pipeline_async_stages)
        │
        ▼
InjectSoftwarePipeline:
  稳态循环里，生产者语句包进 async_commit_queue_scope
  消费者语句前插 async_wait_queue_scope(inflight=N)
        │
        ▼
LowerAsyncCommitWaitAttrs (inject_pipeline.cc 内):
  async_commit_queue_scope  → ptx_commit_group()
  async_wait_queue_scope    → ptx_wait_group(N)
```

关键在于 `wait` 的「等待数 N」：消费者读 shared 前，要保证「它要读的那份数据已经搬完」。N 通常取 `num_stages - 1` 左右——即允许最多 `num_stages-1` 组异步拷贝在飞，再老的就必须等。

#### 4.3.3 源码精读

DSL 层提供了几个相关原语。最常用的是普通 `copy`（会被流水线自动异步化），也有显式的 `async_copy`（强制走 `cp.async`）和 `tma_copy`（强制走 TMA）。

`async_copy` 的文档写明了它发射的指令序列（[tilelang/language/copy_op.py:189-230](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py#L189-L230)）：

```python
def async_copy(src, dst, *, coalesced_width=None, annotations=None, loop_layout=None):
    """Asynchronous copy primitive lowered through cp.async.
    ...
    The backend enforces cp.async constraints and emits:
      `ptx_cp_async(...)` + `ptx_commit_group()`.
    No wait is auto-inserted for `T.async_copy`; synchronization is explicit.
    """
```

注意最后一句：`async_copy` **不自动插 wait**，同步要用户自己管。而流水线场景下，`InjectSoftwarePipeline` 会替你管好 commit/wait。

`tma_copy` 走的是 mbarrier 同步（[copy_op.py:233-309](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py#L233-L309)），它只发射「生产者那一半」（`expect_tx + tma_load`），wait 由用户用 `T.mbarrier_wait_parity()` 显式做：

```python
def tma_copy(src, dst, *, barrier=None, leader_scope_threads=None, ...):
    """TMA copy with user-managed synchronization.
    For loads (global -> shared): issues expect_tx + tma_load (no wait).
    ...
    The user must wait on the same barrier via T.mbarrier_wait_parity().
    """
```

「是否支持异步拷贝」的判定统一收口在 [src/backend/common/target_utils.cc:15-26](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/backend/common/target_utils.cc#L15-L26)，按 target 分发：

```cpp
bool TargetHasAsyncCopy(Target target) {
  if (TargetIsCuda(target))  return TargetCudaHasAsyncCopy(target);
  if (TargetIsRocm(target))  return TargetRocmHasAsyncCopy(target);
  if (TargetIsMaca(target))  return TargetMacaHasAsyncCopy(target);
  return false;
}
```

`commit`/`wait` 注解最终如何落地成 PTX 指令，由 `AsyncCommitWaitAttrLowerer` 负责（[inject_pipeline.cc:346-385](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L346-L385)）：

```cpp
if (op->attr_key == s_tir::attr::async_commit_queue_scope) {
  Stmt commit = Evaluate(Call(..., builtin::ptx_commit_group(), {}));  // → cp.async.commit_group
  return SeqStmt({body, commit});
}
if (op->attr_key == s_tir::attr::async_wait_queue_scope) {
  Stmt wait = Evaluate(Call(..., builtin::ptx_wait_group(), {wait_attrs.second})); // → cp.async.wait_group N
  return SeqStmt({wait, body});
}
```

#### 4.3.4 metax 分支特写：MACA 的异步拷贝现状

虽然 MACA 在 `TargetHasAsyncCopy` 里有自己的分支（`TargetMacaHasAsyncCopy`），DSL 也提供了 `maca_async_copy`（基于 `memcpy_async` + barrier 句柄，[copy_op.py:496-540](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py#L496-L540)），但 **`T.Pipelined` 流水线当前并不为 MACA 启用异步拷贝**。原因有二：

1. `PipelinePlanning` 对 MACA 强制 `use_async_copy = false`（4.2.3），所以 `EmitImplicitAsyncAnnotations` 直接返回，不标异步生产者。
2. 即便支持，`InjectSoftwarePipeline` 的 commit/wait/mbarrier 句柄管理与 MACA 的 `memcpy_async` 语义尚未完全对接。

**后果**：MACA 上 `T.Pipelined` 仍然会做多版本化、prologue/steady/epilogue 拆分（这些与异步无关），但循环里的 copy 是**同步**的——搬运和计算的重叠程度弱于 CUDA。这是 metax 分支相对上游的一个已知差异，也是后续可优化的方向。

#### 4.3.5 代码实践

**实践目标**：在生成的 CUDA 源码里找到 `cp.async` 的痕迹。

1. 用 [example_gemm.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py)（默认 `num_stages=3`）编译一个 CUDA kernel，打印 `kernel.get_kernel_source()`。
2. 在源码里搜索 `cp.async`、`cp.async.commit_group`、`cp.async.wait_group`。
3. **需要观察的现象**：稳态循环里，能看到 `cp.async` 发射拷贝、`commit_group` 编组、`wait_group N` 在 gemm 读 shared 之前等待。
4. **预期结果**：能定位到 `wait_group` 语句，且其等待数与 `num_stages` 相关（增大 `num_stages`，允许在飞的拷贝组数也应增大）。待本地验证。
5. 若用 `target={"kind":"maca"}` 编译（无设备时只取源码），对比应发现 MACA 源码里**没有** `cp.async`，而是普通同步拷贝。

#### 4.3.6 小练习与答案

**练习 1**：`cp.async.wait_group N` 里的 N 越大越好吗？

> **答案**：不是。N 是「允许在飞的、尚未完成的拷贝组数上限」。N 越大，等待越松（性能可能更好），但需要更多缓冲版本（更多 shared memory）；N 过小则等待过紧，可能让消费者空等。它通常与 `num_stages` 联动，由 `InjectSoftwarePipeline` 计算合适值。

**练习 2**：`T.async_copy` 和流水线里自动异步化的 `T.copy` 有何区别？

> **答案**：`T.async_copy` 是**显式**异步原语，强制走 `cp.async` 且**不自动插 wait**，适合需要手动精细控制同步的场景。流水线里的 `T.copy` 是**隐式**异步化——由 `InjectSoftwarePipeline` 自动加 commit/wait、做多版本化。日常 GEMM 用 `T.copy` + `T.Pipelined` 即可，无需手写 `async_copy`。

---

### 4.4 `tl.InjectSoftwarePipeline`：改写、多版本化与 mbarrier

#### 4.4.1 概念说明

`InjectSoftwarePipeline` 是改写 pass。它读入 `PipelinePlanning` 产出的 stage/order 注解，把**单个循环**重写成三段：

- **prologue（预热）**：先发若干轮 copy，把管道「灌满」。
- **steady（稳态）**：copy 和 compute 完全重叠，是性能主力。
- **epilogue（收尾）**：管道里残留的最后几轮 compute 排空。

同时它做三件关键杂活：

1. **多版本化**：把 shared buffer 复制成 N 份，用 `floormod` 下标选当前版本，避免生产者和消费者踩同一块内存。
2. **commit/wait 插入**：为异步生产者插 `commit`，为消费者插 `wait`。
3. **mbarrier 管理**：对 TMA 路径，创建并复制屏障缓冲，插 `mbarrier_wait_parity`。

#### 4.4.2 核心流程

改写的总入口是 `PipelineInjector::Inject`（[inject_pipeline.cc:3073-3084](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L3073-L3084)），对每个带 stage/order 注解的 `for` 循环：

```text
1. 校验 stage/order 合法性（唯一性、依赖不破坏）
2. 若无重叠阶段 → 退化为普通循环（不值得流水化）
3. TMA 路径：RewritePipelineTmaBarriers（把 copy 改写成 tma_copy + 共享屏障）
4. ExpandPipelineBarriers：把所有相关屏障缓冲扩成 num_stages 份
5. RewritePipeline：
   a. 算每个 buffer 的版本数（ComputeBufferVersions，liveness 分析）
   b. 多版本化分配（RewriteAllocBuffer）
   c. 发射 prologue / steady / epilogue（EmitImpl）
   d. 插 commit/wait、放松尾部 wait
6. LowerAsyncCommitWaitAttrs：commit/wait 注解 → PTX 指令
```

**三段如何切**：设原循环为 `[min, min+extent)`，最大阶段为 `max_stage`，则

\[
\text{prologue} = [min,\; min + max\_stage),\quad
\text{steady} = [min + max\_stage,\; min + extent),\quad
\text{epilogue} = [min + extent,\; min + extent + max\_stage)
\]

每个语句在逻辑迭代 `loop_var` 上访问的「真实迭代」是 `loop_var - stage`（称为 skewed loop var）——stage 越大，消费得越「旧」的迭代。

**版本选择**：多版本化后，buffer 第 0 维是版本号，访问时用

\[
\text{version} = \text{floormod}(\,loop\_var - loop\_min,\; num\_versions\,)
\]

来选当前迭代对应的那一份。版本数由活跃性分析得到（[inject_pipeline.cc:1472-1527](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L1472-L1527)）：

\[
num\_versions = use - def + 1 \quad (\text{再按重排情况减一})
\]

对标准 copy→gemm 的 GEMM，最终版本数恰为 `num_stages`。

#### 4.4.3 源码精读

三段发射在 `PipelineRewriter::BuildPipeline`（[inject_pipeline.cc:1223-1230](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L1223-L1230)），就是上面那三个区间：

```cpp
Stmt prologue = EmitImpl(pipeline_loop_->min,
                         pipeline_loop_->min + max_stage_, true, true);
Stmt body     = EmitImpl(pipeline_loop_->min + max_stage_,
                         pipeline_loop_->min + pipeline_loop_->extent, false, false);
Stmt epilogue = EmitImpl(pipeline_loop_->min + pipeline_loop_->extent,
                         pipeline_loop_->min + pipeline_loop_->extent + max_stage_, true, true);
```

`EmitImpl` 内部对每条语句计算 `skewed_loop_var = new_loop_var - stage` 并加边界守卫（[inject_pipeline.cc:2889-2897](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L2889-L2897)）：

```cpp
PrimExpr skewed_loop_var = new_loop_var - stage;
if (need_bound_check)
  inbound = And(pipeline_loop_->min <= skewed_loop_var,
                (skewed_loop_var < pipeline_loop_->min + pipeline_loop_->extent));
```

这正是「prologue 里 compute 因 skewed 越界而被守卫掉、epilogue 里 copy 被守卫掉」的实现机制。

多版本化的下标重写在 `PipelineBodyRewriter`（[inject_pipeline.cc:1099-1127](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L1099-L1127)），给每次 buffer 读写插入版本下标：

```cpp
PrimExpr version = floormod(
    (pipeline_loop_->loop_var - pipeline_loop_->min), new_buffer->shape[0]);
n->indices.insert(n->indices.begin(), version);   // 第 0 维 = 版本号
```

#### 4.4.4 mbarrier 同步：生产者—消费者握手

**mbarrier** 是 Hopper 引入的 64 位共享内存屏障，配合 TMA 使用。它的握手模型是：

- **生产者**（TMA load）：先 `mbarrier.expect_tx byte_count`（告诉屏障「我马上要写这么多字节」），TMA 硬件搬完数据后自动抵达屏障、扣减计数。
- **消费者**：执行 `mbarrier.wait_parity P`，等到屏障的奇偶位翻成 `P`，表示数据已就位。

**parity（奇偶）** 是关键：屏障每被「完全抵达」一次，奇偶就翻转。所以消费者要等第 `k` 轮的数据，就等 parity 等于 `k` 的奇偶。在流水线里，第 `iter` 次迭代的 parity 用

\[
parity = \text{floormod}\!\left(\text{floordiv}(iter,\; num\_stages),\; 2\right)
\]

来计算（见 [inject_pipeline.cc:974](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L974) 与 [inject_pipeline.cc:822-824](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L822-L824)）：

```cpp
PrimExpr ns = IntImm(DataType::Int(32), num_stages);
PrimExpr parity = FloorMod(FloorDiv(loop_var - loop_min, ns), 2);
```

**为什么屏障要复制成 `num_stages` 份**？因为流水线里同时有 `num_stages` 轮 TMA 在飞，它们各自抵达同一个屏障会乱套。`ExpandPipelineBarriers`（[inject_pipeline.cc:769-884](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L769-L884)）把屏障缓冲从 `[N]` 扩成 `[N * num_stages]`，并给每次访问加上 `stage` 偏移（[inject_pipeline.cc:822-823](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L822-L823)）：

```cpp
PrimExpr stage_expr = FloorMod(loop_var - loop_min, ns);
// 访问下标改写为：stage_expr * old_size + 原下标
```

而 `RewritePipelineTmaBarriers`（[inject_pipeline.cc:906-994](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L906-L994)）负责把流水线里的普通 `tl.tileop.copy` 改写成 `tl.tileop.tma_copy`（挂上共享屏障），并在第一个消费阶段前插 `mbarrier_wait_parity`（[inject_pipeline.cc:969-977](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L969-L977)）：

```cpp
PrimExpr barrier_ref = MakeBarrierRef(barrier_buf, IntImm(DataType::Int(32), 0));
PrimExpr parity = FloorMod(FloorDiv(loop_var - loop_min, ns), 2);
wait_stmts.push_back(Evaluate(Call(
    DataType::Handle(), mbarrier_wait_parity(), {barrier_ref, parity})));
```

> 提醒：mbarrier/TMA 路径是 **Hopper（sm_90+）专属**，CUDA Ampere 走的是 `cp.async` + commit/wait，MACA 则两者都不走（同步拷贝）。

pass 的最终注册在 [inject_pipeline.cc:4020-4029](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L4020-L4029)，名为 `tl.InjectSoftwarePipeline`，结尾还跑一遍 `ConvertSSA` 清理变量。

#### 4.4.5 代码实践

**实践目标**：在生成的 CUDA 源码里识别 prologue/steady/epilogue 三段与多版本化。

1. 用 `num_stages=3` 的 GEMM 编译，打印 `kernel.get_kernel_source()`。
2. 在稳态循环里找 shared buffer 的访问下标，应能看到形如 `[(k % 3), i, j]` 的版本下标（`k % 3` 即 `floormod` 选版本）。
3. **需要观察的现象**：
   - prologue 段只有 copy（compute 因越界守卫被裁掉）；
   - 稳态段 copy 和 gemm 都在；
   - epilogue 段只有 gemm（copy 被裁掉）。
4. **预期结果**：能数出 shared buffer 被复制成了 3 份（版本维 = 3）。待本地验证。
5. 若把 `num_stages` 改成 2，版本维应变 2，prologue/epilogue 长度也相应缩短。

#### 4.4.6 小练习与答案

**练习 1**：为什么 prologue 段里 gemm 不会执行？

> **答案**：prologue 的循环范围是 `[min, min+max_stage)`。gemm 的 `stage = num_stages = max_stage`，其 `skewed_loop_var = loop_var - max_stage`，在 prologue 区间内为负值，落在 `[min, min+extent)` 之外，被 `inbound` 守卫裁掉。所以 prologue 只发 copy，把管道灌满。

**练习 2**：`ExpandPipelineBarriers` 为什么要根据「是否有显式 `ptx_arrive_barrier` 调用」来决定是否扩展某个屏障？

> **答案**：见 [inject_pipeline.cc:791-818](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L791-L818)。用户显式 `ptx_arrive_barrier` 的屏障是「用户自管同步」，需要按 stage 分槽才能流水化；而由 tile-op（如 tcgen05 MMA）内部自管的屏障，其抵达由算子自己负责，若也扩展反而会破坏其内部同步，故不扩展。

---

## 5. 综合实践：调参 `num_stages` 并解释重叠

把本讲知识串起来，做一个完整的小任务。

**任务**：基于 [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py)，系统观察 `num_stages` 对性能与生成代码的影响。

**操作步骤**：

1. 固定 `M=N=K=1024`、`block_M=block_N=128`、`block_K=32`，把第 19 行的 `num_stages` 依次设为 **1、2、3、4**，分别编译。
2. 对每个值，用 `kernel.get_profiler().do_bench(backend="cupti")` 测延迟，记录成表（无 GPU 则跳过计时，只做源码分析）。
3. 对每个值，打印 `kernel.get_kernel_source()`，重点看三处：
   - shared buffer 的版本维（`floormod` 里的模数）；
   - prologue / epilogue 的长度；
   - `cp.async` 与 `wait_group` 的等待数。
4. 画出每个 `num_stages` 下「copy 与 compute 的时间重叠」示意（横轴为迭代，纵轴为语句）。

**需要观察与解释的现象**：

| `num_stages` | 版本维 | prologue/epilogue 长度 | copy/compute 重叠 | shared 内存占用 |
|---|---|---|---|---|
| 1 | 1 | 1 | 几乎无重叠 | 最省 |
| 2 | 2 | 2 | 经典双缓冲 | 中等 |
| 3 | 3 | 3 | 较深重叠 | 较大 |
| 4 | 4 | 4 | 更深重叠 | 最大 |

**预期结论**：

- `num_stages` 增大 → 重叠更深 → 延迟一般下降；但 shared 内存占用也线性增长，过大会导致 occupancy（占用率）下降，反而变慢。
- 存在一个「甜点」`num_stages`，通常是 2~4 之间，取决于 block 大小与 GPU 的 shared 内存容量。**待本地验证**具体最优值。
- 对 MACA target，由于异步拷贝未启用（4.3.4），增大 `num_stages` 带来的重叠收益弱于 CUDA——这是 metax 分支的一个可观察差异。

> 提示：如果你实现了自己的 kernel（如 elementwise 或 FlashAttention），同样的 `num_stages` 调参方法同样适用；只要循环体是「copy + compute」结构，流水线就能生效。

---

## 6. 本讲小结

- `T.Pipelined(num_stages=N)` 是软件流水线的推荐入口；`num_stages=0` 不流水，`N≥1` 表示 copy 与 compute 之间保留 N 份缓冲。它在前端只是落成带 `num_stages` 注解的 `for` 循环。
- `tl.PipelinePlanning` 是**规划** pass：分析每条语句的读写，把消费者分到 `stage=num_stages`、把喂数据的 copy 分到 `stage=0` 并排到消费者之后，再标注异步生产者。**metax 分支在此对 MACA 关闭异步拷贝**。
- `tl.InjectSoftwarePipeline` 是**改写** pass：把单循环拆成 prologue/steady/epilogue，用 `floormod` 做 shared buffer 多版本化（版本数经活跃性分析得出，标准 GEMM 下等于 `num_stages`），并为异步生产者插 commit/wait。
- 异步搬运有两条路：CUDA Ampere 走 `cp.async` + `commit_group`/`wait_group`；Hopper 走 TMA + mbarrier。MACA 当前在流水线里**不走异步拷贝**，使用同步拷贝。
- mbarrier 用「expect_tx → 自动抵达 → wait_parity」握手；`parity = floormod(floordiv(iter, num_stages), 2)`；屏障被 `ExpandPipelineBarriers` 复制成 `num_stages` 份、按下标分槽，避免多轮 TMA 互相干扰。
- 两个 pass 在 MACA 的 pass 流水线里注册于 [tilelang/maca/pipeline.py:45-46](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/pipeline.py#L45-L46)，紧接 `MaterializeKernelLaunch`、先于 `LayoutInference`。

---

## 7. 下一步学习建议

- **往深处**：`InjectSoftwarePipeline` 里关于 `wait` 放松（`RelaxTrailingConsumerWaits`、`AsyncPipelineLoopWaitRelaxer`）的逻辑相当精巧，建议结合生成的 PTX 阅读这部分优化如何减少不必要的等待。
- **往广处**：阅读 [src/transform/inject_pipeline.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc) 中 TMA 屏障管理（`RewritePipelineTmaBarriers`）与 cluster copy 的联动，衔接 u8-l2（swizzle/persistent/splitk）。
- **MACA 方向**：若你关心 metax 分支的核心差异，可顺着 u7-l4（MACA 编译流水线）看 `LowerMACAIntrin` 如何处理 `memcpy_async`，以及未来如何把 MACA 的异步拷贝接入 `InjectSoftwarePipeline`。
- **实践方向**：把本讲的 `num_stages` 调参方法应用到 FlashAttention（u8-l4）上，体会流水线对在线 softmax 这种「多段 copy+compute」kernel 的收益。
