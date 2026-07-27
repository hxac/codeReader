# T.Pipelined 软件流水

## 1. 本讲目标

本讲解决一个核心问题：**怎么让 Ascend 核上的「数据搬运」和「计算」重叠起来，把搬运延迟藏到计算时间里**。

学完后你应该掌握：

- 软件流水（software pipeline）的直觉：为什么把一段顺序循环拆成 prefetch / main / tail 三段就能提速；
- `T.Pipelined` 前端原语的接口与 `num_stages` 含义，并区分**单核内流水（intra-core）**与**Cube/Vector 跨核流水（inter-core）**；
- `PipelinePlanning` 与 `InjectSoftwarePipeline` 两个配套 pass 如何把一行 `T.Pipelined` 逐步重写成真正可执行的三段式循环与多版本缓冲；
- 跨核流水里 `cross_interval` 的作用，以及「核内流水与跨核流水不能嵌套」「单个程序只支持一个跨核流水」这两条硬约束。

## 2. 前置知识

在进入本讲前，请确认你已建立以下认知（对应 u2-l3、u3-l1、u3-l2、u3-l3）：

- **循环原语都是 TIR `For` 节点加一种 ForKind 调度提示**：`T.serial` 是最朴素的顺序循环（ForKind::kSerial）。本讲要讲的 `T.Pipelined` 也是在 `T.serial` 的循环上挂一个 `num_stages` 注解，再由编译 pass 改写。
- **片上存储层级与搬运**：Cube 核拥有 L1、L0A/L0B/L0C；Vector 核拥有 UB；GM 是全局显存。`T.copy` 负责在这些层级间搬运数据，搬运（DMA）与计算（Mmad 等）可以并行执行——这正是流水能成立的硬件基础。
- **矩阵乘 `T.gemm_v0`**：A、B 在 L1，结果累加到 L0C，模板内部包含 L1→L0A/L0B 的搬运与 K 分段累加。GEMM 的 K 维循环是最经典的流水改造对象。
- **缓冲与缓冲复用**：`T.alloc_shared`/`T.alloc_fragment` 分配的缓冲在一次循环迭代内被搬运写入、被计算读出。流水要求「搬运下一份」和「计算这一份」同时进行，因此同一块缓冲需要有多个副本（多版本）。

如果你还不熟悉 `T.serial` 与 `T.gemm_v0` 的写法，建议先读完 u2-l3 和 u3-l3 再回来。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tilelang/language/pipeline.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/pipeline.py) | `T.Pipelined` 的 Python 前端薄封装，定义接口与 `num_stages` / `cross_interval` 参数 |
| [src/ir.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc) | C++ FFI `PipelinedFor`，把 `num_stages` 写成一个 `For` 节点的注解 |
| [src/transform/pipeline_planning.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/pipeline_planning.cc) | `PipelinePlanning` pass：分析循环体，自动算出每个语句的 `stage` / `order` |
| [src/transform/inject_pipeline.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/inject_pipeline.cc) | `InjectSoftwarePipeline` pass：按 `stage` / `order` 把循环重写成 prologue/body/epilogue，并对缓冲做多版本化 |
| [src/transform/cross_core_pipeline.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/cross_core_pipeline.cc) | `CrossCorePipeline` pass：检测 Cube↔Vector 跨核流水、消费 `cross_interval` |
| [tilelang/engine/phase.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py) | 编译流水线编排，决定三个 pass 的执行先后 |
| [examples/pipeline/matmul_add_pipeline.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/pipeline/matmul_add_pipeline.py) | 可运行示例：两处 `T.Pipelined` 分别覆盖 K 循环与 vec_proc 循环 |
| [docs/tutorials/t_pipelined.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/tutorials/t_pipelined.md) | 官方教程，含 intra-core / inter-core 时序表与互斥约束说明 |

## 4. 核心概念与源码讲解

### 4.1 T.Pipelined 原语与软件流水原理

#### 4.1.1 概念说明

