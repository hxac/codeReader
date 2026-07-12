# CUDA Graph 捕获与回放

## 1. 本讲目标

本讲聚焦 nano-vllm 在 decode（逐 token 生成）阶段的最后一项加速手段：**CUDA Graph**。学完后你应当能够：

- 说清「kernel launch 开销」是什么，以及为什么 decode 阶段特别受它拖累。
- 读懂 `ModelRunner.capture_cudagraph`，理解它如何为「多档 batch size」各预录一张计算图，以及为什么要反向捕获、为什么要 warmup、为什么要共享 graph pool。
- 读懂 `ModelRunner.run_model`，理解它如何在「走图回放」与「走 eager」之间分流，以及 `graph_vars` 这组静态张量如何在回放前被填充、回放后被读取。
- 能够动手对比 `enforce_eager=True` 与 `False` 的 decode 吞吐，验证 CUDA Graph 的实际收益。

本讲只覆盖两个最小模块：`ModelRunner.capture_cudagraph` 与 `ModelRunner.run_model`，全部源码集中在 [nanovllm/engine/model_runner.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py)。

## 2. 前置知识

### 2.1 kernel launch 开销

GPU 上的每一次计算（一个矩阵乘、一个激活函数、一次 softmax）都对应一次「kernel 启动」：CPU 要把指令组装好、通过驱动下发给 GPU，GPU 才开始执行。这个「CPU 端准备 + 驱动转发」的过程叫 **kernel launch 开销**，通常每个 kernel 几微秒。

Qwen3 这样的模型一次前向会触发上百个 kernel。在 **prefill** 阶段，每个 kernel 处理成百上千个 token，计算量大、耗时长，launch 开销占比可以忽略；但在 **decode** 阶段，每条序列每步只生成 1 个 token（见 u4-l1），每个 kernel 实际只处理很小的工作量，计算本身极快，于是「CPU 启动 kernel 的时间」反而可能逼近甚至超过「GPU 真正计算的时间」。

这就是 decode 阶段的典型瓶颈：**GPU 在等 CPU 把 kernel 一个个发下来**。

### 2.2 CUDA Graph 是什么

CUDA Graph 是 CUDA 提供的一种机制：把一整串 kernel 的启动顺序与参数「录制」下来，封装成一个图对象；之后只要 `replay()`（回放）一次，CPU 就能用一次调用把整串 kernel 全部重新提交给 GPU，几乎消灭逐个 launch 的开销。

直觉上：

\[ T_{\text{step}} \approx T_{\text{launch}} + T_{\text{compute}} \]

- 普通执行：每步都要付出 \(T_{\text{launch}}\)（上百次 launch 之和）。
- CUDA Graph 回放：\(T_{\text{launch}}\) 被压缩到接近一次 launch 的代价。

当 \(T_{\text{compute}}\) 很小（decode 小 batch）时，这个压缩带来的相对加速最显著。

### 2.3 为什么 decode 适合、prefill 不适合

- **decode**：每步对每条序列只处理 1 个 token，整步的形状只随「本步有几条序列在跑」（即 batch size）变化。batch size 取值范围有限，可以预先为每一档 batch size 录一张图。
- **prefill**：用 varlen 把不等长序列打包（见 u4-l1），每步的 `cu_seqlens`、序列数、总 token 数千变万化，无法穷举所有形状去录图。所以 nano-vllm 里 **prefill 永远走 eager，只有 decode 才用图**。

### 2.4 静态张量是 CUDA Graph 的硬约束

CUDA Graph 录制时绑定了**具体的张量对象（内存地址）**。回放时不能换入新的张量，只能**把新数据写进原来那块内存**，再回放。这意味着我们必须预先分配一组「静态输入张量」与「静态输出张量」，这组张量在 nano-vllm 里就叫 `graph_vars`。理解这一点，才能看懂 `run_model` 里那一串「`graph_vars[...] = ...`」的拷贝操作。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [nanovllm/engine/model_runner.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py) | 本讲全部源码。`capture_cudagraph` 预录多档图；`run_model` 在 decode 时回放；`__init__`/`exit` 控制捕获与清理的时机。 |
| [nanovllm/utils/context.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/utils/context.py) | `Context` 全局元数据。捕获与回放都依赖它把 `slot_mapping`/`context_lens`/`block_tables` 送到 Attention。 |
| [nanovllm/layers/embed_head.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/embed_head.py) | `ParallelLMHead`。它的 `dist.gather` 是 `compute_logits` 被排除在图外的关键原因。 |
| [bench.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/bench.py) | 吞吐基准脚本，用于本讲综合实践。 |

