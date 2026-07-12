# ModelRunner 与输入准备

## 1. 本讲目标

前面几讲我们跟到了 `LLMEngine.step()`：调度器 `Scheduler.schedule()` 选出一批 `Sequence`、判定本步是 prefill 还是 decode，然后把球传给 `model_runner.call("run", seqs, is_prefill)`。可模型（Qwen3）是一个标准的 Transformer，它只认张量——`input_ids`、`positions`。那么「一堆带着 `block_table`、`num_scheduled_tokens` 的 Sequence 对象」是如何变成「GPU 能吃下去的输入张量」的？这正是 `ModelRunner` 的输入准备职责。

学完本讲，你应当能够：

- 说清 `ModelRunner.run` 作为「张量桥梁」的五步编排：prepare → sample → run_model → sample → reset。
- 理解 prefill 阶段的 **varlen 打包**：为什么要把多条不等长序列拼成一个一维张量，`cu_seqlens_q` / `cu_seqlens_k` 如何标记边界。
- 掌握 **slot_mapping**：如何把每个 token 的 K/V 写进 paged KV cache 的正确物理槽位，并能手算给定 `block_table` 时的 slot。
- 理解 decode 阶段 `context_lens` 与 `block_tables` 的构造方式，以及它和 prefill 的本质差异。
- 理解全局 `Context` 如何在 `ModelRunner` 与底层 `Attention` / `ParallelLMHead` 之间传递注意力元数据。

本讲是「模型执行」单元的入口，后续讲义（Attention 与 Triton 内核、Context 元数据、Qwen3 结构）都会反复用到这里构造出的张量。

## 2. 前置知识

本讲默认你已掌握前置讲义的两个结论，这里只做最简回顾：

- **Sequence 是请求的账本（来自 u2-l1）**：`len(seq)` 返回 `num_tokens`；`seq.block_table` 是逻辑块号 → 物理块号的列表；`seq.num_cached_tokens` 是「已经算过 KV 的进度水位」（前缀缓存命中时会跳过这一段）；`seq.num_scheduled_tokens` 是「本步要新算的 token 数」；`seq.last_token` 是上一步刚采样出的 token。`seq.last_block_num_tokens` 表示最后一块里实际有几个 token（不满一块时的尾长）。
- **KV Cache 是分页的（来自 u3-l1 / u3-l3）**：真正的 K/V 数据并不在 `Block` 对象里，而是住在一个 6 维大张量 `kv_cache` 中，形状为 `(2, num_layers, num_blocks, block_size, num_kv_heads, head_dim)`。`block_table` 里的物理块号 `b` 对应 `k_cache[b]` 这一段连续 `block_size` 个槽位。把一个 token 的 K/V 写进 cache，就是算出它该落在哪个**槽位（slot）**。

此外，你需要知道两个 flash-attn 的概念（不必现在懂实现）：

- **varlen（variable-length）**：把一个 batch 里多条不等长序列首尾相接拼成一维，再用「累积序列长」`cu_seqlens` 标出每条的边界，从而避免 padding 浪费算力。
- **paged attention / block_table**：attention 读取 K/V 时，不是从连续内存读，而是按 `block_table` 给的物理块号去 KV cache 大张量里「跳着读」。

一句话直觉：**`slot_mapping` 告诉模型「新算出来的 K/V 写到哪里」，`block_tables` 告诉 attention「历史 K/V 从哪里读」。**

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开：

| 文件 | 作用 |
| --- | --- |
| [nanovllm/engine/model_runner.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py) | 本讲主角。`run` 是入口，`prepare_prefill` / `prepare_decode` 构造输入张量，`prepare_block_tables` 拼装块表，`run_model` 真正前向。 |

为了说明这些张量被谁消费，还会少量引用：

| 文件 | 作用 |
| --- | --- |
| [nanovllm/utils/context.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/utils/context.py) | 全局 `Context` 单例，承载本讲构造的所有注意力元数据。 |
| [nanovllm/layers/attention.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py) | `Attention.forward` 用 `slot_mapping` 写 cache、用 `block_tables` 读 cache（u4-l2 详解）。 |
| [nanovllm/layers/embed_head.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/embed_head.py) | `ParallelLMHead` 在 prefill 时只取每序列最后一个 token 算 logits，依赖 `cu_seqlens_q`。 |
| [nanovllm/engine/scheduler.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py) | `schedule` 在调用 `run` 之前设好每个 seq 的 `num_scheduled_tokens` / `num_cached_tokens`。 |

