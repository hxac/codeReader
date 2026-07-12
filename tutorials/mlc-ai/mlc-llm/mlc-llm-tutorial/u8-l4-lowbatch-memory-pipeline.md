# 低 batch 特化、内存估计与流水线并行

## 1. 本讲目标

本讲是编译优化 pass 深入单元（U8）的最后一篇，继续打开 `_mlc_llm_pipeline` 里的三个「面向运行期特性」的 pass。读完本讲，你应当能够：

1. 说清 **`LowBatchGemvSpecialize`** 为什么能为小 batch 解码生成更快的 kernel，以及它是用「多版本 + 分支派发」实现的，而不是写死一个 kernel。
2. 理解 **`AttachMetadataWithMemoryUsage`** 如何估算每个 Relax 函数的临时显存，并把结果写进 `_metadata`，最终被 C++ 引擎用来反推「能开多少并发 / 多长上下文」。
3. 描述 **`PipelineParallelRewrite`** 如何把一个带「stage 边界标记」的单体函数，改写成「N 个 stage 子函数 + 一个按设备组派发的入口 + disco 通信算子」的流水线并行结构。

本讲承接 [u7-l2（compiler pass pipeline 总览）](./u7-l2-pass-pipeline-overview.md)：那一讲给出了五阶段流水线的编排，本讲从 Phase 4 / Phase 5 中挑出这三个相对独立、且都「为了让运行期跑得更好」的 pass 逐行精读。

## 2. 前置知识

- **Prefill 与 Decode 两种执行形态**：prefill 是一次性处理一整段 prompt（序列长、batch 小），decode 是逐 token 生成（batch = 当前并发请求数，通常很小）。两者的 matmul 形状特征截然不同，本讲的第一个 pass 就是为 decode 服务的。
- **GEMM 与 GEMV**：GEMM（通用矩阵乘）是 C=A×B，三个维度都不小；GEMV（矩阵×向量）是 B 的某一维为 1 的特例。decode 阶段的权重矩阵乘，batch 维很小，行为上接近 GEMV。
- **算术强度（arithmetic intensity）**：单位字节读取能换来多少次浮点运算。它是判断一个 kernel 是「算力受限」还是「访存受限」的核心指标，本讲用它解释为何小 batch 需要不同的调度。
- **TVM Dlight**：TVM 自带的「默认调度」框架，给定一个未调度的 TIR `PrimFunc`，`dl.ApplyDefaultSchedule(...)` 会按模式匹配套上一套 GPU/CPU 调度（如 `dl.gpu.Matmul()`、`dl.gpu.LowBatchGEMV(bucket)`）。本讲的 pass 是在 Dlink 调度之上做「多版本特化」。
- **Relax 内存规划**：`StaticPlanBlockMemory` 会为每个 Relax 函数里所有临时张量显式插入 `alloc_storage` / `alloc_tensor`，把「何时分配、分配多大」固化下来。本讲的内存估算 pass 必须在它之后运行。
- **TVM Disco**：TVM 的分布式运行时，把多个 worker 编成 group。本讲的流水线并行 pass 用 disco 的 `send_to_next_group` / `recv_from_prev_group` 在 stage 之间传张量。
- **tirx**：本仓库当前使用的新版 TIR DSL（`from tvm import tirx`），对应最近几次 TVM 重构（见仓库最近提交 `[Refactor] Adapt to TVM PrimType and tirx refactor`）。你会在源码里看到 `tirx.Var`、`tirx.IfThenElse`、`tirx.SBlock` 等。

## 3. 本讲源码地图

| 文件 | 作用 | 所在流水线阶段 |
| --- | --- | --- |
| [python/mlc_llm/compiler_pass/low_batch_specialization.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/low_batch_specialization.py) | 把 decode 用的 `PrimFunc` 特化成「小 batch GEMV + 大 batch GEMM」多版本带分支派发 | Phase 4（Dlight 低层优化）|
| [python/mlc_llm/compiler_pass/estimate_memory_usage.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/estimate_memory_usage.py) | 估算每个 Relax 函数的临时显存，写入 `_metadata` 函数 | Phase 5（内存规划之后）|
| [python/mlc_llm/compiler_pass/pipeline_parallel_rewrite.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline_parallel_rewrite.py) | 把带 stage 边界标记的单体函数改写成「多 stage + 通信」结构 | Phase 5（VM 字节码降级前）|
| [python/mlc_llm/compiler_pass/pipeline.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py) | 把上面三个 pass 装进五阶段 `Sequential` | 装配 |
| [python/mlc_llm/op/pipeline_parallel.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/op/pipeline_parallel.py) | 模型代码里用来标记 stage 边界的算子 `pipeline_stage_boundary` | 模型定义期 |
| [cpp/serve/config.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/config.cc) | 运行期读取 `memory_usage` 估算，反推 KV cache 容量 | 运行期 |