## 4. 核心概念与源码讲解

### 4.1 模块一：capture_cudagraph —— 预录多档 batch 计算图

#### 4.1.1 概念说明

`capture_cudagraph` 在引擎启动时（`enforce_eager=False` 的前提下）一次性跑完，产物是两个字典/对象：

- `self.graphs`：一个 `{batch_size: CUDAGraph}` 的映射，为每一档 batch size 存一张预录好的图。
- `self.graph_vars`：一组**静态张量**（`input_ids`、`positions`、`slot_mapping`、`context_lens`、`block_tables`、`outputs`），它们既是「捕获时」图的输入输出，也是「回放时」我们要往里填数据的容器。

这里有四个关键设计决策，理解了它们就理解了整个函数：

1. **分档捕获（graph_bs）**：decode 的 batch size 每步都在变（有序列结束、有新序列晋升），不可能为 1~512 的每个整数各录一张。nano-vllm 选了一组「分档点」`[1, 2, 4, 8, 16, 32, 48, ..., max_bs]`，回放时把实际 batch size **向上取整到最近的分档点**。这样只需录有限张图（默认配置下 36 张），代价是「实际 bs=5 时用 bs=8 的图多算 3 条空序列」。

2. **反向捕获 + 共享 graph pool**：PyTorch 的 CUDA Graph 支持让多张图共享同一块显存池（`graph_pool`）。标准做法是**先捕获最大的那张图**，让它建立池子，后续小图都挂到这个池上，从而大幅省显存。因此循环用 `reversed(self.graph_bs)`，从最大的 `max_bs` 开始录，第一张图的 `graph.pool()` 被存为 `self.graph_pool`，之后所有图复用它。

3. **捕获前必须 warmup**：CUDA Graph 不能直接捕获一个「冷启动」的计算。cuBLAS/cuDNN 这类库会在首次调用时惰性分配 workspace、创建 handle，这些一次性副作用如果在捕获期间发生，会破坏图的录制。因此每档 bs 在真正捕获前，都先用同样的输入**跑一遍 warmup**，把所有惰性初始化提前消化掉。

4. **静态张量是图的「插口」**：捕获时执行的是 `outputs[:bs] = self.model(input_ids[:bs], positions[:bs])`。注意这是**切片赋值**，写入的是预先分配的静态 `outputs` 张量；输入也是静态的 `input_ids`/`positions`。于是这张图的所有输入输出都被「钉」在了 `graph_vars` 这组张量上，回放时只需改这些张量的内容即可。

#### 4.1.2 核心流程

`capture_cudagraph` 的执行流程可用如下伪代码概括：

```text
max_bs        = min(max_num_seqs, 512)              # 永远不超过 512
max_num_blocks = ceil(max_model_len / block_size)   # block_tables 的列数上限

# 1. 预分配静态张量（形状都按 max_bs 取上限）
input_ids   : [max_bs]
positions   : [max_bs]
slot_mapping: [max_bs]
context_lens: [max_bs]
block_tables: [max_bs, max_num_blocks]
outputs     : [max_bs, hidden_size]

graph_bs = [1, 2, 4, 8] + range(16, max_bs+1, 16)   # 分档点
graphs = {}; graph_pool = None

# 2. 反向遍历：从最大档到最小档
for bs in reversed(graph_bs):
    graph = CUDAGraph()
    set_context(decode 元数据的静态切片)             # 让 Attention 能读到 slot_mapping 等
    outputs[:bs] = model(input_ids[:bs], ...)       # warmup（必须）
    with cuda.graph(graph, graph_pool):             # 开始捕获
        outputs[:bs] = model(input_ids[:bs], ...)   # 这一行被录进图
    if graph_pool is None:
        graph_pool = graph.pool()                   # 第一张（最大）图捐出池子
    graphs[bs] = graph
    synchronize(); reset_context()

# 3. 把静态张量收进 graph_vars，供回放时填充
graph_vars = {input_ids, positions, slot_mapping, context_lens, block_tables, outputs}
```

