# Attention 与 Triton store_kvcache 内核

## 1. 本讲目标

上一篇（u4-l1）我们看到了 `ModelRunner` 如何把一批 `Sequence` 打包成 GPU 张量，并通过全局 `Context` 把 `slot_mapping`、`block_tables`、`cu_seqlens`、`context_lens` 等注意力元数据递交给模型。那些元数据「交给谁、怎么用」，正是本讲要回答的问题。

本讲钻进模型每一层都会调用的 [Attention](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L43-L75) 层，学完后你应当掌握：

1. `Attention.forward` 如何在 **prefill** 与 **decode** 两条路径间分流，分别调用 `flash_attn_varlen_func` 与 `flash_attn_with_kvcache`。
2. 新算出的 K/V 如何经 `store_kvcache` 与 Triton 内核 `store_kvcache_kernel` 写入 paged KV cache 的物理槽位。
3. 当部分前缀已经命中缓存时，prefill 如何改用「从 cache 读 K/V」而不是「直接用刚算出的 K/V」。

理解本讲后，你就补齐了「写 K/V」和「读 K/V」这两个方向，paged KV cache 的数据闭环就完整了。

## 2. 前置知识

在进入源码前，先用通俗语言把几个关键直觉说清楚。

**Flash Attention 是什么。** 标准 Attention 的核心运算是 \( \text{softmax}(QK^\top/\sqrt{d})V \)。朴素实现会先把完整的 \(QK^\top\) 注意力矩阵实例化到显存，长度一长就爆显存。Flash Attention 通过分块（tiling）和在线 softmax（边算边归一化），避免实例化整张注意力矩阵，大幅降低显存、顺便提速。nano-vllm 直接复用 `flash_attn` 库的两个函数，自己不重写注意力数学。

**varlen（变长）打包。** 一次 prefill 往往要把好几条不等长的 prompt 拼在一起算。Flash Attention 提供 `flash_attn_varlen_func`，用「累加长度数组」`cu_seqlens` 标记每条序列在拼接张量里的边界，从而在一次 kernel 调用里同时处理多条变长序列，互不串扰。这正是 u4-l1 讲过的 `cu_seqlens_q` / `cu_seqlens_k` 的用处。

**Paged KV cache 与槽位。** KV cache 不是一整块连续的「每序列一行」，而是被切成固定大小的**物理块**（block），每个块装 `block_size` 个 token 的 K/V。序列用一个 `block_table` 记录自己占用了哪些物理块编号。于是「第 p 个 token 的 K/V 住在哪里」被拆成两步：先由 `block_table` 找到所在物理块 `block_id`，再算块内偏移 `offset`，物理槽位号就是

\[
\text{slot} = \text{block\_id} \times \text{block\_size} + \text{offset}, \quad \text{offset} \in [0, \text{block\_size})
\]

这个 `slot` 就是 u4-l1 里 `ModelRunner.prepare_prefill` / `prepare_decode` 算出来、经 `Context.slot_mapping` 传进来的值。本讲的 Triton 内核就是按 `slot` 把 K/V 写进 cache。

**GQA（分组查询注意力）。** Qwen3 用的是 GQA：query 的头数（`num_heads`）多于 key/value 的头数（`num_kv_heads`），多出来的 query 头共享同一组 KV 头。`flash_attn` 原生支持这种头数不等的广播，所以你会在源码里看到 `num_heads` 与 `num_kv_heads` 两个不同的数。

**Triton 极简心智模型。** Triton 把 GPU 程序抽象成「很多个 program 实例并行执行同一段代码」，每个实例由 `tl.program_id(...)` 拿到自己的编号，据此决定处理哪一块数据。本讲的内核里，每个 program 恰好负责一个 token 的 K/V 写入。

## 3. 本讲源码地图

本讲几乎全部聚焦在同一个文件，但会引用上游如何喂数据、下游如何使用：

