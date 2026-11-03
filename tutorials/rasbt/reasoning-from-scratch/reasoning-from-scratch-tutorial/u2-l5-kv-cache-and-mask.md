# 第 5 讲　KV Cache 与因果掩码：prefill 与缓存解码的两阶段生成

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `Qwen3Model` 上的模型级状态 `current_pos` 与 `forward` 的 `cache` 参数如何配合，把生成切成 **prefill（整段预填）** 与 **decode（逐 token 缓存解码）** 两个阶段。
2. 亲手推导缓存解码时掩码切片 `[pos_start:pos_end, :pos_end]` 的含义：行对应"新来的 token"，列对应"缓存里的全部旧 key + 新 key"。
3. 说明 `reset_kv_cache()` 为什么必须在每次新生成前调用，以及超过 `context_length` 的序列为何触发 `ValueError`、且被拒绝的调用不会污染已有状态。
4. 读懂 `KVCache` 类的"每层一个槽位"设计，以及 GQA 中 `torch.cat` 沿 token 维的增量拼接——并理解缓存里存的是 repeat_interleave 展开前的**压缩版** K/V。

本讲是单元二（模型架构）的收官：u2-l4 留下的悬念"`current_pos` 的完整生命周期"在这里闭环。

## 2. 前置知识

- **自回归生成**（u1-l2、u2-l1）：模型每次前向只关心"最后一个位置的 logits"，从中挑出下一个 token，再把新 token 拼回输入继续。回顾 u2-l1 的数据流：`tok_emb → 28 × TransformerBlock → final_norm → out_head`。
- **注意力的 key/value 复用**（u2-l3）：注意力打分是 \( QK^{\top} \)。生成第 \( t \) 个 token 时，query 只有 1 个新向量，但 key/value 是前 \( t \) 个位置的全部向量——而其中前 \( t-1 \) 个在上一步已经算过，且**结果不会改变**（ causal 注意力保证旧 token 看不到未来，RoPE 旋转角度也只依赖绝对位置）。这就是 KV cache 可行的根本原因：把算过的 K/V 存起来，避免重复计算。
- **RoPE 的 offset**（u2-l4）：`apply_rope(x, cos, sin, offset)` 用 `cos[offset:offset+seq_len]` 取角度，让增量解码的新 token 按其**绝对位置**旋转。offset 从哪来？正是本讲的 `current_pos`。
- **因果掩码**（u2-l1、u2-l3）：`attn_scores.masked_fill(mask, -torch.inf)` 把"看未来"的位置压成负无穷，softmax 后权重为 0。
- **两个术语**：
  - **prefill（预填）**：把整段 prompt（比如 4 个或 400 个 token）一次性送入模型，既产出第一个新 token 的 logits，也顺便把 prompt 所有位置的 K/V 写入缓存。
  - **decode（解码）**：之后每步只送入 **1 个**新 token，与缓存中的旧 K/V 拼接后做注意力。
- ** KV cache 的显存账本**：缓存不吃算力省下的"免费午餐"，它占显存。估算公式：

  \[ \text{显存} \approx n_{\text{layers}} \times 2 \times n_{\text{kv}} \times d_h \times L \times b \times \text{字节每元素} \]

  其中 2 指 K 和 V 两份。对 Qwen3-0.6B（28 层、8 个 KV 组、head_dim=128、bfloat16 每元素 2 字节），每个 token 约 \( 28 \times 2 \times 8 \times 128 \times 2\,\text{B} = 112\,\text{KiB} \)；把 40960 的上下文填满约需 **4.4 GiB**。若没有 GQA（16 个头全存），直接翻倍到约 8.8 GiB——这正是 u2-l3 说"GQA 压缩 KV cache"的定量含义。

## 3. 本讲源码地图

| 文件 | 关键位置 | 作用 |
| --- | --- | --- |
| `reasoning_from_scratch/qwen3.py` | `KVCache` 类（L422-L437） | 极简的按层缓存容器：`get` / `update` / `get_all` / `reset` |
| `reasoning_from_scratch/qwen3.py` | `Qwen3Model.__init__` 的 `current_pos`（L56） | 模型级位置游标，生命周期是本讲主线 |
| `reasoning_from_scratch/qwen3.py` | `Qwen3Model.forward`（L58-L102） | 位置追踪、越界检查、两种掩码构造、逐层读写缓存 |
| `reasoning_from_scratch/qwen3.py` | `Qwen3Model.reset_kv_cache`（L104-L105） | 把 `current_pos` 归零 |
| `reasoning_from_scratch/qwen3.py` | `GroupedQueryAttention.forward`（L183-L225） | `torch.cat` 增量拼接新旧 K/V、RoPE offset、掩码应用 |
| `reasoning_from_scratch/ch02.py` | `generate_text_basic_cache`（L64-L91） | 缓存的标准用法：新建 cache → reset → prefill → 逐 token decode |
| `tests/test_qwen3_context_limits.py` | 三个测试（L48-L88） | 用缩小配置验证越界报错与"被拒调用不污染缓存" |

