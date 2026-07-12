# KV Cache 显存预算与分配

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 `ModelRunner` 在引擎启动时**先预热、后预算、再分配**这三步的顺序与各自职责。
- 逐项解释块数预算公式 \(N_{\text{blocks}}=\lfloor(\,U\cdot\text{util}-\text{used}-(\text{peak}-\text{current})\,)/B_{\text{block}}\rfloor\) 中每个量的来源和物理含义。
- 推导单个 block 的字节构成 \(B_{\text{block}}\)，并理解为什么它要乘上「K/V 两份」和「全部隐藏层」。
- 画出 6 维 `kv_cache` 张量的形状，并解释每个 Attention 层的 `k_cache`/`v_cache` 是如何从这个大张量上「切视图」挂载上去的。

本讲是第 3 单元「显存与 KV Cache」的收尾篇。u3-l1 讲了 `BlockManager` 如何用**逻辑块号**做引用计数与分配，u3-l2 讲了基于哈希的前缀缓存。但那些逻辑块号到底对应到哪里、引擎一共能开多少块，直到本讲才给出答案：物理 K/V 数据住在一个巨大的 `kv_cache` 张量里，而块的总数是由**剩余显存预算公式**在启动时一次性算定的。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个直觉。

### 2.1 什么是 KV Cache，为什么它最吃显存

Transformer 自回归生成时，每生成一个新 token，都要对「之前所有 token」做注意力。如果每一步都重算所有历史 token 的 Key/Value，计算量会随长度平方膨胀。解决办法是：把每一层算出的 Key、Value 缓存下来，下一步直接复用。这份缓存就叫 **KV Cache**。

KV Cache 的大小与序列长度、层数、KV 头数成正比，是推理时**最大的一块显存占用**（通常远超模型权重本身）。因此「能开多少 KV Cache」基本决定了引擎能同时服务多少请求、能跑多长的上下文。

### 2.2 「块」是逻辑账本，张量是物理仓库

u3-l1 讲过，nano-vllm 把 KV Cache 切成固定大小的**块**（block，默认 256 个 token 一块）。但要注意一个容易混淆的点：

- `BlockManager` 里的 `Block` 只是一本**账本**——记录 `block_id`、引用计数、哈希、token 列表，它**不存放任何 K/V 数据**。
- 真正的 K/V 数据住在一个 GPU 大张量 `kv_cache` 里。

`Sequence.block_table` 里存的是**逻辑块号**，这些块号就是 `kv_cache` 张量里的索引。本讲要回答的核心问题之一就是：这个物理仓库一共有多少格（`num_kvcache_blocks`），又是按什么公式定下来的。

### 2.3 为什么分配前必须先「预热」

给 KV Cache 预留多少显存，是一个「扣减法」问题：

\[
\text{可用显存} = \text{总预算上限} - \text{权重占用的} - \text{激活峰值会占用的}
\]

权重占用是静态的，启动后基本不变；但「激活峰值」——一次前向传播中间张量（注意力矩阵、MLP 中间态等）的最大显存——只有**真的跑一次前向**才能测出来。所以引擎在分配 KV Cache 之前，必须先用一个**最大规模的假输入**跑一次前向，把峰值激活测出来，这步就叫**预热（warmup）**。

理解了这三点，下面的源码就是把这些直觉落地。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `nanovllm/engine/model_runner.py` | 本讲主角。`warmup_model` 测峰值，`allocate_kv_cache` 算预算并分配张量。 |
| `nanovllm/config.py` | `Config` 数据类，持有 `gpu_memory_utilization`、`kvcache_block_size`、`num_kvcache_blocks`（启动时被写回）等字段。 |
| `nanovllm/layers/attention.py` | `Attention` 层持有 `k_cache`/`v_cache` 两个属性，`store_kvcache` 把 K/V 写进这些视图——理解挂载后的消费方式。 |
| `nanovllm/engine/llm_engine.py` | 构造顺序：`ModelRunner` 先于 `Scheduler` 构造，使预算结果能流到 `BlockManager`。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 `warmup_model`**：测量推理时的峰值显存（预算公式的前置条件）。
- **4.2 `allocate_kv_cache` 的预算公式**：从剩余显存推导出 `num_kvcache_blocks`，并拆解单块字节 `block_bytes`。
- **4.3 `kv_cache` 张量形状与 `k_cache`/`v_cache` 挂载**：物理仓库的布局与每层的视图切分。