| 文件 | 作用 |
| --- | --- |
| [nanovllm/layers/attention.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py) | 本讲主角。包含 Triton 内核 `store_kvcache_kernel`、启动器 `store_kvcache`、`Attention` 模块。 |
| [nanovllm/utils/context.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/utils/context.py) | 全局 `Context`，承载 `slot_mapping`、`block_tables`、`cu_seqlens` 等元数据，是 `ModelRunner` 与 `Attention` 之间的「数据快递员」。 |
| [nanovllm/engine/model_runner.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py) | `prepare_prefill` / `prepare_decode` 构造 `slot_mapping` 等张量；`run_model` 在 CUDA Graph 路径用 `-1` 填充 `slot_mapping`。 |
| [nanovllm/models/qwen3.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py) | `Qwen3Attention.forward` 做 QKV 投影、q/k norm、RoPE，再把 `(N, heads, head_dim)` 形状的 q/k/v 交给 `Attention`。 |

一句话定位：`attention.py` 是「写 K/V 进 cache」和「读 K/V 算注意力」的交汇点——前者用自写的 Triton 内核，后者用现成的 flash_attn。

## 4. 核心概念与源码讲解

### 4.1 Attention 模块：prefill 与 decode 的两条路径

#### 4.1.1 概念说明

`Attention` 是一个 `nn.Module`，但它本身**不带任何可训练参数**（QKV 投影、o_proj 都在 `Qwen3Attention` 里）。它只持有两样运行时状态：本层 KV cache 的两个视图 `k_cache` / `v_cache`，以及一些形状常量。

它的职责可以浓缩成两句话：

- **写**：把本步新算出的 K/V 持久化进 paged cache，供将来读取。
- **读+算**：用 flash_attn 计算注意力，prefill 和 decode 走不同的函数。

这两件事是解耦的——写用自写 Triton 内核，读用 flash_attn 库。这种解耦是理解整个文件的关键。

#### 4.1.2 核心流程

`Attention.forward(q, k, v)` 的执行流程（`q/k/v` 形状均为 `(N, heads, head_dim)`，其中 `k/v` 的 `heads = num_kv_heads`）：

```text
1. context = get_context()                    # 取回本步元数据
2. 若 k_cache/v_cache 非空（非 warmup）：
       store_kvcache(k, v, k_cache, v_cache, context.slot_mapping)   # 写 K/V
3. 分流：
   if context.is_prefill:
       if context.block_tables is not None:   # 已有前缀在 cache
           k, v = k_cache, v_cache             # 改为从 cache 读全部 K/V
       o = flash_attn_varlen_func(q, k, v, cu_seqlens_q, cu_seqlens_k,
                                  causal=True, block_table=context.block_tables)
   else:  # decode
       o = flash_attn_with_kvcache(q.unsqueeze(1), k_cache, v_cache,
                                   cache_seqlens=context.context_lens,
                                   block_table=context.block_tables, causal=True)
4. return o
```

三种读法的区别是本模块的重点：

- **prefill 且无缓存前缀**：`k/v` 是本步为全部 token 新算出的张量，直接喂给 `flash_attn_varlen_func`，`cu_seqlens_q == cu_seqlens_k`，无需 `block_table`。注意：即便走这条路径，第 2 步的 `store_kvcache` 仍然执行——把 K/V 写进 cache 是为了**后续的 decode** 能读到它们。
- **prefill 且有缓存前缀**（前缀缓存命中，或分块 prefill 的后续块）：本步只算了「后缀」那几个新 token 的 K/V（已由第 2 步写进 cache），而注意力需要看完整序列。于是把 `k, v` 重指向整张 `k_cache/v_cache`，并通过 `block_table` 让 flash_attn 去 paged cache 里把「前缀 + 刚写入的后缀」一起读出来。
- **decode**：每序列只算 1 个新 token，直接调用面向 paged cache 的 `flash_attn_with_kvcache`，用 `context_lens` 指明每条序列要读多长的历史，`block_table` 指明历史分散在哪些物理块里。

#### 4.1.3 源码精读

先看模块构造与空 cache 占位：