几个要点：

- **捕获的是 `self.model(...)`，即 `Qwen3Model.forward`（embed + 各 decoder 层 + 末 norm）**，返回 hidden_states 写进 `outputs`。`lm_head`（`compute_logits`）**不在图内**，留到回放之后单独算（原因见 4.2.1）。
- 捕获时用 `set_context(False, slot_mapping=..., context_lens=..., block_tables=...)` 设置 decode 语义的元数据（见 u4-l3），因为 Attention 在前向里会通过 `get_context()` 读这些字段。
- 每档 bs 捕获完都 `torch.cuda.synchronize()` 确保落盘，再 `reset_context()` 清理，避免污染下一档。

#### 4.1.3 源码精读

捕获的触发点在构造函数里：仅当 `enforce_eager=False` 时才调用，且必须排在 `warmup_model` 与 `allocate_kv_cache` 之后——因为捕获要跑真实前向，需要权重、KV cache 视图都已就位：

[model_runner.py:36-39](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L36-L39) —— 捕获的触发与默认 dtype/device 的恢复。

下面是捕获函数本体。先确定上限与预分配静态张量：

[model_runner.py:222-233](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L222-L233) —— `max_bs = min(max_num_seqs, 512)` 给 batch 上限封顶；`max_num_blocks` 由 `max_model_len` 换算，决定 `block_tables` 的列数；6 个静态张量全部按 `max_bs`（与 `max_num_blocks`）分配上限形状。

分档点列表与字典初始化：

[model_runner.py:234-236](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L234-L236) —— `graph_bs` 小 batch 段用细粒度 `[1,2,4,8]`（小 batch 对延迟最敏感、最常出现），大 batch 段每 16 一档以控制图的总数；`graph_pool` 初始为 `None`，等第一张图来填充。

核心捕获循环（反向遍历）：

[model_runner.py:238-248](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L238-L248) —— 对每一档 `bs`：新建 `CUDAGraph`；用静态张量的切片 `set_context` 设置 decode 元数据；先跑一次 warmup（L241）；再在 `torch.cuda.graph(graph, self.graph_pool)` 上下文里捕获同一行计算（L242-L243）；首张图（因 `reversed` 而是 `max_bs` 那张）把池子存为 `self.graph_pool`（L244-L245）供后续共享；存图、同步、清理。

> 关于「为什么是 `reversed`」：PyTorch 要求被共享 pool 的图，其内存需求不能超过建池那张图。最大 bs 的图内存占用最大，所以必须由它建池；`reversed` 保证了第一轮就是 `max_bs`。

收尾：把静态张量收进 `graph_vars`，供回放时按名引用：

[model_runner.py:250-257](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L250-L257) —— 注意这些就是上面分配的那 6 个张量对象本身（不是副本），所以回放时改它们的内容，图就能读到新输入。

引擎退出时，捕获产物被显式释放：

[model_runner.py:56-57](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L56-L57) —— `del self.graphs, self.graph_pool` 释放图与池，避免进程退出时报显存泄漏。

#### 4.1.4 代码实践

**实践目标**：在不跑 GPU 的前提下，推导默认配置下 nano-vllm 会捕获多少张图、分别是哪些 batch size，从而把「分档捕获」从概念变成可计算的数字。

**操作步骤**：

1. 打开 [config.py:9-18](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L234-L236) 确认 `max_num_seqs` 默认值（512）。
2. 据此推 `max_bs = min(max_num_seqs, 512)`。
3. 用 Python 在本地（无需 GPU，纯算术）枚举 `graph_bs`：
   ```python
   max_bs = 512
   graph_bs = [1, 2, 4, 8] + list(range(16, max_bs + 1, 16))
   print(graph_bs)
   print("共", len(graph_bs), "张图")
   ```