### 4.1 warmup_model：测量推理峰值显存

#### 4.1.1 概念说明

`warmup_model` 解决的问题是：**在不真正处理任何用户请求的情况下，估算引擎最重的一步前向会吃多少激活显存。**

为什么要估「最重的一步」？因为预算公式要用 `peak`（峰值）去预留空间——只有按最坏情况预留，真实推理时才不会因为激活膨胀而把 KV Cache 挤爆（OOM）。nano-vllm 用「单步处理 token 数上限」`max_num_batched_tokens` 来刻画最重的一步：一步里要算的 token 越多，激活越大。所以预热用一组**填满 `max_num_batched_tokens` 的假序列**去触发峰值。

预热与 KV Cache 分配的时序关系很重要：预热在前，分配在后。预热时 KV Cache 还没分配，所以测到的峰值**只含权重与激活**，正好是预算公式需要扣掉的量。

#### 4.1.2 核心流程

`warmup_model` 的执行过程可以用下面这段伪代码概括：

```
1. empty_cache()                          # 先清掉之前残留的缓存碎片
2. reset_peak_memory_stats()              # 把峰值统计归零，准备重新测量
3. seq_len  = min(max_num_batched_tokens, max_model_len)   # 单条假序列长度
4. num_seqs = min(max_num_batched_tokens // seq_len,       # 假序列条数
                  max_num_seqs)
5. 构造 num_seqs 条全 0、长度 seq_len 的假 Sequence
6. 把每条的 num_scheduled_tokens 置为 seq_len
7. run(seqs, is_prefill=True)             # 跑一次 prefill 前向，触发峰值
8. empty_cache()                          # 释放激活，但 peak 已被记录
```

关键点是第 3、4 步对 `seq_len`/`num_seqs` 的取法：

- `seq_len` 取 `max_num_batched_tokens` 与 `max_model_len` 的较小值，保证单条序列不会超过模型支持的最大长度。
- `num_seqs` 取 `max_num_batched_tokens // seq_len`（把预算填满）与 `max_num_seqs`（并发上限）的较小值。

二者乘起来近似等于 `max_num_batched_tokens`，也就是「一步内要处理的最多 token 数」。这样构造的假输入能尽可能逼近真实运行时的最重一步。

#### 4.1.3 源码精读

预热逻辑位于 [nanovllm/engine/model_runner.py:91-101](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L91-L101)：

```python
def warmup_model(self):
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()
    max_num_batched_tokens, max_model_len = self.config.max_num_batched_tokens, self.config.max_model_len
    seq_len = min(max_num_batched_tokens, max_model_len)
    num_seqs = min(max_num_batched_tokens // seq_len, self.config.max_num_seqs)
    seqs = [Sequence([0] * seq_len) for _ in range(num_seqs)]
    for seq in seqs:
        seq.num_scheduled_tokens = seq_len
    self.run(seqs, True)
    torch.cuda.empty_cache()
```

几处要点：

- **先 `reset_peak_memory_stats()` 再跑前向**：这样 `run` 结束后，`torch.cuda.memory_stats()["allocated_bytes.all.peak"]` 反馈的就是这次前向（叠加权重）的峰值，正是下一节 `allocate_kv_cache` 要读的 `peak`。
- **`seq.num_scheduled_tokens = seq_len`**：告诉 `prepare_prefill` 这条序列本步要算 `seq_len` 个 token，配合全 0 的 `input_ids` 形成一次合法的 varlen prefill。
- **末尾 `empty_cache()`**：把前向产生的临时激活释放回操作系统可见的 free 区，让后续 `mem_get_info()` 拿到更干净的 free 值；但 `peak` 仍保留在统计里。

补充一个容易忽略的细节：预热时这些假 `Sequence` 的 `block_table` 是空的，而且此时 `allocate_kv_cache` 还没运行，各 `Attention` 层的 `k_cache`/`v_cache` 仍是空张量（见 [nanovllm/layers/attention.py:57](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L57)）。于是 `Attention.forward` 里 `if k_cache.numel() and v_cache.numel()` 判断为假，`store_kvcache` 被跳过。也就是说，**预热只测权重 + 计算激活峰值，不涉及 KV Cache 的写入**——这正是我们想要的，因为 KV Cache 的预算还没定。