[attention.py:L43-L57 — Attention.\_\_init\_\_](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L43-L57)：保存 `num_heads/head_dim/scale/num_kv_heads`，并把 `k_cache/v_cache` 初始化成空张量 `torch.tensor([])`。真正的 cache 视图要等到 `ModelRunner.allocate_kv_cache` 之后才挂上来（参见 u3-l3），所以 `forward` 里用 `if k_cache.numel() and v_cache.numel()` 判断「cache 是否已就绪」，warmup 时它还是空的、跳过写入。

再看核心前向：

[attention.py:L59-L75 — Attention.forward](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L59-L75)：第 62–63 行无条件写 K/V；第 64–70 行是 prefill 分支；第 71–74 行是 decode 分支。两个关键细节：

- 第 65–66 行的 `k, v = k_cache, v_cache` 是「读法切换」——一旦命中前缀，注意力就改读 cache 而非用刚算出的 `k/v`。
- 第 72 行 `q.unsqueeze(1)` 把 `(B, num_heads, head_dim)` 变成 `(B, 1, num_heads, head_dim)`，因为 `flash_attn_with_kvcache` 期望 query 带一个 `seqlen_q` 维度，decode 时该维度恰好是 1。

至于 `q/k/v` 的来源，可对照 [qwen3.py:L77-L86 — Qwen3Attention.forward](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L77-L86)：QKV 投影后，`k = k.view(-1, num_kv_heads, head_dim)`、`v = v.view(-1, num_kv_heads, head_dim)`，这正是 `store_kvcache` 里 `key.shape = (N, num_heads, head_dim)` 的来源（注意那里的「num_heads」其实指 KV 头数，见 4.2）。

#### 4.1.4 代码实践

**实践目标**：观察 prefill 与 decode 两条分支的分流。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 [attention.py:L59-L75](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L59-L75)。
2. 对照 u4-l1 的 [model_runner.py:L169](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L169)（prefill 设 `is_prefill=True` 并可能在第 163 行设置 `block_tables`）与 [model_runner.py:L187](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L187)（decode 设 `is_prefill=False` 且必设 `block_tables`）。
3. 在一张纸上把 prefill 和 decode 各自会读到的 `Context` 字段列成两列。

**需要观察的现象**：prefill 分支可能带也可能不带 `block_tables`；decode 分支**一定**带 `block_tables` 且用 `context_lens` 而非 `cu_seqlens`。

**预期结果**：你能解释「为什么 decode 不需要 `cu_seqlens`」（每序列只 1 个 query，无需标记变长边界），而 prefill 需要。

**待本地验证**：若你有 GPU 环境，可在 `Attention.forward` 第 64、71 行各加一行 `print(context.is_prefill, q.shape)`，跑一次 `example.py`，确认 prefill 时 `q` 的第 0 维是「本步所有新 token 总数」，decode 时是「batch size」。

#### 4.1.5 小练习与答案

**练习 1**：prefill 没有命中前缀缓存时，`Attention.forward` 为什么仍然调用 `store_kvcache`？
**答**：因为本步算出的 K/V 必须持久化进 cache，后续的 decode（或分块 prefill 的后续块）才能读到它们；只是这一步的注意力直接复用刚算出的 `k/v`，不读 cache 而已。

**练习 2**：decode 时 `q` 为什么要 `unsqueeze(1)`？
**答**：`flash_attn_with_kvcache` 要求 query 形状为 `(B, seqlen_q, num_heads, head_dim)`，decode 每序列只有 1 个 query token，所以要补一个 `seqlen_q=1` 的中间维度。

**练习 3**：prefill 分支里 `context.block_tables is not None` 何时成立？
**答**：当 `cu_seqlens_k[-1] > cu_seqlens_q[-1]`，即存在已缓存的前导 token——前缀缓存命中，或分块 prefill 的后续块——`prepare_prefill` 才会构建 `block_tables`（见 [model_runner.py:L162-L163](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L162-L163)）。

### 4.2 store_kvcache：把新 K/V 写入 paged cache 的启动器

#### 4.2.1 概念说明