本讲的所有实践都可以用**随机初始化的小模型**完成，不需要下载 0.6B 权重。

## 4. 核心概念与源码讲解

### 4.1 KVCache：每层一个槽位的极简缓存容器

#### 4.1.1 概念说明

KV cache 的第一问题是：**缓存放哪里、按什么粒度组织？**

回顾 u2-l2：`TransformerBlock.forward` 返回 `(x, next_cache)`——每一层注意力都会产出自己的一份 `(keys, values)`。28 层就有 28 份互不相干的缓存，所以缓存容器的自然粒度是**层**。

本仓库的答案简单到几乎"不像个类"：一个长度为 `n_layers` 的 Python 列表，每个槽位存该层的 `(keys, values)` 元组（或 `None`）。它不预分配张量、不知道形状、不做任何计算——纯数据搬运。对比：`qwen3_optimized.py` 里有一个"预分配张量"版 KVCache（u10-l1 会专门讲），那条路线用固定大小的张量 + 视图切片换取更少的内存分配次数。

另一个设计要点：**缓存对象（`KVCache` 实例）与位置游标（模型上的 `current_pos`）是分离的**。每次生成通常新建一个 cache（对应一段新对话），而 `current_pos` 挂在模型实例上会跨调用残留——这就是 `reset_kv_cache()` 存在的原因，4.2 节展开。

#### 4.1.2 核心流程

`KVCache` 的生命周期与一次生成严格同构：

```text
新建 KVCache(n_layers)          # [None, None, ..., None]
        │
prefill: forward(prompt, cache) # 每层首次拿到 cache.get(i) == None
        │                       # GQA 走"无旧缓存"分支，返回 (k, v)
        ▼
cache.update(i, (k, v))         # forward 内部逐层写入
        │
decode × N: forward(tok, cache) # get(i) 取出上一步的 (k, v)
        │                       # GQA 拼接后返回新的 (k, v)，再次 update
        ▼
生成结束，cache 对象丢弃
```

接口只有四个：

| 方法 | 行为 |
| --- | --- |
| `get(layer_idx)` | 返回该层缓存（初始为 `None`） |
| `update(layer_idx, value)` | 覆盖该层缓存 |
| `get_all()` | 返回整个列表 |
| `reset()` | 全部槽位置 `None`（注意：不重置模型上的 `current_pos`） |

#### 4.1.3 源码精读

KVCache 类全文只有 16 行：

这里是类定义与初始化，`self.cache` 就是一个填 `None` 的列表，长度等于层数：[qwen3.py:L422-L424](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L422-L424)

这里是四个读写接口：`get` / `update` / `get_all` / `reset`，全部是 O(1) 的列表操作，没有任何张量运算：[qwen3.py:L426-L437](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L426-L437)

两个容易忽略的细节：

1. **每个槽位存的是元组 `(keys, values)`，不是两个列表。** 槽位类型由 GQA 的 `next_cache = (keys, values)` 决定（见 4.3.3）。
2. **`KVCache.reset()` 与 `model.reset_kv_cache()` 是两个不同的方法！** 前者清空槽位列表，后者把模型的 `current_pos` 归零（[qwen3.py:L104-L105](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L104-L105)）。仓库的生成函数只调用后者，因为它们每次都新建 cache 对象，前者自然用不上。

#### 4.1.4 代码实践

**实践目标**：不碰模型，单独把 `KVCache` 当成一个数据结构玩一遍，确认它"什么都不是，只是一个列表"。

**操作步骤**（示例代码，可直接在仓库根目录的 Python 里运行）：

```python
import torch
from reasoning_from_scratch.qwen3 import KVCache

cache = KVCache(n_layers=3)
print("初始槽位:", cache.get_all())            # [None, None, None]

k = torch.zeros(1, 2, 4, 8)                    # (b, kv_groups, tokens, head_dim)
v = torch.ones(1, 2, 4, 8)
cache.update(0, (k, v))                        # 往第 0 层写入一份 (K, V)

print("第 0 层:", type(cache.get(0)), cache.get(0)[0].shape)
print("第 1 层:", cache.get(1))                # 仍是 None

cache.reset()
print("reset 后:", cache.get_all())            # 又全变 None
```