先看一个不加流水的朴素 GEMM K 循环（摘自 [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py#L41-L49)）：

```python
loop_k = T.ceildiv(K, K_L1)
for k in T.serial(loop_k):
    T.copy(A[bx * block_M, k * K_L1], A_L1)   # 搬运：GM → L1
    T.copy(B[k * K_L1, by * block_N], B_L1)   # 搬运：GM → L1
    T.barrier_all()
    T.gemm_v0(A_L1, B_L1, C_L0, init=(k == 0)) # 计算：L1 → L0C
    T.barrier_all()
```

每一轮迭代都严格按「先搬运、再同步、再计算」的顺序串行执行。问题在于：**当 `T.gemm_v0` 在算第 k 块时，DMA 搬运单元是闲着的**——它本可以同时去把第 k+1 块数据搬进来。

软件流水的核心思想就是：**故意打乱顺序，让第 k 轮的计算和第 k+1（乃至 k+2）轮的搬运重叠**。硬件上，Cube 核内部的搬运流水线（MTE2/MTE1）与计算流水线（M/fixpipe）是相互独立的，可以并行推进，所以这种重叠是真实可执行的。

#### 4.1.2 核心流程：prefetch / main / tail 三段式

改写后的循环不再是单一顺序循环，而是被拆成三段（这也是本讲贯穿始终的心智模型）：

1. **prefetch（prologue，预热段）**：循环还没正式进入计算前，先把前若干轮的数据提前搬进来，填满流水「队列」。
2. **main（body，稳态段）**：每一轮同时执行「搬下一份数据」+「算上一份数据」，搬运与计算达到最大重叠，这是性能的来源。
3. **tail（epilogue，排空段）**：数据已全部搬完，把队列里残留的最后几份计算做完。

以 `num_stages=2`（双缓冲）、`loop_k=4` 为例，时序如下（沿用官方文档 [t_pipelined.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/tutorials/t_pipelined.md#L39-L53) 的画法）：

| 时刻 | 搬运 A | 搬运 B | 计算 gemm |
|------|--------|--------|-----------|
| t₀（prefetch） | copy_A₀ | copy_B₀ | — |
| t₁（main） | copy_A₁ | copy_B₁ | **gemm₀** |
| t₂（main） | copy_A₂ | copy_B₂ | **gemm₁** |
| t₃（main） | copy_A₃ | copy_B₃ | **gemm₂** |
| t₄（tail） | — | — | **gemm₃** |

可以看到 `gemm_k` 比它对应的搬运晚了一个时刻出现——这个「延迟」就是流水的标志。

#### 4.1.3 源码精读：`num_stages` 到底是什么

`num_stages` 是控制流水深度的唯一参数。前端定义在 [tilelang/language/pipeline.py:L11-L20](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/pipeline.py#L11-L20)，其文档字符串对它的解释是：

> `num_stages`：The max number of buffer used between pipeline producers and consumers. if num_stages is 0, pipeline will not be enabled.

见 [tilelang/language/pipeline.py:L29-L31](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/pipeline.py#L29-L31)。

关键结论（在后文 4.3、4.4 会用源码逐一验证）：

\[
\text{num\_stages} = \text{同时在流水中的迭代数} = \text{缓冲副本数} = \text{max\_stage} + 1
\]

- `num_stages=0`：不开流水，退化为普通 `T.serial`；
- `num_stages=2`：双缓冲，搬运与计算错开 1 轮（即 4.1.2 的时序表）；
- `num_stages=3`：三缓冲，错开 2 轮，预热段更长、重叠更深，但缓冲副本更多。

前端的另一个参数 `cross_interval`（[tilelang/language/pipeline.py:L32-L35](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/pipeline.py#L32-L35)）只对**跨核流水**生效，4.4 节会展开。

最后前端把参数透传给 C++ FFI（[tilelang/language/pipeline.py:L52](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/pipeline.py#L52)）：

```python
return _ffi_api.Pipelined(start, stop, num_stages, order, stage, sync, group, cross_interval)
```

C++ 侧 `PipelinedFor` 做的唯一一件「实事」就是把 `num_stages` 写进循环注解，供后续 pass 识别（[src/ir.cc:L92-L94](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc#L92-L94)）：

```cpp
Map<String, ObjectRef> anno;
if (num_stages > 0)
  anno.Set("num_stages", PrimExpr(num_stages));
```

也就是说，`T.Pipelined(N)` 在 TIR 层就是一个带 `"num_stages": N` 注解的 `T.serial` 循环。真正的魔法发生在 4.2、4.3 两个 pass 里。

#### 4.1.4 代码实践：读示例，找两处 T.Pipelined

1. **实践目标**：在真实示例里识别 prefetch/main/tail 三段，并理解 `num_stages` 取值。
2. **操作步骤**：
   - 打开 [examples/pipeline/matmul_add_pipeline.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/pipeline/matmul_add_pipeline.py)，定位第 46 行的 K 循环 `for k in T.Pipelined(loop_k, num_stages=3)` 与第 57 行的 `for i in T.Pipelined(vec_proc, num_stages=2)`。
   - 注意第 46 行循环体里**没有** `T.barrier_all()`，而朴素版 GEMM 是有的。原因是该示例在 `@tilelang.jit` 里开了 `TL_ASCEND_AUTO_SYNC: True`（第 12 行），同步由 `AscendSyncInsert` pass 自动插入，手写 barrier 反而冗余。
3. **需要观察的现象**：循环体只声明「搬运 + 计算」两类操作，没有任何显式同步语句。
4. **预期结果**：你能用一句话说清「第 46 行用 `num_stages=3` 表示三缓冲、第 57 行用 `num_stages=2` 表示双缓冲」。
5. **运行环境说明**：本机若无真实 NPU，运行该示例会失败在 `torch.randn(...).npu()`；本实践为**源码阅读型实践**，重点是读懂结构，运行留待「待本地验证」。

#### 4.1.5 小练习与答案

- **练习 1**：把 4.1.2 时序表里的 `num_stages` 改成 3（三缓冲），`loop_k` 仍是 4，画出新的 prefetch/main/tail 时序表。
  - **答案**：预热段填 2 轮搬运（copy₀、copy₁），稳态段每轮同时出现「搬 i+2」与「算 i」，排空段要补算 gemm₂、gemm₃ 两轮。整体仍是 4 轮搬运 + 4 轮计算，但搬运与计算的重叠窗口更宽。
- **练习 2**：`num_stages=1` 在语义上等价于什么？
  - **答案**：等价于普通顺序循环——只有 1 份缓冲、没有错位搬运，所以没有重叠，相当于 `T.serial`。这也对应 pass 里 `num_stages >= 1` 的下限检查（见 4.2 节 `CHECK(num_stages >= 1)`）。

---

### 4.2 PipelinePlanning pass：把 num_stages 规划成 stage / order

#### 4.2.1 概念说明

光有一个 `num_stages` 注解还不够——编译器还得知道**循环体里的每一条语句属于哪个 stage（流水级）、排在哪个 order（同迭代内的先后）**。这两组信息决定了最终三段式重写里各语句的错位量。

`PipelinePlanning` pass（[src/transform/pipeline_planning.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/pipeline_planning.cc)）就是负责**自动算出 stage / order** 的「规划器」。用户只写 `num_stages`，不必手写 stage/order（那属于更底层的 `order`/`stage` 参数，用于 warp-specialize 等高级场景）。

#### 4.2.2 核心流程

pass 的主干在 `PipelinePlanner::VisitStmt_(const ForNode *)`（[src/transform/pipeline_planning.cc:L325-L571](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/pipeline_planning.cc#L325-L571)），逻辑可以概括为：

1. **读注解**：只处理带 `num_stages` 注解的 `T.serial` 循环（第 376-L378 行）；若循环已被显式 `order`/`stage` 注解（warp-specialize 场景，含 -1 哨兵），则原样放行（第 329-L352 行）。
2. **收集 stage 信息**：对循环体里的每条语句调用 `MakePipelineStageInfo`，用 `BufferRegionCollector` 算出它的 `reads` / `writes`，并判断它是不是 **copy 语句**（`copy_stage`，特指 global→片上的搬运，第 107-L112 行）。
3. **建 use-def 链**：分析每个 copy 语句写出的缓冲被后面哪条语句读，得到 `last_use_stage`（第 435-L464 行）。
4. **分配 stage/order**（第 466-L490 行）：
   - 普通计算语句 → 分配 `stage = num_stages`；
   - 喂给它数据的 copy 语句 → 分配 `stage = 0`，`order` 紧跟在对应计算语句之后。
5. **copy 前移优化**：若所有 copy 都排在末尾，可整体前移并把计算 stage 减 1（第 522-L545 行），使总错位更小、缓冲副本更省。
6. **落注解**：把算好的 `stage` / `order` 数组写回循环的 `software_pipeline_stage` / `software_pipeline_order` 注解（第 548-L570 行）。

以 4.1 的双缓冲 GEMM（copy_A、copy_B、gemm 三条语句，`num_stages=2`）为例，规划结果是：

| 语句 | order | stage |
|------|-------|-------|
| copy_A | 0 | 0 |
| copy_B | 1 | 0 |
| gemm   | 2 | 1 |

即「两个 copy 在 stage 0，gemm 在 stage 1，错开 1 轮」——正好对应 4.1.2 时序表里 gemm 比 copy 晚一个时刻。

#### 4.2.3 源码精读：copy 语句的识别

「哪条语句算 copy」决定了它能不能被前移到 stage 0。识别逻辑在 `BufferRegionCollector::VisitStmt_(const BufferStoreNode *)`（[src/transform/pipeline_planning.cc:L94-L113](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/pipeline_planning.cc#L94-L113)）：

```cpp
is_global_read_ = false;
this->VisitExpr(op->value);            // 先访问右端，判断是否读 global
if (is_global_read_ && (store_buffer.scope() == "shared" ||
                        store_buffer.scope() == "shared.ub" ||
                        store_buffer.scope() == "shared.l1")) {
  is_global_copy_pattern_ = true;       // 源在 global、目的在片上 → 记为 copy
}
```

判据很直白：**源缓冲 scope 是 `global`，目的缓冲 scope 是片上（shared / shared.ub / shared.l1）**，就标记为 copy 语句。这正是 GEMM 里 `T.copy(A_gm, A_L1)` 的形态。只有 copy 语句才有资格做 stage 0 的「预取生产者」。

而 `num_stages` 的下限与循环种类检查在第 406-L407 行：

```cpp
CHECK(num_stages >= 1);
CHECK(loop->kind == ForKind::kSerial);
```

——流水只接受 `T.serial`（`T.Pipelined` 降级后就是 serial），且至少 1 级。

#### 4.2.4 代码实践：手算 stage / order

1. **实践目标**：自己用 pass 的规则预测一段循环体规划出的 stage/order，再用 `func.get_kernel_source()` 间接验证。
2. **操作步骤**：
   - 取 4.1 朴素 GEMM 的循环体（copy_A、copy_B、gemm 三条），按 4.2.2 的规则手工分配 stage/order，假设 `num_stages=2`。
   - 把 `example_gemm.py` 第 42 行 `for k in T.serial(loop_k):` 改成 `for k in T.Pipelined(loop_k, num_stages=2):`，并**删掉循环里两条 `T.barrier_all()`**（依赖 auto-sync），重新编译。
   - 调用 `func.get_kernel_source()` 查看生成的 Ascend C 代码，找其中被双缓冲化后的 K 循环结构。
3. **需要观察的现象**：生成代码里 `A_L1`/`B_L1` 缓冲应该被复制成两份（带 stage 下标），K 循环被拆成「先搬 1 份 → 中间边搬边算 → 最后再算 1 份」三段。
4. **预期结果**：你手工预测的「copy→stage0、gemm→stage1」与生成代码里的双缓冲结构一致。
5. **说明**：能否真实编译运行取决于本机是否有 CANN 环境；仅做结构对比属**源码阅读型实践**，运行结果「待本地验证」。

#### 4.2.5 小练习与答案

- **练习 1**：如果循环体里多加一条 `T.copy(C_L0, C_gm)`（把结果搬回 GM），它会被规划成哪个 stage？为什么？
  - **答案**：这条 copy 的源是片上（L0C）、目的是 global，**不符合** copy 语句判据（要求源 global→目片上），所以它不会被识别为可前移的 copy。它会被当作普通计算语句分配到 `stage = num_stages`，即和 gemm 同级。
- **练习 2**：第 522-L545 行的「copy 前移优化」什么条件下触发？
  - **答案**：当所有 copy 语句的 order 都大于所有非 copy 语句的 order（`copy_order_min > non_copy_order_max`）且 `num_stages >= 2` 时触发；它把 copy 整体前移一位并把非 copy 的 stage 减 1，从而减少总缓冲副本。

---

### 4.3 InjectSoftwarePipeline pass：三段式重写与缓冲多版本

#### 4.3.1 概念说明

`PipelinePlanning` 只是给每条语句贴了 stage/order 标签，**循环结构本身还是单层顺序循环**。真正把它改写成 prefetch/main/tail 三段、并为缓冲创建多个副本的，是 `InjectSoftwarePipeline` pass（[src/transform/inject_pipeline.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/inject_pipeline.cc)）。

这是整个流水机制里最重的一个 pass，但它的核心算法其实就两件事：**三段拆分**与**缓冲多版本化**。

#### 4.3.2 核心流程

pass 入口 `InjectSoftwarePipeline()`（[src/transform/inject_pipeline.cc:L1066-L1075](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/inject_pipeline.cc#L1066-L1075)）对每个 `PrimFunc` 跑一遍 `PipelineInjector::Inject`，它递归找到带 `software_pipeline_stage` 注解的循环，交给 `PipelineRewriter::BuildPipeline` 重写（[src/transform/inject_pipeline.cc:L266-L370](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/inject_pipeline.cc#L266-L370)）。

**第一件事——三段拆分**，在 `BuildPipeline` 里直接可见（[src/transform/inject_pipeline.cc:L326-L335](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/inject_pipeline.cc#L326-L335)）：

```cpp
Stmt prologue  = EmitImpl(min,            min + max_stage_,              true, true);  // 预热
Stmt body      = EmitImpl(min + max_stage_, min + extent,                 false,false); // 稳态
Stmt epilogue  = EmitImpl(min + extent,     min + extent + max_stage_,    true, true);  // 排空
SeqStmt stmt = SeqStmt({prologue, body, epilogue});
```

三段的边界由 `max_stage_`（所有语句 stage 的最大值）决定。`EmitImpl`（[src/transform/inject_pipeline.cc:L671-L783](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/inject_pipeline.cc#L671-L783)）按「循环变量 − stage」做时间偏移（第 697 行 `skewed_loop_var = new_loop_var - stage`），并用 `inbound` 边界检查（第 699-L701 行）自动剔除每段里越界的语句——这正是三段能正确「裁剪」出 prefetch/tail 的原理。

仍以双缓冲 GEMM（`max_stage_=1`，extent=4）为例，`EmitImpl` 产出的三段为：

- prologue（i: 0→1）：只 copy₀（gemm 因 `skewed_var=-1` 越界被剔除）；
- body（i: 1→4）：每轮 copy_i + gemm_(i−1)；
- epilogue（i: 4→5）：只 gemm₃（copy 因越界被剔除）。

与 4.1.2 时序表完全吻合。

**第二件事——缓冲多版本化**。既然「搬下一份」和「算这一份」同时发生，`A_L1` 就必须有两份：一份正在被 gemm 读，另一份正在被新 copy 写。`ComputeBufferVersions`（[src/transform/inject_pipeline.cc:L447-L502](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/inject_pipeline.cc#L447-L502)）用活跃性分析算出每个缓冲需要的副本数：

```cpp
int num_versions = buffer_info.use - buffer_info.def + 1;   // 上界
```

其中 `def` 是缓冲首次被写的 stage，`use` 是最后被读的 stage。对 `A_L1`：`def=0`（copy 写）、`use=1`（gemm 读），故 `num_versions=2`。随后 `RewriteAllocBuffer`（[src/transform/inject_pipeline.cc:L510-L519](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/inject_pipeline.cc#L510-L519)）给缓冲在最外维扩出一个长度为 `num_versions` 的维度，并在 `PipelineBodyRewriter` 里用 `floormod(loop_var, num_versions)`（[src/transform/inject_pipeline.cc:L216-L219](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/inject_pipeline.cc#L216-L219)）让每轮迭代访问到对应的「那一格」副本——即用循环变量取模实现乒乓/轮转缓冲。

#### 4.3.3 源码精读：为什么不能有依赖违例

`PipelineInjector::ValidatePipelineBody`（[src/transform/inject_pipeline.cc:L864-L901](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/inject_pipeline.cc#L864-L901)）会在重写前做合法性校验，确保重排不破坏读写依赖（read-after-write）。核心两条规则：

- 同 stage 的两条语句若存在 buffer 依赖，它们的 order 必须保持原顺序（第 893-L898 行）；
- 存在依赖的两条语句，源（被依赖）语句的 stage ≤ 目标语句的 stage（第 889-L892 行）。

这条校验是「用户乱写 stage/order 时编译期报错」的来源——但它不作用于 `T.Pipelined(num_stages=...)` 这条自动路径，因为 stage/order 是 `PipelinePlanning` 算出来、本就合法的。

#### 4.3.4 代码实践：跟踪一次三段式重写

1. **实践目标**：亲眼看到「单层 K 循环」被拆成「三段」，并确认缓冲被多版本化。
2. **操作步骤**：
   - 沿用 4.2.4 改好的 `T.Pipelined(loop_k, num_stages=2)` 版 GEMM。
   - 在 `func.get_kernel_source()` 输出里搜索 `A_L1` 相关声明，观察它是否被改写成带额外维度（两份）的缓冲。
   - 定位 K 维度的循环，确认它被拆成「先单独搬一次 → 主循环边搬边算 → 末尾单独算一次」三段。
3. **需要观察的现象**：prologue 段只有 copy、epilogue 段只有 gemm，且对 `A_L1` 的访问带有 `k % 2` 形式的下标。
4. **预期结果**：与 4.3.2 推导的三段结构一致，缓冲副本数 = `num_stages` = 2。
5. **说明**：本机无 NPU 时 `get_kernel_source()` 仍可在 JIT 触发 lowering/codegen 阶段产出源码文本（这一步不依赖 bisheng 实际编译），属可观察项；若 lowering 阶段也需 CANN，则「待本地验证」。

#### 4.3.5 小练习与答案

- **练习 1**：`ComputeBufferVersions` 第 459-L500 行有一段「双缓冲可以省成单缓冲」的特殊处理。它的触发条件是什么？
  - **答案**：当 `use - def + 1 == 2`，且不存在「写者 order < 读者 order 且写者 stage < 读者 stage 且访问区域相交」的情况时，说明读写实际不冲突，可把副本数减 1（退化为单缓冲）。反过来说，GEMM 里 copy（stage0）与 gemm（stage1）确实读写同一区域且满足上述条件，所以 `need_multi_version=true`，副本数保持 2。
- **练习 2**：`PipelineBodyRewriter` 用 `floormod(loop_var, num_versions)` 选副本，为什么用取模而不是直接索引？
  - **答案**：取模实现了「环形」缓冲复用——只有 `num_versions` 份物理缓冲，但任意多轮迭代都能映射到这有限几份上，节省片上内存。这正是乒乓（2 份）/轮转（多份）缓冲的本质。

---

### 4.4 Inter-core 跨核流水、cross_interval 与互斥约束

#### 4.4.1 概念说明

前面三节讲的都是**单核内流水（intra-core）**：搬运 DMA 与计算在同一颗 Cube 核内部重叠。`T.Pipelined` 还能表达另一种流水——**Cube 与 Vector 之间的跨核流水（inter-core）**。

典型场景是 FlashAttention：Cube 核算 QK 得到 `acc_s_l0c`，需要交给 Vector 核做 softmax 与 PV。两者通过 GM/L2 里的 workspace 缓冲交换数据。若让 Cube「写第 k 段 workspace」与 Vector「读第 k−1 段 workspace」重叠，就构成了跨核流水。

关键区别在于：跨核流水的「生产者（Cube）」和「消费者（Vector）」跑在**不同的物理核**上，必须用核间同步原语（`CrossCoreSetFlag`/`CrossCoreWaitFlag`）来握手，而不是核内的 `set_flag/wait_flag`。

#### 4.4.2 核心流程

跨核流水的识别与改写由 `CrossCorePipeline` pass 完成。它在编译流水线里**早于** `PipelinePlanning` 与 `InjectSoftwarePipeline` 执行（[tilelang/engine/phase.py:L98-L101](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L98-L101)）：

```python
mod = tilelang.transform.CrossCorePipeline()(mod)        # 先识别跨核流水
mod = tilelang.transform.CombineCV()(mod)
mod = tilelang.transform.PipelinePlanning()(mod)         # 再规划 stage/order
mod = tilelang.transform.InjectSoftwarePipeline()(mod)   # 最后三段式重写
```

`CrossCoreDetector` 逐条扫描带 `num_stages` 注解的循环体，用每个操作涉及的缓冲 scope 判断它属于 Cube（`shared.l1`/`wmma.*`）还是 Vector（`shared.ub`/`local.var`）。**一旦同一个循环体里同时出现了 Cube 操作和 Vector 操作，就被判定为跨核流水**（[src/transform/cross_core_pipeline.cc:L119-L124](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/cross_core_pipeline.cc#L119-L124)）：

```cpp
if (current_pipeline_info_->scene == INVALID_SCOPE) {
  current_pipeline_info_->scene = scope;          // 记下第一种核
} else if (current_pipeline_info_->scene != scope) {
  current_pipeline_info_->is_cross_core = true;   // 出现第二种核 → 跨核
}
```

判定为跨核后，`ProcessCrossCorePipeline` 读取 `num_stages` 与 `cross_interval`（[src/transform/cross_core_pipeline.cc:L1326-L1332](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/cross_core_pipeline.cc#L1326-L1332)），由 `LoopRewriter` 把循环改写成带 `CrossCoreSetFlag`/`CrossCoreWaitFlag` 的三段式，workspace 缓冲被自动扩成 `num_stages` 份。

`cross_interval` 控制**核间同步的频率**（[docs/tutorials/t_pipelined.md:L81-L113](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/tutorials/t_pipelined.md#L81-L113)）：

- `cross_interval=1`（默认）：每轮迭代都做一次核间 set/wait 同步，并行度最高；
- `cross_interval=N`：每 N 轮同步一次，减少同步开销，适合多 KV-cache 等场景。

#### 4.4.3 源码精读：两条硬约束

约束一——**单个程序只支持一个跨核流水**。`CrossCorePipeline` 的 `VisitStmt_` 只处理 `cross_core_pipelines_[0]`（[src/transform/cross_core_pipeline.cc:L1311-L1316](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/cross_core_pipeline.cc#L1311-L1316)）：

```cpp
Stmt VisitStmt_(const ForNode *op) override {
  if (op == cross_core_pipelines_[0].for_node) {
    return ProcessCrossCorePipeline(op);   // 只改写第一个跨核流水
  }
  return arith::IRMutatorWithAnalyzer::VisitStmt_(op);
}
```

第二个跨核流水会被原样跳过，故官方文档明确「Multiple inter-core pipelines are not supported within a single program」（[docs/tutorials/t_pipelined.md:L170-L171](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/tutorials/t_pipelined.md#L170-L171)）。

约束二——**核内流水与跨核流水不能嵌套**。文档 [t_pipelined.md:L117-L161](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/tutorials/t_pipelined.md#L117-L161) 给出了正反两例：在一个 `T.Pipelined`（跨核）内部再嵌套一个 `T.Pipelined`（核内）是「Not Supported / undefined behavior」；推荐做法（flat pattern）是用外层 `T.Pipelined` 做跨核同步，内层用 `T.serial` + 手写 `set_flag/wait_flag` 做核内双缓冲。这是因为 `CrossCorePipeline` 与 `InjectSoftwarePipeline` 对同一循环的改写职责不能叠加。

此外，跨核流水必须配套开启自动 CV 分离与同步（[docs/tutorials/t_pipelined.md:L172-L178](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/tutorials/t_pipelined.md#L172-L178)）：

```python
pass_configs = {
    tilelang.PassConfigKey.TL_ASCEND_AUTO_CV_COMBINE: True,
    tilelang.PassConfigKey.TL_ASCEND_AUTO_CV_SYNC: True,
}
```

——否则 Cube/Vector 不会被分到不同 scope，跨核检测不会触发。

#### 4.4.4 代码实践：阅读跨核流水示例

1. **实践目标**：在一个真实跨核流水示例里，识别 Cube 写 workspace、Vector 读 workspace 的时序。
2. **操作步骤**：
   - 打开 [examples/pipeline/flash_attn_bshd_pipeline.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/pipeline/flash_attn_bshd_pipeline.py)，找到用 `T.Pipelined(..., num_stages=...)` 包裹 seq_len 分段的那个 K 循环。
   - 对照官方时序表（[docs/tutorials/t_pipelined.md:L71-L79](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/tutorials/t_pipelined.md#L71-L79)），在纸上列出每一时刻 Cube「写 workspace」与 Vector「读 workspace」的对应关系，标出 `num_stages`。
   - 确认该文件的 `@tilelang.jit` 配置里 `TL_ASCEND_AUTO_CV_COMBINE` 与 `TL_ASCEND_AUTO_CV_SYNC` 均为 `True`。
3. **需要观察的现象**：跨核流水的稳态段里，Cube 写第 k 段 workspace 与 Vector 读第 k−1 段 workspace 同时进行。
4. **预期结果**：你画出的时序表与文档的 write/read 时序一致，并能指出「这是 inter-core，不是 intra-core」。
5. **说明**：本实践为**源码阅读型实践**；真实运行需 A5/NPU 环境，结果「待本地验证」。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 `cross_interval` 在纯 intra-core 流水里「没有效果」？
  - **答案**：`cross_interval` 只被 `CrossCorePipeline` 消费（第 1328-L1331 行），用来调节核间 `CrossCoreSetFlag`/`CrossCoreWaitFlag` 的频率。intra-core 流水不触发 `CrossCorePipeline`，所以这个参数无人读取、不起作用（文档 L115 也明确这一点）。
- **练习 2**：若想让跨核流水的稳态段更长、同步更少，应该调大还是调小 `cross_interval`？代价是什么？
  - **答案**：调大 `cross_interval`（如从 1 改为 2）可减少核间同步次数、降低同步开销，适合多 KV-cache 这类每段数据量大的场景；代价是 workspace 需要缓存的段数可能增加，且并行粒度变粗，极端情况下会降低 Cube/Vector 的重叠效率。

## 5. 综合实践

把 4.2.4、4.3.4 串成一个完整任务，亲历一遍「从 `T.serial` 到 `T.Pipelined`」的改造与验证：

1. **改造**：复制 [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py) 为本地 `example_gemm_pipeline.py`，把第 42 行 `for k in T.serial(loop_k):` 改为 `for k in T.Pipelined(loop_k, num_stages=2):`，并删去循环体里两条手写的 `T.barrier_all()`（第 46、49 行），改由 `@tilelang.jit(..., pass_configs={TL_ASCEND_AUTO_SYNC: True})` 自动同步。
2. **画时序**：设 `K=1024`、`K_L1=64`，则 `loop_k=16`。按 4.1.2 的格式画出 prefetch（1 轮 copy）、main（15 轮 copy+gemm 错位）、tail（1 轮 gemm）三段时序表。
3. **验证正确性**：运行改后脚本，确认仍打印 `Kernel Output Match!`（与参考 `a @ b` 在 rtol=1e-2、atol=1e-2 内一致）。流水只改执行顺序、不改数值，因此结果应与串行版完全相同。
4. **观察代码**：`func.get_kernel_source()` 查看 `A_L1`/`B_L1` 是否被双缓冲化、K 循环是否被拆成三段。
5. **进阶（可选）**：把 `num_stages` 改为 3，重画时序表，观察预热段与排空段各变长一轮、缓冲副本变为三份。

> 提示：步骤 3、4 的实际运行依赖本机 CANN/bisheng 环境；若不具备，步骤 1、2、5 的「改代码 + 画时序」部分仍可完成，运行结果标注「待本地验证」。

## 6. 本讲小结

- `T.Pipelined(N)` 在 TIR 层只是一个带 `"num_stages": N` 注解的 `T.serial` 循环；真正的流水改写由两个 pass 完成。
- 软件流水把顺序循环拆成 **prefetch / main / tail** 三段，让「搬下一份数据」与「算这一份数据」重叠，靠的是 Cube 内 DMA 与计算流水线相互独立这一硬件事实。
- `num_stages` = 同时在流水中的迭代数 = 缓冲副本数 = `max_stage + 1`；`num_stages=2` 即双缓冲。
- `PipelinePlanning` 自动分析循环体，把 copy 语句分到 stage 0、计算语句分到 stage N，产出 `stage`/`order` 标签；`InjectSoftwarePipeline` 再据此做三段拆分与缓冲多版本化（`floormod` 取模实现环形复用）。
- `T.Pipelined` 还能表达 **Cube↔Vector 跨核流水**（inter-core），由更早执行的 `CrossCorePipeline` pass 识别，需配套 `TL_ASCEND_AUTO_CV_COMBINE/SYNC`，并用 `cross_interval` 控制核间同步频率。
- 两条硬约束：**核内流水与跨核流水不能嵌套**（用 flat pattern 规避），**单个程序只支持一个跨核流水**。

## 7. 下一步学习建议

- 想看跨核流水的完整真实案例，进入 **u5-l2 跨核流水与 CrossCorePipeline**，那里会精读 `cross_core_pipeline.cc` 的 `LoopRewriter` 与 workspace 扩容细节。
- 想理解流水稳态段里那些自动插入的 `set_flag/wait_flag`/`CrossCoreSetFlag` 从哪来，进入 **u4-l2 同步原语** 与 **u4-l3 自动同步插入**。
- 想进一步压榨 GEMM 性能（双缓冲 + swizzle + kL0Size 联调），进入 **u7-l2 高性能 GEMM 优化**。
- 若对「缓冲副本如何落到具体 L1/UB 地址」感兴趣，可顺带读 **u6-l5 内存规划与存储重写**（`AscendStorageRewrite`/`AscendMemoryPlanning`）。