## 4. 核心概念与源码讲解

### 4.1 ModelRunner.run：调度器与模型之间的张量桥梁

#### 4.1.1 概念说明

`Scheduler` 输出的是「Python 对象列表 + 一个 bool」，而 `Qwen3ForCausalLM` 输入的是「GPU 张量」。两者之间隔着一道鸿沟：分页显存、变长序列、前缀缓存、采样。`ModelRunner.run` 就是横跨这道鸿沟的桥。它本身几乎不做计算，只做**编排**：把 seqs 翻译成张量、把张量喂给模型、把模型吐出的 logits 采样成 token、最后清理现场。

#### 4.1.2 核心流程

`run` 的一次调用严格走五步：

```text
seqs, is_prefill
      │
      ▼
(1) prepare_prefill 或 prepare_decode   ── 构造 input_ids / positions，
      │                                    并把 cu_seqlens / slot_mapping /
      ▼                                    block_tables 等写进全局 Context
(2) prepare_sample (仅 rank 0)          ── 取出每条 seq 的 temperature
      │
      ▼
(3) run_model(input_ids, positions)     ── 真正前向，返回 logits
      │
      ▼
(4) sampler(logits, temperatures)       ── 仅 rank 0 采样，得到 token_ids
      │
      ▼
(5) reset_context()                     ── 清空全局 Context，准备下一步
```

注意两个工程要点：

- **Context 是「每步」生命周期**：`prepare_*` 里 `set_context(...)` 写入，`run` 末尾 `reset_context()` 清空。如果忘了 reset，下一步的 attention 会读到上一步的旧元数据。
- **采样只在 rank 0 做**：张量并行（TP）只切分矩阵乘法，采样与 logits 拼词表都在 rank 0 完成（详见 u5-l3），所以非 0 rank 的 `temperatures` 和 `token_ids` 都是 `None`。

#### 4.1.3 源码精读

`run` 的全部逻辑只有七行，但它定义了整条数据流的形状：

[:model_runner.py:214-220 — run 的五步编排](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L214-L220)

```python
def run(self, seqs: list[Sequence], is_prefill: bool) -> list[int]:
    input_ids, positions = self.prepare_prefill(seqs) if is_prefill else self.prepare_decode(seqs)
    temperatures = self.prepare_sample(seqs) if self.rank == 0 else None
    logits = self.run_model(input_ids, positions, is_prefill)
    token_ids = self.sampler(logits, temperatures).tolist() if self.rank == 0 else None
    reset_context()
    return token_ids
```

第一行用 `is_prefill` 做分支，这正是 prefill 与 decode 的分水岭——它们构造的张量结构完全不同。`prepare_sample` 只是把每条 seq 的 temperature 收集成一个张量：

[模型 _runner.py:190-193 — prepare_sample 收集温度](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L190-L193)

而 `run_model` 决定走「eager 前向」还是「CUDA Graph 回放」（CUDA Graph 留到 u5-l1 详讲，本讲只关注 eager 这条路）：

[模型 _runner.py:195-212 — run_model 的 eager 与 graph 两条路](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L195-L212)

eager 路径里 `self.model.compute_logits(self.model(input_ids, positions))` 是关键：`self.model(...)` 返回所有 token 的最后一层隐状态，`compute_logits` 再把它映射成 logits。在 prefill 时，`compute_logits` 内部的 `ParallelLMHead` 只会挑出每条序列**最后一个 token** 的隐状态去算 logits（用 `cu_seqlens_q[1:] - 1` 作下标），因为生成只需要「下一个 token」的概率——这是一个把 prefill 与 `cu_seqlens_q` 直接挂钩的优化：