**需要观察的现象**：`get_all()` 的输出从 `[None, None, None]` → 第 0 层变成元组 → reset 后回到全 `None`。

**预期结果**：打印出的第 0 层形状为 `torch.Size([1, 2, 4, 8])`；全程没有任何张量计算发生。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `KVCache` 不像多数 PyTorch 组件那样挂在 `nn.Module` 上、注册成 buffer？

**参考答案**：buffer 会随模型进入 `state_dict` 并被 `.to(device)`、保存/加载一起搬运，而 KV cache 是**一次生成内的临时数据**：不该被序列化进 checkpoint，也不该参与梯度管理（生成都在 `torch.inference_mode()` 下进行）。普通 Python 列表天然满足"用完即弃、不进状态字典"。

**练习 2**：`KVCache.reset()` 把槽位清空后，如果立刻接着 decode 一个 token，会发生什么？

**参考答案**：每层 `cache.get(i)` 又变回 `None`，GQA 走"无旧缓存"分支（见 4.3.3），等价于把这一个 token 当成全新序列的开头做前向；但模型的 `current_pos` 并未被这个方法重置，位置游标和缓存内容就此错位——所以仓库从不单独这样用。

### 4.2 Qwen3Model.forward：current_pos 位置追踪与广播掩码

#### 4.2.1 概念说明

`Qwen3Model.forward` 是两阶段生成的**总调度**。它要回答三个问题：

1. **这次输入的 token 处在序列的什么绝对位置？** 由 `current_pos`（有 cache 时）回答。RoPE 要按绝对位置取角度（u2-l4），掩码要按绝对位置切行——`pos_start` 是两者的共同输入。
2. **这次的注意力掩码该长什么样？** 无 cache 时是熟悉的严格上三角 `(T, T)`；有 cache 时，query 只有新 token，key 却有"缓存的全部旧 token + 新 token"，掩码变成 `(新 token 数, 总 key 数)` 的"宽矩形"。
3. **越界了吗？** `pos_end` 一旦超过 `context_length` 立刻报 `ValueError`——因为 RoPE 的 cos/sin 表只预计算到 `context_length` 个位置（u2-l4），越界位置根本没有角度可用。

`current_pos` 的生命周期（本讲对 u2-l4 承诺的闭环）共四站：**初始化为 0（L56）→ 有 cache 的前向中读出作为 `pos_start`（L64）→ 前向成功后更新为 `pos_end`（L73）→ `reset_kv_cache()` 归零（L105）**。

#### 4.2.2 核心流程

`forward(in_idx, cache=None)` 的调度逻辑：

```text
输入 in_idx: (b, num_tokens)
        │
        ├─ num_tokens = x.shape[1]
        ├─ pos_start = current_pos  (cache 有值时)  否则 0
        ├─ pos_end   = pos_start + num_tokens
        ├─ 若 pos_end > context_length → raise ValueError   ← 先检查，后改状态！
        │
        ├─ cache 不为 None:
        │     current_pos = pos_end                 ← 游标前移
        │     mask = triu(ones(pos_end, pos_end), diag=1)[pos_start:pos_end, :pos_end]
        │            # 先造"全序列"因果掩码，再切出新 token 的行
        └─ cache 为 None:
              mask = triu(ones(num_tokens, num_tokens), diag=1)
        │
        ├─ mask = mask[None, None, :, :]            ← 升成 (1,1,·,·) 便于广播
        │
        └─ for 每层 block:
              blk_cache = cache.get(i) if cache else None
              x, new_blk_cache = block(x, mask, cos, sin, start_pos=pos_start, cache=blk_cache)
              cache.update(i, new_blk_cache)        ← 新缓存写回
```

**掩码切片的数学**。设新 token 在批内序号为 \( i \)（绝对位置 \( p = \text{pos\_start} + i \)），key 的绝对位置为 \( j \)。全序列严格上三角掩码在 \( (p, j) \) 处为 True 当且仅当 \( j > p \)。行切片 `[pos_start:pos_end]` 取出 \( p \) 的所有可能，列切片 `[:pos_end]` 取出全部 key，于是：

\[ \mathrm{mask}[i, j] = \mathbb{1}[\,j > \mathrm{pos\_start} + i\,] \]

即"新 token \( i \) 只被禁止看到严格晚于它的位置"。两个特例：

