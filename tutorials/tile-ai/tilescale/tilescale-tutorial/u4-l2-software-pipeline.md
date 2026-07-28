# 软件流水线与异步拷贝

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚「软件流水线（software pipeline）」要解决什么问题，为什么它能隐藏 GPU 的访存延迟。
- 把业务代码里的 `for ko in T.Pipelined(..., num_stages=N)` 这一行，与编译器里多道 pass 的协作对应起来：前端写 `num_stages` 注解 → `PipelinePlanning` 规划 → `InjectSoftwarePipeline` 落地多缓冲 → `InjectPTXAsyncCopy` / `InjectTmaBarrier` 注入异步指令。
- 理解「多缓冲（multi-buffering）/ 轮转缓冲」是如何通过给 shared memory tile 复制多份、再用 `floormod` 选版本来实现的，并能估算 `num_stages` 增大时的 shared memory 与寄存器代价。
- 区分两条异步搬运路径：非 warp 特化路径下的 `cp.async`（Ampere sm_80+），以及 warp 特化 + TMA 路径下的 `mbarrier` 机制（Hopper sm_90+）。
- 自己动手改 `num_stages`、测延迟、读生成的 CUDA 源码，定性地解释「收益 vs 共享内存/寄存器代价」的权衡。

本讲承接 [u3-l4](u3-l4-optimize-target.md)：`OptimizeForTarget` 的「流水与 warp 特化段」是整段 pass 里唯一带分支的部分，本讲把那一段里和软件流水相关的 pass 逐道拆开讲透。

## 2. 前置知识

本讲假设你已经掌握下面这些概念（前序讲义已建立）：

- **GPU 显存层级**：`global → shared → fragment(local/register)`。一次 GEMM 的典型数据流是：从 global 把 tile 搬到 shared，再在 shared 上做矩阵乘、累加到 fragment。详见 [u2-l2](u2-l2-tile-alloc.md)。
- **`T.Pipelined` 是 K 维循环的骨架**：在 `for ko in T.Pipelined(...)` 内部，通常先 `T.copy`（生产者，访存密集），再 `T.gemm`（消费者，计算密集）。详见 [u2-l4](u2-l4-loops-control-flow.md)、[u1-l3](u1-l3-quickstart.md)。
- **`OptimizeForTarget` 的整体结构**：流水线与 warp 特化是「带分支的第一段」，缓冲区整形与 codegen 收尾是线性段。详见 [u3-l4](u3-l4-optimize-target.md)。
- **pass 配置（pass config）**：一组可以开关某项优化的键值，形如 `{"tl.disable_tma_lower": True}`，通过 `@tilelang.jit(..., pass_configs=...)` 传入。

下面三个直觉概念是本讲的基础，先用大白话讲清楚。

### 2.1 为什么要「流水线」

一个朴素的 K 维循环长这样：

```
for ko in range(K_tiles):
    shared = load(global[ko])   # 生产者：访存，SM 大量空转等显存
    C += gemm(shared)           # 消费者：计算，用 tensor core
```

每一轮里，`load` 和 `gemm` 是**串行**的——必须等显存搬完才能开算。显存延迟通常上百到上千个周期，这段时间 tensor core 完全闲置。**软件流水线**的核心想法是：不要「搬一块、算一块」，而是「**提前搬后面几块，让搬运和计算重叠**」。就像工厂流水线：当第 1 块在算的时候，第 2、3 块已经在搬了。

### 2.2 多缓冲（double / multi buffering）

要让搬运和计算重叠，shared memory 里的 tile 不能只有一份——否则「在算第 1 块」和「在搬第 2 块」会写同一块内存。解决办法是给 tile 准备**多份副本**，用「轮转（round-robin）」方式选当前用哪一份：

\[ \text{slot}(i) = i \bmod \text{num\_versions} \]

这样生产者写「未来的」槽，消费者读「当前的」槽，互不干扰。代价是：副本越多，shared memory 占用越大。

### 2.3 同步拷贝 vs 异步拷贝

- **同步拷贝**：线程发出 load，要等数据到了寄存器才能继续，访存延迟完全暴露。
- **异步拷贝**：线程发出一条「非阻塞」搬运指令，**不等**它完成就继续干别的活，等真正要用数据时再去「等（wait）」。
  - Ampere（sm_80+）有 `cp.async` 指令：global → shared 的异步搬运，配合 `cp.async.commit_group` / `cp.async.wait_group` 做同步。
  - Hopper（sm_90+）有 **TMA（Tensor Memory Accelerator）**：一条指令搬运一整块多维 tile，搬运完成时自动通知一个 `mbarrier`（共享内存屏障）。