最后看一眼调用时机：`warmup_model()` 在 [nanovllm/engine/model_runner.py:34](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L34) 被调用，紧随模型加载之后、`allocate_kv_cache()` 之前。这个顺序是预算公式成立的前提。

#### 4.1.4 代码实践

**实践目标**：理解 `max_num_batched_tokens` 如何影响预热峰值，并亲眼看到 `peak` 这个量。

**操作步骤**（源码阅读 + 本地运行结合）：

1. 在 [nanovllm/engine/model_runner.py:100](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L100) 的 `self.run(seqs, True)` 之后、`torch.cuda.empty_cache()` 之前，临时加一行日志：

   ```python
   print(f"[warmup] seq_len={seq_len} num_seqs={num_seqs} "
         f"peak={torch.cuda.memory_stats()['allocated_bytes.all.peak']/1024**2:.1f} MiB")
   ```

2. 分别用 `max_num_batched_tokens=8192` 和 `max_num_batched_tokens=16384`（默认值）构造引擎：

   ```python
   from nanovllm import LLM
   LLM("Qwen/Qwen3-0.6B", enforce_eager=True, max_num_batched_tokens=8192)
   ```

**需要观察的现象**：`seq_len`、`num_seqs` 的取值是否符合第 4.1.2 节的公式；`max_num_batched_tokens` 翻倍后，`peak` 是否明显上升。

**预期结果**：`max_num_batched_tokens` 越大，预热峰值越高；峰值越高，留给 KV Cache 的预算就越少（下一节会看到 `peak` 直接从预算里被扣掉）。

**待本地验证**：上述峰值的具体数值依赖你的 GPU 与模型，需在真实环境运行后记录。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `reset_peak_memory_stats()` 这行删掉，预算公式会偏向保守还是激进？为什么？

**参考答案**：偏向保守（分配到的块数偏少）。因为删掉后 `peak` 会包含进程启动以来**历史最高**的分配（比如导入依赖、构建模型过程中的临时张量），可能远大于真实前向峰值，于是公式里扣掉的 `peak - current` 偏大，KV Cache 预算偏小。`reset` 的作用就是只保留最近这次前向的真实峰值。

**练习 2**：为什么不直接用一条 `seq_len = max_num_batched_tokens` 的超长序列预热，而要拆成多条？

**参考答案**：因为 `seq_len` 被 `min(..., max_model_len)` 限制，单条不能超过模型支持的最大长度；同时 `num_seqs` 也受 `max_num_seqs` 限制。拆成多条既能把 token 预算填满，又尊重了模型长度上限和并发上限这两条硬约束。

---

### 4.2 allocate_kv_cache：块数预算公式与单块字节

#### 4.2.1 概念说明

预热测出 `peak` 后，`allocate_kv_cache` 要回答唯一一个问题：**在不超过 `gpu_memory_utilization` 这条上限的前提下，还能拿出多少字节给 KV Cache，折算成多少块？**

这是一个典型的「扣减法」。GPU 显存被四类东西瓜分：

1. 非 PyTorch 占用（其他进程、CUDA context 等）；
2. PyTorch 持久占用（模型权重、采样器等，前向后仍保留）；
3. PyTorch 临时激活峰值（每次前向短暂出现，必须预留）；
4. KV Cache（我们正要分配的）。

引擎允许的总占用上限是 `total * gpu_memory_utilization`。从上限里依次扣掉前三类，剩下的就是第 4 类的预算。把预算除以「单块字节数」`block_bytes` 并向下取整，就得到块数 `num_kvcache_blocks`。

#### 4.2.2 核心流程与公式推导

先看预算公式。设：

- \(U\) = `total`（GPU 总显存，来自 `torch.cuda.mem_get_info()`）；
- \(\text{util}\) = `gpu_memory_utilization`；
- \(\text{used} = U - \text{free}\)（当前 GPU 已用字节，含 PyTorch 与非 PyTorch）；
- \(\text{peak}\) = 预热测得的 PyTorch 分配峰值（权重 + 激活峰值）；
- \(\text{current}\) = 当前 PyTorch 已分配字节（前向后保留的部分，主要是权重）；
- \(B_{\text{block}}\) = 单个块占用的字节数。

