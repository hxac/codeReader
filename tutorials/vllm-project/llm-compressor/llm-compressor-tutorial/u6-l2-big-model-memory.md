# 大模型内存策略：逐层加载与磁盘 offloading

## 1. 本讲目标

本讲回答一个非常现实的问题：**当一个模型大到连显存（甚至内存）都装不下时，llm-compressor 是怎么把它量化完的？**

在第三单元你已经学过 `SequentialPipeline`（u3-l5）会「切子图 → 逐子图两遍前向 → 缓存中间激活」。本讲把镜头拉近，专门看这套机制里**和内存有关的每一个开关**：权重是怎么逐层搬上 GPU 的（sequential onloading），中间激活怎么落盘或落 CPU 的（`IntermediatesCache` + `sequential_offload_device`），GPTQ 的 Hessian 怎么搬到 CPU 的（`offload_hessians`），以及 `sequential_prefetch`、`norm_calibration_context` 这两个权衡/正确性开关。

学完后你应当掌握：

- 能说清压缩超大模型时**权重、激活、算法状态**这三条内存战线各自占用什么、由谁负责搬运。
- 能解释 sequential onloading「交换两层 for 循环」的核心直觉，以及 `set_onload_device` / `disable_offloading` 在其中扮演的角色。
- 能区分两种 offloading：**激活 offload**（`IntermediatesCache`，默认 CPU）与**权重磁盘 offload**（`load_offloaded_model` + `device_map="auto_offload"`）。
- 会用 `sequential_offload_device`、`offload_hessians`、`sequential_prefetch` 三个旋钮在「省内存」和「跑得快」之间做权衡。

## 2. 前置知识

本讲假设你已经读过：

- **u3-l5 SequentialPipeline**：知道子图（subgraph）、`sequential_targets`、两遍前向（Calibrating / Propagating）、`propagate_error` 的含义。
- **u3-l3 校准与 Hooks**：知道校准期间会用 forward hook 收集激活统计。
- **u4-l1 GPTQ**：知道 GPTQ 在校准阶段会累积 Hessian 矩阵 \(H = 2X^{\top}X\)，它是一个 hidden×hidden 的张量。

几个需要先建立的直觉：

1. **显存不是只装权重**。压缩时显存里同时住着三类东西：
   - **权重（weights）**：模型参数。一个 70B 的 fp16 模型约 140 GB。
   - **激活（activations）**：子图之间传递的中间输出，大小约等于 `批数 × 序列长度 × 隐藏维`。
   - **算法状态（algorithm state）**：例如 GPTQ 给每个待量化模块存一份 Hessian。
2. **内存 = 峰值**。关心的是「同一时刻最多占多少」，不是「总共搬过多少」。把数据在不同设备间来回搬，只要不让它们同时在场，峰值就能降下来。
3. **搬运有代价**。CPU↔GPU、磁盘↔CPU 的拷贝是慢操作。几乎所有内存策略的本质都是「用额外的搬运时间换更低的峰值」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/llmcompressor/pipelines/sequential/pipeline.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py) | sequential onloading 的主循环：确定 onload/offload 设备、逐子图跑前向、管理激活缓存。 |
| [src/llmcompressor/pipelines/cache.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/cache.py) | `IntermediatesCache`：激活的「写即 offload、读即 onload」缓存，以及 `sequential_prefetch` 的后台预取实现。 |
| [src/llmcompressor/utils/dev.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/utils/dev.py) | 设备工具：`get_main_device`（确定 onload 到哪块卡）、`load_context`（合并权重 offload 与 MoE 线性化的加载上下文）。 |
| [src/llmcompressor/modeling/offset_norm.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/offset_norm.py) | `norm_calibration_context`：把「偏移归一化」层临时换成标准归一化，保证平滑类算法在 Gemma/Qwen3Next 等架构上结果正确。 |
| [src/llmcompressor/modifiers/gptq/base.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py) | `offload_hessians` 开关：把 GPTQ 的 Hessian 矩阵常驻 CPU、用时再 onload。 |
| [src/llmcompressor/args/dataset_arguments.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/dataset_arguments.py) | 所有内存旋钮的默认值定义处。 |

## 4. 核心概念与源码讲解

### 4.1 内存全景：三条战线与「交换两层循环」的核心直觉