- **prefill（pos_start=0，T 个 token）**：还原为标准的 \( T \times T \) 严格上三角。
- **decode（pos_start=p，1 个 token）**：唯一的新 token 绝对位置 \( p = \text{pos\_end}-1 \)，没有任何 \( j \) 满足 \( j > p \)，掩码行**全 False**——这一个 token 可以看到缓存里的全部 key 及它自己，这正是我们想要的。

两阶段对照表（以 prefill 4 个 token、随后 decode 两次为例，记 prompt 长度 \( L{=}4 \)）：

| 阶段 | 输入 token 数 | pos_start | pos_end | 切片后 mask 形状 | current_pos 变化 | 缓存内 token 数 |
| --- | --- | --- | --- | --- | --- | --- |
| prefill | 4 | 0 | 4 | (4, 4) | 0 → 4 | 0 → 4 |
| decode 1 | 1 | 4 | 5 | (1, 5) | 4 → 5 | 4 → 5 |
| decode 2 | 1 | 5 | 6 | (1, 6) | 5 → 6 | 5 → 6 |

**为什么加两个前导维度？** 注释写得很清楚（L84-L89）：注意力分数形状是 `(batch, num_heads, num_tokens, total_keys)`，`(1, 1, ·, ·)` 的掩码可对 batch 维与头维广播，同一份掩码服务所有头。

**无 cache 与有 cache 的算力差**：无 cache 生成时每步都对全序列前向，生成 \( T \) 个 token 的总前向 token 数约为 \( T(T+1)/2 \)（平方级）；有 cache 时是 prefill 的 \( L \) 个加每步 1 个（线性级）。这就是 u1-l2 里"KV 缓存加速"的来源。

#### 4.2.3 源码精读

**① 游标初始化**。`__init__` 末尾把位置游标挂在模型上：[qwen3.py:L56](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L56)

**② 越界检查 + pos_start/pos_end 计算**。这里决定本次输入的绝对位置区间；`ValueError` 的判断是**严格大于**——`pos_end` 恰好等于 `context_length` 仍然放行（RoPE 表恰好有 0 到 `context_length-1` 共 `context_length` 个位置，够用）：[qwen3.py:L63-L70](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L63-L70)

**③ 有 cache 的分支：先推游标，再切掩码**。注意 `self.current_pos = pos_end` 位于掩码构造之前、越界检查之后——这个顺序保证了"被拒绝的调用不改游标"。掩码先造 `pos_end × pos_end` 的全序列因果掩码，再用 `[pos_start:pos_end, :pos_end]` 切出"新 token 行 × 全部 key 列"：[qwen3.py:L72-L76](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L72-L76)

**④ 无 cache 的分支：经典严格上三角**，形状 `(num_tokens, num_tokens)`：[qwen3.py:L77-L80](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L77-L80)

**⑤ 升维广播**。源码注释直接解释了 `(1,1,·,·)` 的用意——与 `(batch, heads, ·, ·)` 的注意力分数对齐：[qwen3.py:L84-L90](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L84-L90)

**⑥ 逐层读写缓存**。模型层把整只 cache 拆成每层的 `blk_cache` 喂给 block，再把 block 返回的新缓存写回对应槽位；同时把 `pos_start` 作为 `start_pos` 传下去（RoPE offset 的来源，见 4.3.3）：[qwen3.py:L92-L98](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L92-L98)

**⑦ reset**。归零游标，一行了事：[qwen3.py:L104-L105](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L104-L105)