[layers/embed_head.py:58-60 — prefill 时只取每序列最后一个 token](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/embed_head.py#L58-L60)

#### 4.1.4 代码实践

**实践目标**：用日志把 `run` 的五步「可视化」，建立对推理节奏的直觉。

**操作步骤**：

1. 打开 `nanovllm/engine/model_runner.py`，在 `run` 函数体里临时加几行日志（**这是示例代码，仅用于观察，验证后请删掉，勿提交**）：

   ```python
   # 示例代码：调试日志
   def run(self, seqs, is_prefill):
       print(f"[run] is_prefill={is_prefill}, num_seqs={len(seqs)}, "
             f"scheduled_tokens={[s.num_scheduled_tokens for s in seqs]}")
       input_ids, positions = self.prepare_prefill(seqs) if is_prefill else self.prepare_decode(seqs)
       print(f"[run] input_ids.shape={input_ids.shape}, positions[-1]={positions[-1].item()}")
       temperatures = self.prepare_sample(seqs) if self.rank == 0 else None
       logits = self.run_model(input_ids, positions, is_prefill)
       token_ids = self.sampler(logits, temperatures).tolist() if self.rank == 0 else None
       reset_context()
       return token_ids
   ```

2. 用 `example.py` 跑一次推理（需 GPU + 已下载的 Qwen3-0.6B）：
   ```bash
   python example.py
   ```

**需要观察的现象**：

- 第一步 `is_prefill=True`，且 `scheduled_tokens` 列表里每个值等于该 prompt 的 token 长度（前缀未命中时）。
- `input_ids.shape` 的第 0 维等于所有 seq 的 `num_scheduled_tokens` 之和（varlen 拼接后的一维总长）。
- 之后每一步 `is_prefill=False`，`num_seqs` 等于正在解码的序列数，每条 `scheduled_tokens=1`。

**预期结果**：你会看到一条 prefill 日志、随后是若干 decode 日志，直到序列结束。具体数字取决于分词结果——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `run` 末尾的 `reset_context()` 注释掉，下一步会发生什么？
**答案**：全局 `Context` 会保留本步的 `slot_mapping` / `block_tables` 等元数据。下一步 `prepare_*` 会再次 `set_context` 覆盖它，所以多数情况下不会立刻报错；但如果某一步因为异常没走到 `set_context`，attention 就会读到陈旧的、张量形状对不上的元数据，引发难以定位的越界或形状错误。因此 `reset_context()` 是安全兜底，保证 Context 的「每步」语义。

**练习 2**：为什么 `temperatures` 和 `token_ids` 在非 0 rank 上是 `None`？
**答案**：张量并行只切分 Transformer 内部的线性层（QKV、MLP、Embedding），每个 rank 各算一部分再 all_reduce / gather。采样需要完整的 logits（拼好整张词表）和温度，这件事只在 rank 0 做，结果再由 rank 0 写回 Sequence。让 worker rank 返回 `None` 既省算力，也避免各 rank 各采一次导致结果分叉。

---

### 4.2 prepare_prefill：变长打包与 slot_mapping

#### 4.2.1 概念说明

prefill 阶段要一次性算出 prompt 中**每个** token 的 K/V 并写进 cache。难点有三：

1. 一个 batch 里多条序列长度不同，若用 padding 对齐到最长会浪费大量算力。nano-vllm 选择 **varlen 打包**：把所有序列首尾相接成一个一维张量，用 `cu_seqlens`（累积长度）标出每条的边界。
2. K/V 要写进**分页**的 cache，每个 token 都要算出自己该落在哪个物理槽位——这就是 `slot_mapping`。
3. 若命中前缀缓存，新 token 只是一段后缀，但 attention 仍要 attend 到已缓存的前缀。这时 query 数（`seqlen_q`，只含新 token）会**小于** key 数（`seqlen_k`，含整个上下文），且历史 K/V 要从 cache 经 `block_tables` 读出。

#### 4.2.2 核心流程

对 batch 中的每一条 seq，`prepare_prefill` 维护这些量：

```text
start       = seq.num_cached_tokens        # 跳过已缓存的前缀
seqlen_q    = seq.num_scheduled_tokens     # 本步新算的 token 数
end         = start + seqlen_q             # 已覆盖到的位置
seqlen_k    = end                          # attention 要看到的全部 key 数
```

- `input_ids` ← `seq[start:end]`（真正参与计算的新 token）。
- `positions` ← `range(start, end)`（**绝对位置**，喂给 RoPE，所以前缀缓存时新 token 的位置不是从 0 开始）。
- `cu_seqlens_q` 累加 `seqlen_q`；`cu_seqlens_k` 累加 `seqlen_k`。两者在前缀**未命中**时相等，命中时 `cu_seqlens_k` 更大。
- `slot_mapping`：为 `[start, end)` 里每个 token 计算物理槽位。
- 当 `cu_seqlens_k[-1] > cu_seqlens_q[-1]`（即有前缀缓存）时，额外构造 `block_tables`，供 attention 从 cache 读历史 K/V。

slot 的核心公式（设块大小为 \(B\)，序列内绝对位置为 \(p\)）：

\[
\text{slot}(p)=\text{block\_table}\!\left[\left\lfloor p/B\right\rfloor\right]\cdot B+(p\bmod B)
\]

即「先由位置定逻辑块号 → 查 `block_table` 得物理块号 → 乘以 \(B\) 得块起点 → 加块内偏移」。由于 `[start, end)` 可能跨越多个块，代码按块逐段生成连续的 slot 区间。

#### 4.2.3 源码精读

先看初始化与逐序列循环的主体：

[model_runner.py:129-150 — prepare_prefill 主体与 varlen 边界](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L129-L150)

```python
def prepare_prefill(self, seqs: list[Sequence]):
    input_ids = []
    positions = []
    cu_seqlens_q = [0]
    cu_seqlens_k = [0]
    max_seqlen_q = 0
    max_seqlen_k = 0
    slot_mapping = []
    block_tables = None
    for seq in seqs:
        start = seq.num_cached_tokens
        seqlen_q = seq.num_scheduled_tokens
        end = start + seqlen_q
        seqlen_k = end
        input_ids.extend(seq[start:end])
        positions.extend(range(start, end))
        cu_seqlens_q.append(cu_seqlens_q[-1] + seqlen_q)
        cu_seqlens_k.append(cu_seqlens_k[-1] + seqlen_k)
        max_seqlen_q = max(seqlen_q, max_seqlen_q)
        max_seqlen_k = max(seqlen_k, max_seqlen_k)
        if not seq.block_table:    # warmup
            continue
```

注意第 149–150 行的 `if not seq.block_table: continue`：预热（`warmup_model`）时构造的假序列没有 `block_table`，跳过 slot 计算。这时 `slot_mapping` 为空，而 `Attention` 里又有 `if k_cache.numel() and v_cache.numel()` 的守护——预热时 cache 还没分配，`store_kvcache` 自然不会执行。

再看按块生成 slot 的循环：

[model_runner.py:151-161 — 跨块生成 slot_mapping 区间](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L151-L161)

```python
start_block = start // self.block_size
end_block = (end + self.block_size - 1) // self.block_size
for i in range(start_block, end_block):
    slot_start = seq.block_table[i] * self.block_size
    if i == start_block:
        slot_start += start % self.block_size
    if i != end_block - 1:
        slot_end = seq.block_table[i] * self.block_size + self.block_size
    else:
        slot_end = seq.block_table[i] * self.block_size + end - i * self.block_size
    slot_mapping.extend(range(slot_start, slot_end))
```

逐块生成连续 slot 区间，正好覆盖 `[start, end)` 的每个位置：

- `start_block`、`end_block`：用向上取整把 `[start, end)` 映射到逻辑块区间。
- 第一块（`i == start_block`）：起点要补上 `start % block_size` 的块内偏移（前缀可能从块中间开始）。
- 中间块：整块都是新 token，`slot_end = 块起点 + block_size`。
- 最后一块（`i == end_block - 1`）：只到 `end` 为止，`slot_end = 块起点 + end - i*block_size`。

随后是「前缀缓存判定」与张量上传：

[model_runner.py:162-170 — 前缀缓存触发 block_tables，并 set_context](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L162-L170)

```python
if cu_seqlens_k[-1] > cu_seqlens_q[-1]:    # prefix cache
    block_tables = self.prepare_block_tables(seqs)
input_ids = torch.tensor(input_ids, dtype=torch.int64, pin_memory=True).cuda(non_blocking=True)
positions = torch.tensor(positions, dtype=torch.int64, pin_memory=True).cuda(non_blocking=True)
cu_seqlens_q = torch.tensor(cu_seqlens_q, dtype=torch.int32, pin_memory=True).cuda(non_blocking=True)
cu_seqlens_k = torch.tensor(cu_seqlens_k, dtype=torch.int32, pin_memory=True).cuda(non_blocking=True)
slot_mapping = torch.tensor(slot_mapping, dtype=torch.int32, pin_memory=True).cuda(non_blocking=True)
set_context(True, cu_seqlens_q, cu_seqlens_k, max_seqlen_q, max_seqlen_k, slot_mapping, None, block_tables)
return input_ids, positions
```

两个工程细节：

- **`pin_memory=True` + `.cuda(non_blocking=True)`**：pinned memory 是锁页主机内存，配合 `non_blocking` 可以让 H→D 拷贝与 CPU 计算重叠，减少上传延迟。
- **前缀缓存的判据是 `cu_seqlens_k[-1] > cu_seqlens_q[-1]`**：只要 batch 中任一序列命中前缀，整体 `cu_seqlens_k` 的总和就会大于 `cu_seqlens_q`，于是构造 `block_tables`。此时 `Attention` 会把读 K/V 的来源从「刚算出的连续 k/v」切到「整个 paged cache」，并用 `block_tables` 导航（详见 u4-l2）。

`set_context` 把所有元数据塞进全局 `Context`，`input_ids` / `positions` 作为返回值显式传给 `run_model`——**模型主干只需要这两个张量，其余注意力元数据全部走 Context**（详见 u4-l3）。

`prepare_block_tables` 只是把每条 seq 的 `block_table` 左对齐补 `-1` 到等长，再转成 GPU 张量：

[model_runner.py:123-127 — prepare_block_tables 左对齐补 -1](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L123-L127)

`-1` 是哨兵值，flash-attn 在 paged 模式下会忽略越界的块号。

#### 4.2.4 代码实践

**实践目标**：手算一组 seq 的 `cu_seqlens_q` / `cu_seqlens_k` / `slot_mapping`，验证它们与 `block_table` 的对应关系（即本讲指定的实践任务）。

**操作步骤**：

1. 在 `prepare_prefill` 的 `set_context(...)` 之前，临时插入打印（**示例代码，验证后删除**）：

   ```python
   # 示例代码：调试打印
   if is_debug := (seqs and seqs[0].block_table):   # 跳过 warmup
       print("==== prepare_prefill ====")
       for s in seqs:
           print(f"  seq_id={s.seq_id} start={s.num_cached_tokens} "
                 f"q={s.num_scheduled_tokens} end={s.num_cached_tokens+s.num_scheduled_tokens} "
                 f"block_table={s.block_table}")
       print("  cu_seqlens_q =", cu_seqlens_q)
       print("  cu_seqlens_k =", cu_seqlens_k)
       print("  slot_mapping =", slot_mapping)
   ```

2. 用 `example.py` 跑两个长度不同的 prompt。

**需要手算验证的现象**（以教学示意，设块大小 \(B=4\) 以便心算；真实 `kvcache_block_size=256`，断言要求它是 256 的整数倍）：

假设某 seq：`start=1`（命中 1 个 token 的前缀）、`seqlen_q=4`、`end=5`、`block_table=[10, 11]`（\(B=4\)，5 个 token 跨 2 块）。

- `cu_seqlens_q` 在本 seq 贡献 `+4`，`cu_seqlens_k` 贡献 `+5`，于是 `cu_seqlens_k[-1] > cu_seqlens_q[-1]` → 触发 `block_tables`。
- slot 手算（套公式 \(\text{slot}(p)=\text{block\_table}[\lfloor p/4\rfloor]\cdot 4 + (p\bmod 4)\)）：
  - \(p=1\): 块 0 → \(10\cdot4 + 1 = 41\)
  - \(p=2\): 块 0 → \(40 + 2 = 42\)
  - \(p=3\): 块 0 → \(40 + 3 = 43\)
  - \(p=4\): 块 1 → \(11\cdot4 + 0 = 44\)
  - 即 `slot_mapping = [41, 42, 43, 44]`，正好 4 个槽位，对应 4 个新 token。

3. 对照代码的逐块循环验证：`start_block=0`、`end_block=(5+3)//4=2`；
   - `i=0`（首块）：`slot_start=40+1=41`，非末块 → `slot_end=40+4=44` → `range(41,44)=[41,42,43]`；
   - `i=1`（末块）：`slot_start=44`，末块 → `slot_end=44 + 5 - 1*4 = 45` → `range(44,45)=[44]`；
   - 合并 `[41,42,43,44]`，与手算一致。✓

**预期结果**：打印出的 `slot_mapping` 长度等于该 seq 的 `num_scheduled_tokens`，且每个值都能用上面的公式由 `block_table` 和位置还原。真实运行数字依分词与块大小而定——**待本地验证**。

> 若无 GPU，可做「源码阅读型实践」：取 `example.py` 里两个 prompt 的分词长度，假设无前缀缓存（`start=0`），手写它们的 `cu_seqlens_q` 与 `cu_seqlens_k`，断言两者相等；再假设第二条命中长度为 \(c\) 的前缀，写出 `seqlen_q < seqlen_k` 的情形。

#### 4.2.5 小练习与答案

**练习 1**：前缀**未命中**时，`cu_seqlens_q` 与 `cu_seqlens_k` 有什么关系？为什么？
**答案**：二者完全相等。未命中时 `start = num_cached_tokens = 0`，于是 `seqlen_q = num_scheduled_tokens`、`end = seqlen_q`、`seqlen_k = end = seqlen_q`，每条序列对两个累积数组贡献相同。此时 `block_tables` 为 `None`，attention 走标准 varlen（query 和 key 等长、因果掩码），不需要查 cache。

**练习 2**：`positions` 为什么用 `range(start, end)` 而不是 `range(0, seqlen_q)`？
**答案**：因为位置编码（RoPE）需要的是 token 在**整条序列中的绝对位置**。前缀缓存命中时，新 token 的真实位置从 `start = num_cached_tokens` 开始，而不是 0。若从 0 开始，RoPE 给出的旋转角度会与已缓存前缀里的位置冲突，attention 出错。

**练习 3**：`slot_mapping` 的长度一定等于 `input_ids` 的长度吗？
**答案**：是的（非 warmup 情况下）。`input_ids.extend(seq[start:end])` 贡献 `seqlen_q` 个元素，逐块循环生成的 slot 区间总数也是 `end - start = seqlen_q` 个。而 `store_kvcache_kernel` 用 `slot_mapping.numel() == N`（N 为新算出的 token 数）做断言保证一一对应。

---

### 4.3 prepare_decode：单 token 解码批与 block_tables / context_lens

#### 4.3.1 概念说明

decode 阶段每条序列每步只产 1 个 token，结构与 prefill 截然不同：

- 每条 seq 只送入**一个** token（上一步采样得到的 `last_token`），所以 `input_ids` 长度等于序列数（batch size），无需 varlen 打包。
- attention 是「1 个新 query 去 attend 全部历史 key」，历史 K/V **全部在 cache 里**，必须通过 `block_tables` 读取。
- 需要告诉 attention 每条序列当前总长（`context_lens`），以及新 token 的 K/V 该写到哪个槽位（`slot_mapping`，每条序列一个值）。

#### 4.3.2 核心流程

对 batch 中每条 seq：

```text
input_ids[i]   = seq.last_token                 # 上一步采样的 token
positions[i]   = len(seq) - 1                    # 新 token 的绝对位置
context_lens[i]= len(seq)                        # 要 attend 的全部历史长度
slot_mapping[i]= block_table[-1] * B + last_block_num_tokens - 1   # 新 K/V 的写入槽位
```

decode 的 slot 公式（每条序列只有一个新 token，位置就是序列末尾 \(p = \text{num\_tokens}-1\)）：

\[
\text{slot} = \text{block\_table}[-1]\cdot B + (\text{last\_block\_num\_tokens} - 1)
\]

其中 `last_block_num_tokens` 是最后一块里 token 的个数（1 到 \(B\) 之间），减 1 转成「块内最后一个 token 的偏移」。当某个 token 刚好开启新块（即 `num_tokens % B == 1`，对应 u3-l1 的 `can_append` 判定），调度器会先 `may_append` 分配新块，于是 `block_table[-1]` 指向新块、`last_block_num_tokens == 1`、slot = 新块起点 + 0，正确指向新块第一个槽位。

#### 4.3.3 源码精读

`prepare_decode` 比 prefill 简洁得多：

[model_runner.py:172-188 — prepare_decode 构造单 token 批](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L172-L188)

```python
def prepare_decode(self, seqs: list[Sequence]):
    input_ids = []
    positions = []
    slot_mapping = []
    context_lens = []
    for seq in seqs:
        input_ids.append(seq.last_token)
        positions.append(len(seq) - 1)
        context_lens.append(len(seq))
        slot_mapping.append(seq.block_table[-1] * self.block_size + seq.last_block_num_tokens  - 1)
    input_ids = torch.tensor(input_ids, dtype=torch.int64, pin_memory=True).cuda(non_blocking=True)
    positions = torch.tensor(positions, dtype=torch.int64, pin_memory=True).cuda(non_blocking=True)
    slot_mapping = torch.tensor(slot_mapping, dtype=torch.int32, pin_memory=True).cuda(non_blocking=True)
    context_lens = torch.tensor(context_lens, dtype=torch.int32, pin_memory=True).cuda(non_blocking=True)
    block_tables = self.prepare_block_tables(seqs)
    set_context(False, slot_mapping=slot_mapping, context_lens=context_lens, block_tables=block_tables)
    return input_ids, positions
```

几个关键点：

- `positions.append(len(seq) - 1)`：decode 时 `last_token` 已经在 `postprocess` 里被 `append_token` 追加（u2-l2），所以 `len(seq)` 已包含它，新 token 的位置是 `num_tokens - 1`。
- `context_lens.append(len(seq))`：等于整条序列当前长度，flash-attn 用它（`cache_seqlens`）确定对每条序列要读 cache 里的前多少个 token。
- **decode 无条件构造 `block_tables`**：与 prefill 不同（prefill 仅在前缀缓存时才构造），decode 必须读历史 cache，所以每步都要块表。
- `set_context(False, slot_mapping=..., context_lens=..., block_tables=...)`：decode 不需要 `cu_seqlens` / `max_seqlen`（它们默认为 `None` / `0`），只传 slot_mapping、context_lens、block_tables 三件套。

`Attention` 在 decode 分支消费这些元数据的方式（u4-l2 详解）：

[attention.py:71-74 — decode 用 flash_attn_with_kvcache 读 paged cache](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L71-L74)

```python
else:    # decode
    o = flash_attn_with_kvcache(q.unsqueeze(1), k_cache, v_cache,
                                cache_seqlens=context.context_lens, block_table=context.block_tables,
                                softmax_scale=self.scale, causal=True)
```

#### 4.3.4 代码实践

**实践目标**：观察 decode 阶段 `context_lens` 与 `block_tables` 随步增长的过程。

**操作步骤**：

1. 在 `prepare_decode` 里临时打印（**示例代码，验证后删除**）：

   ```python
   # 示例代码：调试打印
   print("==== prepare_decode ====")
   print("  context_lens =", [len(s) for s in seqs])
   print("  last_block_num_tokens =", [s.last_block_num_tokens for s in seqs])
   print("  block_tables =", [s.block_table for s in seqs])
   print("  slot_mapping =", slot_mapping)
   ```

2. 跑 `example.py`，观察多步 decode。

**需要观察的现象**：

- `context_lens` 每步 +1（每条序列长 1 个 token）。
- 当某条序列长度跨过块边界（真实 \(B=256\) 时较难触发；可把 `kvcache_block_size` 调到 256 仍偏大，观察长期 decode 到 256、512……时）：`block_tables` 多出一个物理块号，`last_block_num_tokens` 归 1，`slot_mapping` 跳到新块起点。

**手算小例**（教学示意，\(B=4\)）：某 seq `num_tokens=10`，则 `num_blocks=(10+3)//4=3`、`last_block_num_tokens=10-(3-1)*4=2`，故 `block_table` 应有 3 项，设为 `[5,6,7]`。slot = `block_table[-1]*4 + last_block_num_tokens - 1 = 7*4 + 2 - 1 = 29`，即第 10 个 token（位置 9）写入物理块 7 的第 2 个槽位（块 7 起点 28，偏移 1 → 29）。用通用公式复核：位置 9 → 逻辑块 `9//4=2` → `block_table[2]=7`，偏移 `9%4=1`，`7*4+1=29`。✓

**预期结果**：`slot_mapping` 每条序列恰一个值，且等于 `block_table[-1]*B + last_block_num_tokens - 1`。具体数值——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：decode 为什么**每步**都要构造 `block_tables`，而 prefill 只在前缀缓存时才构造？
**答案**：decode 的 attention 必须**读取**全部历史 K/V，而历史 K/V 只存在于分页 cache 中，没有块表就无法定位，所以无条件需要 `block_tables`。prefill 在未命中前缀时，K/V 就是当前刚算出的连续张量，attention 直接用即可，不需要块表；只有命中前缀、需要从 cache 读历史段时才构造。

**练习 2**：decode 的 `slot_mapping` 长度是多少？为什么和 prefill 不同？
**答案**：长度等于 batch 中的序列数（每条序列 1 个）。因为 decode 每条序列每步只产生 1 个新 token，只需为其 K/V 指定 1 个写入槽位；prefill 每条序列产生 `num_scheduled_tokens` 个新 token，所以 `slot_mapping` 长度等于所有序列的新 token 总数。

**练习 3**：`positions.append(len(seq) - 1)` 中的 `-1` 能去掉吗？
**答案**：不能。`len(seq)` 返回 `num_tokens`，而 `last_token` 在 `postprocess` 的 `append_token` 里已经被追加，其 0-indexed 位置正是 `num_tokens - 1`。去掉 `-1` 会让位置编号比真实位置大 1，RoPE 旋转角度偏移，attention 结果错误。

---

## 5. 综合实践

把本讲的三块知识串起来：跟踪 **一次完整 prefill 步** 从 Sequence 到 GPU 张量的全过程，画出「字段流向图」。

**任务**：

1. 在 `LLMEngine.step`（[llm_engine.py:49-55](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L49-L55)）处确认：`schedule()` 返回的每条 seq 已被设置好 `num_scheduled_tokens`（在 [scheduler.py:46](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L46) 处）和 `num_cached_tokens`/`block_table`（在 [scheduler.py:45](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L45) 的 `allocate` 中）。
2. 仿照 4.2.4 的打印，在 `prepare_prefill` 里 dump 出两条不同长度 prompt 的 `cu_seqlens_q`、`cu_seqlens_k`、`slot_mapping`、`block_tables`、`max_seqlen_q/k`。
3. 画一张表，列出：每条 seq 的 `start / seqlen_q / end / seqlen_k`，以及它贡献的 `input_ids` 段、`positions` 段、`slot_mapping` 段。
4. 手验：`cu_seqlens_q` 与 `cu_seqlens_k` 的末项差，是否等于 batch 中命中前缀的 token 总数？`slot_mapping` 总长是否等于 `cu_seqlens_q[-1]`？每条 seq 的 slot 是否能由其 `block_table` + 位置还原？
5. 进阶：在 `prepare_decode` 跑若干步后，挑一条序列，画出它「跨块瞬间」那一步的 `block_table`、`last_block_num_tokens`、`slot_mapping` 三者变化，验证「开启新块时 slot 跳到新块起点」。

**产出**：一张从 `Sequence` 字段到 `Context` 字段的映射图，标注每个张量由哪些 seq 字段计算而来、被 `Attention` / `ParallelLMHead` 中哪一行消费。

## 6. 本讲小结

- `ModelRunner.run` 是调度器（Python 对象）与模型（GPU 张量）之间的桥梁，五步编排：prepare → sample → run_model → sample → reset，末尾的 `reset_context()` 保证 Context 的「每步」语义。
- prefill 用 **varlen 打包**把多条不等长序列拼成一维，用 `cu_seqlens_q`（新 token）/ `cu_seqlens_k`（含缓存前缀的全部 key）标记边界；二者相等表示无前缀缓存，`cu_seqlens_k` 更大表示命中前缀、需构造 `block_tables`。
- `slot_mapping` 用公式 \(\text{slot}(p)=\text{block\_table}[\lfloor p/B\rfloor]\cdot B+(p\bmod B)\) 把每个新 token 的 K/V 落到 paged cache 的物理槽位；prefill 按块逐段生成，decode 每序列一个值。
- decode 每序列送 1 个 token，`context_lens` = 序列总长（告诉 attention 读多少历史），`block_tables` 每步必构造（必须读 cache），`slot_mapping` = `block_table[-1]*B + last_block_num_tokens - 1`。
- `positions` 用**绝对位置**（前缀缓存时从 `num_cached_tokens` 起），保证 RoPE 与已缓存前缀一致。
- 除 `input_ids` / `positions` 外的所有注意力元数据都经全局 `Context` 传递，`Attention` / `ParallelLMHead` 用 `get_context()` 读取——这是后续 u4-l2、u4-l3 的接口基础。

## 7. 下一步学习建议

本讲构造出的 `slot_mapping`、`block_tables`、`cu_seqlens`、`context_lens` 会被底层 attention 消费。建议接着阅读：

- **u4-l2 Attention 与 Triton store_kvcache 内核**：看 `store_kvcache_kernel` 如何用 `slot_mapping` 把 K/V 写进 `k_cache`/`v_cache`，以及 `flash_attn_varlen_func` / `flash_attn_with_kvcache` 如何用 `block_tables` 读 cache——本讲「写到哪里、从哪读」的另一半。
- **u4-l3 Context 元数据传递机制**：精读 `Context` dataclass 与 `set_context` / `get_context` / `reset_context` 的生命周期，理解「为什么用全局变量传参而不改函数签名」，以及 `ParallelLMHead` 如何在 prefill 用 `cu_seqlens_q` 只取末 token。
- 之后 **u4-l4 Qwen3 模型结构** 与 **u4-l5 张量并行线性层**：理解 `self.model(input_ids, positions)` 内部到底算了什么，以及 `compute_logits` 前各并行层如何切分。

如果对 prefill 的「分块」细节还想加深，可回头结合 u2-l3（chunked prefill）与本讲 4.2，体会「同一条序列被切成多步 prefill 时，`start`/`end` 如何在多步间推进」。