#### 4.1.1 概念说明

要压缩一个超大模型，显存同时被三类对象挤压。我们先给它们命名，后面每个模块各自负责压低一条战线：

| 内存战线 | 典型代表 | 谁负责搬运 | 对应开关 |
|---------|---------|-----------|---------|
| 权重 | 模型参数（fp16 的 140 GB） | sequential onloading / 磁盘 offload | `sequential_targets`、`device_map="auto_offload"` |
| 激活 | 子图之间的中间输出 | `IntermediatesCache` | `sequential_offload_device`、`sequential_prefetch` |
| 算法状态 | GPTQ 的 Hessian | modifier 自身 | `offload_hessians` |

#### 4.1.2 核心流程：交换两层循环

最朴素的一次性校准长这样（伪代码）——外层遍历 batch，内层遍历每一层：

```python
for batch in dataloader:          # 外层：数据
    for layer in model.layers:    # 内层：层
        batch = layer(batch)
```

这种写法要求**所有层的权重同时常驻显存**，因为每个 batch 都要走完整个模型。70B 模型在单卡上根本放不下。

sequential onloading 的核心洞察是：**把两层 for 循环交换顺序**。

```python
for layer in model.layers:        # 外层：层
    for batch in dataloader:      # 内层：数据
        batch = layer(batch)
```

交换后，**同一时刻只有一层的权重在显存里**。峰值权重显存从「整个模型」降到「一个子图」，与模型总层数解耦。这正是官方文档用伪代码图示说明的核心思想：