`store_kvcache` 是一个很薄的 Python 包装函数：它不真正算东西，只做两件事——**校验张量布局**，然后**启动 Triton 内核**。把它单列出来，是因为它把「上层 K/V 张量形状」和「下层 cache 内存布局」对接起来，是理解内核索引的前提。

#### 4.2.2 核心流程

```text
输入：key, value 形状 (N, num_kv_heads, head_dim)
     k_cache, v_cache 形状 (num_blocks, block_size, num_kv_heads, head_dim)
     slot_mapping 长度 N
1. 令 D = num_kv_heads * head_dim   # 每个 token 的 KV 向量展宽后的长度
2. 断言 key/value 最内维连续 (stride(-1)==1)
3. 断言 key/value 的头数维步长 == head_dim
4. 断言 k_cache/v_cache 的 block_size 维步长 == D
5. 断言 slot_mapping.numel() == N
6. 以网格 (N,) 启动内核，每 token 一个 program
```

为什么需要这些断言？内核里会用「扁平偏移」一次读写整段 D 元素（`tl.arange(0, D)`），这要求每个 token 的 KV 在内存里是连续的 D 个数；同时 cache 也必须按 D 对齐，才能用 `slot * D` 直接定位。断言就是在保证这两点，否则内核的扁平读写会越界或错位。

#### 4.2.3 源码精读

[attention.py:L33-L40 — store_kvcache](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L33-L40)：

- 第 34 行 `N, num_heads, head_dim = key.shape`：这里的局部变量名 `num_heads` 其实是 **KV 头数**（因为传入的 `key` 来自 `Qwen3Attention` 里 `k.view(-1, num_kv_heads, head_dim)`），不要被名字误导。
- 第 35 行 `D = num_heads * head_dim`：即每个 token 的 K（或 V）展平后的元素数。
- 第 36–39 行：四条 `assert`，分别保证 `head_dim` 连续、`(heads, head_dim)` 二维连续、cache 的 `block_size` 维步长恰为 `D`、`slot_mapping` 长度等于 token 数。
- 第 40 行 `store_kvcache_kernel[(N,)](...)`：网格形状 `(N,)`，即 N 个 program 并行，每个 program 写一个 token。

关于 cache 布局，可回看 u3-l3：[model_runner.py:L115](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L115) 创建的 `kv_cache` 形状是 `(2, num_layers, num_blocks, block_size, num_kv_heads, head_dim)`，再由 [model_runner.py:L117-L121](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L117-L121) 把每一层的 `k_cache = kv_cache[0, layer_id]`、`v_cache = kv_cache[1, layer_id]` 挂到对应的 `Attention` 模块上。于是 `(num_blocks, block_size, num_kv_heads, head_dim)` 这个连续 4 维张量的 `block_size` 维步长正是 `num_kv_heads * head_dim = D`，与断言吻合。

#### 4.2.4 代码实践

**实践目标**：验证 cache 的步长与断言一致。

**操作步骤**（纯 CPU 思想实验，可复制运行）：

```python
import torch
# 模拟一层 cache 的形状（数字随便取，只看 stride）
num_blocks, block_size, num_kv_heads, head_dim = 100, 256, 8, 128
k_cache = torch.empty(num_blocks, block_size, num_kv_heads, head_dim)
D = num_kv_heads * head_dim
print("stride(1) =", k_cache.stride(1), " D =", D)   # 期望两者相等
```

**需要观察的现象**：打印出的 `stride(1)` 应等于 `D = 8*128 = 1024`。

**预期结果**：断言 `k_cache.stride(1) == D` 成立，说明 `slot * D` 能正确定位。

**待本地验证**：在真实模型上 `print(self.attn.k_cache.stride())`，确认最内三维步长依次为 `head_dim`、`D`、`block_size*D`。

#### 4.2.5 小练习与答案

**练习 1**：`store_kvcache` 里的局部变量 `num_heads` 实际代表什么？
**答**：实际是 `num_kv_heads`（因为 `key` 来自 `k.view(-1, num_kv_heads, head_dim)`），所以 `D = num_kv_heads * head_dim`。