三个 pass 在流水线里的位置（节选自 `pipeline.py`）：

[python/mlc_llm/compiler_pass/pipeline.py:L151-L194](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py#L151-L194) —— 注意三者的相对顺序：`LowBatchGemvSpecialize`（L153）在最前面的 Dlight 低层优化里；`PipelineParallelRewrite`（L180）在 `StaticPlanBlockMemory`（L190）之前；`AttachMetadataWithMemoryUsage`（L191）紧跟在 `StaticPlanBlockMemory` 之后。这个顺序对理解后两个 pass 至关重要。

---

## 4. 核心概念与源码讲解

### 4.1 低 batch GEMV 特化（LowBatchGemvSpecialize）

#### 4.1.1 概念说明

在 decode 阶段，每一步要对「当前并发的若干请求」各算一次前向，核心计算是权重矩阵乘：

\[ C[m, n] = A[m, k] \times W[k, n] \]

其中 \(m\) 是 batch（并发请求数，常为 1～8），\(k\) 是隐层维度，\(n\) 是输出特征数。decode 时 \(m\) 极小、\(k,n\) 极大。

算术强度（每读一字节权重对应的浮点运算数）约为：

\[ \text{arithmetic intensity} = \frac{2 m k n}{k n b} = \frac{2m}{b} \]

其中 \(b\) 是每个权重元素的字节数。以 fp16（\(b=2\)）为例：\(m=1\) 时算术强度仅 \(1\) FLOP/byte，**严重访存受限**；\(m=64\) 时为 \(64\) FLOP/byte，接近算力受限。也就是说——**同一个 matmul，小 batch 和大 batch 的最优 kernel 形态完全不同**：

- 大 batch（GEMM 形态）：沿 \(m,n,k\) 三个维度分块（tile），用大 thread block 把算力吃满。
- 小 batch（GEMV 形态）：\(m\) 维根本填不满一个 block，应当把 \(m\) 留在 warp / 寄存器内，让线程并行沿 \(n,k\) 展开，最大化「每读一次权重算尽可能多的输出」——这正是 `dl.gpu.LowBatchGEMV(bucket)` 调度的目标。

`LowBatchGemvSpecialize` 要解决的问题就是：**编译期并不知道运行时 batch 到底是 1 还是 8 还是 64，怎样才能让每种 batch 都跑在合适的 kernel 上？** 它的答案不是预测，而是「多版本 + 运行期分支派发」。

#### 4.1.2 核心流程

对 IRModule 中**每个 TIR `PrimFunc`**，做以下事情：

1. 用两个 bucket（2 和 4）分别套 `dl.gpu.LowBatchGEMV(bucket)`，得到两个 GEMV 特化版本。
2. 若两个特化版本都和原函数结构相同（说明 Dlight 没认出它是可特化的 matmul），跳过。
3. 收集函数 buffer 形状里出现的符号变量；**若不恰好是 1 个符号变量**（即 batch 维），跳过——因为分支派发依赖单一 batch 变量。
4. 再套一次通用 `dl.gpu.Matmul()`，得到大 batch 用的 GEMM 版本。
5. 用嵌套 `IfThenElse` 把三个版本按 batch 阈值串起来：
   - batch ≤ 2 → 用 GEMV(2) 版本
   - batch ≤ 8 → 用 GEMV(4) 版本
   - 否则 → 用通用 Matmul 版本
6. 给新函数打两个属性：`tirx.is_scheduled=1`（告诉后续通用 Dlight「我已经调度过了，别再动」）和 `tirx.HoistIfThenElseExprWithBlock=1`（允许把分支条件提升出线程区域）。

派发结构示意：

```
decode_primfunc(batch, ...):
    if batch <= 2:          # 低并发
        <LowBatchGEMV(2) 调度的 body>
    elif batch <= 8:        # 中等并发
        <LowBatchGEMV(4) 调度的 body>
    else:                   # 高并发（接近 prefill 形态）
        <通用 Matmul 调度的 body>
```

#### 4.1.3 源码精读

整个 pass 只有一个类，结构很紧凑：

[python/mlc_llm/compiler_pass/low_batch_specialization.py:L10-L64](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/low_batch_specialization.py#L10-L64) —— 遍历所有 `PrimFunc`，对每个函数生成多版本并改写。

关键的两组阈值：

[python/mlc_llm/compiler_pass/low_batch_specialization.py:L22-L31](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/low_batch_specialization.py#L22-L31) —— `low_batch_range=[2,8]` 是**分支阈值**（dispatch threshold），`buckets=[2,4]` 是喂给 `LowBatchGEMV` 的**bucket 参数**。下标对齐：`range[0]=2` 配 `funcs[0]=GEMV(2)`，`range[1]=8` 配 `funcs[1]=GEMV(4)`。

「特化失败就跳过」的守卫：

[python/mlc_llm/compiler_pass/low_batch_specialization.py:L32-L43](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/low_batch_specialization.py#L32-L43) —— 第一段 `structural_equal` 检查：如果套了 `LowBatchGEMV` 后函数没变化（Dlight 没匹配上），说明这个函数不是它管得了的 matmul，直接 `continue`。第二段要求 buffer 形状里**恰好 1 个**符号变量——那个变量就是运行期才会确定的 batch。

通用 GEMM 版本与分支组装（这是全文件最巧妙的一段）：

[python/mlc_llm/compiler_pass/low_batch_specialization.py:L44-L60](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/low_batch_specialization.py#L44-L60) —— 注意循环是 `reversed(list(enumerate(low_batch_range)))`，即先处理 `(i=1, range_limit=8)` 再处理 `(i=0, range_limit=2)`，逐层把上一轮的 `body` 塞进 `else` 分支。展开后就是上文那个三层 `if/elif/else`。`tvm_thread_invariant` 告诉编译器「`symVar <= range_limit` 对所有线程都成立」，配合 L62 的 `HoistIfThenElseExprWithBlock`，这个分支判断可以被提升到 host 端、每线程只走一条路径，避免在 GPU kernel 内部分支。

最后两个属性决定了它和后续通用 Dlight 调度的协作：

[python/mlc_llm/compiler_pass/low_batch_specialization.py:L61-L63](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/low_batch_specialization.py#L61-L63) —— `is_scheduled=1` 让紧随其后的通用 `dl.ApplyDefaultSchedule`（见 `pipeline.py` L154-L166）**跳过**这些已特化函数；否则通用调度会覆盖掉这里的多版本结构。

#### 4.1.4 代码实践

**实践目标**：验证 `LowBatchGEMV` 确实改变了 kernel 结构，并理解「特化失败就跳过」的过滤效果。

**操作步骤**：

1. 在 `low_batch_specialization.py` 的 L32 处（`if any(... structural_equal ...)`）前后各加一行临时日志（仅用于阅读，实践后删掉，**不要提交**）：

   ```python
   # 示例代码（仅供本地阅读，非项目原有代码）
   print(f"[debug] func={g_var.name_hint} eq_to_bucket0={tvm_ffi.structural_equal(low_batch_funcs[0], func)}")
   ```

2. 用 `mlc_llm compile` 编译一个小模型（如 RedPajama-INCITE-Chat-3B-v1，target cuda），并在 compile 时通过环境变量 `MLC_DEBUG_DUMP=/tmp/mlc_dump` 把每阶段 IR dump 出来（dump 机制见 `pipeline.py` 里的 `_DebugDump`）。

3. 对比 dump 目录里 `debug-phase4.py`（本 pass 之后）与 `debug-phase3.py`（本 pass 之前）的 `decode` / `batch_decode` 类 `PrimFunc`。

**需要观察的现象**：

- decode 类函数里出现 `if (m <= 2) { ... } else if (m <= 8) { ... } else { ... }` 形态的三段 body。
- 而像 `prefill` 这类 `m` 维本身就是大序列的函数，因为 `structural_equal` 失败或符号变量数 ≠ 1，应当**没有**被改写。

**预期结果**：decode 函数被特化，prefill 函数保持原样。**若无法在本地运行 CUDA 编译，可改为纯源码阅读：直接对照 L32-L43 说明哪些函数会被过滤掉，标注「待本地验证」即可。**

#### 4.1.5 小练习与答案

**练习 1**：如果把 `low_batch_range` 从 `[2, 8]` 改成 `[2, 4, 16]`（并相应把 `buckets` 改成 `[2, 4, 8]`），派发结构会变成几层？请按展开后的 `if/elif/else` 写出来。

**参考答案**：会变成 4 路派发：`if batch<=2 → GEMV(2); elif batch<=4 → GEMV(4); elif batch<=16 → GEMV(8); else → Matmul`。因为 `reversed(enumerate)` 是逐层嵌套 `else`，每多一个 bucket 就多一层 `elif`。

**练习 2**：为什么守卫要求 `len(symbolic_vars) == 1`？如果一个 decode 函数的 batch 维和 seq 维都是符号变量，会发生什么？

**参考答案**：因为分支条件 `symVar <= range_limit` 只能基于单一变量派发。若有两个符号变量，编译期无法判断该走哪条分支，强行派发会得到语义错误的 kernel。因此该 pass 保守地跳过这类函数，留给通用 `Matmul` 调度处理。

**练习 3**：`tvm_thread_invariant(symVar <= range_limit)` 这个标注如果删掉，会对最终 kernel 产生什么潜在影响？

**参考答案**：没有这个标注，编译器可能不敢把分支判断提升出线程块，导致 GPU kernel 内部出现「同 warp 不同线程走不同分支」的 warp divergence，或者反复求值条件。加上它并配合 `HoistIfThenElseExprWithBlock`，才能让分支成为「每线程只走一条路径」的 host 级判断，保证特化带来的收益不被分支开销吃掉。

---

### 4.2 显存估算（AttachMetadataWithMemoryUsage）

#### 4.2.1 概念说明

引擎启动时需要回答一个关键问题：**「给定这张 GPU 的显存，扣除模型权重后，还能开多少并发请求、多长的上下文？」** 也就是 KV cache 能分到多少显存。

要算这笔账，引擎需要知道两件事：

1. 模型权重占多少字节（`params_bytes`）—— 这个从 metadata 的 `params` 列表就能精确求和。
2. 每个函数运行时**临时 workspace** 占多少字节 —— 这个不平凡，因为函数里有大量中间激活张量，每个多大、是否复用同一块显存，取决于 Relax 的内存规划。

`AttachMetadataWithMemoryUsage` 就是把第 2 件事算出来、塞进 `_metadata` 的 pass。它依赖一个重要前提：**它运行在 `StaticPlanBlockMemory` 之后**（`pipeline.py` L190→L191）。在 `StaticPlanBlockMemory` 之前，临时张量的分配是「逻辑上的」；之后，每个张量都对应一个显式的 `relax.memory.alloc_storage` / `relax.builtin.alloc_tensor`，**带确定的形状和 dtype**。所以估算器要做的只是「遍历这些显式分配，把字节数加起来」。

#### 4.2.2 核心流程

pass 分两步：

1. **估算**：`_MemoryEstimator` 是一个 `PyExprVisitor`，对 IRModule 中每个 `relax.Function`，遍历其调用，命中两种分配算子时累加字节：
   - `relax.builtin.alloc_tensor(shape, dtype)`：字节数 = Π(shape 各维) × ((dtype.bits+7)//8) × lanes；若任一维不是 `IntImm`（即符号维度无法定值），跳过该分配。
   - `relax.memory.alloc_storage(size)`：字节数 = size（已是具体整数）。
   - 得到 `{"prefill": N1, "decode": N2, ...}` 这样的映射。
2. **落盘**：把 `memory_usage` 写进 metadata 字典，再 emit 一个名为 `_metadata` 的 Relax 函数，它无参数、返回一个 `StringImm(json.dumps(metadata))`。运行期 C++ 端会取这个函数的返回值，JSON 反序列化拿到所有元数据。

**运行期如何消费**（连接编译期与运行期的关键一环）：在 `cpp/serve/config.cc` 里，引擎遍历 `metadata.memory_usage`，取**所有函数里的最大值**（不是求和），再乘以 2 作为安全裕度，得到 `temp_buffer_bytes`：

[cpp/serve/config.cc:L850-L855](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/config.cc#L850-L855) —— 取 `max` 是因为 prefill / decode / verify 这些函数在任一执行步里**只会有一个在跑**，峰值临时显存是单函数最大值，而不是总和；再 `*= 2` 是为运行期的非规划分配留余量。随后 `temp_buffer_bytes` 与 `params_bytes` 一起喂给 `EstimateMemoryUsageOnMode`，反推 KV cache 可用空间。

#### 4.2.3 源码精读

pass 主体：接收一个 metadata 字典，把估算结果填进去，再 emit `_metadata` 函数：

[python/mlc_llm/compiler_pass/estimate_memory_usage.py:L16-L36](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/estimate_memory_usage.py#L16-L36) —— 注意 L34 `self.metadata["memory_usage"] = _MemoryEstimator().run(mod)` 是就地修改字典，L35 把整个 metadata（含 `params`、`model_type`、`memory_usage` 等）序列化成 JSON 字符串塞进函数返回值。pass 名 `AttachMetadata` 说明了它和 `pipeline.py` 里 `metadata` 变量的关系：它是 metadata 的「最终封装」。

估算器的入口：对每个 `relax.Function` 重置计数器后访问，记录函数名→字节数：

[python/mlc_llm/compiler_pass/estimate_memory_usage.py:L49-L63](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/estimate_memory_usage.py#L49-L63) —— `planned_alloc_mem` 累加的是**计划内的分配**（即 `StaticPlanBlockMemory` 已经安顿好的）。注意它只处理 `relax.Function`（不是 `PrimFunc`），因为临时显存规划发生在 Relax 层。

访问到调用节点时，按算子分派：

[python/mlc_llm/compiler_pass/estimate_memory_usage.py:L65-L70](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/estimate_memory_usage.py#L65-L70) —— `Op.get("relax.builtin.alloc_tensor")` / `Op.get("relax.memory.alloc_storage")` 是用算子的规范名取 `Op` 实例，再与 `call.op` 比对，典型的 Relax visitor 写法。末尾 `super().visit_call_(call)` 保证继续递归子表达式。

两种分配的字节计算：

[python/mlc_llm/compiler_pass/estimate_memory_usage.py:L72-L81](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/estimate_memory_usage.py#L72-L81) —— `alloc_tensor`：形状各维连乘，再乘每元素字节数 `(dtype.bits+7)//8`（向上取整到字节）和 `lanes`（向量化宽度）。L76-L77 的 `if not isinstance(dim_len, IntImm): return` 是关键保守策略：**遇到无法定值的符号维度就直接放弃这个张量**（不计入），宁可低估也不胡估。

[python/mlc_llm/compiler_pass/estimate_memory_usage.py:L83-L87](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/estimate_memory_usage.py#L83-L87) —— `alloc_storage` 更简单：size 已经是字节数（`StaticPlanBlockMemory` 算好的），直接取 `IntImm` 的值累加。

> 一个常被忽略的点：这个估算**只算临时 workspace，不含模型权重**。权重在运行期是作为 `params` 单独管理的，对应 `config.cc` 里独立累加的 `params_bytes`（L838-L848）。两者相加再加 KV cache，才是一张卡上的总占用。

#### 4.2.4 代码实践

**实践目标**：在 IR 层面亲眼看到 `alloc_storage` / `alloc_tensor` 与估算结果的对应关系。

**操作步骤**：

1. 编译任意一个 CUDA 模型时设置 `MLC_DEBUG_DUMP=/tmp/mlc_dump`，找到 dump 出来的 `debug-phase5.py`（`AttachMetadataWithMemoryUsage` 之后）。
2. 在该文件末尾找到 `_metadata` 函数，它应类似：

   ```
   @R.function
   def _metadata() -> R.Object:
       return R.call_pure_packed("vm.builtin.tuple_getitem", ..., ty_args=R.Object)
       # 或直接返回 StringImm
   ```
   把它返回的 JSON 字符串格式化展开，找到 `"memory_usage"` 字段。

**需要观察的现象**：

- `memory_usage` 是一个 `{函数名: 字节数}` 的映射，`decode` / `prefill` / `batch_verify` 各有一条。
- 在同一个 dump 文件里找到对应函数体，人工数一下 `alloc_storage(...)` 的 size 参数之和，应当与 `memory_usage` 中该函数的值吻合。

**预期结果**：手工累加值 ≈ `memory_usage` 记录值（可能因 `alloc_tensor` 与 `alloc_storage` 的层级差异略有出入，但量级一致）。**若无法本地编译，可改为纯阅读：对照 L72-L87 解释两种算子的字节公式，并标注「待本地验证」。**

#### 4.2.5 小练习与答案

**练习 1**：为什么这个 pass 必须排在 `StaticPlanBlockMemory` 之后，而不能更早？

**参考答案**：在 `StaticPlanBlockMemory` 之前，Relax 函数里的临时张量是用 `relax.Tuple` / 抽象 struct info 表达的，没有显式的 `alloc_storage(size)` 调用，size 也不一定定值。只有内存规划把每次分配固化为带具体形状/dtype 的 `alloc_tensor` 或带具体字节数的 `alloc_storage` 之后，估算器才能简单地把它们加起来。所以 `pipeline.py` 把它放在 L191（`StaticPlanBlockMemory` 在 L190）。

**练习 2**：运行期为什么对 `memory_usage` 取 `max` 而不是 `sum`？什么情况下取 `sum` 才合理？

**参考答案**：因为 prefill、decode、batch_verify 等函数是**串行执行**的——同一个 engine step 只会调用其中一个，它们的临时 workspace 不会同时存在，所以峰值显存是 `max`。只有当两个函数的 workspace 会**真正同时驻留**时，取 `sum` 才合理；本项目的执行模型不满足这一点。

**练习 3**：一个形状为 `[seq, 4096]`（`seq` 是符号变量）的 fp16 `alloc_tensor`，会被计入估算吗？为什么？

**参考答案**：不会。因为 `_builtin_tensor_alloc` 在 L76-L77 遇到 `seq` 不是 `IntImm` 就 `return` 了。这是保守策略：编译期无法给符号维度的张量确定字节数，强行估算会引入错误，不如不计。对应地，这类函数的 `memory_usage` 会偏低，运行期 `*= 2` 的安全裕度就是为这类情况兜底。

---

### 4.3 流水线并行改写（PipelineParallelRewrite）

#### 4.3.1 概念说明

当模型大到单卡装不下，但又想保持每层计算完整（不像张量并行那样把每一层切开），就需要**流水线并行（Pipeline Parallelism）**：把模型的不同层组分到不同 GPU 上，前向时激活值从 stage 0 → stage 1 → ... → stage N 依次流过去。

在 MLC 里，这件事用「标记 + 改写」两步实现：

- **标记阶段（模型定义期）**：模型代码在层组之间调用 `pipeline_stage_boundary(hidden_states)`（定义于 `python/mlc_llm/op/pipeline_parallel.py`），它在 IR 里降级成对 `mlc.pipeline_parallel_stage_boundary` 的调用，并把张量的「该属于第几个 stage」属性写进参数 `attrs["pipeline_stages"]`。哪些模型支持？仓库里目前由 `olmo`、`nemotron` 等模型在 `__init__` / `prefill` 里调用此算子（可自行 `Grep` `pipeline_stage_boundary` 查看最新支持列表）。
- **改写阶段（本 pass）**：编译期把这些标记当成「剪刀口」，把一个完整的 Relax 函数剪成 N 个 stage 子函数，并在接缝处插入 disco 的跨设备通信算子。

本 pass 只对**带 `pipeline_parallel_stages` 属性且大于 1** 的函数生效——单卡（`pipeline_parallel_stages=1`）的模型完全不会被触碰。

#### 4.3.2 核心流程

整体由一个 `PyExprMutator`（`_PipelineParallelRewriter`）+ 三个辅助函数完成：

1. **找函数**：遍历 IRModule，挑出 `func.attrs["pipeline_parallel_stages"] > 1` 的 Relax 函数。
2. **剪开 stage**：`_extract_pipeline_stages` 要求函数体只有一个 dataflow block；逐条扫它的 binding，每遇到一个 `mlc.pipeline_parallel_stage_boundary` 调用就「剪一刀」，记录：
   - `pipeline_stages[i]`：第 i 段的 binding 列表；
   - `stage_send_vars[i]`：第 i 段要发给下一段的张量（boundary 调用的参数）；
   - `stage_receive_vars[i]`：第 i 段要从上一段收的张量（boundary 返回值的解包）。
3. **分析每段需要的入参**：`_analyze_required_func_params` 让每段只接收它真正用到的原函数参数，避免把整份 `packed_params` 塞给每个 stage。
4. **为每段造子函数**：`_create_stage_func`：
   - 非 stage 0 的函数，开头先 emit `runtime.disco.recv_from_prev_group` 把上一段发来的张量「收」进来；
   - 重放本段的 binding；
   - 非末段的函数，结尾 emit `runtime.disco.send_to_next_group` 把要传出去的张量「发」走；
   - 处理形状变量与 `packed_params` 的重映射。
5. **改写入口函数**：原函数体被替换成对 `mlc.multi_gpu.DispatchFunctionByGroup` 的调用——运行期根据当前 worker 所在的 disco group id，派发到对应的 stage 子函数。

一次前向的时间线（以 3 stage 为例）：

```
group 0 (stage0): embed → layers[0..k] → send_to_next_group(h)
group 1 (stage1):        recv_from_prev_group(h) → layers[k+1..2k] → send_to_next_group(h)
group 2 (stage2):        recv_from_prev_group(h) → layers[2k+1..] → logits
```

每个 group 物理上是一组 GPU worker，只持有自己那一段权重（靠参数的 `pipeline_stages` 属性分摊，见 `config.cc` 里 `params_bytes /= metadata.pipeline_parallel_stages`）。

#### 4.3.3 源码精读

pass 入口很薄，真正干活的是 mutator：

[python/mlc_llm/compiler_pass/pipeline_parallel_rewrite.py:L11-L21](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline_parallel_rewrite.py#L11-L21) —— `mod.clone()` 保证改写不污染输入 IR。

挑函数 + 校验 + 抽 stage：

[python/mlc_llm/compiler_pass/pipeline_parallel_rewrite.py:L37-L48](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline_parallel_rewrite.py#L37-L48) —— L38 的双重过滤是「只在声明了多 stage 的函数上动手」；L45 断言「标记剪出的段数 == 属性声明的 stage 数」，否则直接报错（模型代码与配置不一致时尽早失败）。

逐 stage 造子函数，最后改写入口：

[python/mlc_llm/compiler_pass/pipeline_parallel_rewrite.py:L69-L101](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline_parallel_rewrite.py#L69-L101) —— 循环里 `_create_stage_func` 返回 `(stage_gv, caller_args)`，把「子函数引用 + 调用参数」攒成 `dispatch_func_args`。L92-L98 用 `call_builtin_with_ctx("mlc.multi_gpu.DispatchFunctionByGroup", args=[dispatch_func_args])` 作为新入口函数体——运行期由这个 builtin 根据 group id 挑一个 stage 函数执行。

子函数里的「接收」与「发送」通信算子：

[python/mlc_llm/compiler_pass/pipeline_parallel_rewrite.py:L129-L139](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline_parallel_rewrite.py#L129-L139) —— 非 stage 0 的函数开头，对每个 `receive_var` emit `runtime.disco.recv_from_prev_group`，并用 `set_var_remap` 把「原来的张量变量」重映射成「收到的张量」，这样后面重放 binding 时引用该变量的地方会自动换成收到的值。

[python/mlc_llm/compiler_pass/pipeline_parallel_rewrite.py:L154-L163](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline_parallel_rewrite.py#L154-L163) —— 非末段函数结尾，对每个 `send_var` 取其重映射后的新变量，emit `runtime.disco.send_to_next_group`。`recv_from_prev_group` / `send_to_next_group` 这一对就是 disco 提供的跨 group 张量传输原语。

`_extract_pipeline_stages` 的「剪刀」逻辑（辅助函数里最值得读的一段）：

[python/mlc_llm/compiler_pass/pipeline_parallel_rewrite.py:L332-L357](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline_parallel_rewrite.py#L332-L357) —— 它把 binding 流分成三类：① 命中 `mlc.pipeline_parallel_stage_boundary` 的调用 → 当前段结束、记录 send 张量、开启新段；② 对 boundary 返回值的 `TupleGetItem` → 登记为 receive 张量；③ 其余 binding → 归入当前段的普通计算。这就把「线性 binding 序列」解释成了「带边界与收发语义的段」。

模型侧的标记算子（连接模型代码与本 pass 的桥梁）：

[python/mlc_llm/op/pipeline_parallel.py:L9-L33](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/op/pipeline_parallel.py#L9-L33) —— `pipeline_stage_boundary(*tensors)` 把传入张量原样返回，但附带一次 `mlc.pipeline_parallel_stage_boundary` 调用——它的「返回值」纯粹是给本 pass 当切分锚点用的，语义上「我把这些张量交给下一段」。

#### 4.3.4 代码实践

**实践目标**：追踪一个 stage 边界标记是如何被「剪开」并长出通信算子的。

**操作步骤**：

1. 阅读 [python/mlc_llm/model/olmo/olmo_model.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/olmo/olmo_model.py) 中的 `_set_pp`（约 L304-L317）和 prefill/forward 里调用 `op_ext.pipeline_stage_boundary(hidden_states)` 的地方（约 L280）。理解：参数 `attrs["pipeline_stages"]` 标记「我属于哪个 stage」，boundary 调用标记「在这里剪一刀」。

2. 在 `_extract_pipeline_stages`（L316-L365）的 L340（`pipeline_stages.append(...)`）后加一行临时日志（仅供阅读，**不要提交**）：

   ```python
   # 示例代码（仅供本地阅读）
   print(f"[debug] cut stage {len(pipeline_stages)-1}, sends={[v.name_hint for v in stage_send_vars[-1]]}")
   ```

3. 用 `--pipeline-parallel-stages 2` 编译一个支持流水线并行的模型（如 olmo/nemotron，具体以仓库当前支持列表为准），dump 出 `debug-phase5.py`。

**需要观察的现象**：

- 原 `prefill` / `batch_forward` 等带 `pipeline_parallel_stages` 属性的函数，被拆成了 `<name>_stage0`、`<name>_stage1` 两个子函数。
- `_stage0` 结尾出现 `runtime.disco.send_to_next_group(...)`。
- `_stage1` 开头出现 `runtime.disco.recv_from_prev_group(...)`。
- 原入口函数体变成对 `mlc.multi_gpu.DispatchFunctionByGroup` 的调用。

**预期结果**：单体函数被改写成「N 个 stage 子函数 + disco 通信 + group 派发入口」。**若本地无法编译此类模型，可改为纯阅读：对照 L129-L139 与 L154-L163 说明 send/recv 是如何插入的，并标注「待本地验证」。**

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_create_stage_func` 要单独分析「每段需要的入参」（`_analyze_required_func_params`），而不是把原函数的所有参数都传给每个 stage？

**参考答案**：每个 stage 只用得到一部分权重 / 输入（比如 stage 0 只需要 embedding 和前几层权重，stage 1 只需要中间几层）。如果把全部参数都传给每个 stage，既浪费跨 group 传输带宽，也违背了「把权重分摊到各 stage 以省显存」的初衷。`_analyze_required_func_params` 让每个 stage 子函数只声明并接收它真正引用的原参数。

**练习 2**：`pipeline_parallel_stages=1` 的函数会发生什么？这个设计有什么好处？

**参考答案**：什么都不发生——L38-L42 的 `if` 直接 `continue` 跳过，函数原封不动。好处是：流水线并行完全是「opt-in」的，普通单卡模型感知不到这个 pass 的存在，既不会引入额外开销，也不会因为误判而破坏 IR。

**练习 3**：`DispatchFunctionByGroup` 为什么放在入口函数里，而不是在每个 stage 子函数里判断？

**参考答案**：因为「我属于哪个 stage」是由运行期 worker 的 group id 决定的，对所有调用是一次性的外部条件。把它提到入口函数里做一次派发，stage 子函数本身就可以保持「纯计算」的干净形态（只包含 recv → 计算 → send），便于复用与调试；同时也避免在每个子函数里重复嵌入「判断 group」的逻辑。

---

## 5. 综合实践

把三个 pass 串起来，做一次「面向运行期特性的编译优化」阅读分析。

**任务**：以一次 decode 步骤为线索，说明这三个 pass 分别贡献了什么。

1. **decode 被调用，batch=4**：此时运行期进入 `LowBatchGemvSpecialize` 改写过的 decode `PrimFunc`。请回答：batch=4 会走哪条分支？为什么这条分支的 kernel 比通用 Matmul 更快？（提示：算术强度 + GEMV 调度的访存友好性。）
2. **引擎启动时规划显存**：请说明 `AttachMetadataWithMemoryUsage` 估算的 `memory_usage` 是怎么进入 `config.cc` 的 `temp_buffer_bytes` 的，以及它如何间接决定「能开多少并发请求」。要求写出从 `_metadata` 函数 → C++ JSON 反序列化 → `max(temp_buffer)` → `*=2` → `EstimateMemoryUsageOnMode` 的数据流。
3. **（选做）若该模型同时开了 `--pipeline-parallel-stages 2`**：请说明 decode 函数会先被 `PipelineParallelRewrite` 拆成两个 stage 子函数（带 send/recv），再各自被 `LowBatchGemvSpecialize` 在 TIR 层特化。思考：这两个 pass 的作用层级（一个在 Relax 层改结构，一个在 TIR 层换调度）为什么可以叠加而不冲突？

**产出**：画一张「decode 一次前向」的时序图，标注三个 pass 各自在哪一层、哪个时刻发挥作用。提示——`PipelineParallelRewrite` 决定「在哪张卡上算哪几层」，`LowBatchGemvSpecialize` 决定「这几层用什么 kernel 形态」，`AttachMetadataWithMemoryUsage` 则在引擎启动期就为「能容纳多少这样的前向」做好了显存预算。

## 6. 本讲小结

- **`LowBatchGemvSpecialize`** 把 decode 用的 TIR `PrimFunc` 特化成「batch≤2 用 GEMV(2)、batch≤8 用 GEMV(4)、否则用通用 Matmul」的多版本带分支结构，靠 `is_scheduled` 属性避免被后续通用 Dlink 调度覆盖；本质是「用编译期多版本换运行期 batch 自适应」。
- 小 batch 解码**访存受限**（算术强度约 \(2m/b\)），需要把 batch 维留在 warp 内、沿 \(n,k\) 并行的 GEMV 调度；大 batch **算力受限**，需要三轴分块的 GEMM 调度——这是本 pass 存在的物理依据。
- **`AttachMetadataWithMemoryUsage`** 在 `StaticPlanBlockMemory` 之后，遍历每个 Relax 函数的 `alloc_tensor`/`alloc_storage` 累加字节数，写入返回 JSON 的 `_metadata` 函数；运行期 C++ 取各函数**最大值**再 `*=2` 得到 `temp_buffer_bytes`，用于反推 KV cache 容量。
- 估算只覆盖**临时 workspace**（不含权重），且对符号维度张量保守跳过——这两点是理解它「为什么是估算而非精确」的关键。
- **`PipelineParallelRewrite`** 读 `pipeline_parallel_stages` 属性，把模型代码里的 `pipeline_stage_boundary` 标记当剪刀，把单体 Relax 函数剪成 N 个 stage 子函数，接缝处插入 `runtime.disco.recv_from_prev_group` / `send_to_next_group`，入口改为 `DispatchFunctionByGroup` 按 group id 派发。
- 三者共同体现「编译期为运行期服务」的思想：特化服务 decode 性能、估算服务显存规划、改写服务多卡扩展；且分处不同层级（TIR 调度 / Relax 元数据 / Relax 结构），可以正交叠加。

## 7. 下一步学习建议

- **横向扩展阅读**：U9（C++ 推理引擎架构）会讲 `ThreadedEngine` 如何调用这些被特化 / 改写过的函数；U10（KV 缓存、采样与推测解码）会用到本讲估算出的显存预算来决定分页 KV cache 的容量。
- **纵向深入 TVM**：本讲的 `LowBatchGEMV` / `Matmul` 调度来自 TVM Dlight，想理解「bucket 到底怎么影响 tile」可去 `tvm-ffi` / `mlc-ai/relax` 仓库读 `dl.gpu.LowBatchGEMV` 的实现。
- **流水线并行的运行期**：本讲只覆盖编译期改写；运行期 disco 如何编排 group 之间的实际通信，建议接着读 U12-l1（多 GPU 与张量并行）与 `cpp/multi_gpu/`、`cpp/serve/threaded_engine.h` 中的 disco 会话建立代码。
- **动手验证**：若本地有 CUDA 环境，按各模块的「代码实践」用 `MLC_DEBUG_DUMP` dump 出每阶段 IR，对照本讲的行号引用逐段核验——这是把「读懂」变成「真懂」的最快路径。