引擎的思路是：当前 GPU 还剩多少 headroom（`U·util − used`），其中要预留 `peak − current` 给即将再次出现的激活峰值，剩下的都给 KV Cache：

\[
\text{KV 预算} = (U\cdot\text{util} - \text{used}) - (\text{peak} - \text{current}) = U\cdot\text{util} - \text{used} - \text{peak} + \text{current}
\]

注意 `peak − current` 正是「前向过程中短暂出现、前向后已释放」的那部分激活显存——它前向后不在 `current` 里，但真实推理时会重新冒出来，所以必须从预算里预留。`used` 已经把权重（即 `current` 的大部分）算进去了，所以公式里再 `+ current` 是为了**只扣掉纯激活部分 `peak − current`**，避免把权重重复扣两次。

最终块数为预算除以单块字节再向下取整：

\[
N_{\text{blocks}} = \left\lfloor \frac{U\cdot\text{util} - \text{used} - \text{peak} + \text{current}}{B_{\text{block}}} \right\rfloor
\]

再看单块字节 \(B_{\text{block}}\) 的构成。一个「块」在 `block_table` 里只占一个块号，但物理上它要为**每一层的 K 和 V** 都存 `block_size` 个 token 的数据：

\[
B_{\text{block}} = \underbrace{2}_{\text{K 与 V}} \times \underbrace{L}_{\text{隐藏层数}} \times \underbrace{S}_{\text{block\_size}} \times \underbrace{h_{\text{kv}}}_{\text{每 rank KV 头数}} \times \underbrace{d}_{\text{head\_dim}} \times \underbrace{b}_{\text{每个元素字节数}}
\]

其中各因子：

- `2`：同一份 token 的 Key 和 Value 分别存一份；
- `L = hf_config.num_hidden_layers`：每个 Transformer 层都有自己的 K/V；
- `S = block_size`（默认 256）：一块装多少 token；
- \(h_{\text{kv}}\) = `num_key_value_heads // world_size`：张量并行下，KV 头在 rank 间均分（每个 rank 只存自己那份）；
- `d = head_dim`：每个头的维度；
- `b = hf_config.dtype.itemsize`：每个元素的字节数（bfloat16 为 2）。

这里有个**关键洞察**值得强调：`block_table` 里一个块号对应「全部层的 K/V」，所以单块字节必须乘上 `2 * L`。逻辑上一个块号、物理上却是 `2L` 份子块的存储——这正是逻辑账本与物理仓库之间的「放大」关系。

举个数值例子（**具体数值请以本地 `config.json` 为准**）。假设某 Qwen3-0.6B 配置为 `num_hidden_layers=28`、`num_key_value_heads=8`、`head_dim=128`、`dtype=bfloat16`，且 `block_size=256`、`tensor_parallel_size=1`，则：

\[
B_{\text{block}} = 2 \times 28 \times 256 \times 8 \times 128 \times 2 = 29\,360\,128 \text{ 字节} \approx 28 \text{ MiB}
\]

即每块约 28 MiB。于是在一张 24 GiB、`util=0.9` 的卡上，扣掉权重与激活后，通常能分到几百块，对应几万到十几万个 token 的 KV 容量。

#### 4.2.3 源码精读

预算公式的实现见 [nanovllm/engine/model_runner.py:103-114](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L103-L114)：

```python
def allocate_kv_cache(self):
    config = self.config
    hf_config = config.hf_config
    free, total = torch.cuda.mem_get_info()
    used = total - free
    peak = torch.cuda.memory_stats()["allocated_bytes.all.peak"]
    current = torch.cuda.memory_stats()["allocated_bytes.all.current"]
    num_kv_heads = hf_config.num_key_value_heads // self.world_size
    head_dim = getattr(hf_config, "head_dim", hf_config.hidden_size // hf_config.num_attention_heads)
    block_bytes = 2 * hf_config.num_hidden_layers * self.block_size * num_kv_heads * head_dim * hf_config.dtype.itemsize
    config.num_kvcache_blocks = int(total * config.gpu_memory_utilization - used - peak + current) // block_bytes
    assert config.num_kvcache_blocks > 0
```

逐行对照公式：