**练习 2**：四条 stride 断言各自保证什么？
**答**：`stride(-1)==1` 保证 `head_dim` 连续；`stride(1)==head_dim` 保证 `(num_kv_heads, head_dim)` 这两维连成一段；`k_cache.stride(1)==D` 保证 cache 的 `block_size` 维步长恰为 D，使 `slot*D` 偏移成立；`slot_mapping.numel()==N` 保证一个 token 对一个 slot。

**练习 3**：为什么网格大小是 `N` 而不是 `N * D`？
**答**：每个 program 用向量化的 `tl.arange(0, D)` 一次性处理整段 D 元素，所以只需 N 个 program（每 token 一个），让 Triton 在 program 内部并行处理 D 维。

### 4.3 store_kvcache_kernel：Triton 内核逐 token 写入

#### 4.3.1 概念说明

`store_kvcache_kernel` 是用 Triton 写的 JIT 内核，做一件极其聚焦的事：**每个 program 把一个 token 的 K 和 V 复制到它在 paged cache 里的物理槽位**。之所以要用 Triton 自己写而不是用 PyTorch 的索引赋值，是因为 paged「散列写入」（按 `slot_mapping` 把连续 token 散到不连续的物理块）属于 gather/scatter，自定义内核能把「读一段连续 KV、写到任意 slot」压成一次高效的向量化访存，避免 Python 索引开销——这对 decode 那种每步都要写、又被 CUDA Graph 反复回放（见 u5-l1）的热路径尤其重要。

#### 4.3.2 核心流程

每个 program 实例 `idx`（范围 `[0, N)`）做：

```text
slot = slot_mapping[idx]
if slot == -1: return              # 跳过填充位
key   = load(key_ptr   + idx*key_stride   + [0..D))    # 本 token 的 K（D 个元素）
value = load(value_ptr + idx*value_stride + [0..D))    # 本 token 的 V
cache_off = slot * D + [0..D)
store(k_cache_ptr + cache_off, key)
store(v_cache_ptr + cache_off, value)
```

写入偏移的数学关系（把 cache 视为扁平内存）：

\[
\text{offset}(\text{slot}, d) = \text{slot} \cdot D + d, \qquad d \in [0, D),\quad D = H_{kv} \cdot d_h
\]

由于 cache 是 `(num_blocks, block_size, num_kv_heads, head_dim)` 的连续张量，而 `slot = block_id * block_size + offset`，代入得

\[
\text{offset} = (\text{block\_id}\cdot \text{block\_size} + \text{offset}) \cdot D + d
\]

恰好落在该 token 应在的 `(block_id, offset, :, :)` 位置，于是「扁平 slot」与「多维索引」自然对齐，内核不需要任何分支。

#### 4.3.3 源码精读

[attention.py:L10-L30 — store_kvcache_kernel](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L10-L30)，逐行解读：

- 第 21 行 `idx = tl.program_id(0)`：本 program 负责第 `idx` 个 token。
- 第 22 行 `slot = tl.load(slot_mapping_ptr + idx)`：读出该 token 的物理槽位号。
- 第 23 行 `if slot == -1: return`：**跳过填充位**。`slot=-1` 来自 CUDA Graph 路径——[model_runner.py:L206-L207](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L206-L207) 里 `run_model` 先把 `graph_vars["slot_mapping"].fill_(-1)`，再只把真实的 `[:bs]` 个 slot 写进去，超出实际 batch 的位置保持 `-1`；内核必须跳过它们，否则会把垃圾 K/V 写进 cache 里无辜的物理块。
- 第 24–27 行：用 `tl.arange(0, D)` 向量化地读出本 token 的 K 与 V（`D` 是 `tl.constexpr`，编译期常量，Triton 会据此展开向量化访存）。
- 第 28–30 行：算出 cache 偏移 `slot * D + [0..D)`，把 K 写进 `k_cache`、V 写进 `v_cache`。注意 K 和 V 用的是**同一个 slot**，因为它们在 `kv_cache` 里分别挂在 `[0, layer]` 和 `[1, layer]`，每层各自的 `k_cache`/`v_cache` 视图共享同样的 `(block, offset)` 语义。