本讲要讲清的，就是 TileLang 编译器如何把 `T.Pipelined` 这行业务代码，**自动**变成「多缓冲 + 异步拷贝 + 同步插入」的真实硬件指令——而业务代码里你只需要写一个 `num_stages`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/language/pipeline.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/pipeline.py) | `T.Pipelined` 的 Python 入口，把 `num_stages` 等参数转发给 FFI。 |
| [src/ir.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/ir.cc) | C++ 侧 `PipelinedFor` 构造 For 循环，把 `num_stages` 写成循环注解。 |
| [src/transform/pipeline_planning.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/pipeline_planning.cc) | **`PipelinePlanning`**：读 `num_stages`，分析循环体每条语句的读写，规划出 `stage`/`order`/`async` 注解。 |
| [src/transform/inject_pipeline.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_pipeline.cc) | **`InjectSoftwarePipeline`**：读注解，做多缓冲（给 buffer 加版本维），把循环改写成 prologue / body / epilogue 三段，插入异步 commit/wait 占位。 |
| [src/transform/inject_ptx_async_copy.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_ptx_async_copy.cc) | **`InjectPTXAsyncCopy`**（非 warp 特化路径）：把 async 作用域内的 global→shared 拷贝替换成 `cp.async` 指令。 |
| [src/transform/inject_tma_barrier.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_tma_barrier.cc) | **`InjectTmaBarrier`**（warp 特化 + TMA 路径）：给 TMA 搬运配 `mbarrier` 的 `expect_tx`/`arrive`/`wait`。 |
| [tilelang/engine/phase.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py) | `OptimizeForTarget`：决定上述 pass 的执行顺序与分支条件。 |
| [examples/quickstart.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py) | 本讲实践使用的 matmul+relu 标本。 |

本讲的主线是下面这条改写链（注意 `num_stages` 注解是如何一步步变成真实指令的）：

```
T.Pipelined(num_stages=N)
        │  前端：写 "num_stages" 注解到 For 节点（src/ir.cc）
        ▼
 PipelinePlanning      ──►  写 software_pipeline_stage / order / async_stages 注解
        ▼
 InjectSoftwarePipeline ──►  多缓冲 + prologue/body/epilogue + async_scope/commit/wait
        ▼
 InjectPTXAsyncCopy        （非 WS 路径，sm_80+）
        或
 InjectTmaBarrier          （WS + TMA 路径，sm_90+）
        ▼
   真实 cp.async / TMA+mbarrier 指令
```

## 4. 核心概念与源码讲解

### 4.1 从 T.Pipelined 到 num_stages 注解

#### 4.1.1 概念说明

业务代码里写 `for ko in T.Pipelined(T.ceildiv(K, block_K), num_stages=3)`，意思是「请把这个 K 维循环变成 3 级软件流水」。但「软件流水」并不是一个硬件概念，它是一个**编译期约定**——前端只负责把「这里要做流水、级数是 3」这个意图，以**注解（annotation）**的形式挂到 TIR 的 `For` 节点上，真正「怎么排」交给后面几道 pass。

关键认知：`num_stages` 在前端阶段还**不是**任何指令，它只是挂在循环上的一个整数标记，等待 `PipelinePlanning` 读取。到此为止，循环体里依然是 `copy → gemm` 的原始顺序，改写尚未发生。

#### 4.1.2 核心流程

1. Python `T.Pipelined(start, stop, num_stages, order, stage, sync, group)` 把参数打包，转发给 FFI。
2. C++ `PipelinedFor` 构造一个普通的 `Serial` for 循环；当 `num_stages > 0` 时，把 `num_stages` 作为名为 `"num_stages"` 的注解挂到循环上；若用户显式提供了 `order`/`stage`（warp 特化场景预留），则写成 `tl_pipeline_order` / `tl_pipeline_stage`。
3. 这个挂了注解的循环进入 `OptimizeForTarget`，被 `PipelinePlanning` 识别。

#### 4.1.3 源码精读

Python 入口只是参数转发，`num_stages` 的含义在文档串里讲得很清楚——「生产者与消费者之间最多使用的缓冲数，为 0 则不启用流水」：