[docs/guides/big_models_and_distributed/sequential_onloading.md:9-21](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/guides/big_models_and_distributed/sequential_onloading.md#L9-L21) —— 文档对比了两种循环顺序，明确指出 sequential onloading 就是「把两层 for 的顺序换掉」。

同理，激活也可以「写即搬走、读再搬回」，使**峰值激活显存与 batch 数解耦**（4.3 节展开）。

> 注意：交换循环顺序后，每个 batch 的中间激活必须跨「层循环」保存下来留给下一层用，这就是 `IntermediatesCache` 存在的原因。

#### 4.1.3 代码实践：读图，确认交换循环的含义

1. **实践目标**：把上面的两层循环直觉和真实源码对应起来。
2. **操作步骤**：打开 [pipeline.py 的 `__call__`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L55-L181)，找到 `for subgraph_index, subgraph in enumerate(subgraphs)`（外层，L136）和它内部的 `for batch_idx, inputs in _get_batches(...)`（内层，L145）。
3. **需要观察的现象**：外层是「层/子图」、内层是「batch/数据」，与朴素写法正好相反。
4. **预期结果**：你能用一句话说出「外层循环每推进一格，显存里就只留这一个子图的权重」。

#### 4.1.4 小练习与答案

**练习 1**：如果把两层循环交换顺序，权重峰值显存降了，但什么代价变大了？
**答案**：每个子图都要把全部 batch 跑一遍，子图之间的中间激活必须存下来；总的前向计算次数不变，但多了大量「层与层之间」的激活搬运与缓存开销，因此运行时间通常变长。

**练习 2**：为什么交换循环后必须有一个 `IntermediatesCache`？
**答案**：因为第 N 个子图的输出要留给第 N+1 个子图当输入，而这两层现在分处外层循环的不同迭代，中间结果必须被持久化保存（并 offload 出去省显存）。

---

### 4.2 Sequential Onloading：逐子图把权重搬到 GPU

#### 4.2.1 概念说明

「交换循环」只是思路，真正让「同一时刻只有一层权重在显存」落地的是一套基于 `compressed_tensors` 的 **offload/onload 机制**：

- 模型加载时，权重被分配到不同设备（CPU、甚至磁盘）。
- 当某个模块在前向中被访问时，它的权重被**透明地 onload（搬上）**到一块指定的 GPU。
- 用完之后，可以再 **offload（搬回）** 以腾出显存。

llm-compressor 在此之上加了两个控制：**onload 到哪块卡**，以及**在一个子图的处理期间不要反复搬**。

#### 4.2.2 核心流程

`SequentialPipeline.__call__` 开头三行决定了整个权重搬运策略：

[sequential/pipeline.py:84-87](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L84-L87) —— 这里读取主设备作为 `onload_device`、读取 `sequential_offload_device` 作为 `offload_device`，并调用 `set_onload_device(model, onload_device)` 把「权重被访问时应搬到哪块卡」这个全局设定注册到模型上。

接着进入逐子图循环。每个子图内部，用 `disable_offloading()` 把搬运「冻结」：

[sequential/pipeline.py:136-160](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L136-L160) —— 外层遍历子图；`with disable_offloading():` 包住「Calibrating」第一遍前向，注释明确写着「reduce memory movement by keeping modules onloaded」（让已 onload 的模块留在原地，减少搬运）。

`get_main_device` 决定 onload 到哪块卡：

[utils/dev.py:157-172](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/utils/dev.py#L157-L172) —— 优先返回当前加速器（CUDA/MPS），分布式时返回 `torch.device(accel_type, rank)`（每 rank 一块卡）；没有加速器则警告并退回 CPU。

整理成时序：

```
1. set_onload_device(model, GPU)            # 声明：被访问的权重搬到 GPU
2. for subgraph in subgraphs:               # 外层：逐子图（权重战线）
3.   with disable_offloading():             # 冻结搬运，避免反复横跳
4.     for batch in dataloader:             # 内层：逐 batch
5.       subgraph.forward(...)              # 首次访问时该子图权重被 onload 到 GPU
6.   sequential_epoch_end(subgraph.modules) # 触发该子图的权重量化
```

`disable_offloading` 的关键作用是：进入它之后，被 onloaded 的模块**不会在用完后被立即搬回 CPU/磁盘**，于是同一个子图处理多个 batch 时，权重只搬一次、常驻到这个子图结束。这直接对应源码注释「keeping modules onloaded」。

#### 4.2.3 源码精读：OOM 时的提示

如果选的 `sequential_targets` 太大（比如包含巨大的 MoE 层），单子图权重依然可能爆显存。这时 `handle_sequential_oom` 装饰器会把原始 OOM 包装成一条带建议的错误：

[pipelines/sequential/helpers.py:482-501](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/helpers.py#L482-L501) —— 捕获 `OutOfMemoryError`，建议把 `sequential_targets` 改小（如稠密模型用 `'Linear'`），MoE 模型则建议用「attention 类 + 单个 expert 类」组合并配合 `sequential_targets_per_subgraph`。

`sequential_targets` 的粒度直接决定「一次 onload 多少权重」：

[args/dataset_arguments.py:234-258](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/dataset_arguments.py#L234-L258) —— `sequential_targets`（默认回退到 HF 模型的 `no_split_params`，通常是一个 decoder 层）、`sequential_targets_per_subgraph`（每个子图含几个 target，默认 1，越大越快但越费显存）。

#### 4.2.4 代码实践：观察「子图数 ≈ 层数 + 1」

1. **实践目标**：直观感受 sequential onloading 把模型切成了多少块。
2. **操作步骤**：用一个极小的本地模型跑一次 GPTQ/sequential，在日志里数 `Calibrating` 进度条的段数（形如 `(1/N): Calibrating`）。
3. **需要观察的现象**：进度条段数 `N` 即子图数。
4. **预期结果**：对常见 decoder-only 模型，子图数约等于 decoder 层数 + 1（多出来的一个是开头不含 target 的子图，见 [helpers.py:159-166](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/helpers.py#L159-L166) 的注释与告警）。若你的环境没有 GPU，可改为源码阅读型实践：在 `trace_subgraphs` 返回处打印 `len(subgraphs)`（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 sequential onloading 默认就开启，而 basic pipeline 反而需要显式指定？
**答案**：因为 oneshot 在需要校准数据时默认就用 sequential 管线（见 u3-l5），而 sequential 管线天然就是 sequential onloading。basic 管线要求整个模型能放进显存，只在小模型且追求速度时才用，所以需要 `pipeline="basic"` 显式打开。

**练习 2**：把 `sequential_targets` 从「一个 decoder 层」改成 `'Linear'`，显存和耗时会怎么变？
**答案**：每个子图只含一个 Linear，单次 onload 的权重更少，**峰值显存更低**；但子图数大幅增加、循环开销与激活搬运次数变多，**耗时更长**。

---

### 4.3 激活的 offload 与预取：IntermediatesCache + sequential_prefetch

#### 4.3.1 概念说明

权重战线由 onloading 解决，激活战线由 `IntermediatesCache` 解决。它的设计哲学一句话：**写入缓存时立刻把张量 offload 到 `offload_device`（默认 CPU），读取时再 onload 回 GPU**。这样 GPU 上同一时刻只留正在用的那一个 batch 的激活，峰值与 batch 总数解耦。

#### 4.3.2 核心流程

缓存的构造入口在管线里：

[sequential/pipeline.py:119-121](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L119-L121) —— `IntermediatesCache.from_dataloader` 把整个 dataloader 的所有 batch 预先 offload 到 `offload_device`，建立缓存。

`from_dataloader` 逐 batch 调 `_offload_value` 把张量搬到 offload 设备：

[pipelines/cache.py:71-102](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/cache.py#L71-L102) —— 构造时遍历 dataloader，把每个 batch 的每个值 offload。

读路径 `fetch` 走 `_onload_value`：

[pipelines/cache.py:104-122](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/cache.py#L104-L122) 与 [pipelines/cache.py:303-324](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/cache.py#L303-L324) —— 取值时把张量 onload 回它的原始设备；当源是 pinned memory 且目标是加速器时用 `non_blocking` 传输，以便和计算重叠。

子图产出的新激活要回写缓存：

[sequential/pipeline.py:155-158](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L155-L158) —— 非 error-propagate 分支里，`activations.update(batch_idx, outputs)` 写回（随即被 offload），`activations.delete(batch_idx, subgraph.consumed_names)` 删掉那些后续子图不再需要的输入。

`consumed_names` 是省内存的关键：它由 `trace_consumed_names` 预先算出「每个中间值最后一次被哪个子图使用」，于是在最后一次消费后立即从缓存删除，避免无谓占用：

[pipelines/sequential/helpers.py:398-414](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/helpers.py#L398-L414) —— 反向遍历子图，给每个输入名标记「最后一次使用」的位置。

#### 4.3.3 两个旋钮：`sequential_offload_device` 与 `sequential_prefetch`

**旋钮一：`sequential_offload_device`** —— 激活 offload 到哪个设备。

[args/dataset_arguments.py:244-251](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/dataset_arguments.py#L244-L251) —— 默认 `"cpu"`；文档建议**多卡时用 `cuda:1`**：把激活 offload 到第二块卡，让主卡专心算权重、第二卡当「激活仓库」，避免 CPU↔GPU 的 PCIe 瓶颈。

注意区分两种 offload 目标：

| 设置 | 含义 | 适用 |
|------|------|------|
| `sequential_offload_device="cpu"`（默认） | 激活放 CPU 内存 | 单卡、CPU 内存充足 |
| `sequential_offload_device="cuda:1"` | 激活放第二块 GPU | 多卡，想用显存换速度 |

**旋钮二：`sequential_prefetch`** —— 是否在后台线程预取下一个 batch。

[args/dataset_arguments.py:292-299](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/args/dataset_arguments.py#L292-L299) —— 默认 `False`；开启后「在后台线程预取下一 batch，把 onload（H2D 拷贝）和当前 batch 的前向计算重叠」，代价是显存里同时存在两个 batch。

管线侧通过 `_get_batches` 选择普通迭代还是预取迭代：

[sequential/pipeline.py:27-48](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/sequential/pipeline.py#L27-L48) —— 根据 `sequential_prefetch` 在 `iter_prefetch` 与 `iter` 间二选一。

预取的实现用了一条专用 CUDA 流 + 后台线程：

[pipelines/cache.py:244-295](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/cache.py#L244-L295) —— 用 `ThreadPoolExecutor` 提前 fetch 下一个 batch，并在专用 `h2d_stream` 上做 H2D 拷贝，再用 CUDA `Event` 让主计算流等待拷贝完成，从而实现「取下一个」与「算当前」的重叠。

> 直觉：`sequential_prefetch` 是「用一点显存（多放一个 batch）换速度」，`sequential_offload_device` 是「换一个更快的仓库」。两者都服务激活战线。

#### 4.3.4 代码实践：对比 `sequential_prefetch` 开关

1. **实践目标**：体会预取对速度的影响。
2. **操作步骤**：对同一个模型 + 同一份校准数据，分别用 `sequential_prefetch=False`（默认）和 `sequential_prefetch=True` 跑两次 oneshot，记录耗时（可用 `time` 命令或 Python `time.perf_counter` 包住 `oneshot`）。
3. **需要观察的现象**：开启预取后，GPU 上同时会有两个 batch 的激活，显存略升；耗时通常略降。
4. **预期结果**：若显存有余，`sequential_prefetch=True` 更快；若显存紧张，开启后可能 OOM。无 GPU 环境可改为阅读 `iter_prefetch` 源码，画出「主线程算 batch i / 后台线程取 batch i+1」的时间线（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`consumed_names` 解决的是什么问题？
**答案**：中间激活在「最后一次被某子图使用」之前都必须保留，但用完之后若不删，会一直占着 offload 设备的空间。`consumed_names` 标记每个值最后一次使用的位置，使缓存能在最后消费后立即释放，降低 offload 设备（CPU/第二卡）的占用。

**练习 2**：为什么 `sequential_prefetch` 默认关闭？
**答案**：因为它会让显存里同时驻留两个 batch 的激活，对显存紧张的「大模型」场景不友好；只有显存有余、想加速时才打开。默认关 = 保守的内存优先策略。

---

### 4.4 更深的内存杠杆：磁盘 offloading 与 offload_hessians

#### 4.4.1 概念说明

到这里，权重放 CPU、激活放 CPU，但若**模型大到连 CPU 内存都装不下**怎么办？答案是**把权重进一步 offload 到磁盘**。这与 4.3 的「激活 offload」是两套独立机制，务必分清：

- **激活 offload**：`IntermediatesCache` + `sequential_offload_device`，搬的是校准中间激活，默认 CPU。
- **权重磁盘 offload**：`compressed_tensors` 的 `load_offloaded_model` + `device_map="auto_offload"`，搬的是模型权重，CPU 放不下时落盘。

#### 4.4.2 核心流程：权重磁盘 offload

加载时用 `load_offloaded_model` 上下文 + `device_map="auto_offload"`：

[docs/guides/big_models_and_distributed/model_loading.md:28-46](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/guides/big_models_and_distributed/model_loading.md#L28-L46) —— 文档给出标准范式：`with load_offloaded_model():` 里 `from_pretrained(..., device_map="auto_offload", max_memory={"cpu": ...}, offload_folder="./offload_folder")`，权重尽量放 CPU、放不下部分写进 `offload_folder` 磁盘目录。

llm-compressor 把它和 MoE 线性化打包成一个 `load_context`：

[utils/dev.py:35-52](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/utils/dev.py#L35-L52) —— `load_context` 用 `ExitStack` 同时进入 `load_offloaded_model`（支持权重 offload）和 `load_quantizable_moe`（加载时线性化 MoE 专家），一站式满足「超大模型 + MoE」的加载需求。

真实示例（用小模型模拟大模型的磁盘 offload）：

[examples/disk_offloading/qwen3_example.py:14-21](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/examples/disk_offloading/qwen3_example.py#L14-L21) —— 在 `load_context()` 内用 `device_map="auto_offload"`、`max_memory={"cpu": 6e8}`（故意把 CPU 配额设得很小）把权重逼到磁盘，`offload_folder="./offload_folder"` 指定落盘目录。

加载好之后，4.2 的 sequential onloading 会**透明地**把这些磁盘/CPU 权重在需要时 onload 到 GPU——`set_onload_device` 对磁盘 offload 的权重同样生效。也就是说：磁盘 offload 决定「权重平时住哪」，sequential onloading 决定「权重被用到时搬到哪、何时搬回」。

> 给初学者：`device_map` 的取值含义见 [model_loading.md:16-26](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/guides/big_models_and_distributed/model_loading.md#L16-L26) 的表格。sequential 管线推荐 `auto_offload`（CPU 装不下落磁盘）；basic/datafree 管线推荐 `auto`（直接尽量上 GPU）。

#### 4.4.3 第三战线：`offload_hessians`（算法状态）

GPTQ 校准时给每个模块存一份 Hessian \(H = 2X^{\top}X\)，是 hidden×hidden 的张量。对大模型，所有模块的 Hessian 加起来也是一笔不小的显存。`offload_hessians` 把它们常驻 CPU、用时再 onload：

[modifiers/gptq/base.py:130](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L130) —— 字段定义 `offload_hessians: bool = False`，docstring 写明「True = 更低内存、更长耗时」。

开启后，Hessian 创建在 CPU：

[modifiers/gptq/base.py:273-278](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L273-L278) —— `init_device = "cpu" if self.offload_hessians else get_execution_device(module)`，据此决定 `make_empty_hessian` 的初始设备。

用一个上下文管理器在「用之前 onload、用完后 offload」：

[modifiers/gptq/base.py:389-399](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L389-L399) —— `_maybe_onload_hessian`：进入时若开启则把 Hessian `.to(device)` 搬到模块所在 GPU，退出时再 `.to("cpu")` 搬回。

它在三处被调用，覆盖了 Hessian 的一生：

[modifiers/gptq/base.py:284-290](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L284-L290) —— `calibrate_module` 累积 Hessian 时 onload。
[modifiers/gptq/base.py:327-340](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L327-L340) —— `compress_modules` 调 `quantize_weight` 做量化时 onload。
[modifiers/gptq/base.py:350-356](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L350-L356) —— DDP 下 `reduce` 归约 Hessian 时 onload。

至此三条战线都有了「搬走省内存」的对应开关：

| 战线 | 省内存开关 | 平时住哪 | 用时搬到哪 |
|------|-----------|---------|-----------|
| 权重 | 磁盘 offload / sequential onloading | CPU / 磁盘 | GPU |
| 激活 | `sequential_offload_device` | CPU / 第二卡 | 主 GPU |
| 算法状态（Hessian） | `offload_hessians` | CPU | GPU |

#### 4.4.4 代码实践：跑通磁盘 offload 示例

1. **实践目标**：让权重真的落到磁盘，并确认 sequential onloading 仍能正常量化。
2. **操作步骤**：参考 [examples/disk_offloading/qwen3_example.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/examples/disk_offloading/qwen3_example.py) 跑一遍；跑完后检查 `./offload_folder` 目录是否产生了落盘文件。
3. **需要观察的现象**：脚本开头会打印 `Model was offloaded to the following devices: {...}`，其中应包含 `disk`（因为 `max_memory={"cpu": 6e8}` 故意压小了 CPU 配额）。
4. **预期结果**：量化能正常完成，证明磁盘上的权重被透明 onloading 到 GPU 参与校准；运行会比纯 CPU/GPU 慢（磁盘 IO 代价）。无磁盘写权限或无小模型时，可改为源码阅读：在 `load_context` 处标注它合并了哪两个上下文（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`offload_hessians` 为什么默认是 `False`？
**答案**：因为 Hessian 在 CPU↔GPU 之间反复搬运会显著拖慢 GPTQ（每个模块累积和量化都要搬一次）。默认关 = 速度优先；只有显存真的装不下所有模块的 Hessian 时才打开，用时间换内存。

**练习 2**：磁盘 offload 和 sequential onloading 是冲突还是配合？
**答案**：配合。磁盘 offload 决定「权重平时住磁盘」，sequential onloading 的 `set_onload_device` 在权重被前向访问时把它从磁盘→CPU→GPU 透明搬上来。两者叠加才能压缩「连 CPU 内存都装不下」的超大模型。

---

### 4.5 norm_calibration_context：超大/新架构的正确性保障

#### 4.5.1 概念说明

本模块讲的不是「省内存」，而是「省内存的代价下如何保证正确」。有些新架构（Gemma、Qwen3Next）的归一化层用的是**偏移归一化（offset norm）**：前向算的是 `output * (1 + weight)`，而不是标准的 `output * weight`。

这会破坏 AWQ、SmoothQuant 等平滑类算法——它们会「把 norm 的权重除以一个缩放因子 s」。对标准 norm，`weight / s` 是合法的等价变换；但对 offset norm，`(1 + weight) / s` 改变了那个 `1`，结果就错了。

`norm_calibration_context` 的办法是：**校准期间临时把 offset norm 换成标准 norm，校准完再换回去并把平滑后的权重转回 offset 约定**。

#### 4.5.2 核心流程

它是一个上下文管理器，三步走：

[modeling/offset_norm.py:96-149](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/offset_norm.py#L96-L149) —— 进入时扫描模型、把所有注册过的 offset-norm 模块替换成标准 norm 校准模块；`yield` 让校准跑完；退出时把平滑后的权重转回 offset 约定、恢复原模块。

替换规则用注册表 + 别名匹配类名：

[modeling/offset_norm.py:54-93](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/offset_norm.py#L54-L93) —— `CalibrationOffsetNorm` 注册了 `GemmaRMSNorm` 及一串别名（`Gemma2RMSNorm`、`Gemma3RMSNorm`、`Qwen3NextRMSNorm` 等）。进入时 `self.weight = 1 + original.weight`（把 offset 变成乘性 weight），退出 `restore` 时 `original.weight = self.weight - 1`（再变回 offset）。

数学上，替换保证了等价性：

- 原 offset norm：\(\text{out} = \text{RMSNorm}(x) \cdot (1 + w)\)
- 校准期标准 norm：\(\text{out} = \text{RMSNorm}(x) \cdot w'\)，其中 \(w' = 1 + w\)
- 平滑算法把 \(w'\) 除以 \(s\) 得到 \(w'_{\text{new}}\)
- 退出时还原：\(w_{\text{new}} = w'_{\text{new}} - 1\)，于是 \((1 + w_{\text{new}}) = w'_{\text{new}}\)，平滑效果被正确保留。

#### 4.5.3 它和「大模型」的关系：挂在 oneshot 的校准全程

`norm_calibration_context` 在 `oneshot` 里包住了整个校准过程（和 MoE 专家校准上下文并列）：

[entrypoints/oneshot.py:242-245](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L242-L245) —— `with ExitStack() as stack:` 里 `stack.enter_context(norm_calibration_context(self.model))`，然后才 `session.initialize(...)` 和跑管线。

之所以放进「大模型内存策略」这一讲，是因为大模型往往也是**最新架构**（Gemma3、Qwen3Next 等），它们既需要内存策略压缩、又需要这个上下文保证平滑算法正确。两个需求在 oneshot 里同时满足。

#### 4.5.4 代码实践：识别你的模型是否命中 offset norm

1. **实践目标**：判断一个模型是否触发了 norm 替换。
2. **操作步骤**：加载一个 Gemma 系或 Qwen3Next 模型，在 `with norm_calibration_context(model):` 前后各打印一次某个 RMSNorm 层的 `weight` 前几个值；或者直接跑 oneshot 观察日志是否出现 `Found N offset-norm modules to convert`（见 [offset_norm.py:131](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/offset_norm.py#L131)）。
3. **需要观察的现象**：进入上下文后 weight = 1 + 原 weight；退出后又变回 weight − 1。
4. **预期结果**：Gemma/Qwen3Next 会触发替换；普通 Llama/Qwen 不会（它们的 RMSNorm 本就是标准乘性）。无对应模型时可改为阅读 `_is_registered`（[offset_norm.py:152-156](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/offset_norm.py#L152-L156)），理解类名如何经 `standardize_lookup_name` 匹配注册表与别名（待本地验证）。

#### 4.5.5 小练习与答案

**练习 1**：如果不用 `norm_calibration_context`，直接对 Gemma 的 RMSNorm 跑 SmoothQuant 会怎样？
**答案**：SmoothQuant 会把 RMSNorm 的权重除以缩放因子 s，但 Gemma 的 RMSNorm 实际算的是 `output * (1 + weight)`，除以 s 破坏了那个常数 1，导致平滑后的输出与原模型不等价，量化精度受损。

**练习 2**：`norm_calibration_context` 是「永久替换」还是「临时替换」？依据是什么？
**答案**：临时替换。`CalibrationOffsetNorm.is_permanent = False`（[offset_norm.py:73](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modeling/offset_norm.py#L73)），且上下文 `finally` 块里调 `restore` 把原模块换回，并按 `weight - 1` 把权重转回 offset 约定。

---

## 5. 综合实践

**任务**：用 `sequential_offload_device` 与 `offload_hessians` 两个开关的四种组合，量化**同一个**小模型，记录峰值显存与耗时，画出 memory/speed 权衡曲线。

**为什么选这两个开关**：它们分别服务「激活战线」和「算法状态战线」，互不干扰，组合起来能清楚看到不同战线省内存的边际效果。

**操作步骤**：

1. 选一个本地可得的小模型（如 `Qwen/Qwen3-0.6B`）和一个需校准数据的算法（如 GPTQ 的 `W4A16`，这样 `offload_hessians` 才有意义）。
2. 写一个脚本，对下面四种组合各跑一次 `oneshot`，用 `torch.cuda.max_memory_allocated()`（在 `oneshot` 前后 `torch.cuda.reset_peak_memory_stats()`）记录峰值显存，用 `time.perf_counter()` 记录耗时：

   | 组合 | `sequential_offload_device` | `offload_hessians` |
   |------|-----------------------------|--------------------|
   | A 基线 | `"cpu"` | `False` |
   | B 激活省内存 | `"cpu"` | `True` |
   | C（多卡才有意义） | `"cuda:1"` | `False` |
   | D 全省 | `"cpu"` | `True` |

3. 把四组结果画成「峰值显存 vs 耗时」散点图。

**需要观察的现象**：

- 从 A→B（打开 `offload_hessians`）：峰值显存下降，耗时上升（Hessian 反复搬运）。
- 从 A→C（激活放第二卡，需多卡）：耗时下降（避开 PCIe 瓶颈），第二卡显存上升。
- D 最省主卡显存但最慢。

**预期结果**：你会得到一条清晰的「省内存就要花时间」的权衡曲线。这正是本讲所有开关的共同本质——**用搬运时间换峰值内存**。无 GPU/无多卡环境时，可退化为源码阅读型实践：画出三种 offload（权重磁盘、激活 CPU/第二卡、Hessian CPU）的数据流图，标注每种搬运用到的源码函数。

> 提示：若用 GPTQ，recipe 形如 `GPTQModifier(scheme="W4A16", targets="Linear", ignore=["lm_head"], offload_hessians=...)`；`sequential_offload_device` 是 `oneshot` 的关键字参数（属 `DatasetArguments`）。

## 6. 本讲小结

- 压缩超大模型要同时管好**权重、激活、算法状态**三条内存战线，每条都有对应的「搬走省内存」开关。
- **Sequential onloading** 的核心是「交换两层 for 循环」：外层遍历子图、内层遍历 batch，使同一时刻只有一份子图权重在显存；`set_onload_device` 声明搬运目标，`disable_offloading` 让权重在子图处理期间常驻不回搬。
- **激活战线**由 `IntermediatesCache` 负责：写即 offload 到 `sequential_offload_device`（默认 CPU，多卡建议 `cuda:1`）、读即 onload；`consumed_names` 在最后一次使用后立即释放；`sequential_prefetch` 用后台线程 + 专用 CUDA 流把取下一 batch 和算当前 batch 重叠，用一点显存换速度。
- **磁盘 offloading**（`load_offloaded_model` + `device_map="auto_offload"`）是权重战线的最后一道防线，CPU 内存也装不下时让权重落盘；它与 sequential onloading 配合而非冲突。`load_context` 把它和 MoE 线性化打包。
- **`offload_hessians`** 服务算法状态战线：把 GPTQ 的 Hessian 常驻 CPU、用 `_maybe_onload_hessian` 在累积/量化/归约时临时 onload，用时间换显存。
- **`norm_calibration_context`** 不是内存策略，而是超大模型常伴的新架构（Gemma/Qwen3Next 的 offset norm）正确性保障：校准期临时把 `output*(1+w)` 换成标准 `output*w'`，让平滑类算法正确，退出再转回。

## 7. 下一步学习建议

- 想看「省内存 + 多卡」如何叠加，继续学 **u6-l1 DDP 分布式量化**：它讲 sequential 管线在多卡下如何 all-reduce 激活统计、用贪心装箱并行压缩，与本讲的内存策略正交组合。
- 想了解压缩产物如何落盘成 vLLM 可加载的格式，学 **u6-l3 保存与 compressed-tensors 格式**。
- 想深入 sequential 管线本身的「切子图、两遍前向、误差传播」细节，回看 **u3-l5 SequentialPipeline 逐层校准深析**（本讲的依赖）。
- 建议延伸阅读：[docs/guides/big_models_and_distributed/](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/guides/big_models_and_distributed/) 目录下的 `sequential_onloading.md`、`model_loading.md`、`distributed_oneshot.md` 三篇文档，以及示例 `examples/disk_offloading/`、`examples/big_models_with_sequential_onloading/`。