**需要观察的现象 / 预期结果**：

- `range(16, 513, 16)` = `[16, 32, 48, ..., 512]`，共 32 个值。
- 加上 `[1, 2, 4, 8]`，`graph_bs` 共 **36** 个分档点，即默认会捕获 **36 张图**。
- 第一个被捕获的是 `reversed(graph_bs)` 的第一个元素，即 `512`；它建立 `graph_pool`。

> 上述数值为静态推导结果，无需 GPU 即可验证。若你改大 `max_num_seqs`，由于 `max_bs` 被 `min(..., 512)` 封顶，图的数量不会继续增长——这是 `>512` 时回退 eager 的直接原因（见 4.2）。

#### 4.1.5 小练习与答案

**练习 1**：为什么循环是 `reversed(self.graph_bs)`，而不是正序？如果改成正序会发生什么？

**参考答案**：因为多图要共享同一个 `graph_pool`，而 PyTorch 要求池子的容量由「内存占用最大的那张图」建立。batch size 越大、图越大，所以必须先捕获 `max_bs` 那张最大的图来建池。`reversed` 把 `graph_bs` 从大到小遍历，保证第一轮就是 `max_bs`。若改正序（从小到大），第一张图（bs=1）建出的池子容量太小，后续大图复用该池时会因内存不足而报错。

**练习 2**：捕获循环里 L241 的 warmup 和 L243 的捕获执行的是同一行代码 `outputs[:bs] = self.model(input_ids[:bs], positions[:bs])`。既然一样，为什么必须跑两次？

**参考答案**：第一次是 warmup，目的是触发 cuBLAS/cuDNN 等库的惰性初始化（分配 workspace、创建 handle），把这些一次性的副作用在捕获上下文之外消化掉。若省掉 warmup 直接捕获，这些副作用会被录进图或干扰录制导致出错。两次执行的代码相同，但语义不同：一次是「热身」，一次是「正式录制」。

**练习 3**：捕获时执行的是 `self.model(...)`，即 `Qwen3ForCausalLM.forward`，但它返回的是 hidden_states（见 u4-l4）。最终的 logits 计算在哪里？为什么没有一起录进图？