- `free, total = torch.cuda.mem_get_info()`：取 \(U\) 与 free。
- `used = total - free`：当前 GPU 总占用（含非 PyTorch）。
- `peak` / `current`：直接取预热后留下的统计量，对应公式同名项。
- `num_kv_heads = num_key_value_heads // world_size`：TP 下每 rank 的 KV 头数，这就是 \(h_{\text{kv}}\)。
- `head_dim`：优先读 `hf_config.head_dim`（Qwen3 显式设定），退化到 `hidden_size // num_attention_heads`。
- `block_bytes`：与上面的 \(B_{\text{block}}\) 公式逐字对应。
- 最后一行：`int(...)` 先截断成整数再做整除 `//`，相当于向下取整得到 \(N_{\text{blocks}}\)，并把结果**写回 `config.num_kvcache_blocks`**。
- `assert config.num_kvcache_blocks > 0`：预算若不足以放下哪怕一块，直接报错——这是「显存不够」最常见的第一现场。

这里有一个**承接 u1-l4 / u3-l1 的关键设计**：`num_kvcache_blocks` 在 `Config` 里初值是 `-1`（占位符，见 [nanovllm/config.py:18](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/config.py#L18)），直到此刻才被填上真实值。而这个值随后要传给 `BlockManager` 决定能开多少块。这能成立，是因为 [nanovllm/engine/llm_engine.py:31-34](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L31-L34) 里 `ModelRunner`（内部调用 `allocate_kv_cache`）**先于** `Scheduler`（内部构造 `BlockManager`）创建：

```python
self.model_runner = ModelRunner(config, 0, self.events)   # 这里把 config.num_kvcache_blocks 写好
self.tokenizer = AutoTokenizer.from_pretrained(config.model, use_fast=True)
config.eos = self.tokenizer.eos_token_id
self.scheduler = Scheduler(config)                        # BlockManager 读到写好的块数
```

换句话说，`config` 对象在 `ModelRunner` 与 `Scheduler` 之间充当了一次「单例信箱」，把显存预算结果从执行侧传递到调度侧。

#### 4.2.4 代码实践

**实践目标**：亲手验证预算公式——调整 `gpu_memory_utilization`，打印实际 `num_kvcache_blocks`，并与理论计算对照。

**操作步骤**：

1. 先读出公式所需的全部输入。在 `allocate_kv_cache` 里 `assert` 之前临时加日志：

   ```python
   print(f"[alloc] total={total/1024**3:.2f}GiB used={used/1024**3:.2f}GiB "
         f"peak={peak/1024**2:.1f}MiB current={current/1024**2:.1f}MiB "
         f"block_bytes={block_bytes/1024**2:.2f}MiB "
         f"util={config.gpu_memory_utilization} "
         f"=> num_kvcache_blocks={config.num_kvcache_blocks}")
   ```

2. 用不同利用率构造引擎，分别记录 `num_kvcache_blocks`：

   ```python
   from nanovllm import LLM
   for util in (0.6, 0.9):
       llm = LLM("Qwen/Qwen3-0.6B", enforce_eager=True, gpu_memory_utilization=util)
       mr = llm.model_runner
       print("util =", util, "num_kvcache_blocks =", mr.config.num_kvcache_blocks)
       del llm
   ```

3. 手算理论 `block_bytes` 与预算做交叉验证：

   ```python
   hf = mr.config.hf_config
   num_kv_heads = hf.num_key_value_heads // mr.world_size
   head_dim = getattr(hf, "head_dim", hf.hidden_size // hf.num_attention_heads)
   block_bytes = 2 * hf.num_hidden_layers * mr.block_size * num_kv_heads * head_dim * hf.dtype.itemsize
   print("理论 block_bytes =", block_bytes, "字节，约", block_bytes / 1024**2, "MiB")
   ```

**需要观察的现象**：

- 两次运行的 `used`、`peak`、`current`、`block_bytes` 应当**几乎相同**（同一模型同一卡，只有 `util` 变了）。
- `num_kvcache_blocks` 应随 `util` 线性增大；两次块数之差乘以 `block_bytes`，应近似等于 `(0.9 − 0.6) * total`。

**预期结果**：用日志里的 `total/used/peak/current` 代入公式手算，得到的 `num_kvcache_blocks` 与代码输出一致（差 `//` 的取整误差）；`util` 每提高 0.1，块数大约增加 `0.1 * total / block_bytes`。

**待本地验证**：具体块数与字节数依赖 GPU 总显存与模型配置，需在真实环境运行后记录。

#### 4.2.5 小练习与答案

**练习 1**：把 `tensor_parallel_size` 从 1 调到 2，每 rank 的 `num_kvcache_blocks` 会变大、变小还是不变？总 KV 容量呢？

**参考答案**：每 rank 的 `num_kvcache_blocks` 会**变大**。因为 TP=2 时 `num_kv_heads = num_key_value_heads // 2` 减半，`block_bytes` 减半；同时两张卡各自有独立的显存预算，每张卡扣掉一半的 KV 头后，同样的预算能换到约两倍的块数。但要注意每块对应的 token 数没变（仍是 `block_size`），所以**每 rank 能表示的 token 数约翻倍**——不过这只是在「每 rank 只存一半 KV 头」意义上的扩容，从模型整体看总 KV 容量按 token 计算的提升还要结合调度逻辑综合判断。

**练习 2**：公式里为什么是 `− peak + current`，而不是直接 `− peak`？

**参考答案**：因为 `used` 已经把 `current`（权重等持久占用）算进 GPU 总占用了。若再 `− peak`，就把权重部分扣了两次。正确的做法是只扣「纯激活」`peak − current`，展开后即 `− used − peak + current`。`+ current` 正是用来抵消 `used` 里已经包含的权重、避免重复扣减。

---

### 4.3 kv_cache 张量形状与 k_cache/v_cache 挂载

#### 4.3.1 概念说明

算出 `num_kvcache_blocks` 后，`allocate_kv_cache` 还要做两件事：**真正分配物理张量**，并把它**挂载到每个 Attention 层**。

这里要理解一个设计选择：nano-vllm 没有给每一层分别分配一个独立的小张量，而是**先开一个覆盖所有层、所有块、K/V 双份的巨型张量**，然后让每层的 `k_cache`/`v_cache` 成为这个大张量上的**视图（view）**。这样做的好处是：块号 `block_id` 在所有层之间统一编号，`block_table` 只需存一份，调度层完全不用关心「第几层」；同时大张量一次性分配，显存布局连续、便于内核访问。

#### 4.3.2 核心流程

张量的形状是一个 6 维结构：

```
kv_cache.shape = (2, num_hidden_layers, num_kvcache_blocks, block_size, num_kv_heads, head_dim)
                  └┘ └──────────────┘ └──────────────┘ └────────┘ └─────────┘ └──────┘
                  K/V     第几层           块号         块内 token     KV 头       头维度
                0=K,1=V
```

挂载过程则是一次对模型所有子模块的遍历：

```
layer_id = 0
for module in model.modules():                       # 深度优先遍历全部子模块
    if module 拥有 k_cache 和 v_cache 属性:           # 即每个 Attention 层
        module.k_cache = kv_cache[0, layer_id]        # 切出该层的 K 视图
        module.v_cache = kv_cache[1, layer_id]        # 切出该层的 V 视图
        layer_id += 1
```

因为 `DecoderLayer` 在模型里是顺序排列的，`modules()` 的深度优先遍历会**按层序**逐个命中 `Attention` 模块，于是 `layer_id` 与张量的第 1 维（`num_hidden_layers`）一一对应。每层拿到的 `k_cache` 形状是 `(num_kvcache_blocks, block_size, num_kv_heads, head_dim)`，正是该层私有的「块 × 块内 token × KV 头 × 头维度」仓库。

#### 4.3.3 源码精读

张量分配与挂载见 [nanovllm/engine/model_runner.py:115-121](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L115-L121)：

```python
self.kv_cache = torch.empty(2, hf_config.num_hidden_layers, config.num_kvcache_blocks,
                            self.block_size, num_kv_heads, head_dim)
layer_id = 0
for module in self.model.modules():
    if hasattr(module, "k_cache") and hasattr(module, "v_cache"):
        module.k_cache = self.kv_cache[0, layer_id]
        module.v_cache = self.kv_cache[1, layer_id]
        layer_id += 1
```

要点：

- `torch.empty(...)`：只分配不初始化（KV Cache 会被前向逐步填满，无需清零），节省启动时间。
- `kv_cache[0, layer_id]` 是**视图而非拷贝**：每层 `Attention` 的 `k_cache`/`v_cache` 与大张量共享同一片显存。任何一层对 `k_cache` 的写入，立刻反映在大张量里。
- `hasattr(module, "k_cache")`：`Attention` 在 `__init__` 里就把这两个属性初始化为空张量（[nanovllm/layers/attention.py:57](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L57)），这里正是用这个标记来识别「哪些模块是 Attention」并完成挂载。

挂载之后，`Attention.forward` 通过 `store_kvcache` 把新算出的 K/V 写进视图。见 [nanovllm/layers/attention.py:33-40](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L33-L40)：

```python
def store_kvcache(key, value, k_cache, v_cache, slot_mapping):
    N, num_heads, head_dim = key.shape
    D = num_heads * head_dim
    ...
    store_kvcache_kernel[(N,)](key, key.stride(0), value, value.stride(0), k_cache, v_cache, slot_mapping, D)
```

这里 `D = num_kv_heads * head_dim`，正好是 `k_cache` 后两维的合并跨度。Triton 内核（[nanovllm/layers/attention.py:28-30](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L28-L30)）按 `cache_offsets = slot * D + arange(D)` 把每个 token 的 K/V 写入 `k_cache` 视图，其中 `slot = block_id * block_size + 块内偏移`。换句话说，`slot_mapping` 把一个 token 映射到「块号 × 块大小 + 块内位置」，再乘以 `D` 跨过一个 token 的全部 KV 头维度——这与 `kv_cache` 的 `(num_blocks, block_size, num_kv_heads, head_dim)` 布局精确吻合。

把本节和 4.2 节合在一起看：`block_bytes` 里那个 `2 * num_hidden_layers` 因子，本质就来自这个 6 维张量的前两维——**一个块号要在 K/V 两份、每一层都占一格存储**。逻辑账本上的一块，物理仓库里是 \(2L\) 格。

#### 4.3.4 代码实践

**实践目标**：验证 `kv_cache` 的形状，并确认每层 `Attention` 的 `k_cache` 是大张量的视图（共享存储）。

**操作步骤**：

```python
from nanovllm import LLM
import torch

llm = LLM("Qwen/Qwen3-0.6B", enforce_eager=True)
mr = llm.model_runner

print("kv_cache.shape =", tuple(mr.kv_cache.shape))
print("kv_cache.dtype =", mr.kv_cache.dtype)

# 找到第一个 Attention 层，验证它是视图
attentions = [m for m in mr.model.modules() if hasattr(m, "k_cache") and m.k_cache.numel() > 0]
att = attentions[0]
print("num Attention layers =", len(attentions))
print("att.k_cache.shape =", tuple(att.k_cache.shape))

# 视图验证：改大张量的一处，看是否反映到层视图
mr.kv_cache[0, 0, 0, 0, 0, 0] = 12345.0
print("层视图同步到的值 =", att.k_cache[0, 0, 0, 0].item())
```

**需要观察的现象**：

- `kv_cache.shape` 是 6 维，且第 0 维为 2、第 1 维等于 `num_hidden_layers`、第 2 维等于 `num_kvcache_blocks`。
- `len(attentions)` 恰好等于 `num_hidden_layers`，说明每层都被挂载。
- `att.k_cache.shape` 为 `(num_kvcache_blocks, block_size, num_kv_heads, head_dim)`。
- 修改大张量 `kv_cache[0,0,0,...]` 后，`att.k_cache[0,0,0,0]` 读到相同值，证明二者共享存储。

**预期结果**：以上四点全部成立，即验证了「大张量 + 每层视图」的挂载模型。

**待本地验证**：具体形状数值依赖模型配置与实际块数，需在真实环境运行后记录。

#### 4.3.5 小练习与答案

**练习 1**：`kv_cache` 第 0 维的 `2` 和第 1 维的 `num_hidden_layers`，与 4.2 节 `block_bytes` 公式里的哪两个因子一一对应？

**参考答案**：分别对应 `block_bytes` 里的 `2`（K/V 双份）和 `L = num_hidden_layers`（每个层各存一份）。这也解释了为什么逻辑上的一个块号，物理上要占 `2 * num_hidden_layers` 份子块存储。

**练习 2**：如果某层 `Attention` 的 `k_cache` 不是视图而是独立张量，`store_kvcache` 写入后会发生什么问题？

**参考答案**：写入只会落到那个独立张量里，`kv_cache` 大张量不会更新；更严重的是，每层各自独立就失去了「块号跨层统一」的好处——调度层用一个 `block_id` 就能同时定位所有层的 K/V 这一前提会被打破，`block_table` 的设计也会失效。视图挂载保证了「一个块号、所有层同步」的一致性。

---

## 5. 综合实践

把本讲三块知识串成一个端到端的小任务：**从显存上限反推 KV 容量，并验证调度层确实在用这个容量。**

任务步骤：

1. **记录预算输入**。按 4.2.4 的方法在 `allocate_kv_cache` 里加日志，用默认 `gpu_memory_utilization=0.9` 启动引擎，抄下 `total / used / peak / current / block_bytes / num_kvcache_blocks` 六个数。

2. **手算验证**。用抄到的五个输入代入公式 \(N_{\text{blocks}}=\lfloor(U\cdot\text{util}-\text{used}-\text{peak}+\text{current})/B_{\text{block}}\rfloor\) 手算，确认与代码输出的 `num_kvcache_blocks` 一致（允许 `//` 取整误差）。

3. **换利用率复跑**。改用 `gpu_memory_utilization=0.5` 再启动一次，记录新的 `num_kvcache_blocks`。计算两次块数差乘以 `block_bytes`，确认它近似等于 \((0.9-0.5)\times total\)，验证公式对 `util` 的线性关系。

4. **连接调度层**。打印 `llm.scheduler.block_manager.blocks` 的长度，确认它等于 `config.num_kvcache_blocks`——这正是 u3-l1 里 `BlockManager` 能分配的块总数来源。再打印 `mr.kv_cache.shape[2]`，确认三者完全一致。

5. **画出数据流**。画一张图：`mem_get_info / warmup peak` → `预算公式` → `num_kvcache_blocks` → 既是 `kv_cache.shape[2]`（物理仓库格数），又流到 `BlockManager(num_blocks=...)`（逻辑账本块数）。

完成这个任务后，你就把「显存预算公式 → 物理张量布局 → 逻辑块管理」这条链路彻底打通了。

## 6. 本讲小结

- 引擎启动时严格按 **预热 → 预算 → 分配 → （可选）CUDA Graph 捕获** 的顺序进行，`warmup_model` 在 `allocate_kv_cache` 之前运行，只为测出 `peak`。
- 块数预算公式是扣减法：\(N_{\text{blocks}}=\lfloor(U\cdot\text{util}-\text{used}-\text{peak}+\text{current})/B_{\text{block}}\rfloor\)，其中 `peak − current` 是必须预留的纯激活峰值。
- 单块字节 \(B_{\text{block}}=2\cdot L\cdot S\cdot h_{\text{kv}}\cdot d\cdot b\)，`2` 与 `L` 说明一个逻辑块号在物理上覆盖全部层的 K 与 V。
- 预算结果写回 `config.num_kvcache_blocks`，再经 `Scheduler` 流入 `BlockManager`，依赖 `ModelRunner` 先于 `Scheduler` 构造的顺序。
- 物理仓库是一个 6 维 `kv_cache` 张量，每个 `Attention` 层通过 `k_cache`/`v_cache` **视图**共享它，保证「一个块号、所有层同步」。
- `store_kvcache` 用 `slot_mapping` 把 token 映射到 `block_id × block_size + 块内偏移`，与张量的 `(blocks, block_size, kv_heads, head_dim)` 布局精确吻合。

## 7. 下一步学习建议

本讲讲完了「显存侧」的全部静态机制——块管理（u3-l1）、前缀缓存（u3-l2）、显存预算与分配（本讲）。接下来进入第 4 单元「模型执行」，建议按顺序阅读：

- **u4-l1 ModelRunner 与输入准备**：看 `prepare_prefill`/`prepare_decode` 如何构造 `input_ids`、`positions`，特别是 `slot_mapping` 和 `cu_seqlens` 是怎么生成的——它们正是本讲 `kv_cache` 与 `store_kvcache` 的直接消费方。
- **u4-l2 Attention 与 Triton store_kvcache 内核**：精读本讲多次提到的 `store_kvcache` 内核，理解 `slot_mapping` 的偏移映射与 `slot=-1` 的跳过分支。
- **u5-l1 CUDA Graph 捕获与回放**：理解 `allocate_kv_cache` 之后那一步 `capture_cudagraph` 是怎么把前向录制成可回放的图的。