#### 4.3.4 代码实践

**实践目标**：给定一组 `slot_mapping`，亲手画出 key/value 写入 `k_cache`/`v_cache` 的偏移映射，并解释 `slot=-1` 的跳过分支。

**操作步骤**：

1. 阅读内核 [attention.py:L10-L30](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L10-L30)。
2. 设定一个玩具场景：`block_size = 4`，`num_kv_heads = 1`，`head_dim = 2`，故 `D = 2`。本步有 3 个 token 待写入，`slot_mapping = [5, 6, -1]`，对应 key 为 `[[a0,a1],[b0,b1],[c0,c1]]`（第三个是 CUDA Graph 填充位）。
3. 按 `offset = slot*D + d` 逐项算出写入位置，填表：

   | token idx | slot | key 写入偏移（k_cache） | 是否写入 |
   | --- | --- | --- | --- |
   | 0 | 5 | 偏移 10, 11 ← `[a0,a1]` | 是 |
   | 1 | 6 | 偏移 12, 13 ← `[b0,b1]` | 是 |
   | 2 | -1 | —— | **跳过**（第 23 行 return） |

4. 用下面这段纯 PyTorch（CPU）脚本验证你的映射和内核语义一致：

```python
import torch

def store_kvcache_torch(key, value, k_cache, v_cache, slot_mapping):
    # 形状: key (N, num_kv_heads, head_dim); k_cache (num_blocks, block_size, num_kv_heads, head_dim)
    N = key.shape[0]
    D = key.shape[1] * key.shape[2]
    key_flat = key.reshape(N, D)
    val_flat = value.reshape(N, D)
    k_view = k_cache.reshape(-1, D)     # 把 cache 看成 (num_slots, D)
    v_view = v_cache.reshape(-1, D)
    for i in range(N):
        slot = int(slot_mapping[i])
        if slot == -1:
            continue                    # 对应内核的 if slot == -1: return
        k_view[slot] = key_flat[i]
        v_view[slot] = val_flat[i]

# 构造玩具数据
block_size, num_kv_heads, head_dim = 4, 1, 2
num_blocks = 4                          # 共 16 个 slot
k_cache = torch.zeros(num_blocks, block_size, num_kv_heads, head_dim)
v_cache = torch.zeros_like(k_cache)
key   = torch.tensor([[1.,2.],[3.,4.],[5.,6.]]).reshape(3,1,2)
value = torch.tensor([[7.,8.],[9.,10.],[11.,12.]]).reshape(3,1,2)
slot_mapping = torch.tensor([5, 6, -1])

store_kvcache_torch(key, value, k_cache, v_cache, slot_mapping)
print("k_cache flat =", k_cache.reshape(-1, 2))   # 第 5、6 行应被写入，其余为 0
```

**需要观察的现象**：扁平化后的 `k_cache` 第 5 行是 `[1,2]`、第 6 行是 `[3,4]`，对应 slot 5、6；第三个 token 因 `slot=-1` 被跳过，没有任何位置被写成 `[5,6]`。`v_cache` 同理。

**预期结果**：你画出的偏移表与脚本输出一致，从而验证 `offset = slot*D + d` 的映射以及 `slot=-1` 跳过分支。

**待本地验证**：在装了 `triton` + GPU 的机器上，可直接调用真实的 `store_kvcache`（注意 `key`/`value` 需在 cuda、且满足 stride 断言），对比它与上面 torch 版本的输出是否逐元素相同。

#### 4.3.5 小练习与答案

**练习 1**：`slot == -1` 的来源是什么？为什么必须跳过？
**答**：来自 CUDA Graph 路径的 `run_model`：先 `fill_(-1)` 再只写 `[:bs]`，超出实际 batch 的填充位是 `-1`；若不跳过，内核会把无效 K/V 写进 cache 里不该被改动的物理块，污染后续 attention。