[ tilelang/language/pipeline.py:10-47 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/pipeline.py#L10-L47) — `T.Pipelined` 的签名与 `num_stages` 语义，最后 `return _ffi_api.Pipelined(...)` 转发到 C++。

真正构造循环、写注解的是 C++ 的 `PipelinedFor`：

[ src/ir.cc:92-115 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/ir.cc#L92-L115) — 当 `num_stages > 0` 时 `anno.Set("num_stages", PrimExpr(num_stages))`（L109-110），同时把可选的 `order`/`stages` 写成 `tl_pipeline_order`（L112）/`tl_pipeline_stage`（L114）。注意循环类型是 `ForKind::kSerial`。

要点：到此为止，「流水」还只是一条带 `num_stages` 注解的普通串行循环。

#### 4.1.4 代码实践

**目标**：亲眼看到一个 `T.Pipelined` 循环上的 `num_stages` 注解，确认前端阶段改写尚未发生。

**步骤**：

1. 复制 `examples/quickstart.py`，在文件末尾加一段用 `lower`（而非 `compile`）拿 IRModule 的代码：

```python
# 示例代码：打印流水循环的 num_stages 注解
mod = tilelang.lower(matmul_relu_kernel, target="cuda")
for _, f in mod.functions.items():
    print(f.show())
```

2. 在输出里找到那个 `for ko` 循环，观察它 `annotations` 字段里的 `"num_stages": 3`（quickstart 里写的是 `num_stages=3`）。

**需要观察的现象**：循环类型为 `serial`，注解里有 `num_stages=3`，循环体仍是 `T.copy → T.copy → T.gemm` 的原始顺序（还没被改写）。

**预期结果**：你能确认「前端的流水意图 = 一个循环注解」。如果本地拿不到 `lower` 的输出，标注为「待本地验证」。

#### 4.1.5 小练习与答案

**Q1**：把 `num_stages` 改成 0 会怎样？
**答**：`PipelinedFor` 里 `if (num_stages > 0)` 不成立，不挂 `num_stages` 注解；下游 `PipelinePlanning` 找不到该注解就直接跳过，循环保持普通串行循环，不做多缓冲、不插异步拷贝。

**Q2**：`num_stages` 是挂在循环上还是挂在循环体里的语句上？
**答**：挂在 `For` 节点的 `annotations` 上，是一个循环级别的标记。具体每条语句属于第几级 stage，是 `PipelinePlanning` 后面才算出来的。

---

### 4.2 PipelinePlanning：规划 stage / order / async

#### 4.2.1 概念说明

`PipelinePlanning` 是整条改写链里的「大脑」。它做的事用一句话讲：**「看看循环体里有哪几条语句、每条语句读写哪些 buffer，然后决定每条语句属于第几级 stage、在一次迭代里排第几个 order、哪些级是异步的」**，最后把这些决定写成 TVM 标准的软件流注解。

几个关键术语：

- **stage（级）**：语句的时间偏移。stage 越小越靠前（越早执行/越早 prefetch）。生产者（copy）通常 `stage=0`，消费者（gemm）stage 较大。
- **order（序）**：在改写后的单次迭代内，语句的执行先后。
- **copy stage（搬运级）**：被判定为「global → shared」数据搬运的语句，是天然的「第一级」生产者。
- **async stage**：被标记为异步执行的级，最终会落到 `cp.async` / TMA。

#### 4.2.2 核心流程

`PipelinePlanner::VisitStmt_(ForNode)` 的算法分几步：

1. **识别两种入口**：
   - 若循环已有 `tl_pipeline_order` / `tl_pipeline_stage`（warp 特化场景预留），把它们转写成标准注解；若 order/stage 里含 `-1`，表示 TMA+warp 特化已接管，本 pass 直接跳过。
   - 否则读 `num_stages` 注解；没有就跳过。
2. **拆解循环体**：要求循环体是 `SeqStmt`（可能被 `IfThenElse`（无 else）/`LetStmt` 包裹），把每条子语句当成一个「流水级候选」。
3. **为每条语句收集读写区域**，并判定它是不是「global → shared」的 copy stage。对 tcgen05（Blackwell）场景，`AsyncDependencyChainBuilder` 还会把 `tcgen5mma` 与其 `mbarrier` 的读写依赖串起来。
4. **传播 `producer_for_copy`**：如果某个非 copy 语句生产了某个 copy 语句要读的数据，就把这个生产者也标记为第一级，保证数据在被搬运前已就绪。
5. **算 `last_use_stmt_index`**：对每个第一级（copy / producer），向后扫描，找到「最后一个读到它产出数据」的语句下标——这决定了它该被排到哪个 order（紧跟在它最后一个消费者之后）。同时检测写后写冲突（同一 buffer region 被多个 copy 写，会直接报错）。
6. **分配 stage / order**：非第一级语句 `stage = num_stages`；第一级（copy）语句 `stage = 0`，并插到其最后一个消费者的 order 之后。
7. **「copy 全在尾部」优化**：如果所有 copy 的 order 都排在所有非 copy 之后，就把这些 order 轮转到最前面，并**把非 copy 的 stage 整体减 1**。这一步是把缓冲副本数从「上界的 `num_stages+1`」降到「`num_stages`」的关键（数学说明见 4.3.2）。
8. **写注解**：输出 `software_pipeline_stage`、`software_pipeline_order`；当目标支持异步拷贝且未禁用时，额外写 `software_pipeline_async_stages = {0}`。

#### 4.2.3 源码精读

入口与「已注解 / TMA+WS 跳过」分支：

[ src/transform/pipeline_planning.cc:359-408 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/pipeline_planning.cc#L359-L408) — 读取 `tl_pipeline_order` / `tl_pipeline_stage` / `num_stages` 三个注解；若 order/stage 数组里含 `-1` 则判定为 TMA+warp 特化已接管，本 pass 直接 `return`（L384-386）。

读 `num_stages` 并要求循环体是 `SeqStmt`：

[ src/transform/pipeline_planning.cc:410-452 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/pipeline_planning.cc#L410-L452) — 取出 `num_stages`，`CHECK(num_stages >= 1)` 且 `CHECK(loop->kind == ForKind::kSerial)`；逐层穿透 `IfThenElse` / `LetStmt` 找到最内层的 `SeqStmt`，否则 `LOG(FATAL)`。

判定一条语句是不是「global → shared」的搬运：

[ src/transform/pipeline_planning.cc:152-204 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/pipeline_planning.cc#L152-L204) — `BufferRegionCollector` 在 `BufferStore` 的值里若读到 `global` buffer（且不在 `if_then_else` 的条件里），且写入目标是 `shared` / `shared.dyn`，就置 `is_global_copy_pattern_ = true`，即这条语句是 copy stage。

`last_use_stmt_index` 的分析与写后写冲突检测：

[ src/transform/pipeline_planning.cc:564-611 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/pipeline_planning.cc#L564-L611) — 对每个第一级语句向后扫描后续语句的 reads，用 `MayConflict` 判断区域相交，取最大的下标作为 `last_use_stmt_index`；若发现多个 copy 写同一重叠 region，则 `LOG(FATAL)` 报错。

stage / order 的分配主逻辑 + 「copy 全在尾部」优化：

[ src/transform/pipeline_planning.cc:613-675 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/pipeline_planning.cc#L613-L675) — 跳过有消费者的第一级语句；给非第一级语句 `stage=num_stages`、递增 order，然后把「最后一个消费者是当前语句」的 copy 级插到它后面（`stage=0`）。紧接着 L652-675 的 `copy_stage_at_end` 优化在「copy 全在尾部」时轮转 order 并对非 copy 的 stage 减 1。

最后写出标准软件流注解 + async：

[ src/transform/pipeline_planning.cc:685-700 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/pipeline_planning.cc#L685-L700) — 把每条语句的 `order` / `stage` 收集成数组，设为 `software_pipeline_order` / `software_pipeline_stage`；`TargetHasAsyncCopy(target_)` 成立时设 `software_pipeline_async_stages = {0}`。

> 关于 `TargetHasAsyncCopy`：[ src/target/utils.cc:84-87 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/utils.cc#L84-L87) 对 CUDA 目标，当架构号 `arch >= 80`（Ampere 及以后）返回 true——这正是 `cp.async` 可用的最低架构。

pass 注册与 `use_async_copy` 开关：

[ src/transform/pipeline_planning.cc:719-734 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/pipeline_planning.cc#L719-L734) — `PipelinePlanning()` 从 pass 配置读 `tir.use_async_copy`（默认 true），把它作为 `use_async_copy` 传入，决定是否标记 async stage，并注册为 `tl.transform.PipelinePlanning`。

#### 4.2.4 代码实践

**目标**：用纸笔追踪 quickstart 的 3 条语句被规划成什么样的 stage/order，理解「copy 全在尾部」优化。

**步骤**：

1. quickstart 的循环体是 `[T.copy(A), T.copy(B), T.gemm]`，共 3 条语句，`num_stages=3`。
2. 按 4.2.3 的分配逻辑手算：
   - 两个 copy 都是 copy stage（`stage=0`），gemm 是非 copy（初始 `stage=num_stages=3`）。
   - 每个 copy 的 `last_use` 都是 gemm（gemm 是它们数据的最后读者）。
   - 分配 order：先给 gemm 一个 order，再把两个 copy 插到它后面 → 初始 order 大致为 `[gemm=0, copy_A=1, copy_B=2]`，即 copy 全在尾部。
   - 触发「copy 全在尾部」优化：轮转 order、非 copy 的 stage 减 1 → gemm 的 `stage` 变成 `num_stages-1=2`。
   - 最终：`copy_A: stage=0`，`copy_B: stage=0`，`gemm: stage=2`。
3. 在 4.1.4 打印的 IR 基础上，对照 `InjectSoftwarePipeline` 之后的输出，看是否与手算一致。

**需要观察的现象**：`software_pipeline_stage` 数组里 copy 对应位置是 0，gemm 对应位置是 `num_stages-1`。

**预期结果**：理解「为什么是 stage=num_stages-1 而不是 num_stages」——正是「copy 全在尾部」优化的功劳，它把副本数从 `num_stages+1` 省到 `num_stages`。具体数组下标「待本地验证」。

#### 4.2.5 小练习与答案

**Q1**：为什么 `PipelinePlanning` 要识别「global → shared」这个模式？
**答**：只有 global → shared 的搬运才适合用 `cp.async` / TMA 异步化，也才适合作为流水的「第一级生产者」prefetch 到未来迭代。识别出它，才能把它标成 `stage=0` + async，从而让搬运与计算重叠。

**Q2**：`software_pipeline_async_stages` 为什么默认是 `{0}`？
**答**：因为只有第一级（生产者 copy，stage=0）才需要、也才适合异步化——消费者（gemm）必须等数据搬完才能算，不能异步。

**Q3**：如果两条 copy 语句写了同一块 shared region，会发生什么？
**答**：`last_use_stmt_index` 分析里的写后写冲突检测（L592-609）会 `LOG(FATAL)`，提示「多个 copy 写入重叠 buffer region 不被支持」。

---

### 4.3 InjectSoftwarePipeline：多缓冲与 prologue/body/epilogue

#### 4.3.1 概念说明

`PipelinePlanning` 只产出「注解」，还没真正改循环。`InjectSoftwarePipeline` 才是把注解**落地**为真实流水循环的 pass。它干两件大事：

1. **多缓冲（multi-versioning）**：给「跨级使用的 buffer」（如 shared 里的 `A_shared` / `B_shared`）增加一个版本维，让它有 `num_versions` 份副本，每份对应流水的一个 slot。
2. **三段式展开**：把原来的单层循环改写成 `prologue（预热）+ body（稳态）+ epilogue（排空）` 三段，并给每条语句按 stage 做「循环偏斜（skew）」，再用 `floormod` 选版本。

异步相关：它还会给 async 级包上 `async_scope`、在合适位置插 `async_commit_queue_scope` 和 `async_wait_queue_scope` / `async_wait_inflight_count`——这些是后续 `cp.async` / TMA 注入要用的「同步占位」。

#### 4.3.2 核心流程

**(a) 缓冲副本数怎么定？**

对一个被生产者写出（def 级）、被消费者读入（use 级）的 buffer，副本数的上界是：

\[ \text{num\_versions} \le \text{use} - \text{def} + 1 \]

这是「同一时刻在飞（in-flight）的最大迭代数」的上界。代码里还有一个特例优化：若不存在「order 更小且 stage 更小、且区域相交」的真实写后读冲突，则副本数减 1。

对 4.2 手算的 quickstart：`def=0`（copy），`use=num_stages-1`（gemm），故 `num_versions = num_stages`。也就是说 **shared tile 的副本数 ≈ num_stages**——这正是「num_stages 越大、shared memory 越费」的根因。

**(b) 三段式展开与循环偏斜**

设 `max_stage = use`（gemm 的级）。原循环变量范围 \([0, N)\) 被拆成：

- **prologue**：\([0, \text{max\_stage})\)，只跑生产者（灌满流水），消费者可能还没数据，需要边界保护。
- **body**：\([\text{max\_stage}, N)\)，稳态，所有级都跑。
- **epilogue**：\([N, N+\text{max\_stage})\)，只跑消费者（排空流水），需要边界保护。

在 body 的某次新迭代 `i`，stage 为 `s` 的语句访问的是「原始循环变量」：

\[ k_{\text{orig}} = i - s + \text{max\_stage} \]

代入 quickstart（`max_stage = num_stages-1`）：

- copy（`s=0`）：\(k_{\text{orig}} = i + \text{num\_stages}-1\) —— **提前 prefetch 未来第 `num_stages-1` 轮**。
- gemm（`s=\text{num\_stages}-1`）：\(k_{\text{orig}} = i\) —— **算当前轮**。

也就是说：稳态下，每次迭代「算第 i 块」的同时「搬第 i+num_stages-1 块」，二者重叠。同一时刻在飞的数据块是 \(i, i+1, \dots, i+\text{num\_stages}-1\)，共 `num_stages` 块——与副本数一致。

**(c) 版本选择**

读写多版本 buffer 时，在最前面插入一个版本下标：

\[ \text{version} = (k_{\text{orig}} - k_{\text{min}}) \bmod \text{num\_versions} \]

这样不同迭代轮转使用不同槽位，生产者写「未来槽」、消费者读「当前槽」，互不踩踏。

**(d) 异步同步占位**

- async 级的语句体被包进 `attr::async_scope`（标记「这段是异步发射」）。
- 一个 async commit group 里**最后一条**语句被标 `async_commit_queue_scope = stage`（标记「到这为止算一组异步提交」）。
- 在消费者读 async buffer 之前，插 `async_wait_queue_scope = stage` 与 `async_wait_inflight_count = N`（「等飞行中的异步操作数降到 N 以下再继续」）。`PopulateWaitCounts` 负责算出这个 count。

#### 4.3.3 源码精读

副本数计算与特例优化：

[ src/transform/inject_pipeline.cc:491-546 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_pipeline.cc#L491-L546) — `ComputeBufferVersions`：基础值 `use - def + 1`（L502）；若 `>=2` 再扫描写/读块对，若无真实冲突（`order(w)<order(r)` 且 `stage(w)<stage(r)` 且 `MayConflict`）则 `num_versions--`（L541-543）。

给 buffer 加版本维：

[ src/transform/inject_pipeline.cc:554-564 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_pipeline.cc#L554-L564) — `RewriteAllocBuffer` 把 `num_versions` 作为第 0 维插到 buffer shape 最前面，对应 `shared memory` 里多分配几份 tile。

在读写处插入 `floormod` 版本选择：

[ src/transform/inject_pipeline.cc:243-271 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_pipeline.cc#L243-L271) — `PipelineBodyRewriter` 对 `BufferStore` / `BufferLoad` 在 indices 最前插入 `version = floormod((loop_var - min), num_versions)`。

三段式展开的总装：

[ src/transform/inject_pipeline.cc:380-408 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_pipeline.cc#L380-L408) — `BuildPipeline` 依次 `EmitImpl` 出 prologue、body、epilogue，再用 `SeqStmt` 拼起来，重新分配 local buffer。

单段内的偏斜与边界保护：

[ src/transform/inject_pipeline.cc:769-903 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_pipeline.cc#L769-L903) — `EmitImpl`：算 `skewed_loop_var = new_loop_var - stage`（L808），prologue/epilogue 用 `need_bound_check` 加 `inbound` 谓词（L809-812），把原始 `pipeline_loop_->loop_var` 替换成 `normalized_access_index`。

异步 commit/wait 的插入：

[ src/transform/inject_pipeline.cc:723-760 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_pipeline.cc#L723-L760) — `CompletePipelineLoopStatements` 给 commit group 末尾加 `async_commit_queue_scope`，给等待点加 `async_wait_queue_scope` / `async_wait_inflight_count`。

[ src/transform/inject_pipeline.cc:852-859 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_pipeline.cc#L852-L859) — async 级的语句体被包进 `attr::async_scope`。

合法性校验（保证重排不破坏依赖）：

[ src/transform/inject_pipeline.cc:977-1014 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_pipeline.cc#L977-L1014) — `ValidatePipelineBody`：每个 order 唯一；对任意「写→读」依赖，要求 `stage(src) <= stage(dst)`，且若 `stage(src)==stage(dst)` 则必须 `order(src) < order(dst)`。

pass 注册：

[ src/transform/inject_pipeline.cc:1339-1354 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_pipeline.cc#L1339-L1354) — `InjectSoftwarePipeline()` 调 `PipelineInjector::Inject`，再做一次 `ConvertSSA`，注册为 `tl.transform.InjectSoftwarePipeline`。

#### 4.3.4 代码实践

**目标**：从最终生成的 CUDA 源码里「看到」多缓冲——shared tile 的声明规模约为 `num_stages × block_M × block_K`。

**步骤**：

1. 把 quickstart 的 `num_stages` 临时设小（如 2），方便阅读。由于 `OptimizeForTarget` 是整段 pass，最简单的办法是看**最终生成的 CUDA 源码**：

```python
# 示例代码：取出 CUDA 源码观察多缓冲
kernel = matmul(M, N, K, block_M, block_N, block_K)  # 需把 num_stages 提到外层参数，见第 5 节
src = kernel.get_kernel_source()
print(src)
```

2. 在 `src` 里找 shared memory 声明：你会看到 `A_shared` / `B_shared` 被声明成了多份（或带一个额外的最外维），对应 `RewriteAllocBuffer` 插入的版本维。
3. 找循环结构：你会看到一个「预热循环 + 主循环 + 排空循环」的形态（prologue / body / epilogue）。

**需要观察的现象**：shared tile 的声明规模约为 `num_stages × block_M × block_K`；存在多段循环而非单一 for。

**预期结果**：直观看到「多缓冲」就是「shared memory 里多放几份 tile」。具体行数与变量名「待本地验证」。

#### 4.3.5 小练习与答案

**Q1**：为什么需要 prologue 和 epilogue，不能只留 body？
**答**：body 是稳态，默认「每次迭代 prefetch 未来 max_stage 轮、算当前轮」。但最前 max_stage 轮还没 prefetch 过（需要先把流水灌满），最后 max_stage 轮已经没有新块可搬（只需要把已搬的算完）。prologue 负责「只搬不算」灌满流水，epilogue 负责「只算不搬」排空流水，二者都需要边界保护。

**Q2**：`num_versions = use - def + 1` 在什么情况下会被减 1？
**答**：当代码扫描发现「不存在 order 更小、stage 更小、且访问区域相交的写→读对」时（`ComputeBufferVersions` L533-540），说明实际上不会发生「生产者还没写完，消费者就要读」的冲突，可以少留一份缓冲。

**Q3**：`async_wait_inflight_count` 表示什么？
**答**：表示「等待直到该 async 队列里飞行中（已发射但未完成）的操作数 ≤ 这个数」。它给消费者加了一道闸：只有当 prefetch 足够 ahead 时才允许读，保证读到的是已完成搬运的数据。

---

### 4.4 异步拷贝注入：cp.async 与 TMA/mbarrier

#### 4.4.1 概念说明

到 `InjectSoftwarePipeline` 为止，异步搬运还只是「带 `async_scope` 标记的普通 buffer store」。真正把它们替换成硬件异步指令的是下面两道 pass（二者根据是否走 warp 特化+TMA 路径二选一）：

- **`InjectPTXAsyncCopy`（cp.async，Ampere sm_80+，非 warp 特化路径）**：把 async 作用域内的 `shared[off] = global[off]` 这种 store 替换成 PTX 的 `cp.async` 指令，不等完成就返回。在 `phase.py` 里，它固定跑在 `ThreadSync("shared.dyn")` **之后**——因为 `cp.async` 调用不会被 `ThreadSync` 识别为普通的 buffer load，必须先插完共享内存 barrier 再替换。
- **`InjectTmaBarrier`（TMA + mbarrier，Hopper sm_90+，warp 特化+TMA 路径）**：TMA 一条指令搬一整块多维 tile，搬运到指定字节数时自动让一个 `mbarrier` 到达（`expect_tx` 预告字节数）；消费者 `mbarrier_wait_parity` 等待。这条路径只在 `allow_tma_and_warp_specialized` 为真时启用。

#### 4.4.2 核心流程

**`cp.async` 注入（`InjectPTXAsyncCopy`）**：

1. 进入 `async_scope` 时置 `in_async=true`，离开时复位。
2. 在 async 作用域内，若遇到 `shared[off] = global[off]` 这种 store，且搬运字节数是 4/8/16 之一，就替换成 `ptx_cp_async(dst_ptr, dst_off, src_ptr, src_off, bytes)`。
3. 若 store 的值是 `if_then_else(pred, global_load, 0)`（边界 tile 用谓词保护、缺失部分补 0），则生成**带谓词**的 `cp.async`（多传一个谓词参数）——因为 `cp.async` 的硬件默认补零语义刚好匹配 else 值为 0。
4. 支持标量与向量化（`Ramp`）下标；对合并动态共享内存产生的 byte buffer 会做下标缩放（`index_factor`）。

**TMA + mbarrier 注入（`InjectTmaBarrier`）**：

1. 仅当 kernel 只用 `threadIdx.x`（`ThreadTagChecker::HasOnlyThreadIdxX`）时启用，否则 warning 并原样返回。
2. `TmaExpectTxRewriter`：在每个 TMA load 前，按搬运字节数（`TmaTraitsCollector` 累积 `bulk_copy_bytes`）插入 `mbarrier_expect_tx(barrier, bytes)`，并把 TMA load 的 barrier 参数改成 `get_mbarrier(0)`。
3. `TmaBarrierCollector`：从 TMA load / expect_tx 扫到 `ptx_arrive_barrier`，建立「TMA 操作 ↔ barrier id」映射。
4. `TmaBarrierRewriter`：据映射重写各 TMA 调用的 barrier 实参；在 warp 特化的「仅生产者」场景，被选中的 leader 线程负责 arrive/expect，因此 barrier 的到达线程数置为 1。

#### 4.4.3 源码精读

`cp.async` 注入的核心：

[ src/transform/inject_ptx_async_copy.cc:41-52 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_ptx_async_copy.cc#L41-L52) — 进入 `async_scope` 时置 `in_async=true`，离开时复位。

[ src/transform/inject_ptx_async_copy.cc:54-108 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_ptx_async_copy.cc#L54-L108) — `InjectPTX`：要求 `load->buffer.scope()=="global"`、字节数 ∈ {4,8,16}（L68），发射 `tvm::tir::builtin::ptx_cp_async(...)`（L106-107）；带谓词版本多传一个 `predicate_value`。

[ src/transform/inject_ptx_async_copy.cc:183-218 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_ptx_async_copy.cc#L183-L218) — `VisitStmt_(BufferStoreNode)`：在 async+shared 场景识别 `if_then_else(pred, global_load, 0)`，当 else 值为 0 时生成谓词版 `cp.async`。

TMA 的 `expect_tx` 字节统计：

[ src/transform/inject_tma_barrier.cc:51-91 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_tma_barrier.cc#L51-L91) — `TmaTraitsCollector`：遍历 `tma_load` / `tma_load_im2col`，按 `access_ptr` 的 extent 与元素字节数累加 `bulk_copy_bytes`，作为 `expect_tx` 的字节数。

插入 `expect_tx` 与改写 TMA 的 barrier 参数：

[ src/transform/inject_tma_barrier.cc:120-179 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_tma_barrier.cc#L120-L179) — `TmaExpectTxRewriter::VisitExpr_`：把 TMA load 的 barrier 实参改成 `get_mbarrier(0)`；`VisitStmt_(IfThenElseNode)` 在 TMA 块前插 `mbarrier_expect_tx`。

TMA 操作到 barrier 的映射与重写：

[ src/transform/inject_tma_barrier.cc:204-229 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_tma_barrier.cc#L204-L229) — `TmaBarrierCollector`：扫描到 `ptx_arrive_barrier` 时，把此前累积的 pending TMA 操作都关联到这个 barrier id。

[ src/transform/inject_tma_barrier.cc:507-568 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_tma_barrier.cc#L507-L568) — `TmaBarrierRewriter::VisitExpr_`：据映射重写 `tma_load` / `mbarrier_expect_tx` 的 barrier 实参，并在合适处把 `expect_tx` 改写为 `ptx_arrive_barrier_expect_tx`。

`InjectTmaBarrier` 的启用门槛：

[ src/transform/inject_tma_barrier.cc:581-596 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/transform/inject_tma_barrier.cc#L581-L596) — 若函数用了 `threadIdx.x` 以外的线程维度，则 warning 并原样返回（TMA barrier 要求重构为只用 `threadIdx.x`）。

两条路径在 `phase.py` 里的位置：

[ tilelang/engine/phase.py:197-223 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L197-L223) — `allow_tma_and_warp_specialized` 为真时走 TMA+mbarrier 分支（含 `InjectTmaBarrier`，L201），否则走普通分支（不含 TMA）。

[ tilelang/engine/phase.py:268-272 ](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/phase.py#L268-L272) — `InjectPTXAsyncCopy` 固定排在 `ThreadSync("shared.dyn")` 之后（注释说明：`cp.async` 不被识别为合法 buffer load，必须在共享内存同步插入之后才替换）。

#### 4.4.4 代码实践

**目标**：在生成的 CUDA 源码里找到真正的异步搬运指令。

**步骤**：

1. 在 Ampere（sm_80）或更新的 GPU 上编译 quickstart（`num_stages=3`）。
2. 取出 CUDA 源码：

```python
src = matmul_relu_kernel.get_kernel_source()
print(src)
```

3. 在 `src` 里搜索 `cp.async`（或 `cp_async`）。你应当能在搬运 A/B tile 的位置看到形如 `cp.async.cg.shared.global ...` 的 PTX 内联。
4. 顺带搜索 `cp.async.commit_group` 与 `cp.async.wait_group`，它们对应 `InjectSoftwarePipeline` 插入的 commit/wait。

**需要观察的现象**：搬运 global→shared 的代码不是普通 `ld.global` + `st.shared`，而是 `cp.async`；存在成对的 commit/wait。

**预期结果**：确认「async_scope 注解 → cp.async 指令」的落地。若你的 GPU 是 Hopper 且走了 TMA 路径，则应改为搜索 `cp.async.bulk.tensor`（TMA）与 `mbarrier`。具体指令助记符「待本地验证」。

#### 4.4.5 小练习与答案

**Q1**：为什么 `InjectPTXAsyncCopy` 必须在 `ThreadSync` 之后？
**答**：`ThreadSync` 通过识别 buffer load/store 来分析共享内存同步需求。一旦把 store 替换成 `cp.async` 调用，就不再是合法的 buffer load，`ThreadSync` 会漏分析。所以必须先插同步、再替换。

**Q2**：`cp.async` 的「谓词版本」何时用到？
**答**：当 tile 是边界 tile（如 K 维最后一块不满）时，缺失部分要补 0。前端会生成 `shared = if_then_else(in_bound, global_load, 0)`；当 else 值正好是 0 时，`cp.async` 的硬件默认补零语义刚好匹配，于是生成带谓词的 `cp.async`。

**Q3**：TMA 路径下，`mbarrier_expect_tx` 的作用是什么？
**答**：TMA 是「搬运到指定字节数后自动让 barrier 到达」的硬件机制。`expect_tx(barrier, bytes)` 告诉 barrier「预期还有 bytes 字节要到」，每搬完一些字节 barrier 内部计数自减，减到 0 时到达；消费者 `mbarrier_wait` 等到达即可，无需每条指令都同步。

---

## 5. 综合实践

把本讲的多道 pass 串起来，做一个「调 `num_stages` 看权衡」的小实验。

**背景**：基于 `examples/quickstart.py` 的 matmul+relu。先把 `num_stages` 提到外层工厂函数的参数里，方便从外部传入：

```python
# 示例代码：把 num_stages 参数化
import tilelang
import tilelang.language as T

@tilelang.jit
def matmul(M, N, K, block_M, block_N, block_K, num_stages, dtype=T.float16, accum_dtype=T.float32):
    @T.prim_func
    def matmul_relu_kernel(A: T.Tensor((M, K), dtype), B: T.Tensor((K, N), dtype), C: T.Tensor((M, N), dtype)):
        with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
            A_shared = T.alloc_shared((block_M, block_K), dtype)
            B_shared = T.alloc_shared((block_K, block_N), dtype)
            C_local = T.alloc_fragment((block_M, block_N), accum_dtype)
            T.clear(C_local)
            for ko in T.Pipelined(T.ceildiv(K, block_K), num_stages=num_stages):
                T.copy(A[by * block_M, ko * block_K], A_shared)
                T.copy(B[ko * block_K, bx * block_N], B_shared)
                T.gemm(A_shared, B_shared, C_local)
            for i, j in T.Parallel(block_M, block_N):
                C_local[i, j] = T.max(C_local[i, j], 0)
            T.copy(C_local, C[by * block_M, bx * block_N])
    return matmul_relu_kernel
```

**任务**：

1. 固定 `M=N=K=4096`、`block_M=block_N=128`、`block_K=32`，依次取 `num_stages ∈ {1, 2, 3, 4}`。
2. 对每个 `num_stages`：
   - 用 `kernel.get_profiler(tensor_supply_type=tilelang.TensorSupplyType.Normal).do_bench()` 记录延迟。
   - 用 `kernel.get_kernel_source()` 取出 CUDA 源码，统计 `A_shared` / `B_shared` 的 shared memory 总字节数（应大致随 `num_stages` 线性增长）。
3. 画一张表：`num_stages | 延迟(ms) | shared mem 估算`。
4. 解释：
   - 延迟为什么在 `num_stages` 从 1 增到 2/3 时通常下降（更多重叠，更好隐藏访存延迟）。
   - 为什么 `num_stages` 过大后延迟不再下降甚至回升（shared memory 占满 → 占用率（occupancy）下降；寄存器压力上升）。

**预期结果**：你会看到一个「先降后平/升」的曲线，对应「收益 vs 共享内存/寄存器代价」的权衡。这正是选 `num_stages` 的工程直觉——通常 2~4 是甜点区，但具体值要靠测（这也是 u5 自动调优要自动搜索的参数之一）。若本地无 GPU，「待本地验证」延迟数据，但 shared memory 随 `num_stages` 增长可从源码静态确认。

## 6. 本讲小结

- `T.Pipelined(num_stages=N)` 在前端只是一条挂在 `For` 上的 `num_stages` 注解（`src/ir.cc`），真正的改写由后面几道 pass 完成。
- **`PipelinePlanning`** 分析循环体每条语句的读写，判定谁是 copy 生产者（global→shared）、谁是消费者，规划出 `stage` / `order`，并在「copy 全在尾部」时把非 copy 的 stage 减 1（把副本数从 `num_stages+1` 省到 `num_stages`）。
- **`InjectSoftwarePipeline`** 负责落地：给跨级 buffer 加版本维（多缓冲）、把循环展开成 prologue/body/epilogue 三段、用 `floormod` 选版本、用「循环偏斜」让生产者 prefetch 未来迭代，并插入 `async_scope` / `commit` / `wait` 占位。
- **`InjectPTXAsyncCopy`**（sm_80+，非 warp 特化）把 async 作用域内的 global→shared 搬运替换成 `cp.async`；**`InjectTmaBarrier`**（sm_90+，warp 特化+TMA）给 TMA 配 `mbarrier` 的 `expect_tx` / `arrive` / `wait`。
- shared tile 的副本数 ≈ `num_stages`，所以 `num_stages` 越大，重叠越好，但 shared memory 与寄存器代价也越大——选值是「延迟 vs 占用率」的权衡。

## 7. 下一步学习建议

- **warp 特化与 Hopper wgmma（u4-l3）**：本讲的 TMA+mbarrier 路径是 warp 特化（生产者 warp 搬、消费者 warp 算）的一部分，建议接着学 `WarpSpecialized` 重写与 `T.ws`，并阅读 `examples/warp_specialize/` 下的示例。
- **存储与内存管理 pass（u4-l4）**：多缓冲带来的多份 shared memory，如何被 `MergeSharedMemoryAllocations` / `StorageRewrite` 合并、复用，是控制 shared mem 代价的关键。
- **自动调优（u5-l1）**：`num_stages` 是典型可调参数；学完 `AutoTuner` 后，可以让搜索自动替你找第 5 节综合实践里的甜点值。
- **源码延伸阅读**：`src/transform/multi_version_buffer_rewriter.cc`（warp 特化路径的多缓冲）与 `src/transform/warp_specialized_rewriter.cc` 里对 `tl_pipeline_stage`/`tl_pipeline_order` 注解（含 `-1` 跳过）的处理，是本讲 4.3 机制在 WS 路径下的对应实现。