**被拒绝的调用不污染状态**——这不是巧合，仓库测试明确验证了它。测试先 prefill 7 个 token，再喂 2 个 token 触发 `ValueError`（9 > 8），随后只喂 1 个 token：此时 `pos_end = 8` 恰好等于 `context_length`，调用成功，且结果与"整段 8 个 token 无 cache 前向"完全一致：[test_qwen3_context_limits.py:L58-L71](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3_context_limits.py#L58-L71)

**reset 的必要性**：仓库所有生成函数都遵守"新建 cache + `reset_kv_cache()` 成对出现"的约定，例如 `generate_text_basic_cache` 的开头三行——新建每层缓存、游标归零、prefill 取最后一个位置的 logits：[ch02.py:L73-L75](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L73-L75)

#### 4.2.4 代码实践

**实践目标**：不依赖模型，用纯 torch 手工复现 `forward` 里的掩码构造，验证 4.2.2 的切片公式。

**操作步骤**（示例代码）：

```python
import torch

# 场景：prefill 4 个 token 后，decode 1 个 token
pos_start, pos_end = 4, 5

full = torch.triu(torch.ones(pos_end, pos_end, dtype=torch.bool), diagonal=1)
mask = full[pos_start:pos_end, :pos_end]

print("全序列掩码 (5x5):\n", full.int())
print("切片后掩码形状:", mask.shape)
print("切片后内容:", mask.int())   # 期望 [[0, 0, 0, 0, 0]]

# 对照：prefill 自己的掩码（pos_start=0, 4 个 token）
prefill_mask = torch.triu(torch.ones(4, 4, dtype=torch.bool), diagonal=1)
print("prefill 掩码:\n", prefill_mask.int())
```

**需要观察的现象**：`full` 是 5×5 严格上三角；切片取出最后一行（绝对位置 4 的那个新 token），全部为 0（False）。

**预期结果**：切片后形状为 `torch.Size([1, 5])`，内容全 0——新 token 可以关注包括自身在内的全部 5 个 key；而 `prefill_mask` 的第 0 行是 `[0,1,1,1]`（第一个 token 看不到后面三个）。

#### 4.2.5 小练习与答案

**练习 1**：若 prefill 后忘记调用 `reset_kv_cache()` 就带着**新建的空 cache** 开始第二次生成，源码层面会错在哪里？

**参考答案**：`forward` 里 `pos_start = self.current_pos`（L64）会读到上一次残留的非零游标，而新 cache 每层都是 `None`。于是 RoPE 从错误的 `offset` 开始旋转（新 token 被当成"序列中段"），掩码按 `[旧 pos_start : pos_end, :pos_end]` 切出"行 1 列多于注意力分数列数"的形状，与 GQA 里只有 1 个 key 的注意力分数无法正确对齐。具体表现是形状不匹配报错还是静默错位取决于当时的张量形状，**待本地验证**；但无论如何结果都是错的——这就是 reset 必须与新建 cache 成对出现的原因。

**练习 2**：`context_length = 8` 时，一次 `pos_end = 8` 的前向会触发 `ValueError` 吗？为什么？

**参考答案**：不会。L66 的条件是 `pos_end > context_length`（严格大于），`pos_end = 8` 恰好等于上下文长度是允许的：RoPE 的 cos/sin 表（u2-l4 的 `compute_rope_params`）按 `context_length` 个位置预计算，位置 0–7 都有角度可用；第 9 个位置（`pos_end = 9`）才会报错。

**练习 3**：为什么 `self.current_pos = pos_end`（L73）必须放在越界检查（L66-L70）**之后**？

**参考答案**：如果先更新游标再检查，一次越界调用会把游标推到非法值，即使调用者随后改喂短输入，位置也已错位，缓存与游标永久失配。放在检查之后，被拒绝的调用"什么事都没发生"，`cache` 未被 `update`、游标未动，之前的前缀仍可继续使用——这正是仓库测试 `test_rejected_decode_preserves_cache_and_allows_the_last_position` 断言的行为。

### 4.3 GQA 中的 cache 拼接与两阶段闭环

#### 4.3.1 概念说明

模型层把每层的 `(k, v)` 交给 GQA，真正"用缓存"的是 `GroupedQueryAttention.forward`。它做三件事：

1. **增量拼接**：新 token 投影出的 `keys_new` / `values_new` 沿 **token 维（dim=2）** 与缓存里的旧张量 `torch.cat` 到一起。注意力分数天然变成"新 query × 全部 key"。
2. **压缩存储**：拼接发生在 `repeat_interleave` 展开**之前**，所以缓存里存的是 8 个 KV 组的压缩版（u2-l3 的结论在这里落到代码）。展开成 16 个头只发生在本次前向内部、用完即弃。
3. **RoPE offset 闭环**：`start_pos`（即模型层算出的 `pos_start`）作为 `apply_rope` 的 `offset`，让新 token 按绝对位置旋转；同时缓存里存的是**已旋转**的旧 K，新旧 K 因此处于同一坐标系。

还有一个首次 prefill 的小机关：`KVCache` 槽位初始为 `None`，GQA 收到 `cache=None` 就走"无旧缓存"分支——所以"prefill"并不是特殊代码路径，而是"缓存拼接逻辑的第一次执行"。

#### 4.3.2 核心流程

GQA 前向中与缓存相关的部分：

```text
x: (b, num_tokens, d) ─ 投影/分头/QK-Norm（u2-l3）─▶ queries, keys_new, values_new
        │
        ├─ queries   = apply_rope(queries,   cos, sin, offset=start_pos)
        ├─ keys_new  = apply_rope(keys_new,  cos, sin, offset=start_pos)   ← 新 K 先旋转
        │
        ├─ cache 非空: keys = cat([prev_k, keys_new], dim=2)   ← token 维拼接
        │               values = cat([prev_v, values_new], dim=2)
        └─ cache 为空: keys, values = keys_new, values_new     ← 首次 prefill
        │
        ├─ next_cache = (keys, values)        ← 存的是压缩版（尚未展开）
        │
        ├─ keys   = keys.repeat_interleave(group_size, dim=1)   ← 只为本次注意力展开
        ├─ values = values.repeat_interleave(group_size, dim=1)
        │
        └─ attn = softmax(masked_fill(q@k^T / √d_h, mask)) @ values
```

decode 一步的形状账（沿用 4.2.2 的例子，b=1，`n_kv_groups=8`，`head_dim=128`，缓存已有 4 个 token）：

| 张量 | 形状 | 说明 |
| --- | --- | --- |
| `keys_new` | (1, 8, 1, 128) | 新 token 的 K（已旋转） |
| `prev_k` | (1, 8, 4, 128) | 缓存中的旧 K |
| 拼接后 `keys` | (1, 8, 5, 128) | dim=2 从 1 → 5 |
| 展开后 `keys` | (1, 16, 5, 128) | 8 组 → 16 头，仅本次前向有效 |
| `attn_scores` | (1, 16, 1, 5) | 1 个 query × 5 个 key，与掩码 (1,1,1,5) 对齐 |

#### 4.3.3 源码精读

**① RoPE 按绝对位置旋转新 Q/K**。`start_pos` 就是模型层传下来的 `pos_start`（L95），在此进入 `apply_rope` 的 `offset`——u2-l4 讲过的 `cos[offset:offset+seq_len]` 切片在此生效；缓存中的旧 K 上一次前向时已按各自绝对位置旋转过，因此拼接后新旧 K 可直接做内积：[qwen3.py:L203-L204](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L203-L204)

**② 增量拼接——本讲的核心六行**。`dim=2` 是分头后的 token 维；`next_cache` 在 `repeat_interleave` 之前创建，绑定的自然是**压缩版**张量：[qwen3.py:L206-L213](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L206-L213)

> 细读一处"看似防御、实际无效"的代码：else 分支里的 `start_pos = 0  # reset RoPE`（L211）发生在 `apply_rope` **之后**，而 `start_pos` 在其后不再被使用——它对结果没有任何影响。真正保证无 cache 时从 0 开始的是模型层的 `pos_start = self.current_pos if cache is not None else 0`（L64）。读源码时能识别这类"死代码"是很好的习惯。

**③ 每步 decode 重新展开、用完即弃**。`repeat_interleave` 在 `next_cache` 赋值之后才执行，且重新绑定了局部名 `keys` / `values`，不影响已存入缓存的压缩张量：[qwen3.py:L215-L217](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L215-L217)

**④ 掩码在此消费**。注意力分数 (b, 头, 新 token, 全部 key) 被 `(1,1,·,·)` 的掩码广播填充 `-inf`，再除以 \( \sqrt{d_h} \) 后 softmax——4.2 节构造的掩码在这里完成使命：[qwen3.py:L220-L222](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L220-L222)

**⑤ 标准用法回看**。`generate_text_basic_cache` 中 prefill 取 `[:, -1]` 的 logits 选出第一个新 token，之后每步只把 **1 个** token 喂回模型（对比无 cache 版每步拼接整段 `token_ids`）；`if step + 1 < max_new_tokens` 跳过最后一次无用的前向（最后一个 token 生成后不再需要为"下一个"算 logits）：[ch02.py:L75-L87](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L75-L87)

#### 4.3.4 代码实践

**实践目标**：用缩小模型直接打开缓存"黑盒"，观察 token 维增长与压缩存储；并验证"prefill + 逐 token decode"与"整段一次前向"结果一致（与仓库测试同一断言方式）。

**操作步骤**（示例代码；配置取自 `tests/test_qwen3_context_limits.py` 的缩小配置，随机权重即可，无需下载）：

```python
import torch
from reasoning_from_scratch.qwen3 import Qwen3Model, KVCache

cfg = {"vocab_size": 32, "context_length": 8, "emb_dim": 16, "n_heads": 4,
       "n_layers": 2, "hidden_dim": 32, "head_dim": 4, "qk_norm": True,
       "n_kv_groups": 2, "rope_base": 1_000_000.0, "dtype": torch.float32}
torch.manual_seed(123)
model = Qwen3Model(cfg).eval()

tok = torch.tensor([[1, 2, 3, 4]])
with torch.inference_mode():
    cache = KVCache(n_layers=cfg["n_layers"])
    model.reset_kv_cache()
    model(tok, cache=cache)                      # prefill
    print("prefill 后 K 形状:", cache.get(0)[0].shape)   # (1, 2, 4, 4)

    model(torch.tensor([[5]]), cache=cache)      # decode 1
    print("decode 后 K 形状:", cache.get(0)[0].shape)    # (1, 2, 5, 4)

    # 等价性验证：与整段 5 token 无 cache 前向比对
    out_cached = model(torch.tensor([[6]]), cache=cache)[:, -1:]
    out_full   = model(torch.tensor([[1, 2, 3, 4, 5, 6]]))[:, -1:]
    torch.testing.assert_close(out_cached, out_full, rtol=1e-5, atol=1e-5)
    print("两条路径的 logits 一致 ✓")
```

**需要观察的现象**：`cache.get(0)[0]` 的形状从 `(1, 2, 4, 4)` 变为 `(1, 2, 5, 4)`——只有 dim=2（token 维）在涨；第 1 维是 2（`n_kv_groups`），**不是** 4（`n_heads`），印证缓存存的是压缩版。

**预期结果**：两条路径的最后位置 logits 一致（float32 下 `rtol=atol=1e-5` 通过；这一比对方式与 [test_qwen3_context_limits.py:L69-L71](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3_context_limits.py#L69-L71) 完全相同，该测试在 CI 中持续验证此等价性）。

#### 4.3.5 小练习与答案

**练习 1**：缓存里的 `keys` 为什么不存 `repeat_interleave` 之后的 16 头版本？

**参考答案**：16 头 version 只是 8 组数据的机械复制（每组重复 `group_size=2` 次），存展开版显存直接翻倍却不增加任何信息；存压缩版、每次前向临时展开，与 u2-l3"GQA 压缩 KV cache"的动机一致（0.6B 满上下文约 4.4 GiB vs 8.8 GiB）。

**练习 2**：`torch.cat([prev_k, keys_new], dim=2)` 为什么必须拼在 dim=2，而不能是 dim=1 或 dim=3？

**参考答案**：分头后张量形状是 `(b, n_kv_groups, tokens, head_dim)`：dim=1 是 KV 组维（新旧组相同，拼接无意义）、dim=3 是头维（新旧 token 的特征维相同，拼了语义就乱了）；只有 dim=2 是 token/序列维，"旧 token 的 K 在前、新 token 的 K 在后"恰好与掩码列的绝对位置顺序对齐。

**练习 3**：缓存中保存的旧 K 是"旋转前"还是"旋转后"的？依据是哪一行？

**参考答案**：旋转后。`keys_new = apply_rope(keys_new, cos, sin, offset=start_pos)`（L204）先执行，`torch.cat` 与 `next_cache`（L208-L213）后执行，所以进入缓存的 K 已带绝对位置信息；decode 时旧 K 不需要（也不能）再旋转，只有新 token 需要 `offset` 来取对角度。

## 5. 综合实践

**任务**：用缩小配置的模型**手动执行一次完整的两阶段生成**，借助 forward pre-hook 从模型外部"偷看"每次前向的真实掩码形状，最后验证越界报错。三步对应本讲三个模块：掩码调度（4.2）、缓存拼接（4.3）、容器与游标（4.1/4.2）。

**操作步骤**（示例代码，仓库根目录运行 `python` 即可；也可存成脚本）：

```python
import torch
from reasoning_from_scratch.qwen3 import Qwen3Model, KVCache

cfg = {"vocab_size": 32, "context_length": 8, "emb_dim": 16, "n_heads": 4,
       "n_layers": 2, "hidden_dim": 32, "head_dim": 4, "qk_norm": True,
       "n_kv_groups": 2, "rope_base": 1_000_000.0, "dtype": torch.float32}
torch.manual_seed(123)
model = Qwen3Model(cfg).eval()

# --- 用 forward pre-hook 捕获喂给第一个 block 的 (x, mask, ...) ---
masks = []
def spy(module, args):
    masks.append(tuple(args[1].shape))            # args = (x, mask, cos, sin)
handle = model.trf_blocks[0].register_forward_pre_hook(spy)

cache = KVCache(n_layers=cfg["n_layers"])
model.reset_kv_cache()

with torch.inference_mode():
    # 第 1 步：prefill 整段 4 token prompt
    model(torch.tensor([[10, 11, 12, 13]]), cache=cache)
    print(f"prefill   | current_pos: 0 -> {model.current_pos} | mask: {masks[-1]}")

    # 第 2 步：decode 两个 token（各 1 次前向）
    for tok_id in (20, 21):
        model(torch.tensor([[tok_id]]), cache=cache)
        print(f"decode    | current_pos -> {model.current_pos} | mask: {masks[-1]}")

    # 第 3 步：再喂 3 个 token，pos_end = 6 + 3 = 9 > 8，应当报错
    try:
        model(torch.tensor([[30, 31, 32]]), cache=cache)
    except ValueError as e:
        print(f"越界触发  | ValueError: {e}")
    print(f"被拒之后  | current_pos 仍是 {model.current_pos}（未被污染）")

handle.remove()
```

**需要观察的现象**：

1. 三次前向的 mask 形状依次变化，`current_pos` 依次为 4、5、6；
2. 越界调用抛出 `ValueError`，且异常后 `current_pos` 保持在 6；
3. hook 只在真正进入 block 循环时触发——越界那次调用在 block 循环之前就被拒绝，`masks` 长度仍是 3。

**预期结果**（可从源码 L63-L90 直接推出）：

```text
prefill   | current_pos: 0 -> 4 | mask: (1, 1, 4, 4)
decode    | current_pos -> 5    | mask: (1, 1, 1, 5)
decode    | current_pos -> 6    | mask: (1, 1, 1, 6)
越界触发  | ValueError: Sequence length 9 exceeds the model's context length of 8 tokens.
被拒之后  | current_pos 仍是 6（未被污染）
```

逐条对照：prefill 是 `(4, 4)` 上三角升维；每次 decode 掩码列数 = 缓存 token 数 + 1；错误信息与仓库测试的正则 `r"Sequence length 9 exceeds .*8 tokens"`（[test_qwen3_context_limits.py:L64](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3_context_limits.py#L64)）一致。

**延伸（可选）**：把 `model.reset_kv_cache()` 注释掉再运行第 1 步之后的新对话（新建 cache），观察并记录错误现象，回到 4.2.5 练习 1 核对你的解释（具体报错形式待本地验证）。

## 6. 本讲小结

- **KVCache 是"每层一个槽位"的极简容器**（纯 Python 列表），槽位存 GQA 返回的 `(keys, values)` 元组；与 `qwen3_optimized.py` 的预分配版对比是 u10-l1 的伏笔。
- **`current_pos` 是挂在模型上的位置游标**，四站生命周期：初始化 0 → 读作 `pos_start` → 前向成功后更新为 `pos_end` → `reset_kv_cache()` 归零；它与"新建 cache"必须成对出现。
- **两阶段掩码统一于一个切片公式** \(\ \mathrm{mask}[i,j]=\mathbb{1}[j>\mathrm{pos\_start}+i]\ \)：prefill 还原为严格上三角 `(T, T)`，decode 变为全 False 的 `(1, L+1)`，升两维后向 batch 与头广播。
- **越界检查先于一切状态修改**：`pos_end > context_length` 抛 `ValueError`（恰好等于不抛），被拒的调用既不动游标也不动缓存。
- **GQA 沿 token 维（dim=2）`torch.cat` 增量拼接**，且发生在 `repeat_interleave` 之前——缓存存的是 8 组压缩版 K/V；RoPE 的 `offset=start_pos` 让新 token 按绝对位置旋转，与缓存中已旋转的旧 K 同坐标系。
- **首次 prefill 不是特殊路径**：`KVCache` 槽位初始为 `None`，GQA 的 `cache is None` 分支自然处理它。

## 7. 下一步学习建议

单元二只剩最后一讲：**u2-l6 Qwen3Tokenizer 与聊天模板**——模型与缓存都已就绪，下一讲解决"文本如何变成 token id"以及 `<|im_start|>`、`<think>` 等特殊 token 如何包装输入。之后的单元三（文本生成）会把本讲的 KV cache 交给 `generate_text_basic_stream_cache` 等四代生成函数实战。

如果想提前看"缓存的另一种工程实现"，可以直接跳读 [reasoning_from_scratch/qwen3_optimized.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_optimized.py) 的 `KVCache`（预分配 + 视图写入）并与本讲对比，u10-l1 会系统讲解；批处理场景下多条序列共享缓存的问题（left-padding、`shrink_kv_cache_inplace`）则在 u10-l2 展开。