**练习 2**：给定 `block_size=4, D=2, slot=6`，本 token 的 K 写入哪些偏移？
**答**：偏移 `6*2=12` 与 `13`。

**练习 3**：为什么内核用 `slot*D` 这种扁平偏移，而不是显式用 `(block_id, offset)` 二维索引？
**答**：因为 cache 是 `(num_blocks, block_size, num_kv_heads, head_dim)` 的连续张量，`slot*D + d` 恰好等价于正确的多维扁平偏移，既省去分支又便于 `tl.arange(0, D)` 向量化读写。

## 5. 综合实践

把本讲三个模块串起来：用一次极小规模的 prefill+decode，把「写 K/V」与「读 K/V」走完整闭环。

1. 假设有 2 条序列，block_size 取小值（如 4），手算每条序列的 `block_table`、prefill 的 `slot_mapping`（仿照 [model_runner.py:L151-L161](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L151-L161)）。
2. 假设两条序列共享一段前缀，给第二条序列标出命中缓存的块，写出 prefill 时 `cu_seqlens_q` 与 `cu_seqlens_k` 的差异，并据此推断 `Attention.forward` 会走「`k,v = k_cache,v_cache`」那条分支。
3. 把上述手算结果填进 4.3.4 的 torch 脚本，验证「新 token 写入 cache」的偏移，再口述一遍：紧接着的 decode 会用 `flash_attn_with_kvcache` 通过 `block_table` 把这些刚写入的 K/V 连同历史一起读出来。
4. 最后回答一个问题：如果某一步 `slot_mapping` 里混入了 `-1`，闭机的哪一环会出问题？（提示：写阶段）

如果手算与脚本吻合，并能自洽地解释读、写两个方向，说明你已经吃透了 paged KV cache 在 `Attention` 层的数据闭环。

## 6. 本讲小结

- `Attention` 不含参数，只做两件事：用自写 Triton 内核**写**新 K/V 进 paged cache，用 flash_attn **读+算**注意力。
- prefill 与 decode 分流：prefill 用 `flash_attn_varlen_func`（变长打包、`cu_seqlens`），decode 用 `flash_attn_with_kvcache`（paged 读、`context_lens` + `block_table`），后者还要 `q.unsqueeze(1)` 补 `seqlen_q=1`。
- 当已有前缀在 cache（前缀缓存命中或分块 prefill 后续块）时，prefill 会把 `k,v` 重指向 `k_cache,v_cache`，改读整段缓存而非用刚算出的 K/V。
- `store_kvcache` 是薄启动器，靠四条 stride 断言保证「每 token 的 KV 是连续 D 元素、cache 按 D 对齐」，再用 `slot*D` 定位。
- `store_kvcache_kernel` 每个 program 写一个 token；`slot == -1` 分支跳过 CUDA Graph 的填充位，防止污染 cache。
- K 与 V 共用同一 `slot`，分别落在 `kv_cache[0, layer]` 与 `kv_cache[1, layer]`，保证一个 slot 号、所有层与 K/V 同步。

## 7. 下一步学习建议

- 想知道这些元数据是怎么在 `ModelRunner` 与 `Attention` 之间「不传参数就传过去」的，请看 **u4-l3 Context 元数据传递机制**。
- 想从整体上把握一个 token 在 Qwen3 各层里的前向旅程（含 q/k norm、RoPE、MLP、残差 RMSNorm），请看 **u4-l4 Qwen3 模型结构详解**。
- 想理解 decode 阶段为什么 `slot_mapping` 会出现 `-1`、以及 CUDA Graph 如何回放这段写 cache 的计算，请看 **u5-l1 CUDA Graph 捕获与回放**。
- 建议结合阅读 [flash_attn 文档](https://github.com/Dao-AILab/flash-attention) 中 `flash_attn_varlen_func` 与 `flash_attn_with_kvcache` 两个接口的参数说明，把本讲对函数行为的推断与官方语义对照确认。