**参考答案**：logits 由 `self.model.compute_logits(...)` 计算（内部调用 `ParallelLMHead`），它在 `run_model` 里、`graph.replay()` **之后**才执行，没有被录进图。原因是 `ParallelLMHead.forward` 在张量并行（`tp_size>1`）时会执行 `dist.gather` 这种跨 rank 的集合通信（见 [embed_head.py:62-65](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/embed_head.py#L62-L65)），把 NCCL 通信录进 CUDA Graph 既复杂又脆弱；何况采样只需 rank 0 拿到 logits。因此只录「transformer 主体」，把 LM 头与采样留在图外。

---

### 4.2 模块二：run_model —— 图/eager 分流与 graph_vars 回放

#### 4.2.1 概念说明

`run_model` 是每一步推理真正调用模型的地方。它在「走图」与「走 eager」之间做分流，并在走图时完成「把当前步的数据填进静态张量 → 回放 → 从静态输出张量取结果」三件事。

**分流判据**只有一行：

```python
if is_prefill or self.enforce_eager or input_ids.size(0) > 512:
    # eager
else:
    # graph
```

也就是说，只要满足以下任一条件就走 eager：

1. **`is_prefill=True`**：prefill 形状多变，不录图（见 2.3）。
2. **`enforce_eager=True`**：用户在 `Config` 里显式要求禁用图（构造 `LLM` 时传入）。
3. **`input_ids.size(0) > 512`**：本步 batch size 超过 `max_bs` 上限，根本没有对应的图（见 4.1.4 的 `min(..., 512)`）。

只有在 decode 且 batch size ≤ 512 且未禁用图时，才走 CUDA Graph 回放。

**回放的核心机制：copy-in / replay / copy-out。** 因为图绑定了静态张量，回放不能换张量，只能改内容：

- **copy-in**：把当前步真实的 `input_ids`、`positions`、以及从 `get_context()` 取到的 `slot_mapping`/`context_lens`/`block_tables`，**拷贝进** `graph_vars` 里对应的静态张量。
- **replay**：调用 `graph.replay()`，整张图一次性重放，结果写进静态 `outputs`。
- **copy-out**：从 `graph_vars["outputs"][:bs]` 取出 hidden_states，再交给 `compute_logits` 算 logits。

**分档对齐**：实际 batch size `bs` 用 `next(x for x in self.graph_bs if x >= bs)` 向上取整到最近的分档点 `bucket`，回放的是 `self.graphs[bucket]`。这意味着图总是按 `bucket`（≥ bs）规模跑，多余出来的 `bucket - bs` 行是「填充行」。这些填充行必须被处理成**无害**的：

- `slot_mapping` 先 `fill_(-1)` 再写 `[:bs]`，于是填充行的槽位是 `-1`。`store_kvcache_kernel` 见到 `slot=-1` 会跳过（见 u4-l2），**不会污染 KV cache**。
- `context_lens` 先 `zero_()` 再写 `[:bs]`，填充行的历史长度为 0，attention 读不到任何 key。
- `input_ids`/`positions`/`block_tables` 只写 `[:bs]`，填充行保留旧值，但其输出在 `[:bs]` 切片时被丢弃，不影响结果。

这正是 `slot=-1` 这个「哨兵值」在工程上的真正用途：**让填充行在图回放时静默、不写脏数据**。

#### 4.2.2 核心流程

```text
run_model(input_ids, positions, is_prefill):
    if is_prefill or enforce_eager or len(input_ids) > 512:
        # eager：一次性算完主体 + LM 头
        return compute_logits(model(input_ids, positions))

    # ---- graph 路径 ----
    bs      = len(input_ids)
    context = get_context()                                # 本步 decode 元数据
    bucket  = next(x for x in graph_bs if x >= bs)         # 向上取整到分档点
    graph   = self.graphs[bucket]

    # copy-in：把本步数据写进静态张量
    graph_vars["input_ids"][:bs]   = input_ids
    graph_vars["positions"][:bs]   = positions
    graph_vars["slot_mapping"].fill_(-1)                   # 填充行置哨兵
    graph_vars["slot_mapping"][:bs] = context.slot_mapping
    graph_vars["context_lens"].zero_()
    graph_vars["context_lens"][:bs] = context.context_lens
    graph_vars["block_tables"][:bs, :ncols] = context.block_tables

    graph.replay()                                         # 整张图一次性回放

    # copy-out：取静态输出，单独算 LM 头
    return compute_logits(graph_vars["outputs"][:bs])
```

注意 eager 与 graph 两条路径的对称性：两者最终都返回 `compute_logits(...)`，区别只在「主体前向」是即时算（eager）还是回放得到（graph）。这保证了两条路径对调用方 `run` 完全等价。

#### 4.2.3 源码精读

函数签名与分流判据：

[model_runner.py:195-198](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L195-L198) —— `@torch.inference_mode()` 装饰整个函数；三分支判据 `is_prefill or enforce_eager or size(0) > 512` 命中任一则 eager，主体与 LM 头一次性算完返回。

graph 路径的分档选择与 copy-in：

[model_runner.py:200-210](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L200-L210) —— L202 用生成器表达式 `next(x for x in self.graph_bs if x >= bs)` 选档；L204-L210 把本步数据拷进静态张量。重点看 L206 的 `slot_mapping.fill_(-1)` 与 L208 的 `context_lens.zero_()`：它们先把整段清成「哨兵值」，再用 `[:bs]` 覆盖有效部分，确保填充行无害。L210 的 `block_tables` 只覆盖 `[:bs, :context.block_tables.size(1)]`，列方向按本步实际块数对齐。

回放与取结果：

[model_runner.py:211-212](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L211-L212) —— `graph.replay()` 一次性重放整张图，hidden_states 落进静态 `outputs`；随后 `compute_logits(graph_vars["outputs"][:bs])` 只取有效行算 LM 头。`compute_logits` 在 replay 之后、图之外执行，避开了 NCCL gather 的捕获难题。

调用方 `run`（u4-l1 已讲）把 `run_model` 夹在 `prepare_*` 与 `reset_context` 之间，每步封口：

[model_runner.py:214-220](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L214-L220) —— `prepare_decode` 已在本步调 `set_context(False, ...)` 写好元数据，所以 `run_model` 里 `get_context()` 能取到 `slot_mapping`/`context_lens`/`block_tables`；`reset_context()` 收尾保证 Context 每步不留残留。

#### 4.2.4 代码实践

**实践目标**：通过追踪一个具体 batch size，把「分档对齐」与「填充行无害化」从代码读成可手算的映射关系。

**操作步骤**：

1. 假设某一步 decode 同时在跑 5 条序列（`bs=5`）。
2. 求 `bucket = next(x for x in self.graph_bs if x >= 5)`，其中 `graph_bs = [1,2,4,8,16,...]`。显然 `bucket = 8`。
3. 在纸上画出 `graph_vars["slot_mapping"]`（长度 8）在 L206-L207 执行后的状态：索引 `0..4` 是本步 5 条序列的真实槽位，索引 `5..7` 是 `-1`。
4. 同理画出 `context_lens`：`[:5]` 是 5 条序列各自的总长，`[5:8]` 是 0。
5. 阅读并对照 u4-l2 的 `store_kvcache_kernel`：确认它对 `slot=-1` 的索引直接跳过、不写 `k_cache`/`v_cache`。

**需要观察的现象 / 预期结果**：

- bs=5 回放的是 `self.graphs[8]`，图按 8 行的规模执行。
- 填充行（第 6~8 行）的 `slot=-1` 让 Triton 内核跳过写 KV，`context_lens=0` 让 attention 读不到历史，因此它们不产生任何副作用。
- 最终只取 `graph_vars["outputs"][:5]` 算 logits，填充行的输出被丢弃。

> 结论：向上取整带来的「多算几条空序列」是可控、无害的，这是用有限张图覆盖任意 batch size 的代价。

#### 4.2.5 小练习与答案

**练习 1**：某一步 decode 的 `bs=20`，会回放哪一档图？多算了几行填充？

**参考答案**：`next(x for x in graph_bs if x >= 20)`：`graph_bs = [1,2,4,8,16,32,48,...]`，≥20 的最小分档点是 32。所以回放 `self.graphs[32]`，多算 `32-20=12` 行填充。填充行的 `slot_mapping` 为 `-1`、`context_lens` 为 0，不污染 cache、不影响 `[:20]` 的输出。

**练习 2**：为什么 `block_tables` 的写入是 `[:bs, :context.block_tables.size(1)]`，而不是像 `context_lens` 那样先清零再写？

**参考答案**：因为 `context_lens=0` 已经让填充行的 attention 读不到任何 key，填充行读哪一块都已经无所谓；而 `block_tables` 里存的是物理块号，没有「哨兵值」能像 `-1` 之于 `slot_mapping` 那样表达「跳过」。所以代码只在列方向覆盖本步实际用到的块数（`context.block_tables.size(1)`），行方向的填充行干脆不管——它们已被 `context_lens=0` 与最终 `[:bs]` 切片双重屏蔽。

**练习 3**：如果把 `run_model` 的判据里的 `input_ids.size(0) > 512` 改成 `> 256`，会发生什么？是否正确？

**参考答案**：会变得**不正确**（或者说过于保守）。捕获时 `max_bs = min(max_num_seqs, 512)`，默认录到了 bs=512 这一档；若判据改成 `>256`，则 bs 在 `(256, 512]` 区间的 decode 步会被误判成「没有图」而回退 eager，白白浪费了已捕获的大档图、损失性能。判据里的 `512` 必须与 `capture_cudagraph` 里的 `max_bs` 上限保持一致，这正是两处都出现 `512` 的原因。

## 5. 综合实践

**实践目标**：用 `bench.py` 对比 `enforce_eager=True` 与 `False` 的端到端 decode 吞吐，并在 `graph.replay()` 前后打点，量化 CUDA Graph 对单步 decode 的加速。

**操作步骤**：

1. 准备好 Qwen3-0.6B 权重（路径与 `example.py` 一致，见 u1-l1），确保有 GPU 环境。
2. **基准运行（开图）**：直接运行 `bench.py`（其内部已是 `enforce_eager=False`），记录输出的 `Throughput`（tok/s）：
   ```bash
   python bench.py
   ```
3. **对照运行（禁图）**：把 [bench.py:15](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/bench.py#L15) 的 `enforce_eager=False` 改为 `enforce_eager=True` 后再跑一次，记录吞吐。
4. **单步打点（可选，需临时插桩）**：在 [model_runner.py:211](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L211) 的 `graph.replay()` 前后各加一行：
   ```python
   torch.cuda.synchronize(); t0 = time.perf_counter()
   graph.replay()
   torch.cuda.synchronize(); print("decode step:", (time.perf_counter() - t0) * 1000, "ms")
   ```
   （需 `import time`，且这是**临时插桩**，验证完请还原，不要提交。）对照地，在 eager 路径（L198）前后同样打点。
5. 对比两组数据：开图 vs 禁图的吞吐比、单步 decode 耗时比。

**需要观察的现象 / 预期结果**：

- 启用 CUDA Graph 后，decode 吞吐应**明显高于** `enforce_eager=True`；单步 decode 耗时应明显降低。batch size 越小、提升比例越显著（因为小 batch 下 launch 开销占比更大）。
- 若观察到提升不明显，可能是 batch size 较大（计算本身就慢，launch 占比小）或 prefill 占总时间比重过大。
- 由于本环境无 GPU，具体数值**待本地验证**；请不要把任何编造的数字写进结论。

## 6. 本讲小结

- decode 阶段每步只处理极少 token，瓶颈常在 **kernel launch 开销**而非计算本身；CUDA Graph 把整串 kernel 录成一张图，回放时一次提交，几乎消灭 launch 开销。
- nano-vllm 只对 **decode** 用图，prefill 因形状多变始终走 eager；`run_model` 用 `is_prefill or enforce_eager or size(0)>512` 三选一决定是否走图。
- `capture_cudagraph` 为「分档点」`[1,2,4,8,16,...,max_bs]` 各录一张图，`max_bs=min(max_num_seqs,512)`；用 `reversed` 从最大档开始、由首张图捐出共享的 `graph_pool`；每档捕获前必跑 warmup 消化惰性初始化。
- 图绑定静态张量，故回放走「copy-in → replay → copy-out」：把本步数据写进 `graph_vars` 的静态张量，回放后从静态 `outputs` 取结果。
- 实际 batch size 向上取整到分档点，多出的填充行靠 `slot_mapping.fill_(-1)`（Triton 内核跳过）与 `context_lens.zero_()`（attention 读空）保证无害——这正是 u4-l2 中 `slot=-1` 哨兵值的工程用途。
- LM 头（`compute_logits`）留在图外，因为 `ParallelLMHead` 在张量并行下含 `dist.gather` 跨 rank 通信，不适合录进图。

## 7. 下一步学习建议

- 读完本讲，nano-vllm 的「模型执行」侧（u4 全单元）与「CUDA Graph」已闭环。下一讲 **u5-l2 torch.compile 算子融合** 将从另一个角度继续优化模型执行：用 `@torch.compile` 把 RMSNorm、RoPE、SiluAndMul 等小算子融合，进一步减少 kernel 数量——它与本讲的 CUDA Graph 是互补关系（一个减 launch、一个融合小算子），建议对照学习。
- 想深入理解张量并行如何与本讲配合的读者，可继续阅读 **u5-l3 张量并行运行时：多进程与共享内存 IPC**，重点关注每个 rank 各自独立捕获/回放自己的图、`Context` 在各 rank 等价但无需跨进程同步的设计。
- 对「为什么 NCCL 通信难以录进 CUDA Graph」感兴趣的读者，可以顺带查阅 PyTorch 官方文档中 *CUDA Graphs* 与 *NCCL* 章节作为补充阅读，再回到 `embed_head.py` 的 `gather` 处对照体会。
